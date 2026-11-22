"""A job's past runs, one bounded page at a time: the `history` read (DL-540).

The read finds a page through the history index (DL-539) and folds it with the run-history
fold (`runner_history.fold_run_rows`, DL-113), which stays the one definition of a run row.
The index only finds records; the fold reads each record's own payload.

1. **Boundary.** Full rows come only from the covered interval of the index that reaches the
   watermark; its first seq is the boundary, `full_from`. Nothing is stitched across a gap.
2. **Page.** The cursor is a run number, newest first: the openings of the job at or above the
   boundary and below the cursor. A page that shows one run number twice declines for the job.
3. **Evidence**, run by run under a budget of rows (`runner_store.HistoryRead`): the job's trace
   entries and the CONFIG marks of each run's range, through the entry of the opening above the
   page, the records the index names for its run wherever they sit, and the records it names
   for the job and no run from the run's own seq range. A decision with no run belongs to the
   page when its input does, paired by admission index. The configuration of each opening's
   rev is read whole, decoded once per stored content (`_configuration`); a missing one is an
   error of the request that names the rev, never the latest catalog.
4. **Fold** on a synthetic opening `segment` under the oldest opening's rev, as
   `read_run_root` does for a pruned store, with `spool={}`, no anchor and no history; the
   rows kept are the job's whose run is on the page. An opening with no row (a leaf never
   dispatched) still moves the cursor past it.
5. **Attempts** of each run from its `dispatch` records, each joined with its `outcome` by
   attempt id, at most `ATTEMPTS_MAX`, the newest kept.
6. **Earlier.** Once the full rows are exhausted, the windows the snapshot the engine restarted
   from recorded for the job, as recorded, below the job's first full opening. Runs opened
   after that snapshot and below the boundary are in neither list; `missing` names those seqs.
7. **Progress.** An answer that says `more` holds a run, a window or an `incomplete` number
   below the cursor, so `next` moves; one that cannot hold even that within the byte cap is
   refused (`AnswerTooLarge`).

For a run opened at or above the boundary the row equals the whole fold's (`read_run_root`
over the whole journal with the replayed trace and `spool={}`), with one exception: a STATUS
with no run number admitted after the next run opened (a greater seq), at a time not later
than that opening's and before that run's dispatch, when the two runs are on different pages.
The whole fold gives it to the earlier run, by time; that run's page does not hold it. On one
page the read gives it to the earlier run too. On a pruned store the whole fold calls a run
that began before its snapshot `partial` where this read calls it `full`. No decision reads
the answer.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from dsl41.canon import is_scalar_string, is_wire_int
from dsl41.oracle_state import TraceEntry
from dsl41.runner_config import Configuration
from dsl41.runner_history import fold_run_rows
from dsl41.runner_journal import outcome_end, wire_record
from dsl41.runner_store import HistoryQuery, HistoryRead, Record, Store
from dsl41.runner_transitions import AnswerTooLarge, Stopped, agrees_through

#: The openings one page holds when the request names no `limit`.
LIMIT_DEFAULT: Final[int] = 20

#: The most openings one page holds; a larger `limit` is cut to it.
LIMIT_MAX: Final[int] = 200

#: The rows of evidence (trace entries plus records) one request may read.
BUDGET: Final[int] = 20000

#: The attempts one run shows, the newest kept.
ATTEMPTS_MAX: Final[int] = 50

#: The encoded bytes of one whole answer, header included.
REPLY_BYTES: Final[int] = 1 << 20

#: The largest run number a cursor may name: the store's integer range.
CURSOR_MAX: Final[int] = 2**63 - 1

#: The decoded configurations kept across requests, by the digest of their stored bytes.
CONFIGURATIONS_KEPT: Final[int] = 16


@dataclass(frozen=True)
class Request:
    """One `history` request, checked (control-protocol ss4 `history`)."""

    job: str
    cursor: int | None
    limit: int
    history: str | None


@dataclass(frozen=True)
class Restored:
    """What the loop hands over: the seq of the snapshot the engine restarted from (None when
    it restarted from none) and the job's run windows that snapshot recorded, the engine's own
    list, which nothing changes in place."""

    seq: int | None
    windows: Sequence[Mapping[str, Any]]


class Declined(Exception):
    """The read declines for this job; the message is the reason."""


def parse(request: Mapping[str, Any]) -> Request | str:
    """The checked request, or the reason it is refused, naming the field. A bound is two
    comparisons on a plain `int` (DL-540)."""
    job = request.get("job")
    if not (isinstance(job, str) and is_scalar_string(job)):
        return "job must be a string naming the job"
    cursor = request.get("cursor")
    if cursor is not None:
        if not is_wire_int(cursor) or int(cursor) < 0 or int(cursor) > CURSOR_MAX:
            return f"cursor must be null or a run number from 0 to {CURSOR_MAX}"
        cursor = int(cursor)
    limit = request.get("limit", LIMIT_DEFAULT)
    if not is_wire_int(limit) or int(limit) < 1:
        return "limit must be a positive integer"
    history = request.get("history")
    if history is not None and not (isinstance(history, str) and is_scalar_string(history)):
        return "history must be null or a string"
    return Request(job, cursor, min(int(limit), LIMIT_MAX), history)


#: One item of a page in the order the byte cap drops them, oldest last: a run with its row
#: and attempts (None for an opening with no row), or a restored window.
type _Item = tuple[int, str, dict[str, Any] | None]


def answer(
    store: Store,
    request: Request,
    *,
    upto: int,
    header: Mapping[str, Any],
    restored: Restored,
    budget: int = BUDGET,
    byte_cap: int = REPLY_BYTES,
    stopped: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Read one page and shape the whole answer, `header` included, within `byte_cap` encoded
    bytes. A read `stopped` says to stop raises `Stopped` at its next step. A page that shows a
    run number twice, or names a rev the store holds no configuration for, raises `Declined`;
    an answer that cannot hold one item within the cap raises `AnswerTooLarge`."""
    query = HistoryQuery(request.job, request.cursor, request.limit, upto, budget)
    read = store.read_history(query)
    _go_on(stopped)
    if read.duplicate is not None:
        raise Declined(
            f"the history index shows run {read.duplicate} of {request.job!r} twice, so this"
            " page cannot say which records are that run's (DL-540)"
        )
    items: list[_Item] = [(n, "run", run) for n, run in _runs(read, request.job, stopped)]
    _go_on(stopped)
    incomplete = [] if read.incomplete is None else [read.incomplete.run_number]
    earlier: dict[str, Any] | None = None
    later = False  # restored windows remain past the page's slots
    if not read.more:
        below = min([n for n, _, _ in items] + incomplete, default=request.cursor)
        windows = _earlier(restored.windows, read.first_full, below)
        slots = max(request.limit - len(items) - len(incomplete), 0)
        later = len(windows) > slots
        items += [(int(window["run"]), "window", window) for window in windows[:slots]]
        earlier = {"snapshot_seq": restored.seq, "missing": _missing(read, restored, upto)}

    def build(kept: int, passed: list[int]) -> dict[str, Any]:
        shown, dropped = items[:kept], items[kept + len(passed) :]
        runs_shown = sum(1 for _, kind, _ in shown if kind == "run")
        whole = runs_shown + len(passed) >= sum(1 for _, kind, _ in items if kind == "run")
        numbers = [n for n, _, _ in shown] + incomplete + passed
        shaped: dict[str, Any] = {
            **header,
            "job": request.job,
            "runs": [run for _, kind, run in shown if kind == "run" and run is not None],
            "incomplete": incomplete + passed,
            "next": min(numbers, default=request.cursor),
            "more": read.more or later or bool(dropped),
            "full_from": None
            if read.boundary is None
            else {
                "seq": read.boundary,
                "at": None if read.boundary_at is None else read.boundary_at.isoformat(),
            },
            "history": read.histories[-1][0] if read.histories else None,
            "earlier": None
            if earlier is None or not whole
            else {**earlier, "windows": [w for _, kind, w in shown if kind == "window"]},
        }
        if request.history is not None:
            shaped["continuous_through"] = agrees_through(read.histories, request.history, upto)
        return shaped

    # each item is sized once; the answer is encoded again only to settle the estimate
    sizes: list[int] = []
    for _, _, item in items:
        _go_on(stopped)
        sizes.append(0 if item is None else _size(item) + 2)
    room = byte_cap - _size(build(0, []))
    kept = 0
    while kept < len(sizes) and sizes[kept] <= room:
        room -= sizes[kept]
        kept += 1
    shaped = build(kept, [])
    while kept and _size(shaped) > byte_cap:
        _go_on(stopped)
        kept -= 1
        shaped = build(kept, [])
    while kept < len(items) and _size(grown := build(kept + 1, [])) <= byte_cap:
        _go_on(stopped)
        kept += 1
        shaped = grown
    if kept == 0 and items:
        # the newest item cannot fit alone: the cursor passes it as incomplete
        shaped = build(0, [items[0][0]])
    if _size(shaped) > byte_cap:
        raise AnswerTooLarge(
            f"the answer passes the {byte_cap}-byte cap with no run or window in it:"
            " name a shorter job"
        )
    return shaped


