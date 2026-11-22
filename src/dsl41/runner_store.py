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
- The `transitions` rows are the trace entries a record produced, committed in the record's
  own transaction and keyed by its seq and the entry's ordinal in it (DL-536). Coverage names
  the seq intervals whose rows are complete; every append extends it, and a seq no interval
  names has no claim. Nothing the engine decides reads these rows.
"""

from __future__ import annotations

import bisect
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
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


#: A trace entry as the store takes it: a canon-grammar mapping with string `job` and
#: `transition` keys, the two a read filters on (DL-536).
type TransitionEntry = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Interval:
    """Seqs `first` to `last`, both included. `covered` says the rows of every record in it are
    in the table; a gap holds records this build did not commit, so it has no rows (DL-536)."""

    first: int
    last: int
    covered: bool


@dataclass(frozen=True, slots=True)
class TransitionQuery:
    """One read of the `transitions` rows (DL-536). `cursor` is a `(seq, n)` key the rows go
    after (or before, `backward`); None starts at the oldest (or the newest). `upto` is the
    highest seq the read may see. `counts` names the transitions to count by job. With
    `byte_cap`, the read stops at the row that takes the entries' stored bytes past it."""

    cursor: tuple[int, int] | None
    backward: bool
    limit: int
    upto: int
    job: str | None = None
    transition: str | None = None
    counts: tuple[str, ...] | None = None
    byte_cap: int | None = None


@dataclass(frozen=True, slots=True)
class TransitionRead:
    """What one read transaction saw: up to `limit + 1` rows in the read's direction, the oldest
    retained seq, the coverage, the histories in the order they began, and the counts.
    `beyond` says a read the byte cap stopped left a matching row after its last one.
    `pruned_through` is the key of the newest row a prune or a trim ever removed."""

    rows: list[tuple[int, int, dict[str, Any]]]
    first_seq: int
    coverage: list[Interval]
    histories: list[tuple[str, int]]
    counts: dict[str, dict[str, int]] | None = None
    beyond: bool = False
    pruned_through: tuple[int, int] | None = None


class Store(Protocol):
    def append(self, *records: Record, transitions: Sequence[TransitionEntry] = ()) -> int:
        """Write `records` in one transaction and return the last seq. A payload canon refuses
        raises `CanonError`, and so does an empty `kind`, or a `kind`, `actor` or `request_id` that
        is not a string of Unicode scalar values (`actor` and `request_id` may be None). A reused
        `request_id` raises `StoreError`. Nothing is written then.

        `transitions` are the trace entries the first record produced, stored in the same
        transaction under its seq, and coverage extends over the records (DL-536). An entry canon
        refuses raises `CanonError`, and rows with no record raise `StoreError`."""
        ...

    def first_seq(self) -> int:
        """The seq of the oldest retained record, or `last_seq() + 1` when none is retained."""
        ...

    def transitions_coverage(self) -> list[Interval]:
        """The covered intervals and the recorded gaps, in seq order (DL-536)."""
        ...

    def read_transitions(self, query: TransitionQuery) -> TransitionRead:
        """One read of the `transitions` rows in a single read transaction (DL-536)."""
        ...

    def cover_transitions(self, intervals: Sequence[Interval]) -> None:
        """Record `intervals` in place of what coverage said about their seqs: the start's gaps
        (DL-536)."""
        ...

    def trim_transitions(self) -> None:
        """Drop the rows and coverage outside the retained records; with nothing outside them,
        write nothing (DL-536)."""
        ...

    def begin_history(self, history: str, first_seq: int | None = None) -> None:
        """Record that history `history` begins at `first_seq`, after the last seq when None
        (DL-536)."""
        ...

    def histories(self) -> list[tuple[str, int]]:
        """The histories and the seq each began at, in the order they began (DL-536)."""
        ...

    def started_seqs(self) -> list[int]:
        """The seqs of the retained `engine started` records, in seq order."""
        ...

    def read_from(self, seq: int) -> Iterator[tuple[int, Record]]:
        """The records with a seq strictly greater than `seq`, in seq order. `read_from(0)` is
        the whole journal; `read_from(snapshot.seq)` is the tail a snapshot does not cover. The
        iterator is live: records appended after it started are yielded too."""
        ...

    def last_seq(self) -> int:
        """The seq of the newest record, or 0 for an empty journal."""
        ...

    def latest_at(self) -> datetime | None:
        """The latest stamp among retained records, independent of seq order (DL-493)."""
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


