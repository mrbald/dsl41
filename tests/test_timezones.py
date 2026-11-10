"""SEM-35 name resolution and the naive-UTC <-> local conversion.

The ladder's own tests, moved here with the module by DL-163 (they were in
test_runner_scheduler.py, which is now one of its callers). Tests that build
a Scheduler stayed there: those pin the caller, not the ladder.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from struct import pack
from zoneinfo import ZoneInfo

import pytest

from dsl41 import timezones
from dsl41.timezones import (
    DstStartTimes,
    alias_table,
    city_candidates,
    dst_change,
    dst_change_near,
    dst_windows,
    map_entry,
    must_instant,
    parse_timezone_map,
    resolve_timezone,
    start_mins_instants,
    start_time_instants,
    to_local,
    to_utc,
    wall_window_contains,
    wall_window_intervals,
    window_span,
    window_spans_near,
)


def test_map_entry_uses_the_resolver_fold_without_resolving() -> None:
    aliases = parse_timezone_map("Local-Zone Zurich\nLoop Loop\n")
    assert map_entry("LOCAL_zone", aliases) == "Zurich"
    assert map_entry("loop", aliases) == "Loop"
    assert map_entry("missing", aliases) is None
    assert map_entry("LOCAL-zone", {}) is None


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


@pytest.mark.usefixtures("fixed_zone_names")
def test_resolve_timezone_map_suppresses_the_city_default() -> None:
    """(DL-62): a supplied listing is complete estate truth -- a city name
    missing from it does NOT fall back to the zoneinfo city match."""
    assert resolve_timezone("Zurich") is not None
    assert resolve_timezone("Zurich", {"dallas": "US/Central"}) is None


@pytest.mark.usefixtures("fixed_zone_names")
def test_resolve_timezone_ambiguous_city_component_is_refused() -> None:
    """(DL-62): the city default requires a UNIQUE match; multiple zones
    sharing a final path component resolve to none (the preflight ERROR
    names the candidates)."""
    assert resolve_timezone("x") is None
    assert resolve_timezone("X") is None
    assert city_candidates("X") == ("A/X", "B/X")


@pytest.mark.usefixtures("fixed_zone_names")
def test_city_aliases_count_once_and_report_the_deepest_name() -> None:
    """DL-477: identical zone data makes the two Indianapolis names one candidate."""
    assert city_candidates("indianapolis") == ("America/Indiana/Indianapolis",)
    resolved = resolve_timezone("Indianapolis")
    assert resolved is not None
    assert (resolved.zone, resolved.how) == ("America/Indiana/Indianapolis", "city")


def test_city_alias_grouping_keeps_stored_tokens_and_hashes(
    fixed_zone_names: dict[str, bytes | None],
) -> None:
    """DL-477: resolving either inventory leaves configuration bytes and hashes unchanged."""
    from dsl41.ir import lower_source
    from dsl41.runner_config import Configuration, RuntimeProfile
    from dsl41.runner_scheduler import Scheduler

    catalog = lower_source(
        "insert_job: J\njob_type: c\ncommand: true\nmachine: localhost\n"
        'date_conditions: 1\nstart_times: "08:00"\ntimezone: Indianapolis\n'
    )
    row = Configuration(catalog=catalog, profile=RuntimeProfile(default_tz="Indianapolis"))
    before = row.to_bytes(), row.digest(), row.catalog_hash, row.runtime_hash
    for legacy in (True, False):
        if not legacy:
            fixed_zone_names.pop("America/Indianapolis")
            timezones._zone_tables.cache_clear()
        scheduler = Scheduler(catalog, start=datetime(2026, 7, 1), default_tz="Indianapolis")
        assert scheduler.next_occurrence() == datetime(2026, 7, 1, 12)
        assert (row.to_bytes(), row.digest(), row.catalog_hash, row.runtime_hash) == before
    schedule = catalog.jobs["J"].schedule
    assert schedule is not None and schedule.timezone == "Indianapolis"
    assert (
        Configuration.from_bytes(row.to_bytes(), where="fixture").profile.default_tz
        == "Indianapolis"
    )


@pytest.mark.parametrize("data", [b"A/X", b"", None])
def test_city_groups_keep_unreadable_names_distinct_and_break_ties_by_name(
    fixed_zone_names: dict[str, bytes | None], data: bytes | None
) -> None:
    """DL-477: even two failed reads are separate; empty bytes are a data identity."""
    fixed_zone_names.update({"A/X": data, "B/X": data})
    assert city_candidates("X") == (("A/X", "B/X") if data is None else ("A/X",))


def test_city_unreadable_alias_does_not_merge_with_readable_data(
    fixed_zone_names: dict[str, bytes | None],
) -> None:
    fixed_zone_names["America/Indianapolis"] = None
    assert city_candidates("Indianapolis") == (
        "America/Indiana/Indianapolis",
        "America/Indianapolis",
    )
    assert resolve_timezone("Indianapolis") is None


@pytest.mark.usefixtures("fixed_zone_names")
def test_zone_tables_cache_reads_only_collisions_and_is_rebuilt_and_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = timezones._zone_tables()
    reads: list[str] = []

    def read(name: str) -> bytes:
        reads.append(name)
        return b"same zone"

    try:
        with monkeypatch.context() as patch:
            patch.setattr(timezones, "available_timezones", lambda: {"A/X", "B/Deep/X", "A/Y"})
            patch.setattr(timezones, "_zone_bytes", read)
            timezones._zone_tables.cache_clear()
            rebuilt = timezones._zone_tables()
            assert rebuilt[1] == {"x": ("B/Deep/X",), "y": ("A/Y",)}
            assert timezones._zone_tables() is rebuilt
            assert sorted(reads) == ["A/X", "B/Deep/X"]
    finally:
        timezones._zone_tables.cache_clear()
    assert timezones._zone_tables() == original


@pytest.fixture
def zone_search_path() -> Iterator[Callable[[tuple[str, ...]], None]]:
    original = timezones.zoneinfo.TZPATH
    try:
        yield lambda paths: timezones.zoneinfo.reset_tzpath(to=paths)
    finally:
        timezones.zoneinfo.reset_tzpath(to=original)


def test_zone_bytes_searches_host_paths_before_the_optional_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone_search_path: Callable[[tuple[str, ...]], None],
) -> None:
    roots = [tmp_path / name for name in ("first", "second", "package")]
    for root in roots:
        (root / "Area").mkdir(parents=True)
        (root / "Area/City").write_bytes(root.name.encode())
    zone_search_path(tuple(str(root) for root in roots[:2]))
    packages: list[str] = []

    def package_files(package: str) -> Path:
        packages.append(package)
        return roots[2]

    monkeypatch.setattr(timezones, "files", package_files)
    for root in roots:
        assert timezones._zone_bytes("Area/City") == root.name.encode()
        (root / "Area/City").unlink()
    assert packages == ["tzdata.zoneinfo"]
    assert timezones._zone_bytes("Area/City") is None


@pytest.mark.parametrize("error", [ModuleNotFoundError, OSError, ValueError])
def test_zone_bytes_missing_or_unreadable_package_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    zone_search_path: Callable[[tuple[str, ...]], None],
) -> None:
    zone_search_path(())

    def unavailable(package: str) -> Path:
        raise error(package)

    monkeypatch.setattr(timezones, "files", unavailable)
    assert timezones._zone_bytes("Area/City") is None


def test_zone_bytes_does_not_replace_an_unreadable_host_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone_search_path: Callable[[tuple[str, ...]], None],
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    names = {"America/Indianapolis", "America/Indiana/Indianapolis"}
    for root in (first, second):
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"same zone")
    zone_search_path((str(first), str(second)))

    def unreadable(path: Path) -> bytes:
        if path == first / "America/Indianapolis":
            raise PermissionError(str(path))
        return b"later source"

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    monkeypatch.setattr(timezones, "files", lambda package: second)
    monkeypatch.setattr(timezones, "available_timezones", lambda: names)
    timezones._zone_tables.cache_clear()
    try:
        assert timezones._zone_bytes("America/Indianapolis") is None
        assert len(city_candidates("Indianapolis")) == 2
        assert resolve_timezone("Indianapolis") is None
    finally:
        timezones._zone_tables.cache_clear()


def test_inaccessible_earlier_directory_keeps_indianapolis_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zone_search_path: Callable[[tuple[str, ...]], None],
) -> None:
    blocked, readable = tmp_path / "blocked", tmp_path / "readable"
    names = {"America/Indianapolis", "America/Indiana/Indianapolis"}
    data = b"TZif" + b"\0" * 16 + pack(">6l", 0, 0, 0, 0, 1, 4) + pack(">lbb", 0, 0, 0) + b"UTC\0"
    for name in names:
        path = readable / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    zone_search_path((str(blocked), str(readable)))
    original_stat, original_read = timezones.os.stat, Path.read_bytes

    def stat(path, *args, **kwargs):
        if Path(path).is_relative_to(blocked):
            raise PermissionError(str(path))
        return original_stat(path, *args, **kwargs)

    def read(path: Path) -> bytes:
        if path.is_relative_to(blocked):
            raise PermissionError(str(path))
        return original_read(path)

    monkeypatch.setattr(timezones.os, "stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(timezones, "available_timezones", lambda: names)
    monkeypatch.setattr(timezones, "ZoneInfo", ZoneInfo.no_cache)
    timezones._zone_tables.cache_clear()
    try:
        assert ZoneInfo.no_cache("America/Indiana/Indianapolis").utcoffset(None) == timedelta(0)
        resolved = resolve_timezone("Indianapolis")
        assert resolved is not None and resolved.zone == "America/Indiana/Indianapolis"
    finally:
        timezones._zone_tables.cache_clear()


def test_real_zone_database_city_groups_follow_data_identity() -> None:
    """DL-477: each host's collisions partition by bytes, independent of its name inventory."""
    leaves: dict[str, list[str]] = {}
    for name in timezones.available_timezones():
        leaf = name.rsplit("/", 1)[-1].casefold().replace("-", "_")
        leaves.setdefault(leaf, []).append(name)
    timezones._zone_tables.cache_clear()
    try:
        for leaf, names in leaves.items():
            if len(names) < 2:
                continue
            data = {name: timezones._zone_bytes(name) for name in names}
            representatives = city_candidates(leaf)
            for name, contents in data.items():
                group = [
                    representative
                    for representative in representatives
                    if representative == name
                    or (contents is not None and data[representative] == contents)
                ]
                assert len(group) == 1, (leaf, name, representatives)
                representative = group[0]
                assert representative.count("/") >= name.count("/")
                if representative.count("/") == name.count("/"):
                    assert representative <= name
        canonical, alias = "America/Indiana/Indianapolis", "America/Indianapolis"
        names = timezones.available_timezones()
        canonical_data = timezones._zone_bytes(canonical)
        if canonical in names and (
            alias not in names
            or (canonical_data is not None and canonical_data == timezones._zone_bytes(alias))
        ):
            resolved = resolve_timezone("Indianapolis")
            assert resolved is not None and resolved.zone == "America/Indiana/Indianapolis"
    finally:
        timezones._zone_tables.cache_clear()


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
    datetime carries. Calendar row ticks, and start times under
    dst-start-times=fold0, are plain `datetime`s, whose fold is 0, and that
    default is what these instants pin. The callers that differ follow the
    vendor's rules: `start_time_instants` passes fold=1 for a start time in
    the repeated hour (DL-260), and the oracle's window math below. Near a documented DST change,
    `_window_span` passes fold=1 for a window opening in the repeated hour,
    the vendor's standard-time pass (DL-249). A change of another shape, such
    as Europe/Berlin's fall change that repeats 02:00-02:59, keeps the
    wall-time path: it passes a `to_local` result through
    `_next_occurrence`/`_prev_occurrence`, both of which preserve fold, so on
    the second occurrence of an ambiguous local time it hands `to_utc` a
    fold=1 datetime -- and the fold=1 answer is the RIGHT one there, since the
    fold=0 instant is in the past and would make `to_open` negative. The last
    assertion pins that honouring in `to_utc` itself, on New York's instants;
    New York's fall change no longer reaches the wall-time path.

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


