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

from contextlib import closing

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from model_harness import _lose_the_decision_for
from typer.testing import CliRunner

from dsl41 import runner_history
from dsl41.ast_jil import parse
from dsl41.cli import app
from dsl41.ir import lower_catalog, lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner_adapters import LocalCommandAdapter
from dsl41.runner_admission import ApplyResult
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_history import (
    RunHistoryError,
    RunRow,
    fold_run_rows,
    read_run_root,
    read_run_roots,
    read_spool,
    replay_trace,
)
from dsl41.runner_journal import dispatched_runs, read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)

#: a run_id in the ss11a grammar, spelled as test_decision_record.py spells it
_RID = "00000000-0000-4000-8000-000000000001"

#: one CMD job, the smallest estate a replay can be asked about
_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"


# ---------------------------------------------------- record-shape builders


def _header(catalog_hash: str = "h1", started_at: datetime = T0) -> dict[str, Any]:
    """The opening record, in the shape `Journal.create` writes it since
    DL-130. The helper keeps its name because every caller below means
    "the record the fold reads its estate identity from"."""
    return {
        "rec": "segment",
        "segment_no": 1,
        "estate_id": "e-test",
        "period_id": 1,
        "baseline_id": "b-test",
        "catalog_hash": catalog_hash,
        "catalog_hash_version": 2,
        "source_bundle_hash": "sha256:bundle",
        "runtime_hash": "sha256:runtime",
        "state_machine_version": STATE_MACHINE_VERSION,
        "clock_domain": "real",
        "first_index": 1,
        "opens_from_seal": None,
        "reclaimed": None,
        "trust_unaudited": None,
        "at": started_at.isoformat(),
    }


def _dispatch(
    job: str, run_number: int, *, run_dir: str | None, started_at: datetime, run_id: str = _RID
) -> dict[str, Any]:
    return {
        "rec": "dispatch",
        "job": job,
        "run_number": run_number,
        "run_id": run_id,
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
    }


def _applied(index: int) -> dict[str, Any]:
    """The no-effect applied decision that answers attempt `index`.
    `Journal.decision` writes one for EVERY admitted input -- the absence
    of a decision is how replay recognises the crash window -- and since
    DL-156 the records-only fold reads the absence the same way, so a
    fixture depicting an intact log carries one per input. `revisions`
    stays empty although a real applied STATUS moves one: the fold never
    reads the field."""
    return {
        "rec": "decision",
        "index": index,
        "request_id": f"r{index}",
        "decision": "applied",
        "reason": None,
        "revisions": {},
        "legacy_batch": False,
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
        _applied(1),
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
        _applied(1),
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
        _applied(1),
    ]
    [row] = fold_run_rows(records)
    assert row.status == "SUCCESS"
    assert row.ended_at == T0 + timedelta(minutes=5)
    assert row.duration_s == 300.0
    assert row.clock_source == "journal"


def test_a_killjob_terminated_run_closes_via_the_trace_not_a_status_input() -> None:
    """KILLJOB is decided by the oracle synchronously while processing the
    KILLJOB input itself (the job chart's kill row) -- no adapter completion
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
        _applied(1),
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


def test_a_well_formed_dispatch_still_folds_its_row_and_binds_its_run_id() -> None:
    """The control for the refusal above: a log this estate wrote folds its row, and the
    dispatch binds the run's id."""
    records = [
        _header(),
        _applied(1),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
        _applied(2),
    ]
    [row] = fold_run_rows(records)
    assert (row.job, row.run_number, row.status) == ("j1", 1, "SUCCESS")
    assert dispatched_runs(records) == {("j1", 1): _RID}


