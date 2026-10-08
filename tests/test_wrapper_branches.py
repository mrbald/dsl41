"""Branch tests for `dsl41.runner_wrapper` that the lifecycle suite does not
reach (DL-42, DL-229, DL-265).

Normative spec: `docs/supervisor-protocol.md` ss3-ss4 (the wrapper's duties,
its spec refusal and its records). Pure helpers run in this process. The
kill escalation runs real process groups. Two paths of `main()` only exist as
races (a failing SIGCHLD wake-up write; a command that exits as the lifeline
closes), so a driver (tests/wrapper_branch_driver.py) makes each certain and
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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_runner_lifecycle import read_json, wait_for

from dsl41 import runner_procid, runner_wrapper

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


# --------------------------------------------------------------- _drain


def test_wrapper_drain_stops_at_eof_and_at_an_empty_pipe() -> None:
    """A nonblocking self-pipe is emptied whether the writer is still open
    (EAGAIN ends the read) or already closed (EOF ends it)."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    try:
        os.write(w, b"xyz")
        runner_wrapper._drain(r)
        with pytest.raises(BlockingIOError):
            os.read(r, 1)
        os.write(w, b"abc")
        os.close(w)
        w = -1
        runner_wrapper._drain(r)
        assert os.read(r, 1) == b""
    finally:
        os.close(r)
        if w >= 0:
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
        "run_id": "r",
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


def _spec(run_dir: Path, command: str, lifeline_fd: int) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "run_id": "r",
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


def _drive(mode: str, events: Path, spec: bytes, lifeline_r: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), mode, str(events)],
        stdin=subprocess.PIPE,
        pass_fds=(lifeline_r,),
    )
    assert proc.stdin is not None
    proc.stdin.write(spec)
    proc.stdin.close()
    return proc


def _event_count(events: Path) -> int:
    return events.read_bytes().count(b"\n") if events.exists() else 0


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
    (here the lifeline closing) and records it as the command's own exit."""
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
        os.close(lifeline_w)
        lifeline_w = -1
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


def test_wrapper_completion_beats_parent_loss_when_the_exit_lands_with_the_eof(
    tmp_path: Path,
) -> None:
    """The lifeline is already at EOF and the command has exited, but the
    first look called it still running: the second look, after the EOF, finds
    the exit and the run is recorded as exited, not as a lost parent."""
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
    assert status["outcome"] == "exited" and status["exit_code"] == 4
    assert "cause" not in status and "observed" not in status
    assert events.read_bytes().count(b"\n") == 2  # two looks, no kill-wait loop
