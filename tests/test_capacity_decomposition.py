"""DL-120: the capacity state moved onto the entities it describes.

`_bucket_used` summed two facts with different lifetimes -- units HELD by live
runs and units permanently SPENT -- and `release()` decremented only the first
while `_held.pop` dropped the record unconditionally. A depletable's spent
units were then in no row at all, so a seal that recomputed usage from its
holders would refill every quota it carried (period-model ss5).

The obligations this file holds the decomposition to: PR-19 (spent units
survive a release), PR-19a (a ghost bucket survives its resource's removal),
PR-20 (a run releases the vector it ACQUIRED, not what the catalog says it
wants by then), PR-21 (waiter order is on the rows), and PR-52 (the ownership
gate covers the new state). The four invariants `RuntimeState` enforces get a
case each, both directions.
"""

from __future__ import annotations

import importlib.util
import sys

from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from dsl41.capacity import CapacityPool
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CapacityReservation, Event, JobRuntime, OracleError, RuntimeState

T0 = datetime(2026, 7, 1, 8, 0)


def _ev(kind: str, minute: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minute), kind=kind, payload=payload)  # type: ignore[arg-type]


def _fuel_jil(quantity: int = 3, *, amount: int = 10, resource: bool = True) -> str:
    """A depletable and the jobs that draw on it. `resource=False` is the
    re-baseline that removes it."""
    head = f"insert_resource: FUEL\nres_type: D\namount: {amount}\n\n" if resource else ""
    return (
        f"{head}"
        f"insert_job: burn\njob_type: c\ncommand: x\nmachine: m1\n"
        f"resources: (FUEL, QUANTITY={quantity})\n\n"
        "insert_job: eight\njob_type: c\ncommand: x\nmachine: m1\nresources: (FUEL, QUANTITY=8)\n\n"
        "insert_job: seven\njob_type: c\ncommand: x\nmachine: m1\nresources: (FUEL, QUANTITY=7)\n"
    )


def _fits(o: Oracle, job: str) -> bool:
    """Would `job`'s full demand be admitted right now?"""
    return o._pool.can_admit(
        o._pool.demand_vector(o.catalog.jobs[job]), o.store.job, o.store.consumed
    )


# ------------------------------------------------------------------ the carry


def test_pr19_depletable_spent_units_survive_release() -> None:
    """The period-model ss5 reproduction. A depletable's units are gone the
    moment they are acquired, so a release must move them somewhere a row can
    be rebuilt from -- `consumed` -- and not simply drop the holder."""
    o = Oracle(lower_source(_fuel_jil(3)))
    o.feed(_ev("STARTJOB", 0, job="burn"))
    assert o.store.job["burn"].reservations == (
        CapacityReservation(bucket="r:FUEL", units=3, release_policy="never"),
    )
    assert dict(o.store.consumed) == {}  # held, not yet spent

    o.feed(_ev("STATUS", 1, job="burn", status="SUCCESS"))
    assert dict(o.store.consumed) == {"r:FUEL": 3}
    assert o.store.job["burn"].reservations == ()
    # 10 - 3 = 7 free: the quota did NOT come back with the holder's row
    assert _fits(o, "eight") is False
    assert _fits(o, "seven") is True


def test_pr19a_ghost_bucket_survives_removal_and_reintroduction() -> None:
    """`consumed` keys survive their resource (period-model ss3.3). A loader
    that rebuilt capacity from the catalog alone would refund the spend the
    day the resource came back."""
    o = Oracle(lower_source(_fuel_jil(3)))
    o.feed(_ev("STARTJOB", 0, job="burn"))
    o.feed(_ev("STATUS", 1, job="burn", status="SUCCESS"))
    spent = dict(o.store.consumed)
    assert spent == {"r:FUEL": 3}

    # the resource is gone from the catalog: the key is kept, and reading
    # usage over it neither raises nor drops the 3
    without = CapacityPool(lower_source("insert_job: idle\njob_type: c\ncommand: x\n"))
    assert without.used({}, spent) == {"r:FUEL": 3}
    assert "r:FUEL" not in without._bucket_cap

    # and when FUEL returns, the 3 are still spent
    reopened = Oracle(lower_source(_fuel_jil(3)))
    reopened.store.seed_consumed(spent)
    assert _fits(reopened, "eight") is False
    assert _fits(reopened, "seven") is True


