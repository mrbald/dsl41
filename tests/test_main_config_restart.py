"""Main-config restart rulings and refusal boundaries (DL-437, DL-432, DL-433)."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import main_config as main, runner_startup
from dsl41.cli import app
from dsl41.cli_reload import live_of_answer
from dsl41.oracle_state import Event
from dsl41.reload_plan import reread
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_config import Configuration, with_host_settings
from dsl41.runner_control import roundtrip
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import active_configuration, resume_run, start_run
from dsl41.runner_store import Record, SqliteStore
from dsl41.runner_update import UpdateRequest, store_config
from store_surgery import rows
from test_main_config import JOB, config_file, estate_file, without_cookie_scan_notice

pytestmark = pytest.mark.usefixtures("cli_env")


@contextmanager
def cli_engine(path: Path, *, force_unsupported_shell: bool = False):
    root = main.load_run_root(path)
    socket = root / "control.sock"
    log = root.parent / "cli-engine.log"
    ready = stopped = False
    with log.open("w") as stream:
        command = [sys.executable, "-m", "dsl41", "run", "--config", str(path)]
        if force_unsupported_shell:
            bootstrap = (
                "import os,pwd,runpy,sys,types; "
                "account=pwd.getpwuid(os.geteuid()); "
                "pwd.getpwuid=lambda uid: types.SimpleNamespace(pw_name=account.pw_name,pw_shell='/bin/zsh'); "
                "sys.argv=['dsl41','run','--config',sys.argv[1]]; "
                "runpy.run_module('dsl41',run_name='__main__')"
            )
            command = [sys.executable, "-c", bootstrap, str(path)]
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while "engine up; control socket:" not in log.read_text():
                assert child.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            ready = True

            def ask(command="catalog"):
                nonlocal stopped
                answer = roundtrip(socket, {"v": 3, "cmd": command}, timeout=3)
                stopped |= answer.get("engine") in ("DRAINING", "STOPPED")
                return answer

            assert ask("status")["engine"] == "RUNNING"
            yield ask
        finally:
            stopped |= ready and not socket.exists()
            if child.poll() is None and not stopped:
                child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
                assert not stopped, log.read_text()
            assert child.returncode == 0, log.read_text()


@pytest.mark.parametrize("stop", ["socket", "reply"])
@pytest.mark.parametrize("timeout", [False, True], ids=["delayed_exit", "exit_timeout"])
def test_cli_engine_never_sends_term_after_protocol_stop(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, stop: str, timeout: bool
) -> None:
    path = estate_file(short_root)
    socket = short_root / "run" / "control.sock"
    socket.parent.mkdir()
    socket.touch()
    calls = []

    class DelayedExit:
        returncode = None
        killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            calls.append(("wait", timeout))
            if exit_timeout and not self.killed:
                raise subprocess.TimeoutExpired("engine", timeout)
            self.returncode = -9 if self.killed else 0
            return self.returncode

        def terminate(self):
            pytest.fail("TERM after protocol stop")

        def kill(self):
            calls.append(("KILL",))
            self.killed = True

    child = DelayedExit()
    exit_timeout = timeout

    def popen(*args, stdout, **kwargs):
        stdout.write("engine up; control socket: test\n")
        stdout.flush()
        return child

    def answer(socket, request, **kwargs):
        return {"ok": True, "engine": "STOPPED" if request["cmd"] == "engine_stop" else "RUNNING"}

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(f"{__name__}.roundtrip", answer)

    def exercise():
        with cli_engine(path) as ask:
            if stop == "socket":
                socket.unlink()
            else:
                assert ask("engine_stop")["ok"]
            assert child.poll() is None

    if timeout:
        with pytest.raises(AssertionError, match="engine up; control socket: test"):
            exercise()
    else:
        exercise()
        assert child.returncode == 0
    assert calls == ([("wait", 15), ("KILL",), ("wait", 10)] if timeout else [("wait", 15)])


def read_config_row(root: Path, rev: int) -> bytes:
    with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
        return store.get_config(rev)


def plan_for(path: Path, ask):
    root = main.load_run_root(path)
    return reread(live_of_answer(ask()), root / "plans", candidate=main.prepare(path))


@pytest.mark.parametrize(
    "options",
    [
        {"lint": 'ignore = ["L999"]\n'},
        {"lint": 'refuse = "invalid"\n'},
        {"estate": 'semantics = {no_such_switch = "x"}\n'},
        {"estate": 'timezone = "Europe/Lodon"\n'},
        {"estate": 'timezone_map = "missing.map"\n'},
        {"estate": 'non_idempotent = "../invalid"\n'},
        {"include": ["**/*.jil"]},
        {"properties": ["missing.properties"]},
        {"header": 'format_version = 1\nrequires_dsl41 = ">=999.0"\n'},
        {"engine": 'environment = "undeclared"\n'},
        {"engine": "environment = false\n"},
    ],
)
def test_b1_estate_and_lint_edits_stay_pending(short_root: Path, options: dict) -> None:
    path = estate_file(
        short_root,
        engine='environment = "applied"\n',
        estate="[estate.environments.applied]\nproperties = []\n",
    )
    root = short_root / "run"
    with cli_engine(path):
        pass
    before = read_config_row(root, 1)
    config_file(short_root, **options)
    with cli_engine(path) as ask:
        assert ask()["revision"] == 1
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "true"
        assert re.search(
            r"[1-9]\d* pending source changes", (short_root / "cli-engine.log").read_text()
        )
    assert read_config_row(root, 1) == before
    assert main.decoded_parts(active_configuration(root).source_capture).environment == "applied"


@pytest.mark.parametrize("change", ["changed", "deleted", "malformed"])
@pytest.mark.usefixtures("cli_env")
def test_b4_updated_root_resumes_captured_inputs(short_root: Path, change: str) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    with cli_engine(path) as ask:
        (short_root / "a.jil").write_text(JOB.replace("true", "echo reviewed"))
        result = CliRunner().invoke(
            app, ["update", "-S", str(short_root / "run" / "control.sock"), "--wait"]
        )
        assert result.exit_code == 0, result.output
        assert ask()["revision"] == 2
    before = read_config_row(root, 2)
    if change == "deleted":
        (short_root / "a.jil").unlink()
    else:
        (short_root / "a.jil").write_text(
            JOB.replace("true", "echo unreviewed") if change == "changed" else "broken:\n"
        )
    with cli_engine(path) as ask:
        assert ask()["revision"] == 2
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo reviewed"
    assert read_config_row(root, 2) == before


def seed_root(path: Path, *, stopping: bool, boundary: bool, snapshot: bool = False):
    initial = main.prepare(path)
    root = main.load_run_root(path)

    async def seed():
        clock = RealClock()
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            if stopping:
                engine.inject(Event(at=clock.now(), kind="FORCE_STARTJOB", payload={"job": "A"}))
                loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
                try:
                    async with asyncio.timeout(5):
                        while "A" not in engine._live:
                            await asyncio.sleep(0.01)
                finally:
                    loop.cancel()
                    await asyncio.gather(loop, return_exceptions=True)
            if snapshot:
                engine.take_snapshot()
            (path.parent / "a.jil").write_text(JOB.replace("true", "echo captured"))
            candidate = main.prepare(path)
            result = reread(engine.update.live(), root / "plans", candidate=candidate)
            if stopping:
                request = UpdateRequest(result.token, 1, engine.baseline_id, "review-recovery")
                intent = {
                    "event": "intent",
                    "token": result.token,
                    "live_rev": 1,
                    "stop": [{"job": "A", "run_number": 1, "attempt": engine._live["A"].attempt}],
                    "filter": ["A"],
                    "stop_running": True,
                    "request_id": request.request_id,
                    "fingerprint": request.fingerprint,
                }
                engine.journal.append(
                    Record(clock.now(), "config", None, request.request_id, intent)
                )
            if boundary:
                configuration = Configuration(
                    catalog=candidate.catalog,
                    profile=main.profile_of(candidate),
                    source_capture=main.encoded_parts(candidate),
                )
                store_config(engine.journal.store, configuration, 2)
            return result
        finally:
            await engine.shutdown()
            engine.journal.close()

    return initial, asyncio.run(seed())


@pytest.mark.parametrize("snapshot", [False, True])
def test_b4_config_row_without_applied_record_is_not_active(
    short_root: Path, snapshot: bool
) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    initial, _ = seed_root(path, stopping=False, boundary=True, snapshot=snapshot)
    row1, row2 = read_config_row(root, 1), read_config_row(root, 2)
    (short_root / "a.jil").unlink()
    assert active_configuration(root).source_capture == main.encoded_parts(initial)
    with cli_engine(path) as ask:
        assert ask()["revision"] == 1
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "true"
    assert read_config_row(root, 1) == row1 and read_config_row(root, 2) == row2
    assert active_configuration(root).source_capture == main.encoded_parts(initial)


@pytest.mark.parametrize("boundary", [False, True])
def test_b2_b4_stopping_cli_restart_applies_host_and_captured_plan(
    short_root: Path, boundary: bool
) -> None:
    path = estate_file(
        short_root, engine='as_machine = ["before"]\n', estate='timezone = "Europe/Zurich"\n'
    )
    root = short_root / "run"
    _, plan = seed_root(path, stopping=True, boundary=boundary)
    row1 = read_config_row(root, 1)
    row2 = read_config_row(root, 2) if boundary else None
    plan_bytes = {p.name: p.read_bytes() for p in plan.files}
    (short_root / "a.jil").unlink()
    (short_root / "access.toml").write_text('format_version = 1\nunmapped = "read"\n')
    config_file(
        short_root,
        engine='as_machine = ["after"]\nmachine_policy = "local-eligible"\naccess_map = "access.toml"\nenvironment = "invalid"\n',
        estate='timezone = "not-a-zone"\n',
        lint='ignore = ["L999"]\n',
    )
    with cli_engine(path) as ask:
        answer = ask()
        assert answer["revision"] == 2
        assert answer["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo captured"
        assert answer["settings"]["timezone"] == "Europe/Zurich"
        assert answer["launch"]["as_machine"] == ["after"]
        assert answer["launch"]["machine_policy"] == "local-eligible"
        assert answer["launch"]["access_map"] == str(short_root / "access.toml")
        assert ask("status").get("update") is None
    assert read_config_row(root, 1) == row1
    if boundary:
        assert read_config_row(root, 2) == row2
    stored = active_configuration(root)
    assert stored.profile.as_machine == ("before",)
    assert main.decoded_parts(stored.source_capture).metadata["config"]["lint"]["refuse"] == "none"
    assert {p.name: p.read_bytes() for p in plan.files} == plan_bytes
    assert (root / "perimeter.jsonl").is_file()


@pytest.mark.parametrize(
    "damage", ["missing", "corrupt", "incompatible", "settings", "v1", "unversioned"]
)
def test_b3_plan_refusal_precedes_engine_journal_and_signals(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    _, plan = seed_root(path, stopping=True, boundary=False)
    before = rows(root)
    if damage == "missing":
        shutil.rmtree(plan.plan_dir)
    elif damage == "corrupt":
        plan.plan_json.write_bytes(b"invalid JSON")
    elif damage in {"settings", "v1", "unversioned"}:
        import json

        document = json.loads(plan.plan_json.read_bytes())
        if damage == "settings":
            document["settings"] = []
        elif damage == "v1":
            document["plan_format_version"] = 1
        else:
            document.pop("plan_format_version")
        plan.plan_json.write_text(json.dumps(document))
    else:

        def rejected(*args, **kwargs):
            raise main.RereadRefused(["synthetic.jil: attribute rejected by newer lowering"])

        monkeypatch.setattr(main, "lower_catalog", rejected)
    effects = []

    def forbidden(*args, **kwargs):
        effects.append("runtime effect")
        pytest.fail("recovery effects began before the plan refusal")

    monkeypatch.setattr(runner_startup, "Engine", forbidden)
    monkeypatch.setattr(runner_startup, "_first_reading", forbidden)
    import dsl41.runner_procid

    monkeypatch.setattr(dsl41.runner_procid, "signal_ours", forbidden)
    stored = active_configuration(root)
    monkeypatch.setattr(SqliteStore, "append", forbidden)
    monkeypatch.setattr(SqliteStore, "put_config", forbidden)
    with pytest.raises(EngineError, match="plan cannot be read") as error:
        asyncio.run(
            resume_run(
                stored.catalog,
                root,
                clock=RealClock(),
                adapters={"CMD": FakeAdapter()},
                declared=stored.profile,
                stored_sources=True,
            )
        )
    if damage in {"v1", "unversioned"}:
        assert "reload plan version 1 is retired (DL-486)" in str(error.value)
        assert "this build cannot finish the open update" in str(error.value)
        assert "Finish the update with the build that began it." in str(error.value)
        assert "restore" not in str(error.value)
    if damage == "incompatible":
        assert "attribute rejected by newer lowering" in str(error.value)
        assert "use a compatible binary" in str(error.value)
    assert effects == [] and rows(root) == before


@pytest.mark.parametrize("missing", ["config", "launch"])
@pytest.mark.parametrize("boundary", [False, True])
def test_restart_interrupted_update_with_earlier_default_shape(short_root, missing, boundary):
    import base64
    import hashlib
    import json
    import sqlite3

    from dsl41.canon import canonical_bytes
    from dsl41.reload_diff import plan_token
    from dsl41.reload_plan import read_plan
    from dsl41.runner_update import open_plan
    from store_surgery import update

    path = estate_file(short_root)
    root = short_root / "run"
    _, plan = seed_root(path, stopping=True, boundary=boundary)
    document = json.loads(plan.plan_json.read_bytes())
    capture_path = plan.plan_dir / main.CAPTURE
    metadata = json.loads(capture_path.read_bytes())
    if missing == "config":
        assert metadata["config"]["engine"].pop("environment") is None
    else:
        assert metadata["launch"].pop("status_hook_long_running_s") == 0
    capture_path.write_bytes(canonical_bytes(metadata))
    parts = [
        (part["name"], (plan.plan_dir / part["name"]).read_bytes()) for part in document["parts"]
    ]
    for part, (_, data) in zip(document["parts"], parts, strict=True):
        part["sha256"] = hashlib.sha256(data).hexdigest()
    token = plan_token(parts, document["live_rev"])
    document["token"] = token
    plan.plan_json.write_bytes(canonical_bytes(document))
    directory = plan.plan_dir.with_name(token)
    plan.plan_dir.rename(directory)
    for seq, kind, body in rows(root):
        if kind == "config" and body.get("event") == "intent":
            update(root, seq, {**body, "token": token})
    if boundary:
        payload = json.loads(read_config_row(root, 2))
        payload["source_capture"] = {
            name: base64.b64encode(data).decode("ascii") for name, data in parts
        }
        data = canonical_bytes(payload)
        with closing(sqlite3.connect(root / "engine.db", isolation_level=None)) as db:
            db.execute(
                "UPDATE configs SET data = ?, digest = ? WHERE rev = 2",
                (data, "sha256:" + hashlib.sha256(data).hexdigest()),
            )
    assert read_plan(root / "plans", token) is not None
    before = {p.name: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    row1 = read_config_row(root, 1)
    row2 = read_config_row(root, 2) if boundary else None
    (short_root / "a.jil").unlink()
    with cli_engine(path) as ask:
        assert ask()["revision"] == 2
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo captured"
        assert ask("status").get("update") is None
    opened = open_plan(root, token).estate
    assert opened.metadata["config"]["engine"]["environment"] is None
    assert opened.metadata["launch"]["status_hook_long_running_s"] == 0
    assert read_config_row(root, 1) == row1
    if boundary:
        assert read_config_row(root, 2) == row2
    assert {p.name: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("damage", ["unknown", "value", "config", "launch"])
def test_capture_normalization_keeps_strict_binding(short_root, damage):
    import json

    from dsl41.canon import canonical_bytes

    candidate = main.prepare(estate_file(short_root))
    parts = dict(candidate.parts)
    metadata = json.loads(parts[main.CAPTURE])
    if damage == "unknown":
        metadata["config"]["engine"]["unknown_setting"] = True
    elif damage == "value":
        metadata["config"]["engine"]["environment"] = 42
    elif damage == "config":
        metadata["config"]["engine"]["job_env"] = {"SYNTHETIC": "different"}
    else:
        metadata["launch"]["machine_policy"] = "local-eligible"
    parts[main.CAPTURE] = canonical_bytes(metadata)
    with pytest.raises(main.RereadRefused):
        main.verified_from_parts(list(parts.items()))


@pytest.mark.parametrize(
    "section,damage", [(s, d) for s in ("config", "launch") for d in ("unknown", "value")]
)
def test_cli_restart_rejects_invalid_capture_before_effects(
    short_root, monkeypatch, section, damage
):
    import base64
    import hashlib
    import json
    import sqlite3

    from dsl41.canon import canonical_bytes
    import dsl41.runner_procid

    path = estate_file(short_root)
    root, _ = k_seed(path)
    payload = json.loads(read_config_row(root, 1))
    metadata = json.loads(base64.b64decode(payload["source_capture"][main.CAPTURE]))
    target = metadata["config"]["engine"] if section == "config" else metadata["launch"]
    field = "unknown_setting" if damage == "unknown" else "machine_policy"
    target[field] = True if damage == "unknown" else "invalid"
    payload["source_capture"][main.CAPTURE] = base64.b64encode(canonical_bytes(metadata)).decode()
    data = canonical_bytes(payload)
    with closing(sqlite3.connect(root / "engine.db", isolation_level=None)) as db:
        db.execute(
            "UPDATE configs SET data = ?, digest = ? WHERE rev = 1",
            (data, "sha256:" + hashlib.sha256(data).hexdigest()),
        )
    (root / "leader.lock").unlink()
    before = k_root_bytes(root)

    def forbidden(*args, **kwargs):
        pytest.fail("runtime effect before captured config refusal")

    monkeypatch.setattr(runner_startup, "Engine", forbidden)
    monkeypatch.setattr(runner_startup, "_first_reading", forbidden)
    monkeypatch.setattr(dsl41.runner_procid, "signal_ours", forbidden)
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2, result.output
    assert field in result.output
    assert k_root_bytes(root) == before


@pytest.mark.usefixtures("k_bourne_account")
@pytest.mark.parametrize("old_runtime_shape", [False, True])
def test_interrupted_update_survives_distribution_version_change(
    short_root, monkeypatch, old_runtime_shape
):
    import json
    from dataclasses import replace

    from dsl41 import runner_update
    from dsl41.canon import canonical_bytes
    from dsl41.ir import tool_version
    from dsl41.reload_diff import definition_hash
    from dsl41.runner_config import RuntimeProfile
    from dsl41.runner_update import open_plan

    packages = short_root / "packages"
    distribution = packages / "dsl41-2.0.0.dist-info"
    distribution.mkdir(parents=True)
    version_file = distribution / "METADATA"
    version_file.write_text("Name: dsl41\nVersion: 2.0.0\n")
    monkeypatch.syspath_prepend(str(packages))
    assert tool_version() == "2.0.0"
    path = estate_file(short_root)
    root = short_root / "run"
    initial, plan = seed_root(path, stopping=True, boundary=True)
    row1 = read_config_row(root, 1)
    retained = read_config_row(root, 2)
    assert json.loads(retained)["catalog"]["meta"]["tool_version"] == "2.0.0"
    if old_runtime_shape:
        document = json.loads(plan.plan_json.read_bytes())
        assert document["settings"].pop("set_job_attribute_environmentals") is True
        old_settings = runner_update.settings_of(main.profile_of(initial), initial.catalog)
        old_settings.pop("set_job_attribute_environmentals")
        recorded = Configuration.from_bytes(retained, where="recorded candidate")
        for change in document["changed"]:
            name = change["name"]
            change["old_hash"] = definition_hash(
                initial.catalog.jobs[name], initial.catalog, old_settings
            )
            change["new_hash"] = definition_hash(
                recorded.catalog.jobs[name], recorded.catalog, document["settings"]
            )
        plan.plan_json.write_bytes(canonical_bytes(document))
        assert document["changed"][0]["new_hash"] != definition_hash(
            recorded.catalog.jobs["A"], recorded.catalog, main.prepare(path).settings
        )
    plan_bytes = {
        str(p.relative_to(plan.plan_dir)): p.read_bytes()
        for p in plan.plan_dir.rglob("*")
        if p.is_file()
    }
    version_file.write_text("Name: dsl41\nVersion: 2.0.1\n")
    assert tool_version() == "2.0.1"
    candidate = open_plan(root, plan.token).estate
    assert candidate.catalog.meta.tool_version == "2.0.1"
    configuration = Configuration(
        catalog=candidate.catalog,
        profile=main.profile_of(candidate),
        source_capture=main.encoded_parts(candidate),
    )
    stored = active_configuration(root)

    async def restart():
        engine = await resume_run(
            stored.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
            declared=stored.profile,
            stored_sources=True,
            hold_open=True,
        )
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            async with asyncio.timeout(10):
                while not engine.started.is_set() or engine.config_rev != 2:
                    if loop.done():
                        await loop
                    await asyncio.sleep(0.01)
            assert not engine.update.stopping
            assert engine.oracle.catalog.meta.tool_version == "2.0.1"
            assert engine.profile.set_job_attribute_environmentals is True
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(restart())
    assert read_config_row(root, 1) == row1
    assert read_config_row(root, 2) == retained
    assert {
        str(p.relative_to(plan.plan_dir)): p.read_bytes()
        for p in plan.plan_dir.rglob("*")
        if p.is_file()
    } == plan_bytes
    if old_runtime_shape:
        opened = open_plan(root, plan.token)
        nondefault = replace(
            opened,
            estate=replace(
                opened.estate,
                settings={**opened.estate.settings, "set_job_attribute_environmentals": False},
            ),
        )
        with monkeypatch.context() as changed:
            changed.setattr(runner_update, "open_plan", lambda *args: nondefault)
            with pytest.raises(
                EngineError, match="Finish the update using the binary that began it"
            ):
                runner_update.reopen_plan(root, plan.token, configuration=stored)
    job = candidate.catalog.jobs["A"]
    changed_job = job.model_copy(
        update={"exec_": job.exec_.model_copy(update={"command": "false"})}
    )
    changed_catalog = candidate.catalog.model_copy(update={"jobs": {"A": changed_job}})
    with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
        for change in (
            {"catalog": changed_catalog},
            {"profile": RuntimeProfile(default_tz="Europe/Zurich")},
            {"non_idempotent": {"A": "synthetic mark"}},
            {"source_capture": {"different": "Ynl0ZXM="}},
        ):
            with pytest.raises(EngineError, match="configuration rev"):
                store_config(store, configuration.model_copy(update=change), 2)
        assert store.current_config_rev() == 2 and store.get_config(2) == retained


@pytest.mark.usefixtures("k_bourne_account")
@pytest.mark.parametrize("drift_job", ["A", "B"])
def test_interrupted_update_refuses_compiler_definition_drift(short_root, monkeypatch, drift_job):
    from dsl41 import main_config
    from dsl41.ir import tool_version

    applied_version, writer_version, recovering_version = "2.0.0", "2.0.1", "2.0.2"
    packages = short_root / "packages"
    distribution = packages / "dsl41-2.0.0.dist-info"
    distribution.mkdir(parents=True)
    version_file = distribution / "METADATA"
    version_file.write_text(f"Name: dsl41\nVersion: {applied_version}\n")
    monkeypatch.syspath_prepend(str(packages))
    assert tool_version() == applied_version

    path = estate_file(short_root)
    jobs = JOB + "\ninsert_job: B\njob_type: c\ncommand: true\n"
    (short_root / "a.jil").write_text(jobs)
    initial = main.prepare(path)
    assert initial.catalog.meta.tool_version == applied_version
    root = short_root / "run"

    async def crash():
        clock = RealClock()
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            for job in ("A", "B"):
                engine.inject(Event(at=clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))
            loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
            try:
                async with asyncio.timeout(5):
                    while not {"A", "B"} <= engine._live.keys():
                        await asyncio.sleep(0.01)
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            (short_root / "a.jil").write_text(
                jobs.replace("command: true", "command: echo captured", 1)
            )
            version_file.write_text(f"Name: dsl41\nVersion: {writer_version}\n")
            assert tool_version() == writer_version
            candidate = main.prepare(path)
            assert candidate.catalog.meta.tool_version == writer_version
            plan = reread(engine.update.live(), root / "plans", candidate=candidate)
            assert plan.plan.unchanged == ("B",) and plan.stop == ("A",)
            request = UpdateRequest(plan.token, 1, engine.baseline_id, "compiler-drift")
            engine.journal.append(
                Record(
                    clock.now(),
                    "config",
                    None,
                    request.request_id,
                    {
                        "event": "intent",
                        "token": plan.token,
                        "live_rev": 1,
                        "stop": [
                            {"job": "A", "run_number": 1, "attempt": engine._live["A"].attempt}
                        ],
                        "filter": ["A"],
                        "stop_running": True,
                        "request_id": request.request_id,
                        "fingerprint": request.fingerprint,
                    },
                )
            )
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(crash())
    version_file.write_text(f"Name: dsl41\nVersion: {recovering_version}\n")
    assert tool_version() == recovering_version
    stored = active_configuration(root)
    assert stored.catalog.meta.tool_version == applied_version
    before = k_root_bytes(root)
    lower = main_config.lower_catalog
    recovered_versions = []

    def drift(*args, **kwargs):
        catalog = lower(*args, **kwargs)
        recovered_versions.append(catalog.meta.tool_version)
        job = catalog.jobs[drift_job]
        changed = job.model_copy(
            update={"exec_": job.exec_.model_copy(update={"command": "echo drift"})}
        )
        return catalog.model_copy(update={"jobs": {**catalog.jobs, drift_job: changed}})

    def forbidden(*args, **kwargs):
        pytest.fail("recovery effects before compiler drift refusal")

    monkeypatch.setattr(main_config, "lower_catalog", drift)
    monkeypatch.setattr(runner_startup, "Engine", forbidden)
    monkeypatch.setattr(runner_startup, "_first_reading", forbidden)
    monkeypatch.setattr("dsl41.runner_procid.signal_ours", forbidden)
    with pytest.raises(
        EngineError, match="compiled job diff differs from the recorded plan"
    ) as error:
        asyncio.run(
            resume_run(
                stored.catalog,
                root,
                clock=RealClock(),
                adapters={"CMD": FakeAdapter()},
                declared=stored.profile,
                stored_sources=True,
            )
        )
    assert recovered_versions == [recovering_version]
    assert "Finish the update using the binary that began it, then upgrade." in str(error.value)
    assert all(
        version not in str(error.value)
        for version in (applied_version, writer_version, recovering_version)
    )
    assert "recorded under" not in str(error.value)
    assert "cannot be read" not in str(error.value)
    assert k_root_bytes(root) == before


def test_b2_stopping_recovery_keeps_current_profile(short_root: Path) -> None:
    path = estate_file(short_root, engine='as_machine = ["before"]\n')
    root = short_root / "run"
    seed_root(path, stopping=True, boundary=True)
    row1, row2 = read_config_row(root, 1), read_config_row(root, 2)
    stored = active_configuration(root)
    selected = with_host_settings(stored.profile, ["after"], "local-eligible")

    async def restart():
        engine = await resume_run(
            stored.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
            declared=selected,
            stored_sources=True,
            hold_open=True,
        )
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            await asyncio.wait_for(engine.started.wait(), 10)
            assert engine.profile == selected
            assert engine.config_rev == 2
            assert engine.update.token is None
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(restart())
    assert read_config_row(root, 1) == row1 and read_config_row(root, 2) == row2


def test_b2_non_host_profile_drift_still_refuses(short_root: Path) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    seed_root(path, stopping=False, boundary=False)
    before = rows(root)
    stored = active_configuration(root)
    clock = RealClock()
    with pytest.raises(EngineError, match="default_tz"):
        asyncio.run(
            resume_run(
                stored.catalog,
                root,
                clock=clock,
                scheduler=Scheduler(stored.catalog, start=clock.now(), default_tz="Europe/Zurich"),
                adapters={"CMD": FakeAdapter()},
                declared=with_host_settings(stored.profile, ["after"], "local-eligible"),
                stored_sources=True,
            )
        )
    assert rows(root) == before


@pytest.mark.parametrize("placement", ["foreign", "mixed"])
def test_b2_restart_machine_preflight_uses_stored_catalog(short_root: Path, placement: str) -> None:
    path = estate_file(
        short_root, engine='as_machine = ["synthetic-local"]\nmachine_policy = "local-eligible"\n'
    )
    source = short_root / "a.jil"
    if placement == "foreign":
        source.write_text(JOB.replace("machine: localhost", "machine: synthetic-local"))
    else:
        source.write_text(
            JOB.replace("machine: localhost", "machine: synthetic-pool") + "\n"
            "insert_machine: synthetic-local\ntype: r\n\n"
            "insert_machine: synthetic-remote\ntype: r\n\n"
            "insert_machine: synthetic-pool\ntype: v\n"
            "machine: synthetic-local\nmachine: synthetic-remote\n"
        )
    with cli_engine(path):
        pass
    before = rows(short_root / "run")
    source.unlink()
    config_file(
        short_root,
        engine=(
            'as_machine = ["different-host"]\n'
            if placement == "foreign"
            else 'as_machine = ["synthetic-local"]\nmachine_policy = "strict"\n'
        ),
    )
    result = subprocess.run(
        [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2 and "machine" in result.stdout + result.stderr
    assert rows(short_root / "run") == before


@pytest.mark.parametrize(
    "engine,header",
    [
        ('as_machine = [""]\n', "format_version = 1\n"),
        ("as_machine = [3]\n", "format_version = 1\n"),
        ('machine_policy = "invalid"\n', "format_version = 1\n"),
        ('access_map = "missing.map"\n', "format_version = 1\n"),
        ('access_map = "bad.map"\n', "format_version = 1\n"),
        ("", "format_version = 99\n"),
        ("", "format_version = true\n"),
        ("", "[malformed\n"),
    ],
)
def test_b2_invalid_launch_refuses_without_fallback(
    short_root: Path, engine: str, header: str
) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    with cli_engine(path):
        pass
    before = rows(root)
    (short_root / "bad.map").write_text("invalid TOML [")
    config_file(short_root, engine=engine, header=header)
    result = subprocess.run(
        [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert rows(root) == before


@pytest.mark.parametrize("damage", ["missing", "utf8", "directory", "symlink"])
def test_b2_unreadable_selected_config_refuses(short_root: Path, damage: str) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    with cli_engine(path):
        pass
    before = rows(root)
    saved = path.read_bytes()
    path.unlink()
    if damage == "utf8":
        path.write_bytes(b"\xff")
    elif damage == "directory":
        path.mkdir()
    elif damage == "symlink":
        target = short_root / "other.toml"
        target.write_bytes(saved)
        path.symlink_to(target)
    result = subprocess.run(
        [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert rows(root) == before


@pytest.mark.parametrize("location", ["relative", "file", "file-child"])
def test_b2_unusable_root_refuses(short_root: Path, location: str) -> None:
    path = estate_file(short_root)
    blocked = short_root / "blocked"
    blocked.write_text("not a directory")
    selected = "relative" if location == "relative" else str(blocked)
    if location == "file-child":
        selected += "/child"
    path.write_text(path.read_text().replace(str(short_root / "run"), selected))
    result = subprocess.run(
        [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert blocked.read_text() == "not a directory"
    assert not (short_root / "run").exists()


def test_b2_changed_root_selects_a_new_engine(short_root: Path) -> None:
    path = estate_file(short_root)
    old = short_root / "run"
    with cli_engine(path):
        pass
    before = rows(old)
    (short_root / "a.jil").write_text(JOB.replace("true", "echo different engine"))
    selected = short_root / "other-run"
    path.write_text(path.read_text().replace(str(old), str(selected)))
    with cli_engine(path) as ask:
        assert ask()["revision"] == 1
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo different engine"
    assert (selected / "engine.db").is_file() and rows(old) == before


def test_b2_omitted_access_map_uses_owner_only(short_root: Path) -> None:
    access = short_root / "access.toml"
    path = estate_file(short_root, engine='access_map = "access.toml"\n')
    access.write_text('format_version = 1\nunmapped = "read"\n')
    with cli_engine(path):
        pass
    row = read_config_row(short_root / "run", 1)
    config_file(short_root)
    access.unlink()
    with cli_engine(path) as ask:
        assert ask()["launch"]["access_map"] is None
        assert (short_root / "run" / "control.sock").stat().st_mode & 0o777 == 0o600
    assert read_config_row(short_root / "run", 1) == row


@pytest.mark.usefixtures("cli_env")
def test_b2_changed_host_requires_restart_before_reread(short_root: Path) -> None:
    path = estate_file(short_root)
    with cli_engine(path):
        config_file(
            short_root, engine='as_machine = ["after"]\nmachine_policy = "local-eligible"\n'
        )
        result = CliRunner().invoke(app, ["reread", "-S", str(short_root / "run" / "control.sock")])
        assert (
            result.exit_code == 0
            and "host launch settings" in result.output
            and "restart" in result.output
        )
    row = read_config_row(short_root / "run", 1)
    with cli_engine(path) as ask:
        assert ask()["launch"]["as_machine"] == ["after"]
        result = CliRunner().invoke(app, ["reread", "-S", str(short_root / "run" / "control.sock")])
        assert result.exit_code == 0, result.output
    assert read_config_row(short_root / "run", 1) == row


@pytest.mark.usefixtures("cli_env")
def test_k6_socket_update_retry_does_not_reopen_sources(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(short_root)
    with cli_engine(path) as ask:
        (short_root / "a.jil").write_text(JOB.replace("true", "echo reviewed"))
        first = CliRunner().invoke(
            app, ["update", "-S", str(short_root / "run" / "control.sock"), "--wait"]
        )
        assert first.exit_code == 0, first.output
        rid = re.search(r"request_id: (\S+)", first.output).group(1)
        (short_root / "a.jil").unlink()
        path.write_text(
            f'format_version = 999\nrequires_dsl41 = false\nestate = false\nlint = []\n[engine]\nrun_root = "{short_root}/run"\nas_machine = 3\nmachine_policy = false\naccess_map = 7\nenvironment = []\n'
        )
        monkeypatch.setattr(
            main, "load_config", lambda *a, **k: pytest.fail("update interpreted config")
        )
        monkeypatch.setattr(
            main, "load_host", lambda *a, **k: pytest.fail("update read host settings")
        )
        result = CliRunner().invoke(
            app,
            [
                "update",
                "-S",
                str(short_root / "run" / "control.sock"),
                "--request-id",
                rid,
                "--wait",
            ],
        )
        assert result.exit_code == 0, result.output
        assert ask()["revision"] == 2
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo reviewed"


@pytest.fixture
def k_bourne_account(monkeypatch):
    from types import SimpleNamespace
    from dsl41 import runner_preflight

    monkeypatch.setattr(
        runner_preflight.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="synthetic_user", pw_shell="/bin/sh"),
    )


def k_seed(path):
    estate = main.prepare(path)
    root = main.load_run_root(path)
    engine = start_run(
        estate.catalog,
        root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter()},
        launched=main.profile_of(estate),
        source_capture=main.encoded_parts(estate),
    )
    engine.journal.close()
    return root, estate


def k_root_bytes(root):
    return {
        str(path.relative_to(root)): (
            path.stat().st_mode,
            path.read_bytes() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


@pytest.mark.usefixtures("k_bourne_account")
@pytest.mark.parametrize("failure", ["lowering", "catalog", "order", "scanner"])
@pytest.mark.parametrize("missing_lock", [False, True])
def test_k1_captured_compiler_changes_keep_applied_ir(
    short_root, monkeypatch, failure, missing_lock
):
    from dsl41 import cli_run
    from dsl41.ast_jil import JilParseError
    from dsl41.ir import lower_source
    from dsl41.reload_plan import RereadRefused

    path = estate_file(short_root)
    (short_root / "a.jil").write_text(
        JOB + "group: batch\n\ninsert_job: B\njob_type: c\ncommand: true\n"
    )
    root, captured = k_seed(path)
    stored = active_configuration(root)
    before = read_config_row(root, 1)
    if missing_lock:
        (root / "leader.lock").unlink()

    def incompatible(value, **kwargs):
        if failure == "scanner":
            raise JilParseError("rejected by newer scanner", kwargs["file"], 1)
        if failure == "lowering":
            raise RereadRefused(["synthetic.jil: job A: attribute rejected by newer lowering"])
        return (
            lower_source(JOB.replace("true", "false"))
            if failure == "catalog"
            else stored.catalog.model_copy(
                update={"jobs": dict(reversed(list(stored.catalog.jobs.items())))}
            )
        )

    real_end = cli_run._await_run_end
    real_started = cli_run._await_started
    real_inspect = runner_startup._inspect_store
    serving = False
    seen = []

    async def await_started(engine, loop_task):
        nonlocal serving
        serving = await real_started(engine, loop_task)
        return serving

    def inspect(*args, **kwargs):
        assert not serving, "store inspected after engine up"
        return real_inspect(*args, **kwargs)

    async def running(engine, server, loop_task, ui):
        assert engine.started.is_set()
        assert engine.oracle.catalog == stored.catalog
        assert list(engine.oracle.catalog.jobs) == ["A", "B"]
        spec = server._spec({"job": "A"})
        assert spec["ok"] and (spec["jil"] is None) == (failure == "scanner")
        seen.append(engine.config_rev)
        engine.request_stop()
        return await real_end(engine, server, loop_task, ui)

    monkeypatch.setattr(main, "parse" if failure == "scanner" else "lower_catalog", incompatible)
    monkeypatch.setattr(cli_run, "_await_started", await_started)
    monkeypatch.setattr(runner_startup, "_inspect_store", inspect)
    monkeypatch.setattr(cli_run, "_await_run_end", running)
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert seen == [1] and "engine up; control socket:" in result.output
    assert without_cookie_scan_notice(result.stderr) == (
        "known-unsupported warn: 1 finding\n"
        f"files on disk (may differ from applied estate until update): dsl41 lint --config {path}\n"
    )
    assert [body["items"] for _, kind, body in rows(root) if kind == "preflight"] == [
        [
            {
                "severity": "WARN",
                "code": "known-unsupported",
                "job": "A",
                "message": "job A: group: known but unsupported; it has no effect in this release",
            },
        ]
    ]
    assert read_config_row(root, 1) == before
    assert main.encoded_parts(captured) == stored.source_capture


@pytest.mark.usefixtures("k_bourne_account")
@pytest.mark.parametrize("failure", ["lowering", "catalog"])
def test_k1_v2_capture_failure_names_revision_without_root_effects(
    short_root, monkeypatch, failure
):
    import hashlib
    import json
    import sqlite3
    from dataclasses import replace

    from dsl41.canon import canonical_bytes
    from dsl41.ir import lower_source
    from dsl41.reload_plan import RereadRefused

    path = estate_file(short_root)
    root, _ = k_seed(path)
    config = active_configuration(root)
    payload = json.loads(config.to_bytes())
    payload.pop("job_order")
    payload["configuration_format_version"] = 2
    legacy = canonical_bytes(payload)
    with closing(sqlite3.connect(root / "engine.db", isolation_level=None)) as db:
        db.execute(
            "UPDATE configs SET data = ?, digest = ? WHERE rev = 1",
            (legacy, "sha256:" + hashlib.sha256(legacy).hexdigest()),
        )
    # A valid v2 row still restores its capture-derived order, without rewriting the row.
    valid = runner_startup.active_configuration(root, check_lowering=True)
    assert list(valid.catalog.jobs) == list(config.catalog.jobs)
    before = k_root_bytes(root)
    original = main.decoded_parts

    def incompatible(parts):
        if failure == "lowering":
            raise RereadRefused(["synthetic.jil: incompatible lowering"])
        return replace(original(parts), catalog=lower_source(JOB.replace("true", "false")))

    monkeypatch.setattr(main, "decoded_parts", incompatible)
    monkeypatch.setattr(
        runner_startup,
        "_first_reading",
        lambda *args: pytest.fail("recovery effects on K1 refusal"),
    )
    for _ in range(2):
        with pytest.raises(EngineError, match="restart-lowering: configuration revision 1"):
            asyncio.run(
                resume_run(config.catalog, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
            )
        assert k_root_bytes(root) == before
    with pytest.raises(
        EngineError, match="unreadable configuration source_capture|source_capture catalog hash"
    ) as error:
        Configuration.from_bytes(legacy, where="offline")
    assert "restart-lowering" not in str(error.value)


def test_restart_prints_and_journals_authoritative_warnings_once(short_root: Path) -> None:
    path = estate_file(short_root, engine='machine_policy = "local-eligible"\n')
    (short_root / "a.jil").write_text(
        "insert_machine: here\ntype: a\nnode_name: localhost\n\n"
        "insert_machine: there\ntype: a\nnode_name: elsewhere.example.com\n\n"
        "insert_machine: pool\ntype: v\nmachine: here\nmachine: there\n\n"
        "insert_job: A\njob_type: c\ncommand: true\nmachine: pool\nauto_hold: 1\n"
    )
    root = main.load_run_root(path)
    with cli_engine(path, force_unsupported_shell=True):
        pass
    first_log = (short_root / "cli-engine.log").read_text()
    assert first_log.count("preflight WARN [shell]") == 1
    assert first_log.count("preflight WARN [machine-mixed]") == 1
    (root / "leader.lock").unlink()
    with cli_engine(path, force_unsupported_shell=True):
        pass
    restart_log = (short_root / "cli-engine.log").read_text()
    assert restart_log.count("preflight WARN [shell]") == 1
    assert restart_log.count("preflight WARN [machine-mixed]") == 1
    records = [body for _, kind, body in rows(root) if kind == "preflight"]
    assert len(records) == 2
    for record in records:
        codes = [item["code"] for item in record["items"]]
        assert codes.count("shell") == 1 and codes.count("machine-mixed") == 1


@pytest.mark.usefixtures("k_bourne_account")
def test_k2_cli_restart_uses_stored_profile_before_any_effect(short_root, monkeypatch):
    profile = short_root / "stored.profile"
    profile.write_text("export SYNTHETIC=applied\n")
    path = estate_file(short_root, engine=f'job_profile = "{profile}"\n')
    root, _ = k_seed(path)
    profile.unlink()
    config_file(
        short_root, engine='job_profile = "/unused/disk-edit"\njob_env = {NEW = "pending"}\n'
    )
    before = k_root_bytes(root)
    monkeypatch.setattr(
        runner_startup,
        "_first_reading",
        lambda *args: pytest.fail("recovery effects on K2 refusal"),
    )
    for _ in range(2):
        result = CliRunner().invoke(app, ["run", "--config", str(path)])
        assert result.exit_code == 2, result.output
        assert "restart-preflight" in result.output and "[profile]" in result.output
        assert "engine.job_profile" in result.output and "job A" in result.output
        assert k_root_bytes(root) == before


@pytest.mark.usefixtures("k_bourne_account")
def test_k2_pending_candidate_is_checked_before_update_recovery(short_root, monkeypatch):
    path = estate_file(short_root)
    applied = main.prepare(path)
    root = main.load_run_root(path)
    profile = short_root / "pending.profile"
    profile.write_text("export SYNTHETIC=candidate\n")
    engine = start_run(
        applied.catalog,
        root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter()},
        launched=main.profile_of(applied),
        source_capture=main.encoded_parts(applied),
    )
    try:
        (short_root / "a.jil").write_text(JOB + f"profile: {profile}\n")
        candidate = main.prepare(path)
        result = reread(engine.update.live(), root / "plans", candidate=candidate)
        request = UpdateRequest(result.token, 1, engine.baseline_id, "synthetic-pending")
        engine.journal.append(
            Record(
                engine.clock.now(),
                "config",
                None,
                request.request_id,
                {
                    "event": "intent",
                    "token": result.token,
                    "live_rev": 1,
                    "stop": [],
                    "filter": ["A"],
                    "stop_running": True,
                    "request_id": request.request_id,
                    "fingerprint": request.fingerprint,
                },
            )
        )
    finally:
        engine.journal.close()
    profile.unlink()
    before = k_root_bytes(root)
    monkeypatch.setattr(
        runner_startup,
        "_first_reading",
        lambda *args: pytest.fail("recovery effects before pending validation"),
    )
    for _ in range(2):
        with pytest.raises(
            EngineError, match="restart-preflight: configuration revision 2.*profile"
        ):
            asyncio.run(
                resume_run(
                    applied.catalog,
                    root,
                    clock=RealClock(),
                    adapters={"CMD": FakeAdapter()},
                    declared=main.profile_of(applied),
                    stored_sources=True,
                )
            )
        assert k_root_bytes(root) == before


@pytest.mark.usefixtures("k_bourne_account")
def test_k1_state_version_refuses_before_captured_lowering(short_root, monkeypatch):
    import sqlite3

    path = estate_file(short_root)
    root, _ = k_seed(path)
    with closing(sqlite3.connect(root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE meta SET value='-1' WHERE key='state_version'")
    before = k_root_bytes(root)
    monkeypatch.setattr(
        main, "decoded_parts", lambda *args: pytest.fail("lowering preceded state-version gate")
    )
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2 and "state version" in result.output
    assert "restart-lowering" not in result.output
    assert k_root_bytes(root) == before


@pytest.mark.parametrize("updated", [False, True])
@pytest.mark.parametrize("launch", ["cli", "legacy"])
def test_restart_accepts_current_kill_signals(
    short_root: Path, updated: bool, launch: str, owned_processes
) -> None:
    import json
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_config import RuntimeProfile
    from dsl41.runner_control import command, revision_in
    from test_engine_stops import _finish, _jil, _start_jobs, _until
    from test_kill_signals import signal_command

    cmd, ready, signals = signal_command(short_root)
    (short_root / "a.jil").write_text(_jil({"j": cmd}))
    path = config_file(short_root)
    estate = main.prepare(path)
    root = short_root / "run"

    async def seed():
        engine = start_run(
            estate.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(estate),
            source_capture=main.encoded_parts(estate),
        )
        try:
            engine.launch_settings = estate.metadata["launch"]
            if updated:
                config_file(short_root, engine="kill_signals = [15,9]\n")
                plan = reread(engine.update.live(), root / "plans", candidate=main.prepare(path))
                pending = engine.request_update(
                    UpdateRequest(plan.token, 1, engine.baseline_id, "policy")
                )
                await engine.run_until_quiescent(engine.clock.now())
                answer = pending.result()
                await answer.applied
                assert engine.profile.kill_signals == (15, 9)
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(seed())
    config_file(short_root, engine="kill_signals = [9]\n")
    status_file = root / "runs/j.1/1/status.json"
    if launch == "cli":
        with cli_engine(path):
            socket = root / "control.sock"
            for verb in ("STARTJOB", "KILLJOB"):
                read = roundtrip(socket, {"cmd": "status", "job": "j"})
                request = command(
                    verb,
                    {"job": "j"},
                    key="job:j",
                    revision=revision_in(read, "job:j"),
                    baseline_id=read["baseline_id"],
                )
                assert roundtrip(socket, request)["decision"] == "applied"
                deadline = time.monotonic() + 30
                marker = ready if verb == "STARTJOB" else status_file
                while not marker.exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
    else:

        async def resume():
            engine = await resume_run(
                estate.catalog,
                root,
                clock=RealClock(),
                adapters={"CMD": LocalCommandAdapter()},
                declared=RuntimeProfile(kill_signals=(9,)),
                hold_open=True,
            )
            loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
            try:
                await _until(engine.started.is_set)
                assert engine.profile.kill_signals == (9,)
                _start_jobs(engine, "j")
                await _until(ready.exists)
                engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
                await _until(status_file.exists)
            finally:
                await _finish(engine, loop)

        asyncio.run(resume())
    assert json.loads(status_file.read_text())["signal"] == 9 and not signals.exists()
    before = (root / "engine.db").read_bytes()
    config_file(short_root, engine="kill_signals = [2]\n")
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2 and "must end with KILL" in result.output
    assert (root / "engine.db").read_bytes() == before


def test_reload_uses_latest_launch_config_but_drift_uses_applied_capture(short_root: Path):
    path = estate_file(short_root)
    socket = short_root / "run" / "control.sock"
    with cli_engine(path):
        pass
    other = short_root / "other.toml"
    other.write_text(path.read_text().replace('["*.jil"]', '["b.jil"]'))
    (short_root / "b.jil").write_text(JOB.replace("true", "echo from-b"))
    with cli_engine(other) as ask:
        assert ask("status")["spec_drift"] is False
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "true"
        result = roundtrip(socket, {"v": 4, "cmd": "reread"})
        assert result["ok"] and result["source"]["path"] == str(other)
        assert result["preview"]["changed"] == [{"name": "A", "reason": None}]
    path.write_text("broken original config")
    with cli_engine(other) as ask:
        status = ask("status")
        assert status["spec_drift"] is True
        assert "reread" not in status["jobs"]["A"]
        assert roundtrip(socket, {"v": 4, "cmd": "reread"})["ok"]
