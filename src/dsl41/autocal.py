"""Extended-calendar rule interpreter (DL-57).

Pure functions from the opaque CalendarIR/CycleIR carry (DL-36) to day
sets, implementing the SEM-36..39 doc-freeze: candidate generation from
the date-condition keyword grammar (SEM-37), then the disposition
pipeline (SEM-38: category filters, N one-shot / W-P iterating
replacements, uniform blind `adjust`; a specified holiday action governs
holcal dates outright -- Q8a resolved, DL-58). The compiler still never
expands rules -- the runner's Scheduler and preflight are the consumers,
and autocal remains the reference implementation this one is diffed
against once a live instance exists (Q8c/Q8d residue).

Every undocumented COMPOSITION corner is a pinned deterministic default
carrying a `# PENDING: Q8x` marker (DL-59: the scheduler must schedule an
ordinary estate; refusing open corners blocked whole runs). CalendarRuleError
is reserved for what genuinely cannot be interpreted: unknown tokens,
doc-defective tokens (vendor's own text is garbled -- no sane default
exists), missing holcal/cyccal dependencies, and degenerate shapes (a
walk with nowhere to land). Never a silent guess either way.
"""

from __future__ import annotations

import calendar as _calmod
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .ir import CalendarIR, CatalogIR, unquote_jil_value

__all__ = [
    "CalendarRuleError",
    "CompiledCalendar",
    "compile_calendar",
    "standard_days",
    "standard_rows",
]


class CalendarRuleError(ValueError):
    """A calendar definition the interpreter refuses: unknown or
    doc-defective tokens, missing holcal/cyccal dependencies, or a
    degenerate shape (a walk with nowhere to land). The message names
    the calendar. Open COMPOSITION corners never refuse -- they run on
    pinned defaults (DL-59)."""


#: W/P walks and windowed generation share this reach: a year of walking
#: (366) plus the maximum |adjust| (9) plus slack. A walk that travels
#: further has no valid day within a year -- degenerate, refused.
_WALK_CAP = 366
_MARGIN = 400

#: Dormancy scan ceiling for unbounded rule calendars: beyond this the
#: calendar is treated as generating nothing (a leap-day-plus-weekday
#: conjunction can legally gap ~40 years; 60 covers it with slack).
_SCAN_YEARS = 60

_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_CODES2 = ("mo", "tu", "we", "th", "fr", "sa", "su")
_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

#: Doc-defective tokens (SEM-37): WORKDXnn's text contradicts its
#: month-scoped siblings; the CWEK family's definitions are garbled in
#: the vendor's own render. Refused outright, no default, no switch.
_DEFECTIVE_RE = re.compile(r"workdx\d+|cwek(#(\d|l)|m\d|x\d)", re.ASCII)


def standard_rows(cal: CalendarIR) -> dict[date, frozenset[tuple[int, int]]]:
    """A standard calendar's date rows as day -> row (hour, minute) ticks.
    Rows are `mm/dd/yyyy` with an optional HH:MM or HH:MM:SS tail -- the
    observed export sample stamps `00:00:00` (Q9, DL-60); seconds beyond the
    minute truncate (ticks are minute-grained). A bare row's tick is 00:00
    (E11 resolved, DL-58: CA support worked examples -- the row time is the
    vendor's firing time for a job with no start_times/start_mins of its
    own). Raises CalendarRuleError naming the first bad row."""
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
                t = datetime.strptime(tail, "%H:%M:%S" if tail.count(":") == 2 else "%H:%M").time()
                tick = (t.hour, t.minute)
        except ValueError:
            raise CalendarRuleError(
                f"calendar {cal.name!r}: unparseable date row {row!r}"
                " (expected mm/dd/yyyy with an optional HH:MM[:SS] tail)"
            ) from None
        rows.setdefault(day, set()).add(tick)
    return {d: frozenset(ticks) for d, ticks in rows.items()}


def standard_days(cal: CalendarIR) -> frozenset[date]:
    """A standard calendar's date rows as a day set -- the membership view
    of standard_rows() for exclusion and day-eligibility checks."""
    return frozenset(standard_rows(cal))


# ---------------------------------------------------------------- parsing


@dataclass(frozen=True)
class _Ctx:
    """Evaluation context one compiled calendar closes over."""

    workdays: frozenset[int]  # date.weekday() values
    holidays: frozenset[date]
    periods: tuple[tuple[date, date], ...]


_Pred = Callable[[date, _Ctx], bool]


