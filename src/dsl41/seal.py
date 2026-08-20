"""The seal artifact, and the two pure functions over it.

Normative spec: `docs/period-model.md` ss3 (ss3.1 shape, ss3.2 canonical
form, ss3.3 carried and not carried, ss3.4 `next_period`, ss3.5 the
execution union) and ss4 (`baseline_id` rotates per period). Built by
DL-132. Obligations PR-05b, PR-05c, PR-07's opening half, PR-08d, PR-10
through PR-14, PR-22, PR-24a, PR-47d.

A period ends by writing down everything the next one cannot reconstruct.
This module is the written-down form -- the typed sidecar -- plus CLOSE (a
runtime snapshot becomes a `Seal`) and OPEN (a sidecar becomes an
`OpenedRuntime`). Both are pure: no clock, no socket, no filesystem, no
adapter. The seal OPERATION -- the cutoff barrier (ss6), staging, the three
write liturgy (ss3), the `seal` record, the anchor CAS and every CLI verb
-- is the unit above this one and calls these two functions unchanged.

**The artifact is the model, and the model is the artifact.** Every section
of ss3.1 is a frozen `extra="forbid"` model, and the wire keys are the
field names, so a section this binary does not know is a refusal rather
than a silent drop (DL-07's rule, at a new ingress). What a reader gets
back from `open_from_seal` is not a dict.

**The digest is over ss3.2's canonical bytes with only the top-level
`digest` key removed** (PR-13), so no section here may be called `digest`:
a recursive strip would collide two documents that differ in a nested
payload key of that name, and this module's job is to make that
impossible to write by accident.

**A `Seal` object is always a valid seal.** Every invariant of ss7 phase 3
step 6 that reads the artifact ALONE is a model validator, not a check in
one of the two functions: an implementation with the checks in `open`
alone would let `close` write a sidecar nothing can open, and one with
them in `close` alone would accept a tampered file. The three checks that
need something the artifact does not carry -- the naming record's digest,
the committed manifest, and an execution's CMD-or-FW job type, which needs
C2's catalog -- are `open_from_seal` parameters, and the catalog half is
deliberately the loader's.

**What is carried is ss3.3's table, and the two exclusions are typed.**
`SealedHost` has no `last_contact` field at all and its `deadman_us` is
typed `None`: a leader that carried either could evict a quarantined host
before the supervisor's real kill bound, which is the one state that
permits a double run (PR-24a). The exclusions are enforced by the SHAPE,
so no writer can put them back.

**`routes` is carried and has no storage yet.** ss3.3 makes it a row like
the other three, owned by `RuntimeState` and remapped by the `host` cmd's
`route` verb; neither exists here. Today every effect is born for the one
local executor, so the honest projection is `implicit_routes` -- one row,
whose role IS that executor's id, at revision 0, because no verb that
could have moved it exists. The row shape is the frozen one, so the unit
that adds the storage and the wire verb changes the producer and not this
artifact.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    DIGEST_KEY,
    CanonError,
    canonical_bytes,
    check_artifact_version,
    decode,
    digest,
    hash_over,
    with_digest,
)
from dsl41.classify import Classification, Verdict
from dsl41.oracle_state import Event, GlobalRuntime, HostRuntime, HostState, JobRuntime
from dsl41.period import (
    CATALOG_HASH_VERSION,
    Manifest,
    check_manifest_self_consistent,
    is_hash_address,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect, is_valid_run_id


def _current_recipe(value: int) -> int:
    if value != CATALOG_HASH_VERSION:
        raise ValueError(
            f"catalog_hash_version {value}: a seal pins the current recipe"
            f" ({CATALOG_HASH_VERSION}) -- an unauditable version is unauditable"
            " on both sides of the boundary (ss1.1)"
        )
    return value


_CURRENT_RECIPE = AfterValidator(_current_recipe)


#: period 1's baseline is minted uuid4 (ss1.2); every later one is derived
_UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _naive_utc(value: datetime) -> datetime:
    """ss3.2: a datetime in this artifact is naive UTC.

    REFUSED rather than converted, which is what `canon` does for a value
    that reaches it aware. Two reasons the artifact is stricter than the
    encoder: a timestamp that arrived with an offset came from a clock this
    engine does not run on, and this model COMPARES instants -- `now`,
    `scheduler_admitted_through` and T are one value -- so an aware/naive
    pair would raise `TypeError` out of a validator instead of naming the
    field."""
    if value.tzinfo is not None:
        raise ValueError(f"{value.isoformat()} is not naive UTC (ss3.2)")
    return value


def _estate_relative(value: str) -> str:
    """ss3.5: a run directory is relative to the estate root.

    Refused rather than normalized: an absolute path is the physical
    roll's silent failure -- the sidecar imports into a new root and names
    a directory in the old one -- and `..` escapes the root the same way."""
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"run_dir {value!r} is not relative to the estate root (ss3.5)")
    return value


#: A datetime this artifact carries in its own fields. The foreign models a
#: seal embeds -- `Event`, `Effect`, `JobRuntime` -- are not this module's
#: to annotate, and `_canon_ready` is the backstop that catches them.
NaiveUtc = Annotated[datetime, AfterValidator(_naive_utc)]

#: The two job statuses an execution entry may stand behind (ss3.5's
#: one-way join). A RUNNING or STARTING row MAY lack an entry -- a
#: `CHANGE_STATUS STARTING` overwrite produces exactly that and is safe to
#: carry (PR-22a) -- but an entry with no live row is an execution nothing
#: owns.
LIVE_STATUS: Final[frozenset[str]] = frozenset({"STARTING", "RUNNING"})


# ------------------------------------------------------------ the request


class BoundaryRequest(BaseModel):
    """ss3.1's authoritative boundary input: who asked, under what id, and
    whether they forced it.

    Three of the four fields originate in the request and nowhere else, so
    audit checks them for equality between the sidecar and the `seal`
    record and carries them. `source` is the exception -- audit DERIVES it
    (ss11) and compares. Two values, not three: a live seal through the
    control socket and an offline seal from the CLI are one kind of
    boundary, a request carrying an id its caller minted.

    "Claimed actor", not "principal": there is no authentication at this
    tier (`control-protocol.md` ss7 gap 2), and the seal must not spell an
    unauthenticated claim as if it were one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["request", "adopt"]
    #: for an adoption this is derived -- `sha256("adopt" || estate_id)` --
    #: and audit re-derives it (PR-47b); for a request it is the caller's
    request_id: str = Field(min_length=1)
    claimed_actor: str
    force_seal: bool


