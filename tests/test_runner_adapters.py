"""Clock, real-adapter, and result-mapping tests (phase 11b).

Normative spec: docs/runner-design.md ss6 (adapters), ss6a (the wrapper
tier), ss9 (time domains), and runner_clock.py's / runner_adapters.py's own
docstrings for RealClock, LocalCommandAdapter, FileWatcherAdapter, and the
Terminated/Failed/AdapterResult contract. The wrapper crash matrix, kill-boundary tests, and
crash-recovery integration test are tests/test_runner_lifecycle.py's
territory (owned elsewhere, not duplicated here) -- this file stays on the
adapters and clocks themselves: does RealClock behave per ss9, does a real
command run end to end through the wrapper with the documented log/stdin/
profile semantics, does FileWatcherAdapter's polling state machine match ss6
under a deterministic VirtualClock, and does the engine map every
AdapterResult shape (including a contract violation) the way Engine.
_run_adapter's code says it does.

House style follows test_runner.py: one asyncio.run per async scenario,
tmp_path for real-domain run roots. Every expected outcome here was verified
empirically against the real runner/wrapper before the assertion was written
(CLAUDE.md: fidelity is tested, not asserted) -- see the final report for
anything that surprised us or contradicted the design doc.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from subprocess_harness import wait_for

from dsl41 import runner_procid as _procid
from dsl41.ir import JobIR, lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    AdapterContext,
    AdapterResult,
    Failed,
    FileWatcherAdapter,
    LocalCommandAdapter,
    Terminated,
    spool_names_run,
)
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_startup import start_run

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("the adapters/wrapper tier is POSIX-only", allow_module_level=True)

T0 = datetime(2026, 7, 1, 8, 0)


# ---------------------------------- 1a. one result, one reported status (C7)


class _ReturnsAdapter:
    """An adapter that hands back exactly what the test names."""

    def __init__(self, result: object) -> None:
        self.result = result

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> Any:
        return self.result


def _live_status(text: str, result: object) -> dict:
    """What the LIVE path puts on the STATUS input for `result`."""
    engine = Engine(
        lower_source(text), clock=VirtualClock(start=T0), adapters={"CMD": _ReturnsAdapter(result)}
    )
    seen: list[Event] = []
    engine._enqueue = lambda ev, **_kw: seen.append(ev)  # type: ignore[method-assign]
    asyncio.run(engine._run_adapter(engine.oracle.catalog.jobs["j"], 1, _ReturnsAdapter(result)))
    [event] = seen
    assert {event.payload["job"], event.payload["run_number"]} == {"j", 1}
    return {k: v for k, v in event.payload.items() if k not in ("job", "run_number")}


def _resumed_status(run_root: Path, text: str, result: object, monkeypatch) -> dict:
    """What the restart's settle step records for the same result: the open attempt's
    `outcome` carries the STATUS fields the result maps to."""
    from dsl41 import runner_startup
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_journal import read_records
    from dsl41.runner_startup import resume_run

    engine = start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},  # parks the run: open at the restart
    )

    async def _resolve(*_a: Any, **_kw: Any) -> Any:
        return result, None

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        with monkeypatch.context() as mp:
            mp.setattr(runner_startup, "resolve_spool", _resolve)
            resumed = await resume_run(
                lower_source(text),
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter(default=None)},
            )
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()

    asyncio.run(scenario())
    [outcome] = [r.payload for _, r in read_records(run_root) if r.kind == "outcome"]
    return dict(outcome["status"])


def test_one_adapter_result_is_reported_the_same_live_and_at_resume(
    tmp_path: Path, monkeypatch
) -> None:
    """DL-145. Turning an `AdapterResult` into the STATUS input's payload
    had two bodies, and they did not agree.

    Both spelled the exit code, TERMINATED and FAILURE the same way. The
    FOURTH case is where they parted: the engine REFUSED a result it could
    not classify, and the resume ladder's copy fell through to FAILURE --
    fabricating a fate for a run nobody read, on the one path where a run's
    fate is being recovered rather than observed.

    One body now (`runner_adapters.status_payload`), driven here through
    both call sites over the same three results."""
    from dsl41.runner_adapters import status_payload

    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    for index, result in enumerate((0, 7, Terminated("wrapper lost"), Failed("unobservable"))):
        expected = status_payload(result, where="pin")  # type: ignore[arg-type]
        live = _live_status(text, result)
        resumed = _resumed_status(tmp_path / f"root{index}", text, result, monkeypatch)
        assert live == expected == resumed, result

    # and the fourth case refuses on BOTH, naming the run it could not
    # report on: the resume copy used to answer FAILURE here
    with pytest.raises(EngineError, match="adapter for 'j' returned"):
        _live_status(text, "not an adapter result")
    with pytest.raises(EngineError, match="the spool ladder for j.1 returned"):
        _resumed_status(tmp_path / "root-bad", text, "not an adapter result", monkeypatch)


# --------------------------------------------------------------- 1. RealClock


def test_real_clock_now_is_naive_utc_close_to_the_system_clock() -> None:
    """(runner-design ss9 / RealClock docstring): now() is NAIVE UTC (tzinfo
    stripped) so DST never runs the oracle's non-decreasing feed discipline
    backwards -- and it tracks the wall clock, not some fixed epoch."""
    clock = RealClock()
    now = clock.now()
    assert now.tzinfo is None
    reference = datetime.now(UTC).replace(tzinfo=None)
    assert abs((reference - now).total_seconds()) < 2.0


def test_real_clock_wait_until_returns_early_when_interrupted() -> None:
    """(RealClock docstring): wait_until sleeps in bounded slices toward `t`
    but wakes immediately when `interrupt` fires from another task -- the
    mechanism that lets queue activity re-plan the real-domain loop's wait
    without polling."""

    async def scenario() -> float:
        clock = RealClock()
        interrupt = asyncio.Event()

        async def fire() -> None:
            await asyncio.sleep(0.05)
            interrupt.set()

        setter = asyncio.create_task(fire())
        start = time.monotonic()
        await clock.wait_until(clock.now() + timedelta(seconds=10), interrupt=interrupt)
        elapsed = time.monotonic() - start
        await setter
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0  # nowhere near the 10s deadline it was given


def test_real_clock_sleep_until_sleeps_roughly_the_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(RealClock docstring): sleep_until is a plain sleep for the requested
    delta -- and a deadline already in the past returns immediately rather
    than blocking.

    The clock's `now` is held at one instant and the sleeps it asks for are
    recorded, so neither check depends on how long a loaded box takes
    between two reads of the clock or to wake the task: one sleep of exactly
    the delta for the future deadline, none for the past one."""
    real_sleep = asyncio.sleep
    asked: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        asked.append(seconds)
        await real_sleep(seconds)

    class HeldClock(RealClock):
        instant = RealClock().now()

        def now(self) -> datetime:
            return self.instant

    async def scenario() -> tuple[float, list[float]]:
        clock = HeldClock()
        t0 = time.monotonic()
        await clock.sleep_until(clock.now() + timedelta(seconds=0.2))
        forward = time.monotonic() - t0
        future = list(asked)
        await clock.sleep_until(clock.now() - timedelta(seconds=5))
        return forward, future

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    forward, future = asyncio.run(scenario())
    assert future == [0.2]
    assert forward >= 0.15  # a real sleep happened
    assert asked == future  # the past deadline asked for no sleep


