"""subscription as a statechart (docs/statechart-engine.md §16).

The chart is well formed and draws no warning. The generated completeness test
runs every witness configuration by every event and payload partition, and a
table written from control-protocol §5 and the block doc, not from the rows,
pins each cell. Every event name a poster sends is in the chart's alphabet, and
the chart's own actions raise nothing. A statechart error that a control
handler meets, under `_respond` or under `_subscribe`, is not answered: the
connection hangs up and `dsl41 run` stops with exit 7 and the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc
from test_boundary import C2_JIL, _close, _genesis, _request, _seal, _seal_request_wire, _stage
from test_runner_control import _serve, _teardown, _versioned

from dsl41 import runner, runner_control, runner_journal
from dsl41.boundary import BoundaryFailStop
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event
from dsl41.period import wal_path
from dsl41.runner import SEAL_BOUNDARY, Engine
from dsl41.runner_control import ControlServer
from dsl41.runner_journal import (
    APPEND,
    BACKFILL_SENT,
    STREAM_ENDS,
    SUBSCRIPTION,
    Journal,
    Offer,
    Subscription,
    read_journal,
)
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    STRICT_ENV,
    Context,
    Executor,
    Outcome,
    State,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = SUBSCRIPTION
BUDGET = 100
#: the backlog a `held` witness holds, in bytes
HELD = 10


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES


# ---- the abstraction (§13)


def _open_witness(phase: str, backlog: str) -> sc.Builder:
    """A feed the tier built: in `backfill` or `live`, with an empty backlog or
    one that holds `HELD` bytes of a `BUDGET`-byte budget."""

    def build() -> tuple[Any, Any]:
        machines = Executor()
        feed = Subscription(machines, BUDGET, None)
        if backlog == "held":
            assert feed.offer({"rec": "preflight"}, HELD)
        if phase == "live":
            feed.go_live()
        return machines, feed

    return build


def _final_witness(state: str) -> sc.Builder:
    """A feed resting in a final state. The tier forgets the instance in the
    call that made it final, so the witness restores one, keyed and backed by
    a feed record of its own."""

    def build() -> tuple[Any, Any]:
        feed = Subscription(Executor(), BUDGET, None)
        machines = Executor()
        machines.restore(feed, CHART, {"feed", state}, data=feed)
        return machines, feed

    return build


#: the size of an appended record: well within the budget, exactly filling a
#: held backlog, one byte past that, and larger than the whole budget
_SIZES = {"small": 1, "exact": BUDGET - HELD, "one_over": BUDGET - HELD + 1, "oversized": 1000}

ABSTRACTION = sc.Abstraction(
    witnesses={
        **{
            f"{phase}/{backlog}": _open_witness(phase, backlog)
            for phase in ("backfill", "live")
            for backlog in ("empty", "held")
        },
        "removed": _final_witness("removed"),
        "closed": _final_witness("closed"),
    },
    partitions={
        APPEND: {label: Offer({"rec": "preflight"}, size) for label, size in _SIZES.items()}
    },
)


def _row(number: str) -> str:
    return f"subscription.{number}"


def _section_5(witness: str, event: str, label: str) -> str:
    """The cell as control-protocol §5 and the block doc state it, written
    without the rows.

    A record enters an empty backlog; otherwise only if the backlog plus the
    record stays within the budget, and a refused record removes the feed.
    The backfill sent moves the feed live, and its stream's end closes it. A
    removed or closed feed takes nothing."""
    if witness in ("removed", "closed"):
        return f"ignored {_row({APPEND: '06', BACKFILL_SENT: '07', STREAM_ENDS: '08'}[event])}"
    if event == BACKFILL_SENT:
        return _row("02")
    if event == STREAM_ENDS:
        return _row("04")
    held = HELD if witness.endswith("/held") else 0
    fits = held == 0 or held + _SIZES[label] <= BUDGET
    return _row("05") if fits else _row("03")


def test_the_completeness_table_is_section_5() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    expected = {cell: _section_5(*cell) for cell in table}
    assert table == expected
    assert {event for _, event, _ in table} == set(alphabet(CHART))
    # the oversized record fits an empty backlog (the one-record rule) and
    # removes a held one
    assert table[("live/empty", APPEND, "oversized")] == _row("05")
    assert table[("live/held", APPEND, "oversized")] == _row("03")
    assert table[("backfill/held", APPEND, "exact")] == _row("05")
    assert table[("backfill/held", APPEND, "one_over")] == _row("03")
    # four open witnesses take four appends, a stream end and, in backfill
    # only, the backfill sent; the two finals take one cell per event
    assert len(table) == 4 * (len(_SIZES) + 1) + 2 + 2 * (len(_SIZES) + 2)


def test_a_second_backfill_sent_raises_rather_than_being_ignored() -> None:
    """The excluded cell: in `live`, `backfill_sent` finds no row, and the
    root's ignore row holds only for a feed at rest."""
    assert [(x.state, x.event) for x in CHART.exclusions] == [("live", BACKFILL_SENT)]
    machines, feed = _open_witness("live", "empty")()
    with pytest.raises(UnhandledEvent, match="no row and no deferral for backfill_sent"):
        feed.go_live()


