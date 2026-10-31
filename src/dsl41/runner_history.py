"""Run history (DL-113): a projection, not a new record kind.

`docs/runner-design.md` ss7 lists every record the journal writes; this module adds none. It
folds `dispatch`, the `input` records the ss4 stale-completion gate journals, each `decision`
(its verdict on a completion), and the run root's manifest and spool into one row per job
run. It is offline: no engine, no control socket, no wire verb.

Two layers: `fold_run_rows` is a pure function of parsed journal records plus an optional
catalog and trace. `read_run_root` is the I/O shell: it rebuilds the root's catalog from its own
stored inputs (DL-130's content-addressed bundle, so no estate-file argument), replays the WAL
through a fresh `Oracle` as `dsl41 journal` does, and reads the spool. `read_spool` is a separate function so the WAL and
the spool stay independent I/O concerns. The decision itself is `docs/decision-log.md` DL-113;
five decisions shape it.

1. **One clock per row.** `spawn.json`/`status.json` are process truth; the journal's
   `dispatch` record plus the terminal `STATUS` input are semantic truth. `clock_source` is
   "spool" only when spawn.json names this exact (job, run_number) and, for a run the journal
   shows complete, status.json does too. `started_at` and `ended_at` never come from different
   clocks. A run still open takes spool's `started_at` from a valid spawn.json alone.

2. **Boxes get rows, from the trace.** A box has no `dispatch` record (runner-design ss4) and
   its fold is emitted, never journaled (ss7), so a box row is built from the replayed trace:
   the Nth `*->STARTING` transition for the job opens run N, and the next terminal transition
   closes it (SEM-01 latching: a later CHANGE_STATUS overwrites the recorded close). A box set
   INACTIVE while it runs closes its window as INACTIVE at that instant (SEM-18, DL-242).
   `clock_source` is always "journal". `started_by` for every row, box or leaf, is the cause
   the STARTING transition carries, which a `dispatch` record does not.

   The trace also closes the leaf runs that produce no `STATUS` input: KILLJOB and a
   term_run_time auto-TERMINATE are decided by the oracle while it processes the KILLJOB or
   timer input (the job chart's kill and term_run_time rows, ``oracle_writers``), so no adapter
   completion matches by run_number. Reading only `dispatch` and `input(kind=STATUS)` would
   report such a run as RUNNING. A box's INACTIVE cascading to a live member (SEM-18, DL-242)
   is the same case: the trace's `RUNNING->INACTIVE` closes the run.

3. **An incomplete run gets no duration.** A run with no journal-visible and no trace close
   gets `ended_at: None, duration_s: None`. So does a `STATUS` input from `source="reconcile"`
   whose payload has no `ended_at`: a restart's plan step (`runner.Engine.record_outcome`)
   writes it only when status.json supplied a true one, and a feed with no record omits it
   because the process's real end is unknown and the event's own `at` is only when the
   engine noticed. Such a row keeps its recorded status and a null duration.

4. **Segmentation follows the job, not the estate.** Every row carries the `catalog_hash` of its
   segment and this job's own `job_hash`, in every `--format`. The break line `--format table`
   prints between two consecutive rows of one job fires on `job_hash`, falling back to
   `catalog_hash` only when either row has none. The estate hash moves for every job when any
   job changes (`reload_diff.catalog_hash_v2`), so a break on it would mark the whole estate.
   `runner_config.job_fingerprints` states what the per-job hash can say: it fingerprints the
   resolved definition, so an estate whose placeholders vary per run gets no more than
   `catalog_hash` gave it. Rows sort by (job, started_at), so a break lands where that job changed.

5. **A missing manifest degrades; a wrong one refuses.** A root whose retention pruned its
   manifest folds from records alone. A manifest whose `catalog_hash` disagrees with the
   journal's opening record refuses, because that fact is wrong, not missing. The degraded
   path's cost is carried per row in `fidelity`, where a JSON or CSV consumer sees it: no box
   rows, no `box_name`, `started_by` or `job_hash`, bare-default exit-code verdicts, and a run
   closed by KILLJOB or term_run_time reading as RUNNING (decision 2's trace is unavailable). A
   completion the ss4 crash window left undecided is refused on the same ground (DL-156): with
   no catalog there is no gate to re-run, so the row stands on what the records decide and
   `undecided` says the newest completion did not. The full-fidelity path replays the gate and
   takes its recovered verdicts.

6. **A pruned store anchors at its snapshot.** Retention deletes the oldest records, and the
   replay starts from the latest snapshot (`runner_journal.replay_store`), so the trace covers
   only what follows it. The snapshot's job rows are the anchor: a job's run numbering continues
   from the row's `run_number`, and that run's window opens or closes as the row says. A run
   that began at or before the anchor is `partial`: its status comes from its retained
   completion, the anchor row and the trace after it, and a run that ended before the anchor
   with none of those is `UNKNOWN`, never RUNNING. `full` is only for a run the trace covers
   from its start.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.canon import is_wire_int, naive_utc
from dsl41.ir import CatalogIR, Semantics
from dsl41.oracle_state import LIVE, TERMINAL, JobRuntime, JobStatus, OracleError, TraceEntry
from dsl41.runner_adapters import load_json, spool_names_run
from dsl41.runner_admission import ApplyResult
from dsl41.runner_clock import EngineError
from dsl41.reload_diff import catalog_hash_v2
from dsl41.runner_config import Configuration, job_fingerprints
from dsl41.runner_journal import (
    CONFIG_MARKER,
    StoreReplay,
    load_configuration,
    open_reader,
    replay_store,
    wire_record,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_store import Store


class RunHistoryError(Exception):
    """A run root cannot be read or replayed into history: a bad manifest, a catalog that does
    not lower, or a journal replay refusal. The CLI exits 2."""


class RunRow(BaseModel):
    """One job run, folded from a run root's journal (and, where available,
    its spool). See the module docstring for the five decisions this shape
    encodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    run_number: int
    catalog_hash: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float | None = None
    #: the run's status, or `UNKNOWN` for a run that ended before the snapshot a pruned store
    #: replays from with no retained record of how (decision 6)
    status: JobStatus | Literal["UNKNOWN"]
    exit_code: int | None = None
    started_by: str | None = None
    run_dir: str | None = None
    box_name: str | None = None
    #: which clock supplied started_at/ended_at together (decision 1): "spool" means both came from
    #: the wrapper's spool (or spawn.json alone, for an open run); "journal" means at least one
    #: fell back to the engine's records.
    clock_source: Literal["spool", "journal"]
    #: this job's definition fingerprint (decision 4), or None without a catalog; it moves only for
    #: this job's change, where `catalog_hash` moves for any change in the estate.
    job_hash: str | None = None
    #: how much of the row could be established (decision 5). "records_only" means the run root had
    #: no period manifest: no box rows, null `box_name`, `started_by` and `job_hash`, the bare
    #: SEM-09 exit-code default, and a run closed by KILLJOB or term_run_time reads as RUNNING.
    #: It rides on the row so a JSON or CSV consumer sees it. "partial" means the run began
    #: before the snapshot a pruned store replays from: its status comes from the snapshot's
    #: rows and the records after it, and its trace evidence before the snapshot is gone
    #: (decision 6).
    fidelity: Literal["full", "records_only", "partial"] = "full"
    #: the ss4 crash window (DL-156): the newest completion the records hold for this run was
    #: admitted and has no decision record, and with no catalog there is no gate to re-run, so it
    #: did not decide the row. The status stands on the earlier durable verdict or the
    #: pre-completion RUNNING. A decided row with a later undecided completion keeps its status
    #: and the flag. Only True at `records_only` fidelity; a full-fidelity read takes the
    #: gate's recovered verdict.
    undecided: bool = False