def test_a_spool_record_naming_a_stranger_reads_as_absent(tmp_path: Path) -> None:
    """DL-118 at the reporting layer: the durable SPAWN bound this run's
    run_id, and a spool record naming a different one is a stranger's --
    its timings must not be reported as this run's. Absent, not refused:
    offline reporting costs one row's timings for one corrupt directory,
    not the whole report."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "job": "j1",
                "run_number": 1,
                "run_id": "rid-else",
                "started_at": "2026-07-01T08:00:00",
            }
        )
    )
    assert read_spool(run_dir, "j1", 1, "rid-bound") is None
    found = read_spool(run_dir, "j1", 1, "rid-else")
    assert found is not None and found.started_at is not None
    # and a stranger's status alone costs only the end time
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "job": "j1",
                "run_number": 1,
                "run_id": "rid-bound",
                "started_at": "2026-07-01T08:00:00",
            }
        )
    )
    (run_dir / "status.json").write_text(
        json.dumps(
            {"job": "j1", "run_number": 1, "run_id": "rid-else", "ended_at": "2026-07-01T08:05:00"}
        )
    )
    partial = read_spool(run_dir, "j1", 1, "rid-bound")
    assert partial is not None and partial.ended_at is None


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
        _applied(1),
    ]
    [row] = fold_run_rows(records)
    assert row.run_number == 1
    assert row.status == "SUCCESS"


def test_fold_run_rows_refuses_a_record_list_with_no_opening_record() -> None:
    with pytest.raises(RunHistoryError, match="segment"):
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
        _applied(1),
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
        _applied(1),
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
        _applied(1),
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
    """Start the run the way `dsl41 run` does -- start_run records the configuration row,
    the catalog the history reads back -- inject STARTJOB for every named job, run to
    quiescence, shut down and close the store. The shared shape every read_run_root
    integration scenario below needs. Real subprocesses (`command: exit N`), the
    test_runner_adapters.py pattern."""
    jil = parse(text, file=file)
    catalog = lower_catalog([jil], permit_unknown=False)
    clock = RealClock()
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
    )
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


def test_read_run_root_reads_the_catalog_from_the_configuration_row(tmp_path: Path) -> None:
    """(DL-113 decision 5, DL-424): the store's configuration row carries the canonical
    catalog the engine ran, so every root this binary wrote folds at full fidelity, with
    nothing staged beside it and no estate file named."""
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["j1"]))
    [row] = read_run_root(run_root)
    assert row.job == "j1"
    assert row.fidelity == "full"
    assert row.job_hash is not None
    assert row.started_by is not None
    assert row.status == "SUCCESS"


def test_an_unreadable_configuration_row_refuses_naming_the_root(tmp_path: Path) -> None:
    """(DL-113 decision 5): a configuration that does not read is a WRONG fact, not a missing
    one, and reading a run against a catalog it may not have run is the silent semantic drift
    runner-design ss7 refuses everywhere else."""
    import sqlite3

    run_root = tmp_path / "run"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", run_root, ["j1"]
        )
    )
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE configs SET data = ?", (b'{"catalog": 1}',))
    with pytest.raises(RunHistoryError, match="unreadable configuration row") as refused:
        read_run_root(run_root)
    assert str(run_root) in str(refused.value)


def test_a_1x_run_root_is_refused_by_name(tmp_path: Path) -> None:
    """A 1.x root holds `journal.jsonl` and `wal/` and no store: history names it rather than
    calling it unreadable (docs/protocol-evolution.md ss6)."""
    (tmp_path / "wal").mkdir()
    with pytest.raises(RunHistoryError, match="a 1.x run root"):
        read_run_root(tmp_path)
    with pytest.raises(RunHistoryError, match="not a run root"):
        read_run_root(tmp_path / "nothing")


async def _resume_real(jil: Path, run_root: Path, jobs: list[str]) -> None:
    """`dsl41 run --resume` on a sealed root, in process: open the period
    the seal committed and run the named jobs in it."""
    catalog = lower_catalog([parse(jil.read_text(), file=str(jil))], permit_unknown=False)
    clock = RealClock()
    engine = await resume_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter()},
    )
    now = clock.now()
    for job in jobs:
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": job}))
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


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
    assert header.split(",")[-1] == "undecided"
    assert row.split(",")[-1] == "false"  # csv spells bools as json does (DL-156)


def test_cli_runs_since_filters_out_earlier_runs(tmp_path: Path) -> None:
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["j1"]))
    far_future = (datetime.now().replace(microsecond=0) + timedelta(days=3650)).isoformat()
    result = CliRunner().invoke(app, ["runs", str(run_root), "--since", far_future])
    assert result.exit_code == 0
    assert "j1" not in result.output


# ------------------------------------------ 4. what the ss4 gate decided


async def _run_then_late_completion(run_root: Path, source: str) -> None:
    """A real run that FAILS, then a late `exit 0` for the same run.

    No hand-built `decision` records: the shapes this scenario turns on are
    the gate's own verdicts, and a fixture that spelled them would be
    asserting against its own guess. `source` picks which verdict the ss4
    gate reaches -- an `adapter` completion is stale-gated and REJECTED
    ("job already terminal"), a `control` CHANGE_STATUS is never gated and
    is APPLIED (SEM-01 parity, `runner_admission._gate`)."""
    jil = parse("insert_job: j1\njob_type: c\ncommand: exit 1\nmachine: m1\n", file="estate.jil")
    catalog = lower_catalog([jil], permit_unknown=False)
    clock = RealClock()
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
    )
    engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j1"}))
    await engine.run_until_quiescent(datetime.max)
    engine.inject(
        Event(
            at=clock.now(),
            kind="STATUS",
            payload={"job": "j1", "run_number": 1, "exit_code": 0},
        ),
        source=source,
    )
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _verdicts(run_root: Path) -> list[tuple[str, str | None]]:
    records = read_journal(run_root)
    return [(r["decision"], r.get("reason")) for r in records if r.get("rec") == "decision"]


def test_a_completion_the_gate_rejected_never_decides_the_row(tmp_path: Path) -> None:
    """A durable decision is AUTHORITATIVE (concurrency-model ss4).
    `replay_inputs` does not feed a rejected attempt to the oracle, so the
    fold does not read one either.

    The reproduction: the run really reaches FAILURE exit 1, the late `exit
    0` is admitted and REJECTED, and `read_run_root` reported SUCCESS 0 --
    the offline half of exactly what the ss4 gate exists to prevent,
    reaching `dsl41 runs` and `read_run_root` alike."""
    run_root = tmp_path / "run"
    asyncio.run(_run_then_late_completion(run_root, "adapter"))
    assert ("rejected", "job already terminal") in _verdicts(run_root)

    [row] = read_run_root(run_root)
    assert row.status == "FAILURE"
    assert row.exit_code == 1


def test_a_completion_the_gate_applied_still_decides_the_row(tmp_path: Path) -> None:
    """The skip reads the DECISION, never lateness. The same late `exit 0`
    as a control CHANGE_STATUS is never stale-gated (SEM-01 parity), the
    gate APPLIES it, and it still overwrites the row -- exactly as it
    overwrites oracle state."""
    run_root = tmp_path / "run"
    asyncio.run(_run_then_late_completion(run_root, "control"))
    assert all(verdict == "applied" for verdict, _ in _verdicts(run_root))

    [row] = read_run_root(run_root)
    assert row.status == "SUCCESS"
    assert row.exit_code == 0


# ------------------------------------------ 4a. the ss4 crash window (DL-156)


async def _killed_run_then_late_completion(run_root: Path) -> None:
    """The DL-156 crash-window estate, up to the crash itself: KILLJOB
    terminates a real run mid-flight (the test_runner_adapters.py KILLJOB
    scenario, staged for full fidelity like `_run_real_and_manifest`), then
    the wrapper's late `exit 0` STATUS is admitted and REJECTED by the ss4
    stale gate. `_lose_the_decision_for` below supplies the crash: the
    admitted input stays durable, the verdict does not."""
    jil = parse("insert_job: j1\njob_type: c\ncommand: sleep 20\nmachine: m1\n", file="estate.jil")
    catalog = lower_catalog([jil], permit_unknown=False)
    clock = RealClock()
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
    )
    engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j1"}))
    # The kill must find a run to kill. The adapter's `dispatch` record and
    # the wrapper's spawn.json are written by a real spawn, whose duration a
    # loaded host stretches past any fixed offset (CI, 2026-09-09: a runner
    # 3.5x slower than a workstation), and a KILLJOB queued at a fixed offset
    # that lands first ends a run that never dispatched: no row to fold,
    # which is not the DL-156 estate. In the real domain a horizon call
    # blocks on the live task, so the kill is injected from a concurrent
    # task once spawn.json exists (`_enqueue` wakes the loop); `sleep 20`
    # keeps the run alive however slow the spawn, and the kill ends it.
    killer = asyncio.create_task(_kill_once_spawned(engine, clock, run_root, "j1", 1))
    await engine.run_until_quiescent(datetime.max)
    await killer  # a spawn that never came surfaces here, not as a silent no-kill
    engine.inject(
        Event(
            at=clock.now(),
            kind="STATUS",
            payload={"job": "j1", "run_number": 1, "exit_code": 0},
        ),
        source="adapter",
    )
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


async def _kill_once_spawned(
    engine: Any,
    clock: RealClock,
    run_root: Path,
    job: str,
    run_number: int,
    *,
    timeout_s: float = 15.0,
) -> None:
    """Inject KILLJOB for `job` once the wrapper of its run has durably
    written spawn.json (runner_wrapper step 2), which the adapter's
    `dispatch` record precedes -- so the kill always meets a dispatched run.
    Runs beside the engine's own loop: the journal is not read (its dispatch
    record is buffered until a later flushing write) and the loop is not
    stepped (a real-domain horizon call blocks on the live task)."""
    spawn = run_root / "runs" / f"{job}.{run_number}" / "1" / "spawn.json"
    deadline = time.monotonic() + timeout_s
    while not spawn.exists():
        assert time.monotonic() < deadline, f"{job} never spawned within {timeout_s}s"
        await asyncio.sleep(0.02)
    engine.inject(Event(at=clock.now(), kind="KILLJOB", payload={"job": job}))


def test_a_store_that_lost_a_rejection_refuses_rather_than_folds(tmp_path: Path) -> None:
    """DL-156's crash window no longer exists: an attempt and its decision are one store
    transaction. A store that holds the killed run's late `exit 0` without its rejection
    disagrees with itself, and the full-fidelity read refuses it, naming the root, rather
    than re-deciding it."""
    run_root = tmp_path / "run"
    asyncio.run(_killed_run_then_late_completion(run_root))
    assert ("rejected", "job already terminal") in _verdicts(run_root)
    [row] = read_run_root(run_root)
    assert (row.status, row.exit_code, row.undecided) == ("TERMINATED", None, False)
    _lose_the_decision_for(run_root, "adapter")
    assert ("rejected", "job already terminal") not in _verdicts(run_root)

    with pytest.raises(RunHistoryError, match="has no decision") as refused:
        read_run_root(run_root)
    assert str(run_root) in str(refused.value)


def test_records_only_refuses_to_let_an_undecided_completion_decide(tmp_path: Path) -> None:
    """DL-156, the records-only half, over the SAME records: with no
    catalog there is no gate to re-run, and refusing to decide exercises no
    authority either. The row keeps its pre-completion status -- RUNNING,
    the documented records-only KILLJOB degrade -- and carries the flag."""
    run_root = tmp_path / "run"
    asyncio.run(_killed_run_then_late_completion(run_root))
    _lose_the_decision_for(run_root, "adapter")

    records = read_journal(run_root)
    [row] = fold_run_rows(records)
    assert row.fidelity == "records_only"
    assert row.undecided is True
    assert row.status == "RUNNING"  # NOT the undecided completion's SUCCESS
    assert row.exit_code is None


def test_a_restart_refuses_a_store_that_lost_a_verdict(tmp_path: Path) -> None:
    """The restart's half of the same rule: a store holding an attempt with no decision is
    refused before anything is written, and the records alone still refuse to let the
    undecided completion decide its row."""
    run_root = tmp_path / "run"
    asyncio.run(_run_then_late_completion(run_root, "control"))
    _lose_the_decision_for(run_root, "control")
    [lost] = fold_run_rows(read_journal(run_root))
    assert (lost.status, lost.undecided) == ("FAILURE", True)

    async def resume() -> None:
        jil = parse(
            "insert_job: j1\njob_type: c\ncommand: exit 1\nmachine: m1\n", file="estate.jil"
        )
        await resume_run(
            lower_catalog([jil], permit_unknown=False),
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
            settle_seconds=0.0,
        )

    with pytest.raises(EngineError, match="has no decision"):
        asyncio.run(resume())


def test_a_decided_completion_never_marks_its_row_undecided(tmp_path: Path) -> None:
    """The default: every completion in an intact log has its durable
    verdict and nothing sets the flag, full and records-only fidelity
    alike -- and the explicitly rejected late `exit 0` is still skipped in
    both, exactly as before DL-156."""
    run_root = tmp_path / "run"
    asyncio.run(_run_then_late_completion(run_root, "adapter"))

    [row] = read_run_root(run_root)
    assert (row.status, row.exit_code, row.undecided) == ("FAILURE", 1, False)

    records = read_journal(run_root)
    [records_only] = fold_run_rows(records)
    assert (records_only.status, records_only.exit_code) == ("FAILURE", 1)
    assert records_only.undecided is False


def test_a_records_only_fold_refuses_recovered_verdicts() -> None:
    """DL-156's grant, enforced at the door: the recovered verdicts are
    the gate's re-run UNDER a catalog, and a records-only fold holds no
    gate to have run -- consuming them would be exactly the authority the
    records-only half refuses to exercise. Refuse-don't-degrade is the
    module's posture, so handing them anyway refuses by name."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    verdict = ApplyResult(
        index=1, request_id="r1", decision="rejected", reason="job already terminal"
    )
    with pytest.raises(RunHistoryError, match="records-only fold cannot consume"):
        fold_run_rows(records, recovered=[verdict])


