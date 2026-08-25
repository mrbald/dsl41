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
scheduler (runner_scheduler; its timezone ladder left for the phase-free
`timezones` module with DL-163), and preflight
(runner_preflight) each own their file and their paragraphs of this
docstring; DL-78 continued it with the ss10 control plane
(runner_control), and DL-106 with taking possession of a run root --
genesis, resume, and ss7's takeover barrier (runner_startup). What stays
here is the engine loop: the half that runs while an estate is live, over
an Engine that runner_startup constructs and hands over.

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
- Resume (ss7) moved to runner_startup with DL-106, paragraph and all. What
  the loop still owes it: reconciliation completions arrive through
  `inject(source="reconcile")` and go through the ss4 stale gate like any
  adapter completion, so a late real record cannot silently overwrite a
  terminal state replay already reached.

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
queued-input path (`_Do.EVENT`) like every other input, and the
single-writer loop serializes them, which is why that tier deliberately
carries no controller lease (DL-41a). Query handlers read the oracle
store between feeds -- safe because feed() never yields.
"""

from __future__ import annotations

import asyncio
import heapq
import time
import uuid

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event
from dsl41.boundary import (
    BoundaryFailStop,
    CommittedBoundary,
    EstateHome,
    PeriodSealed,
    SealRequest,
    Snapshot,
    StagedContext,
    commit_boundary,
    executions_at,
    load_staged_catalog,
    QUIESCE_WAIT_S,
    executing_jobs,
    retry_horizon_gate,
    staged_bytes_for,
    validate_staged,
)
from dsl41.classify import Baseline, CarriedState, carried_from_oracle
from dsl41.runner_adapters import (
    AdapterContext,
    DetachSignal,
    JobAdapter,
    SealBarrier,
    SupervisorClient,
    SupervisorUnavailable,
    status_payload,
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
from dsl41.period import CMD_GRACE_S, StagedManifest, staging_dir
from dsl41.runner_journal import (
    Journal,
    read_journal,
)
from dsl41.runner_ledger import Fence
from dsl41.seal import Execution, SealedHost, SealedState, implicit_routes
from dsl41.runner_scheduler import Scheduler
from dsl41.timezones import alias_table


#: How many event-loop turns the sealed engine gives the control server to
#: write its answer before the loop unwinds. Three, not one: the server
#: awaits the future, shapes the answer and drains the writer, and each is
#: its own turn. The client's other route is the committed seal in the next
#: period (period-model ss2.2), so this is courtesy, never the contract.
_ANSWER_TURNS = 3


@dataclass
class _LiveRun:
    run_number: int
    task: asyncio.Task[None]


@dataclass
class _PendingSeal:
    """One boundary awaiting the single writer (period-model ss7).

    Not a `_Pending`: a seal is not an oracle input and takes no index. Its
    DECISION is the `seal` record, which is why it cannot ride the ordinary
    admission order -- before the seal a crash would leave a durable
    "applied" for a boundary that never happened, and after it records
    after a seal are forbidden (ss2.2)."""

    request: SealRequest
    future: asyncio.Future[CommittedBoundary]


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


class _Do(Enum):
    """What the loop does next (DL-137). One name per alternative, so the
    act is decided once -- in `Engine._next_work`, which owns the whole
    choice -- and carried out once, in `run_until_quiescent`."""

    #: take the queue head -- an input already raised
    EVENT = auto()
    #: advance to the calendar tick and enqueue its STARTJOB(s)
    TICK = auto()
    #: fire the due oracle timer as a verbless time observation
    TIMER = auto()
    #: nothing more before the horizon: return what this call emitted
    QUIESCE = auto()
    #: block until `at` or queue activity, then choose again
    WAIT = auto()


@dataclass(frozen=True)
class _Work:
    """One choice and the instant it is about: the tick for TICK, the
    effective due instant for TIMER, the wake target for WAIT (None where
    the real domain knows no instant at all and waits on activity alone).
    EVENT and QUIESCE carry none -- the queue head and "nothing" name
    themselves."""

    do: _Do
    at: datetime | None = None


def _raise_if_failed(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc  # adapter bug: fail loudly, never guess


def _oracle_aliases(scheduler: "Scheduler | None") -> dict[str, str] | None:
    """The SEM-35 alias table the ORACLE resolves `timezone:` through: the
    scheduler's own, so the two halves of one engine read a job's zone the
    same way (DL-62).

    The oracle used to resolve without it. Two consequences, both silent
    until a job started: a name only the `--timezone-map` table can resolve
    raised `OracleError` at the first start although preflight and the
    scheduler had accepted the estate, and a city name the table
    deliberately re-points still resolved through the ladder's unique-city
    default (DL-151).

    Empty reads as absent, by `timezones.alias_table`: the rule belongs to
    the ladder, and the period's own pin carries `{}` for both."""
    return alias_table(scheduler.tz_aliases if scheduler is not None else None)


