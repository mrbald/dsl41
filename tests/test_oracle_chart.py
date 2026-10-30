"""The oracle's job rows on the `job` chart (DL-341): its job instances on
the state owner's executor, and each input family through the chart.

Each case runs a script on the oracle and pins what the case is about: the
rows the chart fires, and the trace lines, the rows or the timers they leave;
every instance agrees with its row after each input (`_agrees`). The goldens hold every input of the
sample to the answers recorded before DL-351 (tests/oracle_goldens.py). The
cascade on the executor's worklist, its hook, its moments and its bound are
tests/test_oracle_chart_cascade.py's (DL-350)."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import oracle_inputs
import pytest
from estate_gen import SealPoint
from oracle_inputs import EPOCH, observations

from dsl41.capacity import CapacityPool
from dsl41.ir import lower_source
from dsl41.job_chart import JOB
from dsl41.job_chart_events import JobData, Release
from dsl41.job_chart_rows import configuration, row_configuration
from dsl41.oracle import Oracle
from dsl41.oracle_cascade import OracleCascade
from dsl41.oracle_facts import DoorFacts, RowUnits, StartFacts, job_data
from dsl41.oracle_schedule import OracleSchedule
from dsl41.oracle_state import (
    CapacityReservation,
    CarriedRows,
    Event,
    EventKind,
    JobRuntime,
    OracleError,
    ReleasePolicy,
    RuntimeState,
)
from dsl41.runner_hosts import HostCommand, host_move
from dsl41.semantics import resolve
from dsl41.state_machine import (
    COMPLETION,
    HITS_ENV,
    Executor,
    Outcome,
    Statechart,
    StatechartError,
)

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
    an input this pins the commit's check and that no row lacks an instance;
    right after a fork or a construction it pins the copy and the restore."""
    for job, row in oracle.store.job.items():
        assert oracle.store.machines.active(RuntimeState.job_key(job)) == row_configuration(row)


def _driven(jil: str, script: list[Event]) -> Oracle:
    """The oracle over `jil`, fed `script`, every instance on its row."""
    oracle = Oracle(lower_source(jil))
    for event in script:
        oracle.feed(event)
        _agrees(oracle)
    return oracle


# ---- the STATUS family


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


