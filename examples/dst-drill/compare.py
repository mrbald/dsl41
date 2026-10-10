"""The DST drill's verdict: pure functions over trace text, journal records and
lint output. No clock, no processes; tests/test_dst_drill.py covers them.

A trace line is `<UTC instant> <job> <what> [<reason>]`, the format of
`dsl41 rehearse --format text` and `dsl41 journal`. The real run is compared
with the rehearsal over the same window:

- a start the scheduler made matches to the second. The engine stamps it at
  the computed tick, not at its clock reading, so this checks the DST
  computation, not the timing;
- the timing is the launch check: each scheduled start's `dispatch` record,
  stamped with the engine's clock reading, comes at most the tolerance after
  its tick;
- a start the engine made from another job's status (a follow-on) may lag by
  at most the tolerance: real launch latency times the clock speed;
- an engine timer (a must alarm, a `term_run_time` kill) matches to the
  second; it too is stamped at the computed deadline. A kill's KILL effect
  must be applied, and the wrapper's spool must show the command signaled
  within the tolerance of the deadline;
- each run ends in the same status;
- a scheduled start the rehearsal makes while the real engine was down is a
  `drop` record in the real journal at the same instant (E9), and a
  follow-on of it does not happen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

TRACE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?) (\S+) (\S+) \[(.*)\]$")
TERMINAL = ("SUCCESS", "FAILURE", "TERMINATED")
TIMERS = ("MUST_START_ALARM", "MUST_COMPLETE_ALARM")


@dataclass(frozen=True)
class Run:
    """One start of a job, how it was made, and the status it ended in."""

    job: str
    at: datetime
    made_by: Literal["scheduler", "engine"]
    status: str | None = None


@dataclass(frozen=True)
class Timer:
    """An event the engine's own timers make: a must alarm or a kill."""

    job: str
    at: datetime
    what: str


@dataclass(frozen=True)
class Trace:
    runs: tuple[Run, ...]
    timers: tuple[Timer, ...]


@dataclass(frozen=True)
class Row:
    """One line of the verdict table."""

    check: str
    job: str
    expected: str
    got: str
    ok: bool


def parse_trace(text: str) -> Trace:
    """The starts, end statuses and timer events of a trace."""
    runs: list[Run] = []
    timers: list[Timer] = []
    open_run: dict[str, int] = {}
    for line in text.splitlines():
        found = TRACE.match(line.strip())
        if not found:
            continue
        stamp, job, what, reason = found.groups()
        at = datetime.fromisoformat(stamp).replace(microsecond=0)
        if what.endswith("->STARTING"):
            made_by: Literal["scheduler", "engine"] = (
                "scheduler" if "(scheduler)" in reason else "engine"
            )
            open_run[job] = len(runs)
            runs.append(Run(job, at, made_by))
            continue
        status = what.rpartition("->")[2]
        if "->" in what and status in TERMINAL and job in open_run:
            index = open_run.pop(job)
            runs[index] = Run(job, runs[index].at, runs[index].made_by, status)
            if "term_run_time" in reason:
                timers.append(Timer(job, at, "term_run_time kill"))
        elif what in TIMERS:
            timers.append(Timer(job, at, what))
    return Trace(tuple(runs), tuple(timers))


def clip(trace: Trace, first: datetime, last: datetime) -> Trace:
    """The runs that start, and the timers that fire, inside [first, last]."""
    return Trace(
        tuple(r for r in trace.runs if first <= r.at <= last),
        tuple(t for t in trace.timers if first <= t.at <= last),
    )


Tick = tuple[str, datetime]


def _records(records: Iterable[Mapping[str, object]], rec: str) -> list[Tick]:
    out = []
    for record in records:
        payload = record.get("payload")
        if record.get("rec") != rec or record.get("kind") != "STARTJOB":
            continue
        if rec == "input" and record.get("source") != "scheduler":
            continue
        if isinstance(payload, dict) and "job" in payload:
            at = datetime.fromisoformat(str(record["at"])).replace(tzinfo=None, microsecond=0)
            out.append((str(payload["job"]), at))
    return sorted(out)


