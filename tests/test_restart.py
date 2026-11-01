"""The restart: the live engine starting with a non-empty state (spec-restart, DL-433).

The engine is always up, an engine death ends every running attempt, and at restart the engine
owns nothing alive. The restart refuses before its first write, folds the store, takes one cookie
scan as its first reading, and then the single-writer loop catches up: one observation of each
open attempt and one cleanup of each closing one, in seq order, before any time advance; the owed
plans and the pending starts; then the time jump, after which the barrier lifts and `engine
started` is recorded. No step waits on a process, a wrapper or a record.

Every order is asserted on record seqs, never on elapsed time. The units drive the restart over
hand-written spools and FakeAdapter; the process cases run against real processes on Linux, where
the cookie can be read.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from store_surgery import append, rows, update
from test_runner_control import _control_call

from dsl41 import runner_procid, runner_startup, runner_stops
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, JobAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlServer
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)
GRACE = EngineSettings(grace_term_s=0.2, grace_kill_s=0.3)
linux_cookie = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the process cookie is read on Linux only",
)


def _jil(*jobs: str, extra: str = "") -> str:
    return "".join(f"insert_job: {job}\njob_type: c\ncommand: x\n{extra}\n" for job in jobs)


def _open(
    run_root: Path,
    text: str,
    jobs: list[str],
    *,
    marked: dict[str, str] | None = None,
    at: datetime = T0,
) -> list[dict[str, Any]]:
    """A run root whose engine died with an attempt of each of `jobs` open (FakeAdapter parks
    them); answers their dispatch records in seq order."""

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent=marked,
        )
        for job in jobs:
            engine.inject(Event(at=at, kind="STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(at)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()  # the engine dies: no shutdown record

    asyncio.run(first())
    return [b for _, k, b in rows(run_root) if k == "dispatch"]


def _write(dispatch: dict[str, Any], name: str, **fields: Any) -> None:
    spool = Path(dispatch["spool"])
    spool.mkdir(parents=True, exist_ok=True)
    identity = {
        "version": 1,
        "run_id": dispatch["attempt"],
        "job": dispatch["job"],
        "run_number": dispatch["run_number"],
    }
    (spool / name).write_text(json.dumps({**identity, **fields}))


async def _resume(run_root: Path, text: str, *, minutes: float = 60) -> Engine:
    return await resume_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0 + timedelta(minutes=minutes)),
        adapters={"CMD": FakeAdapter(default=None)},
        settings=GRACE,
    )


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _restart(run_root: Path, text: str, *, minutes: float = 60) -> Engine:
    async def scenario() -> Engine:
        engine = await _resume(run_root, text, minutes=minutes)
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)
        return engine

    return asyncio.run(scenario())


def _seq(run_root: Path, kind: str, **match: Any) -> list[int]:
    return [
        s
        for s, k, b in rows(run_root)
        if k == kind and all(b.get(key) == value for key, value in match.items())
    ]


def _started(run_root: Path) -> int:
    return max(_seq(run_root, "engine", event="started"))


# ------------------------------------------------------------------ §4, §6: the observations


@pytest.mark.parametrize("n", [0, 1, 20])
def test_open_attempts_end_engine_death_in_seq_order_before_the_first_time_advance(
    tmp_path: Path, n: int
) -> None:
    """spec-restart §4, §6: each open attempt with no record ends `engine_death` at its
    observation, in seq order, before the first time advance (`t`'s outage deadline); every
    feed precedes `engine started`; each idempotent attempt reruns once, behind the barrier,
    but `t`, which the time jump ended first."""
    run_root = tmp_path / "run"
    jobs = [f"j{i}" for i in range(n)]
    text = _jil(*jobs) + "insert_job: t\njob_type: c\ncommand: x\nterm_run_time: 30\n"
    dispatches = _open(run_root, text, [*jobs, "t"])

    async def scenario() -> None:
        engine = await _resume(run_root, text)
        # past the instant, so the outage deadline is due (the frontier rule)
        await engine.run_until_quiescent(engine.clock.now() + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(scenario())
    restart_at = _seq(run_root, "engine", event="started")[0]  # genesis's
    outcomes = [(s, b) for s, k, b in rows(run_root) if k == "outcome" and s > restart_at]
    assert [b["attempt"] for _, b in outcomes] == [d["attempt"] for d in dispatches]
    assert {b["cause"] for _, b in outcomes} == {"engine_death"}
    # the first time advance: the outage deadline's input, a verbless one
    advances = [s for s, k, b in rows(run_root) if k == "input" and b.get("kind") is None]
    assert advances and max(s for s, _ in outcomes) < min(advances)
    feeds = _seq(run_root, "input", source="reconcile")
    assert all(s < _started(run_root) for s in feeds)
    reruns = [b for _, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2]
    # `t`'s deadline fell inside the outage: the time jump ends its run before the barrier
    # lifts, so its rerun is never launched
    assert sorted(b["job"] for b in reruns) == sorted(jobs)
    assert all(s > max(s for s, _ in outcomes) for s in _seq(run_root, "dispatch", ordinal=2))


def test_every_open_attempt_is_instantiated_before_the_loop_s_first_step(tmp_path: Path) -> None:
    """spec-restart §2, §11: when `resume_run` returns, every open attempt has its instance,
    a closing one in ENDED with its committed outcome, and the fold has written nothing; the
    catch-up's steps wait for the loop and the engine is STARTING."""
    run_root = tmp_path / "run"
    text = _jil("a", "b")
    first, second = _open(run_root, text, ["a", "b"])
    body = {"attempt": second["attempt"], "job": "b", "run_number": 1, "status": {}}
    body |= {"at": T0.isoformat(), "outcome": "natural", "ladder": "running"}
    append(run_root, "outcome", body, at=T0)
    before = rows(run_root)

    async def scenario() -> tuple[str, str, str | None, str]:
        engine = await _resume(run_root, text)
        tier = engine.attempts
        outcome = tier.outcome(second["attempt"])
        states = (tier.state(first["attempt"]), tier.state(second["attempt"]))
        state = engine.lifecycle.state
        await _close(engine)
        return *states, None if outcome is None else outcome.kind, state

    assert asyncio.run(scenario()) == ("SPAWNING", "ENDED", "natural", "STARTING")
    assert rows(run_root)[: len(before)] == before
    assert [k for _, k, _ in rows(run_root)[len(before) :]] == []


def test_a_success_recorded_before_the_crash_stays_success_at_its_recorded_end(
    tmp_path: Path,
) -> None:
    """spec-restart §6: a natural SUCCESS the wrapper recorded before the crash is fed at its
    recorded end, before the outage deadline after that end, so the deadline never beats it;
    nothing is rerun."""
    run_root = tmp_path / "run"
    text = "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 5\n"
    [dispatch] = _open(run_root, text, ["x"])
    ended = T0 + timedelta(minutes=2)
    _write(dispatch, "status.json", outcome="exited", exit_code=0, ended_at=ended.isoformat())
    engine = _restart(run_root, text, minutes=30)
    assert engine.oracle.store.job["x"].status == "SUCCESS"
    [feed] = [b for _, k, b in rows(run_root) if k == "input" and b.get("source") == "reconcile"]
    assert feed["at"] == ended.isoformat()
    assert _seq(run_root, "dispatch", ordinal=2) == []


@pytest.mark.parametrize("marked", [False, True], ids=["idempotent", "marked"])
def test_parent_lost_is_stopped_lifeline_and_reruns_or_holds(tmp_path: Path, marked: bool) -> None:
    """spec-restart §4: `parent lost` is `stopped{lifeline}`; an idempotent job reruns, a
    marked one is FAILURE `interrupted` and held, with the evidence under `status`."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"], marked={"j": "posts"} if marked else None)
    _write(dispatch, "status.json", outcome="terminated", cause="parent lost")
    engine = _restart(run_root, text)
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert (outcome["outcome"], outcome["cause"]) == ("stopped", "lifeline")
    [plan] = [b for _, k, b in rows(run_root) if k == "follow_up" and b["event"] == "plan"]
    assert plan["plan"] == ("hold" if marked else "rerun")
    row = engine.oracle.store.job["j"]
    if marked:
        assert (row.status, row.on_hold) == ("FAILURE", True)
        assert engine.evidence["j"]["cause"] == "lifeline"
    else:
        assert [b["ordinal"] for _, k, b in rows(run_root) if k == "dispatch"] == [1, 2]


def test_a_closing_attempt_is_never_observed_again(tmp_path: Path) -> None:
    """spec-restart §5: an attempt whose outcome was committed while its ladder ran is
    restored ENDED: its cleanup finds nothing left and closes it, its record is never read
    again, and its plan dispatches exactly one replacement. One `outcome` and one plan."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    run_id = dispatch["attempt"]
    _write(dispatch, "status.json", outcome="exited", exit_code=0)  # read by nobody
    body = {"attempt": run_id, "job": "j", "run_number": 1, "status": {}, "at": T0.isoformat()}
    append(
        run_root,
        "outcome",
        {**body, "outcome": "stopped", "cause": "engine_stop", "ladder": "running"},
        at=T0,
    )
    step = {"step": "dispatch", "attempt": "9d1f2e3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f", "ordinal": 2}
    step |= {"env": {}, "after": "engine_stop"}
    plan = {"event": "plan", "attempt": run_id, "job": "j", "run_number": 1, "plan": "rerun"}
    append(run_root, "follow_up", {**plan, "steps": [step]}, at=T0)
    _restart(run_root, text)
    assert len(_seq(run_root, "outcome")) == 1
    assert len(_seq(run_root, "follow_up", event="plan")) == 1
    [done] = _seq(run_root, "follow_up", event="ladder_done")
    [rerun] = _seq(run_root, "dispatch", ordinal=2)
    assert done < rerun


def test_a_marked_job_interrupted_is_held_until_force_startjob(tmp_path: Path) -> None:
    """spec-restart §12: a marked job interrupted is FAILURE `interrupted` and ON_HOLD, its
    evidence under `status`; a STARTJOB starts nothing, FORCE_STARTJOB clears it and runs."""
    run_root = tmp_path / "run"
    text = _jil("k")
    _open(run_root, text, ["k"], marked={"k": "posts twice"})

    async def scenario() -> None:
        engine = await _resume(run_root, text)
        await engine.run_until_quiescent(engine.clock.now())
        row = engine.oracle.store.job["k"]
        assert (row.status, row.on_hold) == ("FAILURE", True)
        assert engine.evidence["k"]["cause"] == "engine_death"
        at = engine.clock.now()
        engine.inject(Event(at=at, kind="STARTJOB", payload={"job": "k"}))
        await engine.run_until_quiescent(at)
        assert engine.oracle.store.job["k"].run_number == 1
        engine.inject(Event(at=at, kind="FORCE_STARTJOB", payload={"job": "k"}))
        await engine.run_until_quiescent(at)
        assert engine.oracle.store.job["k"].run_number == 2 and "k" not in engine.evidence
        await _close(engine)

    asyncio.run(scenario())


def test_a_rerun_keeps_its_invocation_across_two_crashes(tmp_path: Path) -> None:
    """spec-restart §12: the replacement keeps `DSL41_JOB`, `DSL41_RUN_NUMBER` and
    `DSL41_SCHEDULED_AT` across two engine deaths, each a new attempt of the same run; a job
    with `n_retrys: 1` interrupted once is rerun once, never retried."""
    run_root = tmp_path / "run"
    text = _jil("j", extra="n_retrys: 1\n")
    _open(run_root, text, ["j"])

    async def crash_again(minutes: float) -> None:
        engine = await _resume(run_root, text, minutes=minutes)
        await engine.run_until_quiescent(engine.clock.now())
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()  # dies again: no shutdown record

    asyncio.run(crash_again(30))
    asyncio.run(crash_again(60))
    attempts = [b for _, k, b in rows(run_root) if k == "dispatch"]
    assert [(b["run_number"], b["ordinal"]) for b in attempts] == [(1, 1), (1, 2), (1, 3)]
    for name in ("DSL41_JOB", "DSL41_RUN_NUMBER", "DSL41_SCHEDULED_AT"):
        assert len({b["env"][name] for b in attempts}) == 1


# ------------------------------------------------------------------ §7: quarantine


def _quarantined(run_root: Path, engine: Engine, job: str, reason: str) -> None:
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome" and b["job"] == job]
    assert (outcome["outcome"], outcome["cause"]) == ("unobservable", "quarantined")
    assert reason in outcome["status"]["note"]
    [plan] = [
        b
        for _, k, b in rows(run_root)
        if k == "follow_up" and b.get("job") == job and b.get("plan")
    ]
    assert plan["plan"] == "hold" and reason in plan["evidence"]["note"]
    # the outcome record closes it as live: no `ladder_done` of its own (Opus round 1, R1)
    assert not _seq(run_root, "follow_up", event="ladder_done", attempt=outcome["attempt"])
    assert engine.started.is_set() and engine.oracle.store.job[job].on_hold


def _release_runs(run_root: Path, text: str, job: str) -> None:
    """The operator's release runs the job (DL-243)."""

    async def scenario() -> None:
        engine = await _resume(run_root, text, minutes=90)
        await engine.run_until_quiescent(engine.clock.now())
        at = engine.clock.now()
        engine.inject(Event(at=at, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(at)
        assert engine.oracle.store.job[job].run_number == 2
        await _close(engine)

    asyncio.run(scenario())


def test_a_status_record_naming_another_run_is_quarantined_and_the_rest_runs(
    tmp_path: Path,
) -> None:
    """spec-restart §7: a `status.json` naming another run id than the dispatch bound is never
    acted on. The attempt is quarantined; `o`, observed beside it, reruns; the engine is up;
    the operator's release runs the job."""
    run_root = tmp_path / "run"
    text = _jil("j", "o")
    first = _open(run_root, text, ["j", "o"])[0]
    _write(first, "status.json", run_id="00000000-0000-4000-8000-00000000beef", outcome="exited")
    engine = _restart(run_root, text)
    _quarantined(run_root, engine, "j", "00000000-0000-4000-8000-00000000beef")
    assert [b["job"] for _, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2] == [
        "o"
    ]
    _release_runs(run_root, text, "j")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory whatever its mode")
def test_an_unreadable_spool_is_quarantined(tmp_path: Path) -> None:
    """spec-restart §7: a spool directory that cannot be listed quarantines the attempt."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    spool = Path(dispatch["spool"])
    spool.mkdir(parents=True)
    spool.chmod(0)
    try:
        engine = _restart(run_root, text)
    finally:
        spool.chmod(0o700)
    _quarantined(run_root, engine, "j", "spool unreadable")
    _release_runs(run_root, text, "j")


def test_a_failed_first_reading_lets_the_record_decide_and_holds_what_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-restart §7 (Opus round 1, R2-R4): a failed reading is never a quarantine. The scan
    fails with EIO; the SUCCESS the wrapper recorded decides the outcome, `natural`, and the
    run completes; what may be left is held as live, `scan failed: EIO`, re-read at each look,
    nothing released and nothing signalled while the readings fail."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    _write(dispatch, "status.json", outcome="exited", exit_code=0)
    sent: list[int] = []

    def signal_ours(pid: int, *_: object, **_kwargs: Any) -> bool:
        sent.append(pid)
        return True

    failed = runner_procid.Ours({}, error=errno.EIO)
    monkeypatch.setattr(runner_procid, "ours", lambda *_: failed)
    monkeypatch.setattr(runner_procid, "signal_ours", signal_ours)
    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: True)

    async def scenario() -> tuple[str, list[str], str | None]:
        engine = await _resume(run_root, text)
        await engine.run_until_quiescent(engine.clock.now())
        engine.stops._look()  # a look while the readings fail
        status = engine.oracle.store.job["j"].status
        held = list(engine.held_groups)
        note = engine.attempt_facts(dispatch["attempt"]).note
        await _close(engine)
        return status, held, note

    status, held, note = asyncio.run(scenario())
    assert (status, held, note) == ("SUCCESS", [dispatch["attempt"]], "scan failed: EIO")
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert (outcome["outcome"], outcome["ladder"]) == ("natural", "running")
    assert not _seq(run_root, "follow_up", event="ladder_done") and sent == []


# ------------------------------------------------------------------ §8: live from the first step


def test_the_socket_answers_during_the_catch_up_and_status_says_starting(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-restart §8, ruling R2 (Opus round 1, R7): the socket is served while the loop takes
    the catch-up's steps. Between the first and the second step, `status` says STARTING and
    shows `a` already ended and `b`'s attempt still open; once the barrier lifts, RUNNING."""
    run_root = short_root / "run"
    text = _jil("a", "b")
    _open(run_root, text, ["a", "b"])
    path = run_root / "control.sock"
    real = Engine._control
    served: list[dict[str, Any]] = []
    taken: list[int] = []

    async def control(self: Engine, pending: Any) -> None:
        if pending.restart is not None:
            taken.append(1)
            if len(taken) == 2:  # one step taken, the rest still queued
                served.append(await _control_call(path, {"cmd": "status"}))
        await real(self, pending)

    monkeypatch.setattr(Engine, "_control", control)

    async def scenario() -> dict[str, Any]:
        engine = await _resume(run_root, text)
        server = ControlServer(engine, path)
        await server.start()
        try:
            loop = asyncio.ensure_future(engine.run_until_quiescent(engine.clock.now()))
            await loop
            after = await _control_call(path, {"cmd": "status"})
        finally:
            await server.close()
            await _close(engine)
        return after

    after = asyncio.run(scenario())
    [during] = served
    assert during["engine"] == "STARTING"
    assert "attempt" not in during["jobs"]["a"]  # observed: ended, its rerun behind the barrier
    assert during["jobs"]["b"]["attempt"]["state"] == "SPAWNING"
    assert after["engine"] == "RUNNING"


def test_a_stop_during_the_catch_up_is_shutdown_during_startup_and_ends_each_attempt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec-restart §8, DL-428: a stop taken during the catch-up ends the engine with
    `shutdown_during_startup`; the steps not taken are dropped, and the next restart takes them.
    Each attempt ends exactly once across both restarts."""
    run_root = tmp_path / "run"
    text = _jil("a", "b")
    at = T0 + timedelta(minutes=5)
    _open(run_root, text, ["a", "b"], at=at)
    real = runner_startup._observe
    stopped: list[bool] = []

    def observe_then_stop(engine: Engine, *args: Any) -> None:
        real(engine, *args)
        if not stopped:
            stopped.append(True)
            engine.request_stop()  # stamped before the catch-up's steps: taken next

    monkeypatch.setattr(runner_startup, "_observe", observe_then_stop)

    async def interrupted() -> None:
        # the clock reads before the journal's last instant, so the stop sorts first
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=GRACE,
        )
        await engine.run_until_quiescent(at)
        assert engine.leaving_record == "shutdown_during_startup"
        await _close(engine)

    asyncio.run(interrupted())
    assert len(_seq(run_root, "outcome")) == 1
    monkeypatch.setattr(runner_startup, "_observe", real)
    _restart(run_root, text)
    events = [b["event"] for _, k, b in rows(run_root) if k == "engine"]
    assert events.index("shutdown_during_startup") < len(events) - 1
    assert sorted(b["job"] for _, k, b in rows(run_root) if k == "outcome") == ["a", "b"]


# ------------------------------------------------------------------ §1, §3: the root and the cookie


def test_the_root_id_names_every_cookie_across_engine_starts(tmp_path: Path) -> None:
    """spec-restart §3: `root_id` is written at genesis and read at every restart; every
    command attempt's cookie names it, whichever engine start dispatched it."""
    from dsl41.runner_adapters import LocalCommandAdapter

    run_root = tmp_path / "run"
    text = _jil("j")
    _open(run_root, text, ["j"])
    with closing(sqlite3.connect(run_root / "engine.db")) as db:
        (root_id,) = db.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()

    async def scenario() -> tuple[str, str]:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(hours=1)),
            adapters={"CMD": cast(JobAdapter, LocalCommandAdapter(grace_seconds=0.1))},
            settings=GRACE,
        )
        engine._launch = lambda *_, **__: None  # type: ignore[method-assign]
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)
        return engine.root_id, engine.instance

    root, instance = asyncio.run(scenario())
    assert root == root_id and instance != root_id
    [rerun] = [b for _, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2]
    assert rerun["env"]["DSL41_COOKIE"] == f"{root_id}.{rerun['attempt']}"


