"""The hand-off at the end of a stop sequence (DL-524): an attempt whose stop sequence has
ended is released only after a reading of what is left of its command.

The sequence's end and the consumption of its completion are two turns of the loop. A status
record can land between them. The scenarios put it there deterministically: the stand-in
sequence queues the record with `call_soon` before it returns, so the record runs before the
task's done callback. The survivor check runs on a controlled clock: `runner_stops` reads a
stand-in `time.monotonic`, and its poll's sleep advances that clock by the poll interval.
Every reading of what is left (`EngineStops.left`, the held poll's included) agrees with the
stand-in group and is logged with the clock's time. No test sleeps on the real clock.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from test_engine_stops_cookie import PGID, RUN_ID, _Procs
from test_runner_attempts import World, _running, _tiers, _turn, signaled
from test_update_atomic import _stopping
from test_update_settlement import SIGNALED, _close, _engine, _jil, _kinds

from dsl41 import runner_stops
from dsl41.engine_charts import UPDATE_STOP
from dsl41.runner import Engine
from dsl41.runner_procid import NOTHING_LEFT, Left

#: the controlled clock's grace after a stop sequence, and its survivor poll interval
GRACE = 5.0
POLL = 0.5


class _Sequence:
    """The stand-ins around one stop sequence on a controlled clock.

    The sequence parks until `end` and returns the clock's time as its end. `in_gap` runs in
    the turn after the sequence returned and before its completion is consumed. Every reading
    of what is left finds the group until `gone_at`. The survivor poll's sleep waits for
    `resume` while it is clear, then advances the clock by its delay."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, gone_at: float) -> None:
        self.now = 0.0
        self.gone_at = gone_at
        self.readings: list[float] = []
        self.gate = asyncio.Event()
        self.resume = asyncio.Event()
        self.resume.set()
        self.in_gap: Callable[[], None] | None = None
        self.polls = 0
        sequence = self

        async def ladder(_self: Any, _pgid: int, _token: str, _grace: float) -> float:
            await sequence.gate.wait()
            if sequence.in_gap is not None:
                asyncio.get_running_loop().call_soon(sequence.in_gap)
            return sequence.now

        def left(_self: Any, pgid: int, _token: str, *_args: Any, **_kwargs: Any) -> Left:
            sequence.readings.append(sequence.now)
            if pgid == PGID and sequence.now < sequence.gone_at:
                return Left("ours", {PGID: "t"}, "processes")
            return NOTHING_LEFT

        real_sleep = asyncio.sleep

        async def sleep(delay: float) -> None:
            sequence.polls += 1
            await sequence.resume.wait()
            sequence.now += delay
            await real_sleep(0)

        clock = SimpleNamespace(monotonic=lambda: sequence.now)
        loop = SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop)
        monkeypatch.setattr(runner_stops.EngineStops, "ladder", ladder)
        monkeypatch.setattr(runner_stops.EngineStops, "left", left)
        monkeypatch.setattr(runner_stops, "time", clock)
        monkeypatch.setattr(runner_stops, "asyncio", loop)
        monkeypatch.setattr(runner_stops, "SETTLE_POLL_S", POLL)

    def record_at(self, engine: Engine, at: float) -> Callable[[], None]:
        """The wrapper's record, landing at clock time `at`."""

        def record() -> None:
            self.now = at
            engine.attempts.status_recorded(RUN_ID, SIGNALED)

        return record


def _replacements(tmp_path: Any, job: str = "j") -> list[int]:
    """The ordinals above 1 dispatched for `job`: the replacements of its first attempt."""
    return [
        d["ordinal"] for d in _kinds(tmp_path, "dispatch") if d["job"] == job and d["ordinal"] > 1
    ]


async def _teardown(engine: Engine, sequence: _Sequence) -> None:
    sequence.gate.set()
    sequence.resume.set()
    engine.stops.cancel_all()
    await engine.shutdown()
    _close(engine)


# ---------------------------------------------------------------- the gap