@pytest.mark.usefixtures("fixed_zone_names")
def test_alias_table_is_the_switch_between_the_city_default_and_the_map() -> None:
    """The rule stated end to end, because `alias_table`'s answer is what
    `resolve_timezone` reads: an absent table lets the unique-city default
    resolve `Zurich`, and any supplied table -- even one that says nothing
    about Zurich -- turns it off."""
    assert resolve_timezone("Zurich", alias_table(None)) is not None
    assert resolve_timezone("Zurich", alias_table({})) is not None  # {} -> None -> default on
    assert resolve_timezone("Zurich", alias_table({"dallas": "US/Central"})) is None


def test_sem33_dst_change_names_only_the_documented_one_hour_shape() -> None:
    """DL-249: the vendor's run_window DST rules are scoped by offset shape:
    one-hour changes where spring skips 02:00-02:59 and fall repeats
    01:00-01:59. Europe/Berlin's and Australia/Sydney's spring changes have
    that shape and their fall changes (02:00-02:59 repeated) do not;
    Europe/London's fall change has it and its spring change (01:00-01:59
    skipped) does not. A half-hour change, a zone
    without DST, a fixed offset and no zone are never a change."""
    ny = ZoneInfo("America/New_York")
    assert dst_change(date(2026, 3, 8), ny) == "spring"
    assert dst_change(date(2026, 11, 1), ny) == "fall"
    assert dst_change(date(2026, 3, 9), ny) is None
    berlin = ZoneInfo("Europe/Berlin")
    assert dst_change(date(2026, 3, 29), berlin) == "spring"
    assert dst_change(date(2026, 10, 25), berlin) is None
    london = ZoneInfo("Europe/London")
    assert dst_change(date(2026, 3, 29), london) is None
    assert dst_change(date(2026, 10, 25), london) == "fall"
    sydney = ZoneInfo("Australia/Sydney")
    assert dst_change(date(2026, 10, 4), sydney) == "spring"
    assert dst_change(date(2026, 4, 5), sydney) is None
    lord_howe = ZoneInfo("Australia/Lord_Howe")
    assert dst_change(date(2026, 4, 5), lord_howe) is None
    assert dst_change(date(2026, 10, 4), lord_howe) is None
    assert dst_change(date(2026, 3, 8), ZoneInfo("America/Phoenix")) is None
    assert dst_change(date(2026, 3, 8), timezone(timedelta(hours=-5))) is None
    assert dst_change(date(2026, 3, 8), None) is None


