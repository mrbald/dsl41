#!/usr/bin/env python3
"""The compressed soak: play the soak estate day after day under a virtual
clock, measure each day, and seal as an operator would.

    uv run python examples/soak/compressed.py sealed --days 7 --root DIR
    uv run python examples/soak/compressed.py period --days 7 --root DIR
    uv run python examples/soak/compressed.py scenario --days 7 --root FILE

`sealed` closes a period at every midnight: each day is one process that
opens the period, plays it and seals it, as `dsl41 run --resume` and a live
`dsl41 seal` do. `audit` then attests the period. `period` plays every day in
one period and one process, and seals once at the end; it shows what grows
when nothing is sealed.

It wires the engine the way `dsl41 rehearse --run-root` does: a virtual clock,
scripted outcomes and the estate's own scheduler. `rehearse` plays one period
in a fresh root and cannot seal, so this driver also opens each next period
(`resume_run`) and asks the engine for the boundary (`submit_seal`). Each job's
outcome comes from its command: `sleep N` takes N seconds, `false` exits 1.

Each day also carries a little operator traffic, through the engine's
control path: a forced start, its exact retry (answered from the first
decision), a hold and its release, and a request id reused for another
command (refused). Without it the engine's `deduped` and `refusals` lists
stay empty.

`scenario` writes the same outcomes as a `dsl41 rehearse --scenario` file
(to the path `--root` names), so `rehearse --run-root` can play the same days
in one period for comparison.

Every measurement is one JSON line on stdout and in `--out`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dsl41.boundary import PeriodSealed, SealRequest, stage_next_period, stage_period
from dsl41.cli_common import load_catalog_and_ast_or_exit_2
from dsl41.ir import CatalogIR, ExecSpec
from dsl41.oracle_state import Event
from dsl41.period import runtime_profile_from_cli
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run

HERE = Path(__file__).resolve().parent
ESTATE = HERE / "estate" / "soak.jil"
START = "2026-01-05T00:00:00"  # a Monday; scheduled starts fall 01:00-21:45
LIVE = ("STARTING", "RUNNING")


def outcome(command: str) -> tuple[float, int]:
    """(duration_s, exit_code) of a soak command: `sleep N`, `true`,
    `false`, and `;`-joined sequences of them."""
    duration, code = 0.0, 0
    for part in command.split(";"):
        words = part.split()
        if words[:1] == ["sleep"]:
            duration += float(words[1])
        elif words == ["false"]:
            code = 1
        elif words == ["true"]:
            code = 0
        else:
            raise ValueError(f"not a soak command: {command!r}")
    return duration, code


def adapters(catalog: CatalogIR) -> dict[str, FakeAdapter]:
    script = {
        name: outcome(job.exec_.command)
        for name, job in catalog.jobs.items()
        if isinstance(job.exec_, ExecSpec) and job.exec_.command
    }
    fake = FakeAdapter(job_default=script)
    return {"CMD": fake, "FW": fake}


def rss_bytes() -> int:
    """This process's resident set now, from `ps` (kilobytes on both
    macOS and Linux)."""
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    return int(out.stdout.strip() or 0) * 1024


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def wal_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in (root / "wal").glob("*.jsonl"))


def segment_bytes(root: Path) -> int:
    segments = sorted((root / "wal").glob("*.jsonl"))
    return segments[-1].stat().st_size if segments else 0


def day_counts(engine: Engine, since: int) -> dict[str, int]:
    """Starts, failures, queue waits and the most runs live at once, over
    the trace entries from index `since` on."""
    catalog = engine.oracle.catalog
    trace = engine.oracle._trace
    counts = {"launches": 0, "box_starts": 0, "failures": 0, "que_waits": 0, "max_live": 0}
    live: set[str] = set()
    for entry in trace[since:]:
        _, arrow, new = entry.transition.partition("->")
        job = catalog.jobs.get(entry.job)
        if job is None or not arrow:
            continue  # a marker such as RUN_WINDOW_DEFER moves no status
        if job.job_type == "CMD":
            if new == "STARTING":
                counts["launches"] += 1
            if new in LIVE:
                live.add(entry.job)
            else:
                live.discard(entry.job)
            counts["max_live"] = max(counts["max_live"], len(live))
        elif job.job_type == "BOX" and new == "RUNNING":
            counts["box_starts"] += 1
        if new == "FAILURE":
            counts["failures"] += 1
        if new == "QUE_WAIT":
            counts["que_waits"] += 1
    return counts


def lists(engine: Engine) -> dict[str, int]:
    return {
        "trace": len(engine.oracle._trace),
        "drops": len(engine.drops),
        "deduped": len(engine.deduped),
        "refusals": len(engine.refusals),
        "decisions": len(engine.decisions._by_index),
        "outbox": len(engine.outbox._effects),
    }


def time_verbs(engine: Engine, root: Path) -> dict[str, float]:
    """The engine-side cost of one `trace` call (asking only for entries
    past the end, as a polling client does) and one `status` call: the
    handler plus the JSON line it writes. The single writer waits for both."""
    server = ControlServer(engine, root / "unused.sock")
    out: dict[str, float] = {}
    for verb, call in (
        ("trace_verb_s", lambda: server._trace({"since": len(engine.oracle._trace)})),
        ("trace_full_s", lambda: server._trace({"since": 0})),
        ("status_verb_s", lambda: server._status({})),
    ):
        t0 = time.perf_counter()
        json.dumps(call())
        out[verb] = round(time.perf_counter() - t0, 4)
    return out


def operator_traffic(engine: Engine, day: str) -> None:
    """A morning's commands, each under a request id as `sendevent` sends."""
    now = engine.clock.now()

    def send(rid: str, kind: str, job: str, at: datetime = now) -> None:
        engine.inject(Event(at=at, kind=kind, payload={"job": job}), request_id=f"{day}-{rid}")

    send("force", "FORCE_STARTJOB", "SK_SCHED01_C")
    send("force", "FORCE_STARTJOB", "SK_SCHED01_C")  # the exact retry
    send("hold", "ON_HOLD", "SK_SCHED02_C")
    send("release", "OFF_HOLD", "SK_SCHED02_C", now + timedelta(minutes=5))
    send("force", "FORCE_STARTJOB", "SK_SCHED03_C")  # an id reused: refused


