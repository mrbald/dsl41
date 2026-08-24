"""autocal interpreter tests (DL-57): the SEM-36..39 doc-freeze pinned.

Every worked example the vendor docs contain is a test here; the Q8
defaults and refusals each get one. Naming: test_sem3x_* pins a SEM
entry's documented behavior, test_q8x_* a pinned default or refusal.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.autocal import CalendarRuleError, compile_calendar, standard_days, standard_rows
from dsl41.ir import CalendarIR, CatalogIR, CycleIR, lower_source

# ------------------------------------------------------------ builders


def _catalog(**cals: CalendarIR) -> CatalogIR:
    return CatalogIR(jobs={}, calendars=dict(cals))


def _ext(name: str = "ext", conditions: list[str] | None = None, **attrs: str) -> CalendarIR:
    return CalendarIR(name=name, kind="extended", attrs=attrs, conditions=conditions or [])


def _std(name: str, *rows: str) -> CalendarIR:
    return CalendarIR(name=name, kind="standard", dates=list(rows))


def _days(cal: CalendarIR, lo: date, hi: date, **extra: CalendarIR) -> set[date]:
    catalog = _catalog(**{cal.name: cal, **extra})
    return set(compile_calendar(cal, catalog).days_between(lo, hi))


JUL26 = (date(2026, 7, 1), date(2026, 7, 31))


# ------------------------------------------------- SEM-36: record model


def test_sem36_workday_serializations_agree() -> None:
    """Positional {X|.}x7 (Monday-first) and the comma two-letter list are
    the same mask."""
    positional = _ext(workday="xxxxx..", conditions=["WORKDAYS"])
    commas = _ext(workday="mo,tu,we,th,fr", conditions=["WORKDAYS"])
    lo, hi = JUL26
    assert _days(positional, lo, hi) == _days(commas, lo, hi)


def test_sem36_workday_defaults_to_monday_friday() -> None:
    days = _days(_ext(conditions=["WORKDAYS"]), *JUL26)
    assert date(2026, 7, 3) in days  # Friday
    assert date(2026, 7, 4) not in days  # Saturday


def test_sem36_holiday_action_requires_holcal() -> None:
    with pytest.raises(CalendarRuleError, match="requires holcal"):
        compile_calendar(_ext(holiday="W"), _catalog(ext=_ext(holiday="W")))


def test_sem36_holcal_must_be_standard() -> None:
    other = _ext(name="rules", conditions=["EOM"])
    cal = _ext(holiday="W", holcal="rules")
    with pytest.raises(CalendarRuleError, match="only.*standard"):
        compile_calendar(cal, _catalog(ext=cal, rules=other))


def test_sem36_unknown_attribute_refuses() -> None:
    cal = _ext(zone="UTC")
    with pytest.raises(CalendarRuleError, match="unrecognized attribute"):
        compile_calendar(cal, _catalog(ext=cal))


def test_sem36_empty_values_mean_absent() -> None:
    """The vendor's own example exports empty `holiday:`/`holcal:` values."""
    cal = _ext(holiday="", holcal="", adjust="0", conditions=["EOM"])
    assert _days(cal, *JUL26) == {date(2026, 7, 31)}


# ------------------------------------------- SEM-37: keyword grammar


def test_sem37_daily_default_when_no_conditions() -> None:
    days = _days(_ext(), date(2026, 7, 1), date(2026, 7, 7))
    assert len(days) == 7


def test_sem37_eomwork_last_workday_of_month() -> None:
    # July 31 2026 is a Friday; August's last workday is Monday the 31st
    assert _days(_ext(conditions=["EOMWORK"]), *JUL26) == {date(2026, 7, 31)}
    assert _days(_ext(conditions=["EOMWORK"]), date(2026, 8, 1), date(2026, 8, 31)) == {
        date(2026, 8, 31)
    }