#: Each STATUS case's end: every row's (status, run number, exit code, held units),
#: the consumed units, and the whole trace as `job transition` lines. Pinned from the
#: oracle's output, which the goldens hold equal to the answers recorded before DL-351.
_CASE_ENDS: dict[str, tuple[dict[str, tuple[Any, ...]], dict[str, int], list[str]]] = {
    "INACTIVE on a job, a member and a box": (
        {
            "b": ("INACTIVE", 1, 1, ()),
            "j": ("INACTIVE", 1, None, (("r:FREE", 1),)),
            "k": ("INACTIVE", 0, None, ()),
            "m": ("INACTIVE", 1, None, ()),
            "n": ("INACTIVE", 0, None, ()),
        },
        {},
        [
            "j INACTIVE->STARTING",
            "j STARTING->RUNNING",
            "j RUNNING->INACTIVE",
            "b INACTIVE->STARTING",
            "b STARTING->RUNNING",
            "n ON_HOLD",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
            "m RUNNING->INACTIVE",
            "b RUNNING->INACTIVE",
        ],
    ),
    "a box restarted by its own completion (free)": (
        {
            "ob": ("RUNNING", 2, None, ()),
            "om": ("RUNNING", 2, None, (("r:Q", 1),)),
        },
        {},
        [
            "ob INACTIVE->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
            "om RUNNING->SUCCESS",
            "ob RUNNING->SUCCESS",
            "om SUCCESS->INACTIVE",
            "ob SUCCESS->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
        ],
    ),
    "a box restarted by its own completion (keep)": (
        {
            "ob": ("RUNNING", 2, None, ()),
            "om": ("RUNNING", 2, None, (("r:Q", 1),)),
        },
        {},
        [
            "ob INACTIVE->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
            "om RUNNING->SUCCESS",
            "ob RUNNING->SUCCESS",
            "om SUCCESS->INACTIVE",
            "ob SUCCESS->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
        ],
    ),
    "a box restarted by its own completion (spend)": (
        {
            "ob": ("RUNNING", 2, None, ()),
            "om": ("RUNNING", 2, None, (("r:Q", 1),)),
        },
        {"r:Q": 1},
        [
            "ob INACTIVE->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
            "om RUNNING->SUCCESS",
            "ob RUNNING->SUCCESS",
            "om SUCCESS->INACTIVE",
            "ob SUCCESS->STARTING",
            "ob STARTING->RUNNING",
            "om INACTIVE->STARTING",
            "om STARTING->RUNNING",
        ],
    ),
    "an idle box restarted by its member's end (D, after a failure)": (
        {
            "b": ("RUNNING", 2, None, ()),
            "m": ("QUE_WAIT", 1, None, ()),
        },
        {"r:R": 1},
        [
            "b INACTIVE->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
            "m RUNNING->FAILURE",
            "b RUNNING->FAILURE",
            "m FAILURE->SUCCESS",
            "b FAILURE->SUCCESS",
            "m SUCCESS->INACTIVE",
            "b SUCCESS->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->QUE_WAIT",
        ],
    ),
    "an idle box restarted by its member's end (D, never run)": (
        {
            "b": ("RUNNING", 1, None, ()),
            "m": ("RUNNING", 1, None, (("r:R", 1),)),
        },
        {},
        [
            "m INACTIVE->SUCCESS",
            "b INACTIVE->SUCCESS",
            "m SUCCESS->INACTIVE",
            "b SUCCESS->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
        ],
    ),
    "an idle box restarted by its member's end (R, after a failure)": (
        {
            "b": ("RUNNING", 2, None, ()),
            "m": ("RUNNING", 2, None, (("r:R", 1),)),
        },
        {},
        [
            "b INACTIVE->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
            "m RUNNING->FAILURE",
            "b RUNNING->FAILURE",
            "m FAILURE->SUCCESS",
            "b FAILURE->SUCCESS",
            "m SUCCESS->INACTIVE",
            "b SUCCESS->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
        ],
    ),
    "an idle box restarted by its member's end (R, never run)": (
        {
            "b": ("RUNNING", 1, None, ()),
            "m": ("RUNNING", 1, None, (("r:R", 1),)),
        },
        {},
        [
            "m INACTIVE->SUCCESS",
            "b INACTIVE->SUCCESS",
            "m SUCCESS->INACTIVE",
            "b SUCCESS->STARTING",
            "b STARTING->RUNNING",
            "m INACTIVE->STARTING",
            "m STARTING->RUNNING",
        ],
    ),
    "exit codes": (
        {
            "b": ("INACTIVE", 0, None, ()),
            "j": ("FAILURE", 2, 3, (("r:FREE", 1),)),
            "k": ("INACTIVE", 0, None, ()),
            "m": ("INACTIVE", 0, None, ()),
            "n": ("INACTIVE", 0, None, ()),
            "nowhere": ("SUCCESS", 0, 0, ()),
        },
        {},
        [
            "j INACTIVE->STARTING",
            "j STARTING->RUNNING",
            "j RUNNING->SUCCESS",
            "j SUCCESS->STARTING",
            "j STARTING->RUNNING",
            "j RUNNING->FAILURE",
            "nowhere INACTIVE->SUCCESS",
        ],
    ),
    "injected statuses": (
        {
            "b": ("INACTIVE", 0, None, ()),
            "j": ("TERMINATED", 0, 0, ()),
            "k": ("INACTIVE", 0, None, ()),
            "m": ("INACTIVE", 0, None, ()),
            "n": ("INACTIVE", 0, None, ()),
            "nowhere": ("INACTIVE", 0, None, ()),
        },
        {},
        [
            "j INACTIVE->STARTING",
            "j STARTING->RUNNING",
            "j RUNNING->SUCCESS",
            "j SUCCESS->FAILURE",
            "j FAILURE->TERMINATED",
            "nowhere INACTIVE->INACTIVE",
        ],
    ),
    "units held, then the run's again": (
        {
            "b": ("INACTIVE", 0, None, ()),
            "j": ("INACTIVE", 0, None, ()),
            "k": ("INACTIVE", 1, None, (("r:KEPT", 1),)),
            "m": ("INACTIVE", 0, None, ()),
            "n": ("INACTIVE", 0, None, ()),
        },
        {},
        [
            "k INACTIVE->STARTING",
            "k STARTING->RUNNING",
            "k RUNNING->FAILURE",
            "k FAILURE->RUNNING",
            "k RUNNING->TERMINATED",
            "k TERMINATED->INACTIVE",
        ],
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_sem09_status_cases_end_in_their_rows_and_trace(case: str) -> None:
    """Each STATUS row and branch, the settlement's three policies, the
    relabelling of held units and a box restarted by its own completion:
    every instance agrees with its row after each input, and the case ends
    in its pinned rows, consumed units and trace (`_CASE_ENDS`)."""
    jil, script = _CASES[case]
    oracle = _driven(jil, script)
    rows, consumed, trace = _CASE_ENDS[case]
    assert {
        job: (
            r.status,
            r.run_number,
            r.exit_code,
            tuple((h.bucket, h.units) for h in r.reservations),
        )
        for job, r in sorted(oracle.store.job.items())
    } == rows
    assert dict(oracle.store.consumed) == consumed
    assert [f"{t.job} {t.transition}" for t in oracle.trace()] == trace


def test_a_terminator_loop_ends_at_the_box_restart() -> None:
    """A box that restarts on its own TERMINATED, and a member that is its
    box_terminator and a job_terminator: a failed member ends the box, whose
    end starts it again. Until DL-348 the job_terminator cascade then
    killed the new run's member, which ended the box again, until the
    cascade passed the recursion limit. Now the cascade reaches no run the
    box started again, at Python's default limit."""
    jil = (
        "insert_job: tb\njob_type: b\ncondition: t(tb)\n\n"
        "insert_job: tm\njob_type: c\ncommand: x\nmachine: m1\nbox_name: tb\n"
        "box_terminator: 1\njob_terminator: 1\n"
    )
    limit = sys.getrecursionlimit()
    try:
        paired = Oracle(lower_source(jil))
        sys.setrecursionlimit(1000)
        for event in (ev("FORCE_STARTJOB", 1, job="tb"), ev("STATUS", 2, job="tm", exit_code=2)):
            paired.feed(event)
            _agrees(paired)
    finally:
        sys.setrecursionlimit(limit)
    rows = paired.store.job
    assert [(rows[job].status, rows[job].run_number) for job in ("tb", "tm")] == [
        ("RUNNING", 2),
        ("RUNNING", 2),
    ]


def test_sem09_a_payload_the_oracle_refuses_is_refused_before_the_chart() -> None:
    oracle = Oracle(lower_source(_JIL))
    with pytest.raises(OracleError, match="STATUS requires payload.status or integer"):
        oracle.feed(ev("STATUS", 1, job="j", exit_code="0"))
    with pytest.raises(OracleError, match="unknown status 'QUE_WAIT'"):
        oracle.feed(ev("STATUS", 2, job="j", status="QUE_WAIT"))
    with pytest.raises(OracleError, match="unknown status 3"):
        oracle.feed(ev("STATUS", 3, job="j", status=3))


# ---- the agreement check


def _swapped(chart: Statechart, a: str, b: str) -> Statechart:
    return replace(chart, guards={**chart.guards, a: chart.guards[b], b: chart.guards[a]})


@pytest.mark.parametrize(("job", "row"), [("b", "job.120"), ("j", "job.135")])
def test_an_inactive_row_for_the_other_job_type_is_a_statechart_error(job: str, row: str) -> None:
    """Planted: the job's and the box's INACTIVE guards swapped. The row the
    chart fires decides the move, so the wrong one cannot pass as the path
    the operator's INACTIVE would take for the catalog's job type."""

    class Swapped(Oracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return _swapped(chart, "injects_inactive_job", "injects_inactive_box"), active, data

    oracle = Swapped(lower_source(_JIL))
    with pytest.raises(StatechartError, match=f"job '{job}': {row} fired for a"):
        oracle.feed(ev("STATUS", 1, job=job, status="INACTIVE"))


def test_a_macrostep_that_raises_sets_every_cascade_field_back_to_between_inputs() -> None:
    """`Oracle._recover` (DL-356): a macrostep raises with every field the
    cascade keeps for one input set, the owed scan included; after the fault
    each field holds what a fresh oracle's does, and the instances agree with
    the rows."""
    catalog = lower_source(_JIL)
    fresh = OracleCascade(catalog)
    fields = set(vars(fresh)) - set(vars(OracleSchedule(catalog)))
    assert "_scan_owed" in fields and len(fields) == 4

    class Faulting(Oracle):
        def _perform(self, outcome: Outcome) -> None:
            super()._perform(outcome)
            for name in fields:
                setattr(self, name, object())
            raise RuntimeError("planted")

    oracle = Faulting(catalog)
    with pytest.raises(RuntimeError, match="planted"):
        oracle.feed(ev("STARTJOB", 1, job="j"))
    assert {name: getattr(oracle, name) for name in fields} == {
        name: getattr(fresh, name) for name in fields
    }
    _agrees(oracle)


# ---- where the instances are


def test_a_bare_store_keeps_no_job_instances() -> None:
    store = RuntimeState()
    store.runtime("j")
    assert store.job_instance is None
    with pytest.raises(StatechartError, match="no instance 'job:j'"):
        store.machines.active("job:j")


def test_every_row_has_its_instance_after_construction_an_input_and_a_read() -> None:
    """The constructor restores one instance per row; an input that moves rows
    outside the chart (a start) is followed at its commit; a row read into
    existence gets its instance as it is created."""
    oracle = Oracle(lower_source(_JIL))
    _agrees(oracle)
    assert oracle.store.machines.data("job:b") == JobData(box=True)
    assert oracle.store.machines.data("job:m") == JobData(job_terminator=True)
    assert oracle.store.machines.data("job:n") == JobData(auto_hold=True)
    oracle.feed(ev("STARTJOB", 1, job="k"))
    assert oracle.store.machines.active("job:k") == configuration("RUNNING", holding="reserved")
    assert oracle.store.machines.data("job:k") == JobData()  # the units are the run's, not data
    oracle.store.runtime("ghost")
    assert oracle.store.machines.data("job:ghost") == JobData(catalog=False)
    _agrees(oracle)


def test_a_fork_copies_the_instances_and_its_store_asks_the_fork() -> None:
    """The cheap invariant at a fork: the twin holds every instance, agreeing
    with its rows, its owner asks the twin for job data, and a STATUS on the
    twin leaves the parent's instance where it was."""
    oracle = Oracle(lower_source(_JIL))
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
    first = Oracle(lower_source(_JIL))
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
    second = Oracle(lower_source(_JIL.split("insert_job: b")[0]), carried=carried)
    _agrees(second)
    assert second.store.machines.active("job:k") == configuration("FAILURE", holding="held")
    assert second.store.machines.data("job:m") == JobData(catalog=False)
    second.store.begin_input()
    second.store.register_host("h")
    second.store.witness_host("h", state="passive")
    second.store.commit_input()
    _agrees(second)
    assert second.store.machines.data("job:k") == JobData()
    assert RowUnits(second, "k").release(False) == "keep"


def test_a_commit_follows_only_jobs_that_have_a_row() -> None:
    """A timer alone touches its job's key. Armed and fired in one input, it
    moves no revision, and the commit creates no row for the job, as the old
    oracle's does not."""
    oracle = Oracle(lower_source(_JIL))
    store = oracle.store
    store.begin_input()
    store.enqueue_timer(T0, ev("TIMER", job="rowless", check="must_start"))
    assert store.pop_timer_due(T0) is not None
    assert store.commit_input() == []
    assert "rowless" not in store.job
    with pytest.raises(StatechartError, match="no instance 'job:rowless'"):
        store.machines.active("job:rowless")


# ---- the data and the units


def test_row_units_read_the_policy_of_each_unit_for_both_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`release_reservations`' rules: a `completion` unit is freed by every
    end and a `success` unit by SUCCESS; what is not freed is kept on a
    renewable and spent on a depletable, and one kept unit makes the end keep.
    The handle reads the row when asked (DL-349)."""
    oracle = Oracle(
        lower_source(
            "insert_resource: R\nres_type: R\namount: 2\n\n"
            "insert_resource: D\nres_type: D\namount: 2\n\n"
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        )
    )

    handle = RowUnits(oracle, "j")
    assert repr(handle) == "RowUnits('j')"

    def data(*units: tuple[str, ReleasePolicy]) -> tuple[str, str]:
        row = JobRuntime(
            reservations=tuple(
                CapacityReservation(bucket=bucket, units=1, release_policy=policy)
                for bucket, policy in units
            )
        )
        monkeypatch.setattr(oracle, "_runtime", lambda job: row)
        assert job_data(oracle, "j") == JobData()
        return handle.release(True), handle.release(False)

    assert data() == ("free", "free")
    assert data(("m:m1", "completion")) == ("free", "free")
    assert data(("r:R", "success")) == ("free", "keep")
    assert data(("r:D", "success")) == ("free", "spend")
    assert data(("r:D", "never")) == ("spend", "spend")
    assert data(("r:D", "never"), ("r:R", "never")) == ("keep", "keep")


def test_the_host_gate_reads_the_store_s_instance_and_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads the host chart with `Executor.resolve` on the store's
    own instance (DL-375): it builds no executor and moves nothing, so a
    host command's cost does not grow with the job count."""
    oracle = Oracle(lower_source(_JIL))
    store = oracle.store
    store.begin_input()
    store.register_host("h")
    store.commit_input()
    machines = store.machines

    def rebuild() -> Executor:
        raise AssertionError("the gate rebuilt an executor")

    monkeypatch.setattr(store, "rebuild_machines", rebuild)
    outcome = host_move(store, HostCommand(verb="drain", host_id="h"))
    assert (outcome.kind, outcome.rows) == ("fired", ("host.02",))
    assert store.machines is machines
    assert machines.active("host:h") == {"host", "routed", "active"}
    assert machines.active("job:j") == configuration("INACTIVE")


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
#: `start` items for the job, and a trace line the oracle writes for it.
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
        [("job.441",)],
        "START_REFUSED",
    ),
    "refused: the box is not running": (
        [ev("STARTJOB", 1, job="m")],
        "m",
        ("job.177",),
        [("job.441",)],
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
        [("job.441",)],
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
        [("job.441",)],
        "START_REFUSED",
    ),
    "nothing": ([ev("STARTJOB", 1, job="cond")], "cond", ("job.177",), [("job.180",)], ""),
    "arm": (
        [ev("ON_HOLD", 1, job="dated"), ev("STARTJOB", 2, job="dated")],
        "dated",
        ("job.177",),
        [("job.429",)],
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
        [("job.404",)],
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


#: The status a case's last attempt meets, where its row is one row for every status of a set
#: (DL-368): the status its name claims, read before the last input.
_OUTCOME_STATUS = {
    "refused: live": "RUNNING",
    "refused: the box is not running": "INACTIVE",
    "refused: ran in this box run": "SUCCESS",
    "refused: taken off ice in this box run": "INACTIVE",
}


def _spied(oracle: Oracle) -> list[Outcome]:
    """Every outcome the oracle's hook writes from now on, in order."""
    seen: list[Outcome] = []
    perform = oracle._perform  # type: ignore[attr-defined]

    def spy(outcome: Outcome) -> None:
        seen.append(outcome)
        perform(outcome)

    oracle._perform = spy  # type: ignore[attr-defined]
    return seen


@pytest.mark.parametrize("case", sorted(_OUTCOMES))
def test_sem10_each_start_outcome_fires_its_row_and_writes_its_line(
    case: str,
) -> None:
    """Every attempt is one `start` on the job's instance, an input's through
    its relay: the row the chart fires is the outcome the rule reaches, its
    line is written, and every instance agrees with its row."""
    script, job, relay, starts, line = _OUTCOMES[case]
    paired = Oracle(lower_source(_START_JIL))
    for event in script[:-1]:
        paired.feed(event)
    if case in _OUTCOME_STATUS:
        assert paired.store.job[job].status == _OUTCOME_STATUS[case]
    seen = _spied(paired)
    traced = len(paired.trace())
    paired.feed(script[-1])
    _agrees(paired)
    mine = [o for o in seen if o.key == RuntimeState.job_key(job)]
    relayed = [o.rows for o in mine if o.event.name in ("STARTJOB", "FORCE_STARTJOB")]
    assert relayed == ([relay] if relay else [])
    assert [o.rows for o in mine if o.event.name == "start"] == starts
    lines = [t.transition for t in paired.trace()[traced:] if t.job == job]
    assert (line in lines) if line else lines == []
    causes = [t.cause for t in paired.trace()[traced:] if t.job == job]
    if "member's continuation" in case:  # box-stale wins over superseded
        assert causes[-1].startswith("deferred start belongs to box 'kb' run 1")
    elif "continuation after" in case:
        assert causes[-1].startswith("the job has moved on since the scan stopped")
    if starts[-1] in (("job.102",), ("job.124",), ("job.104",)):
        started = [o.rows for o in mine if o.event.name == "started"]
        assert started == [("job.124",) == starts[-1] and ("job.125",) or ("job.105",)]


def test_sem33_a_postponed_start_replays_through_the_window_relay() -> None:
    """A run_window-deferred start's timer replays the attempt through its
    own relay (job.179)."""
    paired = Oracle(lower_source(_START_JIL))
    paired.feed(ev("STARTJOB", 1, job="wdefer"))
    seen = _spied(paired)
    paired.advance(T0 + timedelta(hours=2, minutes=1))
    rows = [(o.event.name, o.rows) for o in seen if o.key == "job:wdefer"]
    assert rows[:2] == [("TIMER run_window", ("job.179",)), ("start", ("job.102",))]
    assert paired.store.job["wdefer"].status == "RUNNING"


def test_an_attempt_on_a_job_outside_the_catalog_moves_nothing() -> None:
    """With no row there is no instance and nothing to dispatch; with a row
    the chart ignores the job (job.180)."""
    paired = Oracle(lower_source(_START_JIL))
    seen = _spied(paired)
    paired.feed(ev("STARTJOB", 1, job="ghost"))
    assert "ghost" not in paired.store.job
    assert [o for o in seen if o.key == "job:ghost"] == []
    paired.feed(ev("STATUS", 2, job="ghost", status="SUCCESS"))
    paired.feed(ev("FORCE_STARTJOB", 3, job="ghost"))
    rows = [o.rows for o in seen if o.key == "job:ghost" and o.event.name == "start"]
    assert rows == [("job.180",)]


def test_dl256_a_restart_takes_the_new_resource_and_wakes_the_queue() -> None:
    """A restart that no longer asks for the resource its job holds takes
    the new one and gives the old back, which wakes the queue; a start that
    holds a machine's load owes the admit-only scan, its `load taken` mark
    (DL-255)."""
    text = (
        "insert_machine: box1\ntype: a\nnode_name: box1\nmax_load: 2\n\n"
        "insert_resource: OLD\nres_type: R\namount: 1\n\n"
        "insert_resource: NEW\nres_type: R\namount: 1\n\n"
        "insert_job: ha\njob_type: c\ncommand: x\nmachine: m1\nresources: (OLD, QUANTITY=1)\n\n"
        "insert_job: wa\njob_type: c\ncommand: y\nmachine: m1\nresources: (OLD, QUANTITY=1)\n\n"
        "insert_job: ml\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n"
    )
    oracle = Oracle(lower_source(text))
    for event in (
        ev("STARTJOB", 0, job="ha"),
        ev("STATUS", 1, job="ha", status="FAILURE"),
        ev("STARTJOB", 2, job="wa"),
    ):
        oracle.feed(event)
    seen = _spied(oracle)
    oracle.feed(ev("STARTJOB", 2, job="ml"))
    assert [str(o.key) for o in seen if o.event.name == "load taken"] == ["job:ml"]
    assert oracle.store.job["wa"].status == "QUE_WAIT"
    moved = lower_source(
        text.replace("(OLD, QUANTITY=1)\n\ninsert_job: wa", "(NEW, QUANTITY=1)\n\ninsert_job: wa")
    )
    oracle.catalog = moved
    oracle._pool = CapacityPool(moved)
    oracle.feed(ev("STARTJOB", 3, job="ha"))
    _agrees(oracle)
    assert oracle.store.job["wa"].status == "RUNNING"
    assert [(h.bucket, h.units) for h in oracle.store.job["ha"].reservations] == [("r:NEW", 1)]


# ---- the facts are lazy


def test_sem20_a_fact_the_rule_never_reads_is_never_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An iced job's condition is not evaluated, nor the run window of a job
    whose condition is false: the payload computes a fact on its first read,
    in `start_verdict`'s order. Read later, it is computed then."""
    oracle = Oracle(lower_source(_START_JIL))
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


def _planted(**guards: Callable[..., bool]) -> type[Oracle]:
    class Planted(Oracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return replace(chart, guards={**chart.guards, **guards}), active, data

    return Planted


_JOB_TYPE_PAIRS = {
    "admission": (
        ("start_admits_job", "start_admits_box"),
        [ev("STARTJOB", 1, job="plain")],
        "job.124 fired for a non-box job",
    ),
    "skip": (
        ("start_skips", "start_skips_member"),
        [ev("STATUS", 1, job="wskip", status="SUCCESS"), ev("STARTJOB", 2, job="wskip")],
        "job.107 fired for a job in box None",
    ),
    "started": (
        ("started_job", "started_box"),
        [ev("STARTJOB", 1, job="plain")],
        "job.125 fired for a non-box job",
    ),
}


@pytest.mark.parametrize("pair", sorted(_JOB_TYPE_PAIRS))
def test_a_start_row_for_the_other_job_type_is_a_statechart_error(pair: str) -> None:
    """Planted: the guards of a row pair that differs only by the job type
    swapped (job.102/124, job.106/107, job.105/125). The fired row decides
    the move, and a catalog that disagrees with it is caught at once, as for
    job.120 and job.135."""
    (a, b), script, message = _JOB_TYPE_PAIRS[pair]
    guards = JOB.guards
    oracle = _planted(**{a: guards[b], b: guards[a]})(lower_source(_START_JIL))
    for event in script[:-1]:
        oracle.feed(event)
    with pytest.raises(StatechartError, match=message):
        oracle.feed(script[-1])


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
    the late settlement runs after the box rules, so a sibling the
    restart attempts first finds the unit still taken and queues, to be
    admitted when the settlement frees it (the owed settlement is read past
    the gates, job.401; settling whenever one is owed would start the
    sibling at once)."""
    jil, script, statuses = _SETTLEMENTS[case]
    paired = _driven(jil, script)
    assert {job: paired.store.job[job].status for job in statuses} == statuses
    if "rs" in statuses:
        assert "INACTIVE->QUE_WAIT" in [t.transition for t in paired.trace() if t.job == "rs"]


_OWED_SCRIPT = [
    ev("FORCE_STARTJOB", 1, job="rb"),
    ev("FORCE_STARTJOB", 2, job="rm"),
    ev("STATUS", 3, job="rm", exit_code=0),
]


def test_sem16_an_attempt_that_skips_the_owed_settlement_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutant: the owed settlement is never read, so the attempt never takes
    job.401 and its admission is read with the ended run's units still on
    the row, where only job.312, marked unreachable, settles them. The
    mutant parts from the oracle at the input, for both verdicts. The
    mutant's hits stay out of the session."""
    for resource in ("R", "D"):
        catalog = lower_source(_member_restart_jil(resource, ""))
        expected = observations(Oracle(catalog), _OWED_SCRIPT)
        with monkeypatch.context() as patch:
            patch.setenv(HITS_ENV, str(tmp_path))
            patch.setattr(StartFacts, "settlement_owed", property(lambda self: False))
            found = observations(Oracle(catalog), _OWED_SCRIPT)
        assert found[:-1] == expected[:-1]
        assert found[-1] != expected[-1]


_POLICY_JIL = (
    "insert_resource: FR\nres_type: R\namount: 1\n\n"
    "insert_resource: KR\nres_type: R\namount: 1\n\n"
    "insert_resource: SD\nres_type: D\namount: 3\n\n"
    "insert_job: free\njob_type: c\ncommand: x\nmachine: m1\nresources: (FR, QUANTITY=1, FREE=A)\n\n"
    "insert_job: keep\njob_type: c\ncommand: x\nmachine: m1\nresources: (KR, QUANTITY=1, FREE=N)\n\n"
    "insert_job: spend\njob_type: c\ncommand: x\nmachine: m1\nresources: (SD, QUANTITY=1)\n\n"
    "insert_job: pb\njob_type: b\n\n"
    "insert_job: pfree\njob_type: c\ncommand: x\nmachine: m1\nbox_name: pb\n"
    "resources: (FR, QUANTITY=1, FREE=A)\n\n"
    "insert_job: pkeep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: pb\n"
    "resources: (KR, QUANTITY=1, FREE=N)\n\n"
    "insert_job: pspend\njob_type: c\ncommand: x\nmachine: m1\nbox_name: pb\n"
    "resources: (SD, QUANTITY=1)\n"
)
#: The sender of each settlement: a status move's late settlement, a kill's,
#: and a box's INACTIVE that takes a running member along (the batch path).
_SENDERS: dict[str, tuple[list[Event], Callable[[str], Event]]] = {
    "a status move": ([], lambda job: ev("STATUS", 2, job=job, exit_code=0)),
    "a kill": ([], lambda job: ev("KILLJOB", 2, job=job)),
    "the batch path": (
        [ev("FORCE_STARTJOB", 0, job="pb")],
        lambda job: ev("STATUS", 2, job="pb", status="INACTIVE"),
    ),
}
#: The holding row of each policy.
_POLICY_ROWS = {"free": "job.304", "keep": "job.305", "spend": "job.306"}


@pytest.mark.parametrize("sender", sorted(_SENDERS))
@pytest.mark.parametrize("policy", sorted(_POLICY_ROWS))
def test_dl256_a_settlement_reads_its_policy_from_the_row_when_it_pops(
    sender: str, policy: str
) -> None:
    """DL-349: the release policy is a fact of `leave STARTING or
    RUNNING`'s payload, built by its sender with the handle its own payload
    carries, and read when the settlement pops, from the units the row holds
    then: free (job.304), keep (job.305) and spend (job.306), each written by
    the store's verb. The job's data carries no policy."""
    setup, final = _SENDERS[sender]
    job = policy if sender != "the batch path" else f"p{policy}"
    script = setup if setup else [ev("STARTJOB", 1, job=job)]
    paired = Oracle(lower_source(_POLICY_JIL))
    for event in script:
        paired.feed(event)
    oracle = paired
    assert oracle.store.job[job].reservations
    machines = oracle.store.machines
    reads: list[str] = []
    real = RowUnits.release

    def read(self: RowUnits, success: bool) -> Release:
        current = machines._current
        assert current is not None
        reads.append(f"{current.key} {current.event.name}")
        return real(self, success)

    seen = _spied(oracle)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(RowUnits, "release", read)
        paired.feed(final(job))
    _agrees(oracle)
    settled = [
        o for o in seen if o.key == f"job:{job}" and o.event.name == "leave STARTING or RUNNING"
    ]
    assert [o.rows for o in settled] == [(_POLICY_ROWS[policy],)]
    assert settled[0].event.payload.release == policy
    # read once for the job, at its settlement's pop (the batch settles each member)
    assert reads.count(f"job:{job} leave STARTING or RUNNING") == 1
    assert all(entry.endswith(" leave STARTING or RUNNING") for entry in reads)
    assert oracle.store.machines.data(f"job:{job}") == JobData()


_MOVED = [("box rules", ("job.405",)), ("settle", ("job.406",)), ("wake", ("job.407",))]
#: The admitting verdicts and, after the attempt's `start` item, the items
#: its `start admitted` sends to the job itself, its children: (event, rows).
#: An admission's row enters ADMITTED, whose entry sends the units and the
#: run's start, and a box's resets to its members; ADMITTED's completion
#: (job.427) follows them (DL-364).
_EXTENTS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "admit a job": [
        ("admitted start", ("job.311",)),
        ("actual start", ("job.431",)),
        (COMPLETION, ("job.427",)),
    ],
    "admit a box": [
        ("admitted start", ("job.311",)),
        ("actual start", ("job.431",)),
        (COMPLETION, ("job.427",)),
    ],
    "queue": _MOVED,
    "bypass": [("actual start", ("job.431",)), *_MOVED],
    "forced on held units": [
        ("admitted start", ("job.303",)),
        ("actual start", ("job.431",)),
        (COMPLETION, ("job.427",)),
    ],
}
#: What ADMITTED's completion sends to the job itself, its children: the
#: STARTING move's box rules, settlement (the LIVE cell, job.418) and wakes,
#: a box's reset notices, then `started`.
_STARTING = [("box rules", ("job.405",)), ("settle", ("job.418",)), ("wake", ("job.407",))]
_STARTINGS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "admit a job": [*_STARTING, ("started", ("job.105",))],
    "admit a box": [*_STARTING, ("notify step", ("job.413",)), ("started", ("job.125",))],
    "forced on held units": [*_STARTING, ("started", ("job.105",))],
}
_VERDICTS = {
    "job.101": "bypass",
    "job.102": "admit",
    "job.103": "queue",
    "job.104": "on_held",
    "job.124": "admit",
}
#: The row of `start admitted` for each admitting verdict.
_EXTENT_ROWS = {"admit": "job.402", "bypass": "job.424", "queue": "job.425", "on_held": "job.426"}


@pytest.mark.parametrize("case", sorted(_EXTENTS))
def test_dl304_start_admitted_precedes_the_admissions_moves_for_every_admitting_verdict(
    case: str,
) -> None:
    """DL-349, DL-350: each admitting row (job.101 to job.104,
    job.124) sends `start admitted` first; its verdict's row (job.402,
    job.424 to job.426) sends what the admission does, so the admission's
    moves are its descendants and pop after it: the units, the arm, then
    ADMITTED's completion, which moves to STARTING and sends `started`
    (DL-364); a bypass's and a queue's status move, written at the attempt,
    has its box rules, its settlement and its wakes sent from there."""
    script, job, _relay, starts, _line = _OUTCOMES[case]
    paired = Oracle(lower_source(_START_JIL))
    for event in script[:-1]:
        paired.feed(event)
    seen = _spied(paired)
    paired.feed(script[-1])
    mine = [o for o in seen if o.key == RuntimeState.job_key(job)]
    at = next(i for i, o in enumerate(mine) if o.event.name == "start")
    assert mine[at].rows == starts[-1]
    admitted = mine[at + 1]
    verdict = _VERDICTS[starts[-1][0]]
    assert (admitted.event.name, admitted.rows) == ("start admitted", (_EXTENT_ROWS[verdict],))
    assert admitted.event.payload.verdict == verdict
    children = [o for o in mine[at + 2 :] if o.depth == admitted.depth + 1]
    assert [(o.event.name, o.rows) for o in children] == _EXTENTS[case]
    if case in _STARTINGS:
        after = mine[mine.index(children[-1]) + 1 :]
        end = next((i for i, o in enumerate(after) if o.depth <= admitted.depth + 1), len(after))
        grandchildren = [o for o in after[:end] if o.depth == admitted.depth + 2]
        assert [(o.event.name, o.rows) for o in grandchildren] == _STARTINGS[case]


#: An empty box under `condition: d(RB)` that holds R, queued behind `rh`,
#: which keeps R after its run (FREE=N) until RELEASE_RESOURCE.
_READMITTED_JIL = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: rh\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=N)\n\n"
    "insert_job: RB\njob_type: b\ncondition: d(RB)\nresources: (R, QUANTITY=1, FREE=Y)\n"
)


