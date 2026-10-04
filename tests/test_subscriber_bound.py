"""A subscriber that stops reading has a fixed backlog budget (DL-267).

control-protocol ss5: past the budget the engine removes the subscriber,
ends its handler and aborts its transport. The journal append that
overflowed it succeeds, the engine keeps running, every other subscriber
keeps its feed, and the removed client resumes from its cursor through
the ordinary backfill. Every other hangup the server makes gives a
reading peer a bounded grace and then aborts.

Every window here is constructed, not timed. A stalled client is a real
socket that reads its ack and then nothing more. A mutator task makes
real journaled mutations and pauses once the server's side of that
socket is over its high-water mark, which is the state in which `drain`
blocks. The test acts on that state and then lets the mutator go on. A
test that must prove a hangup was immediate sets the grace to an hour,
so the grace path could not finish inside the test's own deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket as socket_mod
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from test_access import ME, _map_granting, _receipts, _serve_armed
from test_access import TEXT as ACCESS_TEXT
from test_boundary import C2_JIL, _close, _genesis, _request, _seal, _stage
from test_runner_control import _read_revision, _sendevent, _serve, _teardown

from dsl41.oracle_state import Event
from dsl41.runner_adapters import LINE_LIMIT
from dsl41.runner_admission import PROTOCOL_VERSION, Envelope
from dsl41.runner_control import (
    SUBSCRIBER_BACKLOG_BYTES,
    ControlClient,
    ControlClientError,
    ControlServer,
    StreamLineTooLong,
    command,
    subscribe_lines,
)
from dsl41.runner_journal import Journal, read_journal

TEXT = "insert_job: sb_job\njob_type: c\ncommand: x\nmachine: m1\n"
#: one SET_GLOBAL value: real mutations that fill socket buffers quickly,
#: each record still smaller than the smallest kernel socket buffer (8 KiB
#: on macOS), so the stalled peer receives whole records before the stall
BIG = "v" * 2048
#: the socket tests' budget. The fixed default is pinned separately below;
#: a few records of BIG pass this one, so overflow follows the stall soon
SMALL_BUDGET = 64 * 1024
#: a mutator that has not seen its state after this many mutations fails
MAX_MUTATIONS = 2000
#: a grace no test outlives: a hangup that took the grace path would fail
#: the test's own deadline, so finishing proves the hangup was immediate
AN_HOUR = 3600.0


class _BrokenStderr:
    """A stderr whose consumer has gone: every write is a broken pipe."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


# ------------------------------------------------------------ the journal


