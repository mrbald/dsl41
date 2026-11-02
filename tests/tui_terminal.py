"""Event-driven PTY observations for the DL-391 interaction checks."""

from __future__ import annotations

import asyncio
import fcntl
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
        self.process: asyncio.subprocess.Process | None = None
        os.set_blocking(self.master, False)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 150, 0, 0))

    async def start(self, command: list[str], env: dict[str, str], **kwargs: Any) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            start_new_session=True,
            cwd=self.diagnostics,
            env={**env, "TERM": "xterm-256color"},
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
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 110, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)
        return mark

    async def until(self, label: str, predicate: Callable[[bytes], bool], *, after: int) -> None:
        self.marks[label] = after

        async def observe() -> None:
            while True:
                self.changed.clear()
                if predicate(bytes(self.raw[after:])):
                    return
                assert not self.eof, f"terminal exited waiting for {label}; see {self.diagnostics}"
                self.waiting.setdefault(label, asyncio.Event()).set()
                await self.changed.wait()

        try:
            await asyncio.wait_for(observe(), self.timeout)
        except TimeoutError as exc:
            raise AssertionError(
                f"terminal did not acknowledge {label}; see {self.diagnostics}"
            ) from exc

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        if self.slave >= 0:
            os.close(self.slave)
        (self.diagnostics / "terminal.raw").write_bytes(self.raw)


def painted_lines(rendered: bytes, columns: int, rows: int) -> list[str]:
    """Only positioned, observed cells count; unknown cells cannot complete a label."""
    cells = [["\ufffd"] * columns for _ in range(rows)]
    row, column = -1, -1
    for token in re.findall(rb"\x1b\[[0-?]*[ -/]*[@-~]|[^\x1b]+|\x1b", rendered):
        position = re.fullmatch(rb"\x1b\[(\d+);(\d+)H", token)
        if position:
            row, column = int(position[1]) - 1, int(position[2]) - 1
        elif re.fullmatch(rb"\x1b\[[\d;:]*m", token):
            continue
        elif token.startswith(b"\x1b"):
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
