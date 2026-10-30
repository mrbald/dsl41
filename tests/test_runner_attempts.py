"""The attempt and engine lifecycle tiers (src/dsl41/runner_attempts.py), against fakes.

The pure parts get a test per row: the `status.json` translation, the missing-record verdict,
the oracle mapping and the interruption rule's plans. The tiers run on one executor with a fake
`Processes` and a fake `EngineEffects` that write one ordered log. A fake ladder parks on a gate
until the test opens it, so a test sees that a stop returns before any ladder runs and that many
ladders are in flight at once. Assertions are on the log's order, never on elapsed time.

The last test runs every tier scenario into a private hits directory and checks that the tiers
alone reach every coverage item of both charts but one, which it names with its reason.
"""

from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from dsl41.engine_charts import (
    RECORD_OUTCOME,
    ATTEMPT,
    ENGINE_LIFECYCLE,
    ENGINE_STOP,
    UPDATE_STOP,
    WRAPPER_LOST,
    AttemptOutcome,
    DrainRequest,
    outcome_of,
)
from dsl41.runner_adapters import Failed, Terminated
from dsl41.runner_attempts import (
    DISPATCH,
    MAX_ATTEMPTS_PER_RUN,
    FEED_STATUS,
    SET_ON_HOLD,
    AttemptTier,
    EngineTier,
    FollowUp,
    FollowUpStep,
    Interrupted,
    attempt_id,
    attempt_spool_dir,
    command_group,
    exit_record_from_status,
    follow_up_plan,
    missing_record_verdict,
    oracle_input_for,
)
from dsl41.engine_charts import Intent
from dsl41.state_machine import HITS_ENV, Event, Outcome, coverage_items

#: the graces of the scenarios and the time of a stop request
GRACE_TERM, GRACE_KILL = 10.0, 5.0
REQUEST = DrainRequest(now=100.0, grace_term_s=GRACE_TERM, grace_kill_s=GRACE_KILL)
SECOND = DrainRequest(now=103.0, grace_term_s=GRACE_TERM, grace_kill_s=GRACE_KILL)
STARTED = "2026-10-30T01:00:00+00:00"
ENDED_AT = "2026-10-30T01:00:05+00:00"


def spawn(pgid: int) -> dict[str, Any]:
    """A `spawn.json` naming the command group `pgid`."""
    return {
        "version": 1,
        "command_pid": pgid,
        "command_pgid": pgid,
        "command_start_time": f"ticks:{pgid}",
        "started_at": STARTED,
    }


def exited(code: int) -> dict[str, Any]:
    return {"version": 1, "outcome": "exited", "exit_code": code, "ended_at": ENDED_AT}


def signaled(sig: int) -> dict[str, Any]:
    return {"version": 1, "outcome": "signaled", "signal": sig, "ended_at": ENDED_AT}


OBSERVED = {"outcome": "signaled", "signal": 15}
PARENT_LOST = {"outcome": "terminated", "cause": "parent lost", "observed": OBSERVED}
SPAWN_FAILED_STATUS = {"outcome": "spawn_failed", "error": "No such file: /in"}
RECORD_FAILED = {
    "outcome": "terminated",
    "cause": "spawn record write failed ([Errno 28] No space left on device); killed",
    "observed": OBSERVED,
}


# ---- the translation table: one test per row


@pytest.mark.parametrize(
    ("status", "stopping", "terminated_by", "launched", "kind", "cause"),
    [
        pytest.param(exited(3), False, None, True, "natural", None, id="exited"),
        pytest.param(exited(0), True, "stop", True, "stopped", UPDATE_STOP, id="exit_of_stop"),
        pytest.param(signaled(9), False, None, True, "natural", None, id="signal_from_outside"),
        pytest.param(signaled(15), True, "stop", True, "stopped", UPDATE_STOP, id="signal_of_stop"),
        pytest.param(PARENT_LOST, False, "lifeline", True, "stopped", "lifeline", id="lifeline"),
        pytest.param(PARENT_LOST, True, "lifeline", True, "stopped", "lifeline", id="lifeline_2"),
        pytest.param(SPAWN_FAILED_STATUS, False, "wrapper", False, "spawn_failed", None, id="sf"),
        pytest.param(RECORD_FAILED, False, "wrapper", True, "spawn_failed", None, id="record"),
    ],
)
def test_a_status_record_translates_to_its_exit_record_and_outcome(
    status: dict[str, Any],
    stopping: bool,
    terminated_by: str | None,
    launched: bool,
    kind: str,
    cause: str | None,
) -> None:
    record = exit_record_from_status(status, stop_in_progress=stopping, spawn=spawn(7))
    assert (record.terminated_by, record.launched) == (terminated_by, launched)
    assert record.wrapper_outcome == status["outcome"]
    assert record.started_at == STARTED and record.ended_at == status.get("ended_at")
    outcome = outcome_of(record, UPDATE_STOP if stopping else None)
    assert (outcome.kind, outcome.cause, outcome.record) == (kind, cause, record)


def test_a_record_keeps_the_wrappers_evidence() -> None:
    lost = exit_record_from_status(RECORD_FAILED, stop_in_progress=False)
    assert lost.observed == OBSERVED and lost.cause == RECORD_FAILED["cause"]
    assert lost.started_at is None  # no spawn.json: no start time
    failed = exit_record_from_status(SPAWN_FAILED_STATUS, stop_in_progress=False)
    assert failed.error == "No such file: /in" and failed.observed is None
    code = exit_record_from_status(exited(7), stop_in_progress=False)
    assert (code.exit_code, code.signal, code.cause, code.error) == (7, None, None, None)
    sig = exit_record_from_status(signaled(9), stop_in_progress=True)
    assert (sig.exit_code, sig.signal) == (None, 9)


