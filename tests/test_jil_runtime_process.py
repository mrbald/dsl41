from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from dsl41 import runner_adapters as adapters
from dsl41 import runner_procid as procid
from dsl41.runner_clock import EngineError


def test_file_observations_and_recovery(tmp_path: Path):
    a, b, hidden = (tmp_path / name for name in ("a.dat", "b.dat", ".hidden.dat"))
    a.write_text("1234")
    b.write_text("12")
    hidden.write_text("1234")
    log = tmp_path / adapters.WATCH_LOG
    now = datetime(2026, 1, 1)
    adapters.append_watch_line(
        log, {"artifact_format_version": 1, "kind": "start", "at": now.isoformat(), "run_id": "run"}
    )
    first = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    assert [Path(item.path).name for item in first] == ["a.dat"]
    state = adapters.append_pattern_poll(
        log, "run", now, adapters.PatternWatchProgress(), first, immediate=False
    )
    assert not state.complete
    saved = adapters.read_watch_log(tmp_path).pattern
    assert saved == state
    replacement = tmp_path / "replacement"
    replacement.write_text("1234")
    replacement.replace(a)
    after = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    assert first[0].inode != after[0].inode or first[0].device != after[0].device
    state = adapters.append_pattern_poll(log, "run", now, saved, after, immediate=False)
    assert not state.complete and state.stable[str(a)] == 1
    a.unlink()
    state = adapters.append_pattern_poll(log, "run", now, state, (), immediate=False)
    assert not state.previous
    b.write_text("1234")
    obs = adapters.observe_pattern(str(tmp_path / "*.dat"), 4)
    state = adapters.append_pattern_poll(log, "run", now, state, obs, immediate=False)
    state = adapters.append_pattern_poll(log, "run", now, state, obs, immediate=False)
    assert state.complete and adapters.read_watch_log(tmp_path).pattern == state
    assert [
        Path(item.path).name for item in adapters.observe_pattern(str(tmp_path / ".*.dat"), 4)
    ] == [".hidden.dat"]


def test_watcher_first_poll_and_durable_before_progress(tmp_path: Path, monkeypatch):
    entry = adapters.FileObservation("/a", 1, 2, 3, 100, 100)
    prior = adapters.PatternWatchProgress()
    assert adapters.fold_pattern_poll(prior, (entry,), immediate=True).complete

    def fail(*args):
        raise OSError("fsync failure")

    monkeypatch.setattr(adapters, "append_watch_line", fail)
    with pytest.raises(OSError, match="fsync failure"):
        adapters.append_pattern_poll(
            tmp_path / "log", "r", datetime.now(), prior, (entry,), immediate=False
        )
    assert prior == adapters.PatternWatchProgress()


def test_watcher_bad_and_torn_evidence(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    log.write_text(json.dumps(start) + '\n{"kind":')
    assert adapters.read_watch_log(tmp_path).watch_seq == 1
    log.write_text(json.dumps(start) + '\n{"artifact_format_version":1,"run_id":"foreign"}\n')
    with pytest.raises(EngineError, match="run_id"):
        adapters.read_watch_log(tmp_path)
    bad = {
        "artifact_format_version": 1,
        "kind": "sample",
        "run_id": "r",
        "at": start["at"],
        "files": [
            {"path": "/a", "device": True, "inode": 2, "size": 3, "mtime_ns": 10, "ctime_ns": 10}
        ],
    }
    log.write_text(json.dumps(start) + "\n" + json.dumps(bad) + "\n")
    with pytest.raises(EngineError, match="observation"):
        adapters.read_watch_log(tmp_path)


def test_selected_ladder_real_target_and_spacing():
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    token = procid.proc_start_token(process.pid)
    assert token is not None
    start = time.monotonic()
    try:
        asyncio.run(
            adapters.selected_signal_ladder(process.pid, token, (signal.SIGINT, signal.SIGKILL))
        )
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert time.monotonic() - start >= 5.0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def test_selected_ladder_guards_and_unknown(monkeypatch):
    sent = []
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kw: sent.append((pid, token, sig)) or True
    )
    asyncio.run(
        adapters.selected_signal_ladder(
            1, "old", (9,), lambda: procid.Left("ours", {123: "token"}), pidfd=False
        )
    )
    assert sent == [(123, "token", 9)]
    with pytest.raises(OSError, match="unknown"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "old", (9,), lambda: procid.Left("unknown", reason="unknown")
            )
        )
    sent.clear()
    asyncio.run(adapters.selected_signal_ladder(1, "old", (9,), lambda: procid.NOTHING_LEFT))
    assert not sent


