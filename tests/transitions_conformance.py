"""A conformance checker for the ENGINE's `transitions` answers (DL-541).

Test coverage of the engine, not shipped: every property of an answer that control-protocol
ss4 `transitions` states, relations between fields and against the request included. The
terminal UI checks only the schema of what it reads and trusts the engine for the rest; this
checker is how the tests hold the engine to the rest. `check_conformance` takes an answer and
the request it answers, and raises `NonConforming` naming the property it breaks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

type _Key = tuple[int, int]
_KEY_MAX = 2**63 - 1


def _naive_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is None else None


#: the most entries one page holds whatever the request asked (control-protocol ss4)
_PAGE_MOST = 5000


class NonConforming(ValueError):
    """A `transitions` answer out of the contract's form, or not an answer to its request."""


@dataclass(frozen=True)
class PageEntry:
    """One entry of a decoded page: the `trace` entry's fields, typed, and its key."""

    seq: int
    n: int
    at: datetime
    at_text: str
    job: str
    transition: str
    cause: str
    start_source: str | None = None
    run_number: int | None = None
    refused_status: str | None = None
    cut: bool = False

    @property
    def key(self) -> _Key:
        return self.seq, self.n

    def fields(self) -> dict[str, Any]:
        """The entry as the console and the refusal count read it: its decoded fields only."""
        out: dict[str, Any] = {
            "seq": self.seq,
            "n": self.n,
            "at": self.at_text,
            "job": self.job,
            "transition": self.transition,
            "cause": self.cause,
        }
        for name in ("start_source", "run_number", "refused_status"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        return out


type _Interval = tuple[int, int]


@dataclass(frozen=True)
class TransitionPage:
    """A `transitions` answer decoded whole (control-protocol ss4)."""

    baseline_id: str
    applied_index: int
    entries: tuple[PageEntry, ...]
    next: _Key | None
    more: bool
    floor: int
    expired: bool
    pruned: tuple[_Interval, ...]
    covered: tuple[_Interval, ...]
    gaps: tuple[_Interval, ...]
    watermark: int
    history: str | None
    continuous_through: int | None = None
    counts: dict[str, dict[str, int]] | None = None
    counts_covered: tuple[_Interval, ...] | None = None
    counts_gaps: tuple[_Interval, ...] | None = None


def _form_seq(value: Any, what: str) -> int:
    """A seq or a count: an integer from 0 to 2^63 - 1, never a boolean."""
    if type(value) is not int:
        raise NonConforming(f"{what} is not an integer")
    number = int(value)
    if number < 0 or number > _KEY_MAX:
        raise NonConforming(f"{what} is out of range")
    return number


def _form_key(value: Any, what: str) -> _Key:
    if not isinstance(value, list) or len(value) != 2:
        raise NonConforming(f"{what} is not a key")
    return _form_seq(value[0], what), _form_seq(value[1], what)


def _form_intervals(
    value: Any, what: str, *, ordered: bool, watermark: int
) -> tuple[_Interval, ...]:
    """Seq intervals, both ends included, at or below the watermark, as every seq the answer
    names is; `ordered` requires low not above high."""
    if not isinstance(value, list):
        raise NonConforming(f"{what} is not a list")
    out: list[_Interval] = []
    for interval in value:
        low, high = _form_key(interval, what)
        if ordered and low > high:
            raise NonConforming(f"{what} holds an interval whose low is above its high")
        if low > watermark or high > watermark:
            raise NonConforming(f"{what} names a seq above the watermark")
        out.append((low, high))
    return tuple(out)


def _form_text(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise NonConforming(f"{what} is not a string")
    return value


def _form_bool(value: Any, what: str) -> bool:
    if type(value) is not bool:
        raise NonConforming(f"{what} is not a boolean")
    return bool(value)


def _form_entry(value: Any) -> PageEntry:
    """An entry: the `trace` entry's fields, unset optional ones omitted (control-protocol
    ss4)."""
    if not isinstance(value, Mapping):
        raise NonConforming("an entry is not an object")
    for name in ("seq", "n", "at", "job", "transition", "cause"):
        if name not in value:
            raise NonConforming(f"an entry has no {name}")
    at_text = _form_text(value["at"], "an entry's at")
    at = _naive_utc(at_text)
    # written as `trace` writes it: a naive UTC time in Python's isoformat, a time included
    if at is None or at.isoformat() != at_text:
        raise NonConforming("an entry's at is not written as `trace` writes it")
    optional: dict[str, Any] = {}
    for name in ("start_source", "refused_status"):
        if name in value:
            optional[name] = _form_text(value[name], f"an entry's {name}")
    if "run_number" in value:
        optional["run_number"] = _form_seq(value["run_number"], "an entry's run_number")
    cut = False
    if "cut" in value:
        if value["cut"] is not True:
            raise NonConforming("an entry's cut is not true")
        cut = True
    return PageEntry(
        seq=_form_seq(value["seq"], "an entry's seq"),
        n=_form_seq(value["n"], "an entry's n"),
        at=at,
        at_text=at_text,
        job=_form_text(value["job"], "an entry's job"),
        transition=_form_text(value["transition"], "an entry's transition"),
        cause=_form_text(value["cause"], "an entry's cause"),
        cut=cut,
        **optional,
    )


@dataclass(frozen=True)
class Refusal:
    """An `ok: false` answer's envelope (control-protocol ss2): `code` names the reason and may
    be missing on a replayed decision; `error` is prose."""

    code: str | None
    error: str


def check_refusal(answer: Mapping[str, Any]) -> Refusal:
    """The envelope of an `ok: false` answer, or `NonConforming`: `code` and `error` are
    strings when present (control-protocol ss2)."""
    code, error = answer.get("code"), answer.get("error", "")
    if code is not None and not isinstance(code, str):
        raise NonConforming("a refusal's code is not a string")
    if "code" in answer and code is None:
        raise NonConforming("a refusal's code is null")
    if not isinstance(error, str):
        raise NonConforming("a refusal's error is not a string")
    return Refusal(code, error.strip())


def check_conformance(answer: Any, request: Mapping[str, Any]) -> TransitionPage:
    """The page `answer` holds for `request`, or `NonConforming` (DL-541). A refusal is
    classified before this, by its code. The form is control-protocol ss4 `transitions`: the
    header's types; the entries' fields; keys unique and strictly ascending in both
    directions, within the 63-bit bounds, not above the watermark and on the asked side of the
    request's cursor; at most the asked limit and 5000 entries; a cut entry alone; `next` as the
    direction requires, or the request's cursor clamped to the watermark on an empty page; the
    continuity field when a history was sent; the counts when they were asked. A field the
    contract does not name is ignored."""
    if not isinstance(answer, Mapping) or answer.get("ok") is not True:
        raise NonConforming("the answer is not an ok object")
    names = ("baseline_id", "applied_index", "entries", "next", "more", "floor", "expired")
    for name in (*names, "pruned", "covered", "gaps", "watermark", "history"):
        if name not in answer:
            raise NonConforming(f"the answer has no {name}")
    watermark = _form_seq(answer["watermark"], "watermark")
    history = answer["history"]
    if history is not None:
        history = _form_text(history, "history")
    raw = answer["entries"]
    if not isinstance(raw, list):
        raise NonConforming("entries is not a list")
    entries = tuple(_form_entry(value) for value in raw)
    limit = request.get("limit", 500)
    most = min(int(limit) if type(limit) is int else 500, _PAGE_MOST)
    if len(entries) > most:
        raise NonConforming(f"the page holds {len(entries)} entries, past {most}")
    if any(entry.cut for entry in entries) and len(entries) != 1:
        raise NonConforming("a cut entry does not come alone")
    keys = [entry.key for entry in entries]
    if any(later <= earlier for earlier, later in zip(keys, keys[1:], strict=False)):
        raise NonConforming("the keys are not unique and ascending")
    if keys and keys[-1][0] > watermark:
        raise NonConforming("an entry is above the watermark")
    backward = request.get("direction") == "backward"
    asked = request.get("cursor")
    cursor = None if asked is None else _form_key(asked, "the request's cursor")
    if cursor is not None and keys:
        if (backward and keys[-1] >= cursor) or (not backward and keys[0] <= cursor):
            raise NonConforming("an entry is on the wrong side of the cursor")
    following = None if answer["next"] is None else _form_key(answer["next"], "next")
    if keys:
        expected: _Key | None = keys[0] if backward else keys[-1]
    else:
        expected = None if cursor is None else min(cursor, (watermark, _KEY_MAX))
    if following != expected:
        raise NonConforming("next does not continue the page")
    page = TransitionPage(
        baseline_id=_form_text(answer["baseline_id"], "baseline_id"),
        applied_index=_form_seq(answer["applied_index"], "applied_index"),
        entries=entries,
        next=following,
        more=_form_bool(answer["more"], "more"),
        floor=_form_seq(answer["floor"], "floor"),
        expired=_form_bool(answer["expired"], "expired"),
        pruned=_form_intervals(answer["pruned"], "pruned", ordered=True, watermark=watermark),
        covered=_form_intervals(answer["covered"], "covered", ordered=True, watermark=watermark),
        gaps=_form_intervals(answer["gaps"], "gaps", ordered=True, watermark=watermark),
        watermark=watermark,
        history=history,
    )
    if request.get("history") is not None:
        if "continuous_through" not in answer:
            raise NonConforming("the answer has no continuous_through")
        through = _form_seq(answer["continuous_through"], "continuous_through")
        if through > watermark:
            raise NonConforming("continuous_through is above the watermark")
        page = replace(page, continuous_through=through)
    asked_counts = request.get("counts")
    if asked_counts is not None:
        page = replace(page, **_form_counts(answer, watermark, page.floor, asked_counts))
    elif "counts" in answer:
        raise NonConforming("counts answers a request that did not name it")
    if entries and entries[0].seq < page.floor:
        raise NonConforming("an entry is below the floor")
    _form_expiry(page, cursor, backward)
    _form_coverage(page, cursor, backward)
    return page


def _form_union(intervals: Iterable[_Interval], what: str) -> _Interval | None:
    """The one seq range disjoint intervals cover together, or None for none; `NonConforming`
    when two overlap or they leave a hole."""
    ordered = sorted(intervals)
    for (_, high), (low, _) in zip(ordered, ordered[1:], strict=False):
        if low <= high:
            raise NonConforming(f"{what} overlap")
        if low != high + 1:
            raise NonConforming(f"{what} leave seqs unaccounted for")
    return (ordered[0][0], ordered[-1][1]) if ordered else None


def _form_coverage(page: TransitionPage, cursor: _Key | None, backward: bool) -> None:
    """`covered` and `gaps` are seq intervals over the seqs the page spans at or above `floor`
    (control-protocol ss4): together they account for that span once, disjoint and holding
    every entry. The span runs from the cursor, or from `floor` forward and the watermark
    backward for a null cursor, the cursor clamped to the watermark backward, to the page's
    last entry, or to the end of the retained journal when nothing remains that way; a page
    with no entry that says `more` spans its cursor's seq alone."""
    union = _form_union((*page.covered, *page.gaps), "covered and gaps")
    floor, watermark = page.floor, page.watermark
    for entry in page.entries:
        if union is None or entry.seq < union[0] or entry.seq > union[1]:
            raise NonConforming("an entry lies outside the seqs covered and gaps account for")
    if backward:
        high = watermark if cursor is None else min(cursor[0], watermark)
        if not page.more:
            low = floor
        else:
            low = page.entries[0].seq if page.entries else high
    else:
        low = floor if cursor is None else cursor[0]
        if not page.more:
            high = watermark
        else:
            high = min(page.entries[-1].seq if page.entries else low, watermark)
    start = max(low, floor)
    if union != ((start, high) if start <= high else None):
        raise NonConforming("covered and gaps do not account for the page's span")


def _form_expiry(page: TransitionPage, cursor: _Key | None, backward: bool) -> None:
    """`expired` and `pruned` agree with each other and with the request (control-protocol
    ss4): only a cursor expires, never one at or above `floor`; `pruned` lists the seqs an
    expired cursor had not reached, and is empty otherwise. A forward cursor's list starts at
    the cursor; a backward cursor below `floor` expires with every seq up to it."""
    if page.expired != bool(page.pruned):
        raise NonConforming("expired and pruned disagree")
    if not page.expired:
        # every seq up to the cursor, at or below the watermark: none when either is 0, and
        # then the cursor does not expire
        if cursor is not None and backward and cursor[0] < page.floor:
            if min(cursor[0], page.watermark) >= 1:
                raise NonConforming("a backward cursor below the floor did not expire")
        return
    if cursor is None or cursor[0] >= page.floor:
        raise NonConforming("a cursor that cannot expire expired")
    if len(page.pruned) != 1:
        raise NonConforming("pruned is not the one range the cursor had not reached")
    low, high = page.pruned[0]
    if backward and (low, high) != (1, min(cursor[0], page.watermark)):
        raise NonConforming("pruned is not every seq up to the backward cursor")
    if not backward and low != max(cursor[0], 1):  # seqs begin at 1
        raise NonConforming("pruned does not start at the forward cursor")


def _form_counts(
    answer: Mapping[str, Any], watermark: int, floor: int, asked: Any
) -> dict[str, Any]:
    """`counts` by job and the transitions the request named, and the coverage of the whole
    range they count, `floor` to the watermark (control-protocol ss4)."""
    for name in ("counts", "counts_covered", "counts_gaps"):
        if name not in answer:
            raise NonConforming(f"the answer has no {name}")
    counts = answer["counts"]
    if not isinstance(counts, Mapping):
        raise NonConforming("counts is not an object")
    typed: dict[str, dict[str, int]] = {}
    for job, by_transition in counts.items():
        if not isinstance(job, str) or not isinstance(by_transition, Mapping):
            raise NonConforming("counts is not by job and transition")
        typed[job] = {
            _form_text(name, "a counted transition"): _form_seq(number, "a count")
            for name, number in by_transition.items()
        }
        named = set(asked) if isinstance(asked, list) else set()
        if not set(typed[job]) <= named:
            raise NonConforming("counts names a transition the request did not")
    covered = _form_intervals(
        answer["counts_covered"], "counts_covered", ordered=True, watermark=watermark
    )
    gaps = _form_intervals(answer["counts_gaps"], "counts_gaps", ordered=True, watermark=watermark)
    union = _form_union((*covered, *gaps), "counts_covered and counts_gaps")
    whole = (floor, watermark) if floor <= watermark else None
    if union != whole:
        raise NonConforming("counts_covered and counts_gaps do not cover floor to the watermark")
    return {"counts": typed, "counts_covered": covered, "counts_gaps": gaps}
