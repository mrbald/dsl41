"""The archive database (DL-545): a row retention prunes is first copied to the archive, read
back and compared in this engine run; only then does the loop delete it, a step at a time
(DL-543). A conflict or an unusable archive stops pruning; rows the archive never received are
a recorded gap. The tests assert what was deleted and when in the order of turns, never a
duration."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import fcntl
import os
import shutil
import sqlite3
import stat
import threading
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_prune_steps import (
    T0,
    _alongside,
    _backlog,
    _setter,
    _steps,
    _taken,
    _turn_between_steps,
    _unbroken,
)

from dsl41 import runner, runner_archive
from dsl41.engine_charts import DrainRequest
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_archive import (
    COVERED,
    EXPIRED,
    GAP,
    Archive,
    Report,
    config_digest,
    record_digest,
    set_digest,
)
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_startup import arm_archive, resume_run, start_run
from dsl41.runner_store import Interval, Record, RecordOwner, RunOpening, SqliteStore, Store

_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_BOX_JIL = "insert_job: b\njob_type: b\n\n" + "".join(
    f"insert_job: c{n}\njob_type: c\nbox_name: b\ncommand: x\n\n" for n in range(3)
)
_STEP = 3
#: the backlog `_backlog(engine, 7)` leaves: seqs 1 to 15, below the snapshot's bound of 16
_BACKLOG = 15
_WAIT_S = 60.0  # a bound on a wait for the worker, so a stuck thread fails instead of hanging

_STARTED: list[Engine] = []


class _Fault:
    """A fault on the archive's connection: once a statement starting with `after` (or `at`)
    has run, the `nth` one starting with `at` raises, or runs as `rewrite` instead."""

    def __init__(
        self, at: str, after: str | None = None, rewrite: str | None = None, nth: int = 1
    ) -> None:
        self.at, self.after, self.rewrite, self.nth = at, after or at, rewrite, nth
        self.armed = self.fired = False
        self.seen = 0

    def statement(self, sql: str) -> str:
        if not self.armed and sql.startswith(self.after):
            self.armed = True
        if self.armed and not self.fired and sql.startswith(self.at):
            self.seen += 1
            if self.seen < self.nth:
                return sql
            self.fired = True
            if self.rewrite is None:
                raise sqlite3.OperationalError("injected fault")
            return self.rewrite
        return sql


class _Hold:
    """Holds the `nth` call of `Archive.<name>`, on the worker's thread, until released; the
    teardown releases every hold, so a failed test leaves no worker held."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, name: str = "check", nth: int = 1):
        self.entered, self.release = threading.Event(), threading.Event()
        real = getattr(Archive, name)
        calls = [0]

        def held(archive: Archive, *args: Any) -> Any:
            calls[0] += 1
            if calls[0] == nth:
                self.entered.set()
                assert self.release.wait(_WAIT_S)
            return real(archive, *args)

        monkeypatch.setattr(Archive, name, held)
        _HOLDS.append(self)

    async def reached(self) -> None:
        assert await asyncio.to_thread(self.entered.wait, _WAIT_S)


_HOLDS: list[_Hold] = []

#: the fault the archive's connections meet, at most one
_FAULT: list[_Fault] = []


def _statement(sql: str) -> str:
    return _FAULT[0].statement(sql) if _FAULT else sql


class _FaultyConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return super().execute(_statement(sql), parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return super().executemany(_statement(sql), parameters)


@pytest.fixture(autouse=True)
def _closed_after(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each engine's worker is stopped and joined and its store closed at teardown, also when
    the test failed first. Batches are `_STEP` rows, as the steps are."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    yield
    _FAULT.clear()
    while _HOLDS:
        _HOLDS.pop().release.set()
    stuck: list[str] = []
    while _STARTED:
        engine = _STARTED.pop()
        try:
            if engine.archive is not None:
                engine.archive.stop()
                # the thread closes its two connections as it ends
                if not engine.archive.join(_WAIT_S):
                    stuck.append(str(engine.archive.path))
        finally:
            if engine.journal is not None:
                engine.journal.close()
    assert not stuck, f"archive workers still running: {stuck}"


def _run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    """`_run(scenario)`; when it fails, every engine it started is shut down in its
    own loop before the failure goes on, so no task or worker outlives the test."""

    async def guarded() -> Any:
        begun = len(_STARTED)
        try:
            return await scenario()
        except BaseException:
            for engine in _STARTED[begun:]:
                with contextlib.suppress(Exception):
                    await engine.shutdown()
            raise

    return asyncio.run(guarded())


def _settings(**values: Any) -> EngineSettings:
    return EngineSettings(**{"retention_days": 1, "archive_days": 30, **values})


def _armed(engine: Engine, run_root: Path, settings: EngineSettings) -> Engine:
    _STARTED.append(engine)
    arm_archive(engine, run_root, settings)
    return engine


def _start(
    run_root: Path,
    settings: EngineSettings | None = None,
    *,
    jil: str = _JIL,
    adapter: FakeAdapter | None = None,
) -> Engine:
    settings = settings or _settings()
    engine = start_run(
        lower_source(jil),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
        settings=settings,
    )
    return _armed(engine, run_root, settings)


async def _resume(run_root: Path, at: datetime, settings: EngineSettings | None = None) -> Engine:
    settings = settings or _settings()
    engine = await resume_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
        settings=settings,
    )
    _armed(engine, run_root, settings)
    await engine.run_until_quiescent(engine.clock.now())
    return engine


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _store(engine: Engine) -> Store:
    assert engine.journal is not None
    return engine.journal.store


async def _settled(engine: Engine) -> None:
    """Wait for the worker to finish what it was asked; its reports were handed to the loop
    before it went idle, so they have run when this returns."""
    worker = engine.archive
    assert worker is not None
    assert await asyncio.to_thread(worker.idle.wait, _WAIT_S)


async def _round(engine: Engine) -> int:
    """One snapshot round and the worker's whole answer to it; answers the round's bound."""
    engine.take_snapshot()
    await _settled(engine)
    assert engine._archive_bound is not None
    return engine._archive_bound


async def _drain(engine: Engine) -> None:
    """The loop with nothing due: it takes every owed step and quiesces."""
    await engine.run_until_quiescent(engine.clock.now())


def _later(engine: Engine, **delta: float) -> None:
    clock = engine.clock
    assert isinstance(clock, VirtualClock)
    clock._now = clock.now() + timedelta(**delta)


def _archive(run_root: Path) -> sqlite3.Connection:
    """A read-only connection to the root's archive; the caller closes it."""
    return sqlite3.connect(f"file:{run_root / 'archive.db'}?mode=ro", uri=True)


def _rows(db: sqlite3.Connection, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in db.execute(sql, params)]


def _coverage(db: sqlite3.Connection) -> list[tuple[int, int, str]]:
    """The journal rows' coverage, for every root."""
    return [
        (int(a), int(b), str(state))
        for a, b, state in db.execute(
            "SELECT first_seq, last_seq, state FROM coverage ORDER BY root_id, first_seq"
        )
    ]


def _live(run_root: Path, below: int) -> Any:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        batch = store.raw_batch(1, below, 10**6)
        return batch, batch.config_data


async def _job_backlog(engine: Engine) -> None:
    """A run of `j` that ends at T0 plus a second, beside three setters, then two days on the
    clock: dispatch, outcome, a STATUS input and its decision, all past the retention."""
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    for n in range(3):
        engine.inject(_setter(n, T0), request_id=f"r-{n}")
    await engine.run_until_quiescent(T0 + timedelta(minutes=1))
    _later(engine, days=2)


def test_a_pruned_range_equals_its_archive_rows_and_nothing_goes_before_the_report(
    tmp_path: Path,
) -> None:
    """Rule 1: the round deletes nothing; after the worker's reports the loop deletes the
    range, and every archive row is the live row byte for byte: the journal fields and the
    payload, the transitions, the history index and the configuration, each seq observed once
    under the live store's history."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root, adapter=FakeAdapter({("j", 1): (1.0, 0)}, default=None))
        await _job_backlog(engine)
        store = _store(engine)
        engine.take_snapshot()
        bound = engine._archive_bound
        assert bound is not None and store.first_seq() == 1 and engine._prune_owed is None
        live, configs = _live(run_root, bound)
        await _settled(engine)
        assert store.first_seq() == 1 and engine._prune_owed == bound  # owed, not taken
        await _drain(engine)
        assert store.first_seq() == bound and _unbroken(store)
        with closing(_archive(run_root)) as db:
            held = [
                (seq, at, kind, actor, rid, payload.encode())
                for seq, at, kind, actor, rid, payload in db.execute(
                    "SELECT seq, at_utc, kind, actor, request_id, payload FROM records ORDER BY seq"
                )
            ]
            assert held == live.records and len(held) == bound - 1
            transitions = [
                (seq, n, job, name, entry.encode())
                for seq, n, job, name, entry in db.execute(
                    "SELECT seq, n, job, transition, entry FROM transitions ORDER BY seq, n"
                )
            ]
            assert transitions == live.transitions and transitions
            runs = _rows(db, "SELECT seq, n, job, run_number, rev FROM job_runs ORDER BY seq, n")
            assert runs == live.job_runs and runs
            owners = _rows(db, "SELECT seq, job, run_number FROM job_records ORDER BY seq")
            assert owners == live.job_records and len(owners) >= 4
            assert [
                (rev, digest, data.encode())
                for rev, digest, data in db.execute("SELECT rev, digest, data FROM configs")
            ] == configs
            assert _coverage(db) == [(1, bound - 1, COVERED)]
            assert _rows(db, "SELECT seq, history FROM all_records ORDER BY seq") == [
                (seq, store.histories()[0][0]) for seq in range(1, bound)
            ]
        await _close(engine)

    _run(scenario)


def test_a_failed_archive_write_deletes_nothing_and_status_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault inside the archive's transaction rolls it back and stops the archive: nothing
    is deleted, `status` names the reason, and a request id below the bound stays taken."""
    run_root = tmp_path / "run"
    real = runner_archive._text
    calls: list[int] = []

    def failing(data: bytes, what: str) -> str:
        calls.append(1)
        if len(calls) == 2:  # inside the transaction, after its first row
            raise sqlite3.OperationalError("disk I/O error")
        return real(data, what)

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        monkeypatch.setattr(runner_archive, "_text", failing)
        await _round(engine)
        await _drain(engine)
        assert store.first_seq() == 1 and engine._prune_owed is None
        status = ControlServer(engine, run_root / "c.sock")._status({})["archive"]
        assert status["state"] == "stopped"
        assert status["reason"] == "OperationalError: disk I/O error"
        with closing(_archive(run_root)) as db:
            assert _rows(db, "SELECT COUNT(*) FROM records") == [(0,)]  # rolled back
        engine.inject(_setter(0, T0), request_id="r-0")
        await _drain(engine)
        assert [rid for rid, _ in engine.deduped] == ["r-0"]  # still taken
        await _close(engine)

    _run(scenario)


def _differ(run_root: Path, seq: int) -> None:
    """An archive row at `seq` of this root that is not the live row."""
    with closing(sqlite3.connect(run_root / "archive.db")) as db:
        db.execute("UPDATE records SET payload = '{}' WHERE seq = ?", (seq,))
        db.commit()


def test_a_crash_between_the_archive_commit_and_the_delete_is_verified_again(
    tmp_path: Path,
) -> None:
    """The first run copies and verifies and ends before its delete. The next run reads the
    same rows, finds them in the archive, compares and deletes; each copy is held once, with
    the time it was first archived."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        await _round(engine)
        assert _store(engine).first_seq() == 1
        await _close(engine)

    _run(first)
    with closing(_archive(run_root)) as db:
        before = _rows(db, "SELECT seq, archived_at FROM records ORDER BY seq")
    assert [seq for seq, _ in before] == list(range(1, _BACKLOG + 1))

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=1))
        bound = await _round(engine)
        await _drain(engine)
        assert _store(engine).first_seq() == bound
        await _close(engine)

    _run(second)
    with closing(_archive(run_root)) as db:
        after = _rows(db, "SELECT seq, archived_at FROM records ORDER BY seq")
        assert _rows(db, "SELECT COUNT(*), COUNT(DISTINCT seq) FROM records") == [
            (len(after), len(after))
        ]
    assert after[: len(before)] == before  # each copy keeps its first archive time


def test_a_retention_raised_at_restart_deletes_nothing_above_its_bound(tmp_path: Path) -> None:
    """Copies made under one day of retention stay in the archive; the restart under thirty
    days verifies nothing below its own bound, so nothing is deleted."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        await _round(engine)
        await _close(engine)

    _run(first)

    async def second() -> None:
        engine = await _resume(
            run_root, T0 + timedelta(days=2, minutes=1), _settings(retention_days=30)
        )
        bound = await _round(engine)
        await _drain(engine)
        assert bound == 1 and _store(engine).first_seq() == 1 and _unbroken(_store(engine))
        await _close(engine)

    _run(second)
    with closing(_archive(run_root)) as db:
        assert _rows(db, "SELECT COUNT(*) FROM records") == [(_BACKLOG,)]


def test_a_report_of_an_older_round_or_after_the_stop_began_deletes_nothing(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        await _round(engine)
        engine._prune_owed = None  # forget the round's own reports
        engine.archive_report(Report(engine._archive_round - 1, verified=(1, _BACKLOG)))
        assert engine._prune_owed is None
        engine.lifecycle.stop_requested(DrainRequest(0.0, 0.05, 0.1))
        engine.archive_report(Report(engine._archive_round, verified=(1, _BACKLOG)))
        assert engine._prune_owed is None
        rounds = engine._archive_round
        await _drain(engine)
        assert engine.stopped.is_set() and engine._archive_round == rounds  # the stop's
        assert store.first_seq() == 1  # snapshot asked for no round and took no step
        await _close(engine)

    _run(scenario)


def _old_rows(db: sqlite3.Connection, root: str, seqs: range, at: datetime, size: int) -> None:
    """Another root's rows in the archive, archived at `at`."""
    db.execute(
        "INSERT INTO roots(root_id, first_archived_at) VALUES (?, ?)", (root, at.isoformat())
    )
    payload = '{"x":"' + "y" * size + '"}'
    db.executemany(
        "INSERT INTO records(root_id, seq, digest, at_utc, kind, actor, request_id, payload,"
        " archived_at) VALUES (?, ?, ?, ?, 'engine', NULL, NULL, ?, ?)",
        [
            (
                root,
                seq,
                record_digest((seq, at.isoformat(), "engine", None, None, payload.encode())),
                at.isoformat(),
                payload,
                at.isoformat(),
            )
            for seq in seqs
        ],
    )
    db.execute(
        "INSERT INTO configs(root_id, rev, digest, data, archived_at) VALUES (?, 1, ?, '{}', ?)",
        (root, config_digest(b"{}"), at.isoformat()),
    )


def test_expiry_keeps_what_the_live_store_holds_and_releases_the_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows older than `archive_days`: this root's go only below the live store's first seq;
    another root's never go here, however old; a configuration the live store holds stays;
    the coverage names the expired range; the compaction leaves no free page in the file."""
    run_root = tmp_path / "run"
    monkeypatch.setattr(runner, "PRUNE_STEP", 100)

    async def scenario() -> None:
        engine = _start(run_root, _settings(archive_days=3))
        for n in range(150):
            event = Event(at=T0, kind="SET_GLOBAL", payload={"name": f"G{n}", "value": "x" * 2000})
            engine.inject(event, request_id=f"big-{n}")
        await engine.run_until_quiescent(T0)
        _later(engine, days=2)
        store = _store(engine)
        await _round(engine)
        engine._prune_owed = None
        while store.first_seq() < 200:  # the loop took the owed steps up to seq 200 only
            engine._prune_step(200)
        assert store.first_seq() == 200
        with closing(sqlite3.connect(run_root / "archive.db")) as db:
            _old_rows(db, "old", range(1, 50), T0 - timedelta(days=900), 1000)
            db.commit()
            pages = db.execute("PRAGMA page_count").fetchone()[0]
        _later(engine, days=4)
        await _round(engine)
        assert store.first_seq() == 200  # the loop took no step: the live store holds 200 on
        with closing(_archive(run_root)) as db:
            mine = [
                seq
                for (seq,) in db.execute(
                    "SELECT seq FROM records WHERE root_id = ? ORDER BY seq", (engine.root_id,)
                )
            ]
            assert mine[0] == 200  # 1 to 199 expired; the rest is old, but the live store holds it
            assert _rows(db, "SELECT COUNT(*) FROM records WHERE root_id = 'old'") == [(49,)]
            # this root's configuration stays while the live store holds it (R1); the other
            # root's stays, as nothing here expires that root
            assert _rows(db, "SELECT root_id FROM configs ORDER BY 1") == sorted(
                [("old",), (engine.root_id,)]
            )
            assert _rows(
                db,
                "SELECT first_seq, last_seq, state FROM coverage WHERE root_id = ?"
                " ORDER BY first_seq",
                engine.root_id,
            )[0] == (1, 199, EXPIRED)
            assert db.execute("PRAGMA freelist_count").fetchone()[0] == 0
            assert db.execute("PRAGMA page_count").fetchone()[0] < pages
        await _close(engine)

    _run(scenario)


def test_the_archive_file_is_owner_only_and_vacuums_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a round writes it, the file the engine created, its `-wal`, its `-shm` and its
    lock are 0600."""
    run_root = tmp_path / "run"
    hold = _Hold(monkeypatch)

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 2)
        engine.take_snapshot()
        await hold.reached()
        for name in ("archive.db", "archive.db-wal", "archive.db-shm", "archive.db.lock"):
            assert stat.S_IMODE(os.stat(run_root / name).st_mode) == 0o600, name
        hold.release.set()
        await _settled(engine)
        with closing(_archive(run_root)) as db:
            assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # incremental
        await _close(engine)

    _run(scenario)


def test_an_archive_file_others_may_read_is_refused(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "archive.db").touch(mode=0o644)
    os.chmod(run_root / "archive.db", 0o644)

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 2)
        await _round(engine)
        assert engine.archive_view()["reason"] == (
            f"{run_root / 'archive.db'}: mode 0644 lets group or others read or write it; the"
            " archive holds dispatch environments and is written only at mode 0600"
        )
        assert (run_root / "archive.db").stat().st_size == 0  # left as it was
        await _close(engine)

    _run(scenario)