def test_sem33_dst_change_near_looks_two_days_either_side() -> None:
    """DL-249: the guard that sends a run_window check to the interval path
    spans the change day and two days either side, never three. No zone is
    never near a change, and a day outside the calendar's range counts as
    no change, at both ends."""
    ny = ZoneInfo("America/New_York")
    for day in (date(2026, 3, 6), date(2026, 3, 8), date(2026, 3, 10)):
        assert dst_change_near(day, ny)
    for day in (date(2026, 3, 5), date(2026, 3, 11)):
        assert not dst_change_near(day, ny)
    assert not dst_change_near(date(2026, 3, 8), None)
    for day in (date.min, date(1, 1, 2), date(9999, 12, 30), date.max):
        assert not dst_change_near(day, ny)
        assert not dst_change_near(day, None)


_NY_ZONE = ZoneInfo("America/New_York")


@pytest.mark.parametrize("dst", ["vendor", "fold0"])
def test_sem32_dst_start_time_instants_pair_each_instant_with_its_index(dst: DstStartTimes) -> None:
    """DL-260: the one conversion the scheduler ticks and the oracle names
    slots from. Each instant carries its entry's index in the list as
    written, and entries come back in wall-time order. On 2026-03-08 in
    America/New_York the vendor runs the first missing-hour start time at
    03:00 plus its minute as seconds and drops the second; fold=0 maps both
    past the gap, where 02:45 shares 03:45's instant."""
    day = date(2026, 3, 8)
    times = [(3, 45), (2, 45), (2, 50), (1, 0)]
    got = start_time_instants(day, times, _NY_ZONE, dst=dst)
    if dst == "vendor":
        assert got == [
            (3, datetime(2026, 3, 8, 6, 0)),
            (1, datetime(2026, 3, 8, 7, 0, 45)),
            (0, datetime(2026, 3, 8, 7, 45)),
        ]
    else:
        assert got == [
            (3, datetime(2026, 3, 8, 6, 0)),
            (1, datetime(2026, 3, 8, 7, 45)),
            (2, datetime(2026, 3, 8, 7, 50)),
            (0, datetime(2026, 3, 8, 7, 45)),
        ]


