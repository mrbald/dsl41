"""Fulfilment: a warehouse wave against PostgreSQL and an HTTP carrier."""

from __future__ import annotations

import re
from pathlib import Path

from lane import Lane, Result, read_json, scheduler_rows

TIMEOUT = 120
JOBS = [
    "FF_CANCEL_EARLY_C",
    "FF_CANCEL_LATE_C",
    "FF_LABEL_1_C",
    "FF_LABEL_2_C",
    "FF_MANIFEST_C",
    "FF_PACK_C",
    "FF_RESERVE_C",
]


def schema(run: Path) -> str:
    value = read_json(run / "config.json")["schema"]
    assert re.fullmatch(r"fulfilment_[0-9a-f]{32}", value), value
    return value


def worker_attempts(lane: Lane, run: Path) -> dict[str, int]:
    """Attempt rows each worker commits before its business transaction."""
    rows = lane.psql(f'SELECT job, count(*) FROM "{schema(run)}".attempts GROUP BY job')
    return {job: int(count) for job, count in (line.split("|") for line in rows.splitlines())}


def recheck(lane: Lane, run_dir: str) -> Result:
    """The README's offline recheck against the retained carrier ledger."""
    script = '. "$1/profile.sh" && python examples/fulfilment/check.py --run "$1" --offline-carrier'
    return lane.exec("sh", "-c", script, "sh", run_dir)


def test_fulfilment_normal_wave(lane: Lane) -> None:
    result = lane.run("fulfilment", "--no-incident", timeout=TIMEOUT).require()
    run_dir = result.run_dir()
    run = lane.collected(run_dir)

    business = result.final_json()
    assert business["phase"] == "complete"
    assert business["stock"] == [3, 1, 0, 2]
    assert business["carrier_effects"] == 3
    assert read_json(run / "identity-conflict.json") == {"http_status": 409}
    assert not (run / "check-incident.json").exists()

    rows = scheduler_rows(run / "runs.json")
    assert [(job, number) for job, number, _, _ in rows] == [
        (job, 1) for job in sorted([*JOBS, "FF_WAVE_B"])
    ]
    assert {status for _, _, status, _ in rows} == {"SUCCESS"}
    assert all(row["fidelity"] == "full" for row in read_json(run / "runs.json"))

    assert worker_attempts(lane, run) == dict.fromkeys(JOBS, 1)


def test_fulfilment_lost_label_reply_incident(lane: Lane) -> None:
    result = lane.run("fulfilment", timeout=TIMEOUT).require()
    run_dir = result.run_dir()
    run = lane.collected(run_dir)

    business = result.final_json()
    assert business["stock"] == [3, 1, 0, 2]
    assert business["carrier_effects"] == 3
    incident = read_json(run / "check-incident.json")
    assert incident["phase"] == "incident" and incident["stock"] == [3, 1, 1, 1]

    rows = scheduler_rows(run / "runs.json")
    assert [row for row in rows if row[0] == "FF_LABEL_2_C"] == [
        ("FF_LABEL_2_C", 1, "FAILURE", 1),
        ("FF_LABEL_2_C", 2, "SUCCESS", 0),
    ]
    others = [row for row in rows if row[0] != "FF_LABEL_2_C"]
    assert [(job, number, status) for job, number, status, _ in others] == [
        (job, 1, "SUCCESS") for job in sorted([*JOBS, "FF_WAVE_B"]) if job != "FF_LABEL_2_C"
    ]
    commands = [line for line in (run / "commands.jsonl").read_text().splitlines() if line]
    assert any('"FORCE_STARTJOB"' in line for line in commands)

    assert worker_attempts(lane, run) == {**dict.fromkeys(JOBS, 1), "FF_LABEL_2_C": 2}
    first = (run / "engine" / "logs" / "FF_LABEL_2_C.1.err").read_text()
    assert "RemoteDisconnected" in first

    again = recheck(lane, run_dir).require()
    assert again.final_json() == business


def test_fulfilment_checker_rejects_damaged_stock(lane: Lane) -> None:
    run_dir = lane.run("fulfilment", "--no-incident", timeout=TIMEOUT).require().run_dir()
    run = lane.collected(run_dir)
    recheck(lane, run_dir).require()

    # One more unit dispatched than the wave shipped; conservation still holds.
    damaged = lane.psql(
        f'UPDATE "{schema(run)}".stock SET available = available - 1,'
        " dispatched = dispatched + 1 RETURNING initial, available, reserved, dispatched"
    )
    assert damaged == "3|0|0|3"

    refused = recheck(lane, run_dir)
    assert refused.exit not in (0, None), refused.describe()
    assert "AssertionError" in refused.stderr, refused.describe()
    assert "assert stock == [(3, 1, 0, 2)]" in refused.stderr, refused.describe()


def test_fulfilment_checker_rejects_damaged_operation_payload(lane: Lane) -> None:
    run_dir = lane.run("fulfilment", "--no-incident", timeout=TIMEOUT).require().run_dir()
    run = lane.collected(run_dir)
    recheck(lane, run_dir).require()

    # A rerun of FF_PACK_C would replay from this record; the schema still accepts it.
    damaged = lane.psql(
        f'UPDATE "{schema(run)}".operations'
        " SET payload = jsonb_set(payload, '{revision}', '2')"
        " WHERE key LIKE '%/SPLIT/1/pack' RETURNING payload->>'revision'"
    )
    assert damaged == "2"

    refused = recheck(lane, run_dir)
    assert refused.exit not in (0, None), refused.describe()
    assert "AssertionError" in refused.stderr, refused.describe()
    assert "assert operations == expected_operations" in refused.stderr, refused.describe()
