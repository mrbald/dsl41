"""Edits to a run root's store between a crash and a restart, for the tests that model a crash
window by removing or adding records.

No engine may hold the store while these run: they open their own connection and write under it,
as nothing could while an engine held the root's lock.
"""

from __future__ import annotations

from contextlib import closing

import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from dsl41.canon import canonical_bytes, decode
from dsl41.runner_journal import STORE_NAME


def _db(run_root: Path) -> sqlite3.Connection:
    path = run_root if run_root.name == STORE_NAME else run_root / STORE_NAME
    return sqlite3.connect(path, isolation_level=None)


def rows(run_root: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """Every journal row: seq, kind and decoded payload."""
    with closing(_db(run_root)) as db:
        found = db.execute("SELECT seq, kind, payload FROM journal ORDER BY seq").fetchall()
    out = []
    for seq, kind, payload in found:
        body = decode(payload)
        assert isinstance(body, dict)
        out.append((int(seq), str(kind), body))
    return out


def delete(run_root: Path, which: Callable[[int, str, dict[str, Any]], bool]) -> list[int]:
    """Delete the rows `which` selects; answer their seqs."""
    seqs = [seq for seq, kind, body in rows(run_root) if which(seq, kind, body)]
    db = _db(run_root)
    try:
        db.executemany("DELETE FROM journal WHERE seq = ?", [(seq,) for seq in seqs])
    finally:
        db.close()
    return seqs


def append(
    run_root: Path,
    kind: str,
    payload: Mapping[str, Any],
    *,
    at: datetime,
    request_id: str | None = None,
) -> int:
    """Append one row as the engine's writer would; answer its seq."""
    db = _db(run_root)
    try:
        cursor = db.execute(
            "INSERT INTO journal(at_utc, kind, actor, request_id, payload) VALUES (?, ?, ?, ?, ?)",
            (
                at.isoformat(timespec="microseconds"),
                kind,
                None,
                request_id,
                canonical_bytes(dict(payload)),
            ),
        )
        seq = cursor.lastrowid
        assert seq is not None
        return seq
    finally:
        db.close()


def update(run_root: Path, seq: int, payload: Mapping[str, Any]) -> None:
    """Replace one row's payload."""
    db = _db(run_root)
    try:
        db.execute(
            "UPDATE journal SET payload = ? WHERE seq = ?", (canonical_bytes(dict(payload)), seq)
        )
    finally:
        db.close()


def drop_snapshots(run_root: Path) -> None:
    """Delete every snapshot, so a restart replays the whole journal."""
    db = _db(run_root)
    try:
        db.execute("DELETE FROM snapshots")
    finally:
        db.close()
