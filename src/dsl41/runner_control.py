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
  injected source=control (journaled by the take_event path like every
  input; the engine's single-writer loop serializes them -- deliberately
  no controller lease here, DL-41a). Queries (status/trace/explain/plan)
  read the oracle store between feeds -- safe because feed() never yields.
  subscribe streams journal records live (at-least-once for unsequenced
  dispatch/drop records during the backfill race; seq'd records exactly
  once). A stale socket file from a crashed run is detected by a probe
  connect and unlinked; a LIVE socket refuses the second engine.

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
"""

from __future__ import annotations

import asyncio
import contextlib
import graphlib
import hashlib
import json
import os
import socket as socket_mod
import time

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, get_args

from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.ir import ExecSpec, FwSpec, JobIR
from dsl41.oracle import Event, EventKind, JobRuntime, JobStatus
from dsl41.runner import Engine
from dsl41.runner_adapters import LINE_LIMIT, job_log_paths
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import read_journal
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


class ControlServer:
    """ss10 control plane: a unix domain socket in the run directory, mode
    0600, JSON lines both ways. One request object per line; one response
    object per line ({"ok": bool, ...}), except `subscribe`, which streams
    journal records until the client hangs up.

    Verbs: {"cmd": "sendevent", "event": <verb>, ...} for the sendevent
    parity set (job verbs carry "job"; SET_GLOBAL carries "name"/"value";
    CHANGE_STATUS carries "job"/"status" and optional int "exit_code" --
    injected as STATUS, keeping overwrite parity). Queries: status [job],
    trace [since], explain job, spec job, deps job, timers, plan,
    global name, globals names; and subscribe [since]. Job arguments
    are validated against the catalog -- vendor sendevent errors on unknown
    jobs rather than queueing them.

    Revision-bearing reads (DL-87, concurrency-model ss6): `status` carries
    each job's `state_rev`, and `global` / `globals` answer
    `{present, value, state_rev}` for NAMED globals and insert nothing. The
    naming is the point -- a map of the globals that exist cannot express the
    absence a conditional create has to condition on, so an unset name is
    answered `{present: false, state_rev: 0}` rather than omitted.

    Injections go through Engine.inject (source=control), so the WAL
    journals every control input at feed time (ss10: the WAL is the audit
    trail; there is no second log) and the single-writer loop serializes
    them -- deliberately no controller lease at this tier (DL-41a).
    Queries read the oracle store directly: feed() never yields, so a
    handler task can never observe a half-applied event."""

    #: seconds between on-disk re-checks of the estate fingerprint (the
    #: drift hint below); reads happen lazily inside a status query
    DRIFT_CHECK_INTERVAL_S = 15.0

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
                    await self._send(writer, {"ok": False, "error": f"bad request: {exc}"})
                    continue
                if request.get("cmd") == "subscribe":
                    await self._subscribe(writer, request)
                    break  # a subscription owns its connection until hangup
                try:
                    response = self._respond(request)
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

    def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "sendevent":
            return self._sendevent(request)
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
        return {"ok": False, "error": f"unknown cmd {cmd!r}"}

    def _check_job(self, job: object) -> dict[str, Any] | None:
        if isinstance(job, str) and job in self.engine.oracle.catalog.jobs:
            return None
        return {"ok": False, "error": f"unknown job {job!r}"}

    def _sendevent(self, request: dict[str, Any]) -> dict[str, Any]:
        verb = request.get("event")
        at = self.engine.clock.now()
        if verb in JOB_EVENT_VERBS:
            job = request.get("job")
            if (error := self._check_job(job)) is not None:
                return error
            ev = Event(at=at, kind=verb, payload={"job": job})
        elif verb == "SET_GLOBAL":
            name, value = request.get("name"), request.get("value")
            if not (isinstance(name, str) and name):
                return {"ok": False, "error": "SET_GLOBAL requires a global name"}
            if not isinstance(value, str):
                return {"ok": False, "error": "SET_GLOBAL requires a string value"}
            ev = Event(at=at, kind="SET_GLOBAL", payload={"name": name, "value": value})
        elif verb == "CHANGE_STATUS":
            job, status = request.get("job"), request.get("status")
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
            payload: dict[str, object] = {"job": job, "status": status}
            if "exit_code" in request:
                if not isinstance(request["exit_code"], int):
                    return {"ok": False, "error": "exit_code must be an integer"}
                payload["exit_code"] = request["exit_code"]
            ev = Event(at=at, kind="STATUS", payload=payload)
        else:
            return {"ok": False, "error": f"unknown event {verb!r}"}
        self.engine.inject(ev, source="control")
        return {"ok": True, "kind": ev.kind, "at": at.isoformat()}

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
        across the backfill/live seam; unsequenced dispatch/drop records in
        the race window are at-least-once (runner.py module docstring)."""
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
            # the send bumps journal.seq and would be skipped as "covered"
            # despite never being backfilled (DL-45)
            max_seq = since if since is not None else journal.seq
            await self._send(writer, {"ok": True, "subscribed": True})
            if since is not None:
                records = read_journal(journal.path)
                cut = 0
                for index, record in enumerate(records):
                    seq = record.get("seq")
                    if isinstance(seq, int) and seq <= since:
                        cut = index + 1
                for record in records[cut:]:
                    seq = record.get("seq")
                    if isinstance(seq, int):
                        max_seq = max(max_seq, seq)
                    await self._send(writer, record)
            while True:
                record = await queue.get()
                seq = record.get("seq")
                if isinstance(seq, int):
                    if seq <= max_seq:
                        continue  # already delivered by the backfill
                    max_seq = seq
                await self._send(writer, record)
        finally:
            journal.unsubscribe(queue)


# ---------------------------------------------------------------- clients (ss10)


class ControlClientError(RuntimeError):
    """The control socket is unreachable or hung up mid-exchange."""


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
        async with self._lock:
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
                line = await self._reader.readline()
            except OSError as exc:
                await self._drop()
                raise ControlClientError(str(exc)) from exc
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
                raise ControlClientError("engine hung up")
            try:
                response = json.loads(line)
            except ValueError as exc:
                await self._drop()
                raise ControlClientError(f"bad response line: {exc}") from exc
            if not isinstance(response, dict):
                await self._drop()
                raise ControlClientError("response is not a JSON object")
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
            request: dict[str, Any] = {"cmd": "subscribe"}
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
    this stays free of typer (DL-78)."""
    try:
        conn = socket_mod.socket(socket_mod.AF_UNIX)
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        conn.close()
        response = json.loads(buf)
    except (OSError, ValueError) as exc:
        raise ControlClientError(f"control socket {socket_path}: {exc}") from exc
    if not isinstance(response, dict):
        raise ControlClientError(f"control socket {socket_path}: response is not a JSON object")
    return response