def _go_on(stopped: Callable[[], bool]) -> None:
    if stopped():
        raise Stopped("the read was stopped before its answer was shaped")


_kept: OrderedDict[bytes, Configuration] = OrderedDict()
_kept_lock = threading.Lock()


def _configuration(data: bytes, rev: int) -> Configuration:
    """The configuration `data` holds, decoded once per content and kept for the next
    requests: the key is the digest of the stored bytes, never the rev alone, since a restored
    root can hold other bytes under a rev. Reads on several threads share it."""
    key = hashlib.sha256(data).digest()
    with _kept_lock:
        found = _kept.get(key)
        if found is not None:
            _kept.move_to_end(key)
            return found
    decoded = Configuration.from_bytes(data, where=f"configuration rev {rev}")
    with _kept_lock:
        _kept[key] = decoded
        while len(_kept) > CONFIGURATIONS_KEPT:
            _kept.popitem(last=False)
    return decoded


def _runs(
    read: HistoryRead, job: str, stopped: Callable[[], bool]
) -> list[tuple[int, dict[str, Any] | None]]:
    """Each opening of the page, newest first, with its row and attempts, or None for one the
    fold gives no row (a leaf never dispatched)."""
    if not read.runs:
        return []
    configurations: dict[int, Configuration] = {}
    for rev, data in read.configs.items():
        if data is None:
            raise Declined(
                f"the store holds no configuration rev {rev}, which a run of {job!r} opened"
                " under, and the read takes no other in its place (DL-540)"
            )
        _go_on(stopped)
        configurations[rev] = _configuration(data, rev)
    oldest = configurations[read.runs[-1].rev]
    upper = read.runs[0].until
    loose = _paired(read.loose, None if upper is None else upper[0])
    evidence = sorted([*read.owned, *loose], key=lambda found: found[0])
    records = [{"rec": "segment", "rev": read.runs[-1].rev, "catalog_hash": oldest.catalog_hash}]
    records += [wire for _, record in evidence if (wire := wire_record(record)) is not None]
    rows = fold_run_rows(
        records,
        catalog=oldest.catalog,
        trace=[TraceEntry.model_validate(entry) for _, _, entry in read.entries],
        spool={},
        catalogs={rev: configuration.catalog for rev, configuration in configurations.items()},
    )
    by_run = {row.run_number: row for row in rows if row.job == job}
    out: list[tuple[int, dict[str, Any] | None]] = []
    for run in read.runs:
        row = by_run.get(run.run_number)
        if row is None:
            out.append((run.run_number, None))
            continue
        attempts = _attempts(read.owned, run.run_number)
        out.append(
            (
                run.run_number,
                {
                    "row": row.model_dump(mode="json"),
                    "attempts": attempts[-ATTEMPTS_MAX:],
                    "attempts_cut": len(attempts) > ATTEMPTS_MAX,
                },
            )
        )
    return out


