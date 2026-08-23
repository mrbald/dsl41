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
import shutil

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import runner_history
from dsl41.ast_jil import parse
from dsl41.boundary import stage_period
from dsl41.cli import app
from dsl41.ir import lower_catalog, lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner_adapters import LocalCommandAdapter
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
from dsl41.period import read_period_manifest, runtime_profile_from_cli, wal_path
from dsl41.runner_journal import read_journal, read_outbox
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
        "state_machine_version": 1,
        "clock_domain": "real",
        "first_index": 1,
        "opens_from_seal": None,
        "reclaimed": None,
        "trust_unaudited": None,
        "at": started_at.isoformat(),
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


def _spawn_decision(
    index: int, at: datetime, *, job: str, run_number: int, executor_id: str = "local"
) -> dict[str, Any]:
    """One decision and the SPAWN it planned (DL-118): an effect has no
    record of its own, so the fold reads it out of the decision that wanted
    it.

    The whole shape `Journal.decision` writes, birth identity included: a
    SPAWN carries the `run_id` minted in its own transaction and the host
    `generation` it was born on, and since DL-139 run history refuses one
    that does not, exactly as `read_outbox` always did."""
    return {
        "rec": "decision",
        "index": index,
        "request_id": f"r{index}",
        "decision": "applied",
        "reason": None,
        "revisions": {},
        "legacy_batch": False,
        "effects": [
            {
                "effect_id": f"e{index}:SPAWN:{job}.{run_number}",
                "kind": "SPAWN",
                "job": job,
                "run_number": run_number,
                "executor_id": executor_id,
                "index": index,
                "at": at.isoformat(),
                "run_id": _RID,
                "generation": 0,
            }
        ],
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


def test_executor_id_comes_from_the_spawn_effect_in_the_decision() -> None:
    records = [
        _header(),
        _spawn_decision(1, T0, job="j1", run_number=1, executor_id="local"),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records)
    assert row.executor_id == "local"


def _corrupt_spawn_decision(index: int, at: datetime, *, job: str, run_number: int) -> Any:
    """A well-formed decision with ONE identity field taken back out of its
    SPAWN. The lenient loop DL-139 replaced read `executor_id` off the raw
    dict with `str(...)`, so this record used to report the run as having
    run on a host called "None"."""
    record = _spawn_decision(index, at, job=job, run_number=run_number)
    del record["effects"][0]["executor_id"]
    return record


def test_a_decision_with_a_malformed_effect_refuses_rather_than_degrades() -> None:
    """DL-139: run history REFUSES a corrupt decision like `read_outbox`
    does. The row it would otherwise print names an executor no host ever
    had, and an operator-facing history that invents the answer is worse
    than one that stops."""
    corrupt = _corrupt_spawn_decision(1, T0, job="j1", run_number=1)
    records = [
        _header(),
        corrupt,
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    with pytest.raises(EngineError, match="malformed effect"):
        fold_run_rows(records)


def test_history_and_read_outbox_refuse_one_corrupt_decision_with_one_text() -> None:
    """One question, one answer (DL-139). "Is this decision's effect list
    valid" is decided at `runner_journal.decision_effects` for every reader
    of a decision, so the two history call sites and the outbox reader
    cannot disagree about a record -- nor about what they tell the
    operator.

    `_bound_run_ids` is named directly because it is the narrowest way to
    ask history's SECOND call site the same question: both read every
    `decision` in the log, so no public argument reaches one without the
    other."""
    corrupt = _corrupt_spawn_decision(1, T0, job="j1", run_number=1)
    records = [
        _header(),
        corrupt,
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    refusals = []
    for read in (
        lambda: fold_run_rows(records),
        lambda: runner_history._bound_run_ids(records),
        lambda: read_outbox(records),
    ):
        with pytest.raises(EngineError) as caught:
            read()
        refusals.append(str(caught.value))
    assert len(set(refusals)) == 1, refusals
    assert "decision at index 1: malformed effect" in refusals[0]


def test_a_well_formed_decision_still_folds_its_row_and_binds_its_run_id() -> None:
    """The control for the two refusals above: nothing about a log this
    estate actually wrote changes at DL-139."""
    records = [
        _header(),
        _spawn_decision(1, T0, job="j1", run_number=1, executor_id="local"),
        _dispatch("j1", 1, run_dir=None, started_at=T0),
        _status_input(2, T0 + timedelta(minutes=1), job="j1", run_number=1, exit_code=0),
    ]
    [row] = fold_run_rows(records)
    assert (row.job, row.run_number, row.status, row.executor_id) == ("j1", 1, "SUCCESS", "local")
    assert runner_history._bound_run_ids(records) == {("j1", 1): _RID}
    assert [effect.effect_id for effect in read_outbox(records).effects()] == ["e1:SPAWN:j1.1"]


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
    """Stage the period the way `dsl41 run` does (`boundary.stage_period`:
    the content-addressed input bundle), then start_run -- which installs
    `periods/000001/manifest.json` -- inject STARTJOB for every named job,
    run to quiescence, shut down and close the journal. The shared shape
    every read_run_root integration scenario below needs. Real subprocesses
    (`command: exit N`), the test_runner_adapters.py pattern."""
    jil = parse(text, file=file)
    catalog = lower_catalog([jil], permit_unknown=False)
    clock = RealClock()
    # the staged profile must BE the wiring below (the DL-130 gate refuses
    # a fiction): grace 2s, exactly what the adapter runs
    staged = stage_period(run_root, [jil], catalog, runtime_profile_from_cli(cmd_grace_s=2.0))
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
        staged=staged,
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


def test_read_run_root_degrades_rather_than_refusing_when_there_is_no_manifest(
    tmp_path: Path,
) -> None:
    """(DL-113 decision 5): stored inputs are DL-66 and then DL-130, so a
    run root predating both has none and retention prunes them -- exactly
    the old roots a history tool exists to read. The rows come back, and
    every one says `records_only` so nothing reads like a complete row."""
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

    asyncio.run(scenario())  # deliberately unstaged: no bundle, no manifest
    [row] = read_run_root(run_root)
    assert row.job == "j1"
    assert row.fidelity == "records_only"
    assert row.job_hash is None  # no catalog to fingerprint against
    assert row.box_name is None and row.started_by is None
    assert row.status == "SUCCESS"  # a plain exit 0 still reads correctly


def test_a_manifest_belonging_to_another_journal_still_refuses(tmp_path: Path) -> None:
    """(DL-113 decision 5): a MISSING manifest is a missing fact and degrades;
    a manifest whose catalog_hash disagrees with the segment is a WRONG one,
    and reading a run against another run's estate is the silent semantic
    drift runner-design ss7 refuses everywhere else."""
    run_root = tmp_path / "run"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", run_root, ["j1"]
        )
    )
    manifest = run_root / "periods" / "000001" / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["catalog_hash"] = "sha256:" + "0" * 64
    manifest.write_text(json.dumps(payload))
    # the full PR-22 agreement fires now (peer-review round: not one field)
    with pytest.raises(RunHistoryError, match="not this segment's|not this journal's"):
        read_run_root(run_root)


def test_d9_a_retired_manifest_layout_refuses_by_name(tmp_path: Path) -> None:
    """D9 (DL-138), discriminated ON THE FILE.

    A root holding `manifest/manifest.json` where `periods/<id>/manifest.json`
    is absent is DL-66's retired run-root layout and refuses BY NAME. A
    `manifest/` directory WITHOUT that file is unknown residue and refuses
    generically. The two are different states and an operator needs to be
    told which one is on the disk."""
    text = "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(text, run_root, ["j1"]))
    shutil.rmtree(run_root / "periods")
    (run_root / "manifest").mkdir()

    with pytest.raises(RunHistoryError, match="no manifest.json inside") as residue:
        read_run_root(run_root)
    assert "DL-138" not in str(residue.value)

    (run_root / "manifest" / "manifest.json").write_text(
        json.dumps({"catalog_hash": "h1", "sources": []})
    )
    with pytest.raises(RunHistoryError, match="RETIRED") as retired:
        read_run_root(run_root)
    assert "DL-138" in str(retired.value)


def test_pr50_run_history_spans_a_boundary(tmp_path: Path) -> None:
    """PR-50 (period-model ss13.8): history SPANS a boundary, and a run
    keeps the period that dispatched it and its own run number.

    A run root is one WAL until it seals and many segments afterwards
    (period-model ss1.1, I1). A reader that opened only the ACTIVE segment
    was right for as long as there was one, and became silently wrong at
    the first boundary: `dsl41 runs` answered with an EMPTY table, because
    every row it had ever printed lives in a closed period's segment. That
    is the defect DL-135 closed for the subscriber's backfill, in the other
    reader; DL-136 closed it here.

    The same job and the same box run in BOTH periods, which is what makes
    the second half of the property visible. Run numbers are monotone
    across the ESTATE (I2), so `b1` and `j1` must come back as runs 1 and
    2 -- a fold that replayed each segment from an empty oracle would
    number both periods from 1 and print one `(job, run_number)` twice. The
    leaf takes its number from the `dispatch` record and could never
    duplicate; the BOX has no dispatch record and takes its number from the
    replay, so the box rows are where the carry is proved. `started_by` on
    period 2's leaf row proves the other half of the same fix: the window
    is found by run NUMBER, not by position in a segment-local list.

    `j1`'s definition never changes, so both its rows carry one `job_hash`
    across a moved `catalog_hash` -- DL-113 decision 4's segmentation rule,
    now across periods as it already was across roots."""
    # the machine is DECLARED here and nowhere else in this file: the seal
    # runs preflight over C2 before it closes C1 (period-model ss8), and a
    # job on an undeclared machine is an ERROR there
    machine = "insert_machine: m1\ntype: a\nnode_name: localhost\n\n"
    estate = (
        "insert_job: b1\njob_type: b\n\n"
        "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\nbox_name: b1\n"
    )
    c1 = tmp_path / "c1.jil"
    c1.write_text(machine + estate)
    c2 = tmp_path / "c2.jil"
    c2.write_text(
        machine + estate + "\ninsert_job: j2\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    )
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(c1.read_text(), run_root, ["b1"], file=str(c1)))

    sealed = CliRunner().invoke(
        app, ["seal", "--run-root", str(run_root), "--next", str(c2)], catch_exceptions=False
    )
    assert sealed.exit_code == 0, sealed.output
    asyncio.run(_resume_real(c2, run_root, ["b1"]))

    rows = read_run_root(run_root)
    assert {row.job for row in rows} == {"b1", "j1"}  # period 1's runs did NOT disappear
    for job in ("b1", "j1"):
        series = sorted((row for row in rows if row.job == job), key=lambda r: r.started_at)
        assert [row.run_number for row in series] == [1, 2], (job, rows)
        assert all(row.status == "SUCCESS" for row in series), (job, series)
        assert len({row.job_hash for row in series}) == 1  # the definition never moved
        assert len({row.catalog_hash for row in series}) == 2  # the estate did
        assert all(row.started_by for row in series)  # the window was found by number
    first = read_period_manifest(run_root, 1)
    second = read_period_manifest(run_root, 2)
    assert first is not None and second is not None
    assert first.catalog_hash != second.catalog_hash
    by_period = {row.run_number: row.catalog_hash for row in rows if row.job == "j1"}
    assert by_period == {1: first.catalog_hash, 2: second.catalog_hash}
    assert [row for row in rows if row.job == "j1" and row.run_number == 1][
        0
    ].clock_source == "spool"


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
    staged = stage_period(run_root, [jil], catalog, runtime_profile_from_cli(cmd_grace_s=2.0))
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
        staged=staged,
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
    records = read_journal(wal_path(run_root, 1))
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


# ---------------------------------------- 5. the replay version gate


def test_replay_refuses_a_segment_naming_a_foreign_state_machine_version(
    tmp_path: Path,
) -> None:
    """period-model ss2.1: one executable implements one state machine, so a
    foreign binary "cannot lead OR replay". The LEAD half
    (`runner_ledger.check_leader_eligibility`) runs on resume only; without
    the replay half a v999 log replayed in silence and the trace narrated
    transitions this build's semantics invented."""
    foreign = dict(_header())
    foreign["state_machine_version"] = 999
    with pytest.raises(RunHistoryError, match="state_machine_version 999"):
        replay_trace(tmp_path, [foreign], lower_source(_SOLO_JIL))


def test_replay_accepts_the_state_machine_version_this_build_derives(tmp_path: Path) -> None:
    assert replay_trace(tmp_path, [_header()], lower_source(_SOLO_JIL)) == []


def test_dsl41_journal_refuses_a_foreign_state_machine_version(tmp_path: Path) -> None:
    """The same gate on the other replay door. `dsl41 journal` builds its
    catalog from the bundle the segment pins, so a version bump alone leaves
    every other check green and the trace would print.

    Only the SEGMENT is edited. `period.Sentinel` does not carry the version
    at all, so the sentinel cannot refuse on one; what guards the other
    doors is PR-22's manifest-vs-segment agreement, which a segment-only
    edit trips first. Editing the segment alone therefore isolates this
    gate on the one reader that has no other."""
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(_SOLO_JIL, run_root, ["j1"]))
    segment = wal_path(run_root, 1)
    lines = segment.read_text().splitlines()
    opening = json.loads(lines[0])
    opening["state_machine_version"] = 999
    lines[0] = json.dumps(opening, sort_keys=True)
    segment.write_text("\n".join(lines) + "\n")

    result = CliRunner().invoke(app, ["journal", str(segment)])
    assert result.exit_code == 2
    assert "state_machine_version 999" in result.output


