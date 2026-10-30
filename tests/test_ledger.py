"""One engine per run root: the lock and eligibility (S6a).

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

from contextlib import closing

import asyncio
import fcntl
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
from proc_pipes import close_pipes

from dsl41.ir import lower_source
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import LOCK_NAME, STATE_MACHINE_VERSION, LeaderLock
from dsl41.runner_startup import resume_run, start_run
from store_surgery import append, delete, rows

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

        # a resume meets the same wall, and it is the one that matters: the
        # create path already refuses a used run root on its own
        with pytest.raises(EngineError, match="held by another engine"):
            asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
    finally:
        _close(engine)

    # the contrast: released, the next engine runs
    resumed = asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
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
        before = rows(run_root)
        with pytest.raises(EngineError, match="held by another engine"):
            asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
        assert rows(run_root) == before  # not one record
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
                lock.note(at=__import__("datetime").datetime.now())
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
        close_pipes(child)


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
        before = rows(run_root)

        lock_path.unlink()
        usurper = LeaderLock(run_root)
        usurper.acquire()  # nothing stopped it: this is the failure being caught
        try:
            engine.inject(_event("KILLJOB", 1))
            with pytest.raises(EngineError, match="was replaced"):
                await engine.run_until_quiescent(T0 + timedelta(minutes=2))
            # refused BEFORE the write: an engine that appended and then
            # noticed would have already admitted the input
            assert rows(run_root) == before
        finally:
            usurper.release()

    asyncio.run(scenario())
    _close(engine)


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
    _close(engine)


def test_the_fence_stops_the_spawn_and_not_only_the_record(tmp_path: Path) -> None:
    """Why fencing appends is enough. Every start is recorded before it is
    attempted (runner-design ss7), so an append this engine may not make is
    a start it never makes -- no second mechanism, and no window between the
    two in which a fence could hold for one and not the other."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    (run_root / LOCK_NAME).unlink()

    async def scenario() -> None:
        engine.inject(_event("STARTJOB", 0))
        with pytest.raises(EngineError):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    _close(engine)
    assert engine.oracle.store.job["j"].run_number == 0
    assert list((run_root / "runs").iterdir()) == []
    assert engine.live_jobs() == frozenset()


# ------------------------------------------------------- 4. eligibility (ss7)


def test_the_store_pins_the_state_machine_version(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    _close(_start(run_root))
    segment = read_journal(run_root)[0]
    assert segment["state_machine_version"] == STATE_MACHINE_VERSION


def _set_state_version(run_root: Path, version: int) -> None:
    import sqlite3

    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE meta SET value = ? WHERE key = 'state_version'", (str(version),))


def test_a_build_that_derives_different_state_may_not_lead_this_store(tmp_path: Path) -> None:
    """DL-424: a store's `meta.state_version` is written at creation, before the first input,
    so a binary with another version refuses to start naming both, even when no snapshot
    exists yet. Mixed builds derive different revisions from identical inputs and nothing
    downstream can detect the disagreement."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    _set_state_version(run_root, STATE_MACHINE_VERSION + 1)
    with pytest.raises(EngineError) as refused:
        asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))
    assert str(refused.value) == (
        f"{run_root / 'engine.db'}: state version {STATE_MACHINE_VERSION + 1}, this binary"
        f" runs {STATE_MACHINE_VERSION}: drain and snapshot with the binary that wrote it, or"
        " start a fresh run root (DL-424)"
    )


def test_a_snapshot_another_build_wrote_is_refused_naming_both_versions(tmp_path: Path) -> None:
    """A snapshot carries the version it was taken under; a binary loads only its own and
    replays nothing across versions (no migration in 2.0, DL-424)."""
    run_root = tmp_path / "run"
    engine = _start(run_root)
    assert engine.journal is not None
    engine.journal.store.put_snapshot(engine.journal.last_seq, STATE_MACHINE_VERSION - 1, b"{}")
    _close(engine)
    with pytest.raises(EngineError, match=f"state version {STATE_MACHINE_VERSION - 1}, this"):
        asyncio.run(_resume(run_root, T0 + timedelta(minutes=1)))


def test_an_older_build_s_store_is_refused_for_its_version_not_its_hash(
    tmp_path: Path,
) -> None:
    """DL-253: a store an older build wrote pins that build's state version; the version is
    checked first, so the refusal names it and never says the estate changed, even against a
    changed estate."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    _set_state_version(run_root, STATE_MACHINE_VERSION - 1)
    changed = lower_source(_SOLO_JIL.replace("command: x", "command: y"))
    with pytest.raises(EngineError, match="state version") as refused:
        asyncio.run(
            resume_run(
                changed,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
            )
        )
    assert "catalog hash mismatch" not in str(refused.value)


def test_a_changed_estate_refuses(tmp_path: Path) -> None:
    """The gate's own reason for existing: a changed estate is an update (3b), never a silent
    drift (ss7)."""
    run_root = tmp_path / "run"
    _close(_start(run_root))
    changed = lower_source(_SOLO_JIL.replace("command: x", "command: y"))
    with pytest.raises(EngineError, match="catalog hash mismatch"):
        asyncio.run(
            resume_run(
                changed,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
            )
        )


# ------------------------------------------------- 5. the takeover barrier


def _log_without_dispatch(run_root: Path) -> None:
    """Cut the start's `dispatch` record: the previous engine died in the
    window between deciding the start and dispatching it. The only honest
    way to build a crash window -- racing for one would test the scheduler,
    not the rule."""
    assert len(delete(run_root, lambda seq, kind, body: kind == "dispatch")) == 1


def _crash_after_deciding_a_start(run_root: Path) -> None:
    async def scenario() -> None:
        engine = _start(run_root)
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())