def test_sem37_mnthd_forward_and_backward() -> None:
    assert _days(_ext(conditions=["MNTHD#15"]), *JUL26) == {date(2026, 7, 15)}
    # M-convention: 01 = last (the WEEKMnn wording, applied family-wide)
    assert _days(_ext(conditions=["MNTHDM01"]), *JUL26) == {date(2026, 7, 31)}
    lo, hi = JUL26
    assert _days(_ext(conditions=["MNTHDM01"]), lo, hi) == _days(_ext(conditions=["EOM"]), lo, hi)


def test_sem37_named_month_forms() -> None:
    whole = _days(_ext(conditions=["jul"]), date(2026, 1, 1), date(2026, 12, 31))
    assert len(whole) == 31 and all(d.month == 7 for d in whole)
    assert _days(_ext(conditions=["jul#4"]), date(2026, 1, 1), date(2026, 12, 31)) == {
        date(2026, 7, 4)
    }


def test_sem37_weekday_ordinals() -> None:
    # first Wednesday of July 2026 is the 1st; third Monday the 20th
    assert _days(_ext(conditions=["wed#1"]), *JUL26) == {date(2026, 7, 1)}
    assert _days(_ext(conditions=["mon#3"]), *JUL26) == {date(2026, 7, 20)}
    # backward: friM1 = last Friday (the federal-holidays example's idiom)
    assert _days(_ext(conditions=["friM1"]), *JUL26) == {date(2026, 7, 31)}


def test_sem37_weekdays_auto_subtracts_holcal() -> None:
    """[V] 'The utility automatically excludes all dates that are listed in
    the calendar that you specify in the holiday calendar field.'"""
    hol = _std("hols", "07/03/2026 00:00")
    cal = _ext(holcal="hols", conditions=["WEEKDAYS"])
    days = _days(cal, *JUL26, hols=hol)
    assert date(2026, 7, 3) not in days  # Friday, but a holiday
    assert date(2026, 7, 2) in days


def test_sem37_week_anchoring_jan1_and_wekr_override() -> None:
    """[V] weeks begin on Jan 1's weekday; WEKRddd re-anchors (the doc's
    2014 example: Jan 1 2014 is a Wednesday)."""
    # WEEKD#1 under the default anchor: every Wednesday of 2014
    default = _days(_ext(conditions=["WEEKD#1"]), date(2014, 1, 6), date(2014, 1, 12))
    assert default == {date(2014, 1, 8)}  # the Wednesday
    monday = _days(_ext(conditions=["WEKRMon#01"]), date(2014, 1, 6), date(2014, 1, 12))
    assert monday == {date(2014, 1, 6)}  # the Monday


def test_sem37_case_insensitive_and_word_operators() -> None:
    lo, hi = JUL26
    upper = _days(_ext(conditions=["EOMWORK"]), lo, hi)
    lower = _days(_ext(conditions=["eomwork"]), lo, hi)
    assert upper == lower
    symbolic = _days(_ext(conditions=["jul & (wed#1 | fri)"]), lo, hi)
    words = _days(_ext(conditions=["jul AND (wed#1 OR fri)"]), lo, hi)
    assert symbolic == words  # PENDING: Q8d(iii)


def test_sem37_defective_tokens_refuse() -> None:
    for token in ("WORKDX02", "CWEK#1", "CWEK#L", "CWEKM1", "CWEKX1"):
        cal = _ext(conditions=[token], cyccal="cyc")
        with pytest.raises(CalendarRuleError, match="doc-defective"):
            compile_calendar(cal, _catalog(ext=cal))


def test_sem37_folk_tokens_do_not_exist() -> None:
    for token in ("DAY#5", "MONTH#L", "YEAR#1", "CYCLE#L"):
        cal = _ext(conditions=[token])
        with pytest.raises(CalendarRuleError, match="unknown date-condition token"):
            compile_calendar(cal, _catalog(ext=cal))


# ---------------------------------------- SEM-38: dispositions + adjust


def _dec_catalog(action: str, workday: str = "mo,tu,we,th,fr") -> tuple[CalendarIR, CalendarIR]:
    hol = _std("hols", "12/25/2026 00:00", "12/26/2026 00:00")
    cal = _ext(workday=workday, holiday=action, holcal="hols", conditions=["dec#25"])
    return cal, hol


