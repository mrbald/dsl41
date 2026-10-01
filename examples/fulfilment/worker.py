"""One bounded business operation per invocation. No scheduling or automatic retry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


def connect(config: dict):
    conn = psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=5,
        options=(
            "-c statement_timeout=10000 -c lock_timeout=5000 "
            "-c idle_in_transaction_session_timeout=15000"
        ),
    )
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(config["schema"])))
    conn.commit()
    return conn


def initialise(config: dict) -> None:
    with connect(config) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(config["schema"])))
        conn.execute((Path(__file__).with_name("schema.sql")).read_text())
        conn.execute("INSERT INTO wave VALUES (%s)", (config["wave"],))


def carrier(config: dict, key: str, payload: dict) -> dict:
    url = config["carrier_url"] + "/operations"
    try:
        with urlopen(url + "/" + quote(key, safe=""), timeout=3) as response:
            found = json.load(response)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        request = Request(
            url,
            json.dumps({"key": key, "payload": payload}).encode(),
            {"Content-Type": "application/json"},
        )
        # A dropped reply escapes to main. There is no immediate retry.
        with urlopen(request, timeout=3) as response:
            found = json.load(response)
    if found["key"] != key or found["payload"] != payload:
        raise ValueError("carrier identity is bound to different content")
    return found["receipt"]


def operate(config: dict, action: str, target: str, job: str) -> None:
    allowed = {
        "reserve": {"WAVE"},
        "cancel": {"CANCEL", "SPLIT"},
        "pack": {"SPLIT"},
        "label": {"P1", "P2"},
        "manifest": {"WAVE"},
    }
    if target not in allowed[action]:
        raise ValueError("operation does not name an admitted fixture entity")
    with connect(config) as conn:
        conn.execute("INSERT INTO attempts(job) VALUES (%s)", (job,))
    key = f"{config['wave']}/{target}/1/{action}"
    payload = {"wave": config["wave"], "target": target, "revision": 1, "action": action}
    with connect(config) as conn:
        # All wave changes share a bounded lock. Scheduler slots are not inventory.
        conn.execute("SELECT sku FROM stock WHERE sku = 'WIDGET' FOR UPDATE").fetchone()
        if conn.execute("SELECT name FROM wave").fetchall() != [(config["wave"],)]:
            raise ValueError("worker configuration names another business wave")
        if conn.execute("SELECT revision FROM orders ORDER BY id").fetchall() != [(1,), (1,)]:
            raise ValueError("worker requires the admitted order revision")
        previous = conn.execute(
            "SELECT payload, result FROM operations WHERE key=%s", (key,)
        ).fetchone()
        if previous:
            if previous[0] != payload:
                raise ValueError("business operation identity conflicts")
            print(json.dumps(previous[1]))
            return
        delta = (0, 0, 0)
        result = {"state": "done"}
        if action == "reserve":
            states = conn.execute("SELECT state FROM parcels").fetchall()
            if states != [("accepted",)] * 3:
                raise ValueError("reservation requires the original accepted wave")
            conn.execute("UPDATE parcels SET state='reserved'")
            conn.execute("UPDATE orders SET state='reserved'")
            delta = (-3, 3, 0)
        elif action == "cancel":
            state = conn.execute("SELECT state FROM orders WHERE id=%s", (target,)).fetchone()[0]
            if state in {"packed", "fulfilled"}:
                result = {"state": "refused", "reason": "packing cutoff passed"}
            elif state == "reserved" and target == "CANCEL":
                conn.execute("UPDATE orders SET state='cancelled' WHERE id=%s", (target,))
                conn.execute("UPDATE parcels SET state='cancelled' WHERE order_id=%s", (target,))
                delta = (1, -1, 0)
                result = {"state": "cancelled"}
            else:
                raise ValueError("cancellation is not eligible")
        elif action == "pack":
            if (
                conn.execute("SELECT state FROM orders WHERE id='SPLIT'").fetchone()[0]
                != "reserved"
            ):
                raise ValueError("packing requires a reserved order")
            conn.execute("UPDATE parcels SET state='packed' WHERE order_id='SPLIT'")
            conn.execute("UPDATE orders SET state='packed' WHERE id='SPLIT'")
        elif action == "label":
            parcel = conn.execute(
                "SELECT order_id, sku, quantity, state FROM parcels WHERE id=%s", (target,)
            ).fetchone()
            if parcel is None or parcel[3] != "packed":
                raise ValueError("label requires a packed parcel")
            external = {**payload, "order": parcel[0], "sku": parcel[1], "quantity": parcel[2]}
            receipt = carrier(config, key, external)
            conn.execute(
                "INSERT INTO carrier_receipts VALUES (%s,%s,%s)",
                (key, Jsonb(external), Jsonb(receipt)),
            )
            conn.execute("UPDATE parcels SET state='dispatched' WHERE id=%s", (target,))
            delta = (0, -parcel[2], parcel[2])
            result = receipt
        elif action == "manifest":
            states = conn.execute("SELECT state FROM parcels WHERE order_id='SPLIT'").fetchall()
            if states != [("dispatched",)] * 2:
                raise ValueError("both split parcels must dispatch before fulfilment")
            labels = conn.execute("SELECT key FROM carrier_receipts ORDER BY key").fetchall()
            external = {**payload, "labels": [row[0] for row in labels]}
            receipt = carrier(config, key, external)
            conn.execute(
                "INSERT INTO carrier_receipts VALUES (%s,%s,%s)",
                (key, Jsonb(external), Jsonb(receipt)),
            )
            conn.execute("UPDATE orders SET state='fulfilled' WHERE id='SPLIT'")
            result = receipt
        else:
            raise ValueError(f"unknown action {action}")
        conn.execute(
            "INSERT INTO operations VALUES (%s,%s,%s)", (key, Jsonb(payload), Jsonb(result))
        )
        if any(delta):
            conn.execute("INSERT INTO movements VALUES (%s,%s,%s,%s)", (key, *delta))
            conn.execute(
                "UPDATE stock SET available=available+%s,reserved=reserved+%s,"
                "dispatched=dispatched+%s WHERE sku='WIDGET'",
                delta,
            )
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("action", choices=["reserve", "cancel", "pack", "label", "manifest"])
    parser.add_argument("target")
    args = parser.parse_args()
    config = json.loads((args.run / "config.json").read_text())
    operate(config, args.action, args.target, args.job)


if __name__ == "__main__":
    main()
