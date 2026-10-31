"""Runner control plane (ss10): the engine's public protocol, both ends (DL-78).

This is the OUTER contract: what operators, the ss11 TUI, the headless
`query`/`sendevent` CLI and any non-local controller speak to a running engine.
docs/control-protocol.md freezes it, as docs/supervisor-protocol.md freezes the
ss6a lifecycle tier's INNER contract. Server and clients live together so the wire
vocabulary has one definition.

Control plane (ss10; DL-45):

- A unix socket in the run root, mode 0600, carries JSON lines. Each sendevent
  parity verb names one oracle EventKind, but the set is not the whole alphabet:
  CHANGE_STATUS is the wire name of STATUS, and TIMER, MUST_START_ALARM and
  MUST_COMPLETE_ALARM are the engine's own and have no wire door. Inputs are
  injected source=control and journaled by the queued-input path; the engine's
  single-writer loop serializes them. There is no controller lease (DL-41a).
- Queries (status/trace/explain/plan) read the oracle store between feeds, which
  is safe because feed() never yields. subscribe streams journal records live:
  unsequenced dispatch/drop/decision records are at-least-once during the
  backfill race, seq'd records exactly once.
- A stale socket file from a crashed run is found by a probe connect and unlinked;
  a LIVE socket refuses a second engine.

TUI surface (ss11; DL-46; runner_tui.py's docstring holds the TUI-side detail):

- The ss10 status response carries two read-only fields per job, for the ss11
  views and any headless `query status` consumer. `pending_timers` holds (due,
  kind) pairs from Oracle.pending_timers(); its liveness filter mirrors the
  fire-time staleness rules, so a heap entry a fire would discard is not shown.
  `log_out`/`log_err` are the ss6 append targets of the CURRENT run, resolved by
  job_log_paths(), the resolver the adapter wrapper spec also uses. They are
  CMD-only; a never-started job reports only explicit std files.
- The protocol has no TUI verb: the TUI is `status` + `trace --since` + `explain`
  + `sendevent` + `subscribe`-as-wake-up, the headless surface (DL-45).

Read model (DL-78): every query handler is a pure projection of (oracle, catalog,
scheduler, spool paths) and mutates nothing. Because feed() never yields, a handler
never observes a half-applied event. Two handlers reach into oracle
package-privates (`_cond_true`, `_referencers`): explain and deps serve the
ORACLE's truth, never a second implementation.

Transports (DL-78): `ControlClient` is the persistent async client the ss11 TUI
drives; `roundtrip` is the one-shot blocking client the typer CLI drives outside an
event loop. Both speak one protocol, raise ControlClientError, and know nothing of
typer or textual.

Protocol v3 (DL-118; DL-90 introduced v2; docs/control-protocol.md is the frozen
text):

- **Every request names `"v": 3`.** control-protocol ss2 refuses a request that
  names none or another version (DL-90, DL-118); there is no unversioned fallback.
  The check sits in `_handle`, ahead of the subscribe branch. Under v3, `result`
  and standalone `effect` are one `decision`.
- **A mutation carries the concurrency-model ss6 envelope and is answered with its
  decision.** The request is `verb` + `payload` plus the required `request_id` and
  `expect`. The handler awaits: ss4 emits `command_committed` at step 4 and
  `oracle_applied` at step 7, and answering at the first would report a kill as
  written down, not as landed.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import graphlib
import hashlib
import json
import os
import re
import socket as socket_mod
import time
import uuid

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, get_args


from dsl41.canon import is_scalar_json, is_wire_int
from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.ir import ExecSpec, FwSpec, JobIR
from dsl41.runner_config import FW_DEFAULT_INTERVAL_S
from dsl41.oracle_state import (
    INJECTABLE_STATUSES,
    Event,
    EventKind,
    JobRuntime,
    JobStatus,
)
from dsl41.runner import Engine
from dsl41.runner_access import AccessControl, peer_principal
from dsl41.runner_adapters import LINE_LIMIT, job_log_paths
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    AdmissionRefused,
    ApplyResult,
    EnvelopeError,
    RequestCollision,
    addressed_key,
    parse_envelope,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import UNKNOWN_OUTCOME_CODES, Code
from dsl41.runner_journal import best_effort_report, read_backfill
from dsl41.reload_diff import catalog_hash_v2, catalog_payload
from dsl41.reload_update import Admitted, Refused
from dsl41.runner_config import EngineSettings
from dsl41.reload_plan import spec_texts
from dsl41.runner_update import APPLICATION_KEYS, OpenedPlan, UpdateRequest
from dsl41.runner_store import Store
from dsl41.runner_preflight import and_success_skeleton
from dsl41.state_machine import StatechartError

#: sendevent verbs whose payload is a single catalog job (1:1 onto EventKind)
JOB_EVENT_VERBS: frozenset[EventKind] = frozenset(
    {
        "STARTJOB",
        "FORCE_STARTJOB",
        "KILLJOB",
        "ON_ICE",
        "OFF_ICE",
        "ON_HOLD",
        "OFF_HOLD",
        "ON_NOEXEC",
        "OFF_NOEXEC",
        "DISARM",  # the DL-158 operator disarm (period-model ss10.4)
        "RELEASE_RESOURCE",  # frees the units a job still holds (DL-256)
    }
)
#: the parse alphabet of a typed status: every `JobStatus`. The TUI uses it to tell a
#: status from a job name. An operator may send only `INJECTABLE_STATUSES`, which
#: leaves out QUE_WAIT (DL-264).
STATUSES: frozenset[str] = frozenset(get_args(JobStatus))

#: the operator-flag alphabet of the `status` payload and the ORDER both surfaces render
#: it in (set states first, the armed latch last), so a flags cell is diffable by eye
#: (DL-68). `dsl41 query --brief` and the TUI import it: one alphabet, not two copies
#: (DL-145).
STATUS_FLAG_MARKS: tuple[tuple[str, str], ...] = (
    ("I", "on_ice"),
    ("H", "on_hold"),
    ("N", "on_noexec"),
    ("A", "armed"),
)

#: ss5's subscriber budget (DL-267): the encoded bytes one subscription may hold queued.
#: One command appends its input record (at most three times its request line, since the
#: stream escapes non-ASCII), then its decision, budgeted at one request line. The
#: bundled clients read lines up to the same bound, so a record that fits the budget is
#: one they can read.
SUBSCRIBER_BACKLOG_BYTES: int = 4 * LINE_LIMIT


#: the widest attempt note a surface shows (the TUI's column, `query --brief`'s status field);
#: a longer note, a ladder's exception text above all, is cut to it with an ellipsis
ATTEMPT_NOTE_WIDTH = 32


def attempt_note(row: Mapping[str, Any]) -> str:
    """What a `status` row's attempt adds to its status (DL-426), empty for a first attempt
    running: "attempt 2, after update_stop", or the attempt's own state while it stops,
    "STOPPING (engine_stop)", "STOPPING (ladder failed: ...)", or why an attempt keeps the
    job's starts back, "held: command survived its KILL", "held: stop ladder running". The
    TUI's attempt column shows it."""
    attempt = row.get("attempt")
    if not isinstance(attempt, Mapping):
        return ""
    if attempt.get("held"):  # an attempt that keeps the job's starts back
        note = f"held: {attempt.get('note')}"
    elif attempt.get("state") == "STOPPING":
        why = attempt.get("note") or attempt.get("cause")
        note = f"STOPPING ({why})" if why else "STOPPING"
    else:
        parts = [f"attempt {attempt['n']}"] if attempt.get("n", 1) != 1 else []
        if attempt.get("after"):
            parts.append(f"after {attempt['after']}")
        note = ", ".join(parts)
    if len(note) > ATTEMPT_NOTE_WIDTH:
        note = note[: ATTEMPT_NOTE_WIDTH - 1] + "…"
    return note


def status_label(row: Mapping[str, Any]) -> str:
    """A `status` row's status with its attempt beside it, as `query --brief` shows it:
    "RUNNING (attempt 2, after update_stop)", "STOPPING (ladder failed: ...)" (DL-426)."""
    status = str(row.get("status", ""))
    note = attempt_note(row)
    if not note or note.startswith("STOPPING"):
        return note or status
    return f"{status} ({note})"


#: A plan token names a directory under `plans/`: a sha256 hex digest, nothing else.
UPDATE_TOKEN = re.compile(r"[0-9a-f]{64}")

#: The key an update's `expect` names: the configuration rev the caller read.
CONFIG_KEY = "config"


