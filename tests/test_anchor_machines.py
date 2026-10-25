"""anchor_head and period_row as statecharts (docs/statechart-engine.md §16, DL-336).

Normative spec: `docs/period-model.md` ss1.3 (the anchor, its moves and the
registry), PR-02c, and ss11 step 4. Both charts are well formed and draw no
warning. Their state lives in `anchor.json`, which no process owns, so each
move builds its executor from the file read under the anchor lock and drops it
with the method (`boundary._Move`). The generated completeness tests run a
witness per head and row state against tables written from ss1.3 and ss11,
not from the rows. Every event a poster sends is in its chart's alphabet, and
the posters drive the real functions. A refusal row is the method's
`EngineError`, before any write; a gap is a `StatechartError`, before any
write too, and it stops `dsl41 run` with exit 7 at the seal, at resume and at
genesis.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

import pytest
import statechart_completeness as sc
from test_boundary import (
    C1_JIL,
    C2_JIL,
    T0,
    _catalog,
    _close,
    _crash_at,
    _genesis,
    _request,
    _resume,
    _seal,
    _stage,
    _without_row,
)

from dsl41.boundary import (
    ANCHOR_HEAD,
    PERIOD_ROW,
    Anchor,
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    PeriodRow,
    RowTag,
    _Move,
    claim_id_for,
    default_anchor_dir,
    read_seal,
    row_tag,
)
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event
from dsl41.period import wal_path
from dsl41.runner import Engine
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import read_journal
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    Executor,
    Outcome,
    Row,
    Statechart,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)

ESTATE = "estate-anchor-machines"
SEAL_A = "sha256:" + "a" * 64
HEAD_TAGS = ("open", "closed", "claimed")
ROW_TAGS = ("provisional", "durable", "attested")


@pytest.fixture
def anchor(tmp_path: Path) -> Iterator[EstateAnchor]:
    handle = EstateAnchor(tmp_path / "anchor")
    handle.acquire()
    yield handle
    handle.release()


#: one post to an anchor chart: (machine, event, rows fired, refusal codes)
type _Post = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[_Post]:
    """Record every post to an anchor chart, from the executor's outcomes.

    Each move builds its own executor, so the spy sits on the class."""
    posts: list[_Post] = []
    dispatch = Executor.dispatch

    def spy(self: Executor, key: Any, event: Any) -> list[Outcome]:
        outcomes = dispatch(self, key, event)
        for outcome in outcomes:
            machine = outcome.rows[0].split(".")[0] if outcome.rows else ""
            if machine in (ANCHOR_HEAD.name, PERIOD_ROW.name):
                fired = tuple(r for r in outcome.rows if not outcome.refusals)
                posts.append((machine, outcome.event.name, fired, outcome.refusals))
        return outcomes

    monkeypatch.setattr(Executor, "dispatch", spy)
    return posts


def _fired(posts: list[_Post]) -> set[str]:
    return {row for post in posts for row in post[2]}


def _private_hits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A broken copy of a chart records into a private directory, so its rows
    never count for the real chart's coverage."""
    directory = tmp_path / "hits"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(HITS_ENV, str(directory))


def _without(chart: Statechart, keep: Callable[[Row], bool]) -> Statechart:
    return replace(chart, rows=tuple(row for row in chart.rows if keep(row)))


# ---- the declarations


def test_the_charts_are_well_formed_and_draw_no_warning() -> None:
    for chart in (ANCHOR_HEAD, PERIOD_ROW):
        assert well_formed(chart) == []
        assert chart_warnings(chart) == []
        assert chart.clock == "logical"
        assert chart in MACHINES
        assert not chart.guards and not chart.actions  # the method does the work
        assert all(row.mark is None for row in chart.rows)
    assert alphabet(ANCHOR_HEAD) == sorted(
        ["create_open", "close_period", "claim_successor", "open_claimed", "reclaim"]
    )
    assert alphabet(PERIOD_ROW) == sorted(
        ["create_open", "close_period", "open_claimed", "finalize", "attest"]
    )
    # rows .01 of both and .03 of the row went with `absent` (no file, no row,
    # no instance); the others keep their ids
    moves = {row.id for chart in (ANCHOR_HEAD, PERIOD_ROW) for row in chart.rows}
    assert {f"anchor_head.0{n}" for n in range(2, 7)} <= moves
    assert {"period_row.02", "period_row.04", "period_row.05"} <= moves
    assert not {"anchor_head.01", "period_row.01", "period_row.03"} & moves
    assert {row.code for row in ANCHOR_HEAD.rows if row.kind == "refusal"} == {
        "anchor_exists",
        "head_not_open",
        "head_not_closed",
        "head_not_claimed",
    }
    assert {row.code for row in PERIOD_ROW.rows if row.kind == "refusal"} == {
        "row_exists",
        "row_attested",
        "row_durable",
        "row_not_durable",
    }
    assert [row.id for row in ANCHOR_HEAD.rows if row.kind == "internal"] == ["anchor_head.06"]