def ticks_of(records: Iterable[Mapping[str, object]]) -> list[Tick]:
    """(job, instant) of each scheduler tick a journal admitted. The trace
    does not show them all: a tick that finds the job's start already
    deferred to its run_window prints nothing."""
    return _records(records, "input")


def drops_of(records: Iterable[Mapping[str, object]]) -> list[Tick]:
    """(job, instant) of each scheduler tick a journal dropped at resume."""
    return _records(records, "drop")


def _stamp(text: object) -> datetime:
    return datetime.fromisoformat(str(text)).replace(tzinfo=None)


def effects_of(records: Iterable[Mapping[str, object]], kind: str) -> list[Mapping[str, object]]:
    """The effects of one kind ("SPAWN", "KILL") in the journal's decisions."""
    out: list[Mapping[str, object]] = []
    for record in records:
        effects = record.get("effects") if record.get("rec") == "decision" else None
        if isinstance(effects, list):
            out += [e for e in effects if isinstance(e, dict) and e.get("kind") == kind]
    return out


def launch_rows(
    real: Trace,
    records: Iterable[Mapping[str, object]],
    tolerance: timedelta,
    median: timedelta,
) -> list[Row]:
    """A row per scheduled start: its `dispatch` record, stamped with the
    engine's clock reading after it launched the wrapper, comes 0 to
    `tolerance` after the tick the start is stamped with. One more row
    bounds the median lateness by `median`: a clock that runs slow or a
    sleep that overshoots makes every launch late, while a slow launch now
    and then moves only the largest one."""
    records = list(records)
    run_of = {
        (str(e["job"]), _stamp(e["at"])): e["run_number"] for e in effects_of(records, "SPAWN")
    }
    launched = {
        (str(r["job"]), r["run_number"]): _stamp(r["started_at"])
        for r in records
        if r.get("rec") == "dispatch"
    }
    rows = []
    lates = []
    for run in real.runs:
        if run.made_by != "scheduler":
            continue
        at = launched.get((run.job, run_of.get((run.job, run.at))))
        late = None if at is None else at - run.at
        ok = late is not None and timedelta(0) <= late <= tolerance
        got = "no dispatch" if late is None else f"+{late.total_seconds():.1f}s"
        rows.append(Row("launch", run.job, f"{_when(run.at)} +0..{_secs(tolerance)}s", got, ok))
        lates += [] if late is None else [late]
    if lates:
        middle = sorted(lates)[len(lates) // 2]
        got = f"+{middle.total_seconds():.1f}s"
        rows.append(Row("launch median", "-", f"+0..{_secs(median)}s", got, middle <= median))
    return rows


def kill_rows(
    real: Trace,
    records: Iterable[Mapping[str, object]],
    statuses: Mapping[tuple[str, int], Mapping[str, object]],
    tolerance: timedelta,
) -> list[Row]:
    """A row per `term_run_time` kill: its KILL effect is applied, and the
    wrapper's status.json shows the command signaled within `tolerance` of
    the deadline."""
    records = list(records)
    state = {
        str(r.get("effect_id")): r.get("state") for r in records if r.get("rec") == "effect_result"
    }
    kills = effects_of(records, "KILL")
    rows = []
    for timer in real.timers:
        if timer.what != "term_run_time kill":
            continue
        effect = next(
            (e for e in kills if e.get("job") == timer.job and _stamp(e["at"]) == timer.at), None
        )
        status = statuses.get((timer.job, int(str(effect["run_number"])))) if effect else None
        got = "no KILL effect" if effect is None else str(state.get(str(effect["effect_id"])))
        ok = effect is not None and got == "applied" and status is not None
        if status is not None:
            late = _stamp(status.get("ended_at")) - timer.at
            ok = ok and status.get("outcome") == "signaled" and timedelta(0) <= late <= tolerance
            got += f", {status.get('outcome')} +{late.total_seconds():.1f}s"
        want = f"applied, signaled +0..{_secs(tolerance)}s"
        rows.append(Row("kill applied", timer.job, want, got, ok))
    return rows


def _secs(span: timedelta) -> str:
    return f"{span.total_seconds():g}"


def _down(at: datetime, downtime: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(stop < at <= start for stop, start in downtime)


def _when(at: datetime) -> str:
    return f"{at:%H:%M:%S}"


def compare(
    real: Trace,
    reference: Trace,
    *,
    tolerance: timedelta,
    downtime: Iterable[tuple[datetime, datetime]] = (),
    ticks: tuple[Iterable[Tick], Iterable[Tick]] | None = None,
    drops: Iterable[Tick] = (),
) -> list[Row]:
    """Rows comparing a real run with the rehearsal of the same window.

    `downtime` holds the (stop, start) instants the real engine was down.
    `ticks` is (real, rehearsal) scheduler ticks from the two journals, and
    `drops` the ticks the real journal dropped at resume: a rehearsal tick
    inside a downtime must be a drop, any other a real tick."""
    downtime = list(downtime)
    rows: list[Row] = []
    if ticks is not None:
        real_ticks, reference_ticks = (sorted(side) for side in ticks)
        dropped = sorted(drops)
        for job, at in sorted(set(real_ticks) | set(reference_ticks) | set(dropped)):
            n = reference_ticks.count((job, at))
            want = (0, n) if _down(at, downtime) else (n, 0)
            have = (real_ticks.count((job, at)), dropped.count((job, at)))
            rows.append(Row("tick", job, _tick(at, want), _tick(at, have), have == want))
    live = [r for r in reference.runs if not _down(r.at, downtime)]
    for job in sorted({r.job for r in live} | {r.job for r in real.runs}):
        rows.extend(_compare_job(job, real.runs, live, tolerance))
    want_timers = sorted(
        (t.job, t.at, t.what) for t in reference.timers if not _down(t.at, downtime)
    )
    have_timers = sorted((t.job, t.at, t.what) for t in real.timers)
    for job, at, what in sorted(set(want_timers) | set(have_timers)):
        want_n, have_n = want_timers.count((job, at, what)), have_timers.count((job, at, what))
        rows.append(
            Row(what, job, f"{_when(at)} x{want_n}", f"{_when(at)} x{have_n}", want_n == have_n)
        )
    return rows


def _tick(at: datetime, counts: tuple[int, int]) -> str:
    parts = [f"{what} x{n}" for what, n in zip(("admitted", "dropped"), counts) if n]
    return f"{_when(at)} {', '.join(parts) or 'none'}"


def _compare_job(
    job: str, real: Iterable[Run], reference: Iterable[Run], tolerance: timedelta
) -> list[Row]:
    rows: list[Row] = []
    for made_by in ("scheduler", "engine"):
        want = sorted((r for r in reference if r.job == job and r.made_by == made_by), key=_at)
        have = sorted((r for r in real if r.job == job and r.made_by == made_by), key=_at)
        check = "start" if made_by == "scheduler" else "follow-on start"
        for index in range(max(len(want), len(have))):
            w = want[index] if index < len(want) else None
            h = have[index] if index < len(have) else None
            if w is None or h is None:
                rows.append(Row(check, job, _show(w), _show(h), False))
                continue
            lag = h.at - w.at
            on_time = (
                lag == timedelta(0)
                if made_by == "scheduler"
                else (timedelta(0) <= lag <= tolerance)
            )
            got = _show(h) if lag == timedelta(0) else f"{_show(h)} (+{int(lag.total_seconds())}s)"
            rows.append(Row(check, job, _show(w), got, on_time and w.status == h.status))
    return rows


def _at(run: Run) -> datetime:
    return run.at


def _show(run: Run | None) -> str:
    return "-" if run is None else f"{_when(run.at)} {run.status or 'open'}"


def max_follow_on_lag(rows: Iterable[Row]) -> int:
    """The largest follow-on lag in seconds the rows show."""
    lags = [int(m.group(1)) for r in rows if (m := re.search(r"\(\+(\d+)s\)", r.got))]
    return max(lags, default=0)


def max_launch_lag(rows: Iterable[Row]) -> float:
    """The largest launch lag in seconds the rows show."""
    lags = [float(r.got[1:-1]) for r in rows if r.check == "launch" and r.got.startswith("+")]
    # the median row is not a launch
    return max(lags, default=0.0)


def table(rows: Iterable[Row], *, all_rows: bool = False) -> str:
    """A short table: a count of matching rows by check, then every mismatch
    (every row with `all_rows`)."""
    rows = list(rows)
    counts: dict[str, list[int]] = {}
    for row in rows:
        tally = counts.setdefault(row.check, [0, 0])
        tally[0 if row.ok else 1] += 1
    lines = [f"{'check':<22} {'match':>5} {'differ':>6}"]
    lines += [f"{check:<22} {ok:>5} {bad:>6}" for check, (ok, bad) in counts.items()]
    shown = [r for r in rows if all_rows or not r.ok]
    if shown:
        lines.append(f"{'':<2}{'check':<20} {'job':<16} {'rehearse':<22} {'real':<22} verdict")
        lines += [
            f"  {r.check:<20} {r.job:<16} {r.expected:<22} {r.got:<22} {'ok' if r.ok else 'DIFFER'}"
            for r in shown
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------- L023 cross-check

FINDING = re.compile(
    r"L023 (?:warn|info): (?:(?P<attr>\w+) (?P<shown>\S+) of '(?P<job>[^']+)' is changed"
    r"|start_mins of '(?P<mins_job>[^']+)' repeats every hour).*?"
    r"Under the runner's rules, (?P<clauses>.*)\. (?:Move the time|$)"
)
CLOCK = r"(\d\d:\d\d(?::\d\d)?)"
PASS = r"(?:, (?:once, )?in the (first|second) pass \(UTC([+-]\d\d):(\d\d)\))?"
WHEN = CLOCK + r"( next day| previous day)?" + PASS


@dataclass(frozen=True)
class Claim:
    """What one L023 finding says the runner does on the scenario's change day."""

    job: str
    attr: str
    text: str


def claims(lint_output: str, change: str, windowed: Iterable[str] = ()) -> list[Claim]:
    """The clause of each L023 finding for the change day `change`, which is
    the text after "on a day the clock", for example "skips 01:00-01:59".

    The start_mins finding states the scheduler's ticks; a run_window then
    defers or skips some of them. So its claim is left out for the jobs in
    `windowed`, whose window finding is checked instead."""
    windowed = set(windowed)
    out = []
    for line in lint_output.splitlines():
        found = FINDING.search(line)
        if not found:
            continue
        job = found["job"] or found["mins_job"]
        attr = found["attr"] or "start_mins"
        if attr == "start_mins" and job in windowed:
            continue
        for clause in re.split(r"; (?=on a day )", found["clauses"]):
            lead = f"on a day the clock {change}"
            if clause.startswith(lead):
                body = clause[len(lead) :].removeprefix(" (unverified shape)").lstrip(", ")
                out.append(Claim(job, attr, body))
    return out


def instant(day: date, clock: str, tz: ZoneInfo, *, shift: str = "", offset: str = "") -> datetime:
    """The naive UTC instant of local wall time `clock` on `day`, in the pass
    whose UTC offset is `offset` ("+01:00"). A wall time in a repeated hour
    needs the offset: without it the text does not say which pass."""
    wall = datetime.combine(day, time.fromisoformat(clock))
    wall += {" next day": timedelta(days=1), " previous day": timedelta(days=-1)}.get(
        shift, timedelta()
    )
    folds = [wall.replace(tzinfo=tz, fold=f) for f in (0, 1)]
    if offset:
        want = _offset(offset)
        folds = [f for f in folds if f.utcoffset() == want]
        if not folds:
            raise ValueError(f"{clock} has no pass at UTC{offset} in {tz}")
    elif folds[0].utcoffset() != folds[1].utcoffset() and _exists(folds[0]):
        raise ValueError(f"{clock} is in a repeated hour of {tz} and names no pass")
    return _utc(folds[0])


def _offset(text: str) -> timedelta:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return sign * timedelta(hours=int(hours), minutes=int(minutes))


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _exists(moment: datetime) -> bool:
    """Whether an aware local wall time exists: a time in a gap does not
    survive the round trip through UTC."""
    tz = moment.tzinfo
    back = moment.astimezone(ZoneInfo("UTC")).astimezone(tz)
    return back.replace(tzinfo=None) == moment.replace(tzinfo=None)


def _at_text(day: date, tz: ZoneInfo, m: re.Match[str], first: int) -> datetime:
    clock, shift, _, hh, mm = m.group(first, first + 1, first + 2, first + 3, first + 4)
    return instant(day, clock, tz, shift=shift or "", offset=f"{hh}:{mm}" if hh else "")


@dataclass(frozen=True)
class Expectation:
    """What one claim predicts. `kind` says which fields count:

    - "starts": `instants` are the job's scheduled starts in the scenario;
    - "timer": `instants` are the job's must alarms;
    - "windows": every start lies in one of `spans`, and each holds one;
    - "ticks": each of `instants` holds exactly one start; no start falls at
      an instant in `never`, nor strictly inside an interval in `gaps`
      except at one of `instants`."""

    kind: Literal["starts", "timer", "windows", "ticks"]
    instants: tuple[datetime, ...] = ()
    spans: tuple[tuple[datetime, datetime], ...] = ()
    never: tuple[datetime, ...] = ()
    gaps: tuple[tuple[datetime, datetime], ...] = ()


def expect(claim: Claim, day: date, tz: ZoneInfo) -> Expectation:
    """Read one claim's text into the instants it predicts on `day`."""
    text = claim.text
    if claim.attr == "start_times":
        if text.startswith("it does not run"):
            return Expectation("starts")
        m = _match(r"it runs at " + WHEN, text)
        return Expectation("starts", (_at_text(day, tz, m, 1),))
    if claim.attr in ("must_start_times", "must_complete_times"):
        if text.startswith("it is never armed"):
            return Expectation("timer")
        m = _match(r"it is due at " + WHEN, text)
        return Expectation("timer", (_at_text(day, tz, m, 1),))
    if claim.attr == "run_window":
        if text == "the window never opens":
            return Expectation("windows")
        pattern = r"opens (?:a second time )?at " + WHEN + r" and closes at " + WHEN
        spans = tuple(
            (_at_text(day, tz, m, 1), _at_text(day, tz, m, 6)) for m in re.finditer(pattern, text)
        )
        if not spans or any(lo > hi for lo, hi in spans):
            raise ValueError(f"unread L023 text: {text}")
        return Expectation("windows", spans=spans)
    return _expect_ticks(text, day, tz)


def _expect_ticks(text: str, day: date, tz: ZoneInfo) -> Expectation:
    """A start_mins claim. A moved tick and a tick that runs twice are a
    count at an instant: a moved tick that lands on an ordinary tick merges
    into it, so one start there either way. A tick in a gap that does not
    run, or moves, leaves no start inside the gap: from the job's last
    ordinary tick before the change to its first one after it."""
    present: list[datetime] = []
    never: list[datetime] = []
    missing: list[str] = []
    minutes: set[int] = set()
    for group in text.removeprefix("ticks ").split("; "):
        kind, _, ticks = group.partition(": ")
        for tick in ticks.split(", "):
            if kind == "move":
                m = _match(CLOCK + r" runs at " + WHEN, tick)
                present.append(_at_text(day, tz, m, 2))
                source = m.group(1)
            elif kind == "run twice":
                source = tick
                present += [instant(day, tick, tz, offset=o) for o in _offsets(day, tick, tz)]
            elif kind == "do not run":
                source = tick
            else:
                source = tick
                which = _match(r"run once, in the (first|second) pass", kind).group(1)
                first, second = (instant(day, tick, tz, offset=o) for o in _offsets(day, tick, tz))
                present.append(first if which == "first" else second)
                never.append(second if which == "first" else first)
            minutes.add(time.fromisoformat(source).minute)
            wall = datetime.combine(day, time.fromisoformat(source)).replace(tzinfo=tz)
            if not _exists(wall):
                missing.append(source)
            elif kind == "do not run":
                never += [
                    instant(day, source, tz, offset=o) for o in set(_offsets(day, source, tz))
                ]
    gaps = tuple(sorted({_gap(day, clock, minutes, tz) for clock in missing}))
    return Expectation("ticks", tuple(present), never=tuple(never), gaps=gaps)


def _gap(day: date, clock: str, minutes: set[int], tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC interval around the gap that holds `clock`: from the last
    ordinary tick (a minute in `minutes` at an existing wall time) before the
    change to the first one at or after it."""
    wall = datetime.combine(day, time.fromisoformat(clock))
    late, early = _utc(wall.replace(tzinfo=tz, fold=0)), _utc(wall.replace(tzinfo=tz, fold=1))
    lo, hi = min(late, early), max(late, early)
    change = lo
    while change < hi and _local_offset(change, tz) == _local_offset(lo, tz):
        change += timedelta(minutes=1)
    ordinary = sorted(
        _utc(local.replace(fold=fold))
        for offset in (-1, 0, 1)
        for hour in range(24)
        for minute in minutes
        for fold in (0, 1)
        if _exists(
            local := datetime.combine(day + timedelta(days=offset), time(hour, minute)).replace(
                tzinfo=tz
            )
        )
    )
    before = max(at for at in ordinary if at < change)
    after = min(at for at in ordinary if at >= change)
    return before, after


def _local_offset(at: datetime, tz: ZoneInfo) -> timedelta | None:
    return at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).utcoffset()


def _match(pattern: str, text: str) -> re.Match[str]:
    found = re.match(pattern, text)
    if found is None:
        raise ValueError(f"unread L023 text: {text}")
    return found


def _offsets(day: date, clock: str, tz: ZoneInfo) -> list[str]:
    wall = datetime.combine(day, time.fromisoformat(clock))
    out = []
    for fold in (0, 1):
        offset = wall.replace(tzinfo=tz, fold=fold).utcoffset()
        assert offset is not None
        minutes = int(offset.total_seconds() // 60)
        out.append(f"{'-' if minutes < 0 else '+'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}")
    return out


def check_claims(
    found: Iterable[Claim],
    real: Trace,
    day: date,
    tz: ZoneInfo,
    *,
    downtime: Iterable[tuple[datetime, datetime]] = (),
    drops: Iterable[tuple[str, datetime]] = (),
) -> list[Row]:
    """A row per claim: does the real run show what the L023 message states?
    A predicted start inside a downtime must be a drop instead."""
    downtime = list(downtime)
    dropped = set(drops)
    rows = []
    for claim in found:
        want = expect(claim, day, tz)
        starts = [r.at for r in real.runs if r.job == claim.job and r.made_by == "scheduler"]
        seen = sorted(starts + [at for job, at in dropped if job == claim.job])
        label = f"L023 {claim.attr}"
        if want.kind == "starts":
            ok = seen == sorted(want.instants) and all(
                (claim.job, at) in dropped for at in want.instants if _down(at, downtime)
            )
            rows.append(Row(label, claim.job, _whens(want.instants), _whens(seen), ok))
        elif want.kind == "timer":
            have = sorted(t.at for t in real.timers if t.job == claim.job and t.what in TIMERS)
            ok = have == sorted(want.instants)
            rows.append(Row(label, claim.job, _whens(want.instants), _whens(have), ok))
        elif want.kind == "windows":
            # a dropped tick is no start here: the window may have deferred it
            inside = all(any(lo <= at <= hi for lo, hi in want.spans) for at in starts)
            each = all(any(lo <= at <= hi for at in starts) for lo, hi in want.spans)
            shown = " ".join(f"{_when(lo)}-{_when(hi)}" for lo, hi in want.spans) or "none"
            rows.append(Row(label, claim.job, shown, _whens(sorted(starts)), inside and each))
        else:
            once = all(seen.count(at) == 1 for at in set(want.instants))
            stray = [
                at
                for at in seen
                if at in want.never
                or (at not in want.instants and any(lo < at < hi for lo, hi in want.gaps))
            ]
            shown = _whens(want.instants) + "".join(
                f" none in {_when(lo)}..{_when(hi)}" for lo, hi in want.gaps
            )
            if want.never:
                shown += f" none at {_whens(want.never)}"
            rows.append(Row(label, claim.job, shown, _whens(seen), once and not stray))
    return rows


def _whens(instants: Iterable[datetime]) -> str:
    return ",".join(_when(at) for at in instants) or "none"
