"""A start the engine decided is wanted until its run ends, and never after (DL-551, BUG-112,
BUG-113).

One fold over a batch's trace (`fold_runs`) says which runs the batch began and which it ended,
from the run numbers before the batch. Planning launches the starts it left open, in the order
the batch made them. A start that waits (behind its job's group or an update, deferred in
recovery, pending while draining) is discharged in the transaction that ends its run, so an
operator's `CHANGE_STATUS ... STARTING` afterwards launches nothing, then or at any restart.
Each scenario runs on a virtual clock; no sleep, no random data.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate
from store_surgery import rows
from test_engine_stops_cookie import PGID, _Procs
from test_update_atomic import _Ladders
from test_update_member_holds import SETTINGS, _adapters, _admit, _estate, _fw, _starts
from test_update_member_holds import _start as _start_estate
from test_update_owned_processes import _crash, _observed, _started, _teardown
from test_update_settlement import SIGNALED, _plan, _request
from test_update_watches import _launches
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import runner_admission
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import DecidedStart, RunFold, dispatch_plan, fold_runs
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import RuntimeProfile
from dsl41.runner_journal import STORE_NAME, replay_store
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import Record, SqliteStore
from dsl41.runner_update import EngineUpdate

T0 = datetime(2026, 7, 1, 8, 0)


def _entry(job: str, transition: str, run: int | None = None) -> TraceEntry:
    return TraceEntry(at=T0, job=job, transition=transition, cause="c", run_number=run)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ev(minutes: float, kind: str, **payload: object) -> Event:
    return Event(at=_at(minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


class _Counting(FakeAdapter):
    """A FakeAdapter that records every run it is asked to carry out."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, int]] = []

    async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
        self.calls.append((job_ir.name, run_number))
        return await super().run(job_ir, run_number, ctx)


def _kinds(run: Path, kind: str) -> list[tuple[int, dict[str, Any]]]:
    """The stored records of `kind` with their seqs."""
    return [(seq, body) for seq, found, body in rows(run) if found == kind]


def _dispatched(run: Path) -> list[tuple[str, int]]:
    return [(body["job"], body["run_number"]) for _, body in _kinds(run, "dispatch")]


def _genesis(run: Path) -> Any:
    """A replay of the root from genesis (`Replay`)."""
    with closing(SqliteStore(run / STORE_NAME, read_only=True)) as store:
        return replay_store(store, where="t").replay


def _start(run: Path, catalog: Any, adapter: FakeAdapter, **kwargs: Any) -> Engine:
    return start_run(
        catalog, run, clock=VirtualClock(start=T0), adapters={"CMD": adapter}, **kwargs
    )


async def _resume(run: Path, catalog: Any, adapter: FakeAdapter, minutes: float) -> Engine:
    return await resume_run(
        catalog, run, clock=VirtualClock(start=_at(minutes)), adapters={"CMD": adapter}
    )


async def _feed(engine: Engine, *events: Event) -> None:
    for event in events:
        engine.inject(event)
        await engine.run_until_quiescent(event.at)


async def _close(engine: Engine) -> None:
    journal = engine.journal
    assert journal is not None
    try:
        await engine.shutdown()
    finally:
        journal.close()


def _appends(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[str, dict[str, Any]]]]:
    """Every store append from now on, each one transaction: the kind and body of each record
    it commits."""
    appends: list[list[tuple[str, dict[str, Any]]]] = []
    real = SqliteStore.append

    def append(store: SqliteStore, *records: Record, **kwargs: Any) -> int:
        appends.append([(record.kind, dict(record.payload)) for record in records])
        return real(store, *records, **kwargs)

    monkeypatch.setattr(SqliteStore, "append", append)
    return appends


def _discharged_with(appends: list[list[tuple[str, dict[str, Any]]]], kind: str) -> list[int]:
    """The pending seqs discharged in a store append that also commits a record of `kind`."""
    return [
        body["pending"]
        for records in appends
        if any(found == kind for found, _ in records)
        for found, body in records
        if found == "dispatch_pending" and body["event"] == "discharged"
    ]


# ------------------------------------------------------------------ 1. the fold alone


def test_a_numbered_starting_opens_a_start_that_survives() -> None:
    fold = fold_runs([_entry("j", "INACTIVE->STARTING", 1)], {"j": 0})
    assert fold == RunFold((DecidedStart("j", 1, 0),), frozenset())


