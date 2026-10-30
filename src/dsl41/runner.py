"""Runner engine: the sans-IO shell over the oracle.

Normative spec: docs/runner-design.md (DL-41, DL-41a). The Oracle is the single semantics
authority; the engine contributes dispatch (adapters), time (the ss9 clock domains),
durability (WAL), the calendar scheduler and the control surface. The bisimulation gate
(ss13, ss14) runs every SEM trace test through both Oracle-direct and Engine(VirtualClock,
FakeAdapter) with identical traces.

The clocks (runner_clock), adapters (runner_adapters), WAL (runner_journal), calendar scheduler
(runner_scheduler; its timezone ladder lives in `timezones`, DL-163), preflight
(runner_preflight), the ss10 control plane (runner_control, DL-78) and possession of a run root
(genesis and resume: runner_startup, DL-106) each own their file and their part of this
contract (DL-74). This
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

Journal before effect (ss7, DL-44): an input's decision record is durable before anything it
implies is dispatched, and a start's `dispatch` record, naming the run's `run_id`, is durable
before its adapter task exists. A kill is not journaled: the oracle's terminal status is the
record, and a tethered wrapper records how its command died (ss6a).

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
Engine.submit. They are journaled by the queued-input path (`_Do.EVENT`), and the single-writer
loop serializes them, so that tier carries no controller lease (DL-41a). Query handlers read the
oracle store between feeds, which is safe because feed() never yields.
"""

from __future__ import annotations

import asyncio
import heapq
import uuid

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Final

from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventSource, JobRuntime
from dsl41.runner_adapters import (
    AdapterContext,
    Failed,
    FileWatcherAdapter,
    JobAdapter,
    status_payload,
)
from dsl41.runner_admission import (
    ENGINE_REQUEST_ID_PREFIX,
    AdmissionRefused,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    Frontiers,
    RequestCollision,
    TimeHalfFault,
    TimeHalfStop,
    apply_attempt,
    classify_fault,
    dispatch_plan,
    fingerprint,
    report_quarantine,
    spawn_superseded,
    trial_apply,
)
from dsl41.canon import CanonError, canonical_bytes
from dsl41.runner_codes import Rejection
from dsl41.runner_clock import Clock, EngineError, ZeroDelayCycleError
from dsl41.idempotence import invocation_env
from dsl41.runner_config import (
    EngineSettings,
    OracleReading,
    RuntimeProfile,
    oracle_reading,
    switches_of,
)
from dsl41.runner_journal import (
    Journal,
    dispatch_record,
    encode_snapshot,
    follow_up_done,
    pending_record,
)
from dsl41.runner_history import run_history
from dsl41.runner_ledger import STATE_MACHINE_VERSION, Proof
from dsl41.runner_store import Record, Store
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
    #: the attempt's id, the `run_id` its `dispatch` record bound; None with no journal
    attempt: str | None = None


