"""The viewer decodes the schema of a `transitions` answer and fails fast (DL-541).

It checks that each field it reads is present and of its type, and refuses a malformed answer
whole with a diagnostic naming the verb, the field's path, what was expected and what came. It
trusts the engine for every relation between fields: those are held by the engine's own
conformance checker (`tests/transitions_conformance.py`), which the real-engine sweep below
runs beside the decoder.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from test_transitions import (  # noqa: E402
    _JIL,
    _as_previous_build,
    _close,
    _incarnation,
    _latest,
    _server,
)
from transitions_conformance import check_conformance  # noqa: E402

from dsl41.ir import lower_source  # noqa: E402
from dsl41.runner_adapters import FakeAdapter  # noqa: E402
from dsl41.runner_clock import VirtualClock  # noqa: E402
from dsl41.runner_journal import STORE_NAME  # noqa: E402
from dsl41.runner_startup import resume_run  # noqa: E402
from dsl41.runner_store import SqliteStore  # noqa: E402
from dsl41.runner_tui import MalformedPage, decode_refusal, decode_transitions  # noqa: E402

MAX = 2**63 - 1
FORWARD: dict[str, Any] = {"cmd": "transitions", "direction": "forward"}


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


def _refused_as(answer: Any, diagnostic: str, request: dict[str, Any] = FORWARD) -> None:
    with pytest.raises(MalformedPage) as caught:
        decode_transitions(answer, request)
    assert str(caught.value) == diagnostic


# ------------------------------------------------------------- conforming pages decode


def test_a_page_decodes_into_the_typed_fields_the_viewer_reads() -> None:
    page = decode_transitions(_page(), FORWARD)
    assert [entry.key for entry in page.entries] == [(3, 0), (3, 1)]
    assert page.entries[1].run_number == 1 and page.entries[0].at == datetime(2026, 7, 1, 8)
    assert (page.next, page.more, page.watermark, page.history) == ((3, 1), True, 9, "h")


@pytest.mark.parametrize(
    ("change", "request_fields"),
    [
        ({}, {"direction": "backward"}),
        ({"entries": [], "next": None, "more": False}, {}),
        ({"entries": [_entry(3, cut=True)], "next": [3, 0]}, {}),
        ({"gaps": [[4, 9]], "history": None}, {}),
        ({"continuous_through": 9}, {"history": "h"}),
        ({"extra": 1, "cut": True, "covered": "not read"}, {}),
    ],
    ids=["backward", "empty", "cut-entry", "gap-null-history", "continuity", "unread-fields"],
)
def test_a_conforming_page_of_each_kind_decodes(
    change: dict[str, Any], request_fields: dict[str, Any]
) -> None:
    decode_transitions(_page(**change), {**FORWARD, **request_fields})


@pytest.mark.parametrize(
    "change",
    [
        {"entries": [_entry(4), _entry(3)], "next": [9, 9]},  # keys out of order, next astray
        {"gaps": [[9, 1]], "expired": True, "floor": 50, "watermark": 2},  # a self-contradiction
    ],
    ids=["order-and-next", "intervals-and-expiry"],
)
def test_a_page_that_breaks_a_relation_and_keeps_the_schema_decodes(change: dict[str, Any]) -> None:
    """The viewer does not validate the engine: relations are the engine's to keep."""
    decode_transitions(_page(**change), FORWARD)


# ------------------------------------------------- the schema, one rule at a time


