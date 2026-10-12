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
  replay. The batch carries both halves in ONE record -- the attempt's own
  `at` IS the observation -- which is the stronger form of what ss4 asks
  for: one line cannot be torn in half by a crash (DL-111 corrected the
  two-record reading this comment used to assert).

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
  never entered the system, so the log says nothing about it. The dry apply,
  after step 3's clock check and before the frontier moves, refuses the
  same way: a control input whose apply raises (`apply_faulted`) or whose
  own half breaks a declared transition (`transition_violation`) on a fork
  of the oracle, before it is logged.
- **Rejected** -- step 6. The envelope was good and the precondition was
  not: the entity moved between the caller's read and this input. That IS
  an event in the estate's history -- it consumed an index and its time
  half fired timers -- so it is recorded as a decision.

An engine-made input has nobody to hear a refusal, so its dry apply ends
another way. If the apply raises and the time half alone applies, the
input is quarantined: it is logged with a rejected decision under
`engine_apply_faulted`, and only its time half applies. If the time half
raises too, the engine stops before the input is logged
(`TimeHalfStop`). Replay classifies an undecided attempt that raises the
same way: both ask `classify_fault`.

S5a adds a second shape of verbless attempt beside the time observation: a
routing-table command (`docs/concurrency-model.md` ss8). It is admitted by
this same order and gated in this same place, and it is applied to the ss3
owner rather than fed, because the oracle must never read a host row.

The outbox with its `effect_id`-to-`executor_id` binding (S5c, DL-96) and
epoch allocation (S6, DL-99) have both landed since. `epoch` names a real
leader term, and the check for it is in its ss4 place, AFTER dedup, so an
exact old-epoch retry recovers its original result while an unseen
old-epoch request is refused. `INERT_EPOCH` survives as the default for an
Engine that held no election at all -- no run root, no log -- and not as a
placeholder.
"""

from __future__ import annotations

import hashlib
import json
import reprlib
import sys

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, model_validator

from dsl41.oracle import Oracle
from dsl41.canon import is_scalar_string, is_wire_int
from dsl41.period import CMD_GRACE_S
from dsl41.oracle_state import LIVE, TERMINAL, Event, EventKind, RuntimeState
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import Code, Rejection
from dsl41.runner_hosts import HostCommand, apply_host_command, host_rejection_reason
from dsl41.state_machine import (
    Context,
    Executor,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
    TransitionError,
    Violation,
)
from dsl41.state_machine import Event as ChartEvent

#: The wire version of the ss6 envelope. There is no earlier version to fall
#: back to: ss0 refuses a caller that does not name a version, and accepting
#: an unversioned or older request "for compatibility" is the opt-out ss0
#: forbids. v1 went that way at DL-90; v2 goes the same way at DL-118, which
#: replaces the subscribe stream's `result` and `effect` records with one
#: atomic `decision` -- a v2 client waiting on `rec == "effect"` would
#: silently stop seeing intents, so a compatibility projection would be a
#: second record shape for one fact.
PROTOCOL_VERSION = 3

#: Sources whose events are ENGINE-MADE completions and therefore pass the
#: stale gate. Externally injected STATUS keeps sendevent CHANGE_STATUS
#: parity and is never gated -- it may legally overwrite a terminal status.
#: Derived from provenance rather than carried beside it (DL-68): a record
#: in the log has its source and nothing else, so replay must be able to
#: reach the same verdict from the same field the live engine did.
COMPLETION_SOURCES: frozenset[str] = frozenset({"adapter", "reconcile"})

#: What the engine does when an apply breaks a declared transition
#: (`dsl41 run --on-transition-violation`). A run option and not a semantic
#: switch: it changes no derived state, so replay never reads it.
#:
#: - `refuse`: refuse a control input; take an engine-made move and record it.
#: - `continue`: take every move and record it.
#: - `stop`: as `refuse`, and stop the engine once a recorded move's
#:   decision is durable, so the next resume replays that decision.
ViolationPolicy = Literal["refuse", "continue", "stop"]
VIOLATION_POLICIES: tuple[str, ...] = get_args(ViolationPolicy)

#: ss6 shipped `epoch` in the v2 envelope though it was inert on one host,
#: because adding it after the clients migrate is a second wire break. S6
#: allocates it for real.
INERT_EPOCH = 0

#: The request-id prefix the engine reserves for itself. An input the
#: engine makes (a tick, a completion, an injection with no id) is named
#: `engine:<index>` (`Engine._attempt`); a client id in that space could
#: be decided at an index and then be taken again by a later engine-made
#: input, which would overwrite the client's decision. `parse_envelope`
#: refuses such an id, so no external request holds a name in this space
#: (control-protocol ss3). A committed seal's retry is answered before the
#: check on the control socket, which is harmless: a seal's id never enters
#: the decision index.
ENGINE_REQUEST_ID_PREFIX = "engine:"

#: This build's event alphabet, for `Attempt.event`'s named refusal: an
#: `input` record whose kind is outside it refuses by name -- the WAL row's
#: strict-kind discipline applied at the event alphabet (protocol-evolution
#: ss1 as amended by DL-158).
_EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))


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
    they will route around. Its `code` is required: the raise site names
    the reason, and the socket answers it (control-protocol ss2)."""

    code: Code

    def __init__(self, message: str, *, code: Code) -> None:
        super().__init__(message, code=code)


