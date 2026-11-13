"""FEAT-37: persisted engine branches reached through live admissions (DL-491)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import sys
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from pathlib import Path

import pytest
from store_surgery import rows
from test_engine_stops import _script, _until
from test_engine_stops_cookie import _kill, _live, _written, linux_only
from test_main_config import config_file
from test_restart import T0, _close, _open

from dsl41 import main_config as main
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import roundtrip
from dsl41.runner_journal import decode_snapshot, open_reader
from dsl41.runner_startup import resume_run, start_run

pytestmark = pytest.mark.usefixtures("cli_env")


def _records(root, kind):
    return [body for _, found, body in rows(root) if found == kind]


def _plans(root, event="plan"):
    return [b for b in _records(root, "follow_up") if b["event"] == event]


def _job(command, extra="", name="j"):
    return f"insert_job: {name}\njob_type: c\ncommand: {command}\n{extra}\n"


def _config(directory, text, *, semantics=""):
    (directory / "jobs.jil").write_text(text)
    path = config_file(directory, engine='machine_policy = "local-eligible"\n', estate=semantics)
    root = directory / "run"
    root.mkdir(exist_ok=True)
    (root / "engine.toml").write_text("grace_term_s = 30\ngrace_kill_s = 0.5\n")
    return path


def _stubborn(childfile):
    """A TERM-ignoring child exits on a release file (DL-491)."""
    program = childfile.with_suffix(".py")
    program.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"marker = Path({str(childfile)!r})\n"
        "release = marker.with_name(marker.name + f'.{os.getpid()}.exit')\n"
        "marker.write_text(str(os.getpid()))\n"
        "while not (release.exists() or marker.with_suffix('.exit').exists()): time.sleep(0.01)\n"
    )
    return f"{sys.executable} {program} &\n"


def _release_child(childfile):
    if childfile.exists() and (pid := childfile.read_text().strip()):
        childfile.with_name(childfile.name + f".{pid}.exit").touch()


async def _next_child(childfile, previous):
    await _until(lambda: (pid := childfile.read_text().strip()) and int(pid) != previous)
    return await _written(childfile)


class _Client:
    def __init__(self, root):
        self.root = root
        self.socket = root / "control.sock"
        self.serial = 0

    async def ask(self, cmd, **fields):
        return await asyncio.to_thread(
            roundtrip, self.socket, {"v": 4, "cmd": cmd, **fields}, timeout=10
        )

    async def event(self, verb, job="j", *, rid=None, **payload):
        status = await self.ask("status")
        self.serial += 1
        answer = await self.ask(
            "sendevent",
            verb=verb,
            payload={"job": job, **payload},
            request_id=rid or f"event-{self.serial}",
            baseline_id=status["baseline_id"],
            expect={f"job:{job}": status["jobs"][job]["state_rev"]},
        )
        assert answer["ok"], answer
        return answer

    async def update(self, *, rid="update", wait=False):
        status = await self.ask("status")
        answer = await self.ask(
            "update", baseline_id=status["baseline_id"], request_id=rid, wait=wait
        )
        assert answer["ok"], answer
        return answer


@asynccontextmanager
async def _cli(path):
    """A real CLI engine; tests kill its process at a journaled crash boundary."""
    root = Path(main.load_host(path).run_root)
    log_path = root.parent / "live-engine.log"
    with log_path.open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=log,
            stderr=log,
        )
        client = _Client(root)
        try:
            await _until(
                lambda: (
                    "engine up; control socket:" in log_path.read_text() or child.poll() is not None
                )
            )
            assert child.poll() is None, log_path.read_text()
            yield client, child
        finally:
            if child.poll() is None:
                with contextlib.suppress(OSError):
                    await client.ask("engine_stop")
                (root.parent / "child.exit").touch()
                try:
                    await asyncio.to_thread(child.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    await asyncio.to_thread(child.wait, timeout=5)
                    pytest.fail(log_path.read_text())
            assert child.returncode in (0, -signal.SIGKILL), log_path.read_text()


async def _kill_engine(child):
    child.kill()
    await asyncio.to_thread(child.wait, timeout=5)
    assert child.returncode == -signal.SIGKILL


def _ladder_done(root):
    return _plans(root, "ladder_done")


@linux_only
def test_retry_waits_for_cleanup_then_update_releases_replacement(short_root):
    """t1 24/26/46, t3 47: a natural retry crosses the update's dispatch filter."""
    childfile = short_root / "child"
    body = _stubborn(childfile)
    body += f"while [ ! -s {childfile} ]; do sleep 0.01; done\nexit 1\n"
    command = _script(short_root, "retry", body)
    path = _config(short_root, _job(command, "n_retrys: 1\n"))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, _):
            await client.event("STARTJOB")
            child = await _written(childfile)
            await _until(lambda: bool(_records(root, "outcome")))
            [outcome] = _records(root, "outcome")
            assert (outcome["outcome"], outcome["ladder"]) == ("natural", "running")
            [plan] = _plans(root)
            assert plan["plan"] == "rerun"
            (short_root / "jobs.jil").write_text(_job("true", "n_retrys: 1\n"))
            await client.update()
            assert len(_records(root, "dispatch")) == 1 and _live(child)
            assert not _ladder_done(root)
            _release_child(childfile)
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            [first, replacement] = _records(root, "dispatch")
            assert replacement["attempt"] == plan["steps"][0]["attempt"]
            assert (replacement["ordinal"], replacement["run_number"]) == (2, 1)
            assert replacement["after"] == "exit 1" and not _live(child)
            journal = rows(root)
            done = next(
                s for s, k, b in journal if k == "follow_up" and b["event"] == "ladder_done"
            )
            applied = next(
                s
                for s, k, b in journal
                if k == "config" and b["event"] == "applied" and b["rev"] == 2
            )
            launched = next(s for s, k, b in journal if k == "dispatch" and b != first)
            assert done < applied < launched
            await _until(lambda: len(_records(root, "outcome")) == 2)
            assert (await client.ask("status"))["jobs"]["j"]["status"] == "SUCCESS"

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


