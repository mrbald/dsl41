"""Phase 11b lifecycle-tier tests: the wrapper crash matrix and resume.

Normative spec: docs/runner-design.md ss6a/ss7/ss13 (DL-41a) and
docs/supervisor-protocol.md (the frozen spool format). DL-42 item 8 pins
the phase-boundary kill matrix exercised here via the wrapper's
DSL41_WRAPPER_TEST_PAUSE scaffolding: the wrapper SIGSTOPs itself at a
named boundary, the test SIGKILLs (or SIGCONTs) it there, and the
reconciliation ladder (`runner._resolve_spool`) must report what actually
happened -- truthfully, never guessed. The "post-fork pre-exec" boundary
from DL-42 is covered by post_spawn_pre_record: from the recorder's point
of view both mean "command pid exists, spawn.json does not" (wrapper
docstring).

House style: process-level tests spawn the wrapper BY FILE PATH exactly as
the engine does; ladder-level tests fabricate spool directories and call
the private `_resolve_spool` directly (white-box, like test_runner.py's
gate tests). The crash-recovery integration test drives a real engine
subprocess (tests/runner_crash_driver.py) and SIGKILLs it mid-run --
that single test also proves the lifeline fd-hygiene invariant through
the real adapter path: two concurrently spawned wrappers must BOTH see
EOF when the engine dies, which a leaked write end would prevent.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("lifecycle tier is POSIX-only", allow_module_level=True)

from dsl41 import runner_wrapper
from dsl41.ir import lower_source
from dsl41.runner import (
    Failed,
    LocalCommandAdapter,
    RealClock,
    Terminated,
    _resolve_spool,
    catalog_hash,
    read_journal,
    resume_run,
)

WRAPPER = Path(runner_wrapper.__file__)
DRIVER = Path(__file__).parent / "runner_crash_driver.py"


def wait_for(predicate, timeout_s: float = 10.0, interval_s: float = 0.05):
    """Poll until predicate() is truthy; return its value. Loud on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


def read_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def proc_state(pid: int) -> str:
    """First letter of the process state ('T' = stopped), '' if gone."""
    out = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return out.stdout.strip()[:1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def spawn_wrapper(
    run_dir: Path,
    command: str,
    *,
    grace_seconds: float = 2.0,
    pause: str | None = None,
) -> tuple[subprocess.Popen, int]:
    """Spawn the wrapper exactly as the engine does (file path, spec on
    stdin, lifeline read end via pass_fds). Returns (wrapper Popen, lifeline
    write fd) -- the caller owns the write end, per the fd-hygiene
    invariant."""
    lifeline_r, lifeline_w = os.pipe()
    spec = {
        "version": runner_wrapper.SPEC_VERSION,
        "run_id": "test-run",
        "job": run_dir.name.rsplit(".", 1)[0],
        "run_number": int(run_dir.name.rsplit(".", 1)[1]),
        "command": command,
        "run_dir": str(run_dir),
        "lifeline_fd": lifeline_r,
        "stdout_path": str(run_dir / "out.log"),
        "stderr_path": str(run_dir / "err.log"),
        "stdin_path": None,
        "grace_seconds": grace_seconds,
    }
    env = dict(os.environ)
    if pause:
        env[runner_wrapper.PAUSE_ENV] = pause
    else:
        env.pop(runner_wrapper.PAUSE_ENV, None)
    proc = subprocess.Popen(
        [sys.executable, str(WRAPPER)],
        stdin=subprocess.PIPE,
        pass_fds=(lifeline_r,),
        env=env,
    )
    os.close(lifeline_r)
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(spec).encode())
    proc.stdin.close()
    return proc, lifeline_w


# --------------------------------------------------------- import boundary


