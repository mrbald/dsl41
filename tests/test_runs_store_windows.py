"""`dsl41 runs` takes a run's window where the store records it (DL-555, `runner_history`
decision 7). A run whose opening the history index holds in the stretch it covers up to the
store's last seq gets its window from the index's opening and the stored transitions, as the
`history` query does; any other run keeps the window of the replay and the snapshot. The two
readers then give equal rows on an engine-written root, except where `runner_runs` says.

Every check is deterministic: engines on a virtual clock, or real commands whose spool is
compared field by field.
"""

from __future__ import annotations

from contextlib import closing

import asyncio
import base64
import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import dispatch_record
from test_engine_stops_cookie import OTHER_ID
from test_history_verb import _ATTEMPT, _SCENARIOS, _pages
from test_run_history import _run_real_and_manifest
from retention import FAR, prune
from test_run_openings import T0, _at, _ev, _Root, _scenario
from test_transitions import _as_previous_build
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)
from typer.testing import CliRunner

from dsl41 import runner, runner_history, runner_runs
from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import Configuration, EngineSettings
from dsl41.runner_history import (
    RunHistoryError,
    RunRow,
    fold_run_rows,
    read_run_root,
    read_run_roots,
)
from dsl41.runner_journal import STORE_NAME, wire_record
from dsl41.runner_runs import Declined
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import (
    Interval,
    Record,
    RecordOwner,
    RunEvidence,
    RunOpening,
    SqliteStore,
    Store,
    TransitionQuery,
)

_PINNED = (
    "insert_job: p\njob_type: c\ncommand: x\n\n"
    "insert_job: t\njob_type: c\ncommand: x\n\n"
    "insert_job: j\njob_type: c\ncommand: x\n\n"
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nbox_name: b\n"
)

_MIXED = (
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nbox_name: b\n\n"
    "insert_job: k\njob_type: c\ncommand: x\n"
)

Key = tuple[str, int]


def _offline(root: Path) -> dict[Key, RunRow]:
    """`read_run_root`'s rows by key, each key once."""
    rows = read_run_root(root)
    keys = [(row.job, row.run_number) for row in rows]
    assert len(keys) == len(set(keys)), keys
    return dict(zip(keys, rows, strict=True))


def _replay_only(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[Key, RunRow]:
    """The rows the replay and the snapshot alone give: the store's windows left out."""
    with monkeypatch.context() as patched:
        patched.setattr(
            runner_history,
            "_stored_runs",
            lambda evidence, anchor_runs: runner_history._StoredRuns({}, {}),
        )
        return _offline(root)


def _history(root: Path, job: str, limit: int = 20) -> dict[int, RunRow]:
    """The `history` query's rows of `job`, every page joined."""
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        pages = _pages(store, job, limit)
    rows = [RunRow.model_validate(run["row"]) for page in pages for run in page["runs"]]
    return {row.run_number: row for row in rows}


def _equal_but_listed(offline: RunRow, shown: RunRow) -> None:
    """The `history` row equals the offline row but for the differences `runner_runs` lists:
    the spool's times and `clock_source`, and `fidelity` for a run that began before the
    snapshot. The page-boundary STATUS has its own test."""
    assert shown.clock_source == "journal" and shown.fidelity == "full"
    expected = offline.model_copy(update={"fidelity": "full"})
    if offline.clock_source == "spool":
        expected = expected.model_copy(
            update={
                "started_at": shown.started_at,
                "ended_at": shown.ended_at,
                "duration_s": shown.duration_s,
                "clock_source": "journal",
            }
        )
    assert shown == expected


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


# ------------------------------------------------- an older open attempt pins the prune


def _pinned(tmp_path: Path) -> Path:
    """`j` runs once; `p` starts and stays open, pinning the prune; `j`, and box `b` with its
    member `m`, run twice more; three days pass and a snapshot with one day's retention
    prunes up to `p`'s attempt. The snapshot's history keeps no window of the finished runs."""
    root = tmp_path / "run"

    async def run() -> None:
        engine = start_run(
            lower_source(_PINNED),
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("p", 1): None}, default=(60.0, 0))},
            settings=EngineSettings(retention_days=1, snapshot_interval_s=10**6),
        )
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(1.5))
        engine.inject(_ev(1.5, "STARTJOB", job="p"))
        await engine.run_until_quiescent(_at(1.8))
        for minutes in (2, 10):
            engine.inject(_ev(minutes, "STARTJOB", job="j"))
            engine.inject(_ev(minutes, "STARTJOB", job="b"))
            await engine.run_until_quiescent(_at(minutes + 5))
        later = T0 + timedelta(days=3)
        engine.inject(Event(at=later, kind="STARTJOB", payload={"job": "t"}))
        await engine.run_until_quiescent(later + timedelta(minutes=5))
        engine.take_snapshot()
        await _close(engine)

    asyncio.run(run())
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        assert store.first_seq() > 1  # the head is pruned
    return root


