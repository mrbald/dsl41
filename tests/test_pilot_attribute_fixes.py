"""Admission, hook policy and revisioned watcher defaults (DL-469)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

import pytest

from dsl41 import main_config as main
from dsl41.ir import CatalogIR, LoweringError, lower_source
from dsl41.lint import rule_known_unsupported
from dsl41.reload_diff import definition_hash
from dsl41.reload_plan import RereadRefused, reread
from dsl41.runner_adapters import FakeAdapter, read_watch_log
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_startup import active_configuration, resume_run, start_run, wire_from_profile
from dsl41.runner_update import UpdateRequest, settings_of
from dsl41.status_hook import HookSettings, StatusHooks, payload
from test_main_config import JOB, config_file, estate_file
from test_status_hook import T0, Capture, feed, make_engine

pytestmark = pytest.mark.usefixtures("cli_env")


def test_box_and_fw_retry_admission_warning_and_fail_sweep():
    from dsl41.rehearse_check import expected_bounds, run_fail_sweep
    from dsl41.derive import derive_graph
    from dsl41.runner_preflight import preflight

    for kind, execution in (("b", ""), ("f", "watch_file: /tmp/synthetic-watch")):
        for count in (1, 20):
            catalog = lower_source(
                f"insert_job: A\njob_type: {kind}\n{execution}\nn_retrys: {count}\n"
            )
            findings = preflight(catalog)
            assert not [f for f in findings if f.severity == "ERROR"]
            assert [f.detail for f in rule_known_unsupported(catalog)] == ["n_retrys"]
        zero = lower_source(f"insert_job: A\njob_type: {kind}\n{execution}\nn_retrys: 0\n")
        assert not rule_known_unsupported(zero)
        for bad in ("21", "-1", "1.5", "no"):
            try:
                catalog = lower_source(
                    f"insert_job: A\njob_type: {kind}\n{execution}\nn_retrys: {bad}\n"
                )
            except LoweringError:
                continue
            assert any(f.code == "n-retrys" and f.severity == "ERROR" for f in preflight(catalog))
    catalog = lower_source("insert_job: A\njob_type: f\nwatch_file: /tmp/synthetic\nn_retrys: 2\n")
    graph = derive_graph(catalog)
    # A scripted FW is reached rather than parked; retries remain inert for this job type.
    adapter = FakeAdapter({("A", 1): (0, 0)})
    bounds = expected_bounds(catalog, graph, {}, parked=set())
    cases, _ = run_fail_sweep(
        catalog,
        {"A": 1},
        bounds,
        adapter,
        [],
        start=T0,
        horizon=T0 + timedelta(hours=1),
        producers=["A"],
        parked=set(),
    )
    assert cases[0].outcome == "ran"


@pytest.mark.parametrize("attribute", ["alarm_if_fail", "alarm_if_terminated", "max_run_alarm"])
def test_hook_attributes_stop_known_unsupported_warning(attribute):
    catalog = lower_source(JOB + f"{attribute}: 1\n")
    assert not rule_known_unsupported(catalog)
    from dsl41.reload_plan import estate_settings

    expected = {
        "alarm_if_fail": "sha256:810b03f17f0dda8ce9a968e9c1961b7add7c74f9748f7617d4db3054eafa9794",
        "alarm_if_terminated": "sha256:32afeabd582fb28e754cb7ff4507161fbca0c9f17692c9bd28c248ee39a40a59",
        "max_run_alarm": "sha256:9d137d001608239bdea3c37cec932e4b564e79a513b855404f57e3f9983ef383",
    }
    assert (
        definition_hash(catalog.jobs["A"], catalog, estate_settings(None, None, {}, catalog))
        == expected[attribute]
    )


@pytest.mark.parametrize("flag,expected", [("", True), ("y", True), ("n", False)])
def test_failure_and_termination_flags_are_postcommit_and_nonraising(
    tmp_path, monkeypatch, flag, expected
):
    async def scenario():
        attrs = "send_notification: y\n"
        if flag:
            attrs += f"alarm_if_fail: {flag}\nalarm_if_terminated: {flag}\n"
        # Admission is already validated with the site's configured hook.
        catalog = lower_source(JOB + attrs)
        engine = start_run(
            catalog,
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            hook_configured=True,
        )
        hooks = StatusHooks(
            engine, HookSettings(status_hook="/missing", status_hook_long_running_s=1)
        )
        engine.status_hooks = hooks
        events = []

        def enqueue(event, **values):
            assert engine.journal.store.last_seq() > 0
            events.append((event, values))

        monkeypatch.setattr(hooks.delivery, "enqueue", enqueue)
        try:
            await feed(engine, "STATUS", job="A", status="RUNNING")
            await feed(engine, "STATUS", 1, job="A", status="FAILURE")
            await feed(engine, "STATUS", 2, job="A", status="RUNNING")
            await feed(engine, "KILLJOB", 3, job="A")
            await feed(engine, "STATUS", 4, job="A", status="RUNNING")
            await engine.clock.wait_until(T0 + timedelta(seconds=5))
            hooks.check_long_running()
            assert [e for e, _ in events] == ["job_failure", "job_terminated", "job_long_running"]
            assert events[0][1]["alarm_if_fail"] is expected
            assert events[1][1]["alarm_if_terminated"] is expected
            assert events[1][1]["exit_code"] is None
            hooks.check_long_running()
            assert len(events) == 3
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_hook_payload_always_has_nullable_alarm_keys():
    for event in (
        "job_failure",
        "job_terminated",
        "alarm",
        "job_hold",
        "job_long_running",
        "engine_down",
        "engine_refused",
        "future_event",
    ):
        row = json.loads(payload(event, None))
        assert row["v"] == 1
        assert row["alarm_if_fail"] is (True if event == "job_failure" else None)
        assert row["alarm_if_terminated"] is (True if event == "job_terminated" else None)


def test_per_job_thresholds_update_fallback_missing_jobs_and_bad_annotations(monkeypatch):
    async def scenario():
        text = JOB + "max_run_alarm: 1\n" + JOB.replace("A", "B") + "max_run_alarm: 0\n"
        engine, _ = make_engine(text)
        hooks = StatusHooks(engine, HookSettings(status_hook="/missing"))
        engine.status_hooks = hooks
        events = []
        monkeypatch.setattr(
            hooks.delivery, "enqueue", lambda event, **kw: events.append((event, kw))
        )
        try:
            await feed(engine, "STATUS", job="A", status="RUNNING")
            await feed(engine, "STATUS", job="B", status="RUNNING")
            await feed(engine, "STATUS", job="ghost", status="RUNNING")
            await engine.clock.wait_until(T0 + timedelta(seconds=59))
            hooks.check_long_running()
            assert not events
            hooks.threshold = 120
            await engine.clock.wait_until(T0 + timedelta(seconds=60))
            hooks.check_long_running()
            assert [(kw["job"], kw["threshold_s"]) for _, kw in events] == [("A", 60)]
            # B gains a per-job threshold after the hook monitor was constructed.
            engine.oracle.catalog.jobs["B"].annotations["max_run_alarm"] = "1"
            hooks.check_long_running()
            assert [kw["job"] for _, kw in events] == ["A", "B"]
            await feed(engine, "STATUS", 61, job="A", status="SUCCESS")
            await feed(engine, "STATUS", 61, job="A", status="RUNNING")
            engine.oracle.catalog.jobs["A"].annotations["max_run_alarm"] = "invalid"
            engine.oracle.catalog.jobs["A"].annotations["alarm_if_fail"] = "invalid"
            engine.oracle.catalog.jobs["A"].annotations["alarm_if_terminated"] = "invalid"
            hooks.threshold = 2
            await engine.clock.wait_until(T0 + timedelta(seconds=63))
            hooks.check_long_running()
            assert events[-1][1]["threshold_s"] == 2
            await feed(engine, "STATUS", 64, job="A", status="FAILURE")
            assert events[-1][1]["alarm_if_fail"] is True
            await feed(engine, "STATUS", 65, job="A", status="TERMINATED")
            assert events[-1][1]["alarm_if_terminated"] is True
            await feed(engine, "STATUS", 65, job="ghost", status="FAILURE")
            assert events[-1][1]["alarm_if_fail"] is True
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


def test_interval_revision_update_restart_and_unchanged_hashes(tmp_path):
    path = config_file(tmp_path, engine="fw_default_interval_s = 7\n")
    text = "insert_job: A\njob_type: f\nwatch_file: /tmp/synthetic-absent-a\n"
    text += text.replace("A", "B").replace("absent-a", "absent-b")
    (tmp_path / "a.jil").write_text(text)
    initial = main.prepare(path)
    profile = main.profile_of(initial)
    assert profile.fw_default_interval_us == 7_000_000
    assert settings_of(profile, initial.catalog) == initial.settings
    root = tmp_path / "run"

    async def scenario():
        clock = VirtualClock(start=T0)
        wires = wire_from_profile(initial.catalog, profile, start=T0)
        engine = start_run(
            initial.catalog,
            root,
            clock=clock,
            adapters=wires.adapters,
            scheduler=wires.scheduler,
            launched=profile,
            source_capture=main.encoded_parts(initial),
        )
        try:
            await feed(engine, "FORCE_STARTJOB", job="A")
            assert (
                ControlServer(engine, root / "control.sock")._fw_watching(
                    engine.oracle.catalog.jobs["A"]
                )["interval"]
                == 7
            )
            config_file(tmp_path, engine="fw_default_interval_s = 11\n")
            candidate = main.prepare(path)
            plan = reread(engine.update.live(), root / "plans", candidate=candidate, start=T0)
            assert plan.plan.changed == () and plan.stop == ()
            for name in initial.catalog.jobs:
                assert definition_hash(
                    initial.catalog.jobs[name], initial.catalog, initial.settings
                ) == (
                    definition_hash(
                        candidate.catalog.jobs[name], candidate.catalog, candidate.settings
                    )
                )
            answer = engine.request_update(
                UpdateRequest(plan.token, 1, engine.baseline_id, "interval")
            )
            await engine.run_until_quiescent(T0)
            assert answer.result().applied.result()["rev"] == 2
            assert engine.profile.fw_default_interval_us == 11_000_000
            assert (
                ControlServer(engine, root / "control.sock")._fw_watching(
                    engine.oracle.catalog.jobs["A"]
                )["interval"]
                == 7
            )
            await feed(engine, "FORCE_STARTJOB", job="B")
            assert (
                ControlServer(engine, root / "control.sock")._fw_watching(
                    engine.oracle.catalog.jobs["B"]
                )["interval"]
                == 11
            )
            assert engine.watching_interval("not_live") is None
            assert active_configuration(root).profile.fw_default_interval_us == 11_000_000
            await engine.run_until_quiescent(T0 + timedelta(seconds=7))
            assert read_watch_log(Path(engine.dispatches["A"]["spool"])).last_at == T0 + timedelta(
                seconds=7
            )
            assert read_watch_log(Path(engine.dispatches["B"]["spool"])).last_at == T0
            await engine.run_until_quiescent(T0 + timedelta(seconds=11))
            assert read_watch_log(Path(engine.dispatches["B"]["spool"])).last_at == T0 + timedelta(
                seconds=11
            )
        finally:
            await engine.shutdown()
            engine.journal.close()
        applied = active_configuration(root)
        clock = VirtualClock(start=T0 + timedelta(seconds=12))
        wires = wire_from_profile(applied.catalog, applied.profile, start=clock.now())
        resumed = await resume_run(
            applied.catalog,
            root,
            clock=clock,
            adapters=wires.adapters,
            scheduler=wires.scheduler,
            declared=applied.profile,
        )
        try:
            await resumed.run_until_quiescent(clock.now())
            assert resumed.watching_interval("A") == 11
            assert resumed.watching_interval("B") == 11
            await resumed.run_until_quiescent(T0 + timedelta(seconds=18))
            assert read_watch_log(Path(resumed.dispatches["A"]["spool"])).last_at == T0 + timedelta(
                seconds=18
            )
            plan = reread(
                resumed.update.live(),
                root / "plans",
                candidate=main.prepare(path),
                start=clock.now(),
            )
            assert plan.plan.changed == () and plan.stop == ()
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_default_interval_preserves_main_hash_and_explicit_job_interval(tmp_path):
    path = estate_file(tmp_path)
    ordinary = main.prepare(path)
    expected = definition_hash(ordinary.catalog.jobs["A"], ordinary.catalog, ordinary.settings)
    assert expected == "sha256:7f82dc011e94799affe01f5e8cba3362525b56923ecbd71e02974349a1ab162a"
    config_file(tmp_path, engine="fw_default_interval_s = 7\n")
    configured = main.prepare(path)
    assert (
        definition_hash(configured.catalog.jobs["A"], configured.catalog, configured.settings)
        == expected
    )
    config_file(tmp_path, engine="fw_default_interval_s = 60\n")
    explicit = main.prepare(path)
    assert explicit.settings.keys() == ordinary.settings.keys()
    assert "fw_default_interval_s" not in ordinary.settings
    assert (
        definition_hash(explicit.catalog.jobs["A"], explicit.catalog, explicit.settings) == expected
    )
    for value in ("0", "-1", "1.5", "true", '"7"'):
        config_file(tmp_path, engine=f"fw_default_interval_s = {value}\n")
        with pytest.raises(RereadRefused):
            main.prepare(path)
    catalog = lower_source(
        "insert_job: W\njob_type: f\nwatch_file: /tmp/synthetic\nwatch_interval: 3\n"
    )

    async def scenario():
        clock = VirtualClock(start=T0)
        from dsl41.runner_adapters import FileWatcherAdapter

        engine = start_run(
            catalog, None, clock=clock, adapters={"FW": FileWatcherAdapter(default_interval_s=7)}
        )
        try:
            await feed(engine, "FORCE_STARTJOB", job="W")
            assert engine.watching_interval("W") == 3
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [-1, 527041])
def test_runtime_limit_rejects_new_sources_but_restores_stored_catalog(tmp_path, bad):
    with pytest.raises(LoweringError, match="term_run_time"):
        lower_source(JOB + f"term_run_time: {bad}\n")
    for value in (0, 527040):
        assert (
            lower_source(JOB + f"term_run_time: {value}\n").jobs["A"].sem.term_run_time_min == value
        )
    path = estate_file(tmp_path)
    valid = main.prepare(path)
    legacy = valid.catalog.model_copy(deep=True)
    legacy.jobs["A"].sem.term_run_time_min = bad
    legacy = CatalogIR.model_validate_json(legacy.model_dump_json())
    source_name = next(r["name"] for r in valid.metadata["records"] if r["role"] == "jil")
    parts = tuple(
        (name, data + f"term_run_time: {bad}\n".encode() if name == source_name else data)
        for name, data in valid.parts
    )
    # Source-display bytes and the applied catalog are retained independently (DL-468).
    estate = replace(valid, catalog=legacy, parts=parts)
    capture = main.encoded_parts(estate)
    root = tmp_path / "run"

    async def scenario():
        engine = start_run(
            legacy,
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=main.profile_of(valid),
            source_capture=capture,
        )
        await engine.shutdown()
        engine.journal.close()
        stored = active_configuration(root)
        assert stored.catalog.jobs["A"].sem.term_run_time_min == bad
        assert main.decoded_parts(capture, applied=stored).catalog == legacy
        resumed = await resume_run(
            legacy,
            root,
            clock=VirtualClock(start=T0 + timedelta(seconds=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            declared=stored.profile,
        )
        try:
            (tmp_path / "a.jil").write_text(JOB + f"term_run_time: {bad}\n")
            with pytest.raises(RereadRefused, match="term_run_time"):
                main.prepare(path)
            assert resumed.oracle.catalog.jobs["A"].sem.term_run_time_min == bad
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_update_box_restart_emits_termination(tmp_path):
    path = estate_file(tmp_path)
    box = "insert_job: BX\njob_type: b\n"
    (tmp_path / "a.jil").write_text(box + JOB + "box_name: BX\n")
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
        capture = Capture()
        engine.status_hooks = capture
        try:
            await feed(engine, "FORCE_STARTJOB", job="BX")
            (tmp_path / "a.jil").write_text(
                box + JOB + "box_name: BX\n" + JOB.replace("A", "B") + "box_name: BX\n"
            )
            plan = reread(
                engine.update.live(),
                root / "plans",
                candidate=main.prepare(path),
                start=T0,
            )
            result = engine.request_update(UpdateRequest(plan.token, 1, engine.baseline_id, "box"))
            await engine.run_until_quiescent(T0)
            assert result.result().code is None
            for _ in range(10):
                await asyncio.sleep(0)
                await engine.run_until_quiescent(T0)
                if result.result().applied.done():
                    break
            assert result.result().applied.done()
            assert any(
                row.event == "job_terminated"
                and row.job == "BX"
                and row.alarm_if_terminated is True
                for row in capture.rows
            )
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())
