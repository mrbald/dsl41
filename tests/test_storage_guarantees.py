"""O: portable DL-99 exclusion; installed and ENOSPC cells run in lane S."""

from pathlib import Path
import json
import runpy
import signal
import socket
import subprocess
import sys
import time

import pytest

PROBE = Path(__file__).parents[1] / "examples/nightbank/deploy/storage_probe.py"
run_probe = runpy.run_path(str(PROBE))["run_probe"]


def test_frozen_leader_refuses_a_contender_before_and_after_cont(short_root: Path):
    """O: extra legacy child: exit 2, no writes and one effect (DL-99/DL-428)."""
    result = run_probe(
        [sys.executable, str(PROBE), "leader", "--worker", "--work", str(short_root)],
    )
    assert result.returncode == 0, result.stdout + result.stderr


FROZEN_CHILD = r"""
import json, os, runpy, signal, socket, sys
from pathlib import Path
probe = runpy.run_path(sys.argv[1])
signal.signal(signal.SIGTERM, probe['deadline_signal'])
work = Path(sys.argv[2])
work.mkdir()
with probe['engine'](work) as live:
    live.freeze()
    Path(sys.argv[3]).write_text(str(live.proc.pid))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ready:
        ready.connect(sys.argv[4])
        ready.sendall(json.dumps({'event': 'engine_stopped', 'engine': live.proc.pid,
            'worker': os.getpid(), 'supervisor': os.getppid()}).encode() + b'\n')
    signal.pause()
"""


def frozen_acknowledgement(listener, pidfile, *, terminate_supervisor):
    def on_started(process):
        deadline = time.monotonic() + 30

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("no frozen-engine acknowledgement within 30 seconds")
            return seconds

        listener.settimeout(remaining())
        connection, _ = listener.accept()
        with connection:
            data = bytearray()
            while not data.endswith(b"\n"):
                connection.settimeout(remaining())
                chunk = connection.recv(4096)
                assert chunk, "worker closed before its acknowledgement"
                data.extend(chunk)
        acknowledgement = json.loads(data)
        assert acknowledgement["event"] == "engine_stopped"
        assert acknowledgement["engine"] == int(pidfile.read_text())
        owner = "supervisor" if terminate_supervisor else "worker"
        assert acknowledgement[owner] == process.pid
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(acknowledgement["engine"])],
            capture_output=True,
            text=True,
            timeout=remaining(),
        )
        assert state.returncode == 0 and state.stdout.strip().startswith("T"), state
        if terminate_supervisor:
            process.send_signal(signal.SIGTERM)

    return on_started


def assert_not_running(pid: int):
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    assert not state or state.startswith("Z"), f"engine {pid} survived cleanup: {state}"


def test_probe_deadline_ends_the_group_with_a_stopped_engine(short_root: Path):
    """O: guide §4 pins no running/stopped child after the caller's deadline."""
    pidfile = short_root / "engine.pid"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        endpoint = short_root / "ready.sock"
        listener.bind(str(endpoint))
        listener.listen(1)
        with pytest.raises(subprocess.TimeoutExpired):
            run_probe(
                [
                    sys.executable,
                    "-c",
                    FROZEN_CHILD,
                    str(PROBE),
                    str(short_root / "child"),
                    str(pidfile),
                    str(endpoint),
                ],
                timeout_s=0.1,
                on_started=frozen_acknowledgement(listener, pidfile, terminate_supervisor=False),
            )
    assert_not_running(int(pidfile.read_text()))


def test_service_probe_term_ends_the_group_with_a_stopped_engine(short_root: Path):
    """O: guide §4 pins cleanup when an external timeout sends the supervisor TERM."""
    supervisor = """
import runpy, signal, sys
probe = runpy.run_path(sys.argv[1])
signal.signal(signal.SIGTERM, probe['deadline_signal'])
probe['run_probe']([sys.executable, '-c', sys.argv[2], *sys.argv[1:2], *sys.argv[3:]], 30)
"""
    pidfile = short_root / "engine.pid"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        endpoint = short_root / "ready.sock"
        listener.bind(str(endpoint))
        listener.listen(1)
        result = run_probe(
            [
                sys.executable,
                "-c",
                supervisor,
                str(PROBE),
                FROZEN_CHILD,
                str(short_root / "child"),
                str(pidfile),
                str(endpoint),
            ],
            timeout_s=20,
            on_started=frozen_acknowledgement(listener, pidfile, terminate_supervisor=True),
        )
    assert result.returncode == 124, result.stdout + result.stderr
    assert_not_running(int(pidfile.read_text()))
