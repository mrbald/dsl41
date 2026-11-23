"""Where a run opens (DL-537): the oracle puts the run's number on the status entry that begins
it, a STARTING or an ON_NOEXEC bypass's SUCCESS, and the run-history fold opens and numbers its
windows by that number. An injected `STATUS STARTING` begins no run, so it opens no window.

The scenarios run an engine over a run root and read it back as `dsl41 runs` does
(`read_run_root`); `transitions_check` compares each root's stored rows with their replay and
checks the numbering of its runs. Every check is deterministic: a virtual clock, no sleep.
"""

from __future__ import annotations

from reload_fixtures import prepare_estate, write_config

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
    runs_open_in_order,
)

from dsl41 import main_config as main
from dsl41 import oracle_writers
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_intents import StatusLine
from dsl41.oracle_state import Event, JobRuntime, TraceEntry
from dsl41.reload_plan import reread
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_history import RunRow, read_run_root, run_history
from dsl41.runner_journal import StoreReplay, decode_snapshot, load_configuration
from dsl41.runner_startup import start_run
from dsl41.runner_update import UpdateRequest

T0 = datetime(2026, 7, 1, 8, 0)

_LEAF = "insert_job: j\njob_type: c\ncommand: {cmd}\n"


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ev(minutes: float, kind: str, **payload: object) -> Event:
    return Event(at=_at(minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


class _Root:
    """One estate directory and the engine that runs it over `run`: a main config and one JIL
    file, so an update can be planned from an edit of the file."""

    def __init__(self, tmp_path: Path, text: str, script: dict[Any, Any] | None = None) -> None:
        self.dir = tmp_path / "estate"
        self.run = tmp_path / "run"
        write_config(self.dir)
        (self.dir / "jobs.jil").write_text(text)
        estate = prepare_estate(self.dir)
        self.engine: Engine = start_run(
            estate.catalog,
            self.run,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(script or {}, default=None)},
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
        )

    async def feed(self, *events: Event) -> None:
        """Each event at its own time, the engine quiescent after each."""
        for event in events:
            self.engine.inject(event)
            await self.engine.run_until_quiescent(event.at)

    async def update(self, minutes: float, text: str) -> None:
        """An update to `text`, applied at `minutes` with nothing of it running."""
        await self.engine.run_until_quiescent(_at(minutes))
        (self.dir / "jobs.jil").write_text(text)
        engine = self.engine
        assert engine.run_root is not None
        plan = reread(
            engine.update.live(), engine.run_root / "plans", candidate=prepare_estate(self.dir)
        )
        answer = engine.request_update(
            UpdateRequest(plan.token, engine.config_rev, engine.baseline_id, f"u{minutes}")
        )
        await engine.run_until_quiescent(engine.clock.now())
        assert answer.done()

    async def close(self) -> None:
        """Shut the engine down, keeping each rev's catalog hash (`hashes`). The store closes
        and the root's lock is released on every path."""
        journal = self.engine.journal
        assert journal is not None
        try:
            await self.engine.shutdown()
            self.hashes = {
                rev: load_configuration(journal.store, rev, where="t").catalog_hash
                for rev in range(1, self.engine.config_rev + 1)
            }
        finally:
            journal.close()


def _rows(root: _Root, job: str) -> list[RunRow]:
    return [row for row in read_run_root(root.run) if row.job == job]


def _scenario(
    tmp_path: Path,
    text: str,
    body: Callable[[_Root], Awaitable[Any]],
    script: dict[Any, Any] | None = None,
) -> tuple[_Root, Any]:
    async def run() -> tuple[_Root, Any]:
        root = _Root(tmp_path, text, script)
        try:
            found = await body(root)
        finally:
            await root.close()
        return root, found

    return asyncio.run(run())


def test_an_injected_starting_opens_no_run_and_the_real_start_keeps_its_own(
    tmp_path: Path,
) -> None:
    """A finished leaf, an injected `STATUS STARTING`, then an update and a real start: two
    runs, and the second is the real start's, with its `started_by` and its revision."""

    async def body(root: _Root) -> str | None:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))
        await root.feed(_ev(2, "STATUS", job="j", status="STARTING"))
        await root.feed(_ev(3, "STATUS", job="j", status="SUCCESS"))
        await root.update(4, _LEAF.format(cmd="y"))
        await root.feed(_ev(5, "STARTJOB", job="j"), _ev(6, "STATUS", job="j", status="SUCCESS"))
        return root.engine.oracle.store.job["j"].started_by

    root, started_by = _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    first, second = _rows(root, "j")
    assert (first.run_number, second.run_number) == (1, 2)
    assert second.started_by == started_by != "injected STATUS"
    assert second.started_at == _at(5)
    assert (first.catalog_hash, second.catalog_hash) == (root.hashes[1], root.hashes[2])


