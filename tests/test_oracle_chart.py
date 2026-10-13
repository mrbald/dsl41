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
from dsl41.capacity import CapacityPool
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
from dsl41.state_machine import Event as ChartEvent
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


def test_a_terminator_loop_raises_the_cascade_error_on_both_oracles() -> None:
    """A box that restarts on its own TERMINATED, and a member that is its
    box_terminator and a job_terminator: a failed member ends and restarts the
    box until the cascade passes the fitted recursion limit (DL-340 records
    the loop). Both oracles raise `CascadeDepthError`; where in the cascade
    each meets the limit is not compared (the harness's rule)."""
    jil = (
        "insert_job: tb\njob_type: b\ncondition: t(tb)\n\n"
        "insert_job: tm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: tb\n"
        "box_terminator: 1\njob_terminator: 1\n"
    )
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        fit_recursion_limit(2, ChartOracle)
        paired = LockstepOracle(lower_source(jil), new=ChartOracle)
        paired.feed(ev("FORCE_STARTJOB", 1, job="tb"))
        with pytest.raises(CascadeDepthError):
            paired.feed(ev("STATUS", 2, job="tm", exit_code=2))
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


# ---- the start family (STARTJOB, FORCE_STARTJOB, every attempt)

_DAILY = 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
_START_JIL = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_resource: KEPT\nres_type: R\namount: 2\n\n"
    "insert_job: plain\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: cond\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(plain)\n\n"
    f"insert_job: dated\njob_type: c\ncommand: x\nmachine: m1\n{_DAILY}\n"
    f'insert_job: wdefer\njob_type: c\ncommand: x\nmachine: m1\n{_DAILY}run_window: "10:00-11:00"\n\n'
    f'insert_job: wskip\njob_type: c\ncommand: x\nmachine: m1\n{_DAILY}run_window: "06:00-07:00"\n\n'
    "insert_job: nx\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_NOEXEC\n\n"
    "insert_job: q1\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: q2\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: kh\njob_type: c\ncommand: x\nmachine: m1\nresources: (KEPT, QUANTITY=1, FREE=N)\n\n"
    "insert_job: lp\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_NOEXEC\ncondition: s(lp)\n\n"
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n\n"
    "insert_job: mw\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\ncondition: s(plain)\n\n"
    "insert_job: wm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\ncondition: s(plain)\n"
    f'{_DAILY}run_window: "06:00-07:00"\n\n'
    "insert_job: b2\njob_type: b\n\n"
    f'insert_job: wx\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b2\n{_DAILY}run_window: "06:00-07:00"\n\n'
    "insert_job: after\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(b2)\n"
)

