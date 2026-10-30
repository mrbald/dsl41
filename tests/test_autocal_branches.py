"""Branch tests for src/dsl41/autocal.py: the refusals of `compile_calendar`
that nothing else reaches.
"""

from __future__ import annotations

from datetime import date

import pytest

from dsl41.autocal import (
    CalendarRuleError,
    classify_row,
    compile_calendar,
)
from dsl41.ir import CalendarIR, CatalogIR, CycleIR


def _ext(name: str = "ext", conditions: list[str] | None = None, **attrs: str) -> CalendarIR:
    return CalendarIR(name=name, kind="extended", attrs=attrs, conditions=conditions or [])


def _std(name: str, *rows: str) -> CalendarIR:
    return CalendarIR(name=name, kind="standard", dates=list(rows))


def _catalog(*cals: CalendarIR, cycles: dict[str, CycleIR] | None = None) -> CatalogIR:
    return CatalogIR(jobs={}, calendars={c.name: c for c in cals}, cycles=cycles or {})


def _compile(cal: CalendarIR, *others: CalendarIR, cycles: dict[str, CycleIR] | None = None):
    return compile_calendar(cal, _catalog(cal, *others, cycles=cycles))


# ------------------------------------------------------------------- rows


def test_a_row_that_fits_no_time_form_classifies_as_none() -> None:
    assert classify_row("01/02/2026") == "date"
    assert classify_row("01/02/2026 03:04") == "hh:mm"
    assert classify_row("01/02/2026 03:04:05") == "hh:mm:ss"
    assert classify_row("01/02/2026 03:04:05:06") is None
    assert classify_row("01/02/2026 03:04 05") is None


# ------------------------------------------------------------- compilation


def test_compile_refuses_a_standard_calendar_and_names_the_other_door() -> None:
    with pytest.raises(CalendarRuleError, match="is standard; use standard_days"):
        _compile(_std("s", "01/02/2026"))


def test_a_rule_with_input_left_after_its_expression_is_trailing() -> None:
    with pytest.raises(CalendarRuleError, match="trailing input"):
        _compile(_ext(conditions=["WORKDAYS )"]))
    assert _compile(_ext(conditions=["(WORKDAYS)"])) is not None


def test_workday_list_accepts_three_letter_names_like_two_letter_codes() -> None:
    long_form = _compile(_ext(workday="mon,tue", conditions=["WORKDAYS"]))
    short_form = _compile(_ext(workday="mo,tu", conditions=["WORKDAYS"]))
    window = (date(2026, 7, 1), date(2026, 7, 31))
    assert long_form.days_between(*window) == short_form.days_between(*window)
    assert all(d.weekday() in (0, 1) for d in long_form.days_between(*window))


def test_workday_list_refuses_a_day_it_does_not_know() -> None:
    with pytest.raises(CalendarRuleError, match="workday: unrecognized day 'xx'"):
        _compile(_ext(workday="mo,xx", conditions=["WORKDAYS"]))


def test_an_action_outside_osnwp_is_refused_by_key() -> None:
    with pytest.raises(CalendarRuleError, match="non_workday: expected one of O/S/N/W/P, got 'x'"):
        _compile(_ext(non_workday="x"))


def test_a_cyccal_that_names_no_cycle_is_refused() -> None:
    with pytest.raises(CalendarRuleError, match="names no cycle in the loaded set"):
        _compile(_ext(cyccal="nosuch", conditions=["CYCLE"]))


@pytest.mark.parametrize(
    ("periods", "message"),
    [
        ([("13/45/2026", "14/01/2026")], "unparseable period"),
        ([("07/10/2026", "07/01/2026")], "period ends before it starts"),
        ([], "has no periods"),
    ],
)
def test_a_cycle_whose_periods_cannot_be_used_is_refused(
    periods: list[tuple[str, str]], message: str
) -> None:
    cycles = {"cyc": CycleIR(name="cyc", periods=periods)}
    with pytest.raises(CalendarRuleError, match=message):
        _compile(_ext(cyccal="cyc", conditions=["CYCLE"]), cycles=cycles)
    good = {"cyc": CycleIR(name="cyc", periods=[("07/01/2026", "07/10/2026")])}
    assert _compile(_ext(cyccal="cyc", conditions=["CYCLE"]), cycles=good) is not None


def test_a_holcal_that_names_no_calendar_is_refused() -> None:
    with pytest.raises(CalendarRuleError, match="holcal 'nosuch' names no calendar"):
        _compile(_ext(holcal="nosuch", conditions=["DAILY"]))


def test_adjust_must_be_an_integer() -> None:
    with pytest.raises(CalendarRuleError, match="adjust: expected an integer, got 'two'"):
        _compile(_ext(adjust="two", conditions=["DAILY"]))
    assert _compile(_ext(adjust="2", conditions=["DAILY"])) is not None
