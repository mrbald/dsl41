"""DL-391: bounded PTY and served-browser interaction checks.

The browser lane is opt-in with DSL41_BROWSER_TESTS=1. Both lanes retain
raw terminal output under pytest's temporary directory on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import signal
import socket
import struct
import sys
import termios
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from dsl41.runner_control import ControlClient, ControlClientError

_TEXT = (
    "insert_job: recovery_box\njob_type: b\n\n"
    "insert_job: recovery_child\njob_type: c\ncommand: x\nmachine: m1\n"
    "box_name: recovery_box\n\n"
    "insert_job: recovery_other\njob_type: c\ncommand: x\nmachine: m1\n"
)
_TIMEOUT = 15.0
# DL-391: after Esc clears the filter, the driver waits for the table title that only a cleared
# filter renders. The event log already names every job, so a job name proves nothing, and a `q`
# sent within textual's escape delay of the Esc is parsed as alt+q and never detaches.
_UNFILTERED = "jobs 3/3"
# DL-391: the footer draws "esc clear" only while the filter line has focus, so it proves `/`
# opened the line. A kept filter shows its value, and the event log already names that job.
_FILTER_OPEN = "esc clear"
_SGR = re.compile(rb"\x1b\[[\d;:]*m")


def _sgr_free(data: bytes | bytearray) -> bytes:
    """The rendered bytes without colour codes, which split a footer key from its label."""
    return _SGR.sub(b"", data)


def _command(*args: str) -> list[str]:
    executable = os.environ.get("DSL41_TUI_EXECUTABLE") or os.environ.get(
        "DSL41_INSTALLED_EXECUTABLE"
    )
    return [executable, *args] if executable else [sys.executable, "-m", "dsl41", *args]


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


class _Estate:
    """A real CLI engine, stopped and resumed while its viewer remains attached."""

    def __init__(self, base: Path, diagnostics: Path) -> None:
        self.run_root = base / "run"
        self.socket = self.run_root / "control.sock"
        self.source = base / "estate.jil"
        self.source.write_text(_TEXT)
        self.diagnostics = diagnostics
        self.process: asyncio.subprocess.Process | None = None
        self.generation = 0

    async def start(self, *, resume: bool = False) -> None:
        self.generation += 1
        args = ["run", "--run-root", str(self.run_root), "--as-machine", "m1"]
        if resume:
            args.append("--resume")
        with (self.diagnostics / f"engine-{self.generation}.log").open("wb") as log:
            self.process = await asyncio.create_subprocess_exec(
                *_command(*args, str(self.source)),
                stdout=log,
                stderr=log,
                cwd=self.diagnostics,
                env=_environment(),
                start_new_session=True,
            )
        client = ControlClient(self.socket)
        try:
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline:
                assert self.process.returncode is None, self.diagnostics
                try:
                    answer = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=2)
                except (ControlClientError, TimeoutError):
                    await asyncio.sleep(0.03)
                else:
                    assert answer.get("ok"), answer
                    return
            raise AssertionError(f"engine did not start; see {self.diagnostics}")
        except BaseException:
            await _stop(self.process)
            raise
        finally:
            await client.close()

    async def restart(self) -> None:
        """Stop the engine with SIGTERM, resume the same root and force-start `recovery_other`,
        whose FAILURE the still-attached viewer must then show."""
        client = ControlClient(self.socket)
        try:
            before = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=_TIMEOUT)
        finally:
            await client.close()
        assert self.process is not None
        self.process.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(self.process.wait(), timeout=_TIMEOUT) == 0
        await self.start(resume=True)
        client = ControlClient(self.socket)
        try:
            after = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=_TIMEOUT)
            assert after["baseline_id"] == before["baseline_id"]
            assert after["jobs"]["recovery_child"]["on_hold"]
        finally:
            await client.close()
        # a state the viewer has not seen before the restart: the attached viewer must read it
        # from the new engine
        process = await asyncio.create_subprocess_exec(
            *_command(
                "sendevent", "FORCE_STARTJOB", "-J", "recovery_other", "-S", str(self.socket)
            ),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.diagnostics,
            env=_environment(),
        )
        assert await asyncio.wait_for(process.wait(), timeout=_TIMEOUT) == 0

    async def close(self) -> None:
        if self.process is not None:
            await _stop(self.process)


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Reap the whole UI/serve session, including per-browser app children."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=5)


async def _held(client: ControlClient) -> bool:
    read = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=_TIMEOUT)
    jobs = read["jobs"]
    assert not jobs["recovery_box"]["on_hold"]
    assert not jobs["recovery_other"]["on_hold"]
    return bool(jobs["recovery_child"]["on_hold"])


async def _wait_held(client: ControlClient) -> None:
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        if await _held(client):
            return
        await asyncio.sleep(0.03)
    raise AssertionError("the filtered, selected child never received ON_HOLD")


def test_real_terminal_filter_selection_resize_confirm_cancel_and_detach(
    short_root: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        estate = _Estate(short_root, tmp_path)
        await estate.start()
        client = ControlClient(estate.socket)
        master, slave = pty.openpty()
        raw = bytearray()
        process: asyncio.subprocess.Process | None = None
        reader_task: asyncio.Task[None] | None = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 150, 0, 0))
            os.set_blocking(master, False)
            process = await asyncio.create_subprocess_exec(
                *_command("ui", "--socket", str(estate.socket)),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env={**_environment(), "TERM": "xterm-256color"},
                cwd=tmp_path,
            )
            os.close(slave)
            slave = -1

            async def drain_terminal() -> None:
                while True:
                    try:
                        data = os.read(master, 1024 * 1024)
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
                    except OSError:
                        return
                    else:
                        if not data:
                            return
                        raw.extend(data)

            reader_task = asyncio.create_task(drain_terminal())

            async def until(text: str, *, after: int = 0) -> None:
                deadline = time.monotonic() + _TIMEOUT
                while time.monotonic() < deadline:
                    if text.encode() in _sgr_free(raw[after:]):
                        return
                    assert process is not None and process.returncode is None, raw.decode(
                        errors="replace"
                    )
                    await asyncio.sleep(0.02)
                raise AssertionError(f"terminal did not render {text!r}; see {tmp_path}")

            await until("recovery_box")
            os.write(master, b"/")
            await until("filter: substring")
            for char in b"recovery_child":
                os.write(master, bytes([char]))
                await asyncio.sleep(0.02)
            await until("1/3")
            os.write(master, b"\r")
            await asyncio.sleep(0.05)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 110, 0, 0))
            os.kill(process.pid, signal.SIGWINCH)
            mark = len(raw)
            os.write(master, b"h")
            await _wait_held(client)
            await until("--claimed-actor", after=mark)
            os.write(master, b"k")
            await until("KILLJOB recovery_child")
            mark = len(raw)
            os.write(master, b"\x1b")
            await until("cancelled", after=mark)
            mark = len(raw)
            await estate.restart()
            await until("FAILURE", after=mark)
            mark = len(raw)
            os.write(master, b"/")
            await until(_FILTER_OPEN, after=mark)
            os.write(master, b"\x1b")
            await until(_UNFILTERED, after=mark)
            os.write(master, b"q")
            assert await asyncio.wait_for(process.wait(), timeout=5) == 0
            assert await _held(client)
            assert estate.socket.exists()
        finally:
            if process is not None:
                await _stop(process)
            if reader_task is not None:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task
            os.close(master)
            if slave >= 0:
                os.close(slave)
            (tmp_path / "terminal.raw").write_bytes(raw)
            await client.close()
            await estate.close()

    asyncio.run(scenario())


def _footer_ready(rendered: bytes, columns: int, rows: int) -> bool:
    """DL-391: require a complete footer at its acknowledged size and table focus."""
    if columns <= 0 or rows <= 0:
        return False
    cells: list[str | None] = [None] * columns
    row, column = 0, 0
    for token in re.findall(rb"\x1b\[[0-?]*[ -/]*[@-~]|[^\x1b]+|\x1b", rendered):
        position = re.fullmatch(rb"\x1b\[(\d+);(\d+)H", token)
        if position:
            row, column = int(position[1]), int(position[2])
            if not (1 <= row <= rows and 1 <= column <= columns):
                cells = [None] * columns
                row, column = 0, 0
        elif re.fullmatch(rb"\x1b\[[\d;:]*m", token):
            continue
        elif token.startswith(b"\x1b"):
            cells = [None] * columns
            row, column = 0, 0
        else:
            try:
                text = token.decode("utf-8")
            except UnicodeDecodeError:
                cells = [None] * columns
                row, column = 0, 0
                continue
            # DL-391: only observed one-cell writes prove this footer; unknown controls
            # or widths invalidate it until another complete, positioned repaint.
            for char in text:
                if char == "\n" and 1 <= row < rows:
                    row += 1
                    column = 0
                    continue
                if (
                    not (1 <= row <= rows and 1 <= column <= columns)
                    or unicodedata.category(char) in {"Mn", "Me", "Cf"}
                    or not char.isprintable()
                    or unicodedata.combining(char)
                    or unicodedata.east_asian_width(char) in {"W", "F"}
                ):
                    cells = [None] * columns
                    row, column = 0, 0
                    break
                if row == rows:
                    cells[column - 1] = char
                column += 1
    if any(cell is None for cell in cells):
        return False
    footer = "".join(cell for cell in cells if cell is not None)
    return all(label in footer for label in (" s start ", " k kill ", " q detach "))


_FOOTER = b" s start  k kill  q detach "


@pytest.mark.parametrize("segmented", [False, True])
def test_browser_footer_accepts_complete_positioned_repaints(segmented: bool) -> None:
    footer = _FOOTER.decode() + "▏^p palette "
    if segmented:
        rendered = b"".join(
            f"\x1b[38;{column}H\x1b[32m{char}\x1b[0m".encode()
            for column, char in enumerate(footer, 1)
        )
    else:
        rendered = f"\x1b[38;1H\x1b[32m{footer}\x1b[0m".encode()
    assert _footer_ready(rendered, len(footer), 38)


def test_browser_footer_accepts_interleaved_observed_fragments() -> None:
    fragments = (
        " s start ",
        " f force ",
        " k kill ",
        " q detach ",
        " d details ",
        " t triggers ",
        " m zoom log ",
        " o out/err ",
        " r refresh ",
        " f1 help ",
        " / filte",
        "▏^",
        "p palett",
        "e ",
    )
    output = bytearray()
    column = 1
    for fragment in fragments:
        output.extend(f"\x1b[38;{column}H\x1b[32m{fragment}\x1b[0m".encode())
        output.extend(b"\x1b[13;120H  \x1b[13;122H\n")
        column += len(fragment)
    assert column == 123
    assert _footer_ready(bytes(output), 122, 38)


def test_browser_footer_accepts_complete_repaint_after_malformed_bytes() -> None:
    rendered = b"\x1b[38;1H\xff\x1b[38;1H" + _FOOTER
    assert _footer_ready(rendered, len(_FOOTER), 38)


@pytest.mark.parametrize(
    ("rendered", "columns", "rows"),
    [
        (b"\x1b[38;1H" + _FOOTER, len(_FOOTER), 37),
        (b"\x1b[38;1H" + _FOOTER, len(_FOOTER) - 1, 38),
        (b"\x1b[38;1H" + _FOOTER, len(_FOOTER) + 1, 38),
        (b"\x1b[38;2H" + _FOOTER[1:], len(_FOOTER), 38),
        (b"\x1b[38;1H" + b" esc clear  f1 help ".ljust(len(_FOOTER)), len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER.replace(b"detach", b"      "), len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[1;1H\x1b[2J", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[38;1H" + b" " * len(_FOOTER), len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\n", len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + b"\xff", len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[37;1H\nxxxxx", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[37;27Hxx", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[39;1H", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[38;0H", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[37;1H\v", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\x1b[37;1H\f", len(_FOOTER), 38),
        (b"\x1b[38;1H" + _FOOTER + b"\t", len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + "界".encode(), len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + "\u1715".encode(), len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + "\ufe0f".encode(), len(_FOOTER) + 1, 38),
        (b"\x1b[38;1H" + _FOOTER + "\u20dd".encode(), len(_FOOTER) + 1, 38),
    ],
    ids=[
        "wrong-row",
        "too-wide",
        "incomplete",
        "missing-first-cell",
        "console-focus",
        "no-detach",
        "unsupported-control",
        "overwritten-detach",
        "line-feed",
        "invalid-utf8",
        "line-feed-into-footer",
        "overflow-on-other-row",
        "out-of-screen-cursor",
        "column-zero",
        "vertical-tab-on-other-row",
        "form-feed-on-other-row",
        "tab",
        "wide-character",
        "combining-spacing-mark",
        "variation-selector",
        "enclosing-mark",
    ],
)
def test_browser_footer_refuses_incomplete_or_unproven_repaints(
    rendered: bytes, columns: int, rows: int
) -> None:
    assert not _footer_ready(rendered, columns, rows)


def _websocket_geometry(url: str) -> tuple[int, int] | None:
    try:
        query = parse_qs(urlsplit(url).query)
        width, height = query["width"], query["height"]
        if len(width) != 1 or len(height) != 1:
            return None
        size = int(width[0]), int(height[0])
    except (KeyError, ValueError):
        return None
    return size if min(size) > 0 else None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("width=166&height=50", (166, 50)),
        ("width=166", None),
        ("width=x&height=50", None),
        ("width=0&height=50", None),
        ("width=166&height=-1", None),
        ("width=166&width=122&height=50", None),
    ],
)
def test_browser_initial_websocket_geometry(query: str, expected: tuple[int, int] | None) -> None:
    assert _websocket_geometry(f"ws://127.0.0.1/ws?{query}") == expected


def _browser_drive(
    engine: str, url: str, diagnostics: Path, transition: Callable[[], None]
) -> None:
    from playwright.sync_api import sync_playwright

    output: list[bytes] = []
    resizes: list[tuple[int, int]] = []
    resize_frames: list[int] = []
    frame_marks: dict[str, int] = {}
    initial_url: str | None = None
    initial_geometry: tuple[int, int] | None = None
    before_resize_geometry: tuple[int, int] | None = None
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(int(_TIMEOUT * 1000))
        page.on("pageerror", lambda error: errors.append(str(error)))

        def socket_opened(ws: Any) -> None:
            nonlocal initial_url, initial_geometry
            if initial_url is None:
                initial_url = ws.url
                initial_geometry = _websocket_geometry(initial_url)
            ws.on(
                "framereceived",
                lambda data: output.append(data if isinstance(data, bytes) else data.encode()),
            )

            def sent(data: str | bytes) -> None:
                frame = json.loads(data)
                if frame[0] == "resize":
                    resizes.append((frame[1]["width"], frame[1]["height"]))
                    resize_frames.append(len(output))

            ws.on("framesent", sent)

        page.on("websocket", socket_opened)
        page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.locator("body.-first-byte").wait_for(state="attached")
            terminal = page.locator(".xterm-helper-textarea")
            terminal.focus()

            def until(text: str, *, after: int = 0) -> None:
                deadline = time.monotonic() + _TIMEOUT
                while time.monotonic() < deadline:
                    if text.encode() in _sgr_free(b"".join(output[after:])):
                        assert not errors, errors
                        return
                    page.wait_for_timeout(20)
                raise AssertionError(f"{engine} did not render {text!r}; see {diagnostics}")

            until("recovery_box")
            assert initial_geometry is not None, f"unproven initial geometry: {initial_url}"
            page.keyboard.press("/")
            until("filter: substring")
            page.keyboard.type("recovery_child", delay=20)
            until("1/3")
            page.keyboard.press("Enter")
            before_resize_geometry = resizes[-1] if resizes else initial_geometry
            resize_mark = len(resizes)
            render_mark = len(output)
            frame_marks.update(resize=resize_mark, render=render_mark)
            page.set_viewport_size({"width": 1100, "height": 760})
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline:
                if len(resizes) > resize_mark and resizes[-1] != before_resize_geometry:
                    columns, rows = resizes[-1]
                    frame_marks["acknowledged_render"] = max(render_mark, resize_frames[-1])
                    if _footer_ready(
                        b"".join(output[frame_marks["acknowledged_render"] :]), columns, rows
                    ):
                        break
                page.wait_for_timeout(20)
            else:
                raise AssertionError(
                    f"{engine} did not repaint after resize: initial={initial_geometry}, "
                    f"before={before_resize_geometry}, sent={resizes}; see {diagnostics}"
                )
            mark = len(output)
            frame_marks["mutation"] = mark
            page.keyboard.press("h")
            until("--claimed-actor", after=mark)
            page.keyboard.press("k")
            until("KILLJOB recovery_child")
            mark = len(output)
            page.keyboard.press("Escape")
            until("cancelled", after=mark)
            mark = len(output)
            transition()
            until("FAILURE", after=mark)
            mark = len(output)
            page.keyboard.press("/")
            until(_FILTER_OPEN, after=mark)
            page.keyboard.press("Escape")
            until(_UNFILTERED, after=mark)
            page.keyboard.press("q")
            page.locator("body.-closed").wait_for(state="attached")
            assert not errors, errors
        finally:
            diagnostics.mkdir(parents=True, exist_ok=True)
            (diagnostics / "terminal.raw").write_bytes(b"".join(output))
            (diagnostics / "page-errors.txt").write_text("\n".join(errors))
            (diagnostics / "resize-evidence.json").write_text(
                json.dumps(
                    {
                        "engine": engine,
                        "initial_url": initial_url,
                        "initial_geometry": initial_geometry,
                        "before_resize_geometry": before_resize_geometry,
                        "resizes": resizes,
                        "resize_frames": resize_frames,
                        "frame_marks": frame_marks,
                        "frame_lengths": [len(frame) for frame in output],
                    },
                    indent=2,
                )
                + "\n"
            )
            browser.close()


@pytest.mark.skipif(
    os.environ.get("DSL41_BROWSER_TESTS") != "1",
    reason="served TUI browser lane requires DSL41_BROWSER_TESTS=1",
)
@pytest.mark.parametrize("browser_engine", ["chromium", "webkit", "firefox"])
def test_served_tui_real_browser_filter_resize_confirm_cancel_and_detach(
    browser_engine: str, short_root: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        estate = _Estate(short_root, tmp_path)
        await estate.start()
        client = ControlClient(estate.socket)
        process: asyncio.subprocess.Process | None = None
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            with (tmp_path / "serve.log").open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    *_command(
                        "serve",
                        "--socket",
                        str(estate.socket),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ),
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    cwd=tmp_path,
                    env=_environment(),
                )
                deadline = time.monotonic() + _TIMEOUT
                while time.monotonic() < deadline:
                    assert process.returncode is None, (tmp_path / "serve.log").read_text()
                    try:
                        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    except OSError:
                        await asyncio.sleep(0.03)
                    else:
                        writer.close()
                        await writer.wait_closed()
                        break
                else:
                    raise AssertionError(f"serve did not bind; see {tmp_path / 'serve.log'}")
                loop = asyncio.get_running_loop()

                def transition() -> None:
                    asyncio.run_coroutine_threadsafe(estate.restart(), loop).result(
                        timeout=_TIMEOUT * 2
                    )

                await asyncio.to_thread(
                    _browser_drive, browser_engine, f"http://127.0.0.1:{port}", tmp_path, transition
                )
                assert await _held(client)
                assert estate.socket.exists()
        finally:
            if process is not None:
                await _stop(process)
            await client.close()
            await estate.close()

    asyncio.run(scenario())
