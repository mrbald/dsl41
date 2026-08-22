"""Runner control plane (ss10): the engine's public protocol, both ends.

Split out of runner.py + runner_tui.py + cli.py by DL-78, with the
paragraphs each owned, verbatim. The seam is the one tests already used:
tests/test_runner_control.py predates this module and named it.

This module is the OUTER contract -- what operators, the ss11 TUI, the
headless `query`/`sendevent` CLI and any future non-local controller speak
to a running engine. It is frozen in docs/control-protocol.md, the same
stance docs/supervisor-protocol.md takes for the ss6a lifecycle tier's
INNER contract. Server and clients live together so the wire vocabulary
has exactly one definition: before DL-78 the verb sets were runner.py
module-privates that runner_tui.py imported across the boundary, and the
two client implementations sat in two other modules.

Phase 11c (ss10; DL-45 pins the decisions):

- Control plane (ss10): unix socket in the run root, mode 0600, JSON
  lines. sendevent parity verbs map 1:1 onto oracle EventKind and are
  injected source=control (journaled by the queued-input path like every
  input; the engine's single-writer loop serializes them -- deliberately
  no controller lease here, DL-41a). Queries (status/trace/explain/plan)
  read the oracle store between feeds -- safe because feed() never yields.
  subscribe streams journal records live (at-least-once for unsequenced
  dispatch/drop/decision records during the backfill race; seq'd records
  exactly once). A stale socket file from a crashed run is detected by a
  probe connect and unlinked; a LIVE socket refuses the second engine.

Phase 11d (ss11; DL-46 pins the decisions; runner_tui.py's docstring is the
TUI-side normative detail):

- The ss10 status response grows two read-only fields per job, both for the
  ss11 views and useful to any headless `query status` consumer:
  `pending_timers` -- (due, kind) pairs from Oracle.pending_timers(), whose
  liveness filter mirrors the fire-time staleness rules (display truth is
  the dispatch truth: a heap entry a fire would discard as stale is not
  shown as pending) -- and `log_out`/`log_err`, the ss6 append targets of
  the CURRENT run resolved by job_log_paths(), the one resolver the adapter
  wrapper spec also uses (the log tail reads what the wrapper writes; the
  two can never diverge). CMD-only; a never-started job reports only
  explicit std files (nothing else exists to tail).
- The protocol itself gains no verb: the TUI is `status` + `trace --since`
  + `explain` + `sendevent` + `subscribe`-as-wake-up, exactly the headless
  surface (DL-45: the TUI consumes the same ss10 protocol, idempotently).

Read-model stance (DL-78): every query handler below is a pure projection
of (oracle, catalog, scheduler, spool paths) -- it mutates nothing. What
ties it to the engine's single-writer task is only that feed() never
yields, so a handler can never observe a half-applied event. Two handlers
deliberately reach into oracle package-privates (`_cond_true`,
`_referencers`): explain and deps must serve the ORACLE's truth, never a
second implementation of it.

Transports (DL-78): `ControlClient` is the persistent async client the ss11
TUI drives; `roundtrip` is the one-shot blocking client the typer CLI
drives from outside an event loop. They are two transports for one
protocol, not two protocols -- both raise ControlClientError and neither
knows anything about typer or textual.

Protocol v3 (DL-118; v2 was stage S3, DL-90; docs/control-protocol.md is
the frozen text). Two changes at v2, taken as ONE wire break because taking
them as two would break every client twice:

- **Every request names `"v": 3`** -- the version handshake control-protocol
  ss7 recorded as a known gap. No fallback to an older version:
  concurrency-model ss0 refuses a caller that does not name one, and "accept
  it unversioned for compatibility" is exactly the opt-out ss0 forbids. The
  check sits in `_handle`, ahead of the subscribe branch, so no door is left
  unversioned. v3 is DL-118: `result` and standalone `effect` become one
  `decision`, and a v2 client on `rec == "effect"` goes blind.
- **A mutation carries the ss6 envelope and is answered with its
  decision.** The flat `event`/`job`/`name` fields became `verb` +
  `payload`; `request_id` and `expect` joined them and are required. The
  handler now awaits: ss4 emits `command_committed` at step 4 and
  `oracle_applied` at step 7, and a request/response transport that
  answered with the first would tell an operator their kill was written
  down, not that it landed. A precondition nobody can see the outcome of is
  not a precondition.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import graphlib
import hashlib
import json
import os
import socket as socket_mod
import time
import uuid

from collections.abc import AsyncIterator, Awaitable, Mapping
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from dsl41.boundary import SealRequest
from dsl41.canon import is_scalar_json, is_scalar_string
from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.ir import ExecSpec, FwSpec, JobIR
from dsl41.oracle_state import Event, EventKind, HostRuntime, JobRuntime, JobStatus
from dsl41.runner import Engine
from dsl41.runner_adapters import LINE_LIMIT, job_log_paths
from dsl41.runner_admission import (
    PROTOCOL_VERSION,
    AdmissionRefused,
    ApplyResult,
    EnvelopeError,
    addressed_key,
    parse_envelope,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_hosts import HOST_VERBS, HostCommand
from dsl41.seal import StagedNextPeriod
from dsl41.runner_journal import read_backfill
from dsl41.runner_preflight import and_success_skeleton

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
    }
)
STATUSES: frozenset[str] = frozenset(get_args(JobStatus))

#: The operator-flag alphabet of the `status` payload and the ORDER both
#: surfaces render it in: the three set states first, the armed latch last,
#: so a flags cell is diffable by eye (DL-68). It lives beside the payload
#: that carries the keys, and `dsl41 query --brief` and the TUI both import
#: it -- two hand-kept copies of one alphabet were two alphabets waiting to
#: drift (DL-145).
STATUS_FLAG_MARKS: tuple[tuple[str, str], ...] = (
    ("I", "on_ice"),
    ("H", "on_hold"),
    ("N", "on_noexec"),
    ("A", "armed"),
)


class ControlServer:
    """ss10 control plane: a unix domain socket in the run directory, mode
    0600, JSON lines both ways. One request object per line; one response
    object per line ({"ok": bool, ...}), except `subscribe`, which streams
    journal records until the client hangs up.

    Every request names `"v": 3` (DL-90, DL-118). Queries: status [job], trace
    [since], explain job, spec job, deps job, timers, plan, global name,
    globals names; and subscribe [since]. Every answer carries the ss6 read
    header -- `baseline_id`, `epoch`, `applied_index` -- so a client can
    tell which log a revision it holds was read from.

    A mutation is `{"cmd": "sendevent"}` plus the ss6 envelope: `verb`,
    `payload`, `request_id`, `expect`, optional `claimed_actor`. Job
    arguments are validated against the catalog -- vendor sendevent errors
    on an unknown job rather than queueing it -- and `expect` is MANDATORY
    (concurrency-model ss0), naming the addressed entity's revision and
    nothing else. The answer is the DECISION, not the receipt.

    Revision-bearing reads (DL-87, concurrency-model ss6): `status` carries
    each job's `state_rev`, and `global` / `globals` answer
    `{present, value, state_rev}` for NAMED globals and insert nothing. The
    naming is the point -- a map of the globals that exist cannot express the
    absence a conditional create has to condition on, so an unset name is
    answered `{present: false, state_rev: 0}` rather than omitted. These are
    the reads a caller composes an `expect` from.

    Mutations go through Engine.submit (source=control), so the WAL journals
    every control input at admission (ss10: the WAL is the audit trail;
    there is no second log) and the single-writer loop serializes them --
    deliberately no controller lease at this tier (DL-41a). Queries read the
    oracle store directly: feed() never yields, so a handler task can never
    observe a half-applied event."""

    #: seconds between on-disk re-checks of the estate fingerprint (the
    #: drift hint below); reads happen lazily inside a status query
    DRIFT_CHECK_INTERVAL_S = 15.0

    #: how long a sendevent waits for its decision before answering "I do not
    #: know" (S3). Applying one input is microseconds, so anything near this
    #: means the single-writer loop is not running -- and the diagnosis is
    #: worth more than the wait. Deliberately below `roundtrip`'s client
    #: timeout so the caller gets this sentence instead of a bare socket
    #: timeout, which would say only that something did not answer.
    DECISION_TIMEOUT_S = 5.0

    #: A boundary is not a mutation and does not answer in 5s: it drains
    #: every admitted attempt, waits an unbound spawn and an unresolved
    #: KILL ladder out, and writes four artifacts. The client's other route
    #: is the committed seal in the next period (ss2.2), so a timeout here
    #: is `unknown` and never a refusal. Comfortably above the two
    #: independent `QUIESCE_WAIT_S` budgets the barrier can spend, so a
    #: HEALTHY seal never lands in the unknown branch.
    SEAL_TIMEOUT_S = 180.0

    def __init__(
        self,
        engine: Engine,
        path: Path,
        *,
        spec_texts: Mapping[str, str] | None = None,
        estate_fingerprint: Mapping[str, str] | None = None,
    ) -> None:
        self.engine = engine
        self.path = path
        #: job -> preserve-rendered JIL block, post-placeholder (the `spec`
        #: verb, DL-64): what THIS run actually loaded, not the template on
        #: disk. Optional -- embedders without source text serve jil: null.
        self.spec_texts: Mapping[str, str] = spec_texts or {}
        #: input-file path -> sha256 of the bytes the run LOADED (JIL +
        #: properties). The `spec_drift` status flag (DL-65, the
        #: daemon-reload-hint analog inverted): there is deliberately no
        #: reload -- the hint tells the operator the running catalog no
        #: longer matches the files on disk (cold restart to adopt).
        self.estate_fingerprint: Mapping[str, str] = estate_fingerprint or {}
        self._drift: bool = False
        self._drift_checked_at: float | None = None
        self._server: asyncio.Server | None = None
        self._conn_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        """Bind (0600 from birth via umask) after the stale-socket probe: a
        connect() that succeeds means a LIVE engine serves this run root --
        refuse; a refused/failed connect means a crashed run's leftover --
        unlink and claim it."""
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
            # two engines racing past the probe: the loser's bind fails --
            # same refusal class as the live-socket case
            raise EngineError(f"cannot bind control socket {self.path}: {exc}") from exc
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)  # belt: some platforms ignore umask on bind

    async def close(self) -> None:
        # cancel handlers BEFORE wait_closed(): since 3.12 wait_closed blocks
        # until every handler task finishes, and a subscribe handler is parked
        # on queue.get() until cancelled -- the reverse order deadlocks the
        # engine's shutdown whenever any viewer is attached (DL-45)
        if self._server is not None:
            self._server.close()
            # one tick: a connection accepted just before close spawns its
            # handler via a scheduled callback -- let it land in _conn_tasks
            # so the cancel sweep below reaches it too
            await asyncio.sleep(0)
        for task in list(self._conn_tasks):
            task.cancel()
        await asyncio.gather(*self._conn_tasks, return_exceptions=True)
        self._conn_tasks.clear()
        # one more tick: a cancelled handler's writer.close() only SCHEDULES
        # its connection_lost; without this the transport never detaches from
        # the server and its deallocator trips after the loop is gone
        await asyncio.sleep(0)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        self.path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conn_tasks.add(task)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                except (json.JSONDecodeError, ValueError) as exc:
                    await self._send(
                        writer, {"ok": False, "error": f"bad request: {exc}", "refused": True}
                    )
                    continue
                if request.get("v") != PROTOCOL_VERSION:
                    # ss0 refuses a caller that does not name a version, and it
                    # is checked HERE so that subscribe -- which owns its
                    # connection and never reaches _respond -- cannot be the
                    # one unversioned door left open (DL-90). It carries
                    # `refused` for the reason the envelope doors do: this is
                    # a door a MUTATION can hit, and every ok:false a mutation
                    # can be answered with, other than the no-decision
                    # timeout, has to say that nothing was admitted (DL-92)
                    await self._send(
                        writer,
                        {
                            "ok": False,
                            "refused": True,
                            "error": f"protocol version {request.get('v')!r}: this engine"
                            f' speaks v{PROTOCOL_VERSION} -- name it as {{"v":'
                            f" {PROTOCOL_VERSION}}}",
                        },
                    )
                    continue
                if request.get("cmd") == "subscribe":
                    await self._subscribe(writer, request)
                    break  # a subscription owns its connection until hangup
                try:
                    response = await self._respond(request)
                except Exception as exc:  # noqa: BLE001 -- a query bug must
                    # answer ok:false, never kill the connection unreplied
                    # (the client would only see a timeout; DL-45)
                    response = {"ok": False, "error": f"internal error: {exc!r}"}
                await self._send(writer, response)
        except (ConnectionResetError, BrokenPipeError):
            pass  # client hangup mid-write: its problem, not the engine's
        finally:
            if task is not None:
                self._conn_tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write(json.dumps(obj, sort_keys=True).encode("utf-8") + b"\n")
        await writer.drain()

    async def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        """Route one already-versioned request and stamp the ss6 read
        header onto its answer.

        *(Amended by DL-133, at build of period-model ss1.3.)* **The fence
        is re-proved before the answer, not only before an append.** Frozen
        v2 makes these reads leader-only and stamps lineage coordinates on
        each one, so a displaced leader that kept answering `status`,
        `routes` and backfill until its next mutation would be serving
        revisions from a lineage it no longer leads. A `status` immediately
        after the anchor is replaced is REFUSED, not answered (PR-03).

        **Two proofs, two rules, and the split is each spec's own.** The
        RUN ROOT's proof is CM-14's: losing it stops dispatch on the way
        into admission's first append, and that is what stops the
        incumbent. This check is the LINEAGE's proof alone -- refusing the
        run-root half here would leave a displaced leader answering nothing
        and stopping never, and would also refuse the read a client
        composes its `expect` from, so the mutation that was supposed to
        stop the engine would never be sent.*"""
        lost = self._lineage_lost()
        if lost is not None:
            return lost
        response = await self._dispatch(request)
        # ss6: every read publishes where the log has reached and which log it
        # is, so a client can tell a revision it may name from one it may not.
        # Leader-only in v2 -- one engine per run root IS the leader (ss7's
        # election arrives in S6), enforced by the socket probe in start().
        return response | {
            "baseline_id": self.engine.baseline_id,
            "epoch": self.engine.epoch,
            "applied_index": self.engine.frontiers.applied_index,
        }

    def _lineage_lost(self) -> dict[str, Any] | None:
        """The refusal a displaced leader owes a reader, or None.

        Deliberately WITHOUT the ss6 read header: the header publishes a
        baseline, an epoch and a log position, and those are exactly the
        coordinates this process may no longer speak for."""
        estate = self.engine.estate
        if estate is None or estate.anchor.lock.held is False:
            return None
        try:
            estate.anchor.check()
        except EngineError:
            pass
        else:
            return None
        return {
            "ok": False,
            "refused": True,
            "error": "this engine can no longer prove it leads this estate's lineage:"
            " the anchor was deleted or replaced. Nothing is answered from a lineage"
            " this process does not lead (period-model ss1.3, PR-03)",
        }

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "sendevent":
            return await self._sendevent(request)
        if cmd == "host":
            return await self._host(request)
        if cmd == "seal":
            return await self._seal(request)
        if cmd == "hosts":
            return self._hosts(request)
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
                return {"ok": False, "error": "globals requires a list of names"}
            return self._globals(names)
        if cmd == "plan":
            return self._plan()
        return {"ok": False, "error": f"unknown cmd {cmd!r}", "refused": True}

    def _check_job(self, job: object) -> dict[str, Any] | None:
        if isinstance(job, str) and job in self.engine.oracle.catalog.jobs:
            return None
        return {"ok": False, "error": f"unknown job {job!r}"}

    async def _sendevent(self, request: dict[str, Any]) -> dict[str, Any]:
        """One externally requested mutation, through the ss4 admission
        order, answered with its DECISION rather than its receipt.

        The order below is the ss4 order: framing first (the verb and its
        payload, which is what says WHICH entity is addressed), then the
        envelope -- `expect` cannot be validated before the addressed key is
        known. Both refuse; nothing is admitted. Past that, the engine owns
        the outcome, and a precondition that lost its race comes back as a
        recorded rejection, not a refusal: it consumed an index and its time
        half fired timers, so it happened."""
        payload_ev = self._event_for(request)
        if isinstance(payload_ev, dict):
            return payload_ev | {"refused": True}
        ev = payload_ev
        try:
            envelope = parse_envelope(
                request,
                addressed=addressed_key(ev.kind, ev.payload),
                baseline_id=self.engine.baseline_id,
            )
        except EnvelopeError as exc:
            # every ok:false this handler produces BEFORE submitting is a
            # refusal, and says so in one field: a machine client must be able
            # to tell "nothing happened and nothing was written" from "a
            # decision went against you", because only the first is safe to
            # re-compose and send again unchanged
            return {"ok": False, "error": str(exc), "refused": True}
        return await self._decision(self.engine.submit(ev, envelope), kind=ev.kind)

    async def _host(self, request: dict[str, Any]) -> dict[str, Any]:
        """One routing-table change, through the same ss4 admission order and
        answered with the same four outcomes (concurrency-model ss8).

        A separate `cmd` from `sendevent` because the verb sets are separate
        things: sendevent's map 1:1 onto oracle `EventKind`, and a host verb
        deliberately maps onto none -- a job's condition truth cannot depend
        on where its machine routes (DL-93). The ENVELOPE is the same
        envelope, parsed by the same function, because ss0 admits one
        mandate and not one per verb set."""
        parsed = self._host_command_for(request)
        if isinstance(parsed, dict):
            return parsed | {"refused": True}
        try:
            envelope = parse_envelope(
                request, addressed=parsed.key, baseline_id=self.engine.baseline_id
            )
        except EnvelopeError as exc:
            return {"ok": False, "error": str(exc), "refused": True}
        return await self._decision(self.engine.submit_host(parsed, envelope), kind=parsed.verb)

    async def _seal(self, request: dict[str, Any]) -> dict[str, Any]:
        """ss2.2's `seal` verb: one period boundary, requested over the
        socket and DECIDED by the `seal` record.

        Three things make it unlike every other mutating verb, and each is
        a consequence of what a boundary is. It names an `expect` on
        NOTHING, because it addresses no row. Its decision is a `seal`
        record rather than a `decision`, because before the seal a crash
        would leave a durable "applied" for a boundary that never happened
        and after it records after a seal are forbidden. And **a committed
        seal's exact retry is answered ahead of the baseline gate**: the
        generic v3 parser rejects a foreign `baseline_id` before it reads
        `request_id`, and a retry of the boundary that closed C1
        necessarily carries B1 while C2 answers under B2 -- so without a
        dedicated route the promised exact-retry answer is unreachable
        (PR-30a, PR-30e).

        The CLI stages C2 first and names the staged bytes by
        `stage_digest`; the engine validates exactly those bytes, performs
        the cutoff in its single-writer loop, and then exits with code 3
        ("sealed; period N+1 is ready to open"). It does NOT load C2 into
        itself: a transition is a restart, not a reload (DL-65)."""
        try:
            parsed = SealRequest(
                baseline_id=str(request.get("baseline_id")),
                epoch=int(request["epoch"]),
                request_id=str(request.get("request_id")),
                next_period=StagedNextPeriod.model_validate(request.get("next_period")),
                stage_digest=str(request.get("stage_digest")),
                force_seal=bool(request.get("force_seal", False)),
                claimed_actor=str(request.get("claimed_actor") or ""),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            return {"ok": False, "error": f"malformed seal request: {exc}", "refused": True}
        answered = self._committed_seal(parsed)
        if answered is not None:
            return answered
        try:
            parse_envelope(request, addressed=None, baseline_id=self.engine.baseline_id)
        except EnvelopeError as exc:
            return {"ok": False, "error": str(exc), "refused": True}
        try:
            committed = await asyncio.wait_for(
                self.engine.submit_seal(parsed), timeout=self.SEAL_TIMEOUT_S
            )
        except AdmissionRefused as exc:
            return {"ok": False, "error": str(exc), "refused": True}
        except EngineError as exc:
            # a readiness or phase-2 refusal: C1 is still open and correct,
            # and the cutoff work already admitted stays as legitimate C1
            # activity (ss7's exit codes)
            return {"ok": False, "error": str(exc), "refused": True}
        except TimeoutError:
            return {
                "ok": False,
                "error": f"no boundary outcome within {self.SEAL_TIMEOUT_S}s: the seal may"
                " still commit -- re-read before retrying, and retry only under"
                f" request_id {parsed.request_id}",
            }
        return _seal_answer(committed.record)

    def _committed_seal(self, parsed: SealRequest) -> dict[str, Any] | None:
        """ss2.2's retry route: the engine of period N+1 keeps the `seal`
        record it opened from and answers an exact retry from it.

        The lookup reaches exactly ONE seal back. A retry of an older seal
        is refused as a stale baseline, which is a liveness loss and not a
        safety one (PR-30e). An uncommitted seal request is UNSEEN: it left
        nothing behind, its retry is a fresh request that attempts the
        boundary again, and only a committed seal is ever deduplicated."""
        estate = self.engine.estate
        record = estate.prior_seal_record if estate is not None else None
        if record is None or record.get("request_id") != parsed.request_id:
            return None
        if record.get("request_fingerprint") != parsed.fingerprint:
            return {
                "ok": False,
                "refused": True,
                "error": f"request_id {parsed.request_id} named the boundary that closed"
                f" period {record.get('period_id')} under a different envelope: force is an"
                " authorization and the actor is attribution, and neither may be swapped"
                " under a retry (period-model ss2.2, PR-30c)",
            }
        return _seal_answer(record)

    async def _decision(self, submitted: Awaitable[ApplyResult], *, kind: str) -> dict[str, Any]:
        """Wait out one submitted mutation and shape its answer -- the half
        of a mutation that is identical whatever it mutates.

        Deliberately no `at`: v1 answered with the receipt, whose timestamp
        was the one this request had just been stamped with. A v2 answer is a
        DECISION, and a retry's answer is the ORIGINAL decision -- echoing
        the retry's own stamp beside it would make two answers to one command
        differ in a field that is not about the command. `index` is the
        handle; the leader timestamp lives in the log next to it."""
        try:
            result = await asyncio.wait_for(submitted, timeout=self.DECISION_TIMEOUT_S)
        except AdmissionRefused as exc:
            return {"ok": False, "error": str(exc), "refused": True}
        except TimeoutError:
            # not a decision and not a refusal: we do not know. Saying so is
            # the only honest answer -- the input may be durably admitted and
            # about to apply, so a client must re-read rather than assume.
            # This is the ONE ok:false a mutation can be answered with that
            # does not carry `refused`, which is what lets `outcome_of` read
            # the absence as uncertainty rather than as a fourth kind of no.
            return {
                "ok": False,
                "error": f"no decision within {self.DECISION_TIMEOUT_S}s: the engine loop is"
                " not draining. The command may still be admitted -- re-read before retrying.",
            }
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
        return answer

    def _host_command_for(self, request: dict[str, Any]) -> HostCommand | dict[str, Any]:
        """The verb half of framing for `host`, or the refusal it earns --
        `_event_for`'s sibling, and refusing for the same reasons: a shape
        the protocol does not define never becomes an admitted input.

        Note what is NOT checked here: whether the host exists, and whether
        an eviction may proceed. Both read mutable state, so both are the
        engine's to decide inside the input's batch (ss8)."""
        verb = request.get("verb")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return {"ok": False, "error": f"payload must be an object, got {payload!r}"}
        if verb not in HOST_VERBS:
            return {
                "ok": False,
                "error": f"unknown host verb {verb!r} (one of {sorted(HOST_VERBS)})",
            }
        host_id = payload.get("id")
        if not isinstance(host_id, str) or not host_id:
            return {"ok": False, "error": "a host verb addresses a host by id"}
        if not is_scalar_string(host_id):
            return {"ok": False, "error": "host id carries an unpaired surrogate"}  # PR-10a
        force = payload.get("force", False)
        if not isinstance(force, bool):
            return {"ok": False, "error": f"force must be a boolean, got {force!r}"}
        return HostCommand(verb=verb, host_id=host_id, force=force)

    def _hosts(self, request: dict[str, Any]) -> dict[str, Any]:
        """The ss8 routing table, and the read a `host` command's `expect` is
        composed from (concurrency-model ss6).

        With no `ids` it answers the WHOLE table, unlike `globals`: a routing
        table is a small, enumerable inventory that ss7's takeover barrier
        has to walk in full, so "everything" is a meaningful answer here in a
        way it is not for globals. Named `ids` are answered whether or not
        they exist, at revision 0 when they do not -- absence you cannot name
        is absence you cannot lock against."""
        table = self.engine.oracle.store.hosts
        ids = request.get("ids")
        if ids is None:
            names = sorted(table)
        elif isinstance(ids, list) and all(isinstance(name, str) for name in ids):
            names = list(ids)
        else:
            return {"ok": False, "error": "ids must be a list of host id strings"}
        return {
            "ok": True,
            "executor": self.engine.executor_id,
            "hosts": {name: self._host_row(table.get(name)) for name in names},
        }

    @staticmethod
    def _host_row(row: HostRuntime | None) -> dict[str, Any]:
        if row is None:
            return {"present": False, "state_rev": 0}
        return {
            "present": True,
            "state": row.state,
            "generation": row.generation,
            "deadman_s": row.deadman_s,
            "last_contact": row.last_contact.isoformat() if row.last_contact else None,
            # non-null is the ss8 incident marker: this host's work was
            # rerouted without proof its executor was dead
            "forced_by": row.forced_by,
            "state_rev": row.state_rev,
        }

    def _event_for(self, request: dict[str, Any]) -> Event | dict[str, Any]:
        """The verb half of framing: the Event a well-formed request names,
        or the refusal it earns. Job arguments are catalog-checked here --
        vendor sendevent errors on an unknown job rather than queueing it."""
        verb = request.get("verb")
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return {"ok": False, "error": f"payload must be an object, got {payload!r}"}
        at = self.engine.clock.now()
        if verb in JOB_EVENT_VERBS:
            job = payload.get("job")
            if (error := self._check_job(job)) is not None:
                return error
            ev = Event(at=at, kind=verb, payload={"job": job})
        elif verb == "SET_GLOBAL":
            name, value = payload.get("name"), payload.get("value")
            if not (isinstance(name, str) and name):
                return {"ok": False, "error": "SET_GLOBAL requires a global name"}
            if not isinstance(value, str):
                return {"ok": False, "error": "SET_GLOBAL requires a string value"}
            ev = Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": value})
        elif verb == "CHANGE_STATUS":
            job, status = payload.get("job"), payload.get("status")
            if (error := self._check_job(job)) is not None:
                # SEM-07 (DL-65 review): "JOB^INST" with INST a declared
                # insert_xinst is a legal CHANGE_STATUS target even though
                # no such job is in the catalog -- overwriting the store's
                # pseudo-entity is exactly how an operator satisfies a
                # cross-instance atom the sandbox cannot see (the oracle
                # creates the store row on demand; only this gate stood in
                # the way). Other job verbs stay catalog-only: starting or
                # killing a ghost is meaningless.
                name, sep, inst = job.rpartition("^") if isinstance(job, str) else ("", "", "")
                if not (sep and name and inst in self.engine.oracle.catalog.external_instances):
                    return error
            if status not in STATUSES:
                return {
                    "ok": False,
                    "error": f"unknown status {status!r} (one of {sorted(STATUSES)})",
                }
            status_payload: dict[str, object] = {"job": job, "status": status}
            if "exit_code" in payload:
                if not isinstance(payload["exit_code"], int):
                    return {"ok": False, "error": "exit_code must be an integer"}
                status_payload["exit_code"] = payload["exit_code"]
            ev = Event(at=at, kind="STATUS", payload=status_payload)
        else:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        if not is_scalar_json(ev.payload):
            # PR-10a: a lone surrogate is a legal Python str and a legal JSON
            # escape, and canonicalization raises on one -- a single admitted
            # SET_GLOBAL value would leave the estate unsealable, so the door
            # refuses it and nothing is written (period-model ss3.2).
            return {"ok": False, "error": "payload carries an unpaired surrogate"}
        return ev

    def _spec_drift(self) -> bool | None:
        """None = no fingerprint to check against (embedders). Lazy re-read
        at most every DRIFT_CHECK_INTERVAL_S; estate files are small, so the
        synchronous reads inside a status query are acceptable by design."""
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
                return {"ok": False, "error": f"unknown job {job!r}"}
            names = [job]
        else:
            names = sorted(set(catalog.jobs) | set(store.job))
        # ss11 additions (DL-46): pending timers from the oracle's own
        # liveness rules, and the ss6 log paths the wrapper appends to --
        # the TUI's jobs table and log tail read these, never a re-derivation
        pending: dict[str, list[dict[str, str]]] = {}
        for due, timer_job, kind in self.engine.oracle.pending_timers():
            pending.setdefault(timer_job, []).append({"due": due.isoformat(), "kind": kind})
        held = self.engine.held_jobs()
        jobs: dict[str, dict[str, Any]] = {}
        for name in names:
            rt = store.job.get(name) or JobRuntime()  # never insert from a query
            log_out = log_err = None
            job_ir = catalog.jobs.get(name)
            if self.engine.run_root is not None and job_ir is not None and job_ir.job_type == "CMD":
                if rt.run_number >= 1:
                    log_out, log_err = job_log_paths(job_ir, rt.run_number, self.engine.run_root)
                elif isinstance(job_ir.exec_, ExecSpec):
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
                # DL-54 (DL-46's display-truth rule): an armed job looks
                # INACTIVE but the next condition edge starts it -- operators
                # must see the latch, not tail the trace for SCHED_ARM
                "armed": rt.armed,
                # DL-68: the trigger of the most recent start, trace-cause
                # verbatim (null = never started)
                "started_by": rt.started_by,
                # DL-87: the revision an `expect` precondition names for this
                # entity. Published on every read because a client that acts on
                # what it saw must be able to say WHAT it saw.
                "state_rev": rt.state_rev,
                # DL-94: the oracle started it and its executor routes no new
                # effects, so no process was launched (concurrency-model ss8).
                # Derived, never stored. Published because a drained estate
                # whose jobs sit in STARTING with no explanation is a silent
                # hang, and the drain is a maintenance operation an operator
                # has to be able to watch
                "held": name in held,
                "pending_timers": pending.get(name, []),
                "log_out": log_out,
                "log_err": log_err,
                # DL-65: catalog placement, so the ss11 table can render the
                # box hierarchy without a second query (null for store-only
                # ghosts a CHANGE_STATUS invented)
                "job_type": job_ir.job_type if job_ir is not None else None,
                "box_name": job_ir.box.box_name if job_ir is not None else None,
            }
            if job_ir is not None:
                watching = self._fw_watching(job_ir)
                if watching is not None:
                    # DL-68: present ONLY for a live FW run -- absence of the
                    # key is itself the "not watching" signal
                    jobs[name]["watching"] = watching
        return {"ok": True, "jobs": jobs, "spec_drift": self._spec_drift()}

    def _globals(self, names: list[Any]) -> dict[str, Any]:
        """DL-87 (concurrency-model ss6): answer NAMED globals with
        `{present, value, state_rev}`, inserting nothing.

        An unset name is answered rather than omitted, and at revision 0 --
        which is exactly what a conditional create conditions on, since the
        catalog seed is an input and so anything that exists is at 1 or more.
        Absence you cannot name is absence you cannot lock against."""
        store = self.engine.oracle.store
        answers: dict[str, Any] = {}
        for name in names:
            if not isinstance(name, str):
                return {"ok": False, "error": f"global name must be a string, got {name!r}"}
            row = store.globals_.get(name)
            answers[name] = {
                "present": row is not None,
                "value": None if row is None else row.value,
                "state_rev": 0 if row is None else row.state_rev,
            }
        return {"ok": True, "globals": answers}

    def _trace(self, request: dict[str, Any]) -> dict[str, Any]:
        since = request.get("since", 0)
        if not isinstance(since, int):
            return {"ok": False, "error": "since must be an integer trace seq"}
        entries = self.engine.oracle.trace()
        return {
            "ok": True,
            "last_seq": len(entries),
            "entries": [
                {
                    "seq": seq,
                    "at": entry.at.isoformat(),
                    "job": entry.job,
                    "transition": entry.transition,
                    "cause": entry.cause,
                }
                for seq, entry in enumerate(entries, start=1)
                if seq > since
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
        # oracle._cond_true is package-private on purpose: explain must use
        # the ORACLE's truth (ice bypass, lookback, instances), never a copy
        atoms: list[dict[str, Any]] = []
        for atom in iter_atoms(cond):
            entry: dict[str, Any] = {
                "atom": cond_to_source(atom),
                "true": oracle._cond_true(atom, job),
            }
            if isinstance(atom, GlobalAtom):
                # DL-66 (review): atom truth alone hides WHY -- serve the
                # effective global value (null = never set); there is no
                # standalone show-globals verb, explain is the read path
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
        """DL-65 (the list-dependencies analog). upstream/globals = what this
        job's condition references, split by ATOM TYPE -- never by sniffing
        the oracle's key strings, where a job legally named 'g:x' (DL-39
        escapes) collides with global x. downstream = jobs whose conditions
        reference this one, from the oracle's edge-trigger index (same
        package-private stance as _explain; the index inherits that key
        collision for pathological names, documented not guarded). Condition
        edges are NOT the whole blast radius of a box, so containment is
        served alongside: box_name (upward) and members (downward) -- a
        KILLJOB/ON_HOLD on a box reaches every member with no condition
        edge in sight."""
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
                elif atom.job.instance is None:
                    upstream.add(atom.job.name)
                else:
                    upstream.add(f"{atom.job.name}^{atom.job.instance}")
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
        """{file, interval, min_size} for a live FW run (DL-68). A watch is
        only an in-flight adapter task -- no registry, no status field -- so
        the facts come from the spec; interval resolves through the FW
        adapter's default so the number shown is the one the poll sleeps."""
        if not isinstance(job_ir.exec_, FwSpec) or job_ir.name not in self.engine.live_jobs():
            return None
        spec = job_ir.exec_
        default = getattr(self.engine.adapters.get("FW"), "default_interval_s", 60)
        return {
            "file": spec.watch_file,
            "interval": spec.watch_interval or default,
            "min_size": spec.watch_file_min_size,
        }

    def _timers(self) -> dict[str, Any]:
        """DL-65 (the list-timers analog): every pending oracle timer plus
        each scheduled job's next calendar tick, one due-ordered list --
        'what happens next' across the estate. DL-68: live filewatches join
        as due-less rows after the dated ones (they fire on a file, not a
        clock, but they ARE a pending trigger an operator must see)."""
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
            if job_ir is None:
                continue
            watching = self._fw_watching(job_ir)
            if watching is None:
                continue
            # no "watching" prefix: the row's kind column already says
            # filewatch, and the path is the information (review MINOR)
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
            return {
                "ok": False,
                "error": "plan disabled: cycle in the AND-success skeleton"
                f" ({' -> '.join(exc.args[1])})",
            }
        waves: list[list[str]] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready())
            waves.append(ready)
            sorter.done(*ready)
        return {"ok": True, "waves": waves}

    async def _subscribe(self, writer: asyncio.StreamWriter, request: dict[str, Any]) -> None:
        """Stream journal records: optional backfill from `since` (an input/
        advance seq; the cut is positional -- everything after the last
        record at or below it), then live. seq'd records are exactly-once
        across the backfill/live seam; unsequenced dispatch/drop/decision
        records in the race window are at-least-once (runner.py module
        docstring). The seam keys on `seq` and never on a record name, so
        DL-118's `decision` inherited `result`'s guarantee unchanged.

        The backfill spans SEGMENTS since DL-135. A cursor taken before a
        boundary names an index in an earlier segment, and reading only
        the active one answered such a subscriber with the records after
        the boundary and no sign that anything came before them. I2 makes
        the index estate-wide, so the cursor still means one thing across
        the whole lineage and only the reader had to widen. What this root
        no longer retains cannot be sent, and that case is the GAP MARKER
        (period-model ss11) rather than a short stream."""
        lost = self._lineage_lost()  # a subscription response is a read too (PR-03)
        if lost is not None:
            await self._send(writer, lost)
            return
        journal = self.engine.journal
        if journal is None:
            await self._send(writer, {"ok": False, "error": "this run has no journal"})
            return
        since = request.get("since")
        if since is not None and not isinstance(since, int):
            await self._send(writer, {"ok": False, "error": "since must be an integer seq"})
            return
        queue = journal.subscribe()
        try:
            # sample the seam BEFORE the ack yields: a record written during
            # the send takes the next index and would be skipped as "covered"
            # despite never being backfilled (DL-45). The seam is the
            # admission frontier now (concurrency-model ss2) -- the journal
            # stopped allocating the number when the frontier started to
            max_seq = since if since is not None else self.engine.frontiers.committed_index
            await self._send(writer, {"ok": True, "subscribed": True})
            if since is not None:
                try:
                    backfill = read_backfill(journal.path, since=since)
                except EngineError as exc:
                    # the read now spans segments, so it can meet a file
                    # this one did not write. The ack has gone, so the
                    # refusal goes on the STREAM -- a handler that raised
                    # here would hang the client up with no answer at all.
                    # PR-03 holds for THIS response too: the read yielded,
                    # and a displaced leader must answer the refusal a
                    # displaced leader owes, not narrate a lineage it no
                    # longer leads
                    lost = self._lineage_lost()
                    await self._send(writer, lost or {"ok": False, "error": str(exc)})
                    return
                records = backfill.records
                if backfill.gap_from is not None:
                    # ss11: a cursor below the earliest retained record is
                    # told so, explicitly. Silence would read as "nothing
                    # happened between your cursor and the first line you
                    # got", which is the one thing that is not true. The
                    # marker is a RESPONSE, so PR-03's fence runs in front
                    # of it like every other one
                    lost = self._lineage_lost()
                    if lost is not None:
                        await self._send(writer, lost)
                        return
                    await self._send(writer, {"gap": True, "earliest_retained": backfill.gap_from})
                cut = 0
                for index, record in enumerate(records):
                    seq = record.get("seq")
                    if isinstance(seq, int) and seq <= since:
                        cut = index + 1
                for record in records[cut:]:
                    lost = self._lineage_lost()
                    if lost is not None:
                        # PR-03 holds per RESPONSE, not per connection: the
                        # accept-time check proves nothing about a lineage
                        # replaced mid-stream, and a displaced leader that
                        # kept backfilling would publish records for an
                        # estate it no longer leads
                        await self._send(writer, lost)
                        return
                    seq = record.get("seq")
                    if isinstance(seq, int):
                        max_seq = max(max_seq, seq)
                    await self._send(writer, record)
            while True:
                record = await queue.get()
                lost = self._lineage_lost()
                if lost is not None:
                    await self._send(writer, lost)  # same PR-03 rule, live seam
                    return
                seq = record.get("seq")
                if isinstance(seq, int):
                    if seq <= max_seq:
                        continue  # already delivered by the backfill
                    max_seq = seq
                await self._send(writer, record)
        finally:
            journal.unsubscribe(queue)


