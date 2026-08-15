"""Scheduler + preflight tests (phase 11c).

Normative spec: docs/runner-design.md ss5 (scheduler), ss8 (preflight), and
the runner_scheduler.py / runner_preflight.py 11c docstring blocks (DL-45
pins the decisions). House style follows test_runner.py: T0-style fixed
datetimes, JIL text fixtures inline, async scenarios driven by one
`asyncio.run(...)` per test.

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
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine, resume_run, start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_preflight import (
    _local_identity,
    and_success_skeleton,
    preflight,
    resolve_machine,
)
from dsl41.runner_scheduler import Scheduler, parse_timezone_map, resolve_timezone

# 2026-07-01 is a Wednesday; 07-03 Fri, 07-04 Sat, 07-05 Sun, 07-06 Mon.


def _boom(*_a: object) -> str:
    raise AssertionError("getfqdn must not be called (DL-52 drops reverse-DNS)")


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
    including weekends (runner_scheduler.py Scheduler docstring)."""
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


def test_timezone_city_name_resolves_via_the_unique_iana_match() -> None:
    """(SEM-35/DL-62): a vendor city name (`timezone: Zurich`, a
    ujo_timezones City entry) with no --timezone-map resolves to the unique
    zoneinfo zone whose final path component matches -- Europe/Zurich, CEST
    (UTC+2) in July, so the 09:00 local tick lands at 07:00 UTC."""
    text = (
        "insert_job: city_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: Zurich\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 6, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 7, 6, 7, 0)


def test_timezone_names_are_case_insensitive() -> None:
    """(SEM-35, TechDocs: 'not case-sensitive'): america/new_york reads
    like America/New_York."""
    text = (
        "insert_job: fold_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: america/new_york\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 6, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 7, 6, 13, 0)


def test_timezone_map_alias_chain_resolves_to_the_zone() -> None:
    """(SEM-35/DL-62): tz_aliases (the autotimezone listing) chains
    Alias -> City -> zone exactly like the vendor's <=5 table reads."""
    text = (
        "insert_job: hq_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: HQ\n"
    )
    aliases = parse_timezone_map("HQ Alias Zurich\nZurich City Europe/Zurich\n")
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 6, 0, 0), tz_aliases=aliases)
    assert sched.next_occurrence() == datetime(2026, 7, 6, 7, 0)


def test_timezone_unresolvable_in_scheduler_raises_engine_error() -> None:
    """(ss5 backstop): preflight gates first, but direct Scheduler
    construction with an unresolvable per-job zone refuses comprehensibly."""
    text = (
        "insert_job: bad_tz\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "timezone: Bogus/Fake_Zone\n"
    )
    with pytest.raises(EngineError, match="bad_tz.*not resolvable"):
        Scheduler(lower_source(text), start=datetime(2026, 7, 6, 0, 0))


def test_resolve_timezone_posix_offsets_are_west_positive() -> None:
    """(SEM-35, TechDocs TZ syntax): POSIX offsets are west-positive --
    IST-5:30 (the docs' own example) is UTC+05:30, GMT+5 is UTC-05:00.
    A POSIX string WITH dst rules stays unresolvable (modelling vendor DST
    rules approximately would silently shift ticks)."""
    ist = resolve_timezone("IST-5:30")
    assert ist is not None and ist.how == "posix"
    assert ist.tz.utcoffset(None) == timedelta(hours=5, minutes=30)
    gmt5 = resolve_timezone("GMT+5")
    assert gmt5 is not None and gmt5.tz.utcoffset(None) == timedelta(hours=-5)
    assert resolve_timezone("MET-1METDST") is None


def test_resolve_timezone_chain_limit_and_cycles_stay_unresolved() -> None:
    """(SEM-35: 'the ujo_timezones table is read up to five times'): a
    six-hop chain exhausts the limit; a cycle terminates as unresolvable."""
    deep = {
        "a": "b",
        "b": "c",
        "c": "d",
        "d": "e",
        "e": "f",
        "f": "Europe/Zurich",
    }
    assert resolve_timezone("a", deep) is None  # 6th read would be needed
    assert resolve_timezone("b", deep) is not None  # 5 reads suffice from here
    assert resolve_timezone("loop", {"loop": "pool", "pool": "loop"}) is None


def test_resolve_timezone_map_suppresses_the_city_default() -> None:
    """(DL-62): a supplied listing is complete estate truth -- a city name
    missing from it does NOT fall back to the zoneinfo city match."""
    assert resolve_timezone("Zurich") is not None
    assert resolve_timezone("Zurich", {"dallas": "US/Central"}) is None


