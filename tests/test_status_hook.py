"""The lossy site boundary, with independent expected outcomes (DL-446)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import replay_store
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import SqliteStore
from dsl41.status_hook import HookDelivery, HookSettings, StatusHooks, _Invocation
from test_main_config import config_file, estate_file

pytestmark = pytest.mark.usefixtures("cli_env")
T0 = datetime(2026, 7, 1, 8)
JOB = "insert_job: A\njob_type: c\ncommand: true\n"


def script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "literal hook with spaces"
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return path


class Capture:
    def __init__(self):
        self.rows = []
        self.delivery = self

    def observe(self, rows):
        self.rows.extend(rows)

    async def close(self):
        pass


def make_engine(text=JOB, *, root=None):
    adapter = FakeAdapter(default=None)
    engine = start_run(
        lower_source(text),
        root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter},
    )
    capture = Capture()
    engine.status_hooks = capture
    return engine, capture


async def feed(engine, kind, seconds=0, *, source=None, request_id=None, **values):
    ev = Event(at=T0 + timedelta(seconds=seconds), kind=kind, payload=values)
    if source is None:
        engine.inject(ev, request_id=request_id)
    else:
        engine._enqueue(ev, source=source)
    await engine.run_until_quiescent(ev.at)


@pytest.mark.parametrize("value", [False, 1, [], "", "relative", "/bad\x00path"])
def test_invalid_path_is_exit_2_without_delivery(tmp_path, value):
    path = estate_file(tmp_path, engine=f"status_hook = {json.dumps(value)}\n")
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("value", [True, -1, 1.0, "1", [], None])
def test_threshold_is_strict_nonnegative_integer(value):
    with pytest.raises(ValueError):
        HookSettings(status_hook_long_running_s=value)


def test_defaults_disable_both_observers():
    assert HookSettings().model_dump() == {"status_hook": None, "status_hook_long_running_s": 0}


def test_refusal_after_valid_settings_sends_one_payload(tmp_path):
    out = tmp_path / "out"
    hook = script(
        tmp_path, f"import sys\nopen({str(out)!r}, 'ab').write(sys.stdin.buffer.read())\n"
    )
    path = estate_file(tmp_path, engine=f"status_hook = {json.dumps(str(hook))}\n")
    (tmp_path / "a.jil").unlink()
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["event"] == "engine_refused" and rows[0]["exit_code"] == 2
    assert rows[0]["run_root"] == str(tmp_path / "run")


def test_malformed_toml_cannot_obtain_hook_settings(tmp_path):
    out = tmp_path / "out"
    hook = script(tmp_path, f"open({str(out)!r}, 'w').write('called')\n")
    path = config_file(tmp_path, engine=f"status_hook = {json.dumps(str(hook))}\n")
    path.write_text(path.read_text() + "broken = [\n")
    assert CliRunner().invoke(app, ["run", "--config", str(path)]).exit_code == 2
    assert not out.exists()


def test_transitions_include_boxes_injections_and_hold_edges():
    async def run():
        engine, capture = make_engine("insert_job: B\njob_type: b\n" + JOB + "box_name: B\n")
        try:
            await feed(engine, "STARTJOB", job="B")
            await feed(engine, "STATUS", 1, job="A", status="FAILURE", exit_code=9)
            await feed(engine, "STATUS", 2, job="A", status="FAILURE", exit_code=8)
            await feed(engine, "ON_HOLD", 3, job="A")
            await feed(engine, "ON_HOLD", 4, job="A")
            await feed(engine, "OFF_HOLD", 5, job="A")
            await feed(engine, "ON_HOLD", 6, job="A")
            assert [
                (r.event, r.job, r.run_number, r.transition, r.exit_code) for r in capture.rows
            ] == [
                ("job_failure", "A", 1, "RUNNING->FAILURE", 9),
                ("job_failure", "B", 1, "RUNNING->FAILURE", None),
                ("job_hold", "A", 1, "ON_HOLD", None),
                ("job_hold", "A", 1, "ON_HOLD", None),
            ]
        finally:
            await engine.shutdown()

    asyncio.run(run())


def test_initial_hold_is_silent_and_auto_hold_notifies():
    async def run():
        initial, first = make_engine(JOB + "status: ON_HOLD\n")
        auto, second = make_engine(
            "insert_job: B\njob_type: b\n" + JOB + "box_name: B\nauto_hold: 1\n"
        )
        try:
            await feed(initial, "ON_HOLD", job="A")
            assert first.rows == []
            await feed(auto, "STARTJOB", job="B")
            assert [(r.event, r.job, r.transition) for r in second.rows] == [
                ("job_hold", "A", "ON_HOLD"),
            ]
        finally:
            await initial.shutdown()
            await auto.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "check,blocked,completed,expected",
    [
        ("must_start", True, False, [("alarm", "A", 0, "MUST_START_ALARM")]),
        ("must_start", False, False, []),
        ("must_complete", False, False, [("alarm", "A", 1, "MUST_COMPLETE_ALARM")]),
        ("must_complete", False, True, []),
    ],
)
def test_sem34_alarms_at_timer_and_observed_run(check, blocked, completed, expected):
    async def run():
        text = (
            JOB + f"date_conditions: 1\ndays_of_week: all\nstart_times: 08:00\n{check}_times: +1\n"
        )
        if blocked:
            text += "condition: s(G)\ninsert_job: G\njob_type: c\ncommand: true\n"
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        capture = Capture()
        engine.status_hooks = capture
        try:
            await feed(engine, "STARTJOB", job="A")
            if completed:
                await feed(engine, "STATUS", 30, job="A", status="SUCCESS")
            await engine.run_until_quiescent(T0 + timedelta(seconds=61))
            assert [(r.event, r.job, r.run_number, r.transition) for r in capture.rows] == expected
            if expected:
                assert capture.rows[0].at == T0 + timedelta(seconds=60)
        finally:
            await engine.shutdown()

    asyncio.run(run())


def test_rejected_completion_keeps_committed_time_half_alarm():
    async def run():
        text = (
            JOB
            + "date_conditions: 1\ndays_of_week: all\nstart_times: 08:00\nmust_complete_times: +1\n"
        )
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        capture = Capture()
        engine.status_hooks = capture
        try:
            await feed(engine, "STARTJOB", job="A")
            await feed(engine, "STATUS", 60, source="adapter", job="A", run_number=99, exit_code=1)
            assert engine.drops[-1][1] == "run_number mismatch"
            assert [(r.event, r.transition, r.run_number) for r in capture.rows] == [
                ("alarm", "MUST_COMPLETE_ALARM", 1),
            ]
            assert engine.oracle.store.runtime("A").status == "RUNNING"
        finally:
            await engine.shutdown()

    asyncio.run(run())


def test_failed_commit_dry_apply_and_dedup_never_deliver(tmp_path, monkeypatch):
    async def run():
        engine, capture = make_engine(root=tmp_path / "run")
        try:
            await feed(engine, "STATUS", job="A", status="FAILURE", request_id="one")
            await feed(engine, "STATUS", 10, job="A", status="FAILURE", request_id="one")
            assert len(capture.rows) == 1
            assert len(engine.deduped) == 1
            assert engine.oracle.fork().status_observations is None

            def fail(*args):
                raise OSError("commit failed")

            monkeypatch.setattr(engine.journal, "admission", fail)
            with pytest.raises(OSError, match="commit failed"):
                await feed(engine, "ON_HOLD", 11, job="A")
            assert [(r.event, r.transition) for r in capture.rows] == [
                ("job_failure", "INACTIVE->FAILURE")
            ]
            assert engine.oracle.status_observations is None
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(run())


def test_replay_and_restart_are_silent_but_new_recovery_admission_notifies(tmp_path):
    async def run():
        root = tmp_path / "run"
        engine, before = make_engine(root=root)
        await feed(engine, "STATUS", job="A", status="FAILURE")
        await feed(engine, "ON_HOLD", 1, job="A")
        await engine.shutdown()
        engine.journal.close()
        store = SqliteStore(root / "engine.db", read_only=True)
        try:
            replayed = replay_store(store, where="test")
            assert replayed.oracle.status_observations is None
            assert replayed.oracle.store.runtime("A").on_hold is True
        finally:
            store.close()
        restored = await resume_run(
            lower_source(JOB),
            root,
            clock=VirtualClock(start=T0 + timedelta(seconds=2)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        after = Capture()
        restored.status_hooks = after
        try:
            await restored.run_until_quiescent(T0 + timedelta(seconds=2))
            assert after.rows == []
            await feed(restored, "OFF_HOLD", 3, job="A")
            await feed(restored, "STATUS", 4, job="A", status="SUCCESS")
            await feed(restored, "STATUS", 4, job="A", status="RUNNING")
            restored._enqueue(
                Event(
                    at=T0 + timedelta(seconds=5),
                    kind="STATUS",
                    payload={"job": "A", "status": "FAILURE", "hold": True, "run_number": 1},
                ),
                source="reconcile",
            )
            await restored.run_until_quiescent(T0 + timedelta(seconds=5))
            assert [(r.event, r.transition) for r in after.rows] == [
                ("job_hold", "ON_HOLD"),
                ("job_failure", "RUNNING->FAILURE"),
            ]
            assert len(before.rows) == 2
        finally:
            await restored.shutdown()
            restored.journal.close()

    asyncio.run(run())


def test_long_running_checks_intervals_threshold_and_restart(monkeypatch):
    async def run():
        engine, _ = make_engine()
        settings = HookSettings(status_hook="/missing", status_hook_long_running_s=5)
        hooks = StatusHooks(engine, settings)
        events = []
        monkeypatch.setattr(
            hooks.delivery, "enqueue", lambda event, **kw: events.append((event, kw))
        )
        try:
            await feed(engine, "STATUS", job="A", status="RUNNING")
            await engine.clock.wait_until(T0 + timedelta(seconds=4))
            hooks.check_long_running()
            assert events == []
            await engine.clock.wait_until(T0 + timedelta(seconds=5))
            hooks.check_long_running()
            hooks.check_long_running()
            assert [(e, kw["threshold_s"], kw["run_number"]) for e, kw in events] == [
                ("job_long_running", 5, 0)
            ]
            await feed(engine, "STATUS", 6, job="A", status="SUCCESS")
            hooks.check_long_running()
            await feed(engine, "STATUS", 7, job="A", status="RUNNING")
            await engine.clock.wait_until(T0 + timedelta(seconds=12))
            hooks.check_long_running()
            assert len(events) == 2
            restarted = StatusHooks(engine, settings)
            monkeypatch.setattr(
                restarted.delivery, "enqueue", lambda event, **kw: events.append((event, kw))
            )
            try:
                restarted.check_long_running()
                assert len(events) == 3
            finally:
                await restarted.close()
            await feed(engine, "STATUS", 13, job="A", status="SUCCESS")
            hooks.check_long_running()
            assert len(events) == 3
        finally:
            await hooks.close()
            await engine.shutdown()

    asyncio.run(run())


def test_fifo_payload_and_process_isolation(tmp_path, monkeypatch):
    out = tmp_path / "out"
    fd_path = tmp_path / "inherited"
    fd = os.open(fd_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(fd, True)
    monkeypatch.setenv("STATUS_HOOK_PRIVATE", "secret")
    create = subprocess.Popen
    calls = []

    def spawned(*args, **kwargs):
        if args[0][0] != "/bin/ps":
            calls.append((args, kwargs))
        return create(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawned)
    hook = script(
        tmp_path,
        f"""import json, os, sys