def test_derived_rows_follow_their_live_coverage_and_a_box_start_arrives_whole(
    tmp_path: Path,
) -> None:
    """A range the live history coverage does not name stores no history set, and its
    observations say no claim; the transitions there arrive. A box start's many rows at one
    seq all arrive, as one set."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root, jil=_BOX_JIL, adapter=FakeAdapter(default=(1.0, 0)))
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "b"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        _later(engine, days=2)
        store = _store(engine)
        assert isinstance(store, SqliteStore)
        box = [seq for seq, n, job, *_ in store.raw_batch(1, 10**6, 10**6).job_runs if job == "b"]
        assert box
        live = store.raw_batch(1, 10**6, 10**6)
        crowded = max(
            {seq for seq, *_ in live.job_runs}, key=lambda s: sum(r[0] == s for r in live.job_runs)
        )
        assert sum(r[0] == crowded for r in live.job_runs) >= 2
        assert sum(r[0] == crowded for r in live.transitions) >= 3
        last = store.last_seq()
        store.cover_history([Interval(crowded + 1, last, False)])  # an earlier build's appends
        bound = await _round(engine)
        with closing(_archive(run_root)) as db:
            at_crowded = _rows(db, "SELECT n FROM job_runs WHERE seq = ? ORDER BY n", crowded)
            assert at_crowded == [(r[1],) for r in live.job_runs if r[0] == crowded]
            assert _rows(db, "SELECT COUNT(*) FROM transitions WHERE seq = ?", crowded) == [
                (sum(r[0] == crowded for r in live.transitions),)
            ]
            assert _rows(db, "SELECT COUNT(*) FROM job_runs WHERE seq > ?", crowded) == [(0,)]
            assert _rows(db, "SELECT COUNT(*) FROM job_records WHERE seq > ?", crowded) == [(0,)]
            assert _rows(db, "SELECT COUNT(*) FROM transitions WHERE seq > ?", crowded)[0][0] > 0
            assert _rows(
                db,
                "SELECT COUNT(*) FROM observations WHERE seq > ? AND job_runs_set IS NULL"
                " AND job_records_set IS NULL AND transitions_set IS NOT NULL",
                crowded,
            ) == [(bound - 1 - crowded,)]
            assert _rows(
                db, "SELECT size FROM sets WHERE table_name = 'transitions' AND seq = ?", crowded
            ) == [(sum(r[0] == crowded for r in live.transitions),)]
            assert _coverage(db) == [(1, bound - 1, COVERED)]
        await _drain(engine)
        assert store.first_seq() == bound
        await _close(engine)

    _run(scenario)


@pytest.mark.parametrize("alias", [False, True], ids=["one-name", "two-names"])
def test_a_second_engine_on_one_archive_is_stopped_while_a_round_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: bool
) -> None:
    """The first engine's round holds the lock from its open to its close; the second's
    round meanwhile is stopped, also when it names the file through a symbolic link. Once the
    first round ends, the second works."""
    shared = tmp_path / "shared.db"
    other = tmp_path / "alias.db"
    other.symlink_to(shared)
    hold = _Hold(monkeypatch)

    async def scenario() -> None:
        one = _start(tmp_path / "one", _settings(archive_path=str(shared)))
        two = _start(tmp_path / "two", _settings(archive_path=str(other if alias else shared)))
        for engine in (one, two):
            await _backlog(engine, 2)
        one.take_snapshot()
        await hold.reached()
        await _round(two)
        assert two.archive_view() == {
            "state": "stopped",
            "reason": "another engine holds the archive, or an operator holds its lock",
            "verified_through": None,
            "histories": None,
            "last_seq": None,
        }
        await _drain(two)
        assert _store(two).first_seq() == 1
        hold.release.set()
        await _settled(one)
        assert one.archive_view()["state"] == "working"
        bound = await _round(two)
        await _drain(two)
        assert two.archive_view()["state"] == "working" and _store(two).first_seq() == bound
        await _close(one)
        await _close(two)

    _run(scenario)


def test_the_rows_an_earlier_build_pruned_are_a_gap_in_the_archive(tmp_path: Path) -> None:
    """The build before the archive prunes in one delete; this build's next round records
    what it never received as a gap and copies on from the live store's first seq."""
    from test_transitions import _previous_store_module

    run_root = tmp_path / "run"

    async def first() -> int:
        engine = _start(run_root)
        await _backlog(engine, 7)
        await _round(engine)
        await _drain(engine)
        for n in range(3):
            engine.inject(_setter(n, engine.clock.now()), request_id=f"later-{n}")
        await _drain(engine)
        engine.take_snapshot()  # nothing past retention: the round copies nothing
        await _settled(engine)
        last = _store(engine).last_seq()
        await _close(engine)
        return last

    last = _run(first)
    with closing(_archive(run_root)) as db:
        [(_, held, _)] = _coverage(db)
    old = _previous_store_module()
    with closing(old.SqliteStore(run_root / STORE_NAME)) as previous:
        assert previous.prune_before(last + 1, T0 + timedelta(days=5)) > 0
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        first_live = store.first_seq()
    assert first_live > held + 1

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=5))
        bound = await _round(engine)
        with closing(_archive(run_root)) as db:
            assert _coverage(db) == [
                (1, held, COVERED),
                (held + 1, first_live - 1, GAP),
                (first_live, bound - 1, COVERED),
            ]
        await _close(engine)

    _run(second)


