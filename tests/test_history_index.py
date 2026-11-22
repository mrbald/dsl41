"""The history index (DL-539): `job_runs` and `job_records` commit with the records they name,
`history_coverage` says which seqs they cover, the start settles it and pruning trims it, as
the `transitions` rows do. `transitions_check` compares each root's index with its replay and
with an independent scan of its journal; the named scenarios below assert it compared rows.

The scenarios run on a virtual clock, except a retry and an update's replacement, which need
the wrapper and wait on a condition, never on a fixed delay.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import transitions_check
from test_run_openings import _LEAF, _Root, _at, _ev
from test_runner_journal import _applied, _attempt
from test_transitions import _start as _start_engine
from test_transitions import (
    ROOT,
    T0,
    _as_previous_build,
    _ask,
    _commits,
    _incarnation,
    _last,
    _latest,
    _previous_store_module,
    _record,
    _recovered,
)
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import runner_startup
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_history import read_run_root
from dsl41.runner_journal import STORE_NAME, Journal, index_rows
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_store import (
    Interval,
    MemoryStore,
    Record,
    RecordOwner,
    RunOpening,
    SqliteStore,
    Store,
    StoreError,
)
from dsl41.runner_transitions import settle_history

KINDS = ["sqlite", "memory"]

#: The bound on a whole scenario with real processes, so a loop that ends early cannot leave
#: one waiting; each wait inside it has its own shorter deadline.
_WHOLE_S = 120.0

type Runs = list[tuple[Any, ...]]


def _index(run_root: Path) -> tuple[Runs, Runs]:
    """The `job_runs` rows, and the `job_records` rows each with its record's kind."""
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        runs = [
            tuple(row)
            for row in store._db.execute(
                "SELECT seq, n, job, run_number, rev FROM job_runs ORDER BY seq, n"
            )
        ]
        records = [
            tuple(row)
            for row in store._db.execute(
                "SELECT r.seq, j.kind, r.job, r.run_number FROM job_records r"
                " JOIN journal j USING (seq) ORDER BY r.seq"
            )
        ]
    return runs, records


def _history_coverage(run_root: Path) -> list[Interval]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return store.history_coverage()


def _kinds(records: Runs, *kinds: str) -> list[tuple[Any, ...]]:
    return [(kind, job, run) for _, kind, job, run in records if kind in kinds]


def _rows_of(store: Store) -> tuple[Runs, Runs]:
    """The index's rows of a store a test built: `(seq, n, job, run_number, rev)` and
    `(seq, job, run_number)`."""
    if isinstance(store, SqliteStore):
        return (
            [tuple(r) for r in store._db.execute("SELECT * FROM job_runs ORDER BY seq, n")],
            [tuple(r) for r in store._db.execute("SELECT * FROM job_records ORDER BY seq")],
        )
    assert isinstance(store, MemoryStore)
    return list(store._job_runs), list(store._job_records)


@contextlib.contextmanager
def _store(kind: str, directory: Path) -> Iterator[Store]:
    store: Store = SqliteStore(directory / "s.db") if kind == "sqlite" else MemoryStore()
    try:
        yield store
    finally:
        store.close()


def _indexed(store: Store, n: int) -> int:
    """One record with an opening and an owner, both naming run `n + 1` of `j`."""
    return store.append(
        _record(n),
        openings=[RunOpening(0, "j", n + 1, 1)],
        owners=[RecordOwner(0, "j", n + 1)],
    )


# ------------------------------------------------------------------ the named scenarios


def _retried(tmp_path: Path) -> None:
    """A retry: a command that exits 3 under `n_retrys: 1` has two dispatches of run 1."""
    from test_cmd_retries import serving, terminal
    from test_engine_stops import _jil, _start, _start_jobs, _until

    root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(root, _jil({"j": "exit 3"}, "n_retrys: 1\n"))
        async with asyncio.timeout(_WHOLE_S), serving(engine):
            _start_jobs(engine, "j")
            await _until(lambda: terminal(engine))

    asyncio.run(scenario())
    runs, records = _index(root)
    assert [(job, run) for _, _, job, run, _ in runs] == [("j", 1)]
    assert _kinds(records, "dispatch", "outcome") == [
        ("dispatch", "j", 1),
        ("outcome", "j", 1),
        ("dispatch", "j", 1),
        ("outcome", "j", 1),
    ]