def test_a_start_whose_run_ends_later_in_the_batch_does_not_survive() -> None:
    """The run's own end closes its start and reports the run ended (BUG-112 B)."""
    entries = [
        _entry("j", "INACTIVE->STARTING", 1),
        _entry("j", "STARTING->TERMINATED"),
        _entry("j", "TERMINATED->STARTING"),  # an operator's STARTING, as the command half
    ]
    assert fold_runs(entries, {"j": 0}) == RunFold((), frozenset({("j", 1)}))


def test_two_unnumbered_ends_are_told_apart_by_the_seeded_run() -> None:
    """`j.1` runs at the batch's start; its end and the end of `j.2`, begun between them, write
    the same unnumbered entry. The seed names the first, the numbered start the second."""
    entries = [
        _entry("j", "RUNNING->INACTIVE"),
        _entry("j", "INACTIVE->STARTING", 2),
        _entry("j", "STARTING->RUNNING"),
        _entry("j", "RUNNING->INACTIVE"),
    ]
    assert fold_runs(entries, {"j": 1}) == RunFold((), frozenset({("j", 1), ("j", 2)}))
    assert fold_runs(entries[:1], {"j": 1}).ended == {("j", 1)}


def test_a_bypass_s_numbered_success_sets_the_run_and_opens_no_start() -> None:
    entries = [
        _entry("j", "SUCCESS->SUCCESS", 2),
        _entry("j", "SUCCESS->STARTING"),
        _entry("j", "STARTING->INACTIVE"),
    ]
    assert fold_runs(entries, {"j": 1}) == RunFold((), frozenset({("j", 2)}))


def test_the_four_moves_between_starting_and_running_close_nothing() -> None:
    """Numbered or not, in either direction, a move inside the live statuses ends no run."""
    entries = [
        _entry("j", "INACTIVE->STARTING", 1),
        _entry("j", "STARTING->RUNNING"),
        _entry("j", "RUNNING->RUNNING"),
        _entry("j", "RUNNING->STARTING"),
        _entry("j", "STARTING->STARTING"),
        _entry("j", "STARTING->RUNNING", 1),
    ]
    assert fold_runs(entries, {"j": 0}) == RunFold((DecidedStart("j", 1, 0),), frozenset())


def test_an_injected_starting_opens_nothing() -> None:
    entries = [_entry("j", "TERMINATED->STARTING"), _entry("j", "STARTING->RUNNING")]
    assert fold_runs(entries, {"j": 1}) == RunFold()


def test_markers_are_no_status_entries_and_positions_count_the_job_s_own() -> None:
    """A marker, numbered or not, is no status entry: a refused start carries its run's number
    and begins nothing. A start's position counts its own job's status entries only."""
    entries = [
        _entry("k", "INACTIVE->STARTING", 1),
        _entry("j", "START_REFUSED", 4),
        _entry("j", "SCHED_ARM"),
        _entry("j", "TERMINATED->INACTIVE"),
        _entry("k", "STARTING->RUNNING"),
        _entry("j", "INACTIVE->STARTING", 5),
    ]
    fold = fold_runs(entries, {"j": 4, "k": 0})
    assert fold == RunFold((DecidedStart("k", 1, 0), DecidedStart("j", 5, 1)), frozenset())


def test_a_job_with_no_row_before_the_batch_counts_from_run_zero() -> None:
    entries = [_entry("g", "STARTING->INACTIVE")]
    assert fold_runs(entries, {}).ended == {("g", 0)}


# ------------------------------------------------------------- 2. one mixed batch


#: `x` ends by its runtime limit at T0 + 1 minute; `j` and `k` start on it, and `j` again on its
#: own failure
_MIXED = (
    "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: y\ncondition: t(x) | f(j)\n\n"
    "insert_job: k\njob_type: c\ncommand: z\ncondition: t(x)\n"
)


