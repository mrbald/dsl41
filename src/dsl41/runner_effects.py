"""The effect outbox: intent recorded before the attempt, and what came of it.

Normative spec: docs/concurrency-model.md ss5 (effects), ss4 step 7 (the
outbox commits with the decision), ss1 (the outbox lives IN the ledger).
Stage S5c. `ssN` in this module always names concurrency-model.

ss5 exists for one sentence in ss0: no `(job, run_number)` ever executes
twice. An effect that is merely "at-most-once per durable id" is unbound to
any host -- after an uncertain SPAWN, a takeover can route the same id to a
different relay, each host's dedup store correctly reports "first
application", and two processes run. So every effect here carries the
`executor_id` it is bound to, and a retry goes to THAT executor or becomes a
new run with a new effect.

**Built ON the spool, not beside it** (DL-93). The lifecycle tier already
records durably what a naive outbox would re-invent: `spawn.json` says a
spawn happened and with what process identity, `status.json` says how it
ended, and the ABSENCE of status.json is already the unobservable case (E7).
ss5's `indeterminate` IS that absence. What the engine lacked, and what this
module adds, is the layer above:

- **Intent recorded before the attempt.** Today an engine that decides a
  start and dies before spawning leaves only the oracle's own STARTING
  behind; the shell's intention to act on it was never written down.
- **A kill that is an effect with an id**, rather than a `task.cancel()`
  with none. This closes a real leak: a detached run whose job the oracle
  terminated, on an engine that died before cancelling, was skipped by
  reconciliation (its job is already TERMINAL) and ran on orphaned. A
  recorded kill is re-driven instead -- which is exactly the one side
  effect runner-design ss7 already permits at resume.
- **Four states, not two.** "Deduplicate and replay the original result"
  is unimplementable as stated: persist tombstone, act, crash before
  persisting the result, and nothing can know whether the signal landed.
  `pending` -> `applied` | `indeterminate` | `retired`, and an effect whose
  outcome is unknown answers `OUTCOME_UNAVAILABLE` rather than a plausible
  guess. `retired` is the recorded outcome of the supersession rule below,
  not an omission: "safe to forget" and "must not be forgotten" are
  different facts (ss5's DL-111 amendment).

What is deliberately NOT here, and why:

- ~~`run_id` is not bound before the attempt~~ -- **it is now** (DL-118,
  period-model ss2.3, PR-36a). A SPAWN's `run_id` is minted inside the
  step-7 decision transaction and rides in the durable effect, so the WAL,
  the wrapper spec and the spool name ONE key: an engine that dies between
  the durable effect and the spawn resumes knowing which identity the run
  would have had, which is what ss11a's supervisor-index dedup keys on.
  DL-96 deferred the binding "until the relay needs it"; the seal needed it
  first. The mint stays out of `effect_id`, which remains derived -- replay
  reconstructs the same outbox without trusting a uuid, and reads the
  `run_id` back from the record instead of re-minting it.
- **TERM and KILL are one effect, not two staged ids.** ss5 splits them so a
  relay can tell a retried TERM from a retried KILL. The adapter's ladder
  (TERM, grace, KILL) never yields to the engine between its stages, so
  there is no engine-visible state between them for a second id to name; a
  re-driven kill re-runs the whole ladder, and TERM to a dead group is a
  no-op. The split lands with the relay that needs it.
- ~~A pending SPAWN is not re-driven at resume~~ -- **it is now** (DL-102,
  the takeover barrier). A durable pending SPAWN is committed intent, and
  the barrier re-drives it through the same gates a fresh effect passes.
  What DL-41a's "provably-never-ran is never re-executed silently" still
  governs is the start with NO recorded intent: an admitted input whose
  decision never landed planned nothing durable, and resume FAILS that run
  loudly rather than re-deciding its effects after the fact.
"""

from __future__ import annotations

import re

from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from dsl41.oracle_state import TERMINAL, Event, JobRuntime
from dsl41.runner_clock import EngineError

#: ss5's effect alphabet. SHUTDOWN is not here, and what defers it is no
#: longer a missing identity: the incarnation is allocated by the supervisor
#: at start (DL-80) and the epoch by S6a. It is that nothing needs it -- the
#: shutdown path speaks to the supervisor directly and leaves no intent for
#: an outbox to carry (ss5).
EffectKind = Literal["SPAWN", "KILL"]

