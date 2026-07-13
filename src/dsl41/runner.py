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

Phase 11c (ss5, ss8, ss10; DL-45 pins the decisions):

- Scheduler (ss5): the calendar the oracle deliberately lacks. Per
  date_conditions job it computes the next occurrence from days_of_week +
  start_times/start_mins (absent days_of_week = every day; per-job
  zoneinfo timezone, else the run-level base zone, else UTC -- both
  defaults PENDING: E10) and hands the engine STARTJOB events at the tick,
  timestamped at the tick and journaled like any input (source=scheduler).
  It fires UNCONDITIONALLY: SEM-32 abandonment (Q3) and SEM-33 run_window
  stay oracle-side. While the engine is up a late tick still fires (event
  stamped at the tick); across downtime missed ticks are dropped AND
  journaled at resume, never fired late (PENDING: E9).
- Engine loop commit discipline (DL-45): in the real domain the loop
  commits to work -- journaling an advance, popping a scheduler tick,
  feeding an event -- only once its instant is due (<= now); anything
  earlier is waited for INTERRUPTIBLY so a control injection or adapter
  completion arriving mid-wait re-plans the iteration. 11b journaled the
  advance and then slept uninterruptibly, so a completion stamped inside
  the sleep fed behind the already-advanced oracle clock and crashed the
  engine (feed time went backwards); regression-pinned. Virtual-domain
  jumps never yield mid-move, so the 11a determinism pins are unchanged.
- Preflight (ss8): ERROR refuses the run (job-type / machine / owner /
  calendar / timezone / oracle construction), WARN prints + journals and
  runs (n-retrys Q4, resources, AND-success skeleton cycle -- cycles are
  legal AutoSys, DL-13/L010, so they only disable `plan`). Identity rules
  (machine/owner) guard real execution and are skipped for rehearse
  (execution=False): the FakeAdapter runs nothing.
- Control plane (ss10): unix socket in the run root, mode 0600, JSON
  lines. sendevent parity verbs map 1:1 onto oracle EventKind and are
  injected source=control (journaled by the take_event path like every
  input; the engine's single-writer loop serializes them -- deliberately
  no controller lease here, DL-41a). Queries (status/trace/explain/plan)
  read the oracle store between feeds -- safe because feed() never yields.
  subscribe streams journal records live (at-least-once for unsequenced
  dispatch/drop records during the backfill race; seq'd records exactly
  once). A stale socket file from a crashed run is detected by a probe
  connect and unlinked; a LIVE socket refuses the second engine.

Phase 11d (ss11; DL-46 pins the decisions; runner_tui.py's docstring is the
TUI-side normative detail):

- The ss10 status response grows two read-only fields per job, both for the
  ss11 views and useful to any headless `query status` consumer:
  `pending_timers` -- (due, kind) pairs from Oracle.pending_timers(), whose
  liveness filter mirrors the fire-time staleness rules (display truth is
  the dispatch truth: a heap entry a fire would discard as stale is not
  shown as pending) -- and `log_out`/`log_err`, the ss6 append targets of
  the CURRENT run resolved by job_log_paths(), the one resolver the adapter
  wrapper spec also uses (the log tail reads what the wrapper writes; the
  two can never diverge). CMD-only; a never-started job reports only
  explicit std files (nothing else exists to tail).
- The protocol itself gains no verb: the TUI is `status` + `trace --since`
  + `explain` + `sendevent` + `subscribe`-as-wake-up, exactly the headless
  surface (DL-45: the TUI consumes the same ss10 protocol, idempotently).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import graphlib
import hashlib
import heapq
import json
import os
import signal
import socket as socket_mod
import subprocess
import sys
import time
import uuid

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, get_args
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from dsl41 import runner_supervisor as _supervisor
from dsl41 import runner_wrapper as _wrapper
from dsl41.conditions import And, Cond, Paren, StatusAtom, iter_atoms
from dsl41.ir import CatalogIR, ExecSpec, FwSpec, JobIR, MachineIR, ScheduleBlock, _unquote
from dsl41.oracle import _TERMINAL, Event, EventKind, JobRuntime, JobStatus, Oracle, OracleError