@pytest.mark.parametrize("died", [False, True], ids=["live", "restart"])
def test_one_batch_that_opens_j1_k1_and_j2_launches_k1_then_j2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, died: bool
) -> None:
    """BUG-113: a FAILURE injected on `j` at `x`'s deadline shares the deadline's input, which
    starts `j.1` and `k.1`; the injection ends `j.1` and `f(j)` starts `j.2`. The launches are
    `k.1` then `j.2`, in the order the batch made them, live and in a replay. When the engine
    dies before either dispatch, the restart owes them and launches them in that order."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    catalog = lower_source(_MIXED)
    real = Engine._dispatch

    def dispatch(engine: Engine, applied: Any, selected: Any = None) -> None:
        if died and DecidedStart("j", 2, 3) in applied.starts:
            raise _Died
        real(engine, applied, selected)

    async def scenario() -> None:
        engine = _start(run, catalog, adapter)
        try:
            await _feed(engine, _ev(0, "STARTJOB", job="x"))
            with monkeypatch.context() as patch:
                patch.setattr(Engine, "_dispatch", dispatch)
                if died:
                    with pytest.raises(_Died):
                        await _feed(engine, _ev(1, "STATUS", job="j", status="FAILURE"))
                else:
                    await _feed(engine, _ev(1, "STATUS", job="j", status="FAILURE"))
            batch = [(e.job, e.transition, e.run_number) for e in engine.oracle.trace()]
            assert ("j", "FAILURE->STARTING", 2) in batch
            assert ("k", "INACTIVE->STARTING", 1) in batch
        finally:
            await _close(engine)
        if died:
            assert _dispatched(run) == [("x", 1)]
            restarted = await _resume(run, catalog, adapter, 3)
            try:
                await restarted.run_until_quiescent(_at(3))
            finally:
                await _close(restarted)

    asyncio.run(scenario())
    want = [("x", 1), ("k", 1), ("j", 2)]
    assert adapter.calls == want and _dispatched(run) == want
    replay = _genesis(run)
    assert replay.spawns == set(want)
    assert list(replay.unended) == [("k", 1), ("j", 2)]  # `x.1` ended at its deadline


# -------------------------------------------------- 3. a kill before the next start


def test_a_kill_of_j1_comes_before_the_start_of_j2_with_its_selected_signals() -> None:
    """`j` is conditioned on `t(j)`: the KILLJOB of `j.1` starts `j.2` in the same input. The
    kill, with the signals the command selected, is carried out before the start."""
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: y\ncondition: t(j)\n")
    done: list[tuple[str, Any]] = []

    async def scenario() -> None:
        engine = Engine(
            catalog,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            profile=RuntimeProfile(kill_signals=(15, 9)),
        )
        real_kill, real_spawn = Engine._kill, Engine.spawn

        def kill(self: Engine, live: Any, signals: Any = None) -> None:
            done.append(("kill", signals))
            real_kill(self, live, signals)

        def spawn(self: Engine, job: str, run_number: int, **kwargs: Any) -> bool:
            done.append(("spawn", (job, run_number)))
            return real_spawn(self, job, run_number, **kwargs)

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Engine, "_kill", kill)
                patch.setattr(Engine, "spawn", spawn)
                engine.inject(_ev(0, "FORCE_STARTJOB", job="j"))
                await engine.run_until_quiescent(T0)
                engine.inject(_ev(1, "KILLJOB", job="j", kill_signals=[2, 9]))
                await engine.run_until_quiescent(_at(1))
        finally:
            await engine.shutdown()

    asyncio.run(scenario())
    assert done == [("spawn", ("j", 1)), ("kill", (2, 9)), ("spawn", ("j", 2))]


def test_the_plan_puts_a_kill_at_its_emitted_position_before_a_later_start() -> None:
    """The plan of that input: the kill of `j.1` at its emission, then the start of `j.2`."""
    oracle = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: y\ncondition: t(j)\n"))
    oracle.feed(_ev(0, "FORCE_STARTJOB", job="j"))
    traced = oracle.trace_length()
    with oracle.batch(_at(1)) as batch:
        batch.feed(_ev(1, "KILLJOB", job="j"))
    fold = fold_runs(oracle.trace_since(traced)[1], {"j": 1})
    assert fold == RunFold((DecidedStart("j", 2, 1),), frozenset({("j", 1)}))
    origins: dict[Any, int] = {}
    plan = dispatch_plan(
        batch.emitted,
        dispatched={"j": 1},
        live={"j": 1},
        dispatchable=frozenset({"j"}),
        starts=fold.starts,
        origins=origins,
    )
    assert plan == [("KILL", "j", 1), ("SPAWN", "j", 2)]
    assert origins == {("KILL", "j", 1): 0, ("SPAWN", "j", 2): 1}


# ---------------------------------------- 4. a start whose run ends in its own input


def _deadline(past_due: bool) -> Any:
    """`x` ends by its runtime limit at T0 + 1 minute and starts `j`. With `past_due`, `j`'s own
    limit is already past at its start (a stored catalog keeps one, DL-469), so it ends `j.1`
    inside the input that started it."""
    catalog = lower_source(
        "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
        "insert_job: j\njob_type: c\ncommand: y\ncondition: t(x)\nterm_run_time: 1\n"
    )
    if past_due:
        catalog.jobs["j"].sem.term_run_time_min = -1
    return catalog


#: `x` and the box `bx` both end by their runtime limits at T0 + 1 minute; `x`'s end starts the
#: member `j`, and the box's end terminates it (`job_terminator`)
_BOXED = (
    "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
    "insert_job: bx\njob_type: b\nterm_run_time: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: y\nbox_name: bx\ncondition: t(x)\n"
    "job_terminator: y\n"
)


@pytest.mark.parametrize("case", ["past-due", "box", "control"])
def test_a_start_its_own_input_ends_launches_nothing_after_an_injected_starting(
    tmp_path: Path, case: str
) -> None:
    """BUG-112 B: an operator's `STATUS STARTING` on `j` at `x`'s deadline is that input's
    command half. When the input also ends `j.1`, nothing of `j` launches, live or at a
    restart: by `j`'s own limit, already past at its start, or by its box's limit at the same
    instant. With no such end, `j.1` launches once."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    ends = case != "control"
    catalog = lower_source(_BOXED) if case == "box" else _deadline(past_due=ends)

    async def scenario() -> None:
        engine = _start(run, catalog, adapter)
        try:
            await _feed(engine, _ev(0, "STARTJOB", job="x"))
            if case == "box":
                await _feed(engine, _ev(0, "STARTJOB", job="bx"))
            await _feed(engine, _ev(1, "STATUS", job="j", status="STARTING"))
            trace = [(e.job, e.transition, e.run_number) for e in engine.oracle.trace()]
            assert ("j", "INACTIVE->STARTING", 1) in trace
            row = engine.oracle.store.job["j"]
            assert (row.status, row.run_number) == ("STARTING", 1)
        finally:
            await _close(engine)
        restarted = await _resume(run, catalog, adapter, 3)
        try:
            await restarted.run_until_quiescent(_at(4))
        finally:
            await _close(restarted)

    asyncio.run(scenario())
    want = [("x", 1)] if ends else [("x", 1), ("j", 1)]
    # the restart may run a launched run again as its second attempt (spec-wiring 2b)
    first = [(b["job"], b["run_number"]) for _, b in _kinds(run, "dispatch") if b["ordinal"] == 1]
    assert first == want and _kinds(run, "dispatch_pending") == []
    if ends:
        assert adapter.calls == want
    assert _genesis(run).spawns == set(want)


