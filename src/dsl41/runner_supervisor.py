"""Supervisor: the Tier-1 availability process.

Spec: docs/runner-design.md ss6a (Tier 1), docs/supervisor-protocol.md ss5 (DL-41a,
DL-42, DL-48). STDLIB ONLY: it imports nothing third-party and nothing from dsl41,
the extraction boundary it shares with runner_wrapper.py (DL-42; the import-graph
test is in tests/test_runner_supervisor.py). The only non-stdlib imports are three
sibling stdlib-only modules loaded by file path (DL-72): runner_procid, canon (the
ss3.2 canonical form of the ss11a tombstone files) and state_machine (the executor
that runs the process and lease statecharts). The engine runs it BY FILE PATH
(``sys.executable <this file> --run-root <root>``), never ``-m``, which would import
the dsl41 package __init__ and its third-party imports.

Purpose (ss6a Tier 1): a tethered engine kills its jobs when it dies, because the
wrappers' lifelines EOF. The supervisor owns the lifelines, so an engine restart
does not kill active work. The engine ACQUIREs a lease, SPAWNs through the
supervisor and REATTACHes on restart; the E4 orphan-adoption problem does not arise
because the jobs' parent never died (DL-41a item 8).

It is deliberately DUMB: it forks wrappers, reaps them, forwards exit notifications
and relays one signal per SIGNAL call. It has no scheduling timers, conditions or
policy, and its only time bounds are lifecycle bounds (supervisor-protocol preamble,
DL-150). The oracle decides kills. Tier 2 (the init system) restarts it; it never
restarts itself.

Protocol (docs/supervisor-protocol.md ss5): JSON lines over a named SOCK_STREAM unix
socket, 0600, with a same-uid peer-cred check on every accept. One request line gets
one response line, except async exit PUSHES to the lease-holding connection.
Read-only verbs (LIST/PING) need no lease; mutating verbs (SPAWN/SIGNAL/SHUTDOWN/MAINTENANCE_STOP)
carry the monotonic fencing token from ACQUIRE.

Linux hardening: PR_SET_CHILD_SUBREAPER (prctl 36) reparents a killed wrapper's
command to the supervisor, not to init.

SPAWN IDEMPOTENCY IS DIRECTORY-BACKED (period-model ss11a, DL-129). The key is
`run_id` and the store is the run directory, not `self.runs`: LIST stays bounded
and a delayed duplicate SPAWN cannot become a second execution once a completed
entry leaves memory. A DETACHED run's directory is created on receipt. Three files
make the tombstone: the `runs/.by_run_id/<run_id>` index, `receipt.json` (written
BEFORE the wrapper is forked) and `reply.json` (the answer as first given). A replay
answers from the directory; the incoming path is read only when there is no index
entry (DL-150, DL-151).

The DEADMAN (S5b, DL-95; docs/concurrency-model.md ss8) is opt-in per run root. With
`--deadman-seconds N`, a supervisor with no LIVE leaseholder for N seconds stops its
loop. Its death EOFs every lifeline, the existing kill path of ss5: each wrapper
takes EOF, kills its group and records in its own time (DL-205). It decides nothing
about what runs. It makes ss8's `evict` PROVABLE, since nothing else in this tier
bounds when a controller-less supervisor's wrappers die. It is opt-in because
tolerating an absent controller is what lets an engine crash and resume with its
runs intact (DL-79); a run root without it is reroutable only by force.
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
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

# DL-72: the durability helpers and the (pid, start-time) PID-reuse guard live in the
# sibling stdlib-only runner_procid, shared with the wrapper. sys.path[0] is this
# file's directory under file-path execution, except with PYTHONSAFEPATH=1, so the
# directory is prepended when missing and removed again: the engine also imports this
# file as a package module, and a library must not leave its directory on sys.path.
#
# That gives two module OBJECTS per shared module: `dsl41.canon.CanonError` does not
# catch the `CanonError` raised here, and the same holds for `StatechartError` from
# `state_machine`. A future in-process caller must catch by the name it imported.
#
# The TYPE_CHECKING branch names the same file under the name mypy maps it to, so
# verify_alive and killpg_quiet stay typed. The else branch is what runs, and the
# import-graph test reads runtime imports.
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
        flock_existing,
        verify_lock_path,
        ServiceContainment,
        stop_guard_refusal,
        proc_is_zombie,
        proc_start_token,
        start_tokens_match,
        valid_start_token,
        durable_write,
        durable_write_json,
        fsync_dir,
        killpg_quiet,
        spool_version_supported,
        utc_now_iso,
        verify_alive,
        write_all,
    )
    from dsl41.state_machine import (
        EXIT_STATECHART_ERROR,
        Context,
        Effects,
        Event,
        Exclusion,
        Executor,
        Outcome,
        Region,
        Row,
        State,
        Statechart,
        StatechartError,
        TimeEvent,
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
        flock_existing,
        verify_lock_path,
        ServiceContainment,
        stop_guard_refusal,
        proc_is_zombie,
        proc_start_token,
        start_tokens_match,
        valid_start_token,
        durable_write,
        durable_write_json,
        fsync_dir,
        killpg_quiet,
        spool_version_supported,
        utc_now_iso,
        verify_alive,
        write_all,
    )
    from state_machine import (  # noqa: E402
        EXIT_STATECHART_ERROR,
        Context,
        Effects,
        Event,
        Exclusion,
        Executor,
        Outcome,
        Region,
        Row,
        State,
        Statechart,
        StatechartError,
        TimeEvent,
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


#: the Tier-0 wrapper, run by file path (never -m), resolved relative to THIS file.
_WRAPPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner_wrapper.py")

#: period-model ss11a: `run_id` is filename-safe by GRAMMAR, checked at the wire. The
#: same pattern is runner_effects.RUN_ID_RE, which this tier may not import (DL-42);
#: the two are one grammar and a change to either is a protocol change.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

#: the run_id -> (job, run_number) index under runs/. It is the FIRST durable write of
#: a SPAWN; "no index entry" means "first application" (ss11a retention floor).
_INDEX_DIR = ".by_run_id"

#: how many COMPLETED runs stay in `self.runs` for LIST (ss11a). The directory is the
#: idempotency store; older completions are read from the spool.
_LIST_COMPLETED_WINDOW = 256


def _is_wire_int_equal(value: object, expected: int) -> bool:
    """Integer identity on the wire, which Python's `==` is not (DL-151).

    A JSON `true` or `1.0` must not pass as protocol version 1 or fencing token 1."""
    return is_wire_int(value) and value == expected


# ------------------------------------------------------- ss11a tombstone files


def _fingerprint_form(value: object) -> object:
    """`value` with every float written as its exact hexadecimal form.

    ss3.2's grammar has no floats and the wrapper input spec (ss2) carries
    `grace_seconds`. Only the fingerprint reads the result."""
    if isinstance(value, float):
        return "float:" + value.hex()
    if isinstance(value, dict):
        return {key: _fingerprint_form(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fingerprint_form(item) for item in value]
    return value


def spec_fingerprint(spec: dict[str, Any]) -> str:
    """`receipt.json`'s `spec_fingerprint` (ss11a): sha256 over the ss3.2 canonical form
    of the wrapper input spec without `lifeline_fd`, which the supervisor fills.

    It hashes the WHOLE body, not `canon.digest`, which strips a top-level `digest`
    key; two specs differing only there would share a fingerprint."""
    body = {key: item for key, item in spec.items() if key != "lifeline_fd"}
    return "sha256:" + hashlib.sha256(canonical_bytes(_fingerprint_form(body))).hexdigest()


def _write_canonical(path: str, record: dict[str, Any]) -> None:
    """Write one ss11a artifact in ss3.2 canonical form by `durable_write`."""
    durable_write(path, canonical_bytes(record))


# --------------------------------------------------------------- peer identity


def peer_uid(sock: socket.socket) -> int | None:
    """The connecting peer's uid for the same-uid gate, or None where the platform has
    no credential (Linux SO_PEERCRED, macOS LOCAL_PEERCRED)."""
    if hasattr(socket, "SO_PEERCRED"):  # Linux
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
        _pid, uid, _gid = struct.unpack("iII", data)
        return int(uid)
    if sys.platform == "darwin":
        sol_local, local_peercred = 0, 0x001
        # struct xucred: u_int cr_version; uid_t cr_uid; short cr_ngroups;
        # gid_t cr_groups[16] -> 76 bytes; cr_uid is the second u_int
        raw = sock.getsockopt(sol_local, local_peercred, 76)
        _version, cr_uid = struct.unpack("=II", raw[:8])
        return int(cr_uid)
    return None  # pragma: no cover -- POSIX-only tier


# ------------------------------------------------------------------ the daemon

#: A forced maintenance group: its pgid and its contained members' start tokens (DL-394).
_Group = tuple[int, dict[int, str]]

#: The least interval between containment samples of forced groups. Each sample runs the
#: manager check; every KILL takes its own sample first (DL-394).
_MAINTENANCE_SAMPLE_S = 0.25

#: MAINTENANCE_STOP's own request version (DL-401). Version 2 is answered at admission and
#: the operation runs in the loop. Version 1, answered only at completion, is refused by name.
MAINTENANCE_STOP_VERSION = 2
_MAINTENANCE_STOP_V1_RETIRED = (
    "MAINTENANCE_STOP v 1, answered only at completion, is retired by DL-401; send v 2"
)

#: `<run_root>/maintenance.json`: the durable state of the last admitted maintenance on the
#: root (DL-401). One JSON object, written as spawn.json is; a reader refuses another version.
MAINTENANCE_RECORD = "maintenance.json"
MAINTENANCE_RECORD_VERSION = 1
#: the version of the start note each supervisor writes into supervisor.lock (DL-401)
START_NOTE_VERSION = 1

#: The parts of `bound_s`, the supervisor's estimate of an operation under its latency model
#: (DL-401); the ladder never stops at it. Forced live work waits for spawn records, the
#: longest grace and the KILL settle; each live group has two verified sends, TERM and KILL,
#: at up to 4 s each; the samples between them have their own allowance. Teardown is two
#: idle proofs of up to 5 s each, the owned-file teardown, the two-second reply flush and
#: the exit.
_MAINTENANCE_SPAWN_WAIT_S = 5.0
_MAINTENANCE_SETTLE_S = 2.0
_MAINTENANCE_SEND_S = 4.0
_MAINTENANCE_SAMPLES_S = 10.0
_MAINTENANCE_TEARDOWN_S = 15.0


def _is_bound(value: object) -> bool:
    """A finite number of zero or more, never a boolean."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


#: what every maintenance record holds, by field (DL-401)
_RECORD_FIELDS: dict[str, Any] = {
    "state": lambda v: v in ("admitted", "completed", "uncertain"),
    "incarnation": lambda v: isinstance(v, str) and bool(v),
    "supervisor_pid": lambda v: is_wire_int(v) and v > 0,
    "supervisor_start_time": valid_start_token,
    "boot_id": lambda v: isinstance(v, str) and bool(v),
    "force": lambda v: v is None or v == "terminate",
    "bound_s": _is_bound,
    "started_at": lambda v: isinstance(v, str),
}


def read_maintenance_record(run_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    """`<run_root>/maintenance.json`, or None when the file does not exist (DL-401).

    Unknown fields are ignored. A record that is not one JSON object, names a `version`
    other than MAINTENANCE_RECORD_VERSION or none, or lacks or mistypes a field raises
    ValueError: every reader refuses it rather than guess what it says."""
    path = os.path.join(os.fspath(run_root), MAINTENANCE_RECORD)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{path} cannot be read: {exc}") from exc
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{path} is not one JSON object: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"{path} is not one JSON object")
    version = record.get("version")
    if not _is_wire_int_equal(version, MAINTENANCE_RECORD_VERSION):
        raise ValueError(
            f"{path} has version {version!r}; this reader implements"
            f" {MAINTENANCE_RECORD_VERSION} (DL-401)"
        )
    fields = dict(_RECORD_FIELDS)
    if record.get("state") != "admitted":
        fields["finished_at"] = lambda v: isinstance(v, str)
    if record.get("state") == "uncertain":
        fields["detail"] = lambda v: isinstance(v, str)
    bad = sorted(name for name, valid in fields.items() if not valid(record.get(name)))
    if bad:
        raise ValueError(f"{path} lacks or mistypes {', '.join(bad)} (DL-401)")
    return record


