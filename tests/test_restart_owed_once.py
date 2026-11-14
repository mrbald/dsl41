"""Restart owes each decided start once, including held starts (DL-425, DL-426)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from store_surgery import rows
from test_engine_stops import _script, _until
from test_engine_stops_cookie import _kill, _live, _written, linux_only
from test_live_engine_branches import (
    _Client,
    _cli,
    _config,
    _job,
    _kill_engine,
    _ladder_done,
    _next_child,
    _plans,
    _records,
    _release_child,
    _stubborn,
)

from dsl41.ir import lower_source
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlClientError, ControlServer
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_startup import resume_run, start_run

pytestmark = pytest.mark.usefixtures("cli_env")

BEGINNING = datetime(2026, 7, 1)
LATER = BEGINNING + timedelta(minutes=2)


def _overdue_engine(directory: Path, fake: bool) -> None:
    """The clock stands in for latency between K's admission and dispatch (DL-491)."""
    root = directory / "run"

    class DispatchClock(RealClock):
        opened = False

        def now(self):
            admitted = self.opened and any(
                b["kind"] == "STARTJOB" and b["payload"].get("job") == "K"
                for b in _records(root, "input")
            )
            return LATER if admitted else BEGINNING

    async def run():
        restarting = (root / STORE_NAME).exists()
        clock = DispatchClock()
        clock.opened = restarting
        catalog = lower_source((directory / "jobs.jil").read_text())
        adapter = (
            FakeAdapter(default=(0, 0) if restarting else None)
            if fake
            else LocalCommandAdapter(grace_seconds=30)
        )
        options = {
            "clock": clock,
            "adapters": {"CMD": adapter},
            "settings": EngineSettings(grace_term_s=30, grace_kill_s=0.5),
            "hold_open": True,
        }
        engine = (
            await resume_run(catalog, root, **options)
            if restarting
            else start_run(catalog, root, **options)
        )
        clock.opened = True
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        try:
            # K's dispatch passes this horizon; no next admission sweeps its timer (DL-491).
            await engine.run_until_quiescent(datetime.max if restarting else BEGINNING)
            if not restarting:
                await asyncio.Event().wait()
        finally:
            await server.close()
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(run())


@asynccontextmanager
async def _clock_process(directory: Path, *, fake: bool = False):
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, str(Path.cwd() / 'tests')); "
        "from test_restart_owed_once import _overdue_engine; "
        "_overdue_engine(Path(sys.argv[1]), sys.argv[2] == 'fake')"
    )
    root = directory / "run"
    log_path = directory / "clock-engine.log"
    client = _Client(root)
    with log_path.open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(directory), "fake" if fake else "real"],
            stdout=log,
            stderr=log,
        )
        try:
            # A reply proves the new engine opened its store; a stale socket does not (DL-491).
            async with asyncio.timeout(30):
                while child.poll() is None:
                    try:
                        await client.ask("status")
                        break
                    except (OSError, ControlClientError):
                        await asyncio.sleep(0.01)
            assert child.poll() is None, log_path.read_text()
            yield client, child
        finally:
            if child.poll() is None:
                await _kill_engine(child)


def _seq(root, kind, **match):
    return [
        seq
        for seq, found, body in rows(root)
        if found == kind and all(body.get(key) == value for key, value in match.items())
    ]


async def _wait_status(client, **jobs):
    async with asyncio.timeout(30):
        while True:
            status = await client.ask("status")
            if status["engine"] == "RUNNING" and all(
                status["jobs"][job]["status"] == expected for job, expected in jobs.items()
            ):
                return status
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("stop_during_startup", [False, True], ids=["direct", "interrupted"])
def test_overdue_timer_queues_its_dependent_once_on_restart(short_root, stop_during_startup):
    """DL-425: a timer before the journal's last instant owes one start behind the barrier."""
    _config(short_root, _job("x", "condition: t(K)\n", "J") + _job("x", "term_run_time: 1\n", "K"))
    root = short_root / "run"

    async def scenario():
        async with _clock_process(short_root, fake=True) as (client, child):
            await client.event("STARTJOB", "K")
            await _until(lambda: len(_records(root, "dispatch")) == 1)
            [first] = _records(root, "dispatch")
            assert first["job"] == "K" and first["started_at"] == LATER.isoformat()
            [admitted] = [b for b in _records(root, "input") if b["kind"] == "STARTJOB"]
            assert datetime.fromisoformat(admitted["at"]) + timedelta(minutes=1) < LATER
            assert not _records(root, "dispatch_pending")
            await _kill_engine(child)
        if stop_during_startup:

            class RestartClock(RealClock):
                def now(self):
                    return LATER

            engine = await resume_run(
                lower_source((short_root / "jobs.jil").read_text()),
                root,
                clock=RestartClock(),
                adapters={"CMD": FakeAdapter()},
            )
            try:
                # Due catch-up precedes this stop; the barrier cannot launch J (DL-425).
                stopped = engine.request_stop()
                await engine.run_until_quiescent(LATER)
                assert await stopped == "STOPPED"
                assert _seq(root, "engine", event="shutdown_during_startup")
                assert len(_seq(root, "dispatch_pending", event="pending", job="J")) == 1
                assert not _seq(root, "dispatch", job="J")
            finally:
                await engine.shutdown()
                engine.journal.close()
        async with _clock_process(short_root, fake=True) as (client, child):
            await _until(lambda: len(_records(root, "engine")) >= 2 or child.poll() is not None)
            await _until(
                lambda: (
                    any(b.get("job") == "J" for b in _records(root, "outcome"))
                    or child.poll() is not None
                )
            )
            assert child.poll() is None, (short_root / "clock-engine.log").read_text()
            assert [(b["job"], b["run_number"]) for b in _records(root, "dispatch")] == [
                ("K", 1),
                ("J", 1),
            ]
            [pending] = _seq(root, "dispatch_pending", event="pending", job="J")
            [launch] = _seq(root, "dispatch", job="J", run_number=1, discharges=pending)
            assert pending < launch < _seq(root, "engine", event="started")[-1]
            assert len([b for b in _records(root, "outcome") if b["job"] == "J"]) == 1
            status = await _wait_status(client, J="SUCCESS")
            assert status["engine"] == "RUNNING" and status["jobs"]["J"]["status"] == "SUCCESS"
            assert (await client.ask("engine_stop"))["ok"]
            assert await asyncio.to_thread(child.wait, timeout=10) == 0, (
                short_root / "clock-engine.log"
            ).read_text()

    asyncio.run(scenario())


