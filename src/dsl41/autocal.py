"""Extended-calendar rule interpreter (DL-57).

Pure functions from the opaque CalendarIR/CycleIR carry (DL-36) to day sets, implementing
the SEM-36..39 doc-freeze: candidate generation from the date-condition keyword grammar
(SEM-37), then the disposition pipeline (SEM-38: category filters, N one-shot and W/P
iterating replacements, uniform blind `adjust`; a specified holiday action governs holcal
dates outright, Q8a, DL-58). The compiler never expands rules; the runner's Scheduler and
preflight consume this module, which is the reference implementation to diff against a live
instance (Q8c/Q8d).

Every undocumented composition corner is a pinned deterministic default carrying a
`# PENDING: Q8x` marker (DL-59: the scheduler must schedule an ordinary estate).
CalendarRuleError is reserved for what cannot be interpreted: unknown tokens, doc-defective
tokens (the vendor text is garbled, so no default exists), missing holcal/cyccal
dependencies, and degenerate shapes (a walk with nowhere to land).
"""

from __future__ import annotations

import calendar as _calmod
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .ir import CalendarIR, CatalogIR, unquote_jil_value
from .semantics import DEFAULTS as DEFAULT_SWITCHES
from .semantics import SemanticSwitches, WekrFirstWeek

__all__ = [
    "CalendarRuleError",
    "CompiledCalendar",
    "compile_calendar",
    "reads_family",
    "standard_days",
    "standard_rows",
]


class CalendarRuleError(ValueError):
    """A calendar definition the interpreter refuses; the message names the calendar.

    Causes: unknown or doc-defective tokens, missing holcal/cyccal dependencies, a degenerate
    shape. Open composition corners never refuse; they run on pinned defaults (DL-59)."""


#: W/P walks and windowed generation share this reach: a year of walking (366) plus the
#: maximum |adjust| (9) plus slack. A walk that goes further has no valid day: refused.
_WALK_CAP = 366
_MARGIN = 400

#: Dormancy scan ceiling for unbounded rule calendars: beyond it the calendar generates
#: nothing (a leap-day-plus-weekday conjunction can gap ~40 years).
_SCAN_YEARS = 60

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_CODES2 = ("mo", "tu", "we", "th", "fr", "sa", "su")
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

#: `compile_calendar`'s attribute allow-list (SEM-36); the simulation coverage register
#: derives its calendar surface from it (DL-209).
CALENDAR_ATTRS = frozenset(
    {"description", "workday", "non_workday", "holiday", "holcal", "cyccal", "adjust"}
)

#: Doc-defective tokens (SEM-37): WORKDXnn contradicts its month-scoped siblings and the CWEK
#: family is garbled in the vendor's own text. Refused outright, no default, no switch.
#: Named per family so the register carries a refusal row for each; `_parse_token` and
#: `classify_token` both read the table (DL-209).
_DEFECTIVE_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("workdx", re.compile(r"workdx\d+", re.ASCII)),
    ("cwek", re.compile(r"cwek(#(\d|l)|m\d|x\d)", re.ASCII)),
)


#: The shapes a standard-calendar date row can take (SEM-36, Q9, DL-60): the date alone, or
#: the date plus a time tail in one of two spellings. (field count, colon count in the tail)
#: -> (form name, strptime format). The register derives the row-shape space from it (DL-209).
_ROW_TIME_FORMS: tuple[tuple[str, int, int, str | None], ...] = (
    ("date", 1, 0, None),
    ("hh:mm", 2, 1, "%H:%M"),
    ("hh:mm:ss", 2, 2, "%H:%M:%S"),
)


def _row_format(fields: int, colons: int) -> str | None:
    """The strptime format for a row of this shape, or None."""
    for _name, form_fields, form_colons, fmt in _ROW_TIME_FORMS:
        if form_fields == fields and form_colons == colons:
            return fmt
    return None


def classify_row(row: str) -> str | None:
    """Which `_ROW_TIME_FORMS` shape this date row is written in, or None (refused)."""
    parts = row.split()
    tail_colons = parts[1].count(":") if len(parts) == 2 else 0
    for name, fields, colons, _fmt in _ROW_TIME_FORMS:
        if len(parts) == fields and tail_colons == colons:
            return name
    return None


def standard_rows(cal: CalendarIR) -> dict[date, frozenset[tuple[int, int]]]:
    """A standard calendar's date rows as day -> (hour, minute) ticks.

    Rows are `mm/dd/yyyy` with an optional HH:MM or HH:MM:SS tail (Q9, DL-60); seconds
    truncate. A bare row's tick is 00:00, the vendor's firing time for a job with no
    start_times/start_mins (E11, DL-58). Raises CalendarRuleError naming the first bad row."""
    rows: dict[date, set[tuple[int, int]]] = {}
    for row in cal.dates:
        parts = row.split()
        try:
            day = datetime.strptime(parts[0] if parts else "", "%m/%d/%Y").date()
            if len(parts) > 2:
                raise ValueError
            tick = (0, 0)
            if len(parts) == 2:
                tail = parts[1]
                fmt = _row_format(len(parts), tail.count(":"))
                if fmt is None:
                    raise ValueError
                t = datetime.strptime(tail, fmt).time()
                tick = (t.hour, t.minute)
        except ValueError:
            raise CalendarRuleError(
                f"calendar {cal.name!r}: unparseable date row {row!r}"
                " (expected mm/dd/yyyy with an optional HH:MM[:SS] tail)"
            ) from None
        rows.setdefault(day, set()).add(tick)
    return {d: frozenset(ticks) for d, ticks in rows.items()}


def standard_days(cal: CalendarIR) -> frozenset[date]:
    """A standard calendar's date rows as a day set, for exclusion and eligibility checks."""
    return frozenset(standard_rows(cal))


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True)
class _Ctx:
    """Evaluation context one compiled calendar closes over."""

    workdays: frozenset[int]  # date.weekday() values
    holidays: frozenset[date]
    periods: tuple[tuple[date, date], ...]
    #: the `wekr-first-week` switch (DL-259): where week 1 of a WEKR token's year starts
    wekr_first_week: WekrFirstWeek