# ---------------------------------------------------------------- clients (ss10)


def _seal_answer(record: Mapping[str, Any]) -> dict[str, Any]:
    """ss2.2's answer shape, built from the `seal` record and nothing else
    -- so a fresh commit and an exact retry answer identically, which is
    the whole point of the retry route."""
    return {
        "ok": True,
        "kind": "seal",
        "decision": "applied",
        "period_id": record["period_id"],
        "digest": record["digest"],
        "next_period_id": record["next_period_id"],
        "next_baseline_id": record["next_baseline_id"],
        "request_id": record["request_id"],
    }


class ControlClientError(RuntimeError):
    """The control socket is unreachable or hung up mid-exchange.

    `delivered` says which of those two it was, and it is the difference
    between the operator's two next moves (ss3, and `outcome_of`'s reading
    of them). A request that never left this process changed nothing
    anywhere: no index was taken, the log says nothing about it, and it is
    safe to send again UNCHANGED. A request that left and got no answer
    back promises neither -- the engine fsyncs the attempt before it feeds
    it, so a connection that died after the write may well have died over a
    command that is already durably admitted. That is `unknown`, and its
    only safe retry is under the same `request_id`.

    Defaulted to False because the constructor is called from both clients
    and the safe default is the one that claims less: a caller that reads
    `delivered` and gets False on a delivered failure would tell an
    operator to resend, which is how a command applies twice."""

    def __init__(self, *args: object, delivered: bool = False) -> None:
        super().__init__(*args)
        self.delivered = delivered