@pytest.mark.parametrize("damage", ["no_root_id", "state_version"])
def test_a_store_with_no_root_id_or_another_state_version_refuses_by_name(
    tmp_path: Path, damage: str
) -> None:
    """spec-restart §1: refused before the first write, naming what is wrong."""
    run_root = tmp_path / "run"
    text = _jil("j")
    _open(run_root, text, ["j"])
    before = rows(run_root)
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        if damage == "no_root_id":
            db.execute("DELETE FROM meta WHERE key = 'root_id'")
        else:
            db.execute("UPDATE meta SET value = '26' WHERE key = 'state_version'")
    match = "no root_id" if damage == "no_root_id" else "state version 26, this binary runs 27"
    with pytest.raises(EngineError, match=match):
        asyncio.run(_resume(run_root, text))
    assert rows(run_root) == before


def _cookied(run_root: Path, run_id: str, script: str) -> subprocess.Popen[bytes]:
    """A process in a group of its own carrying the cookie of `run_id` in `run_root`."""
    with closing(sqlite3.connect(run_root / "engine.db")) as db:
        (root_id,) = db.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()
    env = {**os.environ, "DSL41_COOKIE": runner_procid.cookie(root_id, run_id)}
    return subprocess.Popen(["/bin/sh", "-c", script], env=env, start_new_session=True)


