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
- `request_id` is unique in the journal. `prune_step` deletes rows, and a deleted row's
  `request_id` is free again, so the retry window is the retention window.
- Configs are an append-only history. Every `put_config` is a new rev, and the same digest may
  stand under several revs; `find_config` returns the latest.
- `read_from` is live: an open iterator sees records appended after it started.
- The `transitions` rows are the trace entries a record produced, committed in the record's
  own transaction and keyed by its seq and the entry's ordinal in it (DL-536). Coverage names
  the seq intervals whose rows are complete; every append extends it, and a seq no interval
  names has no claim. Nothing the engine decides reads these rows.
- The history index (DL-539) is two tables committed in the same transaction as the records
  they name: `job_runs`, keyed as the `transitions` row of each status entry that begins a
  run, and `job_records`, keyed by the seq of each record that belongs to a job's run.
  `history_coverage` says which seqs they cover, under the rules of `transitions_coverage`.
  Neither table has a unique key beyond its own, so an index row never fails a commit the
  records alone would pass. Nothing the engine decides reads them; `read_history` finds one
  page of a job's runs and their records through them, in one read transaction (DL-540).
"""

from __future__ import annotations

import bisect
import dataclasses
import sqlite3
import sys
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class Pruned:
    """What one prune step deleted: `deleted` journal rows, the highest decision index among
    them in `decision`, None when it deleted no `decision` record (DL-543)."""

    deleted: int
    decision: int | None


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
class RunOpening:
    """A `job_runs` row of one append (DL-539): entry `n` of its transitions begins run
    `run_number` of `job`, under configuration `rev`."""

    n: int
    job: str
    run_number: int
    rev: int


@dataclass(frozen=True, slots=True)
class RecordOwner:
    """A `job_records` row of one append (DL-539): the record at `offset` from the append's
    first belongs to `job`'s run `run_number`, or to no named run when it is None."""

    offset: int
    job: str
    run_number: int | None


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


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """One read of a job's past runs (DL-540): the openings of `job` below run `cursor` (None
    for the newest), at most `limit` of them, with the evidence of each, newest first, until
    `budget` rows of evidence (trace entries and records) are taken. `upto` is the highest seq
    the read may see."""

    job: str
    cursor: int | None
    limit: int
    upto: int
    budget: int


