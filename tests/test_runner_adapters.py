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
import signal
import subprocess
import sys
import time
import os
import uuid
from datetime import UTC, datetime, timedelta

from pathlib import Path
from typing import Any

import pytest

from dsl41 import runner_procid as _procid
from dsl41.ir import JobIR, lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_startup import start_run
from dsl41.runner_adapters import (
    AdapterContext,
    AdapterResult,
    Failed,
    FileWatcherAdapter,
    LocalCommandAdapter,
    SupervisedCommandAdapter,
    SupervisorClient,
    Terminated,
    spool_names_run,
)
from dsl41.runner_clock import EngineError, RealClock, VirtualClock

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
    engine._enqueue = seen.append  # type: ignore[method-assign]
    asyncio.run(engine._run_adapter(engine.oracle.catalog.jobs["j"], 1, _ReturnsAdapter(result)))
    [event] = seen
    assert {event.payload["job"], event.payload["run_number"]} == {"j", 1}
    return {k: v for k, v in event.payload.items() if k not in ("job", "run_number")}


def _resumed_status(run_root: Path, text: str, result: object, monkeypatch) -> dict:
    """What the RESUME ladder puts on the same input for the same result."""
    from dsl41 import runner_startup
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import _reconcile

    engine = start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},  # parks the run: RUNNING at resume
    )
    seen: list[dict] = []

    async def _resolve(*_a: Any, **_kw: Any) -> Any:
        return result, None

    def _capture(_engine, _job, _run_number, extras, *, at, last_at) -> None:
        seen.append(extras)

    monkeypatch.setattr(runner_startup, "resolve_spool", _resolve)
    monkeypatch.setattr(runner_startup, "_inject_completion", _capture)

    async def scenario() -> None:
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        # the ladder's candidate set is (dispatch records + runs/), and
        # `FakeAdapter` writes neither -- so the run is handed the one
        # record a dispatched CMD run really leaves
        records = [{"rec": "dispatch", "job": "j", "run_number": 1, "run_dir": None}]
        try:
            await _reconcile(engine, records, T0, settle_seconds=0.0, grace_seconds=0.0)
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
    [extras] = seen
    return dict(extras)


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


class _ClientWire:
    """In-memory transport with the real request lock and reply/push reader."""

    def __init__(self, client: SupervisorClient) -> None:
        self.client = client
        self.stream = asyncio.StreamReader()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        client._writer = self  # type: ignore[assignment]
        client.lost = asyncio.Event()
        client._reader_task = asyncio.create_task(client._reader(self.stream, client.lost))

    def write(self, data: bytes) -> None:
        self.sent.put_nowait(json.loads(data))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.stream.feed_eof()

    async def wait_closed(self) -> None:
        pass

    async def expect(self, command: str) -> dict[str, Any]:
        request = await asyncio.wait_for(self.sent.get(), timeout=3.0)
        assert request["cmd"] == command
        return request

    def reply(self, value: dict[str, Any]) -> None:
        self.stream.feed_data(json.dumps(value).encode() + b"\n")


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


def test_dl210_client_never_unlinks_a_refused_supervisor_socket(
    tmp_path: Path, monkeypatch
) -> None:
    async def refused(*args, **kwargs):
        raise ConnectionRefusedError("bind happened; listen has not")

    monkeypatch.setattr(asyncio, "open_unix_connection", refused)
    path = tmp_path / "supervisor.sock"
    path.write_bytes(b"supervisor owns this pathname")
    inode = path.stat().st_ino
    assert asyncio.run(SupervisorClient(tmp_path)._try_connect()) is False
    assert path.stat().st_ino == inode
    assert path.read_bytes() == b"supervisor owns this pathname"


