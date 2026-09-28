"""Recovering a mutation whose answer was lost (DL-216, DL-217).

Two halves, one per entry. DL-216 is the transport: once a client has
attempted a write it cannot prove the request did not arrive, so every
failure from the write on is `delivered`; and the server never parses a
line that lost its terminator at EOF, so a client that died mid-write
leaves nothing behind. DL-217 is the retry: the CLI prints the id and the
three pinned envelope values before it sends, the exit-4 advice repeats
them, and re-running the original arguments with those flags is an exact
retry the engine answers from its original decision -- in the same period,
after the entity moved, and after a restart. A collision refusal carries
the id's earlier decision beside it, never as the retry's own outcome.

Synchronization follows the repo's idiom: bounded polls on a condition,
never a fixed sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import dsl41.runner_control as control
from dsl41.ir import lower_source
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ApplyResult,
    Attempt,
    DecisionIndex,
    EngineError,
    RequestCollision,
)
from dsl41.runner_clock import RealClock
from dsl41.runner_control import ControlClient, ControlClientError, ControlServer
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import resume_run
from dsl41.runner_tui import _outcome_line, _transport_line
from test_access import ME, _map_granting, _serve_armed
from test_access import TEXT as _ACCESS_TEXT
from test_preconditions import _SOLO_JIL, _call, _sendevent_cli, _serve, _teardown

#: wording a refusal of a RETRY must never carry (R7): each one says or
#: implies that the ORIGINAL request did not happen, which a retry's own
#: refusal cannot know
_NEVER_APPLIED_CLAIMS = ("never applied", "never happened", "not sent", "nothing logged")


@pytest.fixture
def short_root():
    """AF_UNIX paths are length-limited (104 bytes on macOS), so these tests
    use a short base directory rather than pytest's deep tmp_path."""
    directory = tempfile.mkdtemp(prefix="dsl41r-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dsl41", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _advice(stderr: str) -> list[str]:
    """The flags the exit-4 advice prints, as argv -- the literal line an
    operator would copy."""
    for line in stderr.splitlines():
        head, sep, tail = line.partition("retry ONLY as ")
        if sep and head.startswith("no decision:"):
            return shlex.split(tail)
    raise AssertionError(f"no retry advice on stderr:\n{stderr}")


def _sending(stderr: str) -> list[str]:
    """The pre-send record, as argv: the retry flags the CLI printed before
    it wrote anything."""
    lines = [line for line in stderr.splitlines() if line.startswith("sending: ")]
    assert len(lines) == 1, stderr
    return shlex.split(lines[0].removeprefix("sending: "))


def _records_for(run_root: Path, request_id: str) -> list[str]:
    return [
        r["rec"]
        for r in read_journal(run_root / "journal.jsonl")
        if r.get("request_id") == request_id
    ]


async def _poll(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")
        await asyncio.sleep(0.01)


def _dropping_replies(server: ControlServer, key: str, value: str):
    """Patch the server so the answer whose `key` is `value` is never
    written: the decision exists, the connection closes, the client sees
    EOF. That is the lost reply."""
    original_send = server._send

    async def drop(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
        if response.get(key) == value:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        await original_send(writer, response)

    return patch.object(server, "_send", drop)


async def _lose_the_reply(
    server: ControlServer, args: tuple[str, ...], kind: str
) -> subprocess.CompletedProcess[str]:
    """Send `args` through the CLI with every answer of `kind` dropped."""
    with _dropping_replies(server, "kind", kind):
        lost = await asyncio.to_thread(_cli, *args)
    assert lost.returncode == 4, (lost.stdout, lost.stderr)
    return lost


# ------------------------------------------------ a. the pinned exact retry

_STATUS_ARGS = ("sendevent", "CHANGE_STATUS", "--job", "j", "--status", "SUCCESS")


def test_the_printed_advice_retries_a_lost_applied_reply_in_the_same_period(
    short_root: Path,
) -> None:
    """The answer to an APPLIED command is lost. The advice line, pasted
    after the original arguments, is answered from the original decision:
    one index, one input, one decision."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            args = (*_STATUS_ARGS, "--socket", str(server.path))
            lost = await _lose_the_reply(server, args, "STATUS")
            flags = _advice(lost.stderr)
            request_id = flags[1]
            # the pre-send record carried the same id and the same three pins
            assert _sending(lost.stderr) == flags
            assert engine.oracle.store.job["j"].status == "SUCCESS"  # it DID apply
            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 0, retry.stderr
            answer = json.loads(retry.stdout)
            assert [rid for rid, _ in engine.deduped] == [request_id]
            assert answer["index"] == engine.deduped[0][1].index
            assert _records_for(run_root, request_id) == ["input", "decision"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_pinned_retry_survives_another_actor_moving_the_job(short_root: Path) -> None:
    """Without the pins the CLI re-reads the revision, which another actor
    has moved, and the retry is a different envelope under a reused id. With
    them it is the same envelope, answered from the original decision and
    taking no second index."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            args = (*_STATUS_ARGS, "--socket", str(server.path))
            lost = await _lose_the_reply(server, args, "STATUS")
            flags = _advice(lost.stderr)
            request_id = flags[1]
            moved = await asyncio.to_thread(_sendevent_cli, server.path, "ON_HOLD", "--job", "j")
            assert moved.returncode == 0, moved.stderr
            applied_before = engine.frontiers.applied_index

            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 0, retry.stderr
            assert json.loads(retry.stdout)["index"] < applied_before
            assert engine.frontiers.applied_index == applied_before  # no second index
            assert [rid for rid, _ in engine.deduped] == [request_id]

            # the id alone, re-read against the moved job, is not the retry
            unpinned = await asyncio.to_thread(_cli, *args, "--request-id", request_id)
            assert unpinned.returncode == 2
            assert "decided earlier: applied" in unpinned.stderr
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


async def _resume_served(run_root: Path) -> tuple[Engine, ControlServer, asyncio.Future[Any]]:
    engine = await resume_run(
        lower_source(_SOLO_JIL),
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        hold_open=True,
        settle_seconds=0.0,
        grace_seconds=0.0,
    )
    server = ControlServer(engine, run_root / "control.sock")
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task


def test_the_pinned_retry_is_answered_after_a_same_period_restart(short_root: Path) -> None:
    """The restart takes a new epoch and rebuilds the decision index from
    the WAL. The pinned retry names the OLD epoch -- the one its original
    carried -- and dedup precedes the epoch check, so it is answered from
    the original decision."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            args = (*_STATUS_ARGS, "--socket", str(server.path))
            lost = await _lose_the_reply(server, args, "STATUS")
            flags = _advice(lost.stderr)
            old_epoch = engine.epoch
        finally:
            await _teardown(engine, server, loop_task)

        resumed, server, loop_task = await _resume_served(run_root)
        try:
            assert resumed.epoch > old_epoch
            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 0, retry.stderr
            assert [rid for rid, _ in resumed.deduped] == [flags[1]]
            assert _records_for(run_root, flags[1]) == ["input", "decision"]
        finally:
            await _teardown(resumed, server, loop_task)

    asyncio.run(scenario())


def test_an_unseen_original_is_refused_as_stale_after_a_restart(short_root: Path) -> None:
    """The original never reached the log: the writer was down, the answer
    was `unknown`. After a restart its pinned replay names a superseded
    epoch and no decision holds its id, so it is refused with the re-read
    advice and nothing applies. The refusal does not claim the original
    never applied -- this client cannot know that."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        loop_task.cancel()  # nothing will be admitted while the writer is down
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task
        server.DECISION_TIMEOUT_S = 0.2
        args = (*_STATUS_ARGS, "--socket", str(server.path))
        try:
            lost = await asyncio.to_thread(_cli, *args)
            assert lost.returncode == 4, lost.stderr
            flags = _advice(lost.stderr)
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

        resumed, server, loop_task = await _resume_served(run_root)
        try:
            replay = await asyncio.to_thread(_cli, *args, *flags)
            assert replay.returncode == 2, replay.stderr
            answer = json.loads(replay.stdout)
            assert answer["refused"] is True
            assert "re-read and re-compose" in answer["error"]
            assert "original_decision" not in answer
            for claim in _NEVER_APPLIED_CLAIMS:
                assert claim not in replay.stdout + replay.stderr
            assert resumed.oracle.store.job["j"].status != "SUCCESS"
            assert _records_for(run_root, flags[1]) == []
        finally:
            await _teardown(resumed, server, loop_task)

    asyncio.run(scenario())


def test_a_host_action_recovers_its_lost_reply_through_the_same_pins(short_root: Path) -> None:
    """`host` composes the same envelope through the same composer, so its
    lost reply recovers the same way."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            args = ("host", "drain", engine.executor_id, "--socket", str(server.path))
            lost = await _lose_the_reply(server, args, "drain")
            flags = _advice(lost.stderr)
            assert flags[2:7:2] == ["--expect", "--epoch", "--baseline"]
            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 0, retry.stderr
            assert json.loads(retry.stdout)["kind"] == "drain"
            assert [rid for rid, _ in engine.deduped] == [flags[1]]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------- b-e. what the transport proves


class PartialSend:
    """A socket whose `sendall` puts every byte but the newline on the wire
    and then fails -- the partial write a client cannot tell from none."""

    def __init__(self) -> None:
        self.conn = socket.socket(socket.AF_UNIX)

    def settimeout(self, value: float) -> None:
        self.conn.settimeout(value)

    def connect(self, path: str) -> None:
        self.conn.connect(path)

    def sendall(self, payload: bytes) -> None:
        assert payload.endswith(b"\n")
        self.conn.sendall(payload[:-1])
        raise OSError("injected failure before the final newline")

    def close(self) -> None:
        self.conn.close()


def test_a_partial_sync_write_is_delivered_and_the_fragment_is_never_applied(
    short_root: Path,
) -> None:
    """Both halves of DL-216 on one request. The client reports it as
    delivered, because a write was attempted; the server drops the
    unterminated fragment at EOF, so nothing was admitted. The proof of the
    second half is the resend: it applies FRESH, with no dedup on either
    side, whichever of the two the server read first."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            state = await _call(server.path, control.versioned({"cmd": "status", "job": "j"}))
            request = control.versioned(
                control.command(
                    "CHANGE_STATUS",
                    {"job": "j", "status": "FAILURE"},
                    key="job:j",
                    revision=state["jobs"]["j"]["state_rev"],
                    baseline_id=state["baseline_id"],
                    epoch=state["epoch"],
                    request_id="partial-1",
                )
            )

            def partial() -> ControlClientError:
                fake = SimpleNamespace(AF_UNIX=socket.AF_UNIX, socket=lambda _family: PartialSend())
                with patch.object(control, "socket_mod", fake):
                    with pytest.raises(ControlClientError) as failed:
                        control.roundtrip(server.path, request)
                return failed.value

            failure = await asyncio.to_thread(partial)
            assert failure.delivered is True
            await _poll(lambda: not server._conn_tasks)
            assert engine.oracle.store.job["j"].status != "FAILURE"

            resent = await asyncio.to_thread(control.roundtrip, server.path, request)
            assert resent["ok"] is True, resent
            await _poll(lambda: not server._conn_tasks)
            assert engine.deduped == []  # the fragment was never an admission
            assert _records_for(run_root, "partial-1") == ["input", "decision"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_server_answers_a_terminated_line_and_drops_an_unterminated_one(
    short_root: Path,
) -> None:
    """R3 at the reader, isolated: the same status request answered with its
    newline and met with a silent close without it."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            line = json.dumps(control.versioned({"cmd": "status"})).encode()
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            writer.write(line)
            writer.write_eof()
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=5.0) == b""
            writer.close()
            answered = await _call(server.path, control.versioned({"cmd": "status"}))
            assert answered["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


async def _request_through(client: ControlClient, writer: object) -> dict[str, Any]:
    """Run one request over a live, idle reader and a fake writer: the
    connection is attached, not at EOF, so the request goes to the write."""
    client._reader = asyncio.StreamReader()
    client._writer = writer  # type: ignore[assignment]
    return await client.request({"cmd": "status"})


class DrainFailure:
    """A writer whose write lands and whose drain then fails."""

    wrote = False
    closed = False

    def write(self, payload: bytes) -> None:
        self.wrote = True

    async def drain(self) -> None:
        raise OSError("injected drain failure after the write")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def test_an_async_drain_failure_after_the_write_is_delivered() -> None:
    """The kernel may have taken every byte before the drain failed, so the
    failure is `delivered` -- and the TUI says NO DECISION, never "not
    sent"."""
    client = ControlClient(Path("/nowhere.sock"))
    writer = DrainFailure()
    with pytest.raises(ControlClientError) as failed:
        asyncio.run(_request_through(client, writer))
    assert writer.wrote is True
    assert failed.value.delivered is True
    assert writer.closed is True and client._writer is None  # dropped, not reused
    line = _transport_line("> ON_HOLD j", failed.value).plain
    assert "NO DECISION" in line and "not sent" not in line


def test_the_tui_says_not_sent_only_when_no_write_was_attempted() -> None:
    never = ControlClient(Path("/nowhere-at-all.sock"))
    with pytest.raises(ControlClientError) as failed:
        asyncio.run(never.request({"cmd": "status"}))
    assert failed.value.delivered is False
    assert "not sent" in _transport_line("> ON_HOLD j", failed.value).plain


def test_an_unencodable_request_fails_before_any_transport() -> None:
    """Serialized before the writer or the socket is touched, so it is the
    caller's error and never a transport outcome of either kind."""
    client = ControlClient(Path("/nowhere.sock"))
    with pytest.raises(TypeError):
        asyncio.run(client.request({"cmd": "status", "bad": object()}))
    assert client._writer is None
    with pytest.raises(TypeError):
        control.roundtrip(Path("/nowhere.sock"), {"cmd": "status", "bad": object()})


_HEADER = {"ok": True, "baseline_id": "base-1", "epoch": 7, "applied_index": 3}


async def _fake_engine(
    path: Path, on_mutation: Callable[[asyncio.StreamWriter], Awaitable[None]]
) -> asyncio.AbstractServer:
    """A socket that answers reads like an engine and hands every mutation
    to `on_mutation` -- which misbehaves."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if request.get("cmd") in ("sendevent", "host"):
                    await on_mutation(writer)
                    return
                if request.get("cmd") == "status":
                    jobs = {
                        "j": {"state_rev": 2, "on_hold": True},
                        "k": {"state_rev": 5, "on_hold": True},
                    }
                    answer: dict[str, Any] = _HEADER | {"jobs": jobs}
                else:
                    answer = _HEADER | {"hosts": {"h1": {"state_rev": 4}}}
                writer.write(json.dumps(answer).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, str(path))


async def _malformed(writer: asyncio.StreamWriter) -> None:
    writer.write(b"this is not json\n")
    await writer.drain()


async def _hang_up(writer: asyncio.StreamWriter) -> None:
    return None


@pytest.mark.parametrize("misbehave", [_malformed, _hang_up], ids=["malformed", "eof"])
def test_a_reply_that_is_malformed_or_missing_is_delivered_and_exits_4(
    short_root: Path, misbehave: Callable[[asyncio.StreamWriter], Awaitable[None]]
) -> None:
    """Both transports read either as `delivered`, and the CLI prints the
    pinned advice built from the values it placed in the envelope."""
    path = short_root / "fake.sock"

    async def scenario() -> None:
        server = await _fake_engine(path, misbehave)
        try:
            request = control.command(
                "ON_HOLD", {"job": "j"}, key="job:j", revision=2, baseline_id="b", epoch=1
            )
            with pytest.raises(ControlClientError) as sync_failed:
                await asyncio.to_thread(control.roundtrip, path, request)
            assert sync_failed.value.delivered is True

            client = ControlClient(path)
            with pytest.raises(ControlClientError) as async_failed:
                await client.request(request)
            assert async_failed.value.delivered is True
            await client.close()

            cli = await asyncio.to_thread(
                _cli, "sendevent", "ON_HOLD", "--job", "j", "--socket", str(path)
            )
            assert cli.returncode == 4, cli.stderr
            flags = _advice(cli.stderr)
            assert flags[2:] == ["--expect", "2", "--epoch", "7", "--baseline", "base-1"]
            assert _sending(cli.stderr) == flags
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_each_pin_replaces_only_its_own_read_value(short_root: Path) -> None:
    """R4: a pin replaces one read-header value; what is not pinned is read.
    Checked on the envelope the CLI placed, through its pre-send line."""
    path = short_root / "fake.sock"

    async def scenario() -> None:
        server = await _fake_engine(path, _hang_up)
        try:
            base = ("sendevent", "ON_HOLD", "--job", "j", "--socket", str(path))
            cases = [
                ((), "--expect 2 --epoch 7 --baseline base-1"),
                (("--epoch", "3"), "--expect 2 --epoch 3 --baseline base-1"),
                (("--baseline", "old"), "--expect 2 --epoch 7 --baseline old"),
                (("--expect", "9"), "--expect 9 --epoch 7 --baseline base-1"),
            ]
            for extra, pins in cases:
                sent = await asyncio.to_thread(_cli, *base, *extra)
                assert shlex.join(_sending(sent.stderr)[2:]) == pins, (extra, sent.stderr)
            host = await asyncio.to_thread(
                _cli, "host", "drain", "h1", "--epoch", "2", "--socket", str(path)
            )
            assert host.returncode == 4, host.stderr
            assert shlex.join(_sending(host.stderr)[2:]) == "--expect 4 --epoch 2 --baseline base-1"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_release_held_names_the_one_job_retry_with_its_pins(short_root: Path) -> None:
    """Each per-job id is fresh, so a sweep cannot be retried as a sweep.
    Both lines name the one-job `sendevent OFF_HOLD --job <name>` with that
    job's own revision."""
    path = short_root / "fake.sock"

    async def scenario() -> None:
        server = await _fake_engine(path, _hang_up)
        try:
            swept = await asyncio.to_thread(_cli, "release-held", "--socket", str(path))
        finally:
            server.close()
            await server.wait_closed()
        assert swept.returncode == 1, swept.stderr
        for job, revision in (("j", 2), ("k", 5)):
            verb = f"sendevent OFF_HOLD --job {job} --socket {path}"
            sending = [
                line.removeprefix("sending: ")
                for line in swept.stderr.splitlines()
                if line.startswith(f"sending: {verb} ")
            ]
            assert len(sending) == 1, swept.stderr
            argv = shlex.split(sending[0])
            assert argv[8:] == ["--expect", str(revision), "--epoch", "7", "--baseline", "base-1"]
            assert f"retry ONLY as {sending[0]}" in swept.stderr

    asyncio.run(scenario())


def test_a_cancelled_request_closes_its_connection_and_the_next_one_reconnects(
    short_root: Path,
) -> None:
    """A cancelled exchange leaves its answer unread on the stream, so the
    connection is dropped: CancelledError propagates, and the next request
    opens a fresh connection and reads its OWN answer."""
    path = short_root / "slow.sock"

    async def scenario() -> None:
        got_first = asyncio.Event()
        connections: list[int] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            connections.append(len(connections) + 1)
            number = connections[-1]
            try:
                while line := await reader.readline():
                    if number == 1:
                        got_first.set()
                        await reader.read()  # answer nothing until the client hangs up
                        return
                    request = json.loads(line)
                    writer.write(json.dumps({"ok": True, "echo": request["n"]}).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_unix_server(handle, str(path))
        client = ControlClient(path)
        try:
            task = asyncio.ensure_future(client.request({"cmd": "status", "n": 1}))
            await asyncio.wait_for(got_first.wait(), timeout=5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client._writer is None
            answer = await client.request({"cmd": "status", "n": 2})
            assert answer == {"ok": True, "echo": 2}
            assert connections == [1, 2]
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


# ------------------------------------------ f. a retry under a changed policy


def test_a_retry_denied_by_a_changed_policy_does_not_claim_the_original_never_applied(
    short_root: Path,
) -> None:
    """The perimeter decides under the CURRENT policy, before the retry route
    is reached: the exact retry of an applied command is denied once the
    caller has lost the tier, and nothing is looked up. Its words say what
    was denied and nothing about the original."""
    run_root = short_root / "run"

    async def scenario() -> None:
        map_path = _map_granting(short_root / "roles.toml", "ops")
        engine, server, loop_task, access = await _serve_armed(run_root, _ACCESS_TEXT, map_path)
        try:
            args = ("sendevent", "ON_HOLD", "--job", "acc_job", "--socket", str(server.path))
            first = await asyncio.to_thread(_cli, *args, "--request-id", "acc-1")
            assert first.returncode == 0, first.stderr
            flags = _sending(first.stderr)
            assert flags[:2] == ["--request-id", "acc-1"]

            _map_granting(short_root / "roles.toml", "read")
            access.reload()
            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 2, retry.stderr
            answer = json.loads(retry.stdout)
            assert answer["error"] == f"os/{ME} holds read tier; sendevent:ON_HOLD needs ops tier"
            assert engine.deduped == []  # denied before the lookup
            for claim in _NEVER_APPLIED_CLAIMS:
                assert claim not in retry.stdout + retry.stderr
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# --------------------------------------------- g. the collision's two facts


def test_a_collision_carries_the_ids_earlier_decision_applied_or_rejected(
    short_root: Path,
) -> None:
    """A reused id under a different command is refused, and the refusal
    carries what the id's first command decided -- applied with its
    revisions, or rejected with its reason. The CLI prints both facts and
    exits with the refusal's 2: the nested decision is never the retry's
    own outcome."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            applied = await asyncio.to_thread(
                _sendevent_cli, server.path, "ON_HOLD", "--job", "j", "--request-id", "c-1"
            )
            assert applied.returncode == 0, applied.stderr
            first = json.loads(applied.stdout)
            reused = await asyncio.to_thread(
                _sendevent_cli, server.path, "OFF_HOLD", "--job", "j", "--request-id", "c-1"
            )
            assert reused.returncode == 2, reused.stderr
            answer = json.loads(reused.stdout)
            assert answer["ok"] is False and answer["refused"] is True
            # the additive field leaves the classification alone (DL-217)
            assert control.outcome_of(answer) == control.REFUSED
            assert "decision" not in answer and "index" not in answer
            assert answer["original_decision"] == {
                "index": first["index"],
                "request_id": "c-1",
                "decision": "applied",
                "reason": None,
                "revisions": first["revisions"],
            }
            assert "baseline_id" in answer  # the header is the responding engine's
            revision = first["revisions"]["job:j"]
            assert f"refused: {answer['error']}" in reused.stderr
            assert (
                f"request_id c-1 was decided earlier: applied at index {first['index']},"
                f" revisions job:j={revision}"
            ) in reused.stderr

            rejected = await asyncio.to_thread(
                _sendevent_cli,
                server.path,
                *("OFF_HOLD", "--job", "j", "--expect", "99", "--request-id", "c-2"),
            )
            assert rejected.returncode == 3, rejected.stderr
            lost_race = json.loads(rejected.stdout)
            reused = await asyncio.to_thread(
                _sendevent_cli, server.path, "ON_ICE", "--job", "j", "--request-id", "c-2"
            )
            assert reused.returncode == 2, reused.stderr
            nested = json.loads(reused.stdout)["original_decision"]
            assert nested["decision"] == "rejected"
            assert nested["reason"] == lost_race["error"]
            assert (
                f"request_id c-2 was decided earlier: rejected at index {lost_race['index']}:"
                f" {lost_race['error']}"
            ) in reused.stderr
            assert engine.oracle.store.job["j"].on_ice is False  # the collision applied nothing
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_tui_shows_the_collision_as_both_facts() -> None:
    line = _outcome_line(
        "> OFF_HOLD j",
        {
            "ok": False,
            "refused": True,
            "error": "request_id 'x' was admitted for a different command",
            "original_decision": {
                "index": 4,
                "request_id": "x",
                "decision": "applied",
                "reason": None,
                "revisions": {},
            },
        },
    ).plain
    assert "refused: request_id 'x' was admitted" in line
    assert "request_id x was decided earlier: applied at index 4, revisions none" in line
    assert "nothing logged" not in line


def test_a_collision_with_an_undecided_original_carries_nothing_nested(
    short_root: Path,
) -> None:
    """The index knows the id but holds no decision for it yet -- the crash
    window a boundary's check can meet. The collision is still refused, and
    the answer carries no `original_decision`, because there is none to
    carry."""
    index = DecisionIndex()
    attempt = Attempt(
        index=1,
        at=datetime(2026, 7, 1, 8, 0),
        kind="ON_HOLD",
        payload={"job": "j"},
        source="control",
        request_id="u-1",
        fingerprint="fp-a",
    )
    index.note(attempt)
    with pytest.raises(RequestCollision) as collided:
        index.lookup("u-1", "fp-b")
    assert collided.value.original is None
    with pytest.raises(EngineError, match="admitted but undecided"):
        index.lookup("u-1", "fp-a")
    index.record(ApplyResult(index=1, request_id="u-1", decision="applied"))
    with pytest.raises(RequestCollision) as decided:
        index.lookup("u-1", "fp-b")
    assert decided.value.original is not None and decided.value.original.index == 1

    async def scenario() -> dict[str, Any]:
        engine, server, loop_task = await _serve(short_root / "run")
        try:
            future: asyncio.Future[ApplyResult] = asyncio.get_running_loop().create_future()
            future.set_exception(RequestCollision("reused", original=None))
            return await server._decision(future, kind="ON_HOLD")
        finally:
            await _teardown(engine, server, loop_task)

    answer = asyncio.run(scenario())
    assert answer == {"ok": False, "error": "reused", "refused": True}


# ------------------------------------- review rework: the rest of DL-216/217


class RaisingSend(PartialSend):
    """A real socket whose `sendall` puts the WHOLE line on the wire and then
    raises what a signal handler might: CPython checks for signals after the
    last successful send, before its loop test."""

    def __init__(self, raised: BaseException) -> None:
        super().__init__()
        self.raised = raised
        self.closed = False

    def sendall(self, payload: bytes) -> None:
        self.conn.sendall(payload)
        raise self.raised

    def close(self) -> None:
        self.closed = True
        super().close()


def test_any_exception_from_a_sync_write_is_delivered_but_an_interrupt_propagates(
    short_root: Path,
) -> None:
    """A RuntimeError out of `sendall` is still an attempted write, so it is
    `delivered`. KeyboardInterrupt is not wrapped: it propagates, after the
    socket is closed."""
    path = short_root / "fake.sock"

    async def scenario() -> None:
        server = await _fake_engine(path, _hang_up)
        try:
            for raised, wrapped in (
                (RuntimeError("handler raised"), True),
                (KeyboardInterrupt(), False),
            ):
                fake = RaisingSend(raised)
                module = SimpleNamespace(AF_UNIX=socket.AF_UNIX, socket=lambda _f, fake=fake: fake)

                def send(module: SimpleNamespace = module) -> BaseException:
                    with patch.object(control, "socket_mod", module):
                        try:
                            control.roundtrip(path, {"cmd": "status"})
                        except BaseException as exc:  # noqa: BLE001 -- the subject
                            return exc
                    raise AssertionError("expected the injected failure")

                got = await asyncio.to_thread(send)
                assert fake.closed is True
                if wrapped:
                    assert isinstance(got, ControlClientError) and got.delivered is True
                    assert "handler raised" in str(got)
                else:
                    assert isinstance(got, KeyboardInterrupt)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


class RaisingDrain(DrainFailure):
    async def drain(self) -> None:
        raise RuntimeError("handler raised out of the drain")


def test_any_exception_after_an_async_write_is_delivered_and_before_it_propagates() -> None:
    client = ControlClient(Path("/nowhere.sock"))
    writer = RaisingDrain()
    with pytest.raises(ControlClientError) as failed:
        asyncio.run(_request_through(client, writer))
    assert failed.value.delivered is True
    assert writer.closed is True and client._writer is None

    async def refuse_to_open(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no connection was ever made")

    with patch.object(asyncio, "open_unix_connection", refuse_to_open):
        with pytest.raises(RuntimeError, match="no connection was ever made"):
            asyncio.run(client.request({"cmd": "status"}))
    assert client._writer is None


def test_an_idle_connection_the_engine_closed_is_replaced_before_the_write(
    short_root: Path,
) -> None:
    """An engine restart closes the client's idle persistent connection. The
    next request sees the reader at EOF, reconnects, and reads its own
    answer -- rather than writing into the dead connection and reporting an
    unknown outcome for a request no engine ever saw."""
    path = short_root / "restart.sock"

    async def scenario() -> None:
        connections: list[int] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            connections.append(len(connections) + 1)
            try:
                line = await reader.readline()
                if line:
                    request = json.loads(line)
                    writer.write(json.dumps({"ok": True, "echo": request["n"]}).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()  # one answer per connection: the "restart"

        server = await asyncio.start_unix_server(handle, str(path))
        client = ControlClient(path)
        try:
            assert await client.request({"cmd": "status", "n": 1}) == {"ok": True, "echo": 1}
            reader = client._reader
            assert reader is not None
            await _poll(reader.at_eof)
            assert await client.request({"cmd": "status", "n": 2}) == {"ok": True, "echo": 2}
            assert connections == [1, 2]
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_the_pre_send_record_precedes_the_write_and_survives_the_clients_death(
    short_root: Path,
) -> None:
    """The engine decides and holds its answer. The client's record is read
    off its stderr while it is still alive and unanswered; the client is
    SIGKILLed; the record alone, after the original arguments, recovers the
    original decision."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        decided, released = asyncio.Event(), asyncio.Event()
        original_send = server._send

        async def hold(writer: asyncio.StreamWriter, response: dict[str, Any]) -> None:
            if response.get("kind") == "STATUS":
                decided.set()
                await released.wait()
                return  # the answer is never written
            await original_send(writer, response)

        args = (*_STATUS_ARGS, "--socket", str(server.path))
        try:
            with patch.object(server, "_send", hold):
                proc = subprocess.Popen(
                    [sys.executable, "-m", "dsl41", *args],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert proc.stderr is not None and proc.stdout is not None
                try:
                    record = await asyncio.wait_for(
                        asyncio.to_thread(proc.stderr.readline), timeout=30.0
                    )
                    assert record.startswith("sending: "), record
                    await asyncio.wait_for(decided.wait(), timeout=10.0)
                    assert proc.poll() is None  # alive, and no answer has been written
                    proc.send_signal(signal.SIGKILL)
                    await asyncio.to_thread(proc.wait, 10)
                    assert proc.returncode == -signal.SIGKILL
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
                    proc.stderr.close()
                    released.set()
            flags = shlex.split(record.removeprefix("sending: "))
            retry = await asyncio.to_thread(_cli, *args, *flags)
            assert retry.returncode == 0, retry.stderr
            assert [rid for rid, _ in engine.deduped] == [flags[1]]
            assert _records_for(run_root, flags[1]) == ["input", "decision"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_a_collision_after_a_same_period_restart_carries_the_decision_from_the_wal(
    short_root: Path,
) -> None:
    """The restarted engine's index is rebuilt from the WAL, so a reused id
    under a different command is refused with the ORIGINAL decision nested,
    as the first engine would have answered."""
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root)
        try:
            applied = await asyncio.to_thread(
                _sendevent_cli, server.path, "ON_HOLD", "--job", "j", "--request-id", "w-1"
            )
            assert applied.returncode == 0, applied.stderr
            first = json.loads(applied.stdout)
        finally:
            await _teardown(engine, server, loop_task)

        resumed, server, loop_task = await _resume_served(run_root)
        try:
            reused = await asyncio.to_thread(
                _sendevent_cli, server.path, "OFF_HOLD", "--job", "j", "--request-id", "w-1"
            )
            assert reused.returncode == 2, reused.stderr
            answer = json.loads(reused.stdout)
            assert answer["epoch"] == resumed.epoch != first["epoch"]
            assert answer["original_decision"] == {
                "index": first["index"],
                "request_id": "w-1",
                "decision": "applied",
                "reason": None,
                "revisions": first["revisions"],
            }
            assert "request_id w-1 was decided earlier: applied" in reused.stderr
        finally:
            await _teardown(resumed, server, loop_task)

    asyncio.run(scenario())
