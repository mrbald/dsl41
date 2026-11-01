"""Real-domain admission and clock-step warnings (runner-design ss4, ss9)."""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import datetime, timedelta

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine, _Plan, feed_step
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope, Frontiers
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_journal import Journal, read_journal
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_store import MemoryStore

T0 = datetime(2026, 7, 1, 8)


class _Wall(RealClock):
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        if t == datetime.max and interrupt is not None:
            async with asyncio.timeout(5):
                await interrupt.wait()
            return
        assert t <= self.at, "admission must not wait for the wall to reach the frontier"


class _Stderr:
    def __init__(self, capture: pytest.CaptureFixture[str]) -> None:
        self.capture = capture
        self.text = ""

    def getvalue(self) -> str:
        self.text += self.capture.readouterr().err
        return self.text


@pytest.fixture
def stderr(capsys: pytest.CaptureFixture[str]) -> _Stderr:
    return _Stderr(capsys)


def _warnings(stderr: _Stderr) -> list[str]:
    return [line for line in stderr.getvalue().splitlines() if line.startswith("dsl41: WARNING:")]


def test_real_clock_step_admits_at_the_frontier_and_warns_once_per_step(
    stderr: _Stderr,
) -> None:
    async def scenario() -> None:
        frontier = T0 + timedelta(minutes=5)
        clock = _Wall(frontier)
        journal = Journal(MemoryStore())
        engine = Engine(lower_source(""), clock=clock, adapters={}, journal=journal)

        async def request(index: int) -> None:
            result = engine.submit(
                Event(
                    at=clock.now(), kind="SET_GLOBAL", payload={"name": "FLAG", "value": str(index)}
                ),
                Envelope(request_id=f"r{index}", expect={"global:FLAG": index - 1}),
            )
            await engine.run_until_quiescent(clock.now())
            assert (await result).decision == "applied"

        try:
            await request(1)
            clock.at = T0 + timedelta(minutes=3)
            await request(2)
            await request(3)
            assert engine.frontiers.at == frontier
            assert len(_warnings(stderr)) == 1
            assert _warnings(stderr)[0] == (
                "dsl41: WARNING: wall clock below admission frontier:"
                f" wall={clock.at.isoformat()} frontier={frontier.isoformat()} gap_s=120.0"
            )

            retry = engine.submit(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "FLAG", "value": "2"}),
                Envelope(request_id="r2", expect={"global:FLAG": 1}),
            )
            await engine.run_until_quiescent(clock.now())
            assert (await retry).index == 2
            assert engine.frontiers.committed_index == 3
            assert len(_warnings(stderr)) == 1

            clock.at = T0 + timedelta(minutes=4)
            await request(4)
            assert len(_warnings(stderr)) == 1
            clock.at -= timedelta(seconds=30)
            await request(5)
            assert len(_warnings(stderr)) == 2
            clock.at = frontier
            await request(6)
            clock.at -= timedelta(minutes=1)
            await request(7)
            assert len(_warnings(stderr)) == 3
            clock.at = frontier + timedelta(seconds=1)
            await request(8)
            assert len(_warnings(stderr)) == 3
            stamps = [r["at"] for r in read_journal(journal.store) if r["rec"] == "input"]
            assert stamps == [frontier.isoformat()] * 7 + [clock.at.isoformat()]
            assert engine.refused_inputs == []
        finally:
            await engine.shutdown()
            journal.close()

    asyncio.run(scenario())


