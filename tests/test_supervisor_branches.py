"""Branch tests for `dsl41.runner_supervisor` that the supervisor, backlog and
idempotency suites do not reach (DL-42, DL-105, DL-129, DL-210, DL-265).

Normative spec: `docs/supervisor-protocol.md` ss5 (the verbs and their
refusals, SHUTDOWN, SIGTERM and SIGINT) and ss11a of `docs/period-model.md`
(the tombstone table that answers a replayed SPAWN). Everything
runs in this process against real sockets and real directories. Platform arms
(peer credentials, the Linux subreaper) are driven through stand-ins for the
module and `ctypes`, so the numbers do not depend on the host. Time is a fake
clock where a wait must run out. Each refusal has a twin that does not trigger
it.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import struct
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_runner_supervisor import _exchange

from dsl41 import runner_supervisor as rs


def _run_id() -> str:
    return str(uuid.uuid4())


def _spec(sup: rs.Supervisor, run_id: str, job: str = "j", number: int = 1, **over: Any) -> dict:
    run_dir = sup.run_dir_for(job, number)
    spec: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "job": job,
        "run_number": number,
        "command": "exit 0",
        "run_dir": run_dir,
        "stdout_path": run_dir + "/out.log",
        "stderr_path": run_dir + "/err.log",
        "stdin_path": None,
        "grace_seconds": 1.0,
    }
    spec.update(over)
    return spec


def _index(sup: rs.Supervisor, run_id: str, job: str = "j", number: int = 1) -> None:
    os.makedirs(os.path.dirname(sup.index_path(run_id)), exist_ok=True)
    rs._write_canonical(
        sup.index_path(run_id),
        {
            "artifact_format_version": rs.ARTIFACT_FORMAT_VERSION,
            "job": job,
            "run_id": run_id,
            "run_number": number,
        },
    )


def _receipt(directory: str, run_id: str, fingerprint: str) -> None:
    os.makedirs(directory, exist_ok=True)
    rs._write_canonical(
        os.path.join(directory, "receipt.json"),
        {
            "artifact_format_version": rs.ARTIFACT_FORMAT_VERSION,
            "received_at": "2026-01-01T00:00:00+00:00",
            "run_id": run_id,
            "spec_fingerprint": fingerprint,
        },
    )


def _fake_run(sup: rs.Supervisor, run_id: str, run_dir: Path, **over: Any) -> rs._Run:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "job": "j",
        "run_number": 1,
        "run_dir": str(run_dir),
        "wrapper_pid": 424242,
        "lifeline_w": -1,
        "spawned_at": "2026-01-01T00:00:00+00:00",
        "grace_seconds": 1.0,
    }
    fields.update(over)
    run = rs._Run(**fields)
    sup.runs[run_id] = run
    return run


# ------------------------------------------------------------ fingerprint


def test_sv_fingerprint_tags_floats_inside_lists_and_dicts() -> None:
    """A float anywhere in the spec is written as its exact bits, so two specs
    differing only in a nested float do not share a fingerprint. The
    `lifeline_fd` the supervisor fills in never counts."""
    assert rs._fingerprint_form([1.5, {"x": 2.0}, "s", 3]) == [
        "float:" + (1.5).hex(),
        {"x": "float:" + (2.0).hex()},
        "s",
        3,
    ]
    assert rs.spec_fingerprint({"a": [1.5]}) != rs.spec_fingerprint({"a": [2.5]})
    assert rs.spec_fingerprint({"a": [1.5]}) == rs.spec_fingerprint({"a": [1.5], "lifeline_fd": 9})


# -------------------------------------------------------------- peer_uid


class _CredSocket:
    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.args: tuple[Any, ...] = ()

    def getsockopt(self, *args: Any) -> bytes:
        self.args = args
        return self.reply


def test_sv_peer_uid_reads_so_peercred_where_the_socket_module_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rs, "socket", SimpleNamespace(SO_PEERCRED=17, SOL_SOCKET=1))
    sock = _CredSocket(struct.pack("iII", 4242, 501, 20))
    assert rs.peer_uid(sock) == 501  # type: ignore[arg-type]
    assert sock.args == (1, 17, struct.calcsize("iII"))


def test_sv_peer_uid_reads_the_xucred_uid_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SO_PEERCRED: macOS asks LOCAL_PEERCRED and takes `cr_uid`, the
    second unsigned int of a 76-byte `xucred`."""
    monkeypatch.setattr(rs, "socket", SimpleNamespace(SOL_SOCKET=1))
    monkeypatch.setattr(rs, "sys", SimpleNamespace(platform="darwin"))
    sock = _CredSocket(struct.pack("=II", 0, 777) + bytes(68))
    assert rs.peer_uid(sock) == 777  # type: ignore[arg-type]
    assert sock.args == (0, 1, 76)


