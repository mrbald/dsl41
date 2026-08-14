"""The DL-50 capacity subsystem: sized buckets and the QUE_WAIT queue.

Extracted from `oracle.py` by DL-88 along the line DL-74 already drew: the
pool decides who may be admitted and in what order; every status transition
and event emission that decision implies stays on the Oracle. Nothing here
knows about statuses, events or time -- which is why it could move at all,
and why it is the piece to move first when the interpreter needs room.

The pool is authoritative state that deliberately does NOT live under
`RuntimeState` (concurrency-model ss3, settled by DL-86). It carries tested
invariants instead: its waiter set is exactly the QUE_WAIT jobs and only a
starting or running job holds units, so no change here is constructible
without an accompanying row change to carry it into a revision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dsl41.ir import CatalogIR, JobIR


class CapacityPool:
    """The DL-50 capacity subsystem of one Oracle: the sized buckets (machine
    max_load, resource amounts), the units each RUNNING job holds, and the
    QUE_WAIT queue with its admission ORDER. Extracted from Oracle by DL-74
    along the one line that matters: the pool decides who may be admitted and
    in what order; every status transition and event emission the decision
    implies stays on the Oracle."""

    def __init__(self, catalog: CatalogIR) -> None:
        self.catalog = catalog
        # DL-50 resource/load buckets: capacity per contended entity, seeded
        # from the catalog (malformed -> skipped; preflight refuses the run).
        #: bucket key -> capacity. `m:<machine>` = max_load, `r:<name>` = amount.
        self._bucket_cap: dict[str, int] = {}
        #: bucket key -> units currently held by RUNNING acquirers.
        self._bucket_used: dict[str, int] = {}
        for mname, machine in catalog.machines.items():
            cap = _safe_units(machine.max_load_units)
            if cap is not None:
                self._bucket_cap[f"m:{mname}"] = cap
        for rname, resource in catalog.resources.items():
            cap = _safe_units(resource.capacity_units)
            if cap is not None:
                self._bucket_cap[f"r:{rname}"] = cap
        #: job -> [(bucket_key, units, release_policy)] it holds while RUNNING.
        self._held: dict[str, list[tuple[str, int, str]]] = {}
        #: QUE_WAIT jobs and their enqueue sequence (deterministic ordering).
        self._waiters: list[str] = []
        self._waiter_seq: dict[str, int] = {}
        self._enqueue_counter = 0

    def demand_vector(self, job_ir: JobIR) -> list[tuple[str, int, str, str | None]]:
        """The full (bucket_key, units, mode, release_policy) demand of a start.
        mode is 'acquire' (holds units) or 'gate' (threshold: check-only, never
        holds). Only buckets the oracle can size appear -- an unsized resource
        or an absent max_load contributes nothing here (preflight refuses the
        former for execution; the latter is AutoSys's unlimited-load default)."""
        raw: list[tuple[str, int, str, str | None]] = []
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
        # SUM the demand so can_admit's per-entry test and acquire's sum agree
        # -- else two `(LOCK, QUANTITY=2)` entries each pass free>=2 while the
        # acquire over-commits to 4 (review MINOR). Release policy merges to the
        # most restrictive so asymmetric FREE never frees early.
        merged: dict[str, tuple[int, str, str | None]] = {}
        for key, units, mode, policy in raw:
            if key in merged:
                u0, m0, p0 = merged[key]
                mode = "acquire" if "acquire" in (m0, mode) else "gate"
                merged[key] = (u0 + units, mode, _merge_policy(p0, policy))
            else:
                merged[key] = (units, mode, policy)
        return [(key, units, mode, policy) for key, (units, mode, policy) in merged.items()]

    def can_admit(self, vector: list[tuple[str, int, str, str | None]]) -> bool:
        """True iff every bucket has room for its demand (gate and acquire share
        the same free>=units test; keys are guaranteed sized)."""
        return all(
            self._bucket_used.get(key, 0) + units <= self._bucket_cap[key]
            for key, units, _, _ in vector
        )

    def acquire(self, job: str, vector: list[tuple[str, int, str, str | None]]) -> None:
        held: list[tuple[str, int, str]] = []
        for key, units, mode, policy in vector:
            if mode == "acquire":
                self._bucket_used[key] = self._bucket_used.get(key, 0) + units
                assert policy is not None
                held.append((key, units, policy))
        if held:
            # extend, never overwrite: with release-before-wake the job's prior
            # record is already released, so this is empty -> assignment; the
            # extend is the belt-and-braces that turns any missed release into a
            # recoverable over-hold, never a permanent strand (review BLOCKER).
            self._held.setdefault(job, []).extend(held)

    def holds(self, job: str) -> bool:
        """True while `job` still holds units: only a holder's terminal
        transition frees anything (the Oracle's release-before-wake gate)."""
        return job in self._held

    def release(self, job: str, terminal_status: str) -> None:
        """Return a completed holder's units per each request's release policy;
        'never' / unmet 'success' units stay consumed (depletable / hold-on-
        failure) -- and the job is terminal, so they never come back."""
        for key, units, policy in self._held.pop(job, []):
            if policy == "completion" or (policy == "success" and terminal_status == "SUCCESS"):
                self._bucket_used[key] = self._bucket_used.get(key, 0) - units

    def enqueue(self, job: str) -> None:
        """Record a refused admission in the queue; the QUE_WAIT transition it
        implies is the Oracle's (DL-74)."""
        self._enqueue_counter += 1
        self._waiter_seq[job] = self._enqueue_counter
        if job not in self._waiters:
            self._waiters.append(job)

    def sorted_waiters(self) -> list[str]:
        def key(j: str) -> tuple[int, int, str]:
            # PENDING: Qr2 -- lower priority number == higher priority assumed;
            # unset sorts last. enqueue-seq then name make the order total.
            prio = _safe_units(self.catalog.jobs[j].priority_value)
            return (prio if prio is not None else 1 << 31, self._waiter_seq.get(j, 0), j)

        return sorted(self._waiters, key=key)

    def dequeue(self, job: str) -> None:
        if job in self._waiters:
            self._waiters.remove(job)
        self._waiter_seq.pop(job, None)


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


def _release_policy(res_type: str, free: str | None) -> str:
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


def _merge_policy(a: str | None, b: str | None) -> str | None:
    """Coalesce two release policies for one bucket (duplicate resource refs) to
    the MOST RESTRICTIVE, so asymmetric FREE never frees early (DL-50 review)."""
    if a is None:
        return b
    if b is None:
        return a
    rank = {"never": 0, "success": 1, "completion": 2}
    return a if rank[a] <= rank[b] else b
