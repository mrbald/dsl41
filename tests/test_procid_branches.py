"""Branch tests for `dsl41.runner_procid` that the lifecycle and supervisor
suites do not reach (DL-72, DL-210, DL-265).

Normative spec: `docs/supervisor-protocol.md` (the durable records and the
(pid, start-time) identity guard) and DL-41a (the durability liturgy). The
platform arms are driven by faking `sys.platform`, `/proc` and `ps`, so the
numbers do not depend on which host runs the suite. Each refusal has a twin
that does not trigger it.
"""

from __future__ import annotations

import io
import errno
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dsl41 import runner_procid as procid

# ------------------------------------------------------------------ helpers


def _platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The module reads `sys.platform` only; a stand-in `sys` keeps the real
    one untouched for pytest and coverage."""
    monkeypatch.setattr(procid, "sys", SimpleNamespace(platform=name))


def _stat_line(pid: int, comm: str, state: str, start: str, threads: str = "1") -> bytes:
    """A /proc/<pid>/stat line. Field 3 is the state, field 20 the thread
    count, field 22 the start ticks; the other fields from 4 to 21 are
    filler."""
    filler = " ".join(threads if n == 20 else "0" for n in range(4, 22))
    return f"{pid} ({comm}) {state} {filler} {start} 0 0\n".encode("ascii")


def _fake_open(content: bytes | str | None) -> Any:
    """An `open` replacement: `None` raises like a missing file."""

    def opener(path: str, mode: str = "r", encoding: str | None = None) -> Any:
        if content is None:
            raise FileNotFoundError(path)
        if isinstance(content, bytes):
            return io.BytesIO(content)
        return io.StringIO(content)

    return opener


def _wait_for(predicate: Any, timeout_s: float = 10.0) -> None:
    """Poll until `predicate()` is truthy. Loud on timeout."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")
        time.sleep(0.02)


