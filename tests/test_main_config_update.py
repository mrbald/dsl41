"""Captured main-config plans through the unchanged update machine (DL-432, DL-437)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from dsl41 import main_config as main
from dsl41.oracle_state import Event
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted, Refused
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import replay_store
from dsl41.runner_startup import active_configuration, resume_run, start_run
from dsl41.runner_store import Record
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
