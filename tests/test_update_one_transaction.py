"""An update with nothing to stop commits in one store transaction (DL-560): its `config intent`,
its configuration row, its `config applied` with the discharged records, and the CONFIG mark
bound to the applied record. After a crash the store holds the old revision and no trace of the
request, or the whole application; never an intent alone (BUG-19).

The engine scenarios run on the virtual clock with fake command tasks that never end by
themselves. A crash is a store write that raises, before its commit or after it, and so ends the
loop; the engine is then shut down without a stop record and restarted from its run root. A
command's end that the restart recovers is its spool's records, written while no engine runs and
read at the restart's catch-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate
from store_surgery import rows
from test_engine_stops_cookie import T0
from test_update_settlement import _close, _engine, _estate
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41.canon import canonical_bytes
from dsl41.oracle_state import Event
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_journal import CONFIG_MARKER, STORE_NAME, oracle_rows, read_backfill, replay_store
from dsl41.runner_startup import active_configuration, resume_run
from dsl41.runner_store import (
    ConfigRow,
    Interval,
    MemoryStore,
    Record,
    SqliteStore,
    Store,
    StoreError,
    TransitionQuery,
)
from dsl41.runner_update import UpdateAnswer, UpdateRequest
from dsl41.state_machine import Executor

KINDS = ["sqlite", "memory"]


# ---------------------------------------------------------------- the store's capability


@contextmanager
def _store(kind: str, directory: Path) -> Iterator[Store]:
    store: Store = SqliteStore(directory / "s.db") if kind == "sqlite" else MemoryStore()
    try:
        yield store
    finally:
        store.close()


def _record(n: int) -> Record:
    return Record(T0 + timedelta(seconds=n), "engine", None, None, {"event": "started", "n": n})


def _mark(n: int) -> dict[str, Any]:
    return {
        "at": T0 + timedelta(seconds=n),
        "job": "",
        "transition": CONFIG_MARKER,
        "cause": f"configuration rev {n}",
        "start_source": None,
        "run_number": None,
        "refused_status": None,
    }


def _marks(store: Store) -> list[tuple[int, int, str]]:
    read = store.read_transitions(TransitionQuery(None, False, 10**9, upto=store.last_seq()))
    return [(seq, n, entry["cause"]) for seq, n, entry in read.rows]


@pytest.mark.parametrize("kind", KINDS)
def test_dl560_records_a_configuration_row_and_a_later_records_mark_commit_together(
    kind: str, tmp_path: Path
) -> None:
    """One append holds several records, the next configuration rev and the trace entries of a
    record after the first: the entries are keyed by that record's seq, and the coverage spans
    every record of the append."""
    with _store(kind, tmp_path) as store:
        assert store.put_config(b"genesis", "sha256:genesis") == 1
        assert store.append(_record(0)) == 1
        last = store.append(
            _record(1),
            _record(2),
            _record(3),
            transitions=[_mark(2)],
            produced_by=1,
            config=ConfigRow(2, b"second", "sha256:second"),
        )
        assert last == 4
        assert (store.current_config_rev(), store.get_config(2)) == (2, b"second")
        assert store.find_config("sha256:second") == 2
        assert _marks(store) == [(3, 0, "configuration rev 2")]
        assert store.transitions_coverage() == [Interval(1, 4, True)]
        assert store.history_coverage() == [Interval(1, 4, True)]


@pytest.mark.parametrize("kind", KINDS)
def test_dl560_a_row_at_another_rev_or_a_mark_on_no_record_writes_nothing(
    kind: str, tmp_path: Path
) -> None:
    """A configuration row that is not the next rev, entries bound to an offset outside the
    records, or a row with no record refuse the whole append before anything is written."""
    with _store(kind, tmp_path) as store:
        store.put_config(b"genesis", "sha256:genesis")
        store.append(_record(0))
        for rev in (1, 3):
            with pytest.raises(StoreError, match="configuration rev"):
                store.append(_record(1), config=ConfigRow(rev, b"x", "sha256:x"))
        for offset in (-1, 2):
            with pytest.raises(StoreError, match="offset"):
                store.append(_record(1), _record(2), transitions=[_mark(1)], produced_by=offset)
        with pytest.raises(StoreError, match="with the records"):
            store.append(config=ConfigRow(2, b"x", "sha256:x"))
        assert (store.last_seq(), store.current_config_rev(), _marks(store)) == (1, 1, [])


def test_dl560_a_failure_after_the_configuration_row_rolls_back_the_records_and_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configuration row joins the records' transaction: a failure after both were written
    in it, on the history index's rows, rolls back the records, the row and the marks, and the
    store reopens without them."""
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        store.put_config(b"genesis", "sha256:genesis")
        store.append(_record(0))
        real = store._record_history

        def fails(first: int, last: int, *index: Any) -> None:
            assert store._db.in_transaction
            (written,) = store._db.execute("SELECT COUNT(*) FROM journal").fetchone()
            (rev,) = store._db.execute("SELECT MAX(rev) FROM configs").fetchone()
            assert (written, rev) == (3, 2)  # the records and the row are in the transaction
            real(first, last, *index)
            raise OSError("disk full")

        monkeypatch.setattr(store, "_record_history", fails)
        with pytest.raises(OSError, match="disk full"):
            store.append(
                _record(1),
                _record(2),
                transitions=[_mark(2)],
                produced_by=1,
                config=ConfigRow(2, b"second", "sha256:second"),
            )
        # the writer itself, before it closes: no transaction is left open over the rows
        assert not store._db.in_transaction
        assert (store.last_seq(), store.current_config_rev(), _marks(store)) == (1, 1, [])
    with closing(SqliteStore(path)) as reopened:
        assert [seq for seq, _ in reopened.read_from(0)] == [1]
        assert reopened.current_config_rev() == 1 and _marks(reopened) == []
        assert reopened.transitions_coverage() == [Interval(1, 1, True)]


