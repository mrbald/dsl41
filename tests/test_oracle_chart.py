"""The second oracle, `ChartOracle` (DL-341): its job instances on the
state owner's executor, the STATUS family through the `job` chart, and the
check that the chart and today's writes agree.

The lockstep cases run each script on the old oracle and the chart oracle
together (`LockstepOracle`), so a difference in the trace, the emitted events,
the rows, the revisions or the violations fails the case at its input."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from oracle_lockstep import LockstepOracle

from dsl41 import oracle_chart
from dsl41.ir import lower_source
from dsl41.job_chart import JobData, configuration, row_configuration
from dsl41.oracle import Oracle, fit_recursion_limit
from dsl41.oracle_chart import ChartOracle, job_data
from dsl41.oracle_state import (
    CapacityReservation,
    CascadeDepthError,
    CarriedRows,
    Event,
    EventKind,
    JobRuntime,
    OracleError,
    ReleasePolicy,
    RuntimeState,
)
from dsl41.runner_hosts import HostCommand, host_move
from dsl41.state_machine import Executor, Outcome, Statechart, StatechartError

T0 = datetime(2026, 7, 1, 8, 0)

_JIL = (
    "insert_resource: FREE\nres_type: R\namount: 2\n\n"
    "insert_resource: KEPT\nres_type: R\namount: 2\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nresources: (FREE, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: k\njob_type: c\ncommand: x\nmachine: m1\nresources: (KEPT, QUANTITY=1, FREE=N)\n\n"
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\njob_terminator: 1\n\n"
    "insert_job: n\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\nauto_hold: 1\n"
)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def _agrees(oracle: Oracle) -> None:
    """Every row has an instance in the configuration the row projects. After
    an input this pins the commit's realign and that no row lacks an instance;
    right after a fork or a construction it pins the copy and the restore."""
    for job, row in oracle.store.job.items():
        assert oracle.store.machines.active(RuntimeState.job_key(job)) == row_configuration(row)


def _lockstep(jil: str, script: list[Event]) -> LockstepOracle:
    paired = LockstepOracle(lower_source(jil), new=ChartOracle)
    for event in script:
        paired.feed(event)
    paired.check_seal()
    _agrees(paired.twin)
    return paired


# ---- the STATUS family, against the old oracle


def _restart_jil(res_type: str, free: str) -> str:
    return (
        f"insert_resource: Q\nres_type: {res_type}\namount: 2\n\n"
        "insert_job: ob\njob_type: b\ncondition: s(ob)\n\n"
        "insert_job: om\njob_type: c\ncommand: m\nmachine: m1\nbox_name: ob\n"
        f"resources: (Q, QUANTITY=1{free})\n"
    )


def _idle_restart_jil(res_type: str) -> str:
    """A box that restarts on its own success, idle, and a member holding a
    unit when it runs: a STATUS on the member that does not end a run ends the
    box by its idle recompute, and the box's restart starts the member again
    inside the member's own box rules."""
    return (
        f"insert_resource: R\nres_type: {res_type}\namount: 1\n\n"
        "insert_job: b\njob_type: b\ncondition: s(b)\n\n"
        "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n"
        "resources: (R, QUANTITY=1, FREE=Y)\n"
    )


