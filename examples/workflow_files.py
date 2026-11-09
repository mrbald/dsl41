"""Bounded POSIX file actions for one UID's private, precreated rehearsal root.

Ordinary calls re-execute. Explicit keys cover replacement attempts of one logical
run only. A namespace belongs to one engine run root and one ledger; use a fresh
namespace for a fresh engine root. Root recreation or ledger deletion invalidates
evidence. No outside writer may change selected paths during a call. Multiply
linked sources are unsupported, including unfinished link-based publications.
Archive destinations are explicit: the caller chooses a new destination each cycle.
The caller must not watch the reserved .workflow-stage directory or .workflow.lock.
There is no automatic retry, rollback, rotation, retention or reconciliation.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any
import unicodedata
import uuid


LOCK = ".workflow.lock"
STAGE = ".workflow-stage"
FILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_receipts (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    invocation_id TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    claim_key TEXT UNIQUE,
    spec TEXT NOT NULL,
    identity TEXT NOT NULL,
    state TEXT NOT NULL,
    cause TEXT NOT NULL,
    result TEXT NOT NULL
);
"""
IDENTITY = ("DSL41_JOB", "DSL41_RUN_NUMBER", "DSL41_ATTEMPT", "DSL41_SCHEDULED_AT")
Fault = Callable[[str, int], None]


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=True)


def folded(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def text_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label}: expected nonempty string without NUL")
    return value


def relative(value: Any) -> str:
    name = text_value(value, "path")
    if name.startswith("/") or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("path must be relative without empty, dot or parent components")
    if any(c in name for c in "*?[]"):
        raise ValueError("path must be concrete, without wildcard syntax")
    return name


def distinct(paths: list[str]) -> None:
    if len({folded(path) for path in paths}) != len(paths):
        raise ValueError("duplicate or case/Unicode-alias paths")


def validate_action(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("file_action must be an object")
    operation = raw.get("operation")
    fields = {
        "move": {"source", "destination"},
        "archive": {"source", "destination", "missing_ok"},
        "remove": {"paths", "missing_ok"},
        "delay": {"seconds"},
    }
    if not isinstance(operation, str) or operation not in fields:
        raise ValueError("unknown file action operation")
    if raw.keys() - (fields[operation] | {"operation", "key"}):
        raise ValueError("unknown file action field")
    action: dict[str, Any] = {"operation": operation}
    if "key" in raw:
        action["key"] = text_value(raw["key"], "file_action.key")
    if operation in ("move", "archive"):
        for field in ("source", "destination"):
            action[field] = relative(raw.get(field))
    if operation == "remove":
        paths = raw.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("remove paths must contain concrete paths")
        action["paths"] = [relative(path) for path in paths]
        distinct(action["paths"])
    if operation in ("archive", "remove"):
        if not isinstance(raw.get("missing_ok", False), bool):
            raise ValueError("missing_ok must be boolean")
        action["missing_ok"] = raw.get("missing_ok", False)
    if operation == "delay":
        seconds = raw.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 3600
        ):
            raise ValueError("delay seconds must be finite and between 0 and 3600")
        action["seconds"] = seconds
    return action


class Refusal(Exception):
    def __init__(self, cause: str, message: str):
        super().__init__(message)
        self.cause = cause


class EvidenceError(Exception):
    def __init__(self, invocation: str, cause: str, error: Exception):
        super().__init__(f"invocation {invocation}: uncommitted cause {cause}: {error}")
        self.invocation = invocation
        self.cause = cause