def _set_cookie(run_root: Path) -> None:
    with closing(sqlite3.connect(run_root / "engine.db")) as db:
        (root_id,) = db.execute("SELECT value FROM meta WHERE key = 'root_id'").fetchone()
    for seq, kind, body in rows(run_root):
        if kind == "dispatch":
            cookie = runner_procid.cookie(root_id, body["attempt"])
            update(run_root, seq, {**body, "env": {**body["env"], "DSL41_COOKIE": cookie}})


async def _ready(path: Path) -> None:
    async with asyncio.timeout(10):
        while not path.exists():
            await asyncio.sleep(0.01)


@linux_cookie
@pytest.mark.parametrize("spawn", [True, False], ids=["spawn_json", "no_spawn_json"])
def test_a_live_command_with_no_record_ends_engine_death_and_the_loop_stops_it(
    tmp_path: Path, spawn: bool
) -> None:
    """spec-restart §3, §4: the command outlived its engine and its wrapper. The first reading
    TERMs it (it ignores TERM); the attempt ends `engine_death` with `ladder: running`; the
    ladder KILLs it in the loop; `ladder_done` lands once a reading finds nothing left, and only
    then is the rerun dispatched. A record written after the observation is read by nobody.
    With no `spawn.json` the cookie alone names it."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    ready = tmp_path / "ready"
    child = _cookied(run_root, dispatch["attempt"], f"trap '' TERM; touch {ready}; sleep 60")
    try:
        asyncio.run(_ready(ready))
        if spawn:
            token = runner_procid.proc_start_token(child.pid)
            _write(
                dispatch,
                "spawn.json",
                command_pid=child.pid,
                command_pgid=child.pid,
                command_start_time=token,
                wrapper_pid=1,
                wrapper_start_time="ticks:1",
                boot_id=runner_procid.current_boot_id(),
            )

        async def scenario() -> None:
            engine = await _resume(run_root, text)
            await engine.run_until_quiescent(engine.clock.now())
            _write(dispatch, "status.json", outcome="exited", exit_code=0)  # too late
            await engine.run_until_quiescent(engine.clock.now())
            await _close(engine)

        asyncio.run(scenario())
        assert child.wait(5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    [(seq, outcome)] = [(s, b) for s, k, b in rows(run_root) if k == "outcome"]
    assert (outcome["cause"], outcome["ladder"]) == ("engine_death", "running")
    [done] = _seq(run_root, "follow_up", event="ladder_done")
    [rerun] = _seq(run_root, "dispatch", ordinal=2)
    assert seq < done < rerun


@linux_cookie
def test_two_roots_under_one_uid_do_not_cross_read(tmp_path: Path) -> None:
    """spec-restart §3: the first reading reads this root's cookies only. A process the
    previous engine of this root launched for an open attempt is found and TERMed; another
    root's process, under the same uid and for an attempt open there, is never touched."""
    text = _jil("j")
    one, two = tmp_path / "one", tmp_path / "two"
    [mine] = _open(one, text, ["j"])
    [theirs] = _open(two, text, ["j"])
    _set_cookie(one)  # the first reading reads each open attempt through its cookie
    _set_cookie(two)
    a = _cookied(one, mine["attempt"], f"touch {tmp_path / 'a'}; exec sleep 60")
    b = _cookied(two, theirs["attempt"], f"touch {tmp_path / 'b'}; exec sleep 60")
    try:
        for ready in ("a", "b"):  # each has exec'd with its cookie
            asyncio.run(_ready(tmp_path / ready))

        async def scenario() -> set[str]:
            engine = await _resume(one, text)
            termed = set(engine.stops.termed)
            await _close(engine)
            return termed

        assert asyncio.run(scenario()) == {mine["attempt"]}
        assert a.wait(5) == -signal.SIGTERM
        assert b.poll() is None
    finally:
        for proc in (a, b):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# ------------------------------------------------------------------ the units of the fold


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_record_that_cannot_be_read_is_quarantined(tmp_path: Path) -> None:
    """spec-restart §7: a `status.json` that exists and cannot be read quarantines the
    attempt; nothing reads it as absent."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _write(dispatch, "status.json", outcome="exited", exit_code=0)
    status = Path(dispatch["spool"]) / "status.json"
    status.chmod(0)
    try:
        engine = _restart(run_root, text)
    finally:
        status.chmod(0o600)
    _quarantined(run_root, engine, "j", "status.json unreadable")


def test_a_watch_log_that_does_not_read_is_a_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec-restart §7: a `watch.jsonl` the reader refuses quarantines the watch, as one that
    names another run does; one that names this run, or none, takes the watch up."""
    from dsl41.runner_adapters import WatchLog
    from dsl41.runner_journal import OpenAttempt

    attempt = OpenAttempt("r1", "w", 1, 1, "/nonexistent", 0, T0.isoformat())

    def refused(_spool: Path) -> WatchLog:
        raise EngineError("watch.jsonl: empty interior line 2")

    monkeypatch.setattr(runner_startup, "read_watch_log", refused)
    assert runner_startup._watch_split(attempt) == "watch.jsonl: empty interior line 2"
    monkeypatch.setattr(runner_startup, "read_watch_log", lambda _spool: None)
    assert runner_startup._watch_split(attempt) is None


