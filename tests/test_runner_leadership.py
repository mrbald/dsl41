"""Stage S7c: S5's routing table and S6's election under real processes.

S0-S4 each got a real-process tier as they landed -- the wrapper's kill
matrix, the supervisor's lease and fencing, the deadman, engine SIGKILL
tethered and detached. S5 and S6 did not. Everything those stages shipped
with drives one engine in-process under a virtual clock, which is the right
instrument for interleavings (S7a/S7b) and the wrong one for the four
claims below. Each is a claim about processes -- two of them, or one of
them and the kernel -- and none of it is observable from inside a single
interpreter:

- **the mutex is an `flock`**, so a refusal needs a second OS process to
  be refused, "the first engine is untouched" needs it to still be serving
  afterwards, and "the kernel releases it" needs a holder that dies without
  releasing anything;
- **the fence is an inode comparison**, so losing proof needs a real
  `unlink` under a live engine, and the consequence to check is that
  nothing started -- against the same command, on the same engine,
  starting something a moment earlier;
- **the outbox window is between two statements**, so a pending SPAWN
  needs a process that really died there and a spool that really has
  nothing in it;
- **the eviction bound's inputs are produced, not configured** -- a
  deadman read back off a live supervisor, a `last_contact` stamped by a
  lease exchange that really landed, a quarantine reached only after the
  renewal loop really gave up.

Seconds, not minutes. Nothing here waits out a bound: what waiting out the
real `T_kill` would prove is arithmetic, and the arithmetic is pinned under
a controlled clock in test_hosts.py. The one deliberate slow spot is the
quarantine: the loop gives up on the FIFTH consecutive failure, and only
the first of those five waits `_RENEW_EVERY_S` (shortened here) -- the
other four wait a hardcoded second each. That ~4.2 s is the property, not
overhead, so it is paid and asserted rather than shortened away.

**Every claim about a mechanism carries a positive control.** An empty
`runs/` proves nothing on its own: it is also what a build where nothing
ever starts would produce. So the fence test starts a job through the very
path it then breaks, the re-drive test reads the job's own stdout, and the
quarantine test waits for a real lease exchange to move `last_contact`
before killing the thing that was stamping it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("leadership tier is POSIX-only", allow_module_level=True)

from runner_redrive_driver import CRASH_CODE, REDRIVE_JIL

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import (
    FileWatcherAdapter,
    LocalCommandAdapter,
    SupervisedCommandAdapter,
    SupervisorClient,
)
from dsl41.runner_clock import RealClock
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, T_KILL_S, HostCommand, skew_allowance
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import LOCK_NAME
from dsl41.runner_startup import resume_run, start_run

REDRIVE_DRIVER = Path(__file__).parent / "runner_redrive_driver.py"

IDLE_JIL = "insert_job: idle\njob_type: c\ncommand: sleep 600\n"

#: two jobs so the fence test can carry its own control: `proof` starts
#: through the intact engine, `after` is asked for once the lock is gone.
FENCE_JIL = (
    "insert_job: proof\njob_type: c\ncommand: sleep 600\n\n"
    "insert_job: after\njob_type: c\ncommand: sleep 600\n"
)


@pytest.fixture
def short_root():
    """A short base dir: an engine binds <run_root>/control.sock, and
    pytest's tmp_path overruns sun_path's 104-byte macOS limit (same
    workaround as the supervisor tier's fixture)."""
    d = tempfile.mkdtemp(prefix="dsl41l-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def wait_for(predicate, timeout_s: float = 15.0, interval_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


def cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """One `dsl41` invocation. Bounded, because an unbounded one hits
    pytest's faulthandler timeout, which kills the SESSION and prints no
    report -- the least useful way for a hung engine to be reported."""
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args], capture_output=True, text=True, timeout=timeout
    )


