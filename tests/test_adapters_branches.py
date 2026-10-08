"""Branch tests for `dsl41.runner_adapters` (DL-265, DL-269).

Each test drives one decision the rest of the suite leaves untaken and asserts
what that decision does. Platform- and timing-dependent arms are driven through
stand-ins (a fake process, a scripted `_request`, a patched `time`), so the
number does not depend on the host. Real sockets and real processes appear only
where a stand-in would prove nothing: the blocking `SupervisorConn` client and
the tethered wrapper's own exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_runner_adapters import _ACK, _ClientWire, _ScriptedRequests

from dsl41 import runner_adapters as _adapters
from dsl41 import runner_procid as _procid
from dsl41.canon import ARTIFACT_FORMAT_VERSION
from dsl41.ir import JobIR, lower_source
from dsl41.runner_adapters import (
    WATCH_LOG,
    AdapterContext,
    Failed,
    FileWatcherAdapter,
    LocalCommandAdapter,
    SealBarrier,
    SpawnInProgress,
    SupervisedCommandAdapter,
    SupervisorClient,
    SupervisorConn,
    SupervisorUnavailable,
    Terminated,
    _build_run_spec,
    _named,
    job_log_paths,
    read_watch_log,
    resolve_spool,
    running_supervisor_deadman,
)
from dsl41.runner_clock import EngineError, VirtualClock

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("the adapters/wrapper tier is POSIX-only", allow_module_level=True)

T0 = datetime(2026, 7, 1, 8, 0)


def _cmd_job(name: str = "j") -> JobIR:
    return lower_source(f"insert_job: {name}\njob_type: c\ncommand: exit 0\n").jobs[name]


def _fw_job(watch_file: Path, *, extra: str = "") -> JobIR:
    text = f"insert_job: w\njob_type: f\nwatch_file: {watch_file}\nwatch_interval: 30\n{extra}"
    return lower_source(text).jobs["w"]


class _FakeMonotonic:
    """A `time` stand-in whose `monotonic` advances by a fixed step per call."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


def _patch_monotonic(monkeypatch: pytest.MonkeyPatch, step: float) -> None:
    monkeypatch.setattr(_adapters, "time", _FakeMonotonic(step))


