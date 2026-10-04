"""Branch gaps in oracle.py that the DL-105 gate list leaves open (F5b).

Each test holds the observable effect of one branch: a status, a trace record,
a refusal and its message, a timer. The branches are the ones the combined
coverage run missed. test_oracle.py runs every trace twice, oracle-direct and
through the engine; these tests drive the oracle directly because several of
them reach into hand-built state (a catalog edited after lowering, a timer the
scheduler never arms), which the engine harness cannot carry.

The neighbouring tests cite the SEM or DL entry the branch implements; so do
these.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dsl41.ir import lower_source
from dsl41.oracle import DEFERRED_TIMER_KEY, Oracle
from dsl41.oracle_state import Event, EventKind, OracleError
from dsl41.semantics import resolve as resolve_switches

T0 = datetime(2026, 7, 1, 8, 0)  # a Wednesday


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def at(when: datetime, kind: EventKind, **payload: object) -> Event:
    return Event(at=when, kind=kind, payload=payload)


def transitions(o: Oracle, job: str) -> list[str]:
    return [t.transition for t in o.trace() if t.job == job]


def causes(o: Oracle, job: str, transition: str) -> list[str]:
    return [t.cause for t in o.trace() if t.job == job and t.transition == transition]


def statuses(o: Oracle, *jobs: str) -> dict[str, str]:
    return {job: o.store.job[job].status for job in jobs}


_ONE_JOB = "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n"

# ------------------------------------------------------------- input batch, timers


def _arm_jobless_timer(o: Oracle, due: datetime) -> None:
    """A deferred-start timer that names no job, to make a fire raise. The
    scheduler never arms one, but `_schedule_timer` admits the shape (PR-09
    names the shapes, not the payload keys)."""
    with o.batch(T0) as batch:
        batch.feed(ev("STARTJOB", job="a"))
        o._schedule_timer(
            due, Event(at=due, kind="TIMER", payload={DEFERRED_TIMER_KEY: "restored"})
        )


def test_dl087_a_timer_that_raises_on_entry_still_commits_the_batch() -> None:
    """A batch commits "whether or not the drain raised" (DL-87): the oracle
    has no rollback, so what did change must reach a reader. A due timer
    that names no job raises when it fires, inside `__enter__`; the batch
    commits, the error leaves, and the oracle takes the next input."""
    o = Oracle(lower_source(_ONE_JOB))
    _arm_jobless_timer(o, T0 + timedelta(minutes=5))
    with pytest.raises(OracleError, match="TIMER requires payload.job"):
        o.advance(T0 + timedelta(minutes=6))
    assert o.store.timers() == []  # the popped timer is gone, not stuck mid-input
    o.feed(ev("STATUS", 7, job="a", status="SUCCESS"))  # no open input blocks this
    assert statuses(o, "a") == {"a": "SUCCESS"}


# --------------------------------------------------------------- STATUS payloads


def test_sem09_a_status_event_needs_a_status_or_an_integer_exit_code() -> None:
    o = Oracle(lower_source(_ONE_JOB))
    with pytest.raises(OracleError, match="requires payload.status or integer payload.exit_code"):
        o.feed(ev("STATUS", job="a"))
    with pytest.raises(OracleError, match="requires payload.status or integer payload.exit_code"):
        o.feed(ev("STATUS", job="a", exit_code="0"))
    assert statuses(o, "a") == {"a": "INACTIVE"}
    o.feed(ev("STATUS", job="a", exit_code=0))  # twin: an integer exit code resolves
    assert statuses(o, "a") == {"a": "SUCCESS"}


def test_sem09_a_status_outside_the_injectable_set_is_refused() -> None:
    o = Oracle(lower_source(_ONE_JOB))
    with pytest.raises(OracleError, match="unknown status 'BOGUS'"):
        o.feed(ev("STATUS", job="a", status="BOGUS"))
    assert statuses(o, "a") == {"a": "INACTIVE"}
    o.feed(ev("STATUS", job="a", status="FAILURE"))  # twin: a member of the set is applied
    assert statuses(o, "a") == {"a": "FAILURE"}


# ------------------------------------------------------- operator events (DL-254)

_QUEUE_TEXT = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: q\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n"
)


def test_dl050_off_hold_readmits_a_queued_job_whose_resource_freed_while_held() -> None:
    """A held waiter stays queued when the resource frees (DL-247: a held
    waiter blocks no one and starts nothing). OFF_HOLD re-attempts it, so it
    is admitted at once."""
    o = Oracle(lower_source(_QUEUE_TEXT))
    o.feed(ev("STARTJOB", 0, job="h"))
    o.feed(ev("STARTJOB", 0, job="q"))
    o.feed(ev("ON_HOLD", 1, job="q"))
    o.feed(ev("STATUS", 2, job="h", status="SUCCESS"))
    assert statuses(o, "q") == {"q": "QUE_WAIT"}  # freed, but held
    o.feed(ev("OFF_HOLD", 3, job="q"))
    assert statuses(o, "q") == {"q": "RUNNING"}


def test_dl050_off_hold_on_a_queued_job_with_the_resource_still_taken_keeps_it_queued() -> None:
    """Twin: the re-attempt finds the resource still taken."""
    o = Oracle(lower_source(_QUEUE_TEXT))
    o.feed(ev("STARTJOB", 0, job="h"))
    o.feed(ev("STARTJOB", 0, job="q"))
    o.feed(ev("ON_HOLD", 1, job="q"))
    o.feed(ev("OFF_HOLD", 2, job="q"))
    assert statuses(o, "h", "q") == {"h": "RUNNING", "q": "QUE_WAIT"}


def test_dl254_on_noexec_for_a_removed_job_leaves_its_status_alone() -> None:
    """A row can outlive its definition (a removed job's held units, DL-256).
    ON_NOEXEC on a removed job that FAILED has no definition to move to
    INACTIVE through (DL-243) or to retry a start from, so it sets the flag,
    records it, and leaves the status and the exit code as they were."""
    o = Oracle(lower_source(_ONE_JOB))
    o.feed(ev("STATUS", 0, job="gone", status="FAILURE", exit_code=2))
    o.feed(ev("ON_NOEXEC", 1, job="gone"))
    row = o.store.job["gone"]
    assert (row.status, row.exit_code, row.on_noexec) == ("FAILURE", 2, True)
    assert transitions(o, "gone") == ["INACTIVE->FAILURE", "ON_NOEXEC"]


def test_dl243_on_noexec_for_a_defined_failed_job_moves_it_to_inactive() -> None:
    """The same event on a defined job that FAILED materializes the INACTIVE
    move and clears the exit code (DL-243)."""
    o = Oracle(lower_source(_ONE_JOB))
    o.feed(ev("STATUS", 0, job="a", status="FAILURE", exit_code=2))
    o.feed(ev("ON_NOEXEC", 1, job="a"))
    row = o.store.job["a"]
    assert (row.status, row.exit_code, row.on_noexec) == ("INACTIVE", None, True)


# ------------------------------------------------------------- lookback (SEM-04)

_ZERO_LOOKBACK = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: hold\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: pred\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: ev\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(pred, 0)\n"
)


def test_sem04_a_zero_lookback_is_false_for_a_predecessor_that_never_ended() -> None:
    """Q2a (DL-54): `n(pred, 0)` holds when pred's last end is at or after
    the evaluator's. `pred` waits in QUE_WAIT, so n() is true for it (DL-50)
    but it has no end at all, and it cannot have ended "since" the
    evaluator's. The evaluator, which has ended once, is refused."""
    o = Oracle(lower_source(_ZERO_LOOKBACK))
    o.feed(ev("STARTJOB", 0, job="ev"))  # never ended: no anchor, the atom is satisfied
    o.feed(ev("STATUS", 1, job="ev", status="SUCCESS"))
    o.feed(ev("STARTJOB", 2, job="hold"))
    o.feed(ev("STARTJOB", 2, job="pred"))
    assert statuses(o, "pred") == {"pred": "QUE_WAIT"}
    o.feed(ev("STARTJOB", 3, job="ev"))
    assert statuses(o, "ev") == {"ev": "SUCCESS"}  # not started again
    assert transitions(o, "ev").count("INACTIVE->STARTING") == 1


# ------------------------------------------------------- run_window skip (DL-154)


def test_sem33_a_forced_skip_of_a_member_of_an_idle_box_leaves_the_box_alone() -> None:
    """DL-154: a window skip is a bypass only inside a RUNNING box ("members
    of non-RUNNING boxes keep the plain skip"). A FORCE_STARTJOB on a member
    of an idle box, 10 minutes after the window closed, records the skip and
    resolves nothing: the member and the box stay INACTIVE."""
    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\nrun_window: "02:00-04:00"\n'
    )
    o = Oracle(lower_source(text))
    o.feed(at(datetime(2026, 7, 1, 4, 10), "FORCE_STARTJOB", job="m"))
    assert transitions(o, "m") == ["RUN_WINDOW_SKIP"]
    assert statuses(o, "b", "m") == {"b": "INACTIVE", "m": "INACTIVE"}
    assert o.store.job["b"].window_skipped_members == frozenset()