class FileRoot:
    def __init__(self, root: Path, ledger: Path):
        if not root.is_absolute():
            raise ValueError("--file-root must be absolute")
        if root.is_symlink():
            raise ValueError("--file-root itself must not be a symlink")
        self.root = root.resolve(strict=True)
        self.ledger = ledger.absolute().resolve()
        self.info = self.root.stat()
        if self.root == Path("/"):
            raise ValueError("filesystem root is not a rehearsal root")
        self.directory(self.root)
        self.reserved = {folded(LOCK), folded(STAGE)}
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(self.ledger) + suffix)
            if target.is_relative_to(self.root):
                self.reserved.add(folded(target.relative_to(self.root).as_posix()))

    @staticmethod
    def directory(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"directory must be owned and not group/other writable: {path}")

    def path(self, name: str) -> Path:
        name = relative(name)
        if folded(name) in self.reserved or folded(name.split("/")[0]) in {
            folded(LOCK),
            folded(STAGE),
        }:
            raise Refusal("reserved_path", "path is reserved for file-tool metadata")
        path = self.root
        for component in name.split("/"):
            path /= component
            if path.is_symlink():
                raise Refusal("unsupported_path", "symlink path components are unsupported")
            if path.exists() and path != self.root / name and not path.is_dir():
                raise Refusal("unsupported_path", "path ancestor is not a directory")
        if not path.resolve().is_relative_to(self.root):
            raise Refusal("outside_root", "path escapes file root")
        try:
            self.directory(path.parent)
        except (OSError, ValueError) as error:
            raise Refusal("unsupported_path", str(error)) from error
        if path.exists() and self.ledger.exists() and path.samefile(self.ledger):
            raise Refusal("reserved_path", "path aliases the ledger")
        return path

    def source(self, name: str, missing_ok: bool = False) -> dict[str, Any]:
        path = self.path(name)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return {"path": name, "status": "absent"}
            raise Refusal("missing_source", f"missing source: {name}") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Refusal("unsupported_source", "source must be regular and singly linked")
        if info.st_dev != self.info.st_dev:
            raise Refusal("cross_device", "source is on another filesystem")
        return {
            "path": name,
            "status": "selected",
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }

    def destination(self, name: str) -> Path:
        path = self.path(name)
        if path.parent.stat().st_dev != self.info.st_dev:
            raise Refusal("cross_device", "destination is on another filesystem")
        if path.exists():
            raise Refusal("collision", f"destination exists: {name}")
        return path

    @contextmanager
    def locked(self) -> Iterator[None]:
        path = self.root / LOCK
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError as error:
            raise Refusal("unsupported_path", f"cannot open peer lock: {error}") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise Refusal("unsupported_path", "invalid peer lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal("busy", "file root lock is busy") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def select(root: FileRoot, action: dict[str, Any]) -> list[dict[str, Any]]:
    operation = action["operation"]
    if operation == "delay":
        return []
    if operation == "publish":
        staging = root.root / STAGE
        if staging.is_symlink():
            raise Refusal("unsupported_path", "staging must not be a symlink")
        if staging.exists():
            try:
                root.directory(staging)
            except (OSError, ValueError) as error:
                raise Refusal("unsupported_path", str(error)) from error
            if staging.stat().st_dev != root.info.st_dev:
                raise Refusal("cross_device", "staging is on another filesystem")
        for item in action["items"]:
            root.destination(item["target"])
        return [{"path": item["target"], "status": "selected"} for item in action["items"]]
    names = action["paths"] if operation == "remove" else [action["source"]]
    selected = [root.source(name, action.get("missing_ok", False)) for name in names]
    if operation in ("move", "archive"):
        if folded(action["source"]) == folded(action["destination"]):
            raise Refusal("same_file", "source and destination alias")
        destination = root.path(action["destination"])
        source = root.path(action["source"])
        if destination.exists() and source.exists() and destination.samefile(source):
            raise Refusal("same_file", "source and destination alias")
        root.destination(action["destination"])
    return selected


def mutate(
    root: FileRoot,
    action: dict[str, Any],
    selected: list[dict[str, Any]],
    result: dict[str, Any],
    progress: Callable[[], None],
    fault: Fault,
) -> None:
    operation = action["operation"]
    for index, member in enumerate(selected):
        name = member["path"]
        result["uncertain"] = {"path": name, "index": index}
        progress()
        fault("before_mutation", index)
        if member["status"] == "absent":
            status = "absent"
        elif operation in ("move", "archive"):
            source = root.path(name)
            root.source(name)
            destination = root.destination(action["destination"])
            sync_file(source)
            os.rename(source, destination)
            fault("after_mutation", index)
            sync_directory(source.parent)
            sync_directory(destination.parent)
            status = "moved"
        elif operation == "remove":
            source = root.path(name)
            root.source(name)
            source.unlink()
            fault("after_mutation", index)
            sync_directory(source.parent)
            status = "removed"
        else:
            item = action["items"][index]
            staging = root.root / STAGE
            if not staging.exists():
                staging.mkdir(mode=0o700)
                sync_directory(root.root)
            if staging.is_symlink():
                raise Refusal("unsupported_path", "staging must not be a symlink")
            root.directory(staging)
            staged = staging / f".{uuid.uuid4().hex}"
            with staged.open("xb") as stream:
                stream.write(item["content"].encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(staging)
            fault("staged", index)
            destination = root.destination(item["target"])
            if staged.stat().st_dev != destination.parent.stat().st_dev:
                raise OSError(errno.EXDEV, "staging and target are on different filesystems")
            os.rename(staged, destination)
            fault("after_mutation", index)
            sync_directory(staging)
            sync_directory(destination.parent)
            status = "published"
        result["members"].append({**member, "status": status})
        result["uncertain"] = None
        progress()
        fault("after_item", index)


def no_fault(phase: str, index: int) -> None:
    pass


def initialize_wal(connection: sqlite3.Connection) -> None:
    """Repeat only BUSY WAL conversion, within one five-second pre-effect budget."""
    deadline = time.monotonic() + 5
    last_error = sqlite3.OperationalError("database is locked")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_error
        timeout_ms = min(5000, max(0, int(remaining * 1000)))
        connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                raise
            last_error = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))
    connection.execute("PRAGMA busy_timeout=5000")


