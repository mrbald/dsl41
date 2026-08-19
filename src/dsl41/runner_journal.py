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
`decision` record carries the decision that attempt got, appended after it,
which is what makes replay two-pass: pass one indexes the decisions, pass
two applies. An attempt whose decision never landed is applied, through the
same gate the live engine used (concurrency-model ss4, runner_admission).

DL-118 (period-model ss2.3) made the second half of that pair ONE line too.
It used to be a `result` record plus one standalone `effect` record per
intent, each its own `_write` and its own fsync, and the window between
them was the atomicity violation ss4 step 7 forbids: the decision durable,
the process dead before the KILL effect was written, recovery finding every
attempt decided and an empty outbox. A `decision` record now carries the
decision, the revisions it moved and the effects it planned, in admission
order, in one write. `result` and standalone `effect` are RETIRED: nothing
here writes them, and both readers still accept them, because a run root
written before DL-118 must keep replaying, resuming and reporting.

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

from collections.abc import Sequence
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
from pydantic import ValidationError

from dsl41.runner_effects import Effect, EffectOutcome, Outbox, is_valid_run_id
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
        _repair_tail(self.path)
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

    def decision(self, result: ApplyResult, effects: Sequence[Effect]) -> None:
        """Append the whole ss4 step-7 batch as ONE line (period-model ss2.3,
        DL-118): the decision an admitted attempt got, the revisions it
        moved, and every effect it planned.

        One line, one fsync -- the same argument `admit` already makes for
        the input side. Each effect lands here before anything ATTEMPTS it,
        which is the whole content of an outbox: an engine that dies between
        deciding and acting leaves the record that it meant to act, and a
        recorded kill is re-driven at resume rather than lost with the task
        that would have delivered it. Writing the decision and the intents
        separately left a window in which the first was durable and the
        second was not, and no precondition anywhere could see it (CM-17).

        Written unconditionally, including for an application that moved
        nothing and planned nothing: the ABSENCE of a decision is how replay
        recognises the crash window, so an absence that also meant "nothing
        happened" would make that window unreadable.

        `effects` is in ADMISSION ORDER -- the order `Outbox` receives them,
        so a SPAWN precedes its run's later KILL.

        The index rides under `index`, not `seq`: `seq` is the ss10
        subscribe cursor, and a decision shares its attempt's number, so two
        records under one cursor value would leave the second undeliverable
        to a resuming subscriber (DL-89).

        `legacy_batch` is on every decision and `false` from this writer. A
        `true` one is a batch folded from a legacy estate's separate fsyncs
        at adoption (period-model ss11), which nothing builds yet -- the
        field is on the record now so the schema is one rather than two.

        A NATIVE decision names its identity at birth, and this writer
        refuses one that does not: `generation` on every effect, `run_id`
        on every SPAWN (DL-118, PR-16/PR-36a). The model's None defaults
        exist so records READ from a pre-DL-118 journal validate -- a fresh
        effect reaching this method without them is a planner bug, and
        writing it would smuggle the legacy shape out under
        `legacy_batch: false`."""
        for effect in effects:
            if (
                effect.generation is None
                or (effect.kind == "SPAWN" and effect.run_id is None)
                or (effect.run_id is not None and not is_valid_run_id(effect.run_id))
            ):
                raise EngineError(
                    f"effect {effect.effect_id}: a native decision binds identity at"
                    " birth -- generation on every effect, run_id on a SPAWN, and any"
                    " run_id in the ss11a uuid4 grammar (DL-118); an empty or"
                    " freehand id would lose to a fallback mint downstream"
                )
        self._write(
            {
                "rec": "decision",
                "index": result.index,
                "request_id": result.request_id,
                "decision": result.decision,
                "reason": result.reason,
                "revisions": result.revisions,
                "legacy_batch": False,
                "effects": [effect.model_dump(mode="json") for effect in effects],
            }
        )

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
        if self._lock is not None:
            # ss1's epoch-conditional append, and ss7's "losing proof stops
            # dispatch, not merely renewal" (S6b). BEFORE the write, so a
            # leader that cannot prove it leads admits nothing rather than
            # admitting and then discovering it had no right to.
            #
            # An append is also what precedes every effect -- the outbox
            # records intent before the attempt -- so fencing appends fences
            # dispatch without a second mechanism to keep in step with this
            # one. An engine with nothing to append dispatches nothing, which
            # is why there is no background prober: the only proof that goes
            # unchecked is proof nothing was about to rely on.
            self._lock.check()
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


