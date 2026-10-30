"""Admission: the frozen order every input takes, and the record it leaves.

Normative spec (S2): docs/concurrency-model.md ss4 (admission and application), ss2 (identity), ss6
(the envelope). Bare `ssN` elsewhere in the runner means docs/runner-design.md; references in
this module name their document.

One admission rule covers every input (concurrency-model ss4): scheduler ticks, adapter
completions, reconciliation injections and standalone time observations take the path operator
commands take:

    dedup -> stamp -> append the batch -> apply the time half
          -> decide -> feed or reject -> record the result

- Dedup precedes admission. A retry that already has a decision is answered from the index and
  consumes no index, leader timestamp or time observation, so a retry storm cannot walk the
  clock forward (CM-05).
- The time half applies before the decision. The batch is `TimeAdvanced(at)` plus the attempt,
  and the timers due at or before `at` fire first, so a term_run_time kill lands before the gate
  reads the status it gates on (CM-04). `Oracle.batch` holds both halves in one transaction, so
  the pair moves each entity one revision (ss3).
- The time half applies even when the attempt is rejected: a rejected completion still carries a
  real clock observation, and the kill it let fire is already acted on. The batch carries both
  halves in one record, so a crash cannot tear it (DL-111).

The decision index (`request_id` -> `ApplyResult`) makes a retry idempotent and replay two-pass.
`ApplyResult` is appended after the attempt, so pass one builds the index and pass two applies.
An attempt with no result is applied, because admission is the commit point, and it goes through
the gate because its decision did not survive. A durable decision is authoritative and never
recomputed: a build whose gate has changed reproduces the log's history.

Preconditions are mandatory (concurrency-model ss0). An externally requested mutation names the
revision its author read, or it is refused; there is no opt-out and no `"any"`. `parse_envelope`
is the one place that refuses it, so every transport reaches the same verdict (ss10 socket, S5
relay).

An input fails in one of two ways, recorded differently:

- Refused (steps 1-2): bad framing, an absent or malformed `expect`, a `baseline_id` from another
  run, a reused `request_id`, a stale `epoch`. Nothing is appended, no index is consumed, no
  clock moves. The dry apply, after step 3's clock check and before the frontier moves, refuses
  the same way when a control input's apply raises on a fork of the oracle (`apply_faulted`).
- Rejected (step 6): the envelope was good and the entity moved since the caller's read. It
  consumed an index and its time half fired timers, so it is recorded as a decision.

An engine-made input has nobody to hear a refusal. If its dry apply raises and the time half
alone applies, the input is logged with a rejected decision under `engine_apply_faulted` and only
its time half applies. If the time half raises too, the engine stops before logging
(`TimeHalfStop`). Replay classifies an undecided attempt that raises the same way; both ask
`classify_fault`.

A routing-table command (S5a, concurrency-model ss8) is a second verbless attempt beside the time
observation. It is admitted and gated by the same order, and applied to the ss3 owner rather than
fed, because the oracle never reads a host row.

The outbox binds `effect_id` to `executor_id` (S5c, DL-96) and `epoch` names a real leader term
(S6, DL-99). The epoch check sits in its ss4 place after dedup, so an exact old-epoch retry
recovers its original result and an unseen old-epoch request is refused. `INERT_EPOCH` is the
default for an Engine with no election: no run root, no log.
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
from dsl41.run_root import CMD_GRACE_S
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
)
from dsl41.state_machine import Event as ChartEvent

#: The wire version of the ss6 envelope. ss0 refuses a caller that does not name a version, and
#: accepting an older request "for compatibility" is the opt-out ss0 forbids. DL-90 dropped v1 and
#: DL-118 dropped v2; a compatibility projection of the subscribe stream would be a second record
#: shape for one fact.
PROTOCOL_VERSION = 3

#: Sources whose events are engine-made completions and pass the stale gate. Externally injected
#: STATUS keeps sendevent CHANGE_STATUS parity and is never gated: it may overwrite a terminal
#: status. The source field alone decides (DL-68), so replay reaches the live verdict.
COMPLETION_SOURCES: frozenset[str] = frozenset({"adapter", "reconcile"})

#: The epoch of an Engine with no election. The envelope carries `epoch` so that adding it is
#: not a second wire break (ss6, S6).
INERT_EPOCH = 0

#: The request-id prefix the engine reserves. An input the engine makes is named
#: `engine:<index>` (`Engine._attempt`); a client id in that space could be overwritten by a later
#: engine-made input. `parse_envelope` refuses such an id (control-protocol ss3). A committed
#: seal's retry is answered before this check on the control socket, harmlessly: a seal's id never
#: enters the decision index.
ENGINE_REQUEST_ID_PREFIX = "engine:"

#: This build's event alphabet: an `input` record whose kind is outside it refuses by name
#: (protocol-evolution ss1 as amended by DL-158).
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

    `at` and the transport framing are absent: two deliveries of one command differ only there,
    and hashing them would defeat dedup. `expect` is in: the same verb at two revisions is two
    commands. `host` (S5a, ss8) has its own key, so a host command never hashes equal to a verb,
    and it is omitted when absent so every earlier fingerprint is unchanged; a changed hash would
    turn an exact retry across a resume into a `RequestCollision` (ss7 mixed-build hazard).
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
    """A request refused at the door (concurrency-model ss4 step 1). Its message names the field
    and what a good one looks like. Its `code` is required: the raise site names the reason and
    the socket answers it (control-protocol ss2)."""

    code: Code

    def __init__(self, message: str, *, code: Code) -> None:
        super().__init__(message, code=code)


class Envelope(BaseModel):
    """The ss6 command envelope, minus the transport framing and the verb
    itself. What a caller must say about a mutation beyond what it does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    #: the revisions this command was composed against, ss6-namespaced; mandatory (ss0)
    expect: dict[str, int]
    epoch: int = INERT_EPOCH
    #: a client hint. Without an access map the claim stands as sent; with one the server
    #: overwrites it with the authenticated principal before it is fingerprinted or journaled
    #: (access-model ss3, DL-146/147).
    claimed_actor: str | None = None