@linux_only
def test_restart_holds_a_pending_start_until_the_restored_ladder_ends(short_root):
    """t1 42: recovery discharges an owed start while its old cookie still lives."""
    childfile = short_root / "child"
    command = _script(short_root, "waiter", _stubborn(childfile) + "wait\n")
    path = _config(short_root, _job(command))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, engine):
            await client.event("STARTJOB")
            first_child = await _written(childfile)
            await client.event("KILLJOB")
            await _until(lambda: bool(_records(root, "outcome")))
            assert _records(root, "outcome")[0]["ladder"] == "running"
            await client.event("STARTJOB")
            [pending] = [b for b in _records(root, "dispatch_pending") if b["event"] == "pending"]
            pending_seq = next(
                s for s, k, b in rows(root) if k == "dispatch_pending" and b == pending
            )
            await _kill_engine(engine)
        async with _cli(path) as (client, _):
            status = await client.ask("status")
            assert status["engine"] == "RUNNING"
            assert len(_records(root, "dispatch")) == 1 and not _ladder_done(root)
            _release_child(childfile)
            await _until(lambda: len(_records(root, "dispatch")) == 2)
            assert _records(root, "dispatch")[1]["discharges"] == pending_seq
            assert _records(root, "dispatch")[1]["run_number"] == 2
            assert _ladder_done(root)
            second_child = await _next_child(childfile, first_child)
            _release_child(childfile)
            await _until(lambda: not _live(second_child))

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


