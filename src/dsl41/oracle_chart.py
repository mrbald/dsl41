"""The second oracle: `Oracle` with its job rows driven by the `job` statechart
(docs/statechart-engine.md §16, DL-341).

`ChartOracle` is built beside the old oracle and converted one input family at
a time, under the old-versus-new harness (tests/oracle_lockstep.py, DL-338),
which names it as `dsl41.oracle_chart:ChartOracle`. It overrides the handlers
of the families converted so far and inherits every other one, so `oracle.py`
stays the twin it is compared with. The STATUS family is converted. The other
families, the cascade between instances (the box rules and the wakes), the
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
back to its row at the input's commit, and the STATUS handler does so for its
job before it dispatches (`follow_job`). That refresh is also where `JobData`'s
release fields become the current run's. With that follow before the dispatch,
the chart decides each STATUS from the row's projection: the harness proves the
STATUS rows, not the chart's own evolution, which becomes the contract as the
families convert and the follow and `Executor.realign` go.

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
settlement writes; the flags, where it shows only that today's STATUS write
moves none (the other families' flag moves are realigned before the dispatch,
not checked); and the job type behind the INACTIVE pair, which
`_inject_inactive` reads from the catalog (a box that fires job.120, or a job
that fires job.135, is a `StatechartError`). A move that leaves STARTING or
RUNNING has its flags compared when its status is written, before the box rules
run, and its holding when its own settlement has run (late, early in a restart
by DL-340, or first on the batch path). Any other move settles nothing, so its
holding is compared with the flags at the write, before the box rules can start
the job again. A later move of the same job by the cascade, which the chart does
not declare yet, is never taken for a difference. A difference is a `StatechartError` naming both
configurations. The constructor compares every row with its instance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from dsl41.ir import CatalogIR, Semantics
from dsl41.job_chart import (
    JOB,
    STATUS,
    Injection,
    JobData,
    Release,
    projection,
    row_configuration,
)
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    BOX_STATUS_INACTIVE,
    INJECTABLE_STATUSES,
    LIVE,
    STATUS_EXIT_FAILURE,
    STATUS_EXIT_SUCCESS,
    STATUS_INACTIVE,
    STATUS_INJECTED,
    CarriedRows,
    Event,
    JobRuntime,
    JobStatus,
    OracleError,
    Transition,
)
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Statechart, StatechartError

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
    """The chart's move for one STATUS, waiting for today's writes to compare
    against: the job, its instance's key, the event, the configuration the
    chart left, the move as its two statuses, and whether it leaves STARTING
    or RUNNING, so that its holding waits for its settlement. `written`: the
    first write is compared."""

    job: str
    key: str
    event: ChartEvent
    active: frozenset[str]
    move: tuple[str, str]
    releasing: bool
    written: bool = False


class ChartOracle(Oracle):
    """`Oracle` with the STATUS family on the `job` chart (DL-341)."""

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
            error = StatechartError(
                f"job {job!r}: STATUS fired {', '.join(fired) or 'no row'}, and no status row"
                " the oracle writes"
            )
            raise store.machines.explain(error, key, event)
        active = store.machines.active(key)
        new = cast(JobStatus, projection(active)[0])
        releasing = old in LIVE and new not in LIVE
        agreement = self._agreement = _Agreement(job, key, event, active, (old, new), releasing)
        try:
            if row in _INJECT_INACTIVE:
                job_ir = self.catalog.jobs[job]
                if (row == _BOX_INACTIVE) != (job_ir.job_type == "BOX"):
                    raise self._differ(f"{row} fired for a {job_ir.job_type} job", agreement)
                self._inject_inactive(job_ir, code, _MOVES[row])
            else:
                self._set_status(
                    job, _MOVES[row], cause="injected STATUS", exit_code=code, status=new
                )
            if self._agreement is not None:
                raise self._differ("the move's settlement never ran", self._agreement)
        finally:
            self._agreement = None

    def _record(self, job: str, transition: str, cause: str) -> None:
        """Every status write is followed at once by its trace line, before
        the box rules or the settlement run, so the first write is compared
        here. It adds one leaf frame, as `_settle_row` does, and neither call is
        ever the cascade's deepest point: the recursion runs through
        `_after_transition`, which this class leaves alone, so the cascade meets
        the fitted recursion limit where the old oracle meets it."""
        self._compare_written(job)
        super()._record(job, transition, cause)

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


def _listed(active: frozenset[str]) -> str:
    return ", ".join(sorted(active))