@dataclass(frozen=True, slots=True)
class _Transition:
    """A trace entry after the checks that may refuse it: its two filter columns and its
    canonical bytes, which hold every value the canon grammar holds (DL-536)."""

    job: str
    transition: str
    entry: bytes


def _encode_transition(entry: TransitionEntry) -> _Transition:
    if not isinstance(entry, Mapping):
        raise CanonError(f"a transition entry is a mapping, got {type(entry).__name__}")
    for key in ("job", "transition"):
        value = entry.get(key)
        if not isinstance(value, str) or not is_scalar_string(value):
            raise CanonError(f"a transition's {key} is a string of Unicode scalar values")
    return _Transition(entry["job"], entry["transition"], _encode_payload(entry))


def _advanced(coverage: list[Interval], first: int, last: int) -> list[Interval]:
    """`coverage` once records `first` to `last` were appended with their rows: the covered
    interval that ends at `first - 1` grows, or a new one starts (DL-536)."""
    for position, interval in enumerate(coverage):
        if interval.covered and interval.last == first - 1:
            grown = Interval(interval.first, last, True)
            return [*coverage[:position], grown, *coverage[position + 1 :]]
    return [*coverage, Interval(first, last, True)]


def _trimmed(coverage: list[Interval], first: int, last: int) -> list[Interval]:
    """`coverage` cut to the seqs `first` to `last` the journal retains (DL-536)."""
    kept = []
    for interval in coverage:
        low, high = max(interval.first, first), min(interval.last, last)
        if low <= high:
            kept.append(Interval(low, high, interval.covered))
    return kept


def _overlaid(coverage: list[Interval], written: Sequence[Interval]) -> list[Interval]:
    """`coverage` with `written` in place of whatever it said about those seqs (DL-536)."""
    kept = list(coverage)
    for interval in written:
        cut: list[Interval] = []
        for old in kept:
            if old.last < interval.first or old.first > interval.last:
                cut.append(old)
                continue
            if old.first < interval.first:
                cut.append(Interval(old.first, interval.first - 1, old.covered))
            if old.last > interval.last:
                cut.append(Interval(interval.last + 1, old.last, old.covered))
        kept = cut
    return _merged([*kept, *written])


