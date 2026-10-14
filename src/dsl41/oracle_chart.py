"""The second oracle: `Oracle` with its job rows driven by the `job` statechart
(docs/statechart-engine.md §5, §11, §16; DL-341, DL-350).

`ChartOracle` is built beside the old oracle under the old-versus-new
harness (tests/oracle_lockstep.py, DL-338), which names it as
`dsl41.oracle_chart:ChartOracle`. Every input that moves a job row is the
chart's: STATUS (DL-341), STARTJOB, FORCE_STARTJOB and a window timer's
replay (DL-342), KILLJOB and the TIMER input (DL-343), the flag inputs
(DL-344), RELEASE_RESOURCE (DL-345), SET_GLOBAL's wakes and the period
opening. The deadline alarms move no state and stay `Oracle`'s.

Instances. Each job row has one `job` instance on the state owner's executor
(`RuntimeState.machines`), under the row's `job_key`, with `JobData` from the
catalog (`job_data`). The owner restores them from the rows in
`rebuild_machines`, which the constructor has it run
(`RuntimeState.keep_job_instances`) and an install and a witness run; a fork
copies them (`Executor.fork`, DL-339); a row read into existence gets its
instance as it is created, outside any macrostep.

The cascade (DL-350). An input is one root item on the job's instance,
or one per job where today's code loops over jobs outside any transition
(SET_GLOBAL's referencers, the period opening's removed holders), and the
input's owed scan is a root item of its own after them (DL-255). A macrostep
runs the whole cascade on the executor's worklist, depth first, in today's
order (`oracle_chart_cascade`). The rows' actions are pure; this oracle is
the tier whose hook (`_perform`) the executor calls with each outcome, before
the next item pops: it writes what the fired rows reached, with the store's
verbs and the old tables' transitions, each status with today's trace line
and STATUS emission, in today's order within the microstep, then applies the
intents the actions reported. A fired row it has no write for is a
statechart error. The chart oracle calls none of the old recursive bodies.

The start bound is job.100's: the attempt's ancestry holds two `start
admitted` items of the job (DL-304). The cascade takes no Python frame per
nested start, so the class's `FRAMES_PER_NESTED_START` is 0; a RecursionError
can come only from a fact's own recursion (a deep condition), and the root
dispatch names it as a `CascadeDepthError`.

At each input's commit, every job the input touched has its row project to
its instance's configuration (`RuntimeState.commit_input`). An exception out
of a macrostep leaves the instances rebuilt from the rows, as the oracle has
no rollback: the input is refused, or quarantined with its time half kept.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Final, cast

from dsl41.capacity import machine_load, to_reservations
from dsl41.ir import CatalogIR, Semantics
from dsl41.job_chart import (
    DISARM,
    FORCE_STARTJOB,
    JOB,
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
    Settlement,
    Statechart,
    projection,
    row_configuration,
)
from dsl41.oracle import DEFERRED_RESCAN_KEY, DEFERRED_TIMER_KEY
from dsl41.oracle_chart_agreement import listed
from dsl41.oracle_chart_cascade import (
    AdmissionFacts,
    Batch,
    CascadeOracle,
    FlagFacts,
    Intent,
    MemberEnd,
    Move,
    Program,
    Rule,
    RunStart,
    Settle,
    Starting,
    StatusInput,
)
from dsl41.oracle_chart_facts import (
    FoldFacts,
    NoexecFacts,
    RecomputeFacts,
    RowUnits,
    StartFacts,
    Started,
    job_data,
)
from dsl41.oracle_state import (
    ARM,
    AUTO_HOLD,
    BOX_CASCADE,
    BOX_END_DISARMS,
    BOX_FAILURE_OVERRIDE,
    BOX_FOLD_FAILURE,
    BOX_FOLD_SUCCESS,
    BOX_NOEXEC,
    BOX_NOEXEC_OFF,
    BOX_RESET,
    BOX_RUN,
    BOX_STATUS_INACTIVE,
    BOX_SUCCESS_OVERRIDE,
    BOX_TERMINATOR,
    FORCE_CLEARS_HOLD,
    FORCE_CLEARS_ICE,
    FORCE_ON_HELD_UNITS,
    HELD_RELEASED,
    HELD_RELEASED_AT_OPENING,
    HOLD_IGNORED,
    HOLD_OFF,
    HOLD_ON,
    ICE_IGNORED,
    ICE_OFF,
    ICE_ON,
    IDLE_BOX_DERIVE,
    IDLE_BOX_OVERRIDE,
    INJECTABLE_STATUSES,
    JOB_BYPASS,
    JOB_CANCEL_WAITER,
    JOB_ICE_QUEUED,
    JOB_KILL,
    JOB_KILL_QUEUED,
    JOB_LEAVE_QUEUE,
    JOB_NOEXEC_QUEUED,
    JOB_QUEUE,
    JOB_RUN,
    JOB_TERM_RUN_TIME,
    JOB_TERMINATOR,
    JOB_WINDOW_SKIP,
    JOB_WINDOW_SKIP_MEMBER,
    KILL_DISARMS,
    KILL_IGNORED,
    LIVE,
    NOEXEC_CLEARS_HOLD,
    NOEXEC_COMPLETED,
    NOEXEC_IGNORED,
    NOEXEC_OFF,
    NOEXEC_ON,
    QUEUE_LEFT_DISARMS,
    RELEASE_ALL,
    RELEASE_KEEP_HELD,
    RELEASE_SPEND,
    START_REFUSED,
    STATUS_EXIT_FAILURE,
    STATUS_EXIT_SUCCESS,
    STATUS_INACTIVE,
    STATUS_INJECTED,
    TAKE_OVER_HELD,
    CarriedRows,
    CascadeDepthError,
    Event,
    EventKind,
    FlagState,
    HoldingState,
    JobRuntime,
    JobStatus,
    OracleError,
    Transition,
)
from dsl41.oracle_state import DISARM as DISARM_MOVE
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Outcome, StatechartError

#: The flag inputs, dispatched to the job's instance (DL-344).
_FLAG_INPUTS: Final = frozenset({ON_ICE, OFF_ICE, ON_HOLD, OFF_HOLD, ON_NOEXEC, OFF_NOEXEC, DISARM})
#: The internal rows an ignored flag input takes in today's table.
_FLAG_IGNORED: Final[dict[str, Mapping[JobStatus, Transition[JobStatus]]]] = {
    ON_ICE: ICE_IGNORED,
    ON_HOLD: HOLD_IGNORED,
    ON_NOEXEC: NOEXEC_IGNORED,
}
#: The hold rows ON_NOEXEC fires beside its flag's: today's `_set_noexec`
#: writes the flag and its line first, where the region order puts the hold.
_NOEXEC_HOLDS: Final = frozenset({"job.207", "job.232"})
#: The settlement rows and the move each stands for: the verb decides the
#: move from the units, so the row's is checked against it as it writes.
_RELEASES: Final[dict[str, Transition[HoldingState]]] = {
    "job.304": RELEASE_ALL,
    "job.305": RELEASE_KEEP_HELD,
    "job.306": RELEASE_SPEND,
}
#: The rows whose path the job type decides, and whether each is a box's
#: (6.3c F1): a catalog that disagrees with the fired row is a statechart error.
_BOX_ROWS: Final[dict[str, bool]] = {
    "job.102": False,
    "job.124": True,
    "job.105": False,
    "job.125": True,
    "job.113": False,
    "job.120": False,
    "job.121": False,
    "job.135": True,
    "job.136": True,
    "job.169": True,
    "job.170": True,
}
#: A skip's rows, and whether each is a member's (`_window_skip_bypass`).
_MEMBER_ROWS: Final[dict[str, bool]] = {"job.106": False, "job.107": True, "job.404": True}


class ChartOracle(CascadeOracle):
    """`Oracle` with every job move decided by the `job` chart, its cascade
    on the executor's worklist, and its writes made by the hook (DL-350)."""

    #: The cascade is iterative: no frame per nested start (`fit_recursion_limit`).
    FRAMES_PER_NESTED_START = 0

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        default_tz: str | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        super().__init__(
            catalog,
            carried=carried,
            tz_aliases=tz_aliases,
            default_tz=default_tz,
            semantics=semantics,
        )
        self._readmitted = False
        #: the settlement row being written, which the verb's move must match
        self._settling: tuple[str, str] | None = None
        #: the instances the macrostep's items reached, checked at its end
        self._reached: set[str] = set()
        self.store.holding_moved = self._holding_moved
        self.store.keep_job_instances(self._job_instance)
        for job, row in self.store.job.items():
            active = self.store.machines.active(self.store.job_key(job))
            if active != row_configuration(row):
                raise StatechartError(
                    f"job {job!r}: the instance and the row differ at construction: the"
                    f" instance's configuration is {listed(active)}, the row's"
                    f" {listed(row_configuration(row))}"
                )

    def _job_instance(self, job: str, row: JobRuntime) -> tuple[Statechart, frozenset[str], object]:
        return JOB, row_configuration(row), job_data(self, job)

    def fork(self) -> ChartOracle:
        """`Oracle.fork`, with the twin's store asking the twin for its job
        instances' data, and the cascade's bookkeeping the twin's own: a
        shallow copy would share the set of instances a macrostep reached, so
        a row the twin creates would reach the parent's next check
        (DL-350). Between inputs no ended run waits for its settlement."""
        assert not self._late_settlements, "a fork with a settlement still owed"
        twin = cast(ChartOracle, super().fork())
        twin.store.job_instance = twin._job_instance
        twin.store.holding_moved = twin._holding_moved
        twin._reached = set()
        twin._late_settlements = {}
        return twin

    # ---- the inputs: each a root item

    def _dispatch(self, ev: Event) -> None:
        """Every input that moves a job row is a root item on the job's
        instance; SET_GLOBAL's referencers' attempts are one each. A start
        input arms its deadlines first, as today (DL-248)."""
        kind = ev.kind
        if kind == STATUS:
            self._handle_status(ev)
        elif kind in (KILLJOB, RELEASE_RESOURCE):
            job = self._required_job(ev)
            self._root(job, ChartEvent(kind, Payload(units=RowUnits(self, job))))
        elif kind == "TIMER" and self._handle_timer(ev):
            return
        elif kind in _FLAG_INPUTS:
            job = self._required_job(ev)
            payload = (
                NoexecFacts(self, job) if kind == ON_NOEXEC else Payload(units=RowUnits(self, job))
            )
            self._root(job, ChartEvent(kind, payload))
        elif kind in (STARTJOB, FORCE_STARTJOB, "TIMER"):
            job = self._required_job(ev)
            if kind == STARTJOB:
                # SEM-34: the schedule tick arms both deadlines whether or not
                # the start succeeds (DL-248), before the attempt, as today
                for check in ("must_start", "must_complete"):
                    if (timer := self._tick_deadline(job, check)) is not None:
                        self._schedule_timer(timer.at, timer)
            # DL-68: a sourced event names its trigger; a window-postponed
            # start replays the original start's provenance (SEM-33)
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
            super()._dispatch(ev)

    def _root(self, job: str, event: ChartEvent, cause: str = "") -> None:
        """One root item on the job's instance, its row read into existence
        first, and its whole cascade, the hook writing as each item ends."""
        self._runtime(job)
        try:
            self.store.machines.dispatch(self.store.job_key(job), event, self._perform)
            self._check_reached()
        except BaseException as exc:
            self._recover()
            if isinstance(exc, RecursionError) and not isinstance(exc, CascadeDepthError):
                # the cascade takes no frame per start: only a fact's own
                # recursion (a deep condition) meets the limit
                raise CascadeDepthError(job, cause or event.name, sys.getrecursionlimit()) from exc
            raise

    def _check_reached(self) -> None:
        """At a macrostep's end, every job an item of it reached has its row
        project to its instance's configuration: an instance the hook moved
        and never wrote is caught here, where the commit sees only the rows
        the input touched."""
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
        """A macrostep that raised: no write is undone, so the instances are
        rebuilt from the rows, and the cascade's bookkeeping, which today's
        `finally` clauses empty, is empty again."""
        self.store.restore_instances()
        self._in_wake = False
        self._full_scan_requested = False
        self._window_starts = None
        self._late_settlements = {}
        self._readmitted = False
        self._reached = set()

    def _attempt_root(self, job: str, relay: str, cause: str) -> None:
        """An attempt outside any macrostep: an input's own, relayed through
        `relay`, or SET_GLOBAL's (`start` itself). A job with neither a
        catalog entry nor a row is no instance, and nothing happens."""
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
        """The TIMER input (DL-343); False for a window timer's replay of a
        start, which `_dispatch` relays. A deadline alarm moves no state and
        stays `_dispatch_timer_check`. term_run_time carries whether its run is
        the row's. A window timer reads its own facts first, as today: a
        deferral that no longer belongs to its box run, or a scan continuation
        the job has moved on from, is refused with today's text, and a live
        continuation resumes the scan."""
        check = ev.payload.get("check")
        if check == "term_run_time":
            job = self._required_job(ev)
            current = ev.payload.get("run") == self._runtime(job).run_number
            self._root(job, ChartEvent(TERM_RUN_TIME, Deadline(current, units=RowUnits(self, job))))
            return True
        if check is not None:
            return self._dispatch_timer_check(ev)
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
        """DL-247, DL-255: the input's owed admit-only scan, a root item of its
        own once the input's macrosteps are done, while one is owed."""
        while self._scan_owed and not self._in_wake:
            self._scan_owed = False
            if self._pool.has_priority_waiters(self.store.job):
                waiters = self._pool.sorted_waiters(self.store.job)
                job = waiters[0] if waiters else next(iter(self.store.job))
                program = Program(f"{SCAN} owed by the input", self._scan(keep_stopped=True))
                self._root(job, ChartEvent(SCAN, program))

    def _release_removed_holders(self) -> None:
        """DL-256: at the period's opening, each carried row whose job the
        catalog no longer defines and that holds units gives them back
        (job.308), each a root item; then the waiters wake."""
        owed, self._opening_release = self._opening_release, []
        for job in owed:
            self._root(job, ChartEvent(PERIOD_OPENING))
        if owed:
            program = Program(f"{SCAN} after the opening", self._scan(keep_stopped=False))
            self._root(owed[0], ChartEvent(SCAN, program))

    # ---- the hook

    def _perform(self, outcome: Outcome) -> None:
        """The executor's hook (statechart-engine §5): each fired row's writes,
        in today's order within the microstep, then the intents its actions
        reported. A fired row with no write here is a statechart error."""
        key = str(outcome.key)
        self._reached.add(key)
        job = key.partition(":")[2]
        rows = outcome.rows
        if len(rows) > 1 and any(row in _NOEXEC_HOLDS for row in rows):
            rows = tuple(sorted(rows, key=lambda row: row in _NOEXEC_HOLDS))
        for row in rows:
            writer = _WRITERS.get(row)
            if writer is None:
                raise self._unwritten(job, outcome, "row")
            self._check_type(job, row)
            getattr(self, writer)(job, outcome)
        for intent in outcome.intents:
            cast(Intent, intent).apply(self)

    def _check_type(self, job: str, row: str) -> None:
        """The row's job type, or its being a member, is the catalog's (6.3c F1)."""
        box = _BOX_ROWS.get(row)
        if box is not None and box != self._is_box(job):
            kind = "BOX" if not box else "non-box"
            raise StatechartError(f"job {job!r}: {row} fired for a {kind} job")
        member = _MEMBER_ROWS.get(row)
        job_ir = self.catalog.jobs.get(job)
        parent = None if job_ir is None else job_ir.box.box_name
        if member is not None and member != (parent is not None):
            raise StatechartError(f"job {job!r}: {row} fired for a job in box {parent!r}")

    def _holding_moved(self, job: str, move: Transition[HoldingState]) -> None:
        """The store's verb writes `move` for `job`: a settlement row's verb
        decides its move from the units, which must be the row's (DL-349)."""
        if self._settling is not None and self._settling[0] == job:
            row = self._settling[1]
            if _RELEASES[row] is not move:
                raise StatechartError(
                    f"job {job!r}: the chart's {row} stands for {_RELEASES[row].id}, and the"
                    f" verb's holding move is {move.id}"
                )

    def _write(
        self,
        job: str,
        move: Transition[JobStatus],
        cause: str,
        *,
        code: int | None = None,
        clear: bool = False,
    ) -> None:
        """A status write (`_set_status`'s first half): the status the chart
        reached, by today's declared move, its trace line and its emission."""
        old = self._runtime(job).status
        active = self.store.machines.active(self.store.job_key(job))
        new = cast(JobStatus, projection(active)[0])
        self.store.transition(job, move, new, self._now, code, clear_exit_code=clear)
        self._record(job, f"{old}->{new}", cause)
        self._emit("STATUS", job=job, status=new)

    def _flag(self, job: str, move: Transition[FlagState], marker: str, cause: str) -> None:
        self.store.move_flag(job, move)
        self._record(job, marker, cause)

    def _mark_resolved(self, job: str) -> None:
        """An operator's INACTIVE on a member of a RUNNING box resolves it,
        before its move (SEM-11, DL-242)."""
        box = self.catalog.jobs[job].box.box_name
        if box is not None and self._runtime(box).status == "RUNNING":
            self.store.record_resolution(box, job)

    def _skip_line(self, job: str, attempt: StartFacts) -> None:
        self._record(
            job,
            "RUN_WINDOW_SKIP",
            f"outside run_window; closer to previous close -- not run ({attempt.cause})",
        )

    def _refusal(self, job: str, attempt: StartFacts) -> str:
        """Today's START_REFUSED reason for a refused attempt: the job is live
        or queued, or its box gate refuses it (SEM-10, SEM-20)."""
        status = self._runtime(job).status
        if status in ("STARTING", "RUNNING", "QUE_WAIT"):
            return f"already {status} -- concurrent or repeated start request, no effect"
        box = self.catalog.jobs[job].box.box_name
        gate = attempt.known("box")
        if gate == "idle":
            return f"box {box!r} is not RUNNING -- rerun needs FORCE_STARTJOB (SEM-10)"
        if gate == "ran":
            return f"already ran in this {box!r} execution -- rerun needs FORCE_STARTJOB (SEM-10)"
        return (
            f"taken off ice during this {box!r} execution -- it waits for the"
            " box's next run; a start now needs FORCE_STARTJOB (SEM-20)"
        )

    # ---- the writers, one per row

    def _w_nothing(self, job: str, outcome: Outcome) -> None:
        """A row that writes nothing: its move is the configuration's, or none."""

    def _w_bounded(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._ignore(
            job,
            START_REFUSED,
            "START_REFUSED",
            f"re-trigger loop inside one instant: started twice in one nested cascade;"
            f" refused (L010; {attempt.cause})",
        )

    def _w_bypass(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._write(job, JOB_BYPASS, f"ON_NOEXEC bypass ({attempt.cause})")

    def _w_queue(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self.store.enqueue_waiter(job)
        self._write(job, JOB_QUEUE, f"waiting for resources ({attempt.cause})")

    def _w_skip(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._skip_line(job, attempt)
        self._write(
            job, JOB_WINDOW_SKIP, "run_window skip: closer to previous close (SEM-33, DL-246)"
        )

    def _w_skip_member(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        box = cast(str, self.catalog.jobs[job].box.box_name)
        self._skip_line(job, attempt)
        self.store.record_resolution(box, job)
        self._write(
            job,
            JOB_WINDOW_SKIP_MEMBER,
            f"run_window skip bypasses inside box {box!r} (SEM-33, DL-154)",
        )

    def _w_skip_inactive_member(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._skip_line(job, attempt)
        self.store.record_resolution(cast(str, self.catalog.jobs[job].box.box_name), job)

    def _w_refused(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        # a refused attempt is an input's own: `start_verdict` ignores a
        # wake's or a retry's where an input's is refused (DL-81)
        reason = attempt.refusal or f"{self._refusal(job, attempt)} ({attempt.cause})"
        self._ignore(job, START_REFUSED, "START_REFUSED", reason)

    def _w_start_ignored(self, job: str, outcome: Outcome) -> None:
        """job.180: a gate holds the attempt silently; a scan continuation
        resumes; outside the window, a postponed start is queued, or a skip
        that moves nothing writes its line (`_run_window_permits`)."""
        attempt = cast(StartFacts, outcome.event.payload)
        if attempt.rescan:
            self._defer_to_next_opening(self.catalog.jobs[job], attempt.cause)
            return
        side = attempt.known("window")
        if side == "defer":
            self._defer_start(self.catalog.jobs[job], attempt.known("next_open"), attempt.cause)
        elif side == "skip":
            self._skip_line(job, attempt)

    def _w_arm(self, job: str, outcome: Outcome) -> None:
        why = "blocked ON_HOLD" if self._runtime(job).on_hold else "condition false"
        self._flag(job, ARM, "SCHED_ARM", f"scheduled tick {why}; armed (SEM-32, DL-54/58)")

    def _w_force_ice(self, job: str, outcome: Outcome) -> None:
        cause = cast(StartFacts, outcome.event.payload).cause
        self._flag(
            job,
            FORCE_CLEARS_ICE,
            "OFF_ICE",
            f"FORCE_STARTJOB clears ON_ICE (SEM-23, DL-243; {cause})",
        )

    def _w_force_hold(self, job: str, outcome: Outcome) -> None:
        cause = cast(StartFacts, outcome.event.payload).cause
        self._flag(
            job,
            FORCE_CLEARS_HOLD,
            "OFF_HOLD",
            f"FORCE_STARTJOB clears ON_HOLD (SEM-23, DL-243; {cause})",
        )

    def _w_starting(self, job: str, outcome: Outcome) -> None:
        starting = cast(Starting, outcome.event.payload)
        self._write(job, starting.move, starting.cause)

    def _w_running(self, job: str, outcome: Outcome) -> None:
        started = cast(Started, outcome.event.payload)
        if started.had_demand:
            cause = "admitted: resources acquired (DL-50)"
        else:
            cause = "QUE_WAIT collapses to immediate (ss7 non-goal)"
        self._write(job, BOX_RUN if self._is_box(job) else JOB_RUN, cause)

    def _w_actual_start(self, job: str, outcome: Outcome) -> None:
        """`actual start`: the run's term_run_time, then `start_run`, which
        consumes the arm, bumps the run and marks the box's sets (DL-54)."""
        run = cast(RunStart, outcome.event.payload)
        job_ir = self.catalog.jobs[job]
        if run.arm:
            self._arm_term_run_time(job_ir)
        self.store.start_run(
            job, cause=run.cause, box=job_ir.box.box_name, is_box=job_ir.job_type == "BOX"
        )

    def _w_reserve(self, job: str, outcome: Outcome) -> None:
        admission = cast(AdmissionFacts, outcome.event.payload)
        self.store.reserve(job, to_reservations(list(admission.vector)))

    def _w_take_over(self, job: str, outcome: Outcome) -> None:
        admission = cast(AdmissionFacts, outcome.event.payload)
        self.store.take_over_held(job, TAKE_OVER_HELD, to_reservations(list(admission.vector)))

    def _w_on_held(self, job: str, outcome: Outcome) -> None:
        admission = cast(AdmissionFacts, outcome.event.payload)
        held = self._runtime(job).reservations
        load = to_reservations(machine_load(list(admission.vector)))
        self.store.take_over_held(job, FORCE_ON_HELD_UNITS, held + load)

    def _w_leave_live(self, job: str, outcome: Outcome) -> None:
        settlement = cast(Settlement, outcome.event.payload)
        move = settlement.move
        if not isinstance(move, Move):
            raise self._unwritten(job, outcome, "settlement with no move")
        self._settling = (job, outcome.rows[0])
        try:
            self.store.release_reservations(job, move.old, move.new, self._pool.keeps_held)
        finally:
            self._settling = None
        move.released = True

    def _w_settle(self, job: str, outcome: Outcome) -> None:
        """`settle`: the move's late settlement leaves the box rules' registry,
        and the rank the move left drops, unless the run settled early."""
        move = cast(Settle, outcome.event.payload).move
        lates = {j: [x for x in xs if x is not move] for j, xs in self._late_settlements.items()}
        self._late_settlements = {j: xs for j, xs in lates.items() if xs}
        if not move.done:
            self._drop_rank(job)

    def _w_readmit(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)

    def _w_cancel(self, job: str, outcome: Outcome) -> None:
        box = self.catalog.jobs[job].box.box_name
        self.store.dequeue_waiter(job)
        self._write(
            job, JOB_CANCEL_WAITER, f"QUE_WAIT cancelled: box {box!r} no longer RUNNING (DL-50)"
        )

    def _w_left_armed(self, job: str, outcome: Outcome) -> None:
        mode = self.semantics.queued_recheck
        self._flag(
            job,
            QUEUE_LEFT_DISARMS,
            "SCHED_DISARM",
            f"left QUE_WAIT unstarted; waits for its next start time (queued-recheck={mode}, DL-257)",
        )

    def _w_left(self, job: str, outcome: Outcome) -> None:
        moved = cast(FlagFacts, outcome.event.payload)
        _check, why = moved.facts.known("recheck") if moved.facts is not None else ("", "")
        mode = self.semantics.queued_recheck
        box = self.catalog.jobs[job].box.box_name
        self.store.dequeue_waiter(job)
        if box is not None:
            self.store.void_resolution(box, job)
        cause = f"QUE_WAIT left unstarted: {why} (queued-recheck={mode}, DL-257)"
        self._write(job, JOB_LEAVE_QUEUE, cause)

    def _w_killed_waiter(self, job: str, outcome: Outcome) -> None:
        self._write(job, JOB_KILL_QUEUED, "KILLJOB (dequeued from QUE_WAIT, DL-50)")

    def _w_kill_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self.store.move_flag(job, KILL_DISARMS)

    def _w_iced_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self._write(job, JOB_ICE_QUEUED, "iced while queued (DL-50)")

    def _w_noexec_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self._mark_resolved(job)
        self._write(
            job,
            JOB_NOEXEC_QUEUED,
            "ON_NOEXEC takes a queued job out of the queue (DL-254)",
            clear=True,
        )

    def _w_noexec_failed(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        self._write(
            job,
            NOEXEC_COMPLETED,
            "ON_NOEXEC settles a completed job to INACTIVE (DL-243)",
            clear=True,
        )

    def _w_noexec_box(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        self._write(
            job, BOX_NOEXEC, "ON_NOEXEC on a box: CHANGE_STATUS INACTIVE (DL-254)", clear=True
        )

    def _w_kill(self, job: str, outcome: Outcome) -> None:
        self._write(job, JOB_KILL, "KILLJOB")

    def _w_kill_ignored(self, job: str, outcome: Outcome) -> None:
        status = self._runtime(job).status
        self._ignore(
            job,
            KILL_IGNORED,
            "EVENT_IGNORED",
            f"KILLJOB ignored: the job is {status}, not running or queued",
        )

    def _w_term_run_time(self, job: str, outcome: Outcome) -> None:
        self._write(job, JOB_TERM_RUN_TIME, "term_run_time exceeded (dossier ss5)")

    def _w_job_terminator(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._write(job, JOB_TERMINATOR, f"job_terminator: box {box!r} ended")

    def _w_injected(self, job: str, outcome: Outcome) -> None:
        moves = {"job.117": STATUS_EXIT_SUCCESS, "job.118": STATUS_EXIT_FAILURE}
        move = moves.get(outcome.rows[0], STATUS_INJECTED)
        self._write(
            job, move, "injected STATUS", code=cast(StatusInput, outcome.event.payload).code
        )

    def _w_injected_inactive(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        code = cast(StatusInput, outcome.event.payload).code
        self._write(job, STATUS_INACTIVE, "injected STATUS", code=code)

    def _w_injected_box(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        code = cast(StatusInput, outcome.event.payload).code
        self._write(job, BOX_STATUS_INACTIVE, "injected STATUS", code=code)

    def _w_reset(self, job: str, outcome: Outcome) -> None:
        box = cast(Batch, cast(Rule, outcome.event.payload).batch).box
        cause = (
            f"box {box!r} started: statuses from the previous box cycle are not retained"
            " (SEM-10, DL-242)"
        )
        self._write(job, BOX_RESET, cause, clear=True)

    def _w_reset_inactive(self, job: str, outcome: Outcome) -> None:
        """job.195: a job already INACTIVE loses its exit code by a plain write."""
        row = self._runtime(job)
        if row.status == "INACTIVE" and row.exit_code is not None:
            self.store.clear_exit_code(job)

    def _w_cascaded(self, job: str, outcome: Outcome) -> None:
        batch = cast(Batch, cast(Rule, outcome.event.payload).batch)
        cause = (
            f"box {batch.box!r} set INACTIVE: cascades to every job it contains (SEM-18, DL-242)"
        )
        self._write(job, BOX_CASCADE, cause, clear=batch.clear)

    def _w_override(self, job: str, outcome: Outcome) -> None:
        target = projection(self.store.machines.active(self.store.job_key(job)))[0]
        move = BOX_SUCCESS_OVERRIDE if target == "SUCCESS" else BOX_FAILURE_OVERRIDE
        self._write(job, move, f"box_{target.lower()} override met (SEM-12)")

    def _w_fold(self, job: str, outcome: Outcome) -> None:
        fold = cast(FoldFacts, outcome.event.payload)
        if outcome.rows[0] == "job.129":
            self._write(job, BOX_FOLD_FAILURE, "default box fold: a member failed (SEM-11)")
        elif fold.at_start:
            self._write(
                job,
                BOX_FOLD_SUCCESS,
                "default box fold at box start: no member in the run (SEM-11)",
            )
        else:
            self._write(job, BOX_FOLD_SUCCESS, "default box fold: all members SUCCESS (SEM-11)")

    def _w_box_terminator(self, job: str, outcome: Outcome) -> None:
        end = cast(MemberEnd, outcome.event.payload)
        self._write(
            job, BOX_TERMINATOR, f"box_terminator member {end.member!r} ended {end.new} (SEM-14)"
        )

    def _w_recompute(self, job: str, outcome: Outcome) -> None:
        facts = cast(RecomputeFacts, outcome.event.payload)
        target = cast(JobStatus, projection(self.store.machines.active(self.store.job_key(job)))[0])
        if outcome.rows[0] in _OVERRIDE_RECOMPUTES:
            move, why = IDLE_BOX_OVERRIDE[target], "idle-box override recompute"
        else:
            move, why = IDLE_BOX_DERIVE[target], "idle-box recompute"
        self._write(job, move, f"{why} (SEM-15): {facts.cause}")

    def _w_auto_hold(self, job: str, outcome: Outcome) -> None:
        self._flag(job, AUTO_HOLD, "ON_HOLD", "auto_hold on box start (dossier ss5)")

    def _w_box_disarms(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._flag(
            job,
            BOX_END_DISARMS,
            "SCHED_DISARM",
            f"unconsumed arm dies with box {box!r} run (Q3c pin, DL-54/58)",
        )

    def _w_box_noexec_flag(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._flag(job, NOEXEC_ON, "ON_NOEXEC", f"box {box!r} put ON_NOEXEC (DL-254)")

    def _w_noexec_hold(self, job: str, outcome: Outcome) -> None:
        self._flag(
            job, NOEXEC_CLEARS_HOLD, "OFF_HOLD", "ON_NOEXEC supersedes ON_HOLD (SEM-22, DL-254)"
        )

    def _w_box_noexec_off(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._flag(job, BOX_NOEXEC_OFF, "OFF_NOEXEC", f"box {box!r} taken OFF_NOEXEC (DL-254)")

    def _w_ice_on(self, job: str, outcome: Outcome) -> None:
        self._flag(job, ICE_ON, "ON_ICE", "sendevent ON_ICE")

    def _w_ice_off(self, job: str, outcome: Outcome) -> None:
        self.store.move_flag(job, ICE_OFF)
        cause = "sendevent OFF_ICE"
        if outcome.rows[0] == "job.202":
            box = self._off_ice_in_running_box(job)
            if box is not None:
                cause += f"; taken off ice in its running box {box!r}: sits out this run (SEM-20)"
        self._record(job, "OFF_ICE", cause)

    def _w_hold_on(self, job: str, outcome: Outcome) -> None:
        self._flag(job, HOLD_ON, "ON_HOLD", "sendevent ON_HOLD")

    def _w_hold_off(self, job: str, outcome: Outcome) -> None:
        self._flag(job, HOLD_OFF, "OFF_HOLD", "sendevent OFF_HOLD")

    def _w_noexec_on(self, job: str, outcome: Outcome) -> None:
        self._flag(job, NOEXEC_ON, "ON_NOEXEC", "sendevent ON_NOEXEC")

    def _w_noexec_off(self, job: str, outcome: Outcome) -> None:
        self._flag(job, NOEXEC_OFF, "OFF_NOEXEC", "sendevent OFF_NOEXEC")

    def _w_disarm(self, job: str, outcome: Outcome) -> None:
        cause = (
            "sendevent DISARM" if outcome.rows[0] == "job.214" else "sendevent DISARM (no latch)"
        )
        self._flag(job, DISARM_MOVE, "DISARM", cause)

    def _w_flag_ignored(self, job: str, outcome: Outcome) -> None:
        kind = cast(EventKind, outcome.event.name)
        why = self._oob_ignored(kind, job)
        if why is None:
            raise self._unwritten(job, outcome, "ignored line (today's rules apply it)")
        self._ignore(job, _FLAG_IGNORED[kind], "EVENT_IGNORED", why)

    def _w_released(self, job: str, outcome: Outcome) -> None:
        freed = self.store.release_held(job, HELD_RELEASED)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE (frees {units})")

    def _w_release_ignored(self, job: str, outcome: Outcome) -> None:
        status = self._runtime(job).status
        why = "nothing held"
        if status in LIVE:
            why = f"no effect: {status}; the run's units go back when it ends"
        self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE ({why})")

    def _w_opening(self, job: str, outcome: Outcome) -> None:
        freed = self.store.release_held(job, HELD_RELEASED_AT_OPENING)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(
            job,
            "RELEASE_RESOURCE",
            f"job removed from the catalog: its held units go back at the period"
            f" opening (frees {units}; DL-256)",
        )


#: The rows whose idle recompute an override decided (SEM-15).
_OVERRIDE_RECOMPUTES: Final = frozenset(f"job.{n}" for n in (131, 132, 161, 162, 163, 164))

#: The hook's write for each row the chart oracle can fire, by its method's
#: name, so a subclass's method is the one called. A row missing here
#: is one the oracle never writes: job.312 (a start on a run's unsettled
#: units, which job.401 settles first) and the cells the chart marks
#: unreachable.
_WRITERS: Final[dict[str, str]] = {
    "job.100": "_w_bounded",
    "job.101": "_w_bypass",
    "job.102": "_w_nothing",
    "job.103": "_w_queue",
    "job.104": "_w_nothing",
    "job.105": "_w_running",
    "job.106": "_w_skip",
    "job.107": "_w_skip_member",
    "job.108": "_w_readmit",
    "job.109": "_w_cancel",
    "job.110": "_w_left",
    "job.111": "_w_killed_waiter",
    "job.112": "_w_iced_waiter",
    "job.113": "_w_noexec_waiter",
    "job.114": "_w_kill",
    "job.115": "_w_term_run_time",
    "job.116": "_w_job_terminator",
    "job.117": "_w_injected",
    "job.118": "_w_injected",
    "job.119": "_w_injected",
    "job.120": "_w_injected_inactive",
    "job.121": "_w_noexec_failed",
    "job.122": "_w_reset",
    "job.123": "_w_cascaded",
    "job.124": "_w_nothing",
    "job.125": "_w_running",
    "job.126": "_w_override",
    "job.127": "_w_override",
    "job.128": "_w_fold",
    "job.129": "_w_fold",
    "job.130": "_w_box_terminator",
    **{f"job.{n}": "_w_recompute" for n in (*range(131, 135), *range(161, 169))},
    "job.135": "_w_injected_box",
    "job.136": "_w_noexec_box",
    **{f"job.{n}": "_w_kill_ignored" for n in range(137, 141)},
    **{f"job.{n}": "_w_refused" for n in range(141, 148)},
    **{f"job.{n}": "_w_flag_ignored" for n in range(148, 159)},
    "job.159": "_w_cascaded",
    "job.160": "_w_cascaded",
    "job.169": "_w_noexec_box",
    "job.170": "_w_noexec_box",
    **{f"job.{n}": "_w_nothing" for n in (177, 178, 179, 181, 183, 184, 185, 187)},
    "job.180": "_w_start_ignored",
    **{f"job.{n}": "_w_nothing" for n in (189, 191, 196, 197, 198)},
    "job.195": "_w_reset_inactive",
    "job.201": "_w_ice_on",
    "job.202": "_w_ice_off",
    "job.203": "_w_force_ice",
    "job.204": "_w_hold_on",
    "job.205": "_w_hold_off",
    "job.206": "_w_force_hold",
    "job.207": "_w_noexec_hold",
    "job.208": "_w_auto_hold",
    "job.209": "_w_noexec_on",
    "job.210": "_w_noexec_off",
    "job.211": "_w_box_noexec_off",
    "job.212": "_w_arm",
    "job.213": "_w_actual_start",
    "job.214": "_w_disarm",
    "job.215": "_w_box_disarms",
    "job.216": "_w_left_armed",
    "job.217": "_w_kill_waiter",
    "job.218": "_w_ice_on",
    "job.219": "_w_ice_off",
    "job.220": "_w_hold_on",
    "job.221": "_w_hold_off",
    "job.222": "_w_noexec_on",
    "job.223": "_w_noexec_off",
    "job.224": "_w_actual_start",
    "job.225": "_w_disarm",
    "job.226": "_w_kill_waiter",
    **{f"job.{n}": "_w_nothing" for n in (227, 228, 229, 230)},
    "job.231": "_w_box_noexec_flag",
    "job.232": "_w_noexec_hold",
    "job.233": "_w_box_noexec_flag",
    "job.301": "_w_reserve",
    "job.302": "_w_take_over",
    "job.303": "_w_on_held",
    "job.304": "_w_leave_live",
    "job.305": "_w_leave_live",
    "job.306": "_w_leave_live",
    "job.307": "_w_released",
    "job.308": "_w_opening",
    "job.311": "_w_reserve",
    **{f"job.{n}": "_w_nothing" for n in (313, 314, 319)},
    "job.315": "_w_release_ignored",
    "job.316": "_w_release_ignored",
    "job.401": "_w_nothing",
    "job.402": "_w_nothing",
    "job.403": "_w_starting",
    "job.404": "_w_skip_inactive_member",
    **{f"job.{n}": "_w_nothing" for n in range(405, 418)},
    "job.406": "_w_settle",
    "job.418": "_w_settle",
}