def test_dl246_a_box_start_skips_a_member_outside_its_window_and_completes_the_box() -> None:
    """Twin: a skip decided while the box runs (at its start, DL-246)
    resolves the member, so the box completes (SEM-11, DL-154)."""
    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\nrun_window: "02:00-04:00"\n'
    )
    o = Oracle(lower_source(text))
    o.feed(at(datetime(2026, 7, 1, 4, 5), "STARTJOB", job="b"))
    assert statuses(o, "b") == {"b": "SUCCESS"}  # the box's start skipped its member (DL-246)
    assert transitions(o, "m").count("RUN_WINDOW_SKIP") == 1


# ------------------------------------------------- queued recheck (DL-257), the day


def test_dl257_a_condition_recheck_overtaken_by_a_restart_reports_cancelled() -> None:
    """`n(j, 00.01)` is true for a job whose status changed a minute ago or
    less. `j` sat queued for ten minutes, so the recheck finds the condition
    false and sends it INACTIVE. That transition is a fresh change, so the
    wake of it starts `j` again before the readmission returns. The decision
    to leave the queue unstarted is overtaken, and `_readmit` reports
    "cancelled". Its one caller treats "cancelled" and "admitted" alike, so
    the value pins the internal outcome only; the observable rule is that `j`
    is RUNNING and no second start or timer follows.

    Only an unscheduled job can be restarted by that wake, and an unscheduled
    job has only a condition check, so no window or day recheck reaches the
    guard with a restarted job. A job with date conditions is disarmed here
    and starts only on a tick (SEM-30, DL-54)."""
    text = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n"
        "condition: n(j, 00.01)\n"
    )
    o = Oracle(lower_source(text), semantics=resolve_switches({"queued-recheck": "1"}))
    results: list[str] = []
    readmit = o._readmit

    def spy(job: str, *, keep_stopped: bool = False) -> str:
        results.append(readmit(job, keep_stopped=keep_stopped))
        return results[-1]

    o._readmit = spy  # type: ignore[method-assign]
    o.feed(ev("STARTJOB", 0, job="h"))
    o.feed(ev("STARTJOB", 0, job="j"))
    assert statuses(o, "j") == {"j": "QUE_WAIT"}
    o.feed(ev("STATUS", 10, job="h", status="SUCCESS"))
    assert results == ["cancelled"]
    assert transitions(o, "j") == [
        "INACTIVE->QUE_WAIT",
        "QUE_WAIT->INACTIVE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    assert statuses(o, "j") == {"j": "RUNNING"}
    assert o.pending_timers() == []
    assert o.store.job["j"].waiter_seq is None


_NO_TICKS = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n"
    "date_conditions: 1\ndays_of_week: mo\n"
)