def test_the_overflow_drops_the_backlog_and_reports_the_removal() -> None:
    """Row .03's action records the reason in ss5's words and empties the
    queue; the tier forgets the instance, and the feed keeps its state."""
    machines, feed = _open_witness("backfill", "held")()
    assert feed.offer({"rec": "preflight"}, BUDGET) is False
    assert feed.overflow == (
        f"subscriber backlog of {HELD} bytes cannot take a {BUDGET}-byte record"
        f" within the {BUDGET}-byte budget"
    )
    assert feed.empty() and feed.backlog_bytes == 0 and feed.state == "removed"
    with pytest.raises(StatechartError, match="no instance"):
        machines.active(feed)


# ---- the alphabet (DL-325)


def _spy(machines: Executor, posted: list[str]) -> None:
    """Record every event posted to a feed, and check that each post is one
    microstep at depth 0."""
    dispatch = machines.dispatch

    def spy(key: Any, event: ChartEvent) -> list[Outcome]:
        outcomes = dispatch(key, event)
        assert [outcome.depth for outcome in outcomes] == [0], outcomes
        if isinstance(key, Subscription):
            posted.append(event.name)
        return outcomes

    machines.dispatch = spy  # type: ignore[method-assign]


def test_every_event_the_posters_send_is_in_the_alphabet(short_root: Path) -> None:
    """The posters are the journal's fan-out and the subscribe handler: a
    stream with a backfill gets a live record, and ends at shutdown; a
    stalled stream overflows."""
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    posted: list[str] = []

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        _spy(engine.machines, posted)
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        try:
            writer.write(json.dumps(_versioned({"cmd": "subscribe", "since": 0})).encode() + b"\n")
            await writer.drain()
            ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            assert ack["subscribed"] is True
            journal = engine.journal
            assert journal is not None

            async def joined() -> None:
                while BACKFILL_SENT not in posted:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(joined(), timeout=5)
            journal.preflight([])  # a live record
            stalled = journal.subscribe(engine.machines, budget=1)
            journal.preflight([])
            journal.preflight([])  # the stalled feed's backlog cannot take it
            assert stalled.state == "removed"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())
    assert set(posted) == set(alphabet(CHART))
    assert posted[-1] == STREAM_ENDS  # the handler's `finally`


def test_the_chart_s_actions_raise_nothing() -> None:
    """No action raises an event or requests a continuation: the overflow
    reports the removal, for the journal to carry out, and the enqueue
    reports nothing."""
    for name, action in CHART.actions.items():
        feed = Subscription(Executor(), BUDGET, None)
        feed.backlog_bytes = HELD
        ctx = Context(feed, ChartEvent(APPEND, Offer({"rec": "preflight"}, 1)), frozenset(), feed)
        effects = action(ctx)
        assert effects is None or (effects.send == () and effects.then == ""), name


# ---- the subscription path lets a statechart error through


def test_a_statechart_error_from_an_owner_reaches_the_append(tmp_path: Path) -> None:
    """`tell_owner` contains any other owner fault (DL-267), not this one: the
    append that overflowed the feed raises it, after its record is durable and
    a feed listed later has it."""
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    error = UnhandledEvent("an owner met a gap")

    def owner(_why: str) -> None:
        raise error

    machines = Executor()
    journal.subscribe(machines, owner, budget=1)
    healthy = journal.subscribe(machines)
    journal.preflight([])
    with pytest.raises(UnhandledEvent) as raised:
        journal.preflight([])
    assert raised.value is error
    on_disk = [json.loads(line)["rec"] for line in journal.path.read_text().splitlines()]
    assert on_disk == ["preflight"] * 2
    assert [healthy.get_nowait()["rec"] for _ in range(2)] == ["preflight"] * 2
    journal.close()