def test_a_closing_attempt_whose_spawn_record_does_not_read_is_cleaned_by_its_cookie(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """spec-restart §5: a closing attempt's `spawn.json` naming another run names no group:
    the cleanup says so, reads the cookie's processes alone, finds nothing here, and closes."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    run_id = dispatch["attempt"]
    _write(dispatch, "spawn.json", run_id="00000000-0000-4000-8000-00000000beef")
    body = {"attempt": run_id, "job": "j", "run_number": 1, "status": {}, "at": T0.isoformat()}
    append(run_root, "outcome", {**body, "outcome": "natural", "ladder": "running"}, at=T0)
    _restart(run_root, text)
    assert "spawn.json reports run_id" in capfd.readouterr().err
    assert _seq(run_root, "follow_up", event="ladder_done", attempt=run_id)
    assert len(_seq(run_root, "outcome")) == 1


def test_the_first_reading_terms_what_is_owed_and_reports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """spec-restart §3: of the processes carrying a cookie of this root, an open attempt's
    without an outcome is TERMed (its ladder's first rung) unless it is already gone; a
    closing `natural` attempt's under `process-leak=leak` is named and left running; one of no
    open attempt is reported once with its job and run when the tail names them, and left."""
    run_root = tmp_path / "run"
    text = _jil("a", "b")
    first, second = _open(run_root, text, ["a", "b"])
    body = {"attempt": second["attempt"], "job": "b", "run_number": 1, "status": {}}
    body |= {"at": T0.isoformat(), "outcome": "natural", "ladder": "running"}
    append(run_root, "outcome", body, at=T0)
    gone = "00000000-0000-4000-8000-0000000000aa"
    append(run_root, "dispatch", {**first, "attempt": gone, "ordinal": 9}, at=T0)
    append(run_root, "outcome", {**body, "attempt": gone, "job": "a"} | {"ladder": None}, at=T0)
    _set_cookie(run_root)  # the first reading reads each attempt through its cookie
    found = {
        11: (first["attempt"], "ticks:11"),
        12: (first["attempt"], "ticks:12"),
        13: (second["attempt"], "ticks:13"),
        14: (gone, "ticks:14"),
        15: ("00000000-0000-4000-8000-0000000000bb", "ticks:15"),
    }
    sent: list[tuple[int, int]] = []

    def signal_ours(pid: int, _token: str, sig: int, **_kwargs: Any) -> bool:
        sent.append((pid, sig))
        return pid != 12  # 12 exited meanwhile

    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: True)
    monkeypatch.setattr(runner_procid, "ours", lambda *_: runner_procid.Ours(found))
    monkeypatch.setattr(runner_procid, "signal_ours", signal_ours)
    monkeypatch.setattr(runner_stops.EngineStops, "leak_policy", property(lambda _s: "leak"))

    async def scenario() -> set[str]:
        engine = await _resume(run_root, text)
        termed = set(engine.stops.termed)
        await _close(engine)
        return termed

    assert asyncio.run(scenario()) == {first["attempt"]}
    assert sent == [(11, signal.SIGTERM), (12, signal.SIGTERM)]
    err = capfd.readouterr().err
    assert "b.1.1: leaked: 1, left running (pids 13)" in err
    assert f"a.1: run id {gone} has no open attempt" in err
    assert "?.?: run id 00000000-0000-4000-8000-0000000000bb has no open attempt" in err


def test_grace_seconds_is_the_live_ladder_s_grace(tmp_path: Path) -> None:
    """`resume_run(grace_seconds=...)` is the TERM grace of this incarnation's ladders, over the
    settings' `grace_term_s`; there is no settle window (spec-restart §9)."""
    run_root = tmp_path / "run"
    text = _jil("j")
    _open(run_root, text, ["j"])

    async def scenario() -> float:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            grace_seconds=0.7,
        )
        grace = engine.attempts.grace_term_s
        await _close(engine)
        return grace

    assert asyncio.run(scenario()) == 0.7


@linux_cookie
@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_the_restart_cookie_scan_stops_an_escape_with_either_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pidfd: bool
) -> None:
    """A restart finds an unrecorded escape by cookie, TERMs it before the loop, then runs
    the grace and KILL with either primitive (DL-433, DL-436)."""
    if pidfd and not runner_procid.pidfd_supported():
        pytest.skip("this Python or kernel has no pidfd")
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    [dispatch] = [body for _, kind, body in rows(run_root) if kind == "dispatch"]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,signal,time;os.setsid();"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)",
        ],
        env={**os.environ, **dispatch["env"]},
        stdout=subprocess.PIPE,
    )
    sent: list[int] = []
    try:
        assert child.stdout is not None and child.stdout.readline() == b"ready\n"
        monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: pidfd)
        if pidfd:
            real_send = signal.pidfd_send_signal

            def send(fd: int, sig: int) -> None:
                sent.append(sig)
                real_send(fd, sig)

            monkeypatch.setattr(signal, "pidfd_send_signal", send)
        else:
            real_kill = os.kill

            def kill(pid: int, sig: int) -> None:
                if pid == child.pid and sig:
                    sent.append(sig)
                real_kill(pid, sig)

            monkeypatch.setattr(os, "kill", kill)

        async def scenario() -> None:
            engine = await _resume(run_root, text)
            try:
                assert sent == [signal.SIGTERM]
                assert child.poll() is None
                await engine.run_until_quiescent(engine.clock.now())
                await engine.attempts.join()
                assert child.wait(5) == -signal.SIGKILL
                assert sent == [signal.SIGTERM, signal.SIGKILL]
            finally:
                await _close(engine)

        asyncio.run(scenario())
    finally:
        child.kill()
        child.wait()
        assert child.stdout is not None
        child.stdout.close()