def test_a_record_the_wrapper_never_writes_stays_natural_and_maps_as_the_adapter_does() -> None:
    """A malformed record is natural, so nothing reruns it, and its oracle input is the adapter's
    truthful FAILURE or TERMINATED."""
    odd: list[tuple[dict[str, Any], type]] = [
        ({"outcome": "exited", "exit_code": "7"}, Failed),
        ({"outcome": "vanished"}, Failed),
        ({"outcome": ["exited"]}, Failed),
        ({"outcome": "terminated", "cause": "someone else"}, Terminated),
        ({"outcome": "signaled", "ended_at": 5}, Terminated),
    ]
    for status, result in odd:
        record = exit_record_from_status(status, stop_in_progress=False)
        assert record.terminated_by is None and record.ended_at is None
        outcome = outcome_of(record, None)
        assert outcome.kind == "natural"
        assert isinstance(oracle_input_for(outcome), result), status


# ---- the missing record


def _never(pgid: int, token: str) -> bool:
    raise AssertionError("no command to verify")


def test_a_live_wrapper_means_wait() -> None:
    assert missing_record_verdict(spawn(7), wrapper_alive=True, command_alive=_never) == "wait"


def test_a_gone_wrapper_with_no_spawn_record_names_no_command() -> None:
    assert missing_record_verdict(None, wrapper_alive=False, command_alive=_never) == (
        "wrapper_gone"
    )


def test_a_gone_wrapper_with_a_dead_command_is_gone_and_with_a_live_one_is_lost() -> None:
    asked: list[tuple[int, str]] = []

    def alive(answer: bool) -> Callable[[int, str], bool]:
        def check(pgid: int, token: str) -> bool:
            asked.append((pgid, token))
            return answer

        return check

    assert missing_record_verdict(spawn(7), wrapper_alive=False, command_alive=alive(False)) == (
        "wrapper_gone"
    )
    assert missing_record_verdict(spawn(7), wrapper_alive=False, command_alive=alive(True)) == (
        "wrapper_lost"
    )
    assert asked == [(7, "ticks:7"), (7, "ticks:7")]


def test_a_spawn_record_that_names_no_signalable_group_counts_as_none() -> None:
    for bad in (
        {**spawn(7), "command_pgid": 8},
        {**spawn(7), "command_pgid": "7"},
        {**spawn(7), "command_start_time": None},
    ):
        assert command_group(bad) is None
        assert missing_record_verdict(bad, wrapper_alive=False, command_alive=_never) == (
            "wrapper_gone"
        )
    assert command_group(spawn(7)) == (7, "ticks:7")


# ---- what the oracle is told


def test_each_outcome_feeds_the_oracle_what_the_adapter_maps() -> None:
    natural = AttemptOutcome(
        "natural", record=exit_record_from_status(exited(3), stop_in_progress=False)
    )
    assert oracle_input_for(natural) == 3
    killed = exit_record_from_status(signaled(9), stop_in_progress=False)
    assert oracle_input_for(AttemptOutcome("natural", record=killed)) == Terminated(
        "killed by signal 9"
    )
    for shape in (
        AttemptOutcome("spawn_failed", error="EMFILE"),
        outcome_of(exit_record_from_status(RECORD_FAILED, stop_in_progress=False), None),
    ):
        assert oracle_input_for(shape) == Failed("wrapper_spawn_failed")
    assert oracle_input_for(AttemptOutcome("stopped", cause=ENGINE_STOP)) is None
    assert oracle_input_for(AttemptOutcome("unobservable")) is None
    with pytest.raises(AssertionError):
        oracle_input_for(AttemptOutcome("natural"))


# ---- the interruption rule's plans

STOPPED = AttemptOutcome(
    "stopped",
    cause=UPDATE_STOP,
    record=exit_record_from_status(signaled(15), stop_in_progress=True, spawn=spawn(7)),
)


def _plan(outcome: AttemptOutcome = STOPPED, **changes: Any) -> FollowUp:
    facts: dict[str, Any] = {
        "job": "emea.ledger_post",
        "run_number": 3,
        "attempt": 1,
        "in_catalog": True,
        "live_run": (3, "RUNNING"),
        "marked": False,
    }
    return follow_up_plan(outcome, **{**facts, **changes})


def test_an_interrupted_idempotent_job_reruns_the_same_run_under_the_next_attempt_id() -> None:
    plan = _plan()
    assert plan == FollowUp("rerun", (FollowUpStep(DISPATCH, "emea.ledger_post.3.2"),))
    assert _plan(AttemptOutcome("unobservable"), attempt=2).steps[0].detail == (
        "emea.ledger_post.3.3"
    )


def test_an_interrupted_marked_job_is_failure_interrupted_then_held_with_its_evidence() -> None:
    plan = _plan(marked=True)
    evidence = Interrupted(
        "emea.ledger_post.3.1", "stopped", UPDATE_STOP, STARTED, ENDED_AT, 15, None
    )
    assert plan == FollowUp(
        "hold",
        (FollowUpStep(FEED_STATUS, Failed("interrupted")), FollowUpStep(SET_ON_HOLD, evidence)),
    )
    unseen = _plan(AttemptOutcome("unobservable", cause=ENGINE_STOP), marked=True).steps[1]
    assert unseen.detail == Interrupted(
        "emea.ledger_post.3.1", "unobservable", ENGINE_STOP, None, None, None, None
    )


