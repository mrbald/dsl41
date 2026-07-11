"""Per-run wrapper shim: the Tier-0 process-lifecycle recorder (phase 11b).

Normative spec: docs/runner-design.md ss6a (DL-41a) + docs/supervisor-protocol.md
(the spool format this module writes is the tier's frozen public contract,
DL-42). STDLIB ONLY: this module imports nothing from dsl41 and nothing
third-party -- its dumbness is a correctness property and the enforced
extraction boundary (DL-42; import-graph test in tests/test_runner_wrapper.py).
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
   the recorder before it records (the DL-41a codex-found bug).
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
   ``proc_start_token`` / ``start_tokens_match`` below, +/-2s tolerance on
   macOS's 1-second ``ps -o lstart=`` resolution, tick-exact on Linux.
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
from datetime import UTC, datetime
from typing import Any

SPEC_VERSION = 1

#: pause-point env var; see module docstring (test scaffolding, inert in prod)
PAUSE_ENV = "DSL41_WRAPPER_TEST_PAUSE"


# ------------------------------------------------------------------ durability


def durable_write(path: str, data: bytes) -> None:
    """The DL-41a durability liturgy: same-dir temp file, fsync(file),
    rename, fsync(directory). Requires a local filesystem."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def durable_write_json(path: str, record: dict[str, Any]) -> None:
    durable_write(path, json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")


# ------------------------------------------------------------ machine identity


def current_boot_id() -> str:
    """Boot session identity (DL-42 item 5): a reboot recycles the whole
    (pid, start-time) space, so a boot_id mismatch voids liveness checks AND
    proves nothing survived."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        pass
    out = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return "unknown"


def proc_start_token(pid: int) -> str | None:
    """Opaque start-time token for the (pid, start-time) PID-reuse guard, or
    None when the pid is gone. Linux: tick-exact starttime, field 22 of
    /proc/<pid>/stat (split after the LAST ')' -- comm may contain spaces and
    parens). macOS: ``ps -o lstart=`` under LC_ALL=C, 1-second resolution."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("ascii", "replace")
        except OSError:
            return None
        fields = raw.rsplit(")", 1)[1].split()
        return f"ticks:{fields[19]}"  # fields[0] is field 3 overall
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    lstart = out.stdout.strip()
    if out.returncode != 0 or not lstart:
        return None
    return f"lstart:{lstart}"


_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def start_tokens_match(a: str, b: str, *, tolerance_s: float = 2.0) -> bool:
    """Compare start-time tokens: tick tokens exactly, lstart tokens within
    +/-2s (macOS ps rounds to whole seconds; DL-41a probed the drift)."""
    if a.startswith("ticks:") or b.startswith("ticks:"):
        return a == b
    if not (a.startswith("lstart:") and b.startswith("lstart:")):
        return False
    try:
        ta = time.mktime(time.strptime(a[len("lstart:") :], _LSTART_FORMAT))
        tb = time.mktime(time.strptime(b[len("lstart:") :], _LSTART_FORMAT))
    except ValueError:
        return False
    return abs(ta - tb) <= tolerance_s


def verify_alive(pid: int, recorded_token: str) -> bool:
    """The PID-reuse guard: a pid is only 'ours' if it exists AND its start
    time matches the recorded token. Never signal a pid that fails this."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists under another uid; the token check decides
    token = proc_start_token(pid)
    return token is not None and start_tokens_match(token, recorded_token)


# ------------------------------------------------------------------- the shim


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # the whole group is already gone


def _await_exit_after_kill(
    child: subprocess.Popen[bytes], self_pipe_r: int, grace_s: float
) -> dict[str, Any]:
    """SIGTERM the command pgid, wait up to grace_s (waking on SIGCHLD via
    the self-pipe), then SIGKILL and wait unconditionally (an unkillable
    zombie-to-be still exits on SIGKILL; D-state is the machine's problem)."""
    _killpg_quiet(child.pid, signal.SIGTERM)
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
    _killpg_quiet(child.pid, signal.SIGKILL)
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

    _test_pause("pre_spawn")
    try:
        with (
            open(spec["stdout_path"], "ab") as stdout_f,  # vendor APPENDS
            open(spec["stderr_path"], "ab") as stderr_f,
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
    except OSError as exc:
        try:
            durable_write_json(
                os.path.join(run_dir, "status.json"),
                {
                    "version": SPEC_VERSION,
                    **identity,
                    "outcome": "spawn_failed",
                    "error": str(exc),
                    "ended_at": _utc_now_iso(),
                },
            )
        except OSError as write_exc:
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
        "started_at": _utc_now_iso(),
    }
    try:
        durable_write_json(os.path.join(run_dir, "spawn.json"), spawn_record)
    except OSError as exc:
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
                    "ended_at": _utc_now_iso(),
                },
            )
        except OSError:
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
            {"version": SPEC_VERSION, **identity, **status, "ended_at": _utc_now_iso()},
        )
    except OSError as exc:
        print(f"runner_wrapper: status.json write failed: {exc}", file=sys.stderr)
        _reap(child)
        return 3
    _test_pause("post_status_pre_reap")
    _reap(child)  # duty 5: record first, reap after
    return 0


if __name__ == "__main__":
    sys.exit(main())