@dataclass(frozen=True)
class SpoolRead:
    """One run's spool facts, validated against (job, run_number); plain data."""

    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class _TraceWindow:
    """One run's boundaries in the replayed trace (decision 2): the Nth `*->STARTING` transition for
    a job opens it, the next terminal transition closes it (None if none shows). `partial` marks
    a run that began before the trace (decision 6); `approximate` one whose start only an anchor
    row gives, as the row's last status change."""

    run_number: int
    started_at: datetime
    started_by: str
    ended_at: datetime | None
    status: str | None
    partial: bool = False
    approximate: bool = False
    #: the configuration rev in force where the run opened, in trace order; None where nothing
    #: says (an anchor row, a history written without it)
    rev: int | None = None


def _marked_rev(entry: TraceEntry) -> int | None:
    """The rev a `CONFIG` marker names, or None for any other entry."""
    if entry.job or entry.transition != CONFIG_MARKER:
        return None
    return int(entry.cause.rsplit(" ", 1)[1])


def definition_change(previous: RunRow, row: RunRow) -> Literal["definition", "catalog"] | None:
    """Whether two consecutive rows of one job cross a change, and which signal says so
    (decision 4); None when nothing changed.

    `"definition"` is this job's `job_hash` moving. `"catalog"` is the estate-wide fallback,
    returned only when a row has no fingerprint (`fidelity="records_only"`), so a caller reports
    the weaker signal as weaker."""
    if previous.job_hash is not None and row.job_hash is not None:
        return None if previous.job_hash == row.job_hash else "definition"
    return None if previous.catalog_hash == row.catalog_hash else "catalog"


def _parse_timestamp(value: str) -> datetime:
    """Normalize a spool/manifest timestamp to the journal's naive-UTC basis (canon.naive_utc,
    DL-178j). Spool times may carry a UTC offset (supervisor-protocol ss3); the journal's never
    do (RealClock strips tzinfo)."""
    return naive_utc(datetime.fromisoformat(value))


#: The statuses an anchor row closes its run with (decision 6): a terminal end, or the INACTIVE
#: a box cascade or an injected status leaves on a run (DL-242).
_CLOSING: frozenset[str] = frozenset(TERMINAL | {"INACTIVE"})