def test_dl257_a_tickless_job_with_no_run_window_has_no_opening_to_defer_to() -> None:
    """The day check under `queued-recheck=2` sends `j` INACTIVE on a
    Wednesday (it runs Mondays). It has no ticks, so DL-257 would defer it to
    the next window opening; with no run_window there is no opening, so it
    is not deferred and no timer is armed."""
    o = Oracle(lower_source(_NO_TICKS), semantics=resolve_switches({"queued-recheck": "2"}))
    o.feed(ev("STARTJOB", 0, job="h"))
    o.feed(ev("STARTJOB", 0, job="j"))
    o.feed(ev("STATUS", 10, job="h", status="SUCCESS"))
    assert statuses(o, "j") == {"j": "INACTIVE"}
    assert "is not a run day" in causes(o, "j", "QUE_WAIT->INACTIVE")[0]
    assert "RUN_WINDOW_DEFER" not in transitions(o, "j")
    assert o.pending_timers() == []


def test_dl257_a_tickless_job_with_a_run_window_is_deferred_to_its_next_opening() -> None:
    """Twin: the same job with a run_window gets one deferred start, at the
    opening of the next Monday."""
    text = _NO_TICKS + 'run_window: "07:00-10:00"\n'
    o = Oracle(lower_source(text), semantics=resolve_switches({"queued-recheck": "2"}))
    o.feed(ev("STARTJOB", 0, job="h"))
    o.feed(ev("STARTJOB", 0, job="j"))
    o.feed(ev("STATUS", 10, job="h", status="SUCCESS"))
    assert statuses(o, "j") == {"j": "INACTIVE"}
    assert o.pending_timers() == [(datetime(2026, 7, 6, 7, 0), "j", "run_window")]


