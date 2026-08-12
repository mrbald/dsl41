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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dsl41.ir import CatalogIR
from dsl41.oracle import Event, Oracle
from dsl41.runner_clock import EngineError

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

    def __init__(self, path: Path | str, *, fsync_each: bool, start_seq: int = 0) -> None:
        self.path = Path(path)
        self._f = self.path.open("ab")
        os.chmod(self.path, 0o600)  # owner-only: the WAL carries globals + every input
        self._fsync_each = fsync_each
        self.seq = start_seq
        #: live feeds for ss10 subscribe: every appended record is fanned out
        #: post-write; queues are unbounded (a slow subscriber buffers, the
        #: WAL never blocks on one)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    @classmethod
    def create(
        cls, path: Path | str, *, catalog: CatalogIR, clock_domain: str, started_at: datetime
    ) -> Journal:
        journal = cls(path, fsync_each=clock_domain == "real")
        journal._write(
            {
                "rec": "header",
                "catalog_hash": catalog_hash(catalog),
                "dsl41_version": _dsl41_version(),
                "clock_domain": clock_domain,
                "started_at": started_at.isoformat(),
            }
        )
        return journal

    def input(self, ev: Event, source: str | None) -> None:
        """source in {scheduler, adapter, control, reconcile} (ss7); None
        marks an unattributed script injection and never occurs in a real
        run. Persisted so replay re-derives the same causes (DL-68)."""
        self.seq += 1
        self._write(
            {
                "rec": "input",
                "seq": self.seq,
                "at": ev.at.isoformat(),
                "kind": ev.kind,
                "payload": ev.payload,
                "source": source,
            }
        )

    def advance(self, at: datetime) -> None:
        """A time observation the engine acted on (Oracle.advance): part of
        the input alphabet (DL-44 amendment) -- the timer firings it causes
        (term_run_time kills, alarms) must replay, or a crash after an
        advance-fired kill would resurrect the job. Shares the input seq so
        replay interleaves feeds and advances in the original order."""
        self.seq += 1
        self._write({"rec": "advance", "seq": self.seq, "at": at.isoformat()})

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


def replay_inputs(oracle: Oracle, records: list[dict[str, Any]]) -> None:
    """Apply the journal's input AND advance records, in seq order, through
    `oracle` (an advance is a time observation -- the other half of the
    input alphabet; DL-44 amendment)."""
    replayable = sorted(
        (r for r in records if r.get("rec") in ("input", "advance")),
        key=lambda r: int(r["seq"]),
    )
    for record in replayable:
        if record["rec"] == "advance":
            oracle.advance(datetime.fromisoformat(record["at"]))
        else:
            oracle.feed(
                Event(
                    at=datetime.fromisoformat(record["at"]),
                    kind=record["kind"],
                    payload=record["payload"],
                    # DL-68: provenance is part of the input alphabet -- replay
                    # must re-derive the same causes and started_by
                    source=record.get("source"),
                )
            )


def _last_journal_at(records: list[dict[str, Any]]) -> datetime:
    """max time the journal proves the run reached (ss7 'last journal at')."""
    stamps = [datetime.fromisoformat(records[0]["started_at"])]
    for record in records:
        for key in ("at", "started_at"):
            if key in record:
                stamps.append(datetime.fromisoformat(record[key]))
    return max(stamps)
