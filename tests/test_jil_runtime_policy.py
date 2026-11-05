"""Synthetic policy inputs and shared refusal boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dsl41 import cli_run, main_config, runner_preflight, runner_startup
from dsl41.ir import LoweringError, lower_source
from dsl41.reload_diff import definition_hash
from dsl41.reload_plan import Estate, Live, RereadRefused, estate_settings, reread
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_config import Configuration, RuntimeProfile, runtime_hash
from dsl41.runner_update import profile_for, settings_of
from dsl41.runtime_policy import (
    ATTRIBUTE_NAMES,
    JobRuntimePolicy,
    PolicyRefused,
    RuntimePolicySettings,
    attribute_environment,
    job_policy,
    parse_limits,
    resolve_job_shell,
    stream_target,
)
from store_surgery import rows
from test_main_config import JOB, estate_file


@pytest.fixture(autouse=True)
def bourne_account(monkeypatch):
    monkeypatch.setattr(
        runner_preflight.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="policy_user", pw_shell="/bin/sh"),
    )


def catalog(attributes="", kind="c"):
    execution = (
        "command: true" if kind == "c" else "watch_file: /tmp/policy-input" if kind == "f" else ""
    )
    if any(line.startswith(("command:", "watch_file:")) for line in attributes.splitlines()):
        execution = ""
    return lower_source(f"insert_job: policy_job\njob_type: {kind}\n{execution}\n{attributes}\n")


REFUSALS = [
    ("condition: s(remote^INSTANCE)", "c", "external-instance"),
    ("job_class: batch", "c", "job-class"),
    ("permission: gx", "c", "permission"),
    ("auto_delete: 0", "c", "auto-delete"),
    ("heartbeat_interval: 1", "c", "heartbeat-unsupported"),
    ("notification_template: template", "c", "notification-unsupported"),
    ("notification_alarm_types: MAXRUNALARM", "c", "notification-unsupported"),
    ("send_notification: 1", "c", "notification-hook"),
    ("n_retrys: 1", "b", "n-retrys"),
    ("n_retrys: 1", "f", "n-retrys"),
    ("n_retrys: 21", "c", "n-retrys"),
    ("profile: /tmp/policy-profile", "f", "fw-execution"),
    ("std_out_file: /tmp/policy-out", "f", "fw-execution"),
    ("std_err_file: /tmp/policy-err", "f", "fw-execution"),
    ("shell: /bin/sh", "c", "shell"),
    ("profile: $PROFILE", "c", "profile"),
    ("std_out_file: > output", "c", "std-file"),
    ("std_in_file: >>input", "c", "std-file"),
    ('ulimit: n="2,1"', "c", "ulimit"),
]

REFUSALS += [
    (f"{attribute}: {spelling}", "f" if attribute == "watch_file" else "c", "global-substitution")
    for attribute in (
        "command",
        "profile",
        "std_in_file",
        "std_out_file",
        "std_err_file",
        "watch_file",
    )
    for spelling in (
        "$$NAME",
        "$$(NAME)",
        "$${NAME}",
        "$$7",
        "$$!",
        "$$",
        r"\$$NAME",
        "'$$NAME'",
        '"$$NAME"',
        "blob:$$",
    )
]


@pytest.mark.usefixtures("cli_env")
@pytest.mark.parametrize("attributes,kind,code", REFUSALS[:19])
def test_known_unsupported_ignore_preserves_refusals(tmp_path, attributes, kind, code):
    from typer.testing import CliRunner
    from dsl41.cli import app

    path = estate_file(tmp_path, lint='ignore = ["known-unsupported"]\n')
    execution = "command: true" if kind == "c" else "watch_file: /tmp/input" if kind == "f" else ""
    (tmp_path / "a.jil").write_text(
        f"insert_job: policy_job\njob_type: {kind}\n{execution}\n{attributes}\ngroup: x\n"
    )
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2 and f"[{code}]" in result.output, result.output
    assert "group:" not in result.output
    candidate = main_config.prepare(path)
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch=candidate.metadata["launch"])
    with pytest.raises(RereadRefused, match=code) as caught:
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate)
    assert "group:" not in str(caught.value)


@pytest.mark.parametrize("attributes,kind,code", [row for row in REFUSALS if row[2] == "std-file"])
def test_std_file_refusal_is_reported_once(attributes, kind, code):
    jobs = catalog(attributes, kind)
    findings = [item for item in runner_preflight.preflight(jobs) if item.code == code]
    assert len(findings) == 1 and findings[0].severity == "ERROR"
    assert runner_preflight.runtime_variable_preflight(jobs) == []
    with pytest.raises(EngineError) as caught:
        runner_startup.start_run(jobs, None, clock=RealClock(), adapters={"CMD": FakeAdapter()})
    assert str(caught.value).count("preflight ERROR [std-file]") == 1
    assert "runtime-variable" not in str(caught.value)


@pytest.mark.parametrize("attributes,kind,code", REFUSALS)
def test_shared_policy_refuses_start_restart_and_reload(
    tmp_path, monkeypatch, attributes, kind, code
):
    candidate = catalog(attributes, kind)
    settings = estate_settings(None, None, {}, candidate)
    findings = runner_preflight.preflight(candidate)
    assert any(item.severity == "ERROR" and item.code == code for item in findings)
    with pytest.raises(EngineError, match=code):
        runner_startup.start_run(
            candidate, None, clock=RealClock(), adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()}
        )
    root = tmp_path / "root"
    with monkeypatch.context() as seed:
        seed.setattr(runner_startup, "_check_runtime_variables", lambda *a, **kw: None)
        engine = runner_startup.start_run(
            candidate, root, clock=RealClock(), adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()}
        )
        engine.journal.close()
    before = rows(root)
    for _ in range(2):
        with pytest.raises(EngineError, match=f"restart-preflight.*{code}"):
            asyncio.run(
                runner_startup.resume_run(
                    candidate,
                    root,
                    clock=RealClock(),
                    adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
                )
            )
        assert rows(root) == before
    live_catalog = catalog()
    live = Live(live_catalog, estate_settings(None, None, {}, live_catalog), 1, {})
    candidate_estate = Estate(
        candidate,
        settings,
        (),
        metadata={"config": {"lint": {"refuse": "none"}}, "non_idempotent": {}, "launch": {}},
    )
    with pytest.raises(RereadRefused, match=code):
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate_estate)
    assert not (tmp_path / "plans").exists()


@pytest.mark.parametrize(
    "spelling",
    [
        "$$NAME",
        "$$(NAME)",
        "$${NAME}",
        "$$7",
        "$$!",
        "$$",
        r"\$$NAME",
        "'$$NAME'",
        '"$$NAME"',
        "blob:$$",
    ],
)
@pytest.mark.parametrize(
    "attribute", ["command", "profile", "std_in_file", "std_out_file", "std_err_file", "watch_file"]
)
def test_literal_double_dollars_are_refused_without_var_site_index(spelling, attribute):
    value = catalog(kind="f" if attribute == "watch_file" else "c")
    job = value.jobs["policy_job"]
    setattr(job.exec_, attribute, spelling)
    job.var_sites.clear()
    findings = runner_preflight.execution_policy_preflight(value)
    item = next(item for item in findings if item.code == "global-substitution")
    assert item.message.startswith(f"job policy_job: {attribute}:")
    assert "AutoSys substitution and JIL PID shorthand are unsupported" in item.message


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("$$SYNTHETIC_NAME", "SYNTHETIC_NAME"),
        ("$${SYNTHETIC_NAME}", "SYNTHETIC_NAME"),
        ("$$7", None),
        ("$$!", None),
        ("$$", None),
    ],
)
def test_global_refusal_reports_only_a_recognized_identifier(source, expected):
    job = catalog(f"command: echo PREFIX_SECRET {source} SUFFIX_SECRET").jobs["policy_job"]
    with pytest.raises(PolicyRefused) as error:
        job_policy(job)
    message = str(error.value)
    assert "global-substitution" in message
    assert "AutoSys substitution and JIL PID shorthand are unsupported" in message
    assert "PREFIX_SECRET" not in message and "SUFFIX_SECRET" not in message
    if expected is not None:
        assert f"global {expected}" in message
    else:
        assert "(global " not in message


def test_real_startup_refuses_cross_instance_without_job_effects(tmp_path):
    class NoLaunch(FakeAdapter):
        async def run(self, *args, **kwargs):
            pytest.fail("cross-instance refusal launched a job")

    root = tmp_path / "root"
    with pytest.raises(EngineError, match="external-instance"):
        runner_startup.start_run(
            catalog("condition: s(remote^INSTANCE)"),
            root,
            clock=RealClock(),
            adapters={"CMD": NoLaunch()},
        )
    assert not (root / "engine.db").exists()
    assert not (root / "runs").exists()


@pytest.mark.parametrize("inherited", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_reserved_attribute_path_refuses_start_and_reread(
    tmp_path, monkeypatch, inherited, enabled
):
    name = "__job_name"
    if inherited:
        monkeypatch.setenv(name, "WRONG_TARGET")
    else:
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "$__job_name.out"
    jobs = catalog(f"std_out_file: {output}")
    findings = runner_preflight.preflight(jobs, set_job_attribute_environmentals=enabled)
    assert any(i.code == "runtime-variable" and name in i.message for i in findings)
    root = tmp_path / "root"
    profile = RuntimeProfile(set_job_attribute_environmentals=enabled)
    with pytest.raises(EngineError, match="runtime-variable.*__job_name"):
        runner_startup.start_run(
            jobs, root, clock=RealClock(), adapters={"CMD": FakeAdapter()}, launched=profile
        )
    assert not (root / "engine.db").exists() and not (tmp_path / "WRONG_TARGET.out").exists()
    path = estate_file(
        tmp_path, engine=f"set_job_attribute_environmentals = {str(enabled).lower()}\n"
    )
    (tmp_path / "a.jil").write_text(
        f"insert_job: policy_job\njob_type: c\ncommand: true\nstd_out_file: {output}\n"
    )
    candidate = main_config.prepare(path)
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch=candidate.metadata["launch"])
    with pytest.raises(RereadRefused, match="runtime-variable.*__job_name"):
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists() and not (tmp_path / "WRONG_TARGET.out").exists()


@pytest.mark.parametrize("inherited", [False, True])
def test_forged_reserved_path_refuses_actual_replacement_dispatch(tmp_path, monkeypatch, inherited):
    from dsl41.oracle_state import Event

    if inherited:
        monkeypatch.setenv("__job_name", "WRONG_TARGET")
    else:
        monkeypatch.delenv("__job_name", raising=False)

    class CountingAdapter(FakeAdapter):
        calls = 0

        async def run(self, *args, **kwargs):
            self.calls += 1
            return await super().run(*args, **kwargs)

    async def scenario():
        adapter = CountingAdapter()
        clock = RealClock()
        engine = runner_startup.start_run(
            catalog(), tmp_path / "root", clock=clock, adapters={"CMD": adapter}
        )
        try:
            engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "policy_job"}))
            await engine.run_until_quiescent(datetime.max)
            first = dict(engine.dispatches["policy_job"])
            engine.inject(
                Event(
                    at=clock.now(),
                    kind="STATUS",
                    payload={"job": "policy_job", "status": "RUNNING"},
                    source="control",
                )
            )
            await engine.run_until_quiescent(datetime.max)
            engine.oracle.catalog.jobs["policy_job"].exec_.std_out_file = str(
                tmp_path / "$__job_name.out"
            )
            before = engine.journal.store.last_seq()
            with pytest.raises(EngineError, match="runtime-variable.*__job_name"):
                engine.spawn("policy_job", 1, ordinal=2, env=first["env"])
            assert engine.journal.store.last_seq() == before
            assert engine.dispatches["policy_job"] == first and adapter.calls == 1
            assert not (tmp_path / "WRONG_TARGET.out").exists()
        finally:
            await engine.shutdown()
            engine.journal.close()

    from datetime import datetime

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_shell", "/bin/bash"),
        ("job_profile", "/tmp/synthetic-profile"),
        ("std_file_mode", "truncate"),
        ("set_job_attribute_environmentals", False),
    ],
)
def test_runtime_policy_settings_profile_round_trip(field, value):
    jobs = catalog()
    profile = RuntimeProfile.model_validate({field: value})
    settings = estate_settings(None, None, {}, jobs, runtime_policy={field: value})
    assert settings_of(profile, jobs) == settings
    assert profile_for(settings, RuntimeProfile()) == profile
    assert profile_for(estate_settings(None, None, {}, jobs), profile) == RuntimeProfile()


def test_default_policy_definition_hash_matches_main():
    jobs = lower_source(JOB)
    settings = settings_of(RuntimeProfile(), jobs)
    assert set(settings) == {
        "timezone",
        "timezone_aliases",
        "semantics",
        "job_env",
        "job_profile",
        "job_shell",
        "std_file_mode",
        "kill_signals",
        "set_job_attribute_environmentals",
        "external_instances",
    }
    assert settings == estate_settings(None, None, {}, jobs)
    assert definition_hash(jobs.jobs["A"], jobs, settings) == (
        "sha256:7f82dc011e94799affe01f5e8cba3362525b56923ecbd71e02974349a1ab162a"
    )


def test_settings_capture_defaults_hashes_and_pass_through(tmp_path):
    default = RuntimeProfile()
    assert default.job_profile is None and default.std_file_mode == "append"
    assert default.kill_signals == (2, 9) and default.set_job_attribute_environmentals is True
    assert "job_env" not in default.model_dump()
    assert runtime_hash(default) == runtime_hash(
        RuntimeProfile.model_validate(default.model_dump())
    )
    profile = tmp_path / "site.profile"
    profile.write_text("export POLICY=site\n")
    path = estate_file(
        tmp_path,
        engine=f'job_profile = "{profile}"\nstd_file_mode = "truncate"\nkill_signals = [15,9]\nset_job_attribute_environmentals = false\n',
    )
    prepared = main_config.prepare(path)
    restored = main_config.decoded_parts(main_config.encoded_parts(prepared))
    selected = main_config.profile_of(restored)
    assert selected.job_profile == str(profile) and selected.std_file_mode == "truncate"
    assert selected.kill_signals == (15, 9) and not selected.set_job_attribute_environmentals
    config = Configuration(catalog=restored.catalog, profile=selected)
    assert Configuration.from_bytes(config.to_bytes(), where="synthetic").profile == selected


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_shell", "relative"),
        ("job_shell", "/bin/zsh"),
        ("job_profile", "relative"),
        ("job_profile", "/$PROFILE"),
        ("job_profile", "/bad\0path"),
        ("std_file_mode", "overwrite"),
        ("kill_signals", []),
        ("kill_signals", [0, 9]),
        ("kill_signals", [99999, 9]),
        ("kill_signals", [True, 9]),
        ("kill_signals", [2]),
        ("set_job_attribute_environmentals", 1),
    ],
)
def test_setting_refusals(field, value):
    with pytest.raises(ValidationError):
        RuntimePolicySettings.model_validate({field: value})


@pytest.mark.parametrize("name", sorted(ATTRIBUTE_NAMES))
def test_attribute_names_reserved_without_disclosing_values(name):
    with pytest.raises(ValidationError) as error:
        RuntimeProfile(job_env={name: "private-value-sentinel"})
    assert "private-value-sentinel" not in str(error.value)


@pytest.mark.parametrize("kind", ["c", "b", "f"])
def test_alarm_retry_notification_defaults_and_boundaries(kind):
    job = catalog(kind=kind).jobs["policy_job"]
    assert job_policy(job) == JobRuntimePolicy()
    job.annotations = {
        "max_run_alarm": "0",
        "min_run_alarm": "2147483647",
        "heartbeat_interval": "0",
        "send_notification": "y",
        "alarm_if_fail": "n",
        "alarm_if_terminated": "0",
        "notification_template": '""',
        "notification_alarm_types": '""',
    }
    policy = job_policy(job, hook_configured=True)
    assert policy.max_run_alarm == 0 and policy.min_run_alarm == 2147483647
    assert policy.send_notification and not policy.alarm_if_fail and not policy.alarm_if_terminated


@pytest.mark.parametrize(
    "attribute,text",
    [
        ("max_run_alarm", "-1"),
        ("min_run_alarm", "2147483648"),
        ("heartbeat_interval", "1.5"),
        ("alarm_if_fail", "yes"),
        ("alarm_if_terminated", ""),
        ("send_notification", "true"),
    ],
)
def test_alarm_notification_values_are_validated(attribute, text):
    with pytest.raises(PolicyRefused, match="runtime-attribute"):
        job_policy(catalog(f"{attribute}: {text}").jobs["policy_job"])


@pytest.mark.parametrize(
    "attribute,prefix,mode",
    [
        ("std_out_file", ">", "truncate"),
        ("std_err_file", ">>", "append"),
        ("std_out_file", "", "truncate"),
        ("std_in_file", "", None),
    ],
)
def test_literal_stream_operators_precede_expansion(attribute, prefix, mode):
    assert stream_target("policy_job", attribute, prefix + "$OUTPUT", "truncate") == (
        "$OUTPUT",
        mode,
    )


@pytest.mark.parametrize(
    ("attribute", "code"), [("max_run_alarm", "runtime-attribute"), ("ulimit", "ulimit")]
)
def test_oversized_decimal_refuses_preflight_start_and_reread(tmp_path, attribute, code):
    digits = "9" * 5000
    value = digits if attribute == "max_run_alarm" else f'n="{digits},unlimited"'
    jobs = catalog(f"{attribute}: {value}")
    finding = next(item for item in runner_preflight.preflight(jobs) if item.code == code)
    assert finding.severity == "ERROR" and finding.job == "policy_job"
    assert attribute in finding.message and "; use " in finding.message
    root = tmp_path / "root"
    with pytest.raises(EngineError, match=code):
        runner_startup.start_run(jobs, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
    assert not (root / "engine.db").exists()
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text(
        f"insert_job: policy_job\njob_type: c\ncommand: true\n{attribute}: {value}\n"
    )
    candidate = main_config.prepare(path)
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch=candidate.metadata["launch"])
    with pytest.raises(RereadRefused, match=code):
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists()


def test_leading_zeros_do_not_exceed_numeric_limits():
    assert parse_limits("policy_job", f'n="{"0" * 5000}1,2"')[0].soft == 1
    assert (
        job_policy(catalog(f"max_run_alarm: {'0' * 5000}1").jobs["policy_job"]).max_run_alarm == 1
    )


@pytest.mark.parametrize(
    ("platform", "version", "infinity", "maximum"),
    [
        ("darwin", (3, 12), 2**63 - 1, 2**63 - 2),
        ("darwin", (3, 14), 2**63 - 1, 2**63 - 2),
        ("linux", (3, 12), -1, 2**63 - 1),
        ("linux", (3, 13), -1, 2**64 - 2),
        ("linux", (3, 14), -1, 2**64 - 2),
    ],
)
def test_limit_execution_binding_finite_boundaries(platform, version, infinity, maximum):
    finite_max = runner_preflight._finite_limit_max(platform, "cpython", version, infinity)
    assert finite_max == maximum
    for value in (0, maximum - 1, maximum):
        assert value <= finite_max
    assert maximum + 1 > finite_max


@pytest.mark.parametrize(
    ("platform", "version", "infinity", "maximum"),
    [
        ("darwin", (3, 12), 2**63 - 1, 2**63 - 2),
        ("darwin", (3, 14), 2**63 - 1, 2**63 - 2),
        ("linux", (3, 12), -1, 2**63 - 1),
        ("linux", (3, 14), -1, 2**64 - 2),
    ],
)
def test_execution_preflight_uses_binding_boundary(
    monkeypatch, platform, version, infinity, maximum
):
    check = runner_preflight._finite_limit_max
    monkeypatch.setattr(
        runner_preflight,
        "_finite_limit_max",
        lambda *args: check(platform, "cpython", version, infinity),
    )
    monkeypatch.setattr(runner_preflight.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner_preflight.resource, "RLIM_INFINITY", infinity)
    monkeypatch.setattr(
        runner_preflight.resource, "getrlimit", lambda resource_id: (infinity, infinity)
    )
    for value in (0, maximum):
        findings = runner_preflight.preflight(catalog(f'ulimit: n="{value},{value}"'))
        assert not [item for item in findings if item.code == "ulimit"]
    findings = runner_preflight.preflight(catalog(f'ulimit: n="{maximum + 1},unlimited"'))
    refusal = next(item for item in findings if item.code == "ulimit")
    assert refusal.job == "policy_job" and refusal.severity == "ERROR"
    assert "ulimit" in refusal.message


def test_limit_unknown_execution_binding_has_no_finite_range():
    maximum = runner_preflight._finite_limit_max
    assert maximum("linux", "pypy", (3, 14), -1) is None
    assert maximum("linux", "cpython", (3, 11), -1) is None
    assert maximum("other", "cpython", (3, 14), -1) is None
    assert maximum("darwin", "cpython", (3, 14), -1) is None


@pytest.mark.parametrize("unit,letter", [(512, "c"), (1024, "d")])
def test_scaled_limits_remain_host_independent(unit, letter):
    bound = 2**63 - 1
    last = (bound - 1) // unit
    assert parse_limits("policy_job", f'{letter}="{last},{last}"')[0].hard == last * unit
    assert (
        parse_limits("policy_job", f'{letter}="{last + 1},{last + 1}"')[0].hard == (last + 1) * unit
    )
    finite_max = runner_preflight._finite_limit_max("darwin", "cpython", (3, 14), bound)
    assert last * unit <= finite_max < (last + 1) * unit
    assert parse_limits("policy_job", f'{letter}="0,unlimited"')[0].hard is None
    with pytest.raises(PolicyRefused, match="ulimit"):
        parse_limits("policy_job", f'{letter}="{last + 1},{last}"')


@pytest.mark.parametrize("value", ['n="4,unlimited"', 'n="0,4"'])
def test_unrepresentable_soft_or_hard_refuses_real_preflight_start_and_reread(
    tmp_path, monkeypatch, value
):
    monkeypatch.setattr(runner_preflight, "_finite_limit_max", lambda *args: 3)
    jobs = catalog(f"ulimit: {value}")
    finding = next(item for item in runner_preflight.preflight(jobs) if item.code == "ulimit")
    assert finding.severity == "ERROR" and finding.job == "policy_job"
    assert "ulimit" in finding.message and "smaller representable finite value" in finding.message
    assert "explicit unlimited" in finding.message
    root = tmp_path / "root"
    with pytest.raises(EngineError, match="ulimit"):
        runner_startup.start_run(jobs, root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
    assert not (root / "engine.db").exists()
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text(
        f"insert_job: policy_job\njob_type: c\ncommand: true\nulimit: {value}\n"
    )
    candidate = main_config.prepare(path)
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch=candidate.metadata["launch"])
    with pytest.raises(RereadRefused, match="ulimit"):
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists()


def test_representable_finite_and_explicit_unlimited_pass_binding_check(monkeypatch):
    monkeypatch.setattr(runner_preflight, "_finite_limit_max", lambda *args: 3)
    monkeypatch.setattr(runner_preflight.resource, "getrlimit", lambda resource_id: (-1, -1))
    monkeypatch.setattr(runner_preflight.resource, "RLIM_INFINITY", -1)
    for value in ('n="0,3"', 'n="3,unlimited"', 'n="unlimited,unlimited"'):
        assert not [
            item
            for item in runner_preflight.preflight(catalog(f"ulimit: {value}"))
            if item.code == "ulimit"
        ]


def test_limit_unsupported_and_hard_attainability_remain_enforced(monkeypatch):
    monkeypatch.setattr(runner_preflight, "_finite_limit_max", lambda *args: 10)
    monkeypatch.setattr(runner_preflight.os, "geteuid", lambda: 123)
    monkeypatch.setattr(runner_preflight.resource, "RLIM_INFINITY", -1)
    monkeypatch.setattr(runner_preflight.resource, "getrlimit", lambda resource_id: (1, 2))
    findings = runner_preflight.preflight(catalog('ulimit: n="1,3"'))
    assert any(
        item.code == "ulimit" and "unattainable hard limit" in item.message for item in findings
    )
    monkeypatch.delattr(runner_preflight.resource, "RLIMIT_NOFILE")
    findings = runner_preflight.preflight(catalog('ulimit: n="1,2"'))
    assert any(
        item.code == "ulimit" and "unsupported resource" in item.message for item in findings
    )


def test_repeated_ulimit_entries_keep_each_resource():
    job = catalog('ulimit: c="1,2"\nulimit: n="3,4"').jobs["policy_job"]
    limits = job_policy(job).limits
    assert [(item.resource, item.soft, item.hard) for item in limits] == [
        ("c", 512, 1024),
        ("n", 3, 4),
    ]
    assert parse_limits("policy_job", 'm="1,unlimited"')[0].soft == 1024


@pytest.mark.parametrize(
    "text", ['n="1,2",n="2,3"', 'z="1,2"', 'n="unlimited,2"', 'n="1,2",', "", "(-c 512)"]
)
def test_bad_limits_are_deliberate_refusals(text):
    with pytest.raises(PolicyRefused, match="ulimit"):
        parse_limits("policy_job", text)


@pytest.mark.parametrize(
    "pattern", ["/tmp/*/file", "/tmp/[ab]", "/tmp/**", "/tmp/{a,b}", "/tmp/file\\x"]
)
def test_watcher_pattern_refusals(pattern):
    with pytest.raises(PolicyRefused, match="watch-pattern"):
        job_policy(
            lower_source(f"insert_job: policy_job\njob_type: f\nwatch_file: {pattern}\n").jobs[
                "policy_job"
            ]
        )


@pytest.mark.parametrize("shell", ["/bin/zsh", "/missing/sh", "sh"])
def test_unsupported_passwd_shell_warns_and_falls_back(monkeypatch, shell):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        runner_preflight.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="policy_user", pw_shell=shell),
    )
    selected, warning = resolve_job_shell(None)
    assert selected == "/bin/sh" and shell in warning and selected in warning
    findings = runner_preflight.execution_policy_preflight(catalog())
    assert [(item.severity, item.code) for item in findings] == [("WARN", "shell")]


def test_shell_fallback_is_one_stderr_warning(monkeypatch, capsys):
    monkeypatch.setattr(
        runner_preflight.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_name="policy_user", pw_shell="/bin/zsh"),
    )
    jobs = lower_source(
        "insert_job: one\njob_type: c\ncommand: true\ninsert_job: two\njob_type: c\ncommand: true\n"
    )
    cli_run._preflight_items_or_exit(runner_preflight.execution_policy_preflight(jobs))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.count("preflight WARN [shell]") == 1
    assert "/bin/zsh" in output.err and "/bin/sh" in output.err


def test_explicit_shell_never_falls_back():
    assert resolve_job_shell("/bin/sh", "/bin/zsh") == ("/bin/sh", None)
    with pytest.raises(ValueError, match="job_shell"):
        resolve_job_shell("/missing/sh", "/bin/sh")
    assert any(
        item.code == "shell" and item.severity == "ERROR"
        for item in runner_preflight.execution_policy_preflight(catalog(), job_shell="/missing/sh")
    )


def test_reread_refuses_unavailable_explicit_shell(tmp_path):
    path = estate_file(tmp_path, engine='job_shell = "/missing/sh"\n')
    candidate = main_config.prepare(path)
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch=candidate.metadata["launch"])
    with pytest.raises(RereadRefused, match="shell"):
        reread(tmp_path, live, tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists()


def test_declared_shell_profile_round_trip():
    profile = RuntimeProfile(job_shell="/bin/sh")
    stored = Configuration(catalog=catalog(), profile=profile)
    assert Configuration.from_bytes(stored.to_bytes(), where="synthetic").profile == profile
    assert RuntimeProfile().job_shell is None


@pytest.mark.parametrize("owner", ["policy_user", "policy_user@remote-origin"])
def test_owner_origin_does_not_select_execution_placement(owner):
    assert not runner_preflight.execution_policy_preflight(catalog(f"owner: {owner}"))


@pytest.mark.parametrize(
    "owner", ["foreign", "policy_user@", "policy_user@one@two", "policy_user@two hosts"]
)
def test_foreign_or_malformed_owner_is_refused(owner):
    assert any(
        item.code == "owner"
        for item in runner_preflight.execution_policy_preflight(catalog(f"owner: {owner}"))
    )


def test_generated_attribute_overlay_preserves_literal_machine():
    value = lower_source(
        "insert_job: outer\njob_type: b\ninsert_job: inner\njob_type: c\ncommand: true\nbox_name: outer\nmachine: alias\n"
    )
    job = value.jobs["inner"]
    assert attribute_environment(job, "local-host") == {
        "__job_name": "inner",
        "__machine": "alias",
        "__run_machine": "local-host",
        "__box_name": "outer",
        "__max_exit_success": "0",
    }
    assert attribute_environment(job, "local-host", enabled=False) == {}


@pytest.mark.parametrize(
    "text,code", [("n_retrys: invalid", "n-retrys"), ("std_in_file: /tmp/in", "fw-execution")]
)
def test_lowering_refusals_keep_codes(text, code):
    with pytest.raises(LoweringError, match=code):
        catalog(text, "f" if code == "fw-execution" else "c")
