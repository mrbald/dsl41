"""The seal artifact, and the two pure functions over it.

Normative spec: `docs/period-model.md` ss3 (ss3.1 shape, ss3.2 canonical form, ss3.3 carried and
not carried, ss3.4 `next_period`, ss3.5 the execution union) and ss4 (`baseline_id` rotates per
period). Obligations PR-05b, PR-05c, PR-07's opening half, PR-08d, PR-10 through PR-14, PR-22,
PR-24a, PR-47d (DL-132).

CLOSE turns a runtime snapshot into a `Seal`; OPEN turns a sidecar into an `OpenedRuntime`. Both
are pure: no clock, no socket, no filesystem, no adapter. The seal operation (cutoff barrier ss6,
staging, the three write liturgy ss3, the `seal` record, the anchor CAS, every CLI verb) lives
above this module and calls both unchanged.

**The artifact is the model.** Every section of ss3.1 is a frozen `extra="forbid"` model whose
wire keys are the field names, so an unknown section refuses instead of dropping (DL-07).
`open_from_seal` returns models, not dicts.

**The digest covers ss3.2's canonical bytes with only the top-level `digest` key removed**
(PR-13), so no section may be named `digest`: a recursive strip would collide two documents that
differ in a nested payload key of that name.

**A `Seal` is always valid.** Every ss7 phase 3 step 6 invariant that reads the artifact alone is
a model validator, so `close` cannot write a sidecar nothing can open and a tampered file cannot
be accepted. Three checks need something the artifact does not carry: the naming record's digest,
the committed manifest, and an execution's CMD-or-FW job type (C2's catalog, the loader's half).
They are `open_from_seal` parameters.

**Carried rows follow ss3.3's table, with two typed exclusions.** `SealedHost` has no
`last_contact` and its `deadman_us` is typed `None`: a leader carrying either could evict a
quarantined host before the supervisor's real kill bound, the one state that permits a double run
(PR-24a).

**`routes` is carried and has no storage.** ss3.3 makes it the fourth row, owned by
`RuntimeState` and remapped by the `host` cmd's `route` verb, neither of which exists. Every
effect is born for the one local executor, so `implicit_routes` projects one row: role = that
executor's id, revision 0. The row shape is the frozen one.
"""

from __future__ import annotations

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
    is_canonical_file,
    require_artifact_version,
    with_digest,
)
from dsl41.classify import Classification, Verdict
from dsl41.ir import CatalogIR
from dsl41.oracle_state import (
    CarriedRows,
    Event,
    GlobalRuntime,
    HostRuntime,
    HostState,
    LIVE,
    JobRuntime,
    may_outlive_run,
)
from dsl41.period import (
    CATALOG_HASH_VERSION,
    RETIRED_CATALOG_HASH_VERSIONS,
    Manifest,
    check_addresses,
    check_manifest_self_consistent,
    disagreements,
    is_hash_address,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect, is_valid_run_id


def _current_recipe(value: int) -> int:
    """A seal pins the current recipe, a stricter question than
    `period.check_catalog_hash_version`'s "may this binary read it", and is not routed through it
    (D4, DL-138). A seal is a closed estate artifact, exact-schema on its row of the evolution
    matrix (`docs/protocol-evolution.md` ss1), so a merely readable version is the wrong pin."""
    if value != CATALOG_HASH_VERSION:
        retired = RETIRED_CATALOG_HASH_VERSIONS.get(value)
        retired_clause = f" (retired by name, {retired})" if retired is not None else ""
        raise ValueError(
            f"catalog_hash_version {value}{retired_clause}: a seal pins the current"
            f" recipe ({CATALOG_HASH_VERSION}) -- an unauditable version is"
            " unauditable on both sides of the boundary (ss1.1)"
        )
    return value


_CURRENT_RECIPE = AfterValidator(_current_recipe)


def _naive_utc(value: datetime) -> datetime:
    """ss3.2: a datetime in this artifact is naive UTC.

    Refused, not converted (`canon` converts an aware value reaching the encoder): an offset means
    a foreign clock, and this model compares instants, so an aware/naive pair would raise
    `TypeError` out of a validator."""
    if value.tzinfo is not None:
        raise ValueError(f"{value.isoformat()} is not naive UTC (ss3.2)")
    return value


def _estate_relative(value: str) -> str:
    """ss3.5: a run directory is relative to the estate root.

    Refused rather than normalized: an absolute path names a directory in the old root after a
    physical roll, and `..` escapes the root the same way."""
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"run_dir {value!r} is not relative to the estate root (ss3.5)")
    return value