# --------------------------------------------- 2. end-to-end real domain (CMD)


async def _run_real(
    text: str, run_root: Path, jobs: list[str], *, grace_seconds: float = 2.0
) -> Engine:
    """start_run + inject STARTJOB for every job at `now`, run to
    quiescence, shut down, close the journal -- the shared shape of every
    LocalCommandAdapter end-to-end scenario below. shutdown() matters here,
    not just for cleanliness: the real-domain quiescence check does not wait
    for an in-flight cancellation teardown (ss4/DL-43 item 5 -- "settling is
    undecidable and unnecessary" in the real domain), so status.json is only
    guaranteed written once shutdown()'s gather has returned."""
    catalog = lower_source(text)
    clock = RealClock()
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": LocalCommandAdapter(grace_seconds=grace_seconds)},
    )
    now = clock.now()
    for job in jobs:
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": job}))
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()
    return engine


def test_exit_zero_is_success_exit_three_is_failure(tmp_path: Path) -> None:
    """(runner-design ss4 DL-33 boundary, through the real adapter): the
    wrapper reports the raw exit code only; SEM-09's default boundary
    (max_exit_success=0) stays the oracle's call, never the adapter's."""
    text = (
        "insert_job: exit0\njob_type: c\ncommand: exit 0\nmachine: m1\n\n"
        "insert_job: exit3\njob_type: c\ncommand: exit 3\nmachine: m1\n"
    )
    engine = asyncio.run(_run_real(text, tmp_path / "run", ["exit0", "exit3"]))
    assert engine.oracle.store.job["exit0"].status == "SUCCESS"
    assert engine.oracle.store.job["exit0"].exit_code == 0
    assert engine.oracle.store.job["exit3"].status == "FAILURE"
    assert engine.oracle.store.job["exit3"].exit_code == 3


def test_max_exit_success_threshold_honored_oracle_side(tmp_path: Path) -> None:
    """(runner-design ss4 DL-33 boundary): max_exit_success: 2 makes exit
    code 2 a SUCCESS through the real adapter -- the same boundary function
    the oracle applies to a scripted FakeAdapter run."""
    text = "insert_job: mx\njob_type: c\ncommand: exit 2\nmachine: m1\nmax_exit_success: 2\n"
    engine = asyncio.run(_run_real(text, tmp_path / "run", ["mx"]))
    assert engine.oracle.store.job["mx"].status == "SUCCESS"
    assert engine.oracle.store.job["mx"].exit_code == 2


