"""Runner engine: the sans-IO shell over the oracle (phase 11a).

Normative spec: docs/runner-design.md (frozen 2026-07-11, DL-41/DL-41a).
The Oracle stays the single semantics authority; the engine contributes
dispatch (adapters), time (the ss9 clock domains), and -- in later phases --
durability (WAL, 11b), the calendar scheduler and control surface (11c).
Phase 11a scope (ss14): engine loop + VirtualClock + FakeAdapter, proven by
the bisimulation gate (ss13): every SEM trace test runs through both
Oracle-direct and Engine(VirtualClock, FakeAdapter) with identical traces.

DL-74 split this module: the clocks (runner_clock), the adapters and the
supervisor client (runner_adapters), the WAL (runner_journal), the calendar
scheduler with its timezone ladder (runner_scheduler), and preflight
(runner_preflight) each own their file and their paragraphs of this
docstring; DL-78 continued it with the ss10 control plane
(runner_control). What stays here is the engine loop and the run
lifecycle.

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

Phase 11b (ss6-ss7; DL-41a/DL-42 pin the lifecycle semantics):

- Kill-wins gate ordering (DL-44 amendment): before gating a
  completion, the engine fires the oracle timers due at or before the
  completion's timestamp (feed() would fire exactly these anyway), so the
  gate sees every kill decision first and drops the late natural exit --
  a kill, once decided, is never overwritten by a completion the engine
  made. Externally injected STATUS keeps CHANGE_STATUS overwrite parity.
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

Phase 11c (ss5, ss8; DL-45 pins the decisions):

- Engine loop commit discipline (DL-45): in the real domain the loop
  commits to work -- journaling an advance, popping a scheduler tick,
  feeding an event -- only once its instant is due (<= now); anything
  earlier is waited for INTERRUPTIBLY so a control injection or adapter
  completion arriving mid-wait re-plans the iteration. 11b journaled the
  advance and then slept uninterruptibly, so a completion stamped inside
  the sleep fed behind the already-advanced oracle clock and crashed the
  engine (input time went backwards); regression-pinned. Virtual-domain
  jumps never yield mid-move, so the 11a determinism pins are unchanged.

The ss10 control plane -- the socket server, its wire vocabulary, and both
clients -- moved to runner_control.py (DL-78) and is frozen in
docs/control-protocol.md. What stays relevant here: control injections
arrive through Engine.inject(source="control") and are journaled by the
take_event path like every other input, and the single-writer loop
serializes them, which is why that tier deliberately carries no controller
lease (DL-41a). Query handlers read the oracle store between feeds -- safe
because feed() never yields.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import os

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dsl41 import runner_procid as _procid
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import TERMINAL, Event, Oracle
from dsl41.runner_adapters import (
    AdapterContext,
    DetachSignal,
    Failed,
    JobAdapter,
    SupervisedCommandAdapter,
    SupervisorClient,
    SupervisorUnavailable,
    Terminated,
    _fsync_dir,
    _resolve_spool,
)
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_journal import (
    Journal,
    _last_journal_at,
    catalog_hash,
    read_journal,
    replay_inputs,
)
from dsl41.runner_scheduler import Scheduler


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
        #: time-ordered event queue: (at, arrival seq, event, is_completion);
        #: provenance rides on Event.source (DL-68); see the module docstring
        #: for why FIFO alone is wrong here
        self._queue: list[tuple[datetime, int, Event, bool]] = []
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

    def live_jobs(self) -> frozenset[str]:
        """Jobs with an in-flight adapter task. The ss10 read model needs it
        (a filewatch is ONLY an in-flight task -- no registry, no status
        field), and DL-78 made it public rather than let the control plane
        reach into `_live` from another module."""
        return frozenset(self._live)

    def inject(self, ev: Event, *, source: str | None = "control") -> None:
        """Queue an external event (test scripts; ss10 sendevent verbs).
        External events are never gated: injected STATUS keeps its
        CHANGE_STATUS parity. source=None injects unattributed (the bisim
        harness: oracle-direct scripts carry no provenance, DL-68)."""
        self._enqueue(ev, is_completion=False, source=source)

    def _enqueue(self, ev: Event, *, is_completion: bool, source: str | None = "adapter") -> None:
        ev.source = source  # DL-68: the event carries its own provenance
        self._queue_seq += 1
        heapq.heappush(self._queue, (ev.at, self._queue_seq, ev, is_completion))
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
                _, _, ev, is_completion = heapq.heappop(self._queue)
                if is_completion:
                    # kill-wins gate ordering (DL-44 amendment):
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
                    self.journal.input(ev, ev.source)  # WAL-append + fsync BEFORE feed (ss7)
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
                    # instead of abandoning them (DL-45; the
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
        if rt.status in TERMINAL:
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
            elif status in TERMINAL:
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
    # the run root holds the journal (global values, every control input),
    # job output, and data -- owner-only, loudly, not umask-hopefully
    os.chmod(run_root, 0o700)
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    journal = Journal.create(
        journal_path,
        catalog=catalog,
        clock_domain="virtual" if clock.virtual else "real",
        started_at=clock.now(),
    )
    _fsync_dir(run_root)  # the journal's directory entry is a record too
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
    same-instant siblings' appends must lose none of them silently); the
    unjournaled remainder of the window up to wall-now was missed
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
    os.chmod(run_root, 0o700)  # tighten a pre-existing looser root (same reason as create)
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
        # silently, with no drop record (DL-45). Journaled ticks
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
    boot_now = _procid.current_boot_id()
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
        if rt is None or rt.run_number != run_number or rt.status in TERMINAL:
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
                raise EngineError(  # refuse loudly: never leave it hanging
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
