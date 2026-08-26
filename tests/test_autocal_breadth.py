"""Breadth tests for the autocal interpreter (DL-57), the Scheduler/preflight
wiring, and the ir.py CalendarIR/CycleIR lanes -- everything the FOCUSED
suites (tests/test_autocal.py, the calendar bits of tests/test_runner_scheduler.py
and tests/test_dsl.py) don't already pin. Every expected date is derived from
the real 2026/2027 Gregorian calendar independently of dsl41.autocal -- see
each test's docstring for the by-hand reasoning (day-of-week facts, month
lengths, leap-year arithmetic).

Section map: (1) token-family coverage per SEM-37's inventory table, (2)
generation edge behavior (boundary crossings, adjust corners, long scans),
(3) Scheduler integration with extended calendars, (4) preflight branches
specific to the autocal generator probe, (5) ir.py lowering lanes (condition
ordering, cycle pairing, ext_calendar/extended_calendar spelling).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dsl41.autocal import compile_calendar
from dsl41.ir import CalendarIR, CatalogIR, CycleIR, LoweringError, lower_source
from dsl41.runner_preflight import preflight
from dsl41.runner_scheduler import Scheduler

# ------------------------------------------------------------ builders
# (mirrors tests/test_autocal.py's local helpers; duplicated rather than
# imported so this file stays a self-contained, independently-readable unit)


def _catalog(**cals: CalendarIR) -> CatalogIR:
    return CatalogIR(jobs={}, calendars=dict(cals))


def _ext(name: str = "ext", conditions: list[str] | None = None, **attrs: str) -> CalendarIR:
    return CalendarIR(name=name, kind="extended", attrs=attrs, conditions=conditions or [])


def _days(cal: CalendarIR, lo: date, hi: date, **extra: CalendarIR) -> set[date]:
    catalog = _catalog(**{cal.name: cal, **extra})
    return set(compile_calendar(cal, catalog).days_between(lo, hi))


JUL26 = (date(2026, 7, 1), date(2026, 7, 31))


def _cycle_catalog(*conditions: str) -> tuple[CalendarIR, CatalogIR]:
    """Two periods, both starting on a Saturday: Mar28-Apr2 2026 (Sat-Thu)
    and Jun27-Jul2 2026 (Sat-Thu) -- each has exactly 4 Mon-Fri workdays."""
    cyc = CycleIR(
        name="cyc",
        periods=[("03/28/2026", "04/02/2026"), ("06/27/2026", "07/02/2026")],
    )
    cal = _ext(cyccal="cyc", conditions=list(conditions))
    catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"cyc": cyc})
    return cal, catalog


# =====================================================================
# 1. Token families the focused suite doesn't reach
# =====================================================================


def test_workd_ordinal_forward_and_backward() -> None:
    """July 2026 has 23 Mon-Fri workdays: Jul 1 (Wed) is the first, Jul 31
    (Fri) the last -- WORKD#01/WORKDM01 pin the ends, #02/M02 one step in
    from each end (Jul 2, Jul 30)."""
    assert _days(_ext(conditions=["WORKD#01"]), *JUL26) == {date(2026, 7, 1)}
    assert _days(_ext(conditions=["WORKD#02"]), *JUL26) == {date(2026, 7, 2)}
    assert _days(_ext(conditions=["WORKDM01"]), *JUL26) == {date(2026, 7, 31)}
    assert _days(_ext(conditions=["WORKDM02"]), *JUL26) == {date(2026, 7, 30)}


def test_fomwork_eomwork_exclude_forms() -> None:
    """XFOMWORK/XEOMWORK (infix exclusion) subtract the month's first/last
    workday from a union -- July 2026's 23-day WORKDAYS set loses just the
    matched end, 22 remain either way."""
    lo, hi = JUL26
    all_workdays = _days(_ext(conditions=["WORKDAYS"]), lo, hi)
    assert len(all_workdays) == 23
    minus_first = _days(_ext(conditions=["WORKDAYS", "XFOMWORK"]), lo, hi)
    assert minus_first == all_workdays - {date(2026, 7, 1)}
    minus_last = _days(_ext(conditions=["WORKDAYS", "XEOMWORK"]), lo, hi)
    assert minus_last == all_workdays - {date(2026, 7, 31)}


def test_fomweek_eomweek_ignore_the_custom_workday_mask() -> None:
    """FOMWEEK/EOMWEEK are always Mon-Fri (SEM-37's fixed weekday-of-month
    family) -- unlike FOMWORK/EOMWORK, which honor a custom `workday:`
    mask. October 2026's 1st is a Thursday and its last Mon-Fri day is Fri
    Oct 30; with workday: mo,tu,we those fall outside the custom mask, so
    the custom-workday ends land one step in: first Mon Oct 5, last Wed
    Oct 28."""
    narrow = {"workday": "mo,tu,we"}
    oct_lo, oct_hi = date(2026, 10, 1), date(2026, 10, 31)
    assert _days(_ext(conditions=["FOMWORK"], **narrow), oct_lo, oct_hi) == {date(2026, 10, 5)}
    assert _days(_ext(conditions=["FOMWEEK"], **narrow), oct_lo, oct_hi) == {date(2026, 10, 1)}
    assert _days(_ext(conditions=["EOMWORK"], **narrow), oct_lo, oct_hi) == {date(2026, 10, 28)}
    assert _days(_ext(conditions=["EOMWEEK"], **narrow), oct_lo, oct_hi) == {date(2026, 10, 30)}


def test_xfomweek_xeomweek_exclude_forms() -> None:
    """XFOMWEEK/XEOMWEEK subtract the month's first/last Mon-Fri day from a
    union; July 2026's WEEKDAYS set (23 days, no holcal to subtract) loses
    just the matched end."""
    lo, hi = JUL26
    weekdays = _days(_ext(conditions=["WEEKDAYS"]), lo, hi)
    assert len(weekdays) == 23
    assert _days(_ext(conditions=["WEEKDAYS", "XEOMWEEK"]), lo, hi) == weekdays - {
        date(2026, 7, 31)
    }
    assert _days(_ext(conditions=["WEEKDAYS", "XFOMWEEK"]), lo, hi) == weekdays - {date(2026, 7, 1)}


def test_weekdmn_backward_and_weekdxn_exclude() -> None:
    """Jan 1 2014 is a Wednesday (SEM-37's own worked example's anchor), so
    default (Jan1-anchored) weeks run Wed-Tue; the week containing the
    Jan6-12 2014 window is Jan1(Wed)-Jan7(Tue). WEEKDM01 (backward, 'last
    day of week') picks Jan 7; WEEKDX01 (exclude the FORWARD first day,
    Wed) removes Jan 8 from a 7-day DAILY set."""
    window = _days(_ext(conditions=["WEEKDM01"]), date(2014, 1, 6), date(2014, 1, 12))
    assert window == {date(2014, 1, 7)}
    excluded = _days(_ext(conditions=["DAILY", "WEEKDX01"]), date(2014, 1, 6), date(2014, 1, 12))
    assert excluded == {
        date(2014, 1, 6),
        date(2014, 1, 7),
        date(2014, 1, 9),
        date(2014, 1, 10),
        date(2014, 1, 11),
        date(2014, 1, 12),
    }


def test_week_of_year_family() -> None:
    """2026: Jan 1 is a Thursday (default anchor), so week 1 = Jan1-7, week
    2 = Jan8-14. Jan1-Dec31 spans 364 days, giving 53 week slots with week
    53 a lone day, Dec 31 (364 // 7 + 1 = 53; the 53rd slot only ever
    reaches offset 364, i.e. just that one day)."""
    assert _days(_ext(conditions=["WEEK#02"]), date(2026, 1, 1), date(2026, 1, 21)) == {
        date(2026, 1, 8) + timedelta(days=i) for i in range(7)
    }
    assert _days(_ext(conditions=["WEEK#E"]), date(2026, 1, 1), date(2026, 1, 14)) == {
        date(2026, 1, 8) + timedelta(days=i) for i in range(7)
    }
    assert _days(_ext(conditions=["WEEK#O"]), date(2026, 1, 1), date(2026, 1, 14)) == {
        date(2026, 1, 1) + timedelta(days=i) for i in range(7)
    }
    assert _days(_ext(conditions=["WEEKM01"]), date(2026, 12, 1), date(2026, 12, 31)) == {
        date(2026, 12, 31)
    }
    assert _days(_ext(conditions=["DAILY", "WEEKX01"]), date(2026, 1, 1), date(2026, 1, 7)) == set()


def test_mnthdxnn_excludes_a_day_of_month() -> None:
    """MNTHDXnn (infix exclude) removes the nnth day of the month from a
    union -- July's 31 days minus the 15th leaves 30, the 15th gone."""
    days = _days(_ext(conditions=["jul", "MNTHDX15"]), *JUL26)
    assert len(days) == 30
    assert date(2026, 7, 15) not in days