#: What an effect whose outcome is unknown answers when asked for its result
#: (ss5, CM-06). Deliberately not a plausible guess: the whole reason ss5 has
#: three states is that "we sent it and then died" is a fact, and reporting it
#: as either success or failure invents one.
OUTCOME_UNAVAILABLE = "outcome_unavailable"


#: period-model ss11a pins `run_id` to a filename-safe grammar at the wire:
#: the canonical uuid4 string form the adapter has always minted. Everything
#: that WRITES or ACCEPTS a bound run_id checks it -- an empty or freehand
#: string would survive presence checks and then lose to `or`-style
#: fallbacks downstream, splitting the one key DL-118 exists to keep whole.
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
    #: the admitted input that decided it -- ss4 step 7 commits the outbox
    #: entry with that input's `ApplyResult`, so this is how a reader ties an
    #: effect back to the decision that wanted it
    index: int
    at: datetime
    #: the run's process identity, minted IN the decision transaction for a
    #: SPAWN (period-model ss2.3, PR-36a) and carried by the KILL of a run
    #: this log spawned. None on a KILL for a run this root holds no binding
    #: for, and on a hand-built record before the reader's gate sees it --
    #: never from this writer's planner for a SPAWN.
    run_id: str | None = None
    #: the executor host row's generation, read at birth (PR-16): an effect
    #: born before an eviction cannot pass for one born after it. None only
    #: on a hand-built record, which validates so `read_outbox`'s
    #: birth-identity gate can refuse it by name; nothing this engine plans
    #: leaves it None. STRICT, because lax pydantic would wave `false`,
    #: `"0"` and `0.0` through every integer comparison below it.
    generation: int | None = Field(default=None, strict=True)