def _calendar_walk_text() -> str:
    """An extended calendar of Wednesdays that compiles, answers for any one
    day whose generation window stops short of 4 July 2026, and fails when a
    scan reaches it:
    that day is a holiday under `holiday: W`, and every Monday after it for
    sixty weeks is a holiday too, so the walk to a working Monday finds none.
    (The same calendar as the preflight generation-error test.)"""
    mondays = [date(2026, 7, 6) + timedelta(days=7 * i) for i in range(60)]
    rows = "\n".join(f"{d:%m/%d/%Y}" for d in [date(2026, 7, 4), *mondays])
    return (
        f"calendar: hols\n{rows}\n\n"
        "extended_calendar: wedwalk\nworkday: mo\nholiday: W\n"
        "holcal: hols\ncondition: wed\ncondition: jul#4\n\n"
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n"
        'date_conditions: 1\ndays_of_week: all\nrun_window: "09:00-23:00"\n'
        "exclude_calendar: wedwalk\n"
    )


def _queue_and_free(text: str, today: datetime) -> Oracle:
    o = Oracle(lower_source(text), semantics=resolve_switches({"queued-recheck": "1"}))
    o.feed(at(today, "STARTJOB", job="h"))
    o.feed(at(today, "STARTJOB", job="j"))
    assert statuses(o, "j") == {"j": "QUE_WAIT"}
    o.feed(at(today + timedelta(minutes=1), "STATUS", job="h", status="SUCCESS"))
    return o


def test_dl257_a_calendar_that_fails_over_the_opening_scan_is_refused_by_name() -> None:
    """The recheck asks the exclusion calendar about one day, and the window
    around 2025-03-05 holds no 4 July 2026, so `wedwalk` answers: today, a
    Wednesday, is excluded. The deferral then scans 732 days at once; that
    range does reach 4 July 2026, the holiday walk fails, and the oracle
    refuses with the job named, never a guess (DL-257)."""
    text = _calendar_walk_text()
    with pytest.raises(OracleError, match=r"^j: extended calendar 'wedwalk'.*no valid day"):
        _queue_and_free(text, datetime(2025, 3, 5, 12, 0))


def test_dl257_the_same_calendar_defers_when_the_scan_stays_clear_of_the_failure() -> None:
    """Twin: with today on 2028-03-01 (also a Wednesday) the holiday sits
    behind the window, the scan succeeds, and `j` is deferred to the opening
    of the next day the calendar does not exclude."""
    text = _calendar_walk_text()
    o = _queue_and_free(text, datetime(2028, 3, 1, 12, 0))
    assert statuses(o, "j") == {"j": "INACTIVE"}
    assert o.pending_timers() == [(datetime(2028, 3, 2, 9, 0), "j", "run_window")]


# ------------------------------------------------------------------ boxes (SEM-10..15)

_AUTO_HOLD = (
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\nauto_hold: 1\n"
)


def test_dossier_ss5_auto_hold_on_box_start_does_not_hold_a_member_twice() -> None:
    """A member already ON_HOLD when its box starts keeps that hold and the
    start records no second one. Without the hold, auto_hold records its own."""
    o = Oracle(lower_source(_AUTO_HOLD))
    o.feed(ev("ON_HOLD", 0, job="m"))
    o.feed(ev("STARTJOB", 1, job="b"))
    assert o.store.job["m"].on_hold is True
    assert causes(o, "m", "ON_HOLD") == ["sendevent ON_HOLD"]

    fresh = Oracle(lower_source(_AUTO_HOLD))
    fresh.feed(ev("STARTJOB", 1, job="b"))
    assert causes(fresh, "m", "ON_HOLD") == ["auto_hold on box start (dossier ss5)"]


