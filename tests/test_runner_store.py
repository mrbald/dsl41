"""The runner store (slice 1a): one suite over `SqliteStore` and `MemoryStore`, plus the
SQLite-only checks of its tables, its busy rule and its crash safety."""

from __future__ import annotations

import heapq
import itertools
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nightbank_props import compute_properties, write_kv
from sealed_state import SealedState, _canon_ready

from dsl41.ast_jil import parse
from dsl41.canon import CanonError, canonical_bytes, decode, digest
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event, TraceEntry
from dsl41.placeholders import load_properties, substitute
from dsl41.runner_clock import EngineError
from dsl41.runner_config import Configuration, RuntimeProfile
from dsl41.runner_store import MemoryStore, Record, SqliteStore, Store, StoreError

ROOT = Path(__file__).resolve().parent.parent
NB = ROOT / "examples" / "nightbank"
KINDS = ["sqlite", "memory"]
T0 = datetime(2026, 1, 6, 0, 0)


@contextmanager
def open_store(kind: str, directory: Path) -> Iterator[Store]:
    store: Store = SqliteStore(directory / "store.db") if kind == "sqlite" else MemoryStore()
    try:
        yield store
    finally:
        store.close()


def rec(
    n: int = 0,
    *,
    kind: str = "input",
    request_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    at: datetime | None = None,
) -> Record:
    return Record(at or T0 + timedelta(seconds=n), kind, "tester", request_id, payload or {"n": n})


# ------------------------------------------------------------------ round trip

json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**70), 2**70) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)
payloads = st.dictionaries(st.text(), json_values, max_size=4).filter(
    lambda p: "artifact_format_version" not in p
)
instants = st.datetimes(min_value=datetime(1970, 1, 1), max_value=datetime(2200, 1, 1))
texts = st.text(max_size=12)


@st.composite
def record_lists(draw: st.DrawFn) -> list[Record]:
    count = draw(st.integers(0, 6))
    ids = draw(st.lists(texts, min_size=count, max_size=count, unique=True))
    out = []
    for request_id in ids:
        out.append(
            Record(
                draw(instants),
                draw(st.text(min_size=1, max_size=12)),
                draw(st.none() | texts),
                draw(st.none() | st.just(request_id)),
                draw(payloads),
            )
        )
    return out


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(first=record_lists(), second=record_lists())
def test_append_and_read_from_round_trip(
    kind: str, first: list[Record], second: list[Record]
) -> None:
    second = [r for r in second if r.request_id is None or r.request_id not in _ids(first)]
    with tempfile.TemporaryDirectory() as tmp, open_store(kind, Path(tmp)) as store:
        assert store.last_seq() == 0
        assert store.append(*first) == len(first)
        assert store.append(*second) == len(first) + len(second)
        expected = first + second
        got = list(store.read_from(0))
        assert [seq for seq, _ in got] == list(range(1, len(expected) + 1))
        for (_, back), sent in zip(got, expected, strict=True):
            assert back.at_utc == sent.at_utc
            assert (back.kind, back.actor, back.request_id) == (
                sent.kind,
                sent.actor,
                sent.request_id,
            )
            assert dict(back.payload) == dict(sent.payload)
            assert canonical_bytes(back.payload) == canonical_bytes(sent.payload)
        assert store.last_seq() == len(expected)
        for seq, sent in enumerate(expected, start=1):
            assert [s for s, _ in store.read_from(seq - 1)][:1] == [seq]
            if sent.request_id is not None:
                found = store.find_request(sent.request_id)
                assert found is not None
                assert canonical_bytes(found.payload) == canonical_bytes(sent.payload)


def _ids(records: list[Record]) -> set[str]:
    return {r.request_id for r in records if r.request_id is not None}


@pytest.mark.parametrize("kind", KINDS)
def test_read_from_is_exclusive_and_stable_under_appends(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n) for n in range(1, 1201)))  # more than one read chunk
        assert [s for s, _ in store.read_from(0)] == list(range(1, 1201))
        assert [s for s, _ in store.read_from(1199)] == [1200]
        assert list(store.read_from(1200)) == []
        assert store.last_seq() == 1200


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("rows", [3, 1200])  # inside one read chunk, and across several
def test_read_from_is_live(kind: str, rows: int, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n) for n in range(1, rows + 1)))
        reader = store.read_from(0)
        seen = [next(reader)[0]]
        store.append(rec(rows + 1))  # a write between two reads of one iterator
        seen += [s for s, _ in reader]
        assert seen == list(range(1, rows + 2))
        store.append(rec(rows + 2))  # after the iterator ended it stays ended
        assert list(reader) == []


@pytest.mark.parametrize("kind", KINDS)
def test_read_from_follows_a_prune_during_the_read(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n, at=T0 + timedelta(days=n)) for n in range(1, 11)))
        store.put_snapshot(8, 1, b"s")
        reader = store.read_from(0)
        assert next(reader)[0] == 1
        store.prune_before(8, T0 + timedelta(days=100))  # rows 1..7 go
        assert [s for s, _ in reader][-3:] == [8, 9, 10]


@pytest.mark.parametrize("kind", KINDS)
def test_aware_instants_are_stored_as_naive_utc(kind: str, tmp_path: Path) -> None:
    zone = timezone_plus_two()
    with open_store(kind, tmp_path) as store:
        store.append(rec(at=datetime(2026, 1, 6, 12, 0, tzinfo=zone)))
        ((_, back),) = store.read_from(0)
        assert back.at_utc == datetime(2026, 1, 6, 10, 0)
        assert back.at_utc.tzinfo is None