def test_sem38_holiday_n_is_one_shot() -> None:
    """[V] Dec 25 -> Dec 26 'even if December 26th is a holiday'."""
    cal, hol = _dec_catalog("N")
    days = _days(cal, date(2026, 12, 1), date(2026, 12, 31), hols=hol)
    assert days == {date(2026, 12, 26)}  # still a holiday AND a Saturday


def test_sem38_holiday_w_iterates_to_nonholiday_workday() -> None:
    """[V] the doc's walk: Dec 25 (hol) -> 26 (hol) -> 27 (non-workday) ->
    lands Dec 28. In 2026: 26 Sat-holiday, 27 Sunday, 28 Monday."""
    cal, hol = _dec_catalog("W")
    days = _days(cal, date(2026, 12, 1), date(2026, 12, 31), hols=hol)
    assert days == {date(2026, 12, 28)}


def test_sem38_holiday_p_iterates_backward() -> None:
    """[V] mirror walk to the previous non-holiday workday: with We/Th
    marked non-workdays, Dec 25 walks 24, 23 down to Tuesday Dec 22."""
    cal, hol = _dec_catalog("P", workday="mo,tu,fr")
    days = _days(cal, date(2026, 12, 1), date(2026, 12, 31), hols=hol)
    assert days == {date(2026, 12, 22)}


def test_sem38_holiday_o_keeps_only_holidays() -> None:
    hol = _std("hols", "07/03/2026 00:00")
    cal = _ext(holiday="O", holcal="hols", conditions=["jul"])
    assert _days(cal, *JUL26, hols=hol) == {date(2026, 7, 3)}


def test_sem38_non_workday_w_walks_to_workday() -> None:
    # July 4 2026 is a Saturday; the next workday is Monday the 6th
    cal = _ext(non_workday="W", conditions=["jul#4"])
    assert _days(cal, *JUL26) == {date(2026, 7, 6)}  # PENDING: Q8c


def test_sem38_adjust_is_uniform_and_blind() -> None:
    """[V] the WED#1 -1 example (nothing excluded, still shifted) and the
    14/15/16 passage (no landing re-validation)."""
    cal = _ext(adjust="-1", conditions=["wed#1"])
    assert _days(cal, date(2026, 6, 15), date(2026, 7, 31)) >= {date(2026, 6, 30)}
    blind = _ext(adjust="3", conditions=["MNTHD#15"])
    assert _days(blind, *JUL26) == {date(2026, 7, 18)}  # a Saturday; stays


def test_sem38_adjust_composes_with_s_the_vendor_worked_example() -> None:
    """[V] KB 280764 (Q8b's documented side, DL-58): WORKD#1 + adjust 1 +
    non_workday S = "the day after the first workday of the month", kept
    even when it lands on a Saturday ("if the first workday is Friday,
    [the] job runs Saturday"). Our filter-then-replace-then-shift pipeline
    reproduces the vendor's vector as-is; nonzero adjust with an N/W/P
    REPLACEMENT runs the same pipeline order as a pinned default (Q8b,
    DL-59)."""
    cal = _ext(non_workday="S", adjust="1", conditions=["workd#1"])
    days = _days(cal, date(2026, 5, 1), date(2026, 6, 30))
    # May 2026: first workday Fri May 1 -> Sat May 2 stays under S;
    # June 2026: Mon Jun 1 -> Tue Jun 2
    assert days == {date(2026, 5, 2), date(2026, 6, 2)}


def test_sem38_adjust_range_enforced() -> None:
    cal = _ext(adjust="10", conditions=["EOM"])
    with pytest.raises(CalendarRuleError, match="-9..\\+9"):
        compile_calendar(cal, _catalog(ext=cal))


def test_q8a_holiday_action_governs_dual_classified_dates() -> None:
    """Q8a RESOLVED (DL-58): a holiday falling on a non-workday under TWO
    disagreeing replacement codes takes the HOLIDAY action -- '[the
    utility] applies that action to all of the dates listed in the
    [holiday] calendar'. Sat Jul 4 under holiday N goes to Sun Jul 5
    (one-shot, even onto a non-workday); non_workday W (which would say
    Mon Jul 6) never sees it. The pre-DL-58 compile-time disagreement
    refusal is gone."""
    hol = _std("hols", "07/04/2026 00:00")  # a Saturday
    cal = _ext(holiday="N", non_workday="W", holcal="hols", conditions=["jul#4"])
    assert _days(cal, *JUL26, hols=hol) == {date(2026, 7, 5)}


