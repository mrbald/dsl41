"""Literal runtime variables, dispatch durability and tail targets (DL-447)."""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsl41 import main_config, runner
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.reload_plan import RereadRefused, reread
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_config import RuntimeProfile, runtime_hash
from dsl41.runner_control import ControlServer
from dsl41.runner_environment import expand_path, resolve_streams, RuntimeVariableError
from dsl41.runner_journal import decode_snapshot, read_records, replay_store
from dsl41.runner_preflight import preflight
from dsl41.runner_startup import start_run
from dsl41.runner_update import UpdateRequest
from dsl41.runtime_policy import PolicyFinding, PolicyRefused

T0 = datetime(2026, 7, 1)


def catalog(extra: str = "", command: str = "true"):
    return lower_source(f"insert_job: J0\njob_type: c\ncommand: {command}\n{extra}\n")


@pytest.mark.parametrize(
    "name", ["BAD-NAME", "1BAD", "", "A\0", "DSL41_CUSTOM", "AUTO_JOB_NAME", "AUTORUN"]
)
def test_job_env_refuses_invalid_and_reserved_names(name: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeProfile(job_env={name: "literal"})


@pytest.mark.parametrize("value", ["bad\0value", 2, True, ["value"]])
def test_job_env_refuses_nul_and_non_strings(value: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeProfile(job_env={"VALUE": value})


@pytest.mark.parametrize(
    "text", ["$", "$$", "$$GLOBAL", "${}", "${1BAD}", "${A:-x}", "$(id)", "$1", "$?", "${A"]
)
def test_path_dollar_syntax_refuses(text: str) -> None:
    with pytest.raises(
        RuntimeVariableError, match=r"std_out_file.*supported forms.*\$VAR.*\$\{VAR\}"
    ):
        expand_path("J0", "std_out_file", text, {"A": "value"})


@pytest.mark.parametrize(
    "name", ["__job_name", "__machine", "__run_machine", "__box_name", "__max_exit_success"]
)
@pytest.mark.parametrize("braced", [False, True])
@pytest.mark.parametrize("supplied", [False, True])
def test_reserved_attribute_paths_refuse_before_environment_lookup(name, braced, supplied):
    class Guarded(dict):
        def __contains__(self, key):
            pytest.fail("reserved variable reached environment membership lookup")

        def __getitem__(self, key):
            pytest.fail("reserved variable reached environment value lookup")

    env = Guarded({name: "PRIVATE_VALUE"} if supplied else {})
    source = "${" + name + "}" if braced else "$" + name
    with pytest.raises(RuntimeVariableError) as error:
        expand_path("J0", "std_out_file", source, env)
    assert "J0" in str(error.value) and "std_out_file" in str(error.value)
    assert name in str(error.value) and "unavailable" in str(error.value)
    assert "literal path" in str(error.value) and "PRIVATE_VALUE" not in str(error.value)


def test_reserved_spelling_inside_supported_value_remains_literal():
    value, names = expand_path("J0", "std_out_file", "$SUPPORTED", {"SUPPORTED": "${__job_name}"})
    assert value == "${__job_name}" and names == {"SUPPORTED"}


def test_substitution_is_literal_once_and_paths_anchor_to_dispatch_cwd(tmp_path: Path) -> None:
    text, names = expand_path(
        "J0", "std_out_file", "$A/${_b2}/$A", {"A": "${OTHER}", "_b2": "* ~ $(id)"}
    )
    assert text == "${OTHER}/* ~ $(id)/${OTHER}" and names == {"A", "_b2"}
    paths, _ = resolve_streams(
        catalog("std_out_file: $A").jobs["J0"], {"A": "relative file"}, cwd=tmp_path
    )
    assert paths == {
        "std_in_file": None,
        "std_out_file": str(tmp_path / "relative file"),
        "std_err_file": None,
    }


@pytest.mark.parametrize("attribute", ["std_out_file", "std_err_file"])
@pytest.mark.parametrize("prefix", ["", ">", ">>"])
@pytest.mark.parametrize("target", ["relative", "/absolute", ">literal"])
def test_stream_prefixes_resolve_before_expansion(tmp_path, attribute, prefix, target):
    paths, names = resolve_streams(
        catalog(f"{attribute}: {prefix}${{TARGET}}").jobs["J0"],
        {"TARGET": target},
        cwd=tmp_path,
    )
    assert paths[attribute] == (target if target.startswith("/") else f"{tmp_path}/{target}")
    assert names == {"TARGET"}


def test_spawn_wraps_std_file_refusal(monkeypatch):
    engine = runner.Engine(catalog(), clock=RealClock(), adapters={"CMD": FakeAdapter()})
    engine.oracle.feed(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "J0"}))

    def refused(*args, **kwargs):
        raise PolicyRefused(
            PolicyFinding("std-file", "J0", "std_out_file", "bad target", "use a path")
        )

    monkeypatch.setattr(runner, "resolve_streams", refused)
    with pytest.raises(EngineError, match=r"preflight ERROR \[std-file\]: job J0: std_out_file"):
        engine.spawn("J0", 1)
    assert engine.dispatches == {} and engine._live == {}


