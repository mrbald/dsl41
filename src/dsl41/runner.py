"""Runner engine: the sans-IO shell over the oracle (phase 11a).

Normative spec: docs/runner-design.md (frozen 2026-07-11, DL-41/DL-41a).
The Oracle stays the single semantics authority; the engine contributes
dispatch (adapters), time (the ss9 clock domains), and -- in later phases --
durability (WAL, 11b), the calendar scheduler and control surface (11c).
Phase 11a scope (ss14): engine loop + VirtualClock + FakeAdapter, proven by
the bisimulation gate (ss13): every SEM trace test runs through both
Oracle-direct and Engine(VirtualClock, FakeAdapter) with identical traces.

Engine loop (ss4, single writer): exactly one task owns the Oracle (it is
not reentrant). Event sources in 11a: externally injected events (the test
script; the control socket and scheduler join in 11c) and adapter
completions. Each iteration processes the earliest work item at or before
the horizon; determinism pins (each has a test):

- queued event vs oracle timer due at the same instant: the event is fed
  and Oracle.feed() itself fires timers due <= ev.at first -- timer before
  event, identical to oracle-direct scripts by construction.
- oracle timer due strictly before every queued event: the clock advances
  and Oracle.advance(now) fires it; feed-only vs advance+feed equivalence
  is a pinned bisimulation property (ss13). Timers due at or before the
  already-processed instant follow the frontier rule (run_until_quiescent
  docstring): they stay lazy until time moves past that instant, exactly
  as oracle-direct feed() leaves them armed until the next event.
- adapter sleep due: the same clock advance resolves it; the adapter task
  then enqueues its completion, which feeds like any other event. The event
  queue is ordered by (at, arrival seq), not pure FIFO: pre-injected script
  events carry future timestamps while completions enqueue at the processed
  frontier, so FIFO would feed a later-stamped external ahead of an earlier
  completion and break the oracle's non-decreasing feed discipline. At
  equal times, arrival order decides -- an injected event beats the
  completion that enqueues after it.

Under VirtualClock the natural-exit vs kill race always resolves
deterministically to the kill: a terminal decision cancels the adapter task
before its completion can enqueue (resolution and enqueueing are separated
by the settle step, and cancel lands between them). The stale-completion
gate below therefore guards the REAL time domain (11b), where a process
exit can already be queued when the oracle decides terminal; virtual runs
exercise it only white-box (test_runner.py).

Dispatch table (ss4): emitted STATUS STARTING for a job_type with a
registered adapter spawns an adapter task -- but only for an ORACLE-DECIDED
start, recognized by the run_number bump every real start performs (the
ghost-run gate): an injected CHANGE_STATUS-parity STARTING overwrite
re-emits STARTING without bumping and, vendor parity, launches nothing. An
emitted terminal status for a job with a live task cancels it (KILLJOB /
term_run_time: the oracle decides, the shell kills; a cancelled adapter
never reports, and anything it dies with other than the cancellation itself
re-raises at the next settle -- fail loudly). Boxes have no adapter row;
ON_NOEXEC bypass never emits STARTING, so nothing spawns by construction.
MUST_START/MUST_COMPLETE alarms are journal + UI surface only (11b/11d) --
no engine action here.

Stale-completion gate (ss4, DL-41 decision 4): completions carry
(job, run_number); the engine drops -- recorded on Engine.drops, the WAL in
11b -- any completion whose run_number mismatches the current one or whose
job is already terminal. The gate guards ONLY engine-made completions:
externally injected STATUS keeps sendevent CHANGE_STATUS parity (it may
legally overwrite terminal statuses; oracle module docstring).

Adapter contract (ss6): ``async run(job_ir, run_number, ctx) -> AdapterResult``
where an ``int`` is the RAW exit code (SEM-09/DL-33 classification stays
oracle-side), ``Terminated`` reports a kill the wrapper actually observed
(-> STATUS TERMINATED), and ``Failed`` reports a completion with no raw exit
code (spawn failure, or the E7 unobservable case -> STATUS FAILURE with its
cause). Adapters never retry (Q4 parity: a shell-side retry would fork
semantics from the simulator) and never time out (term_run_time is the
oracle's timer). Under VirtualClock an adapter may block ONLY through
ctx.clock.sleep_until; that restriction is what makes quiescence decidable
(Engine._settle counts live tasks against pending sleeps). Real adapters
(11b) run under RealClock, where the loop blocks on real IO -- _settle is a
single reaping pass and the loop waits on the queue-activity event instead
of settling (DL-43 item 5).

Phase 11b (ss6-ss7; DL-41a/DL-42 pin the lifecycle semantics):

- Engine time basis is NAIVE UTC in the real domain (RealClock.now()):
  DST never runs the oracle's non-decreasing feed discipline backwards.
  The 11c scheduler converts per-job zoneinfo ticks to UTC instants.
- Journal (ss7): inputs-only JSONL WAL, journal-first -- every injected
  event is WAL-appended (+fsync in the real domain) BEFORE feed(); emitted
  events and the trace replay from oracle determinism, never stored. The
  input alphabet has TWO halves (DL-44 amendment, review B1): external
  events (input records) and time observations (advance records, written
  before every Oracle.advance the engine performs) -- without the latter,
  an advance-fired term_run_time kill would vanish from replay and a late
  natural-exit record could resurrect the job at resume. Dispatch records
  are audit/ordering only (DL-41a): spawn.json, written by the process
  that spawned, is the authoritative spawn record, so dispatch carries
  wrapper_pid + run_dir rather than a pgid the engine never observes.
- Kill-wins gate ordering (DL-44 amendment, review B1): before gating a
  completion, the engine fires the oracle timers due at or before the
  completion's timestamp (feed() would fire exactly these anyway), so the
  gate sees every kill decision first and drops the late natural exit --
  a kill, once decided, is never overwritten by a completion the engine
  made. Externally injected STATUS keeps CHANGE_STATUS overwrite parity.
- LocalCommandAdapter runs every command under the ss6a Tier-0 wrapper
  (runner_wrapper.py, spawned BY FILE PATH -- see its docstring), and the
  wrapper's status.json is the sole outcome channel; the wrapper's exit
  only notifies the engine to read it. Cancel (the oracle said terminal) =
  verify the recorded (pid, start-time), signal the command pgid SIGTERM,
  grace, SIGKILL; the wrapper observes and records; the cancelled adapter
  never reports.
- Resume (ss7): refuse on catalog-hash or clock-domain mismatch, replay
  inputs through a fresh Oracle, seed the ghost-run gate so replayed starts
  never respawn, then reconcile from the spool ladder: live wrapper ->
  settle window; status.json -> inject the real completion at
  max(ended_at, last journal at) with the true ended_at in the payload;
  verified command group orphaned by a dead wrapper -> kill it, TERMINATED
  "wrapper lost; killed at resume" (a kill that happened); nothing ->
  FAILURE exit_status_unobservable (PENDING: E7). A start with no spool
  trace at all (crash between feed and spawn) -> FAILURE "dispatch lost to
  engine crash" -- provably-never-ran is still never re-executed silently
  (measure-seven-times: no side effects on resume beyond recorded kills).
  FW watchers are the exception: polling is an idempotent read, so
  incomplete FW runs are re-dispatched instead. Reconciliation completions
  go through the ss4 stale gate like any adapter completion: if replay
  already reached a terminal state (say a term_run_time TERMINATED), the
  late real record is dropped AND journaled -- never a silent overwrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import os
import signal
import sys
import time
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dsl41 import runner_wrapper as _wrapper
from dsl41.ir import CatalogIR, ExecSpec, FwSpec, JobIR
from dsl41.oracle import _TERMINAL, Event, Oracle

#: the ss6a Tier-0 shim, executed by file path (never -m; see its docstring)
_WRAPPER_PATH = Path(_wrapper.__file__)


class EngineError(RuntimeError):
    """A shell-level refusal (never a semantics verdict): the engine detected
    it cannot make progress -- e.g. the zero-delay-cycle guard in
    run_until_quiescent. Loud by design (CLAUDE.md: no silent loss)."""


class Clock(Protocol):
    """ss9 time domain: the engine's only source of "now" and waiting."""

    #: True for the virtual domain (engine drives time and settles adapters);
    #: False for the real domain (the loop blocks on real IO instead)
    virtual: bool

    def now(self) -> datetime: ...

    def next_sleeper_due(self) -> datetime | None:
        """Earliest pending adapter sleep, or None. RealClock (11b) returns
        None -- real sleeps wake themselves; only the virtual domain needs
        the engine to drive them forward."""
        ...

    def pending_sleepers(self) -> int:
        """Count of pending adapter sleeps. Virtual-domain bookkeeping for
        Engine._settle; RealClock (11b) returns 0 and its loop blocks on
        real IO instead of settling."""
        ...

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        """Engine-side wait (ss9): real -- sleep until `t`, waking early when
        `interrupt` fires (queue activity); virtual -- jump instantly."""
        ...

    async def sleep_until(self, t: datetime) -> None:
        """Adapter-side blocking wait: returns once now >= t."""
        ...


