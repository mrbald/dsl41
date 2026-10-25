"""Experimental single-host file workers, independent of the scheduler.

The producer atomically publishes complete, immutable, uniquely named inbox
files. The root is private to one pipeline and one processor identity. SQLite
owns receipts and work; JSON TODO files are its recoverable outbox projection.
There is no transaction spanning SQLite, a handler's effects and the filesystem.
See examples/file_processing/README.md for the protocol and its limits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from types import MappingProxyType
from typing import Literal, Protocol

from dsl41.file_processing_charts import ARCHIVE, PROCESS, state_after
from dsl41.state_machine import StatechartError


class ProtocolError(ValueError):
    """Corrupt, inconsistent or unsupported worker input. Stop without skipping it."""


class LockBusy(RuntimeError):
    """Another invocation owns this role's OS lock."""


@dataclass(frozen=True)
class Policy:
    order: Literal["oldest", "newest"] = "oldest"
    batch: Literal["one", "all"] = "all"

    def __post_init__(self) -> None:
        if self.order not in ("oldest", "newest") or self.batch not in ("one", "all"):
            raise ValueError("policy requires oldest/newest and one/all")


@dataclass(frozen=True)
class Summary:
    """Role-local counts. remaining covers admitted ledger work, not later arrivals."""

    selected: int = 0
    completed: int = 0
    duplicates: int = 0
    remaining: int = 0
    unknown: int = 0
    reconciled: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class WorkItem:
    """receipt_sequence is the admission sequence of the delivery creating this work row."""

    input_path: Path
    processing_key: str
    content_hash: str
    receipt_sequence: int
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class Completed:
    evidence: str


@dataclass(frozen=True)
class RetrySafe:
    reason: str


@dataclass(frozen=True)
class Unknown:
    reason: str


type Result = Completed | RetrySafe | Unknown


class Handler(Protocol):
    def process(self, item: WorkItem) -> Result: ...


class ReconcilingHandler(Handler, Protocol):
    def reconcile(self, item: WorkItem) -> Result: ...


type Failpoint = Callable[[str], None]
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def processing_key(processor_id: str, content_hash: str) -> str:
    return hashlib.sha256(_json([processor_id, content_hash])).hexdigest()


def _json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ProtocolError(f"not a regular file: {path}")