@dataclass(frozen=True, slots=True)
class HistoryRun:
    """One `job_runs` row on a read page (DL-540): entry `n` of seq `seq` opened `run_number`
    under `rev`. `until` is the key of the job's next opening and `until_rev` its rev, both
    None when the watermark ends the run's range."""

    seq: int
    n: int
    run_number: int
    rev: int
    until: tuple[int, int] | None
    until_rev: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryRead:
    """What one history read transaction saw (DL-540). `boundary` is the first seq of the
    covered interval of the index that reaches the watermark, None when none does; full rows
    come only from at or above it, and `boundary_at` is that record's stamp. `first_full` is the
    run number of the job's oldest opening there. `duplicate` is a run number the page showed
    twice; the read then gathers nothing. `runs` are the openings whose evidence fits the
    budget, newest first; `incomplete` is one too large for the budget alone; `more` says
    openings remain below the last of them. `entries` are the job's trace entries and the
    CONFIG marks over the runs' ranges, in key order, each range through the entry of the
    opening above it. `owned` are the records the index names for the job and a run of `runs`;
    `loose` those it names for the job and no run, from above the oldest opening's seq to one
    seq past the seq of the opening above the page, or to the watermark. `configs` holds the
    stored
    configuration of each rev of `runs` and of the opening above them, None for a rev the store
    holds no row for."""

    histories: list[tuple[str, int]]
    first_seq: int
    boundary: int | None = None
    boundary_at: datetime | None = None
    first_full: int | None = None
    duplicate: int | None = None
    runs: list[HistoryRun] = field(default_factory=list)
    incomplete: HistoryRun | None = None
    more: bool = False
    entries: list[tuple[int, int, dict[str, Any]]] = field(default_factory=list)
    owned: list[tuple[int, Record]] = field(default_factory=list)
    loose: list[tuple[int, Record]] = field(default_factory=list)
    configs: dict[int, bytes | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawBatch:
    """One archive batch of the live rows as the tables hold them, read in one transaction
    (DL-545). `records` are journal rows `(seq, at_utc, kind, actor, request_id, payload)`,
    oldest first; `transitions`, `job_runs` and `job_records` are their derived rows, each read
    only over the seqs its own coverage names as covered. `configs` are every configuration's
    `(rev, digest)`, and `config_data` the `(rev, digest, data)` of those the read was not
    told it knows. `first_seq` is the journal's oldest retained seq and `last_seq` its newest assigned
    one; `histories` the history list `(history, first_seq)` in the live order (DL-536)."""

    first_seq: int
    records: list[tuple[int, str, str, str | None, str | None, bytes]]
    transitions: list[tuple[int, int, str, str, bytes]]
    job_runs: list[tuple[int, int, str, int, int]]
    job_records: list[tuple[int, str, int | None]]
    transitions_coverage: list[Interval]
    history_coverage: list[Interval]
    configs: list[tuple[int, str]]
    config_data: list[tuple[int, str, bytes]] = field(default_factory=list)
    histories: list[tuple[str, int]] = field(default_factory=list)
    last_seq: int = 0


@dataclass(frozen=True, slots=True)
class RunEvidence:
    """What `dsl41 runs` reads of a root's history index and stored transitions, in the one
    read transaction its reads of the store share (DL-555). `first_seq` and `last_seq` are the retained journal
    range; both coverages are cut to it. `openings` are the `job_runs` rows `(seq, n, job,
    run_number, rev)` and `transitions` the `transitions` rows `(seq, n, entry)`, each only
    where its own coverage names the seq as covered, in key order. A table this file does not
    have gives no rows and no coverage."""

    first_seq: int
    last_seq: int
    history_coverage: list[Interval]
    transitions_coverage: list[Interval]
    openings: list[tuple[int, int, str, int, int]]
    transitions: list[tuple[int, int, dict[str, Any]]]


def covered_parts(coverage: Sequence[Interval], first: int, last: int) -> list[tuple[int, int]]:
    """The seqs from `first` to `last` that `coverage` names as covered, as intervals (DL-545)."""
    parts = []
    for interval in coverage:
        low, high = max(interval.first, first), min(interval.last, last)
        if interval.covered and low <= high:
            parts.append((low, high))
    return parts


class Store(Protocol):
    def append(
        self,
        *records: Record,
        transitions: Sequence[TransitionEntry] = (),
        openings: Sequence[RunOpening] = (),
        owners: Sequence[RecordOwner] = (),
    ) -> int:
        """Write `records` in one transaction and return the last seq. A payload canon refuses
        raises `CanonError`, and so does an empty `kind`, or a `kind`, `actor` or `request_id` that
        is not a string of Unicode scalar values (`actor` and `request_id` may be None). A reused
        `request_id` raises `StoreError`. Nothing is written then.

        `transitions` are the trace entries the first record produced, stored in the same
        transaction under its seq, and coverage extends over the records (DL-536). An entry canon
        refuses raises `CanonError`, and rows with no record raise `StoreError`.

        `openings` and `owners` are the history index's rows, stored in the same transaction, and
        the history coverage extends over the records (DL-539)."""
        ...

    def first_seq(self) -> int:
        """The seq of the oldest retained record, or `last_seq() + 1` when none is retained."""
        ...

    def first_at(self) -> datetime | None:
        """The stamp of the oldest retained record, None when none is retained. The run root's
        files follow it (DL-557)."""
        ...

    def transitions_coverage(self) -> list[Interval]:
        """The covered intervals and the recorded gaps, in seq order (DL-536)."""
        ...

    def history_coverage(self) -> list[Interval]:
        """The history index's covered intervals and recorded gaps, in seq order (DL-539)."""
        ...

    def cover_history(self, intervals: Sequence[Interval]) -> None:
        """Record `intervals` in place of what the history coverage said about their seqs
        (DL-539)."""
        ...

    def trim_history(self) -> None:
        """Drop the history index's rows and coverage outside the retained records; with nothing
        outside them, write nothing (DL-539)."""
        ...

    def read_transitions(self, query: TransitionQuery) -> TransitionRead:
        """One read of the `transitions` rows in a single read transaction (DL-536)."""
        ...

    def read_history(self, query: HistoryQuery) -> HistoryRead:
        """One page of a job's past runs and their evidence, in a single read transaction
        (DL-540)."""
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

    def prune_snapshots(self) -> None:
        """Delete every snapshot but the latest two, in a transaction of its own (DL-543)."""
        ...

    def prune_bound(self, seq: int, older_than: datetime) -> int | None:
        """The seq retention may prune below: the least of `seq`, the latest snapshot's seq,
        and the first seq whose row is not older than `older_than`. None when no snapshot is
        stored. A read; it deletes nothing (spec-wiring 1b, DL-543)."""
        ...

    def prune_step(self, bound: int, limit: int) -> Pruned:
        """Delete the oldest journal rows below `bound`, at most `limit` of them, in one
        transaction, and answer how many went and the highest decision index among them. The
        delete reads seqs only, so the rows kept are one unbroken range after every step
        (spec-wiring 1b). The `transitions` rows, the history index's rows and both coverages
        below the new first seq go in the same transaction (DL-536, DL-539); `limit` counts
        journal rows only. With no journal row below `bound` the step writes nothing: a
        consistent store holds no derived row below its first seq, and the start's trim owns
        the other case. Configs and snapshots are kept. `bound` comes from `prune_bound`; a
        `limit` below 1 raises `StoreError` (DL-543)."""
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


def _decision_index(data: bytes) -> int:
    """The `index` of a stored `decision` record: the admission it decided (DL-543)."""
    return int(_decode_payload(data)["index"])


def _step_limit(limit: int) -> int:
    """A prune step's row limit as a plain int; below 1 raises, since SQLite reads a negative
    `LIMIT` as none (DL-543)."""
    count = int(limit)
    if count < 1:
        raise StoreError(f"a prune step's limit is at least 1, not {count}")
    return count


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


# ------------------------------------------------------------------ the history read (DL-540)

#: The trace marker an update's application leaves, a row with no job (`runner_journal`): the
#: entries after it run under the rev it names, so a page's fold reads every mark in its range.
_CONFIG_MARK: Final[str] = "CONFIG"


class _HistoryRows(Protocol):
    """The statements one history read is made of, each inside the read's one transaction."""

    def first_seq(self) -> int: ...

    def history_coverage(self) -> list[Interval]: ...

    def histories(self) -> list[tuple[str, int]]: ...

    def get_config(self, rev: int) -> bytes: ...

    def _go_on(self) -> None: ...

    def _stamp_of(self, seq: int) -> datetime | None:
        """The stamp of the record at `seq`, None when none is retained."""
        ...

    def _openings(
        self, job: str, floor: int, upto: int, below: int | None, count: int
    ) -> list[tuple[int, int, int, int]]:
        """At most `count` openings `(seq, n, run_number, rev)` of `job` from seq `floor` to
        `upto`, below run `below`, by run number newest first, then by key."""
        ...

    def _lowest_run(self, job: str, floor: int, upto: int) -> int | None:
        """The lowest run number among the openings of `job` from seq `floor` to `upto`."""
        ...

    def _next_opening(self, job: str, run: int, upto: int) -> tuple[int, int, int] | None:
        """The key and rev `(seq, n, rev)` of the opening of `job` with the next run number
        above `run`, at or below `upto`."""
        ...

    def _entries(
        self, job: str, low: tuple[int, int], high: tuple[int, int], cap: int
    ) -> list[tuple[int, int, bytes]]:
        """At most `cap` rows of `job` and CONFIG marks from key `low` up to `high`, excluded,
        in key order."""
        ...

    def _owned(self, job: str, run: int, upto: int, cap: int) -> list[int]:
        """The seqs of at most `cap` records the index names for `job`'s run `run`."""
        ...

    def _loose(self, job: str, low: int, high: int, cap: int) -> list[int]:
        """The seqs of at most `cap` records the index names for `job` and no run, above `low`
        and at or below `high`."""
        ...

    def _records(self, seqs: Sequence[int]) -> list[tuple[int, Record]]:
        """The records at `seqs`, in seq order."""
        ...


def _read_history(rows: _HistoryRows, query: HistoryQuery) -> HistoryRead:
    """One history page (`HistoryRead`), run by run, newest first. A page holds whole
    admissions. A run's range runs from its opening's key through the entry of the next
    opening, or past the watermark for the newest, so the fold sees when the next run began as
    the whole fold does. Its evidence is the job's entries and the CONFIG marks in that range,
    the records the index names for its run wherever they sit, and the records it names for the
    job and no run above the opening's seq and up to one seq past the next opening's: entries
    are keyed under their input's seq, so the input at an opening's own seq was admitted before
    it opened, and its decision is the next record of its transaction (`Journal.admission`).
    Every row read counts in the budget; one read for two runs counts for each."""
    job, upto = query.job, query.upto
    rows._go_on()
    seen = HistoryRead(histories=rows.histories(), first_seq=rows.first_seq())
    reach = next(
        (i for i in rows.history_coverage() if i.covered and i.first <= upto <= i.last), None
    )
    if reach is None:
        return seen
    floor = reach.first
    rows._go_on()
    found = rows._openings(job, floor, upto, query.cursor, query.limit + 1)
    base = HistoryRead(
        histories=seen.histories,
        first_seq=seen.first_seq,
        boundary=floor,
        boundary_at=rows._stamp_of(floor),
        first_full=rows._lowest_run(job, floor, upto),
    )
    for newer, older in zip(found, found[1:], strict=False):
        if newer[2] == older[2]:  # ordered by number: a number shown twice is adjacent
            return dataclasses.replace(base, duplicate=newer[2])
    page = found[: query.limit]
    taken: list[HistoryRun] = []
    entries: dict[tuple[int, int], bytes] = {}
    owned: list[int] = []
    loose: set[int] = set()
    left = query.budget
    incomplete: HistoryRun | None = None
    rest: list[tuple[int, int, int, int]] = []
    for at, (seq, n, number, rev) in enumerate(page):
        rows._go_on()
        above = rows._next_opening(job, number, upto)
        high = (upto + 1, 0) if above is None else (above[0], above[1] + 1)
        last = upto if above is None else min(above[0] + 1, upto)
        run_entries = rows._entries(job, (seq, n), high, left + 1)
        run_owned = rows._owned(job, number, upto, max(left + 1 - len(run_entries), 0))
        run_loose = rows._loose(
            job, seq, last, max(left + 1 - len(run_entries) - len(run_owned), 0)
        )
        used = len(run_entries) + len(run_owned) + len(run_loose)
        until = None if above is None else (above[0], above[1])
        run = HistoryRun(seq, n, number, rev, until, None if above is None else above[2])
        if used > left:
            if not taken:
                incomplete, rest = run, page[at + 1 :]
            else:
                rest = page[at:]
            break
        left -= used
        taken.append(run)
        entries.update({(e_seq, e_n): entry for e_seq, e_n, entry in run_entries})
        owned.extend(run_owned)
        loose.update(run_loose)
    rows._go_on()
    configs: dict[int, bytes | None] = {}
    revs = [run.rev for run in taken]
    if taken and taken[0].until_rev is not None:
        revs.append(taken[0].until_rev)
    for wanted in revs:
        if wanted not in configs:
            try:
                configs[wanted] = rows.get_config(wanted)
            except KeyError:
                configs[wanted] = None
    rows._go_on()
    return dataclasses.replace(
        base,
        runs=taken,
        incomplete=incomplete,
        more=bool(rest) or len(found) > query.limit,
        entries=[(key[0], key[1], dict(_decode_payload(entries[key]))) for key in sorted(entries)],
        owned=rows._records(sorted(owned)),
        loose=rows._records(sorted(loose)),
        configs=configs,
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
        #: the `transitions` rows in `(seq, n)` order, their coverage, and the histories in the
        #: order they began (DL-536)
        self._transitions: list[tuple[int, int, _Transition]] = []
        self._coverage: list[Interval] = []
        self._histories: list[tuple[str, int]] = []
        self._pruned_through: tuple[int, int] | None = None
        #: the history index (DL-539): `job_runs` as `(seq, n, job, run_number, rev)` in key
        #: order, `job_records` as `(seq, job, run_number)` in seq order, and their coverage
        self._job_runs: list[tuple[int, int, str, int, int]] = []
        self._job_records: list[tuple[int, str, int | None]] = []
        self._history_coverage: list[Interval] = []

    def append(
        self,
        *records: Record,
        transitions: Sequence[TransitionEntry] = (),
        openings: Sequence[RunOpening] = (),
        owners: Sequence[RecordOwner] = (),
    ) -> int:
        encoded = [_encode(record) for record in records]
        rows = [_encode_transition(entry) for entry in transitions]
        if (rows or openings or owners) and not encoded:
            raise StoreError("rows are stored with the records that produced them")
        seen: set[str] = set()
        for row in encoded:
            if row.request_id is None:
                continue
            if row.request_id in self._requests or row.request_id in seen:
                raise StoreError(f"request_id {row.request_id!r} is already in the journal")
            seen.add(row.request_id)
        if encoded:
            first = self._next_seq
            last = first + len(encoded) - 1
            self._transitions.extend((first, n, row) for n, row in enumerate(rows))
            self._coverage = _advanced(self._coverage, first, last)
            self._job_runs.extend((first, o.n, o.job, o.run_number, o.rev) for o in openings)
            self._job_records.extend((first + o.offset, o.job, o.run_number) for o in owners)
            self._history_coverage = _advanced(self._history_coverage, first, last)
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

    def prune_snapshots(self) -> None:
        for old in sorted(self._snapshots)[:-2]:
            del self._snapshots[old]

    def prune_bound(self, seq: int, older_than: datetime) -> int | None:
        latest = self.latest_snapshot()
        if latest is None:
            return None
        cutoff = _stamp(older_than)
        recent = next((entry.seq for entry in self._rows if entry.row.at >= cutoff), None)
        return min(seq, latest.seq, recent if recent is not None else seq)

    def prune_step(self, bound: int, limit: int) -> Pruned:
        count = _step_limit(limit)
        below = bisect.bisect_left(self._rows, bound, key=lambda entry: entry.seq)
        if below == 0:
            return Pruned(0, None)  # nothing below the bound: no write (DL-543)
        cut = min(below, count)
        # the new first seq when the step stops short of the bound (DL-543)
        upto = self._rows[cut].seq if cut < below else bound
        gone = self._rows[:cut]
        decision = next(
            (_decision_index(e.row.payload) for e in reversed(gone) if e.row.kind == "decision"),
            None,
        )
        for entry in gone:
            if entry.row.request_id is not None:
                del self._requests[entry.row.request_id]
        self._rows = self._rows[cut:]
        self._trim(upto, self.last_seq())
        self._trim_history(upto, self.last_seq())
        return Pruned(cut, decision)

    def meta(self) -> dict[str, str]:
        return dict(self._meta)

    def close(self) -> None:
        return None  # nothing to release

    # ---- transitions (DL-536)

    def first_seq(self) -> int:
        return self._rows[0].seq if self._rows else self._next_seq

    def first_at(self) -> datetime | None:
        return _unstamp(self._rows[0].row.at) if self._rows else None

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

    # ---- the history index (DL-539)

    def history_coverage(self) -> list[Interval]:
        return list(self._history_coverage)

    def cover_history(self, intervals: Sequence[Interval]) -> None:
        self._history_coverage = _overlaid(self._history_coverage, intervals)

    def trim_history(self) -> None:
        self._trim_history(self.first_seq(), self.last_seq())

    def _trim_history(self, first: int, last: int) -> None:
        self._job_runs = [row for row in self._job_runs if first <= row[0] <= last]
        self._job_records = [row for row in self._job_records if first <= row[0] <= last]
        self._history_coverage = _trimmed(self._history_coverage, first, last)

    # ---- the history read (DL-540)

    def read_history(self, query: HistoryQuery) -> HistoryRead:
        return _read_history(self, query)

    def _go_on(self) -> None:
        return None  # the read runs on the loop, with nothing to stop it between steps

    def _stamp_of(self, seq: int) -> datetime | None:
        found = self._records([seq])
        return found[0][1].at_utc if found else None

    def _openings(
        self, job: str, floor: int, upto: int, below: int | None, count: int
    ) -> list[tuple[int, int, int, int]]:
        found = [
            (seq, n, run, rev)
            for seq, n, owner, run, rev in self._job_runs
            if owner == job and floor <= seq <= upto and (below is None or run < below)
        ]
        return sorted(found, key=lambda row: (-row[2], -row[0], -row[1]))[:count]

    def _lowest_run(self, job: str, floor: int, upto: int) -> int | None:
        found = self._openings(job, floor, upto, None, len(self._job_runs))
        return found[-1][2] if found else None

    def _next_opening(self, job: str, run: int, upto: int) -> tuple[int, int, int] | None:
        above = [
            (number, seq, n, rev)
            for seq, n, owner, number, rev in self._job_runs
            if owner == job and number > run and seq <= upto
        ]
        return min(above)[1:] if above else None

    def _entries(
        self, job: str, low: tuple[int, int], high: tuple[int, int], cap: int
    ) -> list[tuple[int, int, bytes]]:
        start = bisect.bisect_left(self._transitions, low, key=_key)
        found: list[tuple[int, int, bytes]] = []
        for seq, n, row in self._transitions[start:]:
            if (seq, n) >= high or len(found) >= cap:
                break
            if row.job == job or (row.job == "" and row.transition == _CONFIG_MARK):
                found.append((seq, n, row.entry))
        return found

    def _owned(self, job: str, run: int, upto: int, cap: int) -> list[int]:
        return [
            seq
            for seq, owner, number in self._job_records
            if owner == job and number == run and seq <= upto
        ][:cap]

    def _loose(self, job: str, low: int, high: int, cap: int) -> list[int]:
        return [
            seq
            for seq, owner, number in self._job_records
            if owner == job and number is None and low < seq <= high
        ][:cap]

    def _records(self, seqs: Sequence[int]) -> list[tuple[int, Record]]:
        wanted = set(seqs)
        return [(entry.seq, _record(entry.row)) for entry in self._rows if entry.seq in wanted]


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
    # DL-539: the history index, created where missing as the tables above are. No unique key
    # on (job, run_number): a derived row never fails the commit of its records.
    """CREATE TABLE IF NOT EXISTS job_runs (
        seq INTEGER NOT NULL,
        n INTEGER NOT NULL,
        job TEXT NOT NULL,
        run_number INTEGER NOT NULL,
        rev INTEGER NOT NULL,
        PRIMARY KEY (seq, n)
    ) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job, run_number)",
    """CREATE TABLE IF NOT EXISTS job_records (
        seq INTEGER PRIMARY KEY,
        job TEXT NOT NULL,
        run_number INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS job_records_job ON job_records(job, run_number, seq)",
    """CREATE TABLE IF NOT EXISTS history_coverage (
        first_seq INTEGER PRIMARY KEY,
        last_seq INTEGER NOT NULL,
        covered INTEGER NOT NULL
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
            _end(self._db, "ROLLBACK")
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

    def append(
        self,
        *records: Record,
        transitions: Sequence[TransitionEntry] = (),
        openings: Sequence[RunOpening] = (),
        owners: Sequence[RecordOwner] = (),
    ) -> int:
        encoded = [_encode(record) for record in records]
        rows = [_encode_transition(entry) for entry in transitions]
        if not encoded:
            if rows or openings or owners:
                raise StoreError("rows are stored with the records that produced them")
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
            first = last - len(encoded) + 1
            self._record_transitions(first, last, rows)
            self._record_history(first, last, openings, owners)
            return last

    def _record_transitions(self, first: int, last: int, rows: list[_Transition]) -> None:
        """The rows of record `first` and the coverage over `first` to `last`, inside the
        append's transaction: a failure here rolls the records back too (DL-536)."""
        self._db.executemany(
            "INSERT INTO transitions(seq, n, job, transition, entry) VALUES (?, ?, ?, ?, ?)",
            [(first, n, row.job, row.transition, row.entry) for n, row in enumerate(rows)],
        )
        self._advance("transitions_coverage", first, last)

    def _record_history(
        self,
        first: int,
        last: int,
        openings: Sequence[RunOpening],
        owners: Sequence[RecordOwner],
    ) -> None:
        """The history index's rows of records `first` to `last` and its coverage over them,
        inside the append's transaction (DL-539)."""
        self._db.executemany(
            "INSERT INTO job_runs(seq, n, job, run_number, rev) VALUES (?, ?, ?, ?, ?)",
            [(first, o.n, o.job, o.run_number, o.rev) for o in openings],
        )
        self._db.executemany(
            "INSERT INTO job_records(seq, job, run_number) VALUES (?, ?, ?)",
            [(first + o.offset, o.job, o.run_number) for o in owners],
        )
        self._advance("history_coverage", first, last)

    def _advance(self, table: str, first: int, last: int) -> None:
        """Coverage table `table` once records `first` to `last` were appended with their rows:
        the covered interval that ends at `first - 1` grows, or a new one starts (DL-536)."""
        grown = self._db.execute(
            f"UPDATE {table} SET last_seq = ? WHERE covered = 1 AND last_seq = ?",
            (last, first - 1),
        )
        if grown.rowcount == 0:
            self._db.execute(
                f"INSERT INTO {table}(first_seq, last_seq, covered) VALUES (?, ?, 1)",
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

    def prune_snapshots(self) -> None:
        with self._transaction():
            self._db.execute(
                "DELETE FROM snapshots WHERE seq NOT IN"
                " (SELECT seq FROM snapshots ORDER BY seq DESC LIMIT 2)"
            )

    def prune_bound(self, seq: int, older_than: datetime) -> int | None:
        latest = self._db.execute("SELECT MAX(seq) FROM snapshots").fetchone()[0]
        if latest is None:
            return None
        # the first row inside the window, in seq order: the rows already pruned are gone, so
        # the scan stops near the head
        recent = self._db.execute(
            "SELECT seq FROM journal WHERE at_utc >= ? ORDER BY seq LIMIT 1",
            (_stamp(older_than),),
        ).fetchone()
        return min(seq, int(latest), int(recent[0]) if recent is not None else seq)

    def prune_step(self, bound: int, limit: int) -> Pruned:
        count = _step_limit(limit)
        if self._db.execute("SELECT 1 FROM journal WHERE seq < ? LIMIT 1", (bound,)).fetchone():
            with self._transaction():
                # the first row the step keeps when it stops short of the bound (DL-543)
                beyond = self._db.execute(
                    "SELECT seq FROM journal WHERE seq < ? ORDER BY seq LIMIT 1 OFFSET ?",
                    (bound, count),
                ).fetchone()
                upto = bound if beyond is None else int(beyond[0])
                # the rows below `upto` are the step's own: the search stays inside them
                last = self._db.execute(
                    "SELECT payload FROM journal WHERE seq < ? AND kind = 'decision'"
                    " ORDER BY seq DESC LIMIT 1",
                    (upto,),
                ).fetchone()
                deleted = self._db.execute("DELETE FROM journal WHERE seq < ?", (upto,)).rowcount
                self._trim(upto, self.last_seq())
                self._trim_history(upto, self.last_seq())
                return Pruned(
                    int(deleted), None if last is None else _decision_index(bytes(last[0]))
                )
        return Pruned(0, None)  # nothing below the bound: no write (DL-543)

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

    def first_at(self) -> datetime | None:
        row = self._db.execute("SELECT at_utc FROM journal ORDER BY seq LIMIT 1").fetchone()
        return None if row is None else _unstamp(row[0])

    def transitions_coverage(self) -> list[Interval]:
        return self._coverage("transitions_coverage")

    def _coverage(self, table: str) -> list[Interval]:
        return [
            Interval(int(first), int(last), bool(covered))
            for first, last, covered in self._db.execute(
                f"SELECT first_seq, last_seq, covered FROM {table} ORDER BY first_seq"
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
            _end(self._db, "COMMIT")

    def cover_transitions(self, intervals: Sequence[Interval]) -> None:
        with self._transaction():
            self._set_coverage(
                "transitions_coverage", _overlaid(self.transitions_coverage(), intervals)
            )

    def trim_transitions(self) -> None:
        if self._settled(("transitions",), "transitions_coverage"):
            return  # a start with nothing to trim commits nothing for it
        with self._transaction():
            self._trim(self.first_seq(), self.last_seq())

    def _settled(self, tables: tuple[str, ...], coverage: str) -> bool:
        """Whether no row of `tables` and no interval of `coverage` lies outside the retained
        records (DL-536)."""
        first, last = self.first_seq(), self.last_seq()
        held = self._coverage(coverage)
        for table in tables:
            outside = self._db.execute(
                f"SELECT 1 FROM {table} WHERE seq < ? UNION ALL"
                f" SELECT 1 FROM {table} WHERE seq > ? LIMIT 1",
                (first, last),
            ).fetchone()
            if outside is not None:
                return False
        return _trimmed(held, first, last) == held

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
        self._set_coverage(
            "transitions_coverage", _trimmed(self.transitions_coverage(), first, last)
        )

    def _set_coverage(self, table: str, coverage: list[Interval]) -> None:
        self._db.execute(f"DELETE FROM {table}")
        self._db.executemany(
            f"INSERT INTO {table}(first_seq, last_seq, covered) VALUES (?, ?, ?)",
            [(item.first, item.last, int(item.covered)) for item in coverage],
        )

    # ---- the history index (DL-539)

    def history_coverage(self) -> list[Interval]:
        return self._coverage("history_coverage")

    def cover_history(self, intervals: Sequence[Interval]) -> None:
        with self._transaction():
            self._set_coverage("history_coverage", _overlaid(self.history_coverage(), intervals))

    def trim_history(self) -> None:
        if self._settled(("job_runs", "job_records"), "history_coverage"):
            return  # a start with nothing to trim commits nothing for it
        with self._transaction():
            self._trim_history(self.first_seq(), self.last_seq())

    def _trim_history(self, first: int, last: int) -> None:
        """Inside the caller's transaction: the index's rows and coverage outside `first` to
        `last` go, two key ranges per table so no kept row is scanned."""
        for table in ("job_runs", "job_records"):
            self._db.execute(f"DELETE FROM {table} WHERE seq < ?", (first,))
            self._db.execute(f"DELETE FROM {table} WHERE seq > ?", (last,))
        self._set_coverage("history_coverage", _trimmed(self.history_coverage(), first, last))

    # ---- the history read (DL-540)

    def read_history(self, query: HistoryQuery) -> HistoryRead:
        """One read transaction, as `read_transitions`: it holds no lock a writer waits on
        (WAL), every part is read at the same point, and an interrupt stops it at its next
        step. Each statement searches one index from one end of its key range."""
        self._db.execute("BEGIN")
        try:
            return _read_history(self, query)
        finally:
            _end(self._db, "COMMIT")

    def _stamp_of(self, seq: int) -> datetime | None:
        row = self._db.execute("SELECT at_utc FROM journal WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else _unstamp(row[0])

    def _openings(
        self, job: str, floor: int, upto: int, below: int | None, count: int
    ) -> list[tuple[int, int, int, int]]:
        under = "" if below is None else " AND run_number < ?"
        return [
            (int(seq), int(n), int(run), int(rev))
            for seq, n, run, rev in self._db.execute(
                "SELECT seq, n, run_number, rev FROM job_runs INDEXED BY job_runs_job"
                f" WHERE job = ?{under} AND seq >= ? AND seq <= ?"
                " ORDER BY run_number DESC, seq DESC, n DESC LIMIT ?",
                (job, *(() if below is None else (below,)), floor, upto, count),
            )
        ]

    def _lowest_run(self, job: str, floor: int, upto: int) -> int | None:
        row = self._db.execute(
            "SELECT run_number FROM job_runs INDEXED BY job_runs_job"
            " WHERE job = ? AND seq >= ? AND seq <= ? ORDER BY run_number LIMIT 1",
            (job, floor, upto),
        ).fetchone()
        return None if row is None else int(row[0])

    def _next_opening(self, job: str, run: int, upto: int) -> tuple[int, int, int] | None:
        row = self._db.execute(
            "SELECT seq, n, rev FROM job_runs INDEXED BY job_runs_job"
            " WHERE job = ? AND run_number > ? AND seq <= ? ORDER BY run_number, seq, n LIMIT 1",
            (job, run, upto),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]), int(row[2]))

    def _entries(
        self, job: str, low: tuple[int, int], high: tuple[int, int], cap: int
    ) -> list[tuple[int, int, bytes]]:
        found = [
            (int(seq), int(n), bytes(entry))
            for statement, params in (
                (
                    "SELECT seq, n, entry FROM transitions INDEXED BY transitions_job"
                    " WHERE job = ? AND (seq, n) >= (?, ?) AND (seq, n) < (?, ?)",
                    (job,),
                ),
                (
                    "SELECT seq, n, entry FROM transitions INDEXED BY transitions_job_transition"
                    " WHERE job = '' AND transition = ? AND (seq, n) >= (?, ?) AND (seq, n) < (?, ?)",
                    (_CONFIG_MARK,),
                ),
            )
            for seq, n, entry in self._db.execute(
                f"{statement} ORDER BY seq, n LIMIT ?", (*params, *low, *high, cap)
            )
        ]
        return sorted(found)[:cap]

    def _owned(self, job: str, run: int, upto: int, cap: int) -> list[int]:
        return [
            int(seq)
            for (seq,) in self._db.execute(
                "SELECT seq FROM job_records INDEXED BY job_records_job"
                " WHERE job = ? AND run_number = ? AND seq <= ? ORDER BY seq LIMIT ?",
                (job, run, upto, cap),
            )
        ]

    def _loose(self, job: str, low: int, high: int, cap: int) -> list[int]:
        return [
            int(seq)
            for (seq,) in self._db.execute(
                "SELECT seq FROM job_records INDEXED BY job_records_job"
                " WHERE job = ? AND run_number IS NULL AND seq > ? AND seq <= ?"
                " ORDER BY seq LIMIT ?",
                (job, low, high, cap),
            )
        ]

    def _records(self, seqs: Sequence[int]) -> list[tuple[int, Record]]:
        found: list[tuple[int, Record]] = []
        for start in range(0, len(seqs), _CHUNK):
            chunk = list(seqs[start : start + _CHUNK])
            self._go_on()
            marks = ", ".join("?" * len(chunk))
            for seq, at, kind, actor, request_id, payload in self._db.execute(
                "SELECT seq, at_utc, kind, actor, request_id, payload FROM journal"
                f" WHERE seq IN ({marks}) ORDER BY seq",
                chunk,
            ):
                record = Record(_unstamp(at), kind, actor, request_id, _decode_payload(payload))
                found.append((int(seq), record))
        return found

    # ---- the run history's read (DL-555)

    @contextmanager
    def run_evidence(self) -> Iterator[RunEvidence]:
        """One read transaction for a reader's reads of the store: it yields the history
        index's openings, the stored transitions and both coverages (`RunEvidence`), and every
        read of this store the caller makes inside the block sees the same point. On a reader's
        connection it holds no lock a writer waits on (WAL)."""
        self._db.execute("BEGIN")
        try:
            tables = {
                str(name)
                for (name,) in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            first, last = self.first_seq(), self.last_seq()

            def coverage(table: str, rows: tuple[str, ...]) -> list[Interval]:
                if not {table, *rows} <= tables:
                    return []
                return _trimmed(self._coverage(table), first, last)

            history = coverage("history_coverage", ("job_runs",))
            stored = coverage("transitions_coverage", ("transitions",))
            openings: list[tuple[int, int, str, int, int]] = []
            for low, high in covered_parts(history, first, last):
                openings.extend(
                    (int(seq), int(n), str(job), int(run), int(rev))
                    for seq, n, job, run, rev in self._db.execute(
                        "SELECT seq, n, job, run_number, rev FROM job_runs"
                        " WHERE seq >= ? AND seq <= ? ORDER BY seq, n",
                        (low, high),
                    )
                )
            transitions: list[tuple[int, int, dict[str, Any]]] = []
            for low, high in covered_parts(stored, first, last):
                transitions.extend(
                    (int(seq), int(n), dict(_decode_payload(bytes(entry))))
                    for seq, n, entry in self._db.execute(
                        "SELECT seq, n, entry FROM transitions"
                        " WHERE seq >= ? AND seq <= ? ORDER BY seq, n",
                        (low, high),
                    )
                )
            yield RunEvidence(first, last, history, stored, openings, transitions)
        finally:
            self._db.execute("COMMIT")

    # ---- the archive's reads (DL-545)

    def raw_batch(
        self, first: int, below: int, limit: int, known: Collection[tuple[int, str]] = ()
    ) -> RawBatch:
        """At most `limit` journal rows from seq `first` to below `below`, with their derived
        rows, the coverage, the configurations' revs and the bytes of every one whose
        `(rev, digest)` is not in `known`, the history list and the last seq, as stored, in one
        read transaction: every part is read at the same point, and on a reader's connection it
        holds no lock a writer waits on (WAL)."""
        count = _step_limit(limit)
        self._db.execute("BEGIN")
        try:
            records = [
                (int(seq), str(at), str(kind), actor, request_id, bytes(payload))
                for seq, at, kind, actor, request_id, payload in self._db.execute(
                    "SELECT seq, at_utc, kind, actor, request_id, payload FROM journal"
                    " WHERE seq >= ? AND seq < ? ORDER BY seq LIMIT ?",
                    (int(first), int(below), count),
                )
            ]
            transitions_coverage = self.transitions_coverage()
            history_coverage = self.history_coverage()
            transitions: list[tuple[int, int, str, str, bytes]] = []
            job_runs: list[tuple[int, int, str, int, int]] = []
            job_records: list[tuple[int, str, int | None]] = []
            if records:
                low, high = records[0][0], records[-1][0]
                for a, b in covered_parts(transitions_coverage, low, high):
                    transitions.extend(
                        (int(seq), int(n), str(job), str(name), bytes(entry))
                        for seq, n, job, name, entry in self._db.execute(
                            "SELECT seq, n, job, transition, entry FROM transitions"
                            " WHERE seq >= ? AND seq <= ? ORDER BY seq, n",
                            (a, b),
                        )
                    )
                for a, b in covered_parts(history_coverage, low, high):
                    job_runs.extend(
                        (int(seq), int(n), str(job), int(run), int(rev))
                        for seq, n, job, run, rev in self._db.execute(
                            "SELECT seq, n, job, run_number, rev FROM job_runs"
                            " WHERE seq >= ? AND seq <= ? ORDER BY seq, n",
                            (a, b),
                        )
                    )
                    job_records.extend(
                        (int(seq), str(job), None if run is None else int(run))
                        for seq, job, run in self._db.execute(
                            "SELECT seq, job, run_number FROM job_records"
                            " WHERE seq >= ? AND seq <= ? ORDER BY seq",
                            (a, b),
                        )
                    )
            configs = [
                (int(rev), str(digest))
                for rev, digest in self._db.execute("SELECT rev, digest FROM configs ORDER BY rev")
            ]
            config_data: list[tuple[int, str, bytes]] = []
            for rev in [rev for rev, digest in configs if (rev, digest) not in known]:
                config_data.extend(
                    (int(found), str(digest), bytes(data))
                    for found, digest, data in self._db.execute(
                        "SELECT rev, digest, data FROM configs WHERE rev = ?", (rev,)
                    )
                )
            return RawBatch(
                first_seq=self.first_seq(),
                records=records,
                transitions=transitions,
                job_runs=job_runs,
                job_records=job_records,
                transitions_coverage=transitions_coverage,
                history_coverage=history_coverage,
                configs=configs,
                config_data=config_data,
                histories=self.histories(),
                last_seq=self.last_seq(),
            )
        finally:
            _end(self._db, "COMMIT")


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
            _end(self._db, "ROLLBACK")


def _end(db: sqlite3.Connection, statement: str) -> None:
    """End the open transaction with `statement`, COMMIT or ROLLBACK, only while one is open.
    SQLite ends a transaction by itself after some errors (a full disk): the statement would
    then raise and hide the error that ended it (DL-553)."""
    if db.in_transaction:
        db.execute(statement)