#: a datetime this artifact carries in its own fields; `_canon_ready` catches the foreign models a
#: seal embeds (`Event`, `Effect`, `JobRuntime`)
NaiveUtc = Annotated[datetime, AfterValidator(_naive_utc)]

# ------------------------------------------------------------ the request


class BoundaryRequest(BaseModel):
    """ss3.1's authoritative boundary input: who asked, under what id, and whether they forced it.

    Audit checks three of the four fields for equality between the sidecar and the `seal` record
    and carries them; it derives `source` and compares. `source` has one value: a live seal
    through the control socket and an offline seal from the CLI are the same kind of boundary
    (DL-138).

    "Claimed actor", not "principal": there is no authentication at this tier
    (`control-protocol.md` ss7 gap 2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["request"]
    #: the caller's, and audit compares rather than trusts it (PR-47b)
    request_id: str = Field(min_length=1)
    claimed_actor: str
    force_seal: bool


class ForcedGate(BaseModel):
    """ss3.1's gate output: null, or the retry horizon that was overridden.

    Audit re-derives all three from the closing period's committed `retry_horizon_us`, the WAL's
    last externally requested attempt with a durable decision, and T. An unnecessary
    `--force-seal` records `force_seal: true` and leaves this null (PR-30)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["retry_horizon"]
    horizon_us: int = Field(ge=0)
    observed_age_us: int = Field(ge=0)


# ------------------------------------------------------------ carried rows


class RouteRuntime(BaseModel):
    """ss3.3's fourth row: which executor a logical role resolves to.

    A route names an executor and nothing else. It carries no generation: effect birth takes
    `generation` from the host row's current value, so a stale route cannot exist, and an evicted
    host's work is the concurrency model's ss8 case (DL-189)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    executor_id: str = Field(min_length=1)
    state_rev: int = Field(default=0, ge=0)


def implicit_routes(executor_id: str) -> dict[str, RouteRuntime]:
    """Today's route table: one route whose role is the engine-wide `executor_id` itself.

    The catalog names no role yet, and a second name for one fact would be a parallel model
    (DL-91). `state_rev` is 0 because no verb that moves it exists; the `host{verb: route}` record
    and the v3 `routes` query arrive with the storage, with PR-16b's revision assertions."""
    return {executor_id: RouteRuntime(executor_id=executor_id)}


