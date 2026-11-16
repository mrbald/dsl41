"""Engine ordering from persisted runs, socket inputs and host readings (DL-491)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime

import pytest
from test_engine_stops import GRACE, TIMEOUT_S, _finish, _script, _spawned, _start, _until
from test_engine_stops_cookie import _kill, _live, _written, linux_only
from test_runner_lifecycle import proc_state
from test_live_engine_branches import (
    _cli,
    _config,
    _job,
    _kill_engine,
    _ladder_done,
    _records,
)

from dsl41 import main_config as main
from dsl41 import runner_adapters, runner_procid
from dsl41.oracle_state import Event
from dsl41.runner_adapters import LocalCommandAdapter, read_watch_log
from dsl41.runner_clock import RealClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_startup import resume_run
from dsl41.runner_update import UpdateRequest

pytestmark = pytest.mark.usefixtures("cli_env")


def test_a_box_cascade_stops_the_old_run_once_and_launches_the_new_one(short_root):
    """A cascade's new start has already stopped the run its later kill names (DL-491)."""
    text = (
        "insert_job: B\njob_type: b\ncondition: t(K)\nbox_failure: e(K)=1 | f(J)\n"
        + _job("sleep 600", "machine: local\nbox_name: B\njob_terminator: 1\n", "J")
        + _job("true", "machine: local\nbox_name: B\njob_terminator: 1\ncondition: f(J)\n", "K")
    )
    path = _config(short_root, text)
    path.write_text(path.read_text().replace("[engine]\n", '[engine]\nas_machine = ["local"]\n'))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, _):
            await client.event("FORCE_STARTJOB", "J")
            await _spawned(root, "J")
            await client.event("CHANGE_STATUS", "J", status="INACTIVE")
            await client.event("CHANGE_STATUS", "K", status="RUNNING", exit_code=1)
            before = await client.ask("trace")
            await client.event("FORCE_STARTJOB", "B")
            trace = await client.ask("trace", since=before["last_seq"])
            assert [
                e["transition"].rsplit("->", 1)[-1] for e in trace["entries"] if e["job"] == "J"
            ] == ["STARTING", "RUNNING", "TERMINATED", "INACTIVE", "STARTING", "RUNNING"], trace
            await _spawned(root, "J", run=3)
            status = await client.ask("status")
            assert status["jobs"]["B"]["run_number"] == 2
            assert status["jobs"]["J"]["status"] == "RUNNING"
            assert status["jobs"]["K"]["status"] == "INACTIVE"
            dispatches = _records(root, "dispatch")
            assert [(b["job"], b["run_number"]) for b in dispatches] == [("J", 1), ("J", 3)]
            [ended] = _records(root, "outcome")
            assert ended["attempt"] == dispatches[0]["attempt"]
            assert (ended["outcome"], ended["cause"]) == ("stopped", "oracle_kill")

    asyncio.run(scenario())


def test_a_box_cascade_keeps_the_new_watch_live_after_the_old_runs_kill(short_root):
    """A stale kill cannot cancel the new watch launched earlier in its pass (DL-491)."""
    watched = short_root / "watched"
    text = (
        "insert_job: B\njob_type: b\ncondition: t(K)\nbox_failure: e(K)=1 | f(J)\n"
        "insert_job: J\njob_type: f\nbox_name: B\njob_terminator: 1\n"
        f"watch_file: {watched}\nwatch_interval: 1\n"
        + _job("true", "machine: local\nbox_name: B\njob_terminator: 1\ncondition: f(J)\n", "K")
    )
    path = _config(short_root, text)
    path.write_text(path.read_text().replace("[engine]\n", '[engine]\nas_machine = ["local"]\n'))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, _):
            await client.event("FORCE_STARTJOB", "J")
            await _until(lambda: read_watch_log(root / "runs/J.1") is not None)
            await client.event("CHANGE_STATUS", "J", status="INACTIVE")
            await client.event("CHANGE_STATUS", "K", status="RUNNING", exit_code=1)
            before = await client.ask("trace")
            await client.event("FORCE_STARTJOB", "B")
            trace = await client.ask("trace", since=before["last_seq"])
            assert [
                e["transition"].rsplit("->", 1)[-1] for e in trace["entries"] if e["job"] == "J"
            ] == ["STARTING", "RUNNING", "TERMINATED", "INACTIVE", "STARTING", "RUNNING"], trace
            row = (await client.ask("status"))["jobs"]["J"]
            assert row["run_number"] == 3 and row["status"] == "RUNNING"
            assert row["watching"]["file"] == str(watched)
            dispatches = _records(root, "dispatch")
            assert [(b["job"], b["run_number"]) for b in dispatches] == [("J", 1), ("J", 3)]
            await _until(lambda: bool(_records(root, "outcome")))
            [ended] = _records(root, "outcome")
            assert ended["attempt"] == dispatches[0]["attempt"]
            assert (ended["outcome"], ended["cause"]) == ("stopped", "killed")
            watched.touch()
            await _until(lambda: len(_records(root, "outcome")) == 2)
            [old, completed] = _records(root, "outcome")
            assert old == ended
            assert completed["attempt"] == dispatches[1]["attempt"]
            assert completed["outcome"] == "natural"
            assert (await client.ask("status"))["jobs"]["J"]["status"] == "SUCCESS"

    asyncio.run(scenario())


