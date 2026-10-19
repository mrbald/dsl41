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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dsl41.runner_control import ControlClient, ControlClientError

_TEXT = (
    "insert_job: recovery_box\njob_type: b\n\n"
    "insert_job: recovery_child\njob_type: c\ncommand: x\nmachine: m1\n"
    "box_name: recovery_box\n\n"
    "insert_job: recovery_other\njob_type: c\ncommand: x\nmachine: m1\n"
)
_TIMEOUT = 15.0


def _command(*args: str) -> list[str]:
    executable = os.environ.get("DSL41_TUI_EXECUTABLE") or os.environ.get(
        "DSL41_INSTALLED_EXECUTABLE"
    )
    return [executable, *args] if executable else [sys.executable, "-m", "dsl41", *args]


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


class _Estate:
    """A real CLI engine, sealed and resumed while its viewer remains attached."""

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

    async def seal_reopen(self) -> None:
        client = ControlClient(self.socket)
        try:
            before = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=_TIMEOUT)
        finally:
            await client.close()
        self.source.write_text(
            _TEXT + "\ninsert_job: recovery_added\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        process = await asyncio.create_subprocess_exec(
            *_command(
                "seal",
                "--run-root",
                str(self.run_root),
                "--next",
                str(self.source),
                "--next-as-machine",
                "m1",
                "--force-seal",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.diagnostics,
            env=_environment(),
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=_TIMEOUT)
            (self.diagnostics / "seal.log").write_bytes(output)
            assert process.returncode == 0, output.decode(errors="replace")
        finally:
            await _stop(process)
        assert self.process is not None
        assert await asyncio.wait_for(self.process.wait(), timeout=5) == 3
        await self.start(resume=True)
        client = ControlClient(self.socket)
        try:
            after = await asyncio.wait_for(client.request({"cmd": "status"}), timeout=_TIMEOUT)
            assert after["baseline_id"] != before["baseline_id"]
            assert "recovery_added" in after["jobs"]
            assert after["jobs"]["recovery_child"]["on_hold"]
        finally:
            await client.close()

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
                    if text.encode() in raw[after:]:
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
            await estate.seal_reopen()
            await until("trace baseline changed", after=mark)
            mark = len(raw)
            os.write(master, b"/")
            await until("recovery_child", after=mark)
            os.write(master, b"\x1b")
            await until("recovery_added", after=mark)
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


def _browser_drive(
    engine: str, url: str, diagnostics: Path, transition: Callable[[], None]
) -> None:
    from playwright.sync_api import sync_playwright

    output: list[bytes] = []
    resizes: list[tuple[int, int]] = []
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.set_default_timeout(int(_TIMEOUT * 1000))
        page.on("pageerror", lambda error: errors.append(str(error)))

        def socket_opened(ws: Any) -> None:
            ws.on(
                "framereceived",
                lambda data: output.append(data if isinstance(data, bytes) else data.encode()),
            )

            def sent(data: str | bytes) -> None:
                frame = json.loads(data)
                if frame[0] == "resize":
                    resizes.append((frame[1]["width"], frame[1]["height"]))

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
                    if text.encode() in b"".join(output[after:]):
                        assert not errors, errors
                        return
                    page.wait_for_timeout(20)
                raise AssertionError(f"{engine} did not render {text!r}; see {diagnostics}")

            until("recovery_box")
            page.keyboard.press("/")
            until("filter: substring")
            page.keyboard.type("recovery_child", delay=20)
            until("1/3")
            page.keyboard.press("Enter")
            resize_mark = len(resizes)
            render_mark = len(output)
            page.set_viewport_size({"width": 1100, "height": 760})
            deadline = time.monotonic() + _TIMEOUT
            while time.monotonic() < deadline:
                if len(resizes) > resize_mark:
                    columns, rows = resizes[-1]
                    # DL-391: the app must paint its footer at the acknowledged terminal size
                    # before a mutation can wrap its recovery command at that size.
                    footer = re.compile(
                        rb"\x1b\[" + str(rows).encode() + rb";1H(.*?)(?=\x1b\[\d+;\d+H)",
                        re.DOTALL,
                    )
                    rendered = b"".join(output[render_mark:])
                    lines = [
                        re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", match[1]).decode(errors="replace")
                        for match in footer.finditer(rendered)
                    ]
                    if any(len(line) == columns and "detach" in line for line in lines):
                        break
                page.wait_for_timeout(20)
            else:
                raise AssertionError(f"{engine} did not repaint after resize; see {diagnostics}")
            mark = len(output)
            page.keyboard.press("h")
            until("--claimed-actor", after=mark)
            page.keyboard.press("k")
            until("KILLJOB recovery_child")
            mark = len(output)
            page.keyboard.press("Escape")
            until("cancelled", after=mark)
            mark = len(output)
            transition()
            until("trace baseline changed", after=mark)
            mark = len(output)
            page.keyboard.press("/")
            until("recovery_child", after=mark)
            page.keyboard.press("Escape")
            until("recovery_added", after=mark)
            page.keyboard.press("q")
            page.locator("body.-closed").wait_for(state="attached")
            assert not errors, errors
        finally:
            diagnostics.mkdir(parents=True, exist_ok=True)
            (diagnostics / "terminal.raw").write_bytes(b"".join(output))
            (diagnostics / "page-errors.txt").write_text("\n".join(errors))
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
                    asyncio.run_coroutine_threadsafe(estate.seal_reopen(), loop).result(
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