def _fake_run(returncode: int, stdout: str) -> Any:
    def run(argv: list[str], **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run


# ------------------------------------------------------------ mkdir_durable


def _recording_os(synced: list[str], exists: Any) -> Any:
    """The module's `os`, with `os.open` recording the directories it opens
    (each is then fsynced) and `os.path.exists` replaced."""
    real_open = os.open

    class RecordingOs:
        path = SimpleNamespace(exists=exists, abspath=os.path.abspath, dirname=os.path.dirname)

        def __getattr__(self, name: str) -> Any:
            return getattr(os, name)

        @staticmethod
        def open(path: str, flags: int, *args: Any) -> int:
            synced.append(str(path))
            return real_open(path, flags, *args)

    return RecordingOs()


def test_procid_mkdir_durable_stops_at_a_filesystem_root_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing existing all the way to `/`, the ancestor probe stops at
    the root instead of looping, and every level from the new directory up to
    the root is fsynced. The twin is the ordinary call: it stops at the first
    existing ancestor and never reaches the root."""
    synced: list[str] = []
    target = tmp_path / "a" / "b"
    monkeypatch.setattr(procid, "os", _recording_os(synced, lambda _p: False))
    procid.mkdir_durable(str(target))
    assert target.is_dir()
    assert synced[:2] == [str(target), str(target.parent)]
    assert synced[-1] == "/" and str(tmp_path) in synced

    synced.clear()
    other = tmp_path / "c" / "d"
    monkeypatch.setattr(procid, "os", _recording_os(synced, os.path.exists))
    procid.mkdir_durable(str(other))
    assert other.is_dir()
    assert synced == [str(other), str(other.parent), str(tmp_path)]


# ------------------------------------------------------------ durable_write


def test_procid_durable_write_cleans_its_temp_even_when_the_failure_took_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename removes the temp file and re-raises. If the temp is
    already gone, the cleanup tolerates that and the original error still
    surfaces; nothing is published either way."""
    target = tmp_path / "rec.json"

    def rename_fails(src: str, dst: str) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(procid.os, "rename", rename_fails)
    with pytest.raises(OSError, match="rename refused"):
        procid.durable_write(str(target), b"x")
    assert sorted(p.name for p in tmp_path.iterdir()) == []

    def rename_loses_temp(src: str, dst: str) -> None:
        os.unlink(src)
        raise OSError("temp vanished")

    monkeypatch.setattr(procid.os, "rename", rename_loses_temp)
    with pytest.raises(OSError, match="temp vanished"):
        procid.durable_write(str(target), b"x")
    assert sorted(p.name for p in tmp_path.iterdir()) == []


# ---------------------------------------------------------- current_boot_id


def test_procid_boot_id_prefers_proc_then_sysctl_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kernel file wins when readable; else `sysctl` when it answers; else
    the literal `unknown`. A zero exit with empty output is not an answer."""
    monkeypatch.setattr(procid, "open", _fake_open("boot-uuid-1\n"), raising=False)
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(1, ""))
    assert procid.current_boot_id() == "boot-uuid-1"

    monkeypatch.setattr(procid, "open", _fake_open(None), raising=False)
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "  sess-2\n"))
    assert procid.current_boot_id() == "sess-2"

    monkeypatch.setattr(procid.subprocess, "run", _fake_run(1, "sess-3\n"))
    assert procid.current_boot_id() == "unknown"
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "  \n"))
    assert procid.current_boot_id() == "unknown"


# --------------------------------------------------------- proc_start_token


def test_procid_start_token_linux_reads_field_22_after_the_last_paren(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comm with spaces and parens must not shift the fields. A missing
    /proc entry is None, not an error."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(
        procid, "open", _fake_open(_stat_line(7, "a (b) c", "S", "4242")), raising=False
    )
    assert procid.proc_start_token(7) == "ticks:4242"
    monkeypatch.setattr(procid, "open", _fake_open(None), raising=False)
    assert procid.proc_start_token(7) is None


def test_procid_start_token_macos_reads_lstart_and_none_when_ps_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "Mon Jan  1 00:00:00 2001\n"))
    assert procid.proc_start_token(7) == "lstart:Mon Jan  1 00:00:00 2001"
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(1, "Mon Jan  1 00:00:00 2001\n"))
    assert procid.proc_start_token(7) is None
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "\n"))
    assert procid.proc_start_token(7) is None


# ------------------------------------------------------------ proc_is_zombie


def test_procid_zombie_linux_is_the_state_field_after_the_last_paren(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Z` is a zombie, any other state is not, a missing entry is not, and a
    stat line with nothing after the comm is not (no state to read)."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "open", _fake_open(_stat_line(9, "x) (Z", "Z", "1")), raising=False)
    assert procid.proc_is_zombie(9) is True
    monkeypatch.setattr(procid, "open", _fake_open(_stat_line(9, "x", "S", "1")), raising=False)
    assert procid.proc_is_zombie(9) is False
    monkeypatch.setattr(procid, "open", _fake_open(None), raising=False)
    assert procid.proc_is_zombie(9) is False
    monkeypatch.setattr(procid, "open", _fake_open(b"9 (x)"), raising=False)
    assert procid.proc_is_zombie(9) is False


def test_procid_zombie_macos_is_a_stat_that_starts_with_z(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "Z+\n"))
    assert procid.proc_is_zombie(9) is True
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "S\n"))
    assert procid.proc_is_zombie(9) is False
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(1, "Z\n"))
    assert procid.proc_is_zombie(9) is False


# ----------------------------------------------- token grammar and comparison


def test_procid_valid_start_token_rejects_non_strings_and_bad_forms() -> None:
    assert procid.valid_start_token(None) is False
    assert procid.valid_start_token(12) is False
    assert procid.valid_start_token("ticks:0") is True
    assert procid.valid_start_token("ticks:007") is False
    assert procid.valid_start_token("lstart:Mon Jan  1 00:00:00 2001") is True
    assert procid.valid_start_token("lstart:not a date") is False
    assert procid.valid_start_token("other:1") is False


def test_procid_start_tokens_match_by_kind() -> None:
    """Tick tokens match exactly; lstart tokens within the tolerance; a
    foreign or unparseable token never matches."""
    assert procid.start_tokens_match("ticks:1", "ticks:1") is True
    assert procid.start_tokens_match("ticks:1", "ticks:2") is False
    assert procid.start_tokens_match("ticks:1", "lstart:Mon Jan  1 00:00:00 2001") is False
    base = "lstart:Mon Jan  1 00:00:00 2001"
    near = "lstart:Mon Jan  1 00:00:02 2001"
    far = "lstart:Mon Jan  1 00:00:03 2001"
    assert procid.start_tokens_match(base, near) is True
    assert procid.start_tokens_match(base, far) is False
    assert procid.start_tokens_match(base, far, tolerance_s=5.0) is True
    assert procid.start_tokens_match(base, "other:1") is False
    assert procid.start_tokens_match("other:1", base) is False
    assert procid.start_tokens_match(base, "lstart:garbage") is False


# -------------------------------------------------------------- verify_alive


def test_procid_verify_alive_handles_gone_foreign_and_reused_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gone pid is not alive. A pid owned by another uid (EPERM) exists, so
    the start token decides: a match is ours, a mismatch is a reused pid."""

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    def foreign(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(procid.os, "kill", gone)
    assert procid.verify_alive(5, "ticks:1") is False

    monkeypatch.setattr(procid.os, "kill", foreign)
    monkeypatch.setattr(procid, "_proc_look", lambda pid: ("ticks:1", False))
    assert procid.verify_alive(5, "ticks:1") is True
    assert procid.verify_alive(5, "ticks:2") is False
    monkeypatch.setattr(procid, "_proc_look", lambda pid: None)
    assert procid.verify_alive(5, "ticks:1") is False


def test_procid_verify_alive_linux_counts_a_zombie_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zombie keeps its /proc entry and its start ticks until it is
    reaped, so the token alone reads it as ours. State `Z` with one task
    left makes it dead. `Z` with two tasks is a live process whose main
    thread exited: alive. Any other state with the right token is alive, and
    the wrong token is not ours whatever the state."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid.os, "kill", lambda pid, sig: None)

    def stat(state: str, threads: str = "1") -> None:
        line = _stat_line(7, "sh", state, "4242", threads)
        monkeypatch.setattr(procid, "open", _fake_open(line), raising=False)

    stat("Z")
    assert procid.verify_alive(7, "ticks:4242") is False
    assert procid.proc_is_zombie(7) is True
    stat("Z", threads="2")
    assert procid.verify_alive(7, "ticks:4242") is True
    assert procid.proc_is_zombie(7) is False
    stat("S")
    assert procid.verify_alive(7, "ticks:4242") is True
    assert procid.verify_alive(7, "ticks:4243") is False
    monkeypatch.setattr(procid, "open", _fake_open(None), raising=False)
    assert procid.verify_alive(7, "ticks:4242") is False  # gone after kill(0)


def test_procid_linux_zombie_without_a_thread_count_reads_by_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stat line cut short before field 20 has no count to read; its state
    decides, as it did before the count was read."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid, "open", _fake_open(b"9 (x) Z 1 1"), raising=False)
    assert procid.proc_is_zombie(9) is True


def test_procid_verify_alive_macos_counts_a_zombie_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The macOS arm: one `ps -o stat=,lstart=` gives the state and the
    token. A stat starting with Z is dead; any other stat with a token in
    the tolerance is alive; a token outside it is not ours; no row is
    gone."""
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(procid.os, "kill", lambda pid, sig: None)
    lstart = "Mon Jan  1 00:00:00 2001"
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, f"Z    {lstart}    \n"))
    assert procid.verify_alive(7, f"lstart:{lstart}") is False
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, f"S+   {lstart}    \n"))
    assert procid.verify_alive(7, f"lstart:{lstart}") is True
    assert procid.verify_alive(7, "lstart:Mon Jan  1 00:00:05 2001") is False
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(1, ""))
    assert procid.verify_alive(7, f"lstart:{lstart}") is False
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, "Z\n"))
    assert procid.verify_alive(7, f"lstart:{lstart}") is False


def test_procid_verify_alive_a_reap_after_the_first_look_cannot_read_a_zombie_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first look finds a zombie with the right token; the pid is reaped
    before any later look. With the token and the state read in two looks,
    the second found nothing, answered "not a zombie", and the gone pid read
    alive. One look decides both. Linux (/proc) and macOS (`ps`) arms."""
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(procid.os, "kill", lambda pid, sig: None)
    looks: list[str] = []

    def stat_then_reaped(path: str, mode: str = "r", encoding: str | None = None) -> Any:
        looks.append(path)
        if len(looks) > 1:
            raise FileNotFoundError(path)  # reaped after the first look
        return io.BytesIO(_stat_line(7, "sh", "Z", "4242"))

    monkeypatch.setattr(procid, "open", stat_then_reaped, raising=False)
    assert procid.verify_alive(7, "ticks:4242") is False

    _platform(monkeypatch, "darwin")
    lstart = "Mon Jan  1 00:00:00 2001"
    calls: list[str] = []

    def ps_then_reaped(argv: list[str], **kwargs: Any) -> Any:
        column = argv[argv.index("-o") + 1]
        calls.append(column)
        if len(calls) > 1:
            return SimpleNamespace(returncode=1, stdout="")  # reaped
        rows = {"lstart=": lstart, "stat=": "Z", "stat=,lstart=": f"Z    {lstart}"}
        return SimpleNamespace(returncode=0, stdout=rows[column] + "\n")

    monkeypatch.setattr(procid.subprocess, "run", ps_then_reaped)
    assert procid.verify_alive(7, f"lstart:{lstart}") is False
    assert calls == ["stat=,lstart="]  # one fork per check, as before this change


def test_procid_verify_alive_a_real_zombie_is_dead_and_a_live_child_is_alive() -> None:
    """A real child on the host: alive with its own token, not ours with
    another, and dead once it has exited, although this process has not
    reaped it and its start token still matches."""
    child = subprocess.Popen(["/bin/sh", "-c", "read line"], stdin=subprocess.PIPE)
    try:
        token = procid.proc_start_token(child.pid)
        assert token is not None
        other = "ticks:1" if token.startswith("ticks:") else "lstart:Mon Jan  1 00:00:00 2001"
        assert procid.verify_alive(child.pid, token) is True
        assert procid.verify_alive(child.pid, other) is False
        assert child.stdin is not None
        child.stdin.close()  # `read` sees EOF and the shell exits on its own
        _wait_for(lambda: procid.proc_is_zombie(child.pid))
        assert procid.proc_start_token(child.pid) == token  # the token alone says ours
        assert procid.verify_alive(child.pid, token) is False
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=10)


#: Starts a worker that waits for stdin EOF and then ends the process, and
#: ends the MAIN thread with pthread_exit. The process lives on in the worker.
_MAIN_THREAD_EXITS = """
import ctypes, os, sys, threading
def worker():
    sys.stdin.buffer.read()
    os._exit(0)
threading.Thread(target=worker).start()
ctypes.CDLL(None).pthread_exit(None)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc thread states")
def test_procid_verify_alive_linux_a_live_process_whose_main_thread_exited_is_alive() -> None:
    """Linux shows the thread-group leader's state for the process, so a
    process whose main thread called pthread_exit reads `Z` while its other
    threads run. It is alive: two tasks are left. Once the last thread ends
    the process, it is a real zombie, one task, and dead."""
    child = subprocess.Popen([sys.executable, "-c", _MAIN_THREAD_EXITS], stdin=subprocess.PIPE)
    try:
        token = procid.proc_start_token(child.pid)
        assert token is not None
        _wait_for(lambda: (procid._linux_stat_fields(child.pid) or ["?"])[0] == "Z")
        with open(f"/proc/{child.pid}/status", encoding="ascii") as f:
            assert "Threads:\t2\n" in f.read()  # the worker still runs
        assert procid.proc_is_zombie(child.pid) is False
        assert procid.verify_alive(child.pid, token) is True
        assert child.stdin is not None
        child.stdin.close()  # the worker sees EOF and ends the process
        _wait_for(lambda: procid.proc_is_zombie(child.pid))
        assert procid.verify_alive(child.pid, token) is False
    finally:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=10)


def test_procid_killpg_quiet_swallows_only_a_gone_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        if pgid == 1:
            raise ProcessLookupError
        if pgid == 2:
            raise PermissionError

    monkeypatch.setattr(procid.os, "killpg", killpg)
    procid.killpg_quiet(1, 15)
    with pytest.raises(PermissionError):
        procid.killpg_quiet(2, 15)
    assert calls == [(1, 15), (2, 15)]


@pytest.mark.parametrize("listing", ["", "42 42 Z\n43 42 Z+\n44 43 S\n", "44 43 R+\n"])
def test_darwin_killpg_eperm_rereads_the_whole_group_before_suppressing(
    monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    _platform(monkeypatch, "darwin")
    calls: list[str] = []
    listing = f"1 1 S\n{os.getpid()} 2 S\n{listing}"

    def killpg(pgid: int, sig: int) -> None:
        assert (pgid, sig) == (42, 15)
        calls.append("killpg")
        raise PermissionError(errno.EPERM, "denied")

    def scan(argv: list[str], **kwargs: Any) -> Any:
        assert calls == ["killpg"]
        assert argv == ["ps", "-A", "-o", "pid=,pgid=,stat="]
        assert kwargs["env"]["LC_ALL"] == "C" and kwargs["timeout"] > 0
        calls.append("scan")
        return SimpleNamespace(returncode=0, stdout=listing)

    monkeypatch.setattr(procid.os, "killpg", killpg)
    monkeypatch.setattr(procid.subprocess, "run", scan)
    procid.killpg_quiet(42, 15)
    assert calls == ["killpg", "scan"]


@pytest.mark.parametrize(
    "listing",
    ["", "42 42 Z\n", "1 1 S\n42 42 Z\n", "{caller} 2 S\n42 42 Z\n"],
)
def test_darwin_killpg_eperm_keeps_the_error_without_both_listing_witnesses(
    monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    _platform(monkeypatch, "darwin")
    denied = PermissionError(errno.EPERM, "denied")

    def killpg(pgid: int, sig: int) -> None:
        raise denied

    monkeypatch.setattr(procid.os, "killpg", killpg)
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(0, listing.format(caller=os.getpid())))
    with pytest.raises(PermissionError) as caught:
        procid.killpg_quiet(42, 15)
    assert caught.value is denied


@pytest.mark.parametrize(
    ("returncode", "listing"),
    [
        (0, "42 42 Z\n43 42 S+\n"),
        (0, "42 42 R\n"),
        (0, "42 42 ?\n"),
        (0, "42 42 Z\n43 ? S\n"),
        (0, "42 42 Z\n? 43 S\n"),
        (0, "42 42 Z\n43 43\n"),
        (0, "42 42 Z\n43 43 S extra\n"),
        (1, ""),
        (1, "42 42 Z\n"),
    ],
)
def test_darwin_killpg_eperm_keeps_the_error_with_live_or_unreadable_members(
    monkeypatch: pytest.MonkeyPatch, returncode: int, listing: str
) -> None:
    _platform(monkeypatch, "darwin")
    denied = PermissionError(errno.EPERM, "denied")
    listing = f"1 1 S\n{os.getpid()} 2 S\n{listing}"

    def killpg(pgid: int, sig: int) -> None:
        raise denied

    monkeypatch.setattr(procid.os, "killpg", killpg)
    monkeypatch.setattr(procid.subprocess, "run", _fake_run(returncode, listing))
    with pytest.raises(PermissionError) as caught:
        procid.killpg_quiet(42, 15)
    assert caught.value is denied


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.EIO, "scan failed"),
        PermissionError(errno.EPERM, "ps denied"),
        subprocess.TimeoutExpired("ps", 5.0),
    ],
)
def test_darwin_killpg_eperm_keeps_the_original_error_after_a_failed_scan(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    _platform(monkeypatch, "darwin")
    denied = PermissionError(errno.EPERM, "kill denied")

    def killpg(pgid: int, sig: int) -> None:
        raise denied

    def scan(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(procid.os, "killpg", killpg)
    monkeypatch.setattr(procid.subprocess, "run", scan)
    with pytest.raises(PermissionError) as caught:
        procid.killpg_quiet(42, 9)
    assert caught.value is denied


@pytest.mark.parametrize(("platform", "error"), [("linux", errno.EPERM), ("darwin", errno.EACCES)])
def test_killpg_permission_errors_outside_the_darwin_eperm_case_never_scan(
    monkeypatch: pytest.MonkeyPatch, platform: str, error: int
) -> None:
    _platform(monkeypatch, platform)
    denied = PermissionError(error, "denied")

    def killpg(pgid: int, sig: int) -> None:
        raise denied

    def scan(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("only Darwin EPERM may scan")

    monkeypatch.setattr(procid.os, "killpg", killpg)
    monkeypatch.setattr(procid.subprocess, "run", scan)
    with pytest.raises(PermissionError) as caught:
        procid.killpg_quiet(42, 15)
    assert caught.value is denied