def test_a_decided_row_with_a_later_undecided_completion_keeps_status_and_flags() -> None:
    """DL-156's chosen corner semantics, pinned so the shape is a choice
    and not an accident: a decided completion followed by an undecided one
    for the same run keeps the decided STATUS -- the verdict the records do
    hold -- and still carries the flag, because the newest completion is
    one the records do not decide. Over-warning in the safe direction."""
    records = [
        _header(),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=1),
        _applied(1),
        _status_input(2, T0 + timedelta(minutes=2), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records)
    assert row.status == "FAILURE"  # the durable verdict stands
    assert row.exit_code == 1
    assert row.undecided is True  # and the newest completion is named undecided


# ---------------------------------------- 5. the replay version gate


def test_history_refuses_a_store_naming_a_foreign_state_version(tmp_path: Path) -> None:
    """period-model ss2.1: one executable implements one state machine, so a foreign binary
    "cannot lead OR replay". Without the replay half a foreign store replayed in silence and
    the trace narrated transitions this build's semantics invented."""
    import sqlite3

    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(_SOLO_JIL, run_root, ["j1"]))
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'state_version'")
    with pytest.raises(RunHistoryError, match="state version 999"):
        read_run_root(run_root)
    result = CliRunner().invoke(app, ["journal", str(run_root)])
    assert result.exit_code == 2
    assert "state version 999" in result.output