def test_q8a_agreeing_replacements_proceed() -> None:
    """Same-code W/W on a holiday Saturday: the holiday action governs
    (Q8a, DL-58) and walks to the next non-holiday workday."""
    hol = _std("hols", "07/04/2026 00:00")
    cal = _ext(holiday="W", non_workday="W", holcal="hols", conditions=["jul#4"])
    assert _days(cal, *JUL26, hols=hol) == {date(2026, 7, 6)}


def test_q8a_holiday_filter_shields_holcal_dates_from_non_workday_action() -> None:
    """Q8a (DL-58): with a holiday action SPECIFIED, holcal dates never
    receive non-workday treatment -- holiday O keeps the Saturday holiday
    in place; non_workday W moves only non-holcal non-workdays. (The
    pre-DL-58 pipeline let W drag the Saturday holiday to Monday.)"""
    hol = _std("hols", "07/03/2026 00:00", "07/04/2026 00:00")  # Fri + Sat
    cal = _ext(holiday="O", non_workday="W", holcal="hols", conditions=["jul"])
    days = _days(cal, *JUL26, hols=hol)
    assert days == {date(2026, 7, 3), date(2026, 7, 4)}  # both holidays stay put


def test_sem38_non_workday_n_walks_to_a_workday() -> None:
    """[V] 'N -- Specifies to include the next workday that also meets all
    other criteria' -- NOT a blind +1 day (that is holiday-N's wording).
    Saturdays under non_workday N land on Mondays (PENDING: Q8c pin: next
    non-holiday workday; date-conditions not re-checked)."""
    cal = _ext(non_workday="N", conditions=["sat"])
    days = _days(cal, date(2026, 1, 1), date(2026, 1, 31))
    assert days == {date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)}
    assert all(d.weekday() == 0 for d in days)  # Mondays, never Sundays


def test_q8b_nonzero_adjust_with_replacement_action_pinned_replace_then_shift() -> None:
    """PENDING Q8b, pinned default (DL-59 -- deterministic over the old
    fail-closed refusal): the SEM-38 pipeline order as-is -- disposition
    replaces first, then the uniform blind adjust shifts every survivor.
    The two probes are the runbook's discriminator pair; (Aug 15, Aug 18)
    is the replace-then-shift signature. Aug 14 2026 is a Friday (workday,
    W no-op, +1 lands Saturday and STAYS -- adjust is blind); Aug 15 is a
    Saturday (W walks to Mon 17, +1 = Tue 18)."""
    one = _ext(adjust="1", non_workday="W", conditions=["MNTHD#14"])
    assert _days(one, date(2026, 8, 1), date(2026, 8, 31)) == {date(2026, 8, 15)}
    two = _ext(adjust="1", non_workday="W", conditions=["MNTHD#15"])
    assert _days(two, date(2026, 8, 1), date(2026, 8, 31)) == {date(2026, 8, 18)}
    # adjust: 0 alongside an action is the vendor's own example -- inert
    zero = _ext(adjust="0", non_workday="W", conditions=["EOMWORK"])
    assert _days(zero, *JUL26) == {date(2026, 7, 31)}


def test_q8_walk_with_no_workdays_refuses_at_compile() -> None:
    cal = _ext(workday=".......", non_workday="W", conditions=["EOM"])
    with pytest.raises(CalendarRuleError, match="nowhere to walk"):
        compile_calendar(cal, _catalog(ext=cal))


# --------------------------------------------------- SEM-39: cycles


def _cycle_catalog(*conditions: str) -> tuple[CalendarIR, CatalogIR]:
    cyc = CycleIR(
        name="cyc",
        periods=[("03/28/2026", "04/02/2026"), ("06/27/2026", "07/02/2026")],
    )
    cal = _ext(cyccal="cyc", conditions=list(conditions))
    catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"cyc": cyc})
    return cal, catalog


