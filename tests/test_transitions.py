"""The committed transition rows (DL-536): each record's trace entries commit in its own
transaction, the table states its coverage, the start records what another build
left, pruning trims the rows with the journal, and the `transitions` verb pages them off the
engine's loop. Every check is deterministic: no sleep stands in for an ordering.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from retention import EarlierPrune, prune
from store_surgery import rows as journal_rows
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import runner_startup, runner_transitions
from dsl41.canon import CanonError, canonical_bytes, decode
from dsl41.ir import lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner import Engine
from dsl41.runner_access import REQUIRED_TIER, Tier
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import STORE_NAME, Journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import resume_run, start_run, validate_resume
from dsl41.runner_store import (
    Interval,
    MemoryStore,
    Record,
    SqliteStore,
    Store,
    StoreError,
    TransitionQuery,
)
from dsl41.runner_transitions import CURSOR_MAX, Request, begin, fit, parse

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 1\n"
_PAIR_JIL = _JIL + "\ninsert_job: k\njob_type: c\ncommand: y\n"
KINDS = ["sqlite", "memory"]


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _canon(entry: TraceEntry) -> dict[str, Any]:
    decoded = decode(canonical_bytes(entry.model_dump()))
    assert isinstance(decoded, dict)
    return decoded


def _everything(store: Store) -> list[tuple[int, int, dict[str, Any]]]:
    return store.read_transitions(TransitionQuery(None, False, 10**9, upto=store.last_seq())).rows


def _stored(run_root: Path) -> list[tuple[int, int, dict[str, Any]]]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return _everything(store)


def _coverage(run_root: Path) -> list[Interval]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return store.transitions_coverage()


def _last(run_root: Path) -> int:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return store.last_seq()


def _first(run_root: Path) -> int:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return store.first_seq()


def _start(run_root: Path, text: str = _JIL, **kwargs: Any) -> Engine:
    return start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter({("j", 1): (300.0, 0)}, default=None)},
        **kwargs,
    )


async def _resume(run_root: Path, minutes: float, text: str = _JIL, **kwargs: Any) -> Engine:
    return await resume_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0 + timedelta(minutes=minutes)),
        adapters={"CMD": FakeAdapter(default=None)},
        **kwargs,
    )


def _entry(n: int = 0, **fields: Any) -> dict[str, Any]:
    return {
        "at": T0 + timedelta(seconds=n),
        "job": "j",
        "transition": "INACTIVE->STARTING",
        "cause": f"cause {n}",
        "start_source": None,
        "run_number": None,
        "refused_status": None,
        **fields,
    }


def _record(n: int = 0) -> Record:
    return Record(T0 + timedelta(seconds=n), "engine", None, None, {"event": "started", "n": n})


def reply(read: Any, request: Request, *, byte_cap: int = 1 << 20) -> dict[str, Any]:
    return fit(read, request, header={}, byte_cap=byte_cap)


def _cover(store: Store, rows: list[Any], intervals: list[Interval]) -> None:
    """Rows written straight into the table, then the coverage that claims them: a store built
    for a read test, with rows no admission produced."""
    from dsl41.runner_store import _encode_transition

    encoded = [(seq, n, _encode_transition(entry)) for seq, n, entry in rows]
    if isinstance(store, SqliteStore):
        with store._transaction():
            store._db.executemany(
                "INSERT OR REPLACE INTO transitions(seq, n, job, transition, entry)"
                " VALUES (?, ?, ?, ?, ?)",
                [(seq, n, row.job, row.transition, row.entry) for seq, n, row in encoded],
            )
    else:
        assert isinstance(store, MemoryStore)
        keys = {(seq, n) for seq, n, _ in encoded}
        kept = [row for row in store._transitions if (row[0], row[1]) not in keys]
        store._transitions = sorted([*kept, *encoded], key=lambda r: (r[0], r[1]))
    store.cover_transitions(intervals)


@contextlib.contextmanager
def _store(kind: str, directory: Path) -> Iterator[Store]:
    store: Store = SqliteStore(directory / "s.db") if kind == "sqlite" else MemoryStore()
    try:
        yield store
    finally:
        store.close()


# ------------------------------------------------------------------ the commit


def test_an_admission_commits_the_entries_it_added_under_its_input_seq(tmp_path: Path) -> None:
    """The rows are the in-memory trace, entry for entry and in order, each under the `input`
    record whose admission added it, with `n` its ordinal there. A time-only admission (the
    `term_run_time` kill) has its rows; an admission that moves nothing (a global no job reads)
    has none, and coverage still runs over every record."""
    run_root = tmp_path / "run"

    async def scenario() -> list[TraceEntry]:
        engine = _start(run_root)
        engine.inject(_ev("STARTJOB", 0, job="j"))
        engine.inject(_ev("SET_GLOBAL", 0.5, name="G", value="1"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        trace = engine.oracle.trace()
        await _close(engine)
        return trace

    trace = asyncio.run(scenario())
    stored = _stored(run_root)
    assert [entry for _, _, entry in stored] == [_canon(entry) for entry in trace]
    inputs = {seq: body for seq, kind, body in journal_rows(run_root) if kind == "input"}
    by_seq: dict[int, list[int]] = {}
    for seq, n, _ in stored:
        by_seq.setdefault(seq, []).append(n)
    assert set(by_seq) <= set(inputs)
    assert all(ns == list(range(len(ns))) for ns in by_seq.values())
    timed = {seq for seq, body in inputs.items() if "kind" not in body}
    assert any(e["transition"] == "RUNNING->TERMINATED" for s, _, e in stored if s in timed)
    [global_input] = [seq for seq, body in inputs.items() if body.get("kind") == "SET_GLOBAL"]
    assert global_input not in by_seq
    assert _coverage(run_root) == [Interval(1, _last(run_root), True)]


def _updated(run_root: Path) -> None:
    """An update applied with no job running: its `config applied` and its CONFIG mark."""
    from reload_fixtures import prepare_estate, write_config
    from test_update_wiring import Estate, _jil, _plan, _request

    from dsl41 import main_config as main

    estate = Estate(run_root.parent, _jil({"a": "x"}))

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            estate.catalog,
            run_root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "anchor"}))
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        write_config(estate.dir)
        estate.write(_jil({"a": "y"}))
        answer = engine.request_update(_request(engine, _plan(engine, estate)))
        await engine.run_until_quiescent(clock.now())
        assert answer.done()
        await _close(engine)

    asyncio.run(scenario())


def test_a_config_mark_commits_with_its_config_applied_record(tmp_path: Path) -> None:
    """An update's `CONFIG` mark is a row of its `config applied` record."""
    run_root = tmp_path / "run"
    _updated(run_root)
    [applied] = [
        seq
        for seq, kind, body in journal_rows(run_root)
        if kind == "config" and body["event"] == "applied" and body["rev"] == 2
    ]
    marks = [(n, e) for s, n, e in _stored(run_root) if s == applied]
    assert [(n, e["job"], e["transition"], e["cause"]) for n, e in marks] == [
        (0, "", "CONFIG", "configuration rev 2")
    ]


def test_a_failed_row_insert_leaves_neither_the_records_nor_any_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows join the records' transaction: a failure on the row insert, after the journal
    rows were written in it, rolls back both, and the store reopens without them."""
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        store.append(_record(0), transitions=[_entry(0)])
        real = store._record_transitions

        def fails(first: int, last: int, rows: list[Any]) -> None:
            assert store._db.in_transaction
            (written,) = store._db.execute("SELECT COUNT(*) FROM journal").fetchone()
            assert written == 3  # the two records of this append are in the transaction
            real(first, last, rows)
            raise OSError("disk full")

        monkeypatch.setattr(store, "_record_transitions", fails)
        with pytest.raises(OSError, match="disk full"):
            store.append(_record(1), _record(2), transitions=[_entry(1), _entry(2)])
    with closing(SqliteStore(path)) as reopened:
        assert [seq for seq, _ in reopened.read_from(0)] == [1]
        assert [(s, n) for s, n, _ in _everything(reopened)] == [(1, 0)]
        assert reopened.transitions_coverage() == [Interval(1, 1, True)]


@pytest.mark.parametrize("kind", KINDS)
def test_an_entry_at_the_edges_of_what_the_oracle_accepts_reads_back_equal(
    kind: str, tmp_path: Path
) -> None:
    """A run number past any fixed-width integer, a long cause and an entry with every
    optional field absent are stored and read back unchanged: the row keeps canonical bytes,
    whose grammar is the journal's."""
    entries = [
        TraceEntry(at=T0, job="j", transition="RUNNING->FAILURE", cause="x", run_number=2**200),
        TraceEntry(at=T0, job="j" * 64, transition="EVENT_IGNORED", cause="é\n\x00" * 100_000),
        TraceEntry(at=T0, job="", transition="CONFIG", cause=""),
    ]
    with _store(kind, tmp_path) as store:
        store.append(_record(0), transitions=[entry.model_dump() for entry in entries])
        assert [TraceEntry.model_validate(e) for _, _, e in _everything(store)] == entries