def test_cm09_a_start_the_previous_engine_never_dispatched_is_dispatched(tmp_path: Path) -> None:
    """DL-102. A decided start with no trace anywhere -- no run directory,
    no `dispatch` record -- is one the previous engine decided and did not
    get to. Nothing ran, so resume starts the run once, at the run_number
    the oracle already decided, through the same supersession check a fresh
    start passes."""
    run_root = tmp_path / "run"
    _crash_after_deciding_a_start(run_root)
    _log_without_dispatch(run_root)

    async def scenario():
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].status == "RUNNING"  # not FAILURE
    assert resumed.live_jobs() == frozenset({"j"})
    assert resumed.oracle.store.job["j"].run_number == 1  # the same run, not a second
    _close(resumed)
    dispatched = [r for r in read_journal(run_root) if r["rec"] == "dispatch"]
    assert [(r["job"], r["run_number"]) for r in dispatched] == [("j", 1)]


def test_a_dispatched_start_that_left_no_trace_is_run_again_as_its_next_attempt(
    tmp_path: Path,
) -> None:
    """The contrast. Same estate, same missing spool, one record different: the store says
    the attempt was dispatched, and it left no record and no process, so it is unobservable.
    The interruption rule runs the idempotent job's run again as attempt 2, with the same
    invocation, and the oracle is told nothing (DL-424)."""
    run_root = tmp_path / "run"
    _crash_after_deciding_a_start(run_root)  # the dispatch record stays

    async def scenario():
        resumed = await _resume(run_root, T0 + timedelta(minutes=1))
        await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
        return resumed

    resumed = asyncio.run(scenario())
    assert resumed.oracle.store.job["j"].status == "RUNNING"
    assert resumed.oracle.store.job["j"].run_number == 1
    assert resumed.live_jobs() == frozenset({"j"})
    _close(resumed)
    outcomes = [body for _, kind, body in rows(run_root) if kind == "outcome"]
    assert [o["outcome"] for o in outcomes] == ["unobservable"]
    ordinals = [body["ordinal"] for _, kind, body in rows(run_root) if kind == "dispatch"]
    assert ordinals == [1, 2]


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
        lock.note(at=T0)
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
    append(  # a record stamped ahead of the machine clock
        run_root,
        "drop",
        {"at": "2099-01-01T00:00:00", "kind": "STARTJOB", "payload": {}, "reason": "x"},
        at=datetime(2099, 1, 1),
    )

    async def scenario() -> None:
        with pytest.raises(EngineError, match="the store is from the future"):
            await resume_run(
                lower_source(_SOLO_JIL),
                run_root,
                clock=RealClock(),
                adapters={"CMD": FakeAdapter(default=None)},
            )

    asyncio.run(scenario())