def test_a_box_retyped_after_an_injected_starting_reads_as_one_box_run_and_one_command_run(
    tmp_path: Path,
) -> None:
    """An empty box J runs, is set STARTING and then TERMINATED by injected status, an update
    retypes J to a command, and J starts: `J.1` as a box, `J.2` as a command under its own
    revision, and no run number twice."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="J"))
        await root.feed(_ev(1, "STATUS", job="J", status="STARTING"))
        await root.feed(_ev(2, "STATUS", job="J", status="TERMINATED"))
        await root.update(3, "insert_job: J\njob_type: c\ncommand: x\n")
        await root.feed(_ev(4, "STARTJOB", job="J"), _ev(5, "STATUS", job="J", status="SUCCESS"))

    root, _ = _scenario(tmp_path, "insert_job: J\njob_type: b\n", body)
    rows = _rows(root, "J")
    assert [(row.run_number, row.clock_source) for row in rows] == [(1, "journal"), (2, "journal")]
    box, command = rows
    # SEM-01 latching: the injected TERMINATED is run 1's last close
    assert (box.status, box.catalog_hash, box.started_at, box.ended_at) == (
        "TERMINATED",
        root.hashes[1],
        T0,
        _at(2),
    )
    assert box.box_name is None and box.exit_code is None
    assert (command.status, command.catalog_hash, command.started_at) == (
        "SUCCESS",
        root.hashes[2],
        _at(4),
    )
    assert command.job_hash != box.job_hash


def test_a_bypass_inside_a_failed_box_leaves_each_failure_where_it_was(tmp_path: Path) -> None:
    """A box ends in FAILURE at T1 with its member, goes ON_NOEXEC, and starts again at T2. A
    box does not bypass (SEM-22): its run 2 is a SUCCESS that starts and ends at T2, started by
    the start, and run 1 keeps FAILURE at T1. The member bypasses as run 2, which dispatched
    nothing and has no row; its run 1 keeps FAILURE at T1."""
    text = "insert_job: b\njob_type: b\n\ninsert_job: m\njob_type: c\ncommand: x\nbox_name: b\n"

    async def body(root: _Root) -> list[tuple[str, int | None, str]]:
        await root.feed(_ev(0, "STARTJOB", job="b"), _ev(1, "STATUS", job="m", status="FAILURE"))
        await root.feed(_ev(2, "ON_NOEXEC", job="b"), _ev(3, "STARTJOB", job="b"))
        return [
            (entry.transition, entry.run_number, entry.cause)
            for entry in root.engine.oracle.trace()
            if entry.job == "m" and entry.at == _at(3)
        ]

    root, member = _scenario(tmp_path, text, body)
    assert member == [("INACTIVE->SUCCESS", 2, "ON_NOEXEC bypass (box 'b' started)")]
    first, second = _rows(root, "b")
    assert (first.run_number, first.status, first.ended_at) == (1, "FAILURE", _at(1))
    assert (second.run_number, second.status) == (2, "SUCCESS")
    assert second.started_at == second.ended_at == _at(3)
    assert second.started_by is not None and second.started_by.startswith("STARTJOB")
    [run] = _rows(root, "m")
    assert (run.run_number, run.status, run.ended_at) == (1, "FAILURE", _at(1))


def test_a_bypass_after_a_killed_leaf_leaves_the_killed_run_closed_and_adds_no_row(
    tmp_path: Path,
) -> None:
    """A leaf's run 1 ends by KILLJOB; ON_NOEXEC, then a start bypasses. Run 1 keeps TERMINATED
    and its time; the bypassed run dispatched nothing, so it has no row."""

    async def body(root: _Root) -> int:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "KILLJOB", job="j"))
        await root.feed(_ev(2, "ON_NOEXEC", job="j"), _ev(3, "STARTJOB", job="j"))
        return root.engine.oracle.store.job["j"].run_number

    root, run_number = _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    [row] = _rows(root, "j")
    assert (row.run_number, row.status, row.ended_at) == (1, "TERMINATED", _at(1))
    assert run_number == 2


def test_an_injected_starting_after_a_bypass_reads_the_dispatched_run_as_the_bypass_s(
    tmp_path: Path,
) -> None:
    """BUG-107: ON_NOEXEC J, a start bypasses (run 1), an update, then an injected `STATUS
    STARTING`: the engine dispatches J.1. The fold gives one row for J.1, under the bypass's
    revision, and raises nothing."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "ON_NOEXEC", job="j"), _ev(1, "STARTJOB", job="j"))
        await root.update(2, _LEAF.format(cmd="y"))
        await root.feed(_ev(3, "STATUS", job="j", status="STARTING"))
        await root.engine.run_until_quiescent(_at(6))

    root, _ = _scenario(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (120.0, 0)})
    [row] = _rows(root, "j")
    assert row.run_number == 1
    assert row.catalog_hash == root.hashes[1]
    assert row.started_by is not None and row.started_by.startswith("ON_NOEXEC bypass")
    assert (row.status, row.clock_source, row.exit_code) == ("SUCCESS", "journal", 0)
    assert (row.started_at, row.ended_at, row.duration_s) == (_at(3), _at(5), 120.0)