class Soak:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.catalog, self.parsed, _ = load_catalog_and_ast_or_exit_2([ESTATE], False)
        self.profile = runtime_profile_from_cli(timezone="UTC")

    def scheduler(self, at: datetime) -> Scheduler:
        return Scheduler(self.catalog, start=at, default_tz="UTC")

    async def open(self, at: datetime) -> tuple[Engine, float]:
        t0 = time.perf_counter()
        if not (self.root / "wal").exists():
            self.root.mkdir(parents=True, exist_ok=True)
            staged = stage_period(self.root, self.parsed, self.catalog, self.profile)
            engine = start_run(
                self.catalog,
                self.root,
                clock=VirtualClock(at),
                adapters=adapters(self.catalog),
                scheduler=self.scheduler(at),
                staged=staged,
                launched=self.profile,
            )
        else:
            engine = await resume_run(
                self.catalog,
                self.root,
                clock=VirtualClock(at),
                adapters=adapters(self.catalog),
                scheduler=self.scheduler(at),
                declared=self.profile,
            )
        await engine.run_until_quiescent(at)
        return engine, time.perf_counter() - t0

    async def play_day(self, engine: Engine, start: datetime) -> dict[str, Any]:
        first = len(engine.oracle._trace)
        t0, c0 = time.perf_counter(), time.process_time()
        morning = start + timedelta(hours=10)
        await engine.run_until_quiescent(morning)
        await engine.clock.wait_until(morning)
        operator_traffic(engine, start.date().isoformat())
        midnight = start + timedelta(days=1)
        await engine.run_until_quiescent(midnight)
        await engine.clock.wait_until(midnight)
        await engine.run_until_quiescent(midnight)
        return {
            "day": start.date().isoformat(),
            "wall_s": round(time.perf_counter() - t0, 2),
            "cpu_s": round(time.process_time() - c0, 2),
            **day_counts(engine, first),
            **lists(engine),
            "segment_bytes": segment_bytes(self.root),
            "wal_bytes": wal_bytes(self.root),
            "rss_bytes": rss_bytes(),
            "peak_rss_bytes": peak_rss_bytes(),
            **time_verbs(engine, self.root),
        }

    async def seal(self, engine: Engine) -> float:
        t0 = time.perf_counter()
        staged_manifest = stage_period(self.root, self.parsed, self.catalog, self.profile)
        staged = stage_next_period(self.root, staged_manifest=staged_manifest)
        request = SealRequest(
            baseline_id=engine.baseline_id,
            epoch=engine.epoch,
            request_id=f"seal-{engine.clock.now().date().isoformat()}",
            next_period=staged,
            stage_digest=staged.stage_digest,
            force_seal=False,
            claimed_actor="soak@localhost",
        )
        engine.submit_seal(request)
        try:
            await engine.run_until_quiescent(engine.clock.now())
        except PeriodSealed:
            return time.perf_counter() - t0
        raise SystemExit("the seal did not commit")


