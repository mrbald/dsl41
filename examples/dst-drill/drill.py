"""The DST drill: run the real engine on a fast fake clock across DST changes
and midnight, and compare it with `dsl41 rehearse` over the same window. One
scenario steps the wall clock backward (DL-435).

    python examples/dst-drill/drill.py [SCENARIO ...] [--speed 60] [--work DIR]

Run it inside the drill's container (README.md): it needs libfaketime and
GNU date. With no SCENARIO it runs them all. It prints a short table per
scenario and exits 1 on any difference.

The driver itself reads the real clock. Every dsl41 process it starts runs
under ft.sh with a fake start that continues one timeline: fake time is
the scenario's start plus the real time since the first launch, times the
speed. The clock-step scenario runs at speed 1 under ft-step.sh, whose spec
file the driver rewrites to step the wall clock alone. compare.py holds the
verdict.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _load_compare() -> ModuleType:
    """compare.py by its path, under a name of its own: no sys.path entry,
    so nothing else named `compare` can shadow it or be shadowed."""
    name = "dst_drill_compare"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "compare.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if TYPE_CHECKING:
    import compare
else:
    compare = _load_compare()

ESTATE = HERE / "estate.jil"
STEP_ESTATE = HERE / "estate-step.jil"
FT = HERE / "ft.sh"
FT_STEP = HERE / "ft-step.sh"
#: what the rehearsal needs to know of the real commands: these two sleep
#: 600 seconds, so the engine kills the first by term_run_time and raises the
#: second's must alarm; every other command ends at once with exit 0
REHEARSAL = {
    "adapter": {
        "runs": [
            {"job": "DD_KILL", "run_number": 1, "duration_s": 600, "exit_code": 0},
            {"job": "DD_MUST", "run_number": 1, "duration_s": 600, "exit_code": 0},
        ]
    }
}
#: real time bounds, before the speed. A follow-on start may lag its
#: rehearsal by FOLLOW_ON_S: the producer's command, its wrapper's exit and
#: the engine's decision. A launch (the dispatch record, taken after the
#: wrapper process is started) may lag its tick by LAUNCH_S. The median
#: launch may lag by MEDIAN_S: a slow clock or an overshooting sleep moves
#: every launch. A kill starts no process, so its signal has a bound of its
#: own, KILL_S. The v2 runs measured far inside them (README, Verdict)
FOLLOW_ON_S = 0.25
LAUNCH_S = 0.75
MEDIAN_S = 0.1
KILL_S = 0.1
#: starts this close to either end of the window are not compared
GUARD = timedelta(minutes=2)
#: the jobs with a run_window, whose start_mins finding is not checked alone
WINDOWED = ("DD_WINDOW", "DD_WINDOW_LATE")
#: the clock-step estate's job that the step lands inside, and the job that follows it
STEP_PRODUCER = "DS_TICK"
STEP_FOLLOWER = "DS_AFTER"


@dataclass(frozen=True)
class Step:
    """A backward step of the wall clock (DL-435): at fake instant `at`, by
    `back`. Right after it the driver sends SET_GLOBAL, an input the engine
    must admit while the wall clock is below its frontier."""

    at: datetime
    back: timedelta


@dataclass(frozen=True)
class Scenario:
    """One run. Instants are UTC. `change` names the change day's L023 clause
    ("skips 01:00-01:59"); `day` is its local date. `restart` is (stop,
    start) of a clean engine stop and a `dsl41 run --resume`. `speed` fixes
    the clock rate when the scenario cannot take the driver's; `step` makes
    it the clock-step scenario."""

    name: str
    zone: str
    start: datetime
    end: datetime
    change: str | None = None
    day: date | None = None
    restart: tuple[datetime, datetime] | None = None
    speed: int | None = None
    step: Step | None = None
    guard: timedelta = GUARD

    @property
    def downtime(self) -> list[tuple[datetime, datetime]]:
        return [self.restart] if self.restart is not None else []

    @property
    def estate(self) -> Path:
        return ESTATE if self.step is None else STEP_ESTATE


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


SCENARIOS = (
    # 00:20 GMT to 03:25 BST; the clock skips 01:00-01:59 at 01:00Z
    Scenario(
        "london-spring",
        "Europe/London",
        _utc("2026-03-29T00:20"),
        _utc("2026-03-29T02:25"),
        "skips 01:00-01:59",
        date(2026, 3, 29),
    ),
    # 00:20 BST to 03:25 GMT; the clock repeats 01:00-01:59 at 01:00Z
    Scenario(
        "london-fall",
        "Europe/London",
        _utc("2026-10-24T23:20"),
        _utc("2026-10-25T03:25"),
        "repeats 01:00-01:59",
        date(2026, 10, 25),
    ),
    # 00:20 EST to 03:25 EDT; the clock skips 02:00-02:59 at 07:00Z
    Scenario(
        "newyork-spring",
        "America/New_York",
        _utc("2026-03-08T05:20"),
        _utc("2026-03-08T07:25"),
        "skips 02:00-02:59",
        date(2026, 3, 8),
    ),
    # 00:20 EDT to 03:25 EST; the clock repeats 01:00-01:59 at 06:00Z
    Scenario(
        "newyork-fall",
        "America/New_York",
        _utc("2026-11-01T04:20"),
        _utc("2026-11-01T08:25"),
        "repeats 01:00-01:59",
        date(2026, 11, 1),
    ),
    # 23:40 to 00:52 BST: DD_AT_2359 and DD_AT_0001 on either side of local
    # midnight, and the scheduler's day rolling over with the engine running
    Scenario(
        "london-midnight",
        "Europe/London",
        _utc("2026-07-15T22:40"),
        _utc("2026-07-15T23:52"),
    ),
    # as london-fall, with the engine stopped at 01:55 BST (first pass) and
    # started again at 01:05 GMT, on the far side of the change
    Scenario(
        "london-fall-restart",
        "Europe/London",
        _utc("2026-10-24T23:20"),
        _utc("2026-10-25T03:25"),
        "repeats 01:00-01:59",
        date(2026, 10, 25),
        restart=(_utc("2026-10-25T00:55"), _utc("2026-10-25T01:05")),
    ),
    # a tick every minute; the wall clock steps back 90 s at 00:12:10, 10 s
    # after the 00:12 tick, and reaches that frontier again 90 s later.
    # Speed 1: a wall-only step needs a real monotonic clock, and a faked
    # rate would scale it
    Scenario(
        "clock-step",
        "UTC",
        _utc("2026-07-15T00:11:30"),
        _utc("2026-07-15T00:14:15"),
        speed=1,
        step=Step(_utc("2026-07-15T00:12:10"), timedelta(seconds=90)),
        guard=timedelta(seconds=10),
    ),
)


class FakeClock:
    """The scenario's one timeline, read from the real monotonic clock."""

    def __init__(self, start: datetime, speed: int) -> None:
        self.start, self.speed = start, speed
        self.origin = time.monotonic()

    def now(self) -> datetime:
        return self.start + timedelta(seconds=(time.monotonic() - self.origin) * self.speed)

    def wait_until(self, at: datetime) -> None:
        delay = (at - self.now()).total_seconds() / self.speed
        if delay > 0:
            time.sleep(delay)

    def command(self, *args: str) -> list[str]:
        return [str(FT), f"{self.now():%Y-%m-%dT%H:%M:%S}", str(self.speed), *args]

    def client(self, *args: str) -> list[str]:
        """A control verb on the same timeline."""
        return self.command(*args)