def test_pid_token_guard_leaves_real_unrelated_process_alive():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not procid.signal_ours(
            process.pid, "not-the-start-token", signal.SIGKILL, pidfd=False
        )
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_signal_ladder_stops_after_early_exit():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None and process.stdout.readline().strip() == "ready"
    token = procid.proc_start_token(process.pid)
    assert token is not None
    start = time.monotonic()
    try:
        asyncio.run(
            adapters.selected_signal_ladder(process.pid, token, (signal.SIGTERM, signal.SIGKILL))
        )
        assert process.wait(timeout=5) == -signal.SIGTERM
        assert time.monotonic() - start < 5.0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def test_signal_ladder_target_failures(monkeypatch):
    monkeypatch.setattr(procid, "signal_ours", lambda *args, **kwargs: False)
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: True)
    asyncio.run(
        adapters.selected_signal_ladder(1, "token", (9,), lambda: procid.Left("ours", {2: "old"}))
    )
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    with pytest.raises(OSError, match="not sent"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "token", (9,), lambda: procid.Left("ours", {2: "old"})
            )
        )

    def failed_read(*args):
        raise OSError("permission denied")

    monkeypatch.setattr(procid, "scanned_process_gone", failed_read)
    with pytest.raises(OSError, match="identity read failed"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "token", (9,), lambda: procid.Left("ours", {2: "old"})
            )
        )


def test_signal_ladder_group_requires_leader_proof(monkeypatch):
    calls = []
    monkeypatch.setattr(procid, "verify_alive", lambda *args: False)
    monkeypatch.setattr(procid, "killpg_quiet", lambda *args: calls.append(args))
    asyncio.run(adapters.selected_signal_ladder(123, "wrong", (9,)))
    assert calls == []
    monkeypatch.setattr(procid, "verify_alive", lambda *args: True)
    monkeypatch.setattr(adapters, "group_has_live_member", lambda *args: False)
    asyncio.run(adapters.selected_signal_ladder(123, "right", (9,)))
    assert calls == []


def test_pattern_observe_race_and_duplicate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(adapters.glob, "iglob", lambda pattern: iter(["/vanished"]))

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(adapters.os, "stat", gone)
    assert adapters.observe_pattern("/irrelevant/*", 0) == ()
    item = adapters.FileObservation("/a", 1, 2, 3, 100, 100)
    with pytest.raises(EngineError, match="duplicate"):
        adapters.fold_pattern_poll(adapters.PatternWatchProgress(), (item, item), immediate=False)


@pytest.mark.parametrize(
    "record, error",
    [
        ({"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": None}, "sample files"),
        (
            {"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": [{"path": ""}]},
            "invalid file",
        ),
        ({"kind": "sample", "run_id": "r", "at": "bad", "files": []}, "timestamp"),
        ({"kind": "wrong", "run_id": "r", "at": "2026-01-01", "files": []}, "kind"),
        ({"kind": "sample", "run_id": "r", "at": "2026-01-01", "files": [None]}, "invalid file"),
        (
            {
                "kind": "sample",
                "run_id": "r",
                "at": "2026-01-01",
                "files": [
                    {
                        "path": "/x",
                        "device": 1,
                        "inode": 2,
                        "size": -1,
                        "mtime_ns": 10,
                        "ctime_ns": 10,
                    }
                ],
            },
            "observation",
        ),
    ],
)
def test_pattern_rejects_malformed_candidate(tmp_path: Path, record: dict, error: str):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    record = {"artifact_format_version": 1, **record}
    log.write_text(json.dumps(start) + "\n" + json.dumps(record) + "\n")
    with pytest.raises(EngineError, match=error):
        adapters.read_watch_log(tmp_path)


