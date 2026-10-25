"""supervisor_client as a statechart (docs/statechart-engine.md §16).

The chart is well formed and draws no warning. The generated completeness test
runs every witness configuration, built through the client's own code, by
every event, and a table written from supervisor-protocol §5 and the block
doc, not from the rows, pins each cell. Every event name the client posts is
in the chart's alphabet, and the chart declares no action. A statechart error
on the client's path stops `dsl41 run` with exit 7 and the report, from the
renewal task, the reader task and the teardown's close alike; outside a run
the client keeps one a background task met, and the offline sealer exits 7.
A rule is one row on the group whose leaves all take it (DL-374): every cell
answers as DL-330's flat chart did, and a row deleted leaves a gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc
import typer
from test_runner_adapters import _ScriptedRequests

from dsl41 import runner_adapters
from dsl41.cli_common import stops_on_statechart_error
from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event
from dsl41.period import runtime_profile_from_cli, wal_path
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    CANCEL,
    CLIENT_PHASES,
    CLOSE,
    CONNECT,
    EOF,
    SUPERVISOR_CLIENT,
    SupervisorClient,
    SupervisorUnavailable,
)
from dsl41.runner_ledger import acquire_run_root
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    Executor,
    State,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = SUPERVISOR_CLIENT
_LEAVES = ("disconnected", "connected", "lost", "closed")
#: each leaf's configuration: the leaf and the groups above it
_CONFIGURATION: dict[str, frozenset[str]] = {
    "disconnected": frozenset({"client", "alive", "down", "disconnected"}),
    "connected": frozenset({"client", "alive", "connected"}),
    "lost": frozenset({"client", "alive", "down", "lost"}),
    "closed": frozenset({"client", "closed"}),
}
#: each group row and the leaves it is the rule of
_GROUP_ROWS: dict[str, tuple[str, frozenset[str]]] = {
    "supervisor_client.01": ("down", frozenset({"disconnected", "lost"})),
    "supervisor_client.05": ("alive", frozenset({"disconnected", "connected", "lost"})),
}


def _row(number: str) -> str:
    return f"supervisor_client.{number}"


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART.time_events == () and CHART.defer_bound == 0
    assert CHART in MACHINES


def _parents(state: State) -> dict[str, str]:
    """Each state below `state` to its parent."""
    found: dict[str, str] = {}
    for region in state.regions:
        for child in region.states:
            found[child.name] = state.name
            found |= _parents(child)
    return found


def _regions(state: State) -> list[str]:
    """The name of every region below `state`, nested ones included."""
    return [
        name
        for region in state.regions
        for name in (region.name, *(n for child in region.states for n in _regions(child)))
    ]


def test_the_leaves_are_the_phases_under_their_groups() -> None:
    """`alive` holds every state but `closed`, and `down` the two with no live connection
    (DL-374). `phase()` reads the leaf alone, so the groups are invisible to it; each
    region has its own name."""
    parents = _parents(CHART.root)
    leaves = {name for name in parents if name not in parents.values()}
    assert leaves == set(_LEAVES) == CLIENT_PHASES
    assert parents == {
        "alive": "client",
        "closed": "client",
        "down": "alive",
        "connected": "alive",
        "disconnected": "down",
        "lost": "down",
    }
    names = _regions(CHART.root)
    assert names == ["phase", "connection", "epoch"]
    assert set(names) & set(parents) == set()  # no region shares a state's name
    for leaf, configuration in _CONFIGURATION.items():
        assert {leaf} | {parents[name] for name in configuration if name in parents} == (
            configuration
        )


def test_the_group_rows_are_unguarded_and_sourced_at_their_groups() -> None:
    """Rows .01 and .05 are the rules of `down` and `alive`; no member has its own row for
    connect or close (DL-374). No row has a guard: the final `closed` ignores every event
    with no row (DL-380), and its ids .08 to .11 stay out."""
    rows = {row.id: row for row in CHART.rows}
    for row_id, (group, _members) in _GROUP_ROWS.items():
        assert rows[row_id].source == group and rows[row_id].guard == ""
    for row_id, (group, members) in _GROUP_ROWS.items():
        event = rows[row_id].event
        inner = {row.id for row in CHART.rows if row.event == event and row.source in members}
        assert inner == set(), row_id
        assert {leaf for leaf in _LEAVES if group in _CONFIGURATION[leaf]} == members
    assert dict(CHART.guards) == {}
    assert [row.id for row in CHART.rows if row.guard or row.source == "closed"] == []
    assert set(rows) == {_row(f"{n:02}") for n in range(1, 8)}  # a retired id stays out (DL-364)


#: Each cell as the flat chart of DL-330 answered it: the fired row and the leaf after, or
#: `UnhandledEvent` for the two excluded cells. Generated once from that declaration's
#: executor; the groups (DL-374) must answer every cell the same. In the four cells of
#: `closed` the final state ignores the event with no row (DL-380); an empty row number
#: stands for that.
FLAT: dict[tuple[str, str], tuple[str, str] | None] = {
    ("disconnected", CANCEL): None,
    ("disconnected", CLOSE): ("05", "closed"),
    ("disconnected", CONNECT): ("01", "connected"),
    ("disconnected", EOF): None,
    ("connected", CANCEL): ("04", "lost"),
    ("connected", CLOSE): ("05", "closed"),
    ("connected", CONNECT): ("02", "connected"),
    ("connected", EOF): ("03", "lost"),
    ("lost", CANCEL): ("06", "lost"),
    ("lost", CLOSE): ("05", "closed"),
    ("lost", CONNECT): ("01", "connected"),
    ("lost", EOF): ("07", "lost"),
    ("closed", CANCEL): ("", "closed"),
    ("closed", CLOSE): ("", "closed"),
    ("closed", CONNECT): ("", "closed"),
    ("closed", EOF): ("", "closed"),
}


def _restored(leaf: str, chart: Any = CHART) -> Executor:
    executor = Executor()
    executor.restore("client", chart, set(_CONFIGURATION[leaf]))
    return executor


def test_every_cell_answers_as_the_flat_chart_did() -> None:
    """16 of 16 cells: the same row fires and the same leaf is reached, with its groups;
    the two excluded cells still find no row."""
    for (leaf, event), answer in FLAT.items():
        executor = _restored(leaf)
        if answer is None:
            with pytest.raises(UnhandledEvent, match=f"no row and no deferral for {event}"):
                executor.dispatch("client", ChartEvent(event))
            continue
        (outcome,) = executor.dispatch("client", ChartEvent(event))
        rows = (_row(answer[0]),) if answer[0] else ()
        assert (outcome.kind, outcome.rows, outcome.refusals) == ("fired", rows, ())
        assert executor.active("client") == _CONFIGURATION[answer[1]], (leaf, event)
    assert len(FLAT) == len(_LEAVES) * len(alphabet(CHART))


@pytest.fixture
def private_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chart copy with a row deleted bears the chart's name; its hits go to a private
    directory so the gate does not count them."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))


@pytest.mark.usefixtures("private_hits")
@pytest.mark.parametrize("row_id", sorted(_GROUP_ROWS))
def test_a_group_row_deleted_leaves_a_gap_at_every_member(row_id: str) -> None:
    """No member has its own row for the group row's event, so a chart copy without it
    raises `UnhandledEvent` at each leaf it was the rule of, with the leaf unmoved: no
    leaf it was the rule of is final, so no implicit ignore takes the event."""
    (rule,) = (row for row in CHART.rows if row.id == row_id)
    _group, members = _GROUP_ROWS[row_id]
    gapped = replace(CHART, rows=tuple(row for row in CHART.rows if row.id != row_id))
    for leaf in sorted(members):
        executor = _restored(leaf, gapped)
        with pytest.raises(UnhandledEvent, match=f"no row and no deferral for {rule.event}"):
            executor.dispatch("client", ChartEvent(rule.event))
        assert executor.active("client") == _CONFIGURATION[leaf]


@pytest.mark.usefixtures("private_hits")
def test_no_row_deleted_falls_into_another_row() -> None:
    """Every row of the chart: without it, each cell it took is a gap and every other cell
    answers as before."""
    for row in CHART.rows:
        gapped = replace(CHART, rows=tuple(r for r in CHART.rows if r.id != row.id))
        for (leaf, event), answer in FLAT.items():
            if answer is None:
                continue
            took = (_row(answer[0]),) if answer[0] else ()
            if took == (row.id,):
                with pytest.raises(UnhandledEvent):
                    _restored(leaf, gapped).dispatch("client", ChartEvent(event))
                continue
            (other,) = _restored(leaf, gapped).dispatch("client", ChartEvent(event))
            assert other.rows == took, (row.id, leaf, event)


# ---- the abstraction (§13)


class _Writer:
    """A stream writer on an in-memory stream: closing it is the peer's EOF."""

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self.stream = stream
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.stream.feed_eof()

    async def wait_closed(self) -> None:
        pass


