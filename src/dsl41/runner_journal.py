"""Runner journal (ss7): the inputs-only write-ahead log and its replay.

Split out of runner.py by DL-74, with the paragraph it owns, verbatim.

Phase 11b (ss6-ss7; DL-41a/DL-42 pin the lifecycle semantics):

- Journal (ss7): inputs-only JSONL WAL, journal-first -- every injected
  event is WAL-appended (+fsync in the real domain) BEFORE feed(); emitted
  events and the trace replay from oracle determinism, never stored. The
  input alphabet has TWO halves (DL-44 amendment): external
  events (input records) and time observations (advance records, written
  before every Oracle.advance the engine performs) -- without the latter,
  an advance-fired term_run_time kill would vanish from replay and a late
  natural-exit record could resurrect the job at resume. Dispatch records
  are audit/ordering only (DL-41a): spawn.json, written by the process
  that spawned, is the authoritative spawn record, so dispatch carries
  wrapper_pid + run_dir rather than a pgid the engine never observes.

Stage S2 (concurrency-model ss4) made this log a two-record ledger without
changing what an input record means. An `input`/`advance` record IS the
`InputAttempt` -- one line, so the batch it carries cannot be torn in half
by a crash -- and it now names its `request_id` and `fingerprint`. A
`result` record carries the decision that attempt got, appended after it,
which is what makes replay two-pass: pass one indexes the decisions, pass
two applies. An attempt whose result never landed is applied, through the
same gate the live engine used (concurrency-model ss4, runner_admission).

Stage S6a (concurrency-model ss1/ss7) makes it a LEDGER rather than only a
log. A `leader` record allocates this incarnation's epoch by being
appended -- allocation and the log's account of it are one write, so they
cannot disagree -- under a lock on the run root that this object owns the
release of (runner_ledger). The header gains ss7's second pin, the
state-machine version, beside the catalog hash it already carried.

A journal written before S2 replays unchanged. Its attempts have no
results, so all of them apply -- exactly what the single-pass reader did,
and the reason no format gate was needed. One written before S6a has no
`leader` record either, so the first term over it is 1.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dsl41.ir import CatalogIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner_admission import (
    INERT_EPOCH,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Frontiers,
    apply_attempt,
    fingerprint,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_effects import Effect, EffectOutcome, Outbox
from dsl41.runner_hosts import HostCommand
from dsl41.runner_ledger import STATE_MACHINE_VERSION, LeaderLock

if TYPE_CHECKING:  # annotation only: the WAL stays a leaf of the DL-74 DAG
    from dsl41.runner_preflight import PreflightItem


def catalog_hash(catalog: CatalogIR) -> str:
    """Content hash gating resume (ss7): sha256 of the catalog's canonical
    JSON dump. Conservative by design -- an estate that changed in ANY way
    re-baselines explicitly rather than silently drifting semantics."""
    return hashlib.sha256(catalog.model_dump_json().encode("utf-8")).hexdigest()


def _dsl41_version() -> str:
    try:
        from importlib.metadata import version

        return version("dsl41")
    except Exception:  # not installed (editable src run): version is advisory
        return "0+unknown"


class Journal:
    """ss7 append-only JSONL WAL, one file per run. Inputs-only principle:
    emitted events and the trace are pure functions of the input sequence
    (oracle determinism), so only injected inputs are stored; `journal
    render` replays them through a fresh Oracle. Record kinds: header /
    input / advance / dispatch / drop / preflight (module docstring covers
    why dispatch is audit-only and why advances are inputs; preflight keeps
    the ss8 WARN caveats next to the run). fsync per record in the
    real domain (write-ahead: append + fsync BEFORE feed); buffered in
    rehearse, fsync on close. macOS caveat, accepted: os.fsync does not
    force the drive cache (F_FULLFSYNC would, at a large cost)."""

    def __init__(
        self,
        path: Path | str,
        *,
        fsync_each: bool,
        baseline_id: str = "",
        lock: LeaderLock | None = None,
    ) -> None:
        self.path = Path(path)
        self._f = self.path.open("ab")
        os.chmod(self.path, 0o600)  # owner-only: the WAL carries globals + every input
        self._fsync_each = fsync_each
        #: concurrency-model ss2: this log's identity, part of every
        #: fingerprint. Empty for a journal written before S2, and for the
        #: journal-less engines the bisimulation harness runs.
        self.baseline_id = baseline_id
        #: S6a: the run root's mutex, acquired before this file was opened
        #: and released when it is closed. The ledger is the log plus the
        #: lock that says who may append to it, so closing one closes both --
        #: an engine that dropped the file and kept the lock would exclude
        #: its own successor. None for the journal-less and lock-less
        #: engines the bisimulation harness runs.
        self._lock = lock
        #: live feeds for ss10 subscribe: every appended record is fanned out
        #: post-write; queues are unbounded (a slow subscriber buffers, the
        #: WAL never blocks on one)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    @classmethod
    def create(
        cls,
        path: Path | str,
        *,
        catalog: CatalogIR,
        clock_domain: str,
        started_at: datetime,
        lock: LeaderLock | None = None,
    ) -> Journal:
        baseline_id = str(uuid.uuid4())
        journal = cls(path, fsync_each=clock_domain == "real", baseline_id=baseline_id, lock=lock)
        journal._write(
            {
                "rec": "header",
                "baseline_id": baseline_id,
                "catalog_hash": catalog_hash(catalog),
                "dsl41_version": _dsl41_version(),
                # ss7's other pin, gated where catalog_hash is (S6a,
                # runner_ledger): what leader eligibility means is that these
                # two match, and a pin nothing reads is not a pin
                "state_machine_version": STATE_MACHINE_VERSION,
                "clock_domain": clock_domain,
                "started_at": started_at.isoformat(),
            }
        )
        return journal

    def leader(self, *, epoch: int, at: datetime) -> None:
        """One term of leadership over this run root (ss1's leader record,
        S6a). Appended under the lock, immediately after acquiring it, which
        is what makes the epoch MONOTONE: it is allocated by being written,
        so no two terms can read the same log and choose the same number.

        It is also what makes a failover reconstructible after the fact --
        every input between two of these records was admitted by the
        incarnation the earlier one names."""
        self._write(
            {
                "rec": "leader",
                "epoch": epoch,
                "at": at.isoformat(),
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "dsl41_version": _dsl41_version(),
            }
        )

    def admit(self, attempt: Attempt) -> None:
        """Append one admitted input -- the concurrency-model ss4 step-4
        batch -- as ONE line, so no crash can leave its time observation
        without its attempt or the reverse.

        The three record names are the three shapes of an attempt, not three
        paths: `input` has an oracle verb, `host` carries a routing-table
        command (S5a, concurrency-model ss8), `advance` is the standalone
        time observation (DL-44's other half of the input alphabet), and all
        three carry the same admission fields. `source` in {scheduler,
        adapter, control, reconcile} (ss7); None marks an unattributed
        script injection and never occurs in a real run. It is persisted
        because replay must re-derive the same causes and the same gate
        verdict from it (DL-68).

        S3 adds the rest of the ss6 envelope. `epoch` rides on every record
        because it is the leader's, not the caller's, and S6 fences on it.
        `expect` and `claimed_actor` ride only where they exist: an input
        the ENGINE raised has neither, and writing nulls for them would blur
        the one distinction the log has to keep -- which inputs were
        externally requested and therefore had to name a revision."""
        record: dict[str, Any] = {
            "rec": "input"
            if attempt.kind is not None
            else ("host" if attempt.host is not None else "advance"),
            "seq": attempt.index,
            "at": attempt.at.isoformat(),
            "request_id": attempt.request_id,
            "fingerprint": attempt.fingerprint,
            "epoch": attempt.epoch,
        }
        if attempt.kind is not None:
            record |= {"kind": attempt.kind, "payload": attempt.payload, "source": attempt.source}
        elif attempt.host is not None:
            # under its own key, not `payload`: two record shapes that spell
            # one field name two ways is how a reader learns to guess
            record |= {"host": attempt.host.wire(), "source": attempt.source}
        if attempt.expect is not None:
            record["expect"] = attempt.expect
        if attempt.claimed_actor is not None:
            record["claimed_actor"] = attempt.claimed_actor
        self._write(record)

    def result(self, result: ApplyResult) -> None:
        """Append the decision an admitted attempt got (ss4 step 7). Written
        unconditionally, including for an application that changed nothing:
        the absence of a result is how replay recognises the crash window,
        so an absence that also meant "nothing happened" would make that
        window unreadable.

        Its index rides under `index`, not `seq`: `seq` is the ss10
        subscribe cursor, and a result shares its attempt's number, so two
        records under one cursor value would leave the second undeliverable
        to a resuming subscriber."""
        self._write(
            {
                "rec": "result",
                "index": result.index,
                "request_id": result.request_id,
                "decision": result.decision,
                "reason": result.reason,
                "revisions": result.revisions,
            }
        )

    def effect(self, effect: Effect) -> None:
        """One intended effect, appended with the ss4 step-7 batch that
        decided it (concurrency-model ss1: the outbox lives IN the ledger, so
        the decision and what it implies cannot be torn apart by a crash).

        BEFORE the attempt, which is the whole content of an outbox: an
        engine that dies between deciding and acting leaves a record that it
        meant to act, and a recorded kill is re-driven at resume rather than
        lost with the task that would have delivered it."""
        self._write({"rec": "effect", **effect.model_dump(mode="json")})

    def effect_result(self, outcome: EffectOutcome) -> None:
        """What came of one attempt (concurrency-model ss5). Absent means
        `pending` -- the crash window -- which is exactly the distinction
        `indeterminate` exists to keep separate from it: nothing was tried
        vs something was tried and cannot be reported on."""
        self._write({"rec": "effect_result", **outcome.model_dump(mode="json")})

    def dispatch(
        self,
        job: str,
        run_number: int,
        *,
        wrapper_pid: int | None,
        run_dir: str | None,
        started_at: datetime,
    ) -> None:
        self._write(
            {
                "rec": "dispatch",
                "job": job,
                "run_number": run_number,
                "wrapper_pid": wrapper_pid,
                "run_dir": run_dir,
                "started_at": started_at.isoformat(),
            }
        )

    def drop(self, ev: Event, reason: str) -> None:
        self._write(
            {
                "rec": "drop",
                "at": ev.at.isoformat(),
                "kind": ev.kind,
                "payload": ev.payload,
                "reason": reason,
            }
        )

    def preflight(self, items: list[PreflightItem]) -> None:
        """ss8: WARN prints, JOURNALS, and runs -- the record keeps the run's
        stated caveats next to its inputs. Replay ignores it (not an input);
        read_journal carries it like any other record."""
        self._write(
            {
                "rec": "preflight",
                "items": [item.model_dump() for item in items],
            }
        )

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _write(self, record: dict[str, Any]) -> None:
        self._f.write(json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")
        self._f.flush()
        if self._fsync_each:
            os.fsync(self._f.fileno())
        for queue in self._subscribers:
            queue.put_nowait(record)

    def close(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
        if self._lock is not None:
            self._lock.release()  # the term ends where the log does (S6a)


def read_journal(path: Path | str) -> list[dict[str, Any]]:
    """Parse a run journal. A torn FINAL line (crash mid-append) is dropped
    -- write-ahead means the corresponding feed never happened; torn or
    invalid INTERIOR lines are corruption and raise loudly."""
    records: list[dict[str, Any]] = []
    raw = Path(path).read_bytes()
    lines = raw.split(b"\n")
    trailing = lines.pop() if lines and lines[-1] == b"" else None
    for index, line in enumerate(lines):
        if not line:
            raise EngineError(f"journal {path}: empty interior line {index + 1}")
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and trailing is None:
                break  # torn final append: the feed it preceded never ran
            raise EngineError(f"journal {path}: corrupt line {index + 1}: {exc}") from exc
    if not records or records[0].get("rec") != "header":
        raise EngineError(f"journal {path}: missing header record")
    return records


def baseline_id(records: list[dict[str, Any]]) -> str:
    return str(records[0].get("baseline_id") or "")


def read_attempts(records: list[dict[str, Any]]) -> list[Attempt]:
    """The log's admitted inputs, in admission order.

    A record written before S2 carries no `request_id` and no
    `fingerprint`; it gets the ones it would have been admitted under, so
    the rest of the pipeline sees one kind of attempt rather than two. The
    synthesized id names its position in this log, which is the only thing
    that could ever have identified it."""
    base = baseline_id(records)
    attempts: list[Attempt] = []
    for record in records:
        rec = record.get("rec")
        if rec not in ("input", "advance", "host"):
            continue
        kind = record.get("kind") if rec == "input" else None
        payload = record.get("payload") or {}
        host = HostCommand.from_wire(record["host"]) if rec == "host" else None
        source = record.get("source")
        expect = record.get("expect")
        actor = record.get("claimed_actor")
        epoch = int(record.get("epoch", INERT_EPOCH))
        attempts.append(
            Attempt(
                index=int(record["seq"]),
                at=datetime.fromisoformat(record["at"]),
                request_id=str(record.get("request_id") or f"log:{record['seq']}"),
                fingerprint=str(
                    record.get("fingerprint")
                    or fingerprint(
                        baseline_id=base,
                        kind=kind,
                        payload=payload,
                        source=source,
                        epoch=epoch,
                        expect=expect,
                        claimed_actor=actor,
                        host=host,
                    )
                ),
                kind=kind,
                payload=payload,
                host=host,
                source=source,
                # the precondition replays with the attempt: an input admitted
                # without a result is re-decided through the same gate, and the
                # revision it named is half of what that gate reads
                expect=expect,
                epoch=epoch,
                claimed_actor=actor,
            )
        )
    return sorted(attempts, key=lambda a: a.index)


def read_decisions(records: list[dict[str, Any]]) -> DecisionIndex:
    """Pass one: the durable decisions, indexed. Built from the whole log
    before anything is applied, because an attempt says nothing about its
    own fate -- its result is a LATER record, and the gap between them is
    the crash window (concurrency-model ss4)."""
    index = DecisionIndex()
    for attempt in read_attempts(records):
        index.note(attempt)
    for record in records:
        if record.get("rec") != "result":
            continue
        index.record(
            ApplyResult(
                index=int(record["index"]),
                request_id=str(record["request_id"]),
                decision=record["decision"],
                reason=record.get("reason"),
                revisions=record.get("revisions") or {},
            )
        )
    return index


def read_outbox(records: list[dict[str, Any]]) -> Outbox:
    """The effects this log intended, and what became of them (ss5).

    One pass, in file order, because an outcome always follows the effect it
    resolves -- the two are written by the same engine in that order, and an
    outcome for an unknown effect would mean the log lost the record that
    said what was meant."""
    outbox = Outbox()
    for record in records:
        if record.get("rec") == "effect":
            outbox.record(Effect.model_validate({k: v for k, v in record.items() if k != "rec"}))
        elif record.get("rec") == "effect_result":
            outbox.resolve(
                EffectOutcome.model_validate({k: v for k, v in record.items() if k != "rec"})
            )
    return outbox


@dataclass
class Replay:
    """Where the log left the state machine (concurrency-model ss2/ss4)."""

    frontiers: Frontiers = field(default_factory=Frontiers)
    decisions: DecisionIndex = field(default_factory=DecisionIndex)
    #: attempts admitted with no durable result -- the crash window, decided
    #: here by re-running the gate rather than guessed at
    recovered: list[ApplyResult] = field(default_factory=list)
    #: the effects this log intended and what became of them (ss5). Read from
    #: the records rather than re-planned: an effect the previous engine
    #: decided is a fact, and re-deriving it would let a changed planner
    #: silently disagree with the log about what was meant to happen.
    outbox: Outbox = field(default_factory=Outbox)


def replay_inputs(oracle: Oracle, records: list[dict[str, Any]]) -> Replay:
    """Apply the journal through `oracle`, two-pass (concurrency-model ss4).

    Pass one indexes the decisions; pass two applies each attempt in
    admission order. A durable decision is authoritative -- a rejected
    attempt is NOT fed, and an applied one is fed without re-deciding, so
    this build reproduces the log's history rather than the one its current
    gate would write. An attempt with no result is applied, because
    admission is the commit point; it goes through the gate on the way,
    since a decision is exactly what it is missing.

    The time half of every batch applies either way. A rejected completion
    still observed the clock, and the kill that observation let fire is a
    decision the estate already acted on -- skipping it wholesale would
    resurrect a killed job -- the case DL-44's amendment added the advance
    record for, now decided by record rather than by absence."""
    decisions = read_decisions(records)
    replay = Replay(decisions=decisions, outbox=read_outbox(records))
    for attempt in read_attempts(records):
        durable = decisions.for_index(attempt.index)
        applied = apply_attempt(oracle, attempt, decided=durable)
        if durable is None:
            decisions.record(applied.result)
            replay.recovered.append(applied.result)
        replay.frontiers = replay.frontiers.admit(attempt.at).record(attempt.index)
    return replay


def _last_journal_at(records: list[dict[str, Any]]) -> datetime:
    """max time the journal proves the run reached (ss7 'last journal at')."""
    stamps = [datetime.fromisoformat(records[0]["started_at"])]
    for record in records:
        for key in ("at", "started_at"):
            if key in record:
                stamps.append(datetime.fromisoformat(record[key]))
    return max(stamps)
