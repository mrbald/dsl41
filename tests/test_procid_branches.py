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
import os
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


def _stat_line(pid: int, comm: str, state: str, start: str) -> bytes:
    """A /proc/<pid>/stat line. Field 3 is the state, field 22 the start
    ticks; fields 4..21 are filler."""
    filler = " ".join("0" for _ in range(4, 22))
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


# ------------------------------------------------------------ durable_create


def test_procid_durable_create_tolerates_a_temp_that_is_already_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final temp unlink is best effort: when the temp vanished after the
    link, the publication still stands and the call returns."""
    target = tmp_path / "rec.json"
    real_link = os.link

    def link_then_drop_temp(src: str, dst: str) -> None:
        real_link(src, dst)
        os.unlink(src)

    monkeypatch.setattr(procid.os, "link", link_then_drop_temp)
    procid.durable_create(str(target), b"payload")
    assert target.read_bytes() == b"payload"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rec.json"]


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
    monkeypatch.setattr(procid, "proc_start_token", lambda pid: "ticks:1")
    assert procid.verify_alive(5, "ticks:1") is True
    assert procid.verify_alive(5, "ticks:2") is False
    monkeypatch.setattr(procid, "proc_start_token", lambda pid: None)
    assert procid.verify_alive(5, "ticks:1") is False


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
