"""`dsl41 serve` and `dsl41 ui` CLI tests (phase 11e).

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
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app

cli_runner = CliRunner()


def test_serve_missing_socket_exits_2(tmp_path, monkeypatch) -> None:
    # bypass the real textual-serve import so this test's outcome does not
    # depend on whether the [ui] extra happens to be installed (ss14)
    monkeypatch.setattr("dsl41.cli_control._import_textual_serve_or_exit_2", lambda: object)
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

    monkeypatch.setattr("dsl41.cli_control._import_textual_serve_or_exit_2", lambda: FakeServer)
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

    monkeypatch.setattr("dsl41.cli_control._import_textual_serve_or_exit_2", lambda: FakeServer)
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

    monkeypatch.setattr("dsl41.cli_control._import_textual_serve_or_exit_2", lambda: FakeServer)
    result = cli_runner.invoke(app, ["serve", "--socket", str(sock)])
    assert result.exit_code == 2
    assert "address already in use" in result.output


# ---------------------------------------------------------------- `ui` front door
#
# F3 of the 2026-09-08 TUI UX review: textual's fatal-error path sets
# `app.return_code = 1` and returns normally from `run()`/`run_async()`
# (textual/app.py's `_handle_exception` and its `return_code` property) --
# it never raises. A front door that only checks for a raised exception
# treats a crashed TUI as a clean exit.


class _FakeTuiModule:
    """Stand-in for the module `import_tui_or_exit_2` returns: its
    `RunnerApp.run()` sets `return_code` the way textual's real fatal-error
    path does, with no need for textual to be installed. `captured`, when
    given, records the constructor's kwargs (P1-2): `dsl41 ui` passes no
    `owns_run`, and a test that never checks that is blind to a dropped or
    mistyped `owns_run=True` at the OTHER front door leaving the whole
    suite green (review MAJOR 2)."""

    def __init__(self, final_return_code: int | None, captured: dict | None = None) -> None:
        final = final_return_code
        sink = captured if captured is not None else {}

        class RunnerApp:
            def __init__(self, socket_path, **kw):
                self.return_code = None
                sink["kwargs"] = kw

            def run(self):
                self.return_code = final

        self.RunnerApp = RunnerApp


def test_ui_exits_with_the_tui_return_code_on_a_fatal_error(tmp_path, monkeypatch) -> None:
    sock = tmp_path / "control.sock"
    sock.touch()
    captured: dict = {}
    monkeypatch.setattr(
        "dsl41.cli_control.import_tui_or_exit_2", lambda: _FakeTuiModule(1, captured)
    )
    result = cli_runner.invoke(app, ["ui", "--socket", str(sock)])
    assert result.exit_code == 1
    assert captured["kwargs"].get("owns_run") in (None, False)


def test_ui_exits_0_when_the_tui_return_code_is_none(tmp_path, monkeypatch) -> None:
    """The companion case: a TUI that quit cleanly (`return_code` stays
    `None`) is unaffected by the new check."""
    sock = tmp_path / "control.sock"
    sock.touch()
    captured: dict = {}
    monkeypatch.setattr(
        "dsl41.cli_control.import_tui_or_exit_2", lambda: _FakeTuiModule(None, captured)
    )
    result = cli_runner.invoke(app, ["ui", "--socket", str(sock)])
    assert result.exit_code == 0
    assert captured["kwargs"].get("owns_run") in (None, False)


# ---------------------------------------------------------- `run --ui` front door
#
# `_serve_run` drives a real engine and control socket, so these need a real
# textual import (`run`'s own `import_tui_or_exit_2` guard, unpatched) and a
# short AF_UNIX socket path -- test_runner_tui.py's fixture and skip guard,
# duplicated here rather than imported (that file's own docstring explains
# why: test_runner.py duplicates test_oracle.py's small helpers the same way).

_MINIMAL_JIL = "insert_job: cc_job\njob_type: c\ncommand: x\n"


@pytest.fixture
def short_root():
    """A short-path base directory for AF_UNIX control sockets (see
    test_runner_tui.py's fixture of the same name/docstring). The platform
    guard (NIT 12) matches test_runner_tui.py's module-level one
    (line 73): `mkdtemp(dir="/tmp")` is POSIX-specific, and running it
    before any check errors on a non-POSIX host instead of skipping --
    `_skip_unless_posix_textual` below only runs INSIDE a test, after this
    fixture has already set up."""
    if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
        pytest.skip("unix-domain control sockets are POSIX-only")
    d = tempfile.mkdtemp(prefix="dsl41srv-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _fake_runner_app(final_return_code: int | None, captured: dict | None = None):
    """A `runner_tui.RunnerApp` stand-in whose `run_async` returns normally
    with `return_code` set -- the textual fatal-error shape `_serve_run`
    must treat as a crash, not a quit (F3). `captured`, when given, records
    the constructor's kwargs (P1-2): `run --ui` passes `owns_run=True`, and
    nothing asserted that until now (review MAJOR 2) -- deleting or
    mistyping it would leave the whole suite green while `run --ui`
    silently reverted to quit-without-confirm."""

    class FakeRunnerApp:
        def __init__(self, socket_path, **kw):
            self.return_code = None
            if captured is not None:
                captured["kwargs"] = kw

        async def run_async(self):
            self.return_code = final_return_code

        def exit(self):
            pass

    return FakeRunnerApp


def _skip_unless_posix_textual() -> None:
    pytest.importorskip("textual")
    if not sys.platform.startswith(("linux", "darwin")):
        pytest.skip("unix-domain control sockets are POSIX-only")


def test_run_ui_tui_failure_is_not_an_operator_stop(short_root, monkeypatch) -> None:
    """F3 end to end: a TUI that fails without raising (return_code set,
    run_async returns normally) must fail the run, not stop it cleanly."""
    _skip_unless_posix_textual()
    jil_path = short_root / "estate.jil"
    jil_path.write_text(_MINIMAL_JIL)
    run_root = short_root / "run"
    captured: dict = {}
    monkeypatch.setattr("dsl41.runner_tui.RunnerApp", _fake_runner_app(1, captured))
    result = cli_runner.invoke(app, ["run", str(jil_path), "--run-root", str(run_root), "--ui"])
    assert result.exit_code == 1
    assert "TUI failed: exit code 1" in result.output
    assert captured["kwargs"].get("owns_run") is True


def test_run_ui_operator_stop_when_the_tui_return_code_is_none(short_root, monkeypatch) -> None:
    """The companion case: `return_code` stays `None`, the existing
    operator-stop path (a clean quit), unaffected by F3's new check."""
    _skip_unless_posix_textual()
    jil_path = short_root / "estate.jil"
    jil_path.write_text(_MINIMAL_JIL)
    run_root = short_root / "run"
    captured: dict = {}
    monkeypatch.setattr("dsl41.runner_tui.RunnerApp", _fake_runner_app(None, captured))
    result = cli_runner.invoke(app, ["run", str(jil_path), "--run-root", str(run_root), "--ui"])
    assert result.exit_code == 0
    assert "stopping:" in result.output
    assert captured["kwargs"].get("owns_run") is True
