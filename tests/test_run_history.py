"""Run history tests (DL-113): the projection folded out of a run
root's journal and spool.

Normative spec: `docs/decision-log.md` DL-113 and `runner_history.py`'s
own module docstring, which is the API under test. `runner-design.md` ss7 is the
record shapes this module reads; nothing here writes one.

House style follows test_runner_journal.py: T0 = datetime(2026, 7, 1, 8, 0).
The pure-fold section builds journal records as plain dicts, matching the
exact shapes `Journal`'s own write methods produce (verified against
`runner_journal.py` and `runner.py` before any assertion was written,
CLAUDE.md's fidelity discipline) -- no filesystem, no Oracle, no Engine.
The integration section drives a real subprocess through `start_run` +
`RealClock` + `LocalCommandAdapter` (the `test_runner_adapters.py` pattern)
so `read_run_root` is exercised against a real journal, a real manifest and
a real spool.
"""

from __future__ import annotations

import asyncio
import json

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import runner_history
from dsl41.ast_jil import parse
from dsl41.cli import _write_manifest, app
from dsl41.ir import lower_catalog, lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner_adapters import LocalCommandAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_history import (
    RunHistoryError,
    RunRow,
    fold_run_rows,
    read_run_root,
    read_run_roots,
    read_spool,
)
from dsl41.runner_startup import start_run

T0 = datetime(2026, 7, 1, 8, 0)


# ---------------------------------------------------- record-shape builders


def _header(catalog_hash: str = "h1", started_at: datetime = T0) -> dict[str, Any]:
    return {
        "rec": "header",
        "catalog_hash": catalog_hash,
        "dsl41_version": "0+test",
        "state_machine_version": 1,
        "clock_domain": "real",
        "started_at": started_at.isoformat(),
    }


def _dispatch(
    job: str, run_number: int, *, run_dir: str | None, started_at: datetime, wrapper_pid: int = 111
) -> dict[str, Any]:
    return {
        "rec": "dispatch",
        "job": job,
        "run_number": run_number,
        "wrapper_pid": wrapper_pid,
        "run_dir": run_dir,
        "started_at": started_at.isoformat(),
    }


def _status_input(
    seq: int,
    at: datetime,
    *,
    job: str,
    run_number: int | None,
    source: str | None = "adapter",
    exit_code: int | None = None,
    status: str | None = None,
    cause: str | None = None,
    ended_at: datetime | None = None,
) -> dict[str, Any]:
    payload: dict[str, object] = {"job": job}
    if run_number is not None:
        payload["run_number"] = run_number
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if status is not None:
        payload["status"] = status
    if cause is not None:
        payload["cause"] = cause
    if ended_at is not None:
        payload["ended_at"] = ended_at.isoformat()
    return {
        "rec": "input",
        "seq": seq,
        "at": at.isoformat(),
        "kind": "STATUS",
        "payload": payload,
        "source": source,
        "request_id": f"r{seq}",
        "fingerprint": f"fp{seq}",
        "epoch": 0,
    }


def _spawn_effect(
    index: int, at: datetime, *, job: str, run_number: int, executor_id: str = "local"
) -> dict[str, Any]:
    return {
        "rec": "effect",
        "effect_id": f"e{index}:SPAWN:{job}.{run_number}",
        "kind": "SPAWN",
        "job": job,
        "run_number": run_number,
        "executor_id": executor_id,
        "index": index,
        "at": at.isoformat(),
    }


def _drop(job: str, at: datetime = T0) -> dict[str, Any]:
    return {
        "rec": "drop",
        "at": at.isoformat(),
        "kind": "STARTJOB",
        "payload": {"job": job},
        "reason": "downtime (E9)",
    }


# ------------------------------------------------------------- 1. pure fold


