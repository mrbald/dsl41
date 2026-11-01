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
from test_main_config import JOB, config_file, estate_file

pytestmark = pytest.mark.usefixtures("cli_env")


@contextmanager
def cli_engine(path: Path):
    root = main.load_run_root(path)
    log = root.parent / "cli-engine.log"
    with (
        log.open("w") as stream,
        subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=stream,
            stderr=subprocess.STDOUT,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 30
            while "engine up; control socket:" not in log.read_text():
                assert child.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)

            def ask(command="catalog"):
                return roundtrip(root / "control.sock", {"v": 3, "cmd": command}, timeout=3)

            assert ask("status")["engine"] == "RUNNING"
            yield ask
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
            assert child.returncode == 0, log.read_text()


def read_config_row(root: Path, rev: int) -> bytes:
    with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
        return store.get_config(rev)


def plan_for(path: Path, ask):
    root = main.load_run_root(path)
    return reread(path.parent, live_of_answer(ask()), root / "plans", candidate=main.prepare(path))


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
        token = plan_for(path, ask).token
        result = CliRunner().invoke(app, ["update", token, "--config", str(path), "--wait"])
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
            result = reread(path.parent, engine.update.live(), root / "plans", candidate=candidate)
            if stopping:
                request = UpdateRequest(
                    result.token, True, 1, engine.baseline_id, "review-recovery"
                )
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


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_b3_plan_refusal_precedes_engine_journal_and_signals(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    path = estate_file(short_root)
    root = short_root / "run"
    _, plan = seed_root(path, stopping=True, boundary=False)
    before = rows(root)
    if damage == "missing":
        shutil.rmtree(plan.plan_dir)
    else:
        plan.plan_json.write_bytes(b"invalid JSON")
    effects = []

    def forbidden(*args, **kwargs):
        effects.append("runtime effect")
        pytest.fail("recovery effects began before the plan refusal")

    monkeypatch.setattr(runner_startup, "Engine", forbidden)
    monkeypatch.setattr(runner_startup, "_first_reading", forbidden)
    import dsl41.runner_procid

    monkeypatch.setattr(dsl41.runner_procid, "signal_ours", forbidden)
    stored = active_configuration(root)
    with pytest.raises(EngineError, match="plan cannot be read"):
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
    assert effects == [] and rows(root) == before


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
        result = CliRunner().invoke(app, ["reread", "--config", str(path)])
        assert (
            result.exit_code == 2 and "as_machine" in result.output and "restart" in result.output
        )
    row = read_config_row(short_root / "run", 1)
    with cli_engine(path) as ask:
        assert ask()["launch"]["as_machine"] == ["after"]
        result = CliRunner().invoke(app, ["reread", "--config", str(path)])
        assert result.exit_code == 0, result.output
    assert read_config_row(short_root / "run", 1) == row


@pytest.mark.usefixtures("cli_env")
def test_k6_update_reads_only_the_engine_location(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(short_root)
    with cli_engine(path) as ask:
        (short_root / "a.jil").write_text(JOB.replace("true", "echo reviewed"))
        token = plan_for(path, ask).token
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
        result = CliRunner().invoke(app, ["update", token, "--config", str(path), "--wait"])
        assert result.exit_code == 0, result.output
        assert ask()["revision"] == 2
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "echo reviewed"