_Pred = Callable[[date, _Ctx], bool]


@dataclass(frozen=True)
class _Token:
    base: _Pred  # the token's day set WITHOUT the exclusion flip
    exclusive: bool
    cycle_scoped: bool


# expression AST: ("tok", _Token) | ("not", node) | ("and", l, r) | ("or", l, r)
_Node = tuple[Any, ...]


def _err(cal: str, msg: str) -> CalendarRuleError:
    return CalendarRuleError(f"extended calendar {cal!r}: {msg}")


def _ord_in(cal: str, tok: str, text: str, lo: int, hi: int) -> int:
    try:
        n = int(text)
    except ValueError:  # pragma: no cover -- int() reads every digit the callers' \d patterns match
        raise _err(cal, f"token {tok!r}: ordinal {text!r} is not a number") from None
    if not lo <= n <= hi:
        raise _err(cal, f"token {tok!r}: ordinal {n} outside {lo}..{hi}")
    return n


def _nth_of(seq: list[date], n: int, *, back: bool) -> date | None:
    """1-based nth (back: 1 = last, the vendor's 'M' convention)."""
    idx = len(seq) - n if back else n - 1
    return seq[idx] if 0 <= idx < len(seq) else None


def _month_days(day: date) -> int:
    return _calmod.monthrange(day.year, day.month)[1]


def _month_matching(day: date, keep: Callable[[date], bool]) -> list[date]:
    first = day.replace(day=1)
    return [d for offset in range(_month_days(day)) if keep(d := first + timedelta(days=offset))]


def _week_start(day: date, anchor_wd: int) -> date:
    return day - timedelta(days=(day.weekday() - anchor_wd) % 7)


def _jan1_anchor(day: date) -> int:
    """Default week basis (SEM-37 [V]): weeks begin on Jan 1's weekday."""
    return date(day.year, 1, 1).weekday()


def _week_number(day: date, anchor_wd: int | None = None) -> int:
    jan1 = date(day.year, 1, 1)
    wd = _jan1_anchor(day) if anchor_wd is None else anchor_wd
    return (day - _week_start(jan1, wd)).days // 7 + 1


def _wekr_week(day: date, anchor_wd: int, first_week: WekrFirstWeek) -> int:
    """`day`'s week of its own year for a WEKR token anchored on weekday `anchor_wd` (SEM-37).

    0 is a day before week 1. `first-full`: week 1 starts on the first anchor day on or after
    January 1 and earlier days are in no week. `partial`: week 1 runs from January 1 to the
    day before that anchor day. A week past December 31 ends there; its January days count
    in the next year."""
    jan1 = date(day.year, 1, 1)
    number = (day - _week_start(jan1, anchor_wd)).days // 7 + 1
    if first_week == "first-full" and jan1.weekday() != anchor_wd:
        number -= 1
    return number


def _year_weeks(day: date) -> int:
    return (date(day.year, 12, 31) - date(day.year, 1, 1)).days // 7 + 1


def _period_of(day: date, ctx: _Ctx) -> tuple[int, date, date] | None:
    for idx, (start, end) in enumerate(ctx.periods, start=1):
        if start <= day <= end:
            return idx, start, end
    return None


def _period_chunk(day: date, ctx: _Ctx) -> tuple[int, int] | None:
    """(week-of-period, last-week-of-period): 7-day chunks from each period's first day.

    The last chunk may be partial. Chunks anchor to the period start, not to calendar weeks:
    a Broadcom example reads CWEEK#01|CWEEK#02 over a quarterly cycle as "the first 14 days
    in every quarter" (Q8e, DL-58)."""
    hit = _period_of(day, ctx)
    if hit is None:
        return None
    _, start, end = hit
    return (day - start).days // 7 + 1, (end - start).days // 7 + 1


def _period_matching(day: date, ctx: _Ctx, keep: Callable[[date], bool]) -> list[date] | None:
    hit = _period_of(day, ctx)
    if hit is None:
        return None
    _, start, end = hit
    return [
        d for offset in range((end - start).days + 1) if keep(d := start + timedelta(days=offset))
    ]


def _ordinal(cal: str, raw: str, letter: str, digits: str, *, lo: int, hi: int) -> tuple[int, bool]:
    """Decode one `(#|m|x)(\\d+|l)` ordinal suffix (SEM-37) to (n, back).

    `letter` is the family's back/exclusion marker, `digits` the ordinal or `l`. `l` ("last",
    extended to all families, Q9, DL-60) maps to n=1 with back True, so `_nth_of` reads it
    like any from-end count. `m` also forces back."""
    last = digits == "l"
    n = 1 if last else _ord_in(cal, raw, digits, lo, hi)
    back = letter == "m" or last
    return n, back


#: The rule-expression alphabet; the coverage register derives the operator surface from it
#: (DL-209). `{}` group like `()`: the observed export writes braces where TechDocs shows
#: parens (Q9, DL-60).
_GROUP_CHARS: dict[str, str] = {"(": "(", ")": ")", "{": "(", "}": ")"}
#: `&`/`|` and their word synonyms (PENDING: Q8d), to the AST node kind.
_BINARY_OPS: dict[str, Literal["and", "or"]] = {"&": "and", "|": "or", "and": "and", "or": "or"}
_UNARY_OPS = frozenset({"not"})
#: The X- prefix exclusion convention (SEM-37) and the rule-list separator.
_EXCLUSION_PREFIX = "x"
_RULE_SEPARATOR = ","


def _workday_seq(day: date, ctx: _Ctx) -> list[date]:
    """The month's workdays, in order -- the sequence WORKD#n counts into."""
    return _month_matching(day, lambda d: d.weekday() in ctx.workdays)


def _month_keyword(month: int) -> _Pred:
    return lambda d, c: d.month == month


