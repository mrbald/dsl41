"""DL-393: execute complete recovery commands from the selected release wheel.

Set DSL41_RELEASE_WHEEL and DSL41_RECOVERY_PYTHON to a wheel and a non-venv
Python. DSL41_RECOVERY_WHEELHOUSE defaults to the wheel's directory. The
wheelhouse must contain that Python's UI dependency wheels. Installation is
offline and writes only to pytest's temporary user base and virtual environments.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest


def _run(
    arguments: list[str], work: Path, environment: dict[str, str], *, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=work,
        env=environment,
        input=input,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (arguments, result.stdout, result.stderr)
    return result


@pytest.fixture(scope="module", params=["user-site", "retained-venv"])
def recovery_install(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Path, dict[str, str], Path, Path | None]:
    wheel_value = os.environ.get("DSL41_RELEASE_WHEEL")
    python_value = os.environ.get("DSL41_RECOVERY_PYTHON")
    if wheel_value is None or python_value is None:
        pytest.skip("set DSL41_RELEASE_WHEEL and DSL41_RECOVERY_PYTHON for installed recovery")
    wheel = Path(wheel_value).resolve()
    python = Path(python_value).absolute()
    assert wheel.is_file() and python.is_file()
    wheelhouse = Path(os.environ.get("DSL41_RECOVERY_WHEELHOUSE", str(wheel.parent))).resolve()
    work = tmp_path_factory.mktemp(f"recovery-{request.param}")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PIP_")) and key != "VIRTUAL_ENV"
    }
    environment.update(PYTHONUSERBASE=str(work / "user ' base"), PIP_CONFIG_FILE=os.devnull)
    _run(
        [str(python), "-c", "import sys; assert sys.prefix == sys.base_prefix"],
        work,
        environment,
    )
    _run([str(python), "-m", "ensurepip", "--user"], work, environment)
    install = [
        "install",
        "--no-index",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--find-links",
        str(wheelhouse),
        "--ignore-installed",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        f"{wheel}[ui]",
    ]
    if request.param == "user-site":
        _run([str(python), "-m", "pip", *install, "--user"], work, environment)
        return python, environment, work, None
    retained = work / ".venvs" / "retained ' runtime"
    replacement = work / ".venvs" / "replacement"
    for runtime in (retained, replacement):
        _run([str(python), "-m", "venv", "--without-pip", str(runtime)], work, environment)
    _run(
        [str(python), "-m", "pip", "--python", str(retained / "bin/python"), *install],
        work,
        environment,
    )
    active = work / "active"
    active.symlink_to(retained, target_is_directory=True)
    return active / "bin/python", environment, work, replacement


_GENERATE = """
import json
import sys
from pathlib import Path
from dsl41 import runner_tui

inputs = json.load(sys.stdin)
if inputs['replacement'] is not None:
    active = Path(inputs['active'])
    active.unlink()
    active.symlink_to(inputs['replacement'], target_is_directory=True)
print(json.dumps({
    'recovery': runner_tui.recovery_command(Path(inputs['socket']), inputs['request']),
    'module': runner_tui.__file__,
    'prefix': sys.prefix,
    'base_prefix': sys.base_prefix,
    'executable': sys.executable,
}))
"""


def test_installed_recovery_executes_with_original_envelope_and_runtime(
    recovery_install: tuple[Path, dict[str, str], Path, Path | None], short_root: Path
) -> None:
    python, environment, work, replacement = recovery_install
    endpoint = short_root / "control.sock"
    original = {
        "cmd": "sendevent",
        "verb": "SET_GLOBAL",
        "payload": {"name": "FLAG", "value": "a'b;$HOME=$(pwd)"},
        "baseline_id": "original ' period",
        "request_id": "request ' one",
        "expect": {"global:FLAG": 17},
        "claimed_actor": "original user@host",
    }
    retained_python = python.parent.resolve() / python.name
    generated = json.loads(
        _run(
            [str(python), "-c", _GENERATE],
            work,
            environment,
            input=json.dumps(
                {
                    "socket": str(endpoint),
                    "request": original,
                    "active": str(python.parent.parent),
                    "replacement": str(replacement) if replacement else None,
                }
            ),
        ).stdout
    )
    args = shlex.split(generated["recovery"])
    assert args[:4] == [str(retained_python), "-P", "-m", "dsl41"]
    if replacement is None:
        assert generated["prefix"] == generated["base_prefix"]
        assert Path(generated["module"]).is_relative_to(environment["PYTHONUSERBASE"])
    else:
        assert generated["prefix"] != generated["base_prefix"]
        assert Path(generated["module"]).resolve().is_relative_to(retained_python.parent.parent)
        assert python.parent.parent.resolve() == replacement
        assert retained_python.is_symlink()
        assert retained_python.resolve() != retained_python

    shadow = work / "recovery directory" / "dsl41"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise RuntimeError('cwd shadow imported')\n")
    (shadow / "__main__.py").write_text("raise RuntimeError('cwd shadow executed')\n")
    requests: list[dict[str, Any]] = []
    failures: list[BaseException] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(endpoint))
        listener.listen(2)
        listener.settimeout(10)

        def capture() -> None:
            try:
                for index in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(10)
                        data = bytearray()
                        while not data.endswith(b"\n"):
                            chunk = connection.recv(65536)
                            assert chunk, "client closed before complete request"
                            data.extend(chunk)
                        requests.append(json.loads(data))
                        response = (
                            {
                                "ok": True,
                                "baseline_id": "different",
                                "globals": {"FLAG": {"value": "new", "state_rev": 333}},
                            }
                            if index == 0
                            else {"ok": True, "decision": "applied", "index": 1}
                        )
                        connection.sendall(json.dumps(response).encode() + b"\n")
            except BaseException as error:
                failures.append(error)

        worker = threading.Thread(target=capture)
        worker.start()
        try:
            _run(["/bin/sh", "-c", generated["recovery"]], shadow.parent, environment)
        finally:
            worker.join(11)
            listener.close()
            worker.join(1)
        assert not worker.is_alive()
        assert not failures, failures
    assert len(requests) == 2
    assert requests[1].pop("v") == 3
    assert requests[1] == original