@pytest.mark.parametrize(
    ("script", "runs"),
    [
        (
            [
                ev("STARTJOB", 1, job="rh"),
                ev("STATUS", 2, job="rh", status="SUCCESS"),
                ev("FORCE_STARTJOB", 3, job="RB"),
                ev("RELEASE_RESOURCE", 4, job="rh"),
            ],
            3,
        ),
        ([ev("FORCE_STARTJOB", 1, job="RB")], 2),
    ],
    ids=["readmitted", "started directly"],
)
def test_dl304_a_readmitted_run_is_not_a_frame_of_the_start_bound(
    script: list[Event], runs: int
) -> None:
    """DL-304, DL-349: a readmission sends no `start admitted`, so a readmitted
    run is not counted by the start bound and a restart nested in it counts
    from zero. RB, readmitted by rh's release, runs three times and is
    refused once; started directly, it runs twice and is refused once. The
    readmission (job.108) sends no `start admitted`; each nested start does."""
    paired = _driven(_READMITTED_JIL, script[:-1]) if len(script) > 1 else None
    paired = paired or Oracle(lower_source(_READMITTED_JIL))
    seen = _spied(paired)
    paired.feed(script[-1])
    lines = [t.transition for t in paired.trace() if t.job == "RB"]
    assert sum(line.endswith("->STARTING") for line in lines) == runs
    assert lines.count("START_REFUSED") == 1
    mine = [o for o in seen if o.key == "job:RB"]
    readmits = [o.rows for o in mine if o.event.name == "readmit"]
    assert readmits == ([("job.108",)] if runs == 3 else [])
    admitted = [o.rows for o in mine if o.event.name == "start admitted"]
    assert len(admitted) == 2


def test_a_readmission_enters_admitted_and_its_completion_carries_the_readmit() -> None:
    """DL-364: a readmitted waiter enters ADMITTED with its `readmit`
    (job.108). The state's entry sends the units and the run's start; its
    completion carries the readmission's payload, so it takes the
    readmission's row (job.428), and moves the waiter to STARTING with the
    readmission's cause, then sends the move's moments and `started`."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: rh\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1, FREE=N)\n\n"
        "insert_job: w\njob_type: c\ncommand: y\nmachine: m1\nresources: (R, QUANTITY=1)\n"
    )
    setup = [
        ev("STARTJOB", 1, job="rh"),
        ev("STATUS", 2, job="rh", status="SUCCESS"),
        ev("STARTJOB", 3, job="w"),
    ]
    paired = _driven(jil, setup)
    assert paired.store.job["w"].status == "QUE_WAIT"
    seen = _spied(paired)
    paired.feed(ev("RELEASE_RESOURCE", 4, job="rh"))
    mine = [o for o in seen if o.key == RuntimeState.job_key("w")]
    at = next(i for i, o in enumerate(mine) if o.event.name == "readmit" and o.rows)
    readmit = mine[at]
    assert readmit.rows == ("job.108",)
    children = [o for o in mine[at + 1 :] if o.depth == readmit.depth + 1]
    assert [(o.event.name, o.rows) for o in children[:3]] == [
        ("admitted start", ("job.301",)),
        ("actual start", ("job.431",)),
        (COMPLETION, ("job.428",)),
    ]
    completion = children[2]
    assert completion.event.payload is readmit.event.payload
    after = mine[mine.index(completion) + 1 :]
    end = next((i for i, o in enumerate(after) if o.depth <= completion.depth), len(after))
    assert [(o.event.name, o.rows) for o in after[:end] if o.depth == completion.depth + 1] == [
        *_STARTING,
        ("started", ("job.105",)),
    ]
    starting = [t for t in paired.trace() if t.job == "w" and t.transition == "QUE_WAIT->STARTING"]
    assert [t.cause for t in starting] == ["resources freed (QUE_WAIT admitted, DL-50)"]


def _below(seen: list[Outcome], item: Outcome) -> list[tuple[str, str, tuple[str, ...]]]:
    """(job, event, rows) of the items `item` sent, in pop order."""
    after = seen[seen.index(item) + 1 :]
    end = next((i for i, o in enumerate(after) if o.depth <= item.depth), len(after))
    return [
        (str(o.key).partition(":")[2], o.event.name, o.rows)
        for o in after[:end]
        if o.depth == item.depth + 1
    ]


def test_a_readmitted_box_s_completion_sends_its_reset_notices() -> None:
    """DL-364: a box readmitted from QUE_WAIT (job.108) enters ADMITTED, whose
    entry resets its member m (job.122). Its completion takes the
    readmission's row (job.428): the move's moments, then the reset notices
    (`notify step`, m's box rules and wake), then `started` (job.125)."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: rh\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1, FREE=N)\n\n"
        "insert_job: W\njob_type: b\nresources: (R, QUANTITY=1)\n\n"
        "insert_job: m\njob_type: c\ncommand: y\nmachine: m1\nbox_name: W\n"
    )
    setup = [
        ev("STATUS", 0, job="m", status="SUCCESS"),
        ev("STARTJOB", 1, job="rh"),
        ev("STATUS", 2, job="rh", status="SUCCESS"),
        ev("STARTJOB", 3, job="W"),
    ]
    paired = _driven(jil, setup)
    assert (paired.store.job["W"].status, paired.store.job["m"].status) == ("QUE_WAIT", "SUCCESS")
    seen = _spied(paired)
    paired.feed(ev("RELEASE_RESOURCE", 4, job="rh"))
    readmit = next(o for o in seen if o.event.name == "readmit" and o.rows == ("job.108",))
    assert _below(seen, readmit) == [
        ("W", "admitted start", ("job.301",)),
        ("W", "actual start", ("job.431",)),
        ("m", "box start", ("job.122",)),
        ("W", COMPLETION, ("job.428",)),
    ]
    completion = next(o for o in seen if o.rows == ("job.428",))
    assert _below(seen, completion) == [
        ("W", "box rules", ("job.405",)),
        ("W", "settle", ("job.418",)),
        ("W", "wake", ("job.407",)),
        ("W", "notify step", ("job.413",)),
        ("W", "started", ("job.125",)),
    ]
    notices = next(o for o in seen if o.event.name == "notify step")
    assert _below(seen, notices)[:2] == [
        ("m", "box rules", ("job.405",)),
        ("m", "wake", ("job.407",)),
    ]
    moved = [(t.job, t.transition) for t in paired.trace() if "->" in t.transition]
    assert moved[-5:] == [
        ("m", "SUCCESS->INACTIVE"),
        ("W", "QUE_WAIT->STARTING"),
        ("W", "STARTING->RUNNING"),
        ("m", "INACTIVE->STARTING"),
        ("m", "STARTING->RUNNING"),
    ]


# ---- KILLJOB and the TIMER input (DL-343)

_KT_JIL = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: w\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=Y)\n"
    f"{_DAILY}\n"
    "insert_job: t\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
    "insert_resource: KEPT\nres_type: R\namount: 1\n\n"
    "insert_job: tk\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n"
    "resources: (KEPT, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: b\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\njob_terminator: 1\n\n"
    "insert_job: n\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b\n\n"
    "insert_job: bw\njob_type: b\n\n"
    f'insert_job: d\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bw\n{_DAILY}run_window: "10:00-11:00"\n\n'
    f'insert_job: wd\njob_type: c\ncommand: x\nmachine: m1\n{_DAILY}run_window: "10:00-11:00"\n'
)
#: The window timers' scan continuation (DL-257), from the harness's directed
#: estate: `kt_scan` queues behind `kt_hold` and leaves the queue for a scan
#: that finds no eligible day in its span, so a continuation waits at its end.
_SCAN_SETUP = [
    Event(at=EPOCH + timedelta(hours=8), kind="STARTJOB", payload={"job": "kt_hold"}),
    Event(at=EPOCH + timedelta(hours=8), kind="STARTJOB", payload={"job": "kt_scan"}),
    Event(
        at=EPOCH + timedelta(hours=8, minutes=1),
        kind="STATUS",
        payload={"job": "kt_hold", "status": "SUCCESS"},
    ),
]
_SCAN_BOUND = datetime(2002, 1, 2, 0, 1)


def _scan_ev(minutes: int, kind: EventKind, **payload: object) -> Event:
    return Event(at=EPOCH + timedelta(hours=8, minutes=minutes), kind=kind, payload=payload)


def _directed(name: str) -> list[Event | datetime]:
    """A script of the harness's directed kill and timer estate, its seal
    points left out and its time observations as instants."""
    script = oracle_inputs._kill_timer_scripts()[name]
    return [s if isinstance(s, Event) else s.at for s in script if not isinstance(s, SealPoint)]


