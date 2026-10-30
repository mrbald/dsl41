"""A 2.0.0 run root holds one store, `engine.db`, opened from nothing (spec-wiring 1b).

Genesis refuses a root that already holds a store, and both genesis and restart refuse a 1.x
root (its `journal.jsonl` sentinel or its `wal/` segments) by name, before any write: 2.0.0
runs from the store only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from genesis_harness import C1_JIL, _close, _genesis, _resume

from dsl41.runner_clock import EngineError


def _root(tmp_path: Path) -> Path:
    run_root = tmp_path / "run"
    _close(_genesis(run_root))
    return run_root


def test_a_root_with_its_own_store_resumes(tmp_path: Path) -> None:
    """The counterpart: a resume that passes every check tightens a loosened root."""
    run_root = _root(tmp_path)
    os.chmod(run_root, 0o750)
    _close(_resume(run_root, C1_JIL))
    assert run_root.stat().st_mode & 0o777 == 0o700
    assert {"engine.db", "leader.lock", "plans", "runs"} <= {p.name for p in run_root.iterdir()}


def test_genesis_refuses_a_root_that_already_holds_a_store(tmp_path: Path) -> None:
    run_root = _root(tmp_path)
    with pytest.raises(EngineError, match="already exists: a new run needs a fresh run root"):
        _genesis(run_root)


def test_a_restart_of_a_root_with_no_store_refuses(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(EngineError, match="no engine.db to resume"):
        _resume(tmp_path / "empty", C1_JIL)


@pytest.mark.parametrize("marker", ["journal.jsonl", "wal"])
def test_a_1x_root_is_refused_by_name_before_any_write(tmp_path: Path, marker: str) -> None:
    run_root = tmp_path / "old"
    run_root.mkdir()
    if marker == "wal":
        (run_root / "wal").mkdir()
    else:
        (run_root / marker).write_text('{"rec": "period_root"}\n')
    before = sorted(p.name for p in run_root.iterdir())
    with pytest.raises(EngineError, match="a 1.x run root"):
        _genesis(run_root)
    with pytest.raises(EngineError, match="a 1.x run root"):
        _resume(run_root, C1_JIL)
    assert sorted(p.name for p in run_root.iterdir() if p.name != "leader.lock") == before