@contextmanager
def _read(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProtocolError(f"not a regular file: {path}")
        yield fd
    finally:
        os.close(fd)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with _read(path) as fd:
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_object(root: Path, content_hash: str) -> Path:
    if not _HEX.fullmatch(content_hash):
        raise ProtocolError("invalid archived object identity")
    path = root / "objects" / content_hash
    try:
        actual = _hash(path)
    except OSError as exc:
        raise ProtocolError(f"missing or unreadable archived object: {path}") from exc
    if actual != content_hash:
        raise ProtocolError(f"corrupt archived object: {path}")
    return path


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _lock(root: Path, role: str) -> Iterator[None]:
    fd = os.open(root / f"{role}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProtocolError("lock is not regular")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(role) from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def _write(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def _open(root: Path, processor_id: str) -> sqlite3.Connection:
    ledger = root / "ledger.sqlite3"
    if ledger.is_symlink():
        raise ProtocolError("symlink ledger")
    if not ledger.exists() and (
        any(any((root / name).iterdir()) for name in ("objects", "todo", "tmp"))
        or any(root.glob("ledger.sqlite3-*"))
    ):
        raise ProtocolError("missing ledger in an established worker root")
    db = sqlite3.connect(ledger, isolation_level=None, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        with _write(db):
            db.execute("CREATE TABLE IF NOT EXISTS identity (processor_id TEXT NOT NULL)")
            rows = db.execute("SELECT processor_id FROM identity").fetchall()
            if not rows:
                db.execute("INSERT INTO identity VALUES (?)", (processor_id,))
            elif len(rows) != 1 or rows[0][0] != processor_id:
                raise ProtocolError("root belongs to a different processor identity")
            db.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,"
                "state TEXT NOT NULL CHECK(state IN ('FILE_RECEIVED','FILE_ARCHIVED')),"
                "content_hash TEXT, work_key TEXT, cleaned INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS work ("
                "work_key TEXT PRIMARY KEY, content_hash TEXT UNIQUE NOT NULL,"
                "receipt_seq INTEGER NOT NULL REFERENCES receipts(seq), metadata TEXT NOT NULL,"
                "state TEXT NOT NULL CHECK(state IN ('READY','RUNNING','DONE','UNKNOWN')),"
                "evidence TEXT, detail TEXT)"
            )
        _sync_dir(root)
        return db
    except BaseException:
        db.close()
        raise


def _layout(root: Path, processor_id: str) -> None:
    if not processor_id or "\x00" in processor_id:
        raise ValueError("processor_id must be nonempty and contain no NUL")
    root.mkdir(parents=True, exist_ok=True)
    for path in (root, *(root / name for name in ("inbox", "objects", "todo", "tmp"))):
        path.mkdir(exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ProtocolError(f"not a real directory: {path}")
    _sync_dir(root)


def _todo(processor_id: str, row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema": 1,
        "processor_id": processor_id,
        "processing_key": row["work_key"],
        "content_hash": row["content_hash"],
        "receipt_sequence": row["receipt_seq"],
        "metadata": json.loads(row["metadata"]),
    }


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load(path: Path) -> object:
    with _read(path) as fd:
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_unique_pairs)


def _validate_todo(path: Path, processor_id: str, row: sqlite3.Row) -> None:
    value = _load(path)
    expected = _todo(processor_id, row)
    if (
        not isinstance(value, dict)
        or type(value.get("schema")) is not int
        or type(value.get("receipt_sequence")) is not int
        or value != expected
    ):
        raise ProtocolError(f"TODO differs from ledger: {path}")


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    _sync_dir(path.parent)


def _publish_todo(root: Path, db: sqlite3.Connection, processor_id: str, key: str) -> None:
    """Serialize TODO eligibility and publication with DONE under a write transaction."""
    with _write(db):
        row = db.execute("SELECT * FROM work WHERE work_key=?", (key,)).fetchone()
        assert row is not None
        target = root / "todo" / f"{key}.json"
        if target.exists() or target.is_symlink():
            _validate_todo(target, processor_id, row)
            if row["state"] == "DONE":
                _remove(target)
            else:
                _sync_dir(target.parent)
            return
        if row["state"] == "DONE":
            return
        fd, name = tempfile.mkstemp(prefix="todo-", dir=root / "tmp")
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_json(_todo(processor_id, row)))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, target)
            _sync_dir(target.parent)
        finally:
            temp.unlink(missing_ok=True)


def _repair(root: Path, db: sqlite3.Connection, processor_id: str) -> None:
    for path in (root / "tmp").iterdir():
        if path.name.startswith(("archive-", "todo-")):
            _regular(path)
            path.unlink()
    for row in db.execute("SELECT work_key FROM work ORDER BY receipt_seq").fetchall():
        _publish_todo(root, db, processor_id, row[0])
    for row in db.execute(
        "SELECT * FROM receipts WHERE state='FILE_ARCHIVED' AND cleaned=0 ORDER BY seq"
    ).fetchall():
        _cleanup_receipt(root, db, row)


def _cleanup_receipt(root: Path, db: sqlite3.Connection, row: sqlite3.Row) -> None:
    _verified_object(root, row["content_hash"])
    source = root / "inbox" / row["name"]
    if source.exists() or source.is_symlink():
        if _hash(source) != row["content_hash"]:
            raise ProtocolError(f"archived inbox file changed: {source}")
        _remove(source)
    else:
        _sync_dir(source.parent)
    with _write(db):
        db.execute("UPDATE receipts SET cleaned=1 WHERE seq=?", (row["seq"],))


