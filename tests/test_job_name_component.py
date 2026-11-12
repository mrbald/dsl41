"""DL-494: job names reach lowering before lint, launch and reread."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_main_config import JOB, estate_file
from test_main_config_restart import cli_engine
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import LoweringError, lower_source
from dsl41.runner_control import roundtrip

pytestmark = pytest.mark.usefixtures("cli_env")
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REFUSED = [
    (name, job_type)
    for job_type in ("c", "b", "f")
    for name in ("a/b", ".", "..", "a\0b", "\x00pseudo")
]
ACCEPTED = ["a.b", "a-b", "a_b", ".a", "a..b"]


def _source(name: str, job_type: str = "c") -> str:
    execution = {
        "c": "command: true\nmachine: localhost\n",
        "b": "",
        "f": "watch_file: /dev/null\nmachine: localhost\n",
    }[job_type]
    return f"insert_job: {name}\njob_type: {job_type}\n{execution}"


@pytest.mark.parametrize(("name", "job_type"), REFUSED)
def test_lowering_refuses_non_component_job_names(name: str, job_type: str) -> None:
    with pytest.raises(LoweringError) as caught:
        lower_source("\n" + _source(name, job_type), file="jobs.jil")
    (finding,) = caught.value.findings
    assert finding.message == f"job name {name!r} must be one directory component (DL-494)"
    assert finding.span is not None
    assert finding.span.file == "jobs.jil"
    assert finding.span.line_start == 2
    assert "jobs.jil:2:" in str(caught.value)


@pytest.mark.parametrize("name", ACCEPTED)
def test_lowering_accepts_component_job_names(name: str) -> None:
    assert list(lower_source(_source(name)).jobs) == [name]


@pytest.mark.parametrize("extra", ["", "bogus: 1\n"])
def test_invalid_box_keeps_its_other_findings_without_a_missing_box_error(extra: str) -> None:
    source = _source("a/b", "b") + extra + "\n" + _source("child") + "box_name: a/b\n"
    with pytest.raises(LoweringError) as caught:
        lower_source(source, file="jobs.jil")
    messages = [finding.message for finding in caught.value.findings]
    assert messages[0] == "job name 'a/b' must be one directory component (DL-494)"
    assert len(messages) == (2 if extra else 1)
    if extra:
        assert "unknown attribute 'bogus'" in messages[1]
    assert not any("is not defined" in message for message in messages)


@pytest.mark.parametrize("output_format", ["lint", "report", "explore"])
def test_cli_gets_no_partial_catalog_for_an_invalid_box(tmp_path: Path, output_format: str) -> None:
    source = tmp_path / "jobs.jil"
    source.write_text(_source("a/b", "b") + "bogus: 1\n\n" + _source("child") + "box_name: a/b\n")
    target = tmp_path / "graph.html"
    arguments = (
        ["lint", str(source)]
        if output_format == "lint"
        else ["viz", str(source), "--format", output_format, "-o", str(target)]
    )
    result = runner.invoke(app, arguments)
    output = ANSI.sub("", result.output)
    assert result.exit_code == 2, output
    assert "job name 'a/b' must be one directory component" in output
    assert "unknown attribute 'bogus'" in output
    assert "is not defined" not in output
    assert not target.exists()


@pytest.mark.parametrize(("name", "job_type"), REFUSED)
@pytest.mark.parametrize("command", ["lint", "run"])
def test_cli_refuses_non_component_job_names(
    short_root: Path, name: str, job_type: str, command: str
) -> None:
    source = short_root / "jobs.jil"
    source.write_text(_source(name, job_type))
    arguments = [command, str(source)]
    root = short_root / "run"
    if command == "run":
        arguments += ["--run-root", str(root)]
        result = subprocess.run(
            [sys.executable, "-m", "dsl41", *arguments],
            capture_output=True,
            text=True,
            timeout=5,
        )
        code, output = result.returncode, result.stdout + result.stderr
    else:
        invoked = runner.invoke(app, arguments)
        code, output = invoked.exit_code, invoked.output
    assert code == 2, output
    output = ANSI.sub("", output)
    assert f"job name {name!r} must be one directory component" in output
    assert f"{source}:1:" in output
    assert not (root / "engine.db").exists()


@pytest.mark.parametrize("name", ACCEPTED)
def test_lint_accepts_component_job_names(tmp_path: Path, name: str) -> None:
    source = tmp_path / "jobs.jil"
    source.write_text(_source(name))
    result = runner.invoke(app, ["lint", str(source)])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("name", ACCEPTED)
def test_run_accepts_and_executes_component_job_names(short_root: Path, name: str) -> None:
    config = estate_file(short_root)
    (short_root / "a.jil").write_text(_source(name))
    root = short_root / "run"
    with cli_engine(config) as ask:
        status = ask("status")
        reply = roundtrip(
            root / "control.sock",
            {
                "cmd": "sendevent",
                "verb": "FORCE_STARTJOB",
                "payload": {"job": name},
                "baseline_id": status["baseline_id"],
                "request_id": "start",
                "expect": {f"job:{name}": status["jobs"][name]["state_rev"]},
            },
        )
        assert reply["ok"], reply
        deadline = time.monotonic() + 10
        row = ask("status")["jobs"][name]
        while row["status"] not in {"SUCCESS", "FAILURE", "TERMINATED"}:
            assert time.monotonic() < deadline, row
            time.sleep(0.01)
            row = ask("status")["jobs"][name]
        assert row["status"] == "SUCCESS", (row, (short_root / "cli-engine.log").read_text())
        assert (root / "runs" / f"{name}.1" / "1" / "status.json").is_file()


@pytest.mark.parametrize("name", ["a/b", ".", "..", "a\0b"])
def test_reread_refuses_non_component_job_without_changing_engine(
    short_root: Path, name: str
) -> None:
    config = estate_file(short_root)
    with cli_engine(config) as ask:
        before = ask()
        jobs = ask("status")["jobs"]
        (short_root / "a.jil").write_text(JOB + "\n" + _source(name))
        result = runner.invoke(app, ["reread", "-S", str(short_root / "run/control.sock")])
        assert result.exit_code == 2, result.output
        assert f"job name {name!r} must be one directory component" in ANSI.sub("", result.output)
        after = ask()
        assert after["revision"] == before["revision"]
        assert after["catalog"] == before["catalog"]
        status = ask("status")
        assert status["engine"] == "RUNNING"
        assert status["jobs"] == jobs
