"""The 2.x attempt and engine lifecycle tiers (DL-424, DL-426).

An attempt is one launch of a run. Its durable identity is the `run_id` its `dispatch` record
binds, a uuid4 (DL-118); its ordinal `n` counts the attempts of one run from 1 and names its
spool directory and its display (`attempt_id`). The attempt tier keeps one
`engine_charts.ATTEMPT` instance per run id on the executor it is given, posts the events the
adapter's observations imply, and carries out the chart's intents through an injected
`Processes`. The engine tier keeps the one `engine_charts.ENGINE_LIFECYCLE` instance and carries
out its intents through an injected `EngineEffects`. Neither tier reads a clock or touches a
process itself; the engine (`runner.Engine`) is both injections.

The pure parts are here too: the translation of the wrapper's `status.json` into an
`ExitRecord` (`exit_record_from_status`), the verdict for a wrapper that left no record
(`missing_record_verdict`), what each outcome feeds the oracle (`oracle_input_for`), and the
interruption rule's plan for each outcome (`follow_up_plan`). The spool files are the frozen
`docs/wrapper-protocol.md` shapes.

A tier posts each event and handles the outcomes after the macrostep, so an intent that posts
again (the drain's stop of every attempt) never re-enters the executor.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from dsl41.engine_charts import (
    ABORT_DEADLINE,
    ALL_ATTEMPTS_SETTLED,
    ARM_ABORT_DEADLINE,
    ARM_RECORD_TIMEOUT,
    ATTEMPT,
    CANCEL_RECORD_TIMEOUT,
    CANCEL_WATCHERS,
    ENGINE_KEY,
    ENGINE_LIFECYCLE,
    EXIT_PROCESS,
    EXIT_RECORDED,
    JOURNAL_DRAINING,
    JOURNAL_SHUTDOWN_DURING_STARTUP,
    RECORD_CLEAN_SHUTDOWN,
    RECORD_OUTCOME,
    RECORD_TIMEOUT,
    RECORD_UNCLEAN_SHUTDOWN,
    REPLAY_DONE,
    SPAWN_FAILED,
    SPAWN_RECORDED,
    STOP,
    STOP_ATTEMPT,
    STOP_ATTEMPTS,
    STOP_REQUESTED,
    WRAPPER_GONE,
    WRAPPER_LOST,
    AttemptOutcome,
    AttemptRecord,
    DrainRequest,
    ExitRecord,
    Intent,
    Lifecycle,
)
from dsl41.runner_adapters import AdapterResult, Failed, outcome_from_status
from dsl41.runner_procid import verify_alive
from dsl41.state_machine import Event, Executor, Outcome

#: The `status.json` cause the wrapper writes when it killed the group on lifeline EOF.
LIFELINE_CAUSE: Final = "parent lost"
#: The start of the cause the wrapper writes when it launched the command, could not write
#: `spawn.json`, and killed the command (`docs/wrapper-protocol.md`).
SPAWN_RECORD_FAILED: Final = "spawn record write failed"
#: The oracle statuses that count as a live run for the interruption rule: a box member can be
#: dispatched while its row is still STARTING.
LIVE_STATUSES: Final = frozenset({"STARTING", "RUNNING"})
#: The most attempts one run gets: an interrupted attempt numbered this or above is held, not
#: replaced, so the engine replaces an interrupted attempt at most four times (DL-424).
MAX_ATTEMPTS_PER_RUN: Final = 5
#: The cause of a stop the oracle decided: a KILLJOB, a `term_run_time`, a run the oracle ended.
#: Its outcome gets plan `none`: the oracle has ended the run (DL-424).
ORACLE_KILL: Final = "oracle_kill"
#: The attempt states that are not settled.
UNSETTLED: Final = frozenset({"SPAWNING", "RUNNING", "STOPPING"})
_ATTEMPT_STATES: Final = frozenset({*UNSETTLED, "ENDED"})
_ENGINE_STATES: Final = frozenset({"STARTING", "RUNNING", "DRAINING", "STOPPED"})


# -- the attempt's name and spool


def attempt_id(job: str, run_number: int, n: int) -> str:
    """An attempt's display name `<job>.<run>.<n>`; `n` counts from 1 within one run. The
    attempt's identity is its run id, never this name (DL-118)."""
    return f"{job}.{run_number}.{n}"


def attempt_spool_dir(run_root: Path, job: str, run_number: int, n: int) -> Path:
    """An attempt's own spool directory, `runs/<job>.<run>/<n>/`, so a replacement attempt never
    meets the first one's exclusive `mkdir`. The run directory above it keeps the log files."""
    return run_root / "runs" / f"{job}.{run_number}" / str(n)