# ----------------------------------------------------- _set_subreaper


def test_sv_subreaper_is_a_linux_prctl_and_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux asks the kernel for PR_SET_CHILD_SUBREAPER; any other platform
    does nothing; a missing libc or a libc without `prctl` is tolerated."""
    sup = rs.Supervisor.__new__(rs.Supervisor)
    calls: list[tuple[Any, ...]] = []

    class Libc:
        def prctl(self, *args: Any) -> int:
            calls.append(args)
            return 0

    def cdll(name: str, use_errno: bool) -> Libc:
        calls.append((name, use_errno))
        return Libc()

    monkeypatch.setattr(rs, "ctypes", SimpleNamespace(CDLL=cdll))
    monkeypatch.setattr(rs, "sys", SimpleNamespace(platform="darwin"))
    sup._set_subreaper()
    assert calls == []

    monkeypatch.setattr(rs, "sys", SimpleNamespace(platform="linux"))
    sup._set_subreaper()
    assert calls == [("libc.so.6", True), (36, 1, 0, 0, 0)]

    def missing(name: str, use_errno: bool) -> Libc:
        calls.append((name, use_errno))
        raise OSError("no libc")

    calls.clear()
    monkeypatch.setattr(rs, "ctypes", SimpleNamespace(CDLL=missing))
    sup._set_subreaper()  # no libc: tolerated, and no prctl was attempted
    assert calls == [("libc.so.6", True)]

    class NoPrctl:
        pass

    def cdll_without_prctl(name: str, use_errno: bool) -> NoPrctl:
        calls.append((name, use_errno))
        return NoPrctl()

    calls.clear()
    monkeypatch.setattr(rs, "ctypes", SimpleNamespace(CDLL=cdll_without_prctl))
    sup._set_subreaper()  # libc without `prctl`: AttributeError, tolerated
    assert calls == [("libc.so.6", True)]


# ----------------------------------------------------- signal handlers


def test_sv_signal_handlers_swallow_a_full_wake_up_pipe(queued_supervisor) -> None:
    """The handlers only note the signal. When the self-pipe is full, the
    SIGTERM latch is still set, nothing raises and nothing is added to the
    pipe; with room in the pipe the bytes arrive."""
    sup, _ = queued_supervisor
    sup._on_chld_signal(signal.SIGCHLD, None)
    sup._on_term_signal(signal.SIGTERM, None)
    assert os.read(sup._chld_r, 8) == b"ct"
    assert sup._shutdown_requested is True

    sup._shutdown_requested = False
    filled = 0
    try:
        while True:
            filled += os.write(sup._chld_w, b"x" * 4096)
    except BlockingIOError:
        pass
    sup._on_chld_signal(signal.SIGCHLD, None)
    sup._on_term_signal(signal.SIGTERM, None)
    assert sup._shutdown_requested is True
    drained = b""
    try:
        while True:
            drained += os.read(sup._chld_r, 65536)
    except BlockingIOError:
        pass
    assert drained == b"x" * filled  # the refused bytes were not queued


# ------------------------------------------------------------ _accept


def test_sv_accept_ignores_a_transient_error_without_logging(
    queued_supervisor, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only descriptor exhaustion is logged. Another accept error (the
    listener had nothing ready) just returns; no connection appears."""
    sup, _ = queued_supervisor

    class Idle:
        def accept(self) -> Any:
            raise BlockingIOError(errno.EAGAIN, "nothing ready")

    sup._listen = Idle()  # type: ignore[assignment]
    try:
        sup._accept()
    finally:
        sup._listen = None
    assert capsys.readouterr().err == "" and not sup._conns


# ---------------------------------------------- connection bookkeeping


