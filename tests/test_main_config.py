"""Main-config acceptance 1–11: resolution, capture, policy, plans and restart (DL-437)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import main_config as main
from dsl41.cli import app
from dsl41.cli_reload import reread_command
from dsl41.ir import CatalogIR
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
    assert (
        reread_command(
            tmp_path,
            run_root=tmp_path / "run",
            send=lambda *_: Live(CatalogIR(), {}, 1, {}),
            config=path,
        )
        == 2
    )
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
    root = tmp_path / "run"
    assert (
        reread_command(
            tmp_path, run_root=root, send=lambda *_: live_of(active, {"A": "RUNNING"}), config=path
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "source moved A:" in output and "a.jil" in output and "moved.jil" in output
    (tmp_path / "moved.jil").write_text(JOB.replace("A", "B"))
    assert (
        reread_command(tmp_path, run_root=root, send=lambda *_: live_of(active), config=path) == 0
    )
    output = capsys.readouterr().out
    assert "source added B:" in output and "source removed A:" in output
    config_file(tmp_path, estate='timezone = "Europe/Zurich"\n')
    assert (
        reread_command(tmp_path, run_root=root, send=lambda *_: live_of(active), config=path) == 0
    )
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

    def changed(selected: Path):
        selected.write_text(selected.read_text().replace('/run"', '/other"'))
        return prepare(selected)

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


def boot_and_stop(path: Path) -> str:
    root = Path(main.load_config(path).engine.run_root)
    with subprocess.Popen(
        [sys.executable, "-m", "dsl41", "run", "--config", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as child:
        try:
            deadline = time.monotonic() + 30
            while (
                not (root / "control.sock").exists()
                and child.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if child.poll() is None:
                child.terminate()
            output, _ = child.communicate(timeout=30)
            assert child.returncode == 0, output
            return output
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


def test_10_restart_uses_stored_catalog_and_reports_pending(short_root: Path) -> None:
    tmp_path = short_root
    path = estate_file(tmp_path)
    assert "engine" in boot_and_stop(path)
    before = active_configuration(tmp_path / "run")
    (tmp_path / "a.jil").write_text("malformed: deliberately\n")
    config_file(tmp_path, lint='refuse = "warn"\n')
    output = boot_and_stop(path)
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
