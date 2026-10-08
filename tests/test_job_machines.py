"""The job's machines and the RuntimeState assembly, declared with `state_machine.py`.

`oracle_state` declares `job_status`, `job_flags`, `job_holding` and
`runtime_assembly`; docs/state-machines.md renders them. The rule code in
`oracle.py` names the transition of every move, and the store's verbs check it.
A job move off its table is noted on the violation channel and the write still
happens; an assembly move off its table raises before the phase changes. The SEM
corpus runs every job move under the suite's strict variable (test_oracle.py),
and its autouse fixture proves each one is taken inside an InputBatch
(batch_harness). These tests pin the declarations, both sides of each check,
and the writers that seed rows without taking a transition.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import batch_harness
import pytest

from dsl41 import cli_control
from dsl41.classify import CarriedJob, CarriedState, _seeded
from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    FAILED,
    FLAG_FIELDS,
    FORCE_CLEARS_ICE,
    HELD_RELEASED,
    IDLE_BOX_DERIVE,
    IDLE_BOX_OVERRIDE,
    ICE_ON,
    JOB_FLAGS,
    JOB_HOLDING,
    JOB_RUN,
    JOB_STATUS,
    KILL_IGNORED,
    RESERVE,
    RUNTIME_ASSEMBLY,
    STATUS_INJECTED,
    TAKE_OVER_HELD,
    TERMINAL,
    AssemblyPhase,
    CapacityReservation,
    CarriedRows,
    Event,
    FlagState,
    HoldingState,
    JobRuntime,
    JobStatus,
    OracleError,
    VIOLATION_MARKER,
    RuntimeState,
)
from dsl41.runner_hosts import seed_local_executor
from dsl41.state_machine import HITS_ENV, STRICT_ENV, TransitionError, Violation, well_formed

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_LOCK = CapacityReservation(bucket="r:LOCK", units=1, release_policy="success")


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, not strict, so a deliberate violation stays out of
    the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    return directory


def _hits(directory: Path) -> set[tuple[str, str]]:
    return {
        (record["machine"], record["id"])
        for path in sorted(directory.glob("hits-*.jsonl"))
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
    }


# ------------------------------------------------------------------- declarations


def test_the_machines_are_registered_and_well_formed() -> None:
    machines = (JOB_STATUS, JOB_FLAGS, JOB_HOLDING, RUNTIME_ASSEMBLY)
    assert all(machine in MACHINES for machine in machines)
    assert {m.name: len(m.transitions) for m in machines} == {
        "job_status": 58,
        "job_flags": 17,
        "job_holding": 8,
        "runtime_assembly": 7,
    }
    assert all(well_formed(machine) == [] for machine in machines)
    assert all(t.mark is None for machine in machines for t in machine.transitions)


def test_the_state_sets_are_the_code_s_own_names() -> None:
    """The status machine's states are `JobStatus`; each flag state names one
    boolean field of the row; the other two are their own Literal types."""
    assert JOB_STATUS.states == set(get_args(JobStatus))
    assert JOB_FLAGS.states == set(get_args(FlagState.__value__)) == set(FLAG_FIELDS)
    assert JOB_HOLDING.states == set(get_args(HoldingState.__value__))
    assert RUNTIME_ASSEMBLY.states == set(get_args(AssemblyPhase.__value__))
    fields = {field for field, _ in FLAG_FIELDS.values()}
    assert fields == {"on_ice", "on_hold", "on_noexec", "armed"}
    assert all(JobRuntime.model_fields[field].annotation is bool for field in fields)


def test_failed_is_named_once() -> None:
    assert FAILED == TERMINAL - {"SUCCESS"}
    assert cli_control._QUERY_PREDICATES["is-failed"] is FAILED


# ------------------------------------------------------- a job move off its table


def test_a_status_move_off_its_table_is_noted_and_still_written(recording: Path) -> None:
    store = RuntimeState()
    store.transition("j", STATUS_INJECTED, "RUNNING", T0)  # any source, a payload target
    assert store.drain_violations() == []
    store.transition("j", JOB_RUN, "RUNNING", T0)  # RUNNING is not STARTING
    store.transition("j", STATUS_INJECTED, "QUE_WAIT", T0)  # not an injectable status
    assert store.drain_violations() == [
        (
            "j",
            Violation(
                "job_status",
                "job_status.05",
                "RUNNING",
                "RUNNING",
                "RUNNING is not a source of job_status.05",
            ),
        ),
        (
            "j",
            Violation(
                "job_status",
                "job_status.19",
                "RUNNING",
                "QUE_WAIT",
                "QUE_WAIT is not one of the targets of job_status.19",
            ),
        ),
    ]
    assert store.runtime("j").status == "QUE_WAIT"  # the check never refuses


def test_an_internal_transition_moves_nothing(recording: Path) -> None:
    store = RuntimeState()
    before = store.runtime("j")
    store.stay("j", KILL_IGNORED)
    assert store.runtime("j") is before
    assert store.drain_violations() == []
    store.transition("j", STATUS_INJECTED, "RUNNING", T0)
    store.stay("j", KILL_IGNORED)
    [(subject, violation)] = store.drain_violations()
    assert (subject, violation.reason) == ("j", "RUNNING is not a source of job_status.37")


def test_an_internal_transition_is_one_row_per_status(recording: Path) -> None:
    """An internal transition's source is its target (design: source ==
    {target}), so the table itself refuses a move that changes the status;
    the idle-box rows likewise refuse a re-derive to the status it has."""
    internal = [t for t in JOB_STATUS.transitions if int(t.id.split(".")[1]) >= 37]
    assert len(internal) == 22
    assert all(t.source == {t.target} for t in internal)
    assert JOB_STATUS.take(KILL_IGNORED["INACTIVE"], "INACTIVE", "TERMINATED") is not None
    for verdict in ("SUCCESS", "FAILURE"):
        for rows in (IDLE_BOX_OVERRIDE, IDLE_BOX_DERIVE):
            assert JOB_STATUS.take(rows[verdict], verdict, verdict) is not None


def test_a_set_target_needs_a_named_status() -> None:
    """A transition whose payload chooses the target cannot be taken
    without the status: the oracle refuses before any write."""
    oracle = Oracle(lower_source(_JIL))
    with pytest.raises(OracleError, match=r"job_status\.19 lets the payload choose"):
        oracle._set_status("j", STATUS_INJECTED, cause="no status")
    assert oracle.store.runtime("j").status == "INACTIVE"


def test_a_flag_move_off_its_table_is_noted_and_still_written(recording: Path) -> None:
    store = RuntimeState()
    store.move_flag("j", FORCE_CLEARS_ICE)  # only an iced job can be cleared by a force
    [(_, violation)] = store.drain_violations()
    assert violation.reason == "ice_off is not a source of job_flags.03"
    store.move_flag("j", ICE_ON)
    store.move_flag("j", FORCE_CLEARS_ICE)
    assert store.drain_violations() == []
    assert store.runtime("j").on_ice is False


def test_a_holding_move_off_its_table_is_noted_and_still_written(recording: Path) -> None:
    store = RuntimeState()
    store.take_over_held("j", TAKE_OVER_HELD, [_LOCK])  # nothing held to take over
    store.release_held("k", HELD_RELEASED)  # nothing held to release
    assert [(s, v.reason) for s, v in store.drain_violations()] == [
        ("j", "none is not a source of job_holding.02"),
        ("k", "none is not a source of job_holding.07"),
    ]
    assert store.runtime("j").reservations == (_LOCK,)
    store.reserve("m", [_LOCK])
    store.transition("m", STATUS_INJECTED, "FAILURE", T0)
    store.release_reservations("m", "RUNNING", "FAILURE", lambda bucket: True)
    store.take_over_held("m", TAKE_OVER_HELD, [])
    assert store.drain_violations() == []
    assert store.runtime("m").reservations == ()


def test_reserving_nothing_is_no_move(recording: Path) -> None:
    store = RuntimeState()
    store.reserve("j", [])
    store.reserve("k", [_LOCK])
    assert _hits(recording) == {("job_holding", RESERVE.id)}


# ------------------------------------------------- the writers that take nothing


def test_the_harness_sees_a_move_outside_an_input_batch(
    monkeypatch: pytest.MonkeyPatch, recording: Path
) -> None:
    """The proof in test_oracle.py's fixture is not vacuous: a move in a bare
    store transaction is caught, and so is a channel left full at
    `begin_input`. An InputBatch's moves are not."""
    misplaced = batch_harness.install(monkeypatch)
    oracle = Oracle(lower_source(_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    assert misplaced == []
    store = oracle.store
    store.begin_input()
    store.move_flag("j", ICE_ON)
    store.commit_input()
    store.note_violation("j", Violation("job_flags", "x", "a", "b", "orphan"))
    store.begin_input()
    store.commit_input()
    assert misplaced == [
        "a move of j was taken outside an InputBatch",
        "a move of j was taken outside an InputBatch",
        "the channel held [('j', Violation(machine='job_flags', transition='x', old='a',"
        " new='b', reason='orphan'))] at begin_input",
    ]


def test_the_seeds_take_no_job_transition(monkeypatch: pytest.MonkeyPatch, recording: Path) -> None:
    """The constructor's genesis seed, the carried-row install, classify's
    seeding and the executor seed open their own store transaction, so none
    may take a job transition. Only the assembly moves are recorded."""
    misplaced = batch_harness.install(monkeypatch)
    catalog = lower_source(_JIL + "status: ON_HOLD\n\ninsert_job: gone\njob_type: c\ncommand: y\n")
    carried = CarriedRows(jobs={"gone": JobRuntime(status="SUCCESS")}, period_id=2)
    oracle = Oracle(catalog, carried=carried)
    seed_local_executor(oracle.store, "local", at=T0)
    _seeded(catalog, CarriedState(jobs={"j": CarriedJob(row=JobRuntime(status="QUE_WAIT"))}))
    assert oracle.store.runtime("j").on_hold is True
    assert misplaced == []
    assert {machine for machine, _ in _hits(recording)} == {"runtime_assembly"}


def test_classify_rebuilds_carried_rows_as_installs() -> None:
    """The row each carried job gets is the row a sequence of transitions
    would have left: a terminal row's last end is its status time, a row
    that ended and started again keeps its earlier end, and a waiter gets a
    rank."""
    end = T0 - timedelta(hours=1)
    rows = {
        "done": JobRuntime(status="FAILURE", status_at=T0, last_end_at=end, exit_code=3),
        "again": JobRuntime(status="RUNNING", status_at=T0, last_end_at=end, on_ice=True),
        "queued": JobRuntime(status="QUE_WAIT", status_at=T0, armed=True, on_hold=True),
    }
    catalog = lower_source(
        "".join(f"insert_job: {name}\njob_type: c\ncommand: x\n\n" for name in rows)
    )
    carried = CarriedState(jobs={name: CarriedJob(row=row) for name, row in rows.items()})
    store = _seeded(catalog, carried).store
    lifecycle = (
        "status",
        "status_at",
        "last_end_at",
        "exit_code",
        "on_ice",
        "on_hold",
        "on_noexec",
        "armed",
        "waiter_seq",
    )
    got = {name: tuple(getattr(store.job[name], f) for f in lifecycle) for name in rows}
    assert got == {
        "done": ("FAILURE", T0, T0, 3, False, False, False, False, None),
        "again": ("RUNNING", T0, end, None, True, False, False, False, None),
        "queued": ("QUE_WAIT", T0, None, None, False, True, False, True, 1),
    }


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
    assert len(_hits(recording)) == len(RUNTIME_ASSEMBLY.transitions)


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
    genesis input, are no rows of the table. Assembly is not an input, so
    the violation has no channel: the move raises."""
    store = RuntimeState()
    for name in setup:
        getattr(store, name)()
    phase = store._phase
    with pytest.raises(OracleError, match=message):
        getattr(store, move)()
    assert store._phase == phase


def test_a_strict_session_raises_the_same_assembly_move_as_a_transition_error(
    monkeypatch: pytest.MonkeyPatch, recording: Path
) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    store = RuntimeState()
    with pytest.raises(TransitionError, match=r"runtime_assembly\.07"):
        store.commit_input()
    assert store._phase == "fresh"


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


def test_every_ignorable_event_in_every_reachable_state_takes_a_declared_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a plain job, a resource-bearing job, a box and a resource-bearing
    box, every status, ice or hold, and a box's member idle, iced, running
    or queued: build each state this builder reaches (not every reachable
    state), then send every operator verb under the strict variable, which
    the test sets itself. Each verb either takes a declared transition or
    is refused by the oracle's own rule (OracleError); a TransitionError
    means a table row is missing. The ignore conditions are the code's own
    (`_oob_ignored`, `_attempt_start`, the KILLJOB dispatch), met by building
    the states, not restated here."""
    monkeypatch.setenv(STRICT_ENV, "1")
    statuses = ("INACTIVE", "QUE_WAIT", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
    reached: set[tuple[object, ...]] = set()
    missing: list[str] = []
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
                        except TransitionError as exc:
                            missing.append(f"{verb} on {state}: {exc}")
                        if any(e.transition == VIOLATION_MARKER for e in twin.trace()):
                            missing.append(f"{verb} on {state}: a violation line was traced")
    assert missing == []
    assert len(reached) >= 100 and applied == len(reached) * len(_VERBS)