def record_owner_problem(run_root: str | os.PathLike[str], record: dict[str, Any]) -> str | None:
    """Why `record` is not the outcome of the root's last supervisor, or None (DL-401). Each
    start notes its incarnation in supervisor.lock and then writes supervisor.pid. The note
    must be well formed and name the record's incarnation: a missing, empty, partial or
    unparsable note is no evidence, and a record needs evidence. A pid record, when present,
    must name it too; one that names another incarnation is a later supervisor that ended
    outside maintenance."""
    root = os.fspath(run_root)
    named: list[tuple[str, object]] = []
    for name in ("supervisor.lock", "supervisor.pid"):
        try:
            with open(os.path.join(root, name), "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            if name == "supervisor.lock":
                return f"{name} is missing: no start note names the record's supervisor"
            continue
        except OSError as exc:
            return f"{name} cannot be read: {exc}"
        try:
            note = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return f"{name} is not one JSON object"
        if not isinstance(note, dict):
            return f"{name} is not one JSON object"
        if name == "supervisor.lock" and not _is_wire_int_equal(
            note.get("version"), START_NOTE_VERSION
        ):
            return f"{name} has version {note.get('version')!r}; this reader implements 1 (DL-401)"
        named.append((name, note.get("incarnation")))
    for name, incarnation in named:
        if incarnation != record["incarnation"]:
            return (
                f"{name} names another supervisor ({incarnation}), which ended outside"
                " maintenance: not a maintenance outcome; read the unit and the log"
            )
    return None


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    """Every byte at `offset`, or an error: `os.pwrite` may write fewer bytes than given."""
    written = 0
    while written < len(data):
        count = os.pwrite(fd, data[written:], offset + written)
        if count <= 0:  # pragma: no cover -- POSIX: 0 only for an empty write
            raise OSError(f"short write: {written} of {len(data)} bytes")
        written += count


class _Maintenance:
    """An admitted maintenance operation (DL-401): its record, the containment proof it was
    admitted under, the forced ladder (None when admission proved idle) and the pause the
    loop takes before the next pass."""

    __slots__ = ("record", "proof", "ladder", "pause")

    def __init__(
        self,
        record: dict[str, Any],
        proof: ServiceContainment,
        ladder: Iterator[float] | None,
    ) -> None:
        self.record = record
        self.proof = proof
        self.ladder = ladder
        self.pause = 0.0


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


# --------------------------------------------------------- state machines
#
# Two statecharts (docs/statechart-engine.md) built on the shared core (state_machine.py,
# reached by path like canon) run in each Supervisor's one executor: `supervisor_lease`
# for the lease and `supervisor_process` for the process lifecycle. The lease chart's
# guards, registered in both charts, are the one fence of the mutating verbs.

#: The process lifecycle: the leaf states of SUPERVISOR_PROCESS.
type ProcessState = Literal[
    "starting",
    "bound",
    "serving",
    "shutting_down",
    "stopped",
    "refused",
    "closed",
    "maintenance_checking",
    "maintenance_stopping",
    "maintenance_uncertain",
    "maintenance_completing",
    "maintenance_completed",
]

_SP5 = "supervisor-protocol ss5"

# ---------------------------------------------------------- the lease statechart
#
# The lease rows pick every move of the lease. Their guards and actions are the only
# code that reads a lease verb's request or writes the holder, token, deadline, expiry
# or connection; `_push_exit` and `_writable` also write the dropped-push notice
# (`pushes_dropped`, `drops`). The supervisor posts a lease verb under its own name,
# `disconnect` for every connection it drops while open, and `expire`, produced by
# sampling the monotonic deadline wherever it reads the lease, once per deadline.
#
# Ties: the deadline is sampled before anything else at that instant, and the
# deadline instant is due. An expiry and a RENEW or RELEASE at the same instant
# resolve expiry first: the verb is refused stale_token, and an ACQUIRE may be granted.

#: the lease's instance key in the supervisor's executor: one lease per process.
_LEASE = "lease"
#: the time event of a lease whose deadline has passed. It has no generation: the
#: supervisor samples the deadline synchronously and posts it only while the lease is
#: held, so an expiry is never stale.
EXPIRE = "expire"
#: the event of a connection the supervisor drops, whoever held it.
DISCONNECT = "disconnect"
#: the leaf states of SUPERVISOR_LEASE.
_PHASES = frozenset({"free", "expired", "live", "orphaned"})


class _Request:
    """A verb's request as a chart sees it: the payload of SPAWN, SIGNAL and SHUTDOWN,
    and the base of a lease verb's. The fence guards read `incarnation` and `token`."""

    __slots__ = ("req",)

    def __init__(self, req: dict[str, Any]) -> None:
        self.req = req

    def __repr__(self) -> str:
        """The request without its token, for a statechart error's report."""
        shown = {key: value for key, value in self.req.items() if key != "token"}
        return f"{type(self).__name__}({shown!r})"


class _LeaseRequest(_Request):
    """A lease verb as the chart sees it: the request, its connection, and the monotonic
    `now` and wall-clock `wall` instants the supervisor sampled.

    An error computing `ttl_s` or the expiry is kept, not raised: the guards read it,
    and for an `internal` refusal the supervisor raises it so the dispatch belt answers
    and nothing is written (DL-300)."""

    __slots__ = ("conn", "now", "wall", "_ttl", "_expiry")

    def __init__(self, req: dict[str, Any], conn: _Conn, now: float, wall: float) -> None:
        super().__init__(req)
        self.conn = conn
        self.now = now
        self.wall = wall
        self._ttl: float | Exception | None = None
        self._expiry: tuple[float, str] | Exception | None = None

    def ttl(self) -> float | Exception:
        """ttl_s as a number, 60 when it is absent, or the error converting it raised."""
        if self._ttl is None:
            try:
                self._ttl = float(self.req.get("ttl_s", 60))
            except Exception as exc:  # noqa: BLE001 -- kept for the belt to answer
                self._ttl = exc
        return self._ttl

    def expiry(self) -> tuple[float, str] | Exception:
        """The monotonic deadline and `expires_at` that ttl_s gives, or the error computing
        them. A zero or negative ttl_s gives a deadline that is already due (ss5)."""
        if self._expiry is None:
            ttl_s = self.ttl()
            if isinstance(ttl_s, Exception):
                self._expiry = ttl_s
            else:
                try:
                    expires_at = datetime.fromtimestamp(self.wall + ttl_s, UTC).isoformat()
                    self._expiry = (self.now + ttl_s, expires_at)
                except Exception as exc:  # noqa: BLE001 -- kept for the belt to answer
                    self._expiry = exc
        return self._expiry

    def error(self) -> Exception:
        """The error computing the expiry raised; only an `internal` refusal asks."""
        expiry = self.expiry()
        assert isinstance(expiry, Exception)
        return expiry


# -- the guards. Each docstring is its rows' prose.


def _named(ctx: Context) -> bool:
    controller_id = ctx.event.payload.req.get("controller_id")
    return isinstance(controller_id, str) and controller_id != ""


def _timed(ctx: Context) -> bool:
    return not isinstance(ctx.event.payload.expiry(), Exception)


def _numeric_ttl(ctx: Context) -> bool:
    """ttl_s is a number, or absent. It reads `ttl()` alone and never computes the expiry."""
    return not isinstance(ctx.event.payload.ttl(), Exception)


def _this_incarnation(ctx: Context) -> bool:
    """The request names this incarnation (DL-80)."""
    return bool(ctx.event.payload.req.get("incarnation") == ctx.data.incarnation)


def _foreign(ctx: Context) -> bool:
    """The request names no incarnation or another one: the supervisor it knew is gone,
    so it must re-acquire and reconcile (DL-80)."""
    return not _this_incarnation(ctx)


def _held(ctx: Context) -> bool:
    """`held` is active in the lease chart: the lease is live or orphaned. A pure read of
    the lease instance; the supervisor posts a due `expire` before the verb."""
    return "held" in ctx.data._machines.active(_LEASE)


def _credentials(ctx: Context) -> bool:
    """This incarnation and the current token of an unexpired lease, an integer on the
    wire (DL-151). Any connection may present them. The one fence of RENEW, RELEASE,
    SPAWN, SIGNAL and SHUTDOWN (ss5)."""
    lease = ctx.data.lease
    return (
        _this_incarnation(ctx)
        and _held(ctx)
        and lease is not None
        and _is_wire_int_equal(ctx.event.payload.req.get("token"), lease.token)
    )


def _stale(ctx: Context) -> bool:
    """This incarnation without credentials: the token is stale or the lease is not held,
    so the request is refused stale_token (ss5). With `foreign` and `credentials` this
    partitions every request."""
    return _this_incarnation(ctx) and not _credentials(ctx)


def _grantable(ctx: Context) -> bool:
    """controller_id is a non-empty string and ttl_s gives a representable expiry. A zero
    or negative ttl_s is grantable: the lease is due at once and the next sample
    expires it (ss5)."""
    return _named(ctx) and _timed(ctx)


def _incumbent(ctx: Context) -> bool:
    """Grantable, with this incarnation and the current token: the incumbent re-keys and
    its old token dies (DL-79, DL-80). controller_id is a label and the token proves
    incumbency, not authenticity; the same-uid peer-cred gate on accept authenticates
    (supervisor-protocol ss5)."""
    return _grantable(ctx) and _credentials(ctx)


def _challenger(ctx: Context) -> bool:
    """controller_id is a non-empty string and ttl_s is a number, without the current
    token: a live lease yields only to its incumbent (DL-79). The expiry is not
    computed."""
    return _named(ctx) and _numeric_ttl(ctx) and not _credentials(ctx)


def _unnamed(ctx: Context) -> bool:
    """controller_id is not a non-empty string; checked before ttl_s and any lease state
    (DL-150)."""
    return not _named(ctx)


def _untimed(ctx: Context) -> bool:
    """controller_id is a non-empty string and ttl_s gives no representable expiry (not a
    number, inf, nan, 1e20). Nothing changes, not even the token counter (DL-300)."""
    return _named(ctx) and not _timed(ctx)


def _renewable(ctx: Context) -> bool:
    """This incarnation and the current token, and ttl_s gives a representable expiry
    (DL-150, DL-300)."""
    return _credentials(ctx) and _timed(ctx)


def _renewal_untimed(ctx: Context) -> bool:
    """This incarnation and the current token, and ttl_s gives no representable expiry;
    nothing changes (DL-300)."""
    return _credentials(ctx) and not _timed(ctx)


def _holder(ctx: Context) -> bool:
    """The dropped connection is the one that holds the lease."""
    lease = ctx.data.lease
    return lease is not None and lease.conn is ctx.event.payload


# -- the actions


def _grant(ctx: Context) -> Effects:
    """Mint the next token and write a new record held on the claimant's connection with
    the request's deadline. A re-grant to the same controller_id keeps the dropped-push
    notice (DL-210). Answer the token, expiry and incarnation (DL-80)."""
    sup: Supervisor = ctx.data
    request: _LeaseRequest = ctx.event.payload
    expiry = request.expiry()
    assert not isinstance(expiry, Exception)  # the row's guard held
    deadline, expires_at = expiry
    token = sup._next_token
    sup._next_token += 1  # monotonic: never regresses while any run is alive
    previous = sup.lease
    holder = request.req["controller_id"]
    sup.lease = _Lease(holder, token, deadline, expires_at, request.conn)
    if previous is not None and previous.holder == holder:
        sup.lease.pushes_dropped = previous.pushes_dropped
        sup.lease.drops = previous.drops
    answer = {"ok": True, "token": token, "expires_at": expires_at, "incarnation": sup.incarnation}
    return Effects(intents=(answer,))


def _extend(ctx: Context) -> Effects:
    """Move the deadline and expires_at to the request's. The connection is not adopted,
    so an orphaned lease stays orphaned and its pushes keep dropping until an ACQUIRE."""
    sup: Supervisor = ctx.data
    expiry = ctx.event.payload.expiry()
    assert sup.lease is not None and not isinstance(expiry, Exception)  # the guard held
    sup.lease.deadline, sup.lease.expires_at = expiry
    return Effects(intents=({"ok": True, "expires_at": sup.lease.expires_at},))


def _release(ctx: Context) -> Effects:
    """Drop the record and answer ok."""
    ctx.data.lease = None
    return Effects(intents=({"ok": True},))


def _forget(ctx: Context) -> None:
    """Forget the holder's connection: pushes drop until the holder re-ACQUIREs, and any
    controller may ACQUIRE an orphaned lease, so a crashed engine's resume need not wait
    out the TTL. This is sound because the kernel closes the AF_UNIX fd only when the
    holder is gone or closes it; a non-local transport would break that (ss5)."""
    ctx.data.lease.conn = None


_LEASE_CITE = f"{_SP5} lease verbs"

#: a lease verb is answered by the one row it takes: an action answers, or a refusal
#: row's code is the answer (with the incarnation for wrong_incarnation and the holder
#: for lease_held). An `internal` refusal is the dispatch belt's `internal:` answer.
#: SPAWN, SIGNAL and SHUTDOWN read the lease and never write it.
SUPERVISOR_LEASE = Statechart(
    name="supervisor_lease",
    root=State(
        "lease",
        regions=(
            Region(
                "phase",
                "unheld",
                (
                    State(
                        "unheld",
                        regions=(Region("record", "free", (State("free"), State("expired"))),),
                    ),
                    State(
                        "held",
                        regions=(Region("connection", "live", (State("live"), State("orphaned"))),),
                        wait=frozenset({EXPIRE}),
                    ),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "supervisor_lease.01",
            "unheld",
            "ACQUIRE",
            "live",
            guard="grantable",
            action="grant",
            cite=f"{_LEASE_CITE}, DL-79, DL-150",
        ),
        Row(
            "supervisor_lease.02",
            "orphaned",
            "ACQUIRE",
            "live",
            guard="grantable",
            action="grant",
            cite=f"{_LEASE_CITE}, DL-79, DL-150",
        ),
        Row(
            "supervisor_lease.03",
            "live",
            "ACQUIRE",
            guard="incumbent",
            action="grant",
            kind="internal",
            cite=f"{_LEASE_CITE}, DL-79, DL-80",
        ),
        Row(
            "supervisor_lease.04",
            "live",
            "ACQUIRE",
            guard="challenger",
            kind="refusal",
            code="lease_held",
            cite=f"{_LEASE_CITE}, DL-79",
        ),
        Row(
            "supervisor_lease.05",
            "lease",
            "ACQUIRE",
            guard="unnamed",
            kind="refusal",
            code="bad_controller_id",
            cite=f"{_LEASE_CITE}, DL-150",
        ),
        Row(
            "supervisor_lease.06",
            "lease",
            "ACQUIRE",
            guard="untimed",
            kind="refusal",
            code="internal",
            cite=f"{_LEASE_CITE}, DL-300",
        ),
        Row(
            "supervisor_lease.07",
            "held",
            "RENEW",
            guard="renewable",
            action="extend",
            kind="internal",
            cite=f"{_LEASE_CITE}, DL-150",
        ),
        Row(
            "supervisor_lease.08",
            "held",
            "RENEW",
            guard="renewal_untimed",
            kind="refusal",
            code="internal",
            cite=f"{_LEASE_CITE}, DL-300",
        ),
        Row(
            "supervisor_lease.09",
            "lease",
            "RENEW",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5} incarnation, DL-80, DL-150",
        ),
        Row(
            "supervisor_lease.10",
            "lease",
            "RENEW",
            guard="this_incarnation",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5} incarnation, DL-150",
        ),
        Row(
            "supervisor_lease.11",
            "held",
            "RELEASE",
            "free",
            guard="credentials",
            action="release",
            cite=f"{_LEASE_CITE}, DL-150",
        ),
        Row(
            "supervisor_lease.12",
            "lease",
            "RELEASE",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5} incarnation, DL-80, DL-150",
        ),
        Row(
            "supervisor_lease.13",
            "lease",
            "RELEASE",
            guard="this_incarnation",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5} incarnation, DL-150",
        ),
        Row(
            "supervisor_lease.14",
            "live",
            DISCONNECT,
            "orphaned",
            guard="holder",
            action="forget",
            cite=f"{_LEASE_CITE}, DL-150",
        ),
        Row(
            "supervisor_lease.15",
            "expired",
            DISCONNECT,
            guard="holder",
            action="forget",
            kind="internal",
            cite=_LEASE_CITE,
        ),
        Row("supervisor_lease.16", "lease", DISCONNECT, kind="ignored", cite=_LEASE_CITE),
        Row("supervisor_lease.17", "held", EXPIRE, "expired", cite=_LEASE_CITE),
        Row("supervisor_lease.18", "lease", EXPIRE, kind="ignored", cite=_LEASE_CITE),
    ),
    guards={
        "grantable": _grantable,
        "incumbent": _incumbent,
        "challenger": _challenger,
        "unnamed": _unnamed,
        "untimed": _untimed,
        "renewable": _renewable,
        "renewal_untimed": _renewal_untimed,
        "foreign": _foreign,
        "this_incarnation": _this_incarnation,
        "credentials": _credentials,
        "holder": _holder,
    },
    actions={"grant": _grant, "extend": _extend, "release": _release, "forget": _forget},
    time_events=(TimeEvent(EXPIRE),),
    clock="monotonic",
)