def test_resolve_timezone_ambiguous_city_component_is_refused() -> None:
    """(DL-62): the city default requires a UNIQUE match; multiple zones
    sharing a final path component resolve to none (the preflight ERROR
    names the candidates)."""
    from dsl41 import runner_scheduler as runner_mod

    monkey = {"x": ("A/X", "B/X")}
    original = runner_mod._zone_tables
    runner_mod._zone_tables = lambda: ({}, monkey)  # type: ignore[assignment]
    try:
        assert resolve_timezone("x") is None
        assert resolve_timezone("X") is None
    finally:
        runner_mod._zone_tables = original


def test_parse_timezone_map_listing_pairs_and_junk() -> None:
    """(DL-62): the autotimezone -l shape (header + ruler + 3-field rows)
    and bare pairs parse; names fold case and -/_; junk raises naming the
    line (no silent loss)."""
    listing = (
        "Entry Type Zone\n"
        "---------------------- ------ ----------------\n"
        "US/Samoa Alias Pacific/Samoa\n"
        "Port-au-Prince City America/Port-au-Prince\n"
        "\n"
        "Zurich Europe/Zurich\n"
    )
    aliases = parse_timezone_map(listing)
    assert aliases == {
        "us/samoa": "Pacific/Samoa",
        "port_au_prince": "America/Port-au-Prince",
        "zurich": "Europe/Zurich",
    }
    with pytest.raises(ValueError, match="line 1"):
        parse_timezone_map("this is not a listing row\n")


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