def test_archive_days_zero_prunes_as_before_and_makes_no_archive(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root, _settings(archive_days=0))
        assert engine.archive is None
        await _backlog(engine, 7)
        store = _store(engine)
        engine.take_snapshot()
        assert store.first_seq() == _STEP + 1  # the round's own step, as DL-543 takes it
        await _drain(engine)
        assert store.first_seq() == _BACKLOG + 1
        assert ControlServer(engine, run_root / "c.sock")._status({})["archive"] == {"state": "off"}
        await _close(engine)

    _run(scenario)
    assert sorted(p.name for p in run_root.iterdir() if p.name.startswith("archive")) == []


def test_a_worker_past_the_shutdown_deadline_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A batch held inside its read back outlives the shutdown's deadline; the shutdown goes
    on and says so. Let go, the batch adds its copies and reports; the report deletes nothing,
    and the worker ends without another batch."""
    run_root = tmp_path / "run"
    entered, release = threading.Event(), threading.Event()
    real: Callable[..., None] = Archive.check

    def held(self: Archive, *args: Any) -> None:
        entered.set()
        assert release.wait(_WAIT_S)
        real(self, *args)

    monkeypatch.setattr(Archive, "check", held)
    monkeypatch.setattr(runner, "STOP_DEADLINE_S", 0.01)

    async def scenario() -> None:
        try:
            await held_scenario()
        finally:
            release.set()  # a failure never leaves the worker held

    async def held_scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        engine.take_snapshot()
        assert await asyncio.to_thread(entered.wait, _WAIT_S)
        await engine.shutdown()
        assert "did not stop within" in capsys.readouterr().err
        release.set()
        worker = engine.archive
        assert worker is not None and await asyncio.to_thread(worker.join, _WAIT_S)
        await asyncio.sleep(0)  # a turn for anything the worker handed over
        assert engine._prune_owed is None and _store(engine).first_seq() == 1
        assert engine._archive_verified is None
        assert engine.journal is not None
        engine.journal.close()
        with closing(_archive(run_root)) as db:
            assert _rows(db, "SELECT MAX(seq) FROM records") == [(_STEP,)]

    _run(scenario)


@pytest.mark.parametrize(("layout", "reader"), [("1", "1"), ("3", "2"), ("0", "0")])
def test_a_layout_this_build_does_not_write_is_refused_naming_both_versions(
    tmp_path: Path, layout: str, reader: str
) -> None:
    """A writer opens only its own layout: the earlier layouts, which a `layout_version`
    names (the seq-keyed one and the branch one were both 1), and any other, are refused by
    name before anything changes. No step from another layout exists."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    with closing(sqlite3.connect(run_root / "archive.db")) as db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("layout_version", layout), ("oldest_reader", reader)],
        )
        db.commit()
    os.chmod(run_root / "archive.db", 0o600)

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 2)
        await _round(engine)
        assert engine.archive_view()["reason"] == (
            f"{run_root / 'archive.db'}: archive layout layout_version {layout}; this build"
            " writes dsl41-archive-content-1 only"
        )
        await _close(engine)

    _run(scenario)
    with closing(_archive(run_root)) as db:
        assert _rows(db, "SELECT name FROM sqlite_master") == [
            ("meta",),
            ("sqlite_autoindex_meta_1",),
        ]


def test_status_shows_the_archive_in_each_state(tmp_path: Path) -> None:
    """Working before any round and after one; stopped with its reason; `off` without an
    archive is in the archive-days test."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        server = ControlServer(engine, run_root / "c.sock")
        assert server._status({})["archive"] == {
            "state": "working",
            "verified_through": None,
            "histories": None,
            "last_seq": None,
        }
        await _backlog(engine, 7)
        await _round(engine)
        last = _store(engine).last_seq()
        assert server._status({})["archive"] == {
            "state": "working",
            "verified_through": _BACKLOG,
            "histories": 1,
            "last_seq": last,
        }
        await _drain(engine)
        engine.archive_report(Report(engine._archive_round, stopped="the archive is gone"))
        assert server._status({})["archive"] == {
            "state": "stopped",
            "reason": "the archive is gone",
            "verified_through": None,
            "histories": 1,
            "last_seq": last,
        }
        await _close(engine)

    _run(scenario)


def test_with_the_archive_a_ready_task_gets_a_turn_between_every_two_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-543's drain with the archive on: the verified backlog goes in whole steps, with a
    turn for another task between every two, and an input it feeds after the second step is
    admitted before the third."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        bound = await _round(engine)
        assert _taken(log) == []  # the round itself deletes nothing
        now = engine.clock.now()
        fed: list[int] = []

        def feed() -> None:
            fed.append(store.last_seq())
            engine.inject(_setter(9, now), request_id="mid")

        await _alongside(engine, log, {2: feed})
        assert _taken(log) == [_STEP] * (_BACKLOG // _STEP) and _turn_between_steps(log)
        steps = [entry for entry in log if entry[0] == "step"]
        decided = {record.request_id: seq for seq, record in store.read_from(0)}
        assert steps[1][2] == fed[0] < decided["mid"] - 1 <= steps[2][2]
        assert store.first_seq() == bound
        await _close(engine)

    _run(scenario)


def test_a_file_that_is_not_an_archive_is_refused_and_left_as_it_is(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    with closing(sqlite3.connect(run_root / "archive.db")) as db:
        db.execute("CREATE TABLE notes (x)")
        db.commit()
    os.chmod(run_root / "archive.db", 0o600)

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 2)
        await _round(engine)
        assert engine.archive_view()["reason"] == (
            f"{run_root / 'archive.db'} is not a dsl41 archive: it names no layout"
        )
        with closing(sqlite3.connect(run_root / "archive.db")) as db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES ('layout', 'something else')")
            db.commit()
        await _round(engine)
        assert engine.archive_view()["reason"].endswith(
            "archive layout 'something else'; this build writes dsl41-archive-content-1 only"
        )
        await _drain(engine)
        assert _store(engine).first_seq() == 1
        await _close(engine)

    _run(scenario)


def test_an_engine_with_no_store_file_and_an_unasked_worker() -> None:
    """A rehearsal's memory store has no archive. A worker never asked for a round starts no
    thread, ends at once and takes no request once stopped."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=_settings(),
        )
        arm_archive(engine, Path("/nonexistent"), _settings())
        assert engine.archive is None and engine.archive_view() == {"state": "off"}
        reports: list[Report] = []
        worker = runner_archive.ArchiveWorker(
            Path("/nonexistent/engine.db"),
            Path("/nonexistent/archive.db"),
            root={"root_id": "r"},
            days=1,
            step=1,
            now=lambda: T0,
            report=reports.append,
        )
        worker.stop()
        worker.request(1, 10)
        assert worker.join(0) and worker.idle.is_set() and reports == []

    _run(scenario)


def test_the_settings_name_the_archive_file(tmp_path: Path) -> None:
    import tomllib

    assert EngineSettings().archive_file(tmp_path) == tmp_path / "archive.db"
    relative = EngineSettings(archive_path="keep/a.db")
    assert relative.archive_file(tmp_path) == tmp_path / "keep" / "a.db"
    assert EngineSettings(archive_path="/srv/a.db").archive_file(tmp_path) == Path("/srv/a.db")
    assert EngineSettings(archive_days=0, archive_path="x.db").archive_file(tmp_path) is None
    assert EngineSettings.model_validate(tomllib.loads(relative.to_toml())) == relative
    with pytest.raises(ValueError):
        EngineSettings(archive_days=-1)


def test_coverage_intervals_overlay_and_find_their_holes() -> None:
    held = [(1, 3, COVERED), (6, 8, EXPIRED), (12, 20, COVERED)]
    assert runner_archive._holes(held, 1, 10) == [(4, 5), (9, 10)]
    assert runner_archive._holes(held, 7, 9) == [(9, 9)]
    assert runner_archive._holes([], 2, 4) == [(2, 4)]
    assert runner_archive._overlay(held, 2, 7, GAP) == [
        (1, 1, COVERED),
        (2, 7, GAP),
        (8, 8, EXPIRED),
        (12, 20, COVERED),
    ]
    assert runner_archive._overlay([(1, 3, GAP)], 4, 5, GAP) == [(1, 5, GAP)]
    assert runner_archive._expired([(1, 4, GAP), (5, 12, COVERED)], [2, 5, 6, 10]) == [
        (1, 4, GAP),
        (5, 6, EXPIRED),
        (7, 9, COVERED),
        (10, 10, EXPIRED),
        (11, 12, COVERED),
    ]
    with pytest.raises(runner_archive.ArchiveStopped, match="record 7 is not UTF-8 text"):
        runner_archive._text(b"\xff", "record 7")


async def _basic(run_root: Path) -> Engine:
    """Seven inputs past the retention, the archive on and never asked yet."""
    engine = _start(run_root)
    await _backlog(engine, 7)
    return engine


async def _after_an_older_prune(run_root: Path) -> Engine:
    """Rows 1 to 15 pruned without an archive, then the archive armed: its first round records
    a gap over them before it copies."""
    engine = _start(run_root, _settings(archive_days=0))
    await _backlog(engine, 7)
    engine.take_snapshot()
    await _drain(engine)
    assert _store(engine).first_seq() == _BACKLOG + 1
    arm_archive(engine, run_root, _settings())
    for n in range(3):
        engine.inject(_setter(n, engine.clock.now()), request_id=f"later-{n}")
    await _drain(engine)
    _later(engine, days=2)
    return engine


async def _with_expired_copies(run_root: Path) -> Engine:
    """A round verified and the loop deleted rows 1 to 15; two days on, their copies are past
    `archive_days`, so the next round expires them."""
    engine = _start(run_root, _settings(archive_days=1))
    await _backlog(engine, 7)
    await _round(engine)
    await _drain(engine)
    _later(engine, days=2)
    return engine


def _held_records(run_root: Path) -> int:
    with closing(_archive(run_root)) as db:
        return int(db.execute("SELECT COUNT(*) FROM records").fetchone()[0])


def _held_coverage(run_root: Path) -> int:
    with closing(_archive(run_root)) as db:
        return int(db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0])


_ARMS: list[tuple[str, Callable[[Path], Any], _Fault | None, str, Callable[[Path], bool]]] = [
    # the open: a file SQLite cannot read, WAL refused; the batch's first write
    ("open", _basic, None, "DatabaseError: file is not a database", lambda r: True),
    (
        "open-wal",
        _basic,
        _Fault("PRAGMA journal_mode=WAL", rewrite="PRAGMA journal_mode"),
        "journal_mode is 'delete', the archive needs WAL",
        lambda r: True,
    ),
    # the read again after the commit fails: the batch stays committed, and no report
    (
        "read-again",
        _basic,
        # each row is read before its write and once inside the transaction
        _Fault(
            "SELECT at_utc, kind, actor, request_id, payload, archived_at FROM records",
            nth=2 * _STEP + 1,
        ),
        "injected fault",
        lambda r: _held_records(r) == _STEP,
    ),
    (
        "gaps",
        _after_an_older_prune,
        _Fault("DELETE FROM coverage"),
        "injected fault",
        lambda r: _held_records(r) == 0 and _held_coverage(r) == 0,
    ),
    # the expiry delete rolls back: the expired copies stay
    (
        "expiry",
        _with_expired_copies,
        _Fault("DELETE FROM records WHERE root_id"),
        "injected fault",
        lambda r: _held_records(r) > _BACKLOG,
    ),
    (
        "compact",
        _with_expired_copies,
        _Fault("PRAGMA incremental_vacuum"),
        "injected fault",
        lambda r: _held_records(r) <= _BACKLOG,
    ),
]


