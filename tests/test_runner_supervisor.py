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

from test_runner_lifecycle import module_imports, procid_import_branches

from dsl41 import canon, runner_procid, runner_supervisor, runner_wrapper
from dsl41.ir import lower_source
from dsl41.runner_startup import resume_run
from dsl41.runner_adapters import (
    FileWatcherAdapter,
    SupervisedCommandAdapter,
    SupervisorClient,
    SupervisorRunRow,
    load_json,
)
from dsl41.runner_clock import RealClock
from dsl41.runner_journal import read_journal
from dsl41.period import active_wal

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


def start_supervisor(
    run_root: Path, env: dict | None = None, deadman_s: float | None = None
) -> subprocess.Popen:
    (run_root / "runs").mkdir(parents=True, exist_ok=True)
    (run_root / "logs").mkdir(exist_ok=True)
    logf = (run_root / "supervisor.log").open("ab")
    argv = [sys.executable, str(SUPERVISOR), "--run-root", str(run_root)]
    if deadman_s is not None:
        argv += ["--deadman-seconds", str(deadman_s)]
    proc = subprocess.Popen(
        argv,
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
        #: DL-80: a real client pairs the incarnation with its token on every
        #: request (SupervisorClient does it in _request), so the raw client
        #: learns it the same way -- from any response that carries one. Tests
        #: that need a WRONG one pass it explicitly and this leaves it alone.
        self.incarnation: str | None = None

    def raw(self, payload: bytes) -> dict:
        self.sock.sendall(payload)
        return self._read()

    def send(self, obj: dict) -> dict:
        if "incarnation" not in obj:
            obj = {**obj, "incarnation": self.incarnation}
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
            if isinstance(obj.get("incarnation"), str) and obj.get("error") is None:
                self.incarnation = obj["incarnation"]
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
    boundary alongside the wrapper -- stdlib only, nothing from dsl41. Its
    non-stdlib RUNTIME imports are the two sibling stdlib-only modules it
    reaches by the DL-72 by-path rule: runner_procid, which it shares with
    the wrapper (that module's own boundary is pinned in
    tests/test_runner_lifecycle.py, whose reader also owns the runtime-vs-
    type-time distinction the next test relies on), and canon, the one
    implementation of the ss3.2 canonical form the DL-129 tombstone files
    are written in -- copying an encoder into this tier would have been a
    second implementation of a byte format audit compares against."""
    non_stdlib = sorted(module_imports(SUPERVISOR) - set(sys.stdlib_module_names))
    assert non_stdlib == ["canon", "runner_procid"], (
        f"supervisor imports outside stdlib: {non_stdlib}"
    )
    assert sorted(module_imports(Path(canon.__file__)) - set(sys.stdlib_module_names)) == [], (
        "canon must stay stdlib-only to be importable inside the tier"
    )


def test_supervisor_procid_calls_are_type_checked() -> None:
    """DL-72's recorded residue, closed here as in the wrapper: the by-path
    import alone left verify_alive and killpg_quiet Any-typed in the file
    that owns the PID-reuse guard and the group kill. The TYPE_CHECKING
    alias types them and is erased at runtime; both halves must import the
    same helpers, or the static types describe an import that never runs."""
    type_time, runtime = procid_import_branches(SUPERVISOR)
    assert type_time == runtime
    assert {"verify_alive", "killpg_quiet"} <= set(runtime)


def test_supervisor_serves_under_pythonsafepath(short_root: Path) -> None:
    """DL-72: run by file path, the supervisor reaches runner_procid through
    its own directory on sys.path[0] -- the entry PYTHONSAFEPATH=1 strips.
    With the guard it binds, PINGs, and shuts down as usual."""
    proc = start_supervisor(short_root, env={**os.environ, "PYTHONSAFEPATH": "1"})
    cli = RawClient(short_root)
    try:
        assert cli.send({"v": 1, "cmd": "PING"})["ok"] is True
        token = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "t"})["token"]
        assert cli.send({"v": 1, "cmd": "SHUTDOWN", "token": token}) == {"ok": True}
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


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
        ping = cli.send({"v": 1, "cmd": "PING"})
        assert ping["ok"] is True and ping["version"] == 1
        assert isinstance(ping["incarnation"], str) and ping["incarnation"]  # DL-80
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
        # DL-79: the incumbent re-keys by presenting its CURRENT token, and
        # gets a strictly greater one. The label alone no longer suffices --
        # see test_live_lease_yields_only_to_the_token_holder below.
        r2 = a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60, "token": 1})
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


def test_live_lease_yields_only_to_the_token_holder(short_root: Path) -> None:
    """DL-79, the multihost fencing hole. Until DL-79 a live lease was handed
    to ANY claimant presenting a matching controller_id, which was safe only
    because one run_root had one engine and the ss10 control-socket bind
    enforced that ON ONE MACHINE. A second host serving the same logical run
    breaks the label: the partitioned OLD leader re-ACQUIREs, mints a higher
    token, and fences out the leader that legitimately took over.

    The rule now: a live lease (unexpired, holder's connection open) yields
    only to a claimant that presents the CURRENT token. The label decides
    nothing -- proved here from both directions."""
    proc = start_supervisor(short_root)
    a = RawClient(short_root)
    b = RawClient(short_root)
    try:
        r1 = a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
        assert r1["ok"] and r1["token"] == 1
        b.send({"v": 1, "cmd": "PING"})  # DL-80: the incarnation is public

        # the impersonator: right label, right incarnation, no token -> refused
        spoof = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
        assert spoof == {
            "ok": False,
            "error": "lease_held",
            "holder": "A",
            "expires_at": r1["expires_at"],
        }
        # right label, STALE token -> still refused (a returning old leader)
        assert (
            b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60, "token": 0})[
                "error"
            ]
            == "lease_held"
        )
        # and A is untouched: its token still mutates
        assert a.send({"v": 1, "cmd": "LIST"})["lease"]["holder"] == "A"

        # the other direction: the CURRENT token re-keys regardless of label,
        # because the token is the only thing the holder alone can have
        r2 = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B", "ttl_s": 60, "token": 1})
        assert r2["ok"] and r2["token"] == 2
        assert a.send({"v": 1, "cmd": "SHUTDOWN", "token": 1}) == {
            "ok": False,
            "error": "stale_token",
        }
    finally:
        a.close()
        b.close()
        teardown_supervisor(short_root, proc)


def test_fencing_survives_a_supervisor_restart_token_reuse(short_root: Path) -> None:
    """DL-80, the cross-incarnation ABA. The fencing counter is in-memory, so a
    restarted supervisor mints token 1 again. DL-79 made the token the
    credential, which turned that reuse into a hole: a controller still holding
    a token 1 from the PREVIOUS incarnation presents it to the new one, matches
    the NEW holder's token 1 by coincidence, and takes the lease away from a
    controller that legitimately owns it.

    The `incarnation` id closes it. Note the two refusals are deliberately
    DIFFERENT: `wrong_incarnation` tells a client its supervisor is gone (all
    its wrappers died by lifeline -- re-acquire and reconcile from the spool),
    while `stale_token` tells it someone else legitimately holds the lease and
    it must NOT re-acquire. Collapsing them into one error loses that."""
    proc = start_supervisor(short_root)
    old = RawClient(short_root)
    try:
        r1 = old.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "old", "ttl_s": 60})
        assert r1["ok"] and r1["token"] == 1
        inc1 = r1["incarnation"]
        old.close()
    finally:
        teardown_supervisor(short_root, proc)

    proc2 = start_supervisor(short_root)
    fresh = RawClient(short_root)
    stale = RawClient(short_root)
    try:
        r2 = fresh.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "fresh", "ttl_s": 60})
        # the counter really did reset -- this is the premise of the hole
        assert r2["ok"] and r2["token"] == 1
        inc2 = r2["incarnation"]
        assert inc2 != inc1

        # the stale controller replays its old (matching!) token
        theft = stale.send(
            {
                "v": 1,
                "cmd": "ACQUIRE",
                "controller_id": "old",
                "ttl_s": 60,
                "token": 1,
                "incarnation": inc1,
            }
        )
        assert theft == {
            "ok": False,
            "error": "lease_held",
            "holder": "fresh",
            "expires_at": r2["expires_at"],
        }
        # and it cannot mutate either -- a distinct error, not stale_token
        spawned = stale.send({"v": 1, "cmd": "LIST"})
        assert spawned["incarnation"] == inc2
        sig = stale.send(
            {
                "v": 1,
                "cmd": "SIGNAL",
                "token": 1,
                "incarnation": inc1,
                "run_id": "whatever",
                "sig": "TERM",
            }
        )
        assert sig == {"ok": False, "error": "wrong_incarnation", "incarnation": inc2}
        # the rightful holder is untouched
        assert fresh.send({"v": 1, "cmd": "RENEW", "token": 1, "incarnation": inc2, "ttl_s": 60})[
            "ok"
        ]
    finally:
        fresh.close()
        stale.close()
        teardown_supervisor(short_root, proc2)


def test_dead_holder_frees_the_lease_without_waiting_out_the_ttl(short_root: Path) -> None:
    """The resume property DL-79 must not break: a crashed engine's lease is
    unexpired for up to ttl_s, and resume has to re-ACQUIRE without waiting it
    out. That no longer comes from a matching controller_id -- it comes from
    the holder's CONNECTION dying, which on this AF_UNIX socket the kernel
    does only when the holder process is gone (kill -9 included). A 60s TTL
    with a 5s budget here: if the orphan path regressed, this cannot pass."""
    proc = start_supervisor(short_root)
    a = RawClient(short_root)
    b = RawClient(short_root)
    try:
        assert a.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["ok"]
        a.close()  # the holder "crashes": the kernel closes its fd
        deadline = time.monotonic() + 5.0
        while True:
            r = b.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "B-resumed", "ttl_s": 60})
            if r.get("ok"):
                break
            assert r["error"] == "lease_held"  # only the EOF has yet to land
            assert time.monotonic() < deadline, "orphaned lease never freed"
            time.sleep(0.05)
        assert r["token"] == 2
    finally:
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


def test_signal_in_the_spawn_window_is_not_ready_not_a_noop(short_root: Path) -> None:
    """DL-83. SPAWN returns once the wrapper is FORKED; the wrapper writes
    spawn.json a few syscalls later. A signal landing in that window used to
    answer {ok, noop} -- indistinguishable from "the group is already gone" --
    so a KILLJOB decided milliseconds after a start was silently dropped and
    the engine recorded TERMINATED for a job that ran to completion.

    The real window is a few milliseconds, so racing it would give a test that
    passes vacuously whenever it loses the race. Instead the window is
    RECREATED deterministically: wait for spawn.json, move it aside, and signal
    while the wrapper is demonstrably alive. That is the exact state the engine
    saw -- live wrapper, no record -- and the answer must be a retryable
    not_ready, never a noop."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "slowspawn.1"
        spawned = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(rd, "sleep 30", grace=0.5)}
        )
        assert spawned["ok"]
        run_id = spawned["run_id"]
        record = rd / "spawn.json"
        wait_for(record.exists)
        saved = record.read_bytes()
        record.unlink()  # re-enter the spawn window, deterministically

        assert _wrapper_alive(cli, tok, run_id), "the discriminator needs a LIVE wrapper"
        r = cli.send({"v": 1, "cmd": "SIGNAL", "token": tok, "run_id": run_id, "sig": "TERM"})
        assert r == {"ok": False, "error": "not_ready"}, "a live wrapper with no record must retry"

        record.write_bytes(saved)  # addressable again: the same call now lands
        assert cli.send(
            {"v": 1, "cmd": "SIGNAL", "token": tok, "run_id": run_id, "sig": "TERM"}
        ) == {"ok": True}
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def _wrapper_alive(cli: "RawClient", tok: int, run_id: str) -> bool:
    for row in cli.send({"v": 1, "cmd": "LIST"})["runs"]:
        if row["run_id"] == run_id:
            return bool(row["wrapper_alive"])
    return False