def _window(run: int, minutes: float, status: str) -> dict[str, Any]:
    return {
        "run": run,
        "started_at": _at(minutes).isoformat(),
        "started_by": f"run {run}",
        "ended_at": _at(minutes + 1).isoformat(),
        "status": status,
        "approximate": False,
        "rev": 1,
    }


def test_a_real_start_replaces_a_history_window_of_the_same_number() -> None:
    """A history an older build wrote is read as it is: windows 1 and 2 with the job's row at
    run 1. The trace's real start carries 2, so run 2 is the real start's window and the
    history's window 2 is gone; window 1 stays."""
    history = {"j": [_window(1, 0, "SUCCESS"), _window(2, 2, "TERMINATED")]}
    anchor = {"j": JobRuntime(status="SUCCESS", status_at=_at(1), run_number=1)}
    trace = [
        TraceEntry(at=_at(10), job="j", transition="SUCCESS->STARTING", cause="real", run_number=2),
        TraceEntry(at=_at(10), job="j", transition="STARTING->RUNNING", cause="admitted"),
    ]
    runs = run_history(history, trace, None, anchor, 1)["j"]
    assert [(run["run"], run["started_by"], run["status"]) for run in runs] == [
        (1, "run 1", "SUCCESS"),
        (2, "real", None),
    ]


def test_the_snapshot_history_holds_no_run_an_injected_starting_began(tmp_path: Path) -> None:
    """The history a snapshot carries (`run_history`) after an injected `STATUS STARTING` holds
    the one run that started."""

    async def body(root: _Root) -> dict[str, Any]:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))
        await root.feed(_ev(2, "STATUS", job="j", status="STARTING"))
        seq = root.engine.take_snapshot()
        assert root.engine.journal is not None
        stored = root.engine.journal.store.latest_snapshot()
        assert stored is not None
        return decode_snapshot(seq, stored.data, where="t").runs

    _, runs = _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    assert [(run["run"], run["status"], run["ended_at"]) for run in runs["j"]] == [
        (1, "SUCCESS", _at(1).isoformat())
    ]