class ForcedGate(BaseModel):
    """ss3.1's gate OUTPUT: null, or the retry horizon that was overridden.

    Audit re-derives all three from the CLOSING period's committed
    `retry_horizon_us`, the WAL's last externally requested attempt with a
    durable decision, and T -- so an unnecessary `--force-seal` records
    `force_seal: true` in the request and leaves this null (PR-30)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["retry_horizon"]
    horizon_us: int = Field(ge=0)
    observed_age_us: int = Field(ge=0)


# ------------------------------------------------------------ carried rows


class RouteRuntime(BaseModel):
    """ss3.3's fourth row: which executor a logical role resolves to.

    **A route names an executor and nothing else.** It carries no
    generation -- at effect birth `executor_id` comes from the route and
    `generation` from the host row's CURRENT value, so a stale route
    cannot exist and an evicted host's work is the concurrency model's
    ss8 case, not a route state (`ha-deployment.md` ss4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor_id: str = Field(min_length=1)
    state_rev: int = Field(default=0, ge=0)


def implicit_routes(executor_id: str) -> dict[str, RouteRuntime]:
    """Today's route table, projected from what exists.

    The catalog names no role yet and `plan_effects` takes one engine-wide
    `executor_id`, so there is exactly one route and its role is that
    executor's own id -- inventing a second name for one fact would be the
    parallel model DL-91 exists to catch. `state_rev` is 0 because no verb
    that could move it exists: the `host{verb: route}` record and the v3
    `routes` query arrive with the storage, and PR-16b's revision
    assertions arrive with them."""
    return {executor_id: RouteRuntime(executor_id=executor_id)}


class SealedHost(BaseModel):
    """A host row as a seal carries it: ss3.3's row MINUS `last_contact`
    and with `deadman_us` null.

    Both exclusions are in the SHAPE rather than in a writer. A carried
    `last_contact` lets the new period conclude a quarantined host's
    deadman already expired -- the one state that permits a double run --
    and a carried deadman lets C2 restart the supervisor at 120s while the
    row still says 60s and permit eviction 60s before the real kill bound.
    The row's deadman is null until the host re-registers in the new
    period, and a host with a null deadman is not evictable except by
    force, which is the safe direction (PR-24a).

    `deadman_us`, not `deadman_s`: ss3.2's grammar has no floats at any
    depth, and microseconds are what `RuntimeProfile` already stores. The
    field stays in the schema, always present and always null, because
    ss3.2 requires a typed field to be present rather than absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: HostState = "active"
    generation: int = Field(default=0, ge=0)
    deadman_us: None = None
    forced_by: str | None = None
    state_before_quarantine: HostState | None = None
    state_rev: int = Field(default=0, ge=0)

    @classmethod
    def of(cls, row: HostRuntime | SealedHost) -> SealedHost:
        """Project a live host row. Every field the seal carries by name,
        so a field added to `HostRuntime` later lands here or the ss3.3
        exclusion test reds."""
        if isinstance(row, SealedHost):
            return row
        return cls(
            state=row.state,
            generation=row.generation,
            forced_by=row.forced_by,
            state_before_quarantine=row.state_before_quarantine,
            state_rev=row.state_rev,
        )

    def to_row(self) -> HostRuntime:
        """The `HostRuntime` an opening engine seeds: the carried fields,
        with the two exclusions null until the host re-registers and the
        new leader has its own contact."""
        return HostRuntime(
            state=self.state,
            generation=self.generation,
            deadman_s=None,
            last_contact=None,
            forced_by=self.forced_by,
            state_before_quarantine=self.state_before_quarantine,
            state_rev=self.state_rev,
        )


class SealedVerdict(BaseModel):
    """One job's ss10 verdict as the seal records it: the class, and the
    named assumption when there is one (ss3.1).

    The wire key is `class`, which is a Python keyword, so the field is
    `verdict` under an alias -- the same two fields `JobVerdict` carries,
    projected. `tier` and `changed` stay in the migration report: they are
    why, and the seal records what."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    verdict: Verdict = Field(
        validation_alias=AliasChoices("class", "verdict"), serialization_alias="class"
    )
    assumption: str | None = None

    @model_validator(mode="after")
    def _assumption_iff_assumed(self) -> SealedVerdict:
        if (self.assumption is not None) != (self.verdict == "A"):
            raise ValueError(
                f"class {self.verdict!r} with assumption {self.assumption!r}:"
                " an A records its sentence and nothing else carries one (ss3.1)"
            )
        return self


# -------------------------------------------------------------- executions


class PendingSpawn(BaseModel):
    """ss3.5: a SPAWN recorded and not delivered.

    `run_id` is the EFFECT's (ss2.3), never an adapter's: the id is minted
    in the decision transaction, so an engine that dies between the durable
    effect and the spawn resumes knowing which identity the run would have
    had (PR-36a)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["pending_spawn"] = "pending_spawn"
    job: str
    run_number: int = Field(ge=1)
    effect_id: str
    index: int = Field(ge=0)
    run_id: str
    executor_id: str
    generation: int = Field(ge=0)


class BoundRun(BaseModel):
    """ss3.5: a SPAWN applied and the spool binding known.

    ss8 requires every applied CMD SPAWN to be bound or terminal before the
    seal commits, so there is no applied-but-unbound kind: it is
    milliseconds and the sealer waits (PR-27)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["bound"] = "bound"
    job: str
    run_number: int = Field(ge=1)
    effect_id: str
    index: int = Field(ge=0)
    run_id: str
    executor_id: str
    generation: int = Field(ge=0)
    #: RELATIVE to the estate root (ss3.5): an absolute path would not
    #: survive the physical roll that imports this sidecar into a new root,
    #: and the rule is enforced rather than stated -- the adapter that
    #: records a run directory today records an absolute one
    #: (`runner_adapters.py`), so the sealer must relativize it
    run_dir: Annotated[str, AfterValidator(_estate_relative)]