# ------------------------------------------ peer-review round-1 pins (DL-136)


def _two_period_root(tmp_path: Path) -> Path:
    """The spanning fixture's estate, reduced: two sealed-and-resumed
    periods, real subprocesses, ready for chain/manifest/opening pins."""
    machine = "insert_machine: m1\ntype: a\nnode_name: localhost\n\n"
    estate = (
        "insert_job: b1\njob_type: b\n\n"
        "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\nbox_name: b1\n"
    )
    c1 = tmp_path / "c1.jil"
    c1.write_text(machine + estate)
    c2 = tmp_path / "c2.jil"
    c2.write_text(
        machine + estate + "\ninsert_job: j2\njob_type: c\ncommand: exit 0\nmachine: m1\n"
    )
    run_root = tmp_path / "run"
    asyncio.run(_run_real_and_manifest(c1.read_text(), run_root, ["b1"], file=str(c1)))
    sealed = CliRunner().invoke(
        app, ["seal", "--run-root", str(run_root), "--next", str(c2)], catch_exceptions=False
    )
    assert sealed.exit_code == 0, sealed.output
    asyncio.run(_resume_real(c2, run_root, ["b1"]))
    return run_root


def test_read_run_root_names_the_root_when_a_decision_is_corrupt(tmp_path: Path) -> None:
    """DL-139 at the I/O shell: the shared decoder's refusal reaches an
    operator as this module's own error, naming the root -- `dsl41 runs`
    reads several roots in one command, and "decision at index 5" alone
    does not say whose. Restored, the same root folds the same rows."""
    from dsl41.period import wal_path

    run_root = tmp_path / "run"
    asyncio.run(
        _run_real_and_manifest(
            "insert_job: j1\njob_type: c\ncommand: exit 0\nmachine: m1\n", run_root, ["j1"]
        )
    )
    clean = read_run_root(run_root)
    wal = wal_path(run_root, 1)
    saved = wal.read_text()
    lines = saved.splitlines()
    for position, line in enumerate(lines):
        record = json.loads(line)
        if record.get("rec") == "decision" and record.get("effects"):
            del record["effects"][0]["executor_id"]
            lines[position] = json.dumps(record)
            break
    else:  # pragma: no cover - the run above always plans a SPAWN
        raise AssertionError("the WAL holds no decision with an effect")
    wal.write_text("\n".join(lines) + "\n")
    with pytest.raises(RunHistoryError) as refused:
        read_run_root(run_root)
    assert str(run_root) in str(refused.value)
    assert "malformed effect" in str(refused.value)
    wal.write_text(saved)
    assert read_run_root(run_root) == clean


