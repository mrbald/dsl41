"""The DST drill's pure parts (examples/dst-drill/): the estates, the L023
findings of the DST estate, and the comparator that gives the drill its
verdict. The drill itself needs libfaketime in a container
(.github/workflows/dst-drill.yml); these tests need neither. They check the comparator against the virtual
clock: on a rehearsal compared with itself every row matches, and every L023
claim the drill checks holds on the rehearsal of its scenario.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.cli_common import load_catalog_or_exit_2
from dsl41.lint import rule_l023

DRILL_DIR = Path(__file__).resolve().parent.parent / "examples" / "dst-drill"
WORKFLOW = DRILL_DIR.parents[1] / ".github" / "workflows" / "dst-drill.yml"
L023_TODAY = date(2026, 6, 1)
runner = CliRunner()


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dst_drill", DRILL_DIR / "drill.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dst_drill"] = module
    spec.loader.exec_module(module)
    return module


drill = _load()
compare = drill.compare
BY_NAME = {s.name: s for s in drill.SCENARIOS}


@pytest.fixture
def _l023_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dsl41.lint._reference_today", lambda: L023_TODAY)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ------------------------------------------------------------------ the estate


def test_dst_drill_estate_lowers_with_invented_names_and_the_shapes_it_needs() -> None:
    catalog = load_catalog_or_exit_2([drill.ESTATE], False)
    jobs = catalog.jobs
    assert all(name.startswith("DD_") for name in jobs)
    schedules = {name: job.schedule for name, job in jobs.items() if job.schedule is not None}
    assert any(s.start_times for s in schedules.values())
    assert any(s.start_mins for s in schedules.values())
    assert any(s.must_complete for s in schedules.values())
    assert {name for name, s in schedules.items() if s.run_window} == set(drill.WINDOWED)
    assert jobs["DD_KILL"].sem.term_run_time_min == 5
    assert jobs["DD_AFTER"].sem.condition is not None


def test_dst_drill_estate_lints_as_the_drill_expects() -> None:
    catalog = load_catalog_or_exit_2([drill.ESTATE], False)
    common = {
        ("DD_AT_0100", "start_times 01:00"),
        ("DD_AT_0130", "start_times 01:30"),
        ("DD_MUST", "start_times 01:15"),
        ("DD_MUST", "must_complete_times 01:20"),
        ("DD_WINDOW", "run_window 01:15-01:45"),
        ("DD_WINDOW", "start_mins"),
        ("DD_WINDOW_LATE", "start_mins"),
        ("DD_EVERY", "start_mins"),
    }
    new_york = common | {
        ("DD_AT_0200", "start_times 02:00"),
        ("DD_AT_0230", "start_times 02:30"),
        ("DD_WINDOW_LATE", "run_window 02:15-02:45"),
    }
    for zone, want in (("Europe/London", common), ("America/New_York", new_york)):
        found = rule_l023(catalog, base_tz=zone, today=L023_TODAY)
        assert {(v.jobs[0], v.detail) for v in found} == want, zone


def test_dst_drill_step_estate_ticks_every_minute_and_keeps_a_run_open() -> None:
    catalog = load_catalog_or_exit_2([drill.STEP_ESTATE], False)
    jobs = catalog.jobs
    assert set(jobs) == {"DS_TICK", "DS_AFTER"}
    schedule = jobs["DS_TICK"].schedule
    assert schedule is not None and len(schedule.start_mins) == 60
    assert jobs["DS_AFTER"].sem.condition is not None
    step = BY_NAME["clock-step"].step
    assert step is not None and BY_NAME["clock-step"].speed == 1
    # the step lands inside a run (a tick at :00, 25 s long) and below its tick
    assert step.at.second == 10 and step.back > timedelta(seconds=25)


def test_dst_drill_workflow_runs_every_scenario() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "push:" not in text and "pull_request" not in text
    assert "--init" in text and "examples/dst-drill/drill.py" in text
    assert "Dockerfile" in text and "docker build" in text
    mount = '-v "$RUNNER_TEMP/dst-drill:/tmp/dst-drill"'
    for name in BY_NAME:
        assert f"--init {mount} dsl41-dst-drill python examples/dst-drill/drill.py {name}\n" in text
    # GitHub allows the runner context in a step, not in a job's env
    assert "runner." not in text.split("steps:")[0]
    assert "if: failure()" in text and "upload-artifact" in text


# ----------------------------------------------------- against the virtual clock


def _rehearse(scenario: object, tmp_path: Path) -> tuple[str, list[tuple[str, datetime]]]:
    s = scenario
    plan = tmp_path / "rehearsal.json"
    plan.write_text(json.dumps(drill.REHEARSAL), encoding="utf-8")
    hours = (s.end - s.start).total_seconds() / 3600  # type: ignore[attr-defined]
    root = tmp_path / "rr"
    result = runner.invoke(
        app,
        [
            "rehearse",
            str(s.estate),  # type: ignore[attr-defined]
            "--scenario",
            str(plan),
            "--start",
            s.start.isoformat(),  # type: ignore[attr-defined]
            "--hours",
            f"{hours:.3f}",
            "--timezone",
            s.zone,  # type: ignore[attr-defined]
            "--run-root",
            str(root),
        ],
    )
    assert result.exit_code == 0, result.output
    records = drill._journal_records(root)
    return result.stdout, compare.ticks_of(records)


@pytest.mark.parametrize("name", list(BY_NAME))
def test_dst_drill_scenario_holds_on_its_own_rehearsal(
    name: str, tmp_path: Path, _l023_date: None
) -> None:
    """Each scenario's checks pass when the rehearsal stands in for the real
    run: the comparator does not differ on equal input, and every L023 claim
    the drill checks is what the virtual clock does."""
    s = BY_NAME[name]
    text, ticks = _rehearse(s, tmp_path)
    trace = compare.clip(compare.parse_trace(text), s.start + s.guard, s.end - s.guard)
    assert trace.runs
    rows = compare.compare(trace, trace, tolerance=timedelta(0), ticks=(ticks, ticks))
    assert rows and all(r.ok for r in rows), compare.table(rows)
    if s.change is None:
        return
    lint = runner.invoke(app, ["lint", str(drill.ESTATE), "--timezone", s.zone])
    claims = compare.claims(lint.stdout, s.change, drill.WINDOWED)
    attrs = {c.attr for c in claims}
    assert {"start_times", "run_window", "start_mins"} <= attrs
    rows = compare.check_claims(claims, trace, s.day, ZoneInfo(s.zone))
    assert all(r.ok for r in rows), compare.table(rows)


# ------------------------------------------------------------------ the comparator

TRACE = """\
2026-03-29T00:58:00 DD_KILL INACTIVE->STARTING [STARTJOB event (scheduler)]
2026-03-29T00:58:00 DD_KILL STARTING->RUNNING [QUE_WAIT collapses to immediate (ss7 non-goal)]
2026-03-29T01:00:00 DD_AT_0100 INACTIVE->STARTING [STARTJOB event (scheduler)]
2026-03-29T01:00:00 DD_AT_0100 RUNNING->SUCCESS [injected STATUS]
2026-03-29T01:00:00 DD_AFTER INACTIVE->STARTING [status of 'DD_AT_0100' changed to SUCCESS]
2026-03-29T01:00:00 DD_AFTER RUNNING->SUCCESS [injected STATUS]
2026-03-29T01:03:00 DD_KILL RUNNING->TERMINATED [term_run_time exceeded (dossier ss5)]
2026-03-29T01:15:00 DD_WINDOW RUN_WINDOW_SKIP [outside run_window; not run (STARTJOB event (scheduler))]
2026-03-29T01:20:00 DD_MUST MUST_COMPLETE_ALARM [must_complete_times deadline (SEM-34)]
"""


def _shift(text: str, job: str, seconds: int, what: str = "->STARTING") -> str:
    out = []
    for line in text.splitlines():
        stamp, name, rest = line.split(" ", 2)
        if name == job and what in rest:
            stamp = (_at(stamp) + timedelta(seconds=seconds)).isoformat()
        out.append(f"{stamp} {name} {rest}")
    return "\n".join(out)


def test_dst_drill_parse_trace_reads_starts_statuses_and_timers() -> None:
    trace = compare.parse_trace(TRACE)
    runs = {(r.job, r.made_by, r.status) for r in trace.runs}
    assert runs == {
        ("DD_KILL", "scheduler", "TERMINATED"),
        ("DD_AT_0100", "scheduler", "SUCCESS"),
        ("DD_AFTER", "engine", "SUCCESS"),
    }
    assert {(t.job, t.at, t.what) for t in trace.timers} == {
        ("DD_KILL", _at("2026-03-29T01:03:00"), "term_run_time kill"),
        ("DD_MUST", _at("2026-03-29T01:20:00"), "MUST_COMPLETE_ALARM"),
    }


def _rows(real: str, tolerance: int = 15) -> list[object]:
    return compare.compare(
        compare.parse_trace(real),
        compare.parse_trace(TRACE),
        tolerance=timedelta(seconds=tolerance),
    )


def _differ(rows: list[object]) -> list[tuple[str, str]]:
    return [(r.check, r.job) for r in rows if not r.ok]  # type: ignore[attr-defined]


def test_dst_drill_a_scheduled_start_must_match_to_the_second() -> None:
    assert _differ(_rows(TRACE)) == []
    assert _differ(_rows(_shift(TRACE, "DD_AT_0100", 1))) == [("start", "DD_AT_0100")]


def test_dst_drill_a_follow_on_may_lag_by_the_tolerance_only() -> None:
    assert _differ(_rows(_shift(TRACE, "DD_AFTER", 15))) == []
    assert _differ(_rows(_shift(TRACE, "DD_AFTER", 16))) == [("follow-on start", "DD_AFTER")]
    assert _differ(_rows(_shift(TRACE, "DD_AFTER", -1))) == [("follow-on start", "DD_AFTER")]


def test_dst_drill_timers_statuses_and_missing_runs_differ() -> None:
    late_kill = _shift(TRACE, "DD_KILL", 1, "TERMINATED")
    assert _differ(_rows(late_kill)) == [("term_run_time kill", "DD_KILL")] * 2
    failed = TRACE.replace("DD_AT_0100 RUNNING->SUCCESS", "DD_AT_0100 RUNNING->FAILURE")
    assert _differ(_rows(failed)) == [("start", "DD_AT_0100")]
    missing = "\n".join(line for line in TRACE.splitlines() if "MUST_COMPLETE" not in line)
    assert _differ(_rows(missing)) == [("MUST_COMPLETE_ALARM", "DD_MUST")]
    no_start = "\n".join(line for line in TRACE.splitlines() if "DD_AT_0100 INACTIVE" not in line)
    assert ("start", "DD_AT_0100") in _differ(_rows(no_start))


def test_dst_drill_a_tick_inside_a_downtime_must_be_a_drop() -> None:
    """A rehearsal start inside the downtime is left out, with its follow-on;
    its tick must be a drop in the real journal, and any other tick admitted."""
    reference = compare.parse_trace(TRACE)
    real = compare.parse_trace(
        "\n".join(line for line in TRACE.splitlines() if "DD_AT_0100" not in line)
        .replace("DD_AFTER INACTIVE->STARTING", "DD_GONE INACTIVE->STARTING")
        .replace("DD_AFTER RUNNING", "DD_GONE RUNNING")
    )
    real = compare.Trace(tuple(r for r in real.runs if r.job != "DD_GONE"), real.timers)
    down = [(_at("2026-03-29T00:59:00"), _at("2026-03-29T01:01:00"))]
    tick = ("DD_AT_0100", _at("2026-03-29T01:00:00"))
    kill = ("DD_KILL", _at("2026-03-29T00:58:00"))

    def rows(real_ticks: list[object], drops: list[object]) -> list[tuple[str, str]]:
        return _differ(
            compare.compare(
                real,
                reference,
                tolerance=timedelta(0),
                downtime=down,
                ticks=(real_ticks, [kill, tick]),
                drops=drops,
            )
        )

    assert rows([kill], [tick]) == []
    assert rows([kill, tick], []) == [("tick", "DD_AT_0100")]
    assert rows([kill], []) == [("tick", "DD_AT_0100")]
    assert rows([], [kill, tick]) == [("tick", "DD_KILL")]


LINT = (
    "estate.jil:34: L023 warn: start_times 01:00 of 'DD_AT_0100' is changed by DST in"
    " Europe/London. Under the runner's rules, on a day the clock skips 01:00-01:59 (unverified"
    " shape) it runs at 02:00; on a day the clock repeats 01:00-01:59 it runs at 01:00, once, in"
    " the second pass (UTC+00:00), the same tick as the 00:00 start time (one run; unverified)."
    " Move the time out of the affected hour.\n"
    "estate.jil:98: L023 info: start_mins of 'DD_EVERY' repeats every hour, so a DST change in"
    " Europe/London reaches it. Under the runner's rules, on a day the clock skips 01:00-01:59"
    " (unverified shape), ticks move: 01:15 runs at 02:15, 01:45 runs at 02:45; on a day the"
    " clock repeats 01:00-01:59, ticks run twice: 01:15; run once, in the first pass: 01:45."
    " Move the time out of the affected hour.\n"
    "estate.jil:99: L023 info: start_mins of 'DD_WINDOW' repeats every hour, so a DST change in"
    " Europe/London reaches it. Under the runner's rules, on a day the clock repeats"
    " 01:00-01:59, ticks run twice: 01:00. Move the time out of the affected hour.\n"
)
LONDON = ZoneInfo("Europe/London")
FALL = date(2026, 10, 25)


def test_dst_drill_claims_take_the_change_days_clause_and_skip_windowed_ticks() -> None:
    spring = compare.claims(LINT, "skips 01:00-01:59", ["DD_WINDOW"])
    assert [(c.job, c.attr, c.text) for c in spring] == [
        ("DD_AT_0100", "start_times", "it runs at 02:00"),
        ("DD_EVERY", "start_mins", "ticks move: 01:15 runs at 02:15, 01:45 runs at 02:45"),
    ]
    fall = compare.claims(LINT, "repeats 01:00-01:59", ["DD_WINDOW"])
    assert [c.text for c in fall] == [
        "it runs at 01:00, once, in the second pass (UTC+00:00), the same tick as the 00:00"
        " start time (one run; unverified)",
        "ticks run twice: 01:15; run once, in the first pass: 01:45",
    ]
    assert len(compare.claims(LINT, "repeats 01:00-01:59")) == 3


SANTIAGO = ZoneInfo("America/Santiago")
NEW_YORK = ZoneInfo("America/New_York")
SPRING = date(2026, 3, 29)
SANTIAGO_TEXT = (
    "the window opens at 23:30, in the first pass (UTC-03:00) and closes at 23:59, in the first"
    " pass (UTC-03:00), then opens a second time at 23:30, in the second pass (UTC-04:00) and"
    " closes at 00:30 next day"
)


def test_dst_drill_expectations_resolve_the_pass_and_the_day() -> None:
    fall = compare.claims(LINT, "repeats 01:00-01:59")
    assert compare.expect(fall[0], FALL, LONDON).instants == (_at("2026-10-25T01:00:00"),)
    ticks = compare.expect(fall[1], FALL, LONDON)
    assert ticks.instants == (
        _at("2026-10-25T00:15:00"),
        _at("2026-10-25T01:15:00"),
        _at("2026-10-25T00:45:00"),
    )
    assert ticks.never == (_at("2026-10-25T01:45:00"),)
    first = compare.Claim(
        "J", "must_complete_times", "it is due at 01:20, once, in the first pass (UTC+01:00)"
    )
    assert compare.expect(first, FALL, LONDON).instants == (_at("2026-10-25T00:20:00"),)
    window = compare.Claim("J", "run_window", SANTIAGO_TEXT)
    assert compare.expect(window, date(2026, 4, 4), SANTIAGO).spans == (
        (_at("2026-04-05T02:30:00"), _at("2026-04-05T02:59:00")),
        (_at("2026-04-05T03:30:00"), _at("2026-04-05T04:30:00")),
    )
    open_all_day = compare.Claim(
        "J",
        "run_window",
        "the window does not close that day: it opens at 01:20 previous day and closes at 01:10"
        " next day",
    )
    assert compare.expect(open_all_day, SPRING, LONDON).spans == (
        (_at("2026-03-28T01:20:00"), _at("2026-03-30T00:10:00")),
    )
    gap = compare.expect(
        compare.Claim("J", "start_mins", "ticks do not run: 02:15, 02:45"),
        date(2026, 3, 8),
        NEW_YORK,
    )
    assert gap.gaps == ((_at("2026-03-08T06:45:00"), _at("2026-03-08T07:15:00")),)
    no_pass = compare.Claim("J", "run_window", "the window opens at 01:15 and closes at 01:59")
    no_day = compare.Claim("J", "run_window", "the window opens at 23:30 and closes at 00:30")
    for unread in (no_pass, no_day, compare.Claim("J", "start_times", "it wanders off")):
        with pytest.raises(ValueError):
            compare.expect(unread, FALL, LONDON)


def _starts(job: str, *instants: str) -> str:
    return "\n".join(
        f"{at} {job} INACTIVE->STARTING [STARTJOB event (scheduler)]" for at in instants
    )


def _alarm(job: str, at: str) -> str:
    return f"{at} {job} MUST_COMPLETE_ALARM [must_complete_times deadline (SEM-34)]"


RIGHT_AND_WRONG = [
    # (zone, day, attr, claim text, a run that does it, a run that does not)
    (
        "Europe/London",
        "2026-10-25",
        "start_times",
        "it runs at 01:00, once, in the second pass (UTC+00:00)",
        _starts("J", "2026-10-25T01:00:00"),
        _starts("J", "2026-10-25T00:00:00"),
    ),
    (
        "Europe/London",
        "2026-03-29",
        "start_times",
        "it does not run",
        "",
        _starts("J", "2026-03-29T01:30:00"),
    ),
    (
        "America/New_York",
        "2026-03-08",
        "start_times",
        "it runs at 03:00, the same tick as the 02:00 start time (one run; unverified)",
        _starts("J", "2026-03-08T07:00:00"),
        _starts("J", "2026-03-08T07:00:00", "2026-03-08T07:00:00"),
    ),
    (
        "Europe/London",
        "2026-10-25",
        "must_complete_times",
        "it is due at 01:20, once, in the second pass (UTC+00:00)",
        _alarm("J", "2026-10-25T01:20:00"),
        _alarm("J", "2026-10-25T00:20:00"),
    ),
    (
        "Europe/London",
        "2026-10-25",
        "must_start_times",
        "it is never armed: its start time does not run that day",
        "",
        _alarm("J", "2026-10-25T01:20:00"),
    ),
    (
        "Europe/London",
        "2026-10-25",
        "run_window",
        "the window opens at 01:15, in the second pass (UTC+00:00) and closes at 01:45, in the"
        " second pass (UTC+00:00)",
        _starts("J", "2026-10-25T01:30:00"),
        _starts("J", "2026-10-25T00:30:00"),
    ),
    (
        "America/Santiago",
        "2026-04-04",
        "run_window",
        SANTIAGO_TEXT,
        _starts("J", "2026-04-05T02:45:00", "2026-04-05T04:00:00"),
        _starts("J", "2026-04-05T02:45:00"),
    ),
    (
        "Europe/London",
        "2026-03-29",
        "run_window",
        "the window never opens",
        "",
        _starts("J", "2026-03-29T01:30:00"),
    ),
    (
        "Europe/London",
        "2026-03-29",
        "run_window",
        "the window does not close that day: it opens at 01:20 previous day and closes at 01:10"
        " next day",
        _starts("J", "2026-03-29T12:00:00"),
        _starts("J", "2026-03-29T12:00:00", "2026-03-30T00:30:00"),
    ),
    (
        "Europe/London",
        "2026-03-29",
        "start_mins",
        "ticks move: 01:15 runs at 02:15, 01:45 runs at 02:45",
        _starts("J", "2026-03-29T00:45:00", "2026-03-29T01:15:00", "2026-03-29T01:45:00"),
        _starts(
            "J",
            "2026-03-29T00:45:00",
            "2026-03-29T01:15:00",
            "2026-03-29T01:15:00",
            "2026-03-29T01:45:00",
        ),
    ),
    (
        "Australia/Lord_Howe",
        "2026-10-04",
        "start_mins",
        "ticks move: 02:15 runs at 02:45",
        _starts("J", "2026-10-03T14:45:00", "2026-10-03T15:45:00", "2026-10-03T16:15:00"),
        _starts(
            "J",
            "2026-10-03T14:45:00",
            "2026-10-03T15:30:00",
            "2026-10-03T15:45:00",
            "2026-10-03T16:15:00",
        ),
    ),
    (
        "Europe/London",
        "2026-03-29",
        "start_mins",
        "ticks move: 01:15 runs at 02:15, 01:45 runs at 02:45",
        _starts("J", "2026-03-29T01:15:00", "2026-03-29T01:45:00"),
        _starts("J", "2026-03-29T01:00:00", "2026-03-29T01:15:00", "2026-03-29T01:45:00"),
    ),
    (
        "Europe/London",
        "2026-10-25",
        "start_mins",
        "ticks run twice: 01:15",
        _starts("J", "2026-10-25T00:15:00", "2026-10-25T01:15:00"),
        _starts("J", "2026-10-25T01:15:00"),
    ),
    (
        "Europe/London",
        "2026-10-25",
        "start_mins",
        "ticks run once, in the first pass: 01:45",
        _starts("J", "2026-10-25T00:45:00"),
        _starts("J", "2026-10-25T00:45:00", "2026-10-25T01:45:00"),
    ),
    (
        "America/New_York",
        "2026-03-08",
        "start_mins",
        "ticks do not run: 02:15, 02:45",
        _starts("J", "2026-03-08T06:45:00", "2026-03-08T07:15:00"),
        _starts(
            "J",
            "2026-03-08T06:45:00",
            "2026-03-08T07:00:00",
            "2026-03-08T07:00:30",
            "2026-03-08T07:15:00",
        ),
    ),
]


@pytest.mark.parametrize(("zone", "day", "attr", "text", "right", "wrong"), RIGHT_AND_WRONG)
def test_dst_drill_every_l023_check_can_fail(
    zone: str, day: str, attr: str, text: str, right: str, wrong: str
) -> None:
    """For each kind of L023 claim, a run that does what it states passes and
    a run that does something else fails."""
    claim = compare.Claim("J", attr, text)
    tz, on = ZoneInfo(zone), date.fromisoformat(day)
    assert compare.check_claims([claim], compare.parse_trace(right), on, tz)[0].ok
    assert not compare.check_claims([claim], compare.parse_trace(wrong), on, tz)[0].ok


def test_dst_drill_a_dropped_start_stands_for_the_claimed_one() -> None:
    claim = compare.Claim(
        "DD_AT_0100", "start_times", "it runs at 01:00, once, in the second pass (UTC+00:00)"
    )
    down = [(_at("2026-10-25T00:55:00"), _at("2026-10-25T01:05:00"))]
    dropped = [("DD_AT_0100", _at("2026-10-25T01:00:00"))]
    empty = compare.parse_trace("")
    assert compare.check_claims([claim], empty, FALL, LONDON, downtime=down, drops=dropped)[0].ok
    assert not compare.check_claims([claim], empty, FALL, LONDON, downtime=down)[0].ok


def _dispatch(job: str, at: str) -> dict[str, object]:
    return {"rec": "dispatch", "job": job, "run_number": 1, "started_at": at}


@pytest.mark.parametrize(
    ("dispatched", "ok"),
    [
        ("2026-03-29T01:00:03.500000", True),
        ("2026-03-29T01:00:15", True),
        ("2026-03-29T01:00:15.100000", False),
        ("2026-03-29T00:59:59", False),
        (None, False),
    ],
)
def test_dst_drill_launch_check_bounds_the_dispatch_after_its_tick(
    dispatched: str | None, ok: bool
) -> None:
    """The start is stamped with its tick whatever the clock did, so the
    launch is read from the dispatch record, stamped with the clock."""
    records = [_dispatch("DD_KILL", "2026-03-29T00:58:01")]
    if dispatched is not None:
        records.append(_dispatch("DD_AT_0100", dispatched))
    trace = compare.parse_trace(TRACE)
    rows = {
        r.job: r
        for r in compare.launch_rows(trace, records, timedelta(seconds=15), timedelta(seconds=6))
    }
    assert set(rows) == {"DD_AT_0100", "DD_KILL", "-"}
    assert rows["DD_KILL"].ok
    assert rows["DD_AT_0100"].ok is ok
    if dispatched == "2026-03-29T01:00:03.500000":
        assert compare.max_launch_lag(rows.values()) == 3.5


def test_dst_drill_the_median_launch_catches_a_clock_that_makes_every_launch_late() -> None:
    def median_row(late_s: list[float]) -> object:
        ticks = [f"2026-03-29T0{i}:00:00" for i in range(len(late_s))]
        trace = compare.parse_trace("\n".join(_starts("J", t) for t in ticks))
        records: list[dict[str, object]] = [
            {
                "rec": "dispatch",
                "job": "J",
                "run_number": i,
                "started_at": (_at(t) + timedelta(seconds=s)).isoformat(),
            }
            for i, (t, s) in enumerate(zip(ticks, late_s, strict=True))
        ]
        rows = compare.launch_rows(trace, records, timedelta(seconds=45), timedelta(seconds=6))
        return rows[-1]

    one_slow = median_row([2, 3, 40, 2, 3])
    all_late = median_row([20, 22, 21, 25, 30])
    assert one_slow.check == "launch median" and one_slow.ok  # type: ignore[attr-defined]
    assert not all_late.ok  # type: ignore[attr-defined]


def _outcome(**changes: object) -> dict[str, object]:
    """An `outcome` record of the kill at 01:03:00: the engine's `oracle_kill`
    stop and the wrapper's status, signaled 1.2 s after the deadline."""
    status = {
        "wrapper_outcome": "signaled",
        "signal": 15,
        "ended_at": "2026-03-29T01:03:01.2+00:00",
    }
    record: dict[str, object] = {
        "rec": "outcome",
        "job": "DD_KILL",
        "run_number": 1,
        "outcome": "stopped",
        "cause": "oracle_kill",
        "status": status,
    }
    return record | changes