class FwWatch(BaseModel):
    """ss3.5: a live file-watcher run.

    No process stands behind it, so it carries no executor and no
    generation -- what it carries is the progress that decides when the
    watch completes, which a restart would otherwise reset (PR-34). And it
    is EVIDENCE, not memory: every field here is a pure function of the
    first `watch_seq` lines of `runs/<job>.<run_number>/watch.jsonl`, which
    is why the prefix is named by a line COUNT and not by wall time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["fw_watch"] = "fw_watch"
    job: str
    run_number: int = Field(ge=1)
    effect_id: str
    index: int = Field(ge=0)
    run_id: str
    #: the count of durable `watch.jsonl` lines at T. At least 1: the
    #: adapter's first durable act on dispatch is a `start` line, and a
    #: watch not yet dispatched is a `pending_spawn` rather than this
    watch_seq: int = Field(ge=1)
    #: the last observed size, or null before the first poll
    previous_size: int | None = None
    stable_polls: int = Field(ge=0)
    #: after `start` and no poll line, `start.at`; after a poll line,
    #: `poll.at + interval` (ss3.5, asserted directly by PR-34)
    next_poll_at: NaiveUtc


#: ss3.5's discriminated lifecycle. One row shape does not describe the
#: states the code has: a seal that carried only the RUNNING row lost
#: `run_id`, `executor_id`, `generation` and the spool binding, and resume
#: could not say which executor owned the run.
Execution = Annotated[PendingSpawn | BoundRun | FwWatch, Field(discriminator="kind")]


# ------------------------------------------------------------- the state


class SealedState(BaseModel):
    """ss3.1's `state`: every authoritative row plus the three scalars no
    row holds.

    The rows are the live models, not copies of three of their fields: a
    second spelling of a carried row is a second authority for what the
    state IS. Only `hosts` is projected, because ss3.3 excludes two of its
    fields by name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, JobRuntime] = {}
    globals: dict[str, GlobalRuntime] = {}
    hosts: dict[str, SealedHost] = {}
    routes: dict[str, RouteRuntime] = {}
    #: ss3.2's order: `(due, token)`, never heap-array layout. The token
    #: carries the CROSS-JOB firing order of equal-time timers, which
    #: decides resource release, box cascades and who starts
    timers: tuple[tuple[NaiveUtc, int, Event], ...] = ()
    timer_seq: int = Field(default=0, ge=0)
    #: SEM-16's irreversible depletion. Keys survive their resource: a
    #: bucket C2 removes keeps its entry and still counts if a later
    #: period brings the resource back (PR-19a)
    consumed: dict[str, int] = {}
    enqueue_counter: int = Field(default=0, ge=0)
    now: NaiveUtc

    @model_validator(mode="before")
    @classmethod
    def _project_hosts(cls, data: Any) -> Any:
        """Accept live `HostRuntime` rows and project them (ss3.3). The
        caller holds `store.hosts`; making it convert first would be one
        more place the two exclusions could be forgotten."""
        if isinstance(data, Mapping) and isinstance(data.get("hosts"), Mapping):
            hosts = {
                host_id: SealedHost.of(row) if isinstance(row, (HostRuntime, SealedHost)) else row
                for host_id, row in data["hosts"].items()
            }
            data = {**data, "hosts": hosts}
        return data

    @model_validator(mode="after")
    def _load_invariants(self) -> SealedState:
        _check_timers(self.timers, self.timer_seq)
        _check_waiters(self.jobs, self.enqueue_counter)
        _check_reservations(self.jobs)
        for bucket, units in sorted(self.consumed.items()):
            if units < 0:
                raise ValueError(
                    f"consumed[{bucket!r}] is {units}: a seal that opened with invented"
                    " capacity is worse than one that refuses (ss5)"
                )
        for role, route in sorted(self.routes.items()):
            if route.executor_id not in self.hosts:
                raise ValueError(
                    f"route {role!r} names executor {route.executor_id!r}, which has no"
                    " host row: an effect born for it could never dispatch (ss7)"
                )
        return self


def _check_timers(timers: tuple[tuple[datetime, int, Event], ...], timer_seq: int) -> None:
    """ss7 phase 3 step 6: tokens unique, positive and <= `timer_seq`, in
    `(due, token)` order.

    Uniqueness is not tidiness: two equal `(due, token)` entries force the
    heap to compare two `Event` objects, which are not orderable, so the
    first equal-time pair would raise inside `heapq` at an arbitrary later
    moment."""
    seen: set[int] = set()
    for due, token, _event in timers:
        if token <= 0:
            raise ValueError(f"timer token {token} at {due.isoformat()}: tokens are positive")
        if token > timer_seq:
            raise ValueError(
                f"timer token {token} is above timer_seq {timer_seq}: the allocator"
                " cannot have issued it"
            )
        if token in seen:
            raise ValueError(
                f"timer token {token} appears twice: two equal heap keys make the"
                " firing order undefined"
            )
        seen.add(token)
    order = [(due, token) for due, token, _ in timers]
    if order != sorted(order):
        raise ValueError("timers are not in (due, token) order (ss3.2)")


def _check_waiters(jobs: Mapping[str, JobRuntime], enqueue_counter: int) -> None:
    """ss5: `waiter_seq` non-null iff QUE_WAIT, unique, positive, and no
    higher than the allocator that issued it."""
    ranks: dict[int, str] = {}
    for name, row in sorted(jobs.items()):
        if (row.waiter_seq is not None) != (row.status == "QUE_WAIT"):
            raise ValueError(
                f"job {name!r}: status {row.status} with waiter_seq {row.waiter_seq!r} --"
                " a rank is held exactly while QUE_WAIT (ss5)"
            )
        if row.waiter_seq is None:
            continue
        if row.waiter_seq <= 0:
            raise ValueError(f"job {name!r}: waiter_seq {row.waiter_seq} is not positive")
        if row.waiter_seq > enqueue_counter:
            raise ValueError(
                f"job {name!r}: waiter_seq {row.waiter_seq} is above enqueue_counter"
                f" {enqueue_counter} -- the allocator cannot have issued it (ss5)"
            )
        if row.waiter_seq in ranks:
            raise ValueError(
                f"jobs {ranks[row.waiter_seq]!r} and {name!r} share waiter_seq"
                f" {row.waiter_seq}: admission order would not survive the boundary (PR-21)"
            )
        ranks[row.waiter_seq] = name


def _check_reservations(jobs: Mapping[str, JobRuntime]) -> None:
    """ss5: a vector is held only while STARTING or RUNNING, and one bucket
    appears at most once in it (ss3.2's "after duplicate-bucket
    rejection")."""
    for name, row in sorted(jobs.items()):
        if row.reservations and row.status not in LIVE_STATUS:
            raise ValueError(
                f"job {name!r}: status {row.status} still holds"
                f" {len(row.reservations)} reservation(s) -- a terminal transition"
                " releases the vector and moves what it kept into consumed (ss5)"
            )
        buckets = [reservation.bucket for reservation in row.reservations]
        if len(set(buckets)) != len(buckets):
            raise ValueError(
                f"job {name!r}: bucket {sorted(buckets)} appears twice in one"
                " reservation vector (ss3.2)"
            )


# ----------------------------------------------------------- the next period


