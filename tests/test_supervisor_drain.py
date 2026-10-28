"""The drain a host signal starts (supervisor-protocol ss5 "The drain", DL-416).

A first SIGTERM or SIGINT stops admissions: SPAWN is refused `draining`, while
the lease verbs, LIST, PING and SIGNAL are served. Once no wrapper is live, or
`DSL41_DRAIN_S` has passed, the supervisor runs its TERM/grace/KILL wait and
exits 0. A second signal runs that wait at once. The engine holds a SPAWN
refused `draining` and sends it again until a supervisor accepts it.

Each test drives a real supervisor process and waits on files and answers,
never on a fixed sleep: a job ends when the test creates its gate file.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("supervisor tier is POSIX-only", allow_module_level=True)

from test_runner_supervisor import (
    RawClient,
    _exchange,
    _spec,
    start_supervisor,
    teardown_supervisor,
    wait_for,
)

from dsl41 import runner_supervisor
from dsl41.ir import lower_source
from dsl41.runner_adapters import (
    AdapterContext,
    Failed,
    ReplyLost,
    SupervisedCommandAdapter,
    SupervisorClient,
    SupervisorDraining,
    SupervisorUnavailable,
    Terminated,
    outcome_from_status,
)
from dsl41.runner_clock import RealClock

DSL41 = Path(sys.executable).with_name("dsl41")
LAUNCH = Path(__file__).resolve().parent.parent / "examples/nightbank/deploy/dsl41-launch"


def _env(drain_s: str) -> dict[str, str]:
    return {**os.environ, runner_supervisor.DRAIN_ENV: drain_s}


def _gated(gate: Path) -> str:
    """A command that runs until `gate` exists, then exits 0."""
    return f"while [ ! -e {gate} ]; do sleep 0.05; done; exit 0"


def _state(run_root: Path) -> str | None:
    try:
        cli = RawClient(run_root)
    except OSError:
        return None
    try:
        return cli.send({"v": 1, "cmd": "PING"}).get("process_state")
    except (OSError, AssertionError):
        return None
    finally:
        cli.close()


def _status(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "status.json").read_text())


@pytest.mark.parametrize("first", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_drains_a_live_run_to_its_own_outcome(short_root: Path, first: int) -> None:
    """A run live at the signal ends on its own and records its own outcome; the
    supervisor then exits 0 within the bound. During the drain SPAWN is refused
    `draining`, and PING, LIST, RENEW and SIGNAL are answered. SIGINT drains as
    SIGTERM does."""
    proc = start_supervisor(short_root, env=_env("120"))
    cli = RawClient(short_root)
    gate = short_root / "gate"
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        spec = _spec(run_dir, _gated(gate))
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        signalled_at = time.monotonic()
        proc.send_signal(first)
        wait_for(lambda: _state(short_root) == "draining")
        later = _spec(short_root / "runs" / "k.1", "exit 0")
        refused = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": later})
        assert refused == {"ok": False, "error": "draining"}
        assert not (short_root / "runs" / "k.1").exists()  # nothing ran
        listing = cli.send({"v": 1, "cmd": "LIST"})
        assert listing["process_state"] == "draining"
        assert [(r["job"], r["wrapper_alive"]) for r in listing["runs"]] == [("j", True)]
        assert cli.send({"v": 1, "cmd": "RENEW", "token": tok, "ttl_s": 60})["ok"] is True
        unknown = {"v": 1, "cmd": "SIGNAL", "token": tok, "run_id": "none", "sig": "TERM"}
        assert cli.send(unknown) == {"ok": False, "error": "unknown_run"}
        assert proc.poll() is None  # a live run holds the drain
        gate.touch()
        assert proc.wait(timeout=30) == 0
        assert time.monotonic() - signalled_at < 120
        assert _status(run_dir)["outcome"] == "exited"
        assert _status(run_dir)["exit_code"] == 0
        log = (short_root / "supervisor.log").read_text()
        assert "supervisor: draining: SPAWN refused; 1 live run(s)" in log
        assert "supervisor: drain ended: no live run" in log
        assert not (short_root / "supervisor.sock").exists()
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_a_run_past_the_drain_bound_takes_term_then_kill(short_root: Path) -> None:
    """A run that outlives `DSL41_DRAIN_S` and ignores SIGTERM: at the bound the
    supervisor sends TERM, then KILL after the run's grace, and exits 0. The
    wrapper records the KILL, which the engine reads as TERMINATED."""
    proc = start_supervisor(short_root, env=_env("1"))
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        seen = short_root / "term-seen"
        command = f"trap 'echo TERM >> {seen}' TERM; while :; do sleep 0.05; done"
        spec = _spec(run_dir, command, grace=0.5)
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        assert seen.read_text().startswith("TERM")
        status = _status(run_dir)
        assert (status["outcome"], status["signal"]) == ("signaled", signal.SIGKILL)
        assert isinstance(outcome_from_status(status), Terminated)
        log = (short_root / "supervisor.log").read_text()
        assert "supervisor: drain ended: the bound passed with 1 live run(s)" in log
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_a_second_signal_runs_the_stop_at_once(short_root: Path) -> None:
    """A second SIGTERM during the drain skips the rest of its 600 s bound: the
    supervisor TERMs the live run, which records the signal, and exits 0."""
    proc = start_supervisor(short_root, env=_env("600"))
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        spec = _spec(run_dir, _gated(short_root / "never"))
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        proc.send_signal(signal.SIGTERM)
        wait_for(lambda: _state(short_root) == "draining")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
        status = _status(run_dir)
        assert (status["outcome"], status["signal"]) == ("signaled", signal.SIGTERM)
        assert "drain ended" not in (short_root / "supervisor.log").read_text()
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "soon"])
def test_a_drain_bound_that_is_not_a_finite_non_negative_number_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """A configuration refusal, before the supervisor takes its root."""
    monkeypatch.setenv(runner_supervisor.DRAIN_ENV, value)
    assert runner_supervisor.main(["--run-root", str(tmp_path)]) == 2
    assert "DSL41_DRAIN_S must be a finite number of 0 or more" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_the_engine_holds_a_spawn_through_the_drain_and_sends_it_to_the_next_supervisor(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A SPAWN refused `draining` starts nothing and is not a failure: the adapter
    holds it and sends it again. The drained supervisor exits, the SPAWN then
    finds none, and the hold goes on. A new supervisor on the root takes the
    re-ACQUIRE and the SPAWN, and the run ends with its own exit code."""
    monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.05)
    first = start_supervisor(short_root, env=_env("120"))
    second = None
    gate = short_root / "gate"
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 3\n").jobs["k"]

    async def scenario() -> object:
        nonlocal second
        client = SupervisorClient(short_root)
        await client.connect()
        await client.acquire()
        refusals: list[type[BaseException]] = []
        drained, absent = asyncio.Event(), asyncio.Event()
        spawn = client.spawn

        async def recorded(spec: dict[str, Any], *gate: Any) -> dict[str, Any]:
            try:
                return await spawn(spec, *gate)
            except SupervisorUnavailable as exc:
                refusals.append(type(exc))
                if refusals.count(SupervisorDraining) >= 2:
                    drained.set()
                if type(exc) is SupervisorUnavailable:
                    absent.set()
                raise

        live = _spec(short_root / "runs" / "j.1", _gated(gate))
        assert (await client.spawn(live))["ok"] is True
        monkeypatch.setattr(client, "spawn", recorded)
        first.send_signal(signal.SIGTERM)
        await asyncio.to_thread(wait_for, lambda: _state(short_root) == "draining")
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0)
        ctx = AdapterContext(clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4()))
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await asyncio.wait_for(drained.wait(), timeout=20.0)
            assert not task.done() and not (short_root / "runs" / "k.1").exists()
            gate.touch()
            assert await asyncio.to_thread(first.wait, 30) == 0
            await asyncio.wait_for(absent.wait(), timeout=20.0)
            assert not task.done()
            second = await asyncio.to_thread(start_supervisor, short_root)
            return await asyncio.wait_for(task, timeout=30.0)
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    try:
        assert asyncio.run(scenario()) == 3
        assert _status(short_root / "runs" / "k.1")["exit_code"] == 3
        err = capsys.readouterr().err
        assert err.count("dsl41: k: SPAWN held: SPAWN refused: draining (None)") == 1
        assert err.count("dsl41: k: SPAWN accepted after the hold") == 1
    finally:
        teardown_supervisor(short_root, first)
        if second is not None:
            teardown_supervisor(short_root, second)


