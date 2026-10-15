"""The leak detector (tests/leak_detector_plugin.py, DL-355): each probe
names the state a test leaks on purpose, and the fixture fails such a test.

Each test changes one piece of process state, reads the probes, and puts the
state back before it returns, so the detector's own check of the test passes."""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from leak_detector_plugin import PROBES, ProbeUnavailable, changes, child_pids, snapshot

TESTS = Path(__file__).resolve().parent
_CHILDREN_READ = pytest.mark.skipif(
    not sys.platform.startswith("linux") and sys.platform != "darwin",
    reason="the probe reads children on Linux and macOS only",
)


def _leaked(change: Callable[[], Callable[[], object]]) -> list[str]:
    """The detector's lines for a change; `change` makes it and returns the undo."""
    before = snapshot()
    undo = change()
    try:
        after = snapshot()
    finally:
        undo()
    return changes(before, after)


def _only(lines: list[str], name: str) -> str:
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"{name}: "), lines
    return lines[0]


def test_a_clean_snapshot_reports_nothing() -> None:
    assert changes(snapshot(), snapshot()) == []
    assert len({probe.name for probe in PROBES}) == len(PROBES)


def test_the_recursion_limit_is_named_before_and_after() -> None:
    limit = sys.getrecursionlimit()

    def change() -> Callable[[], object]:
        sys.setrecursionlimit(limit + 7)
        return lambda: sys.setrecursionlimit(limit)

    line = _only(_leaked(change), "recursion limit")
    assert f"before {limit}, after {limit + 7}" in line


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a Linux prctl flag")
def test_the_child_subreaper_flag_is_named() -> None:
    import ctypes

    prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
    flag = ctypes.c_int(0)
    assert prctl(37, ctypes.byref(flag), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    was = flag.value

    def change() -> Callable[[], object]:
        prctl(36, 1 - was, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
        return lambda: prctl(36, was, 0, 0, 0)

    line = _only(_leaked(change), "child subreaper")
    assert f"before {was}, after {1 - was}" in line


def test_automatic_collection_turned_off_is_named() -> None:
    def change() -> Callable[[], object]:
        gc.disable()
        return gc.enable

    assert _only(_leaked(change), "gc enabled") == "gc enabled: before True, after False"


def test_a_changed_collection_threshold_is_named() -> None:
    threshold = gc.get_threshold()

    def change() -> Callable[[], object]:
        gc.set_threshold(threshold[0] + 1, *threshold[1:])
        return lambda: gc.set_threshold(*threshold)

    line = _only(_leaked(change), "gc threshold")
    assert f"before {threshold!r}" in line


def test_an_installed_signal_handler_is_named() -> None:
    def handler(signum: int, frame: object) -> None:
        pass

    def change() -> Callable[[], object]:
        old = signal.signal(signal.SIGUSR1, handler)
        return lambda: signal.signal(signal.SIGUSR1, old)

    line = _only(_leaked(change), "signal handlers")
    assert "SIGUSR1: before " in line and "handler" in line


def test_a_wakeup_fd_left_set_is_named() -> None:
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)

    def change() -> Callable[[], object]:
        old = signal.set_wakeup_fd(write_end)
        return lambda: signal.set_wakeup_fd(old)

    try:
        line = _only(_leaked(change), "signal handlers")
    finally:
        os.close(read_end)
        os.close(write_end)
    assert f"wakeup fd: before -1, after {write_end}" in line


def test_an_environment_variable_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DSL41_LEAK_PROBE", raising=False)

    def change() -> Callable[[], object]:
        os.environ["DSL41_LEAK_PROBE"] = "1"
        return lambda: os.environ.pop("DSL41_LEAK_PROBE")

    line = _only(_leaked(change), "environment")
    assert "DSL41_LEAK_PROBE added ('1')" in line


def test_pytests_own_phase_variable_is_not_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    def change() -> Callable[[], object]:
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "elsewhere")
        return lambda: None

    assert _leaked(change) == []


def test_a_changed_working_directory_is_named(tmp_path: Path) -> None:
    here = os.getcwd()

    def change() -> Callable[[], object]:
        os.chdir(tmp_path)
        return lambda: os.chdir(here)

    line = _only(_leaked(change), "working directory")
    assert f"before {here!r}" in line


def test_a_changed_umask_is_named() -> None:
    def change() -> Callable[[], object]:
        old = os.umask(0o077)
        os.umask(old)
        os.umask(old ^ 0o002)
        return lambda: os.umask(old)

    _only(_leaked(change), "umask")


def test_a_sys_path_entry_is_named() -> None:
    def change() -> Callable[[], object]:
        sys.path.insert(0, "/nonexistent/leak")
        return lambda: sys.path.remove("/nonexistent/leak")

    line = _only(_leaked(change), "sys.path")
    assert "added ['/nonexistent/leak'], removed []" in line


