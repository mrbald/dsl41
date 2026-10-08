"""Every event the process tier's machines accept, in every state a builder reaches.

DL-293 found a missing row in the job machine with an exhaustive event x state
test (tests/test_job_machines.py). These tests do the same for the supervisor's
`supervisor_process` and `supervisor_lease`, the engine's `supervisor_client`,
and the anchor's `anchor_head` and `period_row`. Each test builds states
through the owner's own code, sends every event that code accepts under the
strict variable, which the test sets itself, and passes only when each event
takes a declared transition or is refused by the code's own rule.

None of these machines writes a trace line. A violation shows as a
`TransitionError`, as an entry in the violations file, or as a stderr line
that starts with `VIOLATION_LOG_PREFIX`; each test checks all three. Each
test records into its own directory and asserts which declared (transition,
source) pairs its builder took; each pair it does not take is named, with the
reason. The fixture then hands the hits, never the violations, to the
session's directory, so the coverage gate counts them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import selectors
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from dsl41 import runner_supervisor
from dsl41.boundary import (
    ANCHOR_HEAD,
    CLAIMS_DIR,
    PERIOD_ROW,
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    row_tag,
)
from dsl41.runner_adapters import SUPERVISOR_CLIENT, SupervisorClient, SupervisorUnavailable
from dsl41.runner_clock import EngineError
from dsl41.state_machine import HITS_ENV, STRICT_ENV, VIOLATION_LOG_PREFIX, TransitionError


@pytest.fixture
def strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The strict variable, and a private directory for hits and violations.
    Afterwards the hits go on to the session's directory, if it has one."""
    session = os.environ.get(HITS_ENV)
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setenv(STRICT_ENV, "1")
    yield directory
    if session:
        for path in directory.glob("hits-*.jsonl"):
            shutil.copyfile(path, Path(session) / f"hits-{tmp_path.name}-{path.name}")