def _weekday_keyword(weekday: int) -> _Pred:
    return lambda d, c: d.weekday() == weekday


#: SEM-37's bare keywords: body -> (day predicate, cycle_scoped). The coverage register
#: derives its calendar surface from the table (DL-209). A keyword and a family pattern never
#: match the same body.
_KEYWORDS: dict[str, tuple[_Pred, bool]] = {
    "daily": (lambda d, c: True, False),
    # [V] auto-subtracts the holiday calendar (SEM-37 quote, DL-244); no holcal means an
    # empty set
    "workdays": (lambda d, c: d.weekday() in c.workdays and d not in c.holidays, False),
    # [V] auto-subtracts the holiday calendar (SEM-37 quote)
    "weekdays": (lambda d, c: d.weekday() < 5 and d not in c.holidays, False),
    "fomwork": (lambda d, c: bool(s := _workday_seq(d, c)) and d == s[0], False),
    "eomwork": (lambda d, c: bool(s := _workday_seq(d, c)) and d == s[-1], False),
    "fomweek": (
        lambda d, c: bool(s := _month_matching(d, lambda x: x.weekday() < 5)) and d == s[0],
        False,
    ),
    "eomweek": (
        lambda d, c: bool(s := _month_matching(d, lambda x: x.weekday() < 5)) and d == s[-1],
        False,
    ),
    "fom": (lambda d, c: d.day == 1, False),
    "eom": (lambda d, c: d.day == _month_days(d), False),
    "cycle": (lambda d, c: _period_of(d, c) is not None, True),
    **{name: (_month_keyword(i + 1), False) for i, name in enumerate(_MONTH_NAMES)},
    **{name: (_weekday_keyword(i), False) for i, name in enumerate(_DAY_NAMES)},
}


#: One family builder: (calendar name, raw token, the family match, the X-prefix flag) -> the
#: token. Ordinal families accept a literal L as "last" (from-end 1), as the observed export
#: uses WORKD#L (Q9, DL-60).
_Builder = Callable[[str, str, "re.Match[str]", bool], _Token]


def _mk(base: _Pred, *, excl: bool, cycle: bool = False) -> _Token:
    """`excl` has no default so a builder cannot forget the X-prefix flag."""
    return _Token(base=base, exclusive=excl, cycle_scoped=cycle)


def _b_workd(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=31)
    return _mk(lambda d, c: _nth_of(_workday_seq(d, c), n, back=back) == d, excl=exclusive)


def _b_weekd(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=7)
    pred: _Pred = (
        (lambda d, c: (d - _week_start(d, _jan1_anchor(d))).days == 7 - n)
        if back
        else (lambda d, c: (d - _week_start(d, _jan1_anchor(d))).days == n - 1)
    )
    return _mk(pred, excl=exclusive or m.group(1) == "x")


def _wekr_anchor(cal: str, raw: str, text: str) -> int:
    """A WEKR anchor as a `date.weekday()` value: a day name in 12.x, a digit 1-7 in 24.2
    (SEM-37)."""
    if text in _DAY_NAMES:
        return _DAY_NAMES.index(text)
    number = int(text)
    if not 1 <= number <= 7:
        raise _err(cal, f"token {raw!r}: anchor {number} outside 1..7 (1 = Monday)")
    return number - 1


def _b_wekr(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    """The nth week of the year, weeks starting on the anchor day (SEM-37).

    `#` counts from week 1, `M` back from the week holding December 31, `X` excludes. Week 1
    follows the `wekr-first-week` switch."""
    anchor = _wekr_anchor(cal, raw, m.group(1))
    n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=53)

    def wekr(d: date, c: _Ctx) -> bool:
        week = _wekr_week(d, anchor, c.wekr_first_week)
        if not back:
            return week == n
        last = _wekr_week(date(d.year, 12, 31), anchor, c.wekr_first_week)
        return week >= 1 and week == last - n + 1

    return _mk(wekr, excl=exclusive or m.group(2) == "x")