#: Per outcome class: the script, the job, the rows its last input's relay
#: fires (none for an attempt no input makes), the rows of that input's
#: `start` items for the job, and a trace line the old oracle writes for it.
_OUTCOMES: dict[str, tuple[list[Event], str, tuple[str, ...], list[tuple[str, ...]], str]] = {
    "admit a job": (
        [ev("STARTJOB", 1, job="plain")],
        "plain",
        ("job.177",),
        [("job.102",)],
        "INACTIVE->STARTING",
    ),
    "admit a box": (
        [ev("FORCE_STARTJOB", 1, job="b")],
        "b",
        ("job.178",),
        [("job.124",)],
        "INACTIVE->STARTING",
    ),
    "queue": (
        [ev("STARTJOB", 1, job="q1"), ev("STARTJOB", 2, job="q2")],
        "q2",
        ("job.177",),
        [("job.103",)],
        "INACTIVE->QUE_WAIT",
    ),
    "bypass": (
        [ev("STARTJOB", 1, job="nx")],
        "nx",
        ("job.177",),
        [("job.101",)],
        "INACTIVE->SUCCESS",
    ),
    "forced on held units": (
        [
            ev("STARTJOB", 1, job="kh"),
            ev("STATUS", 2, job="kh", status="FAILURE"),
            ev("FORCE_STARTJOB", 3, job="kh"),
        ],
        "kh",
        ("job.178",),
        [("job.104",)],
        "FAILURE->STARTING",
    ),
    "forced clears of ice and hold": (
        [
            ev("ON_ICE", 1, job="plain"),
            ev("ON_HOLD", 2, job="plain"),
            ev("FORCE_STARTJOB", 3, job="plain"),
        ],
        "plain",
        ("job.178", "job.203", "job.206"),
        [("job.102",)],
        "OFF_HOLD",
    ),
    "refused: live": (
        [ev("STARTJOB", 1, job="plain"), ev("STARTJOB", 2, job="plain")],
        "plain",
        ("job.177",),
        [("job.144",)],
        "START_REFUSED",
    ),
    "refused: the box is not running": (
        [ev("STARTJOB", 1, job="m")],
        "m",
        ("job.177",),
        [("job.141",)],
        "START_REFUSED",
    ),
    "refused: ran in this box run": (
        [
            ev("FORCE_STARTJOB", 1, job="b"),
            ev("STATUS", 2, job="m", exit_code=0),
            ev("STARTJOB", 3, job="m"),
        ],
        "m",
        ("job.177",),
        [("job.145",)],
        "START_REFUSED",
    ),
    "refused: taken off ice in this box run": (
        [
            ev("ON_ICE", 1, job="m"),
            ev("FORCE_STARTJOB", 2, job="b"),
            ev("OFF_ICE", 3, job="m"),
            ev("STARTJOB", 4, job="m"),
        ],
        "m",
        ("job.177",),
        [("job.141",)],
        "START_REFUSED",
    ),
    "nothing": ([ev("STARTJOB", 1, job="cond")], "cond", ("job.177",), [("job.180",)], ""),
    "arm": (
        [ev("ON_HOLD", 1, job="dated"), ev("STARTJOB", 2, job="dated")],
        "dated",
        ("job.177",),
        [("job.212",)],
        "SCHED_ARM",
    ),
    "defer": (
        [ev("STARTJOB", 1, job="wdefer")],
        "wdefer",
        ("job.177",),
        [("job.180",)],
        "RUN_WINDOW_DEFER",
    ),
    "skip": (
        [ev("STATUS", 1, job="wskip", status="SUCCESS"), ev("STARTJOB", 2, job="wskip")],
        "wskip",
        ("job.177",),
        [("job.106",)],
        "SUCCESS->INACTIVE",
    ),
    "skip a member": (
        [
            ev("FORCE_STARTJOB", 1, job="b"),
            ev("STATUS", 2, job="wm", status="FAILURE"),
            ev("FORCE_STARTJOB", 3, job="wm"),
        ],
        "wm",
        ("job.178",),
        [("job.107",)],
        "FAILURE->INACTIVE",
    ),
    "a skip that moves nothing and completes the box": (
        [
            ev("ON_HOLD", 1, job="wx"),
            ev("FORCE_STARTJOB", 2, job="b2"),
            ev("OFF_HOLD", 3, job="wx"),
            ev("STARTJOB", 4, job="wx"),
        ],
        "wx",
        ("job.177",),
        [("job.180",)],
        "RUN_WINDOW_SKIP",
    ),
    "the start bound": (
        [ev("FORCE_STARTJOB", 1, job="lp")],
        "lp",
        ("job.178",),
        [("job.101",), ("job.101",), ("job.100",)],
        "START_REFUSED",
    ),
    "an OFF_HOLD retry": (
        [ev("ON_HOLD", 1, job="plain"), ev("OFF_HOLD", 2, job="plain")],
        "plain",
        (),
        [("job.102",)],
        "INACTIVE->STARTING",
    ),
    "a wake": (
        [ev("STARTJOB", 1, job="plain"), ev("STATUS", 2, job="plain", exit_code=0)],
        "cond",
        (),
        [("job.102",)],
        "INACTIVE->STARTING",
    ),
}


def _spied(oracle: Oracle) -> list[Outcome]:
    """Every outcome the oracle's executor reports from now on."""
    machines = oracle.store.machines
    seen: list[Outcome] = []
    dispatch = machines.dispatch

    def spy(key: Any, event: Any) -> list[Outcome]:
        outcomes = dispatch(key, event)
        seen.extend(outcomes)
        return outcomes

    machines.dispatch = spy  # type: ignore[method-assign]
    return seen