def test_sv_a_dropped_connection_takes_no_more_frames(queued_supervisor) -> None:
    sup, connect = queued_supervisor
    conn, peer = connect()
    sup._drop_conn(conn)
    sup._send(conn, {"ok": True})
    assert not conn.out and conn.backlog == 0
    sup._writable(conn)  # a dropped connection has nothing to flush either
    peer.settimeout(0.2)
    assert peer.recv(16) == b""


def test_sv_resuming_a_paused_reader_that_hit_eof_drops_it_without_rearming(
    queued_supervisor,
) -> None:
    """The output pause clears, the buffered reads resume, and the peer has
    already closed its side: the connection is dropped by that read, so the
    flush does not re-arm interest on a socket that is gone."""
    sup, connect = queued_supervisor
    conn, peer = connect()
    sup._backlog_bytes = 10
    sup._send(conn, {"ok": True, "pad": "x" * 50})
    assert conn.paused
    peer.shutdown(socket.SHUT_WR)  # the peer stops sending, still reads
    sup._writable(conn)
    assert not sup._connected(conn)
    assert json.loads(peer.recv(4096))["ok"] is True  # the frame was flushed first


def test_sv_buffered_requests_wait_unless_the_process_is_serving(queued_supervisor) -> None:
    """A connection whose earlier output pause cleared can hold complete
    request lines in its buffer when the loop reads it next. Outside
    `serving` none of them is dispatched, not even the first: an error that
    ended a SHUTDOWN's wait leaves the process `shutting_down` for the rest
    of the loop pass, and a request read then must not run. In `serving`
    the same buffer is dispatched in order."""
    sup, connect = queued_supervisor
    conn, _peer = connect()
    lines = b'{"v":1,"cmd":"PING"}\n{"v":1,"cmd":"LIST"}\n'
    for state in ("starting", "shutting_down", "stopped"):
        sup.state = state
        conn.buf = lines
        sup._readable(conn)
        assert not conn.out and conn.buf == lines, state
    sup.state = "serving"
    sup._readable(conn)
    assert [json.loads(frame)["ok"] for frame, _ in conn.out] == [True, True]
    assert conn.buf == b""


def test_sv_a_blank_line_is_ignored_and_a_request_is_answered(queued_supervisor) -> None:
    sup, connect = queued_supervisor
    conn, _peer = connect()
    sup._dispatch(conn, b"  \t ")
    assert not conn.out
    sup._dispatch(conn, b'{"v":1,"cmd":"PING"}')
    assert len(conn.out) == 1


# -------------------------------------------------------- _answer


def test_sv_answer_refuses_what_is_not_a_request(queued_supervisor) -> None:
    sup, connect = queued_supervisor
    conn, _peer = connect()
    answer = sup._answer
    assert answer(conn, b"[1, 2]") == {"ok": False, "error": "malformed_json"}
    assert answer(conn, b'"PING"') == {"ok": False, "error": "malformed_json"}
    assert answer(conn, b'{"v": 2, "cmd": "PING"}') == {"ok": False, "error": "unsupported_version"}
    assert answer(conn, b'{"v": true, "cmd": "PING"}')["error"] == "unsupported_version"
    assert answer(conn, b'{"v": 1, "cmd": "NOPE"}') == {"ok": False, "error": "unknown_verb"}
    assert answer(conn, b'{"v": 1, "cmd": 5}') == {"ok": False, "error": "unknown_verb"}
    assert answer(conn, b'{"v": 1}') == {"ok": False, "error": "unknown_verb"}
    assert answer(conn, b'{"v": 1, "cmd": "PING"}')["ok"] is True


