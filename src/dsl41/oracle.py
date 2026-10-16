"""AutoSys semantics oracle: a deterministic discrete-event interpreter.

Normative spec: docs/ir-design.md ss7 (interface, determinism, non-goals) and every SEM entry in
docs/autosys-semantics.md, each mapped to a trace test (dossier ss8). Implementation order:
DL-03.

Execution model (dossier ss0): jobs are state machines; the event processor reacts to events and
re-evaluates the starting conditions of potentially affected jobs. A job starts when date/time
gates, `condition`, box-RUNNING and not-held/not-iced all hold.

Interpreter decisions (each has a trace test; PENDING items keep switches):
- Completion is script-driven: the oracle never invents run durations. A CMD/FW job completes
  when the script injects STATUS (explicit status or exit_code, SEM-09 boundary applied) or
  KILLJOB. The oracle emits only derived transitions: STARTING/RUNNING on start, bypass-SUCCESS
  for ON_NOEXEC (SEM-22), TERMINATED for terminator cascades (SEM-14), and box folds
  (SEM-11/12).
- One feed(event) drains a same-timestamp FIFO cascade queue: the injected event, then its
  consequences in deterministic order (jobs in catalog order, insertion sequence as tie-break).
  ir-design ss7's "(event kind priority, insertion order)" holds degenerately: the cascade is
  never a mixed-kind queue. Timers the oracle schedules (run_window reschedules, SLA deadlines)
  fire inside the next feed() whose `at` reaches them; feed times are non-decreasing. Two
  shell-facing extensions (runner-design ss3): next_timer_due() peeks the heap, and
  advance(now) fires due timers with no external event. Bisimulation pins feed-only ==
  advance+feed.
- Re-evaluation is edge-triggered (DL-13): a transition, SET_GLOBAL or ON_ICE wakes exactly the
  jobs whose `condition` references the changed entity. A self-referencing condition may
  re-trigger its own job (L010's concern, not the oracle's).
- Scheduling is script-driven: the oracle owns no calendar. The script injects STARTJOB where
  the scheduler would fire (start_times / start_mins ticks); a date_conditions job, standalone
  or box member (the SEM-31/L013 double gate), normally starts only on its tick. A tick blocked
  at a releasable gate (`condition` false, or ON_HOLD) arms the job (SEM-32 arm-and-wait, Q3
  resolved by citation, DL-58): condition edges and OFF_HOLD may then start it through the
  schedule gate, any start consumes the arm (one start per tick), and an unconsumed arm never
  expires. Mechanism and no-expiry boundary are confirmed by vendor support with reproduced
  tests. Ticks blocked at ON_ICE (SEM-20: conditions must reoccur), at box-not-RUNNING (member
  ticks count only while the box runs) or on an already-live job do not arm. run_window gating
  applies when a start goes through, armed or not. PENDING: Q3c -- the arm's box-run scope
  (unconsumed member arms die with the box run, DL-54) is in tension with one vendor aside
  ("JobB would start immediately after the next time its parent box starts"); the pin stands
  until a live test.
- run_window (SEM-33): a start attempt outside the window applies the closer-edge rule. Nearer
  the next opening: schedule a TIMER STARTJOB at window open (a box stays RUNNING overnight).
  Nearer the previous end: no run, and a standalone job moves to INACTIVE (DL-246). Inside a
  live box run the skip is a bypass (DL-154): the member goes INACTIVE, stays out of the ran
  set, and the SEM-11 fold completes past it (TechDocs 12.1, run_window page). A box start
  decides each waiting run_window member's disposition before its schedule gate (DL-246); the
  deferred STARTJOB is a start attempt with a tick's standing, through the normal gates
  (provisional). The exact midpoint goes to the next opening ([?] undocumented; revisit with
  live access). The window is read in the job's own `timezone` (SEM-35), compared on local wall
  time, and the queued timer goes back on the engine clock; the name resolves through the
  runner's ladder. A job with no `timezone:` compares in the constructor's `default_tz`, else on
  the engine clock (DL-155). Within two days of a one-hour DST change at 02:00 local, the window
  is built as concrete intervals following the vendor's DST rules (DL-249); other change shapes
  keep the wall-time comparison, unverified.
- Lookback (SEM-04): window -> status_at >= now - window. Zero -> satisfied iff the
  predecessor's last end (last_end_at) is at or after the evaluating job's last end (Q2a
  resolved by citation, DL-54). Both sides are end times, so an n() predecessor bounced to
  INACTIVE is not a fresh run; box overrides anchor on the box itself. Q2b resolved by citation
  (DL-58): a never-ended evaluator has no anchor and the atom is satisfied.
- ON_ICE (SEM-05/SEM-20, DL-243) splits by whether the atom carries a lookback qualifier. An
  atom with one, the zero form included, keeps the blanket pin: every status/exitcode atom on an
  iced predecessor evaluates TRUE, f()/t()/e() included, lookback ignored (DL-13, Q6-adjacent;
  KB 438836, DL-58). An ordinary atom (`atom.lookback is None`) follows the vendor's ON_ICE
  truth table (Start Conditions, AutoSys 24.2): success/done/notrunning TRUE,
  failure/terminated/exitcode FALSE. The two pages read f()/t()/exitcode differently and Q10
  (section 9) stays open over which a live instance follows. The `ice-lookback` switch selects
  the table's reading (DL-252): `ordinary` applies the table to lookback atoms too. ON_ICE sent
  to a STARTING or RUNNING job is ignored (DL-254): no flag, no wake, one EVENT_IGNORED trace
  line. ON_ICE on a QUE_WAIT job still dequeues it (DL-50). An iced job never starts on a plain
  STARTJOB; FORCE_STARTJOB on a non-live iced job clears the flag first (SEM-23), which
  narrows DL-13's "FORCE included". OFF_ICE does not re-evaluate (conditions must reoccur).
  Under the default `off-ice-in-running-box=next-run`, a member taken off ice while its box
  runs, before it ran there, sits that run out: a plain start is refused and the box completes
  without it (Start Conditions, Job States, AutoSys 24.2). `same-run` lets it start.
- ON_HOLD (SEM-21): the held job does not start; nothing else changes; OFF_HOLD immediately
  re-evaluates its start (missed runs collapse to at most one). ON_HOLD sent to a STARTING or
  RUNNING job is ignored like ON_ICE (DL-254); a QUE_WAIT job is held in the queue.
- ON_NOEXEC (SEM-22, DL-243): when the job would start, it bypasses to SUCCESS (STARTING/RUNNING
  skipped) and downstream runs normally. A BOX does not bypass: it goes RUNNING and its members
  bypass as their conditions are met, level by level. A member bypasses on its own flag or any
  containing box's. The bypass joins the box's ran set like a real start, so the SEM-11 fold
  waits for it and the once-per-box-run gate holds; it counts as the tick's run (Q3, DL-54), so
  no MUST_START_ALARM follows. A FAILURE or TERMINATED non-live, non-BOX job put ON_NOEXEC
  moves to INACTIVE through DL-242's operator-INACTIVE path, exit code cleared (Job States
  page). This is an event-time transition: the stored row changes, a RUNNING box member
  resolves as for an operator's CHANGE_STATUS INACTIVE, and referencers wake normally. SUCCESS
  is left alone. ON_NOEXEC sent to a non-box job that is STARTING, RUNNING or ON_ICE, or to a
  box that is RUNNING or ON_ICE or contains (at any depth) a job that is iced, STARTING,
  RUNNING or QUE_WAIT, is ignored (DL-254): no flag, no transition, one EVENT_IGNORED trace
  line. A QUE_WAIT job leaves the queue and moves to INACTIVE the same way. ON_NOEXEC
  supersedes ON_HOLD: the hold is cleared and recorded as an OFF_HOLD. A job released from a
  hold or the queue retries its start. On a box it is CHANGE_STATUS INACTIVE for the box (the
  SEM-18 cascade), and every contained job at every level takes the flag; OFF_NOEXEC on a box
  clears them all (DL-254).
- initial_status (SEM-24, DL-18): definition-time ON_HOLD/ON_ICE/ON_NOEXEC seeds the flag before
  the first event, with no trace entry. INACTIVE is the default.
- Boxes:
  SEM-10: members start when the box is RUNNING and their own condition holds, at most once per
  box run; a box start resets every contained job that is not live to INACTIVE (DL-242).
  SEM-11 literal (DL-13): the box cannot complete until every member that is not ON_ICE has run
  to a terminal state, been bypassed to SUCCESS by SEM-22, or been resolved to INACTIVE inside
  the run by a window skip (SEM-33, DL-154) or an injected STATUS INACTIVE (DL-242). A member
  whose condition never fires, or whose run_window deferred it, keeps the box RUNNING.
  SEM-12: override gating evaluates internal refs on the referenced member's transition and
  external/global refs only at member completion moments. "Inside" is transitive (as in
  derive._is_inside), so every ancestor box evaluates its overrides.
  SEM-13: TERMINATED boxes are sticky until the next box start.
  SEM-14: a box_terminator member ending FAILURE or TERMINATED kills the box (TERMINATED only
  under the default `box-terminator-on-terminated=true`); job_terminator members die with the
  box.
  SEM-15: a terminal member transition, or an injected INACTIVE on a member, re-derives a
  non-running, non-TERMINATED box's status once every member that is not INACTIVE is terminal.
  INACTIVE members are ignored (DL-242), and under the default `idle-box-iced-member=ignore`
  so is an iced member out of the run.
  SEM-17: a member box starting is a member start; folds recurse; the ACTIVATED label is
  unmodeled, so a waiting member reads INACTIVE.
  SEM-18: an injected INACTIVE on a box cascades to every job it contains (DL-242).
- FORCE_STARTJOB (SEM-23, DL-243): overrides false conditions, ON_HOLD and the box-RUNNING gate.
  A non-live job that is ON_ICE or ON_HOLD is a non-executable state the force clears first
  (sendevent Start Jobs page), recorded as an OFF_ICE/OFF_HOLD sendevent with a cause naming
  FORCE_STARTJOB. The flag stays cleared even if a later gate (`run_window`) refuses the start.
  A job already STARTING/RUNNING/QUE_WAIT is still refused (concurrent runs of one job are
  unsupported). ON_NOEXEC is untouched by FORCE. Forced runs emit normal statuses and satisfy
  downstream latches.
- An injected STATUS may overwrite a terminal status (the CHANGE_STATUS analog): a
  script-authoring hazard, documented and not guarded.
- must_start_times / must_complete_times (SEM-34): alarms only, never control flow. The STARTJOB
  tick arms both halves (DL-248), even when the start is abandoned or deferred. A relative
  deadline is tick+offset. An absolute one is the slot's must time on the tick's local calendar
  day, hours 24-71 on the days after, with the vendor's DST rules (DL-253). must_start alarms
  iff no new run began by the deadline. must_complete alarms iff the first run to begin after
  the tick has not ended by it; a late start does not move the deadline, and a FORCE_STARTJOB,
  which is no tick, arms none. One deadline of each kind is pending per job: a tick on a live
  job, or while an earlier deadline of that kind is pending, arms none (DL-248, DL-253). N
  values against N start_times pair by position; start_mins takes a single relative offset
  (DL-248). A single offset broadcasts over every start time, and an instant that matches no
  start time keeps the first offset. An absolute form arms nothing for such an instant.
- term_run_time (dossier ss5): control flow; auto-TERMINATE when the run exceeds the limit,
  checked lazily as the clock advances.
- n_retrys: Q4 resolved (DL-53). The trigger set is FAILURE-only application failures
  (TERMINATED does not restart; system failures restart via the scheduler's MaxRestartTrys).
  Retry modeling is out of scope v1 as a recorded DL-53 decision. auto_hold: a member enters
  ON_HOLD when its box starts (dossier ss5 [C]).
- Undefined jobs in conditions evaluate FALSE forever (SEM-06). Cross-instance atoms (SEM-07)
  evaluate against instance-qualified pseudo-job entries in the status store, settable only by
  injected STATUS events with job "name^INST".
- Resources/load (DL-50): a job that clears its start gate acquires an atomic full demand vector
  (machine-load slots, job_load vs machine max_load; `resources:` semaphore units, QUANTITY vs
  insert_resource `amount`) before RUNNING. If any bucket is short it enters QUE_WAIT and is
  admitted later in (priority, enqueue-seq, name) order when a holder's terminal release frees
  room. All-or-nothing acquire: a job that holds nothing from an earlier run never holds and
  waits; QUANTITY=1 shared is a mutex. A holder of units kept from an earlier run (DL-256)
  queues with them, and with DL-255's priority block two such holders can wait on each other
  for ever: a stated limit, broken only by an operator act (DL-286). Priority blocking can
  starve a lower-priority job while a higher-priority waiter cannot fit; preflight refuses a
  load that can never fit, which bounds that wait to feasible loads (DL-247). Only a job with a
  positive priority checks machine load; an unset or zero priority and a FORCE_STARTJOB skip
  the check, and every start holds its load. A job waiting for load blocks every lower positive
  priority on the same machine, on a fresh start and in the readmission scan (DL-247). A job
  past its load check and short on a named resource blocks every lower positive priority that
  names any resource it names, forced or not (DL-255); a queued job holds no load. A job
  leaving the queue and a waiter put on hold wake the queue, since each can lift a block; a box
  leaving RUNNING, and a start or enqueue that takes machine load, owe an admit-only scan. The
  input pays it once fully applied, after its releases and every referencer it woke, and it
  leaves the stopped box's queued members queued. res_type sets the default release (R/absent
  free-on-success, D never, T a level gate that never acquires); per-request FREE overrides it
  (Y success-only, N never, A unconditional). An omitted FREE on a renewable reads the
  `renewable-free` switch: Y, the vendor default, or A, free on every completion (DL-256). A
  renewable's units its policy does not free stay held on the job's row after the run, until
  RELEASE_RESOURCE or the job's next run, which re-uses them; a depletable's are spent. A
  FORCE_STARTJOB of a FAILURE or TERMINATED holder starts on its held units with no admission
  check (DL-256). A queued job re-validates box-RUNNING/ice/hold at admission. Whether it
  re-checks its starting conditions is the `queued-recheck` switch, the vendor's
  EvaluateQueuedJobStarts (DL-257): at the default 0 they are not re-checked (Qr6, decided); 1
  re-checks the condition, run_window and exclude_calendar; 2 also the day's run_calendar or
  days_of_week. A job that fails leaves the queue for INACTIVE, its arm cleared, and waits for
  its next start time; a member of a running box stays unresolved, the ACTIVATED analog. A
  run_window failure takes DL-246's disposition at that instant, and a day failure of a job the
  scheduler never ticks is deferred to its next eligible opening. The runner's preflight (DL-50)
  enforces unsized, unknown-res_type and malformed shapes; the oracle models only sizeable
  buckets, so oracle-direct over an unrefused bad catalog runs unthrottled.
- Non-goals v1: definition-time mutations (SEM-16, including mid-run update_resource
  replenishment of depletables) and agent failures.

Structure (docs/statechart-engine.md ss5, ss8, ss11, ss16; DL-341, DL-350). `Oracle` is built in
layers, one module each: `oracle_reads.OracleReads` (the store, the trace and the reads),
`oracle_schedule.OracleSchedule` (the clock's reads and the timers),
`oracle_cascade.OracleCascade` (the cascade's moments), `oracle_writers.OracleWriters` (each
fired row's writes), and this module's `Oracle` (the inputs, the hook and the checks). The
payloads are in `oracle_facts` and `oracle_intents`.

Every input that moves a job row is a root item on the job's instance of the `job` statechart:
STATUS, STARTJOB, FORCE_STARTJOB, a window timer's replay, KILLJOB, the TIMER input, the flag
inputs and RELEASE_RESOURCE. SET_GLOBAL's referencers and the period opening's removed holders
are one root item each, and the input's owed scan is a root item of its own after them
(DL-255). The deadline alarms move no state. Each job row has one `job` instance on the state
owner's executor (`RuntimeState.machines`) under the row's `job_key`, with `JobData` from the
catalog (`job_data`). The owner restores them from the rows
(`RuntimeState.keep_job_instances`), a fork copies them (`Executor.fork`, DL-339), and a row
read into existence gets its instance as it is created, outside any macrostep.

A macrostep runs the whole cascade on the executor's worklist, depth first (`oracle_cascade`).
The rows' actions are pure. The executor calls the hook (`_perform`) with each outcome, before
the next item pops; it writes what the fired rows reached, then applies the intents their
actions reported (`oracle_writers`). The start bound is job.100's: the attempt's ancestry holds
two `start admitted` items of the job (DL-304). A cascade that does not end takes items: the
hook counts a root dispatch's items and stops it at the item cap (`item_cap`, DL-351) with a
`CascadeDepthError`. A fact's own recursion past the recursion limit, such as a deep condition,
raises the same error at the root.

At each macrostep's end, every job an item reached has its row project to its instance's
configuration; at each input's commit, every job the input touched does
(`RuntimeState.commit_input`). An exception out of a macrostep leaves the instances rebuilt from
the rows, as the oracle has no rollback: the input is refused, or quarantined with its time half
kept.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from typing import Final, cast

from dsl41.ir import CatalogIR, Semantics
from dsl41.job_chart import JOB
from dsl41.job_chart_events import (
    DISARM,
    FORCE_STARTJOB,
    KILLJOB,
    OFF_HOLD,
    OFF_ICE,
    OFF_NOEXEC,
    ON_HOLD,
    ON_ICE,
    ON_NOEXEC,
    PERIOD_OPENING,
    RELEASE_RESOURCE,
    RUN_WINDOW,
    SCAN,
    START,
    STARTJOB,
    STATUS,
    TERM_RUN_TIME,
    Deadline,
    Payload,
)
from dsl41.job_chart_rows import row_configuration
from dsl41.oracle_facts import NoexecFacts, RowUnits, StartFacts, job_data
from dsl41.oracle_intents import Armed, Entered, Intent, Program, StatusInput
from dsl41.oracle_schedule import DEFERRED_RESCAN_KEY, DEFERRED_TIMER_KEY
from dsl41.oracle_state import (
    INJECTABLE_STATUSES,
    LIVE,
    CarriedRows,
    CascadeDepthError,
    Event,
    JobRuntime,
    OracleError,
    TraceEntry,
)
from dsl41.oracle_writers import OracleWriters
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Outcome, Statechart, StatechartError


def _note_cleanup(exc: StatechartError, cleanup: Exception) -> None:
    """A batch's cleanup raised `cleanup` while a `StatechartError` was leaving it.

    The statechart error is an assertion failure, not a fault to classify
    (docs/statechart-engine.md section 5), so it leaves unchanged with the cleanup's finding as
    a note. Any other exception in flight gives way to the cleanup's.
    """
    exc.add_note(f"the batch's cleanup then raised {type(cleanup).__name__}: {cleanup}")


class InputBatch:
    """One admitted input, applied as one store transaction.

    The frozen admission order (concurrency-model ss4) commits the time observation
    `TimeAdvanced(at)` and the attempt as a unit, and ss3 puts one revision on that unit.
    Entering applies the time half; timers due at or before `at` fire here, ahead of the shell's
    gate (ss4 step 5). `feed` applies the attempt. Leaving commits whether or not the attempt was
    fed or the drain raised: the oracle has no rollback, so whatever changed is durable (DL-87).

    Hold one open only when a decision sits between the halves: the engine's stale-completion
    gate does, and S3's preconditions will.
    """

    def __init__(self, oracle: Oracle, at: datetime) -> None:
        self._oracle = oracle
        self._at = at
        #: Events emitted across the whole batch, in order; set at commit.
        self.emitted: list[Event] = []
        #: Entity key -> its revision after this input: the ss4 step-7 record of what the input
        #: moved, and the only place the changed set leaves the owner.
        self.revisions: dict[str, int] = {}
        self._emitted_start = 0

    def __enter__(self) -> InputBatch:
        oracle = self._oracle
        if oracle._now is not None and self._at < oracle._now:
            raise OracleError(f"input time went backwards: {self._at} < {oracle._now}")
        self._emitted_start = len(oracle._emitted)
        oracle.store.begin_input()
        try:
            # DL-256: a period's first input gives back the units of removed jobs first, at the
            # opening instant, before any timer fires
            oracle._release_removed_holders()
            # fire timers due at or before this input, in time order
            oracle._fire_timers_due(self._at)
            oracle._now = self._at
            oracle._lazy_clock_checks()
            oracle._drain()  # checks-then-drain adjacency
        except BaseException as exc:
            try:
                self._commit()
            except Exception as cleanup:
                if not isinstance(exc, StatechartError):
                    raise
                _note_cleanup(exc, cleanup)
            raise
        return self

    def feed(self, ev: Event) -> None:
        """Apply the attempt half. Its stamp is the batch's time observation."""
        if ev.at != self._at:
            raise OracleError(f"attempt stamped {ev.at} in a batch opened at {self._at}")
        self._oracle._queue.append(ev)
        self._oracle._drain()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self._commit()
        except Exception as cleanup:
            if not isinstance(exc, StatechartError):
                raise
            _note_cleanup(exc, cleanup)

    def _commit(self) -> None:
        oracle = self._oracle
        changed = oracle.store.commit_input()
        self.revisions = {key: oracle.store.revision(key) for key in changed}
        self.emitted = oracle._emitted[self._emitted_start :]