class SealedHost(BaseModel):
    """A host row as a seal carries it: ss3.3's row minus `last_contact`, with `deadman_us` null.

    Both exclusions are in the shape. A carried `last_contact` lets the new period conclude a
    quarantined host's deadman expired, and a carried deadman lets C2 restart the supervisor at
    120s while the row says 60s, permitting eviction before the real kill bound. A host with a
    null deadman is not evictable except by force, the safe direction; the deadman stays null
    until the host re-registers (PR-24a).

    `deadman_us`, not `deadman_s`: ss3.2 has no floats at any depth. The field is always present
    and null, since ss3.2 requires a typed field to be present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: HostState = "active"
    generation: int = Field(default=0, ge=0)
    deadman_us: None = None
    forced_by: str | None = None
    state_before_quarantine: HostState | None = None
    state_rev: int = Field(default=0, ge=0)

    @classmethod
    def of(cls, row: HostRuntime | SealedHost) -> SealedHost:
        """Project a live host row. Every carried field is named, so a field added to
        `HostRuntime` lands here or the ss3.3 exclusion test reds."""
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
        """The `HostRuntime` an opening engine seeds: the carried fields, with the two exclusions
        null until the host re-registers."""
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
    """One job's ss10 verdict as the seal records it: the class and the named assumption, if any
    (ss3.1).

    The wire key `class` is a Python keyword, so the field is `verdict` under an alias. `tier` and
    `changed` stay in the migration report."""

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

    `run_id` is the effect's (ss2.3), minted in the decision transaction, so an engine that dies
    between the durable effect and the spawn resumes knowing the run's identity (PR-36a)."""

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

    ss8 requires every applied CMD SPAWN to be bound or terminal before the seal commits, so
    there is no applied-but-unbound kind (PR-27)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["bound"] = "bound"
    job: str
    run_number: int = Field(ge=1)
    effect_id: str
    index: int = Field(ge=0)
    run_id: str
    executor_id: str
    generation: int = Field(ge=0)
    #: relative to the estate root (ss3.5): an absolute path would not survive the physical roll,
    #: so the sealer relativizes the absolute path the adapter records (`runner_adapters.py`)
    run_dir: Annotated[str, AfterValidator(_estate_relative)]


class FwWatch(BaseModel):
    """ss3.5: a live file-watcher run.

    No process stands behind it, so it carries no executor and no generation, only the progress
    that decides when the watch completes (PR-34). It is evidence: every field is a pure function
    of the first `watch_seq` lines of `runs/<job>.<run_number>/watch.jsonl`, so the prefix is
    named by a line count, not wall time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["fw_watch"] = "fw_watch"
    job: str
    run_number: int = Field(ge=1)
    effect_id: str
    index: int = Field(ge=0)
    run_id: str
    #: the count of durable `watch.jsonl` lines at T; at least 1, since the adapter's first
    #: durable record on dispatch is a `start` line (an undispatched watch is a `pending_spawn`)
    watch_seq: int = Field(ge=1)
    #: the last observed size, or null before the first poll
    previous_size: int | None = None
    stable_polls: int = Field(ge=0)
    #: after `start` and no poll line, `start.at`; after a poll line,
    #: `poll.at + interval` (ss3.5, asserted directly by PR-34)
    next_poll_at: NaiveUtc


#: ss3.5's discriminated lifecycle: one row shape cannot describe the states the code has, and
#: resume needs `run_id`, `executor_id`, `generation` and the spool binding
Execution = Annotated[PendingSpawn | BoundRun | FwWatch, Field(discriminator="kind")]


# ------------------------------------------------------------- the state


class SealedState(BaseModel):
    """ss3.1's `state`: every authoritative row plus the three scalars no row holds.

    The rows are the live models, since a second spelling of a carried row would be a second
    authority. Only `hosts` is projected, because ss3.3 excludes two of its fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, JobRuntime] = {}
    globals: dict[str, GlobalRuntime] = {}
    hosts: dict[str, SealedHost] = {}
    routes: dict[str, RouteRuntime] = {}
    #: ss3.2's order: `(due, token)`, never heap-array layout. The token carries the cross-job
    #: firing order of equal-time timers, which decides resource release, box cascades and who
    #: starts
    timers: tuple[tuple[NaiveUtc, int, Event], ...] = ()
    timer_seq: int = Field(default=0, ge=0)
    #: SEM-16's irreversible depletion. Keys survive their resource: a removed bucket keeps its
    #: entry and counts if a later period brings the resource back (PR-19a)
    consumed: dict[str, int] = {}
    enqueue_counter: int = Field(default=0, ge=0)
    now: NaiveUtc

    @model_validator(mode="before")
    @classmethod
    def _project_hosts(cls, data: Any) -> Any:
        """Accept live `HostRuntime` rows and project them (ss3.3), so no caller has to remember
        the two exclusions."""
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
    """ss7 phase 3 step 6: tokens unique, positive and <= `timer_seq`, in `(due, token)` order.

    Two equal `(due, token)` entries would force the heap to compare two unorderable `Event`
    objects."""
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
    """ss5: a vector is held while STARTING or RUNNING; a non-live row holds only a renewable's
    units its run's policy did not free (DL-256, `may_outlive_run`); a bucket appears at most once
    (ss3.2)."""
    for name, row in sorted(jobs.items()):
        if row.status not in LIVE and not all(map(may_outlive_run, row.reservations)):
            raise ValueError(
                f"job {name!r}: status {row.status} still holds a machine load or a"
                " reservation released on completion -- the run's end releases those,"
                " and a row that is not live keeps only a resource's unreleased units"
                " (ss5, DL-256)"
            )
        buckets = [reservation.bucket for reservation in row.reservations]
        if len(set(buckets)) != len(buckets):
            raise ValueError(
                f"job {name!r}: bucket {sorted(buckets)} appears twice in one"
                " reservation vector (ss3.2)"
            )