class SteppedClock(FakeClock):
    """A speed-1 timeline whose wall clock can step: the offset from the real
    clock lives in a spec file that ft-step.sh makes libfaketime read."""

    def __init__(self, start: datetime, spec: Path) -> None:
        super().__init__(start, 1)
        self.spec = spec
        self.offset = float(round((start - datetime.now(UTC).replace(tzinfo=None)).total_seconds()))
        self._write()

    def _write(self) -> None:
        self.spec.write_text(f"{self.offset:+.0f}\n", encoding="utf-8")

    def now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=self.offset)

    def step(self, back: timedelta) -> None:
        self.offset -= back.total_seconds()
        self._write()

    def command(self, *args: str) -> list[str]:
        return [str(FT_STEP), str(self.spec), *args]

    def client(self, *args: str) -> list[str]:
        """A control verb reads no clock. libfaketime's clock_nanosleep wrapper
        refuses Python's `time.sleep` beside a real monotonic clock, which
        `engine stop --wait` calls, so the verb runs unpreloaded."""
        return list(args)


def _dsl41() -> str:
    found = shutil.which("dsl41") or str(Path(sys.executable).with_name("dsl41"))
    return found


def _clear_shm() -> None:
    """libfaketime keeps a shared timeline in /dev/shm; a killed tree leaves
    its file behind."""
    for stale in Path("/dev/shm").glob("faketime_*"):
        try:
            stale.unlink()
        except OSError:
            pass


