"""DL-389, DL-391: subprocess contracts of an explicitly selected installed wheel."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def installed_cli() -> Path:
    value = os.environ.get("DSL41_INSTALLED_EXECUTABLE")
    if value is None:
        pytest.skip("set DSL41_INSTALLED_EXECUTABLE to qualify an installed wheel")
    path = Path(value)
    assert path.is_absolute() and path.is_file(), value
    return path


def invoke(cli: Path, work: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment.update(NO_COLOR="1", TERM="dumb")
    return subprocess.run(
        [str(cli), *args], cwd=work, env=environment, capture_output=True, text=True, timeout=30
    )


@pytest.mark.parametrize(
    "command",
    [
        (),
        *(
            (name,)
            for name in (
                "lint",
                "report",
                "viz",
                "equiv",
                "uc",
                "decompile",
                "folds",
                "minify",
                "resolve",
                "run",
                "rehearse",
                "journal",
                "runs",
                "query",
                "sendevent",
                "release-held",
                "host",
                "ui",
                "serve",
                "supervise",
                "seal",
                "audit",
                "verify",
                "estate",
            )
        ),
        ("estate", "prune"),
        ("estate", "reclaim"),
    ],
)
def test_installed_public_help(
    installed_cli: Path, tmp_path: Path, command: tuple[str, ...]
) -> None:
    result = invoke(installed_cli, tmp_path, *command, "--help")
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ["no-such-command"],
        ["supervise", "shutdown", "--run-root", "/nonexistent-dsl41-root", "--force=orphan"],
        ["supervise", "start", "--run-root", "/nonexistent-dsl41-root", "--force=terminate"],
        ["sendevent", "STARTJOB"],
    ],
)
def test_installed_usage_refuses(installed_cli: Path, tmp_path: Path, arguments: list[str]) -> None:
    result = invoke(installed_cli, tmp_path, *arguments)
    assert result.returncode == 2
    assert result.stderr


@pytest.mark.parametrize(
    "answer", ["applied", "refused", "rejected", "lost", "truncated", "partial"]
)
def test_installed_mutation_outcomes_and_no_resend(
    installed_cli: Path, short_root: Path, answer: str
) -> None:
    """A real executable receives failures on both sides of its mutation write."""
    endpoint = short_root / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(endpoint))
    listener.listen(2)
    listener.settimeout(10)
    requests: list[dict[str, Any]] = []
    failures: list[BaseException] = []

    def server() -> None:
        try:
            for index in range(2):
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(10)
                    if index == 1 and answer == "partial":
                        assert connection.recv(32)
                        return
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(65536)
                        if not chunk:
                            raise AssertionError("client closed before complete request")
                        data.extend(chunk)
                    request = json.loads(data)
                    requests.append(request)
                    if index == 0:
                        response = {
                            "ok": True,
                            "baseline_id": "current",
                            "epoch": 99,
                            "globals": {"FLAG": {"value": "old", "state_rev": 99}},
                        }
                    elif answer in {"lost", "truncated"}:
                        if answer == "truncated":
                            connection.sendall(b'{"ok": true')
                        return
                    else:
                        response = {"ok": answer == "applied", "decision": answer}
                        if answer == "refused":
                            response["error"] = "permission_denied"
                            response["refused"] = True
                    connection.sendall(json.dumps(response).encode() + b"\n")
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=server)
    worker.start()
    try:
        value = "x" * 100_000 if answer == "partial" else "quoted ' value"
        result = invoke(
            installed_cli,
            short_root,
            "sendevent",
            "SET_GLOBAL",
            "-G",
            f"FLAG={value}",
            "-S",
            str(endpoint),
            "--request-id",
            "original-id",
            "--expect",
            "7",
            "--epoch",
            "3",
            "--baseline",
            "original-baseline",
            "--claimed-actor",
            "actor@host",
        )
    finally:
        worker.join(11)
        listener.close()
        worker.join(1)
    assert not worker.is_alive()
    assert not failures, failures
    assert result.returncode == {"applied": 0, "refused": 2, "rejected": 3}.get(answer, 4), result
    assert "original-id" in result.stderr and "--claimed-actor actor@host" in result.stderr
    assert len(requests) == (1 if answer == "partial" else 2)
    if answer != "partial":
        mutation = requests[1]
        assert mutation["request_id"] == "original-id"
        assert mutation["baseline_id"] == "original-baseline" and mutation["epoch"] == 3
        assert mutation["expect"] == {"global:FLAG": 7}
        assert mutation["claimed_actor"] == "actor@host"


def test_installed_socket_permissions_refuse_before_write(
    installed_cli: Path, short_root: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses socket mode; run this lane as the unprivileged rehearsal user")
    endpoint = short_root / "denied.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(endpoint))
        listener.listen(1)
        endpoint.chmod(0)
        try:
            result = invoke(installed_cli, short_root, "query", "status", "-S", str(endpoint))
        finally:
            endpoint.chmod(0o600)
        assert result.returncode == 2
        assert "Permission denied" in result.stderr or "not readable" in result.stderr, (
            result.stderr
        )
        listener.setblocking(False)
        with pytest.raises(BlockingIOError):
            listener.accept()