@pytest.mark.parametrize("write", ["append", "put_config", "put_snapshot"])
def test_dl560_a_failed_commit_rolls_back_so_the_writer_reads_only_what_is_durable(
    tmp_path: Path, write: str
) -> None:
    """A writable blob handle open on the writer's connection makes its COMMIT fail (SQLite:
    statements in progress). The transaction is rolled back, not left open: the writer's own
    reads, an independent reader and a reopen all see the old state, and the next write
    commits."""
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        store.put_config(b"genesis", "sha256:genesis")
        store.append(Record(T0, "config", None, "rid", {"event": "x"}))
        writes: dict[str, Callable[[], Any]] = {
            "append": lambda: store.append(
                Record(T0, "config", None, "next", {"event": "y"}),
                config=ConfigRow(2, b"second", "sha256:second"),
            ),
            "put_config": lambda: store.put_config(b"second", "sha256:second"),
            "put_snapshot": lambda: store.put_snapshot(1, 28, b"snapshot"),
        }
        blob = store._db.blobopen("configs", "data", 1, readonly=False)
        try:
            with pytest.raises(sqlite3.OperationalError, match="statements in progress"):
                writes[write]()
            assert not store._db.in_transaction
        finally:
            with contextlib.suppress(sqlite3.Error):
                blob.close()
        seen = (store.last_seq(), store.current_config_rev(), store.find_request("next"))
        assert seen == (1, 1, None) and store.latest_snapshot() is None
        with closing(SqliteStore(path, read_only=True)) as reader:
            assert (reader.last_seq(), reader.current_config_rev()) == (1, 1)
        writes[write]()
    with closing(SqliteStore(path)) as reopened:
        assert reopened.current_config_rev() == (2 if write != "put_snapshot" else 1)


