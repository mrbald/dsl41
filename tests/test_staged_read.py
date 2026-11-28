"""`update` applies what `reread` read (DL-569; BUG-126).

`reread` reads the files and the engine stages the read: one in memory, named by its plan
token. `update` with that name applies the read, not the files on disk then; `update` with no
name reads the files and applies them in one go. A restart, a later read of the disk and an
applied update drop the staged read; a refused apply keeps it. `status` says what is staged and
whether the disk has changed since, and `spec_drift` sees a new file under an existing glob.

The scenarios run a main-config engine in this process on the real clock, with real local
commands and its control server; the preparation worker runs in this process too
(`reload_fixtures.in_process_worker`), so the answers are the isolated worker's. The restart
scenario runs `dsl41 run` and the shell commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import in_process_worker
from test_main_config import T0, config_file
from test_main_config_restart import cli_engine
from test_prune_steps import _aged, _listed
from test_update_wiring import _finish
from typer.testing import CliRunner

from dsl41 import cli_reload
from dsl41 import main_config as main
from dsl41.cli import app
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlServer
from dsl41.runner_startup import start_run

GRACE = EngineSettings(grace_term_s=0.05, grace_kill_s=0.1)
#: a stop that takes its whole ladder: TERM is ignored, so KILL ends it after the grace
SLOW = EngineSettings(grace_term_s=2.0, grace_kill_s=1.0)
TIMEOUT_S = 30.0
STALE = "is not the staged read: another reread, an update or a restart replaced it"
ACTIVE = frozenset({"QUE_WAIT", "STARTING", "RUNNING"})


def _jil(**jobs: str) -> str:
    return "".join(
        f"insert_job: {name}\njob_type: c\ncommand: {command}\nmachine: localhost\n\n"
        for name, command in jobs.items()
    )


async def _until(predicate: Callable[[], object]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def _engine(
    directory: Path, settings: EngineSettings = GRACE
) -> AsyncIterator[tuple[Engine, ControlServer]]:
    """The engine `dsl41 run --config` starts on `directory`'s main config, its control
    server (disk compared at every `status`) and its loop."""
    config = directory / "dsl41.toml"
    initial = main.prepare(config)
    engine = start_run(
        initial.catalog,
        directory / "run",
        clock=RealClock(),
        adapters={"CMD": LocalCommandAdapter(grace_seconds=settings.grace_term_s)},
        hold_open=True,
        settings=settings,
        non_idempotent=initial.metadata["non_idempotent"],
        launched=main.profile_of(initial),
        source_capture=main.encoded_parts(initial),
    )
    engine.launch_settings = dict(initial.metadata["launch"])
    engine.reread_source = str(config)
    server = ControlServer(
        engine,
        directory / "run" / "control.sock",
        estate_fingerprint=main.fingerprint(initial),
        sources=main.source_baseline(initial),
    )
    server.DRIFT_CHECK_INTERVAL_S = 0  # type: ignore[misc]
    server._prepare_reload = in_process_worker(server)  # type: ignore[method-assign]
    loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    await engine.started.wait()
    try:
        yield engine, server
    finally:
        await _finish(engine, loop)


def _estate(directory: Path, **files: str) -> None:
    config_file(directory)
    for name, text in files.items():
        (directory / name).write_text(text)


async def _reread(server: ControlServer, actor: str = "alice", **fields: Any) -> dict[str, Any]:
    return await server._reread({"v": 5, "cmd": "reread", "claimed_actor": actor, **fields})


async def _update(
    server: ControlServer, rid: str, read: str | None = None, **fields: Any
) -> dict[str, Any]:
    request = {
        "v": 5,
        "cmd": "update",
        "baseline_id": server.engine.baseline_id,
        "request_id": rid,
        "wait": True,
        **({} if read is None else {"read": read}),
        **fields,
    }
    async with asyncio.timeout(TIMEOUT_S):
        return await server._update(request)


async def _start(server: ControlServer, job: str, running: str | None = None) -> None:
    """Force-start `job` and wait until the command `running` (`job` itself by default) runs."""
    engine = server.engine
    engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))
    command = running or job
    await _until(lambda: (engine.attempt_view(command) or {}).get("state") == "RUNNING")


def _command(engine: Engine, job: str) -> str | None:
    command = engine.oracle.catalog.jobs[job].exec_
    return None if command is None else command.command


def test_dl569_scenario_1_a_delivery_is_read_staged_and_applied(tmp_path: Path) -> None:
    """Files land and `status` shows it; `reread` stages them, with the marks of the time of
    the read; `update NAME` stops the running job and reruns it; then nothing is staged and
    the disk equals the running estate."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="sleep 30")})
        async with _engine(tmp_path) as (engine, server):
            await _start(server, "A")
            status = server._status({})
            assert status["spec_drift"] is False and "staged_read" not in status
            (tmp_path / "a.jil").write_text(_jil(A="sleep 31"))
            assert server._status({})["spec_drift"] is True
            read = await _reread(server)
            name = read["staged_read"]["name"]
            assert read["staged_read"]["actor"] == "alice"
            assert read["preview"]["stop"] == ["A"]
            # the public name is the plan token's first 12 digits; the plan keeps the token
            token = engine.update.staged.token
            assert len(name) == 12 and name == token[:12]
            assert [p.name for p in (tmp_path / "run" / "plans").iterdir()] == [token]
            status = server._status({})
            assert status["staged_read"] == {
                **read["staged_read"],
                "added": 0,
                "changed": 1,
                "removed": 0,
                "disk_changed": False,
            }
            assert status["jobs"]["A"]["reread"] == {"change": "changed", "action": "rerun"}
            answer = await _update(server, "apply", read=name)
            assert answer["ok"] and answer["stop"] == ["A"], answer
            assert answer["application"]["stopped"] == [["A", "rerun"]]
            await _until(lambda: (engine.attempt_view("A") or {}).get("n") == 2)
            assert engine.attempt_view("A")["after"] == "update_stop"
            status = server._status({})
            assert "staged_read" not in status and "reread" not in status["jobs"]["A"]
            assert status["spec_drift"] is False and status["config_rev"] == 2
            assert _command(engine, "A") == "sleep 31"

    asyncio.run(scenario())