# -------------------------------------------------------- the process statechart
#
# `supervisor_process` is the supervisor's own lifecycle, one instance in the executor
# that runs the lease. The supervisor runs its procedures (startup probes and bind,
# orderly shutdown with its bounds, teardown) and posts a fact wherever the next step
# depends on the outcome. The row taken decides the move; an action reports an intent
# the supervisor carries out after the post (exit 1; send the queued replies; run the
# wait; unlink what the state owns). Actions never raise and do no I/O.
#
# The supervisor posts:
#   - `start`, once, from `_bind`, with the fact `_start_fact` decides: the lock held,
#     an owner live, or the root free and the private socket bound;
#   - `publish`, once, after the socket is renamed into place (DL-275);
#   - SPAWN, SIGNAL and SHUTDOWN from the dispatch belt, which runs only while
#     `serving`: the fence, after a due `expire` is posted to the lease;
#   - `term`, when the loop consumes a latched SIGTERM or SIGINT;
#   - `drained`, when the shutdown's wait ends: every wrapper reaped, or the bound passed;
#   - `pass_ended`, at the end of a loop pass that an errored SHUTDOWN wait left in
#     `shutting_down`, with no signal latched;
#   - `deadman`, produced by sampling the deadman's deadline at the end of each loop
#     pass while `serving`, posted only when due;
#   - `teardown`, once, from `_teardown`, unless the process is closed. Teardown then
#     sends replies for up to 2 s, drops every connection, carries out the row's unlink,
#     and closes the open lifelines and the lock. After an error that ended startup or
#     the loop, main exits 1, or 7 for a statechart error.
#
# SHUTDOWN's handler clears a latched signal once the fence passes (.05), before the
# wait, so a refused SHUTDOWN leaves the signal latched. A signal latched during a wait
# that ends finds the process stopped (.09); one latched during a wait that errs runs
# it once more (.36), as the end of that pass does with none latched (.12).
#
# Ties: nothing is concurrent, since the loop is single-threaded, every wait blocks it
# and a signal handler only latches. Within a pass the latch is read before the
# end-of-pass `pass_ended` and before the deadman is sampled.
#
# Unreachable cells: a mutating verb is posted only while `serving`, and each
# lifecycle fact only from the states above. The final `closed` ignores every event
# (statechart-engine ss10, DL-380). Every other cell is an exclusion, and an
# out-of-order post raises `UnhandledEvent`, which stops the supervisor with exit 7.

#: the process's instance key in the supervisor's executor.
_PROCESS = "process"
START = "start"
PUBLISH = "publish"
TERM = "term"
DRAINED = "drained"
PASS_ENDED = "pass_ended"
DEADMAN = "deadman"
TEARDOWN = "teardown"
MAINTENANCE_REFUSED = "maintenance_refused"
MAINTENANCE_STOPPING = "maintenance_stopping"
MAINTENANCE_IDLE = "maintenance_idle"
MAINTENANCE_UNCERTAIN = "maintenance_uncertain"
MAINTENANCE_FINISHED = "maintenance_finished"
#: the end of a loop pass of an admitted operation (DL-401)
MAINTENANCE_PASS = "maintenance_pass"
_MAINTENANCE_PHASES = (
    "maintenance_checking",
    "maintenance_stopping",
    "maintenance_uncertain",
    "maintenance_completing",
    "maintenance_completed",
)
#: `start`'s facts, from `_start_fact`
LOCK_HELD = "lock_held"
OWNER_LIVE = "owner_live"
FREE = "free"
#: `drained`'s facts: every wrapper reaped, or the last-resort bound passed
REAPED = "reaped"
PAST_BOUND = "past_bound"
#: the intents of the process chart's actions
REFUSE = "refuse"
FLUSH = "flush"
DRAIN = "drain"
UNLINK_PUBLISHED = "unlink_published"
UNLINK_PRIVATE = "unlink_private"
ADVANCE = "advance"
COMPLETE = "complete"
#: the leaf states of SUPERVISOR_PROCESS.
_PROCESS_PHASES = frozenset(
    {
        "starting",
        "bound",
        "serving",
        "shutting_down",
        "stopped",
        "refused",
        "closed",
        *_MAINTENANCE_PHASES,
    }
)
_MUTATING = ("SPAWN", "SIGNAL", "SHUTDOWN", "MAINTENANCE_STOP")


# -- the guards. Each docstring is its rows' prose.


def _lock_held(ctx: Context) -> bool:
    """supervisor.lock is held: another supervisor owns this root (DL-210)."""
    return bool(ctx.event.payload == LOCK_HELD)


def _owner_live(ctx: Context) -> bool:
    """The lock is taken, and the published socket answers PING within one of three
    attempts across a second or the pid record does not prove its owner absent (DL-210)."""
    return bool(ctx.event.payload == OWNER_LIVE)


def _free(ctx: Context) -> bool:
    """The lock is taken and no other owner is proven present: private sockets are swept,
    the published path is reclaimed and the private socket is bound (DL-210)."""
    return bool(ctx.event.payload == FREE)


# -- the actions


def _refuse(_ctx: Context) -> Effects:
    """Exit 1: another supervisor owns this root; teardown releases only what this process took."""
    return Effects(intents=(REFUSE,))


def _drain(_ctx: Context) -> Effects:
    """Run the shutdown's wait: up to 5 s for missing spawn records, TERM each live
    command group, KILL each after its grace, and KILL every survivor after the longest
    grace plus 2 s. Lifelines stay open until each wrapper exits (DL-48, DL-150)."""
    return Effects(intents=(DRAIN,))


def _retry(_ctx: Context) -> Effects:
    """An error ended the SHUTDOWN's wait: send the queued replies first, that SHUTDOWN's
    `internal:` answer among them, then run the wait once more (DL-300)."""
    return Effects(intents=(FLUSH, DRAIN))


def _unlink_published(_ctx: Context) -> Effects:
    """This incarnation published supervisor.sock. Teardown sends the queued replies for
    up to 2 s, drops every connection and closes the listener; it unlinks supervisor.sock
    if its inode is still the one renamed into place and supervisor.pid if it names this
    incarnation, then closes the open lifelines and the lock. From `up` an error ended
    the loop, and main exits 1, or 7 for a statechart error (DL-210)."""
    return Effects(intents=(UNLINK_PUBLISHED,))


def _advance(_ctx: Context) -> Effects:
    """Run the forced ladder to its next pause: up to 5 s for missing spawn records, TERM
    each verified command group, and KILL each member that outlives its run's grace, until
    every wrapper is reaped and every group has left containment. Passing the operation's
    bound is uncertain (DL-394, DL-401)."""
    return Effects(intents=(ADVANCE,))


def _complete(_ctx: Context) -> Effects:
    """Prove idle again, verify and remove the published socket and pid record, close the
    listener and the child pipe, and record the operation completed (DL-390, DL-401)."""
    return Effects(intents=(COMPLETE,))


def _unlink_private(_ctx: Context) -> Effects:
    """This incarnation bound its private socket and an error ended startup before the
    publish. Teardown closes the listener, unlinks supervisor.pid if it names this
    incarnation and the private socket, then closes the lock; main exits 1, or 7 for a
    statechart error (DL-210)."""
    return Effects(intents=(UNLINK_PRIVATE,))


_ONLY = "the supervisor posts {event} only {where}"
_VERBS = (
    "a mutating verb is dispatched only while serving: no request is read before"
    " publish, the shutdown runs inside its own dispatch, and teardown drops every"
    " connection after the move to closed"
)
_PROCESS_UNREACHABLE: dict[str, tuple[tuple[str, ...], str]] = {
    START: (("bound", "up", "ended"), _ONLY.format(event=START, where="from starting")),
    PUBLISH: (("starting", "up", "ended"), _ONLY.format(event=PUBLISH, where="from bound")),
    TERM: (
        ("starting", "bound", "refused"),
        _ONLY.format(
            event=TERM, where="from the serving loop, in serving, shutting_down or stopped"
        ),
    ),
    DRAINED: (
        ("starting", "bound", "serving", "ended"),
        _ONLY.format(event=DRAINED, where="when a wait ends, in shutting_down"),
    ),
    PASS_ENDED: (
        ("starting", "bound", "serving", "ended"),
        _ONLY.format(event=PASS_ENDED, where="in shutting_down"),
    ),
    DEADMAN: (
        ("starting", "bound", "shutting_down", "ended"),
        _ONLY.format(event=DEADMAN, where="in serving"),
    ),
    **{verb: (("starting", "bound", "shutting_down", "ended"), _VERBS) for verb in _MUTATING},
}


# Only procedure result facts select maintenance moves; legacy drain facts cannot (DL-390).
for _event, _allowed in {
    MAINTENANCE_REFUSED: {"maintenance_checking"},
    MAINTENANCE_STOPPING: {"maintenance_checking"},
    MAINTENANCE_IDLE: {"maintenance_checking", "maintenance_stopping"},
    MAINTENANCE_UNCERTAIN: {"maintenance_stopping", "maintenance_completing"},
    MAINTENANCE_FINISHED: {"maintenance_completing"},
}.items():
    _PROCESS_UNREACHABLE[_event] = (
        tuple(sorted(_PROCESS_PHASES - _allowed - {"closed"})),
        "maintenance facts are posted only by their owning procedure (DL-390)",
    )