class Envelope(BaseModel):
    """The ss6 command envelope, minus the transport framing and the verb
    itself. What a caller must say about a mutation beyond what it does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    #: the revisions this command was composed against, ss6-namespaced. Never
    #: empty and never optional: ss0 is mandatory.
    expect: dict[str, int]
    epoch: int = INERT_EPOCH
    #: a CLIENT HINT, exactly as named: this is what the caller said about
    #: itself. On an estate with NO access map the socket's mode is the whole
    #: boundary and the claim stands as it arrived; on an ARMED one the
    #: server overwrites it with the kernel-authenticated principal before
    #: anything is fingerprinted or journaled (access-model ss3, DL-146/147).
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
            raise EnvelopeError("SET_GLOBAL addresses a global by name", code="invalid_argument")
        return RuntimeState.global_key(name)
    job = payload.get("job")
    if not isinstance(job, str) or not job:
        raise EnvelopeError(f"{kind} addresses a job by name", code="invalid_argument")
    return RuntimeState.job_key(job)


def refuse_reserved_request_id(request_id: str) -> None:
    """Raise `EnvelopeError` for a client request id in the engine's own
    name space (`ENGINE_REQUEST_ID_PREFIX`). `parse_envelope` calls it for
    every external mutation. `dsl41 seal` offline calls it too, because it
    builds its `SealRequest` without an envelope."""
    if request_id.startswith(ENGINE_REQUEST_ID_PREFIX):
        raise EnvelopeError(
            f"request_id {request_id!r} begins with {ENGINE_REQUEST_ID_PREFIX!r}, a prefix the"
            " engine reserves for the inputs it names itself: choose another id",
            code="invalid_argument",
        )


def parse_envelope(
    request: Mapping[str, Any], *, addressed: str | None, baseline_id: str
) -> Envelope:
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
            f' -- name it as {{"v": {PROTOCOL_VERSION}}}',
            code="unsupported_version",
        )
    named_baseline = request.get("baseline_id")
    if named_baseline != baseline_id:
        raise EnvelopeError(
            f"baseline_id {named_baseline!r} is not this run's {baseline_id!r}:"
            " a revision read from another baseline names nothing here",
            code="baseline_mismatch",
        )
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise EnvelopeError(
            "request_id is required: without one a timed-out command cannot be"
            " retried safely, because nothing could recognise the retry",
            code="invalid_argument",
        )
    refuse_reserved_request_id(request_id)
    epoch = request.get("epoch")
    if not is_wire_int(epoch):
        # required, not defaulted, though it is inert on one host. ss6 ships
        # it in v2 precisely so clients CARRY it -- a field that may be
        # omitted is a field nobody sends, and S6 would then be the second
        # wire break that shipping it early was supposed to avoid. Every read
        # publishes the current epoch beside the revision, so a caller that
        # can compose an `expect` already has it.
        raise EnvelopeError(
            f"epoch is required and must be an integer, got {epoch!r}", code="invalid_argument"
        )
    actor = request.get("claimed_actor")
    if actor is not None and not isinstance(actor, str):
        raise EnvelopeError(
            f"claimed_actor must be a string, got {actor!r}", code="invalid_argument"
        )
    # PR-10a: every string the envelope carries into the WAL is a Unicode
    # scalar string, or the estate could never be sealed while the record
    # stood. The payload's strings are the verb's business; these are the
    # envelope's own.
    for name, value in (("request_id", request_id), ("claimed_actor", actor)):
        if isinstance(value, str) and not is_scalar_string(value):
            raise EnvelopeError(
                f"{name} carries an unpaired surrogate (PR-10a)", code="invalid_argument"
            )
    return Envelope(
        request_id=request_id,
        expect=_parse_expect(request, addressed=addressed),
        epoch=epoch,
        claimed_actor=actor,
    )


def _parse_expect(request: Mapping[str, Any], *, addressed: str | None) -> dict[str, int]:
    """ss3's precondition, read off the REQUEST rather than off a value.

    A command that addresses no row takes no `expect`, and the KEY is what
    arrives: `"expect": null` read as "no expect" made the one command with
    no precondition the door every other command could slip through
    (DL-151). Asking `"expect" in request` is the one place that rule is
    written -- the control server used to write it a second time, by key,
    while this half still tested the value."""
    if addressed is None:
        if "expect" in request:
            raise EnvelopeError(
                "expect is not allowed on a command that addresses no row:"
                " a boundary is not a row mutation (period-model ss2.2)",
                code="invalid_argument",
            )
        return {}
    expect = request.get("expect")
    if expect is None:
        raise EnvelopeError(
            f'expect is required: name the revision you read, as {{"{addressed}": N}}'
            f" (read it from `status`/`global`/`hosts`; 0 means the entity is still"
            " absent)",
            code="expect_required",
        )
    if not isinstance(expect, dict):
        raise EnvelopeError(
            f'expect must be an object like {{"{addressed}": N}}, got {expect!r}',
            code="invalid_argument",
        )
    if set(expect) != {addressed}:
        raise EnvelopeError(
            f"expect names {sorted(expect)} but this command addresses {addressed!r}:"
            " a precondition names the addressed entity and nothing else",
            code="invalid_argument",
        )
    revision = expect[addressed]
    if not is_wire_int(revision) or revision < 0:
        raise EnvelopeError(
            f"expect[{addressed!r}] must be a revision (a non-negative integer)",
            code="invalid_argument",
        )
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
    # `str`, not `EventSource`: replayed from the WAL (see `Event.source`)
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

    @property
    def verbless(self) -> bool:
        """A standalone time observation: no oracle verb and no host
        command. The opening release and the seal's cutoff are such
        observations. It is all time half, so a fault in it is never
        quarantined (concurrency-model ss4)."""
        return self.kind is None and self.host is None

    @property
    def engine_made(self) -> bool:
        """An input the engine made itself, which nobody would hear refused
        (concurrency-model ss4): a tick, a completion, a routing or time
        observation. Every other input is a control input: one that crossed
        the socket, which always carries `expect` (ss0), or an event an
        in-process script raised with the `control` source, which is how it
        sends an operator's command. An unattributed event (the bisimulation
        harness) is engine-made: it must behave as the oracle does when fed
        directly. Read from the logged attempt alone, so the live engine and
        the journal narration ask the same question of the same record."""
        return self.expect is None and self.source != "control"

    def event(self) -> Event | None:
        if self.kind is None:
            return None
        if self.kind not in _EVENT_KINDS:
            # protocol-evolution ss1, the WAL row's discipline applied at the
            # event alphabet (DL-158): an unknown kind refuses BY NAME, not as
            # a raw validation traceback. The refusal states only what it
            # observed -- a wider-alphabet writer and a corrupt record land
            # here alike, and it cannot tell them apart.
            raise EngineError(
                f"input {self.index} carries event kind {self.kind!r}, which this"
                " build's event alphabet does not include"
                f" (kinds: {', '.join(sorted(_EVENT_KINDS))})"
            )
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
    checked against.

    `code` is the stable code of a rejection's reason (period-model ss2.3).
    The model holds only that an application carries none. That a native
    rejection carries one is the WRITER's rule (`Journal.decision`), so a
    record written before codes existed still reads, with `code` None. A
    stored code is opaque here: `str`, never checked against the registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    request_id: str
    decision: Literal["applied", "rejected"]
    reason: str | None = None
    code: str | None = None
    revisions: dict[str, int] = {}

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> ApplyResult:
        if (self.decision == "rejected") != (self.reason is not None):
            raise ValueError("a rejection carries its reason and an application carries none")
        if self.decision == "applied" and self.code is not None:
            raise ValueError("an application carries no code")
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
            raise EngineError(
                f"admission time went backwards: {at} < {self.at}", code="clock_regressed"
            )
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
    retry, because there is nothing in the log to retry against. Its `code`
    is required, as `EnvelopeError`'s is."""

    code: Code

    def __init__(self, message: str, *, code: Code) -> None:
        super().__init__(message, code=code)