class RunProcess:
    """A real `dsl41 run` process, up to the line where it says it is up."""

    _n = 0

    def __init__(
        self,
        base: Path,
        *,
        resume: bool = False,
        jil_text: str = IDLE_JIL,
        extra: Sequence[str] = (),
        run_root: Path | None = None,
        files: Sequence[Path] | None = None,
    ) -> None:
        # `run_root` and `files` are the estate tier's overrides: a PHYSICAL
        # roll opens a SECOND root in the same base, from the catalog the
        # boundary staged rather than from this helper's own
        self.run_root = run_root or base / "run"
        self.jil = base / "estate.jil"
        if not self.jil.exists():
            self.jil.write_text(jil_text)
        self.files = [str(path) for path in (files or [self.jil])]
        argv = [sys.executable, "-m", "dsl41", "run", "--run-root", str(self.run_root)]
        if resume:
            argv.append("--resume")
        argv.extend(extra)  # launch options under test, before the file list
        RunProcess._n += 1
        # per-instance: two engines in one test must not share a file, or the
        # second silently truncates the evidence the first left
        self.err = (base / f"engine{RunProcess._n}.err").open("w+")
        self.proc = subprocess.Popen(
            argv + self.files, stdout=subprocess.PIPE, stderr=self.err, text=True
        )

    def wait_until_up(self, timeout_s: float = 60.0) -> RunProcess:
        """Block on the banner, but never forever: a `readline` on a wedged
        engine hangs until the session-killing faulthandler timeout, so the
        read happens on a thread this can give up on."""
        stdout = self.proc.stdout
        assert stdout is not None
        banner: list[str] = []
        reader = threading.Thread(target=lambda: banner.append(stdout.readline()))
        reader.daemon = True
        reader.start()
        reader.join(timeout_s)
        line = banner[0] if banner else ""
        assert line.startswith("engine up; control socket:"), f"{line!r} / {self.stderr()}"
        return self

    def stderr(self) -> str:
        self.err.seek(0)
        return self.err.read()

    def stop(self, *, signum: int = signal.SIGINT) -> None:
        """SIGINT is the orderly stop an operator performs, and it RELEASES
        the lock on the way out. `signum=SIGKILL` is the other half: nothing
        runs, nothing is released, and whether the run root frees up is the
        kernel's answer alone."""
        if self.proc.poll() is None:
            self.proc.send_signal(signum)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
        if self.proc.poll() is None:  # pragma: no cover -- teardown backstop
            self.proc.kill()
            self.proc.wait()
        assert self.proc.stdout is not None
        self.proc.stdout.close()
        self.err.close()


@contextlib.contextmanager
def engine(
    base: Path,
    *,
    resume: bool = False,
    jil_text: str = IDLE_JIL,
    extra: Sequence[str] = (),
    run_root: Path | None = None,
    files: Sequence[Path] | None = None,
):
    """Started and reaped, including when the startup assertion is what
    fails: an engine left behind holds a `flock` on its run root, and every
    later test against that root would be refused by a ghost."""
    proc = RunProcess(
        base, resume=resume, jil_text=jil_text, extra=extra, run_root=run_root, files=files
    )
    try:
        yield proc.wait_until_up()
    finally:
        proc.stop()


# --------------------------------------------------------- S6a: the mutex


def test_cm14_a_second_engine_is_refused_the_run_root_and_the_first_is_untouched(
    short_root: Path,
) -> None:
    """concurrency-model ss7's ACQUIRE, between two OS processes.

    The refusal is the visible half and the cheap half. The half worth a
    real process pair is what happens to the INCUMBENT: an election that
    disturbed the engine already leading -- by unlinking its socket, by
    appending a term nobody holds -- would trade one double-run window for
    another. So the assertions are that the second process exits 2 naming
    the first by pid, that the first is still serving its control socket
    afterwards, and that the log records exactly ONE term."""
    with engine(short_root) as first:
        second = cli(
            "run",
            "--resume",
            "--run-root",
            str(first.run_root),
            str(short_root / "estate.jil"),
        )
        assert second.returncode == 2
        assert "is held by another engine" in second.stderr
        assert f"pid {first.proc.pid}" in second.stderr  # named, from the lock's note
        assert "epoch 1" in second.stderr
        assert first.proc.poll() is None  # the incumbent never noticed
        status = cli("query", "status", "--socket", str(first.run_root / "control.sock"), "--brief")
        assert status.returncode == 0
        assert "idle" in status.stdout
    terms = [r for r in read_journal(first.run_root / "journal.jsonl") if r.get("rec") == "leader"]
    assert [r["epoch"] for r in terms] == [1]  # the refused engine allocated nothing