@pytest.mark.parametrize("value", ["", "bad\0path"])
def test_empty_and_nul_paths_refuse_without_printing_values(value: str) -> None:
    with pytest.raises(RuntimeVariableError, match="empty path or contains NUL"):
        expand_path("J0", "std_in_file", "$A", {"A": value})


def test_preflight_uses_pre_profile_environment_and_retains_dl240_refusals() -> None:
    ir = catalog(
        "profile: /synthetic/profile\nstd_err_file: ${ONLY_PROFILE}", command="echo $ONLY_PROFILE"
    )
    items = preflight(ir, startup_env={})
    [item] = [item for item in items if item.code == "runtime-variable"]
    assert item.message == (
        "job J0: std_err_file references unavailable variable ONLY_PROFILE; paths resolve before"
        " profile execution; set engine.job_env.ONLY_PROFILE."
    )
    assert not [
        i
        for i in preflight(ir, startup_env={}, job_env={"ONLY_PROFILE": "log"})
        if i.code == "runtime-variable"
    ]
    assert not [i for i in preflight(ir, execution=False) if i.code == "runtime-variable"]
    assert not [
        i
        for i in preflight(catalog("profile: /synthetic/profile", command="echo $ONLY_PROFILE"))
        if i.code == "runtime-variable"
    ]
    refused = catalog("envvars: A=1\nchk_files: 1\nstd_out_file: $$GLOBAL")
    assert {"envvars", "chk-files", "global-substitution", "runtime-variable"} <= {
        i.code for i in preflight(refused)
    }
    fw = lower_source("insert_job: W0\njob_type: f\nwatch_file: $AUTO_JOB_NAME\n")
    assert any(i.code == "runtime-variable" and "watch_file" in i.message for i in preflight(fw))


def test_instance_variables_are_exclusive_and_generated_names_are_symbolic() -> None:
    for name in ("AUTOSERV", "AUTOSYS", "AUTOUSER", "AUTOROOT"):
        ir = catalog(f"std_out_file: ${name}")
        assert any(
            i.code == "runtime-variable" for i in preflight(ir, startup_env={name: "inherited"})
        )
        assert not [
            i
            for i in preflight(ir, startup_env={}, job_env={name: "explicit"})
            if i.code == "runtime-variable"
        ]
    ir = catalog("std_out_file: $AUTO_JOB_NAME.$AUTORUN.$DSL41_ATTEMPT.$DSL41_COOKIE")
    assert not [i for i in preflight(ir, startup_env={}) if i.code == "runtime-variable"]


