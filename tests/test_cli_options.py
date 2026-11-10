"""Root options and ordinary CLI dispatch."""

from pathlib import Path
import re
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import tool_version

pytestmark = pytest.mark.usefixtures("cli_env")
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def test_import_does_not_read_package_metadata() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from unittest.mock import patch\n"
            "with patch('importlib.metadata.version', side_effect=AssertionError('metadata read')):\n"
            "    import dsl41.cli\n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("arguments", [["--version"], ["--version", "lint"]])
def test_version_prints_installed_metadata_and_exits(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert ANSI.sub("", result.stdout) == f"dsl41 {tool_version()}\n"
    assert not result.stderr


@pytest.mark.parametrize("arguments", [["--help"], ["-h"], []])
def test_root_help_preserves_exit_status(arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == (0 if arguments else 2)
    output = ANSI.sub("", result.output)
    assert "Migration compiler and runner" in output
    assert "--version" in output


@pytest.mark.parametrize("command", ["lint", "run", "engine"])
def test_subcommand_help_keeps_root_version_option_out(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    output = ANSI.sub("", result.output)
    assert "Usage:" in output
    assert "--version" not in output


def test_ordinary_dispatch_and_usage_refusals() -> None:
    corpus = Path(__file__).parent / "corpus" / "sem10_box_basic.jil"
    clean = runner.invoke(app, ["lint", str(corpus)])
    assert clean.exit_code == 0, clean.output
    for arguments in (["no-such-command"], ["lint"], ["lint", "--version"]):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2, result.output
        assert "dsl41 " + tool_version() not in ANSI.sub("", result.output)