@pytest.mark.parametrize(
    ("observed", "signal_", "exit_code"),
    [
        ({"outcome": "signaled", "signal": 15}, 15, None),
        ({"outcome": "exited", "exit_code": 143}, None, 143),
    ],
    ids=["observed_signal", "observed_exit"],
)
def test_held_evidence_takes_the_exit_the_wrapper_observed_after_its_own_kill(
    observed: dict[str, Any], signal_: int | None, exit_code: int | None
) -> None:
    status = {**PARENT_LOST, "observed": observed}
    lifeline = outcome_of(exit_record_from_status(status, stop_in_progress=False), None)
    evidence = _plan(lifeline, marked=True).steps[1].detail
    assert (evidence.kind, evidence.cause) == ("stopped", "lifeline")
    assert (evidence.signal, evidence.exit_code) == (signal_, exit_code)


def test_the_fourth_attempt_reruns_and_the_fifth_is_held_naming_the_limit() -> None:
    assert MAX_ATTEMPTS_PER_RUN == 5
    assert _plan(attempt=4) == FollowUp("rerun", (FollowUpStep(DISPATCH, "emea.ledger_post.3.5"),))
    held = _plan(attempt=5)
    assert held.kind == "hold" and held.steps[0] == FollowUpStep(FEED_STATUS, Failed("interrupted"))
    assert held.steps[1].detail == Interrupted(
        "emea.ledger_post.3.5", "stopped", UPDATE_STOP, STARTED, ENDED_AT, 15, None, 5
    )
    assert _plan(attempt=6).kind == "hold"


def test_a_removed_job_gets_no_follow_up() -> None:
    assert _plan(in_catalog=False) == FollowUp("none")
    assert _plan(in_catalog=False, marked=True) == FollowUp("none")


def test_a_run_the_oracle_ended_or_a_later_run_gets_no_follow_up() -> None:
    assert _plan(live_run=(3, "TERMINATED")) == FollowUp("none")
    assert _plan(live_run=None, marked=True) == FollowUp("none")
    assert _plan(live_run=(4, "RUNNING")) == FollowUp("none")


def test_a_starting_run_counts_as_live() -> None:
    assert _plan(live_run=(3, "STARTING")).kind == "rerun"


def test_a_natural_exit_and_both_spawn_failures_complete() -> None:
    natural = AttemptOutcome(
        "natural", record=exit_record_from_status(exited(0), stop_in_progress=False)
    )
    assert _plan(natural) == FollowUp("complete", (FollowUpStep(FEED_STATUS, 0),))
    assert _plan(natural, in_catalog=False, live_run=None).kind == "complete"
    for shape in (
        outcome_of(exit_record_from_status(SPAWN_FAILED_STATUS, stop_in_progress=False), None),
        outcome_of(exit_record_from_status(RECORD_FAILED, stop_in_progress=False), None),
    ):
        assert _plan(shape) == FollowUp(
            "complete", (FollowUpStep(FEED_STATUS, Failed("wrapper_spawn_failed")),)
        )


def test_a_step_is_marked_done_with_its_transaction() -> None:
    step = _plan().steps[0]
    assert step.done_seq is None and step.done(41) == FollowUpStep(DISPATCH, step.detail, 41)


def test_the_attempt_id_and_its_spool_directory() -> None:
    assert attempt_id("emea.ledger_post", 3, 2) == "emea.ledger_post.3.2"
    assert attempt_spool_dir(Path("/r"), "emea.ledger_post", 3, 2) == Path(
        "/r/runs/emea.ledger_post.3/2"
    )


# ---- the fakes


