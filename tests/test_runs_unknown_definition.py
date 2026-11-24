"""A run whose opening records no configuration rev costs one row (DL-555, `runner_history`
decision 7). The row says its definition is not known and why, and carries no hash, no box and
no verdict from a bare exit code. A rev that is recorded and has no configuration row still
refuses the read.

Every check is deterministic: pure folds over plain records, or an engine on a virtual clock.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import closing
import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from retention import FAR, prune
from test_history_verb import _ATTEMPT, _read
from test_run_history import _applied, _dispatch, _status_input
from test_run_openings import _LEAF, T0, _at, _ev, _Root, _scenario
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)
from typer.testing import CliRunner

from dsl41 import runner, runner_history
from dsl41.cli import app
from dsl41.cli_run import _runs_table
from dsl41.ir import lower_source
from dsl41.oracle_state import JobRuntime, TraceEntry
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_history import (
    RunHistoryError,
    RunRow,
    definition_change,
    fold_run_rows,
    read_run_root,
)
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_runs import Declined
from dsl41.runner_startup import start_run
from dsl41.runner_store import Record, RecordOwner, RunOpening, SqliteStore, Store

_CATALOG = lower_source(_LEAF.format(cmd="x"))
_BOXED = lower_source(
    "insert_job: b\njob_type: b\n\ninsert_job: m\njob_type: c\ncommand: x\nbox_name: b\n"
)


def _segment(rev: int = 1) -> dict[str, Any]:
    return {"rec": "segment", "rev": rev, "catalog_hash": "h1"}


def _window(run: int, rev: int, status: str = "SUCCESS") -> dict[str, Any]:
    """A snapshot's retained run window, as `run_history` writes it."""
    return {
        "run": run,
        "started_at": T0.isoformat(),
        "started_by": f"start {run}",
        "ended_at": _at(1).isoformat(),
        "status": status,
        "approximate": False,
        "rev": rev,
    }


def _anchor(run: int, status: str = "RUNNING") -> JobRuntime:
    """A snapshot's job row whose run the history kept no window of: an anchor row's run,
    which gets a window with no rev (decision 6)."""
    return JobRuntime(status=status, status_at=_at(2), run_number=run, started_by="start")  # type: ignore[arg-type]


def _ended(job: str, run: int, **payload: Any) -> list[dict[str, Any]]:
    """The dispatch of `job.run`, then its STATUS input and the decision that applied it."""
    return [
        _dispatch(job, run, run_dir=f"/r/{job}.{run}", started_at=_at(2)),
        _status_input(1, _at(3), job=job, run_number=run, **payload),
        _applied(1),
    ]


def _unknown(job: str, run: int, **payload: Any) -> RunRow:
    """The one row of `job.run`, a run whose opening records no rev."""
    rows = fold_run_rows(
        [_segment(), *_ended(job, run, **payload)],
        catalog=_CATALOG,
        anchor={job: _anchor(run)},
        catalogs={1: _CATALOG},
    )
    [row] = rows
    return row


# ------------------------------------------------------------------ rule 1


def test_an_absent_opening_rev_marks_the_row_and_keeps_its_facts() -> None:
    row = _unknown("j", 4, exit_code=0)
    assert row.definition_unknown == "no_opening_rev"
    assert (row.catalog_hash, row.job_hash, row.box_name) == (None, None, None)
    assert (row.job, row.run_number, row.run_dir) == ("j", 4, "/r/j.4")
    assert (row.started_at, row.ended_at, row.duration_s) == (_at(2), _at(3), 60.0)
    assert (row.started_by, row.clock_source, row.fidelity) == ("start", "journal", "partial")


def test_a_recorded_rev_with_no_configuration_row_still_refuses() -> None:
    """A rev the window records and the store holds no configuration for is a broken
    reference, not an absent fact."""
    with pytest.raises(RunHistoryError, match="run j.1 records configuration rev 7"):
        fold_run_rows(
            [_segment(), *_ended("j", 1, exit_code=0)],
            catalog=_CATALOG,
            anchor={"j": _anchor(1)},
            history={"j": [_window(1, 7)]},
            catalogs={1: _CATALOG},
        )