@pytest.mark.parametrize("case", sorted(_OUTCOMES))
def test_sem10_each_start_outcome_fires_its_row_and_writes_what_the_old_oracle_writes(
    case: str,
) -> None:
    """Every attempt is one `start` on the job's instance, an input's through
    its relay: the row the chart fires is the outcome today's rule code
    reaches, and the chart oracle leaves the trace, the events, the rows, the
    revisions and the seal the old oracle leaves."""
    script, job, relay, starts, line = _OUTCOMES[case]
    paired = LockstepOracle(lower_source(_START_JIL), new=ChartOracle)
    for event in script[:-1]:
        paired.feed(event)
    seen = _spied(paired.twin)
    traced = len(paired.twin.trace())
    paired.feed(script[-1])
    paired.check_seal()
    _agrees(paired.twin)
    mine = [o for o in seen if o.key == RuntimeState.job_key(job)]
    relayed = [o.rows for o in mine if o.event.name in ("STARTJOB", "FORCE_STARTJOB")]
    assert relayed == ([relay] if relay else [])
    assert [o.rows for o in mine if o.event.name == "start"] == starts
    lines = [t.transition for t in paired.twin.trace()[traced:] if t.job == job]
    assert (line in lines) if line else lines == []
    if starts[-1] in (("job.102",), ("job.124",), ("job.104",)):
        started = [o.rows for o in mine if o.event.name == "started"]
        assert started == [("job.124",) == starts[-1] and ("job.125",) or ("job.105",)]


def test_sem33_a_postponed_start_replays_through_the_window_relay() -> None:
    """A run_window-deferred start's timer replays the attempt through its
    own relay (job.179) on the chart oracle, as the old oracle replays it."""
    paired = LockstepOracle(lower_source(_START_JIL), new=ChartOracle)
    paired.feed(ev("STARTJOB", 1, job="wdefer"))
    seen = _spied(paired.twin)
    paired.advance(T0 + timedelta(hours=2, minutes=1))
    paired.check_seal()
    rows = [(o.event.name, o.rows) for o in seen if o.key == "job:wdefer"]
    assert rows[:2] == [("TIMER run_window", ("job.179",)), ("start", ("job.102",))]
    assert paired.twin.store.job["wdefer"].status == "RUNNING"


def test_an_attempt_on_a_job_outside_the_catalog_moves_nothing() -> None:
    """With no row there is no instance and nothing to dispatch; with a row
    the chart ignores the job (job.180), as the old oracle does."""
    paired = LockstepOracle(lower_source(_START_JIL), new=ChartOracle)
    seen = _spied(paired.twin)
    paired.feed(ev("STARTJOB", 1, job="ghost"))
    assert "ghost" not in paired.twin.store.job
    assert [o for o in seen if o.key == "job:ghost"] == []
    paired.feed(ev("STATUS", 2, job="ghost", status="SUCCESS"))
    paired.feed(ev("FORCE_STARTJOB", 3, job="ghost"))
    paired.check_seal()
    rows = [o.rows for o in seen if o.key == "job:ghost" and o.event.name == "start"]
    assert rows == [("job.180",)]


def test_dl256_an_admission_that_frees_held_units_and_holds_load_writes_what_the_old_one_does() -> (
    None
):
    """A restart that no longer asks for the resource its job holds takes
    the new one and gives the old back, which wakes the queue; a start that
    holds a machine's load owes the admit-only scan (DL-255)."""
    text = (
        "insert_machine: box1\ntype: a\nnode_name: box1\nmax_load: 2\n\n"
        "insert_resource: OLD\nres_type: R\namount: 1\n\n"
        "insert_resource: NEW\nres_type: R\namount: 1\n\n"
        "insert_job: ha\njob_type: c\ncommand: x\nmachine: m1\nresources: (OLD, QUANTITY=1)\n\n"
        "insert_job: wa\njob_type: c\ncommand: y\nmachine: m1\nresources: (OLD, QUANTITY=1)\n\n"
        "insert_job: ml\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n"
    )
    paired = LockstepOracle(lower_source(text), new=ChartOracle)
    for event in (
        ev("STARTJOB", 0, job="ha"),
        ev("STATUS", 1, job="ha", status="FAILURE"),
        ev("STARTJOB", 2, job="wa"),
        ev("STARTJOB", 2, job="ml"),
    ):
        paired.feed(event)
    moved = lower_source(
        text.replace("(OLD, QUANTITY=1)\n\ninsert_job: wa", "(NEW, QUANTITY=1)\n\ninsert_job: wa")
    )
    for side in (paired, paired.twin):
        side.catalog = moved
        side._pool = CapacityPool(moved)
    paired.feed(ev("STARTJOB", 3, job="ha"))
    paired.check_seal()
    _agrees(paired.twin)
    assert paired.twin.store.job["wa"].status == "RUNNING"