# ----------------------------------------------------------- the next period


class StagedNextPeriod(BaseModel):
    """ss3.4: what a client may propose -- the identity of what opens next, and nothing about
    where it opens.

    The client cannot stage `period_id` and `segment_no`: period 2 could otherwise open period 4,
    and the attestation the induction requires could never exist (PR-05c). `extra="forbid"` makes
    a staged request carrying `period_id` a refusal.

    `strict=True` (DL-168): a `seal` request's `next_period` validates against this model directly
    over the wire (`runner_control.py`'s `_seal`), and every field is int or str, so the config
    alone closes that ingress. `CommittedNextPeriod` inherits it; `commit` stays safe because
    `model_dump()` over scalar fields never produces anything strict refuses."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    catalog_hash: str
    #: pinned to the current recipe: a seal naming a version this binary cannot recompute cannot
    #: be audited (ss1.1, DL-130)
    catalog_hash_version: Annotated[int, _CURRENT_RECIPE] = CATALOG_HASH_VERSION
    source_bundle_hash: str
    runtime_hash: str
    state_machine_version: int

    @property
    def stage_digest(self) -> str:
        """sha256 over the canonical form of the staged fields (ss7).

        The engine-derived five are excluded by construction: `first_index` is attempt output, and
        a retry closing at a different index stages the same identity, so a digest that moved with
        it would quarantine the same candidate (PR-05b, PR-30d)."""
        return hash_over(self.model_dump())

    def commit(
        self,
        *,
        estate_id: str,
        closing_period_id: int,
        closes_at_index: int,
        clock_domain: str,
    ) -> CommittedNextPeriod:
        """ss3.4's engine half: the five fields only the engine may derive, each derived here
        alone."""
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
    """ss3.4: the opening this boundary commits -- the staged identity plus the five fields only
    the engine may derive.

    Both models forbid extras, so a committed form never validates as a staged one. One type would
    force `first_index` to be omitted (breaking ss3.2's every-field-present rule), null, or
    guessed (PR-08e)."""

    period_id: int = Field(ge=1)
    segment_no: int = Field(ge=1)
    baseline_id: str
    clock_domain: str
    #: `closes_at_index + 1`, unknown until the cutoff barrier has admitted every tick due at T,
    #: so a client cannot stage it (PR-05b)
    first_index: int = Field(ge=1)

    @property
    def stage_digest(self) -> str:
        """The staged half's digest over the staged fields only, so a committed form and the staged
        form it grew from agree and a retry tells the same candidate from a different one."""
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

    Derived, not minted: audit must reproduce every seal field from the opening seal, the WAL, the
    spool and the manifests, and a random uuid appears in none of them, so a mutation applied
    consistently across sidecar, record and manifests would pass. Derived from pre-boundary
    evidence it is reproducible and unique per boundary, because `stage_digest` names the only
    staged identity that can open there (PR-47d)."""
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

    The `digest` key is not a field: it is computed over the canonical bytes of everything else
    (`digest` below), stamped by `to_document` and checked by `from_payload`, since a stored
    digest would be a second authority for a pure function of the rest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_format_version: int = ARTIFACT_FORMAT_VERSION
    estate_id: str
    period_id: int = Field(ge=1)
    baseline_id: str
    catalog_hash: str
    #: pinned to the current recipe: a seal naming a version this binary cannot recompute cannot
    #: be audited (ss1.1, DL-130)
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
        try:
            check_artifact_version({"artifact_format_version": self.artifact_format_version})
        except CanonError:
            raise ValueError(
                f"artifact_format_version {self.artifact_format_version}: this binary"
                f" implements {ARTIFACT_FORMAT_VERSION} (PR-08d)"
            ) from None
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
            if not is_valid_run_id(self.baseline_id):
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
        check_addresses(
            addresses, cite="a typed artifact names no address audit cannot reproduce (ss3.2)"
        )
        try:
            canonical_bytes(self.to_payload())
        except CanonError as exc:
            # a value ss3.2 cannot write (a float smuggled through an opaque payload) refuses at
            # construction, not at the first serialization (PR-11)
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
        """The ss3.2-ready document without its `digest` key.

        Unordered collections sort by their stated key here (`reservations` by bucket, every set
        by value). These are serialization rules, not facts about the state: the live row keeps
        its acquisition order."""
        payload = self.model_dump(by_alias=True)
        payload["state"]["jobs"] = {
            name: {
                **row,
                "reservations": sorted(row["reservations"], key=lambda held: held["bucket"]),
            }
            for name, row in payload["state"]["jobs"].items()
        }
        canon = _canon_ready(payload)
        assert isinstance(canon, dict)  # narrowing only: _canon_ready(Mapping) -> dict
        return canon

    @property
    def digest(self) -> str:
        """`"sha256:" + hexdigest` over the canonical bytes minus the top-level `digest` key
        (PR-13)."""
        return digest(self.to_payload())

    def to_document(self) -> dict[str, Any]:
        """The payload with its `digest` stamped: what the sidecar file holds."""
        return with_digest(self.to_payload())

    def to_bytes(self) -> bytes:
        """ss3.2's canonical bytes of the whole sidecar.

        Revalidated on the way out: frozen stops attribute assignment, not mutation of a dict or
        list inside a field, and a digest stamped over such a mutation would be self-consistent
        and refused only by the next reader."""
        try:
            revalidated = type(self)(**{k: v for k, v in self.to_payload().items()})
        except (ValidationError, ValueError) as exc:
            raise EngineError(f"seal artifact: mutated after validation: {exc}") from exc
        return canonical_bytes(revalidated.to_document())

    @classmethod
    def from_payload(cls, payload: object) -> Seal:
        """Validate a decoded document, its digest and its canonical form.

        The digest is recomputed over the document before the model is built, so a mutation that
        also breaks the schema is reported as tamper and not as a schema error. The canonical form
        is checked second: a document with the right digest but non-ss3.2 bytes is not this
        artifact (ss3.2, PR-13).

        A seal is a closed estate artifact: an absent `artifact_format_version` refuses by field
        name before the digest is read, as the DL-157 readers do (`period.read_sentinel` and
        `boundary.py`'s four). `check_artifact_version` passes absence, so
        `require_artifact_version` is the one that requires the key (DL-227)."""
        if not isinstance(payload, Mapping):
            raise EngineError(f"seal artifact: expected an object, got {type(payload).__name__}")
        try:
            require_artifact_version(payload)
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
                " (corrupt bytes, or a seal written by a build with a different record"
                " shape -- DL-154)"
            )
        return seal

    @classmethod
    def from_bytes(cls, data: bytes | str) -> Seal:
        """ss3.2's ingress: decode (refusing duplicate keys, floats, non-scalar strings and an
        unimplemented `artifact_format_version`), validate the shape and digest, and require the
        file's own bytes to be the canonical bytes. A whitespace-padded or key-reordered copy
        carries the same digest, and accepting it would let two byte-forms of one seal
        circulate."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        try:
            document = decode(raw)
        except CanonError as exc:
            raise EngineError(f"seal artifact: {exc}") from exc
        seal = cls.from_payload(document)
        if not is_canonical_file(raw, seal.to_bytes()):
            raise EngineError(
                "seal artifact: the bytes are not the ss3.2 canonical serialization"
                " -- same digest, different file; a sidecar is one byte string (ss3.2)"
            )
        return seal


def _check_bounds(seal: Seal) -> None:
    """I2's artifact-readable half: nothing in the sidecar postdates the cutoff.

    An index above `closes_at_index` is future intent audit cannot derive, and a `start_period`
    above the closing period is a run started in a period that has not happened."""
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
    """ss7 phase 2: `now == scheduler_admitted_through == T`, all three the cutoff instant.

    A `now` past T carried a feed the barrier had closed."""
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
    """ss3.4 and ss4: everything the opening commits, re-derived from the seal itself.

    Recomputing `baseline_id` makes "derived, not minted" checkable by every reader and not only
    by `audit` (PR-47d)."""
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
    """ss3.2: `outbox_pending` and `executions` are ordered by `(index, effect_id)` (PR-14)."""
    for what, keys in (
        ("outbox_pending", [(e.index, e.effect_id) for e in seal.outbox_pending]),
        ("executions", [(x.index, x.effect_id) for x in seal.executions]),
    ):
        if keys != sorted(keys):
            raise ValueError(f"{what} is not in (index, effect_id) order (ss3.2)")


# ss3.5's one-way join: an execution entry stands behind a live row. A RUNNING or STARTING row may
# lack an entry (a `CHANGE_STATUS STARTING` overwrite produces that and is safe to carry, PR-22a);
# an entry with no live row is an execution nothing owns.
def _check_join(seal: Seal) -> None:
    """ss3.5's join, one way, over dispatchable rows.

    Every execution has a live row and agrees with it on `run_number`; every pending SPAWN has a
    `pending_spawn` counterpart; a shared `run_id` agrees between effect and entry (PR-22). The
    reverse is not required: a `CHANGE_STATUS STARTING` overwrite leaves a STARTING row with no
    intent, spool evidence or live process, and a two-way join would refuse that legal estate
    (PR-22a). A RUNNING box has no adapter, effect or entry either.

    The CMD-or-FW half needs C2's catalog and is `check_executions_dispatchable`, run on the
    resume path (DL-151)."""
    entries: dict[str, Execution] = {}
    for entry in seal.executions:
        if entry.effect_id in entries:
            raise ValueError(
                f"two execution entries for effect {entry.effect_id!r}: one effect is one"
                " attempt (ss3.5)"
            )
        entries[entry.effect_id] = entry
        row = seal.state.jobs.get(entry.job)
        if row is None or row.status not in LIVE:
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
        # a sealed effect is native (the boundary is DL-118's world): birth identity on every
        # one, an unpaired KILL included, which the entry join never sees
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
        # every shared field agrees exactly, nulls included: an entry saying `relay-2` where the
        # effect says `local` is two owners for one attempt, and a None-skipping comparison would
        # bless an identity-less effect under an identified entry
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
        # the join's other direction (ss3.5): an undelivered intent is its pending_spawn entry,
        # so an entry without an effect would open a period holding a run nothing dispatches
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
    """A pydantic failure as one line naming each field and what it said."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