class Engine:
    """ss4 single-writer engine loop over one Oracle. 11a surface: inject()
    external events + run_until_quiescent(horizon). The WAL journal slots in
    front of every feed (journal-first, ss7); the ss5 scheduler and the ss10
    control socket are the 11c event sources. `hold_open` keeps a real-domain
    loop waiting at quiescence instead of returning -- run mode serves the
    control socket until stopped, so "no work now" never means "no work can
    arrive" (ss10)."""

    #: How long the sealer waits an unbound SPAWN, an unresolved KILL ladder
    #: or a still-draining queue out before it refuses (period-model ss8).
    #: It is milliseconds in practice; the bound exists so a wedged tier
    #: refuses instead of hanging, and tests shorten it.
    QUIESCE_WAIT_S: float = QUIESCE_WAIT_S

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
        estate: EstateHome | None = None,
        fence: Fence | None = None,
        carried: CarriedRows | None = None,
    ) -> None:
        self.oracle = Oracle(catalog, carried=carried, tz_aliases=_oracle_aliases(scheduler))
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
        #: task ids whose failure _settle has ALREADY raised: the corpse
        #: stays in `_live`/`_reaping` so a catcher that treats the raise
        #: as reversible (the seal's abort) cannot consume the only
        #: observation -- but teardown must not raise the same failure a
        #: second time to a caller who already saw it
        self._corpses_raised: set[int] = set()
        #: concurrency-model ss5: what this engine intends to do to its
        #: execution hosts, and what came of it. Restored from the log on
        #: resume -- an engine that forgot a kill it had decided would leave a
        #: detached run orphaned for the rest of its life.
        self.outbox = Outbox()
        #: period-model ss1.3: what this engine leads besides its own run
        #: root. None on an engine with no lineage -- the bisimulation
        #: harness, a rehearsal -- and a seal request then has nothing to
        #: close.
        self.estate = estate
        #: ss8's supervisor clauses need the CLIENT, not just the adapter:
        #: the seal must prove the LIST it reconciles against came from the
        #: incarnation whose lease this engine holds (PR-27). None on a
        #: tethered engine -- wrappers are this process's children and EOF
        #: with it, so the clause is vacuous there.
        self.supervisor: SupervisorClient | None = None
        #: BOTH proofs, re-checked before every append, every dispatch and
        #: every revision-bearing read: this run root's `leader.lock` and
        #: the lineage's `anchor.lock` (PR-03). The journal holds the same
        #: object, so an append and a dispatch cannot prove different things.
        self.fence: Fence | None = fence
        #: ss6 step 2's freeze. An ENGINE flag, not a row field: the barrier
        #: freezes ADMISSION and holds no job, so an abort restores nothing
        #: on any row and a committed seal carries every `on_hold` exactly
        #: as the operator left it (PR-28c).
        self.sealing = False
        #: the same freeze, seen by an FW task at its poll boundary (ss3.5)
        self.barrier = SealBarrier()
        self._seal: _PendingSeal | None = None

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

    @property
    def cmd_grace_s(self) -> float:
        """The TERM grace the CMD adapter this engine RUNS waits before it
        kills (concurrency-model ss8's `T_kill` is two of these).

        Read off the wired adapter rather than off the period's manifest for
        `_derive_runtime_profile`'s reason: the bound has to describe the
        machine that runs. Resume holds the two to each other, so on a real
        estate they are the same number; an engine wired with neither -- the
        bisimulation harness -- gets the ss2.1 default.
        """
        cmd = self.adapters.get("CMD")
        grace = getattr(cmd, "grace_seconds", None)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)):
            return CMD_GRACE_S  # an adapter with no grace of its own (a test double)
        return float(grace)

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

    # ------------------------------------------------------ the boundary

    def submit_seal(self, request: SealRequest) -> Awaitable[CommittedBoundary]:
        """Queue ONE boundary and hand back its outcome (ss7, live mode).

        Not `submit`: a seal takes no index, addresses no row and is
        decided by the `seal` record rather than by a `decision`. What it
        shares with a mutation is the only thing that matters here -- it
        waits for the SINGLE WRITER, because the cutoff is the one act that
        must observe a state nothing else can move."""
        future: asyncio.Future[CommittedBoundary] = asyncio.get_running_loop().create_future()
        if self._seal is not None:
            future.set_exception(
                AdmissionRefused(
                    f"a boundary is already in flight (request_id"
                    f" {self._seal.request.request_id}): one seal at a time"
                )
            )
        else:
            self._seal = _PendingSeal(request=request, future=future)
            self._activity.set()
        return future

    def abort_boundary(self) -> None:
        """ss7: EVERY non-commit exit inside the reversible interval runs
        this, while the fence is still valid.

        It clears the sealing flag, reopens control admission, restarts
        scheduler admission and unparks FW tasks -- and it TOUCHES NO ROW,
        because the barrier held no job (ss6). Draft 20 said "refuses, C1
        still open" and a literal implementation returned exit 2 with the
        engine frozen behind ss6 step 2; draft 21 ran the abort only on
        validation failure, and an `ENOSPC` on the sidecar left a live
        engine frozen behind a freeze it would never lift (PR-28b)."""
        self.sealing = False
        self.barrier.release()
        self._activity.set()

    async def _seal_boundary(self) -> None:
        """One queued boundary, run to its outcome inside the loop.

        Three exits and they are three different facts: a commit raises
        `PeriodSealed` and the engine stops with code 3; a refusal answers
        the request, aborts, and C1 carries on -- the cutoff work already
        admitted stays as legitimate C1 activity; and a fail-stop after the
        `seal` append propagates WITHOUT an abort, because reopening
        admission behind a possibly-durable seal line is the one thing
        recovery cannot repair."""
        pending, self._seal = self._seal, None
        assert pending is not None
        try:
            committed = await self._run_boundary(pending.request)
        except BoundaryFailStop:
            raise
        except Exception as exc:
            # not just EngineError: an OSError from a pre-PONR write, fsync
            # or rename is exactly as reversible, and leaving admission
            # frozen behind it turns a transient disk error into a wedged
            # engine (ss7). commit_boundary wraps every post-PONR failure
            # in BoundaryFailStop, so anything else IS pre-PONR.
            if self.fence is not None and not self.fence.intact():
                # DL-101's rule, inside the reversible interval: a leader
                # that cannot prove it leads does not get to reopen
                # admission. It cannot un-run what happened; it turns a
                # divergence into a recorded stop (PR-28b)
                raise
            self.abort_boundary()
            if not pending.future.done():
                pending.future.set_exception(exc)
            return
        if not pending.future.done():
            pending.future.set_result(committed)
        # the control server's turn to write the answer before this loop
        # unwinds: the socket drops when the engine exits, and the client's
        # other route is the committed seal in the NEXT period (ss2.2)
        for _ in range(_ANSWER_TURNS):
            await asyncio.sleep(0)
        raise PeriodSealed(committed)

    async def _run_boundary(self, request: SealRequest) -> CommittedBoundary:
        """ss6's steps 2-8, in the single-writer loop.

        Step 1 is the OPERATOR's -- the runbook's hold set, placed before
        the seal -- and this barrier never touches `on_hold`: it freezes
        ADMISSION, which is an engine flag, and holds no job."""
        estate = self.estate
        if estate is None or self.journal is None:
            raise AdmissionRefused(
                "this engine leads no lineage: a run root with no estate anchor and no"
                " WAL has no boundary to close (period-model ss1.3)"
            )
        if request.epoch != self.epoch:
            # the same rule the ss4 order applies to every other external
            # input, at the one place a seal passes: a boundary composed by
            # a client still talking to a superseded leader is refused
            raise AdmissionRefused(
                f"epoch {request.epoch} is not this leader's {self.epoch}:"
                " re-read and re-compose against the current leader"
            )
        staged_ctx, staged_manifest = self._readiness(request, estate)
        self.sealing = True  # step 2
        self.barrier.park()
        await self._drain_admitted()
        at = self.clock.now()  # step 3
        await self._cutoff(at)  # steps 4-6
        # step 7, and the ss8 proof, until BOTH hold at once: the proof
        # awaits the supervisor, and a completion that lands during that
        # await would otherwise be snapshotted un-drained -- or not at all
        deadline = time.monotonic() + self.QUIESCE_WAIT_S
        while True:
            late = sorted(
                p.ev.kind if p.ev is not None else "request" for _, _, p in self._queue if p.at > at
            )
            if late:
                # C1 owns every instant <= T and nothing after it (ss6): an
                # input stamped past the cutoff cannot be admitted into the
                # period being sealed, and re-choosing T mid-seal would move
                # the boundary under the request that composed it. Refuse;
                # C1 reopens and the retry closes at a later T.
                raise EngineError(
                    f"input(s) {', '.join(late)} arrived stamped after the cutoff"
                    " T: they are C2's, and this boundary refuses rather than"
                    " snapshotting a state that has moved past its own T"
                    " (period-model ss6)"
                )
            if self._queue:
                # a completion at or before T that landed while the proof
                # yielded is C1 work (its run is C1's, PR-33a) -- drained to
                # its decision, then quiescence AND the proof are
                # re-established over the state it moved
                await self._drain_admitted()
            await self._await_quiescence(estate)
            await self._supervisor_proof(estate)  # ss8's supervisor clauses (PR-27)
            # the proof AWAITED: a task that died in that window surfaces
            # in _settle (which re-raises), and every quiescence condition
            # is re-checked -- exit only when a full pass moved nothing
            await self._settle()
            if not self._queue and self._not_quiescent(estate) is None:
                break
            if time.monotonic() > deadline:
                raise EngineError(
                    "inputs keep arriving during the ss8 supervisor proof: the estate"
                    f" is not settling within {self.QUIESCE_WAIT_S}s (period-model ss8)"
                )
        forced_gate = retry_horizon_gate(
            read_journal(self.journal.path),
            horizon_us=estate.manifest.runtime_profile.retry_horizon_us,
            at=at,
            force_seal=request.force_seal,
        )
        return commit_boundary(  # step 8
            run_root=estate.run_root,
            anchor=estate.anchor,
            journal=self.journal,
            estate_id=estate.estate_id,
            closing=estate.manifest,
            staged_ctx=staged_ctx,
            staged_manifest=staged_manifest,
            snapshot=self._snapshot(at, estate=estate),
            prev_seal_digest=estate.prev_seal_digest,
            forced_gate=forced_gate,
            crash_point=self.crash_point,
        )

    def crash_point(self, _stage: str) -> None:
        """The boundary crash matrix's seam, and nothing else
        (`runner_supervisor._crash_point`'s twin).

        A no-op in production. The ss11 matrix stops the operation exactly
        between two durable writes instead of killing a process and hoping
        it died in the window it meant."""

    def _readiness(
        self, request: SealRequest, estate: EstateHome
    ) -> tuple[StagedContext, StagedManifest]:
        """ss8's readiness, BEFORE the current period closes: phase 1 over
        exactly the staged bytes the request's digest names.

        A failure here refuses while C1 is still open and correct, which is
        the whole point of running it first."""
        if request.next_period.stage_digest != request.stage_digest:
            raise AdmissionRefused(
                f"the request stages {request.stage_digest} and its next_period digests to"
                f" {request.next_period.stage_digest}: the candidate is not the one the"
                " request names (period-model ss7)"
            )
        staged_manifest = staged_bytes_for(
            estate.run_root, request.stage_digest, next_period=estate.manifest.period_id + 1
        )
        if staged_manifest is None:
            raise AdmissionRefused(
                f"{staging_dir(estate.run_root, request.stage_digest)}: nothing is staged"
                " at this digest -- stage C2 before asking for the boundary that opens it"
                " (period-model ss7)"
            )
        now = self.clock.now()
        context = StagedContext(
            staged=request.next_period,
            staged_bytes=staged_manifest,
            boundary_request=request.boundary_request,
            request_fingerprint=request.fingerprint,
            c1=Baseline(catalog=self.oracle.catalog, profile=estate.manifest.runtime_profile),
            c2=load_staged_catalog(estate.run_root, staged_manifest),
            carried_state=self._carried(now),
            decision_index=self.decisions,
            state_machine_version=estate.manifest.state_machine_version,
            at=now,
        )
        validate_staged(context)
        return context, staged_manifest

    async def _drain_admitted(self) -> None:
        """ss6 step 2's other half: drain every ALREADY-ADMITTED attempt to
        its durable decision before any sidecar byte is written.

        The active seal request is deliberately NOT waited on -- its
        decision IS the `seal` record and cannot precede the sidecar, and
        draft 26 deadlocked on exactly that (PR-28e). It is not on this
        queue at all, which is what makes the exclusion structural rather
        than a special case."""
        deadline = time.monotonic() + self.QUIESCE_WAIT_S
        while self._queue:
            _, _, pending = heapq.heappop(self._queue)
            await self._admit_and_apply(pending)
            self._dispatch()
            await self._settle()
            if time.monotonic() > deadline:
                raise EngineError(
                    "the input queue is still filling after"
                    f" {self.QUIESCE_WAIT_S}s of draining: the estate is not settling,"
                    " and a boundary over a moving state is not a boundary"
                    " (period-model ss6 step 2)"
                )

    async def _cutoff(self, at: datetime) -> None:
        """ss6 steps 4-6: admit every scheduler tick due at or before T,
        advance the oracle through T firing every due semantic timer, and
        drain what that produced.

        `Scheduler._next` cannot be re-derived at a boundary -- resume
        re-anchors INCLUSIVE of `last_at` and dedups against the ticks the
        journal holds, and a seal cuts that evidence away. So C1 consumes
        every tick it owns here, and `scheduler_admitted_through: T` is the
        only carried evidence the next period needs: C1 owns every tick
        <= T, C2 owns every tick after it."""
        if self.scheduler is not None:
            for tick_ev in self.scheduler.pop_due(at):
                self._enqueue(tick_ev, source="scheduler")
            await self._drain_admitted()
        # a time observation is an input (DL-44): the firings it causes must
        # survive a crash, or replay would resurrect a job the oracle already
        # killed on the way to the cutoff. Unguarded on purpose -- a machine
        # clock that moved backwards refuses the boundary through
        # `Frontiers.admit` rather than skipping the observation and
        # committing a seal whose `now` is behind the log's own frontier
        await self._admit_and_apply(_Pending(at=at, request_id=None))
        self._dispatch()
        await self._drain_admitted()

    async def _await_quiescence(self, estate: EstateHome) -> None:
        """ss8's "always" set, re-checked after the cutoff (ss6 step 7).

        Three of its clauses are things the sealer WAITS OUT rather than
        refuses -- a KILL ladder that has not resolved to a spool proof, an
        applied CMD SPAWN whose adapter task has not yet written
        `spawn.json`, and an FW poll between its observation and its
        durable line. It is milliseconds, and snapshotting a half-run
        ladder would mean carrying a grace deadline the next period cannot
        honour (PR-27, PR-33a)."""
        deadline = time.monotonic() + self.QUIESCE_WAIT_S
        while True:
            await self._settle()
            self._dispatch()
            reason = self._not_quiescent(estate)
            if reason is None:
                return
            if time.monotonic() >= deadline:
                raise EngineError(
                    f"the estate is not quiescent at the cutoff: {reason}."
                    " A seal refuses rather than snapshots a half-run ladder, an unbound"
                    " spawn or a half-recorded poll (period-model ss8)"
                )
            await asyncio.sleep(0 if self.clock.virtual else 0.01)

    async def _supervisor_proof(self, estate: EstateHome) -> None:
        """ss8's supervisor clauses, at the seal (PR-27): the supervisor
        reachable, its LIST from the incarnation whose lease this engine
        holds, and that LIST reconciled BOTH ways against the executions
        the seal will carry.

        A reachable supervisor with an empty LIST is not proof: LIST shows
        what THIS incarnation spawned, and a restarted supervisor has a new
        incarnation and an empty history. Tethered engines return at once --
        wrappers are this process's children, and quiescence already
        settled them."""
        carried = self._executions(estate)
        if self.supervisor is None:
            if estate.manifest.runtime_profile.execution_mode == "detached" and any(
                e.kind in ("bound", "pending_spawn") for e in carried
            ):
                # a detached estate's live work is owned by a supervisor,
                # and an engine holding no client cannot prove that
                # supervisor reachable -- quiescence is unprovable, not
                # vacuous (period-model ss8, PR-27)
                raise EngineError(
                    "this estate runs detached and the engine holds no supervisor"
                    " client: the ss8 supervisor clauses cannot be proved over its"
                    " live executions, so the seal refuses (period-model ss8, PR-27)"
                )
            return
        bound = {(e.job, e.run_number): e for e in carried if e.kind == "bound"}
        unresolved = bool(bound) or any(e.kind == "pending_spawn" for e in carried)
        try:
            listing = await self.supervisor.list_runs()
        except SupervisorUnavailable as exc:
            if not unresolved:
                return  # it owns nothing a seal needs (PR-27a)
            raise EngineError(
                f"the supervisor is unreachable and this estate carries live detached"
                f" work ({exc}): quiescence is unprovable, the seal refuses"
                " (period-model ss8, PR-27)"
            ) from exc
        held = self.supervisor.incarnation
        if held is None or listing.get("incarnation") != held:
            raise EngineError(
                f"the supervisor's LIST is from incarnation"
                f" {listing.get('incarnation')!r} but this engine's lease names"
                f" {held!r}: a restarted supervisor's history is not proof"
                " (period-model ss8, PR-27)"
            )
        rows = {(str(r["job"]), int(r["run_number"])): r for r in listing.get("runs", [])}
        for key, entry in sorted(bound.items()):
            row = rows.get(key)
            if row is None:
                raise EngineError(
                    f"{key[0]}.{key[1]}: bound run {entry.run_id} is not in the"
                    " leased incarnation's LIST -- a carried non-terminal row the"
                    " sweep cannot account for refuses the seal (period-model ss8)"
                )
            if row.get("run_id") != entry.run_id:
                raise EngineError(
                    f"{key[0]}.{key[1]}: the supervisor's LIST names run_id"
                    f" {row.get('run_id')!r} but the bound run is {entry.run_id!r} --"
                    " an identity split at the seal refuses (DL-118)"
                )
        for key, row in sorted(rows.items()):
            if not row.get("wrapper_alive"):
                continue  # history: its outcome resolves from the spool
            ours = bound.get(key)
            if ours is None or ours.run_id != row.get("run_id"):
                raise EngineError(
                    f"{key[0]}.{key[1]}: the supervisor holds live run"
                    f" {row.get('run_id')!r} the seal's executions do not carry --"
                    " the sweep found evidence quiescence cannot account for"
                    " (period-model ss8, PR-27)"
                )

    def _not_quiescent(self, estate: EstateHome) -> str | None:
        """Why this estate may not be sealed right now, or None."""
        if self._queue:
            return f"{len(self._queue)} input(s) still queued"
        if self.frontiers.applied_index != self.frontiers.committed_index:
            return (
                f"attempt {self.frontiers.committed_index} is admitted and undecided"
                f" (applied through {self.frontiers.applied_index})"
            )
        if self._reaping:
            return f"{len(self._reaping)} KILL ladder(s) have not resolved"
        indeterminate = [
            effect.effect_id
            for effect in self.outbox.effects()
            if self.outbox.state_of(effect.effect_id) == "indeterminate"
        ]
        if indeterminate:
            return f"indeterminate effect(s) {', '.join(sorted(indeterminate))}"
        try:
            carried = self._executions(estate)
        except EngineError as exc:
            return str(exc)
        if estate.manifest.runtime_profile.execution_mode == "tethered":
            # the ss8 mode table: "in place, tethered -- full drain".
            # Stopping the engine cancels a tethered command (its wrapper is
            # this process's child), so a seal that carried it as `bound`
            # would name a run the exit code 3 then kills. Pending intents
            # carry (PR-16c) -- no live command dies with the engine -- and
            # FW watches carry (PR-34a) -- the file outlives the process.
            live = sorted(f"{e.job}.{e.run_number}" for e in carried if e.kind == "bound")
            if live:
                return (
                    f"tethered estate with live command(s) {', '.join(live)}: the seal"
                    " waits for the full drain -- stopping the engine cancels them"
                    " (period-model ss8 mode table)"
                )
        return None

    def _executions(self, estate: EstateHome) -> tuple[Execution, ...]:
        return executions_at(
            run_root=estate.run_root,
            outbox=self.outbox,
            rows=self.oracle.store.job,
            catalog=self.oracle.catalog,
            interval_default=max(
                1, round(estate.manifest.runtime_profile.fw_default_interval_us / 1_000_000)
            ),
        )

    def _carried(self, at: datetime) -> CarriedState:
        """The classifier's view of the estate at `at` (ss10.1).

        The three execution sets come from the WAL alone, because no ss10
        rule tells the three ss3.5 kinds apart and readiness runs before
        the sealer has waited an unbound SPAWN out."""
        executing = executing_jobs(self.outbox, self.oracle.store.job)
        return carried_from_oracle(
            self.oracle,
            now=at,
            pending_spawn=[job for job, state in executing.items() if state == "pending"],
            bound=[job for job, state in executing.items() if state == "applied"],
        )

    def _snapshot(self, at: datetime, *, estate: EstateHome) -> Snapshot:
        """The estate at T, taken WITHOUT yielding: everything below is a
        synchronous read of state the single writer owns, so nothing can
        move between the last precondition and the bytes the seal
        carries."""
        store = self.oracle.store
        return Snapshot(
            state=SealedState(
                jobs=dict(store.job),
                globals=dict(store.globals_),
                hosts={host_id: SealedHost.of(row) for host_id, row in store.hosts.items()},
                routes=implicit_routes(self.executor_id),
                timers=tuple(store.timers()),
                timer_seq=store.timer_seq,
                consumed=dict(store.consumed),
                enqueue_counter=store.enqueue_counter,
                now=at,
            ),
            carried=self._carried(at),
            outbox_pending=tuple(self.outbox.pending()),
            executions=self._executions(estate),
            closes_at_index=self.frontiers.applied_index,
            at=at,
            epoch=self.epoch,
        )

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
        if self.sealing and pending.envelope is not None:
            # ss6 step 2, at the one choke point every input passes: the
            # cutoff stops admitting EVERY externally requested attempt --
            # rejected and no-op ones included, since each takes a durable
            # decision. An attempt admitted after the cut would have its
            # decision land after the seal or not at all (PR-28e). The
            # engine's own doors stay open: the drain below has to finish,
            # and an adapter completion is C1 work, not a request.
            self._refuse(
                pending,
                AdmissionRefused(
                    "this period is sealing: nothing externally requested is admitted"
                    " after the cutoff (period-model ss6 step 2)"
                ),
            )
            return
        self._queue_seq += 1
        heapq.heappush(self._queue, (pending.at, self._queue_seq, pending))
        self._activity.set()

    def _next_work(self, horizon: datetime, now: datetime) -> _Work:
        """Choose the loop's next act. One decision, five named outcomes,
        where the 11c loop had three chained-negation booleans in front of
        two fall-through branches (DL-137).

        The booleans were `take_event`, `take_sched` and `fire_timer`, each
        re-stating the negation of the ones before it. Read them as three
        INDEPENDENT predicates over one observation:

          E  a queued input is takeable -- the queue head is at or before
             the horizon, no later than any timer, no later than any tick,
             and (real domain only) already due;
          S  a calendar tick is takeable -- admission is not frozen, the
             tick is at or before the horizon, STRICTLY before the queue
             head, no later than any timer, and (real domain) already due;
          T  a timer firing is takeable -- something is due, its effective
             instant is at or before the horizon, it is not held lazy by the
             frontier rule, and (real domain) already due.

        The old chain is then exactly the priority order E > S > T:

            E S T | old branch taken | choice
            ------+------------------+-----------------------------------
            0 0 0 | fell through     | QUIESCE or WAIT (see below)
            0 0 1 | fire_timer       | TIMER
            0 1 0 | take_sched       | TICK
            0 1 1 | take_sched       | TICK  (T masked by take_sched)
            1 0 0 | take_event       | EVENT
            1 0 1 | take_event       | EVENT (T masked by take_event)
            1 1 0 | IMPOSSIBLE       | --
            1 1 1 | IMPOSSIBLE       | --

        The last two rows cannot happen by construction, not by luck. E and
        S both require the OTHER's instant to be non-None before they
        compare against it, so with both true the queue head and the tick
        both exist -- and then E asks `head_at <= sched_due` while S asks
        `sched_due < head_at`. A tick and a queued input stamped at the same
        instant are E's, by S's strict `<`: the input feeds first and the
        tick waits one iteration. No priority between E and S is being
        chosen here; there is nothing to choose between.

        The `0 0 0` row keeps its own two-way split unchanged. The virtual
        domain is quiescent the moment nothing is takeable -- nothing can
        move without the clock. The real domain is quiescent only when no
        work exists AND none can appear; `hold_open` says more can always
        appear (ss10: run mode waits instead of returning). Otherwise the
        loop WAITS on the earliest instant it knows, or on the horizon when
        a live adapter's completion could still land inside it (DL-45), and
        chooses again. `WAIT` with no instant is the real domain's "nothing
        due at all": it waits on activity alone, PAST the horizon, which is
        what the pre-DL-137 loop did -- the horizon shortcut sits under
        `target is not None` there too, and is preserved, not tidied.

        Commit discipline (DL-45): the real domain commits to work only once
        its instant is due -- an earlier instant is waited out
        interruptibly, so a control injection or completion arriving
        mid-wait re-plans instead of feeding behind an already-journaled
        advance. Virtual jumps never yield, so the 11a determinism pins are
        untouched by those gates."""
        head_at = self._queue[0][0] if self._queue else None
        due = [
            t
            for t in (self.oracle.next_timer_due(), self.clock.next_sleeper_due())
            if t is not None
        ]
        raw_due = min(due) if due else None
        eff_due = max(raw_due, now) if raw_due is not None else None
        sched_due = self.scheduler.next_occurrence() if self.scheduler is not None else None
        if (
            head_at is not None
            and head_at <= horizon
            and (eff_due is None or head_at <= eff_due)
            and (sched_due is None or head_at <= sched_due)
            and (self.clock.virtual or head_at <= now)
        ):
            return _Work(_Do.EVENT)
        if (
            not self.sealing  # ss6 step 2: scheduler admission is frozen too
            and sched_due is not None
            and sched_due <= horizon
            and (head_at is None or sched_due < head_at)
            and (eff_due is None or sched_due <= eff_due)
            and (self.clock.virtual or sched_due <= now)
        ):
            return _Work(_Do.TICK, at=sched_due)
        if (
            raw_due is not None
            and eff_due is not None
            and eff_due <= horizon
            and (raw_due > now or horizon > now)
            and (self.clock.virtual or eff_due <= now)
        ):
            return _Work(_Do.TIMER, at=eff_due)
        if self.clock.virtual or (
            not self.hold_open
            and not self._live
            and not self._queue
            and raw_due is None
            and sched_due is None
        ):
            return _Work(_Do.QUIESCE)
        target = min((t for t in (eff_due, head_at, sched_due) if t is not None), default=None)
        if target is not None and target > horizon:
            # nothing KNOWN this side of the horizon -- but a live adapter's
            # completion has no due timestamp and can still land inside it,
            # so with live tasks wait out the horizon instead of abandoning
            # them (DL-45; the completion-at-horizon contract predates 11c)
            if not self._live or now >= horizon:
                return _Work(_Do.QUIESCE)
            target = horizon
        return _Work(_Do.WAIT, at=target)

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

        What the loop does each iteration is `_next_work`'s single choice;
        its docstring carries the truth table and the frontier rule's place
        in it.

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
            if self._seal is not None:
                # the boundary runs INSIDE the single-writer loop: it is the
                # one place that may freeze admission, choose T and snapshot
                # a state nothing else can move (ss6). A commit raises
                # `PeriodSealed` out of this loop; a refusal answers the
                # request, aborts, and the loop carries on with C1 open.
                await self._seal_boundary()
                continue
            now = self.clock.now()
            if now != instant:
                instant, instant_events = now, 0
            work = self._next_work(horizon, now)
            if work.do is _Do.EVENT:
                _, _, pending = heapq.heappop(self._queue)
                out = await self._admit_and_apply(pending)
                emitted.extend(out)
                self._dispatch()
            elif work.do is _Do.TICK:
                # the calendar tick is next: enqueue its STARTJOB(s), stamped
                # at the tick, and let the next iteration take them like any
                # external input (journal-first at feed; feed() fires timers
                # due <= tick first, identical to oracle-direct scripts)
                assert work.at is not None and self.scheduler is not None
                await self.clock.wait_until(work.at)
                for tick_ev in self.scheduler.pop_due(work.at):
                    self._enqueue(tick_ev, source="scheduler")
            elif work.do is _Do.TIMER:
                assert work.at is not None
                # a time observation is an input (DL-44 amendment): the timer
                # firings it causes must survive a crash, or resume replay
                # would resurrect a job the oracle already killed. It is
                # admitted exactly like an operator command -- an attempt with
                # no verb (concurrency-model ss4)
                out = await self._admit_and_apply(_Pending(at=work.at, request_id=None))
                emitted.extend(out)
                self._dispatch()
            elif work.do is _Do.QUIESCE:
                # virtual quiescence: nothing can move without the clock;
                # real quiescence: no work exists and none can appear, or
                # everything left is beyond the horizon (_next_work's `0 0 0`
                # row) -- unless hold_open, where the control socket can
                # always produce more (run mode waits instead of returning)
                return emitted
            else:
                # real domain: block until queue activity or the instant
                # _next_work chose -- the earliest due one, or the horizon
                # with live tasks. A completed adapter task also fires
                # _activity so _settle can re-raise adapter failures promptly.
                self._activity.clear()
                await self.clock.wait_until(
                    work.at if work.at is not None else datetime.max, interrupt=self._activity
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

    def _note_corpse(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._corpses_raised.add(id(task))

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
                    # raise FIRST: a task that failed stays in `_live`, so a
                    # catcher that treats the raise as reversible (the seal's
                    # abort path) cannot consume the only observation -- the
                    # loop's next settle re-raises the same corpse and the
                    # engine dies loudly, never resuming over an applied
                    # SPAWN whose task and completion are both gone
                    self._note_corpse(run.task)
                    _raise_if_failed(run.task)
                    del self._live[job]
            still_reaping: list[asyncio.Task[None]] = []
            for task in self._reaping:
                if task.done():
                    self._note_corpse(task)
                    _raise_if_failed(task)
                else:
                    still_reaping.append(task)
            self._reaping = still_reaping
            if not self.clock.virtual:
                return
            if (
                not self._reaping
                and len(self._live) == self.clock.pending_sleepers() + self.barrier.parked_tasks
            ):
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
        applied = apply_attempt(self.oracle, attempt, grace_s=self.cmd_grace_s)
        self.decisions.record(applied.result)
        self.frontiers = self.frontiers.record(attempt.index)
        # step 7 commits the decision and the outbox entries it implies as ONE
        # batch (concurrency-model ss1): an engine that dies between deciding
        # and acting must leave behind the record that it MEANT to act, or a
        # kill it decided vanishes with the task that would have delivered it.
        # ONE record and one fsync since DL-118 -- separate writes made "as one
        # batch" a claim the substrate did not keep (CM-17)
        effects = self._plan_effects(applied, attempt.index)
        if self.journal is not None:
            self.journal.decision(applied.result, effects)
        for effect in effects:
            self.outbox.record(effect)
        if applied.result.decision == "rejected" and ev is not None:
            assert applied.result.reason is not None
            self.drops.append((ev, applied.result.reason))
        self._answer(pending, applied.result)
        return applied.emitted

    def _plan_effects(self, applied: Applied, index: int) -> list[Effect]:
        """ss4 step 7's other half: what the shell now intends to do about
        what the oracle just decided.

        The two identity binds happen HERE, inside the decision transaction
        (period-model ss2.3): a SPAWN's `run_id` is minted with the effect
        (PR-36a), and `generation` is read from the executor's host row at
        this moment (PR-16) -- the row exists from the genesis seed, so the
        fallback 0 is the seed's own value and unreachable in practice. A
        KILL looks its run's id up in the outbox, where the SPAWN that
        started it recorded it; a run the outbox holds no binding for has
        none to find."""
        row = self.oracle.store.host(self.executor_id)
        return plan_effects(
            applied.emitted,
            index=index,
            executor_id=self.executor_id,
            generation=row.generation if row is not None else 0,
            runs={job: rt.run_number for job, rt in self.oracle.store.job.items()},
            dispatched=self._dispatched,
            live={job: run.run_number for job, run in self._live.items()},
            dispatchable=self._dispatchable(),
            run_ids={
                (e.job, e.run_number): e.run_id
                for e in self.outbox.effects()
                if e.kind == "SPAWN" and e.run_id is not None
            },
            mint_run_id=lambda: str(uuid.uuid4()),
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
        overtook it would stop a run that had not started.

        The fence is re-proved HERE and not only in the journal writer
        (period-model ss1.3): an append and a dispatch are two acts, and a
        leader that lost the lineage between them would start a process for
        an estate it no longer leads (PR-03)."""
        pending = self.outbox.pending()
        if pending and self.fence is not None:
            self.fence.check()
        for effect in pending:
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
        if job_ir is None or adapter is None:  # pragma: no cover -- see below
            # Unreachable through planning: `_dispatchable` filters both, and
            # a log's effects can only name jobs its own catalog had (the
            # ss7 hash gate refuses a resume against a changed estate). It is
            # kept, not deleted, because what it guards is a DISAGREEMENT
            # between catalog and log -- the one thing that would make the
            # alternative a KeyError in the middle of dispatch.
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
        if stale is not None:  # pragma: no cover -- see below
            # One live attempt per job. Unreachable while the ORACLE refuses to
            # start a job that is STARTING/RUNNING/QUE_WAIT (DL-81), which is
            # what stops run_number advancing under a live task; the guard is
            # kept because that refusal lives two modules away, and a change to
            # it would otherwise leak a task rather than fail. A report from
            # the old task would be gate-dropped anyway (run_number mismatch)
            # -- cancelling is the tidy half, not the safety half.
            stale.task.cancel()
            self._reaping.append(stale.task)
        self._launch(job_ir, effect.run_number, adapter, run_id=effect.run_id)
        self._resolve_effect(EffectOutcome(effect_id=effect.effect_id, state="applied"))

    def _apply_kill(self, effect: Effect) -> None:
        """The oracle decided terminal; the shell stops the run.

        Cancelling the adapter task IS the effect at this tier -- the
        adapter's TERM/grace/KILL ladder is the lifecycle tier's business and
        runs on the way out. `applied` therefore means the cancellation was
        delivered to a live run, which is the whole of what this tier can
        promise; the wrapper records what became of the process."""
        live = self._live.pop(effect.job, None)
        if live is None:  # pragma: no cover -- see below
            # Unreachable through planning, and deliberately so: `plan_effects`
            # refuses to plan a KILL for a job with no live run, because such
            # an effect could only ever be superseded (its own docstring, and
            # a test pins it). Re-driven kills at resume take
            # `_redrive_recorded_kills` instead, which resolves every pending
            # KILL before the barrier's dispatch reaches this.
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

    def _launch(
        self, job_ir: JobIR, run_number: int, adapter: JobAdapter, *, run_id: str | None = None
    ) -> None:
        """Create the adapter task. Reached from `_apply_spawn` (an outbox
        effect) and from resume's FW re-dispatch and detached reattach
        (module docstring), neither of which goes through an effect: one is
        an idempotent re-read and the other is a run that never stopped.
        `run_id` is the effect's, on the `_apply_spawn` path only -- the
        default None is exactly those two effect-less callers, plus a
        re-driven SPAWN the outbox holds no binding for."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_adapter(job_ir, run_number, adapter, run_id))
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
        for task, result in zip(tasks, results, strict=True):
            if id(task) in self._corpses_raised:
                continue  # _settle already raised this failure to a caller
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _run_adapter(
        self, job_ir: JobIR, run_number: int, adapter: JobAdapter, run_id: str | None = None
    ) -> None:
        ctx = AdapterContext(
            clock=self.clock,
            run_root=self.run_root,
            journal=self.journal,
            detach=self.detach,
            run_id=run_id,
            barrier=self.barrier,
            fence=self.fence,
        )
        result = await adapter.run(job_ir, run_number, ctx)
        # (job, run_number) ride along for the ss4 stale-completion gate
        payload: dict[str, object] = {"job": job_ir.name, "run_number": run_number}
        payload |= status_payload(result, where=f"adapter for {job_ir.name!r}")
        # source="adapter" is what makes this a COMPLETION, and therefore
        # what subjects it to the ss4 stale gate (runner_admission)
        self._enqueue(Event(at=self.clock.now(), kind="STATUS", payload=payload))
