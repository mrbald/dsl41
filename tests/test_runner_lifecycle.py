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
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("lifecycle tier is POSIX-only", allow_module_level=True)

from dsl41 import runner_procid, runner_wrapper
from dsl41.ir import lower_source
from dsl41.runner import resume_run
from dsl41.runner_adapters import Failed, LocalCommandAdapter, Terminated, _resolve_spool
from dsl41.runner_clock import RealClock
from dsl41.runner_journal import catalog_hash, read_journal

WRAPPER = Path(runner_wrapper.__file__)
PROCID = Path(runner_procid.__file__)
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


def is_type_checking(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:`, in either spelling."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _collect_runtime_imports(node: ast.AST, imported: set[str]) -> None:
    if isinstance(node, ast.If) and is_type_checking(node.test):
        for stmt in node.orelse:  # the else branch is the one that runs
            _collect_runtime_imports(stmt, imported)
        return
    if isinstance(node, ast.Import):
        imported.update(alias.name.partition(".")[0] for alias in node.names)
        return
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0, "relative imports would reach into dsl41"
        assert node.module is not None
        imported.add(node.module.partition(".")[0])
        return
    for child in ast.iter_child_nodes(node):
        _collect_runtime_imports(child, imported)


def module_imports(path: Path) -> set[str]:
    """Top-level names a source file imports AT RUNTIME, read off the file
    itself (never by importing it -- the boundary is a property of what is on
    disk). The body of an `if TYPE_CHECKING:` does not count and its `else:`
    does: that branch is erased before the process starts, so the type-time
    alias `from dsl41.runner_procid import ...` the two by-path modules carry
    (DL-72) is not a runtime dependency on dsl41, while a real one -- at any
    nesting -- still is."""
    imported: set[str] = set()
    _collect_runtime_imports(ast.parse(path.read_text(encoding="utf-8")), imported)
    return imported


def procid_import_branches(path: Path) -> tuple[list[str], list[str]]:
    """The (type-time, runtime) halves of a by-path module's runner_procid
    import pair -- `if TYPE_CHECKING: from dsl41.runner_procid import ...` /
    `else: from runner_procid import ...` (DL-72) -- as sorted name lists."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.If) and is_type_checking(node.test)):
            continue
        type_time = [
            alias.name
            for stmt in node.body
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "dsl41.runner_procid"
            for alias in stmt.names
        ]
        runtime = [
            alias.name
            for stmt in node.orelse
            if isinstance(stmt, ast.ImportFrom) and stmt.module == "runner_procid"
            for alias in stmt.names
        ]
        if type_time or runtime:
            return sorted(type_time), sorted(runtime)
    raise AssertionError(f"{path.name} has no TYPE_CHECKING runner_procid import pair")


def test_wrapper_imports_are_stdlib_only() -> None:
    """DL-42 item 3: the wrapper is the future extraction boundary; its
    dumbness is a correctness property. Nothing from dsl41, nothing
    third-party -- enforced, not asserted. Its one non-stdlib RUNTIME import
    is the sibling stdlib-only runner_procid (DL-72), held to the same rule
    here; the type-time alias of the same file is not an import the process
    ever performs (see test_wrapper_procid_calls_are_type_checked)."""
    non_stdlib = sorted(module_imports(WRAPPER) - set(sys.stdlib_module_names))
    assert non_stdlib == ["runner_procid"], f"wrapper imports outside stdlib: {non_stdlib}"
    procid = sorted(module_imports(PROCID) - set(sys.stdlib_module_names))
    assert procid == [], f"runner_procid imports outside stdlib: {procid}"


def test_wrapper_procid_calls_are_type_checked() -> None:
    """DL-72's recorded residue, closed. mypy maps the sibling as
    dsl41.runner_procid and cannot also see it under its top-level name, so
    the by-path import alone left every helper call in this file Any-typed --
    in a file that kills process groups. The TYPE_CHECKING alias names the
    module mypy knows; both halves must import the same helpers, or the
    static types describe an import that does not happen."""
    type_time, runtime = procid_import_branches(WRAPPER)
    assert type_time == runtime
    assert "proc_start_token" in runtime  # the PID-reuse guard's own input


def test_type_checking_alias_is_what_types_the_by_path_helpers(tmp_path: Path) -> None:
    """Why the construct above, and not the bare by-path import it replaced
    (DL-72): over the same deliberately wrong call, the old shape's
    import-not-found ignore makes the helper Any and mypy says nothing; the
    alias resolves the real module and reports both arguments. Pinned on a
    snippet -- the two files' own shape is pinned by the tests either side."""
    if importlib.util.find_spec("mypy") is None:  # a dev dependency, not a runtime one
        pytest.skip("mypy is not installed")  # pragma: no cover
    # located, never imported: pulling mypy into THIS process would leave its
    # module graph resident for every later test. One invocation over both
    # probes, for the same reason -- a mypy run is the heaviest thing in this
    # file, and the textual pilot tests downstream are timing-sensitive.
    bad_call = 'verify_alive("not-an-int", 42)\n'
    (tmp_path / "aliased.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from dsl41.runner_procid import verify_alive\n"
        "else:\n"
        "    from runner_procid import verify_alive\n" + bad_call,
        encoding="utf-8",
    )
    (tmp_path / "bare.py").write_text(
        "from runner_procid import verify_alive  # type: ignore[import-not-found]\n" + bad_call,
        encoding="utf-8",
    )
    report = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / "cache"),
            str(tmp_path / "aliased.py"),
            str(tmp_path / "bare.py"),
        ],
        capture_output=True,
        text=True,
        # resolve dsl41 from the tree under test, installed or not; two
        # isolated snippets cannot hit the "source file found twice" that
        # rules MYPYPATH out for the repo's own run (DL-72)
        env={**os.environ, "MYPYPATH": str(PROCID.parent.parent)},
    ).stdout
    errors = [line for line in report.splitlines() if "[arg-type]" in line]
    assert len(errors) == 2, report  # both arguments of the aliased call...
    assert all("aliased.py" in line for line in errors), report  # ...and only there


def test_wrapper_records_under_pythonsafepath(tmp_path: Path, monkeypatch) -> None:
    """DL-72: the wrapper reaches runner_procid as a plain top-level module
    via its own directory on sys.path[0] -- exactly the entry PYTHONSAFEPATH=1
    strips. Without the guard the recorder would not even start; with it the
    outcome is recorded as always."""
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    run_dir = tmp_path / "safepath.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, "exit 5")
    assert proc.wait(timeout=10) == 0
    os.close(lifeline_w)
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "exited" and status["exit_code"] == 5


def test_importing_the_engine_leaves_sys_path_untouched() -> None:
    """DL-72: the wrapper and the supervisor prepend their own directory to
    reach runner_procid by top-level name, but the engine imports BOTH of them
    as ordinary package modules (for __file__ and SPEC_VERSION). A library must
    not leave its package directory on the importing process's sys.path -- there
    it would shadow top-level names (ir, cli, viz, ...) for the whole process.
    The guard therefore adds only what is missing and takes it back off."""
    probe = (
        "import sys; before = list(sys.path)\n"
        "import dsl41.runner\n"
        "assert sys.path == before, [p for p in sys.path if p not in before]\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


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
    assert spawn["boot_id"] == runner_procid.current_boot_id()


def test_wrapper_survives_external_group_kill_and_records_signal(tmp_path: Path) -> None:
    """DL-41a item 2 (the review-found bug, fixed in design): kill(-pgid)
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
    # status.json lands BEFORE the wrapper reaps: the command may still be a
    # zombie (kill(pid, 0) succeeds) for an instant on a slow box
    wait_for(lambda: not pid_alive(spawn["command_pid"]))


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
            runner_procid.current_boot_id(),
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
            runner_procid.current_boot_id(),
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
                    "boot_id": runner_procid.current_boot_id(),
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
        token = runner_procid.proc_start_token(innocent.pid)
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
        wait_for(lambda: not pid_alive(spawn["command_pid"]))  # zombie until the wrapper reaps

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

    from dsl41.runner import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import EngineError, VirtualClock

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

    from dsl41.runner import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import EngineError, VirtualClock

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


# ------------------------- review-finding regressions (DL-44 amendments)

T0 = datetime(2026, 7, 1, 8, 0)

KILL_JIL = """\
insert_job: x
job_type: c
command: sleep 300
term_run_time: 1

insert_job: y
job_type: c
command: true
condition: s(x)
"""


def _fabricate_exit_record(run_root: Path, job: str, run_number: int, ended_at: datetime) -> None:
    run_dir = run_root / "runs" / f"{job}.{run_number}"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "fabricated",
                "job": job,
                "run_number": run_number,
                "outcome": "exited",
                "exit_code": 0,
                "ended_at": ended_at.isoformat(),
            }
        )
    )


def test_b1_advance_fired_kill_beats_late_exit_record_at_resume(tmp_path: Path) -> None:
    """Review B1, the un-fired-timer half (DL-44 item 11b): the engine
    crashed BEFORE the term_run_time deadline ever fired, and the wrapper
    recorded a natural exit 0 (a SIGTERM-trapping command). At resume the
    replayed timer is still armed and due before the record's timestamp:
    the kill-wins gate must fire it first, drop the late exit record
    (journaled), and downstream s(x) must never run."""
    from datetime import timedelta

    from dsl41.runner import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock

    catalog = lower_source(KILL_JIL)
    run_root = tmp_path / "run"

    async def scenario() -> tuple[dict[str, str], list[dict]]:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        from dsl41.oracle import Event

        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0)  # x RUNNING; deadline armed, unfired
        assert engine.oracle.store.job["x"].status == "RUNNING"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()  # "crash": nothing after the STARTJOB is journaled

        _fabricate_exit_record(run_root, "x", 1, T0 + timedelta(minutes=2))
        resumed = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        statuses: dict[str, str] = {job: rt.status for job, rt in resumed.oracle.store.job.items()}
        return statuses, read_journal(run_root / "journal.jsonl")

    statuses, records = asyncio.run(scenario())
    assert statuses["x"] == "TERMINATED"  # the kill stands
    assert statuses["y"] == "INACTIVE"  # s(x) never satisfied
    drops = [r for r in records if r.get("rec") == "drop"]
    assert len(drops) == 1 and drops[0]["payload"]["exit_code"] == 0  # loud, not silent
    assert any(r.get("rec") == "advance" for r in records)  # the time observation


def test_b1_advance_record_replays_the_kill(tmp_path: Path) -> None:
    """Review B1, the fired-timer half (DL-44 item 11a): the engine fired
    the deadline live (advance journaled WAL-first), then crashed. Replay
    alone must reproduce TERMINATED, and the stale spool record is skipped
    without any reconcile injection."""
    from datetime import timedelta

    from dsl41.oracle import Event, Oracle
    from dsl41.runner import start_run
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_journal import replay_inputs

    catalog = lower_source(KILL_JIL)
    run_root = tmp_path / "run"

    async def scenario() -> tuple[dict[str, str], list[dict]]:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))  # deadline fires
        assert engine.oracle.store.job["x"].status == "TERMINATED"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

        _fabricate_exit_record(run_root, "x", 1, T0 + timedelta(minutes=2))
        resumed = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        statuses: dict[str, str] = {job: rt.status for job, rt in resumed.oracle.store.job.items()}
        return statuses, read_journal(run_root / "journal.jsonl")

    statuses, records = asyncio.run(scenario())
    assert statuses["x"] == "TERMINATED" and statuses["y"] == "INACTIVE"
    # the spool record was superseded by replayed truth: no reconcile input
    assert not [r for r in records if r.get("rec") == "input" and r.get("source") == "reconcile"]
    # and replay alone (journal render's path) reproduces the kill
    oracle = Oracle(lower_source(KILL_JIL))
    replay_inputs(oracle, records)
    assert oracle.store.job["x"].status == "TERMINATED"


def test_b1_gate_sees_due_kill_before_forged_completion() -> None:
    """Review B1, the live white-box half: a completion stamped after a due
    term_run_time deadline must lose to it -- the gate advances the oracle
    to the completion's instant first and then drops it as terminal."""
    from datetime import timedelta

    from dsl41.oracle import Event
    from dsl41.runner import Engine
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock

    async def scenario() -> None:
        engine = Engine(
            lower_source(KILL_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0)  # deadline armed, unfired
        engine._enqueue(
            Event(
                at=T0 + timedelta(minutes=2),
                kind="STATUS",
                payload={"job": "x", "run_number": 1, "exit_code": 0},
            ),
            is_completion=True,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["x"].status == "TERMINATED"  # kill wins
        assert engine.drops and "already terminal" in engine.drops[0][1]
        await engine.shutdown()

    asyncio.run(scenario())


def test_m3_malformed_status_records_map_truthfully() -> None:
    """Review M3: a lying or truncated record can only make things worse,
    never better -- and the cause must say what was actually wrong."""
    from dsl41.runner_adapters import _outcome_from_status

    malformed_exit = _outcome_from_status({"outcome": "exited"})
    assert isinstance(malformed_exit, Failed) and "malformed" in malformed_exit.cause
    stringly = _outcome_from_status({"outcome": "exited", "exit_code": "7"})
    assert isinstance(stringly, Failed) and "'7'" in stringly.cause
    unsigned = _outcome_from_status({"outcome": "signaled"})
    assert unsigned == Terminated("killed by signal (unrecorded)")
    unknown = _outcome_from_status({"outcome": "gremlins"})
    assert isinstance(unknown, Failed) and "unrecognized" in unknown.cause


def test_m4_resume_refuses_incomplete_fw_without_adapter(tmp_path: Path) -> None:
    """Review M4: an incomplete FW run whose re-dispatch adapter is missing
    at resume must refuse loudly, never hang RUNNING forever."""
    from dsl41.oracle import Event
    from dsl41.runner import start_run
    from dsl41.runner_adapters import FakeAdapter, FileWatcherAdapter
    from dsl41.runner_clock import EngineError, VirtualClock

    fw_jil = "insert_job: w\njob_type: f\nwatch_file: /nonexistent/watched\n"
    catalog = lower_source(fw_jil)
    run_root = tmp_path / "run"

    async def scenario() -> str:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "w"}))
        await engine.run_until_quiescent(T0)  # watcher parked mid-poll
        assert engine.oracle.store.job["w"].status == "RUNNING"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        try:
            await resume_run(
                catalog,
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter()},  # FW adapter forgotten
            )
        except EngineError as exc:
            return str(exc)
        return ""

    message = asyncio.run(scenario())
    assert "no FW adapter registered" in message


def test_m6_wrapper_spawn_failure_fails_job_not_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M6: an EMFILE-class glitch spawning the WRAPPER fails that
    one job with a truthful FAILURE cause; the engine loop survives and
    other jobs complete normally."""
    from dsl41.oracle import Event
    from dsl41.runner import start_run

    jil = (
        "insert_job: doomed\njob_type: c\ncommand: true\n\n"
        "insert_job: fine\njob_type: c\ncommand: true\n"
    )
    catalog = lower_source(jil)
    real_spawn = asyncio.create_subprocess_exec

    async def flaky_spawn(*args: object, **kwargs: object):
        if any("runner_wrapper" in str(a) for a in args) and flaky_spawn.fail:  # type: ignore[attr-defined]
            flaky_spawn.fail = False  # type: ignore[attr-defined]
            raise OSError(24, "Too many open files")
        return await real_spawn(*args, **kwargs)  # type: ignore[arg-type]

    flaky_spawn.fail = True  # type: ignore[attr-defined]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", flaky_spawn)

    async def scenario() -> tuple[dict[str, str], list[dict]]:
        clock = RealClock()
        engine = start_run(
            catalog,
            tmp_path / "run",
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(grace_seconds=2.0)},
        )
        # sequential so the single flaky spawn deterministically hits doomed
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "doomed"}))
        await engine.run_until_quiescent(datetime.max)
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "fine"}))
        await engine.run_until_quiescent(datetime.max)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        statuses: dict[str, str] = {job: rt.status for job, rt in engine.oracle.store.job.items()}
        return statuses, read_journal(tmp_path / "run" / "journal.jsonl")

    statuses, records = asyncio.run(scenario())
    assert statuses == {"doomed": "FAILURE", "fine": "SUCCESS"}
    causes = [
        r["payload"].get("cause")
        for r in records
        if r.get("rec") == "input" and r["payload"].get("job") == "doomed"
    ]
    assert any(c and "wrapper spawn failed" in str(c) for c in causes)
