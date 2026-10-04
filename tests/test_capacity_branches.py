"""Branch gaps in capacity.py that the DL-105 gate list leaves open (DL-269).

Each test holds the observable effect of one branch: a returned value, a
bucket the pool sized or skipped, a start the oracle admitted. The pool is a
pure function of (catalog, rows, consumed), so most of these call it directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dsl41.capacity import CapacityPool, job_demand, job_priority, merge_requirements
from dsl41.ir import ResourceRef, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobRuntime

T0 = datetime(2026, 7, 1, 8, 0)

_SIZED = (
    "insert_machine: m1\ntype: a\nnode_name: m1\nmax_load: 5\n\n"
    "insert_resource: RENEW\nres_type: R\namount: 2\n\n"
    "insert_resource: DEPL\nres_type: D\namount: 2\n\n"
    "insert_job: sized\njob_type: c\ncommand: x\nmachine: m1\njob_load: 1\npriority: 1\n\n"
    "insert_job: bare\njob_type: c\ncommand: x\nmachine: nowhere\npriority: 1\n\n"
    "insert_job: unprioritised\njob_type: c\ncommand: x\nmachine: m1\njob_load: 1\n"
)


def _queued(seq: int) -> JobRuntime:
    return JobRuntime(status="QUE_WAIT", waiter_seq=seq)


def test_dl256_keeps_held_answers_per_bucket_kind() -> None:
    """DL-256: units a run's policy does not release stay HELD on a renewable
    resource and are SPENT on a depletable one. The machine load is always
    released, so a machine bucket never keeps units. A resource the catalog
    no longer types reads as renewable, so the units stay attributable to the
    job that can still release them."""
    pool = CapacityPool(lower_source(_SIZED))
    assert pool.keeps_held("m:m1") is False
    assert pool.keeps_held("r:RENEW") is True
    assert pool.keeps_held("r:DEPL") is False
    assert pool.keeps_held("r:DROPPED") is True


def test_dl247_has_priority_waiters_needs_a_sized_machine_or_resource() -> None:
    """DL-247, DL-255: only a queued job with a positive priority and a sized
    machine or resource can be held by a priority block. A queued job whose
    machine has no max_load (AutoSys's unlimited default) and that names no
    sized resource cannot, so the scan has nothing to run for it."""
    pool = CapacityPool(lower_source(_SIZED))
    assert pool.has_priority_waiters({"bare": _queued(1)}) is False
    assert pool.has_priority_waiters({"sized": _queued(1)}) is True
    # the first waiter is skipped, the second decides
    assert pool.has_priority_waiters({"bare": _queued(1), "sized": _queued(2)}) is True
    # not queued, zero priority, or unknown to the catalog: none of them counts
    assert pool.has_priority_waiters({"sized": JobRuntime()}) is False
    assert pool.has_priority_waiters({"unprioritised": _queued(1)}) is False
    assert pool.has_priority_waiters({"removed": _queued(1)}) is False


_MALFORMED = (
    "insert_machine: m1\ntype: a\nnode_name: m1\nmax_load: lots\n\n"
    "insert_resource: R\nres_type: R\namount: plenty\n\n"
    "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\njob_load: 3\n"
    "resources: (R, QUANTITY=1)\npriority: urgent\n\n"
    "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\njob_load: 3\n"
    "resources: (R, QUANTITY=1)\n"
)


def test_dl050_a_malformed_size_is_skipped_and_the_oracle_models_what_parses() -> None:
    """DL-50: a malformed max_load or amount is preflight's loud refusal, not
    the oracle's crash. Oracle-direct over a catalog that skipped preflight
    sizes neither bucket, so nothing throttles: both jobs start. A malformed
    priority reads as the vendor default, 0 (DL-247)."""
    catalog = lower_source(_MALFORMED)
    pool = CapacityPool(catalog)
    assert pool._bucket_cap == {}
    assert job_priority(catalog.jobs["a"]) == 0
    o = Oracle(catalog)
    for job in ("a", "b"):
        o.feed(Event(at=T0, kind="STARTJOB", payload={"job": job}))
    assert [o.store.job[j].status for j in ("a", "b")] == ["RUNNING", "RUNNING"]


def test_dl050_a_well_formed_size_is_modelled_and_throttles() -> None:
    """Twin: the same shape with parsable sizes queues the second job."""
    text = _MALFORMED.replace("lots", "5").replace("plenty", "1").replace("urgent", "1")
    o = Oracle(lower_source(text))
    for job in ("a", "b"):
        o.feed(Event(at=T0 + timedelta(minutes=1), kind="STARTJOB", payload={"job": job}))
    assert [o.store.job[j].status for j in ("a", "b")] == ["RUNNING", "QUE_WAIT"]


def test_dl050_merging_two_groups_on_one_bucket_keeps_the_held_policy() -> None:
    """A bucket any group holds is held, the units sum, and the policy is the
    one a group stated: None (a gate states none) never overrides one."""
    held = (2, "acquire", "completion")
    gate = (1, "gate", None)
    assert merge_requirements(held, gate) == (3, "acquire", "completion")
    assert merge_requirements(gate, held) == (3, "acquire", "completion")
    assert merge_requirements(gate, gate) == (2, "gate", None)
    assert merge_requirements(held, (1, "acquire", "never")) == (3, "acquire", "never")


def test_dl050_a_threshold_listed_twice_stays_a_gate_that_holds_nothing() -> None:
    """A threshold resource (res_type T) is check-only. Two groups on it fold
    to one gate with no release policy and a summed demand, which the pool
    tests at admission and never reserves."""
    refs = [ResourceRef(name="T1", quantity=1), ResourceRef(name="T1", quantity=2)]
    assert job_demand("T", refs, "A") == (3, "gate", None)
