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
legally overwrite terminal statuses; oracle module docstring). Which
completions those are is read off `Event.source` (DL-68) rather than
carried beside it: a record in the log has its provenance and nothing else,
so replay must be able to reach the same verdict from the same field.

Stage S2 (docs/concurrency-model.md ss4, DL-89) put ONE admission order in
front of the loop, and every input in this module now goes through it --
operator commands, scheduler ticks, adapter completions, reconciliation
injections and standalone time observations alike. `_admit_and_apply` is
that order and its docstring maps it to the frozen steps. Two consequences
are visible from here: the gate above now runs AFTER the input is durably
admitted, so a rejection is a recorded decision rather than an absence
(replay can honour it instead of guessing); and the batch's time
observation applies even when the attempt is rejected, which is the
property DL-44's advance record was added for. The engine holds the log's
position in `frontiers` and the decisions it has already made in
`decisions`, both restored from the log on resume.

Stage S3 (concurrency-model ss0/ss6, DL-90) made preconditions MANDATORY
for externally requested mutations, and split the engine's two input doors
apart to say so. `submit` is the external door: it takes an ss6 envelope
carrying the revision the caller read, and hands back the DECISION -- the
loop resolves its future at step 7, because a precondition whose outcome
the caller cannot see is not a precondition. `inject` is the engine's own
door and stays as it was: the scheduler, the adapters, reconciliation and
every test script are inside the trust boundary, and ss0's rule is about
what crosses it. A refusal (steps 1-2) and a rejection (step 6) are
different facts here too -- the first never entered the log and is recorded
only on `refusals`, the second is a decision with an index.

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
  incomplete FW runs are re-dispatched instead. A start with no spool trace
  on a host that routes nothing is a second exception (DL-94): there was no
  crash to lose it to, only a drain doing its job, and the drain is durable
  -- so it stays HELD rather than being failed, or the routing state would
  survive a failover while the work it protected did not. Reconciliation completions
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

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dsl41 import runner_procid as _procid
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, TERMINAL
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
    load_json,
    outcome_from_status,
    _resolve_spool,
)
from dsl41.runner_admission import (
    INERT_EPOCH,
    AdmissionRefused,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    Frontiers,
    RequestCollision,
    Applied,
    apply_attempt,
    fingerprint,
)
from dsl41.runner_clock import Clock, EngineError
from dsl41.runner_effects import (
    Effect,
    EffectOutcome,
    Outbox,
    plan_effects,
    superseded_reason,
)
from dsl41.runner_hosts import (
    LOCAL_EXECUTOR_ID,
    HostCommand,
    routes_new_effects,
    seed_local_executor,
)
from dsl41.runner_journal import (
    Journal,
    _last_journal_at,
    baseline_id,
    catalog_hash,
    read_journal,
    replay_inputs,
)
from dsl41.runner_ledger import LeaderLock, check_leader_eligibility, next_epoch
from dsl41.runner_scheduler import Scheduler


@dataclass
class _LiveRun:
    run_number: int
    task: asyncio.Task[None]