#: the ss6a Tier-0 shim, executed by file path (never -m; see its docstring)
_WRAPPER_PATH = Path(_wrapper.__file__)
#: the ss6a Tier-1 supervisor, likewise run by file path (stdlib-only boundary)
_SUPERVISOR_PATH = Path(_supervisor.__file__)


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
    input / advance / dispatch / drop / preflight (module docstring covers
    why dispatch is audit-only and why advances are inputs; preflight keeps
    the ss8 WARN caveats next to the run). fsync per record in the
    real domain (write-ahead: append + fsync BEFORE feed); buffered in
    rehearse, fsync on close. macOS caveat, accepted: os.fsync does not
    force the drive cache (F_FULLFSYNC would, at a large cost)."""

    def __init__(self, path: Path | str, *, fsync_each: bool, start_seq: int = 0) -> None:
        self.path = Path(path)
        self._f = self.path.open("ab")
        self._fsync_each = fsync_each
        self.seq = start_seq
        #: live feeds for ss10 subscribe: every appended record is fanned out
        #: post-write; queues are unbounded (a slow subscriber buffers, the
        #: WAL never blocks on one)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

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

    def preflight(self, items: list[PreflightItem]) -> None:
        """ss8: WARN prints, JOURNALS, and runs -- the record keeps the run's
        stated caveats next to its inputs. Replay ignores it (not an input);
        read_journal carries it like any other record."""
        self._write(
            {
                "rec": "preflight",
                "items": [item.model_dump() for item in items],
            }
        )

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _write(self, record: dict[str, Any]) -> None:
        self._f.write(json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")
        self._f.flush()
        if self._fsync_each:
            os.fsync(self._f.fileno())
        for queue in self._subscribers:
            queue.put_nowait(record)

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
    supervisor) and re-ACQUIRE with the stable controller_id, whose fresh
    fencing token fences anything the poisoned connection had in flight."""

    #: engine defaults (spec ss2): 60s lease, renewed every 20s
    _TTL_S = 60.0
    _RENEW_EVERY_S = 20.0

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.sock_path = run_root / "supervisor.sock"
        # STABLE per run_root, not per process: a crashed engine's lease is
        # unexpired for up to ttl_s, and resume must re-ACQUIRE without waiting
        # it out. Same controller_id => allowed, minting a fresh token that
        # fences the dead engine's stale one (spec ss2). One run_root has one
        # logical engine controller (the ss10 control-socket gate enforces it).
        self.controller_id = f"engine:{run_root.resolve()}"
        self.token: int | None = None
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
            reader, writer = await asyncio.open_unix_connection(str(self.sock_path))
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
        return bool(resp.get("ok"))

    def _spawn_supervisor(self) -> None:
        logf = (self.run_root / "supervisor.log").open("ab")
        try:
            subprocess.Popen(
                [sys.executable, str(_SUPERVISOR_PATH), "--run-root", str(self.run_root)],
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
                        },
                        _connect=False,
                    )
                except SupervisorUnavailable:
                    return False
                if not resp.get("ok"):
                    return False  # fenced out: another controller holds the lease
                self.token = int(resp["token"])
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
                    json.dumps({**obj, "v": 1}, sort_keys=True).encode("utf-8") + b"\n"
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
            {"cmd": "ACQUIRE", "controller_id": self.controller_id, "ttl_s": ttl}
        )
        if not resp.get("ok"):
            raise SupervisorUnavailable(f"lease acquire refused: {resp}")
        self.token = int(resp["token"])
        if self._renew_task is None:
            self._renew_task = asyncio.ensure_future(self._renew_loop(ttl))
        return self.token

    async def _renew_loop(self, ttl_s: float) -> None:
        """Keep the lease alive (spec ss2: renew every 20s). Review fix
        (DL-48): a transient failure must not silently lapse a live engine's
        lease -- pushes would stop with only the adapters' status.json
        re-poll saving outcomes. Failed RENEWs retry on a short backoff; a
        stale/lapsed token re-ACQUIREs with the stable controller_id (fresh
        fencing token); connection loss heals via _request's lazy reconnect.
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
                        # stale_token (fenced by a reconnect's own re-ACQUIRE,
                        # or lapsed): same controller re-acquires, fresh token
                        resp = await self._request(
                            {"cmd": "ACQUIRE", "controller_id": self.controller_id, "ttl_s": ttl_s}
                        )
                        if resp.get("ok"):
                            self.token = int(resp["token"])
                except SupervisorUnavailable:
                    resp = {"ok": False}
                if resp.get("ok"):
                    failures = 0
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
            return await self._await_outcome(reattach_id, run_dir, job_ir.name, run_number)
        run_dir, spec = _build_run_spec(job_ir, run_number, ctx, grace_seconds=self.grace_seconds)
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
        try:
            return await self._await_outcome(run_id, run_dir, job_ir.name, run_number)
        except asyncio.CancelledError:
            if ctx.detach is not None and ctx.detach.stopping:
                raise  # ss3 case b: the job continues under the supervisor
            await self._kill(run_id)  # ss3 case a: the oracle decided terminal
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
                status = _load_json(status_path)
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
                    status = _load_json(status_path)
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
            _wrapper.current_boot_id(),
            settle_seconds=self.settle_seconds,
            grace_seconds=self.grace_seconds,
        )
        return result

    async def _kill(self, run_id: str) -> None:
        """ss3 case a: the oracle said terminal. TERM the command group via the
        supervisor, grace, KILL, await the exit push so the wrapper records --
        the cancelled adapter itself never reports (the oracle already emitted
        the terminal)."""
        fut = self.client.exit_future(run_id)
        try:
            await self.client.signal(run_id, "TERM")
        except SupervisorUnavailable:
            return  # supervisor gone: the wrapper's own lifeline handles it
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(SupervisorUnavailable):
                await self.client.signal(run_id, "KILL")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(fut), timeout=self.grace_seconds)
        finally:
            self.client.forget_exit(run_id)


@dataclass
class _LiveRun:
    run_number: int
    task: asyncio.Task[None]


def _raise_if_failed(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc  # adapter bug: fail loudly, never guess


# ------------------------------------------------------------------ scheduler (ss5)

#: Python date.weekday() (Monday=0) -> JIL day token (ir._DAY_TOKENS)
_DAY_CODES = ("mo", "tu", "we", "th", "fr", "sa", "su")


@dataclass(frozen=True)
class _SchedulePlan:
    """One job's compiled trigger: eligible day tokens, sorted (hour, minute)
    ticks per eligible day, and the resolved zone (None = the engine's naive
    UTC basis directly)."""

    days: frozenset[str]
    times: tuple[tuple[int, int], ...]
    tz: ZoneInfo | None

    def day_eligible(self, day: date) -> bool:
        return _DAY_CODES[day.weekday()] in self.days

    def utc_ticks_on(self, day: date) -> list[datetime]:
        """This local day's ticks as naive-UTC instants (the engine basis)."""
        ticks = []
        for hour, minute in self.times:
            naive_local = datetime(day.year, day.month, day.day, hour, minute)
            ticks.append(
                naive_local.replace(tzinfo=self.tz).astimezone(UTC).replace(tzinfo=None)
                if self.tz
                else naive_local
            )
        return ticks