def test_std_out_file_append_semantics(tmp_path: Path) -> None:
    """(runner-design ss6): std_out_file/std_err_file APPEND (vendor parity)
    -- pre-seed the file and confirm the command's own output lands after
    the pre-existing content, never overwriting it."""
    out = tmp_path / "custom.out"
    out.write_text("PRESEED\n")
    text = (
        f"insert_job: apj\njob_type: c\ncommand: echo appended\nmachine: m1\nstd_out_file: {out}\n"
    )
    engine = asyncio.run(_run_real(text, tmp_path / "run", ["apj"]))
    assert engine.oracle.store.job["apj"].status == "SUCCESS"
    assert out.read_text() == "PRESEED\nappended\n"


def test_default_log_naming_under_run_root_logs(tmp_path: Path) -> None:
    """(runner-design ss6): with no std_out_file/std_err_file, output goes to
    <run_root>/logs/<job>.<run_number>.{out,err}."""
    text = "insert_job: dlj\njob_type: c\ncommand: echo out_text; echo err_text 1>&2\nmachine: m1\n"
    run_root = tmp_path / "run"
    engine = asyncio.run(_run_real(text, run_root, ["dlj"]))
    assert engine.oracle.store.job["dlj"].status == "SUCCESS"
    assert (run_root / "logs" / "dlj.1.out").read_text() == "out_text\n"
    assert (run_root / "logs" / "dlj.1.err").read_text() == "err_text\n"


def test_std_in_file_feeds_the_command_stdin(tmp_path: Path) -> None:
    """(runner-design ss6, LocalCommandAdapter docstring): std_in_file feeds
    the command's stdin (else /dev/null) -- `cat` echoes it straight to the
    default stdout log."""
    infile = tmp_path / "input.txt"
    infile.write_text("fed-via-stdin\n")
    text = f"insert_job: catj\njob_type: c\ncommand: cat\nmachine: m1\nstd_in_file: {infile}\n"
    run_root = tmp_path / "run"
    engine = asyncio.run(_run_real(text, run_root, ["catj"]))
    assert engine.oracle.store.job["catj"].status == "SUCCESS"
    assert (run_root / "logs" / "catj.1.out").read_text() == "fed-via-stdin\n"


def test_profile_sourced_before_the_command(tmp_path: Path) -> None:
    """(runner-design ss6): `profile` sources first -- `. <profile> &&
    <command>` -- so a variable it exports is visible to the command."""
    profile = tmp_path / "profile.sh"
    profile.write_text("export MYVAR=hello\n")
    text = f"insert_job: prj\njob_type: c\ncommand: echo $MYVAR\nmachine: m1\nprofile: {profile}\n"
    run_root = tmp_path / "run"
    engine = asyncio.run(_run_real(text, run_root, ["prj"]))
    assert engine.oracle.store.job["prj"].status == "SUCCESS"
    assert (run_root / "logs" / "prj.1.out").read_text() == "hello\n"


def test_failing_profile_fails_the_job_with_shs_exit_code(tmp_path: Path) -> None:
    """(runner-design ss6/ss15 E5, PENDING): a profile that cannot be sourced
    fails the job with sh's OWN exit code for the failed `.` builtin, never a
    guessed/synthesized code. That code is platform-sh-specific (bash 3.2 in
    POSIX mode on macOS says 1; dash on Debian/Ubuntu says 2), so the
    expectation is derived from /bin/sh itself with the exact construct the
    adapter composes -- the pin is "whatever sh says", not a number."""

    missing = tmp_path / "does-not-exist.sh"
    text = (
        "insert_job: bpj\njob_type: c\ncommand: echo should-not-run\n"
        f"machine: m1\nprofile: {missing}\n"
    )
    expected = subprocess.run(
        ["/bin/sh", "-c", f". {missing} && echo should-not-run"],
        capture_output=True,
        check=False,
    ).returncode
    assert expected != 0  # sourcing a missing file must fail on any sane sh
    run_root = tmp_path / "run"
    engine = asyncio.run(_run_real(text, run_root, ["bpj"]))
    assert engine.oracle.store.job["bpj"].status == "FAILURE"
    assert engine.oracle.store.job["bpj"].exit_code == expected  # PENDING: E5
    assert (run_root / "logs" / "bpj.1.out").read_text() == ""


