"""The queue's scan (DL-50, DL-247, DL-255, DL-345): four sequences that pin its passes by
what an observer of the oracle sees, whatever form the scan takes.

Each case reads the `readmit` items the hook writes, in order, with the mode each carries
(`keep_stopped`), and the rows and trace lines the input leaves:

- the restart: a pass ends at the first waiter whose `readmit` reported a move, and the next
  pass reads the waiters again from the head;
- the upgrade: an admit-only pass asked for a full pass inside it runs a full pass next, which
  cancels a waiter of a stopped box (DL-247);
- the nested request: a scan asked inside a full pass sends nothing, and the pass goes on;
- the join in a wake: a scan asked in a wake with no pass running runs its pass there, depth
  first, before the wake's later referencers take the units.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventKind
from dsl41.scan_chart import IDLE as SCAN_IDLE
from dsl41.scan_chart import SCAN_KEY
from dsl41.semantics import resolve
from dsl41.state_machine import Outcome, StatechartError

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def _oracle(jil: str, setup: list[Event], **kw: Any) -> Oracle:
    oracle = Oracle(lower_source(jil), **kw)
    for event in setup:
        oracle.feed(event)
    return oracle


def _readmits(oracle: Oracle) -> list[tuple[str, bool]]:
    """From now on: each `readmit` the hook writes, as (waiter, keep_stopped), in order."""
    seen: list[tuple[str, bool]] = []
    perform = oracle._perform  # type: ignore[attr-defined]

    def spy(outcome: Outcome) -> None:
        if outcome.event.name == "readmit":
            seen.append((str(outcome.key).partition(":")[2], outcome.event.payload.keep_stopped))
        perform(outcome)

    oracle._perform = spy  # type: ignore[attr-defined]
    return seen


def _moves(oracle: Oracle, since: int) -> list[tuple[str, str]]:
    return [(entry.job, entry.transition) for entry in oracle.trace()[since:]]


def _cmd(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n{extra}\n"


def _box(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


def test_a_pass_ends_at_the_first_move_and_the_next_reads_the_waiters_from_the_head() -> None:
    """R has two units and h holds them; A waits for S, which g holds, and B and C wait for
    one unit of R each. h's end frees R: the first pass keeps A and admits B, and ends; the
    second reads the waiters again, keeps A and admits C; the third keeps A and ends."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        "insert_resource: S\nres_type: R\namount: 1\n\n"
        + _cmd("h", "resources: (R, QUANTITY=2, FREE=Y)\n")
        + _cmd("g", "resources: (S, QUANTITY=1, FREE=Y)\n")
        + _cmd("A", "resources: (S, QUANTITY=1, FREE=Y)\n")
        + _cmd("B", "resources: (R, QUANTITY=1, FREE=Y)\n")
        + _cmd("C", "resources: (R, QUANTITY=1, FREE=Y)\n")
    )
    setup = [ev("STARTJOB", 1, job=job) for job in ("h", "g", "A", "B", "C")]
    oracle = _oracle(jil, setup)
    assert [oracle.store.job[j].status for j in "ABC"] == ["QUE_WAIT"] * 3
    readmits = _readmits(oracle)
    oracle.feed(ev("STATUS", 2, job="h", status="SUCCESS"))
    assert readmits == [
        ("A", False),
        ("B", False),
        ("A", False),
        ("C", False),
        ("A", False),
    ]
    assert [oracle.store.job[j].status for j in "ABC"] == ["QUE_WAIT", "RUNNING", "RUNNING"]


#: DL-247's admit-only scan, asked for a full pass inside it (R11's counter-example to a scan
#: that forgets the request).
_UPGRADE = (
    "insert_machine: up\ntype: a\nnode_name: up\nmax_load: 80\n\n"
    "insert_job: H\njob_type: c\ncommand: h\nmachine: up\njob_load: 30\npriority: 1\n\n"
    + _box("B")
    + "insert_job: M\njob_type: c\ncommand: m\nmachine: up\nbox_name: B\n"
    "job_load: 60\npriority: 5\n\n"
    + _box("Q", "box_failure: s(X) | v(GO) = 1\n")
    + "insert_job: X\njob_type: c\ncommand: x\nmachine: up\nbox_name: Q\n"
    "job_load: 30\npriority: 9\njob_terminator: 1\n"
)
_UPGRADE_SETUP = [
    ev("STARTJOB", 0, job="H"),
    ev("STARTJOB", 1, job="B"),
    ev("STARTJOB", 2, job="Q"),
    ev("SET_GLOBAL", 3, name="GO", value="1"),
]


