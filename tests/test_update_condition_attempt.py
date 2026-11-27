"""An update that changes a waiting member's condition gives it one attempt (DL-564, BUG-116).

When an update applies, a changed job that sits in a box, has no date conditions in its new
definition and whose lowered `condition:` differs gets one plain attempt at the next input,
after the queue's scan, in catalog order, if it still waits in its box's run: its row is
INACTIVE, it has not run in this run of the box, and no verdict resolved it. The chart's gates
decide the rest. The live application selects the names and records them; a replay reads them
from the record, and a snapshot taken between the swap and the next input carries them.

These scenarios run at the oracle. The engine's own scenarios are in
`test_update_condition_attempt_engine.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from dsl41.canon import canonical_bytes, decode
from dsl41.ir import lower_source
from dsl41.oracle import Oracle, update_wakes
from dsl41.oracle_state import CarriedRows, Event
from dsl41.reload_diff import diff
from dsl41.reload_update import woken_members
from dsl41.runner_journal import oracle_rows

T = datetime(2026, 7, 1, 9, 0)

#: the cause the attempt of an update that applied configuration rev 2 carries
CAUSE = "configuration rev 2 changed the condition (DL-564)"


def _box(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


def _cmd(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n{extra}\n"


def _member(name: str, extra: str = "", box: str = "P") -> str:
    return _cmd(name, f"box_name: {box}\n{extra}")


def _at(seconds: int) -> datetime:
    return T + timedelta(seconds=seconds)


def _ev(kind: str, job: str, seconds: int, **payload: object) -> Event:
    return Event(at=_at(seconds), kind=kind, payload={"job": job, **payload})  # type: ignore[arg-type]


def _update(oracle: Oracle, text: str, rev: int = 2) -> tuple[str, ...]:
    """Apply `text` as an update does: the plan's changed jobs, the members it wakes, the swap.
    Answer the members it woke."""
    new = lower_source(text)
    changed = [change.name for change in diff(oracle.catalog, new, {}, {}).changed]
    woken = woken_members(oracle.catalog, new, changed)
    oracle.replace_catalog(
        new,
        semantics=oracle.semantics,
        tz_aliases=None,
        default_tz=None,
        woken=update_wakes(woken, rev),
    )
    return woken


def _row(oracle: Oracle, job: str) -> tuple[str, int]:
    row = oracle.store.job[job]
    return row.status, row.run_number


#: `P` runs; its member `X` waits on `s(Y)`
WAITING = _box("P") + _member("X", "condition: s(Y)\n") + _cmd("Y") + _cmd("Z")


def _running(text: str = WAITING, *events: Event) -> Oracle:
    oracle = Oracle(lower_source(text))
    oracle.feed(_ev("STARTJOB", "P", 0))
    for ev in events:
        oracle.feed(ev)
    assert oracle.store.job["P"].status == "RUNNING"
    return oracle


def test_dl564_bug116_a_member_whose_condition_is_removed_starts_and_its_box_completes() -> None:
    """BUG-116: `X` no longer references `Y`, so `Y`'s move does not wake it. The update's own
    attempt starts it in the input after the application, and `P` completes when it ends."""
    oracle = _running()
    assert _row(oracle, "X") == ("INACTIVE", 0)
    assert _update(oracle, WAITING.replace("condition: s(Y)\n", "")) == ("X",)
    assert _row(oracle, "X") == ("INACTIVE", 0)  # nothing moves between inputs
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("RUNNING", 1)
    assert oracle.store.job["X"].started_by == CAUSE
    starts = [e for e in oracle.trace() if e.job == "X" and e.transition == "INACTIVE->STARTING"]
    assert [e.cause for e in starts] == [CAUSE]
    oracle.feed(_ev("STATUS", "X", 2, status="SUCCESS"))
    assert oracle.store.job["P"].status == "SUCCESS"


def test_dl564_a_member_whose_condition_now_reads_true_starts() -> None:
    oracle = _running(
        WAITING, _ev("FORCE_STARTJOB", "Z", 0), _ev("STATUS", "Z", 0, status="SUCCESS")
    )
    assert _update(oracle, WAITING.replace("s(Y)", "s(Z)")) == ("X",)
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("RUNNING", 1)


def test_dl564_a_member_whose_new_condition_is_false_waits_for_the_job_it_names() -> None:
    """The attempt meets the false condition and moves nothing; a later move of the job the new
    condition names starts it, as any referencer's wake does."""
    oracle = _running()
    assert _update(oracle, WAITING.replace("s(Y)", "s(Z)")) == ("X",)
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("INACTIVE", 0)
    oracle.feed(_ev("FORCE_STARTJOB", "Y", 2))
    oracle.feed(_ev("STATUS", "Y", 2, status="SUCCESS"))
    assert _row(oracle, "X") == ("INACTIVE", 0)
    oracle.feed(_ev("FORCE_STARTJOB", "Z", 3))
    oracle.feed(_ev("STATUS", "Z", 3, status="SUCCESS"))
    assert _row(oracle, "X") == ("RUNNING", 1)
    assert oracle.store.job["X"].started_by == "status of 'Z' changed to SUCCESS"


