"""One engine per run root, under real processes.

Each claim here is about processes -- two of them, or one of them and the kernel -- and none of
it is observable from inside a single interpreter:

- **the mutex is an `flock`**, so a refusal needs a second OS process to be refused, "the first
  engine is untouched" needs it to still be serving afterwards, and "the kernel releases it"
  needs a holder that dies without releasing anything;
- **the fence is an inode comparison**, so losing proof needs a real `unlink` under a live
  engine, and the consequence to check is that nothing started -- against the same command, on
  the same engine, starting something a moment earlier;
- **the dispatch window is between two records**, so a decided start that was never dispatched
  needs a process that really died there and a spool that really has nothing in it.

**Every claim about a mechanism carries a positive control.** An empty `runs/` proves nothing
on its own: it is also what a build where nothing ever starts would produce. So the fence test
starts a job through the very path it then breaks, and the re-drive test reads the job's own
stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import subprocess
import sys
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
from dsl41.runner_adapters import (
    LocalCommandAdapter,
)
from dsl41.runner_clock import RealClock
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import LOCK_NAME
from dsl41.runner_startup import resume_run

REDRIVE_DRIVER = Path(__file__).parent / "runner_redrive_driver.py"

IDLE_JIL = "insert_job: idle\njob_type: c\ncommand: sleep 600\n"

#: two jobs so the fence test can carry its own control: `proof` starts
#: through the intact engine, `after` is asked for once the lock is gone.
FENCE_JIL = (
    "insert_job: proof\njob_type: c\ncommand: sleep 600\n\n"
    "insert_job: after\njob_type: c\ncommand: sleep 600\n"
)


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
        # an empty `files` is a launch whose options name the estate, such as `--manifest`
        self.files = [str(path) for path in ([self.jil] if files is None else files)]
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
    """`leader.lock`'s acquire, between two OS processes.

    The refusal is the visible half and the cheap half. The half worth a
    real process pair is what happens to the INCUMBENT: a refused engine
    that disturbed the one already running -- by unlinking its socket, by
    appending to its log -- would trade one double-run window for another.
    So the assertions are that the second process exits 2 naming the first
    by pid, that the first is still serving its control socket afterwards,
    and that the refused engine appended nothing."""
    with engine(short_root) as first:
        journal = first.run_root
        before = len(read_journal(journal))
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
        assert first.proc.poll() is None  # the incumbent never noticed
        assert len(read_journal(journal)) == before  # the refused engine appended nothing
        status = cli("query", "status", "--socket", str(first.run_root / "control.sock"), "--brief")
        assert status.returncode == 0
        assert "idle" in status.stdout


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
    evidence is that it starts at all and names itself in the lock's note."""
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
        assert json.loads(lock.read_text())["pid"] == second.proc.pid
    assert first.proc.pid != second.proc.pid


# ----------------------------------------------------------- S6b: the fence


@pytest.mark.parametrize(
    ("how", "expected"),
    [("delete", "was deleted"), ("replace", "was replaced")],
)
def test_cm14_an_engine_that_cannot_prove_it_leads_stops_dispatching(
    short_root: Path, how: str, expected: str
) -> None:
    """Losing the proof stops dispatch (S6b), against a live engine and a
    real filesystem.

    Both arms of `LeaderLock.check` are reachable only from outside the
    process: the lock file is unlinked, or a different inode takes its name.
    In-process tests can construct either; what they cannot show is the
    consequence, that the engine holding it stops and stops BEFORE the work.

    `proof` is why this is evidence and not an empty directory. It is
    started by the same verb, over the same socket, on the same engine, one
    step before the lock goes -- so `runs/` holding exactly `proof.1` says
    the path works and `after` was stopped, where an empty `runs/` would
    equally describe a build that never starts anything.

    The fence fires on the way into the FIRST append of admission, so
    `after` never became an input and was never dispatched: every start is
    preceded by its `dispatch` append."""
    with engine(short_root, jil_text=FENCE_JIL) as live:
        socket_path = str(live.run_root / "control.sock")
        started = cli("sendevent", "STARTJOB", "--job", "proof", "--socket", socket_path)
        assert started.returncode == 0, started.stderr
        wait_for(lambda: (live.run_root / "runs" / "proof.1" / "1" / "spawn.json").exists())

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
    records = read_journal(live.run_root)
    jobs = [r["payload"].get("job") for r in records if r.get("rec") == "input"]
    assert jobs == ["proof"]  # admitted, then nothing: `after` never entered the log
    dispatched = [r["job"] for r in records if r.get("rec") == "dispatch"]
    assert dispatched == ["proof"]


# ------------------------------------------------- S6c: the barrier re-drives


def test_cm09_a_spawn_that_never_reached_the_host_is_redriven_at_resume(
    short_root: Path,
) -> None:
    """DL-102, end to end: a real engine dies between the decision record and
    the `dispatch` record, and resume dispatches what it decided.

    The seeded sweep proves this over many interleavings with a recording
    adapter; what it cannot prove is that the window it models is the window
    the process actually has. Here the log is written by a real engine that
    exited inside it, and the dispatch is done by a real `resume_run` through
    a real wrapper: the evidence that the job ran is its own stdout, in a run
    directory that did not exist when resume started."""
    run_root = short_root / "run"
    driver = subprocess.run(
        [sys.executable, str(REDRIVE_DRIVER), str(run_root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert driver.returncode == CRASH_CODE, driver.stderr

    # the fault fired: the start is decided and was never dispatched
    records = read_journal(run_root)
    assert [r["decision"] for r in records if r.get("rec") == "decision"] == ["applied"]
    assert not [r for r in records if r.get("rec") == "dispatch"]
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
    # one directory, from the one dispatch resume made. Not an at-most-once
    # proof -- there was no first run to duplicate -- but it is what would
    # catch a resume that dispatched and then dispatched again
    assert run_dirs == ["lost.1"]
    assert (run_root / "logs" / "lost.1.out").read_text().strip() == "re-driven"
    dispatched = [
        (r["job"], r["run_number"]) for r in read_journal(run_root) if r.get("rec") == "dispatch"
    ]
    assert dispatched == [("lost", 1)]


def test_pr01c_a_foreign_root_is_refused_before_anything_is_written(short_root: Path) -> None:
    """ss1.1: the CLI proves the root is claimable before it writes into it. A 1.x root is
    refused by name, with no store made in it (a used 2.0.0 root resumes: test_cli_run.py)."""
    foreign = short_root / "foreign"
    (foreign / "wal").mkdir(parents=True)
    jil = short_root / "estate.jil"
    jil.write_text(IDLE_JIL)
    result = cli("run", "--run-root", str(foreign), str(jil))
    assert result.returncode == 2
    assert "a 1.x run root" in result.stderr
    assert not (foreign / "engine.db").exists()  # nothing was written into it
