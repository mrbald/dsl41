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
  times must be non-decreasing.
- Re-evaluation is EDGE-TRIGGERED (DL-13): a transition, SET_GLOBAL, or
  ON_ICE wakes exactly the jobs whose `condition` references the changed
  entity, so completed consumers re-run on each fresh satisfaction and a
  self-referencing condition may re-trigger its own job (AutoSys's own
  tight-loop pattern; L010's concern, not the oracle's to prevent).
- Scheduling is script-driven too: the oracle owns no calendar. The script
  injects STARTJOB where AutoSys's scheduler would fire (start_times /
  start_mins ticks); a date_conditions job -- standalone or box member (the
  SEM-31/L013 double gate) -- starts only on its tick, never on condition
  edges. A scheduled STARTJOB whose `condition` is false is ABANDONED
  (SEM-32 default reading; PENDING: Q3 -- arm-and-wait would change this
  branch), except that run_window gating applies first.
- run_window (SEM-33): a start attempt outside the window applies the
  closer-edge rule -- nearer the next opening: schedule a TIMER STARTJOB at
  window open (box context stays RUNNING overnight); nearer the previous
  end: no run this cycle (INACTIVE stays). Exact midpoint: next opening
  ([?] undocumented; pinned here, revisit with live access).
- Lookback (SEM-04): window -> status_at >= now - window. zero -> satisfied
  iff status_at is on `now`'s calendar day (midnight anchor). PENDING: Q2 --
  the switch ORACLE_ZERO_LOOKBACK_ANCHOR ("midnight" | "last_change") keeps
  the alternative reading alive: "last_change" reads zero-lookback as "the
  job's most recent transition satisfies the predicate" (no window at all).
- ON_ICE (SEM-05/SEM-20): every status/exitcode atom whose job is currently
  ON_ICE evaluates TRUE -- f()/t()/e() included, per SEM-05's blanket
  wording over SEM-20's "as though it succeeded" (DL-13, Q6-adjacent) --
  with lookback ignored; the iced job itself never starts (FORCE included);
  OFF_ICE does not re-evaluate (conditions must REOCCUR). Ice on a RUNNING
  job takes effect at completion: atoms read the real in-flight status
  until then ([?] unverified corner, documented).
- ON_HOLD (SEM-21): the held job does not start; nothing else changes;
  OFF_HOLD immediately re-evaluates that job's start (missed runs collapse
  to at most one).
- ON_NOEXEC (SEM-22): when the job would start, it bypasses to SUCCESS
  (STARTING/RUNNING skipped) and downstream runs normally. Members of an
  ON_NOEXEC box bypass as their conditions are met.
