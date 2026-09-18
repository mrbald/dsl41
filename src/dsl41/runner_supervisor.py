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
import errno
import hashlib
import json
import math
import os
import re
import selectors
import signal
import socket
import stat
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
    from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, canonical_bytes, is_wire_int
    from dsl41.canon import decode as canon_decode
    from dsl41.runner_procid import (
        LockHeld,
        current_boot_id,
        flock_exclusive,
        proc_start_token,
        start_tokens_match,
        durable_write,
        durable_write_json,
        fsync_dir,
        killpg_quiet,
        spool_version_supported,
        utc_now_iso,
        verify_alive,
    )
else:
    from canon import (  # noqa: E402
        ARTIFACT_FORMAT_VERSION,
        CanonError,
        canonical_bytes,
        is_wire_int,
    )
    from canon import decode as canon_decode  # noqa: E402
    from runner_procid import (  # noqa: E402
        LockHeld,
        current_boot_id,
        flock_exclusive,
        proc_start_token,
        start_tokens_match,
        durable_write,
        durable_write_json,
        fsync_dir,
        killpg_quiet,
        spool_version_supported,
        utc_now_iso,
        verify_alive,
    )

if _PROCID_DIR_ADDED:
    sys.path.remove(_PROCID_DIR)

PROTOCOL_VERSION = 1

# DL-210: limits are per connection. One reply may cross the output bound.
REQUEST_LINE_LIMIT = 2**20
BACKLOG_BYTES = 2**24


