"""Runner adapters (ss6/ss6a): the adapter protocol, the CMD wrapper paths and the supervisor
client.

This module belongs to the runner split of DL-74.

Adapter contract (ss6): ``async run(job_ir, run_number, ctx) -> AdapterResult``. An ``int`` is the
RAW exit code (SEM-09/DL-33 classification stays oracle-side), ``Terminated`` reports a kill the
wrapper observed (-> STATUS TERMINATED), and ``Failed`` reports a completion with no raw exit code
(spawn failure, or the E7 unobservable case -> STATUS FAILURE with its cause). Adapters never retry
(n_retrys is unmodeled, DL-53 scope: a shell-side retry would fork semantics from the simulator)
and never time out (term_run_time is the oracle's timer). Under VirtualClock an adapter blocks ONLY
through ctx.clock.sleep_until, which makes quiescence decidable (Engine._settle counts live tasks
against pending sleeps). Real adapters run under RealClock, where the loop blocks on real IO and
_settle is a single reaping pass (DL-43 item 5).

Lifecycle (ss6-ss7; DL-41a and DL-42 pin the semantics): LocalCommandAdapter runs every command
under the ss6a Tier-0 wrapper (runner_wrapper.py, spawned BY FILE PATH), and the wrapper's
status.json is the sole outcome channel; the wrapper's exit only notifies the engine to read it.
Cancel (the oracle said terminal) verifies the recorded (pid, start-time), signals the command
pgid SIGTERM, waits the grace, then SIGKILLs; the wrapper observes and records; the cancelled
adapter never reports.
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
import threading
import time
import uuid

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol, TYPE_CHECKING, get_args

from pydantic import BaseModel, ConfigDict, ValidationError

from dsl41 import runner_procid as _procid
from dsl41 import runner_supervisor as _supervisor
from dsl41 import runner_wrapper as _wrapper
from dsl41.runner_wrapper import WrapperOutcome
from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    CanonError,
    canonical_bytes,
    decode,
    is_scalar_json,
    is_wire_int,
    naive_utc,
)
from dsl41.ir import ExecSpec, FwSpec, JobIR
from dsl41.runner_procid import SPOOL_VERSION, fsync_dir, spool_version_supported, write_all
from dsl41.period import CMD_GRACE_S, FW_DEFAULT_INTERVAL_S, RECONCILE_SETTLE_S, SPAWN_WINDOW_S
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_journal import repair_tail
from dsl41.runner_ledger import Proof
from dsl41.semantics import FwExistence, fw_existence_immediate
from dsl41.state_machine import (
    Context,
    Executor,
    Exclusion,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
)
from dsl41.state_machine import Event as ChartEvent

if TYPE_CHECKING:  # annotation only: the Journal object is the engine's, not an adapter's
    from dsl41.runner_journal import Journal


#: the ss6a Tier-0 shim, executed by file path, never -m (see its docstring)
_WRAPPER_PATH = Path(_wrapper.__file__)
#: the ss6a Tier-1 supervisor, likewise run by file path (stdlib-only boundary)
_SUPERVISOR_PATH = Path(_supervisor.__file__)


def supervisor_argv(run_root: Path, deadman_s: float | None = None) -> list[str]:
    """The launch rule for a Tier-1 supervisor: by file path, never `-m` (DL-42), with the
    optional deadman.

    DL-210 has two spawners, this module's engine-side one and `supervise start`, and one rule
    (DL-75). The caller decides what `run_root` spells; `supervise start` resolves it first."""
    argv = [sys.executable, str(_SUPERVISOR_PATH), "--run-root", str(run_root)]
    if deadman_s is not None:
        argv += ["--deadman-seconds", str(deadman_s)]
    return argv


def supervisor_log_path(run_root: Path) -> Path:
    """Where a spawner points the supervisor's stderr; the supervisor opens no log file
    (docs/supervisor-protocol.md ss5)."""
    return run_root / "supervisor.log"


# JSON-lines buffer cap for every asyncio stream endpoint (control socket both sides, supervisor
# client). One `status` response is one line covering EVERY job (~220 bytes each), so asyncio's
# 64 KiB default readline() limit overruns at ~300 jobs; 16 MiB clears any plausible estate and
# still bounds a runaway peer. Public because three modules need it (this one, runner_control's
# server AND client) (DL-78).
LINE_LIMIT: int = 2**24
#: spawns `ensure_running` makes inside its 10 s window when a fresh supervisor exits before
#: publishing its socket (DL-210: exit 1 is retryable)
_SPAWN_ATTEMPTS = 3


@dataclass
class DetachSignal:
    """Set by the engine BEFORE it cancels adapter tasks for a detach-stop (operator SIGINT/SIGTERM
    of a --detached run, or shutdown-for-resume). The SupervisedCommandAdapter's CancelledError
    path branches on it: when stopping, it abandons the await and signals NOTHING, so jobs keep
    running under the supervisor (spec ss3 case b). Tethered adapters ignore it."""

    stopping: bool = False


@dataclass
class SealBarrier:
    """ss6 step 2's park, seen from an adapter (period-model ss3.5, DL-133).

    `parked` is the one record of the freeze: the engine's admission reads it as `Engine.sealing`.
    The engine sets it before it chooses T; every FW task checks it at its POLL BOUNDARY, after
    the sleep and before the observation, and waits there. A poll's observation, its `watch.jsonl`
    append and its progress update contain no `await`, so no poll is in flight across a yield, and
    a task still asleep cannot append before it wakes into this check. Without the park a second
    qualifying poll would land after the snapshot, its completion would never be admitted, and
    audit would derive a completed watch where the seal carries a live one.

    `parked_tasks` tells the engine's virtual-domain settle that a task neither running nor asleep
    on the clock is going nowhere."""

    parked: bool = False
    parked_tasks: int = 0
    _released: "asyncio.Event | None" = None

    def park(self) -> None:
        self.parked = True
        self._released = None

    def release(self) -> None:
        """`abort_boundary`'s half: the interval is reversible until the `seal` append begins, and
        after an abort an FW poll proceeds (PR-28b)."""
        self.parked = False
        if self._released is not None:
            self._released.set()

    async def wait(self) -> None:
        if not self.parked:
            return
        if self._released is None:
            self._released = asyncio.Event()
        self.parked_tasks += 1
        try:
            await self._released.wait()
        finally:
            self.parked_tasks -= 1


@dataclass
class AdapterContext:
    """What an adapter may touch (ss6): the clock, and in the real domain the run-root layout
    (runs/, logs/) plus the WAL for dispatch records. `detach` distinguishes a detach-stop from an
    oracle-decided kill in the detached CMD path (ss3 case b vs a). `run_id` is the identity the
    decision minted for this spawn (DL-118, PR-36a); the wrapper spec carries it onward, so the
    WAL, the spool and every later reader name ONE key. It is None on the paths with no effect
    behind them (FW re-dispatch, detached reattach) and for a SPAWN re-driven from a pre-DL-118
    journal, where the adapter mints its own."""

    clock: Clock
    run_root: Path | None = None
    journal: Journal | None = None
    detach: DetachSignal | None = None
    run_id: str | None = None
    #: ss6 step 2's park; None for adapters that own no durable position
    barrier: SealBarrier | None = None
    #: period-model ss1.3: the fence lives in the journal writer, and the FW adapter appends to a
    #: spool the journal never sees. An append after leadership was lost is evidence written by a
    #: non-leader, so this adapter re-proves before every line (PR-03).
    fence: Proof | None = None


@dataclass(frozen=True)
class Terminated:
    """The command was killed and the kill was OBSERVED (wrapper status.json: signaled /
    terminated). The engine injects STATUS TERMINATED, reserved for kills that happened
    (DL-41a item 7)."""

    cause: str


@dataclass(frozen=True)
class Failed:
    """A completion with no raw exit code: spawn failure, or the E7 unobservable case. The engine
    injects STATUS FAILURE with the cause, never anything that could satisfy a success-dependent
    downstream."""

    cause: str


#: every `status.json` outcome the wrapper can write, derived from its own Literal
WRAPPER_OUTCOMES: Final[frozenset[str]] = frozenset(get_args(WrapperOutcome))

#: int = RAW exit code (SEM-09/DL-33 verdict stays oracle-side)
AdapterResult = int | Terminated | Failed


class JobAdapter(Protocol):
    """ss6 adapter protocol; the module docstring carries the contract."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult: ...


class FakeAdapter:
    """ss6: scripted ``(job, run_number) -> (duration_s, exit_code)``; default instant success.

    A ``None`` entry (per run in the script, per job via ``park``, or estate-wide via
    ``default=None``) makes the run INERT: the task parks on a sleep at datetime.max, which no
    real horizon reaches, so the SCRIPT drives completions via injected STATUS. The bisimulation
    harness runs the estate-wide mode; rehearse scenarios use scripted entries and ``park`` for
    jobs, file watchers above all, that must not fire. ``job_default`` is a per-job completion
    used when no script entry matches and the job is not parked: the cadence check synthesizes
    each job's exit from its own SEM-09 boundary there, because the global exit-0 default is a
    FAILURE under a ``fail_codes`` that lists 0 (DL-184). Precedence: script entry, park,
    job_default, default."""

    def __init__(
        self,
        script: Mapping[tuple[str, int], tuple[float, int] | None] | None = None,
        *,
        default: tuple[float, int] | None = (0.0, 0),
        park: Collection[str] = (),
        job_default: Mapping[str, tuple[float, int] | None] | None = None,
    ) -> None:
        self.script = dict(script or {})
        self.default = default
        self.park = frozenset(park)
        self.job_default = dict(job_default or {})

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int:
        key = (job_ir.name, run_number)
        if key in self.script:
            entry = self.script[key]
        elif job_ir.name in self.park:
            entry = None
        elif job_ir.name in self.job_default:
            entry = self.job_default[job_ir.name]
        else:
            entry = self.default
        if entry is None:
            await ctx.clock.sleep_until(datetime.max)
            raise AssertionError("inert park elapsed: horizon reached datetime.max")
        duration_s, exit_code = entry
        await ctx.clock.sleep_until(ctx.clock.now() + timedelta(seconds=duration_s))
        return exit_code