def test_folding_the_same_records_twice_gives_the_same_rows() -> None:
    """(DL-113): fold_run_rows is a pure function of its
    inputs -- no filesystem, no Oracle, no Engine touched."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    first = fold_run_rows(records)
    second = fold_run_rows(list(records))  # a fresh list of the same dicts
    assert first == second
    assert first == [
        RunRow(
            job="j1",
            run_number=1,
            catalog_hash="h1",
            started_at=T0,
            ended_at=T0 + timedelta(minutes=1),
            duration_s=60.0,
            status="SUCCESS",
            exit_code=0,
            started_by=None,
            executor_id=None,
            run_dir=None,
            box_name=None,
            clock_source="journal",
            job_hash=None,  # no catalog supplied to this pure fold
            fidelity="records_only",
        )
    ]


def test_a_series_spanning_two_run_roots_comes_back_segmented(monkeypatch: Any) -> None:
    """(DL-113): read_run_roots combines and sorts by (job,
    started_at) without merging or hiding a catalog_hash change -- monkey-
    patched at read_run_root so the property under test is the COMBINE, not
    a re-test of folding or of the filesystem (those are covered
    elsewhere)."""
    row_a = RunRow(
        job="j1",
        run_number=1,
        catalog_hash="hA",
        started_at=T0,
        status="SUCCESS",
        clock_source="journal",
    )
    row_b = RunRow(
        job="j1",
        run_number=1,
        catalog_hash="hB",
        started_at=T0 + timedelta(hours=1),
        status="SUCCESS",
        clock_source="journal",
    )
    by_root = {Path("/a"): [row_a], Path("/b"): [row_b]}
    monkeypatch.setattr(runner_history, "read_run_root", lambda root: by_root[root])
    rows = read_run_roots([Path("/a"), Path("/b")])
    assert [r.catalog_hash for r in rows] == ["hA", "hB"]  # segmented, never one series
    assert rows[0].started_at < rows[1].started_at


def test_a_run_still_running_at_the_end_of_the_journal_gets_a_null_duration() -> None:
    """(DL-113 decision 3): dispatched, no completion anywhere in
    the journal -- RUNNING, ended_at/duration_s null, never fabricated."""
    records = [_header(), _dispatch("j1", 1, run_dir=None, started_at=T0)]
    [row] = fold_run_rows(records)
    assert row.status == "RUNNING"
    assert row.ended_at is None
    assert row.duration_s is None


def test_reconcile_completion_with_no_true_ended_at_stays_incomplete() -> None:
    """(DL-113 decision 3, E7): `runner_startup._inject_completion`
    only writes `ended_at` into the payload when `resolve_spool` returned a
    real one. Verdict FAILURE `exit_status_unobservable` is TRUE, but the
    event's own `at` is only when the engine noticed at resume -- never the
    row's ended_at."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(
            1,
            T0 + timedelta(hours=3),
            job="j1",
            run_number=1,
            source="reconcile",
            status="FAILURE",
            cause="exit_status_unobservable",
        ),
    ]
    [row] = fold_run_rows(records)
    assert row.status == "FAILURE"
    assert row.ended_at is None
    assert row.duration_s is None


