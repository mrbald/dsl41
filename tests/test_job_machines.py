"""The job's flag states and the RuntimeState assembly.

The `job` chart (tests/test_job_chart.py) decides every move of a job row,
and the oracle writes what it reached through the store's move verbs;
`runtime_assembly` is a statechart (tests/test_assembly_chart.py).
docs/state-machines.md renders both. The SEM corpus runs every move inside
an InputBatch, and its autouse fixture proves it (batch_harness). These tests
pin the flag states' names, the harness, the writers that seed rows without
a move, the assembly's walk and its refusals, and every operator verb in
every state a builder reaches.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import batch_harness
import pytest

from dsl41 import cli_control
from dsl41.ir import lower_source
from dsl41.job_chart import JOB
from dsl41.job_chart_rows import configuration
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    FAILED,
    FLAG_FIELDS,
    JOB_STATUSES,
    RUNTIME_ASSEMBLY,
    TERMINAL,
    CarriedRows,
    Event,
    FlagState,
    JobRuntime,
    OracleError,
    RuntimeState,
)
from dsl41.runner_hosts import seed_local_executor
from dsl41.state_machine import HITS_ENV

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\n"


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, so a test's own hits stay out of the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


def _hits(directory: Path) -> set[tuple[str, str]]:
    return {
        (record["machine"], record["id"])
        for path in sorted(directory.glob("hits-*.jsonl"))
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
    }


# ------------------------------------------------------------------- declarations


def test_the_job_chart_and_the_assembly_are_registered() -> None:
    assert JOB in MACHINES and RUNTIME_ASSEMBLY in MACHINES
    assert not {"job_status", "job_flags", "job_holding"} & {m.name for m in MACHINES}


def test_the_flag_states_are_the_chart_s_and_name_the_row_s_fields() -> None:
    """Each flag state names one boolean field of the row and its value; each but the arm's is
    a state of the chart's `flags` region, and the arm is a fact of the payloads that read it
    (DL-365). Each status is a state of the chart's `status` region."""
    assert set(get_args(FlagState.__value__)) == set(FLAG_FIELDS)
    every_flag_on = configuration("INACTIVE", ice=True, hold=True, noexec=True)
    assert set(FLAG_FIELDS) - {"arm_off", "arm_on"} == {
        state for state in configuration("INACTIVE") | every_flag_on if "_" in state
    }
    fields = {field for field, _ in FLAG_FIELDS.values()}
    assert fields == {"on_ice", "on_hold", "on_noexec", "armed"}
    assert all(JobRuntime.model_fields[field].annotation is bool for field in fields)
    assert all(status in configuration(status) for status in JOB_STATUSES)


def test_failed_is_named_once() -> None:
    assert FAILED == TERMINAL - {"SUCCESS"}
    assert cli_control._QUERY_PREDICATES["is-failed"] is FAILED


# ------------------------------------------------- the writers that take nothing