def emit(record: dict[str, Any], out: Path | None) -> None:
    line = json.dumps(record, sort_keys=True)
    print(line, flush=True)
    if out is not None:
        with out.open("a") as f:
            f.write(line + "\n")


async def play(root: Path, start: datetime, days: int, seal: bool, out: Path | None) -> None:
    soak = Soak(root)
    engine, open_s = await soak.open(start)
    emit({"event": "open", "at": start.isoformat(), "open_s": round(open_s, 3)}, out)
    try:
        for n in range(days):
            emit({"event": "day", **await soak.play_day(engine, start + timedelta(days=n))}, out)
        if seal:
            seal_s = await soak.seal(engine)
            emit({"event": "seal", "seal_s": round(seal_s, 3), **lists(engine)}, out)
    finally:
        await engine.shutdown()
        if engine.journal is not None:
            engine.journal.close()


async def replay(root: Path, at: datetime, seal: bool, out: Path | None) -> None:
    """Resume a stopped period: the replay a restart pays."""
    soak = Soak(root)
    engine, open_s = await soak.open(at)
    record: dict[str, Any] = {
        "event": "replay",
        "replay_s": round(open_s, 3),
        "rss_bytes": rss_bytes(),
        **lists(engine),
    }
    try:
        if seal:
            record["seal_s"] = round(await soak.seal(engine), 3)
    finally:
        await engine.shutdown()
        if engine.journal is not None:
            engine.journal.close()
    record["peak_rss_bytes"] = peak_rss_bytes()
    emit(record, out)


def scenario(days: int) -> dict[str, Any]:
    """A `dsl41 rehearse --scenario` document with the same outcomes, for
    `days` days: one entry per job and run number, since a scenario has no
    per-job default. Six runs a day per job is more than any job takes."""
    catalog, _, _ = load_catalog_and_ast_or_exit_2([ESTATE], False)
    runs = [
        {"job": name, "run_number": n, "duration_s": duration, "exit_code": code}
        for name, job in sorted(catalog.jobs.items())
        if isinstance(job.exec_, ExecSpec) and job.exec_.command
        for duration, code in [outcome(job.exec_.command)]
        for n in range(1, 6 * days + 1)
    ]
    return {"adapter": {"runs": runs}}


def child(*args: str) -> None:
    subprocess.run([sys.executable, __file__, *args], check=True)


def audit(root: Path, period: int, out: Path | None) -> None:
    t0 = time.perf_counter()
    done = subprocess.run(
        [sys.executable, "-m", "dsl41", "audit", "--run-root", str(root), "--period", str(period)],
        capture_output=True,
        text=True,
    )
    record = {
        "event": "audit",
        "period": period,
        "exit": done.returncode,
        "audit_s": round(time.perf_counter() - t0, 3),
    }
    if done.returncode:
        record["stderr"] = done.stderr.strip()[-400:]
    emit(record, out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("sealed", "period", "play", "replay", "scenario"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--days", type=int, default=7)
        p.add_argument("--start", default=START)
        p.add_argument("--out", type=Path)
        p.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start)
    if args.root.exists() and args.mode in ("sealed", "period", "scenario"):
        raise SystemExit(f"{args.root} exists: name a fresh run root")
    rest = ["--root", str(args.root), *(["--out", str(args.out)] if args.out else [])]
    if args.mode == "scenario":
        args.root.write_text(json.dumps(scenario(args.days)) + "\n")
    elif args.mode == "play":
        asyncio.run(play(args.root, start, args.days, args.seal, args.out))
    elif args.mode == "replay":
        asyncio.run(replay(args.root, start, args.seal, args.out))
    elif args.mode == "sealed":
        for n in range(args.days):
            day = (start + timedelta(days=n)).isoformat()
            child("play", "--start", day, "--days", "1", "--seal", *rest)
            audit(args.root, n + 1, args.out)
    else:
        child("play", "--start", args.start, "--days", str(args.days), *rest)
        end = (start + timedelta(days=args.days)).isoformat()
        child("replay", "--start", end, "--seal", *rest)
        audit(args.root, 1, args.out)


if __name__ == "__main__":
    main()
