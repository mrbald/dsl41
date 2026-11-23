"""The archive database and its worker: verified copies of the rows retention prunes (DL-545).

The live store keeps `retention_days`. Before the engine deletes a journal row, the worker
copies it with its derived rows to a second SQLite file, reads the copy back and compares it,
byte for byte, with the live rows. Only a range this run verified so is reported to the
engine's loop, and only the loop deletes, through the prune step of DL-543. No "archived
through" number is stored or trusted across a restart.

The archive stores by content and records lineage (docs/runner-design.md section 7):

- A journal row is keyed by its root, its seq and a digest of its bytes; a derived set (the
  transitions, run openings or record owners of one seq) by its root, table, seq and a digest
  of the set, and a complete empty set is a constant, not rows; a configuration by its root,
  rev and the live store's digest. The same seq with other bytes is another row; the same
  bytes share one row.
- An observation records, per seq, under which history of the live store's list the seq lay,
  which journal row, which derived set per table (none where the live store made no claim),
  and which configuration a record applies. It is never changed. The live store's history
  list and its last seq are recorded as they are; nothing is inferred from rows that differ,
  and nothing is forked, set aside or stopped because they do.
- Every batch, and a round with nothing to copy, is first turned into one value, its image.
  One transaction writes the image and reads it back from the archive; the two are compared
  whole before the commit, and again after it on a connection opened then. Only that makes a
  report.
- Plain SQL views in the file compute the current line when it is read: the newest list
  gives each seq its history, and a seq reads "on line", "line not known" or "not archived"
  by the observations that count under it (docs/deploy.md).

The worker is one thread per engine run. It is the only writer of the archive; the sidecar
`<archive>.lock` (an advisory `flock`) keeps a second engine out. Every table is keyed by the
live store's `root_id` first, so one archive can hold several roots.

The module imports the store's read helpers and nothing from the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import fcntl
import functools
import hashlib
import json
import os
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from dsl41.runner_store import RawBatch, SqliteStore, covered_parts

#: The layout this build writes and reads; a file with any other is refused by name.
ARCHIVE_LAYOUT: Final[str] = "dsl41-archive-content-1"

#: How long the shutdown waits for the worker to end.
STOP_DEADLINE_S: Final[float] = 5.0

#: The states of a coverage interval of the journal rows: the archive holds verified copies;
#: the archive never received them; they were held and expired.
COVERED: Final = "covered"
GAP: Final = "never received"
EXPIRED: Final = "expired"

#: The derived tables, each stored as sets by seq.
SETS: Final[tuple[str, ...]] = ("transitions", "job_runs", "job_records")

#: The value an observation holds for a complete empty set: a constant, not rows. A set
#: digest is 32 bytes, so this is no digest.
EMPTY_SET: Final[bytes] = b""

#: What an observation of a record that applies a configuration says about it.
BOUND: Final = "bound"
UNHELD_REV: Final = "the live store holds no such rev"

# ---- the reading views (DL-545, docs/deploy.md). Plain SQL that any sqlite3 shell from
# 3.8.3 on parses: an older shell that cannot parse a view cannot read the file at all.

#: The current line as ranges of seqs, from the newest list and its last seq. A history
#: holds the seqs from its first to the one before the next history in the list's order
#: begins, the last history up to the last seq; the seqs before every history have none
#: (NULL). A history that holds no seq has no range, and no seq above the last seq has one.
#: The live store gives the list in the order its histories begin (ties in its own order),
#: and the archive numbers the positions from 0, so the next history is the next position.
_LINE_RANGES: Final = """SELECT root_id, history, first_seq, last_seq FROM (
    SELECT l.root_id, NULL AS history, 1 AS first_seq,
        min(coalesce(e.first_seq - 1, l.last_seq), l.last_seq) AS last_seq
    FROM lists AS l
    LEFT JOIN list_entries AS e
        ON e.root_id = l.root_id AND e.version = l.version AND e.position = 0
    WHERE l.version = (SELECT max(m.version) FROM lists AS m WHERE m.root_id = l.root_id)
    UNION ALL
    SELECT e.root_id, h.history, e.first_seq,
        min(coalesce(f.first_seq - 1, l.last_seq), l.last_seq)
    FROM lists AS l
    JOIN list_entries AS e ON e.root_id = l.root_id AND e.version = l.version
    JOIN histories AS h ON h.root_id = e.root_id AND h.id = e.history_id
    LEFT JOIN list_entries AS f
        ON f.root_id = e.root_id AND f.version = e.version AND f.position = e.position + 1
    WHERE l.version = (SELECT max(m.version) FROM lists AS m WHERE m.root_id = l.root_id))
WHERE first_seq <= last_seq"""

#: An observation counts unless another of the same seq, history and journal row is equal
#: wherever this one makes a claim; two that disagree both count.
_COUNTED: Final = """SELECT o.* FROM observations AS o
WHERE NOT EXISTS (
    SELECT 1 FROM observations AS p
    WHERE p.root_id = o.root_id AND p.seq = o.seq AND p.record_digest = o.record_digest
        AND p.history_id IS o.history_id AND p.id <> o.id
        AND (o.transitions_set IS NULL OR o.transitions_set = p.transitions_set)
        AND (o.job_runs_set IS NULL OR o.job_runs_set = p.job_runs_set)
        AND (o.job_records_set IS NULL OR o.job_records_set = p.job_records_set)
        AND (o.config_rev IS NULL OR o.config_rev = p.config_rev)
        AND (o.config_digest IS NULL OR o.config_digest = p.config_digest)
        AND (o.config_binding IS NULL OR o.config_binding = p.config_binding))"""


def _state(root: str, seq: str, history: str) -> str:
    """A seq's state on the current line, given the history that holds it, decided in this
    order: not archived where no observation under that history counts (for a seq no
    history holds, an observation with no history), whether or not a history holds the seq;
    then line not known where no history holds it or several count; else on line."""
    count = f"""(SELECT count(*) FROM counted_observations AS d
        LEFT JOIN histories AS g ON g.root_id = d.root_id AND g.id = d.history_id
        WHERE d.root_id = {root} AND d.seq = {seq} AND g.history IS {history})"""
    return f"""CASE {count} WHEN 0 THEN 'not archived'
        WHEN 1 THEN CASE WHEN {history} IS NULL THEN 'line not known' ELSE 'on line' END
        ELSE 'line not known' END"""


#: The counting observations of the seqs on the current line, under the history that holds
#: each seq, with its state. The ranges drive the join (CROSS JOIN keeps them outside): the
#: file has no index that finds a range from a seq.
_LINE_OBSERVATIONS: Final = f"""SELECT c.root_id, c.seq,
    {_state("c.root_id", "c.seq", "r.history")} AS state,
    r.history, c.id AS observation, c.record_digest, c.transitions_set, c.job_runs_set,
    c.job_records_set, c.config_rev, c.config_digest, c.config_binding
FROM line_ranges AS r
CROSS JOIN counted_observations AS c
    ON c.root_id = r.root_id AND c.seq >= r.first_seq AND c.seq <= r.last_seq