def test_run_calendar_fires_only_on_the_listed_dates() -> None:
    """(ss5 DL-56): a standard calendar's date rows are the run days --
    membership by day, ticks from start_times; nothing fires between rows."""
    text = (
        "calendar: hol\n07/03/2026 00:00\n07/06/2026 00:00\n\n"
        "insert_job: cal_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: hol\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 10, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 7, 3, 8, 0), datetime(2026, 7, 6, 8, 0)]


def test_exclude_calendar_subtracts_days_from_days_of_week() -> None:
    """(ss5 SEM-31/DL-56): exclude_calendar subtracts days from whichever
    run set is active -- here mo-fr minus the 07/03 holiday row."""
    text = (
        "calendar: hol\n07/03/2026 00:00\n\n"
        "insert_job: excl_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: mo,tu,we,th,fr\n"
        'start_times: "08:00"\nexclude_calendar: hol\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 2, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 6, 23, 59))
    # Thursday 07-02 and Monday 07-06 fire; Friday 07-03 is excluded
    assert [e.at for e in due] == [datetime(2026, 7, 2, 8, 0), datetime(2026, 7, 6, 8, 0)]


def test_exclude_calendar_subtracts_days_from_run_calendar() -> None:
    """(ss5 SEM-31/DL-56): run_calendar minus exclude_calendar -- a date in
    both sets never fires."""
    text = (
        "calendar: runs\n07/03/2026 00:00\n07/06/2026 00:00\n\n"
        "calendar: skips\n07/03/2026 00:00\n\n"
        "insert_job: both_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: runs\nexclude_calendar: skips\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 10, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 7, 6, 8, 0)]


def test_run_calendar_membership_is_evaluated_on_the_local_day() -> None:
    """(ss5 DL-56): the calendar day is the job's LOCAL day -- Auckland
    (+12) 00:30 on 07/01 is 12:30 UTC on 06/30, and still fires."""
    text = (
        "calendar: nzd\n07/01/2026 00:00\n\n"
        "insert_job: tz_cal\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: nzd\ntimezone: Pacific/Auckland\n"
        'start_times: "00:30"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 6, 29, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 2, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 6, 30, 12, 30)]


def test_exhausted_run_calendar_leaves_the_job_dormant() -> None:
    """(ss5 DL-56): a finite date list running out is the calendar meaning
    what it says -- after the last row the job has no next occurrence and
    pop_due drops it; never an error."""
    text = (
        "calendar: once\n07/03/2026 00:00\n\n"
        "insert_job: one_shot\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: once\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 7, 3, 8, 0)
    due = sched.pop_due(datetime(2026, 7, 10, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 7, 3, 8, 0)]
    assert sched.next_occurrence() is None
    assert sched.pop_due(datetime(2027, 7, 10, 0, 0)) == []
    # already exhausted at construction: dormant from the start
    stale = Scheduler(lower_source(text), start=datetime(2026, 8, 1, 0, 0))
    assert stale.next_occurrence() is None


def test_scheduler_refuses_dangling_defective_and_unparseable_calendars() -> None:
    """(ss5 DL-56/57): preflight ERROR territory -- a hand-built catalog
    that bypassed ss8 refuses comprehensibly, never guesses. Extended
    calendars refuse only for what the interpreter cannot express."""
    from dsl41.runner_clock import EngineError

    job = (
        "insert_job: cal_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: {ref}\nstart_times: "08:00"\n'
    )
    with pytest.raises(EngineError, match="no calendar definition"):
        Scheduler(lower_source(job.format(ref="ghost")), start=datetime(2026, 7, 1, 0, 0))
    unknown = (
        "extended_calendar: rules\nworkday: mo,tu,we,th,fr\ncondition: MONTH#L\n\n"
        + job.format(ref="rules")
    )
    with pytest.raises(EngineError, match="unknown date-condition token"):
        Scheduler(lower_source(unknown), start=datetime(2026, 7, 1, 0, 0))
    bad_row = "calendar: bad\nnot-a-date\n\n" + job.format(ref="bad")
    with pytest.raises(EngineError, match="unparseable date row"):
        Scheduler(lower_source(bad_row), start=datetime(2026, 7, 1, 0, 0))


def test_scheduler_honors_extended_calendar_rules() -> None:
    """(ss5 DL-57): a valid extended calendar schedules -- EOMWORK fires on
    the last workday of each month at the job's start_times."""
    text = (
        "extended_calendar: eow\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n\n"
        "insert_job: monthly\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: eow\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    # July 31 2026 is a Friday; August's last workday is Monday the 31st
    assert sched.next_occurrence() == datetime(2026, 7, 31, 8, 0)
    due = sched.pop_due(datetime(2026, 9, 1, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 7, 31, 8, 0), datetime(2026, 8, 31, 8, 0)]


def test_scheduler_run_calendar_without_start_times_fires_at_row_times() -> None:
    """(ss5 E11 RESOLVED, DL-58): a run_calendar job with neither
    start_times nor start_mins fires at each calendar row's own HH:MM
    tail -- a bare row fires at 00:00, and one day may carry several
    rows/ticks (CA support worked examples: '08/24/2014 16:00' fires at
    16:00; 'if the date in the calendar has no time ... 00:00')."""
    text = (
        "calendar: rows\n07/03/2026 09:30\n07/03/2026 16:00\n07/10/2026\n\n"
        "insert_job: rowtime\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: rows\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 8, 1, 0, 0))
    assert [e.at for e in due] == [
        datetime(2026, 7, 3, 9, 30),
        datetime(2026, 7, 3, 16, 0),
        datetime(2026, 7, 10, 0, 0),
    ]


def test_scheduler_job_start_times_override_calendar_row_times() -> None:
    """(ss5 E11, DL-58): job-level start_times override the rows' own
    times -- the calendar contributes only day membership then."""
    text = (
        "calendar: rows\n07/03/2026 09:30\n\n"
        "insert_job: overridden\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: rows\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert [e.at for e in sched.pop_due(datetime(2026, 8, 1, 0, 0))] == [datetime(2026, 7, 3, 8, 0)]


def test_scheduler_extended_run_calendar_without_start_times_ticks_at_midnight() -> None:
    """(ss5 E11, DL-58): a GENERATED (extended) day has no row time and the
    job supplies none -- the tick defaults to 00:00."""
    text = (
        "extended_calendar: eow\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n\n"
        "insert_job: gen_midnight\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: eow\n"
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 7, 31, 0, 0)  # July's last workday


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
        # source="scheduler": the engine tags its ticks, and cause strings are
        # a function of the input including source (DL-68)
        o.feed(Event(at=at, kind="STARTJOB", payload={"job": "bisim_sched"}, source="scheduler"))
        o.feed(Event(at=at, kind="STATUS", payload={"job": "bisim_sched", "status": "SUCCESS"}))

    assert [t.model_dump() for t in o.trace()] == [t.model_dump() for t in engine.oracle.trace()]


def test_scheduler_tick_start_cause_and_started_by_carry_scheduler_source() -> None:
    """(DL-68): a calendar-tick start is attributable -- the trace cause and
    JobRuntime.started_by both read "STARTJOB event (scheduler)", never the
    bare string an unattributed injection leaves."""
    text = (
        "insert_job: prov_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    catalog = lower_source(text)
    start = datetime(2026, 7, 1, 0, 0)

    async def scenario() -> Engine:
        adapter = FakeAdapter()
        engine = Engine(
            catalog,
            clock=VirtualClock(start=start),
            adapters={"CMD": adapter, "FW": adapter},
            scheduler=Scheduler(catalog, start=start),
        )
        await engine.run_until_quiescent(start + timedelta(hours=9))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    starting = next(t for t in engine.oracle.trace() if t.transition == "INACTIVE->STARTING")
    assert starting.cause == "STARTJOB event (scheduler)"
    assert engine.oracle.store.job["prov_sched"].started_by == "STARTJOB event (scheduler)"


def test_source_tag_is_generic_across_arbitrary_sources_not_hardcoded() -> None:
    """(DL-68): the cause-tagging mechanism keys off Event.source verbatim,
    for any string the ss7 input alphabet carries -- not a lookup table
    special-casing "scheduler"/"control". Engine-level reconcile only ever
    re-injects STATUS completions (never a start event), so a direct Oracle
    feed with source="reconcile" is the only way to pin that a re-injected
    START would tag "(reconcile)" exactly like the others, per the single
    `cause = f"{kind} event ({ev.source})"` rule (oracle.py)."""
    text = "insert_job: prov_any\njob_type: c\ncommand: x\nmachine: m1\n"
    o = Oracle(lower_source(text))
    at = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "prov_any"}, source="reconcile"))
    starting = next(t for t in o.trace() if t.transition == "INACTIVE->STARTING")
    assert starting.cause == "STARTJOB event (reconcile)"
    assert o.store.job["prov_any"].started_by == "STARTJOB event (reconcile)"


def test_sourceless_startjob_keeps_the_bare_untagged_cause_format() -> None:
    """(DL-68): Event.source defaults to None for an unattributed internal
    dispatch (the bisim harness, oracle-direct scripts) -- the cause stays
    the pre-DL-68 bare string, no trailing "()" or "(None)"."""
    text = "insert_job: prov_none\njob_type: c\ncommand: x\nmachine: m1\n"
    o = Oracle(lower_source(text))
    at = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "prov_none"}))
    starting = next(t for t in o.trace() if t.transition == "INACTIVE->STARTING")
    assert starting.cause == "STARTJOB event"
    assert o.store.job["prov_none"].started_by == "STARTJOB event"


def test_condition_edge_start_leaves_started_by_as_the_bare_edge_cause() -> None:
    """(DL-68): a condition-edge start (SEM-01, Oracle._wake_referencers) is
    never sourced -- only the explicit STARTJOB/FORCE_STARTJOB/TIMER event
    dispatch tags a source suffix. started_by carries the edge's own cause
    ("status of 'dep' changed to SUCCESS") verbatim, with no "(...)" tail."""
    text = (
        "insert_job: edge_dep\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: edge_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(edge_dep)\n"
    )
    o = Oracle(lower_source(text))
    at = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=at, kind="STATUS", payload={"job": "edge_dep", "status": "SUCCESS"}))
    starting = next(
        t for t in o.trace() if t.job == "edge_cons" and t.transition == "INACTIVE->STARTING"
    )
    assert starting.cause == "status of 'edge_dep' changed to SUCCESS"
    assert o.store.job["edge_cons"].started_by == "status of 'edge_dep' changed to SUCCESS"


def test_started_by_reflects_the_most_recent_start_not_the_first() -> None:
    """(DL-68): started_by is overwritten on every _start, not set-once --
    a second run's cause replaces the first's, exactly like `status_at`."""
    text = "insert_job: prov_two\njob_type: c\ncommand: x\nmachine: m1\n"
    o = Oracle(lower_source(text))
    at = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "prov_two"}, source="scheduler"))
    o.feed(Event(at=at, kind="STATUS", payload={"job": "prov_two", "status": "SUCCESS"}))
    assert o.store.job["prov_two"].started_by == "STARTJOB event (scheduler)"

    at2 = at + timedelta(minutes=5)
    o.feed(Event(at=at2, kind="FORCE_STARTJOB", payload={"job": "prov_two"}, source="control"))
    assert o.store.job["prov_two"].started_by == "FORCE_STARTJOB event (control)"