def _windows_from_entries(
    entries: Sequence[TraceEntry],
    anchor: JobRuntime | None = None,
    history: Sequence[_TraceWindow] = (),
    revs: Sequence[int | None] = (),
) -> list[_TraceWindow]:
    """One job's trace entries, filtered to that job and in trace order, folded into run windows
    (decision 2). Out-of-band markers (`SCHED_DISARM`, `START_REFUSED`, ...) carry no "->" and are
    skipped. The first window is run 1. A pruned store's replay starts at a snapshot (decision
    6): the snapshot's run history gives the runs before the trace and the trace continues the
    last; with no history for the job, the anchor row gives its run, opened or closed as the row
    says. The trace's first start is the run after it. `revs` is the configuration rev each
    entry ran under, in step with `entries`; a window takes its opening entry's."""
    windows: list[_TraceWindow] = []
    rev: int | None = None
    run_number = 0
    started_at: datetime | None = None
    started_by = ""
    ended_at: datetime | None = None
    status: str | None = None
    partial = approximate = False
    if history and (anchor is None or history[-1].run_number >= anchor.run_number):
        windows = list(history[:-1])
        last = history[-1]
        run_number, started_at, started_by = last.run_number, last.started_at, last.started_by
        ended_at, status, partial, approximate = last.ended_at, last.status, True, last.approximate
        rev = last.rev
    elif anchor is not None and anchor.run_number:
        # the history kept nothing of the row's run (retention dropped it): the row still
        # numbers the next run
        windows = list(history)
        run_number, partial, approximate = anchor.run_number, True, True
        started_at = anchor.status_at or datetime.min
        started_by = anchor.started_by or ""
        if anchor.status in _CLOSING:
            ended_at, status = anchor.last_end_at or anchor.status_at, anchor.status
    for position, entry in enumerate(entries):
        old, sep, new = entry.transition.partition("->")
        if not sep:
            continue
        if new == "STARTING":
            if started_at is not None:
                windows.append(
                    _TraceWindow(
                        run_number,
                        started_at,
                        started_by,
                        ended_at,
                        status,
                        partial,
                        approximate,
                        rev,
                    )
                )
            run_number += 1
            started_at, started_by, ended_at, status = entry.at, entry.cause, None, None
            partial = approximate = False
            rev = revs[position] if position < len(revs) else None
        elif new in TERMINAL and started_at is not None:
            # SEM-01 latching: a later CHANGE_STATUS overwrites the close recorded for this run
            ended_at, status = entry.at, new
        elif new == "INACTIVE" and old in LIVE and started_at is not None:
            # DL-242: the oracle moved a live run to INACTIVE with no STATUS input of its own
            # (the SEM-18 cascade from its box). The row closes at that instant, as for an
            # injected INACTIVE (DL-235). INACTIVE from a terminal status (a box-start reset)
            # leaves the recorded close alone.
            ended_at, status = entry.at, new
    if started_at is not None:
        windows.append(
            _TraceWindow(
                run_number, started_at, started_by, ended_at, status, partial, approximate, rev
            )
        )
    return windows


def _trace_windows_by_job(
    trace: Sequence[TraceEntry],
    anchor: Mapping[str, JobRuntime] | None = None,
    history: Mapping[str, Sequence[_TraceWindow]] | None = None,
    start_rev: int | None = None,
) -> dict[str, dict[int, _TraceWindow]]:
    """job -> {run_number: window}; `anchor` is the job rows a pruned store's replay started
    from and `history` the run history its snapshot carries (decision 6). `start_rev` is the
    configuration rev the trace opens under; each `CONFIG` marker moves it, so a window carries
    the rev in force where it opened, in trace order."""
    by_job: dict[str, list[TraceEntry]] = {job: [] for job in [*(anchor or {}), *(history or {})]}
    revs: dict[str, list[int | None]] = {job: [] for job in by_job}
    current = start_rev
    for entry in trace:
        marked = _marked_rev(entry)
        if marked is not None:
            current = marked
            continue
        by_job.setdefault(entry.job, []).append(entry)
        revs.setdefault(entry.job, []).append(current)
    return {
        job: {
            window.run_number: window
            for window in _windows_from_entries(
                entries, (anchor or {}).get(job), (history or {}).get(job, ()), revs[job]
            )
        }
        for job, entries in by_job.items()
    }


def _history_windows(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[_TraceWindow]]:
    """A snapshot's run history read back as windows, each `partial` (decision 6)."""
    return {
        job: [
            _TraceWindow(
                run_number=int(run["run"]),
                started_at=datetime.fromisoformat(run["started_at"]),
                started_by=str(run["started_by"]),
                ended_at=None
                if run["ended_at"] is None
                else datetime.fromisoformat(run["ended_at"]),
                status=run["status"],
                partial=True,
                approximate=bool(run["approximate"]),
                rev=int(run["rev"]),
            )
            for run in runs
        ]
        for job, runs in history.items()
    }


