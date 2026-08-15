"""Leadership over one run root: the lock, the epoch, eligibility (S6a).

Normative spec: docs/concurrency-model.md ss1 (the ledger's five
capabilities), ss7 (leadership, and what the header pins). `ssN` in this
file always names concurrency-model.

The hole this closes is live, and it is worth stating plainly because the
tests below only make sense against it. `_serve_run` claims the control
socket AFTER `resume_run` has replayed the log, reconciled the estate,
re-driven recorded kills and appended to the WAL. Two `dsl41 run --resume`
processes on one run root therefore both act, in full, before either is
refused -- and the refusal is a 0.2-second connect probe that UNLINKS a
socket it cannot reach, so an engine wedged past that timeout loses its
socket to a second engine. A mutex taken after the first side effect is
not a mutex (DL-99).

Two properties carry the rest of the file:

  * **Acquire precedes every act.** Not merely every append: the refused
    resume below must leave the log, the estate and the spool exactly as it
    found them, which is what makes the ordering testable rather than
    asserted.
  * **The mutex needs no liveness heuristic.** The kernel releases an
    `flock` when the holder dies, `kill -9` included. The last test spends
    a real subprocess on that, because it is the entire reason to prefer a
    lock to the probe it replaces.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.runner import resume_run, start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import (
    LOCK_NAME,
    STATE_MACHINE_VERSION,
    LeaderLock,
    check_leader_eligibility,
    next_epoch,
)

T0 = datetime(2026, 7, 1, 8, 0)
_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"


def _start(run_root: Path, at: datetime = T0):
    return start_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
    )


async def _resume(run_root: Path, at: datetime):
    return await resume_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
    )


def _close(engine) -> None:
    assert engine.journal is not None
    engine.journal.close()


# ------------------------------------------------------------- 1. the mutex


def test_a_second_engine_on_one_run_root_is_refused(tmp_path: Path) -> None:
    """One leader per run root, and the refusal names the holder -- an
    operator who is told only "refused" goes looking for a stale file to
    delete, which is the one thing that would actually break this."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    try:
        with pytest.raises(EngineError, match="held by another engine") as refused:
            _start(run_root)
        assert f"pid {os.getpid()}" in str(refused.value)
        assert "epoch 1" in str(refused.value)

        # a resume meets the same wall, and it is the one that matters: the
        # create path already refuses a used run root on its own
        with pytest.raises(EngineError, match="held by another engine"):
            asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
    finally:
        _close(engine)

    # the contrast: released, the next engine leads
    resumed = asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
    assert resumed.epoch == 2
    _close(resumed)


def test_a_refused_resume_leaves_the_log_and_the_estate_untouched(tmp_path: Path) -> None:
    """The ordering property, and the reason ss7's barrier begins at
    ACQUIRE. `resume_run` replays, reconciles, re-drives recorded kills and
    appends; every one of those is an act on the estate. A mutex taken
    after them would refuse the second engine only once it had already done
    what the refusal exists to prevent."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    try:
        before = (run_root / "journal.jsonl").read_bytes()
        with pytest.raises(EngineError, match="held by another engine"):
            asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
        assert (run_root / "journal.jsonl").read_bytes() == before  # not one record
        assert list((run_root / "runs").iterdir()) == []
    finally:
        _close(engine)


def test_a_refused_resume_does_not_hold_the_lock_it_could_not_use(tmp_path: Path) -> None:
    """A resume that gets past the acquire and then fails a gate must let
    go. Holding on would turn one bad estate file into a run root no engine
    can ever lead again, which is a worse failure than the one it reports."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    changed = lower_source(_SOLO_JIL.replace("command: x", "command: y"))

    async def scenario() -> None:
        with pytest.raises(EngineError, match="catalog hash mismatch"):
            await resume_run(
                changed,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
            )
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        assert resumed.epoch == 2  # the refused attempt spent no term either
        _close(resumed)

    asyncio.run(scenario())