def test_real_command_environment_streams_and_completed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOSYS", "must-be-unset")
    monkeypatch.setenv("LITERAL", "inherited")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    (tmp_path / "J0.in").write_text("input\n")
    profile = tmp_path / "profile.sh"
    profile.write_text("export COMMAND_VALUE=profile\nexport LOG_DIR=/unused-profile-path\n")
    ir = catalog(
        f"profile: {profile}\nstd_in_file: $LOG_DIR/${{AUTO_JOB_NAME}}.in\n"
        "std_out_file: $LOG_DIR/${AUTO_JOB_NAME}.$AUTORUN.out\n"
        "std_err_file: $LOG_DIR/${AUTO_JOB_NAME}.$AUTORUN.err",
        command='cat; printf \'%s|%s|%s|%s|%s|%s\' "$AUTO_JOB_NAME" "$AUTORUN" "$LITERAL" "${AUTOSYS-unset}" "$AUTOSERV" "$COMMAND_VALUE"; echo error >&2',
    )

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(
            ir,
            tmp_path / "root",
            clock=clock,
            adapters={"CMD": LocalCommandAdapter()},
            launched=RuntimeProfile(job_env={"LITERAL": "explicit $VALUE", "AUTOSERV": "site"}),
        )
        server = ControlServer(engine, tmp_path / "unused.sock")
        try:
            assert server._status({})["jobs"]["J0"]["log_out"] is None
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(datetime.max)
            assert engine.oracle.store.job["J0"].status == "SUCCESS"
            assert engine.journal is not None
            [(seq, dispatch)] = [
                (seq, r.payload)
                for seq, r in engine.journal.store.read_from(0)
                if r.kind == "dispatch"
            ]
            assert dispatch["first_dispatch_seq"] == seq
            assert dispatch["env"]["AUTORUN"] == f"{seq}-1"
            assert dispatch["inherited"] == {"LOG_DIR": str(tmp_path)}
            assert {
                "DSL41_JOB",
                "DSL41_RUN_NUMBER",
                "DSL41_SCHEDULED_AT",
                "DSL41_ATTEMPT",
                "DSL41_COOKIE",
            } <= dispatch["env"].keys()
            out = tmp_path / f"J0.{seq}-1.out"
            assert out.read_text() == f"input\nJ0|{seq}-1|explicit $VALUE|unset|site|profile"
            assert (tmp_path / f"J0.{seq}-1.err").read_text() == "error\n"
            assert server._status({})["jobs"]["J0"]["log_out"] == str(out)
            engine.take_snapshot()
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_cross_job_sequences_replacements_startup_capture_and_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "first"))
    ir = lower_source(
        "\n".join(
            f"insert_job: {name}\njob_type: c\ncommand: true\nstd_out_file: $LOG_DIR/$AUTORUN\n"
            for name in ("J0", "J1")
        )
    )

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(ir, tmp_path / "root", clock=clock, adapters={"CMD": FakeAdapter()})
        try:
            monkeypatch.setenv("LOG_DIR", str(tmp_path / "later"))
            for name in ir.jobs:
                engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": name}))
            await engine.run_until_quiescent(datetime.max)
            first = dict(engine.dispatches["J0"])
            other = engine.dispatches["J1"]
            assert first["env"]["AUTORUN"] != other["env"]["AUTORUN"]
            engine.inject(
                Event(
                    at=clock.now(),
                    kind="STATUS",
                    payload={"job": "J0", "status": "RUNNING"},
                    source="control",
                )
            )
            await engine.run_until_quiescent(datetime.max)
            engine.profile = RuntimeProfile(job_env={"LOG_DIR": str(tmp_path / "applied")})
            assert engine.spawn("J0", 1, ordinal=2, env=first["env"])
            await asyncio.sleep(0)
            second = engine.dispatches["J0"]
            assert second["first_dispatch_seq"] == first["first_dispatch_seq"]
            assert second["env"]["AUTORUN"] == f"{first['first_dispatch_seq']}-2"
            assert second["env"]["DSL41_SCHEDULED_AT"] == first["env"]["DSL41_SCHEDULED_AT"]
            assert second["paths"]["std_out_file"] == str(
                tmp_path / "applied" / second["env"]["AUTORUN"]
            )
            assert first["inherited"]["LOG_DIR"] == str(tmp_path / "first")
            engine.take_snapshot()
            assert engine.journal is not None
            snapshot = engine.journal.store.latest_snapshot()
            assert snapshot is not None
            restored = decode_snapshot(snapshot.seq, snapshot.data, where="test")
            assert restored.open.dispatches == engine.dispatches
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_missing_variables_refuse_startup_before_store_creation(tmp_path: Path) -> None:
    with pytest.raises(
        EngineError, match=r"\[runtime-variable\].*unavailable variable ABSENT_TEST_VARIABLE"
    ):
        start_run(
            catalog("std_out_file: $ABSENT_TEST_VARIABLE"),
            tmp_path / "root",
            clock=RealClock(),
            adapters={"CMD": FakeAdapter()},
        )
    assert not (tmp_path / "root" / "engine.db").exists()