def test_dl560_a_row_at_another_rev_is_refused_before_any_insert(tmp_path: Path) -> None:
    """The configuration row's rev is checked as the transaction begins: a refused append runs
    no INSERT at all."""
    with closing(SqliteStore(tmp_path / "s.db")) as store:
        store.put_config(b"genesis", "sha256:genesis")
        store.append(_record(0))
        statements: list[str] = []
        store._db.set_trace_callback(statements.append)
        try:
            with pytest.raises(StoreError, match="configuration rev 3"):
                store.append(_record(1), config=ConfigRow(3, b"x", "sha256:x"))
        finally:
            store._db.set_trace_callback(None)
        assert statements and not [s for s in statements if "INSERT" in s.upper()]
        # the rev is read inside the transaction, after BEGIN IMMEDIATE took the write lock
        begin = statements.index("BEGIN IMMEDIATE")
        read = next(i for i, s in enumerate(statements) if "MAX(rev) FROM configs" in s)
        assert begin < read < statements.index("ROLLBACK")


@pytest.mark.parametrize("kind", KINDS)
def test_dl560_configuration_bytes_that_do_not_convert_leave_nothing_behind(
    kind: str, tmp_path: Path
) -> None:
    """A configuration row whose data is no bytes refuses the append, and neither store keeps
    a record, a mark or a row of it."""
    with _store(kind, tmp_path) as store:
        store.put_config(b"genesis", "sha256:genesis")
        store.append(_record(0))
        with pytest.raises(TypeError):
            store.append(
                _record(1),
                _record(2),
                transitions=[_mark(2)],
                produced_by=1,
                config=ConfigRow(2, "not bytes", "sha256:x"),  # type: ignore[arg-type]
            )
        assert (store.last_seq(), store.current_config_rev(), _marks(store)) == (1, 1, [])
        assert store.append(_record(1)) == 2


def test_dl560_the_configuration_row_keeps_the_intents_stamp(tmp_path: Path) -> None:
    """`configs.at_utc` is the stamp of the newest journal row when the row is written. The
    row is written before the record that produced the trace entries, so with an intent and an
    applied record of distinct stamps it keeps the intent's, as the two-step path stored."""
    intent = Record(T0 + timedelta(seconds=1), "config", None, "rid", {"event": "intent"})
    applied = Record(T0 + timedelta(seconds=5), "config", None, None, {"event": "applied"})
    stamps = []
    for path, one in ((tmp_path / "one.db", True), (tmp_path / "two.db", False)):
        with closing(SqliteStore(path)) as store:
            store.put_config(b"genesis", "sha256:genesis")
            store.append(_record(0))
            if one:
                store.append(
                    intent,
                    applied,
                    transitions=[_mark(5)],
                    produced_by=1,
                    config=ConfigRow(2, b"second", "sha256:second"),
                )
            else:
                store.append(intent)
                store.put_config(b"second", "sha256:second")
                store.append(applied, transitions=[_mark(5)])
            (stamp,) = store._db.execute("SELECT at_utc FROM configs WHERE rev = 2").fetchone()
            stamps.append(stamp)
    assert stamps[0] == stamps[1] and stamps[0].startswith("2026-07-01T08:00:01")


# ---------------------------------------------------------------- the engine: estates and cuts


def _box(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


def _cmd(name: str, box: str | None = None, command: str = "sleep 30") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: {command}\n" + (
        f"box_name: {box}\n\n" if box else "\n"
    )


class Crash(RuntimeError):
    """The engine died at a store write."""


@dataclass(frozen=True)
class Cut:
    """The `n`th store write from the request on raises, `before` its commit or `after` it. A
    write is one transaction: one `append` or `put_config` of the engine's SQLite store."""

    n: int
    before: bool

    @property
    def name(self) -> str:
        return f"{'before' if self.before else 'after'} write {self.n}"