def test_pattern_rejects_unreadable_and_nonstart_logs(tmp_path: Path):
    path = tmp_path / adapters.WATCH_LOG
    assert adapters.read_watch_log(tmp_path) is None
    path.mkdir()
    with pytest.raises(EngineError, match="unreadable"):
        adapters.read_watch_log(tmp_path)
    path.rmdir()
    path.write_text(
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01"}\n'
    )
    with pytest.raises(EngineError, match="first line"):
        adapters.read_watch_log(tmp_path)
    path.write_text(
        '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}\n\n'
    )
    with pytest.raises(EngineError, match="empty interior"):
        adapters.read_watch_log(tmp_path)


def test_pattern_fence_checked_before_write(tmp_path: Path, monkeypatch):
    calls = []

    class Fence:
        def check(self):
            assert not log.exists()
            calls.append("checked")

    log = tmp_path / adapters.WATCH_LOG
    state = adapters.append_pattern_poll(
        log,
        "r",
        datetime.now(),
        adapters.PatternWatchProgress(),
        (),
        immediate=False,
        fence=Fence(),
    )
    assert calls == ["checked"] and state.polls == 1 and log.exists()
    before = log.read_bytes()

    def refuse():
        raise EngineError("lost fence")

    monkeypatch.setattr(Fence, "check", lambda self: refuse())
    with pytest.raises(EngineError, match="lost fence"):
        adapters.append_pattern_poll(
            log, "r", datetime.now(), state, (), immediate=False, fence=Fence()
        )
    assert log.read_bytes() == before


def test_pattern_bad_timestamp_empty_and_interior_corruption(tmp_path: Path):
    path = tmp_path / adapters.WATCH_LOG
    path.write_bytes(b"")
    assert adapters.read_watch_log(tmp_path) is None
    start = {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": 4}
    path.write_text(json.dumps(start) + "\n")
    with pytest.raises(EngineError, match="timestamp"):
        adapters.read_watch_log(tmp_path)
    path.write_text(
        '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}\n{bad}\n'
    )
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)


def test_sampled_watcher_collision_and_same_signature_limit():
    before = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    changed = adapters.FileObservation("/watch", 1, 2, 8, 101, 102)
    initial = adapters.fold_pattern_poll(
        adapters.PatternWatchProgress(), (before,), immediate=False
    )
    replaced = adapters.fold_pattern_poll(initial, (changed,), immediate=False)
    assert not replaced.complete and replaced.stable["/watch"] == 1
    indistinguishable = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    assert adapters.fold_pattern_poll(initial, (indistinguishable,), immediate=False).complete


@pytest.mark.parametrize("change", ["write", "chmod", "utime"])
def test_sampled_watcher_same_size_metadata_changes_reset(tmp_path: Path, change: str):
    watched = tmp_path / "watch"
    watched.write_bytes(b"1234")
    first = adapters.observe_pattern(str(watched), 4)
    assert len(first) == 1
    state = adapters.fold_pattern_poll(adapters.PatternWatchProgress(), first, immediate=False)
    if change == "write":
        watched.write_bytes(b"4321")
    elif change == "chmod":
        watched.chmod((watched.stat().st_mode & 0o777) ^ 0o100)
    else:
        stat = watched.stat()
        os.utime(watched, ns=(stat.st_atime_ns, stat.st_mtime_ns - 5_000_000_000))
    second = adapters.observe_pattern(str(watched), 4)
    assert second[0].size == first[0].size
    assert (second[0].mtime_ns, second[0].ctime_ns) != (first[0].mtime_ns, first[0].ctime_ns)
    reset = adapters.fold_pattern_poll(state, second, immediate=False)
    assert not reset.complete and reset.stable[str(watched)] == 1


