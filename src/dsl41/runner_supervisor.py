"""Supervisor: the Tier-1 availability process (phase 11f).

Normative spec: docs/runner-design.md ss6a (Tier 1) + docs/supervisor-protocol.md
ss5 (the socket protocol this module freezes) + DL-41a/DL-42/DL-48. STDLIB ONLY:
this module imports nothing from dsl41 and nothing third-party -- the same
enforced extraction boundary as runner_wrapper.py (DL-42; import-graph test in
tests/test_runner_supervisor.py), its only non-stdlib imports being two sibling
stdlib-only modules under the same by-path rule (DL-72): runner_procid, which
the wrapper shares, and canon, which is the one implementation of the ss3.2
canonical form the ss11a tombstone files are written in. The engine runs it BY
FILE PATH (``sys.executable <this file> --run-root <root>``), never ``-m``:
``-m`` would import the dsl41 package __init__ and drag third-party imports
into the supervisor's runtime.

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

SPAWN IDEMPOTENCY IS DIRECTORY-BACKED (period-model ss11a, DL-129). The key
is `run_id`, and the store is the run directory rather than `self.runs`: a
never-rolling estate needs LIST to stay bounded, and the moment a completed
entry leaves memory an in-memory dedup turns a delayed duplicate SPAWN into a
second execution. So a DETACHED run's directory is created HERE, on receipt,
and three files make the tombstone: the `runs/.by_run_id/<run_id>` index (the
first durable thing that names the run), `receipt.json` (written BEFORE the
wrapper is forked), and `reply.json` (the answer as first given). A replay
resolves through the index, never through the incoming path, and answers from
the directory, not from memory.

The DEADMAN (stage S5b, DL-95; docs/concurrency-model.md ss8) is the one
thing here that is not purely reactive, and it is deliberately the smallest
possible addition: one interval, one exit. With `--deadman-seconds N`, a
supervisor that has had no LIVE leaseholder for N seconds stops its loop and
returns -- and its death EOFs every lifeline it owns, which is the kill path
ss5 already relies on ("supervisor death kills all wrappers by lifeline"),
not a new one. It adds no policy: the supervisor still decides nothing about
what should run, only that nobody is watching it any more.

It exists because ss8's `evict` -- the only state that lets another host run
work bound to this one -- must be PROVABLE rather than assumed, and nothing
else in this tier bounds when a controller-less supervisor's wrappers die.
It is opt-in per run root because it costs something real: tolerating an
absent controller indefinitely is exactly what lets an engine crash and
resume with its runs intact (DL-79). A run root without it is never
reroutable except by force.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import re
import selectors
import signal
import socket
import struct
import sys
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

# DL-72: the durability liturgy and the (pid, start-time) PID-reuse guard live
# in the sibling stdlib-only runner_procid -- one copy, shared with the wrapper
# (they were duplicated here, and had drifted). sys.path[0] is this file's
# directory when we are run by file path, except under PYTHONSAFEPATH=1 which
# strips it; prepend it ourselves so the plain top-level import resolves either
# way (importing dsl41.runner_procid would drag the package __init__, and with
# it third-party imports, into the supervisor's runtime). Prepend only when it
# is missing and take it back off again -- the engine also imports this file as
# an ordinary package module, and a library must not leave its own package
# directory on the importing process's sys.path, where it would shadow
# top-level names for the whole process. Same guard as the wrapper's.
#
# Two module OBJECTS come of this, not one: an engine process that imports
# dsl41.canon and this file's `canon` holds both, so `dsl41.canon.CanonError`
# does not catch the `CanonError` raised in here. Nothing crosses that line
# today (the supervisor answers its own canon errors), and a future in-process
# caller must catch by the name it imported.
#
# The TYPE_CHECKING branch names the same file under the name mypy maps it to
# (it cannot see one file under two names), which is what keeps verify_alive
# and killpg_quiet statically typed at their call sites here -- a PID-reuse
# guard and a group kill are the last calls that should be Any. It is erased
# before the process starts, so the stdlib-only boundary is untouched: the
# else branch is what runs, and the import-graph test reads runtime imports.
_PROCID_DIR = os.path.dirname(os.path.abspath(__file__))
_PROCID_DIR_ADDED = _PROCID_DIR not in sys.path
if _PROCID_DIR_ADDED:
    sys.path.insert(0, _PROCID_DIR)
if TYPE_CHECKING:
    from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, canonical_bytes
    from dsl41.canon import decode as canon_decode
    from dsl41.runner_procid import (
        current_boot_id,
        durable_write,
        durable_write_json,
        killpg_quiet,
        utc_now_iso,
        verify_alive,
    )
else:
    from canon import (  # noqa: E402
        ARTIFACT_FORMAT_VERSION,
        CanonError,
        canonical_bytes,
    )
    from canon import decode as canon_decode  # noqa: E402
    from runner_procid import (  # noqa: E402
        current_boot_id,
        durable_write,
        durable_write_json,
        killpg_quiet,
        utc_now_iso,
        verify_alive,
    )

if _PROCID_DIR_ADDED:
    sys.path.remove(_PROCID_DIR)

PROTOCOL_VERSION = 1

#: the Tier-0 wrapper, a sibling module run by file path (never -m). Resolved
#: relative to THIS file so the supervisor never imports dsl41 to find it.
_WRAPPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_wrapper.py")

#: period-model ss11a: `run_id` is filename-safe by GRAMMAR, checked at the
#: wire, because it names a directory entry here. The same pattern lives in
#: runner_effects.RUN_ID_RE, which this tier may not import (DL-42): the two
#: copies are one grammar, and a change to either is a protocol change.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

#: the run_id -> (job, run_number) index, under runs/. One entry per run, and
#: it is the FIRST durable thing a SPAWN writes: "no index entry" means "first
#: application", so deleting one authorizes a spawn (ss11a retention floor).
_INDEX_DIR = ".by_run_id"

#: how many COMPLETED runs stay in `self.runs` for LIST. The idempotency store
#: is the directory, so memory is bookkeeping: lifelines, exit pushes, and a
#: bounded window of recent completions for a controller that reconnects. An
#: estate whose root never rolls would otherwise grow this without limit
#: (ss11a). Older completions are read from the spool, which is the truth.
_LIST_COMPLETED_WINDOW = 256


# ------------------------------------------------------- ss11a tombstone files


def _fingerprint_form(value: object) -> object:
    """`value` with every float replaced by its exact hexadecimal form.

    ss3.2's grammar has no floats, and the frozen wrapper input spec (ss2)
    carries one -- `grace_seconds`. The fingerprint has to cover the whole
    spec, so the one type the canonical form cannot hold is written as the
    exact bits it has, tagged so no plausible string field can collide with
    it. Nothing but this fingerprint reads the result."""
    if isinstance(value, float):
        return "float:" + value.hex()
    if isinstance(value, dict):
        return {key: _fingerprint_form(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fingerprint_form(item) for item in value]
    return value


def spec_fingerprint(spec: dict[str, Any]) -> str:
    """`receipt.json`'s `spec_fingerprint` (ss11a): sha256 over the ss3.2
    canonical form of the wrapper input spec with `lifeline_fd` removed --
    the fd is ours to fill, so a retry that carried one would otherwise
    fingerprint differently from the receipt we wrote.

    Hashed over the WHOLE body, not through `canon.digest`: that helper
    strips a top-level `digest` key by design, so two specs differing only
    in a field of that name would share a fingerprint and a replay would
    answer duplicate for a spec it never received."""
    body = {key: item for key, item in spec.items() if key != "lifeline_fd"}
    return "sha256:" + hashlib.sha256(canonical_bytes(_fingerprint_form(body))).hexdigest()


def _write_canonical(path: str, record: dict[str, Any]) -> None:
    """One ss11a artifact, ss3.2-canonical, by the liturgy (same-directory
    temp file, fsync(file), rename, fsync(directory))."""
    durable_write(path, canonical_bytes(record))


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

    def __init__(self, run_root: str, *, deadman_s: float | None = None) -> None:
        self.run_root = run_root
        #: ss8's T_deadman, in seconds. None = no deadman: this supervisor
        #: tolerates an absent controller forever, which is what lets an
        #: engine crash and resume with its runs intact (DL-79), and is why
        #: the run root it serves is never reroutable except by force.
        self.deadman_s = deadman_s
        #: monotonic instant since which there has been no LIVE leaseholder,
        #: or None while one is live. Armed at bind rather than at the first
        #: lease, so a supervisor nobody ever leases dies too -- otherwise the
        #: one case with no controller at all would be the one case the
        #: deadman missed.
        self._unleased_since: float | None = None
        self.sock_path = os.path.join(run_root, "supervisor.sock")
        self.pid_path = os.path.join(run_root, "supervisor.pid")
        self.boot_id = current_boot_id()
        #: DL-80: identity of THIS supervisor process, minted per start. The
        #: fencing counter below is in-memory (spec ss5), so a restart mints
        #: token 1 again; once DL-79 made the token the credential, that reuse
        #: let a controller holding a token from the PREVIOUS incarnation match
        #: the new one by coincidence. Every mutating verb carries this, and a
        #: mismatch is `wrong_incarnation` -- deliberately NOT `stale_token`,
        #: because the two demand opposite client behaviour (re-acquire and
        #: reconcile vs do not re-acquire, someone else holds it).
        self.incarnation = uuid.uuid4().hex
        #: LIVE bookkeeping only (ss11a): lifelines, exit pushes and LIST.
        #: The idempotency store is the run directory, so a completed entry
        #: may leave -- and must, or a root that never rolls grows this
        #: without bound.
        self.runs: dict[str, _Run] = {}
        #: completion order, for the bounded LIST window above
        self._completed: deque[str] = deque()
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
        durable_write_json(
            self.pid_path,
            {
                "pid": os.getpid(),
                "boot_id": self.boot_id,
                "incarnation": self.incarnation,
                "started_at": utc_now_iso(),
            },
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
        self._unleased_since = time.monotonic()
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
                elif self._deadman_expired():
                    self._running = False
        finally:
            self._teardown()
        return 0

    # -- deadman (ss8) ------------------------------------------------------

    def _live_leaseholder(self) -> bool:
        """ss5's LIVE lease: unexpired AND its holder's connection still open.

        Both halves, because either alone asks the wrong question. An
        unexpired lease whose connection died means a controller that is
        GONE -- the kernel closes this AF_UNIX fd only when the holder
        process is, kill -9 included. An expired lease whose connection is
        open means a controller that stopped renewing, which is a controller
        that has stopped watching."""
        return self._lease_active() and self.lease is not None and self.lease.conn is not None

    def _deadman_expired(self) -> bool:
        """Has this supervisor been unwatched for T_deadman (ss8)?

        The clock RESTARTS whenever a live leaseholder appears, so a
        reconnecting engine reprieves it. That is the point: the deadman
        bounds how long an UNREACHABLE host keeps running work, and a host
        the leader can still reach is not that."""
        if self.deadman_s is None:
            return False
        if self._live_leaseholder():
            self._unleased_since = None
            return False
        now = time.monotonic()
        if self._unleased_since is None:
            self._unleased_since = now
            return False
        if now - self._unleased_since < self.deadman_s:
            return False
        # loud, in the one log this process has: an operator reading it after
        # the fact must find the reason its jobs died, not infer it
        print(
            f"supervisor: deadman fired -- no live leaseholder for {self.deadman_s}s;"
            f" exiting, which EOFs {len(self.runs)} lifeline(s) (concurrency-model ss8)",
            file=sys.stderr,
            flush=True,
        )
        return True

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
        try:
            answer = handler(conn, req)
        except Exception as exc:  # noqa: BLE001 -- the belt under every verb
            # a handler that raises would end this process, and its death
            # EOFs the lifeline of every wrapper on the host. Whatever went
            # wrong is ONE request's problem; it is answered, and the tier
            # that must outlive the engine keeps running.
            answer = {"ok": False, "error": f"internal: {type(exc).__name__}: {exc}"}
        self._send(conn, answer)

    def _h_ping(self, _conn: _Conn, _req: dict[str, Any]) -> dict[str, Any]:
        # `deadman_s` rides the two READ verbs (S5b): the leader records what
        # this host ACTUALLY runs, not what some engine once asked it to run.
        # A reattaching engine meets a supervisor it did not start, and a
        # bound derived from its own flag would then describe nothing.
        return {
            "ok": True,
            "version": PROTOCOL_VERSION,
            "incarnation": self.incarnation,
            "deadman_s": self.deadman_s,
        }

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
            "incarnation": self.incarnation,
            "deadman_s": self.deadman_s,
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
        """Every mutating verb: the request must name THIS incarnation (DL-80)
        and its token must match a live lease. Incarnation is checked first and
        answers separately: a token from a dead supervisor is not a lost
        election, it is a vanished world -- that controller's wrappers all died
        by lifeline, so it must re-acquire AND reconcile from the spool, which
        is the opposite of what stale_token asks for."""
        if req.get("incarnation") != self.incarnation:
            return {"ok": False, "error": "wrong_incarnation", "incarnation": self.incarnation}
        if not self._lease_active() or self.lease is None or self.lease.token != req.get("token"):
            return {"ok": False, "error": "stale_token"}
        return None

    def _h_acquire(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        controller_id = req.get("controller_id")
        if not isinstance(controller_id, str) or not controller_id:
            return {"ok": False, "error": "bad_controller_id"}
        ttl_s = float(req.get("ttl_s", 60))
        # DL-79. A LIVE lease -- unexpired AND its holder's connection still
        # open -- yields only to the holder itself, and the holder proves
        # incumbency by presenting its CURRENT token. controller_id is a
        # label, not a credential: any client may send any string, and until
        # DL-79 a matching one took the lease away from a live holder. That
        # was safe only because one run_root had one engine, which the
        # engine's own control-socket bind enforced ON THIS MACHINE; the
        # moment a second host can serve the same logical run, the label
        # stops discriminating and the partitioned OLD leader fences out the
        # new one.
        #
        # The ORPHANED case -- lease unexpired, holder's connection gone --
        # stays freely grantable, and that is what lets a crashed engine's
        # resume re-acquire without waiting out the TTL. It is sound here
        # because the kernel closes this AF_UNIX fd only when the holder
        # process is gone (kill -9 included), so EOF is proof of death.
        # A NON-LOCAL transport breaks that inference: a relay must not close
        # the supervisor-side connection while its controller lives, or this
        # branch must become TTL-gated. Recorded, not yet needed.
        #
        # The token proves incumbency, not authenticity -- it is a small
        # monotone integer. Authentication is the same-uid peer-cred gate on
        # accept (ss1); a same-uid process is already inside the trust
        # boundary and can signal the engine directly.
        if self._lease_active() and self.lease is not None and self.lease.conn is not None:
            incumbent = (
                req.get("incarnation") == self.incarnation and req.get("token") == self.lease.token
            )
            if not incumbent:
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
        return {
            "ok": True,
            "token": token,
            "expires_at": expires_at,
            "incarnation": self.incarnation,  # DL-80: pair it with the token
        }

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
        if not isinstance(spec, dict):
            return {"ok": False, "error": "bad_spec", "detail": "spec is not an object"}
        return self.spawn_run(spec)

    # -- ss11a: directory-backed SPAWN idempotency ---------------------------

    def _crash_point(self, _stage: str) -> None:
        """The PR-36 crash matrix's seam, and nothing else.

        Every write in `spawn_run` is followed by one of these, so a test can
        stop the process exactly between two durable acts and then ask a
        FRESH supervisor what the directory says. A no-op in production; the
        alternative was a test that kills a real supervisor and hopes it
        died in the window it meant."""

    def index_path(self, run_id: str) -> str:
        return os.path.join(self.run_root, "runs", _INDEX_DIR, run_id)

    def run_dir_for(self, job: str, run_number: int) -> str:
        """ss11a's one-to-one ownership, as a path: one run_id maps to one
        (job, run_number), and one (job, run_number) maps to one directory.
        The index entry carries the pair, not the path, so this is how a
        replay gets from the index back to the tombstone."""
        return os.path.join(self.run_root, "runs", f"{job}.{run_number}")

    def spawn_run(self, spec: dict[str, Any]) -> dict[str, Any]:
        """SPAWN, minus the lease gate: validate, resolve a replay through the
        index, or apply for the first time (ss11a). Public to the crash
        matrix, which drives it directly rather than over the socket."""
        run_id = spec.get("run_id")
        if not isinstance(run_id, str):
            return {"ok": False, "error": "bad_spec", "detail": "spec.run_id is missing"}
        if _RUN_ID_RE.fullmatch(run_id) is None:
            # ss11a: the id names a directory entry here, so the grammar is a
            # wire check, not a convention. It is refused BEFORE anything is
            # created -- a freehand id would otherwise reach mkdir/open.
            return {"ok": False, "error": "bad_run_id", "detail": f"run_id {run_id!r}"}
        job = spec.get("job")
        run_number = spec.get("run_number")
        if (
            not isinstance(job, str)
            or not job
            or os.sep in job
            or job in (".", "..")
            or isinstance(run_number, bool)
            or not isinstance(run_number, int)
        ):
            return {"ok": False, "error": "bad_spec", "detail": "spec.job / spec.run_number"}
        run_dir = self.run_dir_for(job, run_number)
        if os.path.realpath(str(spec.get("run_dir"))) != os.path.realpath(run_dir):
            # the supervisor OWNS this directory now, so it insists on the one
            # it owns: index -> (job, run_number) -> directory is the only way
            # a replay finds the tombstone again. REALPATH, not normpath: the
            # engine and this process are told the run root separately, so one
            # may hold `./r` where the other holds `/abs/r`, or `/tmp/r` where
            # the other holds `/private/tmp/r`. Two spellings of one directory
            # must not refuse every spawn on the host.
            return {"ok": False, "error": "bad_spec", "detail": f"run_dir must be {run_dir}"}
        bad = _spec_schema_error(spec)
        if bad is not None:
            # the WHOLE frozen ss2 schema, before anything durable and before
            # any replay resolution. Two reasons. A field of the wrong type
            # that only explodes after the fork (grace_seconds: "x" reaching
            # float()) would kill this process and EOF every wrapper on the
            # host. And the fingerprint's "float:"+hex tag is only
            # collision-free while every key's type is pinned: with unknown
            # keys refused and grace_seconds the one number, no VALID spec
            # can hold a string where another holds the float that encodes
            # to it.
            return {"ok": False, "error": "bad_spec", "detail": bad}
        try:
            fingerprint = spec_fingerprint(spec)
        except CanonError as exc:
            # a value ss3.2 cannot hold -- a lone surrogate, say. The receipt
            # could not be written for it either, and this tier answers rather
            # than dies: every wrapper it holds is tethered to this process.
            return {"ok": False, "error": "bad_spec", "detail": f"unfingerprintable spec: {exc}"}
        replay = self._resolve_replay(run_id, job, run_number, run_dir, fingerprint)
        if replay is not None:
            return replay
        return self._first_application(spec, run_id, job, run_number, run_dir, fingerprint)

    def _resolve_replay(
        self, run_id: str, job: str, run_number: int, run_dir: str, fingerprint: str
    ) -> dict[str, Any] | None:
        """The ss11a answer table, or None for "first application".

        Resolution goes through the INDEX, never through the incoming path:
        the index is the first durable thing that names a run_id, so it is
        the only thing that can prove one was received. The incoming path is
        consulted only to refuse a collision on it."""
        index = _load_tombstone(self.index_path(run_id), "index")
        if index is _INVALID:
            # corruption is not absence: "no index entry" AUTHORIZES a spawn,
            # so an unreadable one must never read as one (ss11a)
            return self._indeterminate(f"the index entry for {run_id} is unreadable")
        if index is not None and index.get("run_id") != run_id:
            # the entry disagrees with its own name: tampered or misfiled.
            # Believing either half would answer for a run the other half
            # disowns.
            return self._indeterminate(
                f"the index entry for {run_id} names run_id {index.get('run_id')!r}"
            )
        if index is not None:
            if (index.get("job"), index.get("run_number")) != (job, run_number):
                return self._collision(
                    f"run_id {run_id} is bound to"
                    f" {index.get('job')}.{index.get('run_number')}, not {job}.{run_number}"
                )
            indexed = self.run_dir_for(str(index.get("job")), int(index.get("run_number", 0)))
            if not os.path.isdir(indexed):
                # impossible by write order (mkdir precedes the index); if it
                # is ever seen, nothing may re-spawn
                return self._indeterminate(f"the index names {indexed}, which does not exist")
            return self._answer_from_directory(run_id, job, run_number, indexed, fingerprint)
        receipt = _load_tombstone(os.path.join(run_dir, "receipt.json"), "receipt")
        if receipt is _INVALID:
            return self._indeterminate(f"{run_dir}/receipt.json is unreadable")
        if receipt is not None:
            # no index for this run_id, but the path is somebody's tombstone:
            # a different run_id is a collision, and our own id here means the
            # index was lost under a live tombstone -- either way, never a
            # first application (deleting an index authorizes a spawn, ss11a)
            if receipt.get("run_id") != run_id:
                return self._collision(
                    f"{run_dir} already holds a receipt for run_id {receipt.get('run_id')!r}"
                )
            return self._answer_from_directory(run_id, job, run_number, run_dir, fingerprint)
        if os.path.isdir(run_dir):
            # an orphan directory: a crash between mkdir and the index. It is
            # REUSED, because nothing durable names its run -- unless another
            # run_id's index does, which is the same crash under a different
            # id and is the one case worth an O(n) scan of the index (it runs
            # only here, after a crash, never on a healthy spawn)
            owner = self._indexed_owner(job, run_number)
            if owner is _UNREADABLE:
                return self._indeterminate(f"the {_INDEX_DIR} directory cannot be listed")
            if owner is not None and owner != run_id:
                return self._collision(f"{run_dir} is already indexed under run_id {owner!r}")
            for name in ("spawn.json", "status.json"):
                if os.path.exists(os.path.join(run_dir, name)):
                    # a directory made under the OLD rule, where the engine
                    # created it and no receipt was ever written. It holds a
                    # run's evidence, and forking into it would overwrite that
                    # run's records with a second one's.
                    return self._indeterminate(
                        f"{run_dir} holds a wrapper's {name} and no receipt:"
                        " a run predating this protocol, never reused"
                    )
        return None

    def _answer_from_directory(
        self, run_id: str, job: str, run_number: int, directory: str, fingerprint: str
    ) -> dict[str, Any]:
        """ss11a: a replay answers from the DIRECTORY, not from memory -- the
        original reply if it survived, the wrapper's own record if it did
        not, and a refusal when the crash landed somewhere no answer can be
        reconstructed from."""
        receipt = _load_tombstone(os.path.join(directory, "receipt.json"), "receipt")
        if receipt is _INVALID:
            return self._indeterminate(f"{directory}/receipt.json is unreadable")
        if receipt is None:
            return self._indeterminate(f"{directory} has an index entry and no receipt")
        if receipt.get("run_id") != run_id:  # a receipt for someone else at our path
            return self._collision(
                f"{directory} holds a receipt for run_id {receipt.get('run_id')!r}"
            )
        if receipt.get("spec_fingerprint") != fingerprint:
            return self._collision(f"{directory} was received under a different spec fingerprint")
        # reply/spawn records: _INVALID falls through _duplicate_from's
        # run_id gate to the next table row -- for these two the next row is
        # always SAFER (reconstruct, then in-progress/indeterminate), so
        # unreadable never invents an answer
        reply = _load_tombstone(os.path.join(directory, "reply.json"), "reply")
        answer = _duplicate_from(run_id, reply, "wrapper_pid", "spawned_at")
        if answer is not None:
            return answer
        spawned = _load_json(os.path.join(directory, "spawn.json"))
        if spawned is _INVALID:
            spawned = None
        if spawned is not None and (
            spawned.get("run_id"),
            spawned.get("job"),
            spawned.get("run_number"),
        ) != (run_id, job, run_number):
            # the same rule _signal_command applies to this file: a record that
            # does not name this run is spoofed, stale or foreign, and an
            # answer built from it would hand the engine another run's pid
            spawned = None
        # equivalent, and the protocol says so rather than promising bytes it
        # did not keep: the wrapper's own record carries the same two facts
        answer = _duplicate_from(run_id, spawned, "wrapper_pid", "started_at")
        if answer is not None:
            return answer
        run = self.runs.get(run_id)
        if run is not None and run.wrapper_rc is None:
            # the receipt is durable, the fork happened, and this incarnation
            # still holds the lifeline: no second spawn, and no answer yet.
            # Liveness is the one thing memory is authoritative for -- a
            # wrapper from a previous incarnation cannot be alive (its
            # lifeline EOF'd when that supervisor died)
            return {
                "ok": False,
                "error": "in_progress",
                "run_id": run_id,
                "detail": f"{directory} is mid-spawn: the wrapper is alive and has not recorded",
            }
        return self._indeterminate(
            f"{directory} holds a receipt, no spawn record, and nothing alive"
        )

    def _indexed_owner(self, job: str, run_number: int) -> str | None:
        """The run_id whose index entry claims (job, run_number), if any --
        or `_UNREADABLE` when the directory cannot be listed: "no owner"
        AUTHORIZES reuse, and an EACCES must never spell it."""
        index_dir = os.path.join(self.run_root, "runs", _INDEX_DIR)
        try:
            names = os.listdir(index_dir)
        except FileNotFoundError:
            return None  # no index directory: nothing was ever received here
        except OSError:
            return _UNREADABLE
        for name in sorted(names):
            if _RUN_ID_RE.fullmatch(name) is None:
                # `durable_write` leaves `.<name>.<pid>.tmp` behind if it dies
                # between fsync and rename: a complete record that was never
                # durable, and reading it would refuse a spawn that §11a says
                # is a first application
                continue
            entry = _load_tombstone(os.path.join(index_dir, name), "index")
            if entry is _INVALID:
                # an unreadable entry MIGHT claim this run: reuse under it
                # would be the double spawn ss11a exists to prevent
                return name
            if entry is not None and entry.get("run_id") != name:
                return _UNREADABLE  # an entry disowning its name blocks reuse
            if entry is not None and (entry.get("job"), entry.get("run_number")) == (
                job,
                run_number,
            ):
                return name
        return None

    @staticmethod
    def _collision(detail: str) -> dict[str, Any]:
        """ss11a: never reused, never given a second index. There is no
        correct pick between two identities for one directory."""
        return {"ok": False, "error": "collision", "detail": detail}

    @staticmethod
    def _indeterminate(detail: str) -> dict[str, Any]:
        """ss11a: the crash landed where nothing may re-spawn. The engine's
        E7 policy decides the run; this tier states the fact."""
        return {"ok": False, "error": "indeterminate", "detail": detail}

    def _first_application(
        self,
        spec: dict[str, Any],
        run_id: str,
        job: str,
        run_number: int,
        run_dir: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        """ss11a's write order, exactly: mkdir, index, receipt, spawn, reply,
        answer. Index before receipt, because the frozen idempotency key is
        `run_id` and every later lookup goes through the index -- so the first
        durable thing that names the run must be the index. Receipt before the
        fork, because the failure mode of that direction is a run that never
        happened being reported unknown (which E7 handles), and the failure
        mode of the other is a retry that spawns twice (which nothing does)."""
        # Every write below is answered, never raised: an unhandled error here
        # would end this process, and its death EOFs the lifeline of every
        # wrapper it holds. A tier whose job is to outlive the engine does not
        # die of ENOSPC on one run's tombstone.
        try:
            os.makedirs(os.path.join(self.run_root, "runs", _INDEX_DIR), exist_ok=True)
            try:
                os.mkdir(run_dir)
            except FileExistsError:
                pass  # the orphan directory the resolver cleared for reuse
            _fsync_dir(run_dir)
            _fsync_dir(os.path.dirname(run_dir))
            self._crash_point("after_mkdir")
            _write_canonical(
                self.index_path(run_id),
                {
                    "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                    "job": job,
                    "run_id": run_id,
                    "run_number": run_number,
                },
            )
            self._crash_point("after_index")
            _write_canonical(
                os.path.join(run_dir, "receipt.json"),
                {
                    "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                    "received_at": utc_now_iso(),
                    "run_id": run_id,
                    "spec_fingerprint": fingerprint,
                },
            )
        except (OSError, CanonError) as exc:
            # nothing has forked yet, so this is a spawn that did not happen.
            # Whatever half-written tombstone it leaves reads `indeterminate`
            # on a retry, which is the truthful answer to a receipt this
            # supervisor could not finish writing.
            return {"ok": False, "error": f"spawn_failed: {exc}"}
        self._crash_point("after_receipt")
        try:
            wrapper_pid, lifeline_w = self._spawn_wrapper(spec)
        except OSError as exc:
            return {"ok": False, "error": f"spawn_failed: {exc}"}
        self._crash_point("after_spawn")
        spawned_at = utc_now_iso()
        self.runs[run_id] = _Run(
            run_id=run_id,
            job=job,
            run_number=run_number,
            run_dir=run_dir,
            wrapper_pid=wrapper_pid,
            lifeline_w=lifeline_w,
            spawned_at=spawned_at,
            grace_seconds=float(spec.get("grace_seconds", 10.0)),
        )
        try:
            _write_canonical(
                os.path.join(run_dir, "reply.json"),
                {
                    "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                    "run_id": run_id,
                    "spawned_at": spawned_at,
                    "wrapper_pid": wrapper_pid,
                },
            )
        except (OSError, CanonError) as exc:
            # the wrapper is ALREADY running: the answer stands, and a replay
            # reconstructs it from the wrapper's own spawn.json (ss11a's second
            # row). Losing the run over its receipt copy would be the one
            # mistake this order exists to avoid.
            print(
                f"supervisor: reply.json for {run_id} not written ({exc});"
                " a replay will answer from spawn.json",
                file=sys.stderr,
                flush=True,
            )
        self._crash_point("after_reply")
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
        outcome = self._signal_command(run, sig)
        if outcome == "sent":
            return {"ok": True}
        if outcome == "not_ready":
            # DL-83: SPAWN returns once the wrapper is FORKED, and the wrapper
            # writes spawn.json a few syscalls later. A signal landing in that
            # window used to answer noop -- indistinguishable from "the group
            # is already gone" -- so a KILLJOB decided milliseconds after a
            # start was silently dropped and the engine recorded TERMINATED
            # for a job that ran to completion. The wrapper being ALIVE with
            # no record yet is the discriminator, and it means retry, not
            # nothing-to-do.
            return {"ok": False, "error": "not_ready"}
        return {"ok": True, "noop": True}  # already-dead / unverifiable group

    def _signal_command(self, run: _Run, sig: int) -> str:
        """Signal the recorded command PGID -- never the wrapper (the recorder
        is untouchable) -- after the (pid, start-time) PID-reuse guard.

        Returns "sent", "noop" (nothing there to signal), or "not_ready"
        (DL-83: the wrapper is alive but has not written spawn.json yet, so
        the command may exist and simply not be addressable -- the caller must
        retry rather than treat the kill as done). Collapsing those last two
        into one answer is how a kill decided in the spawn window vanished."""
        spawn = _load_json(os.path.join(run.run_dir, "spawn.json"))
        if spawn is None:
            # wrapper_rc is None while the wrapper lives (set by _reap). A live
            # wrapper with no record is mid-spawn; a dead one never recorded,
            # and there is nothing this tier can still address.
            return "not_ready" if run.wrapper_rc is None else "noop"
        if (spawn.get("job"), spawn.get("run_number"), spawn.get("run_id")) != (
            run.job,
            run.run_number,
            run.run_id,
        ):
            # spoofed/corrupt spawn record: never trust, never signal. The
            # run_id is part of the check (DL-118/DL-129): the tuple alone
            # plus a live foreign pid token would aim the signal at a
            # stranger's process group.
            return "noop"
        pid = spawn.get("command_pid")
        pgid = spawn.get("command_pgid")
        token = spawn.get("command_start_time")
        if not (isinstance(pid, int) and isinstance(pgid, int) and isinstance(token, str)):
            return "noop"
        if not verify_alive(pid, token):  # the PID-reuse guard
            return "noop"
        killpg_quiet(pgid, sig)
        return "sent"

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
            self._evict_completed(run)

    def _push_exit(self, run: _Run) -> None:
        if self.lease is None or self.lease.conn is None or not self._lease_active():
            return
        self._send(
            self.lease.conn,
            {
                "push": "exit",
                "run_id": run.run_id,
                "wrapper_rc": run.wrapper_rc,
                "at": utc_now_iso(),
            },
        )

    def _evict_completed(self, run: _Run) -> None:
        """Bound LIST (ss11a). The exit is recorded and pushed, so this entry
        is history; the newest `_LIST_COMPLETED_WINDOW` of them stay for a
        controller that reconnects and reads LIST, and older ones are read
        from the spool, which was the truth all along. Idempotency does not
        notice: it resolves through the run directory, which outlives both
        this dict and this process. A SIGNAL for an evicted run answers
        `unknown_run`, exactly as it does for any run of an incarnation that
        has ended."""
        self._completed.append(run.run_id)
        while len(self._completed) > _LIST_COMPLETED_WINDOW:
            self.runs.pop(self._completed.popleft(), None)

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


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: the frozen ss2 wrapper-input schema, as (key, predicate) -- the whole of
#: it, because a fingerprint over a spec with an unpinned key type is not
#: collision-free, and an unvalidated field that only explodes after the
#: fork kills the one process whose death EOFs every wrapper on the host.
_SPEC_SCHEMA: dict[str, Any] = {
    "version": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "run_id": lambda v: isinstance(v, str),
    "job": lambda v: isinstance(v, str),
    "run_number": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "command": lambda v: isinstance(v, str),
    "run_dir": lambda v: isinstance(v, str),
    "stdout_path": lambda v: isinstance(v, str),
    "stderr_path": lambda v: isinstance(v, str),
    "stdin_path": lambda v: v is None or isinstance(v, str),
    "grace_seconds": lambda v: (
        isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) >= 0.0
    ),
    "lifeline_fd": lambda v: isinstance(v, int) and not isinstance(v, bool),
}

#: lifeline_fd is the supervisor's to fill; a retry may carry a stale one
#: (the fingerprint strips it), but nothing else may be missing
_SPEC_OPTIONAL = frozenset({"lifeline_fd"})


def _spec_schema_error(spec: dict[str, Any]) -> str | None:
    """The frozen ss2 shape, or the first reason it is not. Unknown keys
    REFUSE: the protocol is frozen, so a key this schema does not pin is a
    key whose type is not pinned either -- and the fingerprint's typed float
    encoding is only injective over pinned types."""
    for key in spec:
        if key not in _SPEC_SCHEMA:
            return f"unknown spec key {key!r}"
    for key, check in _SPEC_SCHEMA.items():
        if key not in spec:
            if key in _SPEC_OPTIONAL:
                continue
            return f"spec.{key} is missing"
        if not check(spec[key]):
            return f"spec.{key} has the wrong type"
    return None


def _duplicate_from(
    run_id: str, doc: dict[str, Any] | None, pid_key: str, at_key: str
) -> dict[str, Any] | None:
    """The frozen ss11a duplicate envelope, built from `doc` -- or None when
    that record cannot carry it or the record does not NAME this run. An
    unreadable, half-written or foreign record is not an answer, so the
    caller falls to the next row of the table rather than inventing a pid
    (or handing back a stranger's)."""
    if doc is None or doc.get("run_id") != run_id:
        return None
    pid = doc.get(pid_key)
    spawned_at = doc.get(at_key)
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(spawned_at, str):
        return None
    return {
        "ok": True,
        "run_id": run_id,
        "wrapper_pid": pid,
        "spawned_at": spawned_at,
        "duplicate": True,
    }


#: `_load_json`'s "present but unreadable" answer. Distinct from None
#: (absent), because ss11a's table keys on ABSENCE -- "no index entry" means
#: "first application" -- and reading corruption as absence would turn a
#: damaged index entry into an authorization to spawn a second process.
_INVALID: dict[str, Any] = {"__invalid__": True}

#: `_indexed_owner`'s "the directory cannot be listed" answer -- outside the
#: run_id grammar, so it can never equal a real owner
_UNREADABLE = "__unreadable__"


def _load_json(path: str) -> dict[str, Any] | None:
    """A wrapper-written spool record: plain JSON. ABSENT means exactly
    ENOENT -- an EACCES or EIO is a file that EXISTS and cannot be read,
    and reading that as absence would erase evidence (ss11a)."""
    try:
        with open(path, "rb") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return None
    except OSError:
        return _INVALID
    except json.JSONDecodeError:
        return _INVALID
    return loaded if isinstance(loaded, dict) else _INVALID


#: the ss11a tombstone schemas: required keys and their type checks, per
#: file. A record answering a replay is EVIDENCE, and evidence with a
#: missing or mistyped field is refused, not partially believed.
#: required on EVERY tombstone: `canon.decode` refuses a version it does not
#: implement but passes an ABSENT one (whether a version is required is the
#: reader's call -- canon.check_artifact_version), and this reader requires
#: it: an unversioned record is unsupported evidence.
_VERSIONED = {
    "artifact_format_version": lambda v: v == ARTIFACT_FORMAT_VERSION and not isinstance(v, bool)
}

_TOMBSTONE_SCHEMAS: dict[str, dict[str, Any]] = {
    "index": {
        **_VERSIONED,
        "run_id": lambda v: isinstance(v, str),
        "job": lambda v: isinstance(v, str),
        "run_number": lambda v: isinstance(v, int) and not isinstance(v, bool),
    },
    "receipt": {
        **_VERSIONED,
        "run_id": lambda v: isinstance(v, str),
        "spec_fingerprint": lambda v: isinstance(v, str),
        "received_at": lambda v: isinstance(v, str),
    },
    "reply": {
        **_VERSIONED,
        "run_id": lambda v: isinstance(v, str),
        "wrapper_pid": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "spawned_at": lambda v: isinstance(v, str),
    },
}


def _load_tombstone(path: str, kind: str) -> dict[str, Any] | None:
    """A supervisor-written ss11a record, read through the ss3.2 ingress:
    duplicate keys, floats, non-scalar strings and an artifact_format_version
    this binary does not implement all refuse (PR-08d/PR-12), and so does a
    record missing a required field -- `json.load` would accept all of them
    and let a replay answer from unsupported evidence."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return _INVALID
    try:
        loaded = canon_decode(raw)
    except CanonError:
        return _INVALID
    if not isinstance(loaded, dict):
        return _INVALID
    for key, check in _TOMBSTONE_SCHEMAS[kind].items():
        if key not in loaded or not check(loaded[key]):
            return _INVALID
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runner_supervisor")
    parser.add_argument("--run-root", required=True)
    parser.add_argument(
        "--deadman-seconds",
        type=float,
        default=None,
        help="exit after this long with no live leaseholder, killing every"
        " wrapper by lifeline EOF (concurrency-model ss8). Omitted: no deadman,"
        " and this run root is never reroutable except by force.",
    )
    args = parser.parse_args(argv)
    if args.deadman_seconds is not None and args.deadman_seconds <= 0:
        print("supervisor: --deadman-seconds must be positive", file=sys.stderr)
        return 2
    # ENOENT on the run_root is a caller bug (the engine makes it first)
    if not os.path.isdir(args.run_root):
        print(f"supervisor: run-root {args.run_root!r} does not exist", file=sys.stderr)
        return 2
    try:
        return Supervisor(args.run_root, deadman_s=args.deadman_seconds).run()
    except SystemExit as exc:  # the live-supervisor gate
        print(str(exc), file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
