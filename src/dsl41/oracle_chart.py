"""The second oracle: `Oracle` with its job rows driven by the `job` statechart
(docs/statechart-engine.md §16, DL-341).

`ChartOracle` is built beside the old oracle and converted one input family at
a time, under the old-versus-new harness (tests/oracle_lockstep.py, DL-338),
which names it as `dsl41.oracle_chart:ChartOracle`. It overrides the handlers
of the families converted so far and inherits every other one, so `oracle.py`
stays the twin it is compared with. The STATUS family, the start attempt
(DL-342), KILLJOB and the TIMER input (DL-343) are converted. The flag
inputs, RELEASE_RESOURCE and SET_GLOBAL, the cascade between instances
(the box rules and the wakes), readmission out of the queue, the
registration of `job` in `machines.MACHINES` and the retirement of
`--on-transition-violation` for jobs are not: until they are, today's code runs
them in today's order, and the old tables keep validating inside the store's
verbs.

Instances. Each job row has one `job` instance on the state owner's executor
(`RuntimeState.machines`), under the row's `job_key`, with `JobData` from the
catalog and the row's units (`job_data`). The owner restores them from the rows
in `rebuild_machines`, which the constructor has it run
(`RuntimeState.keep_job_instances`) and an install and a witness run; a fork
copies them (`Executor.fork`, DL-339); a row read into existence gets its
instance as it is created. A move that code outside the chart makes leaves the
instance behind its row, so the owner brings each instance its input touched
back to its row at the input's commit, and each handler does so for its job
before it dispatches (`follow_job`). That refresh is also where `JobData`'s
release fields become the current run's. With that follow before the dispatch,
the chart decides each input from the row's projection: the harness proves the
rows, not the chart's own evolution, which becomes the contract as the families
convert and the follow and `Executor.realign` go.

STATUS. The handler checks the payload as today, builds the `Injection` from
the same predicates (the exit code's verdict only when no status is given),
dispatches it to the job's instance, and performs what the fired status row
dictates through today's methods: `_set_status` with the row's declared move
for job.117, job.118 and job.119, `_inject_inactive` with the row's move for
job.120 (a job) and job.135 (a box). The status row is the one of the status
region (`job.1NN`); any other is a `StatechartError` naming the rows fired.
Those methods then write the status with today's marker and cause, run the box
rules, settle the run's units and run the wakes, in today's order. The
`leave STARTING or RUNNING` that an idle status posts lands on the ignored
holding rows job.313 and job.314 and writes nothing.

Agreement. The code writes the status the chart reached, so for this family
the status agrees by construction, and the harness is what judges a wrong status
row. The check covers what the code still decides: the holding, which today's
settlement writes; the flags, where it shows only that today's write moves none,
or the arm a queued kill drops (the unconverted families' flag moves are
realigned before the dispatch, not checked); and the job type behind the
INACTIVE pair, which
`_inject_inactive` reads from the catalog (a box that fires job.120, or a job
that fires job.135, is a `StatechartError`). A move that leaves STARTING or
RUNNING has its flags compared when its status is written, before the box rules
run, and its holding when its own settlement has run (late, early in a restart
by DL-340, or first on the batch path). Any other move settles nothing, so its
holding is compared with the flags at the write, before the box rules can start
the job again. A later move of the same job by the cascade, which the chart does
not declare yet, is never taken for a difference. A difference is a `StatechartError` naming both
configurations. The constructor compares every row with its instance.

Starts (DL-342). Every attempt to start a job, a wake's, a box start's, a
retry's, an input's, is one `start` on the job's instance (`_attempt_start`);
STARTJOB, FORCE_STARTJOB and a TIMER's replay dispatch their own event, which
relays it after FORCE_STARTJOB's clears of ice and hold (job.177 to job.179,
job.203, job.206). The payload (`_Facts`) computes each fact on its first
read. The fired row is performed by today's methods: a refusal returns
today's reason to the caller that traces it, job.180 does nothing, job.212
arms (`_arm`), a postponed start or a skip goes through `_defer_start` or
`_window_skip_bypass`, and the bound (job.100), a bypass, a start on held
units, a queue and an admission go through `_start` and `_start_admitted`,
which keep the conversion of a cascade's RecursionError. Readmission
(`_readmit`) stays today's code. The attempt's first status write is compared
with the chart's configuration; the RUNNING after an admission dispatches
`started` and is compared; an attempt that moves no status is compared after
its own writes. The must_start and must_complete deadlines a STARTJOB arms
stay in `_dispatch`, before the attempt. The cascade still recurses, one
attempt inside the next, and is fitted by this class's own measured
`FRAMES_PER_NESTED_START` until the cascade slice removes the recursion.

Kills and timers (DL-343). `_dispatch` is overridden: STATUS, the start
inputs, KILLJOB and TIMER are handled here and the rest are inherited, so a
start input's attempt sits as deep on the stack as today's and passes its
relay (`relay`), never read from the cause text. KILLJOB is dispatched to the
job's instance: job.114 terminates a live job through `_terminate` (a box's
terminator cascade is today's code); job.217 or job.226 drops a waiter's arm
and job.111 terminates it (dequeue, KILL_DISARMS, JOB_KILL_QUEUED); job.137 to
job.140 ignore it with today's EVENT_IGNORED line. Each move is compared as a
STATUS move is (`_moving`). The TIMER input's deadline alarms move no state and
stay `_dispatch_timer_check`. `TIMER term_run_time` carries whether its run is
the row's (`Deadline`): job.115 terminates the running run, job.184 is the
stale deadline and writes nothing. A window timer reads its own facts first,
as today, and dispatches `TIMER run_window` with them real: a deferral that no
longer belongs to its box run, or a scan continuation that the job has moved
on from, is refused with today's text; a live continuation resumes the scan
(`_defer_to_next_opening`); any other replay is the attempt. The timers stay
armed by code: `_arm_term_run_time` in `_run`, the deadlines before a
STARTJOB's attempt, `_defer_start`'s enqueue.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from dsl41.capacity import DemandEntry, takes_machine_load, to_reservations
from dsl41.ir import CatalogIR, JobIR, Semantics
from dsl41.job_chart import (
    FORCE_STARTJOB,
    JOB,
    KILLJOB,
    LEAVE_LIVE,
    RUN_WINDOW,
    START,
    STARTED,
    STARTJOB,
    STATUS,
    TERM_RUN_TIME,
    BoxGate,
    Deadline,
    Injection,
    JobData,
    Release,
    StartVerdict,
    WindowSide,
    projection,
    row_configuration,
)
from dsl41.oracle import DEFERRED_RESCAN_KEY, DEFERRED_TIMER_KEY, MAX_NESTED_STARTS, Oracle
from dsl41.oracle_state import (
    BOX_START,
    BOX_STATUS_INACTIVE,
    FORCE_CLEARS_HOLD,
    FORCE_CLEARS_ICE,
    INJECTABLE_STATUSES,
    JOB_BYPASS,
    JOB_KILL,
    JOB_KILL_QUEUED,
    JOB_START,
    JOB_TERM_RUN_TIME,
    KILL_DISARMS,
    KILL_IGNORED,
    LIVE,
    START_REFUSED,
    STATUS_EXIT_FAILURE,
    STATUS_EXIT_SUCCESS,
    STATUS_INACTIVE,
    STATUS_INJECTED,
    CarriedRows,
    CascadeDepthError,
    Event,
    JobRuntime,
    JobStatus,
    OracleError,
    Transition,
)
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Outcome, Statechart, StatechartError

#: The status region's row ids (`job.1NN`), and the box's INACTIVE row.
_STATUS_REGION: Final = "job.1"
_BOX_INACTIVE: Final = "job.135"
#: The status rows a STATUS fires, each with the declared move of today's write:
#: `_set_status` for the first three, `_inject_inactive` for the INACTIVE pair.
_SET_STATUS: Final[dict[str, Transition[JobStatus]]] = {
    "job.117": STATUS_EXIT_SUCCESS,
    "job.118": STATUS_EXIT_FAILURE,
    "job.119": STATUS_INJECTED,
}
_INJECT_INACTIVE: Final[dict[str, Transition[JobStatus]]] = {
    "job.120": STATUS_INACTIVE,
    _BOX_INACTIVE: BOX_STATUS_INACTIVE,
}
_MOVES: Final = {**_SET_STATUS, **_INJECT_INACTIVE}

#: The row a `start` fires, as `start_verdict` names its outcome. job.180
#: (ignored) stands for every verdict that moves nothing: a gate, a postponed
#: start, a skip of a job already INACTIVE.
_START_ROWS: Final[dict[str, StartVerdict]] = {
    "job.100": "bounded",
    "job.101": "bypass",
    "job.102": "admit",
    "job.103": "queue",
    "job.104": "on_held",
    "job.106": "skip",
    "job.107": "skip_member",
    "job.124": "admit",
    "job.180": "ignored",
    "job.212": "arm",
    **{f"job.{n}": "refused" for n in range(141, 148)},
}
#: The verdicts whose row moves the status: the attempt's first status write
#: is compared with the chart's configuration.
_STATUS_MOVES: Final = frozenset({"bypass", "on_held", "queue", "admit", "skip", "skip_member"})
#: The admission rows and the move each writes into STARTING: the job type is
#: the row's, and a catalog that disagrees is a `StatechartError`.
_ADMIT_MOVES: Final[dict[str, Transition[JobStatus]]] = {
    "job.102": JOB_START,
    "job.124": BOX_START,
}
#: The frames the chart's selection adds once, at a cascade's innermost
#: attempt, over today's gates: measured 0 to 12, bounded here
#: (tests/test_cascade_depth.py). `RECURSION_LIMIT_BASE` absorbs them.
SELECTION_FRAMES: Final = 16
#: The skip of a job in a box, beside job.106's skip of a job in none.
_SKIP_MEMBER: Final = "job.107"
#: The verdicts that reach `_start_admitted`, which settles an ended run first (DL-340).
_ADMITTING: Final = frozenset({"bypass", "on_held", "queue", "admit"})
#: The relay rows of FORCE_STARTJOB that clear a flag before the attempt.
_CLEARS_ICE: Final = "job.203"
_CLEARS_HOLD: Final = "job.206"
#: KILLJOB's rows: a live job terminates; a waiter's arm row reports the
#: flag move and job.111 terminates the waiter; any other status ignores it.
#: Each is the whole of the rows an input fires, its settlement's aside.
_KILL_LIVE: Final = ("job.114",)
_KILL_WAITER: Final = frozenset({("job.217", "job.111"), ("job.226", "job.111")})
_KILL_IGNORED: Final = frozenset((f"job.{n}",) for n in range(137, 141))
#: term_run_time's rows: the run it was armed for is RUNNING, or not.
_TERM_RUN_TIME: Final = ("job.115",)
_TERM_STALE: Final = ("job.184",)


class _Facts:
    """A `start` payload: `Attempt`'s fields, each oracle fact computed on its
    first read and kept.

    `force`, `explicit`, `scheduled`, `stale`, `rescan` and `dated` are eager:
    who asks, the window timer's own facts (DL-343), which today's code
    reads before any gate, and an attribute of the job. `box`, `condition`,
    `window`, `bounded`, `box_noexec`, `admissible` and `demand` are lazy. The
    guards read them in `start_verdict`'s order, which is the order today's
    `_attempt_start`, `_start` and `_start_admitted` test them, so an attempt
    computes the facts the old oracle computes and no other: an iced job's
    condition is never evaluated, nor the window of a job whose condition is
    false. Every fact is a read: `_runtime` of the box, `_cond_true`,
    `_window_side`, `_noexec_bypasses`, the pool's `demand_vector` and
    `_admissible` write nothing to the store, the trace, the timers or the
    resolutions (`_window_side` fills the zone cache, as it does today)."""

    __slots__ = (
        "force",
        "explicit",
        "scheduled",
        "stale",
        "rescan",
        "dated",
        "_oracle",
        "_job",
        "_job_ir",
        "_known",
        "_probe",
    )

    def __init__(
        self,
        oracle: ChartOracle,
        job: str,
        job_ir: JobIR | None,
        *,
        force: bool = False,
        explicit: bool = False,
        scheduled: bool = False,
        stale: bool = False,
        rescan: bool = False,
        known: dict[str, Any] | None = None,
        probe: bool = False,
    ) -> None:
        self.force = force
        self.explicit = explicit
        self.scheduled = scheduled
        self.stale = stale
        self.rescan = rescan
        self.dated = job_ir is not None and job_ir.schedule is not None
        self._oracle = oracle
        self._job = job
        self._job_ir = job_ir
        self._known: dict[str, Any] = {} if known is None else known
        self._probe = probe

    def __repr__(self) -> str:
        known = ", ".join(f"{name}={value!r}" for name, value in self._known.items())
        return f"_Facts({self._job!r}, force={self.force}, explicit={self.explicit}, {known})"

    def relayed(self, *, force: bool) -> _Facts:
        """`Attempt.relayed`: the attempt a relay passes on."""
        return _Facts(
            self._oracle,
            self._job,
            self._job_ir,
            force=force,
            explicit=True,
            scheduled=True,
            stale=self.stale,
            rescan=self.rescan,
        )

    def assuming(self) -> _Facts:
        """This attempt, sharing its facts, with the two that today's code
        reads after a settlement (`_settle_ended_run`) taken as admitting:
        a read that says whether the attempt gets that far."""
        return _Facts(
            self._oracle,
            self._job,
            self._job_ir,
            force=self.force,
            explicit=self.explicit,
            scheduled=self.scheduled,
            stale=self.stale,
            rescan=self.rescan,
            known=self._known,
            probe=True,
        )

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    def _job_ir_known(self) -> JobIR:
        assert self._job_ir is not None, "a fact of a job outside the catalog"
        return self._job_ir

    @property
    def box(self) -> BoxGate:
        """The member's gate on its box's run, as `_attempt_start` reads it."""
        if "box" not in self._known:
            box = self._job_ir_known().box.box_name
            gate: BoxGate = "none"
            if box is not None:
                row = self._oracle._runtime(box)
                if row.status != "RUNNING":
                    gate = "idle"
                elif self._job in row.ran_members:
                    gate = "ran"
                else:
                    gate = "iced_out" if self._job in row.iced_out_members else "running"
            self._known["box"] = gate
        return cast(BoxGate, self._known["box"])

    @property
    def condition(self) -> bool:
        """The job's condition holds, or it has none (`_cond_true`)."""
        if "condition" not in self._known:
            gate = self._job_ir_known().sem.condition
            self._known["condition"] = gate is None or self._oracle._cond_true(gate.cond, self._job)
        return cast(bool, self._known["condition"])

    @property
    def window(self) -> WindowSide:
        """`_window_side`, which `_run_window_permits` calls."""
        if "window" not in self._known:
            side, next_open = self._oracle._window_side(self._job_ir_known())
            self._known["window"] = side
            self._known["next_open"] = next_open
        return cast(WindowSide, self._known["window"])

    @property
    def bounded(self) -> bool:
        """The job has `MAX_NESTED_STARTS` starts in progress (`_start`, DL-304)."""
        if "bounded" not in self._known:
            depth = self._oracle._starts_in_progress.get(self._job, 0)
            self._known["bounded"] = depth >= MAX_NESTED_STARTS
        return cast(bool, self._known["bounded"])

    @property
    def box_noexec(self) -> bool:
        """`_noexec_bypasses`. The guard reads it only for a job that is not a
        box and has no ON_NOEXEC of its own, where it is the boxes above."""
        if self._probe:
            return False
        if "box_noexec" not in self._known:
            job_ir = self._job_ir_known()
            self._known["box_noexec"] = self._oracle._noexec_bypasses(job_ir)
        return cast(bool, self._known["box_noexec"])

    @property
    def admissible(self) -> bool:
        """`_admissible` over the demand vector."""
        if self._probe:
            return True
        if "admissible" not in self._known:
            job_ir = self._job_ir_known()
            self._known["admissible"] = self._oracle._admissible(
                job_ir, self.vector(), force=self.force
            )
        return cast(bool, self._known["admissible"])

    @property
    def demand(self) -> bool:
        """The demand vector reserves units, as `reserve` and `take_over_held`
        read it (`to_reservations`)."""
        return bool(to_reservations(self.vector()))

    def vector(self) -> list[DemandEntry]:
        """The pool's `demand_vector`, which `_start_admitted` reads."""
        if "vector" not in self._known:
            self._known["vector"] = self._oracle._pool.demand_vector(self._job_ir_known())
        return cast(list[DemandEntry], self._known["vector"])


