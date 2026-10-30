"""A 2.0.0 run root holds period 1 only, opened from nothing, under its own sentinel.

Resume checks the WAL against the root before any replay or write: a segment from another
estate, a second segment, or an opening that names a seal refuses by name and leaves the WAL's
bytes as they were.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from genesis_harness import C1_JIL, _close, _genesis, _resume

from dsl41.run_root import read_sentinel, sentinel_path, wal_path
from dsl41.runner_clock import EngineError


def _root(tmp_path: Path) -> Path:
    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    return run_root


def _loosened(run_root: Path) -> tuple[int, int]:
    """Loosen the root's mode, so a resume that tightens it shows; answer (mode, mtime_ns)."""
    os.chmod(run_root, 0o750)
    st = run_root.stat()
    return st.st_mode, st.st_mtime_ns


def _as_left(run_root: Path) -> tuple[int, int]:
    st = run_root.stat()
    return st.st_mode, st.st_mtime_ns


def test_a_root_with_its_own_segment_resumes(tmp_path: Path) -> None:
    """The counterpart: a resume that passes every check tightens a loosened root."""
    run_root = _root(tmp_path)
    _loosened(run_root)
    _close(_resume(run_root, C1_JIL))
    assert run_root.stat().st_mode & 0o777 == 0o700


def test_a_sentinel_naming_another_estate_refuses_before_any_write(tmp_path: Path) -> None:
    run_root = _root(tmp_path)
    sentinel = read_sentinel(run_root)
    assert sentinel is not None
    payload = json.loads(sentinel_path(run_root).read_text())
    payload["estate_id"] = "someone-else"
    sentinel_path(run_root).write_text(json.dumps(payload, sort_keys=True) + "\n")
    before = wal_path(run_root, 1).read_bytes()
    with pytest.raises(EngineError, match="stranger's segment"):
        _resume(run_root, C1_JIL)
    assert wal_path(run_root, 1).read_bytes() == before


def test_a_second_segment_refuses_before_any_write(tmp_path: Path) -> None:
    run_root = _root(tmp_path)
    before = wal_path(run_root, 1).read_bytes()
    wal_path(run_root, 2).write_bytes(before)
    left = _loosened(run_root)
    with pytest.raises(EngineError, match="segment 1 only"):
        _resume(run_root, C1_JIL)
    assert wal_path(run_root, 1).read_bytes() == before
    assert _as_left(run_root) == left


def test_an_opening_that_names_a_seal_refuses_before_any_write(tmp_path: Path) -> None:
    run_root = _root(tmp_path)
    lines = wal_path(run_root, 1).read_text().splitlines()
    opening = json.loads(lines[0])
    opening["opens_from_seal"] = {"period_id": 1, "digest": "sha256:" + "0" * 64}
    lines[0] = json.dumps(opening, sort_keys=True)
    wal_path(run_root, 1).write_text("\n".join(lines) + "\n")
    before = wal_path(run_root, 1).read_bytes()
    left = _loosened(run_root)
    with pytest.raises(EngineError, match="segment 1 opens no seal"):
        _resume(run_root, C1_JIL)
    assert wal_path(run_root, 1).read_bytes() == before
    assert _as_left(run_root) == left