def _repair_tail(path: Path) -> None:
    """Make an existing WAL end on a line boundary before anything appends.

    `_write` puts `json + "\\n"` in one call, and a crash mid-write leaves a
    prefix of it. `read_journal` drops a torn final line (the feed it
    preceded never ran), and the bytes must agree with that reading before
    the next record lands: appended straight after the fragment, the
    successor's `leader` makes one corrupt interior line out of the two,
    and every later read raises. Two shapes, both fixed under the caller's
    lock and fsynced: a tail that parses lost only its newline and gets one
    back; any other tail is cut at the last newline. What `read_journal`
    returns is the same before and after -- this changes bytes, never
    records."""
    if not path.exists():
        return
    with path.open("r+b") as f:
        data = f.read()
        if not data or data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n") + 1
        try:
            json.loads(data[cut:])
        except ValueError:
            f.truncate(cut)
        else:
            f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())


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
    own fate -- its decision is a LATER record, and the gap between them is
    the crash window (concurrency-model ss4).

    `result` is the pre-DL-118 spelling of the same fact, read for the
    reason every legacy shape here is read: a run root does not become
    unreadable because the writer moved on."""
    index = DecisionIndex()
    for attempt in read_attempts(records):
        index.note(attempt)
    for record in records:
        if record.get("rec") not in ("decision", "result"):
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
    said what was meant. Inside one `decision` the nested list is already in
    admission order, so reading it in order is all per-run ordering takes.

    A standalone `effect` record is the pre-DL-118 dialect and folds into
    the same outbox: the two spellings differ in how many fsyncs the writer
    spent, not in what they say."""
    outbox = Outbox()
    # deferred fold checks (period-model ss11, PR-48), run after the pass
    # when the outcomes have been read: adoption refuses a pending legacy
    # outbox, so EVERY folded effect must resolve -- and a fold's
    # null-run_id SPAWN only ever legally resolves retired or indeterminate
    # (a run that never reached an adapter has no id and no file; an
    # applied one must have both)
    folded: list[str] = []
    unidentified: list[str] = []
    for record in records:
        if record.get("rec") == "decision":
            # a `decision` record has exactly one shape (ss2.3): a BOOLEAN
            # `legacy_batch` and a LIST `effects`. `.get(...) or []` here
            # would read a corrupt or foreign record as an empty native
            # decision -- intents silently lost, provenance silently
            # invented -- so the shape is checked, not defaulted around.
            marker = record.get("legacy_batch")
            if not isinstance(marker, bool):
                raise EngineError(
                    f"decision at index {record.get('index')}: legacy_batch is"
                    f" {marker!r}, not a boolean -- the fold allowances are claimed"
                    " with exactly true (DL-118)"
                )
            effects_field = record.get("effects")
            if not isinstance(effects_field, list):
                raise EngineError(
                    f"decision at index {record.get('index')}: effects is"
                    f" {type(effects_field).__name__}, not a list (DL-118)"
                )
            for effect in effects_field:
                try:
                    parsed = Effect.model_validate(effect)
                except ValidationError as exc:
                    # strict fields surface here: `generation: false` or "0"
                    # must not coerce past the fold's exact-0 gate
                    raise EngineError(
                        f"decision at index {record.get('index')}: malformed effect: {exc}"
                    ) from exc
                if parsed.run_id is not None and not is_valid_run_id(parsed.run_id):
                    # any run_id a decision carries is in the ss11a grammar
                    # -- the legacy estate's adapter minted uuid4 too
                    raise EngineError(
                        f"decision at index {record.get('index')}: effect"
                        f" {parsed.effect_id} carries run_id {parsed.run_id!r} outside"
                        " the ss11a grammar (DL-118)"
                    )
                if marker:
                    # a fold is a defined reconstruction, not a guess: one
                    # legacy executor at generation 0, every effect resolved
                    if parsed.generation != 0:
                        raise EngineError(
                            f"decision at index {record.get('index')}: folded effect"
                            f" {parsed.effect_id} carries generation"
                            f" {parsed.generation!r}; a legacy estate had exactly one"
                            " executor at generation 0 (period-model ss11)"
                        )
                    folded.append(parsed.effect_id)
                    if parsed.kind == "SPAWN" and parsed.run_id is None:
                        unidentified.append(parsed.effect_id)
                elif parsed.generation is None or (
                    parsed.kind == "SPAWN" and parsed.run_id is None
                ):
                    # the writer refuses these shapes (Journal.decision), so
                    # a native decision carrying one was not written by this
                    # code: corruption or a foreign writer. Accepting it
                    # would let resume mint an identity AFTER the
                    # transaction -- the exact hole DL-118 closed.
                    raise EngineError(
                        f"decision at index {record.get('index')}: native effect"
                        f" {parsed.effect_id} carries no birth identity (DL-118)"
                    )
                outbox.record(parsed)
        elif record.get("rec") == "effect":
            outbox.record(Effect.model_validate({k: v for k, v in record.items() if k != "rec"}))
        elif record.get("rec") == "effect_result":
            outbox.resolve(
                EffectOutcome.model_validate({k: v for k, v in record.items() if k != "rec"})
            )
    for effect_id in folded:
        if outbox.state_of(effect_id) == "pending":
            raise EngineError(
                f"legacy fold: effect {effect_id} is pending -- adoption refuses a"
                " pending legacy outbox (period-model ss11, PR-48), so a fold that"
                " carries one was not made by it; re-driving it would execute"
                " legacy intent adoption already refused"
            )
    for effect_id in unidentified:
        if outbox.state_of(effect_id) not in ("retired", "indeterminate"):
            raise EngineError(
                f"legacy fold: SPAWN {effect_id} has no run_id and its outcome is"
                f" {outbox.state_of(effect_id)!r} -- null is legal only for a run"
                " that provably never reached an adapter (period-model ss11)"
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


def last_journal_at(records: list[dict[str, Any]]) -> datetime:
    """max time the journal proves the run reached (ss7 'last journal at')."""
    stamps = [datetime.fromisoformat(records[0]["started_at"])]
    for record in records:
        for key in ("at", "started_at"):
            if key in record:
                stamps.append(datetime.fromisoformat(record[key]))
    return max(stamps)