def _merged(coverage: list[Interval]) -> list[Interval]:
    """`coverage` in seq order, adjacent intervals of one kind joined (DL-536)."""
    joined: list[Interval] = []
    for interval in sorted(coverage, key=lambda item: item.first):
        if (
            joined
            and joined[-1].covered == interval.covered
            and joined[-1].last + 1 == interval.first
        ):
            joined[-1] = Interval(joined[-1].first, interval.last, interval.covered)
        else:
            joined.append(interval)
    return joined


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
        #: the `transitions` rows in `(seq, n)` order, their coverage, and the histories in the
        #: order they began (DL-536)
        self._transitions: list[tuple[int, int, _Transition]] = []
        self._coverage: list[Interval] = []
        self._histories: list[tuple[str, int]] = []
        self._pruned_through: tuple[int, int] | None = None

    def append(self, *records: Record, transitions: Sequence[TransitionEntry] = ()) -> int:
        encoded = [_encode(record) for record in records]
        rows = [_encode_transition(entry) for entry in transitions]
        if rows and not encoded:
            raise StoreError("transition rows are stored with the record that produced them")
        seen: set[str] = set()
        for row in encoded:
            if row.request_id is None:
                continue
            if row.request_id in self._requests or row.request_id in seen:
                raise StoreError(f"request_id {row.request_id!r} is already in the journal")
            seen.add(row.request_id)
        if encoded:
            first = self._next_seq
            self._transitions.extend((first, n, row) for n, row in enumerate(rows))
            self._coverage = _advanced(self._coverage, first, first + len(encoded) - 1)
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

    def latest_at(self) -> datetime | None:
        return max((_unstamp(entry.row.at) for entry in self._rows), default=None)

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
        self._trim(bound, self.last_seq())
        return deleted

    def meta(self) -> dict[str, str]:
        return dict(self._meta)

    def close(self) -> None:
        return None  # nothing to release

    # ---- transitions (DL-536)

    def first_seq(self) -> int:
        return self._rows[0].seq if self._rows else self._next_seq

    def transitions_coverage(self) -> list[Interval]:
        return list(self._coverage)

    def read_transitions(self, query: TransitionQuery) -> TransitionRead:
        rows = self._transitions
        found: list[tuple[int, int, dict[str, Any]]] = []
        if query.backward:
            end = (
                len(rows)
                if query.cursor is None
                else bisect.bisect_left(rows, query.cursor, key=_key)
            )
            positions: Iterator[int] = iter(range(end - 1, -1, -1))
        else:
            start = 0 if query.cursor is None else bisect.bisect_right(rows, query.cursor, key=_key)
            positions = iter(range(start, len(rows)))
        size, beyond = 0, False
        for position in positions:
            seq, n, row = rows[position]
            if seq > query.upto:
                if query.backward:
                    continue
                break
            if query.job is not None and row.job != query.job:
                continue
            if query.transition is not None and row.transition != query.transition:
                continue
            if len(found) > query.limit:
                break
            if query.byte_cap is not None and size > query.byte_cap:
                beyond = True  # rows are decoded only up to the cap
                break
            found.append((seq, n, dict(_decode_payload(row.entry))))
            size += len(row.entry)
        counts: dict[str, dict[str, int]] | None = None
        if query.counts is not None:
            counts = {}
            for seq, _, row in rows:
                if seq <= query.upto and row.transition in query.counts:
                    by_job = counts.setdefault(row.job, {})
                    by_job[row.transition] = by_job.get(row.transition, 0) + 1
        return TransitionRead(
            rows=found,
            first_seq=self.first_seq(),
            coverage=list(self._coverage),
            histories=self.histories(),
            counts=counts,
            beyond=beyond,
            pruned_through=self._pruned_through,
        )

    def cover_transitions(self, intervals: Sequence[Interval]) -> None:
        self._coverage = _overlaid(self._coverage, intervals)

    def trim_transitions(self) -> None:
        self._trim(self.first_seq(), self.last_seq())

    def begin_history(self, history: str, first_seq: int | None = None) -> None:
        self._histories.append((history, self.last_seq() + 1 if first_seq is None else first_seq))

    def histories(self) -> list[tuple[str, int]]:
        return sorted(self._histories, key=lambda history: history[1])  # stable: begun order

    def started_seqs(self) -> list[int]:
        return [
            entry.seq
            for entry in self._rows
            if entry.row.kind == "engine" and _record(entry.row).payload.get("event") == "started"
        ]

    def _trim(self, first: int, last: int) -> None:
        below = [_key(row) for row in self._transitions if row[0] < first]
        if below and (self._pruned_through is None or max(below) > self._pruned_through):
            self._pruned_through = max(below)
        self._transitions = [row for row in self._transitions if first <= row[0] <= last]
        self._coverage = _trimmed(self._coverage, first, last)