LEFT JOIN histories AS h ON h.root_id = c.root_id AND h.id = c.history_id
WHERE h.history IS r.history"""

#: Each seq of the current line that reads "line not known", once. Only a seq with a second
#: observation, or one with no history, can read so, and only those are asked of
#: `line_observations` (the CASE keeps that order). The seq is given as a range of one:
#: SQLite 3.26 then finds the observations by it, where `=` lets it scan each history's
#: range.
_LINE_NOT_KNOWN: Final = """SELECT x.root_id, x.seq FROM observations AS x
WHERE NOT EXISTS (
        SELECT 1 FROM observations AS z
        WHERE z.root_id = x.root_id AND z.seq = x.seq AND z.id < x.id)
    AND CASE WHEN EXISTS (
            SELECT 1 FROM observations AS y
            WHERE y.root_id = x.root_id AND y.seq = x.seq
                AND (y.id <> x.id OR y.history_id IS NULL))
        THEN EXISTS (
            SELECT 1 FROM line_observations AS u
            WHERE u.root_id = x.root_id AND u.seq BETWEEN x.seq AND x.seq
                AND u.state = 'line not known')
        ELSE 0 END"""

#: Every seq from 1 to the newest list's last seq, with its history and its state.
_CURRENT_LINE: Final = f"""WITH RECURSIVE seqs(root_id, history, seq, last_seq) AS (
    SELECT root_id, history, first_seq, last_seq FROM line_ranges
    UNION ALL
    SELECT root_id, history, seq + 1, last_seq FROM seqs WHERE seq < last_seq)