class StagedNextPeriod(BaseModel):
    """ss3.4: what a CLIENT may propose -- the identity of WHAT opens next,
    and nothing about WHERE it opens.

    Draft 17 let the client stage `period_id` and `segment_no`, so period 2
    could open period 4 and the attestation the induction requires could
    never exist -- an unauditable lineage by construction (PR-05c). The
    line between this model and the committed one is who may say it, and
    `extra="forbid"` is what makes a staged request carrying `period_id` a
    refusal instead of a field somebody later trusted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    catalog_hash: str
    #: pinned to the CURRENT recipe: a seal naming a version this binary
    #: cannot recompute is one audit cannot audit (ss1.1, DL-130's rule)
    catalog_hash_version: Annotated[int, _CURRENT_RECIPE] = CATALOG_HASH_VERSION
    source_bundle_hash: str
    runtime_hash: str
    state_machine_version: int

    @property
    def stage_digest(self) -> str:
        """sha256 over the canonical form of the STAGED fields (ss7).

        The engine-derived five are excluded by construction rather than by
        a filter: `first_index` is attempt output -- a retry after a crash
        closes at a different index and stages the same identity -- and a
        digest that moved with it would quarantine a candidate that is the
        same candidate (PR-05b, PR-30d)."""
        return hash_over(self.model_dump())

    def commit(
        self,
        *,
        estate_id: str,
        closing_period_id: int,
        closes_at_index: int,
        clock_domain: str,
    ) -> CommittedNextPeriod:
        """ss3.4's engine half: the five fields only the engine may derive.

        Each is derived here and nowhere else, so no caller can compose a
        different opinion about which period opens next."""
        period_id = closing_period_id + 1
        return CommittedNextPeriod(
            **self.model_dump(),
            period_id=period_id,
            segment_no=period_id,
            baseline_id=baseline_id_for(
                estate_id=estate_id, period_id=period_id, stage_digest=self.stage_digest
            ),
            clock_domain=clock_domain,
            first_index=closes_at_index + 1,
        )


class CommittedNextPeriod(StagedNextPeriod):
    """ss3.4: the opening this boundary commits -- the staged identity plus
    the five fields only the engine may derive.

    Subclassing IS "the staged fields plus", and because both models forbid
    extras a committed form never validates as a staged one -- which is
    what keeps a reader from accepting the wrong half. One type would force
    `first_index` to be omitted (breaking ss3.2's every-field-present
    rule), null (not what excluded means) or guessed (PR-08e)."""

    period_id: int = Field(ge=1)
    segment_no: int = Field(ge=1)
    baseline_id: str
    clock_domain: str
    #: `closes_at_index + 1`, and unknown until the cutoff barrier has
    #: admitted every tick due at T: a client that staged it before the
    #: barrier ran could name an index a cutoff tick then took (PR-05b)
    first_index: int = Field(ge=1)

    @property
    def stage_digest(self) -> str:
        """The staged half's digest, computed over the staged fields only
        -- so a committed form and the staged form it grew from agree, and
        a retry can tell "the same candidate" from "a different one"."""
        staged = {name: getattr(self, name) for name in StagedNextPeriod.model_fields}
        return hash_over(staged)

    @model_validator(mode="after")
    def _identity_is_derived(self) -> CommittedNextPeriod:
        if self.segment_no != self.period_id:
            raise ValueError(
                f"segment_no {self.segment_no} != period_id {self.period_id}: every later"
                " segment opens a period, so the two are one number (ss3.4)"
            )
        return self


def baseline_id_for(*, estate_id: str, period_id: int, stage_digest: str) -> str:
    """ss3.4/ss4: `sha256(canonical{estate_id, period_id, stage_digest})`.

    **Derived, not minted.** Audit must reproduce every seal field from the
    opening seal, the WAL, the spool and the manifests, and a random uuid
    appears in none of them -- a wrong audit could only copy the value out
    of the seal it is auditing and check its shape, so a mutation applied
    consistently across the sidecar, the record and the manifests would
    pass. Derived from pre-boundary evidence it is reproducible and still
    unique per boundary, because `stage_digest` names the only staged
    identity that can open there (PR-47d)."""
    return hash_over({"estate_id": estate_id, "period_id": period_id, "stage_digest": stage_digest})


# ---------------------------------------------------------------- the seal


class OpensFromSeal(BaseModel):
    """ss2.1's `opens_from_seal`: which seal the opening segment opened
    from. Null on segment 1 and non-null on every later segment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    period_id: int = Field(ge=1)
    digest: str