# ------------------------------------------------------------------ §3: what ended by its record


@pytest.mark.parametrize("policy", ["terminate", "leak"])
def test_the_first_reading_leaves_what_ended_by_its_own_record_to_the_live_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], policy: str
) -> None:
    """spec-restart §3, DL-433: the scan finishes what the engine's death ended; what ended by
    its own record follows the live rule. `a` left a natural record: its leftover 21 is not
    TERMed at the scan; at its observation the leak rule ladders it under `terminate` and
    leaves it with the warning under `leak`. `b` (a stop's record), `c` (no record) and `d` (a
    record that does not read) ended with the engine: 22, 23 and 24 are TERMed at the scan."""
    run_root = tmp_path / "run"
    text = _jil("a", "b", "c", "d")
    a, b, c, d = _open(run_root, text, ["a", "b", "c", "d"])
    _set_cookie(run_root)
    _write(a, "status.json", outcome="exited", exit_code=0)
    _write(b, "status.json", outcome="terminated", cause="parent lost")
    _write(d, "status.json", run_id="00000000-0000-4000-8000-00000000beef", outcome="exited")
    alive = {21: a, 22: b, 23: c, 24: d}  # each ignores TERM
    sent: list[tuple[int, int]] = []

    def ours(*_: object) -> runner_procid.Ours:
        return runner_procid.Ours({p: (x["attempt"], f"ticks:{p}") for p, x in alive.items()})

    def signal_ours(pid: int, _token: object, sig: int, **_kwargs: Any) -> bool:
        sent.append((pid, sig))
        if sig == signal.SIGKILL:
            alive.pop(pid, None)
        return True

    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: True)
    monkeypatch.setattr(runner_procid, "ours", ours)
    monkeypatch.setattr(runner_procid, "signal_ours", signal_ours)
    monkeypatch.setattr(runner_procid, "verify_alive", lambda pid, _t: pid in alive)
    monkeypatch.setattr(runner_stops.EngineStops, "leak_policy", property(lambda _s: policy))
    at_the_scan: list[tuple[int, int]] = []

    async def scenario() -> None:
        engine = await _resume(run_root, text)
        at_the_scan.extend(sent)
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)

    asyncio.run(scenario())
    assert at_the_scan == [(22, signal.SIGTERM), (23, signal.SIGTERM), (24, signal.SIGTERM)]
    later = sent[len(at_the_scan) :]
    [outcome] = [x for _, k, x in rows(run_root) if k == "outcome" and x["job"] == "a"]
    assert outcome["outcome"] == "natural"
    if policy == "terminate":
        assert [s for p, s in later if p == 21] == [signal.SIGTERM, signal.SIGKILL]
        assert outcome["ladder"] == "running"
        assert _seq(run_root, "follow_up", event="ladder_done", attempt=a["attempt"])
    else:
        assert [s for p, s in later if p == 21] == []
        assert "a.1.1: leaked: 1, left running (pids 21)" in capfd.readouterr().err