def test_main_config_captures_job_env_and_reload_revision(tmp_path: Path) -> None:
    from test_main_config import estate_file

    path = estate_file(tmp_path, engine='[engine.job_env]\nAUTOSERV = "one"\n')
    initial = main_config.prepare(path)
    before = main_config.profile_of(initial)
    assert before.job_env == {"AUTOSERV": "one"}

    async def scenario() -> None:
        clock = VirtualClock(T0)
        engine = start_run(
            initial.catalog,
            tmp_path / "root",
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            launched=before,
            source_capture=main_config.encoded_parts(initial),
        )
        try:
            engine.launch_settings = initial.metadata["launch"]
            path.write_text(path.read_text().replace('AUTOSERV = "one"', 'AUTOSERV = "two"'))
            candidate = main_config.prepare(path)
            plan = reread(
                tmp_path, engine.update.live(), tmp_path / "root" / "plans", candidate=candidate
            )
            reply = engine.request_update(
                UpdateRequest(plan.token, False, 1, engine.baseline_id, "env-update")
            )
            await engine.run_until_quiescent(T0)
            assert reply.result().applied.result()["rev"] == 2
            assert engine.profile.job_env == {"AUTOSERV": "two"}
            assert runtime_hash(engine.profile) != runtime_hash(before)
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())
    path.write_text(path.read_text().replace('AUTOSERV = "two"', 'AUTORUN = "forbidden"'))
    with pytest.raises(RereadRefused, match="cannot override AUTORUN"):
        main_config.prepare(path)


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_crash_boundaries_allocate_from_committed_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from dsl41.runner_startup import resume_run

    ir = catalog("std_out_file: $LOG_DIR/$AUTORUN")
    profile = RuntimeProfile(job_env={"LOG_DIR": str(tmp_path)})
    root = tmp_path / "root"

    class Crash(RuntimeError):
        pass

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(ir, root, clock=clock, adapters={"CMD": FakeAdapter()}, launched=profile)
        assert engine.journal is not None
        append = engine.journal.store.append

        def fail_commit(*records):
            if any(record.kind == "dispatch" for record in records):
                raise Crash("before commit")
            return append(*records)

        def fail_launch(*args, **kwargs):
            raise Crash("after commit")

        with monkeypatch.context() as patch:
            if boundary == "before_commit":
                patch.setattr(engine.journal.store, "append", fail_commit)
            else:
                patch.setattr(engine, "_launch", fail_launch)
            try:
                engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "J0"}))
                with pytest.raises(Crash):
                    await engine.run_until_quiescent(datetime.max)
                assert not (root / "runs" / "J0.1" / "1").exists()
            finally:
                await engine.shutdown()
                engine.journal.close()
        before = [(seq, r.payload) for seq, r in read_records(root) if r.kind == "dispatch"]
        assert len(before) == (1 if boundary == "after_commit" else 0)
        resumed = await resume_run(
            ir, root, clock=RealClock(), adapters={"CMD": FakeAdapter()}, declared=profile
        )
        try:
            await resumed.run_until_quiescent(datetime.max)
            latest = resumed.dispatches["J0"]
            records = [
                (seq, r.payload)
                for seq, r in resumed.journal.store.read_from(0)
                if r.kind == "dispatch"
            ]
            first_seq = records[0][0]
            ordinal = 2 if boundary == "after_commit" else 1
            assert latest["first_dispatch_seq"] == first_seq
            assert latest["ordinal"] == ordinal
            assert latest["env"]["AUTORUN"] == f"{first_seq}-{ordinal}"
            assert latest["paths"]["std_out_file"] == str(tmp_path / f"{first_seq}-{ordinal}")
            if before:
                assert before[0][1]["paths"]["std_out_file"] == str(tmp_path / f"{first_seq}-1")
                assert (
                    latest["env"]["DSL41_SCHEDULED_AT"] == before[0][1]["env"]["DSL41_SCHEDULED_AT"]
                )
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_pruned_snapshot_keeps_completed_paths_and_sequence_high_water(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41.runner_startup import resume_run

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "original"))
    ir = catalog("std_out_file: $LOG_DIR/$AUTORUN")
    root = tmp_path / "root"

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(ir, root, clock=clock, adapters={"CMD": FakeAdapter()})
        try:
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(datetime.max)
            original = dict(engine.dispatches["J0"])
            engine.take_snapshot()
            snapshot = engine.journal.store.latest_snapshot()
            high_water = engine.journal.store.last_seq()
            assert engine.journal.store.prune_before(snapshot.seq, datetime.max) > 0
            replay = replay_store(engine.journal.store, where="test")
            assert replay.anchor.open.dispatches["J0"] == original
        finally:
            await engine.shutdown()
            engine.journal.close()
        monkeypatch.setenv("LOG_DIR", str(tmp_path / "changed"))
        resumed = await resume_run(ir, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
        try:
            await resumed.run_until_quiescent(datetime.max)
            status = ControlServer(resumed, tmp_path / "unused.sock")._status({})
            assert status["jobs"]["J0"]["log_out"] == original["paths"]["std_out_file"]
            resumed.inject(Event(at=resumed.clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await resumed.run_until_quiescent(datetime.max)
            latest = resumed.dispatches["J0"]
            assert latest["first_dispatch_seq"] > high_water
            assert latest["run_number"] == 2
            assert latest["paths"]["std_out_file"] == str(
                tmp_path / "changed" / latest["env"]["AUTORUN"]
            )
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_legacy_prefixed_dispatch_paths_survive_replay(tmp_path, monkeypatch):
    from dsl41.runner_startup import resume_run

    old = tmp_path / ">name"
    old.write_text("old output\n")
    ir = catalog("std_out_file: >name")
    root = tmp_path / "root"

    def legacy_paths(*args, **kwargs):
        paths, names = resolve_streams(*args, **kwargs)
        return {**paths, "std_out_file": str(old)}, names

    async def scenario():
        engine = start_run(ir, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
        try:
            with monkeypatch.context() as patch:
                patch.setattr(runner, "resolve_streams", legacy_paths)
                engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "J0"}))
                await engine.run_until_quiescent(datetime.max)
            original = dict(engine.dispatches["J0"])
            engine.take_snapshot()
            snapshot = engine.journal.store.latest_snapshot()
            restored = decode_snapshot(snapshot.seq, snapshot.data, where="test")
            assert restored.open.dispatches["J0"] == original
        finally:
            await engine.shutdown()
            engine.journal.close()
        resumed = await resume_run(
            ir, root, clock=RealClock(), adapters={"CMD": LocalCommandAdapter()}
        )
        try:
            await resumed.run_until_quiescent(datetime.max)
            assert resumed.dispatches["J0"] == original
            assert ControlServer(resumed, tmp_path / "unused.sock")._status({})["jobs"]["J0"][
                "log_out"
            ] == str(old)
            assert old.read_text() == "old output\n"
            assert len([r for _, r in read_records(root) if r.kind == "dispatch"]) == 1
        finally:
            await resumed.shutdown()
            resumed.journal.close()

    asyncio.run(scenario())


def test_reread_validates_against_engine_startup_values_not_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41.cli_reload import live_of_answer
    from test_main_config import estate_file

    monkeypatch.setenv("CAPTURED_DIR", "captured-secret-value")
    path = estate_file(tmp_path)
    initial = main_config.prepare(path)

    async def scenario() -> None:
        clock = VirtualClock(T0)
        engine = start_run(
            initial.catalog,
            tmp_path / "root",
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            launched=main_config.profile_of(initial),
            source_capture=main_config.encoded_parts(initial),
        )
        try:
            server = ControlServer(engine, tmp_path / "unused.sock")
            answer = server._catalog()
            assert answer["path_environment"]["CAPTURED_DIR"] == "set"
            assert "captured-secret-value" not in str(answer)
            live = live_of_answer(answer)
            monkeypatch.delenv("CAPTURED_DIR")
            (tmp_path / "a.jil").write_text(
                (tmp_path / "a.jil").read_text() + "std_out_file: $CAPTURED_DIR/$AUTORUN\n"
            )
            candidate = main_config.prepare(path)
            plan = reread(tmp_path, live, tmp_path / "root" / "plans", candidate=candidate)
            assert plan.token
            monkeypatch.setenv("CLIENT_ONLY", "client-value")
            (tmp_path / "a.jil").write_text(
                (tmp_path / "a.jil").read_text().replace("CAPTURED_DIR", "CLIENT_ONLY")
            )
            with pytest.raises(RereadRefused, match="references unavailable variable CLIENT_ONLY"):
                reread(
                    tmp_path, live, tmp_path / "root" / "plans", candidate=main_config.prepare(path)
                )
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


def test_replacement_refuses_before_a_dispatch_record_or_launch(tmp_path: Path) -> None:
    async def scenario() -> None:
        ir = catalog("std_out_file: $EXPLICIT_DIR/$AUTORUN")
        clock = RealClock()
        engine = start_run(
            ir,
            tmp_path / "root",
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            launched=RuntimeProfile(job_env={"EXPLICIT_DIR": str(tmp_path)}),
        )
        try:
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "J0"}))
            await engine.run_until_quiescent(datetime.max)
            first = dict(engine.dispatches["J0"])
            engine.inject(
                Event(
                    at=clock.now(),
                    kind="STATUS",
                    payload={"job": "J0", "status": "RUNNING"},
                    source="control",
                )
            )
            await engine.run_until_quiescent(datetime.max)
            engine.profile = RuntimeProfile()
            before = engine.journal.store.last_seq()
            with pytest.raises(
                EngineError, match=r"\[runtime-variable\].*unavailable variable EXPLICIT_DIR"
            ):
                engine.spawn("J0", 1, ordinal=2, env=first["env"])
            assert engine.journal.store.last_seq() == before
            assert engine.dispatches["J0"] == first
        finally:
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [27, 999])
def test_incompatible_store_state_refuses_without_writes(tmp_path: Path, version: int) -> None:
    import sqlite3
    from dsl41.runner_startup import resume_run

    ir = catalog()

    async def scenario() -> None:
        root = tmp_path / "root"
        engine = start_run(ir, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
        await engine.shutdown()
        engine.journal.close()
        with closing(sqlite3.connect(root / "engine.db")) as connection:
            connection.execute("UPDATE meta SET value=? WHERE key='state_version'", (str(version),))
            connection.commit()
        before = (root / "engine.db").read_bytes()
        with pytest.raises(EngineError, match=f"state version {version}.*this binary runs 28"):
            await resume_run(ir, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
        assert (root / "engine.db").read_bytes() == before

    asyncio.run(scenario())


def test_job_env_validation_does_not_echo_values() -> None:
    with pytest.raises(ValidationError) as caught:
        RuntimeProfile(job_env={"VALUE": "synthetic-secret\0"})
    assert "synthetic-secret" not in str(caught.value)


def test_default_log_paths_do_not_expand_dollars_in_the_engine_root(tmp_path: Path) -> None:
    default = str(tmp_path / "$LITERAL_ROOT" / "J0.1.out")
    paths, referenced = resolve_streams(
        catalog().jobs["J0"], {}, cwd=tmp_path, defaults={"std_out_file": default}
    )
    assert paths["std_out_file"] == default
    assert referenced == set()


@pytest.mark.parametrize(
    "text", ["relative/", "/absolute/path/", "relative/./path/", "//absolute/path/"]
)
def test_path_anchoring_preserves_literal_components_and_trailing_slash(
    tmp_path: Path, text: str
) -> None:
    paths, _ = resolve_streams(
        catalog("std_out_file: $VALUE").jobs["J0"], {"VALUE": text}, cwd=tmp_path
    )
    expected = text if text.startswith("/") else f"{tmp_path}/{text}"
    assert paths["std_out_file"] == expected
