"""Version-28 integrity, runtime identity and authority boundaries (DL-447)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from dispatch_fixtures import dispatch_record
from test_runner_environment import T0, catalog

from dsl41.canon import canonical_bytes, decode
from dsl41.cli_reload import SendFailed, live_of_answer
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_config import RuntimeProfile, runtime_hash
from dsl41.runner_journal import OpenTable, check_dispatch
from dsl41.runner_preflight import preflight
from dsl41.runner_startup import start_run
from dsl41.oracle_state import Event

RUN = "00000000-0000-4000-8000-000000000001"


def valid_dispatch():
    return dict(dispatch_record("J0", 1, attempt=RUN, spool=None, started_at=T0, rev=1).payload)


@pytest.mark.parametrize("key", ["first_dispatch_seq", "env", "inherited", "paths", "cwd"])
def test_required_dispatch_data_refuses_when_absent(key: str) -> None:
    data = valid_dispatch()
    del data[key]
    with pytest.raises(EngineError, match="version-28 dispatch"):
        check_dispatch(data)


def test_a_dispatch_builder_without_v28_data_is_refused() -> None:
    from dsl41.runner_journal import dispatch_record as build

    record = build("J0", 1, attempt=RUN, spool=None, started_at=T0, rev=1)
    with pytest.raises(EngineError, match="first_dispatch_seq"):
        check_dispatch(record.payload)


@pytest.mark.parametrize(
    "key,value",
    [
        ("first_dispatch_seq", 0),
        ("first_dispatch_seq", True),
        ("run_number", 0),
        ("ordinal", -1),
        ("rev", None),
        ("spool", 1),
        ("spool", "bad\0spool"),
        ("job", ""),
        ("job", 1),
        ("attempt", "bad"),
        ("env", []),
        ("env", {"BAD-NAME": "x"}),
        ("env", {"A": 4}),
        ("env", {"A": "bad\0value"}),
        ("inherited", []),
        ("inherited", {"AUTOSYS": "bad"}),
        ("cwd", "relative"),
        ("cwd", "bad\0path"),
        ("cwd", 1),
        ("paths", []),
        ("paths", {"other": "/abs"}),
        ("paths", {"std_in_file": None, "std_out_file": "relative", "std_err_file": "/err"}),
        ("paths", {"std_in_file": None, "std_out_file": 4, "std_err_file": "/err"}),
        ("paths", {"std_in_file": None, "std_out_file": "/bad\0path", "std_err_file": "/err"}),
        ("started_at", "bad"),
    ],
)
def test_damaged_dispatch_fields_refuse(key: str, value: object) -> None:
    data = valid_dispatch()
    data[key] = value
    with pytest.raises(EngineError, match="version-28 dispatch"):
        check_dispatch(data)


@pytest.mark.parametrize(
    "key,value",
    [
        ("DSL41_JOB", "wrong"),
        ("DSL41_SCHEDULED_AT", None),
        ("DSL41_SCHEDULED_AT", "bad"),
        ("DSL41_RUN", ""),
        ("DSL41_RUN", "bad"),
        ("DSL41_RUN", "e30="),
        ("DSL41_RUN", "W10="),
    ],
)
def test_damaged_generated_values_refuse(key: str, value: object) -> None:
    data = valid_dispatch()
    if value is None:
        del data["env"][key]
    else:
        data["env"][key] = value
    with pytest.raises(EngineError, match="version-28 dispatch"):
        check_dispatch(data)


def resign(table):
    data = {
        "dispatches": table["dispatches"],
        "attempts": [a["dispatch"] for a in table["attempts"]],
    }
    table["dispatch_hash"] = "sha256:" + hashlib.sha256(canonical_bytes(data)).hexdigest()


@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "hash",
        "lost-job",
        "wrong-map",
        "job-key",
        "seq-bool",
        "seq-zero",
        "attempt-env",
        "attempt-S",
        "missing-step-env",
    ],
)
def test_snapshot_dispatch_data_is_required_and_checked(damage: str) -> None:
    table = OpenTable()
    table.note(1, dispatch_record("J0", 1, attempt=RUN, spool=None, started_at=T0, rev=1))
    raw = decode(canonical_bytes(table.to_payload()))
    if damage == "absent":
        del raw["dispatches"]
    elif damage == "hash":
        raw["dispatch_hash"] = "bad"
    elif damage == "lost-job":
        raw["dispatches"].clear()
    else:
        body = raw["dispatches"]["J0"]
        if damage == "wrong-map":
            raw["dispatches"] = []
        elif damage == "job-key":
            raw["dispatches"] = {"other": body}
        elif damage == "seq-bool":
            body["seq"] = True
        elif damage == "seq-zero":
            body["seq"] = 0
        elif damage == "attempt-env":
            raw["attempts"][0]["env"] = {}
        elif damage == "attempt-S":
            body["first_dispatch_seq"] = 2
            body["env"]["AUTORUN"] = "2-1"
            body["seq"] = 2
        elif damage == "missing-step-env":
            raw["plans"] = [{"steps": [{"step": "dispatch", "env": {}}]}]
        resign(raw)
    with pytest.raises(
        (EngineError, ValueError, KeyError), match="dispatch|snapshot|SCHEDULED|attempt"
    ):
        OpenTable.from_payload(raw)


@pytest.mark.parametrize("inherited", [{}, {"DSL41_RUN": "inherited-tag"}])
def test_generated_run_is_symbolic_and_never_inherited(
    tmp_path: Path, monkeypatch, inherited
) -> None:
    monkeypatch.delenv("DSL41_RUN", raising=False)
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    ir = catalog(f"std_out_file: {tmp_path}/$DSL41_RUN.out", command='printf "%s" "$DSL41_RUN"')
    assert not [i for i in preflight(ir, startup_env=inherited) if i.code == "runtime-variable"]
    from dsl41.runner_adapters import LocalCommandAdapter

    async def scenario():
        engine = start_run(
            ir, tmp_path / "root", clock=RealClock(), adapters={"CMD": LocalCommandAdapter()}
        )
        try:
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(datetime.max)
            body = engine.dispatches["J0"]
            path = Path(body["paths"]["std_out_file"])
            assert path.read_text() == body["env"]["DSL41_RUN"]
            assert body["env"]["DSL41_RUN"] != "inherited-tag"
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_unavailable_reserved_name_never_recommends_forbidden_override() -> None:
    items = preflight(catalog("std_out_file: $DSL41_UNAVAILABLE"), startup_env={})
    [item] = [i for i in items if i.code == "runtime-variable"]
    assert "unavailable generated variable DSL41_UNAVAILABLE" in item.message
    assert "set engine.job_env" not in item.message


def test_empty_job_env_keeps_canonical_identity_and_other_diagnostics() -> None:
    from pydantic import ValidationError

    assert "job_env" not in RuntimeProfile().model_dump(mode="json")
    assert runtime_hash(RuntimeProfile()) == runtime_hash(RuntimeProfile(job_env={}))
    assert "job_env" in RuntimeProfile(job_env={"A": "literal"}).model_dump(mode="json")
    with pytest.raises(ValidationError) as caught:
        RuntimeProfile(machine_policy="bad")
    assert "input_value='bad'" in str(caught.value)


@pytest.mark.parametrize("tier", ["read", "ops", "adm"])
def test_authenticated_catalog_view_ignores_claimed_authority(
    short_root, monkeypatch, tier
) -> None:
    from test_access import _map_granting, _serve_armed as _serve, _teardown, _call

    monkeypatch.setenv("INHERITED_PRIVATE_NAME", "private-value")

    async def scenario():
        engine, server, task, _ = await _serve(
            short_root / "run",
            "insert_job: J0\njob_type: c\ncommand: true\n",
            _map_granting(short_root / "roles.toml", tier),
        )
        engine.profile = RuntimeProfile(job_env={"LOG_DIR": "explicit-value"})
        try:
            answer = await _call(
                server.path,
                {
                    "cmd": "catalog",
                    "claimed_actor": "os/admin",
                    "authority": "adm",
                    "environment_view": "ops",
                },
            )
            if tier == "read":
                assert answer["job_env_names"] == ["LOG_DIR"]
                assert "job_env" not in answer["settings"] and "path_environment" not in answer
                assert "INHERITED_PRIVATE_NAME" not in str(answer) and "explicit-value" not in str(
                    answer
                )
                with pytest.raises(SendFailed, match="ops catalog view"):
                    live_of_answer(answer)
            else:
                assert answer["settings"]["job_env"] == {"LOG_DIR": "explicit-value"}
                assert "INHERITED_PRIVATE_NAME" in answer["path_environment"]
                assert live_of_answer(answer).path_environment is not None
        finally:
            await _teardown(engine, server, task)

    asyncio.run(scenario())


def test_dispatch_keeps_startup_cwd_when_it_is_deleted(tmp_path, monkeypatch) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    async def scenario():
        engine = start_run(
            catalog("std_out_file: relative.out"),
            tmp_path / "root",
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
        )
        cwd.rmdir()
        try:
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(datetime.max)
            assert engine.dispatches["J0"]["paths"]["std_out_file"] == str(cwd / "relative.out")
        finally:
            monkeypatch.chdir(tmp_path)
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_environment_only_revision_stops_nothing_while_a_box_runs(tmp_path):
    from dsl41 import main_config
    from dsl41.reload_diff import definition_hash
    from dsl41.reload_plan import reread
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_update import UpdateRequest
    from test_main_config import config_file

    text = (
        "insert_job: B0\njob_type: b\n\n"
        "insert_job: J0\njob_type: c\nbox_name: B0\ncommand: true\n\n"
        "insert_job: W0\njob_type: f\nwatch_file: /synthetic/watch\n\ninsert_job: L0\njob_type: c\ncommand: true\n"
    )
    (tmp_path / "jobs.jil").write_text(text)
    path = config_file(tmp_path, engine='[engine.job_env]\nVALUE = "old"\n')
    initial = main_config.prepare(path)

    async def scenario():
        clock = VirtualClock(T0)
        engine = start_run(
            initial.catalog,
            tmp_path / "root",
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None), "FW": FakeAdapter(default=None)},
            launched=main_config.profile_of(initial),
            source_capture=main_config.encoded_parts(initial),
        )
        try:
            for job in ("B0", "W0"):
                engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["B0"].status == "RUNNING"
            before = {job: dict(engine.dispatches[job]) for job in ("J0", "W0")}
            path.write_text(path.read_text().replace('VALUE = "old"', 'VALUE = "new"'))
            candidate = main_config.prepare(path)
            assert definition_hash(
                initial.catalog.jobs["J0"], initial.catalog, initial.settings
            ) == definition_hash(
                candidate.catalog.jobs["J0"], candidate.catalog, candidate.settings
            )
            plan = reread(
                tmp_path,
                engine.update.live(),
                engine.run_root / "plans",
                candidate=candidate,
                start=T0,
            )
            assert plan.stop == () and plan.plan.changed == ()
            reply = engine.request_update(
                UpdateRequest(plan.token, False, 1, engine.baseline_id, "env-only")
            )
            await engine.run_until_quiescent(T0)
            assert reply.result().applied.result()["rev"] == 2
            assert engine.oracle.store.job["B0"].status == "RUNNING"
            assert {job: engine.dispatches[job] for job in before} == before
            assert engine.profile.job_env == {"VALUE": "new"}
            old_task = engine._live["J0"].task
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
            assert engine.spawn("J0", 1, ordinal=2, env=before["J0"]["env"])
            second = engine.dispatches["J0"]
            assert second["rev"] == 2 and second["env"]["VALUE"] == "new"
            assert second["first_dispatch_seq"] == before["J0"]["first_dispatch_seq"]
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "L0"}))
            await engine.run_until_quiescent(T0)
            assert engine.dispatches["L0"]["env"]["VALUE"] == "new"
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case",
    [
        "missing-env",
        "missing-prior",
        "changed-S",
        "future-S",
        "future-snapshot",
        "foreign-snapshot",
        "bool-snapshot",
    ],
)
def test_invalid_dispatch_identity_refuses_before_effects(tmp_path, case):
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_journal import decode_snapshot

    async def scenario():
        engine = start_run(
            catalog(),
            tmp_path / "root",
            clock=VirtualClock(T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(T0)
            original = dict(engine.dispatches["J0"])
            before = engine.journal.store.last_seq()
            if case == "missing-env":
                with pytest.raises(EngineError, match="SCHEDULED_AT"):
                    engine.spawn("J0", 1, ordinal=2, env={})
            elif case == "missing-prior":
                engine.dispatches.clear()
                with pytest.raises(EngineError, match="first dispatch sequence"):
                    engine.spawn("J0", 1, ordinal=2, env=original["env"])
            elif case in ("changed-S", "future-S"):
                first = before + 20 if case == "future-S" else original["first_dispatch_seq"] - 1
                forged = dispatch_record(
                    "J0",
                    1,
                    attempt="00000000-0000-4000-8000-000000000002",
                    ordinal=2,
                    spool=None,
                    started_at=T0,
                    rev=1,
                    first_dispatch_seq=first,
                )
                with pytest.raises(EngineError, match="sequence|changes S"):
                    engine.journal.append(forged)
            else:
                seq = engine.take_snapshot()
                data = engine.journal.store.latest_snapshot().data
                before = engine.journal.store.last_seq()
                if case == "future-snapshot":
                    with pytest.raises(EngineError, match="beyond its sequence"):
                        decode_snapshot(0, data, where="test")
                else:
                    raw = decode(data)
                    raw["state_version"] = 27 if case == "foreign-snapshot" else True
                    with pytest.raises(EngineError, match="incompatible snapshot state_version"):
                        decode_snapshot(seq, canonical_bytes(raw), where="test")
                assert seq >= original["seq"]
            assert engine.journal.store.last_seq() == before
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["dispatch-S", "dispatch-schedule", "snapshot-data"])
def test_damaged_store_refuses_before_recovery_effects(tmp_path, monkeypatch, damage):
    import sqlite3
    from contextlib import closing
    from dsl41 import runner_startup
    from dsl41.runner_clock import VirtualClock
    from store_surgery import rows, update

    root = tmp_path / "root"
    ir = catalog()

    async def scenario():
        engine = start_run(
            ir, root, clock=VirtualClock(T0), adapters={"CMD": FakeAdapter(default=None)}
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "J0"}))
        await engine.run_until_quiescent(T0)
        if damage == "snapshot-data":
            engine.take_snapshot()
        await engine.shutdown()
        engine.journal.close()
        if damage == "snapshot-data":
            with closing(sqlite3.connect(root / "engine.db")) as db:
                seq, data = db.execute(
                    "SELECT seq,data FROM snapshots ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                raw = decode(data)
                del raw["open"]["dispatches"]
                db.execute("UPDATE snapshots SET data=? WHERE seq=?", (canonical_bytes(raw), seq))
                db.commit()
        else:
            [(seq, _, body)] = [row for row in rows(root) if row[1] == "dispatch"]
            if damage == "dispatch-S":
                del body["first_dispatch_seq"]
            else:
                del body["env"]["DSL41_SCHEDULED_AT"]
            update(root, seq, body)
        before = (root / "engine.db").read_bytes()
        monkeypatch.setattr(
            runner_startup,
            "_first_reading",
            lambda *args: pytest.fail("recovery effect before refusal"),
        )
        with pytest.raises(EngineError, match="dispatch|SCHEDULED|snapshot"):
            await runner_startup.resume_run(
                ir, root, clock=VirtualClock(T0), adapters={"CMD": FakeAdapter()}
            )
        assert (root / "engine.db").read_bytes() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("reply", [None, "reaped"])
def test_waitid_unavailable_observations_are_portable(monkeypatch, reply):
    from types import SimpleNamespace
    from test_wrapper_branches import _Reports
    from dsl41 import runner_wrapper

    fake = _Reports(0, 0)

    def waitid(*args):
        if reply == "reaped":
            raise ChildProcessError()
        return None

    fake.waitid = waitid
    monkeypatch.setattr(runner_wrapper, "os", fake)
    assert runner_wrapper._observe_exit(SimpleNamespace(pid=1234)) is None


def test_first_reading_skips_watches_and_commands_with_nothing_left(monkeypatch):
    from types import SimpleNamespace as NS
    from dsl41 import runner_startup, runner_procid

    scan = runner_procid.Ours({})
    attempts = {"watch": NS(seq=1, attempt="watch"), "cmd": NS(seq=2, attempt="cmd")}
    engine = NS(
        journal=NS(open=NS(attempts=attempts)),
        stops=NS(scan=lambda: scan, leak_policy="terminate", left=lambda *args: NS(pids={})),
        attempts=NS(knows=lambda key: key == "cmd"),
    )
    monkeypatch.setattr(runner_startup, "_spawn_record", lambda attempt: None)
    assert runner_startup._first_reading(engine, [], frozenset()) == scan


def test_update_rechecks_a_candidate_against_the_engine_environment(tmp_path, monkeypatch):
    from dataclasses import replace
    from dsl41 import main_config
    from dsl41.reload_plan import reread
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_update import UpdateRequest
    from test_main_config import estate_file

    monkeypatch.delenv("PLAN_ONLY", raising=False)
    path = estate_file(tmp_path)
    initial = main_config.prepare(path)

    async def scenario():
        engine = start_run(
            initial.catalog,
            tmp_path / "root",
            clock=VirtualClock(T0),
            adapters={"CMD": FakeAdapter()},
            launched=main_config.profile_of(initial),
            source_capture=main_config.encoded_parts(initial),
        )
        try:
            (tmp_path / "a.jil").write_text(
                (tmp_path / "a.jil").read_text() + "std_out_file: $PLAN_ONLY/out\n"
            )
            candidate = main_config.prepare(path)
            live = replace(engine.update.live(), path_environment={"PLAN_ONLY": "set"})
            plan = reread(tmp_path, live, engine.run_root / "plans", candidate=candidate, start=T0)
            reply = engine.request_update(
                UpdateRequest(plan.token, False, 1, engine.baseline_id, "unknown-env")
            )
            await engine.run_until_quiescent(T0)
            assert (
                reply.result().code == "invalid_argument"
                and "runtime-variable" in reply.result().error
            )
            assert engine.config_rev == 1
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())