def versioned(request: dict[str, Any]) -> dict[str, Any]:
    """Stamp `v` unless the caller already did. The clients are part of this
    protocol's implementation, not callers of it, so making every query site
    repeat the version would be noise -- but a caller that names one keeps
    it, which is what makes the server's refusal testable from here. Public
    because the CLI's raw subscribe socket is a client too: it sent no `v`
    from v2 to v3 and the server's refusal left it hanging on an open
    connection, which is what one stamp site prevents."""
    return request if "v" in request else {**request, "v": PROTOCOL_VERSION}


# ------------------------------------------------ composing a command (ss6)
#
# The client half of the protocol lives here for the reason the server half
# does (DL-78): the wire vocabulary gets exactly one definition. S3 gave a
# mutation a shape a caller has to BUILD -- read the addressed entity, name
# its revision -- and that rule was briefly written once in the CLI, once in
# the TUI and twice in the tests. Four copies of "which query answers a
# `global:` key, and where the revision sits in its answer" is four places
# to fix when the answer's shape moves.
#
# The round trip itself stays at the call site: `ControlClient` and
# `roundtrip` are two transports for one protocol, and these functions are
# what both of them send and read.


def read_for(key: str) -> dict[str, Any]:
    """The query that answers the revision of an ss6-namespaced `key`."""
    namespace, _, name = key.partition(":")
    if namespace == "global":
        return {"cmd": "global", "name": name}
    if namespace == "host":
        return {"cmd": "hosts", "ids": [name]}
    return {"cmd": "status", "job": name}


