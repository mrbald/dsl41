"""CLI reads report transport failures as exit 2 (DL-78)."""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import runner_control
from dsl41.cli import app
from dsl41.runner_procid import ProcessIdentity

pytestmark = pytest.mark.usefixtures("cli_env")

runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COMMANDS = (
    ("query", "status"),
    ("query", "status", "--job", "j"),
    ("query", "subscribe"),
    ("sendevent", "ON_HOLD", "--job", "j"),
    ("off-hold-all",),
    ("engine", "stop"),
    ("ui",),
    ("serve",),
)


def _assert_failure(command: tuple[str, ...], path: Path, diagnostic: str) -> None:
    args = [*command, "--socket", str(path)]
    if command in (("ui",), ("serve",)):
        cmd = [sys.executable, "-m", "dsl41", *args]
        if (
            command == ("serve",)
            and diagnostic == f"dsl41: no engine is running at {path}"
            and not path.exists()
        ):
            started = time.monotonic()
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as child:
                try:
                    stdout, stderr = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        group = os.getpgid(child.pid)
                    except OSError:
                        group = None
                    elapsed = time.monotonic() - started
                    child.kill()
                    stdout, stderr = child.communicate()
                    print(
                        f"serve timeout: elapsed={elapsed:.3f} pid={child.pid} pgid={group} "
                        f"returncode={child.returncode} stdout={stdout!r} stderr={stderr!r}",
                        file=sys.stderr,
                    )
                    raise
                result = subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        assert result.returncode == 2, result.stderr
    else:
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert isinstance(result.exception, SystemExit)
    assert ANSI.sub("", result.stderr) == diagnostic + "\n"
    assert ANSI.sub("", result.stdout) == ""


def _permission_message(path: Path) -> str:
    return (
        f"dsl41: no permission to use the control socket {path}; "
        "run as the engine's user or join its socket group"
    )


@pytest.fixture(params=["missing", "stale", "unreadable", "read_only", "no_search"])
def failed_socket(request: pytest.FixtureRequest, short_root: Path) -> Iterator[tuple[Path, str]]:
    case = request.param
    if case in ("unreadable", "read_only", "no_search") and os.geteuid() == 0:
        pytest.skip("root bypasses filesystem permissions")
    parent = short_root / "engine"
    parent.mkdir()
    path = parent / "control.sock"
    with socket.socket(socket.AF_UNIX) as server:
        if case != "missing":
            server.bind(str(path))
            if case == "stale":
                server.close()
            else:
                server.listen(8)
                if case == "no_search":
                    parent.chmod(0o600)
                else:
                    path.chmod(0o000 if case == "unreadable" else 0o400)
        diagnostic = (
            f"dsl41: no engine is running at {path}"
            if case in ("missing", "stale")
            else _permission_message(path)
        )
        try:
            yield path, diagnostic
        finally:
            parent.chmod(0o700)


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: "-".join(command))
def test_socket_commands_report_plain_connect_errors(
    command: tuple[str, ...], failed_socket: tuple[Path, str]
) -> None:
    path, diagnostic = failed_socket
    _assert_failure(command, path, diagnostic)