_CASES: dict[str, tuple[str, list[Event]]] = {
    "exit codes": (
        _JIL,
        [
            ev("STARTJOB", 1, job="j"),
            ev("STATUS", 2, job="j", exit_code=0),
            ev("STARTJOB", 3, job="j"),
            ev("STATUS", 4, job="j", exit_code=3),
            ev("STATUS", 5, job="nowhere", exit_code=0),
        ],
    ),
    "injected statuses": (
        _JIL,
        [
            ev("STATUS", 1, job="j", status="STARTING"),
            ev("STATUS", 2, job="j", status="RUNNING"),
            ev("STATUS", 3, job="j", status="SUCCESS", exit_code=0),
            ev("STATUS", 4, job="j", status="FAILURE"),
            ev("STATUS", 5, job="j", status="TERMINATED"),
            ev("STATUS", 6, job="nowhere", status="INACTIVE"),
        ],
    ),
    "units held, then the run's again": (
        _JIL,
        [
            ev("STARTJOB", 1, job="k"),
            ev("STATUS", 2, job="k", status="FAILURE"),
            ev("STATUS", 3, job="k", status="RUNNING"),
            ev("STATUS", 4, job="k", status="TERMINATED"),
            ev("STATUS", 5, job="k", status="INACTIVE"),
        ],
    ),
    "INACTIVE on a job, a member and a box": (
        _JIL,
        [
            ev("STARTJOB", 1, job="j"),
            ev("STATUS", 2, job="j", status="INACTIVE"),
            ev("FORCE_STARTJOB", 3, job="b"),
            ev("STATUS", 4, job="m", status="INACTIVE"),
            ev("STATUS", 5, job="b", status="INACTIVE", exit_code=1),
            ev("SET_GLOBAL", 6, name="G", value="1"),
        ],
    ),
    **{
        f"a box restarted by its own completion ({release})": (
            _restart_jil(res_type, free),
            [ev("FORCE_STARTJOB", 1, job="ob"), ev("STATUS", 2, job="om", exit_code=0)],
        )
        for release, res_type, free in (
            ("free", "R", ", FREE=Y"),
            ("keep", "R", ", FREE=N"),
            ("spend", "D", ""),
        )
    },
    **{
        f"an idle box restarted by its member's end ({res_type}, {name})": (
            _idle_restart_jil(res_type),
            script,
        )
        for res_type in ("R", "D")
        for name, script in (
            ("never run", [ev("STATUS", 1, job="m", exit_code=0)]),
            (
                "after a failure",
                [
                    ev("FORCE_STARTJOB", 1, job="b"),
                    ev("STATUS", 2, job="m", status="FAILURE"),
                    ev("STATUS", 3, job="m", status="SUCCESS"),
                ],
            ),
        )
    },
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_sem09_status_on_the_chart_oracle_writes_what_the_old_oracle_writes(case: str) -> None:
    """Each STATUS row and branch, the settlement's three policies, the
    relabelling of held units and a box restarted by its own completion: the
    chart oracle leaves the trace, the events, the rows, the revisions and the
    seal the old oracle leaves, and every instance agrees with its row."""
    jil, script = _CASES[case]
    _lockstep(jil, script)


def _padded(depth: int, call: Callable[[], None]) -> None:
    if depth:
        _padded(depth - 1, call)
    else:
        call()


@pytest.mark.parametrize("depth", [0, 25, 400, 900])
def test_a_terminator_loop_meets_the_recursion_limit_where_the_old_oracle_meets_it(
    depth: int,
) -> None:
    """A box that restarts on its own TERMINATED, and a member that is its
    box_terminator and a job_terminator: a failed member ends and restarts the
    box until the cascade passes the fitted recursion limit (DL-340 records
    the loop). The chart oracle adds no frame to that recursion, so both sides
    raise after the same trace, whatever depth the input is applied from."""
    jil = (
        "insert_job: tb\njob_type: b\ncondition: t(tb)\n\n"
        "insert_job: tm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: tb\n"
        "box_terminator: 1\njob_terminator: 1\n"
    )
    limit = sys.getrecursionlimit()
    try:
        fit_recursion_limit(2)
        paired = LockstepOracle(lower_source(jil), new=ChartOracle)
        paired.feed(ev("FORCE_STARTJOB", 1, job="tb"))
        with pytest.raises(CascadeDepthError):
            _padded(depth, lambda: paired.feed(ev("STATUS", 2, job="tm", exit_code=2)))
    finally:
        sys.setrecursionlimit(limit)


def test_a_status_input_fires_a_job_row_on_the_state_owners_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STATUS dispatches to the job's instance: the status row fires, its
    continuation reports the run's leaving, the holding row of the units'
    policy frees them, and the instance ends where the row ends."""
    oracle = ChartOracle(lower_source(_JIL))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    machines = oracle.store.machines
    seen: list[Outcome] = []
    dispatch = machines.dispatch

    def spy(key: Any, event: Any) -> list[Outcome]:
        outcomes = dispatch(key, event)
        seen.extend(outcomes)
        return outcomes

    monkeypatch.setattr(machines, "dispatch", spy)
    oracle.feed(ev("STATUS", 2, job="j", exit_code=0))
    fired = [(o.key, o.kind, o.rows) for o in seen if o.key == "job:j"]
    assert fired == [
        ("job:j", "fired", ("job.117",)),
        ("job:j", "continued", ()),
        ("job:j", "fired", ("job.304",)),
    ]
    assert machines.active("job:j") == configuration("SUCCESS")


def test_sem09_a_payload_the_oracle_refuses_is_refused_before_the_chart() -> None:
    oracle = LockstepOracle(lower_source(_JIL), new=ChartOracle)
    with pytest.raises(OracleError, match="STATUS requires payload.status or integer"):
        oracle.feed(ev("STATUS", 1, job="j", exit_code="0"))
    with pytest.raises(OracleError, match="unknown status 'QUE_WAIT'"):
        oracle.feed(ev("STATUS", 2, job="j", status="QUE_WAIT"))
    with pytest.raises(OracleError, match="unknown status 3"):
        oracle.feed(ev("STATUS", 3, job="j", status=3))


# ---- the agreement check


def test_a_flag_the_chart_holds_and_the_row_does_not_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: the instance is iced and no follow brings it back, so the
    chart's SUCCESS keeps the ice that today's write does not have."""
    oracle = ChartOracle(lower_source(_JIL))
    store = oracle.store
    monkeypatch.setattr(store, "follow_job", lambda job: None)
    store.machines.realign(
        "job:j", configuration("INACTIVE", ice=True), store.machines.data("job:j")
    )
    with pytest.raises(StatechartError, match="the configuration differs as written") as raised:
        oracle.feed(ev("STATUS", 1, job="j", status="SUCCESS"))
    text = str(raised.value)
    assert "ice_on" in text and "ice_off" in text
    assert "instance: 'job:j'" in text


def test_a_holding_the_chart_keeps_and_the_row_frees_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: the data says the run's units are kept, today's settlement
    frees them."""
    oracle = ChartOracle(lower_source(_JIL))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    monkeypatch.setattr(
        oracle_chart,
        "job_data",
        lambda *_: JobData(release_on_success="keep", release_otherwise="keep"),
    )
    with pytest.raises(StatechartError, match="the holding differs after the settlement"):
        oracle.feed(ev("STATUS", 2, job="j", exit_code=0))


def test_a_move_whose_settlement_never_runs_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: today's consequences of the move are dropped, so the chart's
    holding is never compared."""
    oracle = ChartOracle(lower_source(_JIL))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    monkeypatch.setattr(Oracle, "_after_transition", lambda self, job, old, new: None)
    with pytest.raises(StatechartError, match="the move's settlement never ran"):
        oracle.feed(ev("STATUS", 2, job="j", exit_code=0))


def test_an_instance_that_differs_from_its_row_at_construction_is_a_statechart_error() -> None:
    class Iced(ChartOracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, _active, data = super()._job_instance(job, row)
            return chart, configuration("INACTIVE", ice=True), data

    with pytest.raises(
        StatechartError, match="'j': the instance and the row differ at construction"
    ):
        Iced(lower_source(_JIL))


def _swapped(chart: Statechart, a: str, b: str) -> Statechart:
    return replace(chart, guards={**chart.guards, a: chart.guards[b], b: chart.guards[a]})


@pytest.mark.parametrize(("job", "row"), [("b", "job.120"), ("j", "job.135")])
def test_an_inactive_row_for_the_other_job_type_is_a_statechart_error(job: str, row: str) -> None:
    """Planted: the job's and the box's INACTIVE guards swapped. The row the
    chart fires decides the move, so the wrong one cannot pass as the path
    `_inject_inactive` would take for the catalog's job type."""

    class Swapped(ChartOracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return _swapped(chart, "injects_inactive_job", "injects_inactive_box"), active, data

    oracle = Swapped(lower_source(_JIL))
    with pytest.raises(StatechartError, match=f"job '{job}': {row} fired for a"):
        oracle.feed(ev("STATUS", 1, job=job, status="INACTIVE"))


def test_a_status_row_the_oracle_does_not_write_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: the oracle forgets job.117's move; the row fired is named."""
    moves = {row: move for row, move in oracle_chart._MOVES.items() if row != "job.117"}
    monkeypatch.setattr(oracle_chart, "_MOVES", moves)
    oracle = ChartOracle(lower_source(_JIL))
    with pytest.raises(StatechartError, match="'j': STATUS fired job.117, and no status row"):
        oracle.feed(ev("STATUS", 1, job="j", exit_code=0))


# ---- where the instances are


def test_the_old_oracle_keeps_no_job_instances() -> None:
    oracle = Oracle(lower_source(_JIL))
    assert oracle.store.job_instance is None
    with pytest.raises(StatechartError, match="no instance 'job:j'"):
        oracle.store.machines.active("job:j")


def test_every_row_has_its_instance_after_construction_an_input_and_a_read() -> None:
    """The constructor restores one instance per row; an input that moves rows
    outside the chart (a start) is followed at its commit; a row read into
    existence gets its instance as it is created."""
    oracle = ChartOracle(lower_source(_JIL))
    _agrees(oracle)
    assert oracle.store.machines.data("job:b") == JobData(box=True)
    assert oracle.store.machines.data("job:m") == JobData(job_terminator=True)
    assert oracle.store.machines.data("job:n") == JobData(auto_hold=True)
    oracle.feed(ev("STARTJOB", 1, job="k"))
    assert oracle.store.machines.active("job:k") == configuration("RUNNING", holding="reserved")
    assert oracle.store.machines.data("job:k") == JobData(
        release_on_success="keep", release_otherwise="keep"
    )
    oracle.store.runtime("ghost")
    assert oracle.store.machines.data("job:ghost") == JobData(catalog=False)
    _agrees(oracle)


def test_a_fork_copies_the_instances_and_its_store_asks_the_fork() -> None:
    """The cheap invariant at a fork: the twin holds every instance, agreeing
    with its rows, its owner asks the twin for job data, and a STATUS on the
    twin leaves the parent's instance where it was."""
    oracle = ChartOracle(lower_source(_JIL))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    twin = oracle.fork()
    _agrees(twin)
    instance: Callable[..., Any] | None = twin.store.job_instance
    assert instance is not None and getattr(instance, "__self__", None) is twin
    twin.feed(ev("STATUS", 2, job="j", exit_code=0))
    assert twin.store.machines.active("job:j") == configuration("SUCCESS")
    assert oracle.store.machines.active("job:j") == configuration("RUNNING", holding="reserved")
    _agrees(oracle)


def test_a_carried_install_and_a_witness_restore_the_instances_from_the_rows() -> None:
    """A period that opens over carried rows restores their instances, a row
    whose job the catalog no longer has with `catalog=False`; a witness
    rebuilds the executor with them."""
    first = ChartOracle(lower_source(_JIL))
    first.feed(ev("STARTJOB", 1, job="k"))
    first.feed(ev("STATUS", 2, job="k", status="FAILURE"))
    store = first.store
    carried = CarriedRows(
        jobs=dict(store.job),
        globals_=dict(store.globals_),
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        now=T0 + timedelta(minutes=2),
    )
    second = ChartOracle(lower_source(_JIL.split("insert_job: b")[0]), carried=carried)
    _agrees(second)
    assert second.store.machines.active("job:k") == configuration("FAILURE", holding="held")
    assert second.store.machines.data("job:m") == JobData(catalog=False)
    second.store.begin_input()
    second.store.register_host("h")
    second.store.witness_host("h", state="passive")
    second.store.commit_input()
    _agrees(second)
    assert second.store.machines.data("job:k") == JobData(
        release_on_success="keep", release_otherwise="keep"
    )


def test_a_commit_follows_only_jobs_that_have_a_row() -> None:
    """A timer alone touches its job's key. Armed and fired in one input, it
    moves no revision, and the commit creates no row for the job, as the old
    oracle's does not."""
    oracle = ChartOracle(lower_source(_JIL))
    store = oracle.store
    store.begin_input()
    store.enqueue_timer(T0, ev("TIMER", job="rowless", check="must_start"))
    assert store.pop_timer_due(T0) is not None
    assert store.commit_input() == []
    assert "rowless" not in store.job
    with pytest.raises(StatechartError, match="no instance 'job:rowless'"):
        store.machines.active("job:rowless")


# ---- the data


def test_job_data_reads_the_policy_of_each_unit_for_both_ends() -> None:
    """`release_reservations`' rules: a `completion` unit is freed by every
    end and a `success` unit by SUCCESS; what is not freed is kept on a
    renewable and spent on a depletable, and one kept unit makes the end keep."""
    oracle = ChartOracle(
        lower_source(
            "insert_resource: R\nres_type: R\namount: 2\n\n"
            "insert_resource: D\nres_type: D\namount: 2\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        )
    )

    def data(*units: tuple[str, ReleasePolicy]) -> tuple[str, str]:
        row = JobRuntime(
            reservations=tuple(
                CapacityReservation(bucket=bucket, units=1, release_policy=policy)
                for bucket, policy in units
            )
        )
        found = job_data(oracle, "j", row)
        return found.release_on_success, found.release_otherwise

    assert data() == ("free", "free")
    assert data(("m:m1", "completion")) == ("free", "free")
    assert data(("r:R", "success")) == ("free", "keep")
    assert data(("r:D", "success")) == ("free", "spend")
    assert data(("r:D", "never")) == ("spend", "spend")
    assert data(("r:D", "never"), ("r:R", "never")) == ("keep", "keep")


def test_the_host_gates_scratch_executor_holds_no_job_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads the host chart on a scratch executor rebuilt from the
    rows (DL-333); on the chart oracle it leaves the job instances out, so a
    host command's cost does not grow with the job count."""
    oracle = ChartOracle(lower_source(_JIL))
    store = oracle.store
    store.begin_input()
    store.register_host("h")
    store.commit_input()
    scratch: list[Executor] = []
    rebuild = store.rebuild_machines

    def spy(*, jobs: bool = True) -> Executor:
        scratch.append(rebuild(jobs=jobs))
        return scratch[-1]

    monkeypatch.setattr(store, "rebuild_machines", spy)
    outcome = host_move(store, HostCommand(verb="drain", host_id="h"))
    assert outcome.rows
    (machines,) = scratch
    assert machines.active("host:h") == {"host", "routed", "passive"}  # the read's own move
    assert store.machines.active("host:h") == {"host", "routed", "active"}
    with pytest.raises(StatechartError, match="no instance 'job:j'"):
        machines.active("job:j")
    assert store.machines.active("job:j") == configuration("INACTIVE")
