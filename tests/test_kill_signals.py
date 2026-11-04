"""DL-462: committed kill effects, one-command overrides and real signal delivery."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41 import runner_procid
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope, RequestCollision, dispatch_plan
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import RuntimeProfile
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import read_journal, replay_inputs
from dsl41.runner_startup import start_run
from test_engine_stops import _finish, _jil, _start, _start_jobs, _until

T0 = datetime(2026, 1, 1)
BOX = (
    "insert_job: b\njob_type: b\n\n"
    + _jil({"a": "true"}, "box_name: b\njob_terminator: 1\nbox_terminator: 1\n")
    + _jil({"c": "true"}, "box_name: b\njob_terminator: 1\n")
)


def signal_command(directory: Path, name: str = "child", *, exits: tuple[int, ...] = ()):
    """A ready foreground process; its own log proves received signals."""
    log = directory / f"{name}.signals"
    ready = directory / f"{name}.ready"
    script = directory / f"{name}.py"
    script.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "def received(number, frame):\n"
        f"    with Path({str(log)!r}).open('a') as stream:\n"
        "        stream.write(str(number) + '\\n')\n"
        f"    if number in {exits!r}: raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, received)\n"
        "signal.signal(signal.SIGTERM, received)\n"
        f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.01)\n"
    )
    return f"exec {sys.executable} {script}", ready, log


def _spy_kills(monkeypatch, engine):
    names = {id(live.task): job for job, live in engine._live.items()}
    calls = []
    original = Engine._kill

    def kill(self, live, signals=None):
        calls.append((names[id(live.task)], signals))
        original(self, live, signals)

    monkeypatch.setattr(Engine, "_kill", kill)
    return calls


@pytest.mark.parametrize(
    "kind,source,expected",
    [
        ("KILLJOB", "control", {"a": (15, 9), "c": (15, 9)}),
        ("KILLJOB", "adapter", {"a": (15, 9), "c": (15, 9)}),
        ("KILLJOB", None, {"a": (15, 9), "c": (15, 9)}),
        ("deadline", None, {"a": (15, 9), "c": (15, 9)}),
        ("terminator", "control", {"a": None, "c": (15, 9)}),
        ("STATUS", "control", {"a": None, "c": (15, 9)}),
    ],
)
def test_kill_selection_follows_committed_effects(monkeypatch, kind, source, expected):
    async def scenario():
        text = BOX.replace("job_type: b\n", "job_type: b\nterm_run_time: 1\n")
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            profile=RuntimeProfile(kill_signals=(15, 9)),
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "b"}))
            await engine.run_until_quiescent(T0)
            calls = _spy_kills(monkeypatch, engine)
            if kind != "deadline":
                payload = {"job": "b"}
                verb = kind
                if kind == "terminator":
                    verb, payload = "STATUS", {"job": "a", "status": "FAILURE"}
                elif kind == "STATUS":
                    payload = {"job": "a", "status": "TERMINATED"}
                engine.inject(Event(at=T0, kind=verb, payload=payload), source=source)
            await engine.run_until_quiescent(
                T0 + timedelta(minutes=1) if kind == "deadline" else T0
            )
            assert dict(calls) == expected
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("rejected", [False, True])
def test_override_is_confined_to_applied_command_cascade(monkeypatch, rejected):
    async def scenario():
        engine = Engine(
            lower_source(BOX + _jil({"timer": "true"}, "term_run_time: 1\n")),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            for job in ("b", "timer"):
                engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
            await engine.run_until_quiescent(T0)
            calls = _spy_kills(monkeypatch, engine)
            at = T0 + timedelta(minutes=1)
            answer = engine.submit(
                Event(at=at, kind="KILLJOB", payload={"job": "b", "kill_signals": [9]}),
                Envelope(
                    request_id="kill",
                    expect={"job:b": -1 if rejected else engine.oracle.store.revision("job:b")},
                ),
            )
            await engine.run_until_quiescent(at)
            assert (await answer).decision == ("rejected" if rejected else "applied")
            assert dict(calls) == (
                {"timer": (2, 9)} if rejected else {"timer": (2, 9), "a": (9,), "c": (9,)}
            )
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_kill_override_retry(monkeypatch, tmp_path):
    async def scenario():
        catalog = lower_source(_jil({"j": "true"}))
        engine = start_run(
            catalog,
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            calls = _spy_kills(monkeypatch, engine)
            envelope = Envelope(
                request_id="kill", expect={"job:j": engine.oracle.store.revision("job:j")}
            )
            for _ in range(2):
                answer = engine.submit(
                    Event(at=T0, kind="KILLJOB", payload={"job": "j", "kill_signals": [9]}),
                    envelope,
                )
                await engine.run_until_quiescent(T0)
                assert (await answer).decision == "applied"
            collision = engine.submit(
                Event(at=T0, kind="KILLJOB", payload={"job": "j", "kill_signals": [2, 9]}),
                envelope,
            )
            await engine.run_until_quiescent(T0)
            with pytest.raises(RequestCollision):
                await collision
            assert calls == [("j", (9,))]
            replayed = Oracle(catalog)
            replay_inputs(replayed, read_journal(tmp_path / "run"))
            assert replayed.store.job["j"].status == "TERMINATED"
            assert calls == [("j", (9,))]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_queued_and_noop_kills_bind_no_process(monkeypatch):
    async def scenario():
        engine = Engine(
            lower_source(
                "insert_resource: R\nres_type: R\namount: 1\n\n"
                + _jil({"a": "true", "b": "true"}, "resources: (R, QUANTITY=1)\n")
            ),
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            for job in ("a", "b"):
                engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["b"].status == "QUE_WAIT"
            calls = _spy_kills(monkeypatch, engine)
            for _ in range(2):
                engine.inject(Event(at=T0, kind="KILLJOB", payload={"job": "b"}))
                await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["b"].status == "TERMINATED"
            assert engine.oracle.store.job["a"].status == "RUNNING"
            assert calls == []
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_kill_provenance_is_transient_and_first_emission_wins():
    oracle = Oracle(lower_source(_jil({"j": "true"}, "term_run_time: 1\n")))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    twin = oracle.fork()
    with twin.batch(T0 + timedelta(minutes=1)) as batch:
        batch.feed(
            Event(
                at=T0 + timedelta(minutes=1),
                kind="STATUS",
                payload={"job": "j", "status": "SUCCESS"},
            )
        )
        batch.feed(Event(at=T0 + timedelta(minutes=1), kind="KILLJOB", payload={"job": "j"}))
    assert not oracle._kill_emissions
    assert batch.kill_emissions == {0}
    assert batch.command_start == 1
    assert [ev.payload for ev in batch.emitted] == [
        {"job": "j", "status": "TERMINATED"},
        {"job": "j", "status": "SUCCESS"},
    ]
    origins = {}
    assert dispatch_plan(
        batch.emitted,
        runs={"j": 1},
        dispatched={"j": 1},
        live={"j": 1},
        dispatchable=frozenset({"j"}),
        origins=origins,
    ) == [("KILL", "j", 1)]
    assert origins == {("KILL", "j", 1): 0}


@pytest.mark.usefixtures("cli_env")
@pytest.mark.parametrize(
    "policy,override,first", [((2, 9), None, 2), ((15, 9), None, 15), ((2, 9), "9", 9)]
)
def test_cli_kill_signals_end_to_end(
    short_root, monkeypatch, policy, override, first, owned_processes
):
    command, ready, log = signal_command(short_root)
    sent = []
    for name in ("signal_ours", "killpg_quiet"):
        original = getattr(runner_procid, name)

        def observe(*args, _original=original, _name=name, **kwargs):
            sig = args[2] if _name == "signal_ours" else args[1]
            sent.append((int(sig), time.monotonic()))
            return _original(*args, **kwargs)

        monkeypatch.setattr(runner_procid, name, observe)

    async def scenario():
        root = short_root / "run"
        engine = _start(root, _jil({"j": command}), signals=policy)
        server = ControlServer(engine, root / "control.sock")
        await server.start()
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            _start_jobs(engine, "j")
            await _until(ready.exists)
            args = [
                sys.executable,
                "-m",
                "dsl41",
                "sendevent",
                "KILLJOB",
                "-J",
                "j",
                "--socket",
                str(server.path),
            ]
            if override is not None:
                args += ["-k", override]
            cli = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await cli.communicate()
            assert cli.returncode == 0, output.decode()
            spool = root / "runs/j.1/1/status.json"
            await _until(spool.exists)
            assert json.loads(spool.read_text())["signal"] == 9
            received = log.read_text().splitlines() if log.exists() else []
            assert received == ([] if first == 9 else [str(first)])
            if first != 9:
                beginning = min(at for sig, at in sent if sig == first)
                ending = min(at for sig, at in sent if sig == 9)
                assert ending - beginning >= 5
            assert engine.profile.kill_signals == policy
            assert engine.oracle.store.job["j"].status == "TERMINATED"
        finally:
            try:
                await server.close()
            finally:
                await _finish(engine, loop)

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["engine_stop", "drain", "parent_loss", "leftover", "status"])
def test_non_killjob_stops_keep_term(short_root, stop, owned_processes):
    command, ready, log = signal_command(short_root, exits=(15,))

    async def scenario():
        root = short_root / "run"
        engine = _start(root, _jil({"j": command}), signals=(9,))
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            _start_jobs(engine, "j")
            await _until(ready.exists)
            view = engine.attempt_view("j")
            facts = engine.attempt_facts(view["run_id"])
            if stop == "engine_stop":
                from test_runner_control import _control_call

                server = ControlServer(engine, root / "control.sock")
                await server.start()
                try:
                    answer = await _control_call(server.path, {"cmd": "engine_stop", "v": 4})
                    assert answer["ok"] and answer["engine"] in {"DRAINING", "STOPPED"}
                finally:
                    await server.close()
            elif stop == "drain":
                await engine.request_stop()
            elif stop == "parent_loss":
                facts.launch.close_lifeline()
            elif stop == "status":
                engine.inject(
                    Event(
                        at=engine.clock.now(),
                        kind="STATUS",
                        payload={"job": "j", "status": "TERMINATED"},
                    )
                )
            else:
                await _until(lambda: engine.attempt_view("j")["state"] == "RUNNING")
                spawn = json.loads((facts.spool / "spawn.json").read_text())
                await engine.stops.ladder(spawn["command_pgid"], spawn["command_start_time"], 0.5)
            await _until(log.exists)
            assert log.read_text().splitlines() == ["15"]
        finally:
            await _finish(engine, loop)

    asyncio.run(scenario())