class VirtualClock:
    """ss9: jumps to the next wake instantly -- enabled by the oracle taking
    explicit timestamps everywhere. The engine owns time: wait_until() moves
    `now` forward and resolves due sleeps; sleep_until() parks the calling
    adapter task until the engine's clock reaches its deadline. `interrupt`
    is ignored: jumps are instantaneous, there is nothing to interrupt."""

    virtual = True

    def __init__(self, start: datetime = datetime.min) -> None:
        self._now = start
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = 0  # heap tie-break: registration order

    def now(self) -> datetime:
        return self._now

    def next_sleeper_due(self) -> datetime | None:
        self._prune()
        return self._sleepers[0][0] if self._sleepers else None

    def pending_sleepers(self) -> int:
        self._prune()
        return len(self._sleepers)

    def _prune(self) -> None:
        # a cancelled adapter task (engine cancel on terminal status) leaves
        # a dead future behind; drop them so due/pending reads see live work
        if any(fut.done() for _, _, fut in self._sleepers):
            self._sleepers = [entry for entry in self._sleepers if not entry[2].done()]
            heapq.heapify(self._sleepers)

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        if t > self._now:
            self._now = t
        self._prune()
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.set_result(None)

    async def sleep_until(self, t: datetime) -> None:
        if t <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (t, self._seq, fut))
        await fut