def test_killjob_mid_run_terminates_and_no_completion_overwrites_it(tmp_path: Path) -> None:
    """(runner-design ss4 "the oracle decides, the shell kills"): KILLJOB
    after the wrapper has spawned the command terminates the real process;
    the store lands TERMINATED and status.json records the signal. No
    natural-exit report arrives to overwrite it: a cancelled adapter's run()
    re-raises CancelledError instead of reaching the completion enqueue
    (runner.py module docstring), so drops stays empty.

    The kill waits for spawn.json, not for a fixed offset. A KILLJOB stamped
    a fixed beat after STARTJOB is admitted at once when the loop stalls
    that long before the adapter task first runs; the cancel then lands on
    a task that never spawned a wrapper, and no status.json is written. The
    sleep is long so the kill always precedes the natural exit."""
    text = "insert_job: kj\njob_type: c\ncommand: sleep 30\nmachine: m1\n"
    run_root = tmp_path / "run"
    spawned = run_root / "runs" / "kj.1" / "1" / "spawn.json"

    async def scenario() -> Engine:
        catalog = lower_source(text)
        clock = RealClock()
        engine = start_run(
            catalog, run_root, clock=clock, adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)}
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "kj"}))

        async def kill_once_spawned() -> None:
            while not spawned.exists():
                await asyncio.sleep(0.01)
            engine.inject(Event(at=clock.now(), kind="KILLJOB", payload={"job": "kj"}))

        injector = asyncio.create_task(asyncio.wait_for(kill_once_spawned(), timeout=20.0))
        await engine.run_until_quiescent(datetime.max)
        await injector
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["kj"].status == "TERMINATED"
    assert engine.oracle.store.job["kj"].exit_code is None
    assert engine.drops == []
    status = json.loads((run_root / "runs" / "kj.1" / "1" / "status.json").read_text())
    assert status["outcome"] == "signaled"
    assert status["signal"] == signal.SIGTERM


def test_dl356_a_kill_before_the_adapter_first_runs_spawns_nothing(tmp_path: Path) -> None:
    """A cancel that lands before the adapter task's first step never enters
    `run`: no run directory, no wrapper, nothing for a kill step to stop.
    This is the window a loop stall opens between STARTJOB's admission and
    the task's first step (DL-359)."""
    job = lower_source("insert_job: j\njob_type: c\ncommand: sleep 30\n").jobs["j"]
    ctx = AdapterContext(clock=RealClock(), run_root=tmp_path, run_id="rid-early")

    async def scenario() -> None:
        task = asyncio.create_task(LocalCommandAdapter().run(job, 1, ctx))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not (tmp_path / "runs").exists()


def test_dl356_a_kill_before_the_spec_is_handed_over_prevents_the_launch(
    tmp_path: Path, monkeypatch
) -> None:
    """A cancel that lands while the adapter writes the wrapper's spec must
    stop the wrapper before it can launch the command. The wrapper launches
    only after it reads its spec to EOF, and EOF comes only when the adapter
    closes stdin, so a wrapper killed before that close never launches.
    Left alone, the wrapper waits on stdin past the cancelled start, and it
    launches the command whenever the engine's end of the pipe closes.

    `drain` is the one await on this stretch. It suspends for real only when
    the spec overfills the pipe buffer, so the test holds it open instead."""
    job = lower_source("insert_job: j\njob_type: c\ncommand: sleep 30\n").jobs["j"]
    ctx = AdapterContext(clock=RealClock(), run_root=tmp_path, run_id="rid-drain")
    run_dir = tmp_path / "runs" / "j.1"
    procs: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*args, **kwargs)
        procs.append(proc)
        return proc

    async def scenario() -> None:
        in_drain = asyncio.Event()

        async def held_drain(self: asyncio.StreamWriter) -> None:
            in_drain.set()
            await asyncio.get_running_loop().create_future()  # only a cancel ends it

        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
        monkeypatch.setattr(asyncio.StreamWriter, "drain", held_drain)
        task = asyncio.create_task(LocalCommandAdapter(grace_seconds=1.0).run(job, 1, ctx))
        try:
            await asyncio.wait_for(in_drain.wait(), timeout=20.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=20.0)
            (proc,) = procs
            assert proc.returncode == -signal.SIGKILL, "the wrapper outlived the cancelled start"
            # after the reap: killed before EOF, it never recorded a launch
            assert not (run_dir / "spawn.json").exists()
        finally:
            # a red run leaves the wrapper waiting on stdin; reap it here
            for proc in procs:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    asyncio.run(scenario())


def _assert_killed_by_the_adapter(run_dir: Path) -> None:
    # the supervisor did launch the command; its wrapper records that a
    # moment after the fork, so a run with no kill can lag the task
    wait_for(lambda: (run_dir / "spawn.json").exists())
    # the kill returns on the exit push, which follows the status record
    assert (run_dir / "status.json").exists(), "the command outlived the cancelled start"
    status = json.loads((run_dir / "status.json").read_text())
    assert status["outcome"] == "signaled", status
    assert status["signal"] == signal.SIGTERM


# --------------------------------------------- 3. FileWatcherAdapter (virtual)


