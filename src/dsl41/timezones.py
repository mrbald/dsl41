"""SEM-35 name resolution and the naive-UTC <-> local conversion, once.

A phase-free module: it imports nothing from `dsl41` and nothing imports it
for anything but zones, so every layer that reads a `timezone:` reaches the
SAME ladder without reaching THROUGH another layer to get it. That was the
DL-152 finding -- the oracle (phase 7) resolved names through a deferred
import of `runner_scheduler` (phase 11c), which hid the dependency rather
than removing it, and re-stated the two conversions beside it (DL-163).

SEM-35 name resolution (TechDocs 12.1, `timezone` attribute + the
autotimezone command): a JIL `timezone:` value is matched against the OS
zone database FIRST; only if that misses is the instance's ujo_timezones
table read -- a vendor-shipped, admin-editable map whose City/Alias entries
chain ("up to five times") down to a Zone the OS recognizes. Values are not
case-sensitive. dsl41's port: zoneinfo is the OS database; `--timezone-map`
(the `autotimezone -l` listing, or bare `name zone` pairs) is the table.
Without a map, a city name resolves through a documented deterministic
default (DL-62): the UNIQUE zoneinfo zone whose final path component matches
(Zurich -> Europe/Zurich), surfaced as a preflight WARN -- a supplied listing
is complete estate truth, so the default is off when a map is given. A POSIX
fixed-offset form (`GMT+5`, `IST-5:30`) resolves per the POSIX sign
convention (positive = WEST of GMT); POSIX strings WITH dst rules stay
unresolvable -- modelling vendor DST rules approximately would silently
shift ticks.

Refusals belong to the CALLER, not here: `resolve_timezone` answers None and
each layer says what an unresolvable name means in its own words (the
scheduler an EngineError, the oracle an OracleError, preflight a finding).
"""

from __future__ import annotations

import contextlib
import functools
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Literal, NamedTuple
from zoneinfo import ZoneInfo, available_timezones

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


def city_candidates(name: str) -> tuple[str, ...]:
    """Zoneinfo keys whose final path component is `name` (folded).

    Public because preflight reads it to tell an AMBIGUOUS city apart from
    an unknown name -- it used to reach for the private spelling across the
    module line, which `arch_baseline.json` carried as a finding (DL-163)."""
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
    """The SEM-35 ladder (module docstring): OS lookup, then the alias map
    chained <=5 hops with an OS lookup per hop, then -- only with NO map --
    the unique-city default. None = genuinely unresolvable.

    `aliases` None and `aliases` empty are DIFFERENT resolutions: the
    city default applies only when the estate supplied no table at all, so
    a run WITH a map gets the map's answer and nothing else."""
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
    if aliases is None and len(candidates := city_candidates(name)) == 1:
        return ResolvedTz(ZoneInfo(candidates[0]), candidates[0], "city")
    return None


# ------------------------------------------------------------- the conversion
#
# Instants inside the engine and the oracle are NAIVE UTC (runner-design
# ss5). A time attribute is read in the job's own zone (SEM-35 re-bases every
# one of them), so every comparison crosses this line exactly twice: in to
# compare, out to schedule. The conversion honours the caller's PEP 495
# fold and defaults to fold=0 -- a fall-back ambiguous local time is its
# FIRST occurrence and a spring-forward nonexistent one maps past the gap.
# Callers that follow the vendor's DST rules pass fold=1 or move the time
# themselves: run_window (DL-249), absolute must times (DL-253) and start
# times (`start_time_instants`, DL-260). One definition of each direction,
# because the scheduler and the oracle both cross it and a second spelling
# is a second DST pin (DL-163).


def to_local(when: datetime, tz: tzinfo | None) -> datetime:
    """A naive-UTC instant as naive wall time in `tz`. `tz` None: unchanged,
    which is the "engine clock IS the comparison basis" case."""
    if tz is None:
        return when
    return when.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)


def to_utc(local: datetime, tz: tzinfo | None) -> datetime:
    """The inverse of `to_local`: naive wall time in `tz` as a naive-UTC
    instant."""
    if tz is None:
        return local
    return local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


