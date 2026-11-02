"""U twins of the installed S fault cells; Linux processes, never a mocked absence."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from examples.nightbank.deploy.restart_faults import (
    CASES,
    cleanup_owned,
    owned_processes,
    until,
    witness_checks,
    cleanup_checks,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real /proc evidence")
DRIVER = Path(__file__).resolve().parents[1] / "examples/nightbank/deploy/restart_faults.py"


def kill_driver(process: subprocess.Popen, root: Path) -> None:
    try:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    finally:
        cleanup_owned(root)


@pytest.mark.parametrize("case", CASES)
def test_restart_fault_ordering(tmp_path: Path, case: str) -> None:
    """O, runner §7/DL-431/433/436: fixed effects, record order, holds and bounded refusal."""
    with subprocess.Popen(
        [sys.executable, str(DRIVER), case, str(tmp_path / case)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=75)
        except subprocess.TimeoutExpired:
            process.terminate()  # the driver's TERM handler unwinds its cleanup
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                kill_driver(process, tmp_path / case)
                stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"external deadline: {stdout}\n{stderr}")
    assert process.returncode == 0, (
        stdout
        + stderr
        + "\n".join(path.read_text() for path in (tmp_path / case).glob("engine-*.log"))
    )


def test_witnesses_reject_duplicate_publish_and_unauthorized_term(tmp_path: Path) -> None:
    """O, runner §7/DL-431: actual second publication and outsider TERM fail their assertions."""
    witness_checks(tmp_path)


def test_cleanup_ignores_malformed_evidence_and_unregistered_wrapper(tmp_path: Path) -> None:
    """O, DL-433: cleanup preserves the barrier failure and stops all owned descendants."""
    cleanup_checks(tmp_path)


def test_forced_timeout_stops_driver_group_and_detached_owned_processes(tmp_path: Path) -> None:
    """O, DL-433: the hard deadline kills the whole group and cookie/marker-owned escapees."""
    root = tmp_path / "timeout"
    process = subprocess.Popen(
        [sys.executable, str(DRIVER), "hang", str(root)], start_new_session=True
    )
    try:
        until(lambda: (root / "ready").exists())
        assert len(owned_processes(root)) >= 3
        process.terminate()
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(0.1)
        kill_driver(process, root)
        assert process.wait(5) == -signal.SIGKILL
        assert owned_processes(root) == {}
    finally:
        if process.poll() is None:
            kill_driver(process, root)
        process.wait(5)
        cleanup_owned(root)