class _Witnesses:
    """Builders of the client's configurations, each reached through the
    client's own code on one private loop: a connection accepted, a request
    written, EOF, a cancelled request and close. Every client built is
    closed at the end."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.loop = asyncio.new_event_loop()
        self.clients: list[SupervisorClient] = []
        self.requests: list[asyncio.Task[Any]] = []

    def _run(self, step: Any) -> Any:
        return self.loop.run_until_complete(step)

    def client(self, steps: tuple[str, ...]) -> tuple[Executor, SupervisorClient]:
        machines = Executor()
        client = SupervisorClient(self.root, machines=machines)
        self.clients.append(client)
        for step in steps:
            self._run(self._step(client, step))
        return machines, client

    async def _step(self, client: SupervisorClient, step: str) -> None:
        if step == "connect":
            stream = asyncio.StreamReader()
            client._new_epoch(stream, _Writer(stream))  # type: ignore[arg-type]
        elif step == "request":
            writer = client._writer
            assert isinstance(writer, _Writer)
            self.requests.append(asyncio.ensure_future(client.signal("run", "TERM")))
            while not writer.sent:
                await asyncio.sleep(0)
        elif step == "eof":
            client._on_lost(client.lost)
        elif step == "cancel":
            request = self.requests[-1]
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await request
        else:
            await client.close()

    def builder(self, *steps: str) -> sc.Builder:
        return lambda: self.client(steps)

    def close(self) -> None:
        async def finish() -> None:
            for request in self.requests:
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError, SupervisorUnavailable):
                    await request
            for client in self.clients:
                await client.close()

        self._run(finish())
        self.loop.close()


#: witness -> the steps that build it: each state, a connected client with
#: and without a request in flight, and lost with the writer kept (EOF) and
#: dropped (a cancelled request)
_BUILDS = {
    "disconnected": (),
    "connected/idle": ("connect",),
    "connected/pending": ("connect", "request"),
    "lost/writer_kept": ("connect", "eof"),
    "lost/writer_dropped": ("connect", "request", "cancel"),
    "closed": ("connect", "close"),
}


@pytest.fixture
def witnesses(tmp_path: Path) -> Iterator[_Witnesses]:
    built = _Witnesses(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _abstraction(built: _Witnesses) -> sc.Abstraction:
    return sc.Abstraction(witnesses={name: built.builder(*s) for name, s in _BUILDS.items()})


def _section_5(witness: str, event: str) -> str:
    """The cell as supervisor-protocol §5 and the block doc state it, written
    without the rows.

    A connection the socket accepts starts an epoch: a client with no live
    connection becomes connected, and a connected one supersedes its
    connection and stays connected. EOF loses a connected epoch and changes
    nothing on a lost one. A request cancelled mid-flight poisons the
    connection: a connected client is lost, and a lost one stays lost. Close
    closes any client that is not closed. A closed client takes nothing:
    the tier posts nothing to it, and the final state ignores the event."""
    state = witness.split("/")[0]
    if state == "closed":
        return "ignored in closed"
    if event == CLOSE:
        return _row("05")
    if event == CONNECT:
        return _row("02") if state == "connected" else _row("01")
    if event == EOF:
        return _row("03") if state == "connected" else f"ignored {_row('07')}"
    return _row("04") if state == "connected" else _row("06")


def test_the_completeness_table_is_section_5(witnesses: _Witnesses) -> None:
    table = sc.completeness_table(CHART, _abstraction(witnesses))
    expected = {cell: _section_5(cell[0], cell[1]) for cell in table}
    assert table == expected
    assert (
        {event for _, event, _ in table}
        == set(alphabet(CHART))
        == {
            CONNECT,
            EOF,
            CANCEL,
            CLOSE,
        }
    )
    # six witnesses by four events, less the two cells disconnected excludes
    assert len(table) == 6 * 4 - 2


def test_the_witnesses_hold_what_they_name(witnesses: _Witnesses) -> None:
    """The abstraction's transport facts: a request in flight only where one
    can be, and the writer an EOF keeps and a poison drops."""
    seen = {}
    for name, steps in _BUILDS.items():
        _machines, client = witnesses.client(steps)
        pending = client._pending is not None and not client._pending.done()
        seen[name] = (client.phase(), client._writer is not None, pending)
    assert seen == {
        "disconnected": ("disconnected", False, False),
        "connected/idle": ("connected", True, False),
        "connected/pending": ("connected", True, True),
        "lost/writer_kept": ("lost", True, False),
        "lost/writer_dropped": ("lost", False, False),
        "closed": ("closed", False, False),
    }


@pytest.mark.parametrize("event", [EOF, CANCEL])
def test_an_eof_or_a_cancel_before_the_first_connect_raises(
    witnesses: _Witnesses, event: str
) -> None:
    """The two excluded cells: in disconnected nothing reads and nothing is
    in flight, so neither fact can arrive; one posted finds no row."""
    assert {(x.state, x.event) for x in CHART.exclusions} == {
        ("disconnected", EOF),
        ("disconnected", CANCEL),
    }
    machines, client = witnesses.client(())
    with pytest.raises(UnhandledEvent, match=f"no row and no deferral for {event}"):
        machines.dispatch(client, ChartEvent(event))


def test_a_request_before_the_first_connect_is_refused_before_its_write(
    tmp_path: Path,
) -> None:
    """Why cancel in disconnected is excluded: a request enters the write,
    the only place a cancel poisons, with a writer held and the epoch not
    lost, and only a connect gives it one. Before that it is refused."""

    async def scenario() -> list[str]:
        client = SupervisorClient(tmp_path)
        with pytest.raises(SupervisorUnavailable, match="not connected"):
            await client._request({"cmd": "PING"}, _connect=False)
        with pytest.raises(SupervisorUnavailable, match="not connected"):
            await client.signal("run", "TERM")  # the lazy connect finds no socket
        phases = [client.phase()]
        await client.close()
        return phases

    assert asyncio.run(scenario()) == ["disconnected"]


def test_a_request_cancelled_while_it_waits_for_the_lock_does_not_poison(
    tmp_path: Path,
) -> None:
    """A second request waits on the request lock while the first awaits its
    reply. Cancelled there, it has written nothing: the stream is sound, so
    nothing is poisoned and nothing is posted, and the first request still
    gets its reply."""

    async def scenario() -> tuple[Any, ...]:
        machines = _Spied()
        client = SupervisorClient(tmp_path, machines=machines)
        stream = asyncio.StreamReader()
        writer = _Writer(stream)
        client._new_epoch(stream, writer)  # type: ignore[arg-type]
        first = asyncio.ensure_future(client.signal("a", "TERM"))
        while not writer.sent:
            await asyncio.sleep(0)
        second = asyncio.ensure_future(client.signal("b", "TERM"))
        for _ in range(100):
            if client._lock._waiters:  # the second request waits on the lock
                break
            await asyncio.sleep(0)
        assert client._lock._waiters and not second.done()
        posted = list(machines.posted)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        state = (
            client.phase(),
            client.lost.is_set(),
            client._writer is writer,
            machines.posted == posted,
            [request["run_id"] for request in writer.sent],
        )
        stream.feed_data(json.dumps({"ok": True, "marker": 1}).encode() + b"\n")
        answered = await asyncio.wait_for(first, 3)
        await client.close()
        return (*state, answered)

    assert asyncio.run(scenario()) == (
        "connected",
        False,
        True,
        True,
        ["a"],
        {"ok": True, "marker": 1},
    )


# ---- the alphabet and the actions


class _Peer:
    """A supervisor stand-in on a unix socket, served from threads: it answers
    every request ok and drops every connection on demand. It needs no loop,
    so it outlives any the client runs on."""

    REPLY = {"ok": True, "token": 1, "incarnation": "i", "deadman_s": None, "expires_at": 0}
    #: a LIST with no runs, every header field the client validates
    LISTING = {
        "ok": True,
        "version": 1,
        "supervisor_pid": 1,
        "boot_id": "boot",
        "incarnation": "i",
        "deadman_s": None,
        "lease": None,
        "runs": [],
    }

    def __init__(self, path: Path, *, hang_up_on: str | None = None, answer_first: int = 0) -> None:
        #: a verb the peer hangs up on, after answering it `answer_first` times
        self.hang_up_on = hang_up_on
        self.answer_first = answer_first
        #: a verb the peer never answers
        self.silent_on: str | None = None
        self.seen: list[str] = []
        self._conns: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen()
        self._listener.settimeout(0.05)
        self._spawn(self._accept)

    def _spawn(self, target: Callable[..., None], *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            with self._lock:
                self._conns.append(conn)
            self._spawn(self._serve, conn)

    def _serve(self, conn: socket.socket) -> None:
        buffer = b""
        with contextlib.suppress(OSError):
            while chunk := conn.recv(65536):
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    cmd = json.loads(line)["cmd"]
                    self.seen.append(cmd)
                    if cmd == self.hang_up_on:
                        if self.answer_first == 0:
                            conn.shutdown(socket.SHUT_RDWR)
                            return
                        self.answer_first -= 1
                    if cmd == self.silent_on:
                        continue
                    reply = self.LISTING if cmd == "LIST" else self.REPLY
                    conn.sendall(json.dumps(reply).encode() + b"\n")

    def drop_all(self) -> None:
        with self._lock:
            for conn in self._conns:
                with contextlib.suppress(OSError):
                    conn.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self._stop.set()
        self.drop_all()
        for thread in self._threads:
            thread.join(timeout=5)
        self._listener.close()
        with self._lock:
            for conn in self._conns:
                conn.close()


class _Alive:
    """A spawned supervisor that is still running."""

    def poll(self) -> int | None:
        return None


class _Peers(list[_Peer]):
    """The peers spawned so far, and the clients that spawned them. The next
    peer hangs up on `hang_up_on` after answering it `answer_first` times."""

    hang_up_on: str | None = None
    answer_first = 0

    def __init__(self) -> None:
        super().__init__()
        self.clients: list[SupervisorClient] = []


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Peers]:
    """`SupervisorClient` spawns a `_Peer` on its socket instead of a
    supervisor."""
    started = _Peers()

    def spawn(self: SupervisorClient) -> _Alive:
        started.clients.append(self)
        started.append(
            _Peer(self.sock_path, hang_up_on=started.hang_up_on, answer_first=started.answer_first)
        )
        return _Alive()

    monkeypatch.setattr(SupervisorClient, "_spawn_supervisor", spawn)
    try:
        yield started
    finally:
        for peer in started:
            peer.close()


class _Spied(Executor):
    """An executor that records every event name posted, with the leaf it met."""

    def __init__(self) -> None:
        super().__init__()
        self.posted: list[tuple[str, str]] = []

    def dispatch(self, key: Any, event: ChartEvent) -> Any:
        if isinstance(key, SupervisorClient):
            self.posted.append((key.phase(), event.name))
        return super().dispatch(key, event)


def test_every_event_the_client_posts_is_in_the_alphabet(short_root: Path, peers: _Peers) -> None:
    """The posters are the client's own: `provision` connects, and again
    on a live connection (the supersede); the peer drops the connection
    (EOF); a reconnect, then a request cancelled while its reply is
    outstanding (the poison); and close."""
    machines = _Spied()

    async def scenario() -> None:
        client = SupervisorClient(short_root, machines=machines)
        try:
            await client.provision()
            await client.provision()
            lost = client.lost
            peers[0].drop_all()
            await asyncio.wait_for(lost.wait(), 5)
            assert await client.reconnect()
            peers[0].silent_on = "SIGNAL"
            request = asyncio.ensure_future(client.signal("run", "TERM"))
            while "SIGNAL" not in peers[0].seen:
                await asyncio.sleep(0.01)
            request.cancel()
            with contextlib.suppress(asyncio.CancelledError, SupervisorUnavailable):
                await request
        finally:
            await client.close()

    asyncio.run(scenario())
    names = {name for _, name in machines.posted}
    assert names == set(alphabet(CHART))
    assert ("connected", CONNECT) in machines.posted  # the supersede
    assert ("disconnected", CONNECT) in machines.posted and ("lost", CONNECT) in machines.posted
    assert [leaf for leaf, _ in machines.posted if leaf == "closed"] == []


def test_the_chart_declares_no_action_and_no_guard() -> None:
    """Every transport effect is the tier's (see the module's comment), so no
    row has an action; the hierarchy and the final state decide every cell,
    so no row has a guard."""
    assert CHART.actions == {}
    assert dict(CHART.guards) == {}


# ---- a statechart error on the client's path (statechart-engine ss5)


def _gapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **changes: Any) -> None:
    """Replace the chart with a copy that has a gap, recording its hits into a
    private directory so the gate does not count them."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setattr(runner_adapters, "SUPERVISOR_CLIENT", replace(CHART, **changes))