def _reaped_pid() -> int:
    """An id that named a process that has been reaped: no live process or group has it, and
    a signal sent to it by mistake finds nothing."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ------------------------------------------------------------ SealBarrier


def test_seal_barrier_wait_returns_at_once_when_nothing_is_parked() -> None:
    barrier = SealBarrier()

    async def scenario() -> None:
        await asyncio.wait_for(barrier.wait(), 3)

    asyncio.run(scenario())
    assert barrier.parked_tasks == 0


def test_seal_barrier_holds_every_waiter_until_released() -> None:
    barrier = SealBarrier()

    async def scenario() -> tuple[int, int, bool]:
        barrier.park()
        first = asyncio.create_task(barrier.wait())
        second = asyncio.create_task(barrier.wait())  # meets the event the first made
        while barrier.parked_tasks < 2:
            await asyncio.sleep(0)
        held = (barrier.parked_tasks, int(first.done()) + int(second.done()))
        barrier.release()
        await asyncio.wait_for(asyncio.gather(first, second), 3)
        return held[0], held[1], barrier.parked

    parked_tasks, done_while_parked, parked_after = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert (parked_tasks, done_while_parked) == (2, 0)
    assert parked_after is False
    assert barrier.parked_tasks == 0


def test_seal_barrier_release_without_a_waiter_only_clears_the_flag() -> None:
    barrier = SealBarrier()
    barrier.park()
    barrier.release()
    assert barrier.parked is False
    assert barrier._released is None


# ---------------------------------------------------------- _build_run_spec


def test_build_run_spec_refuses_a_missing_run_root(tmp_path: Path) -> None:
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=None)
    with pytest.raises(EngineError, match="needs a run_root"):
        _build_run_spec(_cmd_job(), 1, ctx, grace_seconds=1.0)


def test_build_run_spec_refuses_a_job_without_an_exec_spec(tmp_path: Path) -> None:
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)
    with pytest.raises(EngineError, match="CMD dispatch without an ExecSpec"):
        _build_run_spec(_fw_job(tmp_path / "f"), 1, ctx, grace_seconds=1.0)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("name", ["a/b", ".", ".."])
def test_build_run_spec_refuses_a_name_that_is_not_a_safe_directory(
    tmp_path: Path, name: str
) -> None:
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)
    job = _cmd_job().model_copy(update={"name": name})
    with pytest.raises(EngineError, match="not a safe run-directory name"):
        _build_run_spec(job, 1, ctx, grace_seconds=1.0)
    assert not (tmp_path / "runs").exists()


def test_job_log_paths_take_the_job_own_files_or_fall_back_to_the_convention(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    # a watch job has no ExecSpec: the convention applies
    assert job_log_paths(_fw_job(tmp_path / "f"), 2, tmp_path) == (
        str(logs / "w.2.out"),
        str(logs / "w.2.err"),
    )
    own = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nstd_out_file: /o.log\nstd_err_file: /e.log\n"
    ).jobs["j"]
    assert job_log_paths(own, 2, tmp_path) == ("/o.log", "/e.log")


# ------------------------------------------------------ LocalCommandAdapter


def test_local_adapter_runs_a_command_with_no_journal(tmp_path: Path) -> None:
    job = lower_source("insert_job: j\njob_type: c\ncommand: exit 5\n").jobs["j"]
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)
    result = asyncio.run(LocalCommandAdapter(grace_seconds=1.0).run(job, 1, ctx))
    assert result == 5


def test_local_adapter_reports_unobservable_when_the_wrapper_leaves_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = tmp_path / "wrapper_stub.py"
    stub.write_text("import sys\nsys.stdin.buffer.read()\nsys.exit(3)\n")
    monkeypatch.setattr(_adapters, "_WRAPPER_PATH", stub)
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)
    result = asyncio.run(LocalCommandAdapter(grace_seconds=1.0).run(_cmd_job(), 1, ctx))
    assert isinstance(result, Failed)
    assert "exit_status_unobservable" in result.cause
    assert "rc=3" in result.cause


class _BrokenStdin:
    def write(self, data: bytes) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    async def drain(self) -> None:
        raise AssertionError("the write already failed")

    def close(self) -> None:
        raise AssertionError("a failed write is not followed by a close")


class _WaitedProc:
    """A wrapper process that died while reading its spec."""

    pid = 4242
    returncode: int | None = 1

    def __init__(self) -> None:
        self.stdin = _BrokenStdin()
        self.waited = 0

    async def wait(self) -> int:
        self.waited += 1
        return 1


def test_local_adapter_fails_the_job_when_the_wrapper_dies_reading_its_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _WaitedProc()

    async def fake_exec(*args: Any, **kwargs: Any) -> _WaitedProc:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)
    result = asyncio.run(LocalCommandAdapter(grace_seconds=1.0).run(_cmd_job(), 1, ctx))
    assert isinstance(result, Failed)
    assert result.cause.startswith("wrapper spawn failed: ")
    assert proc.waited == 1


class _KillProc:
    """A wrapper process that ends when the test says so."""

    stdin = None
    returncode: int | None = None

    def __init__(self, ended: asyncio.Event) -> None:
        self.ended = ended
        self.waited = 0

    async def wait(self) -> int:
        self.waited += 1
        await self.ended.wait()
        return 0


def _record_signals(monkeypatch: pytest.MonkeyPatch, *, on: dict[int, asyncio.Event]) -> list:
    """Replace `killpg_quiet` with a recorder. A signal listed in `on` also ends the process."""
    sent: list[tuple[int, int]] = []

    def killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        if sig in on:
            on[sig].set()

    monkeypatch.setattr(_procid, "killpg_quiet", killpg)
    return sent


def test_kill_gives_up_without_signalling_when_no_spawn_record_ever_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_monotonic(monkeypatch, 2.0)  # the 5 s record wait is spent in three probes
    sent = _record_signals(monkeypatch, on={})
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()

    async def scenario() -> _KillProc:
        proc = _KillProc(asyncio.Event())  # never ends
        await LocalCommandAdapter(grace_seconds=0.05)._kill(run_dir, proc, "rid")  # type: ignore[arg-type]
        return proc

    proc = asyncio.run(scenario())
    assert sent == []
    assert proc.waited == 1  # it waited for the wrapper, bounded, and left it to its own record


def test_kill_does_not_signal_a_group_whose_leader_fails_the_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _record_signals(monkeypatch, on={})
    leader = _reaped_pid()
    probes: list[tuple[int, str]] = []

    def verify(pid: int, token: str) -> bool:
        probes.append((pid, token))
        return False

    monkeypatch.setattr(_procid, "verify_alive", verify)
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "run_id": "rid",
                "command_pid": leader,
                "command_pgid": leader,
                "command_start_time": "ticks:9",
            }
        )
    )

    async def scenario() -> _KillProc:
        ended = asyncio.Event()
        ended.set()
        proc = _KillProc(ended)
        await LocalCommandAdapter(grace_seconds=0.05)._kill(run_dir, proc, "rid")  # type: ignore[arg-type]
        return proc

    proc = asyncio.run(scenario())
    assert probes == [(leader, "ticks:9")]
    assert sent == []
    assert proc.waited == 1


def _kill_with_a_live_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ends_on: int | None
) -> tuple[list, _KillProc, int]:
    """Run `_kill` on a record whose leader verifies alive. The process ends on `ends_on`."""
    monkeypatch.setattr(_procid, "verify_alive", lambda pid, token: True)
    leader, pgid = _reaped_pid(), _reaped_pid()
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    (run_dir / "spawn.json").write_text(
        json.dumps(
            {
                "run_id": "rid",
                "command_pid": leader,
                "command_pgid": pgid,
                "command_start_time": "ticks:9",
            }
        )
    )

    async def scenario() -> tuple[list, _KillProc, int]:
        ended = asyncio.Event()
        sent = _record_signals(monkeypatch, on={} if ends_on is None else {ends_on: ended})
        proc = _KillProc(ended)
        # bounded: a `_kill` that never sends the signal the process waits for fails here
        await asyncio.wait_for(
            LocalCommandAdapter(grace_seconds=0.05)._kill(run_dir, proc, "rid"),  # type: ignore[arg-type]
            10,
        )
        return sent, proc, pgid

    return asyncio.run(scenario())


def test_kill_stops_at_sigterm_when_the_group_honours_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, proc, pgid = _kill_with_a_live_leader(tmp_path, monkeypatch, ends_on=signal.SIGTERM)
    assert sent == [(pgid, signal.SIGTERM)]
    assert proc.waited == 2  # the grace wait, then the final wait


def test_kill_escalates_to_sigkill_when_the_wrapper_outlives_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent, proc, pgid = _kill_with_a_live_leader(tmp_path, monkeypatch, ends_on=signal.SIGKILL)
    assert sent == [(pgid, signal.SIGTERM), (pgid, signal.SIGKILL)]
    assert proc.waited == 2


# ------------------------------------------------------------ read_watch_log


def _start(**over: Any) -> dict[str, Any]:
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": T0.isoformat(),
        "kind": "start",
        "run_id": "r1",
        **over,
    }


def _poll(at_s: int = 30, **over: Any) -> dict[str, Any]:
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": (T0 + timedelta(seconds=at_s)).isoformat(),
        "exists": True,
        "kind": "poll",
        "qualifying": True,
        "run_id": "r1",
        "size": 4,
        "stable_polls": 1,
        **over,
    }


def _write_log(run_dir: Path, *records: dict[str, Any], tail: bytes = b"\n") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / WATCH_LOG
    lines = [json.dumps(r, sort_keys=True) for r in records]
    path.write_bytes("\n".join(lines).encode() + tail)
    return path


def test_watch_log_with_only_a_start_line_polls_first_at_its_start(tmp_path: Path) -> None:
    _write_log(tmp_path, _start())
    log = read_watch_log(tmp_path)
    assert log is not None
    assert log.last_at is None
    assert log.next_poll_at(30) == T0
    # a poll line moves the next poll one interval past it
    _write_log(tmp_path, _start(), _poll(30))
    after_poll = read_watch_log(tmp_path)
    assert after_poll is not None
    assert after_poll.next_poll_at(30) == T0 + timedelta(seconds=60)


def test_watch_log_that_cannot_be_read_refuses_instead_of_reading_as_undispatched(
    tmp_path: Path,
) -> None:
    (tmp_path / WATCH_LOG).mkdir()  # exists, and is not a readable file
    with pytest.raises(EngineError, match="unreadable"):
        read_watch_log(tmp_path)


def test_watch_log_with_an_empty_interior_line_is_corruption(tmp_path: Path) -> None:
    run_dir = tmp_path
    path = run_dir / WATCH_LOG
    path.write_bytes(json.dumps(_start()).encode() + b"\n\n" + json.dumps(_poll()).encode() + b"\n")
    with pytest.raises(EngineError, match="empty interior line 2"):
        read_watch_log(run_dir)


def test_watch_log_line_that_is_not_an_object_refuses(tmp_path: Path) -> None:
    (tmp_path / WATCH_LOG).write_bytes(b"[1, 2]\n")
    with pytest.raises(EngineError, match="line 1 is not an object"):
        read_watch_log(tmp_path)


@pytest.mark.parametrize("content", [b"", b'{"artifact_format_version": '])
def test_watch_log_without_one_complete_line_reads_as_undispatched(
    tmp_path: Path, content: bytes
) -> None:
    (tmp_path / WATCH_LOG).write_bytes(content)
    assert read_watch_log(tmp_path) is None


def test_watch_log_whose_last_line_is_complete_but_unterminated_is_read(tmp_path: Path) -> None:
    _write_log(tmp_path, _start(), _poll(), tail=b"")  # no trailing newline
    log = read_watch_log(tmp_path)
    assert log is not None
    assert log.watch_seq == 2
    assert log.stable_polls == 1


def test_watch_log_start_line_run_id_must_be_a_string_or_null(tmp_path: Path) -> None:
    _write_log(tmp_path, _start(run_id=5))
    with pytest.raises(EngineError, match="start line run_id is not a string or null"):
        read_watch_log(tmp_path)
    _write_log(tmp_path, _start(run_id=None), _poll(run_id=None))
    log = read_watch_log(tmp_path)
    assert log is not None
    assert log.run_id is None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"size": "big"}, r"size 'big'"),
        ({"qualifying": "yes"}, r"qualifying is not a boolean"),
        ({"exists": "yes"}, r"exists is not a boolean"),
        ({"size": None, "qualifying": False, "exists": True}, r"exists True with size None"),
        ({"size": None, "qualifying": True, "exists": False}, r"qualifying with no size"),
    ],
)
def test_watch_log_poll_line_with_a_malformed_field_refuses(
    tmp_path: Path, override: dict[str, Any], message: str
) -> None:
    _write_log(tmp_path, _start(), _poll(**override))
    with pytest.raises(EngineError, match=message):
        read_watch_log(tmp_path)


def test_watch_log_line_without_a_timestamp_refuses(tmp_path: Path) -> None:
    start = _start()
    del start["at"]
    _write_log(tmp_path, start)
    with pytest.raises(EngineError, match="'start' line has no timestamp"):
        read_watch_log(tmp_path)


# --------------------------------------------------------- FileWatcherAdapter


class _StepClock:
    """A clock that never blocks: `sleep_until` jumps to the deadline and records it."""

    virtual = True

    def __init__(self, start: datetime = T0) -> None:
        self._now = start
        self.slept: list[datetime] = []

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, t: datetime) -> None:
        self.slept.append(t)
        self._now = max(self._now, t)


def _fw_ctx(run_root: Path, clock: _StepClock, *, run_id: str | None = "r1") -> AdapterContext:
    return AdapterContext(clock=clock, run_root=run_root, run_id=run_id)  # type: ignore[arg-type]


def _log_kinds(run_dir: Path) -> list[str]:
    return [json.loads(line)["kind"] for line in (run_dir / WATCH_LOG).read_text().splitlines()]


def test_fw_refuses_a_job_without_a_watch_spec(tmp_path: Path) -> None:
    ctx = _fw_ctx(tmp_path, _StepClock())
    with pytest.raises(EngineError, match="FW dispatch without an FwSpec"):
        asyncio.run(FileWatcherAdapter().run(_cmd_job(), 1, ctx))


def test_fw_watch_without_a_fence_or_barrier_logs_every_poll(tmp_path: Path) -> None:
    watched = tmp_path / "watched"
    watched.write_bytes(b"abcd")
    clock = _StepClock()
    result = asyncio.run(FileWatcherAdapter().run(_fw_job(watched), 1, _fw_ctx(tmp_path, clock)))
    assert result == 0
    run_dir = tmp_path / "runs" / "w.1"
    assert _log_kinds(run_dir) == ["start", "poll", "poll"]
    polls = [json.loads(line) for line in (run_dir / WATCH_LOG).read_text().splitlines()[1:]]
    assert [p["stable_polls"] for p in polls] == [1, 2]
    assert clock.slept == [T0, T0 + timedelta(seconds=30)]


def test_fw_resumed_complete_log_returns_success_without_polling(tmp_path: Path) -> None:
    watched = tmp_path / "watched"  # absent: a re-poll would not complete
    run_dir = tmp_path / "runs" / "w.1"
    _write_log(run_dir, _start(), _poll(0, stable_polls=1), _poll(30, stable_polls=2))
    clock = _StepClock()
    result = asyncio.run(FileWatcherAdapter().run(_fw_job(watched), 1, _fw_ctx(tmp_path, clock)))
    assert result == 0
    assert clock.slept == []
    assert _log_kinds(run_dir) == ["start", "poll", "poll"]


def test_fw_resumed_log_with_one_qualifying_poll_completes_only_under_immediate(
    tmp_path: Path,
) -> None:
    watched = tmp_path / "watched"
    watched.write_bytes(b"abcd")
    run_dir = tmp_path / "runs" / "w.1"
    _write_log(run_dir, _start(), _poll(0, stable_polls=1))

    immediate_clock = _StepClock()
    adapter = FileWatcherAdapter(existence="immediate")
    assert asyncio.run(adapter.run(_fw_job(watched), 1, _fw_ctx(tmp_path, immediate_clock))) == 0
    assert immediate_clock.slept == []
    assert _log_kinds(run_dir) == ["start", "poll"]

    stable_clock = _StepClock()
    stable = FileWatcherAdapter(existence="stable")
    assert asyncio.run(stable.run(_fw_job(watched), 1, _fw_ctx(tmp_path, stable_clock))) == 0
    assert len(stable_clock.slept) == 1  # it polled once more to see the second steady size
    assert _log_kinds(run_dir) == ["start", "poll", "poll"]


def test_fw_immediate_resume_completes_from_the_first_poll_only(tmp_path: Path) -> None:
    """DL-258: under `immediate` the file must already exist at the run's FIRST poll. A resumed
    watch whose first poll found nothing and whose second found the file still needs the
    second steady-size observation, so the resume does not complete from the log."""
    watched = tmp_path / "watched"
    watched.write_bytes(b"abcd")
    run_dir = tmp_path / "runs" / "w.1"
    _write_log(
        run_dir,
        _start(),
        _poll(0, exists=False, size=None, qualifying=False, stable_polls=0),
        _poll(30, stable_polls=1),
    )
    clock = _StepClock()
    adapter = FileWatcherAdapter(existence="immediate")
    assert asyncio.run(adapter.run(_fw_job(watched), 1, _fw_ctx(tmp_path, clock))) == 0
    assert len(clock.slept) == 1  # it observed once more before completing
    assert _log_kinds(run_dir) == ["start", "poll", "poll", "poll"]


# ------------------------------------------------------------- SupervisorConn


@contextlib.contextmanager
def _one_shot_server(short_root: Path, reply: bytes | None) -> Iterator[Path]:
    """A unix-socket server that accepts once, reads one request line, sends `reply`
    (nothing when None), and closes."""
    path = short_root / "supervisor.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(10)

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            if reply is not None:
                conn.sendall(reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        listener.close()
        thread.join(10)


def test_supervisor_conn_raises_when_the_supervisor_closes_without_replying(
    short_root: Path,
) -> None:
    with _one_shot_server(short_root, None) as path:
        conn = SupervisorConn(path, timeout_s=10)
        try:
            with pytest.raises(OSError, match="supervisor closed the connection"):
                conn.send({"cmd": "PING"})
        finally:
            conn.close()


def test_supervisor_conn_closes_its_socket_when_the_connect_fails(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[socket.socket] = []

    def make(family: int) -> socket.socket:
        made.append(socket.socket(family))
        return made[-1]

    monkeypatch.setattr(_adapters, "socket", SimpleNamespace(AF_UNIX=socket.AF_UNIX, socket=make))
    with pytest.raises(FileNotFoundError):
        SupervisorConn(short_root / "supervisor.sock")
    [sock] = made
    assert sock.fileno() == -1  # closed: no object was returned to close it


def test_supervisor_conn_refuses_a_reply_that_is_not_an_object(short_root: Path) -> None:
    with _one_shot_server(short_root, b"[1, 2]\n") as path:
        conn = SupervisorConn(path, timeout_s=10)
        try:
            with pytest.raises(EngineError, match="non-object reply"):
                conn.send({"cmd": "PING"})
        finally:
            conn.close()


def test_running_supervisor_deadman_refuses_a_socket_it_cannot_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_adapters, "SupervisorConn", deny)
    with pytest.raises(EngineError, match="cannot reach the supervisor"):
        running_supervisor_deadman(tmp_path)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b'{"ok": true, "deadman_s": 30}\n', (True, 30.0)),
        (b'{"ok": true, "deadman_s": 1.5}\n', (True, 1.5)),
        (b'{"ok": true, "deadman_s": null}\n', (True, None)),
        (b'{"ok": true, "deadman_s": true}\n', (True, None)),
        (b'{"ok": true}\n', (True, None)),
    ],
)
def test_running_supervisor_deadman_reads_a_number_and_nothing_else(
    short_root: Path, reply: bytes, expected: tuple[bool, float | None]
) -> None:
    root = short_root / "r"
    root.mkdir()
    with _one_shot_server(root, reply) as path:
        assert path == root / "supervisor.sock"
        assert running_supervisor_deadman(root) == expected


def test_running_supervisor_deadman_reports_nothing_listening(short_root: Path) -> None:
    assert running_supervisor_deadman(short_root) == (False, None)


# ------------------------------------------------------- SupervisorClient


class _FakeWriter:
    """A stream-writer stand-in. `replies` are fed to `stream` as each request is written."""

    def __init__(
        self,
        stream: asyncio.StreamReader | None = None,
        replies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stream = stream
        self.replies = list(replies or [])
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data))
        if self.stream is not None and self.replies:
            self.stream.feed_data(json.dumps(self.replies.pop(0)).encode() + b"\n")

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self.stream is not None:
            self.stream.feed_eof()

    async def wait_closed(self) -> None:
        pass


def test_a_ping_the_supervisor_refuses_is_not_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SupervisorClient(tmp_path)
    contacts: list[int] = []
    client.on_contact = lambda: contacts.append(1)
    client.sock_path.write_text("")  # only its existence is read
    writers: list[_FakeWriter] = []

    async def fake_open(path: str, limit: int | None = None) -> tuple[Any, Any]:
        stream = asyncio.StreamReader()
        writer = _FakeWriter(stream, [{"ok": False, "error": "unsupported_version"}])
        writers.append(writer)
        return stream, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", fake_open)

    async def scenario() -> bool:
        try:
            return await client._try_connect()
        finally:
            await client.close()

    assert asyncio.run(scenario()) is False
    assert [r["cmd"] for r in writers[0].sent] == ["PING"]
    assert contacts == []
    assert client.supervisor_deadman_s is None


def test_a_connection_lost_before_the_ping_is_answered_is_not_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _HangsUp(_FakeWriter):
        def write(self, data: bytes) -> None:
            super().write(data)
            assert self.stream is not None
            self.stream.feed_eof()  # the supervisor closes instead of answering

    client = SupervisorClient(tmp_path)
    client.sock_path.write_text("")  # only its existence is read
    writers: list[_HangsUp] = []

    async def fake_open(path: str, limit: int | None = None) -> tuple[Any, Any]:
        stream = asyncio.StreamReader()
        writer = _HangsUp(stream)
        writers.append(writer)
        return stream, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", fake_open)

    async def scenario() -> tuple[bool, str]:
        try:
            return await client._try_connect(), client.phase()
        finally:
            await client.close()

    assert asyncio.run(scenario()) == (False, "lost")
    assert [r["cmd"] for r in writers[0].sent] == ["PING"]


class _NeverStarted:
    def poll(self) -> int | None:
        return 1  # exited before publishing its socket


class _NeverPublishes:
    def poll(self) -> int | None:
        return None  # alive, and its socket never appears


def test_ensure_running_gives_up_after_the_spawn_attempt_limit(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)
    spawns: list[int] = []

    def spawn() -> _NeverStarted:
        spawns.append(1)
        return _NeverStarted()

    client._spawn_supervisor = spawn  # type: ignore[method-assign, assignment]
    with pytest.raises(SupervisorUnavailable, match=r"3 spawn attempt\(s\)"):
        asyncio.run(client.ensure_running())
    assert len(spawns) == _adapters._SPAWN_ATTEMPTS


def test_ensure_running_gives_up_when_the_window_passes_with_a_live_unpublished_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_monotonic(monkeypatch, 6.0)  # the 10 s window is gone after two probes
    client = SupervisorClient(tmp_path)
    spawns: list[int] = []

    def spawn() -> _NeverPublishes:
        spawns.append(1)
        return _NeverPublishes()

    client._spawn_supervisor = spawn  # type: ignore[method-assign, assignment]
    with pytest.raises(SupervisorUnavailable, match=r"1 spawn attempt\(s\)"):
        asyncio.run(client.ensure_running())
    assert spawns == [1]


@pytest.mark.parametrize(
    "step",
    [SupervisorUnavailable("synthetic outage"), {"ok": False, "error": "lease_held"}],
)
def test_reconnect_fails_when_the_reacquire_does_not_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: Any
) -> None:
    client = SupervisorClient(tmp_path)
    client.token = 7

    async def connects() -> bool:
        return True

    monkeypatch.setattr(client, "_try_connect", connects)
    requests = _ScriptedRequests([step])
    monkeypatch.setattr(client, "_request", requests)

    async def scenario() -> bool:
        try:
            return await client.reconnect()
        finally:
            await client.close()

    assert asyncio.run(scenario()) is False
    assert requests.commands == ["ACQUIRE"]
    assert client.token == 7  # a refused re-acquire changes nothing


def test_request_without_a_connection_is_unavailable(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)
    with pytest.raises(SupervisorUnavailable, match="not connected"):
        asyncio.run(client._request({"cmd": "PING"}, _connect=False))


def test_request_whose_drain_fails_is_unavailable_and_leaves_nothing_pending(
    tmp_path: Path,
) -> None:
    class _FailingDrain(_FakeWriter):
        async def drain(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    client = SupervisorClient(tmp_path)
    client._writer = _FailingDrain()  # type: ignore[assignment]
    with pytest.raises(SupervisorUnavailable, match="Broken pipe"):
        asyncio.run(client._request({"cmd": "PING"}, _connect=False))
    assert client._pending is None


def test_cancelling_a_request_before_its_reply_fails_the_pending_future(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class _StuckDrain(_FakeWriter):
        async def drain(self) -> None:
            entered.set()
            await asyncio.Event().wait()

    client = SupervisorClient(tmp_path)
    writer = _StuckDrain()
    client._writer = writer  # type: ignore[assignment]

    async def scenario() -> BaseException | None:
        task = asyncio.create_task(client._request({"cmd": "PING"}, _connect=False))
        await asyncio.wait_for(entered.wait(), 3)
        pending = client._pending
        assert pending is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pending.exception()  # also marks it retrieved

    failure = asyncio.run(scenario())
    assert isinstance(failure, SupervisorUnavailable)
    assert "poisoned" in str(failure)
    assert client.lost.is_set()
    assert client._pending is None
    assert writer.closed


def test_poisoning_a_closed_client_with_no_writer_only_marks_it_lost(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)
    asyncio.run(client.close())
    client._poison()
    assert client.lost.is_set()
    assert client.phase() == "closed"


def test_reader_skips_a_line_that_is_not_json_and_still_delivers_the_reply(
    tmp_path: Path,
) -> None:
    client = SupervisorClient(tmp_path)

    async def scenario() -> dict[str, Any]:
        wire = _ClientWire(client)
        try:
            task = asyncio.create_task(client._request({"cmd": "PING"}, _connect=False))
            await wire.expect("PING")
            wire.stream.feed_data(b"{not json\n")
            wire.reply({"ok": True, "marker": 1})
            return await asyncio.wait_for(task, 3)
        finally:
            await client.close()

    assert asyncio.run(scenario()) == {"ok": True, "marker": 1}


def test_reader_drops_a_reply_nobody_is_waiting_for(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)

    async def scenario() -> dict[str, Any]:
        wire = _ClientWire(client)
        try:
            exit_fut = client.exit_future("r1")
            wire.reply({"ok": True, "stray": True})
            wire.reply({"push": "exit", "run_id": "r1", "wrapper_rc": 0})
            await asyncio.wait_for(exit_fut, 3)  # the reader has passed the stray line
            task = asyncio.create_task(client._request({"cmd": "PING"}, _connect=False))
            await wire.expect("PING")
            wire.reply({"ok": True, "marker": 2})
            return await asyncio.wait_for(task, 3)
        finally:
            await client.close()

    assert asyncio.run(scenario()) == {"ok": True, "marker": 2}


def test_reader_that_hits_a_read_error_marks_the_connection_lost(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)

    async def scenario() -> str:
        wire = _ClientWire(client)
        try:
            wire.stream.set_exception(ConnectionResetError(54, "reset by peer"))
            await asyncio.wait_for(client.lost.wait(), 3)
            return client.phase()
        finally:
            await client.close()

    assert asyncio.run(scenario()) == "lost"


def test_an_exit_push_for_an_unknown_or_settled_run_changes_nothing(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)

    async def scenario() -> dict[str, Any]:
        client._deliver_push({"push": "exit", "run_id": "nobody"})
        assert "nobody" not in client._exit_futures
        fut = client.exit_future("r1")
        client._deliver_push({"push": "exit", "run_id": "r1", "wrapper_rc": 0})
        client._deliver_push({"push": "exit", "run_id": "r1", "wrapper_rc": 9})  # a repeat
        return fut.result()

    assert asyncio.run(scenario())["wrapper_rc"] == 0


def test_a_closed_client_arms_no_list_recheck(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)
    client._closed = True
    client._listed_dead["r1"] = asyncio.Event()
    client._arm_list_recheck()
    assert client._list_task is None
    assert not client._list_wakeup.is_set()


def test_list_recheck_loop_ends_without_asking_when_the_client_closes_during_its_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SupervisorClient(tmp_path)
    client._listed_dead["r1"] = asyncio.Event()
    monkeypatch.setattr(_adapters, "_LIST_RECHECK_EVERY", 0)
    asked: list[str] = []

    async def list_runs() -> None:
        asked.append("LIST")

    monkeypatch.setattr(client, "list_runs", list_runs)

    class _ClosesWhileWaiting:
        def clear(self) -> None:
            pass

        def set(self) -> None:
            pass

        async def wait(self) -> None:
            client._closed = True  # the close lands inside the wait

    client._list_wakeup = _ClosesWhileWaiting()  # type: ignore[assignment]
    asyncio.run(asyncio.wait_for(client._list_recheck_loop(), 3))
    assert asked == []


def test_acquire_refused_is_unavailable_and_holds_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SupervisorClient(tmp_path)
    monkeypatch.setattr(
        client, "_request", _ScriptedRequests([{"ok": False, "error": "lease_held"}])
    )
    with pytest.raises(SupervisorUnavailable, match="lease acquire refused"):
        asyncio.run(client.acquire())
    assert client.token is None
    assert client._renew_task is None


def _run_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: list[Any], *, report: bool
) -> tuple[list[str], list[str], bool]:
    """Acquire, then renew until `script` is spent. Returns (events, commands, loop done)."""
    monkeypatch.setattr(SupervisorClient, "_RENEW_EVERY_S", 0.0)
    monkeypatch.setattr(SupervisorClient, "_RETRY_EVERY_S", 0.0)

    async def scenario() -> tuple[list[str], list[str], bool]:
        client = SupervisorClient(tmp_path)
        events: list[str] = []
        client.on_contact = lambda: events.append("contact")
        if report:
            client.on_unreachable = lambda: events.append("unreachable")
        requests = _ScriptedRequests([_ACK, *script])
        monkeypatch.setattr(client, "_request", requests)
        try:
            await client.acquire()
            await asyncio.wait_for(requests.drained.wait(), 5)
            assert client._renew_task is not None
            return events, requests.commands, client._renew_task.done()
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_a_refused_reacquire_after_a_refused_renewal_is_a_failure_not_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = [{"ok": False, "error": "stale_token"}, {"ok": False, "error": "lease_held"}]
    events, commands, done = _run_renewal(tmp_path, monkeypatch, refused, report=True)
    assert commands[:3] == ["ACQUIRE", "RENEW", "ACQUIRE"]
    assert events == ["contact"]  # the first ACQUIRE only; the refused pair reports none
    assert done is False  # a failed renewal does not end the loop


def test_the_fifth_failed_renewal_with_no_report_hook_is_logged_and_renewal_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = [SupervisorUnavailable("synthetic outage") for _ in range(5)]
    events, commands, done = _run_renewal(tmp_path, monkeypatch, script, report=False)
    assert events == ["contact"]
    assert commands.count("RENEW") >= 5
    assert done is False
    err = capsys.readouterr().err
    assert "failed 5 times" in err
    assert "reporting the supervisor unreachable failed" not in err  # no hook: nothing is called


def test_a_refused_reacquire_leaves_the_held_token_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SupervisorClient(tmp_path)
    client.token, client.incarnation = 7, "inc-1"
    requests = _ScriptedRequests(
        [{"ok": False, "error": "stale_token"}, {"ok": False, "error": "lease_held"}]
    )
    monkeypatch.setattr(client, "_request", requests)
    reply = asyncio.run(client._renew_once(60.0))
    assert reply == {"ok": False, "error": "lease_held"}
    assert requests.commands == ["RENEW", "ACQUIRE"]
    assert (client.token, client.incarnation) == (7, "inc-1")


def test_spawn_refusal_in_progress_is_distinct_from_any_other_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SupervisorClient(tmp_path)
    monkeypatch.setattr(
        client,
        "_request",
        _ScriptedRequests(
            [
                {"ok": False, "error": "in_progress", "detail": "forked"},
                {"ok": False, "error": "collision", "detail": "dir exists"},
            ]
        ),
    )

    async def scenario() -> tuple[Exception, Exception]:
        with pytest.raises(SupervisorUnavailable) as first:
            await client.spawn({"job": "j"})
        with pytest.raises(SupervisorUnavailable) as second:
            await client.spawn({"job": "j"})
        return first.value, second.value

    in_progress, collision = asyncio.run(scenario())
    assert isinstance(in_progress, SpawnInProgress)
    assert str(in_progress) == "SPAWN refused: in_progress (forked)"
    assert not isinstance(collision, SpawnInProgress)
    assert str(collision) == "SPAWN refused: collision (dir exists)"


# ------------------------------------------------- SupervisedCommandAdapter


def test_supervised_adapter_refuses_a_context_without_a_run_root(tmp_path: Path) -> None:
    adapter = SupervisedCommandAdapter(SupervisorClient(tmp_path))
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=None)
    with pytest.raises(EngineError, match="needs a run_root"):
        asyncio.run(adapter.run(_cmd_job(), 1, ctx))


def test_await_outcome_reports_unobservable_when_the_exit_push_has_no_status_record(
    tmp_path: Path,
) -> None:
    client = SupervisorClient(tmp_path)
    adapter = SupervisedCommandAdapter(client)
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()

    async def scenario() -> Any:
        client.exit_future("rid").set_result({"push": "exit", "run_id": "rid", "wrapper_rc": 7})
        return await asyncio.wait_for(adapter._await_outcome("rid", run_dir, "j", 1), 5)

    result = asyncio.run(scenario())
    assert isinstance(result, Failed)
    assert result.cause == (
        "exit_status_unobservable (wrapper exited rc=7 without a status record)"
    )
    assert "rid" not in client._exit_futures


class _SignalClient:
    """Just the client surface `kill` and `_signal_when_addressable` use."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.signals: list[str] = []
        self.futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.forgotten: list[str] = []

    def exit_future(self, run_id: str) -> asyncio.Future[dict[str, Any]]:
        if run_id not in self.futures:
            self.futures[run_id] = asyncio.get_running_loop().create_future()
        return self.futures[run_id]

    def forget_exit(self, run_id: str) -> None:
        self.forgotten.append(run_id)

    async def signal(self, run_id: str, sig: str) -> dict[str, Any]:
        self.signals.append(sig)
        answer = self.answers.pop(0) if self.answers else {"ok": True}
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer(self)
            return {"ok": True}
        return answer