def test_wrapper_imports_are_stdlib_only() -> None:
    """DL-42 item 3: the wrapper is the future extraction boundary; its
    dumbness is a correctness property. Nothing from dsl41, nothing
    third-party -- enforced, not asserted."""
    tree = ast.parse(WRAPPER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would reach into dsl41"
            assert node.module is not None
            imported.add(node.module.partition(".")[0])
    non_stdlib = sorted(imported - set(sys.stdlib_module_names))
    assert non_stdlib == [], f"wrapper imports outside stdlib: {non_stdlib}"


# ------------------------------------------------------ wrapper happy paths


def test_wrapper_records_natural_exit_and_appends_stdout(tmp_path: Path) -> None:
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    (run_dir / "out.log").write_text("pre-existing\n")  # vendor APPENDS
    proc, lifeline_w = spawn_wrapper(run_dir, "echo hello; exit 7")
    assert proc.wait(timeout=10) == 0
    os.close(lifeline_w)
    spawn = read_json(run_dir / "spawn.json")
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "exited"
    assert status["exit_code"] == 7
    assert status["job"] == "j1" and status["run_number"] == 1
    assert (run_dir / "out.log").read_text() == "pre-existing\nhello\n"
    # ss6a duty 1: the command's own pgid, the wrapper outside it
    assert spawn["command_pgid"] == spawn["command_pid"] != spawn["wrapper_pid"]
    assert spawn["boot_id"] == runner_wrapper.current_boot_id()


def test_wrapper_survives_external_group_kill_and_records_signal(tmp_path: Path) -> None:
    """DL-41a item 2 (the codex-found bug, fixed in design): kill(-pgid)
    must never kill the recorder before it records."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "sleep 30")
    wait_for(lambda: (run_dir / "spawn.json").exists())
    spawn = read_json(run_dir / "spawn.json")
    os.killpg(spawn["command_pgid"], signal.SIGKILL)
    assert proc.wait(timeout=10) == 0  # the wrapper lived to record
    os.close(lifeline_w)
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "signaled"
    assert status["signal"] == signal.SIGKILL


def test_wrapper_graceful_sigterm_reaches_command_on_parent_loss(tmp_path: Path) -> None:
    """The SIG_IGN-inheritance regression (found by the 11b smoke): the
    wrapper ignores SIGTERM for itself, but the command must NOT inherit
    that disposition through exec -- parent loss must kill the command with
    the graceful SIGTERM, not the SIGKILL escalation."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    code = (
        "import json, os, subprocess, sys, time\n"
        "r, w = os.pipe()\n"
        "spec = json.loads(sys.argv[2])\n"
        "spec['lifeline_fd'] = r\n"
        "p = subprocess.Popen([sys.executable, sys.argv[1]], stdin=subprocess.PIPE,"
        " pass_fds=(r,))\n"
        "os.close(r)\n"
        "p.stdin.write(json.dumps(spec).encode()); p.stdin.close()\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    spec = {
        "version": runner_wrapper.SPEC_VERSION,
        "run_id": "test-run",
        "job": "j1",
        "run_number": 1,
        "command": "sleep 30",
        "run_dir": str(run_dir),
        "lifeline_fd": -1,  # the intermediate parent fills this in
        "stdout_path": str(run_dir / "out.log"),
        "stderr_path": str(run_dir / "err.log"),
        "stdin_path": None,
        "grace_seconds": 5.0,
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(WRAPPER), json.dumps(spec)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    assert parent.stdout.readline().strip() == "READY"
    wait_for(lambda: (run_dir / "spawn.json").exists())
    os.kill(parent.pid, signal.SIGKILL)  # lifeline EOF fires even under -9
    parent.wait()
    status = wait_for(
        lambda: (run_dir / "status.json").exists() and read_json(run_dir / "status.json")
    )
    assert status["outcome"] == "terminated"
    assert status["cause"] == "parent lost"
    assert status["observed"] == {"outcome": "signaled", "signal": signal.SIGTERM}
    spawn = read_json(run_dir / "spawn.json")
    assert not pid_alive(spawn["command_pid"])


def test_lifeline_write_end_leaks_nowhere(tmp_path: Path) -> None:
    """ss6a fd-hygiene invariant, the design-named leak test: one parent
    spawns TWO wrappers; a write end leaked into the sibling (or either
    command) would keep the pipe open past the parent's death and silently
    disable parent-loss detection. Kill the parent -9: BOTH must EOF and
    record."""
    dirs = [tmp_path / "a.1", tmp_path / "b.1"]
    for d in dirs:
        d.mkdir()
    code = (
        "import json, os, subprocess, sys, time\n"
        "for spec_json in sys.argv[2:]:\n"
        "    spec = json.loads(spec_json)\n"
        "    r, w = os.pipe()\n"
        "    spec['lifeline_fd'] = r\n"
        "    p = subprocess.Popen([sys.executable, sys.argv[1]], stdin=subprocess.PIPE,"
        " pass_fds=(r,))\n"
        "    os.close(r)\n"
        "    p.stdin.write(json.dumps(spec).encode()); p.stdin.close()\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    specs = [
        json.dumps(
            {
                "version": runner_wrapper.SPEC_VERSION,
                "run_id": f"test-{d.name}",
                "job": d.name.split(".")[0],
                "run_number": 1,
                "command": "sleep 60",
                "run_dir": str(d),
                "lifeline_fd": -1,
                "stdout_path": str(d / "out.log"),
                "stderr_path": str(d / "err.log"),
                "stdin_path": None,
                "grace_seconds": 2.0,
            }
        )
        for d in dirs
    ]
    parent = subprocess.Popen(
        [sys.executable, "-c", code, str(WRAPPER), *specs],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    assert parent.stdout.readline().strip() == "READY"
    for d in dirs:
        wait_for(lambda d=d: (d / "spawn.json").exists())
    os.kill(parent.pid, signal.SIGKILL)
    parent.wait()
    for d in dirs:
        status = wait_for(lambda d=d: (d / "status.json").exists() and read_json(d / "status.json"))
        assert status["outcome"] == "terminated", (d.name, status)
        assert status["cause"] == "parent lost"


# ---------------------------------------------- kill matrix (DL-42 item 8)


def _resolve(run_dir: Path, job: str = "j1", run_number: int = 1, **kw):
    return asyncio.run(
        _resolve_spool(
            job,
            run_number,
            run_dir,
            runner_wrapper.current_boot_id(),
            settle_seconds=kw.pop("settle_seconds", 1.0),
            grace_seconds=kw.pop("grace_seconds", 2.0),
        )
    )


def _kill_stopped_wrapper(proc: subprocess.Popen) -> None:
    wait_for(lambda: proc_state(proc.pid) == "T")
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()


def test_kill_before_spawn_record_is_unobservable_and_never_signals(tmp_path: Path) -> None:
    """-9 at post_spawn_pre_record (covers DL-42's post-fork pre-exec, see
    module docstring): the command exists but no spawn.json ever will. The
    ladder must report E7 unobservable and must NOT kill the unidentifiable
    survivor -- it finishes its work untouched (accepted residual matrix)."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    marker = run_dir / "survived.txt"
    proc, lifeline_w = spawn_wrapper(
        run_dir, f"sleep 1; echo done > {marker}", pause="post_spawn_pre_record"
    )
    _kill_stopped_wrapper(proc)
    os.close(lifeline_w)
    assert not (run_dir / "spawn.json").exists()
    result, ended_at = _resolve(run_dir)
    assert result == Failed("exit_status_unobservable")
    assert ended_at is None
    # the orphaned command was never signaled: it completes on its own
    wait_for(lambda: marker.exists(), timeout_s=10.0)


def test_kill_after_spawn_record_survivor_killed_at_resume(tmp_path: Path) -> None:
    """-9 at post_record: spawn.json exists, wrapper dead, command group
    verified alive -> the ladder kills it and reports TERMINATED 'wrapper
    lost; killed at resume' (a kill that actually happened)."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "sleep 120", pause="post_record")
    wait_for(lambda: (run_dir / "spawn.json").exists())
    _kill_stopped_wrapper(proc)
    os.close(lifeline_w)
    spawn = read_json(run_dir / "spawn.json")
    assert pid_alive(spawn["command_pid"])  # the survivor
    result, ended_at = _resolve(run_dir)
    assert result == Terminated("wrapper lost; killed at resume")
    assert ended_at is None
    wait_for(lambda: not pid_alive(spawn["command_pid"]))


def test_kill_between_wait_and_status_write_is_unobservable(tmp_path: Path) -> None:
    """-9 at post_wait_pre_status: the exit was OBSERVED but never recorded
    -- observation without a record is worthless, and the ladder must say
    unobservable (E7), never guess the exit code."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "exit 5", pause="post_wait_pre_status")
    _kill_stopped_wrapper(proc)
    os.close(lifeline_w)
    assert (run_dir / "spawn.json").exists()
    assert not (run_dir / "status.json").exists()
    result, _ = _resolve(run_dir)
    assert result == Failed("exit_status_unobservable")


def test_kill_between_status_write_and_reap_preserves_outcome(tmp_path: Path) -> None:
    """-9 at post_status_pre_reap: the record is already durable; the
    ladder reads the REAL completion (record-first-reap-after is exactly
    what makes this window safe)."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "exit 5", pause="post_status_pre_reap")
    wait_for(lambda: (run_dir / "status.json").exists())
    _kill_stopped_wrapper(proc)
    os.close(lifeline_w)
    result, ended_at = _resolve(run_dir)
    assert result == 5  # raw exit code; SEM-09 stays oracle-side
    assert ended_at is not None


def test_live_wrapper_gets_a_settle_window(tmp_path: Path) -> None:
    """ss7 ladder rung 1: a wrapper verified alive is mid-record; the
    ladder waits for its status.json instead of killing or guessing. Here
    the wrapper is frozen at post_wait_pre_status and released mid-settle."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "exit 5", pause="post_wait_pre_status")
    wait_for(lambda: proc_state(proc.pid) == "T")

    async def scenario():
        async def release():
            await asyncio.sleep(0.5)
            os.kill(proc.pid, signal.SIGCONT)

        releaser = asyncio.get_running_loop().create_task(release())
        result = await _resolve_spool(
            "j1",
            1,
            run_dir,
            runner_wrapper.current_boot_id(),
            settle_seconds=5.0,
            grace_seconds=2.0,
        )
        await releaser
        return result

    result, ended_at = asyncio.run(scenario())
    assert result == 5
    assert ended_at is not None
    assert proc.wait(timeout=10) == 0
    os.close(lifeline_w)


def test_spoofed_spawn_json_never_signals_innocents(tmp_path: Path) -> None:
    """DL-42 item 8 'pid reuse (spoofed spawn.json)': a live innocent pid
    with a non-matching start-time token must never be signaled; the run
    resolves unobservable."""
    innocent = subprocess.Popen(["sleep", "30"])
    try:
        run_dir = tmp_path / "j1.1"
        run_dir.mkdir()
        stale_token = (
            "lstart:Mon Jan  1 00:00:00 2001" if not sys.platform.startswith("linux") else "ticks:1"
        )
        (run_dir / "spawn.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": "spoof",
                    "job": "j1",
                    "run_number": 1,
                    "wrapper_pid": innocent.pid,
                    "wrapper_start_time": stale_token,
                    "command_pid": innocent.pid,
                    "command_pgid": innocent.pid,
                    "command_start_time": stale_token,
                    "boot_id": runner_wrapper.current_boot_id(),
                    "started_at": "2026-07-11T00:00:00+00:00",
                }
            )
        )
        result, _ = _resolve(run_dir)
        assert result == Failed("exit_status_unobservable")
        assert pid_alive(innocent.pid)  # untouched
    finally:
        innocent.kill()
        innocent.wait()


def test_boot_id_flip_voids_liveness_and_resolves_from_records(tmp_path: Path) -> None:
    """DL-42 item 5: a foreign boot_id proves nothing survived -- liveness
    checks are skipped entirely (a matching pid would be a recycled one)
    and the run resolves from status.json or E7."""
    innocent = subprocess.Popen(["sleep", "30"])
    try:
        token = runner_wrapper.proc_start_token(innocent.pid)
        assert token is not None
        run_dir = tmp_path / "j1.1"
        run_dir.mkdir()
        (run_dir / "spawn.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": "rebooted",
                    "job": "j1",
                    "run_number": 1,
                    "wrapper_pid": innocent.pid,
                    "wrapper_start_time": token,  # WOULD verify, wrong boot
                    "command_pid": innocent.pid,
                    "command_pgid": innocent.pid,
                    "command_start_time": token,
                    "boot_id": "0000-not-this-boot",
                    "started_at": "2026-07-11T00:00:00+00:00",
                }
            )
        )
        result, _ = _resolve(run_dir)
        assert result == Failed("exit_status_unobservable")
        assert pid_alive(innocent.pid)  # never signaled despite the token match
    finally:
        innocent.kill()
        innocent.wait()


# ------------------------------------------- crash recovery (ss13 item 3)


def test_sigkill_engine_midrun_then_resume(tmp_path: Path) -> None:
    """The flagship 11b test: a real engine (RealClock + wrapper adapters)
    is SIGKILLed mid-run. Tethered semantics record everything: the fast
    job's completion is already in the WAL; both slow jobs' wrappers see
    lifeline EOF, kill their commands, and record 'parent lost'. Resume
    replays the journal, walks the ladder, and lands every job in a
    truthful terminal state -- and because the two slow wrappers were
    spawned concurrently by one engine, both EOFing also proves the
    lifeline fd-hygiene invariant through the real adapter path."""
    run_root = tmp_path / "run"
    driver = subprocess.Popen(
        [sys.executable, str(DRIVER), str(run_root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert driver.stdout is not None
        assert driver.stdout.readline().strip() == "DRIVER-READY"
        journal_path = run_root / "journal.jsonl"

        def fast_completion_journaled() -> bool:
            if not journal_path.exists():
                return False
            return any(
                '"exit_code"' in line and '"fast"' in line
                for line in journal_path.read_text().splitlines()
            )

        # wait until the fast completion is journaled and both slows dispatched
        wait_for(fast_completion_journaled)
        for job in ("slow_one", "slow_two"):
            wait_for(lambda job=job: (run_root / "runs" / f"{job}.1" / "spawn.json").exists())
        os.kill(driver.pid, signal.SIGKILL)
        driver.wait()
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()

    # tethered: both wrappers record parent-lost kills without any help
    for job in ("slow_one", "slow_two"):
        status = wait_for(
            lambda job=job: (
                (run_root / "runs" / f"{job}.1" / "status.json").exists()
                and read_json(run_root / "runs" / f"{job}.1" / "status.json")
            )
        )
        assert status["outcome"] == "terminated"
        assert status["cause"] == "parent lost"
        spawn = read_json(run_root / "runs" / f"{job}.1" / "spawn.json")
        assert not pid_alive(spawn["command_pid"])

    from runner_crash_driver import CRASH_JIL

    catalog = lower_source(CRASH_JIL)

    async def resume() -> dict[str, str]:
        engine = await resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
            settle_seconds=1.0,
            grace_seconds=2.0,
        )
        from datetime import datetime

        await engine.run_until_quiescent(datetime.max)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return {job: rt.status for job, rt in engine.oracle.store.job.items()}

    statuses = asyncio.run(resume())
    assert statuses == {"fast": "SUCCESS", "slow_one": "TERMINATED", "slow_two": "TERMINATED"}

    records = read_journal(run_root / "journal.jsonl")
    sources = {r["source"] for r in records if r.get("rec") == "input"}
    assert "reconcile" in sources  # the ladder injections are in the WAL
    reconciled = [r for r in records if r.get("rec") == "input" and r.get("source") == "reconcile"]
    assert {r["payload"]["job"] for r in reconciled} == {"slow_one", "slow_two"}
    assert all(r["payload"]["status"] == "TERMINATED" for r in reconciled)
    assert all("ended_at" in r["payload"] for r in reconciled)  # true end time (ss7)


def test_resume_refuses_catalog_drift(tmp_path: Path) -> None:
    """ss7 resume step 1: a changed estate re-baselines explicitly."""
    from datetime import datetime

    from runner_crash_driver import CRASH_JIL

    from dsl41.runner import EngineError, FakeAdapter, VirtualClock, start_run

    run_root = tmp_path / "run"
    catalog = lower_source(CRASH_JIL)
    changed = lower_source(CRASH_JIL.replace("sleep 120", "sleep 121"))
    assert catalog_hash(catalog) != catalog_hash(changed)

    async def scenario() -> str:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
            adapters={"CMD": FakeAdapter()},
        )
        assert engine.journal is not None
        engine.journal.close()
        try:
            await resume_run(
                changed,
                run_root,
                clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
                adapters={"CMD": FakeAdapter()},
            )
        except EngineError as exc:
            return str(exc)
        return ""

    message = asyncio.run(scenario())
    assert "catalog hash mismatch" in message


def test_resume_refuses_clock_domain_flip(tmp_path: Path) -> None:
    from runner_crash_driver import CRASH_JIL
    from datetime import datetime

    from dsl41.runner import EngineError, FakeAdapter, VirtualClock, start_run

    catalog = lower_source(CRASH_JIL)
    run_root = tmp_path / "run"

    async def scenario() -> str:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
            adapters={"CMD": FakeAdapter()},
        )
        assert engine.journal is not None
        engine.journal.close()
        try:
            await resume_run(
                catalog,
                run_root,
                clock=RealClock(),
                adapters={"CMD": LocalCommandAdapter()},
            )
        except EngineError as exc:
            return str(exc)
        return ""

    message = asyncio.run(scenario())
    assert "clock-domain mismatch" in message
