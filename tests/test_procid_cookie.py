"""The process cookie's primitives: the scan of /proc (`runner_procid.ours`), the signal that
goes only to the process a scan found (`signal_ours`), the /proc group check, and the stop
ladder that signals an attempt's own processes (`runner_adapters.stop_ladder` with
`members`).

The platform arms run against a stand-in /proc tree and a stand-in `sys`, so they hold on
any host; the real-process twins run on Linux only, where the cookie is read."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dsl41 import runner_adapters
from dsl41 import runner_procid as procid

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the process cookie is read on Linux only"
)

RUN_ID = "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e4a10"
OTHER_ID = "0f6a8f4c-1c2d-4b5e-8f90-a1b2c3d4e5f6"


def _platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(procid, "sys", SimpleNamespace(platform=name))


def _stat(pid: int, state: str, pgrp: int, start: int, threads: int = 1) -> bytes:
    """A /proc/<pid>/stat line: field 3 the state, 5 the pgrp, 20 the threads, 22 the start."""
    fields = {5: str(pgrp), 20: str(threads), 22: str(start)}
    rest = " ".join(fields.get(n, "0") for n in range(4, 25))
    return f"{pid} (a b) c) {state} {rest}\n".encode("ascii")


def _proc(root: Path, pid: int, *, environ: bytes | None, stat: bytes | None) -> Path:
    entry = root / str(pid)
    entry.mkdir()
    if environ is not None:
        (entry / "environ").write_bytes(environ)
    if stat is not None:
        (entry / "stat").write_bytes(stat)
    return entry


def _env(**variables: str) -> bytes:
    return b"".join(f"{k}={v}".encode() + b"\0" for k, v in variables.items())


# ------------------------------------------------------------------- the scan


def test_the_scan_finds_each_process_carrying_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a pid directory of this uid whose `DSL41_COOKIE` starts with the prefix is found,
    with its run id and start token. One whose `environ` it may not read is counted: the
    kernel's shape for a non-dumpable process, its directory keeping the task's uid
    (`task_dump_owner`) and `environ` unreadable (Opus m1; the real one is
    `test_a_non_dumpable_process_is_counted_unreadable`). One gone meanwhile (no environ) and a
    zombie (no environment left) are not found."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    mine = procid.cookie("inst", RUN_ID)
    _proc(tmp_path, 11, environ=_env(HOME="/h", DSL41_COOKIE=mine), stat=_stat(11, "S", 11, 900))
    _proc(tmp_path, 12, environ=_env(DSL41_COOKIE="other." + OTHER_ID), stat=_stat(12, "S", 9, 5))
    _proc(tmp_path, 13, environ=_env(HOME="/h"), stat=_stat(13, "S", 11, 7))
    _proc(tmp_path, 14, environ=b"", stat=_stat(14, "Z", 11, 8))  # a zombie
    _proc(tmp_path, 15, environ=None, stat=None)  # gone between the listing and the read
    (tmp_path / "self").mkdir()
    unreadable = _proc(tmp_path, 17, environ=_env(DSL41_COOKIE=mine), stat=_stat(17, "S", 1, 1))
    (unreadable / "environ").chmod(0)
    try:
        scan = procid.ours("inst.")
    finally:
        (unreadable / "environ").chmod(0o600)
    assert scan is not None
    assert scan.pids == {11: (RUN_ID, "ticks:900")}
    assert scan.skipped == (0 if os.geteuid() == 0 else 1)
    other = procid.ours("other.")
    assert other is not None and other.pids == {12: (OTHER_ID, "ticks:5")}
    other_uid = os.getuid() + 1
    monkeypatch.setattr(procid.os, "getuid", lambda: other_uid)
    stranger = procid.ours("")
    assert stranger is not None and stranger.pids == {}  # another uid's processes


@pytest.mark.parametrize("where", ["directory", "environ", "stat"])
def test_a_read_that_fails_for_another_reason_cuts_the_scan_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Opus M3, Astra B3: only ENOENT and ESRCH mean a process ended while it was read. Any
    other failure (EMFILE here) cuts the scan short with its errno, so its caller knows
    nothing from it; ESRCH at the same place is a process gone."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    mine = procid.cookie("inst", RUN_ID)
    _proc(tmp_path, 31, environ=_env(DSL41_COOKIE=mine), stat=_stat(31, "S", 31, 3))
    real_open = os.open
    failure = [errno.EMFILE]

    def failing(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        name = os.fspath(path)
        if name == (str(tmp_path / "31") if where == "directory" else where):
            raise OSError(failure[0], os.strerror(failure[0]))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(procid.os, "open", failing)
    failed = procid.ours("inst.")
    assert failed is not None and (failed.pids, failed.error) == ({}, errno.EMFILE)
    failure[0] = errno.ESRCH
    gone = procid.ours("inst.")
    assert gone is not None and (gone.pids, gone.error) == ({}, None)


def test_a_foreign_process_hidden_by_hidepid_is_not_a_failed_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus r3-M1: under `hidepid=noaccess` (systemd `ProtectProc=noaccess`) another uid's
    /proc/<pid> cannot be opened (EPERM). This uid can always open its own entries, so the
    denial means a process that is not ours: skipped and counted, never a failed scan, and our
    own cookie process is still found."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    mine = procid.cookie("inst", RUN_ID)
    _proc(tmp_path, 11, environ=_env(DSL41_COOKIE=mine), stat=_stat(11, "S", 11, 900))
    _proc(tmp_path, 12, environ=_env(HOME="/h"), stat=_stat(12, "S", 12, 5))
    real_open = os.open

    def hidden(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == str(tmp_path / "12"):
            raise PermissionError(errno.EPERM, "hidepid")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(procid.os, "open", hidden)
    scan = procid.ours("inst.")
    assert scan is not None
    assert (scan.pids, scan.skipped, scan.error) == ({11: (RUN_ID, "ticks:900")}, 1, None)


@pytest.mark.parametrize(
    ("error", "members"),
    [(errno.EIO, None), (errno.EMFILE, None), (errno.ENOENT, {}), (errno.EACCES, {})],
    ids=["eio", "emfile", "gone", "foreign"],
)
def test_the_group_reader_classifies_a_stat_error_as_the_scan_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int, members: Any
) -> None:
    """Astra M4: one classification for both readers (`proc_entry_error`). An EIO or EMFILE
    on a member's stat is a failed group reading (None), never a member gone; ENOENT is a
    process gone and EACCES one that is not ours, both skipped."""
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    _proc(tmp_path, 51, environ=None, stat=_stat(51, "S", 40, 1))

    def failing(path: Any, *args: Any, **kwargs: Any) -> Any:
        raise OSError(error, os.strerror(error))

    monkeypatch.setattr(procid, "open", failing, raising=False)
    assert procid.linux_group_pids(40) == members
    assert procid.linux_group_has_live_member(40) is (members is None)
    assert procid._linux_stat_fields(51) is None  # a token check reads it as not the process


@linux_only
def test_a_non_dumpable_process_is_counted_unreadable(tmp_path: Path) -> None:
    """Opus m1, confirmed on the kernel: a process that calls `prctl(PR_SET_DUMPABLE, 0)`
    keeps this uid on its /proc directory, its `environ` read is refused, and the scan counts
    it in `skipped` instead of finding it."""
    ready = tmp_path / "ready"
    program = (
        "import ctypes,time;ctypes.CDLL(None).prctl(4,0,0,0,0);"
        f"open({str(ready)!r},'w').write('1');time.sleep(30)"
    )
    mine = procid.cookie("dumpable", RUN_ID)
    child = subprocess.Popen(
        [sys.executable, "-c", program], env={**os.environ, procid.COOKIE_ENV: mine}
    )
    try:
        _until_file(ready)
        assert os.stat(f"/proc/{child.pid}").st_uid == os.getuid()
        scan = procid.ours("dumpable.")
        assert scan is not None and child.pid not in scan.pids and scan.skipped >= 1
    finally:
        child.kill()
        child.wait()


def _until_file(path: Path) -> None:
    """Wait until a child wrote a non-empty marker: readiness comes from the child."""
    deadline = time.monotonic() + 10
    while not (path.exists() and path.read_text().strip()):
        assert time.monotonic() < deadline, f"{path} never written"
        time.sleep(0.01)


def test_the_scan_is_unavailable_off_linux_and_without_proc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _platform(monkeypatch, "darwin")
    assert procid.ours("") is None
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "_PROC", str(tmp_path / "absent"))
    unlisted = procid.ours("")
    assert unlisted is not None and unlisted.error == errno.ENOENT


def test_the_proc_group_check_ignores_zombies_and_other_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner note 1: the group check reads /proc instead of parsing `ps`. A zombie member is
    no live member, a process whose main thread exited while a worker runs is (Astra B1);
    a /proc that cannot be listed proves nothing gone."""
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    _proc(tmp_path, 21, environ=None, stat=_stat(21, "Z", 40, 1))
    _proc(tmp_path, 22, environ=None, stat=_stat(22, "S", 41, 1))
    _proc(tmp_path, 23, environ=None, stat=None)  # gone meanwhile
    (tmp_path / "net").mkdir()
    assert not procid.linux_group_has_live_member(40)
    assert procid.linux_group_has_live_member(41)
    assert not procid.linux_group_has_live_member(42)
    _proc(tmp_path, 24, environ=None, stat=_stat(24, "Z", 41, 9, threads=2))  # main thread gone
    assert procid.linux_group_pids(41) == {22: "ticks:1", 24: "ticks:9"}
    monkeypatch.setattr(procid, "_PROC", str(tmp_path / "absent"))
    assert procid.linux_group_has_live_member(40)
    assert procid.linux_group_pids(40) is None