_ITEMS = (
    "KILLJOB",
    "ice moved",
    "noexec moved",
    "TIMER term_run_time",
    "TIMER run_window",
    "start",
)
#: Per outcome class: the estate, the script (an Event is fed, a datetime
#: advanced to), the job, the rows its last step fires on the job's
#: instance for KILLJOB, the flag move, the TIMER events and `start` (the
#: stale deferral's box restarted, so a second timer replays after it), and
#: a trace line the oracle writes for the job at that step ("" for none).
_KT_OUTCOMES: dict[str, tuple[str, list[Event | datetime], str, list[tuple[str, ...]], str]] = {
    "kill a live job": (
        _KT_JIL,
        [ev("STARTJOB", 1, job="t"), ev("KILLJOB", 2, job="t")],
        "t",
        [("job.114",)],
        "RUNNING->TERMINATED",
    ),
    "kill a live box with a terminator": (
        _KT_JIL,
        [ev("FORCE_STARTJOB", 1, job="b"), ev("KILLJOB", 2, job="b")],
        "b",
        [("job.114",)],
        "RUNNING->TERMINATED",
    ),
    "kill a waiter": (
        _KT_JIL,
        [ev("STARTJOB", 1, job="h"), ev("STARTJOB", 2, job="w"), ev("KILLJOB", 3, job="w")],
        "w",
        [("job.437",)],
        "QUE_WAIT->TERMINATED",
    ),
    "kill an armed waiter": (
        _KT_JIL,
        [
            ev("STARTJOB", 1, job="h"),
            ev("ON_HOLD", 2, job="w"),
            ev("STARTJOB", 2, job="w"),
            ev("OFF_HOLD", 3, job="w"),
            ev("KILLJOB", 4, job="w"),
        ],
        "w",
        [("job.436",)],
        "QUE_WAIT->TERMINATED",
    ),
    "kill ignored: INACTIVE": (
        _KT_JIL,
        [ev("KILLJOB", 1, job="t")],
        "t",
        [("job.440",)],
        "EVENT_IGNORED",
    ),
    "kill ignored: SUCCESS": (
        _KT_JIL,
        [
            ev("STARTJOB", 1, job="t"),
            ev("STATUS", 2, job="t", status="SUCCESS"),
            ev("KILLJOB", 3, job="t"),
        ],
        "t",
        [("job.440",)],
        "EVENT_IGNORED",
    ),
    "kill ignored: FAILURE": (
        _KT_JIL,
        [
            ev("STARTJOB", 1, job="t"),
            ev("STATUS", 2, job="t", exit_code=1),
            ev("KILLJOB", 3, job="t"),
        ],
        "t",
        [("job.440",)],
        "EVENT_IGNORED",
    ),
    "kill ignored: TERMINATED": (
        _KT_JIL,
        [ev("STARTJOB", 1, job="t"), ev("KILLJOB", 2, job="t"), ev("KILLJOB", 3, job="t")],
        "t",
        [("job.440",)],
        "EVENT_IGNORED",
    ),
    "term_run_time on its run": (
        _KT_JIL,
        [ev("STARTJOB", 1, job="t"), T0 + timedelta(minutes=7)],
        "t",
        [("job.115",)],
        "RUNNING->TERMINATED",
    ),
    "term_run_time of an earlier run": (
        _KT_JIL,
        [
            ev("STARTJOB", 1, job="t"),
            ev("STATUS", 2, job="t", status="SUCCESS"),
            ev("STARTJOB", 3, job="t"),
            T0 + timedelta(minutes=7),
        ],
        "t",
        [("job.184",)],
        "",
    ),
    "term_run_time after its run ended": (
        _KT_JIL,
        [
            ev("STARTJOB", 1, job="t"),
            ev("STATUS", 2, job="t", status="SUCCESS"),
            T0 + timedelta(minutes=7),
        ],
        "t",
        [("job.184",)],
        "",
    ),
    "a deferral replayed": (
        _KT_JIL,
        [ev("STARTJOB", 1, job="wd"), T0 + timedelta(hours=2, minutes=1)],
        "wd",
        [("job.179",), ("job.102",)],
        "INACTIVE->STARTING",
    ),
    "a deferral that no longer belongs to its box run": (
        _KT_JIL,
        [
            ev("FORCE_STARTJOB", 1, job="bw"),
            ev("KILLJOB", 2, job="bw"),
            ev("FORCE_STARTJOB", 3, job="bw"),
            T0 + timedelta(hours=2, minutes=1),
        ],
        "d",
        [("job.179",), ("job.441",), ("job.179",), ("job.102",)],
        "START_REFUSED",
    ),
    "a scan continuation resumes": (
        oracle_inputs.KILL_TIMER_JIL,
        [*_SCAN_SETUP, _SCAN_BOUND],
        "kt_scan",
        [("job.179",), ("job.180",)],
        "RUN_WINDOW_DEFER",
    ),
    "a scan continuation after the job started": (
        oracle_inputs.KILL_TIMER_JIL,
        [
            *_SCAN_SETUP,
            _scan_ev(2, "FORCE_STARTJOB", job="kt_scan"),
            _scan_ev(3, "STATUS", job="kt_scan", status="SUCCESS"),
            _SCAN_BOUND,
        ],
        "kt_scan",
        [("job.179",), ("job.441",)],
        "START_REFUSED",
    ),
    "a scan continuation after the job moved on": (
        oracle_inputs.KILL_TIMER_JIL,
        [*_SCAN_SETUP, _scan_ev(2, "STATUS", job="kt_scan", status="FAILURE"), _SCAN_BOUND],
        "kt_scan",
        [("job.179",), ("job.441",)],
        "START_REFUSED",
    ),
    "kill a queued box": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("queued box killed"),
        "kt_box",
        [("job.437",)],
        "QUE_WAIT->TERMINATED",
    ),
    "kill an armed queued box": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("armed queued box killed"),
        "kt_box",
        [("job.436",)],
        "QUE_WAIT->TERMINATED",
    ),
    "kill a running box that holds units": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("running box with units killed"),
        "kt_box",
        [("job.114",)],
        "RUNNING->TERMINATED",
    ),
    "term_run_time on a running box that holds units": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("running box with units past term_run_time"),
        "kt_box",
        [("job.115",)],
        "RUNNING->TERMINATED",
    ),
    "a scan continuation after the job ran and is INACTIVE again": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("rescan after a run, INACTIVE again"),
        "kt_scan",
        [("job.179",), ("job.441",)],
        "START_REFUSED",
    ),
    "a member's continuation after its box restarted and it ran": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("member rescan, box restarted and member ran"),
        "kt_mem",
        [("job.179",), ("job.441",)],
        "START_REFUSED",
    ),
    "a member's continuation after its box restarted": (
        oracle_inputs.KILL_TIMER_JIL,
        _directed("member rescan, box restarted"),
        "kt_mem",
        [("job.179",), ("job.441",)],
        "START_REFUSED",
    ),
}


#: As `_OUTCOME_STATUS`, for the kill and timer cases.
_KT_STATUS = {
    "kill ignored: INACTIVE": "INACTIVE",
    "kill ignored: SUCCESS": "SUCCESS",
    "kill ignored: FAILURE": "FAILURE",
    "kill ignored: TERMINATED": "TERMINATED",
    "a deferral that no longer belongs to its box run": "INACTIVE",
    "a scan continuation after the job started": "SUCCESS",
    "a scan continuation after the job moved on": "FAILURE",
    "a scan continuation after the job ran and is INACTIVE again": "INACTIVE",
    "a member's continuation after its box restarted and it ran": "SUCCESS",
    "a member's continuation after its box restarted": "INACTIVE",
}


@pytest.mark.parametrize("case", sorted(_KT_OUTCOMES))
def test_each_kill_and_timer_outcome_fires_its_row_and_writes_its_line(
    case: str,
) -> None:
    """KILLJOB and the TIMER input are dispatched to the job's instance: the
    row the chart fires is the outcome the rule reaches, its line is written,
    and every instance agrees with its row. The window timer's own facts, a deferral that no
    longer belongs to its box run and a scan continuation, are decided by the
    `start` its relay posts."""
    jil, script, job, rows, line = _KT_OUTCOMES[case]
    semantics = resolve({"queued-recheck": "1"}) if jil is oracle_inputs.KILL_TIMER_JIL else None
    paired = Oracle(lower_source(jil), semantics=semantics)

    def step(item: Event | datetime) -> None:
        if isinstance(item, Event):
            paired.feed(item)
        else:
            paired.advance(item)

    for item in script[:-1]:
        step(item)
    if case in _KT_STATUS:
        assert paired.store.job[job].status == _KT_STATUS[case]
    seen = _spied(paired)
    traced = len(paired.trace())
    step(script[-1])
    _agrees(paired)
    key = RuntimeState.job_key(job)
    mine = [o.rows for o in seen if o.key == key and o.event.name in _ITEMS and o.rows]
    assert mine == rows
    lines = [t.transition for t in paired.trace()[traced:] if t.job == job]
    assert (line in lines) if line else lines == []
    causes = [t.cause for t in paired.trace()[traced:] if t.job == job]
    if "member's continuation" in case:  # box-stale wins over superseded
        assert causes[-1].startswith("deferred start belongs to box 'kb' run 1")
    elif "continuation after" in case:
        assert causes[-1].startswith("the job has moved on since the scan stopped")


def test_sem14_a_killed_box_terminates_its_terminator_members_in_its_cascade() -> None:
    """The kill of a live box is job.114 on the box; the members with
    job_terminator die in the box's end (`box ends step`, DL-348), and the
    others run on."""
    paired = _driven(_KT_JIL, [ev("FORCE_STARTJOB", 1, job="b"), ev("KILLJOB", 2, job="b")])
    rows = paired.store.job
    assert (rows["b"].status, rows["m"].status, rows["n"].status) == (
        "TERMINATED",
        "TERMINATED",
        "RUNNING",
    )
    causes = [t.cause for t in paired.trace() if t.job == "m"]
    assert causes[-1] == "job_terminator: box 'b' ended"


def test_sem33_a_bare_timer_relays_its_start_through_the_window_relay() -> None:
    """An injected TIMER with no check and no deferral is an attempt, relayed
    by job.179 as a window timer's replay is: the relay is the input's, not
    read from the cause text."""
    paired = Oracle(lower_source(_KT_JIL))
    seen = _spied(paired)
    paired.feed(ev("TIMER", 1, job="t"))
    rows = [(o.event.name, o.rows) for o in seen if o.key == "job:t"]
    assert rows[:2] == [("TIMER run_window", ("job.179",)), ("start", ("job.102",))]
    assert [t.cause for t in paired.trace() if t.job == "t"][0] == "TIMER event"


def test_sem34_a_deadline_alarm_stays_code_and_dispatches_nothing() -> None:
    """must_start and must_complete move no state: their alarms are code
    (`_dispatch_timer_check`), before any dispatch, and the job's instance sees no event."""
    paired = Oracle(lower_source(_KT_JIL))
    seen = _spied(paired)
    paired.feed(ev("TIMER", 1, job="t", check="must_start", run=0))
    paired.feed(ev("TIMER", 2, job="t", check="must_complete", run=0))
    assert [o for o in seen if o.key == "job:t"] == []
    lines = [t.transition for t in paired.trace() if t.job == "t"]
    assert lines == ["MUST_START_ALARM", "MUST_COMPLETE_ALARM"]