def test_a_statechart_error_at_the_seal_record_s_fan_out_is_not_an_unknown_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `seal` record is published after its fsync. A statechart error in
    that fan-out leaves the boundary as itself, not as `BoundaryFailStop`, so
    the engine exits 7. The seal does not abort: the barrier stays parked and
    the head stays open on period 1. The record is durable, so a resume opens
    period 2 from it (PR-45)."""
    from test_boundary import _resume
    from test_estate import _head

    from dsl41.boundary import OpenHead

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    journal = engine.journal
    assert journal is not None
    aborted: list[Engine] = []
    monkeypatch.setattr(Engine, "abort_boundary", aborted.append)
    feed = journal.subscribe(engine.machines)
    offer = feed.offer
    error = UnhandledEvent("a feed met a gap at the seal record")

    def offered(record: dict[str, Any], size: int) -> bool:
        if record.get("rec") == "seal":
            raise error
        return offer(record, size)

    feed.offer = offered  # type: ignore[method-assign]
    with pytest.raises(UnhandledEvent) as raised:
        asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    assert raised.value is error and not isinstance(raised.value, BoundaryFailStop)
    assert aborted == [] and engine.sealing
    assert read_journal(wal_path(run_root, 1))[-1]["rec"] == "seal"
    head = _head(run_root)
    assert isinstance(head, OpenHead) and head.period_id == 1
    _close(engine)
    resumed = _resume(run_root, C2_JIL)
    _close(resumed)
    head = _head(run_root)
    assert isinstance(head, OpenHead) and head.period_id == 2


# ---- the control belt (statechart-engine ss5, concurrency-model ss4)


def _gap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: str) -> None:
    """Replace the chart with a copy that lacks one row, recording its hits
    into a private directory."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    rows = tuple(row for row in CHART.rows if row.id != _row(number))
    monkeypatch.setattr(runner_journal, "SUBSCRIPTION", replace(CHART, rows=rows))


def _with_entry(state: State, name: str, action: str) -> State:
    """`state`'s tree with `name` given an entry action."""
    if state.name == name:
        return replace(state, entry=action)
    return replace(
        state,
        regions=tuple(
            replace(region, states=tuple(_with_entry(s, name, action) for s in region.states))
            for region in state.regions
        ),
    )


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


async def _converse(
    sock: Path, request: dict[str, Any], after_ack: Any = None, *, until: int = 0
) -> list[Any]:
    """Send one request and read every line until the server hangs up, or
    until `until` lines when it is set."""
    reader, writer = await asyncio.open_unix_connection(str(sock))
    lines: list[Any] = []
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        while line := await asyncio.wait_for(reader.readline(), timeout=30):
            lines.append(json.loads(line))
            if after_ack is not None and len(lines) == 1:
                after_ack()
            if len(lines) == until:
                break
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return lines


_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _serve_with_a_client(
    run_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: Any,
    after_ack: Any = None,
) -> tuple[int, list[Any]]:
    """`dsl41 run` with one client that sends `request(engine)` once the
    socket is up. Answers the exit code and the lines the client read."""
    from dsl41 import cli_run
    from dsl41.ir import lower_source
    from dsl41.period import runtime_profile_from_cli

    real = Engine.run_until_quiescent
    clients: list[asyncio.Task[list[Any]]] = []

    async def with_a_client(self: Engine, horizon: datetime) -> list[Event]:
        sock = run_root / "control.sock"
        clients.append(asyncio.create_task(_converse(sock, request(self), after_ack)))
        return await real(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_client)

    async def scenario() -> tuple[int, list[Any]]:
        serve = cli_run._serve_run(
            lower_source(_JIL), run_root, False, [], profile=runtime_profile_from_cli()
        )
        # bounded: an engine that swallowed the error would serve on forever
        code = await asyncio.wait_for(serve, timeout=60)
        (client,) = clients
        return code, await client

    return asyncio.run(scenario())


def _timers(_self: ControlServer) -> dict[str, Any]:
    raise UnhandledEvent("a query met a gap")


def _backfill(*_args: Any, **_kwargs: Any) -> Any:
    raise UnhandledEvent("a backfill met a gap")


def _query(_engine: Engine) -> dict[str, Any]:
    return _versioned({"cmd": "timers"})


def _resume_at_zero(_engine: Engine) -> dict[str, Any]:
    return _versioned({"cmd": "subscribe", "since": 0})


def _follow(_engine: Engine) -> dict[str, Any]:
    return _versioned({"cmd": "subscribe"})