def test_runs_refuses_a_root_whose_retained_run_names_a_rev_with_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    """The snapshot's retained run `j.1` names rev 7, which the store holds no row for; the
    head is pruned, so the read starts there. `dsl41 runs` exits 2."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))
        real = runner.run_history

        def named(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
            return {
                job: [{**window, "rev": 7} for window in windows]
                for job, windows in real(*args, **kwargs).items()
            }

        with monkeypatch.context() as patched:
            patched.setattr(runner, "run_history", named)
            root.engine.take_snapshot()
        assert root.engine.journal is not None
        store = root.engine.journal.store
        prune(store, _first_dispatch(store), FAR)

    root, _ = _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    result = CliRunner().invoke(app, ["runs", str(root.run)])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "configuration rev 7" in result.stderr


# ------------------------------------------------------------------ rule 2


def test_a_bare_exit_code_gets_no_verdict_without_the_definition() -> None:
    row = _unknown("j", 4, exit_code=3)
    assert (row.status, row.exit_code) == (None, 3)


def test_an_explicit_applied_status_is_shown_without_the_definition() -> None:
    row = _unknown("j", 4, exit_code=3, status="TERMINATED")
    assert (row.status, row.exit_code) == ("TERMINATED", 3)


def _marked(
    job: str,
    records: list[dict[str, Any]],
    anchor: JobRuntime,
    trace: list[TraceEntry] = [],  # noqa: B006 -- read only
    catalogs: dict[int, Any] | None = None,
) -> RunRow:
    """The one row of a fold whose run has only the anchor row's window, with no rev."""
    [row] = fold_run_rows(
        [_segment(), *records],
        catalog=_BOXED,
        trace=trace,
        anchor={job: anchor},
        catalogs={1: _BOXED} if catalogs is None else catalogs,
    )
    assert row.definition_unknown == "no_opening_rev"
    return row


def _closed(at: datetime, job: str, transition: str) -> TraceEntry:
    return TraceEntry(at=at, job=job, transition=transition, cause="c")


def test_a_trace_close_is_shown_where_the_exit_code_is_bare() -> None:
    """The run's close in the trace after the snapshot is its recorded end."""
    row = _marked(
        "m",
        _ended("m", 4, exit_code=3),
        _anchor(4),
        [_closed(_at(3), "m", "RUNNING->SUCCESS")],
    )
    assert (row.status, row.exit_code, row.ended_at) == ("SUCCESS", 3, _at(3))


def test_a_member_its_box_reset_after_its_run_has_no_status_not_inactive() -> None:
    """Astra's first sequence: `m.1` exits 3 (SUCCESS under a `max_exit_success: 5` the read
    cannot see); its box restarts and resets it to INACTIVE with no new run; the snapshot's
    row says INACTIVE. That is the row's current status, not run 1's end: no status, the raw
    code and the completion's end."""
    reset = JobRuntime(
        status="INACTIVE", status_at=_at(10), last_end_at=_at(3), run_number=1, started_by="s"
    )
    row = _marked("m", _ended("m", 1, exit_code=3), reset)
    assert (row.status, row.exit_code, row.ended_at) == (None, 3, _at(3))
    bare = _marked("m", [_dispatch("m", 1, run_dir="/r/m.1", started_at=_at(2))], reset)
    assert (bare.status, bare.ended_at) == (None, None)


def test_a_later_trace_close_wins_over_an_earlier_explicit_status() -> None:
    """Astra's second sequence: an explicit RUNNING at 08:03, a KILLJOB close at 08:05."""
    row = _marked(
        "m",
        _ended("m", 4, status="RUNNING"),
        _anchor(4),
        [_closed(_at(5), "m", "RUNNING->TERMINATED")],
    )
    assert (row.status, row.ended_at) == ("TERMINATED", _at(5))


def test_a_later_explicit_status_wins_over_an_earlier_trace_close() -> None:
    row = _marked(
        "m",
        _ended("m", 4, status="TERMINATED", exit_code=3),
        _anchor(4),
        [_closed(_at(2.5), "m", "RUNNING->SUCCESS")],
    )
    assert (row.status, row.exit_code, row.ended_at) == ("TERMINATED", 3, _at(3))


