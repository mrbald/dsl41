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
            plan = reread(
                tmp_path, engine.update.live(), root / "plans", candidate=candidate, start=T0
            )
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
            result = reread(
                tmp_path, engine.update.live(), root / "plans", candidate=candidate, start=T0
            )
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
        tmp_path,
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


@pytest.mark.parametrize("version", [1, 2])
def test_older_build_crash_after_config_row_resumes_without_another_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    from dsl41.reload_plan import load_estate

    path = estate_file(tmp_path)
    jobs = JOB + "\ninsert_job: B\njob_type: c\ncommand: true\ncondition: s(A)\n"
    (tmp_path / "a.jil").write_text(jobs)
    (tmp_path / "estate.toml").write_text('name = "boundary"\nfiles = ["a.jil"]\n')
    initial = main.prepare(path) if version == 2 else load_estate(tmp_path)
    profile = main.profile_of(initial) if version == 2 else RuntimeProfile()
    capture = main.encoded_parts(initial) if version == 2 else {}
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
            candidate = main.prepare(path) if version == 2 else load_estate(tmp_path)
            plan = reread(
                tmp_path, engine.update.live(), root / "plans", candidate=candidate, start=T0
            )
            append = engine.journal.append

            def old_writer(store, configuration, rev):
                payload = json.loads(configuration.to_bytes())
                payload.pop("job_order")
                if version == 1:
                    payload.pop("configuration_format_version")
                else:
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
            stored.catalog if version == 2 else candidate.catalog,
            root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            declared=stored.profile,
            stored_sources=version == 2,
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
            plan = reread(tmp_path, engine.update.live(), root / "plans", candidate=candidate)
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
                tmp_path,
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
                roundtrip, server.path, {"v": 4, "cmd": cmd, **fields}, timeout=10
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


@pytest.mark.parametrize("crash", [None, "before_kill", "after_apply"])
def test_box_restart_commits_kills_and_holds_finished_marked_members(
    short_root: Path, monkeypatch, crash
) -> None:
    from dsl41.runner_update import settings_of

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

    async def scenario():
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
                short_root,
                engine.update.live(),
                short_root / "run" / "plans",
                candidate=candidate,
                start=T0,
            )
            answer = engine.request_update(
                UpdateRequest(plan.token, engine.config_rev, engine.baseline_id, "box-update")
            )
            if crash:

                def cut():
                    raise EngineError("cut during box restart")

                monkeypatch.setattr(
                    engine.update, "box_ends" if crash == "before_kill" else "box_starts", cut
                )
                with pytest.raises(EngineError, match="cut during box restart"):
                    await engine.run_until_quiescent(T0)
                assert engine.config_rev == (1 if crash == "before_kill" else 2)
                assert engine.oracle.store.job["BX"].status == (
                    "RUNNING" if crash == "before_kill" else "TERMINATED"
                )
                assert not engine.oracle.store.job["DONE"].on_hold
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
                if crash == "after_apply":
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
            if not remove:
                assert len(view["added"]) + len(view["changed"]) + view["unchanged"] == len(
                    candidate.catalog.jobs
                )
                assert {effect["job"]: effect["action"] for effect in view["effects"]}[
                    "INNER"
                ] == "restart_box"
            plan = reread(
                short_root,
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
        view = roundtrip(socket, {"cmd": "reread", "v": 4})["preview"]
        if case != "retype":
            assert {item["job"]: item["action"] for item in view["effects"]}["MOVER"] == (
                "hold" if marked else "rerun"
            )
            assert view["stop"] == ["BX", "MOVER", "STAY"]
        applied = roundtrip(
            socket,
            {
                "cmd": "update",
                "v": 4,
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
    "case,change,action",
    [
        ("nested", "unchanged", "restart"),
        ("held", "unchanged", "hold"),
        ("finished", "unchanged", "restart"),
        ("finished_held", "unchanged", "hold"),
        ("move_out", "changed", "rerun"),
        ("move_out_held", "changed", "hold"),
        ("retype", "removed", "remove"),
    ],
)
def test_box_restart_marks_unchanged_members(short_root: Path, case, change, action):
    from dsl41.runner_control import roundtrip
    from test_main_config_restart import cli_engine
    from test_runner_control import _sync_sendevent, wait_for

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
    socket = short_root / "run" / "control.sock"
    with cli_engine(path) as ask:
        assert _sync_sendevent(socket, "FORCE_STARTJOB", job="BX")["ok"]
        wait_for(
            lambda: (
                ask("status")["jobs"]["MEMBER"]["status"]
                == ("SUCCESS" if case.startswith("finished") else "RUNNING")
            )
        )
        replacement = text + JOB.replace("A", "NEW") + "box_name: INNER\n"
        replacement += JOB.replace("A", "OUTER") + "box_name: BX\n"
        if case.startswith("move_out"):
            replacement = replacement.replace(member + "box_name: INNER\n", member)
        elif case == "retype":
            replacement = JOB.replace("A", "BX")
        jil.write_text(replacement)
        answer = roundtrip(socket, {"v": 4, "cmd": "reread"})
        assert answer["ok"], answer
        view = answer["preview"]
        effects = {item["job"]: item["action"] for item in view["effects"]}
        jobs = ask("status")["jobs"]
        assert jobs["MEMBER"]["reread"] == {"change": change, "action": action}
        assert effects["MEMBER"] == action
        if change == "unchanged":
            assert "MEMBER" not in {item["name"] for item in view["changed"]}
        applied = roundtrip(
            socket,
            {
                "v": 4,
                "cmd": "update",
                "wait": True,
                "request_id": "restart-marks",
                "baseline_id": ask("status")["baseline_id"],
            },
        )
        assert applied["ok"] and applied["application"], applied
        assert not any(row.get("reread") for row in ask("status")["jobs"].values())


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
                short_root,
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
