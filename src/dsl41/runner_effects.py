"""The effect outbox: intent recorded before the attempt, and what came of it.

Normative spec: docs/concurrency-model.md ss5 (effects), ss4 step 7 (the outbox commits
with the decision), ss1 (the outbox lives IN the ledger). `ssN` in this module always
names concurrency-model. The outbox is built on the spool, not beside it (S5c).

ss5 exists for one sentence in ss0: no `(job, run_number)` ever executes twice. An effect
that is merely "at-most-once per durable id" is unbound to any host: after an uncertain
SPAWN, a takeover can route the same id to a different relay, each host's dedup store
reports "first application", and two processes run. So every effect carries the
`executor_id` it is bound to, and a retry goes to THAT executor or becomes a new run with a
new effect.

**The outbox is built ON the spool** (DL-93). `spawn.json` says a spawn happened and with
what process identity, `status.json` says how it ended, and the ABSENCE of status.json is
the unobservable case (E7); ss5's `indeterminate` IS that absence. This module adds the
layer above:

- **Intent is recorded before the attempt.** A start the engine decided is written down
  before it spawns.
- **A kill is an effect with an id**, not a `task.cancel()` with none. A detached run whose
  job the oracle terminated, on an engine that died before cancelling, is re-driven by its
  recorded kill instead of running on orphaned (runner-design ss7 permits that one side
  effect at resume).
- **Four states, not two.** "Deduplicate and replay the original result" cannot be done
  when the engine persists a tombstone, acts, and crashes before persisting the result.
  The states are `pending` -> `applied` | `indeterminate` | `retired`. An effect whose
  outcome is unknown answers `OUTCOME_UNAVAILABLE`, never a plausible guess. `retired` is
  the recorded outcome of the supersession rule: "safe to forget" and "must not be
  forgotten" are different facts (ss5's DL-111 amendment).

Three rules about identity and resume, one of them a split this module leaves out:

- `run_id` is bound before the attempt (DL-118, period-model ss2.3, PR-36a). A SPAWN's
  `run_id` is minted inside the step-7 decision transaction and rides in the durable
  effect, so the WAL, the wrapper spec and the spool name ONE key; an engine that dies
  between the durable effect and the spawn resumes knowing the run's identity, which
  period-model ss11a's supervisor-index dedup keys on. The mint stays out of `effect_id`,
  which is derived: replay rebuilds the same outbox without trusting a uuid and reads the
  `run_id` back from the record.
- TERM and KILL are one effect, not two staged ids. ss5 splits them so a relay can tell a
  retried TERM from a retried KILL. The adapter's ladder (TERM, grace, KILL) never yields
  to the engine between its stages, so no engine-visible state exists for a second id to
  name; a re-driven kill re-runs the whole ladder, and TERM to a dead group is a no-op. The
  split lands with a relay that needs it (DL-96).
- A pending SPAWN is re-driven at resume (DL-102, the takeover barrier): a durable pending
  SPAWN is committed intent, and the barrier re-drives it through the gates a fresh effect
  passes. DL-41a's "provably-never-ran is never re-executed silently" governs the start with
  NO recorded intent: an admitted input whose decision never landed planned nothing durable,
  and resume FAILS that run loudly rather than re-deciding its effects.
"""

from __future__ import annotations

import re

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from dsl41.oracle_state import LIVE, TERMINAL, Event, JobRuntime
from dsl41.runner_clock import EngineError
from dsl41.state_machine import Context, Executor, Region, Row, State, Statechart, StatechartError
from dsl41.state_machine import Event as ChartEvent

#: ss5's effect alphabet. SHUTDOWN is not an effect: the shutdown path speaks to the
#: supervisor directly and leaves no intent for an outbox to carry; the supervisor allocates
#: the incarnation (DL-80) and the ledger the epoch (S6a) (ss5).
EffectKind = Literal["SPAWN", "KILL"]

#: what an effect of unknown outcome answers when asked for its result (ss5, CM-06); "we sent
#: it and then died" is a fact, and reporting success or failure would invent one
OUTCOME_UNAVAILABLE = "outcome_unavailable"


#: period-model ss11a pins `run_id` to a filename-safe grammar at the wire: the canonical uuid4
#: string form. Every writer and acceptor of a bound run_id checks it, because a freehand string
#: would pass presence checks and split the one key DL-118 keeps whole.
RUN_ID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def is_valid_run_id(run_id: str) -> bool:
    return RUN_ID_RE.fullmatch(run_id) is not None