def test_the_adapter_reads_proc_for_the_group_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def look(pgid: int) -> bool:
        seen.append(pgid)
        return True

    monkeypatch.setattr(runner_adapters, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(procid, "linux_group_has_live_member", look)
    assert runner_adapters.group_has_live_member(77) and seen == [77]


def test_the_adapter_lists_the_groups_with_ps_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = subprocess.CompletedProcess(["ps"], 0, stdout="   40 Z\n   41 S+\n", stderr="")
    monkeypatch.setattr(runner_adapters, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(runner_adapters.subprocess, "run", lambda *_a, **_k: listing)
    assert not runner_adapters.group_has_live_member(40)  # a zombie only
    assert runner_adapters.group_has_live_member(41)
    assert not runner_adapters.group_has_live_member(42)


@linux_only
def test_the_scan_finds_a_real_process_and_its_children() -> None:
    """A process started with a cookie and a child it forks are both found, with their real
    start tokens; a process without the cookie is not."""
    mine = procid.cookie("realscan", RUN_ID)
    env = {**os.environ, procid.COOKIE_ENV: mine}
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess;child=subprocess.Popen(['sleep','30']);"
            "print(child.pid,flush=True);child.wait()",
        ],
        env=env,
        stdout=-1,
    )
    stranger = subprocess.Popen(["sleep", "30"])
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        scan = procid.ours("realscan.")
        assert scan is not None
        assert set(scan.pids) == {parent.pid, child}
        assert scan.pids[child] == (RUN_ID, procid.proc_start_token(child))
    finally:
        os.kill(child, signal.SIGKILL)
        for proc in (parent, stranger):
            proc.kill()
            proc.wait()
        assert parent.stdout is not None
        parent.stdout.close()