def test_sv_a_handler_that_raises_is_answered_and_the_next_request_is_served(
    queued_supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The belt under every verb: an unexpected error is one request's
    problem, not the process's."""
    sup, connect = queued_supervisor
    conn, _peer = connect()

    def boom(_conn: Any, _req: dict) -> dict:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(sup, "_h_ping", boom)
    assert sup._answer(conn, b'{"v": 1, "cmd": "PING"}') == {
        "ok": False,
        "error": "internal: RuntimeError: synthetic",
    }
    assert sup._answer(conn, b'{"v": 1, "cmd": "LIST"}')["ok"] is True


# ----------------------------------------------------- lease verbs


def test_sv_lease_verbs_refuse_bad_ids_and_stale_tokens_and_change_nothing(
    queued_supervisor,
) -> None:
    sup, connect = queued_supervisor
    conn, peer = connect()
    for bad in ({}, {"controller_id": ""}, {"controller_id": 7}):
        reply = _exchange(sup, conn, peer, {"cmd": "ACQUIRE", **bad})
        assert reply == {"ok": False, "error": "bad_controller_id"}
    assert sup.lease is None

    granted = _exchange(sup, conn, peer, {"cmd": "ACQUIRE", "controller_id": "e"})
    token, inc = granted["token"], granted["incarnation"]
    stale = {"token": token + 1, "incarnation": inc}
    assert _exchange(sup, conn, peer, {"cmd": "RELEASE", **stale})["error"] == "stale_token"
    assert sup.lease is not None and sup.lease.token == token  # still held
    wrong = {"token": token, "incarnation": "another"}
    assert _exchange(sup, conn, peer, {"cmd": "RELEASE", **wrong})["error"] == "wrong_incarnation"
    assert _exchange(sup, conn, peer, {"cmd": "SHUTDOWN", **stale})["error"] == "stale_token"
    assert sup.state == "serving"  # the refused SHUTDOWN stopped nothing
    sig = {"cmd": "SIGNAL", "run_id": "r", "sig": "TERM"}
    assert _exchange(sup, conn, peer, {**sig, **stale})["error"] == "stale_token"
    assert _exchange(sup, conn, peer, {"cmd": "SPAWN", "spec": {}, **stale})["error"] == (
        "stale_token"
    )
    assert (
        _exchange(
            sup, conn, peer, {"cmd": "SPAWN", "spec": [], "token": token, "incarnation": inc}
        )["detail"]
        == "spec is not an object"
    )
    ok = {"cmd": "RELEASE", "token": token, "incarnation": inc}
    assert _exchange(sup, conn, peer, ok) == {"ok": True}
    assert sup.lease is None


def test_sv_signal_refuses_an_unknown_signal_and_an_unknown_run(queued_supervisor) -> None:
    sup, connect = queued_supervisor
    conn, peer = connect()
    granted = _exchange(sup, conn, peer, {"cmd": "ACQUIRE", "controller_id": "e"})
    auth = {"token": granted["token"], "incarnation": granted["incarnation"]}
    for sig in ("HUP", 15, None):
        reply = _exchange(sup, conn, peer, {"cmd": "SIGNAL", "run_id": "r", "sig": sig, **auth})
        assert reply == {"ok": False, "error": "bad_signal"}
    for run_id in ("nope", 5):
        reply = _exchange(
            sup, conn, peer, {"cmd": "SIGNAL", "run_id": run_id, "sig": "KILL", **auth}
        )
        assert reply == {"ok": False, "error": "unknown_run"}


# ------------------------------------------------------- spawn_run


def test_sv_spawn_refuses_a_missing_run_id_and_a_non_path_run_dir(queued_supervisor) -> None:
    sup, _ = queued_supervisor
    spec = _spec(sup, _run_id())
    assert sup.spawn_run({k: v for k, v in spec.items() if k != "run_id"}) == {
        "ok": False,
        "error": "bad_spec",
        "detail": "spec.run_id is missing",
    }
    assert sup.spawn_run({**spec, "run_dir": 5})["detail"] == "spec.run_dir is not a path"
    assert sup.spawn_run({**spec, "run_dir": spec["run_dir"] + "\x00"})["error"] == "bad_spec"
    assert not os.path.exists(spec["run_dir"])  # nothing durable was made


def test_sv_spawn_refuses_a_spec_ss3_2_cannot_hold(queued_supervisor) -> None:
    """A lone surrogate is a valid JSON string and not a canonical one, so no
    receipt could be written for it: bad_spec, before anything is created."""
    sup, _ = queued_supervisor
    spec = _spec(sup, _run_id(), command="echo \ud800")
    reply = sup.spawn_run(spec)
    assert reply["error"] == "bad_spec" and reply["detail"].startswith("unfingerprintable spec")
    assert not os.path.exists(spec["run_dir"])


def test_sv_spec_schema_names_the_first_fault() -> None:
    good = {
        "version": 1,
        "run_id": "r",
        "job": "j",
        "run_number": 1,
        "command": "c",
        "run_dir": "/d",
        "stdout_path": "/o",
        "stderr_path": "/e",
        "stdin_path": None,
        "grace_seconds": 1,
    }
    assert rs._spec_schema_error(good) is None  # lifeline_fd is optional
    assert rs._spec_schema_error({**good, "lifeline_fd": 3}) is None
    assert rs._spec_schema_error({k: v for k, v in good.items() if k != "command"}) == (
        "spec.command is missing"
    )
    assert rs._spec_schema_error({**good, "extra": 1}) == "unknown spec key 'extra'"
    assert rs._spec_schema_error({**good, "run_number": True}) == (
        "spec.run_number has the wrong type"
    )
    assert rs._spec_schema_error({**good, "command": "a\x00b"}) == "spec.command holds a NUL"


# --------------------------------------------- the ss11a answer table


def test_sv_replay_with_an_unreadable_receipt_and_no_index_is_indeterminate(
    queued_supervisor,
) -> None:
    sup, _ = queued_supervisor
    spec = _spec(sup, _run_id())
    os.makedirs(spec["run_dir"])
    Path(spec["run_dir"], "receipt.json").write_text("{not canonical")
    reply = sup.spawn_run(spec)
    assert reply["error"] == "indeterminate" and "receipt.json is unreadable" in reply["detail"]
    assert not os.path.exists(sup.index_path(spec["run_id"]))  # no index was invented


def test_sv_an_orphan_directory_is_not_reused_when_the_index_cannot_be_listed(
    queued_supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, _ = queued_supervisor
    spec = _spec(sup, _run_id())
    os.makedirs(spec["run_dir"])
    os.makedirs(os.path.dirname(sup.index_path(spec["run_id"])))
    real_listdir = os.listdir

    def listdir(path: Any = ".") -> list[str]:
        if str(path).endswith(rs._INDEX_DIR):
            raise PermissionError(errno.EACCES, "denied")
        return real_listdir(path)

    monkeypatch.setattr(rs.os, "listdir", listdir)
    reply = sup.spawn_run(spec)
    assert reply["error"] == "indeterminate" and "cannot be listed" in reply["detail"]
    monkeypatch.undo()
    assert rs.Supervisor._indexed_owner(sup, "j", 1) is None  # listable: no owner


def test_sv_index_scan_skips_other_runs_and_stops_at_a_disowning_entry(
    queued_supervisor,
) -> None:
    """An orphan directory is reused only when no index entry claims it. An
    entry for another (job, number) is passed over; an entry whose own run_id
    disagrees with its name blocks reuse, because it might claim anything."""
    sup, _ = queued_supervisor
    spec = _spec(sup, _run_id())
    os.makedirs(spec["run_dir"])
    other = _run_id()
    _index(sup, other, job="elsewhere", number=7)
    assert sup._indexed_owner("j", 1) is None
    claimant = _run_id()
    _index(sup, claimant, job="j", number=1)
    assert sup._indexed_owner("j", 1) == claimant

    reply = sup.spawn_run(spec)
    assert reply["error"] == "collision" and claimant in reply["detail"]

    os.unlink(sup.index_path(claimant))
    liar = _run_id()
    disowned = {
        "artifact_format_version": rs.ARTIFACT_FORMAT_VERSION,
        "job": "j",
        "run_id": other,
        "run_number": 1,
    }
    rs._write_canonical(sup.index_path(liar), disowned)
    assert sup._indexed_owner("j", 1) == rs._UNREADABLE
    assert sup.spawn_run(spec)["error"] == "indeterminate"


def _answer_table_case(sup: rs.Supervisor, run_id: str) -> tuple[dict, str]:
    """An indexed run with a directory: the state `_answer_from_directory`
    reads. Returns the spec and its fingerprint."""
    spec = _spec(sup, run_id)
    os.makedirs(spec["run_dir"])
    _index(sup, run_id)
    return spec, rs.spec_fingerprint(spec)


def test_sv_indexed_replay_refuses_unreadable_foreign_or_mismatched_receipts(
    queued_supervisor,
) -> None:
    sup, _ = queued_supervisor
    run_id = _run_id()
    spec, fingerprint = _answer_table_case(sup, run_id)
    receipt = Path(spec["run_dir"], "receipt.json")

    receipt.write_text("{not canonical")
    reply = sup.spawn_run(spec)
    assert reply["error"] == "indeterminate" and "receipt.json is unreadable" in reply["detail"]

    receipt.unlink()
    _receipt(spec["run_dir"], _run_id(), fingerprint)  # someone else's receipt at our path
    reply = sup.spawn_run(spec)
    assert reply["error"] == "collision" and "holds a receipt for run_id" in reply["detail"]

    receipt.unlink()
    _receipt(spec["run_dir"], run_id, "sha256:other")
    reply = sup.spawn_run(spec)
    assert reply["error"] == "collision" and "different spec fingerprint" in reply["detail"]


def test_sv_indexed_replay_ignores_an_unreadable_spawn_record(queued_supervisor) -> None:
    """An unreadable `spawn.json` is not evidence: with no reply and no live
    wrapper the answer is indeterminate rather than an invented pid. The
    twin has a readable record naming the run, and answers from it."""
    sup, _ = queued_supervisor
    run_id = _run_id()
    spec, fingerprint = _answer_table_case(sup, run_id)
    _receipt(spec["run_dir"], run_id, fingerprint)
    spawn = Path(spec["run_dir"], "spawn.json")
    spawn.write_text("{not json")
    reply = sup.spawn_run(spec)
    assert reply["error"] == "indeterminate"
    assert "nothing alive" in reply["detail"]

    spawn.write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": run_id,
                "job": "j",
                "run_number": 1,
                "wrapper_pid": 77,
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    reply = sup.spawn_run(spec)
    assert reply["ok"] is True and reply["duplicate"] is True and reply["wrapper_pid"] == 77


def test_sv_duplicate_envelope_needs_an_integer_pid_and_a_string_time() -> None:
    doc = {"run_id": "r", "wrapper_pid": 5, "spawned_at": "t"}
    assert rs._duplicate_from("r", doc, "wrapper_pid", "spawned_at") == {
        "ok": True,
        "run_id": "r",
        "wrapper_pid": 5,
        "spawned_at": "t",
        "duplicate": True,
    }
    assert rs._duplicate_from("r", None, "wrapper_pid", "spawned_at") is None
    assert rs._duplicate_from("other", doc, "wrapper_pid", "spawned_at") is None
    assert rs._duplicate_from("r", {**doc, "wrapper_pid": "5"}, "wrapper_pid", "spawned_at") is None
    assert (
        rs._duplicate_from("r", {**doc, "wrapper_pid": True}, "wrapper_pid", "spawned_at") is None
    )
    assert rs._duplicate_from("r", {**doc, "spawned_at": 3}, "wrapper_pid", "spawned_at") is None


# ----------------------------------------------- the first application


def test_sv_a_receipt_that_cannot_be_written_means_no_spawn(
    queued_supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt before fork: when the receipt write fails, the answer is
    `spawn_failed` and no wrapper was started."""
    sup, _ = queued_supervisor
    real_write = rs._write_canonical

    def write(path: str, record: dict) -> None:
        if path.endswith("receipt.json"):
            raise OSError(errno.ENOSPC, "no space")
        real_write(path, record)

    def never(_spec: dict) -> tuple[int, int]:
        raise AssertionError("the wrapper must not be forked")

    monkeypatch.setattr(rs, "_write_canonical", write)
    monkeypatch.setattr(sup, "_spawn_wrapper", never)
    reply = sup.spawn_run(_spec(sup, _run_id()))
    assert reply["ok"] is False and reply["error"].startswith("spawn_failed: ")
    assert sup.runs == {}


def test_sv_a_reply_record_that_cannot_be_written_does_not_lose_the_run(
    queued_supervisor,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The wrapper is already running, so the answer stands; a replay
    reconstructs it from the wrapper's own record. The failure is logged."""
    sup, _ = queued_supervisor
    real_write = rs._write_canonical
    lifeline_r, lifeline_w = os.pipe()

    def write(path: str, record: dict) -> None:
        if path.endswith("reply.json"):
            raise OSError(errno.EIO, "synthetic")
        real_write(path, record)

    monkeypatch.setattr(rs, "_write_canonical", write)
    monkeypatch.setattr(sup, "_spawn_wrapper", lambda _spec: (4242, lifeline_w))
    run_id = _run_id()
    try:
        reply = sup.spawn_run(_spec(sup, run_id))
    finally:
        os.close(lifeline_r)
        os.close(lifeline_w)
    assert reply["ok"] is True and reply["wrapper_pid"] == 4242 and "duplicate" not in reply
    assert sup.runs[run_id].wrapper_pid == 4242
    assert not Path(sup.run_dir_for("j", 1), "reply.json").exists()
    assert f"reply.json for {run_id} not written" in capsys.readouterr().err


# ---------------------------------------------------- _signal_command


def test_sv_signal_command_reads_only_a_record_that_names_the_run(
    queued_supervisor, short_root: Path
) -> None:
    sup, _ = queued_supervisor
    run_dir = short_root / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    run = _fake_run(sup, "rid", run_dir)
    # a pid and group that belonged to a child this test started and reaped:
    # if the identity check ever regressed, nothing live would be signalled
    gone = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    gone.wait()
    assert sup._signal_command(run, signal.SIGTERM) == "not_ready"  # alive, no record yet
    run.wrapper_rc = 0
    assert sup._signal_command(run, signal.SIGTERM) == "noop"  # dead, never recorded

    spawn = run_dir / "spawn.json"
    base = {"run_id": "rid", "job": "j", "run_number": 1}
    base |= {"command_pid": gone.pid, "command_pgid": gone.pid}
    spawn.write_text(json.dumps({**base, "job": "other", "command_start_time": "ticks:1"}))
    assert sup._signal_command(run, signal.SIGTERM) == "noop"  # names another run
    spawn.write_text(
        json.dumps({**base, "command_pid": str(gone.pid), "command_start_time": "ticks:1"})
    )
    assert sup._signal_command(run, signal.SIGTERM) == "noop"  # pid is not an integer
    spawn.write_text(json.dumps({**base, "command_start_time": None}))
    assert sup._signal_command(run, signal.SIGTERM) == "noop"  # no start token


# ------------------------------------------------------ shutdown


def test_sv_shutdown_schedule_for_a_wrapper_that_never_records(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live wrapper writes no `spawn.json`, so there is no group to signal
    and every attempt answers `not_ready`: nothing is killed. What this holds
    is the schedule. The spawn-record wait runs out its five seconds, then the
    TERM attempt; the per-run KILL attempt follows when the grace has passed,
    and the last-resort KILL attempt when the deadline has. Time is a fake
    clock."""
    sup = rs.Supervisor(str(short_root))
    now = [1000.0]

    def sleep(delay: float) -> None:
        now[0] += delay

    monkeypatch.setattr(
        rs, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep, time=time.time)
    )
    reaps: list[float] = []
    monkeypatch.setattr(sup, "_reap", lambda: reaps.append(now[0]))
    sent: list[tuple[str, float]] = []
    real_signal = sup._signal_command

    def spy(run: Any, sig: int) -> str:
        outcome = real_signal(run, sig)
        sent.append((outcome, now[0]))
        return outcome

    monkeypatch.setattr(sup, "_signal_command", spy)
    lifeline_r, lifeline_w = os.pipe()
    sup._bind()
    try:
        run = _fake_run(sup, "rid", short_root / "runs" / "j.1", lifeline_w=lifeline_w)
        sup._orderly_shutdown(rs.PROCESS_SHUTDOWN)
        assert sup.state == "stopped" and run.killed
        # TERM after the 5 s record wait, KILL at TERM + grace (1 s), KILL again
        # past TERM + grace + 2 s
        assert [outcome for outcome, _ in sent] == ["not_ready"] * 3
        term_at, kill_at, last_at = (t for _, t in sent)
        assert term_at - 1000.0 >= 5.0
        assert kill_at - term_at >= 1.0
        assert last_at > term_at + 1.0 + 2.0
        assert reaps  # every wait step reaps
    finally:
        sup._teardown()  # closes the lifeline of a wrapper that never exited
        os.close(lifeline_r)
    assert sup.state == "closed"


# ------------------------------------------------------ reaping


def test_sv_drain_chld_stops_at_end_of_file(queued_supervisor) -> None:
    """The self-pipe is drained until it is empty (EAGAIN) or its writer is
    gone (EOF)."""
    sup, _ = queued_supervisor
    os.write(sup._chld_w, b"cc")
    sup._drain_chld()
    with pytest.raises(BlockingIOError):
        os.read(sup._chld_r, 1)

    real = sup._chld_r
    r, w = os.pipe()
    os.set_blocking(r, False)
    os.write(w, b"cc")
    os.close(w)
    sup._chld_r = r
    try:
        sup._drain_chld()
        assert os.read(r, 1) == b""  # drained to EOF
    finally:
        sup._chld_r = real
        os.close(r)


def test_sv_reap_skips_a_child_that_is_not_a_wrapper(
    queued_supervisor, short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the subreaper set, a reparented grandchild reaps through the same
    `waitpid(-1)`; it is not a wrapper, so nothing is recorded for it. The
    wrapper after it is recorded, its lifeline closed and its exit kept."""
    sup, _ = queued_supervisor
    lifeline_r, lifeline_w = os.pipe()
    run = _fake_run(sup, "rid", short_root / "runs" / "j.1", wrapper_pid=222, lifeline_w=lifeline_w)
    statuses = iter([(111, 0), (222, 256), (0, 0)])
    monkeypatch.setattr(rs.os, "waitpid", lambda _pid, _flags: next(statuses))
    try:
        sup._reap()
        assert run.wrapper_rc == 1 and list(sup._completed) == ["rid"]
        with pytest.raises(OSError):
            os.fstat(lifeline_w)  # closed by the reap
        assert os.read(lifeline_r, 1) == b""  # the wrapper sees EOF
    finally:
        os.close(lifeline_r)


# ------------------------------------------------- the pid-owner guard


def test_sv_pid_guard_treats_a_zombie_as_absent_and_a_deep_record_as_not(
    queued_supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, _ = queued_supervisor
    sup.boot_id = "current-boot"
    record = {"pid": 123, "start_time": "ticks:1", "boot_id": "current-boot"}
    monkeypatch.setattr(rs, "_load_json", lambda _path: record)
    monkeypatch.setattr(rs, "proc_start_token", lambda _pid: "ticks:1")
    monkeypatch.setattr(rs, "proc_is_zombie", lambda _pid: False)
    assert sup._pid_owner_absent() is False  # alive, same start time
    monkeypatch.setattr(rs, "proc_is_zombie", lambda _pid: True)
    assert sup._pid_owner_absent() is True  # exited, unreaped: holds nothing

    def deep(_path: str) -> dict:
        raise RecursionError

    monkeypatch.setattr(rs, "_load_json", deep)
    assert sup._pid_owner_absent() is False  # a record that cannot be read names an owner


# ----------------------------------------------------- record readers


def test_sv_record_readers_refuse_what_is_not_their_record(short_root: Path) -> None:
    directory = short_root / "d"
    directory.mkdir()
    assert rs._load_json(str(directory)) is rs._INVALID  # exists, cannot be read
    assert rs._load_json(str(short_root / "absent")) is None
    assert rs._load_tombstone(str(directory), "index") is rs._INVALID
    assert rs._load_tombstone(str(short_root / "absent"), "index") is None

    path = short_root / "t.json"
    path.write_bytes(b"[1, 2]\n")  # canonical, but not an object
    assert rs._load_tombstone(str(path), "index") is rs._INVALID
    rs._write_canonical(
        str(path), {"artifact_format_version": rs.ARTIFACT_FORMAT_VERSION, "run_id": "r"}
    )
    assert rs._load_tombstone(str(path), "index") is rs._INVALID  # a required key is missing
    rs._write_canonical(
        str(path),
        {
            "artifact_format_version": rs.ARTIFACT_FORMAT_VERSION,
            "run_id": 5,
            "job": "j",
            "run_number": 1,
        },
    )
    assert rs._load_tombstone(str(path), "index") is rs._INVALID  # a key has the wrong type
    rs._write_canonical(
        str(path),
        {
            "artifact_format_version": rs.ARTIFACT_FORMAT_VERSION,
            "run_id": "r",
            "job": "j",
            "run_number": 1,
        },
    )
    assert rs._load_tombstone(str(path), "index") is not rs._INVALID