class Effect(BaseModel):
    """One intended act on an execution host, recorded before it is
    attempted (ss5). FROZEN, like every other durable row here.

    `executor_id` is not decoration: ss5's at-most-once is GLOBAL, and an
    effect that does not name the host it is bound to is one a takeover can
    route somewhere else while the first host still holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str
    kind: EffectKind
    job: str
    run_number: int
    executor_id: str
    #: the admitted input that decided it; ss4 step 7 commits the outbox entry with that
    #: input's `ApplyResult`
    index: int
    at: datetime
    #: the run's process identity, minted IN the decision transaction for a SPAWN (period-model
    #: ss2.3, PR-36a) and carried by the KILL of a run this log spawned. None on a KILL for a
    #: run this root holds no binding for, and on a hand-built record; never from the planner
    #: for a SPAWN.
    run_id: str | None = None
    #: the executor host row's generation, read at birth (PR-16): an effect born before an
    #: eviction cannot pass for one born after it. None only on a hand-built record, which
    #: validates so `read_outbox`'s birth-identity gate can refuse it by name. STRICT, because
    #: lax pydantic would pass `false`, `"0"` and `0.0` through integer comparisons.
    generation: int | None = Field(default=None, strict=True)


#: what became of one attempt: ss5's three outcomes. An effect is `pending` until it has one,
#: and nothing leaves an outcome (ss5)
Outcome = Literal["applied", "indeterminate", "retired"]


class EffectOutcome(BaseModel):
    """What became of one attempt (ss5). `applied` carries what the host
    reported; `indeterminate` carries why nothing can be said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str
    state: Outcome
    #: the process identity the spool named, once it names one: the OBSERVED half. The intended
    #: half is on the Effect (DL-118); the two agree on the live path, and resume reads this one.
    run_id: str | None = None
    detail: str | None = None


def effect_id_for(index: int, kind: EffectKind, job: str, run_number: int) -> str:
    """The derived effect id: one input decides at most one effect of each kind per job, so
    replay rebuilds the same outbox without trusting a uuid."""
    return f"e{index}:{kind}:{job}.{run_number}"


# ---- one effect as a statechart (ss5)
#
# `EFFECT` is the declaration (docs/statechart-engine.md). The outbox holds one instance per open
# effect, keyed by `(outbox, effect_id)`, in the executor it runs on: the engine's, or its own
# when read offline. The outbox in the key keeps two outboxes on one executor apart. No instance
# exists before `record`: the outbox's checks run first (an exact replay is a no-op; a reused id
# with other content, or a run_id bound the other way, is an EngineError), then the instance
# starts in `pending`.
#
# `resolve` is the one event; its payload is the `EffectOutcome`, whose state is the partition
# the three rows' guards read. The rows have no actions: after the microstep the outbox writes
# the outcome and forgets the instance. The outcome record is the memory that `state_of`,
# `result_for` and `pending` read. The outbox refuses a second outcome before any post (DL-295).
# A pending SPAWN held by a host that routes nothing posts nothing and stays pending. The root's
# ignored row exists because the completeness check is syntactic (statechart-engine ss10); no
# tier path posts it. A gap in the chart is a `StatechartError`, which stops the engine with
# exit 7 (statechart-engine ss5).

#: the one event: what became of an attempt; its payload is an `EffectOutcome`
RESOLVE: Final = "resolve"
#: the states an effect rests in once it has an outcome
_FINALS: Final = frozenset(get_args(Outcome))


# -- the guards. Each docstring is its rows' prose.


def _applied(ctx: Context) -> bool:
    """The outcome's state is `applied`."""
    outcome: EffectOutcome = ctx.event.payload
    return outcome.state == "applied"


def _retired(ctx: Context) -> bool:
    """The outcome's state is `retired`."""
    outcome: EffectOutcome = ctx.event.payload
    return outcome.state == "retired"


def _indeterminate(ctx: Context) -> bool:
    """The outcome's state is `indeterminate`."""
    outcome: EffectOutcome = ctx.event.payload
    return outcome.state == "indeterminate"


def _over(ctx: Context) -> bool:
    """The effect rests in a final state. The outbox forgets the instance in the call that made
    it final and refuses a second outcome before any post (DL-295). In `pending` an outcome
    that no row's guard takes raises `UnhandledEvent`."""
    return bool(ctx.active & _FINALS)


