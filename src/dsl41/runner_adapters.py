"""Runner adapters (ss6/ss6a): the dispatch half of the sans-IO shell --
the adapter protocol, the CMD wrapper paths, and the supervisor client.

Split out of runner.py by DL-74, with the paragraphs it owns, verbatim.

Adapter contract (ss6): ``async run(job_ir, run_number, ctx) -> AdapterResult``
where an ``int`` is the RAW exit code (SEM-09/DL-33 classification stays
oracle-side), ``Terminated`` reports a kill the wrapper actually observed
(-> STATUS TERMINATED), and ``Failed`` reports a completion with no raw exit
code (spawn failure, or the E7 unobservable case -> STATUS FAILURE with its
cause). Adapters never retry (n_retrys is unmodeled v1, DL-53 scope: a
shell-side retry would fork semantics from the simulator) and never time
out (term_run_time is the oracle's timer). Under VirtualClock an adapter
may block ONLY through
ctx.clock.sleep_until; that restriction is what makes quiescence decidable
(Engine._settle counts live tasks against pending sleeps). Real adapters
(11b) run under RealClock, where the loop blocks on real IO -- _settle is a
single reaping pass and the loop waits on the queue-activity event instead
of settling (DL-43 item 5).

Phase 11b (ss6-ss7; DL-41a/DL-42 pin the lifecycle semantics):

- LocalCommandAdapter runs every command under the ss6a Tier-0 wrapper
  (runner_wrapper.py, spawned BY FILE PATH -- see its docstring), and the
  wrapper's status.json is the sole outcome channel; the wrapper's exit
  only notifies the engine to read it. Cancel (the oracle said terminal) =
  verify the recorded (pid, start-time), signal the command pgid SIGTERM,
  grace, SIGKILL; the wrapper observes and records; the cancelled adapter
  never reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import os
import signal
import subprocess
import sys
import time
import uuid

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from dsl41 import runner_procid as _procid
from dsl41 import runner_supervisor as _supervisor
from dsl41 import runner_wrapper as _wrapper
from dsl41.ir import ExecSpec, FwSpec, JobIR
from dsl41.runner_clock import Clock, EngineError

if TYPE_CHECKING:  # annotation only: the WAL is the engine's, not an adapter's
    from dsl41.runner_journal import Journal


#: the ss6a Tier-0 shim, executed by file path (never -m; see its docstring)
_WRAPPER_PATH = Path(_wrapper.__file__)
#: the ss6a Tier-1 supervisor, likewise run by file path (stdlib-only boundary)
_SUPERVISOR_PATH = Path(_supervisor.__file__)


# JSON-lines buffer cap for every asyncio stream endpoint (control socket
# both sides, supervisor client). One `status` response is one line covering
# EVERY job (~220 bytes each), so asyncio's 64 KiB default readline() limit
# overruns at ~300 jobs; 16 MiB clears any plausible estate while still
# bounding a runaway peer. Public since DL-78: three modules need it
# (this one, runner_control's server AND client) and a shared constant
# imported through a private name is a boundary that was never real.
LINE_LIMIT: int = 2**24


@dataclass
class DetachSignal:
    """Set by the engine BEFORE it cancels adapter tasks for a detach-stop
    (operator SIGINT/SIGTERM of a --detached run, or shutdown-for-resume).
    The SupervisedCommandAdapter's CancelledError path branches on it: when
    stopping, it abandons the await and signals NOTHING -- jobs keep running
    under the supervisor (spec ss3 case b). Tethered adapters ignore it."""

    stopping: bool = False


@dataclass
class AdapterContext:
    """What an adapter may touch (ss6): the clock, and in the real domain
    the run-root layout (runs/, logs/) plus the WAL for dispatch records.
    `detach` distinguishes a detach-stop from an oracle-decided kill in the
    detached CMD path (ss3 case b vs a)."""

    clock: Clock
    run_root: Path | None = None
    journal: Journal | None = None
    detach: DetachSignal | None = None


@dataclass(frozen=True)
class Terminated:
    """The command was killed and the kill was OBSERVED (wrapper status.json:
    signaled / terminated). The engine injects STATUS TERMINATED -- reserved
    for kills that actually happened (DL-41a item 7)."""

    cause: str


@dataclass(frozen=True)
class Failed:
    """A completion with no raw exit code: spawn failure, or the E7
    unobservable case. The engine injects STATUS FAILURE with the cause --
    never anything that could satisfy a success-dependent downstream."""

    cause: str


#: int = RAW exit code (SEM-09/DL-33 verdict stays oracle-side)
AdapterResult = int | Terminated | Failed


class JobAdapter(Protocol):
    """ss6 adapter protocol; see the module docstring for the contract."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult: ...