def test_sem39_cycle_union_membership() -> None:
    cal, catalog = _cycle_catalog("CYCLE")
    days = set(compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 12, 31)))
    assert len(days) == 12  # two 6-day periods
    assert date(2026, 3, 28) in days and date(2026, 7, 2) in days
    assert date(2026, 5, 1) not in days


def test_sem39_cycl_indexes_each_period() -> None:
    cal, catalog = _cycle_catalog("CYCL#001")
    days = set(compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 12, 31)))
    assert days == {date(2026, 3, 28), date(2026, 6, 27)}


def test_sem39_cwrk_last_workday_per_period() -> None:
    cal, catalog = _cycle_catalog("CWRK#L")
    days = set(compile_calendar(cal, catalog).days_between(date(2026, 1, 1), date(2026, 12, 31)))
    # period 1 ends Thu Apr 2; period 2 ends Thu Jul 2
    assert days == {date(2026, 4, 2), date(2026, 7, 2)}


def test_sem39_cddd_ordinal_with_padded_digits() -> None:
    """The doc's own example: Ctue#02 = second Tuesday of each period."""
    cyc = CycleIR(name="cyc", periods=[("07/01/2026", "07/31/2026")])
    cal = _ext(cyccal="cyc", conditions=["Ctue#02"])
    catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"cyc": cyc})
    days = set(compile_calendar(cal, catalog).days_between(*JUL26))
    assert days == {date(2026, 7, 14)}


def test_sem39_cycle_conditions_require_cyccal() -> None:
    cal = _ext(conditions=["CYCLE"])
    with pytest.raises(CalendarRuleError, match="require cyccal"):
        compile_calendar(cal, _catalog(ext=cal))


def test_sem39_cycle_bound_dormancy() -> None:
    cal, catalog = _cycle_catalog("CWRK#L")
    compiled = compile_calendar(cal, catalog)
    assert compiled.bound is not None
    assert compiled.first_on_or_after(date(2026, 1, 1)) == date(2026, 4, 2)
    assert compiled.first_on_or_after(date(2026, 8, 1)) is None  # exhausted


def test_q8e_week_of_period_chunks() -> None:
    """Default: 7-day chunks from each period's first day."""
    cyc = CycleIR(name="cyc", periods=[("07/06/2026", "07/19/2026")])  # 14 days
    cal = _ext(cyccal="cyc", conditions=["CWEEK#2"])
    catalog = CatalogIR(jobs={}, calendars={"ext": cal}, cycles={"cyc": cyc})
    days = set(compile_calendar(cal, catalog).days_between(*JUL26))
    assert days == {date(2026, 7, 13) + timedelta(days=i) for i in range(7)}


# --------------------------------------------- Q8d: rule combination


def test_q8d_comma_and_repeated_lines_union() -> None:
    lo, hi = JUL26
    one_line = _days(_ext(conditions=["EOMWORK,FOMWORK"]), lo, hi)
    two_lines = _days(_ext(conditions=["EOMWORK", "FOMWORK"]), lo, hi)
    assert one_line == two_lines == {date(2026, 7, 1), date(2026, 7, 31)}


def test_q8d_exclusive_rules_subtract_from_the_union() -> None:
    lo, hi = JUL26
    subtracted = _days(_ext(conditions=["WEEKDAYS", "XEOM"]), lo, hi)
    inline = _days(_ext(conditions=["WEEKDAYS & XEOM"]), lo, hi)
    assert date(2026, 7, 31) not in subtracted
    assert subtracted == inline  # one rule vs rule-list forms agree here


def test_q8d_not_prefixes_and_double_negation() -> None:
    lo, hi = JUL26
    assert _days(_ext(conditions=["WEEKDAYS", "NOT eom"]), lo, hi) == _days(
        _ext(conditions=["WEEKDAYS", "XEOM"]), lo, hi
    )
    assert _days(_ext(conditions=["NOT XEOM"]), lo, hi) == _days(_ext(conditions=["EOM"]), lo, hi)