def load_json(path: Path) -> dict[str, Any] | None:
    """Tolerant spool read: missing or unparseable -> None (an unreadable record is never trusted
    for signaling; the ladder falls through).

    A record carrying an unpaired surrogate is unreadable (PR-10a): it decodes, but nothing
    downstream can canonicalize it. So are bytes that are not UTF-8 (DL-151); they are decoded as
    strict UTF-8 before parsing, because `json.load` on a binary handle accepts UTF-16 and UTF-32
    (DL-229).

    A `version` this binary does not implement is unreadable too: the `Wrapper-owned spool files`
    row of docs/protocol-evolution.md is tolerant on fields and STRICT on versions. None is this
    reader's whole refusal vocabulary, and every caller's next step is safe: an unread
    `status.json` becomes `exit_status_unobservable`, never a success, and an unread `spawn.json`
    is a group this engine will not signal."""
    try:
        with path.open("rb") as f:
            loaded = json.loads(f.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(loaded, dict) or not is_scalar_json(loaded):
        return None
    if not spool_version_supported(loaded):
        return None
    return loaded


def outcome_from_status(status: dict[str, Any]) -> AdapterResult:
    """Map a wrapper status.json record (docs/supervisor-protocol.md ss3) to an adapter result.
    Shared by the live adapter path and reconciliation, so live and resumed runs agree on the same
    record. A malformed record maps to FAILURE with a truthful cause, never to anything that could
    satisfy a success-dependent downstream."""
    outcome = status.get("outcome")
    if not isinstance(outcome, str) or outcome not in WRAPPER_OUTCOMES:
        # the vocabulary is the wrapper's (DL-209); an outcome outside it is refused. The
        # isinstance guard is load-bearing: `outcome` comes from a decoded status.json, where it
        # can be a list or a dict, and a set membership test on an unhashable value raises
        return Failed(f"unrecognized status record outcome {outcome!r}")
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
        # PENDING: E8 -- an EXTERNAL signal death (engine alive, no oracle kill decision) maps
        # to TERMINATED per the DL-41a recorded-signal reading. The vendor's status definition
        # names the UNIX kill command among what makes a job TERMINATED (DL-226). Open: recorded
        # intent versus wait status (the trap-TERM live test), and signal deaths no operator
        # sent (segfault, OOM kill).
        return Terminated(cause)
    if outcome == "terminated":
        return Terminated(str(status.get("cause", "terminated")))
    return Failed(f"spawn failed: {status.get('error')}")  # the fourth outcome


def status_payload(result: AdapterResult, *, where: str) -> dict[str, object]:
    """One adapter result as the STATUS input's payload fields (ss4).

    The live path and the resume ladder share this one body, so an addition to `AdapterResult`
    reaches both surfaces or neither (DL-145): an unrecognised result is refused, never turned
    into a FAILURE that fabricates a fate for a run nobody read.

    `where` is what the caller puts before "returned"; the two callers hold different halves of
    the run's name."""
    if isinstance(result, Terminated):
        # a kill that was observed to happen (DL-41a item 7)
        return {"status": "TERMINATED", "cause": result.cause}
    if isinstance(result, Failed):
        return {"status": "FAILURE", "cause": result.cause}
    if isinstance(result, int):
        # raw exit code only: the SEM-09/DL-33 verdict stays oracle-side
        return {"exit_code": result}
    raise EngineError(f"{where} returned {result!r}")


def _build_run_spec(
    job_ir: JobIR,
    run_number: int,
    ctx: AdapterContext,
    *,
    grace_seconds: float,
    create_run_dir: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """The run_dir, log-path and wrapper-spec construction shared by the tethered
    (LocalCommandAdapter) and detached (SupervisedCommandAdapter) CMD paths: everything the wrapper
    input spec needs EXCEPT lifeline_fd, which each caller fills from the end that owns the pipe's
    write side. One function keeps the run-dir layout, profile composition and log targets of the
    two paths identical (ss6a).

    `create_run_dir=False` is the DETACHED path (period-model ss11a, DL-129): the run directory is
    the SPAWN tombstone, and the supervisor creates it on receipt. An engine that created it
    first could die before sending and leave the retry's supervisor reading "directory exists, no
    receipt" (indeterminate) for a run that never reached the host. The tethered path keeps
    engine ownership, since no supervisor is in it."""
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
    if create_run_dir:
        run_dir.mkdir(parents=True)  # a collision is a bug: run_numbers never repeat
        fsync_dir(run_dir)
        fsync_dir(run_dir.parent)  # the runs dir is fsync'd at creation
    (ctx.run_root / "logs").mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = job_log_paths(job_ir, run_number, ctx.run_root)
    spec = {
        "version": SPOOL_VERSION,
        # the decision's mint when there is one (PR-36a); the local fallback covers only the
        # effect-less and pre-DL-118 paths (AdapterContext). `is None`, not falsy: an empty-string
        # id, which the journal gates refuse, must fail loudly downstream
        "run_id": ctx.run_id if ctx.run_id is not None else str(uuid.uuid4()),
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
    """ss6 CMD adapter: spawn the ss6a Tier-0 wrapper, await it, read status.json, the sole
    outcome channel. No retries (n_retrys unmodeled, DL-53 scope), no timeouts (term_run_time is
    the oracle's timer), no classification.

    stdout/stderr APPEND to std_out_file/std_err_file when set (vendor appends), else to
    <run_root>/logs/<job>.<run_number>.{out,err}; std_in_file when set, else /dev/null. `profile`
    sources first: ``. <profile> && <command>``, and a failing profile fails the job with sh's
    exit code (PENDING: E5). DL-39 [?] verbatim carry applies: the command string is passed to
    /bin/sh exactly as the IR holds it.

    Cancellation (the oracle said terminal): verify the recorded command (pid, start-time),
    SIGTERM the command pgid, grace, SIGKILL; the wrapper observes the deaths and records the
    outcome durably; the cancelled adapter never reports. A cancel before the spec reaches the
    wrapper SIGKILLs the wrapper, which has launched nothing. The lifeline write end lives in this
    process ONLY and is closed in a finally: engine death EOFs every wrapper (tethered semantics,
    ss6a)."""

    def __init__(self, *, grace_seconds: float = CMD_GRACE_S) -> None:
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
                # EMFILE/ENOMEM-class glitch: fail THIS job, not the engine (as the wrapper's own
                # spawn_failed does)
                return Failed(f"wrapper spawn failed: {exc}")
            finally:
                os.close(lifeline_r)  # our copy; the wrapper holds its own now
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(spec).encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except OSError as exc:
                # the wrapper died while reading its spec, pre-spawn by construction: it spawns
                # only after the full spec parses
                await proc.wait()
                return Failed(f"wrapper spawn failed: {exc}")
            except asyncio.CancelledError:
                await self._stop_unlaunched(proc)
                raise
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
                await self._kill(run_dir, proc, str(spec["run_id"]))
                raise
            status = load_json(run_dir / "status.json")
            if status is None:
                # the recorder exited without a record (rc 2/3: spec error, ENOSPC): report that
                # observability is gone, never guess
                return Failed(  # PENDING: E7
                    f"exit_status_unobservable (wrapper exited rc={proc.returncode}"
                    " without a status record)"
                )
            # the exclusive mkdir owned the directory at CREATION; the fate file is written
            # later, so it is held to the run's identity like every other status consumption
            # (DL-118)
            return outcome_from_status(_named(status, str(spec["run_id"]), job_ir.name, run_number))
        finally:
            os.close(lifeline_w)

    @staticmethod
    async def _stop_unlaunched(proc: asyncio.subprocess.Process) -> None:
        """A kill that lands before the spec reached the wrapper (DL-359): SIGKILL the wrapper so
        it never launches the command.

        The wrapper launches only after reading its spec to EOF, and EOF needs the stdin close this
        path has not reached, so it has launched nothing and has nothing to record. A cancel inside
        `create_subprocess_exec` gets the same treatment from asyncio, and one before the task's
        first step spawns nothing."""
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

    async def _kill(self, run_dir: Path, proc: asyncio.subprocess.Process, run_id: str) -> None:
        """The oracle decided terminal: signal the command pgid (never the wrapper, the recorder
        is untouchable), escalate after grace, then wait for the wrapper to record and exit.

        The record that NAMES the group to signal is held to this run's identity first (DL-118):
        the pid-reuse token proves the process the record describes is alive, not that it is
        OURS, and a spoofed record with a live foreign token would aim the kill at a stranger's
        process group."""
        if proc.stdin is not None:
            proc.stdin.close()  # a wrapper still reading its spec must not hang
        spawn = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            spawn = load_json(run_dir / "spawn.json")
            if spawn is not None or proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        if spawn is not None and not spool_names_run(spawn, run_id=run_id):
            raise EngineError(
                f"{run_dir.name}: spawn.json reports run_id {spawn.get('run_id')!r} but"
                f" this run is {run_id!r} -- refusing to signal a stranger's process"
                " group (DL-118)"
            )
        if spawn is None:
            # reachable when the wrapper died or is frozen pre-record (test pauses); the lifeline
            # tether covers the residue, so wait bounded, then leave the wrapper to its own record
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


def default_log_paths(job: str, run_number: int, run_root: Path) -> tuple[Path, Path]:
    """Where a CMD run's output goes when the job names no file of its own:
    `<run_root>/logs/<job>.<run_number>.{out,err}`.

    It is separate from `job_log_paths` because `retention.py` needs the CONVENTION without a
    `JobIR`, and one spelling keeps the two readers agreeing (DL-135)."""
    logs_dir = run_root / "logs"
    return (logs_dir / f"{job}.{run_number}.out", logs_dir / f"{job}.{run_number}.err")


def job_log_paths(job_ir: JobIR, run_number: int, run_root: Path) -> tuple[str, str]:
    """ss6 append targets for a CMD run: std_out_file/std_err_file when set (vendor appends), else
    <run_root>/logs/<job>.<run_number>.{out,err}. One resolver serves the adapter's wrapper spec
    and the ss10 status response, because the ss11 log tail reads what the wrapper writes."""
    out = err = None
    if isinstance(job_ir.exec_, ExecSpec):
        out, err = job_ir.exec_.std_out_file, job_ir.exec_.std_err_file
    fallback_out, fallback_err = default_log_paths(job_ir.name, run_number, run_root)
    return (out or str(fallback_out), err or str(fallback_err))


#: the FW adapter's append-only spool, one line per poll (period-model ss3.5)
WATCH_LOG = "watch.jsonl"

#: ss6's steady-size rule as a number: two consecutive qualifying polls at the same size complete
#: the watch (`fw-existence=stable`, DL-258). The spool records the count, so live and resume read
#: the rule the same way.
FW_STABLE_POLLS = 2


@dataclass(frozen=True)
class WatchLog:
    """`watch.jsonl` folded (period-model ss3.5): what a watch has observed, derived from the log,
    not from a live adapter's memory.

    The fold is a pure function of a PREFIX of the log, and `watch_seq` names the prefix; `at <= T`
    is not a unique log position, so an audit replaying the START input reads the same progress
    (`previous_size=10`, `null`, or a completed watch) the seal carries."""

    run_id: str | None
    start_at: datetime
    #: the last poll's line, or None when only the `start` line is durable
    last_at: datetime | None
    size: int | None
    qualifying: bool
    stable_polls: int
    #: the count of durable lines (period-model ss3.5's `watch_seq`)
    watch_seq: int

    @property
    def complete(self) -> bool:
        """The last durable line is a COMPLETING observation: the watch is over, whether or not the
        engine lived to say so (PR-34a)."""
        return self.stable_polls >= FW_STABLE_POLLS

    def next_poll_at(self, interval_s: int) -> datetime:
        """period-model ss3.5: after `start` and no poll line, `start.at` (the first poll is
        immediate); after a poll line, `poll.at + interval`."""
        if self.last_at is None:
            return self.start_at
        return self.last_at + timedelta(seconds=interval_s)


def read_watch_log(run_dir: Path, *, prefix: int | None = None) -> WatchLog | None:
    """Fold a run directory's `watch.jsonl`, or None when the watch has not been dispatched (no
    file, or no `start` line yet).

    A torn FINAL line is dropped, as `read_journal` drops one: the append is write-ahead, so the
    progress it would have recorded never happened. A torn INTERIOR line is corruption and raises.

    `prefix` folds only the first that many durable lines: audit re-derives a CLOSED period whose
    live watch carried across the boundary, and the successor's later polls in the same file are
    ITS evidence. The cut is POSITIONAL, not wall-time: ss3.5 says an instant is not a unique log
    position, and a successor unparked at the boundary can poll at exactly T. audit owns the
    count's authority (attest.py: the successor segment's `opens_from_seal` pins the sidecar the
    count is read from). Lines past the prefix are not read at all, so the successor's evidence,
    an unsupported version a future binary wrote included, cannot make a closed period
    unauditable. A prefix the log cannot supply refuses: the claimed evidence does not exist."""
    path = run_dir / WATCH_LOG
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # EACCES/EIO is a log that EXISTS and cannot be read; "undispatched" would re-dispatch a
        # live watch over unreadable evidence
        raise EngineError(f"{path}: unreadable: {exc}") from exc
    lines = raw.split(b"\n")
    trailing = lines.pop() if lines and lines[-1] == b"" else None
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if prefix is not None and len(records) == prefix:
            break  # everything further is the successor's, and is not read
        if not line:
            raise EngineError(f"{path}: empty interior line {index + 1}")
        if index == len(lines) - 1 and trailing is None and not _is_json(line):
            break  # torn final append: the poll it recorded never landed
        try:
            record = decode(line)
        except CanonError as exc:
            # a COMPLETE line the ss3.2 ingress refuses (an unimplemented artifact_format_version
            # (PR-08d), a lone surrogate) is not a torn one; dropping it would delete evidence a
            # future binary wrote, so it refuses loudly wherever it sits
            raise EngineError(f"{path}: line {index + 1}: {exc}") from exc
        if not isinstance(record, dict):
            raise EngineError(f"{path}: line {index + 1} is not an object")
        records.append(record)
    if not records:
        return None
    if prefix is not None and len(records) < prefix:
        raise EngineError(
            f"{path}: a prefix of {prefix} line(s) was claimed and the log holds"
            f" {len(records)} -- the claimed evidence does not exist (period-model ss3.5)"
        )
    for position, record in enumerate(records, start=1):
        # required, not implemented-if-present: `decode` passes an absent version, and an
        # unversioned line is unsupported evidence (PR-08d)
        if record.get("artifact_format_version") != ARTIFACT_FORMAT_VERSION:
            raise EngineError(
                f"{path}: line {position} carries artifact_format_version"
                f" {record.get('artifact_format_version')!r}"
            )
    start = records[0]
    if start.get("kind") != "start":
        # a log whose first line is not `start` was not written by this adapter: period-model
        # ss3.5 makes the start line a dispatched watch's first durable act, so anything else is
        # corruption or a foreign writer. It raises, since None means "not dispatched" and would
        # re-dispatch over a stranger's log
        raise EngineError(f"{path}: first line is {start.get('kind')!r}, not 'start'")
    run_id = start.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise EngineError(f"{path}: start line run_id is not a string or null")
    last: dict[str, Any] | None = None
    derived_stable = 0
    previous_size: int | None = None
    for position, record in enumerate(records[1:], start=2):
        # every later line is a poll of THIS watch, in full: a line of an unknown kind, a foreign
        # run_id or a malformed field is evidence the fold cannot hold, and a poll naming another
        # run never completes this one (DL-118)
        if record.get("kind") != "poll":
            raise EngineError(f"{path}: line {position} kind {record.get('kind')!r}")
        if record.get("run_id") != run_id:
            raise EngineError(
                f"{path}: line {position} names run_id {record.get('run_id')!r}"
                f" but the start line named {run_id!r}"
            )
        size = record.get("size")
        if not (size is None or is_wire_int(size)):
            raise EngineError(f"{path}: line {position} size {size!r}")
        qualifying = record.get("qualifying")
        if not isinstance(qualifying, bool):
            raise EngineError(f"{path}: line {position} qualifying is not a boolean")
        exists = record.get("exists")
        if not isinstance(exists, bool):
            raise EngineError(f"{path}: line {position} exists is not a boolean")
        # the writer's own invariants, enforced on read: an observation has no size without a
        # file and does not qualify without a size; otherwise two `qualifying: true, size: null`
        # lines derive a stable pair (None == None) and inject a SUCCESS for a file never there
        if (size is None) != (not exists):
            raise EngineError(f"{path}: line {position}: exists {exists!r} with size {size!r}")
        if qualifying and size is None:
            raise EngineError(f"{path}: line {position}: qualifying with no size")
        # the completion state is DERIVED from the observations and the recorded count is checked
        # against it: a forged `qualifying: false, stable_polls: 2` line would otherwise inject a
        # SUCCESS from an observation that observed nothing (period-model ss3.5: the fold is a
        # pure function of the log, so a count the observations cannot produce is a log that
        # disagrees with itself)
        derived_stable = (
            (derived_stable + 1) if qualifying and size == previous_size else int(qualifying)
        )
        previous_size = size if qualifying else None
        if record.get("stable_polls") != derived_stable:
            raise EngineError(
                f"{path}: line {position} records stable_polls"
                f" {record.get('stable_polls')!r} but the observations derive"
                f" {derived_stable}"
            )
        last = record
    return WatchLog(
        run_id=run_id,
        start_at=_watch_at(path, start),
        last_at=_watch_at(path, last) if last is not None else None,
        size=last.get("size") if last is not None else None,
        qualifying=bool(last.get("qualifying")) if last is not None else False,
        stable_polls=int(last.get("stable_polls", 0)) if last is not None else 0,
        watch_seq=len(records),
    )


def _is_json(line: bytes) -> bool:
    """Whether `line` is syntactically complete. Tornness is a SYNTAX question: a line the ss3.2
    ingress refuses for its content is a whole line, and treating it as torn would truncate it."""
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def _watch_at(path: Path, record: dict[str, Any]) -> datetime:
    # No naive_utc normalization: every "at" this reads was written by append_watch_line from
    # ctx.clock.now(), which is naive in both time domains; this is the one reader of the three
    # that never crosses the wire's aware-UTC ISO.
    at = record.get("at")
    if not isinstance(at, str):
        raise EngineError(f"{path}: a {record.get('kind')!r} line has no timestamp")
    return datetime.fromisoformat(at)


def append_watch_line(path: Path, record: dict[str, Any]) -> None:
    """One ss3.2-canonical line, fsynced (period-model ss3.5). Append-only, so a torn tail is
    truncated on the next open instead of made atomic by rename. 0600 at creation, like every
    other spool file (DL-66)."""
    data = canonical_bytes(record) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class FileWatcherAdapter:
    """ss6 FW adapter: poll every watch_interval seconds (default 60 [?] PENDING: E6) until
    watch_file exists with size >= watch_file_min_size (unset -> 0) and the size is stable
    across two consecutive qualifying polls -- `fw-existence=stable`, the default and dsl41's
    own choice (DL-258). With no minimum size, `fw-existence=immediate` completes only when
    the run's FIRST poll finds the file -- the vendor reading; a file that appears later waits
    for a steady size, as under `stable`. A job with a minimum size always needs the
    steady-size rule, whatever the switch says. Completes with exit 0. Clock-driven
    (ctx.clock.sleep_until), so the same code runs in both time domains; polling is an
    idempotent read, which is why resume may re-dispatch an incomplete watch.

    Its progress is EVIDENCE, not memory (period-model ss3.5, DL-129). The first durable record
    on dispatch is a `start` line in `runs/<job>.<run_number>/watch.jsonl`, then one fsynced line
    per poll, INCLUDING polls that changed nothing. Each line is appended before progress is
    updated or completion is emitted, so an audit sees every observation that moved the watch. A
    resumed watch reconstructs from the log and appends no second `start`. The run directory is
    made and its parent fsynced before the `start` line; a crash between the two leaves no
    `start` line, and resume dispatches the watch again under its bound run_id. Without a
    run_root there is no spool and the watch is memory-only (the virtual-domain harness)."""

    def __init__(
        self,
        *,
        default_interval_s: int = int(FW_DEFAULT_INTERVAL_S),
        existence: FwExistence = "stable",
    ) -> None:
        self.default_interval_s = default_interval_s  # PENDING: E6
        self.existence = existence  # DL-258: the `fw-existence` switch, pinned per period

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        spec_ir = job_ir.exec_
        if not isinstance(spec_ir, FwSpec):
            raise EngineError(f"{job_ir.name!r}: FW dispatch without an FwSpec")
        interval = spec_ir.watch_interval or self.default_interval_s
        min_size = spec_ir.watch_file_min_size or 0
        # DL-258: with no minimum size, `immediate` completes when the RUN'S FIRST poll already
        # qualifies (the vendor's narrow carve-out: "the file already exists when the job
        # runs"). Once the first poll has passed without qualifying, the general rule applies
        # even under `immediate`: the size must reach the minimum and stay steady, the
        # FW_STABLE_POLLS rule `stable` uses. A job with a minimum size always needs that rule.
        immediate = fw_existence_immediate(job_ir, self.existence)
        first_poll_pending = True
        previous: int | None = None
        stable = 0
        run_id = ctx.run_id
        log_path: Path | None = None
        next_at = ctx.clock.now()
        if ctx.run_root is not None:
            run_dir = ctx.run_root / "runs" / f"{job_ir.name}.{run_number}"
            run_dir.mkdir(parents=True, exist_ok=True)
            fsync_dir(run_dir.parent)
            log_path = run_dir / WATCH_LOG
            # the watch log follows the WAL's own tail-repair rule (period-model ss3.5):
            # make it end on a line boundary before anything appends
            repair_tail(log_path)
            log = read_watch_log(run_dir)
            if log is None:
                if ctx.fence is not None:
                    ctx.fence.check()  # the dispatch line is an append too
                append_watch_line(
                    log_path,
                    {
                        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                        "at": next_at,
                        "kind": "start",
                        "run_id": run_id,
                    },
                )
                fsync_dir(run_dir)  # the log's own directory entry is a record
            else:
                # RESUMED: the log is the watch. Its `start` line is the dispatch (ss11's ladder
                # resolves the SPAWN by it), so no second one is appended and the run keeps the
                # identity it was dispatched under, which must BE this launch's (DL-118): a log
                # that appeared since the preflight and names another run is a stranger's, and
                # adopting it would poll a watch the WAL never dispatched.
                if run_id is not None and log.run_id != run_id:
                    # exact, None included: a bound launch met a log, so the log was written
                    # under this protocol and must name the run; an idless one is foreign and
                    # adopting it consumes a fate the WAL never dispatched
                    raise EngineError(
                        f"{job_ir.name}.{run_number}: watch.jsonl names run_id"
                        f" {log.run_id!r} but this run is {run_id!r} -- refusing to"
                        " adopt a stranger's watch (DL-118)"
                    )
                run_id = log.run_id
                previous = log.size if log.qualifying else None
                stable = log.stable_polls
                next_at = log.next_poll_at(interval)
                # `watch_seq` counts the start line plus every poll line, so `== 2` is exactly one
                # poll recorded: the run's first, from which `immediate` may complete (DL-258)
                first_poll_pending = log.watch_seq == 1
                if log.complete or (immediate and log.watch_seq == 2 and log.qualifying):
                    # PR-34a, the FALLBACK reading: the PRIMARY reader is the resume ladder
                    # (`runner_startup._resume_watch`), which meets the same log earlier and
                    # INJECTS the completion without launching anything. This branch answers
                    # when a launch happened anyway (an embedder's re-dispatch, or a watch the
                    # ladder handed to the adapter) and reads completeness the same way (DL-258
                    # included), because re-polling would decide the watch against a moved world.
                    return 0
        while True:
            await ctx.clock.sleep_until(next_at)
            if ctx.barrier is not None:
                # THE poll boundary (ss6 step 2), before the observation: a parked watch owes the
                # log nothing and resumes in C2 from exactly the line the seal counted.
                await ctx.barrier.wait()
            at = ctx.clock.now()
            try:
                size: int | None = os.stat(spec_ir.watch_file).st_size
            except OSError:
                size = None
            qualifying = size is not None and size >= min_size
            stable = (stable + 1) if qualifying and size == previous else int(qualifying)
            if log_path is not None:
                # observe -> RE-CHECK THE FENCE -> append -> move progress. Write-ahead per poll:
                # an observation that changed the watch before it was durable is one audit cannot
                # see, and a line appended after leadership was lost is evidence written by a
                # non-leader (period-model ss3.5, PR-03).
                if ctx.fence is not None:
                    ctx.fence.check()
                append_watch_line(
                    log_path,
                    {
                        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                        "at": at,
                        "exists": size is not None,
                        "kind": "poll",
                        "qualifying": qualifying,
                        "run_id": run_id,
                        "size": size,
                        "stable_polls": stable,
                    },
                )
            previous = size if qualifying else None
            if stable >= FW_STABLE_POLLS or (immediate and first_poll_pending and qualifying):
                return 0
            first_poll_pending = False
            next_at = at + timedelta(seconds=interval)


#: SECONDS between LIST re-checks, in both nets and in no other unit: the per-wait net spends a
#: budget of seconds, so a change to its poll cadence leaves the other net's cadence unmoved
#: (DL-75).
_LIST_RECHECK_EVERY = 5

#: how long `_await_outcome` waits on one pass before re-reading status.json (a push missed at
#: reattach); a test shortens it to check that `_LIST_RECHECK_EVERY` still means seconds
_OUTCOME_POLL_S = 1.0


class SupervisorUnavailable(RuntimeError):
    """The supervisor socket is gone (never came up, refused, or died mid-run). The engine
    survives it: pending runs resolve from the spool ladder (spec ss3), the tethered fallback for
    the detached path."""


class SpawnInProgress(SupervisorUnavailable):
    """ss11a's `in_progress`: this SPAWN's wrapper is forked and has not recorded yet, so the
    supervisor gives no answer. It is NOT a failure: the run is alive, and reporting a completion
    for a running process is what the outcome channel may never do. A subclass of
    SupervisorUnavailable, so every `except SupervisorUnavailable` covers it and only a caller
    that can wait treats it differently."""


class SupervisorConn:
    """Blocking one-shot client of the same ss6a socket, for callers with no event loop (the
    `dsl41 supervise` verb). Async exit PUSHes are skipped: this end is not a data-channel
    consumer (supervisor-protocol ss5 makes them droppable notifications).

    It sits beside `SupervisorClient` as `roundtrip` sits beside `ControlClient` (DL-78, DL-91):
    two transports for ONE protocol. The framing rule is stamp `"v": 1` AND the incarnation, read
    newline-delimited JSON, drop pushes; a frozen document owns it."""

    def __init__(self, sock_path: Path, *, timeout_s: float = 60.0) -> None:
        self.conn = socket.socket(socket.AF_UNIX)
        try:
            # SHUTDOWN replies only AFTER waiting for wrappers (frozen ss5 order), which spans the
            # spawn-record wait plus per-run grace windows
            self.conn.settimeout(timeout_s)
            self.conn.connect(str(sock_path))
        except BaseException:
            self.conn.close()  # the caller never gets an object to close
            raise
        self.buf = b""
        #: DL-80: the incarnation rides every mutating verb beside the token (supervisor-protocol
        #: ss5). It is part of the envelope, stamped here as `SupervisorClient._request` stamps
        #: it; an unstamped SHUTDOWN answers `wrong_incarnation` and stops nothing (DL-151).
        self.incarnation: str | None = None

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.conn.sendall(
            json.dumps({**request, "v": 1, "incarnation": self.incarnation}).encode("utf-8") + b"\n"
        )
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
            if isinstance(obj, dict):
                self._learn_incarnation(obj)
                return obj
            # supervisor-protocol ss5 sends only JSON objects; the contract's "cannot happen" is
            # loud, not degraded into an empty success-shaped reply (DL-220)
            raise EngineError(
                f"the supervisor sent a non-object reply: {obj!r} (supervisor-protocol ss5)"
            )

    def _learn_incarnation(self, reply: dict[str, Any]) -> None:
        """The supervisor names its own incarnation in every reply that carries one (PING, LIST,
        ACQUIRE); the client reads it back, as `SupervisorClient` does off ACQUIRE. A REFUSAL is
        not a source: its `incarnation` field tells a stale client what the world is now, and
        adopting it would let the client retry a refused verb with a token from a vanished
        world."""
        value = reply.get("incarnation")
        if isinstance(value, str) and reply.get("error") is None:
            self.incarnation = value

    def close(self) -> None:
        self.conn.close()


#: how long a physical roll waits for a supervisor already serving its target to answer one
#: PING; a PING answers at once, so a supervisor silent this long is stopped or wedged
PING_TIMEOUT_S = 5.0


def running_supervisor_deadman(run_root: Path) -> tuple[bool, float | None]:
    """Whether a supervisor already serves `run_root`, and the deadman it reports. Read-only: one
    PING, no lease, never a spawn. `(False, None)` when nothing listens on the socket.

    A physical roll asks this before it writes, because a supervisor already up keeps its own
    interval (period-model PR-22b). One that accepts and does not answer within `PING_TIMEOUT_S`
    refuses the roll: its deadman is unknown, and so is the one the run would keep."""
    sock_path = run_root / "supervisor.sock"
    try:
        conn = SupervisorConn(sock_path, timeout_s=PING_TIMEOUT_S)
    except (FileNotFoundError, ConnectionRefusedError):
        return False, None
    except OSError as exc:
        raise EngineError(f"{run_root}: cannot reach the supervisor at {sock_path}: {exc}") from exc
    try:
        reply = conn.send({"cmd": "PING"})
    except (OSError, ValueError) as exc:
        raise EngineError(
            f"{run_root}: the supervisor at {sock_path} did not answer PING within"
            f" {PING_TIMEOUT_S}s ({exc}). A roll needs its deadman before it writes:"
            " resume or stop that supervisor, then retry"
        ) from exc
    finally:
        conn.close()
    reported = reply.get("deadman_s")
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        return True, float(reported)
    return True, None


class SupervisorRunRow(BaseModel):
    """One row of a LIST reply's `runs` array (supervisor-protocol ss5).

    Parsed ONCE, inside `SupervisorClient.list_runs`, for every reader (DL-137, DL-220), at the
    bar `Engine._supervisor_proof` sets (PR-27): every field the wire always sends is required,
    and `strict=True` blocks numeric-string and float coercions. Unknown fields are ignored, per
    the wire's rule (supervisor-protocol ss5; "Supervisor socket" row of
    docs/protocol-evolution.md)."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    run_id: str
    job: str
    run_number: int
    run_dir: str
    wrapper_pid: int
    wrapper_alive: bool
    spawned_at: str
    wrapper_rc: int | None


class SupervisorLease(BaseModel):
    """The `lease` object a LIST reply carries when a lease is unexpired
    (supervisor-protocol ss5). NOT proof of a live controller -- it can
    still name a holder whose connection is gone."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    holder: str
    expires_at: str


class SupervisorListSuccess(BaseModel):
    """A LIST reply that answered (supervisor-protocol ss5): `ok: true`
    plus every field the read-only verb always carries. `_parse_list_reply`
    validates the whole reply through this model in one pass, `runs`
    included -- a malformed row is a nested-model validation failure the
    same `ValidationError` handler turns into `EngineError` (DL-220).

    Tolerant of unknown fields, per the wire's own rule."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    ok: Literal[True]
    version: int
    supervisor_pid: int
    boot_id: str
    incarnation: str
    deadman_s: float | None
    lease: SupervisorLease | None
    runs: list[SupervisorRunRow]


class SupervisorRefusal(BaseModel):
    """A refused reply (supervisor-protocol ss5's `{"ok": false, "error":
    ...}` shape): a malformed envelope, an unsupported version, an unknown
    verb, or a handler exception the dispatch loop turns into `internal:
    ...` rather than letting it kill the process. Carries none of a success
    reply's fields, so parsing a refusal never fails for lacking them
    (DL-220) -- `list_runs` returns this instead of raising for a reply
    `_request` itself did not treat as a transport failure."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    ok: Literal[False]
    error: str


#: `SupervisorClient.list_runs`'s return: the two reply shapes modeled separately (DL-220), so a
#: caller reading `.runs`/`.incarnation` off a refusal is a static error
SupervisorListReply = SupervisorListSuccess | SupervisorRefusal


def _parse_list_reply(raw: dict[str, Any]) -> SupervisorListReply:
    """The one parse of a LIST reply (DL-220), success or refusal.

    An `ok: true` reply validates whole, in one pass: a malformed row (under `runs`) and a
    malformed or mistyped HEADER field (`version`, `supervisor_pid`, `boot_id`, `incarnation`,
    `deadman_s`, `lease`) both refuse as `EngineError`, naming what was wrong; a raw
    `ValidationError` would escape `list_runs`'s three callers (`_list_recheck_loop`,
    `_listed_alive`, `_reconcile`), which expect only `SupervisorUnavailable` or `EngineError`.

    Anything that is not the exact `{"ok": true, ...}` success shape parses as a refusal: a
    non-object reply is already an `EngineError` at the transport (`SupervisorConn.send`), and
    the LIST net's reconnect-and-retry loop (DL-210) needs a garbage reply to read as ONE
    unconfirmed answer. A refusal is the one shape this parse never raises on."""
    if raw.get("ok") is True:
        try:
            return SupervisorListSuccess.model_validate(raw)
        except ValidationError as exc:
            errors = exc.errors()
            if any(len(error["loc"]) > 1 and error["loc"][0] == "runs" for error in errors):
                raise EngineError(
                    f"the supervisor's LIST reply carries a malformed run row: {exc}"
                    " (supervisor-protocol ss5)"
                ) from exc
            field = errors[0]["loc"][0]
            raise EngineError(
                f"the supervisor's LIST reply carries a malformed header"
                f" ({field}): {exc} (supervisor-protocol ss5)"
            ) from exc
    error = raw.get("error")
    return SupervisorRefusal(ok=False, error=error if isinstance(error, str) else "")


# ---- the engine side's connection to the supervisor, as a statechart
#
# `SUPERVISOR_CLIENT` is the declaration (docs/statechart-engine.md). Its configuration is the
# connection's state: `SupervisorClient.phase` reads the active leaf. The instance is keyed by
# the client and lives in the process's one executor, which the wiring hands to the client and
# the engine; a client built without one makes its own (statechart-engine ss11).
#
# The tier posts four facts: the socket accepted a connection (`connect`), EOF or a read error on
# this epoch's connection (`eof`), a request cancelled mid-flight (`cancel`), and `close`. The
# rows decide the configuration. The transport work of each fact is the tier's: EOF sets the
# epoch's `lost` event and fails the pending request, and a cancel also closes the writer, before
# the post; a connect retires what the last epoch left and installs the new one, and a close
# tears down, after it. That work is the same from every state the tier posts in, and it is
# needed in `closed` too, where the tier posts nothing: closing cancels the reader and a renewal
# in flight. So no row has an action; each row's note says what the tier does around it.
#
# Nothing posts to a closed client. The root's ignored rows, guarded by `closed`, exist because
# the completeness check is syntactic (statechart-engine ss10). The lease token, the renewal loop
# and its five-failures rule, pushes and the LIST net are extended state and tier procedures, not
# states. A gap in the chart is a `StatechartError`: in a run it stops the engine with exit 7,
# and outside one the client keeps it and raises it (`_fail`).

#: the socket accepted a connection: a new epoch
CONNECT: Final = "connect"
#: EOF or a read error on this epoch's connection
EOF: Final = "eof"
#: a request was cancelled between its write and its reply (DL-48 item 9)
CANCEL: Final = "cancel"
#: `close` began
CLOSE: Final = "close"

#: where the connection can be: the leaf states of `SUPERVISOR_CLIENT`
CLIENT_PHASES: Final = frozenset({"disconnected", "connected", "lost", "closed"})


# -- the guards. Each docstring is its rows' prose.


def _down(ctx: Context) -> bool:
    """No connection is live: the client is disconnected, before its first connect, or lost. A
    connected client takes `supervisor_client.02`, and a closed one posts nothing."""
    return bool(ctx.active & {"disconnected", "lost"})


def _open(ctx: Context) -> bool:
    """The client is not closed yet: from disconnected, connected or lost, close cancels the
    renewal, reader and LIST tasks, then closes the writer."""
    return "closed" not in ctx.active


def _over(ctx: Context) -> bool:
    """The client is closed. Nothing posts to it: a second close posts nothing, a connection that
    completes after the close is closed unposted, and the EOF and cancel the close itself causes
    do their transport work with no post. Before the first connect, an EOF or a cancel finds no
    row and raises `UnhandledEvent`."""
    return "closed" in ctx.active


_NO_READER = (
    "no reader runs before the first connect, and no row returns to disconnected,"
    " so nothing reports EOF there; one posted raises UnhandledEvent"
)
_NO_REQUEST = (
    "a request enters its write only with a writer held and lost clear, which"
    " first takes a connect, and no row returns to disconnected; one posted"
    " raises UnhandledEvent"
)

#: the engine side's connection to the supervisor (supervisor-protocol ss5), one instance per
#: client
SUPERVISOR_CLIENT: Final = Statechart(
    name="supervisor_client",
    root=State(
        "client",
        regions=(
            Region(
                "phase",
                "disconnected",
                (
                    State("disconnected"),
                    State("connected"),
                    State("lost"),
                    State("closed", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "supervisor_client.01",
            "client",
            CONNECT,
            "connected",
            guard="down",
            cite="supervisor-protocol ss5, DL-48, DL-79",
            note=(
                "A new epoch: the tier closes a writer the lost connection kept and"
                " cancels a reader still running, installs the new writer, a fresh"
                " lost event and its reader, then PINGs. A reconnect re-ACQUIREs"
                " when a token is held."
            ),
        ),
        Row(
            "supervisor_client.02",
            "connected",
            CONNECT,
            kind="internal",
            cite="DL-48",
            note=(
                "Supersede: the tier closes the live writer and cancels its reader,"
                " then installs the new epoch as for row .01."
            ),
        ),
        Row(
            "supervisor_client.03",
            "connected",
            EOF,
            "lost",
            cite="supervisor-protocol ss5, runner-design ss7, DL-48",
            note=(
                "The tier has set the epoch's lost event and failed the pending"
                " request; the next request reconnects."
            ),
        ),
        Row(
            "supervisor_client.04",
            "connected",
            CANCEL,
            "lost",
            cite="DL-48",
            note=(
                "Poisoned: the tier has failed the pending request and closed the"
                " writer, since the stream has no correlation ids and the reply in"
                " flight could reach the next request."
            ),
        ),
        Row(
            "supervisor_client.05",
            "client",
            CLOSE,
            "closed",
            guard="open",
            cite="DL-48",
        ),
        Row(
            "supervisor_client.06",
            "lost",
            CANCEL,
            kind="internal",
            cite="DL-48",
            note=(
                "Poisoned after the loss: EOF failed the pending request before the"
                " cancelled request resumed. The tier closes the writer the loss"
                " kept, so nothing more is written to that stream."
            ),
        ),
        Row(
            "supervisor_client.07",
            "lost",
            EOF,
            kind="ignored",
            cite="statechart-engine ss10",
            note=(
                "The reader of a poisoned epoch meets the EOF its own poison caused:"
                " the epoch is already lost."
            ),
        ),
        Row(
            "supervisor_client.08",
            "client",
            CONNECT,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "supervisor_client.09",
            "client",
            EOF,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "supervisor_client.10",
            "client",
            CANCEL,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "supervisor_client.11",
            "client",
            CLOSE,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
    ),
    guards={"down": _down, "open": _open, "over": _over},
    clock="logical",
    exclusions=(
        Exclusion("disconnected", EOF, _NO_READER),
        Exclusion("disconnected", CANCEL, _NO_REQUEST),
    ),
)

#: consecutive failed renewals that report the host unreachable (ss8)
_UNREACHABLE_AFTER = 5


class SupervisorClient:
    """Engine-side client of the ss6a Tier-1 supervisor (this side may import dsl41 freely).

    It ensures a supervisor is running (spawning one DETACHED if the socket is dead or absent; a
    live socket is reused, which IS reattachment), holds the single-controller lease (ACQUIRE plus
    a background RENEW task), serializes one request/response at a time, and demuxes async exit
    PUSHes into per-run_id futures. Socket loss sets `lost`; adapters awaiting a run watch it, try
    `reconnect()` once, and only then fall back to the spool (supervisor-protocol ss5: pushes are
    notifications, the spool is the truth).

    Cancellation POISONS the connection (DL-48): the frozen ss5 protocol has no correlation ids,
    so a request cancelled between write and reply leaves the stream state unknowable and the
    reply in flight would reach the NEXT request's future. On CancelledError mid-request the
    client fails the pending future, closes the socket, and re-raises; later calls lazily
    reconnect (connect-only, never spawning a supervisor) and re-ACQUIRE presenting the CURRENT
    token (DL-79), whose fresh fencing token fences anything the poisoned connection had in
    flight.

    The connection's state is a `SUPERVISOR_CLIENT` instance in `machines`, the process's
    executor; `phase` reads its leaf. A `StatechartError` that a background task meets (the
    reader, the renewal, the LIST net) goes to `_fail`: in a run, to the run's stop
    (`on_statechart_error`), which ends the engine with exit 7; outside one, the client keeps it
    until the next request or `close` raises it."""

    #: engine defaults (spec ss2): 60s lease, renewed every 20s
    _TTL_S = 60.0
    _RENEW_EVERY_S = 20.0
    #: the short backoff after a failed renewal
    _RETRY_EVERY_S = 1.0

    def __init__(
        self,
        run_root: Path,
        *,
        deadman_s: float | None = None,
        on_contact: Callable[[], None] | None = None,
        machines: Executor | None = None,
    ) -> None:
        self.run_root = run_root
        #: S5b (concurrency-model ss8): the deadman a supervisor WE spawn is started with. What gets
        #: recorded is `supervisor_deadman_s`, read back from the supervisor, because a
        #: reattaching engine meets a supervisor it did not start.
        self.deadman_s = deadman_s
        #: what the supervisor we are talking to says it runs; None until the first PING/ACQUIRE
        #: answers, and afterwards if it runs none
        self.supervisor_deadman_s: float | None = None
        #: called after every confirmed lease exchange (ss8's "positive contact with this host").
        #: A callback, because this module is the transport and the routing table is the engine's
        #: state; what it stamps is UNPROJECTED, so a heartbeat costs no revision and no log
        #: record.
        self.on_contact = on_contact
        #: called once per outage, at the fifth consecutive failed renewal (ss8's unreachability,
        #: which the leader turns into a quarantine); the renewal loop keeps retrying and reports
        #: contact again when the supervisor answers
        self.on_unreachable: Callable[[], None] | None = None
        self.sock_path = run_root / "supervisor.sock"
        # per-INCARNATION (DL-79): it authorizes nothing, since incumbency is proved with the
        # current token. LIST and the lease_held refusal use it to name WHICH engine holds the
        # lease. Fast resume after a crash comes from the holder's connection dying, not from a
        # matching label (supervisor _h_acquire).
        self.controller_id = f"engine:{run_root.resolve()}#{uuid.uuid4().hex[:8]}"
        #: DL-79: presented on re-ACQUIRE to prove incumbency (a free lease needs none; the reply
        #: then teaches a fresh one; see `acquire`, `reconnect`, `_renew_loop`). DL-173: it rides
        #: `_request`'s universal stamp beside `incarnation`, for DL-80's reason: a fencing
        #: credential that a new verb can forget to carry is not a credential.
        self.token: int | None = None
        #: DL-80: the supervisor incarnation our token belongs to; _request pairs it with EVERY
        #: request. A `wrong_incarnation` refusal means the supervisor we knew is gone and our runs
        #: died with it (re-acquire AND reconcile), the opposite of `stale_token`.
        self.incarnation: str | None = None
        #: the current epoch: one event per connection, set when it is lost or poisoned. A reader
        #: keeps its own epoch's event, so a stale reader neither delivers into nor reports on
        #: its successor, and the adapters wait on it. It is a transport fact; `phase` is the state.
        self.lost = asyncio.Event()
        #: the epoch's writer: None before the first connect, after a poison
        #: and after close; an EOF keeps it until the next connect closes it
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._renew_task: asyncio.Task[None] | None = None
        #: the ttl_s the renewal loop renews with: `acquire`'s
        self._renew_ttl_s = self._TTL_S
        self._lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        #: set by `close` before anything else; the lazy reconnect, a late connect, the renewal loop
        #: and the LIST net read it, so a closing client is never connected again
        self._closed = False
        self._pending: asyncio.Future[dict[str, Any]] | None = None
        self._exit_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listed_dead: dict[str, asyncio.Event] = {}
        self._list_task: asyncio.Task[None] | None = None
        self._list_wakeup = asyncio.Event()
        self._list_idle = asyncio.Event()
        self._list_idle.set()
        #: in a run, the run's stop for a `StatechartError` a background task
        #: meets; None outside a run (`on_statechart_error`)
        self._stop: Callable[[StatechartError], object] | None = None
        #: outside a run, the first `StatechartError` a background task met,
        #: until the next request or `close` raises it
        self._fatal: StatechartError | None = None
        #: set when a post raised a `StatechartError`: the instance is discarded and nothing more is
        #: posted to it, so the teardown after the stop meets no second error on the chart
        self._discarded = False
        #: the process's executor, which holds this client's instance
        self._machines = machines if machines is not None else Executor()
        self._machines.start(self, SUPERVISOR_CLIENT)
        self._machines.run()

    def __repr__(self) -> str:
        """The run root names the client in a statechart error's report."""
        return f"SupervisorClient({str(self.run_root)!r})"

    def phase(self) -> str:
        """The connection's phase: the active leaf of this client's `SUPERVISOR_CLIENT` instance;
        after a discarded post, the leaf the failed post found."""
        (leaf,) = self._machines.active(self) & CLIENT_PHASES
        return leaf

    def _post(self, name: str) -> None:
        """Post one fact to this client's instance. The rows have no actions. A gap raises
        `StatechartError` and the instance is discarded: its leaf stays readable but is not
        the connection's state, and later facts go unposted. Whether a request can be written is
        the transport's question (`_writer` held and `lost` clear), so the teardown still
        reconnects, releases the lease and closes."""
        if self._discarded:
            return
        try:
            self._machines.dispatch(self, ChartEvent(name))
        except StatechartError:
            self._discarded = True
            raise

    @property
    def on_statechart_error(self) -> Callable[[StatechartError], object] | None:
        """In a run, the run's stop: every `StatechartError` a background task meets goes to it, and
        it stops the engine with exit 7 (statechart-engine ss5). `_serve_run` sets it beside
        `on_contact` and `on_unreachable`; one met before then is handed over as it is set."""
        return self._stop

    @on_statechart_error.setter
    def on_statechart_error(self, stop: Callable[[StatechartError], object] | None) -> None:
        self._stop = stop
        if stop is not None and (fatal := self._fatal) is not None:
            self._fatal = None
            stop(fatal)

    def _fail(self, error: StatechartError) -> None:
        """A background task met `error`: an assertion failure, never a fault to retry. In a run it
        stops the engine; outside one the client keeps the first until the next request or `close`
        raises it, once, since the caller that meets it reports it."""
        if self._stop is not None:
            self._stop(error)
        elif self._fatal is None:
            self._fatal = error

    # -- connection ---------------------------------------------------------

    async def ensure_running(self) -> None:
        """Connect to a live supervisor, or spawn a fresh one DETACHED (setsid, stdio to
        supervisor.log) and connect-with-retry. A live socket is reused; that reuse IS reattachment
        (spec ss1)."""
        if await self._try_connect():
            return
        child = self._spawn_supervisor()
        spawns = 1
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if await self._try_connect():
                return
            if child.poll() is not None and not self.sock_path.exists():
                # the supervisor exited before publishing: exit 1 is the retryable "another
                # supervisor owns this root" (an old one still tearing down, DL-210), so try again
                # within the window
                if spawns >= _SPAWN_ATTEMPTS:
                    break
                child = self._spawn_supervisor()
                spawns += 1
        raise SupervisorUnavailable(
            f"supervisor did not come up within 10s ({spawns} spawn attempt(s))"
        )

    async def _try_connect(self) -> bool:
        if not self.sock_path.exists():
            return False
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.sock_path), limit=LINE_LIMIT
            )
        except OSError:
            # only the supervisor owns this pathname; a refusal can race its bind/listen window,
            # and unlinking here would hide a live successor
            return False
        if self._closed:
            writer.close()  # nothing posts to a closing client
            return False
        self._new_epoch(reader, writer)
        try:
            resp = await self._request({"cmd": "PING"}, _connect=False)
        except SupervisorUnavailable:
            return False
        if resp.get("ok"):
            self._note_contact(resp)  # PING is where a REATTACH learns the deadman
        return bool(resp.get("ok"))

    def _new_epoch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Post `connect`, then retire the previous connection's remains and install this one
        (supervisor_client.01, .02). The remains go BEFORE the identity swaps: the epoch guard (each
        reader carries its own `lost` event) keeps a stale reader from poisoning or delivering into
        its successor. A connection the chart does not take is closed."""
        try:
            self._post(CONNECT)
        except StatechartError:
            writer.close()
            raise
        if self._writer is not None:
            self._writer.close()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._writer = writer
        self.lost = asyncio.Event()
        self._reader_task = asyncio.ensure_future(self._reader(reader, self.lost))

    def _spawn_supervisor(self) -> subprocess.Popen[bytes]:
        argv = supervisor_argv(self.run_root, self.deadman_s)
        logf = supervisor_log_path(self.run_root).open("ab")
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                start_new_session=True,  # detach job lifetime from this engine
                close_fds=True,
            )
        finally:
            logf.close()  # the child dup'd it; our copy is done
        # the supervisor outlives this call and often this engine, so a daemon thread reaps it and
        # keeps its handle alive until then; dropping the handle while the child runs is a
        # ResourceWarning and a zombie
        threading.Thread(target=child.wait, name="supervisor-reaper", daemon=True).start()
        return child

    # -- request / response / push demux ------------------------------------

    async def _ensure_connected(self) -> None:
        """Lazy reconnect (DL-48): a poisoned or lost connection heals on the next call."""
        if self._closed:
            raise SupervisorUnavailable("client closed")
        if self._writer is not None and not self.lost.is_set():
            return  # the transport can write: `_request`'s own question
        if not await self.reconnect():
            raise SupervisorUnavailable("not connected")

    async def reconnect(self) -> bool:
        """Re-entry after a lost or poisoned connection: connect-only (never spawns a supervisor)
        plus re-ACQUIRE when a lease was held, presenting the current token (DL-79). The supervisor
        mints the fresh fencing token, which fences anything the old connection had in flight.
        False = the supervisor itself is unreachable (callers fall back to the ss7 spool ladder).
        Everything under the reconnect lock uses _connect=False, because re-entering the lazy
        path would deadlock on this non-reentrant lock."""
        async with self._reconnect_lock:
            if self._closed:
                return False
            if self._writer is not None and not self.lost.is_set():
                return True  # another caller already reconnected
            self._arm_list_recheck()
            if not await self._try_connect():
                return False
            if self.token is not None:
                try:
                    resp = await self._request(
                        {
                            "cmd": "ACQUIRE",
                            "controller_id": self.controller_id,
                            "ttl_s": self._TTL_S,
                        },
                        _connect=False,
                    )
                except SupervisorUnavailable:
                    return False
                if not resp.get("ok"):
                    return False  # fenced out: another controller holds the lease
                self.token = int(resp["token"])
                self.incarnation = resp.get("incarnation")  # DL-80
                self._keep_renewing()
            return True

    async def _request(self, obj: dict[str, Any], *, _connect: bool = True) -> dict[str, Any]:
        """One request and its reply. A statechart error that a background task met and kept
        (`_fail`) is raised once, in place of the request or of the `SupervisorUnavailable` the
        request met: the reader fails the pending request before it posts the EOF that meets the
        gap, so the requester wakes to a transport failure while the kept error is already there,
        and a caller that falls back on `SupervisorUnavailable`, such as the seal's supervisor
        proof, must not take it then."""
        if (fatal := self._fatal) is not None:
            self._fatal = None  # raised once: its caller reports it
            raise fatal
        try:
            return await self._exchange(obj, _connect=_connect)
        except SupervisorUnavailable as unavailable:
            if (fatal := self._fatal) is None:
                raise
            self._fatal = None
            raise fatal from unavailable

    async def _exchange(self, obj: dict[str, Any], *, _connect: bool) -> dict[str, Any]:
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
                        # DL-173: `token` joins `incarnation` here, one credential object stamped
                        # in one place on EVERY request including the read-only ones (PING/LIST).
                        # supervisor-protocol.md ss5 makes this wire-safe: "the supervisor ignores
                        # unknown fields" is a request-side rule, not a per-verb one.
                        {**obj, "v": 1, "incarnation": self.incarnation, "token": self.token},
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
                # poison-on-cancel (DL-48): the reply may be in flight, or the request only
                # partially written; with no correlation ids the stream is unknowable, so the
                # connection is torn down, the orphan reply never reaches the NEXT request, and
                # later calls heal via _ensure_connected
                self._poison()
                raise

    def _poison(self) -> None:
        """A request was cancelled mid-flight (supervisor_client.04, .06). The transport work comes
        first and in every state, a closed client's included: `close` cancels a renewal in flight,
        and a request waiting on the lock must find the stream poisoned when the lock passes to
        it. Then the fact is posted, unless the client is closed."""
        self.lost.set()
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(
                SupervisorUnavailable("request cancelled; connection poisoned")
            )
        self._pending = None
        if self._writer is not None:
            self._writer.close()  # the reader sees EOF and exits via its epoch
            self._writer = None
        if self.phase() != "closed":  # nothing posts to a closed client
            self._post(CANCEL)

    async def _reader(self, stream: asyncio.StreamReader, lost: asyncio.Event) -> None:
        """One reader per connection; `lost` is that connection's epoch. A stale reader after a
        poison and reconnect neither delivers into nor poisons its successor (the `lost is
        self.lost` guards). A statechart error in its report of the loss goes to `_fail`: nothing
        awaits this task until `close`."""
        try:
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
                    else:
                        if isinstance(obj, dict) and "pushes_dropped" in obj:
                            self._arm_list_recheck()
                        if self._pending is not None and not self._pending.done():
                            pending, self._pending = self._pending, None
                            pending.set_result(obj if isinstance(obj, dict) else {})
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                self._on_lost(lost)
        except StatechartError as error:
            self._fail(error)

    def _deliver_push(self, obj: dict[str, Any]) -> None:
        run_id = obj.get("run_id")
        fut = self._exit_futures.get(run_id) if isinstance(run_id, str) else None
        if fut is not None and not fut.done():
            fut.set_result(obj)

    def _on_lost(self, lost: asyncio.Event) -> None:
        """EOF or a read error on epoch `lost` (supervisor_client.03, .07). A stale
        connection's reader reports nothing. The transport work comes first and in every state:
        `close` cancels the reader, and the request it was waiting for must fail. Then the fact is
        posted, unless the client is closed."""
        if lost is not self.lost:
            return
        lost.set()
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(SupervisorUnavailable("connection lost"))
        self._pending = None
        if self.phase() != "closed":  # nothing posts to a closed client
            self._post(EOF)

    def exit_future(self, run_id: str) -> asyncio.Future[dict[str, Any]]:
        fut = self._exit_futures.get(run_id)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._exit_futures[run_id] = fut
        return fut

    def forget_exit(self, run_id: str) -> None:
        self._exit_futures.pop(run_id, None)
        self._listed_dead.pop(run_id, None)

    def watch_exit(self, run_id: str) -> asyncio.Event:
        """Make a wait LIST-eligible only after its SPAWN reply, or at reattach.

        Registering the exit future before SPAWN does not prove the supervisor has seen the run,
        and a successful empty LIST in that window must never send the run to the spool ladder.
        """
        if self._list_task is not None and self._list_task.done():
            try:
                self._list_task.result()  # a malformed LIST must fail the adapter loudly
            finally:
                self._list_task = None
                self._arm_list_recheck()  # later waits do not inherit the failed task
        dead = self._listed_dead.setdefault(run_id, asyncio.Event())
        if self._list_task is not None:
            self._list_wakeup.set()
        return dead

    def _arm_list_recheck(self) -> None:
        """One persistent safety net after a dropped push or any reconnect."""
        if self._closed:
            return
        if self._list_task is None:
            self._list_task = asyncio.create_task(self._list_recheck_loop())
        if self._listed_dead:
            self._list_wakeup.set()

    async def _list_recheck_loop(self) -> None:
        """The shared LIST net's task. A statechart error it meets on its request's path goes to
        `_fail`: nothing awaits this task but `watch_exit` and `close`."""
        try:
            while not self._closed:
                if not self._listed_dead:
                    self._list_wakeup.clear()
                await self._list_wakeup.wait()
                await asyncio.sleep(_LIST_RECHECK_EVERY)
                if self._closed:
                    return
                # snapshot BEFORE the request: waits accepted while this LIST is in flight cannot be
                # judged against its older view of the runs
                eligible = dict(self._listed_dead)
                if not eligible:
                    continue
                self._list_idle.clear()
                try:
                    listing = await self.list_runs()
                except SupervisorUnavailable:
                    continue  # the adapters own reconnect failure and spool recovery
                finally:
                    self._list_idle.set()
                if not isinstance(listing, SupervisorListSuccess):
                    continue
                alive = {row.run_id for row in listing.runs if row.wrapper_alive}
                for run_id, dead in eligible.items():
                    if self._listed_dead.get(run_id) is dead and run_id not in alive:
                        dead.set()  # evidence for the spool ladder, never an exit push
        except StatechartError as error:
            self._fail(error)

    # -- verbs --------------------------------------------------------------

    async def acquire(self, *, ttl_s: float | None = None) -> int:
        ttl = self._TTL_S if ttl_s is None else ttl_s
        resp = await self._request(
            {
                "cmd": "ACQUIRE",
                "controller_id": self.controller_id,
                "ttl_s": ttl,
            }
        )
        if not resp.get("ok"):
            raise SupervisorUnavailable(f"lease acquire refused: {resp}")
        self.token = int(resp["token"])
        self.incarnation = resp.get("incarnation")  # DL-80
        self._note_contact(resp)
        self._renew_ttl_s = ttl
        self._keep_renewing()
        return self.token

    def _keep_renewing(self) -> None:
        """Start the renewal loop unless one is running or the client is closed
        (supervisor-protocol ss5: the engine renews while it holds the lease). A lease nobody
        renews lapses one TTL later, every mutating verb then answers `stale_token`, and a
        supervisor with a deadman exits. A loop started after `close` would never be cancelled."""
        if self._closed:
            return
        if self._renew_task is not None and not self._renew_task.done():
            return
        self._renew_task = asyncio.ensure_future(self._renew_loop(self._renew_ttl_s))

    def _note_contact(self, resp: Mapping[str, Any]) -> None:
        """One confirmed lease exchange: ss8's "positive contact with this host". `deadman_s` is
        carried only by the two read verbs and by ACQUIRE, so a RENEW's answer refreshes the contact
        and leaves the interval alone; it cannot change without a new incarnation."""
        reported = resp.get("deadman_s")
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            self.supervisor_deadman_s = float(reported)
        if self.on_contact is not None:
            self.on_contact()

    async def _renew_loop(self, ttl_s: float) -> None:
        """Keep the lease alive (spec ss2: renew every 20s).

        A transient failure must not lapse a live engine's lease (DL-48): pushes would stop, with
        only the adapters' status.json re-poll saving outcomes. Failed RENEWs retry on a short
        backoff; a stale or lapsed token re-ACQUIREs presenting the current token (DL-79:
        incumbency, not a label; a lapsed lease is free, but one another engine now holds refuses
        us, which is the fencing working); connection loss heals via _request's lazy reconnect.

        Five consecutive failures report the host unreachable, loudly and once per outage
        (concurrency-model ss8). The loop continues: it is the one thing that renews the lease,
        so it keeps retrying on the short backoff, and its first success reports contact again,
        which reinstates the host. A loop that ended at the fifth failure would leave a later
        reconnect's lease unrenewed, to lapse one TTL later.

        The loop ends only when it is cancelled, the client is closed, or it meets a
        `StatechartError`. An unexpected error in one renewal is logged and counted as a failed
        renewal, and an error in the unreachable report is logged; neither ends the loop. A
        statechart error is an assertion failure (statechart-engine ss5): it ends the loop and
        goes to `_fail`, whether the renewal's own path met it or a callback into the engine
        did."""
        failures = 0
        try:
            while not self._closed:
                await asyncio.sleep(self._RENEW_EVERY_S if failures == 0 else self._RETRY_EVERY_S)
                try:
                    resp = await self._renew_once(ttl_s)
                    if resp.get("ok"):
                        if failures >= _UNREACHABLE_AFTER:
                            print(
                                f"dsl41: supervisor lease renewed after {failures} failed renewals",
                                file=sys.stderr,
                            )
                        failures = 0
                        self._note_contact(resp)  # ss8: the host answered
                        continue
                except SupervisorUnavailable:
                    pass
                except StatechartError:
                    raise  # an assertion failure is not a failed renewal: below
                except Exception as exc:  # noqa: BLE001 -- one bad renewal must not end renewal
                    print(f"dsl41: supervisor lease renewal failed on {exc!r}", file=sys.stderr)
                failures += 1
                if failures == _UNREACHABLE_AFTER:
                    # ss8: the leader has lost contact with this host. Signalled at the fifth
                    # failure, not the first: one refused connection is a blip
                    print(
                        f"dsl41: supervisor lease renewal failed {_UNREACHABLE_AFTER} times;"
                        " the host is reported unreachable and renewal keeps retrying"
                        " (job outcomes still resolve from the spool)",
                        file=sys.stderr,
                    )
                    try:
                        if self.on_unreachable is not None:
                            self.on_unreachable()
                    except StatechartError:
                        raise  # the engine's assertion failure: below
                    except Exception as exc:  # noqa: BLE001 -- the report must not end renewal
                        print(
                            f"dsl41: reporting the supervisor unreachable failed on {exc!r}",
                            file=sys.stderr,
                        )
        except asyncio.CancelledError:
            pass
        except StatechartError as error:
            self._fail(error)

    async def _renew_once(self, ttl_s: float) -> dict[str, Any]:
        """One RENEW, and a re-ACQUIRE when it is refused; returns the reply that decides the
        renewal."""
        resp = await self._request({"cmd": "RENEW", "ttl_s": ttl_s})
        if resp.get("ok"):
            return resp
        if resp.get("error") == "wrong_incarnation":
            # DL-80: the supervisor restarted under us. Our token belongs to a world that is
            # gone, and every wrapper it held lost its lifeline: each kills its group and
            # records in its own time (DL-205). Drop the pair so the re-ACQUIRE below takes the
            # free path, since a replayed credential can COLLIDE with the new incarnation's
            # counter.
            self.token, self.incarnation = None, None
        # stale_token (fenced by a reconnect's own re-ACQUIRE, or lapsed): the same controller
        # re-acquires, and a fresh token comes back
        resp = await self._request(
            {"cmd": "ACQUIRE", "controller_id": self.controller_id, "ttl_s": ttl_s}
        )
        if resp.get("ok"):
            self.token = int(resp["token"])
            self.incarnation = resp.get("incarnation")  # DL-80
        return resp

    async def spawn(self, spec: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request({"cmd": "SPAWN", "spec": spec})
        if not resp.get("ok"):
            # ss11a gives SPAWN refusals of its own (collision, in_progress, indeterminate); an
            # operator reading the job's cause finds which one, not a transport story
            refusal = f"SPAWN refused: {resp.get('error')} ({resp.get('detail')})"
            if resp.get("error") == "in_progress":
                raise SpawnInProgress(refusal)
            raise SupervisorUnavailable(refusal)
        return resp

    async def signal(self, run_id: str, sig: str) -> dict[str, Any]:
        return await self._request({"cmd": "SIGNAL", "run_id": run_id, "sig": sig})

    async def list_runs(self) -> SupervisorListReply:
        resp = await self._request({"cmd": "LIST"})
        return _parse_list_reply(resp)

    async def shutdown(self) -> dict[str, Any]:
        return await self._request({"cmd": "SHUTDOWN"})

    async def release(self) -> None:
        if self.token is not None:
            with contextlib.suppress(SupervisorUnavailable):
                await self._request({"cmd": "RELEASE"})

    async def close(self) -> None:
        """Post `close` (supervisor_client.05), unless the client is already closed, then tear down:
        cancel the renewal, reader and LIST tasks and close the writer. A second close posts nothing
        and finds nothing left to tear down. The teardown runs even when the post raises. Outside a
        run, a statechart error a background task met and no request raised is raised after it."""
        self._closed = True  # no lazy reconnect may resurrect a closing client
        try:
            if self.phase() != "closed":  # nothing posts to a closed client
                self._post(CLOSE)
        finally:
            await self._tear_down()
        if (fatal := self._fatal) is not None:
            self._fatal = None
            raise fatal

    async def _tear_down(self) -> None:
        # each task hands a statechart error to `_fail` itself, so none ends with one and these
        # suppressions cannot swallow it
        for task in (self._renew_task, self._reader_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._renew_task = self._reader_task = None
        if self._list_task is not None:
            # reader shutdown fails any pending LIST; wait for its request to unwind before
            # cancelling, because cancellation in _request poisons the uncorrelated stream. The
            # wait is bounded in case LIST is queued behind a stuck writer. Ordinary forget_exit
            # never cancels this task.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._list_idle.wait(), timeout=5.0)
            self._list_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._list_task
            self._list_task = None
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None


class SupervisedCommandAdapter:
    """ss6 CMD adapter, detached variant: SPAWN through the supervisor (which owns the wrapper
    lifeline), await the exit push, read status.json. The outcome channel and
    `outcome_from_status` mapping are LocalCommandAdapter's, so tethered and detached runs never
    diverge; run_dir, log and spec construction are shared via `_build_run_spec`.

    Cancellation has two cases (spec ss3):
    (a) oracle-decided terminal (KILLJOB / term_run_time / run_window): SIGNAL TERM via the
        supervisor, grace_seconds, SIGNAL KILL, await the exit push; the outcome shape is the
        tethered kill's.
    (b) engine detach-stop (operator SIGINT/SIGTERM of a --detached run, or shutdown for resume):
        abandon the await and signal NOTHING; the jobs keep running under the supervisor. It
        branches on ctx.detach.stopping."""

    def __init__(
        self,
        client: SupervisorClient,
        *,
        grace_seconds: float = CMD_GRACE_S,
        settle_seconds: float = RECONCILE_SETTLE_S,
    ) -> None:
        self.client = client
        self.grace_seconds = grace_seconds
        self.settle_seconds = settle_seconds
        #: (job, run_number) -> run_id for runs to REATTACH at resume, not respawn (spec ss3);
        #: populated by resume before _launch
        self.reattach: dict[tuple[str, int], str] = {}

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        if ctx.run_root is None:
            raise EngineError("SupervisedCommandAdapter needs a run_root (real domain only)")
        key = (job_ir.name, run_number)
        # a wait whose push is NOT promised re-asks LIST periodically (_await_outcome): a
        # duplicate's run may predate this incarnation, and a reattach's wrapper can die between
        # the startup LIST and the future registration below, so its push lands on no consumer and
        # is dropped (pushes are droppable notifications, ss5). Fresh spawns use the shared client
        # net after a dropped-push hint or reconnect; their futures are registered BEFORE the fork.
        recheck = False
        reattach_id = self.reattach.pop(key, None)
        if reattach_id is not None:
            # the run never stopped (its parent is the supervisor): await its outcome, with no
            # reconciliation injection (E4, ss3)
            run_dir = ctx.run_root / "runs" / f"{job_ir.name}.{run_number}"
            run_id = reattach_id
            recheck = True
        else:
            run_dir, spec = _build_run_spec(
                job_ir,
                run_number,
                ctx,
                grace_seconds=self.grace_seconds,
                create_run_dir=False,  # the supervisor owns it (ss11a, DL-129)
            )
            run_id = spec["run_id"]
            self.client.exit_future(run_id)  # register BEFORE spawn so no push is missed
            reply: dict[str, Any] = {}
            listed_dead = False
            try:
                reply = await self.client.spawn(spec)
                recheck = bool(reply.get("duplicate"))
                listed_dead = recheck and not await self._listed_alive(run_id)
            except SpawnInProgress:
                # ss11a: the wrapper for THIS run_id is already forked; there is nothing to spawn
                # or fail, so await its outcome like any other run of ours, with no wrapper_pid to
                # journal yet
                pass
            except SupervisorUnavailable as exc:
                self.client.forget_exit(run_id)
                return Failed(f"wrapper spawn failed: {exc}")
            except asyncio.CancelledError:
                # DL-359: the supervisor may have forked the wrapper, with its reply still in
                # flight or a duplicate's LIST pending. It holds the lifeline, so no EOF stops the
                # command and the kill must go out, or the command outlives the row. Case b
                # (detach-stop) continues whatever was spawned.
                await self._on_cancel(run_id, ctx, forget_exit=True)
                raise
            if listed_dead:
                # ss11a answered from the DIRECTORY, and nothing is alive to push an exit or write
                # a record (the reply survived a crash its wrapper did not): awaiting would poll
                # forever. Dead evidence is the spool ladder's to judge, as on the supervisor-lost
                # path. A TRANSIENT list failure lands in the await below, which re-asks
                # (`recheck_listing`) instead of deciding on an answer it never got.
                self.client.forget_exit(run_id)
                result, _ended = await resolve_spool(
                    job_ir.name,
                    run_number,
                    run_dir,
                    _procid.current_boot_id(),
                    settle_seconds=self.settle_seconds,
                    grace_seconds=self.grace_seconds,
                    expected_run_id=run_id,
                )
                return result
            if ctx.journal is not None:
                ctx.journal.dispatch(
                    job_ir.name,
                    run_number,
                    wrapper_pid=reply.get("wrapper_pid"),
                    run_dir=str(run_dir),
                    started_at=ctx.clock.now(),
                )
        # ONE await-and-cancel path for both (DL-96): a cancellation of a REATTACHED run must reach
        # the kill ladder, since a kill is the one side effect resume may have
        try:
            return await self._await_outcome(
                run_id, run_dir, job_ir.name, run_number, recheck_listing=recheck
            )
        except asyncio.CancelledError:
            await self._on_cancel(run_id, ctx, forget_exit=False)
            raise

    async def _on_cancel(self, run_id: str, ctx: AdapterContext, *, forget_exit: bool) -> None:
        """The ss3 cancel split (DL-96). Case b (detach-stop): the job continues under the
        supervisor and nothing is killed. Case a (the oracle decided terminal): kill the run. The
        caller re-raises. `forget_exit` is the one difference: the spawn phase registered the run's
        exit future itself, so on case b it drops that future; the await phase leaves it to
        `_await_outcome`."""
        if ctx.detach is not None and ctx.detach.stopping:
            if forget_exit:
                self.client.forget_exit(run_id)
            return
        await self.kill(run_id)

    async def _listed_alive(self, run_id: str) -> bool:
        """Whether the supervisor currently LISTs this run's wrapper as alive. Conservative on
        failure: an unreachable supervisor answers True, because `_await_outcome` handles that path
        (lost -> reconnect -> spool) and a false "dead" would skip a live run's push."""
        try:
            listing = await self.client.list_runs()
        except SupervisorUnavailable:
            return True
        if not isinstance(listing, SupervisorListSuccess):
            return True
        return any(r.run_id == run_id and r.wrapper_alive for r in listing.runs)

    async def _await_outcome(
        self,
        run_id: str,
        run_dir: Path,
        job: str,
        run_number: int,
        *,
        recheck_listing: bool = False,
    ) -> AdapterResult:
        fut = self.client.exit_future(run_id)
        status_path = run_dir / "status.json"
        # `recheck_listing` is any wait whose push is not promised: a duplicate-born one (the run
        # may predate this incarnation) or a reattach (the wrapper can die before the future above
        # existed, and a push with no consumer is dropped). The LIST is re-asked periodically and a
        # definitive "not alive" falls to the spool ladder. Fresh spawns are covered by the shared
        # client net after a dropped-push hint or reconnect.
        #
        # The re-ask is a BUDGET of seconds, spent on the loop's own clock, the clock the poll
        # below is measured against, so `_LIST_RECHECK_EVERY` means seconds here as in the shared
        # net. Only CONNECTED time is spent: the reconnect at the top of the loop owns a lost
        # connection's window, so an outage does not consume the interval.
        loop = asyncio.get_running_loop()
        recheck_left = float(_LIST_RECHECK_EVERY)
        try:
            while True:
                listed_dead = self.client.watch_exit(run_id)
                # after a poison a lost connection does not imply a dead supervisor (DL-48):
                # reconnect first, since falling straight to the spool ladder would kill a healthy
                # detached run whose wrapper is still running
                if self.client.lost.is_set() and not await self.client.reconnect():
                    break  # the supervisor itself is unreachable -> spool below
                status = load_json(status_path)
                if status is not None:
                    return outcome_from_status(_named(status, run_id, job, run_number))
                if listed_dead.is_set():
                    break
                lost_wait = asyncio.ensure_future(self.client.lost.wait())
                exit_wait = asyncio.ensure_future(asyncio.shield(fut))
                listed_wait = asyncio.ensure_future(listed_dead.wait())
                polled_at = loop.time()
                try:
                    await asyncio.wait(
                        {exit_wait, lost_wait, listed_wait},
                        timeout=_OUTCOME_POLL_S,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    lost_wait.cancel()
                    exit_wait.cancel()  # cancels the shield, never fut
                    listed_wait.cancel()
                if fut.done() and not fut.cancelled():
                    status = load_json(status_path)
                    if status is not None:
                        return outcome_from_status(_named(status, run_id, job, run_number))
                    rc = (fut.result() or {}).get("wrapper_rc")
                    return Failed(  # PENDING: E7
                        f"exit_status_unobservable (wrapper exited rc={rc} without a status record)"
                    )
                if recheck_listing and not self.client.lost.is_set():
                    recheck_left -= loop.time() - polled_at
                    if recheck_left <= 0:
                        recheck_left = float(_LIST_RECHECK_EVERY)
                        if not await self._listed_alive(run_id):
                            # definitive: the supervisor answered and the run is not alive
                            # anywhere. Dead evidence is the spool ladder's; waiting on would be
                            # forever. (_listed_alive answers True on a TRANSIENT LIST failure,
                            # so this never fires on one.)
                            break
        finally:
            self.client.forget_exit(run_id)
        # the supervisor connection was lost AND could not be re-established (kill -9 of the
        # supervisor): the wrappers EOF'd and are killing and recording, so resolve via the ss7
        # spool ladder, the reading the tethered resume path uses (spec ss3)
        result, _ended = await resolve_spool(
            job,
            run_number,
            run_dir,
            _procid.current_boot_id(),
            settle_seconds=self.settle_seconds,
            grace_seconds=self.grace_seconds,
            expected_run_id=run_id,
        )
        return result

    #: DL-83: the spawn window. SPAWN returns once the wrapper is FORKED and the wrapper writes
    #: spawn.json a few syscalls later, so a kill decided in between is not addressable yet. The
    #: supervisor answers `not_ready`, and we retry until the run is addressable, the run ends, or
    #: this bound elapses. It matches the supervisor's own shutdown wait (DL-48).
    _SPAWN_WINDOW_S = SPAWN_WINDOW_S
    _SPAWN_POLL_S = 0.02

    async def _signal_when_addressable(
        self, run_id: str, sig: str, fut: "asyncio.Future[dict[str, Any]]"
    ) -> dict[str, Any]:
        """SIGNAL, retrying while the supervisor answers `not_ready`. Gives up the moment the run
        reports its exit (a completed run needs no signal) and after the spawn window, loudly.
        Returns the last answer."""
        deadline = time.monotonic() + self._SPAWN_WINDOW_S
        while True:
            resp = await self.client.signal(run_id, sig)
            if resp.get("error") != "not_ready":
                return resp
            if fut.done() or time.monotonic() >= deadline:
                print(
                    f"dsl41: {sig} for run {run_id} never became addressable"
                    " within the spawn window; the wrapper's lifeline remains"
                    " the backstop",
                    file=sys.stderr,
                )
                return resp
            await asyncio.sleep(self._SPAWN_POLL_S)

    async def kill(self, run_id: str) -> None:
        """ss3 case a: the oracle said terminal. TERM the command group via the supervisor, grace,
        KILL, await the exit push so the wrapper records; the cancelled adapter never reports (the
        oracle already emitted the terminal).

        An `unknown_run` answer to TERM ends the ladder: the supervisor holds no such run, so no
        push can come. It is final even in the SPAWN window. A SPAWN cancelled mid-request poisoned
        its connection, and the reconnect's re-ACQUIRE minted a new token before this SIGNAL went
        out, so a late SPAWN is refused `stale_token` (DL-359).

        Public because resume drives it directly (DL-96): a recorded kill met at resume has a live
        wrapper but no adapter task, and a task cancelled before its first step never enters the
        handler that calls this."""
        fut = self.client.exit_future(run_id)
        try:
            try:
                answer = await self._signal_when_addressable(run_id, "TERM", fut)
            except SupervisorUnavailable:
                return  # supervisor gone: the wrapper's own lifeline handles it
            if answer.get("error") == "unknown_run":
                return  # nothing this supervisor holds can push for it
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(SupervisorUnavailable):
                    await self._signal_when_addressable(run_id, "KILL", fut)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
        finally:
            # every way out forgets the future registered above, the early return and a
            # cancellation of the first signal included
            self.client.forget_exit(run_id)


def spool_names_run(
    doc: Mapping[str, Any] | None,
    *,
    job: str | None = None,
    run_number: int | None = None,
    run_id: str | None = None,
) -> bool:
    """DL-118's identity gate, one owner (DL-178y): does this spool document (`spawn.json`,
    `status.json`, `watch.jsonl`'s start line, anything dict-shaped) name this run? False on an
    absent document or a `job`, `run_number` or `run_id` mismatch, each checked only when the
    caller supplies it. Callers keep their own posture on a False: raise loudly (a live path,
    corruption or spoof) or degrade silently (offline reporting); this predicate answers only
    "does it name the run".

    `job`, `run_number` and `run_id` are keyword-only: a positional `spool_names_run(doc, run_id)`
    would bind to `job`, the identity-check mistake this function exists to prevent."""
    if doc is None:
        return False
    if job is not None and doc.get("job") != job:
        return False
    if run_number is not None and doc.get("run_number") != run_number:
        return False
    if run_id is not None and doc.get("run_id") != run_id:
        return False
    return True


def _named(status: dict[str, Any], run_id: str, job: str, run_number: int) -> dict[str, Any]:
    """A status record is consumed as a run's FATE, so it must name the run (DL-118): a record
    carrying a different `run_id` is a stranger's (corruption or a spoof) and is refused before
    `outcome_from_status` turns it into this job's verdict."""
    if not spool_names_run(status, run_id=run_id):
        raise EngineError(
            f"{job}.{run_number}: status.json reports run_id {status.get('run_id')!r}"
            f" but this run is {run_id!r} -- refusing to consume a stranger's fate"
            " (DL-118)"
        )
    return status


async def resolve_spool(
    job: str,
    run_number: int,
    run_dir: Path | None,
    boot_now: str,
    *,
    settle_seconds: float,
    grace_seconds: float,
    expected_run_id: str | None = None,
) -> tuple[AdapterResult, datetime | None]:
    """Resolve one incomplete CMD run from its spool directory, walking the ss7 ladder top to
    bottom. Returns (outcome, true ended_at if known).

    `expected_run_id` is the identity the durable effect bound (DL-118): a spool record naming a
    DIFFERENT run_id refuses loudly before its fate is consumed. It raises, where the (job,
    run_number) spoof below is dropped, because dropping would read the stranger's presence as
    this run's absence and fabricate "dispatch lost". None means no bound identity exists to check
    (a caller with nothing to expect)."""
    if run_dir is None or not run_dir.is_dir():
        return Failed("dispatch lost to engine crash (run directory missing)"), None
    status_path = run_dir / "status.json"

    def _checked(name: str, doc: dict[str, Any] | None) -> dict[str, Any] | None:
        """The run_id gate, applied to the RAW document at every load: before the tuple filter (a
        wrong-tuple wrong-id record must refuse, not be cleared into a fabricated absence) and on
        every settle-window reload (an absent first load does not exempt the last)."""
        if (
            expected_run_id is not None
            and doc is not None
            and not spool_names_run(doc, run_id=expected_run_id)
        ):
            raise EngineError(
                f"{job}.{run_number}: the spool's {name} reports run_id"
                f" {doc.get('run_id')!r} but the durable effect bound"
                f" {expected_run_id!r} -- refusing to consume a stranger's fate"
                " (DL-118)"
            )
        return doc

    def _tuple_checked(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        """DL-178y: a spool document naming a DIFFERENT (job, run_number) is a stranger's: dropped,
        not trusted. Like `_checked` but silent, since existence under the right name is not
        identity and the mismatch degrades rather than refuses; the caller resolves this run's fate
        from whatever remains. Applied to every doc load, spawn.json and status.json alike."""
        return doc if spool_names_run(doc, job=job, run_number=run_number) else None

    def _load_status() -> dict[str, Any] | None:
        return _tuple_checked(_checked("status.json", load_json(status_path)))

    spawn = _tuple_checked(_checked("spawn.json", load_json(run_dir / "spawn.json")))
    status = _load_status()
    if status is None and spawn is not None and spawn.get("boot_id") == boot_now:
        # same boot: liveness checks mean something (DL-42 item 5)
        wrapper_pid = spawn.get("wrapper_pid")
        wrapper_token = spawn.get("wrapper_start_time")
        if (
            isinstance(wrapper_pid, int)
            and isinstance(wrapper_token, str)
            and _procid.verify_alive(wrapper_pid, wrapper_token)
        ):
            # the wrapper is mid-grace (its own parent-loss kill is running): give its status.json
            # a settle window
            deadline = time.monotonic() + settle_seconds + grace_seconds
            while time.monotonic() < deadline:
                status = _load_status()
                if status is not None:
                    break
                if not _procid.verify_alive(wrapper_pid, wrapper_token):
                    status = _load_status()  # one last read after death
                    break
                await asyncio.sleep(0.1)
        else:
            # one last read after death here too: a wrapper that recorded and exited after the
            # first read is dead now, a zombie included
            status = _load_status()
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
                # command group survived its recorder: kill the verified leader's group
                # (TERMINATED is truthful, a kill happened). SIGKILL follows once the leader has
                # died or the grace has passed, whichever is first: a member that ignores SIGTERM
                # must not outlive the record (DL-226).
                _procid.killpg_quiet(command_pgid, signal.SIGTERM)
                deadline = time.monotonic() + grace_seconds
                try:
                    while time.monotonic() < deadline:
                        if not _procid.verify_alive(command_pid, command_token):
                            break
                        await asyncio.sleep(0.1)
                finally:
                    # a cancel in the grace still ends the group (DL-359)
                    _procid.killpg_quiet(command_pgid, signal.SIGKILL)
                return Terminated("wrapper lost; killed at resume"), None
    if status is not None:
        ended_at = status.get("ended_at")
        return (
            outcome_from_status(status),
            naive_utc(datetime.fromisoformat(ended_at)) if isinstance(ended_at, str) else None,
        )
    return Failed("exit_status_unobservable"), None  # PENDING: E7