def _without(*numbers: str) -> dict[str, Any]:
    return {"rows": tuple(row for row in CHART.rows if row.id not in {_row(n) for n in numbers})}


#: a gap: the first connect is taken, a reconnect from lost is not
_RECONNECT_GAP = {
    "rows": tuple(
        replace(row, source="disconnected") if row.id == _row("01") else row for row in CHART.rows
    )
}
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


def _serve_detached(
    run_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    peers: _Peers,
    during: Callable[[Engine, _Peers], None],
) -> tuple[int, list[str]]:
    """`dsl41 run --detached` against a peer. Once the engine loop runs,
    `during(engine, peers)` acts. Answers the exit code and every step the
    loop took after the client met its statechart error."""
    from dsl41 import cli_run

    met: list[StatechartError] = []
    steps_after: list[str] = []
    real_run, real_next = Engine.run_until_quiescent, Engine._next_work
    real_admit, real_fail = Engine._admit_and_apply, SupervisorClient._fail

    async def run(self: Engine, horizon: datetime) -> list[Event]:
        during(self, peers)
        return await real_run(self, horizon)

    def next_work(self: Engine, *args: Any) -> Any:
        if met:
            steps_after.append("next_work")
        return real_next(self, *args)

    async def admit(self: Engine, *args: Any) -> Any:
        if met:
            steps_after.append("admit")
        return await real_admit(self, *args)

    def fail(self: SupervisorClient, error: StatechartError) -> None:
        # a start queued in the same instant: a loop that took another step
        # would admit it
        engine = engines[0]
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        met.append(error)
        real_fail(self, error)

    engines: list[Engine] = []

    async def run_and_keep(self: Engine, horizon: datetime) -> list[Event]:
        engines.append(self)
        return await run(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", run_and_keep)
    monkeypatch.setattr(Engine, "_next_work", next_work)
    monkeypatch.setattr(Engine, "_admit_and_apply", admit)
    monkeypatch.setattr(SupervisorClient, "_fail", fail)
    monkeypatch.setattr(SupervisorClient, "_RENEW_EVERY_S", 0.05)

    async def scenario() -> int:
        serve = cli_run._serve_run(
            lower_source(_JIL),
            run_root,
            False,
            [],
            profile=runtime_profile_from_cli(detached=True),
            supervisor="spawn",
        )
        # bounded: an engine that swallowed the error would serve on forever
        return await asyncio.wait_for(serve, timeout=60)

    code = asyncio.run(scenario())
    assert len(met) == 1
    return code, steps_after


def _drop(_engine: Engine, peers: _Peers) -> None:
    peers[0].drop_all()


@pytest.mark.parametrize("task", ["renewal", "reader"])
def test_a_statechart_error_in_a_background_task_stops_the_run_with_exit_7(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    peers: _Peers,
    task: str,
) -> None:
    """The peer drops the connection once the loop runs. The renewal task's
    reconnect meets a chart that takes only the first connect; or the reader's
    EOF meets a chart without row .03. The run prints the report once, the
    loop takes no step after the error, nothing is admitted, and `dsl41 run`
    exits 7. The discarded chart does not stop the teardown: the client
    reconnects on the transport's word, gives the lease back and closes."""
    if task == "renewal":
        _gapped(monkeypatch, tmp_path, **_RECONNECT_GAP)
        first = "no row and no deferral for connect in ['alive', 'client', 'down', 'lost']"
    else:
        _gapped(monkeypatch, tmp_path, **_without("03"))
        first = "no row and no deferral for eof in ['alive', 'client', 'connected']"
    run_root = short_root / "root"
    code, steps_after = _serve_detached(run_root, monkeypatch, peers, _drop)
    assert code == 7 and steps_after == []
    report = _report(capsys.readouterr().err)
    assert first in report[0], report[:3]
    assert sum(first in line for line in report) == 1  # the teardown meets no second one
    assert f"{STATECHART_LOG_PREFIX}: machine: supervisor_client" in report
    assert "STARTJOB" not in wal_path(run_root, 1).read_text()
    assert peers[0].seen[-1] == "RELEASE"
    (client,) = peers.clients
    assert client._closed and client._writer is None and client._reader_task is None
    acquire_run_root(run_root).release()  # the teardown released the lock


def test_a_statechart_error_during_the_lease_s_return_exits_7(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    peers: _Peers,
) -> None:
    """An operator stop, then the peer hangs up on RELEASE, and the reader's
    EOF meets a chart without row .03. The run's error is judged after the
    lease went back, so the clean stop's exit 0 becomes 7, with the report
    once, and the lock is released."""
    from dsl41 import cli_run

    _gapped(monkeypatch, tmp_path, **_without("03"))
    peers.hang_up_on = "RELEASE"
    run_root = short_root / "root"
    real_run = Engine.run_until_quiescent

    async def run(self: Engine, horizon: datetime) -> list[Event]:
        asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
        return await real_run(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", run)
    code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL),
            run_root,
            False,
            [],
            profile=runtime_profile_from_cli(detached=True),
            supervisor="spawn",
        )
    )
    assert code == 7
    report = _report(capsys.readouterr().err)
    gap = "no row and no deferral for eof in ['alive', 'client', 'connected']"
    assert gap in report[0] and sum(gap in line for line in report) == 1, report[:3]
    assert peers[0].seen[-1] == "RELEASE"
    acquire_run_root(run_root).release()


