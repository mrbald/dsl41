"""Test-only submission delivery barrier; the real app still owns focus and rendering."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

from textual import events
from textual.widgets import Input

from dsl41.runner_tui import RunnerApp, _FilterInput


def main() -> None:
    channel = socket.socket(fileno=int(sys.argv[2]))
    channel.setblocking(False)
    pending: set[asyncio.Task[None]] = set()
    original = _FilterInput.post_message

    def post(self: _FilterInput, message: object) -> bool:
        if isinstance(message, Input.Submitted):

            async def deliver() -> None:
                loop = asyncio.get_running_loop()
                await loop.sock_sendall(channel, b"submission-blocked\n")
                assert await loop.sock_recv(channel, 1) == b"s"
                original(self, message)

            task = asyncio.create_task(deliver())
            pending.add(task)
            task.add_done_callback(pending.discard)
            return True
        return original(self, message)  # type: ignore[arg-type]

    _FilterInput.post_message = post  # type: ignore[method-assign, assignment]

    class BarrierApp(RunnerApp):
        async def _on_resize(self, event: events.Resize) -> None:
            if (event.size.width, event.size.height) == (110, 36):
                loop = asyncio.get_running_loop()
                await loop.sock_sendall(channel, b"resize-blocked\n")
                assert await loop.sock_recv(channel, 1) == b"r"

    try:
        BarrierApp(Path(sys.argv[1])).run()
    finally:
        channel.close()


if __name__ == "__main__":
    main()
