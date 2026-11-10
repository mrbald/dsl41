"""Branch tests for src/dsl41/cli_control.py: the refusals of the control verbs
that never reach an engine, and the failures of the clients that do.

The verbs run in-process through the typer test runner. The engine's answer is
a canned dict served in place of `_control_roundtrip` (the seam between the
verbs and the socket), and every test that refuses asserts that nothing was
sent past the refusal. The socket protocol itself is tested elsewhere.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import dsl41.cli_control as cli_control
from dsl41.cli import app

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

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


@pytest.fixture(
    params=[
        ("query", "status"),
        ("reread",),
        ("update",),
        ("sendevent", "ON_HOLD", "--job", "socket_job"),
        ("off-hold-all", "--dry-run"),
        ("engine", "stop"),
        ("ui",),
        ("serve",),
    ]
)
def socket_command(request: pytest.FixtureRequest) -> tuple[str, ...]:
    return request.param


@pytest.fixture
def socket_calls(monkeypatch: pytest.MonkeyPatch, cli_env: None) -> list[Path]:
    paths: list[Path] = []

    def reached(socket_path: Path, *args: object, **kwargs: object) -> None:
        paths.append(socket_path)
        raise typer.Exit(0)

    monkeypatch.setattr(cli_control, "_control_roundtrip", reached)
    monkeypatch.setattr("dsl41.runner_control.roundtrip", reached)
    monkeypatch.setattr(cli_control, "check_control_socket", reached)
    monkeypatch.setattr(cli_control, "import_tui_or_exit_2", lambda: object())
    monkeypatch.setattr(cli_control, "_import_textual_serve_or_exit_2", lambda: object())
    return paths


def test_socket_option_uses_environment(socket_command, socket_calls, monkeypatch) -> None:
    monkeypatch.setenv("DSL41_SOCKET", "/environment/control.sock")
    result = runner.invoke(app, list(socket_command))
    assert result.exit_code == 0, result.output
    assert socket_calls == [Path("/environment/control.sock")]


def test_explicit_socket_overrides_environment(socket_command, socket_calls, monkeypatch) -> None:
    monkeypatch.setenv("DSL41_SOCKET", "/environment/control.sock")
    result = runner.invoke(app, [*socket_command, "--socket", "/explicit/control.sock"])
    assert result.exit_code == 0, result.output
    assert socket_calls == [Path("/explicit/control.sock")]


def test_socket_option_is_required_without_environment(socket_command, socket_calls) -> None:
    result = runner.invoke(app, list(socket_command))
    assert result.exit_code == 2
    output = ANSI.sub("", result.output)
    assert "Missing option" in output and "--socket" in output
    assert socket_calls == []


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


# --------------------------------------------------------------- off-hold-all


def test_off_hold_all_stops_when_the_status_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, status={"ok": False, "error": "no status for you"})
    result = _run("off-hold-all")
    assert result.exit_code == 2
    assert "no status for you" in result.output
    assert len(engine.requests) == 1


def test_off_hold_all_stops_when_the_status_carries_no_read_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, status={"ok": True, "jobs": {"j": {"on_hold": True}}})
    result = _run("off-hold-all")
    assert result.exit_code == 2
    assert "the engine answered no read header" in result.output
    assert len(engine.requests) == 1


def test_off_hold_all_with_a_header_and_nothing_held_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterpart of the two refusals above: a complete answer proceeds."""
    _engine(monkeypatch, status={"ok": True, "jobs": {"j": {"on_hold": False}}, **_HEADER})
    result = _run("off-hold-all")
    assert result.exit_code == 0
    assert "no jobs held" in result.output


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
    local_timezone,
) -> None:
    rows = {"j": {"status": "SUCCESS", "status_at": "2026-01-01T00:00:00", "state_rev": 4}}
    _engine(monkeypatch, status={"ok": True, "jobs": rows, "spec_drift": True})
    drifted = _run("query", "status", "--brief")
    assert drifted.exit_code == 0
    assert "estate files on disk differ from the running estate" in drifted.output
    assert "SUCCESS" in drifted.output
    _engine(monkeypatch, status={"ok": True, "jobs": rows})
    quiet = _run("query", "status", "--brief")
    assert quiet.exit_code == 0
    assert "estate files on disk differ" not in quiet.output


