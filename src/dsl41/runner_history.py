"""Run history (DL-113): a projection, not a new record kind.

`docs/runner-design.md` ss7 lists every record the journal ever writes; this
module invents none of them. It reads what is already there -- `dispatch`,
the `input` records the ss4 stale-completion gate journals, each `decision`
(its verdict on a completion and the effects nested in it), and the run
root's manifest/spool -- and folds them into one row per job run: "how long
did it take, run after run, and did it change." Offline only: no engine, no
control socket, no new verb on the wire.

Two layers, deliberately split so the fold stays testable with no
filesystem: `fold_run_rows` is a pure function of already-parsed journal
records plus (optionally) a catalog and a trace -- both themselves plain
data, never a live `Engine`. `read_run_root` is the thin I/O shell: for
EVERY WAL segment the root still retains -- one per period (I1) -- it
rebuilds that period's catalog from its own stored inputs (no estate-file
argument needed, unlike `dsl41 journal`: DL-130's content-addressed bundle),
replays it through a fresh `Oracle` exactly as `dsl41 journal` does, and
reads the spool. `read_spool` is its own thin function for the
same reason: a duration table has two independent I/O concerns (the WAL,
the spool), and mixing them into one function would make the fold hard to
test without both.

Five decisions, each with its reason here; the decision itself is
`docs/decision-log.md` DL-113.

1. **Which clock a run's timing uses.** `spawn.json`/`status.json` are
   process truth; the journal's `dispatch` record plus the terminal
   `STATUS` input are semantic truth. The row carries
   BOTH-or-neither: `clock_source` is "spool" only when spawn.json names
   this exact (job, run_number) and, for a run the journal shows complete,
   status.json also does -- never `started_at` from one and `ended_at` from
   the other. A row that silently mixed clocks would be the same error
   class as a series that silently crosses a definition change (decision 4).
   For a run still open (no journal-visible completion yet), a valid
   spawn.json alone is enough to prefer spool's `started_at` -- there is no
   `ended_at` to mix it with.

2. **Boxes get rows.** A box never gets a `dispatch` record (runner-design
   ss4's dispatch table: "anything on a BOX: none, folds are oracle logic")
   and its fold is emitted, never journaled (ss7's inputs-only principle) --
   so there is no simpler "journal clock" for a box than replaying the
   journal that defines it. A box row is therefore built entirely from the
   replayed trace: the Nth `*->STARTING` transition for that job opens run
   N, the next terminal transition closes it (SEM-01 latching applies here
   too -- a later CHANGE_STATUS overwrites the close already recorded), and
   `clock_source` is always "journal". This is also where `started_by`
   comes from for every row, box or leaf: it is the cause the STARTING
   transition carries, which a `dispatch` record does not.

   The same trace fallback also catches the leaf-job completions that
   never produce a `STATUS` input at all: KILLJOB and a term_run_time
   auto-TERMINATE are decided by the oracle synchronously, while
   processing the KILLJOB/timer input itself (`oracle.py` ``_terminate``),
   so there is no separate adapter completion to match by run_number.
   Reading only `dispatch` + `input(kind=STATUS)` records would silently
   report such a run as still RUNNING; the trace shows what actually
   closed it. This is the one place the design note this
   was built from was wrong: it assumed a leaf run's close always reaches
   the log as a STATUS record. The journal alone does not always carry the
   close, and the fix is the same replay boxes already need, not a new
   record.

3. **Incomplete runs never get a fabricated duration.** A run with no
   journal-visible close (RUNNING at the end of the journal, and no trace
   close either) gets `ended_at: None, duration_s: None`. So does a
   `STATUS` input from `source="reconcile"` whose payload carries no
   `ended_at` -- verified against `runner_startup.py`'s `_inject_completion`
   and `resolve_spool`: `ended_at` rides in the payload only when
   status.json supplied a true one; E7 (`exit_status_unobservable`) and the
   "wrapper lost; killed at resume" TERMINATED case both return `None` for
   it, because in both the process's real end is genuinely unknown -- the
   event's own `at` is only when the ENGINE noticed, at resume, which can
   be arbitrarily later. Both get a row with their real recorded status
   and a null duration, never the resume instant dressed up as an end time.

4. **Segmentation, on the job and not on the estate.** Every row carries
   both the `catalog_hash` of the segment it came from and this job's
   own `job_hash`, always, in every `--format`. The break line `--format
   table` prints between two consecutive rows of the same job fires on
   `job_hash`, falling back to `catalog_hash` only when either row has none.
   The estate hash alone is the wrong signal to draw it from: it is
   deliberately conservative (`period.catalog_hash_v2` -- "an estate
   that changed in ANY way re-baselines"), so a release touching twelve jobs
   of eight hundred moves it for all eight hundred, and a break on it marks
   every job in the estate as changed. See `period.job_fingerprints` for what the
   per-job hash can and cannot say -- in particular that it fingerprints the
   RESOLVED definition, so an estate whose placeholders vary per run gets no
   more than `catalog_hash` already gave it. Rows are sorted by
   (job, started_at) first, so a break lands exactly where that job changed
   -- never a hidden line, never a refusal to print.

5. **A missing manifest degrades; a wrong one refuses; a RETIRED layout
   is named.** A root whose retention pruned its manifest is exactly the
   root a history tool exists to read, so an absent manifest folds from
   records alone rather than refusing the whole root, while a manifest
   whose `catalog_hash` disagrees with the journal's opening record still
   refuses, because that one is not a missing fact but a wrong one. What
   the degraded path costs is real and is carried per row in `fidelity`
   rather than in a warning line a JSON or
   CSV consumer never sees: no box rows at all, no `box_name`, no
   `started_by`, no `job_hash`, bare-default exit-code verdicts, and -- the
   one that misleads rather than omits -- a run closed by KILLJOB or
   term_run_time reading as RUNNING, since decision 2's trace is exactly
   what is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.ast_jil import JilParseError, parse
from dsl41.boundary import read_seal
from dsl41.canon import is_wire_int
from dsl41.ir import CatalogIR, LoweringError, Semantics, lower_catalog
from dsl41.oracle import Oracle
from dsl41.oracle_state import TERMINAL, CarriedRows, JobStatus, OracleError, TraceEntry
from dsl41.period import (
    Manifest,
    RuntimeProfile,
    bundle_dir,
    bundle_source_paths,
    disagreements,
    is_opening,
    job_fingerprints,
    opening_at,
    read_period_manifest,
    tz_aliases_of,
    check_manifest_against_segment,
    SEGMENT_FIELDS,
)
from dsl41.runner_adapters import load_json
from dsl41.runner_clock import EngineError
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
from dsl41.runner_journal import decision_effects, read_journal, replay_inputs
from dsl41.runner_ledger import STATE_MACHINE_VERSION


class RunHistoryError(Exception):
    """A run root cannot be read or replayed into history (offline, no
    engine): a bad manifest, a catalog that no longer lowers, or a journal
    replay refusal. The CLI turns this into exit 2, the house convention for
    an input that never reached the tool."""


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
    #: which clock supplied started_at/ended_at TOGETHER -- decision 1. Never
    #: a per-field mix: "spool" means both came from the wrapper's own spool
    #: (or spawn.json alone, for a run still open); "journal" means at least
    #: one of them had to fall back to the engine's own records.
    clock_source: Literal["spool", "journal"]
    #: this job's own definition fingerprint (decision 4), or None when no
    #: catalog was available. `catalog_hash` moves for ANY change anywhere in
    #: the estate; this moves only for this job's.
    job_hash: str | None = None
    #: how much of the row could be established (decision 5). "records_only"
    #: means the run root had no period manifest, so there was no catalog to
    #: rebuild and no trace to replay: box rows are absent entirely, and on the
    #: leaf rows `box_name`, `started_by` and `job_hash` are null, exit-code
    #: verdicts fall back to the bare SEM-09 default, and a run that KILLJOB or
    #: a term_run_time closed reads as RUNNING. It rides on the row rather than
    #: on a warning line so a JSON or CSV consumer cannot miss it.
    fidelity: Literal["full", "records_only"] = "full"


@dataclass(frozen=True)
class SpoolRead:
    """One run's spool facts, already validated against (job, run_number) --
    the thin I/O layer's output, plain data from here on."""

    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True)