def test_replay_trace_reads_the_configuration_s_profile(tmp_path: Path) -> None:
    """The trace is replayed under the configuration's own profile and catalog (DL-252), and
    an empty store replays to the empty trace."""
    from dsl41.runner_config import Configuration, RuntimeProfile
    from dsl41.runner_store import MemoryStore

    configuration = Configuration(catalog=lower_source(_SOLO_JIL), profile=RuntimeProfile())
    assert replay_trace(MemoryStore(), configuration, "here") == []


def test_a_pruned_store_replays_its_trace_from_the_latest_snapshot(tmp_path: Path) -> None:
    """A store whose oldest rows the retention pruned replays from its latest snapshot: the
    trace covers what follows it, and the configuration row stands in for the pruned
    opening."""
    from dsl41.runner_config import EngineSettings

    run_root = tmp_path / "run"

    async def scenario() -> None:
        jil = parse(_SOLO_JIL, file="estate.jil")
        clock = RealClock()
        engine = start_run(
            lower_catalog([jil], permit_unknown=False),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
            settings=EngineSettings(retention_days=1),
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j1"}))
        await engine.run_until_quiescent(datetime.max)
        assert engine.journal is not None
        engine.journal.store.prune_before(10**6, datetime(2999, 1, 1))  # nothing to keep yet
        engine.take_snapshot()
        engine.journal.store.prune_before(10**6, datetime(2999, 1, 1))
        await engine.shutdown()
        engine.journal.close()

    asyncio.run(scenario())
    assert all(r["rec"] != "segment" for r in read_journal(run_root))  # the head is gone
    assert read_run_root(run_root) == []  # nothing to fold, and nothing refused


def test_runs_that_began_before_the_snapshot_of_a_pruned_store_read_partial(
    tmp_path: Path,
) -> None:
    """Decision 6: the retention pruned only the opening, so a box run and its members are
    retained, but the replay starts at the snapshot taken after they ended and the trace holds
    none of them. The snapshot's rows anchor the fold: the box reads FAILURE, the member the
    term_run_time killed (no STATUS input) reads TERMINATED, never RUNNING, and each row says
    `partial`."""
    from store_surgery import rows

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_store import SqliteStore

    text = (
        "insert_job: x\njob_type: b\n\n"
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: x\n\n"
        "insert_job: c\njob_type: c\ncommand: y\nmachine: m1\nbox_name: x\n"
        "term_run_time: 1\n"
    )
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("a", 1): (30.0, 1)}, default=None)},
        )
        engine.inject(Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "v"}))
        engine.inject(Event(at=T0 + timedelta(minutes=1), kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        assert {job: rt.status for job, rt in engine.oracle.store.job.items()} == {
            "x": "FAILURE",
            "a": "FAILURE",
            "c": "TERMINATED",
        }
        engine.take_snapshot()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    [start] = [
        seq
        for seq, kind, body in rows(run_root)
        if kind == "input" and body.get("kind") == "STARTJOB"
    ]
    with closing(SqliteStore(run_root / "engine.db")) as store:
        store.prune_before(start, datetime(2999, 1, 1))  # the opening and the global only
    by_job = {row.job: row for row in read_run_root(run_root)}
    assert {job: (row.status, row.fidelity) for job, row in by_job.items()} == {
        "x": ("FAILURE", "partial"),
        "a": ("FAILURE", "partial"),
        "c": ("TERMINATED", "partial"),
    }
    assert by_job["c"].ended_at is not None
    result = CliRunner().invoke(app, ["runs", str(run_root)])
    assert result.exit_code == 0, result.output
    assert "fidelity=partial" in result.output


def test_every_retained_box_run_keeps_its_row_after_a_prune(tmp_path: Path) -> None:
    """Decision 6: the snapshot carries the run history, so a retained box run that began
    before it keeps its row. Box `b` and its one-minute member `j` run twice on day 2, five
    minutes apart; the snapshot's own retention prunes day 1's opening. All four runs keep
    their rows, each `partial`, with the statuses the snapshot recorded."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_config import EngineSettings

    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        "term_run_time: 1\n"
    )
    run_root = tmp_path / "run"
    day2 = T0 + timedelta(days=1)

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1, snapshot_interval_s=10**6),
        )
        for minutes in (0, 5):
            at = day2 + timedelta(minutes=minutes)
            engine.inject(Event(at=at, kind="STARTJOB", payload={"job": "b"}))
        await engine.run_until_quiescent(day2 + timedelta(minutes=20))
        engine.take_snapshot()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert all(r["rec"] != "segment" for r in read_journal(run_root))  # the opening is pruned
    rows = {
        (row.job, row.run_number): (row.status, row.fidelity) for row in read_run_root(run_root)
    }
    assert rows == {
        ("b", 1): ("FAILURE", "partial"),
        ("b", 2): ("FAILURE", "partial"),
        ("j", 1): ("TERMINATED", "partial"),
        ("j", 2): ("TERMINATED", "partial"),
    }


def test_a_run_after_a_restart_whose_history_retention_emptied_is_numbered_after_it(
    tmp_path: Path,
) -> None:
    """Decision 6: retention can empty a job's run history while the snapshot's rows still hold
    its run number. The next run after a restart is numbered from the row, so the second box
    run reads as run 2, and nothing is dropped."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_config import EngineSettings

    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        "term_run_time: 1\n"
    )
    run_root = tmp_path / "run"
    settings = EngineSettings(retention_days=1, snapshot_interval_s=10**6)

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=settings,
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "b"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        engine.inject(
            Event(at=T0 + timedelta(days=2), kind="SET_GLOBAL", payload={"name": "G", "value": "v"})
        )
        await engine.run_until_quiescent(T0 + timedelta(days=2))
        engine.take_snapshot()  # run 1 ended outside the window: the history keeps nothing
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())

    async def second() -> None:
        restart = T0 + timedelta(days=2, minutes=1)
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=restart),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=settings,
        )
        await engine.run_until_quiescent(restart)
        engine.inject(Event(at=restart, kind="STARTJOB", payload={"job": "b"}))
        await engine.run_until_quiescent(restart + timedelta(minutes=5))
        engine.take_snapshot()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(second())
    rows = {(row.job, row.run_number): row.status for row in read_run_root(run_root)}
    # run 1's records and history are outside the window; run 2 is whole and numbered after it
    assert rows == {("b", 2): "FAILURE", ("j", 2): "TERMINATED"}


