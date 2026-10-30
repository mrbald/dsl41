"""DL-391: TUI recovery commands reconstruct the one original mutation."""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
import typer
from test_runner_tui import _serve, _teardown, _wait_for_ui
from textual.widgets import RichLog
from typer.testing import CliRunner

from dsl41 import cli_control, runner_tui
from dsl41.cli import app as cli_app
from dsl41.runner_clock import EngineError
from dsl41.runner_control import JOB_EVENT_VERBS, ControlClientError, command
from dsl41.runner_tui import RunnerApp, parse_console_command, recovery_command


@pytest.mark.parametrize(
    "console",
    [
        *(f"{verb} target" for verb in sorted(JOB_EVENT_VERBS)),
        "SET_GLOBAL FLAG=a'b;$HOME=$(pwd)",
        "CHANGE_STATUS target FAILURE -7",
    ],
)
def test_tui_recovery_command_reconstructs_the_complete_envelope(
    console: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parsed = parse_console_command(console, None)
    assert isinstance(parsed, dict)
    payload = parsed["payload"]
    key = f"global:{payload['name']}" if "name" in payload else f"job:{payload['job']}"
    original = command(
        parsed["verb"],
        payload,
        key=key,
        revision=17,
        baseline_id="original ' period",
        request_id="request ' one",
        claimed_actor="original user@host",
    )
    socket_path = tmp_path / "path ' with spaces" / "control.sock"
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli_control,
        "_control_roundtrip",
        lambda *_: {
            "ok": True,
            "baseline_id": "different",
            "jobs": {"target": {"state_rev": 333}},
            "globals": {"FLAG": {"value": "new", "state_rev": 333}},
        },
    )

    def capture(_socket: Path, request: dict[str, Any]) -> None:
        assert _socket == socket_path
        sent.append(request)
        raise typer.Exit(0)

    monkeypatch.setattr(cli_control, "_mutate", capture)
    recovery = shlex.split(recovery_command(socket_path, original))
    assert recovery[:4] == [
        str(Path(sys.executable).parent.resolve() / Path(sys.executable).name),
        "-P",
        "-m",
        "dsl41",
    ]
    result = CliRunner().invoke(cli_app, recovery[4:])
    assert result.exit_code == 0, result.output
    assert sent == [original]


def test_recovery_pins_the_socket_directory_and_backing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original_python = Path(sys.executable).parent.resolve() / Path(sys.executable).name
    monkeypatch.setattr(sys, "argv", ["/installed/dsl41/__main__.py", "ui"])
    monkeypatch.setattr(sys, "executable", "/different/runtime/bin/python")
    envelope = command(
        "ON_HOLD",
        {"job": "j"},
        key="job:j",
        revision=1,
        baseline_id="p",
        claimed_actor="operator@synthetic",
    )
    args = shlex.split(recovery_command(Path("control.sock"), envelope))
    assert args[:4] == [str(original_python), "-P", "-m", "dsl41"]
    assert args[args.index("--socket") + 1] == str(tmp_path / "control.sock")


@pytest.mark.parametrize("failure", [ControlClientError("unavailable"), EngineError("unreadable")])
def test_destructive_confirmation_requires_readable_frozen_pins(
    failure: Exception, short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "j" in app._rows)
                lines: list[str] = []
                sent: list[dict[str, Any]] = []
                real_request = app._client.request

                async def unavailable(_key: str) -> tuple[str, int, int]:
                    raise failure

                async def confirm(*_args: Any) -> bool:
                    pytest.fail("unreadable pins must not open a confirmation")

                async def capture(request: dict[str, Any]) -> dict[str, Any]:
                    if request.get("cmd") == "sendevent":
                        sent.append(request)
                    return await real_request(request)

                monkeypatch.setattr(app, "_precondition", unavailable)
                monkeypatch.setattr(app, "_confirm", confirm)
                monkeypatch.setattr(app, "_console_write", lambda line: lines.append(line.plain))
                monkeypatch.setattr(app._client, "request", capture)
                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: any("cannot confirm" in line for line in lines))
                assert sent == []
                assert not isinstance(app.screen, runner_tui.ConfirmScreen)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