@pytest.mark.parametrize("kind", KINDS)
def test_an_entry_the_store_cannot_hold_is_refused_before_anything_is_written(
    kind: str, tmp_path: Path
) -> None:
    """An entry canon refuses, or one with no string job or transition, refuses the append, and
    rows with no record are refused too; nothing is written."""
    with _store(kind, tmp_path) as store:
        for bad in ({**_entry(), "job": 1}, {**_entry(), "transition": "\ud800"}, _entry(f=1.5)):
            with pytest.raises(CanonError):
                store.append(_record(0), transitions=[bad])
        with pytest.raises(CanonError, match="mapping"):
            store.append(_record(0), transitions=["not an entry"])  # type: ignore[list-item]
        with pytest.raises(StoreError, match="with the record"):
            store.append(transitions=[_entry()])
        assert store.append() == 0
        assert store.last_seq() == 0 and _everything(store) == []


def _commits(store: SqliteStore) -> list[str]:
    """The statements that end a transaction on `store`'s connection, as they run."""
    ended: list[str] = []
    store._db.set_trace_callback(
        lambda statement: ended.append(statement) if statement.startswith("COMMIT") else None
    )
    return ended


def test_an_admission_is_one_transaction_and_a_start_with_nothing_to_do_adds_one(
    tmp_path: Path,
) -> None:
    """An admission with its rows and its coverage is one commit, as it was before the rows.
    A start with nothing to trim and no gap to record writes its history row alone, in one
    commit; a fresh root the same."""
    path = tmp_path / "s.db"

    def record(n: int) -> Record:  # no engine start: there is no earlier start to bound
        return Record(T0, "input", None, None, {"n": n})

    with closing(SqliteStore(path)) as store:
        ended = _commits(store)
        assert begin(store, where="t") is None
        assert len(ended) == 1
        ended.clear()
        store.append(record(0), record(1), transitions=[_entry(0), _entry(1)])
        assert len(ended) == 1
        ended.clear()
        store.append(record(2))  # no rows: coverage still grows in the same commit
        assert len(ended) == 1
    with closing(SqliteStore(path)) as store:  # a restart
        ended = _commits(store)
        assert begin(store, where="t") is None
        assert len(ended) == 1
        assert store.transitions_coverage() == [Interval(1, 3, True)]


# ------------------------------------------------------------------ restart