class _TraceWindow:
    """One run's boundaries as the replayed trace shows them (decision 2):
    the Nth `*->STARTING` transition for a job opens it, the next terminal
    transition closes it (None if the trace never shows one)."""

    run_number: int
    started_at: datetime
    started_by: str
    ended_at: datetime | None
    status: str | None


def definition_change(previous: RunRow, row: RunRow) -> Literal["definition", "catalog"] | None:
    """Whether two consecutive rows of ONE job cross a change, and which
    signal says so -- decision 4's rule, and None when nothing changed.

    This is policy, not presentation: it decides what counts as "the job
    changed", so it lives beside the fingerprints it reads rather than in
    whatever renders the answer. `"definition"` is this job's own `job_hash`
    moving, which is the real signal. `"catalog"` is the estate-wide fallback,
    returned only when a row has no fingerprint (`fidelity="records_only"`),
    and named separately so a caller reports the weaker signal as the weaker
    signal instead of implying the stronger one."""
    if previous.job_hash is not None and row.job_hash is not None:
        return None if previous.job_hash == row.job_hash else "definition"
    return None if previous.catalog_hash == row.catalog_hash else "catalog"


def _parse_timestamp(value: str) -> datetime:
    """Spool/manifest timestamps may carry a UTC offset (supervisor-protocol
    ss3: "aware-UTC ISO-8601"); the journal's own never do (RealClock strips
    tzinfo). Normalize to the journal's naive-UTC basis so a duration is
    never `aware - naive`."""
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _windows_from_entries(entries: Sequence[TraceEntry], first: int = 0) -> list[_TraceWindow]:
    """One job's trace entries, already filtered to that job and in trace
    order, folded into run windows (decision 2). Out-of-band trace markers
    (`SCHED_DISARM`, `START_REFUSED`, ...) carry no "->" and are skipped --
    they are not a job's own status transitions.

    `first` is the run number this job had reached BEFORE the segment, so
    the first window here is `first + 1`. Run numbers are monotone across
    the ESTATE and not across a segment (I2): a replay that always started
    at 1 gave a box that ran in two periods the same `(job, run_number)`
    twice, and made a leaf run's window unfindable by its journal run
    number. Zero -- period 1, or a segment whose opening seal this root no
    longer holds -- is what the fold has always assumed."""
    windows: list[_TraceWindow] = []
    run_number = first
    started_at: datetime | None = None
    started_by = ""
    ended_at: datetime | None = None
    status: str | None = None
    for entry in entries:
        _, sep, new = entry.transition.partition("->")
        if not sep:
            continue
        if new == "STARTING":
            if started_at is not None:
                windows.append(_TraceWindow(run_number, started_at, started_by, ended_at, status))
            run_number += 1
            started_at, started_by, ended_at, status = entry.at, entry.cause, None, None
        elif new in TERMINAL and started_at is not None:
            # SEM-01 latching: a later CHANGE_STATUS overwrites the close
            # already recorded for this same open run, not a new one.
            ended_at, status = entry.at, new
    if started_at is not None:
        windows.append(_TraceWindow(run_number, started_at, started_by, ended_at, status))
    return windows