def test_drain_prunes_every_decision_and_frees_its_request_id(tmp_path):
    """t1 51: the drain snapshot's prune leaves no decision to index."""
    root = tmp_path / "run"
    catalog = lower_source(_job("true"))

    async def scenario():
        clock = VirtualClock(start=T0)
        engine = start_run(
            catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
        )
        envelope = Envelope(request_id="reused", expect={"global:G": 0})
        event = Event(at=T0, kind="SET_GLOBAL", payload={"name": "G", "value": "old"})
        first = engine.submit(event, envelope)
        await engine.run_until_quiescent(T0)
        assert (await first).decision == "applied"
        await clock.wait_until(T0 + timedelta(days=2))
        stop = engine.request_stop()
        await engine.run_until_quiescent(clock.now())
        await stop
        assert not _records(root, "decision")
        assert engine.decisions.state("reused") is None
        await _close(engine)
        engine = await resume_run(
            catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
        )
        try:
            await engine.run_until_quiescent(clock.now())
            again = engine.submit(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "G", "value": "new"}),
                Envelope(request_id="reused", expect={"global:G": 1}),
            )
            await engine.run_until_quiescent(clock.now())
            assert (await again).decision == "applied" and engine.deduped == []
            assert engine.oracle.store.globals_["G"].value == "new"
        finally:
            await _close(engine)

    asyncio.run(scenario())


@linux_only
def test_restart_resumes_the_box_roots_of_a_stopping_update(short_root):
    """t2 4: a real crash leaves an admitted restart_boxes intent."""
    childfile = short_root / "child"
    command = _script(short_root, "box-member", _stubborn(childfile) + "wait\n")
    text = "insert_job: BX\njob_type: b\n" + _job(command, "box_name: BX\n")
    path = _config(short_root, text)
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, engine):
            await client.event("STARTJOB", "BX")
            first_child = await _written(childfile)
            (short_root / "jobs.jil").write_text(text + _job("true", "box_name: BX\n", "new"))
            await client.update()
            [intent] = [b for b in _records(root, "config") if b["event"] == "intent"]
            assert intent["restart_boxes"] == ["BX"]
            await _until(lambda: (root / "runs/j.1/1/status.json").exists())
            assert not any(
                b["event"] == "applied" and b["rev"] == 2 for b in _records(root, "config")
            )
            await _kill_engine(engine)
        async with _cli(path) as (client, _):
            _release_child(childfile)
            await _until(
                lambda: any(
                    b["event"] == "applied" and b["rev"] == 2 for b in _records(root, "config")
                )
            )
            status = await client.ask("status")
            assert status["config_rev"] == 2 and status["jobs"]["BX"]["run_number"] == 2
            await _until(lambda: (root / "runs/new.1/1/spawn.json").exists())
            await _until(lambda: (root / "runs/j.2/1/spawn.json").exists())
            second_child = await _next_child(childfile, first_child)
            _release_child(childfile)
            await _until(lambda: not _live(second_child))

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


@linux_only
def test_restart_uses_leak_policy_updated_during_natural_cleanup(short_root):
    """t2 18: the applied switch leaves a closing natural attempt's cookie alone."""
    childfile = short_root / "child"
    body = _stubborn(childfile)
    body += f"while [ ! -s {childfile} ]; do sleep 0.01; done\nexit 0\n"
    text = _job(_script(short_root, "leak", body))
    path = _config(short_root, text)
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, engine):
            await client.event("STARTJOB")
            child = await _written(childfile)
            await _until(lambda: bool(_records(root, "outcome")))
            assert _records(root, "outcome")[0]["ladder"] == "running"
            _config(short_root, text, semantics='[estate.semantics]\nprocess-leak = "leak"\n')
            await client.update(wait=True)
            assert not _ladder_done(root) and _live(child)
            await _kill_engine(engine)
        async with _cli(path) as (client, _):
            assert (await client.ask("status"))["config_rev"] == 2
            assert _live(child)
            await _until(lambda: bool(_ladder_done(root)))
            assert _live(child) and len(_records(root, "dispatch")) == 1
            assert "j.1.1: leaked: 1, left running" in (short_root / "live-engine.log").read_text()
            _release_child(childfile)
            await _until(lambda: not _live(child))

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