# ------------------------------------------------------------------- subscribe


@pytest.fixture
def display_status() -> dict[str, Any]:
    return {
        "ok": True,
        **_HEADER,
        "state_rev": 8,
        "config_rev": 2,
        "engine": "RUNNING",
        "spec_drift": False,
        "update_window_s": 30,
        "update": {"token": "update-1", "stopping": ["j"]},
        "jobs": {
            "j": {
                "status": "FAILURE",
                "status_at": "2026-07-01T23:30:00.123456",
                "run_number": 3,
                "exit_code": 1,
                "state_rev": 7,
                "on_ice": False,
                "on_hold": True,
                "on_noexec": False,
                "armed": False,
                "held": True,
                "started_by": "STARTJOB",
                "pending_timers": [{"due": "2026-01-01T23:30:00", "kind": "schedule"}],
                "interrupted": {
                    "started_at": "2026-10-25T00:30:00Z",
                    "ended_at": "2026-10-25T01:30:00+00:00",
                    "run_id": "j/3",
                    "cause": "engine_stop",
                    "signal": None,
                },
                "attempt": {"n": 2, "state": "STOPPING", "note": "stop in progress"},
                "watching": {"file": "/synthetic/input", "interval": 60, "min_size": 1},
                "log_out": "/synthetic/j.out",
                "log_err": None,
                "job_type": "CMD",
                "box_name": None,
                "reread": {"change": "changed", "action": "hold"},
            },
            "empty": {"status_at": None, "pending_timers": [], "extra": {}},
        },
    }


def test_single_job_text_keeps_fields_and_local_instants(
    monkeypatch, cli_env, local_timezone, display_status
) -> None:
    original = copy.deepcopy(display_status)
    engine = _engine(monkeypatch, status=display_status)
    result = _run("query", "status", "j")
    assert result.exit_code == 0, result.output
    fields = dict(
        line.split(": ", 1)
        for line in ANSI.sub("", result.stdout).split("\n\n", 1)[1].strip().splitlines()
    )
    expected_times = {
        "jobs.j.status_at": "2026-07-02T01:30:00",
        "jobs.j.pending_timers[0].due": "2026-01-02T00:30:00",
        "jobs.j.interrupted.started_at": "2026-10-25T02:30:00",
        "jobs.j.interrupted.ended_at": "2026-10-25T02:30:00",
        "jobs.empty.status_at": "-",
    }

    assert fields == {
        **expected_times,
        "ok": "true",
        "baseline_id": "b-1",
        "epoch": "3",
        "state_rev": "8",
        "config_rev": "2",
        "engine": "RUNNING",
        "spec_drift": "false",
        "update_window_s": "30",
        "update.token": "update-1",
        "update.stopping[0]": "j",
        "jobs.j.status": "FAILURE",
        "jobs.j.run_number": "3",
        "jobs.j.exit_code": "1",
        "jobs.j.state_rev": "7",
        "jobs.j.on_ice": "false",
        "jobs.j.on_hold": "true",
        "jobs.j.on_noexec": "false",
        "jobs.j.armed": "false",
        "jobs.j.held": "true",
        "jobs.j.started_by": "STARTJOB",
        "jobs.j.pending_timers[0].kind": "schedule",
        "jobs.j.interrupted.run_id": "j/3",
        "jobs.j.interrupted.cause": "engine_stop",
        "jobs.j.interrupted.signal": "null",
        "jobs.j.attempt.n": "2",
        "jobs.j.attempt.state": "STOPPING",
        "jobs.j.attempt.note": "stop in progress",
        "jobs.j.watching.file": "/synthetic/input",
        "jobs.j.watching.interval": "60",
        "jobs.j.watching.min_size": "1",
        "jobs.j.log_out": "/synthetic/j.out",
        "jobs.j.log_err": "null",
        "jobs.j.job_type": "CMD",
        "jobs.j.box_name": "null",
        "jobs.j.reread.change": "changed",
        "jobs.j.reread.action": "hold",
        "jobs.empty.pending_timers": "[]",
        "jobs.empty.extra": "{}",
    }
    assert engine.requests == [{"cmd": "status", "job": "j"}]
    assert display_status == original