def _trace_windows_by_job(
    trace: Sequence[TraceEntry], carried: Mapping[str, int] | None = None
) -> dict[str, dict[int, _TraceWindow]]:
    """job -> {run_number: window}. Keyed by the run NUMBER rather than by
    position, because with `carried` the two are not the same thing."""
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
    """Every BOX job's run rows, entirely from the trace (decision 2): there
    is no dispatch record and no spool for a box.

    The windows are the caller's, so a box and a leaf in one fold are
    numbered from the same carried state."""
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
    """The terminal STATUS input's truthful end time, or None when there is
    none to trust (decision 3): no completion at all, or a reconcile
    completion whose payload carries no `ended_at` (E7 / "wrapper lost;
    killed at resume" -- `runner_startup.py`'s `_inject_completion` only
    writes the key when `resolve_spool` returned a real one)."""
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
    """SUCCESS/FAILURE for a bare exit_code payload needs the job's own
    SEM-09 boundary (`_handle_status` in oracle.py computes it the same
    way) -- without a catalog this falls back to the bare default
    (max_exit_success=0), which is what a job with no override would get
    anyway."""
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


def _attempt_index(record: Mapping[str, Any]) -> int | None:
    """A record's attempt number, read exactly: `seq` on an input, `index`
    on the `decision` that answers it (runner-design ss7 writes the same
    number under two names). None when the field is absent or is not an
    int -- `true` is not 1, the same rule `period.check_segment_record`
    applies, spelled here because these two fields are compared to each
    other.

    None FAILS OPEN at the one call site that matters: a `decision` whose
    `index` is `"3"` or `true` drops out of the rejected set and its
    completion decides the row again. That is the module's degrade posture
    (decision 5) and not a second authority -- a malformed decision is
    already refused by `decision_effects` when it carries effects."""
    key = "index" if record.get("rec") == "decision" else "seq"
    value = record.get(key)
    return value if is_wire_int(value) else None


def _rejected_attempts(records: list[dict[str, Any]]) -> set[int]:
    """The attempt indices a durable `decision` REJECTED.

    A decision is AUTHORITATIVE (concurrency-model ss4): `replay_inputs`
    does not feed a rejected attempt to the oracle, so a fold that read the
    same record would report history the engine refused to make.

    Only an EXPLICIT `rejected` is here. An attempt with no decision at all
    -- the ss4 crash window -- is absent, because the records do not say
    what became of it: `replay_inputs` re-decides such an attempt through
    the gate (`apply_attempt` with `decided=None`), and a fold reading
    records alone cannot run that gate. So a completion whose decision
    record was lost, and which a replay would reject, still decides its
    row. That residue is the crash-window half of this defect and is
    recorded rather than guessed at."""
    rejected: set[int] = set()
    for record in records:
        if record.get("rec") != "decision" or record.get("decision") != "rejected":
            continue
        index = _attempt_index(record)
        if index is not None:
            rejected.add(index)
    return rejected