@linux_only
def test_restart_reports_a_leaked_cookie_with_no_open_attempt(short_root):
    """t2 29/30: a completed leak-policy attempt is absent from the restart's open table."""
    childfile = short_root / "child"
    body = _stubborn(childfile)
    body += f"while [ ! -s {childfile} ]; do sleep 0.01; done\nexit 0\n"
    path = _config(
        short_root,
        _job(_script(short_root, "leak", body)),
        semantics='[estate.semantics]\nprocess-leak = "leak"\n',
    )
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, engine):
            await client.event("STARTJOB")
            child = await _written(childfile)
            await _until(lambda: bool(_plans(root, "done")))
            attempt = _records(root, "dispatch")[0]["attempt"]
            await _kill_engine(engine)
        async with _cli(path) as (client, _):
            assert (await client.ask("status"))["jobs"]["j"]["status"] == "SUCCESS"
            assert _live(child) and len(_records(root, "dispatch")) == 1
            note = f"j.1: run id {attempt} has no open attempt; its processes are left alone"
            assert (short_root / "live-engine.log").read_text().count(note) == 1
            _release_child(childfile)
            await _until(lambda: not _live(child))

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


def test_restart_drops_hold_evidence_after_release_and_a_new_start(tmp_path):
    """t2 47: tail dispatch of a later run clears a real interruption's evidence."""
    root = tmp_path / "run"
    text = _job("true")
    _open(root, text, ["j"], marked={"j": "synthetic non-idempotent effect"})

    async def restart(clock):
        return await resume_run(
            lower_source(text), root, clock=clock, adapters={"CMD": FakeAdapter(default=None)}
        )

    async def scenario():
        clock = VirtualClock(start=T0 + timedelta(minutes=1))
        engine = await restart(clock)
        await engine.run_until_quiescent(clock.now())
        assert engine.oracle.store.job["j"].on_hold and engine.evidence["j"]
        for kind in ("OFF_HOLD", "FORCE_STARTJOB"):
            engine.inject(Event(at=clock.now(), kind=kind, payload={"job": "j"}))
            await engine.run_until_quiescent(clock.now())
        assert _records(root, "dispatch")[-1]["run_number"] == 2
        # _close, like _open, abandons the run without a lifecycle shutdown record or snapshot.
        await _close(engine)
        engine = await restart(VirtualClock(start=T0 + timedelta(minutes=2)))
        try:
            assert "j" not in engine.evidence
            assert not _records(root, "engine")[-1]["event"].endswith("shutdown")
        finally:
            await _close(engine)

    asyncio.run(scenario())


def test_drain_snapshot_restores_and_launches_the_dependent_pending_start(tmp_path):
    """t2 51, t4 61: an adapter completion queued behind stop decides a pending start."""
    root = tmp_path / "run"
    text = _job("true") + _job("true", "condition: s(j)\n", "k")
    catalog = lower_source(text)

    async def scenario():
        clock = VirtualClock(start=T0)
        engine = start_run(
            catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter({("j", 1): (1, 0)}, default=None)},
            settings=EngineSettings(),
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            await clock.wait_until(T0 + timedelta(seconds=1))
            stop = engine.request_stop()
            await engine.run_until_quiescent(clock.now())
            await stop
        finally:
            await _close(engine)
        with closing(open_reader(root)) as reader:
            saved = reader.latest_snapshot()
            snapshot = decode_snapshot(saved.seq, saved.data, where="drain snapshot")
        [pending] = snapshot.open.pending.values()
        assert (pending.job, pending.run_number) == ("k", 1)
        assert [b["job"] for b in _records(root, "dispatch")] == ["j"]
        journal = rows(root)
        drain = next(s for s, k, b in journal if k == "engine" and b["event"] == "draining")
        assert drain < pending.seq <= saved.seq
        engine = await resume_run(
            catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(),
        )
        try:
            await engine.run_until_quiescent(clock.now())
            [launched] = [b for b in _records(root, "dispatch") if b["job"] == "k"]
            assert launched["discharges"] == pending.seq
            assert not engine.journal.open.pending
        finally:
            await _close(engine)

    asyncio.run(scenario())