def test_a_statechart_error_at_the_teardown_s_close_exits_7(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    peers: _Peers,
) -> None:
    """An operator stop. The teardown gives the lease back and closes the
    client, whose close meets a chart without row .05: the client still
    tears down, the journal and the lock are released, and the verb exits 7
    with the report."""
    from dsl41 import cli_run

    _gapped(monkeypatch, tmp_path, **_without("05"))
    run_root = short_root / "root"
    real_run = Engine.run_until_quiescent
    clients: list[SupervisorClient] = []

    async def run(self: Engine, horizon: datetime) -> list[Event]:
        assert isinstance(self.supervisor, SupervisorClient)
        clients.append(self.supervisor)
        asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
        return await real_run(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", run)

    def verb() -> int:
        return asyncio.run(
            cli_run._serve_run(
                lower_source(_JIL),
                run_root,
                False,
                [],
                profile=runtime_profile_from_cli(detached=True),
                supervisor="spawn",
            )
        )

    with pytest.raises(typer.Exit) as stopped:
        stops_on_statechart_error(verb)()
    assert stopped.value.exit_code == 7
    report = _report(capsys.readouterr().err)
    gap = "no row and no deferral for close in ['alive', 'client', 'connected']"
    assert gap in report[0] and sum(gap in line for line in report) == 1, report[:3]
    (client,) = clients
    assert client._writer is None and client._reader_task is None
    assert client._renew_task is None and "RELEASE" in peers[0].seen
    acquire_run_root(run_root).release()


def test_a_lease_return_that_raises_still_judges_the_run_and_names_the_next_step(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    peers: _Peers,
) -> None:
    """An operator stop; a statechart error is recorded while the server
    closes; then the client's close meets a chart without row .05 and
    raises. The run still judges the recorded error (its report, exit 7)
    and prints the reattach note before the close's error leaves for the
    CLI wrapper, which reports it too."""
    from dsl41 import cli_run
    from dsl41.runner_control import ControlServer

    _gapped(monkeypatch, tmp_path, **_without("05"))
    run_root = short_root / "root"
    real_run, real_close = Engine.run_until_quiescent, ControlServer.close

    async def run(self: Engine, horizon: datetime) -> list[Event]:
        asyncio.get_running_loop().call_later(0.05, signal.raise_signal, signal.SIGINT)
        return await real_run(self, horizon)

    async def close(self: ControlServer) -> None:
        await real_close(self)
        self.stop_on(UnhandledEvent("met while the server closed"))

    monkeypatch.setattr(Engine, "run_until_quiescent", run)
    monkeypatch.setattr(ControlServer, "close", close)

    def verb() -> int:
        return asyncio.run(
            cli_run._serve_run(
                lower_source(_JIL),
                run_root,
                False,
                [],
                profile=runtime_profile_from_cli(detached=True),
                supervisor="spawn",
            )
        )

    with pytest.raises(typer.Exit) as stopped:
        stops_on_statechart_error(verb)()
    assert stopped.value.exit_code == 7
    out, err = capsys.readouterr()
    report = _report(err)
    gap = "no row and no deferral for close in ['alive', 'client', 'connected']"
    assert report[0] == f"{STATECHART_LOG_PREFIX}: met while the server closed", report[:3]
    assert sum(gap in line for line in report) == 1
    assert any(line.startswith("detached: reattach") for line in out.splitlines()), out
    acquire_run_root(run_root).release()


@pytest.mark.parametrize("where", ["resume", "proof"])
def test_the_offline_sealer_exits_7_when_the_client_meets_a_gap(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peers: _Peers,
    where: str,
) -> None:
    """The offline `dsl41 seal` wires a client for a detached period. The
    peer hangs up on the resume's LIST, or answers it and hangs up on the
    seal's proof LIST, and the reader's EOF meets a chart without row .03.
    Outside a run the client keeps the error, and the request whose reply
    the EOF failed raises it in place of the transport failure: the resume
    stops, or the proof never takes its transport fallback. The verb exits 7
    with the report once; the lease goes back and the client closes; no seal
    record is written, the head stays open and the lock is released."""
    from test_boundary import C1_JIL, _catalog
    from test_estate import _estate, _head, _seal_offline

    from dsl41.boundary import OpenHead
    from dsl41.period import RuntimeProfile, seal_path, stage_manifest, write_bundle
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import RealClock
    from dsl41.runner_ledger import STATE_MACHINE_VERSION
    from dsl41.runner_startup import start_run

    _c1, _, _ = _estate(tmp_path / "estate")
    nxt = tmp_path / "estate" / "c2.jil"
    run_root = short_root / "root"
    catalog, sources = _catalog(C1_JIL)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(execution_mode="detached"),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    engine = start_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
    )
    assert engine.journal is not None
    engine.journal.close()
    _gapped(monkeypatch, tmp_path, **_without("03"))
    peers.hang_up_on, peers.answer_first = "LIST", {"resume": 0, "proof": 1}[where]
    # the fixture's peer stands in for a spawned supervisor (DL-396)
    result = _seal_offline(run_root, nxt, "--supervisor", "spawn")
    assert result.exit_code == 7, result.output
    lines = result.output.splitlines()
    assert f"{STATECHART_LOG_PREFIX}: machine: supervisor_client" in lines
    gap = "no row and no deferral for eof in ['alive', 'client', 'connected']"
    assert sum(gap in line for line in lines) == 1  # reported once, not again at close
    (peer,) = peers
    assert peer.seen.count("LIST") == {"resume": 1, "proof": 2}[where]
    assert peer.seen[-1] == "RELEASE"
    (client,) = peers.clients
    assert client._closed and client._writer is None
    assert not seal_path(run_root, 1).exists()
    assert isinstance(_head(run_root), OpenHead)
    acquire_run_root(run_root).release()