def revision_in(response: Mapping[str, Any], key: str) -> int:
    """The revision `key` sits at, read out of the answer to `read_for(key)`.

    A REFUSED read answers 0. `status` refuses a job it has neither a catalog
    entry nor a row for, which is the right answer to a typo and the wrong
    one here: an entity with no row is at revision 0 -- exactly what a SEM-07
    CHANGE_STATUS on a not-yet-invented "JOB^INST" has to name. The typo is
    still caught, by the catalog check on the command itself."""
    namespace, _, name = key.partition(":")
    if not response.get("ok"):
        return 0
    if namespace == "global":
        return int(response["globals"][name]["state_rev"])
    if namespace == "host":
        return int(response["hosts"][name]["state_rev"])
    return int(response["jobs"][name]["state_rev"])


#: The four things a v2 command answer can mean -- not two. `ok: False`
#: covers three outcomes that call for three different next moves by the
#: caller, and a client that cannot tell them apart has to guess at exactly
#: the moment guessing is most expensive (DL-92).
APPLIED, REFUSED, REJECTED, UNKNOWN = "applied", "refused", "rejected", "unknown"


def outcome_of(response: Mapping[str, Any]) -> str:
    """Classify one `sendevent` answer (control-protocol ss3, ss6).

    ``applied``   the oracle applied it; `revisions` says what moved.
    ``refused``   nothing was admitted, no index consumed, and the log says
                  NOTHING about it (DL-90). Safe to fix and send again --
                  and safe to send again *unchanged*, since it never
                  happened once.
    ``rejected``  a DECISION went against it. It took an index and its
                  batch's time half fired, so it is in the log and the
                  world moved underneath it. Re-READ and re-decide;
                  resending the same envelope loses the same race, because
                  `expect` is part of it.
    ``unknown``   no decision arrived within the server's window. This is
                  not a failure and must not be treated as one: the command
                  may be durably admitted and about to apply. Re-read, and
                  if it must be retried, retry under the SAME `request_id`
                  -- the one path that cannot apply it twice.

    The rule is here rather than at the two call sites because it is a
    reading of the protocol, not of a screen: the CLI turns it into an exit
    code and the TUI into a sentence, and those may differ. What the answer
    MEANS may not."""
    if response.get("ok"):
        return APPLIED
    if response.get("refused"):
        return REFUSED
    if response.get("decision") == REJECTED:
        return REJECTED
    return UNKNOWN