_PROCESS_UNREACHABLE[MAINTENANCE_PASS] = (
    tuple(sorted(_PROCESS_PHASES - {"maintenance_stopping", "maintenance_completing", "closed"})),
    "the loop posts maintenance_pass only while an admitted operation runs (DL-401)",
)
for _event in (START, PUBLISH, DRAINED, PASS_ENDED, *_MUTATING):
    _states, _reason = _PROCESS_UNREACHABLE[_event]
    _extra = set(_MAINTENANCE_PHASES)
    if _event in _MUTATING:
        # an uncertain or a running operation refuses them (DL-390, DL-401)
        _extra -= {"maintenance_uncertain", "maintenance_stopping", "maintenance_completing"}
    _PROCESS_UNREACHABLE[_event] = (tuple((*_states, *sorted(_extra))), _reason)

for _event in (TERM, DEADMAN):
    _states, _reason = _PROCESS_UNREACHABLE[_event]
    _PROCESS_UNREACHABLE[_event] = ((*_states, "maintenance_checking"), _reason)

#: An admitted operation runs to its outcome: the loop holds a latched signal and samples
#: no deadman until maintenance_uncertain or the exit (DL-401).
_HELD_WHILE_MAINTAINING = tuple(
    Exclusion(
        "maintaining",
        event,
        f"the loop holds {event} while an admitted operation runs, until its outcome (DL-401)",
    )
    for event in (TERM, DEADMAN)
)