def _canon_ready(value: object, path: str = "$") -> Any:
    """ss3.2's two shape rules a pydantic dump does not apply: a set sorts by value, and a
    datetime is naive UTC.

    An aware datetime is refused on `_naive_utc`'s reason; this is that rule's backstop for the
    embedded models this module does not own."""
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

    Pure and deterministic: the same inputs give the same object and bytes, so phase 2 can build a
    candidate, validate it, and write exactly what it validated.

    The closing period's identity (`period_id`, `baseline_id`, `clock_domain` and the three
    hashes) comes from its committed manifest, so no caller can compose a seal that describes a
    period the manifest does not. `next_period` is derived from `staged` here, so the five engine
    fields have one producer (ss3.4).

    Two of ss3.2's three orders are applied here: `Outbox.pending` returns admission order and
    the canonical order is `(index, effect_id)`. The third is required of the caller: `timers`
    arrives from `RuntimeState.timers()`, already `(due, token)`-sorted, and sorting a heap array
    here would hide a caller handing over the raw layout ss3.2 forbids.

    Every refusal is an `EngineError`, the shell's one refusal type. The R gate is restated here
    on purpose: the model refuses an R verdict in the projected map, but two verdicts for one job
    would let a later carry overwrite an R before the model saw it. So duplicates and
    `Classification.refused` refuse on the classifier's object, not the projected map."""
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
    """What an engine needs to seed itself from a seal, validated (ss7 phase 3).

    Not an `Engine` or a `RuntimeState`: `Engine.__init__` takes a clock and adapters, calls
    `clock.now()` and seeds a host row, none of which a pure function may do. This holds the
    seal-derived half: the carried rows, the timer heap, the capacity scalars, the outbox, the
    executions, the ghost-run gate and the identity the period opens under. The catalog-derived
    half (referencers, the capacity pool, the scheduler frontier, new rows) belongs to the loader
    that holds C2.

    **The seeding order the spec pins**, for that loader:

    1. carried rows install VERBATIM, revisions included, before anything else; a "construct C2
       then overwrite" opener would move their revisions and make an operator's `expect` against
       a published revision unholdable.
    2. only new rows are seeded from the catalog afterwards (the SEM-24 initial flags, declared
       globals); a carried row keeps its C1 flags.
    3. `consumed` and `enqueue_counter` are installed, never renormalized: redefining a rank as
       `1 + max(active)` would require proving renormalisation equals genesis replay.
    4. the ghost-run gate `_dispatched` is derived state and not carried (ss3.3); resume rebuilds
       it from every row with `run_number > 0` after the replay (`runner_startup`).
    5. `last_contact` and the host deadman are not seeded: a new leader over-waits rather than
       evicting early, and the deadman stays null until the host re-registers.

    Nothing here touches an adapter, a supervisor, a socket, a clock or a filesystem."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estate_id: str
    #: what the closing boundary committed: the identity the opening segment and manifest must
    #: both agree with (PR-07)
    next_period: CommittedNextPeriod
    #: which seal this opening opened from; the opening `segment` carries it verbatim (ss2.1)
    opens_from_seal: OpensFromSeal
    #: `at` on an opening segment is T, the seal's cutoff instant, not restart wall time. With
    #: `next_period` committing every non-derived opening field, two openings of one seal are
    #: byte-identical (PR-07)
    opened_at: NaiveUtc
    #: the closing period's term; the new period's first term is this + 1 (ss2.4)
    epoch: int
    state: SealedState
    outbox_pending: tuple[Effect, ...] = ()
    executions: tuple[Execution, ...] = ()
    classification: dict[str, SealedVerdict] = {}

    @property
    def host_rows(self) -> dict[str, HostRuntime]:
        """The carried hosts as rows an engine can install, with the two ss3.3 exclusions null."""
        return {host_id: row.to_row() for host_id, row in self.state.hosts.items()}

    @property
    def carried_rows(self) -> "CarriedRows":
        """The sidecar's carried half in the interpreter's own types, one derivation (DL-137), so
        the engine, the auditor and run history open the same period with the same state. The
        translation lives at this seam because the interpreter never learns what a sidecar is."""
        state = self.state
        return CarriedRows(
            jobs=dict(state.jobs),
            globals_=dict(state.globals),
            hosts=self.host_rows,
            timers=state.timers,
            timer_seq=state.timer_seq,
            consumed=dict(state.consumed),
            enqueue_counter=state.enqueue_counter,
            period_id=self.next_period.period_id,
            now=state.now,
        )


def check_executions_dispatchable(seal: Seal, catalog: CatalogIR) -> None:
    """ss3.5's CMD-or-FW half of the join, asked where C2 is held (DL-151).

    `_check_join` refuses what the sidecar alone can refute. Which jobs may stand behind an entry
    needs the opening catalog, so a forged sidecar naming a live BOX row is refused here.

    A box has no adapter, effect or entry: it folds from its members and is outside
    `dispatchable`. A job C2 does not define is outside it too and cannot occur on a legal
    boundary, since `classify` refuses one over an executing job the opening catalog dropped
    (ss10.1's R row). Meeting one means the sidecar is not this estate's or the resume was
    launched with the wrong estate files; `check_leader_eligibility` names the second and runs
    after this, so the refusal says both.

    It is not an allow-list of type names: `job_type` is extensible and `_require_adapters` asks
    about adapters.

    It takes the seal rather than what it opens into, so it can run before the successor's
    segment is written, and one call covers the resume that opens the period and every resume
    after it."""
    for entry in seal.executions:
        job_ir = catalog.jobs.get(entry.job)
        if job_ir is None:
            raise EngineError(
                f"execution {entry.effect_id!r} names job {entry.job!r}, which this"
                " catalog does not define: check the estate files this resume was"
                " launched with -- a legal boundary never carries an execution for a"
                " job the opening catalog dropped (period-model ss3.5, ss10.1)"
            )
        if job_ir.job_type == "BOX":
            raise EngineError(
                f"execution {entry.effect_id!r} names box {entry.job!r}: a box folds"
                " from its members and has no adapter, no effect and no execution"
                " entry (period-model ss3.5)"
            )


def open_from_seal(
    seal: Seal | bytes | str | Mapping[str, Any],
    *,
    expected_digest: str,
    manifest: Manifest,
) -> OpenedRuntime:
    """One sidecar, opened into what an engine seeds itself from (ss7 phase 3).

    Pure. `seal` may be the artifact's bytes (the resume path: ss3.2 ingress, then shape, then
    digest), a decoded document, or an in-memory `Seal` (phase 2's candidate).

    `expected_digest` is the digest the naming record carries: the committed `seal` record at
    resume, or the opening `segment`'s `opens_from_seal` in a rolled root. A self-consistent
    sidecar the record does not name is refused, since a matching digest proves integrity, never
    derivation (ss11).

    `manifest` is the opening period's committed manifest; every field it shares with
    `next_period` must agree (PR-22).

    Both are required: skipping either would seed an engine from a sidecar the lineage does not
    name, or under a manifest that is not this boundary's. A caller validating a candidate with
    neither wants `Seal.from_bytes`."""
    if isinstance(seal, Seal):
        # revalidated even in memory: frozen stops attribute assignment, not mutation inside a
        # dict field, and this is the last gate before an engine seeds from the object
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
    )


def _parse(seal: bytes | str | Mapping[str, Any]) -> Seal:
    if isinstance(seal, (bytes, str)):
        return Seal.from_bytes(seal)
    return Seal.from_payload(seal)


#: the fields a committed manifest and the `next_period` naming it share, derived from the two
#: models so a field added to either is checked by default (DL-83); `runtime_profile` is the
#: manifest's alone, since the seal names the profile by its hash
_SHARED_WITH_MANIFEST: Final[tuple[str, ...]] = tuple(
    sorted(set(Manifest.model_fields) & set(CommittedNextPeriod.model_fields))
)


def _check_manifest(manifest: Manifest, opening: CommittedNextPeriod) -> None:
    disagree = [
        f"{field}: manifest {mine!r} vs next_period {theirs!r}"
        for field, mine, theirs in disagreements(manifest, opening, _SHARED_WITH_MANIFEST)
    ]
    if disagree:
        raise EngineError(
            "the committed manifest disagrees with the boundary that committed it"
            f" ({'; '.join(disagree)}): this manifest is not this seal's (PR-22)"
        )
