"""Runner adapters (ss6/ss6a): the adapter protocol, the CMD wrapper path and the file watcher.

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
import os
import signal
import sys
import time
import uuid

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol, get_args


from dsl41 import runner_procid as _procid
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
from dsl41.runner_config import FW_DEFAULT_INTERVAL_S, GRACE_TERM_S
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_ledger import Proof
from dsl41.semantics import FwExistence, fw_existence_immediate


#: the ss6a Tier-0 shim, executed by file path, never -m (see its docstring)
_WRAPPER_PATH = Path(_wrapper.__file__)
# JSON-lines buffer cap for every asyncio stream endpoint (control socket both sides). One `status` response is one line covering EVERY job (~220 bytes each), so asyncio's
# 64 KiB default readline() limit overruns at ~300 jobs; 16 MiB clears any plausible estate and
# still bounds a runaway peer. Public because three modules need it (this one, runner_control's
# server AND client) (DL-78).
LINE_LIMIT: int = 2**24


@dataclass
class AdapterContext:
    """What an adapter may touch (ss6): the clock, and in the real domain the run-root layout
    (runs/, logs/). `run_id` is the identity the engine minted for this start and journaled in
    its `dispatch` record (DL-118, PR-36a); the wrapper spec carries it onward, so the WAL, the
    spool and every later reader name ONE key. None lets the adapter mint its own, for a caller
    with no journal behind it."""

    clock: Clock
    run_root: Path | None = None
    run_id: str | None = None
    #: the attempt's spool directory, which the engine names in its `dispatch` record; None
    #: takes the first attempt's (`runs/<job>.<run>/1`, a watch's `runs/<job>.<run>`)
    spool: Path | None = None
    #: the run root's lock proof. The journal re-checks it before every append, and the FW
    #: adapter appends to a spool the journal never sees: an append after the lock was lost is
    #: evidence written by an engine that has lost the root, so this adapter re-proves before
    #: every line (PR-03).
    fence: Proof | None = None
    #: the invocation environment the attempt's `dispatch` record holds; the wrapper spec
    #: carries it to the command (crash spec)
    env: dict[str, str] = field(default_factory=dict)


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
) -> tuple[Path, dict[str, Any]]:
    """The run_dir, log-path and wrapper-spec construction shared by the tethered
    CMD path (LocalCommandAdapter): everything the wrapper
    input spec needs EXCEPT lifeline_fd, which each caller fills from the end that owns the pipe's
    write side. One function keeps the run-dir layout, profile composition and log targets of the
    two paths identical (ss6a)."""
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
    run_dir = (
        ctx.spool
        if ctx.spool is not None
        else ctx.run_root / "runs" / f"{job_ir.name}.{run_number}" / "1"
    )
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir()  # a collision is a bug: an attempt's directory is made once
    fsync_dir(run_dir)
    fsync_dir(run_dir.parent)  # the run's and the runs dir are fsync'd at creation
    fsync_dir(run_dir.parent.parent)
    (ctx.run_root / "logs").mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = job_log_paths(job_ir, run_number, ctx.run_root)
    spec = {
        "version": SPOOL_VERSION,
        # the engine's mint when there is one (PR-36a); the local fallback covers only a caller
        # with no journal (AdapterContext). `is None`, not falsy: an empty-string id, which the
        # journal gates refuse, must fail loudly downstream
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
    if ctx.env:
        spec["env"] = dict(ctx.env)
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

    def __init__(self, *, grace_seconds: float = GRACE_TERM_S) -> None:
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


def read_watch_log(run_dir: Path) -> WatchLog | None:
    """Fold a run directory's `watch.jsonl`, or None when the watch has not been dispatched (no
    file, or no `start` line yet).

    A torn FINAL line is dropped: the append is write-ahead, so the progress it would have
    recorded never happened. A torn INTERIOR line is corruption and raises."""
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


def repair_tail(path: Path) -> None:
    """Make an existing JSON-lines log end on a line boundary before anything appends.

    A line is written in one call, and a crash mid-write leaves a prefix. Under the run's lock,
    with fsync: a tail that parses lost only its newline and gets one; any other tail is cut at
    the last newline, so the reader drops nothing it read before."""
    if not path.exists():
        return
    with path.open("r+b") as f:
        data = f.read()
        if not data or data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n") + 1
        try:
            json.loads(data[cut:])
        except ValueError:
            f.truncate(cut)
        else:
            f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())


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
            run_dir = (
                ctx.spool
                if ctx.spool is not None
                else ctx.run_root / "runs" / f"{job_ir.name}.{run_number}"
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            fsync_dir(run_dir.parent)
            log_path = run_dir / WATCH_LOG
            # make the watch log end on a line boundary before anything appends
            # (period-model ss3.5)
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

    `expected_run_id` is the identity the run's `dispatch` record bound (DL-118): a spool record naming a
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
                f" {doc.get('run_id')!r} but the dispatch record bound"
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