def test_a_drain_with_no_leaseholder_ends_at_the_deadman(short_root: Path) -> None:
    """Row .63: the deadman is sampled while draining. With no live leaseholder (host
    shutdown stops the engine first), the drain ends at T_deadman, not at its bound: the
    supervisor exits with no TERM of its own, and the wrapper ends its command on lifeline
    EOF and records `terminated / parent lost` (DL-205, DL-416)."""
    proc = start_supervisor(short_root, env=_env("120"), deadman_s=3.0)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        spec = _spec(run_dir, _gated(short_root / "never"), grace=0.5)
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        proc.send_signal(signal.SIGTERM)
        wait_for(lambda: _state(short_root) == "draining")
        cli.close()  # the leaseholder goes, as the engine does at a host shutdown
        assert proc.wait(timeout=30) == 0
        wait_for(lambda: (run_dir / "status.json").exists())
        status = _status(run_dir)
        assert (status["outcome"], status["cause"]) == ("terminated", "parent lost")
        log = (short_root / "supervisor.log").read_text()
        assert "supervisor: deadman fired" in log and "drain ended" not in log
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_the_maintenance_verb_is_refused_while_draining(short_root: Path) -> None:
    """Rows .65 and .66: `dsl41 supervise shutdown` meets a draining supervisor. It exits 2
    with the one sentence and changes nothing; a raw SHUTDOWN and MAINTENANCE_STOP are refused
    `draining` too. The live run goes on, and the drain then ends it in its own time."""
    proc = start_supervisor(short_root, env=_env("120"))
    cli = RawClient(short_root)
    gate = short_root / "gate"
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        spec = _spec(run_dir, _gated(gate))
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        proc.send_signal(signal.SIGTERM)
        wait_for(lambda: _state(short_root) == "draining")
        draining = {"ok": False, "error": "draining"}
        assert cli.send({"v": 1, "cmd": "SHUTDOWN", "token": tok}) == draining
        stop = {"v": 2, "cmd": "MAINTENANCE_STOP", "token": tok, "force": None}
        assert cli.send(stop) == draining
        cli.close()
        verb = subprocess.run(
            [str(DSL41), "supervise", "shutdown", "--run-root", str(short_root)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert verb.returncode == 2, verb.stderr
        assert "supervisor is draining; wait for the unit to stop" in verb.stderr
        assert not (short_root / "maintenance.json").exists()
        assert proc.poll() is None and _state(short_root) == "draining"
        gate.touch()
        assert proc.wait(timeout=30) == 0
        assert _status(run_dir)["exit_code"] == 0
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_the_unit_stop_mode_drains_a_real_supervisor(short_root: Path) -> None:
    """The example unit's ExecStop, `dsl41-launch supervisor-stop`, on a real supervisor
    process without systemd: it sends SIGTERM to MAINPID alone and waits. The live command
    outlives the stop until it ends on its own, records its own outcome, and the mode returns
    once the supervisor has exited 0 (DL-416)."""
    proc = start_supervisor(short_root, env=_env("120"))
    cli = RawClient(short_root)
    gate = short_root / "gate"
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        run_dir = short_root / "runs" / "j.1"
        spec = _spec(run_dir, _gated(gate))
        assert cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})["ok"] is True
        wait_for(lambda: (run_dir / "spawn.json").exists())
        command = json.loads((run_dir / "spawn.json").read_text())["command_pid"]
        stop = subprocess.Popen(
            ["sh", str(LAUNCH), "supervisor-stop"], env={**os.environ, "MAINPID": str(proc.pid)}
        )
        wait_for(lambda: _state(short_root) == "draining")
        os.kill(command, 0)  # the command outlives the stop's signal
        assert stop.poll() is None
        gate.touch()
        assert proc.wait(timeout=30) == 0  # reaped here, so the mode's kill -0 ends
        assert stop.wait(timeout=30) == 0
        assert _status(run_dir)["outcome"] == "exited"
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def _held_scenario(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, *, attach: bool, rival: bool
) -> tuple[object, list[str]]:
    """A SPAWN held by a drain, then the drained supervisor's exit. `attach` connects as a
    unit's engine does; otherwise the client provisions, as an engine that may fork its own
    supervisor does. With `rival`, a successor starts and another controller takes its lease
    before the engine's reconnect: the re-ACQUIRE is refused `lease_held`."""
    monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.05)
    first = start_supervisor(short_root, env=_env("120"))
    second: subprocess.Popen[bytes] | None = None
    rival_cli: RawClient | None = None
    gate = short_root / "gate"
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 0\n").jobs["k"]
    refusals: list[str] = []

    async def scenario() -> object:
        nonlocal second, rival_cli
        client = SupervisorClient(short_root)
        if attach:
            await client.connect()
        else:
            await client.provision()
        await client.acquire()
        released = asyncio.Event()
        reconnect = client.reconnect

        async def gated() -> bool:
            await released.wait()  # no reconnect reaches the successor before the rival
            return await reconnect()

        drained, absent = asyncio.Event(), asyncio.Event()
        spawn = client.spawn

        async def recorded(spec: dict[str, Any], *gate: Any) -> dict[str, Any]:
            try:
                return await spawn(spec, *gate)
            except SupervisorUnavailable as exc:
                refusals.append(type(exc).__name__)
                (drained if isinstance(exc, SupervisorDraining) else absent).set()
                raise

        live = _spec(short_root / "runs" / "j.1", _gated(gate))
        assert (await client.spawn(live))["ok"] is True
        monkeypatch.setattr(client, "spawn", recorded)
        monkeypatch.setattr(client, "reconnect", gated)
        first.send_signal(signal.SIGTERM)
        await asyncio.to_thread(wait_for, lambda: _state(short_root) == "draining")
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0)
        ctx = AdapterContext(clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4()))
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await asyncio.wait_for(drained.wait(), timeout=20.0)
            gate.touch()
            assert await asyncio.to_thread(first.wait, 30) == 0
            if rival:
                second = await asyncio.to_thread(start_supervisor, short_root)
                rival_cli = RawClient(short_root)
                acquired = rival_cli.send(
                    {"v": 1, "cmd": "ACQUIRE", "controller_id": "rival", "ttl_s": 60}
                )
                assert acquired["ok"] is True
            released.set()
            return await asyncio.wait_for(task, timeout=30.0)
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    try:
        return asyncio.run(scenario()), refusals
    finally:
        if rival_cli is not None:
            rival_cli.close()
        teardown_supervisor(short_root, first)
        if second is not None:
            teardown_supervisor(short_root, second)


