"""Runner engine: the sans-IO shell over the oracle.

Normative spec: docs/runner-design.md (DL-41, DL-41a). The Oracle is the single semantics
authority; the engine contributes dispatch (adapters), time (the ss9 clock domains),
durability (WAL), the calendar scheduler and the control surface. The bisimulation gate
(ss13, ss14) runs every SEM trace test through both Oracle-direct and Engine(VirtualClock,
FakeAdapter) with identical traces.

The clocks (runner_clock), adapters and supervisor client (runner_adapters), WAL
(runner_journal), calendar scheduler (runner_scheduler; its timezone ladder lives in
`timezones`, DL-163), preflight (runner_preflight), the ss10 control plane (runner_control,
DL-78) and possession of a run root (genesis, resume, ss7's takeover barrier:
runner_startup, DL-106) each own their file and their part of this contract (DL-74). This
module is the engine loop that runs while an estate is live, over an Engine that
runner_startup constructs and hands over.

Engine loop (ss4, single writer): exactly one task owns the Oracle, which is not reentrant.
Event sources are injected events, the control socket, the scheduler and adapter
completions. Each iteration processes the earliest work item at or before the horizon.
Determinism pins, each with a test:

- A queued event and an oracle timer due at the same instant: the event is fed and
  Oracle.feed() fires timers due <= ev.at first, so the timer precedes the event, as in
  oracle-direct scripts.
- An oracle timer due strictly before every queued event: the clock advances and
  Oracle.advance(now) fires it. Feed-only and advance+feed are equivalent (ss13). Timers due
  at or before the already-processed instant follow the frontier rule (run_until_quiescent
  docstring): they stay lazy until time moves past that instant, as oracle-direct feed()
  leaves them armed until the next event.
- An adapter sleep due: the same clock advance resolves it; the adapter task then enqueues
  its completion, which feeds like any other event. The event queue is ordered by
  (at, arrival seq), not FIFO: pre-injected script events carry future timestamps while
  completions enqueue at the processed frontier, so FIFO would feed a later-stamped external
  ahead of an earlier completion and break the oracle's non-decreasing feed discipline. At
  equal times arrival order decides.

Under VirtualClock the natural-exit vs kill race always resolves to the kill: a terminal
decision cancels the adapter task before its completion can enqueue (the settle step separates
resolution from enqueueing, and cancel lands between them). The stale-completion gate
therefore guards the REAL time domain, where a process exit can already be queued when the
oracle decides terminal; virtual runs exercise it only white-box (test_runner.py).

Dispatch table (ss4): an emitted STATUS STARTING for a job_type with a registered adapter
spawns an adapter task, but only for an ORACLE-DECIDED start, recognized by the run_number
bump every real start performs (the ghost-run gate). An injected CHANGE_STATUS-parity STARTING
overwrite re-emits STARTING without bumping and, vendor parity, launches nothing. An emitted
terminal status for a job with a live task cancels it (KILLJOB / term_run_time: the oracle
decides, the shell kills). A cancelled adapter never reports, and anything it dies with other
than the cancellation re-raises at the next settle. Boxes have no adapter row; ON_NOEXEC
bypass never emits STARTING, so nothing spawns. MUST_START/MUST_COMPLETE alarms are journal
and UI surface only.

Stale-completion gate (ss4, DL-41 decision 4): completions carry (job, run_number). The engine
drops any completion whose run_number mismatches the current one or whose row is not
STARTING or RUNNING (terminal, or INACTIVE or QUE_WAIT after an injected INACTIVE; DL-235),
and records it on Engine.drops and in the WAL. The gate guards ONLY engine-made completions:
externally injected STATUS keeps sendevent CHANGE_STATUS parity and may overwrite terminal
statuses (oracle module docstring). `Event.source` identifies the engine-made ones (DL-68), so
replay reaches the same verdict from the same field.

Admission (concurrency-model ss4, DL-89): every input goes through ONE admission order (S2):
operator commands, scheduler ticks, adapter completions, reconciliation injections and
standalone time observations. `_admit_and_apply` is that order, and its docstring maps it to
the frozen steps. The gate above runs AFTER the input is durably admitted, so a rejection is a
recorded decision and replay can honour it. The batch's time observation applies even when the
attempt is rejected (DL-44's advance record). The engine holds the log's position in
`frontiers` and its decisions in `decisions`, both restored from the log on resume.

Preconditions (concurrency-model ss0/ss6, DL-90) are MANDATORY (S3) for externally requested
mutations. `submit` is the external door: it takes a concurrency-model ss6 envelope carrying
the revision the caller read and returns the DECISION; the loop resolves its future at step 7,
because a precondition whose outcome the caller cannot see is not a precondition. `inject` is
the engine's own door: the scheduler, adapters, reconciliation and test scripts are inside the
trust boundary, and ss0's rule is about what crosses it. A refusal (steps 1-2) and a rejection
(step 6) are different facts: a refusal never entered the log and is recorded only on
`refused_inputs`; a rejection is a decision with an index.

Lifecycle (ss6-ss7; DL-41a and DL-42 pin the semantics):

- Kill-wins gate ordering (DL-44): before gating a completion, the engine fires the oracle
  timers due at or before the completion's timestamp (feed() fires exactly these anyway), so the
  gate sees every kill decision first and drops the late natural exit. A kill, once decided, is
  never overwritten by a completion the engine made. Externally injected STATUS keeps
  CHANGE_STATUS overwrite parity.
- Resume (ss7) lives in runner_startup (DL-106). Reconciliation completions arrive through
  `inject(source="reconcile")` and go through the ss4 stale gate like any adapter completion,
  so a late real record cannot overwrite a terminal state replay already reached.

Loop commit discipline (ss5, ss8; DL-45): in the real domain the loop commits to work
(journaling an advance, popping a scheduler tick, feeding an event) only once its instant is
due (<= now). Anything earlier is waited for INTERRUPTIBLY, so a control injection or adapter
completion arriving mid-wait re-plans the iteration; otherwise a completion stamped inside the
sleep would feed behind the already-advanced oracle clock. Virtual-domain jumps never yield
mid-move.

The ss10 control plane (socket server, wire vocabulary, clients) lives in runner_control.py
(DL-78) and is frozen in docs/control-protocol.md. Control mutations arrive through
Engine.submit and submit_host (routes). Both are journaled by the queued-input path
(`_Do.EVENT`), and the single-writer loop serializes them, so that tier carries no controller lease (DL-41a). Query handlers read the oracle store
between feeds, which is safe because feed() never yields.
"""