def test_a_run_root_whose_holder_was_killed_is_taken_by_the_next_engine(
    short_root: Path,
) -> None:
    """The contrast test the refusal above needs, and the reason the mutex is
    an `flock` and not a lease: the KERNEL drops it when the holder dies, so
    the successor waits out no expiry and unlinks nothing on a guess.

    SIGKILL, deliberately, not the SIGINT an operator sends. An orderly stop
    runs `release()` on its way out, and a run root freed by its previous
    holder's own cleanup would pass this test against a lease implemented
    with a manual unlock -- which is exactly the design `runner_ledger`'s
    docstring rejects. Here nothing runs after the signal: the successor's
    evidence is that it starts at all, and that it allocates term 2 because
    term 1 is in the LOG, not because anything unlocked."""
    first = RunProcess(short_root)
    try:
        first.wait_until_up()
    finally:
        first.stop(signum=signal.SIGKILL)
    lock = first.run_root / LOCK_NAME
    assert lock.exists()  # nothing cleaned up: the file, and its note, remain
    assert json.loads(lock.read_text())["pid"] == first.proc.pid

    with engine(short_root, resume=True) as second:
        assert second.proc.poll() is None
    assert first.proc.pid != second.proc.pid
    terms = [r for r in read_journal(second.run_root / "journal.jsonl") if r.get("rec") == "leader"]
    assert [r["epoch"] for r in terms] == [1, 2]
    assert [r["pid"] for r in terms] == [first.proc.pid, second.proc.pid]


# ----------------------------------------------------------- S6b: the fence


@pytest.mark.parametrize(
    ("how", "expected"),
    [("delete", "was deleted"), ("replace", "was replaced")],
)
def test_cm14_an_engine_that_cannot_prove_it_leads_stops_dispatching(
    short_root: Path, how: str, expected: str
) -> None:
    """ss7's "losing proof stops dispatch, not merely renewal" (S6b), against
    a live engine and a real filesystem.

    Both arms of `LeaderLock.check` are reachable only from outside the
    process: the lock file is unlinked, or a different inode takes its name.
    In-process tests can construct either; what they cannot show is the
    consequence, that the engine holding it stops and stops BEFORE the work.

    `proof` is why this is evidence and not an empty directory. It is
    started by the same verb, over the same socket, on the same engine, one
    step before the lock goes -- so `runs/` holding exactly `proof.1` says
    the path works and `after` was stopped, where an empty `runs/` would
    equally describe a build that never starts anything.

    What this does NOT separate: the fence fires on the way into the FIRST
    append of admission, so `after` never became an input and therefore
    never became an effect. The stronger reading -- an effect already
    pending in the outbox is not applied after proof is lost -- follows from
    "every effect is preceded by an append" and is not independently held
    here."""
    with engine(short_root, jil_text=FENCE_JIL) as live:
        socket_path = str(live.run_root / "control.sock")
        started = cli("sendevent", "STARTJOB", "--job", "proof", "--socket", socket_path)
        assert started.returncode == 0, started.stderr
        wait_for(lambda: (live.run_root / "runs" / "proof.1" / "spawn.json").exists())

        lock = live.run_root / LOCK_NAME
        assert lock.exists()
        lock.unlink()
        if how == "replace":
            lock.write_bytes(b"{}")  # same name, new inode: our lock excludes nobody
        sent = cli("sendevent", "STARTJOB", "--job", "after", "--socket", socket_path)
        assert live.proc.wait(timeout=15) == 1
        assert f"engine failed: {lock} {expected}" in live.stderr()
        # DL-92's fourth outcome, and the reason this test asserts on it: the
        # command reached a socket and no decision came back, which is not the
        # same as a refusal and must not be retried the same way
        assert sent.returncode == 4
        assert "no decision" in sent.stderr
        assert "--request-id" in sent.stderr
    assert sorted(p.name for p in (live.run_root / "runs").iterdir()) == ["proof.1"]
    records = read_journal(live.run_root / "journal.jsonl")
    jobs = [r["payload"].get("job") for r in records if r.get("rec") == "input"]
    assert jobs == ["proof"]  # admitted, then nothing: `after` never entered the log
    planned = [e for r in records if r.get("rec") == "decision" for e in r["effects"]]
    assert [e["job"] for e in planned] == ["proof"]