def _test_limit(name: str, default: int) -> int:
    """Read an opt-in test seam once at startup; production uses the constant."""
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _valid_start_token(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value.startswith("ticks:"):
        return re.fullmatch(r"ticks:(0|[1-9][0-9]*)", value) is not None
    if value.startswith("lstart:"):
        try:
            time.strptime(value[len("lstart:") :], "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return False
        return True
    return False


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


def _is_wire_int_equal(value: object, expected: int) -> bool:
    """Integer identity on the wire, which Python's `==` is not (DL-151).

    `True == 1` and `1.0 == 1`, so a bare comparison let a JSON `true` or
    `1.0` stand in for protocol version 1 or fencing token 1. A version and
    a fencing token are integers, and nothing else may pass as one: the
    token is the fence that keeps a superseded controller out."""
    return is_wire_int(value) and value == expected


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
    __slots__ = ("holder", "token", "deadline", "expires_at", "conn", "pushes_dropped", "drops")

    def __init__(
        self, holder: str, token: int, deadline: float, expires_at: str, conn: _Conn | None
    ) -> None:
        self.holder = holder
        self.token = token
        self.deadline = deadline  # time.monotonic() basis (immune to clock steps)
        self.expires_at = expires_at
        self.conn = conn
        self.pushes_dropped = False
        self.drops = 0  # a queued reply must not acknowledge a later dropped push


class _Conn:
    __slots__ = ("sock", "buf", "out", "backlog", "paused", "discarding")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.out: deque[tuple[bytes, tuple[_Lease, int] | None]] = deque()
        self.backlog = 0
        self.paused = False
        self.discarding = False


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
        self._lock_fd: int | None = None
        self._private_path = os.path.join(run_root, f".s.{os.getpid()}")
        self._private_bound = False
        self._published = False
        self._socket_inode: int | None = None
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
        self._backlog_bytes = _test_limit("DSL41_SUPERVISOR_TEST_BACKLOG_BYTES", BACKLOG_BYTES)
        self._request_line_limit = _test_limit(
            "DSL41_SUPERVISOR_TEST_REQUEST_LINE_LIMIT", REQUEST_LINE_LIMIT
        )
        self._accept_log_at = float("-inf")

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

    def _probe_answered(self, deadline: float) -> bool:
        """PING within this attempt's budget; connect alone is not liveness."""
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.settimeout(max(0.001, deadline - time.monotonic()))
                probe.connect(self.sock_path)
                probe.settimeout(max(0.001, deadline - time.monotonic()))
                probe.sendall(b'{"v":1,"cmd":"PING"}\n')
                reply = b""
                while b"\n" not in reply and len(reply) < 4096:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    probe.settimeout(left)
                    chunk = probe.recv(4096 - len(reply))
                    if not chunk:
                        return False
                    reply += chunk
                if b"\n" not in reply:
                    return False
                answer = json.loads(reply.split(b"\n", 1)[0])
                return isinstance(answer, dict) and isinstance(answer.get("ok"), bool)
            except (OSError, ValueError, RecursionError):
                return False

    def _pid_owner_absent(self) -> bool:
        """Only positive absence or PID reuse can clear a recorded owner.

        Old supervisors have no start_time field. A live legacy pid is
        therefore enough to refuse. A failed token lookup is not absence:
        kill(pid, 0) must confirm ESRCH before reclamation.
        """
        try:
            record = _load_json(self.pid_path)
        except RecursionError:
            return False
        if record is None:
            # A missing record names no owner. A present but unreadable one
            # cannot authorize reclamation, even after unanswered probes.
            return not os.path.lexists(self.pid_path)
        pid = record.get("pid")
        if not is_wire_int(pid) or pid <= 0:
            return False
        boot = record.get("boot_id")
        if isinstance(boot, str) and boot not in ("", "unknown", self.boot_id):
            if self.boot_id != "unknown":
                return True
        current = proc_start_token(pid)
        recorded = record.get("start_time")
        if current is not None:
            return (
                isinstance(recorded, str)
                and _valid_start_token(recorded)
                and not start_tokens_match(current, recorded)
            )
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        return False

    def _refuse_if_live(self) -> None:
        if os.path.lexists(self.sock_path):
            # Three attempts across one second. macOS can refuse a full
            # listen backlog, so the first refusal never authorizes unlink.
            started = time.monotonic()
            for attempt in range(3):
                deadline = started + (attempt + 1) / 3
                if self._probe_answered(deadline):
                    raise SystemExit("another supervisor owns this root")
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        if not self._pid_owner_absent():
            raise SystemExit("another supervisor owns this root")

    def _bind(self) -> None:
        try:
            self._lock_fd = flock_exclusive(os.path.join(self.run_root, "supervisor.lock"))
        except LockHeld as exc:
            raise SystemExit("another supervisor owns this root") from exc
        # Sweep private sockets only, under the exclusion lock. Ordinary
        # files or symlinks with this prefix are not ours to remove.
        with os.scandir(self.run_root) as entries:
            for entry in entries:
                if entry.name.startswith(".s."):
                    with contextlib.suppress(FileNotFoundError):
                        if stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode):
                            os.unlink(entry.path)
        self._refuse_if_live()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.sock_path)
        old_umask = os.umask(0o177)
        try:
            self._listen = socket.socket(socket.AF_UNIX)
            self._listen.bind(self._private_path)
            self._private_bound = True
            self._listen.listen(64)
        finally:
            os.umask(old_umask)
        os.chmod(self._private_path, 0o600)
        self._socket_inode = os.stat(self._private_path).st_ino
        durable_write_json(
            self.pid_path,
            {
                "pid": os.getpid(),
                "start_time": proc_start_token(os.getpid()),
                "boot_id": self.boot_id,
                "incarnation": self.incarnation,
                "started_at": utc_now_iso(),
            },
        )
        os.rename(self._private_path, self.sock_path)
        self._private_bound = False
        self._published = True
        self._listen.setblocking(False)

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
        try:
            self._set_subreaper()
            self._bind()
            self._install_signals()
            assert self._listen is not None
            self._sel.register(self._listen, selectors.EVENT_READ, ("listen", None))
            self._sel.register(self._chld_r, selectors.EVENT_READ, ("chld", None))
            self._unleased_since = time.monotonic()
            print(
                f"supervisor: started pid={os.getpid()} incarnation={self.incarnation}"
                f" boot_id={self.boot_id}",
                file=sys.stderr,
                flush=True,
            )
            while self._running:
                for key, mask in self._sel.select(timeout=1.0):
                    tag, payload = key.data
                    if tag == "listen":
                        self._accept()
                    elif tag == "chld":
                        self._drain_chld()
                        self._reap()
                        if self._shutdown_requested:
                            self._orderly_shutdown()
                    elif tag == "conn":
                        if mask & selectors.EVENT_WRITE:
                            self._writable(payload)
                        if mask & selectors.EVENT_READ and self._connected(payload):
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
        except OSError as exc:
            if exc.errno in (errno.EMFILE, errno.ENFILE):
                now = time.monotonic()
                if now - self._accept_log_at >= 60:
                    print(f"supervisor: accept: {exc}", file=sys.stderr, flush=True)
                    self._accept_log_at = now
            return
        try:
            uid = peer_uid(conn_sock)
        except OSError:
            conn_sock.close()
            return
        if uid is not None and uid != os.getuid():
            conn_sock.close()  # same-uid gate (ss1)
            return
        conn_sock.setblocking(False)
        conn = _Conn(conn_sock)
        self._conns[conn_sock.fileno()] = conn
        self._sel.register(conn_sock, selectors.EVENT_READ, ("conn", conn))

    def _readable(self, conn: _Conn) -> None:
        """A client's framing failure must not kill its siblings' lifelines."""
        if not self._connected(conn) or conn.paused:
            return
        try:
            self._read_conn(conn)
        except OSError as exc:
            self._client_error(conn, exc)
        except Exception as exc:  # noqa: BLE001 -- isolate this connection
            print(f"supervisor: client read: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _client_error(self, conn: _Conn, exc: OSError) -> None:
        """DL-210: only a named GONE error may close an accepted socket."""
        if isinstance(exc, (BlockingIOError, InterruptedError)) or exc.errno in (
            errno.ENOBUFS,
            errno.ENOMEM,
        ):
            return
        if exc.errno in (
            errno.EPIPE,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ENOTCONN,
            errno.EBADF,
        ):
            self._drop_conn(conn)
        else:
            print(f"supervisor: client socket: errno={exc.errno}", file=sys.stderr, flush=True)

    def _read_conn(self, conn: _Conn) -> None:
        self._dispatch_buffer(conn)
        if conn.paused or not self._running or not self._connected(conn):
            return
        room = self._request_line_limit - len(conn.buf)
        chunk = conn.sock.recv(min(65536, room))
        if not chunk:
            self._drop_conn(conn)
            return
        conn.buf += chunk
        self._dispatch_buffer(conn)

    def _dispatch_buffer(self, conn: _Conn) -> None:
        while not conn.paused and self._connected(conn):
            if b"\n" not in conn.buf:
                if len(conn.buf) >= self._request_line_limit:
                    if not conn.discarding:
                        self._dispatch(conn, b"", too_large=True)
                    conn.discarding = True
                    conn.buf = b""
                elif conn.discarding:
                    conn.buf = b""
                return
            line, conn.buf = conn.buf.split(b"\n", 1)
            if conn.discarding:
                conn.discarding = False
            else:
                self._dispatch(conn, line)
            if not self._running:
                return

    def _connected(self, conn: _Conn) -> bool:
        return self._conns.get(conn.sock.fileno()) is conn

    def _drop_conn(self, conn: _Conn) -> None:
        if not self._connected(conn):
            return
        with contextlib.suppress(KeyError, OSError, ValueError):
            self._sel.unregister(conn.sock)
        self._conns.pop(conn.sock.fileno(), None)
        if self.lease is not None and self.lease.conn is conn:
            self.lease.conn = None  # pushes drop until the holder re-ACQUIREs
        conn.sock.close()
        conn.buf = b""
        conn.out.clear()
        conn.backlog = 0

    def _send(
        self, conn: _Conn, obj: dict[str, Any], *, dropped: tuple[_Lease, int] | None = None
    ) -> None:
        if not self._connected(conn):
            return
        frame = json.dumps(obj, sort_keys=True).encode("utf-8") + b"\n"
        conn.out.append((frame, dropped))
        conn.backlog += len(frame)
        conn.paused = conn.backlog >= self._backlog_bytes
        self._interest(conn)

    def _interest(self, conn: _Conn) -> None:
        events = selectors.EVENT_WRITE if conn.out else 0
        if not conn.paused and self._running:
            events |= selectors.EVENT_READ
        if events:
            self._sel.modify(conn.sock, events, ("conn", conn))

    def _writable(self, conn: _Conn, *, resume: bool = True) -> None:
        if not self._connected(conn):
            return
        budget = 65536  # yield to reaping, other clients and the deadman
        try:
            while conn.out and budget > 0:
                frame, dropped = conn.out[0]
                count = conn.sock.send(memoryview(frame)[:budget])
                if count == 0:
                    return
                conn.backlog -= count
                budget -= count
                if count == len(frame):
                    conn.out.popleft()
                    if dropped is not None:
                        lease, drops = dropped
                        if lease.drops == drops:
                            lease.pushes_dropped = False
                else:
                    conn.out[0] = (frame[count:], dropped)
        except OSError as exc:
            self._client_error(conn, exc)
            return
        was_paused = conn.paused
        conn.paused = conn.backlog >= self._backlog_bytes
        if was_paused and not conn.paused and resume and self._running:
            # Kernel readability may be false: pipelined requests already
            # in our buffer still go first when the output pause clears.
            self._readable(conn)
        if self._connected(conn):
            self._interest(conn)

    # -- request dispatch ---------------------------------------------------

    def _dispatch(self, conn: _Conn, line: bytes, *, too_large: bool = False) -> None:
        if not too_large and not line.strip():
            return
        previous = self.lease
        answer = (
            {"ok": False, "error": "request_too_large"} if too_large else self._answer(conn, line)
        )
        dropped = None
        # RELEASE's final reply still acknowledges the lease it just ended.
        lease = self.lease if self.lease is not None else previous
        if lease is not None and lease.conn is conn and lease.pushes_dropped:
            answer["pushes_dropped"] = True
            dropped = (lease, lease.drops)
        self._send(conn, answer, dropped=dropped)

    def _answer(self, conn: _Conn, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, RecursionError):
            return {"ok": False, "error": "malformed_json"}
        if not _is_wire_int_equal(req.get("v"), PROTOCOL_VERSION):
            return {"ok": False, "error": "unsupported_version"}
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
            return {"ok": False, "error": "unknown_verb"}
        try:
            answer = handler(conn, req)
        except Exception as exc:  # noqa: BLE001 -- the belt under every verb
            # a handler that raises would end this process, and its death
            # EOFs the lifeline of every wrapper on the host. Whatever went
            # wrong is ONE request's problem; it is answered, and the tier
            # that must outlive the engine keeps running.
            answer = {"ok": False, "error": f"internal: {type(exc).__name__}: {exc}"}
        return answer

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
        if (
            not self._lease_active()
            or self.lease is None
            or not _is_wire_int_equal(req.get("token"), self.lease.token)
        ):
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
            incumbent = req.get("incarnation") == self.incarnation and _is_wire_int_equal(
                req.get("token"), self.lease.token
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
        previous = self.lease
        self.lease = _Lease(controller_id, token, time.monotonic() + ttl_s, expires_at, conn)
        if previous is not None and previous.holder == controller_id:
            self.lease.pushes_dropped = previous.pushes_dropped
            self.lease.drops = previous.drops
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
            or "\x00" in job
            or job in (".", "..")
            or not is_wire_int(run_number)
        ):
            return {"ok": False, "error": "bad_spec", "detail": "spec.job / spec.run_number"}
        # NUL is checked HERE, ahead of the two realpath calls below, because
        # os.path takes a NUL as a ValueError and not an OSError: an embedded
        # null in `job` or `run_dir` used to reach realpath and answer
        # `internal:` from the dispatcher's belt instead of `bad_spec`
        # (DL-151). `_spec_schema_error` scans every other string field.
        incoming = spec.get("run_dir")
        if not isinstance(incoming, str) or "\x00" in incoming:
            return {"ok": False, "error": "bad_spec", "detail": "spec.run_dir is not a path"}
        run_dir = self.run_dir_for(job, run_number)
        if os.path.realpath(incoming) != os.path.realpath(run_dir):
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
            fsync_dir(run_dir)
            fsync_dir(os.path.dirname(run_dir))
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
        if self.lease is None:
            return
        if not self._lease_active() or self.lease.conn is None or self.lease.conn.paused:
            self.lease.pushes_dropped = True
            self.lease.drops += 1
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
        # One deadline for all clients, including a SHUTDOWN reply. No
        # requests are dispatched after shutdown, even if reads are ready.
        self._running = False
        deadline = time.monotonic() + 2.0
        while any(conn.out for conn in self._conns.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, mask in self._sel.select(timeout=remaining):
                if key.data[0] == "conn" and mask & selectors.EVENT_WRITE:
                    self._writable(key.data[1], resume=False)
        for conn in list(self._conns.values()):
            self._drop_conn(conn)
        if self._listen is not None:
            with contextlib.suppress(Exception):
                self._listen.close()
        if self._published:
            with contextlib.suppress(OSError):
                if os.stat(self.sock_path).st_ino == self._socket_inode:
                    os.unlink(self.sock_path)
        if self._published or self._private_bound:
            with contextlib.suppress(RecursionError):
                record = _load_json(self.pid_path)
                if record is not None and record.get("incarnation") == self.incarnation:
                    with contextlib.suppress(OSError):
                        os.unlink(self.pid_path)
        if self._private_bound:
            with contextlib.suppress(OSError):
                os.unlink(self._private_path)
        for run in self.runs.values():
            if run.wrapper_rc is None:
                with contextlib.suppress(OSError):
                    os.close(run.lifeline_w)
        for fd in (self._chld_r, self._chld_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        self._sel.close()
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None


#: the frozen ss2 wrapper-input schema, as (key, predicate) -- the whole of
#: it, because a fingerprint over a spec with an unpinned key type is not
#: collision-free, and an unvalidated field that only explodes after the
#: fork kills the one process whose death EOFs every wrapper on the host.
#: `grace_seconds` must be FINITE (DL-151): an `Infinity` passed the old
#: `>= 0.0` test, and SHUTDOWN escalates TERM->KILL after that many seconds,
#: so one such spec made the whole shutdown unbounded -- a supervisor that
#: never exits and a run root that can never be rerouted.
_SPEC_SCHEMA: dict[str, Any] = {
    "version": is_wire_int,
    "run_id": lambda v: isinstance(v, str),
    "job": lambda v: isinstance(v, str),
    "run_number": is_wire_int,
    "command": lambda v: isinstance(v, str),
    "run_dir": lambda v: isinstance(v, str),
    "stdout_path": lambda v: isinstance(v, str),
    "stderr_path": lambda v: isinstance(v, str),
    "stdin_path": lambda v: v is None or isinstance(v, str),
    "grace_seconds": lambda v: (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
        and float(v) >= 0.0
    ),
    "lifeline_fd": is_wire_int,
}

#: lifeline_fd is the supervisor's to fill; a retry may carry a stale one
#: (the fingerprint strips it), but nothing else may be missing
_SPEC_OPTIONAL = frozenset({"lifeline_fd"})


def _spec_schema_error(spec: dict[str, Any]) -> str | None:
    """The frozen ss2 shape, or the first reason it is not. Unknown keys
    REFUSE: the protocol is frozen, so a key this schema does not pin is a
    key whose type is not pinned either -- and the fingerprint's typed float
    encoding is only injective over pinned types.

    A NUL in any string field refuses too (DL-151). It is not a type error,
    so the predicates above cannot see it, and nothing downstream can either:
    `os.open`, `subprocess.Popen` and `os.path` all raise ValueError for an
    embedded null, which is not the OSError the wrapper guards with. A NUL in
    `command` or a path therefore killed the wrapper AFTER the fork, with no
    `status.json` written -- the E7 case the wrapper is built never to
    produce."""
    for key in spec:
        if key not in _SPEC_SCHEMA:
            return f"unknown spec key {key!r}"
    for key, check in _SPEC_SCHEMA.items():
        if key not in spec:
            if key in _SPEC_OPTIONAL:
                continue
            return f"spec.{key} is missing"
        value = spec[key]
        if not check(value):
            return f"spec.{key} has the wrong type"
        if isinstance(value, str) and "\x00" in value:
            return f"spec.{key} holds a NUL"
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
    if not is_wire_int(pid) or not isinstance(spawned_at, str):
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
    and reading that as absence would erase evidence (ss11a).

    Bytes that are not UTF-8 are the same kind of unreadable and take the
    same answer: `json.load` raises `UnicodeDecodeError` for them, which is
    not a `JSONDecodeError`, so before DL-151 it escaped to the dispatcher's
    belt and answered `internal:`."""
    try:
        with open(path, "rb") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return None
    except OSError:
        return _INVALID
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _INVALID
    if not isinstance(loaded, dict) or not spool_version_supported(loaded):
        return _INVALID
    return loaded


#: the ss11a tombstone schemas: required keys and their type checks, per
#: file. A record answering a replay is EVIDENCE, and evidence with a
#: missing or mistyped field is refused, not partially believed.
#: required on EVERY tombstone: `canon.decode` refuses a version it does not
#: implement but passes an ABSENT one (whether a version is required is the
#: reader's call -- canon.check_artifact_version), and this reader requires
#: it: an unversioned record is unsupported evidence.
_VERSIONED = {"artifact_format_version": lambda v: is_wire_int(v) and v == ARTIFACT_FORMAT_VERSION}

_TOMBSTONE_SCHEMAS: dict[str, dict[str, Any]] = {
    "index": {
        **_VERSIONED,
        "run_id": lambda v: isinstance(v, str),
        "job": lambda v: isinstance(v, str),
        "run_number": is_wire_int,
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
        "wrapper_pid": is_wire_int,
        "spawned_at": lambda v: isinstance(v, str),
    },
}


def _load_tombstone(path: str, kind: str) -> dict[str, Any] | None:
    """A supervisor-written ss11a record, read through the ss3.2 ingress:
    duplicate keys, floats, non-scalar strings and an artifact_format_version
    this binary does not implement all refuse (PR-08d/PR-12), and so do bytes
    that are not UTF-8 and a record missing a required field -- `json.load`
    would accept all of them and let a replay answer from unsupported
    evidence."""
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
    if args.deadman_seconds is not None and not (
        math.isfinite(args.deadman_seconds) and args.deadman_seconds > 0
    ):
        # FINITE and positive, in that order (DL-151). `nan` fails every
        # comparison, so `<= 0` let it through and the interval then fired on
        # the first tick -- a supervisor that exits at once and takes every
        # wrapper with it. `inf` passed the same gate and never fired, which
        # is `--deadman-seconds` spelled as no deadman at all.
        print("supervisor: --deadman-seconds must be a finite positive number", file=sys.stderr)
        return 2
    # ENOENT on the run_root is a caller bug (the engine makes it first)
    if not os.path.isdir(args.run_root):
        print(f"supervisor: run-root {args.run_root!r} does not exist", file=sys.stderr)
        return 2
    try:
        supervisor = Supervisor(args.run_root, deadman_s=args.deadman_seconds)
    except ValueError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 1
    try:
        return supervisor.run()
    except OSError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # lock contention or the legacy live-owner gate
        print(str(exc), file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
