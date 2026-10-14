"""The oracle's cascade on the executor's worklist (docs/statechart-engine.md
§5, §8, §11; DL-350).

A move's consequences are items on the state owner's executor, popped depth
first. The rows' actions stay pure: what a row of a job sends beyond its own
move is read here, from the tier's facts, when the row runs
(`OracleCascade._consequences`, which the payload's handle
`RowUnits.consequences` reaches). What it writes is the hook's
(`oracle.Oracle._perform`), with an intent where the row cannot derive the
write.

The order is set by one rule on the senders. A row's action reports one item
per consequence, in order. A consequence whose gate or audience reads state an
earlier sibling's cascade can change is a moment: a self-event whose payload
is the tier's step (`Program`), which reads the gate when it pops, sends the
next item and itself again, and so takes its next step only after that item's
whole cascade. A step that follows a sibling's whole cascade is a
later-reported item. The moments:

- `box rules` (`_box_rules`): a member's moves at its box (`member ends
  FAILURE or TERMINATED`, the door's `member moves` and `completion
  moment`, `box recompute`), then `walk up` and `holders may leave`, then a
  box's `box run ends` to its members;
- `settle` (`_settle`, late): the move's own `leave STARTING or RUNNING`,
  unless the run settled early (job.401) or the job started again (the LIVE
  cell);
- `wake` (`_wake`): each referencer's `start`, then the queue's `scan` when
  the settlement released units or the move lifted a block;
- `walk up` (`_walk_up`) and `holders may leave` (`_holders`, and an ice's own
  completion moment, `_ice_resolves`);
- `scan` (`_scan`): one `readmit` per waiter of a pass, the pass's list read
  when it starts and each waiter's status at its turn; a pass ends at the
  first waiter that moved, which its `readmit` row reports as an intent; the
  pass bookkeeping (`_in_wake`, `_scan_owed`, `_full_scan_requested`) stays
  the oracle's, written by intents;
- `load taken` (DL-255): the owed scan's mark, behind a freed excess's scan;
- `box ends step` (`_box_ends`): one member at a time, while the run that
  ended is the box's run (DL-348);
- `notify step` (`_notify`, the batch INACTIVE path's second phase): the runs
  read after the batch's first phase, the box's STARTING or its disarms, then
  each row's box rules and wakes;
- `window pass` (`_window_pass`): the pass of the box start that found no
  pass pending, over every box run registered with it;
- `after inject` and `after leave`: an idle parent's recompute after an
  operator's INACTIVE (SEM-15), a waiter's window or day disposition after it
  left the queue (DL-257);
- `retry step` (`_retries`): ON_NOEXEC's start retries, after a box's
  contained jobs took the flag and its own status settled.

An admitted start's extent descends from its `start admitted`, which job.100
counts (DL-304): the units, the arm, a box's resets, the move to STARTING
(`starting`, out of the transient `ADMITTED`), `started`, a freed excess's
scan and the load mark. A readmission sends the same extent from its own row:
it is no start the bound counts.

Audiences are the sender's (DL-346): each gate is read in the action or the
moment that sends the item, never on the recipient's row.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, cast

from dsl41.capacity import DemandEntry, takes_machine_load, to_reservations
from dsl41.ir import CatalogIR
from dsl41.job_chart import (
    ACTUAL_START,
    ADMITTED_START,
    AFTER_INJECT,
    AFTER_LEAVE,
    BOX_ENDS,
    BOX_ENDS_STEP,
    BOX_OFF_NOEXEC,
    BOX_ON_NOEXEC,
    BOX_RECOMPUTE,
    BOX_RULES,
    BOX_RUN_ENDS,
    BOX_RUNNING,
    BOX_SET_INACTIVE,
    BOX_START,
    COMPLETION_MOMENT,
    FLAG_MOVED,
    HOLDERS,
    LEAVE_LIVE,
    LOAD_TAKEN,
    MEMBER_ENDS,
    MEMBER_MOVES,
    NOTIFY_STEP,
    READMIT,
    RETRY_STEP,
    SCAN,
    SETTLE,
    START,
    START_ADMITTED,
    STARTED,
    STARTING,
    WAKE,
    WALK_UP,
    WINDOW_PASS,
    Admission,
    Admitted,
    FlagMoved,
    JobData,
    Settlement,
)
from dsl41.oracle_facts import (
    FoldFacts,
    OverrideFacts,
    ReadmitFacts,
    RecomputeFacts,
    RowUnits,
    StartFacts,
    Started,
)
from dsl41.oracle_intents import (
    AdmissionFacts,
    Batch,
    ClearCodes,
    ClosePass,
    DeferToOpening,
    FlagFacts,
    Intent,
    MemberEnd,
    Move,
    Moved,
    Notice,
    Once,
    OpenPass,
    Program,
    Readmitted,
    Register,
    Rule,
    RunStart,
    ScanState,
    Send,
    Settle,
    SettledEarly,
    Starting,
    StatusInput,
    Step,
    WatchReadmit,
)
from dsl41.oracle_schedule import OracleSchedule
from dsl41.oracle_state import BOX_START as BOX_START_MOVE
from dsl41.oracle_state import (
    FAILED,
    JOB_FORCE_ON_HELD,
    JOB_READMIT,
    JOB_START,
    LIVE,
    TERMINAL,
    CarriedRows,
    JobStatus,
    Transition,
)
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Context, Effects, StatechartError
from dsl41.state_machine import Event as ChartEvent

_LIVE_OR_QUEUED: Final = LIVE | {"QUE_WAIT"}
#: The kinds of a status move whose consequences are its box rules, its
#: settlement and its wakes, with what follows them.
_MOVES: Final = frozenset(
    {"moved", "terminated", "completed", "injected", "cancelled", "left queue", "runs box"}
)


class OracleCascade(OracleSchedule):
    """`OracleSchedule` with the cascade on the executor's worklist: the
    consequences a row's action reports, the moments, and their bookkeeping."""

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
        #: DL-247: the queue scan is in a pass (`_scan`)
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
        #: SEM-33 (DL-246): the box runs whose start is in progress, as (box,
        #: run number, cause), collected from the outermost box's RUNNING
        #: on, so the window decisions of a whole started subtree run after
        #: every attempt in it (`_window_pass`). None when no box start is open.
        self._window_starts: list[tuple[str, int, str]] | None = None
        #: DL-340: per job, the moves that ended a run whose settlement waits
        #: behind the move's box rules (`Move.apply`, `_w_settle`). Only a job
        #: with a settlement in flight has an entry, so the dict is empty
        #: between inputs.
        self._late_settlements: dict[str, list[Move]] = {}
        #: the last `readmit` the scan sent moved its waiter (`Readmitted`)
        self._readmitted = False

    def fork(self) -> OracleCascade:
        """`OracleSchedule.fork` with the cascade's bookkeeping copied. Between
        inputs no ended run waits for its settlement."""
        assert not self._late_settlements, "a fork with a settlement still owed"
        twin = cast(OracleCascade, super().fork())
        twin._window_starts = None if self._window_starts is None else list(self._window_starts)
        twin._late_settlements = {}
        return twin

    # ---- small reads

    def _to(self, job: str, name: str, payload: object = None) -> Send:
        return self.store.job_key(job), ChartEvent(name, payload)

    def _units(self, job: str) -> RowUnits:
        return RowUnits(self, job)

    def _is_box(self, job: str) -> bool:
        job_ir = self.catalog.jobs.get(job)
        return job_ir is not None and job_ir.job_type == "BOX"

    def _move(self, job: str, batch: Batch | None = None) -> Move:
        row = self._runtime(job)
        return Move(job, row.status, row.run_number, batch)

    def _drop_rank(self, job: str) -> None:
        """A row that left QUE_WAIT drops its rank (DL-120)."""
        row = self._runtime(job)
        if row.status != "QUE_WAIT" and row.waiter_seq is not None:
            self.store.dequeue_waiter(job)

    def _attempt(self, job: str, cause: str, **facts: Any) -> StartFacts:
        return StartFacts(self, job, self.catalog.jobs.get(job), cause=cause, **facts)

    def _program(self, job: str, name: str, steps: Iterator[Step]) -> Send:
        return self._to(job, name, Program(f"{name} of {job}", steps))

    def _once(self, job: str, name: str, steps: Iterator[Step]) -> Send:
        return self._to(job, name, Once(f"{name} of {job}", steps))

    # ---- what a row sends beyond its own move

    def _consequences(self, job: str, ctx: Context, kind: str) -> Effects | None:
        """What a row of `kind` of the job sends and reports beyond its own
        move, read now: the row's action runs before the hook writes the move."""
        if kind in _MOVES:
            return self._after(job, ctx, kind)
        handler = getattr(self, "_on_" + kind.replace(" ", "_"))
        result: Effects | None = handler(job, ctx)
        return result

    def _after(self, job: str, ctx: Context, kind: str) -> Effects:
        """A status move on its own: its box
        rules, its settlement, its wakes, then what follows them by kind."""
        move = self._move(job)
        intents: list[Intent] = [move]
        tail: list[Send] = []
        if kind == "terminated" and self._is_box(job):
            # the run that ended, read before the write (DL-348)
            tail.append(self._program(job, BOX_ENDS_STEP, self._box_ends(job, move.run)))
        elif kind == "completed":
            # a box's completion: the run is read after the end's wakes
            tail.append(self._program(job, BOX_ENDS_STEP, self._box_ends(job, None)))
        elif kind == "injected":
            tail += self._after_inject(job)
        elif kind == "cancelled":
            intents.append(Readmitted())
        elif kind == "left queue":
            tail.append(self._after_leave(job, cast(FlagFacts, ctx.event.payload)))
        elif kind == "runs box":
            tail += self._box_running(job, move.run, intents)
        return Effects(send=(*self._move_sends(move), *tail), intents=tuple(intents))

    def _move_sends(self, move: Move) -> tuple[Send, ...]:
        job = move.job
        return (
            self._program(job, BOX_RULES, self._box_rules(move, None)),
            self._to(job, SETTLE, Settle(move, self._settle(move))),
            self._once(job, WAKE, self._wake_after(move)),
        )

    def _after_inject(self, job: str) -> list[Send]:
        """An operator's INACTIVE on a member: its parent, read before the move: its recompute
        follows the move's cascade (SEM-15)."""
        box = self.catalog.jobs[job].box.box_name
        if box is None:
            return []
        parent = self._runtime(box)
        return [
            self._once(
                job, AFTER_INJECT, self._inject_tail(job, box, parent.status, parent.run_number)
            )
        ]

    def _after_leave(self, job: str, moved: FlagFacts) -> Send:
        """A waiter's disposition after it left the queue, with the runs read before the move."""
        facts = cast(ReadmitFacts, moved.facts)
        check, why = facts.known("recheck")
        mode = self.semantics.queued_recheck
        cause = f"QUE_WAIT left unstarted: {why} (queued-recheck={mode}, DL-257)"
        box = self.catalog.jobs[job].box.box_name
        box_run = None if box is None else self._runtime(box).run_number
        run = self._runtime(job).run_number
        return self._once(job, AFTER_LEAVE, self._leave_tail(job, check, cause, run, box_run))

    def _box_running(self, box: str, run: int, intents: list[Intent]) -> list[Send]:
        """After a box's RUNNING: its members'
        auto_hold, their starts, then the window pass, opened here when none
        is pending (DL-246)."""
        owner = self._window_starts is None
        if owner:
            intents.append(OpenPass())
        cause = f"box {box!r} started"
        members = self._members(box)
        return [
            *(self._to(m, BOX_RUNNING, Rule(box=box, units=self._units(m))) for m in members),
            *(self._to(m, START, self._attempt(m, cause)) for m in members),
            self._program(box, WINDOW_PASS, self._window_pass(box, run, cause, owner)),
        ]

    # ---- the start family

    def _admitted(self, job: str, ctx: Context, verdict: str, admission: Admission) -> Effects:
        attempt = cast(StartFacts, ctx.event.payload)
        move = self._move(job) if verdict in ("bypass", "queue") else None
        admitted = Admitted(
            verdict,  # type: ignore[arg-type]
            admission,
            move=move,
            attempt=attempt,
            units=self._units(job),
        )
        return Effects(
            send=(self._to(job, START_ADMITTED, admitted),),
            intents=() if move is None else (move,),
        )

    def _on_bypass(self, job: str, ctx: Context) -> Effects:
        return self._admitted(job, ctx, "bypass", Admission())

    def _on_queue(self, job: str, ctx: Context) -> Effects:
        return self._admitted(job, ctx, "queue", Admission())

    def _on_admit(self, job: str, ctx: Context) -> Effects:
        attempt = cast(StartFacts, ctx.event.payload)
        vector = tuple(attempt.vector())
        admission = AdmissionFacts(demand=attempt.demand, vector=vector, units=self._units(job))
        return self._admitted(job, ctx, "admit", admission)

    def _on_on_held(self, job: str, ctx: Context) -> Effects:
        attempt = cast(StartFacts, ctx.event.payload)
        vector = tuple(attempt.vector())
        admission = AdmissionFacts(on_held=True, vector=vector, units=self._units(job))
        return self._admitted(job, ctx, "on_held", admission)

    def _on_extent(self, job: str, ctx: Context) -> Effects:
        """`start admitted` (job.402): the admitted start's extent, each step an
        item, all of them descendants of this one."""
        admitted = cast(Admitted, ctx.event.payload)
        attempt = cast(StartFacts, admitted.attempt)
        cause = attempt.cause
        if admitted.verdict == "bypass":
            run = RunStart(cause=f"ON_NOEXEC bypass ({cause})", arm=False, units=self._units(job))
            move = cast(Move, admitted.move)
            return Effects(send=(self._to(job, ACTUAL_START, run), *self._move_sends(move)))
        vector = attempt.vector()
        load = (
            (self._once(job, LOAD_TAKEN, self._load_taken()),) if takes_machine_load(vector) else ()
        )
        if admitted.verdict == "queue":
            return Effects(send=(*self._move_sends(cast(Move, admitted.move)), *load))
        admission = cast(AdmissionFacts, admitted.admission)
        if admitted.verdict == "on_held":
            cause = f"{cause}; starts on its held units, other resources not re-evaluated (DL-256)"
            sends = self._extent(job, admission, JOB_FORCE_ON_HELD, cause, had_demand=True)
            return Effects(send=(*sends, *load))
        is_box = self._is_box(job)
        sends = self._extent(
            job, admission, BOX_START_MOVE if is_box else JOB_START, cause, bool(vector)
        )
        freed = self._frees_excess(job, list(vector))
        scan = (self._program(job, SCAN, self._scan(keep_stopped=False)),) if freed else ()
        return Effects(send=(*sends, *scan, *load))

    def _frees_excess(self, job: str, vector: list[DemandEntry]) -> bool:
        """Read before the units are written: held units exceed the new
        demand on some bucket, so the queue is owed a wake (DL-256)."""
        held = self._runtime(job).reservations
        if not held:
            return False
        demand: dict[str, int] = {}
        for reservation in to_reservations(vector):
            demand[reservation.bucket] = demand.get(reservation.bucket, 0) + reservation.units
        return any(h.units > demand.get(h.bucket, 0) for h in held)

    def _on_readmit(self, job: str, ctx: Context) -> Effects:
        """job.108: a readmission, its extent sent from its own row."""
        facts = cast(ReadmitFacts, ctx.event.payload)
        vector = tuple(facts.vector())
        admission = AdmissionFacts(demand=facts.demand, vector=vector, units=self._units(job))
        cause = "resources freed (QUE_WAIT admitted, DL-50)"
        sends = self._extent(job, admission, JOB_READMIT, cause, had_demand=True)
        return Effects(send=sends, intents=(Readmitted(),))

    def _extent(
        self,
        job: str,
        admission: Admission,
        move: Transition[JobStatus],
        cause: str,
        had_demand: bool,
    ) -> tuple[Send, ...]:
        """An admitted start's steps: the units, the arm and the run, a box's
        resets around its STARTING or a job's STARTING, then `started`."""
        run = self._runtime(job).run_number
        starting = Starting(move=move, cause=cause, units=self._units(job))
        sends: list[Send] = [
            self._to(job, ADMITTED_START, admission),
            self._to(job, ACTUAL_START, RunStart(cause=cause, units=self._units(job))),
        ]
        if self._is_box(job):
            batch = Batch(job, starting=starting)
            for member in self._reset_audience(job):
                sends.append(
                    self._to(
                        member, BOX_START, Rule(box=job, batch=batch, units=self._units(member))
                    )
                )
            sends.append(self._program(job, NOTIFY_STEP, self._notify(batch)))
        else:
            sends.append(self._to(job, STARTING, starting))
        started = Started(self, job, run + 1, had_demand=had_demand)
        sends.append(self._to(job, STARTED, started))
        return tuple(sends)

    def _on_settle_first(self, job: str, ctx: Context) -> Effects:
        """job.401 (DL-340): the ended run settles by the end it made, and the
        same attempt is made again, reading the admission it leaves."""
        run = self._runtime(job).run_number
        lates = cast(list[Move], self._late_settlements.get(job, []))
        late = next((x for x in lates if x.run == run and not x.done), None)
        if late is None:
            raise StatechartError(
                f"job {job!r}: job.401 fired, and no settlement of run {run} is owed"
            )
        settlement = Settlement(late.new == "SUCCESS", units=self._units(job), move=late)
        attempt = cast(StartFacts, ctx.event.payload)
        return Effects(
            send=(self._to(job, LEAVE_LIVE, settlement), self._to(job, START, attempt.settled())),
            intents=(SettledEarly(late),),
        )

    def _on_skips_in_box(self, job: str, ctx: Context) -> Effects:
        """job.404: a window skip of a member already INACTIVE: its box
        rules, as an INACTIVE move's, with no settlement and no wake."""
        notice = Notice(job, "INACTIVE", "INACTIVE")
        return Effects(send=(self._program(job, BOX_RULES, self._box_rules(notice, None)),))

    # ---- the batch INACTIVE path

    def _on_cascades(self, job: str, ctx: Context) -> Effects:
        """A box set INACTIVE by an operator or by ON_NOEXEC (the batch INACTIVE
        path, SEM-18): its own row first, its settlement, then every job it contains,
        then the second phase, then the idle parent's recompute."""
        noexec = ctx.event.name == FLAG_MOVED
        if noexec:
            batch = Batch(job, clear=True)
        else:
            batch = Batch(job, code=cast(StatusInput, ctx.event.payload).code)
        move = self._move(job, batch)
        sends: list[Send] = []
        if move.old in LIVE:
            sends.append(self._to(job, LEAVE_LIVE, Settlement(units=self._units(job), move=move)))
        for member in self._contained(job, skip_live=False):
            sends.append(
                self._to(
                    member, BOX_SET_INACTIVE, Rule(box=job, batch=batch, units=self._units(member))
                )
            )
        sends.append(self._program(job, NOTIFY_STEP, self._notify(batch)))
        sends += self._after_inject(job)
        return Effects(send=tuple(sends), intents=(move,))

    def _on_batched(self, job: str, ctx: Context) -> Effects:
        """A row of the batch's first phase: written and settled, nothing more."""
        rule = cast(Rule, ctx.event.payload)
        move = self._move(job, rule.batch)
        settle: tuple[Send, ...] = ()
        if move.old in LIVE:
            settle = (self._to(job, LEAVE_LIVE, Settlement(units=self._units(job), move=move)),)
        return Effects(send=settle, intents=(move,))

    def _notify(self, batch: Batch) -> Iterator[Step]:
        """The batch INACTIVE path after its first phase: each row's ancestors'
        runs, read once now; the box's STARTING, or the disarms of every box run
        the cascade ends; then each row still INACTIVE in its run gets its box
        rules and its wakes, and a row a wake moved on owes the queue its wake."""
        runs = {m.job: self._ancestor_runs(m.job) for m in batch.rows}
        if batch.starting is not None:
            yield Step(send=(self._to(batch.box, STARTING, batch.starting),))
        else:
            boxes = [batch.box] + [m.job for m in batch.rows[1:] if self._is_box(m.job)]
            disarms = tuple(
                self._to(member, BOX_RUN_ENDS, Rule(box=each, units=self._units(member)))
                for each in boxes
                for member in self._members(each)
            )
            if disarms:
                yield Step(send=disarms)
        owed = False
        for move in batch.rows:
            row = self._runtime(move.job)
            wake = move.released or self._lifts_a_block(move.old, "INACTIVE")
            if row.status != "INACTIVE" or row.run_number != move.run:
                owed = owed or wake
                continue
            notice = Notice(move.job, move.old, "INACTIVE")
            yield Step(
                send=(
                    self._program(move.job, BOX_RULES, self._box_rules(notice, runs[move.job])),
                    self._once(move.job, WAKE, self._wake(move.job, "INACTIVE", wake)),
                )
            )
        if owed:
            yield Step(send=(self._program(batch.box, SCAN, self._scan(keep_stopped=False)),))

    # ---- the audiences: each gate the sender reads (DL-346, DL-348)

    def _reset_audience(self, box: str) -> list[str]:
        """A box start resets every job it contains but a live or queued one
        and the subtree of a live subbox (`_contained(skip_live=True)`)."""
        return self._contained(box, skip_live=True)

    @staticmethod
    def _reads_overrides(new: str, resolved: bool) -> bool:
        """A RUNNING box's door, and each box above it, read their overrides
        on a member's terminal, RUNNING or resolving move only (SEM-12)."""
        return new in TERMINAL | {"RUNNING"} or resolved

    @staticmethod
    def _recomputes(status: str, new: str) -> bool:
        """An idle box, not live and not TERMINATED, re-derives its status on
        a member's terminal move (SEM-15)."""
        return status not in LIVE and new in TERMINAL

    def _in_run(self, box: str, run: int) -> bool:
        """The walk up reaches a box above the parent only while it is still
        RUNNING the run it ran before the moment (SEM-12, DL-304)."""
        row = self._runtime(box)
        return row.status == "RUNNING" and row.run_number == run

    def _parent_recomputes(self, box: str, status: str, run: int) -> bool:
        """An operator's INACTIVE recomputes an idle parent that was not
        running, starting or TERMINATED, is not now, and whose run did not
        move (SEM-15)."""
        settled = ("RUNNING", "STARTING", "TERMINATED")
        now = self._runtime(box)
        return status not in settled and now.status not in settled and now.run_number == run

    def _still_the_run(self, box: str, run: int) -> bool:
        """A box's end reaches its next job_terminator only while the run that
        ended is still the box's run, read before each member (DL-348)."""
        return self._runtime(box).run_number == run

    # ---- a move's box rules, settlement and wakes

    def _box_rules(self, moved: Moved, runs: Mapping[str, int] | None) -> Iterator[Step]:
        """A move's box rules: the parent's member rules, the walk up, the holders'
        check, and a terminal box's end of its members' arms (Q3c)."""
        job, old, new = moved.job, moved.old, moved.new
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None:
            return
        box = job_ir.box.box_name
        if box is not None:
            if runs is None:
                runs = self._ancestor_runs(job)
            yield from self._member_rules(box, job, old, new)
            chain = self._ancestor_boxes(job)
            if len(chain) > 1:
                resolved = self._resolves(box, job, old, new)
                walk = self._walk_up(job, new, resolved, runs)
                yield Step(send=(self._program(job, WALK_UP, walk),))
            if old in _LIVE_OR_QUEUED and new not in _LIVE_OR_QUEUED and len(chain) > 1:
                yield Step(send=(self._program(job, HOLDERS, self._holders(job, runs)),))
        if job_ir.job_type == "BOX" and new in TERMINAL:
            disarms = tuple(
                self._to(m, BOX_RUN_ENDS, Rule(box=job, units=self._units(m)))
                for m in self._members(job)
            )
            if disarms:
                yield Step(send=disarms)

    def _member_rules(self, box: str, member: str, old: str, new: str) -> Iterator[Step]:
        """A member's move at its box: a member that ends FAILURE or TERMINATED
        meets the box's terminator (SEM-14), which ends a RUNNING box and the
        rules; a TERMINATED box is sticky (SEM-13); a RUNNING box runs its door
        on a terminal, RUNNING or resolving move, and its fold on any move; an
        idle box re-derives its status on a terminal move (SEM-15)."""
        status = self._runtime(box).status
        if new in FAILED:
            fires = self._box_terminator_fires(member, new)
            end = MemberEnd(fires, member=member, new=new, units=self._units(box))
            yield Step(send=(self._to(box, MEMBER_ENDS, end),))
            if fires and status == "RUNNING":
                return
        if status == "TERMINATED":
            return
        resolved = self._resolves(box, member, old, new)
        if status == "RUNNING":
            overrides = self._reads_overrides(new, resolved)
            yield from self._door(box, member, new, completion=resolved, overrides=overrides)
        elif self._recomputes(status, new):
            facts = RecomputeFacts(
                self, self.catalog.jobs[box], member, f"member {member!r} changed"
            )
            yield Step(send=(self._to(box, BOX_RECOMPUTE, facts),))

    def _door(
        self,
        box: str,
        member: str | None,
        new: str | None,
        *,
        completion: bool,
        overrides: bool,
        at_start: bool = False,
    ) -> Iterator[Step]:
        """The completion door of a RUNNING box: its overrides, then, unless one
        ended the box, its default fold (SEM-11, SEM-12)."""
        box_ir = self.catalog.jobs[box]
        if overrides:
            run = self._runtime(box).run_number
            facts = OverrideFacts(self, box_ir, member, new, completion_moment=completion)
            yield Step(send=(self._to(box, MEMBER_MOVES, facts),))
            row = self._runtime(box)
            if row.status != "RUNNING" or row.run_number != run:
                return
        fold = FoldFacts(self, box_ir, at_start=at_start)
        yield Step(send=(self._to(box, COMPLETION_MOMENT, fold),))

    def _walk_up(
        self, member: str, new: str, resolved: bool, runs: Mapping[str, int]
    ) -> Iterator[Step]:
        """The walk up: each box above the parent still in the
        run it was in before the moment reads its overrides on a terminal,
        RUNNING or resolving move; the walk ends at one that ended (SEM-12)."""
        for box in self._ancestor_boxes(member)[1:]:
            row = self._runtime(box)
            if not self._in_run(box, runs[box]):
                continue
            if self._reads_overrides(new, resolved):
                facts = OverrideFacts(
                    self, self.catalog.jobs[box], member, new, completion_moment=resolved
                )
                yield Step(send=(self._to(box, MEMBER_MOVES, facts),))
                after = self._runtime(box)
                if after.status != "RUNNING" or after.run_number != row.run_number:
                    return

    def _holders(self, job: str, runs: Mapping[str, int]) -> Iterator[Step]:
        """The holders' check: each iced box above the job whose parent
        is still in its run gets its completion moment (SEM-20, DL-304)."""
        chain = self._ancestor_boxes(job)
        for holder, parent in zip(chain, chain[1:], strict=False):
            if self._runtime(parent).run_number == runs[parent]:
                yield from self._ice_resolves(holder, self._runtime(holder).status, runs)

    def _ice_resolves(self, job: str, status: str, runs: Mapping[str, int]) -> Iterator[Step]:
        """An iced member out of its RUNNING box's run is
        a completion moment for the box (DL-285), and the walk up follows while
        the box is still in its run."""
        job_ir = self.catalog.jobs.get(job)
        box = job_ir.box.box_name if job_ir is not None else None
        if box is None:
            return
        row = self._runtime(box)
        if (
            row.status != "RUNNING"
            or job in row.ran_members
            or job in row.window_skipped_members
            or job in row.iced_out_members
            or not self._out_of_the_run(job)
        ):
            return
        yield from self._door(box, job, status, completion=True, overrides=True)
        row = self._runtime(box)
        if row.status == "RUNNING" and row.run_number == runs[box]:
            yield from self._walk_up(job, status, True, runs)

    def _settle(self, move: Move) -> Iterator[Step]:
        """The settlement after the box rules: the run's units settle by the
        policy of its end, unless they settled early (job.401) or the job holds
        a later run's; the LIVE cell takes a job started again."""
        # the run compare is the settlement's defence: a job started again
        # inside its own box rules settled its run first (job.401)
        later = self._runtime(move.job).run_number != move.run
        if not move.done and move.releasing and not later:
            settlement = Settlement(move.new == "SUCCESS", units=self._units(move.job), move=move)
            yield Step(send=(self._to(move.job, LEAVE_LIVE, settlement),))

    def _wake_after(self, move: Move) -> Iterator[Step]:
        queue = move.released or self._lifts_a_block(move.old, move.new)
        yield from self._wake(move.job, move.new, queue)

    def _wake(self, job: str, new: str, queue: bool) -> Iterator[Step]:
        """A move's wakes: each referencer's attempt, in catalog order (DL-13),
        then the queue's scan when units were released or a block lifted."""
        yield from self._wakes(job, f"status of {job!r} changed to {new}", queue)

    def _wakes(self, job: str, cause: str, queue: bool) -> Iterator[Step]:
        sends = [
            self._to(name, START, self._attempt(name, cause))
            for name in self._referencers.get(job, ())
        ]
        if queue:
            sends.append(self._program(job, SCAN, self._scan(keep_stopped=False)))
        if sends:
            yield Step(send=tuple(sends))

    def _box_ends(self, box: str, run: int | None) -> Iterator[Step]:
        """A box's end at its job_terminators: each member of the run that ended gets
        `box ends FAILURE or TERMINATED`, while that run is still the box's
        (DL-348). With no run given, the box's own end after its wakes decides
        it (a completion)."""
        if run is None:
            row = self._runtime(box)
            if row.status not in FAILED:
                return
            run = row.run_number
        for member in self._members(box):
            if not self._still_the_run(box, run):
                return
            yield Step(send=(self._to(member, BOX_ENDS, Rule(box=box, units=self._units(member))),))

    def _inject_tail(self, job: str, box: str, status: str, run: int) -> Iterator[Step]:
        """An operator's INACTIVE: SEM-15 on an idle parent, unless the move's own
        wakes started it, or it was running, starting or sticky TERMINATED."""
        if self._parent_recomputes(box, status, run):
            facts = RecomputeFacts(
                self, self.catalog.jobs[box], job, f"member {job!r} set INACTIVE"
            )
            yield Step(send=(self._to(box, BOX_RECOMPUTE, facts),))

    def _leave_tail(
        self, job: str, check: str, cause: str, run: int, box_run: int | None
    ) -> Iterator[Step]:
        """A waiter that left the queue, after the move: unless a wake moved the job
        on or ended its box's run, a window failure takes its window decision
        and a tickless day failure waits for its next eligible opening."""
        row = self._runtime(job)
        if row.status != "INACTIVE" or row.run_number != run:
            return
        job_ir = self.catalog.jobs[job]
        box = job_ir.box.box_name
        if box is not None:
            box_row = self._runtime(box)
            if box_row.status != "RUNNING" or box_row.run_number != box_run:
                return
        if check == "window":
            yield Step(send=(self._to(job, START, self._attempt(job, cause, decided=True)),))
        elif check == "day" and not self._has_ticks(job_ir):
            yield Step(intents=(DeferToOpening(job_ir, cause),))

    def _load_taken(self) -> Iterator[Step]:
        """DL-255: an admitted start's load owes the input's admit-only scan,
        marked behind a freed excess's scan, which clears the mark."""
        yield Step(intents=(ScanState(owed=True),))

    # ---- the queue

    def _scan(self, *, keep_stopped: bool) -> Iterator[Step]:
        """The queue's scan, to a fixpoint: a scan inside a pass asks a full pass
        of an admit-only one and does nothing more; a pass reads the waiters'
        order when it starts and each waiter's status at its turn, and ends at
        the first `readmit` that moved its waiter, which starts the next pass."""
        if self._in_wake:
            if not keep_stopped:  # pragma: no branch -- see below
                # The arm with keep_stopped True is unreachable: the owed scan
                # is a root item of its own, after the input's macrosteps, so
                # it never meets a pass in progress (`_run_owed_scan`).
                yield Step(intents=(ScanState(full=True),))
            return
        begin: tuple[Intent, ...] = (ScanState(in_wake=True),)
        while True:
            waiters = self._pool.sorted_waiters(self.store.job)
            begin += (ScanState(owed=False, full=False),)
            moved = False
            for job in waiters:
                if self.store.job[job].status != "QUE_WAIT":
                    continue
                facts = ReadmitFacts(self, job, self.catalog.jobs[job], keep_stopped=keep_stopped)
                yield Step(send=(self._to(job, READMIT, facts),), intents=(*begin, WatchReadmit()))
                begin = ()
                if self._readmitted:
                    moved = True
                    break
            if begin:
                yield Step(intents=begin)
                begin = ()
            if self._full_scan_requested and keep_stopped:
                keep_stopped = False
                continue
            if not moved:
                break
        yield Step(intents=(ScanState(in_wake=False),))

    def _on_held(self, job: str, ctx: Context) -> Effects | None:
        """ON_HOLD on a waiter: a held waiter blocks no one (DL-247)."""
        if "QUE_WAIT" not in ctx.active:
            return None
        return Effects(send=(self._program(job, SCAN, self._scan(keep_stopped=False)),))

    def _on_released_hold(self, job: str, ctx: Context) -> Effects:
        """OFF_HOLD: a waiter's queue is scanned (DL-50); another job attempts
        its start (SEM-21)."""
        if "QUE_WAIT" in ctx.active:
            return Effects(send=(self._program(job, SCAN, self._scan(keep_stopped=False)),))
        return Effects(send=(self._to(job, START, self._attempt(job, "OFF_HOLD")),))

    def _on_released_held(self, job: str, ctx: Context) -> Effects:
        """RELEASE_RESOURCE gave held units back: the waiters wake (DL-256)."""
        return Effects(send=(self._program(job, SCAN, self._scan(keep_stopped=False)),))

    def _on_waiter_left(self, job: str, ctx: Context) -> Effects:
        """A waiter that fails the queued recheck: its `flag moved`, with the
        scan's facts, and the pass hears that it moved (DL-257)."""
        facts = cast(ReadmitFacts, ctx.event.payload)
        moved = FlagFacts("readmit", facts=facts, units=self._units(job))
        return Effects(send=(self._to(job, FLAG_MOVED, moved),), intents=(Readmitted(),))

    # ---- the flags

    def _on_iced(self, job: str, ctx: Context) -> Effects:
        """ON_ICE (SEM-20, DL-285): the status rule follows the flag; a job that
        is not queued then runs its completion moment and wakes its referencers."""
        moved = self._to(job, FLAG_MOVED, FlagMoved("ON_ICE", units=self._units(job)))
        if "QUE_WAIT" in ctx.active:
            return Effects(send=(moved,))
        status = self._runtime(job).status
        runs = self._ancestor_runs(job)
        resolves = self._program(job, HOLDERS, self._ice_resolves(job, status, runs))
        wakes = self._once(job, WAKE, self._wakes(job, f"{job!r} put ON_ICE", False))
        return Effects(send=(moved, resolves, wakes))

    def _on_iced_again(self, job: str, ctx: Context) -> Effects:
        """ON_ICE on a job on ice: no completion moment, the wakes again."""
        return Effects(
            send=(self._once(job, WAKE, self._wakes(job, f"{job!r} put ON_ICE", False)),)
        )

    def _on_noexec(self, job: str, ctx: Context) -> Effects:
        """ON_NOEXEC (DL-254, DL-347): on a box, each job it contains takes
        the flag first; the status rule follows; then the jobs it released
        from a hold or the queue retry their start, each unless its run moved
        since the input, read now."""
        data = cast(JobData, ctx.data)
        moved = self._to(
            job, FLAG_MOVED, FlagMoved("ON_NOEXEC", ctx.event.payload, units=self._units(job))
        )
        row = self._runtime(job)
        if not data.box:
            released = [job] if data.catalog and (row.on_hold or row.status == "QUE_WAIT") else []
            if not released:
                return Effects(send=(moved,))
            retries = self._retries(released, {job: row.run_number})
            return Effects(send=(moved, self._program(job, RETRY_STEP, retries)))
        tree = [job, *self._contained(job, skip_live=False)]
        runs = {j: self._runtime(j).run_number for j in tree}
        released = [j for j in tree if self._runtime(j).on_hold]
        flags = tuple(
            self._to(j, BOX_ON_NOEXEC, Rule(box=job, units=self._units(j))) for j in tree[1:]
        )
        tail = self._noexec_tail(tuple(tree), moved, released, runs)
        return Effects(send=(*flags, self._program(job, RETRY_STEP, tail)))

    def _noexec_tail(
        self, tree: tuple[str, ...], moved: Send, released: Sequence[str], runs: Mapping[str, int]
    ) -> Iterator[Step]:
        """A box's ON_NOEXEC after the flags: the exit codes go, the box's status
        rule follows, then the retries."""
        yield Step(intents=(ClearCodes(tree),))
        yield Step(send=(moved,))
        yield from self._retries(released, runs)

    def _retries(self, released: Sequence[str], runs: Mapping[str, int]) -> Iterator[Step]:
        """Each released job's start, unless a wake of the same input
        started it already (its run moved)."""
        for job in released:
            if self._runtime(job).run_number == runs[job]:
                yield Step(send=(self._to(job, START, self._attempt(job, "ON_NOEXEC (DL-254)")),))

    def _on_cleared_noexec(self, job: str, ctx: Context) -> Effects | None:
        """OFF_NOEXEC on a box: every job it contains is cleared (DL-254)."""
        if not cast(JobData, ctx.data).box:
            return None
        return Effects(
            send=tuple(
                self._to(j, BOX_OFF_NOEXEC, Rule(box=job, units=self._units(j)))
                for j in self._contained(job, skip_live=False)
            )
        )

    # ---- the window pass

    def _window_pass(self, box: str, run: int, cause: str, owner: bool) -> Iterator[Step]:
        """A box run joins the pending pass; the box start that opened it runs
        it: every run_window member still
        waiting in a run it began, deferrals before skips, each decided by its
        window alone while its box run lasts; then, under
        `box-start-all-members-out=complete`, each box run that left no member
        in the run is a completion moment (SEM-11, DL-246)."""
        if not owner:
            yield Step(intents=(Register(box, run, cause),))
            return
        starts = [*cast(list[tuple[str, int, str]], self._window_starts), (box, run, cause)]
        yield Step(intents=(ClosePass(),))
        waiting: dict[str, list[tuple[str, int, str, str]]] = {"defer": [], "skip": []}
        for each, each_run, each_cause in starts:
            row = self._runtime(each)
            if row.status != "RUNNING" or row.run_number != each_run:
                continue
            for member in self._members(each):
                member_ir = self.catalog.jobs[member]
                if not self._waiting_for_window(each, member_ir):
                    continue
                side, _ = self._window_side(member_ir)
                if side != "inside":
                    waiting[side].append((each, each_run, member, each_cause))
        for each, each_run, member, each_cause in waiting["defer"] + waiting["skip"]:
            row = self._runtime(each)
            if row.status == "RUNNING" and row.run_number == each_run:
                attempt = self._attempt(member, each_cause, decided=True)
                yield Step(send=(self._to(member, START, attempt),))
        # PENDING: Q15 -- no vendor sentence names a box with no member in the run
        if self.semantics.box_start_all_members_out != "complete":
            return
        for each, each_run, _cause in starts:
            row = self._runtime(each)
            if row.status != "RUNNING" or row.run_number != each_run:
                continue
            if all(self._out_of_the_run(member) for member in self._members(each)):
                yield from self._door(
                    each, None, None, completion=True, overrides=True, at_start=True
                )
