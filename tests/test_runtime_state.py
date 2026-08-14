"""RuntimeState: the state owner the concurrency model needs (DL-86, stage S1b).

Normative spec: docs/concurrency-model.md ss3 (state ownership). S1b installs
the OWNER; S1c adds `state_rev` and the input transaction on top of it. What is
tested here is therefore structural -- who may write, what a write is, and
whether the authoritative state outside the rows is accounted for -- not the
AutoSys semantics, which the SEM trace tests and the bisimulation gate already
pin and which this refactor leaves byte-identical.

Three groups:

  * **the owner holds.** Rows are frozen, the maps do not escape, and the
    rebuild path VALIDATES -- `model_copy(update=)` does not, and a store that
    used it would accept a str in `run_number` and leave the corruption to
    surface somewhere else entirely.
  * **the verbs mean what they say.** Each typed operation is exercised for the
    fields it must change AND the fields it must leave alone; a verb that
    quietly clears a sibling field is the exact failure the generic
    `update(**fields)` it replaces made invisible.
  * **the inventory is accounted for** (concurrency-model ss3). `_box_ran`
    moved onto the box row and `_run_started_at` turned out to be write-only
    and is gone, so what remains outside the rows is `_CapacityPool` and its
    waiter order -- kept there deliberately, with the invariants that make the
    projection sound tested here rather than asserted in prose.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from dsl41.ir import lower_source
from dsl41.oracle import Event, GlobalRuntime, JobRuntime, Oracle, RuntimeState

T0 = datetime(2026, 7, 1, 8, 0)


def _ev(kind: str, minute: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minute), kind=kind, payload=payload)  # type: ignore[arg-type]


def _timer(job: str, due: datetime) -> Event:
    return Event(at=due, kind="TIMER", payload={"job": job})


# --------------------------------------------------------------- the owner holds


def test_the_rows_are_frozen() -> None:
    """The reason the owner can promise "one observable change per entity": a
    row cannot be edited in place, so every change is a replacement that goes
    through a verb. Without this, an aliased row reaches past every gate --
    `rt = store.runtime(j); rt.armed = True` -- and scripts/arch_check.py only
    catches the shapes it can see in the source."""
    with pytest.raises(ValidationError):
        JobRuntime().status = "RUNNING"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        GlobalRuntime(value="go").value = "stop"  # type: ignore[misc]


def test_the_maps_do_not_escape() -> None:
    """`store.job` publishes a read-only PROXY, not the map. A caller who
    writes through it fails loudly instead of diverging from the ledger --
    which is what "no mutable map escapes" has to mean at runtime, not just in
    a static check."""
    state = RuntimeState()
    state.set_flags("j", on_ice=True)
    state.set_global("G", "go")
    assert state.job is not state._jobs
    with pytest.raises(TypeError):
        state.job["j"] = JobRuntime()  # type: ignore[index]
    with pytest.raises(TypeError):
        state.globals_["G"] = GlobalRuntime(value="stop")  # type: ignore[index]


def test_the_rebuild_path_validates() -> None:
    """concurrency-model ss3 names the trap: `model_copy(update=)` does NOT
    validate. The owner rebuilds through the model's own constructor instead,
    so a wrong type is refused AT the write rather than read back later as a
    str where an int belongs."""
    state = RuntimeState()
    with pytest.raises(ValidationError):
        state.transition("j", "NOT_A_STATUS", T0)  # type: ignore[arg-type]
    assert state.runtime("j").status == "INACTIVE"  # refused, not half-applied
    # and the trap itself, so this test fails if the owner ever reverts to it
    assert JobRuntime().model_copy(update={"run_number": "seven"}).run_number == "seven"


def test_an_undeclared_field_is_refused() -> None:
    """A typo in a verb's field name must be loud rather than a silently
    created attribute nothing reads (the DL-82 property, kept)."""
    state = RuntimeState()
    with pytest.raises(ValidationError):
        state._replace("j", armd=True)


# ------------------------------------------------------------ the verbs mean it


def test_transition_latches_the_end_only_on_terminal() -> None:
    """`last_end_at` is the Q2 anchor -- the job's OWN last end (DL-54) -- so a
    non-terminal transition must not move it, and every terminal one must."""
    state = RuntimeState()
    state.transition("j", "RUNNING", T0)
    assert state.runtime("j").status_at == T0
    assert state.runtime("j").last_end_at is None
    state.transition("j", "SUCCESS", T0 + timedelta(minutes=5), exit_code=0)
    assert state.runtime("j").last_end_at == T0 + timedelta(minutes=5)
    assert state.runtime("j").exit_code == 0
    # a later non-terminal run does not clear the previous end
    state.transition("j", "RUNNING", T0 + timedelta(minutes=9))
    assert state.runtime("j").last_end_at == T0 + timedelta(minutes=5)


def test_transition_keeps_an_exit_code_it_was_not_given() -> None:
    """SEM-09: a status arriving with no exit code reports nothing about the
    code, so the recorded one stands. Passing None must not erase it."""
    state = RuntimeState()
    state.transition("j", "FAILURE", T0, exit_code=3)
    state.transition("j", "TERMINATED", T0 + timedelta(minutes=1))
    assert state.runtime("j").exit_code == 3


def test_start_run_is_one_act() -> None:
    """Everything an actual start changes, together: the run_number bump, the
    arm it consumes (Q3/DL-54), THIS run's provenance (DL-68), and the SEM-10
    box sets on both sides. They were four write sites; a missed one used to
    hide behind a sibling's write."""
    state = RuntimeState()
    state.set_armed("m", True)
    state.start_run("bx", cause="tick", box=None, is_box=True)
    state.start_run("m", cause="box 'bx' started", box="bx", is_box=False)
    assert state.runtime("m").run_number == 1
    assert state.runtime("m").armed is False
    assert state.runtime("m").started_by == "box 'bx' started"
    assert state.runtime("bx").ran_members == frozenset({"m"})