def _leaf_rows(
    records: list[dict[str, Any]],
    catalog: CatalogIR | None,
    catalog_hash: str,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, Mapping[int, _TraceWindow]],
    fingerprints: Mapping[str, str],
    fidelity: Literal["full", "records_only"],
) -> list[RunRow]:
    """Every CMD/FW job's run rows, from `dispatch` + `input(kind=STATUS)`
    records (decision 1), with the trace as fallback for a close that never
    produced its own STATUS input (decision 2's KILLJOB/term_run_time
    case)."""
    dispatch_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    last_run_number: dict[str, int] = {}
    completion_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    executor_by_key: dict[tuple[str, int], str] = {}
    # BEFORE the walk: a decision is a later record than the attempt it
    # answers, so one pass would store a completion it cannot yet judge
    rejected = _rejected_attempts(records)

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
            if _attempt_index(record) in rejected:
                # the ss4 gate REJECTED this one and the oracle never saw it,
                # so neither does the row: taking it let a late `exit 0` the
                # engine refused overwrite the real FAILURE
                continue
            payload = record.get("payload") or {}
            c_job = payload.get("job")
            if not isinstance(c_job, str):
                continue
            c_run = payload.get("run_number")
            if not isinstance(c_run, int):
                # an operator CHANGE_STATUS names no run_number (cli.py's
                # sendevent has no such option): it overwrites whichever run
                # is currently open, same as the oracle's own SEM-01 read
                c_run = last_run_number.get(c_job)
                if c_run is None:
                    continue
            completion_by_key[(c_job, c_run)] = {
                "payload": payload,
                "at": record["at"],
                "source": record.get("source"),
            }
        elif rec == "decision":
            # the executor a run was bound to rides on the SPAWN effect inside
            # the decision that planned it (DL-118). Typed and validated by
            # the shared decoder, which refuses a malformed effect rather than
            # reporting the run without the host it ran on (DL-139)
            for effect in decision_effects(record):
                if effect.kind == "SPAWN":
                    executor_by_key[(effect.job, effect.run_number)] = effect.executor_id

    rows: list[RunRow] = []
    for key in order:
        job, run_number = key
        dispatch = dispatch_by_key[key]
        completion = completion_by_key.get(key)
        # by NUMBER, never by position: a run carried across a boundary has
        # a run number the segment's own window list does not index (I2)
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
                executor_id=executor_by_key.get(key),
                run_dir=run_dir if isinstance(run_dir, str) else None,
                box_name=box_name,
                clock_source=clock_source,  # type: ignore[arg-type]
                job_hash=fingerprints.get(job),
                fidelity=fidelity,
            )
        )
    return rows


def fold_run_rows(
    records: list[dict[str, Any]],
    *,
    catalog: CatalogIR | None = None,
    trace: Sequence[TraceEntry] = (),
    spool: Mapping[tuple[str, int], SpoolRead] | None = None,
    carried: Mapping[str, int] | None = None,
) -> list[RunRow]:
    """The pure fold (DL-113): a function of already-parsed journal records
    plus, optionally, a catalog (box_name/job_type/SEM-09) and a replayed
    trace (box run boundaries, started_by, and the KILLJOB/term_run_time
    close fallback -- decision 2). No filesystem, no Oracle, no Engine: both
    optional arguments are plain, already-computed data, so folding the same
    inputs twice always reproduces the same rows.

    Without a catalog: no box rows (there is nothing to identify a BOX job
    with), no box_name and no `job_hash` on leaf rows, and exit-code verdicts
    use the bare SEM-09 default rather than a job's declared
    success_codes/fail_codes. Every row then says so in `fidelity`
    (decision 5) rather than reading like a complete one. `read_run_root`
    below supplies a catalog whenever the run root still holds its bundle.

    `carried` is job -> the run number this period OPENED with, so a run
    that crosses a boundary keeps one identity (I2). Absent -- period 1, or
    a caller with no seal to read it from -- the fold numbers from 1, which
    is what it always did."""
    if not records or not is_opening(records[0]):
        raise RunHistoryError("run history requires a journal starting with a segment record")
    catalog_hash = str(records[0]["catalog_hash"])
    trace_windows = _trace_windows_by_job(trace, carried)
    fingerprints = {} if catalog is None else job_fingerprints(catalog)
    fidelity: Literal["full", "records_only"] = "records_only" if catalog is None else "full"
    rows: list[RunRow] = []
    if catalog is not None:
        rows.extend(_box_rows(trace, catalog, catalog_hash, fingerprints, trace_windows))
    rows.extend(
        _leaf_rows(
            records, catalog, catalog_hash, spool or {}, trace_windows, fingerprints, fidelity
        )
    )
    return rows


# ------------------------------------------------------------- I/O: the spool


