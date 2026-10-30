"""The engine lifecycle and the attempt as declared statecharts (docs/statechart-engine.md).

`ENGINE_LIFECYCLE` is the engine process: STARTING, RUNNING, DRAINING, STOPPED. `ATTEMPT` is one
dispatched run's process: SPAWNING, RUNNING, STOPPING, ENDED. The charts run on the engine's
executor, `ENGINE_KEY` for the one lifecycle instance and one instance per attempt.

An attempt instance is keyed by the attempt id, never by the job or the run: a late timer of a
settled attempt must not reach its successor.

Events of the attempt: `spawn_recorded`, `spawn_failed`, `stop` (cause `engine_stop`,
`update_stop` or `wrapper_lost`), `exit_recorded`, `wrapper_gone` and `record_timeout`.
`wrapper_gone` means the wrapper exited without a record and the command is dead; the tier
establishes both before it posts. A wrapper lost while its command lives is a `stop` with cause
`wrapper_lost`.

The charts do no I/O and read no clock. A tier posts the events, with the time it read in the
payload, and carries out the `Intent`s an outcome reports. Both charts end in a final state, which
ignores every later event (statechart-engine ss10), so a late record or a second `abort_deadline`
needs no row.

Two stops use one attempt path. A drain sends `stop` with cause `engine_stop` to every unsettled
attempt; an update sends `stop` with cause `update_stop` to the attempts of the jobs it touches.
The engine chart has no event for an update. The TERM, grace, KILL ladder is the one `stop`
intent; the attempt arms one timer, `record_timeout`, and only in STOPPING.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from dsl41.state_machine import (
    Context,
    Effects,
    Exclusion,
    Region,
    Row,
    State,
    Statechart,
    TimeEvent,
)

#: The key of the one lifecycle instance on the engine's executor.
ENGINE_KEY: Final = "engine"

# -- the engine's events

#: The replay of the WAL is done and the engine serves.
REPLAY_DONE: Final = "replay_done"
#: SIGTERM or `dsl41 engine stop`. The payload is a `DrainRequest`.
STOP_REQUESTED: Final = "stop_requested"
#: Every attempt is settled. The tier posts it only in DRAINING: when the last unsettled attempt
#: ends, or at once with none.
ALL_ATTEMPTS_SETTLED: Final = "all_attempts_settled"
#: The drain's deadline passed. The payload is the tuple of unsettled attempt ids.
ABORT_DEADLINE: Final = "abort_deadline"

# -- the attempt's events

#: `spawn.json` was seen.
SPAWN_RECORDED: Final = "spawn_recorded"
#: The spawn failed and the attempt never ran. The payload is the error text, or None.
SPAWN_FAILED: Final = "spawn_failed"
#: Stop the attempt. The payload is the cause, `ENGINE_STOP` or `UPDATE_STOP`.
STOP: Final = "stop"
#: The wrapper's record of the command's end. The payload is an `ExitRecord`.
EXIT_RECORDED: Final = "exit_recorded"
#: The wrapper exited and left no record, and the command is dead. The tier establishes both
#: before it posts the event. A wrapper lost while its command lives gets `stop` with cause
#: `WRAPPER_LOST` instead, so the ladder runs and `record_timeout` ends the attempt.
WRAPPER_GONE: Final = "wrapper_gone"
#: `grace_kill` passed after the last KILL and no record came. It is re-armable: the tier posts
#: it with the generation the chart holds when the timer was armed.
RECORD_TIMEOUT: Final = "record_timeout"

#: The causes of `stop`. `WRAPPER_LOST` is for a wrapper that exited with no record while its
#: command may still live.
ENGINE_STOP: Final = "engine_stop"
UPDATE_STOP: Final = "update_stop"
WRAPPER_LOST: Final = "wrapper_lost"

#: The seconds the drain's deadline adds to `grace_term_s + grace_kill_s`.
DRAIN_MARGIN_S: Final = 5.0

# -- the intents: the named effects a tier carries out, in the order an outcome lists them

JOURNAL_DRAINING: Final = "journal_draining"
STOP_ATTEMPTS: Final = "stop_attempts"
CANCEL_WATCHERS: Final = "cancel_watchers"
ARM_ABORT_DEADLINE: Final = "arm_abort_deadline"
RECORD_CLEAN_SHUTDOWN: Final = "record_clean_shutdown"
RECORD_UNCLEAN_SHUTDOWN: Final = "record_unclean_shutdown"
EXIT_PROCESS: Final = "exit_process"
STOP_ATTEMPT: Final = "stop_attempt"
#: Arm `record_timeout` at `grace_kill` after the last KILL. The chart moves the timer to a new
#: generation in the same microstep; the action cannot see it, so the tier reads
#: `Executor.generation(key, RECORD_TIMEOUT)` right after the macrostep, before it posts any
#: other event, and posts the timer's event with that generation.
ARM_RECORD_TIMEOUT: Final = "arm_record_timeout"
CANCEL_RECORD_TIMEOUT: Final = "cancel_record_timeout"
JOURNAL_SHUTDOWN_DURING_STARTUP: Final = "journal_shutdown_during_startup"
RECORD_OUTCOME: Final = "record_outcome"


@dataclass(frozen=True)
class Intent:
    """One named effect and what it needs. The tier carries it out."""

    name: str
    detail: Any = None


@dataclass(frozen=True)
class DrainRequest:
    """The payload of `stop_requested`: the time the tier read and the engine's two graces."""

    now: float
    grace_term_s: float
    grace_kill_s: float