@dataclass(frozen=True)
class _Token:
    base: _Pred  # the token's day set WITHOUT the exclusion flip
    exclusive: bool
    cycle_scoped: bool


# expression AST: ("tok", _Token) | ("not", node) | ("and", l, r) | ("or", l, r)
_Node = tuple


def _err(cal: str, msg: str) -> CalendarRuleError:
    return CalendarRuleError(f"extended calendar {cal!r}: {msg}")


def _ord_in(cal: str, tok: str, text: str, lo: int, hi: int) -> int:
    try:
        n = int(text)
    except ValueError:
        raise _err(cal, f"token {tok!r}: ordinal {text!r} is not a number") from None
    if not lo <= n <= hi:
        raise _err(cal, f"token {tok!r}: ordinal {n} outside {lo}..{hi}")
    return n


def _nth_of(seq: list[date], n: int, *, back: bool) -> date | None:
    """1-based nth (back: 1 = last, per the vendor's 'M' convention)."""
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


def _year_weeks(day: date) -> int:
    return (date(day.year, 12, 31) - date(day.year, 1, 1)).days // 7 + 1


def _period_of(day: date, ctx: _Ctx) -> tuple[int, date, date] | None:
    for idx, (start, end) in enumerate(ctx.periods, start=1):
        if start <= day <= end:
            return idx, start, end
    return None


def _period_chunk(day: date, ctx: _Ctx) -> tuple[int, int] | None:
    """(week-of-period, last-week-of-period): consecutive 7-day chunks from
    each period's first day, the last chunk possibly partial. Q8e RESOLVED
    (DL-58): a Broadcom worked example reads CWEEK#01|CWEEK#02 over a
    quarterly cycle as "the first 14 days in every quarter" -- chunks
    anchor to the period start, not to calendar weeks. ([?] the ragged
    last chunk is the arithmetic consequence, not separately worked.)"""
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
    """Decode one `(#|m|x)(\\d+|l)`-shaped ordinal suffix (SEM-37): `letter`
    is the family's back/exclusion marker character, `digits` its
    ordinal-or-`l` body. `l` ("last", extended uniformly across families by
    Q9/DL-60) maps to n=1 with back forced True -- the ONE encoding this
    collapses two into. `_nth_of(seq, n, back=back)` then reads the last
    element the same way an ordinary from-end count would, so no family
    needs its own `seq[-1]` special case. `m` also forces back (the
    vendor's own from-end marker)."""
    last = digits == "l"
    n = 1 if last else _ord_in(cal, raw, digits, lo, hi)
    back = letter == "m" or last
    return n, back


