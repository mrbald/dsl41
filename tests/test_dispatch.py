"""Dispatch: what a decided input starts and stops (runner-design ss4).

The engine plans from what the oracle emitted (`dispatch_plan`) and acts at once: a start writes
its `dispatch` record and launches its adapter; a kill cancels the live task. Two properties
carry it, each tested with the contrast that makes it non-vacuous:

  * **Supersession is by exact desired state**, not by generation. KILLJOB does not advance
    `run_number`, so a start for run N decided in the batch that ended run N must not launch
    (`spawn_superseded`).
  * **The ghost-run gate.** A CHANGE_STATUS-parity STARTING overwrite re-emits the status
    without advancing the run, and vendor parity launches nothing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from model_harness import _lose_the_decision_for

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobRuntime
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import dispatch_plan, spawn_superseded
from dsl41.runner_clock import VirtualClock
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import read_journal, replay_inputs
from dsl41.runner_store import Record
from dsl41.runner_startup import start_run

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


def _engine(jil: str = _SOLO_JIL, adapter: FakeAdapter | None = None) -> Engine:
    return Engine(
        lower_source(jil),
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
    )


def _dispatches(root: Path) -> list[tuple[str, int]]:
    return [
        (str(r["job"]), int(r["run_number"])) for r in read_journal(root) if r["rec"] == "dispatch"
    ]


# ------------------------------------------------------------- 1. planning


def test_planning_reads_the_ghost_run_gate() -> None:
    """A CHANGE_STATUS-parity STARTING overwrite re-emits the status without advancing the run,
    and vendor parity launches nothing. The gate decides whether a start EXISTS."""
    emitted = [_ev("STATUS", 0, job="j", status="STARTING")]
    common = dict(live={}, dispatchable=frozenset({"j"}))
    assert dispatch_plan(emitted, runs={"j": 1}, dispatched={}, **common) == [  # type: ignore[arg-type]
        ("SPAWN", "j", 1)
    ]
    assert dispatch_plan(emitted, runs={"j": 1}, dispatched={"j": 1}, **common) == []  # type: ignore[arg-type]


def test_a_terminal_with_no_live_run_plans_no_kill() -> None:
    """A kill that could only ever be superseded is not planned."""
    emitted = [_ev("STATUS", 0, job="j", status="SUCCESS")]
    common = dict(runs={"j": 1}, dispatched={"j": 1}, dispatchable=frozenset({"j"}))
    assert dispatch_plan(emitted, live={}, **common) == []  # type: ignore[arg-type]
    assert dispatch_plan(emitted, live={"j": 1}, **common) == [("KILL", "j", 1)]  # type: ignore[arg-type]


def test_two_terminal_transitions_of_one_live_run_plan_one_kill() -> None:
    """A deadline firing inside a `CHANGE_STATUS SUCCESS` batch ends one live run twice, at two
    instants; the plan kills it once (DL-232)."""
    emitted = [
        _ev("STATUS", 2, job="j", status="TERMINATED"),
        _ev("STATUS", 2.5, job="j", status="SUCCESS"),
    ]
    plan = dispatch_plan(
        emitted,
        runs={"j": 1},
        dispatched={"j": 1},
        live={"j": 1},
        dispatchable=frozenset({"j"}),
    )
    assert plan == [("KILL", "j", 1)]


def test_boxes_and_ghosts_get_no_dispatch() -> None:
    """`dispatchable` is the catalog jobs with a registered adapter. A box folds from its
    members and a CHANGE_STATUS-invented entity has no definition, so neither is ever something
    to act on; nor is an alarm."""
    emitted = [
        _ev("STATUS", 0, job="thebox", status="STARTING"),
        _ev("MUST_START_ALARM", 0, job="j"),
    ]
    assert (
        dispatch_plan(
            emitted, runs={"thebox": 1}, dispatched={}, live={}, dispatchable=frozenset({"j"})
        )
        == []
    )


def test_an_injected_inactive_on_a_running_job_plans_no_kill() -> None:
    """DL-235: CHANGE_STATUS INACTIVE on a launched run plans no kill: the process runs on and
    only its reservations release (DL-120), as the vendor's database write would leave them."""
    emitted = [_ev("STATUS", 0, job="j", status="INACTIVE")]
    plan = dispatch_plan(
        emitted, runs={"j": 1}, dispatched={"j": 1}, live={"j": 1}, dispatchable=frozenset({"j"})
    )
    assert plan == []