def test_dl569_scenario_2_a_second_delivery_after_the_read_waits_for_the_next_read(
    tmp_path: Path,
) -> None:
    """`update NAME` applies the first read; the second delivery stays on disk, and `status`
    says the disk differs from the running estate."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo first"))
            name = (await _reread(server))["staged_read"]["name"]
            (tmp_path / "a.jil").write_text(_jil(A="echo second"))
            answer = await _update(server, "first", read=name)
            assert answer["application"]["changed"] == ["A"], answer
            assert _command(engine, "A") == "echo first"
            status = server._status({})
            assert status["spec_drift"] is True and "staged_read" not in status

    asyncio.run(scenario())


def test_dl569_scenario_3_a_read_between_two_files_of_one_delivery_sees_the_second_land(
    tmp_path: Path,
) -> None:
    """The read is taken after the first of two files was replaced; once the second lands,
    the staged block says the disk has changed since the read."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true"), "b.jil": _jil(B="true")})
        async with _engine(tmp_path) as (_, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            read = await _reread(server)
            assert read["preview"]["changed"] == [{"name": "A", "reason": None}]
            assert server._status({})["staged_read"]["disk_changed"] is False
            (tmp_path / "b.jil").write_text(_jil(B="echo new"))
            status = server._status({})
            assert status["staged_read"]["disk_changed"] is True
            assert status["staged_read"]["name"] == read["staged_read"]["name"]

    asyncio.run(scenario())


@pytest.mark.usefixtures("cli_env")
def test_dl569_scenario_4_a_restart_before_the_apply_forgets_the_read(short_root: Path) -> None:
    """Over `dsl41 run` and the shell: `reread` prints the name and the command; after a
    restart nothing is staged, the command is refused and says to read again, and `status`
    still says the disk differs."""
    from test_main_config import JOB, estate_file

    path = estate_file(short_root)
    sock = short_root / "run" / "control.sock"
    runner = CliRunner()
    with cli_engine(path):
        (short_root / "a.jil").write_text(JOB.replace("true", "echo new"))
        read = runner.invoke(app, ["reread", "-S", str(sock)])
        assert read.exit_code == 0, read.output
        name = re.search(r"staged read (\S+) by \S+ at ", read.output).group(1)
        assert f"apply with: dsl41 update -S {sock} {name}\n" in read.output
    with cli_engine(path) as ask:
        status = ask("status")
        assert "staged_read" not in status and status["spec_drift"] is True
        refused = runner.invoke(app, ["update", "-S", str(sock), name])
        assert refused.exit_code == 2, refused.output
        assert refused.stderr.splitlines()[-1] == (
            f"update: refused stale_plan: {name} {STALE}; run reread and look again"
        )
        assert ask()["revision"] == 1


def test_dl569_scenario_5_an_apply_refused_tree_active_keeps_the_read(tmp_path: Path) -> None:
    """A member is added under the running box: the read stages, `update NAME` is refused
    `tree_active` and the read stays; once the named jobs are stopped, the same `update NAME`
    applies."""
    box = "insert_job: BX\njob_type: b\n\n" + _jil(B="sleep 30").replace(
        "machine: localhost\n", "machine: localhost\nbox_name: BX\n"
    )

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": box})
        async with _engine(tmp_path) as (engine, server):
            await _start(server, "BX", running="B")
            added = _jil(NEW="true").rstrip("\n") + "\nbox_name: BX\n"
            (tmp_path / "a.jil").write_text(box + added)
            read = await _reread(server)
            name = read["staged_read"]["name"]
            assert read["preview"]["update_refusal"] == {
                "code": "tree_active",
                "names": ["B", "BX"],
            }
            refused = await _update(server, "busy", read=name)
            assert refused["code"] == "tree_active" and refused["names"] == ["B", "BX"]
            assert server._status({})["staged_read"]["name"] == name
            for job in ("B", "BX"):
                if engine.oracle.store.job[job].status in ACTIVE:
                    engine.inject(
                        Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": job})
                    )
                await _until(
                    lambda job=job: (
                        engine.oracle.store.job[job].status not in ACTIVE
                        and engine.attempt_view(job) is None
                    )
                )
            applied = await _update(server, "quiet", read=name)
            assert applied["application"]["added"] == ["NEW"], applied
            assert "NEW" in engine.oracle.catalog.jobs and engine.config_rev == 2

    asyncio.run(scenario())


def test_dl569_scenario_6_a_reread_of_broken_files_stages_nothing_and_drops_the_read(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (_, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            name = (await _reread(server))["staged_read"]["name"]
            (tmp_path / "a.jil").write_text("broken jil")
            broken = await _reread(server)
            assert not broken["ok"] and broken["code"] == "invalid_argument"
            assert "staged_read" not in server._status({})
            refused = await _update(server, "old", read=name)
            assert refused["code"] == "stale_plan" and refused["refused"]
            assert refused["error"] == f"{name} {STALE}; run reread and look again"

    asyncio.run(scenario())


def test_dl569_scenario_7_changed_host_launch_settings_stage_nothing(tmp_path: Path) -> None:
    """A changed host launch setting needs a restart: the read says so and stages nothing,
    and the read staged before it is gone."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (_, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            assert (await _reread(server))["staged_read"] is not None
            config_file(tmp_path, engine='machine_policy = "local-eligible"\n')
            read = await _reread(server)
            assert read["ok"] and read["staged_read"] is None
            assert read["preview"]["definition_changes"]["blocked"] == [
                "engine host launch settings changed: machine_policy; restart the engine to"
                " apply them before update"
            ]
            assert "staged_read" not in server._status({})

    asyncio.run(scenario())


def test_dl569_scenario_8_update_reread_replaces_a_waiting_read_and_applies_the_disk(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo first"))
            name = (await _reread(server))["staged_read"]["name"]
            (tmp_path / "a.jil").write_text(_jil(A="echo second"))
            answer = await _update(server, "disk")
            assert answer["application"]["changed"] == ["A"], answer
            assert _command(engine, "A") == "echo second"
            assert "staged_read" not in server._status({})
            refused = await _update(server, "waiting", read=name)
            assert refused["code"] == "stale_plan"

    asyncio.run(scenario())


def test_dl569_scenario_9_two_operators_apply_only_the_read_they_looked_at(tmp_path: Path) -> None:
    """A reads, B reads other bytes: A's `update NAME` is refused. B reads A's bytes again:
    the same bytes at the same revision are the same read, and A's apply goes through."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo first"))
            looked = (await _reread(server, "alice"))["staged_read"]["name"]
            (tmp_path / "a.jil").write_text(_jil(A="echo other"))
            other = (await _reread(server, "bob"))["staged_read"]
            assert other["name"] != looked and other["actor"] == "bob"
            refused = await _update(server, "alice-1", read=looked)
            assert (
                refused["code"] == "stale_plan"
                and "run reread and look again" in (refused["error"])
            )
            (tmp_path / "a.jil").write_text(_jil(A="echo first"))
            again = (await _reread(server, "bob"))["staged_read"]
            assert again["name"] == looked
            answer = await _update(server, "alice-2", read=looked)
            assert answer["application"]["changed"] == ["A"], answer
            assert _command(engine, "A") == "echo first"

    asyncio.run(scenario())


def test_dl569_scenario_10_a_profile_removed_after_the_read_refuses_before_any_stop(
    tmp_path: Path,
) -> None:
    """The apply checks the read against the host now: a profile file the changed job needs
    is gone, so the update is refused before it stops anything, and the read stays."""
    profile = tmp_path / "profile.sh"
    profile.write_text("")
    job = _jil(A="sleep 30").replace(
        "machine: localhost\n", f"machine: localhost\nprofile: {profile}\n"
    )

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": job})
        async with _engine(tmp_path) as (engine, server):
            await _start(server, "A")
            (tmp_path / "a.jil").write_text(job.replace("sleep 30", "sleep 31"))
            name = (await _reread(server))["staged_read"]["name"]
            profile.unlink()
            refused = await _update(server, "profile", read=name)
            assert refused["code"] == "invalid_argument" and refused["refused"]
            assert refused["error"].startswith("preflight ERROR [profile] A: ")
            assert engine.attempt_view("A")["n"] == 1
            assert engine.attempt_view("A")["state"] == "RUNNING"
            assert engine.config_rev == 1 and not engine.update.stopping
            assert server._status({})["staged_read"]["name"] == name

    asyncio.run(scenario())


def test_dl569_scenario_11_bug126_a_new_or_removed_file_under_a_glob_is_drift(
    tmp_path: Path,
) -> None:
    """BUG-126: `spec_drift` re-hashed only the files the engine loaded, so a new file an
    existing include glob matched was not seen. Disk against applied now follows the rule a
    restart's pending count uses."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true"), "b.jil": _jil(B="true")})
        async with _engine(tmp_path) as (_, server):
            assert server._status({})["spec_drift"] is False
            (tmp_path / "c.jil").write_text(_jil(C="true"))
            assert server._status({})["spec_drift"] is True
            (tmp_path / "c.jil").unlink()
            assert server._status({})["spec_drift"] is False
            (tmp_path / "b.jil").unlink()
            assert server._status({})["spec_drift"] is True

    asyncio.run(scenario())


def test_dl569_scenario_12_a_plain_update_names_the_two_forms(monkeypatch) -> None:
    def contacted(*args: object, **kwargs: object) -> object:
        raise AssertionError("contacted the engine")

    monkeypatch.setattr(cli_reload, "socket_send", contacted)
    plain = CliRunner().invoke(app, ["update", "-S", "/synthetic/control.sock"])
    assert plain.exit_code == 2 and plain.stdout == ""
    assert plain.stderr == cli_reload.UPDATE_FORMS + "\n"
    assert "dsl41 update NAME" in plain.stderr and "dsl41 update --reread" in plain.stderr
    both = CliRunner().invoke(app, ["update", "-S", "/s", "--reread", "c" * 12])
    assert both.exit_code == 2 and both.stderr == "update: give NAME or --reread, not both\n"
    # the literal command names the forms whether or not a socket is set
    for socket in (None, "/environment/control.sock"):
        literal = CliRunner().invoke(app, ["update"], env={"DSL41_SOCKET": socket})
        assert literal.exit_code == 2 and literal.stderr == cli_reload.UPDATE_FORMS + "\n"
    missing = CliRunner().invoke(app, ["update", "--reread"], env={"DSL41_SOCKET": None})
    assert missing.exit_code == 2
    assert missing.stderr == "update: Missing option '--socket' / '-S', or set DSL41_SOCKET\n"


def test_dl569_scenario_13_one_preparation_at_a_time(tmp_path: Path) -> None:
    """A second `reread` and an `update` while a reread runs are refused and told to wait;
    the first read stages as it would alone."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (_, server):
            worker, entered, release = server._prepare_reload, asyncio.Event(), asyncio.Event()

            async def held(**kwargs: Any) -> dict[str, Any]:
                entered.set()
                await release.wait()
                return await worker(**kwargs)

            server._prepare_reload = held  # type: ignore[method-assign]
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            first = asyncio.create_task(_reread(server))
            async with asyncio.timeout(TIMEOUT_S):
                await entered.wait()
            second = await _reread(server, "bob")
            assert second["code"] == "update_in_progress" and second["refused"]
            assert second["error"] == "another reread or update is preparing; wait for it to finish"
            update = await _update(server, "meanwhile")
            assert update["code"] == "update_in_progress" and update["names"] == []
            release.set()
            assert (await first)["staged_read"]["actor"] == "alice"

    asyncio.run(scenario())


@pytest.mark.parametrize("busy", ["stopping", "draining"])
def test_dl569_scenario_13_a_reread_while_an_update_stops_or_the_engine_drains(
    tmp_path: Path, busy: str
) -> None:
    """The read is shown with the reason no update would take it now, and nothing is staged.
    The job's stop ignores TERM, so its ladder holds the update or the drain open."""
    slow = "trap '' TERM; sleep 30 & wait"

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A=slow)})
        async with _engine(tmp_path, SLOW) as (engine, server):
            await _start(server, "A")
            (tmp_path / "a.jil").write_text(_jil(A="sleep 31"))
            plans = tmp_path / "run" / "plans"
            if busy == "stopping":
                name = (await _reread(server))["staged_read"]["name"]
                token = engine.update.staged.token
                admitted = await _update(server, "stops", read=name, wait=False)
                assert admitted["stop"] == ["A"] and engine.update.stopping
                note = "another update is open; wait for it to finish"
            else:
                token = ""
                stop = engine.request_stop()
                await _until(lambda: engine.draining)
                note = "the engine is draining; it takes no update until it restarts"
            # the same files give the open update's token: the reread must write no plan
            listed = sorted(os.listdir(plans)) if plans.exists() else []
            before = (plans / token / "plan.json").read_bytes() if token else b""
            read = await _reread(server)
            assert (sorted(os.listdir(plans)) if plans.exists() else []) == listed
            assert ((plans / token / "plan.json").read_bytes() if token else b"") == before
            assert read["ok"] and read["staged_read"] is None
            assert note in read["preview"]["definition_changes"]["blocked"]
            assert read["preview"]["update_refusal"] is None
            assert "staged_read" not in server._status({})
            if busy == "stopping":
                await _until(lambda: not engine.update.stopping)
                assert engine.config_rev == 2
            else:
                await stop

    asyncio.run(scenario())


