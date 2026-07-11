"""`dsl41 serve` CLI tests (phase 11e).

Normative spec: docs/runner-design.md ss11 (UI: textual-serve wraps the same
app, one app subprocess per browser session) and ss14 (11e scope: serve +
deployment notes, not built) and ss13 item 6 ("TUI: textual pilot snapshot
smoke only" -- serve is thinner still, a CLI wrapper, so these are plain CLI
tests, not pilot tests). House style follows test_runner_control.py's
CLI section: `typer.testing.CliRunner` against `dsl41.cli.app`.

None of these tests actually starts textual-serve's web server (which blocks
running its own event loop until interrupted) -- the constructed Server is
always monkeypatched, per CLAUDE.md's "no runtime dependency in any emitted
artifact" discipline applied to the test suite too: these are unit tests of
the CLI wrapper, not an integration test of textual-serve itself (that was
verified manually for the phase-11e report, see docs/decision-log.md DL-47).
"""

from __future__ import annotations

import shlex
import sys

from typer.testing import CliRunner

from dsl41.cli import app

cli_runner = CliRunner()


def test_serve_missing_socket_exits_2(tmp_path, monkeypatch) -> None:
    # bypass the real textual-serve import so this test's outcome does not
    # depend on whether the [ui] extra happens to be installed (ss14)
    monkeypatch.setattr("dsl41.cli._import_textual_serve_or_exit_2", lambda: object)
    result = cli_runner.invoke(app, ["serve", "--socket", str(tmp_path / "nope.sock")])
    assert result.exit_code == 2
    assert "no such file" in result.output


def test_serve_missing_extra_exits_2_with_pip_hint(tmp_path, monkeypatch) -> None:
    """Guarded import (ss11/ss14): textual-serve is the [ui] extra's other
    half, alongside textual -- a missing install must not traceback."""
    monkeypatch.setitem(sys.modules, "textual_serve", None)
    monkeypatch.setitem(sys.modules, "textual_serve.server", None)
    result = cli_runner.invoke(app, ["serve", "--socket", str(tmp_path / "nope.sock")])
    assert result.exit_code == 2
    assert "pip install 'dsl41[ui]'" in result.output


def test_serve_constructs_the_ui_subprocess_command_quoting_a_space(tmp_path, monkeypatch) -> None:
    """The command textual-serve spawns per session must be exactly
    `<sys.executable> -m dsl41 ui --socket <path>`, properly quoted -- a
    socket path with a space is the fidelity probe."""
    sock = tmp_path / "run root with space" / "control.sock"
    sock.parent.mkdir()
    sock.touch()
    captured: dict = {}

    class FakeServer:
        def __init__(self, command, host="localhost", port=8000, **kwargs):
            captured["command"] = command
            captured["host"] = host
            captured["port"] = port

        def serve(self, debug=False):
            captured["served"] = True

    monkeypatch.setattr("dsl41.cli._import_textual_serve_or_exit_2", lambda: FakeServer)
    result = cli_runner.invoke(
        app, ["serve", "--socket", str(sock), "--host", "0.0.0.0", "--port", "9001"]
    )
    assert result.exit_code == 0, result.output
    assert captured["served"] is True
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9001
    tokens = shlex.split(captured["command"])
    assert tokens == [sys.executable, "-m", "dsl41", "ui", "--socket", str(sock)]


def test_serve_default_host_is_loopback(tmp_path, monkeypatch) -> None:
    """E3 posture: textual-serve ships no auth, so loopback is the default
    bind, not 0.0.0.0 -- a proxy/tunnel is the documented path outward
    (README deployment notes)."""
    sock = tmp_path / "control.sock"
    sock.touch()
    captured: dict = {}

    class FakeServer:
        def __init__(self, command, host="localhost", port=8000, **kwargs):
            captured["host"] = host

        def serve(self, debug=False):
            pass

    monkeypatch.setattr("dsl41.cli._import_textual_serve_or_exit_2", lambda: FakeServer)
    result = cli_runner.invoke(app, ["serve", "--socket", str(sock)])
    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"


def test_serve_bind_failure_exits_2(tmp_path, monkeypatch) -> None:
    """A bind failure (port in use, etc.) is "never started", same exit
    class as a missing socket or a missing extra (cli.py's exit-code
    contract comment)."""
    sock = tmp_path / "control.sock"
    sock.touch()

    class FakeServer:
        def __init__(self, command, host="localhost", port=8000, **kwargs):
            pass

        def serve(self, debug=False):
            raise OSError("address already in use")

    monkeypatch.setattr("dsl41.cli._import_textual_serve_or_exit_2", lambda: FakeServer)
    result = cli_runner.invoke(app, ["serve", "--socket", str(sock)])
    assert result.exit_code == 2
    assert "address already in use" in result.output
