"""The self-recharging file watch, run end to end through the real runner.

An estate watches an inbox, processes each file that lands there, and watches again. The
FW adapter completes once per run (runner-design ss6), so a new watch needs a new start of
the watcher. This estate gets it the way an AutoSys estate does: the watcher and the
processor sit in a box, and a recycler outside the box sends STARTJOB to the box when the
box succeeds. Each box start resets its members (SEM-10, DL-242), so `s(INBOX_FW)` waits
for the new watch, not the old one.

Waits poll the engine's in-memory trace under a deadline; no assertion reads a real-clock
offset. The test writes each file atomically (write beside, then rename into the inbox),
so every poll sees the whole file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    WATCH_LOG,
    FileWatcherAdapter,
    LocalCommandAdapter,
    read_watch_log,
)
from dsl41.runner_admission import ENGINE_REQUEST_ID_PREFIX
from dsl41.runner_clock import RealClock
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import start_run

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("the adapters and the control socket are POSIX-only", allow_module_level=True)

#: Each wait's bound. One cycle is two one-second polls, one processor run and one CLI
#: round trip, so this leaves room for a slow host and keeps the test under 60 s.
WAIT_S = 15.0


def _estate(inbox: Path, done: Path, sock: Path) -> str:
    watched = inbox / "data.txt"
    sendevent = f"{shlex.quote(sys.executable)} -m dsl41 sendevent STARTJOB -S {sock} -J LOOP_B"
    return (
        "insert_job: LOOP_B\njob_type: b\n\n"
        "insert_job: INBOX_FW\njob_type: f\nbox_name: LOOP_B\n"
        f"watch_file: {watched}\nwatch_interval: 1\nwatch_file_min_size: 1\n\n"
        "insert_job: PROCESS_C\njob_type: c\nbox_name: LOOP_B\ncondition: s(INBOX_FW)\n"
        f'command: mv {watched} "{done}/$(cat {watched})"\nmachine: m1\n\n'
        "insert_job: RECYCLE\njob_type: c\ncondition: s(LOOP_B)\n"
        f"command: {sendevent}\nmachine: m1\n"
    )


def _drop(inbox: Path, name: str) -> None:
    staged = inbox.parent / f".{name}"
    staged.write_text(name)
    os.replace(staged, inbox / "data.txt")


def _moves(engine: Engine, job: str, suffix: str) -> int:
    """How many of `job`'s traced transitions end in `suffix`."""
    return sum(1 for e in engine.oracle.trace() if e.job == job and e.transition.endswith(suffix))


def _successes(engine: Engine, job: str) -> int:
    return _moves(engine, job, "RUNNING->SUCCESS")


def _polls(run_root: Path, run_number: int) -> list[dict[str, Any]]:
    """The poll lines of one INBOX_FW run's watch log, in order."""
    path = run_root / "runs" / f"INBOX_FW.{run_number}" / WATCH_LOG
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [line for line in lines if line["kind"] == "poll"]


def _watch_waited(run_root: Path, run_number: int) -> bool:
    return any(not poll["exists"] for poll in _polls(run_root, run_number))


async def _stop(loop_task: asyncio.Task[Any]) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task


async def _until(predicate: Callable[[], bool], what: str, loop_task: asyncio.Task[Any]) -> None:
    """Wait for `predicate` under WAIT_S; fail at once if the engine loop has ended."""
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if loop_task.done():
            cause = None if loop_task.cancelled() else loop_task.exception()
            raise AssertionError(f"the engine loop ended while waiting for {what}") from cause
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {WAIT_S}s waiting for {what}")
        await asyncio.sleep(0.05)


