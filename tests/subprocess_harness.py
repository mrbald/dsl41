"""Subprocess helpers shared by the mypy-snippet and CLI-integration tests.

Not a test file: no test_ prefix, imported by the tests that need it (the
precedent was a supervisor test double).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dsl41


def mypy_report(tmp_path: Path, *files: Path) -> str:
    """Run mypy over `files` in a subprocess, isolated by `tmp_path`'s own
    cache dir and MYPYPATH pointed at dsl41's source tree so `dsl41`
    resolves without an install. Out of process because pulling mypy into
    THIS process would leave its module graph resident for every later
    test."""
    if importlib.util.find_spec("mypy") is None:  # a dev dependency, not a runtime one
        pytest.skip("mypy is not installed")  # pragma: no cover
    src_root = Path(dsl41.__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / "cache"),
            *(str(f) for f in files),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "MYPYPATH": str(src_root)},
        timeout=60,
    )
    return result.stdout


def cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """One `dsl41` CLI invocation, run the way the real entry point is
    reached (`python -m dsl41`), captured as text."""
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


def wait_for(predicate, timeout_s: float = 10.0, interval_s: float = 0.05):
    """Poll `predicate` until it answers truthy, or fail after `timeout_s`."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


def wait_reaped(pid: int, *, reap: bool = False) -> None:
    """Wait until `pid` has left the process table; a test reaps its children
    before it ends (DL-355). `reap` reaps it here, for a child of this process
    nothing else waits for."""
    import os

    def pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def gone() -> bool:
        if not reap:
            return not pid_alive(pid)
        try:
            return os.waitpid(pid, os.WNOHANG)[0] == pid
        except ChildProcessError:
            return True

    try:
        wait_for(gone)
    except AssertionError:
        raise AssertionError(f"process {pid} was not reaped within 10 s") from None