def read_spool(
    run_dir: Path, job: str, run_number: int, bound_run_id: str | None = None
) -> SpoolRead | None:
    """One run's spool, read and validated against (job, run_number) --
    supervisor-protocol ss3's frozen spawn.json/status.json shapes. None
    when spawn.json is missing, unparseable, or names a different run
    (never trust a spoofed or pruned record, same rule
    `runner_adapters.resolve_spool` applies live).

    `bound_run_id` is the identity the run's durable SPAWN minted (DL-118):
    when known, a spool record naming a different `run_id` is a stranger's
    -- its timings would be reported as this run's -- and reads as ABSENT.
    Absent, not refused: this is offline reporting, and one corrupt
    directory should cost one row's timings, not the whole report."""
    spawn = load_json(run_dir / "spawn.json")
    if spawn is None or spawn.get("job") != job or spawn.get("run_number") != run_number:
        return None
    if bound_run_id is not None and spawn.get("run_id") != bound_run_id:
        return None
    raw_started = spawn.get("started_at")
    if not isinstance(raw_started, str):
        return None
    started_at = _parse_timestamp(raw_started)
    ended_at = None
    status = load_json(run_dir / "status.json")
    if status is not None and status.get("job") == job and status.get("run_number") == run_number:
        if bound_run_id is None or status.get("run_id") == bound_run_id:
            raw_ended = status.get("ended_at")
            if isinstance(raw_ended, str):
                ended_at = _parse_timestamp(raw_ended)
    return SpoolRead(started_at=started_at, ended_at=ended_at)