def _recovered(ended_at: datetime | None) -> RunRow:
    """`m.4` dispatched at 08:02; at 08:30 a restart's recovery records its FAILURE, with or
    without the process end status.json gave, and the trace closes the run then."""
    return _marked(
        "m",
        [
            _dispatch("m", 4, run_dir="/r/m.4", started_at=_at(2)),
            _status_input(
                1,
                _at(30),
                job="m",
                run_number=4,
                source="reconcile",
                status="FAILURE",
                ended_at=ended_at,
            ),
            _applied(1),
        ],
        _anchor(4),
        [_closed(_at(30), "m", "RUNNING->FAILURE")],
    )


def test_a_recovery_with_no_process_end_leaves_the_end_unknown() -> None:
    """Astra's first case: the recovery's own time is when the engine noticed, not when the
    process ended, so the trace close at that time gives the verdict and no end."""
    row = _recovered(None)
    assert (row.status, row.ended_at, row.duration_s) == ("FAILURE", None, None)


def test_a_recovery_s_recorded_process_end_is_the_end() -> None:
    row = _recovered(_at(10))
    assert (row.status, row.ended_at, row.duration_s) == ("FAILURE", _at(10), 480.0)


def test_a_marked_run_that_ended_before_the_snapshot_keeps_unknown_and_its_reason() -> None:
    row = _marked(
        "m", [_dispatch("m", 4, run_dir="/r/m.4", started_at=_at(2))], _anchor(5, "SUCCESS")
    )
    assert row.status == "UNKNOWN"


def test_an_empty_configuration_map_is_not_the_single_catalog_fold() -> None:
    """With configuration rows, even none, a run is read only under the rev it records."""
    row = _marked("m", _ended("m", 4, exit_code=0), _anchor(4), catalogs={})
    assert (row.catalog_hash, row.status) == (None, None)


def test_a_known_definition_still_reads_a_bare_exit_code_by_sem09() -> None:
    [row] = fold_run_rows(
        [_segment(), *_ended("j", 4, exit_code=3)],
        catalog=_CATALOG,
        anchor={"j": _anchor(4)},
        history={"j": [_window(4, 1, status="RUNNING")]},
        catalogs={1: _CATALOG},
    )
    assert (row.status, row.definition_unknown) == ("FAILURE", None)
    assert row.catalog_hash is not None and row.job_hash is not None


# ------------------------------------------------------------------ rule 4


def _row(run: int, *, job_hash: str | None, catalog_hash: str | None = "c" * 64) -> RunRow:
    unknown = catalog_hash is None
    return RunRow(
        job="j",
        run_number=run,
        catalog_hash=catalog_hash,
        started_at=_at(run),
        status=None if unknown else "SUCCESS",
        clock_source="journal",
        job_hash=job_hash,
        definition_unknown="no_opening_rev" if unknown else None,
    )


def test_no_change_is_marked_beside_an_unknown_row() -> None:
    """A records-only row (no fingerprint) beside an unknown row would fall back to the
    catalog hash and read None as a change."""
    known, unknown = _row(1, job_hash=None), _row(2, job_hash=None, catalog_hash=None)
    assert definition_change(known, unknown) is None
    assert definition_change(unknown, known) is None
    lines = _runs_table([known, unknown, _row(3, job_hash=None)])
    assert not any(line.startswith("  --") for line in lines)


def test_a_change_across_an_unknown_row_is_marked_after_it() -> None:
    rows = [
        _row(1, job_hash="a" * 64),
        _row(2, job_hash=None, catalog_hash=None),
        _row(3, job_hash="b" * 64),
    ]
    lines = _runs_table(rows)
    assert lines[3] == f"  -- j: definition changed {'a' * 10} -> {'b' * 10} --"
    assert lines[2].split()[1:] == ["2", _at(2).isoformat(), "-", "?", "journal", "?", "?"]
    assert [line.split()[1] for line in (lines[1], lines[4])] == ["1", "3"]


# ------------------------------------------------------------------ rule 5