def test_xfom_xeom_as_inline_expression_complements() -> None:
    """Unlike the rule-LIST/standalone exclusion-subtraction form (already
    pinned for a different token in test_autocal.py's Q8d tests), XFOM/XEOM
    here sit INSIDE a single `&` expression with `jul` -- the exclusive
    flip applies per-atom during evaluation (_eval), not via the top-level
    exclusion-subtraction path (_exclusion_base)."""
    lo, hi = JUL26
    july_days = {date(2026, 7, d) for d in range(1, 32)}
    assert _days(_ext(conditions=["jul & XFOM"]), lo, hi) == july_days - {date(2026, 7, 1)}
    assert _days(_ext(conditions=["jul & XEOM"]), lo, hi) == july_days - {date(2026, 7, 31)}


def test_named_month_backward_and_exclude_forms() -> None:
    """julM01 (the M-backward convention) is the last day of July, the
    31st -- matching EOM; julM05 is 5 back from the end, the 27th.
    Xjul#04/XjulM01 (prefix exclude) subtract single named-month days from
    July's 31, standalone (subtracting from the implicit DAILY default)."""
    assert _days(_ext(conditions=["julM01"]), *JUL26) == {date(2026, 7, 31)}
    assert _days(_ext(conditions=["julM05"]), *JUL26) == {date(2026, 7, 27)}
    july_days = {date(2026, 7, d) for d in range(1, 32)}
    assert _days(_ext(conditions=["Xjul#04"]), *JUL26) == july_days - {date(2026, 7, 4)}
    assert _days(_ext(conditions=["XjulM01"]), *JUL26) == july_days - {date(2026, 7, 31)}