def test_sampled_watcher_independent_paths_and_immediate_first_poll():
    a = adapters.FileObservation("/a", 1, 2, 8, 100, 100)
    b = adapters.FileObservation("/b", 1, 3, 8, 100, 100)
    changed_a = adapters.FileObservation("/a", 1, 2, 8, 100, 101)
    empty = adapters.PatternWatchProgress()
    first = adapters.fold_pattern_poll(empty, (a, b), immediate=False)
    second = adapters.fold_pattern_poll(first, (changed_a, b), immediate=False)
    assert second.complete and second.stable == {"/a": 1, "/b": 2}
    assert adapters.fold_pattern_poll(empty, (a,), immediate=True).complete
    missing = adapters.fold_pattern_poll(empty, (), immediate=True)
    late = adapters.fold_pattern_poll(missing, (a,), immediate=True)
    assert not late.complete
    assert adapters.fold_pattern_poll(late, (a,), immediate=True).complete


def test_sampled_watcher_restart_and_strict_replay(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    at = datetime(2026, 1, 1)
    adapters.append_watch_line(
        log, {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": at.isoformat()}
    )
    first = adapters.FileObservation("/watch", 1, 2, 8, -10, 100)
    state = adapters.append_pattern_poll(
        log, "r", at, adapters.PatternWatchProgress(), (first,), immediate=False
    )
    restored = adapters.read_watch_log(tmp_path).pattern
    assert restored == state
    assert adapters.fold_pattern_poll(restored, (first,), immediate=False).complete
    changed = adapters.FileObservation("/watch", 1, 2, 8, -10, 101)
    assert not adapters.fold_pattern_poll(restored, (changed,), immediate=False).complete
    row = json.loads(log.read_text().splitlines()[1])
    assert row["at"] == "2026-01-01T00:00:00.000000"
    assert row["kind"] == "sample" and row["artifact_format_version"] == 1
    assert row["files"] == [
        {"path": "/watch", "device": 1, "inode": 2, "size": 8, "mtime_ns": -10, "ctime_ns": 100}
    ]
    del row["files"][0]["ctime_ns"]
    log.write_text(
        json.dumps(
            {"artifact_format_version": 1, "kind": "start", "run_id": "r", "at": at.isoformat()}
        )
        + "\n"
        + json.dumps(row)
        + "\n"
    )
    with pytest.raises(EngineError, match="invalid file observation"):
        adapters.read_watch_log(tmp_path)


def test_watcher_disappearance_resets_even_same_later_sample():
    original = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    initial = adapters.fold_pattern_poll(
        adapters.PatternWatchProgress(), (original,), immediate=False
    )
    gone = adapters.fold_pattern_poll(initial, (), immediate=False)
    reappeared = adapters.fold_pattern_poll(gone, (original,), immediate=False)
    assert not reappeared.complete and reappeared.stable["/watch"] == 1


@pytest.mark.parametrize(
    "record",
    [
        '{"artifact_format_version":1,"kind":"start","run_id":"r","run_id":"r","at":"2026-01-01"}',
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[],"files":[{"path":"/a","device":1,"inode":2,"size":3,"mtime_ns":4,"ctime_ns":5}]}',
        '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[{"path":"/a","path":"/b","device":1,"inode":2,"size":3,"mtime_ns":4,"ctime_ns":5}]}',
    ],
)
def test_pattern_evidence_refuses_duplicate_keys_at_any_depth(tmp_path: Path, record: str):
    log = tmp_path / adapters.WATCH_LOG
    start = '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}'
    log.write_text(("" if '"kind":"start"' in record else start + "\n") + record + "\n")
    with pytest.raises(EngineError, match="duplicate object key"):
        adapters.read_watch_log(tmp_path)


def test_pattern_torn_final_poll_keeps_durable_start(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = {
        "artifact_format_version": 1,
        "kind": "start",
        "run_id": "r",
        "at": "2026-01-01T00:00:00",
    }
    adapters.append_watch_line(log, start)
    sample = adapters.FileObservation("/watch", 1, 2, 8, 100, 100)
    saved = adapters.append_pattern_poll(
        log, "r", datetime(2026, 1, 1), adapters.PatternWatchProgress(), (sample,), immediate=False
    )
    with log.open("ab") as stream:
        stream.write(b'{"kind":"po')
    progress = adapters.read_watch_log(tmp_path).pattern
    assert progress == saved and progress.stable["/watch"] == 1


def test_pattern_complete_bad_start_is_not_no_start(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    log.write_bytes(b"{bad}\n")
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)


def test_signal_ladder_sends_terminal_to_all_after_early_refusal(monkeypatch):
    from types import SimpleNamespace

    calls = []
    reads = []
    times = iter([0.0, 6.0])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    monkeypatch.setattr(
        procid, "killpg_quiet", lambda *args: pytest.fail("unexpected group signal")
    )

    def send(pid, token, sig, **kwargs):
        calls.append((pid, token, sig))
        return not (pid == 101 and sig == signal.SIGINT)

    monkeypatch.setattr(procid, "signal_ours", send)

    def members():
        reads.append(1)
        tokens = ("A-final", "B-final") if len(reads) >= 3 else ("A-first", "B-first")
        return procid.Left("ours", {101: tokens[0], 202: tokens[1]})

    with pytest.raises(OSError, match="signal 2 not sent to pid 101"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), members, pidfd=False
            )
        )
    assert calls == [
        (101, "A-first", signal.SIGINT),
        (202, "B-first", signal.SIGINT),
        (101, "A-final", signal.SIGKILL),
        (202, "B-final", signal.SIGKILL),
    ]
    assert len(reads) >= 3