class Leg:
    """The steps of one scenario that touch processes, with their exit codes
    as rows of the verdict."""

    def __init__(self, scenario: Scenario, work: Path, speed: int) -> None:
        self.s, self.work, self.speed = scenario, work, speed
        self.clock: FakeClock = (
            FakeClock(scenario.start, speed)
            if scenario.step is None
            else SteppedClock(scenario.start, work / "clock.spec")
        )
        self.root = work / "rr"
        self.rows: list[compare.Row] = []
        self.log = (work / "engine.log").open("a", encoding="utf-8")

    def _expect(self, step: str, want: int, got: int | None) -> None:
        self.rows.append(compare.Row("exit", step, str(want), str(got), want == got))

    def engine(self, *, resume: bool) -> subprocess.Popen[bytes]:
        args = [_dsl41(), "run", str(self.s.estate), "--run-root", str(self.root)]
        args += ["--as-machine", "localhost", "--timezone", self.s.zone]
        args += ["--resume"] if resume else []
        self.log.write(f"--- {self.clock.now():%FT%T} {' '.join(args)}\n")
        self.log.flush()
        return subprocess.Popen(
            self.clock.command(*args), stdout=self.log, stderr=subprocess.STDOUT, cwd=self.work
        )

    def stop(self, engine: subprocess.Popen[bytes], step: str) -> None:
        """A clean stop: `dsl41 engine stop --wait`, then the engine's own exit."""
        socket = ("--socket", str(self.root / "control.sock"))
        self.call(f"{step}: engine stop", 0, _dsl41(), "engine", "stop", "--wait", *socket)
        try:
            code: int | None = engine.wait(timeout=120)
        except subprocess.TimeoutExpired:
            engine.kill()
            code = None
        self._expect(f"{step}: engine exit", 0, code)

    def call(self, step: str, want: int, *args: str) -> None:
        self.log.write(f"--- {self.clock.now():%FT%T} {' '.join(args)}\n")
        self.log.flush()
        done = subprocess.run(
            self.clock.client(*args),
            stdout=self.log,
            stderr=subprocess.STDOUT,
            cwd=self.work,
            timeout=300,
        )
        self._expect(step, want, done.returncode)

    def run(self) -> None:
        s = self.s
        engine = self.engine(resume=False)
        if s.restart is not None:
            self.clock.wait_until(s.restart[0])
            self.stop(engine, "before the change")
            self.clock.wait_until(s.restart[1])
            engine = self.engine(resume=True)
        if s.step is not None:
            assert isinstance(self.clock, SteppedClock)
            self.clock.wait_until(s.step.at)
            self.clock.step(s.step.back)
            socket = ("--socket", str(self.root / "control.sock"))
            event = ("sendevent", "SET_GLOBAL", "-G", "DRILL_STEP=1")
            self.call("sendevent after the step", 0, _dsl41(), *event, *socket)
        self.clock.wait_until(s.end)
        if s.step is not None:
            alive = engine.poll() is None
            self.rows.append(compare.Row("engine up", "-", "running", str(alive), alive))
        self.stop(engine, "at the end")
        self.log.close()