class Seal(BaseModel):
    """ss3.1's sidecar: `seals/<period_id>.json`, in full.

    The `digest` key is NOT a field. It is computed over the canonical
    bytes of everything else (`digest` below), stamped on the way out
    (`to_document`) and checked on the way in (`from_payload`) -- a stored
    digest would be a second authority for a value that is a pure function
    of the rest, and one an artifact could then disagree with itself
    about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    estate_id: str
    period_id: int = Field(ge=1)
    baseline_id: str
    catalog_hash: str
    #: pinned to the CURRENT recipe: a seal naming a version this binary
    #: cannot recompute is one audit cannot audit (ss1.1, DL-130's rule)
    catalog_hash_version: Annotated[int, _CURRENT_RECIPE] = CATALOG_HASH_VERSION
    source_bundle_hash: str
    runtime_hash: str
    state_machine_version: int
    closes_at_index: int = Field(ge=0)
    #: T -- the cutoff instant, and the `at` of the opening segment
    closed_at: NaiveUtc
    clock_domain: str
    epoch: int = Field(ge=1)
    #: null only on the first seal of an estate's lineage
    prev_seal_digest: str | None = None
    scheduler_admitted_through: NaiveUtc
    boundary_request: BoundaryRequest
    request_fingerprint: str
    forced_gate: ForcedGate | None = None
    state: SealedState
    #: intents recorded and not delivered, in ss3.2's `(index, effect_id)`
    #: order -- so a SPAWN precedes its run's later KILL (PR-14)
    outbox_pending: tuple[Effect, ...] = ()
    executions: tuple[Execution, ...] = ()
    classification: dict[str, SealedVerdict] = {}
    next_period: CommittedNextPeriod

    # ------------------------------------------------------------ validation

    @model_validator(mode="after")
    def _artifact_invariants(self) -> Seal:
        if self.artifact_format_version != ARTIFACT_FORMAT_VERSION:
            raise ValueError(
                f"artifact_format_version {self.artifact_format_version}: this binary"
                f" implements {ARTIFACT_FORMAT_VERSION} (PR-08d)"
            )
        _check_cutoff(self)
        _check_opening(self)
        _check_order(self)
        _check_join(self)
        _check_bounds(self)
        if (self.prev_seal_digest is None) != (self.period_id == 1):
            raise ValueError(
                f"period {self.period_id} with prev_seal_digest"
                f" {self.prev_seal_digest!r}: null terminates a lineage, and only"
                " period 1 has no predecessor to name (ss11)"
            )
        if self.forced_gate is not None:
            # gate OUTPUT is populated only on the one truth-table row that
            # commits through the gate: age < horizon AND force_seal (ss3.1)
            if not self.boundary_request.force_seal:
                raise ValueError(
                    "forced_gate is populated but boundary_request.force_seal is"
                    " false: the gate engages only under an explicit force (ss3.1)"
                )
            if self.forced_gate.observed_age_us >= self.forced_gate.horizon_us:
                raise ValueError(
                    f"forced_gate records observed_age_us"
                    f" {self.forced_gate.observed_age_us} >= horizon_us"
                    f" {self.forced_gate.horizon_us}: a passing gate is null,"
                    " whatever force_seal says (ss3.1)"
                )
        if self.period_id == 1:
            if not _UUID_RE.fullmatch(self.baseline_id):
                raise ValueError(
                    f"baseline_id {self.baseline_id!r}: period 1's baseline is the"
                    " minted uuid4 (ss1.2)"
                )
        elif not is_hash_address(self.baseline_id):
            raise ValueError(
                f"baseline_id {self.baseline_id!r}: a later period's baseline is"
                " DERIVED -- a sha256 address, never free text (ss4, PR-47d)"
            )
        addresses = {
            "catalog_hash": self.catalog_hash,
            "source_bundle_hash": self.source_bundle_hash,
            "runtime_hash": self.runtime_hash,
            "request_fingerprint": self.request_fingerprint,
            "next_period.catalog_hash": self.next_period.catalog_hash,
            "next_period.source_bundle_hash": self.next_period.source_bundle_hash,
            "next_period.runtime_hash": self.next_period.runtime_hash,
            "next_period.baseline_id": self.next_period.baseline_id,
        }
        if self.prev_seal_digest is not None:
            addresses["prev_seal_digest"] = self.prev_seal_digest
        for field_name, value in addresses.items():
            if not is_hash_address(value):
                raise ValueError(
                    f"{field_name} {value!r} is not a sha256 address -- a typed"
                    " artifact names no address audit cannot reproduce (ss3.2)"
                )
        try:
            canonical_bytes(self.to_payload())
        except CanonError as exc:
            # a value ss3.2 cannot write -- a float smuggled through an
            # opaque payload -- must refuse at CONSTRUCTION, not at the
            # first serialization three calls later (PR-11)
            raise ValueError(f"the seal is not ss3.2-serializable: {exc}") from None
        for job, verdict in sorted(self.classification.items()):
            if verdict.verdict == "R":
                raise ValueError(
                    f"job {job!r} is classified R: the boundary refuses until the run is"
                    " done or killed, so a committed seal cannot carry one (ss10.1)"
                )
        return self

    # ------------------------------------------------------- canonical form

    def to_payload(self) -> dict[str, Any]:
        """The ss3.2-ready document WITHOUT its `digest` key.

        Two normalizations happen here rather than in the models, because
        both are serialization rules and not facts about the state: an
        unordered collection sorts by its stated key (`reservations` by
        bucket, every set by value), and the row keeps the order it was
        acquired in."""
        payload = self.model_dump(by_alias=True)
        payload["state"]["jobs"] = {
            name: {
                **row,
                "reservations": sorted(row["reservations"], key=lambda held: held["bucket"]),
            }
            for name, row in payload["state"]["jobs"].items()
        }
        return _canon_ready(payload)

    @property
    def digest(self) -> str:
        """`"sha256:" + hexdigest` over the canonical bytes with only the
        top-level `digest` key removed (PR-13)."""
        return digest(self.to_payload())

    def to_document(self) -> dict[str, Any]:
        """The payload with its `digest` stamped -- what the sidecar file
        holds."""
        return with_digest(self.to_payload())

    def to_bytes(self) -> bytes:
        """ss3.2's canonical bytes of the whole sidecar.

        REVALIDATED on the way out: frozen stops attribute assignment, not
        mutation of a dict or list inside a field, and a writer that
        stamped a digest over a post-validation mutation would emit a
        self-consistent artifact only the NEXT reader refuses. The cost is
        one validation per serialization; serialization happens once per
        boundary."""
        try:
            revalidated = type(self)(**{k: v for k, v in self.to_payload().items()})
        except (ValidationError, ValueError) as exc:
            raise EngineError(f"seal artifact: mutated after validation: {exc}") from exc
        return canonical_bytes(revalidated.to_document())

    @classmethod
    def from_payload(cls, payload: object) -> Seal:
        """Validate a decoded document, its digest and its canonical form.

        The digest is recomputed over the DOCUMENT first, before the model
        is built: a mutation that also breaks the schema -- an added key,
        a retyped field -- must still be reported as what it is, and a
        reader that validated first would name the schema and never reach
        the tamper. The second check is the canonical form: a document
        whose own digest is right but whose bytes are not ss3.2's is not
        this artifact either (ss3.2, PR-13)."""
        if not isinstance(payload, Mapping):
            raise EngineError(f"seal artifact: expected an object, got {type(payload).__name__}")
        try:
            check_artifact_version(payload)
        except CanonError as exc:
            raise EngineError(f"seal artifact: {exc}") from exc
        stamped = payload.get(DIGEST_KEY)
        if not isinstance(stamped, str):
            raise EngineError(
                f"seal artifact: no top-level {DIGEST_KEY!r} -- an artifact that carries"
                " no digest proves nothing about itself (ss3.2)"
            )
        try:
            recomputed = digest(payload)
        except CanonError as exc:
            raise EngineError(f"seal artifact: {exc}") from exc
        if recomputed != stamped:
            raise EngineError(
                f"seal artifact: digest {stamped} but the document hashes to {recomputed}"
                " -- the document is not what it says it is (ss11)"
            )
        body = {key: value for key, value in payload.items() if key != DIGEST_KEY}
        try:
            seal = cls(**body)
        except ValidationError as exc:
            raise EngineError(f"seal artifact: {_why(exc)}") from exc
        if seal.digest != stamped:
            raise EngineError(
                f"seal artifact: digest {stamped} over a document whose canonical form"
                f" hashes to {seal.digest} -- the bytes are not in ss3.2 canonical form"
            )
        return seal

    @classmethod
    def from_bytes(cls, data: bytes | str) -> Seal:
        """ss3.2's ingress: decode -- refusing duplicate keys, floats,
        non-scalar strings and an unimplemented `artifact_format_version`
        -- then validate the shape and the digest, and require the FILE'S
        OWN BYTES to be the canonical bytes: the digest is computed over
        the canonical form, so a whitespace-padded or key-reordered copy
        carries the SAME digest as the real artifact, and accepting it
        would let two byte-forms of one seal circulate under one name."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        try:
            document = decode(raw)
        except CanonError as exc:
            raise EngineError(f"seal artifact: {exc}") from exc
        seal = cls.from_payload(document)
        expected = seal.to_bytes()
        if raw != expected:
            raise EngineError(
                "seal artifact: the bytes are not the ss3.2 canonical serialization"
                " -- same digest, different file; a sidecar is one byte string (ss3.2)"
            )
        return seal


def _check_bounds(seal: Seal) -> None:
    """I2's artifact-readable half: nothing in the sidecar postdates the
    cutoff. An index above `closes_at_index` is future intent audit cannot
    derive from this period's WAL, and a `start_period` above the closing
    period is a run started in a period that has not happened."""
    for effect in seal.outbox_pending:
        if not 1 <= effect.index <= seal.closes_at_index:
            raise ValueError(
                f"pending effect {effect.effect_id!r} at index {effect.index} outside"
                f" [1, {seal.closes_at_index}]: no WAL position derives it (I2)"
            )
    for entry in seal.executions:
        if not 1 <= entry.index <= seal.closes_at_index:
            raise ValueError(
                f"execution {entry.effect_id!r} at index {entry.index} outside"
                f" [1, {seal.closes_at_index}]: no WAL position derives it (I2)"
            )
    for name, row in sorted(seal.state.jobs.items()):
        if row.run_number < 0:
            raise ValueError(
                f"row {name!r} at run_number {row.run_number}: run numbers count"
                " from zero and never go back (I2)"
            )
        if row.start_period > seal.period_id or row.start_period < 1:
            raise ValueError(
                f"row {name!r} started in period {row.start_period} but the seal"
                f" closes period {seal.period_id}: a run cannot start in a period"
                " that has not happened (I2)"
            )


