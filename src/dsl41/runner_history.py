"""Run history (DL-113): a projection, not a new record kind.

`docs/runner-design.md` ss7 lists every record the journal writes; this module adds none. It
folds `dispatch`, the `input` records the ss4 stale-completion gate journals, each `decision`
(its verdict on a completion and the effects nested in it), and the run root's manifest and
spool into one row per job run. It is offline: no engine, no control socket, no wire verb.

Two layers: `fold_run_rows` is a pure function of parsed journal records plus an optional
catalog and trace. `read_run_root` is the I/O shell: for every WAL segment the root retains (one
per period, I1) it rebuilds that period's catalog from its own stored inputs (DL-130's
content-addressed bundle, so no estate-file argument), replays it through a fresh `Oracle` as
`dsl41 journal` does, and reads the spool. `read_spool` is a separate function so the WAL and
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
   whose payload has no `ended_at`: `_inject_completion` and `resolve_spool` in
   `runner_startup.py` write it only when status.json supplied a true one; E7
   (`exit_status_unobservable`) and the "wrapper lost; killed at resume" TERMINATED case omit
   it because the process's real end is unknown and the event's own `at` is only when the
   engine noticed. Both rows keep their recorded status and a null duration.

4. **Segmentation follows the job, not the estate.** Every row carries the `catalog_hash` of its
   segment and this job's own `job_hash`, in every `--format`. The break line `--format table`
   prints between two consecutive rows of one job fires on `job_hash`, falling back to
   `catalog_hash` only when either row has none. The estate hash moves for every job when any
   job changes (`period.catalog_hash_v2`), so a break on it would mark the whole estate.
   `period.job_fingerprints` states what the per-job hash can say: it fingerprints the resolved
   definition, so an estate whose placeholders vary per run gets no more than `catalog_hash`
   gave it. Rows sort by (job, started_at), so a break lands where that job changed.

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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.ast_jil import JilParseError, parse
from dsl41.boundary import read_seal
from dsl41.canon import is_wire_int, naive_utc
from dsl41.ir import CatalogIR, LoweringError, Semantics, lower_catalog
from dsl41.oracle import Oracle
from dsl41.oracle_state import LIVE, TERMINAL, CarriedRows, JobStatus, OracleError, TraceEntry
from dsl41.period import (
    Manifest,
    RuntimeProfile,
    bundle_dir,
    bundle_source_paths,
    disagreements,
    is_opening,
    job_fingerprints,
    read_period_manifest,
    oracle_reading,
    check_manifest_against_segment,
    SEGMENT_FIELDS,
)
from dsl41.runner_adapters import load_json, spool_names_run
from dsl41.runner_admission import ApplyResult
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import ReplayFault, decision_effects, read_journal, replay_period
from dsl41.runner_ledger import check_state_machine_version


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
    status: JobStatus
    exit_code: int | None = None
    started_by: str | None = None
    executor_id: str | None = None
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
    #: It rides on the row so a JSON or CSV consumer sees it.
    fidelity: Literal["full", "records_only"] = "full"
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
    a job opens it, the next terminal transition closes it (None if none shows)."""

    run_number: int
    started_at: datetime
    started_by: str
    ended_at: datetime | None
    status: str | None


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


def _windows_from_entries(entries: Sequence[TraceEntry], first: int = 0) -> list[_TraceWindow]:
    """One job's trace entries, filtered to that job and in trace order, folded into run windows
    (decision 2). Out-of-band markers (`SCHED_DISARM`, `START_REFUSED`, ...) carry no "->" and are
    skipped.

    `first` is the run number the job had reached before the segment, so the first window is
    `first + 1`. Run numbers are monotone across the estate, not across a segment (I2). Zero
    covers period 1 and a segment whose opening seal this root does not hold."""
    windows: list[_TraceWindow] = []
    run_number = first
    started_at: datetime | None = None
    started_by = ""
    ended_at: datetime | None = None
    status: str | None = None
    for entry in entries:
        old, sep, new = entry.transition.partition("->")
        if not sep:
            continue
        if new == "STARTING":
            if started_at is not None:
                windows.append(_TraceWindow(run_number, started_at, started_by, ended_at, status))
            run_number += 1
            started_at, started_by, ended_at, status = entry.at, entry.cause, None, None
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
        windows.append(_TraceWindow(run_number, started_at, started_by, ended_at, status))
    return windows


