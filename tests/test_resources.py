"""DL-50 resource-manager tests that need direct Oracle access (bucket
introspection, Hypothesis property search) and so live outside test_oracle.py's
bisimulation harness. The core admission traces (mutex, pool, threshold,
machine-load, box-member, priority, kill) are in test_oracle.py and run under
BOTH oracle-direct and engine arms; this file adds:

  * the cross-order SAFETY + LIVENESS property (Codex/DL-50): the deterministic
    oracle picks one representative trace, so we certify -- across permuted
    admissible orders -- that no bucket is ever over-committed and that the run
    is deadlock-free (every runnable job eventually admits). A failure here is
    a real bug OR a real order-dependence, surfaced, not hidden;
  * depletable (res_type D) and FREE=N: units that never return;
  * the enforcement-is-preflight boundary: an UNSIZED resource is not modelled
    by the oracle (runs unthrottled oracle-direct) -- the runner's preflight is
    the execution gate that refuses it (see test_runner_scheduler.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.ir import lower_source
from dsl41.oracle import Event, Oracle

T0 = datetime(2026, 7, 1, 8, 0)


def _ev(kind: str, minute: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minute), kind=kind, payload=payload)  # type: ignore[arg-type]


def _no_overcommit(o: Oracle) -> bool:
    return all(o._bucket_used.get(k, 0) <= cap for k, cap in o._bucket_cap.items())


def _renewable_pool_catalog(capacity: int, demands: list[int]) -> str:
    jobs = "".join(
        f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"resources: (R, QUANTITY={q})\n\n"
        for i, q in enumerate(demands)
    )
    return f"insert_resource: R\nres_type: R\namount: {capacity}\n\n{jobs}"


@settings(max_examples=250, deadline=None)
@given(data=st.data())
def test_dl50_admission_never_overcommits_and_is_deadlock_free(data: st.DataObject) -> None:
    """For any capacity, any set of runnable demands, and ANY order of same-
    instant STARTJOBs and completions: (1) no bucket is ever over-committed
    (the safety invariant that makes the manager trustworthy), and (2) every
    job eventually admits (deadlock-freedom -- the all-or-nothing acquire has
    no hold-and-wait). Demands are clamped to <= capacity so each job CAN run;
    an unclamped q>capacity would legitimately hang (a distinct, refused case)."""
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
    assert all(o._bucket_used.get(k, 0) == 0 for k in o._bucket_cap), "renewable units leaked"


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
    """Direct check of the review BLOCKER: after a self-retriggering holder
    finally stops, the renewable bucket must be back to 0 and, at every step,
    `used` must equal the units actually recorded in `_held` (no strand)."""
    text = (
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        "insert_job: sl\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1)\ncondition: s(sl)\n"
    )
    o = Oracle(lower_source(text))

    def held_total() -> int:
        return sum(u for held in o._held.values() for (_, u, _) in held)

    o.feed(_ev("FORCE_STARTJOB", 0, job="sl"))
    assert o._bucket_used.get("r:R", 0) == held_total() == 1
    o.feed(_ev("STATUS", 1, job="sl", status="SUCCESS"))  # r1 done, r2 re-acquires
    assert o._bucket_used.get("r:R", 0) == held_total() == 1  # not 1-with-empty-held (the leak)
    o.feed(_ev("STATUS", 2, job="sl", status="FAILURE"))  # r2 fails, loop stops
    assert o._bucket_used.get("r:R", 0) == held_total() == 0  # fully released, no strand


def test_dl50_duplicate_resource_refs_coalesce_no_overcommit() -> None:
    """Review MINOR: a job listing one resource twice must coalesce to a summed
    demand so the acquire matches the admission test -- used=2, never 4."""
    text = (
        "insert_resource: DUP\nres_type: R\namount: 2\n\n"
        "insert_job: dj\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (DUP, QUANTITY=1) AND (DUP, QUANTITY=1)\n"
    )
    o = Oracle(lower_source(text))
    o.feed(_ev("STARTJOB", 0, job="dj"))
    assert o.store.job["dj"].status == "RUNNING"
    assert o._bucket_used.get("r:DUP") == 2  # coalesced, not 2+2=4
    assert _no_overcommit(o)