def test_an_admit_only_pass_asked_for_a_full_pass_runs_one_next() -> None:
    """B's kill owes the admit-only scan. Its pass keeps M, queued in the stopped box, and
    admits X; X's start fails Q, whose terminator ends X and releases its load, which asks a
    full scan inside the pass. The pass ended at X's move, and the next is a full one: it
    cancels M."""
    oracle = _oracle(_UPGRADE, _UPGRADE_SETUP)
    assert [oracle.store.job[j].status for j in ("M", "X")] == ["QUE_WAIT", "QUE_WAIT"]
    traced = len(oracle.trace())
    readmits = _readmits(oracle)
    oracle.feed(ev("KILLJOB", 4, job="B"))
    assert readmits == [("M", True), ("X", True), ("M", False)]
    assert _moves(oracle, traced)[-1] == ("M", "QUE_WAIT->INACTIVE")
    statuses = {job: oracle.store.job[job].status for job in ("Q", "X", "M")}
    assert statuses == {"Q": "FAILURE", "X": "TERMINATED", "M": "INACTIVE"}


def test_a_scan_asked_inside_a_full_pass_sends_nothing_and_the_pass_goes_on() -> None:
    """W1's readmission completes the empty box at its start, and its release asks a scan
    inside the running pass: that scan sends no readmission. W1 moved, so the pass ends and
    the next admits W2 with the unit W1 gave back; the third finds no waiter."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        + _cmd("h", "resources: (R, QUANTITY=1, FREE=N)\n")
        + _box("W1", "resources: (R, QUANTITY=1, FREE=Y)\n")
        + _cmd("W2", "resources: (R, QUANTITY=1, FREE=Y)\n")
    )
    setup = [
        ev("STARTJOB", 1, job="h"),
        ev("STATUS", 2, job="h", status="SUCCESS"),
        ev("FORCE_STARTJOB", 3, job="W1"),
        ev("STARTJOB", 4, job="W2"),
    ]
    oracle = _oracle(jil, setup)
    readmits = _readmits(oracle)
    oracle.feed(ev("RELEASE_RESOURCE", 5, job="h"))
    assert readmits == [("W1", False), ("W2", False)]
    rows = oracle.store.job
    assert (rows["W1"].status, rows["W2"].status) == ("SUCCESS", "RUNNING")


def test_a_scan_asked_in_a_wake_runs_its_pass_before_the_wake_s_later_referencers() -> None:
    """R has one unit; H, the empty box E, the waiter W and C each need it. H's end frees R
    and wakes E, then C. E starts on R, completes at its start and frees R, and its scan
    admits W there, depth first: C, woken after E, finds R taken and waits. A scan run after
    the input's dispatch would let C take R."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        + _cmd("H", "resources: (R, QUANTITY=1, FREE=Y)\n")
        + _box("E", "condition: s(H)\nresources: (R, QUANTITY=1, FREE=Y)\n")
        + _cmd("W", "resources: (R, QUANTITY=1, FREE=Y)\n")
        + _cmd("C", "condition: s(H)\nresources: (R, QUANTITY=1, FREE=Y)\n")
    )
    semantics = resolve({"box-start-all-members-out": "complete"})
    oracle = _oracle(
        jil, [ev("STARTJOB", 1, job="H"), ev("STARTJOB", 2, job="W")], semantics=semantics
    )
    assert oracle.store.job["W"].status == "QUE_WAIT"
    readmits = _readmits(oracle)
    oracle.feed(ev("STATUS", 3, job="H", status="SUCCESS"))
    rows = oracle.store.job
    assert (rows["E"].status, rows["W"].status, rows["C"].status) == (
        "SUCCESS",
        "RUNNING",
        "QUE_WAIT",
    )
    assert readmits[0] == ("W", False)