@pytest.mark.parametrize(
    ("answer", "diagnostic"),
    [
        ([1, 2], "transitions: (answer): expected an object, got list [1, 2]"),
        (_page(ok="yes"), "transitions: ok: expected true, got str 'yes'"),
        ({**_page(), "ok": None}, "transitions: ok: expected true, got NoneType None"),
        (
            {k: v for k, v in _page().items() if k != "baseline_id"},
            "transitions: baseline_id: expected a string, got nothing",
        ),
        (_page(entries="two"), "transitions: entries: expected a list, got str 'two'"),
        (
            _page(entries=[_entry(3), 7]),
            "transitions: entries[1]: expected an object, got int 7",
        ),
        (
            _page(entries=[_entry(3), _entry("4")]),
            "transitions: entries[1].seq: expected an integer, got str '4'",
        ),
        (
            _page(entries=[_entry(3, n=True)]),
            "transitions: entries[0].n: expected an integer, got bool True",
        ),
        (
            _page(entries=[{k: v for k, v in _entry(3).items() if k != "cause"}]),
            "transitions: entries[0].cause: expected a string, got nothing",
        ),
        (
            _page(entries=[_entry(3, at="2026-07-01T08:00:00+02:00")]),
            "transitions: entries[0].at: expected a naive UTC timestamp as `trace` writes it,"
            " got str '2026-07-01T08:00:00+02:00'",
        ),
        (
            _page(entries=[_entry(3, at="2026-07-01")]),
            "transitions: entries[0].at: expected a naive UTC timestamp as `trace` writes it,"
            " got str '2026-07-01'",
        ),
        (
            _page(entries=[_entry(3, job=None)]),
            "transitions: entries[0].job: expected a string, got NoneType None",
        ),
        (
            _page(entries=[_entry(3, run_number="1")]),
            "transitions: entries[0].run_number: expected an integer, got str '1'",
        ),
        (
            _page(entries=[_entry(3, start_source=1)]),
            "transitions: entries[0].start_source: expected a string, got int 1",
        ),
        (
            _page(entries=[_entry(3, cut="yes")]),
            "transitions: entries[0].cut: expected a boolean, got str 'yes'",
        ),
        (_page(next=[3]), "transitions: next: expected a key [seq, n], got list [3]"),
        (
            {k: v for k, v in _page().items() if k != "next"},
            "transitions: next: expected a key [seq, n], got nothing",
        ),
        (_page(more=1), "transitions: more: expected a boolean, got int 1"),
        (_page(floor="1"), "transitions: floor: expected an integer, got str '1'"),
        (_page(expired=None), "transitions: expired: expected a boolean, got NoneType None"),
        (
            _page(pruned=[[1]]),
            "transitions: pruned[0]: expected a seq interval [low, high], got list [1]",
        ),
        (_page(gaps=None), "transitions: gaps: expected a list, got NoneType None"),
        (
            _page(gaps=[["x", 1]]),
            "transitions: gaps[0]: expected a seq interval [low, high], got list ['x', 1]",
        ),
        (_page(watermark=True), "transitions: watermark: expected an integer, got bool True"),
        (_page(history=7), "transitions: history: expected a string, got int 7"),
        (
            {k: v for k, v in _page().items() if k != "history"},
            "transitions: history: expected a string, got nothing",
        ),
    ],
    ids=repr,
)
def test_a_schema_failure_is_refused_with_its_field_path_expected_and_found(
    answer: Any, diagnostic: str
) -> None:
    _refused_as(answer, diagnostic)


def test_the_continuity_field_is_read_when_a_history_was_sent() -> None:
    _refused_as(
        _page(),
        "transitions: continuous_through: expected an integer, got nothing",
        {**FORWARD, "history": "h"},
    )


def test_a_refusal_s_code_and_error_are_strings() -> None:
    assert decode_refusal({"ok": False, "code": "no_journal", "error": " why "}).error == "why"
    assert decode_refusal({"ok": False, "error": "replayed"}).code is None  # code may be omitted
    with pytest.raises(MalformedPage, match="transitions: code: expected a string, got list"):
        decode_refusal({"ok": False, "code": [], "error": "bad"})
    with pytest.raises(MalformedPage, match="transitions: error: expected a string, got int 7"):
        decode_refusal({"ok": False, "code": "x", "error": 7})
    # the envelope always carries `error` (control-protocol ss2)
    with pytest.raises(MalformedPage, match="transitions: error: expected a string, got nothing"):
        decode_refusal({"ok": False, "code": "x"})