@pytest.mark.parametrize(
    ("setup", "fault", "reason", "held"),
    [arm[1:] for arm in _ARMS],
    ids=[arm[0] for arm in _ARMS],
)
def test_a_fault_at_each_archive_step_stops_pruning_and_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path], Any],
    fault: _Fault | None,
    reason: str,
    held: Callable[[Path], bool],
) -> None:
    """A fault at the open, the insert, the commit, the read back, the coverage write, the
    gap write, the expiry delete or the compaction: the round reports `stopped` with the
    failure, the loop deletes nothing more, and a failed transaction left nothing of itself."""
    run_root = tmp_path / "run"
    real_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if "archive.db" in str(database):
            kwargs["factory"] = _FaultyConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    if fault is None:
        run_root.mkdir()
        (run_root / "archive.db").write_bytes(b"not a database, " * 64)  # SQLite cannot read it
        os.chmod(run_root / "archive.db", 0o600)

    async def scenario() -> None:
        engine = await setup(run_root)
        first = _store(engine).first_seq()
        if fault is not None:
            _FAULT.append(fault)
        await _round(engine)
        await _drain(engine)
        view = engine.archive_view()
        assert view["state"] == "stopped" and reason in view["reason"], view
        assert _store(engine).first_seq() == first and engine._prune_owed is None
        assert fault is None or fault.fired
        _FAULT.clear()
        if fault is not None:
            assert held(run_root)
        await _close(engine)

    _run(scenario)


def test_a_fault_at_the_lock_or_the_live_store_stops_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that fails for another reason than a holder, and a live store that does not
    open for reading, stop the archive with the error."""
    import errno

    from dsl41.runner_store import StoreError

    def no_lock(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "no locks available")

    def no_store(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("the live store does not open")

    async def scenario() -> None:
        engine = await _basic(tmp_path / "run")
        with monkeypatch.context() as patch:
            patch.setattr(runner_archive.fcntl, "flock", no_lock)
            await _round(engine)
        assert engine.archive_view()["reason"] == "OSError: [Errno %d] no locks available" % (
            errno.ENOLCK
        )
        await _close(engine)
        engine = await _basic(tmp_path / "other")
        with monkeypatch.context() as patch:
            patch.setattr(runner_archive, "SqliteStore", no_store)
            await _round(engine)
        assert engine.archive_view()["reason"] == "StoreError: the live store does not open"
        await _drain(engine)
        assert _store(engine).first_seq() == 1
        await _close(engine)

    _run(scenario)


def test_a_verified_range_that_begins_above_the_first_seq_owes_nothing(tmp_path: Path) -> None:
    """The loop's own check: a verified range must begin at or below the live store's first
    seq, or rows below it are unverified; such a report stops the archive."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        engine._prune_owed = None
        engine.archive_report(Report(engine._archive_round, verified=(2, _BACKLOG)))
        assert engine._prune_owed is None and engine._archive_verified is None
        assert engine.archive_view()["reason"] == (
            "a verified range begins at seq 2, above the live store's first seq 1: the rows"
            " below it are not verified"
        )
        await _drain(engine)
        assert _store(engine).first_seq() == 1
        await _close(engine)

    _run(scenario)


def test_an_engine_not_armed_by_dsl41_run_says_its_archive_is_off_and_why(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=_settings(),
        )
        _STARTED.append(engine)
        assert engine.archive_view() == {"state": "off", "reason": runner.UNARMED}
        await _close(engine)

    _run(scenario)


_TABLES = ("roots", "records", "transitions", "job_runs", "job_records", "configs", "coverage")


def _dump(run_root: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every archive table, in key order."""
    with closing(_archive(run_root)) as db:
        return {table: _rows(db, f"SELECT * FROM {table} ORDER BY 1, 2, 3") for table in _TABLES}


async def _copied_not_deleted(run_root: Path) -> None:
    """A run of `j` and three setters, copied and verified by one round; the engine ends
    before its delete."""
    engine = _start(run_root, adapter=FakeAdapter({("j", 1): (1.0, 0)}, default=None))
    await _job_backlog(engine)
    await _round(engine)
    assert _store(engine).first_seq() == 1
    await _close(engine)


def _alter(run_root: Path, sql: str) -> None:
    with closing(sqlite3.connect(run_root / "archive.db")) as db:
        db.execute(sql)
        db.commit()


async def _restarted_round(run_root: Path, settings: EngineSettings | None = None) -> Engine:
    """The restarted engine's round over the altered archive, and the loop after it. The
    engine stays open for the caller's checks and its close."""
    engine = await _resume(run_root, T0 + timedelta(days=2, minutes=5), settings)
    await _round(engine)
    await _drain(engine)
    return engine


def test_a_derived_row_where_the_live_coverage_claims_nothing_is_no_conflict(
    tmp_path: Path,
) -> None:
    """The archive holds the first batch's derived sets; the live store's coverage then names
    those seqs as a gap, so its read omits them. Where the live store claims nothing no set
    is taken and the observations stay as they were, sets and all: the round works and
    prunes."""
    run_root = tmp_path / "run"
    _run(lambda: _copied_not_deleted(run_root))
    query = "SELECT * FROM observations WHERE seq <= ? ORDER BY id"
    with closing(_archive(run_root)) as db:
        before = _rows(db, query, _STEP)
        sets = _rows(db, "SELECT COUNT(*) FROM sets")
    assert len(before) == _STEP and all(row[5] is not None for row in before)
    with closing(SqliteStore(run_root / STORE_NAME)) as store:
        store.cover_transitions([Interval(1, _STEP, False)])
        store.cover_history([Interval(1, _STEP, False)])

    async def scenario() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=5))
        bound = await _round(engine)
        await _drain(engine)
        assert engine.archive_view()["state"] == "working"
        assert _store(engine).first_seq() == bound
        await _close(engine)

    _run(scenario)
    with closing(_archive(run_root)) as db:
        after = _rows(db, query, _STEP)
        assert set(before) <= set(after)  # never changed; one that claims less sits beside
        assert all(row[5:8] == (None, None, None) for row in set(after) - set(before))
        assert _rows(db, "SELECT COUNT(*) FROM sets")[0][0] >= sets[0][0]


def test_nothing_is_owed_past_the_first_batch_before_the_second_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the worker held in its second batch, the first batch's report owes seqs 1 to 3
    and no more: the loop deletes those and stops there. Released, the rest is verified and
    deleted."""
    run_root = tmp_path / "run"
    hold = _Hold(monkeypatch, nth=2)

    async def scenario() -> None:
        engine = await _basic(run_root)
        store = _store(engine)
        engine.take_snapshot()
        await hold.reached()
        assert engine._prune_owed == _STEP + 1
        await _drain(engine)
        assert store.first_seq() == _STEP + 1
        hold.release.set()
        await _settled(engine)
        await _drain(engine)
        assert store.first_seq() == engine._archive_bound
        await _close(engine)

    _run(scenario)


def test_an_operator_holding_the_lock_keeps_the_round_out(tmp_path: Path) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        fd = os.open(run_root / "archive.db.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await _round(engine)
            assert engine.archive_view()["state"] == "stopped"
        finally:
            os.close(fd)
        bound = await _round(engine)
        await _drain(engine)
        assert engine.archive_view()["state"] == "working"
        assert _store(engine).first_seq() == bound
        await _close(engine)

    _run(scenario)


def test_a_configuration_is_read_once_in_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first batch reads rev 1's bytes in its live snapshot and compares them; the later
    batches of the run read them again from neither store."""
    run_root = tmp_path / "run"
    asked: list[list[int]] = []
    real = SqliteStore.raw_batch

    def raw_batch(store: SqliteStore, *args: Any) -> Any:
        batch = real(store, *args)
        if batch.records:
            asked.append([rev for rev, _, _ in batch.config_data])
        return batch

    monkeypatch.setattr(SqliteStore, "raw_batch", raw_batch)

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        await _drain(engine)
        _later(engine, days=2)
        await _round(engine)
        await _close(engine)

    _run(scenario)
    assert asked[0] == [1] and all(revs == [] for revs in asked[1:]) and len(asked) > 2


def test_a_fresh_root_writes_no_archive_key(tmp_path: Path) -> None:
    """Absent keys are the defaults, so the build before the archive reads the file; a key
    set to another value is written."""
    from dsl41.runner_config import load_settings

    load_settings(tmp_path)
    text = (tmp_path / "engine.toml").read_text()
    assert "archive_days" not in text and "archive_path" not in text
    assert "archive_days = 7\n" in EngineSettings(archive_days=7).to_toml()


def test_a_stop_during_expiry_takes_no_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "run"
    real = Archive._expire_variants
    compacted: list[int] = []

    async def scenario() -> None:
        engine = await _with_expired_copies(run_root)
        worker = engine.archive
        assert worker is not None

        def stopping(archive: Archive, keys: Any) -> None:
            real(archive, keys)
            worker.stop()  # the shutdown asks while the delete runs

        monkeypatch.setattr(Archive, "_expire_variants", stopping)
        monkeypatch.setattr(Archive, "compact", lambda archive: compacted.append(1))
        engine.take_snapshot()
        assert await asyncio.to_thread(worker.join, _WAIT_S)
        assert compacted == []
        await _close(engine)

    _run(scenario)


def test_the_engine_says_once_when_the_archive_stops_and_once_when_it_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        os.chmod(run_root / "archive.db", 0o640)
        await _round(engine)
        await _round(engine)
        os.chmod(run_root / "archive.db", 0o600)
        await _round(engine)
        await _round(engine)
        await _close(engine)

    capsys.readouterr()
    _run(scenario)
    lines = [line for line in capsys.readouterr().err.splitlines() if "archive" in line]
    assert len(lines) == 2, lines
    assert lines[0].startswith("dsl41: WARNING: the archive stopped, so pruning waits: ")
    assert "mode 0640" in lines[0]
    assert lines[1] == "dsl41: the archive works again; pruning goes on (DL-545)"