#: The two wall hours a documented DST change touches (DL-249): spring
#: forward skips 02:00-02:59, fall back repeats 01:00-01:59. Both changes
#: happen at 02:00 local, as in America/New_York.
MISSING_HOUR = 2
REPEATED_HOUR = 1

#: The `dst-start-times` switch's values (DL-260). Defined here, where the
#: value is read, so this module still imports nothing from `dsl41`;
#: `semantics` registers the switch from this alias.
DstStartTimes = Literal["vendor", "fold0"]


def _offsets(local: datetime, tz: tzinfo) -> tuple[timedelta | None, timedelta | None]:
    """The UTC offset of wall time `local` at fold=0 and at fold=1 (PEP 495)."""
    return (
        local.replace(tzinfo=tz, fold=0).utcoffset(),
        local.replace(tzinfo=tz, fold=1).utcoffset(),
    )


def dst_change(day: date, tz: tzinfo | None) -> Literal["spring", "fall"] | None:
    """The documented one-hour DST change on local `day`, or None (DL-249).

    "spring": wall 02:00-02:59 does not exist. "fall": wall 01:00-01:59
    happens twice. Any other shape answers None: a half-hour change, a
    change at another hour, a zone without DST, a fixed offset."""
    if tz is None:
        return None
    hour = timedelta(hours=1)

    def gap(h: int, m: int) -> bool:
        before, after = _offsets(datetime.combine(day, time(h, m)), tz)
        return before is not None and after is not None and after - before == hour

    def repeat(h: int, m: int) -> bool:
        first, second = _offsets(datetime.combine(day, time(h, m)), tz)
        return first is not None and second is not None and first - second == hour

    def clean(h: int, m: int) -> bool:
        first, second = _offsets(datetime.combine(day, time(h, m)), tz)
        return first == second

    if gap(MISSING_HOUR, 0) and gap(MISSING_HOUR, 59) and clean(1, 59) and clean(3, 0):
        return "spring"
    if repeat(REPEATED_HOUR, 0) and repeat(REPEATED_HOUR, 59) and clean(0, 59) and clean(2, 0):
        return "fall"
    return None


def dst_change_near(day: date, tz: tzinfo | None, days: int = 2) -> bool:
    """Whether a documented DST change (`dst_change`) falls on a local date
    within `days` of `day` (DL-249). No zone is never near one, and a date
    outside the calendar's range counts as no change."""
    if tz is None:
        return False
    for k in range(-days, days + 1):
        try:
            other = day + timedelta(days=k)
        except OverflowError:
            continue
        if dst_change(other, tz) is not None:
            return True
    return False


def vendor_gap_instant(day: date, hour: int, minute: int, tz: tzinfo | None) -> datetime:
    """A wall time in a spring change's missing hour, as the vendor moves
    it: into the first minute of the next hour, its minute read as seconds.
    "Daylight Time Changes": "a job that is scheduled to run on Sundays at
    2:05 runs at 3:00:05"."""
    first = to_utc(datetime.combine(day, time(hour + 1, 0)), tz)
    return first + timedelta(seconds=minute)