def test_a_box_start_resets_its_own_ran_set_and_joins_its_parents() -> None:
    """SEM-10 at-most-once is per box EXECUTION, so a fresh box run starts with
    an empty set -- and a nested box does both halves, to two different rows,
    in the right order (its own reset must not eat the entry it just made in
    its parent, nor vice versa)."""
    state = RuntimeState()
    state.start_run("inner", cause="c", box="outer", is_box=True)
    state.start_run("leaf", cause="c", box="inner", is_box=False)
    assert state.runtime("inner").ran_members == frozenset({"leaf"})
    assert state.runtime("outer").ran_members == frozenset({"inner"})
    state.start_run("inner", cause="rerun", box="outer", is_box=True)
    assert state.runtime("inner").ran_members == frozenset()  # fresh execution
    assert state.runtime("outer").ran_members == frozenset({"inner"})  # unaffected


def test_set_flags_leaves_the_flags_it_was_not_given() -> None:
    """The verb that replaced `update(**fields)` for SEM-20/21/22 must not turn
    "put this job on hold" into "and take it off ice while you are there"."""
    state = RuntimeState()
    state.set_flags("j", on_ice=True, on_hold=True, on_noexec=True)
    state.set_flags("j", on_hold=False)
    row = state.runtime("j")
    assert (row.on_ice, row.on_hold, row.on_noexec) == (True, False, True)


def test_a_verb_changes_nothing_else() -> None:
    """The general form of the two tests above, over every verb: whatever a
    verb is for, everything else on the row survives it."""
    state = RuntimeState()
    state.transition("j", "RUNNING", T0, exit_code=7)
    state.start_run("j", cause="tick", box=None, is_box=False)
    state.set_flags("j", on_ice=True)
    state.set_armed("j", True)
    row = state.runtime("j")
    assert (row.status, row.status_at, row.exit_code) == ("RUNNING", T0, 7)
    assert (row.run_number, row.started_by) == (1, "tick")
    assert (row.on_ice, row.armed) == (True, True)