def _on_disk(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _filler(size: int) -> dict[str, Any]:
    return {"rec": "preflight", "items": [{"message": "x" * size}]}


def test_dl267_the_budget_is_four_request_lines() -> None:
    """ss5 states the number and where it comes from."""
    assert SUBSCRIBER_BACKLOG_BYTES == 4 * LINE_LIMIT == 64 * 1024 * 1024


def test_dl267_a_feed_is_removed_at_the_fixed_bound_and_never_grows_past_it(
    tmp_path: Path,
) -> None:
    """The real budget, on a feed whose consumer is not waiting: every
    record that fits is kept in order, the first that does not removes
    the feed, and the backlog never passed the budget on the way."""
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    reasons: list[str] = []
    feed = journal.subscribe(reasons.append, budget=SUBSCRIBER_BACKLOG_BYTES)
    written = 0
    while feed.overflow is None:
        journal._write(_filler(256 * 1024))
        written += 1
        assert feed.backlog_bytes <= SUBSCRIBER_BACKLOG_BYTES
    assert journal._subscribers == []
    assert len(reasons) == 1 and str(SUBSCRIBER_BACKLOG_BYTES) in reasons[0]
    assert feed.empty() and feed.backlog_bytes == 0  # the backlog was dropped
    # the record that overflowed it is durable: the append succeeded
    assert len(_on_disk(journal.path)) == written
    journal.close()


def test_dl267_records_within_the_budget_are_all_delivered_in_order(tmp_path: Path) -> None:
    """The non-triggering twin: a reader that keeps up is never removed."""
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    reasons: list[str] = []
    feed = journal.subscribe(reasons.append, budget=4096)
    for n in range(200):
        journal._write(_filler(1000) | {"n": n})
        assert feed.get_nowait()["n"] == n
    assert reasons == [] and journal._subscribers == [feed]
    journal.close()


def test_dl267_one_record_larger_than_the_budget_delays_and_does_not_remove(
    tmp_path: Path,
) -> None:
    """A record always enters an empty backlog, so a healthy subscriber is
    never removed for the size of one record."""
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    feed = journal.subscribe(budget=100)
    journal._write(_filler(1000))
    assert feed.overflow is None and feed.backlog_bytes > feed.budget
    assert feed.get_nowait()["rec"] == "preflight"
    journal._write(_filler(1000))
    assert feed.overflow is None
    journal.close()


def test_dl267_a_burst_past_the_budget_removes_even_a_waiting_feed(tmp_path: Path) -> None:
    """The engine applies queued commands without yielding (concurrency-
    model ss4), so a burst can arrive while a reading subscriber's handler
    waits on an empty backlog. Past the budget it removes that feed too,
    and the backlog never held more than the budget or one record."""

    async def scenario() -> None:
        journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
        waiting = journal.subscribe(budget=4096)
        peaks: list[int] = []
        offer = waiting.offer

        def watched(record: dict[str, Any], size: int) -> bool:
            accepted = offer(record, size)
            peaks.append(waiting.backlog_bytes)
            return accepted

        waiting.offer = watched  # type: ignore[method-assign]

        async def consume() -> None:
            await waiting.get()

        consumer = asyncio.ensure_future(consume())
        await asyncio.sleep(0)  # the consumer parks on the empty backlog
        for n in range(100):  # one synchronous burst, ~25 times the budget
            journal._write(_filler(1000) | {"n": n})
        assert waiting.overflow is not None and journal._subscribers == []
        assert max(peaks) <= waiting.budget
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        journal.close()

    asyncio.run(scenario())


def test_dl267_a_broken_stderr_and_a_raising_owner_never_fail_the_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The append that overflowed a feed is durable, and every feed after
    the overflowed one still gets the record: owners are told only once
    the fan-out is done. An owner that raises, and a stderr that cannot
    take the report of it, go no further."""
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)

    def broken(_why: str) -> None:
        raise RuntimeError("owner bug")

    feed = journal.subscribe(broken, budget=10)
    ownerless = journal.subscribe(budget=10)
    healthy = journal.subscribe()  # listed after the overflowed ones
    journal._write(_filler(10))
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    journal._write(_filler(10))  # overflows `feed` and `ownerless`; returns normally
    monkeypatch.undo()
    assert feed.overflow is not None and ownerless.overflow is not None
    assert journal._subscribers == [healthy]
    assert [r["rec"] for r in _on_disk(journal.path)] == ["preflight", "preflight"]
    assert [healthy.get_nowait()["rec"] for _ in range(2)] == ["preflight", "preflight"]
    journal.close()


def test_dl267_an_overflow_at_the_seal_record_leaves_the_boundary_committed(
    tmp_path: Path,
) -> None:
    """ss2.2: the seal append is the point of no return, and anything that
    raised out of its publication would turn a committed boundary into an
    UNKNOWN fail-stop. The stalled feed is filled at the boundary's last
    step before the append, so the `seal` record is the one that overflows
    it; its owner callback raises as well. The boundary still commits, and
    a healthy feed gets the seal."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    journal = engine.journal
    assert journal is not None
    reasons: list[str] = []

    def stalled_owner(why: str) -> None:
        reasons.append(why)
        raise RuntimeError("owner bug")

    stalled = journal.subscribe(stalled_owner, budget=SUBSCRIBER_BACKLOG_BYTES)
    healthy = journal.subscribe()

    def before_the_seal(stage: str) -> None:
        if stage == "after_sidecar":
            room = stalled.budget - stalled.backlog_bytes
            assert room > 0 and stalled.offer(_filler(1), room)  # now exactly full
            assert stalled.backlog_bytes == stalled.budget

    engine.crash_point = before_the_seal
    boundary = asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert boundary is not None
    assert len(reasons) == 1 and stalled.overflow is not None
    assert stalled not in journal._subscribers and healthy in journal._subscribers
    seen = []
    while not healthy.empty():
        seen.append(healthy.get_nowait()["rec"])
    assert seen[-1] == "seal"
    assert read_journal(journal.path)[-1]["rec"] == "seal"  # durable, last
    _close(engine)


# ------------------------------------------------------------ socket harness


async def _raw_subscribe(path: Path, since: int | None = None) -> tuple[socket_mod.socket, int]:
    """A real subscription socket, read up to and including its ack, and
    the cursor the ack names (ss5)."""
    request: dict[str, Any] = {"cmd": "subscribe", "v": PROTOCOL_VERSION}
    if since is not None:
        request["since"] = since
    sock = socket_mod.socket(socket_mod.AF_UNIX)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(sock, str(path))
    await loop.sock_sendall(sock, json.dumps(request).encode() + b"\n")
    ack = b""
    while not ack.endswith(b"\n"):
        chunk = await asyncio.wait_for(loop.sock_recv(sock, 1), timeout=5.0)
        assert chunk, "engine hung up before the ack"
        ack += chunk
    parsed = json.loads(ack)
    assert parsed == {"ok": True, "subscribed": True, "since": parsed["since"]}
    if since is not None:
        assert parsed["since"] == since
    return sock, parsed["since"]


async def _read_to_eof(sock: socket_mod.socket) -> bytes:
    loop = asyncio.get_running_loop()
    chunks = []
    async with asyncio.timeout(20.0):
        with contextlib.suppress(ConnectionResetError):
            while chunk := await loop.sock_recv(sock, 1 << 16):
                chunks.append(chunk)
    return b"".join(chunks)


def _records(data: bytes) -> list[dict[str, Any]]:
    """Every complete line. A torn final line is skippable (ss6)."""
    lines = data.split(b"\n")
    return [json.loads(line) for line in lines[:-1]]


def _seqs(records: list[dict[str, Any]]) -> list[int]:
    return [r["seq"] for r in records if isinstance(r.get("seq"), int)]


def _wal_seqs(server: ControlServer, after: int) -> list[int]:
    journal = server.engine.journal
    assert journal is not None
    return [s for s in _seqs(read_journal(journal.path)) if s > after]


class _Follower:
    """A healthy subscriber: reads every line as it arrives and parses
    each complete line once."""

    def __init__(self, sock: socket_mod.socket) -> None:
        self.sock = sock
        self.records: list[dict[str, Any]] = []
        self._partial = b""
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        chunks: list[bytes] = []
        while chunk := await loop.sock_recv(self.sock, 1 << 20):
            if b"\n" not in chunk:
                chunks.append(chunk)  # inside a long line: join once it ends
                continue
            *lines, rest = (self._partial + b"".join(chunks) + chunk).split(b"\n")
            chunks.clear()
            self._partial = rest
            self.records.extend(json.loads(line) for line in lines)

    def seqs(self) -> list[int]:
        return _seqs(self.records)

    async def close(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, OSError):
            await self.task
        self.sock.close()


def _capture_writers(
    server: ControlServer,
    answers: Callable[[dict[str, Any]], bool],
    high_water: int | None = None,
) -> list[asyncio.StreamWriter]:
    """The server-side writer of every line `answers` picks, in order. The
    stall is a property of that transport, and the server keeps it only
    for a stream on an armed perimeter, so the tests record it here.
    `high_water` raises that transport's mark before the line is sent, so
    `drain` returns although the peer reads nothing."""
    writers: list[asyncio.StreamWriter] = []
    send = server._send

    async def recording(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        if answers(obj):
            writers.append(writer)
            if high_water is not None:
                writer.transport.set_write_buffer_limits(high=high_water)
        await send(writer, obj)

    server._send = recording  # type: ignore[method-assign]
    return writers


def _capture_stream_writers(server: ControlServer) -> list[asyncio.StreamWriter]:
    return _capture_writers(server, lambda obj: obj.get("subscribed") is True)


async def _subscribe_and_track(
    server: ControlServer, since: int | None = None
) -> tuple[socket_mod.socket, int, asyncio.Task[Any]]:
    """A stalled subscription, its ack cursor, and the server's handler
    task for it. Call it with no other connection in flight, so the new
    task is the one."""
    before = set(server._conn_tasks)
    sock, cursor = await _raw_subscribe(server.path, since=since)
    [task] = set(server._conn_tasks) - before
    return sock, cursor, task


def _aborted(writer: asyncio.StreamWriter) -> bool:
    """Closed with its unsent bytes discarded. A graceful close keeps them
    until the peer reads, which a stalled peer never does."""
    return writer.transport.is_closing() and writer.transport.get_write_buffer_size() == 0


def _backpressured(writer: asyncio.StreamWriter) -> bool:
    """Over the high-water mark: the state in which `drain` blocks."""
    transport = writer.transport
    return transport.get_write_buffer_size() > transport.get_write_buffer_limits()[1]


async def _until(condition: Callable[[], bool], what: str, timeout_s: float = 30.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.01)


class _Mutator:
    """Real journaled mutations on their own connections, in a task. It
    pauses when `pause_when` first holds and resumes on `go`; it stops on
    `stop`. A mutator that fails ends the test through `task`."""

    def __init__(
        self,
        path: Path,
        pause_when: Callable[[], bool] | None = None,
        value: Callable[[int], str] = lambda _n: BIG,
    ) -> None:
        self.path = path
        self.pause_when = pause_when
        self.value = value
        self.paused = asyncio.Event()
        self.go = asyncio.Event()
        self.stopping = False
        self.count = 0
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while not self.stopping:
            if self.count >= MAX_MUTATIONS:
                raise AssertionError(f"no expected state after {MAX_MUTATIONS} mutations")
            value = self.value(self.count)
            answer = await _sendevent(self.path, "SET_GLOBAL", name="SB_G", value=value)
            assert answer.get("ok") is True, answer
            self.count += 1
            if self.pause_when is not None and self.pause_when():
                self.pause_when = None
                self.paused.set()
                await self.go.wait()

    async def stop(self) -> None:
        self.stopping = True
        self.go.set()
        await self.task


async def _wait_or_fail(condition: Callable[[], bool], mutator: _Mutator, what: str) -> None:
    await _until(lambda: condition() or mutator.task.done(), what)
    if mutator.task.done():
        mutator.task.result()  # raise the mutator's own failure
        raise AssertionError(f"the mutator stopped before {what}")


# ----------------------------------------------------------- live overflow


def test_dl267_a_stalled_subscriber_is_removed_and_the_estate_keeps_running(
    short_root: Path,
) -> None:
    """DL-267 end to end, on real sockets: the stalled
    stream backpressures, is removed at the budget, and its handler ends
    at once while its peer still reads nothing; a healthy stream gets
    every record; the engine keeps admitting; and a reconnect at the
    stalled client's last seq gets the rest exactly once across the
    backfill seam."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        journal = engine.journal
        assert journal is not None
        server.subscriber_budget = SMALL_BUDGET
        server.HANGUP_GRACE_S = AN_HOUR  # an overflow must not take the grace path
        writers = _capture_stream_writers(server)
        healthy_sock, healthy_from = await _raw_subscribe(server.path)
        healthy = _Follower(healthy_sock)
        stalled, stalled_from, handler = await _subscribe_and_track(server)
        assert stalled_from == engine.frontiers.committed_index
        healthy_feed, stalled_feed = journal._subscribers
        stalled_writer = writers[1]
        try:
            mutator = _Mutator(server.path, lambda: _backpressured(stalled_writer))
            await _wait_or_fail(mutator.paused.is_set, mutator, "backpressure")
            # the window: the stalled handler is blocked in drain, and its
            # feed is still subscribed and within budget
            assert stalled_feed in journal._subscribers
            assert stalled_feed.overflow is None
            mutator.go.set()
            await _wait_or_fail(lambda: stalled_feed.overflow is not None, mutator, "overflow")
            await mutator.stop()
            assert str(SMALL_BUDGET) in (stalled_feed.overflow or "")
            assert journal._subscribers == [healthy_feed]
            # its handler ended and its transport is gone, though its peer
            # has read nothing since the ack
            await _until(handler.done, "the stalled handler to end")
            assert _aborted(stalled_writer)
            # the engine still admits, and the healthy stream sees it
            after = await _sendevent(server.path, "SET_GLOBAL", name="SB_G", value="after")
            assert after["ok"] is True
            assert engine.oracle.store.global_value("SB_G") == "after"
            assert not loop_task.done()
            final = _wal_seqs(server, healthy_from)[-1]
            await _until(lambda: final in healthy.seqs(), "the healthy stream to catch up")
            assert healthy.seqs() == _wal_seqs(server, healthy_from)
            # the stalled peer reads what reached it, then EOF: a prefix of
            # the live stream with no hole in it
            got = _seqs(_records(await _read_to_eof(stalled)))
            expected = _wal_seqs(server, stalled_from)
            assert got and got == expected[: len(got)] and len(got) < len(expected)
            # reconnect at its last cursor: the rest, exactly once, across
            # the backfill seam into the live stream
            resumed_sock, _ = await _raw_subscribe(server.path, since=got[-1])
            resumed = _Follower(resumed_sock)
            await _until(lambda: final in resumed.seqs(), "the backfill")
            live = await _sendevent(server.path, "SET_GLOBAL", name="SB_G", value="live")
            assert live["ok"] is True
            last = _wal_seqs(server, healthy_from)[-1]
            await _until(lambda: last in resumed.seqs(), "the live record after the seam")
            assert got + resumed.seqs() == _wal_seqs(server, stalled_from)
            await resumed.close()
        finally:
            stalled.close()
            await healthy.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_an_overflow_during_backfill_ends_the_stream_inside_it(
    short_root: Path,
) -> None:
    """A resuming client that stalls inside its own backfill: live records
    queue behind it, the feed overflows, and the stream ends before the
    backfill is through. A reconnect at the last seq it got completes the
    history exactly once."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        journal = engine.journal
        assert journal is not None
        server.subscriber_budget = SMALL_BUDGET
        server.HANGUP_GRACE_S = AN_HOUR
        writers = _capture_stream_writers(server)
        try:
            # history first: a few whole records the peer can receive, then
            # far more backfill than any platform's socket buffers hold
            mutator = _Mutator(
                server.path,
                lambda: journal.path.stat().st_size >= 2 * 1024 * 1024,
                value=lambda n: BIG if n < 10 else "w" * 32768,
            )
            await _wait_or_fail(mutator.paused.is_set, mutator, "the history")
            seam = engine.frontiers.committed_index
            stalled, _, handler = await _subscribe_and_track(server, since=0)
            [writer] = writers
            [feed] = journal._subscribers
            await _until(lambda: _backpressured(writer), "backpressure inside the backfill")
            assert feed.backlog_bytes == 0  # nothing live yet: blocked in the backfill
            mutator.go.set()
            await _wait_or_fail(lambda: feed.overflow is not None, mutator, "overflow")
            await mutator.stop()
            await _until(handler.done, "the stalled handler to end")
            assert _aborted(writer)
            got = _seqs(_records(await _read_to_eof(stalled)))
            stalled.close()
            assert got and got[-1] < seam  # cut inside the backfill
            assert got == _wal_seqs(server, 0)[: len(got)]
            final = _wal_seqs(server, 0)[-1]
            resumed_sock, _ = await _raw_subscribe(server.path, since=got[-1])
            resumed = _Follower(resumed_sock)
            await _until(lambda: final in resumed.seqs(), "the resumed backfill")
            assert got + resumed.seqs() == _wal_seqs(server, 0)
            await resumed.close()
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_a_broken_stderr_does_not_keep_an_overflowed_stream_open(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's overflow owner cancels before it reports, and the
    report is best effort. With a stderr that raises BrokenPipeError the
    overflowing append still succeeds, the stalled handler still ends, and
    a feed listed after it still gets the record."""

    async def scenario() -> None:
        journal = Journal(short_root / "wal.jsonl", fsync_each=False)
        server = ControlServer(_FakeEngine(journal), short_root / "e.sock")  # type: ignore[arg-type]
        server.subscriber_budget = SMALL_BUDGET
        server.HANGUP_GRACE_S = AN_HOUR
        writers = _capture_stream_writers(server)
        await server.start()
        try:
            stalled, _, handler = await _subscribe_and_track(server)
            [writer] = writers
            [stalled_feed] = journal._subscribers
            healthy = journal.subscribe()  # after the stalled one
            written = 0
            while not _backpressured(writer):
                journal._write(_filler(2000))
                written += 1
                await asyncio.sleep(0)  # let the handler send it
            monkeypatch.setattr(sys, "stderr", _BrokenStderr())
            while stalled_feed.overflow is None:
                journal._write(_filler(2000))  # raises here if the report escapes
                written += 1
            monkeypatch.undo()
            await _until(handler.done, "the stalled handler to end")
            assert _aborted(writer)
            assert journal._subscribers == [healthy]
            assert len([healthy.get_nowait() for _ in range(written)]) == written
            assert healthy.empty()
            stalled.close()
        finally:
            await server.close()
            journal.close()

    asyncio.run(scenario())


class _FakeEngine:
    """The journal-only engine `_subscribe` needs: no lineage to lose."""

    def __init__(self, journal: Journal) -> None:
        self.journal = journal
        self.estate = None

        class _Frontiers:
            committed_index = 0

        self.frontiers = _Frontiers()


# ---------------------------------------------- one command at the line limit


def _limit_request(read: tuple[str, int, int], char: str) -> bytes:
    """A SET_GLOBAL request line of exactly LINE_LIMIT bytes before its
    newline, its value made of `char`, sent as raw UTF-8."""
    baseline, epoch, revision = read

    def line(value: str) -> bytes:
        request = command(
            "SET_GLOBAL",
            {"name": "SB_LIMIT", "value": value},
            key="global:SB_LIMIT",
            revision=revision,
            baseline_id=baseline,
            epoch=epoch,
            request_id="r-limit",
        )
        request["v"] = PROTOCOL_VERSION
        return json.dumps(request, ensure_ascii=False).encode("utf-8")

    room = LINE_LIMIT - len(line(""))
    width = len(char.encode("utf-8"))
    value = char * (room // width) + "a" * (room % width)
    framed = line(value)
    assert len(framed) == LINE_LIMIT
    return framed + b"\n"


@pytest.mark.parametrize("char", ["a", "é"], ids=["ascii", "escaped-3x"])
def test_dl267_one_command_at_the_line_limit_keeps_a_reading_subscriber(
    short_root: Path, char: str
) -> None:
    """ss5's burst: one admitted command appends its input record and its
    decision. The request here is a full LINE_LIMIT line; written with a
    two-byte character, its input record is three times that, because the
    stream escapes every non-ASCII character. A reading subscriber whose
    backlog was empty gets both records and stays subscribed."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        journal = engine.journal
        assert journal is not None
        sock, start = await _raw_subscribe(server.path)
        follower = _Follower(sock)
        [feed] = journal._subscribers
        try:
            read = await _read_revision(server.path, "global:SB_LIMIT")
            framed = _limit_request(read, char)
            reader, writer = await asyncio.open_unix_connection(str(server.path), limit=LINE_LIMIT)
            writer.write(framed)
            await writer.drain()
            answer = json.loads(await asyncio.wait_for(reader.readline(), 60.0))
            writer.close()
            assert answer["ok"] is True, answer
            await _until(
                lambda: any(r.get("rec") == "decision" for r in follower.records),
                "the decision",
                timeout_s=60.0,
            )
            assert feed.overflow is None and journal._subscribers == [feed]
            [record] = [r for r in follower.records if r.get("rec") == "input"]
            assert record["seq"] == start + 1
            sent = json.loads(framed)["payload"]["value"]
            assert sent in json.dumps(record, ensure_ascii=False)
        finally:
            await follower.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_queued_commands_keep_a_reading_subscriber_within_the_bound(
    short_root: Path,
) -> None:
    """DL-267's burst, on a real socket: many commands queued with
    `Engine.submit` before the loop runs, which it then applies without
    yielding. At every offer the reading subscriber's backlog stays within
    max(budget, one record); past the budget it is removed, and a
    reconnect at the ack's `since` gets every record exactly once."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        journal = engine.journal
        assert journal is not None
        server.subscriber_budget = 1024 * 1024
        sock, cursor = await _raw_subscribe(server.path)
        follower = _Follower(sock)
        [feed] = journal._subscribers
        bounds: list[tuple[int, int]] = []
        offer = feed.offer

        def watched(record: dict[str, Any], size: int) -> bool:
            accepted = offer(record, size)
            bounds.append((feed.backlog_bytes, max(feed.budget, size)))
            return accepted

        feed.offer = watched  # type: ignore[method-assign]
        try:
            value = "q" * (256 * 1024)
            decided = [
                engine.submit(
                    Event(
                        at=engine.clock.now(),
                        kind="SET_GLOBAL",
                        payload={"name": f"SB_Q{n}", "value": value},
                    ),
                    Envelope(
                        request_id=f"r-q{n}", expect={f"global:SB_Q{n}": 0}, epoch=engine.epoch
                    ),
                )
                for n in range(24)  # about six times the budget, queued at once
            ]
            results = await asyncio.wait_for(asyncio.gather(*decided), 60.0)
            assert all(r.decision == "applied" for r in results)
            assert all(backlog <= bound for backlog, bound in bounds)
            assert feed.overflow is not None and journal._subscribers == []
            final = _wal_seqs(server, cursor)[-1]
            resumed_sock, _ = await _raw_subscribe(server.path, since=cursor)
            resumed = _Follower(resumed_sock)
            await _until(lambda: final in resumed.seqs(), "the resumed backfill")
            assert resumed.seqs() == _wal_seqs(server, cursor)
            await resumed.close()
        finally:
            await follower.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------- shutdown and revocation


def test_dl267_clean_shutdown_is_not_held_by_a_stalled_subscriber(short_root: Path) -> None:
    """A close keeps unsent bytes until the peer takes them, so a handler
    that closed a stalled transport and waited for it waited forever, and
    the server's shutdown with it. The stream here is backpressured and
    inside its budget; shutdown finishes within the grace."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        writers = _capture_stream_writers(server)
        stalled, _, handler = await _subscribe_and_track(server)
        [writer] = writers
        try:
            mutator = _Mutator(server.path, lambda: _backpressured(writer))
            await _wait_or_fail(mutator.paused.is_set, mutator, "backpressure")
            await mutator.stop()
            assert engine.journal is not None
            [feed] = engine.journal._subscribers
            assert feed.overflow is None  # the default budget: no overflow here
        finally:
            async with asyncio.timeout(server.HANGUP_GRACE_S + 10.0):
                await _teardown(engine, server, loop_task)
        assert handler.done() and _aborted(writer)
        await _read_to_eof(stalled)  # EOF, not a hang
        stalled.close()

    asyncio.run(scenario())


async def _set_large_global(server: ControlServer, size: int) -> str:
    value = "g" * size
    answer = await _sendevent(server.path, "SET_GLOBAL", name="SB_BIG", value=value)
    assert answer["ok"] is True
    return value


async def _ask_for_large_global(
    server: ControlServer, high_water: int | None = None
) -> tuple[socket_mod.socket, asyncio.Task[Any], asyncio.StreamWriter]:
    """A `global` query whose answer will not fit the socket buffers, sent
    on a raw socket that then reads nothing. Returns once the server holds
    unsent answer bytes: the window every test below acts in."""
    writers = _capture_writers(
        server, lambda obj: "SB_BIG" in obj.get("globals", {}), high_water=high_water
    )
    before = set(server._conn_tasks)
    sock = socket_mod.socket(socket_mod.AF_UNIX)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(sock, str(server.path))
    request = {"cmd": "global", "name": "SB_BIG", "v": PROTOCOL_VERSION}
    await loop.sock_sendall(sock, json.dumps(request).encode() + b"\n")
    await _until(lambda: bool(writers), "the answer")
    [writer] = writers
    await _until(lambda: writer.transport.get_write_buffer_size() > 0, "unsent answer bytes")
    [task] = set(server._conn_tasks) - before
    return sock, task, writer


def test_dl267_shutdown_is_not_held_by_a_stalled_request_peer(short_root: Path) -> None:
    """Every connection kind, not only streams: a request peer that never
    reads its large answer is aborted when the grace runs out."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        try:
            await _set_large_global(server, 2 * 1024 * 1024)
            sock, handler, writer = await _ask_for_large_global(server)
        finally:
            async with asyncio.timeout(server.HANGUP_GRACE_S + 10.0):
                await _teardown(engine, server, loop_task)
        assert handler.done() and _aborted(writer)
        sock.close()

    asyncio.run(scenario())


def test_dl267_a_request_peer_that_hangs_up_unread_is_aborted_after_the_grace(
    short_root: Path,
) -> None:
    """A peer that sends its request, shuts down its write side and never
    reads: the handler ends at EOF by itself, outside any shutdown. Its
    answer sits unsent below the high-water mark, so `drain` returned.
    Its close waits the grace, then aborts, so the connection does not
    linger for a later shutdown to hang on."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        try:
            await _set_large_global(server, 2 * 1024 * 1024)
            sock, handler, writer = await _ask_for_large_global(server, high_water=1 << 30)
            assert not _backpressured(writer)
            sock.shutdown(socket_mod.SHUT_WR)
            await _until(handler.done, "the grace", timeout_s=server.HANGUP_GRACE_S + 10.0)
            assert _aborted(writer)
            sock.close()
        finally:
            async with asyncio.timeout(10.0):
                await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_a_reading_client_gets_its_whole_answer_at_shutdown(short_root: Path) -> None:
    """The grace's other side: a client still reading its answer when the
    engine shuts down receives every byte of it, then EOF."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        closing: asyncio.Task[None] | None = None
        try:
            value = await _set_large_global(server, 2 * 1024 * 1024)
            sock, handler, _writer = await _ask_for_large_global(server)
            closing = asyncio.ensure_future(_teardown(engine, server, loop_task))
            await _until(lambda: server._closing, "the shutdown to begin")
            data = await _read_to_eof(sock)
            sock.close()
            [answer] = _records(data)
            assert answer["globals"]["SB_BIG"]["value"] == value
            async with asyncio.timeout(server.HANGUP_GRACE_S + 10.0):
                await closing
            assert handler.done()
        finally:
            if closing is None:
                await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_revoking_a_stalled_stream_ends_it_at_once(short_root: Path) -> None:
    """access-model ss7 closes a stream that lost read and cancels its
    handler. For a stalled stream the close alone kept the transport and
    the handler alive; a revocation now aborts it without the grace."""

    async def scenario() -> None:
        run_root = short_root / "run"
        map_path = _map_granting(short_root / "roles.toml", "ops")
        engine, server, loop_task, access = await _serve_armed(run_root, ACCESS_TEXT, map_path)
        server.HANGUP_GRACE_S = AN_HOUR  # a revocation must not take the grace path
        stalled, _ = await _raw_subscribe(server.path)
        [(writer, (_principal, handler))] = list(access.streams.items())
        try:
            mutator = _Mutator(server.path, lambda: _backpressured(writer))
            await _wait_or_fail(mutator.paused.is_set, mutator, "backpressure")
            await mutator.stop()
            _map_granting(short_root / "roles.toml", None)  # deny-all
            access.reload()
            await _until(handler.done, "the revoked handler to end")
            assert _aborted(writer)
            assert engine.journal is not None and engine.journal._subscribers == []
            await _read_to_eof(stalled)
            stalled.close()
        finally:
            await _teardown(engine, server, loop_task)
        revoked = [r for r in _receipts(run_root) if r["rec"] == "stream_revoked"]
        assert [r["principal"] for r in revoked] == [ME]

    asyncio.run(scenario())


# ------------------------------------------------------------- the clients


class _HangupServer:
    """A control socket that answers canned bytes and then HANGS UP."""

    def __init__(self, path: Path, answer: bytes) -> None:
        self.path = path
        self._sock = socket_mod.socket(socket_mod.AF_UNIX)
        self._sock.bind(str(path))
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, args=(answer,), daemon=True)
        self._thread.start()

    def _serve(self, answer: bytes) -> None:
        with contextlib.suppress(OSError):
            conn, _ = self._sock.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(answer)

    def close(self) -> None:
        self._sock.close()
        self.path.unlink(missing_ok=True)


ACK = b'{"ok": true, "since": 3, "subscribed": true}\n'
RECORD = b'{"rec": "input", "seq": 7}\n'
UNSEQUENCED = b'{"index": 7, "rec": "decision"}\n'
ANSWER = b'{"error": "backfill refused", "ok": false}\n'


def test_dl267_subscribe_lines_raises_the_engines_words_for_an_answer_line(
    short_root: Path,
) -> None:
    """An `{"ok": false}` after the ack ends the stream as a refusal, and
    the records before it are still delivered."""
    path = short_root / "s2-words.sock"
    server = _HangupServer(path, ACK + RECORD + ANSWER)
    try:
        lines = subscribe_lines(path, {"cmd": "subscribe"})
        assert json.loads(next(lines))["subscribed"] is True
        assert json.loads(next(lines)) == {"rec": "input", "seq": 7}
        with pytest.raises(ControlClientError, match="backfill refused"):
            next(lines)
    finally:
        server.close()


def test_dl267_subscribe_lines_names_a_hangup_mid_line(short_root: Path) -> None:
    """An abort can cut the last line. Short of LINE_LIMIT that is the
    engine closing the stream, not a line over the limit."""
    path = short_root / "s2-torn.sock"
    server = _HangupServer(path, ACK + RECORD + b'{"rec": "inp')
    try:
        lines = subscribe_lines(path, {"cmd": "subscribe"})
        next(lines), next(lines)
        with pytest.raises(ControlClientError, match="closed the stream mid-line"):
            next(lines)
    finally:
        server.close()


def test_dl267_subscribe_lines_ends_quietly_at_a_plain_hangup(short_root: Path) -> None:
    """The non-triggering twin: records then EOF is an end, not an error."""
    path = short_root / "s2-eof.sock"
    server = _HangupServer(path, ACK + RECORD)
    try:
        assert len(list(subscribe_lines(path, {"cmd": "subscribe"}))) == 2
    finally:
        server.close()


def _subscribe_cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dsl41", "query", "subscribe", "--socket", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("body", "words", "since"),
    [
        (RECORD + UNSEQUENCED, "closed the subscribe stream", 7),
        (RECORD + ANSWER, "backfill refused", 7),
        (UNSEQUENCED, "closed the subscribe stream", 3),  # no seq read: the ack's cursor
        (RECORD + b'{"rec": "inp', "mid-line", 7),
        (RECORD + b"[1]\n" + b"{not json\n", "closed the subscribe stream", 7),
    ],
    ids=["hangup", "answer-line", "ack-cursor", "torn", "unparsable"],
)
def test_dl267_cli_subscribe_exits_2_naming_the_since_to_resume_from(
    short_root: Path, body: bytes, words: str, since: int
) -> None:
    """A monitoring wrapper restarts on a nonzero exit. The records already
    read are on stdout; stderr says why and names the exact `--since`: the
    last seq printed, or the ack's cursor when none was. A line that names
    no cursor leaves it where it was."""
    path = short_root / "s2-cli.sock"
    server = _HangupServer(path, ACK + body)
    try:
        done = _subscribe_cli(path)
    finally:
        server.close()
    assert done.returncode == 2
    assert done.stdout.splitlines()[0] == ACK.decode().strip()
    assert words in done.stderr
    named = re.search(r"--since (-?\d+)", done.stderr)
    assert named is not None and named.group(1) == str(since)


def test_dl267_cli_falls_back_to_the_readers_cursor_without_an_ack_cursor(
    short_root: Path,
) -> None:
    """An engine whose ack names no cursor: the CLI cannot name one either,
    and says to use the reader's own."""
    path = short_root / "s2-old.sock"
    server = _HangupServer(path, b'{"ok": true, "subscribed": true}\n')
    try:
        done = _subscribe_cli(path)
    finally:
        server.close()
    assert done.returncode == 2
    assert "--since set to the last seq you read" in done.stderr


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def test_dl267_a_stalled_monitoring_pipe_recovers_from_its_cursor(short_root: Path) -> None:
    """The stall as the runbook sets it up: `query subscribe` feeding
    a consumer that stops reading. The CLI blocks on its stdout, the
    engine removes the stream, and once the consumer reads again the CLI
    reaches EOF and exits 2, naming a `--since` from which a new
    subscription completes the history."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", TEXT)
        journal = engine.journal
        assert journal is not None
        server.subscriber_budget = SMALL_BUDGET
        start = engine.frontiers.committed_index
        # the consumer's end of the pipe: read the ack, then stop reading
        consumer, cli_stdout = os.pipe()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "dsl41",
            "query",
            "subscribe",
            "--socket",
            str(server.path),
            stdout=cli_stdout,
            stderr=asyncio.subprocess.PIPE,
        )
        os.close(cli_stdout)
        assert proc.stderr is not None
        try:
            ack = b""
            while not ack.endswith(b"\n"):
                ack += await asyncio.wait_for(asyncio.to_thread(os.read, consumer, 1), 30.0)
            assert json.loads(ack) == {"ok": True, "subscribed": True, "since": start}
            await _until(lambda: len(journal._subscribers) == 1, "the CLI's feed")
            [feed] = journal._subscribers
            mutator = _Mutator(server.path)
            await _wait_or_fail(lambda: feed.overflow is not None, mutator, "overflow")
            await mutator.stop()
            out = await asyncio.wait_for(asyncio.to_thread(_read_all, consumer), 30.0)
            err = await asyncio.wait_for(proc.stderr.read(), 30.0)
            assert await asyncio.wait_for(proc.wait(), 30.0) == 2, err
            got = _seqs(_records(out))
            assert got == _wal_seqs(server, start)[: len(got)]
            named = re.search(rb"--since (-?\d+)", err)
            assert named is not None and int(named.group(1)) == got[-1]
            resumed_sock, _ = await _raw_subscribe(server.path, since=int(named.group(1)))
            resumed = _Follower(resumed_sock)
            final = _wal_seqs(server, start)[-1]
            await _until(lambda: final in resumed.seqs(), "the resumed backfill")
            assert got + resumed.seqs() == _wal_seqs(server, start)
            await resumed.close()
        finally:
            os.close(consumer)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_dl267_a_record_past_the_stream_limit_is_not_a_resume_point(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both bundled readers take stream lines up to the budget. A line past
    it raises the distinct `StreamLineTooLong`, and the CLI names no
    `--since`: resubscribing at the same cursor would meet the same record
    and loop."""
    import dsl41.runner_control as control_mod
    from typer.testing import CliRunner

    from dsl41.cli import app

    monkeypatch.setattr(control_mod, "SUBSCRIBER_BACKLOG_BYTES", 64)
    too_long = ACK + RECORD + b'{"rec": "input", "seq": 8, "x": "' + b"z" * 100 + b'"}\n'

    path = short_root / "s2-long-a.sock"
    server = _HangupServer(path, too_long)
    try:
        lines = subscribe_lines(path, {"cmd": "subscribe"})
        next(lines), next(lines)
        with pytest.raises(StreamLineTooLong, match="64-byte limit"):
            next(lines)
    finally:
        server.close()

    async def follow(path: Path) -> list[dict[str, Any]]:
        client = ControlClient(path)
        seen: list[dict[str, Any]] = []
        try:
            async for record in client.subscribe():
                seen.append(record)
        finally:
            await client.close()
        return seen

    path = short_root / "s2-long-b.sock"
    server = _HangupServer(path, too_long)
    try:
        with pytest.raises(StreamLineTooLong):
            asyncio.run(follow(path))
    finally:
        server.close()

    path = short_root / "s2-long-c.sock"
    server = _HangupServer(path, too_long)
    try:
        done = CliRunner().invoke(app, ["query", "subscribe", "--socket", str(path)])
    finally:
        server.close()
    assert done.exit_code == 2
    assert "meets it again" in done.output and "--since" not in done.output