def _status(**changes: object) -> dict[str, object]:
    status = dict(_outcome()["status"])  # type: ignore[call-overload]
    return {"status": status | changes}


@pytest.mark.parametrize(
    ("records", "ok"),
    [
        ([_outcome()], True),
        ([_outcome(cause="operator")], False),
        ([_outcome(job="DD_OTHER")], False),
        ([_outcome(**_status(wrapper_outcome="exited"))], False),
        ([_outcome(**_status(ended_at="2026-03-29T01:03:20+00:00"))], False),
        ([_outcome(**_status(ended_at="2026-03-29T01:03:06+00:00"))], True),
        ([_outcome(**_status(ended_at="2026-03-29T01:03:06.5+00:00"))], False),
        ([_outcome(**_status(ended_at="2026-03-29T01:02:59+00:00"))], False),
        ([_outcome(status=None)], False),
        ([], False),
    ],
)
def test_dst_drill_kill_check_needs_an_oracle_kill_stop_and_a_signaled_command(
    records: list[dict[str, object]], ok: bool
) -> None:
    trace = compare.parse_trace(TRACE)
    (row,) = compare.kill_rows(trace, records, timedelta(seconds=6))
    assert row.ok is ok


STEP_LOG = (
    "engine up; control socket: rr/control.sock\n"
    "dsl41: WARNING: wall clock below admission frontier: wall=2026-07-15T00:10:47.5"
    " frontier=2026-07-15T00:12:00 gap_s=72.5\n"
    "engine stopped: clean_shutdown\n"
)
BACK = timedelta(seconds=90)


