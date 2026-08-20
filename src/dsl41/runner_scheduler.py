"""Runner scheduler (ss5): the calendar the oracle deliberately lacks, and
the SEM-35 timezone ladder that turns its ticks into UTC instants.

Split out of runner.py by DL-74, with the paragraph it owns, verbatim.

Phase 11c (ss5, ss8, ss10; DL-45 pins the decisions):

- Scheduler (ss5): the calendar the oracle deliberately lacks. Per
  date_conditions job it computes the next occurrence from days_of_week +
  start_times/start_mins (absent days_of_week = every day; per-job
  timezone, else the run-level base zone, else UTC -- both defaults
  PENDING: E10 -- with names resolved per SEM-35: zoneinfo, then the
  --timezone-map ujo_timezones listing, then the DL-62 unique-city
  default), or -- run_calendar with neither -- from the
  calendar rows' own times (E11 resolved, DL-58), and hands the engine
  STARTJOB events at the tick, timestamped at the tick and journaled like
  any input (source=scheduler). It fires UNCONDITIONALLY: SEM-32
  arm-and-wait (Q3 resolved, DL-58) and SEM-33 run_window stay
  oracle-side. While the engine is up a late tick still fires (event
  stamped at the tick); across downtime missed ticks are dropped AND
  journaled at resume, never fired late (PENDING: E9).
"""

from __future__ import annotations

import contextlib
import functools
import re

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo, available_timezones

from dsl41.autocal import (
    CalendarRuleError,
    CompiledCalendar,
    compile_calendar,
    standard_rows,
)
from dsl41.ir import CatalogIR, ScheduleBlock
from dsl41.oracle_state import Event
from dsl41.runner_clock import EngineError


# ------------------------------------------------------------------ scheduler (ss5)

#: Python date.weekday() (Monday=0) -> JIL day token (ir._DAY_TOKENS)
_DAY_CODES = ("mo", "tu", "we", "th", "fr", "sa", "su")

#: Occurrence-scan ceiling for UNBOUNDED extended-calendar rules (DL-57);
#: mirrors autocal's dormancy ceiling.
_EXTENDED_SCAN_DAYS = 60 * 366


class _CalCache:
    """Windowed membership over a CompiledCalendar (DL-57). Generation is
    window-priced, so per-day queries fill 366-day blocks once; a
    generation-time refusal (Q8a) surfaces as EngineError."""

    def __init__(self, compiled: CompiledCalendar) -> None:
        self.compiled = compiled
        self._blocks: dict[int, frozenset[date]] = {}

    def contains(self, day: date) -> bool:
        block = day.toordinal() // 366
        if block not in self._blocks:
            lo = date.fromordinal(block * 366)
            try:
                self._blocks[block] = self.compiled.days_between(lo, lo + timedelta(days=365))
            except CalendarRuleError as exc:
                raise EngineError(str(exc)) from exc
        return day in self._blocks[block]


@dataclass(frozen=True)
class _SchedulePlan:
    """One job's compiled trigger: eligible day tokens OR an explicit
    run_calendar day source (SEM-31 XOR) -- a standard date set or an
    extended-calendar generator (DL-57) -- an exclude_calendar source to
    subtract, sorted (hour, minute) ticks per eligible day, and the resolved
    zone (None = the engine's naive UTC basis directly). `last_date` bounds
    the occurrence scan past the last explicit date (DL-56); an unbounded
    extended run source scans to the autocal dormancy ceiling instead."""

    days: frozenset[str]
    times: tuple[tuple[int, int], ...]
    tz: tzinfo | None
    run_dates: frozenset[date] | None = None
    exclude_dates: frozenset[date] = frozenset()
    run_gen: _CalCache | None = None
    exclude_gen: _CalCache | None = None
    last_date: date | None = None
    #: E11 (DL-58): a run_calendar job with no start_times/start_mins fires
    #: at each row's own time (bare rows at 00:00); set only in that shape,
    #: where it REPLACES `times` per eligible day.
    row_times: dict[date, frozenset[tuple[int, int]]] | None = None

    def day_eligible(self, day: date) -> bool:
        if day in self.exclude_dates:  # SEM-31: subtracts from whichever is active
            return False
        if self.exclude_gen is not None and self.exclude_gen.contains(day):
            return False
        if self.run_dates is not None:
            return day in self.run_dates
        if self.run_gen is not None:
            return self.run_gen.contains(day)
        return _DAY_CODES[day.weekday()] in self.days

    def utc_ticks_on(self, day: date) -> list[datetime]:
        """This local day's ticks as naive-UTC instants (the engine basis)."""
        times = self.times if self.row_times is None else self.row_times.get(day, frozenset())
        ticks = []
        for hour, minute in times:
            naive_local = datetime(day.year, day.month, day.day, hour, minute)
            ticks.append(
                naive_local.replace(tzinfo=self.tz).astimezone(UTC).replace(tzinfo=None)
                if self.tz
                else naive_local
            )
        return ticks


