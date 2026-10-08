"""Branch tests for src/dsl41/cli_control.py: the refusals of the control verbs
that never reach an engine, and the failures of the clients that do.

The verbs run in-process through the typer test runner. The engine's answer is
a canned dict served in place of `_control_roundtrip` (the seam between the
verbs and the socket), and every test that refuses asserts that nothing was
sent past the refusal. The socket protocol itself is tested elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import dsl41.cli_control as cli_control
from dsl41.cli import app

runner = CliRunner()

_HEADER = {"baseline_id": "b-1", "epoch": 3}


class _Engine:
    """A stand-in for the control socket: records the requests, answers from a table."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.requests: list[dict[str, Any]] = []

    def __call__(self, socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return self.answers[request["cmd"]]


def _engine(monkeypatch: pytest.MonkeyPatch, **answers: dict[str, Any]) -> _Engine:
    engine = _Engine(answers)
    monkeypatch.setattr(cli_control, "_control_roundtrip", engine)
    return engine


def _run(*args: str, socket: Path | None = None) -> Any:
    return runner.invoke(app, [*args, "--socket", str(socket or Path("/nowhere/control.sock"))])


# ------------------------------------------------------------------ sendevent


def test_sendevent_stops_when_the_engine_answers_no_read_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, status={"ok": True, "error": "no header", "jobs": {}})
    result = _run("sendevent", "STARTJOB", "--job", "j")
    assert result.exit_code == 2
    assert "no header" in result.output
    assert len(engine.requests) == 1  # the read; no command followed it


def test_sendevent_refuses_a_global_that_is_not_name_equals_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    for bad in ("novalue", "=value"):
        result = _run("sendevent", "SET_GLOBAL", "--global", bad)
        assert result.exit_code == 2
        assert 'expects "NAME=value"' in result.output
    assert engine.requests == []


def test_sendevent_refuses_an_event_that_addresses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    result = _run("sendevent", "STARTJOB")
    assert result.exit_code == 2
    assert "STARTJOB addresses a job by name" in result.output
    assert engine.requests == []


# --------------------------------------------------------------- release-held


def test_release_held_stops_when_the_status_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, status={"ok": False, "error": "no status for you"})
    result = _run("release-held")
    assert result.exit_code == 2
    assert "no status for you" in result.output
    assert len(engine.requests) == 1


def test_release_held_stops_when_the_status_carries_no_read_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, status={"ok": True, "jobs": {"j": {"on_hold": True}}})
    result = _run("release-held")
    assert result.exit_code == 2
    assert "the engine answered no read header" in result.output
    assert len(engine.requests) == 1


