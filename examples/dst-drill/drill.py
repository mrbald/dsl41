"""The DST drill: run the real runner on a fast fake clock across DST changes
and midnight, and compare it with `dsl41 rehearse` over the same window.

    python examples/dst-drill/drill.py [SCENARIO ...] [--speed 60] [--work DIR]

Run it inside the drill's container (README.md): it needs libfaketime and
GNU date. With no SCENARIO it runs them all. It prints a short table per
scenario and exits 1 on any difference.

The driver itself reads the real clock. Every dsl41 process it starts runs
under ft.sh with a fake start that continues one timeline: fake time is
the scenario's start plus the real time since the first launch, times the
speed. compare.py holds the verdict.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
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
FT = HERE / "ft.sh"
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
#: the engine's decision; the scenarios measured at most 0.07 s (4 fake s at
#: x60). A launch (the dispatch record, taken after the wrapper process is
#: started) may lag its tick by LAUNCH_S;
#: most launches measured 0.04 to 0.06 s, and the largest of about 250, run
#: three containers at a time, 0.46 s (28 fake s). The median launch may lag
#: by MEDIAN_S: a slow clock or an overshooting sleep moves every launch.
#: A kill starts no process, so its signal has a bound of its own, KILL_S;
#: the scenarios measured 0.012 to 0.025 s (0.7 to 1.5 fake s)
FOLLOW_ON_S = 0.25
LAUNCH_S = 0.75
MEDIAN_S = 0.1
KILL_S = 0.1
#: starts this close to either end of the window are not compared
GUARD = timedelta(minutes=2)
#: the jobs with a run_window, whose start_mins finding is not checked alone
WINDOWED = ("DD_WINDOW", "DD_WINDOW_LATE")


@dataclass(frozen=True)
class Scenario:
    """One run. Instants are UTC. `change` names the change day's L023 clause
    ("skips 01:00-01:59"); `day` is its local date. `restart` is (stop,
    start) of a clean engine stop and a `dsl41 run --resume`; `seal` is
    (seal request, resume) of a live seal, its audit and the next period."""

    name: str
    zone: str
    start: datetime
    end: datetime
    change: str | None = None
    day: date | None = None
    restart: tuple[datetime, datetime] | None = None
    seal: tuple[datetime, datetime] | None = None

    @property
    def downtime(self) -> list[tuple[datetime, datetime]]:
        return [pair for pair in (self.restart, self.seal) if pair is not None]


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
    # 23:40 to 00:52 BST; a live seal at 00:04, its audit, the next period at 00:07
    Scenario(
        "london-midnight-seal",
        "Europe/London",
        _utc("2026-07-15T22:40"),
        _utc("2026-07-15T23:52"),
        seal=(_utc("2026-07-15T23:04"), _utc("2026-07-15T23:07")),
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
        self.clock = FakeClock(scenario.start, speed)
        self.root = work / "rr"
        self.rows: list[compare.Row] = []
        self.log = (work / "engine.log").open("a", encoding="utf-8")

    def _expect(self, step: str, want: int, got: int | None) -> None:
        self.rows.append(compare.Row("exit", step, str(want), str(got), want == got))

    def engine(self, *, resume: bool) -> subprocess.Popen[bytes]:
        args = [_dsl41(), "run", str(ESTATE), "--run-root", str(self.root)]
        args += ["--as-machine", "localhost", "--timezone", self.s.zone]
        args += ["--resume"] if resume else []
        self.log.write(f"--- {self.clock.now():%FT%T} {' '.join(args)}\n")
        self.log.flush()
        return subprocess.Popen(
            self.clock.command(*args), stdout=self.log, stderr=subprocess.STDOUT, cwd=self.work
        )

    def stop(self, engine: subprocess.Popen[bytes], step: str) -> None:
        engine.send_signal(signal.SIGINT)
        try:
            code: int | None = engine.wait(timeout=120)
        except subprocess.TimeoutExpired:
            engine.kill()
            code = None
        self._expect(step, 0, code)

    def call(self, step: str, want: int, *args: str) -> None:
        self.log.write(f"--- {self.clock.now():%FT%T} {' '.join(args)}\n")
        self.log.flush()
        done = subprocess.run(
            self.clock.command(*args),
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
            self.stop(engine, "engine stop before the change")
            self.clock.wait_until(s.restart[1])
            engine = self.engine(resume=True)
        if s.seal is not None:
            self.clock.wait_until(s.seal[0])
            self.call(
                "seal",
                0,
                _dsl41(),
                "seal",
                "--run-root",
                str(self.root),
                "--next",
                str(ESTATE),
                "--next-timezone",
                s.zone,
                "--next-as-machine",
                "localhost",
            )
            try:
                code: int | None = engine.wait(timeout=120)
            except subprocess.TimeoutExpired:
                engine.kill()
                code = None
            self._expect("engine exit at the seal", 3, code)
            self.call("audit", 0, _dsl41(), "audit", "--run-root", str(self.root))
            self.clock.wait_until(s.seal[1])
            engine = self.engine(resume=True)
        self.clock.wait_until(s.end)
        self.stop(engine, "engine stop at the end")
        self.log.close()


def _output(*args: str) -> str:
    done = subprocess.run(args, capture_output=True, text=True, timeout=300)
    return done.stdout + done.stderr


def _journal_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _statuses(root: Path) -> dict[tuple[str, int], dict[str, object]]:
    """Each wrapper's status.json under runs/, by (job, run number)."""
    out: dict[tuple[str, int], dict[str, object]] = {}
    for path in sorted(root.glob("runs/*/status.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        out[(str(doc["job"]), int(doc["run_number"]))] = doc
    return out


def verdict(s: Scenario, work: Path, speed: int) -> list[compare.Row]:
    """Compare the finished run in `work` with the rehearsal and the linter."""
    root = work / "rr"
    real_text = _output(_dsl41(), "journal", str(root))
    (work / "real.txt").write_text(real_text, encoding="utf-8")
    scenario_file = work / "rehearsal.json"
    scenario_file.write_text(json.dumps(REHEARSAL), encoding="utf-8")
    hours = (s.end - s.start).total_seconds() / 3600
    reference_root = work / "rehearsal-rr"
    for stale in (reference_root, reference_root.with_name("rehearsal-rr.anchor")):
        shutil.rmtree(stale, ignore_errors=True)
    reference_text = _output(
        *(_dsl41(), "rehearse", str(ESTATE), "--scenario", str(scenario_file)),
        *("--start", s.start.isoformat(), "--hours", f"{hours:.3f}", "--timezone", s.zone),
        *("--run-root", str(reference_root)),
    )
    (work / "rehearse.txt").write_text(reference_text, encoding="utf-8")
    first, last = s.start + GUARD, s.end - GUARD
    real = compare.clip(compare.parse_trace(real_text), first, last)
    reference = compare.clip(compare.parse_trace(reference_text), first, last)
    records = _journal_records(root)

    def inside(ticks: list[compare.Tick]) -> list[compare.Tick]:
        return [(job, at) for job, at in ticks if first <= at <= last]

    drops = inside(compare.drops_of(records))
    ticks = (
        inside(compare.ticks_of(records)),
        inside(compare.ticks_of(_journal_records(reference_root))),
    )
    follow_on = timedelta(seconds=FOLLOW_ON_S * speed)
    launch = timedelta(seconds=LAUNCH_S * speed)
    rows = compare.compare(
        real, reference, tolerance=follow_on, downtime=s.downtime, ticks=ticks, drops=drops
    )
    rows += compare.launch_rows(real, records, launch, timedelta(seconds=MEDIAN_S * speed))
    kill = timedelta(seconds=KILL_S * speed)
    rows += compare.kill_rows(real, records, _statuses(root), kill)
    if s.change is not None and s.day is not None:
        lint_text = _output(_dsl41(), "lint", str(ESTATE), "--timezone", s.zone)
        (work / "lint.txt").write_text(lint_text, encoding="utf-8")
        found = compare.claims(lint_text, s.change, WINDOWED)
        rows += compare.check_claims(
            found, real, s.day, ZoneInfo(s.zone), downtime=s.downtime, drops=drops
        )
        if not found:
            rows.append(compare.Row("L023", "-", "findings", "none", False))
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
        work = args.work / s.name
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        _clear_shm()
        began = time.monotonic()
        leg = Leg(s, work, args.speed)
        leg.run()
        rows = leg.rows + verdict(s, work, args.speed)
        seconds = time.monotonic() - began
        ok = all(r.ok for r in rows)
        failed += [] if ok else [s.name]
        lag = compare.max_follow_on_lag(rows)
        print(f"== {s.name} ({s.zone}, x{args.speed}): {'PASS' if ok else 'FAIL'}", end="")
        launch = compare.max_launch_lag(rows)
        print(f" in {seconds:.0f} s real; largest lag: launch {launch:.1f} s, follow-on {lag} s")
        print(compare.table(rows, all_rows=args.all_rows))
        (work / "verdict.txt").write_text(compare.table(rows, all_rows=True), encoding="utf-8")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