def _saved(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def test_a_file_with_two_hard_links_is_written_through_neither(tmp_path: Path) -> None:
    """Each name of a hard-linked file would take its own lock: both engines are refused,
    and the file is left as it was."""
    shared = tmp_path / "shared.db"

    async def scenario() -> None:
        one = await _basic(tmp_path / "one")
        arm_archive(one, tmp_path / "one", _settings(archive_path=str(shared)))
        await _round(one)  # the file exists, with one link
        os.link(shared, tmp_path / "second.db")
        before = _dump_file(shared)
        two = await _basic(tmp_path / "two")
        arm_archive(two, tmp_path / "two", _settings(archive_path=str(tmp_path / "second.db")))
        _later(one, minutes=5)
        for engine in (one, two):
            await _round(engine)
            assert "the archive file has 2 hard links" in engine.archive_view()["reason"]
        assert _dump_file(shared) == before
        await _close(one)
        await _close(two)

    _run(scenario)


def _dump_file(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return {table: _rows(db, f"SELECT * FROM {table} ORDER BY 1, 2, 3") for table in _TABLES}


def test_a_copy_is_stamped_once_in_a_run(tmp_path: Path) -> None:
    """Two rounds over a backlog the loop has not deleted: the second verifies the same
    copies again and leaves their `archived_at` as the first wrote it."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        with closing(_archive(run_root)) as db:
            first = _rows(db, "SELECT seq, archived_at FROM records ORDER BY seq")
        _later(engine, minutes=5)
        await _round(engine)
        with closing(_archive(run_root)) as db:
            again = _rows(db, "SELECT seq, archived_at FROM records ORDER BY seq")
        assert again[: len(first)] == first and len(again) > len(first)
        await _close(engine)

    _run(scenario)


def test_a_copy_the_live_store_still_holds_does_not_expire(tmp_path: Path) -> None:
    """Copies past `archive_days` by their own stamp: those below the live store's first seq
    go, those the live store still holds stay."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root, _settings(archive_days=1))
        await _backlog(engine, 7)
        store = _store(engine)
        await _round(engine)
        engine._prune_step(7)
        engine._prune_step(7)
        engine._prune_owed = None
        assert store.first_seq() == 7
        _alter(
            run_root, f"UPDATE records SET archived_at = '{(T0 - timedelta(days=10)).isoformat()}'"
        )
        _later(engine, minutes=5)
        await _round(engine)  # the copies were stamped in this run: not again
        with closing(_archive(run_root)) as db:
            kept = [seq for (seq,) in db.execute("SELECT seq FROM records ORDER BY seq")]
        assert kept[0] == 7 and set(range(7, _BACKLOG + 1)) <= set(kept)
        await _close(engine)

    _run(scenario)


def _files(path: Path) -> dict[str, bytes]:
    """The bytes of a database and of its sidecars that exist."""
    return {
        end: Path(f"{path}{end}").read_bytes()
        for end in ("", "-wal", "-shm")
        if Path(f"{path}{end}").exists()
    }


async def _pruned_then_copied(engine: Engine) -> None:
    """Rows 1 to 15 verified and deleted; four more inputs, two days on, copied and verified
    by a round whose delete is not taken."""
    await _backlog(engine, 7)
    await _round(engine)
    await _drain(engine)
    assert _store(engine).first_seq() == _BACKLOG + 1
    for n in range(4):
        engine.inject(_setter(n, engine.clock.now()), request_id=f"later-{n}")
    await _drain(engine)
    _later(engine, days=2)
    await _round(engine)


@pytest.mark.parametrize("days", [1, 550])
def test_a_successor_root_never_expires_a_predecessors_rows(tmp_path: Path, days: int) -> None:
    """Two roots in one archive: the second engine, whatever its `archive_days`, expires its
    own rows only; the first root's copies stay however old they are."""
    shared = _settings(archive_path=str(tmp_path / "shared.db"))

    async def scenario() -> None:
        old = _start(tmp_path / "old", _settings(archive_path=shared.archive_path, archive_days=1))
        await _backlog(old, 7)
        await _round(old)
        await _drain(old)
        await _close(old)
        new = _start(
            tmp_path / "new", _settings(archive_path=shared.archive_path, archive_days=days)
        )
        await _backlog(new, 7)
        _later(new, days=1000)
        await _round(new)
        await _round(new)
        with closing(sqlite3.connect(f"file:{tmp_path / 'shared.db'}?mode=ro", uri=True)) as db:
            assert _rows(db, "SELECT COUNT(*) FROM records WHERE root_id = ?", old.root_id) == [
                (_BACKLOG,)
            ]
        await _close(new)

    _run(scenario)


@pytest.mark.parametrize(("days", "kept"), [(30, False), (550, True)])
def test_the_current_archive_days_applies_after_a_round_with_nothing_to_copy(
    tmp_path: Path, days: int, kept: bool
) -> None:
    """Fifteen copies made under 30 days; the restart on day 40 has nothing to copy, as a
    raised retention keeps every live row. Its expiry reads the setting it runs with: 550
    keeps the copies, 30 does not."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        await _round(engine)
        await _drain(engine)
        await _close(engine)

    _run(first)

    async def second() -> None:
        engine = await _resume(
            run_root, T0 + timedelta(days=40), _settings(archive_days=days, retention_days=90)
        )
        await _round(engine)
        assert engine.archive_view()["state"] == "working"
        await _close(engine)

    _run(second)
    with closing(_archive(run_root)) as db:
        held = _rows(db, "SELECT COUNT(*) FROM records")
    assert held == [(_BACKLOG if kept else 0,)]


# ---- the model (DL-545): content keys, observations, lineage, and its rulings

_DATA = (
    "roots",
    "records",
    "sets",
    "transitions",
    "job_runs",
    "job_records",
    "configs",
    "histories",
    "lists",
    "list_entries",
    "observations",
    "coverage",
)


def _contents(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every archive table, in a fixed order."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return {table: _rows(db, f"SELECT * FROM {table} ORDER BY 1, 2, 3") for table in _DATA}


_ROOT = {"root_id": "r", "state_version": "1"}


def _line(path: Path, records: Sequence[tuple[str, str]], *, history: str | None = "h1") -> None:
    """A live store at `path` written as a line of history: each record an engine event of
    `(event, at)`."""
    store = SqliteStore(path, identity=_ROOT)
    try:
        if history is not None and not store.histories():
            store.begin_history(history)
        for event, at in records:
            store.append(Record(datetime.fromisoformat(at), "engine", None, None, {"event": event}))
    finally:
        store.close()


def _put(archive: Archive, batch: Any, at: datetime = T0) -> list[str]:
    """One batch or empty round written from its image and read again, as a round does."""
    image = runner_archive._image(batch, _ROOT, runner_archive._stamp(at))
    repaired = archive.take(image)
    archive.check(image)
    return repaired


def _take(live: Path, archive_path: Path, at: datetime = T0) -> list[str]:
    """One batch over the whole live store."""
    with closing(SqliteStore(live, read_only=True)) as store:
        batch = store.raw_batch(store.first_seq(), store.last_seq() + 1, 10**6)
    archive = Archive(archive_path, _ROOT, lambda: at)
    try:
        return _put(archive, batch, at)
    finally:
        archive.close()


_PAYLOAD = b'{"index":5,"kind":"STARTJOB","payload":{"job":"a"}}'


def _batch(**changes: Any) -> Any:
    """A batch of one STARTJOB input at seq 5, every table's coverage claiming the seq."""
    from dsl41.runner_store import RawBatch

    base = RawBatch(
        first_seq=5,
        records=[(5, "2026-07-01T08:00:00.000000", "input", "control", "rid", _PAYLOAD)],
        transitions=[],
        job_runs=[],
        job_records=[],
        transitions_coverage=[Interval(1, 5, True)],
        history_coverage=[Interval(1, 5, True)],
        configs=[],
        histories=[("h1", 1)],
        last_seq=5,
    )
    return dataclasses.replace(base, **changes)


def _applying(seq: int, rev: int, **changes: Any) -> Any:
    payload = json.dumps({"event": "applied", "rev": rev}).encode()
    return _batch(
        first_seq=seq,
        records=[(seq, "2026-07-01T08:00:00.000000", "config", None, None, payload)],
        last_seq=seq,
        **changes,
    )


def test_c1_a_seq_with_other_bytes_is_a_second_row_and_the_same_bytes_share_one(
    tmp_path: Path,
) -> None:
    """Two lines differ at seq 2 and agree at seq 1: seq 1 is one row, seq 2 two rows, and
    the first row of seq 2 is as it was. NULL and empty text, and two request ids, give
    different digests."""
    one, two, archive = tmp_path / "one.db", tmp_path / "two.db", tmp_path / "a.db"
    _line(one, [("a", "2026-07-01T08:00:00"), ("b", "2026-07-01T08:00:01")])
    _line(two, [("a", "2026-07-01T08:00:00"), ("c", "2026-07-01T08:00:01")])
    _take(one, archive)
    first = _contents(archive)["records"]
    _take(two, archive)
    held = _contents(archive)["records"]
    assert [row[1] for row in held] == [1, 2, 2]
    assert all(row in held for row in first)
    assert record_digest((1, "t", "k", None, None, b"x")) != record_digest(
        (1, "t", "k", "", None, b"x")
    )
    assert record_digest((1, "t", "k", None, "a", b"x")) != record_digest(
        (1, "t", "k", None, "b", b"x")
    )


def test_c1_two_rows_that_differ_only_in_request_id_are_two_rows(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        for rid in ("one", "two"):
            records = [(5, "2026-07-01T08:00:00.000000", "input", "control", rid, _PAYLOAD)]
            _put(archive, _batch(records=records))
    finally:
        archive.close()
    assert sorted(row[6] for row in _contents(tmp_path / "a.db")["records"]) == ["one", "two"]


def test_r7_c2_a_complete_empty_set_is_a_constant_and_two_sets_are_two_observations(
    tmp_path: Path,
) -> None:
    """The same STARTJOB input on two lines: after ON_HOLD it made no transition, after
    ON_NOEXEC it made one and opened a run. The empty sets are the constant, with no rows;
    the non-empty ones are rows in numeric key order. Two observations of one row, and
    nothing stops."""
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        held = _batch()
        bypass = _batch(
            transitions=[
                (5, 10, "a", "STARTING->RUNNING", b'{"n":10}'),
                (5, 2, "a", "INACTIVE->STARTING", b'{"n":2}'),
            ],
            job_runs=[(5, 2, "a", 1, 1)],
        )
        for batch in (held, bypass, held):
            _put(archive, batch)
    finally:
        archive.close()
    data = _contents(tmp_path / "a.db")
    assert len(data["records"]) == 1 and len(data["observations"]) == 2
    assert sorted((row[1], row[4]) for row in data["sets"]) == [
        ("job_runs", 1),
        ("transitions", 2),
    ]
    assert [row[3] for row in data["transitions"]] == [2, 10]
    empty = [row for row in data["observations"] if row[5] == runner_archive.EMPTY_SET]
    assert len(empty) == 1 and empty[0][6:8] == (b"", b"")


def test_c2_two_sets_of_one_size_are_two_digests() -> None:
    one = [(5, 0, "a", "x", b"1")]
    two = [(5, 0, "a", "y", b"1")]
    assert set_digest(one) != set_digest(two)
    assert set_digest([(5, 10, "a", "x", b"1"), (5, 2, "a", "y", b"1")]) == set_digest(
        [(5, 2, "a", "y", b"1"), (5, 10, "a", "x", b"1")]
    )


def test_c2_a_seq_the_live_coverage_does_not_claim_stores_no_set(tmp_path: Path) -> None:
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        _put(archive, _batch(history_coverage=[Interval(1, 5, False)]))
    finally:
        archive.close()
    data = _contents(tmp_path / "a.db")
    assert data["sets"] == []
    [observation] = data["observations"]
    assert observation[5] == b"" and observation[6] is None and observation[7] is None


def test_r2_an_observation_is_never_changed_and_no_pair_is_made_up(tmp_path: Path) -> None:
    """Sets A and B, each with no claim on the run openings, then a claim on the openings
    with no claim on the transitions: three observations, as seen, and no pair that was
    never seen together."""
    a = [(5, 0, "a", "x", b"A")]
    b = [(5, 0, "a", "y", b"B")]
    gap = [Interval(1, 5, False)]
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        _put(archive, _batch(transitions=a, history_coverage=gap))
        _put(archive, _batch(transitions=b, history_coverage=gap))
        _put(
            archive,
            _batch(job_runs=[(5, 0, "other", 1, 1)], transitions_coverage=gap),
        )
    finally:
        archive.close()
    seen = {row[5:8] for row in _contents(tmp_path / "a.db")["observations"]}
    opening = set_digest([(5, 0, "other", 1, 1)])
    assert seen == {
        (set_digest(a), None, None),
        (set_digest(b), None, None),
        (None, opening, b""),
    }


def test_r3_a_record_that_applies_a_configuration_is_bound_to_rev_and_digest(
    tmp_path: Path,
) -> None:
    """The binding names the rev and the digest the live store held for it; a rev the live
    store does not hold says so."""
    data = b'{"v":2}'
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        _put(
            archive,
            _applying(
                5,
                2,
                configs=[(2, config_digest(data))],
                config_data=[(2, config_digest(data), data)],
            ),
        )
        _put(archive, _applying(6, 9))
    finally:
        archive.close()
    bindings = sorted(row[2:3] + row[8:11] for row in _contents(tmp_path / "a.db")["observations"])
    assert bindings == [
        (5, 2, config_digest(data), runner_archive.BOUND),
        (6, 9, None, runner_archive.UNHELD_REV),
    ]
    assert _view(
        tmp_path / "a.db", "SELECT seq, state, rev, digest, binding, data FROM line_configurations"
    ) == [
        (5, "on line", 2, config_digest(data), runner_archive.BOUND, '{"v":2}'),
        (6, "on line", 9, None, runner_archive.UNHELD_REV, None),
    ]
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:  # the same record seen where the live store holds rev 9: the two disagree
        held = [(9, config_digest(data))]
        _put(archive, _applying(6, 9, configs=held, config_data=[(*held[0], data)]))
    finally:
        archive.close()
    assert _view(
        tmp_path / "a.db", "SELECT state, binding FROM line_configurations WHERE seq = 6 ORDER BY 2"
    ) == [("line not known", runner_archive.BOUND), ("line not known", runner_archive.UNHELD_REV)]


def test_r1_a_configuration_is_kept_and_deleted_by_rev_and_digest(tmp_path: Path) -> None:
    """Revisions 1 and 3 with the same bytes, a different 2 between them. Once the record
    that applied 1 expires and the live store no longer holds 1, only 1 goes; 3 stays."""
    same, other = b'{"v":"same"}', b'{"v":"other"}'
    configs = [(1, config_digest(same)), (2, config_digest(other)), (3, config_digest(same))]
    data = [
        (rev, digest, same if digest == config_digest(same) else other) for rev, digest in configs
    ]
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _applying(5, 1, configs=configs, config_data=data))
    finally:
        archive.close()
    later = Archive(path, _ROOT, lambda: T0 + timedelta(days=40))
    try:
        later.expire(30, 6, set(configs[1:]), 100, lambda: True)
    finally:
        later.close()
    assert sorted(row[1] for row in _contents(path)["configs"]) == [2, 3]


def test_r1_an_applying_record_is_verified_only_while_its_configuration_is_there(
    tmp_path: Path,
) -> None:
    """For a revision this run read and for one it cached: with the configuration gone after
    the commit, the read again fails, so neither can make a report."""
    data = b'{"v":3}'
    key = (3, config_digest(data))
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        fresh = runner_archive._image(
            _applying(5, 3, configs=[key], config_data=[(*key, data)]), _ROOT, "t1"
        )
        archive.take(fresh)
        _alter_file(path, "DELETE FROM configs")
        with pytest.raises(
            runner_archive.ArchiveStopped, match="after the commit: its (configs|bound) differ"
        ):
            archive.check(fresh)
        cached = runner_archive._image(_applying(6, 3, configs=[key]), _ROOT, "t2")
        assert cached.configs == {} and cached.bound == {key}
        with pytest.raises(
            runner_archive.ArchiveStopped, match="inside the transaction: its bound"
        ):
            archive.take(cached)
    finally:
        archive.close()


def _alter_file(path: Path, sql: str) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(sql)
        db.commit()


def test_c3_configurations_by_rev_and_digest_and_a_bad_body_stops(tmp_path: Path) -> None:
    """A configuration never applied is archived, also in a round with nothing to copy; a
    rev with other bytes is a second row; a live body that does not match its digest stops
    with nothing stored."""
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        for data in (b'{"v":1}', b'{"v":2}'):
            key = (1, config_digest(data))
            _put(archive, _batch(records=[], configs=[key], config_data=[(*key, data)]))
        before = _contents(path)
        bad = _batch(
            configs=[(2, config_digest(b"{}"))],
            config_data=[(2, config_digest(b"{}"), b'{"other":1}')],
        )
        with pytest.raises(runner_archive.ArchiveStopped, match="rev 2 does not match its digest"):
            _put(archive, bad)
    finally:
        archive.close()
    after = _contents(path)
    assert after == before and sorted(row[1] for row in after["configs"]) == [1, 1]


def test_c4_two_rows_under_one_seq_and_history_are_both_observed(tmp_path: Path) -> None:
    """The failed start: the live store is put back to a copy through seq 2, a start whose
    history row failed writes seq 3 under the earlier history, and the next start begins at
    4. Seq 3 has two observations under that history, one of each row."""
    live, older, archive = tmp_path / "live.db", tmp_path / "older.db", tmp_path / "a.db"
    _line(live, [("a", "2026-07-01T08:00:00"), ("b", "2026-07-01T08:00:01")])
    shutil.copyfile(live, older)
    _line(live, [("c", "2026-07-01T08:00:02")])
    _take(live, archive)
    live.unlink()
    shutil.copyfile(older, live)
    _line(live, [("d", "2026-07-01T08:00:03")])  # no history row: it failed
    store = SqliteStore(live)
    store.begin_history("h2", 4)
    store.close()
    _take(live, archive)
    observed = [row for row in _contents(archive)["observations"] if row[2] == 3]
    assert len(observed) == 2 and observed[0][3] == observed[1][3] == 1  # both under h1
    assert observed[0][4] != observed[1][4]


def test_r12_a_seq_before_every_history_has_none(tmp_path: Path) -> None:
    """A root older than DL-536: its first history begins after the retained seq."""
    live, archive = tmp_path / "live.db", tmp_path / "a.db"
    _line(live, [("a", "2026-07-01T08:00:00")], history=None)
    store = SqliteStore(live)
    store.begin_history("later", 5)
    store.close()
    _take(live, archive)
    [observation] = _contents(archive)["observations"]
    assert observation[3] is None
    assert _view(archive, "SELECT seq, history, state FROM current_line ORDER BY seq") == [
        (1, None, "line not known")
    ]
    assert _view(archive, "SELECT seq, history, state FROM line_records") == [
        (1, None, "line not known")
    ]
    assert _view(archive, "SELECT seq FROM line_not_known") == [(1,)]


def test_c5_a_fault_at_any_statement_of_the_batch_transaction_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each statement of the batch's transaction, the writes, the read back inside it and the
    commit, fails in turn: the archive stops with the failure and holds nothing of it."""
    real_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if "archive.db" in str(database):
            kwargs["factory"] = _FaultyConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(runner, "PRUNE_STEP", 1000)  # the backlog is one batch
    statements = [
        "INSERT OR IGNORE INTO roots",
        "INSERT INTO histories",
        "INSERT INTO lists",
        "INSERT INTO list_entries",
        "INSERT OR IGNORE INTO configs",
        "INSERT OR IGNORE INTO records",
        "INSERT INTO sets",
        "INSERT INTO transitions",
        "INSERT INTO job_runs",
        "INSERT INTO job_records",
        "INSERT INTO observations",
        "DELETE FROM coverage",
        "INSERT INTO coverage",
        "SELECT state_version",
        "SELECT data FROM configs",
        "SELECT size FROM sets",
        "SELECT 1 FROM observations",
        "SELECT version, last_seq FROM lists",
    ]
    for n, statement in enumerate(statements):
        run_root = tmp_path / f"run{n}"
        _run(lambda run_root=run_root: _copied_not_deleted_under(run_root, _Fault(statement)))
        data = _contents(run_root / "archive.db")
        assert all(rows == [] for rows in data.values()), (statement, data)
    run_root = tmp_path / "commit"
    _run(
        lambda: _copied_not_deleted_under(
            run_root, _Fault("COMMIT", after="INSERT INTO observations")
        )
    )
    assert all(rows == [] for rows in _contents(run_root / "archive.db").values())


async def _copied_not_deleted_under(run_root: Path, fault: _Fault) -> None:
    """The job backlog's first round under `fault`: it must fire and stop the archive."""
    engine = _start(run_root, adapter=FakeAdapter({("j", 1): (1.0, 0)}, default=None))
    await _job_backlog(engine)
    _FAULT.append(fault)
    try:
        await _round(engine)
    finally:
        _FAULT.clear()
    await _drain(engine)
    assert fault.fired and engine.archive_view()["state"] == "stopped"
    assert _store(engine).first_seq() == 1
    await _close(engine)


_TRIGGERS = {
    "coverage-dropped": "CREATE TRIGGER fault AFTER INSERT ON coverage BEGIN DELETE FROM"
    " coverage WHERE root_id = new.root_id AND first_seq = new.first_seq; END",
    "stamp-1900": "CREATE TRIGGER fault AFTER INSERT ON records BEGIN UPDATE records SET"
    " archived_at = '1900-01-01T00:00:00.000000' WHERE root_id = new.root_id"
    " AND seq = new.seq AND digest = new.digest; END",
    # a false covered seq written with the coverage: the expected coverage is the one
    # computed before the first write, so the read back finds it
    "false-covered": "CREATE TRIGGER fault AFTER INSERT ON coverage WHEN new.first_seq != 999"
    " BEGIN INSERT INTO coverage VALUES (new.root_id, 999, 999, 'covered'); END",
    "row-garbled": "CREATE TRIGGER fault AFTER INSERT ON records BEGIN UPDATE records SET"
    " payload = '{}' WHERE root_id = new.root_id AND seq = new.seq AND digest = new.digest; END",
}


@pytest.mark.parametrize("trigger", sorted(_TRIGGERS))
def test_r4_a_write_that_differs_from_the_image_rolls_back_and_stops(
    tmp_path: Path, trigger: str
) -> None:
    """A fault in the archive alters what the batch writes: the image read back inside the
    transaction differs, the transaction rolls back, and five rounds of it leave every table
    as it was, with nothing pruned."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        for n in range(7):
            engine.inject(_setter(n, T0), request_id=f"r-{n}")
        await engine.run_until_quiescent(T0)
        await _round(engine)  # nothing past the retention yet: an empty round
        _alter(run_root, _TRIGGERS[trigger])
        _later(engine, days=2)
        before = _contents(run_root / "archive.db")
        for _ in range(5):
            _later(engine, minutes=5)
            await _round(engine)
            await _drain(engine)
        reason = engine.archive_view()["reason"]
        assert reason.startswith("the archive does not read back as the batch's image inside")
        assert _contents(run_root / "archive.db") == before
        assert _store(engine).first_seq() == 1 and engine._prune_owed is None
        await _close(engine)

    _run(scenario)


def test_r4_an_empty_round_is_verified_too(tmp_path: Path) -> None:
    """A fault that changes the recorded last seq in a round with nothing to copy fails the
    image's read back and rolls back."""
    live, path = tmp_path / "live.db", tmp_path / "a.db"
    _line(live, [("a", "2026-07-01T08:00:00")])
    _take(live, path)
    _line(live, [("b", "2026-07-01T08:00:01")])
    with closing(sqlite3.connect(live)) as db:
        db.execute("DELETE FROM journal")
        db.commit()
    _alter_file(
        path,
        "CREATE TRIGGER fault AFTER UPDATE ON lists BEGIN UPDATE lists SET last_seq = 777"
        " WHERE root_id = new.root_id AND version = new.version; END",
    )
    before = _contents(path)
    with pytest.raises(runner_archive.ArchiveStopped, match="its last_seq differ"):
        _take(live, path)
    assert _contents(path) == before


def test_a_damaged_record_stops_and_is_named_with_both_digests(tmp_path: Path) -> None:
    """A stored row whose bytes do not give its digest stops the worker at once; nothing is
    rewritten and nothing is pruned."""
    run_root = tmp_path / "run"
    _run(lambda: _copied_not_deleted(run_root))
    _alter(run_root, "UPDATE records SET payload = '{}' WHERE seq = 2")
    with closing(_archive(run_root)) as db:
        [(key, at, kind, actor, rid)] = _rows(
            db, "SELECT digest, at_utc, kind, actor, request_id FROM records WHERE seq = 2"
        )
    found = record_digest((2, at, kind, actor, rid, b"{}"))
    before = _contents(run_root / "archive.db")

    async def scenario() -> None:
        engine = await _restarted_round(run_root)
        assert engine.archive_view()["reason"] == (
            f"the archive's records row at seq 2 is damaged: expected digest {key.hex()},"
            f" found {found.hex()}, for root {engine.root_id}; pruning stops"
        )
        assert _store(engine).first_seq() == 1
        await _close(engine)

    _run(scenario)
    assert _contents(run_root / "archive.db") == before


def test_a_damaged_set_and_a_damaged_cached_configuration_stop_and_are_named(
    tmp_path: Path,
) -> None:
    rows = [(5, 0, "a", "x", b"A")]
    key = set_digest(rows)
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _batch(transitions=rows))
        _alter_file(path, "UPDATE transitions SET job = 'b'")
        found = set_digest([(5, 0, "b", "x", b"A")])
        with pytest.raises(runner_archive.ArchiveStopped) as stopped:
            _put(archive, _batch(transitions=rows))
        assert str(stopped.value) == (
            f"the archive's transitions set at seq 5 is damaged: expected digest {key.hex()},"
            f" found {found.hex()}, for root r; pruning stops"
        )
        _alter_file(path, "DELETE FROM sets")
        with pytest.raises(runner_archive.ArchiveStopped, match="has rows and no header"):
            _put(archive, _batch(transitions=rows))
    finally:
        archive.close()
    data = b'{"v":3}'
    cached = (3, config_digest(data))
    path = tmp_path / "b.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _applying(5, 3, configs=[cached], config_data=[(*cached, data)]))
        _alter_file(path, """UPDATE configs SET data = '{"v":4}'""")
        with pytest.raises(runner_archive.ArchiveStopped) as stopped:
            _put(archive, _applying(6, 3, configs=[cached]))  # cached: no bytes read
        assert str(stopped.value) == (
            f"the archive's configs row at rev 3 is damaged: expected digest {cached[1]},"
            f" found {config_digest(b'{"v":4}')}, for root r; pruning stops"
        )
    finally:
        archive.close()


def test_c5_a_crash_after_the_commit_and_before_the_report_is_verified_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first run commits a batch and dies at its read again: no report, nothing pruned.
    The next run reads the same rows, finds them, verifies, reports and prunes."""
    run_root = tmp_path / "run"
    real = Archive.check
    calls: list[int] = []

    def crashing(archive: Archive, *args: Any) -> None:
        calls.append(1)
        if len(calls) == 1:  # the read again after the first commit
            raise sqlite3.OperationalError("the process died here")
        real(archive, *args)

    monkeypatch.setattr(Archive, "check", crashing)

    async def first() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        await _drain(engine)
        assert engine._archive_verified is None and _store(engine).first_seq() == 1
        await _close(engine)

    _run(first)
    with closing(_archive(run_root)) as db:
        assert _rows(db, "SELECT COUNT(*) FROM records") == [(_STEP,)]  # committed, unreported

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=5))
        bound = await _round(engine)
        await _drain(engine)
        assert _store(engine).first_seq() == bound
        await _close(engine)

    _run(second)


def test_r11_the_read_after_the_commit_opens_a_connection_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_connect = sqlite3.connect

    def connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)
    try:
        image = runner_archive._image(_batch(), _ROOT, "t")
        archive.take(image)
        monkeypatch.setattr(sqlite3, "connect", connect)
        archive.check(image)
    finally:
        archive.close()
    assert len(opened) == 1 and opened[0].endswith("a.db?mode=ro")


def test_r6_c7_lineage_lists_extend_in_place_and_a_change_is_a_new_version(
    tmp_path: Path,
) -> None:
    """The list and the last seq are recorded with a batch and again in a round with nothing
    to copy. A list that extends the newest replaces it in place, ties in the live order and
    an empty history kept; a list that does not is a new version, the earlier one kept."""
    live, archive = tmp_path / "live.db", tmp_path / "a.db"
    _line(live, [("a", "2026-07-01T08:00:00")])
    _take(live, archive)
    store = SqliteStore(live)
    store.begin_history("empty", 2)
    store.begin_history("h3", 2)
    store.close()
    _line(live, [("b", "2026-07-01T08:00:01")])
    with closing(sqlite3.connect(live)) as db:  # pruned: the live store holds no row
        db.execute("DELETE FROM journal")
        db.commit()
    _take(live, archive)  # an empty round
    data = _contents(archive)
    names = {row[1]: row[2] for row in data["histories"]}

    def versions(data: dict[str, list[tuple[Any, ...]]]) -> dict[int, list[tuple[Any, ...]]]:
        found: dict[int, list[tuple[Any, ...]]] = {}
        for _, version, position, hid, first in data["list_entries"]:
            found.setdefault(version, []).append((position, names[hid], first))
        return found

    assert versions(data) == {1: [(0, "h1", 1), (1, "empty", 2), (2, "h3", 2)]}
    assert [row[3] for row in data["lists"]] == [2]
    with closing(sqlite3.connect(live)) as db:  # a restore drops the last two histories
        db.execute("DELETE FROM transitions_history WHERE history != 'h1'")
        db.commit()
    _take(live, archive)
    after = versions(_contents(archive))
    assert after[1] == versions(data)[1] and after[2] == [(0, "h1", 1)]


def test_r5_expiry_works_on_row_variants(tmp_path: Path) -> None:
    """Four eligible variants of seq 1 and a step limit of three: expiry makes progress and
    removes them all. An old variant beside a young one at one seq expires alone. Each kind
    of item goes by its rule: observations with their row, sets and histories nothing refers
    to; the newest list and the gaps stay."""
    path = tmp_path / "a.db"
    for n in range(4):
        archive = Archive(path, _ROOT, lambda: T0)
        try:
            records = [(1, "2026-07-01T08:00:00.000000", "engine", None, f"r{n}", b"{}")]
            _put(archive, _batch(first_seq=1, records=records, last_seq=1))
        finally:
            archive.close()
    young = Archive(path, _ROOT, lambda: T0 + timedelta(days=39))
    try:
        records = [(2, "2026-07-01T08:00:00.000000", "engine", None, "y", b"{}")]
        _put(young, _batch(first_seq=2, records=records, last_seq=2), T0 + timedelta(days=39))
        records = [(2, "2026-07-01T08:00:00.000000", "engine", None, "o", b"{}")]
    finally:
        young.close()
    _alter_file(
        path,
        "UPDATE records SET archived_at = '2026-06-01T00:00:00.000000' WHERE request_id = 'r0' OR request_id = 'r1' OR request_id = 'r2' OR request_id = 'r3'",
    )
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        records = [(2, "2026-07-01T08:00:00.000000", "engine", None, "o", b"{}")]
        _put(archive, _batch(first_seq=2, records=records, last_seq=2))
    finally:
        archive.close()
    later = Archive(path, _ROOT, lambda: T0 + timedelta(days=40))
    try:
        gone = later.expire(30, 3, set(), 3, lambda: True)
    finally:
        later.close()
    data = _contents(path)
    assert gone == 5
    assert [(row[1], row[6]) for row in data["records"]] == [(2, "y")]
    assert [row[2] for row in data["observations"]] == [2]
    assert data["lists"] and data["histories"]


def test_c9_a_copy_the_live_store_holds_stays_at_any_age(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _batch())
    finally:
        archive.close()
    later = Archive(path, _ROOT, lambda: T0 + timedelta(days=4000))
    try:
        assert later.expire(30, 5, set(), 100, lambda: True) == 0
    finally:
        later.close()
    assert len(_contents(path)["records"]) == 1


def test_r9_a_file_without_the_layout_is_refused_and_left_and_an_empty_one_is_taken(
    tmp_path: Path,
) -> None:
    viewed = tmp_path / "view.db"
    with closing(sqlite3.connect(viewed)) as db:
        db.execute("CREATE VIEW v AS SELECT 1")
        db.commit()
    os.chmod(viewed, 0o600)
    before = viewed.read_bytes()
    with pytest.raises(runner_archive.ArchiveStopped, match="is not a dsl41 archive"):
        Archive(viewed, _ROOT, lambda: T0)
    assert viewed.read_bytes() == before
    empty = tmp_path / "empty.db"
    empty.touch(mode=0o600)
    Archive(empty, _ROOT, lambda: T0).close()
    assert _contents(empty)["roots"] == []


def test_r8_the_archive_works_again_only_once_a_round_completed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = await _basic(run_root)
        await _round(engine)
        current = engine._archive_round
        engine.archive_report(Report(current, stopped="the disk is full"))
        engine.archive_report(Report(current, verified=(1, 2)))
        assert engine.archive_view()["state"] == "stopped" and engine._prune_owed == 3
        engine.archive_report(Report(current, completed=True, histories=1, last_seq=9))
        assert engine.archive_view() == {
            "state": "working",
            "verified_through": 2,
            "histories": 1,
            "last_seq": 9,
        }
        await _close(engine)

    capsys.readouterr()
    _run(scenario)
    err = capsys.readouterr().err
    assert err.count("the archive stopped") == 1 and err.count("works again") == 1


def test_the_first_error_is_the_one_reported(tmp_path: Path) -> None:
    """A statement that ended the transaction on its own does not hide the failure behind a
    rollback that has nothing to roll back."""
    archive = Archive(tmp_path / "a.db", _ROOT, lambda: T0)

    def failing() -> None:
        archive._db.execute("ROLLBACK")
        raise ValueError("the first error")

    try:
        with pytest.raises(ValueError, match="the first error"):
            archive._transaction(failing)
    finally:
        archive.close()


def test_c12_the_earlier_layouts_are_refused_by_name(tmp_path: Path) -> None:
    """The seq-keyed layout and the branch layout both wrote `layout_version 1`."""
    path = tmp_path / "a.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)", [("layout_version", "1"), ("oldest_reader", "1")]
        )
        db.commit()
    os.chmod(path, 0o600)
    with pytest.raises(runner_archive.ArchiveStopped) as refused:
        Archive(path, _ROOT, lambda: T0)
    assert str(refused.value) == (
        f"{path}: archive layout layout_version 1; this build writes dsl41-archive-content-1 only"
    )


# ---- reading: the current line and the views (DL-545)


def _view(path: Path, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return _rows(db, sql, *params)


def _extend(path: Path, last: int, line: str) -> None:
    """Engine events appended to the live store at `path` until its last seq is `last`, each
    naming its line and seq."""
    store = SqliteStore(path, identity=_ROOT)
    try:
        while store.last_seq() < last:
            seq = store.last_seq() + 1
            at = datetime(2026, 7, 1, 8) + timedelta(seconds=seq)
            store.append(Record(at, "engine", None, None, {"event": f"{line}-{seq}"}))
    finally:
        store.close()


def _begin(path: Path, history: str, first: int | None = None) -> None:
    store = SqliteStore(path)
    try:
        store.begin_history(history, first)
    finally:
        store.close()


def _restore(live: Path, copy: Path) -> None:
    live.unlink()
    shutil.copyfile(copy, live)


def _read(path: Path, seqs: Sequence[int]) -> dict[int, Any]:
    """Per seq: its history and state on the current line with the line and state of each
    record the default view shows, or None where the current line has no row."""
    seen: dict[int, Any] = {}
    for seq in seqs:
        line = _view(path, "SELECT history, state FROM current_line WHERE seq = ?", seq)
        records = _view(
            path,
            "SELECT history, state, json_extract(payload, '$.event') FROM line_records"
            " WHERE seq = ? ORDER BY observation",
            seq,
        )
        seen[seq] = None if not line else (*line[0], records)
    return seen


def test_c8_the_restore_walk_reads_the_current_line_after_each_step(tmp_path: Path) -> None:
    """Histories h1 at 1 and h2 at 50 through 80; the copy of 60 put back and h3 from 61 to
    90; the backup of 80 put back and h4 from 81. Seq 85 is above the last seq until h4
    writes it."""
    live, archive = tmp_path / "live.db", tmp_path / "a.db"
    copy60, backup80 = tmp_path / "copy60.db", tmp_path / "backup80.db"
    _begin(live, "h1")
    _extend(live, 49, "h1")
    _begin(live, "h2")
    _extend(live, 60, "h2")
    shutil.copyfile(live, copy60)
    _extend(live, 80, "h2")
    shutil.copyfile(live, backup80)
    _take(live, archive)

    def on(history: str, seq: int) -> tuple[str, str, list[tuple[str, str, str]]]:
        return (history, "on line", [(history, "on line", f"{history}-{seq}")])

    assert _read(archive, (55, 70, 85)) == {55: on("h2", 55), 70: on("h2", 70), 85: None}
    _restore(live, copy60)
    _begin(live, "h3")
    _extend(live, 90, "h3")
    _take(live, archive)
    assert _read(archive, (55, 70, 85)) == {55: on("h2", 55), 70: on("h3", 70), 85: on("h3", 85)}
    _restore(live, backup80)
    _begin(live, "h4")
    _extend(live, 83, "h4")
    _take(live, archive)
    assert _read(archive, (55, 70, 85)) == {55: on("h2", 55), 70: on("h2", 70), 85: None}
    _extend(live, 85, "h4")
    _take(live, archive)
    assert _read(archive, (55, 70, 85)) == {55: on("h2", 55), 70: on("h2", 70), 85: on("h4", 85)}
    assert _view(archive, "SELECT max(seq) FROM current_line") == [(85,)]
    assert _view(archive, "SELECT count(*) FROM line_not_known") == [(0,)]
    assert {
        history for (history,) in _view(archive, "SELECT history FROM all_records WHERE seq = 70")
    } == {"h2", "h3"}


def _dispatch(attempt: str, at: datetime) -> Record:
    return Record(at, "dispatch", None, None, {"job": "a", "run_number": 1, "attempt": attempt})


def _outcome(attempt: str, outcome: str, at: datetime) -> Record:
    payload = {"job": "a", "run_number": 1, "attempt": attempt, "outcome": outcome}
    return Record(at, "outcome", None, None, payload)


def _append(path: Path, *records: Record, **rows: Any) -> None:
    store = SqliteStore(path, identity=_ROOT)
    try:
        store.append(*records, **rows)
    finally:
        store.close()


_DISPATCHES = (
    "SELECT seq, state, history, attempt, outcome_seq, outcome FROM line_dispatches ORDER BY seq"
)


def test_c8_a_failed_start_reads_line_not_known_and_no_join_crosses_it(tmp_path: Path) -> None:
    """The live store put back to its copy through seq 2; a start whose history row failed
    writes seq 3 under h1; the next start begins h2 at 4. Seq 3 shows both records, marked,
    and neither the dispatch below it nor the one at it is joined to an outcome."""
    live, older, archive = tmp_path / "live.db", tmp_path / "older.db", tmp_path / "a.db"
    at = datetime(2026, 7, 1, 8)
    _begin(live, "h1")
    _append(live, _dispatch("X", at))
    _extend(live, 2, "h1")
    shutil.copyfile(live, older)
    _append(live, _outcome("X", "natural", at))
    _take(live, archive)
    assert _view(archive, _DISPATCHES) == [(1, "on line", "h1", "X", 3, "natural")]
    _restore(live, older)
    _append(live, _dispatch("Y", at))  # no history row: it failed
    _begin(live, "h2")
    _append(live, _outcome("Y", "natural", at))
    _take(live, archive)
    assert _view(archive, "SELECT seq, history, state FROM current_line ORDER BY seq") == [
        (1, "h1", "on line"),
        (2, "h1", "on line"),
        (3, "h1", "line not known"),
        (4, "h2", "on line"),
    ]
    assert _view(archive, "SELECT kind, state FROM line_records WHERE seq = 3 ORDER BY 1") == [
        ("dispatch", "line not known"),
        ("outcome", "line not known"),
    ]
    assert _view(archive, _DISPATCHES) == [
        (1, "on line", "h1", "X", None, None),
        (3, "line not known", "h1", "Y", None, None),
    ]


def test_c8_a_dispatch_joins_its_outcome_on_the_current_line_only(tmp_path: Path) -> None:
    """A dispatch below the restore point, with its transition, run opening and owner, and
    its outcome above it on the line that was put back and on the line that replaced it."""
    live, older, archive = tmp_path / "live.db", tmp_path / "older.db", tmp_path / "a.db"
    at = datetime(2026, 7, 1, 8)
    _begin(live, "h1")
    _append(
        live,
        _dispatch("A", at),
        transitions=[{"job": "a", "transition": "STARTING->RUNNING"}],
        openings=[RunOpening(0, "a", 1, 3)],
        owners=[RecordOwner(0, "a", 1)],
    )
    _extend(live, 2, "h1")
    shutil.copyfile(live, older)
    _append(live, _outcome("A", "failure", at))
    _take(live, archive)
    assert _view(archive, _DISPATCHES) == [(1, "on line", "h1", "A", 3, "failure")]
    _restore(live, older)
    _begin(live, "h2")
    _append(live, _outcome("A", "success", at))
    _take(live, archive)
    assert _view(archive, _DISPATCHES) == [(1, "on line", "h1", "A", 3, "success")]
    assert _view(archive, "SELECT seq, history FROM all_records WHERE seq = 3 ORDER BY 2") == [
        (3, "h1"),
        (3, "h2"),
    ]
    assert _view(archive, "SELECT seq, state, job, transition FROM line_transitions") == [
        (1, "on line", "a", "STARTING->RUNNING")
    ]
    assert _view(archive, "SELECT seq, state, job, run_number, rev FROM line_run_openings") == [
        (1, "on line", "a", 1, 3)
    ]
    assert _view(archive, "SELECT seq, state, job, run_number FROM line_record_owners") == [
        (1, "on line", "a", 1)
    ]


def test_r2_a_claim_beside_a_fuller_equal_one_is_hidden_and_two_that_disagree_count(
    tmp_path: Path,
) -> None:
    """Seq 5 seen with transitions A and no claim on the history index, then with A and both
    history sets complete: the first is hidden. Seen with transitions B too: two count. An
    observation under another history hides nothing."""
    a = [(5, 0, "a", "x", b"A")]
    b = [(5, 0, "a", "y", b"B")]
    gap = [Interval(1, 5, False)]
    path = tmp_path / "a.db"

    def counted() -> list[tuple[Any, ...]]:
        return _view(
            path,
            "SELECT s.state, t.transition FROM current_line AS s"
            " LEFT JOIN line_transitions AS t USING (root_id, seq) WHERE seq = 5 ORDER BY 2",
        )

    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _batch(transitions=a, history_coverage=gap))
        assert counted() == [("on line", "x")]
        _put(archive, _batch(transitions=a))
        assert counted() == [("on line", "x")]
        assert _view(path, "SELECT record_owners FROM all_records ORDER BY observation") == [
            ("no claim",),
            ("empty",),
        ]
        _put(archive, _batch(transitions=b))
        assert counted() == [("line not known", "x"), ("line not known", "y")]
        _put(archive, _batch(transitions=a, history_coverage=gap, histories=[("h2", 1)]))
        assert counted() == [("on line", "x")]
        _put(archive, _batch(transitions=a, histories=[("h1", 1)]))
        assert counted() == [("line not known", "x"), ("line not known", "y")]
    finally:
        archive.close()


def test_c8_a_seq_in_a_gap_reads_not_archived(tmp_path: Path) -> None:
    """The live store's rows 1 to 3 were pruned before the first copy: the archive never
    received them, and the current line says so under the history that holds them."""
    live, archive = tmp_path / "live.db", tmp_path / "a.db"
    _begin(live, "h1")
    _extend(live, 5, "h1")
    with closing(sqlite3.connect(live)) as db:
        db.execute("DELETE FROM journal WHERE seq <= 3")
        db.commit()
    _take(live, archive)
    assert _view(archive, "SELECT seq, history, state FROM current_line ORDER BY seq") == [
        (1, "h1", "not archived"),
        (2, "h1", "not archived"),
        (3, "h1", "not archived"),
        (4, "h1", "on line"),
        (5, "h1", "on line"),
    ]
    assert _view(archive, "SELECT min(seq) FROM line_records") == [(4,)]


def test_c8_no_seq_above_the_last_seq_is_on_the_current_line(tmp_path: Path) -> None:
    """A list whose next history begins past the last seq: the earlier one ends at it."""
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _batch(histories=[("h0", 1), ("h1", 3), ("h2", 9)]))
    finally:
        archive.close()
    assert _view(path, "SELECT history, min(seq), max(seq) FROM current_line GROUP BY 1") == [
        ("h0", 1, 2),
        ("h1", 3, 5),
    ]


def test_c8_no_join_crosses_a_seq_that_reads_line_not_known_between_its_ends(
    tmp_path: Path,
) -> None:
    """A dispatch at 4 and its outcome at 7 are on line; the live store was put back to its
    copy through 4, and the start whose history row failed wrote another input at 5. The
    outcome is not joined across seq 5."""
    live, older, archive = tmp_path / "live.db", tmp_path / "older.db", tmp_path / "a.db"
    at = datetime(2026, 7, 1, 8)

    def tail(event: str) -> None:
        _append(live, Record(at, "input", "control", None, {"event": event}))
        _extend(live, 6, "h1")
        _append(live, _outcome("A", "natural", at))

    _begin(live, "h1")
    _extend(live, 3, "h1")
    _append(live, _dispatch("A", at))
    shutil.copyfile(live, older)
    tail("X")
    _take(live, archive)
    assert _view(archive, _DISPATCHES) == [(4, "on line", "h1", "A", 7, "natural")]
    _restore(live, older)
    tail("Y")  # no history row: it failed
    _take(live, archive)
    assert _view(archive, "SELECT seq, state FROM current_line WHERE seq >= 4 ORDER BY seq") == [
        (4, "on line"),
        (5, "line not known"),
        (6, "on line"),
        (7, "on line"),
    ]
    assert _view(archive, _DISPATCHES) == [(4, "on line", "h1", "A", None, None)]


def test_c8_a_configuration_is_read_by_its_rev_and_digest(tmp_path: Path) -> None:
    """Rev 2 held with two bodies, on two lines: each applying record shows its own body."""
    one, two = b'{"v":"one"}', b'{"v":"two"}'
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        for seq, data in ((5, one), (6, two)):
            held = [(2, config_digest(data))]
            _put(archive, _applying(seq, 2, configs=held, config_data=[(*held[0], data)]))
    finally:
        archive.close()
    assert _view(path, "SELECT seq, data FROM line_configurations ORDER BY seq") == [
        (5, one.decode()),
        (6, two.decode()),
    ]


def test_c8_a_seq_with_nothing_stored_reads_not_archived_under_any_history(
    tmp_path: Path,
) -> None:
    """The first history begins at 4 and only seq 5 is stored: seqs 1 to 3, which no history
    holds, and seq 4 read not archived."""
    path = tmp_path / "a.db"
    archive = Archive(path, _ROOT, lambda: T0)
    try:
        _put(archive, _batch(histories=[("h1", 4)]))
    finally:
        archive.close()
    assert _view(path, "SELECT seq, history, state FROM current_line ORDER BY seq") == [
        (1, None, "not archived"),
        (2, None, "not archived"),
        (3, None, "not archived"),
        (4, "h1", "not archived"),
        (5, "h1", "on line"),
    ]