def _scheduler_calendar(
    catalog: CatalogIR, job: str, role: str, ref: str
) -> dict[date, frozenset[tuple[int, int]]] | CompiledCalendar:
    """Resolve a run_calendar/exclude_calendar reference for the Scheduler:
    a standard calendar's day -> row-tick map (row times fire E11 jobs,
    DL-58; exclusion consumes only the day keys), or an extended calendar
    compiled by the autocal interpreter (DL-57). Preflight ERROR territory
    (ss8) -- a hand-built catalog that bypassed it refuses comprehensibly
    here, never guesses (DL-56)."""
    cal = catalog.calendars.get(ref)
    if cal is None:
        raise EngineError(f"{job}: {role} {ref!r} has no calendar definition in the loaded set")
    try:
        if cal.kind == "extended":
            return compile_calendar(cal, catalog)
        return standard_rows(cal)
    except CalendarRuleError as exc:
        raise EngineError(f"{job}: {exc}") from exc


# ------------------------------------------------------------- timezone names
#
# SEM-35 name resolution (TechDocs 12.1, `timezone` attribute + the
# autotimezone command): a JIL `timezone:` value is matched against the OS
# zone database FIRST; only if that misses is the instance's ujo_timezones
# table read -- a vendor-shipped, admin-editable map whose City/Alias
# entries chain ("up to five times") down to a Zone the OS recognizes.
# Values are not case-sensitive. dsl41's port: zoneinfo is the OS database;
# `--timezone-map` (the `autotimezone -l` listing, or bare `name zone`
# pairs) is the table. Without a map, a city name resolves through a
# documented deterministic default (DL-62): the UNIQUE zoneinfo zone whose
# final path component matches (Zurich -> Europe/Zurich), surfaced as a
# preflight WARN -- a supplied listing is complete estate truth, so the
# default is off when a map is given. A POSIX fixed-offset form (`GMT+5`,
# `IST-5:30`) resolves per the POSIX sign convention (positive = WEST of
# GMT); POSIX strings WITH dst rules stay unresolvable -- modelling vendor
# DST rules approximately would silently shift ticks.

_TZ_CHAIN_LIMIT = 5  # vendor: "the ujo_timezones table is read up to five times"

_POSIX_FIXED = re.compile(r"^([A-Za-z]{3,})([+-]?\d{1,2})(?::(\d{2})(?::(\d{2}))?)?$")


class ResolvedTz(NamedTuple):
    tz: tzinfo
    zone: str  # the zoneinfo key / POSIX token it landed on
    how: Literal["os", "map", "city", "posix"]


def _tz_fold(name: str) -> str:
    """Vendor names are case-insensitive; fold -/_ too (both are valid JIL
    characters, zoneinfo uses each: Port-au-Prince vs New_York)."""
    return name.casefold().replace("-", "_")


@functools.lru_cache(maxsize=1)
def _zone_tables() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """(folded full name -> canonical key, folded final path component ->
    candidate keys) over the zoneinfo database, built once."""
    full: dict[str, str] = {}
    leaf: dict[str, list[str]] = {}
    for zone in sorted(available_timezones()):
        full.setdefault(_tz_fold(zone), zone)
        leaf.setdefault(_tz_fold(zone.rsplit("/", 1)[-1]), []).append(zone)
    return full, {component: tuple(zones) for component, zones in leaf.items()}


def _city_candidates(name: str) -> tuple[str, ...]:
    """Zoneinfo keys whose final path component is `name` (folded)."""
    return _zone_tables()[1].get(_tz_fold(name), ())


