"""A failed engine says nothing more (DL-563).

When a store write raises, a live `StatechartError` is met, or an exception leaves the engine
loop, memory may hold what the store does not. One latch, `Engine.failure`, is set where the
failure is first seen, and the two exits that read memory read it: the control server writes no
line after it, and the status hook's long-running check enqueues nothing. A client that had no
answer sees its connection close once the teardown runs, as for a killed engine.

Each test observes the server: every line the server writes is recorded with the latch's state
at the write (`_Wire`), and handlers are parked at named points so the order is the test's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_runner_control import _control_call, _serve, _versioned

from dsl41.ir import lower_source
from dsl41.oracle_state import Event, JobStatus, RuntimeState
from dsl41.runner import Engine
from dsl41.runner_access import Tier
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import TimeHalfStop
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import read_journal, read_records
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import SqliteStore
from dsl41.state_machine import UnhandledEvent
from dsl41.status_hook import HookSettings, StatusHooks

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)

TIMEOUT_S = 30.0
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: localhost\n"


async def _until(predicate: Callable[[], object]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


class _Wire:
    """The server's side of every control connection: each line it writes, with whether the
    engine's latch was set when it wrote it, and each `_send` it attempted under the latch.
    Handlers park at the points `parking` names until `release` is set."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.engine: Engine | None = None
        self.writes: list[tuple[bool, bytes]] = []
        self.latched_sends = 0
        self.parking = False
        self.parked: list[str] = []
        self.release = asyncio.Event()
        writers: set[asyncio.StreamWriter] = set()
        wire = self
        real_handle, real_send = ControlServer._handle, ControlServer._send
        real_write = asyncio.StreamWriter.write
        real_dispatch, real_read_store = ControlServer._dispatch, ControlServer._read_store
        real_subscribe = ControlServer._subscribe

        def latched() -> bool:
            assert wire.engine is not None
            return wire.engine.failure.is_set

        async def handle(server: ControlServer, reader: Any, writer: Any) -> None:
            writers.add(writer)
            await real_handle(server, reader, writer)

        def write(writer: asyncio.StreamWriter, data: Any) -> None:
            if writer in writers:
                wire.writes.append((latched(), bytes(data)))
            real_write(writer, data)

        async def send(server: ControlServer, writer: Any, obj: dict[str, Any]) -> None:
            if latched():
                wire.latched_sends += 1
            await real_send(server, writer, obj)

        async def park(name: str) -> None:
            if wire.parking:
                wire.parked.append(name)
                await wire.release.wait()

        async def dispatch(
            server: ControlServer, request: dict[str, Any], *, authority: Tier = Tier.ADM
        ) -> dict[str, Any]:
            if request.get("cmd") == "status":
                await park("status")
            return await real_dispatch(server, request, authority=authority)

        async def read_store(server: ControlServer, journal: Any, work: Any) -> dict[str, Any]:
            await park("history")
            return await real_read_store(server, journal, work)

        async def prepare(server: ControlServer, *, update: bool) -> dict[str, Any]:
            # the worker's own work is not under test: what the handler answers after it is
            await park("update" if update else "reread")
            rev = server.engine.config_rev
            if update:
                return {"ok": True, "token": "t", "live_rev": rev}
            return {"ok": True, "live_rev": rev, "preview": {}}

        async def subscribe(server: ControlServer, writer: Any, request: dict[str, Any]) -> None:
            await park("subscribe")
            await real_subscribe(server, writer, request)

        monkeypatch.setattr(ControlServer, "_handle", handle)
        monkeypatch.setattr(asyncio.StreamWriter, "write", write)
        monkeypatch.setattr(ControlServer, "_send", send)
        monkeypatch.setattr(ControlServer, "_dispatch", dispatch)
        monkeypatch.setattr(ControlServer, "_read_store", read_store)
        monkeypatch.setattr(ControlServer, "_prepare_reload", prepare)
        monkeypatch.setattr(ControlServer, "_subscribe", subscribe)


_OPEN: list[asyncio.StreamWriter] = []


async def _send_only(path: Path, request: dict[str, Any]) -> asyncio.StreamReader:
    """Send one request and leave its answer unread; the connection stays open, its writer
    held so that no collection closes it."""
    reader, writer = await asyncio.open_unix_connection(str(path), limit=1 << 26)
    _OPEN.append(writer)
    writer.write(json.dumps(_versioned(request)).encode() + b"\n")
    await writer.drain()
    return reader


async def _teardown(engine: Engine, server: ControlServer, loop: asyncio.Task[Any]) -> None:
    while _OPEN:
        writer = _OPEN.pop()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    await server.close()
    with contextlib.suppress(Exception):
        await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _raise_on_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    real = RuntimeState.transition

    def transition(
        self: RuntimeState,
        job: str,
        new: JobStatus,
        at: datetime | None,
        exit_code: int | None = None,
        *,
        clear_exit_code: bool = False,
    ) -> None:
        if new == "STARTING":
            raise UnhandledEvent("job j: a declared machine finds no row")
        real(self, job, new, at, exit_code, clear_exit_code=clear_exit_code)

    monkeypatch.setattr(RuntimeState, "transition", transition)