def test_run_window_deferred_start_replays_the_original_events_provenance() -> None:
    """(DL-68 review): the SEM-33 defer re-dispatches through an internal
    TIMER event -- the fired start must carry the deferred event's
    provenance, not collapse to the bare "TIMER event" that made a
    window-deferred scheduler tick indistinguishable from a control-socket
    one."""
    text = (
        "insert_job: rw_prov\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n\n'
        "insert_job: dummy_rwp\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = Oracle(lower_source(text))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 50),
            kind="STARTJOB",
            payload={"job": "rw_prov"},
            source="scheduler",
        )
    )
    defer = next(t for t in o.trace() if t.transition == "RUN_WINDOW_DEFER")
    assert "STARTJOB event (scheduler)" in defer.cause
    # an unrelated later event drives the timer heap past window open
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 10, 1),
            kind="STATUS",
            payload={"job": "dummy_rwp", "status": "SUCCESS"},
        )
    )
    starting = next(
        t for t in o.trace() if t.job == "rw_prov" and t.transition == "INACTIVE->STARTING"
    )
    assert starting.cause == "run_window-deferred STARTJOB event (scheduler)"
    assert o.store.job["rw_prov"].started_by == "run_window-deferred STARTJOB event (scheduler)"


def test_start_refused_record_carries_the_events_source() -> None:
    """(DL-68 review): a refusal is the moment the operator most needs to
    know WHOSE start died -- the START_REFUSED record carries the same
    source-tagged cause the successful path does, so a scheduler tick and
    an operator sendevent rejected at a SEM-10 gate no longer collapse to
    one line."""
    text = (
        "insert_job: box_ref\njob_type: b\n\n"
        "insert_job: mem_ref\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_ref\n"
    )
    o = Oracle(lower_source(text))
    at = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "mem_ref"}, source="control"))
    refused = next(t for t in o.trace() if t.transition == "START_REFUSED")
    assert refused.cause.endswith("(STARTJOB event (control))")

    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "mem_ref"}))
    bare = [t for t in o.trace() if t.transition == "START_REFUSED"][-1]
    assert bare.cause.endswith("(STARTJOB event)")


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
    from dsl41.runner_journal import Journal

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
    assert any(i.code == "job-type" and i.severity == "ERROR" and i.job == "weird" for i in items)


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