# ---- the facts are lazy


def test_sem20_a_fact_today_never_reads_is_never_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An iced job's condition is not evaluated, nor the run window of a job
    whose condition is false: the payload computes a fact on its first read,
    in `start_verdict`'s order. Read later, it is computed then."""
    oracle = ChartOracle(lower_source(_START_JIL))
    computed: list[tuple[str, str]] = []
    cond_true, window_side = oracle._cond_true, oracle._window_side

    def counted_condition(cond: Any, job: str) -> bool:
        computed.append((job, "condition"))
        return cond_true(cond, job)

    def counted_window(job_ir: Any) -> Any:
        computed.append((job_ir.name, "window"))
        return window_side(job_ir)

    monkeypatch.setattr(oracle, "_cond_true", counted_condition)
    monkeypatch.setattr(oracle, "_window_side", counted_window)
    seen = _spied(oracle)

    def attempt(job: str) -> Any:
        starts = [o for o in seen if o.key == f"job:{job}" and o.event.name == "start"]
        return starts[-1].event.payload

    oracle.feed(ev("ON_ICE", 1, job="cond"))
    oracle.feed(ev("STARTJOB", 2, job="cond"))
    assert computed == []
    oracle.feed(ev("FORCE_STARTJOB", 3, job="b"))
    del computed[:]  # the box start's own window pass reads wm's window
    oracle.feed(ev("STARTJOB", 4, job="wm"))  # the box runs, the condition is false: it arms
    iced, member = attempt("cond"), attempt("wm")
    assert iced.known("condition") is None
    assert computed == [("wm", "condition")]
    assert member.known("condition") is False and member.known("window") is None
    assert member.window == "skip" and computed[-1] == ("wm", "window")
    assert "window='skip'" in repr(member)


# ---- the agreement check at a start's writes


def _planted(**guards: Callable[..., bool]) -> type[ChartOracle]:
    class Planted(ChartOracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return replace(chart, guards={**chart.guards, **guards}), active, data

    return Planted


def test_a_start_whose_units_the_chart_reserves_and_the_row_does_not_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: the attempt's payload says the start reserves units, and
    today's admission reserves none for a job with no resources: the chart's
    `reserved` is caught at the STARTING write."""
    monkeypatch.setattr(oracle_chart._Facts, "demand", property(lambda self: True))
    oracle = ChartOracle(lower_source(_START_JIL))
    with pytest.raises(StatechartError, match="the start's status write differs") as raised:
        oracle.feed(ev("STARTJOB", 1, job="plain"))
    assert "reserved" in str(raised.value) and "instance: 'job:plain'" in str(raised.value)


def test_a_running_write_the_chart_does_not_follow_is_a_statechart_error() -> None:
    """Planted: `started` finds no row for a job, so the instance stays
    STARTING while today's code writes RUNNING."""
    oracle = _planted(is_job=lambda ctx: False)(lower_source(_START_JIL))
    with pytest.raises(StatechartError, match="started fired job.183 for a CMD job"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))


_JOB_TYPE_PAIRS = {
    "admission": (
        ("start_admits_job", "start_admits_box"),
        [ev("STARTJOB", 1, job="plain")],
        "job.124 fired for a CMD job",
    ),
    "skip": (
        ("start_skips", "start_skips_member"),
        [ev("STATUS", 1, job="wskip", status="SUCCESS"), ev("STARTJOB", 2, job="wskip")],
        "job.107 fired for a job in box None",
    ),
    "started": (("is_job", "is_box"), [ev("STARTJOB", 1, job="plain")], "started fired job.125"),
}


