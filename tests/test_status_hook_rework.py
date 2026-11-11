"""Spawn, cleanup, config selection and death-recovery regressions (DL-446)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import cli_run, main_config
from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.reload_plan import RereadRefused, reread
from dsl41.runner_control import roundtrip
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import replay_store
from dsl41.runner_store import SqliteStore
from dsl41.status_hook import HookDelivery, HookSettings, StatusHooks, notify_once
from test_main_config import config_file, estate_file, live_of
from test_status_hook import JOB, T0, Capture, feed, make_engine, script

pytestmark = pytest.mark.usefixtures("cli_env")


def test_drill_hook_writes_refusal_evidence_outside_engine_root(tmp_path):
    deploy = Path(__file__).resolve().parents[1] / "examples/nightbank/deploy"
    prefix = str(tmp_path / "srv/dsl41")
    library = tmp_path / "drill-lib.sh"
    library.write_text((deploy / "drill-lib.sh").read_text().replace("/srv/dsl41", prefix))
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; printf "%s\\n" "$ROOT" "$HOOK_EVENTS"', "drill", str(library)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    root, events = map(Path, result.stdout.splitlines())
    assert root == tmp_path / "srv/dsl41/runs/nightbank"
    assert events == tmp_path / "srv/dsl41/nightbank/night/status-hooks.jsonl"
    assert not events.is_relative_to(root)
    root.mkdir(parents=True)
    (root / "sentinel").write_bytes(b"engine-owned")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    events.parent.mkdir(parents=True)
    steps = (deploy / "drill-steps.sh").read_text()
    body = re.search(r"sudo tee /etc/dsl41/status-hook[^\n]*<<'SH'\n(.*?)\nSH", steps, re.S)
    assert body is not None
    subprocess.run(
        ["sh", "-c", body[1].replace("/srv/dsl41", prefix)],
        input=b'{"event":"engine_refused","exit_code":2}\n',
        timeout=5,
        check=True,
    )
    assert [json.loads(line) for line in events.read_text().splitlines()] == [
        {"event": "engine_refused", "exit_code": 2}
    ]
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


async def until(predicate, seconds=15):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


def live_pid(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("finish", ["deadline", "shutdown", "engine_refused", "engine_down"])
def test_sync_spawn_stall_keeps_engine_and_heartbeat_live_and_reaps_late_child(
    tmp_path,
    monkeypatch,
    finish,
):
    hook = script(tmp_path, "import time\ntime.sleep(60)\n")
    create = subprocess.Popen
    init = HookDelivery.__init__
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    launches, deliveries = [], []
    heartbeats = []

    def spawning(args, *a, **kw):
        if args == [str(hook)]:
            launches.append(time.monotonic())
            entered.set()
            release.wait(8)
            process = create(args, *a, **kw)
            returned.set()
            return process
        return create(args, *a, **kw)

    def remember(self, *a, **kw):
        init(self, *a, **kw)
        deliveries.append(self)

    monkeypatch.setattr(subprocess, "Popen", spawning)
    monkeypatch.setattr(HookDelivery, "__init__", remember)

    async def run():
        async def heartbeat():
            while True:
                heartbeats.append(time.monotonic())
                await asyncio.sleep(0.02)

        beat = asyncio.create_task(heartbeat())
        engine, captured = make_engine()
        one = None
        started = time.monotonic()
        if finish in ("engine_refused", "engine_down"):
            one = asyncio.create_task(notify_once(str(hook), tmp_path, finish))
        else:
            delivery = HookDelivery(str(hook), tmp_path)
            delivery.enqueue("job_failure")
            delivery.enqueue("job_hold")
        try:
            await until(entered.is_set)
            await until(lambda: len(heartbeats) >= 5)
            assert not returned.is_set(), "heartbeat ran only after synchronous spawn returned"
            await asyncio.wait_for(feed(engine, "STATUS", job="A", status="FAILURE"), 5)
            assert engine.frontiers.applied_index == 1
            assert [(r.event, r.job) for r in captured.rows] == [("job_failure", "A")]
            delivery = deliveries[0]
            if finish == "shutdown":
                stop_at = time.monotonic()
                await asyncio.wait_for(delivery.close(), 5)
                assert time.monotonic() - stop_at < 5
            elif one is not None:
                await asyncio.wait_for(one, 12)
            else:
                await asyncio.wait_for(delivery.worker, 12)
            assert time.monotonic() - started < 10
            assert delivery.disabled and not returned.is_set()
            assert len(launches) == 1
            assert delivery._invocation is not None
            assert delivery._tasks, "late supervision lost its strong reference"
            delivery.enqueue("job_hold")
            assert len(launches) == 1
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            await engine.shutdown()
            for delivery in deliveries:
                await delivery.close()

    try:
        asyncio.run(run())
    finally:
        release.set()
        for delivery in deliveries:
            owner = delivery._invocation
            if owner is not None:
                assert owner.done.wait(15), "late synchronous spawn was never cleaned up"
                assert owner.clean and owner.process is not None
                assert owner.process.returncode is not None
                assert not live_pid(owner.process.pid)
    assert len(launches) == 1


@pytest.mark.parametrize("parent", ["exits", "hangs"])
@pytest.mark.parametrize("route", ["engine", "helper"])
def test_group_cleanup_kills_term_resistant_grandchild_and_holds_slot(
    tmp_path,
    monkeypatch,
    parent,
    route,
):
    pids = tmp_path / "children"
    hook = script(
        tmp_path,
        f"""import os, signal, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open({str(pids)!r}, 'a') as out:
        out.write(str(os.getpid()) + '\\n')
    while True:
        time.sleep(60)
