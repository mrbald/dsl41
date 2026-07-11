"""Clock, real-adapter, and result-mapping tests (phase 11b).

Normative spec: docs/runner-design.md ss6 (adapters), ss6a (the wrapper
tier), ss9 (time domains), and runner.py's own docstrings for RealClock,
LocalCommandAdapter, FileWatcherAdapter, and the Terminated/Failed/
AdapterResult contract. The wrapper crash matrix, kill-boundary tests, and
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
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsl41 import runner_wrapper as _wrapper
from dsl41.ir import JobIR, lower_source
from dsl41.oracle import Event
from dsl41.runner import (
    AdapterContext,
    AdapterResult,
    Engine,
    EngineError,
    Failed,
    FileWatcherAdapter,
    LocalCommandAdapter,
    RealClock,
    Terminated,
    VirtualClock,
    start_run,
)

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("the adapters/wrapper tier is POSIX-only", allow_module_level=True)

T0 = datetime(2026, 7, 1, 8, 0)


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


def test_real_clock_sleep_until_sleeps_roughly_the_delta() -> None:
    """(RealClock docstring): sleep_until is a plain sleep for the requested
    delta -- and a deadline already in the past returns immediately rather
    than blocking."""

    async def scenario() -> tuple[float, float]:
        clock = RealClock()
        t0 = time.monotonic()
        await clock.sleep_until(clock.now() + timedelta(seconds=0.2))
        forward = time.monotonic() - t0
        t1 = time.monotonic()
        await clock.sleep_until(clock.now() - timedelta(seconds=5))
        past = time.monotonic() - t1
        return forward, past

    forward, past = asyncio.run(scenario())
    assert 0.15 <= forward <= 1.0
    assert past < 0.1


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
    """(runner-design ss4 "the oracle decides, the shell kills"): KILLJOB a
    beat after STARTJOB terminates the real process; the store lands
    TERMINATED and status.json records the signal. No late natural-exit
    report ever arrives to overwrite it: a cancelled adapter's run() re-
    raises CancelledError instead of reaching the completion enqueue
    (runner.py module docstring), so drops stays empty even once real time
    passes the point the sleep would have exited on its own."""
    text = "insert_job: kj\njob_type: c\ncommand: sleep 2\nmachine: m1\n"
    run_root = tmp_path / "run"

    async def scenario() -> Engine:
        catalog = lower_source(text)
        clock = RealClock()
        engine = start_run(
            catalog, run_root, clock=clock, adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)}
        )
        now = clock.now()
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "kj"}))
        engine.inject(
            Event(at=now + timedelta(seconds=0.15), kind="KILLJOB", payload={"job": "kj"})
        )
        await engine.run_until_quiescent(datetime.max)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["kj"].status == "TERMINATED"
    assert engine.oracle.store.job["kj"].exit_code is None
    assert engine.drops == []
    status = json.loads((run_root / "runs" / "kj.1" / "status.json").read_text())
    assert status["outcome"] == "signaled"
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


# --------------------------------------------------- 5. wrapper unit helpers


def test_durable_write_leaves_no_temp_file_and_content_matches(tmp_path: Path) -> None:
    """(runner_wrapper.py durable_write docstring, the DL-41a durability
    liturgy): same-dir temp file, fsync, rename, fsync(directory) -- after
    the call only the final file exists, holding the exact bytes written."""
    target = tmp_path / "sub" / "record.json"
    target.parent.mkdir()
    _wrapper.durable_write(str(target), b"hello world")
    assert target.read_bytes() == b"hello world"
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_start_tokens_match_lstart_within_two_seconds_but_not_beyond() -> None:
    """(start_tokens_match docstring): macOS's `ps -o lstart=` resolves to
    whole seconds, so the PID-reuse guard tolerates +/-2s; beyond that it is
    a different process."""
    base = "lstart:Sat Jul 11 14:19:32 2026"
    within = "lstart:Sat Jul 11 14:19:34 2026"  # +2s: still matches
    beyond = "lstart:Sat Jul 11 14:19:35 2026"  # +3s: does not
    assert _wrapper.start_tokens_match(base, within) is True
    assert _wrapper.start_tokens_match(base, beyond) is False


def test_start_tokens_match_ticks_exact_only_and_mixed_forms_never_match() -> None:
    """(start_tokens_match docstring): Linux tick tokens compare exactly (no
    tolerance); a ticks token against an lstart token (or vice versa) never
    matches -- the two platforms' tokens are never comparable."""
    assert _wrapper.start_tokens_match("ticks:100", "ticks:100") is True
    assert _wrapper.start_tokens_match("ticks:100", "ticks:101") is False
    assert _wrapper.start_tokens_match("ticks:100", "lstart:Sat Jul 11 14:19:32 2026") is False
    assert _wrapper.start_tokens_match("lstart:Sat Jul 11 14:19:32 2026", "ticks:100") is False
