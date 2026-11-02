"""O: portable DL-99 exclusion; installed and ENOSPC cells run in lane S."""

from pathlib import Path
import runpy
import subprocess
import sys

import pytest

PROBE = Path(__file__).parents[1] / "examples/nightbank/deploy/storage_probe.py"
run_probe = runpy.run_path(str(PROBE))["run_probe"]


def test_frozen_leader_refuses_a_contender_before_and_after_cont(short_root: Path):
    """O: extra legacy child: exit 2, no writes and one effect (DL-99/DL-428)."""
    result = run_probe(
        [sys.executable, str(PROBE), "leader", "--worker", "--work", str(short_root)],
    )
    assert result.returncode == 0, result.stdout + result.stderr


FROZEN_CHILD = """
import os, runpy, signal, sys, time
from pathlib import Path
probe = runpy.run_path(sys.argv[1])
signal.signal(signal.SIGTERM, probe['deadline_signal'])
work = Path(sys.argv[2])
work.mkdir()
with probe['engine'](work) as live:
    live.freeze()
    Path(sys.argv[3]).write_text(str(live.proc.pid))
    print('engine STOP acknowledged', flush=True)
    if sys.argv[4] == 'term':
        os.kill(os.getppid(), signal.SIGTERM)
    time.sleep(60)
"""


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
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        run_probe(
            [
                sys.executable,
                "-c",
                FROZEN_CHILD,
                str(PROBE),
                str(short_root / "child"),
                str(pidfile),
                "deadline",
            ],
            timeout_s=5,
        )
    assert "engine STOP acknowledged" in failure.value.output
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
    result = run_probe(
        [
            sys.executable,
            "-c",
            supervisor,
            str(PROBE),
            FROZEN_CHILD,
            str(short_root / "child"),
            str(pidfile),
            "term",
        ],
        timeout_s=20,
    )
    assert result.returncode == 124, result.stdout + result.stderr
    assert "engine STOP acknowledged" in result.stdout
    assert_not_running(int(pidfile.read_text()))
