"""Per-run wrapper shim: the Tier-0 process-lifecycle recorder (phase 11b).

Normative spec: docs/runner-design.md ss6a (DL-41a) + docs/supervisor-protocol.md
(the spool format this module writes is the tier's frozen public contract,
DL-42). STDLIB ONLY: this module imports nothing from dsl41 and nothing
third-party -- its dumbness is a correctness property and the enforced
extraction boundary (DL-42; import-graph test in tests/test_runner_lifecycle.py).
Its one non-stdlib import is the sibling stdlib-only ``runner_procid``, which
carries the durability/identity helpers the supervisor needs too (DL-72).
The engine runs it BY FILE PATH (``sys.executable <this file>``), never
``-m dsl41.runner_wrapper``: ``-m`` would import the dsl41 package __init__
and drag third-party imports into the recorder's runtime.

Why this process exists: Unix gives exactly one wait() observation of a
child's exit status; if the observer is down when the child dies, init reaps
it and the status is gone forever. So the one process that cannot miss the
observation -- the direct parent -- writes it durably. Prior art:
containerd-shim, slurmstepd, HTCondor's starter.

Duties, in order (ss6a Tier 0):

1. Own session via setsid() (tolerated if the spawner already made us a
   leader). The command child is placed in its OWN pgid, separate from the
   wrapper's, via ``process_group=0`` at spawn: kill(-pgid) must never kill
   the recorder before it records (the DL-41a review-found bug).
2. Durably write ``spawn.json`` (run_dir): run/job identity, wrapper and
   command (pid, start-time) tokens, command pgid, boot_id, started_at.
   Durability liturgy for every record: temp file in the same directory,
   fsync(file), rename, fsync(directory). The run dir must be on a local
   filesystem (rename-over-NFS has ambiguous crash semantics).
3. Spawn ``/bin/sh -c <command>`` with the DSL41_RUN env tag (base64url
   JSON: run_id, job, run_number, boot_id). The tag is FORENSICS ONLY --
   macOS KERN_PROCARGS2 omits env for restricted binaries like /bin/sh and
   Linux /proc/pid/environ is ptrace-gated (DL-41a, probed empirically).
   Identity verification uses the (pid, start-time) tuple instead:
   ``proc_start_token`` / ``start_tokens_match`` (runner_procid), +/-2s
   tolerance on macOS's 1-second ``ps -o lstart=`` resolution, tick-exact
   on Linux.
4. Portable event loop: SIGCHLD self-pipe + select over {self-pipe,
   lifeline}. On EVERY wakeup check child-exit BEFORE lifeline EOF -- a job
   completing at the instant its parent dies must be recorded as a
   completion, not as "parent lost".
5. On child exit: observe with waitid(WNOWAIT) (observe-before-reap narrows
   the observe-to-record hole to a few syscalls), durably write
   ``status.json``, then reap.
6. On lifeline EOF (the parent died -- including kill -9; the kernel closes
   fds regardless): re-check child exit, then SIGTERM the command pgid,
   grace, SIGKILL, write ``status.json`` outcome=terminated cause="parent
   lost", exit. This makes "engine death => jobs terminate AND are recorded"
   hold with no polling and no Linux-only mechanism.

status.json outcomes (frozen in docs/supervisor-protocol.md):
  exited(exit_code) | signaled(signal) -- how the command itself ended;
  terminated(cause="parent lost")      -- the wrapper killed it on EOF;
  spawn_failed(error)                  -- /bin/sh could not be spawned at all.
The engine maps: exited -> raw exit_code through SEM-09 oracle-side;
signaled/terminated -> STATUS TERMINATED (a kill that actually happened);
spawn_failed -> STATUS FAILURE. A missing status.json is the one thing this
process can never produce -- that absence IS the E7 unobservable case.

The wrapper ignores SIGTERM/SIGINT/SIGHUP/SIGQUIT: only SIGKILL (or the
machine) can silence the recorder, which pins the residual crash matrix to
exactly the DL-41a accepted cases (-9 of the wrapper alone, or of the whole
tree at once -- both detected at reconciliation and reported truthfully).

Test scaffolding: the DSL41_WRAPPER_TEST_PAUSE env var names comma-separated
pause points ({pre_spawn, post_spawn_pre_record, post_record,
post_wait_pre_status, post_status_pre_reap}); the wrapper SIGSTOPs itself at
each named point so the kill-matrix tests (DL-42 item 8) can freeze it at a
phase boundary. Absent the env var (production) the hook is inert. The
DL-42 "post-fork pre-exec" boundary is not portably hookable from Python;
post_spawn_pre_record covers it -- from the recorder's point of view both
mean "command pid exists, spawn.json does not", and recovery semantics
depend only on that.

Wrapper input: a JSON spec on stdin (see docs/supervisor-protocol.md).
The wrapper is parent-agnostic: engine (11b) and supervisor (11f) spawn it
identically. Its own exit code is only a notification (0 = a status record
was written; 2 = spec/setup error; 3 = record write failed, e.g. ENOSPC) --
status.json is the sole data channel for the command's outcome.
"""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any