def test_reconcile_completion_with_a_true_ended_at_gets_its_real_duration() -> None:
    """The OTHER resume-ladder branch: status.json existed, so
    `resolve_spool` returned a real end and `_inject_completion` carried it
    in the payload -- this run is complete, on the journal clock."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(
            1,
            T0 + timedelta(hours=3),
            job="j1",
            run_number=1,
            source="reconcile",
            exit_code=0,
            ended_at=T0 + timedelta(minutes=5),
        ),
    ]
    [row] = fold_run_rows(records)
    assert row.status == "SUCCESS"
    assert row.ended_at == T0 + timedelta(minutes=5)
    assert row.duration_s == 300.0
    assert row.clock_source == "journal"


def test_a_killjob_terminated_run_closes_via_the_trace_not_a_status_input() -> None:
    """KILLJOB is decided by the oracle synchronously while processing the
    KILLJOB input itself (oracle.py `_terminate`) -- no adapter completion
    is ever journaled for it. Reading only dispatch + STATUS records would
    misreport this run as still RUNNING; the trace shows the real close."""
    records = [_header(), _dispatch("j1", 1, run_dir=None, started_at=T0)]
    trace = [
        TraceEntry(
            at=T0, job="j1", transition="INACTIVE->STARTING", cause="STARTJOB event (control)"
        ),
        TraceEntry(at=T0, job="j1", transition="STARTING->RUNNING", cause="admitted"),
        TraceEntry(
            at=T0 + timedelta(minutes=2),
            job="j1",
            transition="RUNNING->TERMINATED",
            cause="KILLJOB",
        ),
    ]
    [row] = fold_run_rows(records, trace=trace)
    assert row.status == "TERMINATED"
    assert row.ended_at == T0 + timedelta(minutes=2)
    assert row.exit_code is None
    assert row.clock_source == "journal"
    assert row.started_by == "STARTJOB event (control)"


def test_box_row_folds_entirely_from_the_trace() -> None:
    """(DL-113 decision 2): a box gets no dispatch record
    (runner-design ss4's dispatch table) and its fold is emitted, never
    journaled -- the row comes from the Nth `*->STARTING` transition to the
    next terminal one."""
    catalog = lower_source(
        "insert_job: b1\njob_type: b\n\n"
        "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\nbox_name: b1\n"
    )
    trace = [
        TraceEntry(
            at=T0, job="b1", transition="INACTIVE->STARTING", cause="STARTJOB event (control)"
        ),
        TraceEntry(at=T0, job="b1", transition="STARTING->RUNNING", cause="admitted"),
        TraceEntry(
            at=T0 + timedelta(minutes=5), job="b1", transition="RUNNING->SUCCESS", cause="box fold"
        ),
    ]
    [row] = fold_run_rows([_header()], catalog=catalog, trace=trace)
    assert row.job == "b1"
    assert row.run_number == 1
    assert row.started_at == T0
    assert row.ended_at == T0 + timedelta(minutes=5)
    assert row.duration_s == 300.0
    assert row.status == "SUCCESS"
    assert row.exit_code is None
    assert row.run_dir is None
    assert row.executor_id is None
    assert row.clock_source == "journal"
    assert row.started_by == "STARTJOB event (control)"


def test_a_box_still_running_at_journal_end_gets_a_null_duration_too() -> None:
    catalog = lower_source("insert_job: b1\njob_type: b\n")
    trace = [
        TraceEntry(
            at=T0, job="b1", transition="INACTIVE->STARTING", cause="STARTJOB event (control)"
        ),
        TraceEntry(at=T0, job="b1", transition="STARTING->RUNNING", cause="admitted"),
    ]
    [row] = fold_run_rows([_header()], catalog=catalog, trace=trace)
    assert row.status == "RUNNING"
    assert row.ended_at is None
    assert row.duration_s is None


def test_a_second_box_run_opens_its_own_window_after_the_first_closes() -> None:
    catalog = lower_source("insert_job: b1\njob_type: b\n")
    trace = [
        TraceEntry(at=T0, job="b1", transition="INACTIVE->STARTING", cause="first"),
        TraceEntry(at=T0, job="b1", transition="STARTING->RUNNING", cause="first"),
        TraceEntry(
            at=T0 + timedelta(minutes=1), job="b1", transition="RUNNING->SUCCESS", cause="fold"
        ),
        TraceEntry(
            at=T0 + timedelta(hours=1),
            job="b1",
            transition="SUCCESS->STARTING",
            cause="FORCE_STARTJOB event (control)",
        ),
        TraceEntry(
            at=T0 + timedelta(hours=1), job="b1", transition="STARTING->RUNNING", cause="second"
        ),
        TraceEntry(
            at=T0 + timedelta(hours=1, minutes=1),
            job="b1",
            transition="RUNNING->SUCCESS",
            cause="fold",
        ),
    ]
    rows = fold_run_rows([_header()], catalog=catalog, trace=trace)
    assert [r.run_number for r in rows] == [1, 2]
    assert rows[1].started_at == T0 + timedelta(hours=1)
    assert rows[1].started_by == "FORCE_STARTJOB event (control)"


def test_a_dropped_tick_produces_no_row() -> None:
    """(DL-113: a start that produced no run is not a run row): a drop record is not an
    input and folds to nothing."""
    assert fold_run_rows([_header(), _drop("j1")]) == []


def test_a_refused_or_held_job_with_no_actual_start_produces_no_row() -> None:
    """A START_REFUSED trace marker (out-of-band, no "->") and no dispatch:
    nothing ever actually ran."""
    trace = [TraceEntry(at=T0, job="j1", transition="START_REFUSED", cause="on_hold (control)")]
    assert fold_run_rows([_header()], trace=trace) == []


def test_exit_code_status_honors_the_jobs_own_sem09_boundary_with_a_catalog() -> None:
    catalog = lower_source(
        "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\nmax_exit_success: 2\n"
    )
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=2),
    ]
    [row] = fold_run_rows(records, catalog=catalog)
    assert row.status == "SUCCESS"  # 2 <= max_exit_success 2


def test_exit_code_status_without_a_catalog_uses_the_bare_sem09_default() -> None:
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=2),
    ]
    [row] = fold_run_rows(records)  # no catalog: max_exit_success=0 default
    assert row.status == "FAILURE"


def test_leaf_row_carries_its_box_name_from_the_catalog() -> None:
    catalog = lower_source(
        "insert_job: b1\njob_type: b\n\n"
        "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b1\n"
    )
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records, catalog=catalog)
    assert row.box_name == "b1"


def test_executor_id_comes_from_the_spawn_effect_record() -> None:
    records = [
        _header(),
        _spawn_effect(1, T0, job="j1", run_number=1, executor_id="local"),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records)
    assert row.executor_id == "local"


def test_a_run_numberless_change_status_overwrites_the_currently_open_run() -> None:
    """An operator CHANGE_STATUS (cli.py sendevent has no --run-number
    option) names no run_number in its payload; it overwrites whichever run
    is currently open, the same read the oracle's own SEM-01 latching
    gives it."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(
            1,
            T0 + timedelta(minutes=5),
            job="j1",
            run_number=None,
            source="control",
            status="SUCCESS",
        ),
    ]
    [row] = fold_run_rows(records)
    assert row.run_number == 1
    assert row.status == "SUCCESS"


