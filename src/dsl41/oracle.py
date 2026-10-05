"""AutoSys semantics oracle: deterministic discrete-event interpreter.

Phase 7 of the implementation order (CLAUDE.md / DL-03). Normative spec:
docs/ir-design.md ss7 (interface, determinism, non-goals) and every SEM entry
in docs/autosys-semantics.md -- each maps to a trace test (dossier ss8).

Execution model (dossier ss0): jobs are state machines; the event processor
reacts to events and re-evaluates the starting conditions of potentially
affected jobs. A job starts when date/time gates, `condition`, box-RUNNING,
and not-held/not-iced all hold simultaneously.

Interpreter decisions (each with a trace test; PENDING items keep switches):
- Job completion is SCRIPT-DRIVEN: the oracle never invents run durations.
  A CMD/FW job completes when the script injects STATUS (explicit status or
  exit_code, SEM-09 boundary applied) or KILLJOB. The oracle itself only
  emits derived transitions: STARTING/RUNNING on start, bypass-SUCCESS for
  ON_NOEXEC (SEM-22), TERMINATED for terminator cascades (SEM-14), and box
  folds (SEM-11/12).
- One feed(event) drains a same-timestamp FIFO cascade queue: the injected
  event, then consequences in deterministic order (jobs in catalog order,
  insertion sequence as the tie-break; ir-design ss7's "(event kind
  priority, insertion order)" holds degenerately -- the cascade is never a
  mixed-kind queue, so no kind-priority divergence is constructible). Timer
  events the oracle schedules for the future (run_window reschedules, SLA
  deadlines) fire inside the next feed() whose `at` reaches them -- feed
  times must be non-decreasing. Phase 11 (runner-design ss3) adds the only
  two shell-facing extensions: next_timer_due() peeks the heap so a
  wall-clock shell knows when to wake, and advance(now) fires due timers
  with no external event; bisimulation pins feed-only == advance+feed.
- Re-evaluation is EDGE-TRIGGERED (DL-13): a transition, SET_GLOBAL, or
  ON_ICE wakes exactly the jobs whose `condition` references the changed
  entity, so completed consumers re-run on each fresh satisfaction and a
  self-referencing condition may re-trigger its own job (AutoSys's own
  tight-loop pattern; L010's concern, not the oracle's to prevent).
- Scheduling is script-driven too: the oracle owns no calendar. The script
  injects STARTJOB where AutoSys's scheduler would fire (start_times /
  start_mins ticks); a date_conditions job -- standalone or box member (the
  SEM-31/L013 double gate) -- normally starts only on its tick. A scheduled
  tick blocked at a RELEASABLE gate -- `condition` false, or ON_HOLD -- ARMS
  the job (SEM-32 arm-and-wait, Q3 RESOLVED by citation, DL-58): condition
  edges and OFF_HOLD may then start it through the schedule gate, any start
  consumes the arm (at most one start per tick), and an unconsumed arm never
  expires ("no set limit to how long [it] would wait ... regardless of how
  far in the future"; reset only by a start or definition change). Both the
  mechanism ("the STARTJOB event being processed satisfies the start_times/
  run_calendar dependency"; a start "resets" it) and the no-expiry boundary
  are confirmed by Broadcom/CA support with reproduced tests. Ticks
  blocked at ON_ICE (SEM-20: conditions must REOCCUR), at box-not-RUNNING
  (member ticks only count while the box runs -- pinned), or on an
  already-live job do NOT arm. run_window gating applies at the moment a
  start goes through, armed or not. PENDING: Q3c -- the arm's BOX-RUN scope
  (unconsumed member arms die with the box run, DL-54 review) sits in
  tension with one field aside ("JobB would start immediately after the
  next time its parent box starts"); the pin stands until a live test.
- run_window (SEM-33): a start attempt outside the window applies the
  closer-edge rule -- nearer the next opening: schedule a TIMER STARTJOB at
  window open (box context stays RUNNING overnight); nearer the previous
  end: no run, and a standalone job moves to INACTIVE (DL-246). Inside a
  live box run the skip is a bypass (DL-154): the member goes INACTIVE,
  stays out of the ran set, and the SEM-11 fold completes past it -- "the
  job's status changes to INACTIVE. The box job can still run to
  completion" (TechDocs 12.1, run_window page). A box start decides each
  waiting run_window member's disposition at that instant, before its
  schedule gate (DL-246); the deferred STARTJOB is a start attempt with a
  tick's standing, through the normal gates (provisional). Exact midpoint:
  next opening
  ([?] undocumented; pinned here, revisit with live access). The window is
  read in the job's own `timezone` (SEM-35 re-bases every time attribute of
  that job), so the comparison runs on local wall time and the queued timer
  goes back on the engine clock; the name resolves through the runner's
  ladder. A job with no `timezone:` compares in the constructor's
  `default_tz`, else on the engine clock -- the vendor's own rule since
  DL-155 ("scheduled based on the time zone under which the scheduler is
  running"), with the engine clock playing the scheduler's zone. Within two
  days of a one-hour DST change at 02:00 local, the window is built as
  concrete intervals whose endpoints follow the vendor's DST rules (DL-249);
  other change shapes keep the wall-time comparison, unverified.
- Lookback (SEM-04): window -> status_at >= now - window. zero -> satisfied
  iff the predecessor's own last end (last_end_at) is at-or-after the
  EVALUATING job's last end (Q2a RESOLVED by citation, DL-54 -- "examines
  the last end time of the job first ... then the last end time of the
  condition job": BOTH sides are end times, so an n() predecessor bounced
  to INACTIVE is not a fresh run; box overrides anchor on the box itself).
  Q2b RESOLVED by citation (DL-58): a never-ended evaluator has no anchor
  and the atom is satisfied -- CA support: "working as designed. When a new
  job is inserted it has no initial/previous end time", with the epoch-0
  effect observed exactly as modeled.
- ON_ICE (SEM-05/SEM-20, DL-243): split by whether the atom carries a
  lookback qualifier. An atom WITH one -- any kind, the zero form included
  -- keeps the blanket pin: every status/exitcode atom on an iced
  predecessor evaluates TRUE, f()/t()/e() included, lookback ignored
  (DL-13, Q6-adjacent; cited since DL-58: KB 438836, "the system ignores
  the look-back condition"). An ORDINARY atom (`atom.lookback is None`, no
  qualifier at all) follows the vendor's own ON_ICE truth table instead
  (Start Conditions, AutoSys 24.2, ON_ICE downstream-conditions row):
  success/done/notrunning TRUE, failure/terminated/exitcode FALSE -- a
  narrower reading than the blanket-true pin, and the two tables disagree
  on f()/t()/exitcode. For a lookback-qualified atom the AutoSys 24.2
  "condition Attribute" page is explicit: "If the predecessor job being
  evaluated for the look-back condition is currently in an ON_ICE status,
  it always evaluates to true. That is, any look-back evaluation is
  ignored." That is the default. The Start Conditions table does not
  separate lookback atoms, so the two pages read f()/t()/exitcode
  differently and Q10 (section 9) stays open over which a live instance
  follows. The `ice-lookback` semantic switch selects the table's reading
  (DL-252): `ordinary` drops the qualifier and applies the table to the
  lookback atom too. ON_ICE sent to a STARTING
  or RUNNING job is ignored (DL-254, "Change the Executable Status of a
  Job": "The event has no effect on jobs with a status of STARTING or
  RUNNING"): no flag, no wake, one EVENT_IGNORED trace line. ON_ICE on a
  QUE_WAIT job still dequeues it (DL-50). The iced job itself
  never starts on a plain STARTJOB; FORCE_STARTJOB on a non-live iced job
  now clears the flag first (SEM-23 below) -- DL-13's "FORCE included"
  reading no longer holds for that case. OFF_ICE does not re-evaluate
  (conditions must REOCCUR).
- ON_HOLD (SEM-21): the held job does not start; nothing else changes;
  OFF_HOLD immediately re-evaluates that job's start (missed runs collapse
  to at most one). ON_HOLD sent to a STARTING or RUNNING job is ignored
  like ON_ICE (DL-254); a QUE_WAIT job is held in the queue.
- ON_NOEXEC (SEM-22, DL-243): when the job would start, it bypasses to
  SUCCESS (STARTING/RUNNING skipped) and downstream runs normally. A BOX
  does not bypass: it goes RUNNING and its members bypass as their
  conditions are met, box level by box level, so a member box walks its
  own members too. A member bypasses on its own flag or on any containing
  box's. The bypass joins the box's ran set like a real start, so the
  SEM-11 fold waits for it and the once-per-box-run gate holds; it also
  counts as the tick's run (the Q3/DL-54 reading), so no MUST_START_ALARM
  follows a bypass. A FAILURE or TERMINATED (non-live, non-BOX) job that is
  put ON_NOEXEC is moved to INACTIVE through DL-242's operator-INACTIVE
  path, exit code cleared (Job States page: "the effect is the same as
  sending the CHANGE_STATUS event to INACTIVE for the job"). This is an
  EVENT-TIME transition, not a read-time projection: the stored row
  changes, a RUNNING box member resolves the same way an operator's
  CHANGE_STATUS INACTIVE would, and the transition wakes referencers
  normally -- f()/t()/d()/exitcode now read false, n() reads true, s()
  stays false, same as any other INACTIVE job. SUCCESS is the documented
  exception and is left alone. The scheduler ignores ON_NOEXEC sent to a
  non-box job that is STARTING, RUNNING or ON_ICE, and to a box that is
  RUNNING or ON_ICE, or that contains (at any depth) a job that is iced,
  STARTING, RUNNING or QUE_WAIT (DL-254): no flag, no transition, one
  EVENT_IGNORED trace line, so a real failure that follows is not hidden
  and the next run executes. A QUE_WAIT job leaves the queue and moves to
  INACTIVE through the same operator-INACTIVE path, so its next start
  bypasses. ON_NOEXEC supersedes ON_HOLD: the hold is cleared and recorded
  as an OFF_HOLD. A job released from a hold or the queue retries its start,
  so one whose conditions hold bypasses at once. On a box it is CHANGE_STATUS INACTIVE for the box (the
  SEM-18 cascade) and every contained job, at every level, takes the flag;
  OFF_NOEXEC on a box clears them all (DL-254).
- initial_status (SEM-24, DL-18): definition-time ON_HOLD/ON_ICE/ON_NOEXEC
  seeds the corresponding flag before the first event; no trace entry
  (definition state, not a transition). INACTIVE is the default anyway.
- Boxes: SEM-10 (members start when box RUNNING + own condition; at most
  once per box run; a box start resets every contained job that is not
  live to INACTIVE, so no status survives from the previous box cycle,
  DL-242), SEM-11 LITERAL (DL-13: the box cannot complete until
  every member that is not ON_ICE has RUN to a terminal state, been
  bypassed to SUCCESS by SEM-22, or been resolved to INACTIVE inside the
  run by a window skip (SEM-33/DL-154) or an injected STATUS INACTIVE
  (DL-242); a member whose condition never fires,
  or whose run_window deferred it, keeps the box RUNNING: the hung-box
  pattern is real behavior), SEM-12 (override
  gating: internal refs evaluated on the referenced member's transition;
  external/global refs evaluated only at member completion moments -- the
  hung-RUNNING pattern; "inside" is TRANSITIVE, as in derive._is_inside, so
  every ancestor box evaluates its overrides on a descendant's transition,
  not just the direct parent), SEM-13 (TERMINATED boxes are sticky until the
  next box start), SEM-14 (box_terminator member FAILURE -- not
  TERMINATED -- kills the box; job_terminator members die with the box),
  SEM-15 (a terminal member transition, or an injected INACTIVE on a
  member, re-derives a non-running, non-TERMINATED box's status once every
  member that is not INACTIVE is terminal; INACTIVE members are ignored,
  DL-242), SEM-17 (nesting: a member box starting is a member start; folds
  recurse; the ACTIVATED label is unmodeled -- a waiting member reads
  INACTIVE), SEM-18 (an injected INACTIVE on a box cascades to every job it
  contains, DL-242).
- FORCE_STARTJOB (SEM-23, DL-243): overrides false conditions, ON_HOLD, and
  the box-RUNNING gate ("regardless of conditions"). A non-live job that is
  ON_ICE or ON_HOLD is a non-executable state the force clears first
  (sendevent Start Jobs page: "it returns to an executable state, runs,
  and does not revert to the previous ... state"), recorded the same way
  as an OFF_ICE/OFF_HOLD sendevent with a cause naming FORCE_STARTJOB; the
  flag stays cleared even if a later gate (`run_window`) still refuses the
  start, because the event's own effect is the return to an executable
  state. A job that is already STARTING/RUNNING/QUE_WAIT is still refused
  (concurrent runs of one job are unsupported). ON_NOEXEC is not named in
  that vendor sentence and is untouched by FORCE. Forced runs emit normal
  statuses and satisfy downstream latches.
- Injected STATUS may overwrite a terminal status (the CHANGE_STATUS
  analog): script-authoring hazard, documented not guarded.
- must_start_times / must_complete_times (SEM-34): alarms only, never
  control flow. The STARTJOB tick arms both halves (DL-248), even when the
  start is abandoned or deferred -- that is the alarm's point. A relative
  deadline is tick+offset. An absolute one is the slot's must time on the
  tick's local calendar day, hours 24-71 on the days after, with the
  vendor's DST rules (DL-253). must_start alarms iff no new run began by
  the deadline. must_complete alarms iff the first run to begin after the
  tick has not ended by it; a late start does not move the deadline, and a
  FORCE_STARTJOB, which is no tick, arms none. One deadline of each kind
  is pending per job at a time: a tick on a live job, or while an earlier
  deadline of that kind is pending, arms none (DL-248, DL-253). N values
  against N start_times pair BY POSITION -- the tick names the slot;
  start_mins takes a single relative offset only (DL-248). A SINGLE offset
  broadcasts over every start time (the vendor's relative syntax is one
  `+minutes` for each start time), and an instant that matches no start
  time keeps the first offset. An absolute form arms nothing for such an
  instant: its times belong to their start times.
- term_run_time (dossier ss5): control flow -- auto-TERMINATE when the run
  exceeds the limit, checked lazily as the clock advances.
- n_retrys: Q4 resolved (DL-53) -- the trigger set is FAILURE-only application
  failures (TERMINATED does not restart; system failures restart via the
  scheduler's MaxRestartTrys config instead). Retry modeling stays out of
  scope v1 as a recorded DL-53 scope decision, not an open question; a future
  work item. auto_hold: member enters ON_HOLD when its box starts (dossier
  ss5 [C]).
- Undefined jobs in conditions evaluate FALSE forever (SEM-06). Cross-
  instance atoms (SEM-07) evaluate against instance-qualified pseudo-job
  entries in the status store, settable only by injected STATUS events with
  job "name^INST" -- the boundary is script-controlled.
- Resources/load (DL-50): a job that clears its start gate acquires an ATOMIC
  full demand vector -- machine-load slots (job_load vs machine max_load) and
  `resources:` semaphore units (QUANTITY vs insert_resource `amount`) -- before
  RUNNING. If any bucket is short it enters QUE_WAIT and is admitted later, in
  deterministic (priority, enqueue-seq, name) order, when a holder's terminal
  release frees room. All-or-nothing acquire => a job that holds nothing from
  an earlier run never holds and waits; QUANTITY=1 shared == mutex. A holder
  of units kept from an earlier run (DL-256) queues with them, and with
  DL-255's priority block two such holders can wait on each other for ever:
  a stated limit, broken only by an operator act (DL-286). Priority blocking
  (below) can still starve a lower-priority job while a higher-priority
  waiter cannot fit; preflight refuses a load that can never fit, which
  bounds that wait to feasible loads (DL-247). Only a job with a
  positive priority checks machine load; an unset or zero priority and a
  FORCE_STARTJOB skip the check, and every start holds its load. A job
  waiting for load blocks every lower positive priority on the same
  machine, on a fresh start and in the readmission scan (DL-247). A job
  past its load check and short on any named resource blocks every lower
  positive priority that names any resource it names, forced or not
  (DL-255); a queued job holds no load. A job
  leaving the queue and a waiter put on hold wake the queue, since each can
  lift a block; a box leaving RUNNING, and a start or enqueue that takes
  machine load, owe an admit-only scan. The input pays it once fully
  applied, after its releases and every referencer it woke, and it leaves
  the stopped box's queued members queued. res_type sets the default
  release (R/absent free-on-success, D never, T is a level GATE that never
  acquires); per-request FREE overrides it (Y success-only, N never, A
  unconditional). An omitted FREE on a renewable reads the `renewable-free`
  semantic switch: Y, the vendor default, or A, free on every completion
  (DL-256). A renewable's units its policy does not free stay HELD on the
  job's row after the run, until RELEASE_RESOURCE or the job's next run,
  which re-uses them; a depletable's are spent. A FORCE_STARTJOB of a
  FAILURE or TERMINATED holder starts on its held units with no admission
  check (DL-256). A queued job re-validates box-RUNNING/ice/hold at admission.
  Whether it re-checks its starting conditions is the `queued-recheck`
  semantic switch, the vendor's EvaluateQueuedJobStarts (DL-257): at the
  default 0 they are NOT re-checked (Qr6, decided); 1 re-checks the
  condition, run_window and exclude_calendar; 2 also the day's run_calendar
  or days_of_week. A job that fails leaves the queue for INACTIVE, its arm
  cleared, and waits for its next start time; a member of a running box
  stays unresolved, the ACTIVATED analog. A run_window failure takes
  DL-246's disposition at that instant, and a day failure of a job the
  scheduler never ticks is deferred to its next eligible opening, so no
  job waits for a start that never comes. Enforcement of unsized/
  unknown-res_type/malformed shapes is the runner's preflight (DL-50): the
  oracle models only sizeable buckets, so oracle-direct over an unrefused bad
  catalog runs it unthrottled -- the execution gate is preflight, by design.
- Still non-goals v1: definition-time mutations (SEM-16; incl. mid-run
  update_resource replenishment of depletables), agent failures.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from typing import Final, cast

from dsl41.autocal import CalendarRuleError, CompiledCalendar, compile_calendar, standard_days
from dsl41.canon import CanonError, canonical_bytes
from dsl41.capacity import (
    CapacityPool,
    DemandEntry,
    checks_load,
    machine_load,
    takes_machine_load,
    to_reservations,
    without_machine_load,
)
from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    GlobalAtom,
    Lookback,
    Or,
    Paren,
    StatusAtom,
    compare_int,
    compare_value,
)
from dsl41.ir import CatalogIR, JobIR, MustTime, Semantics, Time

from dsl41.oracle_state import (
    INJECTABLE_STATUSES,
    LIVE,
    TERMINAL,
    CarriedRows,
    Event,
    EventKind,
    JobRuntime,
    JobStatus,
    OracleError,
    RuntimeState,
    TraceEntry,
)
from dsl41.semantics import DEFAULTS as DEFAULT_SWITCHES, SemanticSwitches, iced_atom_truth
from dsl41.timezones import (
    MISSING_HOUR,
    REPEATED_HOUR,
    dst_change,
    dst_change_near,
    resolve_timezone,
    start_time_instants,
    to_local,
    to_utc,
    vendor_gap_instant,
)

#: SEM-02: n() is true unless the job is in one of these (WAIT_REPLY/RESTART/
#: SUSPENDED are out-of-scope states the oracle never produces). QUE_WAIT is
#: DELIBERATELY absent (DL-50): a resource-queued job is not running, so n() is
#: TRUE for it -- and every status/exitcode atom reads false (it never ran).
_N_FALSE_STATUSES: frozenset[str] = frozenset({"STARTING", "RUNNING"})

#: how far ahead `_next_eligible_opening` looks for an eligible day (DL-257)
_OPENING_SCAN_DAYS: Final = 2 * 366

#: `date.weekday()` (Monday 0) -> the JIL days_of_week token
_WEEKDAY_TOKENS: Final = ("mo", "tu", "we", "th", "fr", "sa", "su")

#: SEM-34: how long after a start time's instant an event still names its
#: slot -- the minute the wall-time match used to cover (DL-260)
_SLOT_MINUTE = timedelta(minutes=1)


class InputBatch:
    """One admitted input, applied as ONE store transaction.

    The frozen admission order (concurrency-model ss4) commits a batch --
    the time observation `TimeAdvanced(at)` and the attempt itself -- as a
    unit, and ss3 puts one revision on that unit: an entity moves at most
    once per COMMITTED INPUT, which is not the same as once per call into
    the oracle. Doing the two halves as two calls would let one command bump
    an entity twice and make `expect` name a revision no client ever read.

    Entering applies the time half; timers due at or before `at` fire HERE,
    which is what puts them ahead of the shell's gate (ss4 step 5 -- a
    term_run_time firing between the gate and the apply would defeat the
    precondition it just passed). `feed` applies the attempt. Leaving
    commits, whether or not the attempt was fed and whether or not the drain
    raised: the oracle has no rollback, so whatever DID change is durable
    and a reader holding the old revision must be invalidated by it (DL-87).

    Hold one open only when a decision sits between the halves -- the
    engine's stale-completion gate does, and S3's preconditions will.
    """

    def __init__(self, oracle: Oracle, at: datetime) -> None:
        self._oracle = oracle
        self._at = at
        #: events emitted across the whole batch, in order; set at commit
        self.emitted: list[Event] = []
        #: entity key -> its revision after this input -- the ss4 step-7
        #: record of what the input moved, and the only place the changed set
        #: leaves the owner
        self.revisions: dict[str, int] = {}
        self._emitted_start = 0

    def __enter__(self) -> InputBatch:
        oracle = self._oracle
        if oracle._now is not None and self._at < oracle._now:
            raise OracleError(f"input time went backwards: {self._at} < {oracle._now}")
        self._emitted_start = len(oracle._emitted)
        oracle.store.begin_input()
        try:
            # DL-256: a period's first input first gives back the units of
            # removed jobs, at the opening instant, before any timer fires
            oracle._release_removed_holders()
            # fire timers due at or before this input first, in time order
            oracle._fire_timers_due(self._at)
            oracle._now = self._at
            oracle._lazy_clock_checks()
            oracle._drain()  # checks-then-drain adjacency
        except BaseException:
            self._commit()
            raise
        return self

    def feed(self, ev: Event) -> None:
        """Apply the attempt half. Its stamp IS the batch's time observation,
        so a mismatch is a caller bug, not a second observation."""
        if ev.at != self._at:
            raise OracleError(f"attempt stamped {ev.at} in a batch opened at {self._at}")
        self._oracle._queue.append(ev)
        self._oracle._drain()

    def __exit__(self, *exc: object) -> None:
        self._commit()

    def _commit(self) -> None:
        oracle = self._oracle
        changed = oracle.store.commit_input()
        self.revisions = {key: oracle.store.revision(key) for key in changed}
        self.emitted = oracle._emitted[self._emitted_start :]


#: The closed set of deadline checks the oracle arms as TIMER payloads
#: (`payload["check"]`); the fourth shape, the run-window defer, carries
#: `deferred_cause` instead. PR-09 enumerates this set and proves each member
#: canonicalizes; `_schedule_timer` refuses anything outside it.
TIMER_CHECKS: Final[frozenset[str]] = frozenset({"must_start", "must_complete", "term_run_time"})

#: A box member's deferred start also carries its box's name and run number
#: (DL-246): the deferral belongs to that box run, and a box restart, or a
#: rebaseline that moves the member to another box, makes it stale.
DEFERRED_BOX_KEY: Final = "box"
DEFERRED_BOX_RUN_KEY: Final = "box_run"

#: PR-09's fourth armed shape: a run_window-deferred start carries no
#: deadline `check`, it carries the provenance of the start it defers.
#: Named once (DL-209) -- `_schedule_timer` admits it, `_dispatch` replays
#: it, and the coverage register derives the timer domain from the pair.
DEFERRED_TIMER_KEY: Final = "deferred_cause"

#: A deferred start that resumes `queued-recheck`'s scan for an eligible day
#: instead of attempting a start (DL-257) carries this key, valued at the
#: job's run number when the scan stopped at its bound.
DEFERRED_RESCAN_KEY: Final = "rescan_run"


class Oracle:
    """Deterministic interpreter over one CatalogIR (ir-design ss7)."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        default_tz: str | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        self.catalog = catalog
        #: the semantic switches (runner-design ss8a, DL-252): a caller with
        #: a runtime profile passes the period's own (`period.switches_of`);
        #: a static tool with no profile gets the registry defaults
        self.semantics: SemanticSwitches = semantics or DEFAULT_SWITCHES
        self.store = RuntimeState()
        if carried is not None:
            # period-model ss7 phase 3 step 3: carried rows install VERBATIM
            # and BEFORE the genesis seed, so the seed below can skip them
            # instead of overwriting them. A "construct then overwrite"
            # opener moves every carried revision, and an operator's
            # `expect` against a revision the seal published is then
            # unholdable.
            self.store.install(carried)
        # DL-87: the catalog seed IS an input -- the genesis one. Not
        # ceremony: it is what makes `revision(key) == 0` mean "absent" for a
        # global, and therefore what makes a conditional create expressible.
        # A declared global lands at revision 1 like anything else that has
        # been through an input; a never-declared name stays at 0.
        self.store.begin_input()
        for name, job_ir in catalog.jobs.items():
            if carried is not None and name in carried.jobs:
                continue  # ss7 phase 3 step 4: a carried row keeps its C1 flags
            # SEM-24: definition-time state seeds the SEM-20/21/22 flags
            initial = job_ir.sem.initial_status
            self.store.set_flags(
                name,
                on_hold=initial == "ON_HOLD",
                on_ice=initial == "ON_ICE",
                on_noexec=initial == "ON_NOEXEC",
            )
        for name, value in catalog.globals_declared.items():
            if carried is not None and name in carried.globals_:
                continue  # only GENUINELY NEW rows are seeded (ss7 phase 3 step 4)
            self.store.set_global(name, value)
        self.store.commit_input()
        # the constructor's seed is not an input to the seed/advance latch
        self.store.finish_genesis()
        if carried is not None:
            self.store.seed_period(carried.period_id)
        self._trace: list[TraceEntry] = []
        self._emitted: list[Event] = []
        self._queue: deque[Event] = deque()
        #: ss3.3: feed times must be non-decreasing across the boundary, so
        #: an opened interpreter starts from the instant the seal was taken
        self._now: datetime | None = carried.now if carried is not None else None
        #: DL-256: carried rows of jobs this catalog no longer defines that
        #: still hold units while not live. Nothing can address such a job,
        #: so RELEASE_RESOURCE cannot reach its units; the period's first
        #: input gives them back (`_release_removed_holders`). A live one is
        #: refused at the boundary (period-model ss10.1), so it never opens.
        self._opening_release: list[str] = sorted(
            name
            for name, row in (carried.jobs.items() if carried is not None else ())
            if name not in catalog.jobs and row.reservations and row.status not in LIVE
        )
        #: edge-trigger index (DL-13): entity key -> jobs whose `condition`
        #: references it. Keys: job names (incl. "name^INST"), "g:NAME".
        self._referencers: dict[str, list[str]] = {}
        for name, job_ir in catalog.jobs.items():
            attr = job_ir.sem.condition
            if attr is None:
                continue
            for key in _entity_keys(attr.cond):
                self._referencers.setdefault(key, []).append(name)
        #: DL-50 capacity buckets + the QUE_WAIT queue (DL-74): it decides
        #: admission and its order, the transitions below are this class's.
        #: Since DL-120 it holds no state -- the reservations and the ranks are
        #: on the rows, the spent units are under the owner, and the pool is
        #: given all three.
        self._pool = CapacityPool(catalog, self.semantics)
        self._in_wake = False
        #: DL-247: a full scan requested while a scan runs; the running loop
        #: takes it up as its next pass, so an admit-only scan cannot swallow
        #: a release's cancellations.
        self._full_scan_requested = False
        #: DL-247: a block may have lifted with no capacity freed, so the
        #: admit-only scan is owed: a box left RUNNING, or (DL-255) a start
        #: or a queued job took a machine's load and so may have sent a
        #: resource waiter back to its load check. It runs at the waiter step
        #: of the input (DL-50's order: release, referencers, waiters), once
        #: every referencer the input woke has been visited; no nested
        #: transition pays it (DL-255).
        self._scan_owed = False
        #: SEM-35 name -> zone, resolved once per name (the ladder walks
        #: the whole zoneinfo database for a city default)
        self._tz_cache: dict[str, tzinfo] = {}
        #: calendar name -> its days, built on first use by `queued-recheck`
        #: (DL-257): a standard calendar's day set, or a compiled extended one
        self._calendars: dict[str, frozenset[date] | CompiledCalendar] = {}
        #: SEM-35's `ujo_timezones` table, as `--timezone-map` supplies it to
        #: the scheduler (DL-62). None means no map, which is a DIFFERENT
        #: resolution than an empty one: the ladder's unique-city default
        #: applies only when the estate supplied no table, so a run with a
        #: map gets the map's answer and nothing else (`timezones`'
        #: `resolve_timezone`). Wired from the scheduler at engine build:
        #: an oracle that resolved without it refused a map-only name at the
        #: first start although preflight had passed (DL-151).
        self._tz_aliases: dict[str, str] | None = None if tz_aliases is None else dict(tz_aliases)
        #: E10's no-timezone half, closed by citation (DL-155): the vendor
        #: schedules a job that declares no `timezone:` "based on the time
        #: zone under which the scheduler is running". `default_tz` IS that
        #: zone for a directly built oracle; None keeps the engine clock as
        #: the basis -- the same rule with the simulation's own frame as the
        #: scheduler's zone. The name resolves lazily in _job_tz through the
        #: SAME ladder and alias table as a job's own `timezone:` (DL-151).
        #: The runner passes nothing here: `--timezone` stays on the
        #: scheduler.
        self._default_tz: str | None = default_tz
        #: SEM-33 (DL-246): the box runs whose start is in progress, as (box,
        #: run number, cause), collected from the outermost box's RUNNING
        #: transition on, so the window decisions of a whole started subtree
        #: run after every attempt in it. None when no box start is open.
        self._window_starts: list[tuple[str, int, str]] | None = None

    # ------------------------------------------------------------------ plumbing

    def _members(self, box: str) -> list[str]:
        return [n for n, j in self.catalog.jobs.items() if j.box.box_name == box]

    def trace(self) -> list[TraceEntry]:
        return [entry.model_copy() for entry in self._trace]  # no aliasing out

    def batch(self, at: datetime) -> InputBatch:
        """Open one admitted input at `at` (concurrency-model ss4). Use this
        only when a decision sits BETWEEN the two halves of the batch -- the
        time observation and the attempt -- as the engine's gate does. With
        no such decision, feed() and advance() are the same thing said in one
        line."""
        return InputBatch(self, at)

    def feed(self, ev: Event) -> list[Event]:
        """Process one injected event (+ due timers + cascade); return events
        emitted during this call. Feed times must be non-decreasing."""
        with self.batch(ev.at) as batch:
            batch.feed(ev)
        return batch.emitted

    def next_timer_due(self) -> datetime | None:
        """Read-only peek at the timer heap (runner-design ss3): the earliest
        scheduled TIMER's due time, or None. Timers fire lazily inside feed()/
        advance(); a wall-clock shell uses this to know when to wake."""
        return self.store.next_timer_due()

    def pending_timers(self) -> list[tuple[datetime, str, str]]:
        """Read-only snapshot of LIVE pending timers as (due, job, kind),
        due-ordered; kind is the deadline-check name (`must_start`,
        `must_complete`, `term_run_time`) or `run_window` for a SEM-33
        deferred start. Liveness mirrors _dispatch_timer_check's fire-time
        rules -- a heap entry a fire would discard as stale (run_number moved
        on; a term_run_time run no longer RUNNING; a must_complete deadline
        already met by the run its tick asked for, DL-248) is not pending, it
        is dead weight awaiting its lazy pop. The ss10 status query renders this for
        the ss11 jobs table; display truth must be the dispatch truth --
        which is why the order is `store.timers()`'s and is NOT re-sorted
        here. On a TIE that order carries the ordering token, the firing
        order of equal-time timers (period-model ss3.2); a `sorted()` over
        `(due, job, kind)` reads as a no-op on an already-due-ordered list
        and silently replaced it with job-name order (DL-143)."""
        live: list[tuple[datetime, str, str]] = []
        for due, _, ev in self.store.timers():
            job = ev.payload.get("job")
            if not isinstance(job, str):  # pragma: no cover -- see below
                # Unreachable: every timer the oracle arms names its job (the
                # deadline checks and the run_window deferral both build the
                # payload from a job name), so no heap entry lacks one.
                continue
            check = ev.payload.get("check")
            if check is None:
                # SEM-33 deferred starts stay live UNCONDITIONALLY: unlike the
                # deadline checks, whose run-mismatch staleness is permanent,
                # a deferred STARTJOB is a real start attempt whose outcome
                # depends on fire-time state -- a job RUNNING now may have
                # completed by next_open, and the fire would legally start it
                # again. Filtering on current status would hide a timer that
                # can still act (DL-46 review, finding rejected with reason).
                # Two stalenesses ARE permanent: a box restart (DL-246), and
                # a scan continuation whose job has started since (DL-257).
                if self._deferral_is_stale(ev) is None and not (
                    DEFERRED_RESCAN_KEY in ev.payload
                    and self._rescan_superseded(job, ev.payload[DEFERRED_RESCAN_KEY])
                ):
                    live.append((due, job, "run_window"))
                continue
            rt = self.store.job.get(job)
            if check == "must_complete":
                # DL-248: judged against the tick's run, so a job that never
                # started still has a live completion deadline
                if rt is None or not self._slot_run_completed(rt, ev.payload.get("run")):
                    live.append((due, job, str(check)))
                continue
            if rt is None or ev.payload.get("run") != rt.run_number:
                continue  # stale: a later run superseded this deadline
            if check == "term_run_time" and rt.status != "RUNNING":
                continue  # the run already ended; the check fires as a no-op
            live.append((due, job, str(check)))
        return live

    def advance(self, now: datetime) -> list[Event]:
        """Fire timers due <= now without an external event (runner-design
        ss3): the same input as feed(), with the attempt absent. The clock is
        considered to have reached `now`, so a later feed()/advance() before
        `now` errors. Bisimulation (runner-design ss13) pins that feed-only
        and advance+feed schedules trace identically."""
        with self.batch(now) as batch:
            pass  # a standalone time observation is an input (concurrency-model ss4)
        return batch.emitted

    def _fire_timers_due(self, at: datetime) -> None:
        while (popped := self.store.pop_timer_due(at)) is not None:
            due, timer_ev = popped
            self._now = due
            self._lazy_clock_checks()
            self._queue.append(timer_ev)
            self._drain()

    def run_script(self, events: list[Event]) -> list[TraceEntry]:
        for ev in events:
            self.feed(ev)
        return self.trace()

    def _drain(self) -> None:
        while self._queue:
            self._dispatch(self._queue.popleft())
            # DL-255: the input's waiter step, after all its referencers
            self._run_owed_scan()

    def _emit(self, kind: EventKind, **payload: object) -> None:
        assert self._now is not None
        self._emitted.append(Event(at=self._now, kind=kind, payload=dict(payload)))

    def _record(self, job: str, transition: str, cause: str) -> None:
        assert self._now is not None
        self._trace.append(TraceEntry(at=self._now, job=job, transition=transition, cause=cause))

    def _schedule_timer(self, at: datetime, ev: Event) -> None:
        # PR-09: the shapes a timer payload can take are a CLOSED set, because
        # every one must be proven canonicalizable before it may be armed -- a
        # timer that cannot be written would leave the estate unsealable for
        # as long as it stayed armed. A new deadline kind is added to
        # TIMER_CHECKS (and to the PR-09 test that enumerates it) first.
        check = ev.payload.get("check")
        if check is None:
            if DEFERRED_TIMER_KEY not in ev.payload:
                raise OracleError("unregistered timer shape (PR-09)")
        elif check not in TIMER_CHECKS:
            raise OracleError(f"unregistered timer check {check!r} (PR-09)")
        # and the payload itself canonicalizes -- the registry names the
        # shapes, this proves the bytes, and neither vanishes under -O
        try:
            canonical_bytes(ev.payload)
        except CanonError as exc:
            raise OracleError(f"timer payload is not canonicalizable (PR-09): {exc}") from exc
        self.store.enqueue_timer(at, ev)

    # -------------------------------------------------------------- status store

    def _runtime(self, job: str) -> JobRuntime:
        return self.store.runtime(job)  # DL-82: the store owns creation too

    def _set_status(
        self,
        job: str,
        status: JobStatus,
        cause: str,
        exit_code: int | None = None,
        *,
        clear_exit_code: bool = False,
    ) -> None:
        old = self._runtime(job).status
        self.store.transition(job, status, self._now, exit_code, clear_exit_code=clear_exit_code)
        self._record(job, f"{old}->{status}", cause)
        self._emit("STATUS", job=job, status=status)
        self._after_transition(job, old, status)

    def _after_transition(self, job: str, old: str, new: str) -> None:
        """Every consequence of one transition, in the documented order: the
        box rules, then the row's own settlement, then the wakes. A batch of
        transitions (`_set_inactive_batch`) runs the same three pieces, with
        every row settled before anything is notified."""
        if self._box_stopped(job, old, new):
            self._scan_owed = True
        self._notify_boxes(job, old, new)
        released = self._settle_row(job, old, new)
        self._notify_wakes(job, new, wake_queue=released or self._lifts_a_block(old, new))

    def _notify_boxes(self, job: str, old: str, new: str) -> None:
        """The box rules a transition drives: the parent's member rules, the
        ancestors' transitive overrides, and the Q3c disarm of a box that
        reached a terminal status."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None:
            return
        box = job_ir.box.box_name
        if box is not None:
            self._on_member_transition(box, job, old, new)
            self._on_descendant_transition(job, new, resolved=self._resolves(box, job, old, new))
        if job_ir.job_type == "BOX" and new in TERMINAL:
            self._disarm_members(job)

    def _disarm_members(self, box: str) -> None:
        """A member's arm is scoped to the box run that armed it (DL-54
        review MAJOR): an unconsumed arm dies with the run, BEFORE any wake
        can ride it. Nested boxes recurse via their own completion
        transitions, or through the SEM-18 cascade (DL-242)."""
        # PENDING: Q3c -- one field aside (DL-58) hints the vendor latch may
        # instead survive into the NEXT box run; the scoped pin stands until
        # a live test.
        for member in self._members(box):
            m_rt = self.store.job.get(member)
            if m_rt is not None and m_rt.armed:
                self.store.set_armed(member, False)
                self._record(
                    member,
                    "SCHED_DISARM",
                    f"unconsumed arm dies with box {box!r} run (Q3c pin, DL-54/58)",
                )

    def _settle_row(self, job: str, old: str, new: str) -> bool:
        """The row-local consequences of a transition; True when it released
        reservations, so the caller wakes the waiters after the referencers.

        DL-120: a job leaves QUE_WAIT through one of three paths that drop
        its rank first (admitted, killed, cancelled) -- or through an
        injected STATUS, which drops nothing. A rank left behind kept a
        terminal job in the admission queue, where the next release would
        start it again, so the rank goes with the status that owns it.

        DL-50: RELEASE a completed holder's units BEFORE waking anything. A
        self-referencing re-trigger (the L010 tight-loop -- _wake_referencers
        may re-start the very job that just completed) must re-acquire
        against the FREED capacity, not overwrite its own still-held record
        and strand a unit (adversarial review BLOCKER).

        DL-120: the release edge is LEAVING the live statuses, not reaching
        a terminal one. The two coincide for every ordinary run and differ
        only for an injected STATUS INACTIVE on a live holder, which used to
        strand the units in a `_held` record no row could see. A run's
        reservations are taken on entering STARTING or RUNNING (period-model
        ss5), so the release is on the edge that leaves them; a non-SUCCESS
        exit spends what a depletable was always going to spend. What the
        policy does not free stays on a row that is no longer live (DL-256,
        below; `may_outlive_run`).

        DL-256: a renewable's units that the policy does not free (FREE=N,
        and FREE=Y or an omitted FREE under `renewable-free=Y` after a
        FAILURE or TERMINATED) stay on the row, held by the job. So the edge
        is read from `old`, not from the row: a later transition of a job
        that is not live, INACTIVE or QUE_WAIT included, keeps them."""
        if new != "QUE_WAIT" and self._runtime(job).waiter_seq is not None:
            self.store.dequeue_waiter(job)
        released = old in LIVE and new not in LIVE and self._pool.holds(self._runtime(job))
        if released:
            self.store.release_reservations(job, new, self._pool.keeps_held)
        return released

    @staticmethod
    def _lifts_a_block(old: str, new: str) -> bool:
        """DL-247: a job leaving QUE_WAIT can lift a priority block without
        freeing capacity, so it wakes the queue. QUE_WAIT -> STARTING is an
        admission, made by the scan that is already running; that scan's
        next pass sees the change, so it requests nothing."""
        return old == "QUE_WAIT" and new not in ("QUE_WAIT", "STARTING")

    def _box_stopped(self, job: str, old: str, new: str) -> bool:
        """DL-247: a box leaving RUNNING. Its queued members stop blocking at
        once, so a job they blocked may now start."""
        job_ir = self.catalog.jobs.get(job)
        return old == "RUNNING" != new and job_ir is not None and job_ir.job_type == "BOX"

    def _notify_wakes(self, job: str, new: str, *, wake_queue: bool) -> None:
        """SEM-01/dossier ss0: the transition wakes exactly the jobs whose
        condition references this one (edge-triggered, DL-13). Waiters then
        wake after condition referencers -- the documented deterministic
        order -- when capacity was released or a block lifted."""
        self._wake_referencers(job, cause=f"status of {job!r} changed to {new}")
        if wake_queue:
            self._wake_waiters()

    def _run_owed_scan(self) -> None:
        """DL-247: a box that stopped running lifts the blocks its queued
        members held, so the queue is scanned when a queued job has a
        positive priority on a sized machine or resource. DL-255 owes the
        same scan after a start or an enqueue that takes a machine's load:
        a resource waiter whose load no longer fits, or that a new load
        waiter now blocks, is back at its load check and stops blocking on
        its resources. The scan is owed, not run, at the act itself. The
        input pays it once it is fully applied, after every release and
        every referencer it woke (DL-255): a transition nested in that input
        does not, or a scan owed by one referencer's start could admit a
        waiter ahead of a later referencer. A scan that ran in between has
        already paid it. It only
        admits: every member of a stopped box stays queued for the next
        release to cancel (DL-158, DL-54)."""
        while self._scan_owed and not self._in_wake:
            self._scan_owed = False
            if self._pool.has_priority_waiters(self.store.job):
                self._wake_waiters(keep_stopped=True)

    def _set_inactive_batch(
        self,
        rows: list[tuple[str, str]],
        *,
        clear_exit_code: bool = False,
        exit_code: int | None = None,
        between: Callable[[], None],
    ) -> None:
        """Move several jobs to INACTIVE as one act (DL-242): the box-start
        reset (SEM-10) and the box INACTIVE cascade (SEM-18). `rows` pairs
        each job with its trace cause, in order.

        Phase 1 writes every row -- store, trace record, STATUS emission --
        and settles it (rank, reservations). Phase 2 then runs the box rules
        and the wakes for each row, in the same order. Writing first means
        no wake sees a half-moved subtree: a member woken by a sibling's
        reset or cascade meets its box already out of RUNNING. Settling
        first means a restart in phase 2 acquires against freed capacity.
        A wake in phase 2 may restart a job of the batch; such a row did
        its own notifications, so phase 2 skips any row that has moved on.
        `between` runs after phase 1: the cascade's Q3c disarm, or the box's
        own STARTING transition for the reset.

        `exit_code` is written on the first row only (an injected STATUS
        may carry one); `clear_exit_code` clears it on every row."""
        written: list[tuple[str, str, int, bool]] = []
        for index, (job, cause) in enumerate(rows):
            old = self._runtime(job).status
            code = exit_code if index == 0 else None
            self.store.transition(job, "INACTIVE", self._now, code, clear_exit_code=clear_exit_code)
            self._record(job, f"{old}->INACTIVE", cause)
            self._emit("STATUS", job=job, status="INACTIVE")
            # the queue's wake: capacity released, or a block lifted (DL-247)
            wake = self._settle_row(job, old, "INACTIVE") or self._lifts_a_block(old, "INACTIVE")
            if self._box_stopped(job, old, "INACTIVE"):
                self._scan_owed = True
            written.append((job, old, self._runtime(job).run_number, wake))
        between()
        owed = False
        for job, before, run_number, wake in written:
            rt = self._runtime(job)
            if rt.status != "INACTIVE" or rt.run_number != run_number:
                # restarted by an earlier wake of this phase; capacity it
                # released in phase 1 still owes the waiters their wake
                owed = owed or wake
                continue
            # `before` is the pre-batch status even when a wake restarted the
            # box around this row; no rule reads it here, so that is harmless
            self._notify_boxes(job, before, "INACTIVE")
            self._notify_wakes(job, "INACTIVE", wake_queue=wake)
        if owed:
            self._wake_waiters()

    # ------------------------------------------------------------ event dispatch

    def _dispatch(self, ev: Event) -> None:
        kind = ev.kind
        if kind == "STATUS":
            self._handle_status(ev)
        elif kind in ("STARTJOB", "FORCE_STARTJOB", "TIMER"):
            if kind == "TIMER" and self._dispatch_timer_check(ev):
                return  # deadline-check timers are not start attempts
            job = self._required_job(ev)
            force = kind == "FORCE_STARTJOB"
            if kind == "STARTJOB":
                # SEM-34: the schedule tick arms both deadlines whether or not
                # the start succeeds -- that is their point (DL-248)
                if (timer := self._tick_deadline(job, "must_start")) is not None:
                    self._schedule_timer(timer.at, timer)
                if (timer := self._tick_deadline(job, "must_complete")) is not None:
                    self._schedule_timer(timer.at, timer)
            # DL-68: a sourced event names its trigger -- a scheduler tick and
            # an operator sendevent must not collapse to one cause string
            cause = f"{kind} event ({ev.source})" if ev.source else f"{kind} event"
            deferred = ev.payload.get(DEFERRED_TIMER_KEY)
            if isinstance(deferred, str):
                # SEM-33 defer: the fired timer replays the original start's
                # provenance instead of collapsing to a bare TIMER (DL-68)
                cause = f"run_window-deferred {deferred}"
                stale = self._deferral_is_stale(ev)
                if stale is not None:
                    self._record(job, "START_REFUSED", f"{stale} ({cause})")
                    return
                if DEFERRED_RESCAN_KEY in ev.payload:
                    self._resume_opening_scan(job, ev.payload[DEFERRED_RESCAN_KEY], deferred)
                    return
            refused = self._attempt_start(job, force=force, scheduled=True, cause=cause)
            if refused is not None:
                self._record(job, "START_REFUSED", f"{refused} ({cause})")
        elif kind == "SET_GLOBAL":
            name = ev.payload.get("name")
            value = ev.payload.get("value")
            if not isinstance(name, str):
                raise OracleError("SET_GLOBAL requires payload.name")
            self.store.set_global(name, str(value))
            self._wake_referencers(f"g:{name}", cause=f"SET_GLOBAL {name}")
        elif kind == "KILLJOB":
            job = self._required_job(ev)
            status = self._runtime(job).status
            if status in LIVE:
                self._terminate(job, cause="KILLJOB")
            elif status == "QUE_WAIT":
                # DL-50 (review MAJOR): a kill on a QUEUED job must not be
                # silently dropped and then admitted on the next release -- a
                # standalone queued job has no box-end to cancel it. It holds
                # nothing; dequeue and TERMINATE (the kill happened).
                self.store.dequeue_waiter(job)
                # Q3 (DL-54): the kill consumes a latched arm -- the queued
                # attempt was the tick's run and it just got killed.
                self.store.set_armed(job, False)
                self._set_status(job, "TERMINATED", cause="KILLJOB (dequeued from QUE_WAIT, DL-50)")
        elif kind in (
            "ON_ICE",
            "OFF_ICE",
            "ON_HOLD",
            "OFF_HOLD",
            "ON_NOEXEC",
            "OFF_NOEXEC",
            "DISARM",
        ):
            self._handle_oob(kind, self._required_job(ev))
        elif kind == "RELEASE_RESOURCE":
            self._release_resource(self._required_job(ev))
        else:
            raise OracleError(f"uninjectable event kind {kind!r}")

    def _required_job(self, ev: Event) -> str:
        job = ev.job()
        if job is None:
            raise OracleError(f"{ev.kind} requires payload.job")
        return job

    def _handle_status(self, ev: Event) -> None:
        job = self._required_job(ev)
        status = ev.payload.get("status")
        exit_code = ev.payload.get("exit_code")
        job_ir = self.catalog.jobs.get(job)
        if status is None:
            if not isinstance(exit_code, int):
                raise OracleError("STATUS requires payload.status or integer payload.exit_code")
            # SEM-09 (DL-33): per-job boundary -- max_exit_success threshold
            # plus the explicit success_codes/fail_codes sets (Q7 corners
            # pinned in ir.exit_is_success).
            sem = job_ir.sem if job_ir is not None else Semantics()
            status = "SUCCESS" if sem.exit_is_success(exit_code) else "FAILURE"
        if not isinstance(status, str) or status not in INJECTABLE_STATUSES:
            raise OracleError(f"unknown status {status!r}")
        code = exit_code if isinstance(exit_code, int) else None
        if status != "INACTIVE" or job_ir is None:
            # INJECTABLE_STATUSES holds JobStatus members only
            self._set_status(job, cast(JobStatus, status), cause="injected STATUS", exit_code=code)
            return
        self._inject_inactive(job_ir, code)

    def _status(self, job: str) -> str:
        return self._runtime(job).status

    def _inject_inactive(
        self,
        job_ir: JobIR,
        code: int | None,
        *,
        clear_exit_code: bool = False,
        cause: str = "injected STATUS",
    ) -> None:
        """An operator's CHANGE_STATUS INACTIVE (DL-242). Three vendor rules
        ride on it, besides DL-235's no-kill ruling for a launched run.
        DL-243 reuses this path for ON_NOEXEC on a completed FAILURE/
        TERMINATED job (`cause` names that call instead; `clear_exit_code`
        drops the previous run's code, same as the SEM-10 box-start reset).

        In a RUNNING box the member resolves: "affects the box's completion
        status as if the INACTIVE job returned a status of SUCCESS"
        (SEM-11). The mark lands BEFORE the transition, so the transition
        itself runs the completion door. An injected STATUS always records a
        transition, INACTIVE->INACTIVE included, so a waiting member set
        INACTIVE runs the door the same way.

        On a box, every contained job goes INACTIVE too (SEM-18), as one
        batch: every row is written before anything is woken, and the box
        runs in the subtree lose their unconsumed arms (Q3c).

        On a member of a box that is not running, the box re-derives its
        status with INACTIVE members ignored (SEM-15). Only this direct
        target triggers that: the box-start reset, the SEM-18 cascade and a
        window skip are internal transitions and leave the box alone. The
        parent is re-read after the transition: a wake may have started it."""
        job = job_ir.name
        box = job_ir.box.box_name
        parent = None if box is None else self._runtime(box)
        if box is not None and parent is not None and parent.status == "RUNNING":
            self.store.record_resolution(box, job)
        if job_ir.job_type != "BOX":
            self._set_status(
                job, "INACTIVE", cause=cause, exit_code=code, clear_exit_code=clear_exit_code
            )
        else:
            cascade_cause = (
                f"box {job!r} set INACTIVE: cascades to every job it contains (SEM-18, DL-242)"
            )
            inner = [
                j for j in self._contained(job, skip_live=False) if self._status(j) != "INACTIVE"
            ]
            boxes = [job] + [j for j in inner if self.catalog.jobs[j].job_type == "BOX"]

            def disarm() -> None:
                # Q3c: the box run ends here, so its unconsumed arms die
                for each in boxes:
                    self._disarm_members(each)

            self._set_inactive_batch(
                [(job, cause)] + [(j, cascade_cause) for j in inner],
                exit_code=code,
                clear_exit_code=clear_exit_code,
                between=disarm,
            )
        if box is None or parent is None:
            return
        # SEM-15 on an idle parent -- unless this transition's own wakes
        # started it, or it was running, starting or sticky TERMINATED
        settled = ("RUNNING", "STARTING", "TERMINATED")
        now = self._runtime(box)
        if (
            parent.status not in settled
            and now.status not in settled
            and now.run_number == parent.run_number
        ):
            self._idle_box_recompute(
                box, self.catalog.jobs[box], cause=f"member {job!r} set INACTIVE"
            )

    def _oob_ignored(self, kind: EventKind, job: str) -> str | None:
        """Why the scheduler ignores this ON_ICE/ON_HOLD/ON_NOEXEC, or None
        (DL-254). "Change the Executable Status of a Job" (AutoSys 24.2):
        ON_HOLD and ON_ICE have "no effect on jobs with a status of STARTING
        or RUNNING", box or not. ON_NOEXEC is ignored for a non-box job that
        is STARTING, RUNNING or ON_ICE, for a box that is ON_ICE or RUNNING,
        and for "A box job with jobs (including the jobs contained in lower
        level boxes) in a status other than the following status: ON_HOLD,
        ON_NOEXEC, INACTIVE, SUCCESS, FAILURE, ACTIVATED, or TERMINATED". In
        this model that is a contained job that is iced, STARTING, RUNNING
        or QUE_WAIT. QUE_WAIT is not named for the job itself, so a queued
        job keeps the handling below."""
        rt = self._runtime(job)
        if kind in ("ON_ICE", "ON_HOLD"):
            if rt.status in LIVE:
                return f"sendevent {kind} ignored: the job is {rt.status} (DL-254)"
            return None
        if kind != "ON_NOEXEC":
            return None
        job_ir = self.catalog.jobs.get(job)
        is_box = job_ir is not None and job_ir.job_type == "BOX"
        if rt.on_ice:
            return "sendevent ON_NOEXEC ignored: the job is ON_ICE (DL-254)"
        if rt.status == "RUNNING" or (not is_box and rt.status == "STARTING"):
            return f"sendevent ON_NOEXEC ignored: the job is {rt.status} (DL-254)"
        if not is_box:
            return None
        for member in self._contained(job, skip_live=False):
            mrt = self._runtime(member)
            if mrt.on_ice:
                return f"sendevent ON_NOEXEC ignored: contained job {member!r} is ON_ICE (DL-254)"
            if mrt.status in LIVE | {"QUE_WAIT"}:
                return (
                    f"sendevent ON_NOEXEC ignored: contained job {member!r} is"
                    f" {mrt.status} (DL-254)"
                )
        return None

    def _handle_oob(self, kind: EventKind, job: str) -> None:
        ignored = self._oob_ignored(kind, job)
        if ignored is not None:
            # DL-254: no flag, no transition, no wake -- only the trace line,
            # the START_REFUSED shape for an operator event that did nothing
            self._record(job, "EVENT_IGNORED", ignored)
            return
        # the status BEFORE the flag change -- a flag never moves a status, but
        # the rows are frozen (DL-86), so hold the value, not a stale row
        status = self._runtime(job).status
        if kind == "ON_ICE":
            iced = self._runtime(job).on_ice  # a second ice resolves nothing
            self.store.set_flags(job, on_ice=True)
            self._record(job, "ON_ICE", "sendevent ON_ICE")
            if status == "QUE_WAIT":
                # DL-50 (review NIT): an iced job never runs -- drop it from the
                # queue and settle its status now, instead of lingering QUE_WAIT
                # until a later release cancels it. _set_status wakes referencers,
                # so on_ice-satisfaction (SEM-20) still propagates.
                self.store.dequeue_waiter(job)
                self._set_status(job, "INACTIVE", cause="iced while queued (DL-50)")
            else:
                # SEM-20, DL-285: the box rules first, as a transition runs
                # them before its wakes; then downstream conditions treat
                # this job as satisfied
                if not iced:
                    self._ice_resolves_member(job, status)
                self._wake_referencers(job, cause=f"{job!r} put ON_ICE")
        elif kind == "OFF_ICE":
            self.store.set_flags(job, on_ice=False)
            self._record(job, "OFF_ICE", "sendevent OFF_ICE")
            # SEM-20: deliberately NO re-evaluation -- conditions must reoccur
            # PENDING: Q3d (DL-69) -- a pre-existing arm survives the ice
            # round-trip untouched (DL-54 pin, uncited), so a stale tick can
            # still start this job on the next condition edge despite the
            # "reoccur" rule. If the vendor instead discards the queued start
            # on ICE, clear rt.armed in the ON_ICE branch (SCHED_DISARM) and
            # amend SEM-20/32 -- protocol in docs/live-instance-runbook.md.
        elif kind == "ON_HOLD":
            self.store.set_flags(job, on_hold=True)
            self._record(job, "ON_HOLD", "sendevent ON_HOLD")
            if status == "QUE_WAIT":
                self._wake_waiters()  # DL-247: a held waiter blocks no one
        elif kind == "OFF_HOLD":
            self.store.set_flags(job, on_hold=False)
            self._record(job, "OFF_HOLD", "sendevent OFF_HOLD")
            if status == "QUE_WAIT":
                self._wake_waiters()  # DL-50: a held-while-queued job re-attempts
            else:
                # SEM-21: if conditions are already satisfied, run immediately
                self._attempt_start(job, force=False, scheduled=False, cause="OFF_HOLD")
        elif kind == "ON_NOEXEC":
            job_ir = self.catalog.jobs.get(job)
            if job_ir is not None and job_ir.job_type == "BOX":
                self._noexec_box(job_ir)
            else:
                self._noexec_job(job, job_ir, status)
        elif kind == "OFF_NOEXEC":
            # DL-243: a job ON_NOEXEC'd after FAILURE/TERMINATED was already
            # moved to INACTIVE at that event; OFF_NOEXEC here is a plain
            # flag clear on an already-INACTIVE row, same as the vendor's
            # "places the job in the INACTIVE ... status" (Events page,
            # JOB_OFF_NOEXEC). On a box it clears every contained job too
            # (DL-254): "If you send the JOB_OFF_NOEXEC to a box, all jobs in
            # the box (including all jobs that are contained in lower level
            # boxes within the box) are reset".
            self.store.set_flags(job, on_noexec=False)
            self._record(job, "OFF_NOEXEC", "sendevent OFF_NOEXEC")
            job_ir = self.catalog.jobs.get(job)
            if job_ir is not None and job_ir.job_type == "BOX":
                for member in self._contained(job, skip_live=False):
                    if self._runtime(member).on_noexec:
                        self.store.set_flags(member, on_noexec=False)
                        self._record(member, "OFF_NOEXEC", f"box {job!r} taken OFF_NOEXEC (DL-254)")
        elif kind == "DISARM":  # pragma: no branch -- see below
            # The fall-through arm is unreachable: `_dispatch` passes exactly the seven
            # operator kinds, and each has an arm in this chain.
            # period-model ss10.4 (DL-158): the explicit journaled disarm.
            # The drop is the WHOLE effect: no status move, no wake, no
            # timer. An unarmed target is an accepted, recorded no-op (the
            # OFF_HOLD shape), and the verb is legal at any time, not only
            # before a seal. The marker is the verb's own name -- the OOB
            # convention above -- and deliberately NOT `SCHED_DISARM`, which
            # stays the ENGINE's marker for scheduler-caused drops (the Q3c
            # box fold), so an audit reader can tell the two apart.
            was_armed = self._runtime(job).armed
            self.store.set_armed(job, False)
            reason = "sendevent DISARM" if was_armed else "sendevent DISARM (no latch)"
            self._record(job, "DISARM", reason)

    def _set_noexec(self, job: str, cause: str) -> bool:
        """Set the ON_NOEXEC flag and record it. ON_NOEXEC supersedes ON_HOLD
        (DL-254): "The JOB_ON_NOEXEC event supersedes the JOB_ON_HOLD event
        effectively overwriting the ON_HOLD status with the ON_NOEXEC
        status." The hold is cleared and recorded like an OFF_HOLD. Returns
        True when a hold was cleared: the caller then retries the start, as
        OFF_HOLD does, once its own transitions are done."""
        self.store.set_flags(job, on_noexec=True)
        self._record(job, "ON_NOEXEC", cause)
        if not self._runtime(job).on_hold:
            return False
        self.store.set_flags(job, on_hold=False)
        self._record(job, "OFF_HOLD", "ON_NOEXEC supersedes ON_HOLD (SEM-22, DL-254)")
        return True

    def _noexec_job(self, job: str, job_ir: JobIR | None, status: str) -> None:
        """ON_NOEXEC on a job that is not a box and does not ignore it.

        DL-243 (Job States page): "the scheduler places the job in the
        ON_NOEXEC status and the effect is the same as sending the
        CHANGE_STATUS event to INACTIVE for the job" -- for a job that
        already completed FAILURE/TERMINATED, materialize that through
        DL-242's operator-INACTIVE path (exit code cleared) instead of a
        read-time projection. A queued job takes the same path (DL-254):
        "If the job is in the QUEWAIT or RESWAIT status, the scheduler
        removes the job from the load balancing and resource wait queues
        before placing it in the ON_NOEXEC status." It holds no run's
        reservation; units it holds from an earlier run stay held (DL-256).
        INACTIVE and SUCCESS keep their status.

        A job released from a hold or from the queue retries its start, as
        OFF_HOLD does: the vendor bypasses a NOEXEC job "When the NOEXEC job
        meets its starting conditions", so one whose conditions already
        hold bypasses to SUCCESS now. A queued job had met them; the retry
        runs after the removal, so it does not contradict it. The retry is
        skipped when the event's own wakes already started the job: its run
        number moved, and a second start would be a run with no trigger."""
        run_number = self._runtime(job).run_number
        released = self._set_noexec(job, "sendevent ON_NOEXEC")
        if job_ir is None:
            return
        if status == "QUE_WAIT":
            self.store.dequeue_waiter(job)
            self._inject_inactive(
                job_ir,
                None,
                clear_exit_code=True,
                cause="ON_NOEXEC takes a queued job out of the queue (DL-254)",
            )
            released = True
        elif status in ("FAILURE", "TERMINATED"):
            self._inject_inactive(
                job_ir,
                None,
                clear_exit_code=True,
                cause="ON_NOEXEC settles a completed job to INACTIVE (DL-243)",
            )
        if released:
            self._retry_released(job, run_number)

    def _retry_released(self, job: str, run_number: int) -> None:
        """The OFF_HOLD-style start retry for a job ON_NOEXEC released from a
        hold or the queue (DL-254), unless a wake of the same event already
        started it."""
        if self._runtime(job).run_number == run_number:
            self._attempt_start(job, force=False, scheduled=False, cause="ON_NOEXEC (DL-254)")

    def _noexec_box(self, box_ir: JobIR) -> None:
        """ON_NOEXEC on a box that does not ignore it (DL-254): "the effect is
        the same as sending the CHANGE_STATUS event to INACTIVE for a box.
        The box enters the ON_NOEXEC status and the scheduler sets the status
        of all jobs in the box (including all jobs contained in lower level
        boxes within the box) at all levels to ON_NOEXEC."

        Every flag is set first, then DL-242's box INACTIVE path runs (the
        SEM-18 cascade, one batch). Flags first, so a box its own cascade
        restarts already runs in non-execution mode. `_oob_ignored` has
        already refused a box with a live, queued or iced job inside, so
        the cascade kills nothing. Every row the event covers loses its
        exit code, as DL-243 clears it; a row already INACTIVE by a plain
        store write, as the SEM-10 reset does (no wake: an atom only turns
        false). A box whose whole subtree is already INACTIVE keeps its
        status, as an INACTIVE job does -- unless its parent box is RUNNING:
        then the INACTIVE->INACTIVE transition runs, so the parent records
        the resolution and runs its completion door, as an operator's
        CHANGE_STATUS INACTIVE on a waiting member does (DL-242). Jobs whose
        hold the event cleared retry their start afterwards, as OFF_HOLD
        does, unless the cascade's wakes already started them."""
        box = box_ir.name
        tree = [box, *self._contained(box, skip_live=False)]
        runs = {job: self._runtime(job).run_number for job in tree}
        released = [job for job in tree if self._set_noexec(job, self._noexec_cause(box, job))]
        for job in tree:
            rt = self._runtime(job)
            if rt.status == "INACTIVE" and rt.exit_code is not None:
                self.store.clear_exit_code(job)
        parent = box_ir.box.box_name
        parent_running = parent is not None and self._status(parent) == "RUNNING"
        if parent_running or any(self._status(j) != "INACTIVE" for j in tree):
            self._inject_inactive(
                box_ir,
                None,
                clear_exit_code=True,
                cause="ON_NOEXEC on a box: CHANGE_STATUS INACTIVE (DL-254)",
            )
        for job in released:
            self._retry_released(job, runs[job])

    @staticmethod
    def _noexec_cause(box: str, job: str) -> str:
        return "sendevent ON_NOEXEC" if job == box else f"box {box!r} put ON_NOEXEC (DL-254)"

    @property
    def opening_release_owed(self) -> bool:
        """True while a removed job's held units wait for the period's first
        input (DL-256). The engine admits a time observation for it at
        opening, so the waiters do not wait for an unrelated input."""
        return bool(self._opening_release)

    def _release_removed_holders(self) -> None:
        """DL-256: at the opening of a period, a carried row whose job the
        catalog no longer defines, and that holds units while not live,
        gives them back. A removed or renamed job cannot be addressed, so
        no operator verb could ever release them, and a waiter would wait
        forever. Runs once, inside the period's first input, at the opening
        instant; the waiters then wake in DL-50's order."""
        owed, self._opening_release = self._opening_release, []
        for job in owed:
            freed = self.store.release_held(job)
            units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
            self._record(
                job,
                "RELEASE_RESOURCE",
                f"job removed from the catalog: its held units go back at the period"
                f" opening (frees {units}; DL-256)",
            )
        if owed:
            self._wake_waiters()

    def _release_resource(self, job: str) -> None:
        """RELEASE_RESOURCE (DL-256): "To free the resources, issue the
        following command: sendevent -E RELEASE_RESOURCE -J job_name"
        ("resources Attribute", AutoSys 24.2). Every unit a job that is not
        live still holds goes back to the pool, and the waiters wake in
        DL-50's order. No status moves, so no referencer wakes. The marker is
        the verb's own name, the OOB convention. A job holding nothing, and
        a live job, whose units belong to its run and go back when the run
        ends, are recorded no-ops."""
        rt = self._runtime(job)
        if rt.status in LIVE:
            self._record(
                job,
                "RELEASE_RESOURCE",
                f"sendevent RELEASE_RESOURCE (no effect: {rt.status}; the run's units"
                " go back when it ends)",
            )
            return
        if not rt.reservations:
            self._record(job, "RELEASE_RESOURCE", "sendevent RELEASE_RESOURCE (nothing held)")
            return
        freed = self.store.release_held(job)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(job, "RELEASE_RESOURCE", f"sendevent RELEASE_RESOURCE (frees {units})")
        self._wake_waiters()

    # -------------------------------------------------------- condition evaluation

    def _atom_true(self, atom: StatusAtom | ExitCodeAtom, evaluator: str) -> bool:
        name = atom.job.key
        rt = self.store.job.get(name)
        if rt is None:
            return False  # SEM-06: undefined -> permanently, silently false
        if rt.on_ice:
            # The flag reads the same whatever the stored status: ON_ICE on
            # a STARTING/RUNNING job is ignored (DL-254) and a plain start
            # refuses an iced job, so only an injected STATUS can make an
            # iced row live. The row and its open Q10 split live in
            # `iced_atom_truth`; the oracle passes its switch (DL-252).
            return iced_atom_truth(atom, self.semantics.ice_lookback)
        if isinstance(atom, ExitCodeAtom):
            if rt.exit_code is None or not self._lookback_ok(rt, atom.lookback, evaluator):
                return False
            return compare_int(rt.exit_code, atom.op, atom.value)
        wanted = atom.status
        actual = rt.status
        if wanted == "DONE":
            hit = actual in TERMINAL
        elif wanted == "NOTRUNNING":
            hit = actual not in _N_FALSE_STATUSES
        else:
            hit = actual == wanted
        if not hit:
            return False
        if wanted == "NOTRUNNING" and rt.status_at is None:
            return True  # never-run jobs are notrunning with no timestamp
        return self._lookback_ok(rt, atom.lookback, evaluator)

    def _lookback_ok(self, rt: JobRuntime, lookback: Lookback | None, evaluator: str) -> bool:
        if lookback is None or lookback.kind == "indefinite":
            return True
        if rt.status_at is None:  # pragma: no cover -- see below
            # Unreachable: `store.transition` stamps status_at on every status write.
            # A row never written is INACTIVE with no exit code, which no status atom
            # matches except n(), answered above, and an exit-code atom needs an exit
            # code. Every row that gets here has transitioned.
            return False
        assert self._now is not None
        if lookback.kind == "zero":
            # Q2a (DL-54): the predecessor qualifies iff its own LAST END is
            # at-or-after the evaluating job's last end -- both sides of the
            # cited doc reading are end times ("examines the last end time of
            # the job first. It then examines the last end time of the
            # condition job"), so an n() predecessor bounced to INACTIVE does
            # not read its non-end transition as a fresh run (review MINOR).
            anchor_rt = self.store.job.get(evaluator)
            anchor = None if anchor_rt is None else anchor_rt.last_end_at
            if anchor is None:
                # Q2b RESOLVED (DL-58): a never-ended evaluator has no
                # anchor and the atom is satisfied -- CA support: a newly
                # inserted job "has no initial/previous end time".
                return True
            if rt.last_end_at is None:
                return False  # the predecessor never ended: nothing ran "since"
            return rt.last_end_at >= anchor
        assert lookback.minutes is not None
        return rt.status_at >= self._now - timedelta(minutes=lookback.minutes)

    def _cond_true(self, cond: Cond, evaluator: str) -> bool:
        if isinstance(cond, And):
            return all(self._cond_true(op, evaluator) for op in cond.operands)
        if isinstance(cond, Or):
            return any(self._cond_true(op, evaluator) for op in cond.operands)
        if isinstance(cond, Paren):
            return self._cond_true(cond.inner, evaluator)
        if isinstance(cond, GlobalAtom):
            actual = self.store.global_value(cond.name)
            if actual is None:
                return False
            return compare_value(actual, cond.op, cond.value)
        return self._atom_true(cond, evaluator)

    # --------------------------------------------------------------- job starting

    def _attempt_start(self, job: str, *, force: bool, scheduled: bool, cause: str) -> str | None:
        """Returns a refusal reason for the SEM-10 gates, None otherwise.

        Only the explicit-event dispatch path surfaces that reason as a
        START_REFUSED trace record: internal callers (condition edges, box
        starts, OFF_HOLD sweeps) probe members of non-running boxes on every
        wake, where silence is correct -- but an operator's STARTJOB dying
        without any visible acknowledgement proved untrainable (the vendor
        is equally silent; the trace record is our one deliberate visibility
        addition, DL-64)."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None:
            return None  # starting an undefined job is a no-op for the oracle
        rt = self._runtime(job)
        if rt.status in ("STARTING", "RUNNING", "QUE_WAIT"):
            # already starting/running, or queued for resources (DL-50); a tick
            # on a live job does not arm (Q3 pin). DL-81: this branch used to
            # return a bare None, so an explicit STARTJOB against a live job was
            # the one refusal that left NO record anywhere -- two operators
            # racing a start both got ok, one silently did nothing, and the
            # trace showed a single start with no sign the second was ever
            # attempted. Only the explicit-event path surfaces this (the three
            # internal probe callers discard the return), so box sweeps and
            # condition edges stay silent exactly as before.
            return f"already {rt.status} -- concurrent or repeated start request, no effect"
        if rt.on_ice:
            if not force:
                return None  # SEM-20: iced jobs never run on a plain start;
                # never arms: conditions must REOCCUR after OFF_ICE
            # SEM-23/DL-243: FORCE_STARTJOB on a non-live iced job is the
            # vendor's "returns to an executable state" case (sendevent Start
            # Jobs page) -- clear the flag the same way an OFF_ICE would,
            # with a cause naming the force, then fall through to start.
            self.store.set_flags(job, on_ice=False)
            self._record(job, "OFF_ICE", f"FORCE_STARTJOB clears ON_ICE (SEM-23, DL-243; {cause})")
            rt = self._runtime(job)
        if rt.on_hold and not force:
            # SEM-21: held jobs do not start; a scheduled tick latches so the
            # missed run collapses to at most one on OFF_HOLD (Q3, DL-54)
            self._arm(job_ir, rt, scheduled, "blocked ON_HOLD")
            return None
        if rt.on_hold and force:
            # SEM-23/DL-243: same FORCE_STARTJOB rule for ON_HOLD -- the
            # vendor groups ON_HOLD with ON_ICE as "non-executable" states.
            self.store.set_flags(job, on_hold=False)
            self._record(
                job, "OFF_HOLD", f"FORCE_STARTJOB clears ON_HOLD (SEM-23, DL-243; {cause})"
            )
            rt = self._runtime(job)
        if not force:
            if job_ir.schedule is not None and not scheduled and not rt.armed:
                # SEM-30/31 (DL-13): a date_conditions job -- standalone OR
                # box member (the L013 double gate) -- starts only on its
                # script-injected schedule tick, or while a prior tick's arm
                # is latched (Q3, DL-54) -- never on bare condition edges.
                return None
            box = job_ir.box.box_name
            if box is not None:
                if self._runtime(box).status != "RUNNING":
                    # SEM-10: member needs its box RUNNING; member ticks only
                    # count while the box runs -- no arm (Q3 pin)
                    return f"box {box!r} is not RUNNING -- rerun needs FORCE_STARTJOB (SEM-10)"
                if job in self._runtime(box).ran_members:
                    # SEM-10: at most once per box execution
                    return (
                        f"already ran in this {box!r} execution -- "
                        "rerun needs FORCE_STARTJOB (SEM-10)"
                    )
            gate = job_ir.sem.condition
            if gate is not None and not self._cond_true(gate.cond, job):
                # SEM-32 arm-and-wait (Q3 resolved, DL-58): the tick latches;
                # condition edges may start the job later.
                self._arm(job_ir, rt, scheduled, "condition false")
                return None
        if not self._run_window_permits(job_ir, cause):
            return None
        self._start(job, cause, force=force)
        return None

    def _arm(self, job_ir: JobIR, rt: JobRuntime, scheduled: bool, why: str) -> None:
        """Q3 (DL-54, resolved by citation DL-58 -- the abandon switch is
        deleted per the DL-06 protocol): a scheduled tick blocked at a
        releasable gate latches until a start consumes it. Only
        schedule-bearing jobs arm -- the flag is read solely by the schedule
        gate. A member arms only while its box is RUNNING (review MAJOR: the
        hold gate precedes the box gate, so the box state must be re-checked
        here), and the arm dies with that box run (_after_transition)."""
        if not scheduled or job_ir.schedule is None or rt.armed:
            return
        box = job_ir.box.box_name
        if box is not None and self._runtime(box).status != "RUNNING":
            return  # member ticks only count while the box runs (Q3 pin)
        self.store.set_armed(job_ir.name, True)
        self._record(job_ir.name, "SCHED_ARM", f"scheduled tick {why}; armed (SEM-32, DL-54/58)")

    def _job_tz(self, job_ir: JobIR) -> tzinfo | None:
        """SEM-35: the zone this job's time attributes are read in, resolved
        through the runner's own ladder (zoneinfo, POSIX fixed offsets, the
        DL-62 unique-city default). A job that declares no `timezone:` reads
        the constructor's `default_tz`, resolved through the same ladder and
        alias table (DL-151/DL-155). None means the engine clock IS the
        comparison basis -- the vendor's scheduler-zone rule (TechDocs
        12.0.01, timezone attribute) with the simulation's frame as the
        scheduler's zone."""
        schedule = job_ir.schedule
        name = schedule.timezone if schedule is not None else None
        if name is None:
            name = self._default_tz
        if name is None:
            return None
        if name not in self._tz_cache:
            resolved = resolve_timezone(name, self._tz_aliases)
            if resolved is None:
                raise OracleError(
                    f"{job_ir.name}: timezone {name!r} is not resolvable (SEM-35: a zoneinfo"
                    " name, a POSIX fixed offset, or a ujo_timezones entry supplied to the"
                    " runner as --timezone-map)"
                )
            self._tz_cache[name] = resolved.tz
        return self._tz_cache[name]

    def _run_window_permits(self, job_ir: JobIR, cause: str) -> bool:
        """SEM-33 closer-edge rule; True == start may proceed now. The window
        is read in the job's own timezone (SEM-35 re-bases every time
        attribute of that job), so the comparison happens on local wall time
        while the timer it queues goes back on the engine clock. Near a
        documented DST change it happens on the window's engine instants
        instead (DL-249, `_window_span`)."""
        side, next_open = self._window_side(job_ir)
        if side == "inside":
            return True
        if side == "defer":
            assert next_open is not None
            self._defer_start(job_ir, next_open, cause)
        else:
            self._record(
                job_ir.name,
                "RUN_WINDOW_SKIP",
                f"outside run_window; closer to previous close -- not run ({cause})",
            )
            self._window_skip_bypass(job_ir)
        return False

    def _window_side(self, job_ir: JobIR) -> tuple[str, datetime | None]:
        """Which side of the SEM-33 closer-edge rule `now` falls on, with no
        effect: "inside", "defer" (with the next opening on the engine
        clock) or "skip"."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return "inside", None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        now_local = to_local(self._now, tz)
        lo, hi = schedule.run_window
        lo_t = _to_time(lo)
        hi_t = _to_time(hi)
        today = now_local.date()
        if dst_change_near(today, tz):
            # DL-249: near a documented DST change the window is a set of
            # concrete intervals whose endpoints follow the vendor's rules.
            # No zone has such a change within days of the calendar's ends,
            # so the four opening days stay inside the date range.
            spans = [_window_span(today + timedelta(days=k), lo_t, hi_t, tz) for k in range(-2, 2)]
            if any(opens <= self._now <= closes for opens, closes in spans):
                return "inside", None
            next_open = min(opens for opens, _ in spans if opens > self._now)
            prev_close = max(closes for _, closes in spans if closes < self._now)
        else:
            now_t = now_local.time()
            if lo_t <= hi_t:
                inside = lo_t <= now_t <= hi_t
            else:  # window crosses midnight
                inside = now_t >= lo_t or now_t <= hi_t
            if inside:
                return "inside", None
            next_open = to_utc(_next_occurrence(now_local, lo_t), tz)
            prev_close = to_utc(_prev_occurrence(now_local, hi_t), tz)
        # both distances are measured on the ENGINE clock: a DST shift inside
        # the gap makes the two wall-clock distances lie about elapsed time
        to_open = next_open - self._now
        since_close = self._now - prev_close
        if to_open <= since_close:  # [?] midpoint tie -> next opening
            return "defer", next_open
        return "skip", None

    def _defer_start(
        self,
        job_ir: JobIR,
        next_open: datetime,
        cause: str,
        *,
        rescan_run: int | None = None,
        note: str = "outside run_window; closer to next opening -- STARTJOB queued",
    ) -> None:
        """Queue the SEM-33 deferred STARTJOB at the next opening. A member's
        deferral carries its box's run number, so a box restart makes it
        stale (DL-246)."""
        payload: dict[str, object] = {"job": job_ir.name, DEFERRED_TIMER_KEY: cause}
        if rescan_run is not None:
            payload[DEFERRED_RESCAN_KEY] = rescan_run
        box = job_ir.box.box_name
        if box is not None:
            payload[DEFERRED_BOX_KEY] = box
            payload[DEFERRED_BOX_RUN_KEY] = self._runtime(box).run_number
        # DL-54 review MINOR: an armed job can reach this branch on every
        # condition edge -- one pending defer per (job, opening) instant, not
        # one per attempt (duplicate timers spammed pending_timers()). Only a
        # deferred start of the same box run counts: a deadline timer at the
        # same instant is not one (DL-246).
        pending = any(
            due == next_open
            and e.kind == "TIMER"
            and DEFERRED_TIMER_KEY in e.payload
            and e.payload.get("job") == job_ir.name
            and e.payload.get(DEFERRED_BOX_KEY) == payload.get(DEFERRED_BOX_KEY)
            and e.payload.get(DEFERRED_BOX_RUN_KEY) == payload.get(DEFERRED_BOX_RUN_KEY)
            and e.payload.get(DEFERRED_RESCAN_KEY) == payload.get(DEFERRED_RESCAN_KEY)
            for due, _, e in self.store.timers()
        )
        if pending:
            return
        self._schedule_timer(next_open, Event(at=next_open, kind="TIMER", payload=payload))
        self._record(job_ir.name, "RUN_WINDOW_DEFER", f"{note} ({cause})")

    def _deferral_is_stale(self, ev: Event) -> str | None:
        """The refusal reason for a deferred start that no longer belongs to
        its job's box run (DL-246), None otherwise: the box has started
        again since the deferral, or a rebaseline moved the job into another
        box or out of one. The current box run decides the job afresh."""
        job = ev.payload.get("job")
        if not isinstance(job, str):  # pragma: no cover -- see below
            # Unreachable: both callers hold a payload job that is already a str.
            # `pending_timers` filters on it before it asks, and no armed timer
            # lacks one (see there); the dispatch reads the event through
            # `_required_job` first.
            return None
        job_ir = self.catalog.jobs.get(job)
        current = job_ir.box.box_name if job_ir is not None else None
        origin = ev.payload.get(DEFERRED_BOX_KEY)
        if origin != current:
            return (
                f"deferred start was queued in box {origin!r}, and the job is now in box"
                f" {current!r} -- no effect (SEM-33, DL-246)"
            )
        box_run = ev.payload.get(DEFERRED_BOX_RUN_KEY)
        if current is None or self._runtime(current).run_number == box_run:
            return None
        return (
            f"deferred start belongs to box {current!r} run {box_run}, and the box has started"
            " again since -- no effect (SEM-33, DL-246)"
        )

    def _window_skip_bypass(self, job_ir: JobIR) -> None:
        """SEM-33/DL-154: a run_window skip during a live box run is a
        bypass, the ON_ICE shape. TechDocs 12.1, run_window page: "the
        job's status changes to INACTIVE. The box job can still run to
        completion." The member goes INACTIVE, stays OUT of the ran set
        (it casts no vote in the SEM-11 fold), and the box runs through
        the FULL completion door at once -- the skip resolution is a
        completion moment, so a satisfied override fires first and the
        default fold runs only if none did; a specified-but-unmet
        override still hangs the box (SEM-12's own rule, composed with
        the vendor's INACTIVE verdict). A mid-run condition edge -- or a
        FORCE_STARTJOB, which does not override run_window (SEM-23) --
        that lands on the skip branch bypasses identically: the
        closer-edge rule applies at the attempt's own moment. A box start
        decides the same skip for a member outside its window (DL-246).

        For a standalone job the product "does not start the job and
        changes its status to INACTIVE" (same page): a prior SUCCESS,
        FAILURE or TERMINATED moves to INACTIVE and wakes referencers, as
        an injected INACTIVE does, and the exit code stays, as it does
        there (DL-246). A job already INACTIVE gets no transition. Members
        of non-RUNNING boxes keep the plain skip; a member that already ran
        this execution keeps its real result -- a forced re-attempt's skip
        must not un-run it."""
        box = job_ir.box.box_name
        if box is None:
            if self._runtime(job_ir.name).status != "INACTIVE":
                self._set_status(
                    job_ir.name,
                    "INACTIVE",
                    cause="run_window skip: closer to previous close (SEM-33, DL-246)",
                )
            return
        box_rt = self._runtime(box)
        if box_rt.status != "RUNNING" or job_ir.name in box_rt.ran_members:
            return
        self.store.record_resolution(box, job_ir.name)
        cause = f"run_window skip bypasses inside box {box!r} (SEM-33, DL-154)"
        if self._runtime(job_ir.name).status != "INACTIVE":
            # the transition runs the completion door itself: the mark is
            # already visible, so _on_member_transition treats this edge
            # as the member's resolution moment
            self._set_status(job_ir.name, "INACTIVE", cause=cause)
            return
        # already INACTIVE: no transition to ride -- run the same door here,
        # then the ancestors' transitive overrides, as a resolved INACTIVE
        # transition would (SEM-12, DL-242)
        self._completion_door(box, job_ir.name, "INACTIVE", completion_moment=True)
        self._on_descendant_transition(
            job_ir.name,
            "INACTIVE",
            resolved=self._resolves(box, job_ir.name, "INACTIVE", "INACTIVE"),
        )

    def _ancestor_boxes(self, job: str) -> list[str]:
        """Containing boxes, innermost first (SEM-17). Lowering rejects
        containment cycles; the seen-set keeps a hand-built IR finite."""
        chain: list[str] = []
        seen = {job}
        job_ir = self.catalog.jobs.get(job)
        box = job_ir.box.box_name if job_ir is not None else None
        while box is not None and box not in seen:
            chain.append(box)
            seen.add(box)
            box_ir = self.catalog.jobs.get(box)
            box = box_ir.box.box_name if box_ir is not None else None
        return chain

    def _noexec_bypasses(self, job_ir: JobIR) -> bool:
        """SEM-22: True when this start bypasses to SUCCESS instead of
        running. A job bypasses on its own ON_NOEXEC flag, and a member also
        bypasses while a box that contains it is ON_NOEXEC. No vendor text
        states that inheritance (SEM-22 [?]). DL-254's cascade flags every
        job in the tree when a box is put ON_NOEXEC, so the inheritance
        decides only corners where a member lacks the flag under a flagged
        box: a box flagged at definition time, whose members do not take
        the flag (SEM-24); a member taken OFF_NOEXEC alone under a flagged
        box; and a job that a later period's catalog adds to, or moves
        under, a flagged box. A BOX never bypasses: an
        ON_NOEXEC box "goes RUNNING, members are bypassed to SUCCESS as
        their conditions are met", so the rule is applied once per box
        level and a member box walks its own members too."""
        if job_ir.job_type == "BOX":
            return False
        if self._runtime(job_ir.name).on_noexec:
            return True
        return any(self._runtime(box).on_noexec for box in self._ancestor_boxes(job_ir.name))

    def _start(self, job: str, cause: str, *, force: bool = False) -> None:
        job_ir = self.catalog.jobs[job]
        if self._noexec_bypasses(job_ir):
            # SEM-22: lifecycle bypass -- straight to SUCCESS, downstream normal.
            # A bypassed job never runs, so it acquires no resources (DL-50).
            # The bypass still COUNTS as this box run's start for the job: it
            # joins the box's ran set, so the SEM-11 fold waits for every
            # member's bypass and the SEM-10 once-per-run gate keeps a pair of
            # mutually-referencing members from bypassing each other forever.
            self.store.start_run(  # Q3: the bypass IS the tick's run (DL-54)
                job,
                cause=f"ON_NOEXEC bypass ({cause})",
                box=job_ir.box.box_name,
                is_box=False,
            )
            self._set_status(job, "SUCCESS", cause=f"ON_NOEXEC bypass ({cause})")
            return
        # DL-50: atomic admission before RUNNING. Empty demand -> straight to
        # RUNNING, byte-identical to the pre-resource oracle (bisim + the whole
        # existing corpus are untouched: no buckets, no waiters, same cause),
        # unless a higher-priority waiter blocks it (DL-247, DL-255).
        vector = self._pool.demand_vector(job_ir)
        rt = self._runtime(job)
        if force and rt.reservations and rt.status in ("FAILURE", "TERMINATED"):
            self._start_on_held(job_ir, vector, cause)
        elif not self._admissible(job_ir, vector, force=force):
            self._enqueue_waiter(job, cause)
        else:
            # DL-120: the vector is FROZEN onto the row here. The terminal
            # transition releases what this run took, never what the catalog
            # says the job wants by then (PR-20).
            freed = self._acquire(job, vector)
            self._run(job_ir, cause, had_demand=bool(vector))
            if freed:
                self._wake_waiters()
        if takes_machine_load(vector):
            # DL-255: the held load, or the new load waiter, can send a
            # resource waiter on this machine back to its load check; a
            # forced start on held units holds its load too (DL-256)
            self._scan_owed = True

    def _acquire(self, job: str, vector: list[DemandEntry]) -> bool:
        """Freeze an admitted start's vector onto its row (DL-120). A job that
        still holds a renewable's units from an earlier run starts on them
        (DL-256): admission credited them to it (`_admissible`), so the new
        vector replaces them and nothing is counted twice. True when the
        held units exceed the new demand on some bucket -- the catalog
        lowered a QUANTITY, or dropped the resource -- so the caller owes
        the queue a wake for the difference."""
        held = self._runtime(job).reservations
        reservations = to_reservations(vector)
        if not held:
            self.store.reserve(job, reservations)
            return False
        self.store.take_over_held(job, reservations)
        demand: dict[str, int] = {}
        for reservation in reservations:
            demand[reservation.bucket] = demand.get(reservation.bucket, 0) + reservation.units
        return any(h.units > demand.get(h.bucket, 0) for h in held)

    def _start_on_held(self, job_ir: JobIR, vector: list[DemandEntry], cause: str) -> None:
        """DL-256: a FORCE_STARTJOB of a FAILURE or TERMINATED job that still
        holds units. "When you force start a job in FAILURE or TERMINATED
        status that has a virtual resource dependency with free=Y or free=N
        and has not released the virtual resources, the FORCE_STARTJOB event
        ... schedules the job using the held virtual resources. Before force
        starting the job, the scheduler does not re-evaluate other resource
        dependencies." ("Define Virtual Resource Types", AutoSys 24.2.)

        So no bucket is checked. The run holds the units it held, frozen as
        they were taken, and its machine load, which every start holds
        (DL-247). A resource it does not hold is neither checked nor taken."""
        job = job_ir.name
        held = self._runtime(job).reservations
        load = to_reservations(machine_load(vector))
        self.store.take_over_held(job, held + load)
        self._run(
            job_ir,
            f"{cause}; starts on its held units, other resources not re-evaluated (DL-256)",
            had_demand=True,
        )

    def _run(self, job_ir: JobIR, cause: str, *, had_demand: bool) -> None:
        """Start tail once admission has passed: run_number bump, box
        bookkeeping, STARTING -> RUNNING, box member launch (SEM-10). A box
        resets its contained jobs around its STARTING transition (DL-242):
        the rows are written before it, so its wakes read the new cycle,
        and their notifications run after it, while the box is STARTING."""
        job = job_ir.name
        self._arm_term_run_time(job_ir)  # reads run_number before the bump
        # one act: the arm this start consumes (Q3/DL-54 -- the ACTUAL start
        # consumes it, FORCE included; a QUE_WAIT enqueue keeps it latched, so
        # a cancelled queue attempt does not eat the tick), the run_number
        # bump, the DL-68 provenance of THIS run, and the SEM-10 box sets
        self.store.start_run(
            job,
            cause=cause,
            box=job_ir.box.box_name,
            is_box=job_ir.job_type == "BOX",
        )
        run_number = self._runtime(job).run_number

        def starting() -> None:
            self._set_status(job, "STARTING", cause=cause)

        if job_ir.job_type == "BOX":
            self._reset_box_cycle(job, starting)
        else:
            starting()
        rt = self._runtime(job)
        if rt.status != "STARTING" or rt.run_number != run_number:
            # a wake of the STARTING transition (or of the box-start reset)
            # ended this run -- a job_terminator cascade TERMINATES a
            # STARTING member -- so RUNNING must not overwrite that verdict
            return
        running_cause = (
            "admitted: resources acquired (DL-50)"
            if had_demand
            else "QUE_WAIT collapses to immediate (ss7 non-goal)"
        )
        if job_ir.job_type != "BOX":
            self._set_status(job, "RUNNING", cause=running_cause)
            return
        # SEM-33 (DL-246): the window decisions of every box this start
        # begins -- its own, and any subbox started by RUNNING's wakes or by a
        # member attempt -- wait for one pass after all of their attempts
        outermost = self._window_starts is None
        if outermost:
            self._window_starts = []
        try:
            self._set_status(job, "RUNNING", cause=running_cause)
            self._on_box_started(job, run_number)
            starts = self._window_starts
        finally:
            if outermost:
                self._window_starts = None
        if outermost:
            assert starts is not None
            self._decide_windows_at_box_start(starts)

    # ---------------------------------------------------------- resources (DL-50)

    def _admissible(self, job_ir: JobIR, vector: list[DemandEntry], *, force: bool) -> bool:
        """The admission test of a start, fresh or out of the queue (DL-247).
        A job with a positive priority must not be blocked by a
        higher-priority waiter on a named resource it names (DL-255); force
        does not lift that block, since the vendor's force rule is a load
        rule. A FORCE_STARTJOB then checks the named resources only: the
        forced job "runs even if its load exceeds the machine's max_load
        value", and so no load waiter blocks it either. A job whose priority
        is unset or 0 does the same: the scheduler "ignores any load unit
        values" for it. Both still hold their load. Otherwise the job must
        not be blocked by a higher-priority load waiter on its machine, and
        every bucket must fit. Force lives on the event, not the row: a
        forced job that queues on a named resource is readmitted like any
        other."""
        rows, consumed = self.store.job, self.store.consumed
        # DL-256: a job's own held units are available to it, and only to it
        own = job_ir.name
        if self._pool.resource_blocked(job_ir, rows, consumed, self._counts_as_waiter):
            return False
        if force or not checks_load(job_ir):
            return self._pool.can_admit(without_machine_load(vector), rows, consumed, own=own)
        if self._pool.load_blocked(job_ir, rows, consumed, self._counts_as_waiter):
            return False
        return self._pool.can_admit(vector, rows, consumed, own=own)

    def _counts_as_waiter(self, waiter: str) -> bool:
        """A queued job that counts toward priority blocking (DL-247,
        DL-255): not held, and not a member whose box has stopped running.
        The readmission scan cancels the latter and keeps the former queued
        without trying it, so neither is waiting for load or a resource."""
        rt = self._runtime(waiter)
        job_ir = self.catalog.jobs.get(waiter)
        if rt.on_hold or job_ir is None:
            return False
        box = job_ir.box.box_name
        return box is None or self._runtime(box).status == "RUNNING"

    def _enqueue_waiter(self, job: str, cause: str) -> None:
        self.store.enqueue_waiter(job)
        self._set_status(job, "QUE_WAIT", cause=f"waiting for resources ({cause})")

    def _wake_waiters(self, *, keep_stopped: bool = False) -> None:
        """Admit queued jobs whose full vector now fits, in deterministic order,
        to a fixpoint. Re-entrancy-guarded: a nested call (a release inside an
        admitted job's cascade) defers to the outer loop's next scan.
        `keep_stopped` leaves a member of a stopped box queued instead of
        cancelling it (the owed admit-only scan of DL-247 and DL-255). A full scan requested while
        an admit-only one runs turns its next pass into a full one, so a
        release inside the scan still cancels what a release cancels.

        Every pass pays an owed scan, since every pass admits."""
        if self._in_wake:
            if not keep_stopped:  # pragma: no branch -- see below
                # The arm with keep_stopped True is unreachable: its one caller,
                # `_run_owed_scan`, loops under `not self._in_wake`, so it never runs
                # nested. Every nested call comes from a release, with keep_stopped False.
                self._full_scan_requested = True
            return
        self._in_wake = True
        try:
            while True:
                self._scan_owed = False
                self._full_scan_requested = False
                moved = any(
                    self._readmit(job, keep_stopped=keep_stopped) in ("admitted", "cancelled")
                    for job in self._pool.sorted_waiters(self.store.job)
                )
                if self._full_scan_requested and keep_stopped:
                    keep_stopped = False
                    continue
                if not moved:
                    break  # queue unchanged; sorted_waiters is re-read each pass
        finally:
            self._in_wake = False

    def _readmit(self, job: str, *, keep_stopped: bool = False) -> str:
        """One admission attempt for a queued job. Re-validates the guards that
        can change while queued (ice, box-RUNNING, hold) before the capacity
        check. The capacity check is a fresh start's, so a higher-priority
        load waiter on the same machine keeps this job queued even when its
        load fits (DL-247), and so does a higher-priority waiter on a named
        resource it names (DL-255). A job that fits is leaving the queue; at
        the default `queued-recheck=0` its conditions are NOT re-checked
        (Qr6, decided), and otherwise `_queued_recheck` decides (DL-257)."""
        rt = self._runtime(job)
        job_ir = self.catalog.jobs[job]
        if rt.on_ice:  # pragma: no cover -- see below
            # Unreachable: a job never waits while iced. A plain start of an iced job
            # is refused before it can queue, a FORCE_STARTJOB clears the flag first
            # (`_attempt_start`), and ON_ICE on a QUE_WAIT job dequeues it at once
            # (`_handle_oob`). The flag is set nowhere else, so a waiter with it set
            # would mean one of those three had changed.
            return self._cancel_waiter(job, "iced while queued")
        box = job_ir.box.box_name
        if box is not None and self._runtime(box).status != "RUNNING":
            if keep_stopped:
                return "waiting"
            return self._cancel_waiter(job, f"box {box!r} no longer RUNNING")
        if rt.on_hold:
            return "held"  # stays queued; OFF_HOLD re-scans
        vector = self._pool.demand_vector(job_ir)
        if not self._admissible(job_ir, vector, force=False):
            return "waiting"
        failed = self._queued_recheck(job_ir)
        if failed is not None:
            return self._leave_queue_unstarted(job_ir, *failed)
        self.store.dequeue_waiter(job)
        # a held excess freed here is seen by the running scan's next pass
        self._acquire(job, vector)
        self._run(job_ir, cause="resources freed (QUE_WAIT admitted, DL-50)", had_demand=True)
        return "admitted"

    def _queued_recheck(self, job_ir: JobIR) -> tuple[str, str] | None:
        """Why a job leaving QUE_WAIT must not start, as (check, reason), or
        None (DL-257). `check` is "day", "window" or "condition". The
        `queued-recheck` switch is the vendor's EvaluateQueuedJobStarts: 0
        re-checks nothing; 1 re-checks the starting conditions "other than
        the date condition check for the day of evaluation" -- run_calendar,
        days_of_week, start_times and start_mins are not re-read, but a day
        in the exclusion calendar and a time outside the run window still
        stop the start; 2 also checks that today is a run day. The day is
        the job's local day, in the zone its run_window is read in."""
        mode = self.semantics.queued_recheck
        if mode == "0":
            return None
        schedule = job_ir.schedule
        if schedule is not None:
            assert self._now is not None
            today = to_local(self._now, self._job_tz(job_ir)).date()
            excluded = schedule.exclude_calendar
            if excluded is not None and self._calendar_has(job_ir, excluded, today):
                return "day", f"{today} is in exclude_calendar {excluded!r}"
            if mode == "2" and not self._is_run_day(job_ir, today):
                return "day", f"{today} is not a run day"
            if self._window_side(job_ir)[0] != "inside":
                return "window", "outside run_window"
        gate = job_ir.sem.condition
        if gate is not None and not self._cond_true(gate.cond, job_ir.name):
            return "condition", "condition false"
        return None

    def _is_run_day(self, job_ir: JobIR, day: date) -> bool:
        """`queued-recheck=2`'s date check for one day: run_calendar XOR
        days_of_week (SEM-31), with an absent days_of_week read as every
        day, as the scheduler reads it. The exclusion is checked apart."""
        schedule = job_ir.schedule
        assert schedule is not None
        if schedule.run_calendar is not None:
            return self._calendar_has(job_ir, schedule.run_calendar, day)
        days = schedule.days_of_week
        return days is None or "all" in days or _WEEKDAY_TOKENS[day.weekday()] in days

    def _calendar_has(self, job_ir: JobIR, name: str, day: date) -> bool:
        """Whether calendar `name` holds `day`. The scheduler owns ticks; this
        is the one calendar question the oracle asks, so a replay with no
        scheduler reads the same days (DL-257). Preflight refuses a missing
        or uninterpretable calendar before a run; a direct caller that
        skipped it gets an OracleError, never a guess."""
        try:
            days = self._calendar(job_ir, name)
            if isinstance(days, CompiledCalendar):
                return day in days.days_between(day, day)
            return day in days
        except CalendarRuleError as exc:
            raise OracleError(f"{job_ir.name}: {exc}") from exc

    def _calendar(self, job_ir: JobIR, name: str) -> frozenset[date] | CompiledCalendar:
        """Calendar `name`'s days, resolved once: a standard calendar's day
        set, or a compiled extended one. Raises CalendarRuleError on a rule
        it cannot interpret, OracleError on a missing definition."""
        days = self._calendars.get(name)
        if days is None:
            cal = self.catalog.calendars.get(name)
            if cal is None:
                raise OracleError(
                    f"{job_ir.name}: calendar {name!r} has no definition in the loaded set"
                )
            days = (
                compile_calendar(cal, self.catalog, self.semantics)
                if cal.kind == "extended"
                else standard_days(cal)
            )
            self._calendars[name] = days
        return days

    def _leave_queue_unstarted(self, job_ir: JobIR, check: str, why: str) -> str:
        """A job that failed `_queued_recheck` leaves QUE_WAIT for INACTIVE
        without starting (DL-257). Its arm goes first, before any wake can
        ride it: a job with date conditions "re-schedules ... to its next
        start time", so only its next tick starts it. A member of a running
        box is not resolved: it waits, as the vendor's ACTIVATED does, and
        keeps the box RUNNING (SEM-11, DL-242).

        Two failures would otherwise wait for a start that never comes, since
        the scheduler never ticks a job with no start times of its own and a
        box starts its members unscheduled. A run_window failure takes
        DL-246's disposition at this instant: nearer the previous close, the
        SEM-33 skip, which resolves a member of a running box; nearer the
        next opening, one deferred start at it. A day failure of a job with
        no ticks of its own is deferred to the window opening of its next
        eligible day (`_next_eligible_opening`)."""
        job = job_ir.name
        mode = self.semantics.queued_recheck
        if self._runtime(job).armed:
            self.store.set_armed(job, False)
            self._record(
                job,
                "SCHED_DISARM",
                f"left QUE_WAIT unstarted; waits for its next start time"
                f" (queued-recheck={mode}, DL-257)",
            )
        self.store.dequeue_waiter(job)
        cause = f"QUE_WAIT left unstarted: {why} (queued-recheck={mode}, DL-257)"
        box = job_ir.box.box_name
        box_run = self._runtime(box).run_number if box is not None else None
        if box is not None:
            # this attempt ends unresolved: an earlier verdict of the same box
            # run must not complete the box past it
            self.store.void_resolution(box, job)
        run_number = self._runtime(job).run_number
        self._set_status(job, "INACTIVE", cause=cause)
        rt = self._runtime(job)
        if rt.status != "INACTIVE" or rt.run_number != run_number:
            return "cancelled"  # a wake of the transition already moved it on
        if box is not None:
            box_rt = self._runtime(box)
            if box_rt.status != "RUNNING" or box_rt.run_number != box_run:
                # the box run this decision belongs to ended in the wakes; a
                # later run decides the member afresh (DL-246)
                return "cancelled"
        if check == "window":
            self._run_window_permits(job_ir, cause)
        elif check == "day" and not self._has_ticks(job_ir):
            self._defer_to_next_opening(job_ir, cause)
        return "cancelled"

    @staticmethod
    def _has_ticks(job_ir: JobIR) -> bool:
        """Whether the scheduler ticks this job: start_times, start_mins, or a
        run_calendar's own row times (E11, DL-58)."""
        schedule = job_ir.schedule
        return schedule is not None and bool(
            schedule.start_times or schedule.start_mins or schedule.run_calendar
        )

    def _defer_to_next_opening(self, job_ir: JobIR, cause: str) -> None:
        """Defer a tickless job to the run_window opening of its next eligible
        day: the first day after today that is not excluded and is a run day
        by run_calendar or days_of_week (DL-257). The scan covers
        `_OPENING_SCAN_DAYS` days; when none is eligible, one continuation
        timer is armed at local midnight of the last scanned day, and the
        scan resumes there, so a rare eligible day (a 29 February) is still
        found. A job with no run_window has no opening and is not deferred."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return
        assert self._now is not None
        tz = self._job_tz(job_ir)
        today = to_local(self._now, tz).date()
        first, last = today + timedelta(days=1), today + timedelta(days=_OPENING_SCAN_DAYS)
        excluded = self._calendar_days(job_ir, schedule.exclude_calendar, first, last)
        runs = self._calendar_days(job_ir, schedule.run_calendar, first, last)
        lo, hi = (_to_time(t) for t in schedule.run_window)
        day = first
        while day <= last:
            eligible = (excluded is None or day not in excluded) and (
                day in runs if runs is not None else self._is_run_day(job_ir, day)
            )
            if eligible:
                self._defer_start(job_ir, _window_span(day, lo, hi, tz)[0], cause)
                return
            day += timedelta(days=1)
        self._defer_start(
            job_ir,
            to_utc(datetime.combine(last, dtime()), tz),
            cause,
            rescan_run=self._runtime(job_ir.name).run_number,
            note=f"no eligible day through {last}; the scan for one resumes then",
        )

    def _rescan_superseded(self, job: str, run: object) -> bool:
        """Whether a scan continuation armed at the job's run number `run` is
        permanently dead: the job has started since (DL-257). One test for
        `pending_timers` and the firing, so the two cannot disagree. A
        continuation at the same run number stays live whatever the status,
        which can return to INACTIVE before it fires."""
        return self._runtime(job).run_number != run

    def _resume_opening_scan(self, job: str, run: object, cause: str) -> None:
        """A continuation timer of `_defer_to_next_opening` fired: scan on
        from here, unless the job has started since or is not INACTIVE now."""
        if self._rescan_superseded(job, run) or self._runtime(job).status != "INACTIVE":
            self._record(
                job,
                "START_REFUSED",
                f"the job has moved on since the scan stopped -- no effect (DL-257; {cause})",
            )
            return
        self._defer_to_next_opening(self.catalog.jobs[job], cause)

    def _calendar_days(
        self, job_ir: JobIR, name: str | None, first: date, last: date
    ) -> frozenset[date] | None:
        """The days of calendar `name` within [first, last], generated once
        for the range (DL-257); None when there is no calendar."""
        if name is None:
            return None
        try:
            days = self._calendar(job_ir, name)
            if isinstance(days, CompiledCalendar):
                return days.days_between(first, last)
        except CalendarRuleError as exc:
            raise OracleError(f"{job_ir.name}: {exc}") from exc
        return frozenset(d for d in days if first <= d <= last)

    def _cancel_waiter(self, job: str, why: str) -> str:
        self.store.dequeue_waiter(job)
        self._set_status(job, "INACTIVE", cause=f"QUE_WAIT cancelled: {why} (DL-50)")
        return "cancelled"

    def _contained(self, box: str, *, skip_live: bool) -> list[str]:
        """Every job `box` contains, transitively, top-down: a subbox comes
        before its own members. With `skip_live`, a job that is STARTING,
        RUNNING or QUE_WAIT is left out, and so is everything inside a live
        subbox. Lowering rejects containment cycles; the seen-set keeps a
        hand-built IR finite."""
        out: list[str] = []
        seen = {box}
        pending = deque([box])
        while pending:
            for member in self._members(pending.popleft()):
                if member in seen:  # pragma: no cover -- see below
                    # Unreachable: the CatalogIR validator refuses a containment
                    # cycle, and each job has one box, so no member repeats.
                    continue
                seen.add(member)
                if skip_live and self._runtime(member).status in LIVE | {"QUE_WAIT"}:
                    continue
                out.append(member)
                if self.catalog.jobs[member].job_type == "BOX":
                    pending.append(member)
        return out

    def _reset_box_cycle(self, box: str, starting: Callable[[], None]) -> None:
        """SEM-10 (DL-242): "When a box starts running, the status of all the
        jobs it contains (including subboxes) changes to ACTIVATED ... jobs
        in boxes do not retain their statuses from previous box cycles."
        The oracle has no ACTIVATED status; a waiting member reads INACTIVE.
        A live job keeps its run. The exit code goes with the status: it is
        the previous cycle's result, and an e() atom must not read it. A row
        that is already INACTIVE loses its exit code too, by a plain store
        write: no transition, and no wake, since an atom only turns false.
        `last_end_at`, the flags and the arm stay.

        One batch (`_set_inactive_batch`) around the box's own STARTING
        transition, which `starting` performs: the rows are written first,
        so the STARTING wakes read the new cycle, and the rows' box rules
        and wakes run after it. The box is STARTING throughout phase 2: no
        member can start (SEM-10's box-RUNNING gate), no completion door or
        idle recompute runs, and the box cannot be started again. A
        consumer outside the box that reads a reset member -- an n() atom
        with a lookback reads the moved `status_at` -- sees the change."""
        cause = (
            f"box {box!r} started: statuses from the previous box cycle are not retained"
            " (SEM-10, DL-242)"
        )
        rows: list[tuple[str, str]] = []
        for job in self._contained(box, skip_live=True):
            rt = self._runtime(job)
            if rt.status != "INACTIVE":
                rows.append((job, cause))
            elif rt.exit_code is not None:
                self.store.clear_exit_code(job)
        self._set_inactive_batch(rows, clear_exit_code=True, between=starting)

    def _on_box_started(self, box: str, run_number: int) -> None:
        for member in self._members(box):
            member_ir = self.catalog.jobs[member]
            if member_ir.sem.auto_hold:
                rt = self._runtime(member)
                if not rt.on_hold:
                    self.store.set_flags(member, on_hold=True)
                    self._record(member, "ON_HOLD", "auto_hold on box start (dossier ss5)")
        # members with no conditions start immediately; others when theirs hold
        cause = f"box {box!r} started"
        for member in self._members(box):
            self._attempt_start(member, force=False, scheduled=False, cause=cause)
        # the run's window decisions wait for the outermost start's pass
        assert self._window_starts is not None
        self._window_starts.append((box, run_number, cause))

    def _waiting_for_window(self, box: str, member_ir: JobIR) -> bool:
        """A run_window member the box start leaves waiting (DL-246): not
        iced or held, not live, not run or resolved this execution."""
        schedule = member_ir.schedule
        if schedule is None or schedule.run_window is None:
            return False
        rt = self._runtime(member_ir.name)
        box_rt = self._runtime(box)
        return not (
            rt.on_ice
            or rt.on_hold
            or rt.status in LIVE | {"QUE_WAIT"}
            or member_ir.name in box_rt.ran_members
            or member_ir.name in box_rt.window_skipped_members
        )

    def _decide_windows_at_box_start(self, starts: list[tuple[str, int, str]]) -> None:
        """SEM-33 (DL-246): a box start decides the window disposition of
        each run_window member still waiting, at that instant and before its
        own schedule gate. TechDocs 24.2, run_window page: "its status
        changes to ACTIVATED when the box starts running. However, if the
        current time is not in the specified run window for the job, its
        status changes to INACTIVE." Closer to the previous close, the
        member goes through the window-skip bypass, so the box can complete.
        Closer to the next opening, one deferred start is queued, so the box
        keeps running. Inside the window nothing is decided: the member
        waits for its own conditions and schedule.

        `starts` holds every box run this start began, a subbox before its
        parent, with each run's number and cause. The decisions run after
        every start attempt in that subtree, including the attempts of
        RUNNING's wakes, deferrals before skips. A skip is a completion
        moment and may complete its box or an ancestor, so it must not run
        before a sibling or an outer member had its attempt. Each decision
        is bound to its box run: a skip may complete the run and its wakes
        may start the box again, and the new run decides for itself. An iced or held
        member, a live one, one that already ran or was resolved this
        execution, and one already deferred this run (the deferral's own
        dedup) are not decided. The decision is the direct parent's: a
        member of a subbox is decided with its subbox's start."""
        waiting: dict[str, list[tuple[str, int, JobIR, str]]] = {"defer": [], "skip": []}
        for box, run, cause in starts:
            box_rt = self._runtime(box)
            if box_rt.status != "RUNNING" or box_rt.run_number != run:
                continue  # the run already ended, or a later run replaced it
            for member in self._members(box):
                member_ir = self.catalog.jobs[member]
                if not self._waiting_for_window(box, member_ir):
                    continue
                side, _ = self._window_side(member_ir)
                if side != "inside":
                    waiting[side].append((box, run, member_ir, cause))
        for box, run, member_ir, cause in waiting["defer"] + waiting["skip"]:
            # an earlier skip may have completed this run, and its wakes may
            # have started the box again. No member can start during the
            # pass: each is outside its window, so any attempt on it meets
            # the window gate again.
            box_rt = self._runtime(box)
            if box_rt.status == "RUNNING" and box_rt.run_number == run:
                self._run_window_permits(member_ir, cause)

    # ------------------------------------------------------------------ box rules

    def _on_member_transition(self, box: str, member: str, old: str, new: str) -> None:
        box_rt = self._runtime(box)
        box_ir = self.catalog.jobs[box]
        if new == "FAILURE" and self.catalog.jobs[member].box.box_terminator:
            if box_rt.status == "RUNNING":
                # SEM-14: member failure terminates the containing box
                self._terminate(box, cause=f"box_terminator member {member!r} failed")
                return
        if box_rt.status == "TERMINATED":
            return  # SEM-13: sticky until the next box start
        # SEM-12 gating: overrides are evaluated on member transitions. An
        # INACTIVE verdict that resolves the member is a completion moment:
        # the full completion door runs, overrides first, the default fold
        # only if none fired.
        resolved = self._resolves(box, member, old, new)
        if box_rt.status == "RUNNING":
            self._completion_door(
                box,
                member,
                new,
                completion_moment=resolved,
                overrides=new in TERMINAL | {"RUNNING"} or resolved,
            )
        elif box_rt.status not in LIVE and new in TERMINAL:
            # SEM-15 [C]: a member change on a non-running box re-derives the
            # box's status (TERMINATED already returned above, SEM-13 sticky)
            self._idle_box_recompute(box, box_ir, cause=f"member {member!r} changed")

    def _completion_door(
        self, box: str, member: str, new: str, *, completion_moment: bool, overrides: bool = True
    ) -> None:
        """The completion door of a RUNNING box (SEM-11, SEM-12): the
        overrides first when `overrides`, then the default fold only if none
        fired and every member is done. A member transition, a window skip
        on a member already INACTIVE (DL-154) and an ON_ICE on a member that
        has not run (DL-285) all pass through it."""
        box_ir = self.catalog.jobs[box]
        if overrides and self._apply_box_overrides(
            box, box_ir, member, new, completion_moment=completion_moment
        ):
            return
        if self._all_members_done(box):
            self._fold_box_default(box, box_ir)

    def _resolves(self, box: str, member: str, old: str, new: str) -> bool:
        """Whether an INACTIVE transition resolves `member` in a RUNNING run
        of its parent `box` (SEM-11's carve-outs): a window skip (DL-154) or
        an operator's INACTIVE (DL-242), both marked on the box row, or an
        ON_ICE that takes a queued member that has not run out of the queue
        (DL-285). Only that ice moves an iced row from QUE_WAIT."""
        box_rt = self._runtime(box)
        return (
            new == "INACTIVE"
            and box_rt.status == "RUNNING"
            and (
                member in box_rt.window_skipped_members
                or (
                    old == "QUE_WAIT"
                    and self._runtime(member).on_ice
                    and member not in box_rt.ran_members
                )
            )
        )

    def _ice_resolves_member(self, job: str, status: str) -> None:
        """SEM-20 (DL-285): the vendor removes an iced job from all
        conditions and logic, and `_all_members_done` skips it, so an ON_ICE
        on a member of a RUNNING box is a completion moment for that box and
        every RUNNING ancestor, as a window skip is (DL-154). The flag moves
        no status, so no transition carries the check; it runs here, for an
        ice that is not queued (a queued member's INACTIVE transition
        carries it, `_resolves`). A member that ran keeps its vote, and one
        already resolved or iced was already out of the fold: neither is a
        completion moment. A box that is not RUNNING re-derives nothing
        (SEM-15 reads a member's status, which the ice does not move)."""
        job_ir = self.catalog.jobs.get(job)
        box = job_ir.box.box_name if job_ir is not None else None
        if box is None:
            return
        box_rt = self._runtime(box)
        if (
            box_rt.status != "RUNNING"
            or job in box_rt.ran_members
            or job in box_rt.window_skipped_members
        ):
            return
        self._completion_door(box, job, status, completion_moment=True)
        if self._runtime(box).status == "RUNNING":  # else its transition walked up
            self._on_descendant_transition(job, status, resolved=True)

    def _on_descendant_transition(self, member: str, new: str, *, resolved: bool) -> None:
        """SEM-12's "inside the box" is TRANSITIVE -- a grandchild is inside
        every box above it, which is what derive._is_inside implements for
        the static edge classification. So each ancestor ABOVE the direct
        parent evaluates its own overrides on this transition too; without
        it a box whose box_success names a grandchild stayed RUNNING for
        ever while derive said the edge existed.

        Only the SEM-12 override evaluation walks up. The default fold
        (SEM-11) and the SEM-15 idle recompute read an ancestor's OWN
        members, which a descendant transition does not move; they reach the
        ancestor through the direct parent's own transition.

        `resolved`: the member was resolved in its parent's RUNNING run
        (`_resolves`, or an ice), which is a completion moment for every
        ancestor, as it is for the parent."""
        chain = self._ancestor_boxes(member)
        for box in chain[1:]:  # [0] is the direct parent
            box_ir = self.catalog.jobs.get(box)
            if box_ir is None:  # pragma: no cover -- see below
                # Unreachable: lowering refuses a box_name the catalog does not define,
                # and `_ancestor_boxes` only follows `box.box_name` links. The direct
                # parent, `chain[0]`, is indexed without a guard in `_on_member_transition`
                # for the same reason.
                return
            if self._runtime(box).status != "RUNNING":
                continue  # not evaluating: SEM-13 sticky, or already folded
            if (new in TERMINAL | {"RUNNING"} or resolved) and self._apply_box_overrides(
                box, box_ir, member, new, completion_moment=resolved
            ):
                return

    def _idle_box_recompute(self, box: str, box_ir: JobIR, cause: str) -> None:
        """Derived-status recompute for a non-running box (SEM-15): pure
        function of current member statuses -- ran_members does not apply
        outside a live run. "Any jobs in the box with a status of INACTIVE
        are ignored when the status of the box is being re-evaluated"
        (DL-242), so it fires when every other member is terminal. With
        every member INACTIVE the default verdict is SUCCESS."""
        members = self._members(box)
        if not members:  # pragma: no cover -- see below
            # Unreachable: both callers reach here from a transition of a member
            # they found through that member's own `box.box_name`, so `_members(box)`
            # holds at least that member. It would take a catalog edited between the
            # transition and this call to empty it.
            return
        statuses = [s for s in (self._runtime(m).status for m in members) if s != "INACTIVE"]
        if not all(s in TERMINAL for s in statuses):
            return
        for attr, target in (
            (box_ir.sem.box_success, "SUCCESS"),
            (box_ir.sem.box_failure, "FAILURE"),
        ):
            if attr is not None and self._cond_true(attr.cond, box):
                if self._runtime(box).status != target:
                    self._set_status(
                        box,
                        target,  # type: ignore[arg-type]
                        cause=f"idle-box override recompute (SEM-15): {cause}",
                    )
                return
        any_failed = any(s in ("FAILURE", "TERMINATED") for s in statuses)
        derived: JobStatus = "FAILURE" if any_failed else "SUCCESS"
        suppressed = (
            box_ir.sem.box_failure is not None if any_failed else box_ir.sem.box_success is not None
        )
        if not suppressed and self._runtime(box).status != derived:
            self._set_status(box, derived, cause=f"idle-box recompute (SEM-15): {cause}")

    def _apply_box_overrides(
        self, box: str, box_ir: JobIR, member: str, new: str, *, completion_moment: bool = False
    ) -> bool:
        """Returns True if an override fired and set the box status.

        `completion_moment=True` (DL-154, DL-242): a window skip's or an
        operator's INACTIVE verdict RESOLVES the member without a terminal
        status, so the caller names the moment a completion moment for
        SEM-12's external/global-ref gate; the atoms still read the real
        INACTIVE status."""
        member_completed = completion_moment or new in TERMINAL
        for attr, target in (
            (box_ir.sem.box_success, "SUCCESS"),
            (box_ir.sem.box_failure, "FAILURE"),
        ):
            if attr is None:
                continue
            cond = attr.cond
            refs_member = member in _cond_job_names(cond)
            # internal ref: evaluate the moment the referenced job transitions;
            # external/global ref: evaluate only at member completion moments
            if not (refs_member or member_completed):
                continue
            if self._cond_true(cond, box):
                self._set_status(
                    box,
                    target,  # type: ignore[arg-type]
                    cause=f"box_{target.lower()} override met (SEM-12)",
                )
                self._on_box_completed(box)
                return True
        return False

    def _all_members_done(self, box: str) -> bool:
        """SEM-11, literal (DL-13): the box cannot complete until every
        member has run (to a terminal state) or been bypassed (iced, or
        resolved to INACTIVE by a window skip, DL-154, or by an injected
        STATUS INACTIVE, DL-242). A member whose condition never fires
        inside the run -- or whose run_window deferred it -- keeps the box
        RUNNING: the hung-box pattern is real behavior, not a defect to
        smooth over.

        An ON_NOEXEC member is NOT skipped here: SEM-22 bypasses it to
        SUCCESS "as [its] conditions are met", and that bypass joins the
        ran set like any start, so the fold waits for it exactly as it
        waits for a real run."""
        box_rt = self._runtime(box)
        ran = box_rt.ran_members
        resolved = box_rt.window_skipped_members
        for member in self._members(box):
            rt = self._runtime(member)
            if rt.on_ice:
                continue  # SEM-20: an iced member is out of the logic entirely
            if member in resolved and rt.status not in LIVE | {"QUE_WAIT"}:
                # an explicit INACTIVE verdict this execution: a run_window
                # skip (DL-154) or an operator's (DL-242), which counts "as
                # if the INACTIVE job returned a status of SUCCESS". A later
                # operator status on it that is not live keeps it settled;
                # the fold still votes over ran members only. The member's
                # own later start voids the mark.
                continue
            if member not in ran:
                return False  # not yet run this box execution (incl. held)
            if rt.status not in TERMINAL:
                return False  # still STARTING/RUNNING
        return True

    def _fold_box_default(self, box: str, box_ir: JobIR) -> None:
        # SEM-12 third bullet: an unmet specified override suppresses the
        # corresponding default; if neither can fire the box stays RUNNING.
        ran = self._runtime(box).ran_members
        members = [m for m in self._members(box) if m in ran]
        statuses = [self._runtime(m).status for m in members]
        any_failed = any(s in ("FAILURE", "TERMINATED") for s in statuses)
        if not any_failed and box_ir.sem.box_success is None:
            self._set_status(box, "SUCCESS", cause="default box fold: all members SUCCESS (SEM-11)")
            self._on_box_completed(box)
        elif any_failed and box_ir.sem.box_failure is None:
            self._set_status(box, "FAILURE", cause="default box fold: a member failed (SEM-11)")
            self._on_box_completed(box)
        # else: specified-but-unmet override suppresses the default -> RUNNING

    def _on_box_completed(self, box: str) -> None:
        # kill members still running? Only via job_terminator on TERMINATED/
        # FAILURE (SEM-14); SUCCESS completion leaves stragglers alone (they
        # were bypassed or the fold would not have fired).
        if self._runtime(box).status in ("FAILURE", "TERMINATED"):
            self._cascade_job_terminators(box)

    def _terminate(self, job: str, cause: str) -> None:
        self._set_status(job, "TERMINATED", cause=cause)
        job_ir = self.catalog.jobs.get(job)
        if job_ir is not None and job_ir.job_type == "BOX":
            self._cascade_job_terminators(job)

    def _cascade_job_terminators(self, box: str) -> None:
        # SEM-14: members with job_terminator die when their box fails/terminates
        for member in self._members(box):
            member_ir = self.catalog.jobs[member]
            rt = self._runtime(member)
            if member_ir.box.job_terminator and rt.status in LIVE:
                self._terminate(member, cause=f"job_terminator: box {box!r} ended")

    # ------------------------------------------------------------- re-evaluation

    def _wake_referencers(self, entity_key: str, cause: str) -> None:
        """Edge-triggered re-evaluation (DL-13): a change to `entity_key`
        (job name, "name^INST", or "g:NAME") wakes exactly the jobs whose
        `condition` references it, in catalog order. Completed consumers
        re-run on each fresh satisfaction; a self-referencing condition may
        re-trigger its own job -- that is AutoSys's own tight-loop pattern
        (L010's concern), not the oracle's to prevent."""
        for name in self._referencers.get(entity_key, ()):
            self._attempt_start(name, force=False, scheduled=False, cause=cause)

    # ----------------------------------------------------- clocks, SLAs, timeouts

    def _start_slot(self, job_ir: JobIR) -> int | None:
        """SEM-34: which `start_times` entry the current instant is. None
        when the job declares none, or when the instant is not one of them
        -- an operator's sendevent, say. A start_mins job carries one
        broadcast offset (DL-248), so it never needs a slot.

        The slot is named by instant, not by wall time (DL-260). The
        scheduler and this method convert the start times of the instant's
        local day, in the job's zone, through one definition,
        `timezones.start_time_instants`, under the same `dst-start-times`
        value. So a tick names its own slot on a DST change day too: under
        `vendor`, a 02:45 start runs at 03:00:45 and a 03:45 start at
        03:45, each naming its own. An instant names the latest start time
        at or before it, less than a minute earlier, so an event inside a
        start time's minute still names it, as the wall-time match did.
        Under `fold0` a missing-hour start can share an instant with a later
        start time; the scheduler then ticks once, and the tick names the
        earlier wall time."""
        schedule = job_ir.schedule
        if schedule is None or not schedule.start_times:  # pragma: no cover -- see below
            # Unreachable from JIL: both callers need a start-times schedule. A
            # multi-offset relative must time and any absolute one are refused at
            # lowering unless start_times exist (SEM-34, DL-248, DL-253); a single
            # relative offset broadcasts and never asks for a slot (`_sla_offset`);
            # `_slot_deadline` returns first for a job with no schedule.
            return None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        day = to_local(self._now, tz).date()
        times = [(start.hour, start.minute) for start in schedule.start_times]
        best: tuple[datetime, int] | None = None
        for index, at in start_time_instants(day, times, tz, dst=self.semantics.dst_start_times):
            if at <= self._now < at + _SLOT_MINUTE and (best is None or at > best[0]):
                best = (at, index)
        return None if best is None else best[1]

    def _sla_offset(self, job_ir: JobIR, offsets: list[int]) -> int:
        """SEM-34: "+n minutes from each start time" under the strict count
        match -- N offsets against N start_times pair BY POSITION, so the
        second tick gets the second offset.

        Two cases keep the first offset. A SINGLE offset broadcasts over
        every start time: the vendor's relative syntax is one `+minutes`
        "after each start time" (DL-248). An instant that matches no start time cannot be paired at
        all, and the first offset is what the oracle used before the pairing
        existed."""
        if len(offsets) == 1:
            return offsets[0]
        slot = self._start_slot(job_ir)
        return offsets[0] if slot is None else offsets[slot]

    def _slot_deadline(self, job: str, check: str) -> Event | None:
        """SEM-34: the deadline of the slot this tick names, or None. The
        payload carries the run_number AT THE TICK: the run the slot asks for
        is the first one to begin after it (DL-248). A relative form is due
        at tick+offset; an absolute one at `_absolute_deadline`."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None or job_ir.schedule is None:
            return None
        schedule = job_ir.schedule
        spec = schedule.must_start if check == "must_start" else schedule.must_complete
        if spec is None:
            return None
        assert self._now is not None
        deadline: datetime | None
        if spec.kind == "relative":
            if not spec.offsets_min:  # pragma: no cover -- see below
                # Unreachable from JIL: lowering refuses an empty must_start_times or
                # must_complete_times value ("empty value"), and SlaSpec requires
                # offsets_min for a relative spec. Only an IR edited after lowering holds
                # []; it would arm no deadline, which is what this returns.
                return None
            deadline = self._now + timedelta(minutes=self._sla_offset(job_ir, spec.offsets_min))
        else:
            deadline = self._absolute_deadline(job_ir, spec.times or [])
        if deadline is None:
            return None
        payload = {"check": check, "job": job, "run": self._runtime(job).run_number}
        return Event(at=deadline, kind="TIMER", payload=payload)

    def _absolute_deadline(self, job_ir: JobIR, times: list[MustTime]) -> datetime | None:
        """SEM-34 (DL-253): the absolute must time of the slot this tick
        names, on the tick's local calendar day in the job's zone, plus a
        day for each 24 hours past 23 ("Limits: 00:00-71:59 (2 calendar days
        ahead of the current calendar day)"). None when the tick names no
        slot: the times pair with start_times by position, so an instant
        that is no start time has none.

        A deadline that falls before the tick is due at the tick. Only one
        case reaches that, because lowering refuses a must time below its
        own start time (SEM-34): under `dst-start-times=fold0` a start in a
        spring change's missing hour ticks past the gap, later than the
        vendor's first minute of the next hour, where its must time may lie
        (DL-260)."""
        slot = self._start_slot(job_ir)
        schedule = job_ir.schedule
        if slot is None or schedule is None or not schedule.start_times or slot >= len(times):
            return None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        day = to_local(self._now, tz).date()
        due = _must_instant(day, schedule.start_times[slot], times[slot], tz)
        return max(due, self._now)

    def _pending_check(self, job: str, check: str, rt: JobRuntime) -> bool:
        """Is a deadline of this kind still pending for `job`: armed, not
        fired, and not yet met (DL-248, DL-253)? A must_start deadline is met
        once a run began after its tick; a must_complete one once that run
        completed. The test reads the timer heap, so no new state is kept."""
        for _, _, armed in self.store.timers():
            payload = armed.payload
            if payload.get("check") != check or payload.get("job") != job:
                continue
            tick_run = payload.get("run")
            if check == "must_start" and tick_run == rt.run_number:
                return True
            if check == "must_complete" and not self._slot_run_completed(rt, tick_run):
                return True
        return False

    def _tick_deadline(self, job: str, check: str) -> Event | None:
        """SEM-34: the must_start or must_complete deadline this tick arms,
        or None. MUST_START_ALARM fires if no new run has begun by it;
        MUST_COMPLETE_ALARM if the run this tick asks for has not completed
        by it, whenever that run started (DL-248).

        At most one deadline of each kind is pending per job (DL-248,
        DL-253). The vendor inserts the next CHK_START and CHK_COMPLETE only
        "after the job completes", so a tick that finds the job live, or
        finds an earlier tick's deadline of the same kind still pending,
        arms nothing."""
        timer = self._slot_deadline(job, check)
        if timer is None:
            return None
        rt = self._runtime(job)
        if rt.status in ("STARTING", "RUNNING", "QUE_WAIT") or self._pending_check(job, check, rt):
            return None
        return timer

    @staticmethod
    def _slot_run_completed(rt: JobRuntime, tick_run: object) -> bool:
        """SEM-34 (DL-248): has the run a tick asked for completed? That run
        is the first to begin after the tick, number tick_run+1. It has
        completed once it is no longer STARTING or RUNNING, or once a later
        run began -- runs of one job never overlap."""
        if not isinstance(tick_run, int):
            return False
        if rt.run_number > tick_run + 1:
            return True
        return rt.run_number == tick_run + 1 and rt.status not in LIVE

    def _arm_term_run_time(self, job_ir: JobIR) -> None:
        assert self._now is not None
        run_number = self._runtime(job_ir.name).run_number + 1  # the run being started
        # zero means no limit (vendor default; DL-241); arm no timer for it
        if job_ir.sem.term_run_time_min is not None and job_ir.sem.term_run_time_min != 0:
            deadline = self._now + timedelta(minutes=job_ir.sem.term_run_time_min)
            self._schedule_timer(
                deadline,
                Event(
                    at=deadline,
                    kind="TIMER",
                    payload={"check": "term_run_time", "job": job_ir.name, "run": run_number},
                ),
            )

    def _lazy_clock_checks(self) -> None:
        """Deadline timers fire through the timer heap inside feed(); nothing
        else is time-lazy v1."""

    def _dispatch_timer_check(self, ev: Event) -> bool:
        check = ev.payload.get("check")
        if check is None:
            return False
        job = self._required_job(ev)
        rt = self._runtime(job)
        if check == "must_start":
            # inverted run check: alarm iff NO new run began since the tick
            if ev.payload.get("run") == rt.run_number:
                self._emit("MUST_START_ALARM", job=job)
                self._record(job, "MUST_START_ALARM", "must_start_times deadline (SEM-34)")
            return True
        if check == "must_complete":
            # SEM-34 (DL-248): alarm only, no control flow -- iff the run the
            # tick asked for has not completed, including when it never began
            if not self._slot_run_completed(rt, ev.payload.get("run")):
                self._emit("MUST_COMPLETE_ALARM", job=job)
                self._record(job, "MUST_COMPLETE_ALARM", "must_complete_times deadline (SEM-34)")
            return True
        if ev.payload.get("run") != rt.run_number:
            return True  # stale deadline from an earlier run of this job
        if check == "term_run_time":
            if rt.status == "RUNNING":
                self._terminate(job, cause="term_run_time exceeded (dossier ss5)")
        return True


def _cond_job_names(cond: Cond) -> set[str]:
    from dsl41.conditions import iter_atoms

    names: set[str] = set()
    for atom in iter_atoms(cond):
        if not isinstance(atom, GlobalAtom) and atom.job.instance is None:
            names.add(atom.job.name)
    return names


def _entity_keys(cond: Cond) -> set[str]:
    """Edge-trigger keys of a condition: local and instance-qualified job
    names plus "g:NAME" for globals (see Oracle._referencers)."""
    from dsl41.conditions import iter_atoms

    keys: set[str] = set()
    for atom in iter_atoms(cond):
        if isinstance(atom, GlobalAtom):
            keys.add(f"g:{atom.name}")
        else:
            keys.add(atom.job.key)
    return keys


def _to_time(t: Time) -> dtime:
    return dtime(hour=t.hour, minute=t.minute)


def _must_instant(day: date, start: Time, must: MustTime, tz: tzinfo | None) -> datetime:
    """An absolute must time as an engine instant (SEM-34, DL-253). `day`
    is the local calendar day of the start it pairs with. Hours 24-71 land
    on the next days. TechDocs 24.2, "Daylight Time Changes" and "Standard
    Time Changes".

    Spring, a must time in the missing hour moves to the first minute of
    the next hour: "a job that must start by 2:05 and must complete by 2:45
    generates an alarm if the job does not start by 3:00:05 or if it does
    not complete by 3:00:45". A start in the missing hour runs in that
    minute too; when it would run after its must time, the must time moves
    "to the final second of the first minute of the hour following the
    missing hour", 3:00:59.

    Fall, a must time in the repeated hour takes the first, daylight pass
    when the start is before the change: "must complete at 1:30 generates
    an alarm if the job has not completed by 1:30 DT". When the start is in
    the repeated hour too, it takes the second, standard pass. A start on
    an earlier day is before the change.

    Other change shapes keep the plain fold=0 conversion, as run_window's
    do (DL-249)."""
    days, hour = divmod(must.hour, 24)
    local = datetime.combine(day + timedelta(days=days), dtime(hour, must.minute))
    change = dst_change(local.date(), tz)
    if change == "spring" and hour == MISSING_HOUR:
        due = vendor_gap_instant(local.date(), hour, must.minute, tz)
    else:
        second = (
            change == "fall" and hour == REPEATED_HOUR and days == 0 and start.hour == REPEATED_HOUR
        )
        due = to_utc(local.replace(fold=1 if second else 0), tz)
    if dst_change(day, tz) == "spring" and start.hour == MISSING_HOUR:
        runs_at = vendor_gap_instant(day, start.hour, start.minute, tz)
        if runs_at > due:
            due = vendor_gap_instant(day, start.hour, 59, tz)
    return due


def _next_occurrence(now: datetime, target: dtime) -> datetime:
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _prev_occurrence(now: datetime, target: dtime) -> datetime:
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate


def _window_span(day: date, lo: dtime, hi: dtime, tz: tzinfo | None) -> tuple[datetime, datetime]:
    """The run_window that opens on local `day`, as two engine instants,
    with the vendor's endpoint rules on a documented DST change (DL-249).
    TechDocs 12.1 and 24.2, "Daylight Time Changes" and "Standard Time
    Changes".

    Spring, an opening in the missing hour moves to 03:00: "a run window of
    2:45 - 3:45 becomes 3:00 - 3:45". A close in the missing hour keeps the
    window's length: "a run window of 1:00 - 2:30 ... ends at 3:30". Both in
    it: "a run window of 2:15 - 2:45 becomes 3:00 - 3:45". Fall, an opening
    in the repeated hour takes the second, standard-time pass: "a run
    window of 1:45 - 2:45 becomes 1:45 ST - 2:45 ST". A close in it takes
    the first pass: "a run window of 11:30 - 1:30 ends at 1:30 DT". When
    both fall in it, "the run window opens during the second, standard time
    hour", and the close follows it there. The vendor's text describes two
    distinct endpoints, so an equal-endpoint window keeps the SEM-33
    zero-width pin: the single instant its opening maps to."""
    start = datetime.combine(day, lo)
    end = datetime.combine(day if lo <= hi else day + timedelta(days=1), hi)
    change = dst_change(day, tz)
    opens_repeated = change == "fall" and start.hour == REPEATED_HOUR
    if change == "spring" and start.hour == MISSING_HOUR:
        opens = to_utc(start.replace(hour=MISSING_HOUR + 1, minute=0), tz)
    else:
        opens = to_utc(start.replace(fold=1 if opens_repeated else 0), tz)
    if lo == hi:
        return opens, opens
    # fold=0 is the vendor's close in both seasons: past the gap by the
    # missing hour in spring, the daylight-time pass in fall. Only a close
    # on the same day as an opening in the repeated hour takes fold=1.
    closes_second = opens_repeated and end.date() == day and end.hour == REPEATED_HOUR
    closes = to_utc(end.replace(fold=1 if closes_second else 0), tz)
    return opens, closes