@dataclass
class World:
    """The fake `Processes` and `EngineEffects`: one ordered log, a gate every ladder parks on,
    the groups whose command ignores TERM, and the armed timers."""

    log: list[tuple[Any, ...]] = field(default_factory=list)
    ignores_term: set[int] = field(default_factory=set)
    gated: bool = False
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    timers: dict[str, tuple[int, float]] = field(default_factory=dict)
    #: "TERM" or "KILL": the ladder raises before sending that signal
    fail_at: str = ""
    #: the groups whose command lives; TERM ends one unless it ignores TERM, KILL ends any
    #: one not in `unkillable`
    alive: set[int] = field(default_factory=set)
    unkillable: set[int] = field(default_factory=set)
    #: a ladder that swallows its cancellation and returns
    swallow_cancel: bool = False
    #: what each attempt's spool holds now: (spawn.json, status.json)
    spool: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = field(
        default_factory=dict
    )

    async def ladder(self, pgid: int, start_token: str, grace_term_s: float) -> None:
        if self.fail_at == "TERM":
            raise OSError("killpg failed")
        self.log.append(("TERM", pgid))
        if pgid not in self.ignores_term:
            self.alive.discard(pgid)
        if self.gated:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.swallow_cancel:
                    raise
                return
        if pgid in self.ignores_term:
            if self.fail_at == "KILL":
                raise OSError("killpg failed")
            self.log.append(("KILL", pgid))
            if pgid not in self.unkillable:
                self.alive.discard(pgid)

    def ladder_failed(self, attempt: str, error: BaseException) -> None:
        self.log.append(("ladder_failed", attempt, str(error)))

    def command_survived(self, attempt: str) -> None:
        self.log.append(("command_survived", attempt))

    def kill_wrapper(self, attempt: str) -> None:
        self.log.append(("kill_wrapper", attempt))

    def arm_timer(self, key: str, generation: int, delay_s: float) -> None:
        self.timers[key] = (generation, delay_s)
        self.log.append(("arm_timer", key, generation, delay_s))

    def read_spool(self, attempt: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        self.log.append(("read_spool", attempt))
        return self.spool.get(attempt, (None, None))

    def cancel_timer(self, key: str) -> None:
        self.timers.pop(key, None)
        self.log.append(("cancel_timer", key))

    def record_outcome(self, attempt: str, outcome: AttemptOutcome) -> None:
        self.log.append(("outcome", attempt, outcome.kind, outcome.cause))

    def journal_draining(self) -> None:
        self.log.append(("journal_draining",))

    def cancel_watchers(self) -> None:
        self.log.append(("cancel_watchers",))

    def arm_abort_deadline(self, deadline: float) -> None:
        self.log.append(("arm_abort_deadline", deadline))

    def record_clean_shutdown(self) -> None:
        self.log.append(("clean_shutdown",))

    def record_unclean_shutdown(self, unsettled: tuple[str, ...]) -> None:
        self.log.append(("unclean_shutdown", unsettled))

    def journal_shutdown_during_startup(self) -> None:
        self.log.append(("shutdown_during_startup",))

    def exit_process(self, code: int) -> None:
        self.log.append(("exit", code))

    def since(self, mark: int) -> list[tuple[Any, ...]]:
        return self.log[mark:]


def _tiers(
    world: World, command_alive: Callable[[int, str], bool] | None = None
) -> tuple[AttemptTier, EngineTier]:
    """The two tiers on one executor. A command is alive while the world says so."""
    attempts = AttemptTier(
        world,
        grace_term_s=GRACE_TERM,
        grace_kill_s=GRACE_KILL,
        command_alive=command_alive or (lambda pgid, token: pgid in world.alive),
    )
    return attempts, EngineTier(world, attempts)


def _running(attempts: AttemptTier, job: str, pgid: int) -> str:
    attempt = attempts.dispatch(job, 1, 1)
    attempts.spawn_recorded(attempt, spawn(pgid))
    return attempt


async def _turn() -> None:
    """Let every ready task run up to its next park."""
    for _ in range(5):
        await asyncio.sleep(0)


def _fire(world: World, attempts: AttemptTier, key: str) -> None:
    """The armed timer under `key` fires."""
    generation, _ = world.timers.pop(key)
    attempts.record_timeout(key, generation)


# ---- the attempt tier


def test_a_drain_with_no_attempt_shuts_down_clean_at_once() -> None:
    world = World()
    attempts, engine = _tiers(world)
    engine.replay_done()
    engine.stop_requested(REQUEST)
    assert world.log == [
        ("journal_draining",),
        ("cancel_watchers",),
        ("arm_abort_deadline", 120.0),
        ("clean_shutdown",),
        ("exit", 0),
    ]
    assert engine.state == "STOPPED" and engine.deadline == 120.0
    engine.abort_deadline()  # the deadline's timer was never cancelled: STOPPED ignores it
    assert engine.state == "STOPPED" and len(world.log) == 5


def test_a_drain_with_one_attempt_stops_it_and_shuts_down_clean_when_it_ends() -> None:
    async def scenario() -> None:
        world = World()
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        engine.stop_requested(REQUEST)
        assert engine.state == "DRAINING" and attempts.state(job) == "STOPPING"
        assert world.log == [
            ("journal_draining",),
            ("cancel_watchers",),
            ("arm_abort_deadline", 120.0),
        ]
        await _turn()
        assert world.since(3) == [("TERM", 7), ("arm_timer", job, 1, GRACE_KILL)]
        attempts.status_recorded(job, signaled(signal.SIGTERM))
        assert world.since(5) == [("outcome", job, "stopped", ENGINE_STOP), ("cancel_timer", job)]
        # ended, its plan not done: the drain does not settle
        assert engine.state == "DRAINING" and attempts.unsettled() == (job,)
        attempts.plan_done(job)
        assert world.since(7) == [("clean_shutdown",), ("exit", 0)]
        assert engine.state == "STOPPED" and attempts.unsettled() == ()
        await attempts.join()

    asyncio.run(scenario())


def test_a_drain_with_twenty_attempts_runs_their_ladders_together_and_awaits_none() -> None:
    async def scenario() -> None:
        world = World(gated=True)
        attempts, engine = _tiers(world)
        engine.replay_done()
        jobs = [_running(attempts, f"j{n}", 100 + n) for n in range(20)]
        engine.stop_requested(REQUEST)
        # the stop returned with every ladder still to run
        assert not any(entry[0] == "TERM" for entry in world.log)
        await _turn()
        assert world.since(3) == [("TERM", 100 + n) for n in range(20)]  # all in flight at once
        assert engine.state == "DRAINING" and attempts.unsettled() == tuple(jobs)
        world.gate.set()
        await _turn()
        assert [e for e in world.log if e[0] == "arm_timer"] == [
            ("arm_timer", job, 1, GRACE_KILL) for job in jobs
        ]
        for job in jobs:
            assert engine.state == "DRAINING"
            attempts.status_recorded(job, signaled(signal.SIGTERM))
            attempts.plan_done(job)
        assert world.log[-2:] == [("clean_shutdown",), ("exit", 0)]
        await attempts.join()

    asyncio.run(scenario())


def test_a_command_that_ignores_term_is_killed_and_the_timer_armed_after_the_kill() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7})
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        await _turn()
        assert world.log == [("TERM", 7), ("KILL", 7), ("arm_timer", job, 1, GRACE_KILL)]
        attempts.status_recorded(job, signaled(signal.SIGKILL))
        assert attempts.outcome(job) == AttemptOutcome(
            "stopped",
            cause=UPDATE_STOP,
            record=exit_record_from_status(signaled(9), stop_in_progress=True, spawn=spawn(7)),
        )
        assert engine.state == "RUNNING"  # an update stop: the engine is not involved

    asyncio.run(scenario())


