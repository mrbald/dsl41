"""Connected stop waits for the engine process, without lock-file access (DL-496)."""

from __future__ import annotations

import contextlib
import errno
import grp
import json
import os
import pwd
import subprocess
import socket
import struct
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import runner_control, runner_procid
from dsl41.cli import app
from dsl41.runner_procid import ProcessIdentity

pytestmark = pytest.mark.usefixtures("cli_env")


@contextlib.contextmanager
def gated_engine(base: Path):
    """Keep a real CLI engine alive after its store closes and its lock releases."""
    jil = base / "idle.jil"
    jil.write_text("insert_job: idle\njob_type: c\ncommand: sleep 600\n")
    roles = base / "roles.toml"
    roles.write_text(
        f'format_version = 1\nsocket_group = "{grp.getgrgid(os.getegid()).gr_name}"\n'
        f'[[binding]]\nsubject = "user:os/{pwd.getpwuid(os.geteuid()).pw_name}"\ntier = "adm"\n'
    )
    roles.chmod(0o600)
    root = base / "run"
    ready, release = base / "exiting", base / "release"
    bootstrap = """
import atexit, pathlib, runpy, sys, time
ready, release = map(pathlib.Path, sys.argv[1:3])
def exit_gate():
    ready.touch()
    while not release.exists():
        time.sleep(0.01)
atexit.register(exit_gate)
sys.argv = ['dsl41', 'run', sys.argv[3], '--run-root', sys.argv[4], '--access-map', sys.argv[5]]
runpy.run_module('dsl41', run_name='__main__')
"""
    with (base / "engine.log").open("w+") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                bootstrap,
                str(ready),
                str(release),
                str(jil),
                str(root),
                str(roles),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while "engine up; control socket:" not in (base / "engine.log").read_text():
                assert process.poll() is None, (base / "engine.log").read_text()
                assert time.monotonic() < deadline, (base / "engine.log").read_text()
                time.sleep(0.02)
            yield process, root / "control.sock", ready, release
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses chmod 000")
@pytest.mark.parametrize("unreadable", [False, True], ids=["owner-lock", "unreadable-lock"])
def test_connected_stop_waits_for_process_exit(short_root: Path, unreadable: bool) -> None:
    """000 stands for another uid's unreadable lock; peer auth still uses this test's uid."""
    with gated_engine(short_root) as (engine, path, exiting, release):
        assert runner_control.roundtrip(path, {"cmd": "status"})["ok"]
        lock = path.parent / "leader.lock"
        assert lock.stat().st_mode & 0o777 == 0o600
        if unreadable:
            lock.chmod(0)
            with pytest.raises(PermissionError):
                lock.open("rb")
        try:
            with subprocess.Popen(
                [sys.executable, "-m", "dsl41", "engine", "stop", "--wait", "--socket", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as client:
                try:
                    deadline = time.monotonic() + 15
                    while not exiting.exists():
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert engine.poll() is None
                    # Past socket close and lock release, the engine is still in atexit.
                    # DL-496: a free lock and closed socket do not prove process exit.
                    with pytest.raises(subprocess.TimeoutExpired):
                        client.wait(timeout=0.5)
                    release.touch()
                    stdout, stderr = client.communicate(timeout=15)
                    assert client.returncode == 0, (stdout, stderr)
                    assert engine.poll() == 0
                    with pytest.raises(ProcessLookupError):
                        os.kill(engine.pid, 0)
                finally:
                    release.touch()
                    if client.poll() is None:
                        client.kill()
                        client.wait()
        finally:
            lock.chmod(0o600)


@pytest.mark.parametrize("unreadable", [False, True], ids=["hidden-details", "failed-ps"])
def test_process_wait_keeps_a_foreign_pid_when_details_are_unreadable(monkeypatch, unreadable):
    def foreign(pid, sig):
        raise PermissionError("another uid")

    def look(pid):
        if unreadable:
            raise OSError("ps failed")
        return None

    monkeypatch.setattr(runner_procid.os, "kill", foreign)
    monkeypatch.setattr(runner_procid, "_proc_look", look)
    assert ProcessIdentity(123, "ticks:1").alive()
    assert ProcessIdentity(123, None).alive()


@pytest.mark.parametrize(
    "old,new",
    [
        ("ticks:1", "ticks:2"),
        ("lstart:Fri Nov 13 10:00:00 2026", "lstart:Fri Nov 13 10:00:03 2026"),
    ],
)
def test_process_wait_detects_a_reused_pid(monkeypatch, old, new):
    """A scripted new start token stands for PID reuse; no real pid allocator is forced."""
    monkeypatch.setattr(runner_procid.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(runner_procid, "_proc_look", lambda pid: (new, False))
    assert not ProcessIdentity(123, old).alive()
    assert ProcessIdentity(123, None).alive()  # pid-only fallback cannot detect reuse


def test_process_wait_recognizes_an_exited_unreaped_process(monkeypatch):
    monkeypatch.setattr(runner_procid.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(runner_procid, "_proc_look", lambda pid: ("ticks:1", True))
    assert not ProcessIdentity(123, "ticks:1").alive()


@pytest.mark.parametrize("second", [0, 1, 2])
def test_process_wait_tolerates_lstart_drift(monkeypatch, second):
    """DL-41a: at most two seconds of lstart drift is not proof of exit."""
    monkeypatch.setattr(runner_procid.os, "kill", lambda pid, sig: None)
    token = f"lstart:Fri Nov 13 10:00:0{second} 2026"
    monkeypatch.setattr(runner_procid, "_proc_look", lambda pid: (token, False))
    assert ProcessIdentity(123, "lstart:Fri Nov 13 10:00:00 2026").alive()
    assert ProcessIdentity(123, "lstart:Fri Nov 13 10:00:02 2026").alive()


def test_process_wait_recognizes_an_absent_pid(monkeypatch):
    def absent(pid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(runner_procid.os, "kill", absent)
    monkeypatch.setattr(runner_procid, "_proc_look", lambda pid: pytest.fail("pid is absent"))
    assert not ProcessIdentity(123, None).alive()


def test_peer_process_allows_an_unreadable_start_token(short_root, monkeypatch):
    with gated_engine(short_root) as (engine, path, _exiting, _release):

        def hidden(pid):
            raise PermissionError("hidden process")

        monkeypatch.setattr(runner_control, "proc_start_token", hidden)
        peers = []
        assert runner_control.roundtrip(path, {"cmd": "status"}, peer=peers)["ok"]
        assert peers == [ProcessIdentity(engine.pid, None)]


def test_peer_process_names_the_connected_engine(short_root):
    with gated_engine(short_root) as (engine, path, _exiting, _release):
        peers = []
        assert runner_control.roundtrip(path, {"cmd": "status"}, peer=peers)["ok"]
        assert len(peers) == 1 and peers[0].pid == engine.pid
        assert peers[0].start_token == runner_procid.proc_start_token(engine.pid)
        assert peers[0].alive()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses chmod 000")
@pytest.mark.parametrize("unreadable", [False, True], ids=["readable-lock", "unreadable-lock"])
@pytest.mark.parametrize("peer_pid", [0, -1, None], ids=["zero-pid", "negative-pid", "no-option"])
def test_stop_without_a_peer_pid_is_delivered_and_uses_the_lock(
    short_root, monkeypatch, unreadable, peer_pid
):
    """Host-only stand-in: unmapped namespace pid or absent option; the engine is real."""
    with gated_engine(short_root) as (engine, path, exiting, release):
        original = socket.socket.getsockopt
        readings = []

        def getsockopt(conn, level, option, *args):
            if hasattr(socket, "SO_PEERCRED"):
                process_option = (socket.SOL_SOCKET, socket.SO_PEERCRED)
            else:
                process_option = (0, 0x002)
            if (level, option) != process_option:
                return original(conn, level, option, *args)
            readings.append(peer_pid)
            if peer_pid is None:
                raise OSError(errno.ENOPROTOOPT, "no peer pid option")
            if hasattr(socket, "SO_PEERCRED"):
                return struct.pack("iII", peer_pid, os.geteuid(), os.getegid())
            return struct.pack("i", peer_pid)

        monkeypatch.setattr(socket.socket, "getsockopt", getsockopt)
        lock = path.parent / "leader.lock"
        if unreadable:
            lock.chmod(0)
        try:
            result = CliRunner().invoke(app, ["engine", "stop", "--wait", "--socket", str(path)])
            assert readings == [peer_pid]
            assert json.loads(result.stdout)["ok"]
            assert result.exit_code == (2 if unreadable else 0), result.output
            if unreadable:
                assert result.stderr == (
                    "dsl41: the stop was delivered, but --wait cannot confirm the exit because "
                    f"{lock} is not readable to this user\n"
                )
            else:
                assert result.stderr == ""
            deadline = time.monotonic() + 15
            while not exiting.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert engine.poll() is None
            release.touch()
            assert engine.wait(timeout=10) == 0
        finally:
            lock.chmod(0o600)