def _os_zone(token: str) -> ResolvedTz | None:
    """One vendor "recognized by the operating system" lookup: zoneinfo
    exact, zoneinfo case/-/_-insensitive, then a POSIX fixed offset."""
    with contextlib.suppress(KeyError, ValueError, OSError):
        return ResolvedTz(ZoneInfo(token), token, "os")
    canonical = _zone_tables()[0].get(_tz_fold(token))
    if canonical is not None:
        return ResolvedTz(ZoneInfo(canonical), canonical, "os")
    if (match := _POSIX_FIXED.match(token)) is not None:
        _, hours, minutes, seconds = match.groups()
        offset = timedelta(
            hours=abs(int(hours)), minutes=int(minutes or 0), seconds=int(seconds or 0)
        )
        if int(minutes or 0) < 60 and int(seconds or 0) < 60 and offset < timedelta(hours=24):
            west = not hours.startswith("-")  # POSIX: unsigned/+ = west of GMT
            return ResolvedTz(timezone(-offset if west else offset, token), token, "posix")
    return None


def parse_timezone_map(text: str) -> dict[str, str]:
    """An `autotimezone -l`/-q listing (`Entry Type Zone` rows) or bare
    `name zone` pairs -> folded alias map. Header, ruler, and blank lines
    skip; any other unparseable line raises ValueError (no silent loss)."""
    aliases: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or not set(line) - set("- "):
            continue
        fields = line.split()
        if [f.casefold() for f in fields] == ["entry", "type", "zone"]:
            continue
        if len(fields) == 3 and fields[1].casefold() in {"zone", "alias", "city"}:
            name, _, zone = fields
        elif len(fields) == 2:
            name, zone = fields
        else:
            raise ValueError(
                f"line {lineno}: expected an autotimezone 'entry type zone' row"
                f" or a 'name zone' pair, got {raw!r}"
            )
        aliases[_tz_fold(name)] = zone
    return aliases


def resolve_timezone(name: str, aliases: Mapping[str, str] | None = None) -> ResolvedTz | None:
    """The SEM-35 ladder (module comment above): OS lookup, then the alias
    map chained <=5 hops with an OS lookup per hop, then -- only with NO
    map -- the unique-city default. None = genuinely unresolvable."""
    current = name
    seen: set[str] = set()
    for hop in range(_TZ_CHAIN_LIMIT + 1):
        if (resolved := _os_zone(current)) is not None:
            if current == name:
                return resolved
            return ResolvedTz(resolved.tz, resolved.zone, "map")
        folded = _tz_fold(current)
        if aliases is None or hop == _TZ_CHAIN_LIMIT or folded in seen or folded not in aliases:
            break
        seen.add(folded)
        current = aliases[folded]
    if aliases is None and len(candidates := _city_candidates(name)) == 1:
        return ResolvedTz(ZoneInfo(candidates[0]), candidates[0], "city")
    return None


def _scheduler_tz(name: str, owner: str, aliases: Mapping[str, str] | None) -> tzinfo:
    """Resolve or refuse comprehensibly -- preflight gates this first (ss8);
    the EngineError is the backstop for direct Scheduler construction."""
    resolved = resolve_timezone(name, aliases)
    if resolved is None:
        raise EngineError(
            f"{owner}: timezone {name!r} is not resolvable (SEM-35: zoneinfo,"
            " the --timezone-map table, or a POSIX fixed offset)"
        )
    return resolved.tz