class RealClock:
    """ss9 wall-clock domain. now() is NAIVE UTC (module docstring: DST must
    never run the oracle's non-decreasing feed discipline backwards; the 11c
    scheduler converts zoneinfo ticks to UTC instants). wait_until() sleeps
    in bounded chunks, waking early when `interrupt` fires (queue activity);
    sleep_until() is a plain sleep -- real sleeps wake themselves, so the
    engine never drives them (next_sleeper_due()/pending_sleepers() are the
    virtual domain's bookkeeping and stay empty here, DL-43 item 5)."""

    virtual = False

    #: cap one wait slice; re-checking hourly costs nothing and bounds drift
    _MAX_SLICE_S = 3600.0

    def now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def next_sleeper_due(self) -> datetime | None:
        return None

    def pending_sleepers(self) -> int:
        return 0

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        while True:
            remaining = (t - self.now()).total_seconds()
            if remaining <= 0:
                return
            slice_s = min(remaining, self._MAX_SLICE_S)
            if interrupt is None:
                await asyncio.sleep(slice_s)
                continue
            try:
                await asyncio.wait_for(interrupt.wait(), timeout=slice_s)
                return  # queue activity: the engine loop re-plans its wait
            except TimeoutError:
                continue

    async def sleep_until(self, t: datetime) -> None:
        remaining = (t - self.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)


def catalog_hash(catalog: CatalogIR) -> str:
    """Content hash gating resume (ss7): sha256 of the catalog's canonical
    JSON dump. Conservative by design -- an estate that changed in ANY way
    re-baselines explicitly rather than silently drifting semantics."""
    return hashlib.sha256(catalog.model_dump_json().encode("utf-8")).hexdigest()


def _dsl41_version() -> str:
    try:
        from importlib.metadata import version

        return version("dsl41")
    except Exception:  # not installed (editable src run): version is advisory
        return "0+unknown"


