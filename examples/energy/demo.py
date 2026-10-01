"""Run a publication and later-period correction against PostgreSQL and HTTP."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from support import Engine, cli, make_properties, new_run, read_json, write_json  # noqa: E402

HERE = Path(__file__).resolve().parent


def fixture(feed: Path, name: str, *, start: str, revision: int, readings: list) -> bytes:
    raw = (json.dumps(readings, sort_keys=True) + "\n").encode()
    (feed / f"{name}.readings.json").write_bytes(raw)
    write_json(
        feed / f"{name}.manifest.json",
        {
            "snapshot": name,
            "interval_start": start,
            "duration": 900,
            "revision": revision,
            "file": f"{name}.readings.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "count": len(readings),
        },
    )
    return raw


def worker(run: Path, *args: str, refused: str | None = None) -> None:
    result = subprocess.run(
        [sys.executable, str(HERE / "worker.py"), "--run", str(run), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env={key: value for key, value in os.environ.items() if key != "DSL41_RUN"},
    )
    with (run / "worker-probes.jsonl").open("a") as log:
        log.write(
            json.dumps(
                {
                    "args": args,
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            + "\n"
        )
    if refused:
        if result.returncode == 0 or refused not in result.stderr:
            raise RuntimeError(f"expected refusal {refused!r}: {result}")
    elif result.returncode:
        raise RuntimeError(result.stderr)


def observe(run: Path, phase: str, name: str) -> None:
    result = subprocess.run(
        [sys.executable, str(HERE / "check.py"), "--run", str(run), "--phase", phase],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    write_json(run / f"{name}.json", json.loads(result.stdout))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--force-seal", action="store_true", help="record a retry-horizon override")
    args = parser.parse_args()
    database_url = os.environ["DATABASE_URL"]
    run = new_run("energy", args.run_dir)
    print(f"Evidence: {run}", flush=True)
    feed = run / "feed"
    feed.mkdir()
    readings = [
        {"meter": "meter_a", "revision": 1, "units": 10},
        {"meter": "meter_b", "revision": 1, "units": 20},
    ]
    full = fixture(feed, "initial", start="2026-01-15T00:00:00Z", revision=1, readings=readings)
    fixture(
        feed,
        "neighbor",
        start="2026-01-15T00:15:00Z",
        revision=1,
        readings=[
            {"meter": "meter_a", "revision": 1, "units": 5},
            {"meter": "meter_b", "revision": 1, "units": 7},
        ],
    )
    fixture(
        feed,
        "correction",
        start="2026-01-15T00:00:00Z",
        revision=2,
        readings=[{"meter": "meter_a", "revision": 2, "units": 12}, readings[1]],
    )
    fixture(
        feed,
        "conflict",
        start="2026-01-15T00:00:00Z",
        revision=3,
        readings=[{"meter": "meter_a", "revision": 1, "units": 11}, readings[1]],
    )
    # The same conflicting reading under an equivalent, non-canonical spelling.
    fixture(
        feed,
        "respelled",
        start="2026-1-15T0:0:0Z",
        revision=4,
        readings=[{"meter": "meter_a", "revision": 1, "units": 11}, readings[1]],
    )
    # An unseen snapshot whose meter_a reading is older than the accepted correction.
    fixture(feed, "stale", start="2026-01-15T00:00:00Z", revision=5, readings=readings)
    # A stable, valid JSON fragment still fails the full immutable manifest.
    (feed / "initial.readings.json").write_text(json.dumps(readings[:1]))
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=str(feed))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "schema": f"energy_{uuid.uuid4().hex[:16]}",
        "feed_url": f"http://127.0.0.1:{server.server_port}",
    }
    write_json(run / "config.json", config)
    properties = make_properties(run, HERE / "worker.py", env={"DATABASE_URL": database_url})
    engine = Engine(run, HERE / "estate.jil", properties)
    try:
        worker(run, "init")
        engine.start()
        engine.event("STARTJOB", "ENERGY_INITIAL_B")
        engine.wait_job("ENERGY_INITIAL_INGEST", "FAILURE")
        failed = engine.status()
        error_log = Path(failed["jobs"]["ENERGY_INITIAL_INGEST"]["log_err"])
        if "manifest length/hash differs" not in error_log.read_text():
            raise RuntimeError(f"ingest failed for another reason; see {error_log}")
        for job in ("ENERGY_INITIAL_CALCULATE", "ENERGY_INITIAL_PUBLISH"):
            if failed["jobs"][job]["run_number"] != 0:
                raise RuntimeError(f"{job} ran after the incomplete input was refused")
        with psycopg.connect(
            database_url, connect_timeout=3, options="-c statement_timeout=10000"
        ) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(config["schema"])))
            for table in ("readings", "publications"):
                query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                if conn.execute(query).fetchone()[0] != 0:
                    raise RuntimeError(f"incomplete input reached the {table} table")
        write_json(run / "incomplete-refusal.json", failed)
        temporary = feed / "complete.tmp"
        temporary.write_bytes(full)
        temporary.replace(feed / "initial.readings.json")
        engine.event("FORCE_STARTJOB", "ENERGY_INITIAL_INGEST")
        engine.wait_job("ENERGY_INITIAL_B")
        observe(run, "initial", "initial-check")
        worker(run, "ingest", "initial")  # duplicate delivery
        worker(run, "ingest", "conflict", refused="conflicting reading revision")
        worker(run, "ingest", "respelled", refused="non-canonical interval_start")
        observe(run, "initial", "initial-probes-check")
        if not args.force_seal:
            manifest = read_json(engine.root / "periods" / "000001" / "manifest.json")
            horizon = manifest["runtime_profile"]["retry_horizon_us"] / 1_000_000
            if not 0 < horizon <= 120:
                raise ValueError("unexpected retry horizon; seal this run manually")
            print(f"Waiting {horizon + 1:g}s for the control retry horizon", flush=True)
            deadline = time.monotonic() + horizon + 1
            while time.monotonic() < deadline:
                time.sleep(max(0, min(1, deadline - time.monotonic())))
        seal = engine.seal(force=args.force_seal)
        write_json(run / "boundary.json", seal)
        engine.start(resume=True)
        engine.event("STARTJOB", "ENERGY_CORRECTION_B")
        engine.wait_job("ENERGY_CORRECTION_B")
        observe(run, "corrected", "corrected-check")
        worker(run, "ingest", "correction")
        worker(run, "publish", "correction")
        worker(run, "ingest", "initial")  # stale delivery of the accepted old revision
        worker(run, "publish", "initial")
        worker(run, "ingest", "stale", refused="stale reading in a new snapshot")
        observe(run, "corrected", "corrected-probes-check")
        write_json(run / "final-status.json", engine.status())
    finally:
        engine.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    history = cli("runs", str(engine.root), "--format", "json")
    write_json(run / "run-history.json", json.loads(history.stdout))
    # Re-derive the sealed period from its own evidence and keep the attestation
    # beside its seal; the checker verifies it with `dsl41 verify`.
    audit = cli("audit", "--run-root", str(engine.root), "--period", "1", timeout=60)
    write_json(run / "audit.json", {"stdout": audit.stdout, "stderr": audit.stderr})
    observe(run, "complete", "complete-check")
    print(json.dumps(read_json(run / "complete-check.json"), indent=2))


if __name__ == "__main__":
    main()