# DL-72: the shared process-identity helpers live in a sibling stdlib-only
# module. sys.path[0] is this file's directory when we are run by file path --
# except under PYTHONSAFEPATH=1, which strips it; prepend it ourselves so the
# plain top-level import resolves either way (importing dsl41.runner_procid
# would drag the package __init__, and with it third-party imports, in).
# Prepend only when it is missing and take it back off again: this file is ALSO
# imported as an ordinary package module (the engine reads __file__ and
# SPEC_VERSION off it), and a library must not leave its own package directory
# on the importing process's sys.path -- there it would shadow top-level names
# (ir, cli, viz, ...) for the whole process. sys.path ends exactly as CPython
# handed it to us, in both invocation modes.
#
# mypy maps this file's sibling as dsl41.runner_procid and cannot also see it
# under its top-level name, so the by-path import alone left every helper call
# below Any-typed -- in a file that kills processes. The TYPE_CHECKING branch
# names the module mypy already knows; it is erased before the process starts,
# so the stdlib-only boundary is untouched (the import-graph test reads runtime
# imports only, and the else branch is what runs).
_PROCID_DIR = os.path.dirname(os.path.abspath(__file__))
_PROCID_DIR_ADDED = _PROCID_DIR not in sys.path
if _PROCID_DIR_ADDED:
    sys.path.insert(0, _PROCID_DIR)
if TYPE_CHECKING:
    from dsl41.runner_procid import (
        current_boot_id,
        durable_write_json,
        killpg_quiet,
        proc_start_token,
        utc_now_iso,
    )
else:
    from runner_procid import (  # noqa: E402
        current_boot_id,
        durable_write_json,
        killpg_quiet,
        proc_start_token,
        utc_now_iso,
    )

if _PROCID_DIR_ADDED:
    sys.path.remove(_PROCID_DIR)

SPEC_VERSION = 1

#: how a spawn or a durable record write is allowed to fail (DL-151). OSError
#: is the expected half. ValueError is the half that got away: an embedded
#: NUL in a path or in the command makes `os.open`, `subprocess.Popen` and
#: every `os.path` call raise ValueError, not OSError, so such a spec killed
#: this process with no `status.json` at all -- the E7 absence this module
#: exists never to produce. The supervisor now refuses a NUL at the ss2 gate;
#: this is the recorder's own belt, because the absence of a record is the
#: one thing it may not answer with.
_IO_FAILURE = (OSError, ValueError)

#: pause-point env var; see module docstring (test scaffolding, inert in prod)
PAUSE_ENV = "DSL41_WRAPPER_TEST_PAUSE"


# ------------------------------------------------------------------- the shim


def _test_pause(point: str) -> None:
    named = os.environ.get(PAUSE_ENV, "")
    if point in {p.strip() for p in named.split(",") if p.strip()}:
        os.kill(os.getpid(), signal.SIGSTOP)


def _observe_exit(child: subprocess.Popen[bytes]) -> dict[str, Any] | None:
    """Observe the child's exit WITHOUT reaping (waitid WNOWAIT + WNOHANG);
    None while it still runs. Falling back to waitpid (reap-on-observe) only
    where waitid is missing -- the observe-to-record hole widens there.
    Either way ``_reap`` stays safe afterwards: the waitid path leaves a
    reapable zombie for child.wait(); the waitpid path sets child.returncode
    so child.wait() returns immediately."""
    if hasattr(os, "waitid"):
        try:
            info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return None  # already reaped: only possible after we reaped it
        if info is None or info.si_pid == 0:
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
    """Child-side (post-fork pre-exec) reset. The wrapper ignores
    TERM/INT/HUP/QUIT to protect the recorder, but SIG_IGN dispositions are
    inherited ACROSS exec (and non-interactive sh keeps them for its own
    children) -- without this reset the command silently ignores the graceful
    SIGTERM and every kill escalates to SIGKILL (found by the 11b smoke)."""
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, signal.SIG_DFL)


def _await_exit_after_kill(
    child: subprocess.Popen[bytes], self_pipe_r: int, grace_s: float
) -> dict[str, Any]:
    """SIGTERM the command pgid, wait up to grace_s (waking on SIGCHLD via
    the self-pipe), then SIGKILL and wait unconditionally (an unkillable
    zombie-to-be still exits on SIGKILL; D-state is the machine's problem)."""
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


def main() -> int:
    spec = json.load(sys.stdin)
    # repoint stdin at /dev/null: nothing downstream may re-read the spec fd
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    if spec.get("version") != SPEC_VERSION:
        print(f"runner_wrapper: unsupported spec version {spec.get('version')!r}", file=sys.stderr)
        return 2

    # duty 1: own session; tolerate a spawner that already made us a leader
    if os.getsid(0) != os.getpid():
        os.setsid()
    # only SIGKILL (or the machine) may silence the recorder
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
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

    env = dict(os.environ)
    env["DSL41_RUN"] = base64.urlsafe_b64encode(
        json.dumps({**identity, "boot_id": boot_id}, sort_keys=True).encode("utf-8")
    ).decode("ascii")

    def _open_append_0600(path: str):
        # vendor APPENDS; 0600 at create (job output may carry anything the
        # command prints -- owner-only by default)
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
                    "version": SPEC_VERSION,
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
        "version": SPEC_VERSION,
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
        # cannot promise observability without the spawn record: kill what we
        # started, still try to record the outcome, and exit loudly
        print(f"runner_wrapper: spawn.json write failed: {exc}", file=sys.stderr)
        kill_observed = _await_exit_after_kill(
            child, self_pipe_r, float(spec.get("grace_seconds", 10.0))
        )
        _reap(child)
        try:
            durable_write_json(
                os.path.join(run_dir, "status.json"),
                {
                    "version": SPEC_VERSION,
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
            {"version": SPEC_VERSION, **identity, **status, "ended_at": utc_now_iso()},
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