@pytest.mark.parametrize("dst", ["vendor", "fold0"])
def test_sem32_dst_conversions_ignore_the_switch_away_from_a_documented_change(
    dst: DstStartTimes,
) -> None:
    """DL-260: an ordinary day, a zone without DST, a fixed offset, the
    engine clock (no zone) and a change of another shape all convert at
    fold=0 under either switch value. Europe/Berlin's fall change repeats
    02:00-02:59, so its 02:30 is the first, summer-time pass."""
    berlin = ZoneInfo("Europe/Berlin")
    phoenix = ZoneInfo("America/Phoenix")
    cases = [
        (date(2026, 3, 15), _NY_ZONE, datetime(2026, 3, 15, 6, 5)),
        (date(2026, 3, 8), phoenix, datetime(2026, 3, 8, 9, 5)),
        (date(2026, 3, 8), timezone(timedelta(hours=-5)), datetime(2026, 3, 8, 7, 5)),
        (date(2026, 3, 8), None, datetime(2026, 3, 8, 2, 5)),
    ]
    for day, tz, expected in cases:
        assert start_time_instants(day, [(2, 5)], tz, dst=dst) == [(0, expected)]
        assert start_mins_instants(day, [(2, 5)], tz, dst=dst) == [expected]
    assert start_time_instants(date(2026, 10, 25), [(2, 30)], berlin, dst=dst) == [
        (0, datetime(2026, 10, 25, 0, 30))
    ]


