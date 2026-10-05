"""DL-50 resource-manager tests that need direct Oracle access (bucket
introspection, Hypothesis property search) and so live outside test_oracle.py's
bisimulation harness. The core admission traces (mutex, pool, threshold,
machine-load, box-member, priority, kill) are in test_oracle.py and run under
BOTH oracle-direct and engine arms; this file adds:

  * the cross-order SAFETY + LIVENESS property (DL-50): the deterministic
    oracle picks one representative trace, so we certify -- across permuted
    admissible orders -- that no bucket is ever over-committed and that a run
    whose completions all succeed is deadlock-free (every runnable job
    eventually admits; held units are DL-286's stated limit). A failure here is
    a real bug OR a real order-dependence, surfaced, not hidden;
  * depletable (res_type D) and FREE=N: units that never return;
  * machine load with priorities, arrivals, completions and FORCE starts:
    every checked start fits and passes no higher-priority load waiter, and
    the run ends with every job run and no unit held (DL-247);
  * a named resource with priorities, loads, arrivals, completions and
    FORCE starts: no start passes a higher-priority waiter short on a
    resource it names (DL-255);
  * the enforcement-is-preflight boundary: an UNSIZED resource is not modelled
    by the oracle (runs unthrottled oracle-direct) -- the runner's preflight is
    the execution gate that refuses it (see test_runner_scheduler.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event

T0 = datetime(2026, 7, 1, 8, 0)


def _ev(kind: str, minute: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minute), kind=kind, payload=payload)  # type: ignore[arg-type]


def _used(o: Oracle) -> dict[str, int]:
    """Bucket usage as DL-120 decomposes it: the units the live rows reserve
    plus the units the owner records as spent. The pool holds neither."""
    return o._pool.used(o.store.job, o.store.consumed)


def _no_overcommit(o: Oracle) -> bool:
    used = _used(o)
    return all(used.get(k, 0) <= cap for k, cap in o._pool._bucket_cap.items())


def _renewable_pool_catalog(capacity: int, demands: list[int]) -> str:
    jobs = "".join(
        f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY={q})\n\n"
        for i, q in enumerate(demands)
    )
    return f"insert_resource: R\nres_type: R\namount: {capacity}\n\n{jobs}"


@settings(max_examples=250, deadline=None)
@given(data=st.data())
def test_dl50_admission_never_overcommits_and_is_deadlock_free(data: st.DataObject) -> None:
    """For any capacity, any set of runnable demands, and ANY order of same-
    instant STARTJOBs and completions: (1) no bucket is ever over-committed
    (the safety invariant that makes the manager trustworthy), and (2) every
    job eventually admits (deadlock-freedom -- every completion is SUCCESS,
    so no job holds units from an earlier run, and the all-or-nothing acquire
    then has no hold-and-wait). Units held from an earlier run can build a
    circular wait; that is a stated limit (DL-286), pinned in test_oracle.py.
    Demands are clamped to <= capacity so each job CAN run; an unclamped
    q>capacity would legitimately hang (a distinct, refused case)."""
    capacity = data.draw(st.integers(min_value=1, max_value=4))
    demands = data.draw(st.lists(st.integers(min_value=1, max_value=4), min_size=2, max_size=6))
    demands = [min(d, capacity) for d in demands]
    n = len(demands)
    start_order = data.draw(st.permutations(range(n)))
    complete_order = data.draw(st.permutations(range(n)))

    o = Oracle(lower_source(_renewable_pool_catalog(capacity, demands)))
    for idx in start_order:
        o.feed(_ev("STARTJOB", 0, job=f"j{idx}"))
        assert _no_overcommit(o)

    terminal: set[str] = set()
    minute = 1.0
    made_progress = True
    while len(terminal) < n and made_progress:
        made_progress = False
        for idx in complete_order:
            job = f"j{idx}"
            if job in terminal:
                continue
            if o.store.job[job].status == "RUNNING":
                o.feed(_ev("STATUS", minute, job=job, status="SUCCESS"))
                minute += 1
                terminal.add(job)
                assert _no_overcommit(o)
                made_progress = True

    assert len(terminal) == n, "deadlock: a runnable job was never admitted"
    assert all(_used(o).get(k, 0) == 0 for k in o._pool._bucket_cap), "renewable units leaked"


@settings(max_examples=250, deadline=None)
@given(data=st.data())
def test_dl256_held_units_never_overcommit_under_force_and_release(data: st.DataObject) -> None:
    """DL-256 over any script of plain and forced starts, every completion
    outcome, kills and RELEASE_RESOURCE, under the vendor default: no bucket
    is ever used past its capacity, and usage is exactly the rows' units
    plus what was spent. A forced start of a held job checks nothing, so
    this is what proves it re-uses the held units rather than taking more."""
    capacity = data.draw(st.integers(min_value=1, max_value=3))
    demands = data.draw(st.lists(st.integers(min_value=1, max_value=3), min_size=2, max_size=4))
    demands = [min(d, capacity) for d in demands]
    o = Oracle(lower_source(_renewable_pool_catalog(capacity, demands)))
    names = [f"j{i}" for i in range(len(demands))]
    for minute in range(data.draw(st.integers(min_value=1, max_value=20))):
        job = data.draw(st.sampled_from(names))
        kind = data.draw(
            st.sampled_from(["STARTJOB", "FORCE_STARTJOB", "STATUS", "KILLJOB", "RELEASE_RESOURCE"])
        )
        payload: dict[str, object] = {"job": job}
        if kind == "STATUS":
            if o.store.job[job].status not in ("STARTING", "RUNNING"):
                continue
            payload["status"] = data.draw(st.sampled_from(["SUCCESS", "FAILURE", "TERMINATED"]))
        o.feed(_ev(kind, minute, **payload))
        assert _no_overcommit(o)
        held = sum(r.units for row in o.store.job.values() for r in row.reservations)
        assert _used(o).get("r:R", 0) == held + o.store.consumed.get("r:R", 0)


def _machine_load_catalog(capacity: int, jobs: list[tuple[int, int]]) -> str:
    body = "".join(
        f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: lm\n"
        f"job_load: {load}\npriority: {prio}\n\n"
        for i, (load, prio) in enumerate(jobs)
    )
    return f"insert_machine: lm\ntype: a\nnode_name: lm\nmax_load: {capacity}\n\n{body}"


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_dl247_checked_starts_fit_and_respect_priority_blocking(data: st.DataObject) -> None:
    """DL-247 over any script of arrivals (plain or forced) interleaved with
    completions. At every start that checks load -- a positive priority, not
    forced -- the units held just before plus the job's load fit the machine,
    and no queued job of strictly higher positive priority is waiting for
    load it cannot get. The check is computed here from the rows, not by the
    oracle's own test. A priority-0 or forced start may go over the limit.
    Every job eventually runs and no unit leaks. Loads are clamped to
    max_load, the feasibility preflight enforces."""
    capacity = data.draw(st.integers(min_value=1, max_value=5))
    jobs = data.draw(
        st.lists(
            st.tuples(st.integers(min_value=1, max_value=5), st.integers(min_value=0, max_value=3)),
            min_size=3,
            max_size=8,
        )
    )
    jobs = [(min(load, capacity), prio) for load, prio in jobs]
    n = len(jobs)
    forced = data.draw(st.lists(st.booleans(), min_size=n, max_size=n))
    pending = list(data.draw(st.permutations(range(n))))
    names = [f"j{i}" for i in range(n)]
    load = {f"j{i}": job_load for i, (job_load, _) in enumerate(jobs)}
    prio = {f"j{i}": job_prio for i, (_, job_prio) in enumerate(jobs)}

    o = Oracle(lower_source(_machine_load_catalog(capacity, jobs)))
    forcing: list[str | None] = [None]
    reserve = o.store.reserve

    def checked_reserve(job: str, reservations: object) -> None:
        if prio[job] > 0 and forcing[0] != job:
            used = _used(o).get("m:lm", 0)
            assert used + load[job] <= capacity, f"{job} started over max_load"
            for other, row in o.store.job.items():
                if row.waiter_seq is None or other == job:
                    continue
                short = used + load[other] > capacity
                assert not (0 < prio[other] < prio[job] and short), (
                    f"{job} started past {other}, a higher-priority load waiter"
                )
        reserve(job, reservations)  # type: ignore[arg-type]

    o.store.reserve = checked_reserve  # type: ignore[method-assign]

    done: set[str] = set()
    minute = 0.0
    while True:
        running = [job for job in names if o.store.job[job].status == "RUNNING"]
        choices = (["arrive"] if pending else []) + (["complete"] if running else [])
        if not choices:
            break
        minute += 1
        if data.draw(st.sampled_from(choices)) == "arrive":
            idx = pending.pop(0)
            job = names[idx]
            forcing[0] = job if forced[idx] else None
            o.feed(_ev("FORCE_STARTJOB" if forced[idx] else "STARTJOB", minute, job=job))
            forcing[0] = None
        else:
            job = data.draw(st.sampled_from(running))
            o.feed(_ev("STATUS", minute, job=job, status="SUCCESS"))
            done.add(job)

    assert done == set(names), "deadlock: a runnable job was never admitted"
    assert all(_used(o).get(k, 0) == 0 for k in o._pool._bucket_cap), "load units leaked"


def _resource_priority_catalog(
    capacity: int, amounts: dict[str, int], jobs: list[tuple[int, int, int, int]]
) -> str:
    resources = "".join(
        f"insert_resource: {name}\nres_type: R\namount: {amount}\n\n"
        for name, amount in amounts.items()
    )
    body = ""
    for i, (load, prio, qty_r, qty_s) in enumerate(jobs):
        groups = [f"({n}, QUANTITY={q})" for n, q in (("R", qty_r), ("S", qty_s)) if q]
        body += (
            f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: lm\n"
            f"job_load: {load}\npriority: {prio}\n"
            + (f"resources: {' AND '.join(groups)}\n" if groups else "")
            + "\n"
        )
    return f"insert_machine: lm\ntype: a\nnode_name: lm\nmax_load: {capacity}\n\n{resources}{body}"


@settings(max_examples=300, deadline=None)
@given(data=st.data())
def test_dl255_starts_respect_resource_priority_blocking(data: st.DataObject) -> None:
    """DL-255 over any script of arrivals (plain or forced) interleaved with
    completions, on one machine and two named resources. At every start of
    a positive-priority job that names a resource, forced or not, no queued
    job of strictly higher positive priority that names one of the same
    resources is short on ANY resource it names after passing its load
    check: its load fits and no higher-priority load waiter is short (KB
    240816). The check is computed here from the rows, not by the oracle's
    own test. No resource is over-committed, every job eventually runs, and
    no unit leaks. Demands are clamped to what preflight lets through."""
    capacity = data.draw(st.integers(min_value=1, max_value=5))
    amounts = {
        "R": data.draw(st.integers(min_value=1, max_value=4)),
        "S": data.draw(st.integers(min_value=1, max_value=3)),
    }
    jobs = data.draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=2),
                st.integers(min_value=0, max_value=3),
                st.integers(min_value=0, max_value=4),
                st.integers(min_value=0, max_value=3),
            ),
            min_size=3,
            max_size=8,
        )
    )
    jobs = [
        (min(load, capacity), prio, min(qr, amounts["R"]), min(qs, amounts["S"]))
        for load, prio, qr, qs in jobs
    ]
    n = len(jobs)
    forced = data.draw(st.lists(st.booleans(), min_size=n, max_size=n))
    pending = list(data.draw(st.permutations(range(n))))
    names = [f"j{i}" for i in range(n)]
    load = {names[i]: job[0] for i, job in enumerate(jobs)}
    prio = {names[i]: job[1] for i, job in enumerate(jobs)}
    demand = {
        names[i]: {res: q for res, q in (("R", job[2]), ("S", job[3])) if q}
        for i, job in enumerate(jobs)
    }

    o = Oracle(lower_source(_resource_priority_catalog(capacity, amounts, jobs)))
    reserve = o.store.reserve

    def queued() -> list[str]:
        return [job for job, row in o.store.job.items() if row.waiter_seq is not None]

    def load_short(job: str, used: dict[str, int]) -> bool:
        return load[job] > 0 and used.get("m:lm", 0) + load[job] > capacity

    def passed_load(job: str, used: dict[str, int]) -> bool:
        if load_short(job, used):
            return False
        return not any(
            0 < prio[other] < prio[job] and load_short(other, used)
            for other in queued()
            if other != job
        )

    def res_short(job: str, used: dict[str, int]) -> bool:
        return any(used.get(f"r:{res}", 0) + q > amounts[res] for res, q in demand[job].items())

    def checked_reserve(job: str, reservations: object) -> None:
        if prio[job] > 0 and demand[job]:
            used = _used(o)
            for other in queued():
                if other == job or not 0 < prio[other] < prio[job]:
                    continue
                if not demand[job].keys() & demand[other].keys():
                    continue
                assert not (res_short(other, used) and passed_load(other, used)), (
                    f"{job} started past {other}, a higher-priority resource waiter"
                )
        reserve(job, reservations)  # type: ignore[arg-type]
        used = _used(o)
        assert all(used.get(f"r:{res}", 0) <= cap for res, cap in amounts.items()), (
            f"{job} over-committed a resource"
        )

    o.store.reserve = checked_reserve  # type: ignore[method-assign]

    done: set[str] = set()
    minute = 0.0
    while True:
        running = [job for job in names if o.store.job[job].status == "RUNNING"]
        # arrivals twice as likely as completions, so waiters pile up
        choices = (["arrive"] * 2 if pending else []) + (["complete"] if running else [])
        if not choices:
            break
        minute += 1
        if data.draw(st.sampled_from(choices)) == "arrive":
            idx = pending.pop(0)
            o.feed(_ev("FORCE_STARTJOB" if forced[idx] else "STARTJOB", minute, job=names[idx]))
        else:
            job = data.draw(st.sampled_from(running))
            o.feed(_ev("STATUS", minute, job=job, status="SUCCESS"))
            done.add(job)

    assert done == set(names), "deadlock: a runnable job was never admitted"
    assert all(_used(o).get(k, 0) == 0 for k in o._pool._bucket_cap), "units leaked"


def test_dl50_depletable_drains_and_never_refills() -> None:
    """res_type D acquires and NEVER releases (within a session; replenishment
    is update_resource = SEM-16 non-goal). An amount=2 depletable admits the
    first two QUANTITY=1 jobs; after both SUCCEED a third stays QUE_WAIT -- the
    quota is gone."""
    text = (
        "insert_resource: QUOTA\nres_type: D\namount: 2\n\n"
        "insert_job: d1\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n\n"
        "insert_job: d2\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n\n"
        "insert_job: d3\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n"
    )
    o = Oracle(lower_source(text))
    for j in ("d1", "d2", "d3"):
        o.feed(_ev("STARTJOB", 0, job=j))
    assert o.store.job["d3"].status == "QUE_WAIT"
    o.feed(_ev("STATUS", 1, job="d1", status="SUCCESS"))
    o.feed(_ev("STATUS", 2, job="d2", status="SUCCESS"))
    assert o.store.job["d3"].status == "QUE_WAIT"  # depleted: never refilled


def test_dl50_free_n_never_releases_even_on_success() -> None:
    """FREE=N holds units forever, even on SUCCESS -- the waiter never admits."""
    text = (
        "insert_resource: NLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: n1\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (NLOCK, QUANTITY=1, FREE=N)\n\n"
        "insert_job: n2\njob_type: c\ncommand: y\nmachine: m1\nresources: (NLOCK, QUANTITY=1)\n"
    )
    o = Oracle(lower_source(text))
    o.feed(_ev("STARTJOB", 0, job="n1"))
    o.feed(_ev("STARTJOB", 0, job="n2"))
    o.feed(_ev("STATUS", 1, job="n1", status="SUCCESS"))
    assert o.store.job["n2"].status == "QUE_WAIT"


def test_dl50_unsized_resource_is_unmodelled_oracle_direct() -> None:
    """Enforcement-is-preflight boundary: a resource with no insert_resource has
    no oracle bucket, so oracle-direct runs both requesters unthrottled. The
    runner's preflight is what refuses this for execution (DL-50); the oracle
    models only sizeable buckets."""
    text = (
        "insert_job: u1\njob_type: c\ncommand: x\nmachine: m1\nresources: (GHOST, QUANTITY=1)\n\n"
        "insert_job: u2\njob_type: c\ncommand: y\nmachine: m1\nresources: (GHOST, QUANTITY=1)\n"
    )
    o = Oracle(lower_source(text))
    o.feed(_ev("STARTJOB", 0, job="u1"))
    o.feed(_ev("STARTJOB", 0, job="u2"))
    assert o.store.job["u1"].status == "RUNNING"
    assert o.store.job["u2"].status == "RUNNING"  # no bucket -> no throttle


def test_dl50_self_retrigger_leak_invariant_used_equals_held() -> None:
    """Direct check of the self-retriggering-holder fix (DL-50): at every
    step, `used` must equal the units the rows actually reserve (no strand).
    Under the vendor default (DL-256) the failed last run keeps its unit on
    its row, so the bucket is back to 0 only after RELEASE_RESOURCE."""
    text = (
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        "insert_job: sl\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1)\ncondition: s(sl)\n"
    )
    o = Oracle(lower_source(text))

    def held_total() -> int:
        return sum(r.units for row in o.store.job.values() for r in row.reservations)

    o.feed(_ev("FORCE_STARTJOB", 0, job="sl"))
    assert _used(o).get("r:R", 0) == held_total() == 1
    o.feed(_ev("STATUS", 1, job="sl", status="SUCCESS"))  # r1 done, r2 re-acquires
    # not 1-with-empty-held (the leak)
    assert _used(o).get("r:R", 0) == held_total() == 1
    o.feed(_ev("STATUS", 2, job="sl", status="FAILURE"))  # r2 fails, loop stops
    assert _used(o).get("r:R", 0) == held_total() == 1  # held by the failed run, no strand
    o.feed(_ev("RELEASE_RESOURCE", 3, job="sl"))
    assert _used(o).get("r:R", 0) == held_total() == 0  # fully released, no strand


def test_dl50_duplicate_resource_refs_coalesce_no_overcommit() -> None:
    """A job listing one resource twice must coalesce to a summed
    demand so the acquire matches the admission test -- used=2, never 4."""
    text = (
        "insert_resource: DUP\nres_type: R\namount: 2\n\n"
        "insert_job: dj\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (DUP, QUANTITY=1) AND (DUP, QUANTITY=1)\n"
    )
    o = Oracle(lower_source(text))
    o.feed(_ev("STARTJOB", 0, job="dj"))
    assert o.store.job["dj"].status == "RUNNING"
    assert _used(o).get("r:DUP") == 2  # coalesced, not 2+2=4
    assert _no_overcommit(o)
