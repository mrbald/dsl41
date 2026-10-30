"""The engine tier's machines (DL-289): admission and the subscription feed through their
tiers. Admission is a statechart (DL-334); its chart test is test_admission_chart.py.

Each machine is registered and well formed, and each rule has a test that triggers it and one
that does not. A test that builds a chart's cells by hand records into its own directory, so its
hits stay out of the session's gate."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dsl41.machines import MACHINES
from dsl41.runner_admission import (
    ADMISSION,
    ApplyResult,
    Attempt,
    DecisionIndex,
)
from dsl41.runner_store import MemoryStore, Record
from dsl41.runner_journal import Journal
from dsl41.state_machine import (
    HITS_ENV,
    Executor,
    Outcome,
    StatechartError,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
ENGINE_MACHINES = (ADMISSION,)


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, not strict, so a deliberate violation
    stays out of the session and production's handling runs."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


def test_the_engine_machines_are_registered_and_well_formed() -> None:
    for machine in ENGINE_MACHINES:
        assert machine in MACHINES
        assert well_formed(machine) == []
    assert [(row.id, row.mark) for row in ADMISSION.rows if row.mark] == []


# ---------------------------------------------------------------- admission


def _attempt(request_id: str, fingerprint: str = "fp", index: int = 1) -> Attempt:
    return Attempt(index=index, at=T0, request_id=request_id, fingerprint=fingerprint)


def test_a_request_id_moves_unseen_admitted_decided() -> None:
    index = DecisionIndex()
    assert index.state("r1") is None  # unseen: no instance, no decision
    index.note(_attempt("r1"))
    assert index.state("r1") == "admitted"
    index.record(
        ApplyResult(
            index=1, request_id="r1", decision="rejected", reason="no", code="precondition_failed"
        )
    )
    assert index.state("r1") == "rejected"
    assert index.lookup("r1", "fp") is not None  # the stored decision answers


# ------------------------------------------------------- the subscription


def test_a_feed_moves_backfill_live_closed(tmp_path: Path) -> None:
    journal = Journal(MemoryStore())
    try:
        feed = journal.subscribe(Executor())
        assert feed.state == "backfill"
        feed.go_live()
        assert feed.state == "live"
        journal.unsubscribe(feed)
        assert feed.state == "closed"
        journal.unsubscribe(feed)  # a feed already gone leaves nothing to close
        assert feed.state == "closed"
    finally:
        journal.close()


def test_an_overflowed_feed_is_removed_and_its_unsubscribe_takes_nothing(tmp_path: Path) -> None:
    journal = Journal(MemoryStore())
    try:
        reasons: list[str] = []
        machines = Executor()
        feed = journal.subscribe(machines, reasons.append, budget=1)
        record = Record(
            datetime(2026, 7, 1, 8, 0), "preflight", None, None, {"items": [{"message": "x" * 100}]}
        )
        journal.append(record)  # an empty backlog takes one record
        assert feed.state == "backfill"
        journal.append(record)  # the second does not fit: the journal drops the feed
        assert feed.state == "removed" and feed.overflow is not None
        with pytest.raises(StatechartError, match="no instance"):
            machines.active(feed)  # forgotten once it rests in `removed`
        posts: list[str] = []
        dispatch = machines.dispatch

        def spy(key: Any, event: ChartEvent) -> list[Outcome]:
            posts.append(event.name)
            return dispatch(key, event)

        machines.dispatch = spy  # type: ignore[method-assign]
        journal.unsubscribe(feed)
        assert feed.state == "removed" and posts == []  # nothing posted
    finally:
        journal.close()