class SealInFlight(AdmissionRefused):
    """`seal_in_flight`: a boundary is queued or running, and one seal runs
    at a time (period-model ss7), an exact retry of it included. It names
    the boundary in flight, so a client can tell its own boundary, still
    running, from another's (control-protocol ss3, `in_flight_request_id`)."""

    def __init__(self, message: str, *, in_flight_request_id: str) -> None:
        super().__init__(message, code="seal_in_flight")
        self.in_flight_request_id = in_flight_request_id


class TimeHalfStop(EngineError):
    """An engine-made input whose time half raises on a fork: the timers
    due at its stamp, the opening release, or the lazy clock checks
    (concurrency-model ss4). Raised before the frontier moves and before
    the append, so the log stays clean and nothing is dispatched. The same
    timers fire first in every later batch, so the engine stops rather
    than meet the fault again on its next input; `dsl41 run` exits 6, and
    the engine unit does not restart it. Resume replays a clean log and
    stops the same way until a build that fixes the fault runs.

    `cause` is what the time half raised alone; `full`, when it differs,
    is what the whole apply raised first, named as context."""

    def __init__(self, attempt: Attempt, cause: Exception, full: Exception | None = None) -> None:
        note = _whole_input_note(cause, full)
        context = "" if note is None else f" ({note})"
        super().__init__(
            f"engine stopped before admitting input {attempt.index}"
            f" ({describe_input(attempt)} at {attempt.at.isoformat()}): its time half"
            f" raised {type(cause).__name__}: {cause}{context}. Nothing was logged;"
            " deploy a build that fixes the fault, then resume"
        )


