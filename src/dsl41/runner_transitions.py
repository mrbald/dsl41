"""The committed transition rows (DL-536): the start's routine and the `transitions` read.

The engine commits the trace entries each record produced in that record's transaction
(`Journal.append`). The store says which seq intervals those rows cover. Nothing the engine
decides reads them: duplicate detection, dispatch, restart and retention keep their sources.
Nothing replays records to fill rows: a row exists because this build committed it.

`begin` runs once per engine start, after restart validation and never on the validate-only
path. Each step runs on its own, so one that fails leaves the others done:

1. Bound the earlier starts: a start whose own history row could not be written gets one,
   beginning just after the `engine started` record before its own. A start writes its row
   before any record of its own, so no earlier history vouches for a record it wrote.
2. Begin the start's history after the last seq. A client that read rows under an earlier
   history can trust them up to the seq the next history began at; a restored copy forgets the
   histories it never saw, so a cursor from the abandoned future reads as stale.
3. Settle the rows: drop those and the coverage outside the retained records (an earlier build
   pruned without knowing the table, or a copy holds rows its journal lost), and record a gap
   over every retained interval the table does not name (a root older than the table, or
   records an earlier build appended). Coverage starts at the first record this build commits.

If a step raises, the start goes on, one line says so, and `transitions` declines for this run
with the reason (`declined`), which `status` shows too: it asserts no entries, coverage,
continuity or history. The rows keep being committed with their records; the next start runs
the steps again.

`settle_history` is step 3 for the history index (DL-539), run after `begin` and on its own:
it drops the index's rows and coverage outside the retained records and records a gap over
every retained interval its coverage does not name, so the index too covers only what this
build committed. Its failure is kept apart (`history_declined`), and `status` shows it.

`answer` reads one page and shapes the verb's answer where the read runs (control-protocol ss4
`transitions`), so the whole encoded answer is sized there, off the engine's loop.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, NamedTuple

from dsl41.canon import is_scalar_string, is_wire_int
from dsl41.runner_journal import best_effort_report
from dsl41.runner_store import Interval, Store, TransitionQuery, TransitionRead

#: The most entries one answer holds; a larger `limit` is cut to it.
LIMIT_MAX: Final[int] = 5000

#: The entries one answer holds when the request names no `limit`.
LIMIT_DEFAULT: Final[int] = 500

#: The encoded bytes of one whole answer, header and counts included.
REPLY_BYTES: Final[int] = 1 << 20

#: The transition names one request may count.
COUNTS_MAX: Final[int] = 64

#: The largest seq or ordinal a cursor may name: the store's integer range.
CURSOR_MAX: Final[int] = 2**63 - 1

#: The string fields an entry too large for the answer may have cut; the longest goes first.
_CUTTABLE: Final[tuple[str, ...]] = ("cause", "job", "transition", "start_source")


# ---------------------------------------------------------------- the start's routine


class Begun(NamedTuple):
    """What the start's steps leave (DL-536, DL-541): why `transitions` declines for this run,
    or None; and the history this start began, which `status` names, or None when any step
    failed, even after the history was written."""

    declined: str | None
    history: str | None


def begin(store: Store, *, where: str) -> Begun:
    """Run the start's steps (module docstring), the history first and each step on its own,
    so a failed trim or gap step keeps the start's history boundary."""
    history = uuid.uuid4().hex
    steps: tuple[tuple[str, Callable[[], None]], ...] = (
        ("bound the earlier starts", lambda: _bound_earlier_starts(store)),
        ("begin the history", lambda: store.begin_history(history)),
        ("settle the rows", lambda: _settle_rows(store)),
    )
    reasons: list[str] = []
    for what, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 -- any failure: the start goes on, the verb declines
            reasons.append(f"{what}: {type(exc).__name__}: {exc}")
    if not reasons:
        return Begun(None, history)
    reason = "the start could not settle the transition rows: " + "; ".join(reasons)
    best_effort_report(f"dsl41: WARNING: {where}: {reason}; `transitions` declines (DL-536)")
    return Begun(reason, None)


def _bound_earlier_starts(store: Store) -> None:
    """Give every retained engine start with no history row one, beginning just after the
    `engine started` record before it: a start writes its history row before any record of its
    own and its `engine started` after, so the row cannot begin later than the start did, and
    an earlier history never vouches for a record this start wrote."""
    begun = [first for _, first in store.histories()]
    previous = 0
    for started in store.started_seqs():
        if not any(previous < first <= started for first in begun):
            store.begin_history(uuid.uuid4().hex, previous + 1)
        previous = started


def _settle_rows(store: Store) -> None:
    """Steps 1 and 2: drop what the journal no longer retains, record a gap over what this
    build did not commit."""
    _settle(store, store.trim_transitions, store.transitions_coverage, store.cover_transitions)


def settle_history(store: Store, *, where: str) -> str | None:
    """Step 3 for the history index (module docstring, DL-539). Answers None, or why the index
    serves no read for this run."""
    try:
        _settle(store, store.trim_history, store.history_coverage, store.cover_history)
    except Exception as exc:  # noqa: BLE001 -- any failure: the start goes on, the read declines
        reason = f"the start could not settle the history index: {type(exc).__name__}: {exc}"
        best_effort_report(f"dsl41: WARNING: {where}: {reason}; past runs decline (DL-539)")
        return reason
    return None