@pytest.mark.parametrize("pair", sorted(_JOB_TYPE_PAIRS))
def test_a_start_row_for_the_other_job_type_is_a_statechart_error(pair: str) -> None:
    """Planted: the guards of a row pair that differs only by the job type
    swapped (job.102/124, job.106/107, job.105/125). The fired row decides
    the move, and a catalog that disagrees with it is caught at once, as for
    job.120 and job.135."""
    (a, b), script, message = _JOB_TYPE_PAIRS[pair]
    guards = oracle_chart.JOB.guards
    oracle = _planted(**{a: guards[b], b: guards[a]})(lower_source(_START_JIL))
    for event in script[:-1]:
        oracle.feed(event)
    with pytest.raises(StatechartError, match=message):
        oracle.feed(script[-1])


def test_a_running_write_whose_flags_differ_from_the_charts_is_a_statechart_error() -> None:
    """Planted: after the STARTING write the instance is iced, so `started`
    fires its row and the chart's RUNNING keeps an ice the row does not."""

    class Iced(ChartOracle):
        def _record(self, job: str, transition: str, cause: str) -> None:
            super()._record(job, transition, cause)
            if transition == "INACTIVE->STARTING":
                key = self.store.job_key(job)
                active = configuration("STARTING", ice=True)
                self.store.machines.realign(key, active, self.store.machines.data(key))

    oracle = Iced(lower_source(_START_JIL))
    with pytest.raises(StatechartError, match="the RUNNING write differs"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))


def test_an_arm_the_code_does_not_write_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: today's arm is dropped, so the chart's arm_on differs from
    the row after the attempt's writes."""
    monkeypatch.setattr(Oracle, "_arm", lambda *args: None)
    oracle = ChartOracle(lower_source(_START_JIL))
    oracle.feed(ev("ON_HOLD", 1, job="dated"))
    with pytest.raises(StatechartError, match="the start's writes differ"):
        oracle.feed(ev("STARTJOB", 2, job="dated"))


def test_a_start_whose_status_write_never_runs_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planted: the admission is dropped, so the chart's STARTING is never written."""
    monkeypatch.setattr(ChartOracle, "_start", lambda *args, **kwargs: None)
    oracle = ChartOracle(lower_source(_START_JIL))
    with pytest.raises(StatechartError, match="the start's status write never ran"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))
    assert oracle._start_move is None


def test_a_start_before_the_last_ones_status_write_is_a_statechart_error() -> None:
    oracle = ChartOracle(lower_source(_START_JIL))
    oracle._start_move = oracle_chart._Agreement(
        "q1",
        "job:q1",
        ChartEvent("start"),
        configuration("STARTING"),
        ("INACTIVE", "STARTING"),
        False,
    )
    with pytest.raises(StatechartError, match="a start began before the last one's status write"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))


def test_a_start_that_fires_two_rows_is_a_statechart_error() -> None:
    """Planted: a plain admission that also arms. The rows fired are named."""
    oracle = _planted(start_arms=lambda ctx: True)(lower_source(_START_JIL))
    with pytest.raises(StatechartError, match="start fired job.102, job.212, and no start row"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))


def test_a_failed_start_leaves_no_status_move_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """An attempt that raises before its status write clears its move, so the
    next input is not compared against it."""

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OracleError("planted")

    monkeypatch.setattr(Oracle, "_acquire", broken)
    oracle = ChartOracle(lower_source(_START_JIL))
    with pytest.raises(OracleError, match="planted"):
        oracle.feed(ev("STARTJOB", 1, job="plain"))
    assert oracle._start_move is None


# ---- DL-340's settlement and the recursion bound


def _member_restart_jil(resource: str, sibling: str) -> str:
    """A box restarted by its own completion, and a member `rm` holding a
    unit: the box's restart attempts the member before the member's run
    settles. `sibling` adds members beside it."""
    return (
        f"insert_resource: U\nres_type: {resource}\namount: 1\n\n"
        "insert_job: rb\njob_type: b\ncondition: s(rb)\n\n"
        "insert_job: rm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: rb\n"
        f"resources: (U, QUANTITY=1{', FREE=Y' if resource == 'R' else ''})\n{sibling}\n"
        "insert_job: rg\njob_type: c\ncommand: x\nmachine: m1\n"
    )