# ------------------------------------------------------------------- the timers


def test_the_timer_token_orders_equal_time_timers_across_jobs() -> None:
    """concurrency-model ss3: the heap is ordered globally by (due, insertion
    token). Two timers on the SAME instant for DIFFERENT jobs are ordered by
    arming order, and that order decides resource release, box cascades and
    which job starts -- so it is state, not presentation."""
    state = RuntimeState()
    due = T0 + timedelta(minutes=10)
    second = state.enqueue_timer(due, _timer("b", due))
    first = state.enqueue_timer(due, _timer("a", due))  # armed later == fires later
    assert second < first
    assert [ev.payload["job"] for _, _, ev in state.timers()] == ["b", "a"]


def test_the_per_job_tokens_reproduce_the_global_firing_order() -> None:
    """Why the projection carries the ordering TOKEN and not a per-job set
    digest: sorting the union of the per-entity views must reproduce the heap
    exactly. A digest of each job's own timers cannot -- it says nothing about
    where they interleave with anyone else's."""
    state = RuntimeState()
    due = T0 + timedelta(minutes=10)
    later = T0 + timedelta(minutes=20)
    for job, at in (("a", due), ("b", due), ("a", later), ("c", due)):
        state.enqueue_timer(at, _timer(job, at))
    union = sorted((d, token, job) for job in ("a", "b", "c") for d, token in state.timers_for(job))
    assert [(d, token) for d, token, _ in union] == [(d, t) for d, t, _ in state.timers()]
    assert [job for _, _, job in union] == ["a", "b", "c", "a"]


def test_popping_due_timers_drains_in_firing_order_and_stops_at_the_horizon() -> None:
    state = RuntimeState()
    early, late = T0 + timedelta(minutes=1), T0 + timedelta(minutes=30)
    state.enqueue_timer(late, _timer("late", late))
    state.enqueue_timer(early, _timer("early", early))
    assert state.next_timer_due() == early
    popped = state.pop_timer_due(T0 + timedelta(minutes=10))
    assert popped is not None and popped[1].payload["job"] == "early"
    assert state.pop_timer_due(T0 + timedelta(minutes=10)) is None  # `late` is not due
    assert state.next_timer_due() == late


def test_a_deadline_arms_on_a_tick_that_changes_no_row_at_all() -> None:
    """The reason the heap is authoritative state and not a cache of the rows.
    SEM-34 arms the must_start deadline on every schedule tick, succeed or not;
    SEM-32 arms the JOB only on the first, because the second finds it already
    armed. So the second tick leaves the row byte-identical and still moves the
    schedule -- a projection over rows alone would replay a different one."""
    o = Oracle(
        lower_source(
            "insert_job: ms\njob_type: c\ncommand: x\n"
            'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
            "must_start_times: +30\ncondition: s(never_defined)\n\n"
        )
    )
    o.feed(_ev("STARTJOB", 0, job="ms"))  # condition false -> arms, no run (Q3/DL-54)
    after_first = dict(o.store.job["ms"])
    assert after_first["armed"] is True
    assert [kind for _, _, kind in o.pending_timers()] == ["must_start"]

    o.feed(_ev("STARTJOB", 5, job="ms"))
    assert dict(o.store.job["ms"]) == after_first  # the row is untouched -- ENTIRELY
    assert [kind for _, _, kind in o.pending_timers()] == ["must_start", "must_start"]


# -------------------------------------------------------------- the inventory


_POOL_JIL = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: q1\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: q2\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: q3\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
)