def connect(ledger: Path, schema: str) -> sqlite3.Connection:
    connection = sqlite3.connect(ledger, timeout=5)
    try:
        initialize_wal(connection)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(schema + FILE_SCHEMA)
        return connection
    except Exception:
        connection.close()
        raise


def execute(
    root: FileRoot,
    action: dict[str, Any],
    *,
    schema: str,
    job: str,
    scenario: str = "default",
    batch: str = "default",
    namespace: str | None = None,
    logical_run: str | None = None,
    outcome: str = "success",
    identity: dict[str, str] | None = None,
    fault: Fault = no_fault,
) -> dict[str, Any]:
    """Run one validated action; fault is an in-process test seam, never config."""
    identity = dict(identity or {})
    run = identity.get("DSL41_RUN_NUMBER", logical_run)
    if logical_run is not None and "DSL41_RUN_NUMBER" in identity and logical_run != run:
        raise ValueError("logical run disagrees with DSL41_RUN_NUMBER")
    key = action.get("key")
    if key is not None and (namespace is None or run is None):
        raise ValueError("keyed actions require namespace and logical run")
    scope = encoded(
        {
            "root": str(root.root),
            "device": root.info.st_dev,
            "inode": root.info.st_ino,
            "namespace": namespace,
            "scenario": scenario,
            "batch": batch,
            "job": job,
            "run": run,
        }
    )
    operational = {k: v for k, v in action.items() if k != "key"}
    spec = encoded(operational)
    claim = None if key is None else hashlib.sha256(encoded([scope, key]).encode()).hexdigest()
    invocation = uuid.uuid4().hex
    receipt: dict[str, Any] = {
        "version": 1,
        "invocation_id": invocation,
        "job": job,
        "scenario": scenario,
        "batch": batch,
        "outcome": "pending",
        "cause": "pending",
        "action_id": None,
        "attempt_id": None,
        "reused_from": None,
        "prior_uncertain": [],
        "result": {"members": [], "uncertain": None},
    }
    connection: sqlite3.Connection | None = None

    def save(state: str, cause: str, *, pending_claim: str | None = None) -> None:
        nonlocal connection
        try:
            if connection is None:
                connection = connect(root.ledger, schema)
            connection.execute("BEGIN IMMEDIATE")
            if receipt["attempt_id"] is None:
                cursor = connection.execute(
                    "INSERT INTO attempts (recorded_at,scenario,batch,job,identity,outcome,"
                    "effect_id,effect_created) VALUES (?,?,?,?,?,?,NULL,0)",
                    (
                        datetime.now(UTC).isoformat(),
                        scenario,
                        batch,
                        job,
                        encoded({**identity, "invocation_id": invocation}),
                        receipt["outcome"],
                    ),
                )
                receipt["attempt_id"] = cursor.lastrowid
                cursor = connection.execute(
                    "INSERT INTO file_receipts (attempt_id,invocation_id,recorded_at,scope,"
                    "claim_key,spec,identity,state,cause,result) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt["attempt_id"],
                        invocation,
                        datetime.now(UTC).isoformat(),
                        scope,
                        pending_claim,
                        spec,
                        encoded(identity),
                        state,
                        cause,
                        encoded(receipt),
                    ),
                )
                receipt["action_id"] = cursor.lastrowid
            connection.execute(
                "UPDATE attempts SET outcome=? WHERE id=?",
                (receipt["outcome"], receipt["attempt_id"]),
            )
            connection.execute(
                "UPDATE file_receipts SET state=?,cause=?,result=? WHERE id=?",
                (state, cause, encoded(receipt), receipt["action_id"]),
            )
            connection.commit()
        except Exception as error:
            if connection is not None:
                connection.rollback()
            raise EvidenceError(invocation, cause, error) from error

    def refuse(error: Refusal) -> dict[str, Any]:
        receipt.update(outcome="refused", cause=error.cause, error=str(error))
        save("refused", error.cause)
        return receipt

    try:
        try:
            with root.locked():
                try:
                    connection = connect(root.ledger, schema)
                except Exception as error:
                    raise EvidenceError(invocation, "pending", error) from error
                receipt["prior_uncertain"] = [
                    row[0]
                    for row in connection.execute(
                        "SELECT invocation_id FROM file_receipts WHERE scope=? AND state='pending'",
                        (scope,),
                    )
                ]
                previous = (
                    None
                    if claim is None
                    else connection.execute(
                        "SELECT invocation_id,spec,state,result FROM file_receipts WHERE claim_key=?",
                        (claim,),
                    ).fetchone()
                )
                if previous is not None:
                    if previous[1] != spec:
                        return refuse(
                            Refusal("changed_spec", "key already records a different action")
                        )
                    if previous[2] != "completed":
                        return refuse(
                            Refusal("uncertain", "key has a pending action; replay refused")
                        )
                    original = json.loads(previous[3])
                    receipt["result"] = original["result"]
                    receipt["reused_from"] = {
                        "invocation_id": previous[0],
                        "job": job,
                        "run": run,
                        "attempt": json.loads(
                            connection.execute(
                                "SELECT identity FROM file_receipts WHERE claim_key=?", (claim,)
                            ).fetchone()[0]
                        ).get("DSL41_ATTEMPT"),
                        "attempt_id": original["attempt_id"],
                    }
                    receipt.update(outcome=outcome, cause="reused")
                    save("reused", "reused")
                    return receipt
                if outcome == "fail_before_effect":
                    receipt.update(outcome=outcome, cause=outcome)
                    save("failed", outcome)
                    return receipt
                try:
                    selected = select(root, action)
                except (Refusal, OSError) as error:
                    return refuse(
                        error if isinstance(error, Refusal) else Refusal("precondition", str(error))
                    )
                receipt["selected"] = selected
                receipt["started_at"] = datetime.now(UTC).isoformat()
                save("pending", "pending", pending_claim=claim)
                if action["operation"] != "delay":
                    try:
                        mutate(
                            root,
                            action,
                            selected,
                            receipt["result"],
                            lambda: save("pending", "pending"),
                            fault,
                        )
                    except EvidenceError:
                        raise
                    except Exception as error:
                        receipt.update(outcome="failure", cause="partial_io", error=str(error))
                        save("pending", "partial_io")
                        return receipt
                    receipt.update(
                        outcome=outcome,
                        cause="completed",
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                    save("completed", "completed")
                    return receipt
        except Refusal as error:
            return refuse(error)
        deadline = time.monotonic() + action["seconds"]
        fault("delay_started", 0)
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(remaining)
        try:
            with root.locked():
                receipt.update(
                    outcome=outcome, cause="completed", completed_at=datetime.now(UTC).isoformat()
                )
                save("completed", "completed")
        except Refusal as error:
            receipt.update(outcome="refused", cause=error.cause, error=str(error))
            save("pending", error.cause)
        return receipt
    finally:
        if connection is not None:
            connection.close()


def exit_code(receipt: dict[str, Any]) -> int:
    if receipt["outcome"] == "success":
        return 0
    return 2 if receipt["outcome"] == "refused" else 1
