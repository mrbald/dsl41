"""The DL-50 capacity subsystem: sized buckets and the QUE_WAIT queue (DL-74, DL-88).

The pool decides who may be admitted and in what order; every status transition and event
emission that implies stays on the Oracle. Nothing here knows statuses, events or time.

The pool holds no mutable state: its buckets are sized from the catalog and everything else
is passed in (DL-120). Usage is

    used[bucket] = consumed[bucket] + sum(units reserved by the rows)

with the held half on `JobRuntime.reservations` and the spent half in
`RuntimeState.consumed`. The rows that reserve are the live runs and a job that still holds
a renewable's units its run did not free (DL-256). A held and a spent fact are never summed
into one stored number, because no seal can rebuild that sum (period-model ss5). A waiter's
rank is `JobRuntime.waiter_seq`, so admission order is rebuilt from the rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from dsl41.oracle_state import CapacityReservation, ReleasePolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from dsl41.ir import CatalogIR, JobIR, ResourceIR, ResourceRef
    from dsl41.oracle_state import JobRuntime
    from dsl41.semantics import RenewableFree, SemanticSwitches

#: Sorts a waiter with no declared priority, or whose job the catalog lacks, behind every
#: declared priority.
_UNSET_PRIORITY = 1 << 31

#: What one requirement does to a bucket: 'acquire' holds units until the release policy
#: gives them back, 'gate' only checks a level and holds nothing (DL-209).
DemandMode = Literal["acquire", "gate"]

#: The demand vector's entry shape: (bucket key, units, mode, release policy).
DemandEntry = tuple[str, int, DemandMode, ReleasePolicy | None]

#: DL-50's resource types, upper-cased: R renewable, D depletable, T threshold. Absent ("")
#: reads as renewable; any other value has unknown release semantics and `runner_preflight`
#: refuses the run over it. This module owns what each one means (DL-209).
RES_TYPES = frozenset({"R", "D", "T"})

#: DL-50's per-request FREE overrides, upper-cased: Y release on SUCCESS only, N never, A on
#: any terminal. A code outside the mapping is a lowering error (`ir._parse_resources`), so
#: `release_policy` falls back to the res_type default (DL-209). On a renewable the units a
#: policy does not release stay held by the job until RELEASE_RESOURCE or its next run
#: (DL-256); on a depletable they are spent.
_FREE_POLICY: dict[str, ReleasePolicy] = {"Y": "success", "N": "never", "A": "completion"}
#: The domain the register's `free_code` surface enumerates, derived from the mapping (DL-75).
FREE_CODES = frozenset(_FREE_POLICY)

#: The two `RES_TYPES` members this module branches on: T is a check-only threshold, D is the
#: depletable whose default is never-release. R and "" take every other branch.
_THRESHOLD = "T"
_DEPLETABLE = "D"

#: The key prefix of a machine-load bucket; `r:` prefixes a resource.
_MACHINE = "m:"


class CapacityPool:
    """The DL-50 capacity subsystem of one Oracle (DL-74).

    It holds the sized buckets (machine max_load, resource amounts), the demand each start
    makes on them, and the admission order of the QUE_WAIT queue. It is a pure function of
    (catalog, rows, consumed): the caller passes the state (DL-120)."""

    def __init__(self, catalog: CatalogIR, semantics: SemanticSwitches | None = None) -> None:
        self.catalog = catalog
        # imported here: `semantics` reads `resource_type` from this module
        from dsl41.semantics import DEFAULTS

        #: the `renewable-free` switch: what an omitted FREE means on a renewable (DL-256);
        #: no profile gives the registry default.
        self.renewable_free: RenewableFree = (semantics or DEFAULTS).renewable_free
        # DL-50 buckets: capacity per contended entity, seeded from the catalog; a malformed
        # value is skipped and preflight refuses the run
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
        """The (bucket_key, units, mode, release_policy) demand of a start.

        mode is 'acquire' (holds units) or 'gate' (threshold: check-only). Only buckets the
        oracle can size appear: an unsized resource (preflight refuses it) or an absent
        max_load (AutoSys's unlimited default) adds nothing. One entry per bucket;
        `job_demand` folds the groups a job states on one resource, and the explore page
        calls it too (DL-193).

        The machine entry is present whatever the priority, because the load is held whatever
        the priority (DL-247); the Oracle decides whether it is checked (`checks_load`)."""
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
                continue  # unsized: preflight refuses the run
            res_type = resource_type(self.catalog.resources.get(name))
            units, mode, policy = job_demand(res_type, refs, self.renewable_free)
            vector.append((key, units, mode, policy))
        return vector

    def keeps_held(self, bucket: str) -> bool:
        """DL-256: whether the units a run's policy does not release stay held by the job.

        A renewable holds them ("N -- The units are not freed", "resources Attribute",
        AutoSys 24.2), as does the unmet half of FREE=Y; a depletable spends them (SEM-16).
        The machine load is always released. A resource the catalog does not type reads
        as renewable, so the units stay with the job that can release them."""
        if not bucket.startswith("r:"):
            return False
        return resource_type(self.catalog.resources.get(bucket[2:])) != _DEPLETABLE

    def _machine_key(self, job_ir: JobIR) -> str | None:
        """The load bucket of the job's machine, or None when unset or without max_load."""
        spec = job_ir.exec_
        if spec is None or spec.machine is None:
            return None
        key = f"{_MACHINE}{spec.machine}"
        return key if key in self._bucket_cap else None

    def _machine_entry(self, job_ir: JobIR) -> DemandEntry | None:
        """The machine-load entry of a start, or None when it takes no load (Qr4: absent is 0)."""
        key = self._machine_key(job_ir)
        if key is None:
            return None
        load = _safe_units(job_ir.job_load_units) or 0
        if load <= 0:
            return None
        return (key, load, "acquire", "completion")

    def used(
        self,
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
        *,
        own: str | None = None,
    ) -> dict[str, int]:
        """Units unavailable per bucket: those spent plus those held by the rows (DL-120).

        Held units are live runs and a renewable's units a completed run kept (DL-256).
        `own` leaves one job's row out: a start of a job that holds units re-uses them.
        A `consumed` key the catalog does not size still counts, so a period that drops a
        resource does not refund what an earlier one burned (PR-19a, period-model ss3.3)."""
        used = dict(consumed)
        for name, row in rows.items():
            if name == own:
                continue
            for reservation in row.reservations:
                used[reservation.bucket] = used.get(reservation.bucket, 0) + reservation.units
        return used

    def can_admit(
        self,
        vector: list[DemandEntry],
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
        *,
        own: str | None = None,
    ) -> bool:
        """True iff every bucket has room for its demand; gate and acquire share the test.

        `own` credits that job's held units to it (`used`, DL-256)."""
        if not vector:
            return True
        used = self.used(rows, consumed, own=own)
        return all(used.get(key, 0) + units <= self._bucket_cap[key] for key, units, _, _ in vector)

    def load_blocked(
        self,
        job_ir: JobIR,
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
        counts: Callable[[str], bool],
    ) -> bool:
        """DL-247: True when a queued job of strictly higher priority waits for load on this
        job's machine.

        Vendor rule: a job in QUE_WAIT for one machine value "automatically blocks all the
        lower priority jobs that specify the same machine attribute value", not higher or
        equal ones or other machines. The blocked job needs a positive priority and a sized
        machine, not a `job_load`. The blocker has a positive priority, loads the same
        machine, and its own load does not fit now; one whose load fits waits on a resource
        and does not block on the machine. `counts` says which waiters count as queued: the
        Oracle leaves out a held waiter and one whose box is not running."""
        return self._load_blocked(job_ir, self.used(rows, consumed), rows, counts)

    def _load_blocked(
        self,
        job_ir: JobIR,
        used: Mapping[str, int],
        rows: Mapping[str, JobRuntime],
        counts: Callable[[str], bool],
    ) -> bool:
        """`load_blocked` over usage the caller has already summed."""
        priority = job_priority(job_ir)
        key = self._machine_key(job_ir)
        if priority <= 0 or key is None:
            return False
        load_used = used.get(key, 0)
        for name, row in rows.items():
            other = self.catalog.jobs.get(name)
            if row.waiter_seq is None or name == job_ir.name or other is None:
                continue
            other_entry = self._machine_entry(other)
            if other_entry is None or other_entry[0] != key:
                continue
            if not 0 < job_priority(other) < priority:
                continue
            if load_used + other_entry[1] <= self._bucket_cap[key]:
                continue
            if counts(name):
                return True
        return False

    def resource_blocked(
        self,
        job_ir: JobIR,
        rows: Mapping[str, JobRuntime],
        consumed: Mapping[str, int],
        counts: Callable[[str], bool],
    ) -> bool:
        """DL-255: True when a queued job of strictly higher priority waits for a named
        resource this job also names.

        Vendor rule: a job in RESWAIT for one resource name "automatically blocks all the
        lower priority jobs that specify the same resource name"; dsl41 has no RESWAIT, such a
        job is QUE_WAIT. The blocked job needs a positive priority (DL-247). The blocker has a
        positive priority, names a sized resource the blocked job names, and is short now on
        any resource it names; it blocks on all of them, not only the short one (AutoSys KB
        240816, 12.0). It must also have passed its load check, since the vendor evaluates
        resources only after load, so its load fits and no higher-priority load waiter blocks
        it. `counts` is `load_blocked`'s."""
        priority = job_priority(job_ir)
        if priority <= 0:
            return False
        named = {entry[0] for entry in self._resource_entries(job_ir)}
        if not named:
            return False
        used = self.used(rows, consumed)
        for name, row in rows.items():
            other = self.catalog.jobs.get(name)
            if row.waiter_seq is None or name == job_ir.name or other is None:
                continue
            if not 0 < job_priority(other) < priority:
                continue
            entries = self._resource_entries(other)
            if not any(entry[0] in named for entry in entries):
                continue
            # a waiter holding units from an earlier run counts them as its own (DL-256)
            own_used = self.used(rows, consumed, own=name) if row.reservations else used
            if not any(
                own_used.get(key, 0) + units > self._bucket_cap[key] for key, units, _, _ in entries
            ):
                continue
            if not self._passed_load(other, used, rows, counts):
                continue
            if counts(name):
                return True
        return False

    def _resource_entries(self, job_ir: JobIR) -> list[DemandEntry]:
        """The named-resource half of a job's demand vector."""
        return without_machine_load(self.demand_vector(job_ir))

    def _passed_load(
        self,
        job_ir: JobIR,
        used: Mapping[str, int],
        rows: Mapping[str, JobRuntime],
        counts: Callable[[str], bool],
    ) -> bool:
        """DL-255: whether a queued positive-priority job has passed its machine-load stage:
        its load fits and no higher-priority load waiter blocks it."""
        entry = self._machine_entry(job_ir)
        if entry is not None and used.get(entry[0], 0) + entry[1] > self._bucket_cap[entry[0]]:
            return False
        return not self._load_blocked(job_ir, used, rows, counts)

    def has_priority_waiters(self, rows: Mapping[str, JobRuntime]) -> bool:
        """True when a queued job has a positive priority and a sized machine or named
        resource, the only kind a priority block can hold (DL-247, DL-255)."""
        for name, row in rows.items():
            job_ir = self.catalog.jobs.get(name)
            if row.waiter_seq is None or job_ir is None or job_priority(job_ir) <= 0:
                continue
            if self._machine_key(job_ir) is not None or self._resource_entries(job_ir):
                return True
        return False

    def sorted_waiters(self, rows: Mapping[str, JobRuntime]) -> list[str]:
        """The QUE_WAIT jobs in admission order, read off the rows."""

        def key(item: tuple[str, int]) -> tuple[int, int, str]:
            job, seq = item
            # PENDING: Qr2 -- an unset priority sorts last, behind every declared one,
            # explicit 0 included, though the vendor default is 0. Lower number first is
            # documented (DL-247); enqueue-seq then name make the order total.
            #
            # A waiter the catalog lacks takes the same "unset" priority, not a KeyError.
            # period-model ss10 refuses an operator that boundary (QUE_WAIT-and-removed is
            # R); the default is the floor under that gate (period-model ss5).
            job_ir = self.catalog.jobs.get(job)
            prio = _safe_units(job_ir.priority_value) if job_ir is not None else None
            return (prio if prio is not None else _UNSET_PRIORITY, seq, job)

        waiting = [(job, row.waiter_seq) for job, row in rows.items() if row.waiter_seq is not None]
        return [job for job, _ in sorted(waiting, key=key)]


def without_machine_load(vector: list[DemandEntry]) -> list[DemandEntry]:
    """The vector less its machine-load entry: what a start that skips the load check tests
    (DL-247). The start still reserves the whole vector."""
    return [entry for entry in vector if not entry[0].startswith(_MACHINE)]


def takes_machine_load(vector: list[DemandEntry]) -> bool:
    """True when a demand vector holds load on a sized machine (DL-255)."""
    return any(entry[0].startswith(_MACHINE) for entry in vector)


def machine_load(vector: list[DemandEntry]) -> list[DemandEntry]:
    """The vector's machine-load entry alone: what a FORCE_STARTJOB re-using held units
    still holds (DL-247, DL-256)."""
    return [entry for entry in vector if entry[0].startswith(_MACHINE)]


def job_priority(job_ir: JobIR) -> int:
    """A job's priority as load queueing reads it: the declared number, or 0 when unset or
    malformed (the vendor default, DL-247). A negative number compares as itself and checks
    no load."""
    value = _safe_units(job_ir.priority_value)
    return value if value is not None else 0


def checks_load(job_ir: JobIR) -> bool:
    """DL-247: whether a start tests its machine load; only a positive priority does.

    "The scheduler ignores any load unit values ... when the job has a priority value of
    zero". The load of a job that skips the test is still held (`demand_vector`)."""
    return job_priority(job_ir) > 0


def to_reservations(vector: list[DemandEntry]) -> tuple[CapacityReservation, ...]:
    """The acquiring half of a demand vector, as the rows record it.

    A `gate` entry reserves nothing: its threshold was tested at admission."""
    held = []
    for key, units, mode, policy in vector:
        if mode != "acquire":
            continue
        assert policy is not None  # demand_vector gives every acquire entry one
        held.append(CapacityReservation(bucket=key, units=units, release_policy=policy))
    return tuple(held)


def _safe_units(accessor: object) -> int | None:
    """Call a typed-int IR accessor, returning None on a malformed value.

    The oracle models only what parses; preflight refuses a malformed value (DL-50)."""
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


def requirement_demand(
    res_type: str, free: str | None, renewable_free: RenewableFree
) -> tuple[DemandMode, ReleasePolicy | None]:
    """What one `resources:` group demands: the mode and, when it holds units, the policy.

    'gate' is a threshold (`res_type: T`): a level check that holds and releases nothing, so
    it has no policy. 'acquire' holds `QUANTITY` units until `release_policy` says otherwise.
    Public because the explore page states the same demand (DL-192); the T branch is half
    the rule, and `release_policy` alone reported a gate as held units."""
    if res_type == _THRESHOLD:
        return "gate", None
    return "acquire", release_policy(res_type, free, renewable_free)


def job_demand(
    res_type: str, refs: Sequence[ResourceRef], renewable_free: RenewableFree
) -> tuple[int, DemandMode, ReleasePolicy | None]:
    """One job's whole demand on one resource: groups classified by `requirement_demand`
    and coalesced by `merge_requirements`.

    One owner for two readers (DL-193): the pool reserves the result and the explore page
    draws it, so a job listing one resource twice must read as the sum on both. `refs` is
    never empty."""
    total: tuple[int, DemandMode, ReleasePolicy | None] | None = None
    for ref in refs:
        mode, policy = requirement_demand(res_type, ref.free, renewable_free)
        entry: tuple[int, DemandMode, ReleasePolicy | None] = (ref.quantity, mode, policy)
        total = entry if total is None else merge_requirements(total, entry)
    assert total is not None
    return total


def merge_requirements(
    left: tuple[int, DemandMode, ReleasePolicy | None],
    right: tuple[int, DemandMode, ReleasePolicy | None],
) -> tuple[int, DemandMode, ReleasePolicy | None]:
    """Coalesce two requirements on one bucket (a job listing a resource twice).

    The demand sums (two `(LOCK, QUANTITY=2)` groups want four), a bucket any group holds is
    held, and the release policy merges to the most restrictive so asymmetric FREE never
    frees early. Public with `requirement_demand`: the page draws one link per (job, resource)
    and must agree with the pool."""
    units, mode, policy = left
    other_units, other_mode, other_policy = right
    return (
        units + other_units,
        "acquire" if "acquire" in (mode, other_mode) else "gate",
        _merge_policy(policy, other_policy),
    )


def release_policy(res_type: str, free: str | None, renewable_free: RenewableFree) -> ReleasePolicy:
    """DL-50: per-request release policy; FREE overrides the res_type default.

    Returns 'completion' (any terminal), 'success' (SUCCESS only) or 'never'. '' reads as
    renewable. A renewable with no FREE takes the `renewable-free` switch (DL-256): Y, the
    vendor default ("resources Attribute", AutoSys 24.2), frees on SUCCESS only; A frees on
    every completion. A depletable with no FREE never frees. An explicit FREE overrides the
    default on every res_type (DL-50); preflight refuses FREE=Y and FREE=A on a depletable
    (DL-287, Q12), and a direct caller is not refused.

    Public because the explore page states the same policy per lock member (DL-192); one
    owner avoids a second table (DL-72)."""
    # FREE absent, or a code the mapping lacks: the res_type default
    default = "never" if res_type == _DEPLETABLE else _FREE_POLICY[renewable_free]
    return _FREE_POLICY.get(free or "", default)


def _merge_policy(a: ReleasePolicy | None, b: ReleasePolicy | None) -> ReleasePolicy | None:
    """Coalesce two release policies for one bucket to the most restrictive (DL-50)."""
    if a is None:
        return b
    if b is None:
        return a
    rank = {"never": 0, "success": 1, "completion": 2}
    return a if rank[a] <= rank[b] else b
