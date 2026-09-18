"""DL-210: a full macOS listen backlog is not evidence of a stale socket."""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
from pathlib import Path

import pytest

from dsl41.runner_adapters import SupervisorClient


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS returns refusal for a full backlog")
def test_full_listen_backlog_does_not_let_client_unlink_live_socket():
    with tempfile.TemporaryDirectory(prefix="dl210-backlog-", dir="/tmp") as directory:
        root = Path(directory)
        path = root / "supervisor.sock"
        pending = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen(1)
            inode = path.stat().st_ino
            try:
                for _ in range(64):
                    peer = socket.socket(socket.AF_UNIX)
                    peer.settimeout(0.2)
                    pending.append(peer)
                    try:
                        peer.connect(str(path))
                    except ConnectionRefusedError:
                        break
                else:
                    pytest.fail("could not fill a one-entry listen backlog")

                async def refused_client():
                    client = SupervisorClient(root)
                    try:
                        assert not await asyncio.wait_for(client._try_connect(), timeout=2)
                    finally:
                        await client.close()

                asyncio.run(refused_client())
                assert path.stat().st_ino == inode
                listener.settimeout(1)
                accepted, _ = listener.accept()  # the published listener is still live
                accepted.close()
            finally:
                for peer in pending:
                    peer.close()