def test_dl569_scenario_14_an_exact_retry_after_a_lost_reply_is_answered_from_the_store(
    tmp_path: Path,
) -> None:
    """The reply to `update NAME` was lost and the read is gone with the application: the
    exact retry is answered with the same application. The same id with another read, or
    with none, is a reuse."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (_, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            name = (await _reread(server))["staged_read"]["name"]
            lost = await _update(server, "lost", read=name)
            assert lost["application"]["rev"] == 2
            retry = await _update(server, "lost", read=name, retry_only=True)
            assert retry["application"] == lost["application"]
            for other in ({"read": "d" * 64}, {}):
                reused = await _update(server, "lost", **other)
                assert reused["code"] == "request_id_reused", reused

    asyncio.run(scenario())


def test_dl569_scenario_15_retention_keeps_the_staged_plan_and_runs_under_no_reread(
    tmp_path: Path,
) -> None:
    """A round removes an old plan and keeps the staged one, however old; while a reread
    prepares, a round removes no plan."""
    other = "0" * 64

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        config = tmp_path / "dsl41.toml"
        initial = main.prepare(config)
        engine = start_run(
            initial.catalog,
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = dict(initial.metadata["launch"])
        engine.reread_source = str(config)
        server = ControlServer(engine, tmp_path / "run" / "control.sock")
        worker = in_process_worker(server)
        server._prepare_reload = worker  # type: ignore[method-assign]
        plans = tmp_path / "run" / "plans"
        old = T0 - timedelta(days=3)

        def plan(token: str) -> None:
            (plans / token).mkdir(parents=True, exist_ok=True)
            (plans / token / "plan.json").touch()
            _aged(plans / token / "plan.json", old)

        async def round() -> None:
            engine.take_snapshot()
            await _listed(engine)
            await engine.run_until_quiescent(engine.clock.now())

        try:
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            await _reread(server)
            token = engine.update.staged.token
            _aged(plans / token / "plan.json", old)
            plan(other)
            await round()
            assert sorted(os.listdir(plans)) == [token]
            entered, release = asyncio.Event(), asyncio.Event()

            async def held(**kwargs: Any) -> dict[str, Any]:
                entered.set()
                await release.wait()
                return await worker(**kwargs)

            server._prepare_reload = held  # type: ignore[method-assign]
            clock = engine.clock
            assert isinstance(clock, VirtualClock)
            clock._now = T0 + timedelta(days=1)
            engine.inject(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "G", "value": "v"})
            )
            await engine.run_until_quiescent(clock.now())
            (tmp_path / "a.jil").write_text(_jil(A="echo newer"))
            reading = asyncio.create_task(_reread(server))
            async with asyncio.timeout(TIMEOUT_S):
                await entered.wait()
            plan(other)
            await round()
            assert other in os.listdir(plans)
            release.set()
            newer = (await reading)["staged_read"]["name"]
            assert engine.update.staged.token != token and newer == engine.update.staged.name
            assert engine.update.staged.token in os.listdir(plans)
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


def test_dl569_a_reread_with_the_read_previews_it_again_against_the_engine_now(
    tmp_path: Path,
) -> None:
    """The terminal UI's confirmation: `reread` with `read` previews the staged read against
    the engine's state now, reads no file and changes nothing staged; the row marks keep the
    time of the read. A name that is not the staged read is refused."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="sleep 30")})
        async with _engine(tmp_path) as (_, server):
            (tmp_path / "a.jil").write_text(_jil(A="sleep 31"))
            read = await _reread(server)
            name = read["staged_read"]["name"]
            assert read["preview"]["stop"] == []
            await _start(server, "A")
            (tmp_path / "a.jil").write_text("broken jil")
            again = await _reread(server, "bob", read=name)
            assert again["ok"] and again["preview"]["stop"] == ["A"]
            assert again["preview"]["changed"] == [{"name": "A", "reason": None}]
            assert again["staged_read"] == read["staged_read"]
            assert "marks" not in again["preview"]
            status = server._status({})
            assert status["staged_read"]["name"] == name
            assert status["jobs"]["A"]["reread"] == {"change": "changed", "action": None}
            stale = await _reread(server, read="d" * 64)
            assert stale["code"] == "stale_plan" and stale["refused"]
            for bad in ({"read": 7}, {"token": name}):
                invalid = await server._reread({"v": 5, "cmd": "reread", **bad})
                assert invalid["code"] == "invalid_argument"
            invalid = await _update(server, "bad", read=7)
            assert invalid["code"] == "invalid_argument"

    asyncio.run(scenario())