def _object(root: Path, source: Path) -> str:
    fd, name = tempfile.mkstemp(prefix="archive-", dir=root / "tmp")
    temp = Path(name)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as stream, _read(source) as incoming:
            while chunk := os.read(incoming, 1024 * 1024):
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        content_hash = digest.hexdigest()
        target = root / "objects" / content_hash
        try:
            os.link(temp, target)
        except FileExistsError:
            if _hash(target) != content_hash:
                raise ProtocolError(f"corrupt existing object: {target}") from None
        _sync_dir(target.parent)
        return content_hash
    finally:
        temp.unlink(missing_ok=True)


def _select(rows: list[sqlite3.Row], policy: Policy) -> list[sqlite3.Row]:
    ordered = list(reversed(rows)) if policy.order == "newest" else rows
    return ordered[:1] if policy.batch == "one" else ordered


def archive(
    root: Path,
    processor_id: str,
    policy: Policy = Policy(),
    *,
    failpoint: Failpoint = lambda _: None,
) -> Summary:
    """Archive a delivery snapshot. Failures raise; committed handovers repair next run.

    The failpoint callback is a test seam, called at the three documented crash
    boundaries. It is not a retry or a user action hook.
    """
    Policy(policy.order, policy.batch)
    _layout(root, processor_id)
    with _lock(root, "archive"):
        db = _open(root, processor_id)
        try:
            names = sorted(path.name for path in (root / "inbox").iterdir())
            for name in names:
                _regular(root / "inbox" / name)
                row = db.execute("SELECT cleaned FROM receipts WHERE name=?", (name,)).fetchone()
                if row is not None and row[0]:
                    raise ProtocolError(f"delivery basename reused: {name}")
            _repair(root, db, processor_id)
            with _write(db):
                for name in names:
                    db.execute(
                        "INSERT OR IGNORE INTO receipts(name,state) VALUES (?,?)",
                        (name, state_after(ARCHIVE)),
                    )
            rows = db.execute(
                "SELECT * FROM receipts WHERE state='FILE_RECEIVED' ORDER BY seq"
            ).fetchall()
            selected = _select(rows, policy)
            duplicates = 0
            for row in selected:
                content_hash = _object(root, root / "inbox" / row["name"])
                failpoint("object_published")
                key = processing_key(processor_id, content_hash)
                with _write(db):
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO work VALUES (?,?,?,?,?,NULL,NULL)",
                        (
                            key,
                            content_hash,
                            row["seq"],
                            _json({"delivery_name": row["name"]}).decode(),
                            state_after(PROCESS),
                        ),
                    ).rowcount
                    db.execute(
                        "UPDATE receipts SET state=?,content_hash=?,work_key=? WHERE seq=?",
                        (
                            state_after(ARCHIVE, row["state"], "archive_committed"),
                            content_hash,
                            key,
                            row["seq"],
                        ),
                    )
                duplicates += int(not inserted)
                failpoint("archive_committed")
                _publish_todo(root, db, processor_id, key)
                failpoint("todo_published")
                archived = db.execute(
                    "SELECT * FROM receipts WHERE seq=?", (row["seq"],)
                ).fetchone()
                _cleanup_receipt(root, db, archived)
            remaining = db.execute(
                "SELECT count(*) FROM receipts WHERE state='FILE_RECEIVED'"
            ).fetchone()[0]
            return Summary(len(selected), len(selected), duplicates, remaining)
        finally:
            db.close()


def _item(root: Path, row: sqlite3.Row, processor_id: str) -> WorkItem:
    content_hash, key = row["content_hash"], row["work_key"]
    if (
        not _HEX.fullmatch(content_hash)
        or not _HEX.fullmatch(key)
        or processing_key(processor_id, content_hash) != key
    ):
        raise ProtocolError("invalid ledger object identity")
    path = _verified_object(root, content_hash)
    return WorkItem(
        path, key, content_hash, row["receipt_seq"], MappingProxyType(json.loads(row["metadata"]))
    )