# --------------------------------------------------------- 2. supersession


def test_a_start_for_a_run_that_has_ended_is_superseded() -> None:
    """The reason the guard is not a run-number comparison: KILLJOB does not advance
    `run_number`, so a start for run N is still "current" by number after run N was
    TERMINATED. A version check would wave it through."""
    assert spawn_superseded(JobRuntime(status="RUNNING", run_number=1), 1) is None
    reason = spawn_superseded(JobRuntime(status="TERMINATED", run_number=1), 1)
    assert reason is not None and "already TERMINATED" in reason


def test_a_start_for_a_superseded_run_number_is_superseded() -> None:
    reason = spawn_superseded(JobRuntime(status="RUNNING", run_number=2), 1)
    assert reason is not None and "at run 2, not 1" in reason


def test_a_start_for_a_job_set_inactive_is_superseded() -> None:
    """Supersession asks whether the run is still desired running, not only whether it has
    ended. An injected INACTIVE sets the run aside without ending it (DL-232)."""
    assert spawn_superseded(JobRuntime(status="STARTING", run_number=1), 1) is None
    reason = spawn_superseded(JobRuntime(status="INACTIVE", run_number=1), 1)
    assert reason == "the job is INACTIVE: the run is no longer desired running"


def test_a_start_for_a_job_with_no_row_is_superseded() -> None:
    reason = spawn_superseded(None, 1)
    assert reason is not None and "no runtime row" in reason