def test_history_refuses_a_spliced_or_holed_segment_chain(tmp_path: Path) -> None:
    """PR-50 through DL-135's chain proofs: history reads the estate
    through the same validated stream as the subscriber's backfill, so a
    foreign segment or a missing middle refuses instead of silently
    omitting a period or reporting a stranger's rows."""
    from dsl41.period import wal_path

    run_root = _two_period_root(tmp_path)
    (tmp_path / "other-base").mkdir()
    other = _two_period_root(tmp_path / "other-base")
    saved = wal_path(run_root, 1).read_bytes()
    shutil.copyfile(wal_path(other, 1), wal_path(run_root, 1))
    with pytest.raises(RunHistoryError, match="stranger's segment|does not open from"):
        read_run_root(run_root)
    wal_path(run_root, 1).write_bytes(saved)
    assert read_run_root(run_root)  # restored: clean
    # (a missing OLDEST segment is not a hole: it is DL-135's legitimate
    # pruned-history gap. The missing-MIDDLE refusal is pinned where the
    # shared reader lives: tests/test_retention.py's backfill chain pin.)


def test_history_refuses_a_replacement_manifest_sharing_catalog_hash(tmp_path: Path) -> None:
    """PR-22 at the reader: a self-consistent replacement manifest sharing
    catalog_hash but not baseline_id is foreign -- the FULL agreement
    check runs, not one field."""
    from dsl41.period import read_period_manifest, write_period_manifest

    run_root = _two_period_root(tmp_path)
    manifest = read_period_manifest(run_root, 1)
    assert manifest is not None
    import uuid as uuid_mod

    forged = manifest.model_copy(update={"baseline_id": str(uuid_mod.uuid4())})
    write_period_manifest(run_root, forged)
    with pytest.raises(RunHistoryError, match="disagrees with the journal's segment record"):
        read_run_root(run_root)