@dataclass
class Lifecycle:
    """The data of the lifecycle instance: the drain's deadline, once it has one."""

    deadline: float | None = None


@dataclass(frozen=True)
class ExitRecord:
    """The payload of `exit_recorded`: the wrapper's `status.json`, with no AutoSys status (the
    oracle computes it from the raw exit code). `terminated_by` is None for a command that ended
    by itself, `lifeline` or `stop` for a kill, `wrapper` when the wrapper aborted its own
    launch. The times are the wrapper's ISO strings. `wrapper_outcome`, `cause` and `error` are
    the record's own fields, which the adapter's oracle mapping reads. `launched` is False only
    when the wrapper never launched the command; `observed` is the exit the wrapper saw after
    its own kill (docs/wrapper-protocol.md)."""

    exit_code: int | None = None
    signal: int | None = None
    terminated_by: Literal["lifeline", "stop", "wrapper"] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    wrapper_outcome: str | None = None
    cause: str | None = None
    launched: bool = True
    observed: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class AttemptOutcome:
    """How an attempt ended: the data of ENDED, not a state. `record` is the evidence of an
    outcome that came from a wrapper record; `error` is the text of a failed spawn."""

    kind: Literal["natural", "stopped", "unobservable", "spawn_failed"]
    cause: str | None = None
    record: ExitRecord | None = None
    error: str | None = None


@dataclass
class AttemptRecord:
    """The data of an attempt instance: the cause of the stop it took and how it ended."""

    cause: str | None = None
    outcome: AttemptOutcome | None = None


# -- the engine's actions


def _begin_drain(ctx: Context) -> Effects:
    """Set the deadline to now plus both graces plus five seconds. Journal the draining record,
    send `stop` to every unsettled attempt, cancel the file watchers and arm the deadline."""
    life: Lifecycle = ctx.data
    request: DrainRequest = ctx.event.payload
    life.deadline = request.now + request.grace_term_s + request.grace_kill_s + DRAIN_MARGIN_S
    return Effects(
        intents=(
            Intent(JOURNAL_DRAINING),
            Intent(STOP_ATTEMPTS, ENGINE_STOP),
            Intent(CANCEL_WATCHERS),
            Intent(ARM_ABORT_DEADLINE, life.deadline),
        )
    )


def _deadline_now(ctx: Context) -> Effects:
    """A second stop request: the deadline is now. The attempts were already told to stop."""
    life: Lifecycle = ctx.data
    request: DrainRequest = ctx.event.payload
    life.deadline = request.now
    return Effects(intents=(Intent(ARM_ABORT_DEADLINE, life.deadline),))


def _shutdown_during_startup(ctx: Context) -> Effects:
    """A stop request during the replay: no attempt is live, so there is nothing to drain.
    Journal `shutdown_during_startup` and exit 0."""
    return Effects(intents=(Intent(JOURNAL_SHUTDOWN_DURING_STARTUP), Intent(EXIT_PROCESS, 0)))


def _clean_shutdown(ctx: Context) -> Effects:
    """Every attempt settled: record `clean_shutdown` and exit 0."""
    return Effects(intents=(Intent(RECORD_CLEAN_SHUTDOWN), Intent(EXIT_PROCESS, 0)))


def _unclean_shutdown(ctx: Context) -> Effects:
    """The deadline passed: record `unclean_shutdown` with the unsettled attempts and exit 0."""
    unsettled: tuple[str, ...] = tuple(ctx.event.payload)
    return Effects(intents=(Intent(RECORD_UNCLEAN_SHUTDOWN, unsettled), Intent(EXIT_PROCESS, 0)))