def _rows_changed(**changes: dict[str, str]) -> type[Oracle]:
    """A chart whose named rows have the fields given changed."""

    class Changed(Oracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            rows = tuple(
                replace(r, **changes[r.id.replace(".", "_")])
                if r.id.replace(".", "_") in changes
                else r
                for r in chart.rows
            )
            return replace(chart, rows=rows), active, data

    return Changed


_SWAPPED = {"job_114": {"source": "IDLE"}, "job_440": {"source": "LIVE"}}


@pytest.mark.parametrize(
    ("changes", "script", "message"),
    [
        (
            {"job_114": {"guard": "is_job"}},
            [ev("FORCE_STARTJOB", 1, job="b"), ev("KILLJOB", 2, job="b")],
            "UnhandledEvent: .*'job:b'",
        ),
        (
            {"job_114": {"guard": "is_box"}},
            [ev("STARTJOB", 1, job="t"), ev("KILLJOB", 2, job="t")],
            "UnhandledEvent: .*'job:t'",
        ),
    ],
    ids=["jobs only", "boxes only"],
)
def test_a_live_kill_row_narrowed_to_one_job_type_fails(
    changes: dict[str, dict[str, str]],
    script: list[Event],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mutants of job.114: the row narrowed to one job type, so the other
    type's live kill meets a gap, a statechart error (DL-292's Q1). The
    kill's outcome names the error. The mutant's hits stay out of the
    session."""
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    mutant = _rows_changed(**changes)(lower_source(_KT_JIL))
    for event in script[:-1]:
        mutant.feed(event)
    with pytest.raises(Exception) as raised:  # noqa: B017 -- the class is in `message`
        mutant.feed(script[-1])
    assert re.search(message, f"{type(raised.value).__name__}: {raised.value}")


def test_a_live_kill_row_with_its_state_swapped_differs_from_the_goldens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mutant of job.114 with its state dimension swapped with job.440's: a
    live kill ignored, an idle kill terminating. The chart is the one
    declaration of a job's moves, so the oracle writes what the mutant
    reached; the goldens catch it at the first kill their corpus makes. The
    mutant's hits stay out of the session."""
    from oracle_goldens import replay
    from test_oracle_goldens import _directed_runs

    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    mutant = _rows_changed(**_SWAPPED)
    replayed = replay(mutant, ["corpus-seeds"], limit=_directed_runs(), stop_at_first=True)
    assert len(replayed.differences) == 1
    assert "KILLJOB" in replayed.differences[0].input


def test_sem34_a_startjob_arms_its_deadlines_before_its_attempt() -> None:
    """The oracle's own `_dispatch` makes a start input's attempt; a
    STARTJOB at a start time still arms must_start and must_complete first,
    and the attempt that follows is the chart's."""
    text = (
        "insert_job: sla\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'days_of_week: all\nstart_times: "08:01"\nmust_start_times: "08:05"\n'
        'must_complete_times: "08:30"\n'
    )
    paired = _driven(text, [ev("STARTJOB", 1, job="sla")])
    checks = sorted(str(e.payload.get("check")) for _due, _seq, e in paired.store.timers())
    assert checks == ["must_complete", "must_start"]
    assert paired.store.job["sla"].status == "RUNNING"


# ---- the flag family (DL-344)

_FLAG_ITEMS = (
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "ice moved",
    "noexec moved",
    "start",
)


#: Scripts on the directed flag estate for cells the harness's flag scripts do not reach,
#: kept here so the goldens' sample stays as recorded.
_LOCAL_FLAG_SCRIPTS: Final[dict[str, tuple[Event, ...]]] = {
    "ON_HOLD twice on a job that is not a waiter": (
        ev("ON_HOLD", 0, job="fl_dated"),
        ev("ON_HOLD", 1, job="fl_dated"),
    ),
    "OFF_HOLD on a waiter not on hold": (
        ev("STARTJOB", 0, job="fl_hold"),
        ev("STARTJOB", 0, job="fl_wait"),
        ev("OFF_HOLD", 1, job="fl_wait"),
    ),
}


def _flag_steps(name: str, count: int) -> list[Event]:
    """The first `count` inputs of a script of the harness's directed flag
    estate, or of a local one, its seal points left out."""
    script = _LOCAL_FLAG_SCRIPTS.get(name) or oracle_inputs._flag_scripts()[name]
    return [s for s in script if isinstance(s, Event)][:count]


_LIVE = "flags on live jobs ignored"
_ICED = "ON_NOEXEC on an iced job ignored"
_BUSY = "ON_NOEXEC on boxes with busy members ignored"
_WAITERS = "ON_HOLD and ON_ICE on waiters"
_NOEXEC_WAITERS = "ON_NOEXEC on waiters"
_FAILED = "ON_NOEXEC on a FAILED job"
_QUEUED_BOX = "flags on a queued box"
_STARTING_BOX = "ON_NOEXEC on a STARTING box"
_DISARMS = "DISARM on an armed job and box"
_RUNNING_BOX = "OFF_ICE in a running box, ON_NOEXEC on its idle subbox"
#: Per outcome class: the directed flag script and how many of its inputs to
#: feed, the job, the rows the last input fires on the job's instance (the
#: flag row, then the status row of its `ice moved` or `noexec moved` where one
#: fires, then the start a retry posts), and a trace line the oracle writes for
#: the job there.
_FLAG_OUTCOMES: dict[str, tuple[str, int, str, list[tuple[str, ...]], str]] = {
    "ON_ICE ignored: RUNNING": (_LIVE, 2, "fl_job", [("job.442",)], "EVENT_IGNORED"),
    "ON_HOLD ignored: RUNNING": (_LIVE, 3, "fl_job", [("job.443",)], "EVENT_IGNORED"),
    "ON_NOEXEC ignored: RUNNING": (_LIVE, 4, "fl_job", [("job.444",)], "EVENT_IGNORED"),
    "ON_ICE ignored: STARTING": (_LIVE, 6, "fl_hold", [("job.442",)], "EVENT_IGNORED"),
    "ON_HOLD ignored: STARTING": (_LIVE, 7, "fl_hold", [("job.443",)], "EVENT_IGNORED"),
    "ON_NOEXEC ignored: STARTING": (_LIVE, 8, "fl_hold", [("job.444",)], "EVENT_IGNORED"),
    "ON_ICE": (_ICED, 1, "fl_job", [("job.201",), ("job.451",)], "ON_ICE"),
    "ON_ICE on an iced job": (_ICED, 2, "fl_job", [("job.218",)], "ON_ICE"),
    "ON_NOEXEC ignored: ON_ICE": (_ICED, 3, "fl_job", [("job.444",)], "EVENT_IGNORED"),
    "OFF_ICE": (_ICED, 4, "fl_job", [("job.202",)], "OFF_ICE"),
    "OFF_ICE on a job not iced": (_ICED, 5, "fl_job", [("job.219",)], "OFF_ICE"),
    "ON_NOEXEC ignored: a box with a queued member": (
        _BUSY,
        3,
        "fl_q",
        [("job.444",)],
        "EVENT_IGNORED",
    ),
    "ON_NOEXEC ignored: a queued box with a running member": (
        _BUSY,
        6,
        "fl_box",
        [("job.444",)],
        "EVENT_IGNORED",
    ),
    "ON_NOEXEC ignored: a box with a STARTING member": (
        "ON_NOEXEC on a box with a STARTING member ignored",
        2,
        "fl_q",
        [("job.444",)],
        "EVENT_IGNORED",
    ),
    "ON_HOLD on a job that is not a waiter": (_DISARMS, 1, "fl_dated", [("job.204",)], "ON_HOLD"),
    "ON_HOLD on a held job that is not a waiter": (
        "ON_HOLD twice on a job that is not a waiter",
        2,
        "fl_dated",
        [("job.220",)],
        "ON_HOLD",
    ),
    "OFF_HOLD on a job not on hold": (
        "ON_NOEXEC on a STARTING box that holds a unit",
        6,
        "fl_bm",
        [("job.221",), ("job.180",)],
        "OFF_HOLD",
    ),
    "OFF_HOLD on a waiter not on hold": (
        "OFF_HOLD on a waiter not on hold",
        3,
        "fl_wait",
        [("job.238",)],
        "OFF_HOLD",
    ),
    "ON_HOLD on a waiter": (_WAITERS, 3, "fl_wait", [("job.235",)], "ON_HOLD"),
    "ON_HOLD on a held waiter": (_WAITERS, 4, "fl_wait", [("job.236",)], "ON_HOLD"),
    "OFF_HOLD on a held waiter": (_WAITERS, 6, "fl_wait", [("job.237",)], "QUE_WAIT->STARTING"),
    "ON_ICE on a waiter": (
        _WAITERS,
        8,
        "fl_hold",
        [("job.234",), ("job.112",)],
        "QUE_WAIT->INACTIVE",
    ),
    "ON_NOEXEC on a held waiter": (
        _NOEXEC_WAITERS,
        4,
        "fl_wait",
        [("job.207", "job.209"), ("job.113",), ("job.101",)],
        "QUE_WAIT->INACTIVE",
    ),
    "OFF_NOEXEC": (_NOEXEC_WAITERS, 6, "fl_wait", [("job.210",)], "OFF_NOEXEC"),
    "ON_NOEXEC on a waiter": (
        _NOEXEC_WAITERS,
        8,
        "fl_wait",
        [("job.209",), ("job.113",), ("job.101",)],
        "QUE_WAIT->INACTIVE",
    ),
    "ON_NOEXEC on a FAILED job": (
        _FAILED,
        2,
        "fl_job",
        [("job.209",), ("job.121",)],
        "FAILURE->INACTIVE",
    ),
    "OFF_NOEXEC on a job not ON_NOEXEC": (_FAILED, 4, "fl_job", [("job.223",)], "OFF_NOEXEC"),
    "ON_HOLD on a queued box": (_QUEUED_BOX, 3, "fl_box", [("job.235",)], "ON_HOLD"),
    "OFF_HOLD on a held queued box": (_QUEUED_BOX, 4, "fl_box", [("job.237",)], "OFF_HOLD"),
    "DISARM on a queued box": (_QUEUED_BOX, 5, "fl_box", [("job.433",)], "DISARM"),
    "ON_ICE on a queued box": (
        _QUEUED_BOX,
        6,
        "fl_box",
        [("job.234",), ("job.112",)],
        "QUE_WAIT->INACTIVE",
    ),
    "OFF_NOEXEC on a box whose members are not ON_NOEXEC": (
        _QUEUED_BOX,
        7,
        "fl_box",
        [("job.223",)],
        "OFF_NOEXEC",
    ),
    "ON_NOEXEC on a queued box": (
        "ON_NOEXEC on a queued box",
        3,
        "fl_box",
        [("job.209",), ("job.452",)],
        "QUE_WAIT->INACTIVE",
    ),
    "ON_NOEXEC on a STARTING box": (
        _STARTING_BOX,
        3,
        "fl_q",
        [("job.209",), ("job.452",)],
        "STARTING->INACTIVE",
    ),
    "OFF_NOEXEC on a box": (_STARTING_BOX, 4, "fl_q", [("job.210",)], "OFF_NOEXEC"),
    "ON_NOEXEC on a STARTING box that holds a unit": (
        "ON_NOEXEC on a STARTING box that holds a unit",
        5,
        "fl_box",
        [("job.209",), ("job.452",)],
        "STARTING->INACTIVE",
    ),
    "ON_NOEXEC on a held idle box": (
        "ON_NOEXEC on a held idle box",
        3,
        "fl_q",
        [("job.207", "job.209"), ("job.198",), ("job.124",)],
        "OFF_HOLD",
    ),
    "DISARM on an armed job": (_DISARMS, 5, "fl_dated", [("job.432",)], "DISARM"),
    "DISARM with no latch": (_DISARMS, 6, "fl_dated", [("job.433",)], "DISARM"),
    "DISARM on an armed box": (_DISARMS, 7, "fl_box", [("job.432",)], "DISARM"),
    "OFF_HOLD retries the start": (
        _DISARMS,
        8,
        "fl_box",
        [("job.205",), ("job.180",)],
        "OFF_HOLD",
    ),
    "OFF_ICE in a running box": (_RUNNING_BOX, 3, "fl_rm", [("job.202",)], "OFF_ICE"),
    "ON_NOEXEC on an idle box in a running box": (
        _RUNNING_BOX,
        5,
        "fl_sub",
        [("job.209",), ("job.452",)],
        "INACTIVE->INACTIVE",
    ),
    "ON_ICE on a member a running box waits for": (
        "ON_ICE on the last member a running box waits for",
        4,
        "fl_sub",
        [("job.201",), ("job.451",)],
        "ON_ICE",
    ),
    "ON_NOEXEC on a job outside the catalog": (
        "ON_NOEXEC on a job outside the catalog",
        2,
        "fl_ghost",
        [("job.207", "job.209"), ("job.198",)],
        "OFF_HOLD",
    ),
}


#: The cases whose job holds units before the last input: the cell is the
#: settlement of a run's units, so the precondition is asserted, not assumed.
_HOLDS_BEFORE: Final = frozenset({"ON_NOEXEC on a STARTING box that holds a unit"})


#: As `_OUTCOME_STATUS`, for the flag cases.
_FLAG_STATUS = {
    "ON_ICE ignored: RUNNING": "RUNNING",
    "ON_HOLD ignored: RUNNING": "RUNNING",
    "ON_NOEXEC ignored: RUNNING": "RUNNING",
    "ON_ICE ignored: STARTING": "STARTING",
    "ON_HOLD ignored: STARTING": "STARTING",
    "ON_NOEXEC ignored: STARTING": "STARTING",
    "ON_NOEXEC ignored: ON_ICE": "INACTIVE",
    "ON_NOEXEC ignored: a box with a queued member": "INACTIVE",
    "ON_NOEXEC ignored: a queued box with a running member": "QUE_WAIT",
    "ON_NOEXEC ignored: a box with a STARTING member": "INACTIVE",
}


@pytest.mark.parametrize("case", sorted(_FLAG_OUTCOMES))
def test_each_flag_outcome_fires_its_rows_and_writes_its_line(
    case: str,
) -> None:
    """The flag inputs are dispatched to the job's instance: the flag row
    fires, then the status row its `ice moved` or `noexec moved` fires where
    one does, then the start a retry posts; its line is written, and every
    instance agrees with its row."""
    name, count, job, rows, line = _FLAG_OUTCOMES[case]
    steps = _flag_steps(name, count)
    paired = Oracle(lower_source(oracle_inputs.FLAG_JIL))
    for step in steps[:-1]:
        paired.feed(step)
    if case in _FLAG_STATUS:
        assert paired.store.job[job].status == _FLAG_STATUS[case]
    if case in _HOLDS_BEFORE:  # job.452 settles a run's units: the box holds FL_R
        row = paired.store.job[job]
        assert (row.status, [r.bucket for r in row.reservations]) == ("STARTING", ["r:FL_R"])
    seen = _spied(paired)
    traced = len(paired.trace())
    paired.feed(steps[-1])
    _agrees(paired)
    key = RuntimeState.job_key(job)
    mine = [o.rows for o in seen if o.key == key and o.event.name in _FLAG_ITEMS and o.rows]
    assert mine == rows
    assert line in [t.transition for t in paired.trace()[traced:] if t.job == job]


def _flag_oracle(cls: type[Oracle] = Oracle) -> Oracle:
    return cls(lower_source(oracle_inputs.FLAG_JIL))


@pytest.mark.parametrize(
    ("script", "message"),
    [
        (
            [ev("STARTJOB", 1, job="fl_hold"), ev("STARTJOB", 1, job="fl_wait")],
            "job.452, job.198 are all enabled[^']*'job:fl_wait'",
        ),
        ([ev("STATUS", 1, job="fl_q", exit_code=1)], "'fl_q': job.121 fired for a BOX job"),
    ],
    ids=["a queued job takes the box's row", "a failed box takes the job's row"],
)
def test_a_noexec_settlement_for_the_other_job_type_is_a_statechart_error(
    script: list[Event], message: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutant: ON_NOEXEC's job and box settlement guards swapped; either way a
    statechart error before any write. On a FAILED box the job's row job.121
    fires, and the writer's job-type check refuses it, since the catalog says
    a box. On a waiter the box's row job.452 is enabled beside job.198, whose
    guard calls the unswapped `noexec_settles_box`, so the selection raises
    AmbiguousTransition before any writer runs. The mutant's hits stay out of
    the session."""
    monkeypatch.setenv(HITS_ENV, str(tmp_path))

    class Swapped(Oracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return _swapped(chart, "noexec_settles_job", "noexec_settles_box"), active, data

    oracle = _flag_oracle(Swapped)
    for event in script:
        oracle.feed(event)
    job = "fl_wait" if "fl_wait" in message else "fl_q"
    traced = len(oracle.trace())
    with pytest.raises(StatechartError, match=message):
        oracle.feed(ev("ON_NOEXEC", 2, job=job))
    # the flag's microstep wrote its line; the status row's write never ran
    assert not any("->" in t.transition for t in oracle.trace()[traced:])


@pytest.mark.parametrize("job", ["fl_job", "fl_box"])
def test_an_input_the_chart_ignores_and_the_rules_apply_is_a_statechart_error(
    job: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutant: ON_NOEXEC's ignored and applying guards swapped, so the chart
    ignores it on a plain INACTIVE job, and on an idle box whose jobs are
    idle. The hook writes an ignored line only with the rules' reason for it
    (`_oob_ignored`), and there is none."""
    monkeypatch.setenv(HITS_ENV, str(tmp_path))

    class Swapped(Oracle):
        def _job_instance(
            self, job: str, row: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(job, row)
            return _swapped(chart, "noexec_ignored", "noexec_applies"), active, data

    oracle = _flag_oracle(Swapped)
    with pytest.raises(StatechartError, match=f"'{job}': ON_NOEXEC fired job.444, and no ignored"):
        oracle.feed(ev("ON_NOEXEC", 1, job=job))
    assert oracle.store.job[job].status == "INACTIVE"
    assert oracle.trace() == []


@pytest.mark.parametrize(("kind", "row"), [("ON_ICE", "job_442"), ("ON_HOLD", "job_443")])
def test_an_ice_or_hold_the_chart_ignores_on_an_idle_job_is_a_statechart_error(
    kind: str, row: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutant: the ignored row of ON_ICE or ON_HOLD takes an INACTIVE job, where
    no rule ignores the input (DL-254 ignores it on a live job only). The hook
    writes an ignored line only with the rules' reason (`_oob_ignored`), and
    there is none."""
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    oracle = _flag_oracle(_rows_changed(**{row: {"source": "INACTIVE"}}))
    with pytest.raises(StatechartError, match=f"'fl_job': {kind} fired .*and no ignored line"):
        oracle.feed(ev(kind, 1, job="fl_job"))
    assert oracle.trace() == []


def test_sem20_an_ice_on_the_last_member_completes_its_running_box_in_its_cascade() -> None:
    """ON_ICE on a member that a RUNNING box still waits for fires the flag
    row and job.451; the box's completion moment (DL-285) follows from the
    flag row's `holders may leave`, and the box ends SUCCESS."""
    steps = _flag_steps("ON_ICE on the last member a running box waits for", 4)
    paired = _driven(oracle_inputs.FLAG_JIL, steps)
    assert paired.store.job["fl_run"].status == "SUCCESS"
    lines = [(t.job, t.transition) for t in paired.trace()[-2:]]
    assert lines == [("fl_sub", "ON_ICE"), ("fl_run", "RUNNING->SUCCESS")]


# ---- RELEASE_RESOURCE and the readmission (DL-345)

_QUEUE_ITEMS = (
    "RELEASE_RESOURCE",
    "readmit",
    "ice moved",
    "noexec moved",
    "admitted start",
    "actual start",
    "started",
)


def _queue_steps(name: str, count: int) -> list[Event]:
    """The first `count` inputs of a script of the harness's directed queue
    estate, its seal points left out (its one Advance is never among them)."""
    script = oracle_inputs._queue_scripts()[name]
    return [s for s in script if isinstance(s, Event)][:count]


def _queue_semantics(name: str) -> Any:
    rechecks = {"queued-recheck": "1"} if name in oracle_inputs.QUEUE_RECHECKS else {}
    return resolve(rechecks)


_ON_JOBS = "RELEASE_RESOURCE on live, idle and unknown jobs"
_WAKES = "a held job's release wakes its waiter"
_RANKS = "two waiters of different rank"
_WAITERS_ = "held, iced and noexec waiters"
_OWED = "a stopped box's owed scan keeps its member and admits what it blocked"
_ADMITTED = [("job.108",), ("job.301",), ("job.431",), ("job.105",)]
_LEAVES = [("job.439",)]
#: Per outcome class: the directed queue script and how many of its inputs
#: to feed, the job, the rows the last input fires on the job's instance (the
#: release row; or the readmission's rows, a flag move's status row, the
#: admitted start's holding and arm rows and the `started` that follows), and
#: a trace line the oracle writes for the job there ("" for none).
_QUEUE_OUTCOMES: dict[str, tuple[str, int, str, list[tuple[str, ...]], str]] = {
    "RELEASE_RESOURCE on a running job's units": (
        _ON_JOBS,
        2,
        "qu_live",
        [("job.316",)],
        "RELEASE_RESOURCE",
    ),
    "RELEASE_RESOURCE on a running job holding nothing": (
        _ON_JOBS,
        4,
        "qu_bare",
        [("job.315",)],
        "RELEASE_RESOURCE",
    ),
    "RELEASE_RESOURCE on an idle job holding nothing": (
        _ON_JOBS,
        5,
        "qu_keep",
        [("job.315",)],
        "RELEASE_RESOURCE",
    ),
    "RELEASE_RESOURCE on a job outside the catalog": (
        _ON_JOBS,
        6,
        "qu_ghost",
        [("job.315",)],
        "RELEASE_RESOURCE",
    ),
    "RELEASE_RESOURCE on a STARTING job's units": (
        _ON_JOBS,
        8,
        "qu_live",
        [("job.316",)],
        "RELEASE_RESOURCE",
    ),
    "RELEASE_RESOURCE of held units": (_WAKES, 4, "qu_keep", [("job.307",)], "RELEASE_RESOURCE"),
    "a waiter the release wakes is admitted": (
        _WAKES,
        4,
        "qu_w",
        _ADMITTED,
        "QUE_WAIT->STARTING",
    ),
    "the higher rank is admitted first": (_RANKS, 5, "qu_hi", _ADMITTED, "QUE_WAIT->STARTING"),
    "the lower rank stays queued": (_RANKS, 5, "qu_lo", [("job.181",)], ""),
    "the lower rank is admitted at the next release": (
        _RANKS,
        6,
        "qu_lo",
        _ADMITTED,
        "QUE_WAIT->STARTING",
    ),
    "a waiter is admitted on its held units": (
        "a waiter readmitted on its held units",
        5,
        "qu_take",
        [("job.108",), ("job.302",), ("job.431",), ("job.105",)],
        "QUE_WAIT->STARTING",
    ),
    "an armed waiter is admitted and its arm consumed": (
        "an armed waiter admitted",
        6,
        "qu_arm",
        [("job.108",), ("job.301",), ("job.430",), ("job.105",)],
        "QUE_WAIT->STARTING",
    ),
    "a held waiter stays queued": (_WAITERS_, 5, "qu_w", [("job.181",)], ""),
    "a held waiter is admitted on OFF_HOLD": (
        _WAITERS_,
        6,
        "qu_w",
        [*_ADMITTED[:3], ("job.105",)],
        "QUE_WAIT->STARTING",
    ),
    "a waiter whose condition is now false leaves the queue": (
        "a waiter whose condition is now false",
        4,
        "qu_cond",
        _LEAVES,
        "QUE_WAIT->INACTIVE",
    ),
    "an armed waiter whose condition is now false loses its arm and leaves": (
        "an armed waiter whose condition is now false",
        5,
        "qu_armc",
        [("job.438",)],
        "SCHED_DISARM",
    ),
    "a waiter whose window shut nearer its close leaves and skips": (
        "a waiter whose window shut nearer its close",
        3,
        "qu_win",
        _LEAVES,
        "RUN_WINDOW_SKIP",
    ),
    "a waiter whose window shut nearer its opening leaves and defers": (
        "a waiter whose window shut nearer its opening",
        3,
        "qu_win",
        _LEAVES,
        "RUN_WINDOW_DEFER",
    ),
    "a queued box is admitted and starts its members": (
        "a queued box is readmitted and starts its members",
        3,
        "qu_bx",
        [("job.108",), ("job.301",), ("job.431",), ("job.125",)],
        "QUE_WAIT->STARTING",
    ),
    "a member a STATUS took out of the queue is not readmitted": (
        "a box's release meets the member a STATUS took out of the queue",
        3,
        "qu_sm",
        [],
        "QUE_WAIT->INACTIVE",
    ),
    "a queued box its member ended is not readmitted in its parent's release": (
        "a queued box's member ends it, and its parent's release meets it",
        4,
        "qu_q",
        [],
        "QUE_WAIT->SUCCESS",
    ),
    "a box_terminator member set FAILURE in the queue is not readmitted": (
        "a box's release meets its box_terminator member set FAILURE in the queue",
        3,
        "qu_tm",
        [],
        "QUE_WAIT->FAILURE",
    ),
    "a member of a box that ended is cancelled in its member's cascade": (
        "a box's end cancels its queued member in its member's cascade",
        3,
        "qu_bq",
        [("job.109",)],
        "QUE_WAIT->INACTIVE",
    ),
    "an owed scan keeps a member of a stopped box": (
        _OWED,
        4,
        "qu_lm",
        [("job.181",), ("job.181",)],
        "",
    ),
    "an owed scan admits the job the member blocked": (
        _OWED,
        4,
        "qu_lx",
        _ADMITTED,
        "QUE_WAIT->STARTING",
    ),
    "a release cancels the member of the stopped box": (
        _OWED,
        5,
        "qu_lm",
        [("job.109",)],
        "QUE_WAIT->INACTIVE",
    ),
    "a waiter with no units to reserve is admitted": (
        "a job with no load is admitted behind the waiter that blocked it",
        4,
        "qu_nl",
        [("job.108",), ("job.311",), ("job.431",), ("job.105",)],
        "QUE_WAIT->STARTING",
    ),
}
#: The cases whose readmission is the owed scan's, which keeps a member of
#: a stopped box queued (DL-247, DL-255).
_KEEPS_STOPPED: Final = frozenset(
    {
        "an owed scan keeps a member of a stopped box",
        "an owed scan admits the job the member blocked",
    }
)


@pytest.mark.parametrize("case", sorted(_QUEUE_OUTCOMES))
def test_each_release_and_readmit_outcome_fires_its_rows_and_writes_its_line(
    case: str,
) -> None:
    """RELEASE_RESOURCE and each waiter's readmission are dispatched to the
    job's instance: the rows the chart fires are the outcome the rule
    reaches, its line is written, and every instance agrees with its row."""
    name, count, job, rows, line = _QUEUE_OUTCOMES[case]
    steps = _queue_steps(name, count)
    paired = Oracle(lower_source(oracle_inputs.QUEUE_JIL), semantics=_queue_semantics(name))
    for step in steps[:-1]:
        paired.feed(step)
    seen = _spied(paired)
    traced = len(paired.trace())
    paired.feed(steps[-1])
    _agrees(paired)
    key = RuntimeState.job_key(job)
    mine = [o for o in seen if o.key == key and o.event.name in _QUEUE_ITEMS and o.rows]
    assert [o.rows for o in mine] == rows
    readmits = [o.event.payload.keep_stopped for o in mine if o.event.name == "readmit"]
    assert readmits == [case in _KEEPS_STOPPED] * len(readmits)
    lines = [t.transition for t in paired.trace()[traced:] if t.job == job]
    assert (line in lines) if line else lines == []


def _flag_read_setup(table: str, case: str) -> tuple[Oracle, list[Event | datetime], str]:
    """An outcome case of a table above (`flag`, `kill` or `queue`): the
    oracle it runs on, its inputs and its job."""
    if table == "flag":
        name, count, job, _rows, _line = _FLAG_OUTCOMES[case]
        catalog, semantics = lower_source(oracle_inputs.FLAG_JIL), None
        steps: list[Event | datetime] = [*_flag_steps(name, count)]
    elif table == "kill":
        jil, steps, job, _rows, _line = _KT_OUTCOMES[case]
        catalog = lower_source(jil)
        semantics = (
            resolve({"queued-recheck": "1"}) if jil is oracle_inputs.KILL_TIMER_JIL else None
        )
    else:
        name, count, job, _rows, _line = _QUEUE_OUTCOMES[case]
        catalog, semantics = lower_source(oracle_inputs.QUEUE_JIL), _queue_semantics(name)
        steps = [*_queue_steps(name, count)]
    return Oracle(catalog, semantics=semantics), steps, job


@pytest.mark.parametrize(
    ("row", "table", "case", "reads"),
    [
        ("job.112", "flag", "ON_ICE on a waiter", {"ice_on"}),
        ("job.113", "flag", "ON_NOEXEC on a held waiter", {"noexec_on", "hold_off"}),
        ("job.452", "flag", "ON_NOEXEC on an idle box in a running box", {"noexec_on"}),
    ],
)
def test_a_status_rule_that_reads_a_flag_sees_the_flag_moved(
    row: str, table: str, case: str, reads: set[str]
) -> None:
    """The flag is written first, then the status rule reads it
    (`_resolves` and `_out_of_the_run` read `on_ice`; a wake's attempt reads
    `armed`; the retry reads `on_hold` and `on_noexec`). The status row's
    action, run by the oracle, sees the flag write, because it runs as the
    item the flag row's action sends."""
    seen: list[frozenset[str]] = []

    def spying(chart: Statechart) -> Statechart:
        action = next(r.action for r in chart.rows if r.id == row)

        def spy(ctx: Any) -> Any:
            """Record the configuration the job's status row's action sees."""
            if ctx.key == RuntimeState.job_key(job):
                seen.append(ctx.active)
            return chart.actions[action](ctx)

        return replace(
            chart,
            rows=tuple(replace(r, action="spy") if r.id == row else r for r in chart.rows),
            actions={**chart.actions, "spy": spy},
        )

    class Spied(Oracle):
        def _job_instance(
            self, name: str, runtime: JobRuntime
        ) -> tuple[Statechart, frozenset[str], object]:
            chart, active, data = super()._job_instance(name, runtime)
            return spying(chart), active, data

    plain, steps, job = _flag_read_setup(table, case)
    oracle = Spied(plain.catalog, semantics=plain.semantics)
    for step in steps:
        del seen[:]
        if isinstance(step, Event):
            oracle.feed(step)
        else:
            oracle.advance(step)
    _agrees(oracle)
    assert len(seen) == 1 and reads <= seen[0]


@pytest.mark.parametrize(
    ("row", "table", "case", "lines"),
    [
        ("job.436", "kill", "kill an armed waiter", ["QUE_WAIT->TERMINATED"]),
        (
            "job.438",
            "queue",
            "an armed waiter whose condition is now false loses its arm and leaves",
            ["SCHED_DISARM", "QUE_WAIT->INACTIVE"],
        ),
    ],
)
def test_an_armed_waiter_s_kill_or_leave_drops_the_arm_before_its_status_line(
    row: str, table: str, case: str, lines: list[str]
) -> None:
    """The arm is a fact of the payload, not a region (DL-365): one row drops it and moves the
    status, and its writer writes the arm before the status, the arm's line first where it has
    one (DL-54)."""
    oracle, steps, job = _flag_read_setup(table, case)

    def step(item: Event | datetime) -> None:
        if isinstance(item, Event):
            oracle.feed(item)
        else:
            oracle.advance(item)

    for item in steps[:-1]:
        step(item)
    assert oracle.store.job[job].armed and oracle.store.job[job].status == "QUE_WAIT"
    seen = _spied(oracle)
    traced = len(oracle.trace())
    step(steps[-1])
    _agrees(oracle)
    key = RuntimeState.job_key(job)
    assert [o.rows for o in seen if o.key == key and row in o.rows] == [(row,)]
    assert not oracle.store.job[job].armed
    written = [t.transition for t in oracle.trace()[traced:] if t.job == job]
    assert written[: len(lines)] == lines


def test_dl50_the_higher_ranked_waiter_goes_first_and_each_gets_its_own_readmit() -> None:
    """The scan's order is read off the rows (rank, then the queue's order);
    here two waiters differ by rank only, and the higher-ranked one goes
    first: the chart decides each waiter, one `readmit` on its own instance,
    in that order. A move restarts the scan."""
    name = _RANKS
    paired = Oracle(lower_source(oracle_inputs.QUEUE_JIL))
    steps = _queue_steps(name, 5)
    for step in steps[:-1]:
        paired.feed(step)
    seen = _spied(paired)
    paired.feed(steps[-1])
    readmits = [(o.key, o.rows) for o in seen if o.event.name == "readmit"]
    assert readmits == [("job:qu_hi", ("job.108",)), ("job:qu_lo", ("job.181",))]


def _queue_oracle(cls: type[Oracle] = Oracle, name: str = _WAKES) -> Oracle:
    return cls(lower_source(oracle_inputs.QUEUE_JIL), semantics=_queue_semantics(name))


def _fed(oracle: Oracle, name: str, count: int) -> Oracle:
    for step in _queue_steps(name, count):
        oracle.feed(step)
    return oracle


def test_dl50_a_readmission_computes_only_the_facts_the_scan_reads() -> None:
    """A held waiter is kept before its admission is read; a member of a
    stopped box is cancelled before it; an inadmissible waiter's queued
    recheck is not read. The payload computes a fact on its first read, in
    `readmit_verdict`'s order."""
    name = _WAITERS_
    oracle = _fed(_queue_oracle(name=name), name, 4)
    seen = _spied(oracle)
    oracle.feed(_queue_steps(name, 5)[-1])
    held = next(o.event.payload for o in seen if o.event.name == "readmit")
    assert held.known("box") == "none" and held.known("admissible") is None
    assert repr(held) == "ReadmitFacts('qu_w', keep_stopped=False, box='none')"
    oracle = _fed(_queue_oracle(name=_OWED), _OWED, 4)
    seen = _spied(oracle)
    oracle.feed(_queue_steps(_OWED, 5)[-1])
    stopped = next(o.event.payload for o in seen if o.event.name == "readmit")
    assert stopped.known("box") == "idle" and stopped.known("admissible") is None
    oracle = _fed(_queue_oracle(name=_RANKS), _RANKS, 4)
    seen = _spied(oracle)
    oracle.feed(_queue_steps(_RANKS, 5)[-1])
    lo = [o.event.payload for o in seen if o.key == "job:qu_lo"][-1]
    assert lo.known("admissible") is False and lo.known("recheck") is None
    assert lo.known("vector") is not None and "recheck" not in repr(lo)


# ---- the box rules (DL-346)

_BOX_ITEMS = (
    "member ends FAILURE or TERMINATED",
    "member moves",
    "box recompute",
    "box start",
    "box running",
    "box run ends",
    "box ends FAILURE or TERMINATED",
    "box set INACTIVE",
    "OFF_NOEXEC on its box",
    "ON_NOEXEC on its box",
    "first input of a period",
)


def _box_item(outcome: Outcome) -> bool:
    """A box rule's item: one of `_BOX_ITEMS`, or a completion of a phase of a RUNNING box's
    completion door, which carries the visit's payload (DL-371)."""
    return outcome.event.name in _BOX_ITEMS or (
        outcome.event.name == COMPLETION and isinstance(outcome.event.payload, DoorFacts)
    )


def _job(name: str, box: str | None = None, extra: str = "") -> str:
    inside = f"box_name: {box}\n" if box else ""
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n{inside}{extra}\n"


def _boxed(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


_NEVER = "condition: s(never)\n"
#: Boxes for the box rules' outcome classes: `tb` with a box_terminator and a
#: job_terminator member; `fb` and `sb` that fold; `osb` and `ofb` with an
#: override each, and a member that never runs; `ux` and `uy` whose override
#: never holds; idle boxes `rb1` to `rb7` that re-derive their status; `ab`
#: with an auto_hold member; `ib` set INACTIVE with a running, an ended and
#: an INACTIVE member; `nb` taken OFF_NOEXEC; `lz` whose box_success names a
#: job outside it.
_BOX_JIL = (
    _job("never")
    + _boxed("tb")
    + _job("tm", "tb", "box_terminator: 1\n")
    + _job("tn", "tb", "job_terminator: 1\n")
    + _boxed("fb")
    + _job("fm", "fb")
    + _job("fn", "fb")
    + _boxed("sb")
    + _job("sm", "sb")
    + _boxed("osb", "box_success: s(osm)\n")
    + _job("osm", "osb")
    + _job("osn", "osb", _NEVER)
    + _boxed("ofb", "box_failure: f(ofm)\n")
    + _job("ofm", "ofb")
    + _job("ofn", "ofb", _NEVER)
    + _boxed("ux", "box_success: s(never)\n")
    + _job("uxm", "ux")
    + _boxed("uy", "box_failure: f(never)\n")
    + _job("uym", "uy")
    + _boxed("rb1", "box_success: s(r1m)\n")
    + _job("r1m", "rb1")
    + _boxed("rb2", "box_failure: f(r2m)\n")
    + _job("r2m", "rb2")
    + _boxed("rb3")
    + _job("r3m", "rb3")
    + _boxed("rb4")
    + _job("r4m", "rb4")
    + _boxed("rb5")
    + _job("r5m", "rb5")
    + _job("r5n", "rb5")
    + _boxed("rb6")
    + _job("r6m", "rb6", "status: ON_ICE\n")
    + _boxed("rb7", "box_failure: f(r7n)\n")
    + _job("r7m", "rb7")
    + _job("r7n", "rb7")
    + _boxed("ab")
    + _job("am", "ab", "auto_hold: 1\n")
    + _boxed("ib")
    + _job("im", "ib")
    + _job("in", "ib")
    + _job("iz", "ib", _NEVER)
    + _boxed("nb")
    + _job("nm", "nb")
    + _job("nn", "nb")
    + _boxed("lz", "box_success: s(never)\n")
    + _job("lzm", "lz")
)


def _box_ev(minutes: int, kind: EventKind, job: str, **payload: object) -> Event:
    return ev(kind, minutes, job=job, **payload)


def _box_steps(name: str, count: int) -> list[Event]:
    """The first `count` inputs of a script of the harness's directed box
    estate, its seal points left out."""
    script = oracle_inputs._box_scripts()[name]
    return [s for s in script if isinstance(s, Event)][:count]


def _queued_box_steps(count: int) -> list[Event]:
    return _box_steps("queued boxes re-derive their status as a member ends", count)


_TERMINATOR = [_box_ev(0, "FORCE_STARTJOB", "tb"), _box_ev(1, "STATUS", "tm", status="FAILURE")]
_CASCADE = [
    _box_ev(0, "FORCE_STARTJOB", "ib"),
    _box_ev(1, "STATUS", "in", status="SUCCESS"),
    _box_ev(2, "STATUS", "ib", status="INACTIVE"),
]
_OFF_NOEXEC = [
    _box_ev(0, "ON_NOEXEC", "nb"),
    _box_ev(1, "OFF_NOEXEC", "nm"),
    _box_ev(2, "OFF_NOEXEC", "nb"),
]
#: A box's ON_NOEXEC on its jobs: `nm` held and `nn` with no flag; then `nm`
#: already ON_NOEXEC, and `nn` ON_NOEXEC and then held.
_ON_NOEXEC = [_box_ev(0, "ON_HOLD", "nm"), _box_ev(1, "ON_NOEXEC", "nb")]
_ON_NOEXEC_FLAGGED = [
    _box_ev(0, "ON_NOEXEC", "nm"),
    _box_ev(1, "ON_NOEXEC", "nn"),
    _box_ev(2, "ON_HOLD", "nn"),
    _box_ev(3, "ON_NOEXEC", "nb"),
]
_FOLD_FAILURE = [
    _box_ev(0, "FORCE_STARTJOB", "fb"),
    _box_ev(1, "STATUS", "fm", status="FAILURE"),
    _box_ev(2, "STATUS", "fn", status="SUCCESS"),
]
#: Per outcome class: the estate, its script, the job, the rows the box
#: rules fire on the job's instance in the script's last input, in order,
#: and a trace line the oracle writes for the job there ("" for none).
_BOX_OUTCOMES: dict[str, tuple[str, list[Event], str, list[tuple[str, ...]], str]] = {
    "a box_terminator member's FAILURE terminates its running box": (
        _BOX_JIL,
        _TERMINATOR,
        "tb",
        [("job.130",), ("job.189",)],
        "RUNNING->TERMINATED",
    ),
    "a live job_terminator member of a box that ends TERMINATED terminates": (
        _BOX_JIL,
        _TERMINATOR,
        "tn",
        [("job.435",), ("job.116",)],
        "RUNNING->TERMINATED",
    ),
    "a member that is not live stays as its box ends": (
        _BOX_JIL,
        _TERMINATOR,
        "tm",
        [("job.435",), ("job.197",)],
        "RUNNING->FAILURE",
    ),
    "a member's FAILURE that fires no terminator, no override and no fold": (
        _BOX_JIL,
        _FOLD_FAILURE[:2],
        "fb",
        [("job.189",), ("job.454",), ("job.456",), ("job.458",)],
        "",
    ),
    "the default fold ends a box FAILURE": (
        _BOX_JIL,
        _FOLD_FAILURE,
        "fb",
        [("job.454",), ("job.456",), ("job.129",)],
        "RUNNING->FAILURE",
    ),
    "the default fold ends a box SUCCESS": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "sb"), _box_ev(1, "STATUS", "sm", status="SUCCESS")],
        "sb",
        [("job.454",), ("job.456",), ("job.128",)],
        "RUNNING->SUCCESS",
    ),
    "an unmet box_success holds the default SUCCESS": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "ux"), _box_ev(1, "STATUS", "uxm", status="SUCCESS")],
        "ux",
        [("job.454",), ("job.456",), ("job.458",)],
        "",
    ),
    "an unmet box_failure holds the default FAILURE": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "uy"), _box_ev(1, "STATUS", "uym", status="FAILURE")],
        "uy",
        [("job.189",), ("job.454",), ("job.456",), ("job.458",)],
        "",
    ),
    "box_success ends a running box": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "osb"), _box_ev(1, "STATUS", "osm", status="SUCCESS")],
        "osb",
        [("job.454",), ("job.126",)],
        "RUNNING->SUCCESS",
    ),
    "box_failure ends a running box": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "ofb"), _box_ev(1, "STATUS", "ofm", status="FAILURE")],
        "ofb",
        [("job.189",), ("job.454",), ("job.127",)],
        "RUNNING->FAILURE",
    ),
    "a grandparent's box_success ends it when a member of its subbox ends": (
        oracle_inputs.BOX_JIL,
        _box_steps("a grandparent's box_success names a member of its subbox", 2),
        "bx_g",
        [("job.454",), ("job.126",)],
        "RUNNING->SUCCESS",
    ),
    "the subbox whose member ended stays running": (
        oracle_inputs.BOX_JIL,
        _box_steps("a grandparent's box_success names a member of its subbox", 2),
        "bx_c",
        [("job.454",), ("job.456",), ("job.458",), ("job.435",)],
        "",
    ),
    "an idle box's box_success": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r1m", status="SUCCESS")],
        "rb1",
        [("job.446",)],
        "INACTIVE->SUCCESS",
    ),
    "an idle box's box_failure": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r2m", status="FAILURE")],
        "rb2",
        [("job.189",), ("job.447",)],
        "INACTIVE->FAILURE",
    ),
    "an idle box derives SUCCESS": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r3m", status="SUCCESS")],
        "rb3",
        [("job.448",)],
        "INACTIVE->SUCCESS",
    ),
    "an idle box derives FAILURE": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r4m", status="FAILURE")],
        "rb4",
        [("job.189",), ("job.449",)],
        "INACTIVE->FAILURE",
    ),
    "a FAILURE box's box_success": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "r1m", status="FAILURE"),
            _box_ev(1, "STATUS", "r1m", status="SUCCESS"),
        ],
        "rb1",
        [("job.446",)],
        "FAILURE->SUCCESS",
    ),
    "a SUCCESS box's box_failure": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "r2m", status="SUCCESS"),
            _box_ev(1, "STATUS", "r2m", status="FAILURE"),
        ],
        "rb2",
        [("job.189",), ("job.447",)],
        "SUCCESS->FAILURE",
    ),
    "a FAILURE box derives SUCCESS": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "r4m", status="FAILURE"),
            _box_ev(1, "STATUS", "r4m", status="SUCCESS"),
        ],
        "rb4",
        [("job.448",)],
        "FAILURE->SUCCESS",
    ),
    "a SUCCESS box derives FAILURE": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "r3m", status="SUCCESS"),
            _box_ev(1, "STATUS", "r3m", status="FAILURE"),
        ],
        "rb3",
        [("job.189",), ("job.449",)],
        "SUCCESS->FAILURE",
    ),
    "a queued box's box_success": (
        oracle_inputs.BOX_JIL,
        _queued_box_steps(5),
        "bx_q1",
        [("job.446",)],
        "QUE_WAIT->SUCCESS",
    ),
    "a queued box's box_failure": (
        oracle_inputs.BOX_JIL,
        _queued_box_steps(6),
        "bx_q2",
        [("job.189",), ("job.447",)],
        "QUE_WAIT->FAILURE",
    ),
    "a queued box derives FAILURE": (
        oracle_inputs.BOX_JIL,
        _queued_box_steps(7),
        "bx_q3",
        [("job.189",), ("job.449",)],
        "QUE_WAIT->FAILURE",
    ),
    "a queued box derives SUCCESS": (
        oracle_inputs.QUEUE_JIL,
        _queue_steps("a queued box's member ends it, and its parent's release meets it", 4),
        "qu_q",
        [("job.448",), ("job.435",)],
        "QUE_WAIT->SUCCESS",
    ),
    "an idle box with a member still running derives nothing": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "r5n", status="RUNNING"),
            _box_ev(1, "STATUS", "r5m", status="SUCCESS"),
        ],
        "rb5",
        [("job.191",)],
        "",
    ),
    "an iced member's change is no verdict": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r6m", status="SUCCESS")],
        "rb6",
        [("job.191",)],
        "",
    ),
    "an unmet box_failure suppresses the derived FAILURE": (
        _BOX_JIL,
        [_box_ev(0, "STATUS", "r7m", status="FAILURE")],
        "rb7",
        [("job.189",), ("job.191",)],
        "",
    ),
    "a box start resets a member that ended": (
        _BOX_JIL,
        [
            _box_ev(0, "FORCE_STARTJOB", "sb"),
            _box_ev(1, "STATUS", "sm", status="SUCCESS"),
            _box_ev(2, "FORCE_STARTJOB", "sb"),
        ],
        "sm",
        [("job.122",), ("job.227",)],
        "SUCCESS->INACTIVE",
    ),
    "a box start leaves an INACTIVE member": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "osb")],
        "osn",
        [("job.195",), ("job.227",)],
        "",
    ),
    "a box start clears an INACTIVE member's exit code by a plain write": (
        _BOX_JIL,
        [
            _box_ev(0, "STATUS", "osn", status="INACTIVE", exit_code=3),
            _box_ev(1, "FORCE_STARTJOB", "osb"),
        ],
        "osn",
        [("job.195",), ("job.227",)],
        "",
    ),
    "a box's start holds its auto_hold member": (
        _BOX_JIL,
        [_box_ev(0, "FORCE_STARTJOB", "ab")],
        "am",
        [("job.195",), ("job.208",)],
        "ON_HOLD",
    ),
    "a box run's end drops a member's arm": (
        oracle_inputs.BOX_JIL,
        _box_steps("an auto_hold member armed, and its box's kill drops the arm", 3),
        "bx_ah",
        [("job.434",), ("job.197",)],
        "SCHED_DISARM",
    ),
    "a box set INACTIVE takes its running member along": (
        _BOX_JIL,
        _CASCADE,
        "im",
        [("job.445",), ("job.435",)],
        "RUNNING->INACTIVE",
    ),
    "a box set INACTIVE takes its ended member along": (
        _BOX_JIL,
        _CASCADE,
        "in",
        [("job.445",), ("job.435",)],
        "SUCCESS->INACTIVE",
    ),
    "a box set INACTIVE leaves its INACTIVE member": (
        _BOX_JIL,
        _CASCADE,
        "iz",
        [("job.196",), ("job.435",)],
        "",
    ),
    "a box set INACTIVE takes its queued member along": (
        oracle_inputs.BOX_JIL,
        _box_steps("a queued member goes INACTIVE with its box", 3),
        "bx_qm",
        [("job.445",), ("job.435",)],
        "QUE_WAIT->INACTIVE",
    ),
    "a box taken OFF_NOEXEC clears its ON_NOEXEC member": (
        _BOX_JIL,
        _OFF_NOEXEC,
        "nn",
        [("job.211",)],
        "OFF_NOEXEC",
    ),
    "a box taken OFF_NOEXEC leaves a member already off": (
        _BOX_JIL,
        _OFF_NOEXEC,
        "nm",
        [("job.229",)],
        "",
    ),
    "a box put ON_NOEXEC flags its member": (
        _BOX_JIL,
        _ON_NOEXEC,
        "nn",
        [("job.231",)],
        "ON_NOEXEC",
    ),
    "a box put ON_NOEXEC flags its held member and takes its hold off": (
        _BOX_JIL,
        _ON_NOEXEC,
        "nm",
        [("job.232", "job.231")],
        "OFF_HOLD",
    ),
    "a box put ON_NOEXEC flags its member already ON_NOEXEC again": (
        _BOX_JIL,
        _ON_NOEXEC_FLAGGED,
        "nm",
        [("job.233",)],
        "ON_NOEXEC",
    ),
    "the directed flag estate reaches a flagged and held job under its box's ON_NOEXEC": (
        oracle_inputs.FLAG_JIL,
        _flag_steps("ON_NOEXEC on a box over a flagged and held job", 3),
        "fl_qs",
        [("job.232", "job.233")],
        "OFF_HOLD",
    ),
    "a box put ON_NOEXEC takes the hold off its member already ON_NOEXEC": (
        _BOX_JIL,
        _ON_NOEXEC_FLAGGED,
        "nn",
        [("job.232", "job.233")],
        "OFF_HOLD",
    ),
}