def run_history(
    history: Mapping[str, Sequence[Mapping[str, Any]]],
    trace: Sequence[TraceEntry],
    since: datetime | None,
    anchor: Mapping[str, JobRuntime] | None = None,
    start_rev: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """The run history a snapshot carries (decision 6): the history the engine restored from,
    continued by its trace since, keeping every run still open or ended at or after `since` (the
    retention window's start; None keeps every run). `anchor` is the job rows the engine
    restored with: a job whose history retention emptied is still numbered from its row's run.
    A pruned store's reader seeds its windows from it, so a retained run that began before the
    snapshot keeps its row. Each run carries `rev`, the configuration rev it opened under
    (`start_rev` is the trace's), so a pruned store's reader labels it with its own rev."""
    windows = _trace_windows_by_job(trace, anchor, _history_windows(history), start_rev)
    out: dict[str, list[dict[str, Any]]] = {}
    for job, runs in sorted(windows.items()):
        kept = [
            {
                "run": window.run_number,
                "started_at": window.started_at.isoformat(),
                "started_by": window.started_by,
                "ended_at": None if window.ended_at is None else window.ended_at.isoformat(),
                "status": window.status,
                "approximate": window.approximate,
                "rev": window.rev,
            }
            for _, window in sorted(runs.items())
            # a run retention dropped from the history left its rev with it: not kept again
            if window.rev is not None
            and (since is None or window.ended_at is None or window.ended_at >= since)
        ]
        if kept:
            out[job] = kept
    return out


@dataclass(frozen=True, eq=False)
class _Revision:
    """One configuration rev as the fold reads it: its catalog (None without one), its hash, and
    each job's definition fingerprint."""

    catalog: CatalogIR | None
    catalog_hash: str
    fingerprints: Mapping[str, str]


class _Revisions:
    """The configuration each run was read under (decision 4). With the store's configuration
    rows (`catalogs`, by rev), a run is read under the rev its run row holds: the rev in force
    where the run opened, which the replayed trace's window carries and a snapshot's retained
    run history keeps. A run is not an attempt: a `dispatch` record's rev is that attempt's own
    and is never read as the run's. A run is read under that rev's catalog (its catalog hash,
    its job fingerprint, its box and its exit-code verdict); a run whose row holds no rev, or a
    rev the store holds no row for, is refused. With no rows (`catalogs` None) every run reads
    `catalog` under the first segment's hash."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        catalog: CatalogIR | None,
        catalogs: Mapping[int, CatalogIR] | None,
    ) -> None:
        self._catalogs = catalogs is not None
        self.only = _Revision(
            catalog,
            str(records[0]["catalog_hash"]),
            {} if catalog is None else job_fingerprints(catalog),
        )
        self._by_rev: dict[int, _Revision] = {}
        for rev, known in (catalogs or {}).items():
            self._by_rev[rev] = _Revision(known, catalog_hash_v2(known), job_fingerprints(known))

    def all(self) -> list[_Revision]:
        """Each rev once."""
        return list(self._by_rev.values()) if self._catalogs else [self.only]

    def of_rev(self, rev: int | None, run: str) -> _Revision:
        """The revision a run row's `rev` names."""
        if not self._catalogs:
            return self.only
        if rev is None or rev not in self._by_rev:
            raise RunHistoryError(
                f"run {run} records no configuration rev the store holds ({rev!r}): a run's"
                " rev is recorded where it opens, never derived (DL-427)"
            )
        return self._by_rev[rev]


def _box_rows(
    trace: Sequence[TraceEntry],
    revisions: _Revisions,
    windows_by_job: Mapping[str, Mapping[int, _TraceWindow]],
) -> list[RunRow]:
    """Every BOX job's run rows, entirely from the trace (decision 2); a box has no dispatch record
    and no spool. The windows are the caller's, so a box and a leaf in one fold are numbered
    alike. A window is a box run only under a rev whose catalog has the job as a box."""
    rows: list[RunRow] = []
    boxes = sorted(
        {
            name
            for revision in revisions.all()
            if revision.catalog is not None
            for name, job_ir in revision.catalog.jobs.items()
            if job_ir.job_type == "BOX"
        }
    )
    for job in boxes:
        for window in windows_by_job.get(job, {}).values():
            if window.rev is None and window.approximate:
                continue  # only an anchor row names it: retention dropped its record and rev
            revision = revisions.of_rev(window.rev, f"{job}.{window.run_number}")
            catalog = revision.catalog
            job_ir = None if catalog is None else catalog.jobs.get(job)
            if job_ir is None or job_ir.job_type != "BOX":
                continue
            # an anchor row's window starts at the row's last status change, not the run's start
            duration = (
                None
                if window.ended_at is None or window.approximate
                else (window.ended_at - window.started_at).total_seconds()
            )
            rows.append(
                RunRow(
                    job=job,
                    run_number=window.run_number,
                    catalog_hash=revision.catalog_hash,
                    started_at=window.started_at,
                    ended_at=window.ended_at,
                    duration_s=duration,
                    status=window.status or "RUNNING",  # type: ignore[arg-type]
                    exit_code=None,
                    started_by=window.started_by,
                    run_dir=None,
                    box_name=job_ir.box.box_name,
                    clock_source="journal",
                    job_hash=revision.fingerprints.get(job),
                    fidelity="partial" if window.partial else "full",
                )
            )
    return rows


def _journal_ended_at(completion: dict[str, Any] | None) -> datetime | None:
    """The terminal STATUS input's truthful end time, or None (decision 3): no completion, or a
    reconcile completion whose payload has no `ended_at` (a restart's plan step writes the
    key only when the attempt's status.json carried a real one)."""
    if completion is None:
        return None
    payload = completion["payload"]
    ended_at = payload.get("ended_at")
    if isinstance(ended_at, str):
        return _parse_timestamp(ended_at)
    if completion["source"] == "reconcile":
        return None
    return _parse_timestamp(str(completion["at"]))


def _leaf_status(
    job: str, catalog: CatalogIR | None, completion: dict[str, Any] | None
) -> tuple[str, int | None]:
    """SUCCESS/FAILURE for a bare exit_code payload uses the job's own SEM-09 boundary
    (`_handle_status` in oracle.py); without a catalog it falls back to max_exit_success=0."""
    if completion is None:
        return "RUNNING", None
    payload = completion["payload"]
    exit_code = payload.get("exit_code")
    exit_code = exit_code if isinstance(exit_code, int) else None
    status = payload.get("status")
    if isinstance(status, str):
        return status, exit_code
    if exit_code is not None:
        sem = catalog.jobs[job].sem if catalog is not None and job in catalog.jobs else Semantics()
        return ("SUCCESS" if sem.exit_is_success(exit_code) else "FAILURE"), exit_code
    return "RUNNING", None


def attempt_index(record: Mapping[str, Any]) -> int | None:
    """A record's attempt number, read exactly: `seq` on an input, `index` on the `decision` that
    answers it (runner-design ss7). None when the field is absent or not an int (`true` is not
    1, as `period.check_segment_record` applies).

    None fails open on the input side: a STATUS input with a malformed `seq` drops out of both
    checks below and its completion decides the row (decision 5's degrade posture). A malformed
    decision `index` lands in neither set: the rejected-set miss fails open, and at records-only
    fidelity the decided-set miss reads as the crash window, so the completion is refused
    (`_decided_attempts`, DL-156)."""
    key = "index" if record.get("rec") == "decision" else "seq"
    value = record.get(key)
    return value if is_wire_int(value) else None


def _rejected_attempts(records: list[dict[str, Any]]) -> set[int]:
    """The attempt indices a durable `decision` REJECTED.

    A decision is authoritative (concurrency-model ss4): `replay_inputs` does not feed a rejected
    attempt to the oracle, so the fold reports none. Only an explicit `rejected` is here. An
    attempt with no decision (the ss4 crash window) is absent, because the records do not say
    what became of it. `replay_inputs` re-decides it through the gate (`apply_attempt` with
    `decided=None`, DL-156) and reports the verdict in `Replay.recovered`; the full-fidelity fold
    unions the recovered rejections in beside this set (`_leaf_rows`), the verdict a resume
    derives from the same records. A fold of records alone cannot run the gate and refuses to
    let such a completion decide its row (`RunRow.undecided`)."""
    rejected: set[int] = set()
    for record in records:
        if record.get("rec") != "decision" or record.get("decision") != "rejected":
            continue
        index = attempt_index(record)
        if index is not None:
            rejected.add(index)
    return rejected


def _decided_attempts(records: list[dict[str, Any]]) -> set[int]:
    """The attempt indices any durable `decision` answered, applied and rejected alike.

    An input whose `seq` is in no decision is the ss4 crash window (`Journal.decision`). At
    records-only fidelity there is no gate to re-run over it, so `_leaf_rows` does not let such
    a completion decide its row (DL-156)."""
    decided: set[int] = set()
    for record in records:
        if record.get("rec") != "decision":
            continue
        index = attempt_index(record)
        if index is not None:
            decided.add(index)
    return decided


@dataclass(frozen=True)
class _LeafIndex:
    """The leaf index for one segment's CMD/FW walk (D3, DL-178w): every container
    `_emit_leaf_rows` reads, keyed as `_index_leaves` fills them."""

    dispatch_by_key: dict[tuple[str, int], dict[str, Any]]
    order: list[tuple[str, int]]
    completion_by_key: dict[tuple[str, int], dict[str, Any]]
    undecided_keys: set[tuple[str, int]]


def _trace_run_at(windows: Mapping[int, _TraceWindow], at: datetime) -> int | None:
    """The run the trace had opened for a job strictly before `at`, or None
    when the trace opened none. Ties go to the earlier run, the attribution
    the dispatch records already gave."""
    started = [run for run, window in windows.items() if naive_utc(window.started_at) < at]
    return max(started, default=None)


def _index_leaves(
    records: list[dict[str, Any]],
    rejected: set[int],
    decided: AbstractSet[int],
    fidelity: Literal["full", "records_only"],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]] | None = None,
) -> _LeafIndex:
    """Walk phase (D3): one pass over the segment's records builds the leaf index. `rejected` and
    `decided` are the caller's, computed before this walk: a decision is a later record than the
    attempt it answers, so one pass here would store a completion it cannot yet judge."""
    dispatch_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    last_run_number: dict[str, int] = {}
    completion_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    undecided_keys: set[tuple[str, int]] = set()

    for record in records:
        rec = record.get("rec")
        if rec == "dispatch":
            d_job, d_run = str(record["job"]), int(record["run_number"])
            key = (d_job, d_run)
            if key not in dispatch_by_key:
                order.append(key)
            dispatch_by_key[key] = record
            last_run_number[d_job] = d_run
        elif rec == "input" and record.get("kind") == "STATUS":
            index = attempt_index(record)
            if index in rejected:
                # the ss4 gate rejected this one, durably or on the replay's re-run (DL-156), and
                # the oracle never saw it, so neither does the row: a late `exit 0` the engine
                # refused must not overwrite the real FAILURE
                continue
            payload = record.get("payload") or {}
            c_job = payload.get("job")
            if not isinstance(c_job, str):
                continue
            c_run = payload.get("run_number")
            if not isinstance(c_run, int):
                # an operator CHANGE_STATUS names no run_number (cli.py's sendevent has no such
                # option): it overwrites the run currently open, as the oracle's SEM-01 read does
                c_run = last_run_number.get(c_job)
                # DL-242: a run can start and end with no dispatch (a job_terminator cascade
                # TERMINATES it during its STARTING transition and its start is superseded). A later
                # operator status is not that run's close, and rewriting an earlier close would
                # corrupt a finished run. An undispatched run has no row.
                traced = _trace_run_at(
                    (trace_windows or {}).get(c_job, {}),
                    _parse_timestamp(str(record["at"])),
                )
                if traced is not None and (c_run is None or traced > c_run):
                    continue
                if c_run is None:
                    continue
            if fidelity == "records_only" and index is not None and index not in decided:
                # the ss4 crash window with no gate to re-run: the fold does not decide, the row
                # stands on what the records decide, and `undecided` marks it (DL-156)
                undecided_keys.add((c_job, c_run))
                continue
            undecided_keys.discard((c_job, c_run))
            completion_by_key[(c_job, c_run)] = {
                "payload": payload,
                "at": record["at"],
                "source": record.get("source"),
            }

    return _LeafIndex(
        dispatch_by_key=dispatch_by_key,
        order=order,
        completion_by_key=completion_by_key,
        undecided_keys=undecided_keys,
    )