def test_signal_ladder_terminal_refusal_still_sends_other_target(monkeypatch):
    calls = []
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)

    def send(pid, token, sig, **kwargs):
        calls.append((pid, token, sig))
        return pid != 101

    monkeypatch.setattr(procid, "signal_ours", send)
    with pytest.raises(OSError, match="signal 9 not sent to pid 101"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1,
                "leader",
                (signal.SIGKILL,),
                lambda: procid.Left("ours", {101: "A", 202: "B"}),
                pidfd=False,
            )
        )
    assert calls == [(101, "A", signal.SIGKILL), (202, "B", signal.SIGKILL)]


def test_signal_ladder_reports_prior_failure_after_proven_disappearance(monkeypatch):
    readings = iter([procid.Left("ours", {101: "A"}), procid.NOTHING_LEFT])
    calls = []
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kwargs: calls.append((pid, sig)) or False
    )
    monkeypatch.setattr(procid, "scanned_process_gone", lambda *args: False)
    with pytest.raises(OSError, match="signal 2 not sent") as exc:
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), lambda: next(readings)
            )
        )
    assert calls == [(101, signal.SIGINT)] and "still alive" not in str(exc.value)


def test_signal_ladder_unknown_membership_keeps_verified_terminal_sends(monkeypatch):
    from types import SimpleNamespace

    calls = []
    times = iter([0.0, 6.0])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(
        procid, "signal_ours", lambda pid, token, sig, **kwargs: calls.append((pid, sig)) or True
    )

    def unreadable():
        return procid.Left("unknown", {202: "B"}, "membership unreadable")

    with pytest.raises(OSError, match="membership unreadable"):
        asyncio.run(
            adapters.selected_signal_ladder(
                1, "leader", (signal.SIGINT, signal.SIGKILL), unreadable
            )
        )
    assert calls == [(202, signal.SIGINT), (202, signal.SIGKILL)]


def test_pattern_corrupt_interior_record_does_not_redispatch(tmp_path: Path):
    log = tmp_path / adapters.WATCH_LOG
    start = '{"artifact_format_version":1,"kind":"start","run_id":"r","at":"2026-01-01"}'
    poll = '{"artifact_format_version":1,"kind":"sample","run_id":"r","at":"2026-01-01","files":[]}'
    log.write_text(start + "\n{bad}\n" + poll + "\n")
    with pytest.raises(EngineError, match="line"):
        adapters.read_watch_log(tmp_path)