_NOT_READY = {"ok": False, "error": "not_ready"}


def test_signal_retries_while_the_run_is_not_addressable_yet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(SupervisedCommandAdapter, "_SPAWN_POLL_S", 0.0)
    client = _SignalClient([_NOT_READY, _NOT_READY, {"ok": True}])
    adapter = SupervisedCommandAdapter(client)  # type: ignore[arg-type]

    async def scenario() -> None:
        await adapter._signal_when_addressable("rid", "TERM", client.exit_future("rid"))

    asyncio.run(scenario())
    assert client.signals == ["TERM", "TERM", "TERM"]
    assert capsys.readouterr().err == ""


def test_signal_stops_retrying_once_the_run_has_reported_its_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _SignalClient([_NOT_READY])
    adapter = SupervisedCommandAdapter(client)  # type: ignore[arg-type]

    async def scenario() -> None:
        fut = client.exit_future("rid")
        fut.set_result({"push": "exit"})
        await adapter._signal_when_addressable("rid", "TERM", fut)

    asyncio.run(scenario())
    assert client.signals == ["TERM"]
    assert "TERM for run rid never became addressable" in capsys.readouterr().err


def test_signal_gives_up_loudly_after_the_spawn_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(SupervisedCommandAdapter, "_SPAWN_WINDOW_S", 0.0)
    client = _SignalClient([_NOT_READY, _NOT_READY])
    adapter = SupervisedCommandAdapter(client)  # type: ignore[arg-type]

    async def scenario() -> None:
        await adapter._signal_when_addressable("rid", "KILL", client.exit_future("rid"))

    asyncio.run(scenario())
    assert client.signals == ["KILL"]
    assert "KILL for run rid never became addressable" in capsys.readouterr().err


