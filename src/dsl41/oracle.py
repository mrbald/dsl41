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
  end: no run this cycle (INACTIVE stays). Exact midpoint: next opening
  ([?] undocumented; pinned here, revisit with live access). The window is
  read in the job's own `timezone` (SEM-35 re-bases every time attribute of
  that job), so the comparison runs on local wall time and the queued timer
  goes back on the engine clock; the name resolves through the runner's
  ladder. A job with no `timezone:` compares on the engine clock -- the
  run-level base zone is the scheduler's default, not the oracle's
  (PENDING: E10).
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
- ON_ICE (SEM-05/SEM-20): every status/exitcode atom whose job is currently
  ON_ICE evaluates TRUE -- f()/t()/e() included, per SEM-05's blanket
  wording over SEM-20's "as though it succeeded" (DL-13, Q6-adjacent; the
  lookback-ignored half is cited since DL-58: KB 438836, "the system
  ignores the look-back condition") --
  with lookback ignored; the iced job itself never starts (FORCE included);
  OFF_ICE does not re-evaluate (conditions must REOCCUR). Ice on a RUNNING
  job takes effect at completion: atoms read the real in-flight status
  until then ([?] unverified corner, documented).
- ON_HOLD (SEM-21): the held job does not start; nothing else changes;
  OFF_HOLD immediately re-evaluates that job's start (missed runs collapse
  to at most one).
- ON_NOEXEC (SEM-22): when the job would start, it bypasses to SUCCESS
  (STARTING/RUNNING skipped) and downstream runs normally. A BOX does not
  bypass: it goes RUNNING and its members bypass as their conditions are
  met, box level by box level, so a member box walks its own members too.
  A member bypasses on its own flag or on any containing box's. The bypass
  joins the box's ran set like a real start, so the SEM-11 fold waits for
  it and the once-per-box-run gate holds; it also counts as the tick's run
  (the Q3/DL-54 reading), so no MUST_START_ALARM follows a bypass.
- initial_status (SEM-24, DL-18): definition-time ON_HOLD/ON_ICE/ON_NOEXEC
  seeds the corresponding flag before the first event; no trace entry
  (definition state, not a transition). INACTIVE is the default anyway.
- Boxes: SEM-10 (members start when box RUNNING + own condition; at most
  once per box run), SEM-11 LITERAL (DL-13: the box cannot complete until
  every member that is not ON_ICE has RUN -- or been bypassed by SEM-22 --
  to a terminal state; a member whose condition never fires, or whose
  run_window deferred it, keeps the box RUNNING: the hung-box pattern is
  real behavior), SEM-12 (override
  gating: internal refs evaluated on the referenced member's transition;
  external/global refs evaluated only at member completion moments -- the
  hung-RUNNING pattern; "inside" is TRANSITIVE, as in derive._is_inside, so
  every ancestor box evaluates its overrides on a descendant's transition,
  not just the direct parent), SEM-13 (TERMINATED boxes are sticky until the
  next box start), SEM-14 (box_terminator member FAILURE -- not
  TERMINATED -- kills the box; job_terminator members die with the box),
  SEM-15 (a terminal member transition on a non-running, non-TERMINATED
  box re-derives its status once all members are terminal), SEM-17
  (nesting: a member box starting is a member start; folds recurse;
  ACTIVATED is unmodeled -- non-goal v1).