def start_time_instants(
    day: date, times: Sequence[tuple[int, int]], tz: tzinfo | None, *, dst: DstStartTimes
) -> list[tuple[int, datetime]]:
    """The start_times entries `times`, as (hour, minute), on local `day` as
    engine instants, each paired with its index in `times` and listed in
    wall-time order. An entry that does not fire that day is left out.

    `dst` "fold0" is the fold=0 conversion of every entry (the
    `dst-start-times` switch): a repeated wall time is its first
    occurrence, and a missing one maps past the gap. `dst` "vendor" applies
    the documented rules on a change of the shape `dst_change` names
    (DL-260). TechDocs 12.1 and 24.2, "Standard Time Changes": jobs whose
    start_time is "between 1:00 and 1:59" run "during the second (standard
    time) hour". "Daylight Time Changes": a missing-hour start runs "during
    the first minute of the next hour" (2:05 runs at 3:00:05), and "If you
    schedule a job to run more than once during the missing hour (for
    example, at 2:05 and 2:25), only the first scheduled job run occurs."
    Other shapes keep the fold=0 conversion.

    Under fold=0 a missing wall time can land on a later entry's instant;
    the earlier wall time is listed first."""
    change = dst_change(day, tz) if dst == "vendor" else None
    instants: list[tuple[int, datetime]] = []
    gap_taken = False
    for index in sorted(range(len(times)), key=lambda i: times[i]):
        hour, minute = times[index]
        if change == "spring" and hour == MISSING_HOUR:
            if not gap_taken:
                gap_taken = True
                instants.append((index, vendor_gap_instant(day, hour, minute, tz)))
            continue
        second = change == "fall" and hour == REPEATED_HOUR
        local = datetime.combine(day, time(hour, minute)).replace(fold=1 if second else 0)
        instants.append((index, to_utc(local, tz)))
    return instants


def start_mins_instants(
    day: date, ticks: Sequence[tuple[int, int]], tz: tzinfo | None, *, dst: DstStartTimes
) -> list[datetime]:
    """start_mins ticks, as (hour, minute), on local `day` as engine
    instants, in wall-time order.

    `dst` "fold0" is the fold=0 conversion, as in `start_time_instants`.
    `dst` "vendor" applies the documented rules on a change of the shape
    `dst_change` names (DL-260). "Standard Time Changes": "Jobs for which
    the start_mins attribute is set run in both hours." "Daylight Time
    Changes": "Jobs with relative time dependencies run as expected", so a
    tick in the missing hour does not exist: 0, 20 and 40 run at "1:00 ST,
    1:20 ST, 1:40 ST, 3:00 DT, 3:20 DT, and 3:40 DT"."""
    change = dst_change(day, tz) if dst == "vendor" else None
    instants: list[datetime] = []
    for hour, minute in ticks:
        if change == "spring" and hour == MISSING_HOUR:
            continue
        local = datetime.combine(day, time(hour, minute))
        instants.append(to_utc(local, tz))
        if change == "fall" and hour == REPEATED_HOUR:
            instants.append(to_utc(local.replace(fold=1), tz))
    return instants


def must_instant(
    day: date, start: tuple[int, int], must: tuple[int, int], tz: tzinfo | None
) -> datetime:
    """An absolute must time, (hour, minute) with hour 0-71, as an engine
    instant (SEM-34, DL-253). `day` is the local calendar day of the start
    it pairs with, `start` that start time. Hours 24-71 land on the next
    days. TechDocs 24.2, "Daylight Time Changes" and "Standard Time
    Changes".

    Spring, a must time in the missing hour moves to the first minute of
    the next hour: "a job that must start by 2:05 and must complete by 2:45
    generates an alarm if the job does not start by 3:00:05 or if it does
    not complete by 3:00:45". A start in the missing hour runs in that
    minute too; when it would run after its must time, the must time moves
    "to the final second of the first minute of the hour following the
    missing hour", 3:00:59.

    Fall, a must time in the repeated hour takes the first, daylight pass
    when the start is before the change: "must complete at 1:30 generates
    an alarm if the job has not completed by 1:30 DT". When the start is in
    the repeated hour too, it takes the second, standard pass. A start on
    an earlier day is before the change.

    Other change shapes keep the plain fold=0 conversion, as run_window's
    do (DL-249)."""
    start_hour, start_minute = start
    days, hour = divmod(must[0], 24)
    local = datetime.combine(day + timedelta(days=days), time(hour, must[1]))
    change = dst_change(local.date(), tz)
    if change == "spring" and hour == MISSING_HOUR:
        due = vendor_gap_instant(local.date(), hour, must[1], tz)
    else:
        second = (
            change == "fall" and hour == REPEATED_HOUR and days == 0 and start_hour == REPEATED_HOUR
        )
        due = to_utc(local.replace(fold=1 if second else 0), tz)
    if dst_change(day, tz) == "spring" and start_hour == MISSING_HOUR:
        runs_at = vendor_gap_instant(day, start_hour, start_minute, tz)
        if runs_at > due:
            due = vendor_gap_instant(day, start_hour, 59, tz)
    return due