data = sys.stdin.buffer.read()
try:
    leaked = os.fstat({fd}).st_ino == {os.fstat(fd).st_ino}
except OSError:
    leaked = False
mask = os.umask(0o077)
with open({str(out)!r}, 'a') as output:
    output.write(json.dumps([data.decode(), dict(os.environ), os.getcwd(), mask, leaked, sys.argv]) + '\\n')
""",
    )

    async def run():
        delivery = HookDelivery(str(hook), tmp_path)
        try:
            delivery.enqueue(
                "job_failure",
                at=T0,
                job="é",
                run_number=3,
                transition="RUNNING->FAILURE",
                exit_code=8,
            )
            delivery.enqueue("engine_refused", exit_code=2)
            await asyncio.wait_for(delivery.queue.join(), 10)
            assert delivery.counts == {}
        finally:
            await delivery.close()

    try:
        asyncio.run(run())
    finally:
        os.close(fd)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 2
    data, env, cwd, mask, leaked, argv = rows[0]
    row = json.loads(data)
    assert row == {
        "v": 1,
        "id": row["id"],
        "event": "job_failure",
        "at": "2026-07-01T08:00:00Z",
        "host": row["host"],
        "run_root": str(tmp_path),
        "job": "é",
        "run_number": 3,
        "transition": "RUNNING->FAILURE",
        "exit_code": 8,
        "unit": None,
        "threshold_s": None,
        "alarm_if_fail": True,
        "alarm_if_terminated": None,
    }
    assert UUID(row["id"]).version == 4 and row["host"]
    assert data.endswith("\n") and data.count("\n") == 1
    assert calls[0][0] == ([str(hook)],)
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C"}
    assert env["PATH"] == "/usr/bin:/bin" and env["LANG"] == "C"
    assert set(env) <= {"PATH", "LANG", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert (cwd, mask, leaked, argv) == ("/", 0o077, False, [str(hook)])
    assert json.loads(rows[1][0])["event"] == "engine_refused"


def test_overflow_oversize_drop_newest_and_logging_is_bounded(monkeypatch):
    async def run():
        delivery = HookDelivery("/missing", Path("/root"))
        messages = []
        monkeypatch.setattr(logging.Logger, "warning", lambda *args: messages.append(args))
        try:
            for n in range(66):
                delivery.enqueue("job_failure", run_number=n)
            delivery.enqueue("job_failure", job="x" * 16384)
            assert delivery.queue.qsize() == 64
            assert delivery.counts == {"overflow": 2, "oversize": 1}
            assert len(messages) == 1
            assert [
                json.loads(delivery.queue.get_nowait())["run_number"] for _ in range(64)
            ] == list(range(64))
        finally:
            await delivery.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["missing", "nonexecutable", "nonzero", "noisy", "hung", "nonreading"]
)
def test_delivery_failures_are_bounded_and_do_not_retry(tmp_path, mode):
    marker = tmp_path / "marker"
    body = f"import os, sys, time\nopen({str(marker)!r}, 'a').write('called\\n')\n"
    if mode == "nonzero":
        body += "sys.exit(3)\n"
    elif mode == "noisy":
        body += "sys.stdout.write('x' * 1000000)\nsys.stderr.write('x' * 1000000)\n"
    elif mode in ("hung", "nonreading"):
        if mode == "hung":
            body += "sys.stdin.buffer.read()\n"
        body += "time.sleep(60)\n"
    hook = script(tmp_path, body)
    if mode == "missing":
        hook.unlink()
    if mode == "nonexecutable":
        hook.chmod(0o600)

    async def run():
        delivery = HookDelivery(str(hook), tmp_path)
        start = time.monotonic()
        try:
            delivery.enqueue("job_failure", job="x" * 14000)
            await asyncio.wait_for(delivery.queue.join(), 12)
            assert time.monotonic() - start < 10
            assert delivery.counts == (
                {}
                if mode == "noisy"
                else {"nonzero exit": 1}
                if mode == "nonzero"
                else {"delivery failed": 1}
            )
            assert (
                marker.read_text() == "called\n"
                if marker.exists()
                else mode in ("missing", "nonexecutable")
            )
        finally:
            await delivery.close()

    asyncio.run(run())


def test_broken_logging_and_shutdown_drop_backlog(tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    hook = script(
        tmp_path, f"import time\nopen({str(marker)!r}, 'w').write('started')\ntime.sleep(60)\n"
    )

    def broken(*args, **kwargs):
        raise OSError("log broken")

    monkeypatch.setattr(logging.Logger, "warning", broken)

    async def run():
        delivery = HookDelivery(str(hook), tmp_path)
        delivery.diagnostic("test")
        delivery.enqueue("job_failure")
        delivery.enqueue("job_hold")
        deadline = time.monotonic() + 5
        while not (marker.exists() and marker.read_text() == "started"):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        start = time.monotonic()
        await delivery.close()
        assert time.monotonic() - start < 5
        assert delivery.queue.empty() and delivery.worker.done()
        assert marker.read_text() == "started"

    asyncio.run(run())


def test_unconfirmed_cleanup_disables_and_always_logs(monkeypatch):
    messages = []

    def unclean(invocation):
        invocation.done.set()

    monkeypatch.setattr(_Invocation, "run", unclean)
    monkeypatch.setattr(logging.Logger, "warning", lambda *args: messages.append(args))

    async def run():
        delivery = HookDelivery("/missing", Path("/root"))
        delivery.diagnostic("delivery failed")
        delivery.enqueue("job_failure")
        await asyncio.wait_for(delivery.queue.join(), 5)
        delivery.enqueue("job_hold")
        assert delivery.disabled and delivery.queue.empty()
        assert delivery.counts == {
            "delivery failed": 1,
            "cleanup unconfirmed; delivery disabled": 1,
        }
        assert len(messages) == 2 and messages[1][2] == "cleanup unconfirmed; delivery disabled"
        await delivery.close()

    asyncio.run(run())


@pytest.mark.parametrize("setting", ['status_hook = "/other"', "status_hook_long_running_s = 9"])
def test_reread_requires_restart_for_hook_host_settings(tmp_path, setting):
    from dsl41 import main_config as main
    from dsl41.reload_plan import RereadRefused, reread
    from test_main_config import live_of

    path = estate_file(tmp_path)
    original = main.prepare(path)
    config_file(tmp_path, engine=setting + "\n")
    with pytest.raises(RereadRefused, match="restart the engine to apply them before update"):
        reread(tmp_path, live_of(original), tmp_path / "plans", candidate=main.prepare(path))
    host = main.load_host(path)
    assert main.launch_settings(host, path)[setting.split()[0]] == (
        "/other" if setting.startswith("status_hook =") else 9
    )


def test_rehearsal_never_invokes_configured_hook(tmp_path):
    marker = tmp_path / "called"
    hook = script(tmp_path, f"open({str(marker)!r}, 'w').write('called')\n")
    path = estate_file(
        tmp_path, engine=f"status_hook = {json.dumps(str(hook))}\nstatus_hook_long_running_s = 1\n"
    )
    result = CliRunner().invoke(app, ["rehearse", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert not marker.exists()


def test_notification_exception_cannot_change_admission(monkeypatch):
    async def run():
        engine, capture = make_engine()

        def broken(rows):
            raise OSError("delivery broken")

        monkeypatch.setattr(capture, "observe", broken)
        try:
            await feed(engine, "STATUS", job="A", status="FAILURE")
            assert engine.oracle.store.runtime("A").status == "FAILURE"
            assert engine.frontiers.applied_index == 1
        finally:
            await engine.shutdown()

    asyncio.run(run())


def test_spawn_is_inside_deadline_and_unreaped_spawn_disables_delivery(monkeypatch):
    async def run():
        spawning = []
        release = asyncio.Event()
        original = HookDelivery._run

        async def blocked(self, invocation):
            spawning.append(asyncio.current_task())
            await release.wait()
            await original(self, invocation)

        monkeypatch.setattr(HookDelivery, "_run", blocked)
        delivery = HookDelivery("/hook", Path("/root"))
        start = time.monotonic()
        try:
            delivery.enqueue("job_failure")
            delivery.enqueue("job_hold")
            await asyncio.wait_for(delivery.worker, 12)
            assert time.monotonic() - start < 10
            assert len(spawning) == 1 and delivery.disabled
            assert spawning[0] in delivery._tasks
            assert delivery.counts == {
                "delivery failed": 1,
                "cleanup unconfirmed; delivery disabled": 1,
            }
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*spawning), 5)
            await delivery.close()

    asyncio.run(run())


def test_monitor_rechecks_status_and_does_not_move_admission(monkeypatch):
    async def run():
        engine, _ = make_engine()
        hooks = StatusHooks(
            engine, HookSettings(status_hook="/missing", status_hook_long_running_s=1)
        )
        sent = []
        monkeypatch.setattr(hooks.delivery, "enqueue", lambda *a, **k: sent.append((a, k)))
        try:
            await feed(engine, "STATUS", job="A", status="RUNNING")
            await engine.clock.wait_until(T0 + timedelta(seconds=1))
            before = engine.frontiers
            runtime = engine.oracle.store.runtime
            monkeypatch.setattr(
                engine.oracle.store,
                "runtime",
                lambda job: runtime(job).model_copy(update={"status": "SUCCESS"}),
            )
            hooks.check_long_running()
            assert sent == [] and engine.frontiers == before
        finally:
            await hooks.close()
            await engine.shutdown()

    asyncio.run(run())
