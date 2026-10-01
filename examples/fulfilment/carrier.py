"""Local carrier simulator. Its SQLite commit is independent of warehouse PostgreSQL."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote


def serve(ledger: Path, ready: Path, drop_key: str) -> None:
    with closing(sqlite3.connect(ledger, timeout=3)) as conn, conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS receipts "
            "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, receipt TEXT NOT NULL, "
            "reply_dropped INTEGER NOT NULL)"
        )

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def answer(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def record(self, row):
            return {
                "key": row[0],
                "payload": json.loads(row[1]),
                "receipt": json.loads(row[2]),
                "reply_dropped": bool(row[3]),
            }

        def do_GET(self):
            with closing(sqlite3.connect(ledger, timeout=3)) as conn, conn:
                if self.path == "/ledger":
                    rows = conn.execute("SELECT * FROM receipts ORDER BY key").fetchall()
                    self.answer(200, {"operations": [self.record(row) for row in rows]})
                elif self.path.startswith("/operations/"):
                    key = unquote(self.path.removeprefix("/operations/"))
                    row = conn.execute("SELECT * FROM receipts WHERE key=?", (key,)).fetchone()
                    self.answer(200 if row else 404, self.record(row) if row else {"absent": key})
                else:
                    self.answer(404, {"error": "unknown endpoint"})

        def do_POST(self):
            if self.path != "/operations":
                self.answer(404, {"error": "unknown endpoint"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("invalid request length")
                request = json.loads(self.rfile.read(length))
                key, payload = request["key"], request["payload"]
                if not isinstance(key, str) or not isinstance(payload, dict):
                    raise ValueError("invalid identity or payload")
                action = payload["action"]
                if action not in {"label", "manifest"}:
                    raise ValueError("unsupported carrier action")
            except (ValueError, KeyError, TypeError):
                self.answer(400, {"error": "invalid operation"})
                return
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            dropped = False
            with closing(sqlite3.connect(ledger, timeout=3)) as conn, conn:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM receipts WHERE key=?", (key,)).fetchone()
                if row and row[1] != encoded:
                    self.answer(409, {"error": "identity already binds different payload"})
                    return
                if row is None:
                    if action == "manifest":
                        labels = payload.get("labels", [])
                        if not labels or any(
                            conn.execute(
                                "SELECT key FROM receipts WHERE key=?", (label,)
                            ).fetchone()
                            is None
                            for label in labels
                        ):
                            self.answer(409, {"error": "manifest labels absent"})
                            return
                    receipt = {
                        "id": action + "-" + hashlib.sha256(key.encode()).hexdigest()[:16],
                        "key": key,
                        "kind": action,
                    }
                    dropped = key == drop_key
                    row = (key, encoded, json.dumps(receipt), int(dropped))
                    conn.execute("INSERT INTO receipts VALUES (?,?,?,?)", row)
            # The transaction and fault marker committed before the connection closes.
            if dropped:
                self.log_message("dropping the reply to POST /operations for %s", key)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.answer(200, self.record(row))

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"url": f"http://127.0.0.1:{server.server_port}", "sqlite": sqlite3.sqlite_version}
            )
        )
        temporary.replace(ready)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--drop-key", default="")
    args = parser.parse_args()
    serve(args.ledger, args.ready, args.drop_key)