def addressed_key(kind: str, payload: Mapping[str, Any]) -> str:
    """The one entity a verb addresses, in the ss6 `job:` / `global:` key space that
    `RuntimeState.revision` reads. `expect` names only this entity (ss6): others move constantly,
    such as a box cascade bumping every member."""
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
    """Raise `EnvelopeError` for a client request id in the engine's name space
    (`ENGINE_REQUEST_ID_PREFIX`). `parse_envelope` and offline `dsl41 seal` call it."""
    if request_id.startswith(ENGINE_REQUEST_ID_PREFIX):
        raise EnvelopeError(
            f"request_id {request_id!r} begins with {ENGINE_REQUEST_ID_PREFIX!r}, a prefix the"
            " engine reserves for the inputs it names itself: choose another id",
            code="invalid_argument",
        )


def parse_envelope(request: Mapping[str, Any], *, addressed: str, baseline_id: str) -> Envelope:
    """Steps 1-2's framing half, for every external transport (concurrency-model ss4, ss6).
    Raises `EnvelopeError` naming the field.

    The mandate lives here, not in the socket server, so no transport writes it differently.
    In-process callers holding the `Engine` are its own trust domain and do not come through
    here."""
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
        # required, not defaulted, so clients carry it (ss6); every read publishes the epoch
        # beside the revision
        raise EnvelopeError(
            f"epoch is required and must be an integer, got {epoch!r}", code="invalid_argument"
        )
    actor = request.get("claimed_actor")
    if actor is not None and not isinstance(actor, str):
        raise EnvelopeError(
            f"claimed_actor must be a string, got {actor!r}", code="invalid_argument"
        )
    # PR-10a: every envelope string that reaches the WAL is a Unicode scalar string, or the
    # estate could not be sealed. The payload's strings are the verb's business.
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


