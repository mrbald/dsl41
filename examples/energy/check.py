"""Independent observations of the energy example; imports no worker code."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import json
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql


def check(run: Path, phase: str) -> dict:
    if not __debug__:
        raise RuntimeError("run this checker without Python optimization (-O)")
    config = json.loads((run / "config.json").read_text())
    if not re.fullmatch(r"energy_[0-9a-f]{16}", config["schema"]):
        raise ValueError("invalid schema")
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=3,
        options="-c statement_timeout=10000 -c default_transaction_read_only=on",
    ) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(config["schema"])))
        readings = conn.execute(
            "SELECT interval_start,duration,meter,revision,units FROM readings ORDER BY 1,2,3,4"
        ).fetchall()
        snapshots = conn.execute(
            "SELECT snapshot,interval_start,duration,revision FROM snapshots ORDER BY snapshot"
        ).fetchall()
        members = conn.execute(
            "SELECT snapshot,meter,reading_revision FROM members ORDER BY snapshot,meter"
        ).fetchall()
        calculations = conn.execute(
            "SELECT snapshot,total FROM calculations ORDER BY snapshot"
        ).fetchall()
        publications = conn.execute(
            "SELECT snapshot,total,period_id,baseline FROM publications ORDER BY snapshot"
        ).fetchall()
        adjustments = conn.execute("SELECT * FROM adjustments ORDER BY snapshot").fetchall()
        current = conn.execute(
            "SELECT interval_start,revision,total FROM current_publications ORDER BY interval_start"
        ).fetchall()
        counts = {
            name: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
            ).fetchone()[0]
            for name in (
                "readings",
                "snapshots",
                "members",
                "calculations",
                "publications",
                "adjustments",
            )
        }
    target, neighbor = "2026-01-15T00:00:00Z", "2026-01-15T00:15:00Z"
    expected = [
        (target, 900, "meter_a", 1, 10),
        (target, 900, "meter_b", 1, 20),
        (neighbor, 900, "meter_a", 1, 5),
        (neighbor, 900, "meter_b", 1, 7),
    ]
    expected_snapshots = [("initial", target, 900, 1), ("neighbor", neighbor, 900, 1)]
    expected_members = [
        (name, meter, 1) for name in ("initial", "neighbor") for meter in ("meter_a", "meter_b")
    ]
    expected_calculations = [("initial", 30), ("neighbor", 12)]
    first = json.loads((run / "engine" / "wal" / "000001.jsonl").read_text().splitlines()[0])
    assert first["rec"] == "segment" and first["period_id"] == 1 and first["clock_domain"] == "real"
    expected_publications = [
        ("initial", 30, 1, first["baseline_id"]),
        ("neighbor", 12, 1, first["baseline_id"]),
    ]
    period_ids = [1]
    if phase != "initial":
        second = json.loads((run / "engine" / "wal" / "000002.jsonl").read_text().splitlines()[0])
        seal = json.loads((run / "engine" / "seals" / "000001.json").read_text())
        assert second["rec"] == "segment" and second["period_id"] == 2
        assert second["clock_domain"] == "real" and second["estate_id"] == first["estate_id"]
        assert first["baseline_id"] != second["baseline_id"]
        assert second["opens_from_seal"] == {"period_id": 1, "digest": seal["digest"]}
        expected.append((target, 900, "meter_a", 2, 12))
        expected_snapshots.append(("correction", target, 900, 2))
        expected_members.extend([("correction", "meter_a", 2), ("correction", "meter_b", 1)])
        expected_calculations.append(("correction", 32))
        expected_publications.append(("correction", 32, 2, second["baseline_id"]))
        assert adjustments == [("correction", "initial", 2)], adjustments
        assert current == [(target, 2, 32), (neighbor, 1, 12)], current
        assert counts == dict(
            readings=5, snapshots=3, members=6, calculations=3, publications=3, adjustments=1
        )
        period_ids.append(2)
    else:
        assert adjustments == [] and current == [(target, 1, 30), (neighbor, 1, 12)]
        assert counts == dict(
            readings=4, snapshots=2, members=4, calculations=2, publications=2, adjustments=0
        )
    assert readings == sorted(expected), readings
    assert snapshots == sorted(expected_snapshots), snapshots
    assert members == sorted(expected_members), members
    assert calculations == sorted(expected_calculations), calculations
    assert publications == sorted(expected_publications), publications
    attempt_counts = None
    if phase == "complete":
        history = json.loads((run / "run-history.json").read_text())
        leaves = [row for row in history if not row["job"].endswith("_B")]
        attempts = [json.loads(path.read_text()) for path in (run / "attempts").glob("*.json")]
        scheduled = [row for row in attempts if row["origin"] == "scheduler"]
        identities = [
            json.loads(base64.urlsafe_b64decode(row["scheduler_run"])) for row in scheduled
        ]
        expected_jobs = Counter(
            {
                "ENERGY_INITIAL_INGEST": 2,
                "ENERGY_INITIAL_CALCULATE": 1,
                "ENERGY_INITIAL_PUBLISH": 1,
                "ENERGY_CORRECTION_INGEST": 1,
                "ENERGY_CORRECTION_CALCULATE": 1,
                "ENERGY_CORRECTION_PUBLISH": 1,
            }
        )
        assert Counter(row["job"] for row in leaves) == expected_jobs
        assert Counter(row["job"] for row in identities) == expected_jobs
        assert Counter((row["job"], row["run_number"]) for row in identities) == Counter(
            (row["job"], row["run_number"]) for row in leaves
        )
        assert len({row["run_id"] for row in identities}) == 7
        assert all(row["fidelity"] == "full" and not row["undecided"] for row in history)
        failed = [
            (row["job"], row["run_number"], row["status"])
            for row in leaves
            if row["status"] != "SUCCESS"
        ]
        assert failed == [("ENERGY_INITIAL_INGEST", 1, "FAILURE")], failed
        assert Counter(
            (row["action"], row["wave"]) for row in attempts if row["origin"] == "probe"
        ) == Counter(
            {
                ("init", "initial"): 1,
                ("ingest", "initial"): 2,
                ("ingest", "conflict"): 1,
                ("ingest", "correction"): 1,
                ("publish", "correction"): 1,
                ("publish", "initial"): 1,
            }
        )
        assert len(attempts) == 14
        attempt_counts = dict(expected_jobs)
    return {
        "phase": phase,
        "counts": counts,
        "publications": publications,
        "snapshots": snapshots,
        "members": members,
        "calculations": calculations,
        "adjustments": adjustments,
        "estate_id": first["estate_id"],
        "period_ids": period_ids,
        "scheduler_worker_attempts": attempt_counts,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--phase", choices=("initial", "corrected", "complete"), default="complete")
    args = parser.parse_args()
    print(json.dumps(check(args.run, args.phase), indent=2))