class FakeAdapter:
    """ss6: scripted ``(job, run_number) -> (duration_s, exit_code)``;
    default instant success. ``default=None`` makes unscripted runs INERT:
    the task parks on a sleep at datetime.max, which no real horizon ever
    reaches, so the SCRIPT drives completions via injected STATUS -- exactly
    the role the oracle trace tests already play. The bisimulation harness
    runs this mode; rehearse scenarios use scripted entries."""

    def __init__(
        self,
        script: Mapping[tuple[str, int], tuple[float, int]] | None = None,
        *,
        default: tuple[float, int] | None = (0.0, 0),
    ) -> None:
        self.script = dict(script or {})
        self.default = default

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int:
        entry = self.script.get((job_ir.name, run_number), self.default)
        if entry is None:
            await ctx.clock.sleep_until(datetime.max)
            raise AssertionError("inert park elapsed: horizon reached datetime.max")
        duration_s, exit_code = entry
        await ctx.clock.sleep_until(ctx.clock.now() + timedelta(seconds=duration_s))
        return exit_code


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_json(path: Path) -> dict[str, Any] | None:
    """Tolerant spool read: missing or unparseable -> None (an unreadable
    record can never be trusted for signaling; the ladder falls through)."""
    try:
        with path.open("rb") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _naive_utc(iso: str) -> datetime:
    """Wrapper timestamps (aware UTC ISO) -> the engine's naive-UTC basis."""
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _outcome_from_status(status: dict[str, Any]) -> AdapterResult:
    """Map a wrapper status.json record (docs/supervisor-protocol.md ss3) to
    an adapter result. Shared by the live adapter path and reconciliation so
    live and resumed runs can never diverge on the same record. A malformed
    record maps to FAILURE with a truthful cause -- never to anything that
    could satisfy a success-dependent downstream."""
    outcome = status.get("outcome")
    if outcome == "exited":
        exit_code = status.get("exit_code")
        if isinstance(exit_code, int):
            return exit_code
        return Failed(f"malformed status record: outcome 'exited' with exit_code={exit_code!r}")
    if outcome == "signaled":
        sig = status.get("signal")
        cause = (
            f"killed by signal {sig}" if isinstance(sig, int) else "killed by signal (unrecorded)"
        )
        # PENDING: E8 -- an EXTERNAL signal death (engine alive, no oracle
        # kill decision) maps to TERMINATED per the DL-41a recorded-signal
        # reading; vendor parity unverified (real AutoSys may mark FAILURE).
        # Swept 2026-07-28 (DL-53): publicly undocumented. Re-swept
        # 2026-07-30 (DL-58): one agent KB shows a spawn-path signal-9 abort
        # reported as FAILED -- directional evidence for FAILURE, but it is
        # not the mid-run kill scenario; TERMINATED stands until a live
        # one-kill test (trap-TERM variant discriminates the mechanism).
        return Terminated(cause)
    if outcome == "terminated":
        return Terminated(str(status.get("cause", "terminated")))
    if outcome == "spawn_failed":
        return Failed(f"spawn failed: {status.get('error')}")
    return Failed(f"unrecognized status record outcome {outcome!r}")


def _build_run_spec(
    job_ir: JobIR, run_number: int, ctx: AdapterContext, *, grace_seconds: float
) -> tuple[Path, dict[str, Any]]:
    """The run_dir/log-path/wrapper-spec construction shared by the tethered
    (LocalCommandAdapter) and detached (SupervisedCommandAdapter) CMD paths --
    everything the wrapper input spec needs EXCEPT lifeline_fd, which each
    caller fills from the end that owns the pipe's write side (engine tethered,
    supervisor detached). Kept as one function so the two paths can never
    diverge on run-dir layout, profile composition, or log targets (ss6a)."""
    if ctx.run_root is None:
        raise EngineError("command dispatch needs a run_root (real domain only)")
    spec_ir = job_ir.exec_
    if not isinstance(spec_ir, ExecSpec):
        raise EngineError(f"{job_ir.name!r}: CMD dispatch without an ExecSpec")
    if os.sep in job_ir.name or job_ir.name in (".", ".."):
        raise EngineError(f"job name {job_ir.name!r} is not a safe run-directory name")
    command = spec_ir.command
    if spec_ir.profile:
        command = f". {spec_ir.profile} && {command}"  # PENDING: E5
    run_dir = ctx.run_root / "runs" / f"{job_ir.name}.{run_number}"
    run_dir.mkdir(parents=True)  # a collision is a bug: run_numbers never repeat
    _fsync_dir(run_dir)
    _fsync_dir(run_dir.parent)  # liturgy: the runs dir fsync'd at creation
    (ctx.run_root / "logs").mkdir(exist_ok=True)
    stdout_path, stderr_path = job_log_paths(job_ir, run_number, ctx.run_root)
    spec = {
        "version": _wrapper.SPEC_VERSION,
        "run_id": str(uuid.uuid4()),
        "job": job_ir.name,
        "run_number": run_number,
        "command": command,
        "run_dir": str(run_dir),
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdin_path": spec_ir.std_in_file,
        "grace_seconds": grace_seconds,
    }
    return run_dir, spec