def test_a_hold_ends_when_the_successor_refuses_the_re_acquire(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shape 1's bound (DL-416): the successor answers, but another controller holds its
    lease. The re-ACQUIRE is refused `lease_held`, which ends the hold as a failed spawn;
    nothing is resent and nothing ran."""
    result, refusals = _held_scenario(short_root, monkeypatch, attach=True, rival=True)
    assert isinstance(result, Failed), result
    assert "re-ACQUIRE refused: lease_held" in result.cause
    assert "SupervisorDraining" in refusals
    assert not (short_root / "runs" / "k.1").exists()


def test_a_hold_ends_when_no_unit_replaces_the_drained_supervisor(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shape 2's bound (DL-416): a client that may fork its own supervisor has no unit to
    restart it, so the drained supervisor's exit ends the hold as a failed spawn."""
    result, refusals = _held_scenario(short_root, monkeypatch, attach=False, rival=False)
    assert isinstance(result, Failed), result
    assert "wrapper spawn failed" in result.cause
    assert refusals[-1] == "SupervisorUnavailable"
    assert not (short_root / "runs" / "k.1").exists()


def test_a_reconnect_whose_ping_is_refused_names_the_refusal(short_root: Path) -> None:
    """The reconnect notes a successor that answers PING with a refusal, so a hold ends on
    it; a later reconnect that succeeds clears the note (DL-416)."""

    async def scenario() -> tuple[str | None, str | None]:
        answers = [b'{"ok": false, "error": "unsupported_version"}\n']

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while line := await reader.readline():
                request = json.loads(line)
                if answers:
                    writer.write(answers.pop(0))
                elif request["cmd"] == "PING":
                    writer.write(b'{"ok": true, "incarnation": "a", "deadman_s": null}\n')
                await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(serve, path=str(short_root / "supervisor.sock"))
        client = SupervisorClient(short_root)
        try:
            refused = await client.reconnect()
            first = client.refusal
            assert refused is False
            assert await client.reconnect() is True
            return first, client.refusal
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == ("PING refused: unsupported_version", None)


# ---- the latch at the dispatch boundary (DL-416)


def _lines(peer: Any, count: int) -> list[dict[str, Any]]:
    data = b""
    while data.count(b"\n") < count:
        data += peer.recv(65536)
    return [json.loads(line) for line in data.splitlines()[:count]]


def _leased(sup: Any, conn: Any, peer: Any) -> dict[str, Any]:
    granted = _exchange(sup, conn, peer, {"cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
    return {"incarnation": sup.incarnation, "token": granted["token"]}


def test_a_signal_latched_before_a_buffered_spawn_drains_first(queued_supervisor: Any) -> None:
    """The loop reads the connection before the self-pipe: the latch still moves the chart
    first, so the SPAWN read after the signal meets the drain and writes nothing."""
    sup, connect = queued_supervisor
    conn, peer = connect()
    creds = _leased(sup, conn, peer)
    sup._on_term_signal(signal.SIGTERM, None)
    spawn = {"v": 1, "cmd": "SPAWN", "spec": {}, **creds}
    peer.sendall(json.dumps(spawn).encode() + b"\n")
    sup._readable(conn)
    sup._writable(conn)
    assert _lines(peer, 1) == [{"ok": False, "error": "draining"}]
    assert sup.state == "draining" and sup._signals == 0


def test_a_signal_between_buffered_requests_drains_before_the_next(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests in one read: a signal that lands while the first is answered moves the
    chart before the second, a SPAWN, is dispatched."""
    sup, connect = queued_supervisor
    conn, peer = connect()
    creds = _leased(sup, conn, peer)
    ping = sup._h_ping

    def signalled(conn_: Any, req: dict[str, Any]) -> dict[str, Any]:
        sup._on_term_signal(signal.SIGTERM, None)
        return ping(conn_, req)

    monkeypatch.setattr(sup, "_h_ping", signalled)
    spawn = {"v": 1, "cmd": "SPAWN", "spec": {}, **creds}
    peer.sendall(b'{"v": 1, "cmd": "PING"}\n' + json.dumps(spawn).encode() + b"\n")
    sup._readable(conn)
    sup._writable(conn)
    answered, refused = _lines(peer, 2)
    assert answered["ok"] is True and answered["process_state"] == "serving"
    assert refused == {"ok": False, "error": "draining"}
    assert sup.state == "draining"


def test_a_buffered_shutdown_in_a_drain_is_refused_and_keeps_the_latch(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row .65: a SHUTDOWN during the drain is refused `draining`. A signal that lands while
    it is answered stays counted for the loop, which then runs the wait at once."""
    sup, connect = queued_supervisor
    conn, peer = connect()
    creds = _leased(sup, conn, peer)
    sup._orderly_shutdown(runner_supervisor.TERM)
    assert sup.state == "draining"
    fence = sup._fence

    def signalled(verb: str, req: dict[str, Any]) -> dict[str, Any] | None:
        sup._on_term_signal(signal.SIGTERM, None)
        return fence(verb, req)

    monkeypatch.setattr(sup, "_fence", signalled)
    shutdown = {"v": 1, "cmd": "SHUTDOWN", **creds}
    peer.sendall(json.dumps(shutdown).encode() + b"\n")
    sup._readable(conn)
    sup._writable(conn)
    assert _lines(peer, 1) == [{"ok": False, "error": "draining"}]
    assert sup.state == "draining" and sup._signals == 1


def test_two_signals_read_at_once_run_the_wait(queued_supervisor: Any) -> None:
    """Two signals before the loop reads either: .06 and then .61, so the second is not
    lost in the first (DL-416)."""
    sup, _connect = queued_supervisor
    sup._on_term_signal(signal.SIGTERM, None)
    sup._on_term_signal(signal.SIGINT, None)
    sup._orderly_shutdown(runner_supervisor.TERM)
    assert sup.state == "stopped" and sup._signals == 0


# ---- the routing gate and a lost resend reply (DL-416)


def test_a_held_spawn_waits_out_a_quarantine_and_ends_unsent_on_eviction(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each resend passes the routing gate (concurrency-model ss8). A drain refusal holds the
    SPAWN; the supervisor exits; the host is quarantined, so nothing is resent even after a
    supervisor returns; the host is evicted, so the hold ends unsent and nothing runs."""
    monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.05)
    first = start_supervisor(short_root, env=_env("120"))
    second: subprocess.Popen[bytes] | None = None
    gate = short_root / "gate"
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 0\n").jobs["k"]
    host = {"state": "active", "reads": 0}

    def route() -> str:
        host["reads"] += 1
        return host["state"]

    async def scenario() -> object:
        nonlocal second
        client = SupervisorClient(short_root)
        await client.connect()
        await client.acquire()
        attempts: list[str] = []
        drained = asyncio.Event()
        spawn = client.spawn

        async def recorded(spec: dict[str, Any], *gate: Any) -> dict[str, Any]:
            try:
                return await spawn(spec, *gate)
            except SupervisorUnavailable as exc:
                attempts.append(type(exc).__name__)
                if isinstance(exc, SupervisorDraining):
                    drained.set()
                raise

        live = _spec(short_root / "runs" / "j.1", _gated(gate))
        assert (await client.spawn(live))["ok"] is True
        monkeypatch.setattr(client, "spawn", recorded)
        first.send_signal(signal.SIGTERM)
        await asyncio.to_thread(wait_for, lambda: _state(short_root) == "draining")
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0)
        ctx = AdapterContext(
            clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4()), route=route
        )
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await asyncio.wait_for(drained.wait(), timeout=20.0)
            gate.touch()
            assert await asyncio.to_thread(first.wait, 30) == 0
            host["state"] = "quarantined"
            sent = len(attempts)
            second = await asyncio.to_thread(start_supervisor, short_root)
            seen = host["reads"]
            while host["reads"] < seen + 5:  # five passes of the gate with a supervisor up
                await asyncio.sleep(0.01)
            assert len(attempts) <= sent + 1 and not task.done()
            host["state"] = "evicted"
            return await asyncio.wait_for(task, timeout=30.0)
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    try:
        result = asyncio.run(scenario())
        assert isinstance(result, Failed), result
        assert "evicted while the SPAWN was held; nothing was sent" in result.cause
        assert not (short_root / "runs" / "k.1").exists()
    finally:
        teardown_supervisor(short_root, first)
        if second is not None:
            teardown_supervisor(short_root, second)


def test_a_resend_whose_reply_is_lost_is_read_from_the_spool(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successor accepts the resend and dies before its reply reaches the engine. The spool
    shows the run, so the hold ends with no further resend; with no supervisor to ask, the
    engine reads the outcome the wrapper recorded from the spool (DL-416)."""
    monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.05)
    first = start_supervisor(short_root, env=_env("120"))
    second: subprocess.Popen[bytes] | None = None
    gate = short_root / "gate"
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 3\n").jobs["k"]

    async def scenario() -> tuple[object, list[str]]:
        nonlocal second
        client = SupervisorClient(short_root)
        await client.connect()
        await client.acquire()
        attempts: list[str] = []
        drained = asyncio.Event()
        spawn = client.spawn

        async def recorded(spec: dict[str, Any], *gate: Any) -> dict[str, Any]:
            try:
                await spawn(spec, *gate)
            except SupervisorUnavailable as exc:
                attempts.append(type(exc).__name__)
                if isinstance(exc, SupervisorDraining):
                    drained.set()
                raise
            attempts.append("accepted")
            # the wrapper records, then the successor dies before its reply reaches the engine
            wait_for(lambda: (short_root / "runs" / "k.1" / "status.json").exists())
            os.kill(json.loads((short_root / "supervisor.pid").read_text())["pid"], signal.SIGKILL)
            raise ReplyLost("connection lost")

        live = _spec(short_root / "runs" / "j.1", _gated(gate))
        assert (await client.spawn(live))["ok"] is True
        monkeypatch.setattr(client, "spawn", recorded)
        first.send_signal(signal.SIGTERM)
        await asyncio.to_thread(wait_for, lambda: _state(short_root) == "draining")
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0, settle_seconds=0.5)
        ctx = AdapterContext(clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4()))
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await asyncio.wait_for(drained.wait(), timeout=20.0)
            gate.touch()
            assert await asyncio.to_thread(first.wait, 30) == 0
            second = await asyncio.to_thread(start_supervisor, short_root)
            return await asyncio.wait_for(task, timeout=30.0), attempts
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    try:
        result, attempts = asyncio.run(scenario())
        assert result == 3
        assert attempts.count("accepted") == 1 and attempts[-1] == "accepted"
        assert _status(short_root / "runs" / "k.1")["exit_code"] == 3
    finally:
        teardown_supervisor(short_root, first)
        if second is not None:
            teardown_supervisor(short_root, second)


def test_the_engine_reports_its_executors_routing_state(tmp_path: Path) -> None:
    """The gate a held SPAWN's resend passes is the engine's own executor row (ss8)."""
    from datetime import datetime

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_hosts import HostCommand
    from dsl41.runner_startup import start_run

    at = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        lower_source("insert_job: j\njob_type: c\ncommand: x\n"),
        tmp_path / "run",
        clock=VirtualClock(start=at),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def drain() -> None:
        try:
            assert engine.host_state() == "active"
            engine.inject_host(HostCommand(verb="drain", host_id=engine.executor_id))
            await engine.run_until_quiescent(at)
            assert engine.host_state() == "passive"
            engine.executor_id = "absent"
            assert engine.host_state() is None
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(drain())


def test_the_stop_helper_alone_is_no_residual_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unit's ExecStop helper waits in the cgroup for the supervisor's exit: one process
    whose parent is PID 1 and whose argv is the launcher's `supervisor-stop`, with its
    `sleep`. The idle proof leaves it out; two such processes, a wrong parent or another
    argv leave everything in (DL-416)."""
    from types import SimpleNamespace

    from dsl41 import runner_procid as procid

    me = os.getpid()
    launcher = ["/bin/sh", "/opt/dsl41/bin/dsl41-launch", "supervisor-stop"]
    table: dict[int, tuple[str, list[str]]] = {
        101: ("1", launcher),
        102: ("101", ["sleep", "0.2"]),
        103: ("101", ["cat"]),
        104: ("7", launcher),
        106: ("1", ["/bin/sh", "other"]),
        107: ("1", ["/bin/other-launcher", "supervisor-stop"]),
        108: ("1", []),
    }
    # the supervisor imports runner_procid as a top-level module too: patch both copies
    for module in {procid, sys.modules[runner_supervisor.stop_helpers.__module__]}:
        monkeypatch.setattr(
            module, "_linux_stat_fields", lambda pid: ["S", table[pid][0]] if pid in table else None
        )
        monkeypatch.setattr(module, "_cmdline", lambda pid: table[pid][1] if pid in table else None)
    assert procid.stop_helpers({me, 101, 102, 103, 104, 106, 107, 108}) == {101, 102}
    # a sample member gone from /proc by the time it is read, the helper's ended `sleep`,
    # is gone with the helper present, and stays a member without it
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert procid.stop_helpers({me, 101, gone.pid}) == {101, gone.pid}
    assert procid.stop_helpers({me, gone.pid}) == set()
    assert procid._gone(gone.pid) and not procid._gone(me)
    if os.getuid() != 0:
        assert not procid._gone(1)  # alive and not ours: EPERM is no proof of absence
    table[105] = ("1", ["/x/dsl41-launch", "supervisor-stop"])
    assert procid.stop_helpers({me, 101, 105}) == set()
    assert procid.stop_helpers({me, 104}) == set()

    sup = runner_supervisor.Supervisor(str(tmp_path))
    try:
        (tmp_path / "leader.lock").touch()
        sup._maintenance_lock_fd = procid.flock_existing(tmp_path / "leader.lock")
        monkeypatch.setattr(sup, "_reap", lambda: None)
        proof = SimpleNamespace(members=lambda: {me, 101, 102})
        assert sup._maintenance_idle(proof) is True  # type: ignore[arg-type]
        busy = SimpleNamespace(members=lambda: {me, 101, 102, 103})
        assert sup._maintenance_idle(busy) is False  # type: ignore[arg-type]
    finally:
        sup._release_maintenance_lock()
        for fd in (sup._chld_r, sup._chld_w):
            os.close(fd)
        sup._sel.close()


def test_the_cmdline_reader_reads_a_live_process_and_refuses_a_gone_one() -> None:
    from dsl41 import runner_procid as procid

    if sys.platform.startswith("linux"):  # pragma: no cover -- exercised on Linux CI
        assert procid._cmdline(os.getpid())
    assert procid._cmdline(2**22 + 12345) is None


def test_a_second_signal_before_a_buffered_request_stops_without_answering(
    queued_supervisor: Any,
) -> None:
    """In a drain, a counted signal read before the next buffered request runs the wait at
    once (.61); the stopped supervisor dispatches nothing more."""
    sup, connect = queued_supervisor
    conn, peer = connect()
    sup._orderly_shutdown(runner_supervisor.TERM)
    sup._on_term_signal(signal.SIGTERM, None)
    peer.sendall(b'{"v": 1, "cmd": "PING"}\n')
    sup._readable(conn)
    assert sup.state == "stopped" and not conn.out


def test_a_first_spawn_whose_reply_is_lost_fails_as_before(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a hold, a SPAWN whose reply is lost is the spawn's failure, as before DL-416;
    inside one, a lost reply with no receipt on disk sent nothing that ran, so the hold goes
    on and the next resend is taken."""
    proc = start_supervisor(short_root, env=_env("120"))
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 0\n").jobs["k"]

    async def scenario() -> tuple[object, object]:
        client = SupervisorClient(short_root)
        await client.connect()
        await client.acquire()
        spawn = client.spawn
        script: list[BaseException] = [ReplyLost("lost")]

        async def lossy(spec: dict[str, Any], *gate: Any) -> dict[str, Any]:
            if script:
                raise script.pop(0)
            return await spawn(spec, *gate)

        monkeypatch.setattr(client, "spawn", lossy)
        monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.01)
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0)
        try:
            first = await adapter.run(
                job,
                1,
                AdapterContext(clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4())),
            )
            script[:] = [SupervisorDraining("draining"), ReplyLost("lost")]
            second = await adapter.run(
                job,
                2,
                AdapterContext(clock=RealClock(), run_root=short_root, run_id=str(uuid.uuid4())),
            )
            return first, second
        finally:
            await client.close()

    try:
        first, second = asyncio.run(scenario())
        assert isinstance(first, Failed) and "lost" in first.cause
        assert second == 0
    finally:
        teardown_supervisor(short_root, proc)