def _trace_windows_by_job(
    trace: Sequence[TraceEntry], carried: Mapping[str, int] | None = None
) -> dict[str, dict[int, _TraceWindow]]:
    """job -> {run_number: window}, keyed by run number because `carried` breaks the match with
    position."""
    by_job: dict[str, list[TraceEntry]] = {}
    for entry in trace:
        by_job.setdefault(entry.job, []).append(entry)
    return {
        job: {
            window.run_number: window
            for window in _windows_from_entries(entries, (carried or {}).get(job, 0))
        }
        for job, entries in by_job.items()
    }


def _box_rows(
    trace: Sequence[TraceEntry],
    catalog: CatalogIR,
    catalog_hash: str,
    fingerprints: Mapping[str, str],
    windows_by_job: Mapping[str, Mapping[int, _TraceWindow]],
) -> list[RunRow]:
    """Every BOX job's run rows, entirely from the trace (decision 2); a box has no dispatch record
    and no spool. The windows are the caller's, so a box and a leaf in one fold are numbered
    from the same carried state."""
    rows: list[RunRow] = []
    for job, job_ir in catalog.jobs.items():
        if job_ir.job_type != "BOX":
            continue
        for window in windows_by_job.get(job, {}).values():
            duration = (
                None
                if window.ended_at is None
                else (window.ended_at - window.started_at).total_seconds()
            )
            rows.append(
                RunRow(
                    job=job,
                    run_number=window.run_number,
                    catalog_hash=catalog_hash,
                    started_at=window.started_at,
                    ended_at=window.ended_at,
                    duration_s=duration,
                    status=window.status or "RUNNING",  # type: ignore[arg-type]
                    exit_code=None,
                    started_by=window.started_by,
                    executor_id=None,
                    run_dir=None,
                    box_name=job_ir.box.box_name,
                    clock_source="journal",
                    job_hash=fingerprints.get(job),
                    fidelity="full",
                )
            )
    return rows


def _journal_ended_at(completion: dict[str, Any] | None) -> datetime | None:
    """The terminal STATUS input's truthful end time, or None (decision 3): no completion, or a
    reconcile completion whose payload has no `ended_at` (E7 / "wrapper lost; killed at
    resume"; `_inject_completion` writes the key only when `resolve_spool` returned a real
    one)."""
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
    checks below and its completion decides the row (decision 5's degrade posture); a malformed
    decision is already refused by `decision_effects` when it carries effects. A malformed
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
    executor_by_key: dict[tuple[str, int], str]


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
    executor_by_key: dict[tuple[str, int], str] = {}

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
                # TERMINATES it during its STARTING transition and its SPAWN retires). A later
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
        elif rec == "decision":
            # the executor a run was bound to rides on the SPAWN effect inside the decision that
            # planned it (DL-118); the shared decoder refuses a malformed effect (DL-139)
            for effect in decision_effects(record):
                if effect.kind == "SPAWN":
                    executor_by_key[(effect.job, effect.run_number)] = effect.executor_id

    return _LeafIndex(
        dispatch_by_key=dispatch_by_key,
        order=order,
        completion_by_key=completion_by_key,
        undecided_keys=undecided_keys,
        executor_by_key=executor_by_key,
    )


def _emit_leaf_rows(
    index: _LeafIndex,
    catalog: CatalogIR | None,
    catalog_hash: str,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]],
    fingerprints: Mapping[str, str],
    fidelity: Literal["full", "records_only"],
) -> list[RunRow]:
    """Emit phase (D3): one `RunRow` per dispatched (job, run_number), in dispatch order, as a
    pure function of the leaf index."""
    rows: list[RunRow] = []
    for key in index.order:
        job, run_number = key
        dispatch = index.dispatch_by_key[key]
        completion = index.completion_by_key.get(key)
        # by number, not position: a run carried across a boundary has a run number the
        # segment's window list does not index (I2)
        window = trace_windows.get(job, {}).get(run_number)

        journal_started = _parse_timestamp(str(dispatch["started_at"]))
        journal_ended = _journal_ended_at(completion)
        if completion is not None:
            status, exit_code = _leaf_status(job, catalog, completion)
        elif window is not None and window.status is not None:
            status, exit_code = window.status, None
            journal_ended = window.ended_at
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
                catalog_hash=catalog_hash,
                started_at=started_at,
                ended_at=ended_at,
                duration_s=duration,
                status=status,  # type: ignore[arg-type]
                exit_code=exit_code,
                started_by=started_by,
                executor_id=index.executor_by_key.get(key),
                run_dir=run_dir if isinstance(run_dir, str) else None,
                box_name=box_name,
                clock_source=clock_source,  # type: ignore[arg-type]
                job_hash=fingerprints.get(job),
                fidelity=fidelity,
                undecided=key in index.undecided_keys,
            )
        )
    return rows