def test_dl564_the_rule_selects_only_boxed_undated_jobs_whose_condition_changed() -> None:
    """No attempt for a changed job outside every box whose condition reads true, a changed
    member with date conditions, a changed member whose condition did not change, or a
    seed-only edit; the condition is compared lowered, so a respaced one is no change."""
    dated = 'date_conditions: 1\ndays_of_week: all\nstart_times: "18:00"\n'
    old = (
        _box("P")
        + _member("D", "condition: s(Y)\n")
        + _member("C", "condition: s(Y)\n")
        + _member("S", "condition: s(Y)\n")
        + _member("R", "condition: s(Y)\n")
        + _cmd("T", "condition: s(Y)\n")
        + _cmd("Y")
    )
    new = (
        _box("P")
        + _member("D", dated)
        + _member("C", "condition: s(Y)\ndescription: edited\n")
        + _member("S", "condition: s(Y)\nstatus: ON_HOLD\n")
        + _member("R", "condition:   s(Y)\nn_retrys: 1\n")
        + _cmd("T")
        + _cmd("Y")
    )
    live, candidate = lower_source(old), lower_source(new)
    plan = diff(live, candidate, {}, {})
    assert [c.name for c in plan.changed] == ["C", "D", "R", "T"]
    assert [c.name for c in plan.seed_only] == ["S"]
    assert woken_members(live, candidate, [c.name for c in plan.changed]) == ()
    oracle = _running(old)
    _update(oracle, new)
    oracle.advance(_at(1))
    assert _row(oracle, "T") == ("INACTIVE", 0)  # its condition is gone, and nothing woke it


def test_dl564_a_member_that_ran_in_this_run_of_its_box_gets_no_attempt() -> None:
    sibling = WAITING + _member("Q")
    oracle = _running(
        sibling, _ev("FORCE_STARTJOB", "X", 0), _ev("STATUS", "X", 0, status="FAILURE")
    )
    assert _row(oracle, "X") == ("FAILURE", 1)
    _update(oracle, sibling.replace("condition: s(Y)\n", ""))
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("FAILURE", 1)


@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE", "TERMINATED", "INACTIVE"])
def test_dl564_a_member_an_operator_resolved_gets_no_attempt(status: str) -> None:
    """An operator's SUCCESS, FAILURE or TERMINATED on the waiting member leaves a status that
    is not INACTIVE; an operator's INACTIVE leaves the box's resolution mark. Each is a verdict,
    and `Q`, which runs, keeps `P` open. The box gate alone would let the attempt start `X`."""
    sibling = WAITING + _member("Q")
    oracle = _running(sibling, _ev("STATUS", "X", 0, status=status))
    assert oracle.store.job["P"].status == "RUNNING"
    _update(oracle, sibling.replace("condition: s(Y)\n", ""))
    oracle.advance(_at(1))
    assert _row(oracle, "X") == (status, 0)