@pytest.mark.parametrize("actor", ["original user@host", ""])
def test_recovery_actor_pin_is_metadata_and_preserves_an_empty_claim(
    actor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_control, "_pinned_read", lambda *_: ("period", 7))

    def capture(_socket: Path, request: dict[str, Any]) -> None:
        sent.append(request)
        raise typer.Exit(0)

    monkeypatch.setattr(cli_control, "_mutate", capture)
    result = CliRunner().invoke(
        cli_app,
        ["sendevent", "ON_HOLD", "--job", "j", "--socket", "/unused", "--claimed-actor", actor],
    )
    assert result.exit_code == 0, result.output
    assert sent[0]["claimed_actor"] == actor


@pytest.mark.parametrize("lost", ["transport", "answer", "before_write"])
def test_pilot_displays_recovery_before_send_keeps_it_with_unknown_and_never_resends(
    lost: str, short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", "insert_job: recovery_job\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(150, 44)) as pilot:
                await _wait_for_ui(pilot, lambda: "recovery_job" in app._rows)
                lines: list[str] = []
                sent: list[dict[str, Any]] = []
                built: list[dict[str, Any]] = []
                refreshed = False
                real_command = runner_tui.command
                real_write = app._console_write
                console = app.query_one("#console", RichLog)
                real_refresh = console.wait_for_refresh
                real_request = app._client.request

                def construct(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    envelope = real_command(*args, **kwargs)
                    built.append(envelope)
                    return envelope

                def write(line: Any) -> None:
                    lines.append(line.plain)
                    real_write(line)

                async def refresh() -> bool:
                    nonlocal refreshed
                    result = await real_refresh()
                    refreshed = True
                    return result

                async def exchange(request: dict[str, Any]) -> dict[str, Any]:
                    if request.get("cmd") != "sendevent":
                        return await real_request(request)
                    assert refreshed
                    assert lines[-1] == f"sending: {recovery_command(server.path, request)}"
                    sent.append(request)
                    if lost == "before_write":
                        raise ControlClientError("connect refused")
                    await real_request(request)
                    if lost == "transport":
                        raise ControlClientError("answer lost", delivered=True)
                    return {"ok": False, "code": "decision_timeout", "error": "answer lost"}

                monkeypatch.setattr(runner_tui, "command", construct)
                monkeypatch.setattr(app, "_console_write", write)
                monkeypatch.setattr(console, "wait_for_refresh", refresh)
                monkeypatch.setattr(app._client, "request", exchange)
                await pilot.press("h")
                await _wait_for_ui(pilot, lambda: bool(sent) and len(lines) >= 2)
                await app._refresh()
                await app._refresh()
                assert len(built) == len(sent) == 1
                assert built[0] is sent[0]
                recovery = recovery_command(server.path, sent[0])
                if lost == "before_write":
                    assert any("not sent (connect refused)" in line for line in lines)
                    assert not any("recover ONLY" in line for line in lines)
                else:
                    unknown = next(i for i, line in enumerate(lines) if "NO DECISION" in line)
                    assert lines[unknown + 1] == f"re-read; recover ONLY as: {recovery}"
                    assert app._jobs_snapshot["recovery_job"]["on_hold"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_reconnect_keeps_the_last_table_and_never_replays_a_mutation(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", "insert_job: reconnect_job\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "reconnect_job" in app._rows)
                sent: list[dict[str, Any]] = []
                real_request = app._client.request
                lines: list[str] = []
                real_write = app._console_write

                def write(text: Any) -> None:
                    lines.append(text.plain)
                    real_write(text)

                async def capture(request: dict[str, Any]) -> dict[str, Any]:
                    if request.get("cmd") == "sendevent":
                        sent.append(request)
                    return await real_request(request)

                monkeypatch.setattr(app._client, "request", capture)
                monkeypatch.setattr(app, "_console_write", write)
                await pilot.press("h")
                await _wait_for_ui(pilot, lambda: app._jobs_snapshot["reconnect_job"]["on_hold"])
                revision = app._jobs_snapshot["reconnect_job"]["state_rev"]
                await server.close()
                await app._client.close()
                await app._refresh()
                assert app._connected is False
                assert app._jobs_snapshot["reconnect_job"]["on_hold"]
                await server.start()
                await app._refresh()
                assert app._connected is True
                assert app._jobs_snapshot["reconnect_job"]["state_rev"] == revision
                assert len(sent) == 1
                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, runner_tui.ConfirmScreen))
                await pilot.press("escape")
                await _wait_for_ui(
                    pilot,
                    lambda: (
                        not isinstance(app.screen, runner_tui.ConfirmScreen)
                        and any("cancelled" in line for line in lines)
                    ),
                )
                assert len(sent) == 1
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_cli_before_write_and_unknown_advice_include_the_original_actor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41 import runner_control
    from dsl41.cli_common import command_outcome

    envelope = command(
        "ON_HOLD",
        {"job": "j"},
        key="job:j",
        revision=3,
        baseline_id="p",
        request_id="r",
        claimed_actor="operator ' one",
    )

    def lose(_socket: Path, request: dict[str, Any]) -> dict[str, Any]:
        assert request is envelope
        before = capsys.readouterr().err
        assert shlex.split(before)[-2:] == ["--claimed-actor", "operator ' one"]
        raise ControlClientError("lost", delivered=True)

    monkeypatch.setattr(runner_control, "roundtrip", lose)
    assert command_outcome(Path("/unused"), envelope) == 4
    assert shlex.split(capsys.readouterr().err)[-2:] == ["--claimed-actor", "operator ' one"]


