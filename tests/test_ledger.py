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
import fcntl
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from dsl41.ir import lower_source
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_effects import OUTCOME_UNAVAILABLE
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


# ------------------------------------------------------------- 3. the fence


def test_a_replaced_lock_file_stops_the_engine_that_can_no_longer_prove_it_leads(
    tmp_path: Path,
) -> None:
    """ss7: proof is positive, and losing it stops dispatch rather than only
    renewal. Losing it here means the lock file was replaced under us --
    delete the name and the next engine creates a new inode, flocks it
    happily, and two leaders run. The first half of this test proves that
    danger is real rather than hypothetical; the second is the detector.

    A detector, not a preventer, and ss8 already says so of its sibling
    fence: it cannot un-run the duplicate, it stops it continuing and turns
    a silent divergence into a recorded incident."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    lock_path = run_root / LOCK_NAME

    async def scenario() -> None:
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))  # lands: still the leader
        before = (run_root / "journal.jsonl").read_bytes()

        lock_path.unlink()
        usurper = LeaderLock(run_root)
        usurper.acquire()  # nothing stopped it: this is the failure being caught
        try:
            engine.inject(_event("KILLJOB", 1))
            with pytest.raises(EngineError, match="was replaced"):
                await engine.run_until_quiescent(T0 + timedelta(minutes=2))
            # refused BEFORE the write: an engine that appended and then
            # noticed would have already admitted the input
            assert (run_root / "journal.jsonl").read_bytes() == before
        finally:
            usurper.release()

    asyncio.run(scenario())


def test_a_deleted_lock_file_stops_it_too(tmp_path: Path) -> None:
    """The same loss by the simpler route. Reported differently because an
    operator meets them differently: a missing file is something they can go
    and look for."""
    run_root = tmp_path / "run"
    engine = _start(run_root)

    async def scenario() -> None:
        (run_root / LOCK_NAME).unlink()
        engine.inject(_event("STARTJOB", 0))
        with pytest.raises(EngineError, match="was deleted"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())


def test_the_fence_stops_the_spawn_and_not_only_the_record(tmp_path: Path) -> None:
    """Why fencing appends is enough. Every effect is recorded before it is
    attempted (ss5), so an append this engine may not make is an effect it
    never applies -- no second mechanism, and no window between the two in
    which a fence could hold for one and not the other."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    (run_root / LOCK_NAME).unlink()

    async def scenario() -> None:
        engine.inject(_event("STARTJOB", 0))
        with pytest.raises(EngineError):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    assert engine.oracle.store.job["j"].run_number == 0
    assert list((run_root / "runs").iterdir()) == []
    assert engine.outbox.pending() == []


# ------------------------------------------------------- 4. eligibility (ss7)


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


# ------------------------------------------------- 5. the takeover barrier


def _log_without_spawn_result(run_root: Path) -> None:
    """Cut the SPAWN's outcome record: the previous leader died in the window
    between recording what it meant to do and doing it. The same technique
    DL-96 used for the undelivered kill, and the only honest way to build a
    crash window -- racing for one would test the scheduler, not the rule."""
    path = run_root / "journal.jsonl"
    records = read_journal(path)
    kept = [
        r
        for r in records
        if not (r.get("rec") == "effect_result" and str(r.get("effect_id", "")).find(":SPAWN:") > 0)
    ]
    assert len(kept) == len(records) - 1
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept))


def _crash_after_deciding_a_start(run_root: Path) -> None:
    async def scenario() -> None:
        engine = _start(run_root)
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_cm09_a_start_the_previous_leader_never_dispatched_is_re_driven(tmp_path: Path) -> None:
    """ss7's "re-drive pending", and the question DL-96 deferred to this
    barrier. A pending SPAWN with no trace anywhere -- no run directory, no
    dispatch record, nothing the host admits to running -- is an intent the
    previous leader recorded and did not get to. Nothing ran, so re-driving
    starts the run once, at the run_number the oracle already decided.

    Re-driving takes no code: leaving the effect pending is enough, because
    `_dispatch` drains the outbox through the same gates a fresh effect
    passes. A drained host therefore still holds it, and this sweep does not
    have to know that."""
    run_root = tmp_path / "run"
    _crash_after_deciding_a_start(run_root)
    _log_without_spawn_result(run_root)

    async def scenario():
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].status == "RUNNING"  # not FAILURE
    assert resumed.live_jobs() == frozenset({"j"})
    assert resumed.oracle.store.job["j"].run_number == 1  # the same run, not a second
    assert resumed.outbox.pending() == []  # the intent was discharged, not left over
    _close(resumed)