def test_the_adapter_s_wait_does_not_spin_after_a_discarded_post(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peers: _Peers,
) -> None:
    """A run's client whose EOF met a gap: the leaf stays `connected` while
    the epoch is lost. The adapter's wait asks the transport, not the leaf,
    so its reconnect really connects, and the next pass blocks on the new
    epoch for one poll. The bound is one pass per poll interval or per
    reconnect; a wait that trusted the stale leaf would pass again at once,
    without end."""
    from dsl41.runner_adapters import SupervisedCommandAdapter

    _gapped(monkeypatch, tmp_path, **_without("03"))
    monkeypatch.setattr(runner_adapters, "_OUTCOME_POLL_S", 30.0)
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()  # no status.json: only an exit push or a reconnect moves the wait

    async def scenario() -> tuple[int, str, bool]:
        client = SupervisorClient(short_root)
        stops: list[StatechartError] = []
        client.on_statechart_error = stops.append  # as in a run
        passes = 0
        watch = client.watch_exit

        def counted(run_id: str) -> asyncio.Event:
            nonlocal passes
            passes += 1
            return watch(run_id)

        client.watch_exit = counted  # type: ignore[method-assign]
        adapter = SupervisedCommandAdapter(client, grace_seconds=0.0, settle_seconds=0.0)
        await client.provision()
        task = asyncio.ensure_future(adapter._await_outcome("run", run_dir, "j", 1))
        try:
            while passes < 1:
                await asyncio.sleep(0.01)
            peers[0].drop_all()

            async def reconnected() -> None:
                while peers[0].seen.count("PING") < 2:  # the wait's reconnect
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(reconnected(), 5)
            await asyncio.sleep(0.2)
            return passes, client.phase(), client.lost.is_set() or len(stops) != 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.close()

    passes, leaf, broken = asyncio.run(scenario())
    assert passes == 2 and not broken
    assert leaf == "connected"  # the discarded leaf: the transport moved on without it


