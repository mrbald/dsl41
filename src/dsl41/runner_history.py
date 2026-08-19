"""Run history (DL-113): a projection, not a new record kind.

`docs/runner-design.md` ss7 lists every record the journal ever writes; this
module invents none of them. It reads what is already there -- `dispatch`,
the `input` records the ss4 stale-completion gate journals, the effects
nested in each `decision`, and the run root's manifest/spool -- and folds
them into one row per job run: "how long did it take, run after run, and
did it change." Offline only: no engine, no control socket, no new verb on
the wire.

Two layers, deliberately split so the fold stays testable with no
filesystem: `fold_run_rows` is a pure function of already-parsed journal
records plus (optionally) a catalog and a trace -- both themselves plain
data, never a live `Engine`. `read_run_root` is the thin I/O shell: it reads
`journal.jsonl`, rebuilds the catalog from `manifest/` (DL-66's self-
contained artifact -- no estate-file argument needed, unlike `dsl41
journal`), replays it through a fresh `Oracle` exactly as `dsl41 journal`
does, and reads the spool. `read_spool` is its own thin function for the
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
   both the `catalog_hash` of the journal header it came from and this job's
   own `job_hash`, always, in every `--format`. The break line `--format
   table` prints between two consecutive rows of the same job fires on
   `job_hash`, falling back to `catalog_hash` only when either row has none.
   The estate hash alone is the wrong signal to draw it from: it is
   deliberately conservative (`runner_journal.catalog_hash` -- "an estate
   that changed in ANY way re-baselines"), so a release touching twelve jobs
   of eight hundred moves it for all eight hundred, and a break on it marks
   every job in the estate as changed. See `_job_fingerprints` for what the
   per-job hash can and cannot say -- in particular that it fingerprints the
   RESOLVED definition, so an estate whose placeholders vary per run gets no
   more than `catalog_hash` already gave it. Rows are sorted by
   (job, started_at) first, so a break lands exactly where that job changed
   -- never a hidden line, never a refusal to print.

5. **A missing manifest degrades; a wrong one refuses.** `manifest/` is
   DL-66 and a run root predating it has none, as does one whose retention
   pruned it -- and those are exactly the old run roots a history tool
   exists to read. So an absent manifest folds from records alone rather
   than refusing the whole root, while a manifest whose `catalog_hash`
   disagrees with the journal header still refuses, because that one is not
   a missing fact but a wrong one. What the degraded path costs is real and
   is carried per row in `fidelity` rather than in a warning line a JSON or
   CSV consumer never sees: no box rows at all, no `box_name`, no
   `started_by`, no `job_hash`, bare-default exit-code verdicts, and -- the
   one that misleads rather than omits -- a run closed by KILLJOB or
   term_run_time reading as RUNNING, since decision 2's trace is exactly
   what is unavailable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.ast_jil import JilParseError, parse
from dsl41.ir import CatalogIR, LoweringError, Semantics, lower_catalog
from dsl41.oracle import Oracle
from dsl41.oracle_state import TERMINAL, JobStatus, OracleError, TraceEntry
from dsl41.runner_adapters import load_json
from dsl41.runner_clock import EngineError
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, seed_local_executor
from dsl41.runner_journal import read_journal, replay_inputs


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
    #: means the run root had no `manifest/`, so there was no catalog to
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


def _strip_spans(value: Any) -> Any:
    """Every `SourceSpan`/`CondSpan` field in the IR is named exactly `span`
    (`ir.py`, `conditions.py`), so dropping that key recursively drops all
    position metadata. A job that only MOVED in its file must fingerprint the
    same as before it moved."""
    if isinstance(value, dict):
        return {k: _strip_spans(v) for k, v in value.items() if k != "span"}
    if isinstance(value, list):
        return [_strip_spans(v) for v in value]
    return value


def _job_fingerprints(catalog: CatalogIR) -> dict[str, str]:
    """Per-job definition fingerprints -- `runner_journal.catalog_hash`'s
    technique (sha256 over a canonical JSON dump) applied one level down.

    The estate-wide hash exists to gate resume and it is deliberately
    conservative: it moves for ANY change anywhere in the estate. That makes
    it the wrong thing to mark a *job's* series with. A release touching
    twelve jobs of eight hundred moves it for all eight hundred, so a break
    line computed from it fires on every job and tells the reader nothing
    about the one they are looking at.

    **The limit, stated rather than discovered.** `manifest/` holds the
    POST-placeholder JIL (DL-66), so this fingerprints the RESOLVED
    definition. An estate whose placeholders vary per run -- `examples/
    nightbank` bakes the run root into `profile`, `std_out_file` and
    `std_err_file`, so every job's resolved text differs between any two run
    roots -- makes every job look changed, and the break line degrades to
    exactly what `catalog_hash` already said. Where placeholders come from a
    stable site.properties, which is the deployment this is for, it says what
    it claims to. The row carries both hashes so a reader can tell the two
    situations apart.

    What this is NOT is a definition diff. "Changed how, and can the state
    carry across it" is the catalog-diff classification that a re-baseline
    needs, and it is not built."""
    return {
        name: hashlib.sha256(
            json.dumps(_strip_spans(job_ir.model_dump(mode="json")), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for name, job_ir in catalog.jobs.items()
    }


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


def _windows_from_entries(entries: Sequence[TraceEntry]) -> list[_TraceWindow]:
    """One job's trace entries, already filtered to that job and in trace
    order, folded into run windows (decision 2). Out-of-band trace markers
    (`SCHED_DISARM`, `START_REFUSED`, ...) carry no "->" and are skipped --
    they are not a job's own status transitions."""
    windows: list[_TraceWindow] = []
    run_number = 0
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


