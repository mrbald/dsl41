"""DL-493: engine time through live admissions, stores and the control socket."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from reload_fixtures import prepare_estate, write_config
from test_engine_stops import _until
from test_kill_signals import signal_command
from test_restart import _open, _write
from test_runner_control import _control_call
from test_update_wiring import _crash

from dsl41 import main_config as main
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted
from dsl41.runner import INSTANT_BUDGET_FLOOR
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlServer
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_update import UpdateRequest
from dsl41.runner_store import Record
from dsl41.status_hook import HookSettings, StatusHooks
from test_status_hook import script

T0 = datetime(2026, 7, 1, 8)
JIL = "insert_job: j\njob_type: c\ncommand: true\nterm_run_time: 10\n"


class Wall(RealClock):
    """A settable real wall; future adapter sleeps wake only when it reaches them."""

    def __init__(self, at=T0):
        self.at = at
        self.readings = deque()
        self.sleepers = []

    def now(self):
        if self.readings:
            self.set(self.readings.popleft())
        return self.at

    def set(self, at):
        self.at = at
        for due, future in self.sleepers:
            if due <= at and not future.done():
                future.set_result(None)

    async def sleep_until(self, at):
        if at > self.at:
            future = asyncio.get_running_loop().create_future()
            self.sleepers.append((at, future))
            await future
        else:
            await asyncio.sleep(0)

    async def wait_until(self, at, interrupt=None):
        assert at == datetime.max or at <= self.at, f"now work waited for {at} at {self.at}"
        assert interrupt is not None
        await interrupt.wait()


async def close(engine):
    await engine.shutdown()
    engine.journal.close()


def records(engine):
    return [
        {"rec": record.kind, **record.payload, "at": record.at_utc.isoformat()}
        for _, record in engine.journal.store.read_from(0)
    ]


async def request(engine, name, horizon=None):
    answer = engine.submit(
        Event(at=engine.now_admitted(), kind="SET_GLOBAL", payload={"name": name, "value": name}),
        Envelope(request_id=name, expect={f"global:{name}": 0}),
    )
    await engine.run_until_quiescent(engine.clock.at if horizon is None else horizon)
    assert answer.done()
    return answer.result()


def estate_engine(directory, clock, *, schedule=False):
    directory.mkdir(parents=True)
    write_config(directory)
    text = JIL
    if schedule:
        text += "date_conditions: 1\ndays_of_week: all\nstart_mins: 0,1,2,3,4,5\n"
    (directory / "jobs.jil").write_text(text)
    estate = prepare_estate(directory)
    return start_run(
        estate.catalog,
        directory / "run",
        source_capture=main.encoded_parts(estate),
        launched=main.profile_of(estate),
        clock=clock,
        adapters={"CMD": FakeAdapter()},
        scheduler=Scheduler(estate.catalog, start=clock.at) if schedule else None,
    )


def update_request(engine, directory, rid="update"):
    path = directory / "jobs.jil"
    path.write_text(path.read_text().replace("command: true", "command: echo updated"))
    plan = reread(
        engine.update.live(), engine.run_root / "plans", candidate=prepare_estate(directory)
    )
    return UpdateRequest(plan.token, engine.config_rev, engine.baseline_id, rid)


@pytest.mark.parametrize("act", ["stop", "update", "update_exit"])
def test_capture_then_backward_read_does_not_wait(tmp_path, act):
    async def scenario():
        clock = Wall()
        directory = tmp_path / "estate"
        engine = estate_engine(directory, clock)
        try:
            change = update_request(engine, directory) if act == "update" else None
            clock.readings.extend([T0 + timedelta(minutes=5), T0 + timedelta(minutes=2)])
            if act == "stop":
                answer = engine.request_stop()
            elif act == "update":
                answer = engine.request_update(change)
            else:
                engine.post_update_exit(None)
                answer = None
            await engine.run_until_quiescent(T0 + timedelta(minutes=2))
            assert engine._queue == []
            assert answer is None or answer.done()
            if act == "update":
                assert isinstance(answer.result().decision, Admitted)
                assert engine.config_rev == 2
        finally:
            await close(engine)

    asyncio.run(scenario())


@pytest.mark.parametrize("virtual", [False, True])
@pytest.mark.parametrize("horizon", [None, datetime.max])
def test_status_hold_over_socket_and_reads_do_not_give_time(short_root, virtual, horizon, capsys):
    async def run(directory, query):
        clock = VirtualClock(start=T0) if virtual else Wall()
        engine = start_run(lower_source(""), directory, clock=clock, adapters={})
        server = ControlServer(engine, directory / "control.sock")
        await server.start()
        try:
            assert "clock_hold" not in await _control_call(server.path, {"cmd": "status"})
            if virtual:
                return
            clock.set(T0 + timedelta(minutes=5))
            if query:
                # A forward query must not pin time: the next wall reading is lower.
                for _ in range(2):
                    assert "clock_hold" not in await _control_call(server.path, {"cmd": "status"})
            clock.set(T0 + timedelta(minutes=3))
            await request(engine, "first")
            clock.set(T0 + timedelta(minutes=4))
            engine.take_snapshot()  # a present record above the admission frontier
            capsys.readouterr()
            clock.set(T0 + timedelta(minutes=3, seconds=30))
            before = engine._last_wall, engine._now_given
            if query:
                for _ in range(2):
                    answer = await _control_call(server.path, {"cmd": "status"})
                    assert answer["clock_hold"] == {
                        "wall": clock.at.isoformat(),
                        "until": (T0 + timedelta(minutes=4)).isoformat(),
                    }
                    assert (engine._last_wall, engine._now_given) == before
                    assert capsys.readouterr().err == ""
            await engine.run_until_quiescent(clock.at)
            assert capsys.readouterr().err.splitlines() == [
                "dsl41: WARNING: wall clock behind engine time:"
                " wall=2026-07-01T08:03:30 until=2026-07-01T08:04:00 gap_s=30.0"
            ]
            await engine.run_until_quiescent(clock.at)
            assert capsys.readouterr().err == ""
            await request(engine, "held", horizon)
            assert engine.frontiers.at == T0 + timedelta(minutes=4)
            clock.set(T0 + timedelta(minutes=5))
            assert "clock_hold" not in await _control_call(server.path, {"cmd": "status"})
            return [r["at"] for r in records(engine) if r["rec"] == "input"]
        finally:
            await server.close()
            await close(engine)

    async def scenario():
        assert await run(short_root / "read", True) == await run(short_root / "unread", False)

    asyncio.run(scenario())


def test_explicit_horizon_below_wall_keeps_later_due_work_queued(tmp_path):
    async def scenario():
        clock = Wall()
        engine = start_run(lower_source(""), tmp_path / "run", clock=clock, adapters={})
        try:
            due = T0 + timedelta(minutes=3)
            clock.set(T0 + timedelta(minutes=5))
            answer = engine.submit(
                Event(at=due, kind="SET_GLOBAL", payload={"name": "later", "value": "v"}),
                Envelope(request_id="later", expect={"global:later": 0}),
            )
            await engine.run_until_quiescent(T0 + timedelta(minutes=2))
            assert not answer.done()
            assert [at for at, _, _ in engine._queue] == [due]
            assert not any(r["rec"] == "input" for r in records(engine))
            await engine.run_until_quiescent(due)
            assert answer.done() and answer.result().decision == "applied"
            assert next(r["at"] for r in records(engine) if r["rec"] == "input") == due.isoformat()
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_later_admission_cap_is_not_a_stamp(short_root):
    async def scenario():
        clock = Wall()
        root = short_root / "run"
        engine = start_run(lower_source(""), root, clock=clock, adapters={})
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            answer = engine.submit(
                Event(
                    at=engine.now_present(), kind="SET_GLOBAL", payload={"name": "x", "value": "v"}
                ),
                Envelope(request_id="x", expect={"global:x": 0}),
            )
            clock.set(T0 + timedelta(minutes=5))
            await engine.run_until_quiescent(clock.at)
            assert answer.result().decision == "applied"
            assert [r["at"] for r in records(engine) if r["rec"] in {"input", "decision"}] == [
                T0.isoformat()
            ] * 2
            clock.set(T0 + timedelta(minutes=2))
            assert "clock_hold" not in await _control_call(server.path, {"cmd": "status"})
        finally:
            await server.close()
            await close(engine)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "verb,payload",
    [
        ("STARTJOB", {"job": "missing"}),
        ("CHANGE_STATUS", {"job": "j", "status": "unknown"}),
        ("SET_GLOBAL", {"name": "x"}),
        ("SET_GLOBAL", {"name": "x", "value": "\ud800"}),
        ("SET_GLOBAL", {"name": "x", "value": "v"}),  # missing envelope
    ],
)
def test_refused_control_verb_does_not_give_time(short_root, verb, payload):
    async def scenario():
        clock = Wall()
        root = short_root / "run"
        engine = start_run(lower_source(JIL), root, clock=clock, adapters={"CMD": FakeAdapter()})
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            seq = engine.journal.last_seq
            clock.set(T0 + timedelta(minutes=5))
            answer = await _control_call(
                server.path, {"cmd": "sendevent", "verb": verb, "payload": payload}
            )
            assert not answer["ok"] and answer["refused"]
            assert engine.journal.last_seq == seq
            clock.set(T0 + timedelta(minutes=2))
            assert "clock_hold" not in await _control_call(server.path, {"cmd": "status"})
        finally:
            await server.close()
            await close(engine)

    asyncio.run(scenario())


def test_restart_keeps_a_retained_stamp_covered_by_the_latest_snapshot(short_root):
    """Append unordered raw engine stamps through Store, as main would write across a step.

    Only snapshot markers change, so the same empty state can cover the later seq. No
    engine internals are patched; the latest retained stamp precedes the snapshot's tail.
    """

    async def scenario():
        root = short_root / "run"
        clock = Wall(T0 + timedelta(minutes=5))
        engine = start_run(lower_source(""), root, clock=Wall(), adapters={})
        store = engine.journal.store
        engine.take_snapshot()
        snapshot = store.latest_snapshot()
        ahead = T0 + timedelta(minutes=5)
        seq = store.append(
            Record(
                ahead,
                "engine",
                None,
                None,
                {"event": "snapshot", "at": ahead.isoformat(), "seq": snapshot.seq},
            )
        )
        store.put_snapshot(seq, snapshot.state_version, snapshot.data)
        behind = T0 + timedelta(minutes=2)
        store.append(
            Record(
                behind,
                "engine",
                None,
                None,
                {"event": "snapshot", "at": behind.isoformat(), "seq": seq},
            )
        )
        await close(engine)
        clock.set(T0 + timedelta(minutes=3))
        engine = await resume_run(lower_source(""), root, clock=clock, adapters={})
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            answer = await _control_call(server.path, {"cmd": "status"})
            assert answer["clock_hold"] == {
                "wall": clock.at.isoformat(),
                "until": ahead.isoformat(),
            }
            await request(engine, "after")
            assert engine.frontiers.at == ahead
        finally:
            await server.close()
            await close(engine)

    asyncio.run(scenario())


def test_snapshot_interval_keeps_the_single_raw_reading(tmp_path):
    class AdvancingWall(Wall):
        def now(self):
            at = self.at
            self.at += timedelta(seconds=1)
            return at

    async def scenario():
        clock = AdvancingWall()
        engine = start_run(lower_source(""), tmp_path / "run", clock=clock, adapters={})
        try:
            at = clock.at
            engine.take_snapshot()
            record = records(engine)[-1]
            assert record["event"] == "snapshot"
            assert record["at"] == at.isoformat()
            assert engine._snapshot_at == at
            assert clock.at == at + timedelta(seconds=1)
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_long_running_check_uses_held_engine_time_without_remembering(tmp_path):
    async def scenario():
        output = tmp_path / "notifications"
        hook = script(
            tmp_path,
            f"import sys\nwith open({str(output)!r}, 'ab') as stream: stream.write(sys.stdin.buffer.read())\n",
        )
        clock = Wall()
        engine = start_run(
            lower_source(JIL),
            tmp_path / "run",
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
        )
        hooks = engine.status_hooks = StatusHooks(
            engine, HookSettings(status_hook=str(hook), status_hook_long_running_s=120)
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            clock.set(T0 + timedelta(minutes=3))
            engine.take_snapshot()
            clock.set(T0 + timedelta(minutes=1))
            before = engine._last_wall, engine._now_given
            hooks.check_long_running()
            assert (engine._last_wall, engine._now_given) == before
            await asyncio.wait_for(hooks.delivery.queue.join(), 5)
            import json

            rows = [json.loads(line) for line in output.read_text().splitlines()]
            assert len(rows) == 1
            assert rows[0]["event"] == "job_long_running"
            assert rows[0]["at"] == "2026-07-01T08:03:00Z"
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_valid_zero_delta_deadline_is_lazy_with_horizon_below_wall(tmp_path):
    async def scenario():
        clock = Wall(T0 + timedelta(seconds=1))
        catalog = lower_source(
            JIL + 'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
            "must_complete_times: +0\n"
        )
        engine = start_run(
            catalog,
            tmp_path / "run",
            clock=clock,
            scheduler=Scheduler(catalog, start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            async with asyncio.timeout(3):
                await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["j"].status == "RUNNING"
            assert engine.oracle.next_timer_due() == T0
            assert [r["at"] for r in records(engine) if r["rec"] == "input"] == [T0.isoformat()]
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_requests_in_one_hold_reset_budget_on_raw_wall_progress(tmp_path):
    async def scenario():
        clock = Wall()
        engine = start_run(lower_source(""), tmp_path / "run", clock=clock, adapters={})
        try:
            clock.set(T0 + timedelta(days=1))
            engine.take_snapshot()
            # Consecutive loop reads advance below the hold. Admission still sees the held day.
            count = INSTANT_BUDGET_FLOOR + 1
            answers = []
            for i in range(count):
                answers.append(
                    engine.submit(
                        Event(
                            at=engine.now_admitted(),
                            kind="SET_GLOBAL",
                            payload={"name": "shared", "value": str(i)},
                        ),
                        Envelope(request_id=str(i), expect={"global:shared": i}),
                    )
                )
            clock.readings.extend(T0 + timedelta(microseconds=i) for i in range(2 * count + 10))
            await engine.run_until_quiescent(T0)
            assert all(a.done() and a.result().decision == "applied" for a in answers)
            assert engine._queue == []
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_recovered_end_ahead_of_wall_is_fed_at_now(tmp_path):
    root = tmp_path / "run"
    (dispatch,) = _open(root, JIL, ["j"], clock=Wall())
    ended = T0 + timedelta(minutes=5)
    _write(dispatch, "status.json", outcome="exited", exit_code=0, ended_at=ended.isoformat())

    async def scenario():
        clock = Wall(T0 + timedelta(minutes=1))
        engine = await resume_run(
            lower_source(JIL), root, clock=clock, adapters={"CMD": FakeAdapter()}
        )
        try:
            await engine.run_until_quiescent(clock.at)
            (completion,) = [r for r in records(engine) if r.get("source") == "reconcile"]
            assert completion["at"] == clock.at.isoformat()
            assert completion["payload"]["ended_at"] == ended.isoformat()
            assert engine.oracle.store.job["j"].status == "SUCCESS"
            assert engine.started.is_set()
            assert engine._queue == []
        finally:
            await close(engine)

    asyncio.run(scenario())


@pytest.mark.parametrize("ended_minutes", [3, 5, 10])
def test_recovered_feed_remembers_only_a_binding_cap(short_root, ended_minutes):
    """Existing restart helpers supply the wrapper end; only the wall readings stand in."""
    root = short_root / "run"
    text = JIL.replace("term_run_time: 10\n", "")
    (dispatch,) = _open(root, text, ["j"], clock=Wall())
    ended = T0 + timedelta(minutes=ended_minutes)
    _write(dispatch, "status.json", outcome="exited", exit_code=0, ended_at=ended.isoformat())

    async def scenario():
        given_before_step = []

        class RecoveryWall(Wall):
            def now(self):
                if len(self.readings) == 1:
                    given_before_step.append(engine._now_given)
                return super().now()

        clock = RecoveryWall(T0 + timedelta(minutes=1))
        engine = await resume_run(
            lower_source(text), root, clock=clock, adapters={"CMD": FakeAdapter()}
        )
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            capped = ended_minutes > 5
            after = T0 + timedelta(minutes=2) if capped else ended + timedelta(seconds=30)
            clock.readings.extend(
                [
                    T0 + timedelta(minutes=1),
                    T0 + timedelta(minutes=1),
                    T0 + timedelta(minutes=5),
                    after,
                ]
            )
            await engine.run_until_quiescent(datetime.max)
            assert clock.at == after
            remembered = T0 + timedelta(minutes=5 if capped else 1)
            assert given_before_step == [remembered]
            journal = records(engine)
            (outcome,) = [r for r in journal if r["rec"] == "outcome"]
            (feed,) = [r for r in journal if r.get("source") == "reconcile"]
            assert outcome["at"] == (T0 + timedelta(minutes=1)).isoformat()
            stamp = T0 + timedelta(minutes=5) if capped else ended
            assert feed["at"] == stamp.isoformat()
            assert feed["payload"]["ended_at"] == ended.isoformat()
            (started,) = [
                r
                for r in journal
                if r["rec"] == "engine"
                and r.get("event") == "started"
                and r["at"] != T0.isoformat()
            ]
            assert journal.index(feed) < journal.index(started)
            assert engine.oracle.store.job["j"].status == "SUCCESS"
            assert engine.started.is_set() and engine._queue == []
            answer = await _control_call(server.path, {"cmd": "status"})
            if capped:
                assert answer["clock_hold"] == {
                    "wall": after.isoformat(),
                    "until": stamp.isoformat(),
                }
            else:
                assert "clock_hold" not in answer
                assert started["at"] == after.isoformat()
        finally:
            await server.close()
            await close(engine)

    asyncio.run(scenario())


def test_resumed_update_does_not_wait_behind_a_recorded_end(short_root, owned_processes):
    async def scenario():
        clock = Wall()
        command, ready, _ = signal_command(short_root)
        directory = short_root / "estate"
        directory.mkdir()
        write_config(directory)
        path = directory / "jobs.jil"
        text = (
            f"insert_job: j\njob_type: c\ncommand: {command}\n\n"
            "insert_job: k\njob_type: c\ncommand: sleep 30\n"
        )
        path.write_text(text)
        estate = prepare_estate(directory)
        grace = EngineSettings(grace_term_s=0.1, grace_kill_s=0.1)
        engine = start_run(
            estate.catalog,
            directory / "run",
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
            settings=grace,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(grace_seconds=0.1)},
        )
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            for job in ("j", "k"):
                engine.inject(
                    Event(at=engine.now_admitted(), kind="STARTJOB", payload={"job": job})
                )
            await _until(ready.exists)
            await _until(lambda: engine.attempt_view("k")["state"] == "RUNNING")
            path.write_text(text.replace(command, "true"))
            plan = reread(
                engine.update.live(), engine.run_root / "plans", candidate=prepare_estate(directory)
            )
            answer = await engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "update")
            )
            assert isinstance(answer.decision, Admitted)
            assert engine.update.view()["stopping"] == ["j"]
            dispatch = dict(engine.dispatches["k"])
            root = engine.run_root
        finally:
            await _crash(engine, loop)
        # The existing restart helper supplies the wrapper record that could land during the outage.
        ended = T0 + timedelta(minutes=5)
        _write(dispatch, "status.json", outcome="exited", exit_code=0, ended_at=ended.isoformat())
        clock.set(T0 + timedelta(minutes=1))
        engine = await resume_run(
            estate.catalog,
            root,
            stored_sources=True,
            clock=clock,
            settings=grace,
            adapters={"CMD": FakeAdapter()},
        )
        try:
            await engine.run_until_quiescent(clock.at)
            assert engine.config_rev == 2
            assert engine.update.view() is None
            (feed,) = [
                r
                for r in records(engine)
                if r.get("source") == "reconcile" and r.get("payload", {}).get("job") == "k"
            ]
            assert feed["at"] == clock.at.isoformat()
            assert feed["payload"]["ended_at"] == ended.isoformat()
        finally:
            await close(engine)

    asyncio.run(scenario())


def test_backward_read_during_catch_up_keeps_observations_before_feeds(tmp_path):
    root = tmp_path / "run"
    text = JIL + JIL.replace("insert_job: j", "insert_job: k")
    for dispatch in _open(root, text, ["j", "k"], clock=Wall()):
        _write(
            dispatch,
            "status.json",
            outcome="exited",
            exit_code=0,
            ended_at=(T0 + timedelta(seconds=30)).isoformat(),
        )

    async def scenario():
        clock = Wall(T0 + timedelta(minutes=4))
        engine = await resume_run(
            lower_source(text), root, clock=clock, adapters={"CMD": FakeAdapter()}
        )
        try:
            clock.readings.extend([clock.at, clock.at, T0 + timedelta(minutes=1)])
            await engine.run_until_quiescent(clock.at)
            journal = list(records(engine))
            observations = [i for i, r in enumerate(journal) if r["rec"] == "outcome"]
            feeds = [i for i, r in enumerate(journal) if r.get("source") == "reconcile"]
            assert len(observations) == len(feeds) == 2
            assert max(observations) < min(feeds)
            assert engine.started.is_set()
        finally:
            await close(engine)

    asyncio.run(scenario())


@pytest.mark.parametrize("latest", ["outcome", "engine"])
def test_restart_seeds_now_from_every_record_kind_and_status_shows_hold(short_root, latest):
    async def scenario():
        root = short_root / "run"
        clock = Wall()
        engine = start_run(
            lower_source(JIL), root, clock=clock, adapters={"CMD": FakeAdapter(default=(1, 0))}
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        async with asyncio.timeout(3):
            while not clock.sleepers:
                await asyncio.sleep(0)
        clock.set(T0 + timedelta(minutes=5))
        if latest == "engine":
            engine.take_snapshot()
        else:
            async with asyncio.timeout(3):
                while not any(r["rec"] == "outcome" for r in records(engine)):
                    await asyncio.sleep(0)
        await close(engine)  # abandon the run without a shutdown input or record
        clock.set(T0 + timedelta(minutes=2))
        engine = await resume_run(
            lower_source(JIL), root, clock=clock, adapters={"CMD": FakeAdapter()}
        )
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            answer = await _control_call(server.path, {"cmd": "status"})
            assert answer["clock_hold"] == {
                "wall": clock.at.isoformat(),
                "until": (T0 + timedelta(minutes=5)).isoformat(),
            }
            await request(engine, "after_restart")
            new = list(records(engine))
            at = next(
                i
                for i, r in enumerate(new)
                if r["rec"] == latest and r["at"] == (T0 + timedelta(minutes=5)).isoformat()
            )
            assert all(r["at"] >= (T0 + timedelta(minutes=5)).isoformat() for r in new[at + 1 :])
        finally:
            await server.close()
            await close(engine)

    asyncio.run(scenario())


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@pytest.mark.usefixtures("oracle_recursion_limit")
@given(st.lists(st.tuples(st.integers(-90, 120), st.integers(-90, 120)), min_size=8, max_size=35))
def test_random_wall_walk_keeps_now_records_and_arrival_order(tmp_path, steps):
    async def scenario(directory):
        clock = Wall()
        engine = estate_engine(directory, clock, schedule=True)
        requests = []
        try:
            for index, (seconds, queued_seconds) in enumerate(steps):
                clock.set(clock.at + timedelta(seconds=seconds))
                answers = []
                for sibling in range(2):
                    if sibling:
                        assert engine._queue
                        clock.set(clock.at + timedelta(seconds=queued_seconds))
                    name = f"r{index}_{sibling}"
                    requests.append(name)
                    answers.append(
                        engine.submit(
                            Event(
                                at=engine.now_admitted(),
                                kind="SET_GLOBAL",
                                payload={"name": name, "value": name},
                            ),
                            Envelope(request_id=name, expect={f"global:{name}": 0}),
                        )
                    )
                if index % 3 == 0:
                    engine.inject(
                        Event(at=engine.now_admitted(), kind="FORCE_STARTJOB", payload={"job": "j"})
                    )
                if index == 3:
                    update = engine.request_update(update_request(engine, directory))
                await engine.run_until_quiescent(clock.at)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await engine.run_until_quiescent(clock.at)
                assert all(a.done() for a in answers)
                assert engine._queue == []
                if index == 3:
                    assert update.done() and isinstance(update.result().decision, Admitted)
            stop = engine.request_stop()
            await engine.run_until_quiescent(clock.at)
            assert stop.done() and engine._queue == []
            seen = []
            floor = datetime.min
            input_floor = datetime.min
            for record in records(engine):
                stamp = datetime.fromisoformat(record["at"])
                # Admission chooses a queued capture's stamp; it may predate a dispatch
                # recorded before admission (DL-493). Present records follow both.
                chosen = record["rec"] in {"input", "decision", "drop"}
                if not chosen:
                    assert stamp >= floor, record
                if record["rec"] == "input":
                    assert stamp >= input_floor
                    input_floor = stamp
                floor = max(floor, stamp)
                if record["rec"] == "input" and record.get("kind") == "SET_GLOBAL":
                    seen.append(record["request_id"])
            assert seen == requests
        finally:
            await close(engine)

    import tempfile

    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        asyncio.run(scenario(Path(directory) / "estate"))