@linux_cookie
@pytest.mark.parametrize("policy", ["terminate", "leak"])
def test_a_leftover_of_a_natural_record_follows_the_leak_rule_not_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], policy: str
) -> None:
    """spec-restart §3, DL-433, against a real process: the command recorded a natural exit
    and left a child carrying its cookie. The scan sends it nothing; at the observation the
    live leak rule ladders it under `terminate` (`ladder: running`, then `ladder_done`) and
    leaves it running with the warning under `leak`."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    _write(dispatch, "status.json", outcome="exited", exit_code=0)
    ready, termed = tmp_path / "ready", tmp_path / "termed"
    child = _cookied(
        run_root,
        dispatch["attempt"],
        f"trap 'touch {termed}' TERM; touch {ready}; while :; do sleep 0.1; done",
    )
    monkeypatch.setattr(runner_stops.EngineStops, "leak_policy", property(lambda _s: policy))
    try:
        asyncio.run(_ready(ready))

        async def scenario() -> bool:
            engine = await _resume(run_root, text)
            termed_at_the_scan = termed.exists()
            await engine.run_until_quiescent(engine.clock.now())
            await _close(engine)
            return termed_at_the_scan

        assert asyncio.run(scenario()) is False
        if policy == "terminate":
            assert child.wait(5) == -signal.SIGKILL and termed.exists()
        else:
            assert child.poll() is None and not termed.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert outcome["outcome"] == "natural"
    if policy == "terminate":
        assert outcome["ladder"] == "running"
        assert _seq(run_root, "follow_up", event="ladder_done")
    else:  # the shell and its `sleep` both carry the cookie
        err = capfd.readouterr().err
        assert "j.1.1: leaked: " in err and "left running" in err and str(child.pid) in err


# ------------------------------------------------------------------ round 1 rework


class _Box:
    """Stand-in processes for the restart's readings: pid -> (group, carries the cookie of
    `run_id`, ignores TERM). The scan, the group listing, the token check and the signals read
    and act on it, so a signal changes what the next reading finds."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, run_id: str, **procs: Any) -> None:
        self.run_id = run_id
        self.procs: dict[int, tuple[int, bool, bool]] = {int(k[1:]): v for k, v in procs.items()}
        self.sent: list[tuple[int, int]] = []
        monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: True)
        monkeypatch.setattr(runner_procid, "ours", self._ours)
        monkeypatch.setattr(runner_procid, "linux_group_pids", self._group)
        monkeypatch.setattr(runner_procid, "verify_alive", lambda pid, _t: pid in self.procs)
        monkeypatch.setattr(runner_procid, "signal_ours", self._signal)

    def _ours(self, *_: object) -> runner_procid.Ours:
        found = {p: (self.run_id, f"ticks:{p}") for p, (_g, c, _i) in self.procs.items() if c}
        return runner_procid.Ours(found)

    def _group(self, pgid: int) -> dict[int, str]:
        return {p: f"ticks:{p}" for p, (g, _c, _i) in self.procs.items() if g == pgid}

    def _signal(self, pid: int, _start: str, sig: int, **_kwargs: Any) -> bool:
        self.sent.append((pid, sig))
        if pid in self.procs and (sig == signal.SIGKILL or not self.procs[pid][2]):
            del self.procs[pid]
        return True


