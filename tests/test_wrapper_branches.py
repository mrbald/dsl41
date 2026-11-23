"""Branch tests for `dsl41.runner_wrapper` that the lifecycle suite does not
reach (DL-42, DL-229, DL-265).

Normative spec: `docs/supervisor-protocol.md` ss3-ss4 (the wrapper's duties,
its spec refusal and its records). Pure helpers run in this process. The
kill escalation runs real process groups. Three paths of `main()` only exist
as races (a failing SIGCHLD wake-up write; a command that exits as the
lifeline closes; an exit whose Python handler has not run when the select
starts), so a driver (tests/wrapper_branch_driver.py) makes each certain and
the test reads the record the wrapper wrote. Each refusal has a twin that
does not trigger it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_runner_lifecycle import (
    _at_pause,
    _continue_and_wait,
    read_json,
    spawn_wrapper,
    wait_for,
)

from dsl41 import runner_procid, runner_wrapper
from dsl41.runner_attempts import SPAWN_RECORD_FAILED

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("lifecycle tier is POSIX-only", allow_module_level=True)

DRIVER = Path(__file__).parent / "wrapper_branch_driver.py"

# ------------------------------------------------------------ spec refusal


def test_wrapper_spec_refusal_accepts_a_spec_without_grace_seconds() -> None:
    """`grace_seconds` is optional (the wrapper defaults it); only a present
    one is checked. The twins below refuse each mistyped form."""
    assert runner_wrapper._spec_refusal({"version": 1, "lifeline_fd": 3}) is None
    assert (
        runner_wrapper._spec_refusal({"version": 1, "lifeline_fd": 3, "grace_seconds": 0}) is None
    )
    assert runner_wrapper._spec_refusal({"version": 1, "grace_seconds": 2.5}) is None
    for bad in (True, "1", None, -1, float("nan"), float("inf"), 10**400):
        refusal = runner_wrapper._spec_refusal({"version": 1, "grace_seconds": bad})
        assert refusal is not None and "grace_seconds" in refusal
    assert "unsupported spec version" in (runner_wrapper._spec_refusal({"version": True}) or "")
    assert "lifeline_fd" in (runner_wrapper._spec_refusal({"version": 1, "lifeline_fd": "3"}) or "")


def test_wrapper_spec_refusal_checks_a_present_env() -> None:
    """`env` is optional, the invocation the attempt's `dispatch` record holds; a present one
    is an object of strings, or the spec is refused before anything spawns."""
    assert runner_wrapper._spec_refusal({"version": 1, "env": {"DSL41_JOB": "j"}}) is None
    for bad in (["DSL41_JOB"], {"DSL41_RUN_NUMBER": 1}, {1: "j"}, "DSL41_JOB=j"):
        refusal = runner_wrapper._spec_refusal({"version": 1, "env": bad})
        assert refusal is not None and "env" in refusal


# --------------------------------------------------------------- _drain


def test_wrapper_drain_stops_at_an_empty_pipe() -> None:
    """A nonblocking self-pipe is emptied while its writer stays open (DL-321)."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    try:
        os.write(w, b"xyz")
        runner_wrapper._drain(r)
        with pytest.raises(BlockingIOError):
            os.read(r, 1)
    finally:
        os.close(r)
        os.close(w)


# ------------------------------------------------- _restore_default_signals