from __future__ import annotations

import asyncio
import heapq
import uuid

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Final

from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event, EventSource
from dsl41.runner_adapters import (
    AdapterContext,
    FileWatcherAdapter,
    JobAdapter,
    status_payload,
)
from dsl41.runner_admission import (
    ENGINE_REQUEST_ID_PREFIX,
    INERT_EPOCH,
    AdmissionRefused,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    Frontiers,
    RequestCollision,
    Applied,
    TimeHalfFault,
    TimeHalfStop,
    apply_attempt,
    classify_fault,
    fingerprint,
    report_quarantine,
    trial_apply,
)
from dsl41.runner_codes import Rejection
from dsl41.runner_clock import Clock, EngineError, ZeroDelayCycleError
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
from dsl41.run_root import (
    CMD_GRACE_S,
    Manifest,
    OracleReading,
    oracle_reading,
    switches_of,
)
from dsl41.runner_journal import (
    Journal,
    RecoveredAttempt,
)
from dsl41.runner_ledger import Fence
from dsl41.runner_scheduler import Scheduler
from dsl41.semantics import ADAPTER_SWITCHES, SCHEDULER_SWITCHES, SemanticSwitches
from dsl41.semantics import DEFAULTS as DEFAULT_SWITCHES
from dsl41.state_machine import Executor, StatechartError
from dsl41.timezones import alias_table


#: floor for the zero-delay-cycle guard's same-instant event budget (`run_until_quiescent`'s
#: frontier-rule docstring): `max(INSTANT_BUDGET_FLOOR, 100 * len(catalog.jobs))`. A module
#: constant so tests can lower it without a new `rehearse_check` parameter (DL-211).
INSTANT_BUDGET_FLOOR: Final = 10_000


@dataclass
class _LiveRun:
    run_number: int
    task: asyncio.Task[None]


@dataclass
class _Pending:
    """One input awaiting admission.

    A client's `request_id` rides from the moment the input is raised, because concurrency-model
    ss4 deduplicates BEFORE it stamps or indexes anything.

    `envelope` present marks an EXTERNALLY REQUESTED input, one that crossed the ss10 socket and
    named the revision it was composed against (concurrency-model ss0). Absent marks the engine's
    own input: a timer, a scheduler tick, an adapter completion, a reconciliation. `future`
    carries the requester's decision (ss4's second signal).

    A `request_id` of None marks an input the engine raised itself. It takes its identity in the
    log from the admission index, which is never looked up: nobody outside this process can retry
    it, and a per-incarnation counter would collide with the ids a previous incarnation left in
    the log.

    `ev` absent is an attempt with no oracle verb: a routing-table command (`host`) or, with
    neither, a standalone time observation. ss4 admits all three by the same rule.

    `source` is read only when `ev` is absent; an event carries its own (DL-68)."""

    at: datetime
    request_id: str | None
    ev: Event | None = None
    host: HostCommand | None = None
    source: EventSource | None = None
    envelope: Envelope | None = None
    future: asyncio.Future[ApplyResult] | None = None


def _without_frames(exc: Exception) -> Exception:
    """`exc` and the exceptions chained to it, stripped of their tracebacks so keeping it pins no
    frame."""
    seen: set[int] = set()
    links: list[BaseException | None] = [exc]
    while links:
        link = links.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        link.__traceback__ = None
        links += [link.__cause__, link.__context__]
    return exc


class _Do(Enum):
    """What the loop does next (DL-137, DL-150; DL-145 keeps the branch count).

    `Engine._next_work` decides the act once; `run_until_quiescent` carries it out once."""

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
    """One choice and the instant it is about: the tick for TICK, the effective due instant for
    TIMER, the wake target for WAIT (None where the real domain waits on activity alone). EVENT
    and QUIESCE carry none."""

    do: _Do
    at: datetime | None = None