class LocalCommandAdapter:
    """ss6 CMD adapter: spawn the ss6a Tier-0 wrapper, await it, read
    status.json -- the sole outcome channel. No retries (n_retrys unmodeled,
    DL-53 scope), no timeouts (term_run_time is the oracle's timer), no
    classification.
    stdout/stderr APPEND to std_out_file/std_err_file when set (vendor
    appends), else to <run_root>/logs/<job>.<run_number>.{out,err};
    std_in_file when set, else /dev/null. `profile` sources first:
    ``. <profile> && <command>`` -- a failing profile fails the job with
    sh's exit code (PENDING: E5). DL-39 [?] verbatim carry applies: the
    command string is passed to /bin/sh exactly as the IR holds it.

    Cancellation (the oracle said terminal): verify the recorded command
    (pid, start-time), SIGTERM the command pgid, grace, SIGKILL; the
    wrapper observes the deaths and records the outcome durably; the
    cancelled adapter never reports. The lifeline write end lives in this
    process ONLY and is closed in a finally: engine death EOFs every
    wrapper (tethered semantics, ss6a)."""

    def __init__(self, *, grace_seconds: float = 10.0) -> None:
        self.grace_seconds = grace_seconds

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        run_dir, spec = _build_run_spec(job_ir, run_number, ctx, grace_seconds=self.grace_seconds)
        lifeline_r, lifeline_w = os.pipe()
        try:
            spec["lifeline_fd"] = lifeline_r  # tethered: the write end lives HERE
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(_WRAPPER_PATH),
                    stdin=asyncio.subprocess.PIPE,
                    pass_fds=(lifeline_r,),
                )
            except OSError as exc:
                # EMFILE/ENOMEM-class glitch: fail THIS job, not the engine
                # (symmetric with the wrapper's own spawn_failed)
                return Failed(f"wrapper spawn failed: {exc}")
            finally:
                os.close(lifeline_r)  # our copy; the wrapper holds its own now
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(spec).encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except OSError as exc:
                # the wrapper died while reading its spec (pre-spawn by
                # construction: it spawns only after the full spec parses)
                await proc.wait()
                return Failed(f"wrapper spawn failed: {exc}")
            try:
                if ctx.journal is not None:
                    ctx.journal.dispatch(
                        job_ir.name,
                        run_number,
                        wrapper_pid=proc.pid,
                        run_dir=str(run_dir),
                        started_at=ctx.clock.now(),
                    )
                await proc.wait()
            except asyncio.CancelledError:
                await self._kill(run_dir, proc)
                raise
            status = load_json(run_dir / "status.json")
            if status is None:
                # the recorder exited without a record (rc 2/3: spec error,
                # ENOSPC): observability is gone -- report it, never guess
                return Failed(  # PENDING: E7
                    f"exit_status_unobservable (wrapper exited rc={proc.returncode}"
                    " without a status record)"
                )
            return _outcome_from_status(status)
        finally:
            os.close(lifeline_w)

    async def _kill(self, run_dir: Path, proc: asyncio.subprocess.Process) -> None:
        """The oracle decided terminal: signal the command pgid (never the
        wrapper -- the recorder is untouchable), escalate after grace, then
        wait for the wrapper to record and exit."""
        if proc.stdin is not None:
            proc.stdin.close()  # a wrapper still reading its spec must not hang
        spawn = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            spawn = load_json(run_dir / "spawn.json")
            if spawn is not None or proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        if spawn is None:
            # only reachable when the wrapper died or is frozen pre-record
            # (test pauses); the lifeline tether covers the residue -- wait
            # bounded, then leave the wrapper to its own record
            try:
                await asyncio.wait_for(proc.wait(), timeout=2 * self.grace_seconds)
            except TimeoutError:
                pass
            return
        pid = spawn.get("command_pid")
        pgid = spawn.get("command_pgid")
        token = spawn.get("command_start_time")
        if (
            isinstance(pid, int)
            and isinstance(pgid, int)
            and isinstance(token, str)
            and _procid.verify_alive(pid, token)  # the PID-reuse guard
        ):
            _procid.killpg_quiet(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.grace_seconds)
            except TimeoutError:
                _procid.killpg_quiet(pgid, signal.SIGKILL)
        await proc.wait()  # the wrapper records the outcome, then exits


def job_log_paths(job_ir: JobIR, run_number: int, run_root: Path) -> tuple[str, str]:
    """ss6 append targets for a CMD run: std_out_file/std_err_file when set
    (vendor appends), else <run_root>/logs/<job>.<run_number>.{out,err}. One
    resolver shared by the adapter's wrapper spec and the ss10 status
    response (the ss11 log tail reads what the wrapper writes -- the two
    must never diverge)."""
    out = err = None
    if isinstance(job_ir.exec_, ExecSpec):
        out, err = job_ir.exec_.std_out_file, job_ir.exec_.std_err_file
    logs_dir = run_root / "logs"
    return (
        out or str(logs_dir / f"{job_ir.name}.{run_number}.out"),
        err or str(logs_dir / f"{job_ir.name}.{run_number}.err"),
    )


class FileWatcherAdapter:
    """ss6 FW adapter: poll every watch_interval seconds (default 60 [?]
    PENDING: E6) until watch_file exists with size >= watch_file_min_size
    (unset -> 0) and the size is stable across two consecutive qualifying
    polls ([?] steady-size reading pinned -- E6). Completes with exit 0.
    Clock-driven (ctx.clock.sleep_until), so the same code runs in both
    time domains; polling is an idempotent read, which is why resume may
    re-dispatch an incomplete watch (module docstring)."""

    def __init__(self, *, default_interval_s: int = 60) -> None:
        self.default_interval_s = default_interval_s  # PENDING: E6

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        spec_ir = job_ir.exec_
        if not isinstance(spec_ir, FwSpec):
            raise EngineError(f"{job_ir.name!r}: FW dispatch without an FwSpec")
        interval = spec_ir.watch_interval or self.default_interval_s
        min_size = spec_ir.watch_file_min_size or 0
        previous: int | None = None
        while True:
            try:
                size: int | None = os.stat(spec_ir.watch_file).st_size
            except OSError:
                size = None
            if size is not None and size >= min_size:
                if previous == size:
                    return 0
                previous = size
            else:
                previous = None
            await ctx.clock.sleep_until(ctx.clock.now() + timedelta(seconds=interval))