@pytest.mark.parametrize(
    "command", ["PING", "ACQUIRE", "RENEW", "SPAWN", "SIGNAL", "LIST", "RELEASE", "SHUTDOWN"]
)
def test_dl210_any_reply_arms_one_list_net_for_several_waits(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        futures = [client.exit_future(rid) for rid in ("a", "b", "c")]
        marks = [client.watch_exit(rid) for rid in ("a", "b", "c")]
        try:
            request = asyncio.create_task(client._request({"cmd": command}))
            await wire.expect(command)
            wire.reply({"ok": False, "error": "synthetic refusal", "pushes_dropped": 2})
            assert (await request)["ok"] is False
            net = client._list_task
            assert net is not None
            await wire.expect("LIST")
            wire.reply({"ok": True, "runs": [_list_row("b")], "pushes_dropped": 1})
            await asyncio.wait_for(marks[0].wait(), 3.0)
            await asyncio.wait_for(marks[2].wait(), 3.0)
            assert not marks[1].is_set()
            assert all(not future.done() for future in futures)
            assert client._list_task is net
            for rid in ("a", "b", "c"):
                client.forget_exit(rid)
            await asyncio.sleep(0.03)
            assert wire.sent.empty()  # no LIST per waiter and none while dormant
            assert not net.done()
            # A later wait wakes the SAME task without another hint.
            later = client.watch_exit("later")
            await wire.expect("LIST")
            wire.reply({"ok": True, "runs": []})
            await asyncio.wait_for(later.wait(), 3.0)
            assert client._list_task is net
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reply",
    [{"ok": False}, {"ok": None}, {}, {"ok": 1}, {"ok": "true"}, [], None],
)
def test_dl210_refused_list_marks_nothing_and_duplicate_probe_stays_alive(
    tmp_path: Path, monkeypatch, reply
) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        dead = client.watch_exit("run")
        client._arm_list_recheck()
        try:
            await wire.expect("LIST")
            wire.reply(reply)
            await wire.expect("LIST")
            assert not dead.is_set()
            wire.reply({"ok": True, "runs": [_list_row("run")]})
            probe = asyncio.create_task(SupervisedCommandAdapter(client)._listed_alive("run"))
            await wire.expect("LIST")
            wire.reply(reply)
            assert await probe is True
            assert not dead.is_set()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_dl210_list_snapshot_cannot_judge_a_wait_accepted_during_its_request(
    tmp_path: Path, monkeypatch
) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        old = client.watch_exit("old")
        client._arm_list_recheck()
        future = client.exit_future("new")  # registered BEFORE SPAWN
        try:
            await wire.expect("LIST")
            spawn = asyncio.create_task(client.spawn({"run_id": "new"}))
            wire.reply({"ok": True, "runs": []})
            await wire.expect("SPAWN")
            assert "new" not in client._listed_dead
            assert not future.done()
            wire.reply({"ok": True, "run_id": "new"})
            await spawn
            dead = client.watch_exit("new")
            await asyncio.wait_for(old.wait(), 3.0)
            assert not dead.is_set()
            # A reattached wait registered while LIST is in flight must also
            # be excluded from that request's older eligibility snapshot.
            client.forget_exit("old")
            await wire.expect("LIST")
            late = client.watch_exit("late")
            wire.reply({"ok": True, "runs": []})
            await asyncio.wait_for(dead.wait(), 3.0)
            assert not late.is_set()
            assert not future.done()
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("ladder", ["unobservable", "settle", "surviving_command"])
def test_dl210_dropped_fresh_exit_reaches_the_existing_spool_ladder(
    tmp_path: Path, monkeypatch, ladder: str
) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)
    monkeypatch.setattr(_procid, "current_boot_id", lambda: "synthetic-boot")
    rid = str(uuid.uuid4())
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    status = {
        "version": 1,
        "run_id": rid,
        "job": "j",
        "run_number": 1,
        "outcome": "exited",
        "exit_code": 7,
        "ended_at": "2026-09-18T08:00:00+00:00",
    }
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": rid,
                "job": "j",
                "run_number": 1,
                "boot_id": "synthetic-boot",
                "wrapper_pid": 111,
                "wrapper_start_time": "ticks:111",
                "command_pid": 222,
                "command_pgid": 222,
                "command_start_time": "ticks:222",
            }
        )
    )
    checked: list[int] = []
    signals: list[tuple[int, int]] = []

    def alive(pid: int, token: str) -> bool:
        checked.append(pid)
        if ladder == "settle" and pid == 111:
            if checked.count(111) == 2:
                (run_dir / "status.json").write_text(json.dumps(status))
            return True
        return ladder == "surviving_command" and pid == 222

    monkeypatch.setattr(_procid, "verify_alive", alive)
    monkeypatch.setattr(_procid, "killpg_quiet", lambda pid, sig: signals.append((pid, sig)))

    async def scenario() -> AdapterResult:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        adapter = SupervisedCommandAdapter(client, grace_seconds=0.0, settle_seconds=0.5)
        job = lower_source("insert_job: j\njob_type: c\ncommand: true\n").jobs["j"]
        ctx = AdapterContext(clock=RealClock(), run_root=tmp_path, run_id=rid)
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await wire.expect("SPAWN")
            future = client.exit_future(rid)
            assert rid not in client._listed_dead
            wire.reply({"ok": True, "run_id": rid, "pushes_dropped": 1})
            await wire.expect("LIST")
            # No exit push and no status record: only this LIST can release
            # the fresh wait, and it must not fabricate wrapper_rc.
            wire.reply({"ok": True, "runs": []})
            result = await asyncio.wait_for(task, 3.0)
            assert not future.done()
            assert client._listed_dead == {}
            return result
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    result = asyncio.run(scenario())
    assert checked  # resolution reached the existing liveness ladder
    if ladder == "unobservable":
        assert result == Failed("exit_status_unobservable")
        assert signals == []
    elif ladder == "settle":
        assert result == 7
        assert checked.count(111) >= 2
        assert signals == []
    else:
        assert result == Terminated("wrapper lost; killed at resume")
        assert signals == [(222, signal.SIGTERM), (222, signal.SIGKILL)]


