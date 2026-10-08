"""The anchor head and its registry rows as declared state machines.

Normative spec: `docs/period-model.md` ss1.3 (the anchor, its transitions and
the registry), PR-02c. `boundary.ANCHOR_HEAD` and `boundary.PERIOD_ROW` declare
the moves; `EstateAnchor` takes the declared transition before each write. The
transition-coverage gate holds every row to a hit across the suite. These tests
pin what each move records, that a move outside the table is refused before any
write (`EngineError` in production, `TransitionError` under the suite's strict
variable), and that the declarations match the anchor models.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from dsl41.boundary import (
    ANCHOR_HEAD,
    HEAD_CLOSE_PERIOD,
    PERIOD_ROW,
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    HeadTag,
    OpenHead,
    PeriodRow,
    RowTag,
    _take,
    row_tag,
)
from dsl41.machines import MACHINES
from dsl41.runner_clock import EngineError
from dsl41.state_machine import (
    HITS_ENV,
    STRICT_ENV,
    StateMachine,
    Transition,
    TransitionError,
    Violation,
)

ESTATE = "estate-anchor-machines"
SEAL_A = "sha256:" + "a" * 64


@pytest.fixture
def anchor(tmp_path: Path) -> Iterator[EstateAnchor]:
    handle = EstateAnchor(tmp_path / "anchor")
    handle.acquire()
    yield handle
    handle.release()


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, not strict, so a deliberate violation stays out of
    the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    return directory


def _records(directory: Path, prefix: str) -> list[dict[str, str]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob(f"{prefix}-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _moves(directory: Path) -> set[tuple[str, str, str]]:
    return {(r["id"], r["old"], r["new"]) for r in _records(directory, "hits")}


def test_the_machines_are_registered_and_named_after_their_tables() -> None:
    assert ANCHOR_HEAD in MACHINES
    assert PERIOD_ROW in MACHINES
    assert {t.id for t in ANCHOR_HEAD.transitions} == {f"anchor_head.0{n}" for n in range(1, 7)}
    assert {t.id for t in PERIOD_ROW.transitions} == {f"period_row.0{n}" for n in range(1, 6)}
    assert all(t.mark is None for m in (ANCHOR_HEAD, PERIOD_ROW) for t in m.transitions)


def test_the_head_states_are_the_head_tags_and_absent() -> None:
    tags = {model.model_fields["state"].default for model in (OpenHead, ClosedHead, ClaimedHead)}
    assert ANCHOR_HEAD.states == tags | {"absent"}
    assert set(get_args(HeadTag.__value__)) == ANCHOR_HEAD.states
    assert set(get_args(RowTag.__value__)) == PERIOD_ROW.states


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


def test_a_lineage_walk_takes_every_declared_move_and_reports_no_violation(
    anchor: EstateAnchor, recording: Path, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
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
    assert _moves(recording) == {
        ("anchor_head.01", "absent", "open"),
        ("period_row.01", "absent", "provisional"),
        ("period_row.04", "provisional", "durable"),
        ("anchor_head.02", "open", "closed"),
        ("period_row.02", "durable", "durable"),
        ("period_row.05", "durable", "attested"),
        ("anchor_head.03", "closed", "claimed"),
        ("anchor_head.06", "claimed", "claimed"),
        ("anchor_head.05", "claimed", "closed"),
        ("anchor_head.04", "claimed", "open"),
        ("period_row.03", "absent", "durable"),
    }
    assert _records(recording, "violations") == []


def test_an_idempotent_repeat_writes_nothing_and_takes_nothing(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[str] = []
    real_take = StateMachine.take

    def spy(self: StateMachine[Any], t: Transition[Any], old: Any, new: Any) -> Violation | None:
        taken.append(t.id)
        return real_take(self, t, old, new)

    monkeypatch.setattr(StateMachine, "take", spy)
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    assert len(taken) == 6  # the spy sees every move of the first pass: it can fail
    written = anchor.path.read_bytes()
    taken.clear()
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    anchor.attest(1, estate_id=ESTATE, root=root, seal_digest=SEAL_A)
    assert anchor.path.read_bytes() == written
    assert taken == []


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
    anchor: EstateAnchor, recording: Path, tmp_path: Path
) -> None:
    """Not strict, as in production: the move outside the table is refused before the write."""
    root = tmp_path / "run"
    root.mkdir()
    claim_id = _claimed_with_successor_row(anchor, root)
    before = anchor.path.read_bytes()
    with pytest.raises(
        EngineError, match=r"period_row period_row\.03: durable -> durable is not a declared move"
    ):
        anchor.open_claimed(claim_id=claim_id, period_id=2, root=root)
    assert anchor.path.read_bytes() == before
    assert isinstance(anchor.require().head, ClaimedHead)
    violations = _records(recording, "violations")
    assert [(v["machine"], v["id"], v["old"], v["new"]) for v in violations] == [
        ("period_row", "period_row.03", "durable", "durable")
    ]


def test_a_move_outside_the_table_is_refused_naming_the_machine_and_the_move(
    tmp_path: Path, recording: Path
) -> None:
    path = tmp_path / "anchor.json"
    with pytest.raises(EngineError, match=r"anchor_head anchor_head\.02: closed -> closed"):
        _take(path, ANCHOR_HEAD, HEAD_CLOSE_PERIOD, "closed", "closed")
    with pytest.raises(EngineError, match=r"anchor_head anchor_head\.02: open -> claimed"):
        _take(path, ANCHOR_HEAD, HEAD_CLOSE_PERIOD, "open", "claimed")
    _take(path, ANCHOR_HEAD, HEAD_CLOSE_PERIOD, "open", "closed")  # the declared move


def test_opening_over_no_successor_row_reports_nothing(
    anchor: EstateAnchor, recording: Path, tmp_path: Path
) -> None:
    root = tmp_path / "run"
    root.mkdir()
    anchor.create_open(estate_id=ESTATE, root=root)
    anchor.finalize(1)
    anchor.close_period(estate_id=ESTATE, period_id=1, root=root, seal_digest=SEAL_A)
    claim = anchor.claim_successor(
        estate_id=ESTATE, seal_digest=SEAL_A, next_period=2, target_root=root
    )
    anchor.open_claimed(claim_id=claim.claim_id, period_id=2, root=root)
    assert _records(recording, "violations") == []


def test_a_strict_session_raises_on_the_same_forged_registry(
    anchor: EstateAnchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The twin of the refused case: with the strict variable set, as the suite runs,
    the same move out of the table raises `TransitionError` where production raises
    `EngineError`. Either way nothing is written."""
    root = tmp_path / "run"
    root.mkdir()
    claim_id = _claimed_with_successor_row(anchor, root)
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setenv(STRICT_ENV, "1")
    with pytest.raises(TransitionError, match=r"period_row\.03"):
        anchor.open_claimed(claim_id=claim_id, period_id=2, root=root)
    head = anchor.require().head
    assert isinstance(head, ClaimedHead)  # nothing was written


def test_closing_a_period_takes_the_row_from_each_source_it_can_hold(
    anchor: EstateAnchor, tmp_path: Path
) -> None:
    """The close does not need the finalize first, and it tolerates a registry with no row.

    This test records into the session's hits, so the strict variable the suite sets makes
    a move outside the table raise, and the gate's per-source report sees both sources."""
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
