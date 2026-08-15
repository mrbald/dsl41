"""Stage H: the deterministic model harness and its checkers.

Two kinds of test live here. The `test_harness_*` ones pin that the
CHECKERS can fail -- a checker that cannot report a violation makes every
test that calls it green forever, which is the specific way a safety
harness rots. The `test_cmNN_*` ones are the obligations of
docs/concurrency-model.md ss9 that today's single-host code can actually
be held to; the rest arrive with S1c..S6.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from model_harness import (
    ModelRun,
    Spawn,
    SpawnLog,
    check,
    cm09_overlapping_runs,
    cm14_double_spawns,
)

from dsl41.oracle_state import Event

MODEL_JIL = """\
insert_job: solo
job_type: c
command: run-solo

insert_job: bx
job_type: b

insert_job: alpha
job_type: c
command: run-alpha
box_name: bx

insert_job: beta
job_type: c
command: run-beta
box_name: bx
condition: s(alpha)
"""

WATCH_JIL = """\
insert_job: watcher
job_type: f
watch_file: /tmp/dsl41-model-harness-never-appears
"""

T0 = datetime(2026, 1, 1, 0, 0, 0)


def _spawn(job: str, run_number: int, start: int, end: int | None, *, seq: int = 0) -> Spawn:
    return Spawn(
        seq=seq,
        executor_id="local",
        job=job,
        run_number=run_number,
        incarnation=0,
        started_at=T0 + timedelta(seconds=start),
        ended_at=None if end is None else T0 + timedelta(seconds=end),
    )


# --------------------------------------------------- the checkers have teeth


def test_harness_cm14_reports_a_second_run_of_one_run_number() -> None:
    log = SpawnLog(spawns=[_spawn("alpha", 1, 0, 5, seq=0), _spawn("alpha", 1, 9, 12, seq=1)])
    (violation,) = cm14_double_spawns(log)
    assert "alpha run 1 ran twice" in violation
    assert "engine incarnation 0" in violation  # names WHERE, not just THAT


def test_harness_cm14_accepts_distinct_runs() -> None:
    log = SpawnLog(spawns=[_spawn("alpha", 1, 0, 5, seq=0), _spawn("alpha", 2, 9, 12, seq=1)])
    assert cm14_double_spawns(log) == []


def test_harness_cm09_reports_overlap_and_abandonment() -> None:
    overlapping = SpawnLog(
        spawns=[_spawn("alpha", 1, 0, 10, seq=0), _spawn("alpha", 2, 5, 12, seq=1)]
    )
    (violation,) = cm09_overlapping_runs(overlapping)
    assert "overlapping run 2" in violation

    # a run that never ended overlaps everything after it: an abandoned
    # adapter task is a live process, not a finished one
    abandoned = SpawnLog(
        spawns=[_spawn("alpha", 1, 0, None, seq=0), _spawn("alpha", 2, 5, 9, seq=1)]
    )
    (violation,) = cm09_overlapping_runs(abandoned)
    assert "still running (never ended)" in violation


def test_harness_cm09_accepts_back_to_back_runs() -> None:
    """Adjacent runs at one virtual instant are legal: the engine cancels
    the stale task before launching, and _settle reaps it before the clock
    moves, so end == start is the tight-but-correct case."""
    log = SpawnLog(spawns=[_spawn("alpha", 1, 0, 5, seq=0), _spawn("alpha", 2, 5, 9, seq=1)])
    assert cm09_overlapping_runs(log) == []


def test_harness_check_reports_every_violation_at_once() -> None:
    log = SpawnLog(spawns=[_spawn("alpha", 1, 0, None, seq=0), _spawn("alpha", 1, 5, 9, seq=1)])
    with pytest.raises(AssertionError) as excinfo:
        check(log)
    message = str(excinfo.value)
    assert "2 concurrency-model violation(s)" in message
    assert "CM-14:" in message and "CM-09:" in message


# ------------------------------------------------------------ the obligations


def test_cm14_two_starts_for_one_stopped_job_spawn_once(tmp_path: Path) -> None:
    """The question this whole programme started from: two operators send
    START to the same stopped job. One wins; the other must not produce a
    second process. DL-81 made the loser explicit (START_REFUSED); this
    counts the processes."""

    async def scenario() -> None:
        run = ModelRun(MODEL_JIL, tmp_path / "run", script={("solo", 1): (30.0, 0)})
        run.start()
        at = run.clock.now()
        for _ in range(2):
            run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at)

        assert [(s.job, s.run_number) for s in run.log.spawns] == [("solo", 1)]
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm14_a_run_lost_to_an_engine_crash_is_failed_not_rerun(tmp_path: Path) -> None:
    """The failure a per-engine assertion cannot see: engine 0 spawns run
    1, dies with it live, and engine 1 replays a journal that says the job
    is RUNNING. Each engine is individually correct; only a log that
    outlives the crash shows whether the process started twice.

    The guard is reconciliation's rule for a start whose SPAWN the log
    already resolved: no intent is left waiting, so there is nothing to
    deliver and the run is FAILED rather than silently re-run. DL-102
    narrowed that from "any start with no spool trace" -- a start still
    PENDING in the outbox is re-driven instead, which is a different case
    and has its own test (tests/test_ledger.py). The resumed engine is a
    plausible place to break either, because everything it knows says the
    job ought to be running."""

    async def scenario() -> None:
        run = ModelRun(MODEL_JIL, tmp_path / "run", script={("solo", 1): (600.0, 0)})
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at)
        assert [(s.job, s.run_number) for s in run.log.execs()] == [("solo", 1)]

        await run.crash()
        assert run.log.spawns[0].outcome == "cancelled"  # died with the engine, reported nothing

        engine = await run.resume(settle_seconds=0.0, grace_seconds=0.0)
        await run.run_to(at + timedelta(seconds=3600))

        assert [(s.job, s.run_number, s.incarnation) for s in run.log.execs()] == [("solo", 1, 0)]
        assert engine.oracle.store.job["solo"].status == "FAILURE"
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm14_a_resumed_file_watch_rearms_without_executing(tmp_path: Path) -> None:
    """The counter-case, and the reason CM-14 counts execs rather than
    adapter calls: reconciliation re-dispatches an in-flight FW run under
    the SAME run_number on purpose (an "idempotent read"). A checker that
    could not tell a re-armed watch from a re-executed command would
    report this correct behaviour as the double-run it exists to catch."""

    async def scenario() -> None:
        run = ModelRun(WATCH_JIL, tmp_path / "run")
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "watcher"}))
        await run.run_to(at)
        assert [(s.job, s.run_number, s.mode) for s in run.log.spawns] == [("watcher", 1, "watch")]

        await run.crash()
        await run.resume(settle_seconds=0.0, grace_seconds=0.0)
        await run.run_to(at + timedelta(seconds=60))

        modes = [(s.job, s.run_number, s.mode) for s in run.log.spawns]
        assert modes == [("watcher", 1, "watch"), ("watcher", 1, "watch")]
        assert run.log.execs() == []  # nothing executed, twice or otherwise
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm14_a_duplicate_completion_restarts_nothing(tmp_path: Path) -> None:
    """At-least-once delivery is the design (concurrency-model ss5): the
    same completion arriving twice must be idempotent, including through
    the box cascade it releases.

    A COMPOSITION test, deliberately: two guards stand behind this and
    deleting either one alone leaves the other holding, so no single
    mutation reddens it. That makes it worth little as a regression guard
    and something as an end-to-end statement, which is why each guard also
    has a test that isolates it -- the stale gate in
    `test_cm06_a_result_from_a_superseded_run_is_not_applied`, the
    ghost-run gate in
    `test_cm14_a_change_status_to_starting_launches_nothing`."""

    async def scenario() -> None:
        run = ModelRun(
            MODEL_JIL, tmp_path / "run", script={("alpha", 1): (5.0, 0), ("beta", 1): (5.0, 0)}
        )
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "bx"}))
        await run.run_to(at + timedelta(seconds=30))
        assert sorted(s.key for s in run.log.execs()) == [("alpha", 1), ("beta", 1)]

        # replay alpha's completion: the cascade re-fires and beta, already
        # started at this run_number, must not be launched a second time
        replayed = Event(
            at=run.clock.now(),
            kind="STATUS",
            payload={"job": "alpha", "run_number": 1, "status": "SUCCESS"},
        )
        run.inject(replayed)
        run.inject(replayed.model_copy(deep=True))
        await run.run_to(run.clock.now() + timedelta(seconds=30))

        assert sorted(s.key for s in run.log.execs()) == [("alpha", 1), ("beta", 1)]
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm14_a_change_status_to_starting_launches_nothing(tmp_path: Path) -> None:
    """The ghost-run gate in isolation. `sendevent CHANGE_STATUS STARTING`
    rewrites the recorded status and launches no process (vendor parity),
    so against a job that has already run it re-emits STARTING at an
    unchanged run_number -- which, if dispatched, is literally CM-14's
    double run."""

    async def scenario() -> None:
        run = ModelRun(MODEL_JIL, tmp_path / "run", script={("solo", 1): (5.0, 0)})
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at + timedelta(seconds=30))
        assert [(s.job, s.run_number) for s in run.log.execs()] == [("solo", 1)]

        now = run.clock.now()
        run.inject(Event(at=now, kind="STATUS", payload={"job": "solo", "status": "STARTING"}))
        await run.run_to(now + timedelta(seconds=30))

        assert [(s.job, s.run_number) for s in run.log.execs()] == [("solo", 1)]
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm06_a_result_from_a_superseded_run_is_not_applied(tmp_path: Path) -> None:
    """The ss4 stale gate in isolation, and CM-06's shape: an effect
    result that arrives after its run was superseded must be discarded,
    not applied to whatever occupies the job now. Concurrency-model ss5
    states the same rule for SPAWN/TERM/KILL -- applicability is by exact
    run identity -- so this is the half of it today's code already owes."""

    async def scenario() -> None:
        run = ModelRun(
            MODEL_JIL, tmp_path / "run", script={("solo", 1): (600.0, 0), ("solo", 2): (600.0, 0)}
        )
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at)

        # supersede run 1 with a fresh run, the way an operator would
        overwrite = at + timedelta(seconds=10)
        run.inject(Event(at=overwrite, kind="STATUS", payload={"job": "solo", "status": "SUCCESS"}))
        await run.run_to(overwrite)
        run.inject(Event(at=overwrite, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(overwrite)
        engine = run.live
        assert engine.oracle.store.job["solo"].run_number == 2

        # run 1's process reports in late, having failed
        late = overwrite + timedelta(seconds=5)
        run.deliver(
            Event(at=late, kind="STATUS", payload={"job": "solo", "run_number": 1, "exit_code": 1})
        )
        await run.run_to(late)

        assert engine.oracle.store.job["solo"].status == "RUNNING"  # run 2 untouched
        assert [reason for _, reason in engine.drops] == ["run_number mismatch"]
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm09_a_restart_over_a_live_run_does_not_overlap_it(tmp_path: Path) -> None:
    """A CHANGE_STATUS overwrite to a terminal while the process is live,
    then a fresh start: the engine must have killed run 1 before run 2
    begins. Two runs of one job overlapping is CM-09's physical half, and
    it is invisible to CM-14 -- the run_numbers differ."""

    async def scenario() -> None:
        run = ModelRun(
            MODEL_JIL, tmp_path / "run", script={("solo", 1): (600.0, 0), ("solo", 2): (5.0, 0)}
        )
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at)
        assert run.log.execs()[0].ended_at is None  # live

        overwrite = at + timedelta(seconds=10)
        run.inject(Event(at=overwrite, kind="STATUS", payload={"job": "solo", "status": "SUCCESS"}))
        await run.run_to(overwrite)
        run.inject(Event(at=overwrite, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(overwrite + timedelta(seconds=60))

        first, second = run.log.execs()
        assert (first.run_number, second.run_number) == (1, 2)
        assert first.ended_at == overwrite  # killed by the overwrite, not left running
        run.check()
        await run.close()

    asyncio.run(scenario())


def test_cm14_a_kill_stops_the_process_at_the_kill_instant(tmp_path: Path) -> None:
    """A KILLJOB against a live run: the oracle's TERMINATED must cancel
    the adapter task THEN, not leave it running to its natural end with
    the late completion quietly dropped by the stale gate. The semantic
    twin of DL-83's spawn-window fix, which closed the process-tier half
    of the same hazard."""

    async def scenario() -> None:
        run = ModelRun(MODEL_JIL, tmp_path / "run", script={("solo", 1): (600.0, 0)})
        run.start()
        at = run.clock.now()
        run.inject(Event(at=at, kind="STARTJOB", payload={"job": "solo"}))
        await run.run_to(at)
        kill_at = at + timedelta(seconds=10)
        run.inject(Event(at=kill_at, kind="KILLJOB", payload={"job": "solo"}))
        await run.run_to(at + timedelta(seconds=3600))

        (spawn,) = run.log.execs()
        assert (spawn.job, spawn.run_number) == ("solo", 1)
        assert spawn.outcome == "cancelled"  # the kill reached the process
        assert spawn.ended_at == kill_at  # at the kill, not 600s later
        run.check()
        await run.close()

    asyncio.run(scenario())