class Journal:
    """ss7 append-only JSONL WAL, one file per run. Inputs-only principle:
    emitted events and the trace are pure functions of the input sequence
    (oracle determinism), so only injected inputs are stored; `journal
    render` replays them through a fresh Oracle. Record kinds: header /
    input / advance / dispatch / drop (module docstring covers why dispatch
    is audit-only and why advances are inputs). fsync per record in the
    real domain (write-ahead: append + fsync BEFORE feed); buffered in
    rehearse, fsync on close. macOS caveat, accepted: os.fsync does not
    force the drive cache (F_FULLFSYNC would, at a large cost)."""

    def __init__(self, path: Path | str, *, fsync_each: bool, start_seq: int = 0) -> None:
        self.path = Path(path)
        self._f = self.path.open("ab")
        self._fsync_each = fsync_each
        self.seq = start_seq

    @classmethod
    def create(
        cls, path: Path | str, *, catalog: CatalogIR, clock_domain: str, started_at: datetime
    ) -> Journal:
        journal = cls(path, fsync_each=clock_domain == "real")
        journal._write(
            {
                "rec": "header",
                "catalog_hash": catalog_hash(catalog),
                "dsl41_version": _dsl41_version(),
                "clock_domain": clock_domain,
                "started_at": started_at.isoformat(),
            }
        )
        return journal

    def input(self, ev: Event, source: str) -> None:
        """source in {scheduler, adapter, control, reconcile} (ss7)."""
        self.seq += 1
        self._write(
            {
                "rec": "input",
                "seq": self.seq,
                "at": ev.at.isoformat(),
                "kind": ev.kind,
                "payload": ev.payload,
                "source": source,
            }
        )

    def advance(self, at: datetime) -> None:
        """A time observation the engine acted on (Oracle.advance): part of
        the input alphabet (DL-44 amendment) -- the timer firings it causes
        (term_run_time kills, alarms) must replay, or a crash after an
        advance-fired kill would resurrect the job. Shares the input seq so
        replay interleaves feeds and advances in the original order."""
        self.seq += 1
        self._write({"rec": "advance", "seq": self.seq, "at": at.isoformat()})

    def dispatch(
        self,
        job: str,
        run_number: int,
        *,
        wrapper_pid: int | None,
        run_dir: str | None,
        started_at: datetime,
    ) -> None:
        self._write(
            {
                "rec": "dispatch",
                "job": job,
                "run_number": run_number,
                "wrapper_pid": wrapper_pid,
                "run_dir": run_dir,
                "started_at": started_at.isoformat(),
            }
        )

    def drop(self, ev: Event, reason: str) -> None:
        self._write(
            {
                "rec": "drop",
                "at": ev.at.isoformat(),
                "kind": ev.kind,
                "payload": ev.payload,
                "reason": reason,
            }
        )

    def _write(self, record: dict[str, Any]) -> None:
        self._f.write(json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")
        self._f.flush()
        if self._fsync_each:
            os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()


def read_journal(path: Path | str) -> list[dict[str, Any]]:
    """Parse a run journal. A torn FINAL line (crash mid-append) is dropped
    -- write-ahead means the corresponding feed never happened; torn or
    invalid INTERIOR lines are corruption and raise loudly."""
    records: list[dict[str, Any]] = []
    raw = Path(path).read_bytes()
    lines = raw.split(b"\n")
    trailing = lines.pop() if lines and lines[-1] == b"" else None
    for index, line in enumerate(lines):
        if not line:
            raise EngineError(f"journal {path}: empty interior line {index + 1}")
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and trailing is None:
                break  # torn final append: the feed it preceded never ran
            raise EngineError(f"journal {path}: corrupt line {index + 1}: {exc}") from exc
    if not records or records[0].get("rec") != "header":
        raise EngineError(f"journal {path}: missing header record")
    return records


def replay_inputs(oracle: Oracle, records: list[dict[str, Any]]) -> None:
    """Apply the journal's input AND advance records, in seq order, through
    `oracle` (an advance is a time observation -- the other half of the
    input alphabet; DL-44 amendment)."""
    replayable = sorted(
        (r for r in records if r.get("rec") in ("input", "advance")),
        key=lambda r: int(r["seq"]),
    )
    for record in replayable:
        if record["rec"] == "advance":
            oracle.advance(datetime.fromisoformat(record["at"]))
        else:
            oracle.feed(
                Event(
                    at=datetime.fromisoformat(record["at"]),
                    kind=record["kind"],
                    payload=record["payload"],
                )
            )


def _last_journal_at(records: list[dict[str, Any]]) -> datetime:
    """max time the journal proves the run reached (ss7 'last journal at')."""
    stamps = [datetime.fromisoformat(records[0]["started_at"])]
    for record in records:
        for key in ("at", "started_at"):
            if key in record:
                stamps.append(datetime.fromisoformat(record[key]))
    return max(stamps)


@dataclass
class AdapterContext:
    """What an adapter may touch (ss6): the clock, and in the real domain
    the run-root layout (runs/, logs/) plus the WAL for dispatch records."""

    clock: Clock
    run_root: Path | None = None
    journal: Journal | None = None


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


def _load_json(path: Path) -> dict[str, Any] | None:
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
        # reading; vendor parity unverified (real AutoSys may mark FAILURE)
        return Terminated(cause)
    if outcome == "terminated":
        return Terminated(str(status.get("cause", "terminated")))
    if outcome == "spawn_failed":
        return Failed(f"spawn failed: {status.get('error')}")
    return Failed(f"unrecognized status record outcome {outcome!r}")


class LocalCommandAdapter:
    """ss6 CMD adapter: spawn the ss6a Tier-0 wrapper, await it, read
    status.json -- the sole outcome channel. No retries (Q4 parity), no
    timeouts (term_run_time is the oracle's timer), no classification.
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
        if ctx.run_root is None:
            raise EngineError("LocalCommandAdapter needs a run_root (real domain only)")
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
        logs_dir = ctx.run_root / "logs"
        logs_dir.mkdir(exist_ok=True)

        lifeline_r, lifeline_w = os.pipe()
        try:
            spec = {
                "version": _wrapper.SPEC_VERSION,
                "run_id": str(uuid.uuid4()),
                "job": job_ir.name,
                "run_number": run_number,
                "command": command,
                "run_dir": str(run_dir),
                "lifeline_fd": lifeline_r,
                "stdout_path": spec_ir.std_out_file
                or str(logs_dir / f"{job_ir.name}.{run_number}.out"),
                "stderr_path": spec_ir.std_err_file
                or str(logs_dir / f"{job_ir.name}.{run_number}.err"),
                "stdin_path": spec_ir.std_in_file,
                "grace_seconds": self.grace_seconds,
            }
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(_WRAPPER_PATH),
                    stdin=asyncio.subprocess.PIPE,
                    pass_fds=(lifeline_r,),
                )
            except OSError as exc:
                # EMFILE/ENOMEM-class glitch: fail THIS job, not the engine
                # (review M6; symmetric with the wrapper's own spawn_failed)
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
            status = _load_json(run_dir / "status.json")
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
            spawn = _load_json(run_dir / "spawn.json")
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
            and _wrapper.verify_alive(pid, token)  # the PID-reuse guard
        ):
            _killpg_quiet(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.grace_seconds)
            except TimeoutError:
                _killpg_quiet(pgid, signal.SIGKILL)
        await proc.wait()  # the wrapper records the outcome, then exits


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # whole group already gone


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


@dataclass
class _LiveRun:
    run_number: int
    task: asyncio.Task[None]


def _raise_if_failed(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc  # adapter bug: fail loudly, never guess


class Engine:
    """ss4 single-writer engine loop over one Oracle. 11a surface: inject()
    external events + run_until_quiescent(horizon). The WAL journal slots in
    front of every feed in 11b (journal-first, ss7); the scheduler and the
    control socket become additional event sources in 11c."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        clock: Clock,
        adapters: Mapping[str, JobAdapter],
        journal: Journal | None = None,
        run_root: Path | None = None,
    ) -> None:
        self.oracle = Oracle(catalog)
        self.clock = clock
        self.adapters = dict(adapters)  # job_type -> adapter; no BOX row
        self.journal = journal
        self.run_root = run_root
        self.drops: list[tuple[Event, str]] = []  # gate rejections; also WAL drop records
        #: time-ordered event queue: (at, arrival seq, event, is_completion, source);
        #: see the module docstring for why FIFO alone is wrong here
        self._queue: list[tuple[datetime, int, Event, bool, str]] = []
        self._queue_seq = 0
        #: real-domain wake signal: set on every enqueue and adapter-task
        #: completion so a blocked wait_until() re-plans immediately
        self._activity = asyncio.Event()
        self._live: dict[str, _LiveRun] = {}
        #: last run_number dispatched per job -- the ghost-run gate: an
        #: injected CHANGE_STATUS STARTING overwrite re-emits STARTING
        #: without bumping run_number, and (vendor parity) must not launch
        #: a process; only an oracle-decided start advances the counter
        self._dispatched: dict[str, int] = {}
        #: cancelled tasks awaiting collection; _settle re-raises any
        #: non-CancelledError they die with (fail loudly, never swallow)
        self._reaping: list[asyncio.Task[None]] = []

    def inject(self, ev: Event, *, source: str = "control") -> None:
        """Queue an external event (test script now; sendevent in 11c).
        External events are never gated: injected STATUS keeps its
        CHANGE_STATUS parity."""
        self._enqueue(ev, is_completion=False, source=source)

    def _enqueue(self, ev: Event, *, is_completion: bool, source: str = "adapter") -> None:
        self._queue_seq += 1
        heapq.heappush(self._queue, (ev.at, self._queue_seq, ev, is_completion, source))
        self._activity.set()

    async def run_until_quiescent(self, horizon: datetime) -> list[Event]:
        """Process every queued event, due oracle timer, and adapter
        completion at or before `horizon`; return the oracle events emitted.
        Work due after the horizon stays pending for a later call (rehearse
        quiescence, ss9). Time only moves forward across calls.

        The frontier rule (bisimulation-pinned): a timer due at or before
        the already-processed instant (the frontier) fires only once the
        horizon lets time move PAST that instant, and then back-dated to its
        due time via advance(frontier) -- exactly when and how oracle-direct
        feed() would fire it on the next event. This keeps zero-delta
        deadlines (due == now at arming) lazy, matching the oracle, and
        keeps past-due timers (negative offsets lower fine) from tripping
        advance()'s backwards-time check.

        The zero-delay-cycle guard: a condition cycle over instant
        completions generates unbounded work at one frozen virtual instant
        (AutoSys's own tight-loop pattern, L010's concern, compressed to
        zero duration). The engine refuses with EngineError after a
        catalog-scaled event budget at a single instant rather than hanging
        -- loud, not silent."""
        emitted: list[Event] = []
        instant: datetime | None = None
        instant_events = 0
        instant_budget = max(10_000, 100 * len(self.oracle.catalog.jobs))
        while True:
            await self._settle()
            now = self.clock.now()
            if now != instant:
                instant, instant_events = now, 0
            head_at = self._queue[0][0] if self._queue else None
            due = [
                t
                for t in (self.oracle.next_timer_due(), self.clock.next_sleeper_due())
                if t is not None
            ]
            raw_due = min(due) if due else None
            eff_due = max(raw_due, now) if raw_due is not None else None
            take_event = (
                head_at is not None
                and head_at <= horizon
                and (eff_due is None or head_at <= eff_due)
            )
            fire_timer = (
                not take_event
                and raw_due is not None
                and eff_due is not None
                and eff_due <= horizon
                and (raw_due > now or horizon > now)
            )
            if take_event:
                _, _, ev, is_completion, source = heapq.heappop(self._queue)
                if is_completion:
                    # kill-wins gate ordering (DL-44 amendment, review B1):
                    # fire the oracle timers due at or before the completion's
                    # instant FIRST -- feed() would fire exactly these anyway,
                    # but the gate must SEE every kill decision they carry
                    # (term_run_time TERMINATED) or a late natural exit would
                    # overwrite a kill. The gate still precedes ENGINE clock
                    # movement: a dropped completion moves no wall/virtual
                    # time and wakes no sleeper (DL-43 item 11).
                    timer_due = self.oracle.next_timer_due()
                    if timer_due is not None and timer_due <= ev.at:
                        if self.journal is not None:
                            self.journal.advance(ev.at)
                        out = self.oracle.advance(ev.at)
                        emitted.extend(out)
                        self._dispatch(out)
                    reason = self._stale_reason(ev)
                    if reason is not None:
                        self.drops.append((ev, reason))
                        if self.journal is not None:
                            self.journal.drop(ev, reason)
                        continue
                if self.journal is not None:
                    self.journal.input(ev, source)  # WAL-append + fsync BEFORE feed (ss7)
                await self.clock.wait_until(ev.at)
                out = self.oracle.feed(ev)
                emitted.extend(out)
                self._dispatch(out)
            elif fire_timer:
                assert eff_due is not None
                if self.journal is not None:
                    # a time observation is an input (DL-44 amendment): the
                    # timer firings it causes must survive a crash, or resume
                    # replay would resurrect a job the oracle already killed
                    self.journal.advance(eff_due)
                await self.clock.wait_until(eff_due)
                out = self.oracle.advance(eff_due)
                emitted.extend(out)
                self._dispatch(out)
            elif self.clock.virtual or (not self._live and not self._queue and raw_due is None):
                # virtual quiescence: nothing can move without the clock;
                # real quiescence: no work exists and none can appear
                return emitted
            else:
                # real domain: block until queue activity or the next due
                # instant; a completed adapter task also fires _activity so
                # _settle can re-raise adapter failures promptly
                next_wake = [t for t in (eff_due, head_at) if t is not None]
                target = min(next_wake, default=None)
                if target is not None and target > horizon:
                    return emitted  # nothing left on this side of the horizon
                self._activity.clear()
                await self.clock.wait_until(
                    target if target is not None else datetime.max, interrupt=self._activity
                )
                continue  # a pure wait is not same-instant work: skip the budget
            instant_events += 1
            if instant_events > instant_budget:
                raise EngineError(
                    f"no virtual-time progress after {instant_events} events at "
                    f"{instant}: zero-delay condition cycle with instant completions? "
                    "(the AutoSys tight-loop pattern, L010; give the loop's jobs a "
                    "nonzero FakeAdapter duration or break the cycle)"
                )

    async def _settle(self) -> None:
        """Yield until every live adapter task is done or parked on the
        clock and every cancelled task is reaped. Sound because adapters may
        block only via sleep_until (module docstring contract): a live task
        is then either ready -- one more yield lets it progress -- or holds
        exactly one pending sleeper, so live == pending means nothing can
        move without the clock. Reaped tasks that died with anything other
        than the cancellation itself re-raise here (fail loudly, never
        guess). Real domain (DL-43 item 5): adapters block on real IO, so
        settling is undecidable and unnecessary -- one reaping pass, no
        spin; the loop's activity event wakes it when a task finishes."""
        while True:
            for job, run in list(self._live.items()):
                if run.task.done():
                    del self._live[job]
                    _raise_if_failed(run.task)
            still_reaping: list[asyncio.Task[None]] = []
            for task in self._reaping:
                if task.done():
                    _raise_if_failed(task)
                else:
                    still_reaping.append(task)
            self._reaping = still_reaping
            if not self.clock.virtual:
                return
            if not self._reaping and len(self._live) == self.clock.pending_sleepers():
                return
            await asyncio.sleep(0)

    def _stale_reason(self, ev: Event) -> str | None:
        job = ev.job()
        assert job is not None  # engine-made completions always carry a job
        rt = self.oracle.store.job.get(job)
        if rt is None or rt.run_number != ev.payload.get("run_number"):
            return "run_number mismatch"
        if rt.status in _TERMINAL:
            return "job already terminal"
        return None

    def _dispatch(self, emitted: list[Event]) -> None:
        for ev in emitted:
            if ev.kind != "STATUS":
                continue  # alarms: journal + UI surface only (ss4)
            job = ev.job()
            if job is None:
                continue
            status = ev.payload.get("status")
            if status == "STARTING":
                self._spawn(job)
            elif status in _TERMINAL:
                live = self._live.pop(job, None)
                if live is not None:
                    live.task.cancel()  # the oracle decided; the shell kills
                    self._reaping.append(live.task)

    def _spawn(self, job: str) -> None:
        job_ir = self.oracle.catalog.jobs.get(job)
        if job_ir is None:
            return  # pseudo-entries (name^INST) have no definition to run
        adapter = self.adapters.get(job_ir.job_type)
        if adapter is None:
            return  # boxes and unregistered job_types have no dispatch row
        run_number = self.oracle.store.job[job].run_number
        if run_number <= self._dispatched.get(job, 0):
            # STARTING emitted without a run_number bump: an injected
            # CHANGE_STATUS-parity overwrite, not an oracle-decided start.
            # Vendor parity: sendevent CHANGE_STATUS rewrites the DB status
            # and launches nothing -- neither do we (ghost-run gate)
            return
        self._dispatched[job] = run_number
        stale = self._live.pop(job, None)
        if stale is not None:
            # one live attempt per job; a report from the old task would be
            # gate-dropped anyway (run_number mismatch) -- cancel is tidier
            stale.task.cancel()
            self._reaping.append(stale.task)
        self._launch(job_ir, run_number, adapter)

    def _launch(self, job_ir: JobIR, run_number: int, adapter: JobAdapter) -> None:
        """Create the adapter task, bypassing the ghost-run gate. Reached
        from _spawn (oracle-decided starts) and from resume's FW re-dispatch
        (module docstring), where the seeded gate must not refuse."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_adapter(job_ir, run_number, adapter))
        task.add_done_callback(lambda _t: self._activity.set())
        self._live[job_ir.name] = _LiveRun(run_number=run_number, task=task)

    async def shutdown(self) -> None:
        """Cancel every live adapter task and collect the cancellations,
        re-raising anything a task died with OTHER than the cancellation
        itself (fail loudly -- a teardown bug must not vanish). 11a: orderly
        harness/rehearse teardown; the tethered-kill semantics (wrapper
        records the outcome, ss6a) arrive with real adapters in 11b."""
        tasks = [run.task for run in self._live.values()] + self._reaping
        self._live.clear()
        self._reaping = []
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _run_adapter(self, job_ir: JobIR, run_number: int, adapter: JobAdapter) -> None:
        ctx = AdapterContext(clock=self.clock, run_root=self.run_root, journal=self.journal)
        result = await adapter.run(job_ir, run_number, ctx)
        # (job, run_number) ride along for the ss4 stale-completion gate
        payload: dict[str, object] = {"job": job_ir.name, "run_number": run_number}
        if isinstance(result, int):
            # raw exit code only: the SEM-09/DL-33 verdict stays oracle-side
            payload["exit_code"] = result
        elif isinstance(result, Terminated):
            # a kill that was observed to happen (DL-41a item 7)
            payload |= {"status": "TERMINATED", "cause": result.cause}
        elif isinstance(result, Failed):
            payload |= {"status": "FAILURE", "cause": result.cause}
        else:
            raise EngineError(f"adapter for {job_ir.name!r} returned {result!r}")
        self._enqueue(
            Event(at=self.clock.now(), kind="STATUS", payload=payload),
            is_completion=True,
        )


# ------------------------------------------------------------ run lifecycle (ss7)


def start_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
) -> Engine:
    """Create the run-root layout (journal.jsonl, runs/, logs/) and an
    Engine wired to it. Refuses a run_root that already holds a journal --
    that is what --resume is for (no silent re-baselining)."""
    journal_path = run_root / "journal.jsonl"
    if journal_path.exists():
        raise EngineError(
            f"{journal_path} already exists: resume it (resume_run) or pick a fresh run root"
        )
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    journal = Journal.create(
        journal_path,
        catalog=catalog,
        clock_domain="virtual" if clock.virtual else "real",
        started_at=clock.now(),
    )
    _fsync_dir(run_root)  # the journal's directory entry is a record too (review M5)
    return Engine(catalog, clock=clock, adapters=adapters, journal=journal, run_root=run_root)


async def resume_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    settle_seconds: float = 5.0,
    grace_seconds: float = 10.0,
) -> Engine:
    """ss7 resume: hash-gate, replay, reconcile. Returns an Engine with the
    reconciliation completions queued (source=reconcile); the caller runs
    the loop to process them and continue the run."""
    records = read_journal(run_root / "journal.jsonl")
    header = records[0]
    if header.get("catalog_hash") != catalog_hash(catalog):
        raise EngineError(
            "catalog hash mismatch: the estate changed since this journal was written;"
            " re-baseline explicitly with a fresh run (no silent semantic drift, ss7)"
        )
    domain = "virtual" if clock.virtual else "real"
    if header.get("clock_domain") != domain:
        raise EngineError(
            f"clock-domain mismatch: journal is {header.get('clock_domain')!r},"
            f" resume clock is {domain!r}"
        )
    last_at = _last_journal_at(records)
    if not clock.virtual and last_at > clock.now():
        raise EngineError(
            f"journal is from the future ({last_at.isoformat()} > now): the machine"
            " clock moved backwards; refusing to feed non-decreasing time backwards"
        )
    journal = Journal(
        run_root / "journal.jsonl",
        fsync_each=not clock.virtual,
        start_seq=max(
            (int(r["seq"]) for r in records if r.get("rec") in ("input", "advance")),
            default=0,
        ),
    )
    engine = Engine(catalog, clock=clock, adapters=adapters, journal=journal, run_root=run_root)
    replay_inputs(engine.oracle, records)
    # seed the ghost-run gate: replayed starts are reconciliation's business,
    # never a fresh dispatch
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    await _reconcile(
        engine, records, last_at, settle_seconds=settle_seconds, grace_seconds=grace_seconds
    )
    return engine


async def _reconcile(
    engine: Engine,
    records: list[dict[str, Any]],
    last_at: datetime,
    *,
    settle_seconds: float,
    grace_seconds: float,
) -> None:
    """The ss6a/ss7 reconciliation ladder (module docstring). Tethered
    semantics did the killing already (wrappers EOF'd when the engine
    died), so this is mostly READING; signals are for the residual crash
    matrix only, and only ever at a (pid, start-time)-verified target."""
    assert engine.run_root is not None
    boot_now = _wrapper.current_boot_id()
    # sweep = union(journal dispatch records, runs/ directory) (ss7)
    candidates: dict[tuple[str, int], Path | None] = {}
    for record in records:
        if record.get("rec") == "dispatch":
            run_dir = record.get("run_dir")
            candidates[(record["job"], int(record["run_number"]))] = (
                Path(run_dir) if run_dir else None
            )
    runs_dir = engine.run_root / "runs"
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            job, dot, num = entry.name.rpartition(".")
            if entry.is_dir() and dot and num.isdigit():
                candidates.setdefault((job, int(num)), entry)

    def _inject(job: str, run_number: int, extras: dict[str, object], at: datetime) -> None:
        engine._enqueue(
            Event(
                at=max(at, last_at),  # feed times are non-decreasing (ss7)
                kind="STATUS",
                payload={"job": job, "run_number": run_number, **extras},
            ),
            is_completion=True,  # the ss4 gate applies: replay may know better
            source="reconcile",
        )

    for (job, run_number), run_dir in sorted(candidates.items()):
        rt = engine.oracle.store.job.get(job)
        if rt is None or rt.run_number != run_number or rt.status in _TERMINAL:
            continue  # superseded run, or its completion already replayed
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None:
            continue
        if job_ir.job_type == "FW":
            adapter = engine.adapters.get("FW")
            if adapter is None:
                raise EngineError(  # refuse loudly (review M4): never leave it hanging
                    f"incomplete FW run {job}.{run_number}: no FW adapter registered"
                    " to re-dispatch it"
                )
            engine._launch(job_ir, run_number, adapter)  # idempotent read
            continue
        result, ended_at = await _resolve_spool(
            job,
            run_number,
            run_dir,
            boot_now,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
        )
        extras: dict[str, object]
        if isinstance(result, int):
            extras = {"exit_code": result}
        elif isinstance(result, Terminated):
            extras = {"status": "TERMINATED", "cause": result.cause}
        else:
            extras = {"status": "FAILURE", "cause": result.cause}
        if ended_at is not None:
            extras["ended_at"] = ended_at.isoformat()  # true end time (ss7)
        _inject(job, run_number, extras, ended_at or last_at)

    # starts with no spool trace at all (crash between feed and spawn):
    # provably never spawned a wrapper -- FAILURE, never a silent re-run
    for job, rt in engine.oracle.store.job.items():
        if rt.status not in ("STARTING", "RUNNING") or (job, rt.run_number) in candidates:
            continue
        job_ir = engine.oracle.catalog.jobs.get(job)
        if job_ir is None or job_ir.job_type == "BOX":
            continue  # boxes fold from members; pseudo-entries have no dispatch
        if job_ir.job_type == "FW":
            adapter = engine.adapters.get("FW")
            if adapter is None:
                raise EngineError(
                    f"incomplete FW run {job}.{rt.run_number}: no FW adapter registered"
                    " to re-dispatch it"
                )
            engine._launch(job_ir, rt.run_number, adapter)
            continue
        if job_ir.job_type not in engine.adapters:
            continue  # no dispatch row live either: parity with the running engine
        _inject(
            job,
            rt.run_number,
            {"status": "FAILURE", "cause": "dispatch lost to engine crash (never spawned)"},
            last_at,
        )


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
    spawn = _load_json(run_dir / "spawn.json")
    if spawn is not None and not (
        spawn.get("job") == job and spawn.get("run_number") == run_number
    ):
        spawn = None  # spoofed/corrupt spawn record: never trust, never signal
    status = _load_json(status_path)
    if status is None and spawn is not None and spawn.get("boot_id") == boot_now:
        # same boot: liveness checks mean something (DL-42 item 5)
        wrapper_pid = spawn.get("wrapper_pid")
        wrapper_token = spawn.get("wrapper_start_time")
        if (
            isinstance(wrapper_pid, int)
            and isinstance(wrapper_token, str)
            and _wrapper.verify_alive(wrapper_pid, wrapper_token)
        ):
            # the wrapper is mid-grace (its own parent-loss kill is running):
            # give its status.json a settle window
            deadline = time.monotonic() + settle_seconds + grace_seconds
            while time.monotonic() < deadline:
                status = _load_json(status_path)
                if status is not None:
                    break
                if not _wrapper.verify_alive(wrapper_pid, wrapper_token):
                    status = _load_json(status_path)  # one last read after death
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
                and _wrapper.verify_alive(command_pid, command_token)
            ):
                # command group survived its recorder: kill the verified
                # leader's group -- TERMINATED is truthful (a kill happened)
                _killpg_quiet(command_pgid, signal.SIGTERM)
                deadline = time.monotonic() + grace_seconds
                while time.monotonic() < deadline:
                    if not _wrapper.verify_alive(command_pid, command_token):
                        break
                    await asyncio.sleep(0.1)
                else:
                    _killpg_quiet(command_pgid, signal.SIGKILL)
                return Terminated("wrapper lost; killed at resume"), None
    if status is not None:
        ended_at = status.get("ended_at")
        return (
            _outcome_from_status(status),
            _naive_utc(ended_at) if isinstance(ended_at, str) else None,
        )
    return Failed("exit_status_unobservable"), None  # PENDING: E7
