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
            request = UpdateRequest(plan.token, False, 1, engine.baseline_id, "main-update")
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
            stale = engine.request_update(
                UpdateRequest(plan.token, False, 1, engine.baseline_id, "stale")
            )
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
                UpdateRequest(plan.token, True, 2, engine.baseline_id, "draining")
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
            unconsented = engine.request_update(
                UpdateRequest(result.token, False, 1, engine.baseline_id, "no-consent")
            )
            await engine.run_until_quiescent(T0)
            assert unconsented.result().decision.code == "would_stop_running"
            request = UpdateRequest(result.token, True, 1, engine.baseline_id, "stopping")
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


def test_09_running_box_is_refused_before_plan_publication(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text("insert_job: BX\njob_type: b\n" + JOB + "box_name: BX\n")
    initial = main.prepare(path)
    from test_main_config import live_of

    (tmp_path / "a.jil").write_text(
        (tmp_path / "a.jil").read_text() + JOB.replace("A", "B") + "box_name: BX\n"
    )
    with pytest.raises(main.RereadRefused, match="box_running BX"):
        reread(
            tmp_path,
            live_of(initial, {"BX": "RUNNING"}),
            tmp_path / "plans",
            candidate=main.prepare(path),
        )
    assert not (tmp_path / "plans").exists()


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
                engine.request_update(
                    UpdateRequest(plan.token, False, 1, engine.baseline_id, "old-build")
                )
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