def test_a_box_with_no_known_definition_has_no_row_and_its_member_has_its_own() -> None:
    """Box `b` and member `m` ran as run 1 under rev 1; the snapshot's rows are at run 2 with
    no window of it (the history an older build wrote). `b.2` cannot be told from an
    undispatched leaf, so it has no row; `m.2` is its own row, marked, with no box."""
    trace = [
        TraceEntry(at=_at(3), job="m", transition="RUNNING->SUCCESS", cause="exit 0"),
        TraceEntry(at=_at(3), job="b", transition="RUNNING->SUCCESS", cause="members done"),
    ]
    rows = fold_run_rows(
        [_segment(), *_ended("m", 2, exit_code=0)],
        catalog=_BOXED,
        trace=trace,
        anchor={"b": _anchor(2), "m": _anchor(2)},
        history={"b": [_window(1, 1)], "m": [_window(1, 1)]},
        catalogs={1: _BOXED},
    )
    by_run = {(row.job, row.run_number): row for row in rows}
    assert sorted(by_run) == [("b", 1), ("m", 2)]
    box, member = by_run[("b", 1)], by_run[("m", 2)]
    assert box.definition_unknown is None and box.catalog_hash is not None
    assert (member.definition_unknown, member.box_name, member.status) == (
        "no_opening_rev",
        None,
        "SUCCESS",
    )


# ------------------------------------------------------- BUG-108's root, through `runs`


def _first_dispatch(store: Store) -> int:
    return next(seq for seq, record in store.read_from(0) if record.kind == "dispatch")


def _bug108_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Root:
    """`j` runs (1), bypasses under ON_NOEXEC (2), and runs again (3). A snapshot taken while
    run 3 runs holds the history the build before DL-537 wrote: the bypass opened no window
    and run 3, the second STARTING it counted, took number 2. Run 3 ends; the head is pruned
    up to run 1's dispatch, so the read starts at the snapshot and run 3's window is the
    anchor row's, with no rev."""

    def old_history(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        runs = real(*args, **kwargs)
        kept = [window for window in runs["j"] if window["run"] != 2]
        return {**runs, "j": [{**window, "run": n} for n, window in enumerate(kept, 1)]}

    real = runner.run_history

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(2, "ON_NOEXEC", job="j"))
        await root.feed(_ev(3, "STARTJOB", job="j"), _ev(4, "OFF_NOEXEC", job="j"))
        await root.feed(_ev(5, "STARTJOB", job="j"))
        with monkeypatch.context() as patched:
            patched.setattr(runner, "run_history", old_history)
            root.engine.take_snapshot()
        await root.engine.run_until_quiescent(_at(30))
        assert root.engine.journal is not None
        store = root.engine.journal.store
        prune(store, _first_dispatch(store), FAR)

    root, _ = _scenario(
        tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 0), ("j", 3): (600.0, 0)}
    )
    return root