def test_the_head_leaves_are_the_head_tags_and_the_row_leaves_its_flags() -> None:
    tags = {model.model_fields["state"].default for model in (OpenHead, ClosedHead, ClaimedHead)}
    assert tags == set(HEAD_TAGS)
    assert set(get_args(RowTag.__value__)) == {"absent", *ROW_TAGS}
    for tag in HEAD_TAGS:
        anchor = Anchor(estate_id=ESTATE, head=_head_of(tag))
        move = _Move(Path("anchor"), "witness", anchor)
        assert tag in move.machines.active(move.head)
    for tag in ROW_TAGS:
        row = PeriodRow(root="r", segment_durable=tag != "provisional", attested=tag == "attested")
        anchor = Anchor(estate_id=ESTATE, head=_head_of("open"), periods={"1": row})
        move = _Move(Path("anchor"), "witness", anchor, 1)
        assert tag in move.machines.active(move.row)


def _head_of(tag: str) -> OpenHead | ClosedHead | ClaimedHead:
    if tag == "open":
        return OpenHead(period_id=1, root="r")
    if tag == "closed":
        return ClosedHead(period_id=1, seal_digest=SEAL_A, closing_root="r")
    return ClaimedHead(claim_id="c", target_root="r")


@pytest.mark.parametrize(
    ("row", "tag"),
    [
        (None, "absent"),
        (PeriodRow(root="r"), "provisional"),
        (PeriodRow(root="r", segment_durable=True), "durable"),
        (PeriodRow(root="r", segment_durable=True, attested=True), "attested"),
    ],
)
def test_a_row_is_tagged_by_its_flags(row: PeriodRow | None, tag: str) -> None:
    assert row_tag(row) == tag


# ---- the abstraction (§13): witnesses reached through the anchor's own verbs


def _walk(anchor: EstateAnchor, root: Path, steps: tuple[str, ...]) -> None:
    for step in steps:
        match step:
            case "create":
                anchor.create_open(estate_id=ESTATE, root=root)
            case "finalize":
                anchor.finalize(1)
            case "close":
                anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
            case "attest":
                anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
            case "claim":
                anchor.claim_successor(
                    estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
                )
            case _:  # lose the claim file the head names
                head = anchor.require().head
                assert isinstance(head, ClaimedHead)
                anchor.claim_path(head.claim_id).unlink()


#: head witnesses: the claim file is the method's fact, never a chart's, so
#: the two claimed witnesses resolve alike (the method answers a repeat with
#: the file present before any post)
HEAD_WITNESSES: dict[str, tuple[str, ...]] = {
    "open": ("create", "finalize"),
    "closed": ("create", "finalize", "close"),
    "claimed": ("create", "finalize", "close", "claim"),
    "claimed, claim file gone": ("create", "finalize", "close", "claim", "lose"),
}
ROW_WITNESSES: dict[str, tuple[str, ...]] = {
    "provisional": ("create",),
    "durable": ("create", "finalize"),
    "attested": ("create", "finalize", "close", "attest"),
}


