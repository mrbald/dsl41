"""Injected kernel boot identity: wrapper-protocol §3 and restart under DL-433/436.

Only the procfs read is redirected. No production setting or reboot is involved.
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from store_surgery import rows
from test_restart import T0, _close, _cookied, _jil, _open, _restart, _set_cookie, _write

from dsl41 import runner_procid, runner_stops
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_startup import start_run


@pytest.fixture
def boot_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect only the kernel boot-ID file; process stat and cookie reads stay real."""
    path = tmp_path / "boot_id"
    path.write_text("boot-a\n", encoding="ascii")

    def kernel_open(name: Any, *args: Any, **kwargs: Any) -> Any:
        if name == "/proc/sys/kernel/random/boot_id":
            name = path
        return builtins.open(name, *args, **kwargs)

    monkeypatch.setattr(runner_procid, "open", kernel_open, raising=False)
    return path


@pytest.mark.parametrize("changed", [False, True], ids=["same-boot", "changed-boot"])
def test_boot_identity_precedes_liveness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boot_file: Path, changed: bool
) -> None:
    """A boot mismatch voids even a matching leader token (wrapper-protocol §3)."""
    checked: list[int] = []

    def alive(pid: int, _token: str) -> bool:
        checked.append(pid)
        return True

    monkeypatch.setattr(runner_procid, "verify_alive", alive)
    if changed:
        boot_file.write_text("boot-b\n", encoding="ascii")

    async def scenario() -> None:
        engine = start_run(
            lower_source(_jil("j")),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        try:
            engine.stops.note_boot(
                {"command_pgid": 12345, "command_start_time": "ticks:7", "boot_id": "boot-a"}
            )
            assert engine.stops.boot_id == runner_procid.current_boot_id()
            assert engine.stops.boot_id == ("boot-b" if changed else "boot-a")
            assert engine.stops.command_alive(12345, "ticks:7") is (not changed)
            assert checked == ([] if changed else [12345])
        finally:
            await _close(engine)

    asyncio.run(scenario())


def test_changed_boot_keeps_holds_and_run_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boot_file: Path
) -> None:
    """DL-433: gone attempts rerun or hold; durable holds and invocation survive."""
    run_root = tmp_path / "run"
    text = _jil("rerun", "marked", "held")
    roots: list[str] = []

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"marked": "synthetic non-idempotent effect"},
        )
        try:
            roots.append(engine.root_id)
            for job, kind in (
                ("held", "ON_HOLD"),
                ("rerun", "STARTJOB"),
                ("marked", "STARTJOB"),
            ):
                engine.inject(Event(at=T0, kind=kind, payload={"job": job}))
            await engine.run_until_quiescent(T0)
        finally:
            await _close(engine)

    asyncio.run(first())
    dispatches = [b for _, k, b in rows(run_root) if k == "dispatch"]
    assert [b["job"] for b in dispatches] == ["rerun", "marked"]
    for pid, dispatch in enumerate(dispatches, 1_000_000_000):
        _write(
            dispatch,
            "spawn.json",
            command_pid=pid,
            command_pgid=pid,
            command_start_time="ticks:7",
            boot_id=runner_procid.current_boot_id(),
        )
    boot_file.write_text("boot-b\n", encoding="ascii")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("an earlier boot must not cause a liveness check or signal")

    monkeypatch.setattr(runner_procid, "verify_alive", forbidden)
    monkeypatch.setattr(runner_procid, "linux_group_pids", forbidden)
    monkeypatch.setattr(runner_procid, "signal_ours", forbidden)
    monkeypatch.setattr(runner_procid, "killpg_quiet", forbidden)
    monkeypatch.setattr(runner_stops, "group_has_live_member", forbidden)
    engine = _restart(run_root, text)
    assert engine.root_id == roots[0]
    assert engine.oracle.store.job["held"].on_hold
    assert engine.oracle.store.job["marked"].on_hold
    assert engine.oracle.store.job["marked"].status == "FAILURE"
    assert engine.evidence["marked"]["cause"] == "engine_death"
    outcomes = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert [(b["job"], b["outcome"], b["cause"]) for b in outcomes] == [
        ("rerun", "unobservable", "engine_death"),
        ("marked", "unobservable", "engine_death"),
    ]
    assert all("ladder" not in b for b in outcomes)
    plans = [b for _, k, b in rows(run_root) if k == "follow_up" and b["event"] == "plan"]
    assert [(b["job"], b["plan"]) for b in plans] == [("rerun", "rerun"), ("marked", "hold")]
    [replacement] = [b for _, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2]
    original = dispatches[0]
    assert (replacement["job"], replacement["run_number"], replacement["after"]) == (
        "rerun",
        1,
        "engine_death",
    )
    assert replacement["attempt"] != original["attempt"]
    for name in ("DSL41_JOB", "DSL41_RUN_NUMBER", "DSL41_SCHEDULED_AT"):
        assert replacement["env"][name] == original["env"][name]


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="real procfs cookie, start-tick and pidfd checks require Linux; macOS uses ps/sysctl",
)
@pytest.mark.parametrize("pidfd", [False, True], ids=["os-kill", "pidfd"])
@pytest.mark.parametrize("present", [False, True], ids=["missing-pid", "live-reused-pid"])
@pytest.mark.parametrize("boots", ["same", "changed", "mixed"])
def test_restart_with_records_from_two_boots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boot_file: Path,
    pidfd: bool,
    present: bool,
    boots: str,
) -> None:
    """DL-433/436: only current-boot leftovers are checked or signalled, by either primitive.

    A live child deliberately matches the entire recorded pid/start tuple and cookie;
    the injected new boot alone makes that apparent identity collision irrelevant.
    """
    if pidfd and not runner_procid.pidfd_supported():
        pytest.skip("this Linux kernel does not support the pidfd signalling primitive")
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: pidfd)
    run_root = tmp_path / "run"
    text = _jil("a", "b")
    dispatches = _open(run_root, text, ["a", "b"])
    _set_cookie(run_root)
    children: list[subprocess.Popen[bytes]] = []
    pids: list[int] = []
    previous: set[int] = set()
    sent: list[tuple[int, int]] = []
    checked: list[int] = []
    verify = runner_procid.verify_alive
    send = runner_procid.signal_ours

    def verify_current(pid: int, token: str) -> bool:
        checked.append(pid)
        assert pid not in previous, "liveness from another boot is void"
        return verify(pid, token)

    def signal_current(pid: int, token: str, sig: int, **kwargs: Any) -> bool:
        sent.append((pid, sig))
        assert pid not in previous, "never signal a recorded pid from another boot"
        return send(pid, token, sig, **kwargs)

    try:
        for index, dispatch in enumerate(dispatches):
            if present:
                child = _cookied(run_root, dispatch["attempt"], "exec sleep 60")
                children.append(child)
                pid = child.pid
                token = runner_procid.proc_start_token(pid)
                assert token is not None and verify(pid, token)
            else:
                pid, token = 1_000_000_000 + index, "ticks:7"
                assert runner_procid.proc_start_token(pid) is None
            pids.append(pid)
            old = boots == "changed" or (boots == "mixed" and index == 0)
            if old:
                previous.add(pid)
            _write(
                dispatch,
                "spawn.json",
                command_pid=pid,
                command_pgid=pid,
                command_start_time=token,
                wrapper_pid=1,
                wrapper_start_time="ticks:1",
                boot_id="boot-a" if old else "boot-b",
            )
        boot_file.write_text("boot-b\n", encoding="ascii")
        monkeypatch.setattr(runner_procid, "verify_alive", verify_current)
        monkeypatch.setattr(runner_procid, "signal_ours", signal_current)
        engine = _restart(run_root, text)
        assert engine.stops.boot_id == "boot-b"
        current = set(pids) - previous
        assert set(checked) == current
        assert {pid for pid, _sig in sent} == (current if present else set())
        assert all(sig in (signal.SIGTERM, signal.SIGKILL) for _pid, sig in sent)
        for child in children:
            if child.pid in previous:
                assert child.poll() is None
            else:
                assert child.wait(timeout=5) == -signal.SIGTERM
        outcomes = [b for _, k, b in rows(run_root) if k == "outcome"]
        assert [(b["job"], b["outcome"], b["cause"]) for b in outcomes] == [
            ("a", "unobservable", "engine_death"),
            ("b", "unobservable", "engine_death"),
        ]
        replacements = [b for _, k, b in rows(run_root) if k == "dispatch" and b["ordinal"] == 2]
        assert [(b["job"], b["run_number"]) for b in replacements] == [("a", 1), ("b", 1)]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("error", [errno.ENOENT, errno.EACCES])
@pytest.mark.parametrize("fallback", ["sysctl", "unknown", "missing-sysctl"])
def test_unreadable_boot_id_keeps_the_existing_fallback(
    monkeypatch: pytest.MonkeyPatch, error: int, fallback: str
) -> None:
    """Read errors try sysctl; no usable reply gives unknown; exec failure propagates."""

    def unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(error, "synthetic boot-ID read error")

    def sysctl(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"]
        if fallback == "missing-sysctl":
            raise FileNotFoundError(errno.ENOENT, "synthetic missing sysctl")
        return subprocess.CompletedProcess(argv, 0 if fallback == "sysctl" else 1, " boot-b\n")

    monkeypatch.setattr(runner_procid, "open", unreadable, raising=False)
    monkeypatch.setattr(runner_procid.subprocess, "run", sysctl)
    if fallback == "missing-sysctl":
        with pytest.raises(FileNotFoundError, match="synthetic missing sysctl"):
            runner_procid.current_boot_id()
    else:
        assert runner_procid.current_boot_id() == ("boot-b" if fallback == "sysctl" else "unknown")