def test_a_start_with_no_recorded_intent_is_still_failed(tmp_path: Path) -> None:
    """The contrast, and the half of runner-design ss7 that stands. Same
    estate, same missing spool, one record different: the log says the spawn
    was applied, so nothing here is an intent waiting to be delivered. A
    journal written before the outbox existed reaches this branch too, and
    gets the behaviour it was written under."""
    run_root = tmp_path / "run"
    _crash_after_deciding_a_start(run_root)  # the effect_result stays

    async def scenario():
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].status == "FAILURE"
    assert resumed.live_jobs() == frozenset()
    _close(resumed)


def test_a_run_the_host_admits_to_is_never_re_driven(tmp_path: Path) -> None:
    """The guard that keeps the re-drive above from being the double run.
    "Never spawned" is concluded from ABSENCE, and absence that meant only
    "the run directory is gone" would let the barrier start a second process
    for a run the supervisor is still holding. ss7 reconciles every
    execution HOST, so what the host says it is running joins the sweep's
    candidate set beside what the disk shows."""
    run_root = tmp_path / "run"
    _crash_after_deciding_a_start(run_root)
    _log_without_spawn_result(run_root)

    class _HostWithTheRun:
        """A reachable supervisor that reports the run as live -- the state
        the disk cannot show once its directory is gone."""

        async def list_runs(self) -> dict:
            return {
                "runs": [{"job": "j", "run_number": 1, "run_id": "rid-1", "wrapper_alive": True}]
            }

    async def scenario():
        resumed = await resume_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            supervisor=_HostWithTheRun(),  # type: ignore[arg-type]
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    # nothing was launched: the barrier concluded the run had reached the
    # host and did not start a second one. (Reattaching to it is the
    # supervised adapter's job; a FakeAdapter has nothing to reattach to,
    # which is why the run ends up reported rather than resumed here.)
    assert resumed.live_jobs() == frozenset()
    assert resumed.oracle.store.job["j"].run_number == 1
    [outcome] = [r for r in resumed.outbox.effects() if r.kind == "SPAWN"]
    result = resumed.outbox.result_for(outcome.effect_id)
    assert result is not None and result.state == "applied"
    assert "reached the host" in (result.detail or "")
    _close(resumed)


def _event(kind: str, minutes: float):
    from dsl41.oracle_state import Event

    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload={"job": "j"})


# ---------------------------------- 6. what the lock says when it cannot lead


def test_a_lock_file_replaced_during_the_acquire_excludes_nobody(tmp_path: Path) -> None:
    """The race S6b's `check` covers at steady state, met at the acquire
    itself: between opening the file and locking it, the name can be pointed
    at a different inode. The lock then succeeds on an inode nothing else
    will ever open, so it excludes nobody -- and an engine that ran on it
    would believe it led a run root another engine could take."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    lock = LeaderLock(run_root)
    real_flock = fcntl.flock
    replaced: list[bool] = []

    def replace_then_lock(fd: int, op: int) -> None:
        real_flock(fd, op)
        if lock.path.exists():  # once, during the acquire we are testing
            lock.path.unlink()
            if replaced:
                lock.path.touch()  # pointed at a new inode...
            replaced.append(True)  # ...and, the first time, at nothing at all

    with mock.patch.object(fcntl, "flock", replace_then_lock):
        # gone entirely, then pointed at a different inode: both are "the name
        # we locked is not the name we hold" and both must refuse
        for _ in range(2):
            with pytest.raises(EngineError, match="was replaced while acquiring it"):
                lock.acquire()
            assert not lock.held  # and it did not keep the fd it could not use


def test_the_refusal_stays_useful_when_the_holder_note_is_not(tmp_path: Path) -> None:
    """The note is diagnostics, and diagnostics have to survive their own
    absence. A holder that acquired and died before writing one, or a
    truncated write, must still produce a refusal an operator can act on --
    the note is never the fence, so an unreadable one costs a name, not
    safety."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    holder = LeaderLock(run_root)
    holder.acquire()
    try:
        for garbage in (b"", b"{not json", b'"a string, not an object"'):
            holder.path.write_bytes(garbage)
            with pytest.raises(EngineError, match="held by another engine .holder unknown."):
                LeaderLock(run_root).acquire()
    finally:
        holder.release()