- FORCE_STARTJOB (SEM-23): overrides false conditions, ON_HOLD, and the
  box-RUNNING gate ("regardless of conditions"), but never ON_ICE
  (SEM-20's "removed from all logic" wins; DL-13). Forced runs emit normal
  statuses and satisfy downstream latches.
- Injected STATUS may overwrite a terminal status (the CHANGE_STATUS
  analog): script-authoring hazard, documented not guarded.
- must_start_times / must_complete_times (SEM-34): alarms only, never
  control flow. Relative offsets arm on the STARTJOB tick (must_start:
  alarm iff no new run began by tick+offset -- armed even when the start
  is abandoned or deferred, that is the alarm's point) and on the actual
  start (must_complete). N offsets against N start_times pair BY POSITION
  -- the tick's local time of day names the slot. A SINGLE offset
  broadcasts over every start time ([?] the dossier's strict count rule and
  the vendor's own worked example disagree; open against a live instance),
  and an instant that matches no start time keeps the first offset.
  Absolute forms need the calendar the oracle does not own; scripts
  exercise relative forms.
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
  release frees room. All-or-nothing acquire => no hold-and-wait => deadlock-
  free by construction; QUANTITY=1 shared == mutex. res_type sets the default
  release (R/absent free-on-completion, D never, T is a level GATE that never
  acquires); per-request FREE overrides it (Y success-only, N never, A
  unconditional). A queued job re-validates box-RUNNING/ice/hold at admission
  (# PENDING: Qr6 conditions are NOT re-checked). Enforcement of unsized/
  unknown-res_type/malformed shapes is the runner's preflight (DL-50): the
  oracle models only sizeable buckets, so oracle-direct over an unrefused bad
  catalog runs it unthrottled -- the execution gate is preflight, by design.
- Still non-goals v1: definition-time mutations (SEM-16; incl. mid-run
  update_resource replenishment of depletables), agent failures.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, time as dtime, timedelta, tzinfo
from typing import Final

from dsl41.canon import CanonError, canonical_bytes
from dsl41.capacity import CapacityPool, to_reservations
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
from dsl41.ir import CatalogIR, JobIR, Semantics, Time

from dsl41.oracle_state import (
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

#: SEM-02: n() is true unless the job is in one of these (WAIT_REPLY/RESTART/
#: SUSPENDED are out-of-scope states the oracle never produces). QUE_WAIT is
#: DELIBERATELY absent (DL-50): a resource-queued job is not running, so n() is
#: TRUE for it -- and every status/exitcode atom reads false (it never ran).
_N_FALSE_STATUSES: frozenset[str] = frozenset({"STARTING", "RUNNING"})


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


class Oracle:
    """Deterministic interpreter over one CatalogIR (ir-design ss7)."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.catalog = catalog
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
        self._pool = CapacityPool(catalog)
        self._in_wake = False
        #: SEM-35 name -> zone, resolved once per name (the ladder walks
        #: the whole zoneinfo database for a city default)
        self._tz_cache: dict[str, tzinfo] = {}
        #: SEM-35's `ujo_timezones` table, as `--timezone-map` supplies it to
        #: the scheduler (DL-62). None means no map, which is a DIFFERENT
        #: resolution than an empty one: the ladder's unique-city default
        #: applies only when the estate supplied no table, so a run with a
        #: map gets the map's answer and nothing else (runner_scheduler's
        #: `resolve_timezone`). Wired from the scheduler at engine build:
        #: an oracle that resolved without it refused a map-only name at the
        #: first start although preflight had passed (DL-151).
        self._tz_aliases: dict[str, str] | None = None if tz_aliases is None else dict(tz_aliases)

    # ------------------------------------------------------------------ plumbing

    def _boxes(self) -> list[str]:
        return [n for n, j in self.catalog.jobs.items() if j.job_type == "BOX"]

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
        on; deadline's run no longer RUNNING) is not pending, it is dead
        weight awaiting its lazy pop. The ss10 status query renders this for
        the ss11 jobs table; display truth must be the dispatch truth --
        which is why the order is `store.timers()`'s and is NOT re-sorted
        here. On a TIE that order carries the ordering token, the firing
        order of equal-time timers (period-model ss3.2); a `sorted()` over
        `(due, job, kind)` reads as a no-op on an already-due-ordered list
        and silently replaced it with job-name order (DL-143)."""
        live: list[tuple[datetime, str, str]] = []
        for due, _, ev in self.store.timers():
            job = ev.payload.get("job")
            if not isinstance(job, str):
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
                live.append((due, job, "run_window"))
                continue
            rt = self.store.job.get(job)
            if rt is None or ev.payload.get("run") != rt.run_number:
                continue  # stale: a later run superseded this deadline
            if check in ("must_complete", "term_run_time") and rt.status != "RUNNING":
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
            if "deferred_cause" not in ev.payload:
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
        self, job: str, status: JobStatus, cause: str, exit_code: int | None = None
    ) -> None:
        old = self._runtime(job).status
        self.store.transition(job, status, self._now, exit_code)
        self._record(job, f"{old}->{status}", cause)
        self._emit("STATUS", job=job, status=status)
        self._after_transition(job, old, status)

    def _after_transition(self, job: str, old: str, new: str) -> None:
        job_ir = self.catalog.jobs.get(job)
        if job_ir is not None:
            box = job_ir.box.box_name
            if box is not None:
                self._on_member_transition(box, job, old, new)
                self._on_descendant_transition(job, new)
            if job_ir.job_type == "BOX" and new in TERMINAL:
                # PENDING: Q3c -- a member's arm is scoped to the box run
                # that armed it (DL-54 review MAJOR): an unconsumed arm dies
                # with the run, BEFORE any wake can ride it (nested boxes
                # recurse via their own completion transitions). One field
                # aside (DL-58) hints the vendor latch may instead survive
                # into the NEXT box run; the scoped pin stands until a live
                # test.
                for member in self._members(job):
                    m_rt = self.store.job.get(member)
                    if m_rt is not None and m_rt.armed:
                        self.store.set_armed(member, False)
                        self._record(
                            member,
                            "SCHED_DISARM",
                            f"unconsumed arm dies with box {job!r} run (Q3c pin, DL-54/58)",
                        )
        # DL-120: a job leaves QUE_WAIT through one of three paths that drop
        # its rank first (admitted, killed, cancelled) -- or through an
        # injected STATUS, which drops nothing. A rank left behind kept a
        # terminal job in the admission queue, where the next release would
        # start it again, so the rank goes with the status that owns it.
        if new != "QUE_WAIT" and self._runtime(job).waiter_seq is not None:
            self.store.dequeue_waiter(job)
        # DL-50: RELEASE a completed holder's units BEFORE waking anything. A
        # self-referencing re-trigger (the L010 tight-loop -- _wake_referencers
        # may re-start the very job that just completed) must re-acquire against
        # the FREED capacity, not overwrite its own still-held record and strand
        # a unit (adversarial review BLOCKER). Waiters then wake after condition
        # referencers -- the documented deterministic order.
        #
        # DL-120: the release edge is LEAVING the live statuses, not reaching a
        # terminal one. The two coincide for every ordinary run and differ only
        # for an injected STATUS INACTIVE on a live holder, which used to strand
        # the units in a `_held` record no row could see. Reservations exist
        # exactly while STARTING or RUNNING (period-model ss5), so the release
        # is on that same edge; a non-SUCCESS exit spends what FREE=N and a
        # depletable were always going to spend.
        released = new not in ("STARTING", "RUNNING") and self._pool.holds(self._runtime(job))
        if released:
            self.store.release_reservations(job, new)
        # SEM-01/dossier ss0: the transition wakes exactly the jobs whose
        # condition references this one (edge-triggered, DL-13)
        self._wake_referencers(job, cause=f"status of {job!r} changed to {new}")
        if released:
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
                # SEM-34: the schedule tick arms the must_start deadline
                # whether or not the start succeeds -- that is its point
                self._arm_must_start(job)
            # DL-68: a sourced event names its trigger -- a scheduler tick and
            # an operator sendevent must not collapse to one cause string
            cause = f"{kind} event ({ev.source})" if ev.source else f"{kind} event"
            deferred = ev.payload.get("deferred_cause")
            if isinstance(deferred, str):
                # SEM-33 defer: the fired timer replays the original start's
                # provenance instead of collapsing to a bare TIMER (DL-68)
                cause = f"run_window-deferred {deferred}"
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
            if status in ("STARTING", "RUNNING"):
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
        elif kind in ("ON_ICE", "OFF_ICE", "ON_HOLD", "OFF_HOLD", "ON_NOEXEC", "OFF_NOEXEC"):
            self._handle_oob(kind, self._required_job(ev))
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
        if status not in (
            "INACTIVE",
            "STARTING",
            "RUNNING",
            "SUCCESS",
            "FAILURE",
            "TERMINATED",
        ):
            raise OracleError(f"unknown status {status!r}")
        code = exit_code if isinstance(exit_code, int) else None
        self._set_status(job, status, cause="injected STATUS", exit_code=code)

    def _handle_oob(self, kind: EventKind, job: str) -> None:
        # the status BEFORE the flag change -- a flag never moves a status, but
        # the rows are frozen (DL-86), so hold the value, not a stale row
        status = self._runtime(job).status
        if kind == "ON_ICE":
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
                # SEM-20: downstream conditions now treat this job as satisfied
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
        elif kind == "OFF_HOLD":
            self.store.set_flags(job, on_hold=False)
            self._record(job, "OFF_HOLD", "sendevent OFF_HOLD")
            if status == "QUE_WAIT":
                self._wake_waiters()  # DL-50: a held-while-queued job re-attempts
            else:
                # SEM-21: if conditions are already satisfied, run immediately
                self._attempt_start(job, force=False, scheduled=False, cause="OFF_HOLD")
        elif kind == "ON_NOEXEC":
            self.store.set_flags(job, on_noexec=True)
            self._record(job, "ON_NOEXEC", "sendevent ON_NOEXEC")
        elif kind == "OFF_NOEXEC":
            self.store.set_flags(job, on_noexec=False)
            self._record(job, "OFF_NOEXEC", "sendevent OFF_NOEXEC")

    # -------------------------------------------------------- condition evaluation

    def _atom_true(self, atom: StatusAtom | ExitCodeAtom, evaluator: str) -> bool:
        name = (
            atom.job.name if atom.job.instance is None else f"{atom.job.name}^{atom.job.instance}"
        )
        rt = self.store.job.get(name)
        if rt is None:
            return False  # SEM-06: undefined -> permanently, silently false
        if rt.on_ice and rt.status not in ("STARTING", "RUNNING"):
            # SEM-05/SEM-20 + DL-13: an iced predecessor satisfies every atom
            # kind, lookback ignored -- but ice on a running job takes effect
            # at completion (the in-flight run is still real)
            return True
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
        if rt.status_at is None:
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
            return None  # SEM-20: iced jobs never run (FORCE included -- DL-13);
            # never arms: conditions must REOCCUR after OFF_ICE
        if rt.on_hold and not force:
            # SEM-21: held jobs do not start; a scheduled tick latches so the
            # missed run collapses to at most one on OFF_HOLD (Q3, DL-54)
            self._arm(job_ir, rt, scheduled, "blocked ON_HOLD")
            return None
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
        self._start(job, cause)
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
        DL-62 unique-city default). None means the engine clock IS the
        comparison basis -- the case for every job without `timezone:`.

        PENDING: E10 -- a run-level base zone (`--timezone`) is the
        scheduler's default for jobs that declare none; the oracle owns no
        run-level default, so those jobs compare on the engine clock."""
        schedule = job_ir.schedule
        name = schedule.timezone if schedule is not None else None
        if name is None:
            return None
        if name not in self._tz_cache:
            from dsl41.runner_scheduler import resolve_timezone

            resolved = resolve_timezone(name, self._tz_aliases)
            if resolved is None:
                raise OracleError(
                    f"{job_ir.name}: timezone {name!r} is not resolvable (SEM-35: a zoneinfo"
                    " name, a POSIX fixed offset, or a ujo_timezones entry supplied to the"
                    " runner as --timezone-map)"
                )
            self._tz_cache[name] = resolved.tz
        return self._tz_cache[name]

    @staticmethod
    def _local(when: datetime, tz: tzinfo | None) -> datetime:
        """An engine instant as naive wall time in `tz`."""
        if tz is None:
            return when
        return when.replace(tzinfo=UTC).astimezone(tz).replace(tzinfo=None)

    @staticmethod
    def _engine_time(local: datetime, tz: tzinfo | None) -> datetime:
        """The inverse of _local. DST corners follow PEP 495 fold=0, the same
        pin the scheduler's tick conversion uses (runner-design E10)."""
        if tz is None:
            return local
        return local.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)

    def _run_window_permits(self, job_ir: JobIR, cause: str) -> bool:
        """SEM-33 closer-edge rule; True == start may proceed now. The window
        is read in the job's own timezone (SEM-35 re-bases every time
        attribute of that job), so the comparison happens on local wall time
        while the timer it queues goes back on the engine clock."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return True
        assert self._now is not None
        tz = self._job_tz(job_ir)
        now_local = self._local(self._now, tz)
        lo, hi = schedule.run_window
        now_t = now_local.time()
        lo_t = _to_time(lo)
        hi_t = _to_time(hi)
        if lo_t <= hi_t:
            inside = lo_t <= now_t <= hi_t
        else:  # window crosses midnight
            inside = now_t >= lo_t or now_t <= hi_t
        if inside:
            return True
        next_open = self._engine_time(_next_occurrence(now_local, lo_t), tz)
        prev_close = self._engine_time(_prev_occurrence(now_local, hi_t), tz)
        # both distances are measured on the ENGINE clock: a DST shift inside
        # the gap makes the two wall-clock distances lie about elapsed time
        to_open = next_open - self._now
        since_close = self._now - prev_close
        if to_open <= since_close:  # [?] midpoint tie -> next opening
            # DL-54 review MINOR: an armed job can reach this branch on every
            # condition edge -- one pending defer per (job, opening) instant,
            # not one per attempt (duplicate timers spammed pending_timers()).
            pending = any(
                due == next_open and e.kind == "TIMER" and e.payload.get("job") == job_ir.name
                for due, _, e in self.store.timers()
            )
            if not pending:
                self._schedule_timer(
                    next_open,
                    Event(
                        at=next_open,
                        kind="TIMER",
                        payload={"job": job_ir.name, "deferred_cause": cause},
                    ),
                )
                self._record(
                    job_ir.name,
                    "RUN_WINDOW_DEFER",
                    f"outside run_window; closer to next opening -- STARTJOB queued ({cause})",
                )
        else:
            self._record(
                job_ir.name,
                "RUN_WINDOW_SKIP",
                f"outside run_window; closer to previous close -- not run ({cause})",
            )
        return False

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
        bypasses while a box that contains it is ON_NOEXEC ("the bypass
        overrides manual status changes to members while the box is
        ON_NOEXEC"). A BOX never bypasses: an ON_NOEXEC box "goes RUNNING,
        members are bypassed to SUCCESS as their conditions are met", so the
        rule is applied once per box level and a member box walks its own
        members too."""
        if job_ir.job_type == "BOX":
            return False
        if self._runtime(job_ir.name).on_noexec:
            return True
        return any(self._runtime(box).on_noexec for box in self._ancestor_boxes(job_ir.name))

    def _start(self, job: str, cause: str) -> None:
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
        # existing corpus are untouched: no buckets, no waiters, same cause).
        vector = self._pool.demand_vector(job_ir)
        if vector and not self._pool.can_admit(vector, self.store.job, self.store.consumed):
            self._enqueue_waiter(job, cause)
            return
        # DL-120: the vector is FROZEN onto the row here. The terminal
        # transition releases what this run took, never what the catalog says
        # the job wants by then (PR-20).
        self.store.reserve(job, to_reservations(vector))
        self._run(job_ir, cause, had_demand=bool(vector))

    def _run(self, job_ir: JobIR, cause: str, *, had_demand: bool) -> None:
        """Start tail once admission has passed: run_number bump, box
        bookkeeping, STARTING -> RUNNING, box member launch (SEM-10)."""
        job = job_ir.name
        self._arm_sla_and_term(job_ir)  # reads run_number before the bump
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
        self._set_status(job, "STARTING", cause=cause)
        running_cause = (
            "admitted: resources acquired (DL-50)"
            if had_demand
            else "QUE_WAIT collapses to immediate (ss7 non-goal)"
        )
        self._set_status(job, "RUNNING", cause=running_cause)
        if job_ir.job_type == "BOX":
            self._on_box_started(job)

    # ---------------------------------------------------------- resources (DL-50)

    def _enqueue_waiter(self, job: str, cause: str) -> None:
        self.store.enqueue_waiter(job)
        self._set_status(job, "QUE_WAIT", cause=f"waiting for resources ({cause})")

    def _wake_waiters(self) -> None:
        """Admit queued jobs whose full vector now fits, in deterministic order,
        to a fixpoint. Re-entrancy-guarded: a nested call (a release inside an
        admitted job's cascade) defers to the outer loop's next scan."""
        if self._in_wake:
            return
        self._in_wake = True
        try:
            while any(
                self._readmit(job) in ("admitted", "cancelled")
                for job in self._pool.sorted_waiters(self.store.job)
            ):
                pass  # queue changed; sorted_waiters is re-read each scan
        finally:
            self._in_wake = False

    def _readmit(self, job: str) -> str:
        """One admission attempt for a queued job. Re-validates the guards that
        can change while queued (ice, box-RUNNING, hold) before the capacity
        check; conditions are NOT re-checked (# PENDING: Qr6)."""
        rt = self._runtime(job)
        job_ir = self.catalog.jobs[job]
        if rt.on_ice:
            return self._cancel_waiter(job, "iced while queued")
        box = job_ir.box.box_name
        if box is not None and self._runtime(box).status != "RUNNING":
            return self._cancel_waiter(job, f"box {box!r} no longer RUNNING")
        if rt.on_hold:
            return "held"  # stays queued; OFF_HOLD re-scans
        vector = self._pool.demand_vector(job_ir)
        if not self._pool.can_admit(vector, self.store.job, self.store.consumed):
            return "waiting"
        self.store.dequeue_waiter(job)
        self.store.reserve(job, to_reservations(vector))
        self._run(job_ir, cause="resources freed (QUE_WAIT admitted, DL-50)", had_demand=True)
        return "admitted"

    def _cancel_waiter(self, job: str, why: str) -> str:
        self.store.dequeue_waiter(job)
        self._set_status(job, "INACTIVE", cause=f"QUE_WAIT cancelled: {why} (DL-50)")
        return "cancelled"

    def _on_box_started(self, box: str) -> None:
        for member in self._members(box):
            member_ir = self.catalog.jobs[member]
            if member_ir.sem.auto_hold:
                rt = self._runtime(member)
                if not rt.on_hold:
                    self.store.set_flags(member, on_hold=True)
                    self._record(member, "ON_HOLD", "auto_hold on box start (dossier ss5)")
        # members with no conditions start immediately; others when theirs hold
        for member in self._members(box):
            self._attempt_start(member, force=False, scheduled=False, cause=f"box {box!r} started")

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
        # SEM-12 gating: overrides are evaluated on member transitions
        if box_rt.status == "RUNNING" and new in TERMINAL | {"RUNNING"}:
            if self._apply_box_overrides(box, box_ir, member, new):
                return
        if box_rt.status == "RUNNING" and self._all_members_done(box):
            self._fold_box_default(box, box_ir)
        elif box_rt.status not in ("RUNNING", "STARTING") and new in TERMINAL:
            # SEM-15 [C]: a member change on a non-running box re-derives the
            # box's status (TERMINATED already returned above, SEM-13 sticky)
            self._idle_box_recompute(box, box_ir, cause=f"member {member!r} changed")

    def _on_descendant_transition(self, member: str, new: str) -> None:
        """SEM-12's "inside the box" is TRANSITIVE -- a grandchild is inside
        every box above it, which is what derive._is_inside implements for
        the static edge classification. So each ancestor ABOVE the direct
        parent evaluates its own overrides on this transition too; without
        it a box whose box_success names a grandchild stayed RUNNING for
        ever while derive said the edge existed.

        Only the SEM-12 override evaluation walks up. The default fold
        (SEM-11) and the SEM-15 idle recompute read an ancestor's OWN
        members, which a descendant transition does not move; they reach the
        ancestor through the direct parent's own transition."""
        for box in self._ancestor_boxes(member)[1:]:  # [0] is the direct parent
            box_ir = self.catalog.jobs.get(box)
            if box_ir is None:
                return
            if self._runtime(box).status != "RUNNING":
                continue  # not evaluating: SEM-13 sticky, or already folded
            if new in TERMINAL | {"RUNNING"} and self._apply_box_overrides(
                box, box_ir, member, new
            ):
                return

    def _idle_box_recompute(self, box: str, box_ir: JobIR, cause: str) -> None:
        """Derived-status recompute for a non-running box (SEM-15): pure
        function of current member statuses -- ran_members does not apply
        outside a live run. Only fires when every member is terminal."""
        members = self._members(box)
        statuses = [self._runtime(m).status for m in members]
        if not members or not all(s in TERMINAL for s in statuses):
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

    def _apply_box_overrides(self, box: str, box_ir: JobIR, member: str, new: str) -> bool:
        """Returns True if an override fired and set the box status."""
        member_completed = new in TERMINAL
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
        member has run (to a terminal state) or been bypassed (iced).
        A member whose condition never fires inside the run -- or whose
        run_window deferred it -- keeps the box RUNNING: the hung-box
        pattern is real behavior, not a defect to smooth over.

        An ON_NOEXEC member is NOT skipped here: SEM-22 bypasses it to
        SUCCESS "as [its] conditions are met", and that bypass joins the
        ran set like any start, so the fold waits for it exactly as it
        waits for a real run."""
        ran = self._runtime(box).ran_members
        for member in self._members(box):
            rt = self._runtime(member)
            if rt.on_ice:
                continue  # SEM-20: an iced member is out of the logic entirely
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
            if member_ir.box.job_terminator and rt.status in ("STARTING", "RUNNING"):
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
        """SEM-34: which `start_times` entry the current instant is, read in
        the job's own timezone. None when the job declares none, or when the
        instant is not one of them -- an operator's sendevent, or a start a
        condition edge released after the tick (SEM-32)."""
        schedule = job_ir.schedule
        if schedule is None or not schedule.start_times:
            return None
        assert self._now is not None
        now_local = self._local(self._now, self._job_tz(job_ir))
        for index, start in enumerate(schedule.start_times):
            if (start.hour, start.minute) == (now_local.hour, now_local.minute):
                return index
        return None

    def _sla_offset(self, job_ir: JobIR, offsets: list[int]) -> int:
        """SEM-34: "+n minutes from each start time" under the strict count
        match -- N offsets against N start_times pair BY POSITION, so the
        second tick gets the second offset.

        Two cases keep the first offset. A SINGLE offset is the dossier's
        own [?] exception (it broadcasts over every start time; the vendor's
        worked example and the strict rule disagree, open against a live
        instance). An instant that matches no start time cannot be paired at
        all, and the first offset is what the oracle used before the pairing
        existed."""
        if len(offsets) == 1:
            return offsets[0]
        slot = self._start_slot(job_ir)
        return offsets[0] if slot is None else offsets[slot]

    def _arm_must_start(self, job: str) -> None:
        """SEM-34: MUST_START_ALARM if no new run has begun by tick+offset."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None or job_ir.schedule is None:
            return
        spec = job_ir.schedule.must_start
        if spec is None or spec.kind != "relative" or not spec.offsets_min:
            return
        assert self._now is not None
        deadline = self._now + timedelta(minutes=self._sla_offset(job_ir, spec.offsets_min))
        self._schedule_timer(
            deadline,
            Event(
                at=deadline,
                kind="TIMER",
                payload={
                    "check": "must_start",
                    "job": job,
                    "run": self._runtime(job).run_number,  # unchanged == never started
                },
            ),
        )

    def _arm_sla_and_term(self, job_ir: JobIR) -> None:
        assert self._now is not None
        run_number = self._runtime(job_ir.name).run_number + 1  # the run being started
        schedule = job_ir.schedule
        if schedule is not None and schedule.must_complete is not None:
            spec = schedule.must_complete
            if spec.kind == "relative" and spec.offsets_min:
                deadline = self._now + timedelta(minutes=self._sla_offset(job_ir, spec.offsets_min))
                self._schedule_timer(
                    deadline,
                    Event(
                        at=deadline,
                        kind="TIMER",
                        payload={"check": "must_complete", "job": job_ir.name, "run": run_number},
                    ),
                )
        if job_ir.sem.term_run_time_min is not None:
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
        else is time-lazy v1 (hook kept for the SLA/absolute-times extension)."""

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
        if ev.payload.get("run") != rt.run_number:
            return True  # stale deadline from an earlier run of this job
        if check == "must_complete":
            # SEM-34: alarm only, no control flow
            if rt.status == "RUNNING":
                self._emit("MUST_COMPLETE_ALARM", job=job)
                self._record(job, "MUST_COMPLETE_ALARM", "must_complete_times deadline (SEM-34)")
        elif check == "term_run_time":
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
        elif atom.job.instance is None:
            keys.add(atom.job.name)
        else:
            keys.add(f"{atom.job.name}^{atom.job.instance}")
    return keys


def _to_time(t: Time) -> dtime:
    return dtime(hour=t.hour, minute=t.minute)


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