def test_pilot_a_send_resumed_under_a_modal_renders_the_pinned_command_first(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-391: a send worker that resumes while another send's ConfirmScreen is the active
    screen still renders the pinned command, then sends. `App.query_one` finds the console on
    the screen that composes it, so a modal on top does not hide it. This guards a lookup that
    is limited to the active screen."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", "insert_job: modal_job\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(150, 44)) as pilot:
                await _wait_for_ui(pilot, lambda: "modal_job" in app._rows)
                lines: list[str] = []
                sent: list[dict[str, Any]] = []
                screens: list[bool] = []
                real_write = app._console_write
                real_request = app._client.request
                real_precondition = app._precondition
                gate = asyncio.Event()

                waiting: list[str] = []

                async def slow(key: str) -> tuple[str, int, int]:
                    # only the first read, the console worker's, waits for the gate
                    if not waiting:
                        waiting.append(key)
                        await gate.wait()
                    return await real_precondition(key)

                def write(line: Any) -> None:
                    lines.append(line.plain)
                    real_write(line)

                async def exchange(request: dict[str, Any]) -> dict[str, Any]:
                    if request.get("cmd") == "sendevent":
                        screens.append(isinstance(app.screen, runner_tui.ConfirmScreen))
                        assert lines[-1] == f"sending: {recovery_command(server.path, request)}"
                        sent.append(request)
                    return await real_request(request)

                request = parse_console_command("ON_HOLD modal_job", None)
                assert isinstance(request, dict)
                monkeypatch.setattr(app, "_console_write", write)
                monkeypatch.setattr(app._client, "request", exchange)
                monkeypatch.setattr(app, "_precondition", slow)
                app.run_worker(app._do_sendevent(request), group="send", exclusive=False)
                await _wait_for_ui(pilot, lambda: bool(waiting))
                app._selected = "modal_job"
                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, runner_tui.ConfirmScreen))
                gate.set()
                await _wait_for_ui(pilot, lambda: bool(sent))
                await _wait_for_ui(pilot, lambda: app._jobs_snapshot["modal_job"]["on_hold"])
                assert screens == [True]
                assert isinstance(app.screen, runner_tui.ConfirmScreen)
                assert app._exception is None
                assert len(sent) == 1
                await pilot.press("escape")
                await _wait_for_ui(
                    pilot, lambda: not isinstance(app.screen, runner_tui.ConfirmScreen)
                )
                assert len(sent) == 1
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())