# -- the exit record


def exit_record_from_status(
    status: Mapping[str, Any],
    *,
    stop_in_progress: bool,
    spawn: Mapping[str, Any] | None = None,
) -> ExitRecord:
    """Translate one `status.json` into one `ExitRecord`.

    | status.json | terminated_by | outcome |
    | exited, no stop in progress | None | natural{exit_code} |
    | exited while this attempt's stop is in progress | stop | stopped{cause} |
    | signaled, no stop in progress | None | natural{signal} |
    | signaled while this attempt's stop is in progress | stop | stopped{cause} |
    | terminated, cause "parent lost" | lifeline | stopped{lifeline} |
    | spawn_failed | wrapper, launched false | spawn_failed |
    | terminated, cause "spawn record write failed ..." | wrapper, launched true | spawn_failed |

    The wrapper never writes `stop`: an exit or a signal is the stop's when it lands while this
    attempt's stop is in progress, which the caller says with `stop_in_progress`; a command
    that exited before the TERM was recorded before the stop. Any other record is natural and
    keeps the
    wrapper's outcome, so `oracle_input_for` maps it as `outcome_from_status` does. `spawn` is
    the attempt's `spawn.json`, which gives the start time; `status.json` has none.
    """
    outcome = status.get("outcome")
    cause = status.get("cause")
    terminated_by: Literal["lifeline", "stop", "wrapper"] | None = None
    launched = True
    if outcome in ("exited", "signaled") and stop_in_progress:
        terminated_by = "stop"
    elif outcome == "terminated" and cause == LIFELINE_CAUSE:
        terminated_by = "lifeline"
    elif outcome == "terminated" and str(cause).startswith(SPAWN_RECORD_FAILED):
        terminated_by = "wrapper"
    elif outcome == "spawn_failed":
        terminated_by, launched = "wrapper", False
    exit_code, sig, observed, error = (
        status.get(name) for name in ("exit_code", "signal", "observed", "error")
    )
    started = spawn.get("started_at") if spawn is not None else None
    ended = status.get("ended_at")
    return ExitRecord(
        exit_code=exit_code if isinstance(exit_code, int) else None,
        signal=sig if isinstance(sig, int) else None,
        terminated_by=terminated_by,
        started_at=started if isinstance(started, str) else None,
        ended_at=ended if isinstance(ended, str) else None,
        wrapper_outcome=outcome if isinstance(outcome, str) else repr(outcome),
        cause=None if cause is None else str(cause),
        launched=launched,
        observed=dict(observed) if isinstance(observed, Mapping) else None,
        error=None if error is None else str(error),
    )


def _status_of(record: ExitRecord) -> dict[str, Any]:
    """The `status.json` fields `outcome_from_status` reads, back from the record."""
    fields = {
        "outcome": record.wrapper_outcome,
        "exit_code": record.exit_code,
        "signal": record.signal,
        "cause": record.cause,
        "error": record.error,
    }
    return {name: value for name, value in fields.items() if value is not None}


# -- the wrapper that left no record

type Verdict = Literal["wait", "wrapper_gone", "wrapper_lost"]


def missing_record_verdict(
    spawn: Mapping[str, Any] | None,
    *,
    wrapper_alive: bool,
    command_alive: Callable[[int, str], bool],
) -> Verdict:
    """What a missing `status.json` means.

    `wait` while the wrapper lives: it will write the record. `wrapper_gone` when the wrapper
    is gone and so is the command: `spawn` names a group that `command_alive` finds dead, or
    there is no `spawn.json` and no command to name. `wrapper_lost` when the wrapper is gone and
    the command lives: the tier posts `stop` with that cause, so the ladder runs on the group.

    `command_alive(pgid, start_token)` is `runner_procid.verify_alive` in production. A
    `spawn.json` without an integer pgid equal to the command pid and a string start token names
    no command this engine may signal, so it counts as none (`docs/wrapper-protocol.md`).
    """
    if wrapper_alive:
        return "wait"
    group = command_group(spawn)
    if group is None:
        return "wrapper_gone"
    return "wrapper_lost" if command_alive(*group) else "wrapper_gone"


def command_group(spawn: Mapping[str, Any] | None) -> tuple[int, str] | None:
    """The (pgid, start token) a `spawn.json` names, or None when it names no group this engine
    may signal: no record, or no integer pgid equal to the command pid and string token."""
    if spawn is None:
        return None
    pgid, pid, token = (spawn.get(k) for k in ("command_pgid", "command_pid", "command_start_time"))
    if isinstance(pgid, int) and pgid == pid and isinstance(token, str):
        return pgid, token
    return None