class TransitionStop(EngineError):
    """`--on-transition-violation stop`: an applied input broke a declared
    transition, and its decision is durable. Raised after step 7, never
    inside it, so the log holds the decision and the next resume replays it
    without meeting a raise."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="transition_violation")


class RequestCollision(AdmissionRefused):
    """One `request_id`, two different commands. Loud rather than silent:
    answering the second from the first's decision would apply neither.

    `original` is the decision the id already holds, or None while its
    attempt is undecided (DL-217). It rides on the refusal so the socket can
    tell a caller whose retry no longer matches what happened to the first
    command, without promoting that decision into the retry's own outcome."""

    def __init__(self, message: str, *, original: ApplyResult | None = None) -> None:
        super().__init__(message, code="request_id_reused")
        self.original = original


#: The stderr prefix of a quarantined input (concurrency-model ss4). The
#: runbook's watch table alerts on it, beside the decision's stored code.
QUARANTINE_LOG_PREFIX: Final = "dsl41: input quarantined"


def report_quarantine(result: ApplyResult) -> None:
    """One line on stderr for an input quarantined under
    `engine_apply_faulted`: by the live engine once its decision is durable,
    or by resume when it writes the decision replay recovered. Best effort,
    like `runner_journal.best_effort_report` (not imported: runner_journal
    imports this module): a write that fails is dropped."""
    try:
        sys.stderr.write(f"{QUARANTINE_LOG_PREFIX}: input {result.index}: {result.reason}\n")
    except Exception:  # noqa: BLE001, S110 -- a report that cannot be made is dropped
        # never raise into the engine between a decision and its answer
        pass


# ---- one request id's in-flight window as a statechart (concurrency-model ss4)
#
# `ADMISSION` is the declaration (docs/statechart-engine.md). The decision index
# holds one instance per request id whose attempt line is durable and whose
# decision is not, keyed by `(index, request_id)`, on the executor it runs on:
# the engine's, or its own when it is read offline. `note` starts the instance
# in `admitted` once the attempt line is written; `record` posts the decision,
# and the instance, now final, is forgotten in the same call. There is no
# instance before `note`: dedup and the refusals before admission (a stale
# epoch, a stamp behind the frontier during a seal, a dry apply that refuses)
# post nothing, so the old `unseen` state and its rows are gone.
#
# Two events. `decide` carries the `ApplyResult`; its decision is the partition
# the rows in `admitted` read. `input` carries the fingerprint of an input that
# names an open id; the guards compare it with the attempt's, which is the
# instance's data. An input that names a decided id never reaches the chart:
# the index answers an exact retry from the stored decision, and refuses a
# reused id carrying that decision, before any post (DL-217, CM-05). Every
# input carries a request id, engine-made ones too, so an instance resting per
# decided id would keep a whole period's inputs in memory for rows that only
# read a stored decision. The rows have no actions: the index writes its maps
# after the microstep. The root's ignored rows exist because completeness is
# syntactic (statechart-engine ss10); no tier path posts them. A gap in the
# chart is a `StatechartError`, which stops the engine with exit 7
# (statechart-engine ss5).

#: The decision of an admitted input; its payload is the `ApplyResult`.
DECIDE: Final = "decide"
#: An input that names an open request id; its payload is the input's fingerprint.
INPUT: Final = "input"
#: The states an instance rests in once its decision is made.
_FINALS: Final = frozenset({"applied", "rejected"})


# -- the guards. Each docstring is its rows' prose.


def _applied(ctx: Context) -> bool:
    """The decision is `applied`: the gate passed, or a replayed durable decision
    says applied."""
    result: ApplyResult = ctx.event.payload
    return result.decision == "applied"


def _rejected(ctx: Context) -> bool:
    """The decision is `rejected`: a precondition, the stale-completion gate or a
    host guard rejected the input, the input was quarantined, or a replayed
    durable decision says rejected."""
    result: ApplyResult = ctx.event.payload
    return result.decision == "rejected"


def _same(ctx: Context) -> bool:
    """The input carries the fingerprint the attempt was admitted under."""
    attempt: Attempt = ctx.data
    return bool(ctx.event.payload == attempt.fingerprint)