class Writes:
    """Counts the store's writes once armed, and raises `Crash` at its cut."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, cut: Cut | None) -> None:
        self.cut = cut
        self.count = 0
        self.armed = False
        for name in ("append", "put_config"):
            real = getattr(SqliteStore, name)
            monkeypatch.setattr(SqliteStore, name, self._counted(real))

    def _counted(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def write(store: SqliteStore, *args: Any, **kwargs: Any) -> Any:
            if not self.armed:
                return real(store, *args, **kwargs)
            self.count += 1
            cut = self.cut
            if cut is not None and cut.n == self.count and cut.before:
                raise Crash(cut.name)
            result = real(store, *args, **kwargs)
            if cut is not None and cut.n == self.count:
                raise Crash(cut.name)
            return result

        return write


def _plan(engine: Engine, tmp_path: Path, text: str) -> str:
    _estate(tmp_path, text)
    candidate = prepare_estate(tmp_path / "estate")
    return reread(engine.update.live(), tmp_path / "run" / "plans", candidate=candidate).token


async def _feed(engine: Engine, kind: str, job: str, **payload: Any) -> None:
    engine.inject(Event(at=engine.clock.now(), kind=kind, payload={"job": job, **payload}))  # type: ignore[arg-type]
    await engine.run_until_quiescent(engine.clock.now())


async def _update_with(
    engine: Engine, writes: Writes, token: str, rid: str
) -> asyncio.Future[UpdateAnswer]:
    """Send the update with the writes armed; answer its future. A cut ends the loop."""
    writes.armed = True
    answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, rid))
    try:
        if writes.cut is None:
            await engine.run_until_quiescent(engine.clock.now())
        else:
            with pytest.raises(Crash, match=writes.cut.name):
                await engine.run_until_quiescent(engine.clock.now())
    finally:
        writes.armed = False
    return answer


async def _down(engine: Engine) -> None:
    """The engine ends with no stop record: its tasks are cancelled and its store closed."""
    await engine.shutdown()
    _close(engine)


async def _restart(tmp_path: Path) -> Engine:
    stored = active_configuration(tmp_path / "run")
    return await resume_run(
        stored.catalog,
        tmp_path / "run",
        clock=_clock(tmp_path),
        adapters={"CMD": FakeAdapter(default=None)},
        declared=stored.profile,
        stored_sources=True,
    )


def _clock(tmp_path: Path) -> Any:
    from dsl41.runner_clock import VirtualClock

    return VirtualClock(start=T0 + timedelta(minutes=1))


@dataclass
class Found:
    """What a store says of the update under request id `rid` (`_found`)."""

    intent: int | None
    applied: int | None
    configs: int
    marks: list[int]
    request: bool


def _found(run_root: Path, rid: str) -> Found:
    journal = rows(run_root)
    intents = [s for s, k, b in journal if (k, b.get("event")) == ("config", "intent")]
    applied = [
        s for s, k, b in journal if (k, b.get("event")) == ("config", "applied") and "token" in b
    ]
    assert len(intents) <= 1 and len(applied) <= 1
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        marks = [seq for seq, _, cause in _marks(store) if cause.endswith("rev 2")]
        return Found(
            intents[0] if intents else None,
            applied[0] if applied else None,
            store.current_config_rev(),
            marks,
            store.find_request(rid) is not None,
        )


def _cuts(writes: int) -> list[Cut]:
    return [Cut(n, before) for n in range(1, writes + 1) for before in (True, False)]


# ---------------------------------------------------------------- one transaction


OLD = _cmd("A") + _box("P") + _cmd("M", "P")
#: `A`'s command changes and `N` joins the idle box `P`: nothing to stop, one tree touched
NEW = _cmd("A", command="sleep 31") + _box("P") + _cmd("M", "P") + _cmd("N", "P")


async def _cut_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: Cut | None) -> int:
    """The update from `OLD` to `NEW` with the store cut at `cut`; answer the writes counted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as patch:
        writes = Writes(patch, cut)
        engine = _engine(tmp_path, OLD)
        try:
            token = _plan(engine, tmp_path, NEW)
            answer = await _update_with(engine, writes, token, "one")
            if cut is None:
                decision = answer.result().decision
                assert isinstance(decision, Admitted) and decision.stop_set == ()
        finally:
            await _down(engine)
    return writes.count


