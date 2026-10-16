"""The `queue_scan` chart (docs/statechart-engine.md §11, §13; DL-372).

The chart is well formed, draws no warning and is registered. The generated completeness test
runs a witness for each configuration with a record that has no waiter. Each phase is pinned on
a bare executor with a record that plays a queue: the pass walks its list, ends at a move and
reads the list again; a scan asked inside a pass latches without leaving the turn, and an
admit-only scan asked for a full one runs a full pass next. The oracle-level sequences are in
test_queue_scan.py; these cells are built by hand, so their hits stay out of the session's
record.
"""

from __future__ import annotations

from collections.abc import Hashable

import pytest
import statechart_completeness as sc

from dsl41.machines import MACHINES
from dsl41.oracle_state import RuntimeState
from dsl41.scan_chart import IDLE, QUEUE_SCAN, SCAN, SCAN_KEY
from dsl41.state_machine import (
    COMPLETION,
    HITS_ENV,
    Effects,
    Event,
    Executor,
    StatechartError,
    alphabet,
    chart_warnings,
    well_formed,
)

CHART = QUEUE_SCAN


@pytest.fixture(autouse=True)
def _no_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The records here are made by hand: the transition gate counts the rows the oracle's own
    tests fire."""
    monkeypatch.delenv(HITS_ENV, raising=False)


class Record:
    """A scan's record over a queue: `waiting` in order, `moves` the waiters whose
    readmission moves them, `asks` how many scans each readmission asks inside the pass."""

    def __init__(
        self,
        waiting: list[str],
        moves: set[str] = frozenset(),  # type: ignore[assignment]
        asks: dict[str, int] | None = None,
        *,
        keep_stopped: bool = False,
    ) -> None:
        self.keep_stopped = keep_stopped
        self.waiting = list(waiting)
        self.moves = set(moves)
        self.asks = dict(asks or {})
        self.log: list[tuple[str, ...]] = []
        self._list: list[str] = []
        self._turn = 0
        self._moved = False

    @property
    def queued(self) -> bool:
        return any(job in self.waiting for job in self._list[self._turn :])

    @property
    def moved(self) -> bool:
        return self._moved

    def begin(self) -> Effects:
        self._list, self._turn, self._moved = list(self.waiting), 0, False
        self.log.append(("begin", "full" if not self.keep_stopped else "admit-only"))
        return Effects()

    def readmit(self) -> Effects:
        while self._list[self._turn] not in self.waiting:
            self._turn += 1
        job = self._list[self._turn]
        self._turn += 1
        self.log.append(("readmit", job))
        if job in self.moves:
            self.waiting.remove(job)
            self._moved = True
        asked = self.asks.pop(job, 0)
        return Effects(send=tuple((SCAN_KEY, Event(SCAN, Record([]))) for _ in range(asked)))

    def upgrade(self) -> None:
        self.keep_stopped = False
        self.log.append(("upgrade",))


def _scanned(record: Record) -> list[str]:
    """The rows each microstep of one scan fired, from `idle` back to `idle`."""
    executor = Executor()
    executor.restore(SCAN_KEY, CHART, IDLE)
    outcomes = executor.dispatch(SCAN_KEY, Event(SCAN, record))
    assert executor.active(SCAN_KEY) == IDLE
    return [step for outcome in outcomes for step in (*outcome.rows, *outcome.branches)]


# ---- the declaration


def test_the_chart_is_well_formed_draws_no_warning_and_is_registered() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert alphabet(CHART) == [SCAN]
    assert [row.id for row in CHART.rows] == [f"queue_scan.{n:02d}" for n in range(1, 10)]
    assert [b.id for c in CHART.choices for b in c.branches] == ["queue_scan.10", "queue_scan.11"]
    assert [row for row in CHART.rows if row.mark] == []
    assert {row.event for row in CHART.rows if row.event != SCAN} == {COMPLETION}


def test_the_owner_keeps_one_idle_coordinator_with_the_job_instances() -> None:
    """The owner restores the instance where it restores the job instances: a store with none
    has none, and an oracle's store, its fork and its rebuild each hold one, idle."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle

    store = RuntimeState()
    with pytest.raises(StatechartError, match="no instance 'queue_scan'"):
        store.machines.active(SCAN_KEY)
    oracle = Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n"))
    assert oracle.store.machines.active(SCAN_KEY) == IDLE
    assert oracle.fork().store.machines.active(SCAN_KEY) == IDLE
    oracle.store.restore_instances()
    assert oracle.store.machines.active(SCAN_KEY) == IDLE


# ---- the abstraction (§13)

_PASS = ("queue_scan", "pass")
CONFIGURATIONS: dict[str, frozenset[str]] = {
    "idle": IDLE,
    **{
        f"{turn} {request}": frozenset({*_PASS, turn, request})
        for turn in ("next_waiter", "awaiting_readmit", "done")
        for request in ("no_request", "full_requested")
    },
}


def _witness(active: frozenset[str]) -> sc.Builder:
    def build() -> tuple[Executor, Hashable]:
        executor = Executor()
        executor.restore(SCAN_KEY, CHART, active)
        return executor, SCAN_KEY

    return build


