"""Per-run wrapper shim: the Tier-0 process-lifecycle recorder.

Spec: docs/runner-design.md ss6a (DL-41a), docs/supervisor-protocol.md (the spool format
this module writes is the tier's frozen public contract, DL-42). STDLIB ONLY: it imports
nothing from dsl41 and nothing third-party. Its dumbness is a correctness property and
the extraction boundary (DL-42; import-graph test in tests/test_runner_lifecycle.py).
Its one non-stdlib import is the sibling stdlib-only ``runner_procid``, which holds the
durability and identity helpers the supervisor shares (DL-72). The engine runs it BY FILE
PATH (``sys.executable <this file>``), never ``-m dsl41.runner_wrapper``, which would
import the dsl41 package __init__ and its third-party imports.

Purpose: Unix gives one wait() observation of a child's exit status, and if the observer
is down when the child dies, init reaps it and the status is gone. The direct parent, the
one process that cannot miss the observation, writes it durably.

Duties, in order (ss6a Tier 0):

1. Own session via setsid() (tolerated if the spawner already made us a leader). The
   command child gets its OWN pgid via ``process_group=0`` at spawn, so kill(-pgid)
   never kills the recorder before it records (DL-41a).
2. Durably write ``spawn.json`` (run_dir): run/job identity, wrapper and command (pid,
   start-time) tokens, command pgid, boot_id, started_at. Every record uses the same
   liturgy: temp file in the same directory, fsync(file), rename, fsync(directory). The
   run dir must be on a local filesystem (rename-over-NFS has ambiguous crash semantics).
3. Spawn ``/bin/sh -c <command>`` with the DSL41_RUN env tag (base64url JSON: run_id,
   job, run_number, boot_id). The tag is FORENSICS ONLY: macOS KERN_PROCARGS2 omits env
   for restricted binaries like /bin/sh and Linux /proc/pid/environ is ptrace-gated
   (DL-41a). Identity verification uses the (pid, start-time) tuple, ``proc_start_token``
   / ``start_tokens_match`` (runner_procid), +/-2s on macOS's 1-second ``ps -o lstart=``,
   tick-exact on Linux.
4. Portable event loop: SIGCHLD self-pipe + select over {self-pipe, lifeline}. The pipe is
   also the interpreter's wakeup fd, so a SIGCHLD delivered just before the select still
   wakes it. On EVERY wakeup check child-exit BEFORE lifeline EOF, so a job completing
   as its parent dies is recorded as a completion, not "parent lost".
5. On child exit: observe with waitid(WNOWAIT), which narrows the observe-to-record hole
   to a few syscalls, durably write ``status.json``, then reap.
6. On lifeline EOF (the parent died, kill -9 included, since the kernel closes fds):
   re-check child exit, then SIGTERM the command pgid, grace, SIGKILL, write
   ``status.json`` outcome=terminated cause="parent lost", exit. Engine death thus
   terminates AND records the jobs with no polling and no Linux-only mechanism.

status.json outcomes (frozen in docs/supervisor-protocol.md):
  exited(exit_code) | signaled(signal) -- how the command itself ended;
  terminated(cause="parent lost")      -- the wrapper killed it on EOF;
  spawn_failed(error)                  -- the command's stdin, stdout or stderr could
                                          not be opened, or /bin/sh could not be spawned
                                          (DL-150).
The engine maps exited -> raw exit_code through SEM-09 oracle-side; signaled/terminated
-> STATUS TERMINATED (a kill that happened); spawn_failed -> STATUS FAILURE. A missing
status.json is the one thing this process never produces: that absence IS the E7
unobservable case.

The wrapper ignores the signals `_RECORDER_IGNORED` lists: SIGTERM, SIGINT, SIGHUP,
SIGQUIT and the two user signals (DL-229). SIGKILL, SIGABRT, any other terminating signal
the wrapper does not ignore, or machine death silences the recorder, and E7 reports the
run. The DL-41a accepted cases (-9 of the wrapper alone, or of the whole tree at once)
are detected at reconciliation.

Test scaffolding: the DSL41_WRAPPER_TEST_PAUSE env var names comma-separated pause points
({pre_spawn, post_spawn_pre_record, post_record, post_wait_pre_status,
post_status_pre_reap}); the wrapper SIGSTOPs itself at each so the kill-matrix tests
(DL-42 item 8) can freeze it at a phase boundary. Absent the variable the hook is inert.
The DL-42 "post-fork pre-exec" boundary is not portably hookable from Python;
post_spawn_pre_record covers it, since both mean "command pid exists, spawn.json does
not".

Wrapper input: a JSON spec on stdin (docs/supervisor-protocol.md). The wrapper is
parent-agnostic: the engine and the supervisor spawn it identically. Its own exit code is
a notification only: 0 = a status record was written; 1 = an unreadable spec or a missing
required key; 2 = the spec refusal (supervisor-protocol ss4 step 6, DL-229 item 8); 3 = a
record write failed, e.g. ENOSPC. status.json is the sole data channel for the outcome.
"""