def _emit_leaf_rows(
    index: _LeafIndex,
    revisions: _Revisions,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]],
    fidelity: Literal["full", "records_only"],
    anchor_runs: Mapping[str, int],
) -> list[RunRow]:
    """Emit phase (D3): one `RunRow` per dispatched (job, run_number), in dispatch order, as a
    pure function of the leaf index. `anchor_runs` is each job's run at the snapshot a pruned
    store replays from (decision 6)."""
    rows: list[RunRow] = []
    for key in index.order:
        job, run_number = key
        window = trace_windows.get(job, {}).get(run_number)
        # the run row's rev, never an attempt's dispatch
        revision = revisions.of_rev(None if window is None else window.rev, f"{job}.{run_number}")
        catalog = revision.catalog
        dispatch = index.dispatch_by_key[key]
        completion = index.completion_by_key.get(key)
        anchored = run_number <= anchor_runs.get(job, 0)

        journal_started = _parse_timestamp(str(dispatch["started_at"]))
        journal_ended = _journal_ended_at(completion)
        status: str
        if completion is not None:
            status, exit_code = _leaf_status(job, catalog, completion)
        elif window is not None and window.status is not None:
            status, exit_code = window.status, None
            journal_ended = window.ended_at
        elif anchored and run_number < anchor_runs[job]:
            # a later run had started by the snapshot, so this one ended before it, and nothing
            # retained says how (decision 6)
            status, exit_code = "UNKNOWN", None
        else:
            status, exit_code = "RUNNING", None

        spool_read = spool.get(key)
        if (
            spool_read is not None
            and spool_read.started_at is not None
            and (journal_ended is None or spool_read.ended_at is not None)
        ):
            started_at, ended_at, clock_source = spool_read.started_at, spool_read.ended_at, "spool"
        else:
            started_at, ended_at, clock_source = journal_started, journal_ended, "journal"

        duration = None if ended_at is None else (ended_at - started_at).total_seconds()
        box_name = (
            catalog.jobs[job].box.box_name if catalog is not None and job in catalog.jobs else None
        )
        started_by = window.started_by if window is not None else None
        run_dir = dispatch.get("run_dir")
        rows.append(
            RunRow(
                job=job,
                run_number=run_number,
                catalog_hash=revision.catalog_hash,
                started_at=started_at,
                ended_at=ended_at,
                duration_s=duration,
                status=status,  # type: ignore[arg-type]
                exit_code=exit_code,
                started_by=started_by,
                run_dir=run_dir if isinstance(run_dir, str) else None,
                box_name=box_name,
                clock_source=clock_source,  # type: ignore[arg-type]
                job_hash=revision.fingerprints.get(job),
                fidelity="partial" if anchored else fidelity,
                undecided=key in index.undecided_keys,
            )
        )
    return rows