def test_status_brief_uses_full_local_time(monkeypatch, cli_env, local_timezone) -> None:
    _engine(
        monkeypatch,
        status={
            "ok": True,
            "jobs": {"j": {"status": "SUCCESS", "status_at": "2026-07-01T23:30:00"}},
        },
    )
    result = _run("query", "status", "--brief")
    assert result.exit_code == 0
    assert "2026-07-02T01:30:00" in ANSI.sub("", result.output)


@pytest.mark.parametrize("ok", [True, False])
def test_status_json_preserves_complete_answer(
    monkeypatch, cli_env, local_timezone, display_status, ok
) -> None:
    response = display_status if ok else {"ok": False, "error": "unknown job", **_HEADER}
    _engine(monkeypatch, status=response)
    result = _run("query", "status", "--json")
    assert result.exit_code == (0 if ok else 2)
    assert result.stdout == json.dumps(response, sort_keys=True) + "\n"


@pytest.mark.parametrize("brief", [False, True])
def test_status_refusal_prints_error_text(monkeypatch, cli_env, local_timezone, brief) -> None:
    _engine(monkeypatch, status={"ok": False, "error": "unknown job"})
    result = _run("query", "status", *(["--brief"] if brief else []))
    assert result.exit_code == 2
    assert "unknown job" in ANSI.sub("", result.output)


@pytest.mark.parametrize("verb", ["trace", "timers", "subscribe", "is-success"])
def test_json_belongs_to_status_only(monkeypatch, cli_env, verb) -> None:
    engine = _engine(monkeypatch)
    result = _run("query", verb, "--json")
    assert result.exit_code == 2
    assert "--json applies to status only" in result.output
    assert not engine.requests


def test_json_refuses_brief_before_contacting_engine(monkeypatch, cli_env) -> None:
    engine = _engine(monkeypatch)
    result = _run("query", "status", "--json", "--brief")
    assert result.exit_code == 2
    assert "--json and --brief cannot be combined" in result.output
    assert not engine.requests


@pytest.mark.parametrize("verb", ["trace", "timers"])
def test_other_query_output_stays_utc_json(monkeypatch, cli_env, local_timezone, verb) -> None:
    response = {"ok": True, "entries": [{"at": "2026-01-01T23:30:00"}]}
    _engine(monkeypatch, **{verb: response})
    result = _run("query", verb)
    assert result.exit_code == 0
    assert result.stdout == json.dumps(response, sort_keys=True) + "\n"


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


def test_ui_refuses_a_control_socket_that_does_not_exist(short_root: Path) -> None:
    result = _run("ui", socket=short_root / "control.sock")
    assert result.exit_code == 2
    assert "no engine is running" in result.output


def test_serve_gets_the_server_class_from_the_textual_serve_extra() -> None:
    from textual_serve.server import Server

    assert cli_control._import_textual_serve_or_exit_2() is Server


# -------------------------------------------------------------------- supervise


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


#: PING's answer of a supervisor that serves: no admitted operation to watch (DL-401)
_SERVING = {"ok": True, "incarnation": "a" * 32, "process_state": "serving"}


_DRAINING = "supervisor is draining; wait for the unit to stop"
_LEASE = {"ok": True, "incarnation": "a" * 32, "token": 3}


# ------------------------------------------------------------------ engine stop


def _roundtrip(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    """`engine stop` calls `runner_control.roundtrip` itself: it tells an unreachable engine
    from one that took the request."""
    from dsl41 import runner_control

    sent: list[dict[str, Any]] = []

    def roundtrip(_socket: Path, request: dict[str, Any]) -> Any:
        sent.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runner_control, "roundtrip", roundtrip)
    return sent