def _settle(
    store: Store,
    trim: Callable[[], None],
    coverage: Callable[[], list[Interval]],
    cover: Callable[[Sequence[Interval]], None],
) -> None:
    """Trim one table set to the retained records, then record a gap over every retained seq
    its coverage does not name."""
    trim()
    unnamed = _unnamed(coverage(), store.first_seq(), store.last_seq())
    if unnamed:
        cover([Interval(low, high, False) for low, high in unnamed])


def _unnamed(coverage: list[Interval], first: int, last: int) -> list[tuple[int, int]]:
    """The retained seqs no interval names, as `(low, high)` runs in seq order."""
    found: list[tuple[int, int]] = []
    at = first
    for interval in coverage:
        if interval.first > at:
            found.append((at, interval.first - 1))
        at = max(at, interval.last + 1)
    if at <= last:
        found.append((at, last))
    return found


# ---------------------------------------------------------------- the read


@dataclass(frozen=True)
class Request:
    """One `transitions` request, checked (control-protocol ss4)."""

    query: TransitionQuery
    history: str | None


class AnswerTooLarge(ValueError):
    """The answer with no entry in it is already past the byte cap: too many counts."""


def parse(request: Mapping[str, Any], *, upto: int) -> Request | str:
    """The checked request, or the reason it is refused, naming the field."""
    cursor = request.get("cursor")
    if cursor is not None and not (
        isinstance(cursor, list)
        and len(cursor) == 2
        and all(is_wire_int(part) and 0 <= part <= CURSOR_MAX for part in cursor)
    ):
        return f"cursor must be null or [seq, n], two integers from 0 to {CURSOR_MAX}"
    direction = request.get("direction", "forward")
    if direction not in ("forward", "backward"):
        return "direction must be forward or backward"
    limit = request.get("limit", LIMIT_DEFAULT)
    if not is_wire_int(limit) or limit < 0:
        return "limit must be a non-negative integer"
    texts: dict[str, str | None] = {}
    for key in ("job", "transition", "history"):
        value = request.get(key)
        if value is not None and not _text(value):
            return f"{key} must be null or a string"
        texts[key] = value
    counts = request.get("counts")
    if counts is not None and not (
        isinstance(counts, list) and len(counts) <= COUNTS_MAX and all(_text(c) for c in counts)
    ):
        return f"counts must be null or a list of at most {COUNTS_MAX} transition names"
    return Request(
        TransitionQuery(
            cursor=None if cursor is None else (cursor[0], cursor[1]),
            backward=direction == "backward",
            limit=min(limit, LIMIT_MAX),
            upto=upto,
            job=texts["job"],
            transition=texts["transition"],
            counts=None if counts is None else tuple(counts),
        ),
        texts["history"],
    )


class Stopped(Exception):
    """A read was stopped before it finished."""


