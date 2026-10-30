"""Branch tests for `dsl41.runner_startup` that the run and resume suites do not reach (DL-269).

Each test holds one refusal or cleanup path to its observable effect: the error and the
state the run root is left in.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from genesis_harness import C1_JIL, T0, _catalog, _close, _genesis

from dsl41.cli_run import _serve_run
from dsl41.ir import lower_source
from dsl41.runner_config import RuntimeProfile
from dsl41.runner_ledger import LeaderLock
from dsl41.runner_startup import acquire_run_root, wire_from_profile


def test_the_builder_takes_the_stop_grace_from_the_engine_settings() -> None:
    """`wire_from_profile` builds the command adapter with the settings' `grace_term_s`
    (engine.toml, DL-424), and with the default 10 s when it is given none."""
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_config import EngineSettings

    catalog = lower_source("insert_job: a\njob_type: c\ncommand: x\n")
    wired = wire_from_profile(
        catalog, RuntimeProfile(), start=T0, settings=EngineSettings(grace_term_s=3.0)
    )
    adapter = wired.adapters["CMD"]
    assert isinstance(adapter, LocalCommandAdapter) and adapter.grace_seconds == 3.0
    default = wire_from_profile(catalog, RuntimeProfile(), start=T0).adapters["CMD"]
    assert isinstance(default, LocalCommandAdapter) and default.grace_seconds == 10.0


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


def test_a_resume_under_another_profile_is_refused_naming_the_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dsl41 run --resume` wires from the launch profile and then holds it to the profile the
    store's configuration recorded (PR-22). A store recorded under `local-eligible` is refused
    there, exit 2, naming `machine_policy`, before any replay, and the store is unchanged."""
    from store_surgery import rows

    run_root = tmp_path / "run"
    _close(_genesis(run_root, profile=RuntimeProfile(machine_policy="local-eligible")))
    before = rows(run_root)
    catalog, _ = _catalog(C1_JIL)
    rc = asyncio.run(_serve_run(catalog, run_root, True, [], profile=RuntimeProfile()))
    assert rc == 2
    err = capsys.readouterr().err
    assert "runtime-profile mismatch on machine_policy" in err
    assert rows(run_root) == before