def test_the_cmdline_is_split_on_nul(monkeypatch: pytest.MonkeyPatch) -> None:
    from dsl41 import runner_procid as procid

    monkeypatch.setattr(procid.Path, "read_bytes", lambda _self: b"/bin/sh\0x\0")
    assert procid._cmdline(1) == ["/bin/sh", "x"]


def test_an_eviction_during_the_resends_reconnect_sends_nothing(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing gate runs under the client's lock after the reconnect, just before the
    write: a host evicted while the resend awaits its reconnect to a returned supervisor ends
    the hold with nothing written, and nothing runs (concurrency-model ss8, DL-416)."""
    monkeypatch.setattr(SupervisedCommandAdapter, "_HOLD_RETRY_S", 0.05)
    first = start_supervisor(short_root, env=_env("120"))
    second: subprocess.Popen[bytes] | None = None
    gate = short_root / "gate"
    job = lower_source("insert_job: k\njob_type: c\ncommand: exit 0\n").jobs["k"]
    host = {"state": "active"}

    async def scenario() -> object:
        nonlocal second
        client = SupervisorClient(short_root)
        await client.connect()
        await client.acquire()
        drained = asyncio.Event()
        armed = False
        spawn = client.spawn
        ensure = client._ensure_connected
        task: asyncio.Task[object] | None = None

        async def recorded(spec: dict[str, Any], *gate_: Any) -> dict[str, Any]:
            try:
                return await spawn(spec, *gate_)
            except SupervisorDraining:
                drained.set()
                raise

        async def evicting() -> None:
            # lands while the resend awaits its reconnect to the returned supervisor
            if armed and asyncio.current_task() is task:
                if (short_root / "supervisor.sock").exists():
                    host["state"] = "evicted"
            await ensure()

        live = _spec(short_root / "runs" / "j.1", _gated(gate))
        assert (await client.spawn(live))["ok"] is True
        monkeypatch.setattr(client, "spawn", recorded)
        monkeypatch.setattr(client, "_ensure_connected", evicting)
        first.send_signal(signal.SIGTERM)
        await asyncio.to_thread(wait_for, lambda: _state(short_root) == "draining")
        adapter = SupervisedCommandAdapter(client, grace_seconds=1.0)
        ctx = AdapterContext(
            clock=RealClock(),
            run_root=short_root,
            run_id=str(uuid.uuid4()),
            route=lambda: host["state"],
        )
        task = asyncio.create_task(adapter.run(job, 1, ctx))
        try:
            await asyncio.wait_for(drained.wait(), timeout=20.0)
            gate.touch()
            assert await asyncio.to_thread(first.wait, 30) == 0
            armed = True
            second = await asyncio.to_thread(start_supervisor, short_root)
            return await asyncio.wait_for(task, timeout=30.0)
        finally:
            if not task.done():
                task.cancel()
            await client.close()

    try:
        result = asyncio.run(scenario())
        assert isinstance(result, Failed), result
        assert "evicted while the SPAWN was held; nothing was sent" in result.cause
        assert not (short_root / "runs" / "k.1").exists()
    finally:
        teardown_supervisor(short_root, first)
        if second is not None:
            teardown_supervisor(short_root, second)