def test_dl569_only_the_staged_name_itself_selects_the_read(tmp_path: Path) -> None:
    """The name is compared for equality: the whole plan token, or a shorter prefix of the
    name, is refused `stale_plan` by `reread` and by `update`, and the read stays staged."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            name = (await _reread(server))["staged_read"]["name"]
            staged = engine.update.staged
            for read in (staged.token, name[:11], name[:6]):
                again = await _reread(server, read=read)
                assert again["code"] == "stale_plan" and again["refused"], again
                refused = await _update(server, f"rid-{len(read)}", read=read)
                assert refused["code"] == "stale_plan", refused
                assert refused["error"] == f"{read} {STALE}; run reread and look again"
            assert engine.update.staged == staged and engine.config_rev == 1
            applied = await _update(server, "exact", read=name)
            assert applied["application"]["changed"] == ["A"], applied

    asyncio.run(scenario())


def test_dl569_the_shell_sends_the_read_and_names_it_in_the_retry_command(monkeypatch) -> None:
    """`dsl41 update NAME` sends the read; an answer that does not confirm the application
    prints the retry command in the same form."""
    from dsl41 import runner_control

    name = "c" * 12
    sent: list[dict[str, Any]] = []

    def roundtrip(path: Path, request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append(request)
        if request["cmd"] == "status":
            return {"ok": True, "baseline_id": "b", "update_window_s": 1}
        before = kwargs.get("before_send")
        if before is not None:
            before()
        return {"ok": True, "decision": "admitted", "request_id": "r", "stop": [], "pending": True}

    monkeypatch.setattr(runner_control, "roundtrip", roundtrip)
    result = CliRunner().invoke(app, ["update", "-S", "/s", name, "--wait"])
    assert result.exit_code == 1, result.output
    assert sent[-1]["read"] == name and sent[-1]["wait"] is True
    assert f"check with: dsl41 update -S /s {name} --request-id r --wait" in result.stdout
    result = CliRunner().invoke(app, ["update", "-S", "/s", "--reread"])
    assert "read" not in sent[-1]
    assert "check with: dsl41 update -S /s --reread --request-id r --wait" in result.stdout


def test_dl569_a_drain_that_begins_while_a_reread_prepares_stages_nothing(tmp_path: Path) -> None:
    """The worker was asked to stage and stored its plan, but the engine began to drain
    meanwhile: the read is not staged, and the answer says why."""
    from dsl41.reread_worker import answer

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            entered, release = asyncio.Event(), asyncio.Event()

            async def held(*, update: bool, plan: str | None = None) -> dict[str, Any]:
                payload = server._reload_payload(update=update, plan=plan)
                entered.set()
                await release.wait()
                return json.loads(json.dumps(answer(payload)))

            server._prepare_reload = held  # type: ignore[method-assign]
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            reading = asyncio.create_task(_reread(server))
            async with asyncio.timeout(TIMEOUT_S):
                await entered.wait()
            engine.request_stop()
            await _until(lambda: engine.draining)
            release.set()
            read = await reading
            assert read["ok"] and read["staged_read"] is None
            assert engine.update.staged is None
            assert (
                "the engine is draining; it takes no update until it restarts"
                in read["preview"]["definition_changes"]["blocked"]
            )

    asyncio.run(scenario())


def test_dl569_a_worker_that_ignores_the_plan_is_refused_before_admission(tmp_path: Path) -> None:
    """A worker of another build ignores `plan` and reads the disk. The staged read holds
    bytes A, the disk holds bytes B: `update NAME` and `reread` with `read` are refused with
    the cause, nothing is admitted, the revision does not move and the read stays."""
    from dsl41.reread_worker import answer

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            (tmp_path / "a.jil").write_text(_jil(A="echo reviewed"))
            name = (await _reread(server))["staged_read"]["name"]
            staged = engine.update.staged
            (tmp_path / "a.jil").write_text(_jil(A="echo later"))

            async def foreign(*, update: bool, plan: str | None = None) -> dict[str, Any]:
                payload = server._reload_payload(update=update, plan=plan)
                del payload["plan"]
                reply = json.loads(json.dumps(answer({**payload, "plan": None})))
                return reply if reply["ok"] else {**reply, "refused": True}

            server._prepare_reload = foreign  # type: ignore[method-assign]
            cause = "the preparation worker did not read the staged plan: the worker is not this"
            refused = await _update(server, "foreign", read=name)
            assert refused["code"] == "engine_error" and refused["refused"], refused
            assert refused["error"].startswith(cause)
            again = await _reread(server, read=name)
            assert again["code"] == "engine_error" and again["error"].startswith(cause)
            assert engine.config_rev == 1 and not engine.update.stopping
            assert _command(engine, "A") == "true" and engine.update.staged == staged

    asyncio.run(scenario())


def test_dl569_an_equal_answer_that_removes_the_staged_plan_drops_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker compiles the unchanged files differently from the engine (a changed
    lowering), so the read stages; the admission finds it equal to the applied configuration
    and removes its plan. The staged read goes with it, and its name is refused after."""
    from dsl41.reread_worker import answer

    lower = main.lower_catalog

    def changed(*args: Any, **kwargs: Any) -> Any:
        catalog = lower(*args, **kwargs)
        job = catalog.jobs["A"]
        command = job.exec_.model_copy(update={"command": "echo lowered"})
        return catalog.model_copy(update={"jobs": {"A": job.model_copy(update={"exec_": command})}})

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):

            async def worker(*, update: bool, plan: str | None = None) -> dict[str, Any]:
                payload = server._reload_payload(update=update, plan=plan)
                with monkeypatch.context() as patch:
                    patch.setattr(main, "lower_catalog", changed)
                    return json.loads(json.dumps(answer(payload)))

            server._prepare_reload = worker  # type: ignore[method-assign]
            name = (await _reread(server))["staged_read"]["name"]
            token = engine.update.staged.token
            equal = await _update(server, "equal", read=name)
            assert equal["unchanged"]["rev"] == 1, equal
            assert not (tmp_path / "run" / "plans" / token).exists()
            assert "staged_read" not in server._status({})
            assert (await _update(server, "again", read=name))["code"] == "stale_plan"

    asyncio.run(scenario())