# ------------------------------------------------------------------- the signal


@linux_only
@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_a_signal_goes_only_to_the_process_the_scan_found(
    monkeypatch: pytest.MonkeyPatch, pidfd: bool
) -> None:
    """Both primitives recheck the start token: a mismatch and a gone pid get nothing;
    the matching process gets the signal (DL-436)."""
    supported = procid.pidfd_supported()
    if pidfd and not supported:
        pytest.skip("this Python or kernel has no pidfd")
    monkeypatch.setattr(procid, "pidfd_supported", lambda: pidfd)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        token = None
        while token is None:
            token = procid.proc_start_token(proc.pid)
        assert not procid.signal_ours(proc.pid, "ticks:1", signal.SIGKILL)
        assert proc.poll() is None
        assert procid.signal_ours(proc.pid, token, signal.SIGKILL)
        assert proc.wait(5) == -signal.SIGKILL
        assert not procid.signal_ours(proc.pid, token, signal.SIGKILL)  # reaped: gone
    finally:
        proc.kill()
        proc.wait()


@linux_only
@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_a_process_that_ends_before_its_signal_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
    pidfd: bool,
) -> None:
    if pidfd and not procid.pidfd_supported():
        pytest.skip("this Python or kernel has no pidfd")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        token = procid.proc_start_token(proc.pid)
        assert token is not None

        def gone(*_args: Any) -> None:
            raise ProcessLookupError

        monkeypatch.setattr(procid, "pidfd_supported", lambda: pidfd)
        if pidfd:
            monkeypatch.setattr(procid.signal, "pidfd_send_signal", gone)
        else:
            monkeypatch.setattr(procid.os, "kill", gone)
        assert not procid.signal_ours(proc.pid, token, signal.SIGTERM)
    finally:
        monkeypatch.undo()
        proc.kill()
        proc.wait()