def _output(*args: str) -> tuple[str, int]:
    """A reader command's stdout and stderr, and its exit code."""
    done = subprocess.run(args, capture_output=True, text=True, timeout=300)
    return done.stdout + done.stderr, done.returncode


def _exit_row(step: str, code: int) -> compare.Row:
    return compare.Row("exit", step, "0", str(code), code == 0)


def _journal_records(root: Path) -> list[dict[str, Any]]:
    """The store's wire records, and each `outcome` record as a record of its
    own kind: the wire carries no outcome (runner-design §7)."""
    from dsl41.runner_journal import read_journal, read_records

    records = read_journal(root)
    outcomes = [r.payload for _, r in read_records(root) if r.kind == "outcome"]
    return records + [{"rec": "outcome", **payload} for payload in outcomes]


def _input_stamps(root: Path) -> list[datetime]:
    """The stamp of each `input` record, in store order: the admission stamps."""
    from dsl41.runner_journal import read_records

    return [r.at_utc.replace(tzinfo=None) for _, r in read_records(root) if r.kind == "input"]


def verdict(s: Scenario, work: Path, speed: int) -> list[compare.Row]:
    """Compare the finished run in `work` with the rehearsal and the linter."""
    root = work / "rr"
    real_text, journal_rc = _output(_dsl41(), "journal", str(root))
    exits = [_exit_row("dsl41 journal", journal_rc)]
    (work / "real.txt").write_text(real_text, encoding="utf-8")
    scenario_file = work / "rehearsal.json"
    scenario_file.write_text(json.dumps(REHEARSAL), encoding="utf-8")
    hours = (s.end - s.start).total_seconds() / 3600
    reference_root = work / "rehearsal-rr"
    shutil.rmtree(reference_root, ignore_errors=True)
    reference_text, rehearse_rc = _output(
        *(_dsl41(), "rehearse", str(s.estate), "--scenario", str(scenario_file)),
        *("--start", s.start.isoformat(), "--hours", f"{hours:.3f}", "--timezone", s.zone),
        *("--run-root", str(reference_root)),
    )
    (work / "rehearse.txt").write_text(reference_text, encoding="utf-8")
    exits.append(_exit_row("dsl41 rehearse", rehearse_rc))
    first, last = s.start + s.guard, s.end - s.guard
    full = compare.parse_trace(real_text)
    real = compare.clip(full, first, last)
    reference = compare.clip(compare.parse_trace(reference_text), first, last)
    records = _journal_records(root)

    def inside(ticks: list[compare.Tick]) -> list[compare.Tick]:
        return [(job, at) for job, at in ticks if first <= at <= last]

    drops = inside(compare.drops_of(records))
    ticks = (
        inside(compare.ticks_of(records)),
        inside(compare.ticks_of(_journal_records(reference_root))),
    )
    launch = timedelta(seconds=LAUNCH_S * speed)
    if s.step is not None:
        log = (work / "engine.log").read_text(encoding="utf-8")
        given = StepInputs(log, real_text, _input_stamps(root), records, ticks)
        return exits + _step_rows(s, given, reference, launch)
    follow_on = timedelta(seconds=FOLLOW_ON_S * speed)
    rows = exits + compare.compare(
        real, reference, tolerance=follow_on, downtime=s.downtime, ticks=ticks, drops=drops
    )
    median = timedelta(seconds=MEDIAN_S * speed)
    rows += compare.launch_rows(real, records, launch, median, numbering=full)
    rows += compare.kill_rows(real, records, timedelta(seconds=KILL_S * speed))
    if s.change is not None and s.day is not None:
        lint_text, lint_rc = _output(_dsl41(), "lint", str(s.estate), "--timezone", s.zone)
        rows.append(_exit_row("dsl41 lint", lint_rc))
        (work / "lint.txt").write_text(lint_text, encoding="utf-8")
        found = compare.claims(lint_text, s.change, WINDOWED)
        rows += compare.check_claims(
            found, real, s.day, ZoneInfo(s.zone), downtime=s.downtime, drops=drops
        )
        if not found:
            rows.append(compare.Row("L023", "-", "findings", "none", False))
    return rows