def _leaf_rows(
    records: list[dict[str, Any]],
    revisions: _Revisions,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]],
    fidelity: Literal["full", "records_only"],
    recovered: Sequence[ApplyResult],
    anchor_runs: Mapping[str, int],
) -> list[RunRow]:
    """Every CMD/FW job's run rows, from `dispatch` + `input(kind=STATUS)` records (decision 1),
    with the trace as fallback for a close that produced no STATUS input (decision 2's
    KILLJOB/term_run_time case). Two phases (DL-178w): `_index_leaves` walks the records once;
    `_emit_leaf_rows` is pure."""
    # recovered verdicts are the gate's re-run over the crash window (DL-156); only the
    # rejections join the skip set, and a recovered application decides its row as a durable
    # one does
    rejected = _rejected_attempts(records) | {
        result.index for result in recovered if result.decision == "rejected"
    }
    decided = _decided_attempts(records) if fidelity == "records_only" else frozenset()
    index = _index_leaves(records, rejected, decided, fidelity, trace_windows)
    return _emit_leaf_rows(index, revisions, spool, trace_windows, fidelity, anchor_runs)


def fold_run_rows(
    records: list[dict[str, Any]],
    *,
    catalog: CatalogIR | None = None,
    trace: Sequence[TraceEntry] = (),
    spool: Mapping[tuple[str, int], SpoolRead] | None = None,
    recovered: Sequence[ApplyResult] = (),
    anchor: Mapping[str, JobRuntime] | None = None,
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    catalogs: Mapping[int, CatalogIR] | None = None,
) -> list[RunRow]:
    """The pure fold (DL-113): parsed journal records plus, optionally, a catalog
    (box_name/job_type/SEM-09) and a replayed trace (box run boundaries, started_by, the
    KILLJOB/term_run_time close fallback, decision 2). No filesystem, Oracle or Engine; the same
    inputs always give the same rows.

    Without a catalog there are no box rows, no box_name or `job_hash`, and exit-code verdicts
    use the bare SEM-09 default; every row says so in `fidelity` (decision 5). `read_run_root`
    supplies a catalog whenever the run root holds its bundle.

    `recovered` is the ss4 gate's re-run over the crash window (DL-156): the verdicts
    `replay_trace` recovers for attempts whose decision record was never written. A caller that
    replayed supplies them with `trace` (`read_run_root` does); only recovered rejections join
    the skip set. Without a catalog, supplying them raises, and an undecided completion does
    not decide its row: the row carries `undecided`.

    `anchor` is the job rows of the snapshot a pruned store's replay started from, and
    `history` the run history it carries, with `trace` covering what follows it (decision 6).

    `catalogs` maps each configuration rev the records' segments name to its catalog: a run is
    then read under the rev it ran under (`_Revisions`). Without it every rev reads `catalog`,
    each still labelled with its own segment's hash."""
    if not records or records[0].get("rec") != "segment":
        raise RunHistoryError("run history requires a journal starting with a segment record")
    if catalog is None and recovered:
        # DL-156's grant is asymmetric: recovered verdicts are the gate's re-run under a catalog,
        # and a records-only fold holds no gate, so consuming them here would exercise authority
        # the records-only half refuses
        raise RunHistoryError(
            "recovered verdicts with no catalog: a records-only fold cannot consume"
            " the gate's re-run -- it refuses to decide instead (DL-156)"
        )
    revisions = _Revisions(records, catalog, catalogs)
    first = records[0].get("rev")
    trace_windows = _trace_windows_by_job(
        trace, anchor, _history_windows(history or {}), first if is_wire_int(first) else None
    )
    fidelity: Literal["full", "records_only"] = "records_only" if catalog is None else "full"
    rows: list[RunRow] = []
    if catalog is not None:
        rows.extend(_box_rows(trace, revisions, trace_windows))
    rows.extend(
        _leaf_rows(
            records,
            revisions,
            spool or {},
            trace_windows,
            fidelity,
            recovered,
            {job: row.run_number for job, row in (anchor or {}).items()},
        )
    )
    return rows