class Scheduler:
    """ss5: the calendar the oracle deliberately lacks. Computes per-job next
    occurrences from the ScheduleBlock and yields STARTJOB events at the
    tick; it fires unconditionally (SEM-32 abandonment and SEM-33 run_window
    stay oracle-side, exactly as in simulation). Ticks are naive-UTC instants
    (the engine's time basis): per-job `timezone` -- else `default_tz`, else
    UTC -- is applied via zoneinfo, so rehearse under the virtual clock
    exercises real calendar arithmetic (ss5).

    Pinned interpretation defaults (PENDING: E10): absent days_of_week means
    every day; jobs without `timezone` read their times in `default_tz`
    (run-level --timezone), defaulting to UTC -- vendor uses the server's
    zone. DST corners follow PEP 495 fold=0: a fall-back ambiguous time is
    its first occurrence, a spring-forward nonexistent time maps past the
    gap. Schedule blocks with neither start_times nor start_mins trigger
    nothing (run_window/SLA are gates/alarms, not triggers); run_calendar /
    exclude_calendar jobs are skipped here because preflight refuses the run
    before a Scheduler exists (ss8)."""

    def __init__(
        self, catalog: CatalogIR, *, start: datetime, default_tz: str | None = None
    ) -> None:
        base_tz = ZoneInfo(default_tz) if default_tz else None
        self._plans: dict[str, _SchedulePlan] = {}
        for name, job in catalog.jobs.items():
            sched = job.schedule
            if sched is None or not (sched.start_times or sched.start_mins):
                continue
            if sched.run_calendar or sched.exclude_calendar:
                continue  # preflight ERROR territory; never silently guessed here
            if sched.days_of_week is not None and not sched.days_of_week:
                # lowering rejects an empty list; a hand-built IR carrying one
                # would exhaust _occurrence's scan -- refuse comprehensibly
                raise EngineError(f"{name}: days_of_week is empty; nothing to schedule")
            self._plans[name] = _SchedulePlan(
                days=frozenset(
                    _DAY_CODES
                    if (sched.days_of_week is None or "all" in sched.days_of_week)
                    else sched.days_of_week
                ),
                times=self._ticks(sched),
                tz=ZoneInfo(sched.timezone) if sched.timezone else base_tz,
            )
        self._next: dict[str, datetime] = {}
        self.reset(start)

    @staticmethod
    def _ticks(sched: ScheduleBlock) -> tuple[tuple[int, int], ...]:
        if sched.start_times:
            return tuple(sorted((t.hour, t.minute) for t in sched.start_times))
        return tuple(sorted((h, m) for h in range(24) for m in sched.start_mins or []))

    def reset(self, start: datetime, *, inclusive: bool = True) -> None:
        """Re-anchor every job's next tick at or (inclusive=False) strictly
        after `start`. Resume uses the exclusive form anchored at the last
        journal instant: a tick exactly there was already fed by replay."""
        self._next = {
            job: self._occurrence(plan, start, inclusive=inclusive)
            for job, plan in self._plans.items()
        }

    def next_occurrence(self) -> datetime | None:
        """Earliest pending tick across all jobs (naive UTC), or None."""
        return min(self._next.values(), default=None)

    def pop_due(self, upto: datetime) -> list[Event]:
        """Consume every tick due at or before `upto` and return its STARTJOB
        event, stamped at the tick and ordered by (tick, job). A stalled-but-
        alive engine therefore fires its backlog late but truthfully stamped;
        ticks missed across DOWNTIME never reach this path -- resume drops
        and journals them instead (PENDING: E9)."""
        due: list[tuple[datetime, str]] = []
        for job, tick in self._next.items():
            while tick <= upto:
                due.append((tick, job))
                tick = self._occurrence(self._plans[job], tick, inclusive=False)
            self._next[job] = tick
        due.sort()
        return [Event(at=tick, kind="STARTJOB", payload={"job": job}) for tick, job in due]

    @staticmethod
    def _occurrence(plan: _SchedulePlan, t: datetime, *, inclusive: bool) -> datetime:
        # calendar-date iteration (never aware-datetime + timedelta: absolute
        # arithmetic can skip a 25h fall-back local date); per-day ticks are
        # sorted AFTER conversion because a fold=0 nonexistent time can land
        # past a later tick's UTC instant inside a spring-forward gap
        anchor_date = (t.replace(tzinfo=UTC).astimezone(plan.tz) if plan.tz else t).date()
        for offset in range(371):  # a non-empty day set always hits within 7
            day = anchor_date + timedelta(days=offset)
            if not plan.day_eligible(day):
                continue
            for utc_tick in sorted(plan.utc_ticks_on(day)):
                if utc_tick > t or (inclusive and utc_tick == t):
                    return utc_tick
        raise EngineError("no scheduler occurrence within a year (unreachable: validated block)")


class Engine:
    """ss4 single-writer engine loop over one Oracle. 11a surface: inject()
    external events + run_until_quiescent(horizon). The WAL journal slots in
    front of every feed (journal-first, ss7); the ss5 scheduler and the ss10
    control socket are the 11c event sources. `hold_open` keeps a real-domain
    loop waiting at quiescence instead of returning -- run mode serves the
    control socket until stopped, so "no work now" never means "no work can
    arrive" (ss10)."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        clock: Clock,
        adapters: Mapping[str, JobAdapter],
        journal: Journal | None = None,
        run_root: Path | None = None,
        scheduler: Scheduler | None = None,
        hold_open: bool = False,
    ) -> None:
        self.oracle = Oracle(catalog)
        self.clock = clock
        self.adapters = dict(adapters)  # job_type -> adapter; no BOX row
        self.journal = journal
        self.run_root = run_root
        self.scheduler = scheduler
        self.hold_open = hold_open
        #: flipped by the CLI before a detach-stop cancels adapter tasks (ss3
        #: case b): the SupervisedCommandAdapter then abandons instead of killing
        self.detach = DetachSignal()
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
        """Queue an external event (test scripts; ss10 sendevent verbs).
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
            sched_due = self.scheduler.next_occurrence() if self.scheduler is not None else None
            # commit discipline (DL-45): the real domain commits to work only
            # once its instant is due -- an earlier instant is waited for
            # interruptibly in the tail branch, so a control injection or
            # completion arriving mid-wait re-plans instead of feeding behind
            # an already-journaled advance. Virtual jumps never yield, so the
            # 11a determinism pins are untouched by the extra gates.
            take_event = (
                head_at is not None
                and head_at <= horizon
                and (eff_due is None or head_at <= eff_due)
                and (sched_due is None or head_at <= sched_due)
                and (self.clock.virtual or head_at <= now)
            )
            take_sched = (
                not take_event
                and sched_due is not None
                and sched_due <= horizon
                and (head_at is None or sched_due < head_at)
                and (eff_due is None or sched_due <= eff_due)
                and (self.clock.virtual or sched_due <= now)
            )
            fire_timer = (
                not take_event
                and not take_sched
                and raw_due is not None
                and eff_due is not None
                and eff_due <= horizon
                and (raw_due > now or horizon > now)
                and (self.clock.virtual or eff_due <= now)
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
            elif take_sched:
                # the calendar tick is next: enqueue its STARTJOB(s), stamped
                # at the tick, and let the next iteration take them like any
                # external input (journal-first at feed; feed() fires timers
                # due <= tick first, identical to oracle-direct scripts)
                assert sched_due is not None and self.scheduler is not None
                await self.clock.wait_until(sched_due)
                for tick_ev in self.scheduler.pop_due(sched_due):
                    self._enqueue(tick_ev, is_completion=False, source="scheduler")
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
            elif self.clock.virtual or (
                not self.hold_open
                and not self._live
                and not self._queue
                and raw_due is None
                and sched_due is None
            ):
                # virtual quiescence: nothing can move without the clock;
                # real quiescence: no work exists and none can appear --
                # unless hold_open, where the control socket can always
                # produce more (run mode waits instead of returning)
                return emitted
            else:
                # real domain: block until queue activity or the next due
                # instant; a completed adapter task also fires _activity so
                # _settle can re-raise adapter failures promptly. Future-due
                # work routes here too (commit discipline above): the wait is
                # interruptible, the committed branches never sleep.
                next_wake = [t for t in (eff_due, head_at, sched_due) if t is not None]
                target = min(next_wake, default=None)
                if target is not None and target > horizon:
                    # nothing KNOWN this side of the horizon -- but a live
                    # adapter's completion has no due timestamp and can still
                    # land inside it, so with live tasks wait out the horizon
                    # instead of abandoning them (DL-45 review T2; the
                    # completion-at-horizon contract predates 11c)
                    if not self._live or now >= horizon:
                        return emitted
                    target = horizon
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
        ctx = AdapterContext(
            clock=self.clock,
            run_root=self.run_root,
            journal=self.journal,
            detach=self.detach,
        )
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
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
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
    return Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
    )