class Scheduler:
    """ss5: the calendar the oracle deliberately lacks. Computes per-job next
    occurrences from the ScheduleBlock and yields STARTJOB events at the
    tick; it fires unconditionally (SEM-32 arm-and-wait and SEM-33
    run_window stay oracle-side, exactly as in simulation). Ticks are naive-UTC instants
    (the engine's time basis): per-job `timezone` -- else `default_tz`, else
    UTC -- is applied via resolve_timezone's SEM-35 ladder (zoneinfo, the
    `tz_aliases` ujo_timezones map, the DL-62 city default, POSIX fixed
    offsets), so rehearse under the virtual clock exercises real calendar
    arithmetic (ss5).

    Pinned interpretation defaults (PENDING: E10): absent days_of_week means
    every day; jobs without `timezone` read their times in `default_tz`
    (run-level --timezone), defaulting to UTC -- vendor uses the server's
    zone. DST corners follow PEP 495 fold=0: a fall-back ambiguous time is
    its first occurrence, a spring-forward nonexistent time maps past the
    gap. Schedule blocks with neither start_times nor start_mins trigger
    nothing (run_window/SLA are gates/alarms, not triggers).

    Standard calendars are honored (DL-56): `run_calendar` day membership
    (SEM-31: XOR days_of_week) minus `exclude_calendar` days, both evaluated
    on the job's LOCAL day. Ticks are start_times/start_mins when present;
    a run_calendar job with NEITHER fires at each calendar row's own HH:MM
    tail, bare rows at 00:00 (E11 resolved, DL-58: CA support worked
    examples -- job-level times override row times; an extended calendar
    has no rows, so that shape ticks at 00:00). An exhausted calendar
    leaves the job dormant: no next occurrence, never an error. Extended
    calendars compile through the autocal interpreter (DL-57) and scan
    windowed, out to the dormancy ceiling for unbounded rules."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        start: datetime,
        default_tz: str | None = None,
        tz_aliases: Mapping[str, str] | None = None,
    ) -> None:
        base_tz = _scheduler_tz(default_tz, "--timezone", tz_aliases) if default_tz else None
        # what this scheduler was WIRED with, kept READ-ONLY (properties
        # below) so the runtime profile the period pins is derived from the
        # compile-time inputs the plans actually use -- a mutable attribute
        # here would let a post-compile edit pin one timezone while `_plans`
        # execute under another (period-model ss2.1, DL-130)
        self._default_tz = default_tz
        self._tz_aliases: dict[str, str] = dict(tz_aliases or {})
        # the catalog these plans were COMPILED from, pinned as its v2 hash
        # at compile time -- an object reference could be mutated after the
        # plans were built, and the gate would then bless stale plans under
        # the changed catalog's own identity
        from dsl41.period import catalog_hash_v2

        self._catalog_hash = catalog_hash_v2(catalog)
        self._plans: dict[str, _SchedulePlan] = {}
        for name, job in catalog.jobs.items():
            sched = job.schedule
            if sched is None:
                continue
            own_ticks = bool(sched.start_times or sched.start_mins)
            if not own_ticks and sched.run_calendar is None:
                continue  # run_window/SLA are gates/alarms, not triggers
            run_dates: frozenset[date] | None = None
            run_gen: _CalCache | None = None
            row_times: dict[date, frozenset[tuple[int, int]]] | None = None
            if sched.run_calendar is not None:
                source = _scheduler_calendar(catalog, name, "run_calendar", sched.run_calendar)
                if isinstance(source, CompiledCalendar):
                    run_gen = _CalCache(source)
                else:
                    run_dates = frozenset(source)
                    if not own_ticks:
                        # E11 (DL-58): no job-level times -- fire at each
                        # row's own time; job start_times override row times
                        row_times = source
            exclude_dates: frozenset[date] = frozenset()
            exclude_gen: _CalCache | None = None
            if sched.exclude_calendar is not None:
                source = _scheduler_calendar(
                    catalog, name, "exclude_calendar", sched.exclude_calendar
                )
                if isinstance(source, CompiledCalendar):
                    exclude_gen = _CalCache(source)
                else:
                    exclude_dates = frozenset(source)
            if sched.days_of_week is not None and not sched.days_of_week:
                # lowering rejects an empty list; a hand-built IR carrying one
                # would exhaust _occurrence's scan -- refuse comprehensibly
                raise EngineError(f"{name}: days_of_week is empty; nothing to schedule")
            explicit = (run_dates or frozenset()) | exclude_dates
            last_date = max(explicit) if explicit else None
            if run_gen is not None and run_gen.compiled.bound is not None:
                # cycle-bound extended calendars scan like explicit dates
                last_date = max(d for d in (last_date, run_gen.compiled.bound) if d is not None)
            self._plans[name] = _SchedulePlan(
                days=frozenset(
                    _DAY_CODES
                    if (sched.days_of_week is None or "all" in sched.days_of_week)
                    else sched.days_of_week
                ),
                # E11 (DL-58): a generated (extended) day has no row time --
                # neither the calendar nor the job supplies one -> 00:00
                times=self._ticks(sched) if own_ticks else ((0, 0),),
                tz=_scheduler_tz(sched.timezone, name, tz_aliases) if sched.timezone else base_tz,
                run_dates=run_dates,
                exclude_dates=exclude_dates,
                run_gen=run_gen,
                exclude_gen=exclude_gen,
                last_date=last_date,
                row_times=row_times,
            )
        self._next: dict[str, datetime] = {}
        self.reset(start)

    @property
    def default_tz(self) -> str | None:
        return self._default_tz

    @property
    def tz_aliases(self) -> dict[str, str]:
        return dict(self._tz_aliases)  # a copy: mutating it moves nothing here

    @property
    def catalog_hash(self) -> str:
        return self._catalog_hash

    @staticmethod
    def _ticks(sched: ScheduleBlock) -> tuple[tuple[int, int], ...]:
        if sched.start_times:
            return tuple(sorted((t.hour, t.minute) for t in sched.start_times))
        return tuple(sorted((h, m) for h in range(24) for m in sched.start_mins or []))

    def reset(self, start: datetime, *, inclusive: bool = True) -> None:
        """Re-anchor every job's next tick at or (inclusive=False) strictly
        after `start`. Resume uses the exclusive form anchored at the last
        journal instant: a tick exactly there was already fed by replay.
        Jobs whose calendar is already exhausted get no entry (DL-56)."""
        self._next = {}
        for job, plan in self._plans.items():
            occ = self._occurrence(plan, start, inclusive=inclusive)
            if occ is not None:
                self._next[job] = occ

    def next_occurrence(self) -> datetime | None:
        """Earliest pending tick across all jobs (naive UTC), or None."""
        return min(self._next.values(), default=None)

    def upcoming(self) -> list[tuple[datetime, str]]:
        """Read-only snapshot of every scheduled job's NEXT tick, due-ordered
        (the ss10 `timers` verb; the list-timers analog, DL-65). One entry
        per job -- the estate's near future, not the full tick expansion."""
        return sorted((tick, job) for job, tick in self._next.items())

    def pop_due(self, upto: datetime) -> list[Event]:
        """Consume every tick due at or before `upto` and return its STARTJOB
        event, stamped at the tick and ordered by (tick, job). A stalled-but-
        alive engine therefore fires its backlog late but truthfully stamped;
        ticks missed across DOWNTIME never reach this path -- resume drops
        and journals them instead (PENDING: E9)."""
        due: list[tuple[datetime, str]] = []
        exhausted: list[str] = []
        for job, tick in self._next.items():
            nxt: datetime | None = tick
            while nxt is not None and nxt <= upto:
                due.append((nxt, job))
                nxt = self._occurrence(self._plans[job], nxt, inclusive=False)
            if nxt is None:
                exhausted.append(job)  # calendar ran out mid-run: dormant (DL-56)
            else:
                self._next[job] = nxt
        for job in exhausted:
            del self._next[job]
        due.sort()
        return [Event(at=tick, kind="STARTJOB", payload={"job": job}) for tick, job in due]

    @staticmethod
    def _occurrence(plan: _SchedulePlan, t: datetime, *, inclusive: bool) -> datetime | None:
        # calendar-date iteration (never aware-datetime + timedelta: absolute
        # arithmetic can skip a 25h fall-back local date); per-day ticks are
        # sorted AFTER conversion because a fold=0 nonexistent time can land
        # past a later tick's UTC instant inside a spring-forward gap
        anchor_date = (t.replace(tzinfo=UTC).astimezone(plan.tz) if plan.tz else t).date()
        # a non-empty weekly day set always hits within 7; explicit dates
        # push the bound past the last one, after which weekly recurrence
        # (if any) is unobstructed again; unbounded extended rules scan to
        # the autocal dormancy ceiling (a leap-day-weekday conjunction can
        # legally gap ~40 years, DL-57)
        span = 371
        if plan.last_date is not None:
            span = max(span, (plan.last_date - anchor_date).days + 8)
        if plan.run_gen is not None and plan.run_gen.compiled.bound is None:
            span = max(span, _EXTENDED_SCAN_DAYS)
        if plan.exclude_gen is not None:
            # an extended exclusion can outlast the weekly scan; a bounded
            # one scans past its bound, an unbounded one to the ceiling
            ex_bound = plan.exclude_gen.compiled.bound
            span = max(
                span,
                _EXTENDED_SCAN_DAYS if ex_bound is None else (ex_bound - anchor_date).days + 8,
            )
        for offset in range(span):
            day = anchor_date + timedelta(days=offset)
            if not plan.day_eligible(day):
                continue
            for utc_tick in sorted(plan.utc_ticks_on(day)):
                if utc_tick > t or (inclusive and utc_tick == t):
                    return utc_tick
        if plan.run_dates is not None or plan.run_gen is not None or plan.exclude_gen is not None:
            return None  # calendar exhausted/fully excluded: dormant, not an error (DL-56/57)
        raise EngineError("no scheduler occurrence within a year (unreachable: validated block)")