# ------------------------------------------------- S6c: the barrier re-drives


def test_cm09_a_spawn_that_never_reached_the_host_is_redriven_at_resume(
    short_root: Path,
) -> None:
    """DL-102, end to end: a real engine dies in the outbox window, and the
    takeover barrier finishes what it decided.

    The seeded sweep proves this over 48 interleavings with a recording
    adapter; what it cannot prove is that the window it models is the window
    the process actually has. Here the log is written by a real engine that
    exited inside it, and the re-drive is done by a real `resume_run` through
    a real wrapper: the evidence that the job ran is its own stdout, in a run
    directory that did not exist when the barrier started.

    ONE of the two outbox windows. This is the earlier one -- decided,
    nothing attempted -- which is the one that must be re-driven. The later
    one (launched, then dead before the outcome was recorded) is what
    `_reconcile_applied_spawns` exists for, and its answer is the opposite:
    reconcile from the spool, never re-drive. It has no real-process test
    because a process cannot be made to die reliably between `_launch` and
    the next statement; the seeded sweep covers it."""
    run_root = short_root / "run"
    driver = subprocess.run(
        [sys.executable, str(REDRIVE_DRIVER), str(run_root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert driver.returncode == CRASH_CODE, driver.stderr

    # the fault fired, and left exactly what ss5 says a pending effect is
    records = read_journal(run_root / "journal.jsonl")
    effects = [e for r in records if r.get("rec") == "decision" for e in r["effects"]]
    assert [(e["kind"], e["job"]) for e in effects] == [("SPAWN", "lost")]
    assert not [r for r in records if r.get("rec") == "effect_result"]
    # nothing anywhere on the host: `runs/` is the scaffolding every run root
    # gets at genesis, and it is empty
    assert list((run_root / "runs").iterdir()) == []

    async def take_over() -> str:
        engine = await resume_run(
            lower_source(REDRIVE_JIL),
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
            settle_seconds=1.0,
            grace_seconds=2.0,
        )
        try:
            await engine.run_until_quiescent(datetime.max)
            return engine.oracle.store.job["lost"].status
        finally:
            # `resume_run` releases its lock only if IT fails; past that the
            # caller owns it, and a leaked one holds this run root for the
            # rest of the session
            await engine.shutdown()
            if engine.journal is not None:
                engine.journal.close()

    assert asyncio.run(take_over()) == "SUCCESS"
    run_dirs = sorted(p.name for p in (run_root / "runs").iterdir())
    # one directory, from the one dispatch the barrier made. Not an
    # at-most-once proof -- there was no first run to duplicate -- but it is
    # what would catch a barrier that re-drove and then dispatched again
    assert run_dirs == ["lost.1"]
    assert (run_root / "logs" / "lost.1.out").read_text().strip() == "re-driven"
    resolved = [
        r
        for r in read_journal(run_root / "journal.jsonl")
        if r.get("rec") == "effect_result" and r["effect_id"] == effects[0]["effect_id"]
    ]
    assert [r["state"] for r in resolved] == ["applied"]  # the same effect, finally resolved


# --------------------------------------------- S5: quarantine and the bound


HELD_JIL = "insert_job: held\njob_type: c\ncommand: sleep 600\n"


def test_cm09_five_failed_renewals_quarantine_the_host_and_new_work_is_held(
    short_root: Path, monkeypatch
) -> None:
    """ss8 unreachability, produced rather than injected -- and then the
    eviction bound computed from what those real processes reported.

    Every other test of this path calls `note_executor_unreachable` or
    `quarantine_host` directly, which assumes the three things worth
    checking: that a supervisor dying makes the renewal loop give up, that
    giving up is what reaches the routing table, and that a SPAWN planned
    afterwards is HELD rather than failed against a socket that is not
    there. The bound the refusal then names is arithmetic over two produced
    numbers -- the deadman this supervisor said it runs, and the instant it
    last answered -- so the assertion is on the identity, not on a constant.

    Both halves of "five consecutive failures" are asserted, because the
    count is a design decision and not an implementation detail: giving up
    on the FIRST failure would also reach `quarantined`, faster, and would
    hold an estate's work over one refused connection."""
    monkeypatch.setattr(SupervisorClient, "_RENEW_EVERY_S", 0.2)
    deadman = 90.0  # long enough that the bound has not passed by the assertion

    async def scenario() -> tuple[dict, str, frozenset[str], float]:
        run_root = short_root / "run"
        run_root.mkdir(parents=True)
        client = SupervisorClient(run_root, deadman_s=deadman)
        await client.ensure_running()
        await client.acquire()
        engine = start_run(
            lower_source(HELD_JIL),
            run_root,
            clock=RealClock(),
            adapters={
                "CMD": SupervisedCommandAdapter(client, grace_seconds=2.0),
                "FW": FileWatcherAdapter(),
            },
            deadman_s=client.supervisor_deadman_s,
            hold_open=True,  # what `dsl41 run` does: an idle estate is not a finished one
        )
        genesis_contact = engine.oracle.store.host(LOCAL_EXECUTOR_ID).last_contact
        client.on_contact = engine.note_executor_contact
        client.on_unreachable = engine.note_executor_unreachable
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            assert client.supervisor_deadman_s == deadman  # read back off the live supervisor
            # A RENEW has to LAND before the kill, or `last_contact` is still
            # the genesis stamp and the bound below is arithmetic over a
            # number no process produced. The wiring above happens after
            # `acquire()`, so the only exchange that can stamp it is one of
            # the renewals -- wait for one rather than assume it.
            deadline = time.monotonic() + 15
            while engine.oracle.store.host(LOCAL_EXECUTOR_ID).last_contact == genesis_contact:
                assert time.monotonic() < deadline, "no lease exchange stamped the routing row"
                await asyncio.sleep(0.02)

            killed_at = time.monotonic()
            os.kill(json.loads((run_root / "supervisor.pid").read_text())["pid"], signal.SIGKILL)
            deadline = killed_at + 30
            while engine.oracle.store.host(LOCAL_EXECUTOR_ID).state != "quarantined":
                assert time.monotonic() < deadline, "the renewal loop never gave up"
                await asyncio.sleep(0.05)
            # Five CONSECUTIVE failures, not one: a quarantine per blip would
            # hold work for no reason, and that is the half a "did it end up
            # quarantined" poll cannot see -- giving up on the first failure
            # passes such a poll, and passes it FASTER. Four of the five waits
            # are a hardcoded second, so the floor is a real 4 s.
            gave_up_after = time.monotonic() - killed_at
            assert gave_up_after >= 4.0, gave_up_after

            # new work, decided while the host is unreachable
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "held"}))
            deadline = time.monotonic() + 15
            while not engine.outbox.pending_for("held", "SPAWN"):
                assert time.monotonic() < deadline, "the spawn was never planned"
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)  # ...and it STAYS pending: nothing drains it
            row = engine.oracle.store.host(LOCAL_EXECUTOR_ID)
            assert len(engine.outbox.pending_for("held", "SPAWN")) == 1
            status = engine.oracle.store.job["held"].status
            # `held_jobs` is that same pending set under the name the control
            # plane publishes it by (ss8, DL-94) -- checked here because the
            # operator's answer to "why is nothing happening" comes from this
            # side, not from the outbox
            published = engine.held_jobs()
            return row.model_dump(), status, published, _evict_bound(engine)
        finally:
            # a failure before the kill would otherwise leave a live supervisor
            # holding this run root, which the next test would meet as a ghost
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
            engine.detach.stopping = True
            await engine.shutdown()
            if engine.journal is not None:
                engine.journal.close()
            with contextlib.suppress(Exception):
                await client.shutdown()
            await client.close()

    row, status, published, remaining = asyncio.run(scenario())
    assert row["state"] == "quarantined"
    assert row["state_before_quarantine"] == "active"  # what it interrupted, kept
    assert row["deadman_s"] == deadman
    # RUNNING, not FAILURE and not a process: the oracle walks a start through
    # to RUNNING inside one feed, and the shell holds the effect underneath it
    assert status == "RUNNING"
    assert published == frozenset({"held"})
    assert list((short_root / "run" / "runs").iterdir()) == []
    assert remaining > 0