def _restartable(run_root: Path) -> int:
    """An engine that snapshots in the middle of its admissions and goes on after it: the
    restart's tail is what follows the snapshot. Answers the last seq."""

    async def scenario() -> None:
        engine = _start(run_root, _PAIR_JIL, settings=EngineSettings())
        engine.inject(_ev("STARTJOB", 0, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        engine.take_snapshot()
        engine.inject(_ev("STARTJOB", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _close(engine)

    asyncio.run(scenario())
    return _last(run_root)


def test_a_restart_records_no_gap_over_its_own_tail_and_loses_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail a restart reads back into its oracle was committed with its rows: the start's
    steps record no gap and write no row, the rows below the restart are as they were, and
    coverage runs on."""
    run_root = tmp_path / "run"
    last = _restartable(run_root)
    before = _stored(run_root)
    covers: list[Any] = []
    monkeypatch.setattr(SqliteStore, "cover_transitions", lambda *args: covers.append(args))

    async def restart() -> None:
        engine = await _resume(run_root, 4, _PAIR_JIL)
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)

    asyncio.run(restart())
    assert covers == []
    assert [row for row in _stored(run_root) if row[0] <= last] == before
    assert _coverage(run_root) == [Interval(1, _last(run_root), True)]


def test_the_validate_only_path_writes_nothing(tmp_path: Path) -> None:
    """`validate_resume` reads a private copy and returns before the start's steps: the store's
    rows, coverage and histories are as they were, byte for byte."""
    run_root = tmp_path / "run"
    _restartable(run_root)
    path = run_root / STORE_NAME
    before = path.read_bytes()

    asyncio.run(
        validate_resume(
            lower_source(_PAIR_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=4)),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=None,
            declared=_declared(run_root),
            machines=None,
            settings=EngineSettings(),
            stored_sources=False,
        )
    )
    assert path.read_bytes() == before


def _declared(run_root: Path) -> Any:
    from dsl41.runner_startup import active_configuration

    return active_configuration(run_root).profile


def _recovered(run_root: Path) -> int:
    """An engine dies with `k`'s attempt open (`k` is marked non-idempotent); the restart ends
    it with its own input. Answers the last seq before the restart."""

    async def first() -> None:
        engine = start_run(
            lower_source(_PAIR_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"k": "posts twice"},
        )
        engine.inject(_ev("STARTJOB", 0, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())
    last = _last(run_root)

    async def restart() -> None:
        engine = await _resume(run_root, 2, _PAIR_JIL)
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _close(engine)

    asyncio.run(restart())
    return last


def _drained(run_root: Path) -> None:
    """A stop drains the engine while `j` runs: `draining`, the drain's snapshot and
    `clean_shutdown` follow the admissions."""

    async def scenario() -> None:
        engine = _start(run_root, settings=EngineSettings())
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))
        engine.request_stop()
        await engine.run_until_quiescent(T0 + timedelta(minutes=10))
        await _close(engine)

    asyncio.run(scenario())
    events = [body.get("event") for _, kind, body in journal_rows(run_root) if kind == "engine"]
    assert "draining" in events and "clean_shutdown" in events


def test_a_completion_observed_after_a_restart_is_a_new_admission_with_new_rows(
    tmp_path: Path,
) -> None:
    """An attempt that died with the engine is ended by the restart's own input (source
    `reconcile`): a new admission, past the old last seq, whose rows are its own."""
    run_root = tmp_path / "run"
    last = _recovered(run_root)
    [fed] = [
        seq
        for seq, kind, body in journal_rows(run_root)
        if kind == "input" and body.get("source") == "reconcile"
    ]
    assert fed > last
    assert [e["transition"] for s, _, e in _stored(run_root) if s == fed] == [
        "ON_HOLD",
        "RUNNING->FAILURE",
    ]
    assert _coverage(run_root) == [Interval(1, _last(run_root), True)]


# ------------------------------------------------------------------ coverage and the start


def _previous_store_module() -> ModuleType:
    """The store code of the build before this change (commit cfd6fd3), loaded as a module of
    its own. The copy is `git show cfd6fd3:src/dsl41/runner_store.py`, kept in the tests because
    CI's test checkout is shallow and holds no older commit."""
    name = "previous_runner_store"
    if name not in sys.modules:
        source = ROOT / "tests" / "fixtures" / "previous_runner_store.py"
        spec = importlib.util.spec_from_file_location(name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class _PreviousBuild(EarlierPrune):
    """The previous build's writer over a root: its store code, which knows no transition rows,
    under this build's engine, whose state machine and records the previous build shares."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def append(self, *records: Record, **rows: Any) -> int:
        return int(self._store.append(*records))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


@contextlib.contextmanager
def _as_previous_build(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Engines started inside write with the previous build's store and settle no rows."""
    old = _previous_store_module()
    real = runner_startup.SqliteStore

    def opened(path: Path, **kwargs: Any) -> Any:
        if kwargs.get("read_only"):
            return real(path, **kwargs)
        return _PreviousBuild(old.SqliteStore(path, **kwargs))

    with monkeypatch.context() as patched:
        patched.setattr(runner_startup, "SqliteStore", opened)
        patched.setattr(runner_startup, "begin_transitions", lambda store, *, where: None)
        patched.setattr(runner_startup, "settle_history", lambda store, *, where: None)
        yield


def _latest(run_root: Path) -> datetime:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        latest = store.latest_at()
    assert latest is not None
    return latest


async def _incarnation(
    run_root: Path, *, snapshots: int = 0, prune: bool = False, genesis: bool = False
) -> None:
    """One engine run on a virtual clock that starts after the root's latest record: a start
    and a kill of `j` around each of `snapshots` snapshots. With `prune`, the clock passes the
    retention window before each snapshot, so its prune takes the journal up to it."""
    settings = EngineSettings(retention_days=1)
    if genesis:
        engine = _start(run_root, _JIL, settings=settings)
    else:
        engine = await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=_latest(run_root) + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=settings,
        )
        await engine.run_until_quiescent(engine.clock.now())
    clock = engine.clock
    assert isinstance(clock, VirtualClock)
    for step in range(snapshots + 1):
        now = clock.now()
        engine.inject(Event(at=now, kind="FORCE_STARTJOB", payload={"job": "j"}))
        engine.inject(Event(at=now + timedelta(seconds=6), kind="KILLJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(now + timedelta(minutes=1))
        if step < snapshots:
            if prune:
                clock._now = clock.now() + timedelta(days=2)
            engine.take_snapshot()
    await _close(engine)


def test_a_root_that_never_had_the_table_is_a_gap_up_to_this_builds_first_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root the previous build made holds no rows. The first start here fills none: every
    record it finds is a recorded gap, and coverage starts at the first record it commits."""
    run_root = tmp_path / "run"
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(run_root, genesis=True, snapshots=1))
    with closing(sqlite3.connect(run_root / STORE_NAME)) as db:
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
    assert "transitions" not in tables
    last = _last(run_root)
    asyncio.run(_incarnation(run_root))
    assert _coverage(run_root) == [
        Interval(1, last, False),
        Interval(last + 1, _last(run_root), True),
    ]
    assert _stored(run_root) and all(seq > last for seq, _, _ in _stored(run_root))


def test_an_earlier_builds_appends_are_exactly_a_gap_and_coverage_resumes_after_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downgrade sequence: this build covers through L, an earlier build appends L+1 to M
    and snapshots inside them, and this build, reopened, records exactly [L+1, M] as a gap and
    covers from M+1."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    covered_through = _last(run_root)
    assert _coverage(run_root) == [Interval(1, covered_through, True)]
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(run_root, snapshots=1))
    appended = _last(run_root)
    assert _coverage(run_root) == [Interval(1, covered_through, True)]
    asyncio.run(_incarnation(run_root))
    assert _coverage(run_root) == [
        Interval(1, covered_through, True),
        Interval(covered_through + 1, appended, False),
        Interval(appended + 1, _last(run_root), True),
    ]
    assert not [seq for seq, _, _ in _stored(run_root) if covered_through < seq <= appended]


async def _ask(run_root: Path, request: dict[str, Any], *, status: bool = False) -> tuple[Any, ...]:
    """Restart the root, admit one start and one kill, and ask `transitions` (and `status`)."""
    engine = await resume_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=_latest(run_root) + timedelta(minutes=1)),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    await engine.run_until_quiescent(engine.clock.now())
    now = engine.clock.now()
    engine.inject(Event(at=now, kind="FORCE_STARTJOB", payload={"job": "j"}))
    engine.inject(Event(at=now + timedelta(seconds=6), kind="KILLJOB", payload={"job": "j"}))
    await engine.run_until_quiescent(now + timedelta(minutes=1))
    server = _server(engine, run_root)
    answer = await server._respond({"cmd": "transitions", **request})
    status_answer = await server._respond({"cmd": "status"})
    await _close(engine)
    return (answer, engine, status_answer) if status else (answer, engine)


@pytest.mark.parametrize("step", ["trim_transitions", "cover_transitions", "begin_history"])
def test_a_failure_in_any_step_of_the_start_leaves_the_engine_running_and_the_verb_declining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], step: str
) -> None:
    """Whatever step raises, the start goes on and says so in one line; the run's admissions
    still commit their rows; `transitions` declines with the reason and asserts nothing; the
    next start runs the steps again and the verb serves."""
    run_root = tmp_path / "run"
    with _as_previous_build(monkeypatch):  # so the start has a gap to record
        asyncio.run(_incarnation(run_root, genesis=True))
    before = _last(run_root)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError(f"{step} failed")

    failed = f"OSError: {step} failed"
    reason = (
        "the start could not settle the transition rows: "
        + {
            "trim_transitions": f"settle the rows: {failed}",
            "cover_transitions": f"settle the rows: {failed}",
            # the earlier build's start has no history row, so the first step writes one too
            "begin_history": f"bound the earlier starts: {failed}; begin the history: {failed}",
        }[step]
    )
    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, step, broken)
        answer, engine, status = asyncio.run(_ask(run_root, {"history": "h"}, status=True))
    assert engine.started.is_set()
    assert "`transitions` declines" in capfd.readouterr().err
    assert answer == {
        "ok": False,
        "code": "transitions_unavailable",
        "error": reason,
        "baseline_id": engine.baseline_id,
        "applied_index": engine.frontiers.applied_index,
    }
    assert status["transitions_unavailable"] == reason
    assert [seq for seq, _, _ in _stored(run_root) if seq > before]  # rows still commit
    events = [b.get("event") for _, kind, b in journal_rows(run_root) if kind == "engine"]
    assert "shutdown_during_startup" not in events
    served, _, status = asyncio.run(_ask(run_root, {}, status=True))
    assert served["ok"] is True and served["entries"]
    assert "transitions_unavailable" not in status
    assert _coverage(run_root)[0] == Interval(1, before, False)


def test_a_restored_branch_whose_start_failed_asserts_no_history_or_continuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two branches of one stopped backup: the first runs on under a history a client reads;
    the second is restored and its start cannot begin a history. It must not answer with the
    copy's old history, and so cannot vouch for the client's rows."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    copy = tmp_path / "copy"
    shutil.copytree(run_root, copy)
    first, _ = asyncio.run(_ask(run_root, {}))
    assert first["ok"] and first["history"]
    shutil.rmtree(run_root)
    shutil.copytree(copy, run_root)
    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "begin_history", lambda self, history: 1 / 0)
        answer, _ = asyncio.run(_ask(run_root, {"history": first["history"]}))
    assert answer["code"] == "transitions_unavailable"
    assert not {"history", "continuous_through", "entries", "covered"} & set(answer)


def test_a_restored_live_snapshot_whose_first_start_fails_its_trim_keeps_its_boundary() -> None:
    """The review's reproduction: history H covers seqs 1 to 3, a live snapshot is taken there,
    the original runs on to 6 and a client holds rows 4 to 6. The snapshot is restored; its
    first start's trim fails, its own run writes 4 to 6 anew, and a later start serves. The
    client naming H is told the store agrees with it through 3, not 6."""
    import copy

    def record(n: int) -> Record:  # a record that is no engine start
        return Record(T0 + timedelta(seconds=n), "engine", None, None, {"event": "x", "n": n})

    store = MemoryStore()
    assert begin(store, where="t") is None
    for n in range(3):
        store.append(record(n), transitions=[_entry(n, cause="original")])
    snapshot = copy.deepcopy(store)
    for n in range(3, 6):
        store.append(record(n), transitions=[_entry(n, cause="abandoned future")])
    [(old, _)] = store.histories()
    restored = snapshot

    def transient() -> None:
        raise OSError("transient")

    real = restored.trim_transitions
    restored.trim_transitions = transient  # type: ignore[method-assign]
    assert "settle the rows: OSError: transient" in str(begin(restored, where="t"))
    restored.trim_transitions = real  # type: ignore[method-assign]
    for n in range(3, 6):
        restored.append(record(n), transitions=[_entry(n, cause="restored run")])
    assert begin(restored, where="t") is None
    query = TransitionQuery(None, False, 100, upto=restored.last_seq())
    answer = fit(restored.read_transitions(query), Request(query, old), header={})
    assert answer["continuous_through"] == 3


def test_a_start_whose_history_row_failed_is_bounded_by_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that could not write its own history row still recorded `engine started`. The
    next start gives it a row just after the `engine started` before it, so the history before
    it vouches for nothing that start wrote."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    first, _ = asyncio.run(_ask(run_root, {}))
    held = first["history"]
    before = _last(run_root)
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        previous_start = store.started_seqs()[-1]
    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "begin_history", lambda self, *args: 1 / 0)
        declined, _ = asyncio.run(_ask(run_root, {}))
    assert declined["code"] == "transitions_unavailable"
    served, _ = asyncio.run(_ask(run_root, {"history": held}))
    assert served["ok"] is True
    assert served["continuous_through"] == previous_start < before
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        assert [first for _, first in store.histories()][-2] == previous_start + 1


def test_bounding_earlier_starts_writes_nothing_when_every_start_has_its_row() -> None:
    store = MemoryStore()
    assert begin(store, where="t") is None
    store.append(Record(T0, "engine", None, None, {"event": "started", "at": T0.isoformat()}))
    assert begin(store, where="t") is None
    assert len(store.histories()) == 2


@pytest.mark.parametrize("kind", KINDS)
def test_a_forward_cursor_at_the_last_pruned_row_is_not_expired(kind: str, tmp_path: Path) -> None:
    """Expired means a row after the cursor was pruned: a cursor at the last row of `floor - 1`
    had read every removed row; one before it had not."""
    with _store(kind, tmp_path) as store:
        for n in range(8):
            store.append(_record(n), transitions=[_entry(0), _entry(1)])
        store.put_snapshot(5, STATE_MACHINE_VERSION, b"{}")
        prune(store, 5, T0 + timedelta(days=1))
        assert store.first_seq() == 5

        def page(cursor: tuple[int, int]) -> dict[str, Any]:
            query = TransitionQuery(cursor, False, 2, upto=8)
            return runner_transitions.answer(store, Request(query, None), header={})

        at_last = page((4, 1))
        assert (at_last["expired"], at_last["pruned"]) == (False, [])
        assert [(e["seq"], e["n"]) for e in at_last["entries"]] == [(5, 0), (5, 1)]
        before_last = page((4, 0))
        assert (before_last["expired"], before_last["pruned"]) == (True, [[4, 4]])
        earlier = page((2, 1))
        assert (earlier["expired"], earlier["pruned"]) == (True, [[2, 4]])
        at_floor = page((5, 0))
        assert (at_floor["expired"], at_floor["pruned"]) == (False, [])


_SCENARIOS: dict[str, Any] = {
    "admissions": lambda root: asyncio.run(_incarnation(root, genesis=True)),
    "snapshots and a prune": lambda root: asyncio.run(
        _incarnation(root, genesis=True, snapshots=2, prune=True)
    ),
    "a restart": lambda root: (
        asyncio.run(_incarnation(root, genesis=True, snapshots=1)),
        asyncio.run(_incarnation(root)),
    ),
    "a restart mid-run": _restartable,
    "an update applied": _updated,
    "a drain": _drained,
    "a recovery input after a restart": _recovered,
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_the_equality_check_compares_every_named_scenario(tmp_path: Path, name: str) -> None:
    """The rows of each named scenario equal the offline replay's trace (`transitions_check`),
    and the check compared a store and its rows: it does not pass for want of anything."""
    import transitions_check

    before = dict(transitions_check.compared)
    _SCENARIOS[name](tmp_path / "run")
    assert transitions_check.check_roots(tmp_path) > 0
    assert transitions_check.compared["stores"] > before["stores"]


def test_rows_the_previous_build_pruned_below_are_removed_and_coverage_trimmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The previous build prunes the journal and leaves the rows: the next start here removes
    every row below the journal's first seq and trims coverage to it."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(run_root, snapshots=1, prune=True))
    first = _first(run_root)
    assert [seq for seq, _, _ in _stored(run_root) if seq < first]
    asyncio.run(_incarnation(run_root))
    assert first == _first(run_root)
    assert all(seq >= first for seq, _, _ in _stored(run_root))
    assert all(interval.first >= first for interval in _coverage(run_root))


@pytest.mark.parametrize("kind", KINDS)
def test_rows_and_coverage_beyond_the_retained_journal_are_dropped(
    kind: str, tmp_path: Path
) -> None:
    """A copy whose rows outlived its journal (a file-by-file copy of a live root) keeps no row
    or coverage outside the retained records."""
    with _store(kind, tmp_path) as store:
        store.append(_record(0), transitions=[_entry(0)])
        _cover(
            store,
            [(1, 0, _entry(0)), (5, 0, _entry(5))],
            [Interval(1, 1, True), Interval(4, 6, True)],
        )
        store.trim_transitions()
        assert [(s, n) for s, n, _ in _everything(store)] == [(1, 0)]
        assert store.transitions_coverage() == [Interval(1, 1, True)]


@pytest.mark.parametrize("kind", KINDS)
def test_written_intervals_replace_what_coverage_said_and_join_their_kind(
    kind: str, tmp_path: Path
) -> None:
    """A gap the start records replaces the coverage over its seqs and joins an adjacent one of
    its kind; an append after a gap starts the covered interval that follows it."""
    with _store(kind, tmp_path) as store:
        store.append(*[_record(n) for n in range(5)])
        store.cover_transitions([Interval(2, 2, False)])
        store.cover_transitions([Interval(3, 3, False)])
        assert store.transitions_coverage() == [
            Interval(1, 1, True),
            Interval(2, 3, False),
            Interval(4, 5, True),
        ]
        store.append(_record(5))
        assert store.transitions_coverage()[-1] == Interval(4, 6, True)


def test_a_restored_copy_tells_a_client_its_cursor_is_stale(tmp_path: Path) -> None:
    """A whole-root copy taken at a stop and restored later forgets the histories it never saw:
    a client that read under one of those is told nothing it holds past the copy is the store's
    (`continuous_through`), while a client of the copy's own history keeps its cursor."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    copy = tmp_path / "copy"
    shutil.copytree(run_root, copy)
    copied_through = _last(run_root)
    asyncio.run(_incarnation(run_root))

    def histories(root: Path) -> list[tuple[str, int]]:
        with closing(SqliteStore(root / STORE_NAME, read_only=True)) as store:
            return store.read_transitions(TransitionQuery(None, False, 0, upto=0)).histories

    [(genesis, _), (abandoned, _)] = histories(run_root)
    shutil.rmtree(run_root)
    shutil.copytree(copy, run_root)
    asyncio.run(_incarnation(run_root))
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        upto = store.last_seq()

        def agreed(history: str) -> int:
            request = Request(TransitionQuery(None, False, 1, upto=upto), history)
            answer = reply(store.read_transitions(request.query), request)
            assert answer["history"] != abandoned
            return int(answer["continuous_through"])

        assert agreed(abandoned) == 0
        assert agreed(genesis) == copied_through
        assert agreed(histories(run_root)[-1][0]) == upto


# ------------------------------------------------------------------ pruning


@pytest.mark.parametrize("kind", KINDS)
def test_pruning_deletes_the_rows_below_its_actual_bound_with_the_journal(
    kind: str, tmp_path: Path
) -> None:
    """The bound is the store's own (`prune_bound`'s, the time cutoff included): rows below the
    new first seq go in each step's transaction and coverage is trimmed there; rows at and
    above the bound stay (DL-543)."""
    with _store(kind, tmp_path) as store:
        for n in range(6):
            store.append(_record(n), transitions=[_entry(n)])
        store.put_snapshot(5, STATE_MACHINE_VERSION, b"{}")
        # the time cutoff binds first: record 3 is the first inside the window
        bound = store.prune_bound(5, T0 + timedelta(seconds=2))
        assert bound == 3
        assert store.prune_step(bound, 1).deleted == 1
        assert [seq for seq, _, _ in _everything(store)] == [2, 3, 4, 5, 6]
        assert store.transitions_coverage() == [Interval(2, 6, True)]
        assert store.prune_step(bound, 2000).deleted == 1
        assert store.first_seq() == 3
        assert [seq for seq, _, _ in _everything(store)] == [3, 4, 5, 6]
        assert store.transitions_coverage() == [Interval(3, 6, True)]


# ------------------------------------------------------------------ the read


def _filled(path: Path, records: int, per: int) -> SqliteStore:
    store = SqliteStore(path)
    store.append(*[_record(n) for n in range(records)])
    jobs = ["a", "b", "c", "d"]
    _cover(
        store,
        [
            (
                seq,
                n,
                _entry(
                    n,
                    job=jobs[(seq + n) % 4],
                    transition=f"T{n % 3}",
                    cause="c" * (3000 if seq % 12 == 0 else 5),
                ),
            )
            for seq in range(1, records + 1)
            for n in range(per)
        ],
        [Interval(1, records, True)],
    )
    return store


#: A page's entry limit and byte cap small enough that a few hundred rows cross both.
_PAGE_LIMIT, _PAGE_BYTES = 40, 9000


def _pages(store: Store, *, backward: bool, start: Any = None) -> Iterator[Any]:
    cursor = start
    while True:
        query = TransitionQuery(cursor, backward, _PAGE_LIMIT, upto=store.last_seq())
        answer = runner_transitions.answer(
            store, Request(query, None), header={}, byte_cap=_PAGE_BYTES
        )
        yield answer
        if not answer["more"]:
            return
        cursor = tuple(answer["next"])


def test_paging_both_ways_returns_every_row_once_across_a_restart(tmp_path: Path) -> None:
    """Forward and backward pages cover every row exactly once, in key order; some pages stop at
    the entry limit and some at the byte cap; a cursor taken before the store is closed and
    reopened by a restart resumes exactly. A test of the rule needs only enough rows to cross
    both caps several times."""
    path = tmp_path / "s.db"
    records, per = 60, 5
    store = _filled(path, records, per)
    expected = [(seq, n) for seq in range(1, records + 1) for n in range(per)]
    for backward in (False, True):
        seen: list[tuple[int, int]] = []
        sizes: list[tuple[int, bool]] = []
        pages = _pages(store, backward=backward)
        for _ in range(3):
            page = next(pages)
            keys = [(e["seq"], e["n"]) for e in page["entries"]]
            seen.extend(reversed(keys) if backward else keys)
            sizes.append((len(keys), page["more"]))
        cursor = tuple(page["next"])
        store.close()
        store = SqliteStore(path)  # a restart reopens the store and begins a history
        assert begin(store, where="test") is None
        for page in _pages(store, backward=backward, start=cursor):
            keys = [(e["seq"], e["n"]) for e in page["entries"]]
            seen.extend(reversed(keys) if backward else keys)
            sizes.append((len(keys), page["more"]))
            assert _encoded(page) <= _PAGE_BYTES
        assert seen == (list(reversed(expected)) if backward else expected)
        assert (_PAGE_LIMIT, True) in sizes  # a page the entry limit stopped
        assert any(size < _PAGE_LIMIT and more for size, more in sizes)  # the byte cap stopped
    store.close()


def _plans_of(store: SqliteStore, query: TransitionQuery) -> list[str]:
    statements: list[str] = []
    store._db.set_trace_callback(statements.append)
    try:
        store.read_transitions(query)
    finally:
        store._db.set_trace_callback(None)
    plans = []
    for statement in statements:
        if statement.startswith("SELECT") and "FROM transitions " in statement + " ":
            for row in store._db.execute("EXPLAIN QUERY PLAN " + statement):
                plans.append(str(row[3]))
    return plans


@pytest.mark.parametrize("job", [None, "a"])
@pytest.mark.parametrize("transition", [None, "T1"])
@pytest.mark.parametrize("backward", [False, True])
@pytest.mark.parametrize("cursor", [None, (10, 3)])
def test_each_filter_combination_reads_through_an_index(
    tmp_path: Path, job: str | None, transition: str | None, backward: bool, cursor: Any
) -> None:
    """The plan of every page and of the counts names the key or an index and searches it from
    the cursor: no read scans the table."""
    with closing(_filled(tmp_path / "s.db", 20, 4)) as store:
        query = TransitionQuery(
            cursor, backward, 50, upto=20, job=job, transition=transition, counts=("T1",)
        )
        plans = _plans_of(store, query)
        page, counts = plans[0], plans[1]
        index = {
            (False, False): "USING PRIMARY KEY",
            (True, False): "USING INDEX transitions_job ",
            (False, True): "USING INDEX transitions_transition ",
            (True, True): "USING INDEX transitions_job_transition ",
        }[(job is not None, transition is not None)]
        assert page.startswith("SEARCH") and index in page + " ", page
        if cursor is not None or backward:
            assert ("(seq,n)<(?,?)" if backward else "(seq,n)>(?,?)") in page, page
        assert counts.startswith("SEARCH transitions USING INDEX transitions_transition"), counts
        assert not [plan for plan in plans if plan.startswith("SCAN")]


_HEADER = {"ok": True, "baseline_id": "b" * 36, "applied_index": 12345}


def _encoded(answer: dict[str, Any]) -> int:
    """The bytes of an answer as the control server writes it."""
    return len(json.dumps(answer, sort_keys=True).encode("utf-8"))


@pytest.mark.parametrize("kind", KINDS)
def test_a_whole_answer_never_passes_the_byte_cap_and_its_cursor_resumes_exactly(
    kind: str, tmp_path: Path
) -> None:
    """The cap bounds the whole encoded answer, header and counts included, and the next page
    starts where the last one stopped, in both directions."""
    cap = 2048
    with _store(kind, tmp_path) as store:
        store.append(*[_record(n) for n in range(30)])
        rows = [(seq, n, _entry(n, cause="é" * (37 * n))) for seq in range(1, 31) for n in range(3)]
        _cover(store, rows, [Interval(1, 30, True)])
        for backward in (False, True):
            seen: list[tuple[int, int]] = []
            cursor = None
            while True:
                query = TransitionQuery(cursor, backward, 1000, upto=30, counts=("X", "Y"))
                answer = runner_transitions.answer(
                    store, Request(query, "h"), header=_HEADER, byte_cap=cap
                )
                assert _encoded(answer) <= cap
                assert answer["entries"] and not any("cut" in e for e in answer["entries"])
                keys = [(e["seq"], e["n"]) for e in answer["entries"]]
                seen.extend(reversed(keys) if backward else keys)
                if not answer["more"]:
                    break
                cursor = tuple(answer["next"])
            expected = [(seq, n) for seq, n, _ in rows]
            assert seen == (list(reversed(expected)) if backward else expected)


def test_a_page_that_stops_exactly_at_the_cap_holds_every_entry_that_fits() -> None:
    """An answer whose encoding is the cap exactly is served whole; one byte less and its last
    entry waits for the next page."""
    store = MemoryStore()
    store.append(_record(0), transitions=[_entry(n, cause="x" * (10 * n)) for n in range(8)])
    query = TransitionQuery(None, False, 100, upto=1)
    request = Request(query, None)
    whole = runner_transitions.answer(store, request, header=_HEADER, byte_cap=10**6)
    assert len(whole["entries"]) == 8 and not whole["more"]
    exact = _encoded(whole)
    at_cap = runner_transitions.answer(store, request, header=_HEADER, byte_cap=exact)
    assert at_cap == whole and _encoded(at_cap) == exact
    short = runner_transitions.answer(store, request, header=_HEADER, byte_cap=exact - 1)
    assert len(short["entries"]) == 7 and short["more"] and short["next"] == [1, 6]
    assert _encoded(short) <= exact - 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_row_larger_than_the_cap_comes_alone_with_its_text_cut(kind: str, tmp_path: Path) -> None:
    """An accepted status event can carry a cause longer than a whole answer may be: the entry
    comes alone, its longest text cut to fit and marked `cut`, and the cursor passes it."""
    cap = runner_transitions.REPLY_BYTES
    huge = _entry(0, cause="refused: " + "q" * (cap + 31_785))
    with _store(kind, tmp_path) as store:
        store.append(_record(0), transitions=[huge, _entry(1)])
        query = TransitionQuery(None, False, 10, upto=1)
        first = runner_transitions.answer(store, Request(query, None), header=_HEADER)
        assert _encoded(first) <= cap
        [entry] = first["entries"]
        assert (entry["seq"], entry["n"], entry["cut"]) == (1, 0, True)
        assert huge["cause"].startswith(entry["cause"]) and len(entry["cause"]) > cap - 1000
        assert first["more"] and first["next"] == [1, 0]
        then = TransitionQuery((1, 0), False, 10, upto=1)
        second = runner_transitions.answer(store, Request(then, None), header=_HEADER)
        assert [(e["seq"], e["n"]) for e in second["entries"]] == [(1, 1)]
        assert "cut" not in second["entries"][0] and not second["more"]


def test_an_entry_whose_every_text_must_be_cut_has_each_cut_in_turn() -> None:
    """A long cause cut to nothing still leaves too much: the next longest text is cut too."""
    store = MemoryStore()
    store.append(_record(0), transitions=[_entry(0, cause="c" * 900, job="j" * 700)])
    query = TransitionQuery(None, False, 10, upto=1)
    bare = _encoded(
        runner_transitions.answer(store, Request(query, None), header={}, byte_cap=10**6)
    )
    answer = runner_transitions.answer(store, Request(query, None), header={}, byte_cap=bare - 1200)
    [entry] = answer["entries"]
    assert entry["cause"] == "" and 0 < len(entry["job"]) < 700 and entry["cut"] is True
    assert _encoded(answer) <= bare - 1200
    with pytest.raises(runner_transitions.AnswerTooLarge, match="one entry"):
        runner_transitions.answer(store, Request(query, None), header={}, byte_cap=bare - 1700)


def test_counts_past_the_cap_are_refused_naming_them() -> None:
    store = MemoryStore()
    store.append(_record(0), transitions=[_entry(n, job=f"job{n}") for n in range(300)])
    query = TransitionQuery(None, False, 10, upto=1, counts=("INACTIVE->STARTING",))
    with pytest.raises(runner_transitions.AnswerTooLarge, match="name fewer transitions"):
        runner_transitions.answer(store, Request(query, None), header={}, byte_cap=4096)


@pytest.mark.parametrize("kind", KINDS)
def test_a_read_decodes_rows_only_up_to_the_cap(kind: str, tmp_path: Path) -> None:
    """The read stops at the row that takes the stored bytes past the cap, says a row is left
    beyond it, and the answer's `more` says so too."""
    with _store(kind, tmp_path) as store:
        store.append(_record(0), transitions=[_entry(n, cause="c" * 500) for n in range(50)])
        read = store.read_transitions(TransitionQuery(None, False, 5000, upto=1, byte_cap=2000))
        assert 3 <= len(read.rows) <= 5 and read.beyond
        whole = store.read_transitions(TransitionQuery(None, False, 5000, upto=1, byte_cap=10**6))
        assert len(whole.rows) == 50 and not whole.beyond


@pytest.mark.parametrize("kind", KINDS)
def test_counts_by_job_equal_a_count_over_the_rows(kind: str, tmp_path: Path) -> None:
    with _store(kind, tmp_path) as store:
        store.append(*[_record(n) for n in range(20)])
        jobs, names = ["a", "b", "c"], ["RUNNING->FAILURE", "EVENT_IGNORED", "OTHER"]
        rows = [
            (seq, n, _entry(n, job=jobs[(seq * n) % 3], transition=names[(seq + n) % 3]))
            for seq in range(1, 21)
            for n in range(seq % 5)
        ]
        _cover(store, rows, [Interval(1, 20, True)])
        wanted = ("RUNNING->FAILURE", "EVENT_IGNORED", "NEVER")
        read = store.read_transitions(TransitionQuery(None, True, 1, upto=20, counts=wanted))
        tally = Counter(
            (e["job"], e["transition"]) for _, _, e in rows if e["transition"] in wanted
        )
        expected: dict[str, dict[str, int]] = {}
        for (job, transition), count in tally.items():
            expected.setdefault(job, {})[transition] = count
        assert read.counts == expected
        assert (
            store.read_transitions(TransitionQuery(None, True, 1, upto=20, counts=())).counts == {}
        )


@pytest.mark.parametrize("kind", KINDS)
def test_a_read_sees_nothing_past_its_watermark(kind: str, tmp_path: Path) -> None:
    with _store(kind, tmp_path) as store:
        for n in range(4):
            store.append(_record(n), transitions=[_entry(n, job="a" if n % 2 else "b")])
        for backward in (False, True):
            for cursor in (None, (4, 0), (2, 0)):
                read = store.read_transitions(TransitionQuery(cursor, backward, 10, upto=2))
                assert all(seq <= 2 for seq, _, _ in read.rows)
        forward = store.read_transitions(TransitionQuery((1, 0), False, 10, upto=3, job="b"))
        assert [seq for seq, _, _ in forward.rows] == [3]
        both = store.read_transitions(
            TransitionQuery(None, True, 10, upto=4, job="a", transition="INACTIVE->STARTING")
        )
        assert [seq for seq, _, _ in both.rows] == [4, 2]
        other = store.read_transitions(TransitionQuery(None, False, 10, upto=4, transition="X"))
        assert other.rows == []


def test_an_admission_commits_while_a_read_transaction_is_open(tmp_path: Path) -> None:
    """WAL: a reader's open transaction holds no lock the writer waits on, and the writer waits
    for none (`busy_timeout=0`), so the append below would raise if it had to wait."""
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as writer:
        writer.append(_record(0), transitions=[_entry(0)])
        with closing(SqliteStore(path, read_only=True)) as reader:
            reader._db.execute("BEGIN")
            assert reader._db.execute("SELECT COUNT(*) FROM transitions").fetchone() == (1,)
            writer.append(_record(1), transitions=[_entry(1)])
            assert reader._db.execute("SELECT COUNT(*) FROM transitions").fetchone() == (1,)
            reader._db.execute("COMMIT")
            assert len(_everything(reader)) == 2


# ------------------------------------------------------------------ the verb


def test_the_verb_is_a_read_tier_query() -> None:
    assert REQUIRED_TIER["transitions"] is Tier.READ


@pytest.mark.parametrize(
    ("field", "value", "names"),
    [
        ("cursor", [1], "cursor"),
        ("cursor", [1, -1], "cursor"),
        ("cursor", [True, 0], "cursor"),
        ("cursor", "1,0", "cursor"),
        ("direction", "up", "direction"),
        ("limit", -1, "limit"),
        ("limit", 1.5, "limit"),
        ("job", 3, "job"),
        ("transition", "\ud800", "transition"),
        ("history", [], "history"),
        ("counts", "T1", "counts"),
        ("counts", ["a"] * 65, "counts"),
        ("counts", [1], "counts"),
    ],
)
def test_a_malformed_request_is_refused_naming_its_field(
    field: str, value: Any, names: str
) -> None:
    refused = parse({field: value}, upto=3)
    assert isinstance(refused, str) and refused.startswith(names)


def test_the_request_defaults_and_its_limit_cap() -> None:
    parsed = parse({"cursor": [2, 1], "direction": "backward", "limit": 10**6}, upto=9)
    assert isinstance(parsed, Request)
    assert parsed.query == TransitionQuery((2, 1), True, runner_transitions.LIMIT_MAX, upto=9)
    default = parse({}, upto=9)
    assert isinstance(default, Request)
    assert default.query == TransitionQuery(None, False, runner_transitions.LIMIT_DEFAULT, upto=9)
    assert default.history is None


def test_an_empty_page_spans_from_its_cursor() -> None:
    """A page with no entries (limit 0, counts only) keeps the cursor it was given, and its
    span is the cursor's seq when entries remain beyond it."""
    store = MemoryStore()
    for n in range(3):
        store.append(_record(n), transitions=[_entry(n)])
    for backward, cursor in ((False, None), (True, None), (False, (1, 0)), (True, (3, 0))):
        query = TransitionQuery(cursor, backward, 0, upto=3, counts=("INACTIVE->STARTING",))
        answer = reply(store.read_transitions(query), Request(query, None))
        assert answer["entries"] == [] and answer["more"] is True
        assert answer["next"] == (None if cursor is None else list(cursor))
        assert answer["counts"] == {"j": {"INACTIVE->STARTING": 3}}
        assert len(answer["covered"]) == 1 and answer["gaps"] == []
    query = TransitionQuery((3, 0), False, 0, upto=3)
    answer = reply(store.read_transitions(query), Request(query, None))
    assert answer["more"] is False and answer["covered"] == [[3, 3]]


def test_an_answer_entry_carries_the_optional_facts_only_when_set() -> None:
    """The entry shape is `trace`'s: `start_source`, `run_number` and `refused_status` ride
    only when set, and `seq` and `n` are the row's key."""
    store = MemoryStore()
    refused = _entry(0, start_source="scheduler", run_number=3, refused_status="RUNNING")
    store.append(_record(0), transitions=[refused, _entry(1)])
    query = TransitionQuery(None, False, 10, upto=1)
    answer = reply(store.read_transitions(query), Request(query, None))
    assert answer["entries"] == [
        {
            "seq": 1,
            "n": 0,
            "at": "2026-07-01T08:00:00",  # as `trace` writes it
            "job": "j",
            "transition": "INACTIVE->STARTING",
            "cause": "cause 0",
            "start_source": "scheduler",
            "run_number": 3,
            "refused_status": "RUNNING",
        },
        {
            "seq": 1,
            "n": 1,
            "at": "2026-07-01T08:00:01",
            "job": "j",
            "transition": "INACTIVE->STARTING",
            "cause": "cause 1",
        },
    ]


def test_coverage_and_gaps_are_reported_over_the_span_of_the_page() -> None:
    store = MemoryStore()
    for n in range(10):
        store.append(_record(n), transitions=[_entry(n)])
    store._coverage = [Interval(1, 3, True), Interval(4, 6, False), Interval(9, 10, True)]
    query = TransitionQuery((2, 0), False, 3, upto=10)
    answer = reply(store.read_transitions(query), Request(query, None))
    assert [e["seq"] for e in answer["entries"]] == [3, 4, 5]
    assert answer["covered"] == [[2, 3]] and answer["gaps"] == [[4, 5]]
    query = TransitionQuery(None, False, 100, upto=10)
    answer = reply(store.read_transitions(query), Request(query, None))
    assert answer["covered"] == [[1, 3], [9, 10]] and answer["gaps"] == [[4, 8]]
    assert answer["history"] is None


def _server(engine: Engine, root: Path) -> ControlServer:
    return ControlServer(engine, root / "control.sock")


@pytest.mark.parametrize("rehearsal", [False, True])
def test_the_verb_answers_a_page_with_the_index_its_read_saw(
    tmp_path: Path, rehearsal: bool
) -> None:
    """The answer carries the entries in key order, the next cursor, the coverage, the history
    and the watermark, and its read header is the one taken when the read began. A rehearsal's
    store is read in process."""
    run_root = None if rehearsal else tmp_path / "run"

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, int]:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (300.0, 0)}, default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        server = _server(engine, tmp_path)
        answer = await server._respond(
            {"v": 5, "cmd": "transitions", "direction": "backward", "limit": 2, "counts": ["X"]}
        )
        refused = await server._respond({"v": 5, "cmd": "transitions", "limit": "all"})
        assert engine.journal is not None
        watermark, applied = engine.journal.last_seq, engine.frontiers.applied_index
        await _close(engine)
        return answer, refused, watermark, applied

    answer, refused, watermark, applied = asyncio.run(scenario())
    assert refused["ok"] is False and refused["code"] == "invalid_argument"
    assert answer["ok"] is True and answer["applied_index"] == applied
    assert answer["watermark"] == watermark
    keys = [(e["seq"], e["n"]) for e in answer["entries"]]
    assert len(keys) == 2 and keys == sorted(keys) and answer["more"] is True
    assert answer["next"] == list(keys[0])
    assert answer["counts"] == {} and isinstance(answer["history"], str)
    assert answer["gaps"] == [] and answer["covered"][-1][1] == watermark
    assert (answer["floor"], answer["expired"], answer["pruned"]) == (1, False, [])
    assert (answer["counts_covered"], answer["counts_gaps"]) == ([[1, watermark]], [])
    assert set(answer) == {
        "ok",
        "entries",
        "next",
        "more",
        "floor",
        "expired",
        "pruned",
        "covered",
        "gaps",
        "watermark",
        "history",
        "counts",
        "counts_covered",
        "counts_gaps",
        "baseline_id",
        "applied_index",
    }


def test_the_verb_without_a_store_is_refused() -> None:
    engine = Engine(lower_source(_JIL), clock=VirtualClock(start=T0), adapters={})
    answer = asyncio.run(_server(engine, Path("/nonexistent"))._respond({"cmd": "transitions"}))
    assert answer["ok"] is False and answer["code"] == "no_journal"


def test_a_read_in_flight_does_not_hold_up_an_admission(tmp_path: Path) -> None:
    """The read runs off the engine's loop: while it is held open on its own connection, the
    loop admits a control input and commits its decision; the read then answers as of the
    point it began, its header naming the index applied then."""
    from dsl41 import runner_control

    run_root = tmp_path / "run"
    entered, release = threading.Event(), threading.Event()
    real = runner_control.transitions_answer

    def held(store: SqliteStore, request: Request, **kwargs: Any) -> Any:
        store._db.execute("BEGIN")
        store._db.execute("SELECT COUNT(*) FROM transitions").fetchone()
        entered.set()
        assert release.wait(30)
        store._db.execute("COMMIT")
        return real(store, request, **kwargs)

    async def scenario() -> tuple[dict[str, Any], int, int]:
        engine = _start(run_root)
        await engine.run_until_quiescent(T0)
        server = _server(engine, tmp_path)
        before = engine.frontiers.applied_index
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(runner_control, "transitions_answer", held)
            reading = asyncio.ensure_future(server._respond({"cmd": "transitions"}))
            await asyncio.to_thread(entered.wait, 30)
            decided = engine.submit(
                _ev("STARTJOB", 0, job="j"),
                Envelope(request_id="r1", expect={"job:j": engine.oracle.store.revision("job:j")}),
            )
            await engine.run_until_quiescent(T0 + timedelta(seconds=1))
            result = await decided
            assert result.decision == "applied" and not reading.done()
            release.set()
            answer = await reading
        after = engine.frontiers.applied_index
        await _close(engine)
        return answer, before, after

    answer, before, after = asyncio.run(scenario())
    assert after > before
    assert answer["applied_index"] == before
    assert all(e["job"] != "j" for e in answer["entries"])


def test_the_verb_over_the_socket(short_root: Path) -> None:
    """End to end through the socket, with `history` answered by `continuous_through`."""
    run_root = short_root / "run"

    async def scenario() -> dict[str, Any]:
        engine = _start(run_root)
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        server = _server(engine, run_root)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(run_root / "control.sock"))
            request = {"v": 5, "cmd": "transitions", "history": "never", "job": "j"}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            answer: dict[str, Any] = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()
            await _close(engine)
        return answer

    answer = asyncio.run(scenario())
    assert answer["ok"] is True and answer["continuous_through"] == 0
    assert answer["entries"] and all(e["job"] == "j" for e in answer["entries"])


def test_a_cancelled_read_is_interrupted_and_joined_before_the_server_closes(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read whose statement runs inside an open read transaction when the server closes is
    interrupted there, and its thread has ended before `close` returns and the store closes."""
    from dsl41 import runner_control

    run_root = short_root / "run"
    entered = threading.Event()

    def endless(store: SqliteStore, request: Request, **kwargs: Any) -> Any:
        db = store._db
        db.create_function("entered", 0, lambda: entered.set() or 1)
        db.execute("BEGIN")
        db.execute(
            "WITH RECURSIVE c(x) AS (SELECT entered() UNION ALL SELECT x + 1 FROM c)"
            " SELECT COUNT(*) FROM c"
        ).fetchone()  # runs until interrupted
        raise AssertionError("an endless statement ended")

    def readers() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "dsl41-store-read"]

    async def scenario() -> tuple[bool, BaseException | None]:
        engine = _start(run_root)
        await engine.run_until_quiescent(T0)
        server = _server(engine, run_root)
        await server.start()
        monkeypatch.setattr(runner_control, "transitions_answer", endless)
        reader, writer = await asyncio.open_unix_connection(str(run_root / "control.sock"))
        writer.write(json.dumps({"v": 5, "cmd": "transitions"}).encode() + b"\n")
        await writer.drain()
        await asyncio.to_thread(entered.wait, 30)
        assert len(readers()) == 1
        await server.close()
        alive = bool(readers())
        await _close(engine)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return alive, None

    alive, _ = asyncio.run(scenario())
    assert not alive


def _filled_store(path: Path) -> None:
    with closing(SqliteStore(path)) as store:
        for n in range(4):
            store.append(_record(n), transitions=[_entry(n)])


def test_a_read_stopped_before_it_began_reads_nothing(tmp_path: Path) -> None:
    """A read stopped before its thread took the store sees the stop at its first step."""
    from dsl41 import runner_control

    path = tmp_path / "s.db"
    _filled_store(path)
    query = TransitionQuery(None, False, 10, upto=4)
    seen: list[bool] = []

    def work(store: SqliteStore, stopped: Any) -> dict[str, Any]:
        seen.append(stopped())
        return runner_transitions.answer(store, Request(query, None), header={}, stopped=stopped)

    read = runner_control._StoreRead(path, work, deadline_s=5.0)
    read.interrupt()

    async def scenario() -> BaseException | None:
        await read.stop()  # nothing started: nothing to wait for
        try:
            await read.run()
        except Exception as exc:  # noqa: BLE001 -- the outcome is the assertion
            return exc
        return None

    raised = asyncio.run(scenario())
    assert isinstance(raised, sqlite3.OperationalError) and seen == [True]


def test_a_stop_between_two_statements_ends_the_read_at_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's order: the read pauses between two statements, the stop comes (no statement
    runs for SQLite to interrupt), the read goes on, and its next step refuses: the histories
    statement never runs and the answer is never sized."""
    from dsl41 import runner_control

    path = tmp_path / "s.db"
    _filled_store(path)
    paused, resume = threading.Event(), threading.Event()
    statements: list[str] = []
    real_coverage = SqliteStore.transitions_coverage

    def pausing(self: SqliteStore) -> list[Interval]:
        found = real_coverage(self)
        if threading.current_thread().name == "dsl41-store-read":
            paused.set()
            assert resume.wait(30)
        return found

    sized: list[bool] = []
    monkeypatch.setattr(SqliteStore, "transitions_coverage", pausing)
    monkeypatch.setattr(runner_transitions, "fit", lambda *a, **k: sized.append(True) or {})
    query = TransitionQuery(None, False, 10, upto=4, counts=("X",))

    def work(store: SqliteStore, stopped: Any) -> dict[str, Any]:
        store._db.set_trace_callback(statements.append)
        return runner_transitions.answer(store, Request(query, None), header={}, stopped=stopped)

    read = runner_control._StoreRead(path, work, deadline_s=30.0)

    async def scenario() -> BaseException | None:
        reading = asyncio.ensure_future(read.run())
        await asyncio.to_thread(paused.wait, 30)
        stopping = asyncio.ensure_future(read.stop())
        while not read.stopped():
            await asyncio.sleep(0)
        resume.set()
        await stopping
        outcome = await asyncio.gather(reading, return_exceptions=True)
        return outcome[0] if isinstance(outcome[0], BaseException) else None

    raised = asyncio.run(scenario())
    assert isinstance(raised, sqlite3.OperationalError) and "interrupted" in str(raised)
    assert any("COUNT(*)" in statement for statement in statements)
    assert not any("transitions_history" in statement for statement in statements)
    assert sized == []


def test_a_read_stopped_after_its_statements_is_never_sized() -> None:
    store = MemoryStore()
    store.append(_record(0), transitions=[_entry(0)])
    query = TransitionQuery(None, False, 10, upto=1)
    with pytest.raises(runner_transitions.Stopped, match="before its answer was sized"):
        runner_transitions.answer(store, Request(query, None), header={}, stopped=lambda: True)


def test_a_read_that_does_not_stop_in_time_is_reported_and_left(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A stuck read does not hold up a shutdown: past the deadline `stop` reports the read it
    leaves and returns."""
    from dsl41 import runner_control

    path = tmp_path / "s.db"
    _filled_store(path)
    entered, release = threading.Event(), threading.Event()

    def stuck(store: SqliteStore, stopped: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(30)  # Python code, which no SQLite interrupt reaches
        return {}

    read = runner_control._StoreRead(path, stuck, deadline_s=0.0)

    async def scenario() -> None:
        reading = asyncio.ensure_future(read.run())
        await asyncio.to_thread(entered.wait, 30)
        await read.stop()
        assert "did not stop within 0.0s; the shutdown goes on" in capfd.readouterr().err
        release.set()
        await reading  # the thread ends and the read answers after all

    asyncio.run(scenario())
    assert not [t for t in threading.enumerate() if t.name == "dsl41-store-read"]


@pytest.mark.parametrize("rehearsal", [False, True])
def test_a_cursor_outside_the_stores_integers_is_an_invalid_argument(
    tmp_path: Path, rehearsal: bool
) -> None:
    """Both stores answer `invalid_argument`, never `internal_error`."""

    async def scenario() -> list[dict[str, Any]]:
        engine = start_run(
            lower_source(_JIL),
            None if rehearsal else tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        server = _server(engine, tmp_path)
        answers = [
            await server._respond({"cmd": "transitions", "cursor": cursor})
            for cursor in ([2**63, 0], [0, 2**63])
        ]
        fine = await server._respond({"cmd": "transitions", "cursor": [2**63 - 1, 2**63 - 1]})
        await _close(engine)
        return [*answers, fine]

    *refused, fine = asyncio.run(scenario())
    assert [a["code"] for a in refused] == ["invalid_argument", "invalid_argument"]
    assert all("cursor" in a["error"] for a in refused)
    assert fine["ok"] is True and fine["entries"] == []


# ------------------------------------------------------------------ pruning under a reader


@pytest.mark.parametrize("kind", KINDS)
def test_a_prune_between_pages_is_reported_not_skipped(kind: str, tmp_path: Path) -> None:
    """The same request at the same watermark before and after a prune below seq 6: before, it
    answers seqs 2 and 3; after, 6 and 7, with the floor the read saw, its cursor expired and
    seqs 1 to 5 listed as pruned."""
    with _store(kind, tmp_path) as store:
        for n in range(10):
            store.append(_record(n), transitions=[_entry(n)])
        query = TransitionQuery((1, 0), False, 2, upto=8, counts=("INACTIVE->STARTING",))
        before = runner_transitions.answer(store, Request(query, None), header={})
        assert [e["seq"] for e in before["entries"]] == [2, 3]
        assert (before["floor"], before["expired"], before["pruned"]) == (1, False, [])
        assert before["counts"] == {"j": {"INACTIVE->STARTING": 8}}
        store.put_snapshot(6, STATE_MACHINE_VERSION, b"{}")
        prune(store, 6, T0 + timedelta(days=1))
        after = runner_transitions.answer(store, Request(query, None), header={})
        assert [e["seq"] for e in after["entries"]] == [6, 7]
        assert (after["floor"], after["expired"], after["pruned"]) == (6, True, [[1, 5]])
        assert (after["covered"], after["gaps"]) == ([[6, 7]], [])
        assert after["counts"] == {"j": {"INACTIVE->STARTING": 3}}
        assert (after["counts_covered"], after["counts_gaps"]) == ([[6, 8]], [])
        back = TransitionQuery((3, 0), True, 2, upto=8)
        older = runner_transitions.answer(store, Request(back, None), header={})
        assert older["entries"] == [] and older["expired"] and older["pruned"] == [[1, 3]]


@pytest.mark.parametrize("kind", KINDS)
def test_a_cursor_past_the_watermark_reports_nothing_past_it(kind: str, tmp_path: Path) -> None:
    """A backward cursor at [100, 0] with the watermark at 8 reports no gap in the future: every
    seq the answer names is at or below the watermark."""
    with _store(kind, tmp_path) as store:
        for n in range(10):
            store.append(_record(n), transitions=[_entry(n)])
        back = TransitionQuery((100, 0), True, 3, upto=8)
        answer = runner_transitions.answer(store, Request(back, None), header={})
        assert [e["seq"] for e in answer["entries"]] == [6, 7, 8]
        assert (answer["covered"], answer["gaps"]) == ([[6, 8]], [])
        forward = TransitionQuery((100, 0), False, 3, upto=8)
        ahead = runner_transitions.answer(store, Request(forward, None), header={})
        assert ahead["entries"] == [] and (ahead["covered"], ahead["gaps"]) == ([], [])
        # a cursor past the watermark goes on from the watermark, never from above it
        assert not ahead["expired"] and ahead["next"] == [8, CURSOR_MAX]
        stale = TransitionQuery((100, 1), False, 3, upto=2)
        answer = runner_transitions.answer(store, Request(stale, None), header={})
        assert answer["entries"] == [] and answer["next"] == [2, CURSOR_MAX]
        then = TransitionQuery(tuple(answer["next"]), False, 3, upto=4)  # the store moved on
        resumed = runner_transitions.answer(store, Request(then, None), header={})
        assert [e["seq"] for e in resumed["entries"]] == [3, 4]


def test_counts_come_with_the_coverage_of_the_whole_counted_range() -> None:
    """A page of the newest entry still tells the gap the counts were taken across."""
    store = MemoryStore()
    for n in range(10):
        store.append(_record(n), transitions=[_entry(n)])
    store.cover_transitions([Interval(2, 4, False)])
    query = TransitionQuery(None, True, 1, upto=10, counts=("INACTIVE->STARTING",))
    answer = runner_transitions.answer(store, Request(query, None), header={})
    assert (answer["covered"], answer["gaps"]) == ([[10, 10]], [])
    assert answer["counts_covered"] == [[1, 1], [5, 10]]
    assert answer["counts_gaps"] == [[2, 4]]


def test_a_forward_cursor_before_the_first_seq_is_not_expired() -> None:
    """Seq 0 names no record: a cursor there skips nothing, so nothing expired."""
    store = MemoryStore()
    for n in range(3):
        store.append(_record(n), transitions=[_entry(n)])
    query = TransitionQuery((0, 0), False, 10, upto=3)
    answer = runner_transitions.answer(store, Request(query, None), header={})
    assert (answer["floor"], answer["expired"], answer["pruned"]) == (1, False, [])
    assert [e["seq"] for e in answer["entries"]] == [1, 2, 3]
    back = TransitionQuery((0, 5), True, 10, upto=3)
    older = runner_transitions.answer(store, Request(back, None), header={})
    assert (older["entries"], older["expired"], older["pruned"]) == ([], False, [])


def test_a_page_whose_first_estimate_falls_short_still_fills_to_the_cap() -> None:
    """The first estimate counts the request's long cursor; the answer's own `next` is shorter,
    so the page grows to every entry the cap holds."""
    store = MemoryStore()
    store.append(_record(0), transitions=[_entry(n) for n in range(6)])
    query = TransitionQuery((0, 10**15), False, 100, upto=1)
    whole = runner_transitions.answer(store, Request(query, None), header={}, byte_cap=10**6)
    exact = _encoded(whole)
    fitted = runner_transitions.answer(store, Request(query, None), header={}, byte_cap=exact)
    assert fitted == whole and len(fitted["entries"]) == 6


def test_the_verb_refuses_counts_its_cap_cannot_hold(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        engine = _start(tmp_path / "run")
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        server = _server(engine, tmp_path)
        server.transitions_bytes = 300
        answer = await server._respond({"cmd": "transitions", "counts": ["X"] * 40})
        await _close(engine)
        return answer

    answer = asyncio.run(scenario())
    assert answer["code"] == "invalid_argument" and "counts" in answer["error"]


def test_closing_the_server_stops_a_read_no_connection_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read awaited outside a connection handler is interrupted and joined by `close` too;
    its caller gets the interrupted statement's error."""
    from dsl41 import runner_control

    entered = threading.Event()

    def endless(store: SqliteStore, request: Request, **kwargs: Any) -> Any:
        store._db.create_function("entered", 0, lambda: entered.set() or 1)
        store._db.execute(
            "WITH RECURSIVE c(x) AS (SELECT entered() UNION ALL SELECT x + 1 FROM c)"
            " SELECT COUNT(*) FROM c"
        ).fetchone()
        raise AssertionError("an endless statement ended")

    async def scenario() -> tuple[bool, BaseException | None]:
        engine = _start(tmp_path / "run")
        await engine.run_until_quiescent(T0)
        server = _server(engine, tmp_path)
        monkeypatch.setattr(runner_control, "transitions_answer", endless)
        reading = asyncio.ensure_future(server._respond({"cmd": "transitions"}))
        await asyncio.to_thread(entered.wait, 30)
        await server.close()
        alive = any(t.name == "dsl41-store-read" for t in threading.enumerate())
        error = await asyncio.gather(reading, return_exceptions=True)
        await _close(engine)
        return alive, error[0] if isinstance(error[0], BaseException) else None

    alive, error = asyncio.run(scenario())
    assert not alive
    assert isinstance(error, sqlite3.OperationalError) and "interrupt" in str(error)


# ------------------------------------------------------------------ the CLI


def _query(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[Any, list[dict[str, Any]]]:
    from typer.testing import CliRunner

    from dsl41 import cli_control
    from dsl41.cli import app

    sent: list[dict[str, Any]] = []

    def roundtrip(path: Path, request: dict[str, Any]) -> dict[str, Any]:
        sent.append(request)
        return {"ok": True, "entries": []}

    monkeypatch.setattr(cli_control, "_control_roundtrip", roundtrip)
    result = CliRunner().invoke(app, ["query", *args, "--socket", "/nowhere/control.sock"])
    return result, sent


def test_query_transitions_pages_newest_first_with_its_own_options(
    monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    result, sent = _query(monkeypatch, "transitions")
    assert result.exit_code == 0, result.output
    assert sent == [{"cmd": "transitions", "direction": "backward", "limit": 50}]
    result, sent = _query(
        monkeypatch,
        "transitions",
        "--job",
        "j",
        "--transition",
        "RUNNING->FAILURE",
        "--limit",
        "7",
        "--cursor",
        "41:2",
        "--forward",
    )
    assert result.exit_code == 0, result.output
    assert sent == [
        {
            "cmd": "transitions",
            "job": "j",
            "direction": "forward",
            "limit": 7,
            "transition": "RUNNING->FAILURE",
            "cursor": [41, 2],
        }
    ]


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("transitions", "--since", "3"), "--since applies to trace and subscribe"),
        (("transitions", "--name", "G"), "--name applies to global and globals"),
        (("transitions", "--cursor", "41"), "--cursor '41' is not SEQ:N"),
        (("transitions", "--cursor", "4²:1"), "is not SEQ:N"),
        (("transitions", "j"), "JOB applies to status only"),
        (("trace", "--limit", "3"), "--limit applies to transitions and history"),
        (("trace", "--forward"), "--forward applies to transitions only"),
        (("status", "--transition", "X"), "--transition applies to transitions only"),
        (("trace", "--cursor", "1:0"), "--cursor applies to transitions and history"),
    ],
)
def test_query_refuses_by_name_every_option_a_query_does_not_take(
    monkeypatch: pytest.MonkeyPatch, cli_env: None, arguments: tuple[str, ...], error: str
) -> None:
    result, sent = _query(monkeypatch, *arguments)
    assert result.exit_code == 2 and error in result.output
    assert sent == []


# ------------------------------------------------------------------ compatibility


def test_a_store_this_build_wrote_opens_and_appends_under_the_previous_build(
    tmp_path: Path,
) -> None:
    """The previous build's store code opens a root this build wrote, appends to it, snapshots
    and prunes it; this build then reopens it and covers what it appended."""
    old = _previous_store_module()
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    path = run_root / STORE_NAME
    covered = _coverage(run_root)
    at = _latest(run_root)
    with closing(old.SqliteStore(path)) as previous:
        body = {"event": "snapshot", "at": at.isoformat(), "seq": 0}
        last = previous.append(old.Record(at, "engine", None, None, body))
        assert [seq for seq, _ in previous.read_from(last - 1)] == [last]
        assert previous.prune_before(1, at) == 0
    assert _coverage(run_root) == covered
    with closing(SqliteStore(path)) as store:
        assert begin(store, where="test") is None
        assert store.transitions_coverage()[-1].last == last


def test_the_previous_store_copy_is_the_previous_builds() -> None:
    """The copy keeps the previous build's table set, and the one this build adds is new."""
    old = _previous_store_module()
    assert not any("transitions" in statement for statement in old._SCHEMA)
    assert old.SCHEMA_VERSION == 1


def test_a_journal_append_carries_its_rows_to_the_store() -> None:
    journal = Journal(MemoryStore())
    entry = TraceEntry(at=T0, job="j", transition="A->B", cause="c")
    journal.append(_record(0), transitions=[entry])
    assert [TraceEntry.model_validate(e) for _, _, e in _everything(journal.store)] == [entry]