def _replaced(tmp_path: Path) -> None:
    """An update stops a running attempt and dispatches its replacement in the same run."""
    from test_cmd_retries import serving
    from test_engine_stops import _spawned
    from test_update_wiring import Estate, _applied, _jil, _plan, _start, _start_jobs, _update

    root = tmp_path / "run"
    command = 'if [ "$DSL41_ATTEMPT" -le 0 ]; then exit 1; fi; exec sleep 30'
    estate = Estate(tmp_path, _jil({"j": command}, "n_retrys: 0\n"))

    async def scenario() -> None:
        engine = _start(root, estate)
        async with asyncio.timeout(_WHOLE_S), serving(engine):
            _start_jobs(engine, "j")
            await _spawned(root, "j", n=1)
            estate.write(_jil({"j": "exec sleep 30"}, "n_retrys: 2\n"))
            body = await _applied(await _update(engine, _plan(engine, estate)))
            assert body["stopped"] == [["j", "rerun"]]
            await _spawned(root, "j", n=2)

    asyncio.run(scenario())
    _, records = _index(root)
    assert _kinds(records, "dispatch", "outcome")[:3] == [
        ("dispatch", "j", 1),
        ("outcome", "j", 1),
        ("dispatch", "j", 1),
    ]


def _changed_status(tmp_path: Path) -> None:
    """An operator's CHANGE_STATUS names no run: its input and decision name the job alone."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))

    root, _ = _scenario(tmp_path, body)
    _, records = _index(root.run)
    assert ("input", "j", None) in _kinds(records, "input")
    assert ("decision", "j", None) in _kinds(records, "decision")


def _late_exit(tmp_path: Path) -> None:
    """An exit for a run that already ended is rejected; its input and decision name the run."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(2))
        payload = {"job": "j", "run_number": 1, "exit_code": 0}
        root.engine._enqueue(Event(at=_at(3), kind="STATUS", payload=payload), source="adapter")
        await root.engine.run_until_quiescent(_at(3))
        assert "already terminal" in root.engine.drops[-1][1]

    root, _ = _scenario(tmp_path, body, {("j", 1): (60.0, 0)})
    _, records = _index(root.run)
    assert _kinds(records, "input", "decision")[-2:] == [("input", "j", 1), ("decision", "j", 1)]


def _killed(tmp_path: Path) -> None:
    """KILLJOB ends the attempt: its outcome names the run of its dispatch."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "KILLJOB", job="j"))

    root, _ = _scenario(tmp_path, body)
    _, records = _index(root.run)
    assert _kinds(records, "dispatch", "outcome") == [("dispatch", "j", 1), ("outcome", "j", 1)]


def _reconciled(tmp_path: Path) -> None:
    """A restart ends an attempt the engine died with by its own completion."""
    root = tmp_path / "run"
    before = _recovered(root)
    _, records = _index(root)
    assert [(job, run) for seq, kind, job, run in records if kind == "input" and seq > before] == [
        ("k", 1)
    ]


def _readded(tmp_path: Path) -> None:
    """A job an update removed and a later one added back runs again under the next number."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(2))
        await root.update(3, "insert_job: k\njob_type: c\ncommand: x\n")
        await root.update(4, _LEAF.format(cmd="x"))
        await root.feed(_ev(5, "STARTJOB", job="j"))

    root, _ = _scenario(tmp_path, body, {("j", 1): (60.0, 0)})
    runs, _ = _index(root.run)
    assert [(job, run, rev) for _, _, job, run, rev in runs] == [("j", 1, 1), ("j", 2, 3)]


def _bypassed(tmp_path: Path) -> None:
    """An ON_NOEXEC bypass opens its run with its SUCCESS and dispatches nothing."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "KILLJOB", job="j"))
        await root.feed(_ev(2, "ON_NOEXEC", job="j"), _ev(3, "STARTJOB", job="j"))

    root, _ = _scenario(tmp_path, body)
    runs, records = _index(root.run)
    assert [(job, run) for _, _, job, run, _ in runs] == [("j", 1), ("j", 2)]
    assert _kinds(records, "dispatch") == [("dispatch", "j", 1)]


def _rev_between_runs(tmp_path: Path) -> None:
    """An update between two runs of one job: each run opens under the rev it ran."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(2))
        await root.update(3, _LEAF.format(cmd="y"))
        await root.feed(_ev(4, "STARTJOB", job="j"))

    root, _ = _scenario(tmp_path, body, {("j", 1): (60.0, 0)})
    runs, _ = _index(root.run)
    assert [(run, rev) for _, _, _, run, rev in runs] == [(1, 1), (2, 2)]