def test_the_lock_is_released_when_the_log_is_closed(tmp_path: Path) -> None:
    """The ledger is the log plus the lock that says who may append to it,
    so closing one closes both. An engine that dropped the file and kept
    the lock would exclude its own successor."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    lock_path = run_root / LOCK_NAME
    assert lock_path.exists()
    _close(engine)

    probe = LeaderLock(run_root)
    probe.acquire()  # free
    assert probe.held
    probe.release()
    probe.release()  # idempotent
    assert not probe.held
    # released, never unlinked: unlinking is the one thing S6b's check exists
    # to catch, and a lock file left behind excludes nobody
    assert lock_path.exists()


def test_a_dead_holder_leaves_nothing_to_clean_up(tmp_path: Path) -> None:
    """The whole reason to prefer a lock to the connect probe it replaces.
    `kill -9` is the case a heuristic gets wrong in both directions -- an
    engine merely slow to answer looks dead, and a truly dead one leaves a
    file somebody has to decide about. The kernel decides here, and it is
    never wrong."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                import sys, time
                from pathlib import Path
                from dsl41.runner_ledger import LeaderLock
                lock = LeaderLock(Path({str(run_root)!r}))
                lock.acquire()
                lock.note(epoch=7, at=__import__("datetime").datetime.now())
                print("held", flush=True)
                time.sleep(60)
            """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        held = LeaderLock(run_root)
        with pytest.raises(EngineError, match=f"pid {child.pid}"):
            held.acquire()
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        deadline = time.monotonic() + 5
        while True:  # the kernel releases on exit, not on our schedule
            try:
                held.acquire()
                break
            except EngineError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        held.release()
    finally:
        if child.poll() is None:  # pragma: no cover -- only on an assert above
            child.kill()
            child.wait(timeout=10)


# ------------------------------------------------------------- 2. the epoch


def test_the_epoch_is_allocated_by_being_appended(tmp_path: Path) -> None:
    """ss1's monotone allocation. Written under the lock, in the log, so the
    allocation and the log's account of it are one write and cannot
    disagree -- and every input between two `leader` records names the
    incarnation the earlier one names, which is what makes a failover
    reconstructible after the fact."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    _close(asyncio.run(_resume(run_root, T0 + timedelta(minutes=1))))
    third = asyncio.run(_resume(run_root, T0 + timedelta(minutes=2)))
    assert third.epoch == 3
    _close(third)

    terms = [r for r in read_journal(run_root / "journal.jsonl") if r.get("rec") == "leader"]
    assert [r["epoch"] for r in terms] == [1, 2, 3]
    assert all(r["pid"] == os.getpid() for r in terms)
    assert all(r["host"] and r["dsl41_version"] for r in terms)


def test_every_input_names_the_term_that_admitted_it(tmp_path: Path) -> None:
    """The epoch on an attempt is the LEADER's, not the caller's, so an
    input raised inside the engine carries it too. That is what makes the
    log self-describing about incarnations rather than only about clients."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start(run_root)
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()
        _close(engine)

        resumed = await _resume(run_root, T0 + timedelta(minutes=2))
        resumed.inject(_event("KILLJOB", 3))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=4))
        await resumed.shutdown()
        _close(resumed)

    asyncio.run(scenario())
    records = read_journal(run_root / "journal.jsonl")
    epochs = {r["epoch"] for r in records if r.get("rec") in ("input", "advance", "host")}
    assert epochs == {1, 2}


def test_a_journal_written_before_s6a_takes_the_first_term() -> None:
    """No format gate, on the courtesy S2 gave a journal with no
    `request_id`: a log with no `leader` record has had no term held over
    it, so the first real one is 1 -- the same number a fresh run root
    gets, and for the same reason."""
    assert next_epoch([]) == 1
    assert next_epoch([{"rec": "header"}, {"rec": "input", "seq": 1, "epoch": 0}]) == 1
    assert next_epoch([{"rec": "leader", "epoch": 4}, {"rec": "input", "epoch": 4}]) == 5


# ------------------------------------------------------- 3. eligibility (ss7)


def test_the_header_pins_the_state_machine_version(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _close(_start(run_root))
    header = read_journal(run_root / "journal.jsonl")[0]
    assert header["state_machine_version"] == STATE_MACHINE_VERSION


def test_a_build_that_derives_different_state_may_not_lead_this_log(tmp_path: Path) -> None:
    """ss7: eligibility is an exact match on BOTH pins. Mixed builds derive
    different revisions from identical inputs and nothing downstream can
    detect the disagreement -- a SPAWN spec is a resolved literal command
    string, and the supervisor holds no job definitions."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    path = run_root / "journal.jsonl"
    records = read_journal(path)
    records[0]["state_machine_version"] = STATE_MACHINE_VERSION + 1
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))

    with pytest.raises(EngineError, match="state-machine version mismatch"):
        asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))


def test_a_header_that_pins_no_version_reads_as_the_one_that_defined_it() -> None:
    """A journal written before S6a. Refusing it would make the gate's first
    act an outage on every run root in existence."""
    check_leader_eligibility({"catalog_hash": "h"}, expected_catalog_hash="h")
    with pytest.raises(EngineError, match="catalog hash mismatch"):
        check_leader_eligibility({"catalog_hash": "h"}, expected_catalog_hash="other")


def _event(kind: str, minutes: float):
    from dsl41.oracle_state import Event

    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload={"job": "j"})