def test_q8d_exclusion_only_rules_subtract_from_daily() -> None:
    days = _days(_ext(conditions=["XEOM"]), *JUL26)
    assert len(days) == 30 and date(2026, 7, 31) not in days


def test_q8d_flat_left_to_right_precedence() -> None:
    """`a | b & c` reads ((a|b)&c), the SEM-03 house style -- distinct from
    C precedence (a|(b&c))."""
    lo, hi = JUL26
    flat = _days(_ext(conditions=["eom | fom & mon"]), lo, hi)
    grouped = _days(_ext(conditions=["(eom | fom) & mon"]), lo, hi)
    assert flat == grouped == set()  # neither edge of July 2026 is a Monday
    c_style = _days(_ext(conditions=["eom | (fom & mon)"]), lo, hi)
    assert c_style == {date(2026, 7, 31)}


def test_parse_errors_are_loud() -> None:
    for rule in ("eom &", "(eom", "eom mon", "eom @ fom"):
        cal = _ext(conditions=[rule])
        with pytest.raises(CalendarRuleError):
            compile_calendar(cal, _catalog(ext=cal))


def test_q8d_all_exclusive_compound_rules_pinned_literal_include() -> None:
    """PENDING Q8d, pinned default (DL-59 -- deterministic over the old
    fail-closed refusal): a compound rule with no inclusive leaf evaluates
    LITERALLY as an include, the same complement algebra mixed rules use --
    `xtue|xwed` (not-Tue or not-Wed) covers every day, `xtue&xwed` every
    day except Tuesdays and Wednesdays. Near-universal is accepted as the
    honest boolean reading, not silently inverted into an exclusion."""
    every = _days(_ext(conditions=["xtue|xwed"]), *JUL26)
    assert len(every) == 31  # all of July 2026
    no_tue_wed = _days(_ext(conditions=["xtue&xwed"]), *JUL26)
    assert {d.weekday() for d in every - no_tue_wed} == {1, 2}
    # mixed-polarity expressions keep working (the complement-in-AND idiom)
    assert _days(_ext(conditions=["mon & xtue"]), *JUL26) == _days(_ext(conditions=["mon"]), *JUL26)


def test_parser_depth_is_capped() -> None:
    rule = "(" * 150 + "mon" + ")" * 150
    cal = _ext(conditions=[rule])
    with pytest.raises(CalendarRuleError, match="nesting deeper"):
        compile_calendar(cal, _catalog(ext=cal))


# ------------------------------------------------------ properties


@settings(max_examples=25, deadline=None)
@given(
    offset=st.integers(min_value=0, max_value=1200),
    span=st.integers(min_value=1, max_value=90),
)
def test_window_consistency(offset: int, span: int) -> None:
    """days_between is window-stable: a sub-window sees exactly the full
    window's days restricted to it (margin correctness)."""
    hol = _std("hols", "12/25/2026 00:00", "01/01/2027 00:00")
    cal = _ext(non_workday="W", holcal="hols", conditions=["EOMWORK,MNTHD#15"])
    catalog = _catalog(ext=cal, hols=hol)
    compiled = compile_calendar(cal, catalog)
    base = date(2026, 1, 1)
    lo = base + timedelta(days=offset)
    hi = lo + timedelta(days=span)
    wide = compiled.days_between(base, base + timedelta(days=1400))
    narrow = compiled.days_between(lo, hi)
    assert narrow == frozenset(d for d in wide if lo <= d <= hi)


@settings(max_examples=25, deadline=None)
@given(day=st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31)))
def test_first_on_or_after_agrees_with_membership(day: date) -> None:
    cal = _ext(conditions=["FOMWORK"])
    compiled = compile_calendar(cal, _catalog(ext=cal))
    nxt = compiled.first_on_or_after(day)
    assert nxt is not None and nxt >= day
    assert nxt in compiled.days_between(nxt, nxt)
    assert not compiled.days_between(day, nxt - timedelta(days=1))