async def resume_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    settle_seconds: float = 5.0,
    grace_seconds: float = 10.0,
    supervisor: SupervisorClient | None = None,
) -> Engine:
    """ss7 resume: hash-gate, replay, reconcile. Returns an Engine with the
    reconciliation completions queued (source=reconcile); the caller runs
    the loop to process them and continue the run.

    A `scheduler` is re-anchored at the last journal instant INCLUSIVE and
    deduped against the journal's own scheduler ticks (a crash between
    same-instant siblings' appends must lose none of them silently, review
    B2); the unjournaled remainder of the window up to wall-now was missed
    across downtime and is dropped AND journaled -- reported on
    Engine.drops, never fired late (PENDING: E9; a live-but-stalled engine
    fires its backlog, downtime never does)."""
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
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
    )
    replay_inputs(engine.oracle, records)
    # seed the ghost-run gate: replayed starts are reconciliation's business,
    # never a fresh dispatch
    for job, rt in engine.oracle.store.job.items():
        if rt.run_number:
            engine._dispatched[job] = rt.run_number
    if scheduler is not None:
        # re-anchor INCLUSIVE of last_at and dedup against the ticks the
        # journal actually holds: with several jobs scheduled at one instant,
        # a crash between the siblings' input appends leaves last_at == tick
        # with a sibling unjournaled -- an exclusive re-anchor would lose it
        # silently, with no drop record (DL-45 review B2). Journaled ticks
        # were fed by replay and are skipped; the rest of the due window is
        # dropped AND journaled, never fired late.
        replayed_ticks = {
            (record["payload"].get("job"), record["at"])
            for record in records
            if record.get("rec") == "input"
            and record.get("source") == "scheduler"
            and record.get("kind") == "STARTJOB"
        }
        scheduler.reset(last_at, inclusive=True)
        sweep_upto = max(clock.now(), last_at)  # virtual resume: now < last_at
        for tick_ev in scheduler.pop_due(sweep_upto):
            if (tick_ev.job(), tick_ev.at.isoformat()) in replayed_ticks:
                continue  # replay already fed this tick
            reason = "scheduler tick missed while the engine was down; not fired late"
            engine.drops.append((tick_ev, reason))  # PENDING: E9
            journal.drop(tick_ev, reason)
    await _reconcile(
        engine,
        records,
        last_at,
        settle_seconds=settle_seconds,
        grace_seconds=grace_seconds,
        supervisor=supervisor,
    )
    return engine


async def _reconcile(
    engine: Engine,
    records: list[dict[str, Any]],
    last_at: datetime,
    *,
    settle_seconds: float,
    grace_seconds: float,
    supervisor: SupervisorClient | None = None,
) -> None:
    """The ss6a/ss7 reconciliation ladder (module docstring). Tethered
    semantics did the killing already (wrappers EOF'd when the engine
    died), so this is mostly READING; signals are for the residual crash
    matrix only, and only ever at a (pid, start-time)-verified target.

    Detached resume (spec ss3): with a `supervisor`, an in-flight run the
    supervisor still LISTs as wrapper_alive is REATTACHED -- the adapter task
    just awaits its exit push, no reconciliation injection (the run never
    stopped, E4 dissolved). Runs listed dead or unlisted fall through to the
    spool ladder unchanged (the supervisor died, or the run predates it)."""
    assert engine.run_root is not None
    boot_now = _wrapper.current_boot_id()
    supervised_live: dict[tuple[str, int], dict[str, Any]] = {}
    if supervisor is not None:
        with contextlib.suppress(SupervisorUnavailable):
            listing = await supervisor.list_runs()
            supervised_live = {
                (str(r["job"]), int(r["run_number"])): r for r in listing.get("runs", [])
            }
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
        reattach = supervised_live.get((job, run_number))
        if reattach is not None and reattach.get("wrapper_alive"):
            cmd_adapter = engine.adapters.get(job_ir.job_type)
            if isinstance(cmd_adapter, SupervisedCommandAdapter):
                # REATTACH: the run's parent (the supervisor) never died, so it
                # never stopped -- the adapter task just awaits its exit push,
                # NO reconciliation injection (spec ss3)
                cmd_adapter.reattach[(job, run_number)] = str(reattach["run_id"])
                engine._launch(job_ir, run_number, cmd_adapter)
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


# ------------------------------------------------------------------ preflight (ss8)

#: the runner's executable universe; anything else is a preflight ERROR
_RUNNABLE_TYPES = frozenset({"CMD", "BOX", "FW"})


class PreflightItem(BaseModel):
    """One ss8 finding. ERROR refuses the run; WARN prints, journals, and
    runs. Codes are stable kebab keys (fixture pair per rule, ss8)."""

    severity: Literal["ERROR", "WARN"]
    code: str
    job: str | None = None
    message: str


def _local_identity(declared: frozenset[str] = frozenset()) -> frozenset[str]:
    """The machine names this runner answers to (DL-52). `localhost` always
    counts. When the operator DECLARES identities (`--as-machine greezy_spoon`),
    THOSE are the identity -- pure and explicit, no hostname/FQDN guessing: the
    runner knows which estate machine it is, and a job pinned to that name (or
    resolving to it through insert_machine) runs here. Omitted, we fall back to
    the forward hostname (short + full) for zero-config, but NEVER reverse-DNS:
    `getfqdn()` can stall for tens of seconds and, worse, decides placement from
    what the OS resolver thinks this box is called -- a different namespace from
    the estate's machine names (DL-52 replaces DL-45 M6's FQDN matching)."""
    identity = {"localhost"}
    if declared:
        identity |= {d.strip().lower() for d in declared if d.strip()}
        return frozenset(identity)
    hostname = socket_mod.gethostname().lower()
    identity |= {hostname, hostname.split(".")[0]}
    return frozenset(identity)