@pytest.mark.usefixtures("cli_env")
def test_dl569_a_jil_only_restart_that_finishes_an_update_keeps_the_fingerprint_drift(
    short_root: Path,
) -> None:
    """An update is interrupted while it stops its jobs; an unrelated JIL file lands beside the
    estate; the engine restarts on one explicit JIL file and finishes the update. That engine
    has no main config to reread, so `spec_drift` stays the check of the files it loaded."""
    import subprocess
    import sys

    from test_main_config import estate_file
    from test_main_config_restart import seed_root

    from dsl41.runner_control import roundtrip

    path = estate_file(short_root)
    seed_root(path, stopping=True, boundary=False)
    (short_root / "unrelated.jil").write_text(_jil(U="true"))
    root = short_root / "run"
    log = short_root / "legacy.log"
    with log.open("w") as stream:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "dsl41",
                "run",
                str(short_root / "a.jil"),
                "--run-root",
                str(root),
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + TIMEOUT_S
        status: dict[str, Any] = {}
        while status.get("config_rev") != 2:
            assert child.poll() is None, log.read_text()
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
            if "engine up; control socket:" in log.read_text():
                status = roundtrip(root / "control.sock", {"v": 5, "cmd": "status"}, timeout=3)
        assert status["spec_drift"] is False, status
    finally:
        child.terminate()
        child.wait(timeout=15)