def _trace_windows_by_job(trace: Sequence[TraceEntry]) -> dict[str, list[_TraceWindow]]:
    by_job: dict[str, list[TraceEntry]] = {}
    for entry in trace:
        by_job.setdefault(entry.job, []).append(entry)
    return {job: _windows_from_entries(entries) for job, entries in by_job.items()}


def _box_rows(
    trace: Sequence[TraceEntry],
    catalog: CatalogIR,
    catalog_hash: str,
    fingerprints: Mapping[str, str],
) -> list[RunRow]:
    """Every BOX job's run rows, entirely from the trace (decision 2): there
    is no dispatch record and no spool for a box."""
    windows_by_job = _trace_windows_by_job(trace)
    rows: list[RunRow] = []
    for job, job_ir in catalog.jobs.items():
        if job_ir.job_type != "BOX":
            continue
        for window in windows_by_job.get(job, []):
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


def _note_executor(effect: Mapping[str, Any], executor_by_key: dict[tuple[str, int], str]) -> None:
    """Bind one run to the host its SPAWN named. One function for both
    dialects: a nested effect inside a `decision` and a pre-DL-118 standalone
    `effect` record carry the same fields, so reading them twice differently
    is how the two would drift."""
    if effect.get("kind") != "SPAWN":
        return
    job, run_number = effect.get("job"), effect.get("run_number")
    if isinstance(job, str) and isinstance(run_number, int):
        executor_by_key[(job, run_number)] = str(effect.get("executor_id"))


