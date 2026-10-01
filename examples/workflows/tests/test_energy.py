"""Energy: a publication, then a late correction across a sealed dsl41 period.

Every run passes `--force-seal`. The seal records that override, and the
correction still crosses a committed period boundary; the default would only
add a one-minute wait for the control retry horizon. A seal that lands after
the horizon has passed records `forced_gate` null (period-model ss3.1), so the
gate is checked only when the seal records one.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from lane import Lane, Result, read_json, scheduler_rows, scheduler_run

TIMEOUT = 120
LEAVES = [
    "ENERGY_CORRECTION_CALCULATE",
    "ENERGY_CORRECTION_INGEST",
    "ENERGY_CORRECTION_PUBLISH",
    "ENERGY_INITIAL_CALCULATE",
    "ENERGY_INITIAL_INGEST",
    "ENERGY_INITIAL_PUBLISH",
]
COUNTS = {
    "adjustments": 1,
    "calculations": 3,
    "members": 6,
    "publications": 3,
    "readings": 5,
    "snapshots": 3,
}


def demo(lane: Lane) -> tuple[Result, Path]:
    result = lane.run("energy", "--force-seal", timeout=TIMEOUT).require()
    return result, lane.collected(result.run_dir())


def attempts(run: Path) -> list[dict[str, Any]]:
    """Records the worker writes on entry, one file per real invocation."""
    return [read_json(path) for path in sorted((run / "attempts").glob("*.json"))]


def scheduled_runs(run: Path) -> Counter[tuple[str, int]]:
    """(job, run number) each scheduler-started worker saw in its DSL41_RUN tag."""
    rows = [row for row in attempts(run) if row["origin"] == "scheduler"]
    tags = [scheduler_run(row["scheduler_run"]) for row in rows]
    return Counter((tag["job"], tag["run_number"]) for tag in tags)


def test_energy_publishes_both_periods_from_the_documented_run(lane: Lane) -> None:
    result, run = demo(lane)

    business = result.final_json()
    assert business["counts"] == COUNTS
    assert business["calculations"] == [["correction", 32], ["initial", 30], ["neighbor", 12]]
    initial = read_json(run / "initial-check.json")
    assert read_json(run / "complete-check.json") == business
    assert initial["calculations"] == [["initial", 30], ["neighbor", 12]]
    assert [row[:3] for row in initial["publications"]] == [["initial", 30, 1], ["neighbor", 12, 1]]

    rows = scheduler_rows(run / "run-history.json")
    assert sorted(rows) == [
        ("ENERGY_CORRECTION_B", 1, "SUCCESS", None),
        ("ENERGY_CORRECTION_CALCULATE", 1, "SUCCESS", 0),
        ("ENERGY_CORRECTION_INGEST", 1, "SUCCESS", 0),
        ("ENERGY_CORRECTION_PUBLISH", 1, "SUCCESS", 0),
        ("ENERGY_INITIAL_B", 1, "SUCCESS", None),
        ("ENERGY_INITIAL_CALCULATE", 1, "SUCCESS", 0),
        ("ENERGY_INITIAL_INGEST", 1, "FAILURE", 1),
        ("ENERGY_INITIAL_INGEST", 2, "SUCCESS", 0),
        ("ENERGY_INITIAL_PUBLISH", 1, "SUCCESS", 0),
    ]
    assert all(row["fidelity"] == "full" for row in read_json(run / "run-history.json"))

    records = attempts(run)
    assert Counter(row["origin"] for row in records) == {"scheduler": 7, "probe": 9}
    assert sorted({job for job, _ in scheduled_runs(run)}) == LEAVES


def test_energy_late_correction_crosses_the_sealed_period(lane: Lane) -> None:
    result, run = demo(lane)

    business = result.final_json()
    assert business["adjustments"] == [["correction", "initial", 2]]
    assert business["period_ids"] == [1, 2]
    assert [row[:3] for row in business["publications"]] == [
        ["correction", 32, 2],
        ["initial", 30, 1],
        ["neighbor", 12, 1],
    ]
    boundary = read_json(run / "boundary.json")
    assert (boundary["decision"], boundary["period_id"], boundary["next_period_id"]) == (
        "applied",
        1,
        2,
    )
    seal = read_json(run / "engine" / "seals" / "000001.json")
    assert seal["boundary_request"]["force_seal"] is True
    if seal["forced_gate"] is not None:
        assert seal["forced_gate"]["gate"] == "retry_horizon"
    assert (run / "engine" / "seals" / "000001.audit.json").is_file()

    rows = scheduler_rows(run / "run-history.json")
    assert [row for row in rows if row[0] == "ENERGY_INITIAL_INGEST"] == [
        ("ENERGY_INITIAL_INGEST", 1, "FAILURE", 1),
        ("ENERGY_INITIAL_INGEST", 2, "SUCCESS", 0),
    ]
    assert [(job, number) for job, number, _, _ in rows if job in LEAVES] == [
        (job, number)
        for job in LEAVES
        for number in ((1, 2) if job == "ENERGY_INITIAL_INGEST" else (1,))
    ]

    assert scheduled_runs(run) == Counter(
        {(job, 1): 1 for job in LEAVES} | {("ENERGY_INITIAL_INGEST", 2): 1}
    )
    probes = [json.loads(line) for line in (run / "worker-probes.jsonl").read_text().splitlines()]
    refusals = {
        "conflict": "conflicting reading revision",
        "respelled": "non-canonical interval_start",
        "stale": "stale reading in a new snapshot",
    }
    for wave, refusal in refusals.items():
        refused = [probe for probe in probes if probe["args"] == ["ingest", wave]]
        assert [probe["exit"] for probe in refused] == [1], wave
        assert refusal in refused[0]["stderr"], refused[0]["stderr"]

    again = lane.exec("python", "examples/energy/check.py", "--run", result.run_dir()).require()
    assert again.final_json() == business


def test_energy_checker_rejects_damaged_adjustment(lane: Lane) -> None:
    result, run = demo(lane)
    lane.exec("python", "examples/energy/check.py", "--run", result.run_dir()).require()

    schema = read_json(run / "config.json")["schema"]
    assert re.fullmatch(r"energy_[0-9a-f]{16}", schema), schema
    damaged = lane.psql(f'UPDATE "{schema}".adjustments SET delta = delta + 1 RETURNING delta')
    assert damaged == "3"

    refused = lane.exec("python", "examples/energy/check.py", "--run", result.run_dir())
    assert refused.exit not in (0, None), refused.describe()
    assert "AssertionError: [('correction', 'initial', 3)]" in refused.stderr, refused.describe()