# -- what the oracle is told


def oracle_input_for(outcome: AttemptOutcome) -> AdapterResult | None:
    """What an outcome feeds the oracle, through the adapter's own table.

    natural feeds what `outcome_from_status` maps the wrapper's record to: the raw exit code,
    which the oracle classifies (DL-41), or TERMINATED for a signal death. spawn_failed feeds
    FAILURE with cause `wrapper_spawn_failed`; the outcome keeps the error as evidence. stopped
    and unobservable feed nothing: the interruption rule decides (DL-424).
    """
    if outcome.kind == "natural":
        if outcome.record is None:
            raise AssertionError("a natural outcome always carries its record")
        return outcome_from_status(_status_of(outcome.record))
    if outcome.kind == "spawn_failed":
        # a literal cause, so the simulation register derives it (DL-209)
        return Failed("wrapper_spawn_failed")
    return None


# -- the interruption rule

#: What a held interrupted attempt feeds the oracle, with `hold: true` beside it: a FAILURE with
#: cause `interrupted` that holds the job in the same transition (DL-424). A literal cause, so
#: the simulation register derives it (DL-209).
HOLD_INPUT: Final = Failed("interrupted")

FEED_STATUS: Final = "feed_status"
SET_ON_HOLD: Final = "set_on_hold"
DISPATCH: Final = "dispatch"


@dataclass(frozen=True)
class Interrupted:
    """The evidence a held interrupted attempt leaves on its row: the outcome's kind and cause,
    the wrapper's record where there is one (its signal or exit code, top level or as the
    wrapper observed it after its own kill), and the attempt limit when that is why it is
    held."""

    attempt: str
    kind: str
    cause: str | None
    started_at: str | None
    ended_at: str | None
    signal: int | None
    exit_code: int | None
    limit: int | None = None
    #: the attempt's run id; `attempt` is its display name
    run_id: str | None = None


@dataclass(frozen=True)
class Replacement:
    """The attempt a `rerun` plan dispatches: its run id, minted when the plan is chosen so the
    plan names it before it can run, and its ordinal."""

    run_id: str
    ordinal: int


@dataclass(frozen=True)
class FollowUpStep:
    """One effect of a plan. `detail` is what it needs: the oracle input of `feed_status`, the
    evidence of `set_on_hold`, the `Replacement` of `dispatch`. `done_seq` is the
    journal sequence of the transaction that performed it, None until then."""

    effect: str
    detail: Any = None
    done_seq: int | None = None

    def done(self, seq: int) -> FollowUpStep:
        return replace(self, done_seq=seq)


@dataclass(frozen=True)
class FollowUp:
    """The plan committed with an outcome: `complete`, `none`, `hold` or `rerun`, and its steps in
    the order they run."""

    kind: Literal["complete", "none", "hold", "rerun"]
    steps: tuple[FollowUpStep, ...] = ()


