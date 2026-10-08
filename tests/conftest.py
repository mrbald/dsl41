"""Fixtures shared across the test suite."""

from __future__ import annotations

import os
import selectors
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

# records state-machine transition hits for scripts/transition_coverage.py
pytest_plugins = ["transition_hits_plugin"]


@pytest.fixture
def short_root():
    """A short-path base directory for AF_UNIX sockets. pytest's `tmp_path`
    lives deep under the platform temp dir and can exceed `sun_path`'s
    length limit (104 bytes on macOS) once a socket file is appended --
    unlike ordinary files, unix-socket paths have no workaround for that, so
    tests that bind one use this instead of `tmp_path`."""
    directory = tempfile.mkdtemp(prefix="dsl41-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def queued_supervisor(short_root: Path):
    """In-process selector with real sockets and explicit descriptor cleanup."""
    from dsl41 import runner_supervisor

    sup = runner_supervisor.Supervisor(str(short_root))
    peers = []

    def connect():
        server, peer = socket.socketpair()
        server.setblocking(False)
        peer.settimeout(1)
        conn = runner_supervisor._Conn(server)
        sup._conns[server.fileno()] = conn
        sup._sel.register(server, selectors.EVENT_READ, ("conn", conn))
        peers.append(peer)
        return conn, peer

    yield sup, connect
    for conn in list(sup._conns.values()):
        sup._drop_conn(conn)
    for peer in peers:
        peer.close()
    for fd in (sup._chld_r, sup._chld_w):
        os.close(fd)
    sup._sel.close()
