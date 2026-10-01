"""Small PostgreSQL workers for immutable synthetic meter snapshots."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from support import query_status, read_json, write_json  # noqa: E402

WAVES = {
    "initial": ("initial", "neighbor"),
    "correction": ("correction",),
    "conflict": ("conflict",),
    "respelled": ("respelled",),
    "stale": ("stale",),
}
UTC_START = "%Y-%m-%dT%H:%M:%SZ"
METERS = {"meter_a", "meter_b"}
DDL = """
CREATE TABLE readings (
    interval_start text NOT NULL, duration integer NOT NULL, meter text NOT NULL,
    revision integer NOT NULL, units bigint NOT NULL CHECK (units >= 0),
    PRIMARY KEY (interval_start, duration, meter, revision));
CREATE TABLE snapshots (
    snapshot text PRIMARY KEY, interval_start text NOT NULL, duration integer NOT NULL,
    revision integer NOT NULL, digest text NOT NULL,
    UNIQUE(interval_start, duration, revision));
CREATE TABLE members (
    snapshot text REFERENCES snapshots NOT NULL, meter text NOT NULL,
    reading_revision integer NOT NULL, PRIMARY KEY(snapshot, meter));
CREATE TABLE calculations (snapshot text PRIMARY KEY REFERENCES snapshots, total bigint NOT NULL);
CREATE TABLE publications (
    snapshot text PRIMARY KEY REFERENCES snapshots, total bigint NOT NULL,
    period_id integer, baseline text,
    CHECK ((period_id IS NULL) = (baseline IS NULL)));
CREATE TABLE adjustments (
    snapshot text PRIMARY KEY REFERENCES publications,
    previous_snapshot text REFERENCES publications NOT NULL, delta bigint NOT NULL);
CREATE VIEW current_publications AS
    SELECT DISTINCT ON (s.interval_start, s.duration)
        s.interval_start, s.duration, s.revision, p.*
    FROM publications p JOIN snapshots s USING(snapshot)
    ORDER BY s.interval_start, s.duration, s.revision DESC;
