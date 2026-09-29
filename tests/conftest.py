"""Fixtures shared across the test suite."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


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