def _paired(loose: Sequence[tuple[int, Record]], upper: int | None) -> list[tuple[int, Record]]:
    """The records with no run that belong to the page, which holds whole admissions: every
    input at or below the seq of the opening above it (`upper`, None for the watermark), and a
    decision only when its input is among them, by admission index."""
    inputs = [
        (seq, record)
        for seq, record in loose
        if record.kind == "input" and (upper is None or seq <= upper)
    ]
    indices = {record.payload.get("index") for _, record in inputs}
    decisions = [
        (seq, record)
        for seq, record in loose
        if record.kind == "decision" and record.payload.get("index") in indices
    ]
    return [*inputs, *decisions]


def _attempts(owned: Sequence[tuple[int, Record]], run: int) -> list[dict[str, Any]]:
    """The run's attempts in dispatch order, each with its outcome or None: no recorded
    outcome. No environment."""
    outcomes = {
        record.payload["attempt"]: record.payload
        for _, record in owned
        if record.kind == "outcome" and record.payload.get("run_number") == run
    }
    shaped: list[dict[str, Any]] = []
    for _, record in owned:
        body = record.payload
        if record.kind != "dispatch" or body["run_number"] != run:
            continue
        paths = body.get("paths") or {}
        ended = outcomes.get(body["attempt"])
        shaped.append(
            {
                "ordinal": body.get("ordinal"),
                "attempt": body["attempt"],
                "dispatched_at": body["started_at"],
                "rev": body["rev"],
                "after": body.get("after"),
                "spool": body.get("spool"),
                "log_out": paths.get("std_out_file"),
                "log_err": paths.get("std_err_file"),
                "outcome": None if ended is None else _outcome(ended),
            }
        )
    return shaped


def _outcome(body: Mapping[str, Any]) -> dict[str, Any]:
    exit_code = (body.get("status") or {}).get("exit_code")
    return {
        "kind": body.get("outcome"),
        "exit_code": exit_code if is_wire_int(exit_code) else None,
        "ended_at": outcome_end(body),
        "ladder": body.get("ladder"),
    }


def _earlier(
    windows: Sequence[Mapping[str, Any]], first_full: int | None, below: int | None
) -> list[dict[str, Any]]:
    """The restored windows below the job's first full opening and the cursor, newest first,
    as recorded."""
    kept = [
        dict(window)
        for window in windows
        if (first_full is None or int(window["run"]) < first_full)
        and (below is None or int(window["run"]) < below)
    ]
    return sorted(kept, key=lambda window: -int(window["run"]))


def _missing(read: HistoryRead, restored: Restored, upto: int) -> list[int] | None:
    """The seqs where a run may have opened that neither list holds: after the restored
    snapshot (or from the oldest retained record) and below the boundary."""
    low = read.first_seq if restored.seq is None else restored.seq + 1
    high = upto if read.boundary is None else read.boundary - 1
    return [low, high] if low <= high else None


def _size(value: object) -> int:
    """The bytes of `value` as the control server encodes it."""
    return len(json.dumps(value, sort_keys=True).encode("utf-8"))