def answer(
    store: Store,
    request: Request,
    *,
    header: Mapping[str, Any],
    byte_cap: int = REPLY_BYTES,
    stopped: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Read one page and shape the whole answer, `header` included, within `byte_cap` encoded
    bytes. The read decodes rows only up to the cap. A read `stopped` says to stop raises
    `Stopped` before the answer is sized."""
    read = store.read_transitions(dataclasses.replace(request.query, byte_cap=byte_cap))
    if stopped():
        raise Stopped("the read was stopped before its answer was sized")
    return fit(read, request, header=header, byte_cap=byte_cap)


def fit(
    read: TransitionRead,
    request: Request,
    *,
    header: Mapping[str, Any],
    byte_cap: int = REPLY_BYTES,
) -> dict[str, Any]:
    """The answer from `read` with as many entries as its encoding allows within `byte_cap`.
    A first entry that cannot fit alone comes with its longest text cut and `cut: true`, so the
    cursor passes it. An answer past the cap with no entry raises `AnswerTooLarge`."""
    wires = [_wire(seq, n, entry) for seq, n, entry in read.rows[: request.query.limit]]

    def build(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return _page(read, request, header, entries)

    bare = _size(build([]))
    if bare > byte_cap:
        raise AnswerTooLarge(
            f"the answer's counts and coverage alone pass the {byte_cap}-byte cap:"
            " name fewer transitions in counts"
        )
    taken, used = 0, 0
    for wire in wires:  # a first estimate from each entry's own encoding
        used += _size(wire) + (2 if taken else 0)
        if bare + used > byte_cap:
            break
        taken += 1
    shaped = build(wires[:taken])
    while taken and _size(shaped) > byte_cap:
        taken -= 1
        shaped = build(wires[:taken])
    while taken < len(wires) and _size(grown := build(wires[: taken + 1])) <= byte_cap:
        taken += 1
        shaped = grown
    if taken or not wires:
        return shaped
    return _cut(wires[0], lambda entry: build([entry]), byte_cap)


def _cut(
    wire: dict[str, Any], build: Callable[[dict[str, Any]], dict[str, Any]], byte_cap: int
) -> dict[str, Any]:
    """The answer holding `wire` alone, its longest texts cut to the longest prefixes that fit."""
    entry = {**wire, "cut": True}
    fields = sorted(
        (key for key in _CUTTABLE if isinstance(entry.get(key), str)),
        key=lambda key: -_size(entry[key]),
    )
    for key in fields:
        text: str = entry[key]
        if _size(build({**entry, key: ""})) > byte_cap:
            entry[key] = ""
            continue
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _size(build({**entry, key: text[:middle]})) <= byte_cap:
                low = middle
            else:
                high = middle - 1
        entry[key] = text[:low]
        return build(entry)
    raise AnswerTooLarge(
        f"one entry with its texts cut still passes the {byte_cap}-byte cap beside the counts:"
        " name fewer transitions in counts"
    )


def _page(
    read: TransitionRead,
    request: Request,
    header: Mapping[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """The answer holding `entries`, the first of `read.rows` in the read's direction."""
    query = request.query
    more = len(entries) < len(read.rows) or read.beyond
    floor, upto = read.first_seq, query.upto
    last = (int(entries[-1]["seq"]), int(entries[-1]["n"])) if entries else None
    cursor = query.cursor if last is None else last
    if cursor is not None and cursor > (upto, CURSOR_MAX):
        cursor = (upto, CURSOR_MAX)  # a cursor past the watermark goes on from it
    # the seqs the page spans: from its cursor to its last entry, or to the end of the
    # retained journal when nothing is left that way; both ends at or below the watermark
    if query.backward:
        high = upto if query.cursor is None else min(query.cursor[0], upto)
        low = (high if last is None else last[0]) if more else floor
    else:
        low = floor if query.cursor is None else query.cursor[0]
        high = min((low if last is None else last[0]) if more else upto, upto)
    # a forward cursor expires when a row after it was pruned; a backward one below the floor
    # when its page's seqs were
    pruned: list[list[int]] = []
    through = read.pruned_through
    if query.cursor is not None and not query.backward:
        if through is not None and query.cursor < through:
            # DL-541: only the missed seqs at or below the watermark; a cursor past it missed
            # none this answer may name, and the next page, routed later, reports them
            first, newest = max(query.cursor[0], 1), min(through[0], upto)
            if first <= newest:
                pruned.append([first, newest])
    elif query.cursor is not None and query.cursor[0] < floor:
        if 1 <= min(query.cursor[0], upto):
            pruned.append([1, min(query.cursor[0], upto)])
    covered, gaps = _spans(read.coverage, max(low, floor), high)
    shaped: dict[str, Any] = {
        **header,
        "entries": list(reversed(entries)) if query.backward else entries,
        "next": None if cursor is None else [cursor[0], cursor[1]],
        "more": more,
        "floor": floor,
        "expired": bool(pruned),
        "pruned": pruned,
        "covered": covered,
        "gaps": gaps,
        "watermark": upto,
        "history": read.histories[-1][0] if read.histories else None,
    }
    if request.history is not None:
        shaped["continuous_through"] = agrees_through(read.histories, request.history, upto)
    if read.counts is not None:
        counted, missed = _spans(read.coverage, floor, upto)
        shaped |= {"counts": read.counts, "counts_covered": counted, "counts_gaps": missed}
    return shaped


def _size(value: object) -> int:
    """The bytes of `value` as the control server encodes it."""
    return len(json.dumps(value, sort_keys=True).encode("utf-8"))


def _spans(
    coverage: list[Interval], low: int, high: int
) -> tuple[list[list[int]], list[list[int]]]:
    """The covered runs and the gaps between `low` and `high`: a gap is every seq there no
    covered interval names, recorded or not yet examined."""
    covered: list[list[int]] = []
    for interval in coverage:
        start, end = max(interval.first, low), min(interval.last, high)
        if interval.covered and start <= end:
            covered.append([start, end])
    gaps: list[list[int]] = []
    at = low
    for start, end in covered:
        if start > at:
            gaps.append([at, start - 1])
        at = end + 1
    if at <= high:
        gaps.append([at, high])
    return covered, gaps


def agrees_through(histories: Sequence[tuple[str, int]], named: str, upto: int) -> int:
    """The last seq at which history `named` and this store's agree: everything for the current
    history, up to where the next one began for an earlier one, nothing for one it never had."""
    for position, (history, _) in enumerate(histories):
        if history == named:
            if position + 1 == len(histories):
                return upto
            return histories[position + 1][1] - 1
    return 0


def _wire(seq: int, n: int, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One entry as the answer carries it: the `trace` entry's fields, written as `trace` writes
    them, and its key for `seq`."""
    wire = {
        "seq": seq,
        "n": n,
        "at": datetime.fromisoformat(entry["at"]).isoformat(),
        "job": entry["job"],
        "transition": entry["transition"],
        "cause": entry["cause"],
    }
    for key in ("start_source", "run_number", "refused_status"):
        if entry.get(key) is not None:
            wire[key] = entry[key]
    return wire


def _text(value: object) -> bool:
    return isinstance(value, str) and is_scalar_string(value)