SELECT s.root_id, s.seq, s.history, {_state("s.root_id", "s.seq", "s.history")} AS state
FROM seqs AS s"""

#: The default views: the current line's rows, each with its seq's state. A seq that is
#: "line not known" shows every counting row, and no join crosses it: a dispatch is joined
#: to its outcome only when neither they nor any seq between them reads "line not known". A
#: configuration is bound to its own observation, so `line_configurations` shows it at any
#: seq, marked. In `line_dispatches` the dispatches, the outcomes and the seqs that are not
#: known are DISTINCT so that SQLite builds each once per query as a table of its own, indexed for the
#: join; their rows are distinct already. A pair is then checked against the seqs not known,
#: which are normally none, not against every seq between its ends.
#: A set's rows are found by `seq BETWEEN o.seq AND o.seq`: SQLite 3.26 looks that up by
#: seq, where `=` lets it scan the whole range of the seq's history for each row.
_LINE_VIEWS: Final = (
    (
        "line_records",
        """SELECT o.root_id, o.seq, o.state, o.history, o.observation, r.at_utc, r.kind,
            r.actor, r.request_id, r.payload, r.archived_at
        FROM line_observations AS o
        JOIN records AS r
            ON r.root_id = o.root_id AND r.seq = o.seq AND r.digest = o.record_digest""",
    ),
    (
        "line_transitions",
        """SELECT o.root_id, o.seq, o.state, o.history, o.observation, t.n, t.job,
            t.transition, t.entry
        FROM line_observations AS o
        JOIN transitions AS t
            ON t.root_id = o.root_id AND t.seq BETWEEN o.seq AND o.seq
            AND t.digest = o.transitions_set""",
    ),
    (
        "line_run_openings",
        """SELECT o.root_id, o.seq, o.state, o.history, o.observation, j.n, j.job,
            j.run_number, j.rev
        FROM line_observations AS o
        JOIN job_runs AS j
            ON j.root_id = o.root_id AND j.seq BETWEEN o.seq AND o.seq
            AND j.digest = o.job_runs_set""",
    ),
    (
        "line_record_owners",
        """SELECT o.root_id, o.seq, o.state, o.history, o.observation, j.job, j.run_number
        FROM line_observations AS o
        JOIN job_records AS j
            ON j.root_id = o.root_id AND j.seq BETWEEN o.seq AND o.seq
            AND j.digest = o.job_records_set""",
    ),
    (
        "line_configurations",
        """SELECT o.root_id, o.seq, o.state, o.history, o.observation, o.config_rev AS rev,
            o.config_digest AS digest, o.config_binding AS binding, c.data
        FROM line_observations AS o
        LEFT JOIN configs AS c
            ON c.root_id = o.root_id AND c.rev = o.config_rev AND c.digest = o.config_digest
        WHERE o.config_rev IS NOT NULL""",
    ),
    (
        "line_dispatches",
        """WITH dispatches AS (
            SELECT DISTINCT root_id, seq, state, history, observation, at_utc, payload,
                json_extract(payload, '$.attempt') AS attempt
            FROM line_records WHERE kind = 'dispatch'),
        outcomes AS (
            SELECT DISTINCT root_id, seq, at_utc, payload,
                json_extract(payload, '$.attempt') AS attempt
            FROM line_records WHERE kind = 'outcome' AND state = 'on line'),
        joined AS (
            SELECT d.root_id, d.seq AS dispatch_seq, o.seq AS outcome_seq,
                o.at_utc AS outcome_at, o.payload AS outcome_payload
            FROM dispatches AS d
            JOIN outcomes AS o
                ON d.state = 'on line' AND o.root_id = d.root_id AND o.attempt = d.attempt
                AND o.seq > d.seq
            LEFT JOIN (SELECT DISTINCT root_id, seq FROM line_not_known) AS u
                ON u.root_id = d.root_id AND u.seq > d.seq AND u.seq < o.seq
            GROUP BY d.root_id, d.seq, o.seq
            HAVING count(u.seq) = 0)
        SELECT d.root_id, d.seq, d.state, d.history, d.observation, d.at_utc,
            json_extract(d.payload, '$.job') AS job,
            json_extract(d.payload, '$.run_number') AS run_number, d.attempt,
            j.outcome_seq, j.outcome_at, json_extract(j.outcome_payload, '$.outcome') AS outcome,
            j.outcome_payload
        FROM dispatches AS d
        LEFT JOIN joined AS j ON j.root_id = d.root_id AND j.dispatch_seq = d.seq""",
    ),
)

#: What an observation says of one derived table: no claim, a complete empty set, or the
#: set's digest in hex.
_CLAIM: Final = (
    "CASE WHEN o.{0} IS NULL THEN 'no claim' WHEN o.{0} = x'' THEN 'empty'"
    " ELSE lower(hex(o.{0})) END"
)

#: Every stored row, set, configuration and list, each with its history.
_ALL_VIEWS: Final = (
    (
        "all_records",
        f"""SELECT o.root_id, o.seq, h.history, o.id AS observation, r.at_utc, r.kind, r.actor,
            r.request_id, r.payload, r.archived_at,
            {_CLAIM.format("transitions_set")} AS transitions,
            {_CLAIM.format("job_runs_set")} AS run_openings,
            {_CLAIM.format("job_records_set")} AS record_owners,
            o.config_rev, o.config_digest, o.config_binding
        FROM observations AS o
        JOIN records AS r
            ON r.root_id = o.root_id AND r.seq = o.seq AND r.digest = o.record_digest
        LEFT JOIN histories AS h ON h.root_id = o.root_id AND h.id = o.history_id""",
    ),
    (
        "all_transitions",
        """SELECT o.root_id, o.seq, h.history, o.id AS observation, t.n, t.job, t.transition,
            t.entry
        FROM observations AS o
        JOIN transitions AS t
            ON t.root_id = o.root_id AND t.seq = o.seq AND t.digest = o.transitions_set
        LEFT JOIN histories AS h ON h.root_id = o.root_id AND h.id = o.history_id""",
    ),
    (
        "all_run_openings",
        """SELECT o.root_id, o.seq, h.history, o.id AS observation, j.n, j.job, j.run_number,
            j.rev
        FROM observations AS o
        JOIN job_runs AS j
            ON j.root_id = o.root_id AND j.seq = o.seq AND j.digest = o.job_runs_set
        LEFT JOIN histories AS h ON h.root_id = o.root_id AND h.id = o.history_id""",
    ),
    (
        "all_record_owners",
        """SELECT o.root_id, o.seq, h.history, o.id AS observation, j.job, j.run_number
        FROM observations AS o
        JOIN job_records AS j
            ON j.root_id = o.root_id AND j.seq = o.seq AND j.digest = o.job_records_set
        LEFT JOIN histories AS h ON h.root_id = o.root_id AND h.id = o.history_id""",
    ),
    (
        "all_configurations",
        """SELECT c.root_id, c.rev, c.digest, c.archived_at, c.data, o.seq, h.history,
            o.id AS observation
        FROM configs AS c
        LEFT JOIN observations AS o
            ON o.root_id = c.root_id AND o.config_rev = c.rev AND o.config_digest = c.digest
        LEFT JOIN histories AS h ON h.root_id = o.root_id AND h.id = o.history_id""",
    ),
    (
        "all_lists",
        """SELECT l.root_id, l.version, l.recorded_at, l.last_seq, e.position, h.history,
            e.first_seq
        FROM lists AS l
        LEFT JOIN list_entries AS e ON e.root_id = l.root_id AND e.version = l.version
        LEFT JOIN histories AS h ON h.root_id = e.root_id AND h.id = e.history_id""",
    ),
)

_SCHEMA: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS roots (
        root_id TEXT PRIMARY KEY,
        state_version TEXT,
        estate_name TEXT,
        created_at TEXT,
        first_archived_at TEXT NOT NULL
    )""",
    # the journal row as it was, keyed by its content; no unique key on request_id
    """CREATE TABLE IF NOT EXISTS records (
        root_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        digest BLOB NOT NULL,
        at_utc TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT,
        request_id TEXT,
        payload TEXT NOT NULL,
        archived_at TEXT NOT NULL,
        PRIMARY KEY (root_id, seq, digest)
    )""",
    # one complete non-empty derived set of one seq, its rows below under the same key
    """CREATE TABLE IF NOT EXISTS sets (
        root_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        seq INTEGER NOT NULL,
        digest BLOB NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY (root_id, table_name, seq, digest)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS transitions (
        root_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        digest BLOB NOT NULL,
        n INTEGER NOT NULL,
        job TEXT NOT NULL,
        transition TEXT NOT NULL,
        entry TEXT NOT NULL,
        PRIMARY KEY (root_id, seq, digest, n)
    )""",
    """CREATE TABLE IF NOT EXISTS job_runs (
        root_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        digest BLOB NOT NULL,
        n INTEGER NOT NULL,
        job TEXT NOT NULL,
        run_number INTEGER NOT NULL,
        rev INTEGER NOT NULL,
        PRIMARY KEY (root_id, seq, digest, n)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS job_records (
        root_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        digest BLOB NOT NULL,
        job TEXT NOT NULL,
        run_number INTEGER,
        PRIMARY KEY (root_id, seq, digest)
    ) WITHOUT ROWID""",
    """CREATE TABLE IF NOT EXISTS configs (
        root_id TEXT NOT NULL,
        rev INTEGER NOT NULL,
        digest TEXT NOT NULL,
        data TEXT NOT NULL,
        archived_at TEXT NOT NULL,
        PRIMARY KEY (root_id, rev, digest)
    )""",
    # the live store's histories (DL-536), each given an id of this archive
    """CREATE TABLE IF NOT EXISTS histories (
        root_id TEXT NOT NULL,
        id INTEGER NOT NULL,
        history TEXT NOT NULL,
        PRIMARY KEY (root_id, id),
        UNIQUE (root_id, history)
    )""",
    # each version of the live store's history list as recorded, and its last seq
    """CREATE TABLE IF NOT EXISTS lists (
        root_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        recorded_at TEXT NOT NULL,
        last_seq INTEGER NOT NULL,
        PRIMARY KEY (root_id, version)
    )""",
    """CREATE TABLE IF NOT EXISTS list_entries (
        root_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        position INTEGER NOT NULL,
        history_id INTEGER NOT NULL,
        first_seq INTEGER NOT NULL,
        PRIMARY KEY (root_id, version, position)
    ) WITHOUT ROWID""",
    # what one seq was seen as, under one history: a NULL set is no claim, an empty blob a
    # complete empty set; never changed once stored
    """CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY,
        root_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        history_id INTEGER,
        record_digest BLOB NOT NULL,
        transitions_set BLOB,
        job_runs_set BLOB,
        job_records_set BLOB,
        config_rev INTEGER,
        config_digest TEXT,
        config_binding TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS observations_seq ON observations(root_id, seq, record_digest)",
    """CREATE TABLE IF NOT EXISTS coverage (
        root_id TEXT NOT NULL,
        first_seq INTEGER NOT NULL,
        last_seq INTEGER NOT NULL,
        state TEXT NOT NULL,
        PRIMARY KEY (root_id, first_seq)
    ) WITHOUT ROWID""",
    *(
        f"CREATE VIEW IF NOT EXISTS {name} AS {body}"
        for name, body in (
            ("line_ranges", _LINE_RANGES),
            ("counted_observations", _COUNTED),
            ("line_observations", _LINE_OBSERVATIONS),
            ("line_not_known", _LINE_NOT_KNOWN),
            ("current_line", _CURRENT_LINE),
            *_LINE_VIEWS,
            *_ALL_VIEWS,
        )
    ),
)


class ArchiveStopped(Exception):
    """The archive cannot take this round: a file it cannot use, live bytes it cannot store,
    or an archive that does not read back as the image it was given. The text is the reason
    `status` shows."""


@dataclass(frozen=True, slots=True)
class Report:
    """What the worker tells the loop (DL-545). `round` is the engine's round it worked for.
    `stopped` is the reason the archive stopped. `verified` is the seq range this round read
    back and found identical so far, from its first batch's first seq. `completed` says the whole round,
    expiry included, completed and verified, with `histories`, the count of the root's
    histories the archive holds, and `last_seq`, the last seq of the newest list."""

    round: int
    stopped: str | None = None
    verified: tuple[int, int] | None = None
    completed: bool = False
    histories: int | None = None
    last_seq: int | None = None


# ---- digests


def _field(value: object) -> bytes:
    """One field of a digest: a marker for NULL, else a marker, an 8-byte length and the bytes,
    so NULL and empty text differ and no two field lists encode alike."""
    if value is None:
        return b"\x00"
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return b"\x01" + len(data).to_bytes(8, "big") + data


def _digest(fields: Sequence[object]) -> bytes:
    return hashlib.sha256(b"".join(_field(value) for value in fields)).digest()


def record_digest(row: Sequence[Any]) -> bytes:
    """A journal row's digest, over its timestamp, kind, actor, request id and payload bytes:
    the row `(seq, at_utc, kind, actor, request_id, payload)` without its seq."""
    return _digest(row[1:])


def _numeric(row: Sequence[Any]) -> int:
    """A derived row's order in its set: its ordinal `n`, where the table has one."""
    return row[1] if isinstance(row[1], int) else 0


def set_digest(rows: Sequence[Sequence[Any]]) -> bytes:
    """A non-empty derived set's digest, over its size and each row's fields without the seq,
    the rows in their numeric key order."""
    fields: list[object] = [len(rows)]
    for row in sorted(rows, key=_numeric):
        fields.extend(row[1:])
    return _digest(fields)