@linux_only
def test_reread_blocked_notes_and_update_id_collision_over_the_socket(short_root):
    """t4 21/22/29: real drain/update windows and a config intent's unique request id."""
    childfile = short_root / "child"
    command = _script(short_root, "stops", _stubborn(childfile) + "wait\n")
    path = _config(short_root, _job(command))
    root = short_root / "run"

    async def scenario():
        async with _cli(path) as (client, _):
            await client.event("STARTJOB")
            first_child = await _written(childfile)
            (short_root / "jobs.jil").write_text(_job(command + " # changed"))
            await client.update(rid="shared-id")
            preview = await client.ask("reread")
            assert (
                "another update is open or preparing; wait for it to finish"
                in preview["preview"]["definition_changes"]["blocked"]
            )
            status = await client.ask("status")
            collision = await client.ask(
                "sendevent",
                verb="ON_HOLD",
                payload={"job": "j"},
                request_id="shared-id",
                baseline_id=status["baseline_id"],
                expect={"job:j": status["jobs"]["j"]["state_rev"]},
            )
            assert collision["code"] == "request_id_reused" and collision["refused"]
            assert "original_decision" not in collision
            _release_child(childfile)
            await _until(lambda: any(b["event"] == "applied" for b in _records(root, "config")))
            await _until(lambda: (root / "runs/j.1/2/spawn.json").exists())
            second_child = await _next_child(childfile, first_child)
            await client.ask("engine_stop")
            preview = await client.ask("reread")
            assert (
                "the engine is draining; it takes no update until it restarts"
                in preview["preview"]["definition_changes"]["blocked"]
            )
            _release_child(childfile)
            await _until(lambda: not _live(second_child))

    try:
        asyncio.run(scenario())
    finally:
        _kill(childfile)


def test_update_removes_demand_and_next_start_takes_over_held_units(short_root):
    """t5 F4: job.302 -> job.310 frees retained units when current demand is empty."""
    from dsl41.reload_plan import reread
    from dsl41.runner_update import UpdateRequest

    root = short_root / "run"
    demand = "resources: (LOCK, QUANTITY=1, FREE=N)\n"
    text = "insert_resource: LOCK\nres_type: R\namount: 1\n" + _job("true", demand)
    text += _job("true", "resources: (LOCK, QUANTITY=1, FREE=Y)\n", "k")
    path = _config(short_root, text)
    candidate = main.prepare(path)

    async def scenario():
        clock = VirtualClock(start=T0)
        engine = start_run(
            candidate.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(candidate),
            source_capture=main.encoded_parts(candidate),
        )
        try:
            for kind, payload in (
                ("STARTJOB", {"job": "j"}),
                ("STATUS", {"job": "j", "status": "FAILURE"}),
                ("STARTJOB", {"job": "k"}),
            ):
                engine.inject(Event(at=clock.now(), kind=kind, payload=payload))
                await engine.run_until_quiescent(clock.now())
            assert engine.oracle.store.job["j"].reservations
            assert engine.oracle.store.job["k"].status == "QUE_WAIT"
            (short_root / "jobs.jil").write_text(text.replace(demand, ""))
            plan = reread(engine.update.live(), root / "plans", candidate=main.prepare(path))
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "no-demand")
            )
            await engine.run_until_quiescent(clock.now())
            assert answer.result().applied.result()["rev"] == 2
            assert engine.oracle.store.job["j"].reservations
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(clock.now())
            j, k = engine.oracle.store.job["j"], engine.oracle.store.job["k"]
            assert (j.status, j.reservations, k.status) == ("RUNNING", (), "RUNNING")
            assert [(r.bucket, r.units) for r in k.reservations] == [("r:LOCK", 1)]
        finally:
            await _close(engine)

    asyncio.run(scenario())