def _check_cutoff(seal: Seal) -> None:
    """ss7 phase 2: `now == scheduler_admitted_through == T`.

    All three are the cutoff instant. A seal whose `now` ran past T carried
    a feed the barrier had already closed."""
    if seal.state.now != seal.closed_at:
        raise ValueError(
            f"state.now {seal.state.now.isoformat()} != closed_at"
            f" {seal.closed_at.isoformat()}: both are T (ss7)"
        )
    if seal.scheduler_admitted_through != seal.closed_at:
        raise ValueError(
            f"scheduler_admitted_through {seal.scheduler_admitted_through.isoformat()} !="
            f" closed_at {seal.closed_at.isoformat()}: both are T (ss7)"
        )


def _check_opening(seal: Seal) -> None:
    """ss3.4 and ss4: everything the opening commits, re-derived from the
    seal itself.

    `baseline_id` is the load-bearing one. Recomputing it here is what
    makes "derived, not minted" checkable by every reader of the artifact
    and not only by `audit` (PR-47d)."""
    opening = seal.next_period
    if opening.artifact_format_version != ARTIFACT_FORMAT_VERSION:
        raise ValueError(
            f"next_period.artifact_format_version {opening.artifact_format_version}: this"
            f" binary implements {ARTIFACT_FORMAT_VERSION}, so it could not open the"
            " period this boundary commits (ss8, PR-08d)"
        )
    if opening.period_id != seal.period_id + 1:
        raise ValueError(
            f"next_period.period_id {opening.period_id} does not follow {seal.period_id}:"
            " a boundary opens the next period and no other (PR-05c)"
        )
    if opening.first_index != seal.closes_at_index + 1:
        raise ValueError(
            f"next_period.first_index {opening.first_index} != closes_at_index"
            f" {seal.closes_at_index} + 1: a reused index makes every cursor and every"
            " decision lookup ambiguous (PR-05b)"
        )
    if opening.clock_domain != seal.clock_domain:
        raise ValueError(
            f"next_period.clock_domain {opening.clock_domain!r} != {seal.clock_domain!r}:"
            " a domain change is refused at the boundary as resume refuses it (PR-05c)"
        )
    if opening.state_machine_version != seal.state_machine_version:
        raise ValueError(
            f"next_period.state_machine_version {opening.state_machine_version} !="
            f" {seal.state_machine_version}: one executable implements one version, so an"
            " SM bump is a drain and a new estate (ss2.1, PR-17)"
        )
    derived = baseline_id_for(
        estate_id=seal.estate_id,
        period_id=opening.period_id,
        stage_digest=opening.stage_digest,
    )
    if opening.baseline_id != derived:
        raise ValueError(
            f"next_period.baseline_id {opening.baseline_id} is not"
            f" {derived} -- derived from {{estate_id, period_id, stage_digest}}, never"
            " minted (PR-47d)"
        )


def _check_order(seal: Seal) -> None:
    """ss3.2: `outbox_pending` and `executions` are ordered by
    `(index, effect_id)` (PR-14)."""
    for what, keys in (
        ("outbox_pending", [(e.index, e.effect_id) for e in seal.outbox_pending]),
        ("executions", [(x.index, x.effect_id) for x in seal.executions]),
    ):
        if keys != sorted(keys):
            raise ValueError(f"{what} is not in (index, effect_id) order (ss3.2)")


def _check_join(seal: Seal) -> None:
    """ss3.5's join, ONE WAY, over dispatchable rows.

    Every execution has a live row and agrees with it on `run_number`;
    every pending SPAWN has a `pending_spawn` counterpart; a shared
    `run_id` agrees between the effect and the entry (PR-22). The reverse
    is deliberately not required: a `CHANGE_STATUS STARTING` overwrite
    leaves a STARTING row with no intent, no spool evidence and no live
    process, and a two-way join would refuse that legal estate (PR-22a). A
    RUNNING BOX has no adapter, no effect and no entry either.

    The CMD-or-FW half of ss3.5's sentence needs C2's catalog to know a
    job's type, so it belongs to the loader that holds one -- this
    function refuses what the artifact alone can refute."""
    entries: dict[str, Execution] = {}
    for entry in seal.executions:
        if entry.effect_id in entries:
            raise ValueError(
                f"two execution entries for effect {entry.effect_id!r}: one effect is one"
                " attempt (ss3.5)"
            )
        entries[entry.effect_id] = entry
        row = seal.state.jobs.get(entry.job)
        if row is None or row.status not in LIVE_STATUS:
            status = "no row" if row is None else row.status
            raise ValueError(
                f"execution {entry.effect_id!r} names job {entry.job!r} with {status}:"
                " an execution entry stands behind a STARTING or RUNNING row (ss3.5)"
            )
        if row.run_number != entry.run_number:
            raise ValueError(
                f"execution {entry.effect_id!r} is run {entry.run_number} but"
                f" {entry.job!r} is at run {row.run_number}: the row and the entry"
                " disagree about which run is live (PR-22)"
            )
        if not is_valid_run_id(entry.run_id):
            raise ValueError(
                f"execution {entry.effect_id!r}: run_id {entry.run_id!r} is outside the"
                " ss11a grammar"
            )
    pending_spawns: dict[str, Effect] = {}
    effect_ids: set[str] = set()
    for effect in seal.outbox_pending:
        if effect.effect_id in effect_ids:
            raise ValueError(
                f"two pending effects under {effect.effect_id!r}: one effect is one"
                " intent, and an Outbox seeded from this would refuse the second"
            )
        effect_ids.add(effect.effect_id)
        # a sealed effect is NATIVE by definition (the boundary is DL-118's
        # world): birth identity on every one -- an unpaired KILL included,
        # which the entry join never sees
        if effect.run_number < 1:
            raise ValueError(
                f"pending effect {effect.effect_id!r} names run {effect.run_number}:"
                " an intended act names a real run (I2)"
            )
        if effect.generation is None or effect.generation < 0:
            raise ValueError(
                f"pending effect {effect.effect_id!r} carries generation"
                f" {effect.generation!r}: a sealed effect is native and binds a real"
                " identity at birth -- no host row produces a negative one (DL-118)"
            )
        if effect.kind == "SPAWN" and effect.run_id is None:
            raise ValueError(f"pending SPAWN {effect.effect_id!r} carries no run_id (DL-118)")
        if effect.run_id is not None and not is_valid_run_id(effect.run_id):
            raise ValueError(
                f"pending effect {effect.effect_id!r}: run_id {effect.run_id!r} is"
                " outside the ss11a grammar"
            )
        held = entries.get(effect.effect_id)
        if effect.kind == "SPAWN":
            pending_spawns[effect.effect_id] = effect
            if held is None or held.kind != "pending_spawn":
                found = "nothing" if held is None else held.kind
                raise ValueError(
                    f"pending SPAWN {effect.effect_id!r} has {found} in executions:"
                    " an undelivered SPAWN is a pending_spawn (ss3.5)"
                )
        if held is None:
            continue
        # every shared field agrees EXACTLY, nulls included: an entry that
        # says `relay-2` where the effect says `local` is two owners for
        # one attempt, and an identity-less effect under an identified
        # entry is an intent that cannot dispatch under the identity it
        # claims -- a None-skipping comparison would bless both
        for field in ("job", "run_number", "run_id", "executor_id", "generation", "index"):
            if not hasattr(held, field):
                continue  # an fw entry has no executor half
            effect_value = getattr(effect, field, None)
            held_value = getattr(held, field)
            if effect_value != held_value:
                raise ValueError(
                    f"effect {effect.effect_id!r}: {field} {effect_value!r} but its"
                    f" execution says {held_value!r} -- the effect and the entry"
                    " describe one attempt (PR-22)"
                )
    for entry in seal.executions:
        # the join's OTHER direction (ss3.5): an undelivered intent IS its
        # pending_spawn entry, so an entry with no effect behind it would
        # open a period holding a run nothing intends to dispatch
        if entry.kind == "pending_spawn" and entry.effect_id not in pending_spawns:
            raise ValueError(
                f"pending_spawn {entry.effect_id!r} has no pending SPAWN effect in"
                " outbox_pending: the entry without its intent dispatches nothing (ss3.5)"
            )
    # one (job, run_number) <-> one run_id, both directions, across every
    # entry and every id-bearing pending effect (ss11a's map, at the artifact)
    run_of_id: dict[str, tuple[str, int]] = {}
    id_of_run: dict[tuple[str, int], str] = {}
    claims = [(entry.job, entry.run_number, entry.run_id) for entry in seal.executions] + [
        (effect.job, effect.run_number, effect.run_id)
        for effect in seal.outbox_pending
        if effect.run_id is not None
    ]
    for job, run_number, run_id in claims:
        run = (job, run_number)
        if run_of_id.get(run_id, run) != run:
            raise ValueError(
                f"run_id {run_id!r} names both {run_of_id[run_id]} and {run}: one"
                " identity, one run (ss11a)"
            )
        if id_of_run.get(run, run_id) != run_id:
            raise ValueError(
                f"run {job}.{run_number} is bound to both {id_of_run[run]!r} and"
                f" {run_id!r}: one run, one identity (ss11a)"
            )
        run_of_id[run_id] = run
        id_of_run[run] = run_id