def config_digest(data: bytes) -> str:
    """A configuration's digest as the live store writes it."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---- coverage of the journal rows


type _Coverage = list[tuple[int, int, str]]


def _overlay(coverage: _Coverage, first: int, last: int, state: str) -> _Coverage:
    """`coverage` with `state` over the seqs `first` to `last`, adjacent equal states joined."""
    kept: _Coverage = []
    for low, high, old in coverage:
        if high < first or low > last:
            kept.append((low, high, old))
            continue
        if low < first:
            kept.append((low, first - 1, old))
        if high > last:
            kept.append((last + 1, high, old))
    return _joined([*kept, (first, last, state)])


def _joined(coverage: _Coverage) -> _Coverage:
    joined: _Coverage = []
    for low, high, state in sorted(coverage):
        if joined and joined[-1][2] == state and joined[-1][1] + 1 == low:
            joined[-1] = (joined[-1][0], high, state)
        else:
            joined.append((low, high, state))
    return joined


def _holes(coverage: _Coverage, first: int, last: int) -> list[tuple[int, int]]:
    """The seqs from `first` to `last` that no interval of `coverage` names."""
    holes, at = [], first
    for low, high, _ in sorted(coverage):
        if high < at:
            continue
        if low > last:
            break
        if low > at:
            holes.append((at, low - 1))
        at = max(at, high + 1)
    if at <= last:
        holes.append((at, last))
    return holes


def _expired(coverage: _Coverage, seqs: Collection[int]) -> _Coverage:
    """`coverage` with each covered seq of `seqs` marked expired; a gap stays one."""
    marked = coverage
    for seq in sorted(seqs):
        if any(low <= seq <= high and state == COVERED for low, high, state in marked):
            marked = _overlay(marked, seq, seq, EXPIRED)
    return marked


# ---- the image


def _text(data: bytes, what: str) -> str:
    """Canonical bytes as the TEXT the archive holds; bytes that are not UTF-8 cannot be held
    verbatim, and the archive stops."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveStopped(f"{what} is not UTF-8 text and cannot be archived: {exc}") from exc


def _under(histories: Sequence[tuple[str, int]], seq: int) -> str | None:
    """The history of the live list a seq lay under: the last in the list's order that begins
    at or before it; None for a seq before every history, read as "line not known"."""
    found = None
    for history, first in histories:
        if first <= seq:
            found = history
    return found


def _applied_rev(kind: str, payload: bytes) -> int | None:
    """The configuration rev a `config applied` record applies, None for any other record."""
    if kind != "config":
        return None
    body = json.loads(payload)
    return int(body["rev"]) if body.get("event") == "applied" else None


#: An observation as the image holds it: seq, history, the row's digest, the three sets (None
#: for no claim, `EMPTY_SET` for a complete empty set, else a set's digest), and the
#: configuration a record applies (rev, digest, binding), all None for any other record.
type _Observation = tuple[
    int,
    str | None,
    bytes,
    bytes | None,
    bytes | None,
    bytes | None,
    int | None,
    str | None,
    str | None,
]


@dataclass(slots=True)
class _Image:
    """One batch, or a round with nothing to copy, as the archive must hold it: the batch's
    journal rows, sets, configurations and observations, its coverage interval, the history
    list and its last seq. That is its whole scope: no list's `recorded_at`, no physical id,
    and no row another writer may have added is in it. Before the transaction's first write
    `Archive._expect` fills `stamps` and `coverage`, the time each item holds (a new item's
    is `stamp`) and the coverage after the write, from the archive as it is then. The read
    back builds the same value from the archive, and the two are compared whole."""

    stamp: str
    root: tuple[str | None, str | None, str | None]
    rows: dict[tuple[int, bytes], tuple[str, str, str | None, str | None, str]]
    sets: dict[tuple[str, int, bytes], tuple[tuple[Any, ...], ...]]
    configs: dict[tuple[int, str], str]
    bound: set[tuple[int, str]]
    observations: set[_Observation]
    histories: list[tuple[str, int]]
    last_seq: int
    span: tuple[int, int] | None
    first_live: int
    stamps: dict[tuple[Any, ...], str] = field(default_factory=dict)
    coverage: _Coverage = field(default_factory=list)


def _image(batch: RawBatch, root: Mapping[str, str], stamp: str) -> _Image:
    """The image of a live snapshot. A derived set is taken only where the live coverage
    claims the seq complete. The configurations are those whose bytes the snapshot read,
    each checked against its digest."""
    digests = dict(batch.configs)
    configs: dict[tuple[int, str], str] = {}
    for number, given, data in batch.config_data:
        if config_digest(data) != given:
            raise ArchiveStopped(f"the live configuration rev {number} does not match its digest")
        configs[(number, given)] = _text(data, f"configuration {number}")
    image = _Image(
        stamp=stamp,
        root=(root.get("state_version"), root.get("estate_name"), root.get("created_at")),
        rows={},
        sets={},
        configs=configs,
        bound=set(),
        observations=set(),
        histories=list(batch.histories),
        last_seq=batch.last_seq,
        span=None,
        first_live=batch.first_seq,
    )
    if not batch.records:
        return image
    low, high = batch.records[0][0], batch.records[-1][0]
    image.span = (low, high)
    claims = {
        "transitions": covered_parts(batch.transitions_coverage, low, high),
        "job_runs": covered_parts(batch.history_coverage, low, high),
        "job_records": covered_parts(batch.history_coverage, low, high),
    }
    live: dict[str, dict[int, list[tuple[Any, ...]]]] = {table: {} for table in SETS}
    for table, found in (
        ("transitions", batch.transitions),
        ("job_runs", batch.job_runs),
        ("job_records", batch.job_records),
    ):
        for row in found:
            live[table].setdefault(int(row[0]), []).append(tuple(row))
    for record in batch.records:
        seq, at, kind, actor, rid, payload = record
        row_key = record_digest(record)
        image.rows[(seq, row_key)] = (at, kind, actor, rid, _text(payload, f"record {seq}"))
        sets: list[bytes | None] = []
        for table in SETS:
            if not any(a <= seq <= b for a, b in claims[table]):
                sets.append(None)
                continue
            held = sorted(live[table].get(seq, []), key=_numeric)
            if not held:
                sets.append(EMPTY_SET)
                continue
            key = set_digest(held)
            sets.append(key)
            image.sets[(table, seq, key)] = tuple(
                (*row[1:4], _text(row[4], f"transition {seq}.{row[1]}"))
                if table == "transitions"
                else tuple(row[1:])
                for row in held
            )
        applied = _applied_rev(kind, payload)
        binding: tuple[int | None, str | None, str | None] = (None, None, None)
        if applied is not None and applied in digests:
            binding = (applied, digests[applied], BOUND)
            image.bound.add((applied, digests[applied]))
        elif applied is not None:
            binding = (applied, None, UNHELD_REV)
        image.observations.add(
            (seq, _under(batch.histories, seq), row_key, sets[0], sets[1], sets[2], *binding)
        )
    return image


def _set_rows(table: str, rows: Sequence[tuple[Any, ...]], seq: int) -> list[tuple[Any, ...]]:
    """A set's rows as the live store holds them, for its digest: the seq first, bytes for an
    entry."""
    if table == "transitions":
        return [(seq, n, job, name, str(entry).encode("utf-8")) for n, job, name, entry in rows]
    return [(seq, *row) for row in rows]


# ---- the file


def _mode_refusal(path: Path) -> str | None:
    """Why an existing archive file, or its `-wal` or `-shm`, may not be written: a mode that
    lets group or others read or write it. The rows hold dispatch environments."""
    for name in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            mode = stat.S_IMODE(os.stat(name).st_mode)
        except FileNotFoundError:
            continue
        if mode & 0o077:
            return (
                f"{name}: mode {mode:04o} lets group or others read or write it; the archive"
                " holds dispatch environments and is written only at mode 0600"
            )
    return None


