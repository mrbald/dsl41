"""The engine's stops through the attempt and engine lifecycle charts (spec-wiring 2b, DL-426).

The scenarios of spec-drain and spec-crash run against real wrappers on the real clock: a drain
with 0, 1 and 20 attempts, a command that ignores TERM, a natural exit during STOPPING, a second
stop, a crash in DRAINING and its restart, the interruption rule for an idempotent job, a marked
job and a run the oracle ended during the drain, and a SET_GLOBAL admitted just before and just
after `stop_requested`. Assertions read the recorded order, never elapsed time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from store_surgery import rows

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import LocalCommandAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import RealClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_journal import is_valid_run_id
from dsl41.runner_startup import resume_run, start_run

#: short graces, so a ladder that has to KILL takes half a second
GRACE = EngineSettings(grace_term_s=0.5, grace_kill_s=1.0)
#: the deadline the scenarios never reach unless they say so
TIMEOUT_S = 30.0


def _jil(jobs: dict[str, str], extra: str = "") -> str:
    return "".join(
        f"insert_job: {job}\njob_type: c\ncommand: {cmd}\n{extra}\n" for job, cmd in jobs.items()
    )


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / f"{name}.sh"
    path.write_text(body)
    # `exec`, so the script is the group's leader on every /bin/sh: dash forks for `-c`
    return f"exec /bin/sh {path}"


def _start(
    run_root: Path,
    text: str,
    *,
    settings: EngineSettings = GRACE,
    marked: dict[str, str] | None = None,
) -> Engine:
    return start_run(
        lower_source(text),
        run_root,
        clock=RealClock(),
        adapters={"CMD": LocalCommandAdapter(grace_seconds=settings.grace_term_s)},
        hold_open=True,
        settings=settings,
        non_idempotent=marked,
    )


async def _resume(run_root: Path, text: str, *, settings: EngineSettings = GRACE) -> Engine:
    return await resume_run(
        lower_source(text),
        run_root,
        clock=RealClock(),
        adapters={"CMD": LocalCommandAdapter(grace_seconds=settings.grace_term_s)},
        hold_open=True,
        settle_seconds=0.2,
        settings=settings,
    )


def _start_jobs(engine: Engine, *jobs: str) -> None:
    for job in jobs:
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": job}))


async def _spawned(run_root: Path, job: str, run: int = 1, n: int = 1) -> None:
    path = run_root / "runs" / f"{job}.{run}" / str(n) / "spawn.json"
    async with asyncio.timeout(TIMEOUT_S):
        while not path.exists():
            await asyncio.sleep(0.01)


async def _until(predicate: Any) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


async def _finish(engine: Engine, loop: asyncio.Future[Any]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        await loop
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _kinds(run_root: Path, kind: str) -> list[dict[str, Any]]:
    return [body for _, k, body in rows(run_root) if k == kind]


def _engine_events(run_root: Path) -> list[str]:
    return [body["event"] for body in _kinds(run_root, "engine")]


def _plans(run_root: Path) -> list[dict[str, Any]]:
    return [body for body in _kinds(run_root, "follow_up") if body["event"] == "plan"]


# ------------------------------------------------------------------- the drain


@pytest.mark.parametrize("count", [0, 1, 20], ids=["none", "one", "twenty"])
def test_a_drain_stops_every_attempt_and_records_a_clean_shutdown(
    tmp_path: Path, count: int
) -> None:
    """spec-drain: `stop_requested` takes the engine to DRAINING, every unsettled attempt gets
    `stop` with cause `engine_stop`, the ladder ends it, and once every plan is done the engine
    snapshots, records `clean_shutdown` and exits 0. Each idempotent attempt's plan is a rerun
    whose dispatch waits for the restart."""
    jobs = {f"j{i:02}": "sleep 30" for i in range(count)} or {"idle": "sleep 30"}
    run_root = tmp_path / "run"
    states: list[str] = []

    async def scenario() -> Engine:
        engine = _start(run_root, _jil(jobs))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        started = list(jobs)[:count]
        _start_jobs(engine, *started)
        for job in started:
            await _spawned(run_root, job)
        await _until(lambda: all(engine.attempt_view(job) is not None for job in started))
        states.append(await engine.request_stop())
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(scenario())
    assert states == ["STOPPED" if count == 0 else "DRAINING"]
    assert (engine.exit_code, engine.leaving_record) == (0, "clean_shutdown")
    # the record first, then the snapshot that covers it (Astra N1)
    assert _engine_events(run_root) == ["started", "draining", "clean_shutdown", "snapshot"]
    outcomes = _kinds(run_root, "outcome")
    assert len(outcomes) == count
    assert {(o["outcome"], o["cause"]) for o in outcomes} <= {("stopped", "engine_stop")}
    plans = _plans(run_root)
    assert [p["plan"] for p in plans] == ["rerun"] * count
    # the replacement is named before it can run, and no dispatch or done mark followed
    assert all(is_valid_run_id(p["steps"][0]["attempt"]) for p in plans)
    # an outcome that landed while its ladder ran is closed once the ladder ended (DL-426)
    events = [b["event"] for b in _kinds(run_root, "follow_up") if b["event"] != "ladder_done"]
    assert events == ["plan"] * count
    assert len(_kinds(run_root, "dispatch")) == count
    assert all(rt.status == "RUNNING" for rt in engine.oracle.store.job.values() if rt.run_number)


def test_a_command_that_ignores_term_is_killed_after_the_grace(tmp_path: Path) -> None:
    """The ladder: TERM, `grace_term_s`, then KILL; the wrapper records the KILL as this
    attempt's stop."""
    run_root = tmp_path / "run"
    command = _script(tmp_path, "stubborn", "trap '' TERM\nsleep 30\n")

    async def scenario() -> None:
        engine = _start(run_root, _jil({"j": command}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()
        await _finish(engine, loop)

    asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "engine_stop")
    assert (ended["status"]["wrapper_outcome"], ended["status"]["signal"]) == ("signaled", 9)
    assert _engine_events(run_root)[-2:] == ["clean_shutdown", "snapshot"]


def test_an_exit_during_the_stop_is_the_stop_s_and_reruns(tmp_path: Path) -> None:
    """DL-426: a command that traps TERM and exits during STOPPING ends `stopped`, never
    `natural`: its exit code would otherwise feed FAILURE and cascade."""
    run_root = tmp_path / "run"
    command = _script(tmp_path, "trapper", "trap 'exit 3' TERM\nsleep 30 &\nwait $!\n")

    async def scenario() -> Engine:
        engine = _start(run_root, _jil({"j": command}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["status"]["exit_code"]) == ("stopped", 3)
    assert [p["plan"] for p in _plans(run_root)] == ["rerun"]
    assert engine.oracle.store.job["j"].status == "RUNNING"  # the oracle was told nothing


def test_a_second_stop_moves_the_deadline_to_now_and_the_restart_reruns(tmp_path: Path) -> None:
    """spec-drain: a second `stop_requested` while draining moves the deadline to now, so the
    drain ends unclean with the attempt that is still in its grace named. The engine exits 0;
    the wrapper records the lifeline's kill, and the restart reruns the same run with the same
    DSL41_JOB, DSL41_RUN_NUMBER and DSL41_SCHEDULED_AT and DSL41_ATTEMPT 2."""
    run_root = tmp_path / "run"
    seen = tmp_path / "seen"
    body = (
        f'echo "$DSL41_JOB|$DSL41_RUN_NUMBER|$DSL41_SCHEDULED_AT|$DSL41_ATTEMPT" >> {seen}\n'
        'if [ "$DSL41_ATTEMPT" = 1 ]; then trap "" TERM; sleep 30; fi\n'
    )
    text = _jil({"j": _script(tmp_path, "twice", body)})
    slow = EngineSettings(grace_term_s=2.0, grace_kill_s=1.0)
    states: list[str] = []

    async def first() -> Engine:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        states.append(await engine.request_stop())
        states.append(await engine.request_stop())
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(first())
    assert states == ["DRAINING", "DRAINING"]
    assert (engine.exit_code, engine.leaving_record) == (0, "unclean_shutdown")
    [unclean] = [b for b in _kinds(run_root, "engine") if b["event"] == "unclean_shutdown"]
    [dispatch] = _kinds(run_root, "dispatch")
    assert unclean["unsettled"] == [dispatch["attempt"]]
    assert _kinds(run_root, "outcome") == []  # unsettled: the restart settles it

    async def restart() -> Engine:
        engine = await _resume(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(lambda: engine.oracle.store.job["j"].status == "SUCCESS")
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    asyncio.run(restart())
    [lost, finished] = _kinds(run_root, "outcome")
    assert (lost["outcome"], lost["cause"]) == ("stopped", "lifeline")
    assert finished["outcome"] == "natural"
    first_line, second_line = seen.read_text().splitlines()
    assert first_line.split("|")[:3] == second_line.split("|")[:3]
    assert (first_line.split("|")[3], second_line.split("|")[3]) == ("1", "2")
    first_dispatch, second_dispatch = _kinds(run_root, "dispatch")
    assert "after" not in first_dispatch and first_dispatch["spool"].endswith("/j.1/1")
    assert second_dispatch["after"] == "lifeline"
    assert second_dispatch["spool"].endswith("/j.1/2")


def test_a_crash_while_draining_is_reported_at_the_restart(tmp_path: Path) -> None:
    """spec-drain "Crash in DRAINING": the engine dies after its `draining` record and before a
    shutdown record. The restart settles the attempt through its spool and journals
    `drain_interrupted` from the replayed lifecycle, before `engine started`."""
    run_root = tmp_path / "run"
    command = _script(tmp_path, "stubborn", "trap '' TERM\nsleep 30\n")
    text = _jil({"j": command})
    slow = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)

    async def crash() -> None:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()
        loop.cancel()  # the engine dies mid-drain: no shutdown record
        await asyncio.gather(loop, return_exceptions=True)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(crash())
    assert _engine_events(run_root) == ["started", "draining"]

    async def restart() -> None:
        engine = await _resume(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await engine.request_stop()
        await _finish(engine, loop)

    asyncio.run(restart())
    events = _engine_events(run_root)
    assert events[:4] == ["started", "draining", "drain_interrupted", "started"]
    [ended, *_] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "lifeline")


# ------------------------------------------------------------------- the interruption rule


def test_a_marked_job_is_held_with_its_evidence_and_an_idempotent_one_reruns(
    tmp_path: Path,
) -> None:
    """spec-crash: an interrupted attempt of a job named in `non-idempotent.toml` is one oracle
    transition, FAILURE with cause `interrupted` that holds the job, so its own failure
    condition starts nothing (Astra B1), and `query` shows the evidence until its next start;
    an idempotent job's run is dispatched again at the restart."""
    run_root = tmp_path / "run"
    text = _jil({"idem": "sleep 30"}) + _jil({"marked": "sleep 30"}, "condition: f(marked)\n")
    marked = {"marked": "posts twice"}

    async def drain() -> None:
        engine = _start(run_root, text, marked=marked)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "idem")
        engine.inject(
            Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": "marked"})
        )
        for job in ("idem", "marked"):
            await _spawned(run_root, job)
        await _until(lambda: engine.attempt_view("marked") is not None)
        await engine.request_stop()
        await _finish(engine, loop)
        assert engine.oracle.store.job["marked"].on_hold
        held = engine.evidence["marked"]
        assert (held["kind"], held["cause"], held["run_number"]) == ("stopped", "engine_stop", 1)

    asyncio.run(drain())
    plans = {p["job"]: p for p in _plans(run_root)}
    assert (plans["idem"]["plan"], plans["marked"]["plan"]) == ("rerun", "hold")
    assert plans["marked"]["evidence"]["attempt"] == "marked.1.1"
    fed = [b for b in _kinds(run_root, "input") if b.get("payload", {}).get("job") == "marked"]
    [held_input] = [b for b in fed if b["payload"].get("hold")]
    assert (held_input["kind"], held_input["payload"]["cause"]) == ("STATUS", "interrupted")
    # its own `f(marked)` condition started no second run, not even an owed one
    assert [b["job"] for b in _kinds(run_root, "dispatch_pending")] == []

    async def restart() -> Engine:
        engine = await _resume(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _spawned(run_root, "idem", n=2)
        await _until(lambda: engine.attempt_view("idem") is not None)
        view = engine.attempt_view("idem")
        assert view is not None and (view["n"], view["after"]) == (2, "engine_stop")
        assert engine.evidence["marked"]["attempt"] == "marked.1.1"  # it survived the restart
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    marked_row = engine.oracle.store.job["marked"]
    assert (marked_row.status, marked_row.on_hold) == ("FAILURE", True)


def test_a_run_the_oracle_ends_during_the_drain_is_not_replaced(tmp_path: Path) -> None:
    """Rule step 3: a `term_run_time` that fires while the attempt stops ends the run. The
    command's late exit is the stop's, and the plan is `none`: the oracle's own end stands and
    nothing reruns. The run's minute-long `term_run_time` makes this the one slow scenario."""
    run_root = tmp_path / "run"
    command = _script(tmp_path, "lingers", "trap 'sleep 65; exit 0' TERM\nsleep 120 &\nwait $!\n")
    text = _jil({"j": command}, "term_run_time: 1\n")
    slow = EngineSettings(grace_term_s=75.0, grace_kill_s=1.0)

    async def scenario() -> Engine:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()
        async with asyncio.timeout(120):
            await loop
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].status == "TERMINATED"
    assert engine.leaving_record == "clean_shutdown"
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"], ended["status"]["exit_code"]) == (
        "stopped",
        "engine_stop",
        0,
    )
    assert [p["plan"] for p in _plans(run_root)] == ["none"]


