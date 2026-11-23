"""Pruning in bounded steps (DL-543): the snapshot cleanup commits on its own, the bound is a
read, and a backlog drains one step of at most `PRUNE_STEP` rows each time the engine's loop
has nothing due, with a turn for every other task before and after each step."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate

from dsl41 import main_config as main
from dsl41 import runner
from dsl41.engine_charts import DrainRequest
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import Interval, Pruned, SqliteStore, Store
from dsl41.runner_update import UpdateRequest

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_STEP = 3
#: the backlog `_backlog(engine, 7)` leaves: seqs 1 to 15, below the snapshot at 16
_BACKLOG = 15


#: every engine a test here started; `_closed_after` closes their stores at teardown
_STARTED: list[Engine] = []


@pytest.fixture(autouse=True)
def _closed_after() -> Iterator[None]:
    """A scenario that fails before its own close leaves its store open; closed here, at the
    test's teardown, its connection is not left for a later test's garbage collection, where
    `error::ResourceWarning` would fail that test instead."""
    yield
    while _STARTED:
        journal = _STARTED.pop().journal
        if journal is not None:
            journal.close()  # idempotent: the store's close and the lock's release both are


def _tracked(engine: Engine) -> Engine:
    _STARTED.append(engine)
    return engine


def _setter(n: int, at: datetime) -> Event:
    return Event(at=at, kind="SET_GLOBAL", payload={"name": f"G{n}", "value": "v"})


def _start(run_root: Path, adapter: FakeAdapter | None = None) -> Engine:
    return _tracked(
        start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter or FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
        )
    )


async def _resume(run_root: Path, at: datetime) -> Engine:
    engine = _tracked(
        await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=at),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
        )
    )
    await engine.run_until_quiescent(engine.clock.now())
    return engine


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _store(engine: Engine) -> Store:
    assert engine.journal is not None
    return engine.journal.store


def _seqs(store: Store) -> list[int]:
    return [seq for seq, _ in store.read_from(0)]


def _unbroken(store: Store) -> bool:
    seqs = _seqs(store)
    return seqs == list(range(store.first_seq(), store.last_seq() + 1))


def _steps(store: Store, monkeypatch: pytest.MonkeyPatch, log: list[tuple[Any, ...]]) -> None:
    """Log each prune step as `("step", deleted, last seq)`, in order with the other entries."""
    real: Callable[[int, int], Pruned] = store.prune_step

    def counted(bound: int, limit: int) -> Pruned:
        pruned = real(bound, limit)
        log.append(("step", pruned.deleted, store.last_seq()))
        return pruned

    monkeypatch.setattr(store, "prune_step", counted)


def _taken(log: list[tuple[Any, ...]]) -> list[int]:
    return [entry[1] for entry in log if entry[0] == "step"]


async def _alongside(
    engine: Engine,
    log: list[tuple[Any, ...]],
    actions: dict[int, Callable[[], None]] | None = None,
) -> None:
    """One continuous loop run to the clock's instant, beside a task that logs each turn it
    gets as `("turn",)` and runs `actions[k]` at its first turn after the k-th logged step. The
    task is bounded: when it gives up it cancels the loop's run, so a drain that never ends
    fails the test instead of hanging it."""
    actions = dict(actions or {})
    run = asyncio.ensure_future(engine.run_until_quiescent(engine.clock.now()))

    async def watcher() -> None:
        for _ in range(10_000):
            if run.done():
                return
            log.append(("turn",))
            action = actions.pop(len(_taken(log)), None)
            if action is not None:
                action()
            await asyncio.sleep(0)
        run.cancel()
        raise AssertionError("the loop never quiesced")

    try:
        await watcher()
    finally:
        await asyncio.gather(run, return_exceptions=True)
    await run  # the loop's own result or failure
    assert actions == {}, f"actions never run: {sorted(actions)}"


def _turn_between_steps(log: list[tuple[Any, ...]]) -> bool:
    """Whether the watcher had a turn between every two logged steps."""
    kinds = [entry[0] for entry in log]
    return all(not (a == b == "step") for a, b in zip(kinds, kinds[1:], strict=False))


async def _backlog(engine: Engine, inputs: int) -> None:
    """`inputs` retryable inputs at T0, then two days on the clock: every row is past the
    one-day retention when the next snapshot is taken."""
    for n in range(inputs):
        engine.inject(_setter(n, T0), request_id=f"r-{n}")
    await engine.run_until_quiescent(T0)
    clock = engine.clock
    assert isinstance(clock, VirtualClock)
    clock._now = T0 + timedelta(days=2)


async def _feed(engine: Engine, n: int, request_id: str | None = None) -> int:
    """One input at the clock's instant, applied in one loop run; answers the last seq."""
    now = engine.clock.now()
    engine.inject(_setter(n, now), request_id=request_id or f"late-{n}")
    await engine.run_until_quiescent(now)
    return _store(engine).last_seq()