@pytest.fixture(scope="module")
def witnesses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One anchor directory per witness, reached once; a builder re-reads it."""
    base = tmp_path_factory.mktemp("witnesses")
    root = base / "run"
    root.mkdir()
    built: dict[str, Path] = {}
    for name, steps in {**HEAD_WITNESSES, **ROW_WITNESSES}.items():
        anchor = EstateAnchor(base / name.replace(" ", "_").replace(",", ""))
        anchor.acquire()
        try:
            _walk(anchor, root, steps)
        finally:
            anchor.release()
        built[name] = anchor.dir
    return built


def _builder(directory: Path, row: bool) -> sc.Builder:
    def build() -> tuple[Any, Any]:
        stored = EstateAnchor(directory).read()
        move = _Move(directory, "witness", stored, 1 if row else None)
        return move.machines, move.row if row else move.head

    return build


def _head_cell(witness: str, event: str) -> str:
    """The cell as period-model ss1.3 states it, written without the rows.

    Genesis creates over no anchor only (ss1.1): every head refuses it. Each
    of the other four moves has one source head: the seal closes an open
    head, the opener claims a closed one and opens its own claim, and the
    break-glass returns a claim to closed. A claimant that finds its own
    claim rewrites it; the head stays claimed."""
    state = witness.split(",")[0]
    if event == "create_open":
        return "refused anchor_exists"
    moves = {
        ("open", "close_period"): "anchor_head.02",
        ("closed", "claim_successor"): "anchor_head.03",
        ("claimed", "open_claimed"): "anchor_head.04",
        ("claimed", "reclaim"): "anchor_head.05",
        ("claimed", "claim_successor"): "anchor_head.06",
    }
    refused = {
        "close_period": "head_not_open",
        "claim_successor": "head_not_closed",
        "open_claimed": "head_not_claimed",
        "reclaim": "head_not_claimed",
    }
    return moves.get((state, event), f"refused {refused[event]}")


def _row_cell(witness: str, event: str) -> str:
    """The cell as period-model ss1.3 and ss11 state it, written without the rows.

    A row is inserted when a root first owns its period, so a row that exists
    refuses its insertion (genesis, a successor's opening). The close makes an
    unattested row durable at its seal; the finalize makes a provisional row
    durable; the audit attests a durable row only. An attested row moves no
    more."""
    if event in ("create_open", "open_claimed"):
        return "refused row_exists"
    if witness == "attested":
        return "refused row_attested"
    return {
        ("provisional", "close_period"): "period_row.02",
        ("durable", "close_period"): "period_row.02",
        ("provisional", "finalize"): "period_row.04",
        ("durable", "finalize"): "refused row_durable",
        ("provisional", "attest"): "refused row_not_durable",
        ("durable", "attest"): "period_row.05",
    }[(witness, event)]


def test_the_head_completeness_table_is_section_1_3(witnesses: dict[str, Path]) -> None:
    """Four witnesses by five events; nothing excluded."""
    assert ANCHOR_HEAD.exclusions == ()
    abstraction = sc.Abstraction(
        witnesses={name: _builder(witnesses[name], row=False) for name in HEAD_WITNESSES}
    )
    table = sc.completeness_table(ANCHOR_HEAD, abstraction)
    assert table == {
        (witness, event, sc.ANY): _head_cell(witness, event)
        for witness in HEAD_WITNESSES
        for event in alphabet(ANCHOR_HEAD)
    }
    assert len(table) == 20


def test_the_row_completeness_table_is_sections_1_3_and_11(witnesses: dict[str, Path]) -> None:
    """Three witnesses by five events; nothing excluded."""
    assert PERIOD_ROW.exclusions == ()
    abstraction = sc.Abstraction(
        witnesses={name: _builder(witnesses[name], row=True) for name in ROW_WITNESSES}
    )
    table = sc.completeness_table(PERIOD_ROW, abstraction)
    assert table == {
        (witness, event, sc.ANY): _row_cell(witness, event)
        for witness in ROW_WITNESSES
        for event in alphabet(PERIOD_ROW)
    }
    assert len(table) == 15


# ---- the methods read the executor's outcomes


def test_a_lineage_walk_fires_every_move_and_posts_only_the_alphabet(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    posts = _spy(monkeypatch)
    anchor.create_open(estate_id=ESTATE, root=root)
    assert posts == []  # no file, no instance: genesis posts nothing
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    claim = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    anchor.claim_path(claim.claim_id).unlink()
    anchor.claim_successor(estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root)
    anchor.reclaim(estate_id=ESTATE, claimed_actor="operator")
    anchor.claim_successor(estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root)
    anchor.open_claimed(claim_id=claim.claim_id, period_id=2, root=root)
    assert _fired(posts) == {
        "period_row.04",
        "anchor_head.02",
        "period_row.02",
        "period_row.05",
        "anchor_head.03",
        "anchor_head.06",
        "anchor_head.05",
        "anchor_head.04",
    }
    assert [post[3] for post in posts if post[3]] == []
    assert {post[1] for post in posts if post[0] == "anchor_head"} <= set(alphabet(ANCHOR_HEAD))
    assert {post[1] for post in posts if post[0] == "period_row"} <= set(alphabet(PERIOD_ROW))
    # the successor's row is written with its opening, so no row instance moved
    assert ("period_row", "open_claimed") not in {post[:2] for post in posts}
    stored = anchor.require()
    assert isinstance(stored.head, OpenHead) and row_tag(stored.row(2)) == "durable"


def test_an_idempotent_repeat_writes_nothing_and_posts_nothing(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    posts = _spy(monkeypatch)
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    assert len(posts) == 4  # the spy sees every post of the first pass: it can fail
    written = anchor.path.read_bytes()
    posts.clear()
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    assert anchor.path.read_bytes() == written
    assert posts == []
    claim = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    written = anchor.path.read_bytes()
    posts.clear()
    again = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    assert again == claim
    assert anchor.path.read_bytes() == written
    assert posts == []
    with pytest.raises(EngineError, match="already exists and is not ours"):
        anchor.create_open(estate_id=ESTATE, root=root)  # not genesis interrupted: refused
    assert [post[3] for post in posts] == [("anchor_exists",)]


def _claimed_with_successor_row(anchor: EstateAnchor, root: Path) -> str:
    """A claimed head over a registry that already holds the successor's row.

    The row is written in the same write as `claimed -> open` (PR-02c), so no crash
    leaves this; a hand-edited anchor does."""
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    claim = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    stored = anchor.require()
    anchor.write(
        stored.model_copy(
            update={"periods": stored.with_row(2, PeriodRow(root=str(root), segment_durable=True))}
        )
    )
    return claim.claim_id


def test_opening_over_an_existing_successor_row_is_refused_and_writes_nothing(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head would move (.04), and the row refuses (.07): the method raises
    before its write, so neither moves."""
    root = tmp_path / "run"
    root.mkdir()
    claim_id = _claimed_with_successor_row(anchor, root)
    before = anchor.path.read_bytes()
    posts = _spy(monkeypatch)
    with pytest.raises(
        EngineError, match=r"the registry already holds a row for it .* \(period-model ss1\.3, PR"
    ):
        anchor.open_claimed(claim_id=claim_id, period_id=2, root=root)
    assert anchor.path.read_bytes() == before
    assert [(p[0], p[2], p[3]) for p in posts] == [
        ("anchor_head", ("anchor_head.04",), ()),
        ("period_row", (), ("row_exists",)),
    ]


def test_closing_over_an_attested_row_is_refused_and_writes_nothing(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """An open head over an attested row is a hand-edited anchor."""
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    stored = anchor.require()
    row = PeriodRow(root=str(root), segment_durable=True, seal_digest=SEAL_A, attested=True)
    anchor.write(stored.model_copy(update={"periods": stored.with_row(1, row)}))
    before = anchor.path.read_bytes()
    with pytest.raises(EngineError, match=r"cannot close period 1: its registry row is already"):
        anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    assert anchor.path.read_bytes() == before


def test_finalizing_an_attested_row_whose_segment_is_not_durable_is_refused(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """A forged row: attested without its durable flag. Its tag is `attested`."""
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    stored = anchor.require()
    anchor.write(
        stored.model_copy(
            update={"periods": stored.with_row(1, PeriodRow(root=str(root), attested=True))}
        )
    )
    before = anchor.path.read_bytes()
    with pytest.raises(EngineError, match=r"registry row is attested and its segment is not"):
        anchor.finalize(1)
    assert anchor.path.read_bytes() == before


def test_attesting_an_attested_row_whose_segment_is_not_durable_is_refused(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """The same forged row at the audit: it refuses as an uncommitted row does."""
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    stored = anchor.require()
    forged = PeriodRow(root=str(root.resolve()), seal_digest=SEAL_A, attested=True)
    anchor.write(stored.model_copy(update={"periods": stored.with_row(1, forged)}))
    before = anchor.path.read_bytes()
    with pytest.raises(EngineError, match=r"\(segment_durable=False\) .* only a committed, dur"):
        anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    assert anchor.path.read_bytes() == before


def test_closing_a_period_takes_the_row_from_each_source_it_can_hold(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """The close does not need the finalize first, and it tolerates a registry with no row.

    A provisional row fires period_row.02 like a durable one. A rowless close fires
    anchor_head.02 only: no row means no instance, and its write creates the row durable.
    Neither source is an item of the coverage gate, which counts rows, not sources."""
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    closed = anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    row = closed.row(1)
    assert row is not None and row.segment_durable
    other = EstateAnchor(tmp_path / "other")
    other.acquire()
    try:
        other.create_open(estate_id=ESTATE, root=root)
        stored = other.require()
        other.write(stored.model_copy(update={"periods": {}}))
        rowless = other.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    finally:
        other.release()
    assert rowless.row(1) == PeriodRow(
        root=str(root.resolve()), segment_durable=True, seal_digest=SEAL_A
    )


# ---- a chart and a method that disagree: a StatechartError before any write


def _open_anchor(anchor: EstateAnchor, root: Path) -> bytes:
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    return anchor.path.read_bytes()


def test_a_gap_in_the_head_chart_is_a_statechart_error_before_any_write(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    before = _open_anchor(anchor, root)
    _private_hits(monkeypatch, tmp_path)
    gapped = _without(ANCHOR_HEAD, lambda row: row.event != "close_period")
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
    with pytest.raises(UnhandledEvent) as raised:
        anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    assert str(raised.value).startswith(
        f"anchor_head {anchor.dir!r}: no row and no deferral for close_period"
    )
    assert ("machine", "anchor_head") in raised.value.facts
    assert anchor.path.read_bytes() == before


def test_a_refusal_the_method_has_no_text_for_is_a_statechart_error(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    before = _open_anchor(anchor, root)
    _private_hits(monkeypatch, tmp_path)
    renamed = replace(
        ANCHOR_HEAD,
        rows=tuple(
            replace(row, code="head_elsewhere") if row.id == "anchor_head.09" else row
            for row in ANCHOR_HEAD.rows
        ),
    )
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", renamed)
    with pytest.raises(StatechartError) as raised:
        anchor.claim_successor(
            estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
        )
    assert "the refusal head_elsewhere of claim_successor has no text" in str(raised.value)
    assert ("machine", "anchor_head") in raised.value.facts
    assert anchor.path.read_bytes() == before
    assert not (anchor.dir / "claims").exists()


def test_a_chart_that_lets_reclaim_move_an_unclaimed_head_is_a_statechart_error(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The method checks a claim only for a claimed head; a chart row that moves
    an open head on `reclaim` disagrees with it, and nothing is written."""
    root = tmp_path / "run"
    root.mkdir()
    before = _open_anchor(anchor, root)
    _private_hits(monkeypatch, tmp_path)
    added = Row("anchor_head.12", "open", "reclaim", "closed")
    monkeypatch.setattr(
        "dsl41.boundary.ANCHOR_HEAD", replace(ANCHOR_HEAD, rows=(*ANCHOR_HEAD.rows, added))
    )
    with pytest.raises(StatechartError) as raised:
        anchor.reclaim(estate_id=ESTATE, claimed_actor="operator")
    assert "reclaim moved a head that is open" in str(raised.value)
    assert ("machine", "anchor_head") in raised.value.facts
    assert anchor.path.read_bytes() == before


def test_a_write_the_chart_does_not_rest_in_is_a_statechart_error(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state the method writes and no row of the chart moved to."""
    root = tmp_path / "run"
    root.mkdir()
    before = _open_anchor(anchor, root)
    _private_hits(monkeypatch, tmp_path)
    elsewhere = replace(
        ANCHOR_HEAD,
        rows=tuple(
            replace(row, target="claimed") if row.id == "anchor_head.02" else row
            for row in ANCHOR_HEAD.rows
        ),
    )
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", elsewhere)
    with pytest.raises(StatechartError, match=r"the move writes \['anchor_head', 'closed'\]"):
        anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    assert anchor.path.read_bytes() == before


# ---- every poster's names are in the alphabets, and drive the real functions


def _names(posts: list[_Post]) -> set[tuple[str, str]]:
    return {(post[0], post[1]) for post in posts}


def test_every_poster_posts_only_its_chart_s_alphabet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genesis, the seal's commit, the opening from resume and from the roll,
    the three repairs at resume, the audit's attest and the break-glass, each
    through its real function. Never posted: a row's `create_open` (the head
    refuses first) and a row's `open_claimed` (the successor's row is written
    with its opening, so it has no instance yet)."""
    from test_estate import _estate, _invoke, _native_root, _roll, _seal_next, _Stopped

    posts = _spy(monkeypatch)
    seen: dict[str, set[tuple[str, str]]] = {}

    def poster(name: str) -> None:
        seen[name] = _names(posts)
        posts.clear()

    head, row = ANCHOR_HEAD.name, PERIOD_ROW.name
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a = tmp_path / "a"
    _native_root(root_a, c1)
    poster("genesis")
    assert _seal_next(root_a, c2).exit_code == 0
    poster("commit_boundary")
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    poster("attest")
    anchor_dir = default_anchor_dir(root_a)
    with pytest.raises(_Stopped):
        _roll(tmp_path / "b", anchor_dir, c2, stop_at="after_claim")
    posts.clear()
    reclaimed = _invoke("estate", "reclaim", "--estate-anchor", str(anchor_dir), "--force")
    assert reclaimed.exit_code == 0, reclaimed.output
    poster("reclaim")
    _roll(tmp_path / "c", anchor_dir, c2)
    poster("roll")
    stranger = tmp_path / "stranger"
    with pytest.raises(EngineError, match="already exists and is not ours"):
        _stranger_genesis(stranger, anchor_dir)
    poster("genesis over an existing anchor")

    run_root = tmp_path / "in-place"
    engine = _genesis(run_root)
    posts.clear()
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    poster("live seal")
    _close(_resume(run_root, C2_JIL))
    poster("open_next_period at resume")
    _rewind_to_claimed(run_root)
    _close(_resume(run_root, C2_JIL))
    poster("act_on_head: open_claimed")

    crashed = tmp_path / "crashed"
    _close(_sealed_record_without_the_cas(crashed))
    posts.clear()
    _close(_resume(crashed, C2_JIL))
    poster("act_on_head: close_period, then the opening")

    unfinalized = tmp_path / "unfinalized"
    _close(_genesis(unfinalized))
    _unfinalize(unfinalized)
    posts.clear()
    _close(_resume(unfinalized, C1_JIL))
    poster("act_on_head: finalize")

    assert seen == {
        "genesis": {(row, "finalize")},
        "commit_boundary": {(head, "close_period"), (row, "close_period")},
        "attest": {(row, "attest")},
        "reclaim": {(head, "reclaim")},
        "roll": {(head, "claim_successor"), (head, "open_claimed")},
        "genesis over an existing anchor": {(head, "create_open")},
        "live seal": {(head, "close_period"), (row, "close_period")},
        "open_next_period at resume": {(head, "claim_successor"), (head, "open_claimed")},
        "act_on_head: open_claimed": {(head, "open_claimed")},
        "act_on_head: close_period, then the opening": {
            (head, "close_period"),
            (row, "close_period"),
            (head, "claim_successor"),
            (head, "open_claimed"),
        },
        "act_on_head: finalize": {(row, "finalize")},
    }
    posted = set().union(*seen.values())
    assert {event for machine, event in posted if machine == head} == set(alphabet(ANCHOR_HEAD))
    assert {event for machine, event in posted if machine == row} == set(alphabet(PERIOD_ROW)) - {
        "create_open",
        "open_claimed",
    }


def _stranger_genesis(run_root: Path, anchor_dir: Path) -> None:
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_startup import start_run

    catalog, _ = _catalog(C1_JIL)
    start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        anchor_dir=anchor_dir,
    )


def _rewind_to_claimed(run_root: Path) -> None:
    """Put the head back to its claim with the successor's segment durable:
    the crash between the segment and the head (PR-45)."""
    seal = read_seal(run_root, 1)
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        stored = anchor.require()
        claim_id = claim_id_for(prev_seal_digest=seal.digest, next_period=2, target_root=run_root)
        anchor.write(
            stored.model_copy(
                update={
                    "head": ClaimedHead(claim_id=claim_id, target_root=str(run_root.resolve())),
                    "periods": _without_row(stored, 2),
                }
            )
        )
    finally:
        anchor.release()


def _sealed_record_without_the_cas(run_root: Path) -> Engine:
    """The crash between the `seal` record and the close CAS (PR-02b)."""
    engine = _genesis(run_root)
    engine.crash_point = _crash_at("after_seal_record")  # type: ignore[method-assign]
    request = _request(engine, _stage(run_root, C2_JIL))

    async def scenario() -> None:
        engine.submit_seal(request)
        with pytest.raises(EngineError, match="did not complete cleanly"):
            await engine.run_until_quiescent(T0)

    asyncio.run(scenario())
    return engine


def _unfinalize(run_root: Path) -> None:
    """The crash between genesis's segment and its finalize."""
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    try:
        stored = anchor.require()
        anchor.write(
            stored.model_copy(
                update={"periods": stored.with_row(1, PeriodRow(root=stored.periods["1"].root))}
            )
        )
    finally:
        anchor.release()


# ---- a gap stops `dsl41 run` with exit 7 (statechart-engine ss5)


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


def _serve(run_root: Path, text: str, resume: bool) -> int:
    from dsl41 import cli_run
    from dsl41.period import runtime_profile_from_cli

    catalog, _ = _catalog(text)
    serve = cli_run._serve_run(
        catalog, run_root, resume, [], profile=runtime_profile_from_cli(), supervisor="spawn"
    )
    # bounded: an engine that swallowed the error would serve on forever
    return asyncio.run(asyncio.wait_for(serve, timeout=60))


def _resume_and_stop(run_root: Path, text: str) -> None:
    """`dsl41 run --resume` up to its loop: everything resume repairs is done."""

    async def stop(self: Engine, horizon: datetime) -> list[Event]:
        raise RuntimeError("test: stop the resumed run at once")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Engine, "run_until_quiescent", stop)
        assert _serve(run_root, text, resume=True) == 1


def _seal_records(run_root: Path) -> int:
    return [record["rec"] for record in read_journal(wal_path(run_root, 1))].count("seal")


def test_a_gap_at_the_seal_s_close_stops_with_the_record_durable_and_the_head_open(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past the point of no return: commit_boundary re-raises the error before
    its fail-stop wrap (DL-327). The engine exits 7 with the `seal` record
    durable and the head still open, and the next resume closes it (PR-02b)."""
    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    futures: list[Any] = []

    async def with_a_seal(self: Engine, horizon: datetime) -> list[Event]:
        futures.append(self.submit_seal(_request(self, _stage(run_root, C2_JIL))))
        return await real(self, horizon)

    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(Engine, "run_until_quiescent", with_a_seal)
        _private_hits(broken, tmp_path)
        gapped = _without(ANCHOR_HEAD, lambda r: r.event != "close_period")
        broken.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
        assert _serve(run_root, C1_JIL, resume=False) == 7
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: anchor_head ")
    assert report[0].endswith("no row and no deferral for close_period in ['anchor_head', 'open']")
    (future,) = futures
    assert not future.done()
    assert _seal_records(run_root) == 1
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 1
    _resume_and_stop(run_root, C2_JIL)
    reread = EstateAnchor(default_anchor_dir(run_root)).read()
    assert reread is not None and isinstance(reread.head, OpenHead)
    assert reread.head.period_id == 2 and reread.periods["1"].seal_digest is not None


def test_a_gap_at_the_resume_step_stops_with_exit_7_and_the_anchor_unchanged(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ss11 step 4 meets the gap at the close CAS it repairs: the resume
    exits 7, the lineage lock goes with it, and the anchor is unchanged."""
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    _close(_sealed_record_without_the_cas(run_root))
    anchor_file = EstateAnchor(default_anchor_dir(run_root)).path
    before = anchor_file.read_bytes()
    _private_hits(monkeypatch, tmp_path)
    gapped = _without(ANCHOR_HEAD, lambda r: r.event != "close_period")
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
    capsys.readouterr()
    assert _serve(run_root, C2_JIL, resume=True) == 7
    report = _report(capsys.readouterr().err)
    assert report[0].endswith("no row and no deferral for close_period in ['anchor_head', 'open']")
    assert anchor_file.read_bytes() == before
    acquire_run_root(run_root).release()  # the lock went with the stop


def test_a_gap_at_genesis_s_finalize_stops_with_exit_7_and_resume_finalizes(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Genesis's sixth step meets the gap: `dsl41 run` exits 7 with the row
    provisional, and the next resume performs the finalize (ss11 step 4)."""
    run_root = short_root / "root"
    with pytest.MonkeyPatch.context() as broken:
        _private_hits(broken, tmp_path)
        gapped = _without(PERIOD_ROW, lambda r: r.event != "finalize")
        broken.setattr("dsl41.boundary.PERIOD_ROW", gapped)
        assert _serve(run_root, C1_JIL, resume=False) == 7
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: period_row ")
    assert "no row and no deferral for finalize" in report[0]
    anchor = EstateAnchor(default_anchor_dir(run_root))
    stored = anchor.read()
    assert stored is not None and row_tag(stored.row(1)) == "provisional"
    _resume_and_stop(run_root, C1_JIL)
    reread = anchor.read()
    assert reread is not None and row_tag(reread.row(1)) == "durable"


def _locks_free(run_root: Path) -> None:
    """Both locks went with the stop: each can be taken again."""
    from dsl41.runner_startup import acquire_run_root

    acquire_run_root(run_root).release()
    anchor = EstateAnchor(default_anchor_dir(run_root))
    anchor.acquire()
    anchor.release()


def test_a_gap_at_resume_s_open_claimed_repair_stops_with_exit_7_and_resume_repairs(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ss11 step 4's PR-45 window, a claim whose segment is durable, meets a
    gap at the head move: the resume exits 7, the anchor is unchanged, both
    locks are free, and the next resume opens the period."""
    from test_estate import _estate, _native_root, _open_in_place, _seal_next

    c1, c2, _ = _estate(tmp_path / "estate")
    run_root = short_root / "root"
    _native_root(run_root, c1)
    assert _seal_next(run_root, c2).exit_code == 0
    _open_in_place(run_root, c2)
    _rewind_to_claimed(run_root)
    anchor_file = EstateAnchor(default_anchor_dir(run_root)).path
    before = anchor_file.read_bytes()
    with pytest.MonkeyPatch.context() as broken:
        _private_hits(broken, tmp_path)
        gapped = _without(ANCHOR_HEAD, lambda r: r.event != "open_claimed")
        broken.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
        capsys.readouterr()
        assert _serve(run_root, c2.read_text(), resume=True) == 7
    report = _report(capsys.readouterr().err)
    assert report[0].endswith(
        "no row and no deferral for open_claimed in ['anchor_head', 'claimed']"
    )
    assert anchor_file.read_bytes() == before
    _locks_free(run_root)
    _open_in_place(run_root, c2)
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2 and row_tag(stored.row(2)) == "durable"


def test_a_gap_at_resume_s_finalize_repair_stops_with_exit_7_and_resume_repairs(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ss11 step 4's genesis window, a segment with its row provisional, meets
    a gap at the finalize: the resume exits 7, the anchor is unchanged, both
    locks are free, and the next resume finalizes."""
    from test_estate import _estate, _native_root, _open_in_place

    c1, _, _ = _estate(tmp_path / "estate")
    run_root = short_root / "root"
    _native_root(run_root, c1)
    _unfinalize(run_root)
    anchor_file = EstateAnchor(default_anchor_dir(run_root)).path
    before = anchor_file.read_bytes()
    with pytest.MonkeyPatch.context() as broken:
        _private_hits(broken, tmp_path)
        gapped = _without(PERIOD_ROW, lambda r: r.event != "finalize")
        broken.setattr("dsl41.boundary.PERIOD_ROW", gapped)
        capsys.readouterr()
        assert _serve(run_root, c1.read_text(), resume=True) == 7
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: period_row ")
    assert "no row and no deferral for finalize" in report[0]
    assert anchor_file.read_bytes() == before
    _locks_free(run_root)
    _open_in_place(run_root, c1)
    stored = EstateAnchor(default_anchor_dir(run_root)).read()
    assert stored is not None and row_tag(stored.row(1)) == "durable"


def test_a_raise_before_genesis_s_finalize_closes_the_journal_and_frees_both_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every raise after the genesis segment opens closes it: here the leader
    line's. The error is the original one, no file handle leaks (the suite
    makes a ResourceWarning an error), and both locks are free."""
    from dsl41.runner_journal import Journal

    def fail(self: Journal, *, epoch: int, at: datetime) -> None:
        raise OSError("test: the leader line did not land")

    monkeypatch.setattr(Journal, "leader", fail)
    run_root = tmp_path / "run"
    with pytest.raises(OSError, match="the leader line did not land"):
        _genesis(run_root)
    _locks_free(run_root)


def test_a_gap_at_estate_reclaim_exits_7_and_the_anchor_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`estate reclaim --force` turns an `EngineError` into a refusal, exit 2;
    a gap passes through it to the CLI's stop, exit 7, before any write."""
    from test_estate import _invoke

    anchor = EstateAnchor(tmp_path / "anchor")
    root = tmp_path / "run"
    root.mkdir()
    anchor.acquire()
    try:
        _walk(anchor, root, ("create", "finalize", "close", "claim"))
    finally:
        anchor.release()
    before = anchor.path.read_bytes()
    _private_hits(monkeypatch, tmp_path)
    gapped = _without(ANCHOR_HEAD, lambda r: r.event != "reclaim")
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
    result = _invoke("estate", "reclaim", "--estate-anchor", str(anchor.dir), "--force")
    assert result.exit_code == 7, result.output
    assert f"{STATECHART_LOG_PREFIX}: anchor_head " in result.output
    assert "no row and no deferral for reclaim" in result.output
    assert anchor.path.read_bytes() == before
    anchor.acquire()  # the verb released the lineage lock on its way out
    anchor.release()


def test_a_gap_in_the_roll_raises_through_it_with_the_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roll's try/finally releases the lineage lock and re-raises; `dsl41
    run --open-from` is wrapped by the CLI's stop like every running verb."""
    from test_estate import _attested_boundary, _roll

    c2, anchor_dir = _attested_boundary(tmp_path)
    anchor = EstateAnchor(anchor_dir)
    before = anchor.path.read_bytes()
    _private_hits(monkeypatch, tmp_path)
    gapped = _without(ANCHOR_HEAD, lambda r: r.event != "claim_successor")
    monkeypatch.setattr("dsl41.boundary.ANCHOR_HEAD", gapped)
    with pytest.raises(UnhandledEvent, match="no row and no deferral for claim_successor"):
        _roll(tmp_path / "b", anchor_dir, c2)
    assert anchor.path.read_bytes() == before
    anchor.acquire()
    anchor.release()


def test_a_statechart_error_is_never_an_engine_error() -> None:
    """The callers that turn an `EngineError` into a refusal (`estate reclaim`)
    or release the lineage lock and re-raise (the roll) let it through to the
    CLI's stop, which prints its report and exits 7."""
    assert not issubclass(StatechartError, EngineError)