def test_the_sweep_reads_what_is_there_and_ignores_what_is_not(tmp_path: Path) -> None:
    """The restart reads the spools the open attempts name and nothing else: a run root with
    no `runs/` at all (nothing ever dispatched, or an operator cleaned up), and entries in it
    that are not run directories, are neither corruption nor evidence, so neither may raise --
    reconciliation has to survive the state of a directory it does not own."""
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
    body = {
        "index": 1,
        "at": T0.isoformat(),
        "request_id": "ghost-1",
        "fingerprint": "f",
        "kind": "STATUS",
        "payload": {"job": "ghost", "status": "RUNNING"},
        "source": "control",
    }
    append(run_root, "input", body, at=T0)
    append(
        run_root,
        "decision",
        {
            "index": 1,
            "request_id": "ghost-1",
            "fingerprint": "f",
            "decision": "applied",
            "reason": None,
            "code": None,
            "revisions": {"job:ghost": 1},
        },
        at=T0,
        request_id="ghost-1",
    )
    (run_root / "runs" / "ghost.0").mkdir(parents=True)  # a directory no attempt names

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

        # since DL-130 the PINNED root refuses one gate earlier -- before
        # replay, naming the missing type -- which is the same loud refusal
        # this test always pinned, moved to where nothing has mutated yet
        with pytest.raises(EngineError, match="no adapter"):
            await _resume_with(run_root, T0 + timedelta(minutes=2), _FW_JIL, {})

    asyncio.run(scenario())


def test_a_job_whose_type_this_engine_cannot_dispatch_never_reaches_reconciliation(
    tmp_path: Path,
) -> None:
    """An engine with no adapter for a job's type is refused BEFORE replay.

    This used to pin the reconciliation ladder's left-alone parity -- an
    engine that could never have started the run must not invent a verdict
    about it -- and reached that branch through the header-journal twin a
    pre-DL-130 estate was, because a pinned root already refused the missing
    adapter first. DL-134 closed that route and DL-138 deleted the dialect
    it went through. What is pinned here is that the one route left refuses
    before anything replays (DL-134's entry records the ladder branch left
    unreachable; DL-138 deleted it)."""
    run_root = tmp_path / "run"

    async def scenario():
        engine = _start(run_root)
        engine.inject(_event("STARTJOB", 0))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()
        _close(engine)
        with pytest.raises(EngineError, match="wired no adapter"):
            await _resume_with(
                run_root, T0 + timedelta(minutes=2), _SOLO_JIL, {"FW": FakeAdapter(default=None)}
            )

    asyncio.run(scenario())


def _event_for(job: str, kind: str, minutes: float):
    from dsl41.oracle_state import Event

    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload={"job": job})


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
    # the MEMBER's attempt was lost and is run again; the box is never addressed by the
    # restart at all, because it has no attempt. Its status follows from its members, which
    # is the fold doing its job rather than a verdict about the box.
    settled = {body["job"] for _, kind, body in rows(run_root) if kind == "outcome"}
    assert settled == {"member"}


def test_dl210_flock_exclusive_is_shared_and_keeps_the_lock_file(tmp_path: Path):
    from dsl41.runner_procid import LockHeld, flock_exclusive

    path = tmp_path / "shared.lock"
    fd = flock_exclusive(path)
    inode = path.stat().st_ino
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(LockHeld):
            flock_exclusive(path)
    finally:
        os.close(fd)
    successor = flock_exclusive(path)
    try:
        assert path.stat().st_ino == inode
    finally:
        os.close(successor)


def test_dl210_flock_exclusive_closes_fd_on_stat_failure(tmp_path: Path):
    from dsl41 import runner_procid

    path = tmp_path / "shared.lock"
    opened = []
    original = os.open

    def track(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    with (
        mock.patch.object(runner_procid.os, "open", track),
        mock.patch.object(runner_procid.os, "stat", side_effect=PermissionError("stat refused")),
        pytest.raises(PermissionError),
    ):
        runner_procid.flock_exclusive(path)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    fd = runner_procid.flock_exclusive(path)
    os.close(fd)
