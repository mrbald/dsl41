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

The same replay checks how runs are numbered (DL-537): for every row and tombstone the replay
leaves, the status entries that carry a run number are exactly the runs from the one after the
snapshot row's (or 1) to the row's run number (`runs_open_in_order`).

It checks the history index too (DL-539), over the seqs `history_coverage` covers. The
`job_runs` rows after the replay's anchor are the replayed status entries that carry a run
number, with the keys of their rows, each under the rev the replay opened with as moved by the
CONFIG marks before it. The `job_records` rows are what a scan of the retained journal names,
written apart from the engine's code: a dispatch's job and run; a STATUS input's job and run,
or none; a decision paired with its input by admission index; an outcome paired with its
dispatch by attempt id. A STATUS input with no string job names no job, so neither it nor its
decision has a row. A decision or an outcome whose input or dispatch the journal no longer
retains cannot be decided from the retained records, so its stored row is not compared.

`check_roots` answers the count of `transitions` rows it matched; `compared` counts each table.
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
from dsl41.runner_journal import CONFIG_MARKER, STORE_NAME, StoreReplay, first_rev, replay_store
from dsl41.runner_store import SqliteStore

#: tests left out because they edited a store by hand
edited: list[str] = []

#: how many stores and rows the check compared in this session: `rows` of `transitions`, and
#: the history index's rows by table
compared: dict[str, int] = {"stores": 0, "rows": 0, "job_runs": 0, "job_records": 0}

type Row = tuple[int, int, Any]


def rows_and_replay(path: Path) -> dict[str, tuple[list[Any], list[Any]]] | None:
    """For each table, the stored rows over its covered seqs and the rows the replay or the
    scan gives there (module docstring); None for a store the offline replay refuses."""
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
        runs_open_in_order(found)
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
        tables_compared = {"transitions": (stored, [row for row in replayed if counted(row[0])])}
        if "job_runs" in tables:
            start = found.anchor.config_rev if found.anchor is not None else first_rev(store)
            tables_compared |= _history(store, replayed, after, start)
    return tables_compared


def _run(payload: dict[str, Any]) -> int | None:
    """A STATUS payload's run number as the run-history fold reads it (an `int` instance, a
    bool as its value), within the store's signed 64-bit integers; else no run."""
    run = payload.get("run_number")
    if isinstance(run, int) and -(2**63) <= run < 2**63:
        return int(run)
    return None


def _history(
    store: SqliteStore, replayed: list[Row], after: int, rev: int
) -> dict[str, tuple[list[Any], list[Any]]]:
    """The history index's stored rows and the rows the replay and the scan give, over the seqs
    `history_coverage` covers (module docstring)."""
    covered = [(i.first, i.last) for i in store.history_coverage() if i.covered]

    def counted(seq: int) -> bool:
        return any(low <= seq <= high for low, high in covered)

    openings: list[Any] = []
    for seq, n, entry in replayed:
        if entry["transition"] == CONFIG_MARKER:
            rev = int(entry["cause"].rsplit(" ", 1)[1])
        elif "->" in entry["transition"] and entry.get("run_number") is not None:
            openings.append((seq, n, entry["job"], entry["run_number"], rev))
    stored_openings = [
        tuple(row)
        for row in store._db.execute(
            "SELECT seq, n, job, run_number, rev FROM job_runs ORDER BY seq, n"
        )
        if row[0] > after and counted(row[0])
    ]
    named: dict[int, tuple[str, int | None]] = {}
    unpaired: set[int] = set()
    by_attempt: dict[str, tuple[str, int | None]] = {}
    by_index: dict[int, tuple[str, int | None]] = {}
    inputs: set[int] = set()
    for seq, record in store.read_from(0):
        body = record.payload
        if record.kind == "dispatch":
            named[seq] = by_attempt[body["attempt"]] = (body["job"], body["run_number"])
        elif record.kind == "input":
            inputs.add(body["index"])
            payload = body.get("payload") or {}
            if body.get("kind") == "STATUS" and isinstance(payload.get("job"), str):
                named[seq] = by_index[body["index"]] = (payload["job"], _run(payload))
        elif record.kind == "decision":
            if body["index"] in by_index:
                named[seq] = by_index[body["index"]]
            elif body["index"] not in inputs:
                unpaired.add(seq)  # its input was pruned: the journal cannot say whose it is
        elif record.kind == "outcome":
            if body["attempt"] in by_attempt:
                named[seq] = by_attempt[body["attempt"]]
            else:
                unpaired.add(seq)  # its dispatch was pruned
    owners = [(seq, *owner) for seq, owner in sorted(named.items()) if counted(seq)]
    stored_owners = [
        tuple(row)
        for row in store._db.execute("SELECT seq, job, run_number FROM job_records ORDER BY seq")
        if counted(row[0]) and row[0] not in unpaired
    ]
    return {
        "job_runs": (
            stored_openings,
            [row for row in openings if row[0] > after and counted(row[0])],
        ),
        "job_records": (stored_owners, owners),
    }


def runs_open_in_order(found: StoreReplay) -> None:
    """For every live row and every tombstone after the replay, the job's numbered status
    entries are exactly the runs from the one after its anchor row's (or 1) to the row's
    `run_number`, none included (DL-537)."""
    rows = None if found.anchor is None else found.anchor.rows
    before = {} if rows is None else {**rows.tombstones, **rows.jobs}
    numbered: dict[str, list[int]] = {}
    for entry in found.oracle.trace():
        if "->" in entry.transition and entry.run_number is not None:
            numbered.setdefault(entry.job, []).append(entry.run_number)
    store = found.oracle.store
    after = {**store.tombstones, **store.job}
    assert set(numbered) <= set(after), sorted(set(numbered) - set(after))
    for job, row in after.items():
        first = before[job].run_number + 1 if job in before else 1
        runs = numbered.get(job, [])
        assert runs == list(range(first, row.run_number + 1)), (job, runs, row.run_number)


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
    """Assert the equality for every store under `directory`; answer how many `transitions`
    rows matched."""
    matched = 0
    for path in sorted(directory.rglob(STORE_NAME)):
        if not path.is_file():
            continue
        found = rows_and_replay(path)
        if found is None:
            continue
        for table, (stored, expected) in found.items():
            assert stored == expected, (path, table)
            if table == "transitions":
                matched += len(stored)
            compared["rows" if table == "transitions" else table] += len(stored)
        compared["stores"] += 1
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