#: The prose of each refusal the update chart makes (spec-reload), with the names it concerns.
_REFUSALS = {
    "stale_plan": "the plan was diffed against another configuration rev: run reread again",
    "box_running": "the plan edits the membership of a running box: {names}",
    "would_stop_running": "the plan stops running jobs: {names}; stop_running consents to it",
    "update_in_progress": "another update is stopping its jobs: {names}",
}


def _refusal_text(refusal: Refused) -> str:
    """The prose of an update's refusal."""
    return _REFUSALS[refusal.code].format(names=", ".join(refusal.names))


def _update_refusal(
    code: Code, error: str, token: object, names: Sequence[str] = ()
) -> dict[str, Any]:
    """An `update` refusal: it writes nothing and names the token and the jobs or boxes it
    concerns, none for a refusal about the request itself (control-protocol ss2)."""
    return _failure(code, error, refused=True) | {"token": token, "names": list(names)}


def _applied(body: Mapping[str, Any]) -> dict[str, Any]:
    """What an update's answer says of its application: its `config applied` record's rev,
    catalog hash and job sets."""
    return {key: body.get(key) for key in ("rev", "catalog_hash", *APPLICATION_KEYS)}


def _failure(code: Code, error: str, *, refused: bool = False) -> dict[str, Any]:
    """Every `ok: false` answer this server builds except the rejected decision (ss2's
    code table). `code` names the reason and `error` keeps the prose. `refused` says
    nothing was admitted, so an unknown-outcome code never carries it."""
    if refused and code in UNKNOWN_OUTCOME_CODES:
        raise ValueError(f"{code} is an unknown outcome and cannot be a refusal")
    answer: dict[str, Any] = {"ok": False, "code": code, "error": error}
    if refused:
        answer["refused"] = True
    return answer


