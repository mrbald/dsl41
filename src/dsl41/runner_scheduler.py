"""Runner scheduler (ss5): the calendar the oracle deliberately lacks.

The runner modules form an import DAG with runner_clock at the bottom, and this module sits
above it (DL-74). Time-zone name
resolution (SEM-35) and the naive-UTC <-> local conversion live in `dsl41.timezones`
(DL-163); the refusal for an unresolvable zone is this module's own.

Scheduler (ss5, ss8, ss10; DL-45): per date_conditions job it computes the next
occurrence from days_of_week + start_times/start_mins (absent days_of_week = every day,
PENDING: E10). The zone is the per-job timezone, else the run-level base zone, else
UTC; the base-zone default is the vendor's own rule (DL-155). Names resolve per SEM-35:
zoneinfo, then the --timezone-map ujo_timezones listing, then the DL-62 unique-city
default. A run_calendar job with neither start_times nor start_mins fires from the
calendar rows' own times (E11, DL-58). The engine receives STARTJOB events stamped at
the tick and journaled like any input (source=scheduler). The scheduler fires
UNCONDITIONALLY: SEM-32 arm-and-wait (Q3, DL-58) and SEM-33 run_window stay oracle-side.
While the engine is up a late tick still fires, stamped at the tick; across downtime
missed ticks are dropped AND journaled at resume, never fired late (PENDING: E9).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Literal

from dsl41.autocal import (
    CalendarRuleError,
    CompiledCalendar,
    compile_calendar,
    standard_rows,
)
from dsl41.ir import CatalogIR, ScheduleBlock
from dsl41.oracle_state import Event
from dsl41.runner_clock import EngineError
from dsl41.semantics import DEFAULTS as DEFAULT_SWITCHES
from dsl41.semantics import SemanticSwitches
from dsl41.timezones import (
    DstStartTimes,
    resolve_timezone,
    start_mins_instants,
    start_time_instants,
    to_local,
    to_utc,
)


# ------------------------------------------------------------------ scheduler (ss5)

#: Python date.weekday() (Monday=0) -> JIL day token (ir._DAY_TOKENS)
_DAY_CODES = ("mo", "tu", "we", "th", "fr", "sa", "su")

#: occurrence-scan ceiling for UNBOUNDED extended-calendar rules; equals autocal's dormancy
#: ceiling (DL-57)
_EXTENDED_SCAN_DAYS = 60 * 366


class _CalCache:
    """Windowed membership over a CompiledCalendar (DL-57).

    Per-day queries fill 366-day blocks once; a generation-time refusal (Q8a) surfaces as
    EngineError."""

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
    """One job's compiled trigger.

    It holds the eligible day tokens OR a run_calendar day source (SEM-31 XOR: a standard
    date set or an extended-calendar generator, DL-57), an exclude_calendar source to
    subtract, sorted (hour, minute) ticks per eligible day, their source, and the resolved
    zone (None = the engine's naive UTC basis). `last_date` bounds the occurrence scan past
    the last explicit date (DL-56); an unbounded extended run source scans to the autocal
    dormancy ceiling."""

    days: frozenset[str]
    times: tuple[tuple[int, int], ...]
    tz: tzinfo | None
    #: where `times` come from: start_times and start_mins follow the `dst-start-times`
    #: switch on a DST change day (DL-260); calendar row times use the fold=0 conversion
    source: Literal["start_times", "start_mins", "calendar"] = "calendar"
    #: the `dst-start-times` switch
    dst_start_times: DstStartTimes = "vendor"
    run_dates: frozenset[date] | None = None
    exclude_dates: frozenset[date] = frozenset()
    run_gen: _CalCache | None = None
    exclude_gen: _CalCache | None = None
    last_date: date | None = None
    #: E11 (DL-58): a run_calendar job with no start_times/start_mins fires at each row's own
    #: time (bare rows at 00:00); set only in that shape, where it replaces `times` per day.
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
        """This local day's ticks as naive-UTC instants; start_times and start_mins convert
        through the definition the oracle's slot matching shares (DL-260)."""
        if self.source == "start_times":
            instants = start_time_instants(day, self.times, self.tz, dst=self.dst_start_times)
            return [at for _, at in instants]
        if self.source == "start_mins":
            return start_mins_instants(day, self.times, self.tz, dst=self.dst_start_times)
        times = self.times if self.row_times is None else self.row_times.get(day, frozenset())
        ticks = []
        for hour, minute in times:
            naive_local = datetime(day.year, day.month, day.day, hour, minute)
            ticks.append(to_utc(naive_local, self.tz))
        return ticks


def _scheduler_calendar(
    catalog: CatalogIR, job: str, role: str, ref: str, semantics: SemanticSwitches
) -> dict[date, frozenset[tuple[int, int]]] | CompiledCalendar:
    """Resolve a run_calendar/exclude_calendar reference.

    Returns a standard calendar's day -> row-tick map (row times fire E11 jobs, DL-58;
    exclusion reads only the day keys) or an extended calendar compiled by the autocal
    interpreter (DL-57). Preflight gates a bad reference (ss8); a catalog that bypassed it
    is refused here, never guessed (DL-56)."""
    cal = catalog.calendars.get(ref)
    if cal is None:
        raise EngineError(f"{job}: {role} {ref!r} has no calendar definition in the loaded set")
    try:
        if cal.kind == "extended":
            return compile_calendar(cal, catalog, semantics)
        return standard_rows(cal)
    except CalendarRuleError as exc:
        raise EngineError(f"{job}: {exc}") from exc


def _scheduler_tz(name: str, owner: str, aliases: Mapping[str, str] | None) -> tzinfo:
    """Resolve a zone name or raise EngineError; preflight gates it first (ss8)."""
    resolved = resolve_timezone(name, aliases)
    if resolved is None:
        raise EngineError(
            f"{owner}: timezone {name!r} is not resolvable (SEM-35: zoneinfo,"
            " the --timezone-map table, or a POSIX fixed offset)"
        )
    return resolved.tz


class Scheduler:
    """ss5: yields STARTJOB events at each job's tick; it fires unconditionally (SEM-32
    arm-and-wait and SEM-33 run_window stay oracle-side).

    Ticks are naive-UTC instants (the engine's time basis). The zone is the per-job
    `timezone`, else `default_tz`, else UTC, resolved by resolve_timezone's SEM-35 ladder
    (zoneinfo, the `tz_aliases` ujo_timezones map, the DL-62 city default, POSIX fixed
    offsets), so rehearse under the virtual clock exercises real calendar arithmetic.

    Absent days_of_week means every day (E10; the module docstring carries its marker).
    A job without `timezone` reads its times in `default_tz` (run-level --timezone),
    else UTC (DL-155): no-timezone start events are "scheduled based on the time zone
    under which the scheduler is running" (TechDocs 12.0.01, timezone attribute).

    DST corners follow the `dst-start-times` switch (DL-260). Its default, `vendor`,
    applies these rules on a one-hour change at 02:00 local: a start time in the repeated
    01:00-01:59 fires once, in the second pass; start_mins fire in both passes; a start
    time in the missing 02:00-02:59 fires in the first minute of 03:00 with its minute as
    seconds, and only the first such start time fires; start_mins ticks in the missing
    hour do not exist. `fold0`, and every other change shape, use PEP 495 fold=0: a
    repeated time is its first occurrence, a missing one maps past the gap. Calendar row
    times always use fold=0. A schedule block with neither start_times nor start_mins
    triggers nothing (run_window/SLA are gates and alarms).

    `run_calendar` day membership (SEM-31: XOR days_of_week) minus `exclude_calendar`
    days, both on the job's LOCAL day, apply (DL-56). A run_calendar job with neither
    start_times nor start_mins fires at each calendar row's own HH:MM tail, bare rows at
    00:00 (E11, DL-58); job-level times override row times; an extended calendar has no
    rows, so that shape ticks at 00:00. An exhausted calendar leaves the job dormant, never
    an error. Extended calendars compile through the autocal interpreter (DL-57) and scan
    windowed, out to the dormancy ceiling for unbounded rules."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        start: datetime,
        default_tz: str | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        base_tz = _scheduler_tz(default_tz, "--timezone", tz_aliases) if default_tz else None
        # the wiring inputs stay read-only properties, so the runtime profile the period pins
        # comes from the inputs the plans use; a mutable attribute would let an edit pin
        # one timezone while `_plans` run under another (period-model ss2.1, DL-130)
        self._default_tz = default_tz
        self._tz_aliases: dict[str, str] = dict(tz_aliases or {})
        #: the switches the extended calendars compile under; an engine refuses a scheduler
        #: whose switches are not its own (DL-252)
        self._semantics = semantics or DEFAULT_SWITCHES
        # the catalog the plans compiled from is pinned as its v2 hash; an object reference
        # could mutate and the gate would then bless stale plans under the new identity
        from dsl41.run_root import catalog_hash_v2

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
                source = _scheduler_calendar(
                    catalog, name, "run_calendar", sched.run_calendar, self._semantics
                )
                if isinstance(source, CompiledCalendar):
                    run_gen = _CalCache(source)
                else:
                    run_dates = frozenset(source)
                    if not own_ticks:
                        # E11 (DL-58): without job-level times each row's own time fires
                        row_times = source
            exclude_dates: frozenset[date] = frozenset()
            exclude_gen: _CalCache | None = None
            if sched.exclude_calendar is not None:
                source = _scheduler_calendar(
                    catalog, name, "exclude_calendar", sched.exclude_calendar, self._semantics
                )
                if isinstance(source, CompiledCalendar):
                    exclude_gen = _CalCache(source)
                else:
                    exclude_dates = frozenset(source)
            if sched.days_of_week is not None and not sched.days_of_week:  # pragma: no cover
                # unreachable: lowering rejects an empty days_of_week and a runner catalog is
                # re-lowered from its bundle's JIL (boundary.load_bundle_catalog); a
                # hand-built IR carrying one is refused here
                raise EngineError(f"{name}: days_of_week is empty; nothing to schedule")
            explicit = (run_dates or frozenset()) | exclude_dates
            last_date = max(explicit) if explicit else None
            if run_gen is not None and run_gen.compiled.bound is not None:
                # cycle-bound extended calendars scan like explicit dates
                last_date = max(d for d in (last_date, run_gen.compiled.bound) if d is not None)
            tick_source: Literal["start_times", "start_mins", "calendar"] = (
                "start_times" if sched.start_times else "start_mins" if own_ticks else "calendar"
            )
            self._plans[name] = _SchedulePlan(
                days=frozenset(
                    _DAY_CODES
                    if (sched.days_of_week is None or "all" in sched.days_of_week)
                    else sched.days_of_week
                ),
                # E11 (DL-58): a generated (extended) day has no row time, so 00:00
                times=self._ticks(sched) if own_ticks else ((0, 0),),
                tz=_scheduler_tz(sched.timezone, name, tz_aliases) if sched.timezone else base_tz,
                source=tick_source,
                dst_start_times=self._semantics.dst_start_times,
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

    @property
    def semantics(self) -> SemanticSwitches:
        return self._semantics

    @staticmethod
    def _ticks(sched: ScheduleBlock) -> tuple[tuple[int, int], ...]:
        if sched.start_times:
            return tuple(sorted((t.hour, t.minute) for t in sched.start_times))
        return tuple(sorted((h, m) for h in range(24) for m in sched.start_mins or []))

    def reset(self, start: datetime) -> None:
        """Re-anchor every job's next tick at or after `start`; a tick exactly at `start` counts.

        A crash between same-instant siblings leaves one unjournaled, and an exclusive
        re-anchor would drop it unrecorded (period-model ss6: the cutoff barrier). The
        RESUME sweep skips a tick the engine already admitted; the scheduler does not
        encode that (DL-166). A job whose calendar is exhausted gets no entry (DL-56)."""
        self._next = {}
        for job, plan in self._plans.items():
            occ = self._occurrence(plan, start, inclusive=True)
            if occ is not None:
                self._next[job] = occ

    def next_occurrence(self) -> datetime | None:
        """Earliest pending tick across all jobs (naive UTC), or None."""
        return min(self._next.values(), default=None)

    def upcoming(self) -> list[tuple[datetime, str]]:
        """Each scheduled job's NEXT tick, due-ordered, one entry per job (ss10 `timers` verb,
        DL-65)."""
        return sorted((tick, job) for job, tick in self._next.items())

    def pop_due(self, upto: datetime) -> list[Event]:
        """Consume every tick due at or before `upto`; return its STARTJOB event, stamped at
        the tick and ordered by (tick, job).

        A stalled engine fires its backlog late but truthfully stamped. Resume's sweep
        derives ticks missed across DOWNTIME with this call, then drops and journals them
        (PENDING: E9)."""
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
        # iterates calendar dates, since aware-datetime + timedelta can skip a 25h fall-back
        # date; per-day ticks sort AFTER conversion because a missing-hour time can land
        # past a later tick on a spring-forward day: fold=0 maps 02:30 past 03:00, and the
        # vendor's 02:05 runs at 03:00:05 (DL-260)
        anchor_date = to_local(t, plan.tz).date()
        # a non-empty weekly day set hits within 7 days; explicit dates push the bound past
        # the last one; unbounded extended rules scan to the autocal dormancy ceiling,
        # since a leap-day-weekday conjunction can gap ~40 years (DL-57)
        span = 371
        if plan.last_date is not None:
            span = max(span, (plan.last_date - anchor_date).days + 8)
        if plan.run_gen is not None and plan.run_gen.compiled.bound is None:
            span = max(span, _EXTENDED_SCAN_DAYS)
        if plan.exclude_gen is not None:
            # an extended exclusion can outlast the weekly scan: past its bound, or to the ceiling
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
        # unreachable: lowering refuses an unknown day token and an empty list, so a plan
        # without a calendar holds a weekday that recurs within 7 days
        raise EngineError(  # pragma: no cover
            "no scheduler occurrence within a year (unreachable: validated block)"
        )