def _key(row: tuple[int, int, object]) -> tuple[int, int]:
    return row[0], row[1]


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
    # DL-536: created where missing, so a root an older build made gains them on its first
    # writable open here, and an older build ignores them. `entry` holds the whole entry as
    # canonical bytes; `job` and `transition` are the filter columns, one index per filter
    # combination, each ending in the key so a page reads in key order.
    """CREATE TABLE IF NOT EXISTS transitions (
        seq INTEGER NOT NULL,
        n INTEGER NOT NULL,
        job TEXT NOT NULL,
        transition TEXT NOT NULL,
        entry BLOB NOT NULL,
        PRIMARY KEY (seq, n)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS transitions_job ON transitions(job, seq, n)",
    "CREATE INDEX IF NOT EXISTS transitions_transition ON transitions(transition, seq, n)",
    "CREATE INDEX IF NOT EXISTS transitions_job_transition ON transitions(job, transition, seq, n)",
    """CREATE TABLE IF NOT EXISTS transitions_coverage (
        first_seq INTEGER PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        covered INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS transitions_history (
        history TEXT PRIMARY KEY,
        first_seq INTEGER NOT NULL
    )""",
    # the key of the newest row a prune or a trim removed: a forward cursor below it skipped
    # rows that are gone
    """CREATE TABLE IF NOT EXISTS transitions_pruned (
        one INTEGER PRIMARY KEY CHECK (one = 1),
        seq INTEGER NOT NULL,
        n INTEGER NOT NULL
    )""",
)

#: The index a page with each filter combination reads (`(job given, transition given)`), named
#: in the query so the plan cannot fall back to a scan (DL-536).
_PAGE_INDEX: Final[dict[tuple[bool, bool], str]] = {
    (True, False): " INDEXED BY transitions_job",
    (False, True): " INDEXED BY transitions_transition",
    (True, True): " INDEXED BY transitions_job_transition",
    (False, False): "",
}


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
        self._interrupted = False
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

    def append(self, *records: Record, transitions: Sequence[TransitionEntry] = ()) -> int:
        encoded = [_encode(record) for record in records]
        rows = [_encode_transition(entry) for entry in transitions]
        if not encoded:
            if rows:
                raise StoreError("transition rows are stored with the record that produced them")
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
            last = self.last_seq()
            self._record_transitions(last - len(encoded) + 1, last, rows)
            return last

    def _record_transitions(self, first: int, last: int, rows: list[_Transition]) -> None:
        """The rows of record `first` and the coverage over `first` to `last`, inside the
        append's transaction: a failure here rolls the records back too (DL-536)."""
        self._db.executemany(
            "INSERT INTO transitions(seq, n, job, transition, entry) VALUES (?, ?, ?, ?, ?)",
            [(first, n, row.job, row.transition, row.entry) for n, row in enumerate(rows)],
        )
        grown = self._db.execute(
            "UPDATE transitions_coverage SET last_seq = ? WHERE covered = 1 AND last_seq = ?",
            (last, first - 1),
        )
        if grown.rowcount == 0:
            self._db.execute(
                "INSERT INTO transitions_coverage(first_seq, last_seq, covered) VALUES (?, ?, 1)",
                (first, last),
            )

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
            deleted = cursor.rowcount
            self._trim(bound, self.last_seq())
            return deleted

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

    def latest_at(self) -> datetime | None:
        (at,) = self._db.execute("SELECT max(at_utc) FROM journal").fetchone()
        return None if at is None else _unstamp(at)

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

    def interrupt(self) -> None:
        """Stop this connection's reads, from any thread: the statement it runs now, and every
        `read_transitions` step after this, end with `sqlite3.OperationalError` (DL-536)."""
        self._interrupted = True
        self._db.interrupt()

    def _go_on(self) -> None:
        """Between a read's statements and rows: refuse to go on once interrupted."""
        if self._interrupted:
            raise sqlite3.OperationalError("interrupted")

    # ---- transitions (DL-536)

    def first_seq(self) -> int:
        (found,) = self._db.execute("SELECT MIN(seq) FROM journal").fetchone()
        return self.last_seq() + 1 if found is None else int(found)

    def transitions_coverage(self) -> list[Interval]:
        return [
            Interval(int(first), int(last), bool(covered))
            for first, last, covered in self._db.execute(
                "SELECT first_seq, last_seq, covered FROM transitions_coverage ORDER BY first_seq"
            )
        ]

    def read_transitions(self, query: TransitionQuery) -> TransitionRead:
        """One read transaction: on a reader's connection it holds no lock a writer waits on
        (WAL), and every part of the answer is read at the same point (DL-536)."""
        where: list[str] = []
        params: list[object] = []
        if query.job is not None:
            where.append("job = ?")
            params.append(query.job)
        if query.transition is not None:
            where.append("transition = ?")
            params.append(query.transition)
        # one bound per end of the key range, so the index search starts at the cursor
        if not query.backward:
            where.append("seq <= ?")
            params.append(query.upto)
            if query.cursor is not None:
                where.append("(seq, n) > (?, ?)")
                params.extend(query.cursor)
        else:
            below = (query.upto + 1, 0)
            if query.cursor is not None and query.cursor < below:
                below = query.cursor
            where.append("(seq, n) < (?, ?)")
            params.extend(below)
        order = "DESC" if query.backward else "ASC"
        page = (
            "SELECT seq, n, entry FROM transitions"
            + _PAGE_INDEX[(query.job is not None, query.transition is not None)]
            + f" WHERE {' AND '.join(where)} ORDER BY seq {order}, n {order} LIMIT ?"
        )
        self._db.execute("BEGIN")
        try:
            # an interrupt between two statements stops the read at the next one
            self._go_on()
            rows: list[tuple[int, int, dict[str, Any]]] = []
            size, beyond = 0, False
            found = self._db.execute(page, (*params, query.limit + 1))
            for seq, n, entry in found:
                self._go_on()
                if query.byte_cap is not None and size > query.byte_cap:
                    beyond = True  # rows are decoded only up to the cap
                    break
                rows.append((int(seq), int(n), dict(_decode_payload(bytes(entry)))))
                size += len(entry)
            found.close()
            counts: dict[str, dict[str, int]] | None = None
            if query.counts is not None:
                self._go_on()
                counts = {}
                marks = ", ".join("?" * len(query.counts))
                for job, transition, count in self._db.execute(
                    "SELECT job, transition, COUNT(*) FROM transitions"
                    f" INDEXED BY transitions_transition WHERE transition IN ({marks})"
                    " AND seq <= ? GROUP BY job, transition",
                    (*query.counts, query.upto),
                ):
                    counts.setdefault(str(job), {})[str(transition)] = int(count)
            self._go_on()
            first = self.first_seq()
            coverage = self.transitions_coverage()
            self._go_on()
            histories = self.histories()
            through = self._db.execute("SELECT seq, n FROM transitions_pruned").fetchone()
            return TransitionRead(
                rows=rows,
                first_seq=first,
                coverage=coverage,
                histories=histories,
                counts=counts,
                beyond=beyond,
                pruned_through=None if through is None else (int(through[0]), int(through[1])),
            )
        finally:
            self._db.execute("COMMIT")

    def cover_transitions(self, intervals: Sequence[Interval]) -> None:
        with self._transaction():
            self._set_coverage(_overlaid(self.transitions_coverage(), intervals))

    def trim_transitions(self) -> None:
        first, last = self.first_seq(), self.last_seq()
        coverage = self.transitions_coverage()
        outside = self._db.execute(
            "SELECT 1 FROM transitions WHERE seq < ? UNION ALL"
            " SELECT 1 FROM transitions WHERE seq > ? LIMIT 1",
            (first, last),
        ).fetchone()
        if outside is None and _trimmed(coverage, first, last) == coverage:
            return  # a start with nothing to trim commits nothing for it
        with self._transaction():
            self._trim(first, last)

    def begin_history(self, history: str, first_seq: int | None = None) -> None:
        with self._transaction():
            self._db.execute(
                "INSERT INTO transitions_history(history, first_seq) VALUES (?, ?)",
                (history, self.last_seq() + 1 if first_seq is None else first_seq),
            )

    def histories(self) -> list[tuple[str, int]]:
        return [
            (str(history), int(begun))
            for history, begun in self._db.execute(
                "SELECT history, first_seq FROM transitions_history ORDER BY first_seq, rowid"
            )
        ]

    def started_seqs(self) -> list[int]:
        # the event is inside the payload: only the `engine` rows are decoded
        return [
            int(seq)
            for seq, payload in self._db.execute(
                "SELECT seq, payload FROM journal WHERE kind = 'engine' ORDER BY seq"
            )
            if _decode_payload(bytes(payload)).get("event") == "started"
        ]

    def _trim(self, first: int, last: int) -> None:
        """Inside the caller's transaction: the rows and coverage outside `first` to `last`
        go, and the newest removed row below `first` is remembered."""
        newest = self._db.execute(
            "SELECT seq, n FROM transitions WHERE seq < ? ORDER BY seq DESC, n DESC LIMIT 1",
            (first,),
        ).fetchone()
        if newest is not None:
            self._db.execute(
                "INSERT INTO transitions_pruned(one, seq, n) VALUES (1, ?, ?)"
                " ON CONFLICT(one) DO UPDATE SET seq = excluded.seq, n = excluded.n"
                " WHERE (excluded.seq, excluded.n) > (transitions_pruned.seq, transitions_pruned.n)",
                newest,
            )
        # two key ranges, so each delete searches the key and scans no kept row
        self._db.execute("DELETE FROM transitions WHERE seq < ?", (first,))
        self._db.execute("DELETE FROM transitions WHERE seq > ?", (last,))
        self._set_coverage(_trimmed(self.transitions_coverage(), first, last))

    def _set_coverage(self, coverage: list[Interval]) -> None:
        self._db.execute("DELETE FROM transitions_coverage")
        self._db.executemany(
            "INSERT INTO transitions_coverage(first_seq, last_seq, covered) VALUES (?, ?, ?)",
            [(item.first, item.last, int(item.covered)) for item in coverage],
        )


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
