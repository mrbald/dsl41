"""Scheduler + preflight tests (phase 11c).

Normative spec: docs/runner-design.md ss5 (scheduler), ss8 (preflight), and
runner.py's own 11c docstring block (DL-45 pins the decisions). House style
follows test_runner.py: T0-style fixed datetimes, JIL text fixtures inline,
async scenarios driven by one `asyncio.run(...)` per test.

Every expected outcome here was verified empirically against the real
Scheduler/Engine/preflight before the assertion was written (CLAUDE.md:
fidelity is tested, not asserted) -- see the final report for anything that
surprised us or contradicted the design doc.
"""

from __future__ import annotations

import asyncio
import getpass
import socket as socket_mod
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.ir import CatalogIR, JobIR, lower_source
from dsl41.oracle import Event, Oracle
from dsl41.runner import (
    Engine,
    FakeAdapter,
    RealClock,
    Scheduler,
    VirtualClock,
    and_success_skeleton,
    preflight,
    read_journal,
    resume_run,
    start_run,
)

# 2026-07-01 is a Wednesday; 07-03 Fri, 07-04 Sat, 07-05 Sun, 07-06 Mon.


# ------------------------------------------------------------ 1. occurrence math


def test_days_of_week_filters_to_weekdays_only() -> None:
    """(ss5): mo-fr filtering skips the weekend entirely -- Friday 07-03 and
    the following Monday 07-06 fire; Saturday/Sunday do not."""
    text = (
        "insert_job: weekday_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: mo,tu,we,th,fr\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 3, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 6, 23, 59))
    assert [e.at for e in due] == [datetime(2026, 7, 3, 8, 0), datetime(2026, 7, 6, 8, 0)]


def test_start_times_ordering_within_a_day() -> None:
    """(ss5): start_times listed out of order still fire in ascending order
    within the day."""
    text = (
        "insert_job: order_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00, 08:00, 08:30"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 1, 23, 59))
    assert [e.at for e in due] == [
        datetime(2026, 7, 1, 8, 0),
        datetime(2026, 7, 1, 8, 30),
        datetime(2026, 7, 1, 9, 0),
    ]