@pytest.mark.parametrize(
    "cell", ["respond/handler", "respond/chart", "subscribe/handler", "subscribe/chart"]
)
def test_a_statechart_error_in_a_control_handler_stops_the_run_with_exit_7(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cell: str,
) -> None:
    """Under `_respond`, a query raises one, or a seal request's instance start
    meets a seal chart whose entry action is missing. Under `_subscribe`, the
    backfill raises one, or the feed's chart lacks the row for the backfill
    sent. Each time the client gets no reply past the ack, the connection
    hangs up, and the run prints the report and exits 7."""
    run_root = short_root / "root"
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)

    def seal(engine: Engine) -> dict[str, Any]:
        return _seal_request_wire(engine, _stage(run_root, C2_JIL))

    request: Any
    if cell == "respond/handler":
        monkeypatch.setattr(ControlServer, "_timers", _timers)
        request, first = _query, "a query met a gap"
    elif cell == "respond/chart":
        root = _with_entry(SEAL_BOUNDARY.root, "requested", "missing")
        monkeypatch.setattr(runner, "SEAL_BOUNDARY", replace(SEAL_BOUNDARY, root=root))
        request, first = seal, "action 'missing' is not in the registry"
    elif cell == "subscribe/handler":
        monkeypatch.setattr(ControlServer, "_backfill", _backfill)
        request, first = _resume_at_zero, "a backfill met a gap"
    else:
        rows = tuple(row for row in CHART.rows if row.id != _row("02"))
        monkeypatch.setattr(runner_journal, "SUBSCRIPTION", replace(CHART, rows=rows))
        request, first = _follow, "subscription <dsl41.runner_journal.Subscription object at"
    code, lines = _serve_with_a_client(run_root, monkeypatch, request)
    assert code == 7
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: {first}"), report[:3]
    if cell.startswith("subscribe"):
        assert lines[0]["subscribed"] is True
        lines = lines[1:]
        if cell == "subscribe/chart":
            assert f"{STATECHART_LOG_PREFIX}: machine: subscription" in report
    assert lines == []  # no reply, no `internal_error`: the server hung up