"""


def database(run: Path, *, create: bool = False):
    schema = read_json(run / "config.json")["schema"]
    if not isinstance(schema, str) or not re.fullmatch(r"energy_[0-9a-f]{16}", schema):
        raise ValueError("invalid private schema name")
    conn = psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=3,
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )
    try:
        if create:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn
    except BaseException:
        conn.close()
        raise


def fetch(url: str) -> bytes:
    with urlopen(url, timeout=5) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("feed exceeds the example's 64 KiB limit")
    return raw


def bundle(run: Path, key: str):
    base = read_json(run / "config.json")["feed_url"]
    raw_manifest = fetch(f"{base}/{key}.manifest.json")
    manifest = json.loads(raw_manifest)
    if set(manifest) != {
        "snapshot",
        "interval_start",
        "duration",
        "revision",
        "file",
        "sha256",
        "bytes",
        "count",
    }:
        raise ValueError("manifest fields differ from the declared schema")
    # One spelling per instant: the text is the SQL key, so an equivalent
    # spelling must never reach the database as a different interval.
    spelled = manifest["interval_start"]
    try:
        start = datetime.strptime(spelled, UTC_START).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        start = None
    if start is None or start.strftime(UTC_START) != spelled:
        raise ValueError(f"non-canonical interval_start {spelled!r}: expected YYYY-MM-DDTHH:MM:SSZ")
    if start.timestamp() % 900 or manifest["duration"] != 900:
        raise ValueError("expected a UTC quarter-hour business interval")
    if type(manifest["revision"]) is not int or manifest["revision"] < 1:
        raise ValueError("snapshot revision must be a positive integer")
    if not re.fullmatch(r"[a-z0-9_-]+", manifest["snapshot"]):
        raise ValueError("invalid snapshot identity")
    if manifest["snapshot"] != key:
        raise ValueError("manifest does not identify the requested snapshot")
    if not re.fullmatch(r"[a-z0-9_-]+\.readings\.json", manifest["file"]):
        raise ValueError("reading file must be a local feed basename")
    raw = fetch(f"{base}/{manifest['file']}")
    if len(raw) != manifest["bytes"] or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ValueError("incomplete or changed reading file: manifest length/hash differs")
    rows = json.loads(raw)
    if not isinstance(rows, list) or manifest["count"] != 2 or len(rows) != 2:
        raise ValueError("incomplete snapshot: exactly two readings are required")
    for row in rows:
        if set(row) != {"meter", "revision", "units"}:
            raise ValueError("reading fields differ from the declared schema")
        if type(row["revision"]) is not int or row["revision"] < 1:
            raise ValueError("reading revision must be a positive integer")
        if type(row["units"]) is not int or not 0 <= row["units"] <= 1_000_000_000:
            raise ValueError("units must be a bounded non-negative integer")
    if {row["meter"] for row in rows} != METERS:
        raise ValueError("incomplete snapshot: meter_a and meter_b are both required")
    return manifest, rows, hashlib.sha256(raw_manifest).hexdigest()


def ingest(conn, item) -> None:
    manifest, rows, digest = item
    snapshot = manifest["snapshot"]
    old = conn.execute("SELECT digest FROM snapshots WHERE snapshot=%s", (snapshot,)).fetchone()
    if old:
        if old[0] != digest:
            raise ValueError(f"conflicting manifest for {snapshot}")
        return
    interval = (manifest["interval_start"], manifest["duration"])
    for row in rows:
        identity = (*interval, row["meter"], row["revision"])
        old = conn.execute(
            "SELECT units FROM readings WHERE interval_start=%s AND duration=%s "
            "AND meter=%s AND revision=%s",
            identity,
        ).fetchone()
        if old and old[0] != row["units"]:
            raise ValueError(f"conflicting reading revision: {identity}")
        newest = conn.execute(
            "SELECT max(revision) FROM readings"
            " WHERE interval_start=%s AND duration=%s AND meter=%s",
            (*interval, row["meter"]),
        ).fetchone()[0]
        if newest is not None and row["revision"] < newest:
            raise ValueError(f"stale reading in a new snapshot: {identity}")
        conn.execute(
            "INSERT INTO readings VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (*identity, row["units"]),
        )
    conn.execute(
        "INSERT INTO snapshots VALUES (%s,%s,%s,%s,%s)",
        (snapshot, *interval, manifest["revision"], digest),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO members VALUES (%s,%s,%s)", (snapshot, row["meter"], row["revision"])
        )


def calculate(conn, snapshot: str) -> None:
    rows = conn.execute(
        "SELECT r.meter,r.units FROM snapshots s JOIN members m USING(snapshot) "
        "JOIN readings r ON (r.interval_start,r.duration,r.meter,r.revision) = "
        "(s.interval_start,s.duration,m.meter,m.reading_revision) WHERE s.snapshot=%s",
        (snapshot,),
    ).fetchall()
    if len(rows) != 2 or {row[0] for row in rows} != METERS:
        raise ValueError("cannot calculate an incomplete accepted snapshot")
    total = sum(row[1] for row in rows)
    old = conn.execute("SELECT total FROM calculations WHERE snapshot=%s", (snapshot,)).fetchone()
    if old and old[0] != total:
        raise ValueError("stored calculation differs from immutable inputs")
    conn.execute(
        "INSERT INTO calculations VALUES (%s,%s) ON CONFLICT DO NOTHING", (snapshot, total)
    )


def observed_period(run: Path, manual: bool):
    if manual:
        return None, None
    status = query_status(run / "engine" / "control.sock")
    matches = [
        manifest
        for path in (run / "engine" / "periods").glob("[0-9]*/manifest.json")
        if (manifest := read_json(path))["baseline_id"] == status["baseline_id"]
    ]
    if len(matches) != 1 or matches[0]["clock_domain"] != "real":
        raise ValueError("live baseline must identify exactly one real-clock committed period")
    return matches[0]["period_id"], status["baseline_id"]


def publish(conn, snapshot: str, period) -> None:
    if conn.execute("SELECT 1 FROM publications WHERE snapshot=%s", (snapshot,)).fetchone():
        return
    current = conn.execute(
        "SELECT c.snapshot,c.revision,c.total,c.period_id FROM current_publications c "
        "JOIN snapshots s ON (s.interval_start,s.duration)=(c.interval_start,c.duration) "
        "WHERE s.snapshot=%s",
        (snapshot,),
    ).fetchone()
    target = conn.execute(
        "SELECT s.revision,c.total FROM snapshots s JOIN calculations c USING(snapshot) "
        "WHERE s.snapshot=%s",
        (snapshot,),
    ).fetchone()
    if target is None:
        raise ValueError("publication requires a complete calculated snapshot")
    if snapshot == "correction" and current is None:
        raise ValueError("correction requires the original publication")
    if current and target[0] <= current[1]:
        raise ValueError("refusing to publish a stale snapshot")
    if current and period[0] is not None:
        if current[3] is None or period[0] <= current[3]:
            raise ValueError("this example requires corrections in a later scheduler period")
    conn.execute("INSERT INTO publications VALUES (%s,%s,%s,%s)", (snapshot, target[1], *period))
    if current:
        conn.execute(
            "INSERT INTO adjustments VALUES (%s,%s,%s)",
            (snapshot, current[0], target[1] - current[2]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("action", choices=("init", "ingest", "calculate", "publish"))
    parser.add_argument("wave", nargs="?", choices=tuple(WAVES), default="initial")
    args = parser.parse_args()
    tag = os.environ.get("DSL41_RUN")
    if tag and len(tag) > 4096:
        raise ValueError("unexpected scheduler forensic tag length")
    write_json(
        args.run / "attempts" / f"{uuid.uuid4().hex}.json",
        {
            "action": args.action,
            "wave": args.wave,
            "pid": os.getpid(),
            "scheduler_run": tag,
            "origin": "scheduler" if tag else "manual" if args.manual else "probe",
        },
    )
    with ExitStack() as stack:
        if args.manual:
            lock = stack.enter_context((args.run / "engine" / "leader.lock").open("r+"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        items = (
            [bundle(args.run, key) for key in WAVES[args.wave]] if args.action == "ingest" else []
        )
        period = observed_period(args.run, args.manual) if args.action == "publish" else None
        with database(args.run, create=args.action == "init") as conn:
            if args.action == "init":
                conn.execute(DDL)
            else:
                conn.execute(
                    "LOCK TABLE readings,snapshots,members,calculations,publications,adjustments"
                    " IN EXCLUSIVE MODE"
                )
                if args.action == "ingest":
                    for item in items:
                        ingest(conn, item)
                else:
                    for snapshot in WAVES[args.wave]:
                        if args.action == "calculate":
                            calculate(conn, snapshot)
                        else:
                            publish(conn, snapshot, period)
    print(json.dumps({"action": args.action, "wave": args.wave, "period": period, "ok": True}))


if __name__ == "__main__":
    main()