def _leaf_rows(
    records: list[dict[str, Any]],
    catalog: CatalogIR | None,
    catalog_hash: str,
    spool: Mapping[tuple[str, int], SpoolRead],
    trace_windows: Mapping[str, list[_TraceWindow]],
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
            # the decision that planned it (DL-118)
            for effect in record.get("effects") or []:
                _note_executor(effect, executor_by_key)
        elif rec == "effect":
            _note_executor(record, executor_by_key)  # pre-DL-118 dialect

    rows: list[RunRow] = []
    for key in order:
        job, run_number = key
        dispatch = dispatch_by_key[key]
        completion = completion_by_key.get(key)
        window = None
        windows = trace_windows.get(job, [])
        if 0 < run_number <= len(windows):
            window = windows[run_number - 1]

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
) -> list[RunRow]:
    """The pure fold (CM-37): a function of already-parsed journal records
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
    below supplies a catalog whenever the run root has a `manifest/`."""
    if not records or records[0].get("rec") != "header":
        raise RunHistoryError("run history requires a journal starting with a header record")
    catalog_hash = str(records[0]["catalog_hash"])
    trace_windows = _trace_windows_by_job(trace)
    fingerprints = {} if catalog is None else _job_fingerprints(catalog)
    fidelity: Literal["full", "records_only"] = "records_only" if catalog is None else "full"
    rows: list[RunRow] = []
    if catalog is not None:
        rows.extend(_box_rows(trace, catalog, catalog_hash, fingerprints))
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
    decisions in the log (DL-118). Empty for a pre-DL-118 journal."""
    bound: dict[tuple[str, int], str] = {}
    for record in records:
        if record.get("rec") != "decision":
            continue
        for effect in record.get("effects") or []:
            if effect.get("kind") == "SPAWN" and effect.get("run_id") is not None:
                bound[(str(effect.get("job")), int(effect.get("run_number", 0)))] = str(
                    effect.get("run_id")
                )
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


def load_catalog_from_manifest(run_root: Path) -> CatalogIR:
    """Rebuild the catalog `dsl41 run` loaded, from its own self-contained
    artifact (DL-66) -- no estate-file argument needed, unlike `dsl41
    journal`. `manifest/` holds the byte-exact post-placeholder JIL
    (render_preserve, F1); `manifest.json`'s `sources` list gives the file
    order.

    This is NOT hash-gated the way `dsl41 journal` is: `SourceSpan.file`
    inside the reloaded catalog names the manifest path, not the original
    recorded one, so its own `catalog_hash` can never equal the journal
    header's (runner-design ss7 says so explicitly -- a deliberate defer).
    The one check available offline is that manifest.json's OWN recorded
    hash -- computed from the SAME original catalog object `Journal.create`
    hashed for the header, at the same moment -- agrees with the header;
    that catches a manifest that belongs to a different run, not a caller's
    path typo."""
    manifest_dir = run_root / "manifest"
    payload = load_json(manifest_dir / "manifest.json")
    if payload is None:
        raise RunHistoryError(f"{run_root}: no readable manifest/manifest.json (DL-66 artifact)")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RunHistoryError(f"{run_root}: manifest.json carries no sources")
    parsed = []
    try:
        for entry in sources:
            name = entry["file"]
            path = manifest_dir / str(name)
            text = path.read_text(encoding="utf-8")
            parsed.append(parse(text, file=str(path)))
        return lower_catalog(parsed, permit_unknown=True)
    except (OSError, KeyError, TypeError, UnicodeDecodeError, JilParseError, LoweringError) as exc:
        raise RunHistoryError(
            f"{run_root}: cannot rebuild the catalog from manifest/ ({exc})"
        ) from exc


def replay_trace(
    run_root: Path, records: list[dict[str, Any]], catalog: CatalogIR
) -> list[TraceEntry]:
    """The trace decision 2 needs, reconstructed exactly as `dsl41 journal`
    does: a fresh Oracle, this engine's own executor seeded (S6a routing
    reads it, `cli.py`'s `journal` command docstring explains why), then
    `replay_inputs`."""
    oracle = Oracle(catalog)
    started_at = _parse_timestamp(str(records[0]["started_at"]))
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=started_at)
    try:
        replay_inputs(oracle, records)
    except OracleError as exc:
        raise RunHistoryError(f"{run_root}: replay failed ({exc})") from exc
    return oracle.trace()


def read_run_root(run_root: Path) -> list[RunRow]:
    """The thin I/O shell: journal + manifest + spool for
    one run root, folded into its run rows. The only function here that
    touches a filesystem end to end; `fold_run_rows` above stays pure."""
    try:
        records = read_journal(run_root / "journal.jsonl")
    except (OSError, EngineError) as exc:
        raise RunHistoryError(f"{run_root}: {exc}") from exc
    spool = _read_all_spool(records)
    manifest = load_json(run_root / "manifest" / "manifest.json")
    if manifest is None:
        # A MISSING manifest degrades; a WRONG one refuses. The two are
        # different facts and a history tool that refused both would be
        # unable to read exactly the run roots it exists for: `manifest/` is
        # DL-66 and every root predating it has none, as does one whose
        # retention pruned it. Every row it returns says `records_only`, so
        # what is missing rides on the data rather than on a warning the
        # caller may not print (decision 5).
        return fold_run_rows(records, spool=spool)
    if manifest.get("catalog_hash") != records[0].get("catalog_hash"):
        raise RunHistoryError(
            f"{run_root}: manifest/manifest.json's catalog_hash disagrees with the"
            " journal header -- this manifest is not this journal's"
        )
    catalog = load_catalog_from_manifest(run_root)
    trace = replay_trace(run_root, records, catalog)
    return fold_run_rows(records, catalog=catalog, trace=trace, spool=spool)


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