def test_sem32_dst_start_mins_instants_follow_the_switch() -> None:
    """DL-260, "Standard Time Changes": start_mins "run in both hours" of
    the repeated 01:00-01:59; "Daylight Time Changes": no tick exists in the
    missing hour. fold=0 keeps the first pass only and maps a missing-hour
    tick past the gap, onto the real tick an hour later."""
    fall = [(1, 0), (1, 30)]
    assert start_mins_instants(date(2026, 11, 1), fall, _NY_ZONE, dst="vendor") == [
        datetime(2026, 11, 1, 5, 0),
        datetime(2026, 11, 1, 6, 0),
        datetime(2026, 11, 1, 5, 30),
        datetime(2026, 11, 1, 6, 30),
    ]
    assert start_mins_instants(date(2026, 11, 1), fall, _NY_ZONE, dst="fold0") == [
        datetime(2026, 11, 1, 5, 0),
        datetime(2026, 11, 1, 5, 30),
    ]
    spring = [(2, 20), (3, 20)]
    assert start_mins_instants(date(2026, 3, 8), spring, _NY_ZONE, dst="vendor") == [
        datetime(2026, 3, 8, 7, 20)
    ]
    assert start_mins_instants(date(2026, 3, 8), spring, _NY_ZONE, dst="fold0") == [
        datetime(2026, 3, 8, 7, 20),
        datetime(2026, 3, 8, 7, 20),
    ]