# ---- the surface, unit by unit


def test_outside_a_run_the_first_background_error_is_raised_once(tmp_path: Path) -> None:
    """By the next request, or by `close` when no request came first, after
    the teardown. A second error is not kept over the first."""

    async def scenario() -> list[str]:
        seen = []
        client = SupervisorClient(tmp_path)
        client._fail(UnhandledEvent("first"))
        client._fail(UnhandledEvent("second"))
        with pytest.raises(UnhandledEvent) as raised:
            await client.list_runs()
        seen.append(raised.value.message)
        await client.close()  # already raised: the close is quiet
        idle = SupervisorClient(tmp_path)
        idle._fail(UnhandledEvent("never asked"))
        with pytest.raises(UnhandledEvent) as raised:
            await idle.close()
        seen.append(raised.value.message)
        assert idle._closed and idle.phase() == "closed"  # torn down first
        return seen

    assert asyncio.run(scenario()) == ["first", "never asked"]


def test_a_run_s_stop_takes_an_error_met_before_it_and_every_later_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[str]:
        client = SupervisorClient(tmp_path)
        stopped: list[str] = []

        def stop(error: StatechartError) -> None:
            stopped.append(error.args[0])

        assert client.on_statechart_error is None  # outside a run
        client._fail(UnhandledEvent("before the loop"))
        client.on_statechart_error = stop
        assert client.on_statechart_error is stop
        client._fail(UnhandledEvent("in the loop"))
        await client.close()  # handed over: not raised again
        quiet = SupervisorClient(tmp_path)
        quiet.on_statechart_error = lambda error: stopped.append("never")
        await quiet.close()
        return stopped

    assert asyncio.run(scenario()) == ["before the loop", "in the loop"]