@linux_only
def test_recovery_releases_an_owed_start_after_its_superseded_retry_plan(short_root):
    """DL-426: completing J.1's stored plan releases held J.2 before recovery ends."""
    childfile = short_root / "child"
    body = 'if [ "$DSL41_RUN_NUMBER" != 1 ]; then exit 0; fi\n' + _stubborn(childfile)
    body += f"while [ ! -s {childfile} ]; do sleep 0.01; done\nexit 1\n"
    command = _script(short_root, "retry", body)
    _config(
        short_root,
        _job(command, "condition: t(K)\nn_retrys: 1\n", "J")
        + _job("sleep 600", "term_run_time: 1\n", "K"),
    )
    root = short_root / "run"

    async def scenario():
        async with _clock_process(short_root) as (client, child):
            await client.event("FORCE_STARTJOB", "J")
            leaked = await _written(childfile)
            await _until(lambda: bool(_plans(root)))
            [plan] = _plans(root)
            assert plan["plan"] == "rerun" and not _ladder_done(root)
            await client.event("CHANGE_STATUS", "J", status="INACTIVE")
            await client.event("STARTJOB", "K")
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            assert _records(root, "dispatch")[1]["started_at"] == LATER.isoformat()
            await _kill_engine(child)
        _kill(childfile)
        await _until(lambda: not _live(leaked))
        async with _clock_process(short_root) as (client, child):
            await _until(
                lambda: (
                    any(b["job"] == "J" and b["run_number"] == 2 for b in _records(root, "outcome"))
                    or child.poll() is not None
                )
            )
            assert "oracle_kill" not in [
                b.get("cause")
                for b in _records(root, "outcome")
                if b["job"] == "J" and b["run_number"] == 2
            ]
            assert child.poll() is None, (short_root / "clock-engine.log").read_text()
            assert (root / "runs/J.2/1/spawn.json").exists()
            assert [
                (b["job"], b["run_number"], b["ordinal"]) for b in _records(root, "dispatch")
            ] == [
                ("J", 1, 1),
                ("K", 1, 1),
                ("J", 2, 1),
            ]
            [pending] = _seq(root, "dispatch_pending", event="pending", job="J", run_number=2)
            [done] = _seq(root, "follow_up", event="done", attempt=plan["attempt"])
            [launch] = _seq(root, "dispatch", job="J", run_number=2, discharges=pending)
            # J.2 waits for this plan; release defers it before the barrier lifts (DL-426).
            assert pending < done < launch < _seq(root, "engine", event="started")[-1]
            assert not (root / "runs/J.1/2").exists()
            await _until(lambda: len(_records(root, "outcome")) == 3)
            status = await _wait_status(client, J="SUCCESS", K="TERMINATED")
            assert status["engine"] == "RUNNING"
            assert status["jobs"]["K"]["status"] == "TERMINATED"
            assert status["jobs"]["J"]["status"] == "SUCCESS"

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


@linux_only
def test_start_held_at_the_barrier_launches_once_after_recovery(short_root):
    """DL-426: queue filtering must retain an unqueued pending start until its group is gone."""
    childfile = short_root / "child"
    command = _script(short_root, "waiter", _stubborn(childfile) + "wait\n")
    path = _config(short_root, _job(command))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, child):
            await client.event("STARTJOB")
            first_child = await _written(childfile)
            await client.event("KILLJOB")
            await _until(lambda: bool(_records(root, "outcome")))
            assert _records(root, "outcome")[0]["ladder"] == "running"
            await client.event("STARTJOB")
            [pending] = _seq(root, "dispatch_pending", event="pending", job="j", run_number=2)
            await _kill_engine(child)
        async with _cli(path) as (client, _):
            assert (await client.ask("status"))["engine"] == "RUNNING"
            assert len(_records(root, "dispatch")) == 1 and not _ladder_done(root)
            assert len(_seq(root, "dispatch_pending", event="pending")) == 1
            assert not _seq(root, "dispatch_pending", event="discharged", pending=pending)
            _release_child(childfile)
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            [launch] = _seq(root, "dispatch", job="j", run_number=2, discharges=pending)
            [cleaned] = _seq(root, "follow_up", event="ladder_done")
            assert pending < _seq(root, "engine", event="started")[-1] < cleaned < launch
            second_child = await _next_child(childfile, first_child)
            _release_child(childfile)
            await _until(lambda: not _live(second_child))
            await _until(lambda: len(_records(root, "outcome")) == 2)
            assert [(b["run_number"], b["ordinal"]) for b in _records(root, "dispatch")] == [
                (1, 1),
                (2, 1),
            ]

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)