@pytest.mark.parametrize("case", sorted(_BOX_OUTCOMES))
def test_each_box_rule_outcome_fires_its_rows_and_writes_its_line(
    case: str,
) -> None:
    """Each box rule is dispatched to the instance it moves, where the
    cascade runs the rule: the rows the chart fires are the outcome the rule
    reaches, its line is written, and every instance agrees with its row."""
    jil, steps, job, rows, line = _BOX_OUTCOMES[case]
    paired = Oracle(lower_source(jil))
    for step in steps[:-1]:
        paired.feed(step)
    seen = _spied(paired)
    traced = len(paired.trace())
    paired.feed(steps[-1])
    _agrees(paired)
    key = RuntimeState.job_key(job)
    mine = [o for o in seen if o.key == key and _box_item(o) and o.rows]
    assert [o.rows for o in mine] == rows
    lines = [t.transition for t in paired.trace()[traced:] if t.job == job]
    assert (line in lines) if line else lines == []


def _opening(cls: type[Oracle] = Oracle) -> tuple[Oracle, CarriedRows]:
    """A period whose catalog drops `k`, which holds its unit of KEPT
    after a FAILURE, and the rows it carries."""
    first = Oracle(lower_source(_JIL))
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
    k = "insert_job: k\njob_type: c\ncommand: x\nmachine: m1\n"
    without_k = _JIL.replace(f"{k}resources: (KEPT, QUANTITY=1, FREE=N)\n\n", "")
    assert without_k != _JIL
    return cls(lower_source(without_k), carried=carried), carried