def test_using_a_lock_that_was_never_acquired_is_a_loud_error(tmp_path: Path) -> None:
    """Both are invariant guards on this class's own use, and they are loud
    rather than silent because the quiet versions are worse: a `note` that
    did nothing would leave the next refusal naming a stale holder, and a
    `check` that passed would be a fence that always says yes."""
    lock = LeaderLock(tmp_path)
    with pytest.raises(EngineError, match="not held"):
        lock.note(epoch=1, at=T0)
    with pytest.raises(EngineError, match="leadership was never acquired"):
        lock.check()


# ------------------------- 7. the ladder's remaining arms (DL-105)


def _resume_with(run_root: Path, at: datetime, jil: str, adapters: dict):
    return resume_run(
        lower_source(jil),
        run_root,
        clock=VirtualClock(start=at),
        adapters=adapters,
        settle_seconds=0.0,
        grace_seconds=0.0,
    )


def test_a_journal_stamped_in_the_future_refuses_to_resume(tmp_path: Path) -> None:
    """The machine clock moved backwards. Feeding a log whose instants are
    ahead of `now` would either feed time backwards -- which the oracle
    forbids -- or silently fast-forward every timer in the estate."""
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    _close(engine)
    path = run_root / "journal.jsonl"
    records = read_journal(path)
    records[0]["started_at"] = "2099-01-01T00:00:00"
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))

    async def scenario() -> None:
        with pytest.raises(EngineError, match="journal is from the future"):
            await resume_run(
                lower_source(_SOLO_JIL),
                run_root,
                clock=RealClock(),
                adapters={"CMD": FakeAdapter(default=None)},
            )

    asyncio.run(scenario())


def test_the_sweep_reads_what_is_there_and_ignores_what_is_not(tmp_path: Path) -> None:
    """The candidate sweep is a union over things that may each be missing or
    malformed: a run root with no `runs/` at all (nothing ever dispatched, or
    an operator cleaned up), and entries in it that are not run directories.
    Neither is corruption, so neither may raise -- reconciliation has to
    survive the state of a directory it does not own."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    (run_root / "runs").rmdir()  # gone entirely

    async def no_runs_dir() -> None:
        _close(await _resume(run_root, T0 + timedelta(minutes=1)))

    asyncio.run(no_runs_dir())

    (run_root / "runs").mkdir()
    (run_root / "runs" / "j.1").write_text("a file where a directory would be")
    (run_root / "runs" / "no-run-number").mkdir()
    (run_root / "runs" / "j.notanumber").mkdir()

    async def junk_in_runs_dir() -> None:
        _close(await _resume(run_root, T0 + timedelta(minutes=2)))

    asyncio.run(junk_in_runs_dir())


def test_a_run_this_catalog_has_no_job_for_is_left_alone(tmp_path: Path) -> None:
    """The log and the catalog disagreeing is not supposed to happen -- ss7's
    hash gate refuses a resume against a changed estate -- but the oracle
    invents a runtime row for any job it is TOLD about, so a log can carry a
    status for a name the catalog never had. Reconciliation cannot resolve
    such a run (there is no command to have run), so it leaves it rather than
    inventing a verdict about it."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    path = run_root / "journal.jsonl"
    records = read_journal(path)
    next_seq = max((int(r["seq"]) for r in records if "seq" in r), default=0) + 1
    records.append(
        {
            "rec": "input",
            "seq": next_seq,
            "at": T0.isoformat(),
            "request_id": "ghost-1",
            "fingerprint": "f",
            "epoch": 1,
            "kind": "STATUS",
            "payload": {"job": "ghost", "status": "RUNNING"},
            "source": "control",
        }
    )
    records.append(
        {
            "rec": "dispatch",
            "job": "ghost",
            # the run_number the oracle actually holds for it: an injected
            # STATUS never advances one (the ghost-run gate), so this is the
            # only pair that reaches the catalog lookup rather than stopping
            # at the run-number check above it
            "run_number": 0,
            "wrapper_pid": None,
            "run_dir": None,
            "started_at": T0.isoformat(),
        }
    )
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))

    async def scenario():
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["ghost"].status == "RUNNING"  # untouched, not failed
    _close(resumed)


_FW_JIL = "insert_job: watcher\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n"