def test_a_diagnostic_is_capped_with_an_ellipsis() -> None:
    nested: Any = 0
    for _ in range(40):
        nested = [nested] * 6
    with pytest.raises(MalformedPage) as caught:
        decode_transitions(_page(watermark=nested), FORWARD)
    text = str(caught.value)
    assert len(text) == 200 and text.endswith("…")
    assert text.startswith("transitions: watermark: expected an integer, got list ")


# ------------------------------------------------- not stricter than the real engine


async def _sweep(run_root: Path, tokens: list[str | None]) -> int:
    """Resume the root and decode every page of a sweep: both directions, each filter,
    several limits, cursors from null past the watermark, with no history, the current one and
    each of `tokens`, paging on through `next`."""
    engine = await resume_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=_latest(run_root) + timedelta(minutes=1)),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    await engine.run_until_quiescent(engine.clock.now())
    server = _server(engine, run_root)
    first = await server._respond({"cmd": "transitions"})
    watermark, floor = first["watermark"], first["floor"]
    current = (await server._respond({"cmd": "status"}))["history"]
    cursors: list[list[int] | None] = [
        None,
        [0, 0],
        [max(floor - 1, 0), 0],
        [floor, 0],
        [(floor + watermark) // 2, 0],
        [watermark, 0],
        [watermark + 5, 0],
        [MAX, MAX],
    ]
    decoded = 0
    for history in [None, current, *tokens]:
        for direction in ("forward", "backward"):
            for filters in ({}, {"job": "j"}, {"transition": "INACTIVE->STARTING"}):
                for limit in (0, 2, 500):
                    for cursor in cursors:
                        request: dict[str, Any] = {
                            "cmd": "transitions",
                            "direction": direction,
                            "limit": limit,
                            "cursor": cursor,
                            **filters,
                        }
                        if history is not None:
                            request["history"] = history
                        for _ in range(12):
                            answer = await server._respond(copy.deepcopy(request))
                            page = decode_transitions(answer, request)
                            check_conformance(answer, request)
                            decoded += 1
                            if not page.more or page.next is None or limit == 0:
                                break
                            request = {**request, "cursor": list(page.next)}
    counted = {"cmd": "transitions", "counts": ["INACTIVE->STARTING", "STARTING->RUNNING"]}
    answer = await server._respond(dict(counted))
    decode_transitions(answer, counted)
    check_conformance(answer, counted)
    await _close(engine)
    return decoded


def _tokens(run_root: Path) -> list[str]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        return [history for history, _ in store.histories()]


def test_every_page_a_real_engine_answers_decodes_after_a_prune(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True))
    asyncio.run(_incarnation(run_root, snapshots=2, prune=True))
    assert asyncio.run(_sweep(run_root, [*_tokens(run_root), "never-had"])) > 500


def test_every_page_decodes_after_a_restart_named_with_the_earlier_token(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    asyncio.run(_incarnation(run_root, genesis=True, snapshots=1))
    earlier = _tokens(run_root)
    asyncio.run(_incarnation(run_root))
    assert asyncio.run(_sweep(run_root, earlier)) > 500


def test_every_page_decodes_over_an_earlier_build_s_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "run"
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(run_root, genesis=True, snapshots=1))
    asyncio.run(_incarnation(run_root))
    assert asyncio.run(_sweep(run_root, _tokens(run_root))) > 500


def test_every_page_decodes_on_a_restored_copy(tmp_path: Path) -> None:
    run_root, copy_root = tmp_path / "run", tmp_path / "copy"
    asyncio.run(_incarnation(run_root, genesis=True))
    shutil.copytree(run_root, copy_root)
    asyncio.run(_incarnation(run_root))  # the original goes on under a history the copy lacks
    abandoned = _tokens(run_root)
    assert asyncio.run(_sweep(copy_root, [*abandoned, "unknown"])) > 500