def test_start_mins_hourly_ticks() -> None:
    """(ss5): start_mins fires every hour at the given minutes."""
    text = (
        "insert_job: mins_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 15,45\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 7, 50))
    due = sched.pop_due(datetime(2026, 7, 1, 10, 50))
    assert [e.at for e in due] == [
        datetime(2026, 7, 1, 8, 15),
        datetime(2026, 7, 1, 8, 45),
        datetime(2026, 7, 1, 9, 15),
        datetime(2026, 7, 1, 9, 45),
        datetime(2026, 7, 1, 10, 15),
        datetime(2026, 7, 1, 10, 45),
    ]


def test_all_keyword_matches_weekends_too() -> None:
    """(ss5): days_of_week: all fires on a Saturday, unlike a mo-fr list."""
    text = (
        "insert_job: all_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 4, 0, 0))  # Saturday
    assert sched.next_occurrence() == datetime(2026, 7, 4, 8, 0)


def test_absent_days_of_week_defaults_to_every_day() -> None:
    """PENDING: E10 -- absent days_of_week means every day, same as 'all',
    including weekends (runner.py Scheduler docstring)."""
    text = (
        "insert_job: absent_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 4, 0, 0))  # Saturday
    assert sched.next_occurrence() == datetime(2026, 7, 4, 8, 0)


def test_first_tick_inclusive_by_default_exclusive_via_reset() -> None:
    """(ss5 Scheduler.reset docstring): construction anchors inclusively (a
    tick exactly at `start` counts); reset(..., inclusive=False) -- resume's
    tool -- skips it and finds the next one."""
    text = (
        "insert_job: incl_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    catalog = lower_source(text)
    tick = datetime(2026, 7, 1, 8, 0)

    inclusive = Scheduler(catalog, start=tick)
    assert inclusive.next_occurrence() == tick

    exclusive = Scheduler(catalog, start=tick)
    exclusive.reset(tick, inclusive=False)
    assert exclusive.next_occurrence() == datetime(2026, 7, 2, 8, 0)


# ------------------------------------------------------------------- 2. timezone


def test_timezone_converts_to_correct_utc_instant_on_a_normal_day() -> None:
    """(ss5): a per-job timezone converts the local tick to the correct
    naive-UTC instant (America/New_York in July is EDT, UTC-4)."""
    text = (
        "insert_job: tz_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: America/New_York\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 6, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 7, 6, 13, 0)


def test_default_tz_applies_to_jobs_without_their_own_timezone() -> None:
    """(ss5): a job with no `timezone:` attribute reads its times in the
    Scheduler's default_tz (the run-level --timezone)."""
    text = (
        "insert_job: dtz_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
    )
    sched = Scheduler(
        lower_source(text), start=datetime(2026, 7, 6, 0, 0), default_tz="America/New_York"
    )
    assert sched.next_occurrence() == datetime(2026, 7, 6, 13, 0)


def test_dst_spring_forward_nonexistent_time_sorts_after_a_later_local_tick() -> None:
    """(ss5 _occurrence docstring): 2026-03-08 is America/New_York's
    spring-forward day -- 02:30 local never happens. PEP 495 fold=0 reads it
    at its pre-transition (EST, UTC-5) offset, landing at 07:30 UTC -- LATER
    than the very same day's 03:00 tick (post-transition EDT, UTC-4, 07:00
    UTC) despite 02:30 being listed first in start_times. Ticks are sorted
    AFTER UTC conversion, so pop_due returns them in true chronological
    order, not source order."""
    text = (
        "insert_job: dst_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30, 03:00"\n'
        "timezone: America/New_York\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 3, 8, 0, 0))
    due = sched.pop_due(datetime(2026, 3, 8, 23, 59))
    assert [e.at for e in due] == [datetime(2026, 3, 8, 7, 0), datetime(2026, 3, 8, 7, 30)]
    assert due[0].at < due[1].at  # strictly increasing despite the label order


def test_dst_fall_back_ambiguous_time_fires_at_its_first_occurrence() -> None:
    """(ss5 docstring, PEP 495 fold=0): 2026-11-01 01:30 America/New_York is
    ambiguous (it happens twice). fold=0 (the default) picks the FIRST
    occurrence -- pre-transition EDT, UTC-4 -- landing at 05:30 UTC, not the
    second (post-transition EST) occurrence at 06:30 UTC."""
    text = (
        "insert_job: fb_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:30"\n'
        "timezone: America/New_York\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 11, 1, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 11, 1, 5, 30)


def test_dst_ticks_strictly_increase_across_repeated_pop_due_calls() -> None:
    """(ss5): a daily tick run across both the spring-forward (2026-03-08)
    and fall-back (2026-11-01) transitions never crashes and never regresses
    -- every popped tick, across many separate pop_due calls, is strictly
    later than the one before it."""
    text = (
        "insert_job: dst_walk\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: America/New_York\n"
    )
    catalog = lower_source(text)
    for anchor in (datetime(2026, 3, 4), datetime(2026, 10, 28)):
        sched = Scheduler(catalog, start=anchor)
        seen: list[datetime] = []
        cursor = anchor
        for _ in range(8):
            cursor += timedelta(days=1)
            seen.extend(e.at for e in sched.pop_due(cursor))
        assert seen == sorted(seen)
        assert len(seen) == len(set(seen))
        assert len(seen) == 8  # one tick per day, none skipped or doubled


# --------------------------------------------------------------------- 3. pop_due


def test_multiple_jobs_due_at_once_sorted_by_tick_then_job() -> None:
    """(ss5 pop_due docstring): two jobs due at the identical tick sort by
    (tick, job) -- alphabetical on a tie, regardless of catalog order."""
    text = (
        "insert_job: zzz_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n\n'
        "insert_job: aaa_job\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 1, 23, 59))
    assert [e.payload["job"] for e in due] == ["aaa_job", "zzz_job"]
    assert due[0].at == due[1].at == datetime(2026, 7, 1, 8, 0)


def test_pop_advances_state_same_tick_never_returned_twice() -> None:
    """(ss5): once a tick is popped, calling pop_due again with the SAME
    `upto` returns nothing more -- state already advanced past it."""
    text = (
        "insert_job: once_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    tick = datetime(2026, 7, 1, 8, 0)
    assert [e.at for e in sched.pop_due(tick)] == [tick]
    assert sched.pop_due(tick) == []


def test_backlog_fires_every_intermediate_tick_stamped_at_its_own_time() -> None:
    """(ss5 pop_due docstring): calling pop_due with `upto` far in the future
    fires every intermediate daily tick in one go, each stamped at its own
    true instant -- never clamped to `upto`."""
    text = (
        "insert_job: backlog_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 5, 23, 59))
    assert [e.at for e in due] == [
        datetime(2026, 7, 1, 8, 0),
        datetime(2026, 7, 2, 8, 0),
        datetime(2026, 7, 3, 8, 0),
        datetime(2026, 7, 4, 8, 0),
        datetime(2026, 7, 5, 8, 0),
    ]


def test_run_window_or_sla_only_schedule_triggers_nothing() -> None:
    """(ss5 SEM-33): a schedule block with only run_window (no start_times/
    start_mins) is a gate/alarm, never a trigger -- the Scheduler never
    computes an occurrence for it."""
    text = (
        "insert_job: window_only\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_window: "10:00-11:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert sched.next_occurrence() is None
    assert sched.pop_due(datetime(2026, 7, 10, 0, 0)) == []


def test_run_calendar_job_is_skipped_by_the_scheduler() -> None:
    """(ss5): run_calendar/exclude_calendar reference calendar definitions
    the IR does not model -- preflight's territory (ss8), never guessed here
    -- so the Scheduler excludes the job entirely, even with start_times set."""
    text = (
        "insert_job: cal_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: some_cal\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert sched.next_occurrence() is None
    assert sched.pop_due(datetime(2026, 7, 10, 0, 0)) == []


# --------------------------------------------------------- 4. Engine integration


def test_engine_fires_scheduler_startjobs_journaled_source_scheduler_and_respects_horizon(
    tmp_path: Path,
) -> None:
    """(ss5/ss4): a date_conditions estate ticks through the Engine via a
    VirtualClock; each tick is journaled as an input with source=scheduler.
    A tick beyond the horizon does not fire (quiescence); running again to a
    later horizon then fires it -- time only moves forward across calls."""
    text = (
        "insert_job: eng_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00, 09:00"\n'
    )
    catalog = lower_source(text)
    start = datetime(2026, 7, 1, 0, 0)
    run_root = tmp_path / "run"

    async def scenario() -> None:
        clock = VirtualClock(start=start)
        scheduler = Scheduler(catalog, start=start)
        adapter = FakeAdapter()
        engine = start_run(
            catalog,
            run_root,
            clock=clock,
            adapters={"CMD": adapter, "FW": adapter},
            scheduler=scheduler,
        )
        # horizon covers only the 08:00 tick
        await engine.run_until_quiescent(start + timedelta(hours=8))
        assert engine.oracle.store.job["eng_sched"].run_number == 1
        assert engine.oracle.store.job["eng_sched"].status == "SUCCESS"

        # the 09:00 tick has NOT fired yet
        await engine.run_until_quiescent(start + timedelta(hours=10))
        assert engine.oracle.store.job["eng_sched"].run_number == 2

        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())

    records = read_journal(run_root / "journal.jsonl")
    starts = [r for r in records if r.get("rec") == "input" and r.get("kind") == "STARTJOB"]
    assert len(starts) == 2
    assert all(r["source"] == "scheduler" for r in starts)


def test_engine_scheduler_trace_matches_oracle_direct_startjobs(tmp_path: Path) -> None:
    """(ss13 bisimulation flavor): feeding the same STARTJOBs (at the ticks
    the Scheduler computes) straight into an Oracle produces the identical
    trace as running the Engine with the Scheduler attached."""
    text = (
        "insert_job: bisim_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00, 09:00"\n'
    )
    catalog = lower_source(text)
    start = datetime(2026, 7, 1, 0, 0)

    async def scenario() -> Engine:
        clock = VirtualClock(start=start)
        scheduler = Scheduler(catalog, start=start)
        adapter = FakeAdapter()
        engine = Engine(
            catalog, clock=clock, adapters={"CMD": adapter, "FW": adapter}, scheduler=scheduler
        )
        await engine.run_until_quiescent(start + timedelta(hours=10))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())

    o = Oracle(catalog)
    for hour in (8, 9):
        at = start + timedelta(hours=hour)
        o.feed(Event(at=at, kind="STARTJOB", payload={"job": "bisim_sched"}))
        o.feed(Event(at=at, kind="STATUS", payload={"job": "bisim_sched", "status": "SUCCESS"}))

    assert [t.model_dump() for t in o.trace()] == [t.model_dump() for t in engine.oracle.trace()]


# --------------------------------------------------------------------- 5. resume


def test_resume_virtual_scheduler_ticks_never_refire(tmp_path: Path) -> None:
    """(ss7/ss5): resuming a virtual scheduled run with a FRESH Scheduler
    must not refire the tick replay already fed; running to the SAME horizon
    adds nothing new, and extending the horizon fires only the next
    legitimate tick, never a duplicate of the first."""
    text = (
        "insert_job: resume_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    catalog = lower_source(text)
    start = datetime(2026, 7, 1, 0, 0)
    run_root = tmp_path / "run"
    horizon1 = start + timedelta(hours=9)

    async def phase1() -> None:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=start),
            adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
            scheduler=Scheduler(catalog, start=start),
        )
        await engine.run_until_quiescent(horizon1)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(phase1())

    def scheduler_starts() -> list[dict]:
        records = read_journal(run_root / "journal.jsonl")
        return [r for r in records if r.get("rec") == "input" and r.get("kind") == "STARTJOB"]

    assert len(scheduler_starts()) == 1

    async def phase2() -> None:
        engine = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=start),
            adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
            scheduler=Scheduler(catalog, start=start),
        )
        assert engine.drops == []  # virtual resume: nothing missed (ss7 docstring)

        # running back to the SAME horizon must not add a duplicate
        await engine.run_until_quiescent(horizon1)
        assert len(scheduler_starts()) == 1

        # extending the horizon fires only the next tick, once
        await engine.run_until_quiescent(start + timedelta(days=2))
        starts = scheduler_starts()
        assert [r["at"] for r in starts] == [
            (start + timedelta(hours=8)).isoformat(),
            (start + timedelta(days=1, hours=8)).isoformat(),
        ]
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(phase2())


def test_resume_real_domain_missed_ticks_are_dropped_and_journaled(tmp_path: Path) -> None:
    """(ss5/ss7 PENDING: E9): a real-domain journal whose last record is 2h
    in the past, over a job ticking every 30 minutes, resumes with 4 missed
    ticks -- each dropped (never fired late) and journaled as a `drop`
    record, and reported on Engine.drops."""
    from dsl41.runner import Journal

    text = (
        "insert_job: mts_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0,30\n"
    )
    catalog = lower_source(text)
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "runs").mkdir()
    (run_root / "logs").mkdir()

    now = RealClock().now()
    past = now - timedelta(hours=2)
    journal = Journal.create(
        run_root / "journal.jsonl", catalog=catalog, clock_domain="real", started_at=past
    )
    journal.close()

    async def scenario() -> Engine:
        engine = await resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
            scheduler=Scheduler(catalog, start=past),
        )
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(scenario())

    assert len(engine.drops) == 4
    assert all(ev.kind == "STARTJOB" for ev, _ in engine.drops)
    assert all("missed" in reason for _, reason in engine.drops)

    records = read_journal(run_root / "journal.jsonl")
    drop_records = [r for r in records if r.get("rec") == "drop"]
    assert len(drop_records) == 4
    assert all(r["kind"] == "STARTJOB" for r in drop_records)


# ------------------------------------------------------------------ 6. preflight


def test_preflight_job_type_error_for_unsupported_type_direct_catalog() -> None:
    """(ss8): lowering itself refuses any job_type outside CMD/BOX/FW, so a
    catalog with an unsupported type can only be constructed directly in
    Python (this is preflight's belt-and-suspenders check over any catalog
    source, not just JIL)."""
    catalog = CatalogIR(jobs={"weird": JobIR(name="weird", job_type="WEIRD")})
    items = preflight(catalog)
    assert any(
        i.code == "job-type" and i.severity == "ERROR" and i.job == "weird" for i in items
    )


def test_preflight_job_type_clean_for_cmd_box_fw() -> None:
    text = (
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n\n"
        "insert_job: fwj\njob_type: f\nwatch_file: /tmp/dsl41_test_watch\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "job-type" for i in items)


def test_preflight_machine_rejects_a_foreign_host() -> None:
    text = "insert_job: m_job\njob_type: c\ncommand: x\nmachine: some-other-host.example.com\n"
    items = preflight(lower_source(text))
    assert any(i.code == "machine" and i.severity == "ERROR" and i.job == "m_job" for i in items)


def test_preflight_machine_accepts_none_localhost_and_local_hostname() -> None:
    hostname = socket_mod.gethostname()
    text = (
        "insert_job: m_none\njob_type: c\ncommand: x\n\n"
        "insert_job: m_localhost\njob_type: c\ncommand: x\nmachine: localhost\n\n"
        f"insert_job: m_hostname\njob_type: c\ncommand: x\nmachine: {hostname}\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "machine" for i in items)


def test_preflight_owner_rejects_a_different_user() -> None:
    text = (
        "insert_job: o_job\njob_type: c\ncommand: x\nmachine: localhost\n"
        "owner: definitely-not-a-real-user\n"
    )
    items = preflight(lower_source(text))
    assert any(i.code == "owner" and i.severity == "ERROR" and i.job == "o_job" for i in items)


def test_preflight_owner_accepts_unset_or_the_invoking_user() -> None:
    user = getpass.getuser()
    text = (
        "insert_job: o_none\njob_type: c\ncommand: x\n\n"
        f"insert_job: o_self\njob_type: c\ncommand: x\nowner: {user}\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "owner" for i in items)


def test_preflight_calendar_errors_on_run_or_exclude_calendar() -> None:
    text = (
        "insert_job: c1\njob_type: c\ncommand: x\nmachine: localhost\n"
        "date_conditions: 1\nrun_calendar: some_cal\n\n"
        "insert_job: c2\njob_type: c\ncommand: y\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\nexclude_calendar: other_cal\n'
    )
    items = preflight(lower_source(text))
    codes = {(i.code, i.job) for i in items if i.code == "calendar"}
    assert ("calendar", "c1") in codes
    assert ("calendar", "c2") in codes


def test_preflight_calendar_clean_without_run_or_exclude_calendar() -> None:
    text = (
        "insert_job: c3\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "calendar" for i in items)


def test_preflight_timezone_errors_on_a_bogus_zone() -> None:
    text = (
        "insert_job: tzb\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: Bogus/Fake_Zone\n'
    )
    items = preflight(lower_source(text))
    assert any(i.code == "timezone" and i.severity == "ERROR" and i.job == "tzb" for i in items)


def test_preflight_timezone_clean_for_a_real_zone_or_unset() -> None:
    text = (
        "insert_job: tzu\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: UTC\n\n'
        "insert_job: tzn\njob_type: c\ncommand: y\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "timezone" for i in items)


def test_preflight_n_retrys_warns() -> None:
    text = "insert_job: nr\njob_type: c\ncommand: x\nmachine: localhost\nn_retrys: 3\n"
    items = preflight(lower_source(text))
    assert any(i.code == "n-retrys" and i.severity == "WARN" and i.job == "nr" for i in items)


def test_preflight_n_retrys_clean_when_unset() -> None:
    text = "insert_job: nr0\njob_type: c\ncommand: x\nmachine: localhost\n"
    items = preflight(lower_source(text))
    assert not any(i.code == "n-retrys" for i in items)


def test_preflight_resources_warns_on_job_load_and_typed_resources() -> None:
    text = (
        "insert_job: rl1\njob_type: c\ncommand: x\nmachine: localhost\njob_load: 50\n\n"
        "insert_job: rl2\njob_type: c\ncommand: y\nmachine: localhost\n"
        "resources: (r1, quantity=2, free=y)\n"
    )
    items = preflight(lower_source(text))
    codes = {(i.code, i.job) for i in items if i.code == "resources"}
    assert ("resources", "rl1") in codes
    assert ("resources", "rl2") in codes


def test_preflight_resources_clean_without_load_priority_or_resources() -> None:
    text = "insert_job: rl0\njob_type: c\ncommand: x\nmachine: localhost\n"
    items = preflight(lower_source(text))
    assert not any(i.code == "resources" for i in items)


def test_preflight_skeleton_cycle_warns() -> None:
    text = (
        "insert_job: cyc_x\njob_type: c\ncommand: x\nmachine: localhost\ncondition: s(cyc_y)\n\n"
        "insert_job: cyc_y\njob_type: c\ncommand: y\nmachine: localhost\ncondition: s(cyc_x)\n"
    )
    items = preflight(lower_source(text))
    assert any(i.code == "skeleton-cycle" and i.severity == "WARN" for i in items)


def test_preflight_skeleton_cycle_clean_for_an_acyclic_chain() -> None:
    text = (
        "insert_job: chain_a\njob_type: c\ncommand: x\nmachine: localhost\n\n"
        "insert_job: chain_b\njob_type: c\ncommand: y\nmachine: localhost\ncondition: s(chain_a)\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "skeleton-cycle" for i in items)


def test_preflight_execution_false_skips_identity_rules_but_keeps_the_rest() -> None:
    """(ss8 DL-45 decision 4): rehearse (execution=False) never runs a real
    process, so machine/owner are moot -- but calendar/timezone/oracle still
    gate because the scheduler and oracle depend on them regardless."""
    text = (
        "insert_job: ef1\njob_type: c\ncommand: x\nmachine: some-other-host\n"
        "owner: not-a-real-user\ndate_conditions: 1\nrun_calendar: cal1\n\n"
        "insert_job: ef2\njob_type: c\ncommand: y\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: Bogus/Fake_Zone\n'
    )
    catalog = lower_source(text)
    codes_exec = {i.code for i in preflight(catalog, execution=True)}
    codes_rehearse = {i.code for i in preflight(catalog, execution=False)}
    assert {"machine", "owner", "calendar", "timezone"} <= codes_exec
    assert "machine" not in codes_rehearse
    assert "owner" not in codes_rehearse
    assert "calendar" in codes_rehearse
    assert "timezone" in codes_rehearse


def test_preflight_oracle_rule_absent_for_a_normal_catalog() -> None:
    text = "insert_job: ok_job\njob_type: c\ncommand: x\nmachine: m1\n"
    items = preflight(lower_source(text))
    assert "oracle" not in {i.code for i in items}


def test_oracle_construction_currently_never_refuses() -> None:
    """Pins the reality behind the ss8 'oracle' rule being ARMOR: as of 11c,
    Oracle.__init__ has no raise site of its own (every OracleError in
    oracle.py is post-construction), so no catalog that passed CatalogIR
    validation can trigger the rule -- even an unsupported job_type
    constructs cleanly. If this test ever fails, construction refusals have
    arrived: promote the armor test below to a real-catalog fixture pair."""
    catalog = CatalogIR(jobs={"weird": JobIR(name="weird", job_type="WEIRD")})
    Oracle(catalog)  # must not raise
    assert "oracle" not in {i.code for i in preflight(catalog)}


def test_preflight_oracle_rule_is_armor_pinned_by_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(ss8): the design mandates the 'oracle construction failure' ERROR
    rule; the refusal it guards does not exist yet (test above), so the
    plumbing is pinned by injection -- a constructor refusal must surface
    as a preflight ERROR item, never a crash."""
    import dsl41.runner as runner_mod

    from dsl41.oracle import OracleError

    def refuse(_catalog: CatalogIR) -> None:
        raise OracleError("injected construction refusal")

    monkeypatch.setattr(runner_mod, "Oracle", refuse)
    text = "insert_job: ok_job\njob_type: c\ncommand: x\nmachine: m1\n"
    items = preflight(lower_source(text))
    oracle_items = [i for i in items if i.code == "oracle"]
    assert [i.severity for i in oracle_items] == ["ERROR"]
    assert "injected construction refusal" in oracle_items[0].message


# --------------------------------------------------------- 7. and_success_skeleton


def test_and_success_skeleton_and_spine_and_paren_descend() -> None:
    """(ss8/ss10): a Paren-wrapped s() atom under an And spine still counts
    as a hard predecessor -- Paren is transparent to the skeleton walk."""
    text = (
        "insert_job: sk_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: sk_b\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: sk_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: (s(sk_a)) & s(sk_b)\n"
    )
    catalog = lower_source(text)
    skeleton = and_success_skeleton(catalog)
    assert skeleton["sk_c"] == {"sk_a", "sk_b"}
    assert skeleton["sk_a"] == set()


def test_and_success_skeleton_or_breaks_the_spine() -> None:
    """(ss8): an s() atom under an Or is an alternative, not a dependency --
    it contributes no predecessor at all."""
    text = (
        "insert_job: sk2_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: sk2_b\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: sk2_c\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(sk2_a) | s(sk2_b)\n"
    )
    catalog = lower_source(text)
    skeleton = and_success_skeleton(catalog)
    assert skeleton["sk2_c"] == set()


def test_and_success_skeleton_ignores_exitcode_and_non_success_status_atoms() -> None:
    """(ss8): e()/n()/f() atoms are never edges -- only a SUCCESS StatusAtom
    on an AND/Paren spine is."""
    text = (
        "insert_job: sk3_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: sk3_b\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: sk3_c\njob_type: c\ncommand: z\nmachine: m1\n\n"
        "insert_job: sk3_e\njob_type: c\ncommand: w\nmachine: m1\n\n"
        "insert_job: sk3_target\njob_type: c\ncommand: v\nmachine: m1\n"
        "condition: s(sk3_a) & e(sk3_b) = 0 & n(sk3_c) & f(sk3_e)\n"
    )
    catalog = lower_source(text)
    skeleton = and_success_skeleton(catalog)
    assert skeleton["sk3_target"] == {"sk3_a"}


def test_and_success_skeleton_skips_instance_qualified_and_undefined_refs() -> None:
    """(ss8): a cross-instance atom (job^INST) and a reference to a job not
    in the catalog are both skipped -- pseudo-entries have no run to order."""
    text = (
        "insert_job: sk4_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: sk4_b\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(sk4_a^SOME_INST) & s(ghost_job)\n"
    )
    catalog = lower_source(text)
    skeleton = and_success_skeleton(catalog)
    assert skeleton["sk4_b"] == set()