def _fail_append(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """The store refuses the append that carries a record of `kind`, as a full disk does."""
    real = SqliteStore.append

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if any(
            r.kind == kind and (kind != "input" or r.payload.get("kind") == "ON_HOLD")
            for r in records
        ):
            raise sqlite3.OperationalError("database or disk is full")
        return real(store, *records, **rows)

    monkeypatch.setattr(SqliteStore, "append", append)


WAYS = [
    "store_write_in_the_loop",
    "outcome_append_in_an_adapter_task",
    "statechart_error_in_the_loop",
    "statechart_error_in_a_handler",
    "exception_out_of_the_loop",
]


@pytest.mark.parametrize("way", WAYS)
def test_dl563_a_failed_engine_writes_nothing_more_on_any_connection(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, way: str
) -> None:
    run_root = short_root / "run"
    wire = _Wire(monkeypatch)
    crash = {"armed": False}
    real_next = Engine._next_work

    def next_work(self: Engine, *args: Any) -> Any:
        if crash["armed"]:
            raise RuntimeError("the loop crashed")
        return real_next(self, *args)

    def timers(_server: ControlServer) -> dict[str, Any]:
        raise UnhandledEvent("a query met a gap")

    monkeypatch.setattr(Engine, "_next_work", next_work)
    monkeypatch.setattr(ControlServer, "_timers", timers)
    adapter = FakeAdapter({("j", 1): (0.05, 0)}, default=None)

    async def scenario() -> dict[str, Any]:
        engine, server, loop = await _serve(run_root, _JIL, adapter=adapter)
        wire.engine = engine
        server.DECISION_TIMEOUT_S = 0.2  # type: ignore[misc]
        server.on_statechart_error = loop.cancel  # as `dsl41 run` wires it
        seen: dict[str, Any] = {}
        try:
            # answered and fully written before the failure; read only after the teardown
            answered = await _send_only(server.path, {"cmd": "status"})
            await _until(lambda: len(wire.writes) == 1)
            wire.parking = True
            ready = {
                "status": await _send_only(server.path, {"cmd": "status"}),
                "reread": await _send_only(server.path, {"cmd": "reread"}),
                "update": await _send_only(
                    server.path,
                    {"cmd": "update", "request_id": "u1", "baseline_id": engine.baseline_id},
                ),
                "history": await _send_only(server.path, {"cmd": "history", "job": "j"}),
                "subscribe": await _send_only(server.path, {"cmd": "subscribe"}),
            }
            await _until(lambda: sorted(wire.parked) == sorted(ready))
            before = engine.journal.store.last_seq() if engine.journal is not None else 0
            seen["before"] = before
            now = engine.clock.now()
            if way == "store_write_in_the_loop":
                _fail_append(monkeypatch, "input")
                engine.inject(Event(at=now, kind="ON_HOLD", payload={"job": "j"}))
            elif way == "outcome_append_in_an_adapter_task":
                _fail_append(monkeypatch, "outcome")
                engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
            elif way == "statechart_error_in_the_loop":
                _raise_on_starting(monkeypatch)
                engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
            elif way == "statechart_error_in_a_handler":
                await _send_only(server.path, {"cmd": "timers"})
            else:
                crash["armed"] = True
                engine.wake()
            await _until(lambda: engine.failure.is_set)
            await _until(loop.done)
            seen["cause"] = engine.failure.cause
            seen["on_hold"] = engine.oracle.store.job["j"].on_hold
            seen["status"] = engine.oracle.store.job["j"].status
            wire.parking = False
            later = {
                "status": await _send_only(server.path, {"cmd": "status"}),
                "subscribe": await _send_only(server.path, {"cmd": "subscribe"}),
            }
            wire.release.set()
            # every handler has tried its line: five parked, two that came after
            await _until(lambda: wire.latched_sends >= len(ready) + len(later))
            seen["writes"] = list(wire.writes)
            await server.close()
            async with asyncio.timeout(TIMEOUT_S):
                seen["cut"] = {
                    name: await reader.read()
                    for name, reader in [
                        *ready.items(),
                        *(("later " + k, v) for k, v in later.items()),
                    ]
                }
                seen["answered"] = await answered.readline()
                seen["answered_then"] = await answered.read()
        finally:
            await _teardown(engine, server, loop)
        return seen

    seen = asyncio.run(scenario())
    latched = [data for was_set, data in seen["writes"] if was_set]
    assert latched == []
    assert len(seen["writes"]) == 1  # the answer written before the failure, and nothing else
    # each client sees its connection close with no answer
    assert seen["cut"] == dict.fromkeys(seen["cut"], b"")
    assert json.loads(seen["answered"])["ok"] is True and seen["answered_then"] == b""
    records = read_journal(run_root)
    if way == "store_write_in_the_loop":
        # memory took the hold; the store never did, and nothing said otherwise
        assert isinstance(seen["cause"], sqlite3.OperationalError)
        assert seen["on_hold"] is True
        assert not [r for r in records if r.get("kind") == "ON_HOLD"]
    elif way == "outcome_append_in_an_adapter_task":
        assert isinstance(seen["cause"], sqlite3.OperationalError)
        # the raw records, as a restart reads them: the dispatch, and no outcome
        kinds = [record.kind for _, record in read_records(run_root)]
        assert kinds.count("dispatch") == 1 and "outcome" not in kinds
    elif way == "statechart_error_in_the_loop":
        assert isinstance(seen["cause"], UnhandledEvent)
        assert seen["status"] == "INACTIVE"
        assert not [r for r in records if r.get("kind") == "STARTJOB"]
    elif way == "statechart_error_in_a_handler":
        assert isinstance(seen["cause"], UnhandledEvent)
        assert read_records(run_root)[-1][0] == seen["before"]
    else:
        assert isinstance(seen["cause"], RuntimeError)
        assert read_records(run_root)[-1][0] == seen["before"]


def test_dl563_an_outcome_append_that_fails_in_an_adapter_task_latches_before_any_reply(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The append raises in the adapter task, outside the loop task, and the loop sees it only
    at its next turn. A `status` handler made ready at the failed append runs between the two,
    and writes nothing: the latch is set where the write raised, and the loop ends with it."""
    run_root = short_root / "run"
    wire = _Wire(monkeypatch)
    order: list[str] = []
    real_append, wired_send = SqliteStore.append, ControlServer._send

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if any(r.kind == "outcome" for r in records):
            wire.release.set()  # the parked handler is ready before the error leaves
            order.append("append raised")
            raise sqlite3.OperationalError("disk I/O error")
        return real_append(store, *records, **rows)

    async def send(server: ControlServer, writer: Any, obj: dict[str, Any]) -> None:
        order.append("handler sent")
        await wired_send(server, writer, obj)

    monkeypatch.setattr(ControlServer, "_send", send)
    ended: list[BaseException | None] = []

    async def scenario() -> None:
        adapter = FakeAdapter({("j", 1): (0.05, 0)}, default=None)
        engine, server, loop = await _serve(run_root, _JIL, adapter=adapter)
        wire.engine = engine
        try:
            wire.parking = True
            ready = await _send_only(server.path, {"cmd": "status"})
            await _until(lambda: wire.parked == ["status"])
            monkeypatch.setattr(SqliteStore, "append", append)
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await _until(loop.done)
            ended.append(loop.exception())
            await _until(lambda: "handler sent" in order)
            await server.close()
            async with asyncio.timeout(TIMEOUT_S):
                assert await ready.read() == b""
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())
    assert order == ["append raised", "handler sent"]
    assert isinstance(ended[0], sqlite3.OperationalError)
    assert wire.writes == []


def test_dl563_the_long_running_monitor_enqueues_nothing_once_the_engine_failed(
    short_root: Path,
) -> None:
    """A is long-running before the failure and its event is queued; B becomes long-running
    after it and is never enqueued. The queued event is still delivered."""
    out = short_root / "events"
    hook = short_root / "hook"
    hook.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(out)!r}, 'ab').write(sys.stdin.buffer.read())\n"
    )
    hook.chmod(0o755)
    t0 = datetime(2026, 7, 1, 8, 0)
    jil = "".join(
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n" for name in ("A", "B")
    )

    async def scenario() -> list[str]:
        engine = start_run(
            lower_source(jil),
            short_root / "run",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            hook_configured=True,
        )
        hooks = StatusHooks(
            engine, HookSettings(status_hook=str(hook), status_hook_long_running_s=1)
        )
        hooks.monitor.cancel()  # the checks below are the monitor's, at the test's instants
        engine.status_hooks = hooks
        try:
            for seconds, job in ((0, "A"), (3, "B")):
                at = t0 + timedelta(seconds=seconds)
                engine.inject(
                    Event(at=at, kind="STATUS", payload={"job": job, "status": "RUNNING"})
                )
                await engine.run_until_quiescent(at)
                if job == "A":
                    await engine.clock.wait_until(t0 + timedelta(seconds=2))
                    hooks.check_long_running()
                    assert hooks.delivery.queue.qsize() == 1
            await engine.clock.wait_until(t0 + timedelta(seconds=5))
            engine.fail(RuntimeError("a timer raised"))
            assert engine.failure.is_set
            hooks.check_long_running()
            assert "B" not in hooks._seen
            async with asyncio.timeout(TIMEOUT_S):
                await hooks.delivery.queue.join()
        finally:
            await hooks.close()
            with contextlib.suppress(Exception):
                await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()
        return [json.loads(line)["job"] for line in out.read_text().splitlines()]

    assert asyncio.run(scenario()) == ["A"]


@pytest.mark.parametrize("end", ["engine_stop", "cancellation"])
def test_dl563_a_clean_stop_and_a_cancellation_set_no_latch_and_end_as_before(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    """In `dsl41 run`'s order: after the loop ends either way, a read is answered. A mutation
    queued behind the end is never admitted; the server closes before the engine shuts down,
    so its client reads the connection's close, as before DL-563. A clean stop exits 0."""
    from dsl41 import cli_run

    wire = _Wire(monkeypatch)

    async def scenario() -> tuple[dict[str, Any], bytes, int, int]:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        wire.engine = engine
        server.DECISION_TIMEOUT_S = 0.2  # type: ignore[misc]
        try:
            if end == "engine_stop":
                assert (await _control_call(server.path, {"cmd": "engine_stop"}))["ok"]
                async with asyncio.timeout(TIMEOUT_S):
                    await loop
            else:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            status = await _control_call(server.path, {"cmd": "status", "job": "j"})
            assert engine.journal is not None
            last = engine.journal.store.last_seq()
            written = len(wire.writes)
            queued = await _send_only(
                server.path,
                {
                    "cmd": "sendevent",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "request_id": "queued-1",
                    "baseline_id": engine.baseline_id,
                    "expect": {"job:j": status["jobs"]["j"]["state_rev"]},
                },
            )
            await _until(lambda: engine._queue)
            run_end = cli_run._RunEnd(loop, None, None, {loop} if end == "engine_stop" else set())
            code = await cli_run._stop_and_judge(engine, server, run_end)
            async with asyncio.timeout(TIMEOUT_S):
                cut = await queued.read()
            assert not engine.failure.is_set
            assert len(wire.writes) == written  # nothing written for the queued mutation
            assert engine.journal.store.last_seq() == last  # and nothing admitted
            return status, cut, code, last
        finally:
            for writer in _OPEN:
                writer.close()
            _OPEN.clear()
            assert engine.journal is not None
            engine.journal.close()

    status, cut, code, _ = asyncio.run(scenario())
    assert status["ok"] is True and cut == b""
    if end == "engine_stop":
        assert code == 0


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (EngineError("test: a crash"), 1),
        ("time_half", 6),
        (UnhandledEvent("job j: no row and no deferral for STARTJOB"), 7),
    ],
    ids=["crash", "time_half_stop", "statechart_error"],
)
def test_dl563_dsl41_run_keeps_its_exit_status_when_the_real_loop_fails(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, raised: Any, code: int
) -> None:
    """The real loop raises; the latch is set and the exit status is the one before it. A
    time-half stop fails on a fork before live state moves, so silence there costs nothing."""
    from dsl41 import cli_run
    from dsl41.runner_admission import Attempt
    from dsl41.runner_config import runtime_profile_from_cli

    if raised == "time_half":
        attempt = Attempt(index=3, at=datetime(2026, 7, 1), request_id="engine:3", fingerprint="f")
        raised = TimeHalfStop(attempt, RuntimeError("test: a timer fault"))
    engines: list[Engine] = []

    def next_work(self: Engine, *args: Any) -> Any:
        engines.append(self)
        raise raised

    monkeypatch.setattr(Engine, "_next_work", next_work)
    exit_code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL), short_root / "root", False, [], profile=runtime_profile_from_cli()
        )
    )
    assert exit_code == code
    assert engines[0].failure.cause is raised