def test_dst_drill_step_warning_is_one_line_with_wall_frontier_and_gap() -> None:
    assert all(r.ok for r in compare.warning_rows(STEP_LOG, BACK))
    none = [r for r in compare.warning_rows("engine up\n", BACK) if not r.ok]
    assert [r.check for r in none] == ["step warning"]
    twice = STEP_LOG + STEP_LOG.splitlines()[1] + "\n"
    assert [r.check for r in compare.warning_rows(twice, BACK) if not r.ok] == ["step warning"]
    other = STEP_LOG.replace("wall clock below admission frontier", "something else")
    assert [r.check for r in compare.warning_rows(other, BACK) if not r.ok] == ["step warning text"]


@pytest.mark.parametrize(
    "edit",
    [
        ("gap_s=72.5", "gap_s=10"),  # not frontier - wall
        ("gap_s=72.5", "gap_s=-72.5"),
        ("wall=2026-07-15T00:10:47.5", "wall=2026-07-15T00:12:30"),  # wall above the frontier
        ("gap_s=72.5", "gap_s=72.5"),  # control: passes
    ],
)
def test_dst_drill_step_warning_text_is_checked(edit: tuple[str, str]) -> None:
    rows = compare.warning_rows(STEP_LOG.replace(*edit), BACK)
    assert all(r.ok for r in rows) is (edit[0] == edit[1])
    # a gap larger than the step cannot be: the frontier never passes the earlier wall
    big = STEP_LOG.replace(
        "frontier=2026-07-15T00:12:00 gap_s=72.5", "frontier=2026-07-15T00:13:00 gap_s=132.5"
    )
    assert not all(r.ok for r in compare.warning_rows(big, BACK))