def test_dl564_a_held_member_stays_held_and_starts_at_its_release() -> None:
    """The hold gate refuses the attempt; the release's own attempt starts it (SEM-21)."""
    oracle = _running(WAITING, _ev("ON_HOLD", "X", 0))
    _update(oracle, WAITING.replace("condition: s(Y)\n", ""))
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("INACTIVE", 0) and oracle.store.job["X"].on_hold
    oracle.feed(_ev("OFF_HOLD", "X", 2))
    assert _row(oracle, "X") == ("RUNNING", 1)


def test_dl564_an_iced_member_stays_iced_and_sits_the_run_out_at_its_release() -> None:
    """The ice gate refuses the attempt. Taken off ice in the running box, the member sits the
    run out under the default `off-ice-in-running-box=next-run` (SEM-20), and `P` completes
    without it once `Q` ends."""
    sibling = WAITING + _member("Q")
    oracle = _running(sibling, _ev("ON_ICE", "X", 0))
    _update(oracle, sibling.replace("condition: s(Y)\n", ""))
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("INACTIVE", 0) and oracle.store.job["X"].on_ice
    oracle.feed(_ev("OFF_ICE", "X", 2))
    assert _row(oracle, "X") == ("INACTIVE", 0)
    oracle.feed(_ev("STATUS", "Q", 3, status="SUCCESS"))
    assert oracle.store.job["P"].status == "SUCCESS" and _row(oracle, "X") == ("INACTIVE", 0)


def test_dl564_a_member_of_a_box_that_is_not_running_does_not_start() -> None:
    oracle = Oracle(lower_source(WAITING))
    oracle.advance(_at(0))
    assert _update(oracle, WAITING.replace("condition: s(Y)\n", "")) == ("X",)
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("INACTIVE", 0) and _row(oracle, "P") == ("INACTIVE", 0)


def test_dl564_the_queue_scan_goes_first_so_an_older_waiter_takes_the_freed_unit() -> None:
    """The update raises `R` from one unit to two while `H` holds one: one unit is free. `W`
    waited for it before the update, so the scan admits `W` first, and `X`'s attempt queues."""
    needs = "resources: (R, QUANTITY=1, FREE=A)\n"
    text = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        + _cmd("H", needs)
        + _cmd("W", needs)
        + _box("P")
        + _member("X", needs + "condition: s(Y)\n")
        + _cmd("Y")
    )
    oracle = _running(text, _ev("FORCE_STARTJOB", "H", 0), _ev("STARTJOB", "W", 0))
    assert [_row(oracle, j)[0] for j in "HWX"] == ["RUNNING", "QUE_WAIT", "INACTIVE"]
    raised = text.replace("amount: 1", "amount: 2").replace("condition: s(Y)\n", "")
    assert _update(oracle, raised) == ("X",)
    oracle.advance(_at(1))
    assert [_row(oracle, j)[0] for j in "HWX"] == ["RUNNING", "RUNNING", "QUE_WAIT"]


def test_dl564_a_start_the_attempt_makes_pays_its_owed_scan_in_the_same_input() -> None:
    """DL-255 after the attempts: machine `L` has 80 units and a 30-unit holder; `R` has two
    units, one held. `W` (priority 5, 40 units, both of `R`) is past its load check and short on
    `R`, so it blocks `N` (priority 9, one of `R`). `X`'s attempt starts it on `L` with 40
    units, which sends `W` back to its load check (30 + 40 + 40 > 80). The scan that start owes
    runs in the same input, so `N` starts there, with no unit freed."""

    def on_l(name: str, extra: str) -> str:
        return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: L\n{extra}\n"

    text = (
        "insert_machine: L\ntype: a\nnode_name: L\nmax_load: 80\n\n"
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        + on_l("H", "job_load: 30\npriority: 1\n")
        + _cmd("G", "resources: (R, QUANTITY=1)\n")
        + _cmd("N", "priority: 9\nresources: (R, QUANTITY=1)\n")
        + on_l("W", "job_load: 40\npriority: 5\nresources: (R, QUANTITY=2)\n")
        + _box("P")
        + on_l("X", "job_load: 40\nbox_name: P\ncondition: s(Y)\n")
        + _cmd("Y")
    )
    events = [_ev("STARTJOB", j, 0) for j in "HG"] + [_ev("STARTJOB", j, 1) for j in "WN"]
    oracle = _running(text, *events)
    assert [_row(oracle, j)[0] for j in "WNX"] == ["QUE_WAIT", "QUE_WAIT", "INACTIVE"]
    assert _update(oracle, text.replace("condition: s(Y)\n", "")) == ("X",)
    oracle.advance(_at(2))
    assert [_row(oracle, j)[0] for j in "WNX"] == ["QUE_WAIT", "RUNNING", "RUNNING"]