def test_dl560_a_crash_at_any_write_of_an_update_with_nothing_to_stop_leaves_old_or_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-560 rule 1: the engine dies at each store write of the update's step, before its
    commit and after it. The restart's fold finds either the old revision with no record of the
    request, or the new one with its intent, its configuration row, its applied record right
    after the intent and the CONFIG mark on the applied record. It never finds an intent alone,
    so no update resumes."""
    writes = asyncio.run(_cut_update(tmp_path / "uncut", monkeypatch, None))
    assert writes >= 1
    seen: dict[str, str] = {}
    for cut in _cuts(writes):
        directory = tmp_path / cut.name.replace(" ", "_")
        asyncio.run(_cut_update(directory, monkeypatch, cut))
        found = _found(directory / "run", "one")

        async def restart() -> tuple[int, bool, str]:
            engine = await _restart(directory)
            try:
                assert engine.journal is not None
                resumed = engine.journal.open.update is not None or engine.update.tier is not None
                await engine.run_until_quiescent(engine.clock.now())
                command = engine.oracle.catalog.jobs["A"].exec_
                return engine.config_rev, resumed, getattr(command, "command", "")
            finally:
                await _down(engine)

        rev, resumed, command = asyncio.run(restart())
        assert not resumed, cut.name
        if found.intent is None:
            assert (found.applied, found.configs, found.marks, found.request) == (
                None,
                1,
                [],
                False,
            ), cut.name
            assert (rev, command) == (1, "sleep 30"), cut.name
            seen[cut.name] = "old"
        else:
            assert found.applied == found.intent + 1, cut.name
            assert (found.configs, found.marks, found.request) == (2, [found.applied], True)
            assert (rev, command) == (2, "sleep 31"), cut.name
            seen[cut.name] = "whole"
    assert seen["before write 1"] == "old" and seen["after write 1"] == "whole"


def test_dl560_a_retry_after_a_crash_is_answered_from_the_store_or_admitted_afresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-560 rule 2: after each cut, the same request under the same id is answered from the
    store when the application is there, with no new record, and admitted afresh when nothing
    of it is, and then applies in its admission step."""
    writes = asyncio.run(_cut_update(tmp_path / "uncut", monkeypatch, None))
    for cut in _cuts(writes):
        directory = tmp_path / cut.name.replace(" ", "_")
        asyncio.run(_cut_update(directory, monkeypatch, cut))
        before = _found(directory / "run", "one")

        async def retry() -> tuple[UpdateAnswer, int, int]:
            engine = await _restart(directory)
            try:
                await engine.run_until_quiescent(engine.clock.now())
                count = len(rows(directory / "run"))
                token = json.loads(
                    next((directory / "run" / "plans").glob("*/plan.json")).read_text()
                )["token"]
                answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "one"))
                await engine.run_until_quiescent(engine.clock.now())
                return answer.result(), count, engine.config_rev
            finally:
                await _down(engine)

        answer, count, rev = asyncio.run(retry())
        after = _found(directory / "run", "one")
        assert rev == 2, cut.name
        if before.applied is not None:
            assert answer.application is not None and answer.application["rev"] == 2
            assert len(rows(directory / "run")) == count, cut.name
            assert after == before
        else:
            # admitted afresh: the store held nothing of the request, and the retry wrote it
            assert not before.request and before.intent is None, cut.name
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ()
            assert answer.application is None and len(rows(directory / "run")) > count
            assert after.request and after.intent is not None, cut.name
            assert after.applied == after.intent + 1, cut.name
            assert (after.configs, after.marks) == (2, [after.applied])