def test_a_pinned_prune_reads_every_indexed_run_as_history_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _pinned(tmp_path)
    offline = _offline(root)
    assert sorted(offline) == [
        ("b", 1),
        ("b", 2),
        ("j", 2),
        ("j", 3),
        ("m", 1),
        ("m", 2),
        ("p", 1),
        ("t", 1),
    ]
    assert all(row.definition_unknown is None for row in offline.values())
    for job in ("b", "j", "m", "t"):
        for limit in (1, 20):
            shown = _history(root, job, limit)
            assert set(shown) == {run for name, run in offline if name == job}, (job, limit)
            for run, row in shown.items():
                _equal_but_listed(offline[(job, run)], row)
    # the replay alone marks the leaves and loses both box runs; nothing it gives is lost
    alone = _replay_only(root, monkeypatch)
    assert {key for key, row in alone.items() if row.definition_unknown} == {
        ("j", 2),
        ("j", 3),
        ("m", 1),
        ("m", 2),
    }
    assert set(alone) <= set(offline) and ("b", 1) not in alone
    assert offline[("p", 1)] == alone[("p", 1)]  # opened below the index: the replay's window


def test_the_fidelity_difference_is_only_a_run_that_began_before_the_snapshot(
    tmp_path: Path,
) -> None:
    root = _pinned(tmp_path)
    offline = _offline(root)
    shown = {(job, run): row for job in "bjmt" for run, row in _history(root, job).items()}
    differ = {key for key, row in shown.items() if offline[key].fidelity != row.fidelity}
    assert differ == {key for key in shown if offline[key].fidelity == "partial"}
    assert ("j", 2) in differ and ("b", 1) in differ


def test_the_offline_read_takes_no_page_or_byte_budget_of_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _pinned(tmp_path)
    before = _offline(root)
    monkeypatch.setattr(runner_runs, "BUDGET", 1)
    monkeypatch.setattr(runner_runs, "LIMIT_DEFAULT", 1)
    monkeypatch.setattr(runner_runs, "LIMIT_MAX", 1)
    monkeypatch.setattr(runner_runs, "REPLY_BYTES", 1)
    assert _offline(root) == before


# ------------------------------------------------------------------ mixed coverage