def test_dl564_a_later_swap_that_retypes_an_owed_member_ends_its_debt() -> None:
    """Swap 1 owes `X` an attempt; swap 2, before any input, makes `X` a file watcher: another
    job under the name, which no update changed. The next input starts nothing for it."""
    old = WAITING + _member("Q")
    first = old.replace("condition: s(Y)\n", "")
    assert _update(oracle := _running(old), first) == ("X",)
    watcher = "insert_job: X\njob_type: f\nwatch_file: /synthetic/never\nmachine: m1\nbox_name: P\n"
    second = first.replace(
        "insert_job: X\njob_type: c\ncommand: x\nmachine: m1\nbox_name: P\n", watcher
    )
    assert oracle.catalog.jobs["X"].job_type == "CMD"
    assert _update(oracle, second, rev=3) == () and oracle._woken == {}
    assert lower_source(second).jobs["X"].job_type == "FW"
    oracle.advance(_at(1))
    assert _row(oracle, "X") == ("INACTIVE", 0)


def test_dl564_attempts_go_in_catalog_order_and_a_later_swap_keeps_or_drops_each() -> None:
    """Two swaps with no input between: the second keeps the first one's names and adds its
    own, each with its own update's cause. A name the later catalog dates or takes out of every
    box gets no attempt. The attempts go in catalog order, `B` before `A`."""
    dated = 'date_conditions: 1\ndays_of_week: all\nstart_times: "18:00"\n'

    def estate(b: str, a: str, d: str, u: str, u_box: bool = True) -> str:
        last = _member("U", u) if u_box else _cmd("U", u)
        return _box("P") + _member("B", b) + _member("A", a) + _member("D", d) + last + _cmd("Y")

    waits = "condition: s(Y)\n"
    oracle = _running(estate(waits, waits, waits, waits))
    assert _update(oracle, estate("", "", "", "")) == ("A", "B", "D", "U")
    second = estate("condition: n(Y)\n", "", dated, "", u_box=False)
    assert _update(oracle, second, rev=3) == ("B",)
    third = CAUSE.replace("rev 2", "rev 3")
    assert oracle._woken == {"A": CAUSE, "B": third, "D": CAUSE, "U": CAUSE}
    oracle.advance(_at(1))
    starts = [e for e in oracle.trace() if e.transition == "INACTIVE->STARTING"]
    assert [(e.job, e.cause) for e in starts[1:]] == [("B", third), ("A", CAUSE)]
    assert _row(oracle, "D") == ("INACTIVE", 0) and _row(oracle, "U") == ("INACTIVE", 0)
    assert oracle._woken == {}


def test_dl564_a_snapshot_between_the_swap_and_the_next_input_carries_the_attempt_once() -> None:
    """The carried rows hold the owed names and their causes; the restored oracle makes the
    attempt at its first input and owes nothing after. Rows that owe none have no key."""
    oracle = _running()
    assert "woken" not in oracle_rows(oracle)
    new = WAITING.replace("condition: s(Y)\n", "")
    _update(oracle, new)
    dumped = oracle_rows(oracle)
    assert dumped["woken"] == {"X": CAUSE} and dumped["wake_owed"]
    carried = CarriedRows.model_validate(decode(canonical_bytes(dumped)))
    restored = Oracle(lower_source(new), carried=carried)
    restored.advance(_at(1))
    oracle.advance(_at(1))
    assert _row(restored, "X") == _row(oracle, "X") == ("RUNNING", 1)
    assert restored.store.job["X"].started_by == CAUSE
    assert restored._woken == {} and "woken" not in oracle_rows(restored)
    assert Oracle(lower_source(new), carried=CarriedRows(now=T))._woken == {}