def test_the_old_belt_would_have_answered_internal_error(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutant: a belt that does not know a `StatechartError` answers the
    query bug's `internal_error` and keeps the engine serving. The belt's own
    test above fails against it."""

    class NotAStatechartError(Exception):
        pass

    async def scenario() -> tuple[list[Any], list[Any], ControlServer]:
        engine, server, loop_task = await _serve(short_root / "run", _JIL)
        try:
            monkeypatch.setattr(ControlServer, "_timers", _timers)
            belt = await _converse(server.path, _query(engine))
            monkeypatch.setattr(runner_control, "StatechartError", NotAStatechartError)
            mutant = await _converse(server.path, _query(engine), until=1)
        finally:
            await _teardown(engine, server, loop_task)
        return belt, mutant, server

    belt, mutant, server = asyncio.run(scenario())
    assert belt == []  # hung up unanswered
    assert isinstance(server.statechart_error, UnhandledEvent) and server.failed.is_set()
    assert mutant == [
        {
            "ok": False,
            "code": "internal_error",
            "error": "internal error: UnhandledEvent('a query met a gap')",
        }
    ]


def test_a_handler_s_statechart_error_stops_the_loop_before_its_next_step(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handler queues a start of `j` and raises in the same instant. The
    handler cancels the loop at once: the loop takes no further step, admits
    nothing and launches nothing, and the run exits 7."""
    run_root = short_root / "root"
    raised: list[UnhandledEvent] = []
    steps_after: list[str] = []
    real_next, real_admit = Engine._next_work, Engine._admit_and_apply

    def next_work(self: Engine, *args: Any) -> Any:
        if raised:
            steps_after.append("next_work")
        return real_next(self, *args)

    async def admit(self: Engine, *args: Any) -> Any:
        if raised:
            steps_after.append("admit")
        return await real_admit(self, *args)

    def timers(self: ControlServer) -> dict[str, Any]:
        now = self.engine.clock.now()
        self.engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
        raised.append(UnhandledEvent("a query met a gap"))
        raise raised[0]

    monkeypatch.setattr(Engine, "_next_work", next_work)
    monkeypatch.setattr(Engine, "_admit_and_apply", admit)
    monkeypatch.setattr(ControlServer, "_timers", timers)
    code, lines = _serve_with_a_client(run_root, monkeypatch, _query)
    assert code == 7 and lines == []
    assert raised and steps_after == []
    report = _report(capsys.readouterr().err)
    assert report[0] == f"{STATECHART_LOG_PREFIX}: a query met a gap", report[:3]
    assert "STARTJOB" not in wal_path(run_root, 1).read_text()


def test_a_handler_s_statechart_error_beside_a_loop_crash_reports_both_and_tears_down(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both in one instant: the loop crashes after a handler met a statechart
    error. The statechart error wins: its report, then the crash on its own
    line, exit 7; the server closes and the engine shuts down."""
    run_root = short_root / "root"
    servers: list[ControlServer] = []
    torn_down: list[str] = []
    real_start, real_close = ControlServer.start, ControlServer.close
    real_next, real_shutdown = Engine._next_work, Engine.shutdown

    async def start(self: ControlServer) -> None:
        servers.append(self)
        await real_start(self)

    async def close(self: ControlServer) -> None:
        torn_down.append("server")
        await real_close(self)

    async def shutdown(self: Engine) -> None:
        torn_down.append("engine")
        await real_shutdown(self)

    def next_work(self: Engine, *args: Any) -> Any:
        if self._queue:  # the query below queued a start
            (server,) = servers
            server.stop_on(UnhandledEvent("a handler met a gap"))
            raise RuntimeError("the loop crashed")
        return real_next(self, *args)

    def timers(self: ControlServer) -> dict[str, Any]:
        now = self.engine.clock.now()
        self.engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "j"}))
        return {}

    monkeypatch.setattr(ControlServer, "start", start)
    monkeypatch.setattr(ControlServer, "close", close)
    monkeypatch.setattr(Engine, "shutdown", shutdown)
    monkeypatch.setattr(Engine, "_next_work", next_work)
    monkeypatch.setattr(ControlServer, "_timers", timers)
    code, _lines = _serve_with_a_client(run_root, monkeypatch, _query)
    assert code == 7
    err = capsys.readouterr().err.splitlines()
    report = _report("\n".join(err))
    assert report[0] == f"{STATECHART_LOG_PREFIX}: a handler met a gap", report[:3]
    assert err.index("engine failed: the loop crashed") > err.index(report[-1])
    assert torn_down == ["server", "engine"]


def test_a_statechart_error_while_the_server_closes_still_exits_7(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An operator stop with a stream attached: the handler's last post, the
    stream's end, meets a chart without its row while the server closes. A
    clean stop does not exit 0 over it: the run reports it and exits 7."""
    _gap(monkeypatch, tmp_path, "04")

    def interrupt() -> None:
        signal.raise_signal(signal.SIGINT)  # the operator's stop, as the unit sends it

    code, lines = _serve_with_a_client(short_root / "root", monkeypatch, _follow, interrupt)
    assert code == 7
    assert lines[0]["subscribed"] is True
    report = _report(capsys.readouterr().err)
    assert "no row and no deferral for stream_ends" in report[0], report[:3]
    assert not any("had already stopped" in line for line in report)


def test_a_statechart_error_while_the_server_closes_keeps_a_sealed_exit_3(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A seal ends the run with a stream attached, and the stream's end meets
    a chart without its row while the server closes. The run keeps the halt
    the engine chose: exit 3. The report's last line says so, and the report
    goes out before the next period's opener is named."""
    from dsl41 import cli_run
    from dsl41.ir import lower_source
    from dsl41.period import runtime_profile_from_cli

    _gap(monkeypatch, tmp_path, "04")
    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    clients: list[asyncio.Task[list[Any]]] = []
    order: list[str] = []

    async def a_feed_then_a_seal(self: Engine, horizon: datetime) -> list[Event]:
        subscribed = asyncio.Event()
        sock = run_root / "control.sock"
        clients.append(asyncio.create_task(_converse(sock, _follow(self), subscribed.set)))
        await subscribed.wait()
        self.submit_seal(_request(self, _stage(run_root, C2_JIL)))
        return await real(self, horizon)

    def report(exc: StatechartError) -> int:
        order.append("report")
        return real_report(exc)

    real_report = cli_run.statechart_report
    monkeypatch.setattr(Engine, "run_until_quiescent", a_feed_then_a_seal)
    monkeypatch.setattr(cli_run, "statechart_report", report)
    monkeypatch.setattr(cli_run, "say_next", lambda *_args: order.append("say_next"))

    async def scenario() -> tuple[int, list[Any]]:
        serve = cli_run._serve_run(
            lower_source(_JIL), run_root, False, [], profile=runtime_profile_from_cli()
        )
        code = await asyncio.wait_for(serve, timeout=60)
        (client,) = clients
        return code, await client

    code, lines = asyncio.run(scenario())
    assert code == 3
    assert lines[0]["subscribed"] is True
    lines_out = _report(capsys.readouterr().err)
    assert "no row and no deferral for stream_ends" in lines_out[0], lines_out[:3]
    assert lines_out[-1] == f"{STATECHART_LOG_PREFIX}: the engine had already stopped with exit 3"
    assert order == ["report", "say_next"]
