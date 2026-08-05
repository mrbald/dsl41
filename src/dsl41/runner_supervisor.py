"""Supervisor: the Tier-1 availability process (phase 11f).

Normative spec: docs/runner-design.md ss6a (Tier 1) + docs/supervisor-protocol.md
ss5 (the socket protocol this module freezes) + DL-41a/DL-42/DL-48. STDLIB ONLY:
this module imports nothing from dsl41 and nothing third-party -- the same
enforced extraction boundary as runner_wrapper.py (DL-42; import-graph test in
tests/test_runner_supervisor.py). The engine runs it BY FILE PATH
(``sys.executable <this file> --run-root <root>``), never ``-m``: ``-m`` would
import the dsl41 package __init__ and drag third-party imports into the
supervisor's runtime.

Why this process exists (ss6a Tier 1): the wrapper (Tier 0) makes exit status
survive engine downtime, but a tethered engine still KILLS its jobs when it
dies (the wrapper's lifeline EOFs). Long-running estates need the opposite:
an engine restart (upgrade) must NOT kill active work. The supervisor owns the
wrappers' lifelines, so it -- not the engine -- is what the jobs are tethered
to. The engine connects, ACQUIREs a lease, SPAWNs through the supervisor, and
on restart REATTACHES: the E4 orphan-adoption problem dissolves because the
jobs' parent never died (DL-41a item 8).

It is deliberately DUMB (postmaster / s6-supervise philosophy): SPAWN, SIGNAL,
LIST, SHUTDOWN, PING, and the lease verbs -- fork wrappers, reap them, forward
exit notifications. No timers, no conditions, no policy; the oracle decides
kills, the supervisor just relays one signal per SIGNAL call. Near-zero own-bug
crash surface. Surviving ITS OWN death is Tier 2's job (init system) -- the
supervisor never restarts itself.

Protocol (frozen in docs/supervisor-protocol.md ss5): JSON lines over a named
SOCK_STREAM unix socket (0600 + same-uid peer-cred check on every accept).
One request line -> one response line, except async exit PUSHES to the
lease-holding connection. Read-only verbs (LIST/PING) need no lease; mutating
verbs (SPAWN/SIGNAL/SHUTDOWN) carry a monotonic fencing token from ACQUIRE.

Linux hardening: PR_SET_CHILD_SUBREAPER (prctl 36) so a killed wrapper's
command reparents to the supervisor for reaping rather than to init.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

PROTOCOL_VERSION = 1

#: the Tier-0 wrapper, a sibling module run by file path (never -m). Resolved
#: relative to THIS file so the supervisor never imports dsl41 to find it.
_WRAPPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_wrapper.py")


# ------------------------------------------------------------------ durability
# (copied from runner_wrapper, not imported: the stdlib-only boundary forbids a
# dsl41 import, and the two modules share the DL-41a durability liturgy)


def durable_write(path: str, data: bytes) -> None:
    """Same-dir temp file, fsync(file), rename, fsync(directory). Local FS."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ------------------------------------------------------------ machine identity
# (copied from runner_wrapper for the same stdlib-only reason: the SIGNAL
# PID-reuse guard reimplements verify_alive here)


def current_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        pass
    out = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return "unknown"


