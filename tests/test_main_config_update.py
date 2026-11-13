"""Captured main-config plans through the unchanged update machine (DL-432, DL-437)."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing
import json
from datetime import timedelta
from pathlib import Path

import pytest

from dsl41 import main_config as main, runner_update
from dsl41.canon import canonical_bytes
from dsl41.oracle_state import Event
from dsl41.reload_diff import definition_hash
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted, Refused
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_config import Configuration, RuntimeProfile
from dsl41.runner_journal import replay_store
from dsl41.runner_startup import active_configuration, resume_run, start_run
from dsl41.runner_store import MemoryStore, Record, SqliteStore
from dsl41.runner_update import UpdateRequest
from test_main_config import JOB, PYTHON, T0, config_file, estate_file


@pytest.mark.usefixtures("cli_env")
@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("cleanup", ["ok", "missing", "permission"])
def test_bug57_unchanged_update_cli(tmp_path, monkeypatch, wait, cleanup):
    from dataclasses import asdict
    from typer.testing import CliRunner
    from dsl41.cli import app
    from dsl41 import runner_control
    from dsl41.reread_worker import run
    from dsl41.runner_control import ControlServer

    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        server = ControlServer(engine, root / "control.sock")
        submitted = asyncio.Event()
        request_update = engine.request_update
        remove = runner_update.shutil.rmtree

        async def prepare(*, update):
            live = engine.update.live()
            return run(
                {
                    "source": str(path),
                    "run_root": str(root),
                    "update": update,
                    "live": {**asdict(live), "catalog": live.catalog.model_dump(mode="json")},
                }
            )

        def submit(request):
            answer = request_update(request)
            submitted.set()
            return answer

        def remove_plan(directory):
            assert server._preparing_update is not None
            if cleanup == "missing":
                remove(directory)
            if cleanup == "permission":
                raise PermissionError("synthetic cleanup refusal")
            remove(directory)

        monkeypatch.setattr(server, "_prepare_reload", prepare)
        monkeypatch.setattr(engine, "request_update", submit)
        monkeypatch.setattr(runner_update.shutil, "rmtree", remove_plan)
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            before = list(engine.journal.store.read_from(0))
            config = engine.journal.store.get_config(1)
            for n in range(2):
                submitted.clear()
                request = {
                    "v": 5,
                    "cmd": "update",
                    "wait": wait,
                    "baseline_id": engine.baseline_id,
                    "request_id": f"unchanged-{n}",
                }
                task = asyncio.create_task(server._update(request))
                await submitted.wait()
                await engine.run_until_quiescent(T0)
                answer = await task
                assert answer == {
                    "ok": True,
                    "decision": "admitted",
                    "request_id": request["request_id"],
                    "stop": [],
                    "application": None,
                    "unchanged": {
                        "rev": 1,
                        "catalog_hash": active_configuration(root).catalog_hash,
                    },
                }
                assert engine.config_rev == engine.journal.store.current_config_rev() == 1
                assert engine.journal.store.get_config(1) == config
                assert list(engine.journal.store.read_from(0)) == before
                assert len(list((root / "plans").iterdir())) == (cleanup == "permission")
                assert engine.oracle.store.job["A"].status == "RUNNING"
                assert server._preparing_update is None
                assert (await server._dispatch({"cmd": "status"}))["engine"] == "RUNNING"

                def roundtrip(socket, wire, *, before_send=None, **kwargs):
                    if before_send:
                        before_send()
                    if wire["cmd"] == "status":
                        return {"ok": True, "baseline_id": engine.baseline_id}
                    assert "v" not in wire and wire["wait"] is wait
                    return {**answer, "request_id": wire["request_id"]}

                monkeypatch.setattr(runner_control, "roundtrip", roundtrip)
                result = CliRunner().invoke(
                    app, ["update", "-S", str(server.path)] + (["--wait"] if wait else [])
                )
                assert result.exit_code == 0, result.output
                assert result.stdout == (
                    f"nothing changed: catalog revision 1 ({answer['unchanged']['catalog_hash']})\n"
                )
                assert result.stderr.startswith("request_id: ")
                retry = await server._update({**request, "retry_only": True})
                assert retry["code"] == "invalid_argument" and "not retained" in retry["error"]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_bug57_unchanged_update_preserves_marks_until_next_reread(tmp_path, monkeypatch):
    from dataclasses import asdict
    from dsl41.reread_worker import run
    from dsl41.runner_control import ControlServer

    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        server = ControlServer(engine, root / "control.sock")

        async def prepare(*, update):
            live = engine.update.live()
            return run(
                {
                    "source": str(path),
                    "run_root": str(root),
                    "update": update,
                    "live": {**asdict(live), "catalog": live.catalog.model_dump(mode="json")},
                }
            )

        monkeypatch.setattr(server, "_prepare_reload", prepare)
        try:
            await engine.run_until_quiescent(T0)
            (tmp_path / "a.jil").write_text(JOB.replace("true", "echo changed"))
            assert (await server._reread({"v": 5, "cmd": "reread"}))["ok"]
            assert server._status({})["jobs"]["A"]["reread"]["change"] == "changed"
            marks = dict(engine.reread_marks)
            (tmp_path / "a.jil").write_text(JOB)
            plan = reread(
                engine.update.live(),
                root / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            pending = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "revert")
            )
            await engine.run_until_quiescent(T0)
            assert pending.result().unchanged is not None
            assert engine.reread_marks == marks
            assert server._status({})["jobs"]["A"]["reread"] == marks["A"]
            assert (await server._reread({"v": 5, "cmd": "reread"}))["ok"]
            assert engine.reread_marks == {}
            assert "reread" not in server._status({})["jobs"]["A"]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("after", ["application", "restart"])
def test_bug57_equal_update_after_application_or_restart(tmp_path, after):
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        try:
            if after == "application":
                (tmp_path / "a.jil").write_text(JOB.replace("true", "echo changed"))
                plan = reread(
                    engine.update.live(),
                    root / "plans",
                    candidate=main.prepare(path),
                    start=T0,
                )
                pending = engine.request_update(
                    UpdateRequest(plan.token, 1, engine.baseline_id, "changed")
                )
                await engine.run_until_quiescent(T0)
                assert pending.result().applied.result()["rev"] == 2
            else:
                await engine.shutdown()
                engine.journal.close()
                engine = await resume_run(
                    initial.catalog,
                    root,
                    clock=VirtualClock(start=T0),
                    adapters={"CMD": FakeAdapter()},
                    declared=main.profile_of(initial),
                    stored_sources=True,
                )
                await engine.run_until_quiescent(T0)
                engine.launch_settings = initial.metadata["launch"]
            rev = engine.config_rev
            assert rev == (2 if after == "application" else 1)
            before = list(engine.journal.store.read_from(0))
            config = engine.journal.store.get_config(rev)
            plan = reread(
                engine.update.live(),
                root / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            pending = engine.request_update(
                UpdateRequest(plan.token, rev, engine.baseline_id, "equal")
            )
            await engine.run_until_quiescent(T0)
            assert pending.result().unchanged == {
                "rev": rev,
                "catalog_hash": active_configuration(root).catalog_hash,
            }
            assert engine.config_rev == engine.journal.store.current_config_rev() == rev
            assert engine.journal.store.get_config(rev) == config
            assert list(engine.journal.store.read_from(0)) == before
            assert not plan.plan_dir.exists()
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "edit", ["comment", "whitespace", "job_env", "kill_signals", "interval", "lowering"]
)
def test_bug57_edits_still_apply(tmp_path, monkeypatch, edit):
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        try:
            if edit in ("comment", "whitespace"):
                (tmp_path / "a.jil").write_text(
                    ("# synthetic comment\n" if edit == "comment" else "\n") + JOB
                )
            elif edit == "lowering":
                lower = main.lower_catalog

                def changed(*args, **kwargs):
                    catalog = lower(*args, **kwargs)
                    job = catalog.jobs["A"]
                    return catalog.model_copy(
                        update={
                            "jobs": {
                                "A": job.model_copy(
                                    update={
                                        "exec_": job.exec_.model_copy(
                                            update={"command": "echo lowered"}
                                        ),
                                    }
                                )
                            }
                        }
                    )

                monkeypatch.setattr(main, "lower_catalog", changed)
            else:
                config_file(
                    tmp_path,
                    engine={
                        "job_env": '[engine.job_env]\nVALUE = "changed"\n',
                        "kill_signals": "kill_signals = [9]\n",
                        "interval": "fw_default_interval_s = 17\n",
                    }[edit],
                )
            candidate = main.prepare(path)
            if edit == "lowering":
                assert main.encoded_parts(candidate) == main.encoded_parts(initial)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            assert plan.stop == ()
            assert bool(plan.plan.changed) is (edit == "lowering")
            pending = engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, edit))
            await engine.run_until_quiescent(T0)
            answer = pending.result()
            assert answer.unchanged is None and answer.applied.result()["rev"] == 2
            stored = active_configuration(root)
            assert stored.profile == main.profile_of(candidate)
            assert stored.source_capture == main.encoded_parts(candidate)
            if edit == "lowering":
                assert stored.catalog.jobs["A"].exec_.command == "echo lowered"
            assert main.pending_source_changes(candidate) == 0
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_bug57_kill_signals_restart_revert(tmp_path):
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        await engine.shutdown()
        engine.journal.close()
        original = path.read_bytes()
        config_file(tmp_path, engine="kill_signals = [9]\n")
        edited = main.prepare(path)
        engine = await resume_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            declared=main.profile_of(edited),
            stored_sources=True,
        )
        try:
            await engine.run_until_quiescent(T0)
            assert engine.profile.kill_signals == (9,)
            path.write_bytes(original)
            candidate = main.prepare(path)
            assert main.encoded_parts(candidate) == main.encoded_parts(initial)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            pending = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "revert")
            )
            await engine.run_until_quiescent(T0)
            answer = pending.result()
            assert answer.unchanged is None and answer.applied.result()["rev"] == 2
            assert engine.profile.kill_signals == (2, 9)
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("busy", ["drain", "update"])
def test_bug57_unchanged_update_respects_refusals(tmp_path, monkeypatch, busy):
    from dsl41.engine_charts import DrainRequest

    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            plan = reread(engine.update.live(), root / "plans", candidate=initial, start=T0)
            if busy == "drain":
                monkeypatch.setattr(engine.attempts, "unsettled", lambda: ("waiting",))
                monkeypatch.setattr(engine.attempts, "stop_all", lambda cause: None)
                monkeypatch.setattr(engine.stops, "arm_abort_deadline", lambda deadline: None)
                engine.lifecycle.stop_requested(DrainRequest(0, 10, 5))
                assert engine.lifecycle.state == "DRAINING"
            else:
                (tmp_path / "a.jil").write_text(JOB.replace("true", "echo changed"))
                changed = reread(
                    engine.update.live(),
                    root / "plans",
                    candidate=main.prepare(path),
                    start=T0,
                )
                monkeypatch.setattr(engine.update, "stop_attempt", lambda job, cause: None)
                engine.update.admit(UpdateRequest(changed.token, 1, engine.baseline_id, "open"))
                assert engine.update.stopping
            before = list(engine.journal.store.read_from(0))
            answer = engine.update.admit(UpdateRequest(plan.token, 1, engine.baseline_id, "equal"))
            assert answer.unchanged is None
            if busy == "drain":
                assert answer.code == "engine_draining"
            else:
                assert isinstance(answer.decision, Refused)
                assert answer.decision.code == "update_in_progress"
            assert list(engine.journal.store.read_from(0)) == before
            assert engine.config_rev == 1 and plan.plan_dir.is_dir()
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_runtime_policy_update_reread_and_restart(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"
    profile_path = tmp_path / "site.profile"
    profile_path.write_text("export SYNTHETIC_POLICY=site\n")
    config_file(
        tmp_path,
        engine=(
            f'job_profile = "{profile_path}"\n'
            'job_shell = "/bin/bash"\n'
            'std_file_mode = "truncate"\n'
            "set_job_attribute_environmentals = false\n"
        ),
    )
    candidate = main.prepare(path)
    expected = main.profile_of(candidate)

    def unchanged(engine):
        live = engine.update.live()
        plan = reread(live, root / "plans", candidate=candidate, start=T0)
        assert not plan.plan.changed and plan.plan.unchanged == ("A",)
        assert plan.stop == ()
        assert definition_hash(live.catalog.jobs["A"], live.catalog, live.settings) == (
            definition_hash(candidate.catalog.jobs["A"], candidate.catalog, candidate.settings)
        )

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "policy")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.result()["rev"] == 2
            assert engine.profile == expected
            assert active_configuration(root).profile == expected
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["A"].status == "RUNNING"
            unchanged(engine)
        finally:
            await engine.shutdown()
            engine.journal.close()

        clock = VirtualClock(start=T0 + timedelta(seconds=1))
        resumed = await resume_run(
            candidate.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            declared=expected,
        )
        try:
            await resumed.run_until_quiescent(clock.now())
            assert resumed.config_rev == 2 and resumed.profile == expected
            unchanged(resumed)
        finally:
            await resumed.shutdown()
            resumed.journal.close()

        edited = expected.model_copy(update={"job_shell": "/bin/sh"})
        with pytest.raises(EngineError, match="catalog hash mismatch:.*reread and update"):
            await resume_run(
                candidate.catalog,
                root,
                clock=clock,
                adapters={"CMD": FakeAdapter()},
                declared=edited,
            )

    asyncio.run(scenario())


def test_stopping_job_shell_update_resumes_with_candidate_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            config_file(tmp_path, engine='job_shell = "/bin/bash"\n')
            candidate = main.prepare(path)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            assert plan.stop == ("A",)
            with monkeypatch.context() as held:
                held.setattr(engine.update, "stop_attempt", lambda job, cause: None)
                answer = engine.request_update(
                    UpdateRequest(plan.token, 1, engine.baseline_id, "shell-stopping")
                )
                await engine.run_until_quiescent(T0)
                assert isinstance(answer.result().decision, Admitted)
                assert not answer.result().applied.done()
                assert engine.update.view() == {"token": plan.token, "stopping": ["A"]}
                assert engine.config_rev == 1
        finally:
            await engine.shutdown()
            engine.journal.close()

        clock = VirtualClock(start=T0 + timedelta(seconds=1))
        resumed = await resume_run(
            candidate.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            declared=main.profile_of(candidate),
        )
        try:
            await resumed.run_until_quiescent(clock.now())
            assert resumed.config_rev == 2 and not resumed.update.stopping
            assert resumed.profile.job_shell == "/bin/bash"
            assert active_configuration(root).profile.job_shell == "/bin/bash"
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_07_08_09_captured_update_retry_stale_and_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.launch_settings = dict(initial.metadata["launch"])
            engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            (tmp_path / "author.py").write_text(PYTHON)
            config_file(tmp_path, include=["*.jil", "*.py"])
            candidate = main.prepare(path)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            assert plan.plan.unchanged == ("A",)
            (tmp_path / "author.py").unlink()
            (tmp_path / "a.jil").unlink()
            config_file(tmp_path, lint='refuse = "warn"\n')
            import dsl41.cli_common

            monkeypatch.setattr(
                dsl41.cli_common, "module_builder", lambda *a, **k: pytest.fail("import at update")
            )
            request = UpdateRequest(plan.token, 1, engine.baseline_id, "main-update")
            pending = engine.request_update(request)
            await engine.run_until_quiescent(clock.now())
            answer = pending.result()
            assert isinstance(answer.decision, Admitted)
            assert answer.applied is not None and answer.applied.result()["rev"] == 2
            assert engine.oracle.store.job["A"].on_hold
            assert set(engine.oracle.catalog.jobs) == {"A", "P"}
            engine.take_snapshot()
            assert active_configuration(root).source_capture == engine.source_capture
            assert (
                main.decoded_parts(engine.source_capture).metadata["config"]["lint"]["refuse"]
                == "none"
            )
            retry = engine.request_update(request)
            await engine.run_until_quiescent(clock.now())
            assert retry.result().application["rev"] == 2
            stale = engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "stale"))
            await engine.run_until_quiescent(clock.now())
            assert (
                isinstance(stale.result().decision, Refused)
                and stale.result().decision.code == "stale_plan"
            )
            assert main.fingerprint(main.decoded_parts(engine.source_capture)) == main.fingerprint(
                candidate
            )
            assert engine.journal is not None
            assert set(replay_store(engine.journal.store, where="test").oracle.catalog.jobs) == {
                "A",
                "P",
            }
            engine.request_stop()
            refused = engine.request_update(
                UpdateRequest(plan.token, 2, engine.baseline_id, "draining")
            )
            await engine.run_until_quiescent(clock.now())
            assert refused.result().code == "engine_draining"
        finally:
            await engine.shutdown()
            if engine.journal is not None:
                engine.journal.close()

    asyncio.run(scenario())
    assert active_configuration(root).source_capture


def test_09_10_stopping_restart_keeps_admitted_policy_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(tmp_path)
    initial = main.prepare(path)
    root = tmp_path / "run"

    async def crash() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "A"}))
            await engine.run_until_quiescent(T0)
            (tmp_path / "a.jil").write_text(JOB.replace("true", "echo captured"))
            candidate = main.prepare(path)
            result = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            assert result.stop == ("A",)
            request = UpdateRequest(result.token, 1, engine.baseline_id, "stopping")
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
            engine.journal.append(Record(T0, "config", None, request.request_id, intent))
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(crash())
    (tmp_path / "a.jil").unlink()
    config_file(tmp_path, lint='refuse = "warn"\n')
    import dsl41.cli_common

    monkeypatch.setattr(
        dsl41.cli_common, "module_builder", lambda *a, **k: pytest.fail("import at restart")
    )

    async def restart() -> None:
        stored = active_configuration(root)
        clock = VirtualClock(start=T0 + timedelta(seconds=1))
        engine = await resume_run(
            stored.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            declared=stored.profile,
            stored_sources=True,
        )
        try:
            await engine.run_until_quiescent(clock.now())
            assert engine.config_rev == 2 and not engine.update.stopping
            assert engine.oracle.catalog.jobs["A"].exec_.command == "echo captured"
            assert (
                main.decoded_parts(engine.source_capture).metadata["config"]["lint"]["refuse"]
                == "none"
            )
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(restart())


def test_09_running_box_membership_is_prepared(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text("insert_job: BX\njob_type: b\n" + JOB + "box_name: BX\n")
    initial = main.prepare(path)
    from test_main_config import live_of

    (tmp_path / "a.jil").write_text(
        (tmp_path / "a.jil").read_text() + JOB.replace("A", "B") + "box_name: BX\n"
    )
    result = reread(
        live_of(initial, {"BX": "RUNNING"}),
        tmp_path / "plans",
        candidate=main.prepare(path),
    )
    assert result.plan.added == ("B",)
    assert result.plan_json.exists()


def test_09_capture_format_dispatch(tmp_path: Path) -> None:
    candidate = main.prepare(estate_file(tmp_path))
    parts = dict(candidate.parts)
    payload = json.loads(parts[main.CAPTURE])
    payload["unknown_field"] = True
    extended = {**parts, main.CAPTURE: json.dumps(payload).encode()}
    assert set(main.from_parts(list(extended.items())).catalog.jobs) == {"A"}
    for version in (None, True, 999):
        payload = json.loads(parts[main.CAPTURE])
        if version is None:
            payload.pop("format_version")
        else:
            payload["format_version"] = version
        with pytest.raises(main.RereadRefused, match="main-inputs format_version"):
            main.from_parts(
                [
                    *[(n, b) for n, b in candidate.parts if n != main.CAPTURE],
                    (main.CAPTURE, json.dumps(payload).encode()),
                ]
            )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_legacy_config_row_retry_never_appends(tmp_path: Path, version: int, kind: str) -> None:
    candidate = main.prepare(estate_file(tmp_path))
    configuration = Configuration(
        catalog=candidate.catalog,
        profile=main.profile_of(candidate),
        source_capture=main.encoded_parts(candidate) if version == 2 else {},
    )
    payload = json.loads(configuration.to_bytes())
    payload.pop("job_order")
    if version == 1:
        payload.pop("configuration_format_version")
    else:
        payload["configuration_format_version"] = 2
    legacy = canonical_bytes(payload)
    store = MemoryStore() if kind == "memory" else SqliteStore(tmp_path / "boundary.db")
    with closing(store):
        for invalid in (0, 2):
            with pytest.raises(EngineError, match="configuration rev"):
                runner_update.store_config(store, configuration, invalid)
            assert store.current_config_rev() == 0
        assert store.put_config(b"initial", "sha256:initial") == 1
        assert store.put_config(legacy, "sha256:" + hashlib.sha256(legacy).hexdigest()) == 2
        runner_update.store_config(store, configuration, 2)
        assert store.current_config_rev() == 2
        assert store.get_config(2) == legacy
        changed = configuration.model_copy(
            update={"profile": RuntimeProfile(default_tz="Europe/Zurich")}
        )
        with pytest.raises(EngineError, match="configuration rev"):
            runner_update.store_config(store, changed, 2)
        assert store.current_config_rev() == 2
        assert store.get_config(2) == legacy
        for invalid in (0, 1, 4):
            with pytest.raises(EngineError, match="configuration rev"):
                runner_update.store_config(store, configuration, invalid)
            assert store.current_config_rev() == 2


def test_older_build_crash_after_config_row_resumes_without_another_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(tmp_path)
    jobs = JOB + "\ninsert_job: B\njob_type: c\ncommand: true\ncondition: s(A)\n"
    (tmp_path / "a.jil").write_text(jobs)
    initial = main.prepare(path)
    profile = main.profile_of(initial)
    capture = main.encoded_parts(initial)
    root = tmp_path / "run"

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            launched=profile,
            source_capture=capture,
        )
        assert engine.journal is not None
        try:
            (tmp_path / "a.jil").write_text(jobs.replace("true", "echo captured"))
            candidate = main.prepare(path)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            append = engine.journal.append

            def old_writer(store, configuration, rev):
                payload = json.loads(configuration.to_bytes())
                payload.pop("job_order")
                payload["configuration_format_version"] = 2
                data = canonical_bytes(payload)
                assert store.put_config(data, "sha256:" + hashlib.sha256(data).hexdigest()) == rev

            def crash_before_applied(*records):
                if any(r.kind == "config" and r.payload.get("event") == "applied" for r in records):
                    raise RuntimeError("crash before config applied")
                return append(*records)

            with monkeypatch.context() as old:
                old.setattr(runner_update, "store_config", old_writer)
                old.setattr(engine.journal, "append", crash_before_applied)
                engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "old-build"))
                with pytest.raises(RuntimeError, match="crash before config applied"):
                    await engine.run_until_quiescent(T0)
            assert engine.journal.store.current_config_rev() == 2
            legacy = engine.journal.store.get_config(2)
        finally:
            engine.journal.close()
        (tmp_path / "a.jil").unlink()
        stored = active_configuration(root)
        assert stored.catalog.jobs["A"].exec_.command == "true"
        resumed = await resume_run(
            stored.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            declared=stored.profile,
            stored_sources=True,
        )
        try:
            await resumed.run_until_quiescent(T0)
            assert resumed.config_rev == 2 and not resumed.update.stopping
            assert resumed.oracle.catalog.jobs["A"].exec_.command == "echo captured"
            assert resumed.journal.store.current_config_rev() == 2
            assert resumed.journal.store.get_config(2) == legacy
            applied = [
                r
                for _, r in resumed.journal.store.read_from(0)
                if r.kind == "config" and r.payload.get("event") == "applied"
            ]
            assert [r.payload["rev"] for r in applied] == [1, 2]
            assert replay_store(resumed.journal.store, where="legacy update").replay.rev == 2
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("snapshot", [False, True])
def test_v2_main_config_restart_replays_captured_catalog_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, snapshot: bool
) -> None:
    from dsl41.runner_journal import oracle_rows
    from test_runner_journal import _ORDER_GATE, _ORDER_JIL

    path = config_file(tmp_path)
    (tmp_path / "00_gate.jil").write_text(_ORDER_GATE)
    (tmp_path / "10_workers.jil").write_text(_ORDER_JIL.removeprefix(_ORDER_GATE))
    candidate = main.prepare(path)
    root = tmp_path / "run"
    original_bytes = Configuration.to_bytes

    def legacy_bytes(configuration):
        payload = json.loads(original_bytes(configuration))
        payload.pop("job_order")
        payload["configuration_format_version"] = 2
        return canonical_bytes(payload)

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        with monkeypatch.context() as old:
            old.setattr(Configuration, "to_bytes", legacy_bytes)
            engine = start_run(
                candidate.catalog,
                root,
                clock=clock,
                adapters={"CMD": FakeAdapter(default=None)},
                launched=main.profile_of(candidate),
                source_capture=main.encoded_parts(candidate),
            )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "GATE"}))
            await engine.run_until_quiescent(T0)
            if snapshot:
                engine.take_snapshot()
            for minute, name in enumerate(("GATE", "LOW", "HIGH"), 1):
                engine.inject(
                    Event(
                        at=T0 + timedelta(minutes=minute),
                        kind="STATUS",
                        payload={"job": name, "status": "SUCCESS"},
                    )
                )
            await engine.run_until_quiescent(T0 + timedelta(minutes=3))
            assert all(row.status == "SUCCESS" for row in engine.oracle.store.job.values())
            assert [t.job for t in engine.oracle.trace() if t.transition.endswith("STARTING")] == [
                "GATE",
                "LOW",
                "HIGH",
            ]
            expected = oracle_rows(engine.oracle)
            legacy = engine.journal.store.get_config(1)
            assert list(json.loads(legacy)["catalog"]["jobs"]) == ["GATE", "HIGH", "LOW"]
        finally:
            engine.journal.close()
        for source in tmp_path.glob("*.jil"):
            source.unlink()
        stored = active_configuration(root)
        assert list(stored.catalog.jobs) == ["GATE", "LOW", "HIGH"]
        resumed = await resume_run(
            stored.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            declared=stored.profile,
            stored_sources=True,
        )
        try:
            await resumed.run_until_quiescent(clock.now())
            assert oracle_rows(resumed.oracle) == expected
            replayed = replay_store(resumed.journal.store, where="v2 main config")
            assert oracle_rows(replayed.oracle) == expected
            assert resumed.journal.store.get_config(1) == legacy
            assert resumed.journal.store.current_config_rev() == 1
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["catalog", "encoding", "capture"])
def test_v2_configuration_refuses_untrusted_capture_order(tmp_path: Path, damage: str) -> None:
    candidate = main.prepare(estate_file(tmp_path))
    configuration = Configuration(
        catalog=candidate.catalog,
        profile=main.profile_of(candidate),
        source_capture=main.encoded_parts(candidate),
    )
    payload = json.loads(configuration.to_bytes())
    payload.pop("job_order")
    payload["configuration_format_version"] = 2
    if damage == "catalog":
        payload["catalog"]["jobs"]["A"]["exec_"]["command"] = "echo other"
    elif damage == "encoding":
        payload["source_capture"] = {"main-inputs.json": "!"}
    else:
        payload["source_capture"] = {"main-inputs.json": "e30="}
    with pytest.raises(EngineError, match="untrusted: .*source_capture"):
        Configuration.from_bytes(canonical_bytes(payload), where="untrusted")


def test_kill_signals_update_affects_later_kills_only(
    tmp_path: Path, monkeypatch, owned_processes
) -> None:
    from datetime import datetime
    from dsl41 import runner_stops
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_clock import RealClock
    from test_engine_stops import _finish, _jil, _start_jobs, _until
    from test_kill_signals import signal_command

    a, ready_a, log_a = signal_command(tmp_path, "a")
    b, ready_b, log_b = signal_command(tmp_path, "b")
    (tmp_path / "a.jil").write_text(_jil({"a": a, "b": b}))
    path = config_file(tmp_path)
    estate = main.prepare(path)
    root = tmp_path / "run"
    selections = []
    original = runner_stops.selected_signal_ladder

    async def selected(pgid, token, signals, *args, **kwargs):
        selections.append(signals)
        await original(pgid, token, signals, *args, **kwargs)

    monkeypatch.setattr(runner_stops, "selected_signal_ladder", selected)

    async def scenario():
        engine = start_run(
            estate.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter()},
            launched=main.profile_of(estate),
            source_capture=main.encoded_parts(estate),
            hold_open=True,
        )
        engine.launch_settings = estate.metadata["launch"]
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            _start_jobs(engine, "a", "b")
            await _until(lambda: ready_a.exists() and ready_b.exists())
            await _until(lambda: engine.attempt_view("b")["state"] == "RUNNING")
            engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "a"}))
            await _until(log_a.exists)
            config_file(tmp_path, engine="kill_signals = [9]\n")
            candidate = main.prepare(path)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate)
            assert plan.plan.changed == () and plan.stop == ()
            answer = await engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "policy")
            )
            assert isinstance(answer.decision, Admitted)
            await answer.applied
            assert engine.profile.kill_signals == (9,)
            assert engine.attempt_view("b")["state"] == "RUNNING"
            assert selections == [(2, 9)]
            engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "b"}))
            await _until(lambda: (root / "runs/b.1/1/status.json").exists())
            assert not log_b.exists()
            await _until(lambda: (root / "runs/a.1/1/status.json").exists())
            assert selections == [(2, 9), (9,)] and log_a.read_text().splitlines() == ["2"]
            assert json.loads((root / "runs/a.1/1/status.json").read_text())["signal"] == 9
            assert json.loads((root / "runs/b.1/1/status.json").read_text())["signal"] == 9
        finally:
            await _finish(engine, loop)

    asyncio.run(scenario())


def test_fw_wildcard_to_exact_survives_update_and_restart(tmp_path: Path) -> None:
    """DL-467: update and restart keep one sampled FW run and its pathname streak."""
    from dsl41.runner_adapters import FileWatcherAdapter, read_watch_log

    watched = tmp_path / "watched.txt"
    watched.write_text("data")
    path = config_file(tmp_path)
    source = tmp_path / "a.jil"

    def definition(target: str) -> str:
        return f"insert_job: W\njob_type: f\nwatch_file: {target}\nwatch_interval: 30\n"

    source.write_text(definition(str(tmp_path / "*.txt")))
    initial = main.prepare(path)
    root = tmp_path / "run"
    run_dir = root / "runs" / "W.1"
    log = run_dir / "watch.jsonl"

    async def scenario() -> None:
        engine = start_run(
            initial.catalog,
            root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "W"}))
            await engine.run_until_quiescent(T0)
            prefix = log.read_bytes()
            before = [json.loads(line) for line in prefix.splitlines()]
            assert [line["kind"] for line in before] == ["start", "sample"]
            [observed] = before[1]["files"]
            exact = observed["path"]
            assert read_watch_log(run_dir).pattern.stable == {exact: 1}

            def check_waiting(current) -> None:
                row = current.oracle.store.job["W"]
                assert (row.status, row.run_number) == ("RUNNING", 1)
                assert current.config_rev == 2
                assert current.oracle.catalog.jobs["W"].exec_.watch_file == exact
                assert log.read_bytes() == prefix
                assert read_watch_log(run_dir).pattern.stable == {exact: 1}

            source.write_text(definition(exact))
            plan = reread(
                engine.update.live(),
                root / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            pending = engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "fw"))
            await engine.run_until_quiescent(T0 + timedelta(seconds=1))
            assert isinstance(pending.result().decision, Admitted)
            assert pending.result().applied.result()["rev"] == 2
            [applied] = [
                record.payload
                for _, record in engine.journal.store.read_from(0)
                if record.kind == "config"
                and record.payload.get("event") == "applied"
                and record.payload.get("rev") == 2
            ]
            assert applied["stopped"] == [["W", "rerun"]]
            check_waiting(engine)
        finally:
            await engine.shutdown()
            engine.journal.close()

        stored = active_configuration(root)
        assert stored.catalog.jobs["W"].exec_.watch_file == exact
        engine = await resume_run(
            stored.catalog,
            root,
            clock=VirtualClock(start=T0 + timedelta(seconds=2)),
            adapters={"FW": FileWatcherAdapter()},
            declared=stored.profile,
            stored_sources=True,
        )
        try:
            await engine.run_until_quiescent(T0 + timedelta(seconds=2))
            check_waiting(engine)
            await engine.run_until_quiescent(T0 + timedelta(seconds=29))
            check_waiting(engine)
            await engine.run_until_quiescent(T0 + timedelta(seconds=30))
            replayed = replay_store(engine.journal.store, where="FW update and restart")
            row = replayed.oracle.store.job["W"]
            assert (row.status, row.run_number, row.exit_code) == ("SUCCESS", 1, 0)
        finally:
            await engine.shutdown()
            engine.journal.close()

        after = log.read_bytes()
        assert after.startswith(prefix)
        lines = [json.loads(line) for line in after.splitlines()]
        assert [line["kind"] for line in lines] == ["start", "sample", "sample"]
        assert {line["run_id"] for line in lines} == {before[0]["run_id"]}
        assert lines[-1]["files"] == [observed]
        assert read_watch_log(run_dir).pattern.stable == {exact: 2}
        assert sorted(item.name for item in (root / "runs").iterdir()) == ["W.1"]

    asyncio.run(scenario())


def test_socket_reload_preview_update_retry_and_marker(short_root: Path) -> None:
    from datetime import datetime
    from dsl41.runner_clock import RealClock
    from dsl41.runner_control import ControlServer, roundtrip

    path = estate_file(short_root)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(default=None)},
            hold_open=True,
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.reread_source = str(path)
        engine.launch_settings = initial.metadata["launch"]
        server = ControlServer(engine, short_root / "control.sock")
        await server.start()
        task = asyncio.create_task(engine.run_until_quiescent(datetime.max))

        async def ask(cmd, **fields):
            return await asyncio.to_thread(
                roundtrip, server.path, {"v": 5, "cmd": cmd, **fields}, timeout=10
            )

        try:
            await engine.started.wait()
            (short_root / "a.jil").write_text(JOB.replace("true", "echo new"))
            result = await ask("reread")
            assert result["ok"], result
            assert result["preview"]["changed"] == [{"name": "A", "reason": None}]
            assert not list((short_root / "run" / "plans").iterdir())
            status = await ask("status")
            assert status["jobs"]["A"]["reread"]["change"] == "changed"
            rev = engine.config_rev
            request = {
                "baseline_id": engine.baseline_id,
                "request_id": "socket-update",
                "wait": True,
            }
            applied = await ask("update", **request)
            assert applied["ok"] and applied["application"]["rev"] == rev + 1, applied
            assert "token" not in applied and "token" not in applied["application"]
            assert "reread" not in (await ask("status"))["jobs"]["A"]
            path.write_text("broken = [")
            reused = await ask("update", claimed_actor="different", **request)
            assert reused["code"] == "request_id_reused"
            assert (await ask("update", retry_only=True, **request))["application"] == applied[
                "application"
            ]
            assert not (
                await ask(
                    "update", request_id="unknown", retry_only=True, baseline_id=engine.baseline_id
                )
            )["ok"]
            assert not (await ask("reread"))["ok"]
            assert engine.config_rev == rev + 1
            assert (await ask("update", token="a" * 64))["code"] == "invalid_argument"
        finally:
            await server.close()
            engine.request_stop()
            await asyncio.wait_for(task, 10)
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def _box_update_records(records):
    # DL-489: compare durable update effects without sequence numbers or attempt identities.
    keys = (
        "event",
        "rev",
        "kind",
        "payload",
        "source",
        "job",
        "run_number",
        "ordinal",
        "after",
        "added",
        "changed",
        "removed",
        "restored",
        "stopped",
    )
    return [
        (r.kind, {key: r.payload[key] for key in keys if key in r.payload})
        for _, r in records
        if r.kind in {"input", "config", "dispatch"}
    ]


@pytest.mark.parametrize(
    "crash",
    [
        None,
        "before_kill",
        "after_box_kill",
        "between_members",
        "before_apply",
        "after_apply",
        "cancel_time",
        "cancel_hold",
        "cancel_start",
    ],
)
@pytest.mark.parametrize("snapshot", [False, True])
def test_box_restart_commits_kills_and_holds_finished_marked_members(
    short_root: Path, monkeypatch, crash, snapshot: bool
) -> None:
    """Pin crash and virtual-clock cancellation recovery through shutdown and restart.

    The cancellation cases cover restart and operator completion under DL-463.
    Same-engine reuse after cancellation can leave the update unfinished (DL-488).
    """
    from dsl41.runner_update import settings_of

    async def scenario(short_root: Path, crash, snapshot: bool):
        short_root.mkdir(exist_ok=True)
        path = config_file(short_root, estate='non_idempotent = "non-idempotent.toml"\n')
        (short_root / "non-idempotent.toml").write_text(
            '[jobs]\nDONE = "synthetic finished effect"\nHELD = "synthetic running effect"\n'
        )
        text = (
            "insert_job: BX\njob_type: b\n"
            + JOB.replace("A", "LIVE")
            + "box_name: BX\n"
            + JOB.replace("A", "HELD")
            + "box_name: BX\n"
            + JOB.replace("A", "DONE")
            + "box_name: BX\n"
        )
        (short_root / "a.jil").write_text(text)
        initial = main.prepare(path)
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            engine.inject(Event(at=T0, kind="STATUS", payload={"job": "DONE", "status": "SUCCESS"}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["DONE"].status == "SUCCESS"
            old_run = engine.oracle.store.job["BX"].run_number
            (short_root / "a.jil").write_text(text + JOB.replace("A", "NEW") + "box_name: BX\n")
            candidate = main.prepare(path)
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=candidate,
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, engine.config_rev, engine.baseline_id, "box-update")
            )
            before_application = crash in {
                "before_kill",
                "after_box_kill",
                "between_members",
                "before_apply",
            }
            if crash:
                if crash.startswith("cancel_"):
                    # DL-488: cancellation after APPLIED does not extend DL-463's window.
                    wait_until = clock.wait_until
                    waits = 0
                    cut_at = {"cancel_time": 1, "cancel_hold": 2, "cancel_start": 4}[crash]
                    listeners = []
                    engine.update.listeners.append(listeners.append)

                    async def cut_wait(at, interrupt=None):
                        nonlocal waits
                        if engine.config_rev == 2:
                            waits += 1
                            if waits == cut_at:
                                raise asyncio.CancelledError
                        await wait_until(at, interrupt)

                    with monkeypatch.context() as patch:
                        patch.setattr(clock, "wait_until", cut_wait)
                        with pytest.raises(asyncio.CancelledError):
                            await engine.run_until_quiescent(T0)
                    assert waits == cut_at
                    assert engine.update.tier.state == "APPLIED" and engine.update.view() is None
                    assert not answer.result().applied.done() and listeners == []
                    assert [
                        record.payload["kind"]
                        for _, record in engine.journal.store.read_from(0)
                        if record.kind == "input" and record.payload.get("kind") == "ON_HOLD"
                    ] == (["ON_HOLD", "ON_HOLD"] if crash == "cancel_start" else [])
                else:

                    def cut():
                        raise EngineError("cut during box restart")

                    if crash in {"after_box_kill", "between_members"}:
                        admit = engine._admit_and_apply
                        killed = []
                        cut_after = 1 if crash == "after_box_kill" else 2

                        async def cut_kill(pending):
                            event = pending.ev
                            if (
                                event is not None
                                and event.kind == "KILLJOB"
                                and event.source == "reconcile"
                            ):
                                if len(killed) == cut_after:
                                    cut()
                                killed.append(event.payload["job"])
                            return await admit(pending)

                        monkeypatch.setattr(engine, "_admit_and_apply", cut_kill)
                    elif crash == "before_apply":
                        ends = engine.update.box_ends

                        def cut_boxes_ended():
                            names = ends()
                            monkeypatch.setattr(engine.update.tier, "boxes_ended", cut)
                            return names

                        monkeypatch.setattr(engine.update, "box_ends", cut_boxes_ended)
                    else:
                        monkeypatch.setattr(
                            engine.update,
                            "box_ends" if crash == "before_kill" else "box_starts",
                            cut,
                        )
                    with pytest.raises(EngineError, match="cut during box restart"):
                        await engine.run_until_quiescent(T0)
                assert engine.config_rev == (1 if before_application else 2)
                assert engine.oracle.store.job["BX"].status == (
                    "RUNNING" if crash == "before_kill" else "TERMINATED"
                )
                assert engine.oracle.store.job["DONE"].on_hold == (crash == "cancel_start")
                if snapshot:
                    engine.take_snapshot()
                await engine.shutdown()
                engine.journal.close()
                stored = active_configuration(short_root / "run")
                engine = await resume_run(
                    stored.catalog,
                    short_root / "run",
                    clock=clock,
                    adapters={"CMD": FakeAdapter(default=None)},
                    declared=stored.profile,
                    stored_sources=True,
                )
                await engine.run_until_quiescent(T0)
                if not before_application:
                    assert engine.update.tier is None
                    assert engine.oracle.store.job["BX"].status == "TERMINATED"
                    # Documented operator completion: hold marked members before force-start.
                    for name in ("DONE", "HELD"):
                        engine.inject(Event(at=T0, kind="ON_HOLD", payload={"job": name}))
                    engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
                    await engine.run_until_quiescent(T0)
            else:
                await engine.run_until_quiescent(T0)
                assert answer.result().applied.done()
            assert engine.oracle.store.job["BX"].run_number == old_run + 1
            assert engine.oracle.store.job["HELD"].on_hold
            assert engine.oracle.store.job["DONE"].on_hold
            assert engine.oracle.store.job["DONE"].run_number == 1
            assert engine.oracle.store.job["LIVE"].run_number == 2
            records = list(engine.journal.store.read_from(0))
            [intent] = [
                r.payload
                for _, r in records
                if r.kind == "config" and r.payload.get("event") == "intent"
            ]
            assert intent["restart_boxes"] == ["BX"]
            assert not [
                record
                for _, record in records
                if record.kind == "dispatch"
                and record.payload.get("job") in {"LIVE", "HELD", "DONE"}
                and record.payload.get("after") == "update_stop"
            ]
            applied_seq = next(
                seq
                for seq, record in records
                if record.kind == "config"
                and record.payload.get("event") == "applied"
                and record.payload.get("rev") == 2
            )
            killed = [
                seq
                for seq, record in records
                if record.kind == "input" and record.payload.get("kind") == "KILLJOB"
            ]
            assert killed and max(killed) < applied_seq
            assert settings_of(engine.profile, engine.oracle.catalog) == candidate.settings
            return _box_update_records(records)
        finally:
            await engine.shutdown()
            engine.journal.close()

    actual = asyncio.run(scenario(short_root, crash, snapshot))
    if crash in {"before_kill", "after_box_kill", "between_members", "before_apply"}:
        assert actual == asyncio.run(scenario(short_root / "uncut", None, False))


@pytest.mark.parametrize("snapshot", [False, True])
def test_retyped_command_box_scope_recovers_as_the_uncut_update(short_root, monkeypatch, snapshot):
    """DL-489: admission can select a running command that the candidate makes a BOX."""

    async def scenario(directory: Path, crash: bool):
        path = config_file(directory)
        (directory / "a.jil").write_text(JOB.replace("A", "BX"))
        initial = main.prepare(path)
        clock = VirtualClock(start=T0)
        root = directory / "run"
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            (directory / "a.jil").write_text(
                "insert_job: BX\njob_type: b\n" + JOB.replace("A", "NEW") + "box_name: BX\n"
            )
            plan = reread(
                engine.update.live(), root / "plans", candidate=main.prepare(path), start=T0
            )
            engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "retype-box"))
            if crash:

                def cut(job, cause):
                    raise EngineError("cut after intent")

                with monkeypatch.context() as patch:
                    patch.setattr(engine.update, "stop_attempt", cut)
                    with pytest.raises(EngineError, match="cut after intent"):
                        await engine.run_until_quiescent(T0)
                assert engine.oracle.catalog.jobs["BX"].job_type == "CMD"
                assert engine.journal.open.update["restart_boxes"] == ["BX"]
                if snapshot:
                    engine.take_snapshot()
                await engine.shutdown()
                engine.journal.close()
                stored = active_configuration(root)
                engine = await resume_run(
                    stored.catalog,
                    root,
                    clock=clock,
                    adapters={"CMD": FakeAdapter(default=None)},
                    declared=stored.profile,
                    stored_sources=True,
                )
            await engine.run_until_quiescent(T0)
            assert engine.oracle.catalog.jobs["BX"].job_type == "BOX"
            assert engine.oracle.store.job["BX"].status == "RUNNING"
            assert engine.oracle.store.job["BX"].run_number == 2
            assert engine.oracle.store.job["NEW"].run_number == 1
            records = list(engine.journal.store.read_from(0))
            assert not any(
                r.kind == "dispatch" and r.payload.get("after") == "update_stop" for _, r in records
            )
            return _box_update_records(records)
        finally:
            await engine.shutdown()
            engine.journal.close()

    uncut = asyncio.run(scenario(short_root / "uncut", False))
    assert asyncio.run(scenario(short_root / "recovered", True)) == uncut


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.parametrize(
    "roots", ["missing", [], None, "BX", [7], ["A"], ["absent"], ["BX", "BX"], ["Z", "BX"]]
)
def test_box_scope_compatibility_and_refusals(short_root: Path, monkeypatch, snapshot, roots):
    """DL-489: only absence recomputes; invalid roots refuse before writes or signals."""
    import sqlite3
    from dsl41 import reload_plan, runner_procid, runner_startup
    from dsl41.runner_config import EngineSettings

    path = config_file(short_root)
    text = "insert_job: BX\njob_type: b\n" + JOB + "box_name: BX\n"
    (short_root / "a.jil").write_text(text)
    initial = main.prepare(path)
    root = short_root / "run"

    async def scenario():
        clock = VirtualClock(start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            (short_root / "a.jil").write_text(text + JOB.replace("A", "NEW") + "box_name: BX\n")
            plan = reread(
                engine.update.live(), root / "plans", candidate=main.prepare(path), start=T0
            )
            engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "box-scope"))
            admit, killed = engine._admit_and_apply, []

            async def cut(pending):
                event = pending.ev
                if event is not None and event.kind == "KILLJOB" and event.source == "reconcile":
                    if len(killed) == (0 if roots == [] else 1):
                        raise EngineError("cut inside box stop")
                    killed.append(event.payload["job"])
                return await admit(pending)

            monkeypatch.setattr(engine, "_admit_and_apply", cut)
            with pytest.raises(EngineError, match="cut inside box stop"):
                await engine.run_until_quiescent(T0)
            if snapshot:
                engine.take_snapshot()
        finally:
            await engine.shutdown()
            engine.journal.close()

        def alter(intent):
            if roots == "missing":
                intent.pop("restart_boxes")
            else:
                intent["restart_boxes"] = roots

        # DL-489: retained bodies may predate the key or carry damaged scope evidence.
        with closing(sqlite3.connect(root / "engine.db")) as db:
            for seq, data in db.execute(
                "SELECT seq, payload FROM journal WHERE kind='config'"
            ).fetchall():
                body = json.loads(data)
                if body["event"] == "intent":
                    alter(body)
                    db.execute(
                        "UPDATE journal SET payload=? WHERE seq=?", (canonical_bytes(body), seq)
                    )
            if snapshot:
                seq, data = db.execute("SELECT seq, data FROM snapshots").fetchone()
                body = json.loads(data)
                alter(body["update"])
                db.execute("UPDATE snapshots SET data=? WHERE seq=?", (canonical_bytes(body), seq))
            db.commit()
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        def forbidden(*args, **kwargs):
            raise AssertionError("recovery effect or scope recomputation before validation")

        async def restart():
            return await resume_run(
                initial.catalog,
                root,
                clock=clock,
                adapters={"CMD": FakeAdapter(default=None)},
                declared=main.profile_of(initial),
                stored_sources=True,
            )

        if roots not in ("missing", []):
            with monkeypatch.context() as guarded:
                guarded.setattr(runner_startup, "_adopt_open_attempts", forbidden)
                guarded.setattr(runner_procid, "signal_ours", forbidden)
                guarded.setattr(runner_startup.Journal, "append", forbidden)
                with pytest.raises(EngineError, match="config intent restart_boxes") as refused:
                    await restart()
                if roots in (["A"], ["absent"]):
                    assert repr(roots[0]) in str(refused.value)
                    assert "not a BOX of the applied or candidate catalog" in str(refused.value)
                elif snapshot:
                    assert str(refused.value).startswith(
                        f"{root / 'engine.db'}: unreadable snapshot at seq {seq}:"
                    )
                with pytest.raises(EngineError, match="config intent restart_boxes"):
                    await runner_startup.validate_resume(
                        initial.catalog,
                        root,
                        clock=clock,
                        adapters={"CMD": FakeAdapter(default=None)},
                        scheduler=None,
                        declared=main.profile_of(initial),
                        machines=None,
                        settings=EngineSettings(),
                        stored_sources=True,
                    )
            assert {
                p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
            } == before
            return
        with monkeypatch.context() as guarded:
            if roots == []:
                guarded.setattr(reload_plan, "restart_boxes", forbidden)
            engine = await restart()
            try:
                assert engine.update._box_scope.roots == ()
                await engine.run_until_quiescent(T0)
                assert engine.config_rev == 2 and engine.oracle.store.job["BX"].run_number == 1
                if roots == "missing":
                    # DL-489: a keyless interrupted update keeps the legacy BUG-67 outcome.
                    assert engine.oracle.store.job["BX"].status == "TERMINATED"
                    assert any(
                        r.kind == "dispatch" and r.payload.get("after") == "update_stop"
                        for _, r in engine.journal.store.read_from(0)
                    )
            finally:
                await engine.shutdown()
                engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("remove", [False, True])
def test_box_restart_is_outermost_and_removed_boxes_stay_stopped(short_root: Path, remove):
    from dsl41.reread_worker import preview

    text = (
        "insert_job: BX\njob_type: b\ninsert_job: INNER\njob_type: b\nbox_name: BX\n"
        + JOB.replace("A", "MEMBER")
        + "box_name: INNER\n"
        + JOB.replace("A", "OUTSIDE")
    )
    path = estate_file(short_root)
    (short_root / "a.jil").write_text(text)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            replacement = (
                JOB.replace("A", "OUTSIDE")
                if remove
                else text
                + JOB.replace("A", "NEW_INNER")
                + "box_name: INNER\n"
                + JOB.replace("A", "NEW_OUTER")
                + "box_name: BX\n"
            )
            (short_root / "a.jil").write_text(replacement)
            candidate = main.prepare(path)
            view = preview(candidate, engine.update.live())
            assert {effect["job"]: effect["action"] for effect in view["effects"]}["BX"] == (
                "remove" if remove else "restart_box"
            )
            assert [
                effect["job"] for effect in view["effects"] if effect["action"] == "restart_box"
            ] == ([] if remove else ["BX"])
            assert "INNER" in view["stop"]
            if not remove:
                assert len(view["added"]) + len(view["changed"]) + view["unchanged"] == len(
                    candidate.catalog.jobs
                )
                assert {effect["job"]: effect["action"] for effect in view["effects"]}[
                    "INNER"
                ] == "restart"
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=candidate,
                start=T0,
            )
            response = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "outermost")
            )
            await engine.run_until_quiescent(T0)
            assert response.result().applied.done()
            forced = [
                record.payload["payload"]["job"]
                for _, record in engine.journal.store.read_from(0)
                if record.kind == "input" and record.payload.get("kind") == "FORCE_STARTJOB"
            ]
            assert forced == (["BX"] if remove else ["BX", "BX"])
            if remove:
                assert set(engine.oracle.catalog.jobs) == {"OUTSIDE"}
                assert engine.oracle.store.tombstones["BX"].status == "TERMINATED"
            else:
                for name in ("BX", "INNER", "MEMBER"):
                    assert engine.oracle.store.job[name].run_number == 2
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["retype", "move_out", "move_out_marked", "move_in"])
def test_box_restart_preserves_replacement_run_ownership(short_root: Path, case):
    from dsl41.runner_control import roundtrip
    from test_main_config_restart import cli_engine
    from test_runner_control import wait_for
    from store_surgery import rows

    box = "insert_job: BX\njob_type: b\n"
    command = JOB.replace("true", "sleep 30")
    stay = command.replace("A", "STAY") + "box_name: BX\n"
    mover = command.replace("A", "MOVER")
    inside = mover + "box_name: BX\n"
    marked = case == "move_out_marked"
    path = config_file(short_root, estate='non_idempotent = "non-idempotent.toml"\n')
    (short_root / "non-idempotent.toml").write_text(
        '[jobs]\nMOVER = "synthetic effect"\n' if marked else "[jobs]\n"
    )
    old = box + stay + (mover if case == "move_in" else inside if case != "retype" else "")
    new = (
        command.replace("A", "BX")
        if case == "retype"
        else box + stay + (inside if case == "move_in" else mover)
    )
    (short_root / "a.jil").write_text(old)
    root = short_root / "run"
    root.mkdir()
    (root / "engine.toml").write_text("grace_term_s = 0.01\ngrace_kill_s = 0.01\n")
    socket = root / "control.sock"
    with cli_engine(path) as ask:

        def force(name):
            status = ask("status")
            reply = roundtrip(
                socket,
                {
                    "cmd": "sendevent",
                    "verb": "FORCE_STARTJOB",
                    "payload": {"job": name},
                    "baseline_id": status["baseline_id"],
                    "request_id": "start-" + name,
                    "expect": {"job:" + name: status["jobs"][name]["state_rev"]},
                },
            )
            assert reply["ok"], reply

        force("BX")
        if case == "move_in":
            force("MOVER")
        wait_for(lambda: ask("status")["jobs"]["STAY"]["status"] == "RUNNING")
        if case != "retype":
            wait_for(lambda: ask("status")["jobs"]["MOVER"]["status"] == "RUNNING")
        (short_root / "a.jil").write_text(new)
        view = roundtrip(socket, {"cmd": "reread", "v": 5})["preview"]
        if case != "retype":
            assert {item["job"]: item["action"] for item in view["effects"]}["MOVER"] == (
                "hold" if marked else "restart" if case == "move_in" else "rerun"
            )
            assert view["stop"] == ["BX", "MOVER", "STAY"]
        applied = roundtrip(
            socket,
            {
                "cmd": "update",
                "v": 5,
                "wait": True,
                "request_id": "ownership",
                "baseline_id": ask("status")["baseline_id"],
            },
        )
        assert applied["ok"] and applied["application"], applied
        if case == "retype":
            assert ask("status")["jobs"]["BX"]["status"] == "INACTIVE"
            assert not [
                body for _, kind, body in rows(root) if kind == "dispatch" and body["job"] == "BX"
            ]
            forced = [
                body
                for _, kind, body in rows(root)
                if kind == "input" and body.get("kind") == "FORCE_STARTJOB"
            ]
            assert len(forced) == 1
        else:
            if marked:
                wait_for(lambda: ask("status")["jobs"]["MOVER"]["on_hold"])
            else:
                wait_for(
                    lambda: (
                        ask("status")["jobs"]["MOVER"].get("attempt", {}).get("n")
                        == (1 if case == "move_in" else 2)
                    )
                )
            wait_for(lambda: ask("status")["jobs"]["STAY"]["run_number"] == 2)
            jobs = ask("status")["jobs"]
            row = jobs["MOVER"]
            assert row["run_number"] == (2 if case == "move_in" else 1)
            if marked:
                assert row["on_hold"] and row["status"] == "FAILURE"
            else:
                assert row["status"] == "RUNNING"
                assert row["attempt"]["after"] == (None if case == "move_in" else "update_stop")
            assert jobs["STAY"]["attempt"]["n"] == 1
            assert jobs["STAY"]["attempt"]["after"] is None
            assert not [
                body
                for _, kind, body in rows(root)
                if kind == "dispatch"
                and body["job"] == "STAY"
                and body.get("after") == "update_stop"
            ]


@pytest.mark.parametrize(
    "case,change,action,expected_effects",
    [
        (
            "nested",
            "unchanged",
            "restart",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "INNER", "action": "restart"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "restart"},
            ],
        ),
        (
            "in_place",
            "changed",
            "restart",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "INNER", "action": "restart"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "restart"},
            ],
        ),
        (
            "held",
            "unchanged",
            "hold",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "INNER", "action": "restart"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "hold"},
            ],
        ),
        (
            "finished",
            "unchanged",
            "restart",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "KEEP", "action": "restart"},
                {"job": "INNER", "action": "restart"},
                {"job": "MEMBER", "action": "restart"},
            ],
        ),
        (
            "finished_held",
            "unchanged",
            "hold",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "hold"},
                {"job": "INNER", "action": "restart"},
            ],
        ),
        (
            "move_out",
            "changed",
            "rerun",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "INNER", "action": "restart"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "rerun"},
            ],
        ),
        (
            "move_out_held",
            "changed",
            "hold",
            [
                {"job": "BX", "action": "restart_box"},
                {"job": "INNER", "action": "restart"},
                {"job": "KEEP", "action": "restart"},
                {"job": "MEMBER", "action": "hold"},
            ],
        ),
        (
            "retype",
            "removed",
            "remove",
            [
                {"job": "BX", "action": "remove"},
                {"job": "INNER", "action": "remove"},
                {"job": "KEEP", "action": "remove"},
                {"job": "MEMBER", "action": "remove"},
            ],
        ),
    ],
)
def test_box_restart_marks_unchanged_members(
    short_root: Path, case, change, action, expected_effects
):
    from dsl41.reread_worker import preview
    from dsl41.runner_control import roundtrip
    from test_main_config_restart import cli_engine
    from test_main_config import live_of
    from test_runner_control import _sync_sendevent, wait_for
    from store_surgery import rows

    path = config_file(short_root, estate='non_idempotent = "marked.toml"\n')
    marked = "MEMBER" if "held" in case else None
    (short_root / "marked.toml").write_text(
        "[jobs]\n" + (f'{marked} = "synthetic effect"\n' if marked else "")
    )
    boxes = "insert_job: BX\njob_type: b\ninsert_job: INNER\njob_type: b\nbox_name: BX\n"
    command = JOB.replace("true", "sleep 30")
    member = (JOB if case.startswith("finished") else command).replace("A", "MEMBER")
    text = boxes + member + "box_name: INNER\n" + command.replace("A", "KEEP") + "box_name: BX\n"
    jil = short_root / "a.jil"
    jil.write_text(text)
    initial = main.prepare(path)
    socket = short_root / "run" / "control.sock"
    with cli_engine(path) as ask:
        assert _sync_sendevent(socket, "FORCE_STARTJOB", job="BX")["ok"]
        wait_for(
            lambda: (
                ask("status")["jobs"]["MEMBER"]["status"]
                == ("SUCCESS" if case.startswith("finished") else "RUNNING")
            )
        )
        live = live_of(
            initial, {name: row["status"] for name, row in ask("status")["jobs"].items()}
        )
        replacement = text + JOB.replace("A", "NEW") + "box_name: INNER\n"
        replacement += JOB.replace("A", "OUTER") + "box_name: BX\n"
        if case.startswith("move_out"):
            replacement = replacement.replace(member + "box_name: INNER\n", member)
        elif case == "retype":
            replacement = JOB.replace("A", "BX")
        elif case == "in_place":
            replacement = replacement.replace(member, member.replace("sleep 30", "sleep 31"))
        jil.write_text(replacement)
        answer = roundtrip(socket, {"v": 5, "cmd": "reread"})
        assert answer["ok"], answer
        view = answer["preview"]
        effects = {item["job"]: item["action"] for item in view["effects"]}
        jobs = ask("status")["jobs"]
        assert jobs["MEMBER"]["reread"] == {"change": change, "action": action}
        forecast = preview(main.prepare(path), live)
        assert view["effects"] == expected_effects
        assert forecast["marks"]["MEMBER"] == {"change": change, "action": action}
        assert effects["MEMBER"] == action
        if change == "unchanged":
            assert "MEMBER" not in {item["name"] for item in view["changed"]}
        applied = roundtrip(
            socket,
            {
                "v": 5,
                "cmd": "update",
                "wait": True,
                "request_id": "restart-marks",
                "baseline_id": ask("status")["baseline_id"],
            },
        )
        assert applied["ok"] and applied["application"], applied
        if case in {"nested", "in_place", "held", "finished_held"}:
            stopped = [["KEEP", "none"]]
            if case != "finished_held":
                stopped.append(["MEMBER", "none"])
            assert applied["application"]["stopped"] == stopped
            recorded = [
                body
                for _, kind, body in rows(short_root / "run")
                if kind == "config" and body.get("event") == "applied" and body["rev"] == 2
            ]
            assert len(recorded) == 1 and recorded[0]["stopped"] == stopped
        if case in {"held", "finished_held"}:
            member_row = ask("status")["jobs"]["MEMBER"]
            assert member_row["on_hold"] and member_row["run_number"] == 1
        assert not any(row.get("reread") for row in ask("status")["jobs"].values())


@pytest.mark.parametrize("nested", [False, True])
def test_box_restart_holds_before_starts_in_name_order(short_root: Path, nested: bool):
    path = config_file(short_root, estate='non_idempotent = "marked.toml"\n')
    (short_root / "marked.toml").write_text(
        '[jobs]\nZ_HELD = "synthetic effect"\nA_HELD = "synthetic effect"\n'
    )
    text = (
        "insert_job: ZB\njob_type: b\ninsert_job: AB\njob_type: b\n"
        + ("box_name: ZB\n" if nested else "")
        + JOB.replace("A", "Z_HELD")
        + "box_name: AB\n"
        + JOB.replace("A", "A_HELD")
        + "box_name: ZB\n"
    )
    (short_root / "a.jil").write_text(text)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            for name in ["ZB"] if nested else ["ZB", "AB"]:
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": name}))
            await engine.run_until_quiescent(T0)
            before = engine.journal.store.last_seq()
            (short_root / "a.jil").write_text(
                text
                + JOB.replace("A", "NEW_INNER")
                + "box_name: AB\n"
                + JOB.replace("A", "NEW_OUTER")
                + "box_name: ZB\n"
            )
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "box-order")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.done()
            records = list(engine.journal.store.read_from(before))
            assert [
                (record.payload["kind"], record.payload["payload"]["job"])
                for _, record in records
                if record.kind == "input"
                and record.payload.get("kind") in {"ON_HOLD", "FORCE_STARTJOB"}
            ] == [
                ("ON_HOLD", "A_HELD"),
                ("ON_HOLD", "Z_HELD"),
                *[("FORCE_STARTJOB", name) for name in (["ZB"] if nested else ["AB", "ZB"])],
            ]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["moved_box", "unchanged_box", "in_place_running", "in_place_finished", "move_in"]
)
def test_dl490_forecast_labels_follow_candidate_membership(short_root: Path, monkeypatch, case):
    from dsl41.reread_worker import preview
    from dsl41.runner_control import ControlServer

    member = JOB.replace("A", "MEMBER")
    old = (
        "insert_job: BX\njob_type: b\ninsert_job: INNER\njob_type: b\nbox_name: BX\n"
        + JOB.replace("A", "INNER_KEEP")
        + "box_name: INNER\n"
        + JOB.replace("A", "KEEP")
        + "box_name: BX\n"
        + member
        + ("" if case == "move_in" else "box_name: INNER\n")
    )
    path = estate_file(short_root)
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        server = ControlServer(engine, short_root / "run" / "control.sock")
        try:
            for name in ["BX", "MEMBER"] if case == "move_in" else ["BX"]:
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": name}))
            await engine.run_until_quiescent(T0)
            if case == "in_place_finished":
                engine.inject(
                    Event(at=T0, kind="STATUS", payload={"job": "MEMBER", "status": "SUCCESS"})
                )
                await engine.run_until_quiescent(T0)
            new = old + JOB.replace("A", "NEW") + "box_name: BX\n"
            if case == "moved_box":
                new = new.replace("job_type: b\nbox_name: BX\n", "job_type: b\n")
            elif case.startswith("in_place"):
                new = new.replace(member, member.replace("true", "false"))
            elif case == "move_in":
                new = new.replace(member, member + "box_name: INNER\n")
            jil.write_text(new)
            candidate = main.prepare(path)
            live = engine.update.live()
            before = list(engine.journal.store.read_from(0))

            async def prepare(*, update):
                assert not update
                return {"ok": True, "preview": preview(candidate, live)}

            monkeypatch.setattr(server, "_prepare_reload", prepare)
            view = (await server._reread({"v": 5, "cmd": "reread"}))["preview"]
            effects = [("BX", "restart_box")]
            marks = {"BX": {"change": "changed", "action": "restart_box"}}
            if case == "moved_box":
                effects += [("INNER_KEEP", "rerun"), ("KEEP", "restart"), ("MEMBER", "rerun")]
                marks["INNER"] = {"change": "changed", "action": None}
            else:
                effects += [
                    ("INNER", "restart"),
                    ("INNER_KEEP", "restart"),
                    ("KEEP", "restart"),
                    ("MEMBER", "restart"),
                ]
                marks["INNER"] = {
                    "change": "changed" if case == "move_in" else "unchanged",
                    "action": "restart",
                }
                marks["INNER_KEEP"] = {"change": "unchanged", "action": "restart"}
                marks["MEMBER"] = {
                    "change": "unchanged" if case == "unchanged_box" else "changed",
                    "action": "restart",
                }
            marks["KEEP"] = {"change": "unchanged", "action": "restart"}
            assert view["effects"] == [{"job": name, "action": action} for name, action in effects]
            assert view["stop"] == [
                name for name, _ in effects if name != "MEMBER" or case != "in_place_finished"
            ]
            jobs = (await server._dispatch({"cmd": "status"}))["jobs"]
            assert {name: row["reread"] for name, row in jobs.items() if "reread" in row} == marks
            assert list(engine.journal.store.read_from(0)) == before
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["cmd_root", "fw_running", "fw_finished", "outside", "fw_finished_moved_out"]
)
def test_dl490_retyped_jobs_follow_the_new_box_scope(short_root: Path, case):
    from dsl41.reload_diff import diff
    from dsl41.reload_plan import box_scope, root_starts
    from dsl41.reread_worker import preview

    path = config_file(short_root, estate='non_idempotent = "marked.toml"\n')
    marked = short_root / "marked.toml"
    marked.write_text("[jobs]\n")
    boxes = "insert_job: BX\njob_type: b\n"
    keep = JOB.replace("A", "KEEP") + "box_name: BX\n"
    watcher = f"insert_job: MEMBER\njob_type: f\nwatch_file: {short_root}/watched\n"
    finished = JOB.replace("A", "A_FINISHED") + "box_name: BX\n" if case == "fw_finished" else ""
    old = (
        JOB.replace("A", "BX")
        if case == "cmd_root"
        else boxes + keep + watcher + ("" if case == "outside" else "box_name: BX\n") + finished
    )
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None), "FW": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            for name in ["BX", "MEMBER"] if case == "outside" else ["BX"]:
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": name}))
            await engine.run_until_quiescent(T0)
            name = "BX" if case == "cmd_root" else "MEMBER"
            if case.startswith("fw_finished"):
                engine.inject(
                    Event(at=T0, kind="STATUS", payload={"job": name, "status": "SUCCESS"})
                )
                if finished:
                    engine.inject(
                        Event(
                            at=T0, kind="STATUS", payload={"job": "A_FINISHED", "status": "SUCCESS"}
                        )
                    )
                await engine.run_until_quiescent(T0)
            row = engine.oracle.store.job[name]
            assert row.status == ("SUCCESS" if case.startswith("fw_finished") else "RUNNING")
            assert row.run_number == 1
            before = engine.journal.store.last_seq()
            if case == "cmd_root":
                new = boxes + JOB.replace("A", "NEW") + "box_name: BX\n"
            else:
                marked.write_text('[jobs]\nMEMBER = "synthetic effect"\n')
                new = boxes + keep + JOB.replace("A", "MEMBER")
                if case not in {"outside", "fw_finished_moved_out"}:
                    new += "box_name: BX\n"
                new += finished
            jil.write_text(new)
            candidate = main.prepare(path)
            live = engine.update.live()
            changes = diff(live.catalog, candidate.catalog, live.settings, candidate.settings)
            assert name in changes.added and name in changes.removed
            scope = box_scope(changes, live, candidate.catalog)
            forecast = preview(candidate, live)
            action = (
                "restart_box"
                if case == "cmd_root"
                else "remove"
                if case == "outside"
                else None
                if case == "fw_finished_moved_out"
                else "restart"
            )
            assert forecast["marks"][name] == {"change": "removed", "action": action}
            expected_effects = (
                [{"job": name, "action": action}]
                if case in {"cmd_root", "outside"}
                else [
                    {"job": "BX", "action": "restart_box"},
                    {"job": "KEEP", "action": "restart"},
                ]
                if case == "fw_finished_moved_out"
                else [
                    {"job": "BX", "action": "restart_box"},
                    {"job": "KEEP", "action": "restart"},
                    {"job": "MEMBER", "action": "restart"},
                ]
            )
            if finished:
                expected_effects.append({"job": "A_FINISHED", "action": "restart"})
            assert forecast["effects"] == expected_effects
            assert [
                effect["job"] for effect in forecast["effects"] if effect["action"] == "restart_box"
            ] == root_starts(scope, candidate.catalog, candidate.metadata["non_idempotent"])
            assert (name in forecast["stop"]) == (not case.startswith("fw_finished"))
            plan = reread(live, short_root / "run" / "plans", candidate=candidate, start=T0)
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "retyped-forecast")
            )
            await engine.run_until_quiescent(T0)
            body = answer.result().applied.result()
            assert engine.config_rev == 2
            if not case.startswith("fw_finished"):
                assert [name, "none"] in body["stopped"]
            else:
                assert name not in dict(body["stopped"])
            row = engine.oracle.store.job[name]
            assert not row.on_hold
            outside = case in {"outside", "fw_finished_moved_out"}
            assert row.status == ("INACTIVE" if outside else "RUNNING")
            assert row.run_number == (1 if outside else 2)
            if finished:
                assert engine.oracle.store.job["A_FINISHED"].run_number == 2
            records = list(engine.journal.store.read_from(before))
            inputs = [
                (record.payload.get("kind"), record.payload.get("payload", {}).get("job"))
                for _, record in records
                if record.kind == "input"
            ]
            forced = [job for kind, job in inputs if kind == "FORCE_STARTJOB"]
            assert forced == ([] if case == "outside" else ["BX"])
            if case == "cmd_root":
                assert ("KILLJOB", "BX") in inputs
                assert engine.oracle.catalog.jobs["BX"].job_type == "BOX"
                assert engine.oracle.store.job["NEW"].status == "RUNNING"
                assert inputs.index(("KILLJOB", "BX")) < inputs.index(("FORCE_STARTJOB", "BX"))
            else:
                dispatched = [
                    record.payload
                    for _, record in records
                    if record.kind == "dispatch" and record.payload["job"] == name
                ]
                assert len(dispatched) == (0 if outside else 1)
                if dispatched:
                    assert dispatched[0]["run_number"] == 2
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_dl490_a_removed_name_cannot_be_marked(short_root: Path):
    from dsl41.reload_plan import RereadRefused

    path = config_file(short_root, estate='non_idempotent = "marked.toml"\n')
    (short_root / "a.jil").write_text(JOB)
    marked = short_root / "marked.toml"
    marked.write_text('[jobs]\nA = "synthetic effect"\n')
    assert main.prepare(path).metadata["non_idempotent"] == {"A": "synthetic effect"}
    (short_root / "a.jil").write_text(JOB.replace("A", "OTHER"))
    with pytest.raises(RereadRefused, match="estate does not define"):
        main.prepare(path)


@pytest.mark.parametrize("kind", ["BOX", "CMD"])
def test_dl490_moved_out_terminator_ends_without_its_own_kill_or_start(short_root: Path, kind):
    from dsl41.reread_worker import preview

    mover = (
        "insert_job: MOVER\njob_type: b\n" if kind == "BOX" else JOB.replace("A", "MOVER")
    ) + "job_terminator: 1\n"
    old = (
        "insert_job: BX\njob_type: b\n"
        + JOB.replace("A", "KEEP")
        + "box_name: BX\n"
        + mover
        + "box_name: BX\n"
        + (JOB.replace("A", "CHILD") + "box_name: MOVER\n" if kind == "BOX" else "")
    )
    path = estate_file(short_root)
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            row = engine.oracle.store.job["MOVER"]
            assert row.status == "RUNNING" and row.run_number == 1
            before = engine.journal.store.last_seq()
            jil.write_text(old.replace(mover + "box_name: BX\n", mover))
            candidate = main.prepare(path)
            live = engine.update.live()
            forecast = preview(candidate, live)
            # BUG-69: the forecast does not read the old box's terminator cascade.
            assert forecast["marks"]["MOVER"] == {
                "change": "changed",
                "action": None if kind == "BOX" else "rerun",
            }
            assert forecast["stop"] == (
                ["BX", "CHILD", "KEEP"] if kind == "BOX" else ["BX", "KEEP", "MOVER"]
            )
            assert forecast["effects"] == (
                [
                    {"job": "BX", "action": "restart_box"},
                    {"job": "CHILD", "action": "rerun"},
                    {"job": "KEEP", "action": "restart"},
                ]
                if kind == "BOX"
                else [
                    {"job": "BX", "action": "restart_box"},
                    {"job": "KEEP", "action": "restart"},
                    {"job": "MOVER", "action": "rerun"},
                ]
            )
            plan = reread(live, short_root / "run" / "plans", candidate=candidate, start=T0)
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "moved-terminator")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.done() and engine.config_rev == 2
            row = engine.oracle.store.job["MOVER"]
            assert row.status == "TERMINATED" and row.run_number == 1
            assert engine.oracle.catalog.jobs["MOVER"].box.box_name is None
            records = list(engine.journal.store.read_from(before))
            inputs = [
                (record.payload.get("kind"), record.payload.get("payload", {}).get("job"))
                for _, record in records
                if record.kind == "input"
            ]
            assert ("KILLJOB", "BX") in inputs and ("FORCE_STARTJOB", "BX") in inputs
            assert ("KILLJOB", "MOVER") not in inputs
            assert ("FORCE_STARTJOB", "MOVER") not in inputs
            assert not [
                record
                for _, record in records
                if record.kind == "dispatch" and record.payload["job"] == "MOVER"
            ]
            if kind == "CMD":
                assert ["MOVER", "none"] in answer.result().applied.result()["stopped"]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_dl490_moved_out_box_keeps_its_run(short_root: Path):
    old = (
        "insert_job: BX\njob_type: b\ninsert_job: INNER\njob_type: b\nbox_name: BX\n"
        + JOB.replace("A", "MEMBER")
        + "box_name: INNER\n"
        + JOB.replace("A", "KEEP")
        + "box_name: BX\n"
    )
    path = estate_file(short_root)
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "BX"}))
            await engine.run_until_quiescent(T0)
            row = engine.oracle.store.job["INNER"]
            assert row.status == "RUNNING" and row.run_number == 1
            before = engine.journal.store.last_seq()
            jil.write_text(old.replace("job_type: b\nbox_name: BX\n", "job_type: b\n"))
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "moved-box")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.done() and engine.config_rev == 2
            assert engine.oracle.catalog.jobs["INNER"].box.box_name is None
            row = engine.oracle.store.job["INNER"]
            assert row.status == "RUNNING" and row.run_number == 1
            inputs = [
                (record.payload.get("kind"), record.payload.get("payload", {}).get("job"))
                for _, record in engine.journal.store.read_from(before)
                if record.kind == "input"
            ]
            assert ("KILLJOB", "BX") in inputs and ("FORCE_STARTJOB", "BX") in inputs
            assert ("KILLJOB", "INNER") not in inputs
            assert ("FORCE_STARTJOB", "INNER") not in inputs
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["in_place_running", "in_place_finished", "move_in"])
def test_dl490_changed_member_opens_a_new_box_run(short_root: Path, case):
    member = JOB.replace("A", "MEMBER")
    old = (
        "insert_job: BX\njob_type: b\n"
        + JOB.replace("A", "KEEP")
        + "box_name: BX\n"
        + member
        + ("" if case == "move_in" else "box_name: BX\n")
    )
    path = estate_file(short_root)
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            for name in ["BX", "MEMBER"] if case == "move_in" else ["BX"]:
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": name}))
            await engine.run_until_quiescent(T0)
            if case == "in_place_finished":
                engine.inject(
                    Event(at=T0, kind="STATUS", payload={"job": "MEMBER", "status": "SUCCESS"})
                )
                await engine.run_until_quiescent(T0)
            row = engine.oracle.store.job["MEMBER"]
            assert row.status == ("SUCCESS" if case == "in_place_finished" else "RUNNING")
            assert row.run_number == 1
            before = engine.journal.store.last_seq()
            new = (
                old + "box_name: BX\n"
                if case == "move_in"
                else old.replace(member, member.replace("true", "false"))
                + JOB.replace("A", "NEW")
                + "box_name: BX\n"
            )
            jil.write_text(new)
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "changed-member")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.done() and engine.config_rev == 2
            row = engine.oracle.store.job["MEMBER"]
            assert row.status == "RUNNING" and row.run_number == 2
            assert engine.oracle.store.job["BX"].run_number == 2
            records = list(engine.journal.store.read_from(before))
            inputs = [
                (record.payload.get("kind"), record.payload.get("payload", {}).get("job"))
                for _, record in records
                if record.kind == "input"
            ]
            assert (("KILLJOB", "MEMBER") in inputs) == (case != "in_place_finished")
            assert ("FORCE_STARTJOB", "BX") in inputs
            assert ("FORCE_STARTJOB", "MEMBER") not in inputs
            dispatches = [
                record.payload
                for _, record in records
                if record.kind == "dispatch" and record.payload["job"] == "MEMBER"
            ]
            assert len(dispatches) == 1
            assert dispatches[0]["run_number"] == 2 and dispatches[0].get("after") is None
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_dl490_box_kills_include_removed_and_queued_members_in_order(short_root: Path):
    resource = "resources: (LOCK, QUANTITY=1)\n"
    removed = JOB.replace("A", "REMOVED") + "box_name: ZB\n"
    old = (
        "insert_resource: LOCK\nres_type: R\namount: 1\n"
        + JOB.replace("A", "HOLDER")
        + resource
        + "insert_job: ZB\njob_type: b\ninsert_job: AB\njob_type: b\nbox_name: ZB\n"
        + JOB.replace("A", "MEMBER")
        + "box_name: AB\n"
        + removed
        + JOB.replace("A", "WAITER")
        + "box_name: AB\n"
        + resource
    )
    path = estate_file(short_root)
    jil = short_root / "a.jil"
    jil.write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            for name in ("HOLDER", "ZB"):
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": name}))
                await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["WAITER"].status == "QUE_WAIT"
            assert engine.oracle.store.job["AB"].status == "RUNNING"
            before = engine.journal.store.last_seq()
            jil.write_text(old.replace(removed, ""))
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "kill-order")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.done() and engine.config_rev == 2
            kills = [
                record.payload["payload"]["job"]
                for _, record in engine.journal.store.read_from(before)
                if record.kind == "input" and record.payload.get("kind") == "KILLJOB"
            ]
            assert kills == ["ZB", "AB", "REMOVED", "MEMBER", "WAITER"]
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_box_restart_ends_boxes_before_done_sibling_can_start(short_root: Path):
    old = (
        "insert_job: ZB\njob_type: b\ninsert_job: AB\njob_type: b\nbox_name: ZB\n"
        + JOB.replace("A", "A1")
        + "box_name: AB\n"
        + JOB.replace("A", "B1")
        + "box_name: AB\ncondition: d(A1)\n"
    )
    path = estate_file(short_root)
    (short_root / "a.jil").write_text(old)
    initial = main.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            short_root / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
        )
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "ZB"}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["B1"].status == "INACTIVE"
            (short_root / "a.jil").write_text(old + JOB.replace("A", "NEW") + "box_name: ZB\n")
            candidate = main.prepare(path)
            plan = reread(
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=candidate,
                start=T0,
            )
            engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "ordered"))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["B1"].status == "INACTIVE"
            records = list(engine.journal.store.read_from(0))
            kills = [
                record.payload["payload"]["job"]
                for _, record in records
                if record.kind == "input" and record.payload.get("kind") == "KILLJOB"
            ]
            assert kills[:2] == ["ZB", "AB"]
            force = max(
                seq
                for seq, record in records
                if record.kind == "input" and record.payload.get("kind") == "FORCE_STARTJOB"
            )
            assert not [
                record
                for seq, record in records
                if seq < force and record.kind == "dispatch" and record.payload["job"] == "B1"
            ]
            engine.inject(Event(at=T0, kind="STATUS", payload={"job": "A1", "status": "SUCCESS"}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["ZB"].run_number == 2
            assert engine.oracle.store.job["AB"].run_number == 2
            assert engine.oracle.store.job["B1"].status == "RUNNING"
            assert engine._live["B1"].run_number == 1
            starts = [
                seq
                for seq, record in engine.journal.store.read_from(0)
                if record.kind == "dispatch" and record.payload["job"] == "B1"
            ]
            assert len(starts) == 1 and starts[0] > force
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_launch_policy_from_main_config_and_shell_update(tmp_path):
    import resource
    import shutil
    from datetime import datetime
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_preflight import preflight
    from test_engine_stops import _finish, _start_jobs, _until

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("real bash unavailable")
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < 64:
        pytest.skip("hard descriptor limit below witness")
    site, profile = tmp_path / "site's profile", tmp_path / "job's profile"
    site.write_text("export ORDER=site\n")
    profile.write_text('export ORDER="$ORDER:job"\n')
    output = tmp_path / "observed"
    output.write_text("old\n")
    command = (
        'if [ -n "${BASH_VERSION-}" ] && ! shopt -oq posix; '
        "then native=bash; else native=sh; fi; "
        'printf "%s:%s:%s:%s\\n" "$native" "$ORDER" "$(ulimit -n)" "$DSL41_ATTEMPT"; '
        'test "$DSL41_ATTEMPT" -eq 2'
    )
    (tmp_path / "a.jil").write_text(
        f"insert_job: A\njob_type: c\ncommand: {command}\nprofile: {profile}\n"
        f'ulimit: n="64,64"\nstd_out_file: {output}\nn_retrys: 1\n'
    )

    def config(shell):
        return config_file(
            tmp_path,
            engine=(
                f"job_shell = {json.dumps(shell)}\njob_profile = {json.dumps(str(site))}\n"
                'std_file_mode = "truncate"\n'
            ),
        )

    path = config(bash)
    initial = main.prepare(path)
    assert not main.policy_report(initial)[1].by_code("known-unsupported")
    findings = preflight(
        initial.catalog, job_shell=bash, job_profile=str(site), std_file_mode="truncate"
    )
    assert not [f for f in findings if f.code == "known-unsupported" or f.severity == "ERROR"]
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            initial.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter()},
            launched=main.profile_of(initial),
            source_capture=main.encoded_parts(initial),
            hold_open=True,
        )
        engine.launch_settings = initial.metadata["launch"]
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            _start_jobs(engine, "A")
            await _until(lambda: engine.oracle.store.job["A"].status == "SUCCESS")
            assert output.read_text() == "bash:site:job:64:2\n"
            assert (root / "runs/A.1/2/status.json").exists()
            config("/bin/sh")
            candidate = main.prepare(path)
            assert not main.policy_report(candidate)[1].by_code("known-unsupported")
            plan = reread(engine.update.live(), root / "plans", candidate=candidate)
            assert not [
                f
                for f in json.loads((root / "plans" / plan.token / "plan.json").read_text())[
                    "lint_findings"
                ]
                if f["code"] == "known-unsupported"
            ]
            answer = await engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "launch-shell")
            )
            assert isinstance(answer.decision, Admitted)
            await answer.applied
            _start_jobs(engine, "A")
            await _until(
                lambda: (
                    engine.oracle.store.job["A"].run_number == 2
                    and engine.oracle.store.job["A"].status == "SUCCESS"
                )
            )
            assert output.read_text() == "sh:site:job:64:2\n"
            assert engine.profile.job_shell == "/bin/sh"
        finally:
            await _finish(engine, loop)

    asyncio.run(scenario())


def test_shell_removed_after_preflight_is_recorded_spawn_failure(tmp_path):
    from datetime import datetime
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_clock import RealClock
    from test_engine_stops import _finish, _start_jobs, _until

    shell = tmp_path / "sh"
    shell.symlink_to("/bin/sh")
    path = estate_file(tmp_path, engine=f'job_shell = "{shell}"\n')
    estate = main.prepare(path)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            estate.catalog,
            root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter()},
            launched=main.profile_of(estate),
            source_capture=main.encoded_parts(estate),
            hold_open=True,
        )
        shell.unlink()
        loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
        try:
            _start_jobs(engine, "A")
            await _until(lambda: engine.oracle.store.job["A"].status == "FAILURE")
            status = json.loads((root / "runs/A.1/1/status.json").read_text())
            assert status["outcome"] == "spawn_failed" and str(shell) in status["error"]
            assert not (root / "runs/A.1/1/spawn.json").exists()
            assert not loop.done()
        finally:
            await _finish(engine, loop)

    asyncio.run(scenario())