def _leaf_rows(
    records: list[dict[str, Any]],
    catalog: CatalogIR | None,
    catalog_hash: str,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]],
    fingerprints: Mapping[str, str],
    fidelity: Literal["full", "records_only"],
    recovered: Sequence[ApplyResult],
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
    return _emit_leaf_rows(
        index, catalog, catalog_hash, spool, trace_windows, fingerprints, fidelity
    )


def fold_run_rows(
    records: list[dict[str, Any]],
    *,
    catalog: CatalogIR | None = None,
    trace: Sequence[TraceEntry] = (),
    spool: Mapping[tuple[str, int], SpoolRead] | None = None,
    carried: Mapping[str, int] | None = None,
    recovered: Sequence[ApplyResult] = (),
) -> list[RunRow]:
    """The pure fold (DL-113): parsed journal records plus, optionally, a catalog
    (box_name/job_type/SEM-09) and a replayed trace (box run boundaries, started_by, the
    KILLJOB/term_run_time close fallback, decision 2). No filesystem, Oracle or Engine; the same
    inputs always give the same rows.

    Without a catalog there are no box rows, no box_name or `job_hash`, and exit-code verdicts
    use the bare SEM-09 default; every row says so in `fidelity` (decision 5). `read_run_root`
    supplies a catalog whenever the run root holds its bundle.

    `carried` is job -> the run number this period opened with, so a run that crosses a boundary
    keeps one identity (I2). Absent (period 1, or no seal to read it from), numbering starts
    at 1.

    `recovered` is the ss4 gate's re-run over the crash window (DL-156): the verdicts
    `replay_trace` recovers for attempts whose decision record was never written. A caller that
    replayed supplies them with `trace` (`read_run_root` does); only recovered rejections join
    the skip set. Without a catalog, supplying them raises, and an undecided completion does
    not decide its row: the row carries `undecided`."""
    if not records or not is_opening(records[0]):
        raise RunHistoryError("run history requires a journal starting with a segment record")
    if catalog is None and recovered:
        # DL-156's grant is asymmetric: recovered verdicts are the gate's re-run under a catalog,
        # and a records-only fold holds no gate, so consuming them here would exercise authority
        # the records-only half refuses
        raise RunHistoryError(
            "recovered verdicts with no catalog: a records-only fold cannot consume"
            " the gate's re-run -- it refuses to decide instead (DL-156)"
        )
    catalog_hash = str(records[0]["catalog_hash"])
    trace_windows = _trace_windows_by_job(trace, carried)
    fingerprints = {} if catalog is None else job_fingerprints(catalog)
    fidelity: Literal["full", "records_only"] = "records_only" if catalog is None else "full"
    rows: list[RunRow] = []
    if catalog is not None:
        rows.extend(_box_rows(trace, catalog, catalog_hash, fingerprints, trace_windows))
    rows.extend(
        _leaf_rows(
            records,
            catalog,
            catalog_hash,
            spool or {},
            trace_windows,
            fingerprints,
            fidelity,
            recovered,
        )
    )
    return rows