# ---- the coordinator on the oracle's executor (DL-372)

_MUTEX = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    + _cmd("h", "resources: (R, QUANTITY=1, FREE=Y)\n")
    + _cmd("w", "resources: (R, QUANTITY=1, FREE=Y)\n")
)
_MUTEX_SETUP = [ev("STARTJOB", 1, job="h"), ev("STARTJOB", 2, job="w")]


def test_the_hook_counts_the_coordinator_s_microsteps_and_writes_no_row_for_them() -> None:
    """Each microstep of the scan takes one item of the root dispatch's cap; none reaches a
    job row, so the macrostep's check and the writers never see the coordinator."""
    oracle = _oracle(_MUTEX, _MUTEX_SETUP)
    steps: list[tuple[str, int, bool]] = []
    records: list[str] = []
    perform = oracle._perform  # type: ignore[attr-defined]

    def spy(outcome: Outcome) -> None:
        before = oracle._items
        perform(outcome)
        steps.append((str(outcome.key), oracle._items - before, SCAN_KEY in oracle._reached))
        records.append(repr(outcome.event.payload))

    oracle._perform = spy  # type: ignore[attr-defined]
    oracle.feed(ev("STATUS", 3, job="h", status="SUCCESS"))
    scan = [step for step in steps if step[0] == SCAN_KEY]
    assert len(scan) == 6  # begin, w's turn, its move, the restart, no turn, the end
    assert {(counted, reached) for _key, counted, reached in scan} == {(1, False)}
    # an error report names the record by its mode and its turn
    assert records[[key for key, _, _ in steps].index(SCAN_KEY)] == (
        "ScanFacts(keep_stopped=False, turn=0)"
    )
    assert oracle.store.job["w"].status == "RUNNING"


def test_a_fault_inside_a_pass_leaves_the_coordinator_idle_and_the_next_scan_runs() -> None:
    """A write that raises inside a turn: the instances are rebuilt from the rows, the
    coordinator idle among them, and the next scan, after h's next run, admits the waiter."""

    class Faulting(Oracle):
        faults = 1

        def _w_readmit(self, job: str, outcome: Outcome) -> Any:
            if self.faults:
                self.faults -= 1
                raise RuntimeError("planted")
            return super()._w_readmit(job, outcome)  # type: ignore[misc]

    oracle = Faulting(lower_source(_MUTEX))
    for event in _MUTEX_SETUP:
        oracle.feed(event)
    with pytest.raises(RuntimeError, match="planted"):
        oracle.feed(ev("STATUS", 3, job="h", status="SUCCESS"))
    assert oracle.store.machines.active(SCAN_KEY) == SCAN_IDLE
    assert oracle.store.job["w"].status == "QUE_WAIT"
    oracle.feed(ev("STARTJOB", 5, job="h"))
    oracle.feed(ev("STATUS", 6, job="h", status="SUCCESS"))
    assert oracle.store.job["w"].status == "RUNNING"


def test_a_pass_left_open_at_a_dispatch_s_end_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator rests in `idle` at every root dispatch's end. A pass left open, here
    planted by rebuilding the instance mid-turn, would latch every later scan and scan
    nothing: the dispatch that ends with it open raises, and the instances are rebuilt."""
    import dsl41.oracle_state as oracle_state

    oracle = _oracle(_MUTEX, _MUTEX_SETUP)
    open_pass = frozenset({"queue_scan", "pass", "awaiting_readmit", "no_request"})
    with monkeypatch.context() as patch:
        patch.setattr(oracle_state, "SCAN_IDLE", open_pass)
        oracle.store.restore_instances()
    assert oracle.store.machines.active(SCAN_KEY) == open_pass
    with pytest.raises(StatechartError, match="queue_scan: at the macrostep's end"):
        oracle.feed(ev("STATUS", 3, job="h", status="SUCCESS"))
    assert oracle.store.machines.active(SCAN_KEY) == SCAN_IDLE