def _b_week_parity(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    parity = 0 if m.group(1) == "e" else 1
    return _mk(lambda d, c: _week_number(d) % 2 == parity, excl=exclusive)


def _b_week(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=53)
    pred: _Pred = (
        (lambda d, c: _week_number(d) == _year_weeks(d) - n + 1)
        if back
        else (lambda d, c: _week_number(d) == n)
    )
    return _mk(pred, excl=exclusive or m.group(1) == "x")


def _b_mnthd(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=31)
    pred: _Pred = (
        (lambda d, c: d.day == _month_days(d) - n + 1) if back else (lambda d, c: d.day == n)
    )
    return _mk(pred, excl=exclusive or m.group(1) == "x")


def _b_month_ordinal(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    month = _MONTH_NAMES.index(m.group(1)) + 1
    n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=31)
    pred: _Pred = (
        (lambda d, c: d.month == month and d.day == _month_days(d) - n + 1)
        if back
        else (lambda d, c: d.month == month and d.day == n)
    )
    return _mk(pred, excl=exclusive)


def _b_day_ordinal(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    wd = _DAY_NAMES.index(m.group(1))
    n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=5)
    return _mk(
        lambda d, c: (
            d.weekday() == wd
            and _nth_of(_month_matching(d, lambda x: x.weekday() == wd), n, back=back) == d
        ),
        excl=exclusive,
    )


def _b_cycl(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=365)

    def cycl(d: date, c: _Ctx) -> bool:
        hit = _period_of(d, c)
        if hit is None:
            return False
        _, start, end = hit
        idx = (end - d).days + 1 if back else (d - start).days + 1
        return idx == n

    return _mk(cycl, cycle=True, excl=exclusive or m.group(1) == "x")


def _b_cycp(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n = _ord_in(cal, raw, m.group(1), 1, 30)
    return _mk(
        lambda d, c: (h := _period_of(d, c)) is not None and h[0] == n,
        cycle=True,
        excl=exclusive,
    )


def _b_cweek_parity(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    which = m.group(1)

    def cweek_l(d: date, c: _Ctx) -> bool:
        chunk = _period_chunk(d, c)
        if chunk is None:
            return False
        wk, last = chunk
        return wk == last if which == "l" else wk % 2 == (0 if which == "e" else 1)

    return _mk(cweek_l, cycle=True, excl=exclusive)


def _b_cweek(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    # no literal 'l' here: `cweek#([eol])` owns that spelling for this family
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=53)

    def cweek(d: date, c: _Ctx) -> bool:
        chunk = _period_chunk(d, c)
        if chunk is None:
            return False
        wk, last = chunk
        return wk == (last - n + 1 if back else n)

    return _mk(cweek, cycle=True, excl=exclusive or m.group(1) == "x")


def _b_cwrk(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=365)

    def cwrk(d: date, c: _Ctx) -> bool:
        seq = _period_matching(d, c, lambda x: x.weekday() in c.workdays)
        if seq is None:
            return False
        return _nth_of(seq, n, back=back) == d

    return _mk(cwrk, cycle=True, excl=exclusive or m.group(1) == "x")


def _b_cddd(cal: str, raw: str, m: re.Match[str], exclusive: bool) -> _Token:
    wd = _DAY_NAMES.index(m.group(1))
    n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=53)

    def cddd(d: date, c: _Ctx) -> bool:
        if d.weekday() != wd:
            return False
        seq = _period_matching(d, c, lambda x: x.weekday() == wd)
        if seq is None:
            return False
        return _nth_of(seq, n, back=back) == d

    return _mk(cddd, cycle=True, excl=exclusive)


#: SEM-37's regex token families as (name, pattern matched against the X-stripped body,
#: builder); the register derives its surface from the table (DL-209).
_FAMILIES: tuple[tuple[str, re.Pattern[str], _Builder], ...] = (
    ("workd", re.compile(r"workd([#m])(\d+|l)"), _b_workd),
    ("weekd", re.compile(r"weekd([#mx])(\d+|l)"), _b_weekd),
    ("wekr", re.compile(r"wekr(\d|mon|tue|wed|thu|fri|sat|sun)([#mx])(\d+|l)"), _b_wekr),
    ("week_parity", re.compile(r"week#([eo])"), _b_week_parity),
    ("week", re.compile(r"week([#mx])(\d+|l)"), _b_week),
    ("mnthd", re.compile(r"mnthd([#mx])(\d+|l)"), _b_mnthd),
    (
        "month_ordinal",
        re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)([#m])(\d+|l)"),
        _b_month_ordinal,
    ),
    ("day_ordinal", re.compile(r"(mon|tue|wed|thu|fri|sat|sun)([#m])(\d|l)"), _b_day_ordinal),
    ("cycl", re.compile(r"cycl([#mx])(\d+|l)"), _b_cycl),
    ("cycp", re.compile(r"cycp#(\d+)"), _b_cycp),
    ("cweek_parity", re.compile(r"cweek#([eol])"), _b_cweek_parity),
    ("cweek", re.compile(r"cweek([#mx])(\d+)"), _b_cweek),
    ("cwrk", re.compile(r"cwrk([#mx])(\d+|l)"), _b_cwrk),
    ("cddd", re.compile(r"c(mon|tue|wed|thu|fri|sat|sun)([#m])(\d+|l)"), _b_cddd),
)


def classify_token(raw: str) -> tuple[str, str] | None:
    """Which table `_parse_token` reads this token from, without building a predicate.

    Returns ("keyword"|"family"|"defective", name), or None for a token outside the
    inventory. The register's calendar detectors read it (DL-209); the parser stays the
    authority on meaning."""
    tok = raw.lower()
    for name, pattern in _DEFECTIVE_FAMILIES:
        if pattern.fullmatch(tok):
            return "defective", name
    body = tok[1:] if tok.startswith(_EXCLUSION_PREFIX) else tok
    if body in _KEYWORDS:
        return "keyword", body
    for name, pattern, _builder in _FAMILIES:
        if pattern.fullmatch(body):
            return "family", name
    return None


def reads_family(cal: CalendarIR, family: str) -> bool:
    """Whether any word of `cal`'s rules is a token of the SEM-37 family `family`.

    A rule the parser would refuse still answers."""
    return any(
        classify_token(word) == ("family", family)
        for line in cal.conditions
        for word in _TOKEN_RE.findall(line)
    )


def _parse_token(cal: str, raw: str) -> _Token:
    """One keyword to its day predicate; case-insensitive (SEM-37 [V]). Unknown and
    doc-defective tokens refuse."""
    tok = raw.lower()
    if any(pattern.fullmatch(tok) for _, pattern in _DEFECTIVE_FAMILIES):
        raise _err(cal, f"token {raw!r} is doc-defective (SEM-37); refused pending Q8/live")

    # the X- prefix exclusion; infix X-forms match per family. No documented token starts
    # with a non-exclusion 'x'.
    exclusive = tok.startswith(_EXCLUSION_PREFIX)
    body = tok[1:] if exclusive else tok

    entry = _KEYWORDS.get(body)
    if entry is not None:
        base, cycle_scoped = entry
        return _Token(base=base, exclusive=exclusive, cycle_scoped=cycle_scoped)
    for _name, pattern, builder in _FAMILIES:
        m = pattern.fullmatch(body)
        if m is not None:
            return builder(cal, raw, m, exclusive)

    raise _err(cal, f"unknown date-condition token {raw!r} (SEM-37 inventory)")


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9#]*")


def _tokenize(cal: str, rule: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(rule):
        ch = rule[i]
        if ch.isspace():
            i += 1
        elif ch in _GROUP_CHARS or ch in _BINARY_OPS:
            # braces normalize to parens (`_GROUP_CHARS`); `&` and `|` carry through
            out.append(_GROUP_CHARS.get(ch, ch))
            i += 1
        elif m := _TOKEN_RE.match(rule, i):
            out.append(m.group(0))
            i = m.end()
        else:
            raise _err(cal, f"condition {rule!r}: unexpected character {ch!r}")
    return out


def _parse_rule(cal: str, rule: str) -> _Node:
    """One comma-separated rule to an AST. `&`/`|` evaluate flat
    left-to-right and the words AND/OR/NOT are synonyms (PENDING: Q8d)."""
    toks = _tokenize(cal, rule)
    pos = 0

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def term(depth: int) -> _Node:
        nonlocal pos
        if depth > 100:
            raise _err(cal, f"condition {rule!r}: nesting deeper than 100")
        tok = peek()
        if tok is None:
            raise _err(cal, f"condition {rule!r}: dangling operator")
        if tok == "(":
            pos += 1
            node = expr(depth + 1)
            if peek() != ")":
                raise _err(cal, f"condition {rule!r}: missing ')'")
            pos += 1
            return node
        if tok.lower() in _UNARY_OPS:
            pos += 1
            return ("not", term(depth + 1))
        if tok in ("&", "|", ")"):
            raise _err(cal, f"condition {rule!r}: unexpected {tok!r}")
        pos += 1
        return ("tok", _parse_token(cal, tok))

    def expr(depth: int) -> _Node:
        nonlocal pos
        node = term(depth)
        while (tok := peek()) is not None and tok != ")":
            found = _BINARY_OPS.get(tok) or _BINARY_OPS.get(tok.lower())
            if found is None:
                raise _err(cal, f"condition {rule!r}: expected an operator, got {tok!r}")
            op: Literal["and", "or"] = found
            pos += 1
            node = (op, node, term(depth))
        return node

    node = expr(0)
    if pos != len(toks):
        raise _err(cal, f"condition {rule!r}: trailing input")
    return node


def _eval(node: _Node, day: date, ctx: _Ctx) -> bool:
    kind = node[0]
    if kind == "tok":
        tok: _Token = node[1]
        hit = tok.base(day, ctx)
        return not hit if tok.exclusive else hit
    if kind == "not":
        return not _eval(node[1], day, ctx)
    left = _eval(node[1], day, ctx)
    right = _eval(node[2], day, ctx)
    return (left and right) if kind == "and" else (left or right)


def _walk_tokens(node: _Node) -> list[_Token]:
    if node[0] == "tok":
        return [node[1]]
    if node[0] == "not":
        return _walk_tokens(node[1])
    return _walk_tokens(node[1]) + _walk_tokens(node[2])


def _exclusion_base(node: _Node) -> _Node | None:
    """A top-level rule that is only an exclusion subtracts its base set from the union of
    inclusive rules (PENDING: Q8d). Returns the node whose inclusive reading is that set."""
    if node[0] == "tok":
        tok: _Token = node[1]
        if tok.exclusive:
            return ("tok", _Token(base=tok.base, exclusive=False, cycle_scoped=tok.cycle_scoped))
        return None
    if node[0] == "not":
        inner = _exclusion_base(node[1])
        # NOT of an exclusion is inclusive again; NOT of an inclusive rule excludes it
        return None if inner is not None else node[1]
    return None


# ---------------------------------------------------------------- compile


_ACTIONS = frozenset("osnwp")
_REPLACE = frozenset("nwp")  # the replacing action codes; O/S never move a date

#: The two categories an action code can be stated for (SEM-36, SEM-38). The register carries
#: a row per (category, code): N walks for a non-workday and advances one day for a holiday
#: (DL-209).
ActionCategory = Literal["non_workday", "holiday"]
_ACTION_CATEGORIES: tuple[ActionCategory, ...] = ("non_workday", "holiday")
_HOLIDAY_CATEGORY: ActionCategory = "holiday"


@dataclass(frozen=True)
class CompiledCalendar:
    """An extended calendar ready to generate days; pure and immutable.

    `bound` is the last day it can produce (None = unbounded rules)."""

    name: str
    ctx: _Ctx
    include: tuple[_Node, ...]
    exclude: tuple[_Node, ...]
    non_workday: str | None
    holiday: str | None
    adjust: int
    bound: date | None

    def days_between(self, lo: date, hi: date) -> frozenset[date]:
        """Final (post-disposition, post-adjust) days within [lo, hi].

        Generation windows over [lo-margin, hi+margin] so edge-crossing replacements show."""
        if hi < lo:
            return frozenset()
        win_lo, win_hi = lo - timedelta(days=_MARGIN), hi + timedelta(days=_MARGIN)
        candidates = [
            d
            for off in range((win_hi - win_lo).days + 1)
            if self._included(d := win_lo + timedelta(days=off))
        ]
        final = self._dispose(candidates)
        if self.adjust:
            final = {d + timedelta(days=self.adjust) for d in final}
        return frozenset(d for d in final if lo <= d <= hi)

    def first_on_or_after(self, day: date) -> date | None:
        """Earliest generated day at or after `day`, scanning to `bound` or the dormancy
        ceiling; None when the calendar is exhausted or empty."""
        cap = self.bound or day + timedelta(days=_SCAN_YEARS * 366)
        cur = day
        while cur <= cap:
            window_end = min(cur + timedelta(days=365), cap)
            hits = [d for d in self.days_between(cur, window_end) if d >= day]
            if hits:
                return min(hits)
            cur = window_end + timedelta(days=1)
        return None

    # ------------------------------------------------------------- internals

    def _included(self, day: date) -> bool:
        if not any(_eval(n, day, self.ctx) for n in self.include):
            return False
        return not any(_eval(n, day, self.ctx) for n in self.exclude)

    def _dispose(self, candidates: list[date]) -> set[date]:
        """SEM-38 pipeline: filter first, then single-shot replacement.

        O-codes are category filters; N/W/P replace category members with a target day, and a
        replacement result is final, never re-processed by the other category ("applies even
        if the next day is a holiday or non-workday"). A specified holiday action governs
        every holcal date outright: the vendor applies the non-workday action to holcal dates
        only "when you do not specify an action at the Holiday Action prompt" (Q8a, DL-58).
        Whether a replacement target re-enters the other stage is unverified; it stays
        single-shot. With no holiday action, a holcal date is a non-workday for the
        non_workday code whatever its weekday (DL-244, SEM-38): the predicate is
        weekday-non-workday OR holcal member."""
        out: set[date] = set()
        for day in candidates:
            if self.holiday is not None and day in self.ctx.holidays:
                if self.holiday in _REPLACE:
                    out.add(self._replace(_HOLIDAY_CATEGORY, day))
                else:  # "o": restrict-to-holidays keeps it; "s" keeps as-is
                    out.add(day)
                continue
            if self.holiday == "o":
                continue  # not a holiday: the restrict-to-holidays filter drops it
            is_non_workday = day.weekday() not in self.ctx.workdays or day in self.ctx.holidays
            if self.non_workday == "o" and not is_non_workday:
                continue
            if self.non_workday in _REPLACE and is_non_workday:
                out.add(self._replace("non_workday", day))
            else:
                out.add(day)
        return out

    def _replace(self, category: str, day: date) -> date:
        """One replacement code's target for one excluded date."""
        code = self.holiday if category == _HOLIDAY_CATEGORY else self.non_workday
        if code is None or code not in _REPLACE:  # pragma: no cover -- both callers test it first
            raise _err(self.name, f"internal: {category} code {code!r} is not a replacement")
        if code == "n" and category == _HOLIDAY_CATEGORY:
            # [V] one-shot: 'Excludes the holiday and includes the next day. This applies
            # even if the next day is a holiday or non-workday.'
            return day + timedelta(days=1)
        if code == "n":
            # [V] 'include the next workday that also meets all other criteria', pinned as
            # the next non-holiday workday.
            # PENDING: Q8c -- the date-conditions are NOT re-checked
            return self._walk(day, 1, holiday_free=True, code=code, category=category)
        step = 1 if code == "w" else -1
        # holiday W/P demand a non-holiday workday ([V] worked examples); non_workday W/P
        # say only 'work day'.
        # PENDING: Q8c -- no worked example; holiday-ness of the target
        # deliberately not re-checked
        return self._walk(
            day, step, holiday_free=category == _HOLIDAY_CATEGORY, code=code, category=category
        )

    def _walk(self, day: date, step: int, *, holiday_free: bool, code: str, category: str) -> date:
        probe = day
        for _ in range(_WALK_CAP):
            probe = probe + timedelta(days=step)
            if probe.weekday() not in self.ctx.workdays:
                continue
            if holiday_free and probe in self.ctx.holidays:
                continue
            return probe
        raise _err(self.name, f"{category} action {code!r} found no valid day within a year")


def _workday_all(cal: str, text: str) -> frozenset[int]:
    return frozenset(range(7))


def _workday_mask(cal: str, text: str) -> frozenset[int]:
    return frozenset(i for i, ch in enumerate(text) if ch == "x")


def _workday_codes(cal: str, text: str) -> frozenset[int]:
    days = set()
    for part in text.split(","):
        code = part.strip()
        if code in _DAY_CODES2:
            days.add(_DAY_CODES2.index(code))
        elif code in _DAY_NAMES:
            days.add(_DAY_NAMES.index(code))
        else:
            raise _err(cal, f"workday: unrecognized day {part.strip()!r}")
    return frozenset(days)


#: The `workday:` serializations (SEM-36): positional `{X|.}` x7 (Monday-first), the comma
#: list of two- or three-letter day codes, and the observed `all` (Q9, DL-60). The register
#: derives the form space from it (DL-209). `codes` is the fallthrough; its parse refuses an
#: unknown day.
_WORKDAY_FORMS: tuple[
    tuple[str, Callable[[str], bool], Callable[[str, str], frozenset[int]]], ...
] = (
    ("all", lambda text: text == "all", _workday_all),
    ("mask", lambda text: re.fullmatch(r"[x.]{7}", text) is not None, _workday_mask),
    ("codes", lambda text: True, _workday_codes),
)


def classify_workday(value: str) -> str:
    """Which `_WORKDAY_FORMS` entry this `workday:` value is written in."""
    text = value.strip().lower()
    return next(name for name, matches, _build in _WORKDAY_FORMS if matches(text))


def _parse_workday(cal: str, value: str) -> frozenset[int]:
    """All vendor serializations (SEM-36), one per `_WORKDAY_FORMS` row."""
    text = value.strip().lower()
    for _name, matches, build in _WORKDAY_FORMS:
        if matches(text):
            return build(cal, text)
    raise AssertionError("unreachable: the codes form matches everything")


def _parse_action(cal: str, key: str, value: str) -> str | None:
    text = value.strip().lower()
    if not text:
        return None
    if text in _ACTIONS:
        return text
    raise _err(cal, f"{key}: expected one of O/S/N/W/P, got {value.strip()!r}")


def _parse_periods(cal: str, cycle_name: str, catalog: CatalogIR) -> tuple[tuple[date, date], ...]:
    cyc = catalog.cycles.get(cycle_name)
    if cyc is None:
        raise _err(cal, f"cyccal {cycle_name!r} names no cycle in the loaded set")
    periods = []
    for start_raw, end_raw in cyc.periods:
        try:
            start = datetime.strptime(start_raw, "%m/%d/%Y").date()
            end = datetime.strptime(end_raw, "%m/%d/%Y").date()
        except ValueError:
            raise _err(
                cal,
                f"cycle {cycle_name!r}: unparseable period ({start_raw!r}, {end_raw!r})",
            ) from None
        if end < start:
            raise _err(cal, f"cycle {cycle_name!r}: period ends before it starts")
        periods.append((start, end))
    if not periods:
        raise _err(cal, f"cycle {cycle_name!r} has no periods")
    return tuple(periods)


def compile_calendar(
    cal: CalendarIR, catalog: CatalogIR, semantics: SemanticSwitches | None = None
) -> CompiledCalendar:
    """Parse and validate one extended calendar against the loaded set.

    Raises CalendarRuleError on what the SEM-36..39 freeze cannot interpret: unknown or
    defective tokens, missing dependencies, degenerate walks. Open composition corners
    compile on pinned defaults (Q8b, Q8d, DL-59). `semantics` are the period's switches
    (DL-252); None reads the registry defaults."""
    if cal.kind != "extended":
        raise CalendarRuleError(f"calendar {cal.name!r} is standard; use standard_days()")
    if "condition" in cal.attrs:  # pragma: no cover -- lowering never puts it in attrs
        raise _err(cal.name, "condition in attrs (hand-built IR?); lowering owns the lane")

    unknown = set(cal.attrs) - CALENDAR_ATTRS
    if unknown:
        raise _err(cal.name, f"unrecognized attribute(s) {sorted(unknown)!r} (SEM-36)")

    workdays = (
        _parse_workday(cal.name, cal.attrs["workday"])
        if cal.attrs.get("workday", "").strip()
        else frozenset(range(5))
    )
    actions = {
        category: _parse_action(cal.name, category, cal.attrs.get(category, ""))
        for category in _ACTION_CATEGORIES
    }
    non_workday, holiday = actions["non_workday"], actions["holiday"]

    holidays: frozenset[date] = frozenset()
    holcal_name = cal.attrs.get("holcal", "").strip()
    if holcal_name:
        hol = catalog.calendars.get(unquote_jil_value(holcal_name))
        if hol is None:
            raise _err(cal.name, f"holcal {holcal_name!r} names no calendar in the loaded set")
        if hol.kind != "standard":
            raise _err(
                cal.name,
                f"holcal {holcal_name!r} is an extended calendar -- the docs permit only"
                " standard calendars here (SEM-36)",
            )
        holidays = standard_days(hol)
    if holiday is not None and holiday != "s" and not holcal_name:
        # holiday S is a pass-through that consumes no holiday set (the observed export
        # carries `holiday: S` with holcal empty, Q9, DL-60); O/N/W/P need the set (SEM-36)
        raise _err(cal.name, "a holiday action (other than S) requires holcal (SEM-36)")

    adjust_raw = cal.attrs.get("adjust", "").strip()
    adjust = 0
    if adjust_raw:
        try:
            adjust = int(adjust_raw)
        except ValueError:
            raise _err(cal.name, f"adjust: expected an integer, got {adjust_raw!r}") from None
        if abs(adjust) > 9:
            raise _err(cal.name, f"adjust: {adjust} outside the documented -9..+9 (SEM-36)")
    # PENDING: Q8b -- nonzero adjust combined with an N/W/P replacement is
    # undocumented. Pinned default (DL-59, deterministic over fail-closed): the SEM-38
    # pipeline order as-is, so disposition replaces first and the uniform blind adjust then
    # shifts every survivor (replace-then-shift). The runbook's probe pair discriminates
    # vendor truth if access appears.

    rules: list[str] = []
    for line in cal.conditions:
        rules.extend(part.strip() for part in line.split(_RULE_SEPARATOR) if part.strip())
    include: list[_Node] = []
    exclude: list[_Node] = []
    cycle_scoped_only = True
    for rule in rules:
        node = _parse_rule(cal.name, rule)
        base = _exclusion_base(node)
        if base is not None:
            exclude.append(base)
            continue
        # PENDING: Q8d -- a compound rule with NO inclusive leaf (e.g.
        # `xtue|xwed`) is neither a recognized exclusion form nor a likely authorial intent.
        # Pinned default (DL-59, deterministic over fail-closed): literal boolean evaluation
        # as an include, the same complement-in-AND algebra mixed rules use, even though that
        # makes `xtue|xwed` near-universal.
        include.append(node)
        if not all(t.cycle_scoped for t in _walk_tokens(node)):
            cycle_scoped_only = False
    if not include:
        # DAILY is the documented default; exclusion-only rule lists subtract from it
        # rather than from nothing (PENDING: Q8d)
        include.append(("tok", _Token(base=lambda d, c: True, exclusive=False, cycle_scoped=False)))
        cycle_scoped_only = False

    periods: tuple[tuple[date, date], ...] = ()
    needs_cycle = any(t.cycle_scoped for node in include + exclude for t in _walk_tokens(node))
    cyccal_name = cal.attrs.get("cyccal", "").strip()
    if needs_cycle:
        if not cyccal_name:
            raise _err(cal.name, "cycle-scoped conditions require cyccal (SEM-36)")
        periods = _parse_periods(cal.name, unquote_jil_value(cyccal_name), catalog)
    elif cyccal_name:
        # a named but unused cycle must still resolve, as in L018 (rule_l018, lint.py); both
        # key the lookup through ir.unquote_jil_value (DL-178q)
        periods = _parse_periods(cal.name, unquote_jil_value(cyccal_name), catalog)

    if (non_workday in ("w", "p") or holiday in ("w", "p")) and not workdays:
        raise _err(cal.name, "a W/P action with an all-non-workday mask has nowhere to walk")

    bound: date | None = None
    if include and cycle_scoped_only and periods:
        bound = max(end for _, end in periods) + timedelta(days=_MARGIN)

    return CompiledCalendar(
        name=cal.name,
        ctx=_Ctx(
            workdays=workdays,
            holidays=holidays,
            periods=periods,
            wekr_first_week=(semantics or DEFAULT_SWITCHES).wekr_first_week,
        ),
        include=tuple(include),
        exclude=tuple(exclude),
        non_workday=non_workday,
        holiday=holiday,
        adjust=adjust,
        bound=bound,
    )


#: An ordinal's leading zeros after its `#`/`M`/`X` marker: `MNTHD#05` is `MNTHD#5` (SEM-37,
#: zero padding is a spelling width).
_ORDINAL_ZEROS = re.compile(r"([#mx])0+(\d)")
#: A 24.2 numeric WEKR anchor, which `_wekr_anchor` reads as a day name.
_WEKR_DIGIT = re.compile(r"wekr([1-7])(?=[#mx])")


def _canonical_keyword(cal: str, lowered: str) -> str:
    """One spelling per meaning for a keyword the parser accepts.

    Ordinals lose padding and a numeric WEKR anchor becomes its day name, so `#02` and `#2`
    are one key. A refused token keeps its spelling; `compile_calendar` refuses it."""
    try:
        _parse_token(cal, lowered)
    except CalendarRuleError:
        return lowered
    unpadded = _ORDINAL_ZEROS.sub(r"\1\2", lowered)
    return _WEKR_DIGIT.sub(lambda m: "wekr" + _DAY_NAMES[int(m.group(1)) - 1], unpadded)


def semantic_key(
    cal: CalendarIR,
    catalog: CatalogIR | None = None,
    semantics: SemanticSwitches | None = None,
) -> tuple[Any, ...]:
    """The extended-calendar surface this engine reads, canonicalized with the same parsers.

    The DL-131 classifier calls two spellings of one rule set one value; comparing raw
    attribute text would refuse a live boundary over `MON | TUE`, a reordered day list, a
    re-cased action or `adjust: 00`. Tolerant on purpose: a piece the parsers refuse keeps
    its raw collapsed lowered spelling; `compile_calendar` refuses it at preflight. Rules
    compare as a set of canonical token tuples; `{}` reads as `()` and word operators as
    symbols. `semantics` are the switches the holiday-shielding reach compiles under; a
    switch is a classifier node of its own (DL-259)."""

    def canonical_token(token: str) -> str:
        if token in "&|()":
            return token
        lowered = token.lower()
        return {"and": "&", "or": "|"}.get(lowered, _canonical_keyword(cal.name, lowered))

    def parsed_rules() -> tuple[tuple[str, ...], ...]:
        rules: set[tuple[str, ...]] = set()
        for line in cal.conditions:
            for part in line.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    rules.add(tuple(canonical_token(t) for t in _tokenize(cal.name, part)))
                except CalendarRuleError:
                    rules.add((" ".join(part.split()).lower(),))
        if not rules:
            # `compile_calendar`'s fallback: no rules means DAILY, so an omitted rule list
            # and `condition: daily` are one calendar
            rules.add(("daily",))
        return tuple(sorted(rules))

    workday_raw = cal.attrs.get("workday", "").strip()
    # an omitted workday is Monday-Friday, as in `compile_calendar`
    workday: Any = frozenset(range(5))
    if workday_raw:
        try:
            workday = _parse_workday(cal.name, workday_raw)
        except CalendarRuleError:
            workday = workday_raw.lower()

    # shielding reach resolves through the holiday set, not the spelling of the reference: a
    # holcal naming an empty calendar gives `holiday: S` nothing to keep. An unresolvable
    # reference (no catalog, a missing or extended holcal) keeps the syntactic presence, the
    # refusal-safe direction; `compile_calendar` gives the verdict on the reference.
    holcal_raw = cal.attrs.get("holcal", "").strip()
    has_holcal = bool(holcal_raw)
    holidays: frozenset[date] | None = None  # None = unresolvable, be conservative
    if has_holcal and catalog is not None:
        hol = catalog.calendars.get(unquote_jil_value(holcal_raw))
        if hol is not None and hol.kind == "standard":
            try:
                holidays = standard_days(hol)
            except (ValueError, CalendarRuleError):
                holidays = None  # unreadable rows: keep syntactic presence
            else:
                has_holcal = bool(holidays)

    def action(key: str, *, shields_something: bool = False) -> Any:
        raw = cal.attrs.get(key, "").strip()
        if not raw:
            return None
        try:
            parsed = _parse_action(cal.name, key, raw)
        except CalendarRuleError:
            return raw.lower()
        if parsed != "s":
            return parsed
        # "s" is the keep-as-is pass-through, but the keys differ in reach.
        # `non_workday: S` is `_dispose`'s else-branch, identical to no action. `holiday: S`
        # on a holiday keeps the day and skips the non_workday branch, so it is distinct only
        # with a holcal to hit and a non_workday action that would alter the day (o filters,
        # n/w/p walk; DL-58). Otherwise the skipped branch was a no-op.
        return parsed if shields_something else None

    non_workday_action = action("non_workday")

    def action_touches_a_holiday() -> bool:
        """Whether the non_workday action would alter any resolved holiday the rules admit.

        That is the domain `holiday: S` shields (`_dispose`). With no holiday action, an
        admitted holcal date is always a non-workday for the non_workday code (DL-244), so
        "o" never drops a candidate and n/w/p always fires on one: reach is whether the rules
        admit any candidate. The holiday set is finite, so candidacy evaluates through the
        engine's own compile. Unresolvable pieces (raw workday, unreadable holcal, an
        uncompilable calendar, an unknown action spelling) answer True, the refusal-safe
        direction. Conservative, not exact: a replacement walk can land on an already-included
        day, so two calendars with equal day sets can read as different (an accepted
        false-refusal corner, never a false carry)."""
        if holidays is None or not isinstance(workday, frozenset) or catalog is None:
            return True
        try:
            compiled = compile_calendar(cal, catalog, semantics)
        except CalendarRuleError:
            return True
        candidates = frozenset(d for d in holidays if compiled._included(d))
        if non_workday_action == "o":
            return False
        if non_workday_action in _REPLACE:
            return bool(candidates)
        return True  # pragma: no cover -- a spelling compile_calendar refuses returned above

    adjust_raw = cal.attrs.get("adjust", "").strip()
    adjust: Any = 0
    if adjust_raw:
        try:
            adjust = int(adjust_raw)
        except ValueError:
            adjust = adjust_raw
    return (
        workday,
        non_workday_action,
        action(
            "holiday",
            shields_something=has_holcal
            and non_workday_action is not None
            and action_touches_a_holiday(),
        ),
        unquote_jil_value(cal.attrs.get("holcal", "").strip()) or None,
        unquote_jil_value(cal.attrs.get("cyccal", "").strip()) or None,
        adjust,
        parsed_rules(),
    )
