"""Subprocess helpers shared by the mypy-snippet and CLI-integration tests.

Not a test file: no test_ prefix, imported by the tests that need it (the
precedent is `tests/supervisor_list_doubles.py`).
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