def _move(db: sqlite3.Connection, key: str, event: str, result: Result | None = None) -> str:
    with _write(db):
        old = db.execute("SELECT state FROM work WHERE work_key=?", (key,)).fetchone()[0]
        new = state_after(PROCESS, old, event)
        db.execute(
            "UPDATE work SET state=?, evidence=?, detail=? WHERE work_key=?",
            (
                new,
                result.evidence if isinstance(result, Completed) else None,
                result.reason if isinstance(result, RetrySafe | Unknown) else None,
                key,
            ),
        )
        return new


def process(root: Path, processor_id: str, handler: Handler, policy: Policy = Policy()) -> Summary:
    """Read TODOs and process a bounded snapshot under the process lock.

    RUNNING/UNKNOWN reconciliation precedes READY work. A reconciliation consumes
    one selected item, even when it proves RetrySafe; execution waits for the
    next invocation. No reconciler means Unknown. Every failure stops this run.
    """
    Policy(policy.order, policy.batch)
    _layout(root, processor_id)
    with _lock(root, "process"):
        db = _open(root, processor_id)
        try:
            rows = []
            for path in sorted((root / "todo").iterdir()):
                if path.suffix != ".json" or not _HEX.fullmatch(path.stem):
                    raise ProtocolError(f"invalid TODO filename: {path}")
                with _write(db):
                    row = db.execute("SELECT * FROM work WHERE work_key=?", (path.stem,)).fetchone()
                    if row is None:
                        raise ProtocolError(f"TODO has no ledger work: {path}")
                    if row["state"] == "DONE" and not path.exists() and not path.is_symlink():
                        continue
                    _validate_todo(path, processor_id, row)
                    if row["state"] == "DONE":
                        _remove(path)
                    else:
                        rows.append(row)
            rows.sort(key=lambda row: row["receipt_seq"])
            recovery = _select([row for row in rows if row["state"] != "READY"], policy)
            fresh = _select([row for row in rows if row["state"] == "READY"], policy)
            candidates = recovery + fresh
            if policy.batch == "one":
                candidates = candidates[:1]
            selected = completed = reconciled = 0
            stopped = False
            for row in candidates:
                item = _item(root, row, processor_id)
                selected += 1
                call: Callable[[WorkItem], Result] | None
                if row["state"] == "READY":
                    _move(db, item.processing_key, "claim")
                    call = handler.process
                else:
                    reconciled += 1
                    if row["state"] == "RUNNING":
                        _move(db, item.processing_key, "uncertain", Unknown("interrupted RUNNING"))
                    call = getattr(handler, "reconcile", None)
                try:
                    result = call(item) if call is not None else Unknown("no reconciler")
                    if not isinstance(result, Completed | RetrySafe | Unknown):
                        raise TypeError("handler must return Completed, RetrySafe or Unknown")
                    value = result.evidence if isinstance(result, Completed) else result.reason
                    if not isinstance(value, str) or not value:
                        raise ValueError("handler result requires nonempty evidence/reason")
                except StatechartError:
                    raise
                except Exception as exc:
                    result = Unknown(f"{type(exc).__name__}: {exc}")
                event = (
                    "completed"
                    if isinstance(result, Completed)
                    else "retry_safe"
                    if isinstance(result, RetrySafe)
                    else "uncertain"
                )
                _move(db, item.processing_key, event, result)
                if isinstance(result, Completed):
                    completed += 1
                    with _write(db):
                        _remove(root / "todo" / f"{item.processing_key}.json")
                else:
                    stopped = True
                    break
            remaining = db.execute("SELECT count(*) FROM work WHERE state!='DONE'").fetchone()[0]
            unknown = db.execute("SELECT count(*) FROM work WHERE state='UNKNOWN'").fetchone()[0]
            return Summary(selected, completed, 0, remaining, unknown, reconciled, stopped)
        finally:
            db.close()
