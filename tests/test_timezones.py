"""SEM-35 name resolution and the naive-UTC <-> local conversion.

The ladder's own tests, moved here with the module by DL-163 (they were in
test_runner_scheduler.py, which is now one of its callers). Tests that build
a Scheduler stayed there: those pin the caller, not the ladder.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dsl41.timezones import (
    alias_table,
    parse_timezone_map,
    resolve_timezone,
    to_local,
    to_utc,
)


def test_resolve_timezone_posix_offsets_are_west_positive() -> None:
    """(SEM-35, TechDocs TZ syntax): POSIX offsets are west-positive --
    IST-5:30 (the docs' own example) is UTC+05:30, GMT+5 is UTC-05:00.
    A POSIX string WITH dst rules stays unresolvable (modelling vendor DST
    rules approximately would silently shift ticks)."""
    ist = resolve_timezone("IST-5:30")
    assert ist is not None and ist.how == "posix"
    assert ist.tz.utcoffset(None) == timedelta(hours=5, minutes=30)
    gmt5 = resolve_timezone("GMT+5")
    assert gmt5 is not None and gmt5.tz.utcoffset(None) == timedelta(hours=-5)
    assert resolve_timezone("MET-1METDST") is None


def test_resolve_timezone_chain_limit_and_cycles_stay_unresolved() -> None:
    """(SEM-35: 'the ujo_timezones table is read up to five times'): a
    six-hop chain exhausts the limit; a cycle terminates as unresolvable."""
    deep = {
        "a": "b",
        "b": "c",
        "c": "d",
        "d": "e",
        "e": "f",
        "f": "Europe/Zurich",
    }
    assert resolve_timezone("a", deep) is None  # 6th read would be needed
    assert resolve_timezone("b", deep) is not None  # 5 reads suffice from here
    assert resolve_timezone("loop", {"loop": "pool", "pool": "loop"}) is None


def test_resolve_timezone_map_suppresses_the_city_default() -> None:
    """(DL-62): a supplied listing is complete estate truth -- a city name
    missing from it does NOT fall back to the zoneinfo city match."""
    assert resolve_timezone("Zurich") is not None
    assert resolve_timezone("Zurich", {"dallas": "US/Central"}) is None


def test_resolve_timezone_ambiguous_city_component_is_refused() -> None:
    """(DL-62): the city default requires a UNIQUE match; multiple zones
    sharing a final path component resolve to none (the preflight ERROR
    names the candidates)."""
    from dsl41 import timezones as tz_mod

    monkey = {"x": ("A/X", "B/X")}
    original = tz_mod._zone_tables
    tz_mod._zone_tables = lambda: ({}, monkey)  # type: ignore[assignment]
    try:
        assert resolve_timezone("x") is None
        assert resolve_timezone("X") is None
    finally:
        tz_mod._zone_tables = original


def test_parse_timezone_map_listing_pairs_and_junk() -> None:
    """(DL-62): the autotimezone -l shape (header + ruler + 3-field rows)
    and bare pairs parse; names fold case and -/_; junk raises naming the
    line (no silent loss)."""
    listing = (
        "Entry Type Zone\n"
        "---------------------- ------ ----------------\n"
        "US/Samoa Alias Pacific/Samoa\n"
        "Port-au-Prince City America/Port-au-Prince\n"
        "\n"
        "Zurich Europe/Zurich\n"
    )
    aliases = parse_timezone_map(listing)
    assert aliases == {
        "us/samoa": "Pacific/Samoa",
        "port_au_prince": "America/Port-au-Prince",
        "zurich": "Europe/Zurich",
    }
    with pytest.raises(ValueError, match="line 1"):
        parse_timezone_map("this is not a listing row\n")


def test_to_local_and_to_utc_are_identity_without_a_zone() -> None:
    """`tz=None` is the "engine clock IS the comparison basis" case: a job
    that declares no timezone and a run that set no base zone (SEM-35 /
    DL-155). Neither direction may invent an offset there."""
    when = datetime(2026, 7, 1, 9, 0)
    assert to_local(when, None) == when
    assert to_utc(when, None) == when


def test_the_conversion_carries_the_dst_edges_at_the_default_fold() -> None:
    """runner-design E10, and it is pinned HERE because DL-163 made this the
    ONE definition the scheduler's ticks and the oracle's run_window both
    cross -- a second spelling would have been a second DST pin.

    `to_utc` does not IMPOSE fold=0; it honours whatever fold the caller's
    datetime carries, and every caller builds a plain `datetime`, whose fold
    is 0. That default is what these instants pin, and the last assertion
    shows the other fold is still reachable by a caller that asks for it.

    Fall back (2026-11-01, America/New_York): 01:30 local happens twice and
    fold=0 is the FIRST, the -04:00 one. Spring forward (2026-03-08): 02:30
    local never happens, and fold=0 maps it past the gap -- back in local
    terms it lands at 03:30, an hour it did have."""
    ny = ZoneInfo("America/New_York")

    assert to_utc(datetime(2026, 11, 1, 1, 30), ny) == datetime(2026, 11, 1, 5, 30)
    # the second occurrence is reachable, but only from the UTC side
    assert to_local(datetime(2026, 11, 1, 5, 30), ny) == datetime(2026, 11, 1, 1, 30)
    assert to_local(datetime(2026, 11, 1, 6, 30), ny) == datetime(2026, 11, 1, 1, 30)

    gap = to_utc(datetime(2026, 3, 8, 2, 30), ny)
    assert gap == datetime(2026, 3, 8, 7, 30)
    assert to_local(gap, ny) == datetime(2026, 3, 8, 3, 30)

    # not a fold=0 pin, a fold=0 DEFAULT: an explicit fold=1 is honoured
    assert to_utc(datetime(2026, 11, 1, 1, 30, fold=1), ny) == datetime(2026, 11, 1, 6, 30)


def test_to_utc_round_trips_every_unambiguous_local_time() -> None:
    """Away from the two edges the pair is an exact inverse, which is what
    lets the scheduler compute a tick in local terms and the oracle compare
    it in engine terms."""
    ny = ZoneInfo("America/New_York")
    for month, day in ((1, 15), (6, 15), (3, 9), (11, 2)):
        local = datetime(2026, month, day, 9, 17)
        assert to_local(to_utc(local, ny), ny) == local


def test_alias_table_reads_an_empty_table_as_no_table_and_copies() -> None:
    """DL-163's whole subject: SEM-35's unique-city default is conditioned on
    the ABSENCE of a ujo_timezones table, so a caller passing `{}` where it
    meant "no map" would retire that default for every per-job zone. DL-151
    was that bug. The copy matters for the same reason the scheduler's own
    `tz_aliases` property copies -- a caller must not be able to reach back
    into a period's pinned table."""
    assert alias_table(None) is None
    assert alias_table({}) is None
    supplied = {"hq": "Europe/Zurich"}
    taken = alias_table(supplied)
    assert taken == supplied
    assert taken is not supplied
    taken["hq"] = "UTC"
    assert supplied == {"hq": "Europe/Zurich"}


def test_alias_table_is_the_switch_between_the_city_default_and_the_map() -> None:
    """The rule stated end to end, because `alias_table`'s answer is what
    `resolve_timezone` reads: an absent table lets the unique-city default
    resolve `Zurich`, and any supplied table -- even one that says nothing
    about Zurich -- turns it off."""
    assert resolve_timezone("Zurich", alias_table(None)) is not None
    assert resolve_timezone("Zurich", alias_table({})) is not None  # {} -> None -> default on
    assert resolve_timezone("Zurich", alias_table({"dallas": "US/Central"})) is None
