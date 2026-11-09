"""Synthetic scheduler composition; inert adapters receive injected STATUS events."""

from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from bisim_harness import EngineHarness

from dsl41.ast_jil import parse, render, render_canonical
from dsl41.conditions import And, StatusAtom
from dsl41.ir import ExecSpec, FwSpec, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event

FIXTURE = Path(__file__).parent / "fixtures" / "receive_process_load.jil"
ROOT = "file_pipeline"
RECEIVE = "receive"
LEFT = "process_left"
RIGHT = "process_right"
LOAD = "db_load"
JOBS = (ROOT, RECEIVE, LEFT, RIGHT, LOAD)
ORDERS = ((LEFT, RIGHT), (RIGHT, LEFT))
T0 = datetime(2026, 1, 1)


def fixture_text() -> str:
    return FIXTURE.read_bytes().decode("utf-8")


def test_fixture_fidelity_and_strict_lowering() -> None:
    """F1/F2 and SEM-02/03/10: exact types, membership and SUCCESS conjunction."""
    text = fixture_text()
    ast = parse(text, file=FIXTURE.name)
    assert render(ast) == text
    canonical = render_canonical(ast)
    assert render_canonical(parse(canonical, file=FIXTURE.name)) == canonical
    catalog = lower_source(text, file=FIXTURE.name)
    assert {name: job.job_type for name, job in catalog.jobs.items()} == {
        ROOT: "BOX",
        RECEIVE: "FW",
        LEFT: "CMD",
        RIGHT: "CMD",
        LOAD: "CMD",
    }
    assert {name: job.box.box_name for name, job in catalog.jobs.items()} == {
        ROOT: None,
        RECEIVE: ROOT,
        LEFT: ROOT,
        RIGHT: ROOT,
        LOAD: ROOT,
    }
    assert set(catalog.machines) == {"fixture_node"}
    assert catalog.machines["fixture_node"].machine_type == "a"
    assert catalog.jobs[ROOT].exec_ is None
    assert catalog.jobs[ROOT].sem.condition is None
    assert catalog.jobs[RECEIVE].sem.condition is None
    assert catalog.jobs[RECEIVE].exec_ == FwSpec(
        machine="fixture_node",
        watch_file="/synthetic/inbox/sample.ready",
        watch_interval=7,
        watch_file_min_size=1,
    )
    for name in (LEFT, RIGHT, LOAD):
        assert catalog.jobs[name].exec_ == ExecSpec(machine="fixture_node", command=":")
    for name, dependencies in ((LEFT, (RECEIVE,)), (RIGHT, (RECEIVE,)), (LOAD, (LEFT, RIGHT))):
        condition = catalog.jobs[name].sem.condition
        assert condition is not None
        if name == LOAD:
            assert isinstance(condition.cond, And)
            atoms = condition.cond.operands
        else:
            atoms = [condition.cond]
        assert len(atoms) == len(dependencies)
        for atom, dependency in zip(atoms, dependencies, strict=True):
            assert isinstance(atom, StatusAtom)
            assert atom.job.name == dependency
            assert atom.job.name in catalog.jobs
            assert atom.job.instance is None
            assert atom.status == "SUCCESS"
            assert atom.lookback is None
    for job in catalog.jobs.values():
        assert job.schedule is None
        assert job.resources == []
        assert job.passthrough == {}
        assert job.annotations == {}
        if job.exec_ is not None:
            assert job.exec_.machine in catalog.machines


@pytest.fixture(params=("oracle", "engine"))
def workflow(request: pytest.FixtureRequest) -> Iterator[Oracle | EngineHarness]:
    catalog = lower_source(fixture_text(), file=FIXTURE.name)
    runtime = Oracle(catalog) if request.param == "oracle" else EngineHarness(catalog)
    try:
        yield runtime
    finally:
        if isinstance(runtime, EngineHarness):
            runtime.close()


def starts(runtime: Oracle | EngineHarness) -> Counter[str]:
    return Counter(
        entry.job for entry in runtime.trace() if entry.transition.endswith("->STARTING")
    )