async def _failed_engine(
    run_root: Path, wire: _Wire
) -> tuple[Engine, ControlServer, asyncio.Task[Any]]:
    engine, server, loop = await _serve(run_root, _JIL)
    wire.engine = engine
    server.DECISION_TIMEOUT_S = 0.2  # type: ignore[misc]
    return engine, server, loop


async def _cli(*args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "dsl41",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _killed_engine(path: Path) -> asyncio.Server:
    """What a killed engine's socket gives a client: the read it answered before it died, then
    a connection that closes with no answer."""
    answered = {"n": 0}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        if request["cmd"] == "status" and answered["n"] == 0 and "answer_read" in str(path):
            answered["n"] += 1
            row = {"state_rev": 0, "status": "INACTIVE"}
            writer.write(
                json.dumps(
                    {"ok": True, "baseline_id": "b", "applied_index": 0, "jobs": {"j": row}}
                ).encode()
                + b"\n"
            )
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


@pytest.mark.parametrize(
    ("verb", "code"),
    [("read", 2), ("mutation_cut_at_its_read", 2), ("mutation_after_its_read", 4)],
)
def test_dl563_the_cli_over_a_failed_engine_ends_as_over_a_killed_one(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, cli_env: None, verb: str, code: int
) -> None:
    """A read exits 2. `sendevent` reads first: cut there it exits 2; cut after its read was
    answered, its write was attempted, so it exits 4, unknown."""
    wire = _Wire(monkeypatch)
    args = (
        ("query", "status")
        if verb == "read"
        else ("sendevent", "ON_HOLD", "-J", "j", "--request-id", "cli-1")
    )

    async def scenario() -> tuple[int, int]:
        engine, server, loop = await _failed_engine(short_root / "run", wire)
        try:
            if verb == "mutation_after_its_read":
                real_dispatch = ControlServer._dispatch

                async def dispatch(
                    server: ControlServer, request: dict[str, Any], **kw: Any
                ) -> Any:
                    if request.get("cmd") == "sendevent" and not engine.failure.is_set:
                        engine.fail(RuntimeError("a timer raised"))
                    return await real_dispatch(server, request, **kw)

                monkeypatch.setattr(ControlServer, "_dispatch", dispatch)
            else:
                engine.fail(RuntimeError("a timer raised"))
            process = await _cli(*args, "--socket", str(server.path))
            await _until(lambda: wire.latched_sends == 1)
            await server.close()
            async with asyncio.timeout(TIMEOUT_S):
                failed = await process.wait()
            killed_path = short_root / ("answer_read.sock" if code == 4 else "killed.sock")
            killed = await _killed_engine(killed_path)
            try:
                other = await _cli(*args, "--socket", str(killed_path))
                async with asyncio.timeout(TIMEOUT_S):
                    dead = await other.wait()
            finally:
                killed.close()
                await killed.wait_closed()
            return failed, dead
        finally:
            await _teardown(engine, server, loop)

    failed, dead = asyncio.run(scenario())
    assert (failed, dead) == (code, code)
    assert [data for was_set, data in wire.writes if was_set] == []


def test_dl563_the_terminal_ui_shows_the_loss_and_reconnects_with_its_table_unmarked(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A separate viewer of an engine that fails and comes back. While the socket is gone the
    subtitle says `(disconnected)` and the console says so once; the job table keeps the last
    known rows with no mark (FEAT-56). A restarted engine is read again."""
    from test_runner_tui import _wait_for_ui
    from textual.widgets import DataTable

    from dsl41.runner_tui import RunnerApp

    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop = await _serve(run_root, _JIL)
        app = RunnerApp(server.path)
        lines: list[str] = []
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "j" in app._rows)
                real_write = app._console_write
                monkeypatch.setattr(
                    app, "_console_write", lambda text: (lines.append(text.plain), real_write(text))
                )
                table = app.query_one("#jobs", DataTable)
                before = [list(map(str, table.get_row_at(i))) for i in range(table.row_count)]
                engine.fail(RuntimeError("a timer raised"))
                await _until(loop.done)
                refresh = asyncio.create_task(app._refresh())
                await asyncio.sleep(0)
                await server.close()
                await refresh
                await _wait_for_ui(pilot, lambda: app._connected is False)
                assert "(disconnected)" in app.sub_title
                after = [list(map(str, table.get_row_at(i))) for i in range(table.row_count)]
                assert after == before
                assert [line for line in lines if "unreachable" in line] != []
                await asyncio.gather(loop, return_exceptions=True)
                with contextlib.suppress(Exception):
                    await engine.shutdown()
                assert engine.journal is not None
                engine.journal.close()
                again = await resume_run(
                    lower_source(_JIL),
                    run_root,
                    clock=RealClock(),
                    adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
                    hold_open=True,
                )
                engine, server = again, ControlServer(again, run_root / "control.sock")
                await server.start()
                loop = asyncio.ensure_future(again.run_until_quiescent(datetime.max))
                await app._refresh()
                await _wait_for_ui(pilot, lambda: app._connected is True)
                assert "(disconnected)" not in app.sub_title
                assert len([line for line in lines if "unreachable" in line]) == 1
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())


def test_dl563_a_set_latch_ends_the_loop_at_its_next_turn_wherever_it_was_set(
    short_root: Path,
) -> None:
    """The latch is set where no exception reaches the loop, with an input queued while the loop
    still runs: the loop ends with its cause at its next turn and admits nothing."""

    async def scenario() -> tuple[BaseException | None, int, int]:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        try:
            assert engine.journal is not None
            before = engine.journal.store.last_seq()
            cause = sqlite3.OperationalError("a write a callback swallowed")
            engine.inject(Event(at=engine.clock.now(), kind="ON_HOLD", payload={"job": "j"}))
            engine.failure.set(cause)
            assert not loop.done()
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            raised = loop.exception()
            return raised, before, engine.journal.store.last_seq()
        finally:
            await _teardown(engine, server, loop)

    raised, before, after = asyncio.run(scenario())
    assert isinstance(raised, sqlite3.OperationalError) and before == after


def test_dl563_a_store_write_that_raises_sets_the_latch_at_the_journal(tmp_path: Path) -> None:
    """`Journal.writing` is the source: the append's raise sets the latch and calls the stop
    the engine bound to it, before the error leaves the journal."""
    from dsl41.runner_journal import Journal
    from dsl41.runner_store import MemoryStore

    journal = Journal(MemoryStore())
    told: list[Exception] = []
    later: list[Exception] = []
    journal.failure.bind(told.append, later.append)

    def append(*_records: Any, **_rows: Any) -> int:
        raise sqlite3.OperationalError("database or disk is full")

    journal.store.append = append  # type: ignore[method-assign]
    at = datetime(2026, 7, 1)
    with pytest.raises(sqlite3.OperationalError) as raised:
        journal.engine("started", at)
    assert journal.failure.cause is raised.value and told == [raised.value]
    # a second write that fails is reported once, and the first cause stays
    with pytest.raises(sqlite3.OperationalError) as again:
        journal.engine("started", at)
    journal.failure.set(again.value)
    assert journal.failure.cause is raised.value and later == [again.value]


def test_dl563_a_non_store_raise_in_an_adapter_task_latches_before_any_reply(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter task raises with no store write: a `status` handler made ready at the raise
    runs before the loop's settle re-raises it, and writes nothing (`Engine._latching`)."""
    run_root = short_root / "run"
    wire = _Wire(monkeypatch)

    class Raises(FakeAdapter):
        async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
            wire.release.set()  # the parked handler is ready before the error leaves
            raise RuntimeError("an adapter bug")

    async def scenario() -> None:
        engine, server, loop = await _serve(run_root, _JIL, adapter=Raises())
        wire.engine = engine
        try:
            wire.parking = True
            ready = await _send_only(server.path, {"cmd": "status"})
            await _until(lambda: wire.parked == ["status"])
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            await _until(loop.done)
            await _until(lambda: wire.latched_sends == 1)
            await server.close()
            async with asyncio.timeout(TIMEOUT_S):
                assert await ready.read() == b""
        finally:
            await _teardown(engine, server, loop)

    asyncio.run(scenario())
    assert wire.writes == []


@pytest.mark.parametrize("fault", ["store_write", "not_a_store_write"])
def test_dl563_a_failed_ladder_done_append_in_a_done_callback_stops_the_engine(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """KILLJOB stops `j`; its outcome commits while a child that ignores TERM keeps the ladder
    running, and the ladder's done callback appends `ladder_done`, which fails once; or the
    release after it raises with no store write. asyncio would only log that callback's raise:
    the engine stops all the same, `dsl41 run`'s judge gives exit 1, a later mutation gets no
    answer, and nothing more is committed."""
    from dsl41 import cli_run
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_config import EngineSettings
    from test_runner_control import _sendevent

    run_root = short_root / "run"
    script = short_root / "lingers.sh"
    script.write_text(
        "/bin/sh -c \"trap '' TERM; exec sleep 30\" &\ntrap 'exit 3' TERM\nsleep 30 &\nwait $!\n"
    )
    text = f"insert_job: j\njob_type: c\ncommand: exec /bin/sh {script}\nmachine: localhost\n"
    wire = _Wire(monkeypatch)
    failed: list[Any] = []
    real_append = SqliteStore.append

    real_release = Engine._release

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if not failed and any(
            r.kind == "follow_up" and r.payload.get("event") == "ladder_done" for r in records
        ):
            failed.append(records)
            raise sqlite3.OperationalError("database or disk is full")
        return real_append(store, *records, **rows)

    def release(self: Engine, job: str) -> None:
        if not failed and job == "j" and self.journal is not None:
            if any(
                r.kind == "follow_up" and r.payload.get("event") == "ladder_done"
                for _, r in self.journal.store.read_from(0)
            ):
                failed.append(job)
                raise RuntimeError("a release bug")
        real_release(self, job)

    async def scenario() -> dict[str, Any]:
        settings = EngineSettings(grace_term_s=0.5, grace_kill_s=1.0)
        engine = start_run(
            lower_source(text),
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(grace_seconds=settings.grace_term_s)},
            hold_open=True,
            settings=settings,
        )
        wire.engine = engine
        server = ControlServer(engine, run_root / "control.sock")
        server.DECISION_TIMEOUT_S = 0.2  # type: ignore[misc]
        await server.start()
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        server.on_statechart_error = loop.cancel
        seen: dict[str, Any] = {}
        try:
            assert (await _sendevent(server.path, "STARTJOB", job="j"))["ok"]
            spawned = run_root / "runs" / "j.1" / "1" / "spawn.json"
            await _until(lambda: spawned.exists() and engine.attempt_view("j") is not None)
            if fault == "store_write":
                monkeypatch.setattr(SqliteStore, "append", append)
            else:
                monkeypatch.setattr(Engine, "_release", release)
            killed = await _sendevent(server.path, "KILLJOB", job="j")
            assert killed["ok"]  # answered before the failure
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            assert engine.journal is not None
            seen["last"] = engine.journal.store.last_seq()
            late = await _send_only(
                server.path,
                {
                    "cmd": "sendevent",
                    "verb": "ON_HOLD",
                    "payload": {"job": "j"},
                    "request_id": "late-1",
                    "baseline_id": engine.baseline_id,
                    "expect": {"job:j": killed["revisions"]["job:j"]},
                },
            )
            await _until(lambda: wire.latched_sends == 1)  # its handler tried to answer
            run_end = cli_run._RunEnd(loop, None, None, {loop})
            seen["code"] = await cli_run._stop_and_judge(engine, server, run_end)
            async with asyncio.timeout(TIMEOUT_S):
                seen["late"] = await late.read()
            seen["cause"] = engine.failure.cause
            seen["after"] = engine.journal.store.last_seq()
        finally:
            for writer in _OPEN:
                writer.close()
            _OPEN.clear()
            if not loop.done():
                loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await server.close()
            assert engine.journal is not None
            engine.journal.close()
        return seen

    seen = asyncio.run(scenario())
    expected = sqlite3.OperationalError if fault == "store_write" else RuntimeError
    assert len(failed) == 1 and isinstance(seen["cause"], expected)
    assert seen["code"] == 1
    assert seen["late"] == b"" and [d for was_set, d in wire.writes if was_set] == []
    assert seen["after"] == seen["last"]
    assert not [r for _, r in read_records(run_root) if r.payload.get("request_id") == "late-1"]


@pytest.mark.parametrize("where", ["in_a_callback", "raised_by_the_loop"])
def test_dl563_the_first_failure_decides_the_exit_status(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A statechart error reaches the latch, through `Engine.fail` from a callback or raised by
    the loop itself, and a plain failure follows before the run is judged. The statechart
    error decides: the run exits 7, so the unit stays down."""
    from dsl41 import cli_run
    from dsl41.runner_config import runtime_profile_from_cli

    real_next = Engine._next_work
    first = UnhandledEvent("job j: a timer met no row")
    told: list[str] = []

    def both(engine: Engine) -> None:
        engine.fail(first)
        engine.fail(RuntimeError("a later callback raised"))

    def next_work(self: Engine, *args: Any) -> Any:
        if not told:
            told.append(where)
            if where == "in_a_callback":
                asyncio.get_running_loop().call_soon(both, self)
            else:
                later = RuntimeError("a later callback raised")
                asyncio.get_running_loop().call_soon(self.fail, later)
                raise first
        return real_next(self, *args)

    monkeypatch.setattr(Engine, "_next_work", next_work)

    async def scenario() -> int:
        serve = cli_run._serve_run(
            lower_source(_JIL), short_root / "root", False, [], profile=runtime_profile_from_cli()
        )
        return await asyncio.wait_for(serve, timeout=TIMEOUT_S)

    assert asyncio.run(scenario()) == 7


def test_dl563_a_failed_read_in_an_archive_or_retention_report_does_not_stop_the_engine(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store read that fails in a worker's report is no write: memory is not ahead of the
    store, the latch stays clear and the engine runs on, as before DL-563."""
    from dsl41 import runner_retention as retention
    from dsl41.runner_archive import Report

    async def scenario() -> tuple[bool, list[str]]:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        logged: list[str] = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: logged.append(repr(context.get("exception")))
        )
        try:
            assert engine.journal is not None
            engine._archive_bound = 2
            monkeypatch.setattr(
                engine.journal.store,
                "first_seq",
                lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
            )
            engine._retention = retention.Round(1, None)

            def listed(_round: Any, _listing: Any) -> None:
                raise OSError("a listing could not be read")

            monkeypatch.setattr(retention.Round, "listed", listed)
            running = asyncio.get_running_loop()
            running.call_soon(engine.archive_report, Report(engine._archive_round, verified=(1, 2)))
            running.call_soon(engine.retention_report, 1, retention.Listing())
            await _until(lambda: len(logged) == 2)
            await asyncio.sleep(0.05)
            return engine.failure.is_set or loop.done(), logged
        finally:
            await _teardown(engine, server, loop)

    stopped, logged = asyncio.run(scenario())
    assert stopped is False and len(logged) == 2


def test_dl563_a_store_write_that_fails_after_the_loop_ended_is_reported(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loop has ended on a clean stop; a write of the teardown then fails. No loop is left
    to raise it, so it is reported once on standard error."""

    async def scenario() -> None:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        try:
            assert (await _control_call(server.path, {"cmd": "engine_stop"}))["ok"]
            async with asyncio.timeout(TIMEOUT_S):
                await loop
            assert engine.journal is not None

            def append(*_records: Any, **_rows: Any) -> int:
                raise sqlite3.OperationalError("database or disk is full")

            engine.journal.store.append = append  # type: ignore[method-assign]
            with pytest.raises(sqlite3.OperationalError):
                engine.journal.engine("snapshot", engine.clock.now(), seq=1)
            assert engine.failure.is_set
        finally:
            await _teardown(engine, server, loop)

    capsys.readouterr()
    asyncio.run(scenario())
    err = capsys.readouterr().err
    assert err.count("dsl41: the engine failed: ") == 1
    assert "database or disk is full" in err


def test_dl563_a_latched_engine_s_loop_raises_at_every_entry(short_root: Path) -> None:
    """A snapshot write fails and the loop raises. Entered again, the loop raises the same
    cause at once: a queued `SET_GLOBAL` is never admitted and the store does not move."""
    from dsl41.runner_admission import Envelope

    async def scenario() -> tuple[list[BaseException], int, int, bool]:
        clock = VirtualClock(start=datetime(2026, 7, 1))
        engine = start_run(
            lower_source(_JIL), short_root / "run", clock=clock, adapters={"CMD": FakeAdapter()}
        )
        assert engine.journal is not None
        try:
            store = engine.journal.store

            def put_snapshot(*_args: Any) -> None:
                raise sqlite3.OperationalError("database or disk is full")

            store.put_snapshot = put_snapshot  # type: ignore[method-assign]
            with pytest.raises(sqlite3.OperationalError):
                engine.take_snapshot()
            before = store.last_seq()
            answer = engine.submit(
                Event(at=clock.now(), kind="SET_GLOBAL", payload={"name": "g", "value": "v"}),
                Envelope(request_id="g-1", expect={"global:g": 0}),
            )
            raised = []
            for _ in range(2):
                with pytest.raises(sqlite3.OperationalError) as entry:
                    await engine.run_until_quiescent(clock.now())
                raised.append(entry.value)
            return raised, before, store.last_seq(), answer.done()
        finally:
            with contextlib.suppress(Exception):
                await engine.shutdown()
            engine.journal.close()

    raised, before, after, answered = asyncio.run(scenario())
    assert raised[0] is raised[1] and before == after and not answered


class _Pair(FakeAdapter):
    """`b` fails first: it meets a statechart error, or it completes and its outcome append
    fails. `a` waits for that, then fails the other way, before the loop turns."""

    def __init__(self, chart_first: bool) -> None:
        super().__init__()
        self.chart_first = chart_first
        self.b_done = asyncio.Event()

    async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
        chart = job_ir.name == "b" if self.chart_first else job_ir.name == "a"
        if job_ir.name == "a":
            await self.b_done.wait()
        else:
            self.b_done.set()  # `a` is ready before `b`'s error leaves its task
        if chart:
            raise UnhandledEvent(f"job {job_ir.name}: a chart met no row")
        return 0  # its outcome append fails


@pytest.mark.parametrize("first", ["statechart_error", "store_write"])
def test_dl563_a_later_task_fault_never_changes_the_exit_the_first_cause_chose(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    first: str,
) -> None:
    """Jobs `a` and `b` run. `b` fails first in its adapter task, and before the loop turns
    `a` fails in its own, one with a statechart error and the other with a failed outcome
    append. Through `dsl41 run`'s judge and its verb's statechart guard the statechart error
    decides, in either order: exit 7. Both faults are on standard error."""
    from dsl41 import cli_run
    from dsl41.cli_common import statechart_report

    jil = "".join(f"insert_job: {j}\njob_type: c\ncommand: x\nmachine: localhost\n\n" for j in "ab")
    chart_first = first == "statechart_error"
    adapter = _Pair(chart_first)
    store_job = "a" if chart_first else "b"
    real_append = SqliteStore.append

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if any(r.kind == "outcome" and r.payload.get("job") == store_job for r in records):
            raise sqlite3.OperationalError("database or disk is full")
        return real_append(store, *records, **rows)

    monkeypatch.setattr(SqliteStore, "append", append)

    async def scenario() -> int:
        engine, server, loop = await _serve(short_root / "run", jil, adapter=adapter)
        try:
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "a"}))
            await _until(lambda: engine._live.get("a") is not None)
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "b"}))
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            run_end = cli_run._RunEnd(loop, None, None, {loop})
            try:
                return await cli_run._stop_and_judge(engine, server, run_end)
            except UnhandledEvent as exc:  # the `run` verb's guard (`stops_on_statechart_error`)
                return statechart_report(exc)
            except Exception as exc:  # an uncaught error ends the CLI with 1
                print(f"uncaught: {exc}", file=sys.stderr)
                return 1
        finally:
            if not loop.done():
                loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await server.close()
            assert engine.journal is not None
            engine.journal.close()

    capsys.readouterr()
    code = asyncio.run(scenario())
    err = capsys.readouterr().err
    assert code == 7
    assert "a chart met no row" in err and "database or disk is full" in err


@pytest.mark.parametrize("queued", [True, False], ids=["an_input_queued", "nothing_queued"])
def test_dl563_a_latch_set_while_the_settle_yields_raises_before_another_step(
    short_root: Path, queued: bool
) -> None:
    """On the virtual loop, `j` starts and its adapter's snapshot write fails while the loop's
    settle yields. With an ON_HOLD for `k` queued, it is never admitted; with nothing queued
    the loop does not return as quiescent. Either way the loop raises the write's error."""
    from dsl41.runner_admission import Envelope

    t0 = datetime(2026, 7, 1, 8)
    jil = "".join(f"insert_job: {j}\njob_type: c\ncommand: x\nmachine: m1\n\n" for j in "jk")
    engines: list[Engine] = []

    class Snapshots(FakeAdapter):
        async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
            (engine,) = engines
            assert engine.journal is not None

            def put_snapshot(*_args: Any) -> None:
                raise sqlite3.OperationalError("database or disk is full")

            engine.journal.store.put_snapshot = put_snapshot  # type: ignore[method-assign]
            with contextlib.suppress(sqlite3.OperationalError):
                engine.take_snapshot()
            await ctx.clock.sleep_until(datetime.max)
            return 0

    async def scenario() -> tuple[BaseException | None, bool, int, int]:
        engine = start_run(
            lower_source(jil),
            short_root / "run",
            clock=VirtualClock(start=t0),
            adapters={"CMD": Snapshots()},
        )
        engines.append(engine)
        assert engine.journal is not None
        try:
            engine.inject(Event(at=t0, kind="STARTJOB", payload={"job": "j"}))
            hold = None
            if queued:
                hold = engine.submit(
                    Event(at=t0, kind="ON_HOLD", payload={"job": "k"}),
                    Envelope(request_id="hold-k", expect={"job:k": 0}),
                )
            raised: BaseException | None = None
            try:
                await engine.run_until_quiescent(t0)
            except sqlite3.OperationalError as exc:
                raised = exc
            held = engine.oracle.store.job["k"].on_hold
            committed = [
                r for _, r in engine.journal.store.read_from(0) if r.request_id == "hold-k"
            ]
            return raised, held, len(committed), 0 if hold is None else int(hold.done())
        finally:
            with contextlib.suppress(Exception):
                await engine.shutdown()
            engine.journal.close()

    raised, held, committed, answered = asyncio.run(scenario())
    assert isinstance(raised, sqlite3.OperationalError)
    assert (held, committed) == (False, 0)


def test_dl563_a_failed_engine_admits_no_input(short_root: Path) -> None:
    """`_admit_and_apply` is where every input passes: with the latch set it raises the cause
    before anything moves, and the store does not change."""
    from dsl41.runner import _Pending

    t0 = datetime(2026, 7, 1, 8)

    async def scenario() -> tuple[bool, int, int]:
        engine = start_run(
            lower_source(_JIL),
            short_root / "run",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        assert engine.journal is not None
        try:
            before = engine.journal.store.last_seq()
            engine.failure.set(sqlite3.OperationalError("database or disk is full"))
            hold = Event(at=t0, kind="ON_HOLD", payload={"job": "j"})
            with pytest.raises(sqlite3.OperationalError):
                await engine._admit_and_apply(_Pending(at=t0, request_id=None, ev=hold))
            return engine.oracle.store.job["j"].on_hold, before, engine.journal.store.last_seq()
        finally:
            with contextlib.suppress(Exception):
                await engine.shutdown()
            engine.journal.close()

    held, before, after = asyncio.run(scenario())
    assert held is False and before == after


class _Faults(FakeAdapter):
    """`b` fails first, then `a` before the loop turns. A fault is an exception its task
    raises, or "write": the run completes and its outcome append fails."""

    def __init__(self, faults: dict[str, Any]) -> None:
        super().__init__()
        self.faults = faults
        self.b_done = asyncio.Event()

    async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
        if job_ir.name == "a":
            await self.b_done.wait()
        else:
            self.b_done.set()  # `a` is ready before `b`'s fault leaves its task
        fault = self.faults[job_ir.name]
        if isinstance(fault, Exception):
            raise fault
        return 0


def _time_half() -> TimeHalfStop:
    from dsl41.runner_admission import Attempt

    attempt = Attempt(index=3, at=datetime(2026, 7, 1), request_id="engine:3", fingerprint="f")
    return TimeHalfStop(attempt, RuntimeError("a timer fault"))


@pytest.mark.parametrize(
    ("b", "a", "code"),
    [
        ("chart", "plain", 7),
        ("plain", "chart", 7),
        ("write", "time_half", 6),
        ("time_half", "write", 6),
        ("plain", "write", 1),
    ],
)
def test_dl563_the_exit_status_is_the_gravest_fault_in_any_order(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    b: str,
    a: str,
    code: int,
) -> None:
    """Two adapter tasks fail before the loop turns. Whatever their order, a statechart error
    gives 7, else a time-half stop 6, else 1; the later fault is on standard error."""
    from dsl41 import cli_run
    from dsl41.cli_common import statechart_report

    def make(kind: str, job: str) -> Any:
        return {
            "chart": UnhandledEvent(f"job {job}: a chart met no row"),
            "plain": RuntimeError(f"job {job}: an adapter bug"),
            "time_half": _time_half(),
            "write": "write",
        }[kind]

    adapter = _Faults({"a": make(a, "a"), "b": make(b, "b")})
    writes = {job for job, kind in (("a", a), ("b", b)) if kind == "write"}
    real_append = SqliteStore.append

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if any(r.kind == "outcome" and r.payload.get("job") in writes for r in records):
            raise sqlite3.OperationalError("database or disk is full")
        return real_append(store, *records, **rows)

    monkeypatch.setattr(SqliteStore, "append", append)
    jil = "".join(f"insert_job: {j}\njob_type: c\ncommand: x\nmachine: localhost\n\n" for j in "ab")

    async def scenario() -> int:
        engine, server, loop = await _serve(short_root / "run", jil, adapter=adapter)
        try:
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "a"}))
            await _until(lambda: engine._live.get("a") is not None)
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "b"}))
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            run_end = cli_run._RunEnd(loop, None, None, {loop})
            try:
                return await cli_run._stop_and_judge(engine, server, run_end)
            except UnhandledEvent as exc:  # the `run` verb's guard (`stops_on_statechart_error`)
                return statechart_report(exc)
        finally:
            if not loop.done():
                loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await server.close()
            assert engine.journal is not None
            engine.journal.close()

    capsys.readouterr()
    assert asyncio.run(scenario()) == code
    assert "a further failure after the first" in capsys.readouterr().err