else:
    while not os.path.exists({str(pids)!r}) or str(child) not in open({str(pids)!r}).read().splitlines():
        time.sleep(0.01)
    if {parent == "hangs"!r}:
        while True:
            time.sleep(60)
""",
    )
    create = subprocess.Popen
    overlap = []
    signals = []
    killpg = os.killpg

    def spawning(args, *a, **kw):
        if args == [str(hook)] and pids.exists():
            overlap.extend(pid for pid in map(int, pids.read_text().splitlines()) if live_pid(pid))
        return create(args, *a, **kw)

    def signalled(pgid, sig):
        signals.append((pgid, sig))
        return killpg(pgid, sig)

    monkeypatch.setattr(subprocess, "Popen", spawning)
    monkeypatch.setattr(os, "killpg", signalled)

    async def run():
        if route == "helper":
            await asyncio.wait_for(notify_once(str(hook), tmp_path, "engine_down"), 12)
        else:
            delivery = HookDelivery(str(hook), tmp_path)
            try:
                delivery.enqueue("job_failure")
                delivery.enqueue("job_hold")
                await asyncio.wait_for(delivery.queue.join(), 25)
                assert not delivery.disabled
            finally:
                await delivery.close()

    try:
        asyncio.run(run())
        children = [int(pid) for pid in pids.read_text().splitlines()]
        assert len(children) == (1 if route == "helper" else 2)
        assert all(not live_pid(pid) for pid in children)
        assert overlap == [], "next invocation began before the old group was cleaned"
        assert len([sig for _, sig in signals if sig == signal.SIGTERM]) >= len(children)
        assert len([sig for _, sig in signals if sig == signal.SIGKILL]) >= len(children)
    finally:
        if pids.exists():
            for pid in map(int, pids.read_text().splitlines()):
                if live_pid(pid):
                    os.kill(pid, signal.SIGKILL)
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("change", ["path", "omission", "threshold"])
def test_config_edit_between_selection_and_host_load_never_mixes_settings(
    tmp_path,
    monkeypatch,
    restart,
    change,
):
    old = 'status_hook = "/old-hook"\nstatus_hook_long_running_s = 3\n'
    new = {
        "path": 'status_hook = "/new-hook"\nstatus_hook_long_running_s = 3\n',
        "omission": "",
        "threshold": 'status_hook = "/old-hook"\nstatus_hook_long_running_s = 7\n',
    }[change]
    path = estate_file(tmp_path, engine=old)
    initial = main_config.prepare(path)
    if restart:
        root = tmp_path / "run"
        root.mkdir()
        (root / "engine.db").touch()
    load = main_config.load_host
    served, refused = [], []

    def changed(selected, raw=None):
        config_file(tmp_path, engine=new)
        return load(selected, raw)

    async def serve(*args, **kw):
        served.append(kw)
        return 0

    async def notification(hook, root, event, **kw):
        refused.append((hook, event))

    monkeypatch.setattr(main_config, "load_host", changed)
    monkeypatch.setattr(cli_run, "_serve_run", serve)
    monkeypatch.setattr(cli_run, "notify_once", notification)
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    if restart:
        assert result.exit_code == 0, result.output
        assert len(served) == 1 and refused == []
        assert served[0]["hook_settings"].model_dump() == {
            "status_hook": "/old-hook",
            "status_hook_long_running_s": 3,
        }
        assert served[0]["main_launch"] == {
            "run_root": str(tmp_path / "run"),
            "as_machine": [],
            "machine_policy": "strict",
            "access_map": None,
            "status_hook": "/old-hook",
            "status_hook_long_running_s": 3,
        }
    else:
        assert result.exit_code == 2 and "changed during preparation" in result.output
        assert served == [] and refused == [("/old-hook", "engine_refused")]
    with pytest.raises(RereadRefused, match="restart the engine to apply them before update"):
        reread(live_of(initial), tmp_path / "plans", candidate=main_config.prepare(path))


def test_old_exit_code_is_null_for_hold_alarm_long_running_and_code_less_failure(monkeypatch):
    async def run():
        engine = Engine(
            lower_source(
                JOB
                + "date_conditions: 1\ndays_of_week: all\nstart_times: 08:00\nmust_complete_times: +1\n"
            ),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        capture = Capture()
        engine.status_hooks = capture
        hooks = StatusHooks(
            engine, HookSettings(status_hook="/missing", status_hook_long_running_s=1)
        )
        sent = []
        monkeypatch.setattr(hooks.delivery, "enqueue", lambda event, **kw: sent.append((event, kw)))
        try:
            await feed(engine, "FORCE_STARTJOB", job="A")
            await feed(engine, "STATUS", 1, job="A", status="FAILURE", exit_code=9)
            await feed(engine, "STARTJOB", 2, job="A")
            assert engine.oracle.store.runtime("A").run_number == 2
            assert engine.oracle.store.runtime("A").exit_code == 9
            await engine.clock.wait_until(T0 + timedelta(seconds=3))
            hooks.check_long_running()
            await engine.run_until_quiescent(T0 + timedelta(seconds=63))
            await feed(engine, "STATUS", 64, job="A", status="FAILURE")
            await feed(engine, "ON_HOLD", 65, job="A")
            assert sent[0][0] == "job_long_running"
            assert sent[0][1].get("exit_code") is None
            assert [(r.event, r.run_number, r.exit_code) for r in capture.rows] == [
                ("job_failure", 1, 9),
                ("alarm", 2, None),
                ("job_failure", 2, None),
                ("job_hold", 2, None),
            ]
        finally:
            await hooks.close()
            await engine.shutdown()

    asyncio.run(run())


def test_real_engine_death_with_live_run_notifies_catch_up_once(short_root):
    out = short_root / "events"
    hook = script(
        short_root, f"import sys\nopen({str(out)!r}, 'ab').write(sys.stdin.buffer.read())\n"
    )
    path = estate_file(short_root, engine=f"status_hook = {json.dumps(str(hook))}\n")
    (short_root / "a.jil").write_text(
        "insert_job: A\njob_type: c\ncommand: sleep 60\nmachine: localhost\n"
    )
    (short_root / "non-idempotent.toml").write_text('[jobs]\nA = "synthetic interruption"\n')
    root = short_root / "run"
    processes, streams = [], []

    def boot(n):
        log = short_root / f"engine-{n}.log"
        stream = log.open("w")
        streams.append(stream)
        child = subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        processes.append(child)
        end = time.monotonic() + 30
        while "engine up; control socket:" not in log.read_text():
            assert child.poll() is None, log.read_text()
            assert time.monotonic() < end, log.read_text()
            time.sleep(0.02)
        return child

    def status():
        return roundtrip(root / "control.sock", {"v": 3, "cmd": "status"}, timeout=5)

    try:
        first = boot(1)
        view = status()
        answer = roundtrip(
            root / "control.sock",
            {
                "v": 3,
                "cmd": "sendevent",
                "verb": "FORCE_STARTJOB",
                "payload": {"job": "A"},
                "baseline_id": view["baseline_id"],
                "request_id": "death-start",
                "expect": {"job:A": view["jobs"]["A"]["state_rev"]},
            },
            timeout=5,
        )
        assert answer["ok"] is True
        end = time.monotonic() + 30
        while status()["jobs"]["A"].get("attempt", {}).get("state") != "RUNNING":
            assert time.monotonic() < end
            time.sleep(0.02)
        first.kill()
        first.wait(timeout=10)
        assert first.returncode == -signal.SIGKILL
        second = boot(2)
        current = status()["jobs"]["A"]
        assert (current["run_number"], current["status"], current["on_hold"]) == (
            1,
            "FAILURE",
            True,
        )
        end = time.monotonic() + 15
        while not out.exists() or len(out.read_text().splitlines()) < 2:
            assert time.monotonic() < end
            time.sleep(0.02)
        second.terminate()
        second.wait(timeout=15)
        assert second.returncode == 0
        before = out.read_bytes()
        events = [json.loads(line) for line in before.splitlines()]
        assert [(e["event"], e["job"], e["run_number"], e["exit_code"]) for e in events] == [
            ("job_hold", "A", 1, None),
            ("job_failure", "A", 1, None),
        ]
        with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
            replayed = replay_store(store, where="death restart")
            assert replayed.oracle.store.runtime("A").status == "FAILURE"
            assert replayed.oracle.status_observations is None
        assert out.read_bytes() == before
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        for stream in streams:
            stream.close()
