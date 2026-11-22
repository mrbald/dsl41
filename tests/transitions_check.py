"""The equality check of the committed transition rows (DL-536), for the test files whose
scenarios write run roots: over the covered seqs of every store a test left, the rows equal, key
for key, what the offline replay of the same journal produces (`replay_store`). Each replayed
entry is keyed by the journal seq of the record that produced it, an input's or a `config
applied`'s, and its ordinal there. A replay adds no `RESTART` mark, so none is compared; a store
built by hand that the offline replay refuses is not compared.

A test that edits a store on a connection of its own (`store_surgery`, or `sqlite3` in the
test module) leaves a store the engine did not write, so its roots are left out; `edited` names
the tests that did. Appending a valid journal record is not an edit. Whether the check compared
anything is asserted by an explicit test over named scenarios, not here. The comparison runs
once per store, when the module's tests are done, so no test pays for a replay of its own.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import store_surgery
from dsl41 import runner_journal
from dsl41.canon import canonical_bytes, decode
from dsl41.oracle_reads import OracleReads
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import CONFIG_MARKER, STORE_NAME, replay_store
from dsl41.runner_store import SqliteStore

#: tests left out because they edited a store by hand
edited: list[str] = []

#: how many stores and rows the check compared in this session
compared: dict[str, int] = {"stores": 0, "rows": 0}

type Row = tuple[int, int, Any]


def rows_and_replay(path: Path) -> tuple[list[Row], list[Row]] | None:
    """The stored rows over the covered seqs after the replay's anchor, and the replayed rows
    there, both keyed `(seq, n)`; None for a store the offline replay refuses."""
    with closing(SqliteStore(path, read_only=True)) as store:
        tables = {
            str(name)
            for (name,) in store._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "transitions" not in tables:
            return None
        inputs: dict[int, int] = {}
        configs: dict[int, int] = {}
        for seq, record in store.read_from(0):
            if record.kind == "input":
                inputs[int(record.payload["index"])] = seq
            elif record.kind == "config" and record.payload.get("event") == "applied":
                configs[int(record.payload["rev"])] = seq
        replayed: list[Row] = []
        apply_attempt, mark = runner_journal.apply_attempt, OracleReads.mark

        def keyed_apply(oracle: Any, attempt: Any, **kwargs: Any) -> Any:
            begun = oracle.trace_length()
            applied = apply_attempt(oracle, attempt, **kwargs)
            _, entries = oracle.trace_since(begun)
            seq = inputs[attempt.index]
            replayed.extend((seq, n, _canon(entry)) for n, entry in enumerate(entries))
            return applied

        def keyed_mark(oracle: Any, at: Any, transition: str, cause: str) -> None:
            mark(oracle, at, transition, cause)
            if transition == CONFIG_MARKER:
                seq = configs[int(cause.rsplit(" ", 1)[1])]
                replayed.append((seq, 0, _canon(oracle.trace()[-1])))

        try:
            with (
                mock.patch.object(runner_journal, "apply_attempt", keyed_apply),
                mock.patch.object(OracleReads, "mark", keyed_mark),
            ):
                found = replay_store(store, where=str(path))
        except EngineError:
            return None  # a store built by hand that the offline replay refuses
        after = 0 if found.anchor is None else found.anchor.seq
        covered = [(i.first, i.last) for i in store.transitions_coverage() if i.covered]

        def counted(seq: int) -> bool:
            return seq > after and any(low <= seq <= high for low, high in covered)

        stored = [
            (int(seq), int(n), decode(bytes(entry)))
            for seq, n, entry in store._db.execute(
                "SELECT seq, n, entry FROM transitions ORDER BY seq, n"
            )
            if counted(int(seq))
        ]
    return stored, [row for row in replayed if counted(row[0])]


def _canon(entry: Any) -> Any:
    return decode(canonical_bytes(entry.model_dump()))


#: The authorizer actions that leave a store as the engine wrote it: reads, and an insert into
#: the journal, which is a record appended as the writer would.
_KEPT: frozenset[int] = frozenset(
    {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_PRAGMA}
    | {sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)


def _writes_into(touched: list[str]) -> Any:
    """An authorizer that notes every edit a connection makes and allows it."""

    def authorize(action: int, table: object, *_: object) -> int:
        if action not in _KEPT and not (action == sqlite3.SQLITE_INSERT and table == "journal"):
            touched.append(f"{action} {table}")
        return sqlite3.SQLITE_OK

    return authorize


def check_roots(directory: Path) -> int:
    """Assert the equality for every store under `directory`; answer how many rows matched."""
    matched = 0
    for path in sorted(directory.rglob(STORE_NAME)):
        if not path.is_file():
            continue
        found = rows_and_replay(path)
        if found is None:
            continue
        stored, replayed = found
        assert stored == replayed, path
        matched += len(stored)
        compared["stores"] += 1
        compared["rows"] += len(stored)
    return matched


@pytest.fixture(autouse=True, scope="module")
def rows_equal_their_replay_once() -> Any:
    """At the module's end, each store a test of it left is compared once (module docstring)."""
    left: list[Path] = []
    yield left
    for directory in left:
        check_roots(directory)


@pytest.fixture(autouse=True)
def rows_equal_their_replay(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    rows_equal_their_replay_once: list[Path],
) -> Any:
    """Note the stores a test leaves for the module's one comparison, unless it edited one by
    hand (module docstring)."""
    touched: list[str] = []
    real = sqlite3.connect

    def watched(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db = real(*args, **kwargs)
        caller = str(sys._getframe(1).f_globals.get("__name__", ""))
        if caller.startswith("test_") or caller == store_surgery.__name__:
            db.set_authorizer(_writes_into(touched))  # a test's own connection
        return db

    monkeypatch.setattr(sqlite3, "connect", watched)
    directory = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None
    yield
    if directory is None:
        return
    if touched:
        edited.append(request.node.nodeid)
        return
    rows_equal_their_replay_once.append(directory)