#: The item cap (DL-351): a root dispatch takes fewer items than four times the square of the
#: estate's job count, within `ITEM_FLOOR` and `ITEM_CEILING` (the floor binds up to 158 jobs,
#: the square up to 316, the ceiling above). A finite cascade moves each job a bounded number of
#: times and each move wakes at most the other jobs, so its items grow with the square. The
#: largest legitimate dispatches measured at 41 to 1,000 jobs are a fan-in (33,799 items at 200
#: jobs, 127,599 at 400), a queue scan readmitting blocked waiters (81,991 at 400), DL-304's box
#: ring (108 per job) and its nested terminator chain (7,632 at the lowering's 64 levels). The
#: margin over the worst is 23x at 41 jobs, 4.7x at 200 and 3.1x at 400.
ITEMS_PER_JOB_SQUARED: Final = 4

#: The cap of an estate under 159 jobs: a loop there stops in a few seconds.
ITEM_FLOOR: Final = 100_000

#: The ceiling's rule: the largest round count of items whose DL-348 loop stops within half the
#: lease lapse (the TTL less the renewal interval, 40 s, so 20 s) at every measured size up to
#: 1,000 jobs on the reference host (4.5 s at 200 jobs, 14.6 s at 400, 17.0 s at 1,000). The dry
#: apply runs on the engine's loop, so a longer cascade stops the lease's renewal and the
#: supervisor's deadman ends the host's running jobs. The loop's cost per item grows with the
#: job's owed settlements and the catalog's size (`settlement_owed`, `_members`), so the ceiling
#: is re-derived from a new measurement whenever that cost changes, never raised by hand.
ITEM_CEILING: Final = 400_000