- Boxes: SEM-10 (members start when box RUNNING + own condition; at most
  once per box run), SEM-11 LITERAL (DL-13: the box cannot complete until
  every non-bypassed member has RUN to a terminal state -- a member whose
  condition never fires, or whose run_window deferred it, keeps the box
  RUNNING: the hung-box pattern is real behavior), SEM-12 (override
  gating: internal refs evaluated on the referenced member's transition;
  external/global refs evaluated only at member completion moments -- the
  hung-RUNNING pattern), SEM-13 (TERMINATED boxes are sticky until the
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
  start (must_complete). Absolute forms need the calendar the oracle does
  not own; scripts exercise relative forms.
- term_run_time (dossier ss5): control flow -- auto-TERMINATE when the run
  exceeds the limit, checked lazily as the clock advances.
- n_retrys: NOT modeled v1 (PENDING: Q4 -- the retry trigger set is open;
  modeling a guess would bake it in). auto_hold: member enters ON_HOLD when
  its box starts (dossier ss5 [C]).
- Undefined jobs in conditions evaluate FALSE forever (SEM-06). Cross-
  instance atoms (SEM-07) evaluate against instance-qualified pseudo-job
  entries in the status store, settable only by injected STATUS events with
  job "name^INST" -- the boundary is script-controlled.
- Non-goals v1 (ir-design ss7): machines/load (QUE_WAIT collapses to
  immediate RUNNING), definition-time mutations (SEM-16), agent failures.
"""

from __future__ import annotations

import heapq
from collections import deque
from datetime import datetime, time as dtime, timedelta
from typing import Literal

from pydantic import BaseModel

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
from dsl41.ir import CatalogIR, JobIR, Time

#: PENDING: Q2 -- zero-lookback anchoring; see module docstring.
ORACLE_ZERO_LOOKBACK_ANCHOR: Literal["midnight", "last_change"] = "midnight"

JobStatus = Literal[
    "INACTIVE",
    "STARTING",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
]

#: SEM-02: n() is true unless the job is in one of these (WAIT_REPLY/RESTART/
#: SUSPENDED are out-of-scope states the oracle never produces).
_N_FALSE_STATUSES: frozenset[str] = frozenset({"STARTING", "RUNNING"})

_TERMINAL: frozenset[str] = frozenset({"SUCCESS", "FAILURE", "TERMINATED"})

EventKind = Literal[
    "STATUS",
    "STARTJOB",
    "FORCE_STARTJOB",
    "SET_GLOBAL",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "KILLJOB",
    "TIMER",
    "MUST_START_ALARM",
    "MUST_COMPLETE_ALARM",
]


class Event(BaseModel):
    at: datetime
    kind: EventKind
    payload: dict[str, object] = {}

    def job(self) -> str | None:
        job = self.payload.get("job")
        return job if isinstance(job, str) else None


class TraceEntry(BaseModel):
    at: datetime
    job: str
    transition: str  # "OLD->NEW" or an out-of-band marker like "ON_ICE"
    cause: str


class JobRuntime(BaseModel):
    status: JobStatus = "INACTIVE"
    status_at: datetime | None = None
    exit_code: int | None = None
    run_number: int = 0
    on_ice: bool = False
    on_hold: bool = False
    on_noexec: bool = False


class StatusStore(BaseModel):
    """SEM-01 latching store: current recorded status, regardless of age."""

    job: dict[str, JobRuntime] = {}
    globals_: dict[str, str] = {}


class OracleError(ValueError):
    pass


class Oracle:
    """Deterministic interpreter over one CatalogIR (ir-design ss7)."""

    def __init__(self, catalog: CatalogIR) -> None:
        self.catalog = catalog
        self.store = StatusStore()
        for name in catalog.jobs:
            self.store.job[name] = JobRuntime()
        for name, value in catalog.globals_declared.items():
            self.store.globals_[name] = value
        self._trace: list[TraceEntry] = []
        self._emitted: list[Event] = []
        self._queue: deque[Event] = deque()
        self._timers: list[tuple[datetime, int, Event]] = []  # heap
        self._timer_seq = 0
        self._now: datetime | None = None
        #: members already run in the current box execution (SEM-10)
        self._box_ran: dict[str, set[str]] = {b: set() for b in self._boxes()}
        #: jobs started via a box run whose run_number ties them to it
        self._run_started_at: dict[str, datetime] = {}
        #: edge-trigger index (DL-13): entity key -> jobs whose `condition`
        #: references it. Keys: job names (incl. "name^INST"), "g:NAME".
        self._referencers: dict[str, list[str]] = {}
        for name, job_ir in catalog.jobs.items():
            cond = job_ir.sem.condition
            if cond is None:
                continue
            for key in _entity_keys(cond):
                self._referencers.setdefault(key, []).append(name)

    # ------------------------------------------------------------------ plumbing

    def _boxes(self) -> list[str]:
        return [n for n, j in self.catalog.jobs.items() if j.job_type == "BOX"]

    def _members(self, box: str) -> list[str]:
        return [n for n, j in self.catalog.jobs.items() if j.box.box_name == box]

    def trace(self) -> list[TraceEntry]:
        return [entry.model_copy() for entry in self._trace]  # no aliasing out

    def feed(self, ev: Event) -> list[Event]:
        """Process one injected event (+ due timers + cascade); return events
        emitted during this call. Feed times must be non-decreasing."""
        if self._now is not None and ev.at < self._now:
            raise OracleError(f"feed time went backwards: {ev.at} < {self._now}")
        emitted_start = len(self._emitted)
        # fire timers due strictly before this event first, in time order
        while self._timers and self._timers[0][0] <= ev.at:
            due, _, timer_ev = heapq.heappop(self._timers)
            self._now = due
            self._lazy_clock_checks()
            self._queue.append(timer_ev)
            self._drain()
        self._now = ev.at
        self._lazy_clock_checks()
        self._queue.append(ev)
        self._drain()
        return self._emitted[emitted_start:]

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
        self._timer_seq += 1
        heapq.heappush(self._timers, (at, self._timer_seq, ev))

    # -------------------------------------------------------------- status store

    def _runtime(self, job: str) -> JobRuntime:
        if job not in self.store.job:
            self.store.job[job] = JobRuntime()  # pseudo-entries: name^INST
        return self.store.job[job]

    def _set_status(
        self, job: str, status: JobStatus, cause: str, exit_code: int | None = None
    ) -> None:
        rt = self._runtime(job)
        old = rt.status
        rt.status = status
        rt.status_at = self._now
        if exit_code is not None:
            rt.exit_code = exit_code
        self._record(job, f"{old}->{status}", cause)
        self._emit("STATUS", job=job, status=status)
        self._after_transition(job, old, status)

    def _after_transition(self, job: str, old: str, new: str) -> None:
        job_ir = self.catalog.jobs.get(job)
        if job_ir is not None:
            box = job_ir.box.box_name
            if box is not None:
                self._on_member_transition(box, job, old, new)
        # SEM-01/dossier ss0: the transition wakes exactly the jobs whose
        # condition references this one (edge-triggered, DL-13)
        self._wake_referencers(job, cause=f"status of {job!r} changed to {new}")

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
            self._attempt_start(job, force=force, scheduled=True, cause=f"{kind} event")
        elif kind == "SET_GLOBAL":
            name = ev.payload.get("name")
            value = ev.payload.get("value")
            if not isinstance(name, str):
                raise OracleError("SET_GLOBAL requires payload.name")
            self.store.globals_[name] = str(value)
            self._wake_referencers(f"g:{name}", cause=f"SET_GLOBAL {name}")
        elif kind == "KILLJOB":
            job = self._required_job(ev)
            if self._runtime(job).status in ("STARTING", "RUNNING"):
                self._terminate(job, cause="KILLJOB")
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
            # SEM-09: the SUCCESS/FAILURE boundary is per-job max_exit_success
            ceiling = job_ir.sem.max_exit_success if job_ir is not None else 0
            status = "SUCCESS" if exit_code <= ceiling else "FAILURE"
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
        rt = self._runtime(job)
        if kind == "ON_ICE":
            rt.on_ice = True
            self._record(job, "ON_ICE", "sendevent ON_ICE")
            # SEM-20: downstream conditions now treat this job as satisfied
            self._wake_referencers(job, cause=f"{job!r} put ON_ICE")
        elif kind == "OFF_ICE":
            rt.on_ice = False
            self._record(job, "OFF_ICE", "sendevent OFF_ICE")
            # SEM-20: deliberately NO re-evaluation -- conditions must reoccur
        elif kind == "ON_HOLD":
            rt.on_hold = True
            self._record(job, "ON_HOLD", "sendevent ON_HOLD")
        elif kind == "OFF_HOLD":
            rt.on_hold = False
            self._record(job, "OFF_HOLD", "sendevent OFF_HOLD")
            # SEM-21: if conditions are already satisfied, run immediately
            self._attempt_start(job, force=False, scheduled=False, cause="OFF_HOLD")
        elif kind == "ON_NOEXEC":
            rt.on_noexec = True
            self._record(job, "ON_NOEXEC", "sendevent ON_NOEXEC")
        elif kind == "OFF_NOEXEC":
            rt.on_noexec = False
            self._record(job, "OFF_NOEXEC", "sendevent OFF_NOEXEC")

    # -------------------------------------------------------- condition evaluation

    def _atom_true(self, atom: StatusAtom | ExitCodeAtom) -> bool:
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
            if rt.exit_code is None or not self._lookback_ok(rt, atom.lookback):
                return False
            return compare_int(rt.exit_code, atom.op, atom.value)
        wanted = atom.status
        actual = rt.status
        if wanted == "DONE":
            hit = actual in _TERMINAL
        elif wanted == "NOTRUNNING":
            hit = actual not in _N_FALSE_STATUSES
        else:
            hit = actual == wanted
        if not hit:
            return False
        if wanted == "NOTRUNNING" and rt.status_at is None:
            return True  # never-run jobs are notrunning with no timestamp
        return self._lookback_ok(rt, atom.lookback)

    def _lookback_ok(self, rt: JobRuntime, lookback: Lookback | None) -> bool:
        if lookback is None or lookback.kind == "indefinite":
            return True
        if rt.status_at is None:
            return False
        assert self._now is not None
        if lookback.kind == "zero":
            # PENDING: Q2 -- midnight anchor is the documented default
            if ORACLE_ZERO_LOOKBACK_ANCHOR == "midnight":
                return rt.status_at.date() == self._now.date()
            return True  # "last_change": the latched status itself qualifies
        assert lookback.minutes is not None
        return rt.status_at >= self._now - timedelta(minutes=lookback.minutes)

    def _cond_true(self, cond: Cond) -> bool:
        if isinstance(cond, And):
            return all(self._cond_true(op) for op in cond.operands)
        if isinstance(cond, Or):
            return any(self._cond_true(op) for op in cond.operands)
        if isinstance(cond, Paren):
            return self._cond_true(cond.inner)
        if isinstance(cond, GlobalAtom):
            actual = self.store.globals_.get(cond.name)
            if actual is None:
                return False
            return compare_value(actual, cond.op, cond.value)
        return self._atom_true(cond)

    # --------------------------------------------------------------- job starting

    def _attempt_start(self, job: str, *, force: bool, scheduled: bool, cause: str) -> None:
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None:
            return  # starting an undefined job is a no-op for the oracle
        rt = self._runtime(job)
        if rt.status in ("STARTING", "RUNNING"):
            return
        if rt.on_ice:
            return  # SEM-20: iced jobs never run (FORCE included -- DL-13)
        if rt.on_hold and not force:
            return  # SEM-21 (FORCE on a held job: dossier is silent; force wins)
        if not force:
            if job_ir.schedule is not None and not scheduled:
                # SEM-30/31 (DL-13): a date_conditions job -- standalone OR
                # box member (the L013 double gate) -- starts only on its
                # script-injected schedule tick, never on condition edges.
                return
            box = job_ir.box.box_name
            if box is not None:
                if self._runtime(box).status != "RUNNING":
                    return  # SEM-10: member needs its box RUNNING
                if job in self._box_ran.get(box, set()):
                    return  # SEM-10: at most once per box execution
            if job_ir.sem.condition is not None and not self._cond_true(job_ir.sem.condition):
                # PENDING: Q3 -- scheduled trigger with false conditions is
                # abandoned (SEM-32 default reading), not queued
                return
        if not self._run_window_permits(job_ir, cause):
            return
        self._start(job, cause)

    def _run_window_permits(self, job_ir: JobIR, cause: str) -> bool:
        """SEM-33 closer-edge rule; True == start may proceed now."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return True
        assert self._now is not None
        lo, hi = schedule.run_window
        now_t = self._now.time()
        lo_t = _to_time(lo)
        hi_t = _to_time(hi)
        if lo_t <= hi_t:
            inside = lo_t <= now_t <= hi_t
        else:  # window crosses midnight
            inside = now_t >= lo_t or now_t <= hi_t
        if inside:
            return True
        next_open = _next_occurrence(self._now, lo_t)
        prev_close = _prev_occurrence(self._now, hi_t)
        to_open = next_open - self._now
        since_close = self._now - prev_close
        if to_open <= since_close:  # [?] midpoint tie -> next opening
            self._schedule_timer(
                next_open,
                Event(at=next_open, kind="TIMER", payload={"job": job_ir.name}),
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

    def _start(self, job: str, cause: str) -> None:
        job_ir = self.catalog.jobs[job]
        rt = self._runtime(job)
        if rt.on_noexec:
            # SEM-22: lifecycle bypass -- straight to SUCCESS, downstream normal
            self._set_status(job, "SUCCESS", cause=f"ON_NOEXEC bypass ({cause})")
            return
        self._arm_sla_and_term(job_ir)  # reads run_number before the bump
        rt.run_number += 1
        box = job_ir.box.box_name
        if box is not None:
            self._box_ran.setdefault(box, set()).add(job)
        if job_ir.job_type == "BOX":
            # Reset BEFORE the RUNNING transition: the transition's own
            # re-evaluation may already start members, and they must land in
            # the fresh per-run set (SEM-10 at-most-once bookkeeping).
            self._box_ran[job] = set()
        assert self._now is not None
        self._run_started_at[job] = self._now
        self._set_status(job, "STARTING", cause=cause)
        self._set_status(job, "RUNNING", cause="QUE_WAIT collapses to immediate (ss7 non-goal)")
        if job_ir.job_type == "BOX":
            self._on_box_started(job)

    def _on_box_started(self, box: str) -> None:
        for member in self._members(box):
            member_ir = self.catalog.jobs[member]
            if member_ir.sem.auto_hold:
                rt = self._runtime(member)
                if not rt.on_hold:
                    rt.on_hold = True
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
        if box_rt.status == "RUNNING" and new in _TERMINAL | {"RUNNING"}:
            if self._apply_box_overrides(box, box_ir, member, new):
                return
        if box_rt.status == "RUNNING" and self._all_members_done(box):
            self._fold_box_default(box, box_ir)
        elif box_rt.status not in ("RUNNING", "STARTING") and new in _TERMINAL:
            # SEM-15 [C]: a member change on a non-running box re-derives the
            # box's status (TERMINATED already returned above, SEM-13 sticky)
            self._idle_box_recompute(box, box_ir, cause=f"member {member!r} changed")

    def _idle_box_recompute(self, box: str, box_ir: JobIR, cause: str) -> None:
        """Derived-status recompute for a non-running box (SEM-15): pure
        function of current member statuses -- _box_ran does not apply
        outside a live run. Only fires when every member is terminal."""
        members = self._members(box)
        statuses = [self._runtime(m).status for m in members]
        if not members or not all(s in _TERMINAL for s in statuses):
            return
        for cond, target in (
            (box_ir.sem.box_success, "SUCCESS"),
            (box_ir.sem.box_failure, "FAILURE"),
        ):
            if cond is not None and self._cond_true(cond):
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
        member_completed = new in _TERMINAL
        for cond, target in (
            (box_ir.sem.box_success, "SUCCESS"),
            (box_ir.sem.box_failure, "FAILURE"),
        ):
            if cond is None:
                continue
            refs_member = member in _cond_job_names(cond)
            # internal ref: evaluate the moment the referenced job transitions;
            # external/global ref: evaluate only at member completion moments
            if not (refs_member or member_completed):
                continue
            if self._cond_true(cond):
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
        member has run (to a terminal state) or been bypassed (iced/noexec).
        A member whose condition never fires inside the run -- or whose
        run_window deferred it -- keeps the box RUNNING: the hung-box
        pattern is real behavior, not a defect to smooth over."""
        ran = self._box_ran.get(box, set())
        for member in self._members(box):
            rt = self._runtime(member)
            if rt.on_ice or rt.on_noexec:
                continue  # bypassed members do not block completion
            if member not in ran:
                return False  # not yet run this box execution (incl. held)
            if rt.status not in _TERMINAL:
                return False  # still STARTING/RUNNING
        return True

    def _fold_box_default(self, box: str, box_ir: JobIR) -> None:
        # SEM-12 third bullet: an unmet specified override suppresses the
        # corresponding default; if neither can fire the box stays RUNNING.
        members = [m for m in self._members(box) if m in self._box_ran.get(box, set())]
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

    def _arm_must_start(self, job: str) -> None:
        """SEM-34: MUST_START_ALARM if no new run has begun by tick+offset."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None or job_ir.schedule is None:
            return
        spec = job_ir.schedule.must_start
        if spec is None or spec.kind != "relative" or not spec.offsets_min:
            return
        assert self._now is not None
        deadline = self._now + timedelta(minutes=spec.offsets_min[0])
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
                deadline = self._now + timedelta(minutes=spec.offsets_min[0])
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