class ControlServer:
    """ss10 control plane: a unix domain socket in the run directory, mode 0600 (0660 with
    its group set when an access map names a `socket_group`; access-model ss8, DL-146),
    JSON lines both ways. One request object per line, one response object per line
    ({"ok": bool, ...}), except `subscribe`, which streams journal records until the
    client hangs up.

    Every request names `"v": 3` (DL-90, DL-118). Queries: status [job], trace [since],
    explain job, spec job, deps job, timers, plan, global name, globals names; and
    subscribe [since]. An answer that passed routing and the lineage proof carries the
    concurrency-model ss6 read header (`baseline_id`, `applied_index`), so a
    client can tell which log a revision was read from. Everything sent BEFORE routing is
    headerless (DL-148): the bad-request and version refusals, the perimeter's credential
    refusal and denials, the lineage refusal, a raised handler's internal error, and
    every line `subscribe` writes on its own connection.

    A mutation is `{"cmd": "sendevent"}` plus the concurrency-model ss6 envelope: `verb`,
    `payload`, `request_id`, `expect`, optional `claimed_actor`. Job arguments are
    validated against the catalog (an unknown job is an error, as in vendor sendevent).
    `expect` is MANDATORY (concurrency-model ss0) and names the addressed entity's
    revision only. The answer is the DECISION, not the receipt.

    Revision-bearing reads (DL-87, concurrency-model ss6): `status` carries each job's
    `state_rev`, and `global` / `globals` answer `{present, value, state_rev}` for NAMED
    globals and insert nothing. An unset name is answered `{present: false, state_rev:
    0}`, because a map of existing globals cannot express the absence a conditional
    create conditions on. A caller composes an `expect` from these reads.

    Mutations go through Engine.submit (source=control), so the WAL journals every
    control input at admission (ss10: the WAL is the audit trail) and the single-writer
    loop serializes them; there is no controller lease at this tier (DL-41a). Queries
    read the oracle store directly, and because feed() never yields a handler never
    observes a half-applied event."""

    #: seconds between on-disk re-checks of the estate fingerprint, read lazily inside a
    #: status query
    DRIFT_CHECK_INTERVAL_S = 15.0

    #: how long a sendevent waits for its decision before answering "I do not know" (S3).
    #: Applying an input takes microseconds, so waiting near this long means the
    #: single-writer loop is not running. It is below `roundtrip`'s client timeout so the
    #: caller gets this diagnosis, not a bare socket timeout.
    DECISION_TIMEOUT_S = 5.0

    #: ss5's grace at a hangup the server makes for any reason but an overflow or a
    #: revocation (DL-267): how long a closing connection may take to hand its unsent
    #: bytes to a reading peer before it is aborted.
    HANGUP_GRACE_S = 2.0

    def __init__(
        self,
        engine: Engine,
        path: Path,
        *,
        spec_texts: Mapping[str, str] | None = None,
        estate_fingerprint: Mapping[str, str] | None = None,
        access: AccessControl | None = None,
    ) -> None:
        self.engine = engine
        self.path = path
        #: job -> preserve-rendered JIL block, post-placeholder (the `spec` verb, DL-64):
        #: what THIS run loaded. Embedders without source text serve jil: null.
        self.spec_texts: Mapping[str, str] = spec_texts or {}
        #: input-file path -> sha256 of the bytes the run LOADED. Source of the `spec_drift`
        #: status flag (DL-65): there is no reload, and the hint tells the operator the
        #: catalog differs from the files on disk.
        self.estate_fingerprint: Mapping[str, str] = estate_fingerprint or {}
        self._drift: bool = False
        self._drift_checked_at: float | None = None
        self._server: asyncio.Server | None = None
        self._conn_tasks: set[asyncio.Task[Any]] = set()
        #: set by `close`: a handler cancelled while it holds is shutdown's and gets the
        #: hangup grace; any other cancel is an overflow or revocation and ends the
        #: connection at once (ss5, DL-267)
        self._closing = False
        #: each subscription's backlog budget (ss5); tests lower it
        self.subscriber_budget = SUBSCRIBER_BACKLOG_BYTES
        #: the first `StatechartError` a connection handler or a background task of the
        #: run's supervisor client met (`stop_on`), and the event `_serve_run` waits on.
        #: It stops the engine with exit 7 (statechart-engine ss5): an assertion failure,
        #: never answered, never contained
        self.statechart_error: StatechartError | None = None
        self.failed = asyncio.Event()
        #: what `stop_on` calls first: `_serve_run` sets it to the loop task's cancel
        self.on_statechart_error: Callable[[], object] | None = None
        #: the armed perimeter, or None for the 0600 owner-only model (docs/access-model.md
        #: ss4)
        self.access = access
        # an update the engine applies swaps the catalog this server describes (spec-reload)
        engine.update.listeners.append(self.take_applied_plan)

    def take_applied_plan(self, opened: OpenedPlan) -> None:
        """An update applied: `spec` serves the applied plan's JIL, and `spec_drift` compares
        the files on disk with the applied plan's bytes, so it keeps saying whether the running
        catalog differs from disk (DL-65)."""
        self.spec_texts = spec_texts(opened.estate.parsed, self.engine.oracle.catalog)
        self.estate_fingerprint = opened.fingerprint()
        self._drift_checked_at = None  # read the disk again at the next status

    async def start(self) -> None:
        """Bind (0600 from birth via umask) after the stale-socket probe: a connect that
        succeeds means a LIVE engine serves this run root (refuse); a failed connect is a
        crashed run's leftover (unlink and claim)."""
        if self.path.exists():
            probe = socket_mod.socket(socket_mod.AF_UNIX)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except OSError:
                self.path.unlink()  # stale: nobody is listening
            else:
                raise EngineError(f"{self.path} is live: another engine is serving this run root")
            finally:
                probe.close()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(self.path), limit=LINE_LIMIT
            )
        except OSError as exc:
            # asyncio removes a socket already at the path before it binds, so one another
            # engine bound after the probe is replaced; this branch fires for a non-socket
            # or a permission error. A second engine fails the leader lock first
            # (concurrency-model §1, DL-100)
            raise EngineError(f"cannot bind control socket {self.path}: {exc}") from exc
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)  # belt: some platforms ignore umask on bind
        policy = None if self.access is None else self.access.policy
        if policy is not None and policy.socket_gid is not None:
            # access-model ss8: group reachability is deliberate; widen to the policy's group
            # or refuse to serve. The loader resolved the gid once (DL-152)
            group = policy.socket_group
            try:
                os.chown(self.path, -1, policy.socket_gid)
                os.chmod(self.path, 0o660)
            except OSError as exc:
                # bound but not armed: close() runs only after OUR bind, so it never
                # unlinks a live engine's socket
                await self.close()
                raise EngineError(f"cannot arm control socket for group {group!r}: {exc}") from exc

    async def close(self) -> None:
        # cancel handlers BEFORE wait_closed(): it blocks until every handler finishes, and
        # a subscribe handler parks on queue.get(), so the reverse order deadlocks shutdown
        # whenever a viewer is attached (DL-45)
        if self._server is not None:
            self._server.close()
            # one tick, so a connection accepted just before close lands in _conn_tasks
            await asyncio.sleep(0)
        self._closing = True
        for task in list(self._conn_tasks):
            task.cancel()
        await asyncio.gather(*self._conn_tasks, return_exceptions=True)
        self._conn_tasks.clear()
        # one more tick: a handler's abort only SCHEDULES connection_lost, and without
        # it the transport never detaches from the server
        await asyncio.sleep(0)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        # asyncio runs a client_connected_cb coroutine as a task
        if task is not None:  # pragma: no branch
            self._conn_tasks.add(task)
        at_once = False
        try:
            principal = None
            if self.access is not None:
                # access-model ss3: the principal is fixed at accept; no resolvable
                # credential refuses (the supervisor's permissive None is not copied)
                sock = writer.get_extra_info("socket")
                principal = peer_principal(sock) if sock is not None else None
                if principal is None:
                    await self._send(
                        writer,
                        _failure(
                            "peer_unauthenticated",
                            "access control is armed and this peer has no"
                            " resolvable kernel credential (access-model ss3)",
                            refused=True,
                        ),
                    )
                    return
            while True:
                line = await reader.readline()
                if not line.endswith(b"\n"):
                    # EOF, or EOF inside a line. ss2 frames a request as a LINE, and an
                    # unterminated fragment may be a truncated command whose client cannot
                    # know whether it arrived: dropped unanswered, never parsed (DL-216)
                    break
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                except (json.JSONDecodeError, ValueError, RecursionError) as exc:
                    # deeply nested JSON inside LINE_LIMIT raises RecursionError; it is a bad
                    # request, and ss2 forbids dying unreplied (DL-149, DL-151)
                    await self._send(
                        writer, _failure("malformed_request", f"bad request: {exc}", refused=True)
                    )
                    continue
                if request.get("v") != PROTOCOL_VERSION:
                    # control-protocol ss2 refuses a request naming no version or another
                    # (DL-90, DL-118), checked HERE so subscribe, which never reaches
                    # _respond, is not an unversioned door (DL-90). It carries `refused`
                    # because a MUTATION can hit this door, and every ok:false it can get,
                    # except the no-decision timeout, must say nothing was admitted (DL-92)
                    await self._send(
                        writer,
                        _failure(
                            "unsupported_version",
                            f"protocol version {request.get('v')!r}: this engine"
                            f' speaks v{PROTOCOL_VERSION} -- name it as {{"v":'
                            f" {PROTOCOL_VERSION}}}",
                            refused=True,
                        ),
                    )
                    continue
                if self.access is not None and principal is not None:
                    # access-model ss5: one decision per request, before the
                    # cmd split -- the one place subscribe and _respond share
                    # (the DL-90 lesson, applied the second time). A denial
                    # consumes no engine index and advances no engine time.
                    allowed, why = self.access.decide(
                        principal, request.get("cmd"), request.get("verb")
                    )
                    if not allowed:
                        await self._send(writer, _failure("access_denied", why, refused=True))
                        continue
                    # ss3: the authenticated spelling replaces the claim
                    request["claimed_actor"] = principal.spelling
                if request.get("cmd") == "subscribe":
                    if self.access is not None and principal is not None:
                        # ss7: reload revokes exactly the live streams that lost read, so
                        # the perimeter tracks the writer AND the handler task (parked on
                        # queue.get(); a close alone never wakes it on a quiet estate)
                        self.access.streams[writer] = (principal, task)
                        try:
                            await self._subscribe(writer, request)
                        finally:
                            self.access.streams.pop(writer, None)
                    else:
                        await self._subscribe(writer, request)
                    break  # a subscription owns its connection until hangup
                try:
                    response = await self._respond(request)
                except StatechartError:
                    raise  # an assertion failure is not a query bug: below
                except Exception as exc:  # noqa: BLE001 -- a query bug answers ok:false;
                    # an unreplied connection would only look like a timeout (DL-45)
                    response = _failure("internal_error", f"internal error: {exc!r}")
                await self._send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass  # client hangup mid-write: its problem, not the engine's
        except asyncio.CancelledError:
            at_once = not self._closing  # an overflow or a revocation
            raise
        except StatechartError as exc:
            # statechart-engine ss5, concurrency-model ss4: no reply; hang up and stop the
            # engine with exit 7
            self.stop_on(exc)
        finally:
            # still in _conn_tasks: a shutdown waits for this hangup
            await self._hang_up(writer, at_once=at_once)
            if task is not None:  # pragma: no branch -- the same task as above
                self._conn_tasks.discard(task)

    def stop_on(self, error: StatechartError) -> None:
        """Hand a handler's `StatechartError` to the run, which stops the engine and reports
        the first one (statechart-engine ss5). The supervisor client's background tasks
        hand theirs here too."""
        self.statechart_error = self.statechart_error or error
        if self.on_statechart_error is not None:
            self.on_statechart_error()
        self.failed.set()

    async def _hang_up(self, writer: asyncio.StreamWriter, *, at_once: bool) -> None:
        """End one connection without waiting on a peer that stopped reading (ss5, DL-267).
        A close keeps unsent bytes until the peer takes them, so it gets `HANGUP_GRACE_S`
        and the abort after that discards the rest. An overflow or revocation aborts at once."""
        if not at_once:
            writer.close()
            try:
                async with asyncio.timeout(self.HANGUP_GRACE_S):
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                return
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                writer.transport.abort()
                raise
        writer.transport.abort()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write(json.dumps(obj, sort_keys=True).encode("utf-8") + b"\n")
        await writer.drain()

    async def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route one already-versioned request and stamp the concurrency-model ss6 read
        header onto its answer."""
        response = await self._dispatch(request)
        # concurrency-model ss6: every read publishes where the log has reached and which
        # log it is, so a client can tell a revision it may name from one it may not. One
        # engine per run root answers, enforced by the `leader.lock` flock (DL-100).
        return response | {
            "baseline_id": self.engine.baseline_id,
            "applied_index": self.engine.frontiers.applied_index,
        }

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "sendevent":
            return await self._sendevent(request)
        if cmd == "status":
            return self._status(request)
        if cmd == "trace":
            return self._trace(request)
        if cmd == "explain":
            return self._explain(request)
        if cmd == "spec":
            return self._spec(request)
        if cmd == "deps":
            return self._deps(request)
        if cmd == "timers":
            return self._timers()
        if cmd == "global":
            return self._globals([request.get("name")])
        if cmd == "globals":
            names = request.get("names")
            if not isinstance(names, list):
                return _failure("invalid_argument", "globals requires a list of names")
            return self._globals(names)
        if cmd == "plan":
            return self._plan()
        if cmd == "engine_stop":
            return await self._engine_stop()
        if cmd == "catalog":
            return self._catalog()
        if cmd == "update":
            return await self._update(request)
        return _failure("unknown_cmd", f"unknown cmd {cmd!r}", refused=True)

    def _catalog(self) -> dict[str, Any]:
        """`catalog`: what `reread` diffs a candidate against (spec-reload): the catalog the
        engine runs with its hash, the estate settings it runs it under, the configuration rev,
        each row's status and run number, and the flags each tombstone of a removed job kept."""
        live = self.engine.update.live()
        return {
            "ok": True,
            "catalog": catalog_payload(live.catalog),
            "catalog_hash": catalog_hash_v2(live.catalog),
            "settings": dict(live.settings),
            "revision": live.revision,
            "status": dict(live.status),
            "runs": dict(live.runs),
            "tombstones": {name: list(flags) for name, flags in live.tombstones.items()},
        }

    async def _update(self, request: dict[str, Any]) -> dict[str, Any]:
        """`update`: apply the plan `reread` stored under TOKEN (spec-reload). The request
        carries the mutation envelope: `request_id`, `baseline_id` and `expect` naming the
        configuration rev the caller read, as `{"config": N}`. The answer is the admission's
        decision; with `wait`, it comes once the update applied, with the application."""
        token, stop_running, wait = (
            request.get("token"),
            request.get("stop_running", False),
            request.get("wait", False),
        )
        if not (isinstance(token, str) and UPDATE_TOKEN.fullmatch(token)):
            return _update_refusal(
                "invalid_argument", f"token must be a plan token, got {token!r}", token
            )
        if not isinstance(stop_running, bool) or not isinstance(wait, bool):
            return _update_refusal(
                "invalid_argument", "stop_running and wait must be booleans", token
            )
        try:
            envelope = parse_envelope(
                request, addressed=CONFIG_KEY, baseline_id=self.engine.baseline_id
            )
        except EnvelopeError as exc:
            return _update_refusal(exc.code, str(exc), token)
        submitted = UpdateRequest(
            token,
            stop_running,
            envelope.expect[CONFIG_KEY],
            self.engine.baseline_id,
            envelope.request_id,
            envelope.claimed_actor,
        )
        try:
            answer = await asyncio.wait_for(
                self.engine.request_update(submitted), self.DECISION_TIMEOUT_S
            )
        except TimeoutError:
            return _failure(
                "decision_timeout",
                f"no decision within {self.DECISION_TIMEOUT_S}s: the engine loop is not"
                " draining its queue. The update may still be admitted -- re-read before"
                " retrying, under the same request_id.",
            )
        if answer.code is not None:
            return _update_refusal(answer.code, answer.error or answer.code, token)
        decision = answer.decision
        if isinstance(decision, Refused):
            return _update_refusal(
                decision.code, _refusal_text(decision), decision.token, decision.names
            )
        assert isinstance(decision, Admitted)
        reply: dict[str, Any] = {
            "ok": True,
            "decision": "admitted",
            "token": decision.token,
            "request_id": envelope.request_id,
            "stop": list(decision.stop_set),
            "application": answer.application and _applied(answer.application),
        }
        if wait and reply["application"] is None:
            assert answer.applied is not None  # an admission's answer carries it
            try:
                body = await asyncio.wait_for(
                    asyncio.shield(answer.applied), self._application_window()
                )
            except TimeoutError:
                reply["pending"] = True
            else:
                reply["application"] = _applied(body)
        return reply

    def _application_window(self) -> float:
        """How long `update --wait` waits for the application: one stop ladder and the
        decision window. `status` reports it as `update_window_s`, so a client waits on the
        socket at least this long (control-protocol ss2)."""
        settings = self.engine.settings or EngineSettings()
        return settings.grace_term_s + settings.grace_kill_s + 30 + self.DECISION_TIMEOUT_S

    async def _engine_stop(self) -> dict[str, Any]:
        """`engine stop`: drain the engine (spec-drain). The request is a step of the single
        writer, so every mutation is admitted wholly before it or refused after it with
        `engine_draining`; a second one moves the drain's deadline to now. The answer is the
        lifecycle state the step left: DRAINING, or STOPPED when nothing was left to settle.
        The engine then records how it ended and exits."""
        try:
            state = await asyncio.wait_for(self.engine.request_stop(), self.DECISION_TIMEOUT_S)
        except TimeoutError:
            return _failure(
                "decision_timeout",
                f"the stop was not taken within {self.DECISION_TIMEOUT_S}s: the engine loop is"
                " not draining its queue; it may still take it -- re-read before retrying",
            )
        return {"ok": True, "engine": state}

    def _check_job(self, job: object) -> dict[str, Any] | None:
        if isinstance(job, str) and job in self.engine.oracle.catalog.jobs:
            return None
        return _failure("unknown_job", f"unknown job {job!r}")

    async def _sendevent(self, request: dict[str, Any]) -> dict[str, Any]:
        """One externally requested mutation through the ss4 admission order, answered with
        its DECISION, not its receipt.

        Framing comes first (the verb and payload say WHICH entity is addressed), then the
        envelope, since `expect` cannot be validated before the addressed key is known.
        Both refuse and admit nothing. Past that the engine owns the outcome: a
        precondition that lost its race is a recorded rejection, not a refusal, because it
        consumed an index and its time half fired timers."""
        payload_ev = self._event_for(request)
        if isinstance(payload_ev, dict):
            return _failure(payload_ev["code"], payload_ev["error"], refused=True)
        ev = payload_ev
        try:
            envelope = parse_envelope(
                request,
                addressed=addressed_key(ev.kind, ev.payload),
                baseline_id=self.engine.baseline_id,
            )
        except EnvelopeError as exc:
            # every ok:false before submitting is a refusal and says so in one field: only
            # "nothing was written" is safe to re-compose and resend unchanged
            return _failure(exc.code, str(exc), refused=True)
        return await self._decision(self.engine.submit(ev, envelope), kind=ev.kind)

    async def _decision(self, submitted: Awaitable[ApplyResult], *, kind: str) -> dict[str, Any]:
        """Wait out one submitted mutation and shape its answer, the half identical whatever
        it mutates.

        There is no `at`: the answer is a DECISION, and a retry's answer is the ORIGINAL
        decision, so echoing the retry's own stamp would make two answers to one command
        differ in a field that is not about the command. `index` is the handle; the leader
        timestamp lives in the log beside it."""
        try:
            result = await asyncio.wait_for(submitted, timeout=self.DECISION_TIMEOUT_S)
        except RequestCollision as exc:
            # a refusal of THIS request, whole: the id's earlier decision rides nested beside
            # it, so a caller whose retry does not match learns what the first command did
            # without it reading as this request's own (DL-217)
            refusal = _failure(exc.code, str(exc), refused=True)
            if exc.original is not None:
                refusal["original_decision"] = exc.original.model_dump(mode="json")
            return refusal
        except AdmissionRefused as exc:
            return _failure(exc.code, str(exc), refused=True)
        except TimeoutError:
            # not a decision and not a refusal: unknown. The input may be durably admitted
            # and about to apply, so a client must re-read. This is the ONE ok:false a
            # mutation can get without `refused`, which lets `outcome_of` read the absence
            # as uncertainty.
            return _failure(
                "decision_timeout",
                f"no decision within {self.DECISION_TIMEOUT_S}s: the engine loop is"
                " not draining. The command may still be admitted -- re-read before retrying.",
            )
        answer: dict[str, Any] = {
            "ok": result.decision == "applied",
            "kind": kind,
            "decision": result.decision,
            "index": result.index,
            "request_id": result.request_id,
            "revisions": result.revisions,
        }
        if result.reason is not None:
            answer["error"] = result.reason
        if result.code is not None:
            # the STORED code (period-model ss2.3), so an exact retry answers what the first
            # did; a record from before codes existed has none and the field is omitted
            answer["code"] = result.code
        return answer

    def _event_for(self, request: dict[str, Any]) -> Event | dict[str, Any]:
        """The verb half of framing: the Event a well-formed request names, or the refusal it
        earns. Job arguments are catalog-checked, as vendor sendevent does."""
        verb = request.get("verb")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return _failure("invalid_argument", f"payload must be an object, got {payload!r}")
        at = self.engine.clock.now()
        if isinstance(verb, str) and verb in JOB_EVENT_VERBS:
            job = payload.get("job")
            if (error := self._check_job(job)) is not None:
                return error
            ev = Event(at=at, kind=verb, payload={"job": job})
        elif verb == "SET_GLOBAL":
            name, value = payload.get("name"), payload.get("value")
            if not (isinstance(name, str) and name):
                return _failure("invalid_argument", "SET_GLOBAL requires a global name")
            if not isinstance(value, str):
                return _failure("invalid_argument", "SET_GLOBAL requires a string value")
            ev = Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": value})
        elif verb == "CHANGE_STATUS":
            job, status = payload.get("job"), payload.get("status")
            if (error := self._check_job(job)) is not None:
                # SEM-07 (DL-65): "JOB^INST" with INST a declared insert_xinst is a legal
                # CHANGE_STATUS target though no such job is in the catalog: an operator
                # satisfies a cross-instance atom the sandbox cannot see by overwriting the
                # store's pseudo-entity. Other job verbs stay catalog-only.
                name, sep, inst = job.rpartition("^") if isinstance(job, str) else ("", "", "")
                if not (sep and name and inst in self.engine.oracle.catalog.external_instances):
                    return error
            if not (isinstance(status, str) and status in INJECTABLE_STATUSES):
                # DL-264: QUE_WAIT is assigned by the capacity owner and the oracle refuses
                # it as an injected status; by then the input is durable and every replay
                # would stop on it, so refuse before the WAL append
                injectable = f"injectable: {sorted(INJECTABLE_STATUSES)}"
                if isinstance(status, str) and status in STATUSES:
                    return _failure(
                        "status_not_injectable", f"status {status!r} cannot be sent ({injectable})"
                    )
                return _failure("unknown_status", f"unknown status {status!r} ({injectable})")
            status_payload: dict[str, object] = {"job": job, "status": status}
            if "exit_code" in payload:
                if not isinstance(payload["exit_code"], int) or isinstance(
                    payload["exit_code"], bool
                ):
                    # `true` is an int in Python and would reach the WAL as one (DL-151)
                    return _failure("invalid_argument", "exit_code must be an integer")
                status_payload["exit_code"] = payload["exit_code"]
            ev = Event(at=at, kind="STATUS", payload=status_payload)
        else:
            return _failure("unknown_verb", f"unknown verb {verb!r}")
        if not is_scalar_json(ev.payload):
            # PR-10a: a lone surrogate is a legal str and JSON escape, but canonicalization
            # raises on it, and one admitted SET_GLOBAL value would leave the estate
            # unsealable (period-model ss3.2): the door refuses it and writes nothing
            return _failure("invalid_argument", "payload carries an unpaired surrogate")
        return ev

    def _spec_drift(self) -> bool | None:
        """None when there is no fingerprint to check against (embedders). Re-reads lazily at
        most every DRIFT_CHECK_INTERVAL_S; estate files are small, so the synchronous reads
        in a status query are acceptable."""
        if not self.estate_fingerprint:
            return None
        now = time.monotonic()
        if self._drift_checked_at is None or now - self._drift_checked_at >= (
            self.DRIFT_CHECK_INTERVAL_S
        ):
            self._drift_checked_at = now
            self._drift = False
            for path, digest in self.estate_fingerprint.items():
                try:
                    current = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except OSError:
                    self._drift = True  # unreadable/deleted counts as changed
                    break
                if current != digest:
                    self._drift = True
                    break
        return self._drift

    def _status(self, request: dict[str, Any]) -> dict[str, Any]:
        catalog = self.engine.oracle.catalog
        store = self.engine.oracle.store
        job = request.get("job")
        if job is not None:
            if not isinstance(job, str) or (job not in catalog.jobs and job not in store.job):
                return _failure("unknown_job", f"unknown job {job!r}")
            names = [job]
        else:
            names = sorted(set(catalog.jobs) | set(store.job))
        # ss11 additions (DL-46): pending timers by the oracle's liveness rules and the ss6 log
        # paths the wrapper appends to; the TUI reads these, never a re-derivation
        pending: dict[str, list[dict[str, str]]] = {}
        for due, timer_job, kind in self.engine.oracle.pending_timers():
            pending.setdefault(timer_job, []).append({"due": due.isoformat(), "kind": kind})
        jobs: dict[str, dict[str, Any]] = {}
        for name in names:
            rt = store.job.get(name) or JobRuntime()  # never insert from a query
            log_out = log_err = None
            job_ir = catalog.jobs.get(name)
            if self.engine.run_root is not None and job_ir is not None and job_ir.job_type == "CMD":
                if rt.run_number >= 1:
                    log_out, log_err = job_log_paths(job_ir, rt.run_number, self.engine.run_root)
                # lowering refuses a CMD job without a command, so `exec_` is an ExecSpec
                elif isinstance(job_ir.exec_, ExecSpec):  # pragma: no branch
                    # never ran: only explicit std files exist to tail
                    log_out, log_err = job_ir.exec_.std_out_file, job_ir.exec_.std_err_file
            jobs[name] = {
                "status": rt.status,
                "status_at": rt.status_at.isoformat() if rt.status_at else None,
                "run_number": rt.run_number,
                "exit_code": rt.exit_code,
                "on_ice": rt.on_ice,
                "on_hold": rt.on_hold,
                "on_noexec": rt.on_noexec,
                # DL-54 (DL-46): an armed job looks INACTIVE but the next condition edge
                # starts it; operators must see the latch, not tail the trace for SCHED_ARM
                "armed": rt.armed,
                # DL-68: the trigger of the most recent start, trace-cause verbatim (null =
                # never started)
                "started_by": rt.started_by,
                # DL-87: the revision an `expect` precondition names for this entity, on
                # every read so a client can say WHAT it saw
                "state_rev": rt.state_rev,
                # control-protocol ss4's held flag: one engine dispatches every start it
                # decides, so no start is held
                "held": False,
                "pending_timers": pending.get(name, []),
                "log_out": log_out,
                "log_err": log_err,
                # DL-65: catalog placement, so the ss11 table renders the box hierarchy without
                # a second query (null for store-only ghosts a CHANGE_STATUS invented)
                "job_type": job_ir.job_type if job_ir is not None else None,
                "box_name": job_ir.box.box_name if job_ir is not None else None,
            }
            if job_ir is not None:
                watching = self._fw_watching(job_ir)
                if watching is not None:
                    # DL-68: present ONLY for a live FW run; absence is the "not watching" signal
                    jobs[name]["watching"] = watching
            attempt = self.engine.attempt_view(name)
            if attempt is not None:
                # DL-426: present ONLY while the current run has an unsettled attempt
                jobs[name]["attempt"] = attempt
            held = self.engine.evidence.get(name)
            if held is not None:
                # spec-crash: the interrupted attempt a hold names, until the job's next start
                jobs[name]["interrupted"] = held
        answer: dict[str, Any] = {"ok": True, "jobs": jobs, "spec_drift": self._spec_drift()}
        # spec-reload: the configuration rev the engine runs, and an update while it stops
        answer["config_rev"] = self.engine.config_rev
        answer["update_window_s"] = self._application_window()
        update = self.engine.update.view()
        if update is not None:
            answer["update"] = update
        return answer

    def _globals(self, names: list[Any]) -> dict[str, Any]:
        """DL-87 (concurrency-model ss6): answer NAMED globals with `{present, value,
        state_rev}`, inserting nothing.

        An unset name is answered at revision 0, which is what a conditional create
        conditions on: the catalog seed is an input, so anything that exists is at 1 or
        more."""
        store = self.engine.oracle.store
        answers: dict[str, Any] = {}
        for name in names:
            if not isinstance(name, str):
                return _failure("invalid_argument", f"global name must be a string, got {name!r}")
            row = store.globals_.get(name)
            answers[name] = {
                "present": row is not None,
                "value": None if row is None else row.value,
                "state_rev": 0 if row is None else row.state_rev,
            }
        return {"ok": True, "globals": answers}

    def _trace(self, request: dict[str, Any]) -> dict[str, Any]:
        since = request.get("since", 0)
        if not is_wire_int(since):
            return _failure("invalid_argument", "since must be an integer trace seq")
        last_seq, entries = self.engine.oracle.trace_since(since)
        return {
            "ok": True,
            "last_seq": last_seq,
            "entries": [
                {
                    "seq": seq,
                    "at": entry.at.isoformat(),
                    "job": entry.job,
                    "transition": entry.transition,
                    "cause": entry.cause,
                }
                for seq, entry in enumerate(entries, start=max(since, 0) + 1)
            ],
        }

    def _explain(self, request: dict[str, Any]) -> dict[str, Any]:
        job = request.get("job")
        if (error := self._check_job(job)) is not None:
            return error
        assert isinstance(job, str)
        from dsl41.dsl import cond_to_source  # heavyweight surface: load on demand

        oracle = self.engine.oracle
        attr = oracle.catalog.jobs[job].sem.condition
        if attr is None:
            return {"ok": True, "job": job, "condition": None, "satisfied": True, "atoms": []}
        cond = attr.cond
        # oracle._cond_true is package-private on purpose: explain uses the ORACLE's truth
        # (ice bypass, lookback, instances), never a copy
        atoms: list[dict[str, Any]] = []
        for atom in iter_atoms(cond):
            entry: dict[str, Any] = {
                "atom": cond_to_source(atom),
                "true": oracle._cond_true(atom, job),
            }
            if isinstance(atom, GlobalAtom):
                # DL-66: atom truth alone hides WHY; serve the effective global value (null =
                # never set) beside it. `global`/`globals` answer a NAMED global (DL-87);
                # this reads the same value where the condition uses it
                entry["actual"] = oracle.store.global_value(atom.name)
            atoms.append(entry)
        return {
            "ok": True,
            "job": job,
            "condition": cond_to_source(cond),
            "satisfied": oracle._cond_true(cond, job),
            "atoms": atoms,
        }

    def _spec(self, request: dict[str, Any]) -> dict[str, Any]:
        job = request.get("job")
        if (error := self._check_job(job)) is not None:
            return error
        assert isinstance(job, str)
        job_ir = self.engine.oracle.catalog.jobs[job]
        return {
            "ok": True,
            "job": job,
            "job_type": job_ir.job_type,
            "box_name": job_ir.box.box_name,
            "jil": self.spec_texts.get(job),  # null: server started without source texts
        }

    def _deps(self, request: dict[str, Any]) -> dict[str, Any]:
        """DL-65 (the list-dependencies analog). upstream/globals are what this job's
        condition references, split by ATOM TYPE, never by sniffing the oracle's key
        strings, where a job named 'g:x' (DL-39 escapes) collides with global x.
        downstream is the jobs whose conditions reference this one, from the oracle's
        edge-trigger index (package-private like _explain; the index shares that key
        collision for pathological names). Containment is served too, because condition
        edges are not a box's whole blast radius: box_name (upward) and members
        (downward)."""
        job = request.get("job")
        if (error := self._check_job(job)) is not None:
            return error
        assert isinstance(job, str)
        oracle = self.engine.oracle
        job_ir = oracle.catalog.jobs[job]
        upstream: set[str] = set()
        globals_: set[str] = set()
        if job_ir.sem.condition is not None:
            for atom in iter_atoms(job_ir.sem.condition.cond):
                if isinstance(atom, GlobalAtom):
                    globals_.add(atom.name)
                else:
                    upstream.add(atom.job.key)
        members = (
            sorted(n for n, j in oracle.catalog.jobs.items() if j.box.box_name == job)
            if job_ir.job_type == "BOX"
            else []
        )
        return {
            "ok": True,
            "job": job,
            "upstream": sorted(upstream),
            "globals": sorted(globals_),
            "downstream": sorted(oracle._referencers.get(job, [])),
            "box_name": job_ir.box.box_name,
            "members": members,
        }

    def _fw_watching(self, job_ir: JobIR) -> dict[str, Any] | None:
        """{file, interval, min_size} for a live FW run (DL-68). A watch is an in-flight
        adapter task with no registry or status field, so the facts come from the spec;
        interval resolves through the FW adapter's default, the number the poll sleeps."""
        if not isinstance(job_ir.exec_, FwSpec) or job_ir.name not in self.engine.live_jobs():
            return None
        spec = job_ir.exec_
        # an int, as the adapter's own default is: the number goes on the wire
        fallback = int(FW_DEFAULT_INTERVAL_S)
        default = getattr(self.engine.adapters.get("FW"), "default_interval_s", fallback)
        return {
            "file": spec.watch_file,
            "interval": spec.watch_interval or default,
            "min_size": spec.watch_file_min_size,
        }

    def _timers(self) -> dict[str, Any]:
        """DL-65 (the list-timers analog): every pending oracle timer plus each scheduled
        job's next calendar tick, in one due-ordered list. DL-68: live filewatches follow
        as due-less rows; they fire on a file, not a clock, but are a pending trigger."""
        entries: list[dict[str, Any]] = [
            {"due": due.isoformat(), "job": job, "kind": kind}
            for due, job, kind in self.engine.oracle.pending_timers()
        ]
        if self.engine.scheduler is not None:
            entries.extend(
                {"due": tick.isoformat(), "job": job, "kind": "schedule"}
                for tick, job in self.engine.scheduler.upcoming()
            )
        entries.sort(key=lambda e: (e["due"], e["job"]))
        catalog = self.engine.oracle.catalog
        for name in sorted(self.engine.live_jobs()):
            job_ir = catalog.jobs.get(name)
            # the engine's `_live` is keyed by catalog jobs, which never change under a run
            if job_ir is None:  # pragma: no cover
                continue
            watching = self._fw_watching(job_ir)
            if watching is None:
                continue
            # no "watching" prefix: the kind column says filewatch and the path is the information
            detail = f"{watching['file']} every {watching['interval']}s"
            if watching["min_size"] is not None:
                detail += f", min_size {watching['min_size']}"
            entries.append({"due": None, "job": name, "kind": "filewatch", "detail": detail})
        return {"ok": True, "timers": entries}

    def _plan(self) -> dict[str, Any]:
        sorter = graphlib.TopologicalSorter(and_success_skeleton(self.engine.oracle.catalog))
        try:
            sorter.prepare()
        except graphlib.CycleError as exc:
            return _failure(
                "plan_cycle",
                f"plan disabled: cycle in the AND-success skeleton ({' -> '.join(exc.args[1])})",
            )
        waves: list[list[str]] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready())
            waves.append(ready)
            sorter.done(*ready)
        return {"ok": True, "waves": waves}

    async def _subscribe(self, writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
        """Stream the store's wire records: an optional backfill from `since` (an input's index;
        the cut is positional, everything after the last input at or below it), then live.

        The seam is the journal's last seq, read in the same step that joins the feed to the
        fan-out, with no await between: every record at or below it is the backfill's, every
        record above it is the feed's, so no record is skipped or sent twice.

        What the store does not retain cannot be sent; that case is the GAP MARKER, not a short
        stream (DL-135)."""
        journal = self.engine.journal
        if journal is None:
            await self._send(writer, _failure("no_journal", "this run has no journal"))
            return
        since = request.get("since")
        if since is not None and not is_wire_int(since):
            await self._send(writer, _failure("invalid_argument", "since must be an integer seq"))
            return
        handler = asyncio.current_task()

        def overflowed(why: str) -> None:
            # ss5's overflow rule (DL-267): the journal removed the feed and dropped its
            # backlog. End the stream first; the report is best effort and follows.
            # `_subscribe` is awaited only by the connection handler's task
            if handler is not None:  # pragma: no branch
                handler.cancel()
            best_effort_report(f"dsl41: closing a subscribe stream: {why}")

        feed = journal.subscribe(self.engine.machines, overflowed, budget=self.subscriber_budget)
        seam = journal.last_seq
        try:
            # the ack names the cursor, so a client that reads no seq'd record still has one
            # (ss5, DL-267)
            cursor = since if since is not None else self.engine.frontiers.committed_index
            await self._send(writer, {"ok": True, "subscribed": True, "since": cursor})
            if since is not None and not await self._backfill(writer, journal.store, since, seam):
                return
            feed.go_live()
            while True:
                record = await feed.get()
                seq = record.get("seq")
                if since is not None and isinstance(seq, int) and seq <= since:
                    continue  # a cursor ahead of the store: what it already holds is not owed
                await self._send(writer, record)
        finally:
            journal.unsubscribe(feed)

    async def _backfill(
        self, writer: asyncio.StreamWriter, store: Store, since: int, seam: int
    ) -> bool:
        """Send what a subscriber resuming at `since` is owed, up to the seam; False when the
        stream ended on a refusal.

        This phase holds the unsent records it read, outside the subscriber budget: the
        retained store. Each is released once sent, and the list is gone on return (ss5,
        DL-267)."""
        try:
            backfill = read_backfill(store, since=since, upto=seam)
        except EngineError as exc:
            # the ack has gone, so the refusal goes on the STREAM; a raising handler would
            # hang the client up unanswered
            await self._send(writer, _failure(exc.code or "engine_error", str(exc)))
            return False
        gap_from, records = backfill.gap_from, backfill.records
        del backfill
        if gap_from is not None:
            # ss11: a cursor below the earliest retained record is told so explicitly;
            # silence would read as "nothing happened since your cursor"
            await self._send(writer, {"gap": True, "earliest_retained": gap_from})
        pending = deque(records)
        del records
        while pending:
            await self._send(writer, pending.popleft())
        return True


# ---------------------------------------------------------------- clients (ss10)


class ControlClientError(RuntimeError):
    """The control socket is unreachable or hung up mid-exchange.

    `delivered` separates the operator's two next moves (ss3, `outcome_of`). A request
    whose write was never attempted changed nothing and is safe to send again UNCHANGED.
    A request whose write was attempted and got no answer is `unknown`: the engine fsyncs
    the attempt before it feeds it, so the command may be durably admitted. Its only safe
    retry is under the same `request_id` with the same envelope (DL-216, DL-217).

    It defaults to False, the claim of less: a False on a delivered failure would tell
    an operator to resend, and a command would apply twice."""

    def __init__(self, *args: object, delivered: bool = False) -> None:
        super().__init__(*args)
        self.delivered = delivered


class StreamLineTooLong(ControlClientError):
    """A `subscribe` stream line past `SUBSCRIBER_BACKLOG_BYTES` (ss5, DL-267). The
    stream cannot resync past it, and a resubscription at the same cursor meets the
    same record, so retrying does not resume."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"record line over the {limit}-byte limit: resubscribing at the same"
            " cursor meets it again"
        )


def versioned(request: dict[str, Any]) -> dict[str, Any]:
    """Stamp `v` unless the caller already did. The clients implement the protocol, so
    query sites do not repeat the version; a caller that names one keeps it, which makes
    the server's refusal testable. Public because the CLI's raw subscribe socket is a
    client too and must stamp `v` or hang on the server's refusal."""
    return request if "v" in request else {**request, "v": PROTOCOL_VERSION}


def _frame(request: dict[str, Any]) -> bytes:
    """The line-delimited wire encoding of `request`. The two command clients call it
    before the transport is touched (DL-216), so an unencodable request fails as a
    caller's error, never a transport outcome. The subscribe clients frame inside the
    connect; DL-216 leaves `subscribe` alone."""
    return json.dumps(versioned(request)).encode("utf-8") + b"\n"


# ------------------------------------------------ composing a command (concurrency-model ss6)
#
# The client half of the protocol lives here for the reason the server half does
# (DL-78): the wire vocabulary has one definition. A mutation (S3) is a shape a caller
# BUILDS: read the addressed entity, name its revision. "Which query answers a `global:`
# key, and where the revision sits in its answer" is written once, here.
#
# The round trip stays at the call site: `ControlClient` and `roundtrip` are two
# transports for one protocol, and these functions are what both send and read.


def read_for(key: str) -> dict[str, Any]:
    """The query that answers the revision of a concurrency-model ss6-namespaced `key`."""
    namespace, _, name = key.partition(":")
    if namespace == "global":
        return {"cmd": "global", "name": name}
    return {"cmd": "status", "job": name}


def revision_in(response: Mapping[str, Any], key: str) -> int:
    """The revision `key` sits at, read out of the answer to `read_for(key)`.

    A REFUSED read answers 0. `status` refuses a job with neither a catalog entry nor a
    row; an entity with no row is at revision 0, which is what a SEM-07 CHANGE_STATUS on
    a not-yet-invented "JOB^INST" names. The command's own catalog check still catches a
    typo."""
    namespace, _, name = key.partition(":")
    if not response.get("ok"):
        return 0
    if namespace == "global":
        return int(response["globals"][name]["state_rev"])
    return int(response["jobs"][name]["state_rev"])


#: the four things a v2 command answer can mean; `ok: False` covers three outcomes with
#: three different next moves, and a client that cannot tell them apart guesses when
#: guessing is most expensive (DL-92)
APPLIED, REFUSED, REJECTED, UNKNOWN = "applied", "refused", "rejected", "unknown"


def outcome_of(response: Mapping[str, Any]) -> str:
    """Classify one `sendevent` answer (control-protocol ss3, ss6).

    ``applied``   the oracle applied it; `revisions` says what moved.
    ``refused``   THIS request was not admitted, took no index and the log says NOTHING
                  about it (DL-90), so it is safe to fix and send again. A refused RETRY
                  says nothing about its original, which may have applied (DL-217).
    ``rejected``  a DECISION went against it. It took an index and its batch's time half
                  fired, so it is in the log. Re-READ and re-decide; resending the same
                  envelope loses the same race, because `expect` is part of it.
    ``unknown``   no decision arrived within the server's window. The command may be
                  durably admitted and about to apply. Re-read, and retry only under the
                  SAME `request_id`, the one path that cannot apply it twice.

    The rule is a reading of the protocol, not of a screen: the CLI maps it to an exit
    code and the TUI to a sentence, but what the answer MEANS is the same."""
    if response.get("ok"):
        return APPLIED
    if response.get("refused"):
        return REFUSED
    if response.get("decision") == REJECTED:
        return REJECTED
    return UNKNOWN


def original_decision_text(response: Mapping[str, Any]) -> str | None:
    """The second fact a collision refusal carries, in words, or None (DL-217).

    The refusal is still the answer to THIS request and nothing of it was admitted. This
    sentence is about the EARLIER command that holds the id; a surface prints it beside
    the refusal, never instead. The CLI and the TUI share this spelling."""
    original = response.get("original_decision")
    if not isinstance(original, Mapping):
        return None
    head = (
        f"request_id {original.get('request_id')} was decided earlier:"
        f" {original.get('decision')} at index {original.get('index')}"
    )
    if original.get("decision") == REJECTED:
        return f"{head}: {original.get('reason')}"
    revisions = original.get("revisions") or {}
    moved = ", ".join(f"{key}={rev}" for key, rev in sorted(revisions.items()))
    return f"{head}, revisions {moved or 'none'}"


def command(
    verb: str,
    payload: Mapping[str, Any],
    *,
    key: str,
    revision: int,
    baseline_id: str,
    request_id: str | None = None,
    claimed_actor: str | None = None,
) -> dict[str, Any]:
    """One concurrency-model ss6 `sendevent` envelope, complete. `request_id` defaults to a
    fresh uuid4; a caller that RETRIES passes the original's."""
    request: dict[str, Any] = {
        "cmd": "sendevent",
        "baseline_id": baseline_id,
        "request_id": request_id or str(uuid.uuid4()),
        "verb": verb,
        "payload": dict(payload),
        "expect": {key: revision},
    }
    if claimed_actor is not None:
        request["claimed_actor"] = claimed_actor
    return request


def claimed_actor() -> str:
    """What this process says it is (concurrency-model ss6). A CLAIM, never an
    authorization: without an access map the socket's mode is the whole boundary and this
    is a breadcrumb in the log; on an armed estate the server overwrites it with the
    kernel-authenticated principal before anything is fingerprinted or journaled
    (access-model ss3, DL-146)."""
    try:
        user = getpass.getuser()
    except Exception:  # no passwd entry (containers): the claim is still useful
        user = f"uid{os.getuid()}"
    return f"{user}@{socket_mod.gethostname()}"


class ControlClient:
    """JSON-lines client of the ss10 control socket: one persistent request/response
    connection serialized by a lock. subscribe() opens its OWN connection, since a
    subscription owns its connection until hangup (ss10). Any transport error drops the
    connection so the next request reconnects, and the client outlives engine restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        # serialized before the writer is touched, so an unencodable request fails as itself,
        # not as a transport outcome (DL-216)
        encoded = _frame(payload)
        async with self._lock:
            #: whether a write of this request was ATTEMPTED, set just before `write()`: a
            #: failing drain may fail after the kernel took every byte, so every failure
            #: from the write on is `delivered` (DL-216; see ControlClientError)
            sent = False
            try:
                if self._reader is not None and self._reader.at_eof():
                    # the engine hung up on this idle connection (usually a restart).
                    # Writing into it would turn a request that never reached an engine
                    # into `delivered`, so reconnect first. A hang-up between this check
                    # and the write reads as `delivered`, the side that claims less
                    # (DL-216)
                    await self._drop()
                if self._writer is None:
                    # limit: one `status` line covers every job and overruns asyncio's 64 KiB
                    # default at ~300 jobs
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        str(self.path), limit=LINE_LIMIT
                    )
                assert self._reader is not None
                sent = True
                self._writer.write(encoded)
                await self._writer.drain()
                line = await self._reader.readline()
            except OSError as exc:
                await self._drop()
                raise ControlClientError(str(exc), delivered=sent) from exc
            except ValueError as exc:
                # readline answers a line over LINE_LIMIT with ValueError: the answer is
                # unreadable and the stream cannot resync, but the request was delivered.
                # ss1: this client raises ControlClientError, never a raw decode error
                # (DL-151)
                await self._drop()
                raise ControlClientError(
                    f"unreadable response line: {exc}", delivered=sent
                ) from exc
            except Exception as exc:
                # anything else raised once the write was attempted (say, a signal
                # handler's exception out of the drain) is a request that may have arrived
                # (DL-216); before the write it propagates as is
                await self._drop()
                if not sent:
                    raise
                raise ControlClientError(
                    f"control socket {self.path}: {exc!r}", delivered=True
                ) from exc
            except BaseException:
                # a CANCELLED exchange leaves its response unread on the stream; a
                # reused connection would hand that line to the NEXT request and
                # offset every reply after it, so drop it and reconnect lazily (DL-46)
                await self._drop()
                raise
            if not line:
                await self._drop()
                raise ControlClientError("engine hung up", delivered=True)
            try:
                response = json.loads(line)
            except (ValueError, RecursionError) as exc:
                await self._drop()
                raise ControlClientError(f"bad response line: {exc}", delivered=True) from exc
            if not isinstance(response, dict):
                await self._drop()
                raise ControlClientError("response is not a JSON object", delivered=True)
            return response

    async def subscribe(self, since: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield journal records until the engine hangs up. Raises ControlClientError if the
        connection fails or the engine refuses (a journal-less run)."""
        limit = SUBSCRIBER_BACKLOG_BYTES  # a stream line may be any record the budget holds
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path), limit=limit)
        except OSError as exc:
            raise ControlClientError(str(exc)) from exc
        try:
            request: dict[str, Any] = {"cmd": "subscribe", "v": PROTOCOL_VERSION}
            if since is not None:
                request["since"] = since
            writer.write(_frame(request))
            await writer.drain()
            ack_line = await reader.readline()
            if (why := _subscribe_refusal(ack_line)) is not None:
                raise ControlClientError(why)
            while True:
                try:
                    line = await reader.readline()
                except ValueError as exc:  # asyncio's answer to a line over `limit`
                    raise StreamLineTooLong(limit) from exc
                if not line:
                    return  # engine gone; the caller decides whether to retry
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # torn record: it is only a wake-up signal anyway
                yield record
        except (OSError, ValueError) as exc:
            # ValueError is asyncio's answer to an ack over the limit; the stream cannot
            # resync, and ss1 says every failure this client meets leaves as
            # ControlClientError (DL-151)
            raise ControlClientError(str(exc)) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _drop(self) -> None:
        # detach BEFORE the awaits: _drop runs on cancellation paths, and a re-delivered
        # CancelledError mid-close must not leave a half-dead connection attached
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def roundtrip(
    socket_path: Path, request: dict[str, Any], *, timeout: float = 10.0
) -> dict[str, Any]:
    """One-shot blocking request/response for callers with no event loop (the typer
    `sendevent`/`query` verbs). Raises ControlClientError on any transport or decode
    failure; exit-code mapping is the CLI's job, so this stays free of typer (DL-78).

    Everything before the write is UNDELIVERED and everything from the write on is
    DELIVERED, because the boundary is what the failure means, not where it happened. A
    failed `sendall` is delivered (DL-216) whatever it raised: CPython runs signal
    handlers after the last successful send and a handler may raise anything, so no
    failure proves nothing arrived. The request is serialized before the connect for the
    same reason: an unencodable request is a caller's error, not a transport outcome."""
    encoded = _frame(request)
    conn = socket_mod.socket(socket_mod.AF_UNIX)
    try:
        try:
            conn.settimeout(timeout)
            conn.connect(str(socket_path))
        except OSError as exc:
            raise ControlClientError(f"control socket {socket_path}: {exc}") from exc
        try:
            conn.sendall(encoded)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > LINE_LIMIT:
                    # control-protocol ss6: every connection reads under an explicit ceiling;
                    # a peer that never ends the line would otherwise grow this buffer
                    # until the caller died (DL-151)
                    raise ValueError(f"response line over the {LINE_LIMIT}-byte limit")
            response = json.loads(buf)
        except (OSError, ValueError, RecursionError) as exc:
            raise ControlClientError(
                f"control socket {socket_path}: {exc}", delivered=True
            ) from exc
        except Exception as exc:
            # any other exception from the write on (a signal handler that raised inside
            # `sendall` after the peer took the line) is the same unknown outcome (DL-216);
            # BaseExceptions propagate after the `finally` closes the socket
            raise ControlClientError(
                f"control socket {socket_path}: {exc!r}", delivered=True
            ) from exc
    finally:
        conn.close()
    if not isinstance(response, dict):
        raise ControlClientError(
            f"control socket {socket_path}: response is not a JSON object", delivered=True
        )
    return response