def _evict_bound(engine) -> float:
    """What `dsl41 host evict local` would refuse with, from the same gate
    the control server calls -- and every term of it re-derived from the row
    those real processes produced.

    Asserting the identity rather than a number is the point: `deadman_s`
    came back off a live supervisor and `last_contact` was stamped by a real
    lease exchange, so a hard-coded 121.0 would pin the constants and not
    the arithmetic. One `at` for both sides, because two `now()` calls a
    line apart are two different instants and the refusal names tenths."""
    from dsl41.runner_hosts import host_rejection_reason

    at = engine.clock.now()
    store = engine.oracle.store
    row = store.host(LOCAL_EXECUTOR_ID)
    gated = HostCommand(verb="evict", host_id=LOCAL_EXECUTOR_ID)
    reason = host_rejection_reason(store, gated, at)
    assert reason is not None
    assert row.last_contact is not None and row.deadman_s is not None
    bound = row.deadman_s + T_KILL_S
    bound += skew_allowance(bound)  # ss8: skew covers the WHOLE wait, not the deadman
    waited = (at - row.last_contact).total_seconds()
    assert f"was in contact {waited:.1f}s ago" in reason
    assert f"the ss8 bound is {bound:.1f}s" in reason
    assert f"wait {bound - waited:.1f}s more" in reason
    # CM-11's other half, against the same produced preconditions: the wait is
    # skippable, and only by saying so
    forced = HostCommand(verb="evict", host_id=LOCAL_EXECUTOR_ID, force=True)
    assert host_rejection_reason(store, forced, at, actor="alice@ops") is None
    return bound - waited


def test_pr01c_a_foreign_root_is_refused_before_staging_or_supervisor(short_root: Path) -> None:
    """ss1.1: the CLI proves the root is claimable BEFORE it stages a
    bundle into it or starts a supervisor against it -- both are acts on an
    estate this process may turn out not to lead. A used root without
    `--resume` gets the same early refusal."""
    foreign = short_root / "foreign"
    (foreign / "wal").mkdir(parents=True)
    jil = short_root / "estate.jil"
    jil.write_text(IDLE_JIL)
    result = cli("run", "--run-root", str(foreign), "--detached", str(jil))
    assert result.returncode == 2
    assert "not an unused root" in result.stderr
    assert not (foreign / "catalogs").exists()  # nothing was staged into it
    assert not (foreign / "periods").exists()
    assert not (foreign / "supervisor.sock").exists()  # and no supervisor acted on it
    used = short_root / "used"
    used.mkdir()
    (used / "journal.jsonl").write_text('{"rec": "header"}\n')  # any estate, any dialect
    again = cli("run", "--run-root", str(used), "--detached", str(jil))
    assert again.returncode == 2
    assert "already holds an estate" in again.stderr
    assert not (used / "supervisor.sock").exists()