def test_a_run_that_ended_before_the_anchor_with_no_trace_of_how_reads_unknown() -> None:
    """Decision 6: a later run had started by the snapshot, so the earlier one ended, and no
    retained record says how. The fold says UNKNOWN, not RUNNING."""
    from dsl41.oracle_state import JobRuntime

    records = [_header(), _dispatch("j1", 1, run_dir=None, started_at=T0)]
    records.append(_dispatch("j1", 2, run_dir=None, started_at=T0 + timedelta(minutes=5)))
    anchor = {"j1": JobRuntime(status="RUNNING", status_at=T0, run_number=2)}
    catalog = lower_source(_SOLO_JIL)
    first, second = fold_run_rows(records, catalog=catalog, anchor=anchor)
    assert (first.status, first.fidelity) == ("UNKNOWN", "partial")
    assert (second.status, second.fidelity) == ("RUNNING", "partial")


# ------------------------------------------ peer-review round-1 pins (DL-136)


def test_read_run_root_names_the_root_when_a_record_is_corrupt(tmp_path: Path) -> None:
    """DL-139 at the I/O shell: the shared reader's refusal reaches an operator as this
    module's own error, naming the root -- `dsl41 runs` reads several roots in one command.
    Restored, the same root folds the same rows."""
    from store_surgery import rows, update

    run_root = tmp_path / "run"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", run_root, ["j1"]
        )
    )
    clean = read_run_root(run_root)
    [(seq, saved)] = [(seq, body) for seq, kind, body in rows(run_root) if kind == "decision"][:1]
    update(run_root, seq, {**saved, "decision": "maybe"})
    with pytest.raises(RunHistoryError) as refused:
        read_run_root(run_root)
    assert str(run_root) in str(refused.value)
    update(run_root, seq, saved)
    assert read_run_root(run_root) == clean