def test_fw_lifecycle_absence_below_min_growth_reset_then_two_stable_polls_succeed(
    tmp_path: Path,
) -> None:
    """(runner-design ss6 FW adapter, ss15 E6 PENDING): the adapter checks
    immediately at dispatch, then every watch_interval seconds. Absence and
    below-min_size both leave the job RUNNING; growth between two polls
    resets the stability count back to one qualifying poll; only two
    CONSECUTIVE polls at the same qualifying size complete the job (exit 0
    -> SUCCESS). watch_interval: 60 is a virtual-clock instant, never a real
    wait (ss9), so this whole lifecycle runs with no real sleeping."""
    watch_file = tmp_path / "watched.txt"
    text = (
        f"insert_job: fwj\njob_type: f\nwatch_file: {watch_file}\n"
        "watch_interval: 60\nwatch_file_min_size: 5\n"
    )

    async def scenario() -> None:
        engine = Engine(
            lower_source(text), clock=VirtualClock(start=T0), adapters={"FW": FileWatcherAdapter()}
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwj"}))

        await engine.run_until_quiescent(T0 + timedelta(seconds=30))  # immediate check: absent
        assert engine.oracle.store.job["fwj"].status == "RUNNING"

        await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # poll at +60: still absent
        assert engine.oracle.store.job["fwj"].status == "RUNNING"

        watch_file.write_bytes(b"ab")  # 2 bytes: present, below min_size (5)
        await engine.run_until_quiescent(T0 + timedelta(seconds=150))  # poll at +120
        assert engine.oracle.store.job["fwj"].status == "RUNNING"

        watch_file.write_bytes(b"abcdef")  # 6 bytes: qualifies, first such poll
        await engine.run_until_quiescent(T0 + timedelta(seconds=210))  # poll at +180
        assert engine.oracle.store.job["fwj"].status == "RUNNING"

        watch_file.write_bytes(b"abcdefgh")  # grew before the next poll: resets stability
        await engine.run_until_quiescent(T0 + timedelta(seconds=270))  # poll at +240
        assert engine.oracle.store.job["fwj"].status == "RUNNING"

        await engine.run_until_quiescent(T0 + timedelta(seconds=330))  # poll at +300: same size
        assert engine.oracle.store.job["fwj"].status == "SUCCESS"
        assert engine.oracle.store.job["fwj"].exit_code == 0
        await engine.shutdown()

    asyncio.run(scenario())


def test_fw_two_immediately_stable_polls_succeed(tmp_path: Path) -> None:
    """Minimal case, independent of the growth-reset path above: a file
    already at a stable qualifying size before the job starts needs exactly
    two polls -- the immediate dispatch-time check, then the first
    watch_interval poll -- to complete."""
    watch_file = tmp_path / "watched.txt"
    watch_file.write_bytes(b"abcdef")
    text = (
        f"insert_job: fwj3\njob_type: f\nwatch_file: {watch_file}\n"
        "watch_interval: 60\nwatch_file_min_size: 5\n"
    )

    async def scenario() -> None:
        engine = Engine(
            lower_source(text), clock=VirtualClock(start=T0), adapters={"FW": FileWatcherAdapter()}
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwj3"}))
        await engine.run_until_quiescent(T0)  # only the immediate check has happened
        assert engine.oracle.store.job["fwj3"].status == "RUNNING"
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # poll at +60: same size
        assert engine.oracle.store.job["fwj3"].status == "SUCCESS"
        await engine.shutdown()

    asyncio.run(scenario())


# ------------------------------------- 3a. `fw-existence` switch (DL-258)


@pytest.mark.parametrize("existence", ["stable", "immediate"])
def test_fw_existence_with_the_file_already_present(tmp_path: Path, existence: str) -> None:
    """No watch_file_min_size. `stable` (default) still needs the
    dispatch-time check plus one watch_interval poll at the same size --
    test_fw_two_immediately_stable_polls_succeed's lifecycle with no minimum
    size. `immediate` (the vendor reading) completes at the dispatch-time
    check alone, watch_interval ignored."""
    watch_file = tmp_path / "watched.txt"
    watch_file.write_bytes(b"abcdef")
    text = f"insert_job: fwp\njob_type: f\nwatch_file: {watch_file}\nwatch_interval: 60\n"

    async def scenario() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence=existence)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwp"}))
        await engine.run_until_quiescent(T0)  # only the immediate check has happened
        if existence == "immediate":
            assert engine.oracle.store.job["fwp"].status == "SUCCESS"
        else:
            assert engine.oracle.store.job["fwp"].status == "RUNNING"
            await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # poll at +60
            assert engine.oracle.store.job["fwp"].status == "SUCCESS"
        await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("existence", ["stable", "immediate"])
def test_fw_existence_with_the_file_appearing_later(tmp_path: Path, existence: str) -> None:
    """DL-258 (corrected): the vendor's "completes immediately" carve-out is
    narrow -- "the file already exists when the job runs" -- so it only ever
    applies to the run's FIRST poll. Here the file is absent at that first
    poll, so the carve-out's window has already closed by the time it
    appears; `immediate` then needs the same two consecutive stable polls
    `stable` always needed (the watch_interval Attribute page's general
    rule: "the job is considered complete when the watched file reaches the
    minimum size and remains in a steady state ... during the specified
    interval"). This pins the fix against the earlier over-extension, where
    `immediate` completed on ANY first qualifying poll, not just the run's."""
    watch_file = tmp_path / "watched.txt"
    text = f"insert_job: fwl\njob_type: f\nwatch_file: {watch_file}\nwatch_interval: 60\n"

    async def scenario() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence=existence)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwl"}))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))  # first poll: absent
        assert engine.oracle.store.job["fwl"].status == "RUNNING"
        watch_file.write_bytes(b"abcdef")
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # poll at +60: appears
        assert engine.oracle.store.job["fwl"].status == "RUNNING"  # first-poll window passed
        await engine.run_until_quiescent(T0 + timedelta(seconds=150))  # poll at +120: same size
        assert engine.oracle.store.job["fwl"].status == "SUCCESS"
        await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("existence", ["stable", "immediate"])