def _leader(directory):
    """The command itself ignores TERM; its release is independent of the engine."""
    marker = directory / "child"
    program = directory / "leader.py"
    program.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"marker = Path({str(marker)!r})\n"
        "marker.write_text(str(os.getpid()))\n"
        "while not marker.with_suffix('.exit').exists(): time.sleep(0.01)\n"
    )
    return f"exec {sys.executable} {program}"


@linux_only
def test_an_update_waits_past_its_first_gone_group_member_before_kill(short_root):
    """The cookie ladder passes its first, gone member before its surviving member."""
    childfile = short_root / "child"
    leader_ready = short_root / "leader-ready"
    first_file = short_root / "first"
    program = short_root / "child.py"
    program.write_text(
        "import os, signal, subprocess, sys\nfrom pathlib import Path\n"
        "def term(*_):\n"
        f"    Path({str(short_root)!r}, f'termed.{{os.getpid()}}').touch()\n"
        f"    if int(Path({str(first_file)!r}).read_text()) == os.getpid(): sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "if len(sys.argv) == 1:\n"
        "    subprocess.Popen([sys.executable, __file__, 'child'])\n"
        f"    ready = Path({str(leader_ready)!r})\n"
        "else:\n"
        f"    ready = Path({str(childfile)!r})\n"
        "ready.write_text(str(os.getpid()))\n"
        "while True: signal.pause()\n"
    )
    path = _config(short_root, _job(f"exec {sys.executable} {program}"))
    root = short_root / "run"
    grace = 5

    async def scenario():
        async with _cli(path) as (client, _):
            await client.event("STARTJOB")
            child = await _written(childfile)
            await _spawned(root, "j")
            spawn = json.loads((root / "runs/j.1/1/spawn.json").read_text())
            assert await _written(leader_ready) == spawn["command_pid"]
            # The two live group members have the same order in both /proc readings (DL-491).
            members = {spawn["command_pid"], child}
            first, survivor = [
                int(n) for n in os.listdir("/proc") if n.isdigit() and int(n) in members
            ]
            first_file.write_text(str(first))
            (short_root / "jobs.jil").write_text(_job("true"))
            admitted = await client.update()
            assert admitted["application"] is None
            await _until(lambda: all((short_root / f"termed.{pid}").exists() for pid in members))
            await _until(lambda: not _live(first))
            assert _live(survivor)
            assert not _records(root, "outcome") and not _ladder_done(root)
            await _until(lambda: bool(_records(root, "outcome")))
            assert not any(_live(pid) for pid in members)
            [ended] = _records(root, "outcome")
            assert ended["outcome"] == "stopped" and ended["cause"] == "update_stop"
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            replacement = _records(root, "dispatch")[1]
            assert replacement["ordinal"] == 2 and replacement["after"] == "update_stop"
            [intent] = [b for b in _records(root, "config") if b["event"] == "intent"]
            assert (
                datetime.fromisoformat(replacement["started_at"])
                - datetime.fromisoformat(intent["at"])
            ).total_seconds() >= grace

    (root / "engine.toml").write_text(f"grace_term_s = {grace}\ngrace_kill_s = 0.5\n")
    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


@pytest.mark.skipif(sys.platform != "darwin", reason="the non-Linux group reading uses ps")
def test_a_failed_ps_holds_a_real_stop_and_releases_its_next_start(short_root, monkeypatch):
    """A command can lose its wrapper during TERM; failed ps cannot prove its group gone."""
    program = short_root / "command.py"
    ready = short_root / "ready"
    program.write_text(
        "import os, signal, sys\nfrom pathlib import Path\n"
        "def stop(*_):\n"
        "    os.kill(os.getppid(), signal.SIGKILL)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: signal.pause()\n"
    )
    root = short_root / "run"
    actual_run = subprocess.run
    failures = []
    broken = True

    def host_run(args, *a, **kw):
        if broken and args == ["ps", "-A", "-o", "pgid=,stat="]:
            failures.append(args)
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="ps failed")
        return actual_run(args, *a, **kw)

    async def scenario():
        nonlocal broken
        engine = _start(root, _job(f"exec {sys.executable} {program}"), settings=GRACE)
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await _written(ready)
            await _spawned(root, "j")
            monkeypatch.setattr(runner_adapters.subprocess, "run", host_run)
            engine.inject(
                Event(
                    at=engine.clock.now(),
                    kind="KILLJOB",
                    payload={"job": "j", "kill_signals": [15, 9]},
                )
            )
            await _until(lambda: bool(engine.held_groups))
            assert failures and not (root / "runs/j.1/1/status.json").exists()
            assert not _records(root, "outcome")
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await _until(lambda: bool(_records(root, "dispatch_pending")))
            assert len(_records(root, "dispatch")) == 1
            broken = False
            await _spawned(root, "j", run=2)
            assert engine.fault is None and not loop.done()
            assert not engine.held_groups
            [ended] = _records(root, "outcome")
            assert ended["outcome"] == "unobservable" and ended["cause"] == "oracle_kill"
            pending = _records(root, "dispatch_pending")[0]
            assert pending["run_number"] == 2
            assert _records(root, "dispatch")[1]["discharges"] is not None
        finally:
            broken = False
            await _finish(engine, loop)

    asyncio.run(scenario())