def test_wrapper_child_side_reset_restores_every_ignored_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command must not inherit the recorder's SIG_IGN dispositions: each
    signal in `_RECORDER_IGNORED` is reset to its default. (It runs between
    fork and exec, where no coverage data is saved, so it is driven here
    against a recording stand-in for the signal module.)"""
    calls: list[tuple[int, object]] = []
    fake = SimpleNamespace(
        signal=lambda sig, handler: calls.append((sig, handler)), SIG_DFL="default"
    )
    monkeypatch.setattr(runner_wrapper, "signal", fake)
    runner_wrapper._restore_default_signals()
    assert calls == [(sig, "default") for sig in runner_wrapper._RECORDER_IGNORED]
    assert len(calls) == 6


# ------------------------------------------------------------ _observe_exit


def _sh(script: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(["/bin/sh", "-c", script], process_group=0)


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="the waitid arm needs os.waitid")
def test_wrapper_observe_exit_waitid_reports_exit_signal_running_and_reaped() -> None:
    """The waitid route observes without reaping: a normal exit, a signal
    death, a still-running child (None) and an already-reaped one (None)."""
    exiting = _sh("exit 3")
    assert wait_for(lambda: runner_wrapper._observe_exit(exiting)) == {
        "outcome": "exited",
        "exit_code": 3,
    }
    assert runner_wrapper._observe_exit(exiting) is not None  # still observable: not reaped
    exiting.wait()
    assert runner_wrapper._observe_exit(exiting) is None  # reaped: nothing left to observe

    killed = _sh("kill -9 $$")
    assert wait_for(lambda: runner_wrapper._observe_exit(killed)) == {
        "outcome": "signaled",
        "signal": signal.SIGKILL,
    }
    killed.wait()

    running = _sh("sleep 30")
    try:
        assert runner_wrapper._observe_exit(running) is None
    finally:
        os.killpg(running.pid, signal.SIGKILL)
        running.wait()


def _stopped(pid: int) -> bool:
    """Whether `pid` is stopped by a signal: state T in /proc on Linux, in
    `ps` on macOS."""
    if sys.platform.startswith("linux"):
        with open(f"/proc/{pid}/stat", "rb") as f:
            return f.read().rsplit(b")", 1)[1].split()[0] == b"T"
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    return out.stdout.strip().startswith("T")


@pytest.mark.skipif(not hasattr(os, "waitid"), reason="the waitid arm needs os.waitid")
def test_wrapper_observe_exit_waitid_reads_a_stopped_command_as_still_running() -> None:
    """A real command stopped by SIGSTOP is alive. macOS reports it through
    `waitid(WEXITED)` with the stop signal as its status; the wrapper must not
    record that as a signal death. After SIGCONT the command's own exit is the
    one observed."""
    child = _sh("kill -STOP $$; exit 5")
    try:
        wait_for(lambda: _stopped(child.pid))
        assert runner_wrapper._observe_exit(child) is None
        os.killpg(child.pid, signal.SIGCONT)
        assert wait_for(lambda: runner_wrapper._observe_exit(child)) == {
            "outcome": "exited",
            "exit_code": 5,
        }
    finally:
        if child.poll() is None:  # a failed check left it stopped or running
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()


class _Reports:
    """The `os` module with `waitid` answering one fixed report, so every
    host takes each `si_code` arm, whatever its own kernel reports."""

    def __init__(self, si_code: int, si_status: int) -> None:
        self.report = SimpleNamespace(si_pid=4242, si_code=si_code, si_status=si_status)

    def waitid(self, *_args: object) -> SimpleNamespace:
        return self.report

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.mark.parametrize(
    ("si_code", "si_status", "expected"),
    [
        (os.CLD_STOPPED, signal.SIGSTOP, None),
        (os.CLD_STOPPED, signal.SIGTSTP, None),
        (os.CLD_CONTINUED, signal.SIGCONT, None),
        (os.CLD_TRAPPED, signal.SIGTRAP, None),
        (os.CLD_EXITED, 5, {"outcome": "exited", "exit_code": 5}),
        (os.CLD_KILLED, signal.SIGKILL, {"outcome": "signaled", "signal": signal.SIGKILL}),
        (os.CLD_DUMPED, signal.SIGABRT, {"outcome": "signaled", "signal": signal.SIGABRT}),
    ],
    ids=["stopped", "tty-stopped", "continued", "trapped", "exited", "killed", "dumped"],
)
def test_wrapper_observe_exit_maps_each_waitid_report(
    monkeypatch: pytest.MonkeyPatch, si_code: int, si_status: int, expected: object
) -> None:
    """A stop, a continue or a trace trap is a live command (None); an exit
    is `exited`, a kill or a core dump is `signaled`. The real stopped
    command above shows the first case where the host reports it."""
    monkeypatch.setattr(runner_wrapper, "os", _Reports(si_code, si_status))
    child = SimpleNamespace(pid=4242)
    assert runner_wrapper._observe_exit(child) == expected  # type: ignore[arg-type]


class _NoWaitid:
    """The `os` module as seen on a host without `os.waitid`."""

    def __getattr__(self, name: str) -> Any:
        if name == "waitid":
            raise AttributeError(name)
        return getattr(os, name)


def test_wrapper_observe_exit_falls_back_to_waitpid_without_waitid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where `waitid` is missing the wrapper reaps on observe and keeps
    Popen's returncode in step, so the later `wait()` returns at once."""
    monkeypatch.setattr(runner_wrapper, "os", _NoWaitid())

    exiting = _sh("exit 3")
    assert wait_for(lambda: runner_wrapper._observe_exit(exiting)) == {
        "outcome": "exited",
        "exit_code": 3,
    }
    assert exiting.returncode == 3
    assert exiting.wait() == 3
    assert runner_wrapper._observe_exit(exiting) is None  # reaped: ChildProcessError

    killed = _sh("kill -9 $$")
    assert wait_for(lambda: runner_wrapper._observe_exit(killed)) == {
        "outcome": "signaled",
        "signal": signal.SIGKILL,
    }
    assert killed.returncode == -signal.SIGKILL

    running = _sh("sleep 30")
    try:
        assert runner_wrapper._observe_exit(running) is None  # waitpid pid == 0
    finally:
        os.killpg(running.pid, signal.SIGKILL)
        running.wait()


# ---------------------------------------------------- _await_exit_after_kill


@pytest.fixture
def chld_pipe():
    """A nonblocking self-pipe fed by SIGCHLD, as `main()` builds it."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.set_blocking(w, False)

    def on_chld(_signum: int, _frame: object) -> None:
        try:
            os.write(w, b"x")
        except OSError:
            pass

    previous = signal.signal(signal.SIGCHLD, on_chld)
    try:
        yield r
    finally:
        signal.signal(signal.SIGCHLD, previous)
        os.close(r)
        os.close(w)


def _command_that(ignores_term: bool) -> subprocess.Popen[bytes]:
    trap = "trap '' TERM; " if ignores_term else ""
    child = subprocess.Popen(
        ["/bin/sh", "-c", f"{trap}echo ready; while :; do sleep 1; done"],
        process_group=0,
        stdout=subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stdout.readline() == b"ready\n"  # the trap is in place
    return child


def test_wrapper_graceful_command_dies_on_sigterm_without_escalation(chld_pipe: int) -> None:
    child = _command_that(ignores_term=False)
    observed = runner_wrapper._await_exit_after_kill(child, chld_pipe, 30.0)
    runner_wrapper._reap(child)
    assert observed == {"outcome": "signaled", "signal": signal.SIGTERM}
    assert child.stdout is not None
    child.stdout.close()


def test_wrapper_term_ignoring_command_is_killed_when_the_grace_runs_out(
    chld_pipe: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that ignores SIGTERM outlives the grace and is SIGKILLed.
    The first look after the kill is made to miss (the exit is not yet
    visible), so the wait loop's retry runs too."""
    child = _command_that(ignores_term=True)
    sent: list[int] = []
    real_killpg = runner_wrapper.killpg_quiet
    real_observe = runner_wrapper._observe_exit
    misses = []

    def killpg(pgid: int, sig: int) -> None:
        sent.append(sig)
        real_killpg(pgid, sig)

    def observe(c: Any) -> Any:
        if signal.SIGKILL in sent and not misses:
            misses.append(1)
            return None
        return real_observe(c)

    monkeypatch.setattr(runner_wrapper, "killpg_quiet", killpg)
    monkeypatch.setattr(runner_wrapper, "_observe_exit", observe)
    observed = runner_wrapper._await_exit_after_kill(child, chld_pipe, 0.2)
    runner_wrapper._reap(child)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert misses == [1]
    assert observed == {"outcome": "signaled", "signal": signal.SIGKILL}
    assert child.stdout is not None
    child.stdout.close()