def test_dl256_the_period_opening_gives_a_removed_jobs_units_back_through_the_chart() -> None:
    """The period's first input gives back the units of a carried row whose
    job the catalog no longer defines (`first input of a period`, job.308),
    with its line."""
    oracle, carried = _opening()
    paired = Oracle(oracle.catalog, carried=carried)
    seen = _spied(paired)
    paired.feed(ev("STARTJOB", 3, job="j"))
    _agrees(paired)
    assert [o.rows for o in seen if o.key == "job:k" and _box_item(o)] == [("job.308",)]
    [line] = [t for t in paired.trace() if t.job == "k"]
    assert line.transition == "RELEASE_RESOURCE"
    assert line.cause.startswith("job removed from the catalog")
    assert paired.store.job["k"].reservations == ()


_KEPT_JIL = (
    "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1, FREE=N)\n\n"
    "insert_job: k\njob_type: c\ncommand: y\nmachine: m1\nresources: (LOCK, QUANTITY=1, FREE=Y)\n"
)


def test_dl256_a_start_with_no_demand_takes_over_held_units_and_holds_nothing() -> None:
    """A period opens over `j`, which still holds the unit of LOCK its failed
    run kept, and the new catalog keeps `j` but gives it no resources. `k`
    waits for LOCK. `j`'s next start is admitted with an empty demand vector:
    the takeover's `none` branch (job.302, then job.310, job.309's guard
    false) gives the held unit back, `j` runs on nothing, and the scan the
    freed unit owes admits `k` (DL-256)."""
    first = Oracle(lower_source(_KEPT_JIL))
    first.feed(ev("STARTJOB", 1, job="j"))
    first.feed(ev("STATUS", 2, job="j", status="FAILURE"))
    assert first.store.job["j"].reservations != ()
    store = first.store
    carried = CarriedRows(
        jobs=dict(store.job),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        now=T0 + timedelta(minutes=2),
    )
    without = _KEPT_JIL.replace(
        "machine: m1\nresources: (LOCK, QUANTITY=1, FREE=N)\n", "machine: m1\n"
    )
    assert without != _KEPT_JIL
    oracle = Oracle(lower_source(without), carried=carried)
    oracle.feed(ev("STARTJOB", 3, job="k"))
    assert oracle.store.job["k"].status == "QUE_WAIT"
    seen = _spied(oracle)
    oracle.feed(ev("STARTJOB", 4, job="j"))
    _agrees(oracle)
    assert ("job.302",) in [o.rows for o in seen if o.key == "job:j"]
    assert ("job.310",) in [o.branches for o in seen if o.key == "job:j"]
    j, k = oracle.store.job["j"], oracle.store.job["k"]
    assert (j.status, j.reservations, k.status) == ("RUNNING", (), "RUNNING")
    assert [(r.bucket, r.units) for r in k.reservations] == [("r:LOCK", 1)]