def _parse_expect(request: Mapping[str, Any], *, addressed: str) -> dict[str, int]:
    """ss3's precondition, read off the request. This is the one place that rule is
    written."""
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
    # `str`, not `EventSource`: replayed from the WAL (`Event.source`)
    source: str | None = None
    expect: dict[str, int] | None = None
    epoch: int = INERT_EPOCH
    claimed_actor: str | None = None
    #: S5a: a routing-table change (concurrency-model ss8). It carries no oracle event, because a
    #: job's condition truth cannot depend on where its machine routes (DL-93), and rides the ss4
    #: seam for an attempt with no verb.
    host: HostCommand | None = None

    @model_validator(mode="after")
    def _one_kind_of_input(self) -> Attempt:
        if self.kind is not None and self.host is not None:
            raise ValueError("an attempt carries an oracle verb or a host command, never both")
        return self

    @property
    def verbless(self) -> bool:
        """A standalone time observation: no oracle verb and no host command, such as the
        opening release and the seal's cutoff. It is all time half, so a fault in it is never
        quarantined (concurrency-model ss4)."""
        return self.kind is None and self.host is None

    @property
    def engine_made(self) -> bool:
        """An input the engine made itself, which nobody would hear refused (concurrency-model
        ss4): a tick, a completion, a routing or time observation. Every other input is a
        control input: it crossed the socket with `expect` (ss0), or an in-process script raised
        it with the `control` source. An unattributed event (the bisimulation harness) is
        engine-made. It reads the logged attempt alone, so live and journal narration agree."""
        return self.expect is None and self.source != "control"

    def event(self) -> Event | None:
        if self.kind is None:
            return None
        if self.kind not in _EVENT_KINDS:
            # protocol-evolution ss1 (DL-158): an unknown kind refuses by name. A wider-alphabet
            # writer and a corrupt record land here alike
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
        """Step 7. Results land in admission order because steps 5-7 do not yield, so a gap is a
        lost decision."""
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
    """Refused at ss4 steps 1-2: nothing appended, no index consumed, no clock moved. Its `code`
    is required, as `EnvelopeError`'s is."""

    code: Code

    def __init__(self, message: str, *, code: Code) -> None:
        super().__init__(message, code=code)


class TimeHalfStop(EngineError):
    """An engine-made input whose time half raises on a fork: the timers due at its stamp, the
    opening release, or the lazy clock checks (concurrency-model ss4). It is raised before the
    frontier moves and before the append, so the log stays clean. `dsl41 run` exits 6 and the
    engine unit does not restart it; resume stops the same way until a build fixes the fault.

    `cause` is what the time half raised alone; `full`, when it differs, is what the whole apply
    raised, named as context."""

    def __init__(self, attempt: Attempt, cause: Exception, full: Exception | None = None) -> None:
        note = _whole_input_note(cause, full)
        context = "" if note is None else f" ({note})"
        super().__init__(
            f"engine stopped before admitting input {attempt.index}"
            f" ({describe_input(attempt)} at {attempt.at.isoformat()}): its time half"
            f" raised {type(cause).__name__}: {cause}{context}. Nothing was logged;"
            " deploy a build that fixes the fault, then resume"
        )


class RequestCollision(AdmissionRefused):
    """One `request_id`, two different commands; answering the second from the first's decision
    would apply neither.

    `original` is the decision the id holds, or None while its attempt is undecided (DL-217). It
    lets the socket tell a caller whose retry does not match the first command, without making
    that decision the retry's outcome."""

    def __init__(self, message: str, *, original: ApplyResult | None = None) -> None:
        super().__init__(message, code="request_id_reused")
        self.original = original


#: The stderr prefix of a quarantined input (concurrency-model ss4); the runbook's watch table
#: alerts on it.
QUARANTINE_LOG_PREFIX: Final = "dsl41: input quarantined"


def report_quarantine(result: ApplyResult) -> None:
    """One stderr line for an input quarantined under `engine_apply_faulted`, written by the
    live engine once its decision is durable or by resume. Best effort, like
    `runner_journal.best_effort_report`, which imports this module: a failed write is dropped."""
    try:
        sys.stderr.write(f"{QUARANTINE_LOG_PREFIX}: input {result.index}: {result.reason}\n")
    except Exception:  # noqa: BLE001, S110 -- a report that cannot be made is dropped
        # never raise between a decision and its answer
        pass


# ---- one request id's in-flight window as a statechart (concurrency-model ss4)
#
# `ADMISSION` is the declaration (docs/statechart-engine.md). The decision index holds one
# instance per request id whose attempt line is durable and whose decision is not, keyed by
# `(index, request_id)`, on the engine's executor or its own when read offline. `note` starts the
# instance in `admitted`; `record` posts the decision and forgets the now-final instance in the
# same call. Dedup and the refusals before admission (a stale epoch, a stamp behind the frontier
# during a seal, a dry apply that refuses) post nothing.
#
# `decide` carries the `ApplyResult`; its decision is the partition the rows in `admitted` read.
# `input` carries the fingerprint of an input that names an open id; the guards compare it with
# the attempt's, the instance's data. An input that names a decided id never reaches the chart:
# the index answers an exact retry from the stored decision and refuses a reused id carrying it
# (DL-217, CM-05). A resting instance per decided id would hold a whole period's inputs in
# memory. The rows have no actions: the index writes its maps after the microstep. A final
# state ignores every event (statechart-engine ss10, DL-380); no tier path posts one there. A
# gap in the chart is a `StatechartError`, which stops the engine with exit 7
# (statechart-engine ss5).

