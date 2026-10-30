"""The runner's durable store: one journal, the configs, the snapshots, and the meta rows.

Two implementations of one `Store` protocol. `SqliteStore` is the `run` store: one connection,
WAL, `synchronous=FULL`, one `BEGIN IMMEDIATE` per `append`. `MemoryStore` is the `rehearse` and
test store. Both keep a record's payload as canonical bytes (`canon.canonical_bytes`), so a digest
over the journal is stable, and both refuse the same inputs the same way.

The module imports `canon` and nothing from the engine modules.

The engine is the only writer and the `leader.lock` flock guarantees it, so `SqliteStore` does not
retry on `SQLITE_BUSY`: the error propagates. Readers open their own read-only connection
(`SqliteStore(path, read_only=True)`); WAL lets them read during a write, and a reader waits up to
five seconds for a lock the writer holds.

Rules both stores keep:

- A seq is never reused: `journal.seq` is `AUTOINCREMENT`, and `put_snapshot` refuses a seq above
  `last_seq()`.
- `request_id` is unique in the journal. `prune_before` deletes rows, and a deleted row's
  `request_id` is free again, so the retry window is the retention window.
- Configs are an append-only history. Every `put_config` is a new rev, and the same digest may
  stand under several revs; `find_config` returns the latest.
- `read_from` is live: an open iterator sees records appended after it started.
"""

from __future__ import annotations

import bisect
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from dsl41.canon import (
    CanonError,
    canonical_bytes,
    check_artifact_version,
    decode,
    is_scalar_string,
    naive_utc,
)

#: The layout of the tables below; a file with another `schema_version` is refused.
SCHEMA_VERSION: Final[int] = 1

#: Rows `read_from` fetches per query, so a read never holds one cursor open across a write.
_CHUNK: Final[int] = 500

#: How long a reader waits for a lock. The writer waits for none.
_READER_WAIT_S: Final[float] = 5.0


class StoreError(Exception):
    """A constraint the store holds was violated, or the file is not a store this binary reads."""


@dataclass(frozen=True, slots=True)
class Record:
    """One journal record. `payload` is a canon-grammar mapping; the store keeps its canonical
    bytes and reads it back decoded."""

    at_utc: datetime
    kind: str
    actor: str | None
    request_id: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The oracle state at journal position `seq`, tagged with the binary's `state_version`."""

    seq: int
    state_version: int
    data: bytes


class Store(Protocol):
    def append(self, *records: Record) -> int:
        """Write `records` in one transaction and return the last seq. A payload canon refuses
        raises `CanonError`, and so does an empty `kind`, or a `kind`, `actor` or `request_id` that
        is not a string of Unicode scalar values (`actor` and `request_id` may be None). A reused
        `request_id` raises `StoreError`. Nothing is written then."""
        ...

    def read_from(self, seq: int) -> Iterator[tuple[int, Record]]:
        """The records with a seq strictly greater than `seq`, in seq order. `read_from(0)` is
        the whole journal; `read_from(snapshot.seq)` is the tail a snapshot does not cover. The
        iterator is live: records appended after it started are yielded too."""
        ...

    def last_seq(self) -> int:
        """The seq of the newest record, or 0 for an empty journal."""
        ...

    def find_request(self, request_id: str) -> Record | None:
        """The first record written under `request_id`, or None."""
        ...

    def put_config(self, data: bytes, digest: str) -> int:
        """Store a config as a new rev and return it. The history is append-only: the same digest
        may stand under several revs."""
        ...

    def find_config(self, digest: str) -> int | None:
        """The latest rev holding `digest`, or None."""
        ...

    def get_config(self, rev: int) -> bytes:
        """The config bytes of `rev`; `KeyError` for a rev never stored."""
        ...

    def current_config_rev(self) -> int:
        """The newest rev, or 0 when no config was stored."""
        ...

    def put_snapshot(self, seq: int, state_version: int, data: bytes) -> None:
        """Store a snapshot taken at `seq`. A seq already snapshotted, or above `last_seq()`,
        raises `StoreError`."""
        ...

    def latest_snapshot(self) -> Snapshot | None:
        """The snapshot with the highest seq, or None."""
        ...

    def prune_before(self, seq: int, older_than: datetime) -> int:
        """Delete the journal rows below one seq bound, and every snapshot but the latest two.
        The bound is the least of `seq`, the latest snapshot's seq, and the first seq whose row
        is not older than `older_than`; the delete reads seqs only, so the rows it keeps are one
        unbroken range (spec-wiring 1b). Configs are kept. Returns the number of journal rows
        deleted."""
        ...

    def meta(self) -> dict[str, str]:
        """The meta rows: `schema_version`, `engine_version`, `estate_name`, `created_at`, and
        the identity rows the creator named."""
        ...

    def close(self) -> None: ...