def window_span(day: date, lo: time, hi: time, tz: tzinfo | None) -> tuple[datetime, datetime]:
    """The run_window that opens on local `day`, as two engine instants,
    with the vendor's endpoint rules on a documented DST change (DL-249).
    TechDocs 12.1 and 24.2, "Daylight Time Changes" and "Standard Time
    Changes".

    Spring, an opening in the missing hour moves to 03:00: "a run window of
    2:45 - 3:45 becomes 3:00 - 3:45". A close in the missing hour keeps the
    window's length: "a run window of 1:00 - 2:30 ... ends at 3:30". Both in
    it: "a run window of 2:15 - 2:45 becomes 3:00 - 3:45". Fall, an opening
    in the repeated hour takes the second, standard-time pass: "a run
    window of 1:45 - 2:45 becomes 1:45 ST - 2:45 ST". A close in it takes
    the first pass: "a run window of 11:30 - 1:30 ends at 1:30 DT". When
    both fall in it, "the run window opens during the second, standard time
    hour", and the close follows it there. The vendor's text describes two
    distinct endpoints, so an equal-endpoint window keeps the SEM-33
    zero-width pin: the single instant its opening maps to."""
    start = datetime.combine(day, lo)
    end = datetime.combine(day if lo <= hi else day + timedelta(days=1), hi)
    change = dst_change(day, tz)
    opens_repeated = change == "fall" and start.hour == REPEATED_HOUR
    if change == "spring" and start.hour == MISSING_HOUR:
        opens = to_utc(start.replace(hour=MISSING_HOUR + 1, minute=0), tz)
    else:
        opens = to_utc(start.replace(fold=1 if opens_repeated else 0), tz)
    if lo == hi:
        return opens, opens
    # fold=0 is the vendor's close in both seasons: past the gap by the
    # missing hour in spring, the daylight-time pass in fall. Only a close
    # on the same day as an opening in the repeated hour takes fold=1.
    closes_second = opens_repeated and end.date() == day and end.hour == REPEATED_HOUR
    closes = to_utc(end.replace(fold=1 if closes_second else 0), tz)
    return opens, closes


def window_spans_near(
    today: date, lo: time, hi: time, tz: tzinfo | None
) -> list[tuple[datetime, datetime]] | None:
    """The run_window as concrete engine-instant intervals around local day
    `today`, or None when no documented DST change (`dst_change`) is within
    two days of it and the window is compared by wall time instead
    (`wall_window_contains`). DL-249: near a documented change the window is
    a set of intervals whose endpoints follow the vendor's rules. No zone has
    such a change within days of the calendar's ends, so the four opening
    days stay inside the date range."""
    if not dst_change_near(today, tz):
        return None
    return [window_span(today + timedelta(days=k), lo, hi, tz) for k in range(-2, 2)]


def wall_window_contains(now: time, lo: time, hi: time) -> bool:
    """SEM-33 on local wall time: whether `now` is inside the run_window
    `lo`-`hi`, both ends inclusive; a window with `lo` after `hi` crosses
    midnight."""
    if lo <= hi:
        return lo <= now <= hi
    return now >= lo or now <= hi


class DstWindow(NamedTuple):
    """One run of wall minutes a DST change makes a gap (they do not exist)
    or an overlap (they occur twice), as minutes of the local day."""

    kind: str  # "gap" or "overlap"
    start: int
    end: int
    documented: bool  # the one-hour change at 02:00 local that `dst_change` names
    day: date  # a local day it occurs on

    @property
    def label(self) -> str:
        return (
            f"{self.start // 60:02d}:{self.start % 60:02d}-{self.end // 60:02d}:{self.end % 60:02d}"
        )