def _scenario(
    tmp_path: Path, body: Callable[[_Root], Any], script: dict[Any, Any] | None = None
) -> tuple[_Root, Any]:
    async def run() -> tuple[_Root, Any]:
        root = _Root(tmp_path, _LEAF.format(cmd="x"), script)
        try:
            found = await body(root)
        finally:
            await root.close()
        return root, found

    return asyncio.run(run())


_SCENARIOS: dict[str, Callable[[Path], None]] = {
    "a retry": _retried,
    "an update that replaces an attempt": _replaced,
    "an operator CHANGE_STATUS": _changed_status,
    "a rejected late exit": _late_exit,
    "KILLJOB": _killed,
    "a restart with a reconcile completion": _reconciled,
    "a job removed and added again": _readded,
    "an ON_NOEXEC bypass": _bypassed,
    "an update between two runs": _rev_between_runs,
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_the_index_of_each_named_scenario_equals_its_replay_and_its_scan(
    tmp_path: Path, name: str
) -> None:
    """Each scenario's index equals the replay's numbered entries and the journal scan
    (`transitions_check`), and the check compared rows of both tables."""
    before = dict(transitions_check.compared)
    _SCENARIOS[name](tmp_path)
    transitions_check.check_roots(tmp_path)
    assert transitions_check.compared["job_runs"] > before["job_runs"]
    assert transitions_check.compared["job_records"] > before["job_records"]


# ------------------------------------------------------------------ coverage


def test_a_fresh_root_is_covered_from_its_first_record(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    assert _history_coverage(run_root) == [Interval(1, _last(run_root), True)]


def test_a_root_an_earlier_build_wrote_is_a_gap_up_to_this_builds_first_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing fills rows for records an earlier build wrote: they are a recorded gap."""
    run_root = tmp_path / "run"
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(run_root, genesis=True, snapshots=1))
    last = _last(run_root)
    asyncio.run(_incarnation(run_root))
    assert _history_coverage(run_root) == [
        Interval(1, last, False),
        Interval(last + 1, _last(run_root), True),
    ]
    runs, records = _index(run_root)
    assert runs and records and all(row[0] > last for row in [*runs, *records])


def _transitions_store_module() -> ModuleType:
    """The store code of the build before this change (commit fa4c533), which commits the
    `transitions` rows and knows no history index, loaded as a module of its own. The copy is
    `git show fa4c533:src/dsl41/runner_store.py`, kept in the tests for CI's shallow checkout."""
    name = "transitions_runner_store"
    if name not in sys.modules:
        source = ROOT / "tests" / "fixtures" / "transitions_runner_store.py"
        spec = importlib.util.spec_from_file_location(name, source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class _TransitionsBuild:
    """The build before this one over a root: its store code, which commits transitions rows
    and no index rows, under this build's engine."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def append(self, *records: Record, transitions: Any = (), **index: Any) -> int:
        return int(self._store.append(*records, transitions=transitions))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


@contextlib.contextmanager
def _as_transitions_build(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Engines started inside write with the build before this one: transitions rows, their
    start's steps, and no history index."""
    old = _transitions_store_module()
    real = runner_startup.SqliteStore

    def opened(path: Path, **kwargs: Any) -> Any:
        if kwargs.get("read_only"):
            return real(path, **kwargs)
        return _TransitionsBuild(old.SqliteStore(path, **kwargs))

    with monkeypatch.context() as patched:
        patched.setattr(runner_startup, "SqliteStore", opened)
        patched.setattr(runner_startup, "settle_history", lambda store, *, where: None)
        yield


def test_what_a_transitions_only_build_appended_is_a_gap_and_the_later_records_are_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This build covers through L, the build before it appends L+1 to M with its transitions
    rows and no index rows, and this build, reopened, records [L+1, M] as the index's gap while
    `transitions` covers it."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    covered_through = _last(run_root)
    with _as_transitions_build(monkeypatch):
        asyncio.run(_incarnation(run_root, snapshots=1))
    appended = _last(run_root)
    assert _history_coverage(run_root) == [Interval(1, covered_through, True)]
    asyncio.run(_incarnation(run_root))
    assert _history_coverage(run_root) == [
        Interval(1, covered_through, True),
        Interval(covered_through + 1, appended, False),
        Interval(appended + 1, _last(run_root), True),
    ]
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        assert store.transitions_coverage() == [Interval(1, _last(run_root), True)]
    runs, records = _index(run_root)
    assert not [row for row in [*runs, *records] if covered_through < row[0] <= appended]


def test_an_ordinary_restart_keeps_one_covered_interval(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True, snapshots=1))
    asyncio.run(_incarnation(run_root))
    assert _history_coverage(run_root) == [Interval(1, _last(run_root), True)]


@pytest.mark.parametrize("kind", KINDS)
def test_written_intervals_replace_what_the_history_coverage_said(
    kind: str, tmp_path: Path
) -> None:
    with _store(kind, tmp_path) as store:
        store.append(*[_record(n) for n in range(5)])
        store.cover_history([Interval(2, 3, False)])
        assert store.history_coverage() == [
            Interval(1, 1, True),
            Interval(2, 3, False),
            Interval(4, 5, True),
        ]
        assert store.transitions_coverage() == [Interval(1, 5, True)]


def test_a_start_that_cannot_settle_the_index_starts_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The start goes on and says so in one line; `status` shows the reason; `transitions`
    serves; the next start settles the index and `status` drops the reason."""
    run_root = tmp_path / "run"
    with _as_previous_build(monkeypatch):  # so the start has a gap to record
        asyncio.run(_incarnation(run_root, genesis=True))
    before = _last(run_root)

    def broken(*args: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "cover_history", broken)
        answer, _, status = asyncio.run(_ask(run_root, {}, status=True))
    assert "past runs decline (DL-539)" in capfd.readouterr().err
    reason = "the start could not settle the history index: OSError: disk full"
    assert status["history_unavailable"] == reason
    assert "transitions_unavailable" not in status and answer["ok"] is True
    _, _, status = asyncio.run(_ask(run_root, {}, status=True))
    assert "history_unavailable" not in status
    assert _history_coverage(run_root)[0] == Interval(1, before, False)


# ------------------------------------------------------------------ the commit


def test_a_commit_that_fails_leaves_neither_its_records_nor_its_index_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        _indexed(store, 0)
        real = store._record_history

        def fails(*args: Any) -> None:
            real(*args)
            raise OSError("disk full")

        monkeypatch.setattr(store, "_record_history", fails)
        with pytest.raises(OSError, match="disk full"):
            _indexed(store, 1)
    with closing(SqliteStore(path)) as reopened:
        assert reopened.last_seq() == 1
        assert _rows_of(reopened) == ([(1, 0, "j", 1, 1)], [(1, "j", 1)])
        assert reopened.history_coverage() == [Interval(1, 1, True)]


@pytest.mark.parametrize("kind", KINDS)
def test_index_rows_with_no_record_are_refused(kind: str, tmp_path: Path) -> None:
    with _store(kind, tmp_path) as store:
        with pytest.raises(StoreError, match="with the records"):
            store.append(openings=[RunOpening(0, "j", 1, 1)])
        with pytest.raises(StoreError, match="with the records"):
            store.append(owners=[RecordOwner(0, "j", 1)])
        assert store.last_seq() == 0 and _rows_of(store) == ([], [])


def test_a_run_number_repeated_in_the_index_is_committed_and_the_admission_succeeds(
    tmp_path: Path,
) -> None:
    """No unique key on `(job, run_number)`: a second opening of `j.1` commits."""
    journal = Journal(SqliteStore(tmp_path / "s.db"))
    entry = TraceEntry(at=T0, job="j", transition="INACTIVE->STARTING", cause="c", run_number=1)
    for index in (1, 2):
        attempt = _attempt(index, Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        journal.admission(attempt, _applied(attempt), transitions=[entry], rev=1)
    runs, _ = _rows_of(journal.store)
    assert runs == [(1, 0, "j", 1, 1), (3, 0, "j", 1, 1)]
    journal.close()


def test_a_run_opening_with_no_rev_is_refused_before_anything_is_written() -> None:
    journal = Journal(MemoryStore())
    entry = TraceEntry(at=T0, job="j", transition="INACTIVE->STARTING", cause="c", run_number=1)
    with pytest.raises(EngineError, match="j.1: a run's opening commits with the configuration"):
        journal.append(_record(), transitions=[entry])
    assert journal.store.last_seq() == 0


def test_the_writer_indexes_only_the_kinds_it_names_by_what_they_name() -> None:
    """A STATUS input with no string job names no run and neither does its decision. A run
    number names a run as the run-history fold reads it, an `int` instance, so `true` is run 1
    (`runner_history.fold_run_rows`), and only within the store's signed 64-bit integers; any
    other value is no named run. Other kinds and other inputs are not indexed."""

    def record(kind: str, /, **body: Any) -> Record:
        return Record(T0, kind, None, None, body)

    records = [
        record("input", index=1, kind="STATUS", payload={"status": "SUCCESS"}),
        record("decision", index=1),
        record("input", index=2, kind="STATUS", payload={"job": "j", "run_number": "1"}),
        record("decision", index=2),
        record("input", index=3, kind="STARTJOB", payload={"job": "j"}),
        record("decision", index=3),
        record("follow_up", event="done", attempt="a", step=0, seq=1),
        record("input", index=4, kind="STATUS", payload={"job": "j", "run_number": True}),
        record("input", index=5, kind="STATUS", payload={"job": "j", "run_number": 2**63}),
        record("input", index=6, kind="STATUS", payload={"job": "j", "run_number": 2**63 - 1}),
    ]
    entries = [
        TraceEntry(at=T0, job="j", transition="START_REFUSED", cause="c", run_number=1),
        TraceEntry(at=T0, job="j", transition="RUNNING->SUCCESS", cause="c"),
    ]
    assert index_rows(records, entries, None) == (
        [],
        [
            RecordOwner(2, "j", None),
            RecordOwner(3, "j", None),
            RecordOwner(7, "j", 1),
            RecordOwner(8, "j", None),
            RecordOwner(9, "j", 2**63 - 1),
        ],
    )
    assert type(index_rows(records, entries, None)[1][2].run_number) is int


async def _injected(run_root: Path, payload: dict[str, Any], source: Any = "control") -> Any:
    """A fresh SQLite root, one STATUS injected as a rehearsal injects it, then a stop."""
    engine = _start_engine(run_root)
    engine.inject(Event(at=T0, kind="STATUS", payload=payload), source=source)
    await engine.run_until_quiescent(T0 + timedelta(minutes=1))
    quarantined = list(engine.quarantined)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()
    return quarantined


def _journal_kinds(run_root: Path) -> list[tuple[int, str]]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return [(seq, record.kind) for seq, record in store.read_from(0)]


def test_a_status_naming_a_run_past_the_stores_integers_commits_with_no_named_run(
    tmp_path: Path,
) -> None:
    """`run_number = 2**63` passes the inject and the admission: its input and decision commit
    on SQLite, and their rows name the job and no run (DL-539)."""
    run_root = tmp_path / "run"
    payload = {"job": "j", "status": "SUCCESS", "run_number": 2**63}
    asyncio.run(_injected(run_root, payload))
    kinds = _journal_kinds(run_root)
    [(input_seq, _), (decision_seq, _)] = [(s, k) for s, k in kinds if k in ("input", "decision")]
    _, records = _index(run_root)
    assert records == [(input_seq, "input", "j", None), (decision_seq, "decision", "j", None)]
    assert _history_coverage(run_root) == [Interval(1, kinds[-1][0], True)]


#: One admission on a fresh SQLite root, in a child process: a STATUS whose run number is an
#: `IntEnum` member valued 1. A child, so a writer that stalls inside C code is ended by the
#: test's deadline instead of holding the test process.
_ENUM_ADMISSION = """
import asyncio, enum, sys
from datetime import datetime, timedelta
from pathlib import Path
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_startup import start_run

class Run(enum.IntEnum):
    ONE = 1

async def main() -> None:
    at = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        lower_source("insert_job: j\\njob_type: c\\ncommand: x\\n"),
        Path(sys.argv[1]),
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    payload = {"job": "j", "status": "SUCCESS", "run_number": Run.ONE}
    engine.inject(Event(at=at, kind="STATUS", payload=payload), source="control")
    await engine.run_until_quiescent(at + timedelta(minutes=1))
    await engine.shutdown()
    engine.journal.close()

asyncio.run(main())
"""


def test_an_int_enum_run_number_commits_and_names_its_value(tmp_path: Path) -> None:
    """canon writes an `IntEnum` as its number; the index takes it as that plain integer, so
    the admission commits promptly and its rows name run 1 (DL-539)."""
    run_root = tmp_path / "run"
    subprocess.run(
        [sys.executable, "-c", _ENUM_ADMISSION, str(run_root)], check=True, timeout=_WHOLE_S
    )
    _, records = _index(run_root)
    assert _kinds(records, "input", "decision") == [("input", "j", 1), ("decision", "j", 1)]


def test_a_boolean_run_number_is_indexed_as_the_run_the_fold_gives_it(tmp_path: Path) -> None:
    """Runs 1 and 2 complete, then STATUS FAILURE with `run_number=true`. The fold reads an
    `int` instance as its value (`runner_history.fold_run_rows`), so the FAILURE is run 1's;
    the input's and the decision's rows name run 1 too."""

    async def body(root: _Root) -> None:
        for minutes in (0, 2):
            await root.feed(_ev(minutes, "STARTJOB", job="j"))
            await root.engine.run_until_quiescent(_at(minutes + 1.5))
        await root.feed(_ev(5, "STATUS", job="j", status="FAILURE", run_number=True))

    root, _ = _scenario(tmp_path, body, {("j", 1): (60.0, 0), ("j", 2): (60.0, 0)})
    assert [(row.run_number, row.status) for row in read_run_root(root.run)] == [
        (1, "FAILURE"),
        (2, "SUCCESS"),
    ]
    _, records = _index(root.run)
    assert _kinds(records, "input", "decision")[-2:] == [("input", "j", 1), ("decision", "j", 1)]


def test_a_status_with_no_job_has_no_row_and_its_records_stay_covered(tmp_path: Path) -> None:
    """A STATUS with adapter provenance and no job is quarantined: its input and decision
    commit, belong to no job's history and have no row, and the coverage over them claims
    every row of a record that names a job; the scan agrees."""
    run_root = tmp_path / "run"
    [(result, _)] = asyncio.run(
        _injected(run_root, {"status": "SUCCESS", "run_number": 1}, source="adapter")
    )
    assert result.code == "engine_apply_faulted"
    kinds = _journal_kinds(run_root)
    assert [k for _, k in kinds][-2:] == ["input", "decision"]
    assert _index(run_root) == ([], [])
    assert _history_coverage(run_root) == [Interval(1, kinds[-1][0], True)]
    before = dict(transitions_check.compared)
    transitions_check.check_roots(tmp_path)
    assert transitions_check.compared["stores"] > before["stores"]


def test_a_decision_whose_input_was_pruned_is_not_compared(tmp_path: Path) -> None:
    """A retention prune can keep a STATUS's decision and drop its input. The retained records
    cannot say whose the decision is, so the scan leaves its stored row out; the row itself
    names the job, as its admission committed it."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(run_root, settings=EngineSettings(retention_days=1))
        status = {"job": "j", "status": "SUCCESS"}
        engine.inject(Event(at=T0, kind="STATUS", payload=status))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert isinstance(engine.clock, VirtualClock)
        engine.clock._now = engine.clock.now() + timedelta(days=2)
        engine.take_snapshot()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    kinds = _journal_kinds(run_root)
    assert "input" not in [k for _, k in kinds]
    [decision] = [seq for seq, k in kinds if k == "decision"]
    assert _index(run_root)[1] == [(decision, "decision", "j", None)]
    before = dict(transitions_check.compared)
    transitions_check.check_roots(tmp_path)
    assert transitions_check.compared["stores"] > before["stores"]


# ------------------------------------------------------------------ pruning and the start's trim


@pytest.mark.parametrize("kind", KINDS)
def test_pruning_removes_both_tables_rows_below_its_bound_in_its_transaction(
    kind: str, tmp_path: Path
) -> None:
    with _store(kind, tmp_path) as store:
        for n in range(6):
            _indexed(store, n)
        store.put_snapshot(5, STATE_MACHINE_VERSION, b"{}")
        ended = _commits(store) if isinstance(store, SqliteStore) else []
        # the time cutoff binds first: record 3 is the first inside the window
        store.prune_before(5, T0 + timedelta(seconds=2))
        assert store.first_seq() == 3
        runs, records = _rows_of(store)
        assert [row[0] for row in runs] == [row[0] for row in records] == [3, 4, 5, 6]
        assert store.history_coverage() == [Interval(3, 6, True)]
        assert len(ended) == (1 if kind == "sqlite" else 0)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("outside", ["job_runs", "job_records", "coverage"])
def test_the_start_drops_index_rows_and_coverage_outside_the_retained_journal(
    kind: str, outside: str, tmp_path: Path
) -> None:
    """A copy whose index outlived its journal keeps nothing outside the retained records; a
    store with nothing outside them is left as it is, with no commit."""
    with _store(kind, tmp_path) as store:
        _indexed(store, 0)
        ended = _commits(store) if isinstance(store, SqliteStore) else []
        assert settle_history(store, where="t") is None
        assert ended == []
        if isinstance(store, SqliteStore):
            with store._transaction():
                if outside == "job_runs":
                    store._db.execute("INSERT INTO job_runs VALUES (5, 0, 'j', 5, 1)")
                elif outside == "job_records":
                    store._db.execute("INSERT INTO job_records VALUES (5, 'j', 5)")
                else:
                    store._db.execute("INSERT INTO history_coverage VALUES (4, 6, 1)")
        else:
            assert isinstance(store, MemoryStore)
            if outside == "job_runs":
                store._job_runs.append((5, 0, "j", 5, 1))
            elif outside == "job_records":
                store._job_records.append((5, "j", 5))
            else:
                store._history_coverage.append(Interval(4, 6, True))
        assert settle_history(store, where="t") is None
        assert _rows_of(store) == ([(1, 0, "j", 1, 1)], [(1, "j", 1)])
        assert store.history_coverage() == [Interval(1, 1, True)]


def test_the_start_removes_index_rows_an_earlier_builds_prune_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build before this one prunes the journal and leaves the index rows: the next start
    here removes every row below the journal's first seq and trims the coverage to it."""
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    with _as_transitions_build(monkeypatch):
        asyncio.run(_incarnation(run_root, snapshots=1, prune=True))
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        first = store.first_seq()
    runs, records = _index(run_root)
    assert [row for row in [*runs, *records] if row[0] < first]
    asyncio.run(_incarnation(run_root))
    runs, records = _index(run_root)
    assert all(row[0] >= first for row in [*runs, *records])
    assert all(interval.first >= first for interval in _history_coverage(run_root))


# ------------------------------------------------------------------ compatibility


def test_an_earlier_build_opens_and_appends_to_a_root_this_build_wrote(tmp_path: Path) -> None:
    """The store code before DL-536 opens a root with the index, appends to it and prunes it;
    this build reopens it and records that append as the index's gap."""
    old = _previous_store_module()
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    path = run_root / STORE_NAME
    covered = _history_coverage(run_root)
    at = _latest(run_root)
    with closing(old.SqliteStore(path)) as previous:
        body = {"event": "snapshot", "at": at.isoformat(), "seq": 0}
        last = previous.append(old.Record(at, "engine", None, None, body))
        assert previous.prune_before(1, at) == 0
    assert _history_coverage(run_root) == covered
    with closing(SqliteStore(path)) as store:
        assert settle_history(store, where="test") is None
        assert store.history_coverage()[-1] == Interval(last, last, False)


def test_the_transitions_store_copy_is_the_previous_builds() -> None:
    """The copy commits transitions rows and knows no history index."""
    old = _transitions_store_module()
    assert any("transitions_coverage" in statement for statement in old._SCHEMA)
    assert not any("job_runs" in statement for statement in old._SCHEMA)
    assert old.SCHEMA_VERSION == 1