def test_fw_existence_with_a_still_growing_file(tmp_path: Path, existence: str) -> None:
    """No watch_file_min_size, the file grows every poll. `stable` never
    sees two consecutive polls at the same size, so the job stays RUNNING;
    `immediate` does not care about size at all and completes on the first
    poll that sees the file, however much it later grows."""
    watch_file = tmp_path / "watched.txt"
    text = f"insert_job: fwg\njob_type: f\nwatch_file: {watch_file}\nwatch_interval: 60\n"

    async def scenario() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence=existence)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwg"}))
        watch_file.write_bytes(b"a")
        await engine.run_until_quiescent(T0)  # immediate check: 1 byte
        if existence == "immediate":
            assert engine.oracle.store.job["fwg"].status == "SUCCESS"
        else:
            assert engine.oracle.store.job["fwg"].status == "RUNNING"
            watch_file.write_bytes(b"ab")
            await engine.run_until_quiescent(T0 + timedelta(seconds=60))  # poll at +60: grew
            assert engine.oracle.store.job["fwg"].status == "RUNNING"
            watch_file.write_bytes(b"abc")
            await engine.run_until_quiescent(T0 + timedelta(seconds=120))  # poll at +120: grew
            assert engine.oracle.store.job["fwg"].status == "RUNNING"
        await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("existence", ["stable", "immediate"])
def test_fw_existence_does_not_change_a_job_with_a_minimum_size(
    tmp_path: Path, existence: str
) -> None:
    """`fw-existence` only decides a no-minimum-size watch. A job with
    watch_file_min_size still needs two stable qualifying polls whatever the
    switch says -- test_fw_two_immediately_stable_polls_succeed's lifecycle,
    unchanged under both values."""
    watch_file = tmp_path / "watched.txt"
    watch_file.write_bytes(b"abcdef")
    text = (
        f"insert_job: fwmin\njob_type: f\nwatch_file: {watch_file}\n"
        "watch_interval: 60\nwatch_file_min_size: 5\n"
    )

    async def scenario() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence=existence)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "fwmin"}))
        await engine.run_until_quiescent(T0)  # only the immediate check has happened
        assert engine.oracle.store.job["fwmin"].status == "RUNNING"
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # poll at +60: same size
        assert engine.oracle.store.job["fwmin"].status == "SUCCESS"
        await engine.shutdown()

    asyncio.run(scenario())


# ----------------------------------------------- 4. result mapping (white-box)


class _StubAdapter:
    """Returns a fixed result (or, for the contract-violation test, a
    deliberately wrong type) regardless of job/run_number -- isolates
    Engine._run_adapter's AdapterResult mapping from any real adapter."""

    def __init__(self, result: AdapterResult) -> None:
        self.result = result

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        return self.result


_STUB_JIL = "insert_job: sj\njob_type: c\ncommand: x\nmachine: m1\n"


def test_terminated_result_drives_the_store_to_terminated() -> None:
    """(Engine._run_adapter / Terminated docstring): Terminated(cause) maps
    to STATUS TERMINATED -- reserved for kills the wrapper actually
    observed."""

    async def scenario() -> None:
        engine = Engine(
            lower_source(_STUB_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _StubAdapter(Terminated("killed for test"))},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "sj"}))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["sj"].status == "TERMINATED"
        await engine.shutdown()

    asyncio.run(scenario())


def test_failed_result_drives_the_store_to_failure() -> None:
    """(Engine._run_adapter / Failed docstring): Failed(cause) maps to
    STATUS FAILURE -- a completion with no raw exit code, never satisfying a
    success-dependent downstream."""

    async def scenario() -> None:
        engine = Engine(
            lower_source(_STUB_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _StubAdapter(Failed("boom"))},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "sj"}))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["sj"].status == "FAILURE"
        await engine.shutdown()

    asyncio.run(scenario())


def test_bogus_adapter_result_raises_engine_error_loudly() -> None:
    """(Engine._run_adapter): AdapterResult is int | Terminated | Failed;
    anything else is a contract violation the engine refuses to guess about
    -- it raises EngineError loudly at the next settle rather than silently
    swallowing or misinterpreting it (CLAUDE.md: no silent loss)."""

    async def scenario() -> None:
        engine = Engine(
            lower_source(_STUB_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _StubAdapter("bogus-result")},  # type: ignore[arg-type]
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "sj"}))
        with pytest.raises(EngineError, match="returned"):
            await engine.run_until_quiescent(T0)
        await engine.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------- 5. process-identity units