_GATED = "condition: s(rg)\n\ninsert_job: rs\njob_type: c\ncommand: x\nmachine: m1\nbox_name: rb\n"
_SETTLEMENTS: dict[str, tuple[str, list[Event], dict[str, str]]] = {
    "admitted: the spent unit leaves nothing, it queues": (
        _member_restart_jil("D", ""),
        [ev("FORCE_STARTJOB", 1, job="rb"), ev("FORCE_STARTJOB", 2, job="rm")]
        + [ev("STATUS", 3, job="rm", exit_code=0)],
        {"rm": "QUE_WAIT"},
    ),
    "held by its condition: a sibling waits for the late settlement": (
        _member_restart_jil("R", _GATED + "resources: (U, QUANTITY=1, FREE=Y)\n"),
        [
            ev("FORCE_STARTJOB", 1, job="rb"),
            ev("STATUS", 2, job="rs", exit_code=0),
            ev("FORCE_STARTJOB", 3, job="rm"),
            ev("STATUS", 4, job="rm", exit_code=0),
        ],
        {"rm": "INACTIVE", "rs": "RUNNING"},
    ),
}


@pytest.mark.parametrize("case", sorted(_SETTLEMENTS))
def test_sem16_a_restart_settles_the_ended_run_before_the_admission_it_reads(case: str) -> None:
    """DL-340: the restarted member's attempt reads its admission after the
    ended run settles, so a depletable unit it spent leaves nothing to admit
    on. A member whose own condition holds it settles nothing at the attempt:
    the late settlement runs as today, after the box rules, so a sibling the
    restart attempts first finds the unit still taken and queues, to be
    admitted when the settlement frees it (`Executor.resolve` reads whether
    the attempt reaches the admission; settling whenever one is owed would
    start the sibling at once)."""
    jil, script, statuses = _SETTLEMENTS[case]
    paired = _lockstep(jil, script)
    assert {job: paired.twin.store.job[job].status for job in statuses} == statuses
    if "rs" in statuses:
        assert "INACTIVE->QUE_WAIT" in [t.transition for t in paired.twin.trace() if t.job == "rs"]


def _empty_box(name: str, condition: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n" + (f"condition: {condition}\n" if condition else "")


def _cmd(name: str, condition: str = "") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n" + (
        f"condition: {condition}\n" if condition else ""
    )


_DEEP = 100
#: The deepest legal shapes of tests/test_cascade_depth.py: empty boxes each
#: started by the one before; a ring of them that goes round twice, nested,
#: until the start bound refuses; ON_NOEXEC jobs bypassed one inside the next.
_DEEP_SHAPES: dict[str, tuple[str, list[Event]]] = {
    "box chain": (
        "\n".join(_empty_box(f"q{i}", f"s(q{i - 1})" if i else "") for i in range(_DEEP)),
        [ev("FORCE_STARTJOB", 1, job="q0")],
    ),
    "box ring": (
        "\n".join(
            [_empty_box("q0"), _empty_box("q1", f"s(q0) | s(q{_DEEP - 1})")]
            + [_empty_box(f"q{i}", f"s(q{i - 1})") for i in range(2, _DEEP)]
        ),
        [ev("FORCE_STARTJOB", 1, job="q0")],
    ),
    "ON_NOEXEC chain": (
        "\n".join(_cmd(f"q{i}", f"s(q{i - 1})" if i else "") for i in range(_DEEP)),
        [ev("ON_NOEXEC", 0, job=f"q{i}") for i in range(1, _DEEP)]
        + [ev("FORCE_STARTJOB", 1, job="q0"), ev("STATUS", 2, job="q0", exit_code=0)],
    ),
}


@pytest.mark.parametrize("shape", sorted(_DEEP_SHAPES))
def test_sem10_a_deep_legal_cascade_runs_on_the_chart_oracle_at_its_fit(shape: str) -> None:
    """At the limit fitted for the chart oracle (its own
    `FRAMES_PER_NESTED_START`), the deepest legal shapes run to the end
    without a cascade error and write what the old oracle writes."""
    jil, script = _DEEP_SHAPES[shape]
    limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        fit_recursion_limit(_DEEP, ChartOracle)
        paired = _lockstep(jil, script)
    finally:
        sys.setrecursionlimit(limit)
    assert {row.status for row in paired.twin.store.job.values()} == {"SUCCESS"}