def _raise_if_failed(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            raise exc  # adapter bug: fail loudly, never guess


def adapter_switch(adapters: Mapping[str, JobAdapter], name: str) -> str | None:
    """The value of adapter switch `name` (`ADAPTER_SWITCHES`) that the wired adapter runs, or
    None when no adapter that reads it is wired (DL-258, DL-280).

    A name in `ADAPTER_SWITCHES` this function has no read for raises ValueError, so a new
    `reader="fw"` entry cannot pass the drift gates unread."""
    fw = adapters.get("FW")
    if name == "fw-existence":
        return fw.existence if isinstance(fw, FileWatcherAdapter) else None
    if name in ADAPTER_SWITCHES:
        raise ValueError(f"adapter switch {name!r} has no read in runner.adapter_switch")
    return None


def _engine_switches(
    manifest: Manifest | None,
    semantics: SemanticSwitches | None,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
) -> SemanticSwitches:
    """The semantic switches an engine runs (DL-252).

    An engine over a run root runs its manifest's pin. `semantics` is for an engine with no
    manifest; one that disagrees with the pin is refused. So is a scheduler built under another
    value of a switch it reads (DL-259, DL-260), since its calendars or start instants would
    fire under a reading the period and oracle do not use; the derived runtime profile reads
    those switches from the scheduler, so a launcher meets this before any durable write and this
    check is the backstop. The same holds for a wired adapter built under another value of a
    switch it reads (DL-258, DL-280), once a pin or `semantics` names a reading."""
    if manifest is None:
        switches = semantics or DEFAULT_SWITCHES
    else:
        switches = switches_of(manifest.runtime_profile)
        if semantics is not None and semantics != switches:
            raise EngineError(
                f"semantic switches {semantics} disagree with the period's pin {switches}"
                " (runner-design ss8a)"
            )
    if scheduler is not None:
        # only the switches the scheduler reads; another `ice-lookback` fires the same ticks
        for name in SCHEDULER_SWITCHES:
            if scheduler.semantics.value(name) != switches.value(name):
                raise EngineError(
                    f"the scheduler compiled under {name}="
                    f"{scheduler.semantics.value(name)}, the engine runs"
                    f" {switches.value(name)} (runner-design ss8a)"
                )
    if manifest is not None or semantics is not None:
        for name in ADAPTER_SWITCHES:
            wired = adapter_switch(adapters, name)
            if wired is not None and wired != switches.value(name):
                raise EngineError(
                    f"the wired adapter runs {name}={wired}, the engine runs"
                    f" {switches.value(name)} (runner-design ss8a)"
                )
    return switches


def _engine_oracle(
    catalog: CatalogIR,
    carried: CarriedRows | None,
    manifest: Manifest | None,
    semantics: SemanticSwitches | None,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
) -> Oracle:
    """The oracle an `Engine` reads its catalog through."""
    # SEM-35: the base zone and alias table the oracle reads a job's time attributes in. They
    # come from the period's pinned profile, which every replay reads (`oracle_reading`), else
    # from the scheduler: both halves of one engine read a job's zone the same way (DL-62,
    # DL-253). Empty aliases read as absent.
    switches = _engine_switches(manifest, semantics, scheduler, adapters)
    if manifest is not None:
        # the zone and aliases from the pin; the switches as checked above
        reading = OracleReading(
            **{**oracle_reading(manifest.runtime_profile), "semantics": switches}
        )
    else:
        reading = OracleReading(
            default_tz=scheduler.default_tz if scheduler is not None else None,
            tz_aliases=alias_table(scheduler.tz_aliases if scheduler is not None else None),
            semantics=switches,
        )
    return Oracle(catalog, carried=carried, **reading)


class Engine:
    """The ss4 single-writer engine loop over one Oracle.

    Surface: inject() external events and run_until_quiescent(horizon). The WAL journal precedes
    every feed (ss7); the ss5 scheduler and the ss10 control socket are event sources.
    `hold_open` keeps a real-domain loop waiting at quiescence: run mode serves the control
    socket until stopped, so "no work now" never means "no work can arrive" (ss10)."""

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
        manifest: Manifest | None = None,
        fence: Fence | None = None,
        carried: CarriedRows | None = None,
        semantics: SemanticSwitches | None = None,
        machines: Executor | None = None,
    ) -> None:
        self.oracle: Oracle = _engine_oracle(
            catalog, carried, manifest, semantics, scheduler, adapters
        )
        #: concurrency-model ss2/ss8: the execution host this engine dispatches to. One engine
        #: per run root owns one local executor; machine names resolve to a relay through the
        #: routing table (ss5), and with no relay (S5d) every job routes here. It is seeded into
        #: the table at genesis, not admitted, so no log index records how the process was
        #: launched (see seed_local_executor). `deadman_s` is what the LOCAL SUPERVISOR reports
        #: it runs, so the ss8 eviction bound describes the host, not this engine's launch
        #: options (S5b).
        self.executor_id = executor_id
        seed_local_executor(self.oracle.store, executor_id, at=clock.now(), deadman_s=deadman_s)
        self.clock = clock
        self.adapters = dict(adapters)  # job_type -> adapter; no BOX row
        self.journal = journal
        self.run_root = run_root
        self.scheduler = scheduler
        self.hold_open = hold_open
        #: events not applied: step 6's gate rejections and resume's missed
        #: ticks (WAL `drop` records). A quarantine is on `quarantined` only
        self.drops: list[tuple[Event, str]] = []
        self.baseline_id = journal.baseline_id if journal is not None else ""
        #: concurrency-model ss2: the leader's fencing token, allocated by the run root's ledger
        #: when this incarnation took the term (S6a, runner_ledger). INERT_EPOCH means no
        #: election was held: an Engine with no run root and no log, as in the bisimulation
        #: harness. The check sits after dedup (ss4), so an exact old-epoch retry recovers its
        #: original result.
        self.epoch = epoch
        self.frontiers = Frontiers()
        #: retries answered from the index rather than re-applied (CM-05)
        self.deduped: list[tuple[str, ApplyResult]] = []
        #: refused before admission (concurrency-model ss4), so this list is
        #: their only record: each event (None for a verbless input) and its refusal
        self.refused_inputs: list[tuple[Event | None, AdmissionRefused]] = []
        #: engine-made inputs quarantined live, each with the fault its dry apply met: logged with
        #: a rejected `engine_apply_faulted` decision, their time half applied (concurrency-model
        #: ss4); `rehearse` prints each one and fails
        self.quarantined: list[tuple[ApplyResult, Exception]] = []
        self._init_live_state()
        #: this engine's statechart executor (statechart-engine ss11): one worklist for every
        #: instance. Its machines are `ADMISSION`, one instance per request id in flight
        #: (DL-334); `EFFECT`, one per open effect of the outbox (DL-331); `SUBSCRIPTION`, one
        #: per subscribe feed, started by the
        #: control server through `machines`. An engine given no executor makes its own
        self._machines = machines if machines is not None else Executor()
        #: concurrency-model ss5: what this engine intends to do to its execution hosts, and what
        #: came of it; restored from the log on resume, so a decided kill is not forgotten
        self.outbox = Outbox(self._machines)
        #: the decision index, built here because it runs each request id in flight on this
        #: engine's executor
        self.decisions = DecisionIndex(self._machines)
        #: the run root's committed manifest, the pin the oracle reads under. None on an engine
        #: with no run root (the bisimulation harness, a rehearsal)
        self.manifest = manifest
        #: this run root's `leader.lock` proof, re-checked before every append and dispatch. The
        #: journal holds the same object, so an append and a dispatch prove the same thing.
        self.fence: Fence | None = fence

    def _init_live_state(self) -> None:
        """The queue and the live-run books `__init__` starts empty."""
        #: time-ordered input queue: (at, arrival seq, pending); provenance rides on
        #: Event.source (DL-68)
        self._queue: list[tuple[datetime, int, _Pending]] = []
        self._queue_seq = 0
        #: real-domain wake signal, set on every enqueue and adapter-task completion
        self._activity = asyncio.Event()
        self._live: dict[str, _LiveRun] = {}
        #: last run_number dispatched per job: the ghost-run gate. An injected CHANGE_STATUS
        #: STARTING overwrite does not bump run_number and, vendor parity, launches nothing; only
        #: an oracle-decided start advances the counter
        self._dispatched: dict[str, int] = {}
        #: cancelled tasks awaiting collection; _settle re-raises any non-CancelledError they die
        #: with
        self._reaping: list[asyncio.Task[None]] = []
        #: task ids whose failure _settle has already raised: the corpse stays in
        #: `_live`/`_reaping` so a reversible catcher cannot consume the only
        #: observation, and teardown does not raise the same failure twice
        self._corpses_raised: set[int] = set()

    @property
    def machines(self) -> Executor:
        """This engine's one statechart executor (statechart-engine ss11)."""
        return self._machines

    @property
    def outbox(self) -> Outbox:
        """The engine's outbox. One on another executor is refused, since it would split the
        process's one worklist (statechart-engine ss11)."""
        return self._outbox

    @outbox.setter
    def outbox(self, outbox: Outbox) -> None:
        if outbox.machines is not self._machines:
            raise StatechartError(
                "effect: the outbox runs on another executor than the engine's"
                " (statechart-engine ss11)"
            )
        self._outbox = outbox

    @property
    def decisions(self) -> DecisionIndex:
        """The engine's decision index. One on another executor is refused, since it would split
        the process's one worklist (statechart-engine ss11)."""
        return self._decisions

    @decisions.setter
    def decisions(self, decisions: DecisionIndex) -> None:
        if decisions.machines is not self._machines:
            raise StatechartError(
                "admission: the decision index runs on another executor than the engine's"
                " (statechart-engine ss11)"
            )
        self._decisions = decisions

    def note_executor_contact(self) -> None:
        """Stamp positive contact with this engine's own execution host (concurrency-model ss8).

        A confirmed ACQUIRE or RENEW of the supervisor client's lease is the leader hearing back
        from the host, which the eviction bound counts from. `last_contact` is outside the ss3
        semantic projection, so this moves no revision and writes no log record. Clearing a
        quarantine is an input and a separate call."""
        self.oracle.store.touch_host(self.executor_id, self.clock.now())
        self.note_executor_reachable()

    @property
    def cmd_grace_s(self) -> float:
        """The TERM grace the wired CMD adapter waits before it kills (concurrency-model ss8's
        `T_kill` is two of these).

        It is read off the adapter, not the period's manifest, because the bound describes the
        machine that runs; resume holds the two equal. An engine wired with neither gets the
        ss2.1 default."""
        cmd = self.adapters.get("CMD")
        grace = getattr(cmd, "grace_seconds", None)
        if isinstance(grace, bool) or not isinstance(grace, (int, float)):
            return CMD_GRACE_S  # an adapter with no grace of its own (a test double)
        return float(grace)

    def held_jobs(self) -> frozenset[str]:
        """Jobs with a start this shell intended and has not dispatched, because their executor
        routes no new effects (concurrency-model ss8).

        The outbox is the held set (S5c, DL-94): a pending SPAWN is an intent recorded and not
        yet applied, and it survives a restart. A second derivation would be the parallel model
        DL-91 forbids."""
        return frozenset(e.job for e in self.outbox.pending() if e.kind == "SPAWN")

    def live_jobs(self) -> frozenset[str]:
        """Jobs with an in-flight adapter task; a filewatch is ONLY an in-flight task, so the ss10
        read model needs it (DL-78)."""
        return frozenset(self._live)

    def inject(
        self, ev: Event, *, source: EventSource | None = "control", request_id: str | None = None
    ) -> None:
        """Queue an external event (test scripts; ss10 sendevent verbs); it is never gated.

        Injected STATUS keeps its CHANGE_STATUS parity. source=None injects unattributed
        (oracle-direct scripts carry no provenance, DL-68). `request_id` makes the injection
        retryable: an exact retry under the same id is answered from its first decision and
        applies nothing (concurrency-model ss4 step 2). Omitted, the engine names the input."""
        self._enqueue(ev, source=source, request_id=request_id)

    def submit(
        self, ev: Event, envelope: Envelope, *, source: EventSource = "control"
    ) -> Awaitable[ApplyResult]:
        """Queue one EXTERNALLY REQUESTED mutation and return its decision (concurrency-model
        ss0/ss4).

        The envelope carries the precondition, so this is the only entry point that satisfies
        ss0's mandate. The awaitable resolves when step 7 records the result, or raises
        `AdmissionRefused` if steps 1-2 refuse it. It never resolves before the decision exists:
        `command_committed` ("your envelope is durable") does not answer "did my kill land"."""
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
        self, cmd: HostCommand, envelope: Envelope, *, source: EventSource = "control"
    ) -> Awaitable[ApplyResult]:
        """Queue one routing-table change and return its decision (concurrency-model ss8).

        It takes the same envelope, admission order and four outcomes as `submit`. The attempt
        carries no oracle event, so nothing is fed; the ss3 owner is written directly inside the
        batch (DL-93)."""
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

        It is on the trust side of ss0's boundary, like the scheduler and the adapters: no
        envelope, no `expect`, no future. It is still an admitted input, journaled and replayed,
        so a quarantine survives a restart and the next engine routes no work at a host that is
        not answering."""
        self._push(_Pending(at=self.clock.now(), request_id=None, host=cmd, source="reconcile"))

    def note_executor_unreachable(self) -> None:
        """The leader has lost contact with its own execution host (ss8).

        It fires on the fifth consecutive failed renewal of an outage (DL-291), not on a single
        failure. New work is then HELD until the host answers, instead of every spawn failing
        against an absent supervisor and marking its job FAILURE."""
        self.inject_host(HostCommand(verb="quarantine", host_id=self.executor_id))

    def host_state(self) -> str | None:
        """This engine's executor's routing state (ss8), or None with no row: a held SPAWN's
        resend passes the routing gate a fresh effect passes (DL-416)."""
        row = self.oracle.store.host(self.executor_id)
        return row.state if row is not None else None

    def note_executor_reachable(self) -> None:
        """Contact restored: the leader clears what it set and restores the state it interrupted
        (a drained host stays drained)."""
        row = self.oracle.store.host(self.executor_id)
        if row is not None and row.state == "quarantined":
            self.inject_host(HostCommand(verb="reinstate", host_id=self.executor_id))

    def _enqueue(
        self,
        ev: Event,
        *,
        source: EventSource | None = "adapter",
        request_id: str | None = None,
        envelope: Envelope | None = None,
        future: asyncio.Future[ApplyResult] | None = None,
    ) -> None:
        ev.source = source  # DL-68: the event carries its own provenance
        self._push(
            _Pending(at=ev.at, request_id=request_id, ev=ev, envelope=envelope, future=future)
        )

    def observe_opening(self) -> None:
        """DL-256: a period opened over a removed job that still holds units owes their release to
        its first input. Admit a time observation for it now, stamped at the latest of the clock,
        the last admitted instant and the oracle's clock. A no-op when nothing is owed."""
        if not self.oracle.opening_release_owed:
            return
        floors = [t for t in (self.frontiers.at, self.oracle._now) if t is not None]
        self._push(_Pending(at=max([self.clock.now(), *floors]), request_id=None))

    def _push(self, pending: _Pending) -> None:
        """Put one input on the time-ordered queue and wake the loop; the arrival counter keeps
        inputs stamped alike in raised order."""
        self._queue_seq += 1
        heapq.heappush(self._queue, (pending.at, self._queue_seq, pending))
        self._activity.set()

    def _next_work(self, horizon: datetime, now: datetime) -> _Work:
        """Choose the loop's next act: one decision, five named outcomes (DL-137, DL-150; DL-145
        keeps the branch count).

        Three INDEPENDENT predicates over one observation decide it:

          E  a queued input is takeable: the queue head is at or before the horizon, no later
             than any timer, no later than any tick, and (real domain only) already due. The
             real domain compares the head with the timer's raw due instant (DL-232);
          S  a calendar tick is takeable: admission is not frozen, the tick is at or before the
             horizon, STRICTLY before the queue head, no later than any timer, and (real
             domain) already due;
          T  a timer firing is takeable: something is due, its effective instant is at or
             before the horizon, it is not held lazy by the frontier rule, and (real domain)
             already due. In the real domain a timer due strictly before a due queue head is
             also takeable. It fires as its own input, stamped at the latest of its due
             instant, the last admitted instant and the oracle's clock (ss0, DL-232).

        The priority order is E > S > T:

            E S T | choice
            ------+-----------------------------------
            0 0 0 | QUIESCE or WAIT (see below)
            0 0 1 | TIMER
            0 1 0 | TICK
            0 1 1 | TICK  (T masked by S)
            1 0 0 | EVENT
            1 0 1 | EVENT (T masked by E)
            1 1 0 | impossible
            1 1 1 | impossible

        E and S both require the other's instant to be non-None before comparing, so with both
        true the queue head and the tick both exist, and E asks `head_at <= sched_due` while S
        asks `sched_due < head_at`. A tick and a queued input stamped at the same instant are E's:
        the input feeds first and the tick waits one iteration.

        In the `0 0 0` row the virtual domain is quiescent, since nothing can move without the
        clock. The real domain is quiescent only when no work exists AND none can appear;
        `hold_open` says more can always appear (ss10). Otherwise the loop WAITS on the earliest
        known instant, or on the horizon when a live adapter's completion could still land
        inside it (DL-45), and chooses again. `WAIT` with no instant is the real domain's
        "nothing due at all": it waits on activity alone, PAST the horizon.

        Commit discipline (DL-45): the real domain commits to work only once its instant is due;
        an earlier instant is waited out interruptibly, so a control injection or completion
        arriving mid-wait re-plans instead of feeding behind an already-journaled advance.
        Virtual jumps never yield."""
        head_at = self._queue[0][0] if self._queue else None
        due = [
            t
            for t in (self.oracle.next_timer_due(), self.clock.next_sleeper_due())
            if t is not None
        ]
        raw_due = min(due) if due else None
        eff_due = max(raw_due, now) if raw_due is not None else None
        sched_due = self.scheduler.next_occurrence() if self.scheduler is not None else None
        # the instant a queued input must not pass: the effective due instant in the virtual
        # domain (the frontier rule), the raw one in the real domain (DL-232)
        head_gate = eff_due if self.clock.virtual else raw_due
        if (
            head_at is not None
            and head_at <= horizon
            and (head_gate is None or head_at <= head_gate)
            and (sched_due is None or head_at <= sched_due)
            and (self.clock.virtual or head_at <= now)
        ):
            return _Work(_Do.EVENT)
        if (
            sched_due is not None
            and sched_due <= horizon
            and (head_at is None or sched_due < head_at)
            and (eff_due is None or sched_due <= eff_due)
            and (self.clock.virtual or sched_due <= now)
        ):
            return _Work(_Do.TICK, at=sched_due)
        if (
            not self.clock.virtual
            and raw_due is not None
            and head_at is not None
            and raw_due < head_at
            and head_at <= horizon
            and head_at <= now
        ):
            # concurrency-model ss0 (DL-232): a timer due strictly before a due input fires as its
            # own input, so the revision it moves is one the input's gate reads. It is stamped at
            # the latest of its due instant, the last admitted instant and the oracle's clock
            floors = (self.frontiers.at, self.oracle._now)
            return _Work(_Do.TIMER, at=max([raw_due, *(t for t in floors if t is not None)]))
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
            # a live adapter's completion has no due timestamp and can still land inside the
            # horizon, so with live tasks the loop waits out the horizon (DL-45)
            if not self._live or now >= horizon:
                return _Work(_Do.QUIESCE)
            target = horizon
        return _Work(_Do.WAIT, at=target)

    async def run_until_quiescent(self, horizon: datetime) -> list[Event]:
        """Process every queued event, due oracle timer and adapter completion at or before
        `horizon`; return the oracle events emitted. Work due after the horizon stays pending
        for a later call (rehearse quiescence, ss9). Time only moves forward across calls.

        The frontier rule (bisimulation-pinned): a timer due at or before the already-processed
        instant (the frontier) fires only once the horizon lets time move PAST that instant, and
        then back-dated to its due time via advance(frontier), as oracle-direct feed() would fire
        it on the next event. Zero-delta deadlines (due == now at arming) stay lazy, and
        past-due timers do not trip advance()'s backwards-time check. `_next_work` makes the
        per-iteration choice.

        The zero-delay-cycle guard: a condition cycle over instant completions generates
        unbounded work at one frozen virtual instant (L010's concern at zero duration). The
        engine raises ZeroDelayCycleError after a catalog-scaled event budget at a single
        instant; the budget's floor is `INSTANT_BUDGET_FLOOR`."""
        emitted: list[Event] = []
        instant: datetime | None = None
        instant_events = 0
        instant_budget = max(INSTANT_BUDGET_FLOOR, 100 * len(self.oracle.catalog.jobs))
        while True:
            await self._settle()
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
                # enqueue the tick's STARTJOB(s), stamped at the tick; the next iteration takes them
                # like any external input (feed() fires timers due <= tick first)
                assert work.at is not None and self.scheduler is not None
                await self.clock.wait_until(work.at)
                for tick_ev in self.scheduler.pop_due(work.at):
                    self._enqueue(tick_ev, source="scheduler")
            elif work.do is _Do.TIMER:
                assert work.at is not None
                # a time observation is an input (DL-44): its timer firings survive a crash, or
                # resume replay would resurrect a job the oracle killed. It is admitted like an
                # operator command, as an attempt with no verb (concurrency-model ss4)
                out = await self._admit_and_apply(_Pending(at=work.at, request_id=None))
                emitted.extend(out)
                self._dispatch()
            elif work.do is _Do.QUIESCE:
                # virtual: nothing can move without the clock; real: no work exists and none can
                # appear, or everything left is beyond the horizon (_next_work's `0 0 0` row)
                return emitted
            else:
                # real domain: block until queue activity or the instant _next_work chose; a
                # completed adapter task also fires _activity so _settle re-raises its failure
                self._activity.clear()
                await self.clock.wait_until(
                    work.at if work.at is not None else datetime.max, interrupt=self._activity
                )
                continue  # a pure wait is not same-instant work: skip the budget
            instant_events += 1
            if instant_events > instant_budget:
                assert instant is not None  # set the iteration `now` first moved
                started = sorted(
                    {
                        t.job
                        for t in self.oracle.trace()
                        if t.at == instant and t.transition.endswith("->STARTING")
                    }
                )
                raise ZeroDelayCycleError(
                    f"no virtual-time progress after {instant_events} events at "
                    f"{instant}: zero-delay condition cycle with instant completions? "
                    "(the AutoSys tight-loop pattern, L010; give the loop's jobs a "
                    "nonzero FakeAdapter duration or break the cycle)",
                    instant=instant,
                    jobs=tuple(started),
                )

    def _note_corpse(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._corpses_raised.add(id(task))

    async def _settle(self) -> None:
        """Yield until every live adapter task is done or parked on the clock and every cancelled
        task is reaped.

        Adapters block only via sleep_until, so a live task is either ready (one more yield lets
        it progress) or holds exactly one pending sleeper; live == pending means nothing can move
        without the clock. Reaped tasks that died with anything other than the cancellation
        re-raise here. Real domain (DL-43 item 5): adapters block on real IO, so settling makes
        one reaping pass with no spin, and the loop's activity event wakes it."""
        while True:
            for job, run in list(self._live.items()):
                if run.task.done():
                    # raise FIRST: a failed task stays in `_live`, so a reversible catcher (the
                    # seal's abort path) cannot consume the only observation; the next settle
                    # re-raises the corpse, and the engine never resumes over an applied SPAWN
                    # whose task and completion are both gone
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
            if not self._reaping and len(self._live) == self.clock.pending_sleepers():
                return
            await asyncio.sleep(0)

    async def _admit_and_apply(self, pending: _Pending) -> list[Event]:
        """The frozen admission order for ONE input (concurrency-model ss4); every input goes
        through it.

        Against ss4's numbered steps: framing and `baseline_id` are settled by construction (1);
        the index answers an exact retry before anything is stamped or appended, so a retry costs
        no index and moves no clock (2); the frontier checks the stamp is non-decreasing,
        `_dry_apply` may refuse a control input, `_dry_apply_engine_made` may quarantine an
        engine-made one or stop the engine, and only then does the frontier hand out the next
        index (3); the attempt is one line, so its time observation cannot be torn from its verb
        (4); the batch applies the time half, firing due timers, which puts a term_run_time kill
        AHEAD of the gate that reads the status it kills (5); the gate decides (6); the result is
        recorded with the revisions the input moved (7). Steps 5-7 do not yield."""
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
                # a client error, not an engine one: refuse the request and keep serving
                self._refuse(pending, exc)
                return []
            if prior is not None:
                self.deduped.append((pending.request_id, prior))
                self._answer(pending, prior)
                return []  # a retry advances no logical time (CM-05)
        if envelope is not None and envelope.epoch != self.epoch:
            # ss4 step 2, AFTER the dedup: an exact old-epoch retry recovers its original result
            # (the leader that held that epoch decided it), while an UNSEEN old-epoch request was
            # composed against an old leader and is refused
            self._refuse(
                pending,
                AdmissionRefused(
                    f"epoch {envelope.epoch} is not this leader's {self.epoch}:"
                    " re-read and re-compose against the current leader",
                    code="stale_epoch",
                ),
            )
            return []
        admitted = self.frontiers.admit(pending.at)
        # before the frontier moves: a refusal or a stop takes no index
        attempt = self._attempt(pending, admitted.committed_index, fp)
        quarantine: tuple[Rejection, Exception] | None = None
        if not attempt.engine_made:
            refusal = self._dry_apply(attempt)
            if refusal is not None:
                self._refuse(pending, refusal)
                return []
        else:
            quarantine = self._dry_apply_engine_made(attempt)
        forced = None if quarantine is None else quarantine[0]
        self.frontiers = admitted
        if self.journal is not None:
            self.journal.admit(attempt)  # WAL-append + fsync BEFORE apply (ss7)
        self.decisions.note(attempt)
        await self.clock.wait_until(pending.at)
        applied = apply_attempt(self.oracle, attempt, forced=forced, grace_s=self.cmd_grace_s)
        self.decisions.record(applied.result)
        self.frontiers = self.frontiers.record(attempt.index)
        # step 7 commits the decision and the outbox entries it implies as ONE batch
        # (concurrency-model ss1): an engine that dies between deciding and acting leaves the
        # record that it MEANT to act, or a decided kill vanishes with its task. The batch is
        # one record and one fsync (DL-118, CM-17)
        effects = self._plan_effects(applied, attempt.index)
        if self.journal is not None:
            self.journal.decision(applied.result, effects)
        for effect in effects:
            self.outbox.record(effect)
            if effect.kind == "SPAWN":
                # a run has its one identity from its plan (DL-234): a held or retired SPAWN
                # still counts, so a STARTING overwrite at the same run number plans no second
                # one. Resume rebuilds `_dispatched` from every row's run number (runner_startup,
                # period-model ss3.3), which agrees: a dispatchable row reaches run N through a
                # decision that planned run N's SPAWN, and resume writes that decision for an
                # attempt whose decision never landed before it seeds the map
                # (`commit_recovered`, DL-315)
                self._dispatched[effect.job] = max(
                    self._dispatched.get(effect.job, 0), effect.run_number
                )
        if quarantine is not None:  # loud once the quarantine is durable
            self.quarantined.append((applied.result, quarantine[1]))
            report_quarantine(applied.result)
        elif applied.result.decision == "rejected" and ev is not None:
            assert applied.result.reason is not None
            self.drops.append((ev, applied.result.reason))
        self._answer(pending, applied.result)
        return applied.emitted

    def _dry_apply(self, attempt: Attempt) -> AdmissionRefused | None:
        """Apply a control input on a fork of the oracle; return why it must be refused, or None
        (concurrency-model ss4).

        The fork reaches the result the real apply will: `apply_attempt` reads nothing outside
        the oracle, and steps 5-7 do not yield to another writer. A gate rejection is not a
        refusal; the real apply decides and logs it. Every fault in either half refuses,
        unlogged."""
        dry = trial_apply(self.oracle, attempt, grace_s=self.cmd_grace_s)
        if isinstance(dry, Exception):
            return AdmissionRefused(
                f"refused before admission: applying the input raised {type(dry).__name__}: {dry}",
                code="apply_faulted",
            )
        return None

    def _dry_apply_engine_made(self, attempt: Attempt) -> tuple[Rejection, Exception] | None:
        """Apply an engine-made input on a fork of the oracle; return the rejection that
        quarantines it with the fault it met, or None to apply it as the gate decides
        (concurrency-model ss4).

        A fault does not refuse, since nobody would hear of it. If the time half applies alone on
        a second fork, the input is logged with a rejected `engine_apply_faulted` decision and
        its time half applies. If the time half raises too, the engine stops before the frontier
        moves and before the append, with `TimeHalfStop`.

        The fault is kept without its traceback, whose frames hold the fork."""
        try:
            quarantine = classify_fault(self.oracle, attempt, grace_s=self.cmd_grace_s)
        except TimeHalfFault as stop:
            raise TimeHalfStop(attempt, stop.cause, stop.full) from stop.cause
        if quarantine is None:
            return None
        rejection, fault = quarantine
        return rejection, _without_frames(fault)

    def _plan_effects(self, applied: Applied, index: int) -> list[Effect]:
        """ss4 step 7's other half: what the shell now intends to do about what the oracle decided.

        Two identity binds happen inside the decision transaction (period-model ss2.3): a SPAWN's
        `run_id` is minted with the effect (PR-36a), and `generation` is read from the executor's
        host row (PR-16); the row exists from the genesis seed. A KILL looks its run's id up in
        the outbox, where its SPAWN recorded it."""
        return self._plan(
            applied.emitted,
            index,
            runs={job: rt.run_number for job, rt in self.oracle.store.job.items()},
            dispatched=self._dispatched,
            live={job: run.run_number for job, run in self._live.items()},
        )

    def _plan(
        self,
        emitted: list[Event],
        index: int,
        *,
        runs: Mapping[str, int],
        dispatched: Mapping[str, int],
        live: Mapping[str, int],
    ) -> list[Effect]:
        """`plan_effects` with this engine's executor, generation, dispatch rows, run-id bindings
        and mint."""
        row = self.oracle.store.host(self.executor_id)
        return plan_effects(
            emitted,
            index=index,
            executor_id=self.executor_id,
            generation=row.generation if row is not None else 0,
            runs=runs,
            dispatched=dispatched,
            live=live,
            dispatchable=self._dispatchable(),
            run_ids=self.outbox.spawn_run_ids(),
            mint_run_id=lambda: str(uuid.uuid4()),
        )

    def plan_recovered(self, recovered: RecoveredAttempt) -> list[Effect]:
        """The effects a replay-recovered attempt (DL-156) implies, as its step 7 would have
        planned them (DL-315). Nothing is written here; `commit_recovered` writes them.

        The plan reads the rows as of the attempt: `dispatched` is every run number before it and
        `runs` every run number after it, because the seeded `_dispatched` would plan no SPAWN for
        a recovered start and an empty map would plan one for a STARTING overwrite whose run has
        one. `live` is empty, so the plan holds SPAWNs only, and a SPAWN mints its `run_id`."""
        return self._plan(
            recovered.emitted,
            recovered.result.index,
            runs=recovered.runs_after,
            dispatched=recovered.runs_before,
            live={},
        )

    def commit_recovered(self, recovered: RecoveredAttempt, effects: list[Effect]) -> None:
        """Write the step-7 batch an attempt never got: its decision and the effects
        `plan_recovered` planned, as one record (DL-315).

        Resume calls this for each recovered verdict, after the identity preflight and before
        `_dispatched` is seeded. Dispatch follows the decision record, so a missing record means
        no SPAWN of that attempt was recorded or applied; this record is the attempt's decision
        transaction, and its SPAWN's `run_id` the run's one identity. The decision index already
        holds the verdict."""
        assert self.journal is not None  # resume always holds its root's journal
        self.journal.decision(recovered.result, effects)
        for effect in effects:
            self.outbox.record(effect)
        if recovered.result.code == "engine_apply_faulted":
            report_quarantine(recovered.result)  # replay quarantined it (ss4)

    def _dispatchable(self) -> frozenset[str]:
        """Jobs this engine has a dispatch row for: a catalog entry whose job_type has a
        registered adapter. Boxes and pseudo-entries are never an effect."""
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
        """Steps 1-2's other outcome: answer the refusal and record it on `refused_inputs`, since
        a refusal leaves NOTHING in the log (no index, no record)."""
        self.refused_inputs.append((pending.ev, exc))
        if pending.future is not None and not pending.future.done():
            pending.future.set_exception(exc)

    def _attempt(self, pending: _Pending, index: int, fp: str) -> Attempt:
        ev = pending.ev
        envelope = pending.envelope
        return Attempt(
            index=index,
            at=pending.at,
            request_id=pending.request_id or f"{ENGINE_REQUEST_ID_PREFIX}{index}",
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
        """Drain the outbox: apply every pending effect, in admission order (concurrency-model
        ss5). This is the only place an effect is applied.

        ss5 makes per-run effect ordering mandatory: a KILL decided after a SPAWN that overtook it
        would stop a run that had not started.

        The fence is re-proved HERE as well as in the journal writer (period-model ss1.3): an
        append and a dispatch are two acts, and a leader that lost the lineage between them would
        start a process for an estate it does not lead (PR-03)."""
        pending = self.outbox.pending()
        if pending and self.fence is not None:
            self.fence.check()
        for effect in pending:
            self._apply_effect(effect)

    def _apply_effect(self, effect: Effect) -> None:
        """One effect, at-most-once (concurrency-model ss5, CM-09).

        Three gates come before the attempt. An effect the world has moved past is RETIRED, not
        applied; supersession is checked first (DL-234). A host that routes nothing leaves the
        effect PENDING: held, not failed and not rerouted (ss8). Every input ends in a dispatch
        pass, so a held SPAWN is retired in the pass after the status edge that ended its run.
        The outcome is recorded either way, because "attempted and we cannot say" must survive a
        crash (ss5's third state)."""
        reason = superseded_reason(
            effect,
            self.oracle.store.job.get(effect.job),
            self._live[effect.job].run_number if effect.job in self._live else None,
        )
        if reason is not None:
            self._retire(effect, reason)
            return
        if effect.kind == "SPAWN" and not routes_new_effects(
            self.oracle.store.host(effect.executor_id)
        ):
            # ss8: a drained or quarantined host routes no NEW effect. The effect stays pending,
            # which IS the held set: rerouting without proof the old executor is dead is the
            # double run ss7 prevents, and failing it would turn a maintenance window into an
            # estate-wide incident.
            #
            # SPAWN only. ss8's column is about NEW work: `passive` lets running work complete,
            # and a kill is how running work ends, so a drain never holds a kill.
            return
        if effect.kind == "SPAWN":
            self._apply_spawn(effect)
        else:
            self._apply_kill(effect)

    def _retire(self, effect: Effect, detail: str) -> None:
        """Record an effect as retired; its run already counts as dispatched from the plan
        (DL-234)."""
        self._resolve_effect(
            EffectOutcome(effect_id=effect.effect_id, state="retired", detail=detail)
        )

    def _resolve_effect(self, outcome: EffectOutcome) -> None:
        self.outbox.resolve(outcome)
        if self.journal is not None:
            self.journal.effect_result(outcome)

    def _apply_spawn(self, effect: Effect) -> None:
        job_ir = self.oracle.catalog.jobs.get(effect.job)
        adapter = self.adapters.get(job_ir.job_type) if job_ir is not None else None
        if job_ir is None or adapter is None:  # pragma: no cover -- unreachable via planning
            # `_dispatchable` filters both, and a log's effects name only jobs its own catalog
            # had (the ss7 hash gate refuses a resume against a changed estate). The branch
            # guards a DISAGREEMENT between catalog and log, which would otherwise be a
            # KeyError in the middle of dispatch.
            self._retire(effect, f"{effect.job} has no dispatch row in this catalog")
            return
        self._dispatched[effect.job] = effect.run_number
        stale = self._live.pop(effect.job, None)
        if stale is not None:
            # One live attempt per job. An injected INACTIVE (DL-235) leaves the earlier run live
            # in the shell while the oracle's row is not STARTING/RUNNING/QUE_WAIT, so
            # the oracle's DL-81 refusal does not block a restart: the job goes through
            # STARTJOB again and this SPAWN is for the new run. The earlier task is cancelled
            # through the adapter's own ladder, and the second SPAWN records the supersession;
            # the vendor would run both.
            stale.task.cancel()
            self._reaping.append(stale.task)
        self._launch(job_ir, effect.run_number, adapter, run_id=effect.run_id)
        self._resolve_effect(EffectOutcome(effect_id=effect.effect_id, state="applied"))

    def _apply_kill(self, effect: Effect) -> None:
        """The oracle decided terminal; the shell stops the run.

        Cancelling the adapter task IS the effect at this tier; the adapter's TERM/grace/KILL
        ladder runs on the way out. `applied` means the cancellation was delivered to a live
        run; the wrapper records what became of the process."""
        live = self._live.pop(effect.job, None)
        if live is None:  # pragma: no cover -- unreachable via planning
            # `plan_effects` plans no KILL for a job with no live run (a test pins it), and
            # resume re-drives recorded kills through `_redrive_recorded_kills`, which resolves
            # every pending KILL before the barrier's dispatch reaches this
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
        """Create the adapter task, from `_apply_spawn` (an outbox effect) and from resume's FW
        re-dispatch and detached reattach, which go through no effect: one is an idempotent
        re-read and the other a run that never stopped. `run_id` is the effect's on the
        `_apply_spawn` path; None marks those two callers and a re-driven SPAWN the outbox holds
        no binding for."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._run_adapter(job_ir, run_number, adapter, run_id))
        task.add_done_callback(lambda _t: self._activity.set())
        self._live[job_ir.name] = _LiveRun(run_number=run_number, task=task)

    async def shutdown(self) -> None:
        """Cancel every live adapter task and collect the cancellations, re-raising anything a
        task died with OTHER than the cancellation. This is the orderly harness and rehearse
        teardown; the wrapper records the outcome of a tethered kill (ss6a).

        Anyone parked on a decision is told first (S3) that their input never reached admission,
        so nothing in the log refers to it."""
        for _, _, pending in self._queue:
            if pending.future is not None and not pending.future.done():
                pending.future.set_exception(
                    AdmissionRefused(
                        "engine shut down before this input was admitted",
                        code="engine_shutting_down",
                    )
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
            run_id=run_id,
            fence=self.fence,
            route=self.host_state,
        )
        result = await adapter.run(job_ir, run_number, ctx)
        # (job, run_number) ride along for the ss4 stale-completion gate
        payload: dict[str, object] = {"job": job_ir.name, "run_number": run_number}
        payload |= status_payload(result, where=f"adapter for {job_ir.name!r}")
        # source="adapter" makes this a COMPLETION, subject to the ss4 stale gate
        # (runner_admission)
        self._enqueue(Event(at=self.clock.now(), kind="STATUS", payload=payload))