def follow_up_plan(
    outcome: AttemptOutcome,
    *,
    job: str,
    run_number: int,
    attempt: int,
    in_catalog: bool,
    live_run: tuple[int, str] | None,
    marked: bool,
    run_id: str | None = None,
    mint: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> FollowUp:
    """The interruption rule (DL-424), as a plan for the outcome of attempt `attempt` of run
    `run_number` of `job`.

    The caller's precondition: `in_catalog` and `marked` are read from the applied catalog and
    its `non-idempotent.toml`. An update in STOPPING is applied before any plan is chosen.
    `live_run` is the oracle's current (run number, status) for the job, or None.

    natural: `complete`, feed the completion. spawn_failed: `complete`, feed its FAILURE.
    stopped or unobservable: `none` when the job left the catalog, when the stop was the
    oracle's own kill (`oracle_kill`), or when the oracle's current run is not this run in
    STARTING or RUNNING, because the oracle's own end stands. Else `hold` for a marked job, and
    for attempt `MAX_ATTEMPTS_PER_RUN` or above: one step, a FAILURE with cause `interrupted`
    that holds the job in the same transition, with the evidence, which names the limit in the
    second case. Else `rerun`: one dispatch of the next attempt of the same run, under a run id `mint`
    gives. `run_id` is the interrupted attempt's own, which the evidence carries.
    """
    if outcome.kind in ("natural", "spawn_failed"):
        return FollowUp("complete", (FollowUpStep(FEED_STATUS, oracle_input_for(outcome)),))
    if not in_catalog or outcome.cause == ORACLE_KILL:
        # the oracle ended the run itself; nothing follows, whatever its row says now
        return FollowUp("none")
    if live_run is None or live_run[0] != run_number or live_run[1] not in LIVE_STATUSES:
        return FollowUp("none")
    this = attempt_id(job, run_number, attempt)
    limited = attempt >= MAX_ATTEMPTS_PER_RUN
    # PENDING: E7 -- an unobservable attempt takes this rule; the FAILURE it feeds is a marked
    # job's `interrupted`, never `exit_status_unobservable` (DL-424 narrows E7)
    if marked or limited:
        record = outcome.record or ExitRecord()
        observed = record.observed or {}
        sig, code = (
            value if value is not None else _wire_int(observed.get(name))
            for value, name in ((record.signal, "signal"), (record.exit_code, "exit_code"))
        )
        evidence = Interrupted(
            this,
            outcome.kind,
            outcome.cause,
            record.started_at,
            record.ended_at,
            sig,
            code,
            MAX_ATTEMPTS_PER_RUN if limited else None,
            run_id,
        )
        # one oracle transition: the FAILURE holds the job before its own start or a dependent's
        # is evaluated, so a marked job never runs again on its own failure (DL-424)
        return FollowUp("hold", (FollowUpStep(SET_ON_HOLD, evidence),))
    return FollowUp("rerun", (FollowUpStep(DISPATCH, Replacement(mint(), attempt + 1)),))


def _wire_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -- the attempt tier


class Processes(Protocol):
    """What the attempt tier asks of the world. Each call returns at once except `ladder`."""

    async def ladder(self, pgid: int, start_token: str, grace_term_s: float) -> None:
        """TERM the group, wait up to `grace_term_s` for it to go, KILL it if it lives. Each
        signal goes only to a group whose leader still matches `start_token`."""

    def kill_wrapper(self, attempt: str) -> None:
        """Kill the attempt's wrapper, which has not yet recorded a launch (DL-359)."""

    def arm_timer(self, key: str, generation: int, delay_s: float) -> None:
        """Call `AttemptTier.record_timeout(key, generation)` after `delay_s`; replaces any
        timer under `key`."""

    def cancel_timer(self, key: str) -> None: ...

    def read_spool(self, attempt: str) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        """The attempt's `spawn.json` and `status.json` as they are now, each None when absent
        or unreadable."""

    def record_outcome(self, attempt: str, outcome: AttemptOutcome) -> None:
        """Journal the outcome and the interruption rule's plan in one transaction. The attempt
        stays unsettled until `AttemptTier.plan_done`."""

    def ladder_failed(self, attempt: str, error: BaseException) -> None:
        """Report a ladder that raised. No timer is armed; the attempt stays STOPPING."""

    def command_survived(self, attempt: str) -> None:
        """Report a command still alive when its record timeout fired; the attempt stays
        STOPPING."""

    async def outlives(self, pgid: int, start_token: str, grace_kill_s: float) -> bool:
        """Whether anything of the command is still left `grace_kill_s` after its ladder's
        KILL: an ended attempt's survivor verdict, taken where a stopping one's record timeout
        takes it."""

    def leaked(self, attempt: str) -> bool:
        """Report an attempt that ends with no stop while processes of its command are left:
        a leak. True: the tier runs the ladder on them (`process-leak=terminate`); False: they
        are left running (`leak`, the vendor's reading)."""


@dataclass
class _Attempt:
    """The tier's own facts about one attempt: its spawn record and its ladder task."""

    spawn: Mapping[str, Any] | None = None
    ladder: asyncio.Task[None] | None = None
    plan_done: bool = False
    #: its group still had a live member after the ladder's KILL
    survived: bool = False
    #: its ladder raised: the stop was not carried out, so its group is unproven
    failed: bool = False


class AttemptTier:
    """One `ATTEMPT` instance per attempt, on `machines`, keyed by this tier and the attempt's
    run id.

    The methods are the adapter's observations. A ladder runs as a task of its own, so a stop
    returns at once and the ladders of many attempts run together. `on_settled` is called after
    an attempt ends and its intents are done.
    """

    def __init__(
        self,
        processes: Processes,
        *,
        grace_term_s: float,
        grace_kill_s: float,
        machines: Executor | None = None,
        command_alive: Callable[[int, str], bool] = verify_alive,
    ) -> None:
        self.processes = processes
        self.grace_term_s = grace_term_s
        self.grace_kill_s = grace_kill_s
        self.machines = machines if machines is not None else Executor()
        self.command_alive = command_alive
        self.on_settled: Callable[[], None] | None = None
        #: called when an attempt's ladder ended: the wiring may forget it now
        self.on_ladder_done: Callable[[str], None] | None = None
        #: records that came after their attempt ended: evidence, never an outcome (DL-424)
        self.late_records: dict[str, list[ExitRecord]] = {}
        self._attempts: dict[str, _Attempt] = {}
        #: attempts a restart found closing whose group it could not prove gone: no instance,
        #: unsettled until `group_gone` (DL-426)
        self._adopted_held: set[str] = set()

    def key(self, attempt: str) -> tuple[AttemptTier, str]:
        """The executor's key of an attempt: this tier and the id."""
        return (self, attempt)

    def __repr__(self) -> str:
        return f"attempts@{id(self):x}"

    # -- reads

    def state(self, attempt: str) -> str:
        """SPAWNING, RUNNING, STOPPING or ENDED."""
        (leaf,) = self.machines.active(self.key(attempt)) & _ATTEMPT_STATES
        return leaf

    def outcome(self, attempt: str) -> AttemptOutcome | None:
        record: AttemptRecord = self.machines.data(self.key(attempt))
        return record.outcome

    def unsettled(self) -> tuple[str, ...]:
        """The attempts not yet ENDED, and the ENDED ones whose plan is not done, whose ladder
        still runs, or whose group is held (`held`), in dispatch order; then the held attempts
        a restart adopted."""
        live = tuple(a for a in self._attempts if self.is_unsettled(a))
        return live + tuple(sorted(self._adopted_held))

    def is_unsettled(self, attempt: str) -> bool:
        """Whether an attempt this tier instantiated is not ENDED, or its plan is not done, or
        its ladder still runs, or its group is held."""
        return (
            self.state(attempt) in UNSETTLED
            or not self._known(attempt).plan_done
            or self.ladder_running(attempt)
            or self.held(attempt)
        )

    def survived(self, attempt: str) -> bool:
        """Whether the attempt's group still had a live member after its ladder's KILL."""
        return self._known(attempt).survived

    def held(self, attempt: str) -> bool:
        """Whether the group of an attempt this tier instantiated may still live with no stop
        left to end it: it outlived its ladder's KILL, or its ladder raised. Such an attempt
        does not settle and is not forgotten until `group_gone` (DL-426). One a restart
        adopted held (`adopt_held`) has no instance; it is unsettled all the same."""
        known = self._attempts.get(attempt)
        return known is not None and (known.survived or known.failed)

    def group(self, attempt: str) -> tuple[int, str] | None:
        """The command group the attempt's `spawn.json` names, or None."""
        return command_group(self._known(attempt).spawn)

    def adopt_held(self, attempt: str) -> None:
        """A restart found the attempt closing and could not prove its group gone (`held`)."""
        self._adopted_held.add(attempt)

    def group_gone(self, attempt: str) -> None:
        """Nothing of the held attempt's command is left. An adopted one is released. A
        stopping one has its record timeout read once more, which ends it now that its
        command is dead; an ended one may settle (`on_ladder_done`). Either way `on_settled`
        is called, so a drain that waited for the hold ends once nothing else is unsettled."""
        if attempt in self._adopted_held:
            self._adopted_held.discard(attempt)
        else:
            known = self._known(attempt)
            known.survived = known.failed = False
            if self.state(attempt) == "STOPPING":
                self.record_timeout(
                    attempt, self.machines.generation(self.key(attempt), RECORD_TIMEOUT)
                )
        # the hold is lifted: a drain waiting for it may end now
        if self.on_settled is not None:
            self.on_settled()

    def ladder_running(self, attempt: str) -> bool:
        """Whether a ladder started for the attempt has not ended yet."""
        ladder = self._known(attempt).ladder
        return ladder is not None and not ladder.done()

    def knows(self, attempt: str) -> bool:
        """Whether `attempt` was dispatched here and not forgotten."""
        return attempt in self._attempts

    def plan_done(self, attempt: str) -> None:
        """The wiring says the attempt's follow-up plan is done, which settles an ENDED attempt.
        It calls this at once for a `none` plan and for a `rerun` plan whose dispatch waits for
        the restart. A `complete` or `hold` plan, and a `rerun` dispatched now, is done only
        after its last step."""
        self._known(attempt).plan_done = True
        if self.on_settled is not None:
            self.on_settled()

    # -- the observations

    def dispatch(self, attempt: str) -> str:
        """Start the instance of the attempt with run id `attempt` before anything spawns;
        answers the id."""
        self.machines.start(self.key(attempt), ATTEMPT, AttemptRecord())
        self._attempts[attempt] = _Attempt()
        self.machines.run()
        return attempt

    def spawn_recorded(self, attempt: str, spawn: Mapping[str, Any]) -> None:
        """`spawn.json` appeared. Post it before any record found in the same poll; a second
        sight of it posts nothing, because the chart takes it once."""
        known = self._known(attempt)
        if known.spawn is not None:
            return
        known.spawn = spawn
        self._post(attempt, Event(SPAWN_RECORDED, spawn))

    def status_recorded(self, attempt: str, status: Mapping[str, Any]) -> None:
        """`status.json` appeared: translate it and post `exit_recorded`. An exit or a signal
        counts as the stop's while the attempt is STOPPING. A record for an ended attempt is kept in
        `late_records` and changes nothing."""
        known = self._known(attempt)
        stopping = self.state(attempt) == "STOPPING"
        record = exit_record_from_status(status, stop_in_progress=stopping, spawn=known.spawn)
        if self.state(attempt) == "ENDED":
            self.late_records.setdefault(attempt, []).append(record)
        self._post(attempt, Event(EXIT_RECORDED, record))

    def spawn_failed(self, attempt: str, error: str | None) -> None:
        """The adapter could not start the wrapper or hand it the spec: nothing launched. Once
        `spawn.json` was seen the spawn did not fail, so the call posts nothing."""
        if self._known(attempt).spawn is not None:
            return
        self._post(attempt, Event(SPAWN_FAILED, error))

    def wrapper_exited(self, attempt: str, spawn: Mapping[str, Any] | None) -> None:
        """The wrapper exited and left no `status.json`. `spawn` is the `spawn.json` read now; one
        not yet posted is posted first. The verdict then posts `wrapper_gone`, or `stop` with
        cause `wrapper_lost` while the command lives."""
        known = self._known(attempt)
        if spawn is not None and known.spawn is None:
            self.spawn_recorded(attempt, spawn)
        verdict = missing_record_verdict(
            known.spawn, wrapper_alive=False, command_alive=self.command_alive
        )
        if verdict == "wrapper_lost":
            self.stop(attempt, WRAPPER_LOST)
        else:
            self._post(attempt, Event(WRAPPER_GONE))

    def stop(self, attempt: str, cause: str) -> None:
        """Stop one attempt: `update_stop`, `engine_stop` or `wrapper_lost`."""
        self._known(attempt)
        self._post(attempt, Event(STOP, cause))

    def stop_all(self, cause: str) -> None:
        """Stop every attempt not yet ENDED. None of their ladders is awaited."""
        for attempt in [a for a in self._attempts if self.state(a) in UNSETTLED]:
            self.stop(attempt, cause)

    def record_timeout(self, attempt: str, generation: int) -> None:
        """The timer `arm_timer` set fired. A stale generation is discarded by the chart. A timer
        of a forgotten attempt is dropped: its cancel raced its fire.

        A current timer of a stopping attempt reads the spool once more first. A `spawn.json` not
        yet seen is posted, so the ladder runs on its group under a new generation; a
        `status.json` is posted as the record. With neither, a command `command_alive` still finds
        alive is reported through `Processes.command_survived` and the attempt stays STOPPING,
        so it is never settled or replaced while it runs. Only a dead or unnamed command lets
        the timeout reach the chart."""
        known = self._attempts.get(attempt)
        if known is None:
            return
        key = self.key(attempt)
        if self.state(attempt) == "STOPPING" and generation == self.machines.generation(
            key, RECORD_TIMEOUT
        ):
            spawn, status = self.processes.read_spool(attempt)
            launched = spawn is not None and known.spawn is None
            if spawn is not None and launched:
                self.spawn_recorded(attempt, spawn)
            if status is not None:
                self.status_recorded(attempt, status)
            if launched or status is not None:
                return
            group = command_group(known.spawn)
            if group is not None and self.command_alive(*group):
                known.survived = True
                self.processes.command_survived(attempt)
                return
        self._post(attempt, Event(RECORD_TIMEOUT, generation=generation))

    def forget(self, attempt: str) -> None:
        """Drop an ended attempt whose wrapper has exited, so no later observation can come. An
        attempt whose plan is not done, whose ladder still runs, or whose group is held, is
        refused: it is still unsettled."""
        known = self._known(attempt)
        if not known.plan_done or self.ladder_running(attempt) or self.held(attempt):
            raise ValueError(f"attempt {attempt!r}: its plan or its ladder is not done")
        self.machines.forget(self.key(attempt))
        del self._attempts[attempt]

    async def join(self) -> None:
        """Wait for every ladder still running, and raise the first failure."""
        tasks = [a.ladder for a in self._attempts.values() if a.ladder is not None]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                raise result

    # -- inside

    def _known(self, attempt: str) -> _Attempt:
        try:
            return self._attempts[attempt]
        except KeyError:
            raise KeyError(f"no attempt {attempt!r} was dispatched") from None

    def _post(self, attempt: str, event: Event) -> None:
        """Post one event, then carry out the intents of its macrostep in order."""
        outcomes = self.machines.dispatch(self.key(attempt), event)
        self._perform(attempt, outcomes)

    def _perform(self, attempt: str, outcomes: list[Outcome]) -> None:
        for intent in (i for outcome in outcomes for i in outcome.intents):
            name = intent.name
            if name == STOP_ATTEMPT:
                self._start_stop(attempt)
            elif name == ARM_RECORD_TIMEOUT:
                pass  # the stop's ladder arms the timer at its KILL (_start_stop)
            elif name == RECORD_OUTCOME:
                # the record ends the attempt's observation, never its group's cleanup: a
                # started ladder runs on to its closing KILL (DL-426)
                self._what_is_left(attempt, intent.detail)
                self.processes.record_outcome(attempt, intent.detail)
            elif name == CANCEL_RECORD_TIMEOUT:
                self.processes.cancel_timer(attempt)
            else:
                raise ValueError(f"attempt {attempt!r}: no effect for the intent {name!r}")

    def _what_is_left(self, attempt: str, outcome: AttemptOutcome) -> None:
        """At the record, before the outcome is journaled, a reading of what is left of the
        command (`command_alive`). Nothing left, or a ladder still running (it takes the
        verdict at its end), changes nothing. Something left after a `natural` exit is a leak:
        under `process-leak=terminate` (`Processes.leaked`) the ladder runs on it. Something
        left after a stop is the stop's, whatever the switch: a stop whose ladder already ended
        takes the survivor verdict (`_survivor_verdict`), one that ran no ladder (its wrapper was
        killed before `spawn.json`) gets the ladder now. Either way the task holds the
        attempt's ladder slot, so the outcome is journaled `ladder: running` and the job's next
        run waits until a reading finds nothing left. Where only the leader can be told apart,
        `command_alive` is false once it exited, and nothing is done."""
        known = self._attempts[attempt]
        group = command_group(known.spawn)
        if group is None or self.ladder_running(attempt) or not self.command_alive(*group):
            return
        if outcome.kind == "natural" and not self.processes.leaked(attempt):
            return
        if known.ladder is None:
            generation = self.machines.generation(self.key(attempt), RECORD_TIMEOUT)
            task = self._ladder(attempt, *group, generation)
        else:
            task = self._survivor_verdict(attempt, *group)
        known.ladder = asyncio.get_running_loop().create_task(task)
        known.ladder.add_done_callback(lambda done: self._ladder_done(attempt, done))

    async def _survivor_verdict(self, attempt: str, pgid: int, token: str) -> None:
        """An ended attempt's survivor verdict, taken where a stopping one's record timeout
        takes it: anything of its command still left `grace_kill_s` after the KILL
        (`Processes.outlives`) has survived its stop (`Processes.command_survived`), and the
        attempt is held until a reading finds nothing left."""
        if await self.processes.outlives(pgid, token, self.grace_kill_s):
            self._attempts[attempt].survived = True
            self.processes.command_survived(attempt)

    def _start_stop(self, attempt: str) -> None:
        """The ladder on the recorded group, or, with no `spawn.json` yet, the wrapper's kill
        and the timer at once. A `spawn.json` that names no group gets no signal, only the
        timer. The timer's generation is read now, right after the macrostep that moved it."""
        generation = self.machines.generation(self.key(attempt), RECORD_TIMEOUT)
        known = self._attempts[attempt]
        if known.spawn is None:
            self.processes.kill_wrapper(attempt)
        else:
            group = command_group(known.spawn)
            if group is not None:
                known.ladder = asyncio.get_running_loop().create_task(
                    self._ladder(attempt, *group, generation)
                )
                known.ladder.add_done_callback(lambda task: self._ladder_done(attempt, task))
                return
        self.processes.arm_timer(attempt, generation, self.grace_kill_s)

    async def _ladder(self, attempt: str, pgid: int, token: str, generation: int) -> None:
        """Run the ladder to its end, then arm `record_timeout` at its KILL. A ladder that raises
        arms nothing: the stop was not carried out, so the attempt stays STOPPING and unsettled,
        and `_ladder_done` reports the failure. An attempt that ended while the ladder ran gets
        no timer and takes the survivor verdict (`_survivor_verdict`)."""
        await self.processes.ladder(pgid, token, self.grace_term_s)
        if self.state(attempt) != "ENDED":
            self.processes.arm_timer(attempt, generation, self.grace_kill_s)
        else:
            await self._survivor_verdict(attempt, pgid, token)

    def _ladder_done(self, attempt: str, task: asyncio.Task[None]) -> None:
        """Retrieve a finished ladder's failure and report it: a ladder that raised leaves its
        group unproven, so the attempt is held (`held`). The attempt may be settled now
        (`on_ladder_done`, `on_settled`). A cancelled ladder ended nothing: the engine is
        leaving, and the restart finishes it (DL-426)."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._attempts[attempt].failed = True
            self.processes.ladder_failed(attempt, error)
        if self.on_ladder_done is not None:
            self.on_ladder_done(attempt)
        if self.on_settled is not None:
            self.on_settled()


# -- the engine lifecycle tier


class EngineEffects(Protocol):
    """What the engine tier asks of the world. Every call returns at once."""

    def journal_draining(self) -> None: ...

    def cancel_watchers(self) -> None: ...

    def arm_abort_deadline(self, deadline: float) -> None:
        """Call `EngineTier.abort_deadline()` at `deadline`; replaces an earlier one."""

    def record_clean_shutdown(self) -> None: ...

    def record_unclean_shutdown(self, unsettled: tuple[str, ...]) -> None: ...

    def journal_shutdown_during_startup(self) -> None: ...

    def exit_process(self, code: int) -> None: ...


class EngineTier:
    """The one `ENGINE_LIFECYCLE` instance, on the attempt tier's executor.

    `all_attempts_settled` is posted only in DRAINING: once the drain's intents are done with
    no attempt unsettled, and when the wiring reports `plan_done` for the last unsettled
    attempt. A plan is done when the wiring says so, not when it is committed; in DRAINING it
    says so at once for a `rerun` plan, whose dispatch waits for the restart.
    """

    def __init__(self, effects: EngineEffects, attempts: AttemptTier) -> None:
        self.effects = effects
        self.attempts = attempts
        self.machines = attempts.machines
        attempts.on_settled = self._settle_check
        self._busy = False
        self._replayed = False
        self.machines.start(ENGINE_KEY, ENGINE_LIFECYCLE, Lifecycle())
        self.machines.run()

    @property
    def state(self) -> str:
        """STARTING, RUNNING, DRAINING or STOPPED."""
        (leaf,) = self.machines.active(ENGINE_KEY) & _ENGINE_STATES
        return leaf

    @property
    def deadline(self) -> float | None:
        life: Lifecycle = self.machines.data(ENGINE_KEY)
        return life.deadline

    def replay_done(self) -> None:
        """The replay is done. A second call posts nothing, because the chart takes it once."""
        if self._replayed:
            return
        self._replayed = True
        self._post(Event(REPLAY_DONE))

    def stop_requested(self, request: DrainRequest) -> None:
        """SIGTERM or `engine stop`. A second one while draining moves the deadline to now."""
        self._post(Event(STOP_REQUESTED, request))

    def abort_deadline(self) -> None:
        self._post(Event(ABORT_DEADLINE, self.attempts.unsettled()))

    # -- inside

    def _post(self, event: Event) -> None:
        self._busy = True
        try:
            outcomes = self.machines.dispatch(ENGINE_KEY, event)
            for intent in (i for outcome in outcomes for i in outcome.intents):
                self._perform(intent)
        finally:
            self._busy = False
        self._settle_check()

    def _perform(self, intent: Intent) -> None:
        name, detail = intent.name, intent.detail
        if name == JOURNAL_DRAINING:
            self.effects.journal_draining()
        elif name == STOP_ATTEMPTS:
            self.attempts.stop_all(detail)
        elif name == CANCEL_WATCHERS:
            self.effects.cancel_watchers()
        elif name == ARM_ABORT_DEADLINE:
            self.effects.arm_abort_deadline(detail)
        elif name == RECORD_CLEAN_SHUTDOWN:
            self.effects.record_clean_shutdown()
        elif name == RECORD_UNCLEAN_SHUTDOWN:
            self.effects.record_unclean_shutdown(detail)
        elif name == JOURNAL_SHUTDOWN_DURING_STARTUP:
            self.effects.journal_shutdown_during_startup()
        elif name == EXIT_PROCESS:
            self.effects.exit_process(detail)
        else:
            raise ValueError(f"engine: no effect for the intent {name!r}")

    def _settle_check(self) -> None:
        """Post `all_attempts_settled` in DRAINING once no attempt is unsettled, its plan done
        included; the chart excludes it in STARTING and RUNNING. An attempt that ends while this tier carries out a
        drain's intents is checked when they are done."""
        if self._busy or self.state != "DRAINING" or self.attempts.unsettled():
            return
        self._post(Event(ALL_ATTEMPTS_SETTLED))