#: Machine `type:` values the resolver understands (DL-49). Anything else --
#: including a missing type -- is refused, never guessed (Goal-1 plan, codex).
_KNOWN_MACHINE_TYPES = frozenset({"a", "r", "n", "v"})

MachineVerdict = Literal["local", "foreign", "mixed", "error"]

MachinePolicy = Literal["strict", "local-eligible"]


@dataclass(frozen=True)
class MachineResolution:
    """Outcome of resolving a job's `machine:` through `insert_machine`
    (DL-49). `detail` carries the resolved host (local/foreign), a human
    reason (error), or the pool summary (mixed) -- straight into the
    preflight message and, later, the run journal / dispatch router."""

    verdict: MachineVerdict
    detail: str


def _node_name(machine: MachineIR) -> str | None:
    """The machine's node_name, semantically unquoted (a hostname never
    carries quotes) -- node_name feeds host resolution, so it must not be
    read as the raw opaque carry (review: quoted node_name false-refused)."""
    raw = machine.attrs.get("node_name")
    return _unquote(raw) if raw else None


def _leaf_host(machine: MachineIR) -> tuple[str | None, str | None]:
    """Effective host of a NON-virtual machine: (host, error). Agents must
    carry node_name (no guessing); real machines fall back to the record
    name. A missing/unknown type is a refusal, not a default."""
    kind = (machine.machine_type or "").lower()
    if kind == "a":
        node = _node_name(machine)
        if not node:
            return None, f"agent machine {machine.name!r} has no node_name to resolve its host"
        return node, None
    if kind in ("r", "n"):
        return (_node_name(machine) or machine.name), None
    if kind == "":
        return None, f"machine {machine.name!r} has no type (add type: a|r|n|v)"
    return None, f"machine {machine.name!r} has unsupported type {machine.machine_type!r}"


def resolve_machine(
    name: str, machines: Mapping[str, MachineIR], local: frozenset[str]
) -> MachineResolution:
    """Resolve a job's `machine:` name to a placement verdict against this
    runner's declared identity (DL-52 `local`), honouring `insert_machine`
    (DL-49). A DIRECT name match wins first: if the job names a machine this
    runner answers to, run here -- the operator's "this runner IS greezy_spoon"
    is authoritative over whatever node_name the record carries. Otherwise
    agents/real machines resolve through node_name; a virtual machine is local
    iff ALL members resolve local, foreign iff none, else mixed. Undefined and
    not-our-name is foreign. Bad definitions (missing type, undefined / nested /
    typeless member, empty pool) are `error`, never guessed."""
    if name.lower() in local:
        return MachineResolution("local", name)  # DL-52: named identity match
    machine = machines.get(name)
    if machine is None:
        return MachineResolution("foreign", name)  # undefined and not our name
    kind = (machine.machine_type or "").lower()
    if kind and kind not in _KNOWN_MACHINE_TYPES:
        return MachineResolution("error", f"machine {name!r} has unsupported type {machine.machine_type!r}")
    if kind != "v":
        host, err = _leaf_host(machine)
        if err is not None:
            return MachineResolution("error", err)
        assert host is not None
        return MachineResolution("local" if host.lower() in local else "foreign", host)
    # virtual machine: resolve every member leaf, then fold (any-of).
    if not machine.members:
        return MachineResolution("error", f"virtual machine {name!r} has no member machines")
    members_local: list[bool] = []
    for member in machine.members:
        leaf = machines.get(member.name)
        if leaf is None:
            return MachineResolution(
                "error", f"virtual machine {name!r} member {member.name!r} is not defined"
            )
        if (leaf.machine_type or "").lower() == "v":
            return MachineResolution(
                "error", f"virtual machine {name!r} member {member.name!r} is itself virtual (no nesting)"
            )
        host, err = _leaf_host(leaf)
        if err is not None:
            return MachineResolution("error", f"virtual machine {name!r}: {err}")
        assert host is not None
        members_local.append(host.lower() in local)
    count = len(machine.members)
    if all(members_local):
        return MachineResolution("local", f"virtual pool {name!r} ({count} member(s), all local)")
    if not any(members_local):
        return MachineResolution("foreign", f"virtual pool {name!r} ({count} member(s), none local)")
    return MachineResolution("mixed", f"virtual pool {name!r} ({count} member(s), some remote)")


def and_success_skeleton(catalog: CatalogIR) -> dict[str, set[str]]:
    """job -> success-predecessors reachable through AND/Paren spines only
    (an s() atom under an OR is an alternative, not a hard dependency).
    Instance-qualified and undefined references are skipped: pseudo-entries
    have no run to order. Shared by the ss8 cycle WARN and the ss10 `plan`
    view, so the two can never disagree about acyclicity."""

    def collect(cond: Cond, into: set[str]) -> None:
        if isinstance(cond, And):
            for op in cond.operands:
                collect(op, into)
        elif isinstance(cond, Paren):
            collect(cond.inner, into)
        elif (
            isinstance(cond, StatusAtom)
            and cond.status == "SUCCESS"
            and cond.job.instance is None
            and cond.job.name in catalog.jobs
        ):
            into.add(cond.job.name)

    skeleton: dict[str, set[str]] = {}
    for name, job in catalog.jobs.items():
        preds: set[str] = set()
        if job.sem.condition is not None:
            collect(job.sem.condition, preds)
        skeleton[name] = preds
    return skeleton