def test_the_first_reading_proves_the_group_before_its_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra round 1 B1 (spec-restart §3): the first reading reads as live, then TERMs. The
    cookie member 50 of the recorded group proves it and exits on that TERM; 51, a member
    without the cookie, ignores it. The proof holds while the group has members, so the
    cleanup's ladder KILLs 51, and the rerun dispatches only after `ladder_done`."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    pgid = 40000
    _write(
        dispatch,
        "spawn.json",
        command_pid=pgid,
        command_pgid=pgid,
        command_start_time=f"ticks:{pgid}",
        wrapper_pid=1,
        wrapper_start_time="ticks:1",
        boot_id=runner_procid.current_boot_id(),
    )
    box = _Box(monkeypatch, dispatch["attempt"], p50=(pgid, True, False), p51=(pgid, False, True))
    _restart(run_root, text)
    assert box.sent[:2] == [(50, signal.SIGTERM), (51, signal.SIGTERM)]
    assert (51, signal.SIGKILL) in box.sent and box.procs == {}
    [done] = _seq(run_root, "follow_up", event="ladder_done")
    [rerun] = _seq(run_root, "dispatch", ordinal=2)
    assert done < rerun


def test_a_quarantine_s_cookie_processes_get_grace_and_kill_at_its_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus round 1 R4 (spec-restart §7): records that cannot be trusted prove no group; what
    is left of the quarantined attempt is the live cleanup through its cookie alone, so the
    first reading's TERM gets its grace and KILL, and the note names the pids it found."""
    run_root = tmp_path / "run"
    text = _jil("j")
    [dispatch] = _open(run_root, text, ["j"])
    _set_cookie(run_root)
    _write(dispatch, "status.json", run_id="00000000-0000-4000-8000-00000000beef")
    box = _Box(monkeypatch, dispatch["attempt"], p61=(61, True, True))
    _restart(run_root, text)
    assert box.sent == [(61, signal.SIGTERM), (61, signal.SIGKILL)]
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert outcome["cause"] == "quarantined" and "(pids 61)" in outcome["status"]["note"]
    assert outcome["ladder"] == "running"
    assert _seq(run_root, "follow_up", event="ladder_done")


def test_a_quarantine_of_a_run_the_oracle_ended_has_no_plan_and_keeps_its_evidence(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """Opus round 1 R2 (spec-restart §7): where the oracle already ended the run, the plan is
    `none` as for any outcome; the reason stays in the outcome record and the stderr report."""
    run_root = tmp_path / "run"
    text = _jil("j")

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
        at = T0 + timedelta(minutes=1)
        engine.inject(Event(at=at, kind="STATUS", payload={"job": "j", "status": "SUCCESS"}))
        await engine.run_until_quiescent(at)  # the oracle ended the run; its attempt is open
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())
    [dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch"]
    _write(dispatch, "status.json", run_id="00000000-0000-4000-8000-00000000beef")
    engine = _restart(run_root, text)
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert outcome["cause"] == "quarantined" and "beef" in outcome["status"]["note"]
    [plan] = [b for _, k, b in rows(run_root) if k == "follow_up" and b["event"] == "plan"]
    assert plan["plan"] == "none" and not engine.oracle.store.job["j"].on_hold
    assert "j.1.1: quarantined: status.json reports run_id" in capfd.readouterr().err


def test_the_fold_writes_nothing_for_file_watches_either(tmp_path: Path) -> None:
    """Opus round 1 R5, Astra round 1 M2 (spec-restart §2, §11): a file watch is taken up as a
    catch-up step in seq order, never in the fold. `w`'s log completed while the engine was
    down; `x`'s run ended. Nothing is written when `resume_run` returns; in the loop, `w`
    completes and `x` is closed with no plan, each through the live outcome path."""
    from dsl41.canon import canonical_bytes
    from dsl41.runner_adapters import ARTIFACT_FORMAT_VERSION, WATCH_LOG

    run_root = tmp_path / "run"
    text = "".join(
        f"insert_job: {job}\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n\n"
        for job in ("w", "x")
    )

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FakeAdapter(default=None)},
        )
        for job in ("w", "x"):
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
        at = T0 + timedelta(minutes=1)
        engine.inject(Event(at=at, kind="STATUS", payload={"job": "x", "status": "INACTIVE"}))
        await engine.run_until_quiescent(at)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())
    [watch] = [b for _, k, b in rows(run_root) if k == "dispatch" and b["job"] == "w"]
    line = {"artifact_format_version": ARTIFACT_FORMAT_VERSION, "run_id": watch["attempt"]}
    start = {**line, "at": "2026-07-01T08:00:00.000000", "kind": "start"}
    polls = [
        {**line, "at": f"2026-07-01T08:0{n}:00.000000", "exists": True, "kind": "poll"}
        | {"qualifying": True, "size": 5, "stable_polls": n}
        for n in (1, 2)
    ]
    spool = Path(watch["spool"])
    spool.mkdir(parents=True, exist_ok=True)
    (spool / WATCH_LOG).write_bytes(b"".join(canonical_bytes(d) + b"\n" for d in [start, *polls]))
    before = rows(run_root)

    async def scenario() -> list[Any]:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=60)),
            adapters={"FW": FakeAdapter(default=None)},
            settings=GRACE,
        )
        written = rows(run_root)[len(before) :]
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)
        return written

    assert asyncio.run(scenario()) == []
    outcomes = {b["job"]: b for _, k, b in rows(run_root) if k == "outcome"}
    assert outcomes["w"]["outcome"] == "natural"
    assert (outcomes["x"]["outcome"], outcomes["x"]["cause"]) == ("stopped", "run ended")
    plans = {
        b["job"]: b["plan"] for _, k, b in rows(run_root) if k == "follow_up" and b.get("plan")
    }
    assert plans == {"w": "complete", "x": "none"}