def test_standard_days_parses_rows_and_refuses_garbage() -> None:
    assert standard_days(_std("s", "01/01/2026 00:00", "12/25/2026")) == {
        date(2026, 1, 1),
        date(2026, 12, 25),
    }
    with pytest.raises(CalendarRuleError, match="unparseable date row"):
        standard_days(_std("s", "not-a-date"))


def test_key_shaped_date_row_tail_refuses_at_consumption() -> None:
    """The scanner carries a malformed date row verbatim (rule 11; DL-160
    keeps it exempt from the continuation guard). The loud refusal lives
    here, at consumption."""
    with pytest.raises(CalendarRuleError, match="unparseable date row"):
        standard_days(_std("s", "01/01/2026 owner: bob"))


# ------------------------------------------- Q9 export format (DL-60)
# Format facts taken from one observed autocal_asc export sample [F]
# (2026-07-30; every name and date below is synthetic): the
# extended_calendar: spelling, fixed attribute order with empty-valued
# keys emitted, workday comma codes plus `all`, braces as condition
# grouping, #L "last" ordinals, HH:MM:SS row tails, and holiday: S
# carried without a holcal.


def test_q9_standard_rows_accept_hhmmss_tails() -> None:
    """The observed sample stamps `00:00:00` (HH:MM:SS) on standard date rows;
    seconds beyond the minute truncate -- ticks are minute-grained."""
    rows = standard_rows(_std("s", "01/01/2027 00:00:00", "07/03/2027 16:30:00"))
    assert rows == {
        date(2027, 1, 1): frozenset({(0, 0)}),
        date(2027, 7, 3): frozenset({(16, 30)}),
    }
    with pytest.raises(CalendarRuleError, match="unparseable date row"):
        standard_rows(_std("s", "01/01/2027 00:00:00:00"))


def test_q9_workday_all_means_every_day() -> None:
    """`workday: all` appears verbatim in the observed sample."""
    every = _days(_ext(workday="all", conditions=["DAILY"]), *JUL26)
    assert len(every) == 31


def test_q9_braces_group_like_parens() -> None:
    """The observed conditions wrap terms in BRACES -- {MNTHD#7} |
    {MNTHD#21} -- where TechDocs shows parens; both group identically."""
    lo, hi = JUL26
    braced = _days(_ext(conditions=["{MNTHD#7} | {MNTHD#21}"]), lo, hi)
    assert braced == {date(2026, 7, 7), date(2026, 7, 21)}
    assert braced == _days(_ext(conditions=["(MNTHD#7) | (MNTHD#21)"]), lo, hi)
    mixed = _days(_ext(workday="all", conditions=["{feb | jul}&workd#1"]), date(2026, 1, 1), date(2026, 12, 31))
    assert mixed == {date(2026, 2, 1), date(2026, 7, 1)}  # workday all: workd#1 = the 1st


def test_q9_hash_l_ordinal_means_last() -> None:
    """WORKD#L appears in the observed sample; #L = from-end-1, uniformly
    across the ordinal families (the CWRK/Cddd families already documented
    the form)."""
    lo, hi = JUL26
    assert _days(_ext(conditions=["WORKD#L"]), lo, hi) == {date(2026, 7, 31)}  # Friday
    assert _days(_ext(conditions=["WORKD#L"]), lo, hi) == _days(_ext(conditions=["WORKDM1"]), lo, hi)
    assert _days(_ext(conditions=["MNTHD#L"]), lo, hi) == {date(2026, 7, 31)}
    assert _days(_ext(conditions=["SAT#L"]), lo, hi) == {date(2026, 7, 25)}  # last Saturday


def test_q9_holiday_s_without_holcal_compiles() -> None:
    """The observed sample carries `holiday: S` with holcal empty -- S is
    a pass-through consuming no holiday set, so only O/N/W/P keep the
    SEM-36 holcal requirement."""
    cal = _ext(workday="all", holiday="S", conditions=["{feb | jul}&workd#1"])
    days = _days(cal, date(2026, 1, 1), date(2026, 12, 31))
    assert days == {date(2026, 2, 1), date(2026, 7, 1)}
    with pytest.raises(CalendarRuleError, match="requires holcal"):
        compile_calendar(_ext(holiday="O"), _catalog(ext=_ext(holiday="O")))