def job_data(oracle: Oracle, job: str, row: JobRuntime) -> JobData:
    """A job instance's data: its catalog entry's facts, and what leaving the
    run does with the units the row holds after a SUCCESS and after any other
    end, as `RuntimeState.release_reservations` decides it with the pool's
    `keeps_held`. A row that holds nothing frees nothing."""
    job_ir = oracle.catalog.jobs.get(job)

    def release(success: bool) -> Release:
        kept = spent = False
        for unit in row.reservations:
            if unit.release_policy == "completion" or (
                unit.release_policy == "success" and success
            ):
                continue
            if oracle._pool.keeps_held(unit.bucket):
                kept = True
            else:
                spent = True
        return "keep" if kept else "spend" if spent else "free"

    if job_ir is None:
        return JobData(
            catalog=False, release_on_success=release(True), release_otherwise=release(False)
        )
    return JobData(
        box=job_ir.job_type == "BOX",
        job_terminator=job_ir.box.job_terminator,
        auto_hold=job_ir.sem.auto_hold,
        release_on_success=release(True),
        release_otherwise=release(False),
    )


@dataclass
class _Agreement:
    """The chart's move for one input (a STATUS, a kill, a term_run_time) or
    one start, waiting for today's writes to compare against: the job, its
    instance's key, the event, the configuration the chart left, the move as
    its two statuses, and whether it leaves STARTING or RUNNING, so that its
    holding waits for its settlement. `written`: the first write is compared.
    `moves`: for a start, whether the chart's row moves the status, so that a
    status write must come."""

    job: str
    key: str
    event: ChartEvent
    active: frozenset[str]
    move: tuple[str, str]
    releasing: bool
    written: bool = False
    moves: bool = True