def _records(directory: Path, prefix: str) -> list[dict[str, str]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob(f"{prefix}-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _missed_sources(directory: Path, machine: Any) -> set[tuple[str, str]]:
    """The declared (transition, source) pairs the test did not take."""
    taken = {
        (r["id"], r["old"]) for r in _records(directory, "hits") if r["machine"] == machine.name
    }
    return {(t.id, s) for t in machine.transitions for s in t.source} - taken


def _assert_no_violation(directory: Path, stderr: str) -> None:
    assert _records(directory, "violations") == []
    assert VIOLATION_LOG_PREFIX not in stderr


def _spy_on_take(monkeypatch: pytest.MonkeyPatch, machine: Any) -> list[str]:
    """Record the id of every transition taken by any machine of `machine`'s
    class, so a test can name what one event took. The supervisor's machines
    are instances of another module's class (DL-42), so the class is read off
    the machine."""
    taken: list[str] = []
    cls = type(machine)
    take = cls.take

    def spy(self: Any, t: Any, old: Any, new: Any) -> Any:
        taken.append(t.id)
        return take(self, t, old, new)

    monkeypatch.setattr(cls, "take", spy)
    return taken


# ------------------------------------------------------------ supervisor_lease


class _Clock:
    """The supervisor's `time` module with a monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


#: the lease's events. A request is (verb, connection, credentials, ttl_s).
type _LeaseEvent = tuple[str, str, str, float | None]

_SLOTS = ("a", "b")
_CREDENTIALS = ("current", "none", "stale", "foreign")
#: the lease verbs' own refusals (supervisor-protocol ss5). Each changes nothing.
_LEASE_REFUSALS = {"bad_controller_id", "lease_held", "wrong_incarnation", "stale_token"}


def _lease_events() -> list[_LeaseEvent]:
    events: list[_LeaseEvent] = [
        ("ACQUIRE", "a", "bad_controller_id", 60.0),
        ("ELAPSE", "", "", None),
    ]
    for slot in _SLOTS:
        for creds in _CREDENTIALS:
            events += [("ACQUIRE", slot, creds, ttl) for ttl in (60.0, 0.0)]
            events += [("RENEW", slot, creds, ttl) for ttl in (60.0, -1.0)]
            events.append(("RELEASE", slot, creds, None))
        events.append(("EOF", slot, "", None))
    return events


class _LeaseRig:
    """A supervisor with two connections, registered as `_accept` registers
    them. A request goes in through the peer socket and `_readable`, the
    loop's own entry; its reply is taken off the connection's queue. EOF is
    the peer closing, read the same way, and a fresh connection takes the
    slot."""

    def __init__(self, root: Path, clock: _Clock) -> None:
        self.clock = clock
        self.sup = runner_supervisor.Supervisor(str(root))
        self.conns: dict[str, runner_supervisor._Conn] = {}
        self.peers: dict[str, socket.socket] = {}
        for slot in _SLOTS:
            self._open(slot)

    def _open(self, slot: str) -> None:
        server, peer = socket.socketpair()
        server.setblocking(False)
        peer.settimeout(1)
        conn = runner_supervisor._Conn(server)
        self.sup._conns[server.fileno()] = conn
        self.sup._sel.register(server, selectors.EVENT_READ, ("conn", conn))
        self.conns[slot], self.peers[slot] = conn, peer

    def key(self) -> tuple[str, str | None]:
        """The lease's phase and which slot's connection holds it."""
        lease = self.sup.lease
        phase = runner_supervisor.lease_phase(lease, self.clock.now)
        holder = None
        for slot, conn in self.conns.items():
            if lease is not None and lease.conn is conn:
                holder = slot
        return phase, holder

    def snapshot(self) -> tuple[Any, ...] | None:
        lease = self.sup.lease
        if lease is None:
            return None
        return (lease.holder, lease.token, lease.deadline, lease.expires_at, lease.conn)

    def apply(self, event: _LeaseEvent) -> dict[str, Any] | None:
        verb, slot, creds, ttl = event
        if verb == "ELAPSE":
            self.clock.now += 61.0
            return None
        if verb == "EOF":
            self.peers[slot].close()
            self.sup._readable(self.conns[slot])
            self._open(slot)
            return None
        req: dict[str, Any] = {"v": 1, "cmd": verb}
        if ttl is not None:
            req["ttl_s"] = ttl
        if verb == "ACQUIRE":
            req["controller_id"] = "" if creds == "bad_controller_id" else f"controller-{slot}"
        current = self.sup._next_token - 1
        if creds == "current":
            req.update(incarnation=self.sup.incarnation, token=current)
        elif creds == "stale":
            req.update(incarnation=self.sup.incarnation, token=current + 100)
        elif creds == "foreign":
            req.update(incarnation="0" * 32, token=current)
        conn = self.conns[slot]
        self.peers[slot].sendall(json.dumps(req).encode("utf-8") + b"\n")
        self.sup._readable(conn)
        frame, _ = conn.out.popleft()
        reply: dict[str, Any] = json.loads(frame)
        return reply

    def close(self) -> None:
        for conn in list(self.sup._conns.values()):
            self.sup._drop_conn(conn)
        for peer in self.peers.values():
            peer.close()
        for fd in (self.sup._chld_r, self.sup._chld_w):
            runner_supervisor.os.close(fd)
        self.sup._sel.close()


def test_every_lease_verb_in_every_reached_lease_state_takes_a_declared_transition(
    short_root: Path,
    strict: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """supervisor_lease. From a fresh supervisor, a breadth-first walk applies
    every event to every state it reaches, keyed on (phase, which connection
    holds the lease). The events: ACQUIRE, RENEW and RELEASE from either of
    two connections, with the current credentials, none, a stale token or a
    foreign incarnation, ACQUIRE and RENEW with a positive ttl_s and one that
    is not; an ACQUIRE with an empty controller_id; EOF on either connection; and
    time passing beyond any deadline. The walk reaches all seven
    (phase, holder) states: free; live, held by either connection; orphaned;
    expired, with either holder's connection open or with none. A request is
    answered ok after a declared transition, or refused with one of the
    lease's own codes, the record unchanged and no transition taken; an
    `internal:` answer would be the dispatch belt catching a TransitionError.
    Each event must take exactly the transition its verb names: an ok ACQUIRE
    01 or 02, RENEW 03 or 04, RELEASE 05; EOF on the holder's connection 06
    or 07; anything else none.

    Left out: a ttl_s that is not finite, overflows a timestamp, or is not a
    number. The first two answer `internal:` from `_grant` or `_extend`, and
    RENEW moves the deadline before it fails (reported, not changed here); a
    non-numeric one answers `internal:` from `float()` with the record
    unchanged. The clock is the test's, so expiry is a step, not a wait."""
    clock = _Clock()
    monkeypatch.setattr(runner_supervisor, "time", clock)
    monkeypatch.setattr(runner_supervisor, "current_boot_id", lambda: "boot")
    taken = _spy_on_take(monkeypatch, runner_supervisor.SUPERVISOR_LEASE)
    takes = {"ACQUIRE": {"01", "02"}, "RENEW": {"03", "04"}, "RELEASE": {"05"}, "EOF": {"06", "07"}}
    events = _lease_events()
    problems: list[str] = []

    def build(path: tuple[_LeaseEvent, ...]) -> _LeaseRig:
        clock.now = 1000.0
        rig = _LeaseRig(short_root, clock)
        for step in path:
            rig.apply(step)
        return rig

    start = build(())
    reached: dict[tuple[str, str | None], tuple[_LeaseEvent, ...]] = {start.key(): ()}
    start.close()
    frontier: list[tuple[_LeaseEvent, ...]] = [()]
    applied = 0
    while frontier:
        path = frontier.pop(0)
        for event in events:
            rig = build(path)
            state, before = rig.key(), rig.snapshot()
            applied += 1
            taken.clear()
            try:
                reply = rig.apply(event)
            except Exception as exc:  # the supervisor's TransitionError is another module's
                problems.append(f"{event} in {state}: {type(exc).__name__}: {exc}")
                continue
            else:
                if reply is not None and reply.get("ok") is not True:
                    error = str(reply.get("error"))
                    if error not in _LEASE_REFUSALS:
                        problems.append(f"{event} in {state}: {error}")
                    elif rig.snapshot() != before:
                        problems.append(f"{event} in {state}: refused, and the record moved")
                moves = reply is not None and reply.get("ok") is True
                moves = moves or (event[0] == "EOF" and state[1] == event[1])
                expected = {f"supervisor_lease.{n}" for n in takes.get(event[0], set())}
                lease_takes = [i for i in taken if i.startswith("supervisor_lease.")]
                if not (
                    len(lease_takes) == 1 and lease_takes[0] in expected
                    if moves
                    else not lease_takes
                ):
                    problems.append(f"{event} in {state}: took {lease_takes}")
                after = rig.key()
            finally:
                rig.close()
            if after not in reached:
                reached[after] = path + (event,)
                frontier.append(path + (event,))
    assert problems == [], "\n".join(problems)
    assert set(reached) == {
        ("free", None),
        ("live", "a"),
        ("live", "b"),
        ("orphaned", None),
        ("expired", "a"),
        ("expired", "b"),
        ("expired", None),
    }
    assert applied == len(reached) * len(events)
    assert _missed_sources(strict, runner_supervisor.SUPERVISOR_LEASE) == set()
    _assert_no_violation(strict, capfd.readouterr().err)


# ---------------------------------------------------------- supervisor_process


class _Injected(Exception):
    """An error the test raises at a chosen point of the supervisor's run."""


#: where an event is delivered: right after the move into a state, or at the
#: first reap while in it (the loop's tick, and each pass of the shutdown wait)
_POINTS = (
    ("starting", "enter"),
    ("bound", "enter"),
    ("serving", "enter"),
    ("serving", "reap"),
    ("shutting_down", "enter"),
    ("shutting_down", "reap"),
    ("stopped", "enter"),
    ("stopped", "reap"),
    ("refused", "enter"),
    ("closed", "enter"),
)
_STIMULI = (
    "none",
    "signal",
    "sigchld",
    "shutdown",
    "stale_shutdown",
    "deadman",
    "error",
    "error_after_signal",
)
#: what ends the serving loop, delivered on entering `serving`
_STOPPERS = ("signal", "shutdown", "pipelined_shutdowns", "deadman")


class _ProcessRig:
    """One in-process `Supervisor.run()` with events delivered at one point.

    The signal handlers and the subreaper are left out, as in the suite's
    other in-process runs: the test calls the handlers itself, and the
    process-wide state stays the runner's. `_reap` is stubbed, because an
    in-process reap would wait on the test runner's children; the stub is the
    delivery point for the `reap` events."""

    def __init__(self, root: Path, point: tuple[str, str], stimulus: str, stopper: str) -> None:
        self.sup = runner_supervisor.Supervisor(str(root))
        self.point, self.stimulus, self.stopper = point, stimulus, stopper
        self.fired = False
        #: the stimulus reached the supervisor: a request needs a listening socket
        self.delivered = False
        self.client: socket.socket | None = None
        move = self.sup._move

        def moved(t: Any, new: Any) -> None:
            move(t, new)
            self._at(new, "enter")
            if new == "serving":
                self._deliver(self.stopper)

        self.sup._move = moved  # type: ignore[method-assign]
        self.sup._reap = lambda: self._at(self.sup.state, "reap")  # type: ignore[method-assign]
        self.sup._install_signals = lambda: None  # type: ignore[method-assign]
        self.sup._set_subreaper = lambda: None  # type: ignore[method-assign]

    def _at(self, state: str, point: str) -> None:
        if (state, point) == self.point and not self.fired:
            self.fired = True
            self.delivered = self.stimulus.startswith("error")  # delivered as it raises
            self.delivered = self._deliver(self.stimulus)

    def _connect(self) -> socket.socket | None:
        """The controller's connection, once a listening socket exists."""
        if self.client is None:
            for path in (self.sup.sock_path, self.sup._private_path):
                candidate = socket.socket(socket.AF_UNIX)
                try:
                    candidate.connect(path)
                except OSError:
                    candidate.close()
                    continue
                self.client = candidate
                break
        return self.client

    def _request(self, *lines: dict[str, Any]) -> bool:
        client = self._connect()
        if client is None:
            return False
        client.sendall(b"".join(json.dumps({"v": 1, **r}).encode() + b"\n" for r in lines))
        return True

    def _deliver(self, event: str) -> bool:
        """Deliver one event. Returns whether it reached the supervisor."""
        sup = self.sup
        acquire = {"cmd": "ACQUIRE", "controller_id": "controller"}
        shutdown = {"cmd": "SHUTDOWN", "incarnation": sup.incarnation, "token": 1}
        match event:
            case "none":
                pass
            case "signal":
                sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
            case "sigchld":
                sup._on_chld_signal(runner_supervisor.signal.SIGCHLD, None)
            case "shutdown":
                return self._request(acquire, shutdown)
            case "pipelined_shutdowns":
                return self._request(acquire, shutdown, shutdown)
            case "stale_shutdown":
                return self._request({**shutdown, "token": 99})
            case "deadman":
                sup.deadman_s = 1e-9
                sup._on_chld_signal(runner_supervisor.signal.SIGCHLD, None)  # wakes the loop
            case "error":
                raise _Injected(f"{self.point}")
            case _:  # error_after_signal
                sup._on_term_signal(runner_supervisor.signal.SIGTERM, None)
                raise _Injected(f"{self.point}")
        return True

    def run(self) -> str:
        """Run the supervisor, tear it down once more, and name how the run ended."""
        if self.point == ("starting", "enter"):
            try:
                self._at("starting", "enter")
            except _Injected:
                self.sup._teardown()
                return "injected"
        ended = "returned"
        try:
            self.sup.run()
        except _Injected:
            ended = "injected"
        except SystemExit:
            ended = "refused"
        self.sup._teardown()  # a second teardown: `closed` is final and takes nothing
        return ended

    def finish(self) -> list[dict[str, Any]]:
        """Release what the run left open, then read the client's replies."""
        sup = self.sup
        if sup._lock_fd is not None:  # an error at `closed` skipped the teardown's cleanup
            for conn in sup._conns.values():
                conn.sock.close()
            if sup._listen is not None:
                sup._listen.close()
            for fd in (sup._chld_r, sup._chld_w, sup._lock_fd):
                runner_supervisor.os.close(fd)
            sup._sel.close()
        if self.client is None:
            return []
        data = b""
        with self.client:
            self.client.settimeout(5.0)  # every server side is closed: EOF comes at once
            with contextlib.suppress(OSError):
                while chunk := self.client.recv(65536):
                    data += chunk
        return [json.loads(line) for line in data.splitlines()]


def _refusing_root(root: Path, kind: str) -> int | None:
    """Make `root` one a starting supervisor refuses: its lock held, or a pid
    record that names this live process. Returns the lock descriptor to close."""
    if kind == "lock_held":
        return runner_supervisor.flock_exclusive(root / "supervisor.lock")
    runner_supervisor.durable_write_json(
        str(root / "supervisor.pid"),
        {
            "pid": runner_supervisor.os.getpid(),
            "start_time": runner_supervisor.proc_start_token(runner_supervisor.os.getpid()),
            "boot_id": "boot",
            "incarnation": "other",
        },
    )
    return None


def test_every_process_event_at_every_reached_point_takes_a_declared_transition(
    short_root: Path,
    strict: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """supervisor_process. Each case is one in-process `Supervisor.run()`
    that delivers one event at one point of the run. The points: on entering
    each of the seven states (`starting` is before `run`), and at the first
    reap in `serving` (the loop's tick), `shutting_down` (the shutdown's
    wait) and `stopped` (the end of the loop pass that stopped it). The
    events: none; SIGTERM, through the handler the process installs; SIGCHLD;
    a SHUTDOWN from a client that took the lease; a SHUTDOWN with a stale
    token; the deadman falling due; an error; and an error with a SIGTERM
    latched before it. On entering `serving`, one of four stoppers ends the
    loop: SIGTERM, a SHUTDOWN, two SHUTDOWNs pipelined in one write, or the
    deadman. `refused` is reached from a held lock and from a pid record
    that names a live process. A case passes when the run ends `closed`,
    having returned, refused (SystemExit) or raised the injected error, with
    no TransitionError and no `internal: TransitionError` answer.

    Left out: SIGKILL, which runs no code; a request from a second
    connection in the same loop pass as a stop (the pipelined SHUTDOWN is
    the request that can follow one); and a SHUTDOWN before the socket
    listens or after a refused start, which no client can deliver (the test
    asserts exactly which). A SIGTERM at those points is delivered, and the
    latch holds it for `serving`."""
    monkeypatch.setattr(runner_supervisor, "current_boot_id", lambda: "boot")
    problems: list[str] = []
    delivered: set[tuple[tuple[str, str], str]] = set()
    cases = [
        (point, stimulus, stopper, refusal)
        for point in _POINTS
        for stimulus in _STIMULI
        for stopper, refusal in (
            [(None, "lock_held"), (None, "owner_live")]
            if point[0] == "refused"
            else [(s, None) for s in _STOPPERS]
        )
    ]
    for n, (point, stimulus, stopper, refusal) in enumerate(cases):
        root = short_root / str(n)
        root.mkdir()
        lock = _refusing_root(root, refusal) if refusal is not None else None
        rig = _ProcessRig(root, point, stimulus, stopper or "none")
        case = f"{stimulus} at {point} with {stopper or refusal}"
        try:
            ended = rig.run()
        except Exception as exc:  # the supervisor's TransitionError is another module's
            problems.append(f"{case}: {type(exc).__name__}: {exc}")
            ended = "raised"
        finally:
            answers = rig.finish()
            if lock is not None:
                runner_supervisor.os.close(lock)
        if rig.delivered:
            delivered.add((point, stimulus))
        if ended != "raised" and rig.sup.state != "closed":
            problems.append(f"{case}: ended {ended} in {rig.sup.state}")
        problems += [
            f"{case}: answered {a['error']}"
            for a in answers
            if "TransitionError" in str(a.get("error", ""))
        ]
    assert problems == [], "\n".join(problems)
    # a request needs a listening socket: none before `_bind` listens, none after a refusal
    unsent = {
        (point, request)
        for point in (("starting", "enter"), ("bound", "enter"), ("refused", "enter"))
        for request in ("shutdown", "stale_shutdown")
    }
    assert delivered == {(p, s) for p in _POINTS for s in _STIMULI} - unsent
    assert _missed_sources(strict, runner_supervisor.SUPERVISOR_PROCESS) == set()
    _assert_no_violation(strict, capfd.readouterr().err)


# ----------------------------------------------------------- supervisor_client


class _FakeSupervisor:
    """A unix-socket peer that answers every request ok, except a SIGNAL for
    run `hold`, which it never answers. The client's machine reads only its
    transport, so the peer's lease rules do not matter here."""

    def __init__(self) -> None:
        self.writers: list[asyncio.StreamWriter] = []
        self.held = asyncio.Event()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        with contextlib.suppress(OSError):
            while line := await reader.readline():
                req = json.loads(line)
                if req.get("cmd") == "SIGNAL" and req.get("run_id") == "hold":
                    self.held.set()
                    continue
                reply = {"ok": True, "token": 1, "incarnation": "i", "deadman_s": None}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        writer.close()

    def drop_all(self) -> None:
        for writer in self.writers:
            writer.close()
        self.writers.clear()


_CLIENT_EVENTS = ("ensure_running", "reconnect", "request", "acquire", "eof", "cancel", "close")


async def _client_event(client: SupervisorClient, peer: _FakeSupervisor, event: str) -> None:
    """Send one event the client's code accepts. SupervisorUnavailable is the
    client's own refusal and passes; anything else propagates."""
    with contextlib.suppress(SupervisorUnavailable):
        match event:
            case "ensure_running":
                await client.ensure_running()
            case "reconnect":
                await client.reconnect()
            case "request":
                await client.signal("run", "TERM")
            case "acquire":
                await client.acquire()
            case "eof":
                was_connected, lost = client.phase() == "connected", client.lost
                peer.drop_all()
                if was_connected:
                    await asyncio.wait_for(lost.wait(), timeout=5.0)
            case "cancel":
                peer.held.clear()
                request = asyncio.ensure_future(client.signal("hold", "TERM"))
                held = asyncio.ensure_future(peer.held.wait())
                await asyncio.wait({request, held}, timeout=5.0, return_when="FIRST_COMPLETED")
                held.cancel()
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request
            case _:  # close
                await client.close()


def _client_key(client: SupervisorClient) -> tuple[str, bool, bool]:
    """The phase, whether a writer is held, and whether a lease token is held."""
    return client.phase(), client._writer is not None, client.token is not None


def test_every_client_event_in_every_reached_client_state_takes_a_declared_transition(
    short_root: Path, strict: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """supervisor_client. Against a peer on a real unix socket, a
    breadth-first walk applies every event to every state it reaches, keyed on
    (phase, writer held, token held). The events: `ensure_running`,
    `reconnect`, a request, `acquire`, the peer closing the connection (EOF),
    a request cancelled while its reply is outstanding, and `close`. The
    walk reaches disconnected; connected and lost (by EOF, writer kept; by a
    cancelled request, writer dropped), each with and without a token; and
    closed, with and without a token (`close` drops the writer). An event
    passes when it completes or ends in SupervisorUnavailable, with no
    TransitionError. The reader task would swallow one; the violations file
    still records it.

    Left out: spawning a supervisor. `ensure_running` spawns one when its
    connect is refused, as it always is on a closed client, so the test
    replaces `_spawn_supervisor` with a SupervisorUnavailable. A refused
    connect takes no transition."""
    sock = short_root / "supervisor.sock"
    problems: list[str] = []

    def no_spawn() -> None:
        raise SupervisorUnavailable("the test spawns no supervisor")

    async def build(path: tuple[str, ...]) -> SupervisorClient:
        client = SupervisorClient(short_root)
        client._spawn_supervisor = no_spawn  # type: ignore[assignment,method-assign]
        for step in path:
            await _client_event(client, peer, step)
        return client

    async def walk() -> tuple[dict[tuple[str, bool, bool], tuple[str, ...]], int]:
        start = await build(())
        reached: dict[tuple[str, bool, bool], tuple[str, ...]] = {_client_key(start): ()}
        await start.close()
        frontier: list[tuple[str, ...]] = [()]
        applied = 0
        while frontier:
            path = frontier.pop(0)
            for event in _CLIENT_EVENTS:
                client = await build(path)
                state = _client_key(client)
                applied += 1
                try:
                    await _client_event(client, peer, event)
                except Exception as exc:  # noqa: BLE001 -- every failure is reported
                    problems.append(f"{event} in {state}: {type(exc).__name__}: {exc}")
                    await client.close()
                    continue
                after = _client_key(client)
                await client.close()
                if after not in reached:
                    reached[after] = path + (event,)
                    frontier.append(path + (event,))
        return reached, applied

    async def scenario() -> tuple[dict[tuple[str, bool, bool], tuple[str, ...]], int]:
        server = await asyncio.start_unix_server(peer.serve, path=str(sock))
        try:
            return await walk()
        finally:
            peer.drop_all()
            server.close()
            await server.wait_closed()

    peer = _FakeSupervisor()
    reached, applied = asyncio.run(scenario())
    assert problems == [], "\n".join(problems)
    assert set(reached) == {
        ("disconnected", False, False),
        ("connected", True, False),
        ("connected", True, True),
        ("lost", True, False),
        ("lost", True, True),
        ("lost", False, False),
        ("lost", False, True),
        ("closed", False, False),
        ("closed", False, True),
    }
    assert applied == len(reached) * len(_CLIENT_EVENTS)
    # a request failed by EOF and cancelled before it resumes: a race this walk does
    # not build (test_runner_adapters builds it by hand)
    assert _missed_sources(strict, SUPERVISOR_CLIENT) == {("supervisor_client.04", "lost")}
    _assert_no_violation(strict, capfd.readouterr().err)


# ------------------------------------------------------ anchor_head, period_row


class _Crash(Exception):
    """The anchor write that fails after `claim_successor` wrote its claim file."""


_ESTATE = "estate-exhaustive"


def _seal(period: int, variant: str) -> str:
    """A seal digest. A seal names its own period, as a real one does through
    the record it hashes, so no digest closes two periods."""
    return f"sha256:{period}{variant * 63}"


#: an anchor event: (verb, period, root, seal variant); unused fields are empty
type _AnchorEvent = tuple[str, int, str, str]


def _anchor_events() -> list[_AnchorEvent]:
    events: list[_AnchorEvent] = [("reclaim", 0, "", ""), ("lose_claim", 0, "", "")]
    for root in ("r1", "r2"):
        events += [("create_open", 1, root, ""), ("create_open_stranger", 1, root, "")]
        events.append(("open_claimed", 0, root, ""))
        for variant in "ab":
            events += [("attest", p, root, variant) for p in (1, 2, 3)]
            events += [
                (verb, p, root, variant) for verb in ("claim", "claim_crash") for p in (1, 2)
            ]
    # a period closes at its own seal; another digest for it is the variant-b events above
    events += [("close_period", p, "", "a") for p in (1, 2, 3)]
    events += [("finalize", p, "", "") for p in (1, 2, 3)]
    return events


class _AnchorRig:
    """One anchor directory, its lock held once. A state is the bytes of
    `anchor.json` and the claim files, restored before each event; each was
    reached through `EstateAnchor`'s own verbs."""

    def __init__(self, base: Path) -> None:
        self.roots = {name: base / name for name in ("r1", "r2")}
        for root in self.roots.values():
            root.mkdir()
        self.anchor = EstateAnchor(base / "anchor")
        self.anchor.acquire()
        self.claims = self.anchor.dir / CLAIMS_DIR

    def snapshot(self) -> tuple[bytes | None, tuple[tuple[str, bytes], ...]]:
        head = self.anchor.path.read_bytes() if self.anchor.path.exists() else None
        files = sorted(self.claims.glob("*.json")) if self.claims.exists() else []
        return head, tuple((f.name, f.read_bytes()) for f in files)

    def restore(self, state: tuple[bytes | None, tuple[tuple[str, bytes], ...]]) -> None:
        if self.snapshot() == state:
            return  # a refused event wrote nothing
        head, claims = state
        self.anchor.path.unlink(missing_ok=True)
        if head is not None:
            self.anchor.path.write_bytes(head)
        if self.claims.exists():
            for f in self.claims.glob("*.json"):
                f.unlink()
        if claims:
            self.claims.mkdir(exist_ok=True)
        for name, data in claims:
            (self.claims / name).write_bytes(data)

    def key(self) -> tuple[object, ...]:
        """What the verbs read: the head, each row's tag and seal, whether the
        head's claim file exists, and whether a reclaim was recorded (the
        count is never read). Roots are left out of the key: the verbs compare
        them, and every root that compares unequal behaves alike."""
        current = self.anchor.read()
        if current is None:
            return ("absent",)
        head = current.head
        match head:
            case OpenHead():
                shape: tuple[object, ...] = ("open", head.period_id)
            case ClosedHead():
                shape = ("closed", head.period_id, head.seal_digest)
            case ClaimedHead():
                claim = self.anchor.claim_path(head.claim_id)
                shape = ("claimed", head.claim_id, claim.exists())
        rows = tuple(sorted((p, row_tag(r), r.seal_digest) for p, r in current.periods.items()))
        return (*shape, rows, bool(current.reclaimed))

    def apply(self, event: _AnchorEvent) -> None:
        """One event, with the arguments its production callers bind: the
        closer is the open head's own root; a claim names the seal it opens
        from and that seal's next period; `open_claimed` takes the head's
        claim at the claim's own next period."""
        verb, period, root_name, variant = event
        anchor = self.anchor
        root = self.roots.get(root_name, self.roots["r1"])
        match verb:
            case "create_open":
                anchor.create_open(estate_id=_ESTATE, root=root)
            case "create_open_stranger":
                anchor.create_open(estate_id="estate-stranger", root=root)
            case "finalize":
                anchor.finalize(period)
            case "close_period":
                head = anchor.require().head
                closer = Path(head.root) if isinstance(head, OpenHead) else root
                anchor.close_period(
                    estate_id=_ESTATE,
                    period_id=period,
                    root=closer,
                    seal_digest=_seal(period, variant),
                )
            case "attest":
                anchor.attest(
                    period, estate_id=_ESTATE, root=root, seal_digest=_seal(period, variant)
                )
            case "claim" | "claim_crash":
                if verb == "claim_crash":

                    def crash(_anchor: object) -> None:
                        raise _Crash

                    anchor.write = crash  # type: ignore[method-assign,assignment]
                try:
                    anchor.claim_successor(
                        estate_id=_ESTATE,
                        seal_digest=_seal(period, variant),
                        next_period=period + 1,
                        target_root=root,
                    )
                finally:
                    anchor.__dict__.pop("write", None)
            case "open_claimed":
                head = anchor.require().head
                if not isinstance(head, ClaimedHead):
                    anchor.open_claimed(claim_id=_seal(9, "c"), period_id=2, root=root)
                elif (claim := anchor.read_claim(head.claim_id)) is not None:
                    # with the claim file gone, `act_on_head` refuses before this verb
                    anchor.open_claimed(
                        claim_id=claim.claim_id, period_id=claim.next_period, root=root
                    )
            case "reclaim":
                anchor.reclaim(estate_id=_ESTATE, claimed_actor="operator")
            case _:  # lose_claim: something deletes the claim file the head names
                current = anchor.read()
                if current is not None and isinstance(current.head, ClaimedHead):
                    anchor.claim_path(current.head.claim_id).unlink(missing_ok=True)


def test_every_anchor_verb_in_every_reached_anchor_state_takes_a_declared_transition(
    tmp_path: Path,
    strict: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """anchor_head and period_row, which the anchor's verbs take together.
    From an empty anchor directory, a breadth-first walk applies every event
    to every state it reaches, keyed on the head, the registry rows, whether
    the head's claim file exists, and whether a reclaim was recorded. The
    events, over two roots, periods 1 to 3, and each period's seal and a
    second digest for it: `create_open` for this estate and for a stranger;
    `finalize`; `close_period`; `attest`;
    `claim_successor`, and the same claim crashing between its claim file
    and the head write; `open_claimed` with the head's claim at its own next
    period, as both production callers bind it; `reclaim`; and the head's
    claim file going missing. An event passes when it writes after taking
    exactly the transitions its verb names, or is refused with EngineError,
    the anchor's own refusal, having taken none (a crashed claim may have
    taken its row before the write failed). A TransitionError means a row is
    missing.

    Left out: `open_claimed` with a period other than its claim's next
    period, which no caller passes and the verb does not check; the rows of
    other estates; and claims for period 4 and later, which repeat period 3's
    shapes."""
    rig = _AnchorRig(tmp_path)
    events = _anchor_events()
    problems: list[str] = []
    taken = _spy_on_take(monkeypatch, ANCHOR_HEAD)
    head, row = "anchor_head.", "period_row."
    takes = {
        "create_open": [{head + "01", row + "01"}],
        "create_open_stranger": [{head + "01", row + "01"}],
        "close_period": [{head + "02", row + "02"}],
        "claim": [{head + "03"}, {head + "06"}],
        "claim_crash": [{head + "03"}, {head + "06"}],
        "open_claimed": [{head + "04", row + "03"}],
        "reclaim": [{head + "05"}],
        "finalize": [{row + "04"}],
        "attest": [{row + "05"}],
        "lose_claim": [],
    }
    keys: dict[object, tuple[object, ...]] = {}  # snapshot -> key, to parse each state once

    def key_of(snapshot: tuple[bytes | None, tuple[tuple[str, bytes], ...]]) -> tuple[object, ...]:
        if snapshot not in keys:
            keys[snapshot] = rig.key()
        return keys[snapshot]

    try:
        start = rig.snapshot()
        reached = {key_of(start): start}
        frontier = [start]
        applied = 0
        while frontier:
            state = frontier.pop(0)
            for event in events:
                rig.restore(state)
                applied += 1
                taken.clear()
                try:
                    rig.apply(event)
                except (EngineError, _Crash):
                    pass  # the anchor's own refusal, or the injected crash
                except TransitionError as exc:
                    problems.append(f"{event} at {key_of(state)}: {exc}")
                after = rig.snapshot()
                verb, moved = event[0], set(taken)
                wrote = after != state and verb not in ("lose_claim", "claim_crash")
                if wrote or (verb == "claim_crash" and moved):
                    if moved not in takes[verb]:
                        problems.append(f"{event} at {key_of(state)}: took {sorted(moved)}")
                elif moved:
                    problems.append(f"{event} at {key_of(state)}: wrote nothing, took {moved}")
                if key_of(after) not in reached:
                    reached[key_of(after)] = after
                    frontier.append(after)
    finally:
        rig.anchor.release()
    assert problems == [], "\n".join(problems)
    heads = {k[0] for k in reached}
    assert heads == set(ANCHOR_HEAD.states)
    assert applied == len(reached) * len(events)
    assert _missed_sources(strict, ANCHOR_HEAD) == set()
    # an open head always has its row: `absent` is the close's tolerance (the row's guard)
    assert _missed_sources(strict, PERIOD_ROW) == {("period_row.02", "absent")}
    _assert_no_violation(strict, capfd.readouterr().err)
