"""Event-driven PTY observations for interaction checks."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any


class TerminalAcknowledgementError(AssertionError):
    """A failed acknowledgement with the terminal and parent observations attached."""


class Terminal:
    def __init__(self, diagnostics: Path, timeout: float) -> None:
        self.master, self.slave = pty.openpty()
        self.raw = bytearray()
        self.sent: list[bytes] = []
        self.marks: dict[str, int] = {}
        self.diagnostics, self.timeout = diagnostics, timeout
        self.changed = asyncio.Event()
        self.waiting: dict[str, asyncio.Event] = {}
        self.eof = False
        self.columns, self.rows = 150, 44
        self.parent_environment: dict[str, str | None] = {}
        self.pty_sizes: list[dict[str, Any]] = []
        self.process: asyncio.subprocess.Process | None = None
        os.set_blocking(self.master, False)
        fcntl.ioctl(
            self.slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", self.rows, self.columns, 0, 0),
        )

        self.pty_sizes.append({"phase": "initial", "size": list(os.get_terminal_size(self.master))})

    async def start(self, command: list[str], env: dict[str, str], **kwargs: Any) -> None:
        self.parent_environment = {key: env.get(key) for key in ("COLUMNS", "LINES", "TERM")}
        child_env = dict(env)
        child_env.pop("COLUMNS", None)
        child_env.pop("LINES", None)
        child_env["TERM"] = "xterm-256color"
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            cwd=self.diagnostics,
            env=child_env,
            **kwargs,
        )
        os.close(self.slave)
        self.slave = -1
        asyncio.get_running_loop().add_reader(self.master, self._read)

    def _read(self) -> None:
        try:
            data = os.read(self.master, 1024 * 1024)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        self.raw.extend(data)
        if not data:
            self.eof = True
            asyncio.get_running_loop().remove_reader(self.master)
        self.changed.set()

    def send(self, data: bytes) -> int:
        mark = len(self.raw)
        assert os.write(self.master, data) == len(data)
        self.sent.append(data)
        return mark

    def resize(self) -> int:
        assert self.process is not None
        mark = len(self.raw)
        self.columns, self.rows = 110, 36
        fcntl.ioctl(
            self.master,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", self.rows, self.columns, 0, 0),
        )
        self.pty_sizes.append({"phase": "resize", "size": list(os.get_terminal_size(self.master))})
        os.kill(self.process.pid, signal.SIGWINCH)
        return mark

    async def until(self, label: str, predicate: Callable[[bytes], bool], *, after: int) -> None:
        self.marks[label] = after

        async def observe() -> None:
            while True:
                self.changed.clear()
                if predicate(bytes(self.raw[after:])):
                    return
                if self.eof:
                    raise self.failure(f"terminal exited waiting for {label}", after=after)
                self.waiting.setdefault(label, asyncio.Event()).set()
                await self.changed.wait()

        try:
            await asyncio.wait_for(observe(), self.timeout)
        except TimeoutError as exc:
            raise self.failure(
                f"{type(exc).__name__}: terminal did not acknowledge {label}", after=after
            ) from exc

    def failure(self, reason: str, *, after: int | None = None) -> TerminalAcknowledgementError:
        if after is None:
            after = next(reversed(self.marks.values()), 0)
        rendered = bytes(self.raw[after:])
        resets: list[tuple[int, bytes]] = []
        footer = painted_lines(rendered, self.columns, self.rows, reset_controls=resets)[-1]
        reset = (
            {"offset": after + resets[-1][0], "token": repr(resets[-1][1][:128])}
            if resets
            else None
        )
        positions = re.findall(rb"\x1b\[(\d+);(\d+)H", rendered)
        extent = (
            max((int(column) for _row, column in positions), default=0),
            max((int(row) for row, _column in positions), default=0),
        )
        return TerminalAcknowledgementError(
            f"{reason}; see {self.diagnostics}\n"
            f"raw_tail={bytes(self.raw[-4096:])!r}\n"
            f"parsed_screen_size={(self.columns, self.rows)!r}; parsed_footer={footer!r}; "
            f"observed_cursor_extent={extent!r}\n"
            f"last_reset_control={json.dumps(reset)}\n"
            f"parent_pty_sizes={json.dumps(self.pty_sizes)}; "
            f"parent_environment={json.dumps(self.parent_environment)}"
        )

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        if self.slave >= 0:
            os.close(self.slave)
        (self.diagnostics / "terminal.raw").write_bytes(self.raw)


def painted_lines(
    rendered: bytes,
    columns: int,
    rows: int,
    *,
    reset_controls: list[tuple[int, bytes]] | None = None,
) -> list[str]:
    """Only positioned, observed cells count; unknown cells cannot complete a label."""
    cells = [["\ufffd"] * columns for _ in range(rows)]
    row, column = -1, -1
    tokens = rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|[^\x1b]+|\x1b"
    for match in re.finditer(tokens, rendered):
        token = match.group()
        position = re.fullmatch(rb"\x1b\[(\d+);(\d+)H", token)
        if position:
            row, column = int(position[1]) - 1, int(position[2]) - 1
        elif re.fullmatch(rb"\x1b\[[\d;:]*m", token):
            continue
        elif re.fullmatch(
            rb"\x1b\](?:0|2|22);[^\x00-\x1f\x7f]*(?:\x07|\x1b\\)", token
        ) or re.fullmatch(rb"\x1b\[\?(?:25|1000|1003|1004|1006|1015|1016|2004|2026)[hl]", token):
            # Titles, pointer shape, cursor visibility and input/reporting modes paint
            # no cells. Unknown controls and buffer switches still invalidate the observed grid.
            continue
        elif token.startswith(b"\x1b"):
            if reset_controls is not None:
                reset_controls[:] = [(match.start(), token)]
            cells = [["\ufffd"] * columns for _ in range(rows)]
            row, column = -1, -1
        else:
            for char in token.decode("utf-8", errors="replace"):
                if char == "\r":
                    column = 0
                    continue
                if char == "\n":
                    row += 1
                    continue
                if (
                    not (0 <= row < rows and 0 <= column < columns)
                    or not char.isprintable()
                    or char == "\ufffd"
                    or unicodedata.category(char) in {"Mn", "Me", "Cf"}
                    or unicodedata.east_asian_width(char) in {"W", "F"}
                ):
                    cells = [["\ufffd"] * columns for _ in range(rows)]
                    row, column = -1, -1
                    break
                cells[row][column] = char
                column += 1
    return ["".join(line) for line in cells]


def selected_filter_ready(rendered: bytes) -> bool:
    lines = painted_lines(rendered, 150, 44)
    return " jobs 1/3 /recovery_child/ " in lines[4] and " explain: recovery_child " in lines[1]


def table_focus_ready(rendered: bytes, columns: int, rows: int) -> bool:
    footer = painted_lines(rendered, columns, rows)[-1]
    return "\ufffd" not in footer and all(
        label in footer for label in (" s start ", " k kill ", " q detach ")
    )