# ------------------------------------------------------------ main(), by file


def test_wrapper_already_session_leader_still_runs_and_records(tmp_path: Path) -> None:
    """Started as a session leader (its spawner called setsid), the wrapper
    skips its own `setsid` and everything else is unchanged."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    lifeline_r, lifeline_w = os.pipe()
    spec = {
        "version": 1,
        "run_id": "00000000-0000-4000-8000-000000000001",
        "job": "j1",
        "run_number": 1,
        "command": "exit 6",
        "run_dir": str(run_dir),
        "lifeline_fd": lifeline_r,
        "stdout_path": str(run_dir / "out.log"),
        "stderr_path": str(run_dir / "err.log"),
        "stdin_path": None,
        "grace_seconds": 2.0,
    }
    proc = subprocess.Popen(
        [sys.executable, str(Path(runner_wrapper.__file__))],
        stdin=subprocess.PIPE,
        pass_fds=(lifeline_r,),
        start_new_session=True,
    )
    os.close(lifeline_r)
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(spec).encode())
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0
    os.close(lifeline_w)
    spawn = read_json(run_dir / "spawn.json")
    status = read_json(run_dir / "status.json")
    assert spawn["wrapper_pid"] == proc.pid  # the leader the spawner made
    assert spawn["command_pgid"] == spawn["command_pid"] != proc.pid
    assert status["outcome"] == "exited" and status["exit_code"] == 6


def test_wrapper_records_a_spawn_it_could_not_open(tmp_path: Path) -> None:
    """O, wrapper-protocol §3: an unopenable log leaves spawn_failed and exits 0."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    lifeline_r, lifeline_w = os.pipe()
    spec = json.loads(_spec(run_dir, "true", lifeline_r))
    spec["stdout_path"] = str(run_dir)
    out = subprocess.run(
        [sys.executable, str(Path(runner_wrapper.__file__))],
        input=json.dumps(spec).encode(),
        capture_output=True,
        pass_fds=(lifeline_r,),
        check=False,
        timeout=30,
    )
    os.close(lifeline_r)
    os.close(lifeline_w)
    assert out.returncode == 0, out.stderr
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "spawn_failed"
    assert "directory" in status["error"].lower()
    assert not (run_dir / "spawn.json").exists()


def _spec(run_dir: Path, command: str, lifeline_fd: int) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "run_id": "00000000-0000-4000-8000-000000000001",
            "job": "j1",
            "run_number": 1,
            "command": command,
            "run_dir": str(run_dir),
            "lifeline_fd": lifeline_fd,
            "stdout_path": str(run_dir / "out.log"),
            "stderr_path": str(run_dir / "err.log"),
            "stdin_path": None,
            "grace_seconds": 2.0,
        }
    ).encode()


def _drive(
    mode: str, events: Path, spec: bytes, lifeline_r: int, *, pause: str | None = None
) -> subprocess.Popen[bytes]:
    env = None if pause is None else {**os.environ, runner_wrapper.PAUSE_ENV: pause}
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), mode, str(events)],
        stdin=subprocess.PIPE,
        pass_fds=(lifeline_r,),
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(spec)
    proc.stdin.close()
    return proc


def _event_count(events: Path) -> int:
    return events.read_bytes().count(b"\n") if events.exists() else 0


_ON_DARWIN = pytest.mark.skipif(
    not sys.platform.startswith("darwin"),
    reason="the process start reading runs `ps` on macOS and reads /proc on Linux, where the "
    "failure is not made by an unrunnable `ps`",
)


def _ps_stat(pid: int) -> str:
    """The `ps` state of `pid`, empty when it is gone; bounded."""
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return out.stdout.strip()


def _process_table() -> list[int]:
    """Every pid, from `ps`. A table that cannot be read is a failure with its reason, never
    an empty table: `ps` that cannot run, times out, exits non-zero or lists nobody (the
    reader is itself a process) raises."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"the process census failed: {exc!r}") from exc
    if out.returncode != 0:
        raise AssertionError(
            f"the process census failed: ps exited {out.returncode}: {out.stderr.strip()}"
        )
    pids = [int(token) for token in out.stdout.split()]
    if os.getpid() not in pids:
        raise AssertionError("the process census failed: ps did not list this process")
    return pids


def _session_members(sid: int) -> list[int]:
    """Every process in session `sid` (the wrapper calls `setsid`, so its pid is the id and
    the command stays in it); a failed census raises."""
    members = []
    for pid in _process_table():
        with contextlib.suppress(OSError):
            if os.getsid(pid) == sid:
                members.append(pid)
    return members


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _break_ps(tmp_path: Path, kind: str) -> str:
    """A PATH on which `ps` cannot run: an empty directory, or one holding a `ps` file that
    is not executable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if kind == "unexecutable":
        (bin_dir / "ps").write_text("#!/bin/sh\n")
        (bin_dir / "ps").chmod(0o644)
    return str(bin_dir)