# ------------------------------------------------------------------- the ladder


class _Signals:
    """Stand-ins for the ladder's signals, keeping what was sent in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[tuple[str, int, int]] = []
        monkeypatch.setattr(procid, "killpg_quiet", lambda g, s: self.sent.append(("pg", g, s)))
        monkeypatch.setattr(procid, "signal_ours", self._one)
        monkeypatch.setattr(runner_adapters, "SPOOL_POLL_S", 0.0)

    def _one(self, pid: int, start: str, sig: int, **_kwargs: Any) -> bool:
        self.sent.append(("pid", pid, sig))
        return True


TERM, KILL = signal.SIGTERM, signal.SIGKILL


def test_the_ladder_signals_each_process_once_per_step_and_never_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each step reads what is left afresh and signals every process by itself, once; no group
    signal goes. The wait watches the processes the TERM reached by their start tokens and
    ends once they are gone; one forked after the TERM's reading gets the KILL's."""
    signals = _Signals(monkeypatch)
    alive = {"t7": True, "t8": True}
    looks: list[int] = []

    def verify(_pid: int, token: str) -> bool:
        looks.append(1)
        alive["t8"] = len(looks) < 3  # 8 ends inside the grace
        return alive[token]

    monkeypatch.setattr(procid, "verify_alive", verify)
    readings = iter([{7: "t7", 8: "t8"}, {7: "t7", 9: "t9"}])
    asyncio.run(runner_adapters.stop_ladder(70, "t", 0.0, members=lambda: next(readings)))
    assert signals.sent == [("pid", 7, TERM), ("pid", 8, TERM), ("pid", 7, KILL), ("pid", 9, KILL)]
    signals.sent.clear()
    alive["t7"] = False
    readings = iter([{7: "t7"}, {}])
    asyncio.run(runner_adapters.stop_ladder(70, "t", 30.0, members=lambda: next(readings)))
    assert signals.sent == [("pid", 7, TERM)]  # gone inside the grace: nothing to KILL


@pytest.mark.parametrize("move", ["out-of-the-group", "into-the-group"])
def test_a_process_that_moves_between_groups_gets_one_term_and_one_kill(
    monkeypatch: pytest.MonkeyPatch, move: str
) -> None:
    """Astra M2, M3: signals go by process, so a move between the reading and the signal, out
    of the recorded group (`setsid`) or into it, changes nothing: one TERM, then one KILL."""
    signals = _Signals(monkeypatch)
    group = {8: 7 if move == "out-of-the-group" else 8}
    monkeypatch.setattr(procid, "verify_alive", lambda *_: False)

    def reading() -> dict[int, str]:
        group[8] = 8 if move == "out-of-the-group" else 7  # it moves after this reading
        return {8: "t8"}

    asyncio.run(runner_adapters.stop_ladder(7, "t", 0.0, members=reading))
    assert signals.sent == [("pid", 8, TERM), ("pid", 8, KILL)]