@pytest.mark.parametrize(
    ("gone_at", "held"),
    [(4.0, False), (4.5, False), (7.0, True), (float("inf"), True)],
    ids=[
        "gone-at-the-hand-off",
        "gone-within-the-grace",
        "there-at-the-deadline-gone-at-7",
        "alive-throughout",
    ],
)
def test_a_record_between_a_stop_sequence_and_its_hand_off_waits_for_a_survivor_reading(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, gone_at: float, held: bool
) -> None:
    """DL-524, DL-522: an attempt whose stop sequence has ended is released only after a
    reading of what is left of its command. The sequence ends at 0, the record lands and the
    completion is consumed at 4, and the grace is 5, measured from the sequence's end. A group
    gone by the hand-off or within the grace is released: the update applies and one
    replacement is dispatched. A group still there at 5 has survived, even if it goes at 7:
    the attempt is held, the update stays STOPPING and only ordinal 1 was dispatched."""
    _Procs(monkeypatch)
    sequence = _Sequence(monkeypatch, gone_at)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.attempts.grace_kill_s = GRACE
        try:
            await _stopping(engine, tmp_path)
            sequence.in_gap = sequence.record_at(engine, 4.0)
            sequence.gate.set()
            await engine.attempts.join()
            seen.append(RUN_ID in engine.held_groups)
            await engine.run_until_quiescent(engine.clock.now())
            seen.append(engine.update.stopping)
        finally:
            await _teardown(engine, sequence)

    asyncio.run(scenario())
    assert seen == [held, held]
    assert sequence.readings and sequence.readings[0] == 4.0
    assert max(sequence.readings) == (GRACE if held else max(4.0, gone_at))
    assert _replacements(tmp_path) == ([] if held else [2])


# ---------------------------------------------------------------- the neighbours


@pytest.mark.parametrize(
    ("when", "held", "last"),
    [("during-the-sequence", True, 6.0), ("after-the-hand-off", False, 7.0)],
)
def test_a_record_beside_the_hand_off_keeps_its_survivor_grace(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, when: str, held: bool, last: float
) -> None:
    """DL-431, DL-524: a record that lands while the stop sequence runs leaves the verdict to the
    sequence's end, and the grace runs from that end. The sequence ends at 1, so a group still
    there at 6 has survived. A record that lands after the hand-off was consumed takes the
    verdict at the record, and the grace runs from the record. It lands at 4, so a group gone
    at 7 is released and one replacement is dispatched."""
    _Procs(monkeypatch)
    sequence = _Sequence(monkeypatch, gone_at=7.0)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.attempts.grace_kill_s = GRACE
        try:
            await _stopping(engine, tmp_path)
            if when == "during-the-sequence":
                engine.attempts.status_recorded(RUN_ID, SIGNALED)
                sequence.now = 1.0
                sequence.gate.set()
            else:
                sequence.gate.set()
                await engine.attempts.join()
                assert not sequence.readings and not engine.attempts.ladder_running(RUN_ID)
                sequence.record_at(engine, 4.0)()
            await engine.attempts.join()
            seen.append(RUN_ID in engine.held_groups)
            await engine.run_until_quiescent(engine.clock.now())
            seen.append(engine.update.stopping)
        finally:
            await _teardown(engine, sequence)

    asyncio.run(scenario())
    assert seen == [held, held]
    assert sequence.readings[0] == (1.0 if held else 4.0) and max(sequence.readings) == last
    assert _replacements(tmp_path) == ([] if held else [2])


