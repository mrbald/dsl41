"""Branch tests for `dsl41.runner_startup` that the run and resume suites do not reach (DL-269).

Each test holds one refusal or cleanup path to its observable effect: the error and the
state the run root is left in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from genesis_harness import C1_JIL, T0, _close, _genesis

from dsl41.cli_run import _serve_run
from dsl41.ir import lower_source
from dsl41.run_root import RuntimeProfile
from dsl41.runner_clock import EngineError
from dsl41.runner_ledger import LeaderLock
from dsl41.runner_startup import acquire_run_root, wire_from_profile


def test_the_builder_refuses_a_detached_profile_by_name() -> None:
    """`wire_from_profile` wires the tethered mode only: a profile in detached mode is refused
    by name before any adapter is built. Its twin, the tethered profile, wires."""
    catalog = lower_source("insert_job: a\njob_type: c\ncommand: x\n")
    with pytest.raises(EngineError, match="detached mode is retired"):
        wire_from_profile(catalog, RuntimeProfile(execution_mode="detached"), start=T0)
    assert "CMD" in wire_from_profile(catalog, RuntimeProfile(), start=T0).adapters


def test_a_raise_after_genesis_opens_its_segment_closes_the_journal_and_frees_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every raise after the genesis segment opens closes it: here the lock note's. The
    error is the original one, no file handle leaks (the suite makes a ResourceWarning an
    error), and the run root's lock can be taken again."""

    def fail(self: LeaderLock, *, at: datetime) -> None:
        raise OSError("test: the lock note did not land")

    monkeypatch.setattr(LeaderLock, "note", fail)
    run_root = tmp_path / "run"
    with pytest.raises(OSError, match="the lock note did not land"):
        _genesis(run_root)
    acquire_run_root(run_root).release()


def test_a_resume_of_a_root_pinned_in_detached_mode_is_refused_naming_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dsl41 run --resume` wires from the launch profile, which is tethered, and then holds
    it to the root's pin (PR-22). A root pinned in detached mode is refused there, exit 2,
    naming `execution_mode`, before any replay."""
    run_root = tmp_path / "run"
    _close(_genesis(run_root, profile=RuntimeProfile(execution_mode="detached")))
    before = sorted(p.relative_to(run_root) for p in run_root.rglob("*"))
    rc = asyncio.run(_serve_run(lower_source(C1_JIL), run_root, True, [], profile=RuntimeProfile()))
    assert rc == 2
    err = capsys.readouterr().err
    assert "runtime-profile mismatch" in err and "(execution_mode)" in err
    assert sorted(p.relative_to(run_root) for p in run_root.rglob("*")) == before