def test_dl560_an_engine_that_lost_its_root_swaps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6b, DL-560: the lock is proved before the swap of an update with nothing to stop. An
    engine that can no longer prove it holds the run root raises there, with the catalog, the
    revision and the store as they were."""
    from dsl41.runner_clock import EngineError

    async def scenario() -> None:
        engine = _engine(tmp_path, OLD)
        try:
            assert engine.journal is not None
            token = _plan(engine, tmp_path, NEW)
            count = len(rows(tmp_path / "run"))

            def lost() -> None:
                raise EngineError("the run root's lock is gone")

            monkeypatch.setattr(engine.journal, "prove", lost)
            engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "lost"))
            with pytest.raises(EngineError, match="lock is gone"):
                await engine.run_until_quiescent(engine.clock.now())
            command = engine.oracle.catalog.jobs["A"].exec_
            assert (engine.config_rev, getattr(command, "command", "")) == (1, "sleep 30")
            assert "N" not in engine.oracle.catalog.jobs
            assert len(rows(tmp_path / "run")) == count
        finally:
            monkeypatch.undo()
            await _down(engine)

    tmp_path.mkdir(parents=True, exist_ok=True)
    asyncio.run(scenario())


# ---------------------------------------------------------------- BUG-19


#: the box `P` starts on `s(Z)`; the update gives it the member `B`
BUG_OLD = _box("P", "condition: s(Z)\n") + _cmd("A", "P") + _cmd("Z")
BUG_NEW = BUG_OLD + _cmd("B", "P")


def _recover_success(run_root: Path, job: str) -> None:
    """Write the spool records of `job`'s open attempt as a wrapper that exited 0 leaves them,
    from a boot that is not this one, for the restart to read."""
    [dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch" and b["job"] == job]
    spool = Path(dispatch["spool"])
    spool.mkdir(parents=True, exist_ok=True)
    identity = {"version": 1, "run_id": dispatch["attempt"], "job": job, "run_number": 1}
    spawn = {**identity, "wrapper_pid": 1, "wrapper_start_time": "t", "boot_id": "b"}
    (spool / "spawn.json").write_text(json.dumps(spawn))
    (spool / "status.json").write_text(
        json.dumps({**identity, "outcome": "exited", "exit_code": 0})
    )


async def _bug19(directory: Path, monkeypatch: pytest.MonkeyPatch, cut: Cut | None) -> int:
    """`Z` runs, the update gives `P` the member `B` with nothing to stop, and the store is cut
    at `cut`; answer the writes counted. Uncut, the update is admitted with nothing to stop and
    applies at revision 2."""
    directory.mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as patch:
        writes = Writes(patch, cut)
        engine = _engine(directory, BUG_OLD)
        try:
            await _feed(engine, "FORCE_STARTJOB", "Z")
            assert engine.oracle.store.job["Z"].status == "RUNNING"
            token = _plan(engine, directory, BUG_NEW)
            answer = await _update_with(engine, writes, token, "bug19")
            if cut is None:
                decision = answer.result().decision
                assert isinstance(decision, Admitted) and decision.stop_set == ()
                assert engine.config_rev == 2
        finally:
            await _down(engine)
    return writes.count


def test_bug19_a_box_an_update_gives_a_member_never_runs_a_member_set_it_was_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BUG-19, DL-560: the quiet box `P` starts on `s(Z)` while `Z` runs. The update adds the
    member `B` to `P`, and the engine dies at each store write of it. `Z`'s success is recovered
    in the restart's catch-up, so `P` opens a run there. That run's members are the ones the
    revision in force gives `P`, and no update replaces them under it: the members dispatched in
    the run are `P`'s members in the catalog at its end, and `P` ends once they succeed."""
    writes = asyncio.run(_bug19(tmp_path / "count", monkeypatch, None))
    reached: set[int] = set()
    for cut in _cuts(writes):
        directory = tmp_path / cut.name.replace(" ", "_")
        asyncio.run(_bug19(directory, monkeypatch, cut))
        _recover_success(directory / "run", "Z")

        async def restart() -> tuple[int, list[str], list[str], str]:
            engine = await _restart(directory)
            try:
                await engine.run_until_quiescent(engine.clock.now())
                store = engine.oracle.store.job
                assert store["Z"].status == "SUCCESS" and store["P"].run_number == 1, cut.name
                for name in sorted(store):
                    if store[name].status == "RUNNING" and name not in ("P",):
                        await _feed(engine, "STATUS", name, status="SUCCESS")
                members = sorted(
                    name
                    for name, job in engine.oracle.catalog.jobs.items()
                    if job.box.box_name == "P"
                )
                dispatched = sorted(
                    b["job"]
                    for _, k, b in rows(directory / "run")
                    if k == "dispatch" and b["job"] != "Z"
                )
                return engine.config_rev, members, dispatched, store["P"].status
            finally:
                await _down(engine)

        rev, members, dispatched, status = asyncio.run(restart())
        reached.add(rev)
        assert members == (["A", "B"] if rev == 2 else ["A"]), cut.name
        assert dispatched == members, cut.name
        assert status == "SUCCESS", cut.name
    assert reached == {1, 2}  # cuts before the commit and after it