# ------------------------------------ 5. a waiting start whose run ends before its launch


#: `j` ends by its runtime limit a minute after it starts, launched or not
_LIMITED = "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 1\n"

#: `a` exits and starts `b`
_OWED = (
    "insert_job: a\njob_type: c\ncommand: x\n\n"
    "insert_job: b\njob_type: c\ncommand: y\ncondition: s(a)\n"
)


class _Died(Exception):
    """The engine stopped between the input that ended the run and its next step."""


def _hold(monkeypatch: pytest.MonkeyPatch, hold: str, held: set[str]) -> None:
    """Keep a start of a job in `held` back: behind its job's group (`blocked`, DL-426) or
    behind an update's dispatch filter (`EngineUpdate.holds`)."""
    if hold == "group":
        real = Engine.blocked

        def blocked(engine: Engine, job: str, replacing: str | None = None) -> str | None:
            return "held" if job in held else real(engine, job, replacing)

        monkeypatch.setattr(Engine, "blocked", blocked)
    elif hold == "update":
        monkeypatch.setattr(EngineUpdate, "holds", lambda _update, job: job in held)


def _cut(monkeypatch: pytest.MonkeyPatch, cut: str, run: tuple[str, int]) -> None:
    """With `death` the engine stops right after the input that ends `run` commits; with
    `snapshot` it takes a snapshot there first."""
    if cut == "none":
        return
    real = Engine._dispatch

    def dispatch(engine: Engine, applied: Any, selected: Any = None) -> None:
        if run in applied.ended:
            if cut == "snapshot":
                engine.take_snapshot()
            raise _Died
        real(engine, applied, selected)

    monkeypatch.setattr(Engine, "_dispatch", dispatch)


async def _until_died(engine: Engine, until: datetime, cut: str) -> None:
    if cut == "none":
        await engine.run_until_quiescent(until)
        return
    with pytest.raises(_Died):
        await engine.run_until_quiescent(until)