def proc_start_token(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("ascii", "replace")
        except OSError:
            return None
        fields = raw.rsplit(")", 1)[1].split()
        return f"ticks:{fields[19]}"
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    lstart = out.stdout.strip()
    if out.returncode != 0 or not lstart:
        return None
    return f"lstart:{lstart}"


_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def start_tokens_match(a: str, b: str, *, tolerance_s: float = 2.0) -> bool:
    if a.startswith("ticks:") or b.startswith("ticks:"):
        return a == b
    if not (a.startswith("lstart:") and b.startswith("lstart:")):
        return False
    try:
        ta = time.mktime(time.strptime(a[len("lstart:") :], _LSTART_FORMAT))
        tb = time.mktime(time.strptime(b[len("lstart:") :], _LSTART_FORMAT))
    except ValueError:
        return False
    return abs(ta - tb) <= tolerance_s


def verify_alive(pid: int, recorded_token: str) -> bool:
    """The PID-reuse guard: a pid is only 'ours' if it exists AND its start
    time matches the recorded token. Never signal a pid that fails this."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    token = proc_start_token(pid)
    return token is not None and start_tokens_match(token, recorded_token)


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------- peer identity


def peer_uid(sock: socket.socket) -> int | None:
    """Same-uid gate input: the connecting peer's uid, or None where the
    platform exposes no credential. Linux SO_PEERCRED (struct ucred); macOS
    LOCAL_PEERCRED (struct xucred, cr_uid at offset 4)."""
    if hasattr(socket, "SO_PEERCRED"):  # Linux
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
        _pid, uid, _gid = struct.unpack("iII", data)
        return uid
    if sys.platform == "darwin":
        sol_local, local_peercred = 0, 0x001
        # struct xucred: u_int cr_version; uid_t cr_uid; short cr_ngroups;
        # gid_t cr_groups[16] -> 76 bytes; cr_uid is the second u_int
        raw = sock.getsockopt(sol_local, local_peercred, 76)
        _version, cr_uid = struct.unpack("=II", raw[:8])
        return cr_uid
    return None  # pragma: no cover -- POSIX-only tier


# ------------------------------------------------------------------ the daemon


class _Run:
    __slots__ = (
        "run_id",
        "job",
        "run_number",
        "run_dir",
        "wrapper_pid",
        "lifeline_w",
        "spawned_at",
        "wrapper_rc",
        "grace_seconds",
        "killed",
    )

    def __init__(
        self,
        *,
        run_id: str,
        job: str,
        run_number: int,
        run_dir: str,
        wrapper_pid: int,
        lifeline_w: int,
        spawned_at: str,
        grace_seconds: float,
    ) -> None:
        self.run_id = run_id
        self.job = job
        self.run_number = run_number
        self.run_dir = run_dir
        self.wrapper_pid = wrapper_pid
        self.lifeline_w = lifeline_w
        self.spawned_at = spawned_at
        self.grace_seconds = grace_seconds
        self.wrapper_rc: int | None = None
        self.killed = False  # a KILL escalation was sent (SHUTDOWN bookkeeping)


class _Lease:
    __slots__ = ("holder", "token", "deadline", "expires_at", "conn")

    def __init__(
        self, holder: str, token: int, deadline: float, expires_at: str, conn: _Conn | None
    ) -> None:
        self.holder = holder
        self.token = token
        self.deadline = deadline  # time.monotonic() basis (immune to clock steps)
        self.expires_at = expires_at
        self.conn = conn


class _Conn:
    __slots__ = ("sock", "buf")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""


class Supervisor:
    """One supervisor per run_root; owns the socket, the wrapper lifelines, and
    the single-controller lease. Single-threaded selectors loop (SIGCHLD
    self-pipe, listen socket, client sockets) -- the same select+self-pipe
    shape the wrapper uses, so no thread-safety surface."""

    def __init__(self, run_root: str) -> None:
        self.run_root = run_root
        self.sock_path = os.path.join(run_root, "supervisor.sock")
        self.pid_path = os.path.join(run_root, "supervisor.pid")
        self.boot_id = current_boot_id()
        self.runs: dict[str, _Run] = {}
        self.lease: _Lease | None = None
        self._next_token = 1  # monotonic fencing counter (in-memory; ss5)
        self._conns: dict[int, _Conn] = {}
        self._sel = selectors.DefaultSelector()
        self._listen: socket.socket | None = None
        self._chld_r, self._chld_w = os.pipe()
        os.set_blocking(self._chld_r, False)
        os.set_blocking(self._chld_w, False)
        self._running = True
        self._shutdown_requested = False

    # -- startup ------------------------------------------------------------

    def _set_subreaper(self) -> None:
        """Linux PR_SET_CHILD_SUBREAPER (prctl 36, 1): a killed wrapper's
        command reparents to us for reaping, not to init. Best-effort; a
        no-op everywhere else (ss6a)."""
        if not sys.platform.startswith("linux"):
            return
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
        except (OSError, AttributeError):
            pass

    def _refuse_if_live(self) -> None:
        """Parity with the engine's control-socket gate (ss10): a connect that
        succeeds means a live supervisor already serves this run_root -- refuse;
        a refused/absent socket is a crashed run's leftover -- unlink."""
        if not os.path.exists(self.sock_path):
            return
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(0.2)
        try:
            probe.connect(self.sock_path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(self.sock_path)
        else:
            probe.close()
            raise SystemExit(f"supervisor: {self.sock_path} is live; another supervisor serves it")
        finally:
            probe.close()

    def _bind(self) -> None:
        self._refuse_if_live()
        old_umask = os.umask(0o177)  # 0600 from birth
        try:
            self._listen = socket.socket(socket.AF_UNIX)
            self._listen.bind(self.sock_path)
            self._listen.listen(64)
        finally:
            os.umask(old_umask)
        os.chmod(self.sock_path, 0o600)  # belt: some platforms ignore umask on bind
        self._listen.setblocking(False)
        durable_write(
            self.pid_path,
            json.dumps(
                {"pid": os.getpid(), "boot_id": self.boot_id, "started_at": _utc_now_iso()},
                sort_keys=True,
            ).encode("utf-8")
            + b"\n",
        )

    def _install_signals(self) -> None:
        signal.signal(signal.SIGCHLD, self._on_chld_signal)
        # init (Tier 2) or `supervise shutdown` fallback may TERM us: shut down
        # orderly so wrappers record signaled/exited, never parent-lost. Only
        # SIGKILL (unhandleable) leaves wrappers to their own lifeline EOF.
        signal.signal(signal.SIGTERM, self._on_term_signal)
        signal.signal(signal.SIGINT, self._on_term_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def _on_chld_signal(self, _signum: int, _frame: object) -> None:
        try:
            os.write(self._chld_w, b"c")
        except OSError:
            pass

    def _on_term_signal(self, _signum: int, _frame: object) -> None:
        self._shutdown_requested = True
        try:
            os.write(self._chld_w, b"t")
        except OSError:
            pass

    # -- the loop -----------------------------------------------------------

    def run(self) -> int:
        self._set_subreaper()
        self._bind()
        self._install_signals()
        assert self._listen is not None
        self._sel.register(self._listen, selectors.EVENT_READ, ("listen", None))
        self._sel.register(self._chld_r, selectors.EVENT_READ, ("chld", None))
        try:
            while self._running:
                for key, _mask in self._sel.select(timeout=1.0):
                    tag, payload = key.data
                    if tag == "listen":
                        self._accept()
                    elif tag == "chld":
                        self._drain_chld()
                        self._reap()
                        if self._shutdown_requested:
                            self._orderly_shutdown()
                    elif tag == "conn":
                        self._readable(payload)
                # a stray SIGCHLD can be coalesced away by the self-pipe under
                # load; the select timeout gives an unconditional reap tick
                self._reap()
                if self._shutdown_requested:
                    self._orderly_shutdown()
        finally:
            self._teardown()
        return 0

    def _accept(self) -> None:
        assert self._listen is not None
        try:
            conn_sock, _ = self._listen.accept()
        except OSError:
            return
        uid = peer_uid(conn_sock)
        if uid is not None and uid != os.getuid():
            conn_sock.close()  # same-uid gate (ss1)
            return
        conn_sock.setblocking(True)  # small line writes; blocking is simplest
        conn = _Conn(conn_sock)
        self._conns[conn_sock.fileno()] = conn
        self._sel.register(conn_sock, selectors.EVENT_READ, ("conn", conn))

    def _readable(self, conn: _Conn) -> None:
        try:
            chunk = conn.sock.recv(65536)
        except OSError:
            self._drop_conn(conn)
            return
        if not chunk:
            self._drop_conn(conn)
            return
        conn.buf += chunk
        while b"\n" in conn.buf:
            line, conn.buf = conn.buf.split(b"\n", 1)
            self._dispatch(conn, line)
            if not self._running:  # SHUTDOWN reply already sent
                return

    def _drop_conn(self, conn: _Conn) -> None:
        with contextlib.suppress(KeyError):
            self._sel.unregister(conn.sock)
        self._conns.pop(conn.sock.fileno(), None)
        if self.lease is not None and self.lease.conn is conn:
            self.lease.conn = None  # pushes drop until the holder re-ACQUIREs
        conn.sock.close()

    @staticmethod
    def _send(conn: _Conn, obj: dict[str, Any]) -> None:
        try:
            conn.sock.sendall(json.dumps(obj, sort_keys=True).encode("utf-8") + b"\n")
        except OSError:
            pass  # a client that hung up mid-write is its own problem

    # -- request dispatch ---------------------------------------------------

    def _dispatch(self, conn: _Conn, line: bytes) -> None:
        if not line.strip():
            return
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (json.JSONDecodeError, ValueError):
            self._send(conn, {"ok": False, "error": "malformed_json"})
            return
        if req.get("v") != PROTOCOL_VERSION:
            self._send(conn, {"ok": False, "error": "unsupported_version"})
            return
        cmd = req.get("cmd")
        handler = {
            "PING": self._h_ping,
            "LIST": self._h_list,
            "ACQUIRE": self._h_acquire,
            "RENEW": self._h_renew,
            "RELEASE": self._h_release,
            "SPAWN": self._h_spawn,
            "SIGNAL": self._h_signal,
            "SHUTDOWN": self._h_shutdown,
        }.get(cmd if isinstance(cmd, str) else "")
        if handler is None:
            self._send(conn, {"ok": False, "error": "unknown_verb"})
            return
        self._send(conn, handler(conn, req))

    def _h_ping(self, _conn: _Conn, _req: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "version": PROTOCOL_VERSION}

    def _h_list(self, _conn: _Conn, _req: dict[str, Any]) -> dict[str, Any]:
        lease = None
        if self._lease_active():
            assert self.lease is not None
            lease = {"holder": self.lease.holder, "expires_at": self.lease.expires_at}
        return {
            "ok": True,
            "version": PROTOCOL_VERSION,
            "supervisor_pid": os.getpid(),
            "boot_id": self.boot_id,
            "lease": lease,
            "runs": [
                {
                    "run_id": r.run_id,
                    "job": r.job,
                    "run_number": r.run_number,
                    "run_dir": r.run_dir,
                    "wrapper_pid": r.wrapper_pid,
                    "wrapper_alive": r.wrapper_rc is None,
                    "spawned_at": r.spawned_at,
                    "wrapper_rc": r.wrapper_rc,
                }
                for r in self.runs.values()
            ],
        }

    # -- lease --------------------------------------------------------------

    def _lease_active(self) -> bool:
        return self.lease is not None and time.monotonic() < self.lease.deadline

    def _check_token(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Every mutating verb: the token must match a live lease. A stale or
        expired token is refused so a fenced-out controller cannot mutate."""
        if not self._lease_active() or self.lease is None or self.lease.token != req.get("token"):
            return {"ok": False, "error": "stale_token"}
        return None

    def _h_acquire(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        controller_id = req.get("controller_id")
        if not isinstance(controller_id, str) or not controller_id:
            return {"ok": False, "error": "bad_controller_id"}
        ttl_s = float(req.get("ttl_s", 60))
        if self._lease_active():
            assert self.lease is not None
            if self.lease.holder != controller_id:
                return {
                    "ok": False,
                    "error": "lease_held",
                    "holder": self.lease.holder,
                    "expires_at": self.lease.expires_at,
                }
        token = self._next_token
        self._next_token += 1  # monotonic: never regresses while any run is alive
        expires_at = datetime.fromtimestamp(time.time() + ttl_s, UTC).isoformat()
        self.lease = _Lease(controller_id, token, time.monotonic() + ttl_s, expires_at, conn)
        return {"ok": True, "token": token, "expires_at": expires_at}

    def _h_renew(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_token(req)) is not None:
            return err
        assert self.lease is not None
        ttl_s = float(req.get("ttl_s", 60))
        self.lease.deadline = time.monotonic() + ttl_s
        self.lease.expires_at = datetime.fromtimestamp(time.time() + ttl_s, UTC).isoformat()
        return {"ok": True, "expires_at": self.lease.expires_at}

    def _h_release(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_token(req)) is not None:
            return err
        self.lease = None
        return {"ok": True}

    # -- spawn / signal -----------------------------------------------------

    def _h_spawn(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_token(req)) is not None:
            return err
        spec = req.get("spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("run_id"), str):
            return {"ok": False, "error": "bad_spec"}
        run_id = spec["run_id"]
        if run_id in self.runs:  # idempotency: run_id is the key
            r = self.runs[run_id]
            return {
                "ok": True,
                "run_id": run_id,
                "wrapper_pid": r.wrapper_pid,
                "spawned_at": r.spawned_at,
                "duplicate": True,
            }
        try:
            wrapper_pid, lifeline_w = self._spawn_wrapper(spec)
        except OSError as exc:
            return {"ok": False, "error": f"spawn_failed: {exc}"}
        spawned_at = _utc_now_iso()
        self.runs[run_id] = _Run(
            run_id=run_id,
            job=str(spec.get("job")),
            run_number=int(spec.get("run_number", 0)),
            run_dir=str(spec.get("run_dir")),
            wrapper_pid=wrapper_pid,
            lifeline_w=lifeline_w,
            spawned_at=spawned_at,
            grace_seconds=float(spec.get("grace_seconds", 10.0)),
        )
        return {"ok": True, "run_id": run_id, "wrapper_pid": wrapper_pid, "spawned_at": spawned_at}

    def _spawn_wrapper(self, spec: dict[str, Any]) -> tuple[int, int]:
        """Fork the wrapper by file path with the lifeline WRITE END kept here
        only (the ss6a fd-hygiene invariant, now anchored in the supervisor).
        posix_spawn -- not subprocess.Popen -- so the global waitpid(-1) reaper
        never fights Popen's own bookkeeping."""
        lifeline_r, lifeline_w = os.pipe()
        os.set_inheritable(lifeline_r, True)  # the wrapper inherits it as lifeline_fd
        stdin_r, stdin_w = os.pipe()
        wrapper_spec = {**spec, "lifeline_fd": lifeline_r}
        try:
            pid = os.posix_spawn(
                sys.executable,
                [sys.executable, _WRAPPER_PATH],
                dict(os.environ),
                file_actions=[(os.POSIX_SPAWN_DUP2, stdin_r, 0)],
            )
        finally:
            os.close(lifeline_r)  # our copy; the wrapper holds its own now
            os.close(stdin_r)
        try:
            os.write(stdin_w, json.dumps(wrapper_spec).encode("utf-8"))
        finally:
            os.close(stdin_w)  # EOF: the wrapper repoints stdin at /dev/null after
        return pid, lifeline_w

    def _h_signal(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_token(req)) is not None:
            return err
        run_id = req.get("run_id")
        sig_name = req.get("sig")
        sig = (
            {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}.get(sig_name)
            if isinstance(sig_name, str)
            else None
        )
        if sig is None:
            return {"ok": False, "error": "bad_signal"}
        run = self.runs.get(run_id) if isinstance(run_id, str) else None
        if run is None:
            return {"ok": False, "error": "unknown_run"}
        if self._signal_command(run, sig):
            return {"ok": True}
        return {"ok": True, "noop": True}  # already-dead / unverifiable group

    def _signal_command(self, run: _Run, sig: int) -> bool:
        """Signal the recorded command PGID -- never the wrapper (the recorder
        is untouchable) -- after the (pid, start-time) PID-reuse guard. Returns
        False (noop) when spawn.json is absent or the group is already gone."""
        spawn = _load_json(os.path.join(run.run_dir, "spawn.json"))
        if spawn is None:
            return False
        if not (spawn.get("job") == run.job and spawn.get("run_number") == run.run_number):
            return False  # spoofed/corrupt spawn record: never trust, never signal
        pid = spawn.get("command_pid")
        pgid = spawn.get("command_pgid")
        token = spawn.get("command_start_time")
        if not (isinstance(pid, int) and isinstance(pgid, int) and isinstance(token, str)):
            return False
        if not verify_alive(pid, token):  # the PID-reuse guard
            return False
        _killpg_quiet(pgid, sig)
        return True

    # -- shutdown -----------------------------------------------------------

    def _h_shutdown(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._check_token(req)) is not None:
            return err
        # ss5 order: wait for wrappers FIRST, reply {ok}, exit, unlink -- the
        # earlier reply-then-teardown also double-sent {ok} (review fix, DL-48)
        self._orderly_shutdown()
        return {"ok": True}

    def _orderly_shutdown(self) -> None:
        """The one place the supervisor escalates TERM->KILL (the engine may be
        gone). Lifelines stay OPEN until each wrapper exits, so wrappers observe
        the command deaths and record signaled/exited -- never parent-lost."""
        self._shutdown_requested = False
        live = [r for r in self.runs.values() if r.wrapper_rc is None]
        # a JUST-spawned wrapper may not have written spawn.json yet, and
        # _signal_command is a silent no-op without it -- the wrapper would
        # then die only by lifeline EOF at our exit and record "parent lost".
        # Wait briefly for the missing records first (review fix, DL-48; a
        # bounded wait, not policy -- same shape as the engine-side spawn wait).
        spawn_deadline = time.monotonic() + 5.0
        while time.monotonic() < spawn_deadline:
            self._reap()
            if all(
                r.wrapper_rc is not None
                or _load_json(os.path.join(r.run_dir, "spawn.json")) is not None
                for r in live
            ):
                break
            time.sleep(0.05)
        live = [r for r in self.runs.values() if r.wrapper_rc is None]
        term_at = time.monotonic()
        for run in live:
            self._signal_command(run, signal.SIGTERM)
        deadline = term_at + max((r.grace_seconds for r in live), default=0.0) + 2.0
        while True:
            self._reap()
            remaining = [r for r in self.runs.values() if r.wrapper_rc is None]
            if not remaining:
                break
            now = time.monotonic()
            for run in remaining:
                if not run.killed and now - term_at >= run.grace_seconds:
                    self._signal_command(run, signal.SIGKILL)
                    run.killed = True
            if now > deadline:  # last resort: KILL every survivor's group, then reap
                for run in remaining:
                    self._signal_command(run, signal.SIGKILL)
                self._reap()
                break
            time.sleep(0.02)
        self._running = False

    # -- reaping ------------------------------------------------------------

    def _drain_chld(self) -> None:
        try:
            while os.read(self._chld_r, 4096):
                pass
        except BlockingIOError:
            pass

    def _reap(self) -> None:
        """Reap every exited wrapper, close its lifeline, and push the exit to
        the lease-holding connection (a notification only -- droppable)."""
        by_pid = {r.wrapper_pid: r for r in self.runs.values() if r.wrapper_rc is None}
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            run = by_pid.get(pid)
            if run is None:
                continue  # a reparented grandchild (subreaper), not a wrapper
            run.wrapper_rc = os.waitstatus_to_exitcode(status)
            with contextlib.suppress(OSError):
                os.close(run.lifeline_w)
            self._push_exit(run)

    def _push_exit(self, run: _Run) -> None:
        if self.lease is None or self.lease.conn is None or not self._lease_active():
            return
        self._send(
            self.lease.conn,
            {
                "push": "exit",
                "run_id": run.run_id,
                "wrapper_rc": run.wrapper_rc,
                "at": _utc_now_iso(),
            },
        )

    def _teardown(self) -> None:
        for conn in list(self._conns.values()):
            with contextlib.suppress(Exception):
                conn.sock.close()
        if self._listen is not None:
            with contextlib.suppress(Exception):
                self._listen.close()
        with contextlib.suppress(OSError):
            os.unlink(self.sock_path)
        with contextlib.suppress(OSError):
            os.unlink(self.pid_path)


def _load_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner_supervisor")
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    # ENOENT on the run_root is a caller bug (the engine makes it first)
    if not os.path.isdir(args.run_root):
        print(f"supervisor: run-root {args.run_root!r} does not exist", file=sys.stderr)
        return 2
    try:
        return Supervisor(args.run_root).run()
    except SystemExit as exc:  # the live-supervisor gate
        print(str(exc), file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