def _why(exc: ValidationError) -> str:
    """A pydantic failure as one line naming each field and what it said --
    "the seal is invalid" is not actionable, which field is."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


def _canon_ready(value: object, path: str = "$") -> Any:
    """ss3.2's two shape rules that a pydantic dump does not apply: a set
    is an unordered collection and sorts by value, and a datetime is naive
    UTC.

    An aware datetime is REFUSED rather than converted, on `_naive_utc`'s
    reason -- this is that rule's backstop, for the embedded models this
    module does not own."""
    if isinstance(value, Mapping):
        return {key: _canon_ready(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        try:
            items = sorted(value)
        except TypeError as exc:
            raise EngineError(f"{path}: a set of mixed types has no canonical order") from exc
        return [_canon_ready(item, f"{path}[]") for item in items]
    if isinstance(value, (list, tuple)):
        return [_canon_ready(item, f"{path}[{position}]") for position, item in enumerate(value)]
    if isinstance(value, datetime) and value.tzinfo is not None:
        raise EngineError(f"{path}: {value.isoformat()} is not naive UTC (ss3.2)")
    return value


# --------------------------------------------------------------- close


def close_runtime(
    *,
    closing: Manifest,
    estate_id: str,
    epoch: int,
    prev_seal_digest: str | None,
    closes_at_index: int,
    closed_at: datetime,
    scheduler_admitted_through: datetime,
    state: SealedState,
    outbox_pending: Iterable[Effect] = (),
    executions: Iterable[Execution] = (),
    classification: Classification,
    staged: StagedNextPeriod,
    boundary_request: BoundaryRequest,
    request_fingerprint: str,
    forced_gate: ForcedGate | None = None,
) -> Seal:
    """One runtime, closed into one sidecar (ss3).

    Pure and deterministic: the same inputs give the same object and the
    same bytes, which is what lets phase 2 build a candidate, validate it,
    and write exactly what it validated.

    The closing period's identity comes from its COMMITTED MANIFEST rather
    than from parameters beside it -- `period_id`, `baseline_id`,
    `clock_domain` and the three hashes are the manifest's, so no caller
    can compose a seal that describes a period the manifest does not.
    `next_period` is derived from `staged` here, so the five engine fields
    have exactly one producer (ss3.4).

    Two of ss3.2's three orders are applied here rather than asked of the
    caller: `Outbox.pending` returns admission order and the canonical
    order is `(index, effect_id)`, which agrees with it per run and is
    total across runs. The third is required, not applied -- `timers`
    arrives from `RuntimeState.timers()`, which is already
    `(due, token)`-sorted, and sorting a heap array here would hide a
    caller that handed over the raw layout ss3.2 forbids.

    Every refusal here is an `EngineError`, the shell's one refusal type --
    a caller that had to catch two would eventually catch one. The R gate
    IS re-stated here, deliberately: the model refuses an R verdict in the
    projected MAP, but the projection is a dict build -- two verdicts for
    one job would let a later carry OVERWRITE an R before the model ever
    saw it. So duplicates refuse, and `Classification.refused` refuses,
    both on the object the classifier produced rather than on the map
    projected from it."""
    if classification.refused:
        raise EngineError(
            f"the classification refuses the boundary ({', '.join(classification.refused)}):"
            " a sidecar is never built over live changed work (ss10.1)"
        )
    seen_jobs: set[str] = set()
    for verdict in classification.verdicts:
        if verdict.job in seen_jobs:
            raise EngineError(
                f"two verdicts for job {verdict.job!r}: a projection would let the"
                " second silently overwrite the first (ss10)"
            )
        seen_jobs.add(verdict.job)
    try:
        return Seal(
            estate_id=estate_id,
            period_id=closing.period_id,
            baseline_id=closing.baseline_id,
            catalog_hash=closing.catalog_hash,
            catalog_hash_version=closing.catalog_hash_version,
            source_bundle_hash=closing.source_bundle_hash,
            runtime_hash=closing.runtime_hash,
            state_machine_version=closing.state_machine_version,
            closes_at_index=closes_at_index,
            closed_at=closed_at,
            clock_domain=closing.clock_domain,
            epoch=epoch,
            prev_seal_digest=prev_seal_digest,
            scheduler_admitted_through=scheduler_admitted_through,
            boundary_request=boundary_request,
            request_fingerprint=request_fingerprint,
            forced_gate=forced_gate,
            state=state,
            outbox_pending=tuple(sorted(outbox_pending, key=lambda e: (e.index, e.effect_id))),
            executions=tuple(sorted(executions, key=lambda x: (x.index, x.effect_id))),
            classification={
                verdict.job: SealedVerdict(verdict=verdict.verdict, assumption=verdict.assumption)
                for verdict in classification.verdicts
            },
            next_period=staged.commit(
                estate_id=estate_id,
                closing_period_id=closing.period_id,
                closes_at_index=closes_at_index,
                clock_domain=closing.clock_domain,
            ),
        )
    except ValidationError as exc:
        raise EngineError(f"seal artifact: {_why(exc)}") from exc


# ---------------------------------------------------------------- open


class OpenedRuntime(BaseModel):
    """What an engine needs to seed itself from a seal, validated (ss7
    phase 3).

    Not an `Engine`, and not a `RuntimeState`: `Engine.__init__` takes a
    clock and adapters, calls `clock.now()` and seeds a host row, none of
    which a pure function may do. This holds the SEAL-DERIVED half -- the
    carried rows, the timer heap, the capacity scalars, the outbox, the
    executions, the ghost-run gate and the identity the period opens under.
    The catalog-derived half (referencers, the capacity pool, the scheduler
    frontier, and genuinely new rows) belongs to the loader that holds C2.

    **The seeding order the spec pins**, for that loader:

    1. carried rows install VERBATIM -- revisions included -- before
       anything else. A "construct C2 then overwrite" opener seeds carried
       entities first and moves their revisions, and an operator's `expect`
       against a revision the seal published would then be unholdable.
    2. only genuinely new rows are seeded from the catalog afterwards (the
       SEM-24 initial flags, declared globals). A carried row keeps its C1
       flags: genesis seeding applies to new rows only.
    3. `consumed` and `enqueue_counter` are installed, never renormalized:
       redefining a rank as `1 + max(active)` would buy one integer in
       exchange for proving that renormalisation equals genesis replay.
    4. `dispatched` below is rebuilt, not carried -- it is derived state,
       and its reconstruction is normative (ss3.3).
    5. `last_contact` and the host deadman are NOT seeded: a new leader
       over-waits rather than evicting early, and the row's deadman stays
       null until the host re-registers in the new period.

    Nothing here touches an adapter, a supervisor, a socket, a clock or a
    filesystem."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estate_id: str
    #: what the closing boundary committed: the identity the opening
    #: segment and the opening manifest must both agree with (PR-07)
    next_period: CommittedNextPeriod
    #: which seal this opening opened from -- the opening `segment`
    #: carries it verbatim (ss2.1), so the writer above recomputes nothing
    opens_from_seal: OpensFromSeal
    #: `at` on an opening segment IS T -- the seal's cutoff instant, not
    #: restart wall time. That, plus `next_period` committing every
    #: non-derived opening field, is what makes two openings of one seal
    #: byte-identical (PR-07)
    opened_at: NaiveUtc
    #: the closing period's term. The estate-monotone epoch makes the new
    #: period's first term this + 1 (ss2.4)
    epoch: int
    state: SealedState
    outbox_pending: tuple[Effect, ...] = ()
    executions: tuple[Execution, ...] = ()
    classification: dict[str, SealedVerdict] = {}
    #: ss3.3's ghost-run gate: `{job: run_number}` for every row that has
    #: run. NOT a cache -- `plan_effects` plans a SPAWN only when
    #: `run_number > dispatched[job]`, so an opener that left it empty
    #: would let a legal `CHANGE_STATUS STARTING` on a job that completed
    #: run 7 plan run 7 again (PR-18a)
    dispatched: dict[str, int] = {}

    @property
    def host_rows(self) -> dict[str, HostRuntime]:
        """The carried hosts as rows an engine can install, with the two
        ss3.3 exclusions null."""
        return {host_id: row.to_row() for host_id, row in self.state.hosts.items()}