class EffectOutcome(BaseModel):
    """What became of one attempt (ss5). `applied` carries what the host
    reported; `indeterminate` carries why nothing can be said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str
    state: Literal["applied", "indeterminate", "retired"]
    #: the process identity the spool named, once it names one -- the
    #: OBSERVED half. The intended half lives on the Effect since DL-118
    #: (minted at birth); the two agree by construction on the live path and
    #: this field remains what resume paths can actually read back.
    run_id: str | None = None
    detail: str | None = None


def effect_id_for(index: int, kind: EffectKind, job: str, run_number: int) -> str:
    """Derived, not minted. One admitted input decides at most one effect of
    each kind per job, so the input's index plus that triple is already
    unique -- and a derived id means replay reconstructs the same outbox
    without a uuid whose value the log would have to be trusted for."""
    return f"e{index}:{kind}:{job}.{run_number}"


class Outbox:
    """The effects one run root has intended, and their states (ss5).

    In memory here and in the ledger on disk: ss1 requires the log, the
    decision index and the outbox to commit in ONE transaction, so the WAL
    carries both records and replay rebuilds this from them. Nothing is
    stored that the spool already knows."""

    def __init__(self) -> None:
        self._effects: dict[str, Effect] = {}
        self._outcomes: dict[str, EffectOutcome] = {}
        #: insertion order IS admission order, and ss5 makes per-run ordering
        #: mandatory: a KILL decided after a SPAWN must not overtake it.
        self._order: list[str] = []
        #: the ss11a one-to-one ownership maps, both directions
        self._run_ids: dict[tuple[str, int], str] = {}
        self._runs_by_id: dict[str, tuple[str, int]] = {}

    def record(self, effect: Effect) -> None:
        """Note an intended effect. Idempotent on `effect_id`, because replay
        meets the same record the live engine wrote.

        Ownership is one-to-one in BOTH directions (period-model ss11a): one
        `(job, run_number)` maps to one `run_id` and one `run_id` to one
        `(job, run_number)`. The planner keeps that by construction -- a KILL
        looks its run's id up from the SPAWN that bound it -- so a record
        that breaks it (a KILL naming a different id than its run's SPAWN, or
        one id claimed by two runs) is corruption or a foreign writer, and
        acting on either half would act on the wrong process."""
        existing = self._effects.get(effect.effect_id)
        if existing is not None:
            if existing != effect:
                # `effect_id` is DERIVED, so two different effects under one
                # id mean the log disagrees with itself -- and overwriting
                # would let the later record silently replace the intent the
                # earlier one recorded
                raise EngineError(
                    f"effect {effect.effect_id}: recorded twice with different"
                    " content -- the log disagrees with itself (DL-118)"
                )
            return  # exact replay of the same record: a no-op
        run = (effect.job, effect.run_number)
        if effect.run_id is None:
            bound = self._run_ids.get(run)
            if bound is not None:
                # the planner looks a KILL's id up from the SPAWN that bound
                # it, so a NEW identity-less intent for an identified run was
                # not planned by this code (DL-118)
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
        self._order.append(effect.effect_id)
        self._effects[effect.effect_id] = effect

    def resolve(self, outcome: EffectOutcome) -> None:
        """Record what became of one attempt. STRICT on association: an
        outcome for an effect this outbox never saw means the log lost the
        record that said what was meant, and an outcome naming a different
        run_id than its effect bound is a stranger's fate filed under this
        run's intent -- both refuse (DL-118)."""
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
        self._outcomes[outcome.effect_id] = outcome

    def state_of(self, effect_id: str) -> str | None:
        """`pending` | `applied` | `indeterminate` | `retired`, or None for an
        effect this outbox never saw."""
        if effect_id not in self._effects:
            return None
        outcome = self._outcomes.get(effect_id)
        return "pending" if outcome is None else outcome.state

    def result_for(self, effect_id: str) -> EffectOutcome | str | None:
        """The outcome of an effect, `OUTCOME_UNAVAILABLE` if it was attempted
        and nothing can be said, or None if it has not been attempted.

        ss5: an exact retry returns the original result only when that result
        is KNOWN. The three-way answer is the whole point -- collapsing
        `indeterminate` into a failure would let a signal that DID land be
        reported as one that did not."""
        outcome = self._outcomes.get(effect_id)
        if outcome is None:
            return None
        return OUTCOME_UNAVAILABLE if outcome.state == "indeterminate" else outcome

    def pending(self) -> list[Effect]:
        """Every unattempted effect, in admission order (ss5's per-run
        ordering, which a global order satisfies for free)."""
        return [self._effects[eid] for eid in self._order if eid not in self._outcomes]

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

    A pure function of what the oracle emitted plus what the shell is
    holding -- except the one deliberate impurity: a SPAWN's `run_id` comes
    from `mint_run_id`, because identity is CREATED here, in the decision
    transaction (PR-36a), and nowhere later. Replay is not exposed to the
    mint: it never re-plans, it reads the outbox back from the records
    (`Replay`), so the id a resumed engine acts on is the id the log holds.
    A KILL carries the run's id from `run_ids` -- the (job, run_number) ->
    run_id bindings this run root already made -- or None for a run this
    root holds no binding for. `generation` is the executor
    host row's CURRENT value (PR-16), one value because one call plans for
    one executor. The function still decides INTENT only -- whether an
    intent is still desired when its turn comes is `superseded_reason`'s
    question, asked at dispatch, because the world moves between the two.

    Two filters keep un-appliable intent out of the log entirely. The
    ghost-run gate (`runs[job] > dispatched[job]`) is where it has always
    been in meaning -- a CHANGE_STATUS-parity STARTING overwrite re-emits the
    status without advancing the run, and vendor parity launches nothing --
    but it now decides whether an EFFECT exists rather than whether a task
    is created, which is the honest place for it: the shell never intended
    to act. And a terminal status for a job with no live run needs no kill;
    planning one would write an effect that could only ever be superseded."""
    effects: list[Effect] = []
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
        if kind is None:
            continue
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

    ss5 is emphatic that the obvious guard does not work: comparing run
    GENERATIONS never fires for the case that motivates it, because KILLJOB
    does not advance `run_number` -- a delayed SPAWN for run N is still
    "current" after run N has been TERMINATED. So the test is the exact
    desired state, and it differs by kind.

    Read at DISPATCH time, never at planning time. Between the two the
    oracle may have killed the run this SPAWN was for, and applying it then
    would start a process nothing is waiting for."""
    if row is None:
        return f"{effect.job} has no runtime row: nothing to act on"
    if effect.kind == "SPAWN":
        if row.status in TERMINAL:
            return f"{effect.job} is already {row.status}: the run this spawn was for has ended"
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
