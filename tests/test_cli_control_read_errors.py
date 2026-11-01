"""CLI reads report transport failures as exit 2 (DL-78)."""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import runner_control
from dsl41.cli import app

pytestmark = pytest.mark.usefixtures("cli_env")

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def test_query_status_reports_a_missing_control_socket(short_root: Path) -> None:
    socket_path = short_root / "control.sock"
    assert not socket_path.exists()

    result = runner.invoke(app, ["query", "status", "--socket", str(socket_path)])

    assert result.exit_code == 2
    assert ANSI.sub("", result.stderr) == (
        f"control socket {socket_path}: [Errno {errno.ENOENT}] {os.strerror(errno.ENOENT)}\n"
    )
    assert ANSI.sub("", result.stdout) == ""
    assert "Traceback" not in ANSI.sub("", result.output)
    assert isinstance(result.exception, SystemExit)


def test_query_status_reports_an_injected_control_client_error(
    monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    socket_path = short_root / "control.sock"
    diagnostic = f"control socket {socket_path}: unreadable status response"
    requests: list[tuple[Path, dict[str, Any]]] = []

    def roundtrip(path: Path, request: dict[str, Any]) -> dict[str, Any]:
        requests.append((path, request))
        raise runner_control.ControlClientError(diagnostic, delivered=True)

    monkeypatch.setattr(runner_control, "roundtrip", roundtrip)

    result = runner.invoke(app, ["query", "status", "--socket", str(socket_path)])

    assert requests == [(socket_path, {"cmd": "status"})]
    assert result.exit_code == 2
    assert ANSI.sub("", result.stderr) == f"{diagnostic}\n"
    assert ANSI.sub("", result.stdout) == ""
    assert "Traceback" not in ANSI.sub("", result.output)
    assert isinstance(result.exception, SystemExit)