# ---------------------------------------------------------------- what observers see


def test_dl560_an_uncut_update_shows_observers_its_intent_then_its_application(
    tmp_path: Path,
) -> None:
    """DL-560 rule 6: what an observer reads of an uncut update with nothing to stop. The intent
    is followed by its `config applied` and then by the time advance it owes; the CONFIG mark is
    the applied record's row; the subscribe feed and its backfill carry the applied record's
    segment and nothing of the intent; a full replay and a restart from a snapshot taken before
    the update give the live engine's rows and trace."""

    traced_before_update = 0

    async def scenario() -> tuple[bytes, list[tuple[str, str]], list[dict[str, Any]]]:
        nonlocal traced_before_update
        engine = _engine(tmp_path, OLD)
        try:
            assert engine.journal is not None
            engine.take_snapshot()
            traced_before_update = len(engine.oracle.trace())
            feed = engine.journal.subscribe(Executor())
            token = _plan(engine, tmp_path, NEW)
            answer = engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "seen"))
            await engine.run_until_quiescent(engine.clock.now())
            applied = answer.result().applied
            assert applied is not None and applied.done() and applied.result()["rev"] == 2
            live: list[dict[str, Any]] = []
            while not feed.empty():
                live.append(feed.get_nowait())
            trace = [(e.job, e.transition) for e in engine.oracle.trace()]
            return canonical_bytes(oracle_rows(engine.oracle)), trace, live
        finally:
            await _down(engine)

    rows_live, trace_live, feed = asyncio.run(scenario())
    journal = rows(tmp_path / "run")
    kinds = [(k, b.get("event"), b.get("kind")) for _, k, b in journal]
    intent = kinds.index(("config", "intent", None))
    assert kinds[intent + 1] == ("config", "applied", None)
    assert kinds[intent + 2] == ("input", None, None)  # the time advance it owes
    intent_seq, applied_seq = journal[intent][0], journal[intent + 1][0]
    assert applied_seq == intent_seq + 1
    with closing(SqliteStore(tmp_path / "run" / STORE_NAME, read_only=True)) as store:
        assert [(s, n) for s, n, c in _marks(store) if c.endswith("rev 2")] == [(applied_seq, 0)]
        backfill = read_backfill(store, since=0, upto=store.last_seq()).records
        replayed = replay_store(store, where="observer")
    segments = [r for r in backfill if r["rec"] == "segment"]
    assert [r["rev"] for r in segments] == [1, 2]
    assert [r for r in feed if r["rec"] == "segment"] == segments[1:]
    assert not [r for r in backfill + feed if r.get("event") == "intent"]
    assert replayed.replay.rev == 2
    assert canonical_bytes(oracle_rows(replayed.oracle)) == rows_live
    assert [(e.job, e.transition) for e in replayed.oracle.trace()] == trace_live
    assert trace_live.count(("", CONFIG_MARKER)) == 1

    async def from_snapshot() -> tuple[bytes, list[tuple[str, str]]]:
        engine = await _restart(tmp_path)
        try:
            assert engine.journal is not None and engine.journal.open.update is None
            assert engine.config_rev == 2
            snapshot = engine.journal.store.latest_snapshot()
            assert snapshot is not None and snapshot.seq < intent_seq  # the fold crosses it
            return canonical_bytes(oracle_rows(engine.oracle)), [
                (e.job, e.transition) for e in engine.oracle.trace()
            ]
        finally:
            await _down(engine)

    rows_resumed, trace_resumed = asyncio.run(from_snapshot())
    assert rows_resumed == rows_live
    # the trace a restart folds from the snapshot: its RESTART mark, then what the live engine
    # traced after the snapshot, the CONFIG mark included
    restart = trace_resumed.index(("", "RESTART"))
    assert trace_resumed[restart + 1 :] == trace_live[traced_before_update:]
    assert ("", CONFIG_MARKER) in trace_resumed[restart + 1 :]