class ChartOracle(Oracle):
    """`Oracle` with the STATUS family (DL-341), the start attempt (DL-342),
    KILLJOB and the TIMER input (DL-343) on the `job` chart."""

    #: Measured as for the old oracle (tests/test_cascade_depth.py): 11 frames
    #: per nested start on the box chain, 22 per job on the ring, 6.5 on the
    #: running-box fold, 7 on the ON_NOEXEC chain, 2 per box on a kill's
    #: terminator chain, the same slopes as `Oracle`'s. The executor's
    #: selection adds 0 to 12 frames once, at the innermost attempt
    #: (`SELECTION_FRAMES` bounds it), and one at the base for a kill or a
    #: term_run_time (its handler) and for an input this class inherits
    #: (`super()._dispatch`), which the base absorbs: at most 13 in all.
    #: The budget keeps the old one's margin: 16.
    FRAMES_PER_NESTED_START = 16

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
        #: the STATUS move being written, between its dispatch and its settlement
        self._agreement: _Agreement | None = None
        #: a start's status move, between its dispatch and its first status write
        self._start_move: _Agreement | None = None
        self.store.keep_job_instances(self._job_instance)
        for job, row in self.store.job.items():
            active = self.store.machines.active(self.store.job_key(job))
            if active != row_configuration(row):
                raise StatechartError(
                    f"job {job!r}: the instance and the row differ at construction: the"
                    f" instance's configuration is {_listed(active)}, the row's"
                    f" {_listed(row_configuration(row))}"
                )

    def _job_instance(self, job: str, row: JobRuntime) -> tuple[Statechart, frozenset[str], object]:
        return JOB, row_configuration(row), job_data(self, job, row)

    def fork(self) -> ChartOracle:
        """`Oracle.fork`, with the twin's store asking the twin for its job
        instances' data."""
        twin = cast(ChartOracle, super().fork())
        twin.store.job_instance = twin._job_instance
        return twin

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
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        event = ChartEvent(STATUS, Injection(status, success))
        old = store.job[job].status
        fired = store.machines.dispatch(key, event)[0].rows
        row = next((r for r in fired if r.startswith(_STATUS_REGION)), "")
        if row not in _MOVES:
            raise self._unwritten(job, event, fired, "status row")
        with self._moving(job, key, event, old) as agreement:
            if row in _INJECT_INACTIVE:
                job_ir = self.catalog.jobs[job]
                if (row == _BOX_INACTIVE) != (job_ir.job_type == "BOX"):
                    raise self._differ(f"{row} fired for a {job_ir.job_type} job", agreement)
                self._inject_inactive(job_ir, code, _MOVES[row])
            else:
                new = cast(JobStatus, agreement.move[1])
                self._set_status(
                    job, _MOVES[row], cause="injected STATUS", exit_code=code, status=new
                )

    @contextmanager
    def _moving(self, job: str, key: str, event: ChartEvent, old: str) -> Iterator[_Agreement]:
        """The status move the chart just made for an input, from `old` to the
        configuration its instance is in, waiting for today's writes inside
        the block: its status and flags are compared at the job's first write,
        and its holding there too or, for a move that leaves STARTING or
        RUNNING, when its own settlement has run. The block runs in the
        caller's frame, so it adds no frame under the cascade."""
        active = self.store.machines.active(key)
        new = projection(active)[0]
        releasing = old in LIVE and new not in LIVE
        agreement = self._agreement = _Agreement(job, key, event, active, (old, new), releasing)
        try:
            yield agreement
            if self._agreement is not None:
                raise self._differ("the move's settlement never ran", self._agreement)
        finally:
            self._agreement = None

    def _unwritten(
        self, job: str, event: ChartEvent, fired: tuple[str, ...], what: str
    ) -> StatechartError:
        """The chart fired rows the oracle has no write for."""
        error = StatechartError(
            f"job {job!r}: {event.name} fired {', '.join(fired) or 'no row'}, and no {what}"
            " the oracle writes"
        )
        store = self.store
        return store.machines.explain(error, store.job_key(job), event)

    # ---- the inputs

    def _dispatch(self, ev: Event) -> None:
        """The inputs whose job rows the chart decides: STATUS (DL-341),
        STARTJOB, FORCE_STARTJOB and a window timer's replay (DL-342), KILLJOB
        and the TIMER input (DL-343). The others, SET_GLOBAL, the flag
        inputs and RELEASE_RESOURCE, are `Oracle._dispatch`'s. A start input
        makes its attempt here, as today's `_dispatch` does, so the cascade it
        begins sits as deep on the stack as today's; its relay is its own
        event, and a timer's is the window's relay (job.179)."""
        kind = ev.kind
        if kind == STATUS:
            self._handle_status(ev)
        elif kind == KILLJOB:
            self._handle_kill(self._required_job(ev))
        elif kind == "TIMER" and self._handle_timer(ev):
            return
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
            refused = self._attempt_start(
                job,
                force=kind == FORCE_STARTJOB,
                scheduled=True,
                cause=cause,
                relay=RUN_WINDOW if kind == "TIMER" else kind,
            )
            if refused is not None:
                self._ignore(job, START_REFUSED, "START_REFUSED", f"{refused} ({cause})")
        else:
            super()._dispatch(ev)

    def _handle_kill(self, job: str) -> None:
        """KILLJOB on the job's instance (DL-343), its fired row performed
        by today's writes: a live job terminates (job.114, `_terminate`, which
        still runs a box's terminator cascade); a waiter leaves the queue, its
        arm goes (job.217 or job.226) and it terminates (job.111); any other
        status ignores the kill with today's EVENT_IGNORED line (job.137 to
        job.140)."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        event = ChartEvent(KILLJOB)
        old = store.job[job].status
        fired = _fired(store.machines.dispatch(key, event))
        if fired == _KILL_LIVE:
            with self._moving(job, key, event, old):
                self._terminate(job, JOB_KILL, cause="KILLJOB")
        elif fired in _KILL_WAITER:
            with self._moving(job, key, event, old):
                # DL-50: a kill on a queued job dequeues it and terminates it;
                # Q3 (DL-54): it consumes a latched arm
                store.dequeue_waiter(job)
                store.move_flag(job, KILL_DISARMS)
                self._set_status(
                    job, JOB_KILL_QUEUED, cause="KILLJOB (dequeued from QUE_WAIT, DL-50)"
                )
        elif fired in _KILL_IGNORED:
            self._ignore(
                job,
                KILL_IGNORED,
                "EVENT_IGNORED",
                f"KILLJOB ignored: the job is {old}, not running or queued",
            )
        else:
            raise self._unwritten(job, event, fired, "kill row")

    def _handle_timer(self, ev: Event) -> bool:
        """The TIMER input (DL-343); False for a window timer's replay of
        a start, which `_dispatch` attempts through the window's relay. The
        deadline alarms move no state and stay today's code
        (`_dispatch_timer_check`). term_run_time is the chart's event. A
        window timer's own facts are read first, as today: whether the
        deferral still belongs to its box run (`_deferral_is_stale`) and, for
        a scan continuation, whether the scan is superseded
        (`_rescan_superseded`); a stale timer or a continuation is decided by
        its relay's `start` and performed here."""
        check = ev.payload.get("check")
        if check == "term_run_time":
            self._term_run_time(self._required_job(ev), ev.payload.get("run"))
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
        self._window_timer(job, deferred, stale, superseded=superseded, rescan=rescan)
        return True

    def _window_timer(
        self, job: str, deferred: str, stale: str | None, *, superseded: bool, rescan: bool
    ) -> None:
        """A window timer whose own facts decide it, through its relay's
        `start`: a refusal (141 to 147) with today's text, the stale box run's
        reason or the scan's "moved on", or a live continuation's resumed scan
        (job.180, `_defer_to_next_opening`). Nothing it writes moves the row."""
        job_ir = self.catalog.jobs.get(job)
        facts = _Facts(self, job, job_ir, stale=stale is not None or superseded, rescan=rescan)
        _relayed, start, (_status, active) = self._decide_start(job, facts, RUN_WINDOW)
        verdict = self._start_verdict(job, start)
        key = self.store.job_key(job)
        status = self.store.job[job].status
        agreement = _Agreement(job, key, start.event, active, (status, status), False, moves=False)
        if verdict == "refused":
            if stale is None:
                why = (
                    f"the job has moved on since the scan stopped -- no effect (DL-257; {deferred})"
                )
            else:
                why = f"{stale} (run_window-deferred {deferred})"
            self._ignore(job, START_REFUSED, "START_REFUSED", why)
        elif verdict == "ignored" and rescan:
            self._defer_to_next_opening(cast(JobIR, job_ir), deferred)
        else:
            raise self._differ(
                f"a stale or rescanning window timer's start is {verdict}", agreement
            )
        self._agree_after(agreement)

    def _term_run_time(self, job: str, run: object) -> None:
        """`TIMER term_run_time` (dossier ss5): the run it was armed for is
        RUNNING, and it terminates (job.115, `_terminate`); else the deadline
        is an earlier run's, or the run has ended, and nothing moves (job.184)."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        row = store.job[job]
        event = ChartEvent(TERM_RUN_TIME, Deadline(current=run == row.run_number))
        fired = _fired(store.machines.dispatch(key, event))
        if fired == _TERM_RUN_TIME:
            with self._moving(job, key, event, row.status):
                self._terminate(
                    job, JOB_TERM_RUN_TIME, cause="term_run_time exceeded (dossier ss5)"
                )
        elif fired != _TERM_STALE:
            raise self._unwritten(job, event, fired, "term_run_time row")

    def _record(self, job: str, transition: str, cause: str) -> None:
        """Every status write is followed at once by its trace line, before
        the box rules or the settlement run, so the first write is compared
        here. A start's writes are compared here too, and the RUNNING that
        follows an admitted start dispatches `started` first."""
        self._compare_written(job)
        if self._start_move is not None or transition == "STARTING->RUNNING":
            self._compare_start_write(job, transition)
        super()._record(job, transition, cause)

    # ---- the start family

    def _attempt_start(
        self,
        job: str,
        *,
        force: bool,
        scheduled: bool,
        cause: str,
        relay: str | None = None,
    ) -> str | None:
        """Every attempt to start a job, whatever its cause, is one `start` on
        the job's instance; STARTJOB, FORCE_STARTJOB and a TIMER's replay
        dispatch their own event, `relay`, which `_dispatch` passes, and the
        chart relays it (job.177 to job.179) after the forced clears; any
        other caller posts `start` itself. The chart decides; today's methods
        write what the fired row says, in today's order, and the refusal
        reason is returned to the caller that traces it, as today."""
        job_ir = self.catalog.jobs.get(job)
        rt = self._runtime(job) if job_ir is not None else None
        if rt is None and job not in self.store.job:
            return None  # no row, no instance: the chart ignores a job outside the catalog
        facts = _Facts(self, job, job_ir)
        try:
            relayed, start, (status, active) = self._decide_start(job, facts, relay)
        except RecursionError as exc:
            if self._starts_in_progress:
                raise
            # the fit fell short for this catalog, whatever recursed (the
            # cascade, or a fact's own recursion such as a deep condition):
            # the chart's selection is deeper than today's gates, so the limit
            # can be met there before any start, and this attempt is named
            # as the cascade's first start
            raise CascadeDepthError(job, cause, sys.getrecursionlimit()) from exc
        verdict = self._start_verdict(job, start)
        attempt = cast(_Facts, start.event.payload)
        side = attempt.known("window")
        outside = side not in (None, "inside")
        if rt is None or verdict == "ignored" and not relayed and not outside:
            return None
        key = self.store.job_key(job)
        moves = verdict in _STATUS_MOVES
        agreement = _Agreement(
            job, key, start.event, active, (rt.status, status), False, moves=moves
        )
        row = start.rows[0]
        job_ir = cast(JobIR, job_ir)
        if row in _ADMIT_MOVES and (row == "job.124") != (job_ir.job_type == "BOX"):
            raise self._differ(f"{row} fired for a {job_ir.job_type} job", agreement)
        box = job_ir.box.box_name
        if verdict in ("skip", "skip_member") and (row == _SKIP_MEMBER) != (box is not None):
            raise self._differ(f"{row} fired for a job in box {box!r}", agreement)
        if self._start_move is not None:
            if self._start_move.moves:
                raise self._differ("a start began before the last one's status write", agreement)
            self._start_move = None  # a window's writes are done once the cascade attempts
        try:
            if _CLEARS_ICE in relayed:
                self.store.move_flag(job, FORCE_CLEARS_ICE)
                self._record(
                    job, "OFF_ICE", f"FORCE_STARTJOB clears ON_ICE (SEM-23, DL-243; {cause})"
                )
            if _CLEARS_HOLD in relayed:
                self.store.move_flag(job, FORCE_CLEARS_HOLD)
                self._record(
                    job, "OFF_HOLD", f"FORCE_STARTJOB clears ON_HOLD (SEM-23, DL-243; {cause})"
                )
            if verdict == "refused":
                return self._refusal(job, rt, attempt)
            if verdict == "arm":
                why = "blocked ON_HOLD" if rt.on_hold else "condition false"
                self._arm(job_ir, rt, scheduled, why)
            if not moves:
                self._agree_after(agreement)
            if moves or outside:
                self._start_move = agreement
            if outside:
                self._outside_window(
                    job_ir, cause, cast(WindowSide, side), attempt.known("next_open")
                )
            elif verdict in _ADMITTING or verdict == "bounded":
                self._start(
                    job,
                    cause,
                    force=force,
                    verdict=verdict,
                    vector=attempt.known("vector"),
                    move=_ADMIT_MOVES.get(row),
                )
        except BaseException:
            if self._start_move is agreement:
                self._start_move = None
            raise
        if self._start_move is agreement:
            self._start_move = None
            if moves:
                raise self._differ("the start's status write never ran", agreement)
        return None

    def _decide_start(
        self, job: str, facts: _Facts, relay: str | None
    ) -> tuple[tuple[str, ...], Outcome, tuple[str, frozenset[str]]]:
        """Dispatch the attempt to the job's instance, brought to its row
        first, and return the relay's rows, the `start` item's outcome and the
        status and configuration the chart left.

        DL-340: a start that reaches `_start_admitted` settles an ended run
        whose settlement the box rules still hold back, and the admission is
        read after that. When such a settlement is owed, a read of the same
        attempt (`Executor.resolve`, the facts after the settlement taken as
        admitting) says whether the attempt gets that far; if so the run
        settles where today's code settles it, and the dispatch then reads the
        admission it leaves."""
        store = self.store
        machines = store.machines
        key = store.job_key(job)
        store.follow_job(job)
        owed = self._settlement_owed(job)
        # no settlement is owed at an input's own attempt: the box rules that
        # hold one back run inside the cascade, never between inputs
        assert relay is None or not owed, "a settlement owed at an input's own attempt"
        if owed:
            read = machines.resolve(key, ChartEvent(START, facts.assuming()))
            if _START_ROWS.get(read.rows[0] if read.rows else "") in _ADMITTING:
                self._settle_ended_run(job)
                store.follow_job(job)
        outcomes = machines.dispatch(key, ChartEvent(relay or START, facts))
        start = next(o for o in outcomes if o.event.name == START)
        relayed = outcomes[0].rows if relay is not None else ()
        active = machines.active(key)
        return relayed, start, (projection(active)[0], active)

    def _start_verdict(self, job: str, start: Outcome) -> StartVerdict:
        """The verdict the `start` item's row stands for."""
        verdict = _START_ROWS.get(start.rows[0]) if len(start.rows) == 1 else None
        if verdict is None:
            error = StatechartError(
                f"job {job!r}: start fired {', '.join(start.rows) or 'no row'}, and no start"
                " row the oracle writes"
            )
            raise self.store.machines.explain(error, self.store.job_key(job), start.event)
        return verdict

    def _settlement_owed(self, job: str) -> bool:
        """A run of the job ended inside a cascade still running its box rules,
        and its settlement waits (`_settle_ended_run`, DL-340)."""
        run = self.store.job[job].run_number
        return any(x.run == run and not x.done for x in self._late_settlements.get(job, ()))

    def _refusal(self, job: str, rt: JobRuntime, attempt: _Facts) -> str:
        """Today's START_REFUSED reason for a refused attempt: the job is live
        or queued, or its box gate refuses it (SEM-10, SEM-20)."""
        if rt.status in ("STARTING", "RUNNING", "QUE_WAIT"):
            return f"already {rt.status} -- concurrent or repeated start request, no effect"
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

    def _outside_window(
        self, job_ir: JobIR, cause: str, side: WindowSide, next_open: datetime | None
    ) -> None:
        """The writes of `_run_window_permits` outside the window: a postponed
        start, or a skip and its bypass (SEM-33)."""
        if side == "defer":
            assert next_open is not None
            self._defer_start(job_ir, next_open, cause)
            return
        self._record(
            job_ir.name,
            "RUN_WINDOW_SKIP",
            f"outside run_window; closer to previous close -- not run ({cause})",
        )
        self._window_skip_bypass(job_ir)

    def _start(
        self,
        job: str,
        cause: str,
        *,
        force: bool = False,
        verdict: StartVerdict = "admit",
        vector: list[DemandEntry] | None = None,
        move: Transition[JobStatus] | None = None,
    ) -> None:
        """`Oracle._start` with the bound decided by the chart (job.100): the
        refusal is written here, and the RecursionError of a cascade past the
        recursion limit is still converted at its outermost start."""
        if verdict == "bounded":
            self._ignore(
                job,
                START_REFUSED,
                "START_REFUSED",
                f"re-trigger loop inside one instant: started twice in one nested cascade;"
                f" refused (L010; {cause})",
            )
            return
        depth = self._starts_in_progress.get(job, 0)
        outermost = not self._starts_in_progress
        self._starts_in_progress[job] = depth + 1
        try:
            self._start_admitted(job, cause, force=force, verdict=verdict, vector=vector, move=move)
        except RecursionError as exc:
            if not outermost:
                raise
            raise CascadeDepthError(job, cause, sys.getrecursionlimit()) from exc
        finally:
            if depth:
                self._starts_in_progress[job] = depth
            else:
                del self._starts_in_progress[job]

    def _start_admitted(
        self,
        job: str,
        cause: str,
        *,
        force: bool,
        verdict: StartVerdict = "admit",
        vector: list[DemandEntry] | None = None,
        move: Transition[JobStatus] | None = None,
    ) -> None:
        """`Oracle._start_admitted` writing the chart's verdict: the ended
        run's settlement first (DL-340), then a bypass (job.101), a start on
        held units (job.104), a queue (job.103) or an admission (job.102,
        job.124) through today's methods. `force` is unread: the chart decided
        it; the parameter keeps the old method's signature for its callers."""
        job_ir = self.catalog.jobs[job]
        self._settle_ended_run(job)
        if verdict == "bypass":
            self.store.start_run(
                job,
                cause=f"ON_NOEXEC bypass ({cause})",
                box=job_ir.box.box_name,
                is_box=False,
            )
            self._set_status(job, JOB_BYPASS, cause=f"ON_NOEXEC bypass ({cause})")
            return
        if vector is None:
            vector = self._pool.demand_vector(job_ir)
        if verdict == "on_held":
            self._start_on_held(job_ir, vector, cause)
        elif verdict == "queue":
            self._enqueue_waiter(job, cause)
        else:
            freed = self._acquire(job, vector)
            assert move is not None, "an admission without its row's move"
            self._run(job_ir, move, cause, had_demand=bool(vector))
            if freed:
                self._wake_waiters()
        if takes_machine_load(vector):
            self._scan_owed = True

    def _agree_after(self, agreement: _Agreement) -> None:
        """An attempt whose row moves no status: after its own writes (the
        forced clears, the arm), the row is the chart's configuration."""
        if self._row_active(agreement.job) != projection(agreement.active):
            raise self._differ("the start's writes differ from the chart's", agreement)

    def _compare_start_write(self, job: str, transition: str) -> None:
        """A start's first status write is the chart's move; a RUNNING after an
        admitted start is the chart's `started` (job.105, job.125), dispatched
        to the instance the admission left in STARTING. A RUNNING that no
        admission of the chart precedes (a readmission, an injected status) is
        not the start family's. A window's skip that the chart says moves
        nothing (job.180) is watched the same way until its writes are done:
        a status write there differs from the chart's configuration."""
        agreement = self._start_move
        if agreement is not None and agreement.job == job and "->" in transition:
            self._start_move = None
            if self._row_active(job) != projection(agreement.active):
                raise self._differ("the start's status write differs", agreement)
            return
        if transition != "STARTING->RUNNING":
            return
        machines = self.store.machines
        key = self.store.job_key(job)
        if "STARTING" not in machines.active(key):
            return
        event = ChartEvent(STARTED)
        fired = machines.dispatch(key, event)[0].rows
        started = _Agreement(job, key, event, machines.active(key), ("STARTING", "RUNNING"), False)
        job_type = self.catalog.jobs[job].job_type
        if fired != (("job.125",) if job_type == "BOX" else ("job.105",)):
            raise self._differ(
                f"started fired {', '.join(fired) or 'no row'} for a {job_type} job", started
            )
        if self._row_active(job) != projection(started.active):
            raise self._differ("the RUNNING write differs", started)

    def _settle_row(self, job: str, old: str, new: str, *, run: int | None = None) -> bool:
        released = super()._settle_row(job, old, new, run=run)
        agreement = self._agreement
        if agreement is not None and agreement.job == job and agreement.move == (old, new):
            self._agreement = None
            if self._row_active(job)[-1] != projection(agreement.active)[-1]:
                raise self._differ("the holding differs after the settlement", agreement)
        return released

    def _compare_written(self, job: str) -> None:
        """At the first write of the job after the dispatch, the status
        move's: its status and flags are the chart's. A move that does not
        leave STARTING or RUNNING settles nothing, so its holding is compared
        here too, and its agreement closes: the box rules that follow may start
        the job again and take units for the new run."""
        agreement = self._agreement
        if agreement is None or agreement.job != job or agreement.written:
            return
        agreement.written = True
        width, what = 5, "the status and the flags differ"
        if not agreement.releasing:
            self._agreement = None
            width, what = 6, "the configuration differs"
        if self._row_active(job)[:width] != projection(agreement.active)[:width]:
            raise self._differ(f"{what} as written", agreement)

    def _row_active(self, job: str) -> tuple[str, bool, bool, bool, bool, str]:
        return projection(row_configuration(self.store.job[job]))

    def _differ(self, what: str, agreement: _Agreement) -> StatechartError:
        row = row_configuration(self.store.job[agreement.job])
        error = StatechartError(
            f"job {agreement.job!r}: {what}: the chart's configuration is"
            f" {_listed(agreement.active)}, the row's {_listed(row)}"
        )
        return self.store.machines.explain(error, agreement.key, agreement.event)


def _fired(outcomes: list[Outcome]) -> tuple[str, ...]:
    """Every row an input fired on its instance, in order, but the settlement
    of the run it ends (`leave STARTING or RUNNING`), which `_moving` compares
    as the holding. A row beyond the ones a handler performs is named in its
    error."""
    return tuple(r for o in outcomes if o.event.name != LEAVE_LIVE for r in o.rows)


def _listed(active: frozenset[str]) -> str:
    return ", ".join(sorted(active))