def test_a_completed_watch_a_resumed_update_touches_is_fed_before_the_application(
    tmp_path: Path,
) -> None:
    """spec-restart §6, DL-432: a file watch in an update's stop set is neither waited for nor
    kept (the live update cancels it and takes it up after the application; the resumed update
    leaves it out of the stops it waits for). `w`'s watch completed while the engine was down and the plan changes `w`: its outcome,
    its `complete` plan and its STATUS feed land under the old catalog, before `config
    applied`, as every recovered outcome outside the stop set does; a completed run is not
    taken up again."""
    from test_update_settlement import _estate

    from dsl41.canon import canonical_bytes
    from dsl41.reload_plan import load_estate, reread
    from dsl41.runner_adapters import ARTIFACT_FORMAT_VERSION, WATCH_LOG
    from dsl41.runner_store import Record
    from dsl41.runner_update import UpdateRequest

    run_root = tmp_path / "run"

    def jil(command: str, watched: str) -> str:
        return (  # the anchor keeps the estate free of dangling-job warnings
            "insert_job: anchor\njob_type: c\ncommand: true\n\n"
            f"insert_job: j\njob_type: c\ncommand: {command}\ncondition: s(anchor)\n\n"
            f"insert_job: w\njob_type: f\nwatch_file: {watched}\ncondition: s(anchor)\n"
        )

    adapters = {"CMD": FakeAdapter(default=None), "FW": FakeAdapter(default=None)}

    async def first() -> None:
        engine = start_run(
            load_estate(_estate(tmp_path, jil("x", "/tmp/dsl41-one"))).catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters=dict(adapters),
            settings=GRACE,
        )
        for job in ("j", "w"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        assert engine.journal is not None
        table = engine.journal.open.attempts
        ids = {a.job: a.attempt for a in table.values()}
        _estate(tmp_path, jil("y", "/tmp/dsl41-two"))
        token = reread(tmp_path / "estate", engine.update.live(), run_root / "plans").token
        fingerprint = UpdateRequest(token, True, 1, engine.baseline_id, "u1").fingerprint
        intent = {
            "event": "intent",
            "at": T0.isoformat(),
            "token": token,
            "live_rev": 1,
            "stop": [
                {"job": job, "run_number": 1, "attempt": ids[job], "cause": "update_stop"}
                for job in ("j", "w")
            ],
            "filter": ["j", "w"],
            "stop_running": True,
            "request_id": "u1",
            "fingerprint": fingerprint,
        }
        engine.journal.append(Record(T0, "config", None, "u1", intent))
        await engine.shutdown()
        engine.journal.close()  # the engine dies while the update stops

    asyncio.run(first())
    dispatches = {b["job"]: b for _, k, b in rows(run_root) if k == "dispatch"}
    watch = dispatches["w"]
    line = {"artifact_format_version": ARTIFACT_FORMAT_VERSION, "run_id": watch["attempt"]}
    polls = [
        {**line, "at": f"2026-07-01T08:0{n}:00.000000", "exists": True, "kind": "poll"}
        | {"qualifying": True, "size": 5, "stable_polls": n}
        for n in (1, 2)
    ]
    start = {**line, "at": "2026-07-01T08:00:00.000000", "kind": "start"}
    spool = Path(watch["spool"])
    spool.mkdir(parents=True, exist_ok=True)
    (spool / WATCH_LOG).write_bytes(b"".join(canonical_bytes(d) + b"\n" for d in [start, *polls]))
    _write(dispatches["j"], "status.json", outcome="signaled", signal=15)
    seen = len(rows(run_root))

    async def restart() -> None:
        engine = await resume_run(
            load_estate(tmp_path / "estate").catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=5)),
            adapters=dict(adapters),
            settings=GRACE,
        )
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)

    asyncio.run(restart())
    after = rows(run_root)[seen:]
    [applied] = [s for s, k, b in after if k == "config" and b.get("event") == "applied"]
    [outcome] = [s for s, k, b in after if k == "outcome" and b["job"] == "w"]
    [plan] = [s for s, k, b in after if k == "follow_up" and b.get("plan") and b["job"] == "w"]
    [feed] = [
        s
        for s, k, b in after
        if k == "input" and b.get("source") == "reconcile" and b["payload"]["job"] == "w"
    ]
    assert outcome < plan < feed < applied
    assert not [b for _, k, b in after if k == "dispatch" and b["job"] == "w"]
