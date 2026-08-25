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
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
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
# compare, out to schedule. DST corners follow PEP 495 fold=0 -- a fall-back
# ambiguous local time is its FIRST occurrence and a spring-forward
# nonexistent one maps past the gap (runner-design E10). One definition of
# each direction, because the scheduler and the oracle both cross it and a
# second spelling is a second DST pin (DL-163).


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