def test_the_harness_sees_a_move_outside_an_input_batch(
    monkeypatch: pytest.MonkeyPatch, recording: Path
) -> None:
    """The proof in test_oracle.py's fixture is not vacuous: a move in a bare
    store transaction is caught. An InputBatch's moves are not. The bare
    transaction is a store's of its own: the oracle's store checks its job
    instances at each commit, which a bare move would fail first."""
    misplaced = batch_harness.install(monkeypatch)
    oracle = Oracle(lower_source(_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert misplaced == []
    store = RuntimeState()
    store.begin_input()
    store.move_flag("j", "ice_on")
    store.commit_input()
    store.transition("j", "RUNNING", T0)
    assert misplaced == [
        "a move of j was written outside an InputBatch",
        "a move of j was written outside an InputBatch",
    ]


def test_the_seeds_take_no_job_transition(monkeypatch: pytest.MonkeyPatch, recording: Path) -> None:
    """The constructor's genesis seed, the carried-row install and the
    executor seed open their own store transaction, so none may write a job
    move. Only the assembly moves are recorded."""
    misplaced = batch_harness.install(monkeypatch)
    catalog = lower_source(_JIL + "status: ON_HOLD\n\ninsert_job: gone\njob_type: c\ncommand: y\n")
    carried = CarriedRows(jobs={"gone": JobRuntime(status="SUCCESS")}, period_id=2)
    oracle = Oracle(catalog, carried=carried)
    seed_local_executor(oracle.store, "local", at=T0)
    assert oracle.store.runtime("j").on_hold is True
    assert misplaced == []
    assert {machine for machine, _ in _hits(recording)} == {"runtime_assembly"}


# ------------------------------------------------------------------- assembly


def test_assembly_walks_its_table(recording: Path) -> None:
    store = RuntimeState()
    phases: list[str] = [store._phase]
    for step in (
        lambda: store.install(CarriedRows()),
        store.begin_input,
        store.commit_input,
        store.finish_genesis,
        lambda: store.seed_period(2),
        store.begin_input,
        store.commit_input,
        store.begin_input,
        store.commit_input,
    ):
        step()
        phases.append(store._phase)
    assert phases == [
        "fresh",
        "installed",
        "genesis_input",
        "genesis",
        "constructed",
        "seeded",
        "input",
        "live",
        "input",
        "live",
    ]
    assert _hits(recording) == {
        ("runtime_assembly", f"runtime_assembly.{n:02d}") for n in (1, 3, 4, 7, 13, 41, 42)
    }


def test_a_second_install_is_refused() -> None:
    store = RuntimeState()
    store.install(CarriedRows())
    with pytest.raises(ValueError, match="install on a used state"):
        store.install(CarriedRows())
    assert store._phase == "installed"


@pytest.mark.parametrize(
    ("setup", "move", "message"),
    [
        ((), "commit_input", "commit_input in assembly phase fresh: fresh is not a source"),
        (
            ("begin_input",),
            "finish_genesis",
            "finish_genesis in assembly phase genesis_input: genesis_input is not a source",
        ),
    ],
)
def test_an_assembly_move_off_its_table_raises_before_the_phase_moves(
    recording: Path, setup: tuple[str, ...], move: str, message: str
) -> None:
    """A commit with no input open, and the end of construction inside the
    genesis input, are refusal rows of the chart. Assembly is not an input, so
    a refusal has no channel: the verb raises."""
    store = RuntimeState()
    for name in setup:
        getattr(store, name)()
    phase = store._phase
    with pytest.raises(OracleError, match=message):
        getattr(store, move)()
    assert store._phase == phase


# ------------------------------------------- every ignorable event, every state

_EXHAUSTIVE_JIL = (
    "insert_resource: XL\nres_type: R\namount: 1\n\n"
    "insert_job: xh\njob_type: c\ncommand: h\nmachine: m1\nresources: (XL, QUANTITY=1)\n\n"
    "insert_job: xj\njob_type: c\ncommand: j\nmachine: m1\n\n"
    "insert_job: xq\njob_type: c\ncommand: q\nmachine: m1\nresources: (XL, QUANTITY=1)\n\n"
    "insert_job: xb\njob_type: b\n\n"
    "insert_job: xbm\njob_type: c\ncommand: b\nmachine: m1\nbox_name: xb\n"
    "resources: (XL, QUANTITY=1)\n\n"
    "insert_job: xr\njob_type: b\nresources: (XL, QUANTITY=1)\n\n"
    "insert_job: xrm\njob_type: c\ncommand: r\nmachine: m1\nbox_name: xr\n"
)
#: the target jobs: a plain job, a resource-bearing job, a box with a
#: resource-bearing member, and a resource-bearing box, each with its member
_TARGETS = {"xj": None, "xq": None, "xb": "xbm", "xr": "xrm"}
#: every operator verb the oracle dispatches for a job
_VERBS: tuple[str, ...] = (
    "KILLJOB",
    "STARTJOB",
    "FORCE_STARTJOB",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "RELEASE_RESOURCE",
)


def _build(job: str, status: str, flag: str | None, member: str | None) -> Oracle:
    """Feed the inputs that try to reach this state; the caller reads what was reached."""
    oracle = Oracle(lower_source(_EXHAUSTIVE_JIL))
    at = [T0]

    def feed(kind: str, target: str, **payload: object) -> None:
        at[0] += timedelta(minutes=1)
        oracle.feed(Event(at=at[0], kind=kind, payload={"job": target, **payload}))  # type: ignore[arg-type]

    inner = _TARGETS[job]
    if inner is not None and member is not None:
        if member == "QUE_WAIT":  # a member queues only inside a running box
            feed("STATUS", job, status="RUNNING")
            feed("STARTJOB", "xh")
            feed("STARTJOB", inner)
        elif member == "ON_ICE":
            feed("ON_ICE", inner)
        else:
            feed("STATUS", inner, status=member)
    if flag is not None:
        feed(flag, job)
    if status == "QUE_WAIT":
        feed("STARTJOB", "xh")
        feed("STARTJOB", job)
    elif status != "INACTIVE":
        feed("STATUS", job, status=status)
    return oracle


def test_every_ignorable_event_in_every_reachable_state_takes_a_declared_row() -> None:
    """For a plain job, a resource-bearing job, a box and a resource-bearing
    box, every status, ice or hold, and a box's member idle, iced, running
    or queued: build each state this builder reaches (not every reachable
    state), then send every operator verb. Each verb either takes a declared
    row of the `job` chart or is refused by the oracle's own rule
    (OracleError); a cell the chart has no row for raises a
    `StatechartError`, which fails the test. The ignore conditions are the
    code's own (`_oob_ignored`, `job_chart.start_verdict`, the KILLJOB
    dispatch), met by building the states, not restated here."""
    statuses = ("INACTIVE", "QUE_WAIT", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
    reached: set[tuple[object, ...]] = set()
    applied = 0
    for job, inner in _TARGETS.items():
        members = (None,) if inner is None else (None, "ON_ICE", "RUNNING", "QUE_WAIT")
        for status in statuses:
            for flag in (None, "ON_ICE", "ON_HOLD"):
                for member in members:
                    base = _build(job, status, flag, member)
                    row = base.store.job[job]
                    state: tuple[object, ...] = (job, row.status, row.on_ice, row.on_hold)
                    if inner is not None:
                        m = base.store.job[inner]
                        state += (m.status, m.on_ice)
                    if state in reached:
                        continue
                    reached.add(state)
                    for verb in _VERBS:
                        twin = base.fork()
                        applied += 1
                        try:
                            twin.feed(
                                Event(at=T0 + timedelta(days=1), kind=verb, payload={"job": job})
                            )  # type: ignore[arg-type]
                        except OracleError:
                            pass  # the oracle's own refusal
    assert len(reached) >= 100 and applied == len(reached) * len(_VERBS)