def test_fold_run_rows_refuses_a_record_list_with_no_header() -> None:
    with pytest.raises(RunHistoryError, match="header"):
        fold_run_rows([_dispatch("j1", 1, run_dir=None, started_at=T0)])


# ------------------------------------------------------------- 2. the spool


def test_read_spool_reads_a_valid_matching_spawn_and_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "j1.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "started_at": "2026-07-01T08:00:01.5+00:00"})
    )
    (run_dir / "status.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "ended_at": "2026-07-01T08:00:11.5+00:00"})
    )
    found = read_spool(run_dir, "j1", 1)
    assert found is not None
    assert found.started_at == datetime(2026, 7, 1, 8, 0, 1, 500000)  # aware -> naive UTC
    assert found.ended_at == datetime(2026, 7, 1, 8, 0, 11, 500000)


def test_read_spool_returns_none_for_a_pruned_run_dir(tmp_path: Path) -> None:
    """(deployment-runbook ss2: retention is a business decision): retention prunes the spool
    long before the run rows it summarized -- a missing directory is not an
    error, just no spool."""
    assert read_spool(tmp_path / "runs" / "gone.1", "gone", 1) is None


def test_read_spool_never_trusts_a_spawn_record_for_a_different_run(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "j1.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "OTHER", "run_number": 9, "started_at": T0.isoformat()})
    )
    assert read_spool(run_dir, "j1", 1) is None


def test_spool_preferred_over_journal_when_both_files_are_present(tmp_path: Path) -> None:
    """(DL-113 decision 1): spool wins, wholesale, when it can
    prove both ends of this exact run."""
    run_dir = tmp_path / "runs" / "j1.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "started_at": "2026-07-01T08:00:01+00:00"})
    )
    (run_dir / "status.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "ended_at": "2026-07-01T08:00:11+00:00"})
    )
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=str(run_dir), started_at=T0),  # journal says T0
        _status_input(1, T0 + timedelta(seconds=20), job="j1", run_number=1, exit_code=0),
    ]
    spool_read = read_spool(run_dir, "j1", 1)
    assert spool_read is not None  # a None here would pass the test for the wrong reason
    [row] = fold_run_rows(records, spool={("j1", 1): spool_read})
    assert row.clock_source == "spool"
    assert row.started_at == datetime(2026, 7, 1, 8, 0, 1)
    assert row.ended_at == datetime(2026, 7, 1, 8, 0, 11)