def command(
    verb: str,
    payload: Mapping[str, Any],
    *,
    key: str,
    revision: int,
    baseline_id: str,
    epoch: int,
    request_id: str | None = None,
    claimed_actor: str | None = None,
    cmd: str = "sendevent",
) -> dict[str, Any]:
    """One ss6 command envelope, complete. `request_id` defaults to a fresh
    uuid4 -- a caller that wants to RETRY passes the original's.

    `cmd` selects which verb set the request addresses -- `sendevent` for the
    oracle's, `host` for the ss8 routing table's. One composer for both,
    because the envelope IS the same envelope: ss0's mandate is on
    externally requested mutations, not on a particular vocabulary."""
    request: dict[str, Any] = {
        "cmd": cmd,
        "baseline_id": baseline_id,
        "epoch": epoch,
        "request_id": request_id or str(uuid.uuid4()),
        "verb": verb,
        "payload": dict(payload),
        "expect": {key: revision},
    }
    if claimed_actor is not None:
        request["claimed_actor"] = claimed_actor
    return request


def claimed_actor() -> str:
    """What this process says it is (concurrency-model ss6). A CLAIM: the
    control socket has no authentication (control-protocol ss7 gap 2), so
    this is a breadcrumb in the log, never an authorization."""
    try:
        user = getpass.getuser()
    except Exception:  # no passwd entry (containers): the claim is still useful
        user = f"uid{os.getuid()}"
    return f"{user}@{socket_mod.gethostname()}"