def _resource_preflight(name: str, job: JobIR, catalog: CatalogIR) -> list[PreflightItem]:
    """DL-50: resource/load attributes are HONORED by the oracle (capacity
    buckets, QUE_WAIT, deterministic admission). Preflight refuses -- fail-
    closed -- only the shapes the oracle cannot model faithfully, in BOTH run
    and rehearse (resource semantics gate the oracle in either clock domain):
      * a `resources:` requirement whose resource is unsized (no insert_resource
        in the set, or no parseable `amount`) -- an unsized semaphore cannot be
        honored (stricter than L016's warn; a `--resource-capacity` override is
        a documented future escape hatch, not v1);
      * an unknown res_type (not R/D/T) -- unknown release semantics;
      * a malformed job_load/priority/max_load -- a non-integer load.
    It WARNs (does not refuse) a job_load on a pool machine, where per-member
    load placement is unmodelled (# PENDING: Qr3) -- resource semaphores on
    such a job still apply. Cross-machine shared locks need no separate refusal:
    the DL-49 foreign-machine ERROR already makes every runnable job local, so
    every honored resource is contended on this one host (revisit if distributed
    execution lands, DL-49 future track)."""
    items: list[PreflightItem] = []

    def err(message: str) -> None:
        items.append(PreflightItem(severity="ERROR", code="resources", job=name, message=message))

    for accessor in (job.job_load_units, job.priority_value):
        try:
            accessor()  # the accessor's ValueError names the offending attribute
        except ValueError as exc:
            err(str(exc))
    spec = job.exec_
    if spec is not None and spec.machine is not None:
        machine = catalog.machines.get(spec.machine)
        if machine is not None:
            try:
                machine.max_load_units()
            except ValueError as exc:
                err(f"machine {spec.machine!r}: {exc}")
            try:
                load = job.job_load_units()
            except ValueError:
                load = None  # already reported above
            if machine.members and load:
                items.append(
                    PreflightItem(
                        severity="WARN",
                        code="resources",
                        job=name,
                        message=f"job_load={load} on pool machine {spec.machine!r}:"
                        " machine-load throttle unmodelled (PENDING Qr3);"
                        " resource semaphores still apply",
                    )
                )
    seen: set[str] = set()
    for ref in job.resources:
        if ref.name in seen:
            err(f"resources: {ref.name!r} requested more than once -- ambiguous demand (DL-50)")
        seen.add(ref.name)
        resource = catalog.resources.get(ref.name)
        if resource is None:
            err(
                f"resources: {ref.name!r} has no insert_resource in the set --"
                " cannot size the semaphore (DL-50; supply the definition"
                " or drop the requirement)"
            )
            continue
        try:
            capacity = resource.capacity_units()
        except ValueError as exc:
            err(f"resource {ref.name!r}: {exc}")
            continue
        if capacity is None:
            err(f"resource {ref.name!r} has no `amount` -- cannot size the semaphore (DL-50)")
            continue
        if ref.quantity > capacity:
            err(
                f"resources: {ref.name!r} QUANTITY={ref.quantity} exceeds its amount={capacity}"
                " -- can never be satisfied, the job would hang in QUE_WAIT forever (DL-50)"
            )
        res_type = (resource.res_type or "").strip().upper()
        if res_type not in ("", "R", "D", "T"):
            err(
                f"resource {ref.name!r} res_type {resource.res_type!r} is not R/D/T --"
                " unknown release semantics (DL-50)"
            )
    return items


def preflight(
    catalog: CatalogIR,
    *,
    execution: bool = True,
    machine_policy: MachinePolicy = "strict",
    as_machine: frozenset[str] = frozenset(),
) -> list[PreflightItem]:
    """ss8: refuse loudly, run honestly. `execution=False` (rehearse) skips
    the machine/owner identity rules -- they guard real processes, and the
    FakeAdapter runs none -- while everything the scheduler and oracle
    depend on (calendars, timezones, construction) still gates.

    `as_machine` (DL-52) is the identity this runner answers to. Empty = the
    forward hostname (zero-config, no reverse-DNS); non-empty = EXACTLY those
    names plus localhost (the operator declared it -- no hostname guessing). A
    job whose `machine:` is (or resolves through insert_machine to) an identity
    name runs here; anything else is refused foreign.

    `machine_policy` (DL-49) governs the one ambiguous machine verdict: a
    virtual pool with SOME members on this host and some elsewhere. `strict`
    (default) refuses it -- placement among members is unmodelled;
    `local-eligible` runs it here with a WARN that pool placement was
    ignored. Unambiguous verdicts (all-local, none-local, bad definition)
    ignore the policy."""
    items: list[PreflightItem] = []
    local = _local_identity(as_machine)
    user = getpass.getuser()
    for name, job in sorted(catalog.jobs.items()):
        if job.job_type not in _RUNNABLE_TYPES:
            items.append(
                PreflightItem(
                    severity="ERROR",
                    code="job-type",
                    job=name,
                    message=f"job_type {job.job_type!r} has no adapter"
                    " (runner universe is CMD/BOX/FW)",
                )
            )
        spec = job.exec_
        if execution and spec is not None and spec.machine is not None:
            resolution = resolve_machine(spec.machine, catalog.machines, local)
            if resolution.verdict == "foreign":
                items.append(
                    PreflightItem(
                        severity="ERROR",
                        code="machine",
                        job=name,
                        message=f"machine {spec.machine!r} resolves to {resolution.detail},"
                        f" not this host (accepted: {', '.join(sorted(local))});"
                        " no remote fabric (ss12)",
                    )
                )
            elif resolution.verdict == "error":
                items.append(
                    PreflightItem(severity="ERROR", code="machine", job=name, message=resolution.detail)
                )
            elif resolution.verdict == "mixed":
                if machine_policy == "local-eligible":
                    items.append(
                        PreflightItem(
                            severity="WARN",
                            code="machine-mixed",
                            job=name,
                            message=f"{resolution.detail}: running here"
                            " (--machine-policy local-eligible); pool placement ignored",
                        )
                    )
                else:
                    items.append(
                        PreflightItem(
                            severity="ERROR",
                            code="machine",
                            job=name,
                            message=f"{resolution.detail}; refusing"
                            " (--machine-policy local-eligible to run it here)",
                        )
                    )
        if execution and spec is not None and spec.owner is not None and spec.owner != user:
            items.append(
                PreflightItem(
                    severity="ERROR",
                    code="owner",
                    job=name,
                    message=f"owner {spec.owner!r} is not the invoking user {user!r}"
                    " (no setuid in MVP, ss6)",
                )
            )
        sched = job.schedule
        if sched is not None and (sched.run_calendar or sched.exclude_calendar):
            items.append(
                PreflightItem(
                    severity="ERROR",
                    code="calendar",
                    job=name,
                    message="run_calendar/exclude_calendar reference calendar definitions"
                    " the IR does not model (ss5)",
                )
            )
        if sched is not None and sched.timezone is not None:
            try:
                ZoneInfo(sched.timezone)
            except (KeyError, ValueError, OSError):
                items.append(
                    PreflightItem(
                        severity="ERROR",
                        code="timezone",
                        job=name,
                        message=f"timezone {sched.timezone!r} is not resolvable in zoneinfo",
                    )
                )
        if job.sem.n_retrys > 0:
            items.append(
                PreflightItem(
                    severity="WARN",
                    code="n-retrys",
                    job=name,
                    message=f"n_retrys={job.sem.n_retrys}: runs WITHOUT retries (PENDING: Q4;"
                    " a shell-side retry would fork semantics from the oracle)",
                )
            )
        items.extend(_resource_preflight(name, job, catalog))
    try:
        Oracle(catalog)
    except OracleError as exc:
        items.append(
            PreflightItem(
                severity="ERROR",
                code="oracle",
                message=f"oracle construction failed: {exc}",
            )
        )
    try:
        graphlib.TopologicalSorter(and_success_skeleton(catalog)).prepare()
    except graphlib.CycleError as exc:
        items.append(
            PreflightItem(
                severity="WARN",
                code="skeleton-cycle",
                message="cycle in the AND-success skeleton"
                f" ({' -> '.join(exc.args[1])}): legal AutoSys (edge-triggered re-runs,"
                " DL-13/L010); `plan` is disabled for this estate",
            )
        )
    return items


