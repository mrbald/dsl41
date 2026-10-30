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
import socket
import sys
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41.canon import ARTIFACT_FORMAT_VERSION
from dsl41.ir import JobIR, lower_source
from dsl41.runner_adapters import (
    WATCH_LOG,
    AdapterContext,
    FileWatcherAdapter,
    _build_run_spec,
    job_log_paths,
    load_json,
    read_watch_log,
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


class _NeverStarted:
    def poll(self) -> int | None:
        return 1  # exited before publishing its socket


class _NeverPublishes:
    def poll(self) -> int | None:
        return None  # alive, and its socket never appears


# ------------------------------------------------- SupervisedCommandAdapter


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


# ------------------------------------------------------------- load_json


def test_load_json_reads_a_spool_version_it_does_not_implement_as_unreadable(
    tmp_path: Path,
) -> None:
    """The `Wrapper-owned spool files` row is strict on versions: a version this
    binary does not implement reads as None, the same as an unreadable file. The
    version it implements, and an absent one, read through."""
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"version": 2, "outcome": "exited", "exit_code": 0}))
    assert load_json(path) is None
    path.write_text(json.dumps({"version": 1, "outcome": "exited", "exit_code": 0}))
    assert load_json(path) == {"version": 1, "outcome": "exited", "exit_code": 0}
    path.write_text(json.dumps({"outcome": "exited", "exit_code": 0}))
    assert load_json(path) == {"outcome": "exited", "exit_code": 0}