def _pool_invariants(o: Oracle) -> list[str]:
    """The two invariants that let `_CapacityPool` stay outside the rows
    (concurrency-model ss3 gives the owner this choice explicitly): its
    membership is a FUNCTION of the projected statuses, so no pool change can
    happen without an accompanying row change to carry it."""
    broken = []
    queued = {j for j, rt in o.store.job.items() if rt.status == "QUE_WAIT"}
    if set(o._pool._waiters) != queued:
        broken.append(f"waiters {sorted(o._pool._waiters)} != QUE_WAIT jobs {sorted(queued)}")
    for job in list(o.store.job):
        if o._pool.holds(job) and o.store.job[job].status not in ("STARTING", "RUNNING"):
            broken.append(f"{job} holds units at status {o.store.job[job].status}")
    return broken


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_the_capacity_pool_never_changes_without_a_row_change(data: st.DataObject) -> None:
    """`_CapacityPool` and its waiter order are the state that did NOT move
    onto the rows, so the model owes them a tested invariant instead: every
    queued job is QUE_WAIT and every QUE_WAIT job is queued, and only a job
    that is starting or running holds units. Both directions hold after EVERY
    event of a random contended schedule -- so a pool mutation with no
    projected row change is not constructible here."""
    o = Oracle(lower_source(_POOL_JIL))
    jobs = ["q1", "q2", "q3"]
    minute = 0.0
    for _ in range(data.draw(st.integers(min_value=2, max_value=12))):
        job = data.draw(st.sampled_from(jobs))
        kind = data.draw(
            st.sampled_from(["STARTJOB", "STATUS", "KILLJOB", "ON_ICE", "OFF_ICE", "ON_HOLD"])
        )
        payload: dict[str, object] = {"job": job}
        if kind == "STATUS":
            payload["status"] = data.draw(st.sampled_from(["SUCCESS", "FAILURE"]))
            if o.store.job[job].status not in ("STARTING", "RUNNING"):
                continue  # the runner never reports a completion for a job that never ran
        o.feed(_ev(kind, minute, **payload))
        minute += 1
        assert _pool_invariants(o) == []


def test_the_waiter_order_is_the_order_of_the_que_wait_transitions() -> None:
    """Why the waiter order needs no ordering token in the projection while the
    timer heap does: a waiter's rank is fixed at its QUE_WAIT transition, which
    IS a projected row change, so replaying the transitions replays the order.
    A timer can be armed with no row change at all -- hence the token."""
    o = Oracle(lower_source(_POOL_JIL))
    for minute, job in enumerate(["q3", "q1", "q2"]):
        o.feed(_ev("STARTJOB", minute, job=job))
    assert o.store.job["q3"].status == "RUNNING"  # took the single unit
    queued_in_trace = [e.job for e in o.trace() if e.transition.endswith("->QUE_WAIT")]
    assert queued_in_trace == ["q1", "q2"]
    assert o._pool.sorted_waiters() == queued_in_trace


def test_box_membership_bookkeeping_lives_on_the_box_row() -> None:
    """`_box_ran` was a loose map beside the rows; it is `JobRuntime.ran_members`
    now, so SEM-10's at-most-once bookkeeping is projected with the entity it
    describes instead of needing an invariant of its own."""
    o = Oracle(
        lower_source(
            "insert_job: bx\njob_type: b\n\n"
            "insert_job: m1\njob_type: c\ncommand: x\nbox_name: bx\n\n"
            "insert_job: m2\njob_type: c\ncommand: x\nbox_name: bx\ncondition: s(m1)\n\n"
        )
    )
    o.feed(_ev("STARTJOB", 0, job="bx"))
    assert o.store.job["bx"].ran_members == frozenset({"m1"})  # m2 gated on s(m1)
    o.feed(_ev("STATUS", 1, job="m1", status="SUCCESS"))
    assert o.store.job["bx"].ran_members == frozenset({"m1", "m2"})
    o.feed(_ev("STATUS", 2, job="m2", status="SUCCESS"))
    assert o.store.job["bx"].status == "SUCCESS"
    o.feed(_ev("FORCE_STARTJOB", 3, job="bx"))
    assert o.store.job["bx"].ran_members == frozenset({"m1"})  # fresh execution