def test_dddmn_prefix_exclude_forms() -> None:
    """September 2026 has Fridays on the 4th, 11th, 18th, 25th (first/
    last); its last WORKDAY is the 30th (a Wednesday) -- a DIFFERENT day
    than its last Friday (the 25th), so excluding the last Friday leaves
    the month-end workday untouched (a distinctness EOMWORK-exclusion
    would not demonstrate, since there EOMWORK and the last Friday often
    coincide)."""
    sep_lo, sep_hi = date(2026, 9, 1), date(2026, 9, 30)
    workdays = _days(_ext(conditions=["WORKDAYS"]), sep_lo, sep_hi)
    assert len(workdays) == 22
    minus_first_fri = _days(_ext(conditions=["WORKDAYS", "Xfri#1"]), sep_lo, sep_hi)
    assert minus_first_fri == workdays - {date(2026, 9, 4)}
    minus_last_fri = _days(_ext(conditions=["WORKDAYS", "XfriM1"]), sep_lo, sep_hi)
    assert minus_last_fri == workdays - {date(2026, 9, 25)}
    assert date(2026, 9, 30) in minus_last_fri  # EOMWORK survives untouched


def test_cycl_backward_and_exclude_forms() -> None:
    """Period Mar28-Apr2 2026 (6 days): CYCLM01 (backward, 1 from the end)
    is the period's last day, Apr 2 -- the same day CYCL#06 reaches
    counting forward (the period is exactly 6 days long). CYCLX01 (infix
    exclude) removes each period's first day from the CYCLE union."""
    cal_m, catalog_m = _cycle_catalog("CYCLM01")
    assert set(
        compile_calendar(cal_m, catalog_m).days_between(date(2026, 1, 1), date(2026, 12, 31))
    ) == {date(2026, 4, 2), date(2026, 7, 2)}
    cal_f, catalog_f = _cycle_catalog("CYCL#06")
    assert set(
        compile_calendar(cal_f, catalog_f).days_between(date(2026, 1, 1), date(2026, 12, 31))
    ) == {date(2026, 4, 2), date(2026, 7, 2)}
    cal_x, catalog_x = _cycle_catalog("CYCLE", "CYCLX01")
    minus_first = set(
        compile_calendar(cal_x, catalog_x).days_between(date(2026, 1, 1), date(2026, 12, 31))
    )
    assert date(2026, 3, 28) not in minus_first
    assert date(2026, 6, 27) not in minus_first
    assert len(minus_first) == 10  # 12 cycle days minus the two period-starts


def test_cycp_selects_one_whole_period() -> None:
    """CYCP#02 selects every day of the SECOND period only (Jun27-Jul2
    2026), none of the first (Mar28-Apr2)."""
    cal, catalog = _cycle_catalog("CYCP#02")
    days = set(compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 12, 31)))
    assert days == {date(2026, 6, 27) + timedelta(days=i) for i in range(6)}


def test_cweek_parity_last_backward_and_exclude_forms() -> None:
    """A 20-day period (Jan1-20 2026) chunks into 7-day weeks from its own
    start (Q8e's documented default): week1 Jan1-7, week2 Jan8-14, week3
    Jan15-20 (a partial 6-day tail). #E is week2, #O is weeks 1+3, #L and
    M01 both land on the last chunk (week3), and X01 (infix exclude)
    removes week1 from the CYCLE union."""
    cyc = CycleIR(name="chunk", periods=[("01/01/2026", "01/20/2026")])

    def gen(*conditions: str) -> set[date]:
        cal = _ext(cyccal="chunk", conditions=list(conditions))
        catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"chunk": cyc})
        return set(compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 1, 31)))

    week1 = {date(2026, 1, 1) + timedelta(days=i) for i in range(7)}
    week2 = {date(2026, 1, 8) + timedelta(days=i) for i in range(7)}
    week3 = {date(2026, 1, 15) + timedelta(days=i) for i in range(6)}
    assert gen("CWEEK#E") == week2
    assert gen("CWEEK#O") == week1 | week3
    assert gen("CWEEK#L") == week3
    assert gen("CWEEKM01") == week3
    assert gen("CYCLE", "CWEEKX01") == week2 | week3