def test_a_cascaded_inactive_closes_the_members_run_as_an_injected_one_does() -> None:
    """DL-242: `CHANGE_STATUS INACTIVE` on box B cascades to its running
    member J (SEM-18) with no STATUS input for J. The fold reads the
    trace's `RUNNING->INACTIVE` as it reads an injected INACTIVE: J's row
    agrees with the oracle row, closed at the cascade. A later box-start
    reset (`SUCCESS->INACTIVE`) does not reopen or rewrite a closed run."""
    from dsl41.oracle import Oracle

    catalog = lower_source(
        "insert_job: b\njob_type: b\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
    )
    cascade_at = T0 + timedelta(minutes=1)
    oracle = Oracle(catalog)
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "b"}))
    oracle.feed(Event(at=cascade_at, kind="STATUS", payload={"job": "b", "status": "INACTIVE"}))
    records = [_header(), _dispatch("j", 1, run_dir=None, started_at=T0)]
    [box_row, row] = fold_run_rows(records, catalog=catalog, trace=oracle.trace())
    assert row.status == oracle.store.job["j"].status == "INACTIVE"
    assert row.ended_at == cascade_at
    assert row.fidelity == "full"
    assert (box_row.job, box_row.status) == ("b", "INACTIVE")

    second = Oracle(catalog)
    second.feed(Event(at=T0, kind="STARTJOB", payload={"job": "b"}))
    second.feed(Event(at=cascade_at, kind="STATUS", payload={"job": "j", "status": "SUCCESS"}))
    second.feed(Event(at=T0 + timedelta(minutes=5), kind="STARTJOB", payload={"job": "b"}))
    records = [_header(), _dispatch("j", 1, run_dir=None, started_at=T0)]
    rows = fold_run_rows(records, catalog=catalog, trace=second.trace())
    [first_run] = [r for r in rows if r.job == "j" and r.run_number == 1]
    assert (first_run.status, first_run.ended_at) == ("SUCCESS", cascade_at)