# ------------------------------------------------------------------ shared


def _encode_payload(payload: Mapping[str, Any]) -> bytes:
    """The canonical bytes of `payload`, or `CanonError`.

    A top-level `artifact_format_version` is checked here, because `canon.decode` checks it at
    read and a payload that could not be read back must not be stored.
    """
    if not isinstance(payload, Mapping):
        raise CanonError(f"a record payload is a mapping, got {type(payload).__name__}")
    check_artifact_version(payload)
    data = canonical_bytes(payload)
    _decode_payload(data)  # a Mapping that yields a key twice encodes to bytes decode refuses
    return data


def _decode_payload(data: bytes) -> Mapping[str, Any]:
    payload = decode(data)
    if not isinstance(payload, dict):  # written by _encode_payload; a foreign file may differ
        raise StoreError(f"a stored payload is not an object: {type(payload).__name__}")
    return payload


def _stamp(at: datetime) -> str:
    """A naive-UTC instant as the fixed-width text the tables hold; text order is time order."""
    return naive_utc(at).isoformat(timespec="microseconds")


def _unstamp(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True, slots=True)
class _Encoded:
    """A record after the checks that may refuse it."""

    at: str
    kind: str
    actor: str | None
    request_id: str | None
    payload: bytes


def _text(value: object, what: str, *, optional: bool) -> None:
    """Refuse a header field that is not a string of Unicode scalar values, as the same input is
    refused by both stores."""
    if value is None and optional:
        return
    if not isinstance(value, str) or not is_scalar_string(value):
        raise CanonError(f"a record's {what} is a string of Unicode scalar values, got {value!r}")


def _encode(record: Record) -> _Encoded:
    _text(record.kind, "kind", optional=False)
    if record.kind == "":
        raise CanonError("a record's kind is not empty")
    _text(record.actor, "actor", optional=True)
    _text(record.request_id, "request_id", optional=True)
    return _Encoded(
        _stamp(record.at_utc),
        record.kind,
        record.actor,
        record.request_id,
        _encode_payload(record.payload),
    )


def _record(row: _Encoded) -> Record:
    return Record(
        _unstamp(row.at), row.kind, row.actor, row.request_id, _decode_payload(row.payload)
    )


# ------------------------------------------------------------------ memory


@dataclass(slots=True)
class _MemRow:
    seq: int
    row: _Encoded