@dataclass(frozen=True)
class Deferred:
    """A dispatch startup owes and the loop carries out once the due work is processed: a
    pending start (`pending` is its `dispatch_pending` record's seq) or a plan's replacement
    attempt (`plan` is the interrupted attempt's id, `step` the step's position, `attempt` the
    replacement's id). Each is durable before it is deferred, so a crash before its `dispatch`
    loses none (spec-wiring 1b)."""

    job: str
    run_number: int
    pending: int | None = None
    plan: str | None = None
    step: int = 0
    attempt: str | None = None
    ordinal: int = 1
    env: dict[str, str] = field(default_factory=dict)


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

    `ev` absent is a standalone time observation. ss4 admits it by the same rule.

    `source` is read only when `ev` is absent; an event carries its own (DL-68).

    `step` names the follow-up step this input performs: the plan's attempt id and the step's
    position. Its done mark is appended with the admission (spec-wiring 1b)."""

    at: datetime
    request_id: str | None
    ev: Event | None = None
    source: EventSource | None = None
    envelope: Envelope | None = None
    future: asyncio.Future[ApplyResult] | None = None
    step: tuple[str, int] | None = None


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
    profile: RuntimeProfile | None,
    semantics: SemanticSwitches | None,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
) -> SemanticSwitches:
    """The semantic switches an engine runs (DL-252).

    An engine over a run root runs its configuration's profile. `semantics` is for an engine
    with no profile; one that disagrees with the profile is refused. So is a scheduler built under another
    value of a switch it reads (DL-259, DL-260), since its calendars or start instants would
    fire under a reading the period and oracle do not use; the derived runtime profile reads
    those switches from the scheduler, so a launcher meets this before any durable write and this
    check is the backstop. The same holds for a wired adapter built under another value of a
    switch it reads (DL-258, DL-280), once a pin or `semantics` names a reading."""
    if profile is None:
        switches = semantics or DEFAULT_SWITCHES
    else:
        switches = switches_of(profile)
        if semantics is not None and semantics != switches:
            raise EngineError(
                f"semantic switches {semantics} disagree with the profile's {switches}"
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
    if profile is not None or semantics is not None:
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
    profile: RuntimeProfile | None,
    semantics: SemanticSwitches | None,
    scheduler: Scheduler | None,
    adapters: Mapping[str, JobAdapter],
) -> Oracle:
    """The oracle an `Engine` reads its catalog through."""
    # SEM-35: the base zone and alias table the oracle reads a job's time attributes in. They
    # come from the configuration's profile, which every replay reads (`oracle_reading`), else
    # from the scheduler: both halves of one engine read a job's zone the same way (DL-62,
    # DL-253). Empty aliases read as absent.
    switches = _engine_switches(profile, semantics, scheduler, adapters)
    if profile is not None:
        # the zone and aliases from the profile; the switches as checked above
        reading = OracleReading(**{**oracle_reading(profile), "semantics": switches})
    else:
        reading = OracleReading(
            default_tz=scheduler.default_tz if scheduler is not None else None,
            tz_aliases=alias_table(scheduler.tz_aliases if scheduler is not None else None),
            semantics=switches,
        )
    return Oracle(catalog, **reading)


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
        profile: RuntimeProfile | None = None,
        fence: Proof | None = None,
        semantics: SemanticSwitches | None = None,
        machines: Executor | None = None,
        settings: EngineSettings | None = None,
    ) -> None:
        self.oracle: Oracle = _engine_oracle(catalog, profile, semantics, scheduler, adapters)
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
        #: (DL-334), and `SUBSCRIPTION`, one per subscribe feed, started by the control server
        #: through `machines`. An engine given no executor makes its own
        self._machines = machines if machines is not None else Executor()
        #: the decision index, built here because it runs each request id in flight on this
        #: engine's executor
        self.decisions = DecisionIndex(self._machines)
        #: the configuration's runtime profile, the one the oracle reads under. None on an
        #: engine with no configuration (the bisimulation harness, a bare rehearsal)
        self.profile = profile
        #: this run root's `leader.lock` proof. The journal re-checks it before every append, and
        #: the FW adapter before every spool line it writes (`AdapterContext.fence`).
        self.fence: Proof | None = fence
        #: `engine.toml` (DL-424); None takes no snapshot and prunes nothing
        self.settings = settings
        #: the configuration rev the engine runs
        self.config_rev = 0
        #: the clock instant and journal seq of the last snapshot this engine took
        self._snapshot_at: datetime | None = None
        self._snapshot_seq = 0
        #: the run history the snapshot this engine restarted from carried; each snapshot
        #: continues it with the trace since (`runner_history.run_history`)
        self.restored_runs: dict[str, list[dict[str, Any]]] = {}
        #: the job rows that snapshot restored: they number a job's next run when retention
        #: emptied its history
        self.restored_rows: dict[str, JobRuntime] = {}
        #: True from the oracle's apply of an attempt until its records are appended and every
        #: start it decided has its `dispatch`: the store then lacks what the oracle holds, and
        #: no snapshot may be taken. A dispatch that raises leaves it True
        self._applying = False
        #: startup's step 8 (`runner_startup`), which the loop runs once steps 6 and 7 have taken
        #: effect; None on an engine that is not recovering. While it is set, a start an input
        #: decides is owed, not launched: the recovery barrier holds it until the due work has
        #: run (DL-425)
        self.recovery: Callable[[], None] | None = None
        #: set once the engine has recorded `engine started`: at genesis, or by the loop at the
        #: end of a restart's recovery
        self.started = asyncio.Event()

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
        #: the dispatches startup owes (pending starts, plan replacements), and the journal's
        #: last instant. The loop carries them out once it has processed what is due at or
        #: before that instant or the clock, so the reconciliation inputs and the overdue timers
        #: apply first and a start they end is not launched (DL-102, DL-425)
        self._deferred: list[Deferred] = []
        self._deferred_after: datetime | None = None
        #: killed tasks by id: their attempt is closed with a `stopped` outcome once reaped
        self._killed: dict[int, _LiveRun] = {}

    @property
    def machines(self) -> Executor:
        """This engine's one statechart executor (statechart-engine ss11)."""
        return self._machines

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

    def _enqueue(
        self,
        ev: Event,
        *,
        source: EventSource | None = "adapter",
        request_id: str | None = None,
        envelope: Envelope | None = None,
        future: asyncio.Future[ApplyResult] | None = None,
        step: tuple[str, int] | None = None,
    ) -> None:
        ev.source = source  # DL-68: the event carries its own provenance
        self._push(
            _Pending(
                at=ev.at,
                request_id=request_id,
                ev=ev,
                envelope=envelope,
                future=future,
                step=step,
            )
        )

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
            if self.recovery is not None and not self._takes_due_work(
                work, max(now, self._deferred_after or now)
            ):
                # the dispatches startup owes go once this pass has processed every input, timer
                # and tick due at or before the current instant, so the reconciliation and the
                # overdue deadlines apply first. A due item the frontier rule holds lazy is not
                # processed now and does not hold them back. On the real clock a steady stream
                # of due inputs delays them; it never loses one
                self._finish_recovery()
                continue  # dispatch is not same-instant work: skip the budget
            if work.do is _Do.EVENT:
                _, _, pending = heapq.heappop(self._queue)
                out = await self._admit_and_apply(pending)
                emitted.extend(out)
                self._maybe_snapshot()
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
                self._maybe_snapshot()
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
                    # raise FIRST: a failed task stays in `_live`, so a reversible catcher cannot
                    # consume the only observation; the next settle re-raises the corpse, and the
                    # engine never resumes over a dispatched run whose task and completion are
                    # both gone
                    self._note_corpse(run.task)
                    _raise_if_failed(run.task)
                    del self._live[job]
            still_reaping: list[asyncio.Task[None]] = []
            for task in self._reaping:
                if task.done():
                    self._note_corpse(task)
                    _raise_if_failed(task)
                    self._close_killed(task)
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
        index (3); the attempt is one record, so its time observation cannot be torn from its
        verb (4); the batch applies the time half, firing due timers, which puts a term_run_time
        kill AHEAD of the gate that reads the status it kills (5); the gate decides (6); the
        attempt and its result, with the revisions the input moved, are appended in one store
        transaction (4, 7), and only then is what it implies dispatched. Steps 5-7 do not yield.
        A plan step's done mark rides in the same transaction (spec-wiring 1b)."""
        ev = pending.ev
        envelope = pending.envelope
        fp = fingerprint(
            baseline_id=self.baseline_id,
            kind=ev.kind if ev is not None else None,
            payload=dict(ev.payload) if ev is not None else {},
            source=ev.source if ev is not None else pending.source,
            expect=envelope.expect if envelope is not None else None,
            claimed_actor=envelope.claimed_actor if envelope is not None else None,
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
        await self.clock.wait_until(pending.at)
        if self.journal is not None:
            self.journal.prove()  # an engine that lost the root applies nothing (S6b)
        # nothing moves before the wait: a cancel there leaves the engine as the store has it
        self.frontiers = admitted
        self.decisions.note(attempt)
        self._applying = True
        applied = apply_attempt(self.oracle, attempt, forced=forced)
        # the index takes the decision before the store does, so a statechart error stops the
        # engine before any durable write of it (statechart-engine ss5)
        self.decisions.record(applied.result)
        if self.journal is not None:
            # the attempt and its decision are one transaction, durable before anything the
            # decision implies is dispatched or answered (ss7)
            also: list[Record] = []
            if pending.step is not None:
                plan, position = pending.step
                also.append(follow_up_done(plan, position, self.journal.next_seq(), pending.at))
            self.journal.admission(attempt, applied.result, *also)
        self.frontiers = self.frontiers.record(attempt.index)
        if quarantine is not None:  # loud once the quarantine is durable
            self.quarantined.append((applied.result, quarantine[1]))
            report_quarantine(applied.result)
        elif applied.result.decision == "rejected" and ev is not None:
            assert applied.result.reason is not None
            self.drops.append((ev, applied.result.reason))
        # the answer follows the durable decision and does not wait on its dispatch: a dispatch
        # that fails stops the engine with its own error, and never leaves the request unanswered
        self._answer(pending, applied.result)
        self._dispatch(applied.emitted)
        # every start the input decided has its `dispatch` or its `dispatch_pending` now
        self._applying = False
        return applied.emitted

    def _dry_apply(self, attempt: Attempt) -> AdmissionRefused | None:
        """Apply a control input on a fork of the oracle; return why it must be refused, or None
        (concurrency-model ss4).

        The fork reaches the result the real apply will: `apply_attempt` reads nothing outside
        the oracle, and steps 5-7 do not yield to another writer. A gate rejection is not a
        refusal; the real apply decides and logs it. Every fault in either half refuses,
        unlogged, and so does a payload the store cannot hold (ss3.2: no float, no lone
        surrogate)."""
        try:
            canonical_bytes(attempt.payload)
        except CanonError as exc:
            return AdmissionRefused(
                f"refused before admission: the payload cannot be recorded: {exc}",
                code="invalid_argument",
            )
        dry = trial_apply(self.oracle, attempt)
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
            quarantine = classify_fault(self.oracle, attempt)
        except TimeHalfFault as stop:
            raise TimeHalfStop(attempt, stop.cause, stop.full) from stop.cause
        if quarantine is None:
            return None
        rejection, fault = quarantine
        return rejection, _without_frames(fault)

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
            source=ev.source if ev is not None else pending.source,
            expect=dict(envelope.expect) if envelope is not None else None,
            claimed_actor=envelope.claimed_actor if envelope is not None else None,
        )

    def _dispatch(self, emitted: list[Event]) -> None:
        """Carry out what one decided input implies, in the order its events were emitted
        (`dispatch_plan`, ss4). This is the only place a decided start or kill is acted on.

        A planned start counts as dispatched for the ghost-run gate whether or not it launches
        (DL-234), so a STARTING overwrite at the same run number plans no second one. A kill
        cancels the live task of the run it names, and nothing if that run is gone."""
        plan = dispatch_plan(
            emitted,
            runs={job: rt.run_number for job, rt in self.oracle.store.job.items()},
            dispatched=self._dispatched,
            live={job: run.run_number for job, run in self._live.items()},
            dispatchable=self._dispatchable(),
        )
        for kind, job, run_number in plan:
            if kind == "SPAWN":
                self._dispatched[job] = max(self._dispatched.get(job, 0), run_number)
                if self.recovery is not None:
                    self._owe(job, run_number)
                else:
                    self.spawn(job, run_number)
                continue
            live = self._live.get(job)
            if live is not None and live.run_number == run_number:
                del self._live[job]
                self._kill(live)

    def _kill(self, live: _LiveRun) -> None:
        """Cancel a live attempt through its adapter's ladder. Once the task is reaped its
        attempt is closed with a `stopped` outcome and no plan: the oracle decided the run's end
        (spec-wiring 2b's interruption rule, step 3)."""
        live.task.cancel()
        self._reaping.append(live.task)
        self._killed[id(live.task)] = live

    def _close_killed(self, task: asyncio.Task[None]) -> None:
        live = self._killed.pop(id(task), None)
        if live is None or live.attempt is None or self.journal is None:
            return
        if live.attempt not in self.journal.open.attempts:
            return  # the adapter recorded its own outcome before the cancel landed
        job = self.journal.open.attempts[live.attempt].job
        at = self.clock.now()
        self.journal.append(
            outcome_record(live.attempt, job, live.run_number, "stopped", {}, at, cause="killed"),
            plan_record(live.attempt, job, live.run_number, "none", [], at),
        )

    def _owe(self, job: str, run_number: int) -> None:
        """A start decided while the engine recovers (a dependent of a recovered completion):
        durable at once as `dispatch_pending`, and launched only once the due work has run, so a
        deadline already past ends it first (DL-425)."""
        assert self.journal is not None  # only a restart recovers, and it holds the store
        at = self.clock.now()
        row = self.oracle.store.job.get(job)
        scheduled = row.status_at if row is not None and row.status_at is not None else at
        seq = self.journal.append(pending_record(job, run_number, scheduled, at))
        self.defer(Deferred(job, run_number, pending=seq), after=self._deferred_after or at)

    def _finish_recovery(self) -> None:
        """Carry out the dispatches startup owes, each removed only once its `dispatch` (or its
        discharge) has committed, so one that raises leaves itself and the rest owed; then run
        step 8 (`recovery`) and mark the engine started."""
        while self._deferred:
            self._discharge(self._deferred[0])
            self._deferred.pop(0)
        recovery, self.recovery = self.recovery, None
        assert recovery is not None  # the loop finishes only a recovery in progress
        recovery()
        self.started.set()

    def defer(self, item: Deferred, *, after: datetime) -> None:
        """Queue a dispatch a recovering engine owes for the loop (`Deferred`). `after` is the journal's
        last instant: the loop carries it out once it has processed what is due at or before the
        later of that instant and the clock, so the restart's reconciliation and every overdue
        timer apply first."""
        self._deferred.append(item)
        self._deferred_after = max(after, self._deferred_after or after)

    def _discharge(self, item: Deferred) -> None:
        """Carry out one owed dispatch, or record why it is not carried out: a run the oracle
        has moved past is not launched (DL-232), and its pending record or plan step is closed
        with no launch (spec-wiring 1b, startup steps 6 and 7)."""
        assert self.journal is not None  # only startup defers, and startup holds the store
        superseded = spawn_superseded(self.oracle.store.job.get(item.job), item.run_number)
        at = self.clock.now()
        if item.pending is not None:
            if superseded is not None:
                self.journal.append(
                    Record(
                        at,
                        "dispatch_pending",
                        None,
                        None,
                        {"event": "discharged", "pending": item.pending, "dispatch": None},
                    )
                )
                return
            self._dispatched[item.job] = max(self._dispatched.get(item.job, 0), item.run_number)
            self.spawn(item.job, item.run_number, discharges=item.pending)
            return
        assert item.plan is not None and item.attempt is not None  # `Deferred`'s two kinds
        if superseded is not None or item.job not in self._dispatchable():
            self.journal.append(follow_up_done(item.plan, item.step, None, at))
            return
        self.spawn(
            item.job,
            item.run_number,
            attempt=item.attempt,
            ordinal=item.ordinal,
            env=item.env,
            step=(item.plan, item.step),
        )

    def _takes_due_work(self, work: _Work, instant: datetime) -> bool:
        """Whether the loop's chosen act processes an input, a timer or a tick due at or before
        `instant`."""
        if work.do is _Do.EVENT:
            return self._queue[0][0] <= instant
        if work.do in (_Do.TICK, _Do.TIMER):
            assert work.at is not None
            return work.at <= instant
        return False

    def spool_of(self, job_ir: JobIR, run_number: int, ordinal: int) -> Path | None:
        """Where an attempt's spool lives: a command attempt has its own directory under its
        run's, so a replacement never meets the first one's exclusive `mkdir`; a file watch keeps
        its run directory and `watch.jsonl` as its identity (spec-wiring 2b)."""
        if self.run_root is None:
            return None
        run_dir = self.run_root / "runs" / f"{job_ir.name}.{run_number}"
        return run_dir if job_ir.job_type == "FW" else run_dir / str(ordinal)

    def spawn(
        self,
        job: str,
        run_number: int,
        *,
        attempt: str | None = None,
        ordinal: int = 1,
        env: Mapping[str, str] | None = None,
        discharges: int | None = None,
        step: tuple[str, int] | None = None,
    ) -> bool:
        """Start run `run_number` of `job` unless the oracle has moved past it; True if it was
        started. The `dispatch` record, with the attempt's freshly minted id, is durable before
        the adapter task exists (journal before effect, ss7; DL-118). `attempt`, `ordinal` and
        `env` name a replacement attempt a plan chose; `discharges` a pending start this
        dispatch ends; `step` the plan step whose done mark rides with the record."""
        if spawn_superseded(self.oracle.store.job.get(job), run_number) is not None:
            return False  # the batch ended the run, or another start overtook it (DL-232)
        job_ir = self.oracle.catalog.jobs[job]  # `_dispatchable` holds catalog jobs only
        adapter = self.adapters[job_ir.job_type]
        stale = self._live.pop(job, None)
        if stale is not None:
            # One live attempt per job. An injected INACTIVE (DL-235) leaves the earlier run live
            # in the shell while the oracle's row is not STARTING/RUNNING/QUE_WAIT, so the
            # oracle's DL-81 refusal does not block a restart: the job goes through STARTJOB
            # again and this start is for the new run. The earlier task is cancelled through the
            # adapter's own ladder; the vendor would run both.
            self._kill(stale)
        run_id = attempt or str(uuid.uuid4())
        spool = self.spool_of(job_ir, run_number, ordinal)
        now = self.clock.now()
        row = self.oracle.store.job.get(job)
        scheduled = row.status_at if row is not None and row.status_at is not None else now
        # the invocation the process is given is the one the record holds (crash spec)
        invocation = dict(env) if env else invocation_env(job, run_number, scheduled, run_id)
        if self.journal is not None:
            records = [
                dispatch_record(
                    job,
                    run_number,
                    attempt=run_id,
                    spool=None if spool is None else str(spool),
                    started_at=now,
                    ordinal=ordinal,
                    env=invocation,
                    discharges=discharges,
                )
            ]
            if step is not None:
                records.append(follow_up_done(step[0], step[1], self.journal.next_seq(), now))
            self.journal.append(*records)
        self._launch(job_ir, run_number, adapter, run_id=run_id, spool=spool, env=invocation)
        return True

    def _launch(
        self,
        job_ir: JobIR,
        run_number: int,
        adapter: JobAdapter,
        *,
        run_id: str | None = None,
        spool: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Create the adapter task, from `spawn` and from a restart's FW re-dispatch, an
        idempotent re-read under the run's recorded `run_id`. None lets the adapter mint its
        own. `env` is the invocation the process is given."""
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._run_adapter(job_ir, run_number, adapter, run_id, spool, dict(env or {}))
        )
        task.add_done_callback(lambda _t: self._activity.set())
        self._live[job_ir.name] = _LiveRun(run_number=run_number, task=task, attempt=run_id)

    async def shutdown(self) -> None:
        """Cancel every live adapter task and collect the cancellations, re-raising anything a
        task died with OTHER than the cancellation. This is the orderly harness and rehearse
        teardown; the wrapper records the outcome of a tethered kill (ss6a), and the next start
        settles the attempt from that record.

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
        self._killed.clear()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results, strict=True):
            if id(task) in self._corpses_raised:
                continue  # _settle already raised this failure to a caller
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def _run_adapter(
        self,
        job_ir: JobIR,
        run_number: int,
        adapter: JobAdapter,
        run_id: str | None = None,
        spool: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        ctx = AdapterContext(
            clock=self.clock,
            run_root=self.run_root,
            run_id=run_id,
            fence=self.fence,
            spool=spool,
            env=env or {},
        )
        result = await adapter.run(job_ir, run_number, ctx)
        # (job, run_number) ride along for the ss4 stale-completion gate
        payload: dict[str, object] = {"job": job_ir.name, "run_number": run_number}
        payload |= status_payload(result, where=f"adapter for {job_ir.name!r}")
        at = self.clock.now()
        step: tuple[str, int] | None = None
        if self.journal is not None and run_id is not None:
            # the attempt's end and its plan, one transaction: the completion is the plan's one
            # step, marked done with the admission that feeds it (spec-wiring 1b)
            self.journal.append(
                outcome_record(run_id, job_ir.name, run_number, outcome_kind(result), payload, at),
                plan_record(run_id, job_ir.name, run_number, "complete", [_feed(payload)], at),
            )
            step = (run_id, 0)
        # source="adapter" makes this a COMPLETION, subject to the ss4 stale gate
        # (runner_admission)
        self._enqueue(Event(at=at, kind="STATUS", payload=payload), step=step)

    # ---- snapshots (spec-wiring 1b)

    @property
    def at_applied_boundary(self) -> bool:
        """Whether a snapshot may be taken now: every applied input is recorded with the
        `dispatch` of each start it decided, and the engine is not recovering (no snapshot
        during startup, spec-wiring 1b)."""
        return not self._applying and self.recovery is None and not self._deferred

    def _maybe_snapshot(self) -> None:
        """Take a snapshot when `snapshot_interval_s` has passed since the last one and the
        journal moved, once the dispatches startup owes are done. Called after a batch has been
        applied and dispatched: a fully applied boundary."""
        if self.settings is None or self.journal is None or not self.at_applied_boundary:
            return
        now = self.clock.now()
        if self._snapshot_at is None:
            self._snapshot_at = now
            return
        interval = timedelta(seconds=self.settings.snapshot_interval_s)
        if now - self._snapshot_at >= interval and self.journal.last_seq > self._snapshot_seq:
            self.take_snapshot()

    def take_snapshot(self) -> int:
        """Snapshot the engine at the journal's last seq, then prune behind it (spec-wiring 1b):
        below `min(snapshot seq, the oldest seq the open table refers to)` and older than
        `retention_days`. Answers the snapshot's seq."""
        assert self.journal is not None
        journal = self.journal
        journal.prove()  # an engine that lost the root writes no snapshot (S6b)
        seq = journal.last_seq
        now = self.clock.now()
        retention = None if self.settings is None else self.settings.retention_days
        data = encode_snapshot(
            self.oracle,
            frontiers=self.frontiers,
            position=journal.position,
            config_rev=self.config_rev,
            open_table=journal.open,
            runs=run_history(
                self.restored_runs,
                self.oracle.trace(),
                None if retention is None else now - timedelta(days=retention),
                self.restored_rows,
            ),
        )
        journal.store.put_snapshot(seq, STATE_MACHINE_VERSION, data)
        journal.engine("snapshot", now, seq=seq)
        self._snapshot_at, self._snapshot_seq = now, seq
        if self.settings is not None:
            floor = min(seq, journal.open.floor() or seq)
            cutoff = now - timedelta(days=self.settings.retention_days)
            if journal.store.prune_before(floor, cutoff):
                # a pruned request id is free again: retention is the one authority for retry
                # and reuse, before and after a restart (spec-journal)
                retained = _first_decision_index(journal.store, self.frontiers.committed_index + 1)
                self.decisions.evict_below(retained)
        return seq


def _first_decision_index(store: Store, otherwise: int) -> int:
    """The index of the oldest decision the store retains, or `otherwise` when it retains none:
    every decision below it was pruned."""
    for _, record in store.read_from(0):
        if record.kind == "decision":
            return int(record.payload["index"])
    return otherwise


def outcome_kind(result: object) -> str:
    """How the live adapter's result says its attempt ended: an exit code or an observed signal
    is `natural`; a launch that never happened is `spawn_failed`; a wrapper that left no record
    is `unobservable`. The plan is `complete` for each: the live path feeds the adapter's result
    as the oracle mapping names it (spec-wiring 1b)."""
    if isinstance(result, Failed):
        return "spawn_failed" if "spawn failed" in result.cause else "unobservable"
    return "natural"


def _feed(payload: Mapping[str, object]) -> dict[str, Any]:
    """A plan step that feeds the completion STATUS `payload` carries."""
    return {"step": "feed", "payload": dict(payload)}


def outcome_record(
    attempt: str,
    job: str,
    run_number: int,
    kind: str,
    payload: Mapping[str, object],
    at: datetime,
    *,
    cause: str | None = None,
) -> Record:
    """One attempt's `outcome` record."""
    body: dict[str, Any] = {
        "attempt": attempt,
        "job": job,
        "run_number": run_number,
        "outcome": kind,
        "status": {k: v for k, v in payload.items() if k not in ("job", "run_number")},
        "at": at.isoformat(),
    }
    if cause is not None:
        body["cause"] = cause
    return Record(at, "outcome", None, None, body)


def plan_record(
    attempt: str,
    job: str,
    run_number: int,
    plan: str,
    steps: list[dict[str, Any]],
    at: datetime,
    **extra: Any,
) -> Record:
    """One outcome's `follow_up` plan."""
    return Record(
        at,
        "follow_up",
        None,
        None,
        {
            "event": "plan",
            "attempt": attempt,
            "job": job,
            "run_number": run_number,
            "plan": plan,
            "steps": steps,
            **extra,
        },
    )