def test_a_stop_sequence_answers_its_end_on_the_clock_the_survivor_check_reads(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-524: the survivor grace of a hand-off runs from the end of the stop sequence, so the
    sequence answers that end on the clock `outlives` reads, whenever its last signal went."""
    clock = SimpleNamespace(now=0.0)

    async def sequence(*_args: Any, **_kwargs: Any) -> None:
        clock.now = 2.5

    monkeypatch.setattr(runner_stops, "stop_ladder", sequence)
    monkeypatch.setattr(runner_stops, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def scenario() -> float:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            return await engine.stops.ladder(PGID, "t", 0.05)
        finally:
            await engine.shutdown()
            _close(engine)

    assert asyncio.run(scenario()) == 2.5


# ---------------------------------------------------------------- teardown and failures


@dataclass
class _Paused(World):
    """The tiers' fake world. Each stop sequence ends at `end`; one of a group in `parked`
    first waits for `release`. A sequence queues its group's `in_gap` in the turn before its
    completion is consumed. A survivor check waits for `check` unless its group is in
    `prompt`, then raises its group's `check_errors` entry or answers whether its group
    lives."""

    end: float = 3.0
    in_gap: dict[int, Callable[[], None]] = field(default_factory=dict)
    parked: set[int] = field(default_factory=set)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    check: asyncio.Event = field(default_factory=asyncio.Event)
    prompt: set[int] = field(default_factory=set)
    check_errors: dict[int, BaseException] = field(default_factory=dict)

    async def ladder(self, pgid: int, start_token: str, grace_term_s: float) -> float:  # type: ignore[override]
        if pgid in self.parked:
            await self.release.wait()
        await super().ladder(pgid, start_token, grace_term_s)
        if pgid in self.in_gap:
            asyncio.get_running_loop().call_soon(self.in_gap[pgid])
        return self.end

    async def outlives(
        self, pgid: int, start_token: str, grace_kill_s: float, since: float | None = None
    ) -> bool:
        self.log.append(("outlives", pgid, since))
        if pgid not in self.prompt:
            await self.check.wait()
        if pgid in self.check_errors:
            raise self.check_errors[pgid]
        return pgid in self.alive


def _alive_world(**kwargs: Any) -> _Paused:
    """Groups 7 and 8 ignore TERM and outlive their KILL."""
    groups = {7, 8}
    return _Paused(alive=set(groups), ignores_term=set(groups), unkillable=set(groups), **kwargs)


def _completions(world: World) -> list[str]:
    return [e[0] for e in world.log]


@pytest.mark.parametrize("verdict", ["survived", "gone", "failed", "cancelled"])
def test_a_survivor_check_that_took_over_a_stop_sequence_is_waited_for_and_hands_off_once(
    verdict: str,
) -> None:
    """DL-524, DL-433: the survivor check that takes over a stop sequence owns the attempt
    until its own completion. One `join` returns only after its verdict. A survivor is
    reported before the one completion, a failure is reported and holds the attempt before
    it, a group gone is released by it, and a cancelled check ends nothing: no completion and
    no clearance. The check runs once, with the grace from the sequence's end."""

    async def scenario() -> None:
        world = _alive_world()
        attempts, _ = _tiers(world)
        attempts.on_ladder_done = lambda attempt: world.log.append(("ladder_done", attempt))
        job = _running(attempts, "j", 7)
        world.in_gap[7] = lambda: attempts.status_recorded(job, signaled(signal.SIGKILL))
        attempts.stop(job, UPDATE_STOP)
        joined = asyncio.ensure_future(attempts.join())
        await _turn()
        assert not joined.done() and attempts.ladder_running(job)
        assert attempts.state(job) == "ENDED" and ("outlives", 7, 3.0) in world.log
        assert "ladder_done" not in _completions(world)
        if verdict == "gone":
            world.alive.discard(7)
        if verdict == "failed":
            world.check_errors[7] = OSError("reading failed")
        if verdict == "cancelled":
            task = attempts.ladder_task(job)
            assert task is not None
            task.cancel()
        world.check.set()
        if verdict == "failed":
            with pytest.raises(OSError, match="reading failed"):
                await joined
        else:
            await joined
        await _turn()
        names = _completions(world)
        assert names.count("outlives") == 1
        if verdict == "cancelled":
            assert "ladder_done" not in names and attempts.ladder_running(job)
            return
        assert names.count("ladder_done") == 1 and not attempts.ladder_running(job)
        before = {"survived": "command_survived", "failed": "ladder_failed"}.get(verdict)
        if before is not None:
            assert names.index(before) < names.index("ladder_done") and attempts.held(job)
        else:
            assert not attempts.held(job) and "command_survived" not in names

    asyncio.run(scenario())


def test_a_hand_off_reading_that_fails_holds_the_attempt_as_a_failed_stop_sequence() -> None:
    """DL-524, DL-426: the reading the hand-off takes is part of the stop. One that raises
    leaves the group unproven, as a failed sequence does: the attempt is held and the failure
    reported before the one completion, nothing is still owed, and `join` raises it."""

    async def scenario() -> None:
        world = _alive_world()

        def reading(_pgid: int, _token: str) -> bool:
            raise OSError("EMFILE")

        attempts, _ = _tiers(world, command_alive=reading)
        attempts.on_ladder_done = lambda attempt: world.log.append(("ladder_done", attempt))
        job = _running(attempts, "j", 7)
        world.in_gap[7] = lambda: attempts.status_recorded(job, signaled(signal.SIGKILL))
        attempts.stop(job, UPDATE_STOP)
        with pytest.raises(OSError, match="EMFILE"):
            await attempts.join()
        names = _completions(world)
        assert names.count("ladder_done") == 1 and "outlives" not in names
        assert names.index("ladder_failed") < names.index("ladder_done")
        assert attempts.held(job) and not attempts.ladder_running(job)

    asyncio.run(scenario())


def test_a_join_reads_the_failure_of_a_check_whose_attempt_was_forgotten_meanwhile() -> None:
    """DL-524: `join` reads every task that took a ladder slot while it waited. Attempt `a`'s
    sequence is parked. Attempt `b`'s ends, its record lands in the gap, its survivor check
    fails, its group then clears and `b` is forgotten, all while `join` waits for `a`. When
    `a`'s sequence ends, `join` raises `b`'s failure."""

    async def scenario() -> None:
        failure = OSError("b's reading failed")
        world = _alive_world(parked={7}, prompt={8}, check_errors={8: failure})
        attempts, _ = _tiers(world)
        a = _running(attempts, "a", 7)
        b = _running(attempts, "b", 8)
        world.in_gap[8] = lambda: attempts.status_recorded(b, signaled(signal.SIGKILL))
        attempts.stop(a, UPDATE_STOP)
        attempts.stop(b, UPDATE_STOP)
        joined = asyncio.ensure_future(attempts.join())
        await _turn()
        assert attempts.held(b) and ("ladder_failed", b, str(failure)) in world.log
        world.alive.discard(8)
        attempts.group_gone(b)
        attempts.plan_done(b)
        attempts.forget(b)
        assert not attempts.knows(b) and not joined.done()
        world.release.set()
        with pytest.raises(OSError, match="b's reading failed"):
            await joined

    asyncio.run(scenario())


def test_overlapping_joins_each_read_a_forgotten_failure_and_the_last_drops_it() -> None:
    """DL-524: a forgotten attempt's records stay while any `join` waits. Two joins wait for
    attempt `a`'s parked sequence while `b`'s survivor check fails and `b` is forgotten. The
    first join to end leaves `b`'s records in place, so the second still raises `b`'s
    failure; the last join to end drops them."""

    async def scenario() -> None:
        failure = OSError("b's reading failed")
        world = _alive_world(parked={7}, prompt={8}, check_errors={8: failure})
        attempts, _ = _tiers(world)
        a = _running(attempts, "a", 7)
        b = _running(attempts, "b", 8)
        world.in_gap[8] = lambda: attempts.status_recorded(b, signaled(signal.SIGKILL))
        attempts.stop(a, UPDATE_STOP)
        attempts.stop(b, UPDATE_STOP)
        first = asyncio.ensure_future(attempts.join())
        second = asyncio.ensure_future(attempts.join())
        await _turn()
        world.alive.discard(8)
        attempts.group_gone(b)
        attempts.plan_done(b)
        attempts.forget(b)
        assert any(o.attempt == b for o in attempts._owned.values())
        world.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert [str(r) for r in results] == [str(failure)] * 2
        assert not any(o.attempt == b for o in attempts._owned.values())

    asyncio.run(scenario())


def test_cancelling_a_join_cancels_the_stop_sequences_it_waits_for_and_clears_nothing() -> None:
    """DL-524, DL-426: a cancelled `join` cancels the tasks it waits for. A cancelled sequence
    ended nothing: no completion is taken, and the attempt stays unsettled."""

    async def scenario() -> None:
        world = _alive_world(parked={7})
        attempts, _ = _tiers(world)
        attempts.on_ladder_done = lambda attempt: world.log.append(("ladder_done", attempt))
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        joined = asyncio.ensure_future(attempts.join())
        await _turn()
        joined.cancel()
        with pytest.raises(asyncio.CancelledError):
            await joined
        await _turn()
        task = attempts.ladder_task(job)
        assert task is not None and task.cancelled()
        assert "ladder_done" not in _completions(world)
        assert attempts.ladder_running(job) and job in attempts.unsettled()

    asyncio.run(scenario())


def test_a_completion_taken_after_a_join_was_cancelled_hands_nothing_off() -> None:
    """DL-524, DL-426: once a `join` is cancelled the engine is leaving. Here the sequence is
    runnable when the join is cancelled and ends before the join's cancellation is handled,
    with its record in the gap. Its completion then starts no survivor check, calls no
    listener and releases nothing, as a cancelled sequence's would."""

    async def scenario() -> None:
        world = _alive_world(parked={7})
        attempts, _ = _tiers(world)
        attempts.on_ladder_done = lambda attempt: world.log.append(("ladder_done", attempt))
        job = _running(attempts, "j", 7)
        world.in_gap[7] = lambda: attempts.status_recorded(job, signaled(signal.SIGKILL))
        attempts.stop(job, UPDATE_STOP)
        joined = asyncio.ensure_future(attempts.join())
        await _turn()
        world.release.set()  # the sequence is runnable
        joined.cancel()  # before it returns
        with pytest.raises(asyncio.CancelledError):
            await joined
        await _turn()
        names = _completions(world)
        assert attempts.state(job) == "ENDED" and ("KILL", 7) in world.log
        assert "outlives" not in names and "ladder_done" not in names
        assert attempts.ladder_running(job) and job in attempts.unsettled()
        task = attempts.ladder_task(job)
        assert task is not None and task.done()

    asyncio.run(scenario())


def test_the_engine_shutdown_waits_for_the_survivor_check_of_a_hand_off_and_leaves_no_timer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-524: teardown follows the hand-off. A shutdown that is already waiting for a stop
    sequence when the sequence ends returns only after the survivor check that took over has
    given its verdict. A verdict that holds the attempt arms the held poll; after the
    shutdown no timer and no poll is left armed."""
    _Procs(monkeypatch)
    sequence = _Sequence(monkeypatch, gone_at=float("inf"))
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.attempts.grace_kill_s = GRACE
        joining = asyncio.Event()
        join = engine.attempts.join

        async def joined() -> None:
            joining.set()
            await join()

        monkeypatch.setattr(engine.attempts, "join", joined)
        down = False
        try:
            await _stopping(engine, tmp_path)
            shutdown = asyncio.ensure_future(engine.shutdown())
            await joining.wait()
            await _turn()
            sequence.in_gap = sequence.record_at(engine, 4.0)
            sequence.resume.clear()
            sequence.gate.set()
            for _ in range(20):
                if sequence.polls:
                    break
                await asyncio.sleep(0)
            assert sequence.polls, "no survivor check runs after the hand-off"
            seen.append((shutdown.done(), engine.attempts.ladder_running(RUN_ID)))
            sequence.resume.set()
            await shutdown
            down = True
            seen.append((RUN_ID in engine.held_groups, max(sequence.readings)))
            seen.append((dict(engine.stops._timers), engine.stops._poll))
            _close(engine)
        finally:
            if not down:
                await _teardown(engine, sequence)

    asyncio.run(scenario())
    assert seen == [(False, True), (True, GRACE), ({}, None)]