def test_a_start_whose_run_has_ended_launches_nothing() -> None:
    """The wiring that acts on `spawn_superseded`: a start for a run the oracle has ended is not
    launched, and writes no `dispatch` record (`Engine.spawn`)."""
    engine = _engine()

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        engine.inject(_ev("KILLJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "TERMINATED"
        assert engine.spawn("j", 1) is False
        assert engine.live_jobs() == frozenset()

    asyncio.run(scenario())


# ---------------------------------------------- 3. the engine dispatches


def test_a_start_writes_its_dispatch_record_after_its_decision(tmp_path: Path) -> None:
    """runner-design ss7: the decision record carries the decision and its revisions only, and
    the start it implies is a `dispatch` record that follows it, naming the run's run_id."""
    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run")
    kinds = [r["rec"] for r in records]
    [decision] = [r for r in records if r["rec"] == "decision"]
    assert decision["decision"] == "applied" and decision["legacy_batch"] is False
    assert "effects" not in decision
    [dispatch] = [r for r in records if r["rec"] == "dispatch"]
    assert kinds.index("dispatch") == kinds.index("decision") + 1
    assert (dispatch["job"], dispatch["run_number"]) == ("j", 1)
    assert isinstance(dispatch["run_id"], str)


def test_cm09_a_start_is_never_launched_twice() -> None:
    """At-most-once: a second pass over the same emitted start meets the ghost-run gate and
    launches nothing."""
    engine = _engine()

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        emitted = await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        task = engine._live["j"].task
        assert engine.live_jobs() == frozenset({"j"})

        engine._dispatch(emitted)  # a second pass, deliberately
        engine._dispatch(emitted)
        assert engine._live["j"].task is task
        assert engine.oracle.store.job["j"].run_number == 1

    asyncio.run(scenario())


def test_a_kill_cancels_the_live_run(tmp_path: Path) -> None:
    """The oracle decides terminal and the shell stops the run: KILLJOB cancels the run's
    adapter task, and the journal holds no record of the kill beyond the input that decided it
    (a tethered wrapper records how its command died, ss6a)."""
    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        task = engine._live["j"].task
        engine.inject(_ev("KILLJOB", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert task.cancelled() or task.cancelling()
        assert engine.live_jobs() == frozenset()

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    records = read_journal(tmp_path / "run")
    assert {r["rec"] for r in records} <= {"segment", "input", "decision", "dispatch"}


_INACTIVE_GATE_JIL = (
    "insert_job: j\njob_type: c\ncommand: x\n\n"
    "insert_job: dep\njob_type: c\ncommand: y\ncondition: s(j)\n"
)


def test_a_completion_for_a_job_an_operator_set_inactive_is_rejected_not_applied(
    tmp_path: Path,
) -> None:
    """DL-235's other gate. A `CHANGE_STATUS INACTIVE` on a launched run
    plans no KILL, so the process runs on and its natural exit still
    arrives -- the test above holds a SPAWN; this one holds nothing, and the
    completion comes in through the stale-completion gate (ss4) instead of
    ss5's supersession. Applying it would turn the operator's INACTIVE into
    SUCCESS or FAILURE and start `dep`, undoing the reset. Mirrors
    `test_a_held_spawn_whose_job_was_set_inactive_is_retired_not_applied`
    above and `test_cm06_a_result_from_a_superseded_run_is_not_applied`
    (test_model_harness.py), whose live-vs-replayed check this restates
    with an injected INACTIVE in place of a fresh overwriting run."""
    catalog = lower_source(_INACTIVE_GATE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},  # inert: no natural exit of its own
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"

        engine._enqueue(  # forge the late natural exit as a COMPLETION
            _ev("STATUS", 2, job="j", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    row = engine.oracle.store.job["j"]
    assert (row.status, row.run_number) == ("INACTIVE", 1)
    assert engine.oracle.store.job["dep"].status == "INACTIVE"  # never started
    assert engine.live_jobs() == {"j"}  # the process is still live in the shell: no kill
    assert engine.drops and engine.drops[-1][1] == "job not live: INACTIVE"
    rejected = engine.decisions.for_index(engine.frontiers.applied_index)
    assert rejected is not None and rejected.decision == "rejected"
    assert rejected.reason == "job not live: INACTIVE"
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run")
    # a decision record with its reason, not the resume sweep's "drop" rec
    assert [r for r in records if r.get("rec") == "drop"] == []
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    assert (fresh.store.job["j"].status, fresh.store.job["j"].run_number) == ("INACTIVE", 1)
    assert fresh.store.job["dep"].status == engine.oracle.store.job["dep"].status
    assert replay.spawns == {("j", 1)}


_BOX_CASCADE_JIL = (
    "insert_job: bx\njob_type: b\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nbox_name: bx\n\n"
    "insert_job: dep\njob_type: c\ncommand: y\ncondition: s(j)\n"
)


def test_a_box_set_inactive_cascades_without_a_kill_and_its_orphans_exit_is_rejected(
    tmp_path: Path,
) -> None:
    """DL-242 over DL-235: `CHANGE_STATUS INACTIVE` on a RUNNING box sets
    every job it contains INACTIVE (SEM-18). For a launched member that is
    exactly DL-235's injected INACTIVE: no KILL is planned, the process
    runs on, and its later exit meets the stale-completion gate as "job not
    live: INACTIVE". The dependent never starts."""
    catalog = lower_source(_BOX_CASCADE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},  # inert: no natural exit of its own
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="bx"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"

        engine.inject(_ev("STATUS", 1, job="bx", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"

        engine._enqueue(  # forge the orphan's late natural exit as a COMPLETION
            _ev("STATUS", 2, job="j", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    store = engine.oracle.store.job
    assert (store["bx"].status, store["j"].status, store["j"].run_number) == (
        "INACTIVE",
        "INACTIVE",
        1,
    )
    assert store["dep"].status == "INACTIVE"  # never started
    assert engine.live_jobs() == {"j"}  # the process is still live in the shell: no kill
    assert engine.drops and engine.drops[-1][1] == "job not live: INACTIVE"
    rejected = engine.decisions.for_index(engine.frontiers.applied_index)
    assert rejected is not None and rejected.decision == "rejected"
    assert engine.journal is not None
    engine.journal.close()

    fresh = Oracle(catalog)
    replay_inputs(fresh, read_journal(tmp_path / "run"))
    assert (fresh.store.job["j"].status, fresh.store.job["dep"].status) == (
        "INACTIVE",
        "INACTIVE",
    )


def test_a_restart_on_an_inactive_row_cancels_the_orphan_and_spawns_again() -> None:
    """DL-235's M1. An injected INACTIVE leaves the earlier run live in the
    shell (no KILL), and the oracle's DL-81 refusal only blocks STARTJOB on
    a STARTING/RUNNING/QUE_WAIT row, so a restart on the INACTIVE row is
    admitted. `Engine.spawn`'s "one live attempt per job" branch is
    reached this way: the shell holds one live run per job, so the earlier
    adapter task is cancelled. A real CMD adapter
    runs its kill ladder on that cancel (runner_adapters.py); this
    FakeAdapter test pins the cancel, not the ladder. The vendor would run
    both."""
    engine = _engine(_SOLO_JIL)
    orphan: asyncio.Task[None] | None = None

    async def scenario() -> None:
        nonlocal orphan
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"
        orphan = engine._live["j"].task

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"
        assert not orphan.done()  # still live in the shell, no KILL

        engine.inject(_ev("STARTJOB", 2, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    assert orphan is not None and orphan.cancelled()  # the earlier task is gone
    row = engine.oracle.store.job["j"]
    assert (row.status, row.run_number) == ("RUNNING", 2)
    assert engine.live_jobs() == {"j"}
    assert engine._live["j"].run_number == 2  # run 2 only, nothing left of run 1
    assert engine._dispatched == {"j": 2}


def test_a_completion_on_an_inactive_row_is_rejected_and_its_decision_is_never_lost(
    tmp_path: Path,
) -> None:
    """DL-235, pinned through the store. The run-1 completion for a row an operator set
    INACTIVE is rejected by the ss4 gate, and its attempt and rejection are one transaction:
    replay reads the durable verdict and reaches the live engine's state, and `dep` pins that
    the rejection, not an accepted completion, is what reached the dependent. A store that
    holds the attempt without its decision disagrees with itself and replay refuses it."""
    catalog = lower_source(_INACTIVE_GATE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"

        engine._enqueue(  # the run-1 completion for the row set aside
            _ev("STATUS", 2, job="j", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run")
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    [seq] = [
        r["seq"]
        for r in records
        if r.get("rec") == "input" and r.get("kind") == "STATUS" and r.get("source") == "adapter"
    ]
    durable = replay.decisions.for_index(seq)
    assert durable is not None
    assert durable.decision == "rejected"
    assert durable.reason == "job not live: INACTIVE"
    assert (fresh.store.job["j"].status, fresh.store.job["j"].run_number) == ("INACTIVE", 1)
    assert fresh.store.job["dep"].status == "INACTIVE"  # never started

    _lose_the_decision_for(tmp_path / "run", "adapter")
    with pytest.raises(EngineError, match="has no decision"):
        replay_inputs(Oracle(catalog), read_journal(tmp_path / "run"))


#: a negative term_run_time arms a deadline already past due, which the
#: frontier rule fires inside the next input's batch, back-dated to its due
#: instant: two terminal transitions of one live run, at two instants, in one
#: batch
_PAST_DUE_JIL = "insert_job: x\njob_type: c\ncommand: sleep 300\nterm_run_time: -1\n"


def test_a_deadline_and_a_status_ending_one_run_in_one_batch_record_one_kill(
    tmp_path: Path,
) -> None:
    """DL-232: a deadline and a status that end one live run in one batch
    plan one kill, which cancels the run once, and the log replays."""
    catalog = lower_source(_PAST_DUE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["x"].status == "RUNNING"  # the deadline waits
        assert engine.live_jobs() == frozenset({"x"})
        engine.inject(_ev("STATUS", 0, job="x", status="SUCCESS"))
        await engine.run_until_quiescent(T0)

    cancelled: list[str] = []
    real = Engine._dispatch

    def counting(self: Engine, emitted: list[Event]) -> None:
        before = dict(self._live)
        real(self, emitted)
        cancelled.extend(job for job in before if job not in self._live)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Engine, "_dispatch", counting)
        asyncio.run(scenario())
    assert [(t.at, t.transition) for t in engine.oracle.trace()][-2:] == [
        (T0 - timedelta(minutes=1), "RUNNING->TERMINATED"),
        (T0, "TERMINATED->SUCCESS"),
    ]
    assert cancelled == ["x"]
    assert engine.live_jobs() == frozenset()
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run")
    fresh = Oracle(catalog)
    replay_inputs(fresh, records)
    assert fresh.store.job["x"].status == "SUCCESS"


@pytest.mark.parametrize("ending", ["INACTIVE", "TERMINATED"])
def test_a_starting_overwrite_after_an_ended_run_launches_nothing(
    tmp_path: Path, ending: str
) -> None:
    """DL-232: a run counts as dispatched from its plan, so a `CHANGE_STATUS STARTING` at the
    same run number meets the ghost-run gate and plans nothing. The log replays, and a resume
    rebuilds the same gate. A KILLJOB closed run 1's attempt, so the restart has nothing to
    settle; an injected INACTIVE left it running (DL-235), and the engine's stop interrupted
    it while the oracle holds run 1 in STARTING, so the interruption rule runs run 1 again as
    its second attempt (spec-wiring 2b, step 3 counts STARTING as live)."""
    from dsl41.runner_startup import resume_run

    root = tmp_path / "run"
    catalog = lower_source(_SOLO_JIL)

    async def scenario() -> None:
        engine = start_run(
            catalog, root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter(default=None)}
        )
        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        if ending == "TERMINATED":
            engine.inject(_ev("KILLJOB", 2, job="j"))
        else:
            engine.inject(_ev("STATUS", 2, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        live = engine.live_jobs()
        engine.inject(_ev("STATUS", 5, job="j", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=6))

        assert engine.live_jobs() == live  # nothing new launched
        row = engine.oracle.store.job["j"]
        assert (row.status, row.run_number) == ("STARTING", 1)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        assert _dispatches(root) == [("j", 1)]

        # resume replays the same log and rebuilds `_dispatched` from the rows
        resumed = await resume_run(
            catalog,
            root,
            clock=VirtualClock(start=T0 + timedelta(minutes=6)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        assert resumed.oracle.store.job["j"].model_dump() == row.model_dump()
        assert resumed._dispatched == engine._dispatched == {"j": 1}
        await resumed.run_until_quiescent(T0 + timedelta(minutes=7))
        interrupted = ending == "INACTIVE"
        assert resumed.live_jobs() == (frozenset({"j"}) if interrupted else frozenset())
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        assert _dispatches(root) == [("j", 1)] * (2 if interrupted else 1)

    asyncio.run(scenario())


def test_a_kill_for_a_run_that_is_no_longer_live_cancels_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kill names the run it was decided for. When an earlier item of the same plan has
    replaced the live run (a start that cancelled the stale one), the kill cancels nothing:
    stopping the newer run would stop a run nobody asked to stop."""
    import dsl41.runner as runner_mod

    engine = _engine()

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        task = engine._live["j"].task
        monkeypatch.setattr(runner_mod, "dispatch_plan", lambda *_a, **_k: [("KILL", "j", 7)])
        engine._dispatch([])
        assert engine._live["j"].task is task and not task.cancelled()

    asyncio.run(scenario())


def test_replay_derives_no_start_the_batch_itself_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay derives the starts a live engine would have launched: a start the batch's own
    end state supersedes is planned and counted, and not derived (`Replay.spawns`)."""
    import dsl41.runner_journal as journal_mod

    root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_SOLO_JIL),
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    records = read_journal(root)
    assert replay_inputs(Oracle(lower_source(_SOLO_JIL)), records).spawns == {("j", 1)}
    monkeypatch.setattr(journal_mod, "spawn_superseded", lambda *_a: "superseded")
    assert replay_inputs(Oracle(lower_source(_SOLO_JIL)), records).spawns == set()


def test_a_failed_dispatch_still_answers_the_durable_decision(tmp_path: Path) -> None:
    """The answer follows the admission's transaction, not the dispatch: a `dispatch` append
    that fails stops the engine with its own error, and the request that decided the start is
    answered `applied` all the same, never left waiting."""
    from dsl41.runner_admission import Envelope

    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None

    journal = engine.journal
    original = journal.append

    def full(*records: Record) -> int:
        if any(record.kind == "dispatch" for record in records):
            raise OSError(28, "No space left on device")
        return original(*records)

    journal.append = full  # type: ignore[method-assign]

    async def scenario() -> None:
        expect = {"job:j": engine.oracle.store.revision("job:j")}
        answer = engine.submit(
            _ev("STARTJOB", 0, job="j"), Envelope(request_id="r1", expect=expect)
        )
        with pytest.raises(OSError, match="No space left"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert (await answer).decision == "applied"
        assert engine.live_jobs() == frozenset()  # nothing launched without its record
        await engine.shutdown()

    asyncio.run(scenario())
    engine.journal.close()
