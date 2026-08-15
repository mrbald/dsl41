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
- **Three states, not two.** "Deduplicate and replay the original result"
  is unimplementable as stated: persist tombstone, act, crash before
  persisting the result, and nothing can know whether the signal landed.
  `pending` -> `applied` | `indeterminate`, and an effect whose outcome is
  unknown answers `OUTCOME_UNAVAILABLE` rather than a plausible guess.

What is deliberately NOT here, and why:

- **`run_id` is not bound before the attempt.** ss5 binds it atomically for
  the reason a RELAY cannot see a run directory: it has only ids. Locally
  `(job, run_number)` IS the identity -- `runs/<job>.<run_number>` is
  created with `mkdir()` and no `exist_ok`, so a second spawn of one run
  fails loudly rather than doubling -- so the outbox records the process
  identity the spool reports, when it reports it. Binding it earlier is
  S5d's, where a relay exists to need it.
- **TERM and KILL are one effect, not two staged ids.** ss5 splits them so a
  relay can tell a retried TERM from a retried KILL. The adapter's ladder
  (TERM, grace, KILL) never yields to the engine between its stages, so
  there is no engine-visible state between them for a second id to name; a
  re-driven kill re-runs the whole ladder, and TERM to a dead group is a
  no-op. The split lands with the relay that needs it.
- **A pending SPAWN is not re-driven at resume.** runner-design ss7 fails a
  start with no spool trace rather than re-running it ("provably-never-ran
  is still never re-executed silently"), and that is a semantic decision
  DL-41a took deliberately. The outbox makes re-driving EXPRESSIBLE; whether
  a takeover should re-drive rather than fail is ss7's barrier question and
  belongs where leader election gives it a context.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dsl41.oracle_state import TERMINAL, Event, JobRuntime

#: ss5's effect alphabet at this stage. SHUTDOWN is not here: it binds to a
#: supervisor incarnation and a scheduler epoch, and neither is allocated
#: until S6.
EffectKind = Literal["SPAWN", "KILL"]

#: What an effect whose outcome is unknown answers when asked for its result
#: (ss5, CM-06). Deliberately not a plausible guess: the whole reason ss5 has
#: three states is that "we sent it and then died" is a fact, and reporting it
#: as either success or failure invents one.
OUTCOME_UNAVAILABLE = "outcome_unavailable"


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


class EffectOutcome(BaseModel):
    """What became of one attempt (ss5). `applied` carries what the host
    reported; `indeterminate` carries why nothing can be said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effect_id: str
    state: Literal["applied", "indeterminate", "retired"]
    #: the process identity the spool named, once it names one. Recorded here
    #: rather than bound before the attempt: see the module docstring.
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

    def record(self, effect: Effect) -> None:
        """Note an intended effect. Idempotent on `effect_id`, because replay
        meets the same record the live engine wrote."""
        if effect.effect_id not in self._effects:
            self._order.append(effect.effect_id)
        self._effects[effect.effect_id] = effect

    def resolve(self, outcome: EffectOutcome) -> None:
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
    runs: Mapping[str, int],
    dispatched: Mapping[str, int],
    live: Mapping[str, int],
    dispatchable: frozenset[str],
) -> list[Effect]:
    """The effects one applied input implies (ss4 step 7).

    A pure function of what the oracle emitted plus what the shell is
    holding, so the same input plans the same effects on replay as it did
    live. It decides INTENT only -- whether an intent is still desired when
    its turn comes is `superseded_reason`'s question, asked at dispatch,
    because the world moves between the two.

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