# ------------------------------------------------------------- I/O: the spool


def read_spool(
    run_dir: Path, job: str, run_number: int, bound_run_id: str | None = None
) -> SpoolRead | None:
    """One run's spool, validated against (job, run_number) (supervisor-protocol ss3's frozen
    spawn.json/status.json shapes). None when spawn.json is missing, unparseable, or names a
    different run (the rule the restart's step 5 applies, `runner_startup._settle_attempt`).

    `bound_run_id` is the identity the run's durable SPAWN minted (DL-118): a spool record
    naming a different `run_id` is a stranger's and reads as absent, not refused, so one corrupt
    directory costs one row's timings."""
    spawn = load_json(run_dir / "spawn.json")
    if not spool_names_run(spawn, job=job, run_number=run_number, run_id=bound_run_id):
        return None
    assert spawn is not None
    raw_started = spawn.get("started_at")
    if not isinstance(raw_started, str):
        return None
    started_at = _parse_timestamp(raw_started)
    ended_at = None
    status = load_json(run_dir / "status.json")
    if spool_names_run(status, job=job, run_number=run_number, run_id=bound_run_id):
        assert status is not None
        raw_ended = status.get("ended_at")
        if isinstance(raw_ended, str):
            ended_at = _parse_timestamp(raw_ended)
    return SpoolRead(started_at=started_at, ended_at=ended_at)