def statuses(runtime: Oracle | EngineHarness) -> dict[str, str]:
    return {name: runtime.store.job[name].status for name in JOBS}


def complete(
    runtime: Oracle | EngineHarness, job: str, minute: int, status: str = "SUCCESS"
) -> None:
    runtime.feed(
        Event(
            at=T0 + timedelta(minutes=minute),
            kind="STATUS",
            payload={
                "job": job,
                "status": status,
            },
        )
    )


def start_cycle(runtime: Oracle | EngineHarness, minute: int, previous: int) -> None:
    runtime.feed(Event(at=T0 + timedelta(minutes=minute), kind="STARTJOB", payload={"job": ROOT}))
    assert statuses(runtime) == {
        ROOT: "RUNNING",
        RECEIVE: "RUNNING",
        LEFT: "INACTIVE",
        RIGHT: "INACTIVE",
        LOAD: "INACTIVE",
    }
    expected = Counter(dict.fromkeys(JOBS, previous))
    expected.update((ROOT, RECEIVE))
    assert starts(runtime) == expected
    complete(runtime, RECEIVE, minute + 1)
    assert statuses(runtime) == {
        ROOT: "RUNNING",
        RECEIVE: "SUCCESS",
        LEFT: "RUNNING",
        RIGHT: "RUNNING",
        LOAD: "INACTIVE",
    }
    expected.update((LEFT, RIGHT))
    assert starts(runtime) == expected


@pytest.mark.parametrize("order", ORDERS)
def test_sem10_and_join_two_cycles(
    workflow: Oracle | EngineHarness, order: tuple[str, str]
) -> None:
    """SEM-01/02/03/10/11: AND waits for both; box reset clears previous SUCCESS."""
    assert statuses(workflow) == dict.fromkeys(JOBS, "INACTIVE")
    assert starts(workflow) == Counter()
    for cycle, completion_order in enumerate((order, order[::-1])):
        minute = cycle * 10
        start_cycle(workflow, minute, cycle)
        first, second = completion_order
        complete(workflow, first, minute + 2)
        assert statuses(workflow) == {
            ROOT: "RUNNING",
            RECEIVE: "SUCCESS",
            first: "SUCCESS",
            second: "RUNNING",
            LOAD: "INACTIVE",
        }
        expected = Counter(dict.fromkeys(JOBS, cycle + 1))
        expected[LOAD] = cycle
        assert starts(workflow) == expected
        complete(workflow, second, minute + 3)
        assert statuses(workflow) == {
            ROOT: "RUNNING",
            RECEIVE: "SUCCESS",
            LEFT: "SUCCESS",
            RIGHT: "SUCCESS",
            LOAD: "RUNNING",
        }
        assert starts(workflow) == Counter(dict.fromkeys(JOBS, cycle + 1))
        complete(workflow, LOAD, minute + 4)
        assert statuses(workflow) == dict.fromkeys(JOBS, "SUCCESS")
        assert starts(workflow) == Counter(dict.fromkeys(JOBS, cycle + 1))


@pytest.mark.parametrize("failed", (LEFT, RIGHT))
@pytest.mark.parametrize("order", ORDERS)
def test_sem11_failed_branch_leaves_box_running(
    workflow: Oracle | EngineHarness,
    failed: str,
    order: tuple[str, str],
) -> None:
    """SEM-02/03/10/11: a failed AND branch blocks load and the box's final fold."""
    assert statuses(workflow) == dict.fromkeys(JOBS, "INACTIVE")
    assert starts(workflow) == Counter()
    start_cycle(workflow, 0, 0)
    expected_statuses = {
        ROOT: "RUNNING",
        RECEIVE: "SUCCESS",
        LEFT: "RUNNING",
        RIGHT: "RUNNING",
        LOAD: "INACTIVE",
    }
    for minute, job in enumerate(order, start=2):
        verdict = "FAILURE" if job == failed else "SUCCESS"
        complete(workflow, job, minute, verdict)
        expected_statuses[job] = verdict
        assert statuses(workflow) == expected_statuses
        assert starts(workflow) == Counter({ROOT: 1, RECEIVE: 1, LEFT: 1, RIGHT: 1})