def timezone_plus_two() -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=2))


# ------------------------------------------------------------------ requests, refusals


@pytest.mark.parametrize("kind", KINDS)
def test_find_request_returns_the_first_record_and_a_retry_applies_nothing(
    kind: str, tmp_path: Path
) -> None:
    with open_store(kind, tmp_path) as store:
        assert store.find_request("req-1") is None
        store.append(rec(1, request_id="req-1", payload={"v": "first"}), rec(2))
        retry = rec(3, request_id="req-1", payload={"v": "second"})
        with pytest.raises(StoreError):
            store.append(rec(4), retry)  # the whole batch is refused, not just the retry
        assert store.last_seq() == 2
        assert [s for s, _ in store.read_from(0)] == [1, 2]
        found = store.find_request("req-1")
        assert found is not None
        assert dict(found.payload) == {"v": "first"}
        store.append(rec(5, request_id="req-2"))
        assert store.last_seq() == 3
        with pytest.raises(StoreError):
            store.append(rec(6, request_id="dup"), rec(7, request_id="dup"))
        assert store.find_request("dup") is None
        assert store.last_seq() == 3


@pytest.mark.parametrize("kind", KINDS)
def test_records_without_a_request_id_may_repeat(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        assert store.append(rec(1), rec(1), rec(1)) == 3


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "payload",
    [{"x": 1.5}, {"x": [{"y": float("nan")}]}, {"x": {1, 2}}, {"x": b"b"}, {"x": "\ud800"}],
    ids=["float", "nested-nan", "set", "bytes", "lone-surrogate"],
)
def test_a_payload_canon_refuses_is_never_stored(
    kind: str, payload: dict[str, Any], tmp_path: Path
) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(rec(1))
        with pytest.raises(CanonError):
            store.append(rec(2), rec(3, payload=payload))  # the good record goes with it
        assert store.last_seq() == 1
        assert [s for s, _ in store.read_from(0)] == [1]


@pytest.mark.parametrize("kind", KINDS)
def test_a_payload_that_could_not_be_read_back_is_refused(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        with pytest.raises(CanonError):
            store.append(rec(1, payload={"artifact_format_version": 2}))
        with pytest.raises(CanonError):
            store.append(rec(1, payload=[1, 2]))  # type: ignore[arg-type]
        assert store.last_seq() == 0


class _TwiceKeyed(Mapping[str, int]):
    """A mapping that yields one key twice, so its canonical bytes carry a duplicate key."""

    def __getitem__(self, key: str) -> int:
        return 1

    def __iter__(self) -> Iterator[str]:
        return iter(["k", "k"])

    def __len__(self) -> int:
        return 2


@pytest.mark.parametrize("kind", KINDS)
def test_a_payload_whose_bytes_decode_refuses_is_never_stored(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(rec(1))
        with pytest.raises(CanonError):
            store.append(rec(2, payload=_TwiceKeyed()))
        assert store.last_seq() == 1
        assert [s for s, _ in store.read_from(0)] == [1]  # the journal still reads


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "\ud800"},
        {"kind": None},
        {"kind": ""},
        {"kind": 123},
        {"actor": "\ud800"},
        {"actor": 5},
        {"request_id": "\ud800"},
        {"request_id": 5},
    ],
    ids=[
        "kind-surrogate",
        "kind-none",
        "kind-empty",
        "kind-int",
        "actor-surr",
        "actor-int",
        "id-surr",
        "id-int",
    ],
)
def test_header_fields_are_scalar_strings_in_both_stores(
    kind: str, bad: dict[str, Any], tmp_path: Path
) -> None:
    fields: dict[str, Any] = {"kind": "input", "actor": None, "request_id": None}
    fields.update(bad)
    with open_store(kind, tmp_path) as store:
        with pytest.raises(CanonError):
            store.append(
                rec(1), Record(T0, fields["kind"], fields["actor"], fields["request_id"], {})
            )
        assert store.last_seq() == 0
        store.append(Record(T0, "k", None, None, {}))  # None is fine for actor and request_id