#: The engine process. Both rows into STOPPED exit 0; the shutdown record tells them apart.
ENGINE_LIFECYCLE: Final = Statechart(
    name="engine_lifecycle",
    root=State(
        "engine",
        regions=(
            Region(
                "phase",
                "STARTING",
                (
                    State("STARTING"),
                    State("RUNNING"),
                    State("DRAINING"),
                    State("STOPPED", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "engine_lifecycle.01",
            "STARTING",
            REPLAY_DONE,
            "RUNNING",
            note="The WAL replay is done and the engine serves.",
        ),
        Row(
            "engine_lifecycle.02",
            "RUNNING",
            STOP_REQUESTED,
            "DRAINING",
            action="begin_drain",
        ),
        Row(
            "engine_lifecycle.03",
            "DRAINING",
            STOP_REQUESTED,
            kind="internal",
            action="deadline_now",
        ),
        Row(
            "engine_lifecycle.04",
            "DRAINING",
            ALL_ATTEMPTS_SETTLED,
            "STOPPED",
            action="clean_shutdown",
        ),
        Row(
            "engine_lifecycle.05",
            "DRAINING",
            ABORT_DEADLINE,
            "STOPPED",
            action="unclean_shutdown",
        ),
        Row(
            "engine_lifecycle.06",
            "STARTING",
            STOP_REQUESTED,
            "STOPPED",
            action="shutdown_during_startup",
        ),
    ),
    actions={
        "begin_drain": _begin_drain,
        "shutdown_during_startup": _shutdown_during_startup,
        "deadline_now": _deadline_now,
        "clean_shutdown": _clean_shutdown,
        "unclean_shutdown": _unclean_shutdown,
    },
    time_events=(TimeEvent(ABORT_DEADLINE),),
    clock="logical",
    exclusions=(
        Exclusion(
            "STARTING",
            ALL_ATTEMPTS_SETTLED,
            "the tier posts it only while it drains",
        ),
        Exclusion(
            "STARTING",
            ABORT_DEADLINE,
            "the deadline is armed on entry to the drain only",
        ),
        Exclusion(
            "RUNNING",
            REPLAY_DONE,
            "the tier posts replay_done once, from STARTING",
        ),
        Exclusion(
            "RUNNING",
            ALL_ATTEMPTS_SETTLED,
            "the tier posts it only while it drains",
        ),
        Exclusion(
            "RUNNING",
            ABORT_DEADLINE,
            "the deadline is armed on entry to the drain only",
        ),
        Exclusion(
            "DRAINING",
            REPLAY_DONE,
            "the tier posts replay_done once, from STARTING",
        ),
    ),
)


# -- the attempt's actions


def _stop(ctx: Context) -> Effects:
    """Take the stop and keep its cause. One `stop_attempt` intent runs the whole ladder or,
    before the wrapper launched anything, kills the wrapper. The `record_timeout` timer is
    armed after the KILL, under a new generation."""
    record: AttemptRecord = ctx.data
    cause: str = ctx.event.payload
    record.cause = cause
    return Effects(
        intents=(Intent(STOP_ATTEMPT, cause), Intent(ARM_RECORD_TIMEOUT)),
        rearm=(RECORD_TIMEOUT,),
    )


def _stop_again(ctx: Context) -> Effects:
    """The wrapper launched before the stop landed: run the ladder again, on the recorded pgid,
    with the cause the attempt already holds. The timer moves to a new generation, so the first
    ladder's timer is stale."""
    record: AttemptRecord = ctx.data
    return Effects(
        intents=(Intent(STOP_ATTEMPT, record.cause), Intent(ARM_RECORD_TIMEOUT)),
        rearm=(RECORD_TIMEOUT,),
    )


def _end(record: AttemptRecord, outcome: AttemptOutcome) -> Effects:
    """Keep the outcome, journal it and cancel the `record_timeout` timer."""
    record.outcome = outcome
    return Effects(intents=(Intent(RECORD_OUTCOME, outcome), Intent(CANCEL_RECORD_TIMEOUT)))


def outcome_of(exit_record: ExitRecord, cause: str | None) -> AttemptOutcome:
    """The one place an outcome is derived from a wrapper record. `terminated_by` None is
    `natural`. `lifeline` is `stopped{lifeline}`. `stop` is `stopped` with the cause of the stop
    the attempt holds, or `stop` when it holds none. `wrapper` is `spawn_failed`. Every outcome
    carries the record as evidence."""
    match exit_record.terminated_by:
        case None:
            return AttemptOutcome("natural", record=exit_record)
        case "lifeline":
            return AttemptOutcome("stopped", cause="lifeline", record=exit_record)
        case "stop":
            return AttemptOutcome("stopped", cause=cause or "stop", record=exit_record)
        case "wrapper":
            return AttemptOutcome("spawn_failed", record=exit_record)


def _end_spawn_failed(ctx: Context) -> Effects:
    """The attempt never ran: outcome `spawn_failed`, with the error text."""
    error: str | None = ctx.event.payload
    return _end(ctx.data, AttemptOutcome("spawn_failed", error=error))


def _end_from_record(ctx: Context) -> Effects:
    """The wrapper recorded the command's end: the outcome is derived from the record
    (`outcome_of`). A command that exited before the TERM is `natural`; a record that names a
    stop or the lifeline is `stopped`; a wrapper that aborted its own launch is `spawn_failed`."""
    record: AttemptRecord = ctx.data
    return _end(record, outcome_of(ctx.event.payload, record.cause))


def _end_unobservable(ctx: Context) -> Effects:
    """No record: the wrapper is gone, or none came `grace_kill` after the last KILL. Outcome
    `unobservable`, with the cause of the stop the attempt took, if any. A record that comes later is kept as evidence and does not reopen the
    attempt."""
    record: AttemptRecord = ctx.data
    return _end(record, AttemptOutcome("unobservable", cause=record.cause))


#: One dispatched run. ENDED carries the outcome as data.
ATTEMPT: Final = Statechart(
    name="attempt",
    root=State(
        "attempt",
        regions=(
            Region(
                "phase",
                "SPAWNING",
                (
                    State("SPAWNING"),
                    State("RUNNING"),
                    State("STOPPING"),
                    State("ENDED", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "attempt.01",
            "SPAWNING",
            SPAWN_RECORDED,
            "RUNNING",
            note="`spawn.json` was seen.",
        ),
        Row("attempt.02", "SPAWNING", SPAWN_FAILED, "ENDED", action="end_spawn_failed"),
        Row("attempt.03", "SPAWNING", STOP, "STOPPING", action="stop"),
        Row("attempt.04", "RUNNING", STOP, "STOPPING", action="stop"),
        Row("attempt.05", "RUNNING", EXIT_RECORDED, "ENDED", action="end_from_record"),
        Row("attempt.06", "STOPPING", EXIT_RECORDED, "ENDED", action="end_from_record"),
        Row("attempt.07", "STOPPING", RECORD_TIMEOUT, "ENDED", action="end_unobservable"),
        Row(
            "attempt.08",
            "STOPPING",
            STOP,
            kind="ignored",
            note="The ladder already runs; the first cause stands.",
        ),
        Row("attempt.09", "STOPPING", SPAWN_RECORDED, kind="internal", action="stop_attempt"),
        Row("attempt.10", "STOPPING", SPAWN_FAILED, "ENDED", action="end_spawn_failed"),
        Row("attempt.11", "SPAWNING", EXIT_RECORDED, "ENDED", action="end_from_record"),
        Row("attempt.12", "SPAWNING", WRAPPER_GONE, "ENDED", action="end_unobservable"),
        Row("attempt.13", "RUNNING", WRAPPER_GONE, "ENDED", action="end_unobservable"),
        Row("attempt.14", "STOPPING", WRAPPER_GONE, "ENDED", action="end_unobservable"),
    ),
    actions={
        "stop": _stop,
        "stop_attempt": _stop_again,
        "end_spawn_failed": _end_spawn_failed,
        "end_from_record": _end_from_record,
        "end_unobservable": _end_unobservable,
    },
    time_events=(TimeEvent(RECORD_TIMEOUT, generation=True),),
    clock="logical",
    exclusions=(
        Exclusion(
            "SPAWNING",
            RECORD_TIMEOUT,
            "the timer is armed in STOPPING only",
        ),
        Exclusion(
            "RUNNING",
            SPAWN_RECORDED,
            "the tier posts spawn_recorded once, from SPAWNING, and a poll that finds"
            " spawn.json and status.json together posts spawn_recorded first",
        ),
        Exclusion(
            "RUNNING",
            SPAWN_FAILED,
            "a spawn that left spawn.json did not fail",
        ),
        Exclusion(
            "RUNNING",
            RECORD_TIMEOUT,
            "the timer is armed in STOPPING only",
        ),
    ),
)