def test_sem14_a_box_terminator_failure_in_an_idle_box_does_not_terminate_it() -> None:
    """SEM-14 terminates the containing box on a member's failure only while
    the box RUNS. A forced start of the member of an idle box that then fails
    falls through to the SEM-15 recompute: the box takes the derived FAILURE,
    not the sticky TERMINATED."""
    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\nbox_terminator: 1\n"
    )
    o = Oracle(lower_source(text))
    o.feed(ev("FORCE_STARTJOB", 0, job="m"))
    assert statuses(o, "b") == {"b": "INACTIVE"}
    o.feed(ev("STATUS", 1, job="m", status="FAILURE"))
    assert statuses(o, "b", "m") == {"b": "FAILURE", "m": "FAILURE"}
    assert causes(o, "b", "INACTIVE->FAILURE") == [
        "idle-box recompute (SEM-15): member 'm' changed"
    ]


_IDLE_OVERRIDE = (
    "insert_job: b\njob_type: b\nbox_success: s(m)\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n\n"
    "insert_job: m2\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
)


def test_sem15_an_idle_box_already_at_its_override_verdict_is_not_moved_again() -> None:
    """SEM-12: `box_success: s(m)` fires the moment `m` succeeds, with `m2`
    still running. A later member change on the now-idle box re-derives the
    override (SEM-15), finds the box already SUCCESS and writes nothing."""
    o = Oracle(lower_source(_IDLE_OVERRIDE))
    o.feed(ev("STARTJOB", 0, job="b"))
    o.feed(ev("STATUS", 1, job="m", status="SUCCESS"))
    assert statuses(o, "b") == {"b": "SUCCESS"}
    before = transitions(o, "b")
    o.feed(ev("STATUS", 2, job="m2", status="FAILURE"))
    assert statuses(o, "b") == {"b": "SUCCESS"}
    assert transitions(o, "b") == before


def test_sem15_an_idle_box_whose_override_is_true_moves_to_that_verdict() -> None:
    """Twin: an idle box that is not yet at the verdict moves to it. `m`
    succeeds while the box is idle (a forced start), and the override holds."""
    o = Oracle(lower_source(_IDLE_OVERRIDE))
    o.feed(ev("FORCE_STARTJOB", 0, job="m"))
    o.feed(ev("STATUS", 1, job="m", status="SUCCESS"))
    assert statuses(o, "b") == {"b": "SUCCESS"}
    assert causes(o, "b", "INACTIVE->SUCCESS") == [
        "idle-box override recompute (SEM-15): member 'm' changed"
    ]


# ----------------------------------------------------------- deadlines (SEM-34)

_SLA = (
    "insert_job: sla\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    "must_start_times: +30\nterm_run_time: 30\n"
)


def _timer_checks(o: Oracle) -> list[tuple[datetime, str]]:
    """The LIVE deadlines, as the ss11 status query lists them."""
    return [(due, kind) for due, _, kind in o.pending_timers()]


def _armed_checks(o: Oracle) -> list[tuple[datetime, str]]:
    """Every deadline on the heap, live or stale."""
    return [
        (due, str(e.payload["check"])) for due, _, e in o.store.timers() if "check" in e.payload
    ]


def test_sem34_a_tick_after_the_earlier_run_began_arms_a_fresh_must_start() -> None:
    """DL-248, DL-253: at most one deadline of a kind is pending per job, and
    one counts as pending only while no run has begun since its tick. The
    08:00 tick's must_start (due 08:30) is met when the job starts at 08:01.
    The next tick, at 08:20, finds that deadline dead (the run number moved)
    and arms its own, due 08:50."""
    o = Oracle(lower_source(_SLA))
    o.feed(ev("STARTJOB", 0, job="sla"))
    o.feed(ev("STATUS", 5, job="sla", status="SUCCESS"))
    assert _timer_checks(o) == []  # both of run 1's timers are stale, so neither is listed
    o.feed(ev("STARTJOB", 20, job="sla"))
    assert (T0 + timedelta(minutes=50), "must_start") in _armed_checks(o)