def test_pr20_release_uses_the_acquired_vector_not_the_current_definition() -> None:
    """The vector is frozen at acquisition. A re-baseline that raises the
    job's QUANTITY must not make the live run release -- or spend -- more than
    it took."""
    o = Oracle(lower_source(_fuel_jil(3)))
    o.feed(_ev("STARTJOB", 0, job="burn"))
    assert o.store.job["burn"].reservations[0].units == 3

    rebaselined = lower_source(_fuel_jil(5))  # the job now asks for 5
    o.catalog = rebaselined
    o._pool = CapacityPool(rebaselined)
    assert o._pool.demand_vector(o.catalog.jobs["burn"])[0][1] == 5

    o.feed(_ev("STATUS", 1, job="burn", status="SUCCESS"))
    assert dict(o.store.consumed) == {"r:FUEL": 3}  # what it took, never what it now wants


def _queue_jil() -> str:
    return (
        "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: hold\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: wa\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: wb\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: wp\njob_type: c\ncommand: x\nmachine: m1\npriority: 1\n"
        "resources: (LOCK, QUANTITY=1)\n"
    )


def test_pr21_waiter_order_is_on_the_rows() -> None:
    """Admission ORDER is `(priority, waiter_seq, name)` read off the rows, so
    it survives anything that rebuilds the pool -- which a seal's opening
    does."""
    o = Oracle(lower_source(_queue_jil()))
    for minute, job in enumerate(["hold", "wa", "wb", "wp"]):
        o.feed(_ev("STARTJOB", minute, job=job))
    assert o.store.job["hold"].status == "RUNNING"
    assert [o.store.job[j].waiter_seq for j in ("wa", "wb", "wp")] == [1, 2, 3]
    order = o._pool.sorted_waiters(o.store.job)
    assert order == ["wp", "wa", "wb"]  # priority first, then the enqueue rank

    # a pool built fresh from the same catalog holds no order of its own and
    # reproduces this one exactly
    assert CapacityPool(o.catalog).sorted_waiters(o.store.job) == order

    o.feed(_ev("KILLJOB", 5, job="wa"))
    assert o.store.job["wa"].waiter_seq is None
    assert o.store.job["wb"].waiter_seq == 2  # untouched by its neighbour leaving
    assert o._pool.sorted_waiters(o.store.job) == ["wp", "wb"]


def test_sorted_waiters_tolerates_a_waiter_absent_from_the_catalog() -> None:
    """A waiter whose job the catalog no longer defines sorts LAST rather than
    raising KeyError. period-model ss10 classifies QUE_WAIT-and-removed R, so
    the boundary refuses first; this is the floor under that gate."""
    pool = CapacityPool(
        lower_source("insert_job: known\njob_type: c\ncommand: x\nmachine: m1\npriority: 5\n")
    )
    rows = {
        "ghost": JobRuntime(status="QUE_WAIT", waiter_seq=1),
        "known": JobRuntime(status="QUE_WAIT", waiter_seq=2),
    }
    assert pool.sorted_waiters(rows) == ["known", "ghost"]


# -------------------------------------------------------------- the invariants


def _live_reservation() -> CapacityReservation:
    return CapacityReservation(bucket="r:FUEL", units=1, release_policy="completion")


def test_reservations_only_while_starting_or_running() -> None:
    store = RuntimeState()
    store.begin_input()
    store.reserve("j", [_live_reservation()])
    store.transition("j", "RUNNING", T0)
    assert store.commit_input() == ["job:j"]
    assert store.job["j"].reservations == (_live_reservation(),)

    store.begin_input()
    store.transition("j", "SUCCESS", T0)  # the release that must accompany it is missing
    with pytest.raises(OracleError, match="holds capacity at status SUCCESS"):
        store.commit_input()


def test_waiter_seq_iff_que_wait() -> None:
    store = RuntimeState()
    store.begin_input()
    store.enqueue_waiter("j")
    store.transition("j", "QUE_WAIT", T0)
    store.commit_input()
    assert store.job["j"].waiter_seq == 1

    store.begin_input()  # a rank with no queue
    store.enqueue_waiter("k")
    with pytest.raises(OracleError, match="a rank is held exactly while QUE_WAIT"):
        store.commit_input()

    store.begin_input()  # ...and a queue with no rank
    store.transition("m", "QUE_WAIT", T0)
    with pytest.raises(OracleError, match="a rank is held exactly while QUE_WAIT"):
        store.commit_input()


