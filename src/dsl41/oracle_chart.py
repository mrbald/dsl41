"""The second oracle: `Oracle` with its job rows driven by the `job` statechart
(docs/statechart-engine.md §16, DL-341).

`ChartOracle` is built beside the old oracle and converted one input family at
a time, under the old-versus-new harness (tests/oracle_lockstep.py, DL-338),
which names it as `dsl41.oracle_chart:ChartOracle`. It overrides the handlers
of the families converted so far and inherits every other one, so `oracle.py`
stays the twin it is compared with. The STATUS family, the start attempt
(DL-342), KILLJOB and the TIMER input (DL-343), the flag inputs (DL-344),
RELEASE_RESOURCE and the readmission out of the queue (DL-345), the box
rules and the period opening's release (DL-346, `CascadeOracle` in
`oracle_chart_cascade`), and a box's ON_NOEXEC on the jobs it contains
(DL-347) are converted. SET_GLOBAL, the cascade's order (the
recursion, with the wakes of referencers), the registration of `job` in
`machines.MACHINES` and the retirement of `--on-transition-violation` for
jobs are not: until they are, today's code runs them in today's order, and
the old tables keep validating inside the store's verbs.

Instances. Each job row has one `job` instance on the state owner's executor
(`RuntimeState.machines`), under the row's `job_key`, with `JobData` from the
catalog (`job_data`). The owner restores them from the rows
in `rebuild_machines`, which the constructor has it run
(`RuntimeState.keep_job_instances`) and an install and a witness run; a fork
copies them (`Executor.fork`, DL-339); a row read into existence gets its
instance as it is created. A move that code outside the chart makes leaves the
instance behind its row, so the owner brings each instance its input touched
back to its row at the input's commit, and each handler does so for its job
before it dispatches (`follow_job`). With that follow before the dispatch,
the chart decides each input from the row's projection: the harness proves the
rows, not the chart's own evolution, which becomes the contract as the families
convert and the follow and `Executor.realign` go. Each payload that can end a
run carries a handle on the job's units (`RowUnits`): the settlement its row
sends reads its policy from the row when it pops (DL-349).

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

Agreement. Each converted family records the chart's move and has today's
writes compared with it (`AgreeingOracle`, in `oracle_chart_agreement`).

Starts (DL-342). Every attempt to start a job, a wake's, a box start's, a
retry's, an input's, is one `start` on the job's instance (`_attempt_start`);
STARTJOB, FORCE_STARTJOB and a TIMER's replay dispatch their own event, which
relays it after FORCE_STARTJOB's clears of ice and hold (job.177 to job.179,
job.203, job.206). The payload (`StartFacts`) computes each fact on its first
read (`oracle_chart_facts`). The fired row is performed by today's methods: a
refusal returns today's reason to the caller that traces it, job.180 does
nothing, job.212 arms (`_arm`), a postponed start or a skip goes through
`_defer_start` or `_window_skip_bypass`, and the bound (job.100), a bypass, a
start on held units, a queue and an admission go through `_start` and
`_start_admitted`, which keep the conversion of a cascade's RecursionError.
An attempt whose run's settlement is owed takes job.401: the run settles
(`_settle_ended_run`), and the attempt is dispatched again (DL-340,
DL-349). An admitting row sends `start admitted` first; its row
(job.402) sends the admission's moves, as its descendants.
The attempt's first status write is compared with the chart's configuration;
the RUNNING after an admission dispatches `started` and is compared; an
attempt that moves no status is compared after its own writes. The
must_start and must_complete deadlines a STARTJOB arms stay in `_dispatch`,
before the attempt. The cascade still recurses, one attempt inside the next,
and is fitted by this class's own measured `FRAMES_PER_NESTED_START` until
the cascade becomes the executor's worklist and the recursion goes.

Kills and timers (DL-343). `_dispatch` is overridden: STATUS, the start
inputs, KILLJOB and TIMER are handled here and the rest are inherited, so a
start input's attempt sits as deep on the stack as today's and passes its
relay (`relay`), never read from the cause text. KILLJOB is dispatched to the
job's instance: job.114 terminates a live job through `_terminate` (a box's
terminator cascade is the chart's `box ends FAILURE or TERMINATED`,
DL-346); job.217 or job.226 drops a waiter's arm and job.111 terminates
it (dequeue, KILL_DISARMS, JOB_KILL_QUEUED); job.137 to job.140 ignore it with
today's EVENT_IGNORED line. Each move is compared as a
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

Flags (DL-344). ON_ICE, OFF_ICE, ON_HOLD, OFF_HOLD, ON_NOEXEC, OFF_NOEXEC
and DISARM are dispatched to the job's instance by `_handle_oob`, at today's
depth. The chart decides, and its rows choose each dimension today's code
read from the row or the catalog. Whether the flag was on (a second ice
resolves nothing; DISARM's "no latch") is the row's source state, which the
follow before the dispatch makes the row's; a wrong flag target is caught by
the compare after the flag's write. Three dimensions a guard decides are
checked against the row or the catalog before any write, each a
`StatechartError`: whether ON_ICE meets a waiter, the job type of ON_NOEXEC's
settlement, and its status (a job settles out of the queue or from FAILURE or
TERMINATED, else moves nothing; a box that is not INACTIVE settles; whether an
INACTIVE box does is the `change` fact, left to the harness). An ignored input (job.148 to job.158)
writes today's EVENT_IGNORED line with `_oob_ignored`'s text. The flag row is
`move_flag` with today's marker and line, and the row is compared with the
chart's configuration after it. A status the `flag moved` fires is written by
today's methods and compared as a STATUS move is: an iced waiter leaves the
queue (job.112, `_set_status`); ON_NOEXEC settles a waiter or a FAILED job
(job.113, job.121) or a box (job.136, job.169, job.170) through
`_inject_inactive`. ON_NOEXEC's payload (`NoexecFacts`) reads the box's tree
only where today's rule does. The moves the chart does not declare stay
today's code, in today's order: the wakes of referencers after an ice (its
box rules are the chart's, DL-346), the mark of a member taken off ice
in its running box, a waiter's wake on a hold, OFF_HOLD's start retry and
ON_NOEXEC's (each one `start`, DL-342). A box's ON_NOEXEC flags the jobs it
contains and takes their hold off through the chart's `ON_NOEXEC on its box`
(DL-347), and a box's OFF_NOEXEC clears them through `OFF_NOEXEC on its
box` (DL-346).

The queue (DL-345). RELEASE_RESOURCE is dispatched to the job's
instance: held units go back to the pool with today's line and the waiters
wake (job.307); a job holding nothing (job.315), or a live run whose units go
back when it ends (job.316), takes today's no-effect line. Whether the job
holds units it can give back is the row's, checked before any write. Each
waiter's readmission is one `readmit` on its instance, with a payload
(`ReadmitFacts`) that computes each fact where today's scan reads it. The
scan and its order stay code (`_wake_waiters`, the rank, then the queue's
order): the chart has one instance per job, and the order is the store's.
The fired rows are written by today's methods: an admission (job.108)
dequeues, `_acquire` reserves or takes over held units, and `_run` starts
the job, its STARTING and RUNNING compared as a start's; a member of a box
that stopped running is cancelled (job.109, `_cancel_waiter`); a waiter that
fails the queued recheck drops its arm (job.216; job.230 has none, the row's
choice, checked before any write) and its `flag moved` leaves the queue
(job.110, `_leave_queue_unstarted`, whose window or day disposition stays
code); any other stays queued (job.181). A cancel and a leave are compared as
a STATUS move is. ON_NOEXEC's retry (`_retry_released`) is one `start`, as
before.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime
from typing import Final, cast

from dsl41.capacity import DemandEntry, takes_machine_load
from dsl41.ir import CatalogIR, JobIR, Semantics
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
    READMIT,
    RELEASE_RESOURCE,
    RUN_WINDOW,
    START,
    START_ADMITTED,
    STARTJOB,
    STATUS,
    TERM_RUN_TIME,
    Deadline,
    Injection,
    Payload,
    StartVerdict,
    WindowSide,
    projection,
    row_configuration,
)
from dsl41.oracle import DEFERRED_RESCAN_KEY, DEFERRED_TIMER_KEY
from dsl41.oracle_chart_agreement import Agreement, listed
from dsl41.oracle_chart_cascade import CascadeOracle
from dsl41.oracle_chart_facts import NoexecFacts, ReadmitFacts, RowUnits, StartFacts, job_data
from dsl41.oracle_state import (
    BOX_NOEXEC,
    BOX_START,
    BOX_STATUS_INACTIVE,
    FAILED,
    FORCE_CLEARS_HOLD,
    FORCE_CLEARS_ICE,
    HELD_RELEASED,
    HOLD_IGNORED,
    HOLD_OFF,
    HOLD_ON,
    ICE_IGNORED,
    ICE_OFF,
    ICE_ON,
    INJECTABLE_STATUSES,
    JOB_BYPASS,
    JOB_ICE_QUEUED,
    JOB_KILL,
    JOB_KILL_QUEUED,
    JOB_NOEXEC_QUEUED,
    JOB_READMIT,
    JOB_START,
    JOB_TERM_RUN_TIME,
    KILL_DISARMS,
    KILL_IGNORED,
    LIVE,
    NOEXEC_CLEARS_HOLD,
    NOEXEC_COMPLETED,
    NOEXEC_IGNORED,
    NOEXEC_OFF,
    NOEXEC_ON,
    QUEUE_LEFT_DISARMS,
    START_REFUSED,
    STATUS_EXIT_FAILURE,
    STATUS_EXIT_SUCCESS,
    STATUS_INACTIVE,
    STATUS_INJECTED,
    CarriedRows,
    CascadeDepthError,
    Event,
    EventKind,
    FlagState,
    JobRuntime,
    JobStatus,
    OracleError,
    Transition,
)
from dsl41.oracle_state import DISARM as DISARM_MOVE
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
#: attempt or box rule, over today's gates: measured 7 to 12, bounded here
#: (tests/test_cascade_depth.py). `RECURSION_LIMIT_BASE` absorbs them.
SELECTION_FRAMES: Final = 16
#: The skip of a job in a box, beside job.106's skip of a job in none.
_SKIP_MEMBER: Final = "job.107"
#: The verdicts that reach `_start_admitted`; each admitting row sends `start
#: admitted` first (job.402), and the admission's moves follow from it.
_ADMITTING: Final = frozenset({"bypass", "on_held", "queue", "admit"})
_EXTENT: Final = "job.402"
#: An attempt past the bound whose run's settlement is owed (DL-340): the
#: oracle settles the run and dispatches the attempt again.
_SETTLE_FIRST: Final = "job.401"
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
#: The status rows ON_NOEXEC's `flag moved` fires: a job's settlement, out of
#: the queue (job.113) or from FAILURE or TERMINATED (job.121); a box's
#: INACTIVE from an idle status, the queue or STARTING; job.198, no move.
_NOEXEC_JOB: Final = frozenset({"job.113", "job.121"})
_NOEXEC_BOX: Final = frozenset({"job.136", "job.169", "job.170"})
_NO_STATUS_MOVE: Final = "job.198"
#: The flag inputs' rows on the job's instance, as `fired` lists them, and
#: what each comes to: `ignored`, the status region ignores the input for the
#: job (job.148 to job.158); `moved`, the flag changes value; `kept`, it had
#: the value already; `queued`, ON_ICE's waiter leaves the queue (job.112).
#: ON_NOEXEC's flag rows (the hold it supersedes, job.207, then its own,
#: job.209 or job.222) come to the status row their `flag moved` fires.
_FLAG_FIRED: Final[dict[str, dict[tuple[str, ...], str]]] = {
    ON_ICE: {
        ("job.148",): "ignored",
        ("job.149",): "ignored",
        ("job.201", "job.112"): "queued",
        ("job.201", _NO_STATUS_MOVE): "moved",
        ("job.218",): "kept",
    },
    OFF_ICE: {("job.202",): "moved", ("job.219",): "kept"},
    ON_HOLD: {
        ("job.150",): "ignored",
        ("job.151",): "ignored",
        ("job.204",): "moved",
        ("job.220",): "kept",
    },
    OFF_HOLD: {("job.205",): "moved", ("job.221",): "kept"},
    ON_NOEXEC: {
        **{(f"job.{n}",): "ignored" for n in range(152, 159)},
        **{
            (*hold, flag, settles): settles
            for hold in ((), ("job.207",))
            for flag in ("job.209", "job.222")
            for settles in (*sorted(_NOEXEC_JOB | _NOEXEC_BOX), _NO_STATUS_MOVE)
        },
    },
    OFF_NOEXEC: {("job.210",): "moved", ("job.223",): "kept"},
    DISARM: {("job.214",): "moved", ("job.225",): "kept"},
}
#: The flag move each input writes, its marker being the input's name.
_FLAG_MOVES: Final[dict[str, Transition[FlagState]]] = {
    ON_ICE: ICE_ON,
    OFF_ICE: ICE_OFF,
    ON_HOLD: HOLD_ON,
    OFF_HOLD: HOLD_OFF,
    OFF_NOEXEC: NOEXEC_OFF,
    DISARM: DISARM_MOVE,
}
#: The internal rows an ignored flag input takes in today's table.
_FLAG_IGNORED: Final[dict[str, Mapping[JobStatus, Transition[JobStatus]]]] = {
    ON_ICE: ICE_IGNORED,
    ON_HOLD: HOLD_IGNORED,
    ON_NOEXEC: NOEXEC_IGNORED,
}
#: RELEASE_RESOURCE's rows: held units go back (job.307); the job holds
#: nothing (job.315), or its units are a live run's (job.316).
_RELEASED: Final = ("job.307",)
_RELEASE_IGNORED: Final = frozenset({("job.315",), ("job.316",)})
#: A readmission's rows on the waiter's instance, as `fired` lists them, and
#: what each comes to: `admit`, the status row (job.108), then the admitted
#: start's holding row (job.301 reserves, job.302 takes held units over,
#: job.311 reserves nothing) and the arm's (job.213 consumes it, job.224 finds
#: none); `cancel`, a member of a stopped box (job.109); `leave`, the arm row
#: (job.216 drops it, job.230 finds none) and the `flag moved` it reports
#: (job.110); `keep`, the waiter stays queued (job.181).
_READMIT_FIRED: Final[dict[tuple[str, ...], str]] = {
    **{
        ("job.108", holding, arm): "admit"
        for holding in ("job.301", "job.302", "job.311")
        for arm in ("job.213", "job.224")
    },
    ("job.109",): "cancel",
    ("job.216", "job.110"): "leave",
    ("job.230", "job.110"): "leave",
    ("job.181",): "keep",
}
#: The arm row of a waiter that leaves the queue with an arm to drop.
_LEAVE_ARMED: Final = "job.216"


class ChartOracle(CascadeOracle):
    """`Oracle` with the STATUS family (DL-341), the start attempt (DL-342),
    KILLJOB and the TIMER input (DL-343), the flag inputs (DL-344),
    RELEASE_RESOURCE and the readmission (DL-345), and the box rules and the
    period opening (DL-346, DL-347, `CascadeOracle`) on the `job` chart."""

    #: Measured as for the old oracle (tests/test_cascade_depth.py): 11 frames
    #: per nested start on the box chain, 22 per job on the ring, 6.5 on the
    #: running-box fold, 7 on the ON_NOEXEC chain, 2 per box on a kill's
    #: terminator chain, the same slopes as `Oracle`'s. The executor's
    #: selection adds 7 to 12 frames once, at the innermost attempt or box
    #: rule (`SELECTION_FRAMES` bounds it), and one at the base for a kill or
    #: a term_run_time (its handler) and for an input this class inherits
    #: (`super()._dispatch`), which the base absorbs. A flag input's retry
    #: sits at today's depth (`_handle_oob`), and so do RELEASE_RESOURCE, the
    #: readmission and each box rule (`CascadeOracle` replaces today's
    #: methods in place, the fold under the door as today's). The budget keeps
    #: the old one's margin: 16.
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
        event = ChartEvent(STATUS, Injection(status, success, units=RowUnits(self, job)))
        old = store.job[job].status
        fired = self._expect_holding(store.machines.dispatch(key, event))[0].rows
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

    # ---- the inputs

    def _dispatch(self, ev: Event) -> None:
        """The inputs whose job rows the chart decides: STATUS (DL-341),
        STARTJOB, FORCE_STARTJOB and a window timer's replay (DL-342), KILLJOB
        and the TIMER input (DL-343), the flag inputs (DL-344) and
        RELEASE_RESOURCE (DL-345). SET_GLOBAL is `Oracle._dispatch`'s. A
        start input makes its attempt here, as today's `_dispatch` does, so the
        cascade it begins sits as deep on the stack as today's; its relay is its
        own event, and a timer's is the window's relay (job.179)."""
        kind = ev.kind
        if kind == STATUS:
            self._handle_status(ev)
        elif kind == KILLJOB:
            self._handle_kill(self._required_job(ev))
        elif kind == RELEASE_RESOURCE:
            self._release_resource(self._required_job(ev))
        elif kind == "TIMER" and self._handle_timer(ev):
            return
        elif kind in _FLAG_FIRED:
            self._handle_oob(kind, self._required_job(ev))
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

    def _drain(self) -> None:
        """`Oracle._drain` at its depth, its scans included, inside `_owing`."""
        with self._owing():
            while self._queue:
                self._dispatch(self._queue.popleft())
                self._run_owed_scan()

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
        event = ChartEvent(KILLJOB, Payload(units=RowUnits(self, job)))
        old = store.job[job].status
        fired = self._fired_rows(store.machines.dispatch(key, event))
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
        facts = StartFacts(self, job, job_ir, stale=stale is not None or superseded, rescan=rescan)
        _relayed, start, after, (_status, active) = self._decide_start(job, facts, RUN_WINDOW)
        verdict = self._start_verdict(job, start, after)
        key = self.store.job_key(job)
        status = self.store.job[job].status
        agreement = Agreement(job, key, start.event, active, (status, status), False, moves=False)
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
        deadline = Deadline(current=run == row.run_number, units=RowUnits(self, job))
        event = ChartEvent(TERM_RUN_TIME, deadline)
        fired = self._fired_rows(store.machines.dispatch(key, event))
        if fired == _TERM_RUN_TIME:
            with self._moving(job, key, event, row.status):
                self._terminate(
                    job, JOB_TERM_RUN_TIME, cause="term_run_time exceeded (dossier ss5)"
                )
        elif fired != _TERM_STALE:
            raise self._unwritten(job, event, fired, "term_run_time row")

    # ---- the flag family

    def _handle_oob(self, kind: EventKind, job: str) -> None:
        """The operator's flag inputs on the job's instance (DL-344), each
        fired row performed by today's writes. An input the job ignores fires
        the status region's ignored row (job.148 to job.158) and writes today's
        EVENT_IGNORED line, whose text `_oob_ignored` gives. Otherwise the flag
        row is `move_flag` with today's marker and line, compared with the
        chart at once; a status move its `flag moved` fires (ON_ICE's waiter,
        job.112; ON_NOEXEC's settlements) is compared as a STATUS move is. The
        moves the chart does not declare run after, as today: an ice's wakes
        (its box rules are the chart's), a waiter's wake on a hold, OFF_HOLD's
        start retry; a box's OFF_NOEXEC then clears the jobs it contains
        (`OFF_NOEXEC on its box`). A box's ON_NOEXEC flags them and takes
        their hold off before its own status row (`ON_NOEXEC on its box`,
        `_noexec_on_box`)."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        old = store.job[job].status
        event = ChartEvent(kind, NoexecFacts(self, job) if kind == ON_NOEXEC else None)
        fired = self._fired_rows(store.machines.dispatch(key, event))
        active = store.machines.active(key)
        move = (old, projection(active)[0])
        agreement = Agreement(job, key, event, active, move, False, moves=False)
        outcome = _FLAG_FIRED[kind].get(fired)
        if outcome is None:
            raise self._unwritten(job, event, fired, "flag row")
        if outcome == "ignored":
            why = self._oob_ignored(kind, job)
            if why is None:
                raise self._differ(
                    f"{fired[0]} ignores {kind}, which today's rules apply", agreement
                )
            self._ignore(job, _FLAG_IGNORED[kind], "EVENT_IGNORED", why)
            return
        if kind == ON_NOEXEC:
            job_ir = self.catalog.jobs.get(job)
            is_box = job_ir is not None and job_ir.job_type == "BOX"
            if outcome != _NO_STATUS_MOVE and (outcome in _NOEXEC_BOX) != is_box:
                job_type = "BOX" if is_box else "non-box"
                raise self._differ(f"{outcome} fired for a {job_type} job", agreement)
            # the status dimension: a job settles out of the queue (job.113) or
            # from FAILURE or TERMINATED (job.121), else moves nothing; a box
            # moves nothing only when INACTIVE
            if is_box:
                wrong = outcome == _NO_STATUS_MOVE and old != "INACTIVE"
            else:
                wrong = job_ir is not None and (
                    (outcome == "job.113") != (old == "QUE_WAIT")
                    or (outcome == "job.121") != (old in FAILED)
                )
            if wrong:
                raise self._differ(f"{outcome} fired for a {old} job", agreement)
            hold = fired[0] == "job.207"
            if is_box:
                self._noexec_on_box(cast(JobIR, job_ir), outcome, hold, agreement)
            else:
                self._noexec_on_job(job, job_ir, outcome, hold, agreement)
            return
        if kind == ON_ICE and (outcome == "queued") != (old == "QUE_WAIT"):
            raise self._differ(f"{fired[-1]} fired for a {old} job", agreement)
        moved = outcome != "kept"
        store.move_flag(job, _FLAG_MOVES[kind])
        cause = f"sendevent {kind}"
        if kind == ON_ICE:
            self._record(job, kind, cause)
            if outcome == "queued":
                # DL-50: an iced waiter leaves the queue for INACTIVE now
                with self._moving(job, key, event, old):
                    store.dequeue_waiter(job)
                    self._set_status(job, JOB_ICE_QUEUED, cause="iced while queued (DL-50)")
                return
            self._agree_after(agreement, "the flag's writes")
            if moved:  # SEM-20, DL-285: a second ice resolves nothing
                self._ice_resolves_member(job, old, self._ancestor_runs(job))
            self._wake_referencers(job, cause=f"{job!r} put ON_ICE")
            return
        if kind == OFF_ICE and moved:
            box = self._off_ice_in_running_box(job)
            if box is not None:
                cause += f"; taken off ice in its running box {box!r}: sits out this run (SEM-20)"
        elif kind == DISARM and not moved:
            cause += " (no latch)"
        self._record(job, kind, cause)
        self._agree_after(agreement, "the flag's writes")
        if kind in (ON_HOLD, OFF_HOLD) and old == "QUE_WAIT":
            self._wake_waiters()  # DL-247, DL-50: the queue's scan reads the hold
        elif kind == OFF_HOLD:
            # SEM-21: if conditions are already satisfied, run immediately
            self._attempt_start(job, force=False, scheduled=False, cause="OFF_HOLD")
        elif kind == OFF_NOEXEC:
            # DL-254: on a box, every job it contains is cleared too
            job_ir = self.catalog.jobs.get(job)
            if job_ir is not None and job_ir.job_type == "BOX":
                self._box_off_noexec(job)

    def _noexec_flags(self, job: str, hold: bool) -> None:
        """ON_NOEXEC's flag rows on the job's row, with today's markers
        (`_set_noexec`): the flag on (job.209, job.222), then the hold it
        supersedes off where the chart clears one (job.207)."""
        self.store.move_flag(job, NOEXEC_ON)
        self._record(job, ON_NOEXEC, "sendevent ON_NOEXEC")
        if hold:
            self.store.move_flag(job, NOEXEC_CLEARS_HOLD)
            self._record(job, OFF_HOLD, "ON_NOEXEC supersedes ON_HOLD (SEM-22, DL-254)")

    def _noexec_on_job(
        self, job: str, job_ir: JobIR | None, settles: str, hold: bool, agreement: Agreement
    ) -> None:
        """ON_NOEXEC on a job that is not a box (`_noexec_job`): its flag rows,
        then the status row its `flag moved` fires, through DL-242's INACTIVE
        path: a waiter leaves the queue (job.113), a FAILURE or TERMINATED
        settles (job.121), or nothing moves (job.198). A job the input released
        from a hold or the queue retries its start, as today (`_retry_released`);
        a job outside the catalog does not."""
        run_number = self.store.job[job].run_number
        self._noexec_flags(job, hold)
        if job_ir is None or settles == _NO_STATUS_MOVE:
            self._agree_after(agreement, "the flags' writes")
            if job_ir is None:
                return
        else:
            with self._moving(job, agreement.key, agreement.event, agreement.move[0]):
                if settles == "job.113":
                    self.store.dequeue_waiter(job)
                    self._inject_inactive(
                        job_ir,
                        None,
                        JOB_NOEXEC_QUEUED,
                        clear_exit_code=True,
                        cause="ON_NOEXEC takes a queued job out of the queue (DL-254)",
                    )
                else:
                    self._inject_inactive(
                        job_ir,
                        None,
                        NOEXEC_COMPLETED,
                        clear_exit_code=True,
                        cause="ON_NOEXEC settles a completed job to INACTIVE (DL-243)",
                    )
        if hold or settles == "job.113":
            self._retry_released(job, run_number)

    def _noexec_on_box(self, box_ir: JobIR, settles: str, hold: bool, agreement: Agreement) -> None:
        """ON_NOEXEC on a box (`_noexec_box`): its flag rows; `ON_NOEXEC on
        its box` on every job it contains (`_box_on_noexec`, DL-347); their
        exit codes cleared, as today; then the status row its `flag moved`
        fires, through DL-242's box INACTIVE path: from an idle status
        (job.136), the queue (job.169) or STARTING (job.170); or nothing moves
        (job.198). The jobs whose hold the chart cleared retry their start, as
        today."""
        box = box_ir.name
        tree = [box, *self._contained(box, skip_live=False)]
        runs = {job: self._runtime(job).run_number for job in tree}
        self._noexec_flags(box, hold)
        released = [box] if hold else []
        released += self._box_on_noexec(box, tree[1:])
        for job in tree:
            rt = self._runtime(job)
            if rt.status == "INACTIVE" and rt.exit_code is not None:
                self.store.clear_exit_code(job)
        if settles == _NO_STATUS_MOVE:
            self._agree_after(agreement, "the flags' writes")
        else:
            with self._moving(box, agreement.key, agreement.event, agreement.move[0]):
                self._inject_inactive(
                    box_ir,
                    None,
                    BOX_NOEXEC,
                    clear_exit_code=True,
                    cause="ON_NOEXEC on a box: CHANGE_STATUS INACTIVE (DL-254)",
                )
        for job in released:
            self._retry_released(job, runs[job])

    # ---- the queue: RELEASE_RESOURCE and the readmission

    def _release_resource(self, job: str) -> None:
        """RELEASE_RESOURCE on the job's instance (DL-345), its fired row
        performed by today's writes (DL-256): units the job holds after its run
        go back to the pool with today's line, and the waiters wake (job.307);
        a job that holds nothing (job.315), or a live run whose units go back
        when it ends (job.316), takes today's no-effect line, which names a
        live status where there is one. Whether the job holds units it can
        give back is the row's: a row that says otherwise is a
        `StatechartError` before any write. The release is compared with the
        chart after its writes, before the waiters wake."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        row = store.job[job]
        event = ChartEvent(RELEASE_RESOURCE)
        fired = self._fired_rows(store.machines.dispatch(key, event))
        if fired != _RELEASED and fired not in _RELEASE_IGNORED:
            raise self._unwritten(job, event, fired, "release row")
        active = store.machines.active(key)
        move = (row.status, row.status)
        agreement = Agreement(job, key, event, active, move, False, moves=False)
        if (fired == _RELEASED) != (bool(row.reservations) and row.status not in LIVE):
            units = "holding units" if row.reservations else "holding nothing"
            raise self._differ(f"{fired[0]} fired for a {row.status} job {units}", agreement)
        if fired != _RELEASED:
            why = "nothing held"
            if row.status in LIVE:
                why = f"no effect: {row.status}; the run's units go back when it ends"
            self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE ({why})")
            return
        freed = store.release_held(job, HELD_RELEASED)
        units = ", ".join(f"{held.units} of {held.bucket[2:]}" for held in freed)
        self._record(job, RELEASE_RESOURCE, f"sendevent RELEASE_RESOURCE (frees {units})")
        self._agree_after(agreement, "the release's writes")
        self._wake_waiters()

    def _readmit(self, job: str, *, keep_stopped: bool = False) -> str:
        """One queue scan's attempt for a waiter is one `readmit` on its
        instance (DL-345). The scan and its order stay today's code
        (`_wake_waiters` reads the order off the rows, DL-50, DL-255); the
        chart decides each waiter, and today's methods write what its rows
        say. A waiter that fits leaves the queue and starts (job.108:
        `_acquire`, then `_run`, its status writes compared as a start's,
        DL-342). A member whose box stopped running is cancelled (job.109,
        `_cancel_waiter`). One that fails the queued recheck loses its arm
        first (job.216; job.230 finds none), and its `flag moved` leaves the
        queue unstarted (job.110, `_leave_queue_unstarted`). The arm the row
        drops, and whether the recheck failed, are the rows' choices, checked
        before any write. Any other stays queued (job.181). A cancel and a
        leave are compared as a STATUS move is. The return value is today's,
        for the scan."""
        store = self.store
        store.follow_job(job)
        key = store.job_key(job)
        row = store.job[job]
        job_ir = self.catalog.jobs[job]
        facts = ReadmitFacts(self, job, job_ir, keep_stopped=keep_stopped)
        event = ChartEvent(READMIT, facts)
        fired = self._fired_rows(store.machines.dispatch(key, event))
        outcome = _READMIT_FIRED.get(fired)
        if outcome is None:
            raise self._unwritten(job, event, fired, "readmit row")
        if outcome == "keep":
            return "held" if row.on_hold and facts.known("box") != "idle" else "waiting"
        active = store.machines.active(key)
        old = row.status
        agreement = Agreement(job, key, event, active, (old, projection(active)[0]), False)
        if outcome == "cancel":
            with self._moving(job, key, event, old):
                return self._cancel_waiter(job, f"box {job_ir.box.box_name!r} no longer RUNNING")
        failed = facts.known("recheck")
        if (outcome == "leave") != (failed is not None):
            verdict = "failed" if failed else "passed"
            raise self._differ(
                f"{fired[0]} fired for a waiter whose queued recheck {verdict}", agreement
            )
        if outcome == "leave":
            armed = fired[0] == _LEAVE_ARMED
            if armed != row.armed:
                raise self._differ(
                    f"{fired[0]} fired for a waiter with armed={row.armed}", agreement
                )
            if armed:
                # DL-257: the arm goes first, before any wake can ride it
                store.move_flag(job, QUEUE_LEFT_DISARMS)
                self._record(
                    job,
                    "SCHED_DISARM",
                    f"left QUE_WAIT unstarted; waits for its next start time"
                    f" (queued-recheck={self.semantics.queued_recheck}, DL-257)",
                )
            check, why = failed
            with self._moving(job, key, event, old):
                return self._leave_queue_unstarted(job_ir, check, why)
        if self._start_move is not None and self._start_move.moves:
            raise self._differ("a readmission began before a start's status write", agreement)
        # a window's start that moves nothing (job.180) is done once the cascade
        # readmits: DL-154's window skip of a member already INACTIVE passes the
        # completion door, its box may end and release, and the release readmits
        self._start_move = agreement
        try:
            store.dequeue_waiter(job)
            # a held excess freed here is seen by the running scan's next pass
            self._acquire(job, facts.vector())
            self._run(
                job_ir,
                JOB_READMIT,
                cause="resources freed (QUE_WAIT admitted, DL-50)",
                had_demand=True,
            )
        finally:
            unwritten = self._start_move is agreement
            if unwritten:
                self._start_move = None
        if unwritten:
            raise self._differ("the readmission's status write never ran", agreement)
        return "admitted"

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
        facts = StartFacts(self, job, job_ir)
        try:
            relayed, start, after, (status, active) = self._decide_start(job, facts, relay)
        except RecursionError as exc:
            if self._starts_in_progress:
                raise
            # the fit fell short for this catalog, whatever recursed (the
            # cascade, or a fact's own recursion such as a deep condition):
            # the chart's selection is deeper than today's gates, so the limit
            # can be met there before any start, and this attempt is named
            # as the cascade's first start
            raise CascadeDepthError(job, cause, sys.getrecursionlimit()) from exc
        verdict = self._start_verdict(job, start, after)
        attempt = cast(StartFacts, start.event.payload)
        side = attempt.known("window")
        outside = side not in (None, "inside")
        if rt is None or verdict == "ignored" and not relayed and not outside:
            return None
        key = self.store.job_key(job)
        moves = verdict in _STATUS_MOVES
        agreement = Agreement(
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
        self, job: str, facts: StartFacts, relay: str | None
    ) -> tuple[tuple[str, ...], Outcome, list[Outcome], tuple[str, frozenset[str]]]:
        """Dispatch the attempt to the job's instance, brought to its row
        first, and return the relay's rows, the `start` item's outcome, the
        outcomes of the items that follow from it, and the status and
        configuration the chart left.

        DL-340: past the bound, an attempt whose run's settlement the box
        rules still hold back takes job.401 and moves nothing. The run settles
        then, at the point in the verdict order where today's `_start_admitted`
        settles it, and the same attempt is dispatched again
        (`StartFacts.settled`), so it reads the admission the settlement
        leaves. No settlement is owed at an input's own attempt."""
        store = self.store
        machines = store.machines
        key = store.job_key(job)
        store.follow_job(job)
        outcomes = self._expect_holding(machines.dispatch(key, ChartEvent(relay or START, facts)))
        at = next(i for i, o in enumerate(outcomes) if o.event.name == START)
        if outcomes[at].rows == (_SETTLE_FIRST,):
            assert relay is None, "a settlement owed at an input's own attempt"
            assert len(outcomes) == at + 1, "an item after job.401, before the settlement"
            self._settle_ended_run(job)
            store.follow_job(job)
            outcomes = self._expect_holding(
                machines.dispatch(key, ChartEvent(START, facts.settled()))
            )
            at = 0
        relayed = outcomes[0].rows if relay is not None else ()
        active = machines.active(key)
        return relayed, outcomes[at], outcomes[at + 1 :], (projection(active)[0], active)

    def _start_verdict(self, job: str, start: Outcome, after: list[Outcome]) -> StartVerdict:
        """The verdict the `start` item's row stands for, and the items that
        follow from it: an admitting verdict's `start admitted` comes next and
        fires job.402, and every later item is one of its descendants, the
        admission's moves; no other verdict sends it."""
        verdict = _START_ROWS.get(start.rows[0]) if len(start.rows) == 1 else None
        if verdict is None:
            what = f"start fired {', '.join(start.rows) or 'no row'}, and no start row"
            raise self._chart_error(job, start, f"{what} the oracle writes")
        admitted = [o for o in after if o.event.name == START_ADMITTED]
        extent = not admitted
        if verdict in _ADMITTING:
            first = after[0] if after else start
            extent = len(admitted) == 1 and admitted[0] is first and first.rows == (_EXTENT,)
            extent = extent and all(o.depth > first.depth for o in after[1:])
        if not extent:
            items = "; ".join(f"{o.event.name}: {', '.join(o.rows)}" for o in after) or "none"
            what = f"start fired {start.rows[0]} ({verdict}), and the items after it are {items}"
            raise self._chart_error(job, start, what)
        return verdict

    def _chart_error(self, job: str, outcome: Outcome, what: str) -> StatechartError:
        error = StatechartError(f"job {job!r}: {what}")
        return self.store.machines.explain(error, self.store.job_key(job), outcome.event)

    def _refusal(self, job: str, rt: JobRuntime, attempt: StartFacts) -> str:
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
        """`Oracle._start_admitted` writing the chart's verdict: a bypass
        (job.101), a start on held units (job.104), a queue (job.103) or an
        admission (job.102, job.124) through today's methods. The ended run's
        settlement that today's method makes first is the chart's job.401,
        made before the attempt's second dispatch (`_decide_start`, DL-340).
        `force` is unread: the chart decided it; the parameter keeps the old
        method's signature for its callers."""
        job_ir = self.catalog.jobs[job]
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