def test_dst_drill_order_rows_count_backward_stamps() -> None:
    stamps = [_at("2026-07-15T00:12:00"), _at("2026-07-15T00:12:00"), _at("2026-07-15T00:13:00")]
    assert compare.order_row("admission stamps", stamps).ok
    assert not compare.order_row("admission stamps", []).ok  # an empty record proves nothing
    row = compare.order_row("admission stamps", [stamps[2], stamps[0]])
    assert not row.ok and row.got == "1 backward of 2"
    trace = "2026-07-15T00:12:00 J INACTIVE->STARTING [STARTJOB event (scheduler)]\nnoise\n"
    trace += "2026-07-15T00:10:55.5 J RUNNING->SUCCESS [injected STATUS]\n"
    assert compare.trace_stamps(trace) == [_at("2026-07-15T00:12:00"), _at("2026-07-15T00:10:55.5")]
    assert not compare.order_row("trace", compare.trace_stamps(trace)).ok


def test_dst_drill_rejected_row_wants_every_stored_decision_applied() -> None:
    """A refusal writes no record, so only a `rejected` decision can show here."""
    applied = {"rec": "decision", "decision": "applied"}
    assert compare.rejected_row([applied, {"rec": "input"}]).ok
    assert not compare.rejected_row([applied, {"rec": "decision", "decision": "rejected"}]).ok