def test_sem34_a_tick_while_an_earlier_must_start_is_still_pending_arms_nothing() -> None:
    """Twin: with no run begun since the first tick (a held job, whose start
    is refused), its deadline is still pending, so the second tick arms
    nothing."""
    o = Oracle(lower_source(_SLA))
    o.feed(ev("ON_HOLD", 0, job="sla"))
    o.feed(ev("STARTJOB", 1, job="sla"))
    o.feed(ev("STARTJOB", 20, job="sla"))
    must_starts = [due for due, kind in _timer_checks(o) if kind == "must_start"]
    assert must_starts == [T0 + timedelta(minutes=31)]


def test_sem34_a_must_complete_deadline_met_by_an_earlier_run_is_not_pending() -> None:
    """The must_complete test judges the run a tick asked for, run N+1. Once
    that run has completed, a later tick no longer sees the earlier deadline
    as pending and arms its own."""
    text = (
        "insert_job: sla\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +60\n"
    )
    o = Oracle(lower_source(text))
    o.feed(ev("STARTJOB", 0, job="sla"))
    o.feed(ev("STATUS", 5, job="sla", status="SUCCESS"))
    assert [kind for _, kind in _timer_checks(o)] == []  # the 09:00 deadline is met
    o.feed(ev("STARTJOB", 10, job="sla"))
    assert _timer_checks(o) == [(T0 + timedelta(minutes=70), "must_complete")]


def test_sem34_a_must_complete_timer_naming_no_run_cannot_be_shown_complete() -> None:
    """A TIMER event injected without the tick's run number reads as a run
    that has not completed, so the alarm fires. With the run named and
    completed, it does not."""
    o = Oracle(lower_source(_SLA))
    emitted = o.feed(ev("TIMER", 0, check="must_complete", job="sla"))
    assert [e.kind for e in emitted if e.kind == "MUST_COMPLETE_ALARM"] == ["MUST_COMPLETE_ALARM"]
    o.feed(ev("STARTJOB", 1, job="sla"))
    o.feed(ev("STATUS", 2, job="sla", status="SUCCESS"))
    emitted = o.feed(ev("TIMER", 3, check="must_complete", job="sla", run=0))
    assert [e.kind for e in emitted if e.kind == "MUST_COMPLETE_ALARM"] == []


def test_sem34_a_term_run_time_timer_of_an_earlier_run_does_not_end_the_next_run() -> None:
    """The run-number test of a deadline timer: run 1 starts at 08:00 with a
    30-minute limit and succeeds at 08:05; run 2 starts at 08:10. Run 1's
    timer is due at 08:30 and fires into run 2, which is not its run, so it
    is dropped as stale and run 2 keeps running until its own limit (08:40)."""
    o = Oracle(lower_source(_SLA))
    o.feed(ev("STARTJOB", 0, job="sla"))
    o.feed(ev("STATUS", 5, job="sla", status="SUCCESS"))
    o.feed(ev("STARTJOB", 10, job="sla"))
    o.advance(T0 + timedelta(minutes=31))
    assert statuses(o, "sla") == {"sla": "RUNNING"}
    o.advance(T0 + timedelta(minutes=41))
    assert statuses(o, "sla") == {"sla": "TERMINATED"}


def test_pr09_an_injected_timer_with_an_unregistered_check_is_consumed_without_effect() -> None:
    """`_schedule_timer` arms only the checks in TIMER_CHECKS, but a TIMER
    event can also be injected. One naming a check the oracle does not know
    is consumed as a deadline timer: it is not a start attempt, and it moves
    nothing."""
    o = Oracle(lower_source(_ONE_JOB))
    o.feed(ev("TIMER", job="a", check="bogus", run=0))
    assert transitions(o, "a") == []
    assert statuses(o, "a") == {"a": "INACTIVE"}


def test_pr09_an_injected_timer_without_a_check_is_a_start_attempt() -> None:
    """Twin: the same event with no check is a deferred-start shape, and it
    starts the job."""
    o = Oracle(lower_source(_ONE_JOB))
    o.feed(ev("TIMER", job="a"))
    assert statuses(o, "a") == {"a": "RUNNING"}