def test_dst_windows_lists_each_artifact_hour_once_with_its_shape() -> None:
    windows = dst_windows(_NY_ZONE, date(2026, 6, 1))
    assert [(w.kind, w.label, w.documented, w.day) for w in windows] == [
        ("overlap", "01:00-01:59", True, date(2026, 11, 1)),
        ("gap", "02:00-02:59", True, date(2026, 3, 8)),
    ]
    assert dst_windows(ZoneInfo("Asia/Tokyo"), date(2026, 6, 1)) == ()


def test_dst_windows_marks_a_shape_dst_change_does_not_name() -> None:
    windows = dst_windows(ZoneInfo("Europe/London"), date(2026, 6, 1))
    assert {(w.kind, w.documented) for w in windows} == {("gap", False), ("overlap", True)}


def test_wall_window_contains_is_inclusive_and_crosses_midnight() -> None:
    assert wall_window_contains(time(9, 0), time(9, 0), time(17, 0))
    assert wall_window_contains(time(17, 0), time(9, 0), time(17, 0))
    assert not wall_window_contains(time(17, 1), time(9, 0), time(17, 0))
    assert wall_window_contains(time(23, 30), time(23, 0), time(1, 0))
    assert wall_window_contains(time(0, 30), time(23, 0), time(1, 0))
    assert not wall_window_contains(time(12, 0), time(23, 0), time(1, 0))


def test_window_span_and_spans_near_agree_on_the_spring_window() -> None:
    lo, hi = time(2, 15), time(2, 45)
    opens, closes = window_span(date(2026, 3, 8), lo, hi, _NY_ZONE)
    assert (to_local(opens, _NY_ZONE), to_local(closes, _NY_ZONE)) == (
        datetime(2026, 3, 8, 3, 0),
        datetime(2026, 3, 8, 3, 45),
    )
    spans = window_spans_near(date(2026, 3, 8), lo, hi, _NY_ZONE)
    assert spans is not None and spans[2] == (opens, closes)
    assert window_spans_near(date(2026, 6, 1), lo, hi, _NY_ZONE) is None


def test_must_instant_moves_a_missing_hour_must_time_and_pairs_the_start() -> None:
    day = date(2026, 3, 8)
    assert must_instant(day, (2, 5), (2, 10), _NY_ZONE) == datetime(2026, 3, 8, 7, 0, 10)
    assert must_instant(day, (2, 30), (3, 0), _NY_ZONE) == datetime(2026, 3, 8, 7, 0, 59)


def test_wall_window_intervals_lists_each_opening_with_its_wall_ends() -> None:
    london = ZoneInfo("Europe/London")
    usual = wall_window_intervals(
        date(2026, 3, 22), date(2026, 3, 24), time(0, 30), time(1, 30), london
    )
    assert [(o.day, o.strftime("%H:%M"), c.strftime("%H:%M")) for o, c in usual] == [
        (22, "00:30", "01:30"),
        (23, "00:30", "01:30"),
        (24, "00:30", "01:30"),
    ]
    spring = wall_window_intervals(
        date(2026, 3, 28), date(2026, 3, 30), time(0, 30), time(1, 30), london
    )
    assert [(o.day, c.strftime("%H:%M")) for o, c in spring] == [
        (28, "01:30"),
        (29, "00:59"),
        (30, "01:30"),
    ]