#: One effect's lifecycle (ss5), one instance per open effect.
EFFECT: Final = Statechart(
    name="effect",
    root=State(
        "effect",
        regions=(
            Region(
                "outcome",
                "pending",
                (
                    State("pending"),
                    State("applied", final=True),
                    State("retired", final=True),
                    State("indeterminate", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "effect.02",
            "pending",
            RESOLVE,
            "applied",
            guard="applied",
            cite="concurrency-model ss5, DL-96, period-model ss3.5",
            note=(
                "Dispatched; or resume found the run on the host, or a kill landed; or a"
                " replayed effect_result; or a carried execution."
            ),
        ),
        Row(
            "effect.03",
            "pending",
            RESOLVE,
            "retired",
            guard="retired",
            cite="concurrency-model ss5, DL-111, DL-232",
            note="Superseded at dispatch; or resume finds that the run it would kill has exited.",
        ),
        Row(
            "effect.04",
            "pending",
            RESOLVE,
            "indeterminate",
            guard="indeterminate",
            cite="concurrency-model ss5, CM-06, DL-111",
            note=(
                "Resume finds no status and no live wrapper for a kill. An exact retry"
                " answers outcome_unavailable."
            ),
        ),
        Row(
            "effect.05",
            "effect",
            RESOLVE,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable=(
                "rows .02 to .04 partition every outcome in `pending`, so the walk"
                " reaches the root only from a final state"
            ),
            note=(
                "An outcome is final: the outbox refuses a second one with an EngineError"
                " before any post, and forgets the instance in the call that made it final"
                " (DL-295). No tier path posts this row."
            ),
        ),
    ),
    guards={
        "applied": _applied,
        "retired": _retired,
        "indeterminate": _indeterminate,
        "over": _over,
    },
    clock="logical",
)


def _outcome_row(state: Outcome) -> str:
    """The id of the row that takes an outcome of `state` in `pending`.

    The rows partition every `Outcome`, so a miss means the chart lost a row: a
    `StatechartError`, which stops the engine with exit 7 (statechart-engine ss5)."""
    for row in EFFECT.rows:
        if row.source == "pending" and row.guard == state:
            return row.id
    raise StatechartError(f"effect: no row of pending takes the {state} outcome")


class Outbox:
    """The effects one run root has intended, and their states (ss5).

    ss1 requires the log, the decision index and the outbox to commit in ONE transaction, so
    the WAL carries both records and replay rebuilds this from them. Nothing is stored that
    the spool already knows.

    Each open effect is an `EFFECT` instance in `machines`, keyed by `key(effect_id)`. An
    engine's outbox runs on the engine's executor (statechart-engine ss11); an outbox read
    offline makes its own. Two outboxes on one executor never share a key."""

    def __init__(self, machines: Executor | None = None) -> None:
        self.machines = machines if machines is not None else Executor()
        self._effects: dict[str, Effect] = {}
        self._outcomes: dict[str, EffectOutcome] = {}
        #: insertion order is admission order; ss5 makes per-run ordering mandatory, so a KILL
        #: decided after a SPAWN never overtakes it
        self._order: list[str] = []
        #: the period-model ss11a one-to-one ownership maps, both directions
        self._run_ids: dict[tuple[str, int], str] = {}
        self._runs_by_id: dict[str, tuple[str, int]] = {}
        #: the run ids that SPAWN effects bound, which a KILL is planned from
        self._spawn_run_ids: dict[tuple[str, int], str] = {}

    def record(self, effect: Effect) -> None:
        """Note an intended effect; idempotent on `effect_id`, since replay meets the live record.

        Ownership is one-to-one in BOTH directions (period-model ss11a): one `(job, run_number)`
        maps to one `run_id` and back. The planner keeps that by construction, so a record that
        breaks it (a KILL naming a different id than its run's SPAWN, or one id claimed by two
        runs) is corruption or a foreign writer and raises EngineError."""
        existing = self._effects.get(effect.effect_id)
        if existing is not None:
            if existing != effect:
                # `effect_id` is derived, so two different effects under one id mean the log
                # disagrees with itself; overwriting would silently replace the earlier intent
                raise EngineError(
                    f"effect {effect.effect_id}: recorded twice with different"
                    " content -- the log disagrees with itself (DL-118)"
                )
            return  # exact replay of the same record: a no-op
        run = (effect.job, effect.run_number)
        if effect.run_id is None:
            bound = self._run_ids.get(run)
            if bound is not None:
                # the planner looks a KILL's id up from the SPAWN that bound it, so a new
                # identity-less intent for an identified run was not planned here (DL-118)
                raise EngineError(
                    f"effect {effect.effect_id}: carries no run_id but"
                    f" {effect.job}.{effect.run_number} is bound to {bound!r} -- an"
                    " identity-less intent for an identified run (DL-118)"
                )
        if effect.run_id is not None:
            bound = self._run_ids.get(run)
            if bound is not None and bound != effect.run_id:
                raise EngineError(
                    f"effect {effect.effect_id}: names run_id {effect.run_id!r} but"
                    f" {effect.job}.{effect.run_number} is bound to {bound!r} -- one run,"
                    " one identity (DL-118)"
                )
            owner = self._runs_by_id.get(effect.run_id)
            if owner is not None and owner != run:
                raise EngineError(
                    f"effect {effect.effect_id}: run_id {effect.run_id!r} already names"
                    f" run {owner[0]}.{owner[1]} -- one identity, one run (DL-118)"
                )
            self._run_ids[run] = effect.run_id
            self._runs_by_id[effect.run_id] = run
            if effect.kind == "SPAWN":
                self._spawn_run_ids[run] = effect.run_id
        self._order.append(effect.effect_id)
        self._effects[effect.effect_id] = effect
        self.machines.start(self.key(effect.effect_id), EFFECT)
        self.machines.run()

    def resolve(self, outcome: EffectOutcome) -> None:
        """Record what became of one attempt.

        An outcome for an effect this outbox never saw, or naming a different run_id than its
        effect bound, is refused (DL-118). An outcome is final (ss5): a second one is refused
        before anything is written and before any post (DL-295). A live writer resolves only
        pending effects; on replay a log holding two outcomes for one effect stops resume
        (period-model ss11). Otherwise the effect's instance takes the outcome, the outbox
        writes it, and the final instance is forgotten."""
        effect = self._effects.get(outcome.effect_id)
        if effect is None:
            raise EngineError(
                f"outcome for unknown effect {outcome.effect_id!r}: the log lost the"
                " record that said what was meant"
            )
        if (
            effect.run_id is not None
            and outcome.run_id is not None
            and outcome.run_id != effect.run_id
        ):
            raise EngineError(
                f"outcome for {outcome.effect_id} names run_id {outcome.run_id!r} but the"
                f" effect bound {effect.run_id!r} -- a stranger's fate refused (DL-118)"
            )
        first = self._outcomes.get(outcome.effect_id)
        if first is not None:
            row = _outcome_row(outcome.state)
            raise EngineError(
                f"outcome {outcome.state} for {outcome.effect_id}: the effect is already"
                f" {first.state}, and an outcome is final ({row}: {first.state} is not a"
                f" source of {row}) (concurrency-model ss5)"
            )
        self.machines.dispatch(self.key(outcome.effect_id), ChartEvent(RESOLVE, outcome))
        self._outcomes[outcome.effect_id] = outcome
        self.machines.forget(self.key(outcome.effect_id))

    def state_of(self, effect_id: str) -> str | None:
        """`pending` | `applied` | `indeterminate` | `retired`, or None for an unseen effect."""
        outcome = self._outcomes.get(effect_id)
        if outcome is not None:
            return outcome.state
        if effect_id not in self._effects:
            return None
        (leaf,) = self.machines.active(self.key(effect_id)) - {EFFECT.root.name}
        return leaf

    def key(self, effect_id: str) -> tuple[Outbox, str]:
        """The executor's key of an effect's instance: this outbox and the id."""
        return (self, effect_id)

    def __repr__(self) -> str:
        """What a statechart error's report shows for the outbox in a key."""
        return "outbox"

    def result_for(self, effect_id: str) -> EffectOutcome | str | None:
        """The outcome of an effect, `OUTCOME_UNAVAILABLE` if it was attempted and nothing can
        be said, or None if it has not been attempted.

        ss5: an exact retry returns the original result only when it is KNOWN; `indeterminate`
        is never collapsed into a failure."""
        outcome = self._outcomes.get(effect_id)
        if outcome is None:
            return None
        return OUTCOME_UNAVAILABLE if outcome.state == "indeterminate" else outcome

    def pending(self) -> list[Effect]:
        """Every unattempted effect, in admission order (ss5's per-run ordering)."""
        return [self._effects[eid] for eid in self._order if eid not in self._outcomes]

    def spawn_run_ids(self) -> Mapping[tuple[str, int], str]:
        """The `(job, run_number) -> run_id` bindings the recorded SPAWN effects made, as a
        read-only view of the live map."""
        return MappingProxyType(self._spawn_run_ids)

    def effects(self) -> Iterator[Effect]:
        return (self._effects[eid] for eid in self._order)

    def pending_for(self, job: str, kind: EffectKind | None = None) -> list[Effect]:
        return [e for e in self.pending() if e.job == job and (kind is None or e.kind == kind)]


def plan_effects(
    emitted: Iterable[Event],
    *,
    index: int,
    executor_id: str,
    generation: int,
    runs: Mapping[str, int],
    dispatched: Mapping[str, int],
    live: Mapping[str, int],
    dispatchable: frozenset[str],
    run_ids: Mapping[tuple[str, int], str],
    mint_run_id: Callable[[], str],
) -> list[Effect]:
    """The effects one applied input implies (ss4 step 7).

    Pure in what the oracle emitted and what the shell holds, except that a SPAWN's `run_id`
    comes from `mint_run_id`: identity is created here, in the decision transaction (PR-36a).
    Replay never re-plans a decided attempt; it reads the outbox back from the records
    (`Replay`). The one plan made at resume is for an attempt whose decision was never written
    (`Engine.plan_recovered`, DL-315); its mint is the only one. A KILL carries the run's id
    from `run_ids`, or None for a run this root holds no binding for. `generation` is the
    executor host row's CURRENT value (PR-16). The function decides INTENT only; whether an
    intent is still desired is `superseded_reason`'s question, asked at dispatch.

    Two filters keep un-appliable intent out of the log. The ghost-run gate
    (`runs[job] > dispatched[job]`) decides whether an EFFECT exists: a CHANGE_STATUS-parity
    STARTING overwrite re-emits the status without advancing the run, and vendor parity
    launches nothing. A terminal status for a job with no live run plans no kill.

    One call plans at most one effect per `(kind, job, run_number)` and keeps the first
    (DL-232): two terminal transitions of one live run in one batch, such as a deadline firing
    inside a `CHANGE_STATUS SUCCESS` batch, would otherwise plan two KILLs under one id."""
    effects: list[Effect] = []
    planned: set[tuple[EffectKind, str, int]] = set()
    for ev in emitted:
        if ev.kind != "STATUS":
            continue  # alarms are journal + UI surface only (runner-design ss4)
        job = ev.job()
        if job is None or job not in dispatchable:
            continue  # boxes, pseudo-entries, unregistered job types: no dispatch row
        status = ev.payload.get("status")
        kind: EffectKind | None = None
        run_number = 0
        if status == "STARTING" and runs.get(job, 0) > dispatched.get(job, 0):
            kind, run_number = "SPAWN", runs.get(job, 0)
        elif status in TERMINAL and job in live:
            kind, run_number = "KILL", live[job]
        if kind is None or (kind, job, run_number) in planned:
            continue
        planned.add((kind, job, run_number))
        effects.append(
            Effect(
                effect_id=effect_id_for(index, kind, job, run_number),
                kind=kind,
                job=job,
                run_number=run_number,
                executor_id=executor_id,
                index=index,
                at=ev.at,
                run_id=mint_run_id() if kind == "SPAWN" else run_ids.get((job, run_number)),
                generation=generation,
            )
        )
    return effects


def superseded_reason(effect: Effect, row: JobRuntime | None, live_run: int | None) -> str | None:
    """Why this effect must NOT be applied now, or None (ss5).

    The test is the exact desired state by kind, not a run-number comparison: KILLJOB does not
    advance `run_number`, so a delayed SPAWN for run N is still "current" after run N was
    TERMINATED. It is read at DISPATCH time, never at planning time."""
    if row is None:
        return f"{effect.job} has no runtime row: nothing to act on"
    if effect.kind == "SPAWN":
        if row.status in TERMINAL:
            return f"{effect.job} is already {row.status}: the run this spawn was for has ended"
        if row.status not in LIVE:
            # ss5's "still desired running": an injected INACTIVE sets the run aside without
            # ending it (DL-232)
            return (
                f"{effect.job} is {row.status}: the run this spawn was for is no longer"
                " desired running"
            )
        if row.run_number != effect.run_number:
            return (
                f"{effect.job} is at run {row.run_number}, not the {effect.run_number}"
                " this spawn was decided for"
            )
        return None
    if live_run is None:
        return f"{effect.job} has no live run to kill"
    if live_run != effect.run_number:
        return (
            f"{effect.job}'s live run is {live_run}, not the {effect.run_number}"
            " this kill named: killing it would stop a run nobody asked to stop"
        )
    return None
