"""Branch gaps in runner_scheduler.py that the DL-105 gate list leaves open (F5b).

The refusal the Scheduler states for a calendar that compiles and then fails
when it generates, an input preflight would have stopped first (runner-design
ss8). The test builds that input and holds the EngineError the Scheduler
raises in its place. Its two sibling refusals (an empty or unknown day list)
need an IR lowering refuses, so they carry DL-105 exclusions instead.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dsl41.ir import lower_source
from dsl41.runner_clock import EngineError
from dsl41.runner_scheduler import Scheduler

START = datetime(2026, 6, 1, 0, 0)


def _walk_calendar_text(job_attrs: str) -> str:
    """An extended calendar that compiles and fails to generate: `holiday: W`
    walks from 4 July 2026 to a working Monday, and every Monday for sixty
    weeks after it is a holiday, so the walk finds none (the same calendar as
    the preflight generation-error test in test_autocal_breadth.py)."""
    mondays = [date(2026, 7, 6) + timedelta(days=7 * i) for i in range(60)]
    rows = "\n".join(f"{d:%m/%d/%Y}" for d in [date(2026, 7, 4), *mondays])
    return (
        f"calendar: hols\n{rows}\n\n"
        "extended_calendar: walkcap\nworkday: mo\nholiday: W\n"
        "holcal: hols\ncondition: jul#4\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: localhost\n"
        f'date_conditions: 1\n{job_attrs}start_times: "08:00"\n'
    )


def test_dl057_a_calendar_that_fails_to_generate_is_an_engine_error_naming_it() -> None:
    """DL-57: the scheduler reads an extended calendar a block of a year at a
    time. A rule that compiled and then cannot generate a day surfaces as an
    EngineError carrying the calendar's own message, not as the autocal
    exception (preflight reports the same failure as an ERROR item first)."""
    catalog = lower_source(_walk_calendar_text("run_calendar: walkcap\n"))
    with pytest.raises(EngineError, match=r"extended calendar 'walkcap'.*no valid day"):
        Scheduler(catalog, start=START)


def test_dl057_the_same_catalog_on_a_weekday_list_schedules_its_first_tick() -> None:
    """Twin: the walk calendar is defined but unused, and the job runs every
    day, so it schedules its tick on the start date."""
    catalog = lower_source(_walk_calendar_text("days_of_week: all\n"))
    assert Scheduler(catalog, start=START).next_occurrence() == datetime(2026, 6, 1, 8, 0)