async def _owed_start(run: Path) -> None:
    """`a` exits five seconds after T0 and the engine dies before its completion lands, so the
    restart feeds it and owes the start of `b` it decides (DL-425)."""
    engine = _start(run, lower_source(_OWED), FakeAdapter({("a", 1): (5.0, 0)}, default=None))
    assert engine.journal is not None
    real = engine.journal.append

    def die_at_the_feed(*records: Any, **kwargs: Any) -> int:
        if any(r.kind == "follow_up" and r.payload["event"] == "done" for r in records):
            raise RuntimeError("the engine died before the completion landed")
        return real(*records, **kwargs)

    engine.journal.append = die_at_the_feed  # type: ignore[method-assign]
    try:
        engine.inject(_ev(0, "STARTJOB", job="a"))
        with pytest.raises(RuntimeError, match="before the completion landed"):
            await engine.run_until_quiescent(_at(6))
    finally:
        engine.journal.append = real  # type: ignore[method-assign]
        await _close(engine)


@pytest.mark.parametrize("cut", ["none", "death", "snapshot"])
@pytest.mark.parametrize("order", ["before", "after"])
@pytest.mark.parametrize("hold", ["group", "update", "recovery", "drain"])
def test_a_waiting_start_whose_run_ended_never_launches(
    tmp_path: Path, hold: str, order: str, cut: str
) -> None:
    """BUG-112 A. A start waits: behind its job's group or an update, deferred while a restart
    recovers, or pending while the engine drains. Its run ends: by its runtime limit or the
    engine's own kill, or by an operator's kill while a restart recovers. An operator's `STATUS
    STARTING` then comes before the waiting start's release, or after it. With `death` the
    engine stops right after the input that ended the run; with `snapshot` a snapshot is taken
    there first. In every cell the run is never launched, its pending record is
    discharged once, and a restart afterwards owes nothing."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    job = "b" if hold == "recovery" else "j"
    catalog = lower_source(_OWED if hold == "recovery" else _LIMITED)
    held: set[str] = set()
    patches = pytest.MonkeyPatch()  # the hold and the cut, undone at the restart
    _hold(patches, hold, held)
    spy = pytest.MonkeyPatch()
    appends = _appends(spy)
    starting = _ev(0, "STATUS", job=job, status="STARTING")

    async def inject(engine: Engine, minutes: float) -> None:
        await _feed(engine, starting.model_copy(update={"at": _at(minutes)}))

    async def scenario() -> None:
        # the start is decided and waits; its run ends; the engine stops there with a cut
        if hold == "recovery":
            await _owed_start(run)
            _cut(patches, cut, (job, 1))
            engine = await _resume(run, catalog, adapter, 10)
        else:
            _cut(patches, cut, (job, 1))
            engine = _start(run, catalog, adapter)
        try:
            if hold == "recovery":
                # the recovered completion starts `b.1`, and the kill ends it before its release
                engine.inject(_ev(10, "KILLJOB", job=job))
                if order == "before" and cut == "none":
                    engine.inject(starting.model_copy(update={"at": _at(10)}))
                await _until_died(engine, _at(10), cut)
            else:
                held.add(job)
                if hold == "drain":
                    engine.request_stop()
                engine.inject(_ev(0, "STARTJOB", job=job), source=None)  # the engine's own
                if hold == "drain":  # a drained engine applies no later instant
                    engine.inject(_ev(0, "KILLJOB", job=job), source=None)
                await _until_died(engine, _at(2), cut)
            row = engine.oracle.store.job[job]
            injected = hold == "recovery" and order == "before" and cut == "none"
            assert (row.status, row.run_number) == ("STARTING" if injected else "TERMINATED", 1)
            # the release, with the operator's STARTING before or after it
            if cut == "none" and hold in ("group", "update"):
                if order == "before":
                    await inject(engine, 3)
                held.clear()
                engine._release(job)
                if order == "after":
                    await inject(engine, 3)
            elif cut == "none" and hold == "recovery" and order == "after":
                await inject(engine, 11)
        finally:
            await _close(engine)
        if cut != "none" or hold == "drain":  # released by a restart
            patches.undo()
            held.clear()
            restarted = await _resume(run, catalog, adapter, 20)
            try:
                if order == "before":
                    restarted.inject(starting.model_copy(update={"at": _at(20)}))
                await restarted.run_until_quiescent(_at(20))
                if order == "after":
                    await inject(restarted, 21)
            finally:
                await _close(restarted)
        # a restart afterwards owes nothing
        owed = len(_kinds(run, "dispatch_pending"))
        final = await _resume(run, catalog, adapter, 30)
        try:
            await final.run_until_quiescent(_at(31))
            row = final.oracle.store.job[job]
            assert (row.status, row.run_number) == ("STARTING", 1)
            assert final.live_jobs() == frozenset()
        finally:
            await _close(final)
        assert len(_kinds(run, "dispatch_pending")) == owed

    try:
        asyncio.run(scenario())
    finally:
        patches.undo()
        spy.undo()
    assert (job, 1) not in adapter.calls and (job, 1) not in _dispatched(run)
    records = _kinds(run, "dispatch_pending")
    [(seq, pending)] = [(seq, b) for seq, b in records if b["event"] == "pending"]
    assert (pending["job"], pending["run_number"]) == (job, 1)
    assert [b for _, b in records if b["event"] == "discharged"] == [
        {"event": "discharged", "pending": seq, "dispatch": None}
    ]
    # one store transaction holds the input that ended the run and the discharge
    assert _discharged_with(appends, "decision") == [seq]
    assert (job, 1) not in _genesis(run).unended


# ------------------------------------- 6. an update that removes or retypes a waiting job


def _job(name: str, body: str) -> str:
    return f"insert_job: {name}\n{body}\n"


#: `m` as each candidate leaves it: gone, a box, or a command with a new command line
_CANDIDATE_M = {
    "removed": "",
    "retyped": _job("m", "job_type: b\n"),
    "edited": _job("m", 'job_type: c\ncommand: sleep 32\ncondition: v(GO) = "go"\n'),
}


@pytest.mark.parametrize("edit", list(_CANDIDATE_M))
def test_an_update_that_removes_or_retypes_a_waiting_job_retires_its_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    """While an update waits for `c`'s stop, `m`'s condition starts `m.1`, which waits behind
    the update's dispatch filter. An application that removes `m` or retypes it ends its row:
    the start is discharged in the transaction of the `config applied` record. A same-type edit
    ends nothing, and `m.1` launches once under the applied definition. A restart afterwards
    replays the decision of `m.1`, which has no dispatch: the configuration that removed or
    retyped `m` ended it, so nothing is owed and nothing launches."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    appends = _appends(monkeypatch)
    old = _job("c", "job_type: c\ncommand: sleep 30\n") + _job(
        "m", 'job_type: c\ncommand: sleep 30\ncondition: v(GO) = "go"\n'
    )
    new = _job("c", "job_type: c\ncommand: sleep 31\n") + _CANDIDATE_M[edit]
    start = Event(at=T0, kind="STARTJOB", payload={"job": "c"})
    go = Event(at=T0, kind="SET_GLOBAL", payload={"name": "GO", "value": "go"})
    seen = asyncio.run(
        _admit(tmp_path, monkeypatch, launches, old, new, before=(start,), during=(go,), watch=())
    )
    assert "m" in seen["filter"]
    if edit == "edited":
        assert _starts(tmp_path, "m") == [("pending", 1, False), ("dispatch", 1, True)]
        assert [launch[:2] for launch in launches if launch[0] == "m"] == [("m", 1)]
        return
    assert _starts(tmp_path, "m") == [("pending", 1, False), ("discharged", None, True)]
    [(seq, _pending)] = [
        (seq, b)
        for seq, b in _kinds(tmp_path / "run", "dispatch_pending")
        if b["event"] == "pending"
    ]
    # one store transaction holds the `config applied` record and the discharge
    assert _discharged_with(appends, "config") == [seq]
    del launches[:]

    async def restart() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.open_all()
        engine = await resume_run(
            prepare_estate(tmp_path / "estate").catalog,
            tmp_path / "run",
            clock=VirtualClock(start=_at(2)),
            adapters=_adapters(),
            settings=SETTINGS,
        )
        try:
            await engine.run_until_quiescent(_at(2))
            assert engine.started.is_set()
        finally:
            _teardown(engine, ladders)
            await _close(engine)

    asyncio.run(restart())
    assert not [launch for launch in launches if launch[0] == "m"]
    assert _starts(tmp_path, "m") == [("pending", 1, False), ("discharged", None, True)]
    assert not [launch for launch in launches if launch[0] == "m"]