def test_pr36a_the_wrapper_spec_carries_the_decision_minted_run_id(tmp_path: Path) -> None:
    """The one seam between the WAL and the spool (DL-118, period-model
    ss2.3): the durable effect minted `run_id`, and `_build_run_spec` must
    hand THAT id to the wrapper, or the log and the run directory would name
    the same process two ways. The fallback mint exists only for the paths
    with no effect behind them (AdapterContext docstring)."""
    from dsl41.runner_adapters import AdapterContext, _build_run_spec

    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\n")
    job_ir = catalog.jobs["j"]
    bound = AdapterContext(
        clock=VirtualClock(start=T0), run_root=tmp_path / "a", run_id="rid-from-effect"
    )
    _, spec = _build_run_spec(job_ir, 1, bound, grace_seconds=1.0)
    assert spec["run_id"] == "rid-from-effect"

    unbound = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path / "b")
    _, legacy = _build_run_spec(job_ir, 1, unbound, grace_seconds=1.0)
    assert uuid.UUID(legacy["run_id"]).version == 4  # effect-less path still mints

    # the fallback keys on `is None`, never on falsiness: an empty id (which
    # the journal gates refuse anyway) must fail loudly downstream rather
    # than silently become a second, minted identity
    empty = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path / "c", run_id="")
    _, spec_empty = _build_run_spec(job_ir, 1, empty, grace_seconds=1.0)
    assert spec_empty["run_id"] == ""


def test_a_kill_refuses_a_spawn_record_naming_a_stranger(tmp_path: Path) -> None:
    """DL-118 at the sharpest edge: the spawn record names the process group
    the kill will signal, and the pid-reuse token proves that group is
    ALIVE, not that it is ours. A spoofed record with a live foreign token
    would aim the kill at a stranger's processes -- refused on identity
    before any process field is read."""
    from dsl41.runner_adapters import LocalCommandAdapter

    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    (run_dir / "spawn.json").write_text(
        json.dumps({"run_id": "rid-else", "command_pid": os.getpid(), "command_pgid": os.getpid()})
    )

    class _Proc:
        stdin = None
        returncode: int | None = None

    async def scenario() -> None:
        adapter = LocalCommandAdapter(grace_seconds=0.1)
        with pytest.raises(EngineError, match="stranger's process group"):
            await adapter._kill(run_dir, _Proc(), "rid-wal")  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_spool_names_run_checks_only_the_dimensions_given() -> None:
    """DL-178y: `spool_names_run` is the one owner of the DL-118 identity
    gate -- absent doc, a job/run_number mismatch, or a run_id mismatch are
    each False, but only for a dimension the caller actually supplies. A
    caller that never asks about `run_id` (or `job`/`run_number`) does not
    get a new refusal it did not ask for -- the predicate's whole point is
    that each of the five sites keeps its own checked dimensions."""
    doc = {"job": "j", "run_number": 1, "run_id": "rid"}
    assert spool_names_run(doc) is True  # nothing checked -> named
    assert spool_names_run(None) is False
    assert spool_names_run(doc, job="j", run_number=1) is True
    assert spool_names_run(doc, job="other") is False
    assert spool_names_run(doc, run_number=2) is False
    assert spool_names_run(doc, run_id="rid") is True
    assert spool_names_run(doc, run_id="stranger") is False
    assert spool_names_run(doc, job="j", run_number=1, run_id="rid") is True
    assert spool_names_run(doc, job="j", run_number=1, run_id="stranger") is False


def test_durable_write_leaves_no_temp_file_and_content_matches(tmp_path: Path) -> None:
    """(runner_procid.py durable_write docstring, the DL-41a durability
    liturgy): same-dir temp file, fsync, rename, fsync(directory) -- after
    the call only the final file exists, holding the exact bytes written."""
    target = tmp_path / "sub" / "record.json"
    target.parent.mkdir()
    _procid.durable_write(str(target), b"hello world")
    assert target.read_bytes() == b"hello world"
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_start_tokens_match_lstart_within_two_seconds_but_not_beyond() -> None:
    """(start_tokens_match docstring): macOS's `ps -o lstart=` resolves to
    whole seconds, so the PID-reuse guard tolerates +/-2s; beyond that it is
    a different process."""
    base = "lstart:Sat Jul 11 14:19:32 2026"
    within = "lstart:Sat Jul 11 14:19:34 2026"  # +2s: still matches
    beyond = "lstart:Sat Jul 11 14:19:35 2026"  # +3s: does not
    assert _procid.start_tokens_match(base, within) is True
    assert _procid.start_tokens_match(base, beyond) is False


