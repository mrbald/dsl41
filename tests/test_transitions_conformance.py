"""The engine's `transitions` answers hold every property the contract states (DL-541).

`tests/transitions_conformance.py` checks an answer against control-protocol ss4
`transitions`, relations included. The terminal UI does not: it checks the schema of what it
reads and trusts the engine. These are the checker's own cases, one per property, and its
table of the contract's sentences; the real-engine sweep and `tests/test_transitions.py` use
it on the engine's answers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from transitions_conformance import NonConforming, check_conformance, check_refusal

from dsl41.runner_store import TransitionQuery, TransitionRead
from dsl41.runner_transitions import Request, fit

MAX = 2**63 - 1


def _entry(seq: int, n: int = 0, **fields: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "n": n,
        "at": "2026-07-01T08:00:00",
        "job": "j",
        "transition": "INACTIVE->STARTING",
        "cause": f"cause {seq}",
        **fields,
    }


def _page(**fields: Any) -> dict[str, Any]:
    """A conforming forward page from the oldest row: two entries, up to watermark 9."""
    page: dict[str, Any] = {
        "ok": True,
        "baseline_id": "b",
        "applied_index": 4,
        "entries": [_entry(3), _entry(3, 1, run_number=1)],
        "next": [3, 1],
        "more": True,
        "floor": 1,
        "expired": False,
        "pruned": [],
        "covered": [[1, 3]],
        "gaps": [],
        "watermark": 9,
        "history": "h",
    }
    page.update(fields)
    return page


FORWARD: dict[str, Any] = {"cmd": "transitions", "direction": "forward"}


def _malformed(answer: Any, request: dict[str, Any] = FORWARD) -> None:
    with pytest.raises(NonConforming):
        check_conformance(answer, request)


# ------------------------------------------------------------ conforming pages decode


def test_a_forward_page_decodes_into_typed_entries() -> None:
    page = check_conformance(_page(), FORWARD)
    assert [entry.key for entry in page.entries] == [(3, 0), (3, 1)]
    assert page.entries[1].run_number == 1 and page.entries[0].run_number is None
    assert page.entries[0].at == datetime(2026, 7, 1, 8)
    assert (page.next, page.more, page.watermark, page.history) == ((3, 1), True, 9, "h")
    assert page.continuous_through is None and page.counts is None


def test_a_backward_page_is_ascending_and_its_next_is_its_oldest_key() -> None:
    request = {"cmd": "transitions", "direction": "backward", "cursor": [8, 0], "limit": 2}
    page = check_conformance(
        _page(entries=[_entry(5), _entry(7)], next=[5, 0], covered=[[5, 8]]), request
    )
    assert page.next == (5, 0) and [e.key for e in page.entries] == [(5, 0), (7, 0)]


@pytest.mark.parametrize(
    "request_fields",
    [{"job": "j"}, {"transition": "INACTIVE->STARTING"}, {"job": "j", "limit": 2}],
)
def test_a_filtered_page_decodes(request_fields: dict[str, Any]) -> None:
    check_conformance(_page(), {**FORWARD, **request_fields})


def test_a_page_with_counts_decodes_when_counts_were_asked() -> None:
    page = check_conformance(
        _page(
            counts={"j": {"INACTIVE->STARTING": 2}},
            counts_covered=[[1, 9]],
            counts_gaps=[],
        ),
        {**FORWARD, "counts": ["INACTIVE->STARTING"]},
    )
    assert page.counts == {"j": {"INACTIVE->STARTING": 2}}


def test_a_limit_of_zero_may_say_more_and_keeps_the_cursor() -> None:
    request = {**FORWARD, "cursor": [2, 0], "limit": 0}
    # the span is the cursor's seq alone, as the engine answers it; the contract leaves its
    # far end unstated with no last entry, and the decoder does not check it there
    page = check_conformance(_page(entries=[], next=[2, 0], more=True, covered=[[2, 2]]), request)
    assert page.entries == () and page.more and page.next == (2, 0)


def test_an_empty_page_keeps_the_clamped_cursor() -> None:
    request = {**FORWARD, "cursor": [40, 3]}  # past the watermark: it goes on from it
    past = _page(entries=[], next=[9, MAX], more=False, covered=[])
    assert check_conformance(past, request).next == (9, MAX)
    empty = check_conformance(_page(entries=[], next=None, more=False, covered=[[1, 9]]), FORWARD)
    assert empty.next is None


def test_a_cut_entry_comes_alone_and_decodes() -> None:
    page = check_conformance(_page(entries=[_entry(3, job="lo", cut=True)], next=[3, 0]), FORWARD)
    assert page.entries[0].cut


def test_unordered_gaps_and_a_floor_above_the_watermark_decode() -> None:
    """Gaps need no order among them; a floor above the watermark leaves no seq for covered
    and gaps to name, since they lie at or above the floor (control-protocol ss4)."""
    page = check_conformance(
        _page(entries=[], next=None, more=False, covered=[[3, 3]], gaps=[[4, 9], [1, 2]]),
        FORWARD,
    )
    assert page.gaps == ((4, 9), (1, 2))
    above = _page(entries=[], next=None, more=False, covered=[], floor=12)
    assert check_conformance(above, FORWARD).floor == 12
    _malformed(_page(entries=[], next=None, more=False, covered=[], gaps=[[4, 9]], floor=12))


def test_fields_the_contract_does_not_name_are_ignored() -> None:
    check_conformance(_page(cut=True, epoch=1, extra={"x": 1}), FORWARD)
    check_conformance(_page(entries=[_entry(3, extra=1)], next=[3, 0]), FORWARD)


def test_the_continuity_field_is_read_when_a_token_was_sent() -> None:
    page = check_conformance(_page(continuous_through=9), {**FORWARD, "history": "h"})
    assert page.continuous_through == 9


# ------------------------------------------------------- each violation is malformed


@pytest.mark.parametrize(
    "change",
    [
        {"ok": False},
        {"ok": "yes"},
        {"baseline_id": None},
        {"applied_index": True},
        {"applied_index": "4"},
        {"watermark": True},
        {"watermark": -1},
        {"floor": "1"},
        {"more": 1},
        {"expired": None},
        {"entries": "two"},
        {"next": [3]},
        {"next": [3, True]},
        {"pruned": [[1]]},
        {"covered": [[3, 1]]},
        {"gaps": None},
        {"gaps": [[5, 4]]},
        {"gaps": [["1", 2]]},
        {"history": 7},
        {"gaps": [[8, 10]]},  # above the watermark, 9
        {"covered": [[1, 10]]},
        {"pruned": [[10, 12]]},
    ],
    ids=repr,
)
def test_a_header_field_out_of_form_is_malformed(change: dict[str, Any]) -> None:
    _malformed(_page(**change))


@pytest.mark.parametrize(
    "field",
    [
        "baseline_id",
        "applied_index",
        "watermark",
        "floor",
        "more",
        "expired",
        "entries",
        "next",
        "pruned",
        "covered",
        "gaps",
        "history",
    ],
)
def test_a_missing_header_field_is_malformed(field: str) -> None:
    answer = _page()
    del answer[field]
    _malformed(answer)


def test_an_answer_that_is_no_object_is_malformed() -> None:
    _malformed([1, 2])
    _malformed(None)


def test_the_continuity_field_is_required_when_a_token_was_sent() -> None:
    _malformed(_page(), {**FORWARD, "history": "h"})
    _malformed(_page(continuous_through=True), {**FORWARD, "history": "h"})


def test_counts_are_checked_when_counts_were_asked() -> None:
    asked = {**FORWARD, "counts": ["INACTIVE->STARTING"]}
    _malformed(_page(counts_covered=[], counts_gaps=[]), asked)  # no counts
    _malformed(_page(counts={"j": {"x": "2"}}, counts_covered=[], counts_gaps=[]), asked)
    _malformed(_page(counts={"j": {"x": 2}}, counts_gaps=[]), asked)  # no counts_covered
    _malformed(_page(counts={"j": {"x": 2}}, counts_covered=[[1, 10]], counts_gaps=[]), asked)


@pytest.mark.parametrize(
    "entry",
    [
        {k: v for k, v in _entry(3).items() if k != "seq"},
        {k: v for k, v in _entry(3).items() if k != "n"},
        {k: v for k, v in _entry(3).items() if k != "at"},
        {k: v for k, v in _entry(3).items() if k != "job"},
        {k: v for k, v in _entry(3).items() if k != "transition"},
        {k: v for k, v in _entry(3).items() if k != "cause"},
        _entry(3, n=True),
        _entry(3, at="2026-07-01T08:00:00+02:00"),
        _entry(3, at="yesterday"),
        _entry(3, job=None),
        _entry(3, start_source=None),
        _entry(3, run_number=True),
        _entry(3, refused_status=7),
        _entry(3, cut=False),
        7,
    ],
    ids=repr,
)
def test_an_entry_out_of_form_is_malformed(entry: Any) -> None:
    _malformed(_page(entries=[entry], next=[3, 0]))


def test_a_cut_entry_that_does_not_come_alone_is_malformed() -> None:
    _malformed(_page(entries=[_entry(3, cut=True), _entry(4)], next=[4, 0]))


@pytest.mark.parametrize(
    ("entries", "request_fields", "next_key"),
    [
        ([_entry(3), _entry(3)], {}, [3, 0]),  # a duplicated key
        ([_entry(4), _entry(3)], {}, [3, 0]),  # not ascending
        ([_entry(10)], {}, [10, 0]),  # above the watermark
        ([_entry(2**63)], {}, [2**63, 0]),  # past the 63-bit bound
        ([_entry(3)], {"cursor": [3, 0]}, [3, 0]),  # not after a forward cursor
        ([_entry(5)], {"direction": "backward", "cursor": [5, 0]}, [5, 0]),  # not before it
        ([_entry(3), _entry(4)], {"limit": 1}, [4, 0]),  # past the asked limit
    ],
    ids=[
        "duplicate",
        "descending",
        "above-watermark",
        "past-63-bits",
        "forward",
        "backward",
        "limit",
    ],
)
def test_a_key_out_of_form_or_off_the_request_is_malformed(
    entries: list[Any], request_fields: dict[str, Any], next_key: list[int]
) -> None:
    _malformed(_page(entries=entries, next=next_key), {**FORWARD, **request_fields})


def test_more_than_five_thousand_entries_is_malformed() -> None:
    entries = [_entry(seq) for seq in range(1, 5002)]
    _malformed(_page(entries=entries, next=[5001, 0], watermark=6000), {**FORWARD, "limit": 9999})


@pytest.mark.parametrize(
    ("request_fields", "next_key", "entries"),
    [
        ({}, [3, 0], None),  # forward: the last entry is (3, 1)
        ({"direction": "backward"}, [3, 1], None),  # backward: the first entry is (3, 0)
        ({"cursor": [2, 0]}, [3, 0], []),  # empty: the request's cursor
        ({}, [2, 0], []),  # empty with no cursor: null
    ],
)
def test_a_next_that_does_not_continue_the_page_is_malformed(
    request_fields: dict[str, Any], next_key: list[int], entries: list[Any] | None
) -> None:
    answer = _page(next=next_key)
    if entries is not None:
        answer["entries"] = entries
    _malformed(answer, {**FORWARD, **request_fields})


# ----------------------------------------- properties derived from the contract's sentences


def _malformed_as(answer: Any, reason: str, request: dict[str, Any] = FORWARD) -> None:
    with pytest.raises(NonConforming, match=reason):
        check_conformance(answer, request)


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "code": [], "error": "bad"},
        {"ok": False, "code": None, "error": "bad"},
        {"ok": False, "code": "invalid_argument", "error": 7},
    ],
    ids=["code-list", "code-null", "error-number"],
)
def test_a_refusal_s_envelope_out_of_form_is_malformed(answer: dict[str, Any]) -> None:
    with pytest.raises(NonConforming):
        check_refusal(answer)


def test_a_refusal_with_strings_or_no_code_decodes() -> None:
    assert check_refusal({"ok": False, "code": "no_journal", "error": " why "}).code == "no_journal"
    assert check_refusal({"ok": False, "error": "replayed"}).code is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            {
                "covered": [[1, 2]],
                "gaps": [[3, 3]],
                "entries": [_entry(3), _entry(4)],
                "next": [4, 0],
            },
            "outside the seqs",
        ),
        ({"covered": [[1, 2], [4, 4]], "gaps": []}, "unaccounted"),  # Astra's page: seq 3
        ({"covered": [[1, 3]], "gaps": [[3, 3]]}, "overlap"),
        (
            {"covered": [[2, 3]], "floor": 2, "entries": [_entry(1)], "next": [1, 0]},
            "an entry is below the floor",
        ),
        ({"covered": [[2, 3]], "gaps": [[1, 1]], "floor": 2}, "span"),
        ({"covered": [[1, 2]], "entries": [_entry(2)], "next": [2, 0]}, None),  # conforming
    ],
    ids=["entry-outside", "hole", "overlap", "entry-below-floor", "gap-below-floor", "fits"],
)
def test_covered_and_gaps_account_for_the_page_s_seqs(
    change: dict[str, Any], reason: str | None
) -> None:
    if reason is None:
        check_conformance(_page(**change), FORWARD)
    else:
        _malformed_as(_page(**change), reason)


@pytest.mark.parametrize(
    ("request_fields", "change", "reason"),
    [
        # forward from cursor [2, 0], nothing more: the span is 2 to the watermark
        ({"cursor": [2, 0]}, {"more": False, "covered": [[2, 8]]}, "span"),
        ({"cursor": [2, 0]}, {"more": False, "covered": [[1, 9]]}, "span"),
        # forward with more: it ends at the last entry
        ({}, {"covered": [[1, 4]]}, "span"),
        # backward from cursor [9, 0], nothing more: the floor to the cursor
        (
            {"direction": "backward", "cursor": [9, 0]},
            {"more": False, "next": [3, 0], "covered": [[2, 9]]},
            "span",
        ),
        # backward with more: from its oldest entry
        ({"direction": "backward"}, {"next": [3, 0], "covered": [[2, 9]]}, "span"),
        # a null cursor: forward from the floor, backward from the watermark
        ({}, {"more": False, "covered": [[2, 9]]}, "span"),
        ({"direction": "backward"}, {"next": [3, 0], "more": False, "covered": [[1, 8]]}, "span"),
        # an empty page that says more spans its cursor's seq alone
        (
            {"cursor": [2, 0], "limit": 0},
            {"entries": [], "next": [2, 0], "covered": [[2, 9]]},
            "span",
        ),
        (
            {"direction": "backward", "cursor": [6, 0], "limit": 0},
            {"entries": [], "next": [6, 0], "covered": [[1, 6]]},
            "span",
        ),
        # a cursor past the watermark: forward spans nothing, backward ends at the watermark
        ({"cursor": [40, 0]}, {"entries": [], "next": [9, MAX], "more": False}, "span"),
        (
            {"direction": "backward", "cursor": [40, 0]},
            {"next": [3, 0], "more": False, "covered": [[1, 3]]},
            "span",
        ),
    ],
    ids=[
        "forward-start",
        "forward-end",
        "forward-more-end",
        "backward-floor",
        "backward-more-start",
        "null-forward",
        "null-backward",
        "empty-more-forward",
        "empty-more-backward",
        "past-watermark-forward",
        "past-watermark-backward",
    ],
)
def test_covered_and_gaps_start_and_end_where_the_contract_says(
    request_fields: dict[str, Any], change: dict[str, Any], reason: str
) -> None:
    _malformed_as(_page(**change), reason, {**FORWARD, **request_fields})


def test_count_coverage_is_floor_to_the_watermark_once() -> None:
    asked = {**FORWARD, "counts": ["INACTIVE->STARTING"]}
    whole = {"counts": {"j": {"INACTIVE->STARTING": 2}}, "counts_gaps": []}
    check_conformance(_page(**whole, counts_covered=[[1, 9]]), asked)
    _malformed_as(_page(**whole, counts_covered=[[1, 8]]), "floor to the watermark", asked)
    _malformed_as(_page(**whole, counts_covered=[[1, 9], [5, 5]]), "overlap", asked)


def test_counts_answer_only_a_request_that_names_them() -> None:
    counts = {"counts": {"j": {"X": 1}}, "counts_covered": [[1, 9]], "counts_gaps": []}
    _malformed_as(_page(**counts), "did not name")
    _malformed_as(_page(**counts), "did not", {**FORWARD, "counts": ["Y"]})


def _empty(**fields: Any) -> dict[str, Any]:
    """An empty, final page: nothing more, its span accounted for by `covered`."""
    return _page(entries=[], more=False, **fields)


@pytest.mark.parametrize(
    ("request_fields", "page", "reason"),
    [
        ({"cursor": [4, 0]}, _empty(next=[4, 0], covered=[[4, 9]], pruned=[[3, 3]]), "disagree"),
        ({}, _empty(next=None, covered=[[1, 9]], expired=True), "disagree"),
        ({}, _empty(next=None, covered=[[1, 9]], expired=True, pruned=[[1, 2]]), "cannot expire"),
        (
            {"cursor": [3, 0]},
            _empty(next=[3, 0], covered=[[3, 9]], floor=3, expired=True, pruned=[[3, 3]]),
            "cannot expire",
        ),
        (
            {"cursor": [2, 0]},
            _empty(next=[2, 0], covered=[[3, 9]], floor=3, expired=True, pruned=[[1, 4]]),
            "start at",
        ),
        (
            {"cursor": [2, 0]},
            _empty(next=[2, 0], covered=[[7, 9]], floor=7, expired=True, pruned=[[2, 3], [5, 6]]),
            "one range",
        ),
        (
            {"direction": "backward", "cursor": [4, 0]},
            _empty(next=[4, 0], covered=[], floor=6),
            "did not expire",
        ),
        (
            {"direction": "backward", "cursor": [4, 0]},
            _empty(next=[4, 0], covered=[], floor=6, expired=True, pruned=[[2, 4]]),
            "every seq up to",
        ),
    ],
    ids=[
        "pruned-not-expired",
        "expired-nothing",
        "null-cursor",
        "at-floor",
        "forward-start",
        "two-ranges",
        "backward-not-expired",
        "backward-range",
    ],
)
def test_expired_and_pruned_agree_with_each_other_and_the_request(
    request_fields: dict[str, Any], page: dict[str, Any], reason: str
) -> None:
    _malformed_as(page, reason, {**FORWARD, **request_fields})


def test_expired_pages_as_the_contract_states_them_decode() -> None:
    forward = _empty(next=[2, 0], covered=[[3, 9]], floor=3, expired=True, pruned=[[2, 2]])
    assert check_conformance(forward, {**FORWARD, "cursor": [2, 0]}).expired
    backward = _empty(next=[4, 0], covered=[], floor=6, expired=True, pruned=[[1, 4]])
    request = {**FORWARD, "direction": "backward", "cursor": [4, 0]}
    assert check_conformance(backward, request).pruned == ((1, 4),)


def test_continuous_through_is_at_or_below_the_watermark() -> None:
    _malformed_as(_page(continuous_through=10), "above the watermark", {**FORWARD, "history": "h"})


@pytest.mark.parametrize(
    "at", ["20260701", "2026-07-01", "2026-07-01 08:00:00", "2026-07-01T08:00"]
)
def test_an_at_not_written_as_trace_writes_it_is_malformed(at: str) -> None:
    _malformed_as(_page(entries=[_entry(3, at=at)], next=[3, 0]), "as `trace` writes")


def test_a_request_without_direction_is_read_forward() -> None:
    """A page for a request that names no direction holds the rows after its cursor."""
    _malformed_as(
        _page(entries=[_entry(3)], next=[3, 0]),
        "wrong side",
        {"cmd": "transitions", "cursor": [3, 0]},
    )


# ------------------------------------------------------------------- the closed list
#
# One row per sentence of control-protocol ss4 `transitions` (and the two ss2 sentences on an
# answer's envelope and read header it relies on) that states a property of an answer: the
# line, the sentence, the decoder's check and the case that violates it. A sentence about
# content (what the store holds) is "content, not checked"; one about the engine, not the
# answer, is "engine". This table is the closed list of the decoder's form (DL-541).

FORM, CONTENT, ENGINE = "form", "content, not checked", "engine"

CONTRACT: tuple[tuple[int, str, str, str, str], ...] = (
    (
        100,
        '{"ok": false, "code": "<code>", "error": "<message>"}',
        FORM,
        "a refusal's code and error are strings when present",
        "test_a_refusal_s_envelope_out_of_form_is_malformed",
    ),
    (
        100,
        'The answer to a request is {"ok": true, …} or {"ok": false, …}',
        FORM,
        "ok is true for a page; anything else is malformed",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (
        105,
        "Routed responses carry the read header: baseline_id and applied_index",
        FORM,
        "baseline_id a string, applied_index an integer",
        "test_a_missing_header_field_is_malformed",
    ),
    (835, "The trace entries the store committed with their records", CONTENT, "", ""),
    (836, "For a run root, the read and the shaping … run on a thread", ENGINE, "", ""),
    (839, "Shutdown stops a read in flight", ENGINE, "", ""),
    (
        846,
        "A page holds the rows after it (forward) or before it (backward)",
        FORM,
        "every key on the asked side of the request's cursor",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[forward]",
    ),
    (
        846,
        "two integers from 0 to 2^63 - 1",
        FORM,
        "keys within the 63-bit bounds",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[past-63-bits]",
    ),
    (847, "Null starts at the oldest row (forward) or the newest (backward)", CONTENT, "", ""),
    (
        849,
        "direction: forward (the default) or backward",
        FORM,
        "a request without direction is read as forward",
        "test_a_request_without_direction_is_read_forward",
    ),
    (
        850,
        "limit: … 500 by default, cut to 5000",
        FORM,
        "at most the asked limit (500 by default) and 5000 entries",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[limit]",
    ),
    (
        850,
        "cut to 5000",
        FORM,
        "at most 5000 entries",
        "test_more_than_five_thousand_entries_is_malformed",
    ),
    (851, "job, transition: exact filters", CONTENT, "", ""),
    (855, "A malformed field answers invalid_argument naming it", CONTENT, "", ""),
    (855, "A run with no store answers no_journal", CONTENT, "", ""),
    (856, "Counts whose answer alone passes the byte cap answer invalid_argument", CONTENT, "", ""),
    (859, "every request of that run answers transitions_unavailable", CONTENT, "", ""),
    (
        880,
        "watermark, the journal seq it has reached",
        FORM,
        "watermark an integer seq",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (
        881,
        "applied_index, the index applied then",
        FORM,
        "applied_index an integer",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (
        882,
        "floor, the oldest journal seq it holds",
        FORM,
        "floor an integer; no entry below it",
        "test_covered_and_gaps_account_for_the_page_s_seqs[entry-below-floor]",
    ),
    (
        884,
        "Every seq the answer names (entries, next, the intervals, the counts) is at or below "
        "the watermark",
        FORM,
        "entries, next, every interval and continuous_through at or below the watermark",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[above-watermark]",
    ),
    (
        884,
        "Every seq the answer names … at or below the watermark",
        FORM,
        "continuous_through at or below the watermark",
        "test_continuous_through_is_at_or_below_the_watermark",
    ),
    (886, "A prune between pages … can remove rows the cursor had not reached", CONTENT, "", ""),
    (
        887,
        "A forward cursor then expires: expired is true and pruned lists … the seqs from the "
        "cursor to the newest removed row",
        FORM,
        "expired exactly when pruned lists seqs; one range, from the cursor",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[forward-start]",
    ),
    (
        887,
        "expired is true and pruned lists",
        FORM,
        "expired agrees with pruned",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[pruned-not-expired]",
    ),
    (
        887,
        "pruned lists … the seqs",
        FORM,
        "pruned is one range",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[two-ranges]",
    ),
    (889, "A forward cursor at the last row of floor - 1 … does not expire", CONTENT, "", ""),
    (
        890,
        "A backward cursor below floor expires with every seq up to it",
        FORM,
        "pruned is 1 to the cursor's seq (at or below the watermark)",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[backward-range]",
    ),
    (
        890,
        "A backward cursor below floor expires",
        FORM,
        "it is expired",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[backward-not-expired]",
    ),
    (
        894,
        "A cursor at or above floor never expires",
        FORM,
        "not expired at or above floor",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[at-floor]",
    ),
    (
        887,
        "A forward cursor … expires",
        FORM,
        "only a cursor expires",
        "test_expired_and_pruned_agree_with_each_other_and_the_request[null-cursor]",
    ),
    (
        896,
        "entries are in key order whatever the direction",
        FORM,
        "keys unique and strictly ascending in both directions",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[descending]",
    ),
    (
        896,
        "entries are in key order",
        FORM,
        "no key twice",
        "test_a_key_out_of_form_or_off_the_request_is_malformed[duplicate]",
    ),
    (
        896,
        "Each has the trace entry's fields",
        FORM,
        "seq, n, at, job, transition, cause present and typed",
        "test_an_entry_out_of_form_is_malformed",
    ),
    (
        897,
        "written as trace writes them (at included)",
        FORM,
        "at is a naive UTC time in Python's isoformat, a time included",
        "test_an_at_not_written_as_trace_writes_it_is_malformed",
    ),
    (
        897,
        "unset optional ones omitted",
        FORM,
        "start_source, run_number, refused_status absent when unset, typed when present",
        "test_an_entry_out_of_form_is_malformed",
    ),
    (
        898,
        "with seq the journal seq of its record and n its ordinal there",
        FORM,
        "seq and n integers",
        "test_an_entry_out_of_form_is_malformed",
    ),
    (899, "A row is the entry as it was committed", CONTENT, "", ""),
    (
        902,
        "next is … the key of its last entry in that direction",
        FORM,
        "next is the newest key forward, the oldest backward",
        "test_a_next_that_does_not_continue_the_page_is_malformed",
    ),
    (
        903,
        "or the request's cursor when the page is empty",
        FORM,
        "an empty page's next is the request's cursor, or null without one",
        "test_a_next_that_does_not_continue_the_page_is_malformed",
    ),
    (
        904,
        "it is never above [watermark, 2^63 - 1]",
        FORM,
        "an empty page's next is the cursor clamped to the watermark",
        "test_an_empty_page_keeps_the_clamped_cursor",
    ),
    (
        905,
        "more says whether to ask again that way",
        FORM,
        "more a boolean",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (908, "The whole encoded answer … is at most 1 MiB", ENGINE, "", ""),
    (909, "A page may hold fewer entries than the limit", CONTENT, "", ""),
    (
        910,
        'An entry too large to fit alone comes alone … with "cut": true',
        FORM,
        "a cut entry is the page's only entry, and cut is true",
        "test_a_cut_entry_that_does_not_come_alone_is_malformed",
    ),
    (
        912,
        "and next passes it",
        FORM,
        "next is the cut entry's key",
        "test_a_next_that_does_not_continue_the_page_is_malformed",
    ),
    (
        914,
        "covered and gaps are seq intervals, both ends included",
        FORM,
        "lists of integer pairs, low not above high",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (
        914,
        "over the seqs the page spans at or above floor",
        FORM,
        "together one range, disjoint, at or above floor, holding every entry",
        "test_covered_and_gaps_account_for_the_page_s_seqs[hole]",
    ),
    (
        914,
        "over the seqs the page spans",
        FORM,
        "covered and gaps do not overlap",
        "test_covered_and_gaps_account_for_the_page_s_seqs[overlap]",
    ),
    (
        914,
        "at or above floor",
        FORM,
        "no interval below floor",
        "test_covered_and_gaps_account_for_the_page_s_seqs[gap-below-floor]",
    ),
    (
        915,
        "from the cursor to its last entry, or to the end of the retained journal when "
        "nothing remains that way",
        FORM,
        "the range starts and ends there where the contract states the end exactly",
        "test_covered_and_gaps_start_and_end_where_the_contract_says",
    ),
    (921, "A gap holds seqs with no complete rows", CONTENT, "", ""),
    (
        923,
        "counts answers only a request that names it",
        FORM,
        "counts present exactly when asked, naming only asked transitions",
        "test_counts_answer_only_a_request_that_names_them",
    ),
    (
        924,
        "the raw number of retained rows … by job and transition",
        FORM,
        "counts is job to transition to integer",
        "test_counts_are_checked_when_counts_were_asked",
    ),
    (
        925,
        "counts_covered and counts_gaps are the coverage of that whole range, floor to the "
        "watermark",
        FORM,
        "together floor to the watermark, once",
        "test_count_coverage_is_floor_to_the_watermark_once",
    ),
    (
        930,
        "history identifies the store's history",
        FORM,
        "history a string (or null)",
        "test_a_header_field_out_of_form_is_malformed",
    ),
    (933, "A restored copy of a root forgets the histories it never saw", CONTENT, "", ""),
    (
        934,
        "When the request names a history, continuous_through is the last seq at which that "
        "history and the store's agree",
        FORM,
        "present and an integer when a history was sent",
        "test_the_continuity_field_is_required_when_a_token_was_sent",
    ),
    (935, "the watermark for the current one, the seq before the next start …", CONTENT, "", ""),
    (940, "What the terminal UI checks (the summary of the rows above)", ENGINE, "", ""),
    (
        956,
        "ignores fields it does not know (DL-217)",
        FORM,
        "unknown fields ignored",
        "test_fields_the_contract_does_not_name_are_ignored",
    ),
    (
        891,
        "A backward cursor below floor at seq 0, or with the watermark at 0, … does not expire",
        FORM,
        "it is not expired; below the floor with a seq up to it, it is",
        "test_a_cursor_at_seq_0_expires_as_the_completed_sentences_say[backward-seq-0]",
    ),
    (
        893,
        "A forward cursor at seq 0 lists from seq 1, the first journal seq",
        FORM,
        "an expired forward cursor's pruned starts at max(its seq, 1)",
        "test_a_cursor_at_seq_0_expires_as_the_completed_sentences_say[forward-seq-0]",
    ),
    (
        916,
        "For a null cursor the span starts at floor (forward) or ends at the watermark (backward)",
        FORM,
        "covered and gaps start at floor, or end at the watermark",
        "test_covered_and_gaps_start_and_end_where_the_contract_says[null-backward]",
    ),
    (
        918,
        "A forward cursor past the watermark spans no seq … a backward one spans up to the "
        "watermark",
        FORM,
        "covered and gaps empty forward; ending at the watermark backward",
        "test_covered_and_gaps_start_and_end_where_the_contract_says[past-watermark-forward]",
    ),
    (
        919,
        "A page that holds no entry and says more spans one seq",
        FORM,
        "covered and gaps hold the span's one seq",
        "test_covered_and_gaps_start_and_end_where_the_contract_says[empty-more-forward]",
    ),
    (
        930,
        "It is null when the store holds no history",
        FORM,
        "history a string or null",
        "test_a_header_field_out_of_form_is_malformed",
    ),
)


def test_every_form_row_of_the_contract_names_an_existing_violating_case() -> None:
    names = globals()
    assert CONTRACT
    for line, sentence, kind, check, case in CONTRACT:
        assert kind in (FORM, CONTENT, ENGINE), (line, sentence)
        if kind != FORM:
            assert not case, (line, sentence)
            continue
        assert check and case, (line, sentence)
        function, _, param = case.partition("[")
        assert function in names and callable(names[function]), case
        if param:
            ids = [
                i
                for mark in getattr(names[function], "pytestmark", [])
                if mark.name == "parametrize"
                for i in (mark.kwargs.get("ids") or [])
            ]
            assert param.rstrip("]") in ids, case


@pytest.mark.parametrize(
    ("request_fields", "page"),
    [
        ({}, _page(more=False, covered=[[1, 9]])),  # a null cursor forward: floor to watermark
        ({"direction": "backward"}, _page(next=[3, 0], more=False, covered=[[1, 9]])),
        ({"cursor": [2, 0], "limit": 0}, _page(entries=[], next=[2, 0], covered=[[2, 2]])),
        (
            {"direction": "backward", "cursor": [6, 0], "limit": 0},
            _page(entries=[], next=[6, 0], covered=[[6, 6]]),
        ),
        ({"cursor": [40, 0]}, _page(entries=[], next=[9, MAX], more=False, covered=[])),
        (
            {"direction": "backward", "cursor": [40, 0]},
            _page(next=[3, 0], more=False, covered=[[1, 9]]),
        ),
    ],
    ids=[
        "null-forward",
        "null-backward",
        "empty-more-forward",
        "empty-more-backward",
        "past-watermark-forward",
        "past-watermark-backward",
    ],
)
def test_the_completed_span_sentences_decode(
    request_fields: dict[str, Any], page: dict[str, Any]
) -> None:
    check_conformance(page, {**FORWARD, **request_fields})


@pytest.mark.parametrize(
    ("request_fields", "page", "reason"),
    [
        (
            {"direction": "backward", "cursor": [0, 3]},
            _empty(next=[0, 3], covered=[], floor=2, expired=True, pruned=[[1, 1]]),
            "every seq up to",
        ),
        (
            {"cursor": [0, 0]},
            _empty(next=[0, 0], covered=[[3, 9]], floor=3, expired=True, pruned=[[0, 2]]),
            "start at",
        ),
    ],
    ids=["backward-seq-0", "forward-seq-0"],
)
def test_a_cursor_at_seq_0_expires_as_the_completed_sentences_say(
    request_fields: dict[str, Any], page: dict[str, Any], reason: str
) -> None:
    _malformed_as(page, reason, {**FORWARD, **request_fields})
    if request_fields.get("direction") == "backward":
        check_conformance({**page, "expired": False, "pruned": []}, {**FORWARD, **request_fields})
    else:
        check_conformance({**page, "pruned": [[1, 2]]}, {**FORWARD, **request_fields})


def test_the_engine_s_shaping_with_no_history_and_a_watermark_of_0_decodes() -> None:
    """The producer's own shaping (`runner_transitions.fit`) over a read with no history and
    a routing watermark of 0: `history` is null, and a backward cursor below the floor with
    no seq up to it at or below the watermark does not expire."""
    read = TransitionRead(rows=[], first_seq=6, coverage=[], histories=[], pruned_through=None)
    for cursor, backward in (((3, 0), True), ((0, 0), True), ((0, 0), False), (None, False)):
        query = TransitionQuery(cursor, backward, 500, upto=0)
        answer = {
            "ok": True,
            **fit(read, Request(query, None), header={"baseline_id": "b", "applied_index": 0}),
        }
        request: dict[str, Any] = {
            "cmd": "transitions",
            "cursor": None if cursor is None else list(cursor),
            "direction": "backward" if backward else "forward",
        }
        page = check_conformance(answer, request)
        assert page.history is None and not page.expired