def _bound_run_ids(records: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """(job, run_number) -> the run_id its durable SPAWN bound, from the
    decisions in the log (DL-118), through the shared decoder (DL-139).

    Every SPAWN carries a run_id -- the decoder refuses one that does not --
    so the None test below is what tells the type checker that, not a
    tolerance."""
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
    """Rebuild the catalog `dsl41 run` loaded, from the run root's own
    self-contained artifact -- no estate-file argument needed, unlike
    `dsl41 journal`.

    One layout since DL-138, which retired the other. A root keeps its
    inputs in `catalogs/<source_bundle_hash>/`, addressed by their bytes,
    and names the address in `periods/<id>/manifest.json`; the bundle holds
    the byte-exact post-placeholder JIL (render_preserve, F1) in
    command-line order.

    This is NOT hash-gated the way `dsl41 journal` is: `SourceSpan.file`
    inside the reloaded catalog names the stored path, not the original
    recorded one, so its own `catalog_hash` can never equal the journal's
    (runner-design ss7 says so explicitly -- a deliberate defer). The one
    check available offline is that the manifest's OWN recorded hash --
    computed from the SAME original catalog object the opening record was
    written from, at the same moment -- agrees with that record; that
    catches a manifest that belongs to a different run, not a caller's path
    typo.

    `period_id` names WHICH period's catalog to rebuild; omitted, it is the
    active one. Every period of an estate has its own bundle, and a reader
    that always took the active period's would report a closed period's
    runs under the estate files it does NOT hold (period-model ss1.1)."""
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
    """Which period this root's ACTIVE segment holds (period-model I1).

    1 on a root that has never sealed, and the reason this is a
    function: every artifact under `periods/` is addressed by the
    period number, and a reader that defaulted to 1 after a boundary would
    read period 1's manifest beside period N's records and refuse the root
    as inconsistent."""
    try:
        opening = read_journal(run_root)[0]
    except (OSError, EngineError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    # `read_journal` has run ss2.1's schema over the opening, so this is
    # present and an int: a fallback here would be a second authority for a
    # field the reader already proved (DL-138)
    return int(opening["period_id"])


#: Retired estate layouts: the marker FILE that identifies one, and the
#: entry that retired it. APPEND-ONLY (docs/protocol-evolution.md ss6).
RETIRED_LAYOUTS: Final[tuple[tuple[str, str], ...]] = (("manifest/manifest.json", "DL-138"),)


def _refuse_retired_layout(run_root: Path) -> None:
    """D9, DL-138: a root with no period manifest is told WHICH state it is
    in, discriminated on the FILE.

    `<run-root>/manifest/manifest.json` is DL-66's layout and refuses by
    name. A `manifest/` directory WITHOUT that file is unknown residue and
    refuses generically. The two are different facts -- one root predates
    the period model, the other has a directory nothing here wrote -- and
    an operator needs to be told which is on the disk. A root with neither
    simply has no manifest, and the caller degrades (decision 5)."""
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
    """`read_period_manifest`, with its refusal converted to this module's,
    and with D9's layout tombstone where it returns None.

    Every door into run history answers `RunHistoryError` and the CLI
    prints it as exit 2; a decoder error escaping as itself would take
    down a whole multi-root `dsl41 runs` with a traceback."""
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
    """The stored inputs in command-line order -- DL-130's bundle -- or `[]`
    when this root stores none: an engine started with nothing staged, or a
    root whose retention pruned them. Empty is a missing fact and degrades
    (decision 5); a bundle that is present and unreadable is corruption and
    raises. `period_id` names which period's inputs; omitted, the active
    one."""
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
    """period-model ss2.1's other half: a build may replay only a log its
    own state machine wrote.

    One executable implements exactly one `STATE_MACHINE_VERSION` and
    refuses any other, so "a v2 binary cannot lead **or replay** C1".
    `runner_ledger.check_leader_eligibility` is the LEAD half and runs on
    resume only; this is the REPLAY half. Without it a foreign log replays
    in silence and the trace narrates transitions THIS build's semantics
    invented, which is worse than a refusal: it reaches `dsl41 runs` and
    `read_run_root` as ordinary rows.

    Three doors call it, which is every offline reader of a segment:
    `replay_trace` below, `_read_segment` (so the two DEGRADED paths that
    fold records with no replay refuse as well -- a foreign version is a
    WRONG fact, and decision 5 degrades only on a missing one), and the
    `dsl41 journal` lineage walk, where it sits beside the other opening
    checks so the refusal lands before a crossing is proved and announced.
    In `_read_segment` it runs AFTER `prove_opening`: identity before
    semantics, so a root that cannot prove its own opening is told that.

    No well-formed log can trip it while one version exists. A TAMPERED
    segment can, and that is the gate it is today; the version gate is what
    it becomes when a second version exists.

    The version is read EXACTLY, and an absent field refuses.
    `period.check_segment_record` already requires it with its type on
    every record `read_journal` returns, so an opening without one is
    hand-built and names no semantics at all -- history does not guess.
    This is DELIBERATELY stricter than the lead half, which reads an absent
    field as version 1 on the pre-S6a courtesy: that courtesy describes a
    journal `read_journal` has refused unconditionally since DL-138, so the
    two gates cannot disagree about a file on disk."""
    pinned = opening.get("state_machine_version")
    if is_wire_int(pinned) and pinned == STATE_MACHINE_VERSION:
        return
    named = f"{where}: " if where else ""
    raise RunHistoryError(
        f"{named}this segment names state_machine_version {pinned!r} and this build"
        f" derives v{STATE_MACHINE_VERSION} -- one executable implements one state"
        " machine, so this build can neither lead nor replay this log"
        " (period-model ss2.1)"
    )


def _period_profile(run_root: Path, opening: Mapping[str, Any]) -> "RuntimeProfile | None":
    """The runtime profile an opening segment's period was pinned with, or
    None where this root no longer holds the manifest (ss2.1, decision 5's
    degrade)."""
    manifest = read_period_manifest(run_root, int(opening["period_id"]))
    return None if manifest is None else manifest.runtime_profile


def replay_trace(
    run_root: Path,
    records: list[dict[str, Any]],
    catalog: CatalogIR,
    carried: "CarriedRows | None" = None,
) -> list[TraceEntry]:
    """The trace decision 2 needs, reconstructed exactly as `audit` does:
    an Oracle seeded with the rows this period OPENED with, this engine's
    own executor seeded (S6a routing reads it, `cli.py`'s `journal` command
    docstring explains why), then `replay_inputs`.

    A foreign `state_machine_version` refuses here before anything is
    replayed (`check_replay_version`): this build derives its own
    semantics, and narrating another one's log is silent loss.

    `carried` is None for period 1 and for a period whose opening seal this
    root no longer holds. Everywhere else it is required, not an
    optimization: revisions and run numbers are monotone across the ESTATE
    (I2), so an empty oracle derives numbers the log never recorded and
    `replay_inputs` refuses at the first admitted input that touches a
    carried entity (DL-136)."""
    check_replay_version(records[0], where=str(run_root))
    # SEM-35: this period's own alias table, read from its pin -- a narration
    # that resolved `timezone:` without it refuses a log the engine wrote
    # (DL-151). A root that no longer holds the manifest degrades to none,
    # which is what every reader here did before the table was wired at all.
    oracle = Oracle(
        catalog,
        carried=carried,
        tz_aliases=tz_aliases_of(_period_profile(run_root, records[0])),
    )
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(records[0]))
    try:
        replay_inputs(oracle, records)
    except OracleError as exc:
        raise RunHistoryError(f"{run_root}: replay failed ({exc})") from exc
    return oracle.trace()


def read_run_root(run_root: Path) -> list[RunRow]:
    """One run root's WHOLE retained history: every segment it still holds,
    each folded under its own period's catalog, concatenated (PR-50).

    **A period is a baseline, and this is the same fold `read_run_roots`
    already performs per root.** Draft one read the ACTIVE segment alone,
    which was true while a run root held one journal and became silently
    wrong the moment DL-133 made the WAL many files: after the first seal
    `dsl41 runs` reported an empty table, because every row it had ever
    printed lived in a segment it no longer opened. That is the same defect
    DL-135 closed for the subscriber's backfill, in the other reader.

    Each period is folded on its own inputs -- its own manifest, its own
    bundle, its own replay -- so a run is reported under the catalog it
    started in, which is what `start_period` is for. What does NOT restart
    at a boundary is state: the replay is seeded from the rows the period
    opened with (`_opening_rows`), so revisions and run numbers continue,
    and a box that ran in two periods gets runs 1 and 2 rather than run 1
    twice.

    Two limits, stated where a reader meets them. A run that SPANS a
    boundary keeps its row in the period that dispatched it, with its spool
    timings, and its STATUS stays RUNNING: the terminal input is in the
    NEXT segment and this fold reads one segment at a time, so the end time
    is there and the verdict is not. And the cost is one replay per
    retained period -- `--job` and `--since` filter after the fold, so a
    long-lived estate pays for its whole history to answer about one run."""
    from dsl41.period import wal_segments
    from dsl41.runner_journal import read_backfill

    if not wal_segments(run_root) and _archived_here(run_root):
        # every period this root held was ARCHIVED (DL-144). There are no
        # rows here and there is no fault either -- the caller names the
        # missing coverage, and a backfill over an empty segment list would
        # raise instead of saying so. `_archived_here` PROVES each receipt:
        # an unreadable one turning into an empty table is a shorter answer
        # bought with a file nobody checked
        return []
    try:
        # ONE validated read for the whole estate: `read_backfill` is where
        # DL-135 put the chain proofs -- sentinel-bound openings, filename
        # vs record, contiguous retained numbers, each segment opening from
        # the seal that closes the one before it, one estate, a continuous
        # index frontier -- and history reusing it means a spliced foreign
        # segment or a missing middle refuses HERE instead of silently
        # omitting a period or reporting a stranger's rows
        stream = read_backfill(run_root / "journal.jsonl", since=0).records
    except (OSError, EngineError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    rows: list[RunRow] = []
    for records in _split_segments(stream):
        try:
            rows.extend(_read_segment(run_root, records))
        except EngineError as exc:
            # the shared `decision_effects` refusal arrives here as itself
            # (DL-139). Named with the root, like every other refusal this
            # module raises: `dsl41 runs` reads several roots in one command
            # and "decision at index 5" alone does not say whose
            raise RunHistoryError(f"{run_root}: {exc}") from exc
    return rows


def _split_segments(stream: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The validated whole-estate stream, split back into its segments at
    each opening record."""
    out: list[list[dict[str, Any]]] = []
    for record in stream:
        if is_opening(record) or not out:
            out.append([])
        out[-1].append(record)
    return out


def _read_segment(run_root: Path, records: list[dict[str, Any]]) -> list[RunRow]:
    """The thin I/O shell for ONE period: its (already chain-validated)
    records + its manifest + the spool of the runs it dispatched, folded
    into run rows. `fold_run_rows` above stays pure."""
    if not records or not is_opening(records[0]):
        raise RunHistoryError(
            f"{run_root}: run history requires a journal starting with a segment record"
        )
    period_id = records[0]["period_id"]
    prove_opening(run_root, records[0])
    # AFTER the opening proof and before the manifest: identity first, then
    # semantics -- a segment this root cannot prove is that, not a version
    # complaint. Here rather than at `replay_trace` so the two DEGRADED
    # paths below, which fold records with no replay at all, refuse too
    check_replay_version(records[0], where=str(run_root))
    spool = _read_all_spool(records)
    period_manifest = _period_manifest_or_refuse(run_root, period_id)
    if period_manifest is None:
        # A MISSING manifest degrades; a WRONG one refuses, and a RETIRED
        # layout is named (`_period_manifest_or_refuse`). A root whose
        # retention pruned its manifest is exactly the root this tool exists
        # for. Every row it returns says `records_only`, so what is missing
        # rides on the data rather than on a warning the caller may not
        # print (decision 5).
        return fold_run_rows(records, spool=spool)
    # PR-22: the manifest and the segment are ONE object written twice,
    # and a self-consistent REPLACEMENT sharing catalog_hash but not
    # baseline_id or runtime_hash is foreign -- the full agreement
    # check, not one field
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
        # a manifest that names inputs this root no longer holds is the
        # same missing fact as no manifest at all: the rows come back
        # `records_only` rather than the whole root being refused
        return fold_run_rows(records, spool=spool, carried=carried)
    catalog = load_catalog_from_manifest(run_root, period_id)
    trace = replay_trace(run_root, records, catalog, opened)
    return fold_run_rows(records, catalog=catalog, trace=trace, spool=spool, carried=carried)


def prove_opening(run_root: Path, opening: Mapping[str, Any]) -> None:
    """A period whose opening NAMES a seal must hold that sidecar, and the
    sidecar must BE the seal this opening stands on (period-model ss11).

    The ss11 ladder, offline: the digest the naming record carries, the
    period and estate the sidecar claims, the instant it closed, and every
    `next_period` field the opening `segment` repeats. ONE implementation
    across every offline reader (DL-139): run history calls it BEFORE any
    degradation branch -- the manifest-missing path returns `records_only`
    rows, and without this proof it would return them from an opening this
    root cannot prove -- and `dsl41 journal`'s cross-period replay calls it
    at every boundary it crosses, for the reason DL-139 gives about
    diagnosis surfaces: a replay that narrated a forged continuation would
    be worse than one that refused.

    Public and reader-neutral: the refusal names the root and the record,
    never the tool that met them."""
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
    # the projection is DERIVED, never listed: every `next_period` field
    # that is also a segment field is compared, so a field added to
    # either model is covered by default rather than by somebody
    # remembering to extend a list
    shared = sorted(set(type(staged).model_fields) & SEGMENT_FIELDS)
    grafted = (
        seal.digest != digest
        or seal.period_id != period_id
        or seal.estate_id != opening.get("estate_id")
        or seal.closed_at.isoformat() != opening.get("at")
        or bool(disagreements(staged, opening, shared))
    )
    if grafted:
        # the digest alone binds the FILE'S bytes, not its place in this
        # lineage: a valid sidecar from another period or estate, with the
        # link rewritten to its digest, passes a digest-only check -- the
        # seal must also BE the named period's, this estate's, and the one
        # whose next_period IS this opening
        raise RunHistoryError(
            f"{run_root}: the sidecar at period {period_id!r} is not the seal this"
            f" opening stands on (seal: period {seal.period_id}, estate"
            f" {seal.estate_id}, opens {seal.next_period.period_id}) -- an identity"
            " graft, an unproved opening (period-model ss11, PR-50)"
        )


def _opening_rows(
    run_root: Path, opening: Mapping[str, Any], manifest: "Manifest | None"
) -> "CarriedRows | None":
    """The rows this period opened with -- `attest.carried_from_opening`,
    which is where audit gets the same fact.

    None means "this period opened from nothing": period 1, or a root with
    no period manifest. A period whose
    opening NAMES a seal and whose sidecar is absent, unreadable or not
    the named one REFUSES: swallowing that failure would let a period 2
    that happens to run only C2-added jobs replay cleanly from an empty
    state and return full-fidelity history from an opening this root
    cannot prove."""
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
    """Every period of this root whose archive receipt PROVES OUT, in
    order (DL-144).

    `period.archived_periods` lists the receipt FILES, which is what a
    lister owes. Every decision below reads this instead: an unreadable or
    unbound receipt must never buy a shorter answer, and it refuses here
    as `RunHistoryError` so `dsl41 runs` names it rather than printing a
    table with a period quietly missing."""
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
    """One line per period whose inputs were archived, naming the coverage
    this history therefore does NOT have (period-model ss12, DL-144).

    `dsl41 runs` folds a row out of a period's own WAL, so an archived
    period contributes none. The table is shorter for it, and the whole
    rule of this project is that a shorter answer is never a silent one --
    so the periods that are missing are named, with the receipt that says
    why they are missing rather than lost."""
    from dsl41.period import archive_receipt_path, wal_path

    return [
        f"{run_root}: period {period_id} has no rows -- its inputs were archived"
        f" ({archive_receipt_path(run_root, period_id).name}), so this history covers"
        " every period this estate retains and not that one"
        for run_root in run_roots
        for period_id in _archived_here(run_root)
        # the RECEIPT says archived; the SEGMENT says whether the rows are
        # gone. In the crash window between the two -- receipt durable,
        # deletion not done -- the fold still produces this period's rows,
        # and claiming otherwise would be the report lying in the direction
        # this whole warning exists to prevent (DL-144 review)
        if not wal_path(run_root, period_id).is_file()
    ]


def read_run_roots(
    run_roots: Sequence[Path], *, job: str | None = None, since: datetime | None = None
) -> list[RunRow]:
    """Every named run root's rows, combined and filtered (`--job`,
    `--since`), sorted by (job, started_at) -- the shape the CLI prints and
    the shape a segmentation break is computed over (decision 4)."""
    rows: list[RunRow] = []
    for run_root in run_roots:
        rows.extend(read_run_root(run_root))
    if job is not None:
        rows = [row for row in rows if row.job == job]
    if since is not None:
        rows = [row for row in rows if row.started_at >= since]
    return sorted(rows, key=lambda row: (row.job, row.started_at))