def test_dst_drill_tick_rows_want_each_tick_once() -> None:
    tick = ("DS_TICK", _at("2026-07-15T00:13:00"))
    assert all(r.ok for r in compare.tick_rows(([tick], [tick])))
    assert not all(r.ok for r in compare.tick_rows(([tick, tick], [tick])))
    assert not all(r.ok for r in compare.tick_rows(([], [tick])))


def _trace(*lines: str) -> compare.Trace:
    return compare.parse_trace("\n".join(lines))


def _start(job: str, at: str, how: str = "scheduler") -> str:
    return f"{at} {job} INACTIVE->STARTING [STARTJOB event ({how})]"


def _end(job: str, at: str, status: str = "SUCCESS") -> str:
    return f"{at} {job} RUNNING->{status} [injected STATUS]"


def test_dst_drill_start_rows_want_the_rehearsals_scheduled_starts() -> None:
    both = _trace(_start("J", "2026-07-15T00:12:00"), _start("J", "2026-07-15T00:13:00"))
    lost = _trace(_start("J", "2026-07-15T00:12:00"))
    twice = _trace(_start("J", "2026-07-15T00:12:00"), _start("J", "2026-07-15T00:12:00"))
    follow = _trace(_start("K", "2026-07-15T00:12:00", "engine"))
    assert all(r.ok for r in compare.start_rows(both, both))
    assert [r.ok for r in compare.start_rows(lost, both)] == [True, False]
    assert [r.ok for r in compare.start_rows(both, lost)] == [True, False]
    assert not all(r.ok for r in compare.start_rows(twice, lost))
    assert compare.start_rows(follow, _trace()) == []  # a follow-on is `chain_rows`'