def test_the_list_net_hands_a_statechart_error_to_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_adapters, "_LIST_RECHECK_EVERY", 0)
    error = UnhandledEvent("the LIST net met a gap")

    async def scenario() -> StatechartError | None:
        client = SupervisorClient(tmp_path)

        async def list_runs() -> None:
            raise error

        monkeypatch.setattr(client, "list_runs", list_runs)
        client.watch_exit("r1")
        client._arm_list_recheck()
        task = client._list_task
        assert task is not None
        await asyncio.wait_for(task, 3)
        fatal = client._fatal
        client._fatal = None
        await client.close()
        return fatal

    assert asyncio.run(scenario()) is error


def _renewal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list[Any]
) -> tuple[list[str], StatechartError | None, bool]:
    """Run the renewal loop over a scripted `_request`. Answers the commands
    it sent, the error handed to `_fail`, and whether the loop ended."""
    monkeypatch.setattr(SupervisorClient, "_RENEW_EVERY_S", 0.0)
    monkeypatch.setattr(SupervisorClient, "_RETRY_EVERY_S", 0.0)

    async def scenario() -> tuple[list[str], StatechartError | None, bool]:
        client = SupervisorClient(tmp_path)
        requests = _ScriptedRequests(script)
        monkeypatch.setattr(client, "_request", requests)
        loop = asyncio.ensure_future(client._renew_loop(60.0))
        drained = asyncio.ensure_future(requests.drained.wait())
        await asyncio.wait({loop, drained}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        ended = loop.done()
        for task in (loop, drained):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        fatal, client._fatal = client._fatal, None
        await client.close()
        return requests.commands, fatal, ended

    return asyncio.run(scenario())


def test_a_statechart_error_in_a_renewal_ends_the_loop_and_goes_to_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = UnhandledEvent("a renewal met a gap")
    commands, fatal, ended = _renewal(monkeypatch, tmp_path, [error, {"ok": True}])
    assert (commands, fatal, ended) == (["RENEW"], error, True)


def test_the_mutant_renewal_counts_a_statechart_error_as_a_failed_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mutant: a renewal loop that does not know a `StatechartError`
    takes it for one bad renewal, logs it and keeps renewing. The test above
    fails against it."""

    class NotAStatechartError(Exception):
        pass

    monkeypatch.setattr(runner_adapters, "StatechartError", NotAStatechartError)
    error = UnhandledEvent("a renewal met a gap")
    commands, fatal, ended = _renewal(monkeypatch, tmp_path, [error, {"ok": True}])
    # the bad renewal, the good one, and the next, which the script parks
    assert (commands, fatal, ended) == (["RENEW"] * 3, None, False)
    assert "renewal failed on UnhandledEvent('a renewal met a gap')" in capsys.readouterr().err


def test_a_statechart_error_in_the_unreachable_report_goes_to_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's callback at the fifth failed renewal raises one: the loop
    ends and hands it on, where any other error there is only logged."""
    error = UnhandledEvent("the unreachable report met a gap")
    real_init = SupervisorClient.__init__

    def init(self: SupervisorClient, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)

        def report() -> None:
            raise error

        self.on_unreachable = report

    monkeypatch.setattr(SupervisorClient, "__init__", init)
    down = SupervisorUnavailable("synthetic outage")
    commands, fatal, ended = _renewal(monkeypatch, tmp_path, [down] * 5)
    assert (commands, fatal, ended) == (["RENEW"] * 5, error, True)