def _dst_day_windows(day: date, tz: tzinfo) -> list[DstWindow]:
    """The gap and overlap minute runs of one local day with a UTC-offset
    change, read from the zone's own rules (PEP 495 fold 0 against fold 1)."""
    change = dst_change(day, tz)
    documented = {"gap": change == "spring", "overlap": change == "fall"}
    runs: list[DstWindow] = []
    open_run: tuple[Literal["gap", "overlap"], int] | None = None
    for minute in range(24 * 60 + 1):
        kind: Literal["gap", "overlap"] | None = None
        if minute < 24 * 60:
            first, second = _offsets(datetime.combine(day, time(minute // 60, minute % 60)), tz)
            if first is not None and second is not None and first != second:
                kind = "gap" if second > first else "overlap"
        if open_run is not None and kind != open_run[0]:
            run_kind, begun = open_run
            runs.append(DstWindow(run_kind, begun, minute - 1, documented[run_kind], day))
            open_run = None
        if kind is not None and open_run is None:
            open_run = (kind, minute)
    return runs


@functools.lru_cache(maxsize=256)
def _dst_year_windows(tz: tzinfo, year: int) -> tuple[DstWindow, ...]:
    """Every DST gap and overlap of `year` in `tz`: the days whose local
    midnight offsets differ are scanned minute by minute."""
    found: list[DstWindow] = []
    day = date(year, 1, 1)
    while day.year == year:
        following = day + timedelta(days=1)
        before = datetime.combine(day, time()).replace(tzinfo=tz).utcoffset()
        after = datetime.combine(following, time()).replace(tzinfo=tz).utcoffset()
        if before != after:
            found.extend(_dst_day_windows(day, tz))
        day = following
    return tuple(found)


def dst_windows(tz: tzinfo, reference: date) -> tuple[DstWindow, ...]:
    """The gaps and overlaps of the reference year and the next in `tz`, one
    per distinct (kind, minutes, shape) and in wall order, each with the
    first local day it occurs on. A zone whose rules repeat each year gives
    the same shape on every such day, so that day stands for them all."""
    seen: dict[tuple[str, int, int, bool], DstWindow] = {}
    for year in (reference.year, reference.year + 1):
        for window in _dst_year_windows(tz, year):
            seen.setdefault((window.kind, window.start, window.end, window.documented), window)
    return tuple(sorted(seen.values(), key=lambda w: (w.start, w.kind)))


@functools.lru_cache(maxsize=512)
def wall_window_intervals(
    first: date, last: date, lo: time, hi: time, tz: tzinfo
) -> tuple[tuple[datetime, datetime], ...]:
    """The run_window as the wall-time comparison opens it (`wall_window_contains`)
    over the local days `first` to `last`: for each interval of elapsed time
    inside it, the local wall time it opens at and the local wall time of its
    last minute. An interval cut by either end of the range is left out."""
    start = to_utc(datetime.combine(first, time()), tz)
    stop = to_utc(datetime.combine(last + timedelta(days=1), time()), tz)
    intervals: list[tuple[datetime, datetime]] = []
    opened: datetime | None = None
    previous = start
    was_inside: bool | None = None
    now = start
    while now < stop:
        local = to_local(now, tz)
        inside = wall_window_contains(local.time(), lo, hi)
        if inside and was_inside is False:
            opened = local
        if not inside and was_inside and opened is not None:
            intervals.append((opened, previous))
        if not inside:
            opened = None
        previous = local
        was_inside = inside
        now += timedelta(minutes=1)
    return tuple(intervals)


def alias_table(aliases: Mapping[str, str] | None) -> dict[str, str] | None:
    """A ujo_timezones table as `resolve_timezone` takes it: a private copy,
    and EMPTY normalised to None.

    The rule lives here because it is a fact about the ladder, not about any
    one source of a table: the unique-city default is conditioned on the
    ABSENCE of a table, so a caller that passes `{}` where it meant "no map"
    silently retires that default for every per-job zone. Three callers each
    stated this in their own words -- a live scheduler's table, a period
    profile's pin, and the resume path's re-read of that pin -- and DL-151
    was the bug that came of one of them not stating it at all (DL-163)."""
    return dict(aliases) if aliases else None