def test_kill_sends_nothing_further_when_the_supervisor_is_gone() -> None:
    client = _SignalClient([SupervisorUnavailable("gone")])
    adapter = SupervisedCommandAdapter(client, grace_seconds=0.05)  # type: ignore[arg-type]
    asyncio.run(adapter.kill("rid"))
    assert client.signals == ["TERM"]
    assert client.forgotten == ["rid"]


def test_kill_leaves_no_exit_future_behind_when_the_supervisor_is_gone(tmp_path: Path) -> None:
    client = SupervisorClient(tmp_path)  # a real client: it owns `_exit_futures`

    async def gone(run_id: str, sig: str) -> dict[str, Any]:
        raise SupervisorUnavailable("gone")

    client.signal = gone  # type: ignore[method-assign]
    asyncio.run(SupervisedCommandAdapter(client, grace_seconds=0.05).kill("rid"))
    assert client._exit_futures == {}


def test_kill_leaves_no_exit_future_behind_when_it_is_cancelled_mid_signal(
    tmp_path: Path,
) -> None:
    client = SupervisorClient(tmp_path)
    entered = asyncio.Event()

    async def stuck(run_id: str, sig: str) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    client.signal = stuck  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(SupervisedCommandAdapter(client).kill("rid"))
        await asyncio.wait_for(entered.wait(), 5)
        assert "rid" in client._exit_futures
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert client._exit_futures == {}