def _subscribe_refusal(ack: bytes) -> str | None:
    """Why `subscribe` did not start, or None when the first line is an ack that says it
    did.

    The server answers a refusal (no journal, a wrong version, a DL-146 perimeter denial)
    on the same connection and KEEPS IT OPEN; a caller that skips this line waits forever
    for records that never come (DL-151)."""
    if not ack:
        return "engine hung up before the subscribe ack"
    if not ack.endswith(b"\n"):
        # true on both transports sharing this check: the blocking reader's size-capped
        # readline() can truncate at the limit, while asyncio's readline() reaches here only
        # on a hang-up mid-line (a limit overrun raises ValueError upstream)
        return "the subscribe ack ended without a newline: read limit, or a hang-up mid-line"
    try:
        parsed = json.loads(ack)
    except (ValueError, RecursionError) as exc:
        return f"bad subscribe ack: {exc}"
    if not isinstance(parsed, dict):
        return "subscribe ack is not a JSON object"
    if not parsed.get("ok"):
        return str(parsed.get("error", "subscribe refused"))
    return None


def _stream_refusal(line: bytes) -> str | None:
    """The engine's words when a line after the ack is an answer, not a record: ss5's
    `{"ok": false, ...}` that ends a stream before its hangup (a backfill or lineage
    refusal; an overflow sends none). No record has a top-level `ok`, so only a line that
    spells one is parsed."""
    if b'"ok"' not in line:
        return None
    try:
        parsed = json.loads(line)
    except (ValueError, RecursionError):
        return None  # a torn record is skippable (control-protocol ss6)
    if not isinstance(parsed, dict) or parsed.get("ok") is not False:
        return None
    return str(parsed.get("error", "the engine ended the stream"))


