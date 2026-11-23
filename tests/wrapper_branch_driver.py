"""Wrapper subprocess for the branch tests that need a race made certain
(tests/test_wrapper_branches.py). Reads the wrapper spec on stdin, patches
one seam of `dsl41.runner_wrapper`, then runs its `main()`. Unlike the
engine's launch it imports the module as a package module, not by file path;
the races it makes do not depend on that.

Modes (argv[1]); argv[2] is a file this process appends one line to per probe
event, so a test can wait on the event instead of sleeping:
- full_self_pipe: the SIGCHLD self-pipe is already full, so the handler's
  write fails with EAGAIN. One line per refused write.
- count_observes: nothing is patched but the exit probe, which runs the real
  one and then notes the call, so a test can wait for a look it caused to
  finish.
- count_group_kills: nothing is patched but the group kill, which notes the
  call and then runs the real one, so a test reads whether the wrapper itself
  signalled the group (DL-544). One line per call.
- completion_beats_parent_loss: the first exit probe blocks until the command
  has exited and reports "still running", so the lifeline EOF is read with the
  exit already there. One line per probe.
- self_pipe_wakeup_only: the loop's select reports the self-pipe alone, once the
  command has exited, whatever the lifeline holds (DL-503). One line per such wakeup.
- exit_lands_in_select: SIGCHLD is blocked on the main thread and open on a
  parked helper thread, so the kernel delivers it there. The C-level handler
  runs at delivery, but the Python handler waits for the main thread, which
  sits in select. That is the window between the handler's last chance and the
  select syscall, held open. One line before each untimed select that starts
  with nothing to read.
- group_kill_fails_leader_exited, group_kill_fails_leader_alive,
  group_kill_fails_leader_signal_fails (DL-502): the group kill raises, as macOS
  answers EPERM to a group whose proof of absence needs a `ps` that cannot run,
  so the host does not matter. The leader has already exited (a zombie, waited
  for here), or it is alive; in the last mode the signal to the leader alone is
  delivered and then raises as well. The first exit probe after that signal
  reports "still running", so the wait after it runs its loop body.

Not a test file: no test_ prefix, imported by nothing.
"""

from __future__ import annotations

import os
import errno
import select
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from types import SimpleNamespace
from typing import Any

from dsl41 import runner_wrapper


def _note(path: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, b"x\n")
    finally:
        os.close(fd)


def full_self_pipe(event_path: str) -> None:
    real_pipe, real_write = os.pipe, os.write
    keep: list[int] = []
    jammed: list[int] = []

    def pipe() -> tuple[int, int]:
        if sys._getframe(1).f_code is not runner_wrapper.main.__code__:
            return real_pipe()  # Popen and the like need a working pipe
        read_end, write_end = real_pipe()
        keep.append(write_end)  # an open writer, so the read end is not at EOF
        sink_r, sink_w = real_pipe()
        keep.append(sink_r)
        os.set_blocking(sink_w, False)
        try:
            while True:
                real_write(sink_w, b"x" * 65536)
        except BlockingIOError:
            pass
        jammed.append(sink_w)
        return read_end, sink_w

    def write(fd: int, data: Any) -> int:
        if jammed and fd == jammed[0]:
            _note(event_path)
        return real_write(fd, data)

    os.pipe = pipe
    os.write = write


def completion_beats_parent_loss(event_path: str) -> None:
    real_observe = runner_wrapper._observe_exit
    saved = None

    def observe(child: Any) -> Any:
        nonlocal saved
        _note(event_path)
        if saved is None:
            while saved is None:
                saved = real_observe(child)
                if saved is None:
                    time.sleep(0.001)
            return None
        return saved

    runner_wrapper._observe_exit = observe


def self_pipe_wakeup_only(event_path: str) -> None:
    real_select = select.select

    def select_(rlist: Any, wlist: Any, xlist: Any, *timeout: float) -> Any:
        if timeout or len(rlist) != 2:
            return real_select(rlist, wlist, xlist, *timeout)
        while True:  # the command is the wrapper's only child left unreaped
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if info is not None and info.si_pid != 0:
                break
            time.sleep(0.01)
        _note(event_path)
        return real_select(rlist[:1], wlist, xlist, 5.0)

    runner_wrapper.select = SimpleNamespace(select=select_)  # type: ignore[assignment]


def count_observes(event_path: str) -> None:
    real_observe = runner_wrapper._observe_exit

    def observe(child: Any) -> Any:
        observed = real_observe(child)
        _note(event_path)
        return observed

    runner_wrapper._observe_exit = observe