def _other(ctx: Context) -> bool:
    """The input carries another fingerprint than the attempt's."""
    attempt: Attempt = ctx.data
    return bool(ctx.event.payload != attempt.fingerprint)


def _decided(ctx: Context) -> bool:
    """The instance rests in a final state. Nothing posts to it then: the index
    forgets the instance in the call that made it final, and answers an input
    for a decided id from the stored decision before any post (DL-217,
    CM-05)."""
    return bool(ctx.active & _FINALS)


_DECIDED_NOTE: Final = (
    "A decision is final: the index answers an exact retry from the stored decision,"
    " and refuses a reused id with request_id_reused carrying it, before any post"
    " (DL-217, CM-05). It forgets the instance in the call that made it final. No tier"
    " path posts this row."
)

#: One request id from its durable attempt to its durable decision
#: (concurrency-model ss4), one instance per id in flight. A seal request
#: never starts one: its decision is the `seal` record. Readiness does ask
#: `lookup` whether the seal's request_id is already spent on another command
#: (`boundary._check_request_id`).
ADMISSION: Final = Statechart(
    name="admission",
    root=State(
        "admission",
        regions=(
            Region(
                "window",
                "admitted",
                (
                    State("admitted"),
                    State("applied", final=True),
                    State("rejected", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "admission.02",
            "admitted",
            DECIDE,
            "applied",
            guard="applied",
            cite="concurrency-model ss4 steps 5-7, DL-118",
            note="The decision line, with the outbox entries it implies.",
        ),
        Row(
            "admission.03",
            "admitted",
            DECIDE,
            "rejected",
            guard="rejected",
            cite="concurrency-model ss4, DL-235, DL-272, DL-320",
            note="The decision line, with its stored code.",
        ),
        Row(
            "admission.09",
            "admitted",
            INPUT,
            guard="other",
            kind="refusal",
            code="request_id_reused",
            cite="concurrency-model ss4 step 2, DL-217",
            note="Refuse request_id_reused, with no decision to carry.",
        ),
        Row(
            "admission.10",
            "admitted",
            INPUT,
            guard="same",
            kind="internal",
            cite="concurrency-model ss4 step 2",
            note=(
                "The index raises an EngineError: a second writer is applying inputs, or"
                " steps 5-7 yielded. The engine stops."
            ),
        ),
        Row(
            "admission.11",
            "admission",
            DECIDE,
            guard="decided",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable=(
                "rows .02 and .03 partition every decision in `admitted`, so the walk"
                " reaches the root only from a final state"
            ),
            note=_DECIDED_NOTE,
        ),
        Row(
            "admission.12",
            "admission",
            INPUT,
            guard="decided",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable=(
                "rows .09 and .10 partition every fingerprint in `admitted`, so the walk"
                " reaches the root only from a final state"
            ),
            note=_DECIDED_NOTE,
        ),
    ),
    guards={
        "applied": _applied,
        "rejected": _rejected,
        "same": _same,
        "other": _other,
        "decided": _decided,
    },
    clock="logical",
)


class DecisionIndex:
    """`request_id` -> the decision that request already got, plus the
    fingerprint it was admitted under (concurrency-model ss4 step 2).

    An id whose attempt line is durable and whose decision is not is an
    `ADMISSION` instance in `machines`, keyed by `key(request_id)`; its
    decision is the stored `ApplyResult` once made, and the instance is gone.
    An engine's index runs on the engine's executor, so a process has one
    (statechart-engine ss11); an index read offline makes its own. Two indexes
    on one executor never share a key."""

    def __init__(self, machines: Executor | None = None) -> None:
        self.machines = machines if machines is not None else Executor()
        self._results: dict[str, ApplyResult] = {}
        self._by_index: dict[int, ApplyResult] = {}
        self._fingerprints: dict[str, str] = {}

    def key(self, request_id: str) -> tuple[DecisionIndex, str]:
        """The executor's key of a request id's instance: this index and the id."""
        return (self, request_id)

    def __repr__(self) -> str:
        """What a statechart error's report shows for the index in a key. The
        index's identity tells two indexes on one executor apart: a refused
        resume leaves its instances beside a retry's."""
        return f"decisions@{id(self):x}"

    def state(self, request_id: str) -> str | None:
        """`admitted` | `applied` | `rejected`, or None for an id this index
        never saw: the stored decision, else the open instance's leaf."""
        result = self._results.get(request_id)
        if result is not None:
            return result.decision
        if request_id not in self._fingerprints:
            return None
        (leaf,) = self.machines.active(self.key(request_id)) - {ADMISSION.root.name}
        return leaf

    def note(self, attempt: Attempt) -> None:
        """Step 4 is durable: the id's instance starts in `admitted`, with the
        attempt as its data.

        An id that already has an attempt is refused before any start: the
        live engine answers a decided id at dedup and raises on an id in
        flight, so a second attempt line means the log disagrees with
        itself. The fingerprint is stored once the instance exists and
        before its entry runs, so a failed entry leaves the index and the
        executor agreeing that the id is in flight."""
        request_id = attempt.request_id
        decided = self._results.get(request_id)
        if decided is not None:
            raise self._fault(
                request_id,
                f"an attempt at index {attempt.index} for a request id decided at index"
                f" {decided.index} ({decided.decision}): the log disagrees with itself",
                ChartEvent(INPUT, attempt.fingerprint),
            )
        if request_id in self._fingerprints:
            first: Attempt = self.machines.data(self.key(request_id))
            raise self._fault(
                request_id,
                f"an attempt at index {attempt.index} for a request id in flight since index"
                f" {first.index}: the log disagrees with itself",
                ChartEvent(INPUT, attempt.fingerprint),
            )
        self.machines.start(self.key(request_id), ADMISSION, attempt)
        self._fingerprints[request_id] = attempt.fingerprint
        self.machines.run()

    def record(self, result: ApplyResult) -> None:
        """Step 7: the instance takes the decision, the index stores it, and
        the instance, now final, is forgotten. A decision for an id with no
        attempt in flight, never admitted or already decided, is refused
        before any post: the log or the engine lost the attempt it decides."""
        request_id = result.request_id
        if request_id not in self._fingerprints or request_id in self._results:
            raise self._fault(
                request_id,
                f"a decision at index {result.index} for a request id with no attempt in flight",
                ChartEvent(DECIDE, result),
            )
        key = self.key(request_id)
        self.machines.dispatch(key, ChartEvent(DECIDE, result))
        self._results[request_id] = result
        self._by_index[result.index] = result
        self.machines.forget(key)

    def _fault(self, request_id: str, message: str, event: ChartEvent) -> StatechartError:
        """A fault of the log or the engine that the index refuses before any
        post, in its own words (statechart-engine ss5). An id in flight gets
        the executor's facts about its instance; an id with none gets the
        machine, the key, the item and its payload, and the stored decision
        when it has one."""
        key = self.key(request_id)
        error = StatechartError(f"{ADMISSION.name} {key!r}: {message}")
        if request_id in self._fingerprints and request_id not in self._results:
            return self.machines.explain(error, key, event)
        error.facts = [
            ("machine", ADMISSION.name),
            ("instance", repr(key)),
            ("active", "no instance"),
            ("item", f"the event {event.name}"),
            ("payload", reprlib.repr(event.payload)),
        ]
        decided = self._results.get(request_id)
        if decided is not None:
            error.facts.append(("stored decision", f"{decided.decision} at index {decided.index}"))
        return error

    def for_index(self, index: int) -> ApplyResult | None:
        return self._by_index.get(index)

    def lookup(self, request_id: str, fingerprint: str) -> ApplyResult | None:
        """The prior decision for an exact retry, or None for an unseen
        request. Raises on a reused id, carrying the decision that id already
        holds (DL-217), and on an id whose attempt has no
        decision yet -- unreachable while one writer owns the oracle and
        steps 5-7 do not yield, which is exactly why meeting it would mean
        something else is writing.

        A decided id is answered here, before any post: its instance is gone
        and the stored decision is the memory. Only an id in flight is asked
        of its instance."""
        seen = self._fingerprints.get(request_id)
        if seen is None:
            return None
        result = self._results.get(request_id)
        if result is None:
            (outcome,) = self.machines.dispatch(
                self.key(request_id), ChartEvent(INPUT, fingerprint)
            )
            if outcome.refusals:
                raise _collision(request_id, None)
            raise EngineError(
                f"request_id {request_id!r} is admitted but undecided: a second writer"
                " is applying inputs, or steps 5-7 yielded"
            )
        if seen != fingerprint:
            raise _collision(request_id, result)
        return result


def _collision(request_id: str, original: ApplyResult | None) -> RequestCollision:
    return RequestCollision(
        f"request_id {request_id!r} was admitted for a different command"
        " (fingerprint mismatch): reuse an id only for an exact retry",
        original=original,
    )


def precondition_reason(oracle: Oracle, expect: Mapping[str, int]) -> Rejection | None:
    """The ss0 check: did the entity move since the caller read it?

    Read after the batch's time half has applied (ss4 orders step 5 before
    step 6), but revisions move at commit, so the gate reads the revision
    from before this input. A term_run_time kill firing inside this input's
    own batch therefore does not refuse it: everything one input causes
    shares one increment (ss0, DL-90). A deadline due strictly earlier fires
    as its own input first and does bump the job, so a command composed
    against the pre-kill revision is refused (DL-232)."""
    for key, want in expect.items():
        actual = oracle.store.revision(key)
        if actual != want:
            return Rejection(
                "precondition_failed",
                f"precondition failed: {key} is at revision {actual}, not the {want}"
                " this command was composed against",
            )
    return None


def stale_reason(oracle: Oracle, ev: Event) -> Rejection | None:
    """The runner-design ss4 stale-completion gate, as a function of state.

    It is the only precondition the estate has today, and it guards ONLY
    engine-made completions: a completion whose run has moved on, or whose
    row the oracle no longer holds live -- a terminal status, or a status
    an operator injected, such as INACTIVE (DL-235) -- is a report about a
    run that no longer exists. Pure, so replay reaches the same verdict the
    live engine did without an Engine to ask (S3 puts `expect` beside it).
    Every verdict is stored as `stale_completion`; no socket answers one."""
    job = ev.job()
    if job is None:
        return None
    rt = oracle.store.job.get(job)
    if rt is None or rt.run_number != ev.payload.get("run_number"):
        return Rejection("stale_completion", "run_number mismatch")
    if rt.status in TERMINAL:
        return Rejection("stale_completion", "job already terminal")
    if rt.status not in LIVE:
        return Rejection("stale_completion", f"job not live: {rt.status}")
    return None


@dataclass
class Applied:
    """The outcome of applying one admitted attempt: what to record, and
    what the shell has to dispatch on."""

    result: ApplyResult
    emitted: list[Event] = field(default_factory=list)
    #: (subject, violation) for every declared transition the apply broke;
    #: each is a `TRANSITION_VIOLATION` trace line already
    violations: list[tuple[str, Violation]] = field(default_factory=list)
    #: the subset the attempt itself broke, after the time half's
    #: engine-made moves: only these can refuse a control input
    command_violations: list[tuple[str, Violation]] = field(default_factory=list)


def apply_attempt(
    oracle: Oracle,
    attempt: Attempt,
    *,
    decided: ApplyResult | None = None,
    forced: Rejection | None = None,
    grace_s: float = CMD_GRACE_S,
) -> Applied:
    """Steps 5-7 for one admitted attempt, live or replayed.

    One function, because the live engine and replay must not be able to
    disagree: the state they reach is the same state or the log is not a
    record of anything. `decided` is a DURABLE decision from the log, and
    it wins -- a gate that has changed since must reproduce what the log
    says happened, not what it would decide now.

    `forced` is a quarantine (`quarantine_verdict`): the attempt is
    rejected without the gate, and only the time half applies. Unlike
    `decided`, it carries no revisions; the result takes the batch's.

    `grace_s` is the command grace this period runs, and it reaches only
    ss8's eviction bound (`runner_hosts.kill_allowance`). It is a WIRING
    fact and the WAL carries none, so a replay cannot read it -- and that
    changes no verdict, which is worth stating rather than assuming. A
    `decided` attempt never reaches the gate at all. An UNDECIDED one is
    gated against a host row the replay seeded ITSELF, whose `last_contact`
    is the seeding instant (`register_host`) and whose deadman is absent
    outside the engine's own resume: every replayed attempt is older than
    that instant, so the elapsed wait is never positive and a gated
    eviction is rejected whatever the bound is. The value therefore decides
    only for a LIVE gate, which always has the engine's own.
    """
    ev = attempt.event()
    rejection: Rejection | None = None
    decision: Literal["applied", "rejected"]
    with oracle.batch(attempt.at) as batch:  # step 5: the time half, timers first
        if decided is not None:
            decision = decided.decision
        elif forced is not None:
            rejection, decision = forced, "rejected"
        else:
            rejection = _gate(oracle, attempt, ev, grace_s=grace_s)  # step 6
            decision = "rejected" if rejection is not None else "applied"
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
            decision=decision,
            reason=None if rejection is None else rejection.reason,
            code=None if rejection is None else rejection.code,
            revisions=batch.revisions,
        ),
        emitted=batch.emitted,
        violations=batch.violations,
        command_violations=batch.command_violations,
    )


def trial_apply(
    oracle: Oracle,
    attempt: Attempt,
    *,
    forced: Rejection | None = None,
    grace_s: float = CMD_GRACE_S,
) -> Applied | Exception:
    """`apply_attempt` on a fresh fork of `oracle`, which it leaves alone:
    the result, or the exception the apply raised instead. A strict test
    run's `TransitionError` propagates, so the test fails where the move
    was made. A `StatechartError` propagates too: it is an assertion
    failure, never a fault to classify, and the tier stops before any
    durable write (docs/statechart-engine.md §5)."""
    try:
        return apply_attempt(oracle.fork(), attempt, forced=forced, grace_s=grace_s)
    except (TransitionError, StatechartError):
        raise
    except Exception as exc:  # noqa: BLE001 -- the caller classifies every fault
        return exc


def quarantine_verdict(
    oracle: Oracle, attempt: Attempt, cause: Exception, *, grace_s: float = CMD_GRACE_S
) -> Rejection | Exception:
    """What to do with an attempt whose apply raised `cause` on a fork
    (concurrency-model ss4): the rejection that quarantines it, or the
    exception its time half raises on its own.

    The time half runs alone on a second fresh fork, under the rejection
    itself. Where the first apply raised does not tell the halves apart:
    the batch commits both halves on the way out, and a commit-time check
    can trip on what either moved. So the stop names what the time half
    raised alone, which may differ from `cause`. An attempt with no verb is
    all time half, so it is never quarantined and `cause` is its fault.
    The live engine stops before the append on an exception
    (`TimeHalfStop`); replay stops with a `ReplayFault`."""
    if attempt.verbless:
        return cause
    rejection = Rejection(
        "engine_apply_faulted",
        f"quarantined: applying {describe_input(attempt)} at {attempt.at.isoformat()}"
        f" raised {type(cause).__name__}: {cause}. Its time half applied and the"
        " input did not",
    )
    alone = trial_apply(oracle, attempt, forced=rejection, grace_s=grace_s)
    return alone if isinstance(alone, Exception) else rejection


class TimeHalfFault(Exception):
    """`classify_fault`'s stop: the time half raised `cause` on its own,
    after the whole apply raised `full`. Its two callers turn it into their
    own stop, `TimeHalfStop` live and `ReplayFault` on replay, so it never
    leaves them."""

    def __init__(self, cause: Exception, full: Exception) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.full = full

    @property
    def note(self) -> str | None:
        """The whole apply's fault, as the stop's context, or None when
        it reads as the time half's own."""
        return _whole_input_note(self.cause, self.full)


def classify_fault(
    oracle: Oracle, attempt: Attempt, *, grace_s: float = CMD_GRACE_S
) -> tuple[Rejection, Exception] | None:
    """DL-320's rule for one attempt, applied on fresh forks of `oracle`,
    which it leaves alone (concurrency-model ss4).

    - The whole apply passes: None, and the attempt applies as the gate
      decides.
    - It raises and the time half applies alone: the rejection that
      quarantines the attempt, with the fault the whole apply met.
    - The time half raises too: `TimeHalfFault`.

    The live engine asks this for an engine-made input only, because a
    fault refuses a control input there. Replay asks it for every
    undecided attempt, because nobody is left to hear a refusal."""
    full = trial_apply(oracle, attempt, grace_s=grace_s)
    if not isinstance(full, Exception):
        return None
    verdict = quarantine_verdict(oracle, attempt, full, grace_s=grace_s)
    if isinstance(verdict, Exception):
        raise TimeHalfFault(verdict, full)
    return verdict, full


def _whole_input_note(cause: Exception, full: Exception | None) -> str | None:
    """What the whole input's apply raised, as a time-half stop's
    context. None when there is none, or when it reads as the time half's
    own fault `cause`: a stop names one fault once."""
    if full is None or (type(full), str(full)) == (type(cause), str(cause)):
        return None
    return f"applying the whole input raised {type(full).__name__}: {full}"


def describe_input(attempt: Attempt) -> str:
    """An input as an operator reads it: its kind, the job and run it
    names, and its source when it has one."""
    if attempt.kind is not None:
        what = attempt.kind
        job = attempt.payload.get("job")
        if job is not None:
            what += f" for job {job}"
        run = attempt.payload.get("run_number")
        if run is not None:
            what += f" run {run}"
    elif attempt.host is not None:
        what = f"host {attempt.host.verb} of {attempt.host.host_id}"
    else:
        what = "time observation"
    return what if attempt.source is None else f"{what} from {attempt.source}"


def _gate(
    oracle: Oracle, attempt: Attempt, ev: Event | None, *, grace_s: float
) -> Rejection | None:
    if attempt.expect is not None:
        rejection = precondition_reason(oracle, attempt.expect)
        if rejection is not None:
            return rejection  # ss0, and it outranks everything below
    if attempt.host is not None:
        # ss8's own preconditions sit HERE for the reason `expect` does: they
        # read mutable state, so a verdict reached at the door would answer
        # against a table that had moved by the time it applied -- and replay,
        # which has no live host to probe, must reach the same verdict from
        # the same row.
        return host_rejection_reason(
            oracle.store,
            attempt.host,
            attempt.at,
            grace_s=grace_s,
            actor=attempt.claimed_actor,
        )
    if ev is None or attempt.source not in COMPLETION_SOURCES:
        return None  # CHANGE_STATUS parity: an external event is never gated
    return stale_reason(oracle, ev)