def test_dl569_an_answered_request_frees_only_its_own_slot(tmp_path: Path) -> None:
    """Request A is refused in its preparation and freed the slot; request B took it before A's
    answer callback ran. That callback leaves B's slot alone: an exact `retry_only` of B joins
    B's preparation and gets B's answer, and another id is refused while B prepares."""

    async def scenario() -> None:
        _estate(tmp_path, **{"a.jil": _jil(A="true")})
        async with _engine(tmp_path) as (engine, server):
            worker = server._prepare_reload
            gate_a, entered_b, gate_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls: list[int] = []

            async def held(**kwargs: Any) -> dict[str, Any]:
                calls.append(len(calls))
                if len(calls) == 1:
                    await gate_a.wait()
                    return {"ok": False, "code": "invalid_argument", "error": "A", "refused": True}
                entered_b.set()
                await gate_b.wait()
                return await worker(**kwargs)

            server._prepare_reload = held  # type: ignore[method-assign]
            (tmp_path / "a.jil").write_text(_jil(A="echo new"))
            first = asyncio.create_task(_update(server, "a"))
            async with asyncio.timeout(TIMEOUT_S):
                while server._preparing_update is None:
                    await asyncio.sleep(0)
            # A's step runs first and is answered; B's step runs before A's done-callback
            gate_a.set()
            second = asyncio.create_task(_update(server, "b"))
            async with asyncio.timeout(TIMEOUT_S):
                await entered_b.wait()
            assert (await first)["code"] == "invalid_argument"
            await asyncio.sleep(0)  # any callback A queued has run
            assert server._preparing_update is not None
            assert server._preparing_update[0] == "b"
            other = await _update(server, "c")
            assert other["code"] == "update_in_progress"
            retry = asyncio.create_task(_update(server, "b", retry_only=True))
            await asyncio.sleep(0)
            gate_b.set()
            answer = await second
            assert answer["application"]["rev"] == 2, answer
            assert await retry == answer
            assert engine.config_rev == 2 and len(calls) == 2

    asyncio.run(scenario())