@dataclass
class _Pending:
    """One input awaiting admission. A client's `request_id` rides from the
    moment the input is raised, because concurrency-model ss4 deduplicates
    BEFORE it stamps or indexes anything -- an id minted at admission could
    never answer a retry.

    `envelope` present marks an EXTERNALLY REQUESTED input -- one that
    crossed the ss10 socket and therefore had to name the revision it was
    composed against (concurrency-model ss0). Absent marks the engine's own:
    a timer, a scheduler tick, an adapter completion, a reconciliation.
    `future` is how the requester learns its decision; ss4 emits two signals
    for one input and this transport answers with the second, because a
    precondition whose outcome the caller cannot see is not a precondition.

    None is an input the engine raised itself: a timer firing, a scheduler
    tick, an adapter completion. It still takes an identity in the log,
    because the decision index indexes by one, but that identity comes from
    the admission index and is never looked up. Nobody outside this process
    can retry it, and a per-incarnation counter would collide with the ids
    the previous incarnation left in the log the moment a resume replayed
    them.

    `ev` absent is an attempt with no oracle verb: either a routing-table
    command (`host`) or, with neither, a standalone time observation. ss4
    admits all three by the same rule.

    `source` is read only when `ev` is absent -- an event carries its own
    (DL-68), and one field holding what another already holds is one field
    too many."""

    at: datetime
    request_id: str | None
    ev: Event | None = None
    host: HostCommand | None = None
    source: str | None = None
    envelope: Envelope | None = None
    future: asyncio.Future[ApplyResult] | None = None


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
        executor_id: str = LOCAL_EXECUTOR_ID,
        deadman_s: float | None = None,
        epoch: int = INERT_EPOCH,
    ) -> None:
        self.oracle = Oracle(catalog)
        #: concurrency-model ss2/ss8: the execution host this engine dispatches
        #: to. One engine per run root owns one local executor; machine names
        #: resolve to a relay through the routing table (ss5) and there is no
        #: relay until S5d, so every job routes here. Seeded into the table at
        #: genesis rather than admitted, so no log index is spent recording a
        #: fact about how this process was launched (see seed_local_executor).
        #: `deadman_s` is what the LOCAL SUPERVISOR reports it runs, read back
        #: rather than declared, so the ss8 eviction bound describes the host
        #: and not this engine's launch options (S5b).
        self.executor_id = executor_id
        seed_local_executor(self.oracle.store, executor_id, at=clock.now(), deadman_s=deadman_s)
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
        #: concurrency-model ss2: the log's position. `frontiers` is where
        #: admission and application have reached; `decisions` answers a
        #: retry from what it already decided (ss4 step 2). Both are restored
        #: from the log on resume -- an engine that forgot them would re-admit
        #: an input the log had already decided.
        self.baseline_id = journal.baseline_id if journal is not None else ""
        #: concurrency-model ss2: the leader's fencing token, allocated by
        #: the run root's ledger when this incarnation took the term (S6a,
        #: runner_ledger). INERT_EPOCH means no election was held here at
        #: all -- an Engine with no run root and no log, which is what the
        #: bisimulation harness runs -- rather than "not implemented yet".
        #: The CHECK sits in its ss4 place (after dedup), so an exact
        #: old-epoch retry still recovers its original result.
        self.epoch = epoch
        self.frontiers = Frontiers()
        self.decisions = DecisionIndex()
        #: retries answered from the index rather than re-applied (CM-05)
        self.deduped: list[tuple[str, ApplyResult]] = []
        #: refused at ss4 steps 1-2 -- never admitted, so unlike `drops` these
        #: leave no trace in the log and this list is the only record of them
        self.refusals: list[tuple[str, str]] = []
        #: time-ordered input queue: (at, arrival seq, pending); provenance
        #: rides on Event.source (DL-68); see the module docstring for why
        #: FIFO alone is wrong here
        self._queue: list[tuple[datetime, int, _Pending]] = []
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
        #: concurrency-model ss5: what this engine intends to do to its
        #: execution hosts, and what came of it. Restored from the log on
        #: resume -- an engine that forgot a kill it had decided would leave a
        #: detached run orphaned for the rest of its life.
        self.outbox = Outbox()

    def note_executor_contact(self) -> None:
        """Stamp positive contact with this engine's own execution host
        (concurrency-model ss8). Wired to the supervisor client's lease
        exchanges: a confirmed ACQUIRE or RENEW is the leader hearing back
        from the host, which is exactly what the eviction bound counts from.

        Costs nothing per beat -- `last_contact` is outside the ss3 semantic
        projection, so this moves no revision and writes no log record. The
        quarantine it may clear is a different matter and IS an input, which
        is why that is a separate call and not folded in here."""
        self.oracle.store.touch_host(self.executor_id, self.clock.now())
        self.note_executor_reachable()

    def held_jobs(self) -> frozenset[str]:
        """Jobs with a start this shell intended and has not dispatched,
        because their executor routes no new effects (concurrency-model ss8).

        The outbox IS the held set (S5c): a pending SPAWN is exactly an
        intent recorded and not yet applied. DL-94 derived this from the
        oracle's status because intent had nowhere durable to live; now it
        does, and deriving it a second way would be the parallel model DL-91
        exists to catch. It also survives a restart, which the derivation
        could not -- a held job used to need a special case in reconciliation
        to avoid being failed as never-spawned."""
        return frozenset(e.job for e in self.outbox.pending() if e.kind == "SPAWN")

    def live_jobs(self) -> frozenset[str]:
        """Jobs with an in-flight adapter task. The ss10 read model needs it
        (a filewatch is ONLY an in-flight task -- no registry, no status
        field), and DL-78 made it public rather than let the control plane
        reach into `_live` from another module."""
        return frozenset(self._live)

    def inject(
        self, ev: Event, *, source: str | None = "control", request_id: str | None = None
    ) -> None:
        """Queue an external event (test scripts; ss10 sendevent verbs).
        External events are never gated: injected STATUS keeps its
        CHANGE_STATUS parity. source=None injects unattributed (the bisim
        harness: oracle-direct scripts carry no provenance, DL-68).

        `request_id` makes the injection retryable: an exact retry under the
        same id is answered from its first decision and applies nothing
        (concurrency-model ss4 step 2). Omitted, the engine names the input
        itself -- an input nobody can retry still needs an identity, because
        the log indexes decisions by one."""
        self._enqueue(ev, source=source, request_id=request_id)

    def submit(
        self, ev: Event, envelope: Envelope, *, source: str = "control"
    ) -> Awaitable[ApplyResult]:
        """Queue one EXTERNALLY REQUESTED mutation and hand back its
        decision (concurrency-model ss0/ss4).

        The envelope carries the precondition, so this is the only entry
        point that satisfies ss0's mandate -- `inject` is the engine's own
        trust domain and stays ungated. The awaitable resolves when step 7
        records the result, or raises `AdmissionRefused` if steps 1-2 refuse
        it. It never resolves before the decision exists: ss4 emits
        `command_committed` too, but "your envelope is durable" is not an
        answer to "did my kill land"."""
        future: asyncio.Future[ApplyResult] = asyncio.get_running_loop().create_future()
        self._enqueue(
            ev,
            source=source,
            request_id=envelope.request_id,
            envelope=envelope,
            future=future,
        )
        return future

    def submit_host(
        self, cmd: HostCommand, envelope: Envelope, *, source: str = "control"
    ) -> Awaitable[ApplyResult]:
        """Queue one routing-table change and hand back its decision
        (concurrency-model ss8).

        `submit`'s sibling, not a second path: the same envelope, the same
        admission order, the same four outcomes. What differs is only that
        the attempt carries no oracle event, so nothing is fed -- the ss3
        owner is written directly inside the batch (DL-93)."""
        future: asyncio.Future[ApplyResult] = asyncio.get_running_loop().create_future()
        self._push(
            _Pending(
                at=self.clock.now(),
                request_id=envelope.request_id,
                host=cmd,
                source=source,
                envelope=envelope,
                future=future,
            )
        )
        return future

    def inject_host(self, cmd: HostCommand) -> None:
        """The LEADER's own routing observation (concurrency-model ss8).

        `submit_host`'s sibling on the other side of ss0's boundary. An
        operator asserts intent about routing and must name the revision they
        read; the leader reports what it can and cannot reach, which is not
        an externally requested mutation of state the caller was looking at
        -- it is the same trust domain the scheduler and the adapters inject
        from. So: no envelope, no `expect`, and no future to resolve. It is
        still an admitted input, journaled and replayed like any other,
        because a quarantine that did not survive a restart would let the
        next engine route work at a host that is not answering."""
        self._push(_Pending(at=self.clock.now(), request_id=None, host=cmd, source="reconcile"))

    def note_executor_unreachable(self) -> None:
        """The leader has lost contact with its own execution host (ss8).

        Wired to the point where the supervisor client gives up rather than
        to any single failure: one refused connection is a blip, and a
        quarantine per blip would hold work for no reason. What it buys
        locally is worth having on its own -- new work is HELD until the host
        answers again, instead of every spawn failing against a supervisor
        that is not there and every job being marked FAILURE for it."""
        self.inject_host(HostCommand(verb="quarantine", host_id=self.executor_id))

    def note_executor_reachable(self) -> None:
        """Contact restored: the leader clears what it set, putting back the
        state it interrupted (a drained host stays drained)."""
        row = self.oracle.store.host(self.executor_id)
        if row is not None and row.state == "quarantined":
            self.inject_host(HostCommand(verb="reinstate", host_id=self.executor_id))

    def _enqueue(
        self,
        ev: Event,
        *,
        source: str | None = "adapter",
        request_id: str | None = None,
        envelope: Envelope | None = None,
        future: asyncio.Future[ApplyResult] | None = None,
    ) -> None:
        ev.source = source  # DL-68: the event carries its own provenance
        self._push(
            _Pending(at=ev.at, request_id=request_id, ev=ev, envelope=envelope, future=future)
        )

    def _push(self, pending: _Pending) -> None:
        """Put one input on the time-ordered queue and wake the loop. The
        arrival counter breaks ties, so two inputs stamped alike keep the
        order they were raised in."""
        self._queue_seq += 1
        heapq.heappush(self._queue, (pending.at, self._queue_seq, pending))
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
                _, _, pending = heapq.heappop(self._queue)
                out = await self._admit_and_apply(pending)
                emitted.extend(out)
                self._dispatch()
            elif take_sched:
                # the calendar tick is next: enqueue its STARTJOB(s), stamped
                # at the tick, and let the next iteration take them like any
                # external input (journal-first at feed; feed() fires timers
                # due <= tick first, identical to oracle-direct scripts)
                assert sched_due is not None and self.scheduler is not None
                await self.clock.wait_until(sched_due)
                for tick_ev in self.scheduler.pop_due(sched_due):
                    self._enqueue(tick_ev, source="scheduler")
            elif fire_timer:
                assert eff_due is not None
                # a time observation is an input (DL-44 amendment): the timer
                # firings it causes must survive a crash, or resume replay
                # would resurrect a job the oracle already killed. It is
                # admitted exactly like an operator command -- an attempt with
                # no verb (concurrency-model ss4)
                out = await self._admit_and_apply(_Pending(at=eff_due, request_id=None))
                emitted.extend(out)
                self._dispatch()
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

    async def _admit_and_apply(self, pending: _Pending) -> list[Event]:
        """The frozen admission order for ONE input (concurrency-model ss4),
        which every input in this engine goes through: operator commands,
        scheduler ticks, adapter completions, reconciliation injections and
        standalone time observations alike.

        Reading it against ss4's numbered steps: framing and `baseline_id`
        are settled by construction here (1); the index answers an exact
        retry before anything is stamped or appended, so a retry costs no
        index and moves no clock (2); the frontier hands out the next index
        at a non-decreasing stamp (3); the attempt is one line, so its time
        observation cannot be torn from its verb (4); the batch applies the
        time half -- firing due timers, which is what puts a term_run_time
        kill AHEAD of the gate that reads the status it kills (5); the gate
        decides (6); the result is recorded with the revisions the input
        moved (7). Steps 5-7 do not yield: `wait_until` is done with, and
        nothing else in this process writes the oracle."""
        ev = pending.ev
        envelope = pending.envelope
        fp = fingerprint(
            baseline_id=self.baseline_id,
            kind=ev.kind if ev is not None else None,
            payload=dict(ev.payload) if ev is not None else {},
            source=ev.source if ev is not None else pending.source,
            epoch=envelope.epoch if envelope is not None else self.epoch,
            expect=envelope.expect if envelope is not None else None,
            claimed_actor=envelope.claimed_actor if envelope is not None else None,
            host=pending.host,
        )
        if pending.request_id is not None:
            try:
                prior = self.decisions.lookup(pending.request_id, fp)
            except RequestCollision as exc:
                # a client error, not an engine one: refuse the request and
                # keep serving. Raising here would let one confused caller
                # take the estate down.
                self._refuse(pending, exc)
                return []
            if prior is not None:
                self.deduped.append((pending.request_id, prior))
                self._answer(pending, prior)
                return []  # a retry advances no logical time (CM-05)
        if envelope is not None and envelope.epoch != self.epoch:
            # ss4 step 2, and it is AFTER the dedup above on purpose: an exact
            # old-epoch retry recovers its original result (it was decided by
            # the leader that held that epoch), while an UNSEEN old-epoch
            # request is refused -- it was composed by a client still talking
            # to a leader that has been superseded.
            self._refuse(
                pending,
                AdmissionRefused(
                    f"epoch {envelope.epoch} is not this leader's {self.epoch}:"
                    " re-read and re-compose against the current leader"
                ),
            )
            return []
        self.frontiers = self.frontiers.admit(pending.at)
        index = self.frontiers.committed_index
        attempt = self._attempt(pending, index, fp)
        if self.journal is not None:
            self.journal.admit(attempt)  # WAL-append + fsync BEFORE apply (ss7)
        self.decisions.note(attempt)
        await self.clock.wait_until(pending.at)
        applied = apply_attempt(self.oracle, attempt)
        self.decisions.record(applied.result)
        self.frontiers = self.frontiers.record(attempt.index)
        # step 7 commits the decision and the outbox entries it implies as ONE
        # batch (concurrency-model ss1): an engine that dies between deciding
        # and acting must leave behind the record that it MEANT to act, or a
        # kill it decided vanishes with the task that would have delivered it
        effects = self._plan_effects(applied, attempt.index)
        if self.journal is not None:
            self.journal.result(applied.result)
            for effect in effects:
                self.journal.effect(effect)
        for effect in effects:
            self.outbox.record(effect)
        if applied.result.decision == "rejected" and ev is not None:
            assert applied.result.reason is not None
            self.drops.append((ev, applied.result.reason))
        self._answer(pending, applied.result)
        return applied.emitted

    def _plan_effects(self, applied: Applied, index: int) -> list[Effect]:
        """ss4 step 7's other half: what the shell now intends to do about
        what the oracle just decided."""
        return plan_effects(
            applied.emitted,
            index=index,
            executor_id=self.executor_id,
            runs={job: rt.run_number for job, rt in self.oracle.store.job.items()},
            dispatched=self._dispatched,
            live={job: run.run_number for job, run in self._live.items()},
            dispatchable=self._dispatchable(),
        )

    def _dispatchable(self) -> frozenset[str]:
        """Jobs this engine has a dispatch row for: a catalog entry whose
        job_type has a registered adapter. Boxes fold from members and
        pseudo-entries have no definition, so neither is ever an effect."""
        return frozenset(
            name
            for name, job_ir in self.oracle.catalog.jobs.items()
            if job_ir.job_type in self.adapters
        )

    @staticmethod
    def _answer(pending: _Pending, result: ApplyResult) -> None:
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(result)

    def _refuse(self, pending: _Pending, exc: AdmissionRefused) -> None:
        """Steps 1-2's other outcome. Recorded on `refusals` as well as
        answered, because a refusal leaves NOTHING in the log -- no index, no
        record -- so without this list an in-process caller that asked for no
        decision would see a command vanish silently."""
        self.refusals.append((pending.request_id or f"engine:{pending.at.isoformat()}", str(exc)))
        if pending.future is not None and not pending.future.done():
            pending.future.set_exception(exc)

    def _attempt(self, pending: _Pending, index: int, fp: str) -> Attempt:
        ev = pending.ev
        envelope = pending.envelope
        return Attempt(
            index=index,
            at=pending.at,
            request_id=pending.request_id or f"engine:{index}",
            fingerprint=fp,
            kind=ev.kind if ev is not None else None,
            payload=dict(ev.payload) if ev is not None else {},
            host=pending.host,
            source=ev.source if ev is not None else pending.source,
            expect=dict(envelope.expect) if envelope is not None else None,
            epoch=envelope.epoch if envelope is not None else self.epoch,
            claimed_actor=envelope.claimed_actor if envelope is not None else None,
        )

    def _dispatch(self) -> None:
        """Drain the outbox: apply every pending effect, in admission order
        (concurrency-model ss5).

        The shell's whole dispatch surface, and the only place an effect is
        applied. Ordering is not decoration -- ss5 makes per-run effect
        ordering mandatory, because a KILL decided after a SPAWN that
        overtook it would stop a run that had not started."""
        for effect in self.outbox.pending():
            self._apply_effect(effect)

    def _apply_effect(self, effect: Effect) -> None:
        """One effect, at-most-once (concurrency-model ss5, CM-09).

        Three gates before anything happens, and each keeps out a different
        wrong act: a host that routes nothing leaves the effect PENDING (ss8
        -- held, not failed and not rerouted); an effect the world has moved
        past is RETIRED rather than applied; and only then is it attempted.
        The outcome is recorded either way, because "attempted and we cannot
        say" is a fact that has to survive a crash (ss5's third state)."""
        if effect.kind == "SPAWN" and not routes_new_effects(
            self.oracle.store.host(effect.executor_id)
        ):
            # ss8: a drained or quarantined host routes no NEW effect. Left
            # pending, which IS the held set -- rerouting without proof the old
            # executor is dead is the double run this model exists to prevent
            # (ss7), and failing it would turn a maintenance window into an
            # estate-wide incident.
            #
            # SPAWN only. ss8's column is about NEW work: `passive` says
            # running work continues to completion, and a kill is how running
            # work ends. Holding kills during a drain would make KILLJOB stop
            # working exactly while an operator is most likely to reach for it.
            return
        reason = superseded_reason(
            effect,
            self.oracle.store.job.get(effect.job),
            self._live[effect.job].run_number if effect.job in self._live else None,
        )
        if reason is not None:
            self._resolve_effect(
                EffectOutcome(effect_id=effect.effect_id, state="retired", detail=reason)
            )
            return
        if effect.kind == "SPAWN":
            self._apply_spawn(effect)
        else:
            self._apply_kill(effect)

    def _resolve_effect(self, outcome: EffectOutcome) -> None:
        self.outbox.resolve(outcome)
        if self.journal is not None:
            self.journal.effect_result(outcome)

    def _apply_spawn(self, effect: Effect) -> None:
        job_ir = self.oracle.catalog.jobs.get(effect.job)
        adapter = self.adapters.get(job_ir.job_type) if job_ir is not None else None
        if job_ir is None or adapter is None:
            # unreachable through planning (`_dispatchable` filters both), so
            # meeting it means the catalog and the log disagree
            self._resolve_effect(
                EffectOutcome(
                    effect_id=effect.effect_id,
                    state="retired",
                    detail=f"{effect.job} has no dispatch row in this catalog",
                )
            )
            return
        self._dispatched[effect.job] = effect.run_number
        stale = self._live.pop(effect.job, None)
        if stale is not None:
            # one live attempt per job; a report from the old task would be
            # gate-dropped anyway (run_number mismatch) -- cancel is tidier
            stale.task.cancel()
            self._reaping.append(stale.task)
        self._launch(job_ir, effect.run_number, adapter)
        self._resolve_effect(EffectOutcome(effect_id=effect.effect_id, state="applied"))

    def _apply_kill(self, effect: Effect) -> None:
        """The oracle decided terminal; the shell stops the run.

        Cancelling the adapter task IS the effect at this tier -- the
        adapter's TERM/grace/KILL ladder is the lifecycle tier's business and
        runs on the way out. `applied` therefore means the cancellation was
        delivered to a live run, which is the whole of what this tier can
        promise; the wrapper records what became of the process."""
        live = self._live.pop(effect.job, None)
        if live is None:
            self._resolve_effect(
                EffectOutcome(
                    effect_id=effect.effect_id,
                    state="retired",
                    detail=f"{effect.job} has no live run: nothing to cancel",
                )
            )
            return
        live.task.cancel()
        self._reaping.append(live.task)
        self._resolve_effect(EffectOutcome(effect_id=effect.effect_id, state="applied"))

    def _launch(self, job_ir: JobIR, run_number: int, adapter: JobAdapter) -> None:
        """Create the adapter task. Reached from `_apply_spawn` (an outbox
        effect) and from resume's FW re-dispatch and detached reattach
        (module docstring), neither of which goes through an effect: one is
        an idempotent re-read and the other is a run that never stopped."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_adapter(job_ir, run_number, adapter))
        task.add_done_callback(lambda _t: self._activity.set())
        self._live[job_ir.name] = _LiveRun(run_number=run_number, task=task)

    async def shutdown(self) -> None:
        """Cancel every live adapter task and collect the cancellations,
        re-raising anything a task died with OTHER than the cancellation
        itself (fail loudly -- a teardown bug must not vanish). 11a: orderly
        harness/rehearse teardown; the tethered-kill semantics (wrapper
        records the outcome, ss6a) arrive with real adapters in 11b.

        Anyone parked on a decision is told first (S3), and told the truth:
        their input never reached admission, so nothing in the log refers to
        it. Left alone they would wait out their own transport timeout and
        learn only that something did not answer."""
        for _, _, pending in self._queue:
            if pending.future is not None and not pending.future.done():
                pending.future.set_exception(
                    AdmissionRefused("engine shut down before this input was admitted")
                )
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
        # source="adapter" is what makes this a COMPLETION, and therefore
        # what subjects it to the ss4 stale gate (runner_admission)
        self._enqueue(Event(at=self.clock.now(), kind="STATUS", payload=payload))


# ------------------------------------------------------------ run lifecycle (ss7)


def start_run(
    catalog: CatalogIR,
    run_root: Path,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None = None,
    hold_open: bool = False,
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
) -> Engine:
    """Create the run-root layout (journal.jsonl, runs/, logs/) and an
    Engine wired to it. Refuses a run_root that already holds a journal --
    that is what --resume is for (no silent re-baselining).

    Leadership (S6a) is acquired BEFORE that refusal, not after: the
    refusal reads the estate's state, and one leader per run root is the
    rule under which any such read is meaningful. Genesis is epoch 1. A
    `lock` already held by the caller is used as-is and stays theirs to
    release -- `dsl41 run` takes it earlier still, before it starts a
    supervisor."""
    run_root.mkdir(parents=True, exist_ok=True)
    # the run root holds the journal (global values, every control input),
    # job output, and data -- owner-only, loudly, not umask-hopefully
    os.chmod(run_root, 0o700)
    owned = lock is None
    if lock is None:
        lock = LeaderLock(run_root)
        lock.acquire()
    journal_path = run_root / "journal.jsonl"
    if journal_path.exists():
        if owned:
            lock.release()
        raise EngineError(
            f"{journal_path} already exists: resume it (resume_run) or pick a fresh run root"
        )
    (run_root / "runs").mkdir(exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    at = clock.now()
    journal = Journal.create(
        journal_path,
        catalog=catalog,
        clock_domain="virtual" if clock.virtual else "real",
        started_at=at,
        lock=lock,
    )
    epoch = next_epoch([])  # the first term over a log that has none
    journal.leader(epoch=epoch, at=at)
    lock.note(epoch=epoch, at=at)
    _fsync_dir(run_root)  # the journal's directory entry is a record too
    return Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        deadman_s=deadman_s,
        epoch=epoch,
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
    deadman_s: float | None = None,
    lock: LeaderLock | None = None,
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
    os.chmod(run_root, 0o700)  # tighten a pre-existing looser root (same reason as create)
    # ACQUIRE first (S6a, concurrency-model ss7): everything below this line
    # reads or acts -- the log is replayed, the estate is reconciled,
    # recorded kills are re-driven -- and a mutex taken after the first side
    # effect is not a mutex. It also has to precede the READ, or another
    # engine could append between the read and the acquire and this one would
    # allocate an epoch the log already used.
    owned = lock is None
    if lock is None:
        lock = LeaderLock(run_root)
        lock.acquire()
    try:
        engine = await _resume_under_lock(
            catalog,
            run_root,
            lock,
            clock=clock,
            adapters=adapters,
            scheduler=scheduler,
            hold_open=hold_open,
            settle_seconds=settle_seconds,
            grace_seconds=grace_seconds,
            supervisor=supervisor,
            deadman_s=deadman_s,
        )
    except BaseException:
        if owned:
            lock.release()  # a refused resume holds nothing: the next engine may lead
        raise
    return engine


async def _resume_under_lock(
    catalog: CatalogIR,
    run_root: Path,
    lock: LeaderLock,
    *,
    clock: Clock,
    adapters: Mapping[str, JobAdapter],
    scheduler: Scheduler | None,
    hold_open: bool,
    settle_seconds: float,
    grace_seconds: float,
    supervisor: SupervisorClient | None,
    deadman_s: float | None,
) -> Engine:
    """The ss7 resume ladder proper, with leadership already held (S6a).
    Split from `resume_run` so the acquire/release pairing is one readable
    block rather than a `finally` wrapped around a hundred lines."""
    records = read_journal(run_root / "journal.jsonl")
    header = records[0]
    check_leader_eligibility(header, expected_catalog_hash=catalog_hash(catalog))
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
        baseline_id=baseline_id(records),
        lock=lock,
    )
    # the term is allocated by being appended (ss1), before the first input
    # this incarnation admits, so every record after it names its author
    epoch = next_epoch(records)
    journal.leader(epoch=epoch, at=clock.now())
    lock.note(epoch=epoch, at=clock.now())
    engine = Engine(
        catalog,
        clock=clock,
        adapters=adapters,
        journal=journal,
        run_root=run_root,
        scheduler=scheduler,
        hold_open=hold_open,
        deadman_s=deadman_s,
        epoch=epoch,
    )
    replay = replay_inputs(engine.oracle, records)
    # the log's position comes back with its contents (concurrency-model
    # ss2): the next admission continues the index, and a retry of anything
    # this log already decided is still answered from that decision rather
    # than applied a second time
    engine.frontiers = replay.frontiers
    engine.decisions = replay.decisions
    # ss5: the effects the previous engine intended, and what became of them.
    # An engine that forgot a kill it had decided would leave a detached run
    # orphaned for the rest of its life -- its job is already TERMINAL, so
    # reconciliation skips it, and nothing else would ever look again.
    engine.outbox = replay.outbox
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
    # ...and what the HOST says it is running (S6c). ss7 reconciles every
    # execution host, not every local directory: the sweep below concludes
    # "never spawned" from absence here, and absence that only means "the
    # run directory is gone" would let it re-drive a start the supervisor is
    # still running -- the double run the whole model exists to prevent.
    for key in supervised_live:
        candidates.setdefault(key, None)

    def _inject(job: str, run_number: int, extras: dict[str, object], at: datetime) -> None:
        engine._enqueue(
            Event(
                at=max(at, last_at),  # feed times are non-decreasing (ss7)
                kind="STATUS",
                payload={"job": job, "run_number": run_number, **extras},
            ),
            source="reconcile",  # a COMPLETION: the ss4 gate applies, replay may know better
        )

    _reconcile_applied_spawns(engine, candidates)

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

    # Starts with no trace anywhere -- no run directory, no dispatch record,
    # and nothing the host admits to running. ss7's barrier says "retire
    # superseded, re-drive pending", and S6c is where that stops being two
    # words and becomes the two cases below (DL-96 deferred it here).
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
        if engine.outbox.pending_for(job, "SPAWN"):
            # RE-DRIVEN. The log holds an intent to spawn that was never
            # resolved, and nothing anywhere ran: the previous leader died in
            # the window between recording what it meant to do and doing it.
            # Left pending, which is all re-driving takes -- `_dispatch`
            # drains the outbox the moment the loop runs, through the same
            # gates a fresh effect passes, so a drained or quarantined host
            # still HOLDS it (ss8) and this sweep does not need to know that.
            continue
        # FAILED. No pending intent, so the log never said a spawn was meant
        # to happen -- a journal written before the outbox existed (S5c), or
        # an effect already resolved whose spool has since gone. That is the
        # case runner-design ss7 was reasoning about when it chose to fail a
        # start rather than silently re-run it, and it still does.
        _inject(
            job,
            rt.run_number,
            {"status": "FAILURE", "cause": "dispatch lost to engine crash (never spawned)"},
            last_at,
        )
    await _redrive_recorded_kills(engine, supervised_live)
    # ss7's barrier ends where it says it does: ACQUIRE -> reconcile -> retire
    # superseded, re-drive pending -> DISPATCH. Without this the outbox is
    # drained only after the next admitted input (the loop dispatches on the
    # way out of `_admit_and_apply`), so a re-driven start would wait on
    # unrelated traffic to arrive -- hours, on a quiet estate, and never on
    # one whose only remaining work is the run that was lost. Everything
    # still pending here is a SPAWN nothing applied: the kills above are
    # resolved, and a SPAWN whose run reached the host was reconciled from
    # its trace. Superseded ones retire on the way through, and a drained or
    # quarantined host holds its own, in the one gate that owns that call.
    engine._dispatch()


def _reconcile_applied_spawns(
    engine: Engine, candidates: dict[tuple[str, int], Path | None]
) -> None:
    """Resolve the pending SPAWNs whose runs DID reach the host (ss5).

    The classic outbox window: `_apply_spawn` launches and THEN records the
    outcome, so an engine that died between the two left a pending effect
    for a run that may well have started. The spool is the record (DL-93) --
    a run directory means it reached the host -- so the effect is reconciled
    from it rather than re-driven. Without this the next dispatch would drain
    a pending SPAWN into a second `mkdir()` of a directory that exists."""
    for effect in [e for e in engine.outbox.pending() if e.kind == "SPAWN"]:
        spool = candidates.get((effect.job, effect.run_number))
        if (effect.job, effect.run_number) not in candidates:
            continue
        spawned = load_json(spool / "spawn.json") if spool is not None else None
        engine._resolve_effect(
            EffectOutcome(
                effect_id=effect.effect_id,
                state="applied",
                run_id=(spawned or {}).get("run_id"),
                detail="reconciled from the spool: the run reached the host",
            )
        )


async def _redrive_recorded_kills(
    engine: Engine, supervised_live: dict[tuple[str, int], dict[str, Any]]
) -> None:
    """Deliver the kills the previous engine decided and did not get to
    (concurrency-model ss5; S5c).

    This closes a real leak. A kill used to be a `task.cancel()` with no id
    and no record: an engine that decided TERMINATED and died before
    cancelling left a DETACHED run whose parent is the supervisor, and
    reconciliation skipped it on the way past -- its job is already TERMINAL,
    which reads as "its completion was already replayed". Nothing looked
    again, and the process ran on orphaned.

    Re-driving is not a new licence: runner-design ss7 already permits
    exactly this side effect at resume, and only this one ("no side effects
    on resume beyond recorded kills").

    A kill whose run is NOT alive is resolved from the spool, three ways --
    which is where ss5's third state earns its keep. `status.json` saying the
    command was signalled means the kill landed; saying it exited means it
    finished first and the kill is retired, superseded by the truth. No
    status record at all and no live wrapper means nobody can say whether the
    signal landed, and `indeterminate` is the only honest answer: reporting
    it either way would invent a fact about a process nothing observed
    (E7)."""
    assert engine.run_root is not None
    for effect in [e for e in engine.outbox.pending() if e.kind == "KILL"]:
        listing = supervised_live.get((effect.job, effect.run_number))
        job_ir = engine.oracle.catalog.jobs.get(effect.job)
        adapter = engine.adapters.get(job_ir.job_type) if job_ir is not None else None
        if (
            job_ir is not None
            and listing is not None
            and listing.get("wrapper_alive")
            and isinstance(adapter, SupervisedCommandAdapter)
        ):
            # the adapter's own TERM/grace/KILL ladder, driven directly. Not
            # through a reattached task and a cancel: a task cancelled before
            # its first step never enters the handler that runs the ladder, so
            # that route would resolve the effect while stopping nothing.
            # `_live` is empty here anyway -- the supervisor's LIST is what
            # says this run is alive, which is why `_apply_effect`'s
            # supersession check (which reads `_live`) is not the right gate
            # at resume.
            run_id = str(listing["run_id"])
            await adapter.kill(run_id)
            engine._resolve_effect(
                EffectOutcome(
                    effect_id=effect.effect_id,
                    state="applied",
                    run_id=run_id,
                    detail="re-driven at resume: the wrapper was still alive",
                )
            )
            continue
        engine._resolve_effect(_kill_outcome_from_spool(engine.run_root, effect))


def _kill_outcome_from_spool(run_root: Path, effect: Effect) -> EffectOutcome:
    """What the spool can say about an undelivered kill (ss5's three states).

    The record is read through `outcome_from_status`, the one mapping the
    live adapter path and reconciliation already share, rather than by
    reaching into the record here. Its `Terminated` IS "this run ended by a
    kill", which is the question -- and the first draft of this function
    asked a different one, reading `observed`, which the wrapper writes as
    FORENSICS about how the group died rather than as its verdict."""
    run_dir = run_root / "runs" / f"{effect.job}.{effect.run_number}"
    status = load_json(run_dir / "status.json")
    if status is None:
        return EffectOutcome(
            effect_id=effect.effect_id,
            state="indeterminate",
            detail="no status record and no live wrapper: nothing can say whether it landed",
        )
    killed = isinstance(outcome_from_status(status), Terminated)
    return EffectOutcome(
        effect_id=effect.effect_id,
        state="applied" if killed else "retired",
        run_id=status.get("run_id"),
        detail=(
            "the spool records the run as killed"
            if killed
            else "the run ended on its own before the kill was delivered"
        ),
    )