def test_an_exit_during_a_stop_is_the_stops_and_its_ladder_sends_nothing_more() -> None:
    async def scenario() -> None:
        world = World(gated=True, ignores_term={7})
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        await _turn()
        attempts.status_recorded(job, exited(0))
        world.gate.set()
        await attempts.join()
        assert world.log == [
            ("TERM", 7),
            ("outcome", job, "stopped", UPDATE_STOP),
            ("cancel_timer", job),
        ]
        assert engine.state == "RUNNING"  # an attempt that ends while RUNNING posts no settle

    asyncio.run(scenario())


def test_a_stop_before_spawn_json_kills_the_wrapper_and_the_attempt_is_unobservable() -> None:
    world = World()
    attempts, _ = _tiers(world)
    job = attempts.dispatch("j", 1, 1)
    attempts.stop(job, ENGINE_STOP)
    assert world.log == [("kill_wrapper", job), ("arm_timer", job, 1, GRACE_KILL)]
    attempts.wrapper_exited(job, None)
    assert world.since(2) == [
        ("outcome", job, "unobservable", ENGINE_STOP),
        ("cancel_timer", job),
    ]


def test_a_wrapper_that_launched_after_the_stop_gets_a_ladder_and_a_new_generation() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7})
        attempts, _ = _tiers(world)
        job = attempts.dispatch("j", 1, 1)
        attempts.stop(job, UPDATE_STOP)
        first, _ = world.timers[job]
        attempts.spawn_recorded(job, spawn(7))
        await _turn()
        assert world.log == [
            ("kill_wrapper", job),
            ("arm_timer", job, 1, GRACE_KILL),
            ("TERM", 7),
            ("KILL", 7),
            ("arm_timer", job, 2, GRACE_KILL),
        ]
        attempts.record_timeout(job, first)  # the first timer fires late: stale, discarded
        assert attempts.state(job) == "STOPPING"
        _fire(world, attempts, job)
        assert attempts.outcome(job) == AttemptOutcome("unobservable", cause=UPDATE_STOP)
        # a record after the timeout is evidence; it does not reopen the attempt
        attempts.status_recorded(job, signaled(signal.SIGKILL))
        assert attempts.state(job) == "ENDED" and attempts.outcome(job) == AttemptOutcome(
            "unobservable", cause=UPDATE_STOP
        )
        assert [r.signal for r in attempts.late_records[job]] == [9]
        await attempts.join()

    asyncio.run(scenario())


def test_a_wrapper_that_exits_before_any_record_ends_the_attempt_unobservable() -> None:
    """A wrapper that refused its spec, or died before `spawn.json`, names no command."""
    world = World()
    attempts, _ = _tiers(world)
    job = attempts.dispatch("j", 1, 1)
    attempts.wrapper_exited(job, None)
    assert world.log == [("outcome", job, "unobservable", None), ("cancel_timer", job)]


def test_a_spawn_json_seen_at_the_timeout_stops_the_command_instead() -> None:
    """The wrapper launched after its kill was sent and recorded the launch late: the timeout
    reads the spool, finds `spawn.json`, and the ladder runs on the group under a new
    generation instead of the attempt ending unobservable."""

    async def scenario() -> None:
        world = World(ignores_term={7})
        attempts, _ = _tiers(world)
        job = attempts.dispatch("j", 1, 1)
        attempts.stop(job, UPDATE_STOP)
        world.spool[job] = (spawn(7), None)
        _fire(world, attempts, job)
        assert attempts.state(job) == "STOPPING"
        await _turn()
        assert world.since(2) == [
            ("read_spool", job),
            ("TERM", 7),
            ("KILL", 7),
            ("arm_timer", job, 2, GRACE_KILL),
        ]
        world.spool[job] = (spawn(7), None)
        _fire(world, attempts, job)  # no record still: the second timeout ends it
        assert attempts.outcome(job) == AttemptOutcome("unobservable", cause=UPDATE_STOP)
        await attempts.join()

    asyncio.run(scenario())


def test_a_status_json_seen_at_the_timeout_ends_the_attempt_from_the_record() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7})
        attempts, _ = _tiers(world)
        job = _running(attempts, "j", 7)
        attempts.stop(job, ENGINE_STOP)
        await _turn()
        world.spool[job] = (spawn(7), signaled(signal.SIGKILL))
        _fire(world, attempts, job)
        outcome = attempts.outcome(job)
        assert outcome is not None and (outcome.kind, outcome.cause) == ("stopped", ENGINE_STOP)
        assert world.log[-3:] == [
            ("read_spool", job),
            ("outcome", job, "stopped", ENGINE_STOP),
            ("cancel_timer", job),
        ]
        await attempts.join()

    asyncio.run(scenario())


def test_a_wrapper_gone_with_its_command_dead_ends_the_attempt_unobservable() -> None:
    world = World()
    attempts, _ = _tiers(world, command_alive=lambda pgid, token: False)
    job = _running(attempts, "j", 7)
    attempts.wrapper_exited(job, spawn(7))
    assert world.log == [("outcome", job, "unobservable", None), ("cancel_timer", job)]