def test_signal_for_a_dead_wrapper_with_no_record_is_a_noop(short_root: Path) -> None:
    """The other side of DL-83's discriminator: a wrapper that exited without
    ever writing spawn.json left nothing this tier can address, so noop is the
    truthful answer and the caller must not spin retrying."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "gone.1"
        # a spec the wrapper rejects: it exits without a spawn record. The
        # supervisor's own ss2 schema gate (DL-129) must NOT catch it -- a
        # wrong-type field never reaches the wrapper now -- so the rejection
        # is the wrapper's version check, which the schema cannot see past.
        bad = _spec(rd, "true")
        bad["version"] = 999  # spec error -> wrapper exits 2, writes nothing
        spawned = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": bad})
        assert spawned["ok"]
        run_id = spawned["run_id"]
        wait_for(lambda: not (rd / "spawn.json").exists() and _wrapper_done(cli, tok, run_id))
        r = cli.send({"v": 1, "cmd": "SIGNAL", "token": tok, "run_id": run_id, "sig": "TERM"})
        assert r == {"ok": True, "noop": True}
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def _wrapper_done(cli: "RawClient", tok: int, run_id: str) -> bool:
    for row in cli.send({"v": 1, "cmd": "LIST"})["runs"]:
        if row["run_id"] == run_id:
            return row["wrapper_rc"] is not None
    return False


def test_spawn_idempotency_replay(short_root: Path) -> None:
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        rd = short_root / "runs" / "j.1"
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
        # DL-80: the incarnation is PUBLIC (PING/LIST hand it out) -- learn it
        # so this test isolates the TOKEN gate, which is the secret half
        cli.send({"v": 1, "cmd": "PING"})
        # ... and omitting it is its own refusal, not a token failure
        assert (
            cli.send({"v": 1, "cmd": "SPAWN", "spec": _spec(rd, "true"), "incarnation": None})[
                "error"
            ]
            == "wrong_incarnation"
        )
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
                    "boot_id": runner_procid.current_boot_id(),
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


def _supervise_cli(run_root: Path, action: str) -> subprocess.CompletedProcess:
    """The real operator entry point, out of process: `dsl41 supervise ...`.
    Driving the typer callback in-process would miss the transport, which is
    where the DL-80 envelope rule lives."""
    return subprocess.run(
        [sys.executable, "-m", "dsl41", "supervise", action, "--run-root", str(run_root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_dl80_the_supervise_cli_shutdown_carries_the_incarnation(short_root: Path) -> None:
    """DL-151. `dsl41 supervise shutdown` is the one CLI path that MUTATES on
    the ss6a socket, and nothing drove it end to end until this test. DL-80
    put the incarnation beside the token on every mutating verb (frozen ss5);
    `SupervisorConn` stamped only `v`, so the operator verb answered
    wrong_incarnation and exited 2 while the supervisor kept running and its
    wrappers kept their commands alive -- the opposite of what was asked.

    The live shape is the one an operator meets: an engine spawned a command
    and then died, so the lease is unexpired with its holder connection gone
    (freely grantable, frozen ss5), and the CLI must take it, TERM the
    command and end the supervisor."""
    proc = start_supervisor(short_root)
    engine_side = RawClient(short_root)
    rd = short_root / "runs" / "j.1"
    try:
        tok = engine_side.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})[
            "token"
        ]
        engine_side.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(rd, "sleep 30")})
        wait_for(lambda: (rd / "spawn.json").exists())
        # the engine dies. Its lease is unexpired but its connection is gone,
        # so the lease is orphaned and grantable. The supervisor sees the EOF
        # on its next select, long before the CLI's interpreter has started.
        engine_side.close()

        listed = _supervise_cli(short_root, "list")
        assert listed.returncode == 0, listed.stderr
        rows = json.loads(listed.stdout)["runs"]
        assert [r["job"] for r in rows] == ["j"] and rows[0]["wrapper_alive"] is True

        stopped = _supervise_cli(short_root, "shutdown")
        assert "wrong_incarnation" not in stopped.stdout  # the regression itself
        assert json.loads(stopped.stdout) == {"ok": True}
        assert stopped.returncode == 0, stopped.stderr
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert json.loads((rd / "status.json").read_text())["outcome"] == "signaled"
        assert not (short_root / "supervisor.sock").exists()
        assert not (short_root / "supervisor.pid").exists()
    finally:
        with contextlib.suppress(OSError):
            engine_side.close()
        teardown_supervisor(short_root, proc)


def test_dl80_the_blocking_transport_learns_the_incarnation_but_not_from_a_refusal(
    short_root: Path,
) -> None:
    """The incarnation is the supervisor's to name, never the client's to
    invent: `SupervisorConn` reads it back off a reply that carries one, as
    `SupervisorClient` does off ACQUIRE. A REFUSAL also carries it -- to tell
    a stale client what the world is now -- and adopting it there would let a
    client re-send a verb the supervisor just refused, pairing a fresh
    incarnation with a token from a vanished world."""
    from dsl41.runner_adapters import SupervisorConn

    proc = start_supervisor(short_root)
    conn = SupervisorConn(short_root / "supervisor.sock")
    try:
        refusal = conn.send({"cmd": "SHUTDOWN", "token": 1})
        assert refusal["error"] == "wrong_incarnation"
        assert isinstance(refusal["incarnation"], str)
        assert conn.incarnation is None  # a refusal is not a source
        assert conn.send({"cmd": "PING"})["ok"] is True
        assert conn.incarnation == refusal["incarnation"]  # a reply is
        acq = conn.send({"cmd": "ACQUIRE", "controller_id": "cli", "ttl_s": 60})
        assert conn.send({"cmd": "RENEW", "token": acq["token"], "ttl_s": 60})["ok"] is True
    finally:
        conn.close()
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
    lazily reconnect + re-ACQUIRE (current token as incumbency proof, DL-79 -> fresh fencing
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
                # engine's ControlServer learned this as DL-45)
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
    renewal -- the loop re-ACQUIREs (the lapsed lease is free to take) and the
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


def test_dl137_unknown_fields_are_ignored_in_both_directions(short_root: Path) -> None:
    """The tolerant-fields half of the wire's own forward-compatibility rule
    (supervisor-protocol ss5's "ignores unknown fields", the `Supervisor
    socket` row of docs/protocol-evolution.md's table), asked of BOTH ends.

    The request direction is the one a dispatcher test owes, and it is
    driven against the real server here (DL-151): the earlier version of
    this test monkeypatched the client and injected an unknown key into a
    hand-built RESPONSE row, so a server that started refusing unknown
    request fields would have left it green. Every verb is asked, because
    tolerance is the dispatcher's rule and not one handler's.

    The response direction keeps its own assertion, on a row the running
    supervisor actually sent plus the key a newer one might add."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    future = {"from_a_future_controller": "not yet named by this supervisor"}
    try:
        assert cli.send({"v": 1, "cmd": "PING", **future})["ok"] is True
        acquired = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60, **future})
        assert acquired["ok"] is True
        tok = acquired["token"]
        assert cli.send({"v": 1, "cmd": "RENEW", "token": tok, "ttl_s": 60, **future})["ok"] is True

        run_dir = short_root / "runs" / "j.1"
        spawn = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(run_dir, "sleep 30"), **future}
        )
        assert spawn["ok"] is True
        listed = cli.send({"v": 1, "cmd": "LIST", **future})
        assert listed["ok"] is True
        [row] = listed["runs"]
        wait_for(lambda: (run_dir / "spawn.json").exists())  # else SIGNAL is not_ready
        assert (
            cli.send(
                {
                    "v": 1,
                    "cmd": "SIGNAL",
                    "token": tok,
                    "run_id": row["run_id"],
                    "sig": "KILL",
                    **future,
                }
            )["ok"]
            is True
        )

        # the response direction: the row as the supervisor sent it, plus the
        # one key a newer supervisor might add
        parsed = SupervisorRunRow.model_validate({**row, "from_a_future_supervisor": "later"})
        assert parsed.job == "j"
        assert parsed.run_number == 1
        assert parsed.wrapper_alive is True
        assert "from_a_future_supervisor" not in parsed.model_dump()
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


# --------------------------------------- DL-173: the token joins incarnation


def test_dl173_every_mutating_verbs_wire_request_carries_the_current_token(
    short_root: Path,
) -> None:
    """DL-173: the eight hand-spelled `"token": self.token` sites (ACQUIRE's
    three call sites, RENEW, SPAWN, SIGNAL, SHUTDOWN, RELEASE) collapsed into
    one stamp in `_request`, beside `incarnation` (DL-80's existing spot).
    Driven against a fake supervisor that records every request line -- the
    `test_cancelled_request_poisons_and_reconnects` harness -- rather than
    the real subprocess, because the point is the WIRE shape `_request`
    writes, not any handler's reaction to it."""

    async def scenario() -> tuple[list[dict], int, int]:
        seen: list[dict] = []
        next_token = 1

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal next_token
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    req = json.loads(line)
                    seen.append(req)
                    if req.get("cmd") == "ACQUIRE":
                        resp = {
                            "ok": True,
                            "token": next_token,
                            "incarnation": "inc-1",
                            "expires_at": "t",
                        }
                        next_token += 1
                    elif req.get("cmd") == "PING":
                        resp = {
                            "ok": True,
                            "version": 1,
                            "incarnation": "inc-1",
                            "deadman_s": None,
                        }
                    else:
                        resp = {"ok": True}
                    writer.write(json.dumps(resp).encode("utf-8") + b"\n")
                    await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()

        server = await asyncio.start_unix_server(handle, path=str(short_root / "supervisor.sock"))
        client = SupervisorClient(short_root)
        await client.ensure_running()
        tok1 = await client.acquire()
        tok2 = await client.acquire()  # re-ACQUIRE: presents tok1 to prove incumbency
        # RENEW has no public non-loop method; this is the exact shape
        # `_renew_loop` sends (~:1390).
        await client._request({"cmd": "RENEW", "ttl_s": 60})
        await client.spawn({})
        await client.signal("run-x", "TERM")
        await client.shutdown()
        await client.release()
        await client.close()
        server.close()
        await server.wait_closed()
        return seen, tok1, tok2

    seen, tok1, tok2 = asyncio.run(scenario())
    by_cmd: dict[str, list[dict]] = {}
    for req in seen:
        by_cmd.setdefault(req["cmd"], []).append(req)
    acquires = by_cmd["ACQUIRE"]
    assert acquires[0]["token"] is None  # first-ever: nothing to prove incumbency with
    assert acquires[1]["token"] == tok1  # DL-79: re-ACQUIRE presents the current token
    for cmd in ("RENEW", "SPAWN", "SIGNAL", "SHUTDOWN", "RELEASE"):
        assert [r["token"] for r in by_cmd[cmd]] == [tok2], cmd


def test_dl173_ping_carries_token_and_incarnation_and_is_still_answered(
    short_root: Path,
) -> None:
    """PING is a read-only verb: `docs/supervisor-protocol.md` ss5 lists it
    as taking no fields of its own, and `_h_ping` never looks at `_req`.
    DL-173's universal `_request` stamp puts `token` on it anyway, exactly as
    `incarnation` already did (DL-80, unchanged by this slice) -- within
    contract because ss5's own forward-compatibility rule ("the supervisor
    ignores unknown fields") is a REQUEST-side rule, not a per-verb one."""

    async def scenario() -> tuple[dict, dict]:
        seen: list[dict] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    req = json.loads(line)
                    seen.append(req)
                    if req.get("cmd") == "ACQUIRE":
                        resp = {
                            "ok": True,
                            "token": 7,
                            "incarnation": "inc-x",
                            "expires_at": "t",
                        }
                    else:
                        resp = {
                            "ok": True,
                            "version": 1,
                            "incarnation": "inc-x",
                            "deadman_s": None,
                        }
                    writer.write(json.dumps(resp).encode("utf-8") + b"\n")
                    await writer.drain()
            except OSError:
                pass
            finally:
                writer.close()

        server = await asyncio.start_unix_server(handle, path=str(short_root / "supervisor.sock"))
        client = SupervisorClient(short_root)
        await client.ensure_running()
        await client.acquire()
        ping_resp = await client._request({"cmd": "PING"})
        await client.close()
        server.close()
        await server.wait_closed()
        pings = [r for r in seen if r["cmd"] == "PING"]
        return ping_resp, pings[-1]

    ping_resp, wire = asyncio.run(scenario())
    assert ping_resp["ok"] is True  # still answered despite the extra fields
    assert wire["token"] == 7  # DL-173: not just incarnation any more
    assert wire["incarnation"] == "inc-x"  # DL-80, unchanged


# ------------------------------------------------ DL-151: the refusals owed


def test_dl151_bytes_that_are_not_utf8_refuse_rather_than_escape(tmp_path: Path) -> None:
    """ss3.2 has one encoding, so bytes outside it are as unreadable as a
    duplicate key -- but `bytes.decode` raises `UnicodeDecodeError`, which no
    caller's `except CanonError` caught. `canon.decode` is the layer that
    owes the refusal: every reader in the repo turns its `CanonError` into a
    named answer, and none of them had a branch for this one."""
    raw = b'{"artifact_format_version": 1, "run_id": "\xff\xfe"}'
    path = tmp_path / "receipt.json"
    path.write_bytes(raw)

    with pytest.raises(canon.CanonError, match="not UTF-8"):
        canon.decode(raw)
    # both supervisor loaders answer PRESENT-BUT-UNREADABLE, never absence:
    # absence authorizes a spawn (ss11a)
    assert runner_supervisor._load_tombstone(str(path), "receipt") is runner_supervisor._INVALID
    assert runner_supervisor._load_json(str(path)) is runner_supervisor._INVALID


def test_dl151_an_unreadable_index_entry_answers_indeterminate_not_internal(
    short_root: Path,
) -> None:
    """The ss11a table's `indeterminate` row, reached through the invalid-UTF-8
    door. Before the refusal above, the escape hit the dispatcher's belt and
    the operator was told `internal: UnicodeDecodeError` -- a wire answer no
    section of the protocol names."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        spec = _spec(short_root / "runs" / "j.1", "true")
        index = short_root / "runs" / ".by_run_id" / spec["run_id"]
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_bytes(b'{"artifact_format_version": 1, "run_id": "\xff"}')
        answer = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
        assert answer["ok"] is False
        assert answer["error"] == "indeterminate"
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_dl151_an_unsupported_spool_version_is_refused_by_both_readers(tmp_path: Path) -> None:
    """docs/protocol-evolution.md's `Wrapper-owned spool files` row: tolerant
    on fields, STRICT on versions. Nothing read the field before.

    An ABSENT version passes both readers: the matrix has no column for a
    missing version and no document rules one, so refusing it would pick a
    side by guess."""
    path = tmp_path / "status.json"

    def written(record: dict) -> None:
        path.write_text(json.dumps(record))

    written({"version": 1, "outcome": "exited", "exit_code": 0})
    assert runner_supervisor._load_json(str(path)) is not runner_supervisor._INVALID
    assert load_json(path) is not None

    written({"outcome": "exited", "exit_code": 0})  # no version at all
    assert runner_supervisor._load_json(str(path)) is not runner_supervisor._INVALID
    assert load_json(path) is not None

    for foreign in (2, 99, True, 1.0, "1", None):
        written({"version": foreign, "outcome": "exited", "exit_code": 0})
        assert runner_supervisor._load_json(str(path)) is runner_supervisor._INVALID, foreign
        assert load_json(path) is None, foreign


def test_dl151_a_future_status_record_never_becomes_a_success(short_root: Path) -> None:
    """What the version gate buys, said as an outcome: a `status.json` this
    binary cannot read reports `exit_status_unobservable` rather than driving
    the run's verdict from a record whose meaning changed."""
    run_dir = short_root / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps({"version": 2, "run_id": "r", "outcome": "exited", "exit_code": 0})
    )
    from dsl41.runner_adapters import resolve_spool

    outcome, ended = asyncio.run(
        resolve_spool(
            "j", 1, run_dir, runner_procid.current_boot_id(), settle_seconds=0.0, grace_seconds=0.0
        )
    )
    assert "exit_status_unobservable" in str(outcome)
    assert ended is None


def test_dl151_a_nul_in_the_spec_is_bad_spec_not_internal(short_root: Path) -> None:
    """`os.path`, `os.open` and `subprocess.Popen` all raise ValueError -- not
    OSError -- for an embedded null, so a NUL used to reach `realpath` and
    answer `internal:`, or slip past the gate entirely and kill the wrapper
    after the fork with no `status.json` (the E7 absence). Every string field
    of the ss2 spec refuses one."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        for field in ("job", "run_dir", "command", "stdout_path", "stderr_path", "stdin_path"):
            spec = _spec(short_root / "runs" / "j.1", "true")
            spec[field] = f"{spec.get(field) or ''}\x00x"
            answer = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
            assert answer["ok"] is False, field
            assert answer["error"] == "bad_spec", (field, answer)
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_dl151_the_wrapper_records_a_spawn_it_could_not_open(short_root: Path) -> None:
    """The recorder's own belt, under the supervisor's gate. A spec that
    slipped a NUL through -- an older supervisor, a hand-driven wrapper --
    must still leave a record: the absence of `status.json` means the machine
    died, and the wrapper may never be the one to fake that."""
    run_dir = short_root / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    read_fd, write_fd = os.pipe()
    spec = _spec(run_dir, "true")
    spec["stdout_path"] = str(run_dir / "ou\x00t.log")
    spec["lifeline_fd"] = read_fd
    out = subprocess.run(
        [sys.executable, str(Path(runner_wrapper.__file__))],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        pass_fds=(read_fd,),
        check=False,
    )
    os.close(read_fd)
    os.close(write_fd)
    assert out.returncode == 0, out.stderr
    status = json.loads((run_dir / "status.json").read_text())
    assert status["outcome"] == "spawn_failed"
    assert "null" in status["error"]


def test_dl151_grace_seconds_must_be_finite(short_root: Path) -> None:
    """SHUTDOWN escalates TERM->KILL after `grace_seconds`, so an `Infinity`
    made the whole orderly shutdown unbounded -- a supervisor that never
    exits and a run root nothing can reroute. The ss2 gate is where it stops:
    `float("inf") >= 0.0` is True, so the old bound test let it in."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})["token"]
        # json.dumps writes these as the non-standard `Infinity`/`NaN`
        # constants, which is exactly the wire form a client would send
        for grace in (float("inf"), float("-inf"), float("nan")):
            spec = _spec(short_root / "runs" / "j.1", "true")
            spec["grace_seconds"] = grace
            answer = cli.send({"v": 1, "cmd": "SPAWN", "token": tok, "spec": spec})
            assert answer["ok"] is False, grace
            assert answer["error"] == "bad_spec", (grace, answer)
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_dl151_a_bool_is_not_the_version_and_not_a_fencing_token(short_root: Path) -> None:
    """`True == 1` and `1.0 == 1`, so a bare comparison let a JSON `true`
    stand in for protocol version 1 and for fencing token 1. The token is the
    fence that keeps a superseded controller out; nothing may pass as one but
    the integer itself."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        for version in (True, 1.0):
            answer = cli.send({"v": version, "cmd": "PING"})
            assert answer["error"] == "unsupported_version", version
        acquired = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 60})
        assert acquired["token"] == 1
        for token in (True, 1.0):
            answer = cli.send({"v": 1, "cmd": "RENEW", "token": token, "ttl_s": 60})
            assert answer["error"] == "stale_token", token
        # and the incumbency proof on ACQUIRE reads the same way: a `true`
        # token is not the incumbent's, so a second controller is refused
        other = RawClient(short_root)
        try:
            held = other.send(
                {
                    "v": 1,
                    "cmd": "ACQUIRE",
                    "controller_id": "B",
                    "ttl_s": 60,
                    "token": True,
                    "incarnation": cli.incarnation,
                }
            )
            assert held["error"] == "lease_held"
        finally:
            other.close()
        assert cli.send({"v": 1, "cmd": "RENEW", "token": 1, "ttl_s": 60})["ok"] is True
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_dl152_a_tombstone_version_is_the_integer_and_nothing_that_equals_it(
    tmp_path: Path,
) -> None:
    """`True == 1` and `1.0 == 1`, so the tombstone schemas ask the wire-int
    rule, not `==` alone. Two doors close on each: `canon.decode` refuses the
    float literal (no floats on the wire) and the bool version
    (`check_artifact_version`), and the schema refuses either one that reached
    it any other way. The schema half is the one pinned here by hand -- the
    ingress cannot deliver a witness to it."""
    check = runner_supervisor._VERSIONED["artifact_format_version"]
    assert check(canon.ARTIFACT_FORMAT_VERSION) is True
    assert check(True) is False
    assert check(float(canon.ARTIFACT_FORMAT_VERSION)) is False

    # the outer door, on real files: neither literal reaches the schema, and
    # a record that cannot be read is PRESENT-BUT-UNREADABLE, never absent
    path = tmp_path / "receipt.json"
    for literal in (b"1.0", b"true"):
        path.write_bytes(
            b'{"artifact_format_version": '
            + literal
            + b', "run_id": "r", "spec_fingerprint": "f", "received_at": "t"}'
        )
        with pytest.raises(canon.CanonError):
            canon.decode(path.read_bytes())
        assert (
            runner_supervisor._load_tombstone(str(path), "receipt") is runner_supervisor._INVALID
        ), literal


def test_dl151_a_short_write_never_publishes_a_truncated_record(
    tmp_path: Path, monkeypatch
) -> None:
    """The liturgy's first step. `os.write` may write fewer bytes than it was
    given and return the count without raising, so the old body fsynced and
    renamed a TRUNCATED record into place -- a half-written `spawn.json`
    published as the durable one."""
    real_write = os.write
    seen: list[int] = []

    def one_byte_at_a_time(fd: int, data: bytes) -> int:
        seen.append(len(data))
        return real_write(fd, data[:1])

    monkeypatch.setattr(runner_procid.os, "write", one_byte_at_a_time)
    target = tmp_path / "spawn.json"
    payload = b'{"version": 1}\n'
    runner_procid.durable_write(str(target), payload)
    assert target.read_bytes() == payload
    assert len(seen) == len(payload)  # every byte took its own call

    created = tmp_path / "receipt.json"
    runner_procid.durable_create(str(created), payload)
    assert created.read_bytes() == payload


# ------------------------------------------------------------- deadman (ss8)


def _command_pgid(run_dir: Path) -> int:
    wait_for(lambda: (run_dir / "spawn.json").exists())
    return int(json.loads((run_dir / "spawn.json").read_text())["command_pgid"])


def test_cm10_the_deadman_fires_and_takes_its_wrappers_with_it(short_root: Path) -> None:
    """docs/concurrency-model.md ss8. A supervisor that has had no LIVE
    leaseholder for T_deadman exits, and its exit EOFs every lifeline it
    owns -- the kill path ss5 already relies on, not a new one.

    This is what makes `evict` provable rather than assumed: without it,
    nothing bounds how long an unreachable host keeps running work, and the
    leader could never say the old executor is certainly done. The lease
    connection is closed rather than RELEASEd, because that is the shape the
    real failure has: an engine that died, not one that said goodbye."""
    proc = start_supervisor(short_root, deadman_s=1.0)
    cli = RawClient(short_root)
    try:
        tok = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 300})["token"]
        run_dir = short_root / "runs" / "long.1"
        spawned = cli.send(
            {"v": 1, "cmd": "SPAWN", "token": tok, "spec": _spec(run_dir, "sleep 300", grace=0.5)}
        )
        assert spawned["ok"]
        pgid = _command_pgid(run_dir)
        assert pid_alive(pgid)

        # the holder's connection dies. The lease is unexpired -- ttl 300 --
        # so an expiry-only check would wait five minutes; ss5's LIVE lease is
        # unexpired AND connected, and the kernel closes this fd only when the
        # holder process is gone
        cli.close()

        wait_for(lambda: proc.poll() is not None, timeout_s=20)
        wait_for(lambda: not pid_alive(pgid), timeout_s=20)
        assert "deadman fired" in (short_root / "supervisor.log").read_text()
    finally:
        teardown_supervisor(short_root, proc)


def test_a_live_leaseholder_reprieves_the_deadman(short_root: Path) -> None:
    """The contrast that makes the test above non-vacuous: the same interval,
    the same wait, and nothing dies -- because the clock RESTARTS whenever a
    live leaseholder is present. A deadman that fired on a watched supervisor
    would be an outage generator, not a safety property."""
    proc = start_supervisor(short_root, deadman_s=1.0)
    cli = RawClient(short_root)
    try:
        answer = cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 300})
        assert answer["ok"]
        time.sleep(3.0)  # three deadman intervals, connection held open
        assert proc.poll() is None
        assert cli.send({"v": 1, "cmd": "PING"})["deadman_s"] == 1.0
    finally:
        cli.close()
        teardown_supervisor(short_root, proc)


def test_a_supervisor_with_no_deadman_outlives_its_controller(short_root: Path) -> None:
    """ss8: opt-in per run root, and this is what opting out buys. Tolerating
    an absent controller indefinitely is exactly what lets an engine crash and
    resume with its runs intact (DL-79) -- and the price is written into the
    routing row as `deadman_s: null`, which no eviction can get past."""
    proc = start_supervisor(short_root)
    cli = RawClient(short_root)
    try:
        assert cli.send({"v": 1, "cmd": "PING"})["deadman_s"] is None
        cli.send({"v": 1, "cmd": "ACQUIRE", "controller_id": "A", "ttl_s": 1})
        cli.close()
        time.sleep(2.5)  # lease expired AND holder gone: still no deadman
        assert proc.poll() is None
    finally:
        teardown_supervisor(short_root, proc)


def test_a_deadman_interval_must_be_finite_and_positive(short_root: Path) -> None:
    """`nan` and `inf` are the two the bound test could not see (DL-151):
    every comparison against `nan` is False, so `<= 0` passed it and the
    interval then fired on the first tick -- a supervisor that exits at once
    and takes every wrapper with it. `inf` passed the same gate and never
    fired, which is `--deadman-seconds` spelled as no deadman at all."""
    for value in ("0", "-1", "nan", "inf", "-inf"):
        out = subprocess.run(
            # `=` rather than a separate word: argparse reads a leading `-`
            # as the next option name and never reaches this gate
            [
                sys.executable,
                str(SUPERVISOR),
                "--run-root",
                str(short_root),
                f"--deadman-seconds={value}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 2, value
        assert "must be a finite positive number" in out.stderr, value


def _spec(run_dir: Path, command: str, grace: float = 2.0) -> dict:
    """A wrapper input spec for `run_dir`. The directory is NOT created here:
    since DL-129 the supervisor creates a detached run's directory on receipt,
    so a test that made it first would exercise the crash-orphan branch
    instead of the ordinary one."""
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


async def _resume_and_finish(run_root: Path, catalog=None) -> tuple[str, list]:
    client = SupervisorClient(run_root)
    await client.ensure_running()
    await client.acquire()
    engine = await resume_run(
        catalog if catalog is not None else CATALOG,
        run_root,
        clock=RealClock(),
        adapters=_adapters(client),
        supervisor=client,
        settle_seconds=1.0,
        grace_seconds=2.0,
    )
    await engine.run_until_quiescent(datetime.max)
    # what `dsl41 run --detached` does before teardown (spec ss3 case b): a
    # stop must not kill, or every detached shutdown becomes a mass kill. It
    # matters here because a reattached run now honours cancellation (DL-96);
    # before that it ignored it, and this line was invisible.
    engine.detach.stopping = True
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


def _append_records(run_root: Path, records: list[dict]) -> None:
    with active_wal(run_root).open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")


def _decided_kill(run_root: Path, *, with_effect: bool) -> None:
    """Put the log in the state an engine leaves when it decides a kill and
    dies before delivering it (concurrency-model ss5).

    Constructed rather than raced: a live engine cancels the adapter task in
    the same loop turn that applies the input, so the window is microseconds
    wide and a test that raced it would pass vacuously whenever it lost. This
    is the same technique DL-83's spawn-window test uses -- recreate the
    exact state, then assert on the answer.

    Since DL-118 an intent has no record of its own, so the constructed state
    is the admitted input plus the `decision` that planned the kill. The
    `with_effect=False` twin is the SAME decision with an empty `effects`
    list -- identical in every other respect, which is what makes the
    contrast about the recorded intent and nothing else.

    The revisions the decision moved are derived, not guessed: CM-02 is one
    increment per entity per committed input, so the kill takes `job:slow`
    one past wherever the log left it. A wrong number does not pass quietly
    -- replay refuses a decision whose revisions this build cannot
    reproduce."""
    records = read_journal(run_root / "journal.jsonl")
    seq = max(r["seq"] for r in records if "seq" in r) + 1
    at = max(r["at"] for r in records if "at" in r)
    revision = (
        max(
            (
                int(r["revisions"]["job:slow"])
                for r in records
                if r.get("rec") == "decision" and "job:slow" in (r.get("revisions") or {})
            ),
            default=0,
        )
        + 1
    )
    # the id the run's SPAWN bound (DL-118): the real planner looks a KILL's
    # run_id up from the outbox, so the constructed decision must too -- a
    # null one for a bound run is refused as an identity-less intent
    bound = next(
        e["run_id"]
        for r in records
        if r.get("rec") == "decision"
        for e in r.get("effects") or []
        if e["kind"] == "SPAWN" and (e["job"], e["run_number"]) == ("slow", 1)
    )
    effects = (
        [
            {
                "effect_id": f"e{seq}:KILL:slow.1",
                "kind": "KILL",
                "job": "slow",
                "run_number": 1,
                "executor_id": "local",
                "index": seq,
                "at": at,
                # a native decision names identity at birth (DL-118)
                "generation": 0,
                "run_id": bound,
            }
        ]
        if with_effect
        else []
    )
    _append_records(
        run_root,
        [
            {
                "rec": "input",
                "seq": seq,
                "at": at,
                "request_id": f"kill-{seq}",
                "fingerprint": "",  # absent/empty: read_attempts synthesizes it
                "epoch": 0,
                "kind": "KILLJOB",
                "payload": {"job": "slow"},
                "source": "control",
            },
            {
                "rec": "decision",
                "index": seq,
                "request_id": f"kill-{seq}",
                "decision": "applied",
                "reason": None,
                "revisions": {"job:slow": revision},
                "legacy_batch": False,
                "effects": effects,
            },
        ],
    )


async def _resume_and_watch(run_root: Path, command_pid: int) -> bool:
    """Resume, let the loop settle, and answer whether the command SURVIVED.

    Liveness is sampled before any teardown, deliberately. Both teardowns
    here would kill it for reasons that have nothing to do with the question:
    the engine's shutdown cancels live adapter tasks, and the supervisor's
    SHUTDOWN escalates TERM to every command group it holds. A test that
    sampled afterwards would report "the kill was delivered" no matter what
    the outbox did."""
    catalog = lower_source("insert_job: slow\njob_type: c\ncommand: sleep 300; exit 0\n")
    client = SupervisorClient(run_root)
    await client.ensure_running()
    await client.acquire()
    engine = await resume_run(
        catalog,
        run_root,
        clock=RealClock(),
        adapters=_adapters(client),
        supervisor=client,
        settle_seconds=1.0,
        grace_seconds=2.0,
    )
    try:
        await engine.run_until_quiescent(datetime.now())
        deadline = time.monotonic() + 10.0
        while pid_alive(command_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return pid_alive(command_pid)
    finally:
        engine.detach.stopping = True  # a stop must not kill (spec ss3 case b)
        await engine.shutdown()
        if engine.journal is not None:
            engine.journal.close()
        with contextlib.suppress(Exception):
            await client.shutdown()
        await client.close()


def _kill_decided_before_the_crash(short_root: Path, *, with_effect: bool) -> bool:
    """Run the scenario; answer whether the command survived the resume."""
    run_root = short_root / "run"
    driver = subprocess.Popen(
        [sys.executable, str(DRIVER), str(run_root), "300"],
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
        assert pid_alive(spawn["command_pid"]), "the supervisor holds the tether"

        _decided_kill(run_root, with_effect=with_effect)
        return asyncio.run(_resume_and_watch(run_root, spawn["command_pid"]))
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
        _kill_group(run_root)


def test_a_recorded_kill_is_delivered_after_the_engine_that_decided_it_died(
    short_root: Path,
) -> None:
    """concurrency-model ss5, and the leak that made the outbox worth its
    weight (DL-96).

    A kill used to be a `task.cancel()` with no id and no record. An engine
    that decided TERMINATED and died before cancelling left a DETACHED run --
    whose parent is the supervisor, so it survives -- and reconciliation
    walked straight past it, because its job is already TERMINAL, which reads
    as "its completion was already replayed". Nothing looked again and the
    process ran on orphaned.

    With the effect in the log the next engine re-drives it, which is the one
    side effect runner-design ss7 already permits at resume."""
    assert _kill_decided_before_the_crash(short_root, with_effect=True) is False


def test_pr33_without_the_recorded_kill_the_orphan_is_still_re_driven(
    short_root: Path,
) -> None:
    """The same estate minus the one entry that says a kill was meant --
    and since DL-133 the process does NOT survive it.

    This assertion was `is True` when the outbox was the only route: with
    no recorded intent, nothing at resume had a reason to look at a live
    wrapper again. period-model ss3.5 closes that as PR-33 -- **a live
    wrapper under a TERMINAL row is re-driven regardless of the KILL
    effect's recorded state**, and regardless of whether one exists -- and
    it is the same leak from the other side: `_apply_kill` records
    `applied` before its TERM/grace/KILL ladder runs, so an engine that
    dies mid-ladder leaves exactly this state with the effect already
    resolved. The test above stays as the recorded-intent route; this one
    now pins the route that needs no record."""
    assert _kill_decided_before_the_crash(short_root, with_effect=False) is False


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
        engine = __import__("dsl41.runner_startup", fromlist=["start_run"]).start_run(
            catalog, run_root, clock=RealClock(), adapters=_adapters(client)
        )
        from dsl41.oracle_state import Event

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
        from dsl41.oracle_state import Event
        from dsl41.runner_startup import start_run

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