#: The decision of an admitted input; its payload is the `ApplyResult`.
DECIDE: Final = "decide"
#: An input that names an open request id; its payload is the input's fingerprint.
INPUT: Final = "input"


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
    return not _same(ctx)


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
    ),
    guards={
        "applied": _applied,
        "rejected": _rejected,
        "same": _same,
        "other": _other,
    },
    clock="logical",
)


class DecisionIndex:
    """`request_id` -> the decision that request got, plus the fingerprint it was admitted under
    (concurrency-model ss4 step 2).

    An id with a durable attempt and no decision is an `ADMISSION` instance in `machines`, keyed
    by `key(request_id)`; once decided, the stored `ApplyResult` is the memory and the instance is
    gone. An engine's index runs on the engine's executor (statechart-engine ss11); an offline
    index makes its own. Two indexes on one executor never share a key."""

    def __init__(self, machines: Executor | None = None) -> None:
        self.machines = machines if machines is not None else Executor()
        self._results: dict[str, ApplyResult] = {}
        self._by_index: dict[int, ApplyResult] = {}
        self._fingerprints: dict[str, str] = {}

    def key(self, request_id: str) -> tuple[DecisionIndex, str]:
        """The executor's key of a request id's instance: this index and the id."""
        return (self, request_id)

    def __repr__(self) -> str:
        """What a statechart error's report shows for the index in a key; the identity tells two
        indexes on one executor apart."""
        return f"decisions@{id(self):x}"

    def state(self, request_id: str) -> str | None:
        """`admitted` | `applied` | `rejected`, or None for an unseen id: the stored decision,
        else the open instance's leaf."""
        result = self._results.get(request_id)
        if result is not None:
            return result.decision
        if request_id not in self._fingerprints:
            return None
        (leaf,) = self.machines.active(self.key(request_id)) - {ADMISSION.root.name}
        return leaf

    def note(self, attempt: Attempt) -> None:
        """Step 4 is durable: the id's instance starts in `admitted`, with the attempt as data.

        An id that already has an attempt is refused before any start, because the log
        disagrees with itself. The fingerprint is stored before the entry runs, so a failed
        entry leaves the index and the executor agreeing that the id is in flight."""
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
        """Step 7: the instance takes the decision, the index stores it and forgets the final
        instance. A decision for an id with no attempt in flight is refused before any post."""
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
        """A fault of the log or the engine, refused before any post (statechart-engine ss5). An
        id in flight gets the executor's facts; an id with none gets the machine, key, item,
        payload and any stored decision."""
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
        """The prior decision for an exact retry, or None for an unseen request. Raises on a
        reused id, carrying its decision (DL-217), and on an id with no decision yet, which
        means a second writer.

        A decided id is answered here, before any post; only an id in flight asks its
        instance."""
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

    Revisions move at commit, so the gate reads the revision from before this input. A
    term_run_time kill inside this input's own batch does not refuse it: one input shares one
    increment (ss0, DL-90). A deadline due strictly earlier fires as its own input first, and a
    command composed against the pre-kill revision is refused (DL-232)."""
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
    """The runner-design ss4 stale-completion gate, as a pure function of state (S3).

    It guards only engine-made completions: one whose run has moved on, or whose row is not
    live (a terminal status, or one an operator injected such as INACTIVE, DL-235), reports a
    run that has ended. Every verdict is stored as `stale_completion`; no socket answers
    one."""
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
    """The outcome of applying one admitted attempt: what to record and what the shell
    dispatches."""

    result: ApplyResult
    emitted: list[Event] = field(default_factory=list)


def apply_attempt(
    oracle: Oracle,
    attempt: Attempt,
    *,
    decided: ApplyResult | None = None,
    forced: Rejection | None = None,
    grace_s: float = CMD_GRACE_S,
) -> Applied:
    """Steps 5-7 for one admitted attempt, live or replayed.

    Live and replay share this function. `decided` is a durable decision from the log and wins
    over the gate.

    `forced` is a quarantine (`quarantine_verdict`): the attempt is rejected without the gate
    and only the time half applies. Unlike `decided`, it carries no revisions; the result takes
    the batch's.

    `grace_s` is the period's command grace and reaches only ss8's eviction bound
    (`runner_hosts.kill_allowance`). The WAL carries none and a replay changes no verdict: a
    decided attempt never reaches the gate, and an undecided one is gated against a host row the
    replay seeded at `register_host`, so the elapsed wait is never positive and a gated eviction
    is rejected whatever the bound is. The value decides only for a live gate.
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
                # ss8's routing state is applied here, not fed: the oracle never reads a host
                # row, so this writes the ss3 owner inside the same batch
                apply_host_command(oracle.store, attempt.host, actor=attempt.claimed_actor)
    if decided is not None and decided.revisions != batch.revisions:
        # concurrency-model ss7's mixed-build hazard: identical inputs deriving different
        # revisions mean this build is not the state machine that wrote the log
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
    )