def resume_cursor(line: str, cursor: int | None) -> int | None:
    """The `since` to resubscribe with once `line` has been read (ss5, DL-267): the ack's
    `since` first, then each record's `seq`. A line naming neither leaves the cursor."""
    try:
        parsed = json.loads(line)
    except (ValueError, RecursionError):
        return cursor
    if not isinstance(parsed, dict):
        return cursor
    value = parsed.get("since") if parsed.get("subscribed") is True else parsed.get("seq")
    return value if is_wire_int(value) else cursor


def subscribe_lines(socket_path: Path, request: dict[str, Any]) -> Iterator[str]:
    """`subscribe`, blocking: the ack line, then journal records, until the engine hangs up
    or the caller stops iterating.

    A third transport for one protocol (DL-172), beside `ControlClient` and `roundtrip`:
    its own AF_UNIX connection held for the life of the subscription (ss10), under the
    same stamped version and bounded read (control-protocol ss6).

    Raises OSError for a transport failure (unreachable socket, dead connection) and lets
    it propagate, so a caller can still name the socket with `refuse(exc, prefix=...)`
    (DL-78). Raises `ControlClientError` for a PROTOCOL refusal (the ack said no, or a
    record line ran past `SUBSCRIBER_BACKLOG_BYTES` unterminated, `StreamLineTooLong`),
    carrying the engine's own complete words. Two types on purpose: a caller cannot tell
    "the socket was gone" from "the engine said no" by parsing text (DL-92).

    An unversioned subscribe is refused, and (DL-151) a refusal does NOT close the
    connection, so a caller that skips the ack waits forever. `_subscribe_refusal` is that
    check, shared with `ControlClient.subscribe` (DL-178l); reading the ack IS opening the
    subscription.

    An answer line after the ack ends the stream the same way, as a `ControlClientError`
    carrying the engine's words (ss5, DL-267). A plain hangup ends the iteration; the
    caller decides what that means."""
    conn = socket_mod.socket(socket_mod.AF_UNIX)
    try:
        conn.connect(str(socket_path))
        conn.sendall(_frame(request))
        with conn.makefile("rb") as stream:
            ack = stream.readline(LINE_LIMIT + 1)
            if (why := _subscribe_refusal(ack)) is not None:
                raise ControlClientError(why)
            yield ack.decode("utf-8", "replace").rstrip("\n")
            limit = SUBSCRIBER_BACKLOG_BYTES  # any record the budget holds (ss5, DL-267)
            while line := stream.readline(limit + 1):
                # an unterminated line comes back AT the limit and the stream is unreadable
                # from here (DL-151); a shorter one is the engine closing the stream
                # mid-line, which an overflow's abort can do
                if not line.endswith(b"\n"):
                    if len(line) > limit:
                        raise StreamLineTooLong(limit)
                    raise ControlClientError("the engine closed the stream mid-line")
                if (why := _stream_refusal(line)) is not None:
                    raise ControlClientError(why)
                yield line.decode("utf-8", "replace").rstrip("\n")
    finally:
        conn.close()