def test_engine_stop_sends_the_stop_and_prints_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _roundtrip(monkeypatch, {"ok": True, "engine": "DRAINING"})
    result = _run("engine", "stop")
    assert (result.exit_code, sent) == (0, [{"cmd": "engine_stop"}])
    assert '"engine": "DRAINING"' in result.output


def test_engine_stop_refused_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _roundtrip(monkeypatch, {"ok": False, "code": "access_denied", "error": "needs adm"})
    assert _run("engine", "stop", "--wait").exit_code == 2


@pytest.mark.parametrize(
    ("delivered", "wait", "code"),
    [(False, False, 2), (False, True, 0), (True, True, 2)],
    ids=["unreachable", "unreachable_waiting", "lost_after_the_write"],
)
def test_engine_stop_with_an_engine_that_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, delivered: bool, wait: bool, code: int
) -> None:
    """An engine that is not running is stopped, for `--wait` (the unit's ExecStop); a write
    that may have reached it is an unknown outcome and exits 2."""
    from dsl41.runner_control import ControlClientError

    _roundtrip(monkeypatch, ControlClientError("refused", delivered=delivered))
    args = ["engine", "stop", *(["--wait"] if wait else [])]
    result = _run(*args, socket=tmp_path / "control.sock")
    assert result.exit_code == code, result.output