def test_an_operator_status_after_an_undispatched_start_does_not_rewrite_the_earlier_run() -> None:
    """DL-242: run 2 of `j` starts and ends during its STARTING transition
    (a job_terminator cascade; here a KILLJOB at the same instant gives the
    fold the same records), and its SPAWN retires with no dispatch. A later
    operator `STATUS j FAILURE` belongs to run 2, the run the trace has
    open, not to run 1, the last DISPATCHED run: run 1 stays SUCCESS at
    08:01. The fold has no row for an undispatched run."""
    from dsl41.oracle import Oracle

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    oracle = Oracle(catalog)
    script = [
        (0, "STARTJOB", {"job": "j"}),
        (1, "STATUS", {"job": "j", "status": "SUCCESS"}),
        (2, "STARTJOB", {"job": "j"}),
        (2, "KILLJOB", {"job": "j"}),
        (4, "STATUS", {"job": "j", "status": "FAILURE"}),
    ]
    for minute, kind, payload in script:
        oracle.feed(Event(at=T0 + timedelta(minutes=minute), kind=kind, payload=payload))  # type: ignore[arg-type]
    records = [
        _header(),
        _dispatch("j", 1, run_dir=None, started_at=T0),
        _status_input(1, T0 + timedelta(minutes=1), job="j", run_number=1, exit_code=0),
        _status_input(
            2, T0 + timedelta(minutes=4), job="j", run_number=None, status="FAILURE", source=None
        ),
    ]
    [row] = fold_run_rows(records, catalog=catalog, trace=oracle.trace())
    assert (row.run_number, row.status) == (1, "SUCCESS")
    assert row.ended_at == T0 + timedelta(minutes=1)


def test_a_file_watch_run_gets_a_row_like_a_command() -> None:
    """Every CMD and FW start writes a `dispatch` record (runner-design ss7), so a watch's run
    folds into a row as a command's does, with its completion and the catalog's placement. A
    row names no executor: one engine runs every job on its own host."""
    catalog = lower_source("insert_job: w\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n")
    records = [
        _header(),
        _applied(1),
        _dispatch("w", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=3), job="w", run_number=1, exit_code=0),
        _applied(2),
    ]
    [row] = fold_run_rows(records, catalog=catalog)
    assert (row.job, row.run_number, row.status, row.duration_s) == ("w", 1, "SUCCESS", 180.0)
    assert "executor_id" not in RunRow.model_fields