@pytest.mark.parametrize("kind", KINDS)
def test_a_payload_may_carry_a_datetime(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(rec(1, payload={"due": datetime(2026, 1, 6, 1, 2, 3, 4)}))
        ((_, back),) = store.read_from(0)
        assert back.payload == {"due": "2026-01-06T01:02:03.000004"}


# ------------------------------------------------------------------ configs, snapshots


@pytest.mark.parametrize("kind", KINDS)
def test_configs_are_an_append_only_history_and_a_revert_is_a_new_rev(
    kind: str, tmp_path: Path
) -> None:
    with open_store(kind, tmp_path) as store:
        assert store.current_config_rev() == 0
        assert store.find_config("sha256:a") is None
        assert store.put_config(b"alpha", "sha256:a") == 1
        assert store.put_config(b"beta", "sha256:b") == 2
        assert store.find_config("sha256:a") == 1
        assert store.put_config(b"alpha", "sha256:a") == 3  # a revert: A, B, A
        assert store.current_config_rev() == 3
        assert store.find_config("sha256:a") == 3  # the latest rev holding it
        assert store.find_config("sha256:b") == 2
        assert store.find_config("sha256:c") is None
        assert [store.get_config(rev) for rev in (1, 2, 3)] == [b"alpha", b"beta", b"alpha"]
        for rev in (0, 4, -1):
            with pytest.raises(KeyError):
                store.get_config(rev)


@pytest.mark.parametrize("kind", KINDS)
def test_snapshots_latest_wins_and_a_seq_is_stored_once(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        assert store.latest_snapshot() is None
        store.append(*(rec(n) for n in range(1, 6)))
        store.put_snapshot(2, 7, b"two")
        store.put_snapshot(5, 7, b"five")
        store.put_snapshot(3, 7, b"three")  # taken later, at a lower seq: seq decides
        latest = store.latest_snapshot()
        assert latest is not None
        assert (latest.seq, latest.state_version, latest.data) == (5, 7, b"five")
        with pytest.raises(StoreError):
            store.put_snapshot(5, 8, b"again")
        again = store.latest_snapshot()
        assert again is not None and again.data == b"five"


@pytest.mark.parametrize("kind", KINDS)
def test_a_snapshot_above_the_journal_is_refused(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        with pytest.raises(StoreError):
            store.put_snapshot(1, 1, b"empty journal")
        store.append(*(rec(n) for n in range(1, 4)))
        with pytest.raises(StoreError):
            store.put_snapshot(4, 1, b"beyond")
        assert store.latest_snapshot() is None
        store.put_snapshot(3, 1, b"at the end")  # the last seq itself is allowed


# ------------------------------------------------------------------ retention


@pytest.mark.parametrize("kind", KINDS)
def test_prune_before_keeps_the_snapshot_seq_and_what_is_newer(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n, request_id=f"r{n}", at=T0 + timedelta(days=n)) for n in range(1, 11)))
        store.put_snapshot(6, 1, b"state")
        store.put_config(b"cfg", "sha256:c")
        cutoff = T0 + timedelta(days=100)  # every row is older than this
        # the caller names a bound beyond the snapshot; the snapshot's seq still stands
        assert store.prune_before(10, cutoff) == 5
        assert [s for s, _ in store.read_from(0)] == [6, 7, 8, 9, 10]
        latest = store.latest_snapshot()
        assert latest is not None and latest.seq == 6
        assert store.find_request("r5") is None  # a pruned row's request id is free again
        assert store.find_request("r6") is not None
        assert store.get_config(1) == b"cfg"
        assert store.prune_before(10, cutoff) == 0  # idempotent
        assert store.append(rec(11, request_id="r5")) == 11  # seqs are not reused


@pytest.mark.parametrize("kind", KINDS)
def test_a_pruned_seq_is_not_assigned_again(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n, at=T0 + timedelta(days=n)) for n in range(1, 11)))
        store.put_snapshot(10, 1, b"s")
        assert store.prune_before(10, T0 + timedelta(days=100)) == 9  # only row 10 is left
        assert store.append(rec(11)) == 11
        assert [s for s, _ in store.read_from(0)] == [10, 11]


def test_sqlite_assigns_no_seq_twice_even_with_an_empty_journal(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        store.append(rec(1), rec(2), rec(3))
    db = raw(path)
    db.execute("DELETE FROM journal")  # a tool outside the store empties the table
    db.close()
    with closing(SqliteStore(path)) as store:
        assert store.last_seq() == 3
        assert store.append(rec(4)) == 4
        with pytest.raises(StoreError):
            store.put_snapshot(9, 1, b"beyond")


@pytest.mark.parametrize("kind", KINDS)
def test_prune_before_honours_both_bounds(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n, at=T0 + timedelta(days=n)) for n in range(1, 11)))
        store.put_snapshot(9, 1, b"s")
        # seq bound 4 wins: rows 1..3 go; age bound T0+days(2) wins over seq 9: only row 1 goes
        assert store.prune_before(4, T0 + timedelta(days=100)) == 3
        assert store.prune_before(9, T0 + timedelta(days=5)) == 1  # row 4 only (<5 days)
        assert [s for s, _ in store.read_from(0)][:2] == [5, 6]


@pytest.mark.parametrize("kind", KINDS)
def test_prune_before_with_no_snapshot_deletes_no_journal_row(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n) for n in range(1, 4)))
        assert store.prune_before(3, T0 + timedelta(days=100)) == 0
        assert store.last_seq() == 3


@pytest.mark.parametrize("kind", KINDS)
def test_prune_before_keeps_the_latest_two_snapshots(kind: str, tmp_path: Path) -> None:
    with open_store(kind, tmp_path) as store:
        store.append(*(rec(n) for n in range(1, 8)))
        for seq in (1, 2, 3, 5):
            store.put_snapshot(seq, 1, f"s{seq}".encode())
        store.prune_before(1, T0)
        # 1 and 2 are gone, so putting them again is allowed; 3 and 5 are kept
        store.put_snapshot(1, 1, b"back")
        store.put_snapshot(2, 1, b"back")
        for kept in (3, 5):
            with pytest.raises(StoreError):
                store.put_snapshot(kept, 1, b"x")


# ------------------------------------------------------------------ sqlite only


def raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def test_sqlite_settings_and_meta(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    made = datetime(2026, 1, 6, 9, 30, tzinfo=UTC)
    with closing(
        SqliteStore(path, engine_version="2.0.0", estate_name="nb", created_at=made)
    ) as as_store:
        assert as_store.meta() == {
            "schema_version": "1",
            "engine_version": "2.0.0",
            "estate_name": "nb",
            "created_at": "2026-01-06T09:30:00.000000",
        }
        as_store.append(rec(1))
        db = as_store._db
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 0  # the writer waits for no one
        if sys.platform == "darwin":
            assert db.execute("PRAGMA fullfsync").fetchone()[0] == 1
        reader = SqliteStore(path, read_only=True)
        try:
            assert reader._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            reader.close()
    with closing(SqliteStore(path, engine_version="9")) as reopened:  # existing file: meta is kept
        assert reopened.meta()["engine_version"] == "2.0.0"
        assert reopened.last_seq() == 1


def test_sqlite_state_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = SqliteStore(path)
    store.append(rec(1, request_id="a"), rec(2))
    store.put_config(b"cfg", "sha256:c")
    store.put_snapshot(2, 3, b"snap")
    store.close()
    store.close()  # closing twice is harmless
    again = SqliteStore(path)
    try:
        assert again.last_seq() == 2
        assert again.find_request("a") is not None
        assert again.get_config(again.current_config_rev()) == b"cfg"
        snap = again.latest_snapshot()
        assert snap is not None and (snap.seq, snap.state_version) == (2, 3)
    finally:
        again.close()


def test_sqlite_refuses_a_file_that_is_not_its_schema(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    other.write_bytes(b"not a database, just text that is long enough to be read as a header")
    with pytest.raises(StoreError):
        SqliteStore(other)
    future = tmp_path / "future.db"
    SqliteStore(future).close()
    db = raw(future)
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.close()
    with pytest.raises(StoreError, match="schema_version '99'"):
        SqliteStore(future)
    with pytest.raises(StoreError, match="schema_version '99'"):
        SqliteStore(future, read_only=True)


def test_sqlite_refuses_a_foreign_database_and_leaves_it_alone(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.db"
    db = raw(foreign)
    db.execute("CREATE TABLE journal (x)")
    db.execute("INSERT INTO journal VALUES (1)")
    db.close()
    half = tmp_path / "half.db"
    db = raw(half)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.close()
    for path in (foreign, half):
        before = path.read_bytes()
        with pytest.raises(StoreError, match="schema_version"):
            SqliteStore(path)
        db = raw(path)
        try:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # not made WAL
            tables = {
                r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            db.close()
        assert tables <= {"journal", "meta"}
        assert path.read_bytes() == before
    empty = tmp_path / "empty.db"
    empty.touch()
    SqliteStore(empty).close()  # a zero-byte file is a new store


def test_sqlite_in_memory_database_opens(tmp_path: Path) -> None:
    with closing(SqliteStore(":memory:")) as store:
        assert store.append(rec(1)) == 1
        assert [s for s, _ in store.read_from(0)] == [1]


def test_sqlite_each_table_refuses_a_violating_row(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    store = SqliteStore(path)
    store.append(rec(1, request_id="a"))
    store.put_config(b"c", "sha256:c")
    store.put_snapshot(1, 1, b"s")
    store.close()
    db = raw(path)
    try:
        violations = [
            "INSERT INTO journal(seq, at_utc, kind, payload) VALUES (1, 'x', 'k', x'7b7d')",
            "INSERT INTO journal(at_utc, kind, request_id, payload) VALUES ('x', 'k', 'a', x'7b7d')",
            "INSERT INTO journal(at_utc, kind, payload) VALUES ('x', NULL, x'7b7d')",
            "INSERT INTO journal(at_utc, kind, payload) VALUES (NULL, 'k', x'7b7d')",
            "INSERT INTO journal(at_utc, kind, payload) VALUES ('x', 'k', NULL)",
            "INSERT INTO configs(rev, digest, data) VALUES (1, 'sha256:new', x'00')",
            "INSERT INTO configs(digest, data) VALUES (NULL, x'00')",
            "INSERT INTO configs(digest, data) VALUES ('sha256:z', NULL)",
            "INSERT INTO snapshots(seq, state_version, data) VALUES (1, 1, x'00')",
            "INSERT INTO snapshots(seq, state_version, data) VALUES (9, NULL, x'00')",
            "INSERT INTO snapshots(seq, state_version, data) VALUES (9, 1, NULL)",
            "INSERT INTO meta(key, value) VALUES ('schema_version', '1')",
            "INSERT INTO meta(key, value) VALUES ('new_key', NULL)",
        ]
        for statement in violations:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    finally:
        db.close()


def test_sqlite_busy_raises_and_does_not_wait(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as store:
        store.append(rec(1))
        other = raw(path)
        try:
            other.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.append(rec(2))
            assert time.monotonic() - started < 1.0
            other.execute("ROLLBACK")
        finally:
            other.close()
        assert store.last_seq() == 1
        assert store.append(rec(2)) == 2  # the refused append left the store usable


def test_sqlite_reader_sees_commits_during_writes_and_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    with closing(SqliteStore(path)) as writer:
        writer.append(rec(1))
        reader = SqliteStore(path, read_only=True)
        try:
            assert reader.last_seq() == 1
            writer.append(rec(2), rec(3))
            assert [s for s, _ in reader.read_from(1)] == [2, 3]
            with pytest.raises(sqlite3.OperationalError):
                reader.append(rec(4))
            with pytest.raises(sqlite3.OperationalError):
                reader.put_config(b"x", "sha256:x")
            assert writer.last_seq() == 3
        finally:
            reader.close()


def test_sqlite_reader_does_not_create_a_missing_store(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        SqliteStore(tmp_path / "missing.db", read_only=True)
    assert not (tmp_path / "missing.db").exists()


# ------------------------------------------------------------------ crash safety

_CHILD = """
import sys
from datetime import datetime
from dsl41.runner_store import Record, SqliteStore

store = SqliteStore(sys.argv[1])
i = 0
while True:
    batch = [
        Record(datetime(2026, 1, 6), "input", "child", f"r{i + k}", {"i": i + k}) for k in range(3)
    ]
    print(store.append(*batch), flush=True)
    i += 3
"""


#: Batches the child has acknowledged before the parent may kill it.
_MIN_ACKS = 50


def _drain(stream: IO[str], into: list[int]) -> None:
    for line in stream:
        into.append(int(line))


def _assert_whole_batches(path: Path, acked: int, note: str) -> int:
    """The journal at `path` is seq 1..n of whole three-record batches and n >= `acked`."""
    store = SqliteStore(path)
    try:
        got = list(store.read_from(0))
    finally:
        store.close()
    assert [seq for seq, _ in got] == list(range(1, len(got) + 1)), note
    assert len(got) % 3 == 0, f"a torn batch: {note}"
    assert len(got) >= acked, f"acknowledged {acked}, found {len(got)}: {note}"
    for seq, record in got:
        assert record.request_id == f"r{seq - 1}" and record.payload["i"] == seq - 1
    return len(got)


@pytest.mark.parametrize("round_", range(3))
def test_crash_smoke_sigkill_leaves_a_prefix_of_whole_batches(round_: int, tmp_path: Path) -> None:
    """Smoke: a child appends three-record batches and is killed with SIGKILL after at least
    `_MIN_ACKS` acknowledged batches, at a random moment. The deterministic gate is
    `test_recovery_ignores_a_damaged_wal_tail`; this one shows the real kill."""
    path = tmp_path / "s.db"
    delay = random.Random().uniform(0.0, 0.2)
    log = tmp_path / "child.err"
    acked: list[int] = []
    with log.open("wb") as err:
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(path)],
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
        )
        assert child.stdout is not None
        reader = threading.Thread(target=_drain, args=(child.stdout, acked), daemon=True)
        reader.start()  # the pipe is read while the parent waits, so the child never blocks in print
        try:
            deadline = time.monotonic() + 60
            while len(acked) < _MIN_ACKS and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            assert len(acked) >= _MIN_ACKS, (
                f"the child acknowledged {len(acked)}: {log.read_text()}"
            )
            time.sleep(delay)
        finally:
            child.kill()
            child.wait(timeout=30)
            reader.join(timeout=30)
            child.stdout.close()
    assert not reader.is_alive()
    _assert_whole_batches(path, acked[-1], f"delay {delay}")


@pytest.mark.parametrize("damage", ["garbage", "truncated"])
def test_recovery_ignores_a_damaged_wal_tail(damage: str, tmp_path: Path) -> None:
    """The deterministic crash gate. A store with committed batches still in its `-wal` file is
    copied as a crash leaves it (no checkpoint, no close), the copy's WAL tail is damaged, and the
    reopened journal is a prefix of whole batches: all of them after garbage past the last
    frame, and fewer (never a torn one) after the last frames are cut."""
    live = tmp_path / "live"
    live.mkdir()
    store = SqliteStore(live / "s.db")
    try:
        for i in range(0, 60, 3):
            store.append(
                *(
                    Record(T0, "input", "w", f"r{i + k}", {"i": i + k, "pad": "x" * 200})
                    for k in range(3)
                )
            )
        wal = live / "s.db-wal"
        assert wal.stat().st_size > 0, "the commits must still be in the WAL"
        crashed = tmp_path / "crashed"
        crashed.mkdir()
        shutil.copy(live / "s.db", crashed / "s.db")
        shutil.copy(wal, crashed / "s.db-wal")
    finally:
        store.close()
    copy_wal = crashed / "s.db-wal"
    if damage == "garbage":
        with copy_wal.open("ab") as f:
            f.write(b"\x00garbage-that-is-not-a-frame\xff" * 50)
        assert _assert_whole_batches(crashed / "s.db", 60, damage) == 60
    else:
        size = copy_wal.stat().st_size
        with copy_wal.open("r+b") as f:
            f.truncate(size - 100)  # cuts into the last frame, so the last commit is lost
        found = _assert_whole_batches(crashed / "s.db", 0, damage)
        assert 0 < found < 60


# ------------------------------------------------------------------ snapshot plus tail


def small_catalog(directory: Path) -> CatalogIR:
    props = compute_properties(directory, datetime(2026, 1, 6, 0, 5, tzinfo=UTC), 3)
    props_file = directory / "night.properties"
    write_kv(props_file, props)
    bindings = load_properties([props_file])
    parsed = []
    for path in sorted((NB / "estate" / "small").glob("*.jil")):
        resolved, _ = substitute(path.read_text(), bindings, file=str(path))
        parsed.append(parse(resolved, file=str(path)))
    return lower_catalog(parsed, permit_unknown=False)


#: One recorded input: an event to feed, or the instant the clock was advanced to.
type Step = Event | datetime


def input_record(step: Step) -> Record:
    if isinstance(step, datetime):
        return Record(step, "input", None, None, {"at": step, "kind": "ADVANCE"})
    body: dict[str, Any] = {"at": step.at, "kind": step.kind, "payload": step.payload}
    if step.source is not None:
        body["source"] = step.source
    return Record(step.at, "input", step.source, None, body)


def step_of(record: Record) -> Step:
    body = record.payload
    at = datetime.fromisoformat(body["at"])
    if body["kind"] == "ADVANCE":
        return at
    return Event(at=at, kind=body["kind"], payload=dict(body["payload"]), source=body.get("source"))


def apply(oracle: Oracle, step: Step) -> list[Event]:
    return oracle.advance(step) if isinstance(step, datetime) else oracle.feed(step)


def trace_digest(entries: list[TraceEntry]) -> str:
    return digest(
        {
            "trace": [
                {"at": e.at, "job": e.job, "transition": e.transition, "cause": e.cause}
                for e in entries
            ]
        }
    )


def sealed_state(oracle: Oracle, at: datetime) -> SealedState:
    """The oracle's rows as the seal's `state` section holds them (the engine's `_snapshot`)."""
    store = oracle.store
    return SealedState(
        jobs=dict(store.job),
        globals=dict(store.globals_),
        hosts={},  # the host table is dormant and empty after slice 0b (DL-425)
        routes={},
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        now=at,
    )


def seal_state_payload(state: SealedState) -> dict[str, Any]:
    payload = state.model_dump(by_alias=True)
    payload["jobs"] = {
        name: {**row, "reservations": sorted(row["reservations"], key=lambda h: h["bucket"])}
        for name, row in payload["jobs"].items()
    }
    canon = _canon_ready(payload)
    assert isinstance(canon, dict)
    return canon


def clock(oracle: Oracle) -> datetime:
    """The oracle's own clock: the instant of the last input it applied."""
    assert oracle._now is not None
    return oracle._now


def state_digest(oracle: Oracle) -> str:
    """The digest of the oracle's rows and clock, as the seal's `state` section holds them."""
    return digest({"state": seal_state_payload(sealed_state(oracle, clock(oracle)))})


#: Called after each applied input; the first time it is true, a snapshot is taken.
type SnapWhen = Callable[[Step, Oracle], bool]


def record(
    store: Store, catalog: CatalogIR, scripted: list[Event], snapshot_when: SnapWhen
) -> tuple[int, int]:
    """Play `scripted` through the oracle, journalling each input before applying it. A STARTING
    status gets a `dispatch` record and the adapter's RUNNING, then SUCCESS, as later inputs; a due
    timer is a clock advance. The first time `snapshot_when` is true after an input, snapshot the
    oracle's state. Returns the snapshot's seq and the trace length then."""
    oracle = Oracle(catalog)
    queue: list[tuple[datetime, int, Event]] = []
    counter = itertools.count()

    def schedule(ev: Event) -> None:
        heapq.heappush(queue, (ev.at, next(counter), ev))

    for ev in scripted:
        schedule(ev)

    def react(out: Event) -> None:
        job = out.payload["job"]
        if out.kind == "STATUS" and out.payload.get("status") == "STARTING":
            store.append(Record(out.at, "dispatch", "scheduler", None, {"job": job}))
            schedule(
                Event(
                    at=out.at + timedelta(seconds=1),
                    kind="STATUS",
                    source="adapter",
                    payload={"job": job, "status": "RUNNING"},
                )
            )
        elif out.kind == "STATUS" and out.payload.get("status") == "RUNNING":
            schedule(
                Event(
                    at=out.at + timedelta(seconds=20),
                    kind="STATUS",
                    source="adapter",
                    payload={"job": job, "status": "SUCCESS", "exit_code": 0},
                )
            )

    horizon = T0 + timedelta(hours=4)
    snap_seq, snap_trace = 0, 0
    while True:
        due = oracle.next_timer_due()
        step: Step
        if due is not None and due <= horizon and (not queue or due < queue[0][0]):
            step = due  # the clock reaches a timer between two inputs: a time observation
        elif queue:
            step = heapq.heappop(queue)[2]
        else:
            break
        store.append(input_record(step))
        for out in apply(oracle, step):
            react(out)
        if not snap_seq and snapshot_when(step, oracle):
            snap_seq = store.last_seq()
            snap_trace = len(oracle.trace())
            data = canonical_bytes(seal_state_payload(sealed_state(oracle, clock(oracle))))
            store.put_snapshot(snap_seq, 1, data)
    assert snap_seq, "the night ended before the snapshot point"
    return snap_seq, snap_trace


def restored(catalog: CatalogIR, data: bytes) -> Oracle:
    state = SealedState.model_validate(decode(data))
    carried = CarriedRows(
        jobs=dict(state.jobs),
        globals_=dict(state.globals),
        timers=state.timers,
        timer_seq=state.timer_seq,
        consumed=dict(state.consumed),
        enqueue_counter=state.enqueue_counter,
        now=state.now,
    )
    return Oracle(catalog, carried=carried)


# ---- the two estates


def nightbank_script(catalog: CatalogIR) -> list[Event]:
    tops = [name for name, job in catalog.jobs.items() if job.box.box_name is None]
    script = [Event(at=T0, kind="STARTJOB", payload={"job": n}, source="control") for n in tops]
    script += [
        Event(
            at=T0 + timedelta(minutes=40),
            kind="SET_GLOBAL",
            payload={"name": f"RECON_{region}", "value": "CLEAN"},
            source="control",
        )
        for region in ("APAC", "EMEA", "AMER")
    ]
    script.append(
        Event(
            at=T0 + timedelta(minutes=43),
            kind="OFF_HOLD",
            payload={"job": "SOD_APPROVE_C"},
            source="control",
        )
    )
    return script


#: A depletable resource of two units, three jobs wanting one each: the third stays queued, and
#: `consumed` is non-empty once a run completes.
QUOTA_JIL = (
    "insert_resource: QUOTA\nres_type: D\namount: 2\n\n"
    "insert_job: d1\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n\n"
    "insert_job: d2\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n\n"
    "insert_job: d3\njob_type: c\ncommand: x\nmachine: m1\nresources: (QUOTA, QUANTITY=1)\n"
)


def quota_script() -> list[Event]:
    return [
        Event(
            at=T0 + timedelta(minutes=minute),
            kind="STARTJOB",
            payload={"job": job},
            source="control",
        )
        for minute, job in ((0, "d1"), (5, "d2"), (10, "d3"))
    ]


def nth_input(n: int) -> SnapWhen:
    count = itertools.count(1)
    return lambda step, oracle: next(count) == n


def after_first_set_global(step: Step, oracle: Oracle) -> bool:
    return isinstance(step, Event) and step.kind == "SET_GLOBAL"


def after_first_consumption(step: Step, oracle: Oracle) -> bool:
    return bool(oracle.store.consumed)


type Mutation = Callable[[dict[str, Any]], dict[str, Any]]


def drop_globals(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "globals": {}}


def shift_now(state: dict[str, Any]) -> dict[str, Any]:
    back = datetime.fromisoformat(state["now"]) - timedelta(minutes=30)
    return {**state, "now": back.isoformat(timespec="microseconds")}


def empty_consumed(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "consumed": {}}


def drop_timers(state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "timers": []}


@dataclass(frozen=True)
class Night:
    """One recorded run and the facts that make its snapshot worth testing."""

    catalog: CatalogIR
    store: MemoryStore
    snap_seq: int


def make_night(name: str, directory: Path) -> Night:
    store = MemoryStore()
    if name == "quota":
        catalog = lower_source(QUOTA_JIL)
        snap_seq, _ = record(
            store, catalog.model_copy(deep=True), quota_script(), after_first_consumption
        )
    else:
        catalog = small_catalog(directory)
        when = after_first_set_global if name == "after_global" else nth_input(40)
        script = nightbank_script(catalog)
        snap_seq, _ = record(store, catalog.model_copy(deep=True), script, when)
    return Night(catalog, store, snap_seq)


def resume_mismatches(night: Night, mutate: Mutation | None = None) -> list[str]:
    """Restore the snapshot (mutated, when asked), replay the journal's tail, and name what differs
    from a full replay: the state at restore, the trace of the tail, and the final state."""
    store, catalog = night.store, night.catalog
    inputs = [(seq, r) for seq, r in store.read_from(0) if r.kind == "input"]
    full = Oracle(catalog.model_copy(deep=True))
    at_snapshot, trace_len = "", 0
    for seq, record_ in inputs:
        if seq > night.snap_seq and not at_snapshot:
            at_snapshot, trace_len = state_digest(full), len(full.trace())
        apply(full, step_of(record_))
    assert at_snapshot, "the snapshot is at the end of the journal: there is no tail"
    snapshot = store.latest_snapshot()
    assert snapshot is not None and snapshot.seq == night.snap_seq
    data = snapshot.data
    if mutate is not None:
        data = canonical_bytes(mutate(dict(decode(data))))  # type: ignore[call-overload]
    found: list[str] = []
    try:
        resumed = restored(catalog.model_copy(deep=True), data)
        if state_digest(resumed) != at_snapshot:
            found.append("state at restore")
        for _, record_ in store.read_from(night.snap_seq):
            if record_.kind == "input":
                apply(resumed, step_of(record_))
    except Exception as exc:  # noqa: BLE001 -- a restore that fails is a mismatch to report
        return [*found, f"raised {type(exc).__name__}"]
    if trace_digest(resumed.trace()) != trace_digest(full.trace()[trace_len:]):
        found.append("tail trace")
    if state_digest(resumed) != state_digest(full):
        found.append("final state")
    return found


NIGHTS = ["mid", "after_global", "quota"]


@pytest.fixture(scope="module")
def nights(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Night]:
    return {name: make_night(name, tmp_path_factory.mktemp(name)) for name in NIGHTS}


@pytest.mark.parametrize("name", NIGHTS)
def test_snapshot_plus_tail_equals_full_replay(name: str, nights: dict[str, Night]) -> None:
    night = nights[name]
    kinds = {r.kind for _, r in night.store.read_from(0)}
    assert kinds == {"input", "dispatch"}  # the replay skips records that are not inputs
    inputs = [seq for seq, r in night.store.read_from(0) if r.kind == "input"]
    tail = [seq for seq in inputs if seq > night.snap_seq]
    assert 0 < len(tail) < len(inputs)
    assert resume_mismatches(night) == []


def test_the_snapshots_carry_what_the_mutations_below_remove(nights: dict[str, Night]) -> None:
    after_global = restored(
        nights["after_global"].catalog.model_copy(deep=True),
        nights["after_global"].store.latest_snapshot().data,  # type: ignore[union-attr]
    )
    assert any(g.value == "CLEAN" for g in after_global.store.globals_.values())
    assert any(g.value == "PENDING" for g in after_global.store.globals_.values())  # mid-script
    quota = restored(
        nights["quota"].catalog.model_copy(deep=True),
        nights["quota"].store.latest_snapshot().data,  # type: ignore[union-attr]
    )
    assert dict(quota.store.consumed) == {"r:QUOTA": 1}
    assert clock(after_global) == T0 + timedelta(minutes=40)


@pytest.mark.parametrize(
    ("name", "mutation", "expected"),
    [
        ("after_global", drop_globals, "final state"),
        ("mid", shift_now, "state at restore"),
        ("quota", shift_now, "state at restore"),
        ("quota", empty_consumed, "final state"),
        ("mid", drop_timers, "tail trace"),
    ],
    ids=["globals", "now-mid", "now-quota", "consumed", "timers"],
)
def test_a_snapshot_that_loses_state_fails_the_resume_check(
    name: str, mutation: Mutation, expected: str, nights: dict[str, Night]
) -> None:
    found = resume_mismatches(nights[name], mutation)
    assert expected in found, found


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("capture", [{}, {"main-inputs.json": "captured bytes"}])
def test_configuration_round_trip_keeps_catalog_order(
    kind: str, capture: dict[str, str], tmp_path: Path
) -> None:
    catalog = lower_source(
        "".join(f"insert_job: {name}\njob_type: c\ncommand: x\n\n" for name in ("z", "a", "m"))
    )
    configuration = Configuration(catalog=catalog, profile=RuntimeProfile(), source_capture=capture)
    with open_store(kind, tmp_path) as store:
        rev = store.put_config(configuration.to_bytes(), configuration.digest())
        restored = Configuration.from_bytes(store.get_config(rev), where="order regression")
        assert list(restored.catalog.jobs) == ["z", "a", "m"]
        assert restored.source_capture == capture
        assert restored.to_bytes() == configuration.to_bytes()
        assert restored.digest() == configuration.digest()


@pytest.mark.parametrize("version", [None, 1])
def test_legacy_configuration_keeps_its_recorded_order(version: int | None) -> None:
    configuration = Configuration(
        catalog=lower_source(
            "insert_job: z\njob_type: c\ncommand: x\n\ninsert_job: a\njob_type: c\ncommand: x\n"
        ),
        profile=RuntimeProfile(),
        non_idempotent={"z": "synthetic external effect"},
    )
    payload = decode(configuration.to_bytes())
    payload.pop("job_order")
    if version is None:
        payload.pop("configuration_format_version")
    else:
        payload["configuration_format_version"] = version
    data = canonical_bytes(payload)
    restored = Configuration.from_bytes(data, where="legacy store")
    assert list(restored.catalog.jobs) == ["a", "z"]
    assert restored.catalog == configuration.catalog
    assert restored.profile == configuration.profile
    assert restored.non_idempotent == configuration.non_idempotent
    assert restored.source_capture == configuration.source_capture
    reread = Configuration.from_bytes(restored.to_bytes(), where="legacy round trip")
    assert list(reread.catalog.jobs) == ["a", "z"]
    assert reread == restored


@pytest.mark.parametrize(
    "order", [None, "z,a", {}, ["z"], ["z", "z"], ["z", "other"], ["z", 1], [["z"], "a"]]
)
def test_configuration_refuses_malformed_job_order(order: Any) -> None:
    configuration = Configuration(
        catalog=lower_source(
            "insert_job: z\njob_type: c\ncommand: x\n\ninsert_job: a\njob_type: c\ncommand: x\n"
        ),
        profile=RuntimeProfile(),
    )
    payload = decode(configuration.to_bytes())
    payload["job_order"] = order
    with pytest.raises(
        EngineError, match="bad-order: job_order must name every catalog job exactly once"
    ):
        Configuration.from_bytes(canonical_bytes(payload), where="bad-order")
    payload.pop("job_order")
    with pytest.raises(EngineError, match="job_order"):
        Configuration.from_bytes(canonical_bytes(payload), where="missing-order")


@pytest.mark.parametrize("version", [1, 2, 3])
def test_configuration_versions_refuse_unknown_fields(version: int) -> None:
    configuration = Configuration(catalog=CatalogIR(), profile=RuntimeProfile())
    payload = decode(configuration.to_bytes())
    payload["configuration_format_version"] = version
    if version < 3:
        payload.pop("job_order")
    if version == 2:
        payload["source_capture"] = {"main-inputs.json": "captured bytes"}
    payload["unknown"] = True
    with pytest.raises(EngineError, match="unknown configuration fields.*unknown"):
        Configuration.from_bytes(canonical_bytes(payload), where="unknown-field")


def test_configuration_version_and_order_are_digest_bound() -> None:
    configuration = Configuration(
        catalog=lower_source(
            "insert_job: z\njob_type: c\ncommand: x\n\ninsert_job: a\njob_type: c\ncommand: x\n"
        ),
        profile=RuntimeProfile(),
    )
    payload = decode(configuration.to_bytes())
    assert payload["configuration_format_version"] == 3
    assert list(payload["catalog"]["jobs"]) == ["a", "z"]
    assert payload["job_order"] == ["z", "a"]
    payload["job_order"].reverse()
    reordered = Configuration.from_bytes(canonical_bytes(payload), where="reordered")
    assert reordered.catalog_hash == configuration.catalog_hash
    assert reordered.digest() != configuration.digest()
    with pytest.raises(EngineError, match="unsupported configuration_format_version 4"):
        Configuration.from_bytes(b'{"configuration_format_version":4}', where="foreign-version")


def test_empty_configuration_has_an_empty_job_order() -> None:
    configuration = Configuration(catalog=CatalogIR(), profile=RuntimeProfile())
    assert decode(configuration.to_bytes())["job_order"] == []
    assert Configuration.from_bytes(configuration.to_bytes(), where="empty") == configuration