def test_real_clock_step_reports_at_a_quiet_loop_read(stderr: _Stderr) -> None:
    async def scenario() -> None:
        clock = _Wall(T0 + timedelta(minutes=5))
        engine = Engine(lower_source(""), clock=clock, adapters={})
        engine.inject(Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "X", "value": "x"}))
        try:
            await engine.run_until_quiescent(clock.now())
            clock.at = T0 + timedelta(minutes=2)
            await engine.run_until_quiescent(clock.now())
            await engine.run_until_quiescent(clock.now())
            assert len(_warnings(stderr)) == 1
            assert engine.frontiers.committed_index == 1
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_real_clock_step_admits_a_live_plan_at_the_frontier_without_waiting(
    stderr: _Stderr,
) -> None:
    async def scenario() -> None:
        frontier = T0 + timedelta(minutes=5)
        clock = _Wall(frontier)
        engine = Engine(
            lower_source("insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 10\n"),
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j"}))
        try:
            await engine.run_until_quiescent(clock.now())
            clock.at = T0 + timedelta(minutes=2)
            engine._plans["a1"] = _Plan(
                "a1",
                "j",
                1,
                [feed_step({"job": "j", "run_number": 1, "status": "SUCCESS", "exit_code": 0})],
                seq=1,
            )
            engine.perform_plan("a1")
            assert engine._queue[0][0] == frontier
            await engine.run_until_quiescent(clock.now())
            assert engine.frontiers.at == frontier
            assert engine.oracle.store.job["j"].status == "SUCCESS"
            assert clock.now() == T0 + timedelta(minutes=2)
            assert engine.exit_code is None
            assert engine.refused_inputs == []
            assert len(_warnings(stderr)) == 1
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_real_clock_step_admits_a_fake_adapter_completion_below_the_frontier(
    stderr: _Stderr,
) -> None:
    async def scenario() -> None:
        frontier = T0 + timedelta(minutes=5)
        clock = _Wall(frontier)
        journal = Journal(MemoryStore())
        engine = Engine(
            lower_source("insert_job: j\njob_type: c\ncommand: x\n"),
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            journal=journal,
        )
        engine.inject(Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "X", "value": "x"}))
        try:
            await engine.run_until_quiescent(clock.now())
            clock.at = T0 + timedelta(minutes=2)
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(datetime.max)
            assert engine.oracle.store.job["j"].status == "SUCCESS"
            completions = [r for r in read_journal(journal.store) if r.get("source") == "adapter"]
            assert [(r["kind"], r["at"]) for r in completions] == [("STATUS", frontier.isoformat())]
            assert clock.now() == T0 + timedelta(minutes=2)
            assert engine.exit_code is None
            assert engine.refused_inputs == []
            assert len(_warnings(stderr)) == 1
        finally:
            await engine.shutdown()
            journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", [False, True])
def test_real_clock_step_keeps_queued_requests_and_stop_in_arrival_order(
    stderr: _Stderr,
    stop: bool,
) -> None:
    async def scenario() -> None:
        clock = _Wall(T0 + timedelta(minutes=5))
        engine = Engine(lower_source(""), clock=clock, adapters={})

        def request(name: str, at: datetime):
            return engine.submit(
                Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": name}),
                Envelope(request_id=name, expect={f"global:{name}": 0}),
            )

        try:
            seed = request("seed", T0 + timedelta(minutes=4))
            await engine.run_until_quiescent(clock.now())
            assert (await seed).index == 1
            a = request("A", clock.now())
            stopped = engine.request_stop() if stop else None
            clock.at = T0 + timedelta(minutes=2)
            b = request("B", clock.now())
            await engine.run_until_quiescent(datetime.max)
            assert (await a).index == 2
            result = await b
            assert result.index == 3
            assert result.decision == ("rejected" if stop else "applied")
            assert result.code == ("engine_draining" if stop else None)
            if stopped is not None:
                assert await stopped == "STOPPED"
            assert clock.now() == T0 + timedelta(minutes=2)
            assert engine.frontiers.at == T0 + timedelta(minutes=4)
            assert len(_warnings(stderr)) == 1
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_real_clock_step_with_broken_stderr_still_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        clock = _Wall(T0 + timedelta(minutes=5))
        engine = Engine(lower_source(""), clock=clock, adapters={})
        engine.inject(Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "X", "value": "x"}))
        try:
            await engine.run_until_quiescent(clock.now())
            clock.at = T0 + timedelta(minutes=2)
            broken = io.StringIO()
            broken.close()
            with monkeypatch.context() as patch:
                patch.setattr(sys, "stderr", broken)
                engine.inject(
                    Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "Y", "value": "y"})
                )
                await engine.run_until_quiescent(clock.now())
            assert engine.oracle.store.global_value("Y") == "y"
            assert engine.frontiers.at == T0 + timedelta(minutes=5)
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("virtual", [False, True])
def test_clock_step_observation_starts_below_a_restored_frontier(
    stderr: _Stderr, virtual: bool
) -> None:
    clock = VirtualClock(start=T0) if virtual else _Wall(T0)
    engine = Engine(lower_source(""), clock=clock, adapters={})
    engine.frontiers = Frontiers(at=T0 + timedelta(minutes=5))
    assert engine.now_admitted() == T0 + timedelta(minutes=5)
    assert engine.now_admitted() == T0 + timedelta(minutes=5)
    assert len(_warnings(stderr)) == (0 if virtual else 1)