def open_from_seal(
    seal: Seal | bytes | str | Mapping[str, Any],
    *,
    expected_digest: str,
    manifest: Manifest,
) -> OpenedRuntime:
    """One sidecar, opened into what an engine seeds itself from (ss7
    phase 3).

    Pure. `seal` may be the artifact's bytes (the resume path: ss3.2
    ingress, then the shape, then the digest), a decoded document, or an
    in-memory `Seal` (phase 2's candidate, already validated by
    construction).

    `expected_digest` is the digest the NAMING RECORD carries -- the
    committed `seal` record at resume, or the opening `segment`'s
    `opens_from_seal` in a rolled root. A sidecar that is self-consistent
    and is not the one the record names is refused: a matching digest
    proves integrity, never derivation (ss11).

    `manifest` is the OPENING period's committed manifest. Every field it
    shares with `next_period` must agree -- the committed manifest is the
    engine's own output and a disagreement means it is not this
    boundary's (PR-22).

    BOTH facts are REQUIRED, not optional: an opening that skipped either
    would seed an engine from a self-consistent sidecar that is not the
    one the lineage names, or under a manifest that is not this
    boundary's. A caller validating a candidate with neither fact wants
    `Seal.from_bytes`, which is that API."""
    if isinstance(seal, Seal):
        # revalidated even in memory: frozen stops attribute assignment,
        # not mutation inside a dict field, and this is the LAST gate
        # before an engine seeds itself from the object
        try:
            opened = Seal(**seal.to_payload())
        except ValidationError as exc:
            raise EngineError(f"seal artifact: mutated after validation: {_why(exc)}") from exc
    else:
        opened = _parse(seal)
    if opened.digest != expected_digest:
        raise EngineError(
            f"seal artifact: digest {opened.digest} but the record naming it says"
            f" {expected_digest} -- an orphan or a stranger's sidecar (ss11)"
        )
    check_manifest_self_consistent(manifest, "open_from_seal")
    _check_manifest(manifest, opened.next_period)
    return OpenedRuntime(
        estate_id=opened.estate_id,
        next_period=opened.next_period,
        opens_from_seal=OpensFromSeal(period_id=opened.period_id, digest=opened.digest),
        opened_at=opened.closed_at,
        epoch=opened.epoch,
        state=opened.state,
        outbox_pending=opened.outbox_pending,
        executions=opened.executions,
        classification=dict(opened.classification),
        dispatched={
            job: row.run_number for job, row in opened.state.jobs.items() if row.run_number > 0
        },
    )


def _parse(seal: bytes | str | Mapping[str, Any]) -> Seal:
    if isinstance(seal, (bytes, str)):
        return Seal.from_bytes(seal)
    return Seal.from_payload(seal)


#: The fields a committed manifest and the `next_period` that names it
#: share -- DERIVED from the two models, so a field added to either is
#: checked by default rather than by somebody remembering to list it
#: (the DL-83 discipline). `runtime_profile` is the manifest's alone: the
#: seal names the profile by its hash.
_SHARED_WITH_MANIFEST: Final[tuple[str, ...]] = tuple(
    sorted(set(Manifest.model_fields) & set(CommittedNextPeriod.model_fields))
)


def _check_manifest(manifest: Manifest, opening: CommittedNextPeriod) -> None:
    disagreements = [
        f"{field}: manifest {getattr(manifest, field)!r} vs next_period {getattr(opening, field)!r}"
        for field in _SHARED_WITH_MANIFEST
        if getattr(manifest, field) != getattr(opening, field)
    ]
    if disagreements:
        raise EngineError(
            "the committed manifest disagrees with the boundary that committed it"
            f" ({'; '.join(disagreements)}): this manifest is not this seal's (PR-22)"
        )
