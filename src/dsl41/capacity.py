"""The DL-50 capacity subsystem: sized buckets and the QUE_WAIT queue.

Extracted from `oracle.py` by DL-88 along the line DL-74 already drew: the
pool decides who may be admitted and in what order; every status transition
and event emission that decision implies stays on the Oracle. Nothing here
knows about statuses, events or time -- which is why it could move at all,
and why it is the piece to move first when the interpreter needs room.

DL-120 finished the move. The pool holds NO mutable state: its buckets are
sized from the catalog and everything else is passed in. Usage is

    used[bucket] = consumed[bucket] + sum(units reserved by the live rows)

with the held half on `JobRuntime.reservations` and the spent half in
`RuntimeState.consumed`. The old `_bucket_used` added those two together, and
a sum of a transient and an irreversible fact is a number no seal can rebuild:
recomputing it from the holders alone refilled every depletable (period-model
ss5). The waiter queue went the same way -- a rank is `JobRuntime.waiter_seq`,
so admission ORDER is reconstructible from the rows rather than from an
in-memory list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from dsl41.oracle_state import CapacityReservation, ReleasePolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from dsl41.ir import CatalogIR, JobIR, ResourceIR, ResourceRef
    from dsl41.oracle_state import JobRuntime

#: Sorts a waiter whose priority nothing declares -- and one whose job the
#: catalog no longer has at all -- behind every declared priority.
_UNSET_PRIORITY = 1 << 31

#: What one requirement DOES to a bucket: 'acquire' holds units until the
#: release policy gives them back, 'gate' only checks a level and holds
#: nothing. Named (DL-209) so the pair is a code object, not two strings
#: spelled in four places.
DemandMode = Literal["acquire", "gate"]

#: The demand vector's entry shape: (bucket key, units, mode, release policy).
DemandEntry = tuple[str, int, DemandMode, ReleasePolicy | None]

#: DL-50's resource types, upper-cased: R renewable, D depletable, T
#: threshold. Absent ("") reads as renewable; anything else has unknown
#: release semantics and `runner_preflight` refuses the run over it. Named
#: here because this module owns what each one MEANS (DL-209).
RES_TYPES = frozenset({"R", "D", "T"})

#: DL-50's per-request FREE overrides, upper-cased: Y release on SUCCESS
#: only, N never release, A release on any terminal. A code outside the
#: mapping is a lowering error (`ir._parse_resources`), so `release_policy`
#: falls back to the res_type default for anything else (DL-209).
_FREE_POLICY: dict[str, ReleasePolicy] = {"Y": "success", "N": "never", "A": "completion"}
#: The domain the register's `free_code` surface enumerates, derived from the
#: mapping so a fourth code needs one edit, not two (DL-75 review 2026-09-19).
FREE_CODES = frozenset(_FREE_POLICY)

#: The two members of `RES_TYPES` this module branches on by name: T is a
#: check-only threshold, D is the depletable whose default is never-release.
#: R and "" take every other branch, so neither needs a name of its own.
_THRESHOLD = "T"
_DEPLETABLE = "D"

#: The key prefix of a machine-load bucket; `r:` prefixes a resource.
_MACHINE = "m:"


class CapacityPool:
    """The DL-50 capacity subsystem of one Oracle: the sized buckets (machine
    max_load, resource amounts), the demand each start makes on them, and the
    admission ORDER of the QUE_WAIT queue. The pool decides who may be
    admitted and in what order; every status transition and event emission
    the decision implies stays on the Oracle (DL-74).

    Since DL-120 it is a pure function of (catalog, rows, consumed): the
    caller passes the state, the pool answers. Nothing here can be out of step
    with the rows, because there is nothing here to be out of step."""

    def __init__(self, catalog: CatalogIR) -> None:
        self.catalog = catalog
        # DL-50 resource/load buckets: capacity per contended entity, seeded
        # from the catalog (malformed -> skipped; preflight refuses the run).
        #: bucket key -> capacity. `m:<machine>` = max_load, `r:<name>` = amount.
        self._bucket_cap: dict[str, int] = {}
        for mname, machine in catalog.machines.items():
            cap = _safe_units(machine.max_load_units)
            if cap is not None:
                self._bucket_cap[f"{_MACHINE}{mname}"] = cap
        for rname, resource in catalog.resources.items():
            cap = _safe_units(resource.capacity_units)
            if cap is not None:
                self._bucket_cap[f"r:{rname}"] = cap

    def demand_vector(self, job_ir: JobIR) -> list[DemandEntry]:
        """The full (bucket_key, units, mode, release_policy) demand of a start.
        mode is 'acquire' (holds units) or 'gate' (threshold: check-only, never
        holds). Only buckets the oracle can size appear -- an unsized resource
        or an absent max_load contributes nothing here (preflight refuses the
        former for execution; the latter is AutoSys's unlimited-load default).

        One entry per bucket: the groups a job states on ONE resource are
        folded by `job_demand`, which the explore page calls for the same
        job (DL-193). A machine bucket is stated once and needs no fold.

        DL-247: the machine entry is here whatever the priority, because the
        load is held whatever the priority: "even when jobs have a priority
        of 0, AutoSys Workload Automation tracks job loads on each machine".
        Whether the entry is CHECKED is the Oracle's call (`checks_load`)."""
        vector: list[DemandEntry] = []
        load_entry = self._machine_entry(job_ir)
        if load_entry is not None:
            vector.append(load_entry)
        groups: dict[str, list[ResourceRef]] = {}
        for ref in job_ir.resources:
            groups.setdefault(ref.name, []).append(ref)
        for name, refs in groups.items():
            key = f"r:{name}"
            if key not in self._bucket_cap:
                continue  # unsized -> not modelled here (preflight refuses run)
            units, mode, policy = job_demand(resource_type(self.catalog.resources.get(name)), refs)
            vector.append((key, units, mode, policy))
        return vector

    def _machine_key(self, job_ir: JobIR) -> str | None:
        """The load bucket of the job's machine, or None when the machine is
        unset or has no max_load (AutoSys's unlimited default)."""
        spec = job_ir.exec_
        if spec is None or spec.machine is None:
            return None
        key = f"{_MACHINE}{spec.machine}"
        return key if key in self._bucket_cap else None

    def _machine_entry(self, job_ir: JobIR) -> DemandEntry | None:
        """The machine-load entry of a start, or None when it takes no load:
        no sized machine, or no `job_load` (Qr4: absent is 0)."""
        key = self._machine_key(job_ir)
        if key is None:
            return None
        load = _safe_units(job_ir.job_load_units) or 0
        if load <= 0:
            return None
        return (key, load, "acquire", "completion")

    def used(self, rows: Mapping[str, JobRuntime], consumed: Mapping[str, int]) -> dict[str, int]:
        """Units unavailable per bucket: those PERMANENTLY spent plus those
        held by live runs (DL-120). One pass over the rows, so the two facts
        stay separate right up to the addition that needs them together.

        A `consumed` key the catalog no longer sizes is kept and still counts:
        a period that drops a resource must not refund what an earlier one
        burned, and a later period that brings the resource back must not find
        the quota full again (PR-19a, period-model ss3.3)."""
        used = dict(consumed)
        for row in rows.values():
            for reservation in row.reservations:
                used[reservation.bucket] = used.get(reservation.bucket, 0) + reservation.units
        return used

    def can_admit(
        self,
        vector: list[DemandEntry],
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
    ) -> bool:
        """True iff every bucket has room for its demand (gate and acquire share
        the same free>=units test; keys are guaranteed sized)."""
        if not vector:
            return True
        used = self.used(rows, consumed)
        return all(used.get(key, 0) + units <= self._bucket_cap[key] for key, units, _, _ in vector)

    def load_blocked(
        self,
        job_ir: JobIR,
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
        counts: Callable[[str], bool],
    ) -> bool:
        """DL-247: True when a queued job of strictly higher priority waits for
        load on this job's machine. The vendor rule: "A job in the QUE_WAIT
        state for one machine attribute value automatically blocks all the
        lower priority jobs that specify the same machine attribute value.
        It does not automatically block higher or equal priority jobs ... or
        a job that specifies a different machine attribute value."

        The blocked job needs a positive priority and a sized machine; it
        need not state a `job_load`, since priority is itself a
        load-balancing attribute. A priority-0 or unset job is never blocked.
        The blocker is a waiter that checks load (positive priority), loads
        the same machine, and whose own load does not fit now; one whose load
        fits waits on a named resource, and the vendor says such a job does
        not block on the machine. `counts` says which waiters the caller
        counts as queued: the Oracle leaves out a held waiter and one whose
        box no longer runs."""
        priority = job_priority(job_ir)
        key = self._machine_key(job_ir)
        if priority <= 0 or key is None:
            return False
        used = self.used(rows, consumed).get(key, 0)
        for name, row in rows.items():
            other = self.catalog.jobs.get(name)
            if row.waiter_seq is None or name == job_ir.name or other is None:
                continue
            other_entry = self._machine_entry(other)
            if other_entry is None or other_entry[0] != key:
                continue
            if not 0 < job_priority(other) < priority:
                continue
            if used + other_entry[1] <= self._bucket_cap[key]:
                continue
            if counts(name):
                return True
        return False

    def has_load_waiters(self, rows: Mapping[str, JobRuntime]) -> bool:
        """True when a queued job checks machine load on a sized machine:
        the only kind of job a priority block can hold (DL-247)."""
        for name, row in rows.items():
            job_ir = self.catalog.jobs.get(name)
            if row.waiter_seq is None or job_ir is None:
                continue
            if checks_load(job_ir) and self._machine_key(job_ir) is not None:
                return True
        return False

    @staticmethod
    def holds(row: JobRuntime) -> bool:
        """True while this row still holds units. The Oracle releases on the
        edge that LEAVES STARTING/RUNNING (period-model ss5) -- terminal for
        every ordinary run -- before it wakes anything (the release-before-wake
        gate)."""
        return bool(row.reservations)

    def sorted_waiters(self, rows: Mapping[str, JobRuntime]) -> list[str]:
        """The QUE_WAIT jobs in admission order, read off the rows."""

        def key(item: tuple[str, int]) -> tuple[int, int, str]:
            job, seq = item
            # PENDING: Qr2 -- an unset priority sorts last, behind every
            # declared one, explicit 0 included, though the vendor default is
            # 0. Lower number first is documented (DL-247); enqueue-seq then
            # name make the order total.
            #
            # A waiter the catalog does not have takes that same "unset"
            # priority rather than raising KeyError. period-model ss10
            # classifies QUE_WAIT-and-removed R, so an operator is refused the
            # boundary long before this runs; the default is the floor under
            # that gate, so a classifier bug is a misordered queue and not a
            # crash in the admission loop (period-model ss5).
            job_ir = self.catalog.jobs.get(job)
            prio = _safe_units(job_ir.priority_value) if job_ir is not None else None
            return (prio if prio is not None else _UNSET_PRIORITY, seq, job)

        waiting = [(job, row.waiter_seq) for job, row in rows.items() if row.waiter_seq is not None]
        return [job for job, _ in sorted(waiting, key=key)]


def without_machine_load(vector: list[DemandEntry]) -> list[DemandEntry]:
    """The vector less its machine-load entry: what a start that skips the
    load check tests (DL-247). The start still reserves the whole vector."""
    return [entry for entry in vector if not entry[0].startswith(_MACHINE)]


def job_priority(job_ir: JobIR) -> int:
    """A job's priority as load queueing reads it: the declared number, or 0
    when unset or malformed, the vendor default (DL-247). A negative number is
    outside the vendor's range; it compares as itself and checks no load."""
    value = _safe_units(job_ir.priority_value)
    return value if value is not None else 0


def checks_load(job_ir: JobIR) -> bool:
    """DL-247: whether a start tests its machine load. Only a positive
    priority does: "The scheduler ignores any load unit values defined for
    the job or machine when the job has a priority value of zero". The load
    of a job that skips the test is still held (`demand_vector`)."""
    return job_priority(job_ir) > 0


def to_reservations(vector: list[DemandEntry]) -> tuple[CapacityReservation, ...]:
    """The acquiring half of a demand vector, as the rows record it. A `gate`
    entry holds nothing, so it reserves nothing -- the threshold was tested at
    admission and is not owed a release."""
    held = []
    for key, units, mode, policy in vector:
        if mode != "acquire":
            continue
        assert policy is not None  # demand_vector gives every acquire entry one
        held.append(CapacityReservation(bucket=key, units=units, release_policy=policy))
    return tuple(held)


def _safe_units(accessor: object) -> int | None:
    """Call a typed-int IR accessor (job_load_units/max_load_units/...),
    swallowing a malformed-value ValueError to None. The oracle models only
    what parses; a malformed value is preflight's loud refusal (DL-50), not the
    oracle's crash -- oracle-direct over an unrefused catalog simply skips it."""
    assert callable(accessor)
    try:
        value = accessor()
    except ValueError:
        return None
    assert value is None or isinstance(value, int)
    return value


def resource_type(resource: ResourceIR | None) -> str:
    """A resource's `res_type` as every reader compares it: stripped and
    upper-cased, "" when the resource is undeclared or states none."""
    return (resource.res_type or "").strip().upper() if resource is not None else ""


def requirement_demand(res_type: str, free: str | None) -> tuple[DemandMode, ReleasePolicy | None]:
    """What one `resources:` group DEMANDS: the mode and, when it holds units,
    the policy that gives them back.

    'gate' is a threshold (SEM `res_type: T`): a level check that holds
    nothing and so releases nothing, which is why it has no policy.
    'acquire' holds `QUANTITY` units until `release_policy` says otherwise.

    PUBLIC because the explore page states the same demand in words
    (DL-192), and the T branch is half the rule: reusing `release_policy`
    alone reported a threshold gate as held units released on completion."""
    if res_type == _THRESHOLD:
        return "gate", None
    return "acquire", release_policy(res_type, free)


def job_demand(
    res_type: str, refs: Sequence[ResourceRef]
) -> tuple[int, DemandMode, ReleasePolicy | None]:
    """One job's WHOLE demand on one resource: the groups it states there,
    classified by `requirement_demand` and coalesced by `merge_requirements`.

    The fold has one owner because it has two readers (DL-193): the pool
    reserves what it returns and the explore page draws and words it, and a
    job that lists one resource twice must not read as two single-unit
    demands on the page while the pool holds their sum. `refs` is never
    empty -- both callers group by a name a ref stated."""
    total: tuple[int, DemandMode, ReleasePolicy | None] | None = None
    for ref in refs:
        mode, policy = requirement_demand(res_type, ref.free)
        entry: tuple[int, DemandMode, ReleasePolicy | None] = (ref.quantity, mode, policy)
        total = entry if total is None else merge_requirements(total, entry)
    assert total is not None
    return total


def merge_requirements(
    left: tuple[int, DemandMode, ReleasePolicy | None],
    right: tuple[int, DemandMode, ReleasePolicy | None],
) -> tuple[int, DemandMode, ReleasePolicy | None]:
    """Coalesce two requirements on ONE bucket -- a job listing the same
    resource twice. The demand SUMS (two `(LOCK, QUANTITY=2)` groups want
    four units, not two), a bucket any group holds is held, and the release
    policy merges to the most restrictive so asymmetric FREE never frees
    early. PUBLIC with `requirement_demand`: the page draws one link and one
    row per (job, resource) and must agree with what the pool reserves."""
    units, mode, policy = left
    other_units, other_mode, other_policy = right
    return (
        units + other_units,
        "acquire" if "acquire" in (mode, other_mode) else "gate",
        _merge_policy(policy, other_policy),
    )


def release_policy(res_type: str, free: str | None) -> ReleasePolicy:
    """DL-50: per-request release policy. FREE overrides the res_type default.
    Returns 'completion' (release on any terminal), 'success' (only on SUCCESS),
    or 'never'. res_type is upper-cased; '' (absent) reads as renewable.

    PUBLIC because the explore page states the same policy per lock member
    (DL-192), and a second copy of this table would drift from the pool's
    (DL-72). One owner, two readers."""
    # FREE absent, or a code the mapping does not define -> res_type default
    default: ReleasePolicy = "never" if res_type == _DEPLETABLE else "completion"
    return _FREE_POLICY.get(free or "", default)


def _merge_policy(a: ReleasePolicy | None, b: ReleasePolicy | None) -> ReleasePolicy | None:
    """Coalesce two release policies for one bucket (duplicate resource refs) to
    the MOST RESTRICTIVE, so asymmetric FREE never frees early (DL-50 review)."""
    if a is None:
        return b
    if b is None:
        return a
    rank = {"never": 0, "success": 1, "completion": 2}
    return a if rank[a] <= rank[b] else b