class _SpawnRecordFailure:
    """The real wrapper with a spawn record it cannot make, and the test's ownership of
    every process it leaves, whatever fails: `close` ends the wrapper's whole session."""

    def __init__(
        self,
        tmp_path: Path,
        command: str,
        *,
        ps_kind: str | None,
        spawn_json_is_a_directory: bool = False,
        status_json_is_a_directory: bool = False,
        pause: bool = False,
        stderr_closed: bool = False,
        driver_mode: str | None = None,
    ) -> None:
        self.run_dir = tmp_path / "j1.1"
        self.run_dir.mkdir()
        if spawn_json_is_a_directory:
            (self.run_dir / "spawn.json").mkdir()
        if status_json_is_a_directory:
            (self.run_dir / "status.json").mkdir()
        self.pid_file = tmp_path / "command.pid"
        self.fifo = tmp_path / "block.fifo"
        os.mkfifo(self.fifo)
        env = dict(os.environ)
        if ps_kind is not None:
            env["PATH"] = _break_ps(tmp_path, ps_kind)
        if pause:
            env[runner_wrapper.PAUSE_ENV] = "post_spawn_pre_record"
        lifeline_r, self.lifeline_w = os.pipe()
        spec = json.loads(_spec(self.run_dir, command, lifeline_r))
        spec["shell"] = "/bin/sh"
        if stderr_closed:
            reader, stderr = os.pipe()
            os.close(reader)  # the wrapper's stderr is a pipe nobody reads
        else:
            stderr = subprocess.PIPE
        argv = [sys.executable, str(Path(runner_wrapper.__file__))]
        if driver_mode is not None:  # the same wrapper, with one seam made certain
            argv = [sys.executable, str(DRIVER), driver_mode, str(tmp_path / "events")]
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stderr=stderr,
            pass_fds=(lifeline_r,),
            env=env,
        )
        os.close(lifeline_r)
        if stderr_closed:
            os.close(stderr)
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(spec).encode())
        self.proc.stdin.close()

    def command_pid(self, timeout_s: float = 10.0) -> int:
        return int(
            wait_for(
                lambda: self.pid_file.exists() and self.pid_file.read_text().strip(),
                timeout_s=timeout_s,
            )
        )

    def at_the_pause(self) -> None:
        wait_for(lambda: _ps_stat(self.proc.pid).startswith("T"))

    def finish(self) -> int:
        """Wait, bounded, for the wrapper to end; the command must be gone with it. This
        runs before any cleanup, so it proves the wrapper ended the command. A census that
        fails fails the assertion with its reason."""
        code = self.proc.wait(timeout=30)
        assert _session_members(self.proc.pid) == [], "the wrapper left a process behind"
        return code

    def close(self) -> None:
        """End everything the run left, whatever the test reached, then check that nothing
        is left. The wrapper dies first (SIGKILL works on a stopped one), so nothing more is
        spawned while the census is taken. A wrapper killed before `setsid` has spawned
        nothing, because `main` calls `setsid` before it starts the command, so every
        process it ever made is in the session whose id is its pid. The census is the
        session when `ps` can be read, and the command's process group from the pid file
        when that file exists and its process is still in that session. Every signal that
        can be sent is sent before a failed census is raised."""
        failures: list[str] = []
        with contextlib.suppress(OSError):
            self.proc.kill()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            failures.append("the wrapper did not end after SIGKILL")
        sid = self.proc.pid
        command_pid = None
        with contextlib.suppress(OSError, ValueError):
            command_pid = int(self.pid_file.read_text().strip())
        if command_pid is not None:
            with contextlib.suppress(OSError):  # gone, or not ours: nothing to end
                if os.getsid(command_pid) == sid:
                    os.killpg(command_pid, signal.SIGKILL)
        try:
            for pid in _session_members(sid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while _session_members(sid):
                if time.monotonic() >= deadline:
                    failures.append("a process of the wrapper's session is still alive")
                    break
                time.sleep(0.05)
        except AssertionError as exc:  # a failed census, after the signals that needed none
            failures.append(str(exc))
        if command_pid is not None:
            deadline = time.monotonic() + 10
            while _alive(command_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _alive(command_pid):
                failures.append(f"the command {command_pid} is still alive")
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        os.close(self.lifeline_w)
        assert not failures, "; ".join(failures)


def test_wrapper_test_teardown_ends_a_wrapper_and_command_left_by_a_failed_wait(
    tmp_path: Path,
) -> None:
    """The helper owns its processes whatever fails: the readiness wait fails before the pid
    file is read, with the wrapper stopped at its pause and its command alive, and the
    teardown still ends both."""
    run = _SpawnRecordFailure(
        tmp_path,
        f'echo $$ > "{tmp_path / "command.pid"}"; read x < "{tmp_path / "block.fifo"}"',
        ps_kind=None,
        pause=True,
    )
    sid = run.proc.pid
    try:
        run.at_the_pause()  # the command has been started
        with pytest.raises(AssertionError, match="timed out"):
            run.command_pid(timeout_s=0)
    finally:
        run.close()
    assert run.proc.poll() is not None
    assert _session_members(sid) == []
    if run.pid_file.exists():  # the command was fast enough to write it
        assert not _alive(int(run.pid_file.read_text().strip()))


@_ON_DARWIN
@pytest.mark.parametrize("ps_kind", ["unexecutable", "missing"])
def test_wrapper_kills_a_command_whose_spawn_record_cannot_be_built(
    tmp_path: Path, ps_kind: str
) -> None:
    """DL-502, BUG-63: on macOS the start token is read with `ps`. When `ps` cannot run, the
    record cannot be built after the command has started. The wrapper ends what it started,
    reaps it, records `terminated` with the spawn-record cause, says so on stderr, exits 3
    and leaves no `spawn.json`. The command needs nothing from PATH."""
    run = _SpawnRecordFailure(
        tmp_path,
        f'echo $$ > "{tmp_path / "command.pid"}"; read x < "{tmp_path / "block.fifo"}"',
        ps_kind=ps_kind,
        pause=True,
    )
    try:
        run.command_pid()
        run.at_the_pause()
        os.kill(run.proc.pid, signal.SIGCONT)
        assert run.finish() == 3
        assert not (run.run_dir / "spawn.json").exists()
        status = read_json(run.run_dir / "status.json")
        assert status["outcome"] == "terminated"
        assert status["cause"].startswith(SPAWN_RECORD_FAILED)
        assert status["observed"] == {"outcome": "signaled", "signal": signal.SIGTERM}
        assert run.proc.stderr is not None
        assert b"spawn record failed" in run.proc.stderr.read()
    finally:
        run.close()


@_ON_DARWIN
def test_wrapper_records_the_exit_of_a_command_that_ended_before_a_spawn_record_failed(
    tmp_path: Path,
) -> None:
    """DL-502: the command has exited, so its group holds only its zombie leader, and macOS
    answers EPERM to a signal for that group, whose proof of absence runs the `ps` that cannot
    run. The wrapper still records `terminated` with the command's own exit, and exits 3."""
    run = _SpawnRecordFailure(
        tmp_path, f'echo $$ > "{tmp_path / "command.pid"}"', ps_kind="unexecutable", pause=True
    )
    try:
        command_pid = run.command_pid()
        run.at_the_pause()
        wait_for(lambda: _ps_stat(command_pid).startswith("Z"))  # exited, not reaped
        os.kill(run.proc.pid, signal.SIGCONT)
        assert run.finish() == 3
        status = read_json(run.run_dir / "status.json")
        assert status["outcome"] == "terminated"
        assert status["cause"].startswith(SPAWN_RECORD_FAILED)
        assert status["observed"] == {"outcome": "exited", "exit_code": 0}
        assert not (run.run_dir / "spawn.json").exists()
    finally:
        run.close()


@pytest.mark.parametrize(
    ("mode", "command", "observed"),
    [
        pytest.param(
            "group_kill_fails_leader_exited",
            "exit 7",
            {"outcome": "exited", "exit_code": 7},
            id="leader-exited",
        ),
        pytest.param(
            "group_kill_fails_leader_alive",
            'read x < "{fifo}"',
            {"outcome": "signaled", "signal": signal.SIGKILL},
            id="leader-alive",
        ),
        pytest.param(
            "group_kill_fails_leader_signal_fails",
            'read x < "{fifo}"',
            {"outcome": "signaled", "signal": signal.SIGKILL},
            id="leader-alive-signal-fails",
        ),
    ],
)
def test_wrapper_ends_the_leader_alone_when_the_group_kill_raises(
    tmp_path: Path, mode: str, command: str, observed: dict[str, object]
) -> None:
    """DL-502, on both hosts through a stand-in for the group kill: where it raises (macOS
    answers EPERM for a group that holds only the exited leader, and its proof of absence
    needs a `ps` that cannot run), the wrapper takes the exit of a leader that has gone, or
    ends a live one with SIGKILL alone and ignores a failure of that signal, then records
    `terminated` with the spawn-record cause and exits 3. The record cannot be written
    because a directory stands in the place of `spawn.json`."""
    run = _SpawnRecordFailure(
        tmp_path,
        command.format(fifo=tmp_path / "block.fifo"),
        ps_kind=None,
        spawn_json_is_a_directory=True,
        driver_mode=mode,
    )
    try:
        assert run.finish() == 3
        status = read_json(run.run_dir / "status.json")
        assert status["outcome"] == "terminated"
        assert status["cause"].startswith(SPAWN_RECORD_FAILED)
        assert status["observed"] == observed
    finally:
        run.close()


def test_wrapper_ends_its_command_and_exits_3_when_neither_record_can_be_written(
    tmp_path: Path,
) -> None:
    """DL-502, wrapper-protocol §3: the spawn record cannot be written and neither can the
    best-effort `status.json` (a directory stands in the place of each). The command is
    ended and reaped all the same, nothing is recorded, and the wrapper exits 3."""
    run = _SpawnRecordFailure(
        tmp_path,
        f'read x < "{tmp_path / "block.fifo"}"',
        ps_kind=None,
        spawn_json_is_a_directory=True,
        status_json_is_a_directory=True,
    )
    try:
        assert run.finish() == 3
        assert not (run.run_dir / "status.json").is_file()
        assert not (run.run_dir / "spawn.json").is_file()
    finally:
        run.close()


@pytest.mark.parametrize(
    "how",
    [
        pytest.param("spawn_json_is_a_directory", id="write-fails"),
        pytest.param("ps_unexecutable", marks=_ON_DARWIN, id="build-fails"),
    ],
)
def test_wrapper_ends_its_command_when_the_diagnostic_cannot_be_written(
    tmp_path: Path, how: str
) -> None:
    """DL-502: the wrapper's stderr is a pipe with no reader, so the line it prints about the
    failed spawn record raises. The command is already ended and reaped and `status.json` is
    already tried, and the wrapper exits 3, not by a traceback. The failure is a `spawn.json`
    that cannot be replaced (a directory is in its place), which runs on both platforms, and
    on macOS also a `ps` that cannot run."""
    run = _SpawnRecordFailure(
        tmp_path,
        f'echo $$ > "{tmp_path / "command.pid"}"; read x < "{tmp_path / "block.fifo"}"',
        ps_kind="unexecutable" if how == "ps_unexecutable" else None,
        spawn_json_is_a_directory=how == "spawn_json_is_a_directory",
        stderr_closed=True,
    )
    try:
        assert run.finish() == 3
        status = read_json(run.run_dir / "status.json")
        assert status["outcome"] == "terminated"
        assert status["cause"].startswith(SPAWN_RECORD_FAILED)
        assert not (run.run_dir / "spawn.json").is_file()
    finally:
        run.close()


@pytest.mark.parametrize(
    "mode", ["eperm_term_zombie", "eperm_term_empty", "eperm_kill_zombie", "eperm_kill_empty"]
)
def test_wrapper_records_the_real_exit_after_darwin_killpg_eperm(tmp_path: Path, mode: str) -> None:
    """DL-229: absence after EPERM does not replace the child's observed exit status."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    go = Path(str(events) + ".go")
    ready = tmp_path / "ready"
    lifeline_r, lifeline_w = os.pipe()
    command = f"trap '' TERM; touch '{ready}'; while [ ! -f '{go}' ]; do sleep 0.01; done; exit 7"
    spec = json.loads(_spec(run_dir, command, lifeline_r))
    spec["grace_seconds"] = 0.0
    proc = _drive(mode, events, json.dumps(spec).encode(), lifeline_r)
    os.close(lifeline_r)
    try:
        wait_for(lambda: (run_dir / "spawn.json").exists())
        wait_for(ready.exists)
        os.close(lifeline_w)
        lifeline_w = -1
        assert proc.wait(timeout=30) == 0
        status = read_json(run_dir / "status.json")
        assert status["outcome"] == "terminated"
        assert status["cause"] == "parent lost"
        assert status["observed"] == {"outcome": "exited", "exit_code": 7}
        assert _event_count(events) == 2
    finally:
        if lifeline_w >= 0:
            os.close(lifeline_w)
        _cleanup(proc, run_dir, go)


def _cleanup(proc: subprocess.Popen[bytes], run_dir: Path, go: Path) -> None:
    """Whatever the test did, release the command, kill its process group and
    stop the driver, so a failing test leaves nothing behind. The group is
    killed only while the driver (the wrapper) lives: until it reaps the
    command, the group number is still the command's and cannot be reused."""
    go.write_text("")
    spawn = run_dir / "spawn.json"
    if proc.poll() is None and spawn.exists():
        with contextlib.suppress(Exception):
            os.killpg(read_json(spawn)["command_pgid"], signal.SIGKILL)
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def test_wrapper_survives_a_failing_sigchld_wakeup_write(tmp_path: Path) -> None:
    """If the self-pipe cannot take the SIGCHLD wake-up byte, the handler
    swallows the error: the wrapper still sees the exit on the next wake-up
    (here a byte on the open lifeline) and records it as the command's own exit."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    go = tmp_path / "go"
    lifeline_r, lifeline_w = os.pipe()
    command = f"while [ ! -e {go} ]; do sleep 0.05; done; exit 4"
    proc = _drive("full_self_pipe", events, _spec(run_dir, command, lifeline_r), lifeline_r)
    os.close(lifeline_r)
    try:
        wait_for(lambda: (run_dir / "spawn.json").exists())
        before = _event_count(events)  # the wrapper's own probes may have raised SIGCHLD
        command_pid = read_json(run_dir / "spawn.json")["command_pid"]
        go.write_text("")
        wait_for(lambda: runner_procid.proc_is_zombie(command_pid))
        wait_for(lambda: _event_count(events) > before)  # the exit's SIGCHLD write was refused
        os.write(lifeline_w, b"x")  # a wakeup that is not EOF: the lifeline stays open
        assert proc.wait(timeout=30) == 0
    finally:
        if lifeline_w >= 0:
            os.close(lifeline_w)
        _cleanup(proc, run_dir, go)
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "exited" and status["exit_code"] == 4
    assert "cause" not in status


def test_wrapper_keeps_waiting_through_a_wakeup_that_is_neither_an_exit_nor_eof(
    tmp_path: Path,
) -> None:
    """A SIGCHLD with the command still running wakes the loop through the
    self-pipe. The wrapper looks, sees no exit and no lifeline EOF, and goes
    back to waiting: nothing is recorded until the command really exits. The
    signal is sent by the test, so the wake-up does not depend on the host
    (the wrapper's own `ps` probes raise one on macOS only)."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    go = tmp_path / "go"
    lifeline_r, lifeline_w = os.pipe()
    command = f"while [ ! -e {go} ]; do sleep 0.05; done; exit 4"
    proc = _drive("count_observes", events, _spec(run_dir, command, lifeline_r), lifeline_r)
    os.close(lifeline_r)
    try:
        wait_for(lambda: (run_dir / "spawn.json").exists())
        before = _event_count(events)
        os.kill(proc.pid, signal.SIGCHLD)
        wait_for(lambda: _event_count(events) > before)  # woke, looked, found nothing
        assert not (run_dir / "status.json").exists() and proc.poll() is None
        go.write_text("")
        assert proc.wait(timeout=30) == 0
    finally:
        os.close(lifeline_w)
        _cleanup(proc, run_dir, go)
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "exited" and status["exit_code"] == 4
    assert "cause" not in status


def test_wrapper_records_the_exit_of_a_command_that_was_stopped_and_continued(
    tmp_path: Path,
) -> None:
    """A command stopped by SIGSTOP is still running: the wrapper looks while
    it is stopped, records nothing, and after SIGCONT records the command's
    own exit. On macOS the look used to record `signaled 17` (SIGSTOP) and
    lose the real exit. The test wakes the wrapper itself once the command
    is stopped and waits for that look to finish, so the look is certain."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    go = tmp_path / "go"
    lifeline_r, lifeline_w = os.pipe()
    command = f"while [ ! -e {go} ]; do sleep 0.05; done; kill -STOP $$; exit 5"
    proc = _drive("count_observes", events, _spec(run_dir, command, lifeline_r), lifeline_r)
    os.close(lifeline_r)
    try:
        wait_for(lambda: (run_dir / "spawn.json").exists())
        pgid = read_json(run_dir / "spawn.json")["command_pgid"]
        go.write_text("")
        wait_for(lambda: _stopped(pgid))
        before = _event_count(events)
        os.kill(proc.pid, signal.SIGCHLD)
        wait_for(lambda: _event_count(events) > before)  # looked while the command was stopped
        assert not (run_dir / "status.json").exists() and proc.poll() is None
        os.killpg(pgid, signal.SIGCONT)
        assert proc.wait(timeout=30) == 0
    finally:
        os.close(lifeline_w)
        _cleanup(proc, run_dir, go)
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "exited" and status["exit_code"] == 5
    assert "cause" not in status


def test_wrapper_takes_an_exit_the_first_look_missed_into_parent_lost(
    tmp_path: Path,
) -> None:
    """The lifeline is already at EOF and the command has exited, but the
    first look called it still running: the wrapper records `parent lost`, and
    the exit that the second look finds goes inside `observed` (DL-503)."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    lifeline_r, lifeline_w = os.pipe()
    os.close(lifeline_w)
    proc = _drive(
        "completion_beats_parent_loss", events, _spec(run_dir, "exit 4", lifeline_r), lifeline_r
    )
    os.close(lifeline_r)
    try:
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
    assert status["observed"] == {"outcome": "exited", "exit_code": 4}
    assert events.read_bytes().count(b"\n") == 2  # two looks


@pytest.mark.skipif(
    not hasattr(os, "waitid"),
    reason="the driver mode waits for the command's exit with os.waitid, which macOS "
    "has only from Python 3.13",
)
def test_wrapper_reads_the_lifeline_when_only_the_self_pipe_woke_the_select(
    tmp_path: Path,
) -> None:
    """The command's exit wakes the select through the self-pipe alone, and
    the lifeline is already at EOF: the wrapper reads the lifeline all the
    same, so the exit it finds goes inside `parent lost` (DL-503). Without the lifeline read the record would be `exited`."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    lifeline_r, lifeline_w = os.pipe()
    os.close(lifeline_w)
    proc = _drive("self_pipe_wakeup_only", events, _spec(run_dir, "exit 4", lifeline_r), lifeline_r)
    os.close(lifeline_r)
    try:
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
    assert status["observed"] == {"outcome": "exited", "exit_code": 4}
    assert _event_count(events) == 1  # the one wakeup


@pytest.mark.parametrize(
    ("command", "term_it", "observed"),
    [
        ("exit 0", False, {"outcome": "exited", "exit_code": 0}),
        ("exec sleep 600", True, {"outcome": "signaled", "signal": signal.SIGTERM}),
    ],
    ids=["exited-0", "ended-by-term"],
)
def test_wrapper_records_parent_lost_for_an_exit_that_precedes_its_look(
    tmp_path: Path, command: str, term_it: bool, observed: dict[str, int | str]
) -> None:
    """DL-503: the wrapper is held after its spawn record, the command ends (by
    itself, or by the TERM the unit sends), the lifeline's write end closes, and
    the wrapper continues. Both descriptors are ready at its first wakeup; it sees
    the exit and then the lost lifeline (DL-544), so the record is `parent lost`
    and the exit is inside `observed`, whatever its kind."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(run_dir, command, pause="post_record")
    try:
        _at_pause(proc)
        spawn = read_json(run_dir / "spawn.json")
        if term_it:
            os.killpg(spawn["command_pgid"], signal.SIGTERM)
        wait_for(lambda: runner_procid.proc_is_zombie(spawn["command_pid"]))
        os.close(lifeline_w)
        lifeline_w = -1
        assert _continue_and_wait(proc) == 0
    finally:
        if lifeline_w >= 0:
            os.close(lifeline_w)
        if proc.poll() is None:  # a failed test: the wrapper is held, its command may live
            with contextlib.suppress(Exception):
                os.killpg(read_json(run_dir / "spawn.json")["command_pgid"], signal.SIGKILL)
            proc.kill()
            proc.wait()
    status = read_json(run_dir / "status.json")
    assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
    assert status["observed"] == observed


def _next_stop(proc: subprocess.Popen[bytes]) -> None:
    """Wait for the wrapper's next self-stop. Its parent hears each stop once
    (WUNTRACED), so a pause point that follows another is never taken for the one
    before it. A wrapper that exits instead fails here."""

    def stopped() -> bool:
        pid, status = os.waitpid(proc.pid, os.WUNTRACED | os.WNOHANG)
        if pid == 0:
            return False
        assert os.WIFSTOPPED(status), f"the wrapper ended, status {status}"
        return True

    wait_for(stopped)


def _release_held(proc: subprocess.Popen[bytes], run_dir: Path, lifeline_w: int) -> None:
    """A failed test's teardown: close the lifeline, then end the command's group and
    the wrapper, whatever pause holds it."""
    if lifeline_w >= 0:
        os.close(lifeline_w)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            os.killpg(read_json(run_dir / "spawn.json")["command_pgid"], signal.SIGKILL)
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("engine_dies", ["after_the_record", "before_the_lifeline_read"])
def test_wrapper_records_an_exit_it_saw_by_the_lifeline_at_the_next_read(
    tmp_path: Path, engine_dies: str
) -> None:
    """DL-544: the command exits by itself while the wrapper is held after its spawn
    record, so the wrapper's first look sees the exit; it is then held between that
    look and the lifeline read. With the lifeline open at the read, the record is the
    command's own `exited`, and an engine death after the record leaves it as it is.
    With the lifeline lost at the read, the record is `parent lost` with the exit
    inside `observed`: the stated limit, an own exit that the engine's death follows
    before the read."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    proc, lifeline_w = spawn_wrapper(
        run_dir, "exit 7", pause="post_record,post_observe_pre_lifeline,post_status_pre_reap"
    )
    try:
        _next_stop(proc)  # post_record
        spawn = read_json(run_dir / "spawn.json")
        wait_for(lambda: runner_procid.proc_is_zombie(spawn["command_pid"]))
        os.kill(proc.pid, signal.SIGCONT)
        _next_stop(proc)  # post_observe_pre_lifeline, the exit seen
        assert not (run_dir / "status.json").exists()
        if engine_dies == "before_the_lifeline_read":
            os.close(lifeline_w)
            lifeline_w = -1
        os.kill(proc.pid, signal.SIGCONT)
        _next_stop(proc)  # post_status_pre_reap
        written = (run_dir / "status.json").read_bytes()
        if lifeline_w >= 0:
            os.close(lifeline_w)
            lifeline_w = -1
        assert _continue_and_wait(proc) == 0
    finally:
        _release_held(proc, run_dir, lifeline_w)
    assert (run_dir / "status.json").read_bytes() == written
    status = json.loads(written)
    if engine_dies == "after_the_record":
        assert status["outcome"] == "exited" and status["exit_code"] == 7
        assert "cause" not in status
    else:
        assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
        assert status["observed"] == {"outcome": "exited", "exit_code": 7}


@pytest.mark.parametrize("leader_exits", [True, False], ids=["leader-exits", "leader-lives"])
def test_wrapper_looks_again_before_the_ladder_when_the_lifeline_is_lost(
    tmp_path: Path, leader_exits: bool
) -> None:
    """DL-544: the wrapper's first look sees no exit and it is held before the
    lifeline read; the lifeline then closes. If the leader exited during the hold,
    the second look takes that exit into `parent lost` and the wrapper makes no
    group-kill call, so the child that outlives the leader is never signalled. If
    the leader still lives, the ladder runs: its TERM is a group-kill call, reaches
    the child's trap and ends the leader. The driver counts the wrapper's own calls,
    so the check does not wait on the child."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    ready, termed, go = tmp_path / "ready", tmp_path / "termed", tmp_path / "go"
    command = (
        f'/bin/sh -c \'trap "touch {termed}; exit 0" TERM; echo $$ > {ready}; '
        "while :; do sleep 0.05 & wait $!; done' &\n"
        f"while [ ! -e {go} ]; do sleep 0.05; done\n"
        "exit 3\n"
    )
    lifeline_r, lifeline_w = os.pipe()
    proc = _drive(
        "count_group_kills",
        events,
        _spec(run_dir, command, lifeline_r),
        lifeline_r,
        pause="post_observe_pre_lifeline",
    )
    os.close(lifeline_r)
    child = None
    try:
        wait_for(lambda: (run_dir / "spawn.json").exists() and ready.exists())
        child = int(wait_for(lambda: ready.read_text().strip()))
        spawn = read_json(run_dir / "spawn.json")
        os.kill(proc.pid, signal.SIGCHLD)  # a wakeup on every host
        _next_stop(proc)  # post_observe_pre_lifeline, the leader running
        if leader_exits:
            go.write_text("")
            wait_for(lambda: runner_procid.proc_is_zombie(spawn["command_pid"]))
        os.close(lifeline_w)
        lifeline_w = -1
        assert _continue_and_wait(proc) == 0
        status = read_json(run_dir / "status.json")
        assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
        if leader_exits:
            assert status["observed"] == {"outcome": "exited", "exit_code": 3}
            assert _event_count(events) == 0
        else:
            assert status["observed"] == {"outcome": "signaled", "signal": signal.SIGTERM}
            assert _event_count(events) >= 1
            wait_for(termed.exists)
    finally:
        _release_held(proc, run_dir, lifeline_w)
        if child is not None and not termed.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)  # the child that outlived its leader


def test_wrapper_wakes_for_an_exit_whose_python_handler_has_not_run(tmp_path: Path) -> None:
    """The SIGCHLD lands after the Python handler's last chance and before
    the select syscall starts, so the handler writes nothing in time. The
    wrapper still wakes, because the interpreter writes the signal number into
    the self-pipe at delivery (its wakeup fd). The driver holds that window
    open: the signal reaches a helper thread, and the main thread is already
    in select. The lifeline stays open throughout, so nothing else can wake it."""
    run_dir = tmp_path / "j1.1"
    run_dir.mkdir()
    events = tmp_path / "events"
    lifeline_r, lifeline_w = os.pipe()
    proc = _drive(
        "exit_lands_in_select", events, _spec(run_dir, "exec sleep 600", lifeline_r), lifeline_r
    )
    os.close(lifeline_r)
    try:
        wait_for(lambda: _event_count(events) > 0)  # in select, nothing to read
        os.killpg(read_json(run_dir / "spawn.json")["command_pgid"], signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        spawn = run_dir / "spawn.json"
        if proc.poll() is None and spawn.exists():  # the wrapper still holds the group
            with contextlib.suppress(Exception):
                os.killpg(read_json(spawn)["command_pgid"], signal.SIGKILL)
        os.close(lifeline_w)  # a wrapper that slept through the exit wakes now
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    status = read_json(run_dir / "status.json")
    assert status == {**status, "outcome": "signaled", "signal": signal.SIGTERM}
    assert "cause" not in status