def test_pruned_spool_falls_back_to_the_journal_clock_entirely(tmp_path: Path) -> None:
    """The whole run_dir is gone (retention already ran): clock_source is
    "journal" for BOTH started_at and ended_at -- never a per-field mix."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=str(tmp_path / "runs" / "j1.1"), started_at=T0),
        _status_input(1, T0 + timedelta(seconds=20), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records, spool={})  # nothing read: pruned
    assert row.clock_source == "journal"
    assert row.started_at == T0
    assert row.ended_at == T0 + timedelta(seconds=20)


def test_a_partially_pruned_spool_never_mixes_clocks(tmp_path: Path) -> None:
    """spawn.json survives, status.json does not (an unusual half-prune):
    the row still falls back to journal WHOLESALE rather than pairing a
    spool start with a journal end."""
    run_dir = tmp_path / "runs" / "j1.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "started_at": "2026-07-01T08:00:01+00:00"})
    )
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=str(run_dir), started_at=T0),
        _status_input(1, T0 + timedelta(seconds=20), job="j1", run_number=1, exit_code=0),
    ]
    spool_read = read_spool(run_dir, "j1", 1)
    assert spool_read is not None  # a None here would pass the test for the wrong reason
    [row] = fold_run_rows(records, spool={("j1", 1): spool_read})
    assert row.clock_source == "journal"
    assert row.started_at == T0  # NOT the spool's 08:00:01


def test_an_open_run_prefers_spools_started_at_with_no_ended_at_to_mix(tmp_path: Path) -> None:
    """A run still in flight: spawn.json alone is enough to prefer the
    spool's real start, and there is no ended_at to invent."""
    run_dir = tmp_path / "runs" / "j1.1"
    run_dir.mkdir(parents=True)
    (run_dir / "spawn.json").write_text(
        json.dumps({"job": "j1", "run_number": 1, "started_at": "2026-07-01T08:00:01+00:00"})
    )
    records = [_header(), _dispatch("j1", 1, run_dir=str(run_dir), started_at=T0)]
    spool_read = read_spool(run_dir, "j1", 1)
    assert spool_read is not None  # a None here would pass the test for the wrong reason
    [row] = fold_run_rows(records, spool={("j1", 1): spool_read})
    assert row.clock_source == "spool"
    assert row.started_at == datetime(2026, 7, 1, 8, 0, 1)
    assert row.ended_at is None
    assert row.duration_s is None


# --------------------------------------------------------- 3. end to end


async def _run_real_and_manifest(
    text: str, run_root: Path, jobs: list[str], *, file: str = "estate.jil"
) -> None:
    """start_run + inject STARTJOB for every named job, run to quiescence,
    shut down, close the journal, and write manifest/ the way `dsl41 run`
    does (`cli._write_manifest`) -- the shared shape every read_run_root
    integration scenario below needs. Real subprocesses (`command: exit N`),
    the test_runner_adapters.py pattern."""
    jil = parse(text, file=file)
    catalog = lower_catalog([jil], permit_unknown=False)
    clock = RealClock()
    engine = start_run(
        catalog, run_root, clock=clock, adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)}
    )
    _write_manifest(run_root, [jil], catalog, {}, {})
    now = clock.now()
    for job in jobs:
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": job}))
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def test_read_run_root_end_to_end_leaf_and_box_rows(tmp_path: Path) -> None:
    text = (
        "insert_job: b1\njob_type: b\n\n"
        "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\nbox_name: b1\n"
    )
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["b1"]))

    rows = read_run_root(run_root)
    by_job = {row.job: row for row in rows}
    assert by_job["j1"].status == "SUCCESS"
    assert by_job["j1"].exit_code == 0
    assert by_job["j1"].clock_source == "spool"
    assert by_job["j1"].box_name == "b1"
    assert by_job["j1"].run_dir is not None and Path(by_job["j1"].run_dir).exists()
    assert by_job["b1"].status == "SUCCESS"
    assert by_job["b1"].clock_source == "journal"
    assert by_job["b1"].run_dir is None
    assert by_job["b1"].started_at <= by_job["j1"].started_at


def test_read_run_root_degrades_rather_than_refusing_when_there_is_no_manifest(
    tmp_path: Path,
) -> None:
    """(DL-113 decision 5): `manifest/` is DL-66, so every run root predating
    it has none and retention prunes it -- exactly the old roots a history
    tool exists to read. The rows come back, and every one says
    `records_only` so nothing reads like a complete row."""
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"

    async def scenario() -> None:
        jil = parse(text, file="estate.jil")
        catalog = lower_catalog([jil], permit_unknown=False)
        clock = RealClock()
        engine = start_run(
            catalog, run_root, clock=clock, adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)}
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j1"}))
        await engine.run_until_quiescent(datetime.max)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())  # deliberately no _write_manifest call
    [row] = read_run_root(run_root)
    assert row.job == "j1"
    assert row.fidelity == "records_only"
    assert row.job_hash is None  # no catalog to fingerprint against
    assert row.box_name is None and row.started_by is None
    assert row.status == "SUCCESS"  # a plain exit 0 still reads correctly