def _box_oracle(cls: type[Oracle] = Oracle) -> Oracle:
    return cls(lower_source(_BOX_JIL))


def test_sem12_a_box_rule_computes_only_the_facts_the_rule_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member's start is no completion moment and names nothing in the
    box's box_success, so its door reads no override; its fold reads that a
    member is not done, and no vote. The member's SUCCESS is a completion
    moment: the override is read once, and unmet it holds the default."""
    oracle = _box_oracle()
    reads: list[str] = []
    cond_true = oracle._cond_true

    def counted(cond: Any, evaluator: str) -> bool:
        reads.append(evaluator)
        return cond_true(cond, evaluator)

    monkeypatch.setattr(oracle, "_cond_true", counted)
    seen = _spied(oracle)
    oracle.feed(_box_ev(0, "FORCE_STARTJOB", "lz"))
    assert "lz" not in reads
    visits = [o.event.payload for o in seen if o.key == "job:lz" and o.event.name == "member moves"]
    # the member's STARTING folds alone; its RUNNING reads the overrides, then folds
    assert [(d.overrides, d.folds) for d in visits] == [(False, True), (True, True)]
    with pytest.raises(StatechartError, match="overrides read before their phase"):
        _ = visits[0].override
    assert repr(visits[1].override) == "OverrideFacts('lz', target=None)"
    # each visit's fold is its own, built at its phase's entry
    assert visits[0].fold is not visits[1].fold
    assert [d.fold.known("done") for d in visits] == [False, False]
    oracle.feed(_box_ev(1, "STATUS", "lzm", status="SUCCESS"))
    assert reads.count("lz") == 1
    assert oracle.store.job["lz"].status == "RUNNING"


# ---- the job_terminator cascade's run (DL-348)

#: `bx` starts again on its own end; `m` carries both terminators, `k` the
#: job_terminator alone
_SELF_RESTART_JIL = _boxed("bx", "condition: d(bx)") + _job(
    "m", "bx", "box_terminator: 1\njob_terminator: 1"
)
_CARRIED_JIL = _SELF_RESTART_JIL + _job("k", "bx", "job_terminator: 1")

#: the estate, the input after FORCE_STARTJOB bx, and each job's status and
#: run number after it: the box's second run, the member it started, and the
#: member carried RUNNING from the run that ended
_SELF_RESTART_ENDS: Final = {
    "the member's FAILURE ends the box": (
        _SELF_RESTART_JIL,
        ev("STATUS", 2, job="m", status="FAILURE"),
        {"bx": ("RUNNING", 2), "m": ("RUNNING", 2)},
    ),
    "the member's FAILURE ends the box, another member carried": (
        _CARRIED_JIL,
        ev("STATUS", 2, job="m", status="FAILURE"),
        {"bx": ("RUNNING", 2), "m": ("RUNNING", 2), "k": ("RUNNING", 1)},
    ),
    "a KILLJOB ends the box, both members carried": (
        _CARRIED_JIL,
        ev("KILLJOB", 2, job="bx"),
        {"bx": ("RUNNING", 2), "m": ("RUNNING", 1), "k": ("RUNNING", 1)},
    ),
}


@pytest.mark.parametrize("case", sorted(_SELF_RESTART_ENDS))
def test_sem14_the_job_terminator_cascade_reaches_no_run_the_box_started_again(
    case: str,
) -> None:
    """The box's TERMINATED meets `d(bx)` and starts it again inside the
    write's wakes. Its run moved, so the kill sends `box ends FAILURE or
    TERMINATED` to no member: not to the member the new run started, nor to
    the one still RUNNING from the run that ended. The oracle completes,
    where it passed the recursion limit before (DL-348)."""
    jil, end, final = _SELF_RESTART_ENDS[case]
    paired = Oracle(lower_source(jil))
    paired.feed(ev("FORCE_STARTJOB", 1, job="bx"))
    seen = _spied(paired)
    traced = len(paired.trace())
    paired.feed(end)
    _agrees(paired)
    rows = paired.store.job
    assert {job: (rows[job].status, rows[job].run_number) for job in final} == final
    assert not [o for o in seen if o.event.name == "box ends FAILURE or TERMINATED"]
    assert all("job_terminator" not in t.cause for t in paired.trace()[traced:])


_KILLED_MEMBER = _job("a", "bx", "job_terminator: 1")
_BOTH_TERMINATORS = _job("b", "bx", "box_terminator: 1\njob_terminator: 1")

#: a box the kill of its member `a` starts again, and the estate's jobs'
#: status and run number after FORCE_STARTJOB bx and KILLJOB bx: `a` killed in
#: run 1 and started by run 2, `b` carried RUNNING from run 1
_MID_CASCADE: Final = {
    "the box waits on its member's TERMINATED; the other carries both terminators": (
        _boxed("bx", "condition: t(a)") + _KILLED_MEMBER + _BOTH_TERMINATORS,
        {"bx": ("RUNNING", 2), "a": ("RUNNING", 2), "b": ("RUNNING", 1)},
    ),
    "the box waits on its member's TERMINATED; the other carries the job_terminator": (
        _boxed("bx", "condition: t(a)") + _KILLED_MEMBER + _job("b", "bx", "job_terminator: 1"),
        {"bx": ("RUNNING", 2), "a": ("RUNNING", 2), "b": ("RUNNING", 1)},
    ),
    "an empty box the member's TERMINATED starts completes and starts the box": (
        _boxed("z", "condition: t(a)")
        + _boxed("bx", "condition: s(z)")
        + _KILLED_MEMBER
        + _BOTH_TERMINATORS,
        {"z": ("SUCCESS", 1), "bx": ("RUNNING", 2), "a": ("RUNNING", 2), "b": ("RUNNING", 1)},
    ),
}


@pytest.mark.parametrize("case", sorted(_MID_CASCADE))
def test_sem14_a_kill_that_starts_the_box_again_ends_the_job_terminator_cascade(
    case: str,
) -> None:
    """The box's kill writes TERMINATED in run 1 and its job_terminator
    cascade kills `a`, whose TERMINATED starts the box again (directly, or
    through an empty box that completes at its start). The box's run number
    is compared before each member, so the cascade stops there and `b`,
    carried RUNNING into run 2, is not killed. Before, the cascade went on to
    `b`: a wrong kill, or with b's box_terminator a loop to the recursion
    limit (DL-348)."""
    jil, final = _MID_CASCADE[case]
    paired = _driven(jil, [ev("FORCE_STARTJOB", 1, job="bx"), ev("KILLJOB", 2, job="bx")])
    rows = paired.store.job
    assert {job: (rows[job].status, rows[job].run_number) for job in final} == final
    killed = [t.job for t in paired.trace() if t.cause.startswith("job_terminator")]
    assert killed == ["a"]


def test_sem14_a_fold_that_reads_a_later_runs_own_end_sends_it_to_members_that_ignore_it() -> None:
    """The box_failure override ends run 1 (`f(m1)`), and `f(bx)` starts the
    box again in that end's wakes. Run 2's empty subbox completes FAILURE at
    its start, and its box_terminator ends run 2, whose cascade kills `k`,
    carried RUNNING from run 1 into the run that ended. Run 1's
    The box's completion then reads run 2's TERMINATED and sends `box ends` to
    every member again; each ignores it (job.197), and nothing more moves."""
    jil = (
        _job("ext")
        + _boxed("bx", "condition: f(bx)\nbox_failure: f(m1)")
        + _job("m1", "bx")
        + _job("k", "bx", "job_terminator: 1")
        + _boxed("sb", "box_name: bx\nbox_terminator: 1\nbox_failure: f(ext)")
    )
    paired = Oracle(lower_source(jil))
    paired.feed(ev("FORCE_STARTJOB", 1, job="bx"))
    paired.feed(ev("STATUS", 2, job="ext", status="FAILURE"))
    seen = _spied(paired)
    paired.feed(ev("STATUS", 3, job="m1", status="FAILURE"))
    _agrees(paired)
    rows = paired.store.job
    assert {job: (rows[job].status, rows[job].run_number) for job in ("bx", "m1", "k", "sb")} == {
        "bx": ("TERMINATED", 2),
        "m1": ("RUNNING", 2),
        "k": ("TERMINATED", 1),
        "sb": ("FAILURE", 2),
    }
    ends = [
        (o.key[len("job:") :], o.rows[0])
        for o in seen
        if o.event.name == "box ends FAILURE or TERMINATED" and o.rows
    ]
    # run 2's own cascade, then run 1's read of run 2's end
    assert ends == [
        ("m1", "job.197"),
        ("k", "job.116"),
        ("sb", "job.197"),
        ("m1", "job.197"),
        ("k", "job.197"),
        ("sb", "job.197"),
    ]