def test_history_refuses_an_opening_it_cannot_prove(tmp_path: Path) -> None:
    """ss11/PR-50: a period whose opening NAMES a seal and whose sidecar
    is gone must refuse -- swallowing it would replay period 2 from an
    empty state and return full-fidelity history from an unproved
    opening."""
    from dsl41.period import seal_path as _seal_path

    run_root = _two_period_root(tmp_path)
    _seal_path(run_root, 1).unlink()
    with pytest.raises(RunHistoryError, match="unproved opening"):
        read_run_root(run_root)
    # the SAME proof runs before the manifest-degradation branch too: with
    # BOTH artifacts gone, `records_only` rows are still refused
    manifest = run_root / "periods" / "000002" / "manifest.json"
    manifest.unlink()
    with pytest.raises(RunHistoryError, match="unproved opening"):
        read_run_root(run_root)


def test_history_refuses_an_identity_grafted_sidecar(tmp_path: Path) -> None:
    """ss11: the digest binds the file's bytes, not its place in this
    lineage -- a valid sidecar from ANOTHER estate, with the link
    rewritten to its digest, must refuse even on the records_only path."""
    from dsl41.canon import canonical_bytes
    from dsl41.period import seal_path as _seal_path
    from dsl41.period import wal_path as _wal_path
    from dsl41.runner_journal import read_journal

    run_root = _two_period_root(tmp_path)
    (tmp_path / "other-base").mkdir()
    other = _two_period_root(tmp_path / "other-base")
    donor = _seal_path(other, 1).read_bytes()
    _seal_path(run_root, 1).write_bytes(donor)
    import json as json_mod

    donor_digest = json_mod.loads(donor)["digest"]
    records = read_journal(_wal_path(run_root, 2))
    link = dict(records[0]["opens_from_seal"])
    link["digest"] = donor_digest
    records[0] = {**records[0], "opens_from_seal": link}
    _wal_path(run_root, 2).write_bytes(b"".join(canonical_bytes(r) + b"\n" for r in records))
    # the manifest goes too: with it present, open_from_seal would reject
    # the donor anyway and the pin would stay green without _prove_opening
    # -- the records_only path is exactly what this pin exists to hold.
    # And WAL 1 goes: on a two-segment root the chain check refuses first;
    # the rolled/pruned shape (successor segment only) is where the
    # opening proof is the ONLY guard
    (run_root / "periods" / "000002" / "manifest.json").unlink()
    _wal_path(run_root, 1).unlink()
    with pytest.raises(RunHistoryError, match="identity graft|unproved opening"):
        read_run_root(run_root)