def _mixed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pruned: bool = False
) -> tuple[Path, int]:
    """Three stretches. This build: box `b` opens with its member `m` running, and `k.1` runs.
    The build before the transitions table (no rows, no index): KILLJOB ends `m.1`, which
    closes `b.1` too, both in the trace alone, and `k.2` opens. This build again: KILLJOB ends
    `k.2`, `k` bypasses under ON_NOEXEC (3) and runs (4), and a dispatch of `k.9`, whose run
    never opened, is appended. With `pruned`, a snapshot whose history keeps no window of
    `k.1` is taken and the first record pruned, so the read replays from the snapshot.
    Answers the root and the first seq of the last stretch."""
    root = tmp_path / "run"
    text = lower_source(_MIXED)

    async def first() -> None:
        engine = start_run(
            text,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("k", 1): (60.0, 0)}, default=None)},
        )
        engine.inject(_ev(0, "STARTJOB", job="b"))
        engine.inject(_ev(0, "STARTJOB", job="k"))
        await engine.run_until_quiescent(_at(5))
        await _close(engine)

    async def gap() -> None:
        engine = await resume_run(
            text,
            root,
            clock=VirtualClock(start=_at(6)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await engine.run_until_quiescent(_at(6))
        engine.inject(_ev(6.5, "KILLJOB", job="m"))
        await engine.run_until_quiescent(_at(6.5))
        engine.inject(_ev(7, "STARTJOB", job="k"))
        await engine.run_until_quiescent(_at(8))
        await _close(engine)

    async def last() -> None:
        engine = await resume_run(
            text,
            root,
            clock=VirtualClock(start=_at(20)),
            adapters={"CMD": FakeAdapter({("k", 4): (60.0, 0)}, default=None)},
        )
        assert engine.journal is not None
        await engine.run_until_quiescent(_at(20))
        for minutes, kind in (
            (20.5, "KILLJOB"),
            (21, "ON_NOEXEC"),
            (22, "STARTJOB"),
            (23, "OFF_NOEXEC"),
        ):
            engine.inject(_ev(minutes, kind, job="k"))
            await engine.run_until_quiescent(_at(minutes))
        engine.inject(_ev(24, "STARTJOB", job="k"))
        await engine.run_until_quiescent(_at(30))
        engine.journal.append(
            dispatch_record(
                "k",
                9,
                attempt=OTHER_ID,
                spool=str(root / "runs" / "k.9" / "1"),
                started_at=_at(30),
                rev=1,
                first_dispatch_seq=engine.journal.next_seq(),
            )
        )
        if pruned:
            real = runner.run_history

            def without_k1(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
                runs = real(*args, **kwargs)
                return {**runs, "k": [w for w in runs.get("k", []) if w["run"] != 1]}

            with monkeypatch.context() as patched:
                patched.setattr(runner, "run_history", without_k1)
                engine.take_snapshot()
            assert prune(engine.journal.store, 2, FAR) == 1
        await _close(engine)

    asyncio.run(first())
    with _as_previous_build(monkeypatch):
        asyncio.run(gap())
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
        boundary = store.last_seq() + 1
    asyncio.run(last())
    return root, boundary


def test_mixed_coverage_takes_each_run_from_one_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    root, _ = _mixed_root(tmp_path, monkeypatch)
    offline = _offline(root)
    alone = _replay_only(root, monkeypatch)
    shown = _history(root, "k")
    assert set(alone) | {("k", run) for run in shown} <= set(offline)
    assert sorted(offline) == [("b", 1), ("k", 1), ("k", 2), ("k", 4), ("k", 9), ("m", 1)]
    # opened before the last stretch: the replay's window, through a close in the gap
    for key in (("b", 1), ("m", 1), ("k", 1), ("k", 2)):
        assert offline[key] == alone[key], key
    assert offline[("b", 1)].status not in (None, "RUNNING")
    # opened in the last stretch: the `history` row
    assert sorted(shown) == [4]
    _equal_but_listed(offline[("k", 4)], shown[4])
    # no opening anywhere: the marked row, and its line in every format
    marked = offline[("k", 9)]
    assert (marked.definition_unknown, marked.catalog_hash, marked.status) == (
        "no_opening_rev",
        None,
        "RUNNING",
    )
    for output_format in ("table", "json", "csv"):
        result = CliRunner().invoke(app, ["runs", str(root), "--format", output_format])
        assert result.exit_code == 0, result.output
        lines = [line for line in result.stderr.splitlines() if "definition_unknown=" in line]
        assert lines == [
            f"warning: {root}: k run 9: definition_unknown=no_opening_rev: neither the history"
            " index, the snapshot nor the replay holds the rev the run opened under, so its row"
            " has no hashes, no box and no verdict from a bare exit code"
        ]
        if output_format == "json":
            [row] = [row for row in json.loads(result.stdout) if row["run_number"] == 9]
            assert row["catalog_hash"] is None and row["definition_unknown"] == "no_opening_rev"
        elif output_format == "csv":
            [row] = [
                row
                for row in csv.DictReader(io.StringIO(result.stdout))
                if row["run_number"] == "9"
            ]
            assert row["catalog_hash"] == "" and row["definition_unknown"] == "no_opening_rev"
        else:
            [line] = [
                line
                for line in result.stdout.splitlines()
                if line.startswith("k ") and line.split()[1] == "9"
            ]
            assert line.split()[-2:] == ["?", "?"]


# ------------------------------------------------- healthy roots read as they did


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_a_healthy_root_reads_as_the_replay_alone_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Every scenario of the `history` tests, unpruned: the store's windows give the rows the
    replay gave."""
    _SCENARIOS[name](tmp_path)
    root = tmp_path / "run"
    assert _offline(root) == _replay_only(root, monkeypatch)


def _spooled(tmp_path: Path) -> list[Path]:
    """Two roots of real commands, each with its spool."""
    roots = [tmp_path / "a", tmp_path / "b"]
    for root, code in zip(roots, (0, 1), strict=True):
        text = f"insert_job: j1\njob_type: c\ncommand: exit {code}\nmachine: m1\n"
        asyncio.run(_run_real_and_manifest(text, root, ["j1"]))
    return roots


def test_healthy_roots_with_a_spool_read_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _spooled(tmp_path)
    with monkeypatch.context() as patched:
        patched.setattr(
            runner_history,
            "_stored_runs",
            lambda evidence, anchor_runs: runner_history._StoredRuns({}, {}),
        )
        before = read_run_roots(roots)
    after = read_run_roots(roots)
    assert after == before
    assert [(row.status, row.clock_source) for row in after] == [
        ("SUCCESS", "spool"),
        ("FAILURE", "spool"),
    ]


# ------------------------------------------------------- the listed differences


def test_the_spool_difference_is_its_times_and_clock_source(tmp_path: Path) -> None:
    root, _ = _spooled(tmp_path)
    [offline] = read_run_root(root)
    shown = _history(root, "j1")[1]
    assert (offline.clock_source, shown.clock_source) == ("spool", "journal")
    _equal_but_listed(offline, shown)


def test_the_page_boundary_difference_is_a_status_admitted_after_the_next_opening() -> None:
    """Hand-built from an engine's store, since the engine dispatches a start in its own
    admission unless a held group keeps it back: `j.1` fails at 08:01; at 08:02 a start opens
    `j.2`, an operator SUCCESS with no run number is admitted after that opening, at the same
    time, and then `j.2` dispatches. The offline fold gives the SUCCESS to `j.1`, by time, as
    a page of both runs does; a page of `j.1` alone does not hold it (control-protocol
    `history`, Paging). `j.2` is equal on every page."""

    async def scenario() -> Store:
        engine = start_run(
            lower_source("insert_job: j\njob_type: c\ncommand: x\n"),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(0.5))
        await _close(engine)
        assert engine.journal is not None
        return engine.journal.store

    store = asyncio.run(scenario())
    found = list(store.read_from(0))
    start = next(record for _, record in found if record.kind == "input")
    decided = next(record for _, record in found if record.kind == "decision")
    dispatched = next(record for _, record in found if record.kind == "dispatch")
    index = max(int(r.payload["index"]) for _, r in found if r.kind == "input")

    def admitted(at: datetime, kind: str, payload: dict[str, Any]) -> tuple[Record, Record]:
        nonlocal index
        index += 1
        body = {**start.payload, "index": index, "kind": kind, "payload": payload}
        body |= {"request_id": f"r{index}", "at": at.isoformat()}  # the time the fold reads
        decision = {**decided.payload, "index": index, "request_id": f"r{index}"}
        return Record(at, "input", None, f"r{index}", body), Record(
            at, "decision", None, None, decision
        )

    def entry(at: datetime, transition: str, run: int | None = None) -> dict[str, Any]:
        return TraceEntry(
            at=at, job="j", transition=transition, cause="c", run_number=run
        ).model_dump()

    failed = {"job": "j", "run_number": 1, "status": "FAILURE"}
    store.append(
        *admitted(_at(1), "STATUS", failed),
        transitions=[entry(_at(1), "RUNNING->FAILURE")],
        owners=[RecordOwner(0, "j", 1), RecordOwner(1, "j", 1)],
    )
    store.append(
        *admitted(_at(2), "STARTJOB", {"job": "j"}),
        transitions=[entry(_at(2), "FAILURE->STARTING", 2)],
        openings=[RunOpening(0, "j", 2, 1)],
        owners=[RecordOwner(0, "j", None), RecordOwner(1, "j", None)],
    )
    store.append(
        *admitted(_at(2), "STATUS", {"job": "j", "status": "SUCCESS"}),
        transitions=[entry(_at(2), "STARTING->SUCCESS")],
        owners=[RecordOwner(0, "j", None), RecordOwner(1, "j", None)],
    )
    second = {**dispatched.payload, "run_number": 2}
    store.append(
        Record(_at(2), "dispatch", None, None, _renumbered(second, 2)),
        owners=[RecordOwner(0, "j", 2)],
    )
    records = [wire for _, record in store.read_from(0) if (wire := wire_record(record))]
    configuration = Configuration.from_bytes(store.get_config(1), where="t")
    evidence = RunEvidence(
        store.first_seq(),
        store.last_seq(),
        store.history_coverage(),
        store.transitions_coverage(),
        list(store._job_runs),  # type: ignore[attr-defined]
        store.read_transitions(TransitionQuery(None, False, 10**9, upto=store.last_seq())).rows,
    )
    offline = {
        row.run_number: row
        for row in fold_run_rows(
            records,
            catalog=configuration.catalog,
            catalogs={1: configuration.catalog},
            evidence=evidence,
        )
    }

    def paged(limit: int) -> dict[int, RunRow]:
        pages = _pages(store, "j", limit)
        return {
            row.run_number: row
            for row in (RunRow.model_validate(run["row"]) for p in pages for run in p["runs"])
        }

    both, alone = paged(20), paged(1)
    assert offline[1].status == both[1].status == "SUCCESS" and alone[1].status == "FAILURE"
    assert offline[1] == both[1] and offline[2] == both[2] == alone[2]


def _renumbered(body: dict[str, Any], run: int) -> dict[str, Any]:
    """A dispatch body as the first attempt of run `run`, its identity rewritten."""
    attempt = _ATTEMPT.format(run)
    tag = json.loads(base64.urlsafe_b64decode(body["env"]["DSL41_RUN"]))
    run_tag = json.dumps({**tag, "run_id": attempt, "run_number": run}).encode()
    env = {
        **body["env"],
        "DSL41_RUN_NUMBER": str(run),
        "DSL41_RUN": base64.urlsafe_b64encode(run_tag).decode(),
    }
    return {**body, "run_number": run, "attempt": attempt, "env": env}


# ------------------------------------------------------------------ refusals


def test_a_run_the_index_shows_twice_is_refused_as_history_declines_it(
    tmp_path: Path, cli_env: None
) -> None:
    _SCENARIOS["KILLJOB"](tmp_path)
    root = tmp_path / "run"
    with closing(sqlite3.connect(root / STORE_NAME)) as db, db:
        seq, n, job, run, rev = db.execute(
            "SELECT seq, n, job, run_number, rev FROM job_runs ORDER BY seq LIMIT 1"
        ).fetchone()
        (top,) = db.execute("SELECT MAX(n) FROM transitions WHERE seq = ?", (seq,)).fetchone()
        db.execute(
            "INSERT INTO transitions(seq, n, job, transition, entry)"
            " SELECT seq, ?, job, transition, entry FROM transitions WHERE seq = ? AND n = ?",
            (top + 1, seq, n),
        )
        db.execute(
            "INSERT INTO job_runs(seq, n, job, run_number, rev) VALUES (?, ?, ?, ?, ?)",
            (seq, top + 1, job, run, rev),
        )
    with pytest.raises(RunHistoryError, match=f"shows run {run} of {job!r} twice"):
        read_run_root(root)
    with pytest.raises(Declined, match="twice"):
        _history(root, job)
    result = CliRunner().invoke(app, ["runs", str(root)])
    assert result.exit_code == 2 and result.stdout == ""
    assert "until retention drops those openings" in result.stderr


# ------------------------------------------------------------------ a removed job


def test_a_removed_job_s_run_before_the_snapshot_is_partial(tmp_path: Path) -> None:
    """Astra's sequence: `j.1` ends, an update removes `j`, and a snapshot is taken; the head
    is pruned up to `j.1`'s dispatch. The snapshot keeps `j`'s run counter in its tombstone,
    not in its rows, and `j.1` began before it: `partial`, as for a job still in the estate."""
    both = "insert_job: j\njob_type: c\ncommand: x\n\ninsert_job: k\njob_type: c\ncommand: y\n"

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))
        await root.update(2, "insert_job: k\njob_type: c\ncommand: y\n")
        root.engine.take_snapshot()
        assert root.engine.journal is not None
        store = root.engine.journal.store
        first = next(seq for seq, record in store.read_from(0) if record.kind == "dispatch")
        assert prune(store, first, FAR) > 0

    root, _ = _scenario(tmp_path, both, body)
    [row] = [row for row in read_run_root(root.run) if row.job == "j"]
    assert (row.run_number, row.status, row.fidelity) == (1, "SUCCESS", "partial")


def test_an_opening_below_a_later_gap_proves_its_rev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`k.1` opened in the first stretch, below the gap; the snapshot keeps no window of it.
    Its index row still names rev 1, so its row is read under rev 1, not marked."""
    root, _ = _mixed_root(tmp_path, monkeypatch, pruned=True)
    offline = _offline(root)
    alone = _replay_only(root, monkeypatch)
    assert alone[("k", 1)].definition_unknown == "no_opening_rev"
    proved = offline[("k", 1)]
    assert proved.definition_unknown is None
    assert proved.catalog_hash == offline[("k", 4)].catalog_hash is not None
    assert (proved.status, proved.exit_code) == ("SUCCESS", 0)


# ------------------------------------------------- the boundary and the key match


def _entry(seq: int, transition: str, run: int | None = None, job: str = "j") -> Any:
    entry = TraceEntry(at=_at(seq), job=job, transition=transition, cause="c", run_number=run)
    return (seq, 0, entry.model_dump())


def _evidence(
    history: list[Interval], transitions: list[Interval], opened_as: tuple[str, int] = ("j", 1)
) -> RunEvidence:
    """`j.1` opens at seq 30 and ends at 35, `j.2` opens at 70 and ends at 75, both under
    rev 1; each transition row is kept only where `transitions` covers it. `opened_as` is
    the job and run the stored entry at 30 names."""
    rows = [
        _entry(30, "INACTIVE->STARTING", opened_as[1], opened_as[0]),
        _entry(35, "RUNNING->SUCCESS"),
        _entry(70, "SUCCESS->STARTING", 2),
        _entry(75, "RUNNING->SUCCESS"),
    ]
    covered = [(i.first, i.last) for i in transitions if i.covered]
    return RunEvidence(
        1,
        100,
        history,
        transitions,
        [(seq, 0, "j", run, 1) for seq, run in ((30, 1), (70, 2))],
        [row for row in rows if any(low <= row[0] <= high for low, high in covered)],
    )


_GAPPED = [Interval(1, 50, True), Interval(51, 60, False), Interval(61, 100, True)]
_WHOLE = [Interval(1, 100, True)]


@pytest.mark.parametrize(
    ("history", "transitions"),
    [(_WHOLE, _GAPPED), (_GAPPED, _WHOLE)],
    ids=["a transitions gap", "an index gap"],
)
def test_the_boundary_is_where_both_coverages_reach_the_last_seq(
    history: list[Interval], transitions: list[Interval]
) -> None:
    """Either coverage's gap moves the boundary to 61: `j.2` takes the stored window, and
    `j.1`, below it, only its rev."""
    stored = runner_history._stored_runs(_evidence(history, transitions), {})
    assert set(stored.windows["j"]) == {2}
    assert stored.revs == {("j", 1): 1}


@pytest.mark.parametrize("opened_as", [("j", 5), ("x", 1)], ids=["another run", "another job"])
def test_an_index_row_counts_only_where_its_numbered_transition_matches(
    opened_as: tuple[str, int],
) -> None:
    stored = runner_history._stored_runs(_evidence(_WHOLE, _GAPPED, opened_as), {})
    assert set(stored.windows["j"]) == {2}
    assert stored.revs == {}


# ------------------------------------------------------------------ one read transaction


def test_the_store_s_reads_share_one_transaction(tmp_path: Path) -> None:
    """A record a writer commits inside the block is not seen by any read in it."""
    _SCENARIOS["KILLJOB"](tmp_path)
    root = tmp_path / "run"
    with closing(SqliteStore(root / STORE_NAME, read_only=True)) as reader:
        with reader.run_evidence() as evidence:
            with closing(sqlite3.connect(root / STORE_NAME)) as db, db:
                db.execute(
                    "INSERT INTO journal(at_utc, kind, actor, request_id, payload)"
                    " SELECT at_utc, kind, actor, NULL, payload FROM journal"
                    " WHERE kind = 'engine' LIMIT 1"
                )
            assert reader.last_seq() == evidence.last_seq
            assert max(seq for seq, _ in reader.read_from(0)) == evidence.last_seq
        assert reader.last_seq() == evidence.last_seq + 1