#: the supervisor's own lifecycle (ss5, DL-210, DL-289). Rows 01 to 12 and 36 are the
#: lifecycle, 13 to 20 the fence, 21 to 30 the events ignored once closed, and 31 to 35
#: teardown, a row per source state in three ownership classes; 10 and 11 are out of use
#: (DL-378). Rows 37 to 60 are guarded maintenance (DL-390): admission answers in the
#: request, and 55 and 56 run the admitted operation one loop pass at a time (DL-401).
#: A fence refusal is answered with its code, and with the incarnation for
#: wrong_incarnation.
#: SIGKILL ends the process in any state and runs no code, so no row is taken for it:
#: each wrapper takes lifeline EOF and records in its own time (ss5, DL-205).
SUPERVISOR_PROCESS = Statechart(
    name="supervisor_process",
    root=State(
        "process",
        regions=(
            Region(
                "phase",
                "open",
                (
                    State(
                        "open",
                        regions=(
                            Region(
                                "stage",
                                "starting",
                                (
                                    State("starting"),
                                    State("bound"),
                                    State(
                                        "up",
                                        regions=(
                                            Region(
                                                "service",
                                                "serving",
                                                (
                                                    State("serving"),
                                                    State("shutting_down"),
                                                    State("maintenance_checking"),
                                                    State(
                                                        "maintaining",
                                                        regions=(
                                                            Region(
                                                                "operation",
                                                                "maintenance_stopping",
                                                                (
                                                                    State("maintenance_stopping"),
                                                                    State("maintenance_completing"),
                                                                ),
                                                            ),
                                                        ),
                                                    ),
                                                    State("maintenance_uncertain"),
                                                ),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                    State(
                        "ended",
                        regions=(
                            Region(
                                "outcome",
                                "stopped",
                                (
                                    State("stopped"),
                                    State("refused"),
                                    State("maintenance_completed"),
                                ),
                            ),
                        ),
                    ),
                    State("closed", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "supervisor_process.01",
            "starting",
            START,
            "refused",
            guard="lock_held",
            action="refuse",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.02",
            "starting",
            START,
            "refused",
            guard="owner_live",
            action="refuse",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.03",
            "starting",
            START,
            "bound",
            guard="free",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.04",
            "bound",
            PUBLISH,
            "serving",
            cite=f"{_SP5}, DL-210, DL-275",
            note=(
                "The private socket listens with mode 0600, supervisor.pid is written,"
                " and the socket is renamed to supervisor.sock."
            ),
        ),
        Row(
            "supervisor_process.05",
            "serving",
            "SHUTDOWN",
            "shutting_down",
            guard="credentials",
            cite=f"{_SP5} SHUTDOWN, DL-80",
        ),
        Row(
            "supervisor_process.06",
            "serving",
            TERM,
            "shutting_down",
            action="drain",
            cite=f"{_SP5} SHUTDOWN, DL-275, DL-300",
        ),
        Row(
            "supervisor_process.07",
            "shutting_down",
            DRAINED,
            "stopped",
            cite=f"{_SP5} SHUTDOWN, DL-48, DL-150",
            note=(
                "The shutdown's wait ended: every wrapper reaped, or the last-resort"
                " bound passed and every survivor's group was killed."
            ),
        ),
        Row(
            "supervisor_process.08",
            "serving",
            DEADMAN,
            "stopped",
            cite=f"{_SP5} The deadman, DL-95",
            note=(
                "A deadman is set and no live leaseholder for that many seconds;"
                " the supervisor logs the reason first."
            ),
        ),
        Row(
            "supervisor_process.09",
            "stopped",
            TERM,
            kind="internal",
            cite=f"{_SP5} SHUTDOWN",
            note="A signal latched during a shutdown that already ran in this loop pass. Nothing runs.",
        ),
        Row(
            "supervisor_process.12",
            "shutting_down",
            PASS_ENDED,
            action="retry",
            kind="internal",
            cite=f"{_SP5} SHUTDOWN, DL-300",
        ),
        Row(
            "supervisor_process.13",
            "serving",
            "SPAWN",
            guard="credentials",
            kind="internal",
            cite=f"{_SP5} mutating verbs, DL-80",
        ),
        Row(
            "supervisor_process.14",
            "serving",
            "SIGNAL",
            guard="credentials",
            kind="internal",
            cite=f"{_SP5} mutating verbs, DL-80",
        ),
        Row(
            "supervisor_process.15",
            "serving",
            "SPAWN",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.16",
            "serving",
            "SPAWN",
            guard="stale",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.17",
            "serving",
            "SIGNAL",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.18",
            "serving",
            "SIGNAL",
            guard="stale",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.19",
            "serving",
            "SHUTDOWN",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.20",
            "serving",
            "SHUTDOWN",
            guard="stale",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5} mutating verbs, DL-80, DL-229",
        ),
        Row(
            "supervisor_process.31",
            "up",
            TEARDOWN,
            "closed",
            action="unlink_published",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.32",
            "stopped",
            TEARDOWN,
            "closed",
            action="unlink_published",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.33",
            "bound",
            TEARDOWN,
            "closed",
            action="unlink_private",
            cite=f"{_SP5}, DL-210",
        ),
        Row(
            "supervisor_process.34",
            "starting",
            TEARDOWN,
            "closed",
            cite=f"{_SP5}, DL-210",
            note=(
                "An error ended startup before the bind: teardown closes the lock if it was"
                " taken and unlinks nothing; main exits 1, or 7 for a statechart error."
            ),
        ),
        Row(
            "supervisor_process.35",
            "refused",
            TEARDOWN,
            "closed",
            cite=f"{_SP5}, DL-210",
            note=(
                "Another supervisor owns the root: teardown closes the lock if it was taken"
                " and unlinks nothing; main exits 1."
            ),
        ),
        Row(
            "supervisor_process.36",
            "shutting_down",
            TERM,
            action="retry",
            kind="internal",
            cite=f"{_SP5} SHUTDOWN, DL-300",
        ),
        Row(
            "supervisor_process.37",
            "serving",
            "MAINTENANCE_STOP",
            "maintenance_checking",
            guard="credentials",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.38",
            "serving",
            "MAINTENANCE_STOP",
            guard="foreign",
            kind="refusal",
            code="wrong_incarnation",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.39",
            "serving",
            "MAINTENANCE_STOP",
            guard="stale",
            kind="refusal",
            code="stale_token",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.40",
            "maintenance_checking",
            MAINTENANCE_REFUSED,
            "serving",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.41",
            "maintenance_checking",
            MAINTENANCE_STOPPING,
            "maintenance_stopping",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.42",
            "maintenance_checking",
            MAINTENANCE_IDLE,
            "maintenance_completing",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.43",
            "maintenance_stopping",
            MAINTENANCE_IDLE,
            "maintenance_completing",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.44",
            "maintenance_stopping",
            MAINTENANCE_UNCERTAIN,
            "maintenance_uncertain",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.45",
            "maintenance_completing",
            MAINTENANCE_UNCERTAIN,
            "maintenance_uncertain",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.46",
            "maintenance_completing",
            MAINTENANCE_FINISHED,
            "maintenance_completed",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.47",
            "maintenance_completed",
            TEARDOWN,
            "closed",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.48",
            "maintenance_uncertain",
            TERM,
            "shutting_down",
            action="drain",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.49",
            "maintenance_uncertain",
            DEADMAN,
            "stopped",
            cite=f"{_SP5}, DL-390",
        ),
        Row(
            "supervisor_process.50",
            "maintenance_completed",
            TERM,
            kind="internal",
            cite=f"{_SP5}, DL-390",
        ),
        *(
            Row(
                f"supervisor_process.{number}",
                "maintenance_uncertain",
                verb,
                kind="refusal",
                code="maintenance_uncertain",
                cite=f"{_SP5}, DL-390",
            )
            for number, verb in enumerate(_MUTATING, 51)
        ),
        Row(
            "supervisor_process.55",
            "maintenance_stopping",
            MAINTENANCE_PASS,
            action="advance",
            kind="internal",
            cite=f"{_SP5}, DL-394, DL-401",
        ),
        Row(
            "supervisor_process.56",
            "maintenance_completing",
            MAINTENANCE_PASS,
            action="complete",
            kind="internal",
            cite=f"{_SP5}, DL-390, DL-401",
        ),
        *(
            Row(
                f"supervisor_process.{number}",
                "maintaining",
                verb,
                kind="refusal",
                code="maintenance_in_progress",
                cite=f"{_SP5}, DL-401",
            )
            for number, verb in enumerate(_MUTATING, 57)
        ),
    ),
    guards={
        "lock_held": _lock_held,
        "owner_live": _owner_live,
        "free": _free,
        "credentials": _credentials,
        "foreign": _foreign,
        "stale": _stale,
    },
    actions={
        "refuse": _refuse,
        "drain": _drain,
        "retry": _retry,
        "advance": _advance,
        "complete": _complete,
        "unlink_published": _unlink_published,
        "unlink_private": _unlink_private,
    },
    time_events=(TimeEvent(DEADMAN),),
    clock="monotonic",
    exclusions=(
        *(
            Exclusion(state, event, reason)
            for event, (states, reason) in _PROCESS_UNREACHABLE.items()
            for state in states
        ),
        *_HELD_WHILE_MAINTAINING,
    ),
)


class Supervisor:
    """One supervisor per run_root: owns the socket, the wrapper lifelines and the
    single-controller lease. A single-threaded selectors loop (SIGCHLD self-pipe, listen
    socket, client sockets), as in the wrapper."""

    def __init__(self, run_root: str, *, deadman_s: float | None = None) -> None:
        self.run_root = run_root
        #: ss8's T_deadman in seconds. None: this supervisor tolerates an absent
        #: controller forever (DL-79), so its run root is reroutable only by force.
        self.deadman_s = deadman_s
        #: monotonic instant since which there has been no LIVE leaseholder, or None while
        #: one is live. It is armed at bind, so a supervisor nobody leases also dies.
        self._unleased_since: float | None = None
        self.sock_path = os.path.join(run_root, "supervisor.sock")
        self.pid_path = os.path.join(run_root, "supervisor.pid")
        self._lock_fd: int | None = None
        self._maintenance_lock_fd: int | None = None
        #: the admitted maintenance operation, from admission to the exit (DL-401)
        self._maintenance: _Maintenance | None = None
        self._private_path = os.path.join(run_root, f".s.{os.getpid()}")
        self._socket_inode: int | None = None
        self.boot_id = current_boot_id()
        #: DL-80: the identity of THIS process, minted per start. The fencing counter is
        #: in memory (ss5), so a restart mints token 1 again; every mutating verb carries
        #: the incarnation, and a mismatch is `wrong_incarnation`, not `stale_token`,
        #: because the two demand opposite client behaviour (re-acquire and reconcile,
        #: versus someone else holds it).
        self.incarnation = uuid.uuid4().hex
        #: LIVE bookkeeping only (ss11a): lifelines, exit pushes and LIST. The run
        #: directory is the idempotency store, so a completed entry leaves this map.
        self.runs: dict[str, _Run] = {}
        #: completion order, for the bounded LIST window
        self._completed: deque[str] = deque()
        self.lease: _Lease | None = None
        self._next_token = 1  # monotonic fencing counter (in-memory; ss5)
        #: the supervisor's executor (statechart-engine ss11): it runs SUPERVISOR_LEASE,
        #: whose actions write `self.lease`, and SUPERVISOR_PROCESS, whose state is `state`.
        self._machines = Executor()
        self._machines.start(_LEASE, SUPERVISOR_LEASE, self)
        self._machines.start(_PROCESS, SUPERVISOR_PROCESS, self)
        self._machines.run()
        self._conns: dict[int, _Conn] = {}
        self._sel = selectors.DefaultSelector()
        self._listen: socket.socket | None = None
        self._chld_r, self._chld_w = os.pipe()
        os.set_blocking(self._chld_r, False)
        os.set_blocking(self._chld_w, False)
        #: the wakeup fd `_install_signals` replaced (-1: none); None before it installs one
        self._prior_wakeup_fd: int | None = None
        #: SIGTERM or SIGINT arrived. The handler sets only this and the loop takes the
        #: transition: a handler writing `state` could interrupt another write of it.
        self._shutdown_requested = False
        self._backlog_bytes = _test_limit("DSL41_SUPERVISOR_TEST_BACKLOG_BYTES", BACKLOG_BYTES)
        self._request_line_limit = _test_limit(
            "DSL41_SUPERVISOR_TEST_REQUEST_LINE_LIMIT", REQUEST_LINE_LIMIT
        )
        self._accept_log_at = float("-inf")

    @property
    def state(self) -> ProcessState:
        """The active leaf of SUPERVISOR_PROCESS."""
        (phase,) = self._machines.active(_PROCESS) & _PROCESS_PHASES
        return cast(ProcessState, phase)

    def _process(self, name: str, payload: Any = None) -> Outcome:
        """Post one event to SUPERVISOR_PROCESS and give its row's outcome."""
        (outcome,) = self._machines.dispatch(_PROCESS, Event(name, payload))
        return outcome

    @property
    def _running(self) -> bool:
        """Serve normally, run an admitted maintenance operation, or provide diagnostics
        while uncertain (DL-390, DL-401).

        A running or uncertain operation refuses every mutating verb through the chart.
        Legacy shutting_down dispatches nothing, including after a failed wait.
        """
        return self.state in ("serving", "maintenance_uncertain") or self._maintaining

    @property
    def _maintaining(self) -> bool:
        """An admitted operation runs: the loop passes are its steps (DL-401)."""
        return "maintaining" in self._machines.active(_PROCESS)

    def _pause(self) -> float:
        """The loop's select timeout: the pause the running operation asked for, else the
        one-second reap tick."""
        if self._maintaining:
            assert self._maintenance is not None  # admission sets it before maintaining
            return self._maintenance.pause
        return 1.0

    # -- startup ------------------------------------------------------------

    def _set_subreaper(self) -> None:
        """Linux PR_SET_CHILD_SUBREAPER (prctl 36, 1): a killed wrapper's command reparents
        to us, not to init. Best-effort; a no-op elsewhere (ss6a)."""
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
        """True only on positive absence or PID reuse.

        Old supervisors record no start_time, so a live legacy pid refuses. A failed token
        lookup is not absence: kill(pid, 0) must confirm ESRCH."""
        try:
            record = _load_json(self.pid_path)
        except RecursionError:
            return False
        if record is None:
            # a missing record names no owner; an unreadable one never authorizes reclamation
            return not os.path.lexists(self.pid_path)
        pid = record.get("pid")
        if not is_wire_int(pid) or pid <= 0:
            return False
        boot = record.get("boot_id")
        if isinstance(boot, str) and boot not in ("", "unknown", self.boot_id):
            if self.boot_id != "unknown":
                return True
        if proc_is_zombie(pid):
            # exited and unreaped: no descriptors, lock or socket, yet /proc and the
            # start token survive until the parent waits (DL-210)
            return True
        current = proc_start_token(pid)
        recorded = record.get("start_time")
        if current is not None:
            return (
                isinstance(recorded, str)
                and valid_start_token(recorded)
                and not start_tokens_match(current, recorded)
            )
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass
        return False

    def _owner_present(self) -> bool:
        """The published socket answers PING within one of three attempts across a
        second, or the pid record does not prove its owner absent (DL-210)."""
        if os.path.lexists(self.sock_path):
            # three attempts across one second: macOS can refuse a full listen
            # backlog, so one refusal never authorizes unlink
            started = time.monotonic()
            for attempt in range(3):
                deadline = started + (attempt + 1) / 3
                if self._probe_answered(deadline):
                    return True
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        return not self._pid_owner_absent()

    def _start_fact(self) -> tuple[str, LockHeld | None]:
        """`start`'s fact, from the first test that decides it (DL-210): LOCK_HELD when
        supervisor.lock is held, OWNER_LIVE when an owner is present, and otherwise FREE
        once private sockets are swept, the published path is reclaimed and the private
        socket is bound. The tests are I/O with sleeps, so they run here and the guards
        compare the fact. With LOCK_HELD comes the `LockHeld`, the refusal's cause."""
        try:
            self._lock_fd = flock_exclusive(os.path.join(self.run_root, "supervisor.lock"))
        except LockHeld as exc:
            return LOCK_HELD, exc
        # sweep private sockets only, under the lock; other files with this prefix are not ours
        with os.scandir(self.run_root) as entries:
            for entry in entries:
                if entry.name.startswith(".s."):
                    with contextlib.suppress(FileNotFoundError):
                        if stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode):
                            os.unlink(entry.path)
        if self._owner_present():
            return OWNER_LIVE, None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.sock_path)
        old_umask = os.umask(0o177)
        try:
            self._listen = socket.socket(socket.AF_UNIX)
            self._listen.bind(self._private_path)
        finally:
            os.umask(old_umask)
        return FREE, None

    def _bind(self) -> None:
        # FREE is posted right after the private bind, so a listen failure tears down
        # from `bound` and unlinks the private socket (supervisor_process.33)
        fact, cause = self._start_fact()
        if REFUSE in self._process(START, fact).intents:
            raise SystemExit("another supervisor owns this root") from cause  # .01 or .02
        assert self._listen is not None
        self._listen.listen(64)
        os.chmod(self._private_path, 0o600)
        self._socket_inode = os.stat(self._private_path).st_ino
        self._note_start()
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
        self._process(PUBLISH)
        self._listen.setblocking(False)

    def _note_start(self) -> None:
        """Note this incarnation in supervisor.lock, under the lock and before the pid record.
        No teardown removes it, so a maintenance record names the root's last supervisor only
        while the note names it too (DL-401). The file is never cut first. Its first byte is
        spoiled, the new note is written after it, then its first byte, then the file is cut
        to its length: an interruption leaves a note no reader accepts, or the new one. The
        flock is on the inode, so no rename."""
        assert self._lock_fd is not None
        note = json.dumps(
            {"version": START_NOTE_VERSION, "incarnation": self.incarnation, "pid": os.getpid()},
            sort_keys=True,
        ).encode("utf-8")
        _pwrite_all(self._lock_fd, b"!", 0)
        _pwrite_all(self._lock_fd, note[1:], 1)
        _pwrite_all(self._lock_fd, note[:1], 0)
        os.ftruncate(self._lock_fd, len(note))
        os.fsync(self._lock_fd)

    def _install_signals(self) -> None:
        signal.signal(signal.SIGCHLD, self._on_chld_signal)
        # Host TERM/INT uses the legacy bounded drain (DL-390). Wrappers retain their
        # lifelines through that wait; SIGKILL leaves them to lifeline EOF.
        signal.signal(signal.SIGTERM, self._on_term_signal)
        signal.signal(signal.SIGINT, self._on_term_signal)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        # Python handlers run between bytecodes, so a signal landing just before select()
        # would wait for the one-second tick. The wakeup fd writes the signal number into
        # the same pipe at delivery. No asyncio loop owns it; `_teardown` restores the
        # prior one.
        self._prior_wakeup_fd = signal.set_wakeup_fd(self._chld_w, warn_on_full_buffer=False)

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
            # handlers first: _bind publishes supervisor.pid and supervisor.sock, and a
            # peer that sees either must not meet a default action
            self._install_signals()
            self._bind()
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
                for key, mask in self._sel.select(timeout=self._pause()):
                    # A dispatch in this batch can end the loop or close a descriptor it
                    # names (DL-390).
                    if not self._running:
                        break
                    if self._sel.get_map().get(key.fd) != key:
                        continue
                    tag, payload = key.data
                    if tag == "listen":
                        self._accept()
                    elif tag == "chld":
                        self._drain_chld()
                        self._reap()
                        if self._shutdown_requested and not self._maintaining:
                            self._orderly_shutdown(TERM)
                    elif tag == "conn":  # pragma: no branch -- no other tag is registered
                        if mask & selectors.EVENT_WRITE:
                            self._writable(payload)
                        if mask & selectors.EVENT_READ and self._connected(payload):
                            self._readable(payload)
                # the self-pipe can coalesce a SIGCHLD under load; the select timeout is the
                # unconditional reap tick
                self._reap()
                if self._maintaining:
                    # an admitted operation runs to its outcome; a latched signal waits
                    # for it (DL-401)
                    self._maintenance_pass()
                elif self._shutdown_requested:
                    self._orderly_shutdown(TERM)
                elif self.state == "shutting_down":
                    # an error ended a SHUTDOWN's wait: run it once more rather than leave
                    # wrappers to lifeline EOF; a second error ends the loop, exit 1
                    self._orderly_shutdown(PASS_ENDED)
                elif self._running and self._deadman_expired():
                    self._process(DEADMAN)
            exit_status = 8 if self.state == "maintenance_completed" else 0
        finally:
            self._teardown()
        return exit_status

    # -- deadman (ss8) ------------------------------------------------------

    def _live_leaseholder(self) -> bool:
        """ss5's LIVE lease: unexpired AND its holder's connection still open.

        Either half alone misleads. An unexpired lease on a dead connection is a GONE
        controller (the kernel closes this AF_UNIX fd only when the holder is gone). An
        expired lease on an open connection is a controller that stopped watching."""
        return self._lease_phase() == "live"

    def _deadman_expired(self) -> bool:
        """Has this supervisor been unwatched for T_deadman (ss8)? The sampler of the
        `deadman` time event, called once per serving pass, logging the reason first.

        The clock restarts whenever a live leaseholder appears: the deadman bounds how
        long an UNREACHABLE host keeps running work."""
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
        # loud, in the one log this process has, so the reason jobs died is findable
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
            conn_sock.close()  # same-uid gate (supervisor-protocol ss5)
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
        except StatechartError:
            raise  # an assertion failure stops the supervisor (statechart-engine ss5)
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
        while self._running and not conn.paused and self._connected(conn):
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

    def _connected(self, conn: _Conn) -> bool:
        return self._conns.get(conn.sock.fileno()) is conn

    def _drop_conn(self, conn: _Conn) -> None:
        if not self._connected(conn):
            return
        with contextlib.suppress(KeyError, OSError, ValueError):
            self._sel.unregister(conn.sock)
        self._conns.pop(conn.sock.fileno(), None)
        if self.state != "closed":
            # teardown drops connections after the loop ended; nothing reads the lease then
            self._expire_due(time.monotonic())
            self._machines.dispatch(_LEASE, Event(DISCONNECT, conn))
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
            # pipelined requests already buffered go first when the output pause clears
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
            # strict UTF-8 first: json.loads on bytes would accept UTF-16 and UTF-32
            # (DL-229); a UnicodeDecodeError is a ValueError, so it answers malformed_json
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, RecursionError):
            return {"ok": False, "error": "malformed_json"}
        cmd = req.get("cmd")
        if cmd == "MAINTENANCE_STOP":
            # the verb's own version (DL-401); its retired v 1 is refused by name
            if _is_wire_int_equal(req.get("v"), PROTOCOL_VERSION):
                return {
                    "ok": False,
                    "error": "unsupported_version",
                    "detail": _MAINTENANCE_STOP_V1_RETIRED,
                }
            if not _is_wire_int_equal(req.get("v"), MAINTENANCE_STOP_VERSION):
                return {"ok": False, "error": "unsupported_version"}
        elif not _is_wire_int_equal(req.get("v"), PROTOCOL_VERSION):
            return {"ok": False, "error": "unsupported_version"}
        handler = {
            "PING": self._h_ping,
            "LIST": self._h_list,
            "ACQUIRE": self._h_acquire,
            "RENEW": self._h_renew,
            "RELEASE": self._h_release,
            "SPAWN": self._h_spawn,
            "SIGNAL": self._h_signal,
            "SHUTDOWN": self._h_shutdown,
            "MAINTENANCE_STOP": self._h_maintenance_stop,
        }.get(cmd if isinstance(cmd, str) else "")
        if handler is None:
            return {"ok": False, "error": "unknown_verb"}
        try:
            answer = handler(conn, req)
        except StatechartError:
            # a declared machine is wrong, not one request: the supervisor stops, as every
            # tier does (statechart-engine ss5)
            raise
        except Exception as exc:  # noqa: BLE001 -- the belt under every verb
            # a raising handler would end this process and EOF every wrapper's lifeline, so
            # one request's failure is answered and the tier keeps running
            answer = {"ok": False, "error": f"internal: {type(exc).__name__}: {exc}"}
        return answer

    def _h_ping(self, _conn: _Conn, _req: dict[str, Any]) -> dict[str, Any]:
        # `deadman_s` rides the two READ verbs (S5b, DL-95): the leader records what this host
        # ACTUALLY runs, and a reattaching engine did not start this supervisor.
        return {
            "ok": True,
            "version": PROTOCOL_VERSION,
            "incarnation": self.incarnation,
            "deadman_s": self.deadman_s,
            "process_state": self.state,
            **self._operation(),
        }

    def _operation(self) -> dict[str, Any]:
        """The admitted maintenance operation as PING and LIST show it (DL-401): an
        additive `maintenance` field while it runs or stays uncertain."""
        if self._maintenance is None:
            return {}
        record = self._maintenance.record
        return {"maintenance": {key: record[key] for key in ("force", "bound_s", "started_at")}}

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
            "process_state": self.state,
            **self._operation(),
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

    def _expire_due(self, now: float) -> None:
        """Produce the lease's time event: post `expire` once per deadline when the record's
        deadline is not after `now` and the lease is still held. The deadline instant
        itself is due, and so is a NaN deadline."""
        if (
            self.lease is not None
            and not now < self.lease.deadline
            and "held" in self._machines.active(_LEASE)
        ):
            self._machines.dispatch(_LEASE, Event(EXPIRE))

    def _lease_phase(self) -> str:
        """The lease's phase now (free, live, orphaned or expired), taking a due expiry first."""
        self._expire_due(time.monotonic())
        (phase,) = self._machines.active(_LEASE) & _PHASES
        return phase

    def _lease_active(self) -> bool:
        """Unexpired (live or orphaned, `held` in SUPERVISOR_LEASE): LIST reports such a
        lease and an exit push goes only to its holder."""
        return self._lease_phase() in ("live", "orphaned")

    def _refusal(self, code: str) -> dict[str, Any]:
        """The answer of a lease refusal (ss5): its code, plus the incarnation for
        wrong_incarnation and the holder and expiry for lease_held."""
        answer: dict[str, Any] = {"ok": False, "error": code}
        if code == "wrong_incarnation":
            answer["incarnation"] = self.incarnation
        elif code == "lease_held":
            assert self.lease is not None  # only a live lease is held
            answer.update(holder=self.lease.holder, expires_at=self.lease.expires_at)
        return answer

    def _fence(self, verb: str, req: dict[str, Any]) -> dict[str, Any] | None:
        """Post a mutating verb to SUPERVISOR_PROCESS at one sampled
        instant after a due lease expiry, and give the refusal's answer, or None when the
        verb's work runs. The guards are the lease chart's, so RENEW, RELEASE and these
        verbs meet one fence: this incarnation, then the token of an unexpired lease.
        A token from a dead supervisor means the wrappers lost their lifeline and record
        on their own (DL-205): the controller must re-acquire AND reconcile from the
        spool, the opposite of what stale_token asks."""
        self._expire_due(time.monotonic())
        outcome = self._process(verb, _Request(req))
        if outcome.refusals:
            (code,) = outcome.refusals
            return self._refusal(code)
        return None

    def _h_acquire(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        return self._lease_verb(conn, "ACQUIRE", req)

    def _h_renew(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        return self._lease_verb(conn, "RENEW", req)

    def _h_release(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        return self._lease_verb(conn, "RELEASE", req)

    def _lease_verb(self, conn: _Conn, verb: str, req: dict[str, Any]) -> dict[str, Any]:
        """Post a lease verb to SUPERVISOR_LEASE at one sampled instant and give the answer
        of the row taken. An `internal` refusal raises the error computing the expiry, so
        the dispatch belt answers it with nothing changed (DL-300)."""
        now = time.monotonic()
        self._expire_due(now)
        request = _LeaseRequest(req, conn, now, time.time())
        outcome = self._machines.dispatch(_LEASE, Event(verb, request))[0]
        if outcome.intents:
            answer: dict[str, Any] = outcome.intents[0]
            return answer
        (code,) = outcome.refusals
        if code == "internal":
            raise request.error()
        return self._refusal(code)

    # -- spawn / signal -----------------------------------------------------

    def _h_spawn(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._fence("SPAWN", req)) is not None:
            return err
        spec = req.get("spec")
        if not isinstance(spec, dict):
            return {"ok": False, "error": "bad_spec", "detail": "spec is not an object"}
        return self.spawn_run(spec)

    # -- ss11a: directory-backed SPAWN idempotency ---------------------------

    def _crash_point(self, _stage: str) -> None:
        """The PR-36 crash matrix's seam. Each `spawn_run` write is followed by one, so a
        test can stop between two durable acts and ask a FRESH supervisor what the
        directory says. A no-op in production."""

    def index_path(self, run_id: str) -> str:
        return os.path.join(self.run_root, "runs", _INDEX_DIR, run_id)

    def run_dir_for(self, job: str, run_number: int) -> str:
        """ss11a's one-to-one ownership as a path: one run_id maps to one (job, run_number)
        and that to one directory. The index entry carries the pair, not the path."""
        return os.path.join(self.run_root, "runs", f"{job}.{run_number}")

    def spawn_run(self, spec: dict[str, Any]) -> dict[str, Any]:
        """SPAWN minus the lease gate: validate, resolve a replay through the index, or apply
        for the first time (ss11a). Public so the crash matrix can drive it directly."""
        run_id = spec.get("run_id")
        if not isinstance(run_id, str):
            return {"ok": False, "error": "bad_spec", "detail": "spec.run_id is missing"}
        if _RUN_ID_RE.fullmatch(run_id) is None:
            # ss11a: the id names a directory entry, so the grammar is a wire check,
            # refused BEFORE anything is created
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
        # NUL is checked before the realpath calls because os.path raises ValueError, not
        # OSError, on it; the answer is `bad_spec`, not `internal:` (DL-151).
        # `_spec_schema_error` scans every other string field.
        incoming = spec.get("run_dir")
        if not isinstance(incoming, str) or "\x00" in incoming:
            return {"ok": False, "error": "bad_spec", "detail": "spec.run_dir is not a path"}
        run_dir = self.run_dir_for(job, run_number)
        if os.path.realpath(incoming) != os.path.realpath(run_dir):
            # the supervisor OWNS this directory, and index -> (job, run_number) -> directory
            # is how a replay finds the tombstone. REALPATH, not normpath: the engine and
            # this process are told the run root separately, and two spellings of one
            # directory (`./r` and `/abs/r`, `/tmp/r` and `/private/tmp/r`) must not
            # refuse every spawn
            return {"ok": False, "error": "bad_spec", "detail": f"run_dir must be {run_dir}"}
        bad = _spec_schema_error(spec)
        if bad is not None:
            # the WHOLE frozen ss2 schema, before anything durable or any replay. A
            # wrong-typed field that explodes after the fork (grace_seconds: "x" reaching
            # float()) would kill this process and EOF every wrapper. The fingerprint's
            # "float:"+hex tag is collision-free only while every key's type is pinned:
            # with unknown keys refused and grace_seconds the one number, no VALID spec
            # holds a string where another holds the float that encodes to it.
            return {"ok": False, "error": "bad_spec", "detail": bad}
        try:
            fingerprint = spec_fingerprint(spec)
        except CanonError as exc:
            # a value ss3.2 cannot hold (a lone surrogate): the receipt could not be written
            # either, and every wrapper is tethered to this process, so answer
            return {"ok": False, "error": "bad_spec", "detail": f"unfingerprintable spec: {exc}"}
        replay = self._resolve_replay(run_id, job, run_number, run_dir, fingerprint)
        if replay is not None:
            return replay
        return self._first_application(spec, run_id, job, run_number, run_dir, fingerprint)

    def _resolve_replay(
        self, run_id: str, job: str, run_number: int, run_dir: str, fingerprint: str
    ) -> dict[str, Any] | None:
        """The ss11a answer table, or None for "first application".

        Resolution goes through the INDEX first. With no index entry, the incoming path's
        receipt is read (DL-151): one for another run_id is a collision; one for this
        run_id means the index was lost under a live receipt, so the directory answers."""
        index = _load_tombstone(self.index_path(run_id), "index")
        if index is _INVALID:
            # corruption is not absence: "no index entry" AUTHORIZES a spawn (ss11a)
            return self._indeterminate(f"the index entry for {run_id} is unreadable")
        if index is not None and index.get("run_id") != run_id:
            # the entry disagrees with its own name: tampered or misfiled
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
                # impossible by write order (mkdir precedes the index); nothing may re-spawn
                return self._indeterminate(f"the index names {indexed}, which does not exist")
            return self._answer_from_directory(run_id, job, run_number, indexed, fingerprint)
        receipt = _load_tombstone(os.path.join(run_dir, "receipt.json"), "receipt")
        if receipt is _INVALID:
            return self._indeterminate(f"{run_dir}/receipt.json is unreadable")
        if receipt is not None:
            # no index for this run_id but the path is a tombstone: another run_id is a
            # collision and our own means the index was lost; never a first application
            # (deleting an index authorizes a spawn, ss11a)
            if receipt.get("run_id") != run_id:
                return self._collision(
                    f"{run_dir} already holds a receipt for run_id {receipt.get('run_id')!r}"
                )
            return self._answer_from_directory(run_id, job, run_number, run_dir, fingerprint)
        if os.path.isdir(run_dir):
            # an orphan directory (a crash between mkdir and the index) is REUSED unless
            # another run_id's index names it; that O(n) scan runs only here, after a crash
            owner = self._indexed_owner(job, run_number)
            if owner is _UNREADABLE:
                return self._indeterminate(f"the {_INDEX_DIR} directory cannot be listed")
            if owner is not None and owner != run_id:
                return self._collision(f"{run_dir} is already indexed under run_id {owner!r}")
            for name in ("spawn.json", "status.json"):
                if os.path.exists(os.path.join(run_dir, name)):
                    # a directory the engine created before receipts existed holds a
                    # run's evidence, and forking into it would overwrite it
                    return self._indeterminate(
                        f"{run_dir} holds a wrapper's {name} and no receipt:"
                        " a run predating this protocol, never reused"
                    )
        return None

    def _answer_from_directory(
        self, run_id: str, job: str, run_number: int, directory: str, fingerprint: str
    ) -> dict[str, Any]:
        """ss11a: a replay answers from the DIRECTORY: the original reply if it survived,
        the wrapper's own record if not, and a refusal where no answer can be rebuilt."""
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
        # an _INVALID reply or spawn record falls to the next table row, which is always
        # SAFER (reconstruct, then in-progress/indeterminate), so it never invents an answer
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
            # the rule _signal_command applies to this file: a record that does not name this
            # run is spoofed, stale or foreign, and would hand the engine another run's pid
            spawned = None
        # equivalent, not byte-identical: the wrapper's own record carries the same two facts
        answer = _duplicate_from(run_id, spawned, "wrapper_pid", "started_at")
        if answer is not None:
            return answer
        run = self.runs.get(run_id)
        if run is not None and run.wrapper_rc is None:
            # the receipt is durable, the fork happened and this incarnation holds the
            # lifeline: no second spawn, no answer yet. Memory is authoritative only for
            # liveness; a previous incarnation's wrapper cannot be alive (its lifeline EOF'd)
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
        """The run_id whose index entry claims (job, run_number), if any, or `_UNREADABLE`
        when the directory cannot be listed: "no owner" AUTHORIZES reuse, so an EACCES
        must never spell it."""
        index_dir = os.path.join(self.run_root, "runs", _INDEX_DIR)
        try:
            names = os.listdir(index_dir)
        except FileNotFoundError:
            return None  # no index directory: nothing was ever received here
        except OSError:
            return _UNREADABLE
        for name in sorted(names):
            if _RUN_ID_RE.fullmatch(name) is None:
                # `durable_write` leaves `.<name>.<pid>.tmp` if it dies between fsync and
                # rename; that record was never durable and reading it would refuse a spawn
                # that ss11a calls a first application
                continue
            entry = _load_tombstone(os.path.join(index_dir, name), "index")
            if entry is _INVALID:
                # an unreadable entry MIGHT claim this run; reuse would be the double spawn
                # ss11a prevents
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
        """ss11a: never reused, never given a second index; there is no correct pick
        between two identities for one directory."""
        return {"ok": False, "error": "collision", "detail": detail}

    @staticmethod
    def _indeterminate(detail: str) -> dict[str, Any]:
        """ss11a: the crash landed where nothing may re-spawn. The engine's E7 policy
        decides the run; this tier states the fact."""
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
        """ss11a's write order: mkdir, index, receipt, spawn, reply, answer. The index comes
        first because every later lookup goes through it, so the first durable thing that
        names the run is the index. The receipt precedes the fork because the opposite
        failure is a never-happened run reported unknown (E7 handles it), where the
        other is a retry that spawns twice."""
        # every write below is answered, never raised: an error here would end this process
        # and EOF every wrapper's lifeline
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
            # nothing has forked, so this spawn did not happen; a half-written tombstone
            # reads `indeterminate` on a retry, the truthful answer
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
            # the wrapper is ALREADY running: the answer stands and a replay rebuilds it from
            # the wrapper's own spawn.json (ss11a's second row)
            print(
                f"supervisor: reply.json for {run_id} not written ({exc});"
                " a replay will answer from spawn.json",
                file=sys.stderr,
                flush=True,
            )
        self._crash_point("after_reply")
        return {"ok": True, "run_id": run_id, "wrapper_pid": wrapper_pid, "spawned_at": spawned_at}

    def _spawn_wrapper(self, spec: dict[str, Any]) -> tuple[int, int]:
        """Fork the wrapper by file path with the lifeline WRITE END kept here only (the ss6a
        fd-hygiene invariant). posix_spawn, not subprocess.Popen, so the global
        waitpid(-1) reaper never fights Popen's bookkeeping."""
        lifeline_r, lifeline_w = os.pipe()
        stdin_r = stdin_w = -1
        try:
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
                lifeline_r = -1
                os.close(stdin_r)
                stdin_r = -1
            try:
                write_all(stdin_w, json.dumps(wrapper_spec).encode("utf-8"))
            finally:
                os.close(stdin_w)  # EOF: the wrapper repoints stdin at /dev/null after
                stdin_w = -1
        except BaseException:
            # no caller owns the lifeline yet, so every way out closes all four ends; a
            # wrapper forked before the failure sees EOF and ends itself
            for fd in (lifeline_r, lifeline_w, stdin_r, stdin_w):
                if fd >= 0:
                    os.close(fd)
            raise
        return pid, lifeline_w

    def _h_signal(self, _conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._fence("SIGNAL", req)) is not None:
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
            # DL-83: SPAWN returns once the wrapper is FORKED; spawn.json lands a few
            # syscalls later. A live wrapper with no record yet means retry, not
            # noop, so an early KILLJOB is never dropped.
            return {"ok": False, "error": "not_ready"}
        return {"ok": True, "noop": True}  # already-dead / unverifiable group

    def _signal_command(self, run: _Run, sig: int) -> str:
        """Signal the recorded command PGID, never the wrapper, after the (pid, start-time)
        PID-reuse guard.

        Returns "sent", "noop" (nothing to signal) or "not_ready" (DL-83: the wrapper is
        alive but has not written spawn.json, so the caller must retry, not treat the
        kill as done)."""
        spawn = _load_json(os.path.join(run.run_dir, "spawn.json"))
        if spawn is None:
            # wrapper_rc is None while the wrapper lives (set by _reap): a live wrapper with
            # no record is mid-spawn; a dead one never recorded, so nothing is addressable
            return "not_ready" if run.wrapper_rc is None else "noop"
        if (spawn.get("job"), spawn.get("run_number"), spawn.get("run_id")) != (
            run.job,
            run.run_number,
            run.run_id,
        ):
            # spoofed or corrupt spawn record: never signal. run_id is part of the check
            # (DL-118/DL-129); the tuple alone plus a live foreign pid token could aim the
            # signal at a stranger's process group
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

    def _release_maintenance_lock(self) -> None:
        if self._maintenance_lock_fd is not None:
            os.close(self._maintenance_lock_fd)
            self._maintenance_lock_fd = None

    def _maintenance_idle(self, proof: ServiceContainment) -> bool:
        assert self._maintenance_lock_fd is not None
        verify_lock_path(self._maintenance_lock_fd, os.path.join(self.run_root, "leader.lock"))
        self._reap()
        members = proof.members()
        verify_lock_path(self._maintenance_lock_fd, os.path.join(self.run_root, "leader.lock"))
        return not any(run.wrapper_rc is None for run in self.runs.values()) and members == {
            os.getpid()
        }

    def _h_maintenance_stop(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        """Admission, answered in the request (DL-390, DL-401). A refusal changes nothing.
        An admitted operation is recorded first and then runs in the loop, one pass at a
        time, to completed or uncertain."""
        if (error := self._fence("MAINTENANCE_STOP", req)) is not None:
            return error
        try:
            mode = req.get("force")
            if mode is not None and mode != "terminate":
                raise ValueError("force must be null or terminate")
            self._maintenance_lock_fd = flock_existing(os.path.join(self.run_root, "leader.lock"))
            proof = ServiceContainment.observe()
            idle = self._maintenance_idle(proof)
            if not idle and mode is None:
                raise ValueError("active wrappers or residual service processes")
            operation = self._admit(mode, proof, idle)
        except StatechartError:
            raise
        except Exception as exc:
            self._release_maintenance_lock()
            self._process(MAINTENANCE_REFUSED)
            return {"ok": False, "error": "maintenance_refused", "detail": str(exc)}
        self._maintenance = operation
        self._process(MAINTENANCE_IDLE if idle else MAINTENANCE_STOPPING)
        record = operation.record
        print(
            f"supervisor: maintenance admitted force={record['force']} bound_s={record['bound_s']}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "ok": True,
            "maintenance": "admitted",
            **{key: value for key, value in record.items() if key not in ("version", "state")},
        }

    def _admit(self, mode: str | None, proof: ServiceContainment, idle: bool) -> _Maintenance:
        """The operation's estimated bound and its admitted record, written before the answer
        (DL-401). Idle work needs only teardown. Forced work adds the spawn-record wait, the
        longest live grace, the KILL settle, two verified sends per live group and the
        sample allowance. A failed write removes a record of this incarnation, so a refusal
        leaves the previous record or none."""
        start_time = proc_start_token(os.getpid())
        if start_time is None:
            raise OSError("this supervisor's own start token cannot be read")
        bound_s = _MAINTENANCE_TEARDOWN_S
        if not idle:
            live = [run for run in self.runs.values() if run.wrapper_rc is None]
            grace_s = max((run.grace_seconds for run in live), default=0.0)
            bound_s += (
                _MAINTENANCE_SPAWN_WAIT_S
                + grace_s
                + _MAINTENANCE_SETTLE_S
                + 2 * _MAINTENANCE_SEND_S * len(live)
                + _MAINTENANCE_SAMPLES_S
            )
        record = {
            "version": MAINTENANCE_RECORD_VERSION,
            "state": "admitted",
            "incarnation": self.incarnation,
            "supervisor_pid": os.getpid(),
            "supervisor_start_time": start_time,
            "boot_id": self.boot_id,
            "force": mode,
            "bound_s": bound_s,
            "started_at": utc_now_iso(),
        }
        path = os.path.join(self.run_root, MAINTENANCE_RECORD)
        try:
            durable_write_json(path, record)
        except OSError:
            self._unlink_own_record(path)
            raise
        ladder = None if idle else self._maintenance_ladder(proof)
        return _Maintenance(record, proof, ladder)

    def _unlink_own_record(self, path: str) -> None:
        """A write that failed after its rename leaves this incarnation's `admitted` record
        for a refused request: remove it, best effort and logged (DL-401). A record of
        another incarnation is the previous operation's and stays."""
        try:
            left = read_maintenance_record(self.run_root)
            if left is not None and left["incarnation"] == self.incarnation:
                os.unlink(path)
                print(
                    f"supervisor: removed {MAINTENANCE_RECORD} of a refused admission",
                    file=sys.stderr,
                    flush=True,
                )
        except (OSError, ValueError) as exc:
            print(
                f"supervisor: {MAINTENANCE_RECORD} of a refused admission stays: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _record_outcome(self, state: str, **extra: str) -> None:
        """Replace the admitted record by its outcome, with the instant it was reached."""
        assert self._maintenance is not None
        record = {
            **self._maintenance.record,
            "state": state,
            "finished_at": utc_now_iso(),
            **extra,
        }
        durable_write_json(os.path.join(self.run_root, MAINTENANCE_RECORD), record)

    def _maintenance_pass(self) -> None:
        """One loop pass of the admitted operation (DL-401). The row taken names the step:
        the ladder runs to its next pause, or the owned teardown completes the operation.
        Any failure after admission is uncertain: the lifelines and the lock stay."""
        operation = self._maintenance
        assert operation is not None
        intents = self._process(MAINTENANCE_PASS).intents
        try:
            if COMPLETE in intents:
                self._maintenance_prepare_teardown(operation.proof)
                self._record_outcome("completed")
                self._process(MAINTENANCE_FINISHED)
                print("supervisor: maintenance completed", file=sys.stderr, flush=True)
                return
            assert operation.ladder is not None  # only forced work enters stopping
            try:
                operation.pause = next(operation.ladder)
                return
            except StopIteration:
                pass
            if not self._maintenance_idle(operation.proof):
                raise OSError("work remains after the termination bound")
            operation.pause = 0.0
            self._process(MAINTENANCE_IDLE)
        except StatechartError:
            raise
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            print(f"supervisor: maintenance uncertain: {detail}", file=sys.stderr, flush=True)
            try:
                self._record_outcome("uncertain", detail=detail)
            except OSError as lost:
                print(
                    f"supervisor: {MAINTENANCE_RECORD} not written: {lost}",
                    file=sys.stderr,
                    flush=True,
                )
            self._process(MAINTENANCE_UNCERTAIN)

    def _maintenance_groups(
        self, groups: dict[str, _Group], proof: ServiceContainment
    ) -> dict[str, _Group]:
        """Each verified command group's contained members, from one containment sample.

        A group keeps its identity only while a member recorded in its previous proof is
        alive across the sample and still in the group: the kernel never reuses a live
        group's id, so a bare pgid alone never names the group (DL-394). Every surviving
        recorded member must still be contained and in the group, or the sample refuses.
        A lost identity drops the group unsignalled; the final idle proof decides
        (DL-390)."""
        before = {
            run_id: {pid: token for pid, token in members.items() if verify_alive(pid, token)}
            for run_id, (_pgid, members) in groups.items()
        }
        contained = proof.members()
        by_pgid = {pgid: run_id for run_id, (pgid, _members) in groups.items()}
        found: dict[str, dict[int, str]] = {run_id: {} for run_id in groups}
        for pid in sorted(contained - {os.getpid()}):
            try:
                run_id = by_pgid.get(os.getpgid(pid))
            except ProcessLookupError:
                continue
            if run_id is None:
                continue
            token = before[run_id].get(pid) or proc_start_token(pid)
            if token is not None:
                found[run_id][pid] = token
        proven: dict[str, _Group] = {}
        for run_id, (pgid, _members) in groups.items():
            survivors = [pid for pid, token in before[run_id].items() if verify_alive(pid, token)]
            if any(pid not in found[run_id] for pid in survivors):
                raise OSError("command group is outside its verified containment")
            if survivors:
                proven[run_id] = (pgid, found[run_id])
        return proven

    def _maintenance_signal(
        self,
        run: _Run,
        sig: int,
        proof: ServiceContainment,
        group: _Group | None = None,
    ) -> _Group | None:
        """Force addresses only a known command's verified own process group (DL-390).

        The recorded leader proves the group first; later, the members of the group's
        last proof carry that identity after the leader exits (DL-394). Returns the
        group as signalled, or None when nothing verified remains to address."""
        assert self._maintenance_lock_fd is not None
        verify_lock_path(self._maintenance_lock_fd, os.path.join(self.run_root, "leader.lock"))
        spawn = _load_json(os.path.join(run.run_dir, "spawn.json"))
        if spawn is None:
            if run.wrapper_rc is None:
                raise OSError("a live wrapper has no command identity")
            return None
        if (spawn.get("job"), spawn.get("run_number"), spawn.get("run_id")) != (
            run.job,
            run.run_number,
            run.run_id,
        ) or spawn.get("boot_id") != self.boot_id:
            raise OSError("unverified command record")
        pid, pgid, token = (
            spawn.get(key) for key in ("command_pid", "command_pgid", "command_start_time")
        )
        if not (is_wire_int(pid) and pid > 0 and pgid == pid and isinstance(token, str)):
            raise OSError("unverified command group")
        if group is None or group[0] != pgid:
            group = (pgid, {pid: token})
        # Containment, never an absence, decides completion (DL-390).
        group = self._maintenance_groups({run.run_id: group}, proof).get(run.run_id)
        if group is None:
            return None
        verify_lock_path(self._maintenance_lock_fd, os.path.join(self.run_root, "leader.lock"))
        killpg_quiet(pgid, sig)
        return group

    def _maintenance_ladder(self, proof: ServiceContainment) -> Iterator[float]:
        """Deliberate TERM/grace/KILL never turns a bound into completion (DL-390).

        The bound waits for the wrappers and for every verified group to leave
        containment; a member outliving its grace gets KILL (DL-394). Each yield is a
        pause in seconds, which the loop spends serving; a step runs at most one verified
        send or one containment sample (DL-401). `bound_s` estimates this ladder and
        never cuts it short."""
        live = [run for run in self.runs.values() if run.wrapper_rc is None]
        spawn_deadline = time.monotonic() + _MAINTENANCE_SPAWN_WAIT_S
        while time.monotonic() < spawn_deadline:
            self._reap()
            if all(
                run.wrapper_rc is not None
                or _load_json(os.path.join(run.run_dir, "spawn.json")) is not None
                for run in live
            ):
                break
            yield 0.05
        live = [run for run in live if run.wrapper_rc is None]
        groups: dict[str, _Group] = {}
        for run in live:
            if (group := self._maintenance_signal(run, signal.SIGTERM, proof)) is not None:
                groups[run.run_id] = group
            yield 0.0
        # Identity and containment probes precede each send; grace follows them (DL-390).
        term_at = sampled_at = time.monotonic()
        killed: set[str] = set()
        deadline = (
            term_at + max((run.grace_seconds for run in live), default=0.0) + _MAINTENANCE_SETTLE_S
        )
        while True:
            self._reap()
            if groups and time.monotonic() - sampled_at >= _MAINTENANCE_SAMPLE_S:
                groups = self._maintenance_groups(groups, proof)
                sampled_at = time.monotonic()
                yield 0.0
                # A slow sample never spends the reaping interval on stale state (DL-390).
                self._reap()
            remaining = [run for run in live if run.wrapper_rc is None or run.run_id in groups]
            if not remaining:
                return
            now = time.monotonic()
            sent = False
            for run in remaining:
                if run.run_id not in killed and now - term_at >= run.grace_seconds:
                    group = self._maintenance_signal(
                        run, signal.SIGKILL, proof, groups.get(run.run_id)
                    )
                    if group is None:
                        groups.pop(run.run_id, None)
                    else:
                        groups[run.run_id] = group
                    sampled_at = time.monotonic()
                    killed.add(run.run_id)
                    sent = True
                    # Reaping gets its full interval after slow verified KILLs (DL-390).
                    deadline = max(deadline, time.monotonic() + _MAINTENANCE_SETTLE_S)
                    yield 0.0
            if sent:
                continue  # the bound is judged only on state reaped after the KILL samples
            if now > deadline:
                raise OSError("wrappers remain, or command groups stay populated, after the bound")
            yield 0.02

    def _maintenance_prepare_teardown(self, proof: ServiceContainment) -> None:
        """Prove idle and remove owned publications before the completion answer (DL-390)."""
        if not self._maintenance_idle(proof):
            raise OSError("idle proof was lost before teardown")
        current = os.stat(self.sock_path, follow_symlinks=False)
        if not stat.S_ISSOCK(current.st_mode) or current.st_ino != self._socket_inode:
            raise OSError("supervisor socket ownership was lost")
        if not stat.S_ISREG(os.stat(self.pid_path, follow_symlinks=False).st_mode):
            raise OSError("supervisor pid record is not regular")
        record = _load_json(self.pid_path)
        if record is None or record.get("incarnation") != self.incarnation:
            raise OSError("supervisor pid record ownership was lost")
        os.unlink(self.pid_path)
        os.unlink(self.sock_path)
        fsync_dir(self.run_root)
        if self._listen is not None:
            self._sel.unregister(self._listen)
            self._listen.close()
            self._listen = None
        if self._prior_wakeup_fd is not None:
            signal.set_wakeup_fd(self._prior_wakeup_fd)
            self._prior_wakeup_fd = None
        if self._chld_r in self._sel.get_map():
            self._sel.unregister(self._chld_r)
        for name in ("_chld_r", "_chld_w"):
            fd = getattr(self, name)
            os.close(fd)
            setattr(self, name, -1)
        assert self._maintenance_lock_fd is not None
        verify_lock_path(self._maintenance_lock_fd, os.path.join(self.run_root, "leader.lock"))

    def _h_shutdown(self, conn: _Conn, req: dict[str, Any]) -> dict[str, Any]:
        if (err := self._fence("SHUTDOWN", req)) is not None:
            return err
        # supervisor_process.05 moved to shutting_down: a signal latched before now is this
        # shutdown's; one latched during the wait finds it stopped (.09), or retries a wait
        # that errs (.36)
        self._shutdown_requested = False
        # ss5 order: wait for wrappers FIRST, reply {ok}, exit, unlink (DL-48)
        try:
            self._shutdown_wait()
        except StatechartError:
            raise  # the supervisor stops on it; the wait does not run again
        except Exception as exc:
            # the dispatch answers `internal:` and the wait runs again at the end of this loop
            # pass (row 12, or 36 with a signal latched); the log keeps the cause
            print(
                f"supervisor: the shutdown wait failed ({type(exc).__name__}: {exc});"
                " running it once more",
                file=sys.stderr,
                flush=True,
            )
            raise
        return {"ok": True}

    def _orderly_shutdown(self, trigger: str) -> None:
        """The loop's shutdown. `trigger` is TERM, a latched signal the loop consumes, or
        PASS_ENDED, the end of a pass in which an error ended a SHUTDOWN's wait. The row
        taken reports what runs, in order: the wait from serving (supervisor_process.06);
        the queued replies and then the wait once more from shutting_down (.12, .36);
        nothing from stopped (.09)."""
        self._shutdown_requested = False
        intents = self._process(trigger).intents
        if FLUSH in intents:
            for conn in list(self._conns.values()):
                self._writable(conn, resume=False)
        if DRAIN in intents:
            self._shutdown_wait()

    def _shutdown_wait(self) -> None:
        """Legacy SHUTDOWN and host signals use the bounded TERM/grace/KILL ladder.

        Lifelines stay open through this wait (DL-48). Its drained fact permits a
        past-bound exit; maintenance has a separate positive idle proof (DL-390).
        """
        live = [r for r in self.runs.values() if r.wrapper_rc is None]
        # a JUST-spawned wrapper may not have written spawn.json, and _signal_command is a
        # no-op without it, so the wrapper would die only by lifeline EOF and record
        # "parent lost"; wait up to 5 s for the records first (DL-48)
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
                ended = REAPED
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
                ended = PAST_BOUND
                break
            time.sleep(0.02)
        self._process(DRAINED, ended)

    # -- reaping ------------------------------------------------------------

    def _drain_chld(self) -> None:
        try:
            while os.read(self._chld_r, 4096):
                pass
        except BlockingIOError:
            pass

    def _reap(self) -> None:
        """Reap every exited wrapper, close its lifeline and push the exit to the
        lease-holding connection (a droppable notification)."""
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
        """Bound LIST (ss11a). The newest `_LIST_COMPLETED_WINDOW` completed runs stay for a
        reconnecting controller; older ones are read from the spool. Idempotency resolves
        through the run directory, so eviction does not affect it. A SIGNAL for an evicted
        run answers `unknown_run`."""
        self._completed.append(run.run_id)
        while len(self._completed) > _LIST_COMPLETED_WINDOW:
            self.runs.pop(self._completed.popleft(), None)

    def _teardown(self) -> None:
        """Close the process. The teardown row of the state it ends from reports what
        this incarnation owns (supervisor_process.31 to .35). The intent authorises a
        checked unlink: the published socket only while its inode is the one renamed
        into place, the pid record only while it names this incarnation."""
        if self.state == "closed":
            return  # closed is final: a second teardown has nothing to release
        # Maintenance's owned teardown precedes its reply (DL-390). The process exit
        # releases final transport/lock descriptors even when their close reports an error.
        maintenance = self.state == "maintenance_completed"
        # closed first, so no request is dispatched while replies flush; one deadline for
        # all clients, a SHUTDOWN reply included
        unlinks = self._process(TEARDOWN).intents
        deadline = time.monotonic() + 2.0
        while any(conn.out for conn in self._conns.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = self._sel.select(timeout=remaining)
            except OSError:
                if not maintenance:
                    raise
                break  # lost completion delivery is CLI uncertainty, not failed idle proof
            for key, mask in ready:
                if key.data[0] == "conn" and mask & selectors.EVENT_WRITE:
                    self._writable(key.data[1], resume=False)
        for conn in list(self._conns.values()):
            if maintenance:
                with contextlib.suppress(OSError):
                    self._drop_conn(conn)
            else:
                self._drop_conn(conn)
        if self._listen is not None:
            with contextlib.suppress(Exception):
                self._listen.close()
        if UNLINK_PUBLISHED in unlinks:
            with contextlib.suppress(OSError):
                if os.stat(self.sock_path).st_ino == self._socket_inode:
                    os.unlink(self.sock_path)
        if UNLINK_PUBLISHED in unlinks or UNLINK_PRIVATE in unlinks:
            # `bound` writes supervisor.pid before the rename, so either class may own it
            with contextlib.suppress(RecursionError):
                record = _load_json(self.pid_path)
                if record is not None and record.get("incarnation") == self.incarnation:
                    with contextlib.suppress(OSError):
                        os.unlink(self.pid_path)
        if UNLINK_PRIVATE in unlinks:
            with contextlib.suppress(OSError):
                os.unlink(self._private_path)
        for run in self.runs.values():
            if run.wrapper_rc is None:
                with contextlib.suppress(OSError):
                    os.close(run.lifeline_w)
        if self._prior_wakeup_fd is not None:
            # before the pipe closes: a wakeup fd on a closed descriptor would write signal
            # numbers into whatever reuses the number
            signal.set_wakeup_fd(self._prior_wakeup_fd)
            self._prior_wakeup_fd = None
        for fd in (self._chld_r, self._chld_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        if maintenance:
            with contextlib.suppress(OSError):
                self._sel.close()
        else:
            self._sel.close()
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
        with contextlib.suppress(OSError):
            self._release_maintenance_lock()


def _is_grace_seconds(value: object) -> bool:
    """A finite number of zero or more; an integer past float range (10**400) is refused,
    as the wrapper's own check does, not raised."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        grace_s = float(value)
    except OverflowError:
        return False
    return math.isfinite(grace_s) and grace_s >= 0.0


#: the frozen ss2 wrapper-input schema as (key, predicate), the whole of it: a
#: fingerprint over an unpinned key type is not collision-free, and a field that
#: explodes after the fork kills the process whose death EOFs every wrapper.
#: `grace_seconds` must be FINITE (DL-151): SHUTDOWN escalates TERM->KILL after that
#: many seconds, so `Infinity` would make the shutdown unbounded.
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
    "grace_seconds": lambda v: _is_grace_seconds(v),
    "lifeline_fd": is_wire_int,
}

#: lifeline_fd is the supervisor's to fill (the fingerprint strips a stale one); nothing
#: else may be missing
_SPEC_OPTIONAL = frozenset({"lifeline_fd"})


def _spec_schema_error(spec: dict[str, Any]) -> str | None:
    """The frozen ss2 shape, or the first reason it is not.

    Unknown keys REFUSE: an unpinned key has an unpinned type, and the fingerprint's
    float encoding is injective only over pinned types. A NUL in any string field
    refuses too (DL-151): `os.open`, `subprocess.Popen` and `os.path` raise ValueError
    on it, not the OSError the wrapper guards with, so it would kill the wrapper after
    the fork with no `status.json` (the E7 case)."""
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
    """The frozen ss11a duplicate envelope built from `doc`, or None when the record
    cannot carry it or does not NAME this run. An unreadable, half-written or foreign
    record is no answer: the caller falls to the next table row."""
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


#: `_load_json`'s "present but unreadable" answer, distinct from None (absent): ss11a's
#: table keys on ABSENCE, and reading corruption as absence would authorize a second spawn.
_INVALID: dict[str, Any] = {"__invalid__": True}

#: `_indexed_owner`'s "cannot be listed" answer; outside the run_id grammar, so it never
#: equals a real owner
_UNREADABLE = "__unreadable__"


def _load_json(path: str) -> dict[str, Any] | None:
    """A wrapper-written spool record: plain JSON. ABSENT means exactly ENOENT; an EACCES
    or EIO file exists and is unreadable, and reading that as absence would erase
    evidence (ss11a). Bytes that are not UTF-8 are unreadable too (DL-151): they are
    decoded as strict UTF-8 first because `json.load` on a binary handle accepts UTF-16
    and UTF-32 (DL-229)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return _INVALID
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _INVALID
    if not isinstance(loaded, dict) or not spool_version_supported(loaded):
        return _INVALID
    return loaded


#: the ss11a tombstone schemas: required keys and type checks per file. A record that
#: answers a replay is EVIDENCE, refused when a field is missing or mistyped.
#: Every tombstone requires its version: `canon.decode` passes an ABSENT one
#: (canon.check_artifact_version leaves that to the reader), and an unversioned record
#: is unsupported evidence.
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
    """A supervisor-written ss11a record read through the ss3.2 ingress. Duplicate keys,
    floats, non-scalar strings, an unimplemented artifact_format_version (PR-08d/PR-12),
    non-UTF-8 bytes and a missing required field all refuse."""
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
        # FINITE and positive, in that order (DL-151): `nan` fails every comparison and
        # would fire on the first tick; `inf` never fires, which is no deadman at all
        print("supervisor: --deadman-seconds must be a finite positive number", file=sys.stderr)
        return 2
    # ENOENT on the run_root is a caller bug (the engine makes it first)
    if not os.path.isdir(args.run_root):
        print(f"supervisor: run-root {args.run_root!r} does not exist", file=sys.stderr)
        return 2
    # a unit that runs this supervisor must refuse a manual stop (DL-399)
    refusal = stop_guard_refusal()
    if refusal is not None:
        print(f"supervisor: {refusal}", file=sys.stderr)
        return 2
    try:
        supervisor = Supervisor(args.run_root, deadman_s=args.deadman_seconds)
    except ValueError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 1
    except StatechartError as exc:
        return _statechart_stop(exc)
    try:
        return supervisor.run()
    except OSError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # lock contention or the legacy live-owner gate
        print(str(exc), file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 1
    except StatechartError as exc:
        return _statechart_stop(exc)


def _statechart_stop(exc: StatechartError) -> int:
    """Every line of the report under one prefix, and the code the unit does not
    restart. The exit ends every command this supervisor ran, by lifeline EOF."""
    print(exc.report(), file=sys.stderr, flush=True)
    return EXIT_STATECHART_ERROR


if __name__ == "__main__":
    sys.exit(main())