@_CHILDREN_READ
def test_a_live_child_is_named() -> None:
    before = snapshot()
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        after = snapshot()
    finally:
        child.communicate()
    assert child.pid not in child_pids()
    assert f"new {child.pid}" in _only(changes(before, after), "child processes")


@_CHILDREN_READ
@pytest.mark.skipif(not hasattr(os, "waitid"), reason="os.waitid: macOS from Python 3.13")
def test_a_dead_child_nobody_reaped_still_counts() -> None:
    before = snapshot()
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # exited, still unreaped
        after = snapshot()
    finally:
        child.wait()
    assert f"new {child.pid}" in _only(changes(before, after), "child processes")


@contextmanager
def _parked_thread(*, daemon: bool) -> Iterator[threading.Thread]:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="leak-probe", daemon=daemon)
    thread.start()
    try:
        yield thread
    finally:
        release.set()
        thread.join()


def test_a_live_non_daemon_thread_is_named() -> None:
    before = snapshot()
    with _parked_thread(daemon=False):
        after = snapshot()
    assert "leak-probe" in _only(changes(before, after), "non-daemon threads")


def test_a_live_daemon_thread_is_not_probed() -> None:
    before = snapshot()
    with _parked_thread(daemon=True):
        after = snapshot()
    assert changes(before, after) == []


@pytest.mark.skipif(
    not hasattr(asyncio, "get_event_loop_policy"), reason="event-loop policies removed"
)
def test_a_replaced_event_loop_policy_is_named() -> None:
    def change() -> Callable[[], object]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # policies, Python 3.14
            old = asyncio.get_event_loop_policy()
            asyncio.set_event_loop_policy(type(old)())

        def undo() -> None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                asyncio.set_event_loop_policy(old)

        return undo

    _only(_leaked(change), "event-loop policy")


def test_an_event_loop_probe_whose_interface_is_gone_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = tuple(probe for probe in PROBES if "loop" in probe.name)
    assert len(probes) == 2
    monkeypatch.delattr(asyncio, "get_event_loop_policy", raising=False)
    for probe in probes:
        with pytest.raises(ProbeUnavailable, match="get_event_loop_policy is gone"):
            probe.read()
    monkeypatch.setattr(asyncio, "get_event_loop_policy", object, raising=False)  # no `_local`
    with pytest.raises(ProbeUnavailable, match="no current loop"):
        snapshot(probes)


def test_an_open_loop_left_current_is_named() -> None:
    loop = asyncio.new_event_loop()

    def change() -> Callable[[], object]:
        asyncio.set_event_loop(loop)
        return lambda: asyncio.set_event_loop(None)

    try:
        line = _only(_leaked(change), "open current event loop")
    finally:
        loop.close()
    assert "before None" in line


def test_a_closed_loop_left_current_is_not_a_change() -> None:
    loop = asyncio.new_event_loop()
    loop.close()

    def change() -> Callable[[], object]:
        asyncio.set_event_loop(loop)
        return lambda: asyncio.set_event_loop(None)

    assert _leaked(change) == []


def test_a_root_logger_handler_is_named() -> None:
    handler = logging.NullHandler()

    def change() -> Callable[[], object]:
        logging.root.addHandler(handler)
        return lambda: logging.root.removeHandler(handler)

    assert "NullHandler" in _only(_leaked(change), "root logger handlers")


def test_the_root_logger_level_is_named() -> None:
    level = logging.root.level

    def change() -> Callable[[], object]:
        logging.root.setLevel(level + 1)
        return lambda: logging.root.setLevel(level)

    line = _only(_leaked(change), "root logger level")
    assert f"before {level}, after {level + 1}" in line


def test_logging_disabled_is_named() -> None:
    was = logging.root.manager.disable

    def change() -> Callable[[], object]:
        logging.disable(logging.CRITICAL)
        return lambda: logging.disable(was)

    line = _only(_leaked(change), "logging disable level")
    assert f"before {was}, after {logging.CRITICAL}" in line


def test_the_fixture_fails_a_leaking_test_after_its_restore_fixture(tmp_path: Path) -> None:
    """A real pytest run: the leaking test errors at teardown, naming the
    state; a test whose own fixture restores the state passes, because the
    detector checks after every other function fixture is torn down."""
    (tmp_path / "test_leaks.py").write_text(
        textwrap.dedent(
            """
            import sys

            import pytest

            @pytest.fixture
            def restored():
                limit = sys.getrecursionlimit()
                yield
                sys.setrecursionlimit(limit)

            def test_leaks():
                sys.setrecursionlimit(sys.getrecursionlimit() + 11)

            def test_restored_by_its_fixture(restored):
                sys.setrecursionlimit(sys.getrecursionlimit() + 13)

            def test_clean():
                pass
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": str(TESTS)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "leak_detector_plugin",
            "-p",
            "no:cacheprovider",
            str(tmp_path / "test_leaks.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "3 passed, 1 error" in result.stdout, result.stdout
    assert "ERROR at teardown of test_leaks" in result.stdout
    assert "the test left global process state changed" in result.stdout
    assert "recursion limit: before " in result.stdout