ABSTRACTION = sc.Abstraction(
    witnesses={name: _witness(active) for name, active in CONFIGURATIONS.items()},
    partitions={SCAN: {"no waiter": Record([])}},
)


def test_the_completeness_table_matches_the_declaration() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert table == {
        ("idle", SCAN, "no waiter"): "queue_scan.01",
        **{
            (f"{turn} no_request", SCAN, "no waiter"): "queue_scan.02"
            for turn in ("next_waiter", "awaiting_readmit", "done")
        },
        **{
            (f"{turn} full_requested", SCAN, "no waiter"): "queue_scan.03"
            for turn in ("next_waiter", "awaiting_readmit", "done")
        },
    }


# ---- the phases


def test_a_scan_with_no_waiter_begins_a_pass_and_ends() -> None:
    record = Record([])
    assert _scanned(record) == ["queue_scan.01", "queue_scan.04", "queue_scan.11", "queue_scan.09"]
    assert record.log == [("begin", "full")]


def test_a_pass_gives_each_queued_waiter_its_turn_and_ends_with_none_moved() -> None:
    record = Record(["a", "b"])
    assert _scanned(record) == [
        "queue_scan.01",
        *("queue_scan.04", "queue_scan.10"),  # the first turn: a
        *("queue_scan.06", "queue_scan.10"),  # a stays; the next turn: b
        *("queue_scan.06", "queue_scan.11"),  # b stays; no waiter left
        "queue_scan.09",
    ]
    assert record.log == [("begin", "full"), ("readmit", "a"), ("readmit", "b")]


def test_a_pass_ends_at_a_move_and_the_next_reads_the_list_again() -> None:
    record = Record(["a", "b", "c"], moves={"b"})
    rows = _scanned(record)
    assert rows == [
        "queue_scan.01",
        *("queue_scan.04", "queue_scan.10"),  # a's turn
        *("queue_scan.06", "queue_scan.10"),  # a stays; b's turn
        "queue_scan.05",  # b moves: the pass ends
        "queue_scan.08",  # the next pass
        *("queue_scan.04", "queue_scan.10"),  # a again
        *("queue_scan.06", "queue_scan.10"),  # a stays; c's turn
        *("queue_scan.06", "queue_scan.11"),  # c stays; no waiter left
        "queue_scan.09",
    ]
    assert [entry for entry in record.log if entry[0] == "readmit"] == [
        ("readmit", "a"),
        ("readmit", "b"),
        ("readmit", "a"),
        ("readmit", "c"),
    ]


def test_a_scan_asked_inside_a_turn_latches_and_the_turn_s_completion_follows() -> None:
    """Two scans asked inside a's turn: the first latches (queue_scan.02), the second finds
    the latch (queue_scan.03); neither leaves `awaiting_readmit`, so its completion follows
    them. The pass is a full one, so the latch asks nothing more."""
    record = Record(["a"], asks={"a": 2})
    assert _scanned(record) == [
        "queue_scan.01",
        *("queue_scan.04", "queue_scan.10"),
        "queue_scan.02",
        "queue_scan.03",
        *("queue_scan.06", "queue_scan.11"),
        "queue_scan.09",
    ]
    assert record.log == [("begin", "full"), ("readmit", "a")]


@pytest.mark.parametrize("moves", [set(), {"a"}], ids=["no move", "a move"])
def test_an_admit_only_scan_asked_for_a_full_one_runs_a_full_pass_next(moves: set[str]) -> None:
    """DL-247: the request inside an admit-only pass upgrades the next pass, whether the pass
    ended at a move or with none."""
    record = Record(["a", "b"], moves=moves, asks={"a": 1}, keep_stopped=True)
    rows = _scanned(record)
    assert rows[:4] == ["queue_scan.01", "queue_scan.04", "queue_scan.10", "queue_scan.02"]
    assert rows.count("queue_scan.07") == 1
    assert record.log[:1] == [("begin", "admit-only")]
    assert ("upgrade",) in record.log
    after = record.log[record.log.index(("upgrade",)) + 1 :]
    assert after[0] == ("begin", "full")
    assert after[1] == ("readmit", "b" if moves else "a")


def test_a_scan_with_no_record_is_a_statechart_error() -> None:
    executor = Executor()
    executor.restore(SCAN_KEY, CHART, IDLE)
    with pytest.raises(StatechartError, match="scan: a scan with no record"):
        executor.dispatch(SCAN_KEY, Event(SCAN, object()))


def test_a_turn_taken_with_no_waiter_found_is_a_statechart_error() -> None:
    """The entry takes the turn the choice's guard found; a turn with none is a bug."""
    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle
    from dsl41.oracle_intents import ScanFacts

    record = ScanFacts(
        Oracle(lower_source("insert_job: j\njob_type: c\ncommand: x\n")), keep_stopped=False
    )
    record.begin()
    assert not record.queued
    with pytest.raises(StatechartError, match="no waiter found queued"):
        record.readmit()