def test_the_opening_proof_covers_every_shared_field(tmp_path: Path) -> None:
    """ss11/PR-50, coverage pinned: the projection is derived from the
    model-field/segment-field intersection, and every member refuses when
    the OPENING's copy is forged -- on the rolled/pruned shape where the
    opening proof is the only guard. (catalog_hash_version is exempt in
    the loop: check_segment_record refuses its forgery before the proof
    can, which is coverage by an earlier gate, and `at` is asserted
    separately.)"""
    from dsl41.canon import canonical_bytes
    from dsl41.period import SEGMENT_FIELDS
    from dsl41.period import wal_path as _wal_path
    from dsl41.runner_journal import read_journal
    from dsl41.seal import CommittedNextPeriod

    shared = sorted(set(CommittedNextPeriod.model_fields) & SEGMENT_FIELDS)
    assert "state_machine_version" in shared and "catalog_hash" in shared  # the derivation lives
    forged_value: dict[str, Any] = {
        "baseline_id": "11111111-1111-4111-8111-111111111111",
        "catalog_hash": "sha256:" + "3" * 64,
        "clock_domain": "virtual",  # the fixture runs real
        "first_index": 999,
        "period_id": 7,
        "runtime_hash": "sha256:" + "4" * 64,
        "segment_no": 7,
        "source_bundle_hash": "sha256:" + "5" * 64,
        "state_machine_version": 99,
    }
    run_root = _two_period_root(tmp_path)
    (run_root / "periods" / "000002" / "manifest.json").unlink()
    _wal_path(run_root, 1).unlink()  # the single-segment shape
    original = _wal_path(run_root, 2).read_bytes()
    baseline_rows = read_run_root(run_root)  # the honest root still folds
    assert all(row.fidelity == "records_only" for row in baseline_rows)
    for name in shared:
        if name == "catalog_hash_version":
            continue  # forged at read_journal's own gate, not this one
        records = read_journal(_wal_path(run_root, 2))
        records[0] = {**records[0], name: forged_value[name]}
        _wal_path(run_root, 2).write_bytes(b"".join(canonical_bytes(r) + b"\n" for r in records))
        # "could not have admitted" is round-3's seq-range gate refusing a
        # forged first_index BEFORE the proof -- coverage by an earlier
        # gate is coverage
        with pytest.raises(
            RunHistoryError,
            match="identity graft|unproved opening|could not have admitted|one period, one segment|stranger's segment",
        ):
            read_run_root(run_root)
        _wal_path(run_root, 2).write_bytes(original)
    # `at` and `estate_id` are the non-projection halves of the same proof
    for name, wrong, accept in (
        ("at", "2031-01-01T00:00:00", "identity graft|unproved opening"),
        # the sentinel binding (DL-135's gate) refuses a forged estate
        # before the proof -- coverage by an earlier gate is coverage
        ("estate_id", "e-stranger", "identity graft|stranger's segment"),
    ):
        records = read_journal(_wal_path(run_root, 2))
        records[0] = {**records[0], name: wrong}
        _wal_path(run_root, 2).write_bytes(b"".join(canonical_bytes(r) + b"\n" for r in records))
        with pytest.raises(RunHistoryError, match=accept):
            read_run_root(run_root)
        _wal_path(run_root, 2).write_bytes(original)