def test_release_held_with_a_header_and_nothing_held_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart of the two refusals above: a complete answer proceeds."""
    _engine(monkeypatch, status={"ok": True, "jobs": {"j": {"on_hold": False}}, **_HEADER})
    result = _run("release-held")
    assert result.exit_code == 0
    assert "no jobs held" in result.output


# ------------------------------------------------------------------------ host


def test_host_list_prints_the_engines_answer_and_exits_on_its_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, hosts={"ok": True, "hosts": {}})
    result = _run("host", "list")
    assert result.exit_code == 0
    assert result.output.strip() == '{"hosts": {}, "ok": true}'
    assert engine.requests == [{"cmd": "hosts"}]


def test_host_list_exits_two_on_a_refused_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _engine(monkeypatch, hosts={"ok": False, "error": "no hosts for you"})
    result = _run("host", "list")
    assert result.exit_code == 2
    assert "no hosts for you" in result.output


@pytest.mark.parametrize("action", ["drain", "activate", "evict"])
def test_a_host_action_without_a_host_id_is_refused(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    engine = _engine(monkeypatch)
    result = _run("host", action)
    assert result.exit_code == 2
    assert f"`host {action}` needs a host id" in result.output
    assert engine.requests == []


# ----------------------------------------------------------------------- query


def test_a_status_predicate_stops_when_the_status_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, status={"ok": False, "error": "no such job"})
    result = _run("query", "is-success", "--job", "j")
    assert result.exit_code == 2
    assert "no such job" in result.output


def test_a_status_predicate_answers_by_exit_code_when_the_read_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, status={"ok": True, "jobs": {"j": {"status": "SUCCESS"}}})
    matched = _run("query", "is-success", "--job", "j")
    assert (matched.exit_code, matched.output.strip()) == (0, "SUCCESS")
    unmatched = _run("query", "is-failed", "--job", "j")
    assert (unmatched.exit_code, unmatched.output.strip()) == (1, "SUCCESS")


def test_brief_belongs_to_status_only(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch)
    result = _run("query", "trace", "--brief")
    assert result.exit_code == 2
    assert "--brief applies to status only" in result.output
    assert engine.requests == []


@pytest.mark.parametrize("verb", ["global", "globals"])
def test_a_global_query_needs_a_name(monkeypatch: pytest.MonkeyPatch, verb: str) -> None:
    engine = _engine(monkeypatch)
    result = _run("query", verb)
    assert result.exit_code == 2
    assert f"{verb} requires --name" in result.output
    assert engine.requests == []


def test_global_names_one_and_globals_names_several(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(monkeypatch, **{"global": {"ok": True}, "globals": {"ok": True}})
    refused = _run("query", "global", "--name", "A", "--name", "B")
    assert refused.exit_code == 2
    assert "global names one; use `globals` for several" in refused.output
    assert engine.requests == []
    assert _run("query", "global", "--name", "A").exit_code == 0
    assert _run("query", "globals", "--name", "A", "--name", "B").exit_code == 0
    assert engine.requests == [
        {"cmd": "global", "name": "A"},
        {"cmd": "globals", "names": ["A", "B"]},
    ]


def test_brief_status_warns_on_stderr_when_the_estate_files_drifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {"j": {"status": "SUCCESS", "status_at": "2026-01-01T00:00:00", "state_rev": 4}}
    _engine(monkeypatch, status={"ok": True, "jobs": rows, "spec_drift": True})
    drifted = _run("query", "status", "--brief")
    assert drifted.exit_code == 0
    assert "SPEC DRIFT: estate files changed on disk" in drifted.output
    assert "SUCCESS" in drifted.output
    _engine(monkeypatch, status={"ok": True, "jobs": rows})
    quiet = _run("query", "status", "--brief")
    assert quiet.exit_code == 0
    assert "SPEC DRIFT" not in quiet.output


# ------------------------------------------------------------------- subscribe


def _subscribe_with(monkeypatch: pytest.MonkeyPatch, lines: Any) -> Any:
    import dsl41.runner_control as control

    monkeypatch.setattr(control, "subscribe_lines", lambda socket_path, request: lines())
    return _run("query", "subscribe")


def test_subscribe_names_the_socket_when_the_transport_fails_before_the_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lines() -> Any:
        raise OSError("connection refused")
        yield  # makes this a generator

    result = _subscribe_with(monkeypatch, lines)
    assert result.exit_code == 2
    assert "control socket /nowhere/control.sock: connection refused" in result.output
    assert "resubscribe" not in result.output


def test_subscribe_names_the_cursor_when_the_transport_fails_mid_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lines() -> Any:
        yield '{"ok": true, "cursor": 7}'
        yield '{"seq": 8, "rec": "x"}'
        raise OSError("broken pipe")

    result = _subscribe_with(monkeypatch, lines)
    assert result.exit_code == 2
    assert '"seq": 8' in result.output
    assert "broken pipe" in result.output
    assert "resubscribe with --since 8" in result.output


def test_subscribe_ends_quietly_at_the_operators_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lines() -> Any:
        yield '{"ok": true}'
        raise KeyboardInterrupt

    result = _subscribe_with(monkeypatch, lines)
    assert result.exit_code == 0
    assert result.output.strip() == '{"ok": true}'


# -------------------------------------------------------------------- ui, serve


def test_ui_refuses_a_control_socket_that_does_not_exist(tmp_path: Path) -> None:
    result = _run("ui", socket=tmp_path / "control.sock")
    assert result.exit_code == 2
    assert "no such file" in result.output


def test_serve_gets_the_server_class_from_the_textual_serve_extra() -> None:
    from textual_serve.server import Server

    assert cli_control._import_textual_serve_or_exit_2() is Server


# -------------------------------------------------------------------- supervise


def test_supervise_start_refuses_a_run_root_that_is_a_file(tmp_path: Path) -> None:
    occupied = tmp_path / "run"
    occupied.write_text("not a directory")
    result = runner.invoke(app, ["supervise", "start", "--run-root", str(occupied)])
    assert result.exit_code == 2
    assert "supervisor start:" in result.output
    assert "File exists" in result.output  # the mkdir's own refusal, not a later one
    assert occupied.read_text() == "not a directory"


def test_supervise_refuses_a_supervisor_socket_it_cannot_connect_to(short_root: Path) -> None:
    """A path short enough to be connected to: the file is not a listening socket."""
    (short_root / "supervisor.sock").write_text("a file, not a socket")
    result = runner.invoke(app, ["supervise", "list", "--run-root", str(short_root)])
    assert result.exit_code == 2
    assert f"supervisor {short_root / 'supervisor.sock'}:" in result.output
    assert "AF_UNIX path too long" not in result.output


class _Conn:
    """A supervisor connection that answers from a script and notes that it was closed."""

    closed = False

    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.sent: list[dict[str, Any]] = []

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(request)
        answer = self.script.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


def _supervised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list[Any]
) -> tuple[Any, _Conn]:
    import dsl41.runner_adapters as adapters

    (tmp_path / "supervisor.sock").write_text("")  # only its existence is read
    conn = _Conn(script)
    monkeypatch.setattr(adapters, "SupervisorConn", lambda path: conn)
    result = runner.invoke(app, ["supervise", "shutdown", "--run-root", str(tmp_path)])
    return result, conn


def test_supervise_shutdown_stops_when_the_lease_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, conn = _supervised(monkeypatch, tmp_path, [{"ok": False, "error": "held"}])
    assert result.exit_code == 2
    assert 'cannot acquire lease: {"error": "held", "ok": false}' in result.output
    assert [r["cmd"] for r in conn.sent] == ["ACQUIRE"]
    assert conn.closed


def test_supervise_shutdown_reports_a_supervisor_that_drops_the_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, conn = _supervised(monkeypatch, tmp_path, [OSError("connection reset")])
    assert result.exit_code == 2
    assert "connection reset" in result.output
    assert conn.closed


def test_supervise_shutdown_takes_the_lease_then_asks_for_the_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result, conn = _supervised(
        monkeypatch, tmp_path, [{"ok": True, "token": "t-1"}, {"ok": True, "stopped": 0}]
    )
    assert result.exit_code == 0
    assert [r["cmd"] for r in conn.sent] == ["ACQUIRE", "SHUTDOWN"]
    assert conn.sent[1]["token"] == "t-1"
    assert conn.closed