def test_an_incomplete_watch_with_no_adapter_to_re_arm_it_refuses_loudly(tmp_path: Path) -> None:
    """An FW run is re-dispatched at resume because polling is an idempotent
    read -- so an engine resumed without the adapter that would do it has an
    incomplete watch it can neither finish nor honestly report. It refuses to
    start rather than leaving one hanging silently."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_FW_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FakeAdapter(default=None)},
        )
        engine.inject(_event_for("watcher", "STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["watcher"].status == "RUNNING"
        await engine.shutdown()
        _close(engine)

        with pytest.raises(EngineError, match="no FW adapter registered"):
            await _resume_with(run_root, T0 + timedelta(minutes=2), _FW_JIL, {})

    asyncio.run(scenario())


def test_a_job_whose_type_this_engine_cannot_dispatch_is_left_alone(tmp_path: Path) -> None:
    """Parity with the running engine, which would not have dispatched it
    either: an engine with no adapter for a job's type has no dispatch row
    for it live, so reconciliation must not invent a verdict about a run it
    could never have started. Not a failure -- an absence."""
    run_root = tmp_path / "run"

    async def scenario():
        engine = _start(run_root)
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()
        _close(engine)

        resumed = await _resume_with(
            run_root, T0 + timedelta(minutes=2), _SOLO_JIL, {"FW": FakeAdapter(default=None)}
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].status == "RUNNING"  # not FAILURE
    _close(resumed)


def _event_for(job: str, kind: str, minutes: float):
    from dsl41.oracle_state import Event

    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload={"job": job})


def test_a_kill_nobody_can_report_on_is_resolved_from_the_spool_as_indeterminate(
    tmp_path: Path,
) -> None:
    """ss5's third state, reached the way production reaches it. A kill was
    decided and its outcome never recorded; at resume there is no live
    wrapper to ask and no `status.json` to read. Nothing observed whether the
    signal landed, so `indeterminate` is the only honest answer -- reporting
    it either way would invent a fact about a process nothing watched (E7).
    Two states would have to call this one of the two things it is not."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_SOLO_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (3600.0, 0)})},
        )
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.live_jobs() == frozenset({"j"})
        engine.inject(_event("KILLJOB", 2))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())

    path = run_root / "journal.jsonl"
    records = read_journal(path)
    kept = [
        r
        for r in records
        if not (r.get("rec") == "effect_result" and ":KILL:" in str(r.get("effect_id", "")))
    ]
    assert len(kept) == len(records) - 1  # the crash landed between deciding and recording
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in kept))

    async def after():
        resumed = await _resume(run_root, T0 + timedelta(minutes=5))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=6))
        return resumed

    resumed = asyncio.run(after())
    [kill] = [e for e in resumed.outbox.effects() if e.kind == "KILL"]
    assert resumed.outbox.state_of(kill.effect_id) == "indeterminate"
    # and what an exact retry of it is told, which is the reason the third
    # state exists: not a failure, not a success, an answer nobody can give
    assert resumed.outbox.result_for(kill.effect_id) == OUTCOME_UNAVAILABLE
    assert resumed.outbox.pending() == []  # answered, not left hanging
    _close(resumed)


_BOX_JIL = """\
insert_job: bx
job_type: b

insert_job: member
job_type: c
command: x
box_name: bx
"""


def test_a_running_box_is_not_a_start_the_barrier_can_have_lost(tmp_path: Path) -> None:
    """A box is RUNNING with no dispatch trace by design -- it has no command
    and nothing ever spawned for it; its status folds from its members. The
    sweep looks for decisions that left no trace, so a box matches its shape
    exactly while being the one thing that is not a lost start. Failing it
    would take the box down and, with it, every member still to run."""
    run_root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            lower_source(_BOX_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_event_for("bx", "STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["bx"].status == "RUNNING"
        await engine.shutdown()
        _close(engine)

        resumed = await _resume_with(
            run_root, T0 + timedelta(minutes=2), _BOX_JIL, {"CMD": FakeAdapter(default=None)}
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
        return resumed

    resumed = asyncio.run(scenario())
    _close(resumed)
    # the MEMBER's start was lost and is failed; the box is never addressed by
    # the sweep at all. Its status follows from its members afterwards, which
    # is the fold doing its job rather than a verdict about the box.
    reconciled = {
        r["payload"]["job"]
        for r in read_journal(run_root / "journal.jsonl")
        if r.get("rec") == "input" and r.get("source") == "reconcile"
    }
    assert reconciled == {"member"}