def test_cwrk_ordinal_backward_and_exclude_forms() -> None:
    """Period workdays: Mar28-Apr2 2026 -> Mon30, Tue31, Wed Apr1, Thu Apr2;
    Jun27-Jul2 2026 -> Mon29, Tue30, Wed Jul1, Thu Jul2 (both periods start
    on a Saturday, so each has exactly 4 Mon-Fri workdays). CWRK#01/#02
    pick the 1st/2nd from the front, CWRKM01 the last (the same day CWRK#L
    already pins in test_autocal.py), and CWRKX01/CWRKXL (infix exclude)
    remove the first/last workday from the CYCLE union."""

    def gen(*conditions: str) -> set[date]:
        cal, catalog = _cycle_catalog(*conditions)
        return set(
            compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 12, 31))
        )

    assert gen("CWRK#01") == {date(2026, 3, 30), date(2026, 6, 29)}
    assert gen("CWRK#02") == {date(2026, 3, 31), date(2026, 6, 30)}
    assert gen("CWRKM01") == {date(2026, 4, 2), date(2026, 7, 2)}
    minus_first = gen("CYCLE", "CWRKX01")
    assert date(2026, 3, 30) not in minus_first
    assert date(2026, 6, 29) not in minus_first
    assert len(minus_first) == 10
    minus_last = gen("CYCLE", "CWRKXL")
    assert date(2026, 4, 2) not in minus_last
    assert date(2026, 7, 2) not in minus_last
    assert len(minus_last) == 10


def test_cddd_last_backward_and_prefix_exclude_forms() -> None:
    """Tuesdays in July 2026 fall on the 7th, 14th, 21st, 28th (Jul 1 is a
    Wednesday) -- Ctue#L and CtueM01 both pick the last, the 28th (the same
    M01==last convention pinned elsewhere), and XCtue#01 (prefix exclude)
    removes the FIRST, the 7th, from a plain `jul` base."""
    cyc = CycleIR(name="julcyc", periods=[("07/01/2026", "07/31/2026")])

    def gen(*conditions: str) -> set[date]:
        cal = _ext(cyccal="julcyc", conditions=list(conditions))
        catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"julcyc": cyc})
        return set(compile_calendar(cal, catalog).days_between(*JUL26))

    assert gen("Ctue#L") == {date(2026, 7, 28)}
    assert gen("CtueM01") == {date(2026, 7, 28)}
    minus_first = gen("jul", "XCtue#01")
    assert len(minus_first) == 30
    assert date(2026, 7, 7) not in minus_first


# =====================================================================
# 2. Generation edge behavior
# =====================================================================


def test_w_walk_crosses_month_and_year_boundary() -> None:
    """Dec 31 2026 and Jan 1 2027 are BOTH holidays (holcal spans the year
    boundary); the W walk must cross both the month AND the year boundary
    to land on the first valid non-holiday workday: Jan 2 2027 is a
    Saturday, Jan 3 a Sunday (non-workdays under the default Mon-Fri mask),
    landing on Monday Jan 4 2027."""
    hol = CalendarIR(name="hols", kind="standard", dates=["12/31/2026 00:00", "01/01/2027 00:00"])
    cal = _ext(holiday="W", holcal="hols", conditions=["dec#31"])
    days = _days(cal, date(2026, 12, 1), date(2027, 1, 31), hols=hol)
    assert days == {date(2027, 1, 4)}


def test_negative_adjust_crosses_month_start() -> None:
    """adjust is a blind uniform offset (SEM-38): -3 applied to the 1st of
    Feb 2026 (a Sunday, MNTHD-independent of weekday) shifts to Jan 29
    2026 -- crossing from February back into January."""
    cal = _ext(adjust="-3", conditions=["feb#1"])
    assert _days(cal, date(2026, 1, 1), date(2026, 2, 28)) == {date(2026, 1, 29)}


