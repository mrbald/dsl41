"""RuntimeState: the state owner and its revisions (DL-86/87, stages S1b+S1c).

Normative spec: docs/concurrency-model.md ss3 (state ownership and
`state_rev`) plus ss6 (reads). S1b installed the OWNER; S1c puts a revision on
every entity and one input transaction around every input. What is tested here
is therefore structural -- who may write, what a write is, what counts as a
change, and whether the authoritative state outside the rows is accounted for
-- not the AutoSys semantics, which the SEM trace tests and the bisimulation
gate already pin and which both stages leave byte-identical.

Four groups:

  * **the owner holds.** Rows are frozen, the maps do not escape, and the
    rebuild path VALIDATES -- `model_copy(update=)` does not, and a store that
    used it would accept a str in `run_number` and leave the corruption to
    surface somewhere else entirely.
  * **the verbs mean what they say.** Each typed operation is exercised for the
    fields it must change AND the fields it must leave alone; a verb that
    quietly clears a sibling field is the exact failure the generic
    `update(**fields)` it replaces made invisible.
  * **the inventory is accounted for** (ss3). `_box_ran` moved onto the box row
    and `_run_started_at` turned out to be write-only and is gone, so what
    remains outside the rows is `_CapacityPool` and its waiter order -- kept
    there deliberately, with the invariants that make the projection sound
    tested here rather than asserted in prose.
  * **the revision is the projection's** (CM-02/CM-03). One increment per
    entity per input, and only when the SEMANTIC projection moved -- checked
    both as worked cases and as a property over a widened generator, whose
    expectation is recomputed from the PUBLIC row and heap so it is an
    independent oracle rather than a restatement of `_projection`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, GlobalRuntime, JobRuntime, OracleError, RuntimeState

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
    fields = dict(o.store.job["ms"])
    revision = fields.pop("state_rev")
    assert fields == {k: v for k, v in after_first.items() if k != "state_rev"}
    assert [kind for _, _, kind in o.pending_timers()] == ["must_start", "must_start"]
    # ...and the revision moved anyway (DL-87), because the HEAP is projected.
    # Without the timer half of the projection this input would be invisible to
    # an `expect`, and a client that read revision 1 would win a precondition
    # against a run whose schedule it has never seen.
    assert revision == after_first["state_rev"] + 1


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


# --------------------------------------------------- the revision (CM-02, CM-03)


def _independent_projection(o: Oracle, key: str) -> object:
    """What the revision is a revision OF, recomputed from the PUBLIC surface.

    Deliberately NOT `store._projection`: a property test that asks the
    implementation what changed can only prove the implementation agrees with
    itself. This reads the row through `store.job` and the heap through
    `timers_for`, drops `state_rev` (a revision that counted itself would
    justify its own next increment), and is otherwise field-complete by
    construction -- so a field added to JobRuntime is projected here too, and
    an implementation that forgot it fails."""
    namespace, _, name = key.partition(":")
    if namespace == "job":
        row = o.store.job.get(name) or JobRuntime()
        fields = {k: v for k, v in dict(row).items() if k != "state_rev"}
        return (
            tuple(sorted(fields.items(), key=lambda kv: kv[0])),
            tuple(o.store.timers_for(name)),
        )
    grow = o.store.globals_.get(name)
    return None if grow is None else grow.value


def _all_keys(o: Oracle) -> list[str]:
    return [f"job:{j}" for j in o.store.job] + [f"global:{g}" for g in o.store.globals_]


def test_cm02_one_input_that_changes_many_things_increments_each_entity_once() -> None:
    """The cardinality obligation. Starting a box drives a long cascade -- the
    box transitions twice (STARTING then RUNNING), its members start, the box
    row's ran set is rewritten once per member -- and every entity it touched
    must come out exactly one revision higher. Per-write increments would put
    the box at five or six and make `expect` unusable for anything but a
    single-transition input."""
    o = Oracle(
        lower_source(
            "insert_job: bx\njob_type: b\n\n"
            "insert_job: m1\njob_type: c\ncommand: x\nbox_name: bx\n\n"
            "insert_job: m2\njob_type: c\ncommand: y\nbox_name: bx\n\n"
        )
    )
    before = {key: o.store.revision(key) for key in _all_keys(o)}
    o.feed(_ev("STARTJOB", 0, job="bx"))
    assert o.store.job["bx"].status == "RUNNING"
    assert o.store.job["m1"].status == "RUNNING"  # the cascade really ran
    after = {key: o.store.revision(key) for key in _all_keys(o)}
    assert after == {key: rev + 1 for key, rev in before.items()}


_TERM_JIL = "insert_job: x\njob_type: c\ncommand: sleep 300\nterm_run_time: 1\n"


def test_cm02_a_batch_is_one_input_even_though_it_has_two_halves() -> None:
    """The committed input of ss3's cardinality rule is the ss4 BATCH -- the
    time observation and the attempt together -- not one call into the oracle.

    Here the deadline fires and the attempt lands on the same job, so a shell
    that applied the halves as two calls would put x two revisions on for one
    admitted input, and a client's `expect` would name a revision that no read
    of that job ever returned. The paired assertion below is the contrast:
    advance()-then-feed() really does move it twice, so the batch is doing
    work rather than restating the default."""
    o = Oracle(lower_source(_TERM_JIL))
    o.feed(_ev("STARTJOB", 0, job="x"))
    before = o.store.revision("job:x")

    at = T0 + timedelta(minutes=1)
    with o.batch(at) as batch:
        assert o.store.job["x"].status == "TERMINATED"  # the deadline fired on entry
        batch.feed(Event(at=at, kind="STATUS", payload={"job": "x", "status": "SUCCESS"}))
    assert o.store.job["x"].status == "SUCCESS"  # and the attempt landed after it
    assert o.store.revision("job:x") == before + 1
    assert batch.revisions == {"job:x": before + 1}  # the ss4 step-7 record

    twice = Oracle(lower_source(_TERM_JIL))
    twice.feed(_ev("STARTJOB", 0, job="x"))
    base = twice.store.revision("job:x")
    twice.advance(at)
    twice.feed(Event(at=at, kind="STATUS", payload={"job": "x", "status": "SUCCESS"}))
    assert twice.store.revision("job:x") == base + 2


def test_a_batch_that_raises_mid_attempt_still_commits_what_it_changed() -> None:
    """DL-87's no-rollback rule, now that the commit sits in a context
    manager: the oracle cannot undo what the time half already did, so a
    failing attempt must not take the deadline's revision down with it. A
    reader still holding the pre-input revision has to be invalidated by the
    kill that really happened."""
    o = Oracle(lower_source(_TERM_JIL))
    o.feed(_ev("STARTJOB", 0, job="x"))
    before = o.store.revision("job:x")
    at = T0 + timedelta(minutes=1)
    with pytest.raises(OracleError):
        with o.batch(at) as batch:
            batch.feed(Event(at=at + timedelta(minutes=1), kind="STATUS", payload={"job": "x"}))
    assert o.store.job["x"].status == "TERMINATED"
    assert o.store.revision("job:x") == before + 1
    o.store.begin_input()  # the transaction really was closed, not left open
    o.store.commit_input()


def test_cm02_an_input_that_changes_nothing_increments_nothing() -> None:
    """The other half of cardinality, and the one a per-write implementation
    gets wrong for free: a refused start and a same-value SET_GLOBAL are real
    inputs that leave the state where it was. If they moved a revision, every
    idle poll would invalidate every outstanding precondition."""
    o = Oracle(
        lower_source(
            "insert_global: G\nvalue: go\n\n"
            "insert_job: gated\njob_type: c\ncommand: x\ncondition: s(never_defined)\n\n"
        )
    )
    before = {key: o.store.revision(key) for key in _all_keys(o)}
    o.feed(_ev("STARTJOB", 0, job="gated"))  # condition false, no schedule -> no arm
    o.feed(_ev("SET_GLOBAL", 1, name="G", value="go"))  # SEM-06 same-value edge
    assert o.store.job["gated"].status == "INACTIVE"
    assert {key: o.store.revision(key) for key in _all_keys(o)} == before


def test_cm02_the_revision_is_not_part_of_its_own_projection() -> None:
    """Two states that differ ONLY in their revision have the same projection.

    ss3 excludes `state_rev` "else it justifies itself". Worth being exact
    about what that buys today: with the bump applied AFTER the commit-time
    comparison, a projected revision could not yet have moved within its own
    input, so no self-justifying loop is reachable right now and this
    exclusion is defensive, not load-bearing -- which is the honest claim,
    and the reason the test is written against `_projection` directly rather
    than dressed up as a behaviour it cannot actually produce.

    It protects the NEXT reader. `_projection` is the natural semantic digest
    for S2's ApplyResult, and a digest that moves for two semantically
    identical states is a false conflict wherever it is compared."""
    state = RuntimeState()
    state.transition("j", "RUNNING", T0)
    plain = state._projection("job:j")
    state._replace("j", state_rev=41)
    assert state.runtime("j").state_rev == 41  # the field moved...
    assert state._projection("job:j") == plain  # ...and the projection did not


def test_an_input_that_touches_another_entity_leaves_yours_alone() -> None:
    """The revision is per ENTITY, not per input: a busy estate must not
    invalidate every outstanding precondition on every unrelated event."""
    o = Oracle(
        lower_source(
            "insert_job: mine\njob_type: c\ncommand: x\n\n"
            "insert_job: theirs\njob_type: c\ncommand: y\n\n"
        )
    )
    o.feed(_ev("STATUS", 0, job="mine", status="SUCCESS"))
    settled = o.store.revision("job:mine")
    o.feed(_ev("STATUS", 1, job="theirs", status="SUCCESS"))
    o.feed(_ev("SET_GLOBAL", 2, name="UNRELATED", value="x"))
    assert o.store.revision("job:mine") == settled
    assert o.store.revision("job:theirs") == 1


def test_a_global_is_absent_at_revision_zero_and_exists_from_one() -> None:
    """ss6: a conditional create has to be able to condition on ABSENCE, and
    `expect {"global:X": 0}` is how -- which only works if nothing that exists
    is ever at 0. The catalog seed is an input for exactly this reason: a
    DECLARED global is at 1 from genesis, not sharing 0 with the undeclared."""
    o = Oracle(lower_source("insert_global: DECLARED\nvalue: go\n\n"))
    assert o.store.revision("global:DECLARED") == 1
    assert o.store.revision("global:NEVER_SET") == 0
    o.feed(_ev("SET_GLOBAL", 0, name="NEVER_SET", value="now"))
    assert o.store.revision("global:NEVER_SET") == 1
    assert o.store.global_value("NEVER_SET") == "now"


def test_inputs_do_not_nest() -> None:
    """One input, one revision. A nested transaction would let an inner commit
    publish half an input's changes at a revision no reader should ever see."""
    state = RuntimeState()
    state.begin_input()
    with pytest.raises(OracleError, match="do not nest"):
        state.begin_input()


