"""Phase 11f supervisor-tier tests: the frozen socket protocol and the
detached kill matrix (spec ss5 / docs/supervisor-protocol.md ss5, DL-48).

The supervisor is stdlib-only and run BY FILE PATH exactly as the engine
runs it; protocol tests drive it as a subprocess over a raw AF_UNIX socket.
Integration tests use a real detached engine subprocess
(tests/runner_detached_driver.py) and SIGKILL/SIGINT it, then resume
in-process -- the job SURVIVES because its parent is the supervisor, not the
engine (E4 dissolved). Timing follows test_runner_lifecycle.py's wait_for
polling rather than bare sleeps.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("supervisor tier is POSIX-only", allow_module_level=True)

from datetime import datetime

from dsl41 import runner_supervisor, runner_wrapper
from dsl41.ir import lower_source
from dsl41.runner import (
    FileWatcherAdapter,
    RealClock,
    SupervisedCommandAdapter,
    SupervisorClient,
    read_journal,
    resume_run,
)

SUPERVISOR = Path(runner_supervisor.__file__)
DRIVER = Path(__file__).parent / "runner_detached_driver.py"


@pytest.fixture
def short_root():
    """A short base dir for AF_UNIX supervisor sockets: pytest's tmp_path can
    exceed sun_path's 104-byte macOS limit once supervisor.sock is appended
    (same workaround as test_runner_control.py's fixture)."""
    d = tempfile.mkdtemp(prefix="dsl41s-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def wait_for(predicate, timeout_s: float = 10.0, interval_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def proc_state(pid: int) -> str:
    """First letter of the process state ('T' = stopped), '' if gone."""
    out = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return out.stdout.strip()[:1]


# ---------------------------------------------------------- protocol harness


def start_supervisor(run_root: Path, env: dict | None = None) -> subprocess.Popen:
    (run_root / "runs").mkdir(parents=True, exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    logf = (run_root / "supervisor.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, str(SUPERVISOR), "--run-root", str(run_root)],
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        start_new_session=True,
        env=env,
    )
    logf.close()
    # wait for a real PING, not just the socket file: a stale leftover socket
    # exists before a fresh supervisor unlinks and rebinds (test_stale_socket)
    wait_for(lambda: _ping_ok(run_root))
    return proc


def _ping_ok(run_root: Path) -> bool:
    try:
        cli = RawClient(run_root)
    except OSError:
        return False
    try:
        return cli.send({"v": 1, "cmd": "PING"}).get("ok") is True
    except OSError:
        return False
    finally:
        cli.close()


class RawClient:
    """A raw socket client: send one JSON line, read the next NON-push line."""

    def __init__(self, run_root: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10.0)
        self.sock.connect(str(run_root / "supervisor.sock"))
        self.buf = b""

    def raw(self, payload: bytes) -> dict:
        self.sock.sendall(payload)
        return self._read()

    def send(self, obj: dict) -> dict:
        self.sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")
        return self._read()

    def _read(self) -> dict:
        while True:
            while b"\n" not in self.buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise AssertionError("supervisor closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            obj = json.loads(line)
            if obj.get("push"):
                continue
            return obj

    def close(self) -> None:
        self.sock.close()


def teardown_supervisor(run_root: Path, proc: subprocess.Popen) -> None:
    """Best-effort: kill any surviving command groups + the supervisor."""
    _kill_group(run_root)
    if proc.poll() is None:
        proc.kill()
        proc.wait()


# ------------------------------------------------------ import boundary + unit


def test_supervisor_imports_are_stdlib_only() -> None:
    """DL-42 item 3 / spec ss1: the supervisor is the future extraction
    boundary alongside the wrapper -- stdlib only, nothing from dsl41."""
    tree = ast.parse(SUPERVISOR.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would reach into dsl41"
            assert node.module is not None
            imported.add(node.module.partition(".")[0])
    non_stdlib = sorted(imported - set(sys.stdlib_module_names))
    assert non_stdlib == [], f"supervisor imports outside stdlib: {non_stdlib}"


def test_peer_uid_same_uid_on_socketpair() -> None:
    """The same-uid gate input (spec ss1): a local peer's uid is our uid."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert runner_supervisor.peer_uid(a) == os.getuid()
        assert runner_supervisor.peer_uid(b) == os.getuid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux-only subreaper")
def test_subreaper_set_on_linux() -> None:  # pragma: no cover -- exercised on Linux CI
    """spec ss5 item 7: PR_SET_CHILD_SUBREAPER is Linux-only; assert the call
    path runs without error (marked, never faked on darwin)."""
    sup = runner_supervisor.Supervisor.__new__(runner_supervisor.Supervisor)
    sup._set_subreaper()  # best-effort, must not raise


# ------------------------------------------------------------ protocol tests


def test_ping_and_unknown_and_version(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        assert cli.send({"v": 1, "cmd": "PING"}) == {"ok": True, "version": 1}
        assert cli.send({"v": 1, "cmd": "NOPE"})["error"] == "unknown_verb"
        assert cli.send({"cmd": "PING"})["error"] == "unsupported_version"  # missing v
        assert cli.send({"v": 2, "cmd": "PING"})["error"] == "unsupported_version"
        assert cli.raw(b"{not json}\n")["error"] == "malformed_json"
        # a malformed line does not desync the stream
        assert cli.send({"v": 1, "cmd": "PING"})["ok"] is True
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_lease_held_reacquire_and_fencing_monotonicity(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    a = RawClient(short_root)
    b = RawClient(short_root)
    try:
        r1 = a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
        assert r1["ok"] and r1["token"] == 1
        # another controller is refused while A's lease is unexpired
        held = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B", "ttl_s": 60})
        assert held == {
            "ok": False,
            "error": "lease_held",
            "holder": "A",
            "expires_at": r1["expires_at"],
        }
        # re-acquire by the SAME controller mints a strictly greater token
        r2 = a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
        assert r2["ok"] and r2["token"] == 2
        # the OLD token is now stale for mutating verbs (fencing)
        stale = a.send({"v": 1, "cmd": "SHUTDOWN", "token": 1})
        assert stale == {"ok": False, "error": "stale_token"}
        # RELEASE with the live token, then B can acquire (token keeps climbing)
        assert a.send({"v": 1, "cmd": "RELEASE", "token": 2}) == {"ok": True}
        r3 = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B", "ttl_s": 60})
        assert r3["ok"] and r3["token"] == 3
    finally:
        a.close()
        b.close()
        teardown_supervisor(short_root, proc)


def test_lease_expiry_lets_a_new_holder_in(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    a = RawClient(short_root)
    b = RawClient(short_root)
    try:
        assert a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 0.3})["ok"]
        time.sleep(0.5)  # let A's lease expire
        r = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B", "ttl_s": 60})
        assert r["ok"]  # a different controller gets in once the lease lapses
        # A's expired token is refused
        assert a.send({"v": 1, "cmd": "SHUTDOWN", "token": 1})["error"] == "stale_token"
    finally:
        a.close()
        b.close()
        teardown_supervisor(short_root, proc)


def test_spawn_idempotency_replay(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "j.1"
        rd.mkdir()
        spec = _spec(rd, "echo hi; exit 0")
        first = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        assert first["ok"] and "duplicate" not in first
        again = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        # same run_id => the original result, nothing re-spawned
        assert again["duplicate"] is True
        assert again["wrapper_pid"] == first["wrapper_pid"]
        wait_for(lambda: (rd / "status.json").exists())
        # exactly one wrapper ran (one spawn.json)
        assert json.loads((rd / "spawn.json").read_text())["command_pid"]
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_mutating_verbs_require_a_token(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        rd = short_root / "runs" / "j.1"
        rd.mkdir()
        # no lease held: SPAWN/SIGNAL/SHUTDOWN all refuse
        assert (
            cli.send({"v": 1, "cmd": "SPAWN", "spec": _spec(rd, "true")})["error"] == "stale_token"
        )
        assert cli.send({"v": 1, "cmd": "SIGNAL", "run_id": "x", "sig": "TERM"})["error"] == (
            "stale_token"
        )
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_signal_pid_reuse_guard_refuses_spoofed_spawn(short_root: Path) -> None:
    """spec ss5: SIGNAL verifies the recorded (pid, start-time) before killing
    the group. A spoofed spawn.json pointing at an innocent live pid must be a
    noop -- the innocent is never signaled."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    innocent = subprocess.Popen(["sleep", "30"])
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "j.1"
        rd.mkdir()
        # SPAWN a real (short) run to register the run_id, then overwrite its
        # spawn.json with a spoof pointing at the innocent pid + a stale token
        spec = _spec(rd, "sleep 30")
        run_id = spec["run_id"]
        cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        wait_for(lambda: (rd / "spawn.json").exists())
        stale_token = (
            "ticks:1" if sys.platform.startswith("linux") else "lstart:Mon Jan  1 00:00:00 2001"
        )
        (rd / "spawn.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": run_id,
                    "job": "j",
                    "run_number": 1,
                    "wrapper_pid": innocent.pid,
                    "wrapper_start_time": stale_token,
                    "command_pid": innocent.pid,
                    "command_pgid": innocent.pid,
                    "command_start_time": stale_token,
                    "boot_id": runner_supervisor.current_boot_id(),
                    "started_at": "2026-07-11T00:00:00+00:00",
                }
            )
        )
        resp = cli.send({"v": 1, "cmd": "SIGNAL", "token": tok, "run_id": run_id, "sig": "KILL"})
        assert resp == {"ok": True, "noop": True}  # verify-alive failed: never signaled
        assert pid_alive(innocent.pid)  # the innocent is untouched
    finally:
        cli.close()
        innocent.kill()
        innocent.wait()
        teardown_supervisor(short_root, proc)


def test_shutdown_orderly_records_signaled_never_parent_lost(short_root: Path) -> None:
    """spec ss5 item 3: SHUTDOWN TERMs each command; lifelines stay open until
    wrappers exit, so wrappers record signaled/exited, NEVER parent-lost."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "j.1"
        rd.mkdir()
        cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(rd, "sleep 30")})
        wait_for(lambda: (rd / "spawn.json").exists())
        assert cli.send({"v": 1, "cmd": "SHUTDOWN", "token": tok}) == {"ok": True}
        proc.wait(timeout=10)
        assert proc.returncode == 0
        status = json.loads((rd / "status.json").read_text())
        assert status["outcome"] == "signaled"  # killed by SIGTERM, wrapper observed
        assert status.get("cause") != "parent lost"
        assert not (short_root / "supervisor.sock").exists()  # socket unlinked
        assert not (short_root / "supervisor.pid").exists()
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_stale_socket_is_reclaimed(short_root: Path) -> None:
    """spec ss1: a dead supervisor's leftover socket is unlinked and a fresh
    one binds (parity with the engine's ss10 control-socket gate)."""
    proc = start_supervisor(short_root)
    proc.kill()  # -9: no orderly unlink; the socket file lingers
    proc.wait()
    assert (short_root / "supervisor.sock").exists()  # stale file
    proc2 = start_supervisor(short_root)  # binds after unlinking the stale socket
    cli = RawClient(short_root)
    try:
        assert cli.send({"v": 1, "cmd": "PING"})["ok"] is True
    finally:
        cli.close()
        teardown_supervisor(short_root, proc2)


def test_shutdown_waits_for_late_spawn_record(short_root: Path) -> None:
    """DL-48 review fix 2: SHUTDOWN racing a wrapper that has not yet written
    spawn.json must still end in signaled/exited -- the supervisor waits
    (bounded, 5s) for the record instead of no-op-signaling and leaving the
    wrapper to record 'parent lost' at lifeline EOF. The wrapper is frozen at
    post_spawn_pre_record (command spawned, spawn.json absent) and released
    at 3.5s: with grace 0.5s the unfixed shutdown has no-op'd every signal
    and EXITED by ~2.5s (the wrapper then records parent-lost at EOF), while
    the fixed pre-wait is still watching -- the timing pins the fix."""
    env = dict(os.environ)
    env[runner_wrapper.PAUSE_ENV] = "post_spawn_pre_record"
    proc = start_supervisor(short_root, env=env)
    cli = RawClient(short_root)
    releaser: threading.Timer | None = None
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "j.1"
        rd.mkdir()
        spawned = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(rd, "sleep 30", grace=0.5)}
        )
        wrapper_pid = spawned["wrapper_pid"]
        wait_for(lambda: proc_state(wrapper_pid) == "T")  # frozen pre-record
        assert not (rd / "spawn.json").exists()
        releaser = threading.Timer(3.5, os.kill, args=(wrapper_pid, signal.SIGCONT))
        releaser.start()
        # blocks: ss5 order replies only after the wrappers are collected
        assert cli.send({"v": 1, "cmd": "SHUTDOWN", "token": tok}) == {"ok": True}
        proc.wait(timeout=10)
        status = json.loads(
            wait_for(lambda: (rd / "status.json").exists() and (rd / "status.json").read_text())
        )
        assert status["outcome"] == "signaled"  # TERM observed, recorded truthfully
        assert status.get("cause") != "parent lost"
    finally:
        if releaser is not None:
            releaser.cancel()
        cli.close()
        teardown_supervisor(short_root, proc)


def test_cancelled_request_poisons_and_reconnects(short_root: Path) -> None:
    """DL-48 review fix 1 (MAJOR, confirmed by execution): a request cancelled
    between write and reply leaves the reply in flight with no correlation id
    -- delivered verbatim, it would resolve the NEXT request's future (the
    reviewer reproduced a cancelled SPAWN's reply landing in a later ACQUIRE).
    The client must POISON the connection on cancel, and the next call must
    lazily reconnect + re-ACQUIRE (stable controller_id -> fresh fencing
    token) and receive ITS OWN reply, never the orphan. Driven against a fake
    supervisor that holds one LIST reply hostage."""

    async def scenario() -> tuple[dict, int, int | None]:
        held: list[asyncio.StreamWriter] = []
        hold_next_list = True
        next_token = 1

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal hold_next_list, next_token
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    req = json.loads(line)
                    cmd = req.get("cmd")
                    if cmd == "ACQUIRE":
                        resp = {"ok": True, "token": next_token, "expires_at": "t"}
                        next_token += 1
                    elif cmd == "LIST":
                        if hold_next_list:
                            hold_next_list = False
                            held.append(writer)  # hold the reply: the client parks
                            continue
                        resp = {"ok": True, "version": 1, "runs": [], "which": "fresh"}
                    else:
                        resp = {"ok": True, "version": 1}
                    writer.write(json.dumps(resp).encode("utf-8") + b"\n")
                    await writer.drain()
            except OSError:
                pass  # the poisoned peer vanished mid-exchange (EPIPE via the orphan write)
            finally:
                # a handler that exits on EOF leaves the connection half-open;
                # 3.12's Server.wait_closed() then waits for it FOREVER (the
                # engine's ControlServer learned this as DL-45 review B1)
                writer.close()

        server = await asyncio.start_unix_server(handle, path=str(short_root / "supervisor.sock"))
        client = SupervisorClient(short_root)
        await client.ensure_running()
        tok1 = await client.acquire()
        task = asyncio.ensure_future(client.list_runs())
        await asyncio.sleep(0.2)  # LIST written; reply held; task parks on its future
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.lost.is_set()  # the connection was poisoned
        # the orphan reply now arrives on the OLD (closed) connection: nowhere
        if held:
            with contextlib.suppress(OSError, ConnectionResetError):
                held[0].write(json.dumps({"ok": True, "which": "orphan"}).encode() + b"\n")
                await held[0].drain()
        # next call: lazy reconnect + re-ACQUIRE, and it gets ITS OWN reply
        resp = await client.list_runs()
        tok2 = client.token
        await client.close()
        server.close()
        await server.wait_closed()
        return resp, tok1, tok2

    resp, tok1, tok2 = asyncio.run(scenario())
    assert resp.get("which") == "fresh"  # never the orphan
    assert tok2 is not None and tok2 > tok1  # re-ACQUIRE minted a fresh fencing token


def test_renew_loop_reacquires_after_lease_lapse(short_root: Path) -> None:
    """DL-48 review fix 3: a lapsed lease (RENEW -> stale_token) must not end
    renewal -- the loop re-ACQUIREs with the stable controller_id and the
    client keeps a live, monotonically fenced token."""
    proc = start_supervisor(short_root)
    try:

        async def scenario() -> tuple[int, int | None, dict]:
            client = SupervisorClient(short_root)
            await client.ensure_running()
            client._RENEW_EVERY_S = 0.6  # first renew lands AFTER the lease lapses
            tok1 = await client.acquire(ttl_s=0.2)
            await asyncio.sleep(1.5)  # lapse -> stale RENEW -> re-ACQUIRE cycles
            tok2 = client.token
            listing = await client.list_runs()
            await client.release()
            await client.close()
            return tok1, tok2, listing

        tok1, tok2, listing = asyncio.run(scenario())
        assert tok2 is not None and tok2 > tok1  # renewal survived the lapse
        assert listing["ok"] is True  # the client is still usable
    finally:
        teardown_supervisor(short_root, proc)


def _spec(run_dir: Path, command: str, grace: float = 2.0) -> dict:
    import uuid

    return {
        "version": 1,
        "run_id": str(uuid.uuid4()),
        "job": run_dir.name.rsplit(".", 1)[0],
        "run_number": int(run_dir.name.rsplit(".", 1)[1]),
        "command": command,
        "run_dir": str(run_dir),
        "stdout_path": str(run_dir / "out.log"),
        "stderr_path": str(run_dir / "err.log"),
        "stdin_path": None,
        "grace_seconds": grace,
    }


# ----------------------------------------------------------- integration kill


CATALOG = lower_source("insert_job: slow\njob_type: c\ncommand: sleep 3; exit 0\n")


def _adapters(client: SupervisorClient) -> dict:
    return {
        "CMD": SupervisedCommandAdapter(client, grace_seconds=2.0, settle_seconds=1.0),
        "FW": FileWatcherAdapter(),
    }


async def _resume_and_finish(run_root: Path) -> tuple[str, list]:
    client = SupervisorClient(run_root)
    await client.ensure_running()
    await client.acquire()
    engine = await resume_run(
        CATALOG,
        run_root,
        clock=RealClock(),
        adapters=_adapters(client),
        supervisor=client,
        settle_seconds=1.0,
        grace_seconds=2.0,
    )
    await engine.run_until_quiescent(datetime.max)
    await engine.shutdown()
    status = engine.oracle.store.job["slow"].status
    if engine.journal is not None:
        engine.journal.close()
    records = read_journal(run_root / "journal.jsonl")
    reconcile = [r for r in records if r.get("rec") == "input" and r.get("source") == "reconcile"]
    with contextlib.suppress(Exception):
        await client.shutdown()
    await client.close()
    return status, reconcile


def test_sigkill_engine_detached_survives_and_reattaches(short_root: Path) -> None:
    """spec ss5 item 1: SIGKILL the detached ENGINE mid-run; the command
    SURVIVES (its parent is the supervisor), and resume REATTACHES with no
    reconciliation injection and the job's true exit code."""
    run_root = short_root / "run"
    driver = subprocess.Popen(
        [sys.executable, str(DRIVER), str(run_root), "3"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert driver.stdout is not None
        assert driver.stdout.readline().strip() == "DRIVER-READY"
        spawn_path = run_root / "runs" / "slow.1" / "spawn.json"
        spawn = json.loads(wait_for(lambda: spawn_path.exists() and spawn_path.read_text()))
        os.kill(driver.pid, signal.SIGKILL)  # -9 the engine ONLY
        driver.wait()
        # the command survives its engine's death (the supervisor holds the tether)
        assert pid_alive(spawn["command_pid"])
        status, reconcile = asyncio.run(_resume_and_finish(run_root))
        assert status == "SUCCESS"  # reattached, ran to completion, real exit 0
        assert reconcile == []  # REATTACH injects nothing (the run never stopped)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
        _kill_group(run_root)


def test_detach_stop_sigint_then_resume_reattaches(short_root: Path) -> None:
    """spec ss5 item 4: SIGINT the detached engine -> orderly detach-stop, the
    job keeps running -> resume reattaches -> SUCCESS with the real exit code."""
    run_root = short_root / "run"
    driver = subprocess.Popen(
        [sys.executable, str(DRIVER), str(run_root), "3"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert driver.stdout is not None
        assert driver.stdout.readline().strip() == "DRIVER-READY"
        spawn_path = run_root / "runs" / "slow.1" / "spawn.json"
        spawn = json.loads(wait_for(lambda: spawn_path.exists() and spawn_path.read_text()))
        os.kill(driver.pid, signal.SIGINT)  # graceful detach-stop
        driver.wait(timeout=10)
        assert driver.returncode == 0
        assert pid_alive(spawn["command_pid"])  # detach-stop signals NOTHING
        status, reconcile = asyncio.run(_resume_and_finish(run_root))
        assert status == "SUCCESS"
        assert reconcile == []
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
        _kill_group(run_root)


def test_kill_supervisor_midrun_engine_resolves_via_spool(short_root: Path) -> None:
    """spec ss5 item 2: kill -9 the SUPERVISOR mid-run -> wrappers EOF ->
    status.json terminated/parent-lost -> the (still-alive) engine resolves via
    the spool ladder to TERMINATED and survives the socket loss."""

    async def scenario() -> tuple[str, dict]:
        run_root = short_root / "run"
        run_root.mkdir(parents=True)
        client = SupervisorClient(run_root)
        await client.ensure_running()
        await client.acquire()
        catalog = lower_source("insert_job: slow\njob_type: c\ncommand: sleep 60\n")
        engine = __import__("dsl41.runner", fromlist=["start_run"]).start_run(
            catalog, run_root, clock=RealClock(), adapters=_adapters(client)
        )
        from dsl41.oracle import Event

        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "slow"}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        spawn_path = run_root / "runs" / "slow.1" / "spawn.json"

        async def await_file() -> dict:
            while not spawn_path.exists():
                await asyncio.sleep(0.05)
            return json.loads(spawn_path.read_text())

        spawn = await await_file()
        sup_pid = json.loads((run_root / "supervisor.pid").read_text())["pid"]
        os.kill(sup_pid, signal.SIGKILL)  # -9 the SUPERVISOR
        deadline = time.monotonic() + 15
        while engine.oracle.store.job["slow"].status not in ("TERMINATED", "FAILURE", "SUCCESS"):
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
        await engine.shutdown()
        status = engine.oracle.store.job["slow"].status
        if engine.journal is not None:
            engine.journal.close()
        await client.close()
        return status, spawn

    status, spawn = asyncio.run(scenario())
    assert status == "TERMINATED"  # spool ladder read terminated/parent-lost
    # status.json is written BEFORE the wrapper reaps, so the command may
    # still be a zombie (kill(pid, 0) succeeds) for an instant on a slow box
    wait_for(lambda: not pid_alive(spawn["command_pid"]))  # the wrapper killed it on EOF


def test_oracle_kill_detached_terminates(short_root: Path) -> None:
    """spec ss5 item 5: an oracle KILLJOB in detached mode drives TERM->KILL
    through the supervisor -> STATUS TERMINATED, the tethered KILLJOB shape."""

    async def scenario() -> tuple[str, dict]:
        run_root = short_root / "run"
        run_root.mkdir(parents=True)
        client = SupervisorClient(run_root)
        await client.ensure_running()
        await client.acquire()
        catalog = lower_source("insert_job: slow\njob_type: c\ncommand: sleep 60\n")
        from dsl41.oracle import Event
        from dsl41.runner import start_run

        engine = start_run(catalog, run_root, clock=RealClock(), adapters=_adapters(client))
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "slow"}))
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        spawn_path = run_root / "runs" / "slow.1" / "spawn.json"
        while not spawn_path.exists():
            await asyncio.sleep(0.05)
        engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "slow"}))
        deadline = time.monotonic() + 10
        while engine.oracle.store.job["slow"].status != "TERMINATED":
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.05)
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
        await engine.shutdown()
        status = engine.oracle.store.job["slow"].status
        spawn = json.loads(spawn_path.read_text())
        if engine.journal is not None:
            engine.journal.close()
        with contextlib.suppress(Exception):
            await client.shutdown()
        await client.close()
        return status, spawn

    status, spawn = asyncio.run(scenario())
    assert status == "TERMINATED"
    wait_for(lambda: not pid_alive(spawn["command_pid"]))  # zombie until the wrapper reaps


def _kill_group(run_root: Path) -> None:
    runs = run_root / "runs"
    if runs.is_dir():
        for entry in runs.iterdir():
            spawn = entry / "spawn.json"
            if spawn.exists():
                with contextlib.suppress(Exception):
                    pgid = json.loads(spawn.read_text()).get("command_pgid")
                    if isinstance(pgid, int):
                        os.killpg(pgid, signal.SIGKILL)
    sup = run_root / "supervisor.pid"
    if sup.exists():
        with contextlib.suppress(Exception):
            os.kill(json.loads(sup.read_text())["pid"], signal.SIGKILL)
