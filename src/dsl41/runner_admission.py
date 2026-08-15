"""Admission: the frozen order every input takes, and the record it leaves.

Normative spec: docs/concurrency-model.md ss4 (admission and application),
ss2 (identity), ss6 (the envelope). Stage S2. Bare `ssN` elsewhere in the
runner means docs/runner-design.md; every section reference in this module
names its document, because both are normative here and they are not the
same document.

**One admission rule for every input** (concurrency-model ss4). Scheduler
ticks, adapter completions, reconciliation injections and standalone time
observations take the same path as operator commands:

    dedup -> stamp -> append the batch -> apply the time half
          -> decide -> feed or reject -> record the result

The order is the whole content. Three of its steps are counter-intuitive
and each is here because the obvious arrangement is wrong:

- **Dedup precedes admission.** A retry that already has a decision is
  answered from the index and consumes nothing: no index, no leader
  timestamp, no time observation. Admitting it first and deduplicating
  after would let a client's retry storm walk the clock forward (CM-05).
- **The time half applies before the decision.** The batch is
  `TimeAdvanced(at)` + the attempt, and the timers due at or before `at`
  fire on the time half -- so a term_run_time kill lands BEFORE the gate
  reads the status it gates on. Deciding first would let a kill fire
  between the decision and the feed and defeat the precondition that had
  just passed (CM-04). `Oracle.batch` is the single transaction that holds
  both halves, so the pair still moves each entity one revision (ss3).
- **The time half applies even when the attempt is rejected.** A rejected
  completion still carries a real observation of the clock, and the kill it
  let fire is a decision the estate has already acted on. Dropping the
  observation with the attempt would resurrect a killed job on the next
  replay -- which is precisely why ss4 makes the batch two records and not
  one field on one record.

**The decision index** (`request_id` -> `ApplyResult`) is what makes a
retry idempotent and what makes replay two-pass. `ApplyResult` is appended
AFTER the attempt, so a reader that met an attempt could not tell whether
it was applied, rejected, or interrupted between the two. Pass one builds
the index; pass two applies. An attempt with **no** result is applied,
because admission is the commit point -- and it goes through the gate on
the way, since its decision is exactly what did not survive.

A DURABLE decision, by contrast, is authoritative and is never recomputed:
a build whose gate has changed must reproduce the log's own history, not
the history it would write today.

**Preconditions are mandatory** (stage S3, concurrency-model ss0). An
externally requested mutation names the revision its author read, or it is
refused -- there is no opt-out and no `"any"`. `parse_envelope` below is
where that refusal lives, and it is deliberately ONE function rather than a
rule each transport re-implements: the ss10 socket is the only external
transport today, and the relay S5 adds must reach the same verdict.

The two ways an input can fail are not the same fact and are not recorded
the same way:

- **Refused** -- steps 1-2. Bad framing, an absent or malformed `expect`, a
  `baseline_id` from another run, a reused `request_id`, a stale `epoch`.
  Nothing is appended, no index is consumed, no clock moves. The request
  never entered the system, so the log says nothing about it.
- **Rejected** -- step 6. The envelope was good and the precondition was
  not: the entity moved between the caller's read and this input. That IS
  an event in the estate's history -- it consumed an index and its time
  half fired timers -- so it is recorded as a decision.

S5a adds a second shape of verbless attempt beside the time observation: a
routing-table command (`docs/concurrency-model.md` ss8). It is admitted by
this same order and gated in this same place, and it is applied to the ss3
owner rather than fed, because the oracle must never read a host row.

Not here yet: the outbox and the `effect_id`-to-`executor_id` binding
(S5c), and epoch allocation (S6). `epoch` is carried at 0 because ss6 ships
it inert rather than break the wire twice; the check for it is in its ss4 place,
AFTER dedup, so an exact old-epoch retry recovers its original result while
an unseen old-epoch request is refused.
"""

from __future__ import annotations

import hashlib
import json

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, RuntimeState, TERMINAL
from dsl41.runner_clock import EngineError
from dsl41.runner_hosts import HostCommand, apply_host_command, host_rejection_reason

#: The wire version of the ss6 envelope. There is no v1 to fall back to:
#: ss0 refuses a caller that does not name a version, and accepting an
#: unversioned request "for compatibility" is the opt-out ss0 forbids.
PROTOCOL_VERSION = 2