def test_commit_names_the_changed_entities() -> None:
    """S2's outbox and ApplyResult need the changed SET, not just the effect
    of having changed it, and stably ordered so a replay writes the same
    record."""
    state = RuntimeState()
    state.begin_input()
    state.transition("b", "RUNNING", T0)
    state.set_global("G", "go")
    state.set_armed("a", True)
    state.set_armed("untouched", False)  # already False: written, but not CHANGED
    assert state.commit_input() == ["global:G", "job:a", "job:b"]


_WIDE_JIL = (
    "insert_global: FLAG\nvalue: go\n\n"
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: wbox\njob_type: b\n\n"
    "insert_job: wm1\njob_type: c\ncommand: x\nbox_name: wbox\nmachine: m1\n"
    "resources: (R, QUANTITY=1)\nterm_run_time: 10\n\n"
    "insert_job: wm2\njob_type: c\ncommand: y\nbox_name: wbox\nmachine: m1\n"
    "resources: (R, QUANTITY=1)\ncondition: v(FLAG) = go\n\n"
    "insert_job: wsolo\njob_type: c\ncommand: z\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    "must_start_times: +7\ncondition: s(wm1)\n\n"
)
_WIDE_JOBS = ["wbox", "wm1", "wm2", "wsolo"]


@settings(max_examples=250, deadline=None)
@given(data=st.data())
def test_cm03_every_projection_change_moves_exactly_one_revision(data: st.DataObject) -> None:
    """CM-03, with the generator widened past the STATUS / SET_GLOBAL focus
    the earlier bisimulation property had: the full status vocabulary, the six
    out-of-band verbs, forced and scheduled starts, kills, and standalone time
    advances that fire real `term_run_time` and `must_start` timers -- over a
    catalog with a box, a contended resource and a global-gated member, so
    cascades, QUE_WAIT and box folds all occur.

    After EVERY input, for EVERY entity: the revision moved by exactly one if
    the independently recomputed projection moved, and not at all otherwise.
    The safety direction (a change that moves no revision) is the one that
    loses a conflict, and it is the reason this is a property and not a
    handful of cases -- but the cardinality direction is asserted just as
    hard, because a double increment is a false conflict forever."""
    o = Oracle(lower_source(_WIDE_JIL))
    minute = 0.0
    for _ in range(data.draw(st.integers(min_value=1, max_value=14))):
        minute += data.draw(st.integers(min_value=0, max_value=6))
        kind = data.draw(
            st.sampled_from(
                [
                    "STATUS",
                    "SET_GLOBAL",
                    "STARTJOB",
                    "FORCE_STARTJOB",
                    "KILLJOB",
                    "ON_ICE",
                    "OFF_ICE",
                    "ON_HOLD",
                    "OFF_HOLD",
                    "ON_NOEXEC",
                    "OFF_NOEXEC",
                    "ADVANCE",
                ]
            )
        )
        keys = sorted(set(_all_keys(o)) | {"job:wbox", "global:FLAG"})
        before = {key: (_independent_projection(o, key), o.store.revision(key)) for key in keys}

        at = T0 + timedelta(minutes=minute)
        if kind == "ADVANCE":
            o.advance(at)
        elif kind == "SET_GLOBAL":
            value = data.draw(st.sampled_from(["go", "stop"]))
            o.feed(_ev("SET_GLOBAL", minute, name="FLAG", value=value))
        elif kind == "STATUS":
            job = data.draw(st.sampled_from(_WIDE_JOBS))
            status = data.draw(
                st.sampled_from(
                    ["SUCCESS", "FAILURE", "TERMINATED", "STARTING", "RUNNING", "INACTIVE"]
                )
            )
            o.feed(_ev("STATUS", minute, job=job, status=status))
        else:
            o.feed(_ev(kind, minute, job=data.draw(st.sampled_from(_WIDE_JOBS))))

        for key in sorted(set(_all_keys(o)) | set(keys)):
            was_projection, was_revision = before.get(key, (_independent_projection(o, key), 0))
            moved = _independent_projection(o, key) != was_projection
            assert o.store.revision(key) == was_revision + (1 if moved else 0), (
                f"{key} projection {'changed' if moved else 'did not change'}"
                f" but its revision went {was_revision} -> {o.store.revision(key)}"
            )