from __future__ import annotations

import base64
import json
import math
import os
import select
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Literal

# DL-72: the shared process-identity helpers live in a sibling stdlib-only module.
# sys.path[0] is this file's directory under file-path execution, except with
# PYTHONSAFEPATH=1, so the directory is prepended when missing and removed again: this
# file is ALSO imported as a package module (the engine reads __file__ off it), and a
# library must not leave its directory on sys.path, where it would shadow top-level names
# (ir, cli, viz, ...). sys.path ends as CPython handed it, in both modes.
#
# mypy maps the sibling as dsl41.runner_procid and cannot also see it under its top-level
# name, so the TYPE_CHECKING branch names the module mypy knows and keeps the helper calls
# typed in a file that kills processes. The else branch is what runs; the import-graph
# test reads runtime imports only.
_PROCID_DIR = os.path.dirname(os.path.abspath(__file__))
_PROCID_DIR_ADDED = _PROCID_DIR not in sys.path
if _PROCID_DIR_ADDED:
    sys.path.insert(0, _PROCID_DIR)
if TYPE_CHECKING:
    from dsl41.runner_procid import (
        SPOOL_VERSION,
        current_boot_id,
        durable_write_json,
        killpg_quiet,
        proc_start_token,
        utc_now_iso,
    )
else:
    from runner_procid import (  # noqa: E402
        SPOOL_VERSION,
        current_boot_id,
        durable_write_json,
        killpg_quiet,
        proc_start_token,
        utc_now_iso,
    )

if _PROCID_DIR_ADDED:
    sys.path.remove(_PROCID_DIR)

#: how a spawn or a durable record write may fail (DL-151). An embedded NUL in a
#: path or the command makes `os.open`, `subprocess.Popen` and `os.path` raise
#: ValueError, not OSError; either must still leave a `status.json`, because the
#: E7 absence is what this module exists never to produce. The supervisor refuses
#: a NUL at the ss2 gate; this is the recorder's own guard.
_IO_FAILURE = (OSError, ValueError)

#: pause-point env var (test scaffolding, inert in production)
PAUSE_ENV = "DSL41_WRAPPER_TEST_PAUSE"

#: the signals the recorder ignores (supervisor-protocol ss4 duty 2, DL-229):
#: the ones an operator or a tool sends by hand. The command child restores
#: each to its default before exec.
_RECORDER_IGNORED = (
    signal.SIGTERM,
    signal.SIGINT,
    signal.SIGHUP,
    signal.SIGQUIT,
    signal.SIGUSR1,
    signal.SIGUSR2,
)


# ------------------------------------------------------------------- the shim


#: the `status.json` `outcome` vocabulary (docs/supervisor-protocol.md ss3). This process
#: WRITES these strings; `runner_adapters.outcome_from_status` reads the same name, so a
#: fifth outcome cannot reach the engine unannounced (DL-209).
WrapperOutcome = Literal["exited", "signaled", "terminated", "spawn_failed"]


def _test_pause(point: str) -> None:
    named = os.environ.get(PAUSE_ENV, "")
    if point in {p.strip() for p in named.split(",") if p.strip()}:
        os.kill(os.getpid(), signal.SIGSTOP)