def _parse_token(cal: str, raw: str) -> _Token:
    """One keyword to its day predicate. Case-insensitive (SEM-37 [V]);
    unknown and doc-defective tokens refuse loudly."""
    tok = raw.lower()
    if _DEFECTIVE_RE.fullmatch(tok):
        raise _err(cal, f"token {raw!r} is doc-defective (SEM-37); refused pending Q8/live")

    # the X- prefix exclusion convention; infix X-forms match per family.
    # No documented token starts with a non-exclusion 'x'.
    exclusive = tok.startswith("x")
    body = tok[1:] if exclusive else tok

    def mk(base: _Pred, *, cycle: bool = False, excl: bool = exclusive) -> _Token:
        return _Token(base=base, exclusive=excl, cycle_scoped=cycle)

    def workday_seq(day: date, ctx: _Ctx) -> list[date]:
        return _month_matching(day, lambda d: d.weekday() in ctx.workdays)

    if body == "daily":
        return mk(lambda d, c: True)
    if body == "workdays":
        return mk(lambda d, c: d.weekday() in c.workdays)
    if body == "weekdays":
        # [V] auto-subtracts the holiday calendar (SEM-37 quote)
        return mk(lambda d, c: d.weekday() < 5 and d not in c.holidays)
    if body == "fomwork":
        return mk(lambda d, c: bool(s := workday_seq(d, c)) and d == s[0])
    if body == "eomwork":
        return mk(lambda d, c: bool(s := workday_seq(d, c)) and d == s[-1])
    if body == "fomweek":
        return mk(
            lambda d, c: bool(s := _month_matching(d, lambda x: x.weekday() < 5)) and d == s[0]
        )
    if body == "eomweek":
        return mk(
            lambda d, c: bool(s := _month_matching(d, lambda x: x.weekday() < 5)) and d == s[-1]
        )
    if body == "fom":
        return mk(lambda d, c: d.day == 1)
    if body == "eom":
        return mk(lambda d, c: d.day == _month_days(d))
    if body == "cycle":
        return mk(lambda d, c: _period_of(d, c) is not None, cycle=True)

    # ordinal families accept a literal L count = "last" (from-end 1): the
    # observed export sample uses WORKD#L, and the CWRK/Cddd families already
    # documented the form -- extended uniformly (Q9, DL-60)
    if m := re.fullmatch(r"workd([#m])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=31)
        return mk(lambda d, c: _nth_of(workday_seq(d, c), n, back=back) == d)
    if m := re.fullmatch(r"weekd([#mx])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=7)
        pred: _Pred = (
            (lambda d, c: (d - _week_start(d, _jan1_anchor(d))).days == 7 - n)
            if back
            else (lambda d, c: (d - _week_start(d, _jan1_anchor(d))).days == n - 1)
        )
        return mk(pred, excl=exclusive or m.group(1) == "x")
    if m := re.fullmatch(r"wekr(mon|tue|wed|thu|fri|sat|sun)([#mx])(\d+|l)", body):
        anchor = _DAY_NAMES.index(m.group(1))
        n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=7)
        pred = (
            (lambda d, c: (d - _week_start(d, anchor)).days == 7 - n)
            if back
            else (lambda d, c: (d - _week_start(d, anchor)).days == n - 1)
        )
        return mk(pred, excl=exclusive or m.group(2) == "x")
    if m := re.fullmatch(r"week#([eo])", body):
        parity = 0 if m.group(1) == "e" else 1
        return mk(lambda d, c: _week_number(d) % 2 == parity)
    if m := re.fullmatch(r"week([#mx])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=53)
        pred = (
            (lambda d, c: _week_number(d) == _year_weeks(d) - n + 1)
            if back
            else (lambda d, c: _week_number(d) == n)
        )
        return mk(pred, excl=exclusive or m.group(1) == "x")
    if m := re.fullmatch(r"mnthd([#mx])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=31)
        pred = (lambda d, c: d.day == _month_days(d) - n + 1) if back else (lambda d, c: d.day == n)
        return mk(pred, excl=exclusive or m.group(1) == "x")
    if body in _MONTH_NAMES:
        month = _MONTH_NAMES.index(body) + 1
        return mk(lambda d, c: d.month == month)
    if m := re.fullmatch(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)([#m])(\d+|l)", body):
        month = _MONTH_NAMES.index(m.group(1)) + 1
        n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=31)
        pred = (
            (lambda d, c: d.month == month and d.day == _month_days(d) - n + 1)
            if back
            else (lambda d, c: d.month == month and d.day == n)
        )
        return mk(pred)
    if body in _DAY_NAMES:
        wd = _DAY_NAMES.index(body)
        return mk(lambda d, c: d.weekday() == wd)
    if m := re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun)([#m])(\d|l)", body):
        wd = _DAY_NAMES.index(m.group(1))
        n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=5)
        return mk(
            lambda d, c: (
                d.weekday() == wd
                and _nth_of(_month_matching(d, lambda x: x.weekday() == wd), n, back=back) == d
            )
        )
    if m := re.fullmatch(r"cycl([#mx])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=365)

        def cycl(d: date, c: _Ctx) -> bool:
            hit = _period_of(d, c)
            if hit is None:
                return False
            _, start, end = hit
            idx = (end - d).days + 1 if back else (d - start).days + 1
            return idx == n

        return mk(cycl, cycle=True, excl=exclusive or m.group(1) == "x")
    if m := re.fullmatch(r"cycp#(\d+)", body):
        n = _ord_in(cal, raw, m.group(1), 1, 30)
        return mk(lambda d, c: (h := _period_of(d, c)) is not None and h[0] == n, cycle=True)
    if m := re.fullmatch(r"cweek#([eol])", body):
        which = m.group(1)

        def cweek_l(d: date, c: _Ctx) -> bool:
            chunk = _period_chunk(d, c)
            if chunk is None:
                return False
            wk, last = chunk
            return wk == last if which == "l" else wk % 2 == (0 if which == "e" else 1)

        return mk(cweek_l, cycle=True)
    if m := re.fullmatch(r"cweek([#mx])(\d+)", body):
        # no literal 'l' here -- `cweek#([eol])` above owns that spelling for
        # this family; _ordinal's `last` branch is simply never taken
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=53)

        def cweek(d: date, c: _Ctx) -> bool:
            chunk = _period_chunk(d, c)
            if chunk is None:
                return False
            wk, last = chunk
            return wk == (last - n + 1 if back else n)

        return mk(cweek, cycle=True, excl=exclusive or m.group(1) == "x")
    if m := re.fullmatch(r"cwrk([#mx])(\d+|l)", body):
        n, back = _ordinal(cal, raw, m.group(1), m.group(2), lo=1, hi=365)

        def cwrk(d: date, c: _Ctx) -> bool:
            seq = _period_matching(d, c, lambda x: x.weekday() in c.workdays)
            if seq is None:
                return False
            return _nth_of(seq, n, back=back) == d

        return mk(cwrk, cycle=True, excl=exclusive or m.group(1) == "x")
    if m := re.fullmatch(r"c(mon|tue|wed|thu|fri|sat|sun)([#m])(\d+|l)", body):
        wd = _DAY_NAMES.index(m.group(1))
        n, back = _ordinal(cal, raw, m.group(2), m.group(3), lo=1, hi=53)

        def cddd(d: date, c: _Ctx) -> bool:
            if d.weekday() != wd:
                return False
            seq = _period_matching(d, c, lambda x: x.weekday() == wd)
            if seq is None:
                return False
            return _nth_of(seq, n, back=back) == d

        return mk(cddd, cycle=True)

    raise _err(cal, f"unknown date-condition token {raw!r} (SEM-37 inventory)")


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9#]*")


def _tokenize(cal: str, rule: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(rule):
        ch = rule[i]
        if ch.isspace():
            i += 1
        elif ch in "&|(){}":
            # {} group exactly like () -- the observed export sample writes
            # braces ({MNTHD#7} | {MNTHD#21}) where TechDocs shows parens;
            # accepted as synonyms (Q9, DL-60)
            out.append("(" if ch == "{" else ")" if ch == "}" else ch)
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
        if tok.lower() == "not":
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
            if tok in ("&", "|"):
                op: Literal["and", "or"] = "and" if tok == "&" else "or"
            elif tok.lower() in ("and", "or"):
                op = "and" if tok.lower() == "and" else "or"
            else:
                raise _err(cal, f"condition {rule!r}: expected an operator, got {tok!r}")
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
    """A top-level rule that is nothing but an exclusion subtracts its
    base set from the union of inclusive rules (PENDING: Q8d). Returns
    the node whose *inclusive* reading is the set to subtract."""
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


@dataclass(frozen=True)
class CompiledCalendar:
    """An extended calendar ready to generate days. Pure and immutable;
    `bound` is the last day the calendar can possibly produce (None =
    unbounded rules)."""

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
        Generation windows over [lo-margin, hi+margin] so replacements
        that cross the edge are seen from both sides."""
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
        """Earliest generated day at or after `day`, scanning to `bound`
        or the dormancy ceiling. None = the calendar is exhausted/empty."""
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
        O-codes are category-restrictive filters; N/W/P replace category
        members with a target day, and a replacement result is FINAL --
        never re-processed by the other category (the holiday-N wording:
        'applies even if the next day is a holiday or non-workday').
        Q8a RESOLVED (DL-58): a specified holiday action governs every
        holcal date OUTRIGHT -- the vendor treats holcal dates per the
        non-workday action only 'when you do not specify an action at the
        Holiday Action prompt', so the non_workday code (filter or
        replacement) never sees a holiday once a holiday action exists.
        ([?] Q8a residue: whether a replacement target re-enters the other
        stage stays unverified -- kept single-shot per the holiday-N
        wording.)"""
        out: set[date] = set()
        for day in candidates:
            if self.holiday is not None and day in self.ctx.holidays:
                if self.holiday in _REPLACE:
                    out.add(self._replace("holiday", day))
                else:  # "o": restrict-to-holidays keeps it; "s" keeps as-is
                    out.add(day)
                continue
            if self.holiday == "o":
                continue  # not a holiday: the restrict-to-holidays filter drops it
            if self.non_workday == "o" and day.weekday() in self.ctx.workdays:
                continue
            if self.non_workday in _REPLACE and day.weekday() not in self.ctx.workdays:
                out.add(self._replace("non_workday", day))
            else:
                out.add(day)
        return out

    def _replace(self, category: str, day: date) -> date:
        """One replacement code's target for one excluded date."""
        code = self.holiday if category == "holiday" else self.non_workday
        if code is None or code not in _REPLACE:
            raise _err(self.name, f"internal: {category} code {code!r} is not a replacement")
        if code == "n" and category == "holiday":
            # [V] one-shot: 'Excludes the holiday and includes the next
            # day. This applies even if the next day is a holiday or
            # non-workday.'
            return day + timedelta(days=1)
        if code == "n":
            # [V] 'include the next workday that also meets all other
            # criteria' -- pinned as the next non-holiday workday.
            # PENDING: Q8c -- the date-conditions are NOT re-checked
            return self._walk(day, 1, holiday_free=True, code=code, category=category)
        step = 1 if code == "w" else -1
        # holiday W/P demand a NON-HOLIDAY workday ([V] worked examples);
        # non_workday W/P say only 'work day'.
        # PENDING: Q8c -- no worked example; holiday-ness of the target
        # deliberately not re-checked
        return self._walk(
            day, step, holiday_free=category == "holiday", code=code, category=category
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


def _parse_workday(cal: str, value: str) -> frozenset[int]:
    """All vendor serializations (SEM-36): positional `{X|.}` x7
    (Monday-first), the comma list of two-letter day codes, and the
    observed `all` = every day is a workday (Q9, DL-60)."""
    text = value.strip().lower()
    if text == "all":
        return frozenset(range(7))
    if re.fullmatch(r"[x.]{7}", text):
        return frozenset(i for i, ch in enumerate(text) if ch == "x")
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


def compile_calendar(cal: CalendarIR, catalog: CatalogIR) -> CompiledCalendar:
    """Parse and validate one extended calendar against the loaded set.
    Raises CalendarRuleError on anything the SEM-36..39 freeze genuinely
    cannot interpret -- unknown tokens, defective tokens, missing
    dependencies, degenerate walks. Open composition corners compile on
    pinned defaults instead (Q8b/Q8d, DL-59)."""
    if cal.kind != "extended":
        raise CalendarRuleError(f"calendar {cal.name!r} is standard; use standard_days()")
    if "condition" in cal.attrs:
        raise _err(cal.name, "condition in attrs (hand-built IR?); lowering owns the lane")

    known = {"description", "workday", "non_workday", "holiday", "holcal", "cyccal", "adjust"}
    unknown = set(cal.attrs) - known
    if unknown:
        raise _err(cal.name, f"unrecognized attribute(s) {sorted(unknown)!r} (SEM-36)")

    workdays = (
        _parse_workday(cal.name, cal.attrs["workday"])
        if cal.attrs.get("workday", "").strip()
        else frozenset(range(5))
    )
    non_workday = _parse_action(cal.name, "non_workday", cal.attrs.get("non_workday", ""))
    holiday = _parse_action(cal.name, "holiday", cal.attrs.get("holiday", ""))

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
        # holiday S is a pass-through consuming no holiday set -- the
        # observed export sample carries `holiday: S` with holcal empty
        # (Q9, DL-60), against the 12.1 prompt-flow wording; O/N/W/P
        # genuinely need the set (SEM-36)
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
    # undocumented. Pinned default (DL-59, deterministic over fail-closed):
    # the SEM-38 pipeline order as-is -- disposition replaces first, then
    # the uniform blind adjust shifts every survivor (replace-then-shift).
    # The runbook's probe pair discriminates vendor truth if access appears.

    rules: list[str] = []
    for line in cal.conditions:
        rules.extend(part.strip() for part in line.split(",") if part.strip())
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
        # `xtue|xwed`) is neither a recognized exclusion form nor a likely
        # authorial intent. Pinned default (DL-59, deterministic over
        # fail-closed): literal boolean evaluation as an include -- the
        # same complement-in-AND algebra mixed rules already use -- even
        # though that makes `xtue|xwed` near-universal.
        include.append(node)
        if not all(t.cycle_scoped for t in _walk_tokens(node)):
            cycle_scoped_only = False
    if not include:
        # DAILY is the documented default; exclusion-only rule lists
        # subtract from it rather than from nothing (PENDING: Q8d)
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
        # a named but unused cycle must still resolve, same as L018's own
        # check (rule_l018, lint.py) -- both now key the lookup through
        # ir.unquote_jil_value, so a quoted name resolves identically here
        # and there (DL-178q; this was the divergent lenient _unquote before)
        periods = _parse_periods(cal.name, unquote_jil_value(cyccal_name), catalog)

    if (non_workday in ("w", "p") or holiday in ("w", "p")) and not workdays:
        raise _err(cal.name, "a W/P action with an all-non-workday mask has nowhere to walk")

    bound: date | None = None
    if include and cycle_scoped_only and periods:
        bound = max(end for _, end in periods) + timedelta(days=_MARGIN)

    return CompiledCalendar(
        name=cal.name,
        ctx=_Ctx(workdays=workdays, holidays=holidays, periods=periods),
        include=tuple(include),
        exclude=tuple(exclude),
        non_workday=non_workday,
        holiday=holiday,
        adjust=adjust,
        bound=bound,
    )


def semantic_key(cal: CalendarIR, catalog: CatalogIR | None = None) -> tuple[Any, ...]:
    """The extended-calendar surface this rule engine READS, canonicalized
    with the SAME parsers that evaluate it -- for the DL-131 classifier,
    whose calendar node must call two spellings of one rule set one value.

    One authority, exported: the classifier comparing raw attribute text
    would refuse a live boundary over `MON|TUE` respelled `MON | TUE`, a
    reordered day list, a re-cased action, or `adjust: 00` -- all of which
    this engine derives identical dates from. Tolerant on purpose: a piece
    these parsers refuse keeps its raw (collapsed, lowered) spelling as its
    key -- the LOUD refusal is `compile_calendar`'s, at preflight, and a
    classifier must not crash where the gate is elsewhere. Rules compare as
    a SET of canonical token tuples (the fold is any-of and tokens
    case-fold); `{}` reads as `()` and the word operators as their symbols,
    exactly as `_tokenize`/`_parse_rule` do."""

    def canonical_token(token: str) -> str:
        if token in "&|()":
            return token
        lowered = token.lower()
        return {"and": "&", "or": "|"}.get(lowered, lowered)

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
            # `compile_calendar`'s own fallback: no rules means the DAILY
            # include, so an omitted rule list and an explicit
            # `condition: daily` are one calendar
            rules.add(("daily",))
        return tuple(sorted(rules))

    workday_raw = cal.attrs.get("workday", "").strip()
    # `compile_calendar`'s own default: an omitted workday IS Monday-Friday,
    # so spelling the default out is not a change
    workday: Any = frozenset(range(5))
    if workday_raw:
        try:
            workday = _parse_workday(cal.name, workday_raw)
        except CalendarRuleError:
            workday = workday_raw.lower()

    # shielding REACH resolves through the holiday set itself, not the
    # spelling of the reference: a holcal naming an empty calendar gives
    # `holiday: S` nothing to keep, and the compiled dates are identical
    # with or without it. An unresolvable reference (no catalog handed in,
    # a missing or extended holcal) keeps the syntactic presence -- the
    # refusal-safe direction; the loud verdict on the reference itself is
    # `compile_calendar`'s.
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
        # "s" is the keep-as-is pass-through, but the two keys differ in
        # REACH. `non_workday: S` is always `_dispose`'s else-branch --
        # identical to no action. `holiday: S` on a holiday KEEPS the day
        # and `continue`s PAST the non_workday branch, so it is distinct
        # exactly when there is something to shield FROM: a holcal to hit
        # AND a non_workday action that would alter the day (o filters,
        # n/w/p walk -- DL-58's shielding family). With either absent, the
        # skipped branch was a no-op and S is no action at all.
        return parsed if shields_something else None

    non_workday_action = action("non_workday")

    def action_touches_a_holiday() -> bool:
        """Whether the non_workday action would ALTER any resolved holiday
        that the rule set ADMITS -- the exact domain `holiday: S`'s skip
        shields (`_dispose`): "o" drops days IN the workday set, n/w/p walk
        days OUTSIDE it, and either only ever sees a day the include/
        exclude predicate produced as a candidate. The holiday set is
        finite, so candidacy evaluates exactly, through the engine's own
        compile. Unresolvable pieces (raw workday, unreadable holcal, an
        uncompilable calendar, an unknown action spelling) answer True --
        the refusal-safe direction; a replace-walk that happens to land on
        an already-included day is an accepted false-refusal corner, never
        a false carry."""
        if holidays is None or not isinstance(workday, frozenset) or catalog is None:
            return True
        try:
            compiled = compile_calendar(cal, catalog)
        except CalendarRuleError:
            return True
        candidates = frozenset(d for d in holidays if compiled._included(d))
        if non_workday_action == "o":
            return any(d.weekday() in workday for d in candidates)
        if non_workday_action in _REPLACE:
            return any(d.weekday() not in workday for d in candidates)
        return True  # an unparsed action spelling: assume reach

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