def test_a_start_s_trace_entry_and_stored_row_carry_its_run_number(tmp_path: Path) -> None:
    """The entry that begins a run carries its number in the `trace` verb's answer, in its
    stored transition row and in the `transitions` verb's answer; an injected STARTING carries
    none."""

    async def body(root: _Root) -> tuple[dict[str, Any], dict[str, Any]]:
        await root.feed(_ev(0, "STARTJOB", job="j"), _ev(1, "STATUS", job="j", status="SUCCESS"))
        await root.feed(_ev(2, "STATUS", job="j", status="STARTING"))
        server = ControlServer(root.engine, root.run / "control.sock")
        return (
            await server._respond({"cmd": "trace"}),
            await server._respond({"cmd": "transitions"}),
        )

    _, (trace, transitions) = _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    for answer in (trace, transitions):
        assert answer["ok"] is True
        starts = [
            (entry["transition"], entry.get("run_number"))
            for entry in answer["entries"]
            if entry["job"] == "j" and entry["transition"].endswith("->STARTING")
        ]
        assert starts == [("INACTIVE->STARTING", 1), ("SUCCESS->STARTING", None)]


def test_the_numbering_check_fails_on_a_bypass_whose_entry_carries_no_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`runs_open_in_order` checks every row, so a run that began with no numbered entry fails
    it: here a bypass whose writer drops the number, on a job that only ever bypassed."""

    def numberless(self: Any, job: str, outcome: Any) -> StatusLine:
        return StatusLine("ON_NOEXEC bypass (patched)")

    def replayed() -> StoreReplay:
        oracle = Oracle(lower_source(_LEAF.format(cmd="x")))
        oracle.feed(_ev(0, "ON_NOEXEC", job="j"))
        oracle.feed(_ev(1, "STARTJOB", job="j"))
        assert oracle.store.job["j"].run_number == 1
        return cast(StoreReplay, SimpleNamespace(oracle=oracle, anchor=None))

    runs_open_in_order(replayed())
    monkeypatch.setattr(oracle_writers.OracleWriters, "_w_bypass", numberless)
    with pytest.raises(AssertionError, match="'j', \\[\\], 1"):
        runs_open_in_order(replayed())


def test_a_read_error_while_the_root_closes_leaves_its_store_closed_and_its_lock_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revs 2 and 3 apply, a snapshot follows and `j` opens; reading rev 3's catalog hash at
    teardown fails. The failure reaches the caller, the store is closed and the root's lock can
    be taken again."""
    import sqlite3
    import sys

    from dsl41.runner_clock import EngineError
    from dsl41.runner_ledger import acquire_run_root

    read = load_configuration

    def refused(store: Any, rev: int, *, where: str) -> Any:
        if rev == 3:
            raise EngineError("injected read error")
        return read(store, rev, where=where)

    roots: list[_Root] = []

    async def body(root: _Root) -> None:
        roots.append(root)
        await root.update(1, "insert_job: j\njob_type: c\ncommand: y\n")
        await root.update(2, "insert_job: j\njob_type: c\ncommand: z\n")
        root.engine.take_snapshot()
        await root.feed(_ev(3, "STARTJOB", job="j"))
        monkeypatch.setattr(sys.modules[__name__], "load_configuration", refused)

    with pytest.raises(EngineError, match="injected read error"):
        _scenario(tmp_path, _LEAF.format(cmd="x"), body)
    [root] = roots
    journal = root.engine.journal
    assert journal is not None
    with pytest.raises(sqlite3.ProgrammingError):
        journal.store.last_seq()
    acquire_run_root(root.run).release()