def _linked(path: Path, links: int) -> str:
    return (
        f"{path}: the archive file has {links} hard links; each name would take a lock of"
        " its own, so the archive is written only through a file with one"
    )


def _admitted(real: Path) -> bool:
    """Under the lock, before SQLite opens it: an existing archive file must be a regular file
    with one hard link, since each name of a file would take a lock of its own; an absent one
    is created exclusively, mode 0600, so its -wal and -shm take that mode too. Answers whether
    the file is new: absent or empty."""
    try:
        found = os.stat(real)
    except FileNotFoundError:
        os.close(os.open(real, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return True
    if not stat.S_ISREG(found.st_mode):
        raise ArchiveStopped(f"{real} is not a regular file")
    if found.st_nlink != 1:
        raise ArchiveStopped(_linked(real, found.st_nlink))
    return found.st_size == 0


def _check_layout(db: sqlite3.Connection, path: Path) -> None:
    """Refuse a file that is not new and not this layout, naming the layout it has, before
    anything in it is changed. No step from another layout exists."""
    found: dict[str, str] = {}
    tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master")}
    if "meta" in tables:
        found = {str(k): str(v) for k, v in db.execute("SELECT key, value FROM meta")}
    if found.get("layout") == ARCHIVE_LAYOUT:
        return
    if "layout" in found:
        name = repr(found["layout"])
    elif "layout_version" in found:
        name = f"layout_version {found['layout_version']}"
    else:
        raise ArchiveStopped(f"{path} is not a dsl41 archive: it names no layout")
    raise ArchiveStopped(f"{path}: archive layout {name}; this build writes {ARCHIVE_LAYOUT} only")


def _stamp(at: datetime) -> str:
    """The fixed-width text the archive's times use, as the live store's do."""
    return at.replace(tzinfo=None).isoformat(timespec="microseconds")


def _read(db: sqlite3.Connection, root: str, image: _Image) -> _Image:
    """The image as the archive holds it, read by the keys of `image`: what is not
    there is absent from the answer."""
    read = _Image(
        stamp=image.stamp,
        root=(None, None, None),
        rows={},
        sets={},
        configs={},
        bound=set(),
        observations=set(),
        histories=[],
        last_seq=-1,
        span=image.span,
        first_live=image.first_live,
    )
    found = db.execute(
        "SELECT state_version, estate_name, created_at, first_archived_at FROM roots"
        " WHERE root_id = ?",
        (root,),
    ).fetchone()
    if found is not None:
        read.root = (found[0], found[1], found[2])
        read.stamps[("root",)] = str(found[3])
    for rev, config in image.configs:
        found = db.execute(
            "SELECT data, archived_at FROM configs WHERE root_id = ? AND rev = ? AND digest = ?",
            (root, rev, config),
        ).fetchone()
        if found is not None:
            read.configs[(rev, config)] = str(found[0])
            read.stamps[("config", rev, config)] = str(found[1])
    for rev, config in image.bound:
        found = db.execute(
            "SELECT data FROM configs WHERE root_id = ? AND rev = ? AND digest = ?",
            (root, rev, config),
        ).fetchone()
        if found is not None and config_digest(str(found[0]).encode("utf-8")) == config:
            read.bound.add((rev, config))
    for seq, row_key in image.rows:
        found = db.execute(
            "SELECT at_utc, kind, actor, request_id, payload, archived_at FROM records"
            " WHERE root_id = ? AND seq = ? AND digest = ?",
            (root, seq, row_key),
        ).fetchone()
        if found is not None:
            read.rows[(seq, row_key)] = (found[0], found[1], found[2], found[3], str(found[4]))
            read.stamps[("row", seq, row_key)] = str(found[5])
    for table, seq, set_key in image.sets:
        size = db.execute(
            "SELECT size FROM sets WHERE root_id = ? AND table_name = ? AND seq = ? AND digest = ?",
            (root, table, seq, set_key),
        ).fetchone()
        if size is None:
            continue
        rows = _stored_rows(db, root, table, seq, set_key)
        if len(rows) == int(size[0]):
            read.sets[(table, seq, set_key)] = rows
    ids = {
        int(hid): str(history)
        for hid, history in db.execute(
            "SELECT id, history FROM histories WHERE root_id = ?", (root,)
        )
    }
    for observation in image.observations:
        history = observation[1]
        hid = next((i for i, h in ids.items() if h == history), None)
        if history is not None and hid is None:
            continue
        stored = db.execute(
            "SELECT 1 FROM observations WHERE root_id = ? AND seq = ? AND history_id IS ?"
            " AND record_digest = ? AND transitions_set IS ? AND job_runs_set IS ?"
            " AND job_records_set IS ? AND config_rev IS ? AND config_digest IS ?"
            " AND config_binding IS ?",
            (root, observation[0], hid, *observation[2:]),
        ).fetchone()
        if stored is not None:
            read.observations.add(observation)
    newest = db.execute(
        "SELECT version, last_seq FROM lists WHERE root_id = ? ORDER BY version DESC LIMIT 1",
        (root,),
    ).fetchone()
    if newest is not None:
        read.last_seq = int(newest[1])
        read.histories = [
            (ids.get(int(hid), "?"), int(first))
            for hid, first in db.execute(
                "SELECT history_id, first_seq FROM list_entries WHERE root_id = ? AND version = ?"
                " ORDER BY position",
                (root, int(newest[0])),
            )
        ]
    read.coverage = [
        (int(low), int(high), str(state))
        for low, high, state in db.execute(
            "SELECT first_seq, last_seq, state FROM coverage WHERE root_id = ? ORDER BY first_seq",
            (root,),
        )
    ]
    return read


def _stored_rows(
    db: sqlite3.Connection, root: str, table: str, seq: int, digest: bytes
) -> tuple[tuple[Any, ...], ...]:
    """A set's stored rows, in their numeric key order, in the image's shape."""
    key = (root, seq, digest)
    if table == "transitions":
        query = (
            "SELECT n, job, transition, entry FROM transitions"
            " WHERE root_id = ? AND seq = ? AND digest = ? ORDER BY n"
        )
    elif table == "job_runs":
        query = (
            "SELECT n, job, run_number, rev FROM job_runs"
            " WHERE root_id = ? AND seq = ? AND digest = ? ORDER BY n"
        )
    else:
        query = (
            "SELECT job, run_number FROM job_records WHERE root_id = ? AND seq = ? AND digest = ?"
        )
    return tuple(tuple(row) for row in db.execute(query, key))


def _difference(image: _Image, read: _Image) -> str | None:
    """The first part of the image the archive does not hold as given, or None."""
    for part in dataclasses.fields(_Image):
        if getattr(image, part.name) != getattr(read, part.name):
            return part.name
    return None


class Archive:
    """The archive file opened for one round, with its lock, by the worker thread only.

    The lock is a `flock` on the sidecar beside the database's resolved path, so two
    symbolic names of one file take one lock. The round holds it from the open to the close.
    Under the lock, before SQLite opens the file, `_admitted` and `_mode_refusal` check it."""

    def __init__(self, path: Path, root: Mapping[str, str], now: Callable[[], datetime]) -> None:
        self.path = path
        self.root = root["root_id"]
        self.meta = dict(root)
        self._now = now
        self.real = Path(os.path.realpath(path))
        self._lock_fd = self._lock(self.real)
        try:
            new = _admitted(self.real)
            refused = _mode_refusal(self.real)
            if refused is not None:
                raise ArchiveStopped(refused)
            self._db = self._open(self.real, new)
        except BaseException:
            os.close(self._lock_fd)
            raise

    @staticmethod
    def _lock(real: Path) -> int:
        fd = os.open(f"{real}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise ArchiveStopped(
                    "another engine holds the archive, or an operator holds its lock"
                ) from exc
            raise
        return fd

    @staticmethod
    def _open(path: Path, new: bool) -> sqlite3.Connection:
        db = sqlite3.connect(path, isolation_level=None, timeout=0)
        try:
            db.execute("PRAGMA busy_timeout=0")
            if new:
                # before any table exists, or SQLite ignores it
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            else:
                _check_layout(db, path)
            mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode != "wal":
                raise ArchiveStopped(f"{path}: journal_mode is {mode!r}, the archive needs WAL")
            db.execute("PRAGMA synchronous=FULL")
            if sys.platform == "darwin":  # fsync alone does not reach the platters there
                db.execute("PRAGMA fullfsync=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('layout', ?)",
                    (ARCHIVE_LAYOUT,),
                )
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        try:
            self._db.close()
        finally:
            os.close(self._lock_fd)  # closing the descriptor gives the flock back

    def created(self) -> str | None:
        """When this root's first copy came to this file (`roots.first_archived_at`), None
        before it: what tells one archive file from another within a run."""
        row = self._db.execute(
            "SELECT first_archived_at FROM roots WHERE root_id = ?", (self.root,)
        ).fetchone()
        return None if row is None else str(row[0])

    def histories(self) -> int:
        """The count of this root's histories."""
        (count,) = self._db.execute(
            "SELECT COUNT(*) FROM histories WHERE root_id = ?", (self.root,)
        ).fetchone()
        return int(count)

    def _transaction(self, write: Callable[[], None]) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            write()
            self._db.execute("COMMIT")
        except BaseException:
            # a statement that failed may have ended the transaction; the first error is the one
            # reported
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    # ---- writing an image

    def take(self, image: _Image) -> None:
        """The image's one transaction: complete the expected image from the archive as it
        is before any write, write, read it back by its keys, compare the two whole, and
        commit. A difference raises `ArchiveStopped`, and nothing of the image stays."""

        def write() -> None:
            self._expect(image)
            self._write(image)
            self._compare(self._db, image, "inside the transaction")

        self._transaction(write)

    def check(self, image: _Image) -> None:
        """After the commit, on a connection opened now: read the image back and
        compare it whole."""
        db = sqlite3.connect(f"{self.real.as_uri()}?mode=ro", uri=True)
        try:
            self._compare(db, image, "after the commit")
        finally:
            db.close()

    def _compare(self, db: sqlite3.Connection, image: _Image, when: str) -> None:
        part = _difference(image, _read(db, self.root, image))
        if part is not None:
            raise ArchiveStopped(
                f"the archive does not read back as the batch's image {when}: its {part}"
                f" differ, for root {self.root}; pruning stops"
            )

    def _damaged(self, what: str, expected: str, found: str) -> ArchiveStopped:
        """A stored item whose bytes do not give its digest: a damaged archive. Nothing is
        rewritten, since a mismatch cannot tell a damaged key from a damaged body."""
        return ArchiveStopped(
            f"the archive's {what} is damaged: expected digest {expected}, found {found},"
            f" for root {self.root}; pruning stops"
        )

    def _stored_text(self, value: object, what: str) -> str:
        if not isinstance(value, str):
            raise ArchiveStopped(
                f"the archive's {what} cannot be read as text, for root {self.root}; pruning stops"
            )
        return value

    def _expect(self, image: _Image) -> None:
        """Complete the expected image before the transaction's first write: the time each
        item holds (a new item's is the image's stamp) and the coverage after the write, from
        the live snapshot and the archive as it is now. A stored item of the image whose bytes
        do not give its digest, whose text cannot be read, or a set whose rows are there
        without its header, stops the worker."""
        stamp = image.stamp
        found = self._db.execute(
            "SELECT first_archived_at FROM roots WHERE root_id = ?", (self.root,)
        ).fetchone()
        image.stamps[("root",)] = stamp if found is None else str(found[0])
        for rev, config in sorted({*image.configs, *image.bound}):
            what = f"configs row at rev {rev}"
            stored = self._db.execute(
                "SELECT data, archived_at FROM configs WHERE root_id = ? AND rev = ?"
                " AND digest = ?",
                (self.root, rev, config),
            ).fetchone()
            if stored is not None:
                held = config_digest(self._stored_text(stored[0], what).encode("utf-8"))
                if held != config:
                    raise self._damaged(what, config, held)
            if (rev, config) in image.configs:
                image.stamps[("config", rev, config)] = stamp if stored is None else str(stored[1])
        for seq, row_key in image.rows:
            what = f"records row at seq {seq}"
            stored = self._db.execute(
                "SELECT at_utc, kind, actor, request_id, payload, archived_at FROM records"
                " WHERE root_id = ? AND seq = ? AND digest = ?",
                (self.root, seq, row_key),
            ).fetchone()
            if stored is not None:
                payload = self._stored_text(stored[4], what).encode("utf-8")
                held_key = record_digest((seq, *stored[:4], payload))
                if held_key != row_key:
                    raise self._damaged(what, row_key.hex(), held_key.hex())
            image.stamps[("row", seq, row_key)] = stamp if stored is None else str(stored[5])
        for table, seq, set_key in image.sets:
            what = f"{table} set at seq {seq}"
            header = self._db.execute(
                "SELECT size FROM sets WHERE root_id = ? AND table_name = ? AND seq = ?"
                " AND digest = ?",
                (self.root, table, seq, set_key),
            ).fetchone()
            rows = _stored_rows(self._db, self.root, table, seq, set_key)
            if header is None and rows:
                raise ArchiveStopped(
                    f"the archive's {what} has rows and no header, for root {self.root};"
                    " pruning stops"
                )
            if header is not None:
                if table == "transitions":
                    for row in rows:
                        self._stored_text(row[3], what)
                held_key = set_digest(_set_rows(table, rows, seq))
                if held_key != set_key or len(rows) != int(header[0]):
                    raise self._damaged(what, set_key.hex(), held_key.hex())
        coverage = self._coverage()
        if image.span is not None:
            coverage = _overlay(coverage, *image.span, COVERED)
        for low, high in _holes(coverage, 1, image.first_live - 1):
            coverage = _overlay(coverage, low, high, GAP)
        image.coverage = coverage

    def _write(self, image: _Image) -> None:
        """Write what the archive lacks of `image`, completed by `_expect`; an item stored
        under its key is left as it is."""
        stamp = image.stamp
        if image.stamps[("root",)] == stamp:
            self._db.execute(
                "INSERT OR IGNORE INTO roots(root_id, state_version, estate_name, created_at,"
                " first_archived_at) VALUES (?, ?, ?, ?, ?)",
                (self.root, *image.root, stamp),
            )
        self._write_list(image)
        for (rev, config), data in image.configs.items():
            self._db.execute(
                "INSERT OR IGNORE INTO configs(root_id, rev, digest, data, archived_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.root, rev, config, data, stamp),
            )
        for (seq, row_key), fields in image.rows.items():
            self._db.execute(
                "INSERT OR IGNORE INTO records(root_id, seq, digest, at_utc, kind, actor,"
                " request_id, payload, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.root, seq, row_key, *fields, stamp),
            )
        for (table, seq, set_key), rows in image.sets.items():
            if (
                self._db.execute(
                    "SELECT 1 FROM sets WHERE root_id = ? AND table_name = ? AND seq = ?"
                    " AND digest = ?",
                    (self.root, table, seq, set_key),
                ).fetchone()
                is None
            ):
                self._insert_set(table, seq, set_key, rows)
        ids = self._history_ids([history for history, _ in image.histories])
        for observation in image.observations:
            history = observation[1]
            hid = None if history is None else ids[history]
            stored = self._db.execute(
                "SELECT 1 FROM observations WHERE root_id = ? AND seq = ? AND history_id IS ?"
                " AND record_digest = ? AND transitions_set IS ? AND job_runs_set IS ?"
                " AND job_records_set IS ? AND config_rev IS ? AND config_digest IS ?"
                " AND config_binding IS ?",
                (self.root, observation[0], hid, *observation[2:]),
            ).fetchone()
            if stored is None:
                self._db.execute(
                    "INSERT INTO observations(root_id, seq, history_id, record_digest,"
                    " transitions_set, job_runs_set, job_records_set, config_rev,"
                    " config_digest, config_binding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.root, observation[0], hid, *observation[2:]),
                )
        if image.coverage != self._coverage():
            self._set_coverage(image.coverage)

    def _insert_set(
        self, table: str, seq: int, digest: bytes, rows: Sequence[tuple[Any, ...]]
    ) -> None:
        self._db.execute(
            "INSERT INTO sets(root_id, table_name, seq, digest, size) VALUES (?, ?, ?, ?, ?)",
            (self.root, table, seq, digest, len(rows)),
        )
        if table == "transitions":
            statement = (
                "INSERT INTO transitions(root_id, seq, digest, n, job, transition, entry)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
        elif table == "job_runs":
            statement = (
                "INSERT INTO job_runs(root_id, seq, digest, n, job, run_number, rev)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
        else:
            statement = (
                "INSERT INTO job_records(root_id, seq, digest, job, run_number)"
                " VALUES (?, ?, ?, ?, ?)"
            )
        self._db.executemany(statement, [(self.root, seq, digest, *row) for row in rows])

    def _history_ids(self, histories: Sequence[str]) -> dict[str, int]:
        """Each history with its id here; a history seen first is given the next id."""
        ids: dict[str, int] = {}
        for history in histories:
            row = self._db.execute(
                "SELECT id FROM histories WHERE root_id = ? AND history = ?", (self.root, history)
            ).fetchone()
            if row is None:
                (top,) = self._db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM histories WHERE root_id = ?", (self.root,)
                ).fetchone()
                self._db.execute(
                    "INSERT INTO histories(root_id, id, history) VALUES (?, ?, ?)",
                    (self.root, int(top) + 1, history),
                )
                row = (int(top) + 1,)
            ids[history] = int(row[0])
        return ids

    def _write_list(self, image: _Image) -> None:
        """The live store's history list and last seq. A list that extends the newest
        version (the same entries, more at the end) replaces it in place; any other is a new
        version, a record that the line changed under the archive: a restore, or a history
        given afterwards to a start that had recorded none. Earlier versions are kept."""
        ids = self._history_ids([history for history, _ in image.histories])
        listed = [(ids[history], first) for history, first in image.histories]
        row = self._db.execute(
            "SELECT version FROM lists WHERE root_id = ? ORDER BY version DESC LIMIT 1",
            (self.root,),
        ).fetchone()
        entries: list[tuple[int, int]] = []
        if row is not None:
            entries = [
                (int(hid), int(first))
                for hid, first in self._db.execute(
                    "SELECT history_id, first_seq FROM list_entries WHERE root_id = ?"
                    " AND version = ? ORDER BY position",
                    (self.root, int(row[0])),
                )
            ]
        if row is not None and listed[: len(entries)] == entries:
            version = int(row[0])
            self._db.execute(
                "UPDATE lists SET last_seq = ? WHERE root_id = ? AND version = ?",
                (image.last_seq, self.root, version),
            )
            added = listed[len(entries) :]
            start = len(entries)
        else:
            version = 1 if row is None else int(row[0]) + 1
            self._db.execute(
                "INSERT INTO lists(root_id, version, recorded_at, last_seq) VALUES (?, ?, ?, ?)",
                (self.root, version, image.stamp, image.last_seq),
            )
            added, start = listed, 0
        self._db.executemany(
            "INSERT INTO list_entries(root_id, version, position, history_id, first_seq)"
            " VALUES (?, ?, ?, ?, ?)",
            [(self.root, version, start + n, hid, first) for n, (hid, first) in enumerate(added)],
        )

    def _coverage(self) -> _Coverage:
        return [
            (int(low), int(high), str(state))
            for low, high, state in self._db.execute(
                "SELECT first_seq, last_seq, state FROM coverage WHERE root_id = ?"
                " ORDER BY first_seq",
                (self.root,),
            )
        ]

    def _set_coverage(self, coverage: _Coverage) -> None:
        self._db.execute("DELETE FROM coverage WHERE root_id = ?", (self.root,))
        self._db.executemany(
            "INSERT INTO coverage(root_id, first_seq, last_seq, state) VALUES (?, ?, ?, ?)",
            [(self.root, low, high, state) for low, high, state in coverage],
        )

    # ---- expiry and compaction

    def expire(
        self,
        days: int,
        first_live: int,
        live_configs: Collection[tuple[int, str]],
        limit: int,
        going_on: Callable[[], bool],
    ) -> int:
        """Expiry on row variants: delete this root's journal row variants archived more
        than `days` ago, the engine's current `archive_days`, and below `first_live`, the live
        store's first seq read now, so a copy that backs a delete still owed stays; at most
        `limit` variants a transaction, while `going_on` holds. Each goes with its
        observations; then the sets and histories nothing refers to any more. A configuration
        goes when it was archived more than `days` ago, the live store holds no such rev with
        that digest, and no observation refers to it. The rows of any other
        root are never expired here. Answers the variants deleted."""
        cutoff = _stamp(self._now() - timedelta(days=int(days)))
        deleted = 0
        while going_on():
            keys = [
                (int(seq), bytes(digest))
                for seq, digest in self._db.execute(
                    "SELECT seq, digest FROM records WHERE root_id = ? AND seq < ?"
                    " AND archived_at < ? ORDER BY seq, digest LIMIT ?",
                    (self.root, first_live, cutoff, int(limit)),
                )
            ]
            if not keys:
                break
            self._transaction(functools.partial(self._expire_variants, keys))
            deleted += len(keys)
            if len(keys) < limit:
                break
        if going_on():
            stale = [
                (int(rev), str(digest))
                for rev, digest in self._db.execute(
                    "SELECT rev, digest FROM configs WHERE root_id = ? AND archived_at < ?",
                    (self.root, cutoff),
                )
                if (int(rev), str(digest)) not in live_configs
                and self._db.execute(
                    "SELECT 1 FROM observations WHERE root_id = ? AND config_rev = ?"
                    " AND config_digest = ? LIMIT 1",
                    (self.root, rev, digest),
                ).fetchone()
                is None
            ]
            if stale:
                self._transaction(functools.partial(self._expire_configs, stale))
                deleted += len(stale)
        return deleted

    def _expire_variants(self, keys: Sequence[tuple[int, bytes]]) -> None:
        """Inside a transaction: the variants, their observations, the sets and histories
        nothing refers to any more, and the coverage of seqs with no variant left."""
        for seq, digest in keys:
            self._db.execute(
                "DELETE FROM records WHERE root_id = ? AND seq = ? AND digest = ?",
                (self.root, seq, digest),
            )
            self._db.execute(
                "DELETE FROM observations WHERE root_id = ? AND seq = ? AND record_digest = ?",
                (self.root, seq, digest),
            )
        for seq in sorted({seq for seq, _ in keys}):
            for table in SETS:
                for (digest,) in self._db.execute(
                    "SELECT digest FROM sets WHERE root_id = ? AND table_name = ? AND seq = ?",
                    (self.root, table, seq),
                ).fetchall():
                    if (
                        self._db.execute(
                            f"SELECT 1 FROM observations WHERE root_id = ? AND seq = ?"
                            f" AND {table}_set = ? LIMIT 1",
                            (self.root, seq, digest),
                        ).fetchone()
                        is None
                    ):
                        self._db.execute(
                            f"DELETE FROM {table} WHERE root_id = ? AND seq = ? AND digest = ?",
                            (self.root, seq, digest),
                        )
                        self._db.execute(
                            "DELETE FROM sets WHERE root_id = ? AND table_name = ? AND seq = ?"
                            " AND digest = ?",
                            (self.root, table, seq, digest),
                        )
        self._db.execute(
            "DELETE FROM histories WHERE root_id = ?"
            " AND id NOT IN (SELECT history_id FROM list_entries WHERE root_id = ?)"
            " AND id NOT IN (SELECT history_id FROM observations WHERE root_id = ?"
            " AND history_id IS NOT NULL)",
            (self.root, self.root, self.root),
        )
        emptied = [
            seq
            for seq in sorted({seq for seq, _ in keys})
            if self._db.execute(
                "SELECT 1 FROM records WHERE root_id = ? AND seq = ? LIMIT 1", (self.root, seq)
            ).fetchone()
            is None
        ]
        if emptied:
            self._set_coverage(_expired(self._coverage(), emptied))

    def _expire_configs(self, keys: Sequence[tuple[int, str]]) -> None:
        for rev, digest in keys:
            self._db.execute(
                "DELETE FROM configs WHERE root_id = ? AND rev = ? AND digest = ?",
                (self.root, rev, digest),
            )

    def compact(self) -> None:
        """Give the freed pages back to the file system: the incremental vacuum, read to its
        end, then the planner's statistics, then a checkpoint that truncates the log, which
        does nothing while a reader holds it."""
        self._db.execute("PRAGMA incremental_vacuum").fetchall()
        self._db.execute("PRAGMA optimize")
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


class ArchiveWorker:
    """The archive's one thread for an engine run (DL-545). The loop asks for a round with
    `request`; the thread opens the archive, copies and verifies from the live store's first
    seq up to the round's target, expires and compacts, closes the archive, and hands each
    report to the loop with `call_soon_threadsafe`. The thread starts at the first request,
    in the loop that made it.

    State shared with the loop is the request, the stop flag and `idle`, all under one
    condition. Everything else belongs to the thread."""

    def __init__(
        self,
        live: Path,
        path: Path,
        *,
        root: Mapping[str, str],
        days: int,
        step: int,
        now: Callable[[], datetime],
        report: Callable[[Report], None],
    ) -> None:
        self.live = live
        self.path = path
        self._root = dict(root)
        self._days = int(days)
        self._step = int(step)
        self._now = now
        self._report = report
        self._cond = threading.Condition()
        self._wanted: tuple[int, int] | None = None
        self._stopping = False
        #: set while the thread has no round to work and has handed over every report; for a
        #: caller that must know that
        self.idle = threading.Event()
        self.idle.set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._store: SqliteStore | None = None
        #: the configurations, by rev and digest, stored and read back in this run, and the
        #: archive file they were read back in; another file reads them again
        self._configs: tuple[str | None, set[tuple[int, str]]] = (None, set())

    # ---- the loop's side

    def request(self, round: int, target: int) -> None:
        """Ask for round `round`: copy and verify below seq `target`. A request the thread has
        not begun is replaced; a round in progress stops after its batch."""
        with self._cond:
            if self._stopping:
                return
            self._wanted = (int(round), int(target))
            self.idle.clear()
            self._cond.notify()
        if self._thread is None:
            self._loop = asyncio.get_running_loop()
            self._thread = threading.Thread(target=self._main, name="dsl41-archive", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end after the batch it works on; it takes no new step."""
        with self._cond:
            self._stopping = True
            self._wanted = None
            self._cond.notify()

    def join(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the thread to end; True when it did or never ran."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # ---- the thread's side

    def _post(self, report: Report) -> None:
        assert self._loop is not None
        with contextlib.suppress(RuntimeError):  # a closed loop has nobody to tell
            self._loop.call_soon_threadsafe(self._report, report)

    def _going_on(self) -> bool:
        """Whether the round may take its next step: no stop, and no newer round asked."""
        with self._cond:
            return not self._stopping and self._wanted is None

    def _main(self) -> None:
        try:
            while True:
                with self._cond:
                    while self._wanted is None and not self._stopping:
                        self.idle.set()
                        self._cond.wait()
                    if self._stopping:
                        return
                    assert self._wanted is not None
                    round, target = self._wanted
                    self._wanted = None
                self._round(round, target)
        finally:
            if self._store is not None:
                self._store.close()
                self._store = None
            self.idle.set()

    def _round(self, round: int, target: int) -> None:
        """One round with the archive open and locked from start to end. Each round opens
        the configured path again, so a cause of a stop the operator removed (a mode, a lock
        held elsewhere, a full disk) lets the next round work."""
        archive: Archive | None = None
        stopped: str | None = None
        try:
            if self._store is None:
                self._store = SqliteStore(self.live, read_only=True)
            archive = Archive(self.path, self._root, self._now)
            self._work(round, target, self._store, archive)
        except Exception as exc:  # noqa: BLE001 -- any failure stops the archive, not the engine
            stopped = (
                str(exc) if isinstance(exc, ArchiveStopped) else f"{type(exc).__name__}: {exc}"
            )
        finally:
            if archive is not None:
                with contextlib.suppress(sqlite3.Error, OSError):
                    archive.close()
        if stopped is not None:
            self._post(Report(round, stopped=stopped))  # the file is closed and unlocked now

    def _known(self, archive: Archive) -> Collection[tuple[int, str]]:
        """The configurations this run stored and read back in this file."""
        if self._configs[0] != archive.created():
            self._configs = (archive.created(), set())
        return self._configs[1]

    def _work(self, round: int, target: int, store: SqliteStore, archive: Archive) -> None:
        after: int | None = None
        start: int | None = None
        while self._going_on():
            known = self._known(archive)
            batch = store.raw_batch(
                store.first_seq() if after is None else after, target, self._step, known
            )
            image = _image(batch, archive.meta, _stamp(self._now()))
            archive.take(image)
            # committed: read again on a connection opened now, and only that makes a report
            archive.check(image)
            self._configs = (archive.created(), {*known, *image.configs})
            if image.span is None:
                break  # a round's last read, or one with nothing to copy: its image is verified
            if start is None:
                start = image.span[0]
            # the round's range so far: it begins at the live store's first seq as read at
            # the round's start, which only the loop's deletes of verified rows move up
            self._post(Report(round, verified=(start, image.span[1])))
            after = image.span[1] + 1
        else:
            return  # a stopped or superseded round expires nothing and completes nothing
        if not self._going_on():
            return
        expired = archive.expire(
            self._days, store.first_seq(), set(batch.configs), self._step, self._going_on
        )
        if expired and self._going_on():
            archive.compact()
        if self._going_on():
            # said last: the whole round, expiry included, completed
            self._post(
                Report(
                    round, completed=True, histories=archive.histories(), last_seq=image.last_seq
                )
            )