#: The recursion limit the oracle runs under (DL-351): what lowers must evaluate. The deepest
#: condition the lowering accepts at Python's default limit is grouped 985 levels deep (DL-20)
#: and takes about 1,004 frames to evaluate; the engine's and the replays' call sites add their
#: own stack. Each construction raises the process's limit to this and never lowers it. A logged
#: input is far inside it (a run root stores no condition deeper than about 250 levels,
#: `period.catalog_hash_v2`); the headroom serves the paths that hash no catalog: equiv and a
#: directly built oracle.
RECURSION_LIMIT: Final = 3000


def item_cap(jobs: int) -> int:
    """The item cap of a root dispatch over an estate of `jobs` jobs."""
    return min(max(ITEM_FLOOR, ITEMS_PER_JOB_SQUARED * jobs * jobs), ITEM_CEILING)


#: The flag inputs, dispatched to the job's instance (DL-344).
_FLAG_INPUTS: Final = frozenset({ON_ICE, OFF_ICE, ON_HOLD, OFF_HOLD, ON_NOEXEC, OFF_NOEXEC, DISARM})


def listed(active: frozenset[str]) -> str:
    return ", ".join(sorted(active))


class Oracle(OracleWriters):
    """Deterministic interpreter over one CatalogIR (ir-design ss7).

    Every job move is decided by the `job` chart, its cascade runs on the executor's worklist,
    and the hook writes (DL-350).
    """

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        default_tz: str | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        if sys.getrecursionlimit() < RECURSION_LIMIT:
            sys.setrecursionlimit(RECURSION_LIMIT)
        super().__init__(
            catalog,
            carried=carried,
            tz_aliases=tz_aliases,
            default_tz=default_tz,
            semantics=semantics,
        )
        self._queue: deque[Event] = deque()
        #: DL-256: carried rows of jobs this catalog does not define that still hold units
        #: while not live. RELEASE_RESOURCE cannot reach them; the period's first input gives
        #: them back (`_release_removed_holders`). A live one is refused at the boundary
        #: (period-model ss10.1).
        self._opening_release: list[str] = sorted(
            name
            for name, row in (carried.jobs.items() if carried is not None else ())
            if name not in catalog.jobs and row.reservations and row.status not in LIVE
        )
        #: The items the root dispatch in flight has taken, and its cap.
        self._items = 0
        self._cap = item_cap(len(catalog.jobs))
        self._rooted: tuple[str, str] | None = None
        #: The instances the macrostep's items reached, checked at its end.
        self._reached: set[str] = set()
        self.store.keep_job_instances(self._job_instance)

    def fork(self) -> Oracle:
        """`OracleCascade.fork`, with the twin's store asking the twin for its job instances' data
        and the twin's own input queue, removed holders and reached-instance set (DL-350)."""
        twin = cast(Oracle, super().fork())
        twin.store.job_instance = twin._job_instance
        twin._queue = deque(self._queue)
        twin._opening_release = list(self._opening_release)
        twin._reached = set()
        return twin

    def _job_instance(self, job: str, row: JobRuntime) -> tuple[Statechart, frozenset[str], object]:
        return JOB, row_configuration(row), job_data(self, job)

    # ---- the public input API

    def batch(self, at: datetime) -> InputBatch:
        """Open one admitted input at `at` (concurrency-model ss4).

        Use it only when a decision sits between the time observation and the attempt, as the
        engine's gate does; otherwise feed() and advance() do the same.
        """
        return InputBatch(self, at)

    def feed(self, ev: Event) -> list[Event]:
        """Process one injected event (+ due timers + cascade); return the events emitted.
        Feed times must be non-decreasing."""
        with self.batch(ev.at) as batch:
            batch.feed(ev)
        return batch.emitted

    def advance(self, now: datetime) -> list[Event]:
        """Fire timers due <= now without an external event (runner-design ss3).

        The clock has then reached `now`, so a later feed()/advance() before `now` errors.
        Bisimulation (runner-design ss13) pins that feed-only and advance+feed trace identically.
        """
        with self.batch(now) as batch:
            pass  # a standalone time observation is an input (concurrency-model ss4)
        return batch.emitted

    def run_script(self, events: list[Event]) -> list[TraceEntry]:
        for ev in events:
            self.feed(ev)
        return self.trace()

    def _fire_timers_due(self, at: datetime) -> None:
        while (popped := self.store.pop_timer_due(at)) is not None:
            due, timer_ev = popped
            self._now = due
            self._lazy_clock_checks()
            self._queue.append(timer_ev)
            self._drain()

    def _drain(self) -> None:
        while self._queue:
            self._dispatch(self._queue.popleft())
            # DL-255: the input's waiter step, after all its referencers
            self._run_owed_scan()

    @property
    def opening_release_owed(self) -> bool:
        """True while a removed job's held units wait for the period's first input (DL-256). The
        engine admits a time observation for it at opening."""
        return bool(self._opening_release)

    # ---- the inputs: each a root item

    def _dispatch(self, ev: Event) -> None:
        """Every input that moves a job row is a root item on the job's instance; SET_GLOBAL's
        referencers' attempts are one each. A start input arms its deadlines first (DL-248)."""
        kind = ev.kind
        if kind == STATUS:
            self._handle_status(ev)
        elif kind == KILLJOB:
            job = self._required_job(ev)
            self._root(job, ChartEvent(kind, Armed(units=RowUnits(self, job))))
        elif kind == RELEASE_RESOURCE:
            job = self._required_job(ev)
            self._root(job, ChartEvent(kind, Payload(units=RowUnits(self, job))))
        elif kind == "TIMER" and self._handle_timer(ev):
            return
        elif kind in _FLAG_INPUTS:
            job = self._required_job(ev)
            payload: Payload | NoexecFacts = Payload(units=RowUnits(self, job))
            if kind == ON_NOEXEC:
                payload = NoexecFacts(self, job)
            elif kind == DISARM:
                payload = Armed(units=RowUnits(self, job))
            self._root(job, ChartEvent(kind, payload))
        elif kind in (STARTJOB, FORCE_STARTJOB, "TIMER"):
            job = self._required_job(ev)
            if kind == STARTJOB:
                # SEM-34: the schedule tick arms both deadlines before the attempt, whether or
                # not the start succeeds (DL-248)
                if (timer := self._tick_deadline(job, "must_start")) is not None:
                    self._schedule_timer(timer.at, timer)
                if (timer := self._tick_deadline(job, "must_complete")) is not None:
                    self._schedule_timer(timer.at, timer)
            # DL-68: a sourced event names its trigger; a window-postponed start replays the
            # original start's provenance (SEM-33)
            cause = f"{kind} event ({ev.source})" if ev.source else f"{kind} event"
            deferred = ev.payload.get(DEFERRED_TIMER_KEY)
            if isinstance(deferred, str):
                cause = f"run_window-deferred {deferred}"
            self._attempt_root(job, RUN_WINDOW if kind == "TIMER" else kind, cause)
        elif kind == "SET_GLOBAL":
            name = ev.payload.get("name")
            if not isinstance(name, str):
                raise OracleError("SET_GLOBAL requires payload.name")
            self.store.set_global(name, str(ev.payload.get("value")))
            for referencer in self._referencers.get(f"g:{name}", ()):
                self._attempt_root(referencer, START, f"SET_GLOBAL {name}")
        else:
            raise OracleError(f"uninjectable event kind {kind!r}")

    def _root(self, job: str, event: ChartEvent, cause: str = "") -> None:
        """One root item on the job's instance, its row read into existence first, and its whole
        cascade; the hook writes as each item ends and counts it against the item cap."""
        self._runtime(job)
        self._items = 0
        self._rooted = (job, f"{event.name} ({cause})" if cause else event.name)
        try:
            self.store.machines.dispatch(self.store.job_key(job), event, self._perform)
            self._check_reached()
        except BaseException as exc:
            self._recover()
            if isinstance(exc, RecursionError) and not isinstance(exc, CascadeDepthError):
                # the cascade takes no frame per start: only a fact's own recursion (a deep
                # condition) meets the limit
                raise CascadeDepthError(job, cause or event.name, sys.getrecursionlimit()) from exc
            raise
        finally:
            self._items, self._rooted = 0, None

    def _check_reached(self) -> None:
        """At a macrostep's end, every job an item reached has its row project to its instance's
        configuration: the commit sees only the rows the input touched."""
        reached, self._reached = self._reached, set()
        rows = self.store.job
        for key in sorted(reached):
            job = key.partition(":")[2]
            active = self.store.machines.active(key)
            if job in rows and active != row_configuration(rows[job]):
                raise StatechartError(
                    f"job {job!r}: at the macrostep's end its instance is in {listed(active)},"
                    f" and its row projects to {listed(row_configuration(rows[job]))}"
                )

    def _recover(self) -> None:
        """After a macrostep that raised, rebuild the instances from the rows (no write is
        undone) and reset the cascade's bookkeeping (`_cascade_idle`), dropping an owed scan."""
        self.store.restore_instances()
        self._cascade_idle()
        self._reached = set()

    def _attempt_root(self, job: str, relay: str, cause: str) -> None:
        """An attempt outside any macrostep: an input's own, relayed through `relay`, or
        SET_GLOBAL's (`start` itself). A job with neither a catalog entry nor a row does nothing."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None and job not in self.store.job:
            return
        self._root(job, ChartEvent(relay, StartFacts(self, job, job_ir, cause=cause)), cause)

    def _handle_status(self, ev: Event) -> None:
        job = self._required_job(ev)
        status = ev.payload.get("status")
        exit_code = ev.payload.get("exit_code")
        success = False
        if status is None:
            if not isinstance(exit_code, int):
                raise OracleError("STATUS requires payload.status or integer payload.exit_code")
            # SEM-09 (DL-33), read only for a payload with no status
            job_ir = self.catalog.jobs.get(job)
            success = (job_ir.sem if job_ir is not None else Semantics()).exit_is_success(exit_code)
        elif not isinstance(status, str) or status not in INJECTABLE_STATUSES:
            raise OracleError(f"unknown status {status!r}")
        code = exit_code if isinstance(exit_code, int) else None
        payload = StatusInput(status, success, code=code, units=RowUnits(self, job))
        self._root(job, ChartEvent(STATUS, payload))

    def _handle_timer(self, ev: Event) -> bool:
        """The TIMER input (DL-343); False for a window timer's replay of a start, which
        `_dispatch` relays. A deadline alarm moves no state and stays `_dispatch_timer_check`.

        A window timer reads its own facts first: a deferral that does not belong to its box
        run, or a scan continuation the job has moved on from, is refused with its refusal line;
        a live continuation resumes the scan.
        """
        check = ev.payload.get("check")
        if check == "term_run_time":
            job = self._required_job(ev)
            current = ev.payload.get("run") == self._runtime(job).run_number
            self._root(job, ChartEvent(TERM_RUN_TIME, Deadline(current, units=RowUnits(self, job))))
            return True
        if check is not None:
            self._dispatch_timer_check(ev)
            return True
        deferred = ev.payload.get(DEFERRED_TIMER_KEY)
        if not isinstance(deferred, str):
            return False
        job = self._required_job(ev)
        stale = self._deferral_is_stale(ev)
        rescan = DEFERRED_RESCAN_KEY in ev.payload
        if stale is None and not rescan:
            return False
        superseded = stale is None and self._rescan_superseded(job, ev.payload[DEFERRED_RESCAN_KEY])
        if stale is None:
            why = f"the job has moved on since the scan stopped -- no effect (DL-257; {deferred})"
        else:
            why = f"{stale} (run_window-deferred {deferred})"
        facts = StartFacts(
            self,
            job,
            self.catalog.jobs.get(job),
            cause=deferred,
            stale=stale is not None or superseded,
            rescan=rescan,
            refusal=why,
        )
        self._root(job, ChartEvent(RUN_WINDOW, facts))
        return True

    def _run_owed_scan(self) -> None:
        """DL-247, DL-255: the input's owed admit-only scan, a root item of its own after the
        input's macrosteps."""
        while self._scan_owed and not self._in_wake:
            self._scan_owed = False
            if self._pool.has_priority_waiters(self.store.job):
                waiters = self._pool.sorted_waiters(self.store.job)
                job = waiters[0] if waiters else next(iter(self.store.job))
                program = Program(f"{SCAN} owed by the input", self._scan(keep_stopped=True))
                self._root(job, ChartEvent(SCAN, program))

    def _release_removed_holders(self) -> None:
        """DL-256: at the period's opening, each carried row whose job the catalog does not
        define and that holds units gives them back (job.308), each a root item; then the
        waiters wake."""
        owed, self._opening_release = self._opening_release, []
        for job in owed:
            self._root(job, ChartEvent(PERIOD_OPENING))
        if owed:
            program = Program(f"{SCAN} after the opening", self._scan(keep_stopped=False))
            self._root(owed[0], ChartEvent(SCAN, program))

    def _perform(self, outcome: Outcome) -> None:
        """The executor's hook (statechart-engine section 5): each fired row's writes in firing
        order within the microstep, the status its entry reported, then the other intents its
        actions reported. A fired row with no write here is a statechart error. The item that
        reaches the cap ends the dispatch."""

        self._items += 1
        if self._items >= self._cap:
            assert self._rooted is not None, "an item outside a root dispatch"
            jobs = len(self.catalog.jobs)
            raise CascadeDepthError(*self._rooted, self._cap, jobs=jobs)
        key = str(outcome.key)
        self._reached.add(key)
        self._write_rows(key.partition(":")[2], outcome)
        for intent in outcome.intents:
            if not isinstance(intent, Entered):
                cast(Intent, intent).apply(self)