def test_query_status_reports_a_missing_control_socket(short_root: Path) -> None:
    socket_path = short_root / "control.sock"
    assert not socket_path.exists()

    result = runner.invoke(app, ["query", "status", "--socket", str(socket_path)])

    assert result.exit_code == 2
    assert ANSI.sub("", result.stderr) == f"dsl41: no engine is running at {socket_path}\n"
    assert ANSI.sub("", result.stdout) == ""
    assert "Traceback" not in ANSI.sub("", result.output)
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", (*COMMANDS, ("engine", "stop", "--wait")))
@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_socket_commands_map_permission_errnos(
    command: tuple[str, ...], error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    path = short_root / "control.sock"

    def deny(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(socket.socket, "connect", deny)
    result = runner.invoke(app, [*command, "--socket", str(path)])
    assert result.exit_code == 2, result.output
    assert ANSI.sub("", result.stderr) == _permission_message(path) + "\n"
    assert result.stdout == ""


@pytest.mark.parametrize("verb", ["catalog", "update"])
@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.ENOENT, errno.ECONNREFUSED])
def test_reload_socket_commands_use_the_same_diagnostics(
    verb: str, error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    from dsl41.cli_reload import SendFailed, socket_send

    path = short_root / "control.sock"

    def fail(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(socket.socket, "connect", fail)
    with pytest.raises(SendFailed) as raised:
        socket_send(path)(verb, {})
    expected = (
        _permission_message(path)
        if error in (errno.EACCES, errno.EPERM)
        else f"dsl41: no engine is running at {path}"
    )
    assert str(raised.value) == expected


@pytest.mark.parametrize("command", ["reread", "update"])
@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.ENOENT, errno.ECONNREFUSED])
def test_reload_cli_reports_plain_connect_errors(
    command: str, error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:

    path = short_root / "control.sock"

    def fail(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(socket.socket, "connect", fail)
    result = runner.invoke(app, [command, "-S", str(path)])
    expected = (
        _permission_message(path)
        if error in (errno.EACCES, errno.EPERM)
        else f"dsl41: no engine is running at {path}"
    )
    assert result.exit_code == 2, result.output
    assert result.stderr == expected + "\n"
    assert result.stdout == ""


def test_update_connect_failure_after_the_header_prints_no_recovery_receipt(
    monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:

    path = short_root / "control.sock"
    calls: list[str] = []
    real_roundtrip = runner_control.roundtrip

    def answer_header(socket_path: Path, request: dict[str, Any], **kwargs) -> dict[str, Any]:
        calls.append(request["cmd"])
        if request["cmd"] == "status":
            return {"ok": True, "baseline_id": "b"}
        return real_roundtrip(socket_path, request, **kwargs)

    monkeypatch.setattr(runner_control, "roundtrip", answer_header)
    result = runner.invoke(app, ["update", "-S", str(path)])
    assert calls == ["status", "update"]
    assert result.exit_code == 2, result.output
    assert result.stderr == f"dsl41: no engine is running at {path}\n"
    assert result.stdout == ""


def test_mutation_connect_failure_prints_no_recovery_receipt(
    failed_socket: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41.cli_common import command_outcome

    path, diagnostic = failed_socket
    assert command_outcome(path, {"cmd": "sendevent", "request_id": "r"}) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == diagnostic + "\n"


def test_delivered_permission_error_keeps_the_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], short_root: Path
) -> None:
    from dsl41.cli_common import command_outcome

    def lost(_path: Path, _request: dict[str, Any], *, before_send) -> dict[str, Any]:
        before_send()
        raise runner_control.ControlClientError("answer lost", delivered=True) from PermissionError(
            errno.EACCES, os.strerror(errno.EACCES)
        )

    monkeypatch.setattr(runner_control, "roundtrip", lost)
    request = runner_control.command(
        "STARTJOB", {"job": "j"}, key="job:j", revision=1, baseline_id="b", request_id="r"
    )
    assert command_outcome(short_root / "control.sock", request) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "sending: --request-id r --expect 1 --baseline b",
        "answer lost",
        "no decision: this command may still apply. Re-read, then retry ONLY as "
        "--request-id r --expect 1 --baseline b",
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permissions")
def test_ui_connects_to_a_write_only_socket(
    monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    from types import SimpleNamespace

    from dsl41 import cli_control

    path = short_root / "control.sock"
    opened: list[Path] = []
    app_stub = SimpleNamespace(run=lambda: opened.append(path), return_code=0)
    monkeypatch.setattr(
        cli_control,
        "import_tui_or_exit_2",
        lambda: SimpleNamespace(RunnerApp=lambda _path: app_stub),
    )
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        server.listen(1)
        server.settimeout(1.0)
        path.chmod(0o200)
        result = runner.invoke(app, ["ui", "--socket", str(path)])
        assert result.exit_code == 0, result.output
        assert result.output == ""
        assert opened == [path]
        peer, _ = server.accept()
        with peer:
            peer.settimeout(1.0)
            assert peer.recv(1) == b""


def test_ui_initial_connect_has_a_deadline(
    monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    path = short_root / "control.sock"

    def timeout(probe: socket.socket, address: str) -> None:
        assert address == str(path)
        assert probe.gettimeout() == 2.0
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(socket.socket, "connect", timeout)
    result = runner.invoke(app, ["ui", "--socket", str(path)])
    assert result.exit_code == 2
    assert result.stderr == f"control socket {path}: connect timed out\n"
    assert result.stdout == ""


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


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "error",
    [pytest.param(errno.EAGAIN, id="EAGAIN"), pytest.param(errno.EWOULDBLOCK, id="EWOULDBLOCK")],
)
def test_socket_commands_report_a_busy_engine(
    command: tuple[str, ...], error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    path = short_root / "control.sock"

    def busy(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(socket.socket, "connect", busy)
    result = runner.invoke(app, [*command, "--socket", str(path)])
    assert result.exit_code == 2, result.output
    expected = (
        f"dsl41: the engine at {path} is busy; it may already have taken the stop; "
        "check with dsl41 query before retrying"
        if command == ("engine", "stop")
        else f"dsl41: the engine at {path} is busy; try again"
    )
    assert result.stderr == expected + "\n"
    assert result.stdout == ""


@pytest.mark.parametrize("verb", ["reread", "update"])
@pytest.mark.parametrize("error", [errno.EAGAIN, errno.EWOULDBLOCK])
def test_reload_cli_reports_a_busy_engine(
    verb: str, error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:

    path = short_root / "control.sock"

    def busy(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(socket.socket, "connect", busy)
    result = runner.invoke(app, [verb, "-S", str(path)])
    assert result.exit_code == 2, result.output
    assert result.stderr == f"dsl41: the engine at {path} is busy; try again\n"
    assert result.stdout == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permissions")
@pytest.mark.parametrize("stale", [False, True], ids=["missing_socket", "stale_socket"])
def test_engine_stop_wait_reports_an_unreadable_leader_lock(stale: bool, short_root: Path) -> None:
    """DL-496: with no connection, an unreadable lock cannot confirm exit or startup."""
    path = short_root / "custom.sock"
    lock = short_root / "leader.lock"
    lock.touch(mode=0o600)
    if stale:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
    lock.chmod(0)
    try:
        result = runner.invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
    finally:
        lock.chmod(0o600)
    assert result.exit_code == 2, result.output
    assert result.stderr == (
        "dsl41: no stop was sent and --wait cannot confirm the exit because "
        f"{lock} is not readable to this user\n"
    )
    assert result.stdout == ""


@pytest.mark.parametrize("stale", [False, True], ids=["missing-socket", "stale-socket"])
def test_engine_stop_wait_confirms_a_root_with_no_leader_lock(
    short_root: Path, stale: bool
) -> None:
    """DL-496: without a connection or a lock, the CLI confirms nothing holds the root."""
    path = short_root / "control.sock"
    lock = short_root / "leader.lock"
    assert not lock.exists()
    if stale:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
    result = runner.invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("engine not running:")
    assert result.stderr == ""
    assert not lock.exists()
    assert path.exists() == stale


@pytest.mark.parametrize("retry", [False, True], ids=["initial-socket", "retry-socket"])
def test_engine_stop_reports_socket_allocation_failure(short_root, monkeypatch, retry):
    """Socket allocation precedes roundtrip's error wrapper, including during startup."""
    import fcntl

    from dsl41 import cli_control

    path = short_root / "control.sock"
    original = socket.socket
    attempts = []

    def allocate(family, *args, **kwargs):
        assert family == socket.AF_UNIX
        attempts.append(family)
        if len(attempts) == (2 if retry else 1):
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return original(family, *args, **kwargs)

    # A real exclusive lock and no socket are the engine's startup window.
    with (short_root / "leader.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(socket, "socket", allocate)
        monkeypatch.setattr(cli_control, "STOP_POLL_S", 0)
        result = runner.invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
    assert len(attempts) == (2 if retry else 1)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr == f"[Errno {errno.EMFILE}] {os.strerror(errno.EMFILE)}\n"


def test_real_descriptor_exhaustion_prevents_socket_allocation() -> None:
    """EMFILE is a real constructor failure; the subprocess owns its lowered limit."""
    probe = """
import errno, os, resource, socket
original = resource.getrlimit(resource.RLIMIT_NOFILE)
soft = 64 if original[0] == resource.RLIM_INFINITY else min(original[0], 64)
resource.setrlimit(resource.RLIMIT_NOFILE, (soft, original[1]))
fds = []
try:
    while True:
        try:
            fds.append(os.open('/dev/null', os.O_RDONLY))
        except OSError as exc:
            assert exc.errno == errno.EMFILE
            break
    try:
        sock = socket.socket(socket.AF_UNIX)
    except OSError as exc:
        assert exc.errno == errno.EMFILE
        print('AF_UNIX allocation: EMFILE')
    else:
        sock.close()
        raise AssertionError('socket allocation succeeded without a free descriptor')
finally:
    for fd in fds:
        os.close(fd)
    resource.setrlimit(resource.RLIMIT_NOFILE, original)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "AF_UNIX allocation: EMFILE\n"


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM])
def test_engine_stop_wait_reports_connect_failure_during_startup(
    error: int, monkeypatch: pytest.MonkeyPatch, short_root: Path
) -> None:
    from dsl41 import cli_control

    path = short_root / "control.sock"
    real_roundtrip = runner_control.roundtrip
    calls: list[str] = []

    def first_roundtrip(socket_path: Path, request: dict[str, Any], **kwargs) -> dict[str, Any]:
        calls.append(request["cmd"])
        if len(calls) == 1:
            raise runner_control.ControlClientError("engine starting")
        return real_roundtrip(socket_path, request, **kwargs)

    def fail(_socket: socket.socket, address: str) -> None:
        assert address == str(path)
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(runner_control, "roundtrip", first_roundtrip)
    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(cli_control, "_lock_held", lambda _path: True)
    monkeypatch.setattr(cli_control, "STOP_POLL_S", 0.0)
    result = runner.invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
    assert calls == ["engine_stop"] * 2
    assert result.exit_code == 2, result.output
    assert result.stderr == _permission_message(path) + "\n"
    assert result.stdout == ""


@pytest.mark.parametrize("error", [errno.EAGAIN, errno.EWOULDBLOCK])
@pytest.mark.parametrize(
    ("answers", "locks", "exit_code", "attempts"),
    [
        (["busy", "busy", "ok"], [True, True], 0, 3),
        (["busy", "busy"], [True, False], 0, 2),
        (["busy", "delivered_busy"], [True], 2, 2),
        (["delivered_busy"], [], 2, 1),
    ],
    ids=["retry_while_locked", "lock_freed", "retry_may_have_arrived", "first_may_have_arrived"],
)
def test_engine_stop_wait_retries_only_an_undelivered_busy_send(
    error: int,
    answers: list[str],
    locks: list[bool],
    exit_code: int,
    attempts: int,
    monkeypatch: pytest.MonkeyPatch,
    short_root: Path,
) -> None:
    from dsl41 import cli_control

    path = short_root / "control.sock"
    real_roundtrip = runner_control.roundtrip
    script = iter(answers)
    lock_script = iter(locks)
    calls: list[dict[str, Any]] = []
    waited: list[ProcessIdentity] = []
    lock_probes: list[Path] = []

    def roundtrip(socket_path: Path, request: dict[str, Any], **kwargs) -> dict[str, Any]:
        assert socket_path == path
        calls.append(request)
        answer = next(script)
        if answer == "ok":
            kwargs["peer"].append(ProcessIdentity(123, "ticks:1"))
            return {"ok": True, "engine": "DRAINING"}
        if answer == "busy":
            return real_roundtrip(socket_path, request, **kwargs)
        raise runner_control.ControlClientError("busy", delivered=True) from BlockingIOError(
            error, os.strerror(error)
        )

    def busy_connect(probe: socket.socket, address: str) -> None:
        assert address == str(path)
        assert probe.gettimeout() == 10.0
        raise BlockingIOError(error, os.strerror(error))

    def held(lock: Path) -> bool:
        lock_probes.append(lock)
        return next(lock_script)

    monkeypatch.setattr(runner_control, "roundtrip", roundtrip)
    monkeypatch.setattr(socket.socket, "connect", busy_connect)
    monkeypatch.setattr(cli_control, "_lock_held", held)
    monkeypatch.setattr(cli_control, "_wait_for_exit", waited.append)
    monkeypatch.setattr(cli_control, "STOP_POLL_S", 0.0)
    result = runner.invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
    assert result.exit_code == exit_code, result.output
    assert calls == [{"cmd": "engine_stop"}] * attempts
    assert lock_probes == [short_root / "leader.lock"] * len(locks)
    if exit_code == 2:
        assert result.stderr == (
            f"dsl41: the engine at {path} is busy; it may already have taken the stop; "
            "check with dsl41 query before retrying\n"
        )
    else:
        assert result.stderr == ""
    if answers[-1] == "ok":
        assert waited == [ProcessIdentity(123, "ticks:1")]
        assert result.stdout == '{"engine": "DRAINING", "ok": true}\n'
    else:
        assert waited == []
        if exit_code == 0:
            assert result.stdout == (
                f"engine not running: control socket {path}: [Errno {error}] {os.strerror(error)}\n"
            )
        else:
            assert result.stdout == ""


def test_engine_stop_help_names_delivered_exit_unconfirmed() -> None:
    result = runner.invoke(app, ["engine", "stop", "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(ANSI.sub("", result.stdout).split())
    assert "2 the engine refused it, could not" in help_text
    assert "stop was delivered but --wait could not confirm" in help_text