def test_start_tokens_match_ticks_exact_only_and_mixed_forms_never_match() -> None:
    """(start_tokens_match docstring): Linux tick tokens compare exactly (no
    tolerance); a ticks token against an lstart token (or vice versa) never
    matches -- the two platforms' tokens are never comparable."""
    assert _procid.start_tokens_match("ticks:100", "ticks:100") is True
    assert _procid.start_tokens_match("ticks:100", "ticks:101") is False
    assert _procid.start_tokens_match("ticks:100", "lstart:Sat Jul 11 14:19:32 2026") is False
    assert _procid.start_tokens_match("lstart:Sat Jul 11 14:19:32 2026", "ticks:100") is False


# ---------------------------------------------- 6. shared supervisor LIST net


def _list_row(run_id: str, *, alive: bool = True) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "job": "j",
        "run_number": 1,
        "run_dir": "/synthetic/run/j.1",
        "wrapper_pid": 123,
        "wrapper_alive": alive,
        "spawned_at": "2026-09-18T08:00:00+00:00",
        "wrapper_rc": None if alive else 0,
    }


def _list_reply(*, runs: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """A full `{"ok": true, ...}` LIST reply (supervisor-protocol ss5): the
    client's own `list_runs` parses every field below into
    `SupervisorListSuccess`, so a wire double answering LIST must carry
    them all, defaulted past what a given test does not exercise."""
    return {
        "ok": True,
        "version": 1,
        "supervisor_pid": 0,
        "boot_id": "boot",
        "incarnation": "inc-1",
        "deadman_s": None,
        "lease": None,
        "runs": runs or [],
        **extra,
    }


# ------------------------------------- the client's connection and renewal


class _ScriptedRequests:
    """A `SupervisorClient._request` double. It answers each call from the
    script, raises a scripted exception, and parks once the script is spent."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.commands: list[str] = []
        self.drained = asyncio.Event()

    async def __call__(self, obj: dict[str, Any], *, _connect: bool = True) -> dict[str, Any]:
        self.commands.append(obj["cmd"])
        if not self.script:
            self.drained.set()
            await asyncio.Event().wait()  # parks until the test cancels it
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


_ACK = {"ok": True, "token": 1, "incarnation": "inc-1"}
_RENEWED = {"ok": True, "expires_at": "2026-10-08T00:00:00+00:00"}


class _FreezableSupervisor:
    """A supervisor double on a unix socket. It answers every request with `ok`, and ACQUIRE with
    a fresh token. While `frozen` it reads each request and never replies, which is what a stopped
    supervisor's peer sees: the kernel takes the connection and the bytes, and no answer comes.
    A frozen stub still answers once each command listed in `answer_while_frozen`. `delay_s`
    holds the reply to each listed command that long. `reply_once` answers the next request of
    a command with the given reply instead. `push` writes to the connection the last ACQUIRE
    came on, as the supervisor pushes to the lease holder."""

    def __init__(self) -> None:
        self.frozen = False
        self.answer_while_frozen: list[str] = []
        self.delay_s: dict[str, float] = {}
        self.reply_once: dict[str, dict[str, Any]] = {}
        self.incarnation = "inc-1"
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.received: list[str] = []
        self.unanswered: list[str] = []
        self.answered: list[str] = []
        self._holder: asyncio.StreamWriter | None = None
        self._token = 0

    async def push(self, obj: dict[str, Any]) -> None:
        assert self._holder is not None
        self._holder.write(json.dumps(obj).encode("utf-8") + b"\n")
        await self._holder.drain()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while line := await reader.readline():
                request = json.loads(line)
                cmd = request["cmd"]
                self.requests.append(request)
                self.received.append(cmd)
                if self.frozen:
                    if cmd not in self.answer_while_frozen:
                        self.unanswered.append(cmd)
                        continue
                    self.answer_while_frozen.remove(cmd)
                resp: dict[str, Any] = {"ok": True, "incarnation": self.incarnation}
                await asyncio.sleep(self.delay_s.get(cmd, 0.0))
                if cmd in self.reply_once:
                    resp = self.reply_once.pop(cmd)
                elif cmd == "ACQUIRE":
                    self._token += 1
                    resp["token"] = self._token
                    self._holder = writer
                self.answered.append(cmd)  # before the client can read the reply
                writer.write(json.dumps(resp).encode("utf-8") + b"\n")
                await writer.drain()
        except OSError:
            pass  # the client poisoned this connection mid-reply
        finally:
            # a handler that ends on EOF and leaves its writer open makes
            # 3.12's Server.wait_closed() wait for it forever (DL-45)
            writer.close()


async def _serve_freezable(short_root: Path) -> tuple[_FreezableSupervisor, asyncio.Server]:
    stub = _FreezableSupervisor()
    server = await asyncio.start_unix_server(stub.handle, path=str(short_root / "supervisor.sock"))
    return stub, server


async def _until(predicate: Callable[[], bool], why: str, within_s: float = 10.0) -> None:
    deadline = time.monotonic() + within_s
    while not predicate():
        assert time.monotonic() < deadline, why
        await asyncio.sleep(0.005)
