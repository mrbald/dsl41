#!/usr/bin/env python3
"""Sample a running soak engine once a minute, one JSON line per sample.

    python examples/soak/sample.py --run-root DIR/engine --pid PID --out FILE

Each line has the engine's and the supervisor's resident set, CPU time and
open file descriptors (from `ps`, and `/proc` or `lsof`), the open period's
WAL segment and the whole WAL, the trace length from the control socket's
`trace` verb, the command runs and boxes live, and how long one `trace` and
one `status` call took, measured at the client. It stops when the engine
process is gone. Standard library only, plus dsl41's own socket client.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dsl41.runner_control import ControlClientError, roundtrip

LIVE = ("STARTING", "RUNNING")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cpu_seconds(text: str) -> float:
    """`ps -o time=`: [[dd-]hh:]mm:ss[.cc]."""
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (int(days) * 86400 if days else 0)


def lsof_fds(fields: str) -> int:
    """Descriptors in `lsof -F f` output: the `f` lines whose name is a
    number. `cwd`, `txt` and the other named rows are not descriptors, and
    /proc/PID/fd on Linux does not list them either."""
    return sum(1 for line in fields.splitlines() if line[:1] == "f" and line[1:].isdigit())


def open_fds(pid: int) -> int | None:
    proc = Path(f"/proc/{pid}/fd")
    if proc.is_dir():
        return len(os.listdir(proc))
    out = subprocess.run(
        ["lsof", "-n", "-P", "-F", "f", "-p", str(pid)], capture_output=True, text=True
    )
    return lsof_fds(out.stdout) if out.stdout else None


def wal_bytes(run_root: Path) -> tuple[int, int]:
    """(the open period's segment, every segment). A root keeps one segment
    per period until a prune: the first predicts a replay, the second is
    retention."""
    segments = sorted((run_root / "wal").glob("*.jsonl"))
    sizes = [p.stat().st_size for p in segments]
    return (sizes[-1] if sizes else 0), sum(sizes)


def live_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Command runs and boxes live, and jobs queued, from a `status` answer.
    A box is RUNNING while its members run, and takes no machine slot."""
    live = [job for job in jobs if job["status"] in LIVE]
    return {
        "live_runs": sum(1 for job in live if job.get("job_type") != "BOX"),
        "live_boxes": sum(1 for job in live if job.get("job_type") == "BOX"),
        "que_wait": sum(1 for job in jobs if job["status"] == "QUE_WAIT"),
        "run_numbers": sum(job["run_number"] for job in jobs),
    }


def process(pid: int) -> dict[str, Any]:
    out = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)], capture_output=True, text=True)
    fields = out.stdout.split()
    if len(fields) != 2:
        return {"pid": pid, "alive": False}
    return {
        "pid": pid,
        "rss_bytes": int(fields[0]) * 1024,
        "cpu_s": round(cpu_seconds(fields[1]), 2),
        "fds": open_fds(pid),
    }


def timed(sock: Path, request: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    t0 = time.perf_counter()
    try:
        answer = roundtrip(sock, request, timeout=60.0)
    except ControlClientError:
        answer = None
    return answer, round(time.perf_counter() - t0, 4)


def sample(run_root: Path, pid: int) -> dict[str, Any]:
    sock = run_root / "control.sock"
    record: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds")}
    record["engine"] = process(pid)
    try:
        sup = json.loads((run_root / "supervisor.pid").read_text())["pid"]
        record["supervisor"] = process(int(sup))
    except (OSError, ValueError, KeyError, TypeError):
        record["supervisor"] = None  # not started yet, or between restarts
    record["wal_open_bytes"], record["wal_all_bytes"] = wal_bytes(run_root)
    trace, record["trace_call_s"] = timed(sock, {"cmd": "trace", "since": 2**31})
    record["trace"] = trace.get("last_seq") if trace and trace.get("ok") else None
    status, record["status_call_s"] = timed(sock, {"cmd": "status"})
    if status and status.get("ok"):
        record.update(live_counts(list(status["jobs"].values())))
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True, help="The engine's process id.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args()
    while alive(args.pid):
        due = time.monotonic() + args.interval
        line = json.dumps(sample(args.run_root, args.pid), sort_keys=True)
        with args.out.open("a") as f:
            f.write(line + "\n")
        while alive(args.pid) and time.monotonic() < due:
            time.sleep(1.0)  # an engine that exits ends the sampler within a second
    print(f"sample: engine {args.pid} is gone", file=sys.stderr)


if __name__ == "__main__":
    main()