def test_adjust_can_push_a_date_outside_its_own_cycle_period() -> None:
    """SEM-38: adjust is a BLIND offset with no landing-day re-validation --
    not even against a period's own bounds. Period Jan1-5 2026's CYCL#01
    (day 1 = Jan 1) shifted -3 lands Dec 29 2025, three days before the
    period even starts; the calendar must still generate it, never refuse."""
    cyc = CycleIR(name="c", periods=[("01/01/2026", "01/05/2026")])
    cal = _ext(cyccal="c", adjust="-3", conditions=["CYCL#01"])
    catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"c": cyc})
    days = set(compile_calendar(cal, catalog).days_between(date(2025, 12, 20), date(2026, 1, 10)))
    assert days == {date(2025, 12, 29)}


def test_backward_workday_ordinal_at_short_february() -> None:
    """Feb 2026 has 28 days and Feb 28 is a Saturday (2026 is not a leap
    year: not divisible by 4), so the LAST WORKDAY of February is Friday
    Feb 27 -- distinct from EOM/MNTHDM01, which would land on the 28th
    regardless of weekday."""
    assert _days(_ext(conditions=["WORKDM01"]), date(2026, 2, 1), date(2026, 2, 28)) == {
        date(2026, 2, 27)
    }


def test_wekr_anchor_crosses_year_boundary() -> None:
    """WEKR-anchored weeks are pure weekday-offset arithmetic (unlike the
    default Jan1 anchor, which recomputes per calendar year via
    `date(day.year, 1, 1)`), so a Saturday-anchored week can straddle
    Dec31/Jan1: Dec 26 2026 is a Saturday, and the following Friday is
    Jan 1 2027 (Jan 1 2027 is independently a Friday) -- the SAME anchored
    week. Querying exactly that 7-day window isolates it."""
    lo, hi = date(2026, 12, 26), date(2027, 1, 1)
    first = _days(_ext(conditions=["WEKRSat#01"]), lo, hi)
    last = _days(_ext(conditions=["WEKRSatM01"]), lo, hi)
    assert first == {date(2026, 12, 26)}
    assert last == {date(2027, 1, 1)}


def test_touching_cycle_periods_index_independently() -> None:
    """Adjoining periods (period 2 starts the day after period 1 ends, no
    gap) still index independently: CYCL#01 picks each period's OWN first
    day (Jan 1, Jan 6), not a bleed across the join, and CYCL#05 -- each
    period is exactly 5 days -- resolves to each period's own last day
    (Jan 5, Jan 10), never double-counting the boundary."""
    cyc = CycleIR(
        name="touch", periods=[("01/01/2026", "01/05/2026"), ("01/06/2026", "01/10/2026")]
    )
    cal1 = _ext(cyccal="touch", conditions=["CYCL#01"])
    catalog1 = CatalogIR(jobs={}, calendars={"ext": cal1}, cycles={"touch": cyc})
    assert set(
        compile_calendar(cal1, catalog1).days_between(date(2026, 1, 1), date(2026, 1, 31))
    ) == {date(2026, 1, 1), date(2026, 1, 6)}
    cal5 = _ext(cyccal="touch", conditions=["CYCL#05"])
    catalog5 = CatalogIR(jobs={}, calendars={"ext": cal5}, cycles={"touch": cyc})
    assert set(
        compile_calendar(cal5, catalog5).days_between(date(2026, 1, 1), date(2026, 1, 31))
    ) == {date(2026, 1, 5), date(2026, 1, 10)}


def test_first_on_or_after_jumps_multiple_years_to_next_leap_day() -> None:
    """2026 is not a leap year (not divisible by 4) and neither is 2027;
    the next Feb 29 is 2028 (2028 / 4 = 507, no century exception).
    first_on_or_after must scan past two full non-leap years -- its 365-day
    window loop -- rather than give up and return None."""
    cal = _ext(conditions=["feb#29"])
    compiled = compile_calendar(cal, _catalog(ext=cal))
    assert compiled.first_on_or_after(date(2026, 7, 1)) == date(2028, 2, 29)


# =====================================================================
# 3. Scheduler integration
# =====================================================================