def test_start_may_not_overwrite_reservations() -> None:
    """The old pool EXTENDED the held record, so a missed release became a
    permanently stranded unit. Refusing makes the same bug loud at the write
    that would have caused it."""
    store = RuntimeState()
    store.begin_input()
    store.reserve("j", [_live_reservation()])
    with pytest.raises(OracleError, match="a start may not overwrite"):
        store.reserve("j", [_live_reservation()])

    # the release makes the row reservable again, which is the only way back
    store.transition("j", "SUCCESS", T0)
    store.release_reservations("j", "SUCCESS")
    store.reserve("j", [_live_reservation()])
    store.transition("j", "RUNNING", T0)
    store.commit_input()
    assert store.job["j"].reservations == (_live_reservation(),)


def test_consumed_never_negative() -> None:
    """A seal with `consumed["r:FUEL"] = -3` would open with invented capacity
    (PR-22). Refused at the seed, and again at every commit."""
    store = RuntimeState()
    store.seed_consumed({"r:FUEL": 3})
    assert dict(store.consumed) == {"r:FUEL": 3}
    with pytest.raises(OracleError, match="cannot be negative"):
        store.seed_consumed({"r:FUEL": -3})

    store._consumed["r:FUEL"] = -3  # reaching past the owner, which is the case
    store.begin_input()
    store.set_armed("j", True)
    with pytest.raises(OracleError, match="cannot be negative"):
        store.commit_input()


def test_enqueue_counter_bounds_waiter_seq() -> None:
    store = RuntimeState()
    for job in ("a", "b"):
        store.begin_input()
        store.enqueue_waiter(job)
        store.transition(job, "QUE_WAIT", T0)
        store.commit_input()
    assert store.enqueue_counter == 2
    assert max(store.job[j].waiter_seq or 0 for j in ("a", "b")) == store.enqueue_counter

    store.begin_input()  # dequeuing does not give the rank back
    store.dequeue_waiter("b")
    store.transition("b", "INACTIVE", T0)
    store.commit_input()
    assert store.enqueue_counter == 2

    store._enqueue_counter = 0  # a carried counter below the ranks it allocated
    store.begin_input()
    store.set_armed("a", True)
    with pytest.raises(OracleError, match="above the allocator's"):
        store.commit_input()


def test_projection_moves_with_reservations_and_waiter_seq() -> None:
    """Both fields are projected by default, and both change at the moments
    `status` already does -- so a plain start-and-complete moves the revision
    exactly as often for a job that acquires as for one that does not."""
    o = Oracle(
        lower_source(
            _fuel_jil(3) + "\ninsert_job: plain\njob_type: c\ncommand: x\n",
        )
    )
    assert o.store.job["burn"].state_rev == 0

    o.feed(_ev("STARTJOB", 0, job="burn"))
    o.feed(_ev("STARTJOB", 0, job="plain"))
    assert o.store.job["burn"].state_rev == 1  # one input, one increment
    assert o.store.job["burn"].reservations != ()

    o.feed(_ev("STATUS", 1, job="burn", status="SUCCESS"))
    o.feed(_ev("STATUS", 1, job="plain", status="SUCCESS"))
    assert o.store.job["burn"].state_rev == 2
    assert o.store.job["burn"].reservations == ()
    # the acquiring job costs no revision the resource-free one does not
    assert o.store.job["burn"].state_rev == o.store.job["plain"].state_rev


# ------------------------------------------- the edges the invariants exposed
#
# Two transitions kept capacity bookkeeping the old pool could not see, because
# a map beside the rows can hold a fact no row contradicts. Both are reachable
# only by an injected STATUS -- an operator or an adapter reporting something
# the interpreter did not decide -- and both are pinned deterministically here
# rather than left to the CM-03 property that found them.


def _one_lock_jil() -> str:
    return (
        "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: hold\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: wq\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n"
    )


def test_a_queued_job_completed_out_of_band_leaves_the_queue() -> None:
    """A rank left on a terminal row put the job back in the admission queue,
    where the next release started it -- from SUCCESS."""
    o = Oracle(lower_source(_one_lock_jil()))
    o.feed(_ev("STARTJOB", 0, job="hold"))
    o.feed(_ev("STARTJOB", 0, job="wq"))
    assert o.store.job["wq"].status == "QUE_WAIT"

    o.feed(_ev("STATUS", 1, job="wq", status="SUCCESS"))
    assert o.store.job["wq"].waiter_seq is None
    assert o._pool.sorted_waiters(o.store.job) == []

    o.feed(_ev("STATUS", 2, job="hold", status="SUCCESS"))  # frees the lock
    assert o.store.job["wq"].status == "SUCCESS"
    assert o.store.job["wq"].run_number == 0  # never started, then or now