def test_real_clock_step_keeps_a_ticking_scheduler_and_timers_live(
    stderr: _Stderr,
) -> None:
    async def scenario() -> None:
        clock = _Wall(T0)
        catalog = lower_source(
            "insert_job: tick\njob_type: c\ncommand: x\nterm_run_time: 2\n"
            "date_conditions: 1\ndays_of_week: all\nstart_mins: 0,1,2\n"
        )
        scheduler = Scheduler(catalog, start=T0)
        journal = Journal(MemoryStore())
        engine = Engine(
            catalog,
            clock=clock,
            scheduler=scheduler,
            journal=journal,
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            await engine.run_until_quiescent(clock.now())
            assert engine.oracle.store.job["tick"].run_number == 1
            clock.at += timedelta(minutes=1)
            await engine.run_until_quiescent(clock.now())
            assert engine.frontiers.at == T0 + timedelta(minutes=1)
            assert scheduler.next_occurrence() == T0 + timedelta(minutes=2)

            clock.at = T0 + timedelta(seconds=30)
            engine.inject(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "X", "value": "x"})
            )
            await engine.run_until_quiescent(clock.now())
            engine.inject(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "Y", "value": "y"})
            )
            await engine.run_until_quiescent(clock.now())
            assert len(_warnings(stderr)) == 1
            assert engine.frontiers.at == T0 + timedelta(minutes=1)
            assert engine.oracle.store.job["tick"].status == "RUNNING"
            assert scheduler.next_occurrence() == T0 + timedelta(minutes=2)

            clock.at = T0 + timedelta(minutes=2)
            await engine.run_until_quiescent(clock.now())
            row = engine.oracle.store.job["tick"]
            assert (row.status, row.run_number) == ("RUNNING", 2)
            assert engine.exit_code is None
            assert engine.refused_inputs == []
            ticks = [r for r in read_journal(journal.store) if r.get("source") == "scheduler"]
            assert [r["at"] for r in ticks] == [
                (T0 + timedelta(minutes=m)).isoformat() for m in (0, 1, 2)
            ]
            assert len(_warnings(stderr)) == 1
        finally:
            await engine.shutdown()
            journal.close()

    asyncio.run(scenario())


def test_real_clock_step_keeps_future_queue_deadlines(stderr: _Stderr) -> None:
    async def scenario() -> None:
        clock = _Wall(T0 + timedelta(minutes=5))
        engine = Engine(lower_source(""), clock=clock, adapters={})
        engine.inject(Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "X", "value": "x"}))
        try:
            await engine.run_until_quiescent(clock.now())
            due = T0 + timedelta(minutes=9)
            engine.inject(Event(at=due, kind="SET_GLOBAL", payload={"name": "A", "value": "a"}))
            clock.at = T0 + timedelta(minutes=2)
            engine.inject(Event(at=due, kind="SET_GLOBAL", payload={"name": "B", "value": "b"}))
            await engine.run_until_quiescent(clock.now())
            assert [item[0] for item in engine._queue] == [due, due]
            assert engine.frontiers.committed_index == 1
            assert len(_warnings(stderr)) == 1
            clock.at = due
            await engine.run_until_quiescent(clock.now())
            assert engine.oracle.store.global_value("A") == "a"
            assert engine.oracle.store.global_value("B") == "b"
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_real_clock_step_after_queue_pop_cannot_advance_past_the_frontier(
    stderr: _Stderr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = _Wall(T0 + timedelta(minutes=5))
        engine = Engine(lower_source(""), clock=clock, adapters={})
        frontier = T0 + timedelta(minutes=4)
        engine.inject(Event(at=frontier, kind="SET_GLOBAL", payload={"name": "X", "value": "x"}))
        try:
            await engine.run_until_quiescent(clock.now())
            engine.inject(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "Y", "value": "y"})
            )
            admit = engine._admit_and_apply

            async def step_then_admit(pending):
                clock.at = T0 + timedelta(minutes=2)
                return await admit(pending)

            monkeypatch.setattr(engine, "_admit_and_apply", step_then_admit)
            await engine.run_until_quiescent(datetime.max)
            assert engine.frontiers.at == frontier
            assert engine.oracle.store.global_value("Y") == "y"
            assert len(_warnings(stderr)) == 1
        finally:
            await engine.shutdown()

    asyncio.run(scenario())