# ------------------------------------------------------------------- one side of the stop


def test_a_mutation_is_admitted_wholly_before_the_stop_or_refused_after_it(tmp_path: Path) -> None:
    """spec-wiring 2b: `stop_requested` is a step of the single writer. A SET_GLOBAL queued
    before it applies; one queued after it is admitted with a rejected decision that names
    `engine_draining`, its time half applied; an exact retry is answered from that decision."""
    run_root = tmp_path / "run"
    name = "EMEA_REC_GATE_C"  # examples/nightbank/estate/small/emea.jil

    def set_gate(engine: Engine, request_id: str, value: str, rev: int) -> Any:
        event = Event(
            at=engine.clock.now(), kind="SET_GLOBAL", payload={"name": name, "value": value}
        )
        return engine.submit(event, Envelope(request_id=request_id, expect={f"global:{name}": rev}))

    async def scenario() -> tuple[Engine, list[Any]]:
        engine = _start(run_root, _jil({"j": "sleep 30"}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        before = set_gate(engine, "before", "OPEN", 0)
        stop = engine.request_stop()
        after = set_gate(engine, "after", "SHUT", 1)
        answers = [await before, await stop, await after]
        answers.append(await set_gate(engine, "after", "SHUT", 1))  # the exact retry
        await _finish(engine, loop)
        return engine, answers

    engine, (before, state, after, retry) = asyncio.run(scenario())
    assert (before.decision, state) == ("applied", "DRAINING")
    assert (after.decision, after.code) == ("rejected", "engine_draining")
    assert retry == after
    assert engine.oracle.store.globals_[name].value == "OPEN"
    decisions = {b["request_id"]: b for b in _kinds(run_root, "decision")}
    assert decisions["after"]["code"] == "engine_draining"
    order = [
        b["event"] if k == "engine" else b["request_id"]
        for _, k, b in rows(run_root)
        if k == "engine" or (k == "decision" and b["request_id"] in ("before", "after"))
    ]
    assert order.index("before") < order.index("draining") < order.index("after")


# ------------------------------------------------------------------- the attempt's identity


def test_an_attempt_and_its_replacement_keep_one_identity_through_the_store(
    tmp_path: Path,
) -> None:
    """The attempt's durable identity is the dispatch's uuid4 (DL-118); its ordinal names the
    spool and the display (integration pass M4). A tier-driven first dispatch and a committed
    rerun survive the journal and a snapshot restore with the same identity in the tier, the
    plan, the environment (DSL41_ATTEMPT is the ordinal) and the spool."""
    run_root = tmp_path / "run"
    text = _jil({"j": "sleep 30"})

    async def drain() -> str:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        assert engine.attempts.knows(view["run_id"])  # keyed by the run id
        await engine.request_stop()
        await _finish(engine, loop)
        return str(view["run_id"])

    first = asyncio.run(drain())
    [dispatch] = _kinds(run_root, "dispatch")
    [plan] = _plans(run_root)
    [step] = plan["steps"]
    assert (dispatch["attempt"], dispatch["ordinal"], dispatch["env"]["DSL41_ATTEMPT"]) == (
        first,
        1,
        "1",
    )
    assert dispatch["spool"] == str(run_root / "runs" / "j.1" / "1")
    assert plan["attempt"] == first and is_valid_run_id(step["attempt"])
    assert (step["ordinal"], step["env"]["DSL41_ATTEMPT"]) == (2, "2")
    # the drain's snapshot holds the open plan; the restart reads it from there
    from dsl41.runner_journal import open_reader

    store = open_reader(run_root)
    try:
        snapshot = store.latest_snapshot()
        assert snapshot is not None
    finally:
        store.close()

    async def restart() -> None:
        engine = await _resume(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _spawned(run_root, "j", n=2)
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None and (view["run_id"], view["n"]) == (step["attempt"], 2)
        assert engine.attempts.knows(step["attempt"]) and not engine.attempts.knows(first)
        await engine.request_stop()
        await _finish(engine, loop)

    asyncio.run(restart())
    replacement = _kinds(run_root, "dispatch")[1]
    assert (replacement["attempt"], replacement["ordinal"]) == (step["attempt"], 2)
    assert replacement["env"] == step["env"]
    assert {k: v for k, v in replacement["env"].items() if k != "DSL41_ATTEMPT"} == {
        k: v for k, v in dispatch["env"].items() if k != "DSL41_ATTEMPT"
    }
    assert replacement["spool"] == str(run_root / "runs" / "j.1" / "2")
    spawn = (run_root / "runs" / "j.1" / "2" / "spawn.json").read_text()
    assert step["attempt"] in spawn


# ------------------------------------------------------------------- the control verb


def test_engine_stop_over_the_socket_drains_and_shows_the_attempt(
    tmp_path: Path, short_root: Path
) -> None:
    """`{"cmd": "engine_stop"}` is the drain's door on the socket: `status` shows the stopping
    attempt beside the job, the answer is the state the step left, and the engine exits."""
    from dsl41.runner_control import ControlClient, ControlServer

    run_root = short_root / "run"
    command = _script(tmp_path, "stubborn", "trap '' TERM\nsleep 30\n")
    slow = EngineSettings(grace_term_s=1.0, grace_kill_s=1.0)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        engine = _start(run_root, _jil({"j": command}), settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        client = ControlClient(run_root / "control.sock")
        try:
            _start_jobs(engine, "j")
            await _spawned(run_root, "j")
            await _until(lambda: engine.attempt_view("j") is not None)
            answer = await client.request({"cmd": "engine_stop", "v": 3})
            status = await client.request({"cmd": "status", "job": "j", "v": 3})
        finally:
            await client.close()
        await _finish(engine, loop)
        await server.close()
        return answer, status

    answer, status = asyncio.run(scenario())
    assert (answer["ok"], answer["engine"]) == (True, "DRAINING")
    row = status["jobs"]["j"]
    assert (row["attempt"]["state"], row["attempt"]["cause"]) == ("STOPPING", "engine_stop")
    assert _engine_events(run_root)[-2:] == ["clean_shutdown", "snapshot"]


def test_engine_stop_that_the_loop_does_not_take_is_an_unknown_outcome(tmp_path: Path) -> None:
    from dsl41.runner_control import ControlServer

    async def scenario() -> dict[str, Any]:
        engine = _start(tmp_path / "run", _jil({"j": "true"}))
        server = ControlServer(engine, tmp_path / "run" / "control.sock")
        server.DECISION_TIMEOUT_S = 0.05
        answer = await server._dispatch({"cmd": "engine_stop"})  # no loop is running
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return answer

    answer = asyncio.run(scenario())
    assert (answer["ok"], answer["code"]) == (False, "decision_timeout")


# ------------------------------------------------------------------- the rows a launch reaches


def test_a_stop_during_the_restart_records_shutdown_during_startup(tmp_path: Path) -> None:
    """engine_lifecycle.06: SIGTERM (the handler's `request_stop`) while a restart recovers is
    STARTING + stop_requested. The engine records `shutdown_during_startup`, takes no snapshot,
    launches nothing it owed, and exits 0."""
    run_root = tmp_path / "run"
    text = _jil({"j": "sleep 30"})

    async def first() -> None:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await engine.request_stop()  # the rerun waits for the restart
        await _finish(engine, loop)

    asyncio.run(first())

    async def restart() -> Engine:
        engine = await _resume(run_root, text)
        assert engine.lifecycle.state == "STARTING"
        state = engine.request_stop()  # before the loop's first turn: the recovery is pending
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        assert await state == "STOPPED"
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    assert (engine.exit_code, engine.leaving_record) == (0, "shutdown_during_startup")
    assert _engine_events(run_root)[-1] == "shutdown_during_startup"
    assert len(_kinds(run_root, "dispatch")) == 1  # the owed rerun was never launched


def test_a_wrapper_whose_command_cannot_start_records_before_any_spawn(tmp_path: Path) -> None:
    """attempt.11: the wrapper cannot launch the command (its stdin file is missing), so it
    writes `status.json` with `spawn_failed` and no `spawn.json`: SPAWNING + exit_recorded. The
    outcome is `spawn_failed`, and the job is FAILURE `wrapper_spawn_failed`."""
    run_root = tmp_path / "run"
    text = _jil({"j": "true"}, f"std_in_file: {tmp_path / 'missing'}\n")

    async def scenario() -> Engine:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _until(lambda: engine.oracle.store.job["j"].status == "FAILURE")
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["status"]["wrapper_outcome"]) == (
        "spawn_failed",
        "spawn_failed",
    )
    assert not (run_root / "runs" / "j.1" / "1" / "spawn.json").exists()
    assert engine.oracle.store.job["j"].status == "FAILURE"


@pytest.mark.parametrize(
    "stopped", ["no", "drain", "killed"], ids=["running", "stopping", "after_the_timeout"]
)
def test_a_wrapper_the_engine_cannot_start_is_a_failed_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    """attempt.02 and attempt.10: the engine cannot start the wrapper (an EMFILE-class failure,
    stood in by the spawn call). With no stop it is SPAWNING + spawn_failed; with a drain that
    landed while the launch was failing it is STOPPING + spawn_failed. Either way nothing
    launched, the job is FAILURE `wrapper_spawn_failed`, and a drain settles clean. A KILLJOB
    whose record timeout ends the attempt before the launch fails makes the failure late:
    ENDED + spawn_failed, evidence only."""
    run_root = tmp_path / "run"
    release = asyncio.Event()

    async def failing_spawn(*_: Any, **__: Any) -> Any:
        await release.wait()
        raise OSError(24, "Too many open files")

    async def scenario() -> Engine:
        monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_spawn)
        engine = _start(run_root, _jil({"j": "sleep 30"}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        if stopped == "drain":
            assert await engine.request_stop() == "DRAINING"
            view = engine.attempt_view("j")
            assert view is not None and view["state"] == "STOPPING"
        if stopped == "killed":
            engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
            run_id = str(view["run_id"])
            await _until(lambda: engine.attempts.state(run_id) == "ENDED")
            release.set()
            await _until(lambda: not engine.attempts.knows(run_id))
        else:
            release.set()
            await _until(lambda: engine.oracle.store.job["j"].status == "FAILURE")
        if stopped != "drain":
            await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert engine.leaving_record == "clean_shutdown"
    if stopped == "killed":
        assert (ended["outcome"], ended["cause"]) == ("unobservable", "oracle_kill")
        assert engine.oracle.store.job["j"].status == "TERMINATED"
        return
    assert (ended["outcome"], ended["status"]["error"]) == (
        "spawn_failed",
        "wrapper spawn failed: [Errno 24] Too many open files",
    )
    assert engine.oracle.store.job["j"].status == "FAILURE"


def _wrapper_stopped(pid: int) -> bool:
    import subprocess

    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return state.stdout.strip().startswith("T")


@pytest.mark.parametrize("then", ["continued", "killed", "killed_after_spawn"])
def test_records_after_the_record_timeout_are_evidence_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    """spec-drain: a record that arrives after `record_timeout` is kept as evidence and does
    not reopen the attempt. The wrapper is frozen after it launched the command and before
    `spawn.json`; a KILLJOB stops the attempt, whose timeout ends it `unobservable` with plan
    `none`. Continued, the wrapper then writes `spawn.json` and its record (ENDED +
    spawn_recorded, ENDED + exit_recorded); killed, it exits with neither (ENDED +
    wrapper_gone); killed once `spawn.json` names the live command, the tier's verdict is a
    stop with cause `wrapper_lost` (ENDED + stop). None of them reopens the attempt."""
    from dsl41.runner_wrapper import PAUSE_ENV

    run_root = tmp_path / "run"
    pidfile = tmp_path / "command.pid"
    command = _script(tmp_path, "named", f"echo $$ > {pidfile}\nexec sleep 30\n")
    monkeypatch.setenv(PAUSE_ENV, "post_spawn_pre_record,post_record")

    async def scenario() -> tuple[Engine, str]:
        engine = _start(run_root, _jil({"j": command}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        run_id = str(view["run_id"])
        launch = engine.attempt_facts(run_id).launch
        assert launch is not None
        await _until(lambda: launch.proc is not None and _wrapper_stopped(launch.proc.pid))
        assert launch.proc is not None
        wrapper = launch.proc.pid
        engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
        await _until(lambda: engine.attempts.state(run_id) == "ENDED")
        if then != "killed":
            # on to its second pause, post_record: spawn.json is written first
            os.kill(wrapper, signal.SIGCONT)
            await _until(lambda: (run_root / "runs" / "j.1" / "1" / "spawn.json").exists())
            await _until(lambda: _wrapper_stopped(wrapper))
        os.kill(wrapper, signal.SIGCONT if then == "continued" else signal.SIGKILL)
        await _until(lambda: not engine.attempts.knows(run_id))  # the wrapper is gone too
        await engine.request_stop()
        await _finish(engine, loop)
        return engine, run_id

    try:
        engine, run_id = asyncio.run(scenario())
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("unobservable", "oracle_kill")
    assert [p["plan"] for p in _plans(run_root)] == ["none"]
    assert engine.oracle.store.job["j"].status == "TERMINATED"
    late = engine.attempts.late_records.get(run_id, [])
    assert [r.cause for r in late] == (["parent lost"] if then == "continued" else [])


def test_an_empty_drain_answers_every_queued_request_before_it_stops(tmp_path: Path) -> None:
    """Astra N2: STOPPED is taken only once every queued request has its answer. A drain with
    nothing to settle reaches STOPPED in its own step; the SET_GLOBAL queued behind it is
    admitted with its durable `engine_draining` rejection, and a second stop is answered with
    the state (so the lifecycle meets STOPPED + stop_requested, an ignored event)."""
    run_root = tmp_path / "run"
    name = "EMEA_REC_GATE_C"

    async def scenario() -> tuple[str, Any, str]:
        engine = _start(run_root, _jil({"j": "true"}))
        first = engine.request_stop()
        event = Event(
            at=engine.clock.now(), kind="SET_GLOBAL", payload={"name": name, "value": "X"}
        )
        queued = engine.submit(event, Envelope(request_id="queued", expect={f"global:{name}": 0}))
        second = engine.request_stop()
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        answers = (await first, await queued, await second)
        await _finish(engine, loop)
        return answers

    first, queued, second = asyncio.run(scenario())
    assert (first, second) == ("STOPPED", "STOPPED")
    assert (queued.decision, queued.code) == ("rejected", "engine_draining")
    decisions = [b["request_id"] for b in _kinds(run_root, "decision")]
    assert "queued" in decisions
    assert _engine_events(run_root)[-2:] == ["clean_shutdown", "snapshot"]


def test_an_oracle_kill_is_never_relaunched_by_a_status_put_back(tmp_path: Path) -> None:
    """Opus 1: a KILLJOB stops the attempt with cause `oracle_kill`; an operator's
    CHANGE_STATUS back to RUNNING inside the ladder's window does not make its plan a rerun.
    The plan is `none`, and the command is launched once."""
    run_root = tmp_path / "run"
    command = _script(tmp_path, "slow", "trap 'sleep 1; exit 0' TERM\nsleep 30 &\nwait $!\n")

    async def scenario() -> Engine:
        engine = _start(run_root, _jil({"j": command}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        run_id = str(view["run_id"])
        now = engine.clock.now()
        engine.inject(Event(at=now, kind="KILLJOB", payload={"job": "j"}))
        engine.inject(Event(at=now, kind="STATUS", payload={"job": "j", "status": "RUNNING"}))
        await _until(lambda: not engine.attempts.knows(run_id))
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "oracle_kill")
    assert [p["plan"] for p in _plans(run_root)] == ["none"]
    assert len(_kinds(run_root, "dispatch")) == 1
    assert engine.oracle.store.job["j"].status == "RUNNING"  # the operator's word stands


def test_an_exit_the_drain_caused_stays_stopped_across_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus 2: the engine dies after the drain's TERM made `j` exit 3 and before it read the
    record. The restart adopts every attempt the `draining` record names with its stop, cause
    `engine_stop`, so the exit is `stopped` and `j` reruns; its exit code never reaches the
    oracle, and `k`, conditioned on `failure(j)`, never starts."""
    from dsl41.runner_stops import Observer

    run_root = tmp_path / "run"
    trapper = _script(tmp_path, "trapper", "trap 'exit 3' TERM\nsleep 30 &\nwait $!\n")
    text = _jil({"j": trapper}) + _jil({"k": "true"}, "condition: f(j)\n")

    async def crash() -> None:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        with monkeypatch.context() as blind:  # the engine never reads what the wrapper wrote
            blind.setattr(Observer, "status_recorded", lambda *_: None)
            blind.setattr(Observer, "wrapper_exited", lambda *_: None)
            await engine.request_stop()
            await _until(lambda: (run_root / "runs" / "j.1" / "1" / "status.json").exists())
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(crash())
    assert _engine_events(run_root) == ["started", "draining"]

    async def restart() -> Engine:
        engine = await _resume(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    ended = _kinds(run_root, "outcome")[0]
    assert (ended["outcome"], ended["cause"], ended["status"]["exit_code"]) == (
        "stopped",
        "engine_stop",
        3,
    )
    assert _plans(run_root)[0]["plan"] == "rerun"
    assert engine.oracle.store.job["k"].run_number == 0
    assert "drain_interrupted" in _engine_events(run_root)


def _survivor_gone(pid: int) -> bool:
    import subprocess
    import time

    for _ in range(100):
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        if state.stdout.strip() in ("", "Z"):
            return True
        time.sleep(0.05)
    return False


def test_a_command_that_outlived_its_wrapper_is_stopped_at_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus R2-3, the successor of the resume ladder's survivor case: the wrapper is killed and
    the engine dies with it, while the command runs on. The restart finds `spawn.json`, no
    record and a dead wrapper with a live command: `stop` with cause `wrapper_lost`, the
    ladder ends the survivor, and with no record after the KILL the chart ends the attempt
    `unobservable{wrapper_lost}` (attempt.07): an interruption, so the idempotent job reruns."""
    from dsl41.runner_stops import Observer

    run_root = tmp_path / "run"
    pidfile = tmp_path / "command.pid"
    command = _script(tmp_path, "survivor", f"echo $$ > {pidfile}\nexec sleep 30\n")
    text = _jil({"j": command})

    async def crash() -> int:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(pidfile.exists)
        view = engine.attempt_view("j")
        assert view is not None
        launch = engine.attempt_facts(str(view["run_id"])).launch
        assert launch is not None and launch.proc is not None
        with monkeypatch.context() as blind:  # the dying engine never learns the wrapper went
            blind.setattr(Observer, "wrapper_exited", lambda *_: None)
            os.kill(launch.proc.pid, signal.SIGKILL)
            await launch.proc.wait()
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return int(pidfile.read_text())

    survivor = asyncio.run(crash())
    try:
        os.kill(survivor, 0)  # it outlived its wrapper and the engine

        async def restart() -> None:
            engine = await _resume(run_root, text)
            loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            await _until(engine.started.is_set)
            await engine.request_stop()
            await _finish(engine, loop)

        asyncio.run(restart())
        # the ladder ended it, before this test's own cleanup below could
        assert _survivor_gone(survivor)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(survivor, signal.SIGKILL)
    ended = _kinds(run_root, "outcome")[0]
    assert (ended["outcome"], ended["cause"]) == ("unobservable", "wrapper_lost")
    assert _plans(run_root)[0]["plan"] == "rerun"


def test_an_unclean_shutdown_carries_each_attempt_s_stop_to_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R2-M2: the drain's TERM makes `j` exit 3, the engine does not see it, and a second
    stop ends the drain unclean. The shutdown record names `j`'s stop and its covering
    snapshot carries it in the open table, so the restart adopts `j` with `engine_stop`: the
    exit is `stopped`, and `k` (`f(j)`) never starts."""
    from dsl41.runner_stops import Observer

    run_root = tmp_path / "run"
    trapper = _script(tmp_path, "trapper", "trap 'exit 3' TERM\nsleep 30 &\nwait $!\n")
    text = _jil({"j": trapper}) + _jil({"k": "true"}, "condition: f(j)\n")

    async def unclean() -> Engine:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        with monkeypatch.context() as blind:  # the engine never reads what the wrapper wrote
            blind.setattr(Observer, "status_recorded", lambda *_: None)
            blind.setattr(Observer, "wrapper_exited", lambda *_: None)
            await engine.request_stop()
            await _until(lambda: (run_root / "runs" / "j.1" / "1" / "status.json").exists())
            await engine.request_stop()  # the deadline is now
            await _finish(engine, loop)
        return engine

    engine = asyncio.run(unclean())
    assert engine.leaving_record == "unclean_shutdown"
    [record] = [b for b in _kinds(run_root, "engine") if b["event"] == "unclean_shutdown"]
    assert list(record["stops"].values()) == ["engine_stop"]

    async def restart() -> Engine:
        engine = await _resume(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    first = _kinds(run_root, "outcome")[0]
    assert (first["outcome"], first["cause"], first["status"]["exit_code"]) == (
        "stopped",
        "engine_stop",
        3,
    )
    assert engine.oracle.store.job["k"].run_number == 0
    assert "drain_interrupted" not in _engine_events(run_root)  # the drain did end, unclean


def test_a_record_during_the_ladder_never_cancels_the_closing_kill(tmp_path: Path) -> None:
    """Astra R2-M3: the shell leading the group exits 30 ms after its TERM, so the wrapper's
    record reaches the tier while the ladder waits; a child that ignores TERM lives on. The
    record ends the attempt's observation, the ladder runs on to its closing KILL, and the
    drain records `clean_shutdown` only once the group is gone."""
    run_root = tmp_path / "run"
    childfile = tmp_path / "child.pid"
    body = (
        f"(trap '' TERM; exec sleep 30) &\necho $! > {childfile}\n"
        "trap 'sleep 0.03; exit 0' TERM\nwait\n"
    )
    command = _script(tmp_path, "leader", body)

    async def scenario() -> Engine:
        engine = _start(run_root, _jil({"j": command}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(childfile.exists)
        await _until(lambda: engine.attempt_view("j") is not None)
        await asyncio.sleep(0.2)  # the child has trapped TERM
        await engine.request_stop()
        await _finish(engine, loop)
        return engine

    try:
        engine = asyncio.run(scenario())
        child = int(childfile.read_text())
        assert _survivor_gone(child)  # the closing KILL reached it before the clean record
    finally:
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(childfile.read_text()), signal.SIGKILL)
    assert engine.leaving_record == "clean_shutdown"
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "engine_stop")


def _leader_and_stubborn_child(tmp_path: Path, children: Path, on_term: str = "exit 0") -> str:
    """A shell leading its group that ends on TERM, and a child that ignores TERM; each child's
    pid is appended to `children`."""
    body = f"(trap '' TERM; exec sleep 30) &\necho $! >> {children}\ntrap '{on_term}' TERM\nwait\n"
    return _script(tmp_path, "leader", body)


def _kill_all(children: Path) -> None:
    if children.exists():
        for line in children.read_text().split():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(line), signal.SIGKILL)


def test_a_group_that_survives_its_ladder_never_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R2-M3 reopened: an attempt whose group outlives its ladder's KILL (here a survivor
    the engine is told of) is never forgotten. It stays unsettled, the drain cannot end clean,
    and the shutdown record names it with its stop."""
    from dsl41 import runner_stops

    run_root = tmp_path / "run"
    children = tmp_path / "children"
    text = _jil({"j": _leader_and_stubborn_child(tmp_path, children)})
    monkeypatch.setattr(runner_stops, "group_has_live_member", lambda _pgid: True)
    real_ladder = runner_stops.stop_ladder

    async def ladder_outlasting_the_record(pgid: int, token: str, grace: float) -> None:
        await real_ladder(pgid, token, grace)
        await asyncio.sleep(0.3)  # the wrapper's record lands before the ladder has ended

    monkeypatch.setattr(runner_stops, "stop_ladder", ladder_outlasting_the_record)

    async def scenario() -> tuple[Engine, str]:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(children.exists)
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        run_id = str(view["run_id"])
        await asyncio.sleep(0.2)  # the child has trapped TERM
        await engine.request_stop()
        await _until(lambda: engine.attempt_facts(run_id).note == runner_stops.COMMAND_SURVIVED)
        assert engine.attempts.knows(run_id) and run_id in engine.attempts.unsettled()
        await engine.request_stop()  # the deadline is now
        await _finish(engine, loop)
        return engine, run_id

    try:
        engine, run_id = asyncio.run(scenario())
    finally:
        _kill_all(children)
    assert engine.leaving_record == "unclean_shutdown"
    events = _engine_events(run_root)
    assert "clean_shutdown" not in events
    [record] = [b for b in _kinds(run_root, "engine") if b["event"] == "unclean_shutdown"]
    assert record["unsettled"] == [run_id] and record["stops"] == {run_id: "engine_stop"}


def test_an_oracle_kill_stays_the_cause_across_a_drain_and_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R2-M2 reopened: the first stop an attempt took is its cause. A KILLJOB stops `j`,
    an operator puts its row back to RUNNING, a drain begins and the engine dies before it sees
    the exit. The `draining` record keeps `oracle_kill` for `j`, so the restart adopts it with
    that stop: plan `none`, no attempt 2."""
    from dsl41.runner_stops import Observer

    run_root = tmp_path / "run"
    command = _script(tmp_path, "slow", "trap 'sleep 1; exit 0' TERM\nsleep 30 &\nwait $!\n")
    text = _jil({"j": command})
    slow = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)

    async def crash() -> None:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        with monkeypatch.context() as blind:
            blind.setattr(Observer, "status_recorded", lambda *_: None)
            blind.setattr(Observer, "wrapper_exited", lambda *_: None)
            now = engine.clock.now()
            engine.inject(Event(at=now, kind="KILLJOB", payload={"job": "j"}))
            engine.inject(Event(at=now, kind="STATUS", payload={"job": "j", "status": "RUNNING"}))
            await _until(
                lambda: (
                    engine.oracle.store.job["j"].status == "RUNNING"
                    and len(_kinds(run_root, "decision")) >= 3
                )
            )
            await engine.request_stop()
            await _until(lambda: (run_root / "runs" / "j.1" / "1" / "status.json").exists())
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(crash())
    [draining] = [b for b in _kinds(run_root, "engine") if b["event"] == "draining"]
    assert list(draining["stops"].values()) == ["oracle_kill"]

    async def restart() -> None:
        engine = await _resume(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await engine.request_stop()
        await _finish(engine, loop)

    asyncio.run(restart())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["outcome"], ended["cause"]) == ("stopped", "oracle_kill")
    assert [p["plan"] for p in _plans(run_root)] == ["none"]
    assert len(_kinds(run_root, "dispatch")) == 1


def test_a_crash_between_the_outcome_and_the_closing_kill_never_overlaps_a_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R3-M1, R4-M2, R4-M3: the leader exits on the drain's TERM and its record lands
    while the ladder still waits; a TERM-ignoring child lives on, and the engine dies before
    the closing KILL. The attempt stayed open (its outcome committed while the ladder ran).
    The restart cannot prove the leaderless child its own, so it signals nothing and holds
    the attempt: attempt 2 is not launched, and `status` names why. Once the child is gone,
    the attempt closes and attempt 2 launches."""
    from dsl41 import runner_stops

    run_root = tmp_path / "run"
    children = tmp_path / "children"
    text = _jil({"j": _leader_and_stubborn_child(tmp_path, children)})

    async def term_then_hang(pgid: int, token: str, grace: float) -> None:
        os.killpg(pgid, signal.SIGTERM)
        await asyncio.Event().wait()  # the engine dies before this ladder's KILL

    async def crash() -> None:
        engine = _start(run_root, text)
        asyncio.ensure_future(engine.run_until_quiescent(datetime.max))  # cancelled below
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(children.exists)
        await _until(lambda: engine.attempt_view("j") is not None)
        await asyncio.sleep(0.2)  # the child has trapped TERM
        with monkeypatch.context() as hung:
            hung.setattr(runner_stops, "stop_ladder", term_then_hang)
            await engine.request_stop()
            await _until(lambda: len(_kinds(run_root, "outcome")) == 1)
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in others:
                task.cancel()
            await asyncio.gather(*others, return_exceptions=True)
        assert engine.journal is not None
        engine.journal.close()

    try:
        asyncio.run(crash())
        [outcome] = _kinds(run_root, "outcome")
        assert outcome["ladder"] == "running"
        first_child = int(children.read_text().split()[0])
        os.kill(first_child, 0)  # alive across the crash

        async def restart() -> dict[str, Any] | None:
            engine = await _resume(run_root, text)
            loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
            await _until(engine.started.is_set)
            await asyncio.sleep(1.0)  # two looks at the group
            os.kill(first_child, 0)  # nothing was sent to it
            assert [b["ordinal"] for b in _kinds(run_root, "dispatch")] == [1]
            view = engine.attempt_view("j")
            os.kill(first_child, signal.SIGKILL)
            await _spawned(run_root, "j", n=2)
            await asyncio.sleep(0.2)
            await engine.request_stop()
            await _finish(engine, loop)
            return view

        view = asyncio.run(restart())
    finally:
        _kill_all(children)
    assert view is not None and view["note"] == runner_stops.COMMAND_SURVIVED
    first = next(b["attempt"] for _, k, b in rows(run_root) if k == "dispatch")
    closed = next(
        seq
        for seq, k, b in rows(run_root)
        if k == "follow_up" and b.get("attempt") == first and b["event"] == "ladder_done"
    )
    second = next(seq for seq, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2)
    assert closed < second


def test_a_ladder_that_raises_after_the_record_leaves_the_attempt_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R4-M1: the record lands while the ladder runs, then the ladder raises before its
    KILL. The stop was not carried out, so no `ladder_done` is written: the attempt stays open
    and unsettled with its `ladder failed` note, and the drain ends unclean naming it."""
    from dsl41 import runner_stops

    run_root = tmp_path / "run"
    children = tmp_path / "children"
    text = _jil({"j": _leader_and_stubborn_child(tmp_path, children)})

    async def term_then_raise(pgid: int, token: str, grace: float) -> None:
        os.killpg(pgid, signal.SIGTERM)
        await _until(lambda: len(_kinds(run_root, "outcome")) == 1)
        raise PermissionError("EPERM")

    monkeypatch.setattr(runner_stops, "stop_ladder", term_then_raise)

    async def scenario() -> tuple[Engine, str]:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(children.exists)
        await _until(lambda: engine.attempt_view("j") is not None)
        view = engine.attempt_view("j")
        assert view is not None
        run_id = str(view["run_id"])
        await asyncio.sleep(0.2)  # the child has trapped TERM
        await engine.request_stop()
        await _until(lambda: engine.attempt_facts(run_id).note == "ladder failed: EPERM")
        await asyncio.sleep(0.6)  # a look at the group: the child lives
        assert run_id in engine.attempts.unsettled() and engine.blocked("j") == run_id
        await engine.request_stop()  # the deadline is now
        await _finish(engine, loop)
        return engine, run_id

    try:
        engine, run_id = asyncio.run(scenario())
    finally:
        _kill_all(children)
    [outcome] = _kinds(run_root, "outcome")
    assert outcome["ladder"] == "running"
    assert not [b for b in _kinds(run_root, "follow_up") if b["event"] == "ladder_done"]
    assert engine.leaving_record == "unclean_shutdown"
    [record] = [b for b in _kinds(run_root, "engine") if b["event"] == "unclean_shutdown"]
    assert record["unsettled"] == [run_id]


def test_a_new_run_waits_while_its_job_s_group_survives(tmp_path: Path) -> None:
    """Astra R4-M3: a KILLJOB's ladder ends with a TERM-ignoring member alive, so the attempt
    is held. A start of the job decided now is durable as `dispatch_pending` and not
    launched while the member lives, and `status` shows why; once it is gone, run 2 launches
    under that pending record."""
    from dsl41 import runner_stops
    from dsl41.runner_control import status_label

    run_root = tmp_path / "run"
    children = tmp_path / "children"
    text = _jil({"j": _leader_and_stubborn_child(tmp_path, children)})
    real_ladder = runner_stops.stop_ladder

    async def term_only(pgid: int, token: str, grace: float) -> None:
        os.killpg(pgid, signal.SIGTERM)  # the member ignores it; no KILL follows
        await _until(lambda: len(_kinds(run_root, "outcome")) == 1)

    async def scenario() -> tuple[str, list[int], list[int]]:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(children.exists)
        await _until(lambda: engine.attempt_view("j") is not None)
        await asyncio.sleep(0.2)  # the child has trapped TERM
        runner_stops.stop_ladder = term_only
        try:
            engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
            await _until(lambda: bool(engine.held_groups))
        finally:
            runner_stops.stop_ladder = real_ladder
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        await _until(lambda: engine.oracle.store.job["j"].run_number == 2)
        await asyncio.sleep(1.0)  # two looks at the group
        held = [b["run_number"] for b in _kinds(run_root, "dispatch")]
        row = {"status": engine.oracle.store.job["j"].status, "attempt": engine.attempt_view("j")}
        label = status_label(row)
        _kill_all(children)
        await _spawned(run_root, "j", run=2)
        await engine.request_stop()
        await _finish(engine, loop)
        return label, held, [b["run_number"] for b in _kinds(run_root, "dispatch")]

    try:
        label, held, dispatched = asyncio.run(scenario())
    finally:
        _kill_all(children)
    assert held == [1] and dispatched == [1, 2]
    assert label.endswith(f"(held: {runner_stops.COMMAND_SURVIVED})")
    [pending] = [b for b in _kinds(run_root, "dispatch_pending") if b.get("run_number") == 2]
    second = next(b for b in _kinds(run_root, "dispatch") if b["run_number"] == 2)
    assert second["discharges"] is not None and pending["job"] == "j"


def test_a_new_run_waits_for_the_stop_ladder_of_the_run_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fable's round-6 ruling: a job's new run dispatches only when no attempt of it may have
    a live group, a running ladder included. A KILLJOB stops a command that traps TERM; a
    STARTJOB decided while the ladder runs is durable as `dispatch_pending`, and `status`
    says why. Run 2 launches once, after the ladder's end closes run 1 (`ladder_done`)."""
    from dsl41 import runner_stops
    from dsl41.runner_control import status_label

    run_root = tmp_path / "run"
    command = _script(tmp_path, "slow", "trap 'sleep 1; exit 0' TERM\nsleep 30 &\nwait $!\n")
    text = _jil({"j": command})
    slow = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)
    real_ladder = runner_stops.stop_ladder

    async def ladder_outlasting_the_record(pgid: int, token: str, grace: float) -> None:
        await real_ladder(pgid, token, grace)
        await _until(lambda: len(_kinds(run_root, "outcome")) == 1)

    monkeypatch.setattr(runner_stops, "stop_ladder", ladder_outlasting_the_record)

    async def scenario() -> tuple[str, list[int]]:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await asyncio.sleep(0.2)  # the command has trapped TERM
        engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
        await _until(lambda: engine.blocked("j") is not None)
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        await _until(lambda: len(_kinds(run_root, "dispatch_pending")) == 1)
        waiting = [b["run_number"] for b in _kinds(run_root, "dispatch")]
        row = {"status": engine.oracle.store.job["j"].status, "attempt": engine.attempt_view("j")}
        label = status_label(row)
        assert waiting == [1] and engine.blocked("j") is not None
        await _spawned(run_root, "j", run=2)
        await asyncio.sleep(0.2)
        await engine.request_stop()
        await _finish(engine, loop)
        return label, [b["run_number"] for b in _kinds(run_root, "dispatch")]

    label, dispatched = asyncio.run(scenario())
    assert label.endswith(f"(held: {runner_stops.LADDER_RUNNING})")
    assert dispatched == [1, 2]
    first = next(b["attempt"] for _, k, b in rows(run_root) if k == "dispatch")
    closed = next(
        seq
        for seq, k, b in rows(run_root)
        if k == "follow_up" and b.get("attempt") == first and b["event"] == "ladder_done"
    )
    second = next(seq for seq, k, b in rows(run_root) if k == "dispatch" and b["run_number"] == 2)
    assert closed < second
    [pending] = [b for b in _kinds(run_root, "dispatch_pending") if b["event"] == "pending"]
    assert pending["run_number"] == 2


def test_a_restart_after_an_injected_inactive_waits_for_the_earlier_run_s_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-235 under the one guard: an injected INACTIVE leaves run 1 live in the shell, and a
    STARTJOB decides run 2. Run 1's attempt is stopped first (`oracle_kill`); run 2 is durable
    as `dispatch_pending` while that ladder runs, and launches once, after `ladder_done`, with
    run 1's group gone."""
    from dsl41 import runner_adapters, runner_stops

    run_root = tmp_path / "run"
    command = _script(tmp_path, "slow", "trap 'sleep 1; exit 0' TERM\nsleep 30 &\nwait $!\n")
    text = _jil({"j": command})
    slow = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)
    real_ladder = runner_stops.stop_ladder

    async def ladder_outlasting_the_record(pgid: int, token: str, grace: float) -> None:
        await real_ladder(pgid, token, grace)
        await _until(lambda: len(_kinds(run_root, "outcome")) == 1)

    async def scenario() -> tuple[list[int], bool, list[int]]:
        engine = _start(run_root, text, settings=slow)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "j")
        await _spawned(run_root, "j")
        await _until(lambda: engine.attempt_view("j") is not None)
        await asyncio.sleep(0.2)  # the command has trapped TERM
        spawn = json.loads((run_root / "runs" / "j.1" / "1" / "spawn.json").read_text())
        pgid = int(spawn["command_pgid"])
        with monkeypatch.context() as slow_ladder:
            slow_ladder.setattr(runner_stops, "stop_ladder", ladder_outlasting_the_record)
            now = engine.clock.now()
            status = {"job": "j", "status": "INACTIVE"}
            engine.inject(Event(at=now, kind="STATUS", payload=status))
            engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
            await _until(lambda: len(_kinds(run_root, "dispatch_pending")) == 1)
            waiting = [b["run_number"] for b in _kinds(run_root, "dispatch")]
            await _spawned(run_root, "j", run=2)
            old_gone = not runner_adapters.group_has_live_member(pgid)
        await asyncio.sleep(0.2)
        await engine.request_stop()
        await _finish(engine, loop)
        return waiting, old_gone, [b["run_number"] for b in _kinds(run_root, "dispatch")]

    waiting, old_gone, dispatched = asyncio.run(scenario())
    assert waiting == [1] and old_gone and dispatched == [1, 2]
    ended = _kinds(run_root, "outcome")[0]
    assert (ended["run_number"], ended["outcome"], ended["cause"]) == (1, "stopped", "oracle_kill")
    first = next(b["attempt"] for _, k, b in rows(run_root) if k == "dispatch")
    closed = next(
        seq
        for seq, k, b in rows(run_root)
        if k == "follow_up" and b.get("attempt") == first and b["event"] == "ladder_done"
    )
    second = next(seq for seq, k, b in rows(run_root) if k == "dispatch" and b["run_number"] == 2)
    assert closed < second


@pytest.mark.parametrize("stop", ["killjob", "inactive"])
def test_a_stop_before_the_spawn_is_observed_keeps_the_next_run_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """Astra R5-M1: the wrapper has launched the command, but the adapter has not yet seen
    `spawn.json` when a KILLJOB (or an injected INACTIVE and the next start's stop) lands, so
    the stop closes the lifeline and starts no ladder. Run 1's attempt is unsettled until its
    plan is done, so run 2 waits as `dispatch_pending` with `stop in progress`, and launches
    once run 1 has settled, its command gone."""
    from dsl41 import runner_stops
    from dsl41.runner_control import status_label
    from dsl41.runner_stops import Observer

    run_root = tmp_path / "run"
    text = _jil({"j": "sleep 30"})
    spool = run_root / "runs" / "j.1" / "1"

    async def scenario() -> tuple[str, list[int], bool, list[int]]:
        engine = _start(run_root, text)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        with monkeypatch.context() as blind:
            blind.setattr(Observer, "spawn_recorded", lambda *_: None)  # not seen yet
            _start_jobs(engine, "j")
            await _spawned(run_root, "j")
            spawn = json.loads((spool / "spawn.json").read_text())
            run_id = str(spawn["run_id"])
            assert engine.attempts.state(run_id) == "SPAWNING"
            now = engine.clock.now()
            if stop == "killjob":
                engine.inject(Event(at=now, kind="KILLJOB", payload={"job": "j"}))
            else:
                status = {"job": "j", "status": "INACTIVE"}
                engine.inject(Event(at=now, kind="STATUS", payload=status))
            engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
            await _until(lambda: len(_kinds(run_root, "dispatch_pending")) == 1)
        waiting = [b["run_number"] for b in _kinds(run_root, "dispatch")]
        row = {"status": engine.oracle.store.job["j"].status, "attempt": engine.attempt_view("j")}
        label = status_label(row)
        await _spawned(run_root, "j", run=2)
        gone = _survivor_gone(int(spawn["command_pid"]))
        await asyncio.sleep(0.2)
        await engine.request_stop()
        await _finish(engine, loop)
        return label, waiting, gone, [b["run_number"] for b in _kinds(run_root, "dispatch")]

    label, waiting, gone, dispatched = asyncio.run(scenario())
    assert waiting == [1] and gone and dispatched == [1, 2]
    assert label.endswith(f"(held: {runner_stops.STOP_IN_PROGRESS})")
    first = _kinds(run_root, "outcome")[0]
    assert (first["run_number"], first["cause"]) == (1, "oracle_kill")
    settled = next(
        seq for seq, k, b in rows(run_root) if k == "follow_up" and b.get("event") == "plan"
    )
    second = next(seq for seq, k, b in rows(run_root) if k == "dispatch" and b["run_number"] == 2)
    assert settled < second