@dataclass(frozen=True)
class StepInputs:
    """What the clock-step verdict reads of a finished run: the engine's
    stderr log, `dsl41 journal`'s text, the `input` records' stamps in store
    order, the wire records, and the scheduler ticks (real, rehearsal)."""

    log: str
    trace_text: str
    input_stamps: list[datetime]
    records: list[dict[str, Any]]
    ticks: tuple[list[compare.Tick], list[compare.Tick]]


def _step_rows(
    s: Scenario, given: StepInputs, reference: compare.Trace, launch: timedelta
) -> list[compare.Row]:
    """The clock-step verdict (DL-435): the step is reported once, the input
    sent under it is applied, the engine stays up, and the admission stamps
    and the trace stay in order. The real run makes the rehearsal's scheduled
    starts and ticks, each once: the step makes none repeat and loses none.
    The run the step lands in ends SUCCESS and starts exactly one follow-on,
    by count and status: a status that arrives while the wall is below the
    frontier is stamped at the frontier, so its time is not the rehearsal's.
    A completion held until the wall reaches the frontier carries the same
    stamps and is not told apart. The other record kinds (dispatch, outcome,
    engine) carry the wall reading and are not asserted. `reference` is the
    rehearsal's trace, clipped like the real one."""
    assert s.step is not None
    first, last = s.start + s.guard, s.end - s.guard
    full = compare.parse_trace(given.trace_text)
    real = compare.clip(full, first, last)
    stopped = given.log.count("engine stopped: clean_shutdown")
    rows = compare.warning_rows(given.log, s.step.back)
    rows.append(compare.Row("clean shutdown", "-", "1", str(stopped), stopped == 1))
    rows.append(compare.rejected_row(given.records))
    rows.append(compare.order_row("admission stamps", given.input_stamps))
    rows.append(compare.order_row("trace", compare.trace_stamps(given.trace_text)))
    rows += compare.tick_rows(given.ticks)
    rows += compare.start_rows(real, reference)
    tick = s.step.at.replace(second=0, microsecond=0)
    rows += compare.chain_rows(
        full, STEP_PRODUCER, tick, STEP_FOLLOWER, tick + timedelta(minutes=1)
    )
    # at speed 1 no clock runs slow: the median bound is the launch bound
    rows += compare.launch_rows(real, given.records, launch, launch, numbering=full)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*", help="names to run; default all")
    parser.add_argument("--speed", type=int, default=60)
    parser.add_argument("--work", type=Path, default=Path("/tmp/dst-drill"))
    parser.add_argument("--list", action="store_true", help="print the scenario names")
    parser.add_argument("--all-rows", action="store_true", help="print matching rows too")
    args = parser.parse_args(argv)
    by_name = {s.name: s for s in SCENARIOS}
    if args.list:
        print("\n".join(by_name))
        return 0
    unknown = [n for n in args.scenarios if n not in by_name]
    if unknown:
        parser.error(f"unknown scenario: {', '.join(unknown)}")
    if "faketime" in os.environ.get("LD_PRELOAD", ""):
        parser.error("the driver reads the real clock; run it without LD_PRELOAD")
    failed = []
    for s in [by_name[n] for n in args.scenarios] or list(SCENARIOS):
        speed = s.speed or args.speed
        work = args.work / s.name
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        _clear_shm()
        began = time.monotonic()
        leg = Leg(s, work, speed)
        leg.run()
        rows = leg.rows + verdict(s, work, speed)
        seconds = time.monotonic() - began
        ok = all(r.ok for r in rows)
        failed += [] if ok else [s.name]
        lag = compare.max_follow_on_lag(rows)
        print(f"== {s.name} ({s.zone}, x{speed}): {'PASS' if ok else 'FAIL'}", end="")
        launch = compare.max_launch_lag(rows)
        print(f" in {seconds:.0f} s real; largest lag: launch {launch:.1f} s, follow-on {lag} s")
        print(compare.table(rows, all_rows=args.all_rows))
        (work / "verdict.txt").write_text(compare.table(rows, all_rows=True), encoding="utf-8")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