class MemoryStore:
    """The in-process store: `rehearse` and the tests. It holds the same rows and refuses the same
    inputs as `SqliteStore`. `close` does nothing: the rows live as long as the object, and the
    store stays usable after it."""

    def __init__(self, *, identity: Mapping[str, str] | None = None) -> None:
        self._meta = {"schema_version": str(SCHEMA_VERSION), **dict(identity or {})}
        self._rows: list[_MemRow] = []
        self._next_seq = 1
        self._requests: dict[str, _MemRow] = {}
        self._configs: list[tuple[bytes, str]] = []  # rev is the index plus one
        self._snapshots: dict[int, Snapshot] = {}

    def append(self, *records: Record) -> int:
        encoded = [_encode(record) for record in records]
        seen: set[str] = set()
        for row in encoded:
            if row.request_id is None:
                continue
            if row.request_id in self._requests or row.request_id in seen:
                raise StoreError(f"request_id {row.request_id!r} is already in the journal")
            seen.add(row.request_id)
        for row in encoded:
            entry = _MemRow(self._next_seq, row)
            self._next_seq += 1
            self._rows.append(entry)
            if row.request_id is not None:
                self._requests[row.request_id] = entry
        return self.last_seq()

    def read_from(self, seq: int) -> Iterator[tuple[int, Record]]:
        after = seq
        while True:  # a position per step, so an append (or a prune) during the read is followed
            at = bisect.bisect_right(self._rows, after, key=lambda entry: entry.seq)
            if at >= len(self._rows):
                return
            entry = self._rows[at]
            after = entry.seq
            yield entry.seq, _record(entry.row)

    def last_seq(self) -> int:
        return self._next_seq - 1

    def find_request(self, request_id: str) -> Record | None:
        entry = self._requests.get(request_id)
        return None if entry is None else _record(entry.row)

    def put_config(self, data: bytes, digest: str) -> int:
        self._configs.append((bytes(data), digest))
        return len(self._configs)

    def find_config(self, digest: str) -> int | None:
        for rev in range(len(self._configs), 0, -1):
            if self._configs[rev - 1][1] == digest:
                return rev
        return None

    def get_config(self, rev: int) -> bytes:
        if not 1 <= rev <= len(self._configs):
            raise KeyError(rev)
        return self._configs[rev - 1][0]

    def current_config_rev(self) -> int:
        return len(self._configs)

    def put_snapshot(self, seq: int, state_version: int, data: bytes) -> None:
        if seq in self._snapshots:
            raise StoreError(f"a snapshot at seq {seq} is already stored")
        if seq > self.last_seq():
            raise StoreError(
                f"snapshot seq {seq} is above the journal's last seq {self.last_seq()}"
            )
        self._snapshots[seq] = Snapshot(seq, state_version, bytes(data))

    def latest_snapshot(self) -> Snapshot | None:
        return self._snapshots[max(self._snapshots)] if self._snapshots else None

    def prune_before(self, seq: int, older_than: datetime) -> int:
        for old in sorted(self._snapshots)[:-2]:
            del self._snapshots[old]
        latest = self.latest_snapshot()
        if latest is None:
            return 0
        cutoff = _stamp(older_than)
        recent = next((entry.seq for entry in self._rows if entry.row.at >= cutoff), None)
        bound = min(seq, latest.seq, recent if recent is not None else seq)
        kept: list[_MemRow] = []
        for entry in self._rows:
            if entry.seq < bound:
                if entry.row.request_id is not None:
                    del self._requests[entry.row.request_id]
            else:
                kept.append(entry)
        deleted = len(self._rows) - len(kept)
        self._rows = kept
        return deleted

    def meta(self) -> dict[str, str]:
        return dict(self._meta)

    def close(self) -> None:
        return None  # nothing to release


# ------------------------------------------------------------------ sqlite

_SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        at_utc TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT,
        request_id TEXT UNIQUE,
        payload BLOB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS configs (
        rev INTEGER PRIMARY KEY,
        at_utc TEXT,
        digest TEXT NOT NULL,
        data BLOB NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS configs_digest ON configs(digest)",
    """CREATE TABLE IF NOT EXISTS snapshots (
        seq INTEGER PRIMARY KEY,
        at_utc TEXT,
        state_version INTEGER NOT NULL,
        data BLOB NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


class SqliteStore:
    """The `run` store. One connection; `append` is one `BEGIN IMMEDIATE` transaction and returns
    after the commit is durable (`synchronous=FULL`).

    `at_utc` on a config or snapshot row is the time of the newest journal row when it was
    written, so no wall clock enters the file apart from `meta.created_at`; it is NULL when the
    journal was empty.

    `read_only=True` opens an existing store for a reader: no write is accepted and nothing is
    created, and a lock is waited for up to five seconds. The writer waits for nothing: a busy
    database raises `sqlite3.OperationalError`, and nothing here retries.

    A non-empty file that does not hold this store's `meta.schema_version` row is a foreign file:
    opening it for writing raises `StoreError` before anything in it is changed.

    `identity` names further meta rows written when the file is created and never changed after
    (the engine's `state_version` among them), so a reader of an empty journal knows them.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
        engine_version: str = "",
        estate_name: str = "",
        created_at: datetime | None = None,
        identity: Mapping[str, str] | None = None,
    ) -> None:
        self._path = Path(path)
        if read_only:
            uri = self._path.resolve().as_uri() + "?mode=ro"
            self._db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=_READER_WAIT_S)
            self._db.execute("PRAGMA query_only=ON")
        else:
            self._db = sqlite3.connect(self._path, isolation_level=None, timeout=0)
            self._db.execute("PRAGMA busy_timeout=0")
        try:
            if read_only:
                self._check_schema()
            else:
                self._refuse_foreign_file()
                self._set_journal_mode()
                self._db.execute("PRAGMA synchronous=FULL")
                if sys.platform == "darwin":  # fsync alone does not reach the platters there
                    self._db.execute("PRAGMA fullfsync=ON")
                self._create(engine_version, estate_name, created_at, identity or {})
        except BaseException:
            self._db.close()
            raise

    # ---- opening

    def _refuse_foreign_file(self) -> None:
        """Raise `StoreError` for a non-empty database that is not one of ours, before a pragma or
        a table changes it."""
        try:
            tables = {
                str(row[0])
                for row in self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if tables and ("meta" not in tables or self._meta_value("schema_version") is None):
                raise StoreError(f"{self._path} is not a dsl41 store: it has no schema_version")
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"{self._path} is not a dsl41 store: {exc}") from exc

    def _set_journal_mode(self) -> None:
        mode = str(self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        # an in-memory database cannot be WAL and answers "memory"
        if mode != "wal" and not (mode == "memory" and str(self._path) == ":memory:"):
            raise StoreError(f"{self._path}: journal_mode is {mode!r}, the store needs WAL")

    def _create(
        self,
        engine_version: str,
        estate_name: str,
        created_at: datetime | None,
        identity: Mapping[str, str],
    ) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                self._db.execute(statement)
            if self._meta_value("schema_version") is None:
                made = created_at if created_at is not None else datetime.now(UTC)
                self._db.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(SCHEMA_VERSION)),
                        ("engine_version", engine_version),
                        ("estate_name", estate_name),
                        ("created_at", _stamp(made)),
                        *sorted(identity.items()),
                    ],
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._check_schema()

    def _check_schema(self) -> None:
        try:
            found = self._meta_value("schema_version")
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"{self._path} is not a dsl41 store: {exc}") from exc
        if found != str(SCHEMA_VERSION):
            raise StoreError(
                f"{self._path}: schema_version {found!r}, this binary reads {SCHEMA_VERSION}"
            )

    def _meta_value(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def meta(self) -> dict[str, str]:
        """The meta rows: `schema_version`, `engine_version`, `estate_name`, `created_at`, and
        the identity rows named at creation."""
        return {str(k): str(v) for k, v in self._db.execute("SELECT key, value FROM meta")}

    # ---- writes

    def _transaction(self) -> _Transaction:
        return _Transaction(self._db)

    def _journal_time(self) -> str | None:
        row = self._db.execute("SELECT at_utc FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return None if row is None else str(row[0])

    def append(self, *records: Record) -> int:
        encoded = [_encode(record) for record in records]
        if not encoded:
            return self.last_seq()
        with self._transaction():
            try:
                for row in encoded:
                    self._db.execute(
                        "INSERT INTO journal(at_utc, kind, actor, request_id, payload)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (row.at, row.kind, row.actor, row.request_id, row.payload),
                    )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"journal: {exc}") from exc
            return self.last_seq()

    def put_config(self, data: bytes, digest: str) -> int:
        with self._transaction():
            try:
                cursor = self._db.execute(
                    "INSERT INTO configs(at_utc, digest, data) VALUES (?, ?, ?)",
                    (self._journal_time(), digest, bytes(data)),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"configs: {exc}") from exc
            rev = cursor.lastrowid
            assert rev is not None
            return rev

    def find_config(self, digest: str) -> int | None:
        found = self._db.execute(
            "SELECT MAX(rev) FROM configs WHERE digest = ?", (digest,)
        ).fetchone()[0]
        return None if found is None else int(found)

    def put_snapshot(self, seq: int, state_version: int, data: bytes) -> None:
        with self._transaction():
            if seq > self.last_seq():
                raise StoreError(
                    f"snapshot seq {seq} is above the journal's last seq {self.last_seq()}"
                )
            try:
                self._db.execute(
                    "INSERT INTO snapshots(seq, at_utc, state_version, data) VALUES (?, ?, ?, ?)",
                    (seq, self._journal_time(), state_version, bytes(data)),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"snapshots: {exc}") from exc

    def prune_before(self, seq: int, older_than: datetime) -> int:
        with self._transaction():
            self._db.execute(
                "DELETE FROM snapshots WHERE seq NOT IN"
                " (SELECT seq FROM snapshots ORDER BY seq DESC LIMIT 2)"
            )
            latest = self._db.execute("SELECT MAX(seq) FROM snapshots").fetchone()[0]
            if latest is None:
                return 0
            # the first row inside the window, in seq order: the rows already pruned are gone,
            # so the scan stops near the head
            recent = self._db.execute(
                "SELECT seq FROM journal WHERE at_utc >= ? ORDER BY seq LIMIT 1",
                (_stamp(older_than),),
            ).fetchone()
            bound = min(seq, latest, recent[0] if recent is not None else seq)
            cursor = self._db.execute("DELETE FROM journal WHERE seq < ?", (bound,))
            return cursor.rowcount

    # ---- reads

    def read_from(self, seq: int) -> Iterator[tuple[int, Record]]:
        after = seq
        while True:
            rows = self._db.execute(
                "SELECT seq, at_utc, kind, actor, request_id, payload FROM journal"
                " WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, _CHUNK),
            ).fetchall()
            if not rows:  # a short chunk is not the end: an append may have come since the query
                return
            for found, at, kind, actor, request_id, payload in rows:
                yield found, Record(_unstamp(at), kind, actor, request_id, _decode_payload(payload))
            after = rows[-1][0]

    def last_seq(self) -> int:
        # the AUTOINCREMENT high-water mark: the newest seq ever assigned, which a prune cannot lower
        row = self._db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'journal'").fetchone()
        return 0 if row is None else int(row[0])

    def find_request(self, request_id: str) -> Record | None:
        row = self._db.execute(
            "SELECT at_utc, kind, actor, request_id, payload FROM journal WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        at, kind, actor, found, payload = row
        return Record(_unstamp(at), kind, actor, found, _decode_payload(payload))

    def get_config(self, rev: int) -> bytes:
        row = self._db.execute("SELECT data FROM configs WHERE rev = ?", (rev,)).fetchone()
        if row is None:
            raise KeyError(rev)
        return bytes(row[0])

    def current_config_rev(self) -> int:
        found = self._db.execute("SELECT MAX(rev) FROM configs").fetchone()[0]
        return 0 if found is None else int(found)

    def latest_snapshot(self) -> Snapshot | None:
        row = self._db.execute(
            "SELECT seq, state_version, data FROM snapshots ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return None if row is None else Snapshot(int(row[0]), int(row[1]), bytes(row[2]))

    def close(self) -> None:
        self._db.close()


class _Transaction:
    """`BEGIN IMMEDIATE` ... `COMMIT`, or `ROLLBACK` when the block raises."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._db.execute("COMMIT")
        else:
            self._db.execute("ROLLBACK")