# -------------------------------------------------------------- control plane (ss10)

#: sendevent verbs whose payload is a single catalog job (1:1 onto EventKind)
_JOB_EVENT_VERBS: frozenset[EventKind] = frozenset(
    {
        "STARTJOB",
        "FORCE_STARTJOB",
        "KILLJOB",
        "ON_ICE",
        "OFF_ICE",
        "ON_HOLD",
        "OFF_HOLD",
        "ON_NOEXEC",
        "OFF_NOEXEC",
    }
)
_STATUSES: frozenset[str] = frozenset(get_args(JobStatus))


class ControlServer:
    """ss10 control plane: a unix domain socket in the run directory, mode
    0600, JSON lines both ways. One request object per line; one response
    object per line ({"ok": bool, ...}), except `subscribe`, which streams
    journal records until the client hangs up.

    Verbs: {"cmd": "sendevent", "event": <verb>, ...} for the sendevent
    parity set (job verbs carry "job"; SET_GLOBAL carries "name"/"value";
    CHANGE_STATUS carries "job"/"status" and optional int "exit_code" --
    injected as STATUS, keeping overwrite parity). Queries: status [job],
    trace [since], explain job, plan; and subscribe [since]. Job arguments
    are validated against the catalog -- vendor sendevent errors on unknown
    jobs rather than queueing them.

    Injections go through Engine.inject (source=control), so the WAL
    journals every control input at feed time (ss10: the WAL is the audit
    trail; there is no second log) and the single-writer loop serializes
    them -- deliberately no controller lease at this tier (DL-41a).
    Queries read the oracle store directly: feed() never yields, so a
    handler task can never observe a half-applied event."""

    def __init__(self, engine: Engine, path: Path) -> None:
        self.engine = engine
        self.path = path
        self._server: asyncio.Server | None = None
        self._conn_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Bind (0600 from birth via umask) after the stale-socket probe: a
        connect() that succeeds means a LIVE engine serves this run root --
        refuse; a refused/failed connect means a crashed run's leftover --
        unlink and claim it."""
        if self.path.exists():
            probe = socket_mod.socket(socket_mod.AF_UNIX)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except OSError:
                self.path.unlink()  # stale: nobody is listening
            else:
                raise EngineError(f"{self.path} is live: another engine is serving this run root")
            finally:
                probe.close()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        except OSError as exc:
            # two engines racing past the probe: the loser's bind fails --
            # same refusal class as the live-socket case (review M9)
            raise EngineError(f"cannot bind control socket {self.path}: {exc}") from exc
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)  # belt: some platforms ignore umask on bind

    async def close(self) -> None:
        # cancel handlers BEFORE wait_closed(): since 3.12 wait_closed blocks
        # until every handler task finishes, and a subscribe handler is parked
        # on queue.get() until cancelled -- the reverse order deadlocks the
        # engine's shutdown whenever any viewer is attached (DL-45 review B1)
        if self._server is not None:
            self._server.close()
            # one tick: a connection accepted just before close spawns its
            # handler via a scheduled callback -- let it land in _conn_tasks
            # so the cancel sweep below reaches it too
            await asyncio.sleep(0)
        for task in list(self._conn_tasks):
            task.cancel()
        await asyncio.gather(*self._conn_tasks, return_exceptions=True)
        self._conn_tasks.clear()
        # one more tick: a cancelled handler's writer.close() only SCHEDULES
        # its connection_lost; without this the transport never detaches from
        # the server and its deallocator trips after the loop is gone
        await asyncio.sleep(0)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conn_tasks.add(task)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    await self._send(writer, {"ok": False, "error": f"bad request: {exc}"})
                    continue
                if request.get("cmd") == "subscribe":
                    await self._subscribe(writer, request)
                    break  # a subscription owns its connection until hangup
                try:
                    response = self._respond(request)
                except Exception as exc:  # noqa: BLE001 -- a query bug must
                    # answer ok:false, never kill the connection unreplied
                    # (the client would only see a timeout; DL-45 review M5)
                    response = {"ok": False, "error": f"internal error: {exc!r}"}
                await self._send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass  # client hangup mid-write: its problem, not the engine's
        finally:
            if task is not None:
                self._conn_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write(json.dumps(obj, sort_keys=True).encode("utf-8") + b"\n")
        await writer.drain()

    def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "sendevent":
            return self._sendevent(request)
        if cmd == "status":
            return self._status(request)
        if cmd == "trace":
            return self._trace(request)
        if cmd == "explain":
            return self._explain(request)
        if cmd == "plan":
            return self._plan()
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    def _check_job(self, job: object) -> dict[str, Any] | None:
        if isinstance(job, str) and job in self.engine.oracle.catalog.jobs:
            return None
        return {"ok": False, "error": f"unknown job {job!r}"}

    def _sendevent(self, request: dict[str, Any]) -> dict[str, Any]:
        verb = request.get("event")
        at = self.engine.clock.now()
        if verb in _JOB_EVENT_VERBS:
            job = request.get("job")
            if (error := self._check_job(job)) is not None:
                return error
            ev = Event(at=at, kind=verb, payload={"job": job})
        elif verb == "SET_GLOBAL":
            name, value = request.get("name"), request.get("value")
            if not (isinstance(name, str) and name):
                return {"ok": False, "error": "SET_GLOBAL requires a global name"}
            if not isinstance(value, str):
                return {"ok": False, "error": "SET_GLOBAL requires a string value"}
            ev = Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": value})
        elif verb == "CHANGE_STATUS":
            job, status = request.get("job"), request.get("status")
            if (error := self._check_job(job)) is not None:
                return error
            if status not in _STATUSES:
                return {
                    "ok": False,
                    "error": f"unknown status {status!r} (one of {sorted(_STATUSES)})",
                }
            payload: dict[str, object] = {"job": job, "status": status}
            if "exit_code" in request:
                if not isinstance(request["exit_code"], int):
                    return {"ok": False, "error": "exit_code must be an integer"}
                payload["exit_code"] = request["exit_code"]
            ev = Event(at=at, kind="STATUS", payload=payload)
        else:
            return {"ok": False, "error": f"unknown event {verb!r}"}
        self.engine.inject(ev, source="control")
        return {"ok": True, "kind": ev.kind, "at": at.isoformat()}

    def _status(self, request: dict[str, Any]) -> dict[str, Any]:
        catalog = self.engine.oracle.catalog
        store = self.engine.oracle.store
        job = request.get("job")
        if job is not None:
            if not isinstance(job, str) or (job not in catalog.jobs and job not in store.job):
                return {"ok": False, "error": f"unknown job {job!r}"}
            names = [job]
        else:
            names = sorted(set(catalog.jobs) | set(store.job))
        # ss11 additions (DL-46): pending timers from the oracle's own
        # liveness rules, and the ss6 log paths the wrapper appends to --
        # the TUI's jobs table and log tail read these, never a re-derivation
        pending: dict[str, list[dict[str, str]]] = {}
        for due, timer_job, kind in self.engine.oracle.pending_timers():
            pending.setdefault(timer_job, []).append({"due": due.isoformat(), "kind": kind})
        jobs: dict[str, dict[str, Any]] = {}
        for name in names:
            rt = store.job.get(name) or JobRuntime()  # never insert from a query
            log_out = log_err = None
            job_ir = catalog.jobs.get(name)
            if self.engine.run_root is not None and job_ir is not None and job_ir.job_type == "CMD":
                if rt.run_number >= 1:
                    log_out, log_err = job_log_paths(job_ir, rt.run_number, self.engine.run_root)
                elif isinstance(job_ir.exec_, ExecSpec):
                    # never ran: only explicit std files exist to tail
                    log_out, log_err = job_ir.exec_.std_out_file, job_ir.exec_.std_err_file
            jobs[name] = {
                "status": rt.status,
                "status_at": rt.status_at.isoformat() if rt.status_at else None,
                "run_number": rt.run_number,
                "exit_code": rt.exit_code,
                "on_ice": rt.on_ice,
                "on_hold": rt.on_hold,
                "on_noexec": rt.on_noexec,
                "pending_timers": pending.get(name, []),
                "log_out": log_out,
                "log_err": log_err,
            }
        return {"ok": True, "jobs": jobs}

    def _trace(self, request: dict[str, Any]) -> dict[str, Any]:
        since = request.get("since", 0)
        if not isinstance(since, int):
            return {"ok": False, "error": "since must be an integer trace seq"}
        entries = self.engine.oracle.trace()
        return {
            "ok": True,
            "last_seq": len(entries),
            "entries": [
                {
                    "seq": seq,
                    "at": entry.at.isoformat(),
                    "job": entry.job,
                    "transition": entry.transition,
                    "cause": entry.cause,
                }
                for seq, entry in enumerate(entries, start=1)
                if seq > since
            ],
        }

    def _explain(self, request: dict[str, Any]) -> dict[str, Any]:
        job = request.get("job")
        if (error := self._check_job(job)) is not None:
            return error
        assert isinstance(job, str)
        from dsl41.dsl import cond_to_source  # heavyweight surface: load on demand

        oracle = self.engine.oracle
        cond = oracle.catalog.jobs[job].sem.condition
        if cond is None:
            return {"ok": True, "job": job, "condition": None, "satisfied": True, "atoms": []}
        # oracle._cond_true is package-private on purpose: explain must use
        # the ORACLE's truth (ice bypass, lookback, instances), never a copy
        return {
            "ok": True,
            "job": job,
            "condition": cond_to_source(cond),
            "satisfied": oracle._cond_true(cond),
            "atoms": [
                {"atom": cond_to_source(atom), "true": oracle._cond_true(atom)}
                for atom in iter_atoms(cond)
            ],
        }

    def _plan(self) -> dict[str, Any]:
        sorter = graphlib.TopologicalSorter(and_success_skeleton(self.engine.oracle.catalog))
        try:
            sorter.prepare()
        except graphlib.CycleError as exc:
            return {
                "ok": False,
                "error": "plan disabled: cycle in the AND-success skeleton"
                f" ({' -> '.join(exc.args[1])})",
            }
        waves: list[list[str]] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready())
            waves.append(ready)
            sorter.done(*ready)
        return {"ok": True, "waves": waves}

    async def _subscribe(self, writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
        """Stream journal records: optional backfill from `since` (an input/
        advance seq; the cut is positional -- everything after the last
        record at or below it), then live. seq'd records are exactly-once
        across the backfill/live seam; unsequenced dispatch/drop records in
        the race window are at-least-once (module docstring)."""
        journal = self.engine.journal
        if journal is None:
            await self._send(writer, {"ok": False, "error": "this run has no journal"})
            return
        since = request.get("since")
        if since is not None and not isinstance(since, int):
            await self._send(writer, {"ok": False, "error": "since must be an integer seq"})
            return
        queue = journal.subscribe()
        try:
            # sample the seam BEFORE the ack yields: a record written during
            # the send bumps journal.seq and would be skipped as "covered"
            # despite never being backfilled (DL-45 review M4)
            max_seq = since if since is not None else journal.seq
            await self._send(writer, {"ok": True, "subscribed": True})
            if since is not None:
                records = read_journal(journal.path)
                cut = 0
                for index, record in enumerate(records):
                    seq = record.get("seq")
                    if isinstance(seq, int) and seq <= since:
                        cut = index + 1
                for record in records[cut:]:
                    seq = record.get("seq")
                    if isinstance(seq, int):
                        max_seq = max(max_seq, seq)
                    await self._send(writer, record)
            while True:
                record = await queue.get()
                seq = record.get("seq")
                if isinstance(seq, int):
                    if seq <= max_seq:
                        continue  # already delivered by the backfill
                    max_seq = seq
                await self._send(writer, record)
        finally:
            journal.unsubscribe(queue)