def trial_apply(
    oracle: Oracle,
    attempt: Attempt,
    *,
    forced: Rejection | None = None,
    grace_s: float = CMD_GRACE_S,
) -> Applied | Exception:
    """`apply_attempt` on a fresh fork of `oracle`, which it leaves alone: the result, or the
    exception the apply raised. A `StatechartError` propagates: it is an assertion failure, and
    the tier stops before any durable write (docs/statechart-engine.md §5)."""
    try:
        return apply_attempt(oracle.fork(), attempt, forced=forced, grace_s=grace_s)
    except StatechartError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the caller classifies every fault
        return exc


def quarantine_verdict(
    oracle: Oracle, attempt: Attempt, cause: Exception, *, grace_s: float = CMD_GRACE_S
) -> Rejection | Exception:
    """What to do with an attempt whose apply raised `cause` on a fork (concurrency-model ss4):
    the rejection that quarantines it, or the exception its time half raises alone.

    The time half runs alone on a second fresh fork, under the rejection, because the batch
    commits both halves and a commit-time check can trip on either. An attempt with no verb is
    all time half and is never quarantined. The live engine stops before the append on an
    exception (`TimeHalfStop`); replay stops with a `ReplayFault`."""
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
    """`classify_fault`'s stop: the time half raised `cause` alone, after the whole apply raised
    `full`. Its callers turn it into `TimeHalfStop` live and `ReplayFault` on replay."""

    def __init__(self, cause: Exception, full: Exception) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.full = full

    @property
    def note(self) -> str | None:
        """The whole apply's fault as the stop's context, or None when it matches the time
        half's."""
        return _whole_input_note(self.cause, self.full)


def classify_fault(
    oracle: Oracle, attempt: Attempt, *, grace_s: float = CMD_GRACE_S
) -> tuple[Rejection, Exception] | None:
    """DL-320's rule for one attempt, applied on fresh forks of `oracle` (concurrency-model
    ss4).

    - The whole apply passes: None, and the attempt applies as the gate
      decides.
    - It raises and the time half applies alone: the rejection that
      quarantines the attempt, with the fault the whole apply met.
    - The time half raises too: `TimeHalfFault`.

    The live engine asks this for an engine-made input only, since a fault refuses a control
    input. Replay asks it for every undecided attempt."""
    full = trial_apply(oracle, attempt, grace_s=grace_s)
    if not isinstance(full, Exception):
        return None
    verdict = quarantine_verdict(oracle, attempt, full, grace_s=grace_s)
    if isinstance(verdict, Exception):
        raise TimeHalfFault(verdict, full)
    return verdict, full


def _whole_input_note(cause: Exception, full: Exception | None) -> str | None:
    """What the whole input's apply raised, as a time-half stop's context; None when there is
    none or it matches `cause`."""
    if full is None or (type(full), str(full)) == (type(cause), str(cause)):
        return None
    return f"applying the whole input raised {type(full).__name__}: {full}"


def describe_input(attempt: Attempt) -> str:
    """An input as an operator reads it: kind, job and run named, and source."""
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
        # ss8's preconditions sit here as `expect` does: they read mutable state, and replay
        # has no live host to probe
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
