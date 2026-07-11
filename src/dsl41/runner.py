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

Adapter contract (ss6): ``async run(job_ir, run_number, ctx) -> int`` (the
RAW exit code -- SEM-09/DL-33 classification stays oracle-side). Adapters
never retry (Q4 parity: a shell-side retry would fork semantics from the
simulator) and never time out (term_run_time is the oracle's timer).
Under VirtualClock an adapter may block ONLY through ctx.clock.sleep_until;
that restriction is what makes quiescence decidable (Engine._settle counts
live tasks against pending sleeps). Real adapters (11b) run under RealClock,
where the loop blocks on real IO and _settle never spins.
"""

from __future__ import annotations

import asyncio
import heapq

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import _TERMINAL, Event, Oracle


class EngineError(RuntimeError):
    """A shell-level refusal (never a semantics verdict): the engine detected
    it cannot make progress -- e.g. the zero-delay-cycle guard in
    run_until_quiescent. Loud by design (CLAUDE.md: no silent loss)."""


class Clock(Protocol):
    """ss9 time domain: the engine's only source of "now" and waiting."""

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


@dataclass
class AdapterContext:
    """What an adapter may touch (ss6). 11a: the clock only; 11b adds the
    run directory and log paths."""

    clock: Clock


class JobAdapter(Protocol):
    """ss6 adapter protocol; see the module docstring for the contract."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int: ...


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
        self, catalog: CatalogIR, *, clock: Clock, adapters: Mapping[str, JobAdapter]
    ) -> None:
        self.oracle = Oracle(catalog)
        self.clock = clock
        self.adapters = dict(adapters)  # job_type -> adapter; no BOX row
        self.drops: list[tuple[Event, str]] = []  # gate rejections; -> WAL in 11b
        #: time-ordered event queue: (at, arrival seq, event, is_completion);
        #: see the module docstring for why FIFO alone is wrong here
        self._queue: list[tuple[datetime, int, Event, bool]] = []
        self._queue_seq = 0
        self._live: dict[str, _LiveRun] = {}
        #: last run_number dispatched per job -- the ghost-run gate: an
        #: injected CHANGE_STATUS STARTING overwrite re-emits STARTING
        #: without bumping run_number, and (vendor parity) must not launch
        #: a process; only an oracle-decided start advances the counter
        self._dispatched: dict[str, int] = {}
        #: cancelled tasks awaiting collection; _settle re-raises any
        #: non-CancelledError they die with (fail loudly, never swallow)
        self._reaping: list[asyncio.Task[None]] = []

    def inject(self, ev: Event) -> None:
        """Queue an external event (test script now; sendevent in 11c).
        External events are never gated: injected STATUS keeps its
        CHANGE_STATUS parity."""
        self._enqueue(ev, is_completion=False)

    def _enqueue(self, ev: Event, *, is_completion: bool) -> None:
        self._queue_seq += 1
        heapq.heappush(self._queue, (ev.at, self._queue_seq, ev, is_completion))

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
                _, _, ev, is_completion = heapq.heappop(self._queue)
                if is_completion:
                    # gate BEFORE the clock moves: a dropped completion must
                    # be fully inert -- no time advance, no sleeper wakes
                    reason = self._stale_reason(ev)
                    if reason is not None:
                        self.drops.append((ev, reason))
                        continue
                await self.clock.wait_until(ev.at)
                out = self.oracle.feed(ev)  # journal-first slots in here (11b)
                emitted.extend(out)
                self._dispatch(out)
            elif fire_timer:
                assert eff_due is not None
                await self.clock.wait_until(eff_due)
                out = self.oracle.advance(eff_due)
                emitted.extend(out)
                self._dispatch(out)
            else:
                return emitted
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
        guess)."""
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
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_adapter(job_ir, run_number, adapter))
        self._live[job] = _LiveRun(run_number=run_number, task=task)

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
        ctx = AdapterContext(clock=self.clock)
        exit_code = await adapter.run(job_ir, run_number, ctx)
        # raw exit code only: the SEM-09/DL-33 verdict stays oracle-side;
        # (job, run_number) ride along for the ss4 stale-completion gate
        self._enqueue(
            Event(
                at=self.clock.now(),
                kind="STATUS",
                payload={
                    "job": job_ir.name,
                    "run_number": run_number,
                    "exit_code": exit_code,
                },
            ),
            is_completion=True,
        )