def test_bug108_runs_reads_the_root_in_full_as_history_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    """Run 3 opened after the index began, so its window comes from the index's opening and
    the stored transitions: its rev, its cause and its close. Run 1 opened before the pruned
    head and keeps the snapshot's window. Run 3 equals the `history` row but for `fidelity`:
    it began before the snapshot."""
    root = _bug108_root(tmp_path, monkeypatch)
    first, third = read_run_root(root.run)
    assert (first.run_number, first.status, first.exit_code) == (1, "SUCCESS", 0)
    assert (first.catalog_hash, first.definition_unknown) == (root.hashes[1], None)
    assert (third.run_number, third.status, third.exit_code) == (3, "SUCCESS", 0)
    assert (third.catalog_hash, third.definition_unknown) == (root.hashes[1], None)
    assert third.started_by is not None and third.started_by.startswith("STARTJOB")
    assert (third.fidelity, third.ended_at) == ("partial", _at(15))
    with closing(SqliteStore(root.run / STORE_NAME, read_only=True)) as store:
        [shown] = [
            RunRow.model_validate(run["row"])
            for run in _read(store, "j")["runs"]
            if run["row"]["run_number"] == 3
        ]
    assert shown.fidelity == "full"
    assert shown == third.model_copy(update={"fidelity": "full"})
    result = CliRunner().invoke(app, ["runs", str(root.run), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "definition_unknown=" not in result.stderr
    assert [row["run_number"] for row in json.loads(result.stdout)] == [1, 3]


# ------------------------------------------------------- the `history` wire is unchanged


def _run_two(first: dict[str, Any]) -> dict[str, Any]:
    """Dispatch `first` of run 1 as the first attempt of run 2, its identity rewritten."""
    attempt = _ATTEMPT.format(2)
    tag = json.loads(base64.urlsafe_b64decode(first["env"]["DSL41_RUN"]))
    run_tag = json.dumps({**tag, "run_id": attempt, "run_number": 2}).encode()
    env = {
        **first["env"],
        "DSL41_RUN_NUMBER": "2",
        "DSL41_RUN": base64.urlsafe_b64encode(run_tag).decode(),
    }
    return {**first, "run_number": 2, "attempt": attempt, "env": env}


#: the fields of the `history` answer's `row`, as on main before DL-555
_HISTORY_ROW = {
    "job",
    "run_number",
    "catalog_hash",
    "started_at",
    "ended_at",
    "duration_s",
    "status",
    "exit_code",
    "started_by",
    "run_dir",
    "box_name",
    "clock_source",
    "job_hash",
    "fidelity",
    "undecided",
}


def _one_run() -> Store:
    async def scenario() -> Store:
        engine = start_run(
            _CATALOG,
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(1))
        await engine.shutdown()
        assert engine.journal is not None
        return engine.journal.store

    return asyncio.run(scenario())


def test_the_history_row_has_the_fields_it_has_on_main() -> None:
    [run] = _read(_one_run(), "j")["runs"]
    assert set(run["row"]) == _HISTORY_ROW
    assert isinstance(run["row"]["catalog_hash"], str) and run["row"]["status"] == "RUNNING"


def test_history_declines_a_run_its_page_reads_with_no_opening_rev() -> None:
    """Reached only by a hand-built store: the index names an opening of `j.2` under rev 1,
    whose stored start entry carries no run number, so the page's fold sees no window for its
    dispatch. The evidence disagrees: the query declines for the job, as for a run number
    shown twice. A page below it still reads."""
    store = _one_run()
    first = next(record for _, record in store.read_from(0) if record.kind == "dispatch")
    at = datetime(2026, 7, 1, 8, 2)
    store.append(
        Record(at, "engine", None, None, {"event": "x"}),
        transitions=[
            TraceEntry(at=at, job="j", transition="RUNNING->STARTING", cause="c").model_dump()
        ],
        openings=[RunOpening(0, "j", 2, 1)],
    )
    store.append(
        Record(at, "dispatch", None, None, _run_two(first.payload)),
        owners=[RecordOwner(0, "j", 2)],
    )
    with pytest.raises(Declined, match="run 2 of 'j' has no opening entry"):
        _read(store, "j")
    [below] = _read(store, "j", cursor=2)["runs"]
    assert set(below["row"]) == _HISTORY_ROW and below["row"]["run_number"] == 1


# ------------------------------------------------------------------ `runs` output


def test_runs_shows_a_marked_null_status_as_null_blank_and_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    """A marked row with no status: JSON null, a blank CSV cell, `?` in the table; the stderr
    line in every format."""
    row = RunRow(
        job="j",
        run_number=4,
        catalog_hash=None,
        started_at=_at(2),
        status=None,
        exit_code=3,
        clock_source="journal",
        definition_unknown="no_opening_rev",
    )
    monkeypatch.setattr(runner_history, "read_run_roots", lambda roots, **_: [row])
    shown = {}
    for output_format in ("table", "json", "csv"):
        result = CliRunner().invoke(app, ["runs", str(tmp_path), "--format", output_format])
        assert result.exit_code == 0, result.output
        assert len(result.stderr.splitlines()) == 1
        shown[output_format] = result.stdout
    [as_json] = json.loads(shown["json"])
    assert as_json["status"] is None and as_json["exit_code"] == 3
    [as_csv] = csv.DictReader(io.StringIO(shown["csv"]))
    assert (as_csv["status"], as_csv["exit_code"], as_csv["catalog_hash"]) == ("", "3", "")
    [line] = [line for line in shown["table"].splitlines() if line.startswith("j ")]
    assert line.split()[3:] == ["-", "?", "journal", "?", "?"]


def test_runs_refuses_a_root_whose_first_start_wrote_no_configuration(
    tmp_path: Path, cli_env: None
) -> None:
    """A genesis interrupted after the store's schema and before its first configuration: an
    unreadable root, one line and exit 2."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    identity = {"state_version": str(STATE_MACHINE_VERSION), "baseline_id": "b", "root_id": "r"}
    SqliteStore(run_root / STORE_NAME, created_at=T0, identity=identity).close()
    result = CliRunner().invoke(app, ["runs", str(run_root)])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    [line] = result.stderr.splitlines()
    assert "the store holds no configuration" in line