def test_the_ladder_without_members_is_the_group_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no cookie to read, a gone leader ends the ladder at once, as before it."""
    signals = _Signals(monkeypatch)
    monkeypatch.setattr(procid, "verify_alive", lambda *_: False)
    asyncio.run(runner_adapters.stop_ladder(7, "t", 0.0))
    assert signals.sent == []


# ------------------------------------------------------------------- the pidfd gate


def test_the_probe_selects_pidfd_or_token_checked_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing APIs and ENOSYS select os.kill; a signal refused on an available pidfd
    never retries by pid (DL-436)."""
    sent: list[int] = []
    killed: list[tuple[int, int]] = []
    fake_os = SimpleNamespace(
        pidfd_open=lambda _pid: 3,
        getpid=os.getpid,
        close=lambda _fd: None,
        getuid=os.getuid,
        kill=lambda pid, sig: killed.append((pid, sig)),
    )
    fake_signal = SimpleNamespace(pidfd_send_signal=lambda _fd, sig: sent.append(sig))
    monkeypatch.setattr(procid, "os", fake_os)
    monkeypatch.setattr(procid, "signal", fake_signal)
    monkeypatch.setattr(procid, "proc_start_token", lambda _pid: "t")
    assert procid.pidfd_supported()
    assert procid.signal_ours(5, "t", 9) and sent == [9]
    assert not procid.signal_ours(5, "other", 9)  # no longer the process the scan found

    def refused(_fd: int, _sig: int) -> None:
        raise PermissionError(errno.EPERM, "setuid")

    fake_signal.pidfd_send_signal = refused
    assert not procid.signal_ours(5, "t", 9)
    assert killed == []

    def no_pidfd(_pid: int) -> int:
        raise OSError(errno.ENOSYS, "no pidfd")

    fake_os.pidfd_open = no_pidfd
    assert not procid.pidfd_supported()
    assert not procid.signal_ours(5, "t", 9, pidfd=True)
    assert procid.signal_ours(5, "t", 9)
    del fake_os.pidfd_open
    assert not procid.pidfd_supported()
    assert not procid.signal_ours(5, "t", 9, pidfd=True)
    assert procid.signal_ours(5, "t", 9)
    fake_os.pidfd_open = lambda _pid: 3
    del fake_signal.pidfd_send_signal
    assert not procid.pidfd_supported()
    assert not procid.signal_ours(5, "t", 9, pidfd=True)
    assert procid.signal_ours(5, "t", 9)
    assert sent == [9]
    assert killed == [(5, 9)] * 3
    fake_os.kill = refused
    assert not procid.signal_ours(5, "t", 9)


@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
@pytest.mark.parametrize("new_start", [901, None], ids=["reused", "gone"])
def test_a_token_changed_after_the_scan_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pidfd: bool, new_start: int | None
) -> None:
    """A scan's token must still match at the signal, for either primitive (DL-436)."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "_PROC", str(tmp_path))
    entry = _proc(
        tmp_path, 31, environ=_env(DSL41_COOKIE="inst." + RUN_ID), stat=_stat(31, "S", 31, 900)
    )
    scan = procid.ours("inst.")
    assert scan is not None and scan.pids == {31: (RUN_ID, "ticks:900")}
    if new_start is None:
        (entry / "stat").unlink()
    else:
        (entry / "stat").write_bytes(_stat(31, "S", 31, new_start))
    sent: list[tuple[int, int]] = []
    closed: list[int] = []
    monkeypatch.setattr(
        procid,
        "os",
        SimpleNamespace(
            pidfd_open=lambda _pid: 7,
            close=closed.append,
            kill=lambda pid, sig: sent.append((pid, sig)),
        ),
    )
    monkeypatch.setattr(
        procid, "signal", SimpleNamespace(pidfd_send_signal=lambda fd, sig: sent.append((fd, sig)))
    )
    monkeypatch.setattr(procid, "pidfd_supported", lambda: pidfd)
    assert not procid.signal_ours(31, scan.pids[31][1], signal.SIGTERM)
    assert sent == []
    assert closed == ([7] if pidfd else [])


@linux_only
@pytest.mark.parametrize("pidfd", [True, False], ids=["pidfd", "no-pidfd"])
def test_the_ladder_stops_a_setsid_escape_that_ignores_term(
    monkeypatch: pytest.MonkeyPatch, pidfd: bool
) -> None:
    """A real `setsid` child carrying the cookie and ignoring TERM is outside the leader's
    group, so a group kill misses it; the ladder finds it by its cookie and KILLs it."""
    if pidfd and not procid.pidfd_supported():
        pytest.skip("this Python or kernel has no pidfd")
    monkeypatch.setattr(procid, "pidfd_supported", lambda: pidfd)
    mine = procid.cookie("realladder", RUN_ID)
    escape = (
        "import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print(os.getpid(),flush=True);time.sleep(30)"
    )
    env = {**os.environ, procid.COOKIE_ENV: mine}
    child = subprocess.Popen([sys.executable, "-c", escape], env=env, stdout=-1)
    try:
        assert child.stdout is not None and int(child.stdout.readline()) == child.pid

        def members() -> dict[int, str]:
            scan = procid.ours(mine)
            assert scan is not None
            return {pid: start for pid, (_, start) in scan.pids.items()}

        asyncio.run(runner_adapters.stop_ladder(2**22 + 3, "ticks:1", 0.3, members=members))
        assert child.wait(5) == -signal.SIGKILL
    finally:
        child.kill()
        child.wait()
        assert child.stdout is not None
        child.stdout.close()
