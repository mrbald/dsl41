"""Fixtures shared across the test suite."""

from __future__ import annotations

import os
import selectors
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

# records state-machine transition hits for scripts/transition_coverage.py
pytest_plugins = ["transition_hits_plugin"]


@pytest.fixture(autouse=True)
def _restore_recursion_limit():
    """The engine and the replay raise the interpreter's recursion limit to
    fit their catalog (`oracle.fit_recursion_limit`), for the whole process.
    Each test gets back the limit it started with, so a test that relies on
    the default limit does not depend on which tests ran before it."""
    limit = sys.getrecursionlimit()
    yield
    sys.setrecursionlimit(limit)


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


def _configuration(state, phase):
    """`phase` and its ancestors in a statechart's tree, or None when absent."""
    if state.name == phase:
        return frozenset({phase})
    for region in state.regions:
        for child in region.states:
            below = _configuration(child, phase)
            if below is not None:
                return below | {state.name}
    return None


@pytest.fixture
def stand():
    """`stand(sup, phase)` puts a supervisor's process in the leaf state `phase`
    with no row taken, where a test wrote the table machine's `state` before. A
    fresh executor holds the lease as it was and the process in `phase`; a spy
    on the old executor is gone."""
    from dsl41 import runner_supervisor

    def _stand(sup, phase: str) -> None:
        chart = runner_supervisor.SUPERVISOR_PROCESS
        executor = runner_supervisor.Executor()
        executor.restore(
            "lease",
            runner_supervisor.SUPERVISOR_LEASE,
            sup._machines.active("lease"),
            sup,
        )
        executor.restore("process", chart, _configuration(chart.root, phase), sup)
        sup._machines = executor

    return _stand


@pytest.fixture
def queued_supervisor(short_root: Path):
    """In-process selector with real sockets and explicit descriptor cleanup.
    The supervisor stands in `serving`, the only state whose requests are read
    and dispatched; it binds nothing and publishes nothing. It gets there by
    the process chart's rows: the startup fact `free`, then `publish`."""
    from dsl41 import runner_supervisor

    sup = runner_supervisor.Supervisor(str(short_root))
    sup._process(runner_supervisor.START, runner_supervisor.FREE)
    sup._process(runner_supervisor.PUBLISH)
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