class SupervisorUnavailable(RuntimeError):
    """The supervisor socket is gone (never came up, refused, or died
    mid-run). The engine survives it: pending runs resolve from the spool
    ladder (spec ss3), the tethered fallback for the detached path."""


class SupervisorConn:
    """Blocking one-shot client of the same ss6a socket, for callers with no
    event loop -- the `dsl41 supervise` verb. Async exit PUSHes are skipped:
    this end is not a data-channel consumer (supervisor-protocol ss5 makes
    them droppable notifications).

    It lives beside `SupervisorClient` for the reason `roundtrip` lives
    beside `ControlClient` (DL-78, DL-91): two transports for ONE protocol,
    not two protocols. The framing rule below -- stamp `"v": 1`, read
    newline-delimited JSON, drop pushes -- was written once here and once in
    cli.py, which is one place too many for a rule a frozen document owns."""

    def __init__(self, sock_path: Path) -> None:
        self.conn = socket.socket(socket.AF_UNIX)
        # SHUTDOWN replies only AFTER waiting for wrappers (frozen ss5 order),
        # which spans the spawn-record wait plus per-run grace windows
        self.conn.settimeout(60.0)
        self.conn.connect(str(sock_path))
        self.buf = b""

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.conn.sendall(json.dumps({**request, "v": 1}).encode("utf-8") + b"\n")
        while True:
            while b"\n" not in self.buf:
                chunk = self.conn.recv(65536)
                if not chunk:
                    raise OSError("supervisor closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("push"):
                continue  # notifications are droppable (supervisor-protocol ss5)
            return obj

    def close(self) -> None:
        self.conn.close()


class SupervisorClient:
    """Engine-side client of the ss6a Tier-1 supervisor (this side may import
    dsl41 freely). Ensures a supervisor is running (spawning one DETACHED if
    the socket is dead/absent -- a live socket is reused, which is exactly
    reattachment), holds the single-controller lease (ACQUIRE + a background
    RENEW task), serializes one request/response at a time, and demuxes async
    exit PUSHes into per-run_id futures. Socket loss sets `lost`; adapters
    awaiting a run watch it, try `reconnect()` once, and only then fall back
    to the spool (supervisor-protocol ss5: pushes are notifications, the
    spool is the truth).

    Cancellation POISONS the connection (review fix, DL-48): the frozen ss5
    protocol has no correlation ids, so a request cancelled between write
    and reply leaves the stream state unknowable -- the reply in flight
    would be delivered to the NEXT request's future. On CancelledError
    mid-request the client fails the pending future, closes the socket, and
    re-raises; later calls lazily reconnect (connect-only, never spawning a
    supervisor) and re-ACQUIRE presenting the CURRENT token (DL-79), whose fresh
    fencing token fences anything the poisoned connection had in flight."""

    #: engine defaults (spec ss2): 60s lease, renewed every 20s
    _TTL_S = 60.0
    _RENEW_EVERY_S = 20.0

    def __init__(
        self,
        run_root: Path,
        *,
        deadman_s: float | None = None,
        on_contact: Callable[[], None] | None = None,
    ) -> None:
        self.run_root = run_root
        #: S5b (concurrency-model ss8): the deadman a supervisor WE spawn is
        #: started with. It is not what gets recorded -- `supervisor_deadman_s`
        #: below is, read back from the supervisor -- because a reattaching
        #: engine meets a supervisor it did not start, and a bound derived
        #: from this engine's own flag would then describe nothing.
        self.deadman_s = deadman_s
        #: what the supervisor we are talking to says it runs. None until the
        #: first PING/ACQUIRE answers, and None afterwards if it runs none.
        self.supervisor_deadman_s: float | None = None
        #: called after every confirmed lease exchange -- ss8's "positive
        #: contact with this host". A callback rather than a store write here,
        #: because this module is the transport and the routing table is the
        #: engine's state; what it stamps is deliberately UNPROJECTED, so a
        #: heartbeat costs no revision and no log record.
        self.on_contact = on_contact
        self.sock_path = run_root / "supervisor.sock"
        # Per-INCARNATION since DL-79, and deliberately not stable: the old
        # value said "one run_root has one logical engine controller", which
        # the ss10 control-socket bind enforces on THIS machine only, and the
        # supervisor used that equality to hand a live lease to a second
        # claimant. It no longer authorizes anything -- incumbency is proved
        # with the current token -- so this string is now for humans: LIST and
        # the lease_held refusal name WHICH engine holds it. Fast resume after
        # a crash comes from the holder's connection dying, not from a
        # matching label (supervisor _h_acquire).
        self.controller_id = f"engine:{run_root.resolve()}#{uuid.uuid4().hex[:8]}"
        self.token: int | None = None
        #: DL-80: the supervisor incarnation our token belongs to. _request
        #: pairs it with EVERY request rather than each mutating verb naming
        #: it -- a fencing credential that a new verb can forget to carry is
        #: not a credential. A `wrong_incarnation` refusal means the
        #: supervisor we knew is gone and our runs died with it (re-acquire
        #: AND reconcile), which is the opposite of `stale_token`.
        self.incarnation: str | None = None
        self.lost = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._closed = False
        self._pending: asyncio.Future[dict[str, Any]] | None = None
        self._exit_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # -- connection ---------------------------------------------------------

    async def ensure_running(self) -> None:
        """Connect to a live supervisor, or spawn a fresh one DETACHED
        (setsid, stdio to supervisor.log) and connect-with-retry. A live
        socket is reused -- that reuse IS reattachment (spec ss1)."""
        if await self._try_connect():
            return
        self._spawn_supervisor()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if await self._try_connect():
                return
        raise SupervisorUnavailable("supervisor did not come up within 10s")

    async def _try_connect(self) -> bool:
        if not self.sock_path.exists():
            return False
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.sock_path), limit=LINE_LIMIT
            )
        except ConnectionRefusedError:
            with contextlib.suppress(OSError):
                self.sock_path.unlink()  # stale: nobody is listening (parity with ss10)
            return False
        except OSError:
            return False
        # supersede any previous connection's remains BEFORE swapping identity:
        # the epoch guard (each reader carries its own `lost` event) keeps a
        # stale reader from poisoning or delivering into its successor
        if self._writer is not None:
            self._writer.close()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._writer = writer
        self.lost = asyncio.Event()
        self._reader_task = asyncio.ensure_future(self._reader(reader, self.lost))
        try:
            resp = await self._request({"cmd": "PING"}, _connect=False)
        except SupervisorUnavailable:
            return False
        if resp.get("ok"):
            self._note_contact(resp)  # PING is where a REATTACH learns the deadman
        return bool(resp.get("ok"))

    def _spawn_supervisor(self) -> None:
        argv = [sys.executable, str(_SUPERVISOR_PATH), "--run-root", str(self.run_root)]
        if self.deadman_s is not None:
            argv += ["--deadman-seconds", str(self.deadman_s)]
        logf = (self.run_root / "supervisor.log").open("ab")
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                start_new_session=True,  # detach job lifetime from this engine
                close_fds=True,
            )
        finally:
            logf.close()  # the child dup'd it; our copy is done

    # -- request / response / push demux ------------------------------------

    async def _ensure_connected(self) -> None:
        """Lazy reconnect (review fix, DL-48): a poisoned or lost connection
        heals on the next call rather than failing every caller until the
        engine restarts."""
        if self._closed:
            raise SupervisorUnavailable("client closed")
        if self._writer is not None and not self.lost.is_set():
            return
        if not await self.reconnect():
            raise SupervisorUnavailable("not connected")

    async def reconnect(self) -> bool:
        """Re-entry after a lost or poisoned connection: connect-only (never
        spawns a supervisor) + re-ACQUIRE when a lease was held -- the stable
        controller_id mints a fresh fencing token, which fences anything the
        old connection had in flight. False = the supervisor itself is
        unreachable (callers fall back to the ss7 spool ladder). Everything
        under the reconnect lock uses _connect=False: re-entering the lazy
        path from inside it would deadlock on this non-reentrant lock."""
        async with self._reconnect_lock:
            if self._closed:
                return False
            if self._writer is not None and not self.lost.is_set():
                return True  # another caller already reconnected
            if not await self._try_connect():
                return False
            if self.token is not None:
                try:
                    resp = await self._request(
                        {
                            "cmd": "ACQUIRE",
                            "controller_id": self.controller_id,
                            "ttl_s": self._TTL_S,
                            "token": self.token,  # DL-79: prove incumbency
                        },
                        _connect=False,
                    )
                except SupervisorUnavailable:
                    return False
                if not resp.get("ok"):
                    return False  # fenced out: another controller holds the lease
                self.token = int(resp["token"])
                self.incarnation = resp.get("incarnation")  # DL-80
            return True

    async def _request(self, obj: dict[str, Any], *, _connect: bool = True) -> dict[str, Any]:
        if _connect:
            await self._ensure_connected()
        async with self._lock:
            if self._writer is None or self.lost.is_set():
                raise SupervisorUnavailable("not connected")
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending = fut
            try:
                self._writer.write(
                    json.dumps(
                        {**obj, "v": 1, "incarnation": self.incarnation},
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
                await self._writer.drain()
                return await fut  # the reader resolves it, or _on_lost fails it
            except OSError as exc:
                self._pending = None
                raise SupervisorUnavailable(str(exc)) from exc
            except asyncio.CancelledError:
                # poison-on-cancel (review MAJOR, DL-48): the reply may be in
                # flight (or the request only partially written pre-drain);
                # with no correlation ids the stream is unknowable, so tear
                # the connection down -- the orphan reply can then never be
                # delivered to the NEXT request -- and re-raise. Later calls
                # heal via _ensure_connected.
                self._poison()
                raise

    def _poison(self) -> None:
        self.lost.set()
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(
                SupervisorUnavailable("request cancelled; connection poisoned")
            )
        self._pending = None
        if self._writer is not None:
            self._writer.close()  # the reader sees EOF and exits via its epoch
            self._writer = None

    async def _reader(self, stream: asyncio.StreamReader, lost: asyncio.Event) -> None:
        """One reader per connection; `lost` is that connection's epoch. A
        reader superseded by a poison+reconnect must neither deliver into nor
        poison its successor -- hence the `lost is self.lost` guards."""
        try:
            while True:
                line = await stream.readline()
                if not line or lost is not self.lost:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("push") == "exit":
                    self._deliver_push(obj)  # true exit facts: epoch-independent
                elif self._pending is not None and not self._pending.done():
                    pending, self._pending = self._pending, None
                    pending.set_result(obj if isinstance(obj, dict) else {})
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._on_lost(lost)

    def _deliver_push(self, obj: dict[str, Any]) -> None:
        run_id = obj.get("run_id")
        fut = self._exit_futures.get(run_id) if isinstance(run_id, str) else None
        if fut is not None and not fut.done():
            fut.set_result(obj)

    def _on_lost(self, lost: asyncio.Event) -> None:
        if lost is not self.lost:
            return  # a superseded connection's reader: the successor is fine
        if lost.is_set():
            return  # already poisoned; _poison cleaned the pending future
        lost.set()
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(SupervisorUnavailable("connection lost"))
        self._pending = None

    def exit_future(self, run_id: str) -> asyncio.Future[dict[str, Any]]:
        fut = self._exit_futures.get(run_id)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._exit_futures[run_id] = fut
        return fut

    def forget_exit(self, run_id: str) -> None:
        self._exit_futures.pop(run_id, None)

    # -- verbs --------------------------------------------------------------

    async def acquire(self, *, ttl_s: float | None = None) -> int:
        ttl = self._TTL_S if ttl_s is None else ttl_s
        resp = await self._request(
            {
                "cmd": "ACQUIRE",
                "controller_id": self.controller_id,
                "ttl_s": ttl,
                "token": self.token,
            }
        )
        if not resp.get("ok"):
            raise SupervisorUnavailable(f"lease acquire refused: {resp}")
        self.token = int(resp["token"])
        self.incarnation = resp.get("incarnation")  # DL-80
        self._note_contact(resp)
        if self._renew_task is None:
            self._renew_task = asyncio.ensure_future(self._renew_loop(ttl))
        return self.token

    def _note_contact(self, resp: Mapping[str, Any]) -> None:
        """One confirmed lease exchange: ss8's "positive contact with this
        host". `deadman_s` is only carried by the two read verbs and by
        ACQUIRE, so a RENEW's answer refreshes the contact and leaves the
        interval alone -- it cannot change without a new incarnation."""
        reported = resp.get("deadman_s")
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            self.supervisor_deadman_s = float(reported)
        if self.on_contact is not None:
            self.on_contact()

    async def _renew_loop(self, ttl_s: float) -> None:
        """Keep the lease alive (spec ss2: renew every 20s). Review fix
        (DL-48): a transient failure must not silently lapse a live engine's
        lease -- pushes would stop with only the adapters' status.json
        re-poll saving outcomes. Failed RENEWs retry on a short backoff; a
        stale/lapsed token re-ACQUIREs presenting the current token (DL-79:
        incumbency, not a label -- a lapsed lease is free, but one another
        engine now holds refuses us, which is the fencing working); a fresh
        token comes back; connection loss heals via _request's lazy reconnect.
        Only several consecutive failures give up -- loudly, once."""
        failures = 0
        try:
            while True:
                await asyncio.sleep(self._RENEW_EVERY_S if failures == 0 else 1.0)
                try:
                    resp = await self._request(
                        {"cmd": "RENEW", "token": self.token, "ttl_s": ttl_s}
                    )
                    if not resp.get("ok"):
                        if resp.get("error") == "wrong_incarnation":
                            # DL-80: the supervisor restarted under us. Our token
                            # belongs to a world that no longer exists, and every
                            # wrapper it held died by lifeline. Drop the pair so
                            # the re-ACQUIRE below takes the free path instead of
                            # replaying a credential that can now COLLIDE with the
                            # new incarnation's counter.
                            self.token, self.incarnation = None, None
                        # stale_token (fenced by a reconnect's own re-ACQUIRE,
                        # or lapsed): same controller re-acquires, fresh token
                        resp = await self._request(
                            {
                                "cmd": "ACQUIRE",
                                "controller_id": self.controller_id,
                                "ttl_s": ttl_s,
                                "token": self.token,  # DL-79
                            }
                        )
                        if resp.get("ok"):
                            self.token = int(resp["token"])
                            self.incarnation = resp.get("incarnation")  # DL-80
                except SupervisorUnavailable:
                    resp = {"ok": False}
                if resp.get("ok"):
                    failures = 0
                    self._note_contact(resp)  # ss8: the host answered
                    continue
                failures += 1
                if failures >= 5:
                    print(
                        "dsl41: supervisor lease renewal failed 5 times; giving up"
                        " (job outcomes still resolve from the spool)",
                        file=sys.stderr,
                    )
                    return
        except asyncio.CancelledError:
            pass

    async def spawn(self, spec: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request({"cmd": "SPAWN", "token": self.token, "spec": spec})
        if not resp.get("ok"):
            raise SupervisorUnavailable(f"SPAWN refused: {resp}")
        return resp

    async def signal(self, run_id: str, sig: str) -> dict[str, Any]:
        return await self._request(
            {"cmd": "SIGNAL", "token": self.token, "run_id": run_id, "sig": sig}
        )

    async def list_runs(self) -> dict[str, Any]:
        return await self._request({"cmd": "LIST"})

    async def shutdown(self) -> dict[str, Any]:
        return await self._request({"cmd": "SHUTDOWN", "token": self.token})

    async def release(self) -> None:
        if self.token is not None:
            with contextlib.suppress(SupervisorUnavailable):
                await self._request({"cmd": "RELEASE", "token": self.token})

    async def close(self) -> None:
        self._closed = True  # no lazy reconnect may resurrect a closing client
        for task in (self._renew_task, self._reader_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._renew_task = self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None


class SupervisedCommandAdapter:
    """ss6 CMD adapter, detached variant: SPAWN through the supervisor (which
    owns the wrapper lifeline), await the exit push, read status.json --
    the same outcome channel and `_outcome_from_status` mapping as
    LocalCommandAdapter, so tethered and detached runs never diverge. Shares
    run_dir/log/spec construction via `_build_run_spec` (no duplication).

    Cancellation is the subtle part (spec ss3), two distinct cases:
    (a) oracle-decided terminal (KILLJOB / term_run_time / run_window): SIGNAL
        TERM via the supervisor, grace_seconds, SIGNAL KILL, await the exit
        push -- identical outcome shape to the tethered kill.
    (b) engine detach-stop (operator SIGINT/SIGTERM of a --detached run, or
        shutdown for resume): abandon the await, signal NOTHING -- the jobs
        keep running under the supervisor. Branches on ctx.detach.stopping."""

    def __init__(
        self,
        client: SupervisorClient,
        *,
        grace_seconds: float = 10.0,
        settle_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.grace_seconds = grace_seconds
        self.settle_seconds = settle_seconds
        #: (job, run_number) -> run_id for runs to REATTACH at resume, not
        #: respawn (spec ss3); populated by resume before _launch
        self.reattach: dict[tuple[str, int], str] = {}

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        if ctx.run_root is None:
            raise EngineError("SupervisedCommandAdapter needs a run_root (real domain only)")
        key = (job_ir.name, run_number)
        reattach_id = self.reattach.pop(key, None)
        if reattach_id is not None:
            # the run never stopped (its parent is the supervisor); just await
            # its outcome -- no reconciliation injection (E4 dissolved, ss3)
            run_dir = ctx.run_root / "runs" / f"{job_ir.name}.{run_number}"
            run_id = reattach_id
        else:
            run_dir, spec = _build_run_spec(
                job_ir, run_number, ctx, grace_seconds=self.grace_seconds
            )
            run_id = spec["run_id"]
            self.client.exit_future(run_id)  # register BEFORE spawn so no push is missed
            try:
                reply = await self.client.spawn(spec)
            except SupervisorUnavailable as exc:
                self.client.forget_exit(run_id)
                return Failed(f"wrapper spawn failed: supervisor unavailable ({exc})")
            if ctx.journal is not None:
                ctx.journal.dispatch(
                    job_ir.name,
                    run_number,
                    wrapper_pid=reply.get("wrapper_pid"),
                    run_dir=str(run_dir),
                    started_at=ctx.clock.now(),
                )
        # ONE await-and-cancel path for both (DL-96). The reattach branch used
        # to return directly, outside this handler, so a cancellation never
        # reached the kill ladder: KILLJOB against a REATTACHED detached run
        # stopped the adapter task and left the process running. A kill is the
        # one side effect resume is allowed to have, and it was the one the
        # reattached run could not receive.
        try:
            return await self._await_outcome(run_id, run_dir, job_ir.name, run_number)
        except asyncio.CancelledError:
            if ctx.detach is not None and ctx.detach.stopping:
                raise  # ss3 case b: the job continues under the supervisor
            await self.kill(run_id)  # ss3 case a: the oracle decided terminal
            raise

    async def _await_outcome(
        self, run_id: str, run_dir: Path, job: str, run_number: int
    ) -> AdapterResult:
        fut = self.client.exit_future(run_id)
        status_path = run_dir / "status.json"
        try:
            while True:
                # post-poison a lost connection no longer implies a dead
                # supervisor (review fix, DL-48): try to reconnect first --
                # falling straight to the spool ladder would kill a healthy
                # detached run whose wrapper is simply still running
                if self.client.lost.is_set() and not await self.client.reconnect():
                    break  # the supervisor itself is unreachable -> spool below
                status = load_json(status_path)
                if status is not None:
                    return _outcome_from_status(status)
                lost_wait = asyncio.ensure_future(self.client.lost.wait())
                exit_wait = asyncio.ensure_future(asyncio.shield(fut))
                try:
                    await asyncio.wait(
                        {exit_wait, lost_wait},
                        timeout=1.0,  # re-poll status.json (a missed push at reattach)
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    lost_wait.cancel()
                    exit_wait.cancel()  # cancels the shield, never fut
                if fut.done() and not fut.cancelled():
                    status = load_json(status_path)
                    if status is not None:
                        return _outcome_from_status(status)
                    rc = (fut.result() or {}).get("wrapper_rc")
                    return Failed(  # PENDING: E7
                        f"exit_status_unobservable (wrapper exited rc={rc} without a status record)"
                    )
        finally:
            self.client.forget_exit(run_id)
        # the supervisor connection was lost AND could not be re-established
        # (kill -9 of the supervisor): the wrappers EOF'd and are killing+
        # recording -- resolve via the ss7 spool ladder, the same reading the
        # tethered resume path uses (spec ss3)
        result, _ended = await _resolve_spool(
            job,
            run_number,
            run_dir,
            _procid.current_boot_id(),
            settle_seconds=self.settle_seconds,
            grace_seconds=self.grace_seconds,
        )
        return result

    #: DL-83: the spawn window. SPAWN returns once the wrapper is FORKED and
    #: the wrapper writes spawn.json a few syscalls later, so a kill decided in
    #: between is not addressable yet. The supervisor now says `not_ready`
    #: instead of reporting a no-op, and we retry until it becomes addressable,
    #: the run ends, or this bound elapses. Matches the supervisor's own
    #: shutdown wait (DL-48), which fixed this hazard for SHUTDOWN and left the
    #: per-run signal path exposed.
    _SPAWN_WINDOW_S = 5.0
    _SPAWN_POLL_S = 0.02

    async def _signal_when_addressable(
        self, run_id: str, sig: str, fut: "asyncio.Future[dict[str, Any]]"
    ) -> None:
        """SIGNAL, retrying while the supervisor answers `not_ready`. Gives up
        the moment the run reports its exit -- a completed run needs no
        signal -- and after the spawn window, which is loud rather than a
        silent drop."""
        deadline = time.monotonic() + self._SPAWN_WINDOW_S
        while True:
            resp = await self.client.signal(run_id, sig)
            if resp.get("error") != "not_ready":
                return
            if fut.done() or time.monotonic() >= deadline:
                print(
                    f"dsl41: {sig} for run {run_id} never became addressable"
                    " within the spawn window; the wrapper's lifeline remains"
                    " the backstop",
                    file=sys.stderr,
                )
                return
            await asyncio.sleep(self._SPAWN_POLL_S)

    async def kill(self, run_id: str) -> None:
        """ss3 case a: the oracle said terminal. TERM the command group via the
        supervisor, grace, KILL, await the exit push so the wrapper records --
        the cancelled adapter itself never reports (the oracle already emitted
        the terminal).

        Public because resume drives it directly (DL-96): a recorded kill met
        at resume has a live wrapper but no adapter task to cancel, and
        creating one only to cancel it would not work -- a task cancelled
        before its first step never enters the handler that calls this."""
        fut = self.client.exit_future(run_id)
        try:
            await self._signal_when_addressable(run_id, "TERM", fut)
        except SupervisorUnavailable:
            return  # supervisor gone: the wrapper's own lifeline handles it
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(SupervisorUnavailable):
                await self._signal_when_addressable(run_id, "KILL", fut)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
        finally:
            self.client.forget_exit(run_id)


async def _resolve_spool(
    job: str,
    run_number: int,
    run_dir: Path | None,
    boot_now: str,
    *,
    settle_seconds: float,
    grace_seconds: float,
) -> tuple[AdapterResult, datetime | None]:
    """Resolve one incomplete CMD run from its spool directory, walking the
    ss7 ladder top to bottom. Returns (outcome, true ended_at if known)."""
    if run_dir is None or not run_dir.is_dir():
        return Failed("dispatch lost to engine crash (run directory missing)"), None
    status_path = run_dir / "status.json"
    spawn = load_json(run_dir / "spawn.json")
    if spawn is not None and not (
        spawn.get("job") == job and spawn.get("run_number") == run_number
    ):
        spawn = None  # spoofed/corrupt spawn record: never trust, never signal
    status = load_json(status_path)
    if status is None and spawn is not None and spawn.get("boot_id") == boot_now:
        # same boot: liveness checks mean something (DL-42 item 5)
        wrapper_pid = spawn.get("wrapper_pid")
        wrapper_token = spawn.get("wrapper_start_time")
        if (
            isinstance(wrapper_pid, int)
            and isinstance(wrapper_token, str)
            and _procid.verify_alive(wrapper_pid, wrapper_token)
        ):
            # the wrapper is mid-grace (its own parent-loss kill is running):
            # give its status.json a settle window
            deadline = time.monotonic() + settle_seconds + grace_seconds
            while time.monotonic() < deadline:
                status = load_json(status_path)
                if status is not None:
                    break
                if not _procid.verify_alive(wrapper_pid, wrapper_token):
                    status = load_json(status_path)  # one last read after death
                    break
                await asyncio.sleep(0.1)
        if status is None:
            command_pid = spawn.get("command_pid")
            command_pgid = spawn.get("command_pgid")
            command_token = spawn.get("command_start_time")
            if (
                isinstance(command_pid, int)
                and isinstance(command_pgid, int)
                and isinstance(command_token, str)
                and _procid.verify_alive(command_pid, command_token)
            ):
                # command group survived its recorder: kill the verified
                # leader's group -- TERMINATED is truthful (a kill happened)
                _procid.killpg_quiet(command_pgid, signal.SIGTERM)
                deadline = time.monotonic() + grace_seconds
                while time.monotonic() < deadline:
                    if not _procid.verify_alive(command_pid, command_token):
                        break
                    await asyncio.sleep(0.1)
                else:
                    _procid.killpg_quiet(command_pgid, signal.SIGKILL)
                return Terminated("wrapper lost; killed at resume"), None
    if status is not None:
        ended_at = status.get("ended_at")
        return (
            _outcome_from_status(status),
            _naive_utc(ended_at) if isinstance(ended_at, str) else None,
        )
    return Failed("exit_status_unobservable"), None  # PENDING: E7
