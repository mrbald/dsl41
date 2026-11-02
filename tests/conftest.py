"""Fixtures shared across the test suite."""

from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import pytest

# records state-machine transition hits for scripts/transition_coverage.py
pytest_plugins = ["transition_hits_plugin", "leak_detector_plugin"]

# A pytest-xdist worker shares the box with three others, so an example can
# stall past hypothesis's 200 ms deadline and fail as Flaky when its replay does
# not stall. The deadline is a speed check, not a correctness one; under a
# worker it is off, and every other setting is unchanged. xdist sets the
# variable before the worker loads this file.
if os.environ.get("PYTEST_XDIST_WORKER"):
    from hypothesis import settings

    settings.register_profile("xdist-worker", deadline=None)
    settings.load_profile("xdist-worker")


@pytest.fixture(autouse=True)
def _restore_recursion_limit():
    """The oracle the engine and every replay build raises the interpreter's
    recursion limit (`oracle.RECURSION_LIMIT`), for the whole process.
    Each test gets back the limit it started with, so a test that relies on
    the default limit does not depend on which tests ran before it. Only the
    oracle's raise is put back: a test that sets the limit itself restores it,
    or the leak detector names it (DL-355)."""
    from dsl41.oracle import RECURSION_LIMIT

    limit = sys.getrecursionlimit()
    yield
    if sys.getrecursionlimit() == max(limit, RECURSION_LIMIT):
        sys.setrecursionlimit(limit)


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain terminal for a CLI test: no colour and 200 columns. The help text
    and error panels of typer and rich then read the same under a terminal and in
    CI. TERM stays as it is: rich answers 80 columns to a dumb terminal whatever
    COLUMNS says. A test that asserts colour or a given width sets its own."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)


def _subreaper_prctl():
    """Linux's libc `prctl`, or None elsewhere."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes

    try:
        return ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except (OSError, AttributeError):
        return None


_PRCTL = _subreaper_prctl()


@pytest.fixture(autouse=True)
def _restore_child_subreaper():
    """An in-process `Supervisor.run()` and the subreaper test make this
    pytest process a child subreaper (Linux), and the flag outlives the test.
    Left set, a command that a later test orphans by killing its wrapper
    reparents to this process, which never reaps it: it stays a zombie,
    `kill(pid, 0)` keeps succeeding, and a poll for its death times out. Under
    pytest-xdist a worker that ran test_runner_supervisor.py before
    test_runner_lifecycle.py failed two tests that way. Each test gets back
    the flag it started with."""
    if _PRCTL is None:
        yield
        return
    import ctypes

    flag = ctypes.c_int(0)
    read = _PRCTL(37, ctypes.byref(flag), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    yield
    if read:
        _PRCTL(36, flag.value, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER


_RUN_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@pytest.fixture(autouse=True)
def _restore_run_signal_handlers():
    """`dsl41 run` installs asyncio handlers for SIGINT, SIGTERM and SIGHUP,
    and closing its loop sets each to the default rather than back to the
    handler it found. Tests run it in-process, and under coverage the handler
    it found is coverage's SIGTERM handler, which saves the worker's data.
    Each test gets back the handlers it started with, where the handler now is
    what closing the loop leaves (`SIG_DFL`, or `default_int_handler` for
    SIGINT). Any other change is the leak detector's to name (DL-355)."""
    handlers = [signal.getsignal(sig) for sig in _RUN_SIGNALS]
    yield
    for sig, handler in zip(_RUN_SIGNALS, handlers, strict=True):
        now = signal.getsignal(sig)
        left = now is signal.SIG_DFL or now is signal.default_int_handler
        if handler is not None and now is not handler and left:
            signal.signal(sig, handler)


@pytest.fixture
def own_recursion_limit():
    """For a test that sets the interpreter's recursion limit itself: it gets
    back the limit it started with."""
    limit = sys.getrecursionlimit()
    yield
    sys.setrecursionlimit(limit)


@pytest.fixture
def oracle_recursion_limit():
    """The oracle's limit from the start of the test: hypothesis warns when a
    test changes the limit while it runs, as building that oracle does."""
    from dsl41.oracle import RECURSION_LIMIT

    sys.setrecursionlimit(max(sys.getrecursionlimit(), RECURSION_LIMIT))


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