# ------------------------------------------------------------- I/O: the spool


def read_spool(
    run_dir: Path, job: str, run_number: int, bound_run_id: str | None = None
) -> SpoolRead | None:
    """One run's spool, validated against (job, run_number) (supervisor-protocol ss3's frozen
    spawn.json/status.json shapes). None when spawn.json is missing, unparseable, or names a
    different run (the rule `runner_adapters.resolve_spool` applies live).

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


def _bound_run_ids(records: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """(job, run_number) -> the run_id its durable SPAWN bound, from the decisions in the log
    (DL-118), through the shared decoder (DL-139), which refuses a SPAWN with no run_id."""
    bound: dict[tuple[str, int], str] = {}
    for record in records:
        if record.get("rec") != "decision":
            continue
        for effect in decision_effects(record):
            if effect.kind == "SPAWN" and effect.run_id is not None:
                bound[(effect.job, effect.run_number)] = effect.run_id
    return bound


def _read_all_spool(records: list[dict[str, Any]]) -> dict[tuple[str, int], SpoolRead]:
    bound = _bound_run_ids(records)
    spool: dict[tuple[str, int], SpoolRead] = {}
    for record in records:
        if record.get("rec") != "dispatch":
            continue
        job, run_number = str(record["job"]), int(record["run_number"])
        run_dir = record.get("run_dir")
        if not isinstance(run_dir, str):
            continue
        found = read_spool(Path(run_dir), job, run_number, bound.get((job, run_number)))
        if found is not None:
            spool[(job, run_number)] = found
    return spool


# --------------------------------------------------------- I/O: the catalog


def load_catalog_from_manifest(run_root: Path, period_id: int | None = None) -> CatalogIR:
    """Rebuild the catalog `dsl41 run` loaded, from the run root's own artifact; no estate-file
    argument, unlike `dsl41 journal`.

    A root keeps its inputs in `catalogs/<source_bundle_hash>/`, addressed by their bytes
    (DL-138), and names the address in `periods/<id>/manifest.json`; the bundle holds the
    byte-exact post-placeholder JIL (render_preserve, F1) in command-line order.

    The reload is not hash-gated as `dsl41 journal` is: `SourceSpan.file` names the stored path,
    so its `catalog_hash` cannot equal the journal's (runner-design ss7). The offline check is
    that the manifest's own recorded hash, computed from the original catalog object, agrees
    with the opening record; it catches a manifest from a different run.

    `period_id` names which period's catalog to rebuild; omitted, the active one. Each period
    has its own bundle, and the active period's would misreport a closed period's runs
    (period-model ss1.1)."""
    paths = stored_input_paths(run_root, period_id)
    if not paths:
        raise RunHistoryError(f"{run_root}: no stored inputs to rebuild the catalog from")
    parsed = []
    try:
        for path in paths:
            parsed.append(parse(path.read_text(encoding="utf-8"), file=str(path)))
        return lower_catalog(parsed, permit_unknown=True)
    except (OSError, UnicodeDecodeError, JilParseError, LoweringError) as exc:
        raise RunHistoryError(f"{run_root}: cannot rebuild the catalog ({exc})") from exc


def active_period_id(run_root: Path) -> int:
    """Which period this root's active segment holds (period-model I1).

    1 on a root that has never sealed. Artifacts under `periods/` are addressed by period
    number, so defaulting to 1 after a boundary would pair period 1's manifest with period N's
    records and refuse the root."""
    try:
        opening = read_journal(run_root)[0]
    except (OSError, EngineError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    # `read_journal` has run ss2.1's schema over the opening, so the field is present and an
    # int; a fallback would be a second authority (DL-138)
    return int(opening["period_id"])


#: Retired estate layouts: the marker FILE that identifies one, and the entry that retired it.
#: Append-only (docs/protocol-evolution.md ss6).
RETIRED_LAYOUTS: Final[tuple[tuple[str, str], ...]] = (("manifest/manifest.json", "DL-138"),)


def _refuse_retired_layout(run_root: Path) -> None:
    """D9, DL-138: tell a root with no period manifest which state it is in, by the FILE.

    `<run-root>/manifest/manifest.json` is DL-66's layout and refuses by name. A `manifest/`
    directory without that file is unknown residue and refuses generically. A root with neither
    has no manifest, and the caller degrades (decision 5)."""
    for marker, retiring in RETIRED_LAYOUTS:
        if (run_root / marker).exists():
            raise RunHistoryError(
                f"{run_root}: holds `{marker}` and no periods/<id>/manifest.json --"
                f" the `manifest/` run-root layout is RETIRED and refused by name"
                f" since {retiring} (docs/protocol-evolution.md ss6, ss8)"
            )
    if (run_root / "manifest").is_dir():
        raise RunHistoryError(
            f"{run_root}: holds a `manifest/` directory with no manifest.json inside"
            " and no periods/<id>/manifest.json -- nothing here names this root's"
            " inputs, and history does not guess (period-model ss1.1)"
        )


def _period_manifest_or_refuse(run_root: Path, period_id: int | None = None) -> "Manifest | None":
    """`read_period_manifest` with its refusal converted to `RunHistoryError`, and D9's layout
    tombstone where it returns None. Every door into run history answers `RunHistoryError`, so
    a multi-root `dsl41 runs` prints exit 2 and not a traceback."""
    try:
        manifest = read_period_manifest(
            run_root, active_period_id(run_root) if period_id is None else period_id
        )
    except EngineError as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    if manifest is None:
        _refuse_retired_layout(run_root)
    return manifest


def stored_input_paths(run_root: Path, period_id: int | None = None) -> list[Path]:
    """The stored inputs in command-line order (DL-130's bundle), or `[]` when this root stores
    none (nothing staged, or retention pruned them). Empty is a missing fact and degrades (decision
    5); a present, unreadable bundle is corruption and raises. `period_id` names which period's
    inputs; omitted, the active one."""
    manifest = _period_manifest_or_refuse(run_root, period_id)
    if manifest is None:
        return []
    directory = bundle_dir(run_root, manifest.source_bundle_hash)
    if not (directory / "sources.json").exists():
        return []
    try:
        return bundle_source_paths(run_root, manifest.source_bundle_hash)
    except EngineError as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc


def check_replay_version(opening: Mapping[str, Any], *, where: str = "") -> None:
    """period-model ss2.1's other half: a build replays only a log its own state machine wrote,
    so a foreign log never narrates transitions this build's semantics invented.

    A wrapper over `runner_ledger.check_state_machine_version`, mode="replay" (an absent field
    refuses in both halves, DL-189). It converts the `EngineError` to `RunHistoryError`, the
    type this module's contract promises; the message crosses unchanged.

    Two entry points reach it and one nests inside the other. `_read_segment` calls it directly,
    after `prove_opening` (identity before semantics), so the two degraded paths that fold
    records with no replay refuse too: a foreign version is a wrong fact, and decision 5 degrades
    only on a missing one. `replay_trace` asks again at its own entry because it is callable
    alone. The `dsl41 journal` lineage walk calls it beside the other opening checks, before a
    crossing is proved. No well-formed log can trip it while one version exists; a tampered
    segment can."""
    try:
        check_state_machine_version(opening, mode="replay", where=where)
    except EngineError as exc:
        raise RunHistoryError(str(exc)) from exc


def pinned_profile(run_root: Path, opening: Mapping[str, Any]) -> "RuntimeProfile":
    """The runtime profile an opening segment's period was pinned with, from a manifest bound to
    that segment (ss2.1, PR-22).

    Required, not degraded: the profile carries the semantic switches and the alias table the
    engine read conditions under (DL-252), and a replay under defaults narrates a run the engine
    may never have made. A missing or foreign manifest refuses."""
    try:
        manifest = read_period_manifest(run_root, int(opening["period_id"]))
        if manifest is None:
            raise EngineError(
                f"{run_root}: periods/{int(opening['period_id']):06d}/manifest.json is not"
                " there -- a replay reads the period's semantic switches and alias table"
                " from it, and without it would narrate readings the engine may not have"
                " used (period-model ss2.1, DL-252)"
            )
        check_manifest_against_segment(manifest, opening)
    except EngineError as exc:
        raise RunHistoryError(str(exc)) from exc
    return manifest.runtime_profile


@dataclass(frozen=True)
class SegmentReplay:
    """What one segment's offline replay yields (DL-156): the trace (decision 2) and the
    verdicts the ss4 gate re-derived for attempts with no decision record
    (`Replay.recovered`). They are the engine's own gate over its own records, behind
    `check_replay_version`, and a resume derives the same ones; taking them adds no fold
    authority."""

    trace: list[TraceEntry]
    recovered: list[ApplyResult]


def replay_trace(
    run_root: Path,
    records: list[dict[str, Any]],
    catalog: CatalogIR,
    carried: "CarriedRows | None" = None,
) -> SegmentReplay:
    """The trace (decision 2), reconstructed as `audit` does: an Oracle seeded with the rows this
    period opened with, this engine's executor seeded, and the log replayed by `replay_period`
    (S6a routing reads the seed). The recovered crash-window verdicts ride back beside it
    (DL-156).

    A foreign `state_machine_version` refuses before anything is replayed
    (`check_replay_version`).

    `carried` is None for period 1 and for a period whose opening seal this root does not hold.
    Elsewhere it is required: revisions and run numbers are monotone across the estate (I2),
    and an empty oracle derives numbers the log never recorded, so `replay_inputs` refuses at
    the first admitted input that touches a carried entity (DL-136)."""
    check_replay_version(records[0], where=str(run_root))
    # SEM-35: this period's alias table, read from its pin; without it a replay refuses a log
    # the engine wrote (DL-151). A missing or foreign manifest refuses. The semantic switches
    # come from the same pin (DL-252): other switches would narrate conditions the engine never
    # read
    profile = pinned_profile(run_root, records[0])
    oracle = Oracle(catalog, carried=carried, **oracle_reading(profile))
    try:
        replay = replay_period(oracle, records)
    except (OracleError, ReplayFault) as exc:
        # a replayed input that raises arrives named, as a ReplayFault (period-model ss11);
        # `runs` refuses it like any other replay fault
        raise RunHistoryError(f"{run_root}: replay failed ({exc})") from exc
    return SegmentReplay(trace=oracle.trace(), recovered=replay.recovered)


def read_run_root(run_root: Path) -> list[RunRow]:
    """One run root's whole retained history: every segment it holds, each folded under its own
    period's catalog, concatenated (PR-50).

    A period is a baseline, and this is the fold `read_run_roots` performs per root. After a
    seal (DL-133 made the WAL many files) the rows of earlier periods live in segments the
    active one does not hold (DL-135 closed the same gap for the subscriber's backfill).

    Each period folds on its own manifest, bundle and replay, so a run is reported under the
    catalog it started in (`start_period`). State continues across a boundary: the replay is
    seeded from the rows the period opened with (`_opening_rows`), so revisions and run numbers
    continue and a box that ran in two periods gets runs 1 and 2.

    Two limits. A run that spans a boundary keeps its row in the period that dispatched it, with
    its spool timings, and its STATUS stays RUNNING: the terminal input is in the next segment.
    And the cost is one replay per retained period; `--job` and `--since` filter after the
    fold."""
    from dsl41.period import wal_segments
    from dsl41.runner_journal import read_backfill

    if not wal_segments(run_root) and _archived_here(run_root):
        # every period this root held was archived (DL-144): no rows and no fault; the caller
        # names the missing coverage, since a backfill over an empty segment list would raise.
        # `_archived_here` proves each receipt, so an unreadable one never becomes an empty table
        return []
    try:
        # one validated read for the whole estate: `read_backfill` holds the chain proofs
        # (DL-135): sentinel-bound openings, filename vs record, contiguous retained numbers,
        # each segment opening from the seal that closes the one before it, one estate, a
        # continuous index frontier. A spliced foreign segment or a missing middle refuses here
        stream = read_backfill(run_root / "journal.jsonl", since=0).records
    except (OSError, EngineError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    rows: list[RunRow] = []
    for records in _split_segments(stream):
        try:
            rows.extend(_read_segment(run_root, records))
        except EngineError as exc:
            # the shared `decision_effects` refusal arrives as itself (DL-139); the message
            # names the root, since `dsl41 runs` reads several roots in one command
            raise RunHistoryError(f"{run_root}: {exc}") from exc
    return rows


def _split_segments(stream: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The validated whole-estate stream, split into its segments at each opening record."""
    out: list[list[dict[str, Any]]] = []
    for record in stream:
        if is_opening(record) or not out:
            out.append([])
        out[-1].append(record)
    return out


def _read_segment(run_root: Path, records: list[dict[str, Any]]) -> list[RunRow]:
    """The I/O shell for one period: its chain-validated records, manifest and the spool of the
    runs it dispatched, folded into run rows by the pure `fold_run_rows`."""
    if not records or not is_opening(records[0]):
        raise RunHistoryError(
            f"{run_root}: run history requires a journal starting with a segment record"
        )
    period_id = records[0]["period_id"]
    prove_opening(run_root, records[0])
    # after the opening proof and before the manifest: identity first, then semantics, so a
    # segment this root cannot prove is reported as that. Here and not only in `replay_trace`, so
    # the two degraded paths below, which replay nothing, refuse too
    check_replay_version(records[0], where=str(run_root))
    spool = _read_all_spool(records)
    period_manifest = _period_manifest_or_refuse(run_root, period_id)
    if period_manifest is None:
        # a missing manifest degrades, a wrong one refuses, and a retired layout is named
        # (`_period_manifest_or_refuse`). Every degraded row says `records_only`, so what is
        # missing rides on the data (decision 5)
        return fold_run_rows(records, spool=spool)
    # PR-22: the manifest and the segment are one object written twice; a self-consistent
    # replacement that shares catalog_hash but not baseline_id or runtime_hash is foreign, so
    # the full agreement is checked
    try:
        check_manifest_against_segment(period_manifest, records[0])
    except EngineError as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    opened = _opening_rows(run_root, records[0], period_manifest)
    carried = (
        {}
        if opened is None
        else {job: row.run_number for job, row in opened.jobs.items() if row.run_number > 0}
    )
    if not stored_input_paths(run_root, period_id):
        # a manifest that names inputs this root does not hold is a missing fact: the rows
        # come back `records_only`
        return fold_run_rows(records, spool=spool, carried=carried)
    catalog = load_catalog_from_manifest(run_root, period_id)
    replayed = replay_trace(run_root, records, catalog, opened)
    return fold_run_rows(
        records,
        catalog=catalog,
        trace=replayed.trace,
        spool=spool,
        carried=carried,
        recovered=replayed.recovered,
    )


def prove_opening(run_root: Path, opening: Mapping[str, Any]) -> None:
    """A period whose opening names a seal must hold that sidecar, and the sidecar must be the
    seal this opening stands on (period-model ss11).

    The ss11 ladder, offline: the digest the naming record carries, the period and estate the
    sidecar claims, the instant it closed, and every `next_period` field the opening `segment`
    repeats. One implementation for every offline reader (DL-139): run history calls it before
    any degradation branch, and `dsl41 journal`'s cross-period replay calls it at every boundary
    it crosses, because a replay that narrated a forged continuation is worse than a refusal.

    Public and reader-neutral: the refusal names the root and the record, not the tool."""
    link = opening.get("opens_from_seal")
    if not isinstance(link, Mapping):
        return  # period 1: opened from nothing
    period_id = link.get("period_id")
    digest = link.get("digest")
    try:
        seal = read_seal(run_root, int(period_id))  # type: ignore[arg-type]
    except (OSError, EngineError, ValueError, TypeError) as exc:
        raise RunHistoryError(
            f"{run_root}: this period opens from seal {digest!r} of period"
            f" {period_id!r} and that sidecar cannot be read ({exc}) -- an"
            " unproved opening (period-model ss11, PR-50)"
        ) from exc
    staged = seal.next_period
    # the projection is derived: every `next_period` field that is also a segment field is
    # compared, so a field added to either model is covered by default
    shared = sorted(set(type(staged).model_fields) & SEGMENT_FIELDS)
    grafted = (
        seal.digest != digest
        or seal.period_id != period_id
        or seal.estate_id != opening.get("estate_id")
        or seal.closed_at.isoformat() != opening.get("at")
        or bool(disagreements(staged, opening, shared))
    )
    if grafted:
        # the digest binds the file's bytes, not its place in this lineage: a valid sidecar from
        # another period or estate with the link rewritten to its digest would pass. The seal
        # must also be the named period's, this estate's, and the one whose next_period is this
        # opening
        raise RunHistoryError(
            f"{run_root}: the sidecar at period {period_id!r} is not the seal this"
            f" opening stands on (seal: period {seal.period_id}, estate"
            f" {seal.estate_id}, opens {seal.next_period.period_id}) -- an identity"
            " graft, an unproved opening (period-model ss11, PR-50)"
        )


def _opening_rows(
    run_root: Path, opening: Mapping[str, Any], manifest: "Manifest | None"
) -> "CarriedRows | None":
    """The rows this period opened with (`attest.carried_from_opening`, the audit's source).

    None means the period opened from nothing: period 1, or a root with no period manifest. A
    period whose opening names a seal and whose sidecar is absent, unreadable or not the named
    one refuses: swallowing that failure would let a period 2 that runs only C2-added jobs
    replay from an empty state and return full-fidelity history from an unproven opening."""
    from dsl41.attest import carried_from_opening

    if opening.get("opens_from_seal") is None:
        return None
    if manifest is None:
        return None  # pre-DL-130 layout: `records_only` fidelity downstream
    try:
        return carried_from_opening(run_root, opening, manifest)
    except (OSError, EngineError, ValueError, KeyError) as exc:
        raise RunHistoryError(
            f"{run_root}: this period opens from a seal this root cannot prove"
            f" ({exc}) -- history refuses an unproved opening rather than replaying"
            " it from an empty state (period-model ss11, PR-50)"
        ) from exc


def _archived_here(run_root: Path) -> list[int]:
    """Every period of this root whose archive receipt proves out, in order (DL-144).

    `period.archived_periods` lists the receipt files. Decisions read this instead: an
    unreadable or unbound receipt never buys a shorter answer, and it refuses as
    `RunHistoryError` so `dsl41 runs` names it."""
    from dsl41.attest import verify_archive_receipt
    from dsl41.period import archived_periods

    out: list[int] = []
    for period_id in archived_periods(run_root):
        try:
            proved = verify_archive_receipt(run_root, period_id)
        except EngineError as exc:
            raise RunHistoryError(f"{run_root}: {exc}") from exc
        if proved is not None:
            out.append(period_id)
    return out


def archived_coverage(run_roots: Sequence[Path]) -> list[str]:
    """One line per period whose inputs were archived, naming the coverage this history lacks
    (period-model ss12, DL-144).

    An archived period's WAL contributes no rows, and a shorter answer is never silent, so each
    missing period is named with the receipt that says why."""
    from dsl41.period import archive_receipt_path, wal_path

    return [
        f"{run_root}: period {period_id} has no rows -- its inputs were archived"
        f" ({archive_receipt_path(run_root, period_id).name}), so this history covers"
        " every period this estate retains and not that one"
        for run_root in run_roots
        for period_id in _archived_here(run_root)
        # the receipt says archived; the segment says whether the rows are gone. Between a
        # durable receipt and the deletion the fold still produces this period's rows, and
        # claiming otherwise would misreport them (DL-144)
        if not wal_path(run_root, period_id).is_file()
    ]


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
