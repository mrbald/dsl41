"""Main-config acceptance 1–11: resolution, capture, policy, plans and restart (DL-437)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import main_config as main
from dsl41.cli import app
from dsl41.cli_reload import reread_command
from dsl41.lint import LintReport, Violation
from dsl41.reload_plan import Live, RereadRefused, read_plan, reread
from dsl41.runner_clock import EngineError
from dsl41.runner_config import Configuration
from dsl41.runner_journal import replay_store
from dsl41.runner_startup import active_configuration
from dsl41.runner_store import SqliteStore
from dsl41.runner_update import open_plan

pytestmark = pytest.mark.usefixtures("cli_env")

T0 = datetime(2026, 7, 1)
JOB = "insert_job: A\njob_type: c\ncommand: true\nmachine: localhost\n"
PYTHON = """from dsl41.dsl import CatalogBuilder
def estate():
    c = CatalogBuilder()
    c.job('P', command='true', machine='localhost')
    return c
"""


def config_file(
    directory: Path,
    *,
    include: list[str] | None = None,
    properties: list[str] | None = None,
    engine: str = "",
    estate: str = "",
    lint: str = "",
    header: str = "format_version = 1\n",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dsl41.toml"
    path.write_text(
        header
        + f'\n[engine]\nrun_root = "{directory}/run"\n'
        + engine
        + f"\n[estate]\ninclude = {json.dumps(include or ['*.jil'])}\n"
        + f"properties = {json.dumps(properties or [])}\n"
        + estate
        + "\n[lint]\n"
        + lint,
    )
    return path


def estate_file(tmp_path: Path, **options: object) -> Path:
    (tmp_path / "a.jil").write_text(JOB)
    return config_file(tmp_path, **options)  # type: ignore[arg-type]


def live_of(estate: main.Estate, status: dict[str, str] | None = None) -> Live:
    return Live(estate.catalog, estate.settings, 1, status or {}, launch=estate.metadata["launch"])


def test_01_defaults_and_every_key(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    config = main.load_config(path)
    assert config.engine.machine_policy == "strict" and config.engine.as_machine == []
    assert config.estate.timezone == "UTC" and config.estate.semantics == {}
    assert config.lint.refuse == "none" and config.lint.ignore == []
    (tmp_path / "zones").write_text("LOCAL UTC\n")
    (tmp_path / "marked").write_text('[jobs]\nA = "synthetic exception"\n')
    (tmp_path / "base.properties").write_text("VALUE=base\n")
    (tmp_path / "site.properties").write_text("VALUE=site\n")
    path = config_file(
        tmp_path,
        properties=["base.properties"],
        header='format_version = 1\nrequires_dsl41 = ">=1.0"\n',
        engine='environment = "test"\nas_machine = ["executor"]\nmachine_policy = "local-eligible"\naccess_map = "access.toml"\n',
        estate='timezone = "LOCAL"\ntimezone_map = "zones"\nnon_idempotent = "marked"\n[estate.semantics]\nprocess-leak = "terminate"\n[estate.environments.test]\nproperties = ["site.properties"]\n',
        lint='refuse = "error"\nignore = ["L011"]\n',
    )
    loaded = main.prepare(path)
    assert loaded.environment == "test"
    assert loaded.properties == (
        str(tmp_path / "base.properties"),
        str(tmp_path / "site.properties"),
    )
    assert loaded.metadata["non_idempotent"] == {"A": "synthetic exception"}
    assert main.profile_of(loaded).as_machine == ("executor",)


@pytest.mark.parametrize(
    "section,line",
    [
        ("header", "format_version = true"),
        ("header", "format_version = 2"),
        ("header", ""),
        ("header", 'format_version = "1"'),
        ("header", 'format_version = 1\nrequires_dsl41 = ">=999.0"'),
        ("header", 'format_version = 1\nrequires_dsl41 = "==1.0"'),
        ("header", "format_version = 1\nextra = 1"),
        ("engine", "extra = 1"),
        ("engine", "as_machine = [1]"),
        ("engine", 'as_machine = [""]'),
        ("engine", 'machine_policy = "other"'),
        ("engine", 'environment = "missing"'),
        ("engine", 'access_map = ""'),
        ("estate", "extra = 1"),
        ("estate", 'timezone = "No/Such/Zone"'),
        ("estate", "timezone = 3"),
        ("estate", "timezone_map = true"),
        ("estate", 'timezone_map = "*.map"'),
        ("estate", 'non_idempotent = ""'),
        ("estate", "semantics = []"),
        ("estate", 'semantics = {other = "x"}'),
        ("estate", 'semantics = {process-leak = "other"}'),
        ("estate", "[estate.environments.test]\nextra = 1"),
        ("estate", '[estate.environments.""]\nproperties = []'),
        ("estate", "[estate.environments.test]\nproperties = []"),
        ("lint", 'refuse = "other"'),
        ("lint", "ignore = [1]"),
        ("lint", 'ignore = ["L999"]'),
        ("lint", "extra = 1"),
    ],
)
def test_01_config_refusals(tmp_path: Path, section: str, line: str) -> None:
    options = {section: line + "\n"}
    path = config_file(tmp_path, **options)
    with pytest.raises(RereadRefused, match="dsl41.toml"):
        main.load_config(path)


@pytest.mark.parametrize(
    "name", ["", "./a.jil", "../a.jil", "dir//a.jil", "a/", "**/*.jil", "{a,b}.jil"]
)
def test_01_path_refusals(tmp_path: Path, name: str) -> None:
    with pytest.raises(RereadRefused):
        main.load_config(config_file(tmp_path, include=[name]))


def test_01_file_and_encoding_refusals(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(path)
    for selected in (Path("relative.toml"), link, tmp_path / "missing", tmp_path):
        with pytest.raises(RereadRefused):
            main.load_config(selected)
    for data in (
        b"\xff",
        b"[broken",
        b"format_version = 1\n[engine]\nrun_root = 1\n",
        b'format_version = 1\n[engine]\nrun_root = "relative"\n[estate]\ninclude = []\n',
    ):
        path.write_bytes(data)
        with pytest.raises(RereadRefused):
            main.load_config(path)


def test_02_globs_properties_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b"):
        appdir = tmp_path / name
        appdir.mkdir()
        (appdir / "jobs.jil").write_text(JOB.replace("A", name).replace("true", "~{$VALUE}~"))
    (tmp_path / "base.properties").write_text("VALUE=base\n")
    (tmp_path / "site.properties").write_text("VALUE=site\n")
    path = config_file(
        tmp_path,
        include=["b/*.jil", "missing?.jil", "a/[j]*.jil"],
        properties=["base.properties"],
        engine='environment = "test"\n',
        estate='[estate.environments.test]\nproperties = ["site.properties"]\n',
    )
    monkeypatch.chdir("/")
    estate = main.prepare(path)
    assert list(estate.catalog.jobs) == ["a", "b"]
    assert all(j.exec_.command == "site" for j in estate.catalog.jobs.values())
    assert "unmatched pattern" in estate.metadata["diagnostics"][0]
    assert estate.parts == main.prepare(path).parts
    config_file(tmp_path, include=["a/*.jil", "a/jobs.jil"])
    with pytest.raises(RereadRefused, match="a/\\*.jil.*a/jobs.jil"):
        main.prepare(path)
    config_file(tmp_path, include=["a/*.jil"], properties=["*.properties", "site.properties"])
    with pytest.raises(RereadRefused, match="selected twice"):
        main.prepare(path)


def test_02_empty_missing_and_special_inputs(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    with pytest.raises(RereadRefused, match="no catalog files"):
        main.prepare(path)
    config_file(tmp_path, include=["missing.jil"])
    with pytest.raises(RereadRefused, match="missing.jil"):
        main.prepare(path)
    (tmp_path / "empty.jil").write_bytes(b"")
    config_file(tmp_path, include=["empty.jil"])
    assert main.prepare(path).catalog.jobs == {}
    (tmp_path / "directory.jil").mkdir()
    with pytest.raises(RereadRefused, match="not a regular file"):
        main.prepare(config_file(tmp_path, include=["directory.jil"]))
    os.mkfifo(tmp_path / "pipe.jil")
    with pytest.raises(RereadRefused, match="not a regular file"):
        main.prepare(config_file(tmp_path, include=["pipe.jil"]))


def test_03_mixed_compilation_and_duplicate_origins(tmp_path: Path) -> None:
    path = estate_file(tmp_path, include=["*.jil", "*.py", "*.cal"])
    (tmp_path / "author.py").write_text(PYTHON)
    (tmp_path / "dates.cal").write_text("calendar: DAYS\n01/01/2027\n")
    estate = main.prepare(path)
    assert set(estate.catalog.jobs) == {"A", "P"} and "DAYS" in estate.catalog.calendars
    (tmp_path / "author.py").write_text(PYTHON.replace("'P'", "'A'"))
    with pytest.raises(RereadRefused) as caught:
        main.prepare(path)
    assert "a.jil" in str(caught.value) and "author.py" in str(caught.value)


def test_03_origins_use_the_lowered_job_name(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text(JOB.replace("insert_job: A", r"insert_job: A\:B"))
    candidate = main.prepare(path)
    assert candidate.metadata["origins"] == {"A:B": str(tmp_path / "a.jil")}


@pytest.mark.parametrize(
    "source",
    [
        "raise RuntimeError('broken')",
        "def estate(): return 1",
        "x = 1",
        "def estate(:",
        "def estate(): raise RuntimeError('broken')",
    ],
)
def test_03_broken_python_refuses(tmp_path: Path, source: str) -> None:
    path = config_file(tmp_path, include=["author.py"])
    (tmp_path / "author.py").write_text(source)
    with pytest.raises(RereadRefused, match="author.py"):
        main.prepare(path)


@pytest.mark.parametrize("policy", ["none", "error", "warn"])
@pytest.mark.parametrize("ignore", [[], ["L001", "L011"]])
def test_04_lowering_is_always_hard(tmp_path: Path, policy: str, ignore: list[str]) -> None:
    path = estate_file(tmp_path, lint=f'refuse = "{policy}"\nignore = {json.dumps(ignore)}\n')
    (tmp_path / "a.jil").write_text(JOB + "unregistered_attribute: value\n")
    with pytest.raises(RereadRefused, match="unregistered_attribute"):
        main.prepare(path)


@pytest.mark.parametrize("policy", ["none", "error", "warn"])
@pytest.mark.parametrize("severity", ["error", "warn", "info"])
@pytest.mark.parametrize("ignored", [False, True])
def test_05_policy_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str, severity: str, ignored: bool
) -> None:
    path = estate_file(
        tmp_path, lint=f'refuse = "{policy}"\nignore = {json.dumps(["L011"] if ignored else [])}\n'
    )
    monkeypatch.setattr(
        main,
        "lint_catalog",
        lambda *a, **k: LintReport(
            violations=[Violation(code="L011", severity=severity, message="synthetic finding")]
        ),
    )
    selected, report = main.policy_report(main.prepare(path))
    assert bool(report.violations) is not ignored
    assert any(selected.blocks(v.severity) for v in report.violations) == (
        not ignored
        and (severity == "error" and policy != "none" or severity == "warn" and policy == "warn")
    )


def test_05_preflight_and_exceptions_are_hard(tmp_path: Path) -> None:
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text(JOB + "envvars: X=value\n")
    candidate = main.prepare(path)
    with pytest.raises(RereadRefused, match="preflight ERROR"):
        reread(tmp_path, live_of(candidate), tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists()
    (tmp_path / "non-idempotent.toml").write_text("[jobs]\nA = 2\n")
    with pytest.raises(RereadRefused, match="reason"):
        main.prepare(path)


@pytest.mark.parametrize("execution", ["machine: remote.invalid", "owner: another_synthetic_user"])
def test_05_reread_launch_preflight_cannot_be_suppressed(tmp_path: Path, execution: str) -> None:
    path = estate_file(tmp_path)
    (tmp_path / "a.jil").write_text(JOB.replace("machine: localhost", execution))
    candidate = main.prepare(path)
    with pytest.raises(RereadRefused, match="preflight ERROR"):
        reread(tmp_path, live_of(candidate), tmp_path / "plans", candidate=candidate)
    assert not (tmp_path / "plans").exists()


@pytest.mark.parametrize("change", ["source", "property", "membership"])
def test_06_change_during_preparation_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = estate_file(tmp_path, properties=["site.properties"])
    prop = tmp_path / "site.properties"
    prop.write_text("VALUE=one\n")
    compile_parts = main.from_parts

    def changing(parts):
        candidate = compile_parts(parts)
        if change == "source":
            (tmp_path / "a.jil").write_text(JOB + "description: changed\n")
        elif change == "property":
            prop.write_text("VALUE=two\n")
        else:
            (tmp_path / "new.jil").write_text("")
        return candidate

    monkeypatch.setattr(main, "from_parts", changing)
    with pytest.raises(RereadRefused):
        main.prepare(path)
    assert not (tmp_path / "run" / "plans").exists()


def test_07_collision_free_capture_and_python_free_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for appname in ("one", "two"):
        directory = tmp_path / appname
        directory.mkdir()
        (directory / "jobs.jil").write_text(JOB.replace("A", appname))
        (directory / "site.properties").write_text(f"VALUE={appname}\n")
    (tmp_path / "author.py").write_text(PYTHON)
    path = config_file(tmp_path, include=["*/*.jil", "author.py"], properties=["*/*.properties"])
    candidate = main.prepare(path)
    root = tmp_path / "run"
    result = reread(tmp_path, live_of(candidate), root / "plans", candidate=candidate, start=T0)
    for name in ("one", "two"):
        (tmp_path / name / "jobs.jil").unlink()
        (tmp_path / name / "site.properties").unlink()
    (tmp_path / "author.py").write_text("raise AssertionError('must not import')")
    import dsl41.cli_common

    monkeypatch.setattr(
        dsl41.cli_common, "module_builder", lambda *a, **k: pytest.fail("Python imported")
    )
    opened = open_plan(root, result.token)
    assert set(opened.estate.catalog.jobs) == {"one", "two", "P"}
    assert opened.fingerprint() == main.fingerprint(candidate)
    assert all("/plans/" not in name for name in opened.fingerprint())
    document = read_plan(root / "plans", result.token)
    assert document["plan_format_version"] == 2 and "lint_findings" in document


def test_08_origins_moves_and_shared_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = estate_file(tmp_path)
    active = main.prepare(path)
    (tmp_path / "a.jil").rename(tmp_path / "moved.jil")

    def report(live):
        from dsl41.reread_worker import preview

        candidate = main.prepare(path)
        return reread_command(
            send=lambda *args: {
                "ok": True,
                "live_rev": live.revision,
                "source": {"path": str(path)},
                "preview": preview(candidate, live),
            }
        )

    assert report(live_of(active, {"A": "RUNNING"})) == 0
    output = capsys.readouterr().out
    assert "source:" in output and "a.jil" in output and "moved.jil" in output and "A" in output
    (tmp_path / "moved.jil").write_text(JOB.replace("A", "B"))
    assert report(live_of(active)) == 0
    output = capsys.readouterr().out
    assert "added   1 job: B" in output and "removed   1 job: A" in output and "source:" in output
    config_file(tmp_path, estate='timezone = "Europe/Zurich"\n')
    assert report(live_of(active)) == 0
    assert "shared settings sources:" in capsys.readouterr().out


def test_09_partial_plan_and_format_dispatch(tmp_path: Path) -> None:
    candidate = main.prepare(estate_file(tmp_path))
    root = tmp_path / "run"
    result = reread(tmp_path, live_of(candidate), root / "plans", candidate=candidate, start=T0)
    plan = json.loads(result.plan_json.read_bytes())
    result.plan_json.unlink()
    assert read_plan(root / "plans", result.token) is None
    plan["plan_format_version"] = 999
    result.plan_json.write_text(json.dumps(plan))
    with pytest.raises(RereadRefused, match="plan_format_version 999"):
        read_plan(root / "plans", result.token)
    from dsl41.runner_update import PlanUnreadable

    with pytest.raises(PlanUnreadable, match="plan_format_version 999"):
        open_plan(root, result.token)
    plan["plan_format_version"] = 2
    plan["unknown_field"] = True
    result.plan_json.write_text(json.dumps(plan))
    assert read_plan(root / "plans", result.token) is not None
    for version in (1, 2):
        configuration = Configuration(
            catalog=candidate.catalog,
            profile=main.profile_of(candidate),
            source_capture=main.encoded_parts(candidate) if version == 2 else {},
        )
        assert Configuration.from_bytes(configuration.to_bytes(), where="test") == configuration
        payload = json.loads(configuration.to_bytes())
        payload["configuration_format_version"] = 999
        with pytest.raises(EngineError, match="configuration_format_version 999"):
            Configuration.from_bytes(json.dumps(payload).encode(), where="test")
        if version == 1:
            payload.pop("configuration_format_version")
        else:
            payload["configuration_format_version"] = 2
        payload.pop("job_order")
        payload["unknown"] = True
        with pytest.raises(EngineError, match="unknown configuration fields"):
            Configuration.from_bytes(json.dumps(payload).encode(), where="test")
    for version in (True, "2", None):
        with pytest.raises(EngineError, match="configuration_format_version"):
            Configuration.from_bytes(
                json.dumps({"configuration_format_version": version}).encode(), where="test"
            )
    with pytest.raises(EngineError, match="version 2 needs source_capture"):
        Configuration.from_bytes(b'{"configuration_format_version": 2}', where="test")
    with pytest.raises(EngineError, match="configuration must be an object"):
        Configuration.from_bytes(b"[]", where="test")


@pytest.mark.usefixtures("cli_env")
def test_06_run_never_mixes_two_config_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = estate_file(tmp_path)
    prepare = main.prepare

    def changed(selected: Path, raw: bytes | None = None):
        selected.write_text(selected.read_text().replace('/run"', '/other"'))
        return prepare(selected, raw)

    import dsl41.cli_run

    async def unexpected(*args, **kwargs):
        pytest.fail("mixed config reached the runner")

    monkeypatch.setattr(main, "prepare", changed)
    monkeypatch.setattr(dsl41.cli_run, "_serve_run", unexpected)
    result = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert result.exit_code == 2 and "changed during preparation" in result.output
    assert not (tmp_path / "run").exists() and not (tmp_path / "other").exists()


def test_09_main_config_refuses_a_foreign_store(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    foreign = root / "engine.db"
    foreign.write_bytes(b"synthetic foreign store")
    with pytest.raises(EngineError, match="not a dsl41 store"):
        active_configuration(root)
    assert foreign.read_bytes() == b"synthetic foreign store"


@pytest.mark.parametrize("policy", ["none", "warn", "error"])
def test_05_exception_declarations_cannot_be_ignored(tmp_path: Path, policy: str) -> None:
    path = estate_file(tmp_path, lint=f'refuse = "{policy}"\nignore = ["L025"]\n')
    (tmp_path / "non-idempotent.toml").write_text('[jobs]\nMISSING = "synthetic exception"\n')
    with pytest.raises(RereadRefused, match="invalid exception declaration"):
        main.prepare(path)
    (tmp_path / "non-idempotent.toml").write_text('[jobs]\nA = "synthetic exception"\n')
    assert main.policy_report(main.prepare(path))[1].by_code("L024")


@pytest.mark.parametrize("kind", ["jil", "python", "properties", "timezone_map", "exceptions"])
def test_03_invalid_utf8_names_the_input(tmp_path: Path, kind: str) -> None:
    options = {}
    selected = tmp_path / "a.jil"
    if kind == "python":
        selected = tmp_path / "author.py"
        options["include"] = [selected.name]
    elif kind == "properties":
        selected = tmp_path / "site.properties"
        options["properties"] = [selected.name]
    elif kind == "timezone_map":
        selected = tmp_path / "zones"
        options["estate"] = 'timezone_map = "zones"\n'
    elif kind == "exceptions":
        selected = tmp_path / "non-idempotent.toml"
    path = estate_file(tmp_path, **options)
    selected.write_bytes(b"\xff")
    with pytest.raises(RereadRefused) as caught:
        main.prepare(path)
    assert str(selected) in str(caught.value)


def test_02_shell_tokens_stay_literal(tmp_path: Path) -> None:
    (tmp_path / "$APP").mkdir()
    (tmp_path / "$APP" / "~jobs.jil").write_text(JOB)
    assert set(main.prepare(config_file(tmp_path, include=["$APP/~jobs.jil"])).catalog.jobs) == {
        "A"
    }


def boot_and_stop(path: Path, *, pending: bool = False) -> str:
    log = path.parent / "boot.log"
    with (
        log.open("w") as output,
        subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 30
            while child.poll() is None:
                text = log.read_text()
                ready = "engine up; control socket:" in text
                if ready and (not pending or "pending source changes; run dsl41 reread" in text):
                    break
                assert time.monotonic() < deadline, text
                time.sleep(0.05)
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=30)
            text = log.read_text()
            assert child.returncode == 0, text
            return text
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)


@pytest.mark.parametrize("findings", [False, True])
@pytest.mark.parametrize("refuse", ["none", "error"])
def test_start_and_reread_share_counts_without_changing_answers(
    tmp_path, monkeypatch, capsys, findings, refuse
):
    from dsl41 import reread_worker
    from dsl41.runner_preflight import PreflightItem

    monkeypatch.setattr(
        reread_worker,
        "preflight_items",
        lambda *args: [PreflightItem(severity="WARN", code="shell", message="synthetic preflight")],
    )

    path = estate_file(tmp_path, lint=f'refuse = "{refuse}"\n')
    candidate = main.prepare(path)
    candidate.metadata["diagnostics"] = ["synthetic diagnostic"]
    report = LintReport(
        violations=[
            Violation(code=code, severity=severity, message=message)
            for code, severity, message in (
                ("known-unsupported", "warn", "first"),
                ("L002", "info", "external setter"),
                ("L001", "error", "blocking detail"),
                ("L002", "warn", "first setter"),
                ("known-unsupported", "warn", "second"),
                ("L002", "warn", "second setter"),
            )
        ]
        if findings
        else []
    )
    monkeypatch.setattr(main, "lint_catalog", lambda *args, **kwargs: report)
    policy = f"lint policy: refuse={refuse}; ignore=none"
    expected = [policy, "synthetic diagnostic"]
    if findings:
        expected += [
            "L001 error: blocking detail",
            "L002 warn: 2 findings",
            "L002 info: 1 finding",
            "known-unsupported warn: 2 findings",
            f"full list: dsl41 lint --config {shlex.quote(str(path))}",
        ]
    if findings and refuse == "error":
        with pytest.raises(main.typer.Exit) as caught:
            main.emit_policy(candidate, summarize=True)
        assert caught.value.exit_code == 2
    else:
        assert not main.emit_policy(candidate, summarize=True)
    assert capsys.readouterr().err.splitlines() == expected
    live = Live(candidate.catalog, candidate.settings, 1, {}, launch={})
    answer = {
        "ok": True,
        "live_rev": 1,
        "source": {"path": str(path)},
        "preview": reread_worker.preview(candidate, live),
    }
    preflight = "preflight WARN [shell]: synthetic preflight"
    host = "host launch settings changed; restart before update"
    assert answer["preview"]["lint_messages"] == [
        policy,
        *(v.render() for v in report.violations),
        host,
        preflight,
    ]
    assert answer["preview"]["lint_summary"] == [expected[0], *expected[2:], host, preflight]
    update = reread_worker.preview(candidate, live, include_definition_changes=False)
    assert "lint_summary" not in update
    assert update["lint_messages"] == answer["preview"]["lint_messages"]
    before = deepcopy(answer)
    assert reread_command(send=lambda *_: answer) == 0
    # Diagnostics follow lint output on reread (DL-483).
    assert capsys.readouterr().err.splitlines() == [
        expected[0],
        *expected[2:],
        host,
        preflight,
        expected[1],
    ]
    assert answer == before
    result = CliRunner().invoke(app, ["lint", "--config", str(path)])
    assert result.exit_code == int(findings and refuse == "error")
    assert result.stderr.splitlines() == [policy, *(v.render() for v in report.violations)]


@pytest.mark.parametrize("input_kind", ["filename", "machine", "glob"])
def test_finding_shaped_input_text_is_verbatim_at_start_and_reread(
    tmp_path, monkeypatch, capsys, input_kind
):
    import pwd
    from dsl41 import cli_run, reread_worker, runner_preflight

    monkeypatch.setattr(runner_preflight, "_local_identity", lambda *_: frozenset({"localhost"}))
    account = pwd.struct_passwd(
        ("synthetic", "x", os.geteuid(), os.getegid(), "", str(tmp_path), "/bin/sh")
    )
    monkeypatch.setattr(runner_preflight.pwd, "getpwuid", lambda *_: account)

    async def serve(*args, **kwargs):
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    name = "a:1: L002 info: x.jil" if input_kind == "filename" else "a.jil"
    source = JOB
    if input_kind == "filename":
        source += "condition: s(absent)\n"
    elif input_kind == "machine":
        source = source.replace("machine: localhost", 'machine: "foreign:1: L002 info: node"')
    (tmp_path / name).write_text(source)
    include = [name]
    if input_kind == "glob":
        include.append("missing*:1: L002 info: x.jil")
    path = config_file(tmp_path, include=include, lint='refuse = "warn"\nignore = ["L011"]\n')
    candidate = main.prepare(path)
    policy, report = main.policy_report(candidate)
    if input_kind == "filename":
        expected = report.by_code("L001")[0].render()
    elif input_kind == "machine":
        item = next(
            item for item in reread_worker.preflight_items(candidate, T0) if item.code == "machine"
        )
        expected = f"preflight ERROR [machine] A: {item.message}"
    else:
        expected = candidate.metadata["diagnostics"][0]
    start = CliRunner().invoke(app, ["run", "--config", str(path)])
    assert start.exit_code == (0 if input_kind == "glob" else 2), start.output
    footer = ["preflight: refusing to run (runner-design ss8)"] if input_kind == "machine" else []
    assert start.stderr.splitlines() == [policy.describe(), expected, *footer]
    view = reread_worker.preview(candidate, live_of(candidate))
    assert (
        reread_command(
            send=lambda *_: {
                "ok": True,
                "live_rev": 1,
                "source": {"path": str(path)},
                "preview": view,
            }
        )
        == 0
    )
    reread_output = capsys.readouterr().err
    assert reread_output.splitlines() == [policy.describe(), expected]
    assert "L002 info: 1 finding" not in start.stderr + reread_output
    assert "full list:" not in start.stderr + reread_output
    main.emit_policy(candidate, refuse=False)
    raw = capsys.readouterr().err.splitlines()
    if input_kind == "filename":
        assert view["lint_messages"] == raw
    elif input_kind == "machine":
        assert view["lint_messages"] == [*raw, expected]
    else:
        assert view["diagnostics"] == [expected]


@pytest.mark.parametrize("policy", ["none", "error", "warn"])
@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize(
    "attributes,expected",
    [
        ("group: batch\n", ["group"]),
        (
            "elevated: yes\ninteractive: true\navg_runtime: +1\njob_load: +1\n",
            ["elevated", "interactive", "avg_runtime", "job_load"],
        ),
        ("elevated: no\ninteractive: false\navg_runtime: +0\njob_load: +0\n", []),
    ],
)
def test_known_unsupported_lint_policy(
    tmp_path, monkeypatch, capsys, policy, ignored, attributes, expected
):
    from dsl41 import cli_run

    ignore = ["L011", *(["known-unsupported"] if ignored else [])]
    path = estate_file(tmp_path, lint=f'refuse = "{policy}"\nignore = {json.dumps(ignore)}\n')
    (tmp_path / "a.jil").write_text(
        JOB.replace("machine: localhost", "machine: M")
        + attributes
        + "insert_machine: M\ntype: v\nmachine: localhost\n"
        + "insert_machine: localhost\ntype: a\nnode_name: localhost\n"
    )
    candidate = main.prepare(path)
    blocked = policy == "warn" and not ignored and bool(expected)
    warning_count = len(expected) * int(not ignored)
    entered = []

    async def serve(*args, **kwargs):
        entered.append(args[3])
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    runner = CliRunner()
    for command, refusal in [("lint", 1), ("run", 2)]:
        result = runner.invoke(app, [command, "--config", str(path)])
        assert result.exit_code == (refusal if blocked else 0), result.output
        full = command == "lint" or blocked
        assert result.output.count("known-unsupported warn:") == (
            warning_count if full else int(bool(warning_count))
        )
        for attribute in expected:
            assert result.output.count(f"job A: {attribute}:") == int(not ignored and full)
        assert ("full list:" in result.stderr) == bool(warning_count and not full)
    assert bool(entered) is not blocked
    if entered:
        assert sum(item.code == "known-unsupported" for item in entered[0]) == warning_count
    assert not (tmp_path / "run").exists()
    from dsl41.reread_worker import preview

    code = reread_command(
        send=lambda *_: {
            "ok": True,
            "live_rev": 1,
            "source": {"path": str(path)},
            "preview": preview(candidate, live_of(candidate)),
        }
    )
    output = capsys.readouterr()
    assert code == 0
    warnings = "\n".join(
        line for line in (output.out + output.err).splitlines() if not line.startswith("reread:")
    )
    assert warnings.count("known-unsupported warn:") == (
        warning_count if blocked else int(bool(warning_count))
    )
    for attribute in expected:
        assert warnings.count(f"job A: {attribute}:") == int(not ignored and blocked)
    assert not (tmp_path / "run").exists()
    if blocked:
        with pytest.raises(RereadRefused, match="known-unsupported"):
            reread(tmp_path, live_of(candidate), tmp_path / "run" / "plans", candidate=candidate)
        assert not (tmp_path / "run" / "plans").exists()
    else:
        result = reread(
            tmp_path, live_of(candidate), tmp_path / "run" / "plans", candidate=candidate
        )
        document = read_plan(tmp_path / "run" / "plans", result.token)
        assert not any(
            item["code"] == "known-unsupported" for item in document["preflight_warnings"]
        )
        assert (
            sum(item["code"] == "known-unsupported" for item in document["lint_findings"])
            == warning_count
        )


def test_known_unsupported_keeps_other_reread_findings(tmp_path, capsys):
    from dsl41.reread_worker import preview

    path = estate_file(tmp_path, lint='refuse = "error"\n')
    active = main.prepare(path)
    (tmp_path / "a.jil").write_text(JOB + "group: batch\ncondition: s(MISSING)\n")
    assert (
        reread_command(
            send=lambda *_: {
                "ok": True,
                "live_rev": 1,
                "source": {"path": str(path)},
                "preview": preview(main.prepare(path), live_of(active)),
            }
        )
        == 0
    )
    output = capsys.readouterr()
    assert "changed 1 job" in output.out and "1 job: A" in output.out
    assert output.err.count("L001 error:") == 1
    assert "known-unsupported warn: 1 finding" in output.err
    assert "job A: group:" not in output.err
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "configured,policy,ignored",
    [(False, "none", False), (True, "none", False), (True, "warn", False), (True, "warn", True)],
)
def test_known_unsupported_rehearse_reports_once(tmp_path, configured, policy, ignored):
    path = estate_file(
        tmp_path,
        engine="kill_signals = [2, 9]\n",
        lint=f'refuse = "{policy}"\nignore = {json.dumps(["L011"] + (["known-unsupported"] if ignored else []))}\n',
    )
    (tmp_path / "a.jil").write_text(JOB + "group: batch\n")
    selection = ["--config", str(path)] if configured else [str(tmp_path / "a.jil")]
    result = CliRunner().invoke(app, ["rehearse", *selection, "--format", "json", "--hours", "1"])
    assert result.exit_code == (2 if policy == "warn" and not ignored else 0), result.output
    assert result.stderr.count("job A: group:") == int(not ignored)
    if result.exit_code == 0:
        json.loads(result.stdout)


@pytest.mark.parametrize("ignored", [False, True])
def test_known_unsupported_restart_reports_without_refusal(short_root, ignored):
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_startup import start_run
    from store_surgery import rows

    path = estate_file(
        short_root,
        engine='job_shell = "/bin/sh"\n',
        lint='refuse = "warn"\nignore = '
        + json.dumps(["L011"] + (["known-unsupported"] if ignored else []))
        + "\n",
    )
    (short_root / "a.jil").write_text(
        JOB
        + "group: captured\napplication: batch\ncondition: s(B)\n"
        + JOB.replace("insert_job: A", "insert_job: B")
        + "group: captured\napplication: batch\ncondition: s(A)\n"
    )
    captured = main.prepare(path)
    root = short_root / "run"
    engine = start_run(
        captured.catalog,
        root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter()},
        launched=main.profile_of(captured),
        source_capture=main.encoded_parts(captured),
    )
    engine.journal.close()
    (short_root / "a.jil").write_text(JOB + "application: pending\n")
    output = boot_and_stop(path)
    assert "engine up; control socket:" in output
    assert output.count("known-unsupported warn: 5 findings") == int(not ignored)
    assert output.count(
        f"files on disk (may differ from applied estate until update): dsl41 lint --config {path}"
    ) == int(not ignored)
    assert "job A: group:" not in output
    assert "job B: group:" not in output
    assert "job A: application:" not in output
    assert "preflight WARN [skeleton-cycle]: cycle in the AND-success skeleton" in output
    warnings = [
        item
        for _, kind, body in rows(root)
        if kind == "preflight"
        for item in body["items"]
        if item["code"] == "known-unsupported"
    ]
    expected = [
        {
            "severity": "WARN",
            "code": "known-unsupported",
            "job": name,
            "message": f"job {name}: {attribute}: known but unsupported; it has no effect in this release",
        }
        for name in ("A", "B")
        for attribute in ("application", "group")
    ] + [
        {
            "severity": "WARN",
            "code": "known-unsupported",
            "job": None,
            "message": "engine.job_shell: known but unsupported; it has no effect in this release",
        }
    ]
    assert warnings == ([] if ignored else expected)
    assert active_configuration(root).catalog == captured.catalog


def test_restart_pointer_names_disk_findings_when_applied_warning_is_absent_on_disk(short_root):
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_startup import start_run

    path = estate_file(short_root, lint='ignore = ["L011"]\n')
    source = short_root / "a.jil"
    source.write_text(JOB + "group: captured\n")
    applied = main.prepare(path)
    engine = start_run(
        applied.catalog,
        short_root / "run",
        clock=RealClock(),
        adapters={"CMD": FakeAdapter()},
        launched=main.profile_of(applied),
        source_capture=main.encoded_parts(applied),
    )
    engine.journal.close()
    source.write_text(JOB)
    output = boot_and_stop(path, pending=True)
    assert "known-unsupported warn: 1 finding" in output
    assert (
        f"files on disk (may differ from applied estate until update): dsl41 lint --config {path}"
    ) in output
    lint = CliRunner().invoke(app, ["lint", "--config", str(path)])
    assert lint.exit_code == 0, lint.output
    assert lint.stderr == "lint policy: refuse=none; ignore=L011\n"
    assert active_configuration(short_root / "run").catalog == applied.catalog


def test_known_unsupported_startup_and_socket_reread_process(short_root):
    from store_surgery import rows

    path = estate_file(short_root, engine='job_shell = "/bin/sh"\n', lint='ignore = ["L011"]\n')
    (short_root / "a.jil").write_text(
        JOB.replace("machine: localhost", "machine: M")
        + "n_retrys: 2\ngroup: batch\nelevated: yes\ninteractive: true\navg_runtime: +1\njob_load: +1\n"
        + "insert_machine: M\ntype: v\nmachine: localhost\n"
        + "insert_machine: localhost\ntype: a\nnode_name: localhost\n"
    )
    expected = ["n_retrys", "group", "elevated", "interactive", "avg_runtime", "job_load"]
    root = short_root / "run"
    log = short_root / "process.log"
    with (
        log.open("w") as output,
        subprocess.Popen(
            [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as child,
    ):
        try:
            deadline = time.monotonic() + 30
            while "engine up; control socket:" not in log.read_text():
                assert child.poll() is None and time.monotonic() < deadline, log.read_text()
                time.sleep(0.05)
            assert f"known-unsupported warn: {len(expected) + 1} findings" in log.read_text()
            for attribute in expected:
                assert f"job A: {attribute}:" not in log.read_text()
            assert "engine.job_shell:" not in log.read_text()
            jil = short_root / "a.jil"
            jil.write_text(jil.read_text().replace("group: batch", "group: pending"))
            for policy, ignored in [("none", False), ("warn", False), ("warn", True)]:
                config_file(
                    short_root,
                    engine='job_shell = "/bin/sh"\n',
                    lint=f'refuse = "{policy}"\nignore = {json.dumps(["L011"] + (["known-unsupported"] if ignored else []))}\n',
                )
                before = set((root / "plans").iterdir())
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "dsl41",
                        "reread",
                        "-S",
                        str(root / "control.sock"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result.stdout + result.stderr
                assert "changed 1 job" in result.stdout and "1 job: A" in result.stdout
                assert "token:" not in result.stdout
                assert set((root / "plans").iterdir()) == before
                full = policy == "warn" and not ignored
                assert result.stderr.count("known-unsupported warn:") == (
                    len(expected) + 1 if full else int(not ignored)
                )
                for attribute in expected:
                    assert result.stderr.count(f"job A: {attribute}:") == int(full)
                assert result.stderr.count("engine.job_shell:") == int(full)
            assert active_configuration(root).catalog.jobs["A"].passthrough["group"] == "batch"
            with closing(SqliteStore(root / "engine.db", read_only=True)) as store:
                assert store.current_config_rev() == 1
        finally:
            child.terminate()
            try:
                child.wait(timeout=30)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
    assert child.returncode == 0, log.read_text()
    warnings = [
        item
        for _, kind, body in rows(root)
        if kind == "preflight"
        for item in body["items"]
        if item["code"] == "known-unsupported"
    ]
    assert len(warnings) == len(expected) + 1


def test_10_restart_uses_stored_catalog_and_reports_pending(short_root: Path) -> None:
    tmp_path = short_root
    path = estate_file(tmp_path)
    assert "engine" in boot_and_stop(path)
    before = active_configuration(tmp_path / "run")
    (tmp_path / "a.jil").write_text("malformed: deliberately\n")
    config_file(tmp_path, lint='refuse = "warn"\n')
    output = boot_and_stop(path, pending=True)
    assert "2 pending source changes; run dsl41 reread" in output
    assert active_configuration(tmp_path / "run").catalog == before.catalog
    with closing(SqliteStore(tmp_path / "run" / "engine.db", read_only=True)) as store:
        replayed = replay_store(store, where="test")
        assert set(replayed.oracle.catalog.jobs) == {"A"}


@pytest.mark.usefixtures("cli_env")
def test_11_cli_modes_and_rehearsal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = estate_file(tmp_path)
    runner = CliRunner()
    monkeypatch.setattr(main, "DEFAULT_CONFIG", path)
    assert runner.invoke(app, ["lint"]).exit_code == 0
    for spelling in (str(tmp_path) + "//dsl41.toml", str(tmp_path) + "/./dsl41.toml", ""):
        assert runner.invoke(app, ["lint", "--config", spelling]).exit_code == 2
    assert runner.invoke(app, ["lint", "--config", str(path), "--strict"]).exit_code == 2
    assert (
        runner.invoke(app, ["lint", str(tmp_path / "a.jil"), "--config", str(path)]).exit_code == 2
    )
    for flag in ("--permit-unknown", "--timezone", "--environment", "--properties"):
        args = ["lint", "--config", str(path), flag]
        if flag != "--permit-unknown":
            args.append("unused")
        assert runner.invoke(app, args).exit_code == 2
    for flag, value in [
        ("--run-root", str(tmp_path / "run")),
        ("--machine-policy", "strict"),
        ("--grace-term", "10"),
    ]:
        assert runner.invoke(app, ["run", "--config", str(path), flag, value]).exit_code == 2
    for root in (tmp_path / "run", tmp_path / "run" / "rehearsal", tmp_path):
        refused = runner.invoke(app, ["rehearse", "--config", str(path), "--run-root", str(root)])
        assert refused.exit_code == 2 and "must differ" in refused.output
    assert not (tmp_path / "run").exists()
    result = runner.invoke(app, ["rehearse", "--config", str(path), "--hours", "0.01"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "run").exists()
    assert runner.invoke(app, ["lint", str(tmp_path / "a.jil")]).exit_code == 0
    manifest = tmp_path / "estate.toml"
    manifest.write_text('name = "synthetic"\nfiles = ["a.jil"]\n')
    assert runner.invoke(app, ["lint", "--manifest", str(manifest)]).exit_code == 0


@pytest.mark.parametrize("command", ["run", "rehearse"])
@pytest.mark.parametrize("source", ["config", "flags", "manifest"])
@pytest.mark.parametrize(
    ("zone", "map_state", "message"),
    [
        ("Zurich", "absent", "pin it with"),
        ("Zurich", "empty", "pin it with"),
        ("Zurich", "missing-name", "not in the supplied"),
        ("Indianapolis", "absent", "pin it with"),
        ("X", "absent", "zone name or a map via"),
        ("Not/AZone", "absent", "listing in via"),
    ],
)
@pytest.mark.usefixtures("fixed_zone_names")
def test_city_timezone_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    source: str,
    zone: str,
    map_state: str,
    message: str,
) -> None:
    """DL-62/DL-163: findings name the input; empty maps retain the warned city default."""
    from dsl41 import cli_run

    jil = tmp_path / "a.jil"
    jil.write_text(JOB + f'date_conditions: 1\nstart_times: "08:00"\ntimezone: {zone}\n')
    zones = tmp_path / "zones"
    zones.write_text("LOCAL UTC\n" if map_state == "missing-name" else "")
    map_setting = 'timezone_map = "zones"\n' if map_state != "absent" else ""
    if source == "config":
        path = config_file(tmp_path, estate=map_setting)
        args = [command, "--config", str(path)]
        map_source = "estate.timezone_map"
    elif source == "manifest":
        path = tmp_path / "estate.toml"
        path.write_text('name = "synthetic"\nfiles = ["a.jil"]\n' + map_setting)
        args = [command, "--manifest", str(path)]
        map_source = "timezone_map"
    else:
        args = [command, str(jil)]
        if map_state != "absent":
            args += ["--timezone-map", str(zones)]
        map_source = "--timezone-map"
    if command == "run" and source != "config":
        args += ["--run-root", str(tmp_path / "run")]
    if command == "rehearse":
        start = "2026-07-01T11:59:00" if zone == "Indianapolis" else "2026-07-01T05:59:00"
        args += ["--start", start, "--hours", "0.05", "--format", "json"]

    served = []

    async def serve(*args: object, **kwargs: object) -> int:
        served.append(True)
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    result = CliRunner().invoke(app, args)
    assert f"{message} {map_source}" in result.output
    if map_state == "missing-name":
        assert (
            f"timezone 'Zurich' is not resolvable: not in the supplied {map_source};"
            " without a map it would be Europe/Zurich; add the line: Zurich Europe/Zurich"
        ) in result.output
    if source != "flags":
        assert "--timezone-map" not in result.output
    if zone not in {"Zurich", "Indianapolis"} or map_state == "missing-name":
        assert result.exit_code == 2, result.output
        assert "preflight ERROR [timezone] A" in result.output
        assert not served
        if zone == "X":
            assert "ambiguous city name (A/X, B/X)" in result.output
    else:
        assert result.exit_code == 0, result.output
        assert "preflight WARN [timezone] A" in result.output
        expected = "Europe/Zurich" if zone == "Zurich" else "America/Indiana/Indianapolis"
        assert f"timezone {zone!r} assumed to be {expected}" in result.output
        assert "unique zoneinfo city match, DL-62" in result.output
        assert served == ([True] if command == "run" else [])
        if command == "rehearse":
            trace = json.loads(result.stdout)["trace"]
            starts = [row["at"] for row in trace if row["transition"].endswith("->STARTING")]
            expected_start = "2026-07-01T06:00:00" if zone == "Zurich" else "2026-07-01T12:00:00"
            assert starts[0] == expected_start


@pytest.mark.parametrize("source", ["config", "manifest"])
@pytest.mark.parametrize(
    ("zone", "supplied_map", "message"),
    [
        ("Zurich", False, "pin it with"),
        ("Zurich", True, "not in the supplied"),
        ("Indianapolis", False, "pin it with"),
        ("X", False, "zone name or a map via"),
        ("Not/AZone", False, "listing in via"),
    ],
)
@pytest.mark.usefixtures("fixed_zone_names")
def test_reload_timezone_message_source(
    tmp_path: Path, source: str, zone: str, supplied_map: bool, message: str
) -> None:
    from dsl41 import reload_plan

    (tmp_path / "a.jil").write_text(
        JOB + f'date_conditions: 1\nstart_times: "08:00"\ntimezone: {zone}\n'
    )
    (tmp_path / "zones").write_text("LOCAL UTC\n")
    map_setting = 'timezone_map = "zones"\n' if supplied_map else ""
    if source == "config":
        estate = main.prepare(config_file(tmp_path, estate=map_setting))
        map_source = "estate.timezone_map"
    else:
        (tmp_path / "estate.toml").write_text(
            'name = "synthetic"\nfiles = ["a.jil"]\n' + map_setting
        )
        estate = reload_plan.load_estate(tmp_path)
        map_source = "timezone_map"
    items = [item for item in reload_plan.preflight_items(estate, T0) if item.code == "timezone"]
    assert len(items) == 1
    assert f"{message} {map_source}" in items[0].message
    if supplied_map:
        assert items[0].message == (
            f"timezone 'Zurich' is not resolvable: not in the supplied {map_source};"
            " without a map it would be Europe/Zurich; add the line: Zurich Europe/Zurich"
        )
    assert "--timezone-map" not in items[0].message
    if zone == "Indianapolis":
        assert items[0].severity == "WARN"
        assert "assumed to be America/Indiana/Indianapolis" in items[0].message
        assert "unique zoneinfo city match, DL-62" in items[0].message
        result = reread(
            tmp_path,
            Live(estate.catalog, estate.settings, 1, {}, launch=estate.metadata.get("launch", {})),
            tmp_path / "plans",
            candidate=estate if source == "config" else None,
            start=T0,
        )
        document = read_plan(tmp_path / "plans", result.token)
        assert document is not None
        warnings = [item for item in document["preflight_warnings"] if item["code"] == "timezone"]
        assert warnings == [{"code": "timezone", "job": "A", "message": items[0].message}]


@pytest.mark.parametrize("source", ["config", "manifest"])
@pytest.mark.parametrize("empty_map", [False, True])
@pytest.mark.usefixtures("fixed_zone_names")
def test_empty_timezone_map_keeps_the_estate_city_default(
    tmp_path: Path, source: str, empty_map: bool
) -> None:
    """DL-163: an empty map and no map accept the same estate zone through either loader."""
    from dsl41 import reload_plan

    (tmp_path / "a.jil").write_text(JOB + 'date_conditions: 1\nstart_times: "08:00"\n')
    (tmp_path / "zones").write_text("")
    setting = 'timezone = "Indianapolis"\n'
    if empty_map:
        setting += 'timezone_map = "zones"\n'
    if source == "config":
        estate = main.prepare(config_file(tmp_path, estate=setting))
    else:
        (tmp_path / "estate.toml").write_text('name = "synthetic"\nfiles = ["a.jil"]\n' + setting)
        estate = reload_plan.load_estate(tmp_path)
    assert estate.settings["timezone"] == "Indianapolis"
    assert estate.settings["timezone_aliases"] is None
    assert not [
        item for item in reload_plan.preflight_items(estate, T0) if item.severity == "ERROR"
    ]
    result = reread(
        tmp_path,
        Live(estate.catalog, estate.settings, 1, {}, launch=estate.metadata.get("launch", {})),
        tmp_path / "plans",
        candidate=estate if source == "config" else None,
        start=T0,
    )
    assert result.plan.unchanged == ("A",)


@pytest.mark.parametrize("damage", ["relative", "path", "config", "membership"])
def test_source_descriptor_requires_bound_launch_input(tmp_path: Path, damage):
    from dsl41.canon import canonical_bytes

    candidate = main.prepare(estate_file(tmp_path))
    parts = list(candidate.parts)
    metadata = json.loads(parts[-1][1])
    if damage == "relative":
        metadata["config_path"] = "relative.toml"
    elif damage == "path":
        metadata["records"][0]["path"] = str(tmp_path / "another.toml")
    elif damage == "config":
        metadata["config"]["lint"]["refuse"] = "error"
    else:
        parts[:-1] = reversed(parts[:-1])
    parts[-1] = (parts[-1][0], canonical_bytes(metadata))
    with pytest.raises(RereadRefused):
        main.verified_from_parts(parts)