def test_due_inputs_go_first_and_every_step_has_a_turn_for_other_tasks_around_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One continuous run over a backlog: four inputs due at once are admitted with no step
    between them, a concurrent task gets a turn between every two steps, an input it feeds
    after a step is admitted before the next, and the drain ends at the snapshot's bound."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        bound = engine.take_snapshot()
        assert bound - 1 == _BACKLOG and _taken(log) == [_STEP]
        now = engine.clock.now()
        for n in range(4):
            engine.inject(_setter(n, now), request_id=f"late-{n}")
        fed: list[int] = []

        def feed() -> None:
            fed.append(store.last_seq())
            engine.inject(_setter(9, now), request_id="mid")

        await _alongside(engine, log, {2: feed})
        assert _taken(log) == [_STEP] * (_BACKLOG // _STEP)  # whole steps: none after the bound
        assert _turn_between_steps(log)
        steps = [entry for entry in log if entry[0] == "step"]
        decided = {record.request_id: seq for seq, record in store.read_from(0)}
        assert all(decided[f"late-{n}"] <= steps[1][2] for n in range(4))  # all four went first
        mid = [decided["mid"] - 1]  # its input, the record before its decision
        assert steps[1][2] == fed[0] < mid[0] <= steps[2][2]  # between the second and third
        assert store.first_seq() == bound and _unbroken(store)
        await _alongside(engine, log)
        assert len(_taken(log)) == _BACKLOG // _STEP  # nothing owed: no step
        await _close(engine)

    asyncio.run(scenario())


def test_an_idle_engine_drains_its_backlog_a_step_and_a_turn_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        bound = engine.take_snapshot()
        last = store.last_seq()
        await _alongside(engine, log)
        assert _taken(log) == [_STEP] * (_BACKLOG // _STEP) and _turn_between_steps(log)
        assert store.first_seq() == bound and store.last_seq() == last  # nothing was admitted
        await _close(engine)

    asyncio.run(scenario())


def test_an_update_applied_while_pruning_is_owed_leaves_the_drain_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update admits and applies as loop steps that are no admission's batch; the loop
    takes the owed steps once it has nothing due, in the same continuous run."""
    from test_update_settlement import _estate, _jil, _plan

    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    estate = _estate(tmp_path, _jil({"j": "x"}))

    async def scenario() -> None:
        engine = _tracked(
            start_run(
                prepare_estate(estate).catalog,
                tmp_path / "run",
                source_capture=main.encoded_parts(prepare_estate(estate)),
                launched=main.profile_of(prepare_estate(estate)),
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter(default=None)},
                settings=EngineSettings(retention_days=1),
            )
        )
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        bound = engine.take_snapshot()
        assert store.first_seq() == _STEP + 1 < bound
        _estate(tmp_path, _jil({"j": "y"}))
        answer = engine.request_update(
            UpdateRequest(_plan(engine, tmp_path), 1, engine.baseline_id, "u1")
        )
        await _alongside(engine, log)
        assert isinstance(answer.result().decision, Admitted) and engine.config_rev == 2
        assert store.first_seq() == bound and _turn_between_steps(log)
        await _close(engine)

    asyncio.run(scenario())


def test_a_snapshot_due_in_a_drain_takes_its_step_and_the_drain_goes_on_to_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch at which a snapshot falls due takes that snapshot and its one step; the loop
    then drains to the new snapshot's bound."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        first = engine.take_snapshot()
        clock = engine.clock
        assert isinstance(clock, VirtualClock)
        clock._now = clock.now() + timedelta(seconds=EngineSettings().snapshot_interval_s)
        engine.inject(_setter(0, clock.now()), request_id="late-0")
        await _alongside(engine, log)  # bounded: a stalled drain fails, it does not hang
        latest = store.latest_snapshot()
        assert latest is not None and latest.seq > first
        # the new bound is past the first: the first snapshot's own record is the first one
        # inside the window
        assert store.first_seq() == first + 1
        assert _taken(log) == [_STEP] * (_BACKLOG // _STEP) + [1]
        await _close(engine)

    asyncio.run(scenario())


def test_a_retry_in_the_middle_of_a_drain_is_answered_while_its_decision_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry of a request whose decision a step kept answers that decision and appends
    nothing; once a step deleted it, the same id is admitted as a new request."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        engine.take_snapshot()
        found = store.find_request("r-0")  # the first step took seqs 1 to 3, not r-0's decision
        held = store.find_request("r-2")
        assert found is not None and held is not None and store.first_seq() == _STEP + 1
        original, kept = found.payload["index"], held.payload["index"]
        engine.inject(_setter(0, T0), request_id="r-0")
        seen: list[Any] = []

        def after_the_second_step() -> None:
            # it took seqs 4 to 6, r-0's decision among them: its id is free; r-2's is kept
            seen.extend([store.find_request("r-0"), engine.decisions.state("r-0")])
            seen.append(engine.decisions.state("r-2"))
            engine.inject(_setter(0, T0), request_id="r-0")
            engine.inject(_setter(2, T0), request_id="r-2")

        await _alongside(engine, log, {2: after_the_second_step})
        assert seen == [None, None, "applied"]
        assert [(rid, result.index) for rid, result in engine.deduped] == [
            ("r-0", original),
            ("r-2", kept),
        ]
        again = store.find_request("r-0")
        assert again is not None and again.payload["index"] > original
        await _close(engine)

    asyncio.run(scenario())


def test_eviction_reads_the_step_not_a_retained_head_without_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forty `engine snapshot` records head the journal: no step reads the retained journal
    to find the oldest decision. A step that deleted no decision evicts nothing; the step that
    deleted r-0's evicts it and keeps r-1's."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        for _ in range(40):  # at T0 nothing is past retention: the steps delete nothing
            engine.take_snapshot()
        await _backlog(engine, 2)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)

        def unread(seq: int) -> Any:
            raise AssertionError("a step read the retained journal")

        monkeypatch.setattr(store, "read_from", unread)
        engine.take_snapshot()
        assert engine.decisions.state("r-0") == "applied"
        await _alongside(engine, log)
        monkeypatch.undo()
        assert engine.decisions.state("r-0") is None
        assert engine.decisions.state("r-1") == "applied"  # its decision is at the bound
        assert store.find_request("r-1") is not None
        await _close(engine)

    asyncio.run(scenario())


def test_a_step_after_the_root_was_lost_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact retry is answered before any leadership check; the owed step after it proves
    the lock first, so an engine that lost the root raises and deletes nothing (S6b)."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        engine.take_snapshot()
        before = (store.first_seq(), store.transitions_coverage(), store.history_coverage())
        engine.inject(_setter(5, T0), request_id="r-5")
        os.unlink(run_root / "leader.lock")
        with pytest.raises(EngineError, match="leader.lock was deleted"):
            await engine.run_until_quiescent(engine.clock.now())
        assert [rid for rid, _ in engine.deduped] == ["r-5"]
        assert (store.first_seq(), store.transitions_coverage(), store.history_coverage()) == (
            before
        )
        await _close(engine)

    asyncio.run(scenario())


def test_a_stop_drain_begun_in_the_turn_before_a_step_takes_no_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop reads what it owes again after its first turn: a stop that a callback begins
    in that turn leaves the owed step untaken; only the stop's own snapshot steps (DL-543)."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        bound = engine.take_snapshot()
        owed = engine._owed_step
        begun: list[str] = []

        def stop() -> None:
            engine.lifecycle.stop_requested(DrainRequest(time.monotonic(), 0.05, 0.1))
            begun.append(engine.lifecycle.state)

        def watched() -> int | None:
            answer = owed()
            if answer is not None and not begun:
                asyncio.get_running_loop().call_soon(stop)  # runs in the loop's first turn
            return answer

        monkeypatch.setattr(engine, "_owed_step", watched)
        await engine.run_until_quiescent(engine.clock.now())
        assert begun == ["STOPPED"] and engine.stopped.is_set()
        latest = store.latest_snapshot()
        assert latest is not None and latest.seq > bound
        # the one step after the first is the stop snapshot's, after that snapshot's record;
        # an owed step taken in the turn would come before the stop's records
        assert [entry[1:] for entry in log if entry[0] == "step"] == [
            (_STEP, bound + 1),
            (_STEP, latest.seq + 1),
        ]
        await _close(engine)

    asyncio.run(scenario())


def test_a_snapshot_round_with_nothing_prunable_commits_nothing_for_the_step(
    tmp_path: Path,
) -> None:
    """The snapshot, its record and the cleanup commit; a step with no row below its bound
    opens no transaction (DL-543)."""
    from test_transitions import _commits

    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        await _feed(engine, 0)
        store = _store(engine)
        assert isinstance(store, SqliteStore)
        ended = _commits(store)
        engine.take_snapshot()
        store._db.set_trace_callback(None)
        assert len(ended) == 3 and store.first_seq() == 1  # snapshot, its record, the cleanup
        last = store.last_seq()
        assert store.transitions_coverage() == [Interval(1, last, True)]
        assert store.history_coverage() == [Interval(1, last, True)]
        await _close(engine)

    asyncio.run(scenario())


def test_a_restart_in_the_middle_of_a_drain_resumes_and_its_next_snapshot_prunes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine that ends with a backlog left resumes from its root, whose kept rows are one
    unbroken range. The backlog is not stored: the restarted engine takes no step until its
    next snapshot, which reads a bound of its own and drains on from there."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def first() -> int:
        engine = _start(run_root)
        await _backlog(engine, 7)
        bound = engine.take_snapshot()
        await _close(engine)  # a teardown with no lifecycle stop and no loop run
        return bound

    bound = asyncio.run(first())
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        assert store.first_seq() == _STEP + 1 < bound and _unbroken(store)

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=1))
        store = _store(engine)
        assert store.first_seq() == _STEP + 1 and _unbroken(store)  # nothing owed across it
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        await _feed(engine, 1)
        assert _taken(log) == []
        engine.take_snapshot()
        assert _taken(log) == [_STEP] and store.first_seq() == 2 * _STEP + 1
        await _alongside(engine, log)
        # the restart's bound: the first snapshot's own record is the first inside the window
        assert store.first_seq() == bound + 1 and _unbroken(store)
        await _close(engine)

    asyncio.run(second())


def test_a_stop_with_a_backlog_takes_no_step_in_its_drain_and_one_at_its_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop with a dispatched attempt still running and an input queued behind it: the
    input is admitted inside the stop drain and refused, and the drain takes no owed step. The
    stop's snapshot takes one and the engine stops with the rest of the backlog left; the next
    run's first snapshot continues it."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start(run_root, FakeAdapter({("j", 1): (120.0, 0)}, default=None))
        await _backlog(engine, 7)
        store = _store(engine)
        log: list[tuple[Any, ...]] = []
        _steps(store, monkeypatch, log)
        engine.take_snapshot()
        now = engine.clock.now()
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
        reply = engine.request_stop()
        engine.inject(_setter(8, now), request_id="during")
        await engine.run_until_quiescent(now + timedelta(minutes=10))
        assert await reply == "STOPPED" and engine.stopped.is_set()
        kinds = [record.kind for _, record in store.read_from(0)]
        assert "dispatch" in kinds and "outcome" not in kinds  # the attempt was still running
        during = store.find_request("during")
        assert during is not None and during.payload["code"] == "engine_draining"
        assert _taken(log) == [_STEP, _STEP]
        assert store.first_seq() == 2 * _STEP + 1 and _unbroken(store)
        await _close(engine)

    asyncio.run(first())

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=11))
        store = _store(engine)
        assert store.first_seq() == 2 * _STEP + 1 and _unbroken(store)
        engine.take_snapshot()
        assert store.first_seq() == 3 * _STEP + 1 and _unbroken(store)
        await _close(engine)

    asyncio.run(second())


def test_a_step_that_raises_leaves_the_snapshot_written_and_the_cleanup_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        store = _store(engine)
        assert isinstance(store, SqliteStore)
        for n in range(3):
            await _feed(engine, n)
            engine.take_snapshot()
        await _feed(engine, 3)

        def broken(bound: int, limit: int) -> Pruned:
            raise RuntimeError("the step failed")

        monkeypatch.setattr(store, "prune_step", broken)
        with pytest.raises(RuntimeError, match="the step failed"):
            engine.take_snapshot()
        held = [seq for (seq,) in store._db.execute("SELECT seq FROM snapshots ORDER BY seq")]
        latest = store.latest_snapshot()
        assert latest is not None and len(held) == 2 and held[-1] == latest.seq
        assert latest.seq == store.last_seq() - 1  # the snapshot and its `engine` record
        await _close(engine)

    asyncio.run(scenario())


def _modules() -> list[Callable[[], Any]]:
    from test_history_index import _transitions_store_module
    from test_transitions import _previous_store_module

    return [_previous_store_module, _transitions_store_module]


@pytest.mark.parametrize("module", _modules(), ids=["pre-transitions", "pre-index"])
def test_an_earlier_build_opens_and_prunes_a_root_this_build_left_in_a_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Callable[[], Any]
) -> None:
    """The store layout is unchanged: an earlier build's store code opens a root this build
    left with a backlog, prunes it to its own bound in one delete, and this build resumes it."""
    monkeypatch.setattr(runner, "PRUNE_STEP", _STEP)
    run_root = tmp_path / "run"

    async def first() -> int:
        engine = _start(run_root)
        await _backlog(engine, 7)
        bound = engine.take_snapshot()
        await _close(engine)
        return bound

    bound = asyncio.run(first())
    old = module()
    with closing(old.SqliteStore(run_root / STORE_NAME)) as previous:
        assert previous.prune_before(bound, T0 + timedelta(days=1)) == bound - _STEP - 1
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        assert store.first_seq() == bound and _unbroken(store)

    async def second() -> None:
        engine = await _resume(run_root, T0 + timedelta(days=2, minutes=1))
        assert _store(engine).first_seq() == bound and _unbroken(_store(engine))
        await _feed(engine, 0)
        engine.take_snapshot()
        await _close(engine)

    asyncio.run(second())