def _one_local_name() -> str:
    """A hostname the runner accepts as this host, for building fixtures
    whose machines deterministically resolve local (DL-49)."""
    return socket_mod.gethostname()


def test_preflight_machine_resolves_agent_node_name_to_local() -> None:
    # A job pinned to an agent whose node_name IS this host must run, even
    # though the agent's logical name is not a hostname (DL-49).
    host = _one_local_name()
    text = (
        f"insert_machine: unixagent\ntype: a\nnode_name: {host}\n\n"
        "insert_job: m_job\njob_type: c\ncommand: x\nmachine: unixagent\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "machine" for i in items)


def test_preflight_machine_rejects_agent_whose_node_name_is_foreign() -> None:
    text = (
        "insert_machine: unixagent\ntype: a\nnode_name: some-other-host.example.com\n\n"
        "insert_job: m_job\njob_type: c\ncommand: x\nmachine: unixagent\n"
    )
    items = preflight(lower_source(text))
    assert any(i.code == "machine" and i.severity == "ERROR" and i.job == "m_job" for i in items)


def test_preflight_machine_accepts_all_local_virtual_pool() -> None:
    # The reported bug: a job on a `type: v` pool whose members all resolve
    # to this host was refused ("machine X is not this host"). It must run.
    host = _one_local_name()
    text = (
        f"insert_machine: a1\ntype: a\nnode_name: {host}\n\n"
        f"insert_machine: a2\ntype: a\nnode_name: {host}\n\n"
        "insert_machine: pool\ntype: v\nmachine: a1\nmachine: a2\n\n"
        "insert_job: m_job\njob_type: c\ncommand: x\nmachine: pool\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "machine" for i in items)


def test_preflight_machine_mixed_pool_strict_refuses_local_eligible_warns() -> None:
    host = _one_local_name()
    text = (
        f"insert_machine: here\ntype: a\nnode_name: {host}\n\n"
        "insert_machine: there\ntype: a\nnode_name: elsewhere.example.com\n\n"
        "insert_machine: pool\ntype: v\nmachine: here\nmachine: there\n\n"
        "insert_job: m_job\njob_type: c\ncommand: x\nmachine: pool\n"
    )
    catalog = lower_source(text)
    strict = preflight(catalog)  # default machine_policy="strict"
    assert any(i.code == "machine" and i.severity == "ERROR" for i in strict)
    lenient = preflight(catalog, machine_policy="local-eligible")
    assert not any(i.severity == "ERROR" for i in lenient)
    assert any(i.code == "machine-mixed" and i.severity == "WARN" for i in lenient)


def test_preflight_machine_errors_on_malformed_definitions() -> None:
    cases = {
        "no_node": "insert_machine: no_node\ntype: a\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: no_node\n",
        "empty_pool": "insert_machine: p\ntype: v\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: p\n",
        "undefined_member": "insert_machine: p\ntype: v\nmachine: ghost\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: p\n",
        "no_type": "insert_machine: m\nnode_name: whatever\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m\n",
    }
    for label, text in cases.items():
        items = preflight(lower_source(text))
        assert any(i.code == "machine" and i.severity == "ERROR" for i in items), label


def test_resolve_machine_verdicts_direct() -> None:
    local = frozenset({"localhost", "thisbox", "thisbox.example.com"})
    text = (
        "insert_machine: here\ntype: a\nnode_name: thisbox.example.com\n\n"
        "insert_machine: there\ntype: a\nnode_name: otherbox.example.com\n\n"
        "insert_machine: all_local\ntype: v\nmachine: here\n\n"
        "insert_machine: mixed\ntype: v\nmachine: here\nmachine: there\n\n"
        "insert_machine: nested\ntype: v\nmachine: all_local\n"
    )
    machines = lower_source(text).machines
    assert resolve_machine("here", machines, local).verdict == "local"
    assert resolve_machine("there", machines, local).verdict == "foreign"
    assert resolve_machine("all_local", machines, local).verdict == "local"
    assert resolve_machine("mixed", machines, local).verdict == "mixed"
    assert resolve_machine("nested", machines, local).verdict == "error"  # no v-in-v
    # undefined name falls back to a literal host comparison (back-compat)
    assert resolve_machine("thisbox", machines, local).verdict == "local"
    assert resolve_machine("nope.example.com", machines, local).verdict == "foreign"


def test_resolve_machine_declared_name_wins_over_node_name() -> None:
    """DL-52 (the user's case): the runner declares it IS 'greezy_spoon'. A job
    on greezy_spoon runs here even though insert_machine maps it to a foreign-
    looking node -- the declared identity is authoritative over node_name."""
    identity = frozenset({"localhost", "greezy_spoon"})
    machines = lower_source(
        "insert_machine: greezy_spoon\ntype: a\nnode_name: ip-10-0-3-42\n"
    ).machines
    assert resolve_machine("greezy_spoon", machines, identity).verdict == "local"


def test_resolve_machine_declared_node_also_matches_via_resolution() -> None:
    """DL-52: declaring the NODE instead of the record name also works -- the
    job's machine resolves through insert_machine to a node in the identity."""
    identity = frozenset({"localhost", "ip-10-0-3-42"})
    machines = lower_source(
        "insert_machine: greezy_spoon\ntype: a\nnode_name: ip-10-0-3-42\n"
    ).machines
    assert resolve_machine("greezy_spoon", machines, identity).verdict == "local"


def test_resolve_machine_foreign_when_neither_name_nor_node_declared() -> None:
    """DL-52: a job on a machine this runner does not answer to -- by name or
    resolved node -- is refused foreign, with no hostname fallback."""
    identity = frozenset({"localhost", "greezy_spoon"})
    machines = lower_source("insert_machine: other_box\ntype: a\nnode_name: prod-7\n").machines
    assert resolve_machine("other_box", machines, identity).verdict == "foreign"


def test_local_identity_default_is_hostname_no_reverse_dns(monkeypatch) -> None:
    """DL-52: the omitted-identity default is the FORWARD hostname + localhost,
    and getfqdn() is never called (no reverse-DNS stall / namespace guess)."""
    monkeypatch.setattr(socket_mod, "getfqdn", _boom)  # must not be called
    names = _local_identity()
    assert "localhost" in names
    assert socket_mod.gethostname().lower() in names


def test_local_identity_declared_is_explicit_no_hostname() -> None:
    """DL-52: declaring `--as-machine` makes identity EXACTLY those names plus
    localhost -- no hostname guessing (pure explicit)."""
    names = _local_identity(frozenset({"Greezy_Spoon"}))
    assert names == frozenset({"localhost", "greezy_spoon"})


def test_resolve_machine_unquotes_node_name() -> None:
    # review: a quoted node_name is a hostname, not opaque text -- it must
    # resolve, not be compared with the quotes still on.
    local = frozenset({"localhost", "thisbox"})
    machines = lower_source('insert_machine: m\ntype: a\nnode_name: "thisbox"\n').machines
    assert resolve_machine("m", machines, local).verdict == "local"


def test_resolve_machine_uppercase_node_name_key_resolves() -> None:
    # review: NODE_NAME (any case) must resolve, not read as a missing node.
    local = frozenset({"localhost", "thisbox"})
    machines = lower_source("insert_machine: m\ntype: a\nNODE_NAME: thisbox\n").machines
    assert resolve_machine("m", machines, local).verdict == "local"


def test_resolve_machine_empty_node_name_is_error_not_foreign() -> None:
    local = frozenset({"localhost", "thisbox"})
    machines = lower_source('insert_machine: m\ntype: a\nnode_name: ""\n').machines
    assert resolve_machine("m", machines, local).verdict == "error"


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


def test_preflight_calendar_errors_on_dangling_references() -> None:
    """(ss8 DL-56): a calendar name with no definition in the loaded set is
    ERROR here -- L018's lint WARN, fail-closed at run (the same strictness
    split as L016-vs-DL-50 resources)."""
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


def test_preflight_calendar_clean_for_a_resolvable_standard_calendar() -> None:
    """(ss8 DL-56): a standard calendar with parseable rows and start_times
    raises nothing -- the estate runs."""
    text = (
        "calendar: hol\n07/03/2026 00:00\n12/25/2026 00:00\n\n"
        "insert_job: ok\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: hol\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    assert not any(i.code == "calendar" for i in items)


def test_preflight_calendar_errors_on_defective_extended_and_bad_row() -> None:
    """(ss8 DL-56/57): an extended calendar the interpreter cannot express
    (unknown token here) and an unparseable date row each refuse with their
    own message; run_calendar without start_times is a VALID row-time shape
    since DL-58 (E11 resolved), and a valid extended calendar sails
    through."""
    text = (
        "extended_calendar: rules\nworkday: mo,tu,we,th,fr\ncondition: MONTH#L\n\n"
        "extended_calendar: fine\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n\n"
        "calendar: bad\nnot-a-date\n\n"
        "calendar: rowtime\n07/03/2026 09:30\n\n"
        "insert_job: e1\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: rules\nstart_times: "08:00"\n\n'
        "insert_job: e2\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: bad\nstart_times: "08:00"\n\n'
        "insert_job: e3\njob_type: c\ncommand: x\nmachine: localhost\n"
        "date_conditions: 1\nrun_calendar: rowtime\n\n"
        "insert_job: ok\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: fine\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text))
    by_job = {i.job: i.message for i in items if i.code == "calendar" and i.severity == "ERROR"}
    assert "unknown date-condition token" in by_job["e1"]
    assert "unparseable date row" in by_job["e2"]
    assert "e3" not in by_job  # row-time firing is a valid shape (E11, DL-58)
    assert "ok" not in by_job


def test_preflight_calendar_warns_when_exhausted_or_fully_excluded() -> None:
    """(ss8 DL-56): silent-never-fires is preflight's business -- WARN when
    the run set minus exclude set is empty, and (given a start anchor) when
    the last eligible date lies before the run start. No anchor, no
    exhaustion check."""
    excluded = (
        "calendar: runs\n07/03/2026 00:00\n\ncalendar: skips\n07/03/2026 00:00\n\n"
        "insert_job: w1\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: runs\nexclude_calendar: skips\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(excluded))
    assert any(
        i.severity == "WARN" and i.code == "calendar" and "no eligible dates" in i.message
        for i in items
    )
    stale = (
        "calendar: past\n07/03/2026 00:00\n\n"
        "insert_job: w2\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: past\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(stale), start=datetime(2026, 8, 1, 0, 0))
    assert any(
        i.severity == "WARN" and i.code == "calendar" and "exhausted" in i.message for i in items
    )
    assert not any(i.severity == "ERROR" and i.code == "calendar" for i in items)
    # no anchor: the same catalog is silent (bare-construction callers)
    items = preflight(lower_source(stale))
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


def test_preflight_timezone_city_name_warns_and_runs() -> None:
    """(SEM-35/DL-62): with no --timezone-map a vendor city name resolves
    through the unique zoneinfo city match -- a WARN (the assumption is
    recorded), never an ERROR."""
    text = (
        "insert_job: tzc\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: Zurich\n'
    )
    items = preflight(lower_source(text), execution=False)
    tz_items = [i for i in items if i.code == "timezone"]
    assert [i.severity for i in tz_items] == ["WARN"]
    assert "Europe/Zurich" in tz_items[0].message


def test_preflight_timezone_map_is_estate_truth() -> None:
    """(DL-62): a supplied listing resolves its entries silently and turns
    the city default OFF -- a name missing from it is an ERROR naming the
    --timezone-map remedy."""
    text = (
        "insert_job: tzm\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: Zurich\n\n'
        "insert_job: tzx\njob_type: c\ncommand: y\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\ntimezone: Dallas\n'
    )
    catalog = lower_source(text)
    aliases = parse_timezone_map("Zurich City Europe/Zurich\n")
    items = preflight(catalog, execution=False, tz_aliases=aliases)
    tz_items = {i.job: i for i in items if i.code == "timezone"}
    assert "tzm" not in tz_items  # mapped: resolves silently, no assumption
    assert tz_items["tzx"].severity == "ERROR"
    assert "--timezone-map" in tz_items["tzx"].message


def test_preflight_timezone_posix_offset_warns_about_the_sign() -> None:
    """(SEM-35, TechDocs TZ syntax): a POSIX fixed offset runs, with a WARN
    spelling out the west-positive sign convention."""
    text = (
        "insert_job: tzp\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\ntimezone: "IST-5:30"\n'
    )
    items = preflight(lower_source(text), execution=False)
    tz_items = [i for i in items if i.code == "timezone"]
    assert [i.severity for i in tz_items] == ["WARN"]
    assert "UTC+05:30" in tz_items[0].message


def test_preflight_n_retrys_warns() -> None:
    text = "insert_job: nr\njob_type: c\ncommand: x\nmachine: localhost\nn_retrys: 3\n"
    items = preflight(lower_source(text))
    assert any(i.code == "n-retrys" and i.severity == "WARN" and i.job == "nr" for i in items)


def test_preflight_n_retrys_clean_when_unset() -> None:
    text = "insert_job: nr0\njob_type: c\ncommand: x\nmachine: localhost\n"
    items = preflight(lower_source(text))
    assert not any(i.code == "n-retrys" for i in items)


def test_preflight_resources_refuses_unsized_and_malformed(monkeypatch) -> None:
    """DL-50: resources are honored, so preflight refuses (fail-closed) only
    the unmodelable shapes -- an unsized semaphore and a non-integer load."""
    monkeypatch.setattr(socket_mod, "getfqdn", lambda *a: "test.host")  # dodge slow reverse-DNS
    text = (
        "insert_job: rl1\njob_type: c\ncommand: y\nmachine: localhost\n"
        "resources: (r_unsized, quantity=2, free=y)\n\n"  # no insert_resource -> unsized
        "insert_job: rl2\njob_type: c\ncommand: x\nmachine: localhost\njob_load: heavy\n"  # malformed
    )
    items = preflight(lower_source(text))
    refused = {i.job for i in items if i.code == "resources" and i.severity == "ERROR"}
    assert refused == {"rl1", "rl2"}


def test_preflight_resources_honored_when_sized_is_clean(monkeypatch) -> None:
    """A sized renewable semaphore + a job_load with no throttling machine are
    both HONORED (not refused): no `resources` preflight item at all (DL-50)."""
    monkeypatch.setattr(socket_mod, "getfqdn", lambda *a: "test.host")  # dodge slow reverse-DNS
    text = (
        "insert_resource: SIZED_LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: rl_ok\njob_type: c\ncommand: x\nmachine: localhost\njob_load: 50\n"
        "resources: (SIZED_LOCK, QUANTITY=1)\n"
    )
    items = preflight(lower_source(text))
    assert not any(i.code == "resources" for i in items)


def test_preflight_resources_refuses_duplicate_and_unsatisfiable(monkeypatch) -> None:
    """DL-50 (adversarial review): preflight refuses a resource requested twice
    (ambiguous demand, would over-commit the bucket) and a QUANTITY that exceeds
    the resource's amount (statically unsatisfiable -- would hang forever)."""
    monkeypatch.setattr(socket_mod, "getfqdn", lambda *a: "test.host")
    text = (
        "insert_resource: DUPR\nres_type: R\namount: 3\n\n"
        "insert_resource: TINY\nres_type: R\namount: 1\n\n"
        "insert_job: dup\njob_type: c\ncommand: x\nmachine: localhost\n"
        "resources: (DUPR, QUANTITY=1) AND (DUPR, QUANTITY=1)\n\n"
        "insert_job: big\njob_type: c\ncommand: y\nmachine: localhost\nresources: (TINY, QUANTITY=5)\n"
    )
    items = preflight(lower_source(text))
    refused = {i.job for i in items if i.code == "resources" and i.severity == "ERROR"}
    assert refused == {"dup", "big"}


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
    import dsl41.runner_preflight as runner_mod

    from dsl41.oracle_state import OracleError

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