def test_a_manifest_belonging_to_another_journal_still_refuses(tmp_path: Path) -> None:
    """(DL-113 decision 5): a MISSING manifest is a missing fact and degrades;
    a manifest whose catalog_hash disagrees with the header is a WRONG one,
    and reading a run against another run's estate is the silent semantic
    drift runner-design ss7 refuses everywhere else."""
    run_root = tmp_path / "run"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", run_root, ["j1"]
        )
    )
    manifest = run_root / "manifest" / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["catalog_hash"] = "not-this-journals-hash"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(RunHistoryError, match="not this journal's"):
        read_run_root(run_root)


def test_cli_runs_table_prints_a_labelled_break_across_two_run_roots(tmp_path: Path) -> None:
    """(DL-113 decision 4): two run roots under different catalogs,
    same job name -- the table shows one segmented series with a visible
    break, not a blended line."""
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", root_a, ["j1"]
        )
    )
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 1\nmachine: m1\nmax_exit_success: 1\n",
            root_b,
            ["j1"],
        )
    )
    result = CliRunner().invoke(app, ["runs", str(root_a), str(root_b), "--job", "j1"])
    assert result.exit_code == 0
    assert "definition changed" in result.output  # the job's own hash, not the estate's
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert sum(1 for line in lines if line.strip().startswith("j1")) == 2


def test_the_break_marks_only_the_job_whose_definition_actually_changed(tmp_path: Path) -> None:
    """(DL-113 decision 4): the estate hash moves for BOTH jobs when either
    changes, which is why the break cannot be drawn from it. Only `j2`
    changed here, so only `j2` gets a break line."""
    unchanged = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    asyncio.run(
        _run_real_and_manifest(
            unchanged + "\ninsert_job: j2\njob_type: c\ncommand: exit 0\nmachine: m1\n",
            root_a,
            ["j1", "j2"],
        )
    )
    asyncio.run(
        _run_real_and_manifest(
            unchanged + "\ninsert_job: j2\njob_type: c\ncommand: exit 1\nmachine: m1\n"
            "max_exit_success: 1\n",
            root_b,
            ["j1", "j2"],
        )
    )
    rows = read_run_roots([root_a, root_b])
    by_job: dict[str, list[Any]] = {}
    for row in rows:
        by_job.setdefault(row.job, []).append(row)
    # the estate hash moved for both jobs; the per-job fingerprint only for j2
    assert by_job["j1"][0].catalog_hash != by_job["j1"][1].catalog_hash
    assert by_job["j1"][0].job_hash == by_job["j1"][1].job_hash
    assert by_job["j2"][0].job_hash != by_job["j2"][1].job_hash

    result = CliRunner().invoke(app, ["runs", str(root_a), str(root_b)])
    assert result.exit_code == 0
    breaks = [line for line in result.output.splitlines() if "definition changed" in line]
    assert len(breaks) == 1 and "j2" in breaks[0]


def test_cli_runs_json_and_csv_carry_catalog_hash_on_every_row(tmp_path: Path) -> None:
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["j1"]))

    as_json = CliRunner().invoke(app, ["runs", str(run_root), "--format", "json"])
    assert as_json.exit_code == 0
    payload = json.loads(as_json.output)
    assert len(payload) == 1 and payload[0]["job"] == "j1" and payload[0]["catalog_hash"]

    as_csv = CliRunner().invoke(app, ["runs", str(run_root), "--format", "csv"])
    assert as_csv.exit_code == 0
    header, row, *_ = as_csv.output.splitlines()
    assert header.split(",")[0] == "job"
    assert row.split(",")[0] == "j1"


def test_cli_runs_since_filters_out_earlier_runs(tmp_path: Path) -> None:
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["j1"]))
    far_future = (datetime.now().replace(microsecond=0) + timedelta(days=3650)).isoformat()
    result = CliRunner().invoke(app, ["runs", str(run_root), "--since", far_future])
    assert result.exit_code == 0
    assert "j1" not in result.output