def test_sem10_fw_loop_box_rearmed_by_recycler_over_two_files(short_root: Path) -> None:
    """SEM-10, DL-242, SEM-01: two files, two full cycles. Each file is watched by its own
    run of INBOX_FW, which polls at least once before the file lands, then is processed by
    its own run of PROCESS_C and moved to done/. After the second cycle the recycler has
    started the box a third time, and the watcher waits again."""
    base = short_root
    inbox, done = base / "inbox", base / "done"
    inbox.mkdir()
    done.mkdir()
    run_root = base / "run"
    sock = run_root / "control.sock"

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(
            lower_source(_estate(inbox, done, sock)),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            hold_open=True,
        )
        journal = engine.journal
        assert journal is not None
        async with contextlib.AsyncExitStack() as teardown:  # each step runs if one fails
            teardown.callback(journal.close)
            teardown.push_async_callback(engine.shutdown)
            server = ControlServer(engine, sock)
            await server.start()
            teardown.push_async_callback(server.close)
            loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            teardown.push_async_callback(_stop, loop_task)

            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "LOOP_B"}))
            store = engine.oracle.store.job
            for run_number, name in ((1, "one"), (2, "two")):
                # the re-armed watch is running and has seen the empty inbox
                await _until(
                    lambda: (
                        store["INBOX_FW"].run_number == run_number
                        and store["INBOX_FW"].status == "RUNNING"
                        and _watch_waited(run_root, run_number)
                    ),
                    f"watch {run_number} to wait",
                    loop_task,
                )
                _drop(inbox, name)
                await _until(
                    lambda: _successes(engine, "PROCESS_C") == run_number,
                    f"processing {run_number}",
                    loop_task,
                )
            await _until(lambda: _successes(engine, "RECYCLE") == 2, "the recycler", loop_task)
            await _until(
                lambda: store["INBOX_FW"].run_number == 3 and store["INBOX_FW"].status == "RUNNING",
                "the third watch",
                loop_task,
            )
            # the operator started the box once; the recycler started it twice more
            assert _moves(engine, "LOOP_B", "->STARTING") == 3
            assert _moves(engine, "RECYCLE", "->STARTING") == 2
            assert _successes(engine, "INBOX_FW") == 2
            assert _successes(engine, "LOOP_B") == 2
            assert store["LOOP_B"].status == "RUNNING"
            assert store["PROCESS_C"].status == "INACTIVE"  # reset by the third box start
            assert store["PROCESS_C"].run_number == 2

    asyncio.run(scenario())

    records = read_journal(run_root)

    def reported(job: str) -> list[tuple[int, int | None]]:
        return [
            (r["payload"]["run_number"], r["payload"].get("exit_code"))
            for r in records
            if r.get("kind") == "STATUS" and r["payload"].get("job") == job
        ]

    # each job's runs, as the adapter reported them
    assert reported("PROCESS_C") == [(1, 0), (2, 0)]
    assert reported("RECYCLE") == [(1, 0), (2, 0)]

    # the test's own start is engine-made; the recycler's two came over the socket with a
    # client request id and the revision the CLI read (control-protocol ss3)
    starts = [
        r for r in records if r.get("kind") == "STARTJOB" and r["payload"].get("job") == "LOOP_B"
    ]
    assert [r["request_id"].startswith(ENGINE_REQUEST_ID_PREFIX) for r in starts] == [
        True,
        False,
        False,
    ]
    assert ["expect" in r for r in starts] == [False, True, True]

    # each watch polled the empty inbox before its file landed
    for run_number in (1, 2):
        polls = _polls(run_root, run_number)
        first_seen = next(i for i, poll in enumerate(polls) if poll["exists"])
        assert first_seen > 0
        assert not any(poll["exists"] for poll in polls[:first_seen])

    # two completed watches, one per run directory; the third is dispatched and waiting
    for run_number in (1, 2):
        fold = read_watch_log(run_root / "runs" / f"INBOX_FW.{run_number}")
        assert fold is not None and fold.complete
    third = read_watch_log(run_root / "runs" / "INBOX_FW.3")
    assert third is not None and not third.complete

    # both files consumed: moved to done/, the inbox empty
    assert sorted(p.name for p in done.iterdir()) == ["one", "two"]
    assert (done / "one").read_text() == "one"
    assert list(inbox.iterdir()) == []
