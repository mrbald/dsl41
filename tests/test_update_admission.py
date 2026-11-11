"""Update admission precedence and write boundaries (DL-463, DL-478)."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace

import pytest
from reload_fixtures import prepare_estate, write_config

from dsl41 import main_config as main
from dsl41.canon import canonical_bytes, decode
from dsl41.oracle_state import Event
from dsl41.reload_diff import catalog_hash_v2
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted, Refused
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import oracle_rows
from dsl41.runner_startup import start_run
from dsl41.runner_update import UpdateAnswer, UpdateRequest
from test_update_wiring import T0


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMISSION_LOG_DIR", str(tmp_path))
    estate = tmp_path / "estate"
    write_config(estate)
    (estate / "jobs.jil").write_text(
        "insert_job: a\njob_type: c\ncommand: true\nstd_out_file: $ADMISSION_LOG_DIR/out\n"
    )
    prepared = prepare_estate(estate)
    engine = start_run(
        prepared.catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        launched=main.profile_of(prepared),
        source_capture=main.encoded_parts(prepared),
    )
    token = reread(engine.update.live(), tmp_path / "run/plans", candidate=prepared).token
    try:
        yield engine, UpdateRequest(token, 1, engine.baseline_id, "candidate")
    finally:
        assert engine.journal is not None
        engine.journal.close()


def _busy(engine, monkeypatch):
    engine.oracle.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "a"}))
    estate = engine.run_root.parent / "estate"
    path = estate / "jobs.jil"
    path.write_text(path.read_text().replace("command: true", "command: echo changed"))
    plan = reread(engine.update.live(), engine.run_root / "plans", candidate=prepare_estate(estate))
    monkeypatch.setattr(engine.update, "stop_attempt", lambda job, cause: None)
    request = UpdateRequest(plan.token, 1, engine.baseline_id, "open")
    answer = engine.update.admit(request)
    assert answer.decision == Admitted(plan.token, ("a",), frozenset({"a"}), (("a", 1),))
    assert engine.update.view() == {"token": plan.token, "stopping": ["a"]}
    return request, answer


def _without_writes(engine, request, monkeypatch):
    store = engine.journal.store
    before = (
        list(store.read_from(0)),
        store.current_config_rev(),
        store.get_config(engine.config_rev),
        canonical_bytes(oracle_rows(engine.oracle)),
        engine.update.view(),
    )

    def write(*args, **kwargs):
        pytest.fail("refusal, equality or exact retry wrote to the store")

    with monkeypatch.context() as guard:
        for method in ("append", "put_config", "put_snapshot"):
            guard.setattr(store, method, write)
        answer = engine.update.admit(request)
    assert (
        list(store.read_from(0)),
        engine.config_rev,
        store.get_config(engine.config_rev),
        canonical_bytes(oracle_rows(engine.oracle)),
        engine.update.view(),
    ) == before
    return answer


@pytest.mark.parametrize(
    "gate", ["drain", "busy", "read", "damaged", "expect", "digest", "preflight", "equal"]
)
def test_admission_precedence_and_no_writes(candidate, monkeypatch, gate):
    engine, request = candidate

    async def scenario():
        if gate in {"drain", "busy"}:
            _busy(engine, monkeypatch)
        if gate == "drain":
            monkeypatch.setattr(Engine, "draining", property(lambda self: True))
        plan_dir = engine.run_root / "plans" / request.token
        if gate in {"drain", "busy", "read"}:
            shutil.rmtree(plan_dir)
        else:
            path = plan_dir / "plan.json"
            document = decode(path.read_bytes())
            if gate == "damaged":
                del document["estate_dir"]
            if gate in {"damaged", "digest"}:
                document["live_digest"] = "sha256:0"
            path.write_bytes(canonical_bytes(document) + b"\n")
        if gate in {"drain", "busy", "read", "damaged", "expect"}:
            request_now = replace(request, expect=0)
        else:
            request_now = request
        if gate != "equal":
            del engine.startup_env["ADMISSION_LOG_DIR"]
        answer = _without_writes(engine, request_now, monkeypatch)
        expected = {
            "drain": UpdateAnswer(
                code="engine_draining",
                error="the engine is draining: it takes no update until it restarts",
            ),
            "busy": UpdateAnswer(
                Refused(request.token, "update_in_progress", (engine.update.token,))
            ),
            "read": UpdateAnswer(
                code="invalid_argument",
                error=f"no complete plan {request.token} under {engine.run_root / 'plans'}: run reread first",
            ),
            "damaged": UpdateAnswer(
                code="stale_plan",
                error=f"plan {request.token} predates this engine: run update again",
            ),
            "expect": UpdateAnswer(Refused(request.token, "stale_plan")),
            "digest": UpdateAnswer(Refused(request.token, "stale_plan")),
            "preflight": UpdateAnswer(
                code="invalid_argument",
                error="preflight ERROR [runtime-variable]: job a: std_out_file references"
                " unavailable variable ADMISSION_LOG_DIR; paths resolve before profile execution;"
                " set engine.job_env.ADMISSION_LOG_DIR.",
            ),
            "equal": UpdateAnswer(
                Admitted(request.token, (), frozenset()),
                unchanged={"rev": 1, "catalog_hash": catalog_hash_v2(engine.oracle.catalog)},
            ),
        }
        assert answer == expected[gate]
        assert answer.applied is None
        assert plan_dir.exists() == (gate not in {"drain", "busy", "read", "equal"})

    asyncio.run(scenario())


@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("collision", [None, "baseline_id", "claimed_actor"])
def test_exact_retry_precedes_current_admission_conditions(
    candidate, monkeypatch, applied, collision
):
    engine, _ = candidate

    async def scenario():
        request, original = _busy(engine, monkeypatch)
        if applied:
            engine.update.ended("a")
            await engine.run_until_quiescent(T0)
            assert original.applied.done()
        monkeypatch.setattr(Engine, "draining", property(lambda self: True))
        shutil.rmtree(engine.run_root / "plans" / request.token)
        del engine.startup_env["ADMISSION_LOG_DIR"]
        retry = replace(request, token="0" * 64, expect=0)
        if collision:
            retry = replace(retry, **{collision: "other"})
        answer = _without_writes(engine, retry, monkeypatch)
        if collision:
            assert answer == UpdateAnswer(
                code="request_id_reused",
                error="request_id 'open' was admitted for a different command: reuse an id only"
                " for an exact retry",
            )
        else:
            assert answer.decision == original.decision
            assert answer.code is None and answer.error is None and answer.unchanged is None
            assert answer.applied.done() == applied
            assert answer.application == (original.applied.result() if applied else None)
            if applied:
                assert answer.applied.result() == original.applied.result()
            else:
                assert engine.update.stopping
                monkeypatch.setattr(Engine, "draining", property(lambda self: False))
                engine.update.ended("a")
                await engine.run_until_quiescent(T0)
                assert await answer.applied == original.applied.result()

    asyncio.run(scenario())


@pytest.mark.parametrize("later_gate", ["equal", "changed", "preflight"])
def test_plan_revision_is_checked_before_preflight_and_equality(candidate, monkeypatch, later_gate):
    engine, request = candidate
    estate = engine.run_root.parent / "estate"
    if later_gate == "changed":
        path = estate / "jobs.jil"
        path.write_text(path.read_text().replace("command: true", "command: echo changed"))
    plan = reread(
        replace(engine.update.live(), revision=2),
        engine.run_root / "plans",
        candidate=prepare_estate(estate),
    )
    request = replace(request, token=plan.token)
    path = engine.run_root / "plans" / request.token / "plan.json"
    document = decode(path.read_bytes())
    assert request.expect == engine.config_rev == 1 and document["live_rev"] == 2
    if later_gate == "preflight":
        del engine.startup_env["ADMISSION_LOG_DIR"]
    answer = _without_writes(engine, request, monkeypatch)
    assert answer == UpdateAnswer(Refused(request.token, "stale_plan"))
    assert path.is_file()
