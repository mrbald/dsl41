"""The box rules of the second oracle on the `job` chart (docs/statechart-engine.md
§11, DL-346).

A box rule is a moment of one job that moves another: a member's status
write reaches its box, and a box's start, run and end reach its members.
`CascadeOracle` dispatches each moment the chart declares to the instance it
moves, at the point where today's cascade runs the rule, and performs the
fired rows through today's methods, with today's markers and lines. The
cascade's order stays today's recursion: `_after_transition` runs the box
rules, then the move's settlement, then the wakes, and each rule below sits
where today's method sits, so the stack under a cascade is as deep as today's.
A box rule's target (SUCCESS or FAILURE, INACTIVE or no move) is the chart's
choice: the status written is the configuration the chart reached, and a
payload fact that derives another target is a `StatechartError` before any
write.

A member's moments, on its box's instance (`_on_member_transition`): a member
that ends FAILURE or TERMINATED meets its box's terminator (`member ends
FAILURE or TERMINATED`: job.130 terminates a RUNNING box, job.189 is no
move); a RUNNING box's completion door reads its overrides (`member moves`,
job.126 and job.127, or job.185) and then the default fold (`completion
moment`, job.128 and job.129, or job.187); an idle box re-derives its status
(`box recompute`, job.131 to job.134 and job.161 to job.168, or job.191). The
walk up to the boxes above (`_on_descendant_transition`) reads their overrides
through the same `member moves`. A box's moves are compared as a STATUS move
is (`_moving`).

A box's moments, on each member's instance: a box start resets the jobs it
contains (`box start`, job.122; job.195 clears an INACTIVE row's exit code),
and its auto_hold members are held when it runs (`box running`, job.208, or
job.227); a box run that ends drops its members' arms (`box run
ends`, job.215, or job.228); a box that ends FAILURE or TERMINATED terminates
its live job_terminator members (`box ends FAILURE or TERMINATED`, job.116,
or job.197); a box set INACTIVE takes every job it contains with it (`box set
INACTIVE`, job.123, job.159 and job.160, or job.196 for a job already
INACTIVE), on the batch INACTIVE path of DL-242; a box put ON_NOEXEC sets its
contained jobs' flag and takes their hold off (`ON_NOEXEC on its box`,
job.231 or job.233, and job.232, DL-347); a box taken OFF_NOEXEC clears
its contained jobs' flag (`OFF_NOEXEC on its box`, job.211, or job.229). A
batch row is compared when it is written and settled, a flag row after its
write, a member's termination as a STATUS move.

The period opening (`first input of a period`): a removed job that holds
units gives them back (job.308), then the waiters wake.

Audiences. The chart decides each box rule's row; the oracle decides when a
rule is posted and to which instances. `box running`, `box run ends` and `box
ends FAILURE or TERMINATED` reach the box's direct members; `box start`, `box
set INACTIVE`, `ON_NOEXEC on its box` and `OFF_NOEXEC on its box` reach every
job it contains. Where a code gate chooses the audience, the audience is the
sender's, and the worklist slice declares it in the sender's action
(statechart-engine §8, §11).

Not declared by the chart, so today's code: the moves of the box row's sets
(the resolutions, the iced-out marks, the ran members); and the window
decisions at an outermost box start, which move no status.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, cast

from dsl41.ir import JobIR
from dsl41.job_chart import (
    BOX_ENDS,
    BOX_OFF_NOEXEC,
    BOX_ON_NOEXEC,
    BOX_RECOMPUTE,
    BOX_RUN_ENDS,
    BOX_RUNNING,
    BOX_SET_INACTIVE,
    BOX_START,
    COMPLETION_MOMENT,
    MEMBER_ENDS,
    MEMBER_MOVES,
    PERIOD_OPENING,
    Terminator,
    row_configuration,
)
from dsl41.oracle_chart_agreement import AgreeingOracle, Agreement, fired_rows
from dsl41.oracle_chart_facts import FoldFacts, OverrideFacts, RecomputeFacts
from dsl41.oracle_state import (
    AUTO_HOLD,
    BOX_CASCADE,
    BOX_END_DISARMS,
    BOX_FAILURE_OVERRIDE,
    BOX_FOLD_FAILURE,
    BOX_FOLD_SUCCESS,
    BOX_NOEXEC_OFF,
    BOX_RESET,
    BOX_SUCCESS_OVERRIDE,
    BOX_TERMINATOR,
    FAILED,
    HELD_RELEASED_AT_OPENING,
    IDLE_BOX_DERIVE,
    IDLE_BOX_OVERRIDE,
    JOB_TERMINATOR,
    LIVE,
    TERMINAL,
    JobStatus,
    Transition,
)
from dsl41.state_machine import Event as ChartEvent

#: `member ends FAILURE or TERMINATED` on the box: its terminator fires.
_TERMINATES: Final = ("job.130",)
#: `member moves`: an override decides the box's end.
_OVERRIDDEN: Final[dict[str, Transition[JobStatus]]] = {
    "SUCCESS": BOX_SUCCESS_OVERRIDE,
    "FAILURE": BOX_FAILURE_OVERRIDE,
}
_OVERRIDE_ROWS: Final = frozenset({("job.126",), ("job.127",)})
#: `completion moment`: the default fold decides the box's end.
_FOLDED: Final[dict[str, Transition[JobStatus]]] = {
    "SUCCESS": BOX_FOLD_SUCCESS,
    "FAILURE": BOX_FOLD_FAILURE,
}
_FOLD_ROWS: Final = frozenset({("job.128",), ("job.129",)})
#: `box recompute`: an idle box's new status, by whether an override decided it.
_RECOMPUTED: Final[dict[tuple[str, ...], bool]] = {
    **{(f"job.{n}",): True for n in (131, 132, 161, 162, 163, 164)},
    **{(f"job.{n}",): False for n in (133, 134, 165, 166, 167, 168)},
}
#: `box set INACTIVE` on a contained job: it goes INACTIVE with its box.
_CASCADED: Final = frozenset({("job.123",), ("job.159",), ("job.160",)})
#: `ON_NOEXEC on its box` on a contained job: the flag's row, after the hold's
#: where it clears one (region order), and whether the hold goes.
_NOEXEC_HOLD: Final[dict[tuple[str, ...], bool]] = {
    ("job.231",): False,
    ("job.233",): False,
    ("job.232", "job.231"): True,
    ("job.232", "job.233"): True,
}
#: The rows that move nothing, by event.
_NO_MOVE: Final[dict[str, tuple[str, ...]]] = {
    MEMBER_ENDS: ("job.189",),
    MEMBER_MOVES: ("job.185",),
    COMPLETION_MOMENT: ("job.187",),
    BOX_RECOMPUTE: ("job.191",),
    BOX_START: ("job.195",),
    BOX_RUNNING: ("job.227",),
    BOX_RUN_ENDS: ("job.228",),
    BOX_ENDS: ("job.197",),
    BOX_SET_INACTIVE: ("job.196",),
    BOX_OFF_NOEXEC: ("job.229",),
}


class CascadeOracle(AgreeingOracle):
    """`Oracle` with the box rules and the period opening decided by the
    `job` chart (DL-346, DL-347), each at today's place in the cascade."""

    def _box_rule(self, job: str, event: ChartEvent) -> tuple[tuple[str, ...], str, str]:
        """Dispatch a box rule's moment to the job's instance, brought to its
        row first: the rows it fired (its settlement's aside), the key and the
        job's status before it."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        old = store.job[job].status
        return fired_rows(store.machines.dispatch(key, event)), key, old

    def _stays(self, job: str, key: str, event: ChartEvent, old: str) -> Agreement:
        """A rule's move that leaves the status alone, to compare after its writes."""
        active = self.store.machines.active(key)
        return Agreement(job, key, event, active, (old, old), False, moves=False)

    def _no_move(self, job: str, event: ChartEvent, fired: tuple[str, ...]) -> None:
        """The rule moved nothing: any row but its no-move row is unwritten,
        and the instance, which the follow made the row's before the
        dispatch, is still the row's configuration."""
        if fired != _NO_MOVE[event.name]:
            raise self._unwritten(job, event, fired, "box rule row")
        key, row = self.store.job_key(job), self.store.job[job]
        if self.store.machines.active(key) != row_configuration(row):
            stays = self._stays(job, key, event, row.status)
            raise self._differ(f"{fired[0]} moves nothing, and the chart moved the job", stays)

    def _target(self, chosen: str, derived: str | None, agreement: Agreement) -> JobStatus:
        """The chart's target for a box, which a fact's derivation must match."""
        if derived != chosen:
            raise self._differ(
                f"{agreement.event.name} reached {chosen}, and its facts derive {derived}",
                agreement,
            )
        return cast(JobStatus, chosen)

    # ---- a member's moments, on its box's instance

    def _on_member_transition(self, box: str, member: str, old: str, new: str) -> None:
        """Today's member rules, each moment the box's: a member that ends
        FAILURE or TERMINATED meets the box's terminator (SEM-14), and only
        a RUNNING box terminates (job.130); a TERMINATED box is sticky
        (SEM-13); a RUNNING box runs its completion door; an idle box whose
        member reached a terminal status re-derives its status (SEM-15).
        The audience is the sender's, declared by the worklist slice: the
        door reads the overrides only on a terminal, RUNNING or resolving
        move, and the recompute is sent only on a terminal move."""
        if new in FAILED:
            event = ChartEvent(MEMBER_ENDS, Terminator(self._box_terminator_fires(member, new)))
            fired, key, status = self._box_rule(box, event)
            if fired == _TERMINATES:
                with self._moving(box, key, event, status):
                    self._terminate(
                        box,
                        BOX_TERMINATOR,
                        cause=f"box_terminator member {member!r} ended {new} (SEM-14)",
                    )
                return
            self._no_move(box, event, fired)
        box_rt = self._runtime(box)
        if box_rt.status == "TERMINATED":
            return  # SEM-13: sticky until the next box start
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
            self._idle_box_recompute(
                box, self.catalog.jobs[box], cause=f"member {member!r} changed", trigger=member
            )

    def _completion_door(
        self,
        box: str,
        member: str | None,
        new: str | None,
        *,
        completion_moment: bool,
        overrides: bool = True,
    ) -> None:
        """The completion door of a RUNNING box (SEM-11, SEM-12): its
        overrides first (`member moves`), then, if none fired, the default
        fold (`completion moment`, `_fold_box_default`). A door with no
        member is a box start that left no member in the run."""
        box_ir = self.catalog.jobs[box]
        if overrides and self._apply_box_overrides(
            box, box_ir, member, new, completion_moment=completion_moment
        ):
            return
        self._fold_box_default(box, box_ir, at_start=member is None)

    def _fold_box_default(self, box: str, box_ir: JobIR, *, at_start: bool = False) -> None:
        """`completion moment` on a RUNNING box (SEM-11): once every member
        is done, job.128 or job.129 ends the box by the default fold; job.187
        is a member not done, or an unmet override that suppresses the
        default. It sits where today's fold sits, under the door. `at_start`:
        the box start left no member in the run."""
        facts = FoldFacts(self, box_ir)
        event = ChartEvent(COMPLETION_MOMENT, facts)
        fired, key, old = self._box_rule(box, event)
        if fired not in _FOLD_ROWS:
            self._no_move(box, event, fired)
            return
        with self._moving(box, key, event, old) as agreement:
            target = self._target(agreement.move[1], facts.target, agreement)
            if target == "FAILURE":
                cause = "default box fold: a member failed (SEM-11)"
            elif at_start:
                cause = "default box fold at box start: no member in the run (SEM-11)"
            else:
                cause = "default box fold: all members SUCCESS (SEM-11)"
            self._set_status(box, _FOLDED[target], cause=cause)
            self._on_box_completed(box)

    def _apply_box_overrides(
        self,
        box: str,
        box_ir: JobIR,
        member: str | None,
        new: str | None,
        *,
        completion_moment: bool = False,
    ) -> bool:
        """`member moves` on a RUNNING box, its parent's or one above it
        (SEM-12): job.126 or job.127 ends the box by the override that holds;
        job.185 is none. True when the box ended. The audience is the
        sender's, declared by the worklist slice: the door sends it on a
        terminal, RUNNING or resolving move, and the walk up only to an
        ancestor still in the run it was in before the moment."""
        facts = OverrideFacts(self, box_ir, member, new, completion_moment=completion_moment)
        event = ChartEvent(MEMBER_MOVES, facts)
        fired, key, old = self._box_rule(box, event)
        if fired not in _OVERRIDE_ROWS:
            self._no_move(box, event, fired)
            return False
        with self._moving(box, key, event, old) as agreement:
            target = self._target(agreement.move[1], facts.target, agreement)
            self._set_status(
                box, _OVERRIDDEN[target], cause=f"box_{target.lower()} override met (SEM-12)"
            )
            self._on_box_completed(box)
        return True

    def _idle_box_recompute(self, box: str, box_ir: JobIR, cause: str, *, trigger: str) -> None:
        """`box recompute` on a box that is not running (SEM-15): an override
        (job.131, job.132, job.161 to job.164) or the default verdict
        (job.133, job.134, job.165 to job.168) moves the box; job.191 is no
        verdict, or one the box already has. Whether an override decided it is
        the row's choice too, which the facts must match. The audience is the
        sender's, declared by the worklist slice: a member's terminal move on
        a box that is not live or TERMINATED, or the direct target of an
        operator's INACTIVE whose parent's run did not move; never a row the
        cascade took INACTIVE."""
        facts = RecomputeFacts(self, box_ir, trigger)
        event = ChartEvent(BOX_RECOMPUTE, facts)
        fired, key, old = self._box_rule(box, event)
        override = _RECOMPUTED.get(fired)
        if override is None:
            self._no_move(box, event, fired)
            return
        with self._moving(box, key, event, old) as agreement:
            target = self._target(agreement.move[1], facts.target, agreement)
            if facts.override != override:
                raise self._differ(
                    f"{fired[0]} fired, and the facts say override={facts.override}", agreement
                )
            if override:
                move, why = IDLE_BOX_OVERRIDE[target], "idle-box override recompute"
            else:
                move, why = IDLE_BOX_DERIVE[target], "idle-box recompute"
            self._set_status(box, move, cause=f"{why} (SEM-15): {cause}")

    # ---- a box's moments, on its members' instances

    def _cascade_job_terminators(self, box: str) -> None:
        """`box ends FAILURE or TERMINATED` on each member (SEM-14): a live
        job_terminator member terminates (job.116); any other moves nothing
        (job.197)."""
        for member in self._members(box):
            event = ChartEvent(BOX_ENDS)
            fired, key, old = self._box_rule(member, event)
            if fired == ("job.116",):
                with self._moving(member, key, event, old):
                    self._terminate(
                        member, JOB_TERMINATOR, cause=f"job_terminator: box {box!r} ended"
                    )
            else:
                self._no_move(member, event, fired)

    def _disarm_members(self, box: str) -> None:
        """`box run ends` on each member (Q3c, DL-54): an unconsumed arm dies
        with the run (job.215), before any wake can ride it; an unarmed member
        keeps nothing (job.228). Every catalog job has a row from the
        oracle's construction, so every member has its instance."""
        for member in self._members(box):
            event = ChartEvent(BOX_RUN_ENDS)
            fired, key, old = self._box_rule(member, event)
            if fired != ("job.215",):
                self._no_move(member, event, fired)
                continue
            self.store.move_flag(member, BOX_END_DISARMS)
            self._record(
                member,
                "SCHED_DISARM",
                f"unconsumed arm dies with box {box!r} run (Q3c pin, DL-54/58)",
            )
            self._agree_after(self._stays(member, key, event, old), "the disarm's writes")

    def _reset_box_cycle(self, box: str, starting: Callable[[], None]) -> None:
        """`box start` on each job the box contains that is not live or queued
        (SEM-10, DL-242): a terminal job is reset to INACTIVE (job.122) on the
        batch INACTIVE path around the box's own STARTING, which `starting`
        writes; an INACTIVE one loses its exit code by a plain write (job.195).
        The audience is the sender's, declared by the worklist slice:
        `_contained(skip_live=True)` leaves out a live or queued job and
        everything inside a live subbox."""
        cause = (
            f"box {box!r} started: statuses from the previous box cycle are not retained"
            " (SEM-10, DL-242)"
        )
        pending: dict[str, Agreement] = {}
        for job in self._contained(box, skip_live=True):
            event = ChartEvent(BOX_START)
            fired, key, old = self._box_rule(job, event)
            if fired == ("job.122",):
                pending[job] = self._batched(job, key, event, old)
                continue
            self._no_move(job, event, fired)
            rt = self.store.job[job]
            if rt.status == "INACTIVE" and rt.exit_code is not None:
                self.store.clear_exit_code(job)
        rows = [(job, cause, BOX_RESET) for job in pending]
        outer, self._batch = self._batch, dict(pending)
        try:
            self._set_inactive_batch(rows, clear_exit_code=True, between=starting)
            self._batch_done()
        finally:
            self._batch = outer

    def _on_box_started(self, box: str, run_number: int) -> None:
        """`box running` on each member: an auto_hold member not on hold is
        held (job.208); any other keeps its flags (job.227). Then each member
        attempts its start (one `start`, DL-342), and the run's window
        decisions wait for the outermost start's pass, as today."""
        for member in self._members(box):
            event = ChartEvent(BOX_RUNNING)
            fired, key, old = self._box_rule(member, event)
            if fired != ("job.208",):
                self._no_move(member, event, fired)
                continue
            self.store.move_flag(member, AUTO_HOLD)
            self._record(member, "ON_HOLD", "auto_hold on box start (dossier ss5)")
            self._agree_after(self._stays(member, key, event, old), "the auto_hold's writes")
        cause = f"box {box!r} started"
        for member in self._members(box):
            self._attempt_start(member, force=False, scheduled=False, cause=cause)
        assert self._window_starts is not None
        self._window_starts.append((box, run_number, cause))

    def _box_off_noexec(self, box: str) -> None:
        """`OFF_NOEXEC on its box` on each job the box contains (DL-254): an
        ON_NOEXEC job is cleared (job.211); any other keeps its flag (job.229)."""
        for member in self._contained(box, skip_live=False):
            event = ChartEvent(BOX_OFF_NOEXEC)
            fired, key, old = self._box_rule(member, event)
            if fired != ("job.211",):
                self._no_move(member, event, fired)
                continue
            self.store.move_flag(member, BOX_NOEXEC_OFF)
            self._record(member, "OFF_NOEXEC", f"box {box!r} taken OFF_NOEXEC (DL-254)")
            self._agree_after(self._stays(member, key, event, old), "the flag's writes")

    def _box_on_noexec(self, box: str, contained: list[str]) -> list[str]:
        """`ON_NOEXEC on its box` on each job the box contains (DL-254,
        DL-347), each through today's `_set_noexec` with today's lines:
        the flag on (job.231, or job.233 for a job already flagged), then the
        hold it supersedes off (job.232). Two dimensions are checked before
        the job's write (the box's own flag is written already): a row the
        chart ignores, where today's rule writes the flag and its line (as
        DL-344 rules for the flag inputs), and whether the hold goes, the
        row's choice. The jobs whose hold it cleared, by the fired row: they
        retry their start once the box's own move is done."""
        store = self.store
        released = []
        for job in contained:
            event = ChartEvent(BOX_ON_NOEXEC)
            store.follow_job(job)
            key, old = store.job_key(job), store.job[job].status
            outcomes = store.machines.dispatch(key, event)
            fired = fired_rows(outcomes)
            clears = _NOEXEC_HOLD.get(fired)
            if clears is None:
                raise self._unwritten(job, event, fired, "box rule row")
            stays = self._stays(job, key, event, old)
            ignored = [ident for outcome in outcomes for ident in outcome.ignored]
            if ignored:
                raise self._differ(
                    f"{', '.join(ignored)} ignores {event.name}, which today's rules apply", stays
                )
            if clears != store.job[job].on_hold:
                held = "held" if store.job[job].on_hold else "not held"
                raise self._differ(f"{', '.join(fired)} fired for a job {held}", stays)
            self._set_noexec(job, self._noexec_cause(box, job))
            self._agree_after(stays, "the flags' writes")
            if clears:
                released.append(job)
        return released

    def _cascaded(self, box: str) -> dict[str, Agreement]:
        """`box set INACTIVE` on each job the box contains (SEM-18, DL-242):
        the moves of the jobs it takes along (job.123, job.159, job.160), in
        order, whose rows the batch writes; a job already INACTIVE stays
        (job.196)."""
        pending: dict[str, Agreement] = {}
        for job in self._contained(box, skip_live=False):
            event = ChartEvent(BOX_SET_INACTIVE)
            fired, key, old = self._box_rule(job, event)
            if fired in _CASCADED:
                pending[job] = self._batched(job, key, event, old)
            else:
                self._no_move(job, event, fired)
        return pending

    def _inject_inactive(
        self,
        job_ir: JobIR,
        code: int | None,
        move: Transition[JobStatus],
        *,
        clear_exit_code: bool = False,
        cause: str = "injected STATUS",
    ) -> None:
        """`Oracle._inject_inactive` with a box's cascade the chart's: each job
        the box contains is dispatched `box set INACTIVE`, and the batch
        writes the ones it moves (`_cascaded`). The rest is today's: the
        member's resolution in a RUNNING box, the Q3c disarm of every box run
        the cascade ends, and the idle parent's recompute. The recompute's
        audience is the sender's, declared by the worklist slice: the parent
        was not RUNNING, STARTING or TERMINATED, is not now, and its run did
        not move."""
        job = job_ir.name
        box = job_ir.box.box_name
        parent = None if box is None else self._runtime(box)
        if box is not None and parent is not None and parent.status == "RUNNING":
            self.store.record_resolution(box, job)
        if job_ir.job_type != "BOX":
            self._set_status(
                job, move, cause=cause, exit_code=code, clear_exit_code=clear_exit_code
            )
        else:
            cascade_cause = (
                f"box {job!r} set INACTIVE: cascades to every job it contains (SEM-18, DL-242)"
            )
            pending = self._cascaded(job)
            boxes = [job] + [j for j in pending if self.catalog.jobs[j].job_type == "BOX"]

            def disarm() -> None:
                # Q3c: the box run ends here, so its unconsumed arms die
                for each in boxes:
                    self._disarm_members(each)

            outer, self._batch = self._batch, dict(pending)
            try:
                self._set_inactive_batch(
                    [(job, cause, move)] + [(j, cascade_cause, BOX_CASCADE) for j in pending],
                    exit_code=code,
                    clear_exit_code=clear_exit_code,
                    between=disarm,
                )
                self._batch_done()
            finally:
                self._batch = outer
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
                box, self.catalog.jobs[box], cause=f"member {job!r} set INACTIVE", trigger=job
            )

    def _batched(self, job: str, key: str, event: ChartEvent, old: str) -> Agreement:
        """A batch row's move, compared once the batch has written and settled it."""
        active = self.store.machines.active(key)
        return Agreement(job, key, event, active, (old, "INACTIVE"), old in LIVE)

    def _batch_done(self) -> None:
        """Every row the chart moved was written by the batch, which compared
        it and took it out of `_batch` (`_settle_row`). A batch inside
        another's cascade begins after the other's rows are written, so each
        keeps its own and the other's comes back after it."""
        assert self._batch is not None
        for agreement in self._batch.values():
            raise self._differ("the batch never wrote the chart's move", agreement)

    # ---- the period opening

    def _release_removed_holders(self) -> None:
        """`first input of a period` on each carried row whose job the catalog
        no longer defines and that holds units while not live (DL-256): its
        units go back (job.308), with today's line; then the waiters wake."""
        owed, self._opening_release = self._opening_release, []
        for job in owed:
            event = ChartEvent(PERIOD_OPENING)
            fired, key, old = self._box_rule(job, event)
            if fired != ("job.308",):
                raise self._unwritten(job, event, fired, "opening row")
            freed = self.store.release_held(job, HELD_RELEASED_AT_OPENING)
            units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
            self._record(
                job,
                "RELEASE_RESOURCE",
                f"job removed from the catalog: its held units go back at the period"
                f" opening (frees {units}; DL-256)",
            )
            self._agree_after(self._stays(job, key, event, old), "the opening's release")
        if owed:
            self._wake_waiters()