def _observe_exit(child: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Observe the child's exit WITHOUT reaping (waitid WNOWAIT + WNOHANG); None while it
    runs, stopped or continued included. Without waitid it falls back to waitpid
    (reap-on-observe), where the observe-to-record hole widens. `_reap` is safe
    afterwards either way: the waitid path leaves a reapable zombie, and the waitpid path
    sets child.returncode."""
    if hasattr(os, "waitid"):
        try:
            info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return None  # already reaped: only possible after we reaped it
        if info is None or info.si_pid == 0:
            return None
        if info.si_code in (os.CLD_STOPPED, os.CLD_CONTINUED, os.CLD_TRAPPED):
            # macOS reports a stopped child here despite WEXITED, with the stop signal in
            # si_status: the command is alive, not signaled
            return None
        if info.si_code == os.CLD_EXITED:
            return {"outcome": "exited", "exit_code": info.si_status}
        return {"outcome": "signaled", "signal": info.si_status}
    try:
        pid, status = os.waitpid(child.pid, os.WNOHANG)
    except ChildProcessError:
        return None
    if pid == 0:
        return None
    child.returncode = os.waitstatus_to_exitcode(status)  # keep Popen sane
    if os.WIFSIGNALED(status):
        return {"outcome": "signaled", "signal": os.WTERMSIG(status)}
    return {"outcome": "exited", "exit_code": os.WEXITSTATUS(status)}


def _reap(child: subprocess.Popen[bytes]) -> None:
    child.wait()  # zombie after a WNOWAIT observation; immediate if reaped


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except BlockingIOError:
        pass


def _restore_default_signals() -> None:
    """Child-side (post-fork pre-exec) reset. SIG_IGN dispositions are inherited ACROSS
    exec (non-interactive sh keeps them for its children), so without this the command
    would ignore the graceful SIGTERM and every kill would escalate to SIGKILL."""
    for sig in _RECORDER_IGNORED:
        signal.signal(sig, signal.SIG_DFL)


def _await_exit_after_kill(
    child: subprocess.Popen[bytes], self_pipe_r: int, grace_s: float
) -> dict[str, Any]:
    """SIGTERM the command pgid, wait up to grace_s (waking on SIGCHLD via the self-pipe),
    then SIGKILL and wait unconditionally (an unkillable process still exits on SIGKILL;
    D-state is the machine's problem)."""
    killpg_quiet(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while True:
        observed = _observe_exit(child)
        if observed is not None:
            return observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([self_pipe_r], [], [], remaining)
        if ready:
            _drain(self_pipe_r)
    killpg_quiet(child.pid, signal.SIGKILL)
    while True:
        observed = _observe_exit(child)
        if observed is not None:
            return observed
        select.select([self_pipe_r], [], [], 0.05)
        _drain(self_pipe_r)


def _is_wire_int(value: object) -> bool:
    """A JSON integer: `true` is not one (DL-151)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _spec_refusal(spec: dict[str, Any]) -> str | None:
    """The ss4 step 6 refusal (exit 2, before anything spawns), or None.

    `version` must be the integer 1 (`true` and `1.0` are refused). A present
    `lifeline_fd` must be an integer and a present `grace_seconds` a finite number of zero
    or more; a boolean is neither, and an integer too large for a float is out of range
    (DL-229). A mistyped value fails before `spawn.json` exists. An absent `lifeline_fd`
    stays the exit-1 missing key of ss4 step 6."""
    version = spec.get("version")
    if not (_is_wire_int(version) and version == SPOOL_VERSION):
        return f"unsupported spec version {version!r}"
    if "lifeline_fd" in spec and not _is_wire_int(spec["lifeline_fd"]):
        return f"lifeline_fd is not an integer: {spec['lifeline_fd']!r}"
    if "grace_seconds" in spec:
        grace = spec["grace_seconds"]
        refusal = f"grace_seconds is not a finite number of zero or more: {grace!r}"
        if not isinstance(grace, int | float) or isinstance(grace, bool):
            return refusal
        try:
            grace_s = float(grace)  # OverflowError: an integer past float range
        except OverflowError:
            return refusal
        if not (math.isfinite(grace_s) and grace_s >= 0):
            return refusal
    return None


def main() -> int:
    spec = json.load(sys.stdin)
    # repoint stdin at /dev/null: nothing downstream may re-read the spec fd
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    refusal = _spec_refusal(spec)
    if refusal is not None:
        print(f"runner_wrapper: {refusal}", file=sys.stderr)
        return 2

    # duty 1: own session; tolerate a spawner that already made us a leader
    if os.getsid(0) != os.getpid():
        os.setsid()
    # ss4 duty 2: SIGKILL, SIGABRT, a terminating signal not listed here, or
    # the machine may silence the recorder; E7 then reports the run
    for sig in _RECORDER_IGNORED:
        signal.signal(sig, signal.SIG_IGN)

    run_dir: str = spec["run_dir"]
    lifeline_fd: int = spec["lifeline_fd"]
    identity = {
        "run_id": spec["run_id"],
        "job": spec["job"],
        "run_number": spec["run_number"],
    }
    boot_id = current_boot_id()

    # SIGCHLD self-pipe, registered BEFORE spawning so no exit is missed
    self_pipe_r, self_pipe_w = os.pipe()
    os.set_blocking(self_pipe_r, False)
    os.set_blocking(self_pipe_w, False)

    def _on_chld(_signum: int, _frame: object) -> None:
        try:
            os.write(self_pipe_w, b"x")
        except OSError:
            pass

    signal.signal(signal.SIGCHLD, _on_chld)
    # the handler runs between bytecodes, so a SIGCHLD landing before select() starts
    # would leave the pipe empty and the select asleep with the lifeline open. The wakeup
    # fd writes the signal number into the same pipe at delivery, closing that window. It
    # fires only for a signal with a Python handler, so `_on_chld` stays; the select loops
    # drain the bytes and never read them.
    signal.set_wakeup_fd(self_pipe_w, warn_on_full_buffer=False)

    env = dict(os.environ)
    env["DSL41_RUN"] = base64.urlsafe_b64encode(
        json.dumps({**identity, "boot_id": boot_id}, sort_keys=True).encode("utf-8")
    ).decode("ascii")

    def _open_append_0600(path: str):
        # vendor APPENDS; 0600 at create, since job output may carry anything
        return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "ab")

    _test_pause("pre_spawn")
    try:
        with (
            _open_append_0600(spec["stdout_path"]) as stdout_f,
            _open_append_0600(spec["stderr_path"]) as stderr_f,
            open(spec.get("stdin_path") or os.devnull, "rb") as stdin_f,
        ):
            child = subprocess.Popen(
                ["/bin/sh", "-c", spec["command"]],
                stdin=stdin_f,
                stdout=stdout_f,
                stderr=stderr_f,
                env=env,
                process_group=0,  # duty 1: the command's OWN pgid, not ours
                close_fds=True,
                preexec_fn=_restore_default_signals,  # single-threaded: safe
            )
    except _IO_FAILURE as exc:
        try:
            durable_write_json(
                os.path.join(run_dir, "status.json"),
                {
                    "version": SPOOL_VERSION,
                    **identity,
                    "outcome": "spawn_failed",
                    "error": str(exc),
                    "ended_at": utc_now_iso(),
                },
            )
        except _IO_FAILURE as write_exc:
            print(f"runner_wrapper: spawn AND record failed: {write_exc}", file=sys.stderr)
            return 3
        return 0

    _test_pause("post_spawn_pre_record")
    spawn_record = {
        "version": SPOOL_VERSION,
        **identity,
        "wrapper_pid": os.getpid(),
        "wrapper_start_time": proc_start_token(os.getpid()),
        "command_pid": child.pid,
        "command_pgid": child.pid,
        "command_start_time": proc_start_token(child.pid),
        "boot_id": boot_id,
        "started_at": utc_now_iso(),
    }
    try:
        durable_write_json(os.path.join(run_dir, "spawn.json"), spawn_record)
    except _IO_FAILURE as exc:
        # cannot promise observability without the spawn record: kill what we started,
        # still try to record the outcome, and exit loudly
        print(f"runner_wrapper: spawn.json write failed: {exc}", file=sys.stderr)
        kill_observed = _await_exit_after_kill(
            child, self_pipe_r, float(spec.get("grace_seconds", 10.0))
        )
        _reap(child)
        try:
            durable_write_json(
                os.path.join(run_dir, "status.json"),
                {
                    "version": SPOOL_VERSION,
                    **identity,
                    "outcome": "terminated",
                    "cause": f"spawn record write failed ({exc}); killed",
                    "observed": kill_observed,
                    "ended_at": utc_now_iso(),
                },
            )
        except _IO_FAILURE:
            pass  # already loud on stderr; absence of status.json IS the E7 signal
        return 3
    _test_pause("post_record")

    grace_s = float(spec.get("grace_seconds", 10.0))
    status: dict[str, Any]
    while True:
        ready, _, _ = select.select([self_pipe_r, lifeline_fd], [], [])
        if self_pipe_r in ready:
            _drain(self_pipe_r)
        # duty 4: check child exit BEFORE lifeline EOF, on every wakeup
        observed = _observe_exit(child)
        if observed is not None:
            status = observed
            break
        if lifeline_fd in ready and os.read(lifeline_fd, 1) == b"":
            # duty 6: parent died (EOF fires even under kill -9)
            observed = _observe_exit(child)  # completion beats parent death
            if observed is not None:
                status = observed
            else:
                observed = _await_exit_after_kill(child, self_pipe_r, grace_s)
                status = {
                    "outcome": "terminated",
                    "cause": "parent lost",
                    "observed": observed,  # forensics: how the group died
                }
            break

    _test_pause("post_wait_pre_status")
    try:
        durable_write_json(
            os.path.join(run_dir, "status.json"),
            {"version": SPOOL_VERSION, **identity, **status, "ended_at": utc_now_iso()},
        )
    except _IO_FAILURE as exc:
        print(f"runner_wrapper: status.json write failed: {exc}", file=sys.stderr)
        _reap(child)
        return 3
    _test_pause("post_status_pre_reap")
    _reap(child)  # duty 5: record first, reap after
    return 0


if __name__ == "__main__":
    sys.exit(main())