def test_dst_drill_chain_rows_want_a_success_and_one_follow_on() -> None:
    at, before = _at("2026-07-15T00:12:00"), _at("2026-07-15T00:13:00")
    ok = _trace(
        _start("P", "2026-07-15T00:12:00"),
        _end("P", "2026-07-15T00:12:00"),
        _start("F", "2026-07-15T00:12:00", "engine"),
    )

    def differ(trace: compare.Trace) -> list[str]:
        return [r.check for r in compare.chain_rows(trace, "P", at, "F", before) if not r.ok]

    assert differ(ok) == []
    assert differ(_trace()) == ["run ends", "follow-on count"]
    stuck = _trace(_start("P", "2026-07-15T00:12:00"), _start("F", "2026-07-15T00:12:00", "engine"))
    assert differ(stuck) == ["run ends"]
    failed = _trace(
        _start("P", "2026-07-15T00:12:00"),
        _end("P", "2026-07-15T00:12:00", "FAILURE"),
        _start("F", "2026-07-15T00:12:00", "engine"),
    )
    assert differ(failed) == ["run ends"]
    no_follow = _trace(_start("P", "2026-07-15T00:12:00"), _end("P", "2026-07-15T00:12:00"))
    assert differ(no_follow) == ["follow-on count"]
    two = _trace(
        _start("P", "2026-07-15T00:12:00"),
        _end("P", "2026-07-15T00:12:00"),
        _start("F", "2026-07-15T00:12:00", "engine"),
        _start("F", "2026-07-15T00:12:30", "engine"),
    )
    assert differ(two) == ["follow-on count"]
    late = _trace(
        _start("P", "2026-07-15T00:12:00"),
        _end("P", "2026-07-15T00:12:00"),
        _start("F", "2026-07-15T00:13:00", "engine"),
    )
    assert differ(late) == ["follow-on count"]  # the next minute's, not this run's