def test_an_exact_update_retry_waits_while_the_restarted_engine_still_stops(short_root):
    """A real engine death leaves an update intent whose retry joins the resumed stop."""
    childfile = short_root / "child"
    path = _config(short_root, _job(_leader(short_root)))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, engine):
            await client.event("STARTJOB")
            await _written(childfile)
            await _spawned(root, "j")
            (short_root / "jobs.jil").write_text(_job("true"))
            admitted = await client.update()
            assert admitted["application"] is None
            assert (await client.ask("status"))["update"]["stopping"] == ["j"]
            await _kill_engine(engine)
        engine = await resume_run(
            main.prepare(path).catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=30)},
            settings=EngineSettings(grace_term_s=30, grace_kill_s=0.5),
            hold_open=True,
        )
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            async with asyncio.timeout(TIMEOUT_S):
                await engine.started.wait()
            [intent] = [b for b in _records(root, "config") if b["event"] == "intent"]
            async with asyncio.timeout(TIMEOUT_S):
                retry = await engine.request_update(
                    UpdateRequest(
                        intent["token"],
                        intent["live_rev"],
                        engine.baseline_id,
                        admitted["request_id"],
                    )
                )
            assert retry.decision is not None and list(retry.decision.stop_set) == admitted["stop"]
            assert retry.application is None and retry.applied is not None
            assert not retry.applied.done()
            childfile.with_suffix(".exit").touch()
            async with asyncio.timeout(TIMEOUT_S):
                applied = await retry.applied
            assert applied["rev"] == 2
            assert len([b for b in _records(root, "config") if b["event"] == "intent"]) == 1
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            assert _records(root, "dispatch")[1]["after"] == "update_stop"
        finally:
            childfile.with_suffix(".exit").touch()
            await _finish(engine, loop)

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


def test_a_command_the_units_term_ended_first_is_run_again_not_taken_as_success(short_root):
    """DL-503: systemd sends TERM to the whole unit when the engine dies. A command that ends
    with exit 0 on that TERM, before its wrapper acts, is recorded `parent lost` with the exit
    inside `observed`. The restarted engine reads `stopped{lifeline}` and runs the attempt again."""
    childfile = short_root / "child"
    release = childfile.with_suffix(".exit")  # the pilot's release; the CLI harness also touches it
    body = (
        "trap 'exit 0' TERM\n"
        f"echo $$ >> {childfile}\n"
        f"while [ ! -e {release} ]; do sleep 0.05 & wait $!; done\n"
        "exit 0\n"
    )
    path = _config(short_root, _job(_script(short_root, "term-exits-0", body)))
    root = short_root / "run"
    spawn_path = root / "runs/j.1/1/spawn.json"
    status_path = root / "runs/j.1/1/status.json"

    async def scenario():
        wrapper = None
        try:
            async with _cli(path) as (client, engine):
                await client.event("STARTJOB")
                await _written(childfile)
                await _spawned(root, "j")
                spawn = json.loads(spawn_path.read_text())
                wrapper = spawn["wrapper_pid"]
                os.kill(wrapper, signal.SIGSTOP)
                await _until(lambda: proc_state(wrapper) == "T")
                await _kill_engine(engine)
                os.killpg(spawn["command_pgid"], signal.SIGTERM)  # the unit's TERM, first
                await _until(lambda: runner_procid.proc_is_zombie(spawn["command_pid"]))
                os.kill(wrapper, signal.SIGCONT)
                await _until(status_path.exists)
            status = json.loads(status_path.read_text())
            assert status["outcome"] == "terminated" and status["cause"] == "parent lost"
            assert status["observed"] == {"outcome": "exited", "exit_code": 0}
            async with _cli(path) as (client, _):
                await _until(lambda: len(_records(root, "dispatch")) == 2)
                assert _records(root, "dispatch")[1]["ordinal"] == 2
                [first] = _records(root, "outcome")
                assert (first["outcome"], first["cause"]) == ("stopped", "lifeline")
                await _spawned(root, "j", n=2)
                assert (await client.ask("status"))["jobs"]["j"]["status"] == "RUNNING"
                release.touch()
                await _until(lambda: len(_records(root, "outcome")) == 2)
                assert (await client.ask("status"))["jobs"]["j"]["status"] == "SUCCESS"
                assert len(_records(root, "dispatch")) == 2
        finally:
            if wrapper is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(wrapper, signal.SIGCONT)

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)
