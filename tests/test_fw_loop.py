"""The self-recharging file watch, run end to end through the real runner.

An estate watches an inbox, processes each file that lands there, and watches again. The
FW adapter never re-arms itself (runner-design ss6), so a new watch needs a new start of
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
import os
import shlex
import sys
import time
from datetime import datetime
from collections.abc import Callable
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter, read_watch_log
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


async def _until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {WAIT_S}s waiting for {what}")
        await asyncio.sleep(0.05)


def test_sem10_fw_loop_box_rearmed_by_recycler_over_two_files(short_root: Path) -> None:
    """SEM-10, SEM-01: two files, two full cycles. Each file is watched by its own run of
    INBOX_FW, processed by its own run of PROCESS_C and moved to done/. After the second
    cycle the recycler has started the box a third time, and the watcher waits again."""
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
        server = ControlServer(engine, sock)
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "LOOP_B"}))
            store = engine.oracle.store.job
            await _until(lambda: store["INBOX_FW"].status == "RUNNING", "the first watch")

            _drop(inbox, "one")
            await _until(lambda: _successes(engine, "PROCESS_C") == 1, "the first processing")
            _drop(inbox, "two")
            await _until(lambda: _successes(engine, "PROCESS_C") == 2, "the second processing")
            await _until(
                lambda: store["INBOX_FW"].run_number == 3 and store["INBOX_FW"].status == "RUNNING",
                "the third watch",
            )
            # the operator started the box once; the recycler started it twice more
            assert _moves(engine, "LOOP_B", "->STARTING") == 3
            assert _moves(engine, "RECYCLE", "->STARTING") == 2
            assert _successes(engine, "INBOX_FW") == 2
            assert _successes(engine, "LOOP_B") == 2
            assert store["LOOP_B"].status == "RUNNING"
            assert store["PROCESS_C"].status == "INACTIVE"  # reset by the third box start
            assert store["PROCESS_C"].run_number == 2
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())

    # the processor's two runs, as the adapter reported them
    reported = [
        (r["payload"]["run_number"], r["payload"].get("exit_code"))
        for r in read_journal(run_root / "journal.jsonl")
        if r.get("kind") == "STATUS" and r.get("payload", {}).get("job") == "PROCESS_C"
    ]
    assert reported == [(1, 0), (2, 0)]

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
