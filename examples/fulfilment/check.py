"""Independent observations of PostgreSQL, the carrier ledger, and dsl41 history."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote
from urllib.request import urlopen

import psycopg
from psycopg import sql


def check(run: Path, phase: str = "complete", offline_carrier: bool = False) -> dict:
    if not __debug__:
        raise RuntimeError("run this checker without Python optimization (-O)")
    config = json.loads((run / "config.json").read_text())
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=5,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    ) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(config["schema"])))
        assert conn.execute("SELECT name FROM wave").fetchall() == [(config["wave"],)]
        assert conn.execute("SELECT id,revision FROM orders ORDER BY id").fetchall() == [
            ("CANCEL", 1),
            ("SPLIT", 1),
        ]
        assert conn.execute(
            "SELECT id,order_id,sku,quantity FROM parcels ORDER BY id"
        ).fetchall() == [
            ("P1", "SPLIT", "WIDGET", 1),
            ("P2", "SPLIT", "WIDGET", 1),
            ("P3", "CANCEL", "WIDGET", 1),
        ]
        stock = conn.execute("SELECT initial,available,reserved,dispatched FROM stock").fetchall()
        orders = dict(conn.execute("SELECT id,state FROM orders").fetchall())
        parcels = dict(conn.execute("SELECT id,state FROM parcels").fetchall())
        operations = {
            key: (payload, result)
            for key, payload, result in conn.execute("SELECT key,payload,result FROM operations")
        }
        receipts = conn.execute(
            "SELECT key,payload,receipt FROM carrier_receipts ORDER BY key"
        ).fetchall()
        movements = {
            key: (available, reserved, dispatched)
            for key, available, reserved, dispatched in conn.execute(
                "SELECT key,available,reserved,dispatched FROM movements"
            )
        }
        movement_sums = conn.execute(
            "SELECT sum(available),sum(reserved),sum(dispatched),count(*) FROM movements"
        ).fetchone()
        attempts = dict(conn.execute("SELECT job,count(*) FROM attempts GROUP BY job").fetchall())
    if offline_carrier:
        uri = "file:" + quote(str(run / "carrier.sqlite")) + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=3)) as conn:
            external = [
                {
                    "key": row[0],
                    "payload": json.loads(row[1]),
                    "receipt": json.loads(row[2]),
                    "reply_dropped": bool(row[3]),
                }
                for row in conn.execute("SELECT * FROM receipts ORDER BY key")
            ]
    else:
        with urlopen(config["carrier_url"] + "/ledger", timeout=3) as response:
            external = json.load(response)["operations"]
    wave = config["wave"]
    keys = [f"{wave}/P1/1/label", f"{wave}/P2/1/label"]
    expected_payloads = {
        key: {
            "wave": wave,
            "target": parcel,
            "revision": 1,
            "action": "label",
            "order": "SPLIT",
            "sku": "WIDGET",
            "quantity": 1,
        }
        for key, parcel in zip(keys, ("P1", "P2"), strict=True)
    }
    if phase != "incident":
        expected_payloads[f"{wave}/WAVE/1/manifest"] = {
            "wave": wave,
            "target": "WAVE",
            "revision": 1,
            "action": "manifest",
            "labels": keys,
        }
    assert {row["key"]: row["payload"] for row in external} == expected_payloads
    assert len(external) == len(expected_payloads)
    assert len({row["receipt"]["id"] for row in external}) == len(external)
    assert [row["key"] for row in external if row["reply_dropped"]] == (
        [keys[1]] if config["incident"] else []
    )
    by_key = {row["key"]: row for row in external}
    for key, payload, receipt in receipts:
        assert by_key[key]["payload"] == payload and by_key[key]["receipt"] == receipt
        assert receipt["key"] == key and receipt["kind"] == payload["action"]
    # Every operation the worker replays from: exact keys, payloads and results.
    # Label and manifest results are the receipts recorded for the same key.
    local_receipts = {key: receipt for key, _, receipt in receipts}

    def operation(target: str, action: str, result: dict) -> tuple[str, tuple[dict, dict]]:
        key = f"{wave}/{target}/1/{action}"
        payload = {"wave": wave, "target": target, "revision": 1, "action": action}
        return key, (payload, result)

    def receipted(target: str, action: str) -> tuple[str, tuple[dict, dict]]:
        key = f"{wave}/{target}/1/{action}"
        assert key in local_receipts, f"no carrier receipt recorded for {key}"
        return operation(target, action, local_receipts[key])

    done = {"state": "done"}
    expected_operations = dict(
        [
            operation("WAVE", "reserve", done),
            operation("CANCEL", "cancel", {"state": "cancelled"}),
            operation("SPLIT", "pack", done),
            operation("SPLIT", "cancel", {"state": "refused", "reason": "packing cutoff passed"}),
            receipted("P1", "label"),
        ]
    )
    # Each stock movement, attributed to the operation that made it.
    expected_movements = {
        f"{wave}/WAVE/1/reserve": (-3, 3, 0),
        f"{wave}/CANCEL/1/cancel": (1, -1, 0),
        f"{wave}/P1/1/label": (0, -1, 1),
    }
    if phase == "incident":
        assert config["incident"] is True
        assert stock == [(3, 1, 1, 1)]
        assert orders == {"SPLIT": "packed", "CANCEL": "cancelled"}
        assert parcels == {"P1": "dispatched", "P2": "packed", "P3": "cancelled"}
        assert [row[0] for row in receipts] == keys[:1]
        assert movement_sums == (-2, 1, 1, 3)
    else:
        assert stock == [(3, 1, 0, 2)]
        assert orders == {"SPLIT": "fulfilled", "CANCEL": "cancelled"}
        assert parcels == {"P1": "dispatched", "P2": "dispatched", "P3": "cancelled"}
        assert {row[0] for row in receipts} == set(expected_payloads)
        assert movement_sums == (-2, 0, 2, 4)
        expected_operations.update([receipted("P2", "label"), receipted("WAVE", "manifest")])
        expected_movements[f"{wave}/P2/1/label"] = (0, -1, 1)
    assert operations == expected_operations, operations
    assert movements == expected_movements, movements
    if phase == "complete":
        history = json.loads((run / "runs.json").read_text())
        leaves = [row for row in history if row["job"] != "FF_WAVE_B"]
        expected = dict.fromkeys(
            [
                "FF_RESERVE_C",
                "FF_CANCEL_EARLY_C",
                "FF_PACK_C",
                "FF_CANCEL_LATE_C",
                "FF_LABEL_1_C",
                "FF_LABEL_2_C",
                "FF_MANIFEST_C",
            ],
            1,
        )
        expected["FF_LABEL_2_C"] += int(config["incident"])
        assert attempts == expected == dict(Counter(row["job"] for row in leaves))
        assert all(row["fidelity"] == "full" and not row["undecided"] for row in history)
        # The complete execution history: the box, every run number, status and exit code.
        executions = sorted(
            (row["job"], row["run_number"], row["status"], row["exit_code"]) for row in history
        )
        expected_executions = [("FF_WAVE_B", 1, "SUCCESS", None)] + [
            (job, 1, "SUCCESS", 0) for job in expected
        ]
        if config["incident"]:
            expected_executions.remove(("FF_LABEL_2_C", 1, "SUCCESS", 0))
            expected_executions += [
                ("FF_LABEL_2_C", 1, "FAILURE", 1),
                ("FF_LABEL_2_C", 2, "SUCCESS", 0),
            ]
        assert executions == sorted(expected_executions), executions
        assert json.loads((run / "identity-conflict.json").read_text())["http_status"] == 409
    return {
        "phase": phase,
        "stock": stock[0],
        "carrier_effects": len(external),
        "worker_attempts": attempts,
        "schema": config["schema"],
        "wave": wave,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--phase", choices=["incident", "complete", "business"], default="complete")
    parser.add_argument("--offline-carrier", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.run, args.phase, args.offline_carrier), indent=2))