def test_a_wrapper_gone_with_its_command_alive_stops_the_command_as_wrapper_lost() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7}, alive={7})
        attempts, _ = _tiers(world)
        job = attempts.dispatch("j", 1, 1)
        attempts.wrapper_exited(job, spawn(7))  # the spawn record not yet seen is posted first
        assert attempts.state(job) == "STOPPING"
        await _turn()
        assert world.log == [("TERM", 7), ("KILL", 7), ("arm_timer", job, 1, GRACE_KILL)]
        _fire(world, attempts, job)  # the KILL ended the command: the timeout ends the attempt
        assert attempts.outcome(job) == AttemptOutcome("unobservable", cause=WRAPPER_LOST)

    asyncio.run(scenario())


def test_a_wrapper_gone_while_stopping_ends_the_attempt_unobservable() -> None:
    async def scenario() -> None:
        world = World(gated=True)
        attempts, _ = _tiers(world)
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        await _turn()
        attempts.wrapper_exited(job, spawn(7))
        world.gate.set()
        await attempts.join()
        assert world.log == [
            ("TERM", 7),
            ("outcome", job, "unobservable", UPDATE_STOP),
            ("cancel_timer", job),
        ]

    asyncio.run(scenario())


def test_a_spawn_failure_ends_the_attempt_before_and_during_a_stop() -> None:
    world = World()
    attempts, _ = _tiers(world)
    first = attempts.dispatch("j", 1, 1)
    attempts.spawn_failed(first, "EMFILE")
    assert attempts.outcome(first) == AttemptOutcome("spawn_failed", error="EMFILE")
    second = attempts.dispatch("j", 1, 2)
    attempts.stop(second, ENGINE_STOP)
    attempts.spawn_failed(second, None)
    assert attempts.outcome(second) == AttemptOutcome("spawn_failed")


def test_a_record_with_no_spawn_json_ends_the_attempt_from_spawning() -> None:
    world = World()
    attempts, _ = _tiers(world)
    for n, status, launched in ((1, SPAWN_FAILED_STATUS, False), (2, RECORD_FAILED, True)):
        job = attempts.dispatch("j", 1, n)
        attempts.status_recorded(job, status)
        outcome = attempts.outcome(job)
        assert outcome is not None and outcome.record is not None
        assert (outcome.kind, outcome.record.launched) == ("spawn_failed", launched)


def test_a_natural_exit_while_running_is_natural() -> None:
    world = World()
    attempts, _ = _tiers(world)
    job = _running(attempts, "j", 7)
    attempts.status_recorded(job, signaled(signal.SIGSEGV))
    assert attempts.outcome(job) == AttemptOutcome(
        "natural",
        record=exit_record_from_status(signaled(11), stop_in_progress=False, spawn=spawn(7)),
    )


def test_an_ended_attempt_ignores_every_later_observation() -> None:
    """The wrapper was killed before it recorded a launch and no record came: the attempt
    ended unobservable. Whatever is seen later changes nothing; a record is kept as evidence."""
    world = World()
    attempts, _ = _tiers(world, command_alive=lambda pgid, token: False)
    job = attempts.dispatch("j", 1, 1)
    attempts.stop(job, UPDATE_STOP)
    _fire(world, attempts, job)
    mark = len(world.log)
    attempts.spawn_failed(job, None)
    attempts.spawn_recorded(job, spawn(7))
    attempts.stop(job, UPDATE_STOP)
    attempts.wrapper_exited(job, None)
    attempts.record_timeout(job, 1)  # a fire that raced its cancel
    attempts.status_recorded(job, exited(1))
    assert world.since(mark) == [] and attempts.state(job) == "ENDED"
    assert attempts.outcome(job) == AttemptOutcome("unobservable", cause=UPDATE_STOP)
    assert [r.exit_code for r in attempts.late_records[job]] == [1]


def test_a_second_stop_keeps_the_first_cause() -> None:
    async def scenario() -> None:
        world = World(gated=True)
        attempts, _ = _tiers(world)
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        attempts.stop_all(ENGINE_STOP)
        await _turn()
        assert world.log == [("TERM", 7)]  # one ladder
        attempts.status_recorded(job, signaled(signal.SIGTERM))
        outcome = attempts.outcome(job)
        assert outcome is not None and outcome.cause == UPDATE_STOP
        world.gate.set()
        await attempts.join()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_at", ["TERM", "KILL"])
def test_a_ladder_that_raises_is_reported_arms_nothing_and_never_settles(fail_at: str) -> None:
    """The stop was not carried out and the command lives: no outcome, no plan, no rerun, and
    the drain cannot report clean."""

    async def scenario() -> None:
        world = World(fail_at=fail_at, ignores_term={7}, alive={7})
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        engine.stop_requested(REQUEST)
        await _turn()
        signals = [] if fail_at == "TERM" else [("TERM", 7)]
        assert world.since(3) == [*signals, ("ladder_failed", job, "killpg failed")]
        assert world.timers == {}
        # a timer of this attempt fires all the same: the command lives, so it stays
        attempts.record_timeout(
            job, attempts.machines.generation(attempts.key(job), "record_timeout")
        )
        assert world.log[-2:] == [("read_spool", job), ("command_survived", job)]
        assert attempts.state(job) == "STOPPING" and attempts.unsettled() == (job,)
        assert engine.state == "DRAINING" and world.alive == {7}
        assert not any(e[0] in ("outcome", "clean_shutdown") for e in world.log)
        with pytest.raises(OSError, match="killpg failed"):
            await attempts.join()

    asyncio.run(scenario())


