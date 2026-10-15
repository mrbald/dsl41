"""The oracle's writes: what each fired row of the `job` chart reached
(docs/statechart-engine.md ss5, ss11; DL-350).

The executor calls the oracle's hook with each microstep's outcome, before
the next item pops (`oracle.Oracle._perform`). The hook writes each fired
row with the method `_WRITERS` names: the store's verb with what the row reached, the
status's trace line and its STATUS emission, in the order the rows
fired within the microstep, then the intents the row's actions reported. A
row's type is checked against the catalog before it writes (`_check_type`).
A fired row with no write, or whose write needs a fact the payload does not
carry, is a statechart error (`_unwritten`).
"""

from __future__ import annotations

from typing import Final, cast

from dsl41.capacity import machine_load, to_reservations
from dsl41.job_chart import RELEASE_RESOURCE, Settlement, projection
from dsl41.oracle_cascade import OracleCascade
from dsl41.oracle_facts import FoldFacts, RecomputeFacts, StartFacts, Started
from dsl41.oracle_intents import (
    AdmissionFacts,
    Batch,
    FlagFacts,
    MemberEnd,
    Move,
    Rule,
    RunStart,
    Settle,
    Starting,
    StatusInput,
)
from dsl41.oracle_state import LIVE, EventKind, FlagState, JobStatus
from dsl41.state_machine import Outcome, StatechartError

#: The hold rows ON_NOEXEC fires beside its flag's: the flag and its line are
#: written first, where the region order puts the hold (DL-254).
_NOEXEC_HOLDS: Final = frozenset({"job.207", "job.232"})

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

#: A skip's rows, and whether each is a member's (SEM-33, DL-154).
_MEMBER_ROWS: Final[dict[str, bool]] = {"job.106": False, "job.107": True, "job.404": True}