def _step_inputs(tmp_path: Path) -> tuple[object, object, str]:
    """The clock-step verdict's inputs for a run that did what the rehearsal did."""
    s = BY_NAME["clock-step"]
    text, ticks = _rehearse(s, tmp_path)
    root = tmp_path / "rr"
    given = drill.StepInputs(
        STEP_LOG,
        text,
        drill._input_stamps(root),
        drill._journal_records(root),
        (ticks, ticks),
    )
    reference = compare.clip(compare.parse_trace(text), s.start + s.guard, s.end - s.guard)
    return given, reference, text


def _step_differ(given: object, reference: object) -> list[str]:
    s = BY_NAME["clock-step"]
    rows = drill._step_rows(s, given, reference, timedelta(seconds=drill.LAUNCH_S))
    return [r.check for r in rows if not r.ok]


def test_dst_drill_step_verdict_passes_a_run_that_matches_its_rehearsal(tmp_path: Path) -> None:
    given, reference, text = _step_inputs(tmp_path)
    assert "DS_TICK" in text and "DS_AFTER" in text
    assert _step_differ(given, reference) == []


def test_dst_drill_step_verdict_fails_a_lost_start_an_empty_trace_or_a_stuck_run(
    tmp_path: Path,
) -> None:
    given, reference, text = _step_inputs(tmp_path)
    lines = text.splitlines()
    replace = dataclasses.replace

    # the 00:13 start is missing from the real trace; its tick was admitted
    kept = [line for line in lines if not line.startswith("2026-07-15T00:13:00 DS_TICK")]
    assert len(kept) < len(lines)
    lost = replace(given, trace_text="\n".join(kept))
    assert "scheduled start" in _step_differ(lost, reference)

    # `dsl41 journal` printed nothing: no order, no starts, no run to end
    empty = replace(given, trace_text="")
    failing = _step_differ(empty, reference)
    assert {"trace", "scheduled start", "run ends", "follow-on count"} <= set(failing)

    # the 00:12 run never ends, so its follow-on never starts
    stuck = [
        line
        for line in lines
        if not (line.startswith("2026-07-15T00:12") and "DS_" in line and "->SUCCESS" in line)
        and not (line.startswith("2026-07-15T00:12") and "DS_AFTER" in line)
    ]
    assert {"run ends", "follow-on count"} <= set(
        _step_differ(replace(given, trace_text="\n".join(stuck)), reference)
    )


