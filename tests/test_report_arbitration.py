"""A process report takes effect in the turn it arrives (DL-523): competing pairs, each in both
arrival orders, and the writer's steps, none of which suspends.

A report runs between writer steps; the scenarios make it arrive with a competing request or
timer queued and not yet taken, or after the writer took it. The processes are
`test_engine_stops_cookie`'s stand-ins on the virtual clock.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dispatch_fixtures import dispatch_record
from reload_fixtures import prepare_estate
from store_surgery import rows
from test_engine_stops_cookie import PGID, RUN_ID, T0, _Procs
from test_update_atomic import _dispatched, _Ladders, _queued
from test_update_settlement import (
    ANCHOR,
    SIGNALED,
    _close,
    _engine,
    _estate,
    _jil,
    _kinds,
    _plan,
)

from dsl41 import main_config as main
from dsl41 import runner, runner_procid
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import AdapterContext, FakeAdapter
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_stops import AttemptFacts
from dsl41.runner_update import UpdateRequest

EXIT_0 = {"version": 1, "outcome": "exited", "exit_code": 0}
EXIT_1 = {"version": 1, "outcome": "exited", "exit_code": 1}


def _journal(tmp_path: Path) -> list[tuple[str, Any, Any, Any]]:
    """The journal as (kind, event or outcome, job, attempt)."""
    return [
        (
            k,
            b.get("event") or b.get("outcome"),
            b.get("job") or b.get("payload", {}).get("job"),
            b.get("attempt"),
        )
        for _, k, b in rows(tmp_path / "run")
    ]


def _plans(tmp_path: Path) -> list[dict[str, Any]]:
    return [p for p in _kinds(tmp_path, "follow_up") if p["event"] == "plan"]


def _status(engine: Engine, job: str) -> str:
    return engine.oracle.store.job[job].status


def _teardown(engine: Engine, ladders: _Ladders | None = None) -> Any:
    async def down() -> None:
        if ladders is not None:
            ladders.open_all()
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    return down()


# ---------------------------------------------------------------- a KILLJOB against an exit


@pytest.mark.parametrize("first", ["killjob", "exit"])
def test_a_killjob_and_an_exit_take_effect_in_the_order_they_reach_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """DL-523: a KILLJOB is queued and the job's exit 0 arrives before the writer takes it: the
    attempt is recorded as a natural end, the KILLJOB ends the run TERMINATED, and `d`, which
    waits on `j`'s success, does not run. Exit 0 arrives first and a KILLJOB follows: the run is
    SUCCESS and `d` runs."""
    _Procs(monkeypatch)
    text = ANCHOR + (
        "insert_job: j\njob_type: c\ncommand: sleep 30\ncondition: s(anchor)\n\n"
        "insert_job: d\njob_type: c\ncommand: true\ncondition: s(j)\n\n"
    )
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, text)
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "j", RUN_ID, PGID)
            kill = Event(at=T0, kind="KILLJOB", payload={"job": "j"})
            if first == "killjob":
                engine.inject(kill)
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
            else:
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
                engine.inject(kill)
            seen.append(_queued(engine))
            await engine.run_until_quiescent(T0)
            seen.append((_status(engine, "j"), _status(engine, "d")))
        finally:
            await _teardown(engine)

    asyncio.run(scenario())
    [outcome] = [o for o in _kinds(tmp_path, "outcome") if o["attempt"] == RUN_ID]
    assert outcome["outcome"] == "natural"
    [plan] = [p for p in _plans(tmp_path) if p["attempt"] == RUN_ID]
    assert plan["plan"] == "complete"
    d_dispatches = [d for d in _kinds(tmp_path, "dispatch") if d["job"] == "d"]
    if first == "killjob":
        assert seen[0] == [("KILLJOB", "j"), ("STATUS", "j")]
        assert seen[1] == ("TERMINATED", "INACTIVE") and d_dispatches == []
    else:
        assert seen[0] == [("STATUS", "j"), ("KILLJOB", "j")]
        assert seen[1] == ("SUCCESS", "RUNNING") and len(d_dispatches) == 1


# ---------------------------------------------------------------- an admission against an exit


@pytest.mark.parametrize("first", ["admission", "exit"])
def test_an_update_admission_and_an_exit_of_a_job_it_selects_take_effect_in_arrival_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """DL-523, DL-522: with the update request queued and `j`'s exit 0 arriving before the
    writer takes it, the exit commits an ordinary outcome with its plan at once; the admission
    still selects `j` (its row is RUNNING until the plan's feed), finds its attempt ended and
    finished, and applies in the admission step with nothing kept for `j`. With the admission
    taken first, the exit arrives while `j` is stopping: it counts as the stop's, its outcome is
    kept for the application, and `j` reruns under the new revision."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "j", RUN_ID, PGID)
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            request = UpdateRequest(_plan(engine, tmp_path), 1, engine.baseline_id, "u1")
            answer = engine.request_update(request)
            if first == "exit":
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
                seen.append(_queued(engine))
                await engine.run_until_quiescent(T0)
            else:
                await engine.run_until_quiescent(T0)
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
                seen.append(_queued(engine))
                await ladders.end(engine, RUN_ID, PGID)
                await engine.run_until_quiescent(T0)
            decision = answer.result().decision
            assert isinstance(decision, Admitted)
            seen.append(decision.stop_set)
            seen.append((engine.config_rev, _status(engine, "j")))
        finally:
            await _teardown(engine, ladders)

    asyncio.run(scenario())
    order = [o for o in _journal(tmp_path) if o[3] in (RUN_ID, None) or o[0] == "dispatch"]
    intent = order.index(("config", "intent", None, None))
    applied = order.index(("config", "applied", None, None), intent)
    outcome = next(i for i, o in enumerate(order) if o[0] == "outcome" and o[3] == RUN_ID)
    [body] = [c for c in _kinds(tmp_path, "config") if c["event"] == "applied" and "token" in c]
    reruns = [i for i, o in enumerate(order) if o[0] == "dispatch" and i > intent]
    if first == "exit":
        assert seen[0] == [("update", None), ("STATUS", "j")]
        assert order[outcome][1] == "natural" and outcome < intent
        assert body["stopped"] == [["j", "none"]] and reruns == []
        feed = order.index(("input", None, "j", None), intent)
        assert applied < feed
        assert seen[1:] == [("j",), (2, "SUCCESS")]
    else:
        assert seen[0] == []  # the outcome is kept: nothing is queued until the stop ends
        assert order[outcome][1] == "stopped" and applied < outcome
        assert body["stopped"] == [["j", "rerun"]] and len(reruns) == 1 and reruns[0] > outcome
        assert seen[1:] == [("j",), (2, "RUNNING")]


# ---------------------------------------------------------------- a spawn failure against a timeout


def _spawning(engine: Engine, job: str, attempt: str) -> None:
    """An observed attempt of `job`'s run 1 with no `spawn.json` seen yet (`_dispatched`
    without its spawn record)."""
    env = {**engine.dispatches[job]["env"], "DSL41_COOKIE": runner_procid.cookie("inst", attempt)}
    assert engine.journal is not None
    engine.journal.append(
        dispatch_record(
            job,
            1,
            attempt=attempt,
            spool="/nonexistent",
            started_at=T0,
            rev=1,
            ordinal=1,
            env=env,
            first_dispatch_seq=engine.dispatches[job]["first_dispatch_seq"],
        )
    )
    engine.adopt_attempt(
        AttemptFacts(
            attempt, job, 1, 1, Path("/nonexistent"), {"DSL41_SCHEDULED_AT": T0.isoformat(), **env}
        )
    )
    engine._restored.discard(attempt)


@pytest.mark.parametrize("first", ["spawn_failed", "record_timeout"])
def test_a_spawn_failure_and_a_current_record_timeout_the_first_to_arrive_ends_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """DL-523: `j`'s attempt is stopping before any `spawn.json` and its spool holds nothing.
    Whichever report arrives first ends the attempt in its turn, with its outcome and plan: a
    spawn failure completes (its feed is queued), a current record timeout is unobservable and
    reruns at once. The other report then meets an ended attempt and changes nothing. The stop
    before any `spawn.json` is posted directly with cause `wrapper_lost`; no validated input is
    known to reach it (DL-491)."""
    _Procs(monkeypatch)
    timers: list[tuple[str, int]] = []
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        monkeypatch.setattr(engine.stops, "kill_wrapper", lambda _attempt: False)
        monkeypatch.setattr(
            engine.stops, "arm_timer", lambda key, gen, _delay: timers.append((key, gen))
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            _spawning(engine, "j", RUN_ID)
            engine.attempts.stop(RUN_ID, "wrapper_lost")
            [(key, generation)] = timers
            reports = {
                "spawn_failed": lambda: engine.attempts.spawn_failed(RUN_ID, "no such file"),
                "record_timeout": lambda: engine.attempts.record_timeout(key, generation),
            }
            second = "record_timeout" if first == "spawn_failed" else "spawn_failed"
            reports[first]()
            journal = len(rows(tmp_path / "run"))
            seen.append((engine.attempts.state(RUN_ID), _queued(engine)))
            reports[second]()
            seen.append(len(rows(tmp_path / "run")) == journal)
            await engine.run_until_quiescent(T0)
        finally:
            await _teardown(engine)

    asyncio.run(scenario())
    [outcome] = [o for o in _kinds(tmp_path, "outcome") if o["attempt"] == RUN_ID]
    [plan] = [p for p in _plans(tmp_path) if p["attempt"] == RUN_ID]
    second = [d for d in _kinds(tmp_path, "dispatch") if d["job"] == "j" and d["ordinal"] == 2]
    if first == "spawn_failed":
        assert seen == [("ENDED", [("STATUS", "j")]), True]
        assert (outcome["outcome"], outcome.get("cause")) == ("spawn_failed", None)
        assert plan["plan"] == "complete"
        assert plan["steps"][0]["payload"]["status"] == "FAILURE"
        assert second == []
    else:
        assert seen == [("ENDED", []), True]  # the rerun is dispatched in the report's turn
        assert (outcome["outcome"], outcome["cause"]) == ("unobservable", "wrapper_lost")
        assert plan["plan"] == "rerun"
        assert [d["attempt"] for d in second] == [plan["steps"][0]["attempt"]]


# ---------------------------------------------------------------- an exit against a watch


class _HeldClock(VirtualClock):
    """The virtual clock, plus completions the test releases: a task waiting on one counts as
    parked on the clock, as an inert adapter does."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.held: dict[str, asyncio.Future[None]] = {}

    def pending_sleepers(self) -> int:
        return super().pending_sleepers() + sum(not f.done() for f in self.held.values())

    async def hold(self, name: str) -> None:
        self.held[name] = asyncio.get_running_loop().create_future()
        await self.held[name]

    def release(self, name: str) -> None:
        self.held[name].set_result(None)


class _Watch:
    """A file watch that completes, exit 0, when the test releases it."""

    async def run(self, job_ir: Any, _run_number: int, ctx: AdapterContext) -> int:
        clock = ctx.clock
        assert isinstance(clock, _HeldClock)
        await clock.hold(job_ir.name)
        return 0


@pytest.mark.parametrize("first", ["exit", "watch"])
def test_a_command_exit_and_a_watch_completion_take_effect_in_the_order_they_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """DL-523: a command's exit and a file watch's completion are both reports. Each journals
    its outcome and plan in its own turn and queues its feed; the journal and the writer's queue
    follow their arrival order, and `d`, which waits on both, runs once either way."""
    _Procs(monkeypatch)
    text = ANCHOR + (
        "insert_job: c\njob_type: c\ncommand: sleep 30\ncondition: s(anchor)\n\n"
        "insert_job: w\njob_type: f\nwatch_file: /synthetic/never\ncondition: s(anchor)\n\n"
        "insert_job: d\njob_type: c\ncommand: true\ncondition: s(c) & s(w)\n\n"
    )
    seen: list[object] = []

    async def scenario() -> None:
        estate = prepare_estate(_estate(tmp_path, text))
        clock = _HeldClock(T0)
        engine = start_run(
            estate.catalog,
            tmp_path / "run",
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None), "FW": _Watch()},
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        try:
            for job in ("c", "w"):
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "c", RUN_ID, PGID)
            watch = engine._live["w"].task

            async def complete() -> None:
                clock.release("w")
                await watch

            if first == "exit":
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
                await complete()
            else:
                await complete()
                engine.attempts.status_recorded(RUN_ID, EXIT_0)
            seen.append(_queued(engine))
            await engine.run_until_quiescent(T0)
            seen.append((_status(engine, "c"), _status(engine, "w")))
        finally:
            await _teardown(engine)

    asyncio.run(scenario())
    jobs = ["c", "w"] if first == "exit" else ["w", "c"]
    assert seen == [[("STATUS", job) for job in jobs], ("SUCCESS", "SUCCESS")]
    attempts = {RUN_ID} | {d["attempt"] for d in _kinds(tmp_path, "dispatch") if d["job"] == "w"}
    outcomes = [o["job"] for o in _kinds(tmp_path, "outcome") if o["attempt"] in attempts]
    feeds = [
        b["payload"]["job"]
        for b in _kinds(tmp_path, "input")
        if b.get("kind") == "STATUS" and b["payload"]["job"] in ("c", "w")
    ]
    assert outcomes == jobs and feeds == jobs
    assert [d["job"] for d in _kinds(tmp_path, "dispatch")].count("d") == 1


# ---------------------------------------------------------------- an overdue deadline against a retry


def test_a_failing_exit_that_arrives_before_an_overdue_deadline_dispatches_its_retry_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-523, DL-485: `j`'s `term_run_time` deadline is overdue and not yet served when its
    exit 1, eligible for a retry, arrives. The report commits the outcome with a `rerun` plan
    and dispatches attempt 2 in its own turn; the deadline is served after and ends the run
    TERMINATED, stopping attempt 2. One replacement is dispatched. The state is built directly
    because the virtual clock serves no overdue timer while the horizon equals present time; a
    real clock running late reaches it (DL-491)."""
    _Procs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(
            tmp_path, _jil({"j": "sleep 30"}, {"j": "n_retrys: 1\nterm_run_time: 1\n"})
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "j", RUN_ID, PGID)
            due = T0 + timedelta(minutes=1)
            assert (due, "j", "term_run_time") in engine.oracle.pending_timers()
            await engine.clock.wait_until(due + timedelta(minutes=1))  # the deadline is overdue
            engine.attempts.status_recorded(RUN_ID, EXIT_1)
            seen.append((_status(engine, "j"), _queued(engine)))
            # a horizon past the instant already reached lets the held deadline fire
            await engine.run_until_quiescent(engine.clock.now() + timedelta(minutes=1))
            seen.append(_status(engine, "j"))
        finally:
            await _teardown(engine)

    asyncio.run(scenario())
    assert seen == [("RUNNING", []), "TERMINATED"]
    [plan] = [p for p in _plans(tmp_path) if p["attempt"] == RUN_ID]
    assert plan["plan"] == "rerun"
    [(step, retry)] = [(s["ordinal"], s["attempt"]) for s in plan["steps"]]
    assert step == 2
    replacements = [d for d in _kinds(tmp_path, "dispatch") if d["ordinal"] == 2]
    assert [(d["attempt"], d["after"]) for d in replacements] == [(retry, "exit 1")]
    order = [
        (k, b.get("attempt"), b.get("event"), "kind" in b) for _, k, b in rows(tmp_path / "run")
    ]
    exit_at = order.index(("outcome", RUN_ID, None, False))
    plan_at = order.index(("follow_up", RUN_ID, "plan", False))
    dispatch_at = order.index(("dispatch", retry, None, False))
    deadline_at = next(
        i for i, (k, _a, _e, has_kind) in enumerate(order) if k == "input" and not has_kind
    )
    stopped_at = order.index(("outcome", retry, None, False))
    assert exit_at < plan_at < dispatch_at < deadline_at < stopped_at


# ---------------------------------------------------------------- no suspension inside a step


class _Steps:
    """Wraps the writer's two step entries (`_admit_and_apply`, `_control`). Right before each
    outermost step a sentinel task is made ready; after the step it records whether the
    sentinel ran inside it, by step kind. An event per kind is set once one was taken."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.taken: list[tuple[str, bool]] = []
        self.events: dict[str, asyncio.Event] = {}
        self.depth = 0
        admit, control = Engine._admit_and_apply, Engine._control

        async def admit_step(engine: Engine, pending: Any) -> Any:
            return await self._step(self._input_kind(pending), admit(engine, pending))

        async def control_step(engine: Engine, pending: Any) -> Any:
            return await self._step(self._control_kind(pending), control(engine, pending))

        monkeypatch.setattr(Engine, "_admit_and_apply", admit_step)
        monkeypatch.setattr(Engine, "_control", control_step)

    @staticmethod
    def _input_kind(pending: Any) -> str:
        if pending.step is not None:
            return "feed"
        if pending.ev is None:
            return "timer"
        return {"scheduler": "tick", "control": "operator"}.get(pending.ev.source, "other")

    @staticmethod
    def _control_kind(pending: Any) -> str:
        if pending.restart is not None:
            return "catch-up"
        return {"update": "admission", "update_exit": "notice"}.get(pending.control, "other")

    def event(self, kind: str) -> asyncio.Event:
        return self.events.setdefault(kind, asyncio.Event())

    async def _step(self, kind: str, step: Any) -> Any:
        ran: list[bool] = []

        async def sentinel() -> None:
            ran.append(True)

        outer = self.depth == 0
        self.depth += 1
        asyncio.get_running_loop().create_task(sentinel())  # ready before the step runs
        try:
            return await step
        finally:
            self.depth -= 1
            self.taken.append((kind if outer else f"inside {kind}", bool(ran)))
            if outer:
                self.event(kind).set()


class _AheadClock:
    """The stock real clock read `ahead` later: a test moves it to make a deadline and a
    tick due (as a wall step would)."""

    virtual = False

    def __init__(self) -> None:
        self.real = RealClock()
        self.ahead = timedelta(0)

    def now(self) -> datetime:
        return self.real.now() + self.ahead

    def next_sleeper_due(self) -> datetime | None:
        return None

    def pending_sleepers(self) -> int:
        return 0

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        await self.real.wait_until(t - self.ahead, interrupt)

    async def sleep_until(self, t: datetime) -> None:
        await self.real.sleep_until(t - self.ahead)


STEP_KINDS = ("operator", "feed", "admission", "notice", "timer", "tick", "catch-up")


@pytest.mark.parametrize("domain", ["virtual", "real"])
def test_no_writer_step_lets_a_ready_task_run_before_it_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, domain: str
) -> None:
    """DL-523: the writer finishes its current step before another task runs; no admission step
    suspends, on the stock real clock or on the stock virtual clock. For each kind of step (an
    operator event, a plan's feed, an update admission, an update notice, an oracle timer, a
    tick's start, a restart's catch-up step) a task made ready right before the step has not run
    when the step returns, nor inside any step it nests."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    steps = _Steps(monkeypatch)
    timed = {"t": "term_run_time: 1\n", "s": "date_conditions: 1\ndays_of_week: all\n"}
    timed["s"] += 'start_times: "08:03"\n' if domain == "virtual" else 'start_times: "00:00"\n'
    text = _jil({job: "sleep 30" for job in ("j", "f", "t", "s")}, timed)
    text = text.replace("command: sleep 30\ncondition: s(anchor)\ndate", "command: sleep 30\ndate")
    run_root = tmp_path / "run"

    def clock_of() -> Any:
        return VirtualClock(start=T0) if domain == "virtual" else _AheadClock()

    def adapters() -> dict[str, Any]:
        return {"CMD": FakeAdapter(default=None, job_default={"f": (0.0, 0)})}

    async def taken(engine: Engine, kind: str, horizon: datetime | None) -> None:
        if domain == "virtual":
            assert horizon is not None
            await engine.run_until_quiescent(horizon)
        async with asyncio.timeout(20):
            await steps.event(kind).wait()

    async def scenario() -> None:
        estate = prepare_estate(_estate(tmp_path, text))
        clock = clock_of()
        engine = start_run(
            estate.catalog,
            run_root,
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
            clock=clock,
            adapters=adapters(),
            scheduler=Scheduler(estate.catalog, start=clock.now(), default_tz="UTC"),
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        loop = None
        if domain == "real":
            loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        now = clock.now()
        try:
            for job in ("j", "f", "t"):
                engine.inject(Event(at=now, kind="FORCE_STARTJOB", payload={"job": job}))
            await taken(engine, "operator", now)
            await taken(engine, "feed", now)
            _dispatched(engine, "j", RUN_ID, PGID)
            _estate(
                tmp_path,
                text.replace(
                    "command: sleep 30\ncondition: s(anchor)\n\ninsert_job: f",
                    "command: sleep 31\ncondition: s(anchor)\n\ninsert_job: f",
                ),
            )
            answer = engine.request_update(
                UpdateRequest(_plan(engine, tmp_path), 1, engine.baseline_id, "u1")
            )
            await taken(engine, "admission", now)
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            await ladders.end(engine, RUN_ID, PGID)
            await taken(engine, "notice", now)
            application = answer.result().applied
            assert application is not None and application.done()
            if isinstance(clock, _AheadClock):
                clock.ahead = timedelta(hours=25)
                engine.wake()
            later = None if domain == "real" else T0 + timedelta(minutes=5)
            await taken(engine, "timer", later)
            await taken(engine, "tick", later)
        finally:
            ladders.open_all()
            if loop is not None:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            await _teardown(engine)
        clock = clock_of()
        if isinstance(clock, _AheadClock):
            clock.ahead = timedelta(hours=25)
        applied = prepare_estate(tmp_path / "estate").catalog
        restarted = await resume_run(
            applied,
            run_root,
            clock=clock,
            adapters=adapters(),
            scheduler=Scheduler(applied, start=clock.now(), default_tz="UTC"),
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        loop = None
        if domain == "real":
            loop = asyncio.ensure_future(restarted.run_until_quiescent(datetime.max))
        try:
            assert restarted.resume_at is not None  # the catch-up's stamp
            await taken(restarted, "catch-up", restarted.resume_at)
        finally:
            if loop is not None:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            await _teardown(restarted)

    asyncio.run(scenario())
    kinds = {kind for kind, _ in steps.taken}
    assert set(STEP_KINDS) <= kinds, sorted(kinds)
    assert [step for step in steps.taken if step[1]] == []


def test_a_step_with_an_awaiting_clock_lets_the_ready_task_run_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control of the step check above: a virtual clock whose `wait_until`
    suspends, as a production clock must not (DL-523's OPEN), lets the sentinel run inside the
    operator step, so the check can fail."""
    wait_until = VirtualClock.wait_until

    async def awaiting(
        self: VirtualClock, t: datetime, interrupt: asyncio.Event | None = None
    ) -> None:
        await asyncio.sleep(0)
        await wait_until(self, t, interrupt)

    monkeypatch.setattr(VirtualClock, "wait_until", awaiting)
    steps = _Steps(monkeypatch)

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
        finally:
            await _teardown(engine)

    asyncio.run(scenario())
    assert steps.taken == [("operator", True)]


#: Every `await` in the engine's loop and its step entries (DL-523). Inside a step only the
#: step entries it composes and the virtual clock's `wait_until` are awaited; the others run
#: between steps. A new entry here is a new place a step could suspend: review it against
#: DL-523 before adding it.
STEP_AWAITS = {
    "run_until_quiescent": [
        ("self._settle()", None),  # between steps
        # the turn before an owed prune step, where nothing is due, like the idle wait (DL-543)
        ("asyncio.sleep(0)", None),
        ("self._admit_and_apply(pending)", None),  # an input step
        # the turn after the synchronous prune step, before the loop chooses again (DL-543)
        ("asyncio.sleep(0)", None),
        ("self._control(pending)", None),  # a control, update or catch-up step
        ("self.clock.wait_until(work.at)", "self.clock.virtual"),  # a tick, before its starts
        ("self._admit_and_apply(_Pending(at=work.at, request_id=None))", None),  # a timer step
        (  # the idle wait
            "self.clock.wait_until(work.at if work.at is not None else datetime.max,"
            " interrupt=self._activity)",
            None,
        ),
        (  # quiescence waits for a restart's ladders
            "asyncio.wait([*ladders, woke], return_when=asyncio.FIRST_COMPLETED)",
            None,
        ),
    ],
    "_settle": [("asyncio.sleep(0)", None)],  # virtual only, between steps
    "_control": [("self._update_step(pending)", None)],
    "_update_step": [
        ("self._admit_and_apply(_Pending(at=ev.at, request_id=None, ev=ev))", None),
        ("self._admit_and_apply(_Pending(at=self.now_admitted(), request_id=None))", None),
        ("self._admit_and_apply(_Pending(at=ev.at, request_id=None, ev=ev))", None),
    ],
    "_admit_and_apply": [("self.clock.wait_until(pending.at)", "self.clock.virtual")],
}


def test_the_awaits_on_the_admission_path_are_the_known_ones() -> None:
    """DL-523: an alarm for a new `await` on the admission path. Each await of the loop and of
    the step entries is listed, with `self.clock.virtual` where it sits in that branch: the
    virtual clock's `wait_until` in `_admit_and_apply` is the only one inside an input step."""
    tree = ast.parse(inspect.getsource(runner))
    [engine] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Engine"]
    found: dict[str, list[tuple[str, str | None]]] = {}
    for method in engine.body:
        if not isinstance(method, ast.AsyncFunctionDef) or method.name not in STEP_AWAITS:
            continue
        guards: dict[ast.AST, str | None] = {}

        def visit(node: ast.AST, guard: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                inner = guard
                if isinstance(node, ast.If) and child in node.body:
                    if ast.unparse(node.test) == "self.clock.virtual":
                        inner = "self.clock.virtual"
                guards[child] = inner
                visit(child, inner)

        visit(method, None)
        found[method.name] = [
            (ast.unparse(n.value), guards[n]) for n in ast.walk(method) if isinstance(n, ast.Await)
        ]
    assert found == STEP_AWAITS