def test_dl563_a_second_failed_write_is_reported(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An append fails, then a prune step fails too: the latch keeps the append's error as its
    cause and the prune's error is reported on standard error."""

    async def scenario() -> tuple[BaseException | None, str]:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        try:
            assert engine.journal is not None
            store = engine.journal.store

            def append(*_records: Any, **_rows: Any) -> int:
                raise sqlite3.OperationalError("the append failed")

            def prune_step(*_args: Any) -> Any:
                raise sqlite3.OperationalError("the prune failed")

            store.append = append  # type: ignore[method-assign]
            store.prune_step = prune_step  # type: ignore[method-assign]
            with pytest.raises(sqlite3.OperationalError):
                engine.journal.engine("snapshot", engine.clock.now(), seq=1)
            with pytest.raises(sqlite3.OperationalError):
                engine._prune_step(1)
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            return loop.exception(), capsys.readouterr().err
        finally:
            await _teardown(engine, server, loop)

    capsys.readouterr()
    raised, err = asyncio.run(scenario())
    assert str(raised) == "the append failed"
    assert err.count("the prune failed") == 1


def test_dl563_a_query_that_raises_after_the_failure_is_reported_locally(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `history` read raises after the latch is set: nothing reaches the wire, and the fault
    is on standard error."""
    wire = _Wire(monkeypatch)

    async def read_store(_server: ControlServer, _journal: Any, _work: Any) -> dict[str, Any]:
        raise OSError("the history read failed")

    async def scenario() -> bytes:
        engine, server, loop = await _serve(short_root / "run", _JIL)
        wire.engine = engine
        monkeypatch.setattr(ControlServer, "_read_store", read_store)
        try:
            engine.fail(RuntimeError("a timer raised"))
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            asked = await _send_only(server.path, {"cmd": "history", "job": "j"})
            await _until(lambda: wire.latched_sends == 1)
            await server.close()
            async with asyncio.timeout(TIMEOUT_S):
                return await asked.read()
        finally:
            await _teardown(engine, server, loop)

    capsys.readouterr()
    assert asyncio.run(scenario()) == b""
    assert "the history read failed" in capsys.readouterr().err
    assert wire.writes == []


def test_dl563_a_failed_engine_s_shutdown_raises_nothing_over_the_judge(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An adapter's outcome append fails, the first cause. Before the loop turns, a ready
    `status` handler meets a statechart error: `stop_on` cancels the loop, which never raises
    the cause. The judge gives 7, the shutdown raises nothing over it, and both faults are on
    standard error."""
    from dsl41 import cli_run
    from dsl41.cli_common import statechart_report

    wire = _Wire(monkeypatch)
    real_append = SqliteStore.append

    def append(store: SqliteStore, *records: Any, **rows: Any) -> int:
        if any(r.kind == "outcome" for r in records):
            wire.release.set()  # the parked handler runs before the loop's next turn
            raise sqlite3.OperationalError("database or disk is full")
        return real_append(store, *records, **rows)

    def status(_server: ControlServer, _request: Any) -> dict[str, Any]:
        raise UnhandledEvent("a query met a gap")

    async def scenario() -> int:
        adapter = FakeAdapter({("j", 1): (0.05, 0)}, default=None)
        engine, server, loop = await _serve(short_root / "run", _JIL, adapter=adapter)
        wire.engine = engine
        server.on_statechart_error = loop.cancel  # as `dsl41 run` wires it
        try:
            wire.parking = True
            await _send_only(server.path, {"cmd": "status"})
            await _until(lambda: wire.parked == ["status"])
            monkeypatch.setattr(ControlServer, "_status", status)
            monkeypatch.setattr(SqliteStore, "append", append)
            engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            async with asyncio.timeout(TIMEOUT_S):
                await asyncio.wait({loop})
            assert loop.cancelled() and isinstance(engine.failure.cause, sqlite3.OperationalError)
            run_end = cli_run._RunEnd(loop, None, None, {loop})
            try:
                return await cli_run._stop_and_judge(engine, server, run_end)
            except UnhandledEvent as exc:  # the `run` verb's guard (`stops_on_statechart_error`)
                return statechart_report(exc)
            except Exception as exc:  # an uncaught error ends the CLI with 1
                print(f"uncaught: {exc}", file=sys.stderr)
                return 1
        finally:
            for writer in _OPEN:
                writer.close()
            _OPEN.clear()
            await server.close()
            assert engine.journal is not None
            engine.journal.close()

    capsys.readouterr()
    code = asyncio.run(scenario())
    err = capsys.readouterr().err
    assert code == 7
    assert "a query met a gap" in err and "database or disk is full" in err
    assert "uncaught" not in err


def test_dl563_a_handler_s_statechart_error_with_no_stop_wired_still_fails_the_engine(
    short_root: Path,
) -> None:
    """An embedder that wires no `on_statechart_error`: `stop_on` records the error, sets
    `failed` and the latch, and the loop raises the error at its next entry."""

    async def scenario() -> tuple[ControlServer, Engine, BaseException | None]:
        engine = start_run(
            lower_source(_JIL),
            short_root / "run",
            clock=VirtualClock(start=datetime(2026, 7, 1)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        server = ControlServer(engine, short_root / "run" / "control.sock")
        assert server.on_statechart_error is None
        error = UnhandledEvent("a handler met a gap")
        try:
            server.stop_on(error)
            raised: BaseException | None = None
            try:
                await engine.run_until_quiescent(engine.clock.now())
            except UnhandledEvent as exc:
                raised = exc
            return server, engine, raised
        finally:
            with contextlib.suppress(Exception):
                await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    server, engine, raised = asyncio.run(scenario())
    assert server.statechart_error is engine.failure.cause is raised
    assert server.failed.is_set()