#: Sources whose events are ENGINE-MADE completions and therefore pass the
#: stale gate. Externally injected STATUS keeps sendevent CHANGE_STATUS
#: parity and is never gated -- it may legally overwrite a terminal status.
#: Derived from provenance rather than carried beside it (DL-68): a record
#: in the log has its source and nothing else, so replay must be able to
#: reach the same verdict from the same field the live engine did.
COMPLETION_SOURCES: frozenset[str] = frozenset({"adapter", "reconcile"})

#: ss6 ships `epoch` in the v2 envelope though it is inert on one host,
#: because adding it after the clients migrate is a second wire break. S6
#: allocates it for real.
INERT_EPOCH = 0


def fingerprint(
    *,
    baseline_id: str,
    kind: str | None,
    payload: Mapping[str, Any],
    source: str | None,
    epoch: int = INERT_EPOCH,
    expect: Mapping[str, int] | None = None,
    claimed_actor: str | None = None,
    host: HostCommand | None = None,
) -> str:
    """The complete semantic envelope, hashed (concurrency-model ss6).

    `at` is deliberately absent. ss6 gives the leader the timestamp, so two
    deliveries of one command differ in `at` and in nothing else -- a
    fingerprint that included it would call every retry a different command
    and defeat the dedup it exists to support. Transport framing is absent
    for the same reason, from the other direction.

    `expect` is IN, and it is the half that makes retrying safe. The same
    verb against the same job at two different revisions is two different
    commands: "kill the run I saw at 12" is not "kill whatever is running
    now". Hashing them alike would let a retry of the first be answered by
    the second's decision -- which is the confusion optimistic concurrency
    exists to prevent, arriving through the dedup path instead.

    `host` is the S5a attempt that carries no oracle event (ss8). It gets a
    key of its own rather than being folded into `payload`, so no host
    command can ever hash equal to a verb whose payload happens to look like
    one -- and it is OMITTED when absent rather than hashed as null, so
    every fingerprint an earlier build wrote is still the fingerprint this
    build computes. A hash function that quietly changed would turn an exact
    retry across a resume into a `RequestCollision`, which is the ss7
    mixed-build hazard arriving through the one door that has no version
    gate yet.
    """
    return hashlib.sha256(
        json.dumps(
            {
                "baseline_id": baseline_id,
                "epoch": epoch,
                "verb": kind,
                "payload": payload,
                "source": source,
                "expect": dict(expect) if expect is not None else None,
                "claimed_actor": claimed_actor,
                **({"host": host.wire()} if host is not None else {}),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class EnvelopeError(EngineError):
    """A request refused at the door (concurrency-model ss4 step 1). Its
    message is what the caller is told, so it names the field and what a
    good one looks like -- a refusal an operator cannot act on is a refusal
    they will route around."""


class Envelope(BaseModel):
    """The ss6 command envelope, minus the transport framing and the verb
    itself. What a caller must say about a mutation beyond what it does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    #: the revisions this command was composed against, ss6-namespaced. Never
    #: empty and never optional: ss0 is mandatory.
    expect: dict[str, int]
    epoch: int = INERT_EPOCH
    #: a CLIENT HINT, exactly as named. There is no authentication at this
    #: tier (control-protocol ss7 gap 2), so this is what the caller said
    #: about itself and nothing more; ss6 gives the leader the job of
    #: stamping an authenticated principal, and no leader can do that yet.
    claimed_actor: str | None = None


def addressed_key(kind: str, payload: Mapping[str, Any]) -> str:
    """The ONE entity a verb addresses, in the ss6 `job:` / `global:` key
    space -- the same space `RuntimeState.revision` reads, so a precondition
    is a lookup rather than a translation.

    ss6: `expect` names only the addressed entity. Naming others would make
    a command's success depend on state it does not touch, which the
    semantics move constantly (a box cascade bumps every member), so the
    only preconditions an operator could write would be ones that spuriously
    fail. One key, and it is this one."""
    if kind == "SET_GLOBAL":
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise EnvelopeError("SET_GLOBAL addresses a global by name")
        return RuntimeState.global_key(name)
    job = payload.get("job")
    if not isinstance(job, str) or not job:
        raise EnvelopeError(f"{kind} addresses a job by name")
    return RuntimeState.job_key(job)


def parse_envelope(request: Mapping[str, Any], *, addressed: str, baseline_id: str) -> Envelope:
    """Steps 1-2's framing half, for every external transport there will
    ever be (concurrency-model ss4, ss6). Raises `EnvelopeError` naming the
    field; the caller turns that into its own refusal shape.

    The mandate is here rather than in the socket server because ss0 admits
    no exception, and a rule written once per transport is a rule that one
    transport will eventually write differently. In-process callers holding
    the `Engine` object are not external and do not come through here --
    they are the engine's own trust domain, the same one the scheduler and
    the adapters inject from."""
    version = request.get("v")
    if version != PROTOCOL_VERSION:
        raise EnvelopeError(
            f"protocol version {version!r}: this engine speaks v{PROTOCOL_VERSION}"
            f' -- name it as {{"v": {PROTOCOL_VERSION}}}'
        )
    named_baseline = request.get("baseline_id")
    if named_baseline != baseline_id:
        raise EnvelopeError(
            f"baseline_id {named_baseline!r} is not this run's {baseline_id!r}:"
            " a revision read from another baseline names nothing here"
        )
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise EnvelopeError(
            "request_id is required: without one a timed-out command cannot be"
            " retried safely, because nothing could recognise the retry"
        )
    epoch = request.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        # required, not defaulted, though it is inert on one host. ss6 ships
        # it in v2 precisely so clients CARRY it -- a field that may be
        # omitted is a field nobody sends, and S6 would then be the second
        # wire break that shipping it early was supposed to avoid. Every read
        # publishes the current epoch beside the revision, so a caller that
        # can compose an `expect` already has it.
        raise EnvelopeError(f"epoch is required and must be an integer, got {epoch!r}")
    actor = request.get("claimed_actor")
    if actor is not None and not isinstance(actor, str):
        raise EnvelopeError(f"claimed_actor must be a string, got {actor!r}")
    return Envelope(
        request_id=request_id,
        expect=_parse_expect(request.get("expect"), addressed=addressed),
        epoch=epoch,
        claimed_actor=actor,
    )


def _parse_expect(expect: Any, *, addressed: str) -> dict[str, int]:
    if expect is None:
        raise EnvelopeError(
            f'expect is required: name the revision you read, as {{"{addressed}": N}}'
            f" (read it from `status`/`global`; 0 means the entity is still absent)"
        )
    if not isinstance(expect, dict):
        raise EnvelopeError(f'expect must be an object like {{"{addressed}": N}}, got {expect!r}')
    if set(expect) != {addressed}:
        raise EnvelopeError(
            f"expect names {sorted(expect)} but this command addresses {addressed!r}:"
            " a precondition names the addressed entity and nothing else"
        )
    revision = expect[addressed]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise EnvelopeError(f"expect[{addressed!r}] must be a revision (a non-negative integer)")
    return {addressed: revision}


class Attempt(BaseModel):
    """One admitted input, as it goes into the log (concurrency-model ss4
    step 4). `at` is the leader timestamp and doubles as the batch's
    `TimeAdvanced`; `kind` absent means an attempt with no ORACLE verb --
    either a routing-table command (`host` below) or, with neither, a
    standalone time observation. ss4 admits all three by one rule.

    `expect` absent means an input the engine raised itself -- a timer, a
    scheduler tick, an adapter completion. ss0's mandate is on EXTERNALLY
    REQUESTED mutations; a cascade or a time observation is a consequence of
    applying an input, and an operator cannot hold a revision on state only
    the semantics may change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    at: datetime
    request_id: str
    fingerprint: str
    kind: str | None = None
    payload: dict[str, Any] = {}
    source: str | None = None
    expect: dict[str, int] | None = None
    epoch: int = INERT_EPOCH
    claimed_actor: str | None = None
    #: S5a: a routing-table change (concurrency-model ss8). It is an input
    #: like any other -- index, journal, `expect` -- and it carries no oracle
    #: event, because a job's condition truth cannot depend on where its
    #: machine routes (DL-93). So it rides the seam ss4 already had for an
    #: attempt with no verb, beside the standalone time observation.
    host: HostCommand | None = None

    @model_validator(mode="after")
    def _one_kind_of_input(self) -> Attempt:
        if self.kind is not None and self.host is not None:
            raise ValueError("an attempt carries an oracle verb or a host command, never both")
        return self

    def event(self) -> Event | None:
        if self.kind is None:
            return None
        return Event(
            at=self.at,
            kind=self.kind,  # type: ignore[arg-type]
            payload=dict(self.payload),
            source=self.source,
        )


class ApplyResult(BaseModel):
    """What one admitted input decided (concurrency-model ss4 step 7), with
    the revisions it moved -- the ss3 changed set, which is what a client
    that named an `expect` needs back and what a later precondition will be
    checked against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    request_id: str
    decision: Literal["applied", "rejected"]
    reason: str | None = None
    revisions: dict[str, int] = {}

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> ApplyResult:
        if (self.decision == "rejected") != (self.reason is not None):
            raise ValueError("a rejection carries its reason and an application carries none")
        return self


class Frontiers(BaseModel):
    """The log's own position (concurrency-model ss2), typed so the two
    indices cannot be confused for each other.

    They are genuinely different facts. `committed_index` is what is
    durably ADMITTED -- the commit point, past which an input WILL be
    applied by this engine or by the next one that replays the log.
    `applied_index` is what has a durable decision. The gap between them is
    the crash window, and reading one where the other was meant is how a
    replay silently skips an input. `at` is the leader-timestamp frontier,
    monotone across inputs already admitted but not yet applied (ss4 step
    3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    committed_index: int = 0
    applied_index: int = 0
    at: datetime | None = None

    @model_validator(mode="after")
    def _applied_never_leads(self) -> Frontiers:
        if self.applied_index > self.committed_index:
            raise ValueError(
                f"applied {self.applied_index} leads committed {self.committed_index}:"
                " a decision without an admission"
            )
        return self

    def admit(self, at: datetime) -> Frontiers:
        """Steps 3-4: take the next index at a non-decreasing stamp."""
        if self.at is not None and at < self.at:
            raise EngineError(f"admission time went backwards: {at} < {self.at}")
        return Frontiers(
            committed_index=self.committed_index + 1,
            applied_index=self.applied_index,
            at=at,
        )

    def record(self, index: int) -> Frontiers:
        """Step 7. Results land in admission order because steps 5-7 do not
        yield to another state-changing input, so a gap here is not a
        late-arriving decision -- it is a lost one."""
        if index != self.applied_index + 1:
            raise EngineError(
                f"result for index {index} out of order: applied is {self.applied_index}"
            )
        return Frontiers(
            committed_index=self.committed_index,
            applied_index=index,
            at=self.at,
        )


class AdmissionRefused(EngineError):
    """Refused at ss4 steps 1-2: nothing appended, no index consumed, no
    clock moved. The caller may compose a new command; there is nothing to
    retry, because there is nothing in the log to retry against."""


class RequestCollision(AdmissionRefused):
    """One `request_id`, two different commands. Loud rather than silent:
    answering the second from the first's decision would apply neither."""


class DecisionIndex:
    """`request_id` -> the decision that request already got, plus the
    fingerprint it was admitted under (concurrency-model ss4 step 2)."""

    def __init__(self) -> None:
        self._results: dict[str, ApplyResult] = {}
        self._by_index: dict[int, ApplyResult] = {}
        self._fingerprints: dict[str, str] = {}

    def note(self, attempt: Attempt) -> None:
        self._fingerprints[attempt.request_id] = attempt.fingerprint

    def record(self, result: ApplyResult) -> None:
        self._results[result.request_id] = result
        self._by_index[result.index] = result

    def for_index(self, index: int) -> ApplyResult | None:
        return self._by_index.get(index)

    def lookup(self, request_id: str, fingerprint: str) -> ApplyResult | None:
        """The prior decision for an exact retry, or None for an unseen
        request. Raises on a reused id, and on an id whose attempt has no
        decision yet -- unreachable while one writer owns the oracle and
        steps 5-7 do not yield, which is exactly why meeting it would mean
        something else is writing."""
        seen = self._fingerprints.get(request_id)
        if seen is None:
            return None
        if seen != fingerprint:
            raise RequestCollision(
                f"request_id {request_id!r} was admitted for a different command"
                " (fingerprint mismatch): reuse an id only for an exact retry"
            )
        result = self._results.get(request_id)
        if result is None:
            raise EngineError(
                f"request_id {request_id!r} is admitted but undecided: a second writer"
                " is applying inputs, or steps 5-7 yielded"
            )
        return result


def precondition_reason(oracle: Oracle, expect: Mapping[str, int]) -> str | None:
    """The ss0 check: did the entity move since the caller read it?

    Read AFTER the batch's time half has applied (ss4 orders step 5 before
    step 6), which is not a detail. A term_run_time kill firing on this
    input's own clock observation bumps the job it kills, so an operator's
    command composed against the pre-kill revision is refused BY that kill --
    the same ordering CM-04 pins for the completion gate, reaching the
    precondition for free."""
    for key, want in expect.items():
        actual = oracle.store.revision(key)
        if actual != want:
            return (
                f"precondition failed: {key} is at revision {actual}, not the {want}"
                " this command was composed against"
            )
    return None


def stale_reason(oracle: Oracle, ev: Event) -> str | None:
    """The runner-design ss4 stale-completion gate, as a function of state.

    It is the only precondition the estate has today, and it guards ONLY
    engine-made completions: a completion whose run has moved on, or whose
    job the oracle has already ended, is a report about a run that no
    longer exists. Pure, so replay reaches the same verdict the live engine
    did without an Engine to ask (S3 puts `expect` beside it)."""
    job = ev.job()
    if job is None:
        return None
    rt = oracle.store.job.get(job)
    if rt is None or rt.run_number != ev.payload.get("run_number"):
        return "run_number mismatch"
    if rt.status in TERMINAL:
        return "job already terminal"
    return None


@dataclass
class Applied:
    """The outcome of applying one admitted attempt: what to record, and
    what the shell has to dispatch on."""

    result: ApplyResult
    emitted: list[Event] = field(default_factory=list)


def apply_attempt(
    oracle: Oracle, attempt: Attempt, *, decided: ApplyResult | None = None
) -> Applied:
    """Steps 5-7 for one admitted attempt, live or replayed.

    One function, because the live engine and replay must not be able to
    disagree: the state they reach is the same state or the log is not a
    record of anything. `decided` is a DURABLE decision from the log, and
    it wins -- a gate that has changed since must reproduce what the log
    says happened, not what it would decide now.
    """
    ev = attempt.event()
    with oracle.batch(attempt.at) as batch:  # step 5: the time half, timers first
        if decided is not None:
            decision, reason = decided.decision, decided.reason
        else:
            reason = _gate(oracle, attempt, ev)  # step 6
            decision = "rejected" if reason is not None else "applied"
        if decision == "applied":
            if ev is not None:
                batch.feed(ev)
            elif attempt.host is not None:
                # ss8's routing state is admitted input, applied HERE rather
                # than fed: the oracle must never read a host row, so this
                # writes the ss3 owner directly, inside the same batch, and
                # takes that input's single revision like any other entity
                apply_host_command(oracle.store, attempt.host, actor=attempt.claimed_actor)
    if decided is not None and decided.revisions != batch.revisions:
        # concurrency-model ss7's mixed-build hazard, caught where it is
        # cheap: identical inputs that derive different revisions mean this
        # build is not the state machine that wrote the log, and every
        # precondition checked from here on would be checked against a
        # number the log never produced.
        raise EngineError(
            f"replay diverged at index {attempt.index}: the log records revisions"
            f" {decided.revisions} and this build derives {batch.revisions}"
        )
    return Applied(
        result=decided
        or ApplyResult(
            index=attempt.index,
            request_id=attempt.request_id,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            revisions=batch.revisions,
        ),
        emitted=batch.emitted,
    )


def _gate(oracle: Oracle, attempt: Attempt, ev: Event | None) -> str | None:
    if attempt.expect is not None:
        reason = precondition_reason(oracle, attempt.expect)
        if reason is not None:
            return reason  # ss0, and it outranks everything below
    if attempt.host is not None:
        # ss8's own preconditions sit HERE for the reason `expect` does: they
        # read mutable state, so a verdict reached at the door would answer
        # against a table that had moved by the time it applied -- and replay,
        # which has no live host to probe, must reach the same verdict from
        # the same row.
        return host_rejection_reason(oracle.store, attempt.host, attempt.at)
    if ev is None or attempt.source not in COMPLETION_SOURCES:
        return None  # CHANGE_STATUS parity: an external event is never gated
    return stale_reason(oracle, ev)