def test_a_live_holder_reset_to_inactive_releases_its_units() -> None:
    """Reservations exist exactly while STARTING or RUNNING, so the release
    edge is LEAVING those statuses, not reaching a terminal one. The units used
    to strand in a `_held` record no row could see."""
    o = Oracle(lower_source(_one_lock_jil()))
    o.feed(_ev("STARTJOB", 0, job="hold"))
    o.feed(_ev("STARTJOB", 0, job="wq"))
    assert o.store.job["wq"].status == "QUE_WAIT"

    o.feed(_ev("STATUS", 1, job="hold", status="INACTIVE"))
    assert o.store.job["hold"].reservations == ()
    assert o.store.job["wq"].status == "RUNNING"  # the freed unit woke the waiter
    assert dict(o.store.consumed) == {}  # a `completion` release frees, never spends


# ------------------------------------------------------------------- the gate


def _arch_check() -> ModuleType:
    """scripts/ is not a package; the gate is loaded by path, as CI runs it."""
    spec = importlib.util.spec_from_file_location(
        "arch_check", Path(__file__).resolve().parent.parent / "scripts" / "arch_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["arch_check"] = module
    spec.loader.exec_module(module)
    return module


def test_pr52_ownership_gate_covers_the_new_state(tmp_path: Path) -> None:
    """PR-52: the DL-83 gate watches the DL-120 state too. `reservations` and
    `waiter_seq` are `JobRuntime` fields, so the derived watch set picks them
    up for free; `CapacityReservation` joins the model list; and `consumed`
    and `enqueue_counter` join the containers a caller could reach through --
    the counter by rebind, since a scalar has nothing to subscript."""
    arch_check = _arch_check()

    rows = tmp_path / "oracle_state.py"
    rows.write_text(
        "class CapacityReservation(BaseModel):\n    units: int\n\n\n"
        "class JobRuntime(BaseModel):\n"
        "    reservations: tuple = ()\n"
        "    waiter_seq: int | None = None\n\n\n"
        "def poke(row, res):\n"
        "    row.reservations = ()\n"
        "    row.waiter_seq = 4\n"
        "    res.units = 99\n",
        encoding="utf-8",
    )
    messages = [f.message for f in arch_check.state_owner_bypasses([rows])]
    assert any("JobRuntime.reservations" in m for m in messages), messages
    assert any("JobRuntime.waiter_seq" in m for m in messages), messages
    assert any("CapacityReservation.units" in m for m in messages), messages

    outside = tmp_path / "runner.py"
    outside.write_text(
        "def poke(engine):\n"
        "    engine.oracle.store._consumed['r:FUEL'] = 0\n"
        "    engine.oracle.store.consumed['r:FUEL'] = 0\n"
        "    engine.oracle.store._enqueue_counter = 0\n"
        "    engine.oracle.store._consumed.clear()\n",
        encoding="utf-8",
    )
    kinds = sorted({f.message.split(":")[0] for f in arch_check.state_owner_bypasses([outside])})
    assert kinds == [
        "mutates store._consumed through .clear()",
        "rebinds store._enqueue_counter directly",
        "writes store._consumed directly",
        "writes store.consumed directly",
    ]

    # the alias idiom every runner module uses, and a private name reached
    # off anything at all -- both were invisible to a gate that matched only
    # the dotted `<x>.store.<name>` form (U1 review)
    aliased = tmp_path / "aliased.py"
    aliased.write_text(
        "def poke(engine, other):\n"
        "    st = engine.oracle.store\n"
        "    st._consumed.update({'r:FUEL': 0})\n"
        "    st.consumed['r:FUEL'] = 0\n"
        "    other._consumed['r:FUEL'] = 1\n",  # NOT an owner: an unrelated private field
        encoding="utf-8",
    )
    kinds = sorted({f.message.split(":")[0] for f in arch_check.state_owner_bypasses([aliased])})
    assert kinds == [
        "mutates store._consumed through .update()",
        "writes store.consumed directly",
    ]

    # ...and the owner's own body still writes all of it
    owner = tmp_path / "owner.py"
    owner.write_text(
        "class JobRuntime(BaseModel):\n    reservations: tuple = ()\n\n\n"
        "class RuntimeState:\n"
        "    def reserve(self, job, res):\n"
        "        self._consumed[job] = 0\n"
        "        self._enqueue_counter = 1\n"
        "        self.job[job].reservations = res\n",
        encoding="utf-8",
    )
    assert arch_check.state_owner_bypasses([owner]) == []