def _read_all_spool(records: list[dict[str, Any]]) -> dict[tuple[str, int], SpoolRead]:
    """Each run's spool, read from its latest attempt's directory, which its `dispatch` record
    names, held to the id that record bound (DL-118)."""
    spool: dict[tuple[str, int], SpoolRead] = {}
    for record in records:
        if record.get("rec") != "dispatch":
            continue
        job, run_number = str(record["job"]), int(record["run_number"])
        run_dir = record.get("run_dir")
        if not isinstance(run_dir, str):
            continue
        found = read_spool(Path(run_dir), job, run_number, str(record["run_id"]))
        if found is not None:
            spool[(job, run_number)] = found
        else:
            spool.pop((job, run_number), None)
    return spool


def _replay(store: Store, configuration: Configuration, where: str) -> StoreReplay:
    """The store replayed through a fresh Oracle under each configuration it ran, read under
    that configuration's profile, so a replay reads conditions as the engine did (DL-151,
    DL-252); a pruned store from its latest snapshot (decision 6). `configuration` is the one
    the caller folds the rows under, and stands in for a store with no configuration row."""
    try:
        return replay_store(store, where=where, fallback=configuration)
    except (OracleError, EngineError) as exc:
        # a replayed input that raises arrives named, as a ReplayFault (period-model ss11);
        # `runs` refuses it like any other replay fault
        raise RunHistoryError(f"{where}: replay failed ({exc})") from exc


def replay_trace(store: Store, configuration: Configuration, where: str) -> list[TraceEntry]:
    """The trace (decision 2), reconstructed by replaying the store's inputs. A store whose
    oldest records were pruned replays from its latest snapshot: the trace then covers what
    follows it (decision 6)."""
    return _replay(store, configuration, where).oracle.trace()


def read_run_root(run_root: Path) -> list[RunRow]:
    """One run root's history: its store's records folded under the configuration it ran.

    The cost is one replay; `--job` and `--since` filter after the fold."""
    try:
        store = open_reader(run_root)
    except EngineError as exc:
        raise RunHistoryError(str(exc)) from exc
    try:
        meta = store.meta()
        if meta.get("state_version") != str(STATE_MACHINE_VERSION):
            raise RunHistoryError(
                f"{run_root}: state version {meta.get('state_version')}, this build derives"
                f" {STATE_MACHINE_VERSION}: one executable replays only what its own state"
                " machine wrote (DL-424)"
            )
        configuration = Configuration.from_bytes(
            store.get_config(store.current_config_rev()), where=str(run_root)
        )
        records = [w for _, record in store.read_from(0) if (w := wire_record(record)) is not None]
        replayed = _replay(store, configuration, str(run_root))
        if not records or records[0].get("rec") != "segment":
            # the retention pruned the opening: the snapshot's configuration row names it
            opening = replayed.replay.rev if replayed.anchor is None else replayed.anchor.config_rev
            opened = load_configuration(store, int(opening or 1), where=str(run_root))
            records.insert(
                0, {"rec": "segment", "rev": opening, "catalog_hash": opened.catalog_hash}
            )
        # every rev a segment opens, and every rev a retained run of the snapshot names
        revs = {
            int(record["rev"])
            for record in records
            if record.get("rec") == "segment" and is_wire_int(record.get("rev"))
        }
        if replayed.anchor is not None:
            revs |= {
                int(run["rev"])
                for runs in replayed.anchor.runs.values()
                for run in runs
                if is_wire_int(run.get("rev"))
            }
        catalogs = {
            rev: load_configuration(store, rev, where=str(run_root)).catalog for rev in revs
        }
    except (EngineError, ValueError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    finally:
        store.close()
    return fold_run_rows(
        records,
        catalog=configuration.catalog,
        trace=replayed.oracle.trace(),
        spool=_read_all_spool(records),
        anchor=None if replayed.anchor is None else replayed.anchor.rows.jobs,
        history=None if replayed.anchor is None else replayed.anchor.runs,
        catalogs=catalogs,
    )


def read_run_roots(
    run_roots: Sequence[Path], *, job: str | None = None, since: datetime | None = None
) -> list[RunRow]:
    """Every named run root's rows, combined and filtered (`--job`, `--since`), sorted by
    (job, started_at): the shape the CLI prints and a segmentation break is computed over
    (decision 4)."""
    rows: list[RunRow] = []
    for run_root in run_roots:
        rows.extend(read_run_root(run_root))
    if job is not None:
        rows = [row for row in rows if row.job == job]
    if since is not None:
        rows = [row for row in rows if row.started_at >= since]
    return sorted(rows, key=lambda row: (row.job, row.started_at))