def test_a_command_alive_at_its_record_timeout_keeps_the_attempt_stopping() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7}, unkillable={7}, alive={7})
        attempts, _ = _tiers(world)
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        await _turn()
        assert world.log == [("TERM", 7), ("KILL", 7), ("arm_timer", job, 1, GRACE_KILL)]
        _fire(world, attempts, job)
        assert world.since(3) == [("read_spool", job), ("command_survived", job)]
        assert attempts.state(job) == "STOPPING" and attempts.outcome(job) is None
        await attempts.join()

    asyncio.run(scenario())


def test_a_repeated_spawn_record_posts_once_in_running_and_in_stopping() -> None:
    async def scenario() -> None:
        world = World(ignores_term={7, 8})
        attempts, _ = _tiers(world)
        posted: list[str] = []
        dispatch = attempts.machines.dispatch

        def spy(key: Any, event: Event, after: Any = None) -> list[Outcome]:
            posted.append(event.name)
            return dispatch(key, event, after)

        attempts.machines.dispatch = spy  # type: ignore[method-assign]
        running = _running(attempts, "j", 7)
        attempts.spawn_recorded(running, spawn(7))
        assert attempts.state(running) == "RUNNING"
        stopping = attempts.dispatch("k", 1, 1)
        attempts.stop(stopping, UPDATE_STOP)
        attempts.spawn_recorded(stopping, spawn(8))
        attempts.spawn_recorded(stopping, spawn(8))
        await _turn()
        assert posted.count("spawn_recorded") == 2  # one per attempt
        assert [e for e in world.log if e[0] == "TERM"] == [("TERM", 8)]  # one ladder
        assert attempts.machines.generation(attempts.key(stopping), "record_timeout") == 2
        assert attempts.state(stopping) == "STOPPING"
        await attempts.join()

    asyncio.run(scenario())


def test_a_repeated_spawn_failure_or_replay_done_posts_nothing() -> None:
    world = World()
    attempts, engine = _tiers(world)
    engine.replay_done()
    engine.replay_done()
    assert engine.state == "RUNNING"
    job = _running(attempts, "j", 7)
    attempts.spawn_failed(job, "late")  # spawn.json was seen: the spawn did not fail
    assert attempts.state(job) == "RUNNING" and world.log == []


def test_an_ended_attempt_is_settled_only_once_its_plan_is_done() -> None:
    attempts = AttemptTier(World(), grace_term_s=GRACE_TERM, grace_kill_s=GRACE_KILL)
    job = attempts.dispatch("j", 1, 1)
    attempts.spawn_failed(job, None)
    assert attempts.state(job) == "ENDED" and attempts.unsettled() == (job,)
    attempts.plan_done(job)  # no engine tier listens: nothing else happens
    assert attempts.unsettled() == ()


def test_an_attempt_whose_plan_is_not_done_cannot_be_forgotten() -> None:
    async def scenario() -> None:
        world = World()
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        engine.stop_requested(REQUEST)
        await _turn()
        attempts.status_recorded(job, signaled(signal.SIGTERM))  # ended, its plan not done
        with pytest.raises(ValueError, match="plan is not done"):
            attempts.forget(job)
        assert attempts.unsettled() == (job,) and engine.state == "DRAINING"
        attempts.plan_done(job)
        assert engine.state == "STOPPED"
        attempts.forget(job)
        assert attempts.unsettled() == ()
        await attempts.join()

    asyncio.run(scenario())


def test_an_intent_with_no_effect_is_refused() -> None:
    world = World()
    attempts, engine = _tiers(world)
    job = attempts.dispatch("j", 1, 1)
    bogus = Outcome(attempts.key(job), Event("x"), "fired", intents=(Intent("bogus"),))
    with pytest.raises(ValueError, match="no effect for the intent 'bogus'"):
        attempts._perform(job, [bogus])
    with pytest.raises(ValueError, match="no effect for the intent 'bogus'"):
        engine._perform(Intent("bogus"))
    assert RECORD_OUTCOME != "bogus" and world.log == []


def test_a_spawn_record_that_names_no_group_gets_no_signal_only_the_timer() -> None:
    world = World()
    attempts, _ = _tiers(world)
    job = attempts.dispatch("j", 1, 1)
    attempts.spawn_recorded(job, {**spawn(7), "command_pgid": 8})
    attempts.stop(job, UPDATE_STOP)
    assert world.log == [("arm_timer", job, 1, GRACE_KILL)]


def test_a_forgotten_attempt_drops_a_late_timer_and_an_unknown_one_is_refused() -> None:
    world = World()
    attempts, _ = _tiers(world)
    job = attempts.dispatch("j", 1, 1)
    attempts.spawn_failed(job, None)
    attempts.plan_done(job)
    attempts.forget(job)
    attempts.record_timeout(job, 1)
    with pytest.raises(KeyError, match="no attempt 'j.1.1'"):
        attempts.stop(job, UPDATE_STOP)


def test_a_ladder_cancelled_after_its_attempt_was_forgotten_arms_nothing() -> None:
    async def scenario() -> None:
        world = World(gated=True, swallow_cancel=True)
        attempts, _ = _tiers(world)
        job = _running(attempts, "j", 7)
        attempts.stop(job, UPDATE_STOP)
        await _turn()
        ladders = asyncio.all_tasks() - {asyncio.current_task()}
        attempts.status_recorded(job, exited(0))
        attempts.plan_done(job)
        attempts.forget(job)
        # the ladder returns after its attempt is gone: no exception, nothing armed
        assert await asyncio.gather(*ladders, return_exceptions=True) == [None]
        await _turn()
        assert world.log[-1] == ("cancel_timer", job) and world.timers == {}

    asyncio.run(scenario())