def test_dst_drill_launch_numbering_follows_the_whole_trace(tmp_path: Path) -> None:
    """The nth start of a job is the nth run number, counted over the whole trace: a
    start inside the guard (clipped away) still takes a number, and a replacement
    attempt's repeat of a run number is the same run."""
    early, late = "2026-07-15T00:10:00", "2026-07-15T00:20:00"
    full = _trace(_start("J", early), _start("J", late))
    clipped = compare.clip(full, _at("2026-07-15T00:15:00"), _at("2026-07-15T00:30:00"))
    assert [r.at for r in clipped.runs] == [_at(late)]
    two = timedelta(seconds=3)
    records = [
        _dispatch("J", "2026-07-15T00:10:01") | {"run_number": 1},
        _dispatch("J", "2026-07-15T00:20:01") | {"run_number": 2},
    ]

    def lag(rows: list[object]) -> str:
        return next(r.got for r in rows if r.check == "launch")  # type: ignore[attr-defined]

    right = compare.launch_rows(clipped, records, two, two, numbering=full)
    assert lag(right) == "+1.0s"
    # numbered from the clipped trace, the late start would take run 1: 10 minutes early
    wrong = compare.launch_rows(clipped, records, two, two)
    assert not [r for r in wrong if r.check == "launch"][0].ok
    # a replacement attempt repeats run 2 later; only the first dispatch counts
    again = [*records, _dispatch("J", "2026-07-15T00:20:09") | {"run_number": 2}]
    assert lag(compare.launch_rows(clipped, again, two, two, numbering=full)) == "+1.0s"


def test_dst_drill_reads_a_run_root_as_the_verdict_does(tmp_path: Path) -> None:
    """`_journal_records` gives the comparator its ticks and dispatches from
    the store, and `_input_stamps` the admission stamps in store order."""
    s = BY_NAME["clock-step"]
    _, ticks = _rehearse(s, tmp_path)
    root = tmp_path / "rr"
    records = drill._journal_records(root)
    assert {"input", "decision", "dispatch"} <= {r["rec"] for r in records}
    assert compare.ticks_of(records) == ticks and ticks
    stamps = drill._input_stamps(root)
    assert stamps and compare.order_row("admission stamps", stamps).ok


def test_dst_drill_table_lists_every_difference() -> None:
    rows = _rows(_shift(TRACE, "DD_AT_0100", 1))
    text = compare.table(rows)
    assert "DIFFER" in text and "DD_AT_0100" in text and "DD_KILL" not in text
    assert "DD_KILL" in compare.table(rows, all_rows=True)


def test_dst_drill_leaves_sys_path_alone() -> None:
    assert str(DRILL_DIR) not in sys.path
    assert "compare" not in sys.modules or "dst-drill" not in str(sys.modules["compare"])


def test_dst_drill_bounds_a_kill_tighter_than_a_launch() -> None:
    """A kill starts no process, so a kill late by a grace period must fail
    even though a launch that late would pass."""
    assert drill.KILL_S * 60 <= 6 < drill.LAUNCH_S * 60
    late = _outcome(**_status(ended_at="2026-03-29T01:03:30+00:00"))
    trace = compare.parse_trace(TRACE)
    kill = timedelta(seconds=drill.KILL_S * 60)
    (row,) = compare.kill_rows(trace, [late], kill)
    assert not row.ok


def test_dst_drill_lists_its_scenarios() -> None:
    assert set(BY_NAME) == {
        "london-spring",
        "london-fall",
        "newyork-spring",
        "newyork-fall",
        "london-midnight",
        "london-fall-restart",
        "clock-step",
    }
    assert drill.main(["--list"]) == 0