def test_scheduler_extended_exclude_over_a_standard_run_calendar() -> None:
    """A standard run_calendar (three explicit rows) minus an extended
    exclude_calendar (a single-day rule, `jul#6`) drops just the matched
    row -- July 3/6/10 2026 minus the 6th leaves the 3rd and 10th."""
    text = (
        "calendar: runs\n07/03/2026 00:00\n07/06/2026 00:00\n07/10/2026 00:00\n\n"
        "extended_calendar: skip_one\nworkday: mo,tu,we,th,fr\ncondition: jul#6\n\n"
        "insert_job: mix1\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: runs\nexclude_calendar: skip_one\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 15, 0, 0))
    assert [e.at for e in due] == [datetime(2026, 7, 3, 8, 0), datetime(2026, 7, 10, 8, 0)]


def test_scheduler_extended_run_and_extended_exclude() -> None:
    """Both sides of the SEM-31 subtraction are rule calendars: run =
    WORKDAYS (every Mon-Fri), exclude = every Friday (`fri`). Over July
    1-10 2026 that nets Wed/Thu (1, 2) and Mon-Thu the following week (6,
    7, 8, 9); Fridays the 3rd and 10th are excluded, the weekend already
    outside WORKDAYS."""
    text = (
        "extended_calendar: workdays_ext\nworkday: mo,tu,we,th,fr\ncondition: WORKDAYS\n\n"
        "extended_calendar: fridays_ext\ncondition: fri\n\n"
        "insert_job: mix2\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: workdays_ext\nexclude_calendar: fridays_ext\n"
        'start_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    due = sched.pop_due(datetime(2026, 7, 10, 23, 59))
    assert [e.at for e in due] == [
        datetime(2026, 7, 1, 8, 0),
        datetime(2026, 7, 2, 8, 0),
        datetime(2026, 7, 6, 8, 0),
        datetime(2026, 7, 7, 8, 0),
        datetime(2026, 7, 8, 8, 0),
        datetime(2026, 7, 9, 8, 0),
    ]


def test_scheduler_anchor_and_advance_with_an_extended_calendar() -> None:
    """The anchor-counts-the-tick / consuming-it-advances contract (already
    pinned for plain days_of_week schedules in test_runner_scheduler.py)
    holds identically for an extended run_calendar: EOMWORK's July 31 2026
    tick (a Friday, the last workday) counts at the anchor; consuming it
    advances to August's, the 31st (a Monday, August's last workday)."""
    text = (
        "extended_calendar: eow\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n\n"
        "insert_job: ext_incl\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: eow\nstart_times: "08:00"\n'
    )
    catalog = lower_source(text)
    tick = datetime(2026, 7, 31, 8, 0)
    sched = Scheduler(catalog, start=tick)
    assert sched.next_occurrence() == tick
    assert [event.at for event in sched.pop_due(tick)] == [tick]
    assert sched.next_occurrence() == datetime(2026, 8, 31, 8, 0)


def test_scheduler_cycle_bound_extended_run_calendar_goes_dormant_mid_run() -> None:
    """A cycle-bound extended run_calendar (CWRK#L over two periods ending
    Apr 2 and Jul 2 2026) fires exactly twice and then goes dormant --
    popped mid-run (only the first tick due yet), the job is still pending
    its second; once that fires too, pop_due drops it silently, never an
    error (DL-56/57 dormancy, extended-calendar flavor)."""
    text = (
        "cycle: cyc\nstart_date: 03/28/2026\nend_date: 04/02/2026\n"
        "start_date: 06/27/2026\nend_date: 07/02/2026\n\n"
        "extended_calendar: cwrk_last\nworkday: mo,tu,we,th,fr\ncyccal: cyc\ncondition: CWRK#L\n\n"
        "insert_job: cyc_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: cwrk_last\nstart_times: "08:00"\n'
    )
    sched = Scheduler(lower_source(text), start=datetime(2026, 1, 1, 0, 0))
    assert sched.next_occurrence() == datetime(2026, 4, 2, 8, 0)
    first = sched.pop_due(datetime(2026, 5, 1, 0, 0))
    assert [e.at for e in first] == [datetime(2026, 4, 2, 8, 0)]
    assert sched.next_occurrence() == datetime(2026, 7, 2, 8, 0)  # still pending mid-run
    second = sched.pop_due(datetime(2027, 1, 1, 0, 0))
    assert [e.at for e in second] == [datetime(2026, 7, 2, 8, 0)]
    assert sched.next_occurrence() is None  # dormant: dropped, never an error
    assert sched.pop_due(datetime(2028, 1, 1, 0, 0)) == []


# =====================================================================
# 4. Preflight branches specific to the autocal generator probe
# =====================================================================


def test_preflight_extended_run_dormant_after_cycle_bound_with_start_anchor() -> None:
    """(ss8 DL-57): a cycle-bound extended run_calendar's generator probe
    (`_next_eligible_day`) is anchored at `start`; passing an anchor PAST
    the cycle's bound (the periods end in 2026, long before 2028) makes the
    probe return None -- WARN, not silence and not a crash."""
    text = (
        "cycle: cyc\nstart_date: 03/28/2026\nend_date: 04/02/2026\n"
        "start_date: 06/27/2026\nend_date: 07/02/2026\n\n"
        "extended_calendar: cwrk_last\nworkday: mo,tu,we,th,fr\ncyccal: cyc\ncondition: CWRK#L\n\n"
        "insert_job: cyc_job\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: cwrk_last\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text), start=datetime(2028, 1, 1, 0, 0))
    warns = [
        i for i in items if i.code == "calendar" and i.severity == "WARN" and i.job == "cyc_job"
    ]
    assert warns and "dormant" in warns[0].message
    assert not any(i.severity == "ERROR" and i.code == "calendar" for i in items)


def test_preflight_extended_exclude_fully_covering_standard_run_set_warns_dormant() -> None:
    """(ss8 DL-56/57): a standard run_calendar (one Friday row) whose
    exclude_calendar is an EXTENDED WEEKDAYS rule covers it entirely -- the
    probe (run: a frozenset; exclude: a CompiledCalendar) finds no
    surviving day and WARNs dormant. This is a DIFFERENT code path than the
    standard-vs-standard 'no eligible dates' WARN (that one never touches
    the generator probe, since neither side is a CompiledCalendar there)."""
    text = (
        "calendar: runs\n07/03/2026 00:00\n\n"
        "extended_calendar: allweek\nworkday: mo,tu,we,th,fr\ncondition: WEEKDAYS\n\n"
        "insert_job: excl_job\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: runs\nexclude_calendar: allweek\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text), start=datetime(2026, 7, 1, 0, 0))
    warns = [
        i for i in items if i.code == "calendar" and i.severity == "WARN" and i.job == "excl_job"
    ]
    assert warns and "dormant" in warns[0].message


def test_preflight_generation_error_surfaces_as_error_not_a_crash() -> None:
    """(ss8): a calendar that COMPILES cleanly but fails at generation --
    holiday W's holiday-free walk exhausts its year cap because every
    Monday (the only workday) is itself a holiday -- must surface through
    preflight's generator probe as an ERROR item, never propagate as an
    uncaught exception out of preflight(). (Pre-DL-58 this path was
    exercised by the Q8a disagreement gate, deleted with the holiday-wins
    resolution.)"""
    mondays = [date(2026, 7, 6) + timedelta(days=7 * i) for i in range(60)]
    rows = "\n".join(f"{d:%m/%d/%Y}" for d in [date(2026, 7, 4), *mondays])
    text = (
        f"calendar: hols\n{rows}\n\n"
        "extended_calendar: walkcap\nworkday: mo\nholiday: W\n"
        "holcal: hols\ncondition: jul#4\n\n"
        "insert_job: walkcap_job\njob_type: c\ncommand: x\nmachine: localhost\n"
        'date_conditions: 1\nrun_calendar: walkcap\nstart_times: "08:00"\n'
    )
    items = preflight(lower_source(text), start=datetime(2026, 6, 1, 0, 0))
    errors = [
        i
        for i in items
        if i.code == "calendar" and i.severity == "ERROR" and i.job == "walkcap_job"
    ]
    assert errors and "no valid day" in errors[0].message


# =====================================================================
# 5. ir.py lowering lanes
# =====================================================================


def test_condition_lines_interleaved_with_other_attrs_preserve_order() -> None:
    """SEM-36: `condition:` is the one repeatable calendar key; interleaving
    it with other (unique) attrs must not perturb either lane -- conditions
    stay in source order, and every other attr lands regardless of where
    its line sat relative to the condition lines."""
    text = (
        "extended_calendar: eom\n"
        "workday: mo,tu,we,th,fr\n"
        "condition: EOMWORK\n"
        "non_workday: W\n"
        "condition: FOMWORK\n"
        "adjust: 0\n"
    )
    cal = lower_source(text).calendars["eom"]
    assert cal.conditions == ["EOMWORK", "FOMWORK"]
    assert cal.attrs == {"workday": "mo,tu,we,th,fr", "non_workday": "W", "adjust": "0"}


def test_duplicate_non_condition_attr_still_errors_when_interleaved_with_conditions() -> None:
    """A `condition:` line sitting between two `workday:` lines must not
    hide the duplicate -- lowering's per-key duplicate check is independent
    of the repeatable condition lane."""
    text = "extended_calendar: dup\nworkday: mo,tu,we,th,fr\ncondition: EOM\nworkday: mo,tu\n"
    with pytest.raises(LoweringError, match="duplicate attribute"):
        lower_source(text)


def test_cycle_end_date_before_any_start_date_errors() -> None:
    """An `end_date:` with no preceding open `start_date:` cannot form a
    period -- lowering refuses rather than silently dropping the row."""
    with pytest.raises(LoweringError, match="end_date without a preceding start_date"):
        lower_source("cycle: q\nend_date: 04/02/2026\n")


def test_cycle_dangling_start_date_at_end_of_statement_errors() -> None:
    """A trailing `start_date:` with no closing `end_date:` leaves an open
    period -- refused, never silently discarded."""
    with pytest.raises(LoweringError, match="start_date without a closing end_date"):
        lower_source("cycle: q\nstart_date: 03/28/2026\n")


def test_cycle_dangling_start_date_mid_statement_errors() -> None:
    """A SECOND `start_date:` opened before the first one closes hits the
    same dangling-start error, mid-statement rather than at the trailing-
    end check -- a different line in `_lower_cycle`, same message."""
    text = "cycle: q\nstart_date: 03/28/2026\nstart_date: 04/01/2026\nend_date: 04/05/2026\n"
    with pytest.raises(LoweringError, match="start_date without a closing end_date"):
        lower_source(text)


def test_ext_calendar_and_extended_calendar_spellings_lower_and_compile_identically() -> None:
    """SEM-36/Q9: `ext_calendar:` (the Manage Calendars syntax block's
    spelling) and `extended_calendar:` (the autocal_asc command page, our
    corpus's usual form) name the same record kind; both must lower to
    kind='extended' and generate the identical day set through the autocal
    interpreter."""
    a = lower_source("ext_calendar: e1\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n")
    b = lower_source("extended_calendar: e2\nworkday: mo,tu,we,th,fr\ncondition: EOMWORK\n")
    cal_a, cal_b = a.calendars["e1"], b.calendars["e2"]
    assert cal_a.kind == cal_b.kind == "extended"
    assert cal_a.conditions == cal_b.conditions == ["EOMWORK"]
    days_a = compile_calendar(cal_a, a).days_between(*JUL26)
    days_b = compile_calendar(cal_b, b).days_between(*JUL26)
    assert days_a == days_b == {date(2026, 7, 31)}


def test_q9_observed_export_shape_loads_end_to_end() -> None:
    """Q9 (DL-60): a synthetic clone of the observed autocal_asc
    export -- extended_calendar: spelling, fixed attribute order with
    empty-valued keys emitted, workday `all`, holiday S without holcal,
    braced conditions, the WORKD#L ordinal, HH:MM:SS standard rows, and
    repeated cycle pairs -- survives the scanner byte-identically (F1)
    and loads through lowering, the interpreter, and standard_rows()."""
    from dsl41.ast_jil import parse, render
    from dsl41.autocal import standard_rows

    text = (
        "calendar: dsl41_q9_hols\n"
        "description:\n"
        "01/01/2027 00:00:00\n"
        "12/25/2027 00:00:00\n"
        "\n"
        "extended_calendar: dsl41_q9_first_bday\n"
        "description:\n"
        "workday: mo,tu,we,th,fr\n"
        "non_workday: N\n"
        "holiday:\n"
        "holcal:\n"
        "cyccal:\n"
        "adjust: 0\n"
        "condition: {MNTHD#1}\n"
        "\n"
        "extended_calendar: dsl41_q9_feb_jul_first\n"
        "description:\n"
        "workday: all\n"
        "non_workday:\n"
        "holiday: S\n"
        "holcal:\n"
        "cyccal:\n"
        "adjust: 0\n"
        "condition: {feb | jul}&workd#1\n"
        "\n"
        "extended_calendar: dsl41_q9_last_bday\n"
        "description:\n"
        "workday: mo,tu,we,th,fr\n"
        "non_workday:\n"
        "holiday:\n"
        "holcal:\n"
        "cyccal:\n"
        "adjust: 0\n"
        "condition: WORKD#L\n"
        "\n"
        "cycle: dsl41_q9_cyc\n"
        "description: dsl41_q9_cyc\n"
        "start_date: 01/01/2027\n"
        "end_date: 06/30/2027\n"
        "start_date: 07/01/2027\n"
        "end_date: 12/31/2027\n"
    )
    assert render(parse(text)) == text  # F1 on the export shape
    catalog = lower_source(text)
    assert standard_rows(catalog.calendars["dsl41_q9_hols"]) == {
        date(2027, 1, 1): frozenset({(0, 0)}),
        date(2027, 12, 25): frozenset({(0, 0)}),
    }
    assert catalog.cycles["dsl41_q9_cyc"].periods == [
        ("01/01/2027", "06/30/2027"),
        ("07/01/2027", "12/31/2027"),
    ]
    first = compile_calendar(catalog.calendars["dsl41_q9_first_bday"], catalog)
    # Aug 1 2027 is a Sunday: non_workday N walks to Mon Aug 2
    assert first.days_between(date(2027, 8, 1), date(2027, 8, 31)) == frozenset({date(2027, 8, 2)})
    feb_jul = compile_calendar(catalog.calendars["dsl41_q9_feb_jul_first"], catalog)
    assert feb_jul.days_between(date(2027, 1, 1), date(2027, 12, 31)) == frozenset(
        {date(2027, 2, 1), date(2027, 7, 1)}
    )
    last = compile_calendar(catalog.calendars["dsl41_q9_last_bday"], catalog)
    # Jul 31 2027 is a Saturday: the last workday is Friday the 30th
    assert last.days_between(date(2027, 7, 1), date(2027, 7, 31)) == frozenset({date(2027, 7, 30)})