def test_a_standalone_time_advance_is_an_input() -> None:
    """ss4: "scheduler ticks, adapter completions, reconciliation injections
    and standalone time observations feed the same state machine" -- so
    `advance()` opens and closes a transaction exactly as `feed()` does. It
    fires the term_run_time deadline here, which terminates a running job with
    no external event at all, and that must move the job's revision once."""
    o = Oracle(lower_source("insert_job: slow\njob_type: c\ncommand: x\nterm_run_time: 5\n\n"))
    o.feed(_ev("STARTJOB", 0, job="slow"))
    started = o.store.revision("job:slow")
    o.advance(T0 + timedelta(minutes=6))
    assert o.store.job["slow"].status == "TERMINATED"  # the deadline really fired
    assert o.store.revision("job:slow") == started + 1


def test_an_input_whose_only_effect_is_a_timer_leaving_the_heap_still_counts() -> None:
    """The pop side of the heap's projection, isolated. A must_start deadline
    armed by a tick is STALE once the job has run, so when it finally fires it
    emits nothing and writes no field -- the entire effect of the input is one
    entry leaving the heap. If that did not move the revision, a client could
    hold a precondition across a schedule change it never saw."""
    o = Oracle(
        lower_source(
            "insert_job: ms2\njob_type: c\ncommand: x\n"
            'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
            "must_start_times: +30\n\n"
        )
    )
    o.feed(_ev("STARTJOB", 0, job="ms2"))  # runs AND arms the deadline
    assert o.store.job["ms2"].run_number == 1
    o.feed(_ev("STATUS", 1, job="ms2", status="SUCCESS"))
    before_row = dict(o.store.job["ms2"])
    # still ON the heap, though pending_timers() already hides it: that verb
    # reports what a fire would ACT on, and this one is destined to no-op
    assert len(o.store.timers_for("ms2")) == 1
    assert o.pending_timers() == []

    o.advance(T0 + timedelta(minutes=31))  # the stale deadline pops, silently
    after_row = dict(o.store.job["ms2"])
    assert o.store.timers_for("ms2") == []
    assert {k: v for k, v in after_row.items() if k != "state_rev"} == {
        k: v for k, v in before_row.items() if k != "state_rev"
    }
    assert after_row["state_rev"] == before_row["state_rev"] + 1


def test_a_globals_revision_accumulates_across_inputs() -> None:
    """`set_global` rebuilds the row, so it has to carry the revision over --
    a reset would park every global at 1 forever and silently make every
    `expect` on a global succeed."""
    o = Oracle(lower_source("insert_global: G\nvalue: a\n\n"))
    assert o.store.revision("global:G") == 1
    o.feed(_ev("SET_GLOBAL", 0, name="G", value="b"))
    assert o.store.revision("global:G") == 2
    o.feed(_ev("SET_GLOBAL", 1, name="G", value="b"))  # same value: not a change
    assert o.store.revision("global:G") == 2
    o.feed(_ev("SET_GLOBAL", 2, name="G", value="c"))
    assert o.store.revision("global:G") == 3