def test_dl210_reconnect_arms_net_without_drop_hint(tmp_path: Path, monkeypatch) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wires: list[_ClientWire] = []

        async def connect() -> bool:
            wires.append(_ClientWire(client))
            return True

        monkeypatch.setattr(client, "_try_connect", connect)
        future = client.exit_future("run")
        dead = client.watch_exit("run")
        try:
            assert await client.reconnect()
            await wires[-1].expect("LIST")
            wires[-1].reply({"ok": True, "runs": []})
            await asyncio.wait_for(dead.wait(), 3.0)
            assert not future.done()
        finally:
            await client.close()

    asyncio.run(scenario())


def test_dl210_forget_does_not_cancel_list_and_close_cancels_outside_request(
    tmp_path: Path, monkeypatch
) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        cancelled_in_request: list[bool] = []
        original = client._request

        async def request(obj: dict[str, Any], *, _connect: bool = True) -> dict[str, Any]:
            try:
                return await original(obj, _connect=_connect)
            except asyncio.CancelledError:
                cancelled_in_request.append(True)
                raise

        monkeypatch.setattr(client, "_request", request)
        client.watch_exit("run")
        client._arm_list_recheck()
        await wire.expect("LIST")
        net = client._list_task
        client.forget_exit("run")
        assert net is not None and not net.done()
        # A normal request queues behind LIST. Forgetting the last wait must
        # leave that reply aligned with LIST, then PING gets its own reply.
        ping = asyncio.create_task(client._request({"cmd": "PING"}))
        wire.reply({"ok": True, "runs": []})
        await wire.expect("PING")
        wire.reply({"ok": True, "ping_marker": "own reply"})
        assert (await ping)["ping_marker"] == "own reply"
        client.watch_exit("other")
        await wire.expect("LIST")
        await asyncio.wait_for(client.close(), 3.0)  # pending LIST is failed by reader shutdown
        assert cancelled_in_request == []
        assert net.done()
        assert client._list_task is None
        assert client._writer is None

    asyncio.run(scenario())


def test_dl210_malformed_shared_list_fails_the_adapter_loudly(tmp_path: Path, monkeypatch) -> None:
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)

    async def scenario() -> None:
        client = SupervisorClient(tmp_path)
        wire = _ClientWire(client)
        client._arm_list_recheck()
        adapter = SupervisedCommandAdapter(client)
        task = asyncio.create_task(adapter._await_outcome("run", tmp_path / "j.1", "j", 1))
        try:
            await wire.expect("LIST")
            wire.reply({"ok": True, "runs": [{**_list_row("run"), "run_number": "1"}]})
            with pytest.raises(EngineError, match="malformed run row"):
                await asyncio.wait_for(task, 3.0)
            assert client._exit_futures == {}
            assert client._listed_dead == {}
        finally:
            await client.close()

    asyncio.run(scenario())


def test_dl210_close_during_connect_never_publishes_a_new_reader(tmp_path: Path, monkeypatch):
    from dsl41 import runner_adapters

    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0.01)
    (tmp_path / "supervisor.sock").touch()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        reader = asyncio.StreamReader()

        class Writer:
            closed = False

            def close(self):
                self.closed = True

            def write(self, _data):
                pytest.fail("a connection completing during close must send nothing")

        writer = Writer()

        async def connect(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return reader, writer

        monkeypatch.setattr(asyncio, "open_unix_connection", connect)
        client = SupervisorClient(tmp_path)
        client.watch_exit("run")
        client._arm_list_recheck()
        await asyncio.wait_for(entered.wait(), 3)
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)  # close reaches its in-flight request barrier
        assert client._closed and not client._list_idle.is_set()
        release.set()
        try:
            await asyncio.wait_for(closing, 3)
            assert writer.closed
            assert client._reader_task is None and client._writer is None
            assert client._list_task is None
        finally:
            reader.feed_eof()

    asyncio.run(scenario())
