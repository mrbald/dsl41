"""DL-391: bounded PTY and served-browser interaction checks.

The browser lane is opt-in with DSL41_BROWSER_TESTS=1. Both lanes retain
raw terminal output under pytest's temporary directory on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from dsl41.runner_control import ControlClient, ControlClientError
from tui_terminal import (
    Terminal,
    TerminalAcknowledgementError,
    selected_filter_ready,
    table_focus_ready,
)

_TEXT = (
    "insert_job: recovery_box\njob_type: b\n\n"
    "insert_job: recovery_child\njob_type: c\ncommand: x\nmachine: m1\n"
    "box_name: recovery_box\n\n"
    "insert_job: recovery_other\njob_type: c\ncommand: x\nmachine: m1\n"
)
_TIMEOUT = 15.0
# After Esc clears the filter, the driver waits for the table title that only a cleared
# filter renders. The event log already names every job, so a job name proves nothing, and a `q`
# sent within textual's escape delay of the Esc is parsed as alt+q and never detaches.
_UNFILTERED = "jobs 3/3"
# The footer draws "esc clear" only while the filter line has focus, so it proves `/`
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
                assert self.process.returncode is None, (
                    f"engine exited with code {self.process.returncode}; see {self.diagnostics}"
                )
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


async def _wait_barrier(channel: socket.socket, driver: asyncio.Task[int]) -> bytes:
    receive = asyncio.create_task(asyncio.get_running_loop().sock_recv(channel, 128))
    try:
        async with asyncio.timeout(_TIMEOUT):
            done, _pending = await asyncio.wait(
                (receive, driver), return_when=asyncio.FIRST_COMPLETED
            )
        if driver in done:
            driver.result()
            raise AssertionError("terminal driver finished before the barrier")
        return receive.result()
    finally:
        receive.cancel()
        await asyncio.gather(receive, return_exceptions=True)


def test_engine_start_reports_an_early_exit_and_yields(
    short_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = short_root / "startup-exit"
    os.mkfifo(gate)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_command",
        lambda *_args: [
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'rb', buffering=0).read(1); sys.exit(23)",
            str(gate),
        ],
    )

    async def scenario() -> None:
        estate = _Estate(short_root, tmp_path)
        gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
        ticks = 0
        finished = asyncio.Event()

        async def tick() -> None:
            nonlocal ticks
            while not finished.is_set():
                await asyncio.sleep(0.005)
                ticks += 1
                if ticks == 2:
                    assert os.write(gate_fd, b"x") == 1

        ticker = asyncio.create_task(tick())
        try:
            with pytest.raises(AssertionError, match="engine exited with code 23"):
                await asyncio.wait_for(estate.start(), timeout=2)
            assert estate.process is not None and estate.process.returncode == 23
            assert ticks > 1
            assert not estate.socket.exists()
        finally:
            finished.set()
            await ticker
            await estate.close()
            os.close(gate_fd)

    asyncio.run(scenario())


def test_barrier_wait_preserves_the_drivers_early_error() -> None:
    async def scenario() -> None:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        error = AssertionError("terminal did not acknowledge complete filter and selected child")

        async def fail() -> int:
            raise error

        driver = asyncio.create_task(fail())
        try:
            with pytest.raises(AssertionError) as caught:
                await asyncio.wait_for(_wait_barrier(parent, driver), timeout=2)
            assert caught.value is error
        finally:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
            parent.close()
            child.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("eof", [False, True])
def test_failed_terminal_wait_reports_raw_model_and_parent_geometry(
    tmp_path: Path, eof: bool
) -> None:
    async def scenario() -> None:
        terminal = Terminal(tmp_path, 0.01)
        terminal.parent_environment = {"COLUMNS": "80", "LINES": "24", "TERM": None}
        footer = b" s start  k kill  q detach ".ljust(150)
        terminal.raw.extend(b"excluded-prefix" + b"x" * 4096 + b"\x1b[44;1H" + footer)
        terminal.eof = eof
        try:
            with pytest.raises(TerminalAcknowledgementError) as caught:
                await terminal.until("missing acknowledgement", lambda _data: False, after=0)
            message = str(caught.value)
            assert "missing acknowledgement" in message
            assert f"raw_tail={bytes(terminal.raw[-4096:])!r}" in message
            assert "excluded-prefix" not in message
            assert "parsed_screen_size=(150, 44)" in message
            assert f"parsed_footer={footer.decode()!r}" in message
            assert "observed_cursor_extent=(1, 44)" in message
            assert 'parent_environment={"COLUMNS": "80", "LINES": "24", "TERM": null}' in message
            assert 'parent_pty_sizes=[{"phase": "initial", "size": [150, 44]}]' in message
        finally:
            terminal.close()

    asyncio.run(scenario())


def test_child_environment_retains_inherited_geometry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "999")
    monkeypatch.setenv("LINES", "888")
    monkeypatch.setenv("PYTHONPATH", "/not-inherited")
    assert _environment()["COLUMNS"] == "999"
    assert _environment()["LINES"] == "888"
    assert "PYTHONPATH" not in _environment()


def test_failure_does_not_dump_unrelated_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "value-that-must-not-be-logged-482719"
    monkeypatch.setenv("TUI_PRIVATE_SENTINEL", sentinel)

    async def scenario() -> None:
        terminal = Terminal(tmp_path, _TIMEOUT)
        try:
            assert sentinel not in str(terminal.failure("private environment"))
        finally:
            terminal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("override", ["DSL41_TUI_EXECUTABLE", "DSL41_INSTALLED_EXECUTABLE"])
def test_installed_terminal_command_is_launched_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    for name in ("DSL41_TUI_EXECUTABLE", "DSL41_INSTALLED_EXECUTABLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(override, "/not-a-python-script/dsl41")
    invoked: list[tuple[str, ...]] = []

    async def spawn(*command: str, **kwargs: Any) -> None:
        invoked.append(command)
        assert "COLUMNS" not in kwargs["env"]
        assert "LINES" not in kwargs["env"]
        assert kwargs["env"]["TERM"] == "xterm-256color"
        assert kwargs["env"]["KEEP"] == "unchanged"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def scenario() -> None:
        terminal = Terminal(tmp_path, _TIMEOUT)
        command = _command("ui", "--socket", "/test/control.sock")
        try:
            environment = {"COLUMNS": "80", "LINES": "24", "KEEP": "unchanged"}
            before = dict(environment)
            await terminal.start(command, environment)
            assert invoked == [tuple(command)]
            assert environment == before
        finally:
            terminal.close()

    asyncio.run(scenario())


def test_terminal_failure_names_the_last_reset_control_and_offset(tmp_path: Path) -> None:
    async def scenario() -> None:
        terminal = Terminal(tmp_path, _TIMEOUT)
        try:
            terminal.raw.extend(b"prefix\x1b[2J" + b"x" * 4096)
            message = str(terminal.failure("reset", after=2))
            assert '"offset": 6' in message
            assert f'"token": {json.dumps(repr(b"\x1b[2J"))}' in message
        finally:
            terminal.close()

    asyncio.run(scenario())


_FILTERED_SELECTION = b"\x1b[2;91H explain: recovery_child \x1b[5;1H jobs 1/3 /recovery_child/ "


def test_terminal_selection_requires_complete_filter_and_selected_child() -> None:
    assert selected_filter_ready(_FILTERED_SELECTION)
    assert selected_filter_ready(
        _FILTERED_SELECTION.replace(b"recovery_child/", b"\x1b[3mrecovery_child/\x1b[0m")
    )


@pytest.mark.parametrize(
    "rendered",
    [
        b"1/3 recovery_child",
        _FILTERED_SELECTION.replace(b"recovery_child/", b"recovery_c/"),
        _FILTERED_SELECTION.replace(b"recovery_child/", b"recovery_childh/"),
        _FILTERED_SELECTION.replace(b"explain: recovery_child", b"explain: recovery_box"),
        _FILTERED_SELECTION.replace(b"[5;1H", b"[25;1H"),
        _FILTERED_SELECTION + b"\x1b[5;1H" + b" " * 30,
        _FILTERED_SELECTION[:-2],
    ],
)
def test_terminal_selection_rejects_partial_wrong_or_overwritten_rendering(rendered: bytes) -> None:
    assert not selected_filter_ready(rendered)


def test_terminal_freshness_excludes_history_and_waits_for_complete_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        terminal = Terminal(tmp_path, _TIMEOUT)
        try:
            terminal.raw.extend(_FILTERED_SELECTION)
            mark = len(terminal.raw)
            waiter = asyncio.create_task(
                terminal.until("selected", selected_filter_ready, after=mark)
            )
            await asyncio.wait_for(
                terminal.waiting.setdefault("selected", asyncio.Event()).wait(), _TIMEOUT
            )
            assert not waiter.done()
            terminal.raw.extend(_FILTERED_SELECTION[:-2])
            terminal.changed.set()
            # Another observation must be consumed before asserting a partial repaint blocks.
            terminal.waiting["selected"].clear()
            await asyncio.wait_for(terminal.waiting["selected"].wait(), _TIMEOUT)
            assert not waiter.done()
            terminal.raw.extend(_FILTERED_SELECTION[-2:])
            terminal.changed.set()
            await asyncio.wait_for(waiter, _TIMEOUT)
        finally:
            terminal.close()

    asyncio.run(scenario())


def test_terminal_footer_requires_fresh_complete_focus_at_the_requested_geometry() -> None:
    footer = b" s start  k kill  q detach "
    complete = b"\x1b[36;1H" + footer
    assert table_focus_ready(complete, len(footer), 36)
    assert table_focus_ready(complete + b"\x1b[2;1Hother row\r\n", len(footer), 36)
    assert not table_focus_ready(complete, len(footer), 44)
    assert not table_focus_ready(complete, len(footer) + 1, 36)
    assert not table_focus_ready(complete[:-1], len(footer), 36)
    assert not table_focus_ready(complete + b"\x1b[36;1H esc clear ", len(footer), 36)


@pytest.mark.parametrize(
    "control",
    [
        b"\x1b]22;default\x07",
        b"\x1b]22;pointer\x1b\\",
        b"\x1b]0;title\x07",
        b"\x1b]2;title\x1b\\",
        b"\x1b[?25l",
        b"\x1b[?25h",
        b"\x1b[?1000h",
        b"\x1b[?2004h",
        b"\x1b[?2026l",
    ],
)
def test_terminal_non_painting_controls_keep_observed_cells(control: bytes) -> None:
    footer = b" s start  k kill  q detach "
    painted = b"\x1b[36;1H" + footer + b"\x1b[2;5H"
    assert table_focus_ready(painted + control, len(footer), 36)
    assert selected_filter_ready(_FILTERED_SELECTION + control)
    assert not table_focus_ready(control, len(footer), 36)
    assert not table_focus_ready(painted[:-10] + control, len(footer), 36)


@pytest.mark.parametrize(
    "control",
    [
        b"\x1b]22;default",
        b"\x1b]22;default\x1b",
        b"\x1b]22;bad\rshape\x07",
        b"\x1b]52;clipboard\x07",
        b"\x1b[2J",
        b"\x1b[?1049h",
        b"\x1b[?1049l",
        b"\x1b[?9999h",
    ],
)
def test_terminal_unproven_or_painting_controls_invalidate_observed_cells(control: bytes) -> None:
    footer = b" s start  k kill  q detach "
    assert not table_focus_ready(b"\x1b[36;1H" + footer + control, len(footer), 36)
    assert not selected_filter_ready(_FILTERED_SELECTION + control)


async def _filter_and_hold(terminal: Terminal) -> int:
    await terminal.until(
        "initial table focus", lambda data: table_focus_ready(data, 150, 44), after=0
    )
    mark = terminal.send(b"/")
    await terminal.until(
        "filter focus", lambda data: _FILTER_OPEN.encode() in _sgr_free(data), after=mark
    )
    mark = terminal.send(b"recovery_child")
    await terminal.until("complete filter and selected child", selected_filter_ready, after=mark)
    mark = terminal.send(b"\r")
    await terminal.until(
        "submitted table focus", lambda data: table_focus_ready(data, 150, 44), after=mark
    )
    mark = terminal.resize()
    await terminal.until(
        "resized table focus", lambda data: table_focus_ready(data, 110, 36), after=mark
    )
    return terminal.send(b"h")


@pytest.mark.parametrize("barriers", [False, True], ids=["cli", "withheld-acks"])
def test_real_terminal_filter_selection_resize_confirm_cancel_and_detach(
    short_root: Path, tmp_path: Path, barriers: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("LINES", "24")
    inherited_environment = dict(os.environ)

    async def scenario() -> None:
        estate = _Estate(short_root, tmp_path)
        await estate.start()
        client = ControlClient(estate.socket)
        terminal = Terminal(tmp_path, _TIMEOUT)
        driver: asyncio.Task[int] | None = None
        parent, child = socket.socketpair()
        parent.setblocking(False)
        try:
            if barriers:
                await terminal.start(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("tui_submission_barrier.py")),
                        str(estate.socket),
                        str(child.fileno()),
                    ],
                    _environment(),
                    pass_fds=(child.fileno(),),
                )
            else:
                await terminal.start(_command("ui", "--socket", str(estate.socket)), _environment())
            child.close()
            driver = asyncio.create_task(_filter_and_hold(terminal))
            if barriers:
                loop = asyncio.get_running_loop()
                assert await _wait_barrier(parent, driver) == b"submission-blocked\n"
                await asyncio.wait_for(
                    terminal.waiting.setdefault("submitted table focus", asyncio.Event()).wait(),
                    _TIMEOUT,
                )
                assert not await _held(client)
                assert b"h" not in terminal.sent and not driver.done()
                await loop.sock_sendall(parent, b"s")
                assert await _wait_barrier(parent, driver) == b"resize-blocked\n"
                await asyncio.wait_for(
                    terminal.waiting.setdefault("resized table focus", asyncio.Event()).wait(),
                    _TIMEOUT,
                )
                assert not await _held(client)
                assert b"h" not in terminal.sent and not driver.done()
                await loop.sock_sendall(parent, b"r")
            mark = await driver
            await _wait_held(client)
            assert terminal.parent_environment["COLUMNS"] == "80"
            assert terminal.parent_environment["LINES"] == "24"
            assert terminal.pty_sizes == [
                {"phase": "initial", "size": [150, 44]},
                {"phase": "resize", "size": [110, 36]},
            ]

            async def until(text: str, *, after: int) -> None:
                await terminal.until(
                    text, lambda data: text.encode() in _sgr_free(data), after=after
                )

            await until("--claimed-actor", after=mark)
            mark = terminal.send(b"k")
            await until("KILLJOB recovery_child", after=mark)
            mark = terminal.send(b"\x1b")
            await until("cancelled", after=mark)
            mark = len(terminal.raw)
            await estate.restart()
            await until("FAILURE", after=mark)
            mark = terminal.send(b"/")
            await until(_FILTER_OPEN, after=mark)
            mark = terminal.send(b"\x1b")
            await until(_UNFILTERED, after=mark)
            terminal.send(b"q")
            assert terminal.process is not None
            assert await asyncio.wait_for(terminal.process.wait(), timeout=5) == 0
            assert await _held(client)
            assert estate.socket.exists()
        except TerminalAcknowledgementError:
            raise
        except Exception as exc:
            raise terminal.failure(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if driver is not None and not driver.done():
                driver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await driver
            if terminal.process is not None:
                await _stop(terminal.process)
            terminal.close()
            parent.close()
            child.close()
            observations = {
                "parent_pty_sizes": terminal.pty_sizes,
                "parent_environment": terminal.parent_environment,
            }
            (tmp_path / "driver-evidence.json").write_text(
                json.dumps(
                    {
                        "sent": [data.decode() for data in terminal.sent],
                        "marks": terminal.marks,
                        "observations": observations,
                    },
                    indent=2,
                )
                + "\n"
            )
            await client.close()
            await estate.close()

    try:
        asyncio.run(scenario())
    finally:
        assert dict(os.environ) == inherited_environment


def _footer_ready(rendered: bytes, columns: int, rows: int) -> bool:
    """Require a complete footer at its acknowledged size and table focus."""
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
            # Only observed one-cell writes prove this footer; unknown controls
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