class OracleWriters(OracleCascade):
    """`OracleCascade` with a write for each row the chart can fire."""

    def _write_rows(self, job: str, outcome: Outcome) -> None:
        """Each fired row's write, in the order the rows fired within the
        microstep, ON_NOEXEC's hold rows after its flag's; a fired row with no
        write here is a statechart error."""
        rows = outcome.rows
        if len(rows) > 1 and any(row in _NOEXEC_HOLDS for row in rows):
            rows = tuple(sorted(rows, key=lambda row: row in _NOEXEC_HOLDS))
        for row in rows:
            writer = _WRITERS.get(row)
            if writer is None:
                raise self._unwritten(job, outcome, "row")
            self._check_type(job, row)
            getattr(self, writer)(job, outcome)

    def _unwritten(self, job: str, outcome: Outcome, what: str) -> StatechartError:
        """The chart fired rows the oracle has no write for. Raised from the
        hook, the executor gives it the item's facts."""
        rows = ", ".join(outcome.rows) or "no row"
        return StatechartError(
            f"job {job!r}: {outcome.event.name} fired {rows}, and no {what} the oracle writes"
        )

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

    def _write(self, job: str, cause: str, *, code: int | None = None, clear: bool = False) -> None:
        """A status write: the status the chart reached, its trace line and its
        emission."""
        old = self._runtime(job).status
        new = self._status_reached(job)
        self.store.transition(job, new, self._now, code, clear_exit_code=clear)
        self._record(job, f"{old}->{new}", cause)
        self._emit("STATUS", job=job, status=new)

    def _status_reached(self, job: str) -> JobStatus:
        """The status the job's instance is in."""
        return cast(JobStatus, projection(self.store.machines.active(self.store.job_key(job)))[0])

    def _flag(self, job: str, target: FlagState, marker: str, cause: str) -> None:
        self.store.move_flag(job, target)
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
        """The START_REFUSED reason for a refused attempt: the job is live
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

    def _w_nothing(self, job: str, outcome: Outcome) -> None:
        """A row that writes nothing: its move is the configuration's, or none."""

    def _w_bounded(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._record(
            job,
            "START_REFUSED",
            f"re-trigger loop inside one instant: started twice in one nested cascade;"
            f" refused (L010; {attempt.cause})",
        )

    def _w_bypass(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._write(job, f"ON_NOEXEC bypass ({attempt.cause})")

    def _w_queue(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self.store.enqueue_waiter(job)
        self._write(job, f"waiting for resources ({attempt.cause})")

    def _w_skip(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._skip_line(job, attempt)
        self._write(job, "run_window skip: closer to previous close (SEM-33, DL-246)")

    def _w_skip_member(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        box = cast(str, self.catalog.jobs[job].box.box_name)
        self._skip_line(job, attempt)
        self.store.record_resolution(box, job)
        self._write(job, f"run_window skip bypasses inside box {box!r} (SEM-33, DL-154)")

    def _w_skip_inactive_member(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        self._skip_line(job, attempt)
        self.store.record_resolution(cast(str, self.catalog.jobs[job].box.box_name), job)

    def _w_refused(self, job: str, outcome: Outcome) -> None:
        attempt = cast(StartFacts, outcome.event.payload)
        # a refused attempt is an input's own: `start_verdict` ignores a
        # wake's or a retry's where an input's is refused (DL-81)
        reason = attempt.refusal or f"{self._refusal(job, attempt)} ({attempt.cause})"
        self._record(job, "START_REFUSED", reason)

    def _w_start_ignored(self, job: str, outcome: Outcome) -> None:
        """job.180: a gate holds the attempt silently; a scan continuation
        resumes; outside the window, a postponed start is queued, or a skip
        that moves nothing writes its line (`_window_side`)."""
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
        self._flag(job, "arm_on", "SCHED_ARM", f"scheduled tick {why}; armed (SEM-32, DL-54/58)")

    def _w_force_ice(self, job: str, outcome: Outcome) -> None:
        cause = cast(StartFacts, outcome.event.payload).cause
        self._flag(
            job,
            "ice_off",
            "OFF_ICE",
            f"FORCE_STARTJOB clears ON_ICE (SEM-23, DL-243; {cause})",
        )

    def _w_force_hold(self, job: str, outcome: Outcome) -> None:
        cause = cast(StartFacts, outcome.event.payload).cause
        self._flag(
            job,
            "hold_off",
            "OFF_HOLD",
            f"FORCE_STARTJOB clears ON_HOLD (SEM-23, DL-243; {cause})",
        )

    def _w_starting(self, job: str, outcome: Outcome) -> None:
        starting = cast(Starting, outcome.event.payload)
        self._write(job, starting.cause)

    def _w_running(self, job: str, outcome: Outcome) -> None:
        started = cast(Started, outcome.event.payload)
        if started.had_demand:
            cause = "admitted: resources acquired (DL-50)"
        else:
            cause = "QUE_WAIT collapses to immediate (ss7 non-goal)"
        self._write(job, cause)

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
        self.store.take_over_held(job, to_reservations(list(admission.vector)))

    def _w_on_held(self, job: str, outcome: Outcome) -> None:
        admission = cast(AdmissionFacts, outcome.event.payload)
        held = self._runtime(job).reservations
        load = to_reservations(machine_load(list(admission.vector)))
        self.store.take_over_held(job, held + load)

    def _w_leave_live(self, job: str, outcome: Outcome) -> None:
        settlement = cast(Settlement, outcome.event.payload)
        move = settlement.move
        if not isinstance(move, Move):
            raise self._unwritten(job, outcome, "settlement with no move")
        self.store.release_reservations(job, move.new, self._pool.keeps_held)
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
        self._write(job, f"QUE_WAIT cancelled: box {box!r} no longer RUNNING (DL-50)")

    def _w_left_armed(self, job: str, outcome: Outcome) -> None:
        mode = self.semantics.queued_recheck
        self._flag(
            job,
            "arm_off",
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
        self._write(job, cause)

    def _w_killed_waiter(self, job: str, outcome: Outcome) -> None:
        self._write(job, "KILLJOB (dequeued from QUE_WAIT, DL-50)")

    def _w_kill_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self.store.move_flag(job, "arm_off")

    def _w_iced_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self._write(job, "iced while queued (DL-50)")

    def _w_noexec_waiter(self, job: str, outcome: Outcome) -> None:
        self.store.dequeue_waiter(job)
        self._mark_resolved(job)
        self._write(job, "ON_NOEXEC takes a queued job out of the queue (DL-254)", clear=True)

    def _w_noexec_failed(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        self._write(job, "ON_NOEXEC settles a completed job to INACTIVE (DL-243)", clear=True)

    def _w_noexec_box(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        self._write(job, "ON_NOEXEC on a box: CHANGE_STATUS INACTIVE (DL-254)", clear=True)

    def _w_kill(self, job: str, outcome: Outcome) -> None:
        self._write(job, "KILLJOB")

    def _w_kill_ignored(self, job: str, outcome: Outcome) -> None:
        status = self._runtime(job).status
        self._record(
            job,
            "EVENT_IGNORED",
            f"KILLJOB ignored: the job is {status}, not running or queued",
        )

    def _w_term_run_time(self, job: str, outcome: Outcome) -> None:
        self._write(job, "term_run_time exceeded (dossier ss5)")

    def _w_job_terminator(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._write(job, f"job_terminator: box {box!r} ended")

    def _w_injected(self, job: str, outcome: Outcome) -> None:
        self._write(job, "injected STATUS", code=cast(StatusInput, outcome.event.payload).code)

    def _w_injected_inactive(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        code = cast(StatusInput, outcome.event.payload).code
        self._write(job, "injected STATUS", code=code)

    def _w_injected_box(self, job: str, outcome: Outcome) -> None:
        self._mark_resolved(job)
        code = cast(StatusInput, outcome.event.payload).code
        self._write(job, "injected STATUS", code=code)

    def _w_reset(self, job: str, outcome: Outcome) -> None:
        box = cast(Batch, cast(Rule, outcome.event.payload).batch).box
        cause = (
            f"box {box!r} started: statuses from the previous box cycle are not retained"
            " (SEM-10, DL-242)"
        )
        self._write(job, cause, clear=True)

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
        self._write(job, cause, clear=batch.clear)

    def _w_override(self, job: str, outcome: Outcome) -> None:
        target = self._status_reached(job)
        self._write(job, f"box_{target.lower()} override met (SEM-12)")

    def _w_fold(self, job: str, outcome: Outcome) -> None:
        fold = cast(FoldFacts, outcome.event.payload)
        if outcome.rows[0] == "job.129":
            self._write(job, "default box fold: a member failed (SEM-11)")
        elif fold.at_start:
            self._write(job, "default box fold at box start: no member in the run (SEM-11)")
        else:
            self._write(job, "default box fold: all members SUCCESS (SEM-11)")

    def _w_box_terminator(self, job: str, outcome: Outcome) -> None:
        end = cast(MemberEnd, outcome.event.payload)
        self._write(job, f"box_terminator member {end.member!r} ended {end.new} (SEM-14)")

    def _w_recompute(self, job: str, outcome: Outcome) -> None:
        facts = cast(RecomputeFacts, outcome.event.payload)
        if outcome.rows[0] in _OVERRIDE_RECOMPUTES:
            why = "idle-box override recompute"
        else:
            why = "idle-box recompute"
        self._write(job, f"{why} (SEM-15): {facts.cause}")

    def _w_auto_hold(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "hold_on", "ON_HOLD", "auto_hold on box start (dossier ss5)")

    def _w_box_disarms(self, job: str, outcome: Outcome) -> None:
        """A member's arm is scoped to the box run that armed it (DL-54
        review MAJOR): an unconsumed arm dies with the run, before any wake
        can ride it."""
        # PENDING: Q3c -- one field aside (DL-58) hints the vendor latch may
        # instead survive into the NEXT box run; the scoped pin stands until
        # a live test.
        box = cast(Rule, outcome.event.payload).box
        self._flag(
            job,
            "arm_off",
            "SCHED_DISARM",
            f"unconsumed arm dies with box {box!r} run (Q3c pin, DL-54/58)",
        )

    def _w_box_noexec_flag(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._flag(job, "noexec_on", "ON_NOEXEC", f"box {box!r} put ON_NOEXEC (DL-254)")

    def _w_noexec_hold(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "hold_off", "OFF_HOLD", "ON_NOEXEC supersedes ON_HOLD (SEM-22, DL-254)")

    def _w_box_noexec_off(self, job: str, outcome: Outcome) -> None:
        box = cast(Rule, outcome.event.payload).box
        self._flag(job, "noexec_off", "OFF_NOEXEC", f"box {box!r} taken OFF_NOEXEC (DL-254)")

    def _w_ice_on(self, job: str, outcome: Outcome) -> None:
        # PENDING: Q3d (DL-69) -- a pre-existing arm survives the ice
        # round-trip untouched (DL-54 pin, uncited), so a stale tick can
        # still start this job on the next condition edge despite SEM-20's
        # "reoccur" rule. If the vendor instead discards the queued start on
        # ICE, the ON_ICE row disarms (SCHED_DISARM) and SEM-20/32 are
        # amended -- protocol in docs/live-instance-runbook.md.
        self._flag(job, "ice_on", "ON_ICE", "sendevent ON_ICE")

    def _w_ice_off(self, job: str, outcome: Outcome) -> None:
        self.store.move_flag(job, "ice_off")
        cause = "sendevent OFF_ICE"
        if outcome.rows[0] == "job.202":
            box = self._off_ice_in_running_box(job)
            if box is not None:
                cause += f"; taken off ice in its running box {box!r}: sits out this run (SEM-20)"
        self._record(job, "OFF_ICE", cause)

    def _w_hold_on(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "hold_on", "ON_HOLD", "sendevent ON_HOLD")

    def _w_hold_off(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "hold_off", "OFF_HOLD", "sendevent OFF_HOLD")

    def _w_noexec_on(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "noexec_on", "ON_NOEXEC", "sendevent ON_NOEXEC")

    def _w_noexec_off(self, job: str, outcome: Outcome) -> None:
        self._flag(job, "noexec_off", "OFF_NOEXEC", "sendevent OFF_NOEXEC")

    def _w_disarm(self, job: str, outcome: Outcome) -> None:
        cause = (
            "sendevent DISARM" if outcome.rows[0] == "job.214" else "sendevent DISARM (no latch)"
        )
        self._flag(job, "arm_off", "DISARM", cause)

    def _w_flag_ignored(self, job: str, outcome: Outcome) -> None:
        kind = cast(EventKind, outcome.event.name)
        why = self._oob_ignored(kind, job)
        if why is None:
            raise self._unwritten(job, outcome, "ignored line (the rules apply it)")
        self._record(job, "EVENT_IGNORED", why)

    def _w_released(self, job: str, outcome: Outcome) -> None:
        freed = self.store.release_held(job)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE (frees {units})")

    def _w_release_ignored(self, job: str, outcome: Outcome) -> None:
        status = self._runtime(job).status
        why = "nothing held"
        if status in LIVE:
            why = f"no effect: {status}; the run's units go back when it ends"
        self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE ({why})")

    def _w_opening(self, job: str, outcome: Outcome) -> None:
        freed = self.store.release_held(job)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(
            job,
            "RELEASE_RESOURCE",
            f"job removed from the catalog: its held units go back at the period"
            f" opening (frees {units}; DL-256)",
        )


#: The rows whose idle recompute an override decided (SEM-15).
_OVERRIDE_RECOMPUTES: Final = frozenset(f"job.{n}" for n in (131, 132, 161, 162, 163, 164))

#: The hook's write for each row the chart can fire, by its method's name,
#: so a subclass's method is the one called. A row missing here is one the
#: chart marks unreachable, job.312 among them (a start on a run's unsettled
#: units, which job.401 settles first).
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
    **{f"job.{n}": "_w_nothing" for n in (313, 319)},
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