# ---- the engine tier


def test_a_stop_request_during_startup_journals_it_and_exits_0() -> None:
    world = World()
    attempts, engine = _tiers(world)
    assert engine.state == "STARTING"
    engine.stop_requested(REQUEST)
    assert world.log == [("shutdown_during_startup",), ("exit", 0)]
    engine.replay_done()  # the replay finishes before the process exits: ignored
    engine.stop_requested(SECOND)
    assert engine.state == "STOPPED" and len(world.log) == 2 and engine.deadline is None


def test_the_deadline_ends_a_drain_unclean_with_the_unsettled_attempts() -> None:
    async def scenario() -> None:
        world = World(gated=True)
        attempts, engine = _tiers(world)
        engine.replay_done()
        jobs = [_running(attempts, f"j{n}", 10 + n) for n in range(2)]
        engine.stop_requested(REQUEST)
        await _turn()
        engine.abort_deadline()
        assert world.log[-2:] == [("unclean_shutdown", tuple(jobs)), ("exit", 0)]
        attempts.status_recorded(jobs[0], exited(0))  # a record after the exit was asked for
        assert engine.state == "STOPPED" and world.log[-1] == ("cancel_timer", jobs[0])
        world.gate.set()
        await attempts.join()

    asyncio.run(scenario())


def test_a_second_stop_request_moves_the_deadline_to_now() -> None:
    async def scenario() -> None:
        world = World(gated=True)
        attempts, engine = _tiers(world)
        engine.replay_done()
        job = _running(attempts, "j", 7)
        engine.stop_requested(REQUEST)
        await _turn()
        mark = len(world.log)
        engine.stop_requested(SECOND)
        assert world.since(mark) == [("arm_abort_deadline", 103.0)]
        assert engine.deadline == 103.0 and engine.state == "DRAINING"
        engine.abort_deadline()
        assert world.log[-2:] == [("unclean_shutdown", (job,)), ("exit", 0)]
        world.gate.set()
        await attempts.join()

    asyncio.run(scenario())


# ---- the charts' coverage through the tiers alone

TIER_SCENARIOS = [
    test_a_drain_with_no_attempt_shuts_down_clean_at_once,
    test_a_drain_with_one_attempt_stops_it_and_shuts_down_clean_when_it_ends,
    test_a_drain_with_twenty_attempts_runs_their_ladders_together_and_awaits_none,
    test_a_command_that_ignores_term_is_killed_and_the_timer_armed_after_the_kill,
    test_an_exit_during_a_stop_is_the_stops_and_its_ladder_sends_nothing_more,
    test_a_spawn_json_seen_at_the_timeout_stops_the_command_instead,
    test_a_status_json_seen_at_the_timeout_ends_the_attempt_from_the_record,
    test_a_stop_before_spawn_json_kills_the_wrapper_and_the_attempt_is_unobservable,
    test_a_wrapper_that_launched_after_the_stop_gets_a_ladder_and_a_new_generation,
    test_a_wrapper_that_exits_before_any_record_ends_the_attempt_unobservable,
    test_a_wrapper_gone_with_its_command_dead_ends_the_attempt_unobservable,
    test_a_wrapper_gone_with_its_command_alive_stops_the_command_as_wrapper_lost,
    test_a_wrapper_gone_while_stopping_ends_the_attempt_unobservable,
    test_a_spawn_failure_ends_the_attempt_before_and_during_a_stop,
    test_a_record_with_no_spawn_json_ends_the_attempt_from_spawning,
    test_a_natural_exit_while_running_is_natural,
    test_an_ended_attempt_ignores_every_later_observation,
    test_a_second_stop_keeps_the_first_cause,
    test_a_command_alive_at_its_record_timeout_keeps_the_attempt_stopping,
    test_a_stop_request_during_startup_journals_it_and_exits_0,
    test_the_deadline_ends_a_drain_unclean_with_the_unsettled_attempts,
    test_a_second_stop_request_moves_the_deadline_to_now,
]

#: The one coverage item the tiers never reach, and why: the engine tier posts
#: `all_attempts_settled` only while it reads DRAINING, so STOPPED never meets it. The session's
#: gate takes this item from the chart's own completeness test (tests/test_engine_charts.py).
UNREACHABLE_FROM_THE_TIERS = {("engine_lifecycle", "ignored", "STOPPED", "all_attempts_settled")}


def test_the_tiers_alone_reach_every_coverage_item_of_both_charts_but_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = tmp_path / "hits"
    hits.mkdir()
    monkeypatch.setenv(HITS_ENV, str(hits))
    for scenario in TIER_SCENARIOS:
        scenario()
    seen = {
        (r["machine"], r["kind"], r["id"], r["detail"])
        for path in hits.glob("hits-*.jsonl")
        for r in map(json.loads, path.read_text(encoding="utf-8").splitlines())
    }
    missed = set()
    for chart in (ENGINE_LIFECYCLE, ATTEMPT):
        required, forbidden = coverage_items(chart)
        missed |= {(chart.name, *item) for item in required} - seen
        assert not {(chart.name, *item) for item in forbidden} & seen
    assert missed == UNREACHABLE_FROM_THE_TIERS