def test_engine_stop_waits_for_the_socket_and_then_the_lock(
    monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    """`--wait` returns once the socket refuses connects and `leader.lock` is free."""
    import fcntl
    import os
    import socket as socket_mod

    _roundtrip(monkeypatch, {"ok": True, "engine": "DRAINING"})
    monkeypatch.setattr(cli_control, "STOP_POLL_S", 0.01)
    path = short_root / "control.sock"
    server = socket_mod.socket(socket_mod.AF_UNIX)
    server.bind(str(path))
    server.listen(8)
    lock = os.open(short_root / "leader.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    polls: list[str] = []
    real_answers, real_held = cli_control._answers, cli_control._lock_held

    def answers(socket_path: Path) -> bool:
        polls.append("socket")
        if polls.count("socket") == 2:
            server.close()
        return real_answers(socket_path)

    def held(lock_path: Path) -> bool:
        polls.append("lock")
        if polls.count("lock") == 2:
            os.close(lock)
        return real_held(lock_path)

    monkeypatch.setattr(cli_control, "_answers", answers)
    monkeypatch.setattr(cli_control, "_lock_held", held)
    assert _run("engine", "stop", "--wait", socket=path).exit_code == 0
    assert polls == ["socket", "socket", "lock", "lock"]
    assert not real_held(short_root / "absent.lock")


@pytest.mark.parametrize(
    ("answers", "held", "code", "sends"),
    [
        (["undelivered", "undelivered", {"ok": True, "engine": "DRAINING"}], [True] * 3, 0, 3),
        (["undelivered", "undelivered"], [True, False], 0, 2),
        (["undelivered", "delivered"], [True], 2, 2),
    ],
    ids=["delivered_once_it_serves", "lock_freed_first", "lost_after_the_write"],
)
def test_engine_stop_waits_for_an_engine_that_is_still_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answers: list[Any],
    held: list[bool],
    code: int,
    sends: int,
) -> None:
    """`--wait` with no socket and `leader.lock` held: an engine is starting, so the stop is
    sent again every poll until it is delivered (then the command waits for the exit) or the
    lock frees (nothing to stop). Waiting on the lock alone would leave it running."""
    from dsl41 import runner_control
    from dsl41.runner_control import ControlClientError

    script = iter(answers)
    sent: list[dict[str, Any]] = []

    def roundtrip(_socket: Path, request: dict[str, Any]) -> Any:
        sent.append(request)
        answer = next(script)
        if isinstance(answer, str):
            raise ControlClientError(answer, delivered=answer == "delivered")
        return answer

    locks = iter(held)
    waited: list[Path] = []
    monkeypatch.setattr(runner_control, "roundtrip", roundtrip)
    monkeypatch.setattr(cli_control, "STOP_POLL_S", 0.0)
    monkeypatch.setattr(cli_control, "_lock_held", lambda _path: next(locks))
    monkeypatch.setattr(cli_control, "_wait_for_exit", waited.append)
    result = _run("engine", "stop", "--wait", socket=tmp_path / "control.sock")
    assert (result.exit_code, len(sent)) == (code, sends), result.output
    assert waited == ([tmp_path / "control.sock"] if sends == 3 else [])


@pytest.mark.usefixtures("cli_env")
@pytest.mark.parametrize(
    "value,verb,valid",
    [
        (None, "KILLJOB", True),
        ("9", "KILLJOB", True),
        ("15,2,2,9", "KILLJOB", True),
        ("", "KILLJOB", False),
        ("2", "KILLJOB", False),
        ("0,9", "KILLJOB", False),
        ("99999,9", "KILLJOB", False),
        ("INT,9", "KILLJOB", False),
        ("2,,9", "KILLJOB", False),
        ("2, 9", "KILLJOB", False),
        ("9", "STARTJOB", False),
    ],
)
def test_sendevent_k_validation_and_version(monkeypatch, value, verb, valid):
    from dsl41.runner_control import versioned

    requests = []
    monkeypatch.setattr(cli_control, "_pinned_read", lambda *a: ("b", 1))
    monkeypatch.setattr(
        cli_control, "_mutate", lambda path, request: requests.append(versioned(request))
    )
    args = ["sendevent", verb, "-J", "j"] + ([] if value is None else ["-k", value])
    result = _run(*args)
    assert result.exit_code == (0 if valid else 2), result.output
    if valid:
        [request] = requests
        assert request["v"] == (3 if value is None else 4)
        assert request["payload"] == {
            "job": "j",
            **({} if value is None else {"kill_signals": [int(v) for v in value.split(",")]}),
        }
    else:
        assert requests == []


@pytest.mark.parametrize("verb", ["reread", "update"])
@pytest.mark.parametrize("code", ["unsupported_version", "unknown_cmd", "invalid_argument"])
def test_reload_old_engine_has_no_fallback(monkeypatch, cli_env, verb, code):
    from dsl41 import runner_control

    calls = []

    def reply(path, request, **kwargs):
        calls.append(request)
        if request["cmd"] == "status":
            return {"ok": True, "baseline_id": "test"}
        return {"ok": False, "refused": True, "code": code, "error": "old engine"}

    monkeypatch.setattr(runner_control, "roundtrip", reply)
    result = _run(verb)
    assert result.exit_code == 2, result.output
    assert "old engine" in result.output
    assert len([r for r in calls if r["cmd"] == verb]) == 1
    assert calls[-1]["v"] == 4


def test_reload_help_has_no_legacy_route(cli_env):
    for verb in ("reread", "update"):
        output = ANSI.sub("", runner.invoke(app, [verb, "--help"]).output)
        assert "--socket" in output and "DSL41_SOCKET" in output
        for retired in ("--stop-running", "--run-root", "--config", "TOKEN", "ESTATE"):
            assert retired not in output
        assert runner.invoke(app, [verb, "legacy", "-S", "/none"]).exit_code == 2


@pytest.mark.parametrize(
    "arguments,error",
    [
        (("query", "trace", "j"), "JOB applies to status only"),
        (("query", "trace", "--style", "focused"), "--style applies to status only"),
        (("query", "trace", "--style", "neutral"), "--style applies to status only"),
    ],
)
def test_query_cli_refuses_job_or_style_for_other_verbs(monkeypatch, cli_env, arguments, error):
    engine = _engine(monkeypatch)
    result = _run(*arguments)
    assert result.exit_code == 2 and error in result.stderr
    assert engine.requests == []