def count_group_kills(event_path: str) -> None:
    real_killpg = runner_wrapper.killpg_quiet

    def killpg(pgid: int, sig: int) -> None:
        _note(event_path)
        real_killpg(pgid, sig)

    runner_wrapper.killpg_quiet = killpg


def exit_lands_in_select(event_path: str) -> None:
    parked = threading.Event()

    def park() -> None:
        parked.set()
        threading.Event().wait()

    threading.Thread(target=park, daemon=True).start()  # inherits an open mask
    parked.wait()
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
    real_select = select.select

    def select_(rlist: Any, wlist: Any, xlist: Any, *timeout: float) -> Any:
        if not timeout:
            ready = real_select(rlist, wlist, xlist, 0)
            if ready[0]:
                return ready
            _note(event_path)
        return real_select(rlist, wlist, xlist, *timeout)

    runner_wrapper.select = SimpleNamespace(select=select_)  # type: ignore[assignment]


def killpg_eperm(event_path: str, *, at_kill: bool, empty: bool) -> None:
    """DL-229: the record carries the real exit even when signalling races its exit."""
    procid = sys.modules[runner_wrapper.killpg_quiet.__module__]
    procid.sys = SimpleNamespace(platform="darwin")
    real_killpg, real_run = os.killpg, subprocess.run
    group: list[int] = []
    target = signal.SIGKILL if at_kill else signal.SIGTERM

    def killpg(pgid: int, sig: int) -> None:
        if sig != target:
            real_killpg(pgid, sig)
            return
        with open(event_path + ".go", "w"):
            pass
        deadline = time.monotonic() + 5.0
        while True:
            state = real_run(
                ["ps", "-o", "stat=", "-p", str(pgid)],
                capture_output=True,
                text=True,
                check=True,
                timeout=5.0,
            ).stdout.strip()
            if state.startswith("Z"):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("command did not exit")
            time.sleep(0.01)
        group.append(pgid)
        _note(event_path)
        raise PermissionError(errno.EPERM, "killpg denied")

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv != ["ps", "-A", "-o", "pid=,pgid=,stat="]:
            return real_run(argv, **kwargs)
        assert len(group) == 1
        _note(event_path)
        listing = f"1 1 S\n{os.getpid()} {os.getpgrp()} S\n"
        if not empty:
            listing += f"{group[0]} {group[0]} Z\n"
        return subprocess.CompletedProcess(argv, 0, listing, "")

    procid.os.killpg = killpg
    procid.subprocess.run = run


def group_kill_fails(event_path: str, *, leader_exited: bool, signal_fails: bool) -> None:
    """DL-502: the wrapper's group kill raises where the platform's fallback needs `ps`."""

    def killpg(pgid: int, sig: int) -> None:
        if leader_exited:
            while True:  # the command has exited and is not reaped
                info = os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if info is not None and info.si_pid != 0:
                    break
                time.sleep(0.01)
        _note(event_path)
        raise PermissionError(errno.EPERM, "killpg denied")

    runner_wrapper.killpg_quiet = killpg
    if signal_fails:
        real_kill = os.kill

        def kill(pid: int, sig: int) -> None:
            real_kill(pid, sig)
            raise PermissionError(errno.EPERM, "kill reported a failure")

        os.kill = kill

    if not leader_exited:
        real_observe = runner_wrapper._observe_exit
        probes: list[None] = []

        def observe(child: Any) -> Any:
            probes.append(None)
            if len(probes) == 2:  # the first look after the signal to the leader
                return None
            return real_observe(child)

        runner_wrapper._observe_exit = observe


MODES = {
    "count_observes": count_observes,
    "count_group_kills": count_group_kills,
    "full_self_pipe": full_self_pipe,
    "completion_beats_parent_loss": completion_beats_parent_loss,
    "exit_lands_in_select": exit_lands_in_select,
    "self_pipe_wakeup_only": self_pipe_wakeup_only,
    "group_kill_fails_leader_exited": partial(
        group_kill_fails, leader_exited=True, signal_fails=False
    ),
    "group_kill_fails_leader_alive": partial(
        group_kill_fails, leader_exited=False, signal_fails=False
    ),
    "group_kill_fails_leader_signal_fails": partial(
        group_kill_fails, leader_exited=False, signal_fails=True
    ),
    "eperm_term_zombie": partial(killpg_eperm, at_kill=False, empty=False),
    "eperm_term_empty": partial(killpg_eperm, at_kill=False, empty=True),
    "eperm_kill_zombie": partial(killpg_eperm, at_kill=True, empty=False),
    "eperm_kill_empty": partial(killpg_eperm, at_kill=True, empty=True),
}


if __name__ == "__main__":
    MODES[sys.argv[1]](sys.argv[2])
    sys.exit(runner_wrapper.main())