# ------------------------------------------- the live-probe file (runbook ss2)


#: the file an operator imports on a live box. It is a repo artifact, so the
#: suite is what keeps it importable: before this test a two-letter day token
#: or a moved date could sit in it unnoticed until the box refused the import.
PROBE = Path(__file__).resolve().parent.parent / "docs" / "probes" / "dsl41_q8_cals.txt"

AUG26 = (date(2026, 8, 1), date(2026, 8, 31))
DEC26 = (date(2026, 12, 1), date(2026, 12, 31))
#: every day of August 2026, derived from the Gregorian calendar itself
_AUG_DAYS = {date(2026, 8, day) for day in range(1, 32)}


def _aug(*days: int) -> set[date]:
    return {date(2026, 8, day) for day in days}


#: docs/live-instance-runbook.md ss2, "What each August/December 2026
#: observation means": the date set dsl41 is pinned to produce for each probe
#: calendar. The runbook reads the vendor's output against exactly these, so a
#: generator change that moved one would silently move the question too.
PINNED: dict[str, tuple[tuple[date, date], set[date]]] = {
    # Q8b: replace-then-shift is the DL-59 pipeline order -- (15, 18)
    "dsl41_q8b_1": (AUG26, _aug(15)),
    "dsl41_q8b_2": (AUG26, _aug(18)),
    # Q8c_1: Saturdays map to Mondays 3, 10, ?, 24, 31; the `?` is the
    # holiday-free walk, so Mon Aug 17 (a holcal date) is stepped over
    "dsl41_q8c_1": (AUG26, _aug(3, 10, 18, 24, 31)),
    # Q8c_2: holiday N is a verbatim one-shot -- Dec 24 lands on Dec 25 and
    # is not re-processed by the second holiday
    "dsl41_q8c_2": (DEC26, {date(2026, 12, 25)}),
    # Q8d_1: `mon | wed & fri` is flat left-to-right, so no day qualifies
    "dsl41_q8d_1": (AUG26, set()),
    # Q8d_2: order-free union-minus-exclusions -- the later `mon` does not
    # resurrect what `NOT mon` took out
    "dsl41_q8d_2": (AUG26, set()),
    # Q8d_3: an all-exclusive compound is evaluated literally as an include
    "dsl41_q8d_3": (AUG26, _AUG_DAYS),
    # Q8d_4: the OR word unions exactly like `|`
    "dsl41_q8d_4": (AUG26, {day for day in _AUG_DAYS if day.weekday() in (0, 2)}),
    # Q8d_5: an exclusion-only rule list subtracts from the DAILY default
    "dsl41_q8d_5": (AUG26, {day for day in _AUG_DAYS if day.weekday() != 0}),
}


def test_q8_probe_calendars_compile_to_the_runbook_pins() -> None:
    """`docs/probes/dsl41_q8_cals.txt` compiles, and every calendar in it
    produces the date set the runbook pins.

    The probe file only answers Q8b-Q8d if both sides of the diff are real:
    the vendor's export, and OUR date set for the same definition. Nothing
    compiled the file here before, so a refused token in it would have been
    found on the box, and a generator change that moved one of these dates
    would have moved the pinned answer with it."""
    catalog = lower_source(PROBE.read_text(encoding="utf-8"), file=PROBE.name)
    assert set(catalog.calendars) == {"dsl41_hols_aug", "dsl41_hols_dec"} | set(PINNED)
    # the two holiday calendars the extended ones name (SEM-36 holcal)
    assert standard_days(catalog.calendars["dsl41_hols_aug"]) == {date(2026, 8, 17)}  # a Monday
    assert standard_days(catalog.calendars["dsl41_hols_dec"]) == {
        date(2026, 12, 24),
        date(2026, 12, 25),
    }
    for name, (window, expected) in PINNED.items():
        compiled = compile_calendar(catalog.calendars[name], catalog)
        assert set(compiled.days_between(*window)) == expected, name