class ControlClient:
    """JSON-lines client of the ss10 control socket. One persistent
    request/response connection, serialized by a lock; subscribe() opens its
    OWN connection because a subscription owns its connection until hangup
    (ss10). Any transport error drops the connection so the next request
    reconnects -- the client outlives engine restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = versioned(payload)
        async with self._lock:
            #: whether this request reached the socket. Everything after the
            #: drain is `delivered`, including the reads that fail: see
            #: ControlClientError.
            sent = False
            try:
                if self._writer is None:
                    # limit: one `status` response line covers every job and
                    # overruns asyncio's 64 KiB default at ~300 jobs
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        str(self.path), limit=LINE_LIMIT
                    )
                assert self._reader is not None
                self._writer.write(json.dumps(payload).encode("utf-8") + b"\n")
                await self._writer.drain()
                sent = True
                line = await self._reader.readline()
            except OSError as exc:
                await self._drop()
                raise ControlClientError(str(exc), delivered=sent) from exc
            except BaseException:
                # a CANCELLED exchange (an exclusive worker superseded
                # mid-request) leaves the response unread on the stream;
                # reusing the connection would hand that stale line to the
                # NEXT request and offset every reply after it (DL-46)
                # -- drop the connection, reconnect lazily
                await self._drop()
                raise
            if not line:
                await self._drop()
                raise ControlClientError("engine hung up", delivered=True)
            try:
                response = json.loads(line)
            except ValueError as exc:
                await self._drop()
                raise ControlClientError(f"bad response line: {exc}", delivered=True) from exc
            if not isinstance(response, dict):
                await self._drop()
                raise ControlClientError("response is not a JSON object", delivered=True)
            return response

    async def subscribe(self, since: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield journal records until the engine hangs up. Raises
        ControlClientError if the connection fails or the engine refuses
        (e.g. a journal-less run)."""
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path), limit=LINE_LIMIT)
        except OSError as exc:
            raise ControlClientError(str(exc)) from exc
        try:
            request: dict[str, Any] = {"cmd": "subscribe", "v": PROTOCOL_VERSION}
            if since is not None:
                request["since"] = since
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            ack_line = await reader.readline()
            if not ack_line:
                raise ControlClientError("engine hung up before the subscribe ack")
            ack = json.loads(ack_line)
            if not ack.get("ok"):
                raise ControlClientError(str(ack.get("error", "subscribe refused")))
            while True:
                line = await reader.readline()
                if not line:
                    return  # engine gone; the caller decides whether to retry
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # torn record: it is only a wake-up signal anyway
                yield record
        except OSError as exc:
            raise ControlClientError(str(exc)) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _drop(self) -> None:
        # detach BEFORE the awaits: _drop runs on cancellation paths, and a
        # re-delivered CancelledError mid-close must not leave a half-dead
        # connection looking attached
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def roundtrip(
    socket_path: Path, request: dict[str, Any], *, timeout: float = 10.0
) -> dict[str, Any]:
    """One-shot blocking request/response, for callers with no event loop
    (the typer `sendevent`/`query` verbs). Raises ControlClientError on any
    transport or decode failure -- exit-code mapping is the CLI's job, so
    this stays free of typer (DL-78).

    Split in two on purpose: everything up to and including the write is
    UNDELIVERED and everything after it is DELIVERED, because that boundary
    is what the failure means rather than where it happened."""
    conn = socket_mod.socket(socket_mod.AF_UNIX)
    try:
        try:
            conn.settimeout(timeout)
            conn.connect(str(socket_path))
            conn.sendall(json.dumps(versioned(request)).encode("utf-8") + b"\n")
        except OSError as exc:
            raise ControlClientError(f"control socket {socket_path}: {exc}") from exc
        try:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            response = json.loads(buf)
        except (OSError, ValueError) as exc:
            raise ControlClientError(
                f"control socket {socket_path}: {exc}", delivered=True
            ) from exc
    finally:
        conn.close()
    if not isinstance(response, dict):
        raise ControlClientError(
            f"control socket {socket_path}: response is not a JSON object", delivered=True
        )
    return response