# ------------------------------------------------------- 8. a root from before this change


@pytest.mark.parametrize(
    ("retained", "injected"),
    [(True, True), (False, True), (False, False)],
    ids=["tail", "snapshot", "snapshot-ended"],
)
def test_a_root_from_before_this_change_discharges_what_its_tail_shows_ended(
    tmp_path: Path, retained: bool, injected: bool
) -> None:
    """A build before DL-551 left the pending start of `j.1` open after its run ended, and an
    operator's `STATUS STARTING` made the row live again. Where the replayed tail shows the run
    ended after the pending record, the restart discharges it and nothing launches. Where a
    snapshot taken after the end hides it, the pending start is carried out as recorded, as
    DL-550 says of a recorded dispatch: it launches on the live row, and with no injection the
    release finds the run ended and discharges it (DL-232)."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    catalog = lower_source(_LIMITED)
    held = {"j"}
    patches = pytest.MonkeyPatch()
    _hold(patches, "group", held)
    patches.setattr(Engine, "retire", lambda _engine, _ended: [])  # the earlier build

    async def scenario() -> None:
        engine = _start(run, catalog, adapter)
        try:
            await _feed(engine, _ev(0, "STARTJOB", job="j"))
            await engine.run_until_quiescent(_at(2))  # the runtime limit ends `j.1`
            if injected:
                await _feed(engine, _ev(3, "STATUS", job="j", status="STARTING"))
            if not retained:
                engine.take_snapshot()
        finally:
            await _close(engine)
        patches.undo()
        restarted = await _resume(run, catalog, adapter, 5)
        try:
            await restarted.run_until_quiescent(_at(5))
        finally:
            await _close(restarted)

    try:
        asyncio.run(scenario())
    finally:
        patches.undo()
    [(seq, _pending)] = [
        (s, b) for s, b in _kinds(run, "dispatch_pending") if b["event"] == "pending"
    ]
    closes = [b for _, b in _kinds(run, "dispatch_pending") if b["event"] == "discharged"]
    if retained or not injected:
        assert closes == [{"event": "discharged", "pending": seq, "dispatch": None}]
        assert adapter.calls == [] and _dispatched(run) == []
    else:
        assert closes == []
        assert adapter.calls == [("j", 1)]
        [(_, dispatch)] = _kinds(run, "dispatch")
        assert dispatch["discharges"] == seq


def test_an_input_drops_the_runs_it_ends_from_the_starts_a_restart_owes() -> None:
    """The restart's owed step reads the starts its replay derived whose run has not ended
    since (`Engine.unended`): an input applied before that step, a recovered completion's
    cascade, drops the runs it ends and leaves the rest in the order derived (DL-551)."""
    engine = Engine(
        lower_source(_LIMITED + "\ninsert_job: k\njob_type: c\ncommand: y\n"),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        try:
            engine.inject(_ev(0, "STARTJOB", job="j"))
            await engine.run_until_quiescent(T0)
            engine.unended = {("k", 1): None, ("j", 1): None, ("j", 2): None}
            engine.inject(_ev(0.5, "KILLJOB", job="j"))
            await engine.run_until_quiescent(_at(0.5))
        finally:
            await engine.shutdown()

    asyncio.run(scenario())
    assert list(engine.unended) == [("k", 1), ("j", 2)]


#: what the update makes of the watch `j`: a command or a box
_RETYPED_J = {"CMD": "job_type: c\ncommand: sleep 32\n", "BOX": "job_type: b\n"}


@pytest.mark.parametrize("status", ["STARTING", "RUNNING"])
@pytest.mark.parametrize("retype", list(_RETYPED_J))
def test_an_earlier_build_s_pending_of_a_job_an_update_retyped_is_discharged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retype: str, status: str
) -> None:
    """A build before DL-551 held the start of the watch `j.1` behind an update, applied the
    update, which retypes `j`, and died before the release: the pending record stays open. The
    replayed tail shows the configuration that ended `j`'s row, so the restart discharges it
    before an operator's `STATUS STARTING` or `RUNNING` is admitted: nothing launches, and the
    start-up completes."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    old = _job("c", "job_type: c\ncommand: sleep 30\n") + _job("j", _fw(tmp_path, 'v(GO) = "go"'))
    new = _job("c", "job_type: c\ncommand: sleep 31\n") + _job("j", _RETYPED_J[retype])
    go = Event(at=T0, kind="SET_GLOBAL", payload={"name": "GO", "value": "go"})

    def finish(_update: EngineUpdate) -> None:
        raise _Died  # the engine dies once the application is recorded, before the release

    async def earlier_build() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        engine = _start_estate(tmp_path, old, ())
        with monkeypatch.context() as patch:
            patch.setattr(Engine, "retire", lambda _engine, _ended: [])
            patch.setattr(EngineUpdate, "finish", finish)
            try:
                engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "c"}))
                await engine.run_until_quiescent(T0)
                attempt, wrapper = _observed(engine, "c", PGID)
                assert wrapper is not None
                _estate(tmp_path, new, ())
                await _request(engine, _plan(engine, tmp_path))
                await _started(ladders, PGID)
                engine.inject(go)
                await engine.run_until_quiescent(T0)
                assert [item.job for item in engine._held_starts] == ["j"]
                engine.attempts.status_recorded(attempt, SIGNALED)
                wrapper.cancel()
                await engine.run_until_quiescent(engine.clock.now())
                with pytest.raises(_Died):
                    async with asyncio.timeout(30):  # a guard against a hang only
                        await ladders.end(engine, attempt, PGID)
                        await engine.run_until_quiescent(engine.clock.now())
                assert engine.config_rev == 2
            finally:
                _teardown(engine, ladders)
        await _crash(engine)

    asyncio.run(earlier_build())
    [(seq, pending)] = [
        (seq, b)
        for seq, b in _kinds(tmp_path / "run", "dispatch_pending")
        if b["event"] == "pending"
    ]
    assert (pending["job"], pending["run_number"]) == ("j", 1)
    assert [b for _, b in _kinds(tmp_path / "run", "dispatch_pending")] == [pending]
    del launches[:]

    async def restart() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.open_all()
        at = _at(2)
        engine = await resume_run(
            prepare_estate(tmp_path / "estate").catalog,
            tmp_path / "run",
            clock=VirtualClock(start=at),
            adapters=_adapters(),
            settings=SETTINGS,
        )
        try:
            engine.inject(Event(at=at, kind="STATUS", payload={"job": "j", "status": status}))
            await engine.run_until_quiescent(at)
            assert engine.started.is_set()
            assert engine.oracle.store.job["j"].status == status
        finally:
            _teardown(engine, ladders)
            await _close(engine)

    asyncio.run(restart())
    assert not [launch for launch in launches if launch[0] == "j"]
    closes = [b for _, b in _kinds(tmp_path / "run", "dispatch_pending") if b["event"] != "pending"]
    assert closes == [{"event": "discharged", "pending": seq, "dispatch": None}]
    assert not [b for _, b in _kinds(tmp_path / "run", "dispatch") if b["job"] == "j"]