def test_cancelling_a_run_while_the_supervisor_is_gone_leaves_no_exit_future(
    tmp_path: Path,
) -> None:
    """The oracle-kill path: `_await_outcome` forgets its future when it unwinds, then `kill`
    registers a new one. Neither may outlive the run."""
    client = SupervisorClient(tmp_path)

    async def spawned(spec: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "wrapper_pid": 1}

    async def gone(run_id: str, sig: str) -> dict[str, Any]:
        raise SupervisorUnavailable("gone")

    client.spawn = spawned  # type: ignore[method-assign]
    client.signal = gone  # type: ignore[method-assign]
    adapter = SupervisedCommandAdapter(client, grace_seconds=0.05)
    ctx = AdapterContext(clock=VirtualClock(start=T0), run_root=tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(adapter.run(_cmd_job(), 1, ctx))
        while not client._listed_dead:  # `_await_outcome` is waiting on the run
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert client._exit_futures == {}
    assert client._listed_dead == {}


def test_kill_escalates_to_sigkill_when_the_exit_push_misses_the_grace() -> None:
    client = _SignalClient([])
    adapter = SupervisedCommandAdapter(client, grace_seconds=0.05)  # type: ignore[arg-type]
    asyncio.run(adapter.kill("rid"))
    assert client.signals == ["TERM", "KILL"]
    assert client.forgotten == ["rid"]


def test_kill_stops_at_sigterm_when_the_exit_push_arrives_within_the_grace() -> None:
    def exits(client: _SignalClient) -> None:
        client.futures["rid"].set_result({"push": "exit"})

    client = _SignalClient([exits])
    adapter = SupervisedCommandAdapter(client, grace_seconds=5.0)  # type: ignore[arg-type]
    asyncio.run(adapter.kill("rid"))
    assert client.signals == ["TERM"]
    assert client.forgotten == ["rid"]


# ----------------------------------------------------- _named, resolve_spool


def test_named_returns_a_status_that_names_the_run_and_refuses_a_stranger() -> None:
    status = {"run_id": "rid", "outcome": "exited", "exit_code": 0}
    assert _named(status, "rid", "j", 1) is status
    with pytest.raises(EngineError, match="refusing to consume a stranger's fate"):
        _named(status, "other", "j", 1)


def _spool(run_dir: Path, name: str, doc: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(json.dumps(doc))


_IDENT = {"job": "j", "run_number": 1}


def _spawn_doc() -> dict[str, Any]:
    """A spawn record whose wrapper and command ids name reaped processes."""
    command = _reaped_pid()
    return {
        **_IDENT,
        "run_id": "rid",
        "boot_id": "boot-A",
        "wrapper_pid": _reaped_pid(),
        "wrapper_start_time": "ticks:1",
        "command_pid": command,
        "command_pgid": command,
        "command_start_time": "ticks:2",
    }


@pytest.mark.parametrize("name", ["spawn.json", "status.json"])
def test_resolve_spool_refuses_a_record_that_names_another_run(tmp_path: Path, name: str) -> None:
    run_dir = tmp_path / "j.1"
    _spool(run_dir, name, {**_spawn_doc(), "run_id": "someone-else"})
    with pytest.raises(EngineError, match=rf"the spool's {name} reports run_id 'someone-else'"):
        asyncio.run(
            resolve_spool(
                "j",
                1,
                run_dir,
                "boot-A",
                settle_seconds=0.0,
                grace_seconds=0.0,
                expected_run_id="rid",
            )
        )


def test_resolve_spool_reads_a_status_written_as_the_wrapper_died(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "j.1"
    spawn = _spawn_doc()
    _spool(run_dir, "spawn.json", spawn)
    sent = _record_signals(monkeypatch, on={})
    probes: list[int] = []

    def verify(pid: int, token: str) -> bool:
        probes.append(pid)
        if len(probes) == 1:
            return True  # the wrapper is alive when the ladder meets it
        _spool(
            run_dir,
            "status.json",
            {**_IDENT, "run_id": "rid", "outcome": "exited", "exit_code": 4},
        )
        return False  # it records, then dies

    monkeypatch.setattr(_procid, "verify_alive", verify)
    result = asyncio.run(
        resolve_spool(
            "j",
            1,
            run_dir,
            "boot-A",
            settle_seconds=5.0,
            grace_seconds=5.0,
            expected_run_id="rid",
        )
    )
    assert result == (4, None)
    assert probes == [spawn["wrapper_pid"]] * 2
    assert sent == []


def test_resolve_spool_kills_a_surviving_group_when_the_wrapper_never_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "j.1"
    spawn = _spawn_doc()
    _spool(run_dir, "spawn.json", spawn)
    sent = _record_signals(monkeypatch, on={})
    monkeypatch.setattr(_procid, "verify_alive", lambda pid, token: True)
    result, ended_at = asyncio.run(
        resolve_spool(
            "j",
            1,
            run_dir,
            "boot-A",
            settle_seconds=0.05,
            grace_seconds=0.15,
            expected_run_id="rid",
        )
    )
    assert result == Terminated("wrapper lost; killed at resume")
    assert ended_at is None
    pgid = spawn["command_pgid"]
    assert sent == [(pgid, signal.SIGTERM), (pgid, signal.SIGKILL)]
