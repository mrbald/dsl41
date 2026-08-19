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

from typing import TYPE_CHECKING

from dsl41.oracle_state import CapacityReservation, ReleasePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dsl41.ir import CatalogIR, JobIR
    from dsl41.oracle_state import JobRuntime

#: Sorts a waiter whose priority nothing declares -- and one whose job the
#: catalog no longer has at all -- behind every declared priority.
_UNSET_PRIORITY = 1 << 31

#: The demand vector's entry shape: (bucket key, units, mode, release policy).
#: mode is 'acquire' (holds units) or 'gate' (threshold: check-only).
DemandEntry = tuple[str, int, str, ReleasePolicy | None]


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
                self._bucket_cap[f"m:{mname}"] = cap
        for rname, resource in catalog.resources.items():
            cap = _safe_units(resource.capacity_units)
            if cap is not None:
                self._bucket_cap[f"r:{rname}"] = cap

    def demand_vector(self, job_ir: JobIR) -> list[DemandEntry]:
        """The full (bucket_key, units, mode, release_policy) demand of a start.
        mode is 'acquire' (holds units) or 'gate' (threshold: check-only, never
        holds). Only buckets the oracle can size appear -- an unsized resource
        or an absent max_load contributes nothing here (preflight refuses the
        former for execution; the latter is AutoSys's unlimited-load default)."""
        raw: list[DemandEntry] = []
        spec = job_ir.exec_
        if spec is not None and spec.machine is not None:
            key = f"m:{spec.machine}"
            if key in self._bucket_cap:
                load = _safe_units(job_ir.job_load_units) or 0  # Qr4: absent -> 0
                if load > 0:
                    raw.append((key, load, "acquire", "completion"))
        for ref in job_ir.resources:
            key = f"r:{ref.name}"
            if key not in self._bucket_cap:
                continue  # unsized -> not modelled here (preflight refuses run)
            resource = self.catalog.resources.get(ref.name)
            res_type = (resource.res_type or "").strip().upper() if resource else ""
            if res_type == "T":
                raw.append((key, ref.quantity, "gate", None))
            else:
                raw.append((key, ref.quantity, "acquire", _release_policy(res_type, ref.free)))
        # Coalesce duplicate bucket keys (a job listing one resource twice):
        # SUM the demand so can_admit's per-entry test and the reservation's sum
        # agree -- else two `(LOCK, QUANTITY=2)` entries each pass free>=2 while
        # the acquire over-commits to 4 (review MINOR). Release policy merges to
        # the most restrictive so asymmetric FREE never frees early.
        merged: dict[str, tuple[int, str, ReleasePolicy | None]] = {}
        for key, units, mode, policy in raw:
            if key in merged:
                u0, m0, p0 = merged[key]
                mode = "acquire" if "acquire" in (m0, mode) else "gate"
                merged[key] = (u0 + units, mode, _merge_policy(p0, policy))
            else:
                merged[key] = (units, mode, policy)
        return [(key, units, mode, policy) for key, (units, mode, policy) in merged.items()]

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
        used = self.used(rows, consumed)
        return all(used.get(key, 0) + units <= self._bucket_cap[key] for key, units, _, _ in vector)

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
            # PENDING: Qr2 -- lower priority number == higher priority assumed;
            # unset sorts last. enqueue-seq then name make the order total.
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


def _release_policy(res_type: str, free: str | None) -> ReleasePolicy:
    """DL-50: per-request release policy. FREE overrides the res_type default.
    Returns 'completion' (release on any terminal), 'success' (only on SUCCESS),
    or 'never'. res_type is upper-cased; '' (absent) reads as renewable."""
    if free == "Y":
        return "success"
    if free == "N":
        return "never"
    if free == "A":
        return "completion"
    return "never" if res_type == "D" else "completion"  # FREE absent -> res_type default


def _merge_policy(a: ReleasePolicy | None, b: ReleasePolicy | None) -> ReleasePolicy | None:
    """Coalesce two release policies for one bucket (duplicate resource refs) to
    the MOST RESTRICTIVE, so asymmetric FREE never frees early (DL-50 review)."""
    if a is None:
        return b
    if b is None:
        return a
    rank = {"never": 0, "success": 1, "completion": 2}
    return a if rank[a] <= rank[b] else b