def _unclosed(entries: Any, runs: Any) -> RunFold:
    """The build before DL-551: every numbered `->STARTING` of the batch is a start, whether
    or not the batch then ended its run."""
    seen: dict[str, int] = {}
    starts: list[DecidedStart] = []
    for entry in entries:
        if "->" not in entry.transition:
            continue
        position = seen.get(entry.job, 0)
        seen[entry.job] = position + 1
        if entry.run_number is not None and entry.transition.endswith("->STARTING"):
            starts.append(DecidedStart(entry.job, entry.run_number, position))
    return RunFold(tuple(starts), fold_runs(entries, runs).ended)


def test_an_earlier_build_s_pending_written_after_its_run_ended_is_discharged(
    tmp_path: Path,
) -> None:
    """The build before DL-551 planned a start its own input ended: `x`'s end starts the member
    `j.1`, and the box's end terminates it at the same instant. The start waited behind its
    job's group, so its pending record follows the input that ended the run, and an operator
    then set `j` STARTING. The replayed tail shows the end, so the restart discharges the
    record, before or after it, and nothing launches."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    catalog = lower_source(_BOXED)
    held = {"j"}
    patches = pytest.MonkeyPatch()
    _hold(patches, "group", held)
    patches.setattr(Engine, "retire", lambda _engine, _ended: [])
    patches.setattr(runner_admission, "fold_runs", _unclosed)

    async def scenario() -> None:
        engine = _start(run, catalog, adapter)
        try:
            await _feed(engine, _ev(0, "STARTJOB", job="x"), _ev(0, "STARTJOB", job="bx"))
            await engine.run_until_quiescent(_at(1))
            # one input started and ended `j.1`, and its pending record followed that input
            row = engine.oracle.store.job["j"]
            assert (row.status, row.run_number) == ("TERMINATED", 1)
            assert [item.job for item in engine._held_starts] == ["j"]
            await _feed(engine, _ev(2, "STATUS", job="j", status="STARTING"))
        finally:
            await _close(engine)
        patches.undo()
        restarted = await _resume(run, catalog, adapter, 5)
        try:
            await restarted.run_until_quiescent(_at(5))
        finally:
            await _close(restarted)

    try:
        asyncio.run(scenario())
    finally:
        patches.undo()
    [(seq, _pending)] = [
        (s, b) for s, b in _kinds(run, "dispatch_pending") if b["event"] == "pending"
    ]
    closes = [b for _, b in _kinds(run, "dispatch_pending") if b["event"] == "discharged"]
    assert closes == [{"event": "discharged", "pending": seq, "dispatch": None}]
    assert ("j", 1) not in adapter.calls and ("j", 1) not in _dispatched(run)
