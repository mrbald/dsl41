"""The oracle's cascade on the executor's worklist (docs/statechart-engine.md
§5, §8, §11; DL-350).

An input is a root item on the job's instance; its cascade is the items the
rows' actions report, popped depth first; the oracle's hook writes what each
microstep's rows reached before the next item pops. These cases pin:

- the hook: every fired row has a write, and a call into the executor from
  the cascade is `ReentrantDispatch`; an exception leaves the instances
  rebuilt from the rows; the commit's invariant; `Executor.realign` is gone;
- the order: each of the cases where the worklist could part from a
  recursive cascade's order, with the chart's items pinned;
- the bound: job.100 counts the job's `start admitted` items in the
  ancestry (DL-304);
- the audiences: each gate a sender reads, with a mutant that widens it and
  that parts from the oracle on the input;
- the settlement: an owed one before an attempt's admission (job.401), and
  the row's own move written by the verb that decides it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from oracle_inputs import FLAG_JIL, observations

from dsl41.ir import lower_source
from dsl41.job_chart_rows import configuration
from dsl41.oracle import Oracle
from dsl41.oracle_cascade import OracleCascade
from dsl41.oracle_facts import DoorFacts, RowUnits
from dsl41.oracle_intents import StatusLine
from dsl41.oracle_state import CascadeDepthError, Event, EventKind, OracleError
from dsl41.scan_chart import IDLE as SCAN_IDLE
from dsl41.scan_chart import SCAN_KEY
from dsl41.semantics import resolve
from dsl41.state_machine import (
    COMPLETION,
    HITS_ENV,
    Executor,
    Outcome,
    ReentrantDispatch,
    StatechartError,
)
from dsl41.state_machine import Event as ChartEvent

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def _paired(jil: str, setup: list[Event], factory: Any = Oracle, **kw: Any) -> Oracle:
    """An oracle of `factory` over `jil`, fed `setup`."""
    oracle: Oracle = factory(lower_source(jil), **kw)
    for event in setup:
        oracle.feed(event)
    return oracle


def _spied(oracle: Oracle) -> list[Outcome]:
    """Every outcome the oracle's hook writes from now on, in order."""
    seen: list[Outcome] = []
    perform = oracle._perform  # type: ignore[attr-defined]

    def spy(outcome: Outcome) -> None:
        seen.append(outcome)
        perform(outcome)

    oracle._perform = spy  # type: ignore[attr-defined]
    return seen


def _name(key: object) -> str:
    """A job instance's job, or the queue scan's coordinator by its key."""
    text = str(key)
    return text.partition(":")[2] if text.startswith("job:") else text


def _items(seen: list[Outcome], *names: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """(job, event, rows) of the outcomes whose event is named, in order."""
    return [
        (_name(o.key), o.event.name, o.rows) for o in seen if not names or o.event.name in names
    ]


def _with(**methods: Callable[..., Any]) -> type[Oracle]:
    """The oracle with some of its methods replaced: a mutant."""
    return type("Mutant", (Oracle,), dict(methods))


def _cmd(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n{extra}\n"


def _box(name: str, extra: str = "") -> str:
    return f"insert_job: {name}\njob_type: b\n{extra}\n"


# ---- the hook


def test_a_call_into_the_executor_from_the_cascade_is_reentrant() -> None:
    """The cascade runs inside one macrostep: a dispatch from the hook, as
    the recursion made one for every nested attempt, raises
    `ReentrantDispatch`, with the item's facts."""

    class Recursing(Oracle):
        def _w_kill(self, job: str, outcome: Outcome) -> None:
            self.store.machines.dispatch(self.store.job_key("n"), outcome.event)

    oracle = Recursing(lower_source(_cmd("j") + _cmd("n")))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    with pytest.raises(ReentrantDispatch) as raised:
        oracle.feed(ev("KILLJOB", 2, job="j"))
    assert ("item", "the event KILLJOB") in raised.value.facts
    # the instances are the rows' again, and the oracle goes on
    assert oracle.store.machines.active("job:j") == configuration("RUNNING")
    oracle.feed(ev("STATUS", 3, job="j", exit_code=0))
    assert oracle.store.job["j"].status == "SUCCESS"


def test_a_fired_row_the_oracle_has_no_write_for_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row the chart can fire has a write in the hook; one with none is
    a statechart error at its microstep, before the rows after it."""
    from dsl41 import oracle_writers

    writers = dict(oracle_writers._WRITERS)
    del writers["job.117"]
    monkeypatch.setattr(oracle_writers, "_WRITERS", writers)
    oracle = Oracle(lower_source(_cmd("j")))
    with pytest.raises(StatechartError, match="'j': STATUS fired job.117, and no row") as raised:
        oracle.feed(ev("STATUS", 1, job="j", exit_code=0))
    assert ("instance", "'job:j'") in raised.value.facts
    assert oracle.store.job["j"].status == "INACTIVE"


def test_a_status_entered_with_no_line_or_a_line_with_no_status_is_a_statechart_error() -> None:
    """A status leaf's entry reports its status, which the hook writes with the line the
    entering row's writer gives: a status row whose writer gives none, or a writer that gives
    one for a row that enters no status, raises at its microstep."""

    class Lineless(Oracle):
        def _w_kill(self, job: str, outcome: Outcome) -> None:
            """Planted: the kill's TERMINATED has no line."""

    class Stray(Oracle):
        def _w_hold_on(self, job: str, outcome: Outcome) -> StatusLine:
            """Planted: a hold gives a status line."""
            super()._w_hold_on(job, outcome)
            return StatusLine("stray")

    oracle: Oracle = Lineless(lower_source(_cmd("j")))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    lineless = "KILLJOB fired job.114, which entered TERMINATED and whose writers gave 0 status"
    lineless += " lines"
    with pytest.raises(StatechartError, match=lineless):
        oracle.feed(ev("KILLJOB", 2, job="j"))
    assert oracle.store.job["j"].status == "RUNNING"
    oracle = Stray(lower_source(_cmd("j")))
    stray = "ON_HOLD fired job.204, which entered no status and whose writers gave 1 status"
    stray += " line(?!s)"
    with pytest.raises(StatechartError, match=stray):
        oracle.feed(ev("ON_HOLD", 1, job="j"))
    assert oracle.store.job["j"].status == "INACTIVE"


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("box rules", "<box rules of m>"),
        ("member moves", "DoorFacts('b'"),
        (COMPLETION, "DoorFacts('b'"),
        ("wake", "<wake of m>"),
        ("box recompute", "RecomputeFact"),
    ],
    ids=["box rules", "member moves", "a door phase's completion", "wake", "box recompute"],
)
def test_an_error_inside_the_cascade_names_the_item_s_payload(event: str, payload: str) -> None:
    """A statechart error out of an item's write carries the item's facts,
    its payload among them, cut to the executor's length: a moment names its
    step and the job it is for, and the facts a box's rows read their kind."""

    class Failing(Oracle):
        armed = False

        def _perform(self, outcome: Outcome) -> None:
            if self.armed and outcome.event.name == event:
                raise StatechartError("planted")
            super()._perform(outcome)

    jil = _box("b") + _cmd("m", "box_name: b\n") + _cmd("o", "box_name: b\n")
    oracle = Failing(lower_source(jil))
    if event == "box recompute":
        # an idle box re-derives its status on a member's terminal move
        last = ev("STATUS", 1, job="m", status="SUCCESS")
    else:
        oracle.feed(ev("STARTJOB", 1, job="b"))
        last = ev("STATUS", 2, job="m", exit_code=0)
    oracle.armed = True
    with pytest.raises(StatechartError, match="planted") as raised:
        oracle.feed(last)
    assert dict(raised.value.facts)["payload"].startswith(payload)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (ev("SET_GLOBAL", 1, value="x"), "SET_GLOBAL requires payload.name"),
        (ev("MUST_START_ALARM", 1, job="j"), "uninjectable event kind 'MUST_START_ALARM'"),
    ],
    ids=["a global with no name", "an alarm fed as an input"],
)
def test_an_input_the_oracle_cannot_take_is_refused(event: Event, message: str) -> None:
    """The oracle refuses an input it cannot take, with its message, and
    moves no row."""
    oracle = Oracle(lower_source(_cmd("j")))
    with pytest.raises(OracleError, match=message):
        oracle.feed(event)
    assert oracle.store.job["j"].status == "INACTIVE"


def test_job_312_is_no_row_the_oracle_writes() -> None:
    """job.401 settles an ended run before its job's next admission, so the
    admitted start never meets a run's unsettled units (job.312): the row is
    marked unreachable, the hook has no write for it, and a chart that fires
    it is caught."""
    from dsl41 import oracle_writers
    from dsl41.job_chart import JOB

    assert next(row for row in JOB.rows if row.id == "job.312").mark == "unreachable"
    assert "job.312" not in oracle_writers._WRITERS
    unmarked = {"job.420", "job.421", "job.422", "job.423"}
    assert unmarked.isdisjoint(oracle_writers._WRITERS)


def test_noexec_moved_at_admitted_is_the_tripwire_job_453(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A box at ADMITTED meets `noexec moved` only if ON_NOEXEC reached it there, itself the
    marked job.423. The marked job.453 takes the cell, ahead of job.452 and job.198 at
    `lifecycle`, and the hook has no write for it, so the oracle raises before any write. The
    planted fire's hits stay out of the session."""
    from dsl41.job_chart_events import NOEXEC_MOVED
    from dsl41.oracle_facts import NoexecFacts

    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    oracle = Oracle(lower_source(FLAG_JIL))
    key = oracle.store.job_key("fl_box")
    chart, _, data = oracle._job_instance("fl_box", oracle._runtime("fl_box"))
    admitted = frozenset(
        {"job", "lifecycle", "ADMITTED", "flags", "ice_off", "hold_off", "noexec_on", "none"}
    )
    executor = Executor()  # ADMITTED is transient: no row projects to it, so it is planted
    executor.restore(key, chart, admitted, data)
    event = ChartEvent(NOEXEC_MOVED, NoexecFacts(oracle, "fl_box"))
    assert executor.resolve(key, event).rows == ("job.453",)
    with pytest.raises(StatechartError, match="noexec moved fired job.453, and no row the oracle"):
        executor.dispatch(key, event, oracle._perform)
    assert oracle.store.job["fl_box"].status == "INACTIVE"


def test_every_row_the_chart_can_fire_has_a_writer_on_the_oracle() -> None:
    """The hook's table names one writer for each row not marked unreachable,
    and no other; each names a method of `Oracle`. A new row without a writer
    fails here, not at its first fire."""
    from dsl41 import oracle_writers
    from dsl41.job_chart import JOB

    reachable = {row.id for row in JOB.rows if not row.mark}
    assert set(oracle_writers._WRITERS) == reachable
    for row, name in oracle_writers._WRITERS.items():
        assert callable(getattr(Oracle, name, None)), (row, name)


def test_every_action_that_asks_the_tier_names_itself_and_has_a_handler() -> None:
    """Each job chart action that asks the tier what its row sends passes its
    own name (`Units.consequences`), and the cascade answers each name with
    one method: `_after` for a status move on its own, else the method
    `_HANDLERS` names (DL-356). A grep for an action's name finds its
    handler; a new action without one fails here, not at its first fire."""
    from dsl41 import oracle_cascade
    from dsl41.job_chart import JOB
    from dsl41.job_chart_events import JobData, Payload
    from dsl41.state_machine import Context
    from dsl41.state_machine import Event as ChartEvent

    asked: list[str] = []

    class Recording:
        def release(self, success: bool) -> str:
            return "free"

        def consequences(self, ctx: Context, action: str) -> None:
            asked.append(action)

    own = {
        "relay_start",
        "settle_ended_run",
        "report_ice",
        "moment",
        "reads_overrides",
        "reads_fold",
    }
    for name, action in JOB.actions.items():
        if name in own:
            continue
        del asked[:]
        payload = Payload(units=Recording())  # type: ignore[arg-type]
        action(Context("job:j", ChartEvent("e", payload), configuration("INACTIVE"), JobData()))
        assert asked == [name]
    delegating = set(JOB.actions) - own
    assert oracle_cascade._MOVES.isdisjoint(oracle_cascade._HANDLERS)
    assert oracle_cascade._MOVES | set(oracle_cascade._HANDLERS) == delegating
    for name, method in oracle_cascade._HANDLERS.items():
        assert method == f"_on_{name}" and callable(getattr(OracleCascade, method, None))


def test_a_macrostep_checks_each_instance_it_reached_against_its_row() -> None:
    """At a macrostep's end every job an item of it reached has its row
    project to its instance's configuration: a hook that skipped a write is
    caught there, though the input touched no row of the job (DL-350)."""

    class Unheld(Oracle):
        def _w_hold_on(self, job: str, outcome: Outcome) -> None:
            """Planted: the hold the chart reached is never written."""

    oracle = Unheld(lower_source(_cmd("j")))
    with pytest.raises(StatechartError, match="'j': at the macrostep's end its instance") as e:
        oracle.feed(ev("ON_HOLD", 1, job="j"))
    assert "hold_on" in str(e.value) and "hold_off" in str(e.value)


_DATED = _cmd("dated", 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n')


def test_a_microstep_that_omits_the_arm_its_row_latches_is_a_statechart_error() -> None:
    """The arm is no state of the chart, so the hook checks it after each microstep's writes
    (DL-365): a tick that latches (job.429) and leaves the row unarmed raises before the next
    item."""

    class Unarmed(Oracle):
        def _w_arm(self, job: str, outcome: Outcome) -> None:
            """Planted: the arm the row latches is never written."""

    oracle = Unarmed(lower_source(_DATED))
    oracle.feed(ev("ON_HOLD", 1, job="dated"))
    with pytest.raises(StatechartError, match="'dated': start fired job.429") as e:
        oracle.feed(ev("STARTJOB", 2, job="dated"))
    assert "arm is False, where the rows declare True" in str(e.value)


def test_a_microstep_that_moves_the_arm_with_no_arm_row_is_a_statechart_error() -> None:
    """A microstep that fires no arm row leaves the arm as it was (DL-365): a hold's release
    that also clears a latched arm raises before the release's start attempt pops."""

    class SpuriousClear(Oracle):
        def _w_hold_off(self, job: str, outcome: Outcome) -> None:
            """Planted: OFF_HOLD's write also clears the arm."""
            super()._w_hold_off(job, outcome)
            self.store.move_flag(job, "arm_off")

    oracle = SpuriousClear(lower_source(_DATED))
    oracle.feed(ev("ON_HOLD", 1, job="dated"))
    oracle.feed(ev("STARTJOB", 2, job="dated"))
    assert oracle.store.job["dated"].armed
    with pytest.raises(StatechartError, match="'dated': OFF_HOLD fired job.205") as e:
        oracle.feed(ev("OFF_HOLD", 3, job="dated"))
    assert "arm is False, where the rows declare True" in str(e.value)


def test_the_commit_checks_each_touched_row_against_its_instance() -> None:
    """At an input's commit every job the input touched has its row project
    to its instance's configuration (DL-350): here a row written with
    no microstep, which no macrostep reached."""
    oracle = Oracle(lower_source(_cmd("j")))
    store = oracle.store
    store.begin_input()
    store.move_flag("j", "ice_on")
    with pytest.raises(StatechartError, match="'j': at the input's commit its instance") as e:
        store.commit_input()
    assert "ice_on" in str(e.value) and "ice_off" in str(e.value)


def test_the_executor_has_no_realign() -> None:
    """Nothing outside the chart moves a job row in the oracle, so no
    instance is brought back to its row: the transitional method is gone, and
    the store follows no job."""
    assert not hasattr(Executor, "realign")
    oracle = Oracle(lower_source(_cmd("j")))
    assert not hasattr(oracle.store, "follow_job")


def test_an_exception_out_of_a_macrostep_leaves_the_instances_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oracle has no rollback: a write that raised leaves what was
    written, the instances are rebuilt from the rows and the cascade's
    bookkeeping is empty again, so the next input starts clean."""

    def broken(self: Oracle, job: str, outcome: Outcome) -> None:
        raise OracleError("planted")

    jil = "insert_resource: R\nres_type: R\namount: 1\n\n" + _cmd(
        "j", "resources: (R, QUANTITY=1, FREE=Y)\n"
    )
    oracle = Oracle(lower_source(jil))
    with monkeypatch.context() as patch:
        patch.setattr(Oracle, "_w_reserve", broken)
        with pytest.raises(OracleError, match="planted"):
            oracle.feed(ev("STARTJOB", 1, job="j"))
    assert oracle.store.machines.active("job:j") == configuration("INACTIVE")
    assert oracle.store.machines.active(SCAN_KEY) == SCAN_IDLE
    assert (oracle._window_starts, oracle._late_settlements) == (None, {})
    oracle.feed(ev("STARTJOB", 2, job="j"))
    assert oracle.store.job["j"].status == "RUNNING"


def test_a_recursion_error_at_the_root_is_the_cascade_error_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cascade takes no frame per start: only a fact's own recursion (a
    deep condition) meets the limit, inside the root item's macrostep, and
    the root dispatch names it as the cascade's first start."""

    def deep(self: Oracle, cond: Any, evaluator: str) -> bool:
        raise RecursionError("maximum recursion depth exceeded")

    oracle = Oracle(lower_source(_cmd("p") + _cmd("c", "condition: s(p)\n")))
    monkeypatch.setattr(Oracle, "_cond_true", deep)
    with pytest.raises(CascadeDepthError) as raised:
        oracle.feed(ev("STARTJOB", 1, job="c"))
    assert raised.value.job == "c" and "STARTJOB event" in str(raised.value)
    with pytest.raises(CascadeDepthError) as raised:
        oracle.feed(ev("STATUS", 2, job="p", exit_code=0))
    assert raised.value.job == "p" and "STATUS" in str(raised.value)


def test_a_status_move_sends_its_box_rules_then_its_settlement_then_its_wakes() -> None:
    """STATUS on the job's instance: the status row, then the move's `box
    rules`, its `settle`, whose `leave STARTING or RUNNING` frees the run's
    units by policy, and its `wake`, each a moment on the job's own instance;
    the release wakes the queue: the coordinator's pass finds no waiter and the
    scan ends."""
    jil = "insert_resource: R\nres_type: R\namount: 1\n\n" + _cmd(
        "j", "resources: (R, QUANTITY=1, FREE=Y)\n"
    )
    paired = _paired(jil, [ev("STARTJOB", 1, job="j")])
    seen = _spied(paired)
    paired.feed(ev("STATUS", 2, job="j", exit_code=0))
    assert _items(seen) == [
        ("j", "STATUS", ("job.117",)),
        ("j", "box rules", ("job.405",)),
        ("j", "settle", ("job.406",)),
        ("j", "leave STARTING or RUNNING", ("job.304",)),
        ("j", "wake", ("job.407",)),
        (SCAN_KEY, "scan", ("queue_scan.01",)),
        (SCAN_KEY, COMPLETION, ("queue_scan.04",)),
        (SCAN_KEY, COMPLETION, ("queue_scan.09",)),
    ]
    assert paired.store.machines.active("job:j") == configuration("SUCCESS")


# ---- the bound (DL-304)

_LOOP = _cmd("lp", "status: ON_NOEXEC\ncondition: s(lp)\n")


def test_the_bound_refuses_the_third_nested_start_with_the_l010_line() -> None:
    """An ON_NOEXEC job whose own SUCCESS starts it again: its first start
    bypasses and its SUCCESS starts it again, nested; the second's start
    again is the job's third, refused at job.100 with the L010 line. The
    attempt one below the bound is admitted."""
    paired = _paired(_LOOP, [])
    seen = _spied(paired)
    paired.feed(ev("FORCE_STARTJOB", 1, job="lp"))
    starts = [o.rows for o in seen if o.event.name == "start"]
    assert starts == [("job.101",), ("job.101",), ("job.100",)]

    class Refusing(Oracle):
        """Stops at the refusal, whose error's facts name its ancestry."""

        def _perform(self, outcome: Outcome) -> None:
            if outcome.rows == ("job.100",):
                raise StatechartError("refused")
            super()._perform(outcome)

    with pytest.raises(StatechartError) as raised:
        Refusing(lower_source(_LOOP)).feed(ev("FORCE_STARTJOB", 1, job="lp"))
    ancestry = dict(raised.value.facts)["ancestry"].split(" > ")
    assert [step for step in ancestry if step.endswith(" start admitted")] == [
        "'job:lp' start admitted",
        "'job:lp' start admitted",
    ]
    lines = [t.cause for t in paired.trace() if t.transition == "START_REFUSED"]
    assert lines == [
        "re-trigger loop inside one instant: started twice in one nested cascade; refused"
        " (L010; status of 'lp' changed to SUCCESS)"
    ]


@pytest.mark.parametrize(
    ("jil", "start"),
    [
        (_box("sb", "condition: s(sb)\n"), "sb"),
        (_box("r0", "condition: s(r1)\n") + _box("r1", "condition: s(r0)\n"), "r0"),
    ],
    ids=["a box completing at its start under s(B)", "a ring of two"],
)
def test_the_dl304_loop_shapes_end_with_the_refusal(jil: str, start: str) -> None:
    """The loop shapes DL-304 names end with the L010 line."""
    paired = _paired(jil, [])
    paired.feed(ev("FORCE_STARTJOB", 1, job=start))
    assert [t.transition for t in paired.trace()].count("START_REFUSED") == 1


def test_the_deepest_legal_shapes_run_at_python_s_default_limit() -> None:
    """At Python's default limit, set after the oracle raised it, the deep
    legal shapes of test_cascade_depth run to their end: the cascade takes no
    frame per nested start."""
    depth = 100
    chain = "".join(_box(f"q{i}", f"condition: s(q{i - 1})\n" if i else "") for i in range(depth))
    limit = sys.getrecursionlimit()
    try:
        oracle = Oracle(lower_source(chain))
        sys.setrecursionlimit(1000)
        oracle.feed(ev("FORCE_STARTJOB", 1, job="q0"))
        assert {row.status for row in oracle.store.job.values()} == {"SUCCESS"}
    finally:
        sys.setrecursionlimit(limit)


# ---- the settlement


_RESTART = (
    "insert_resource: U\nres_type: {kind}\namount: 1\n\n"
    + _box("rb", "condition: s(rb)\n")
    + _cmd("rm", "box_name: rb\nresources: (U, QUANTITY=1{free})\n")
)


@pytest.mark.parametrize(
    ("kind", "free", "row", "status"),
    [("R", ", FREE=Y", "job.102", "RUNNING"), ("D", "", "job.103", "QUE_WAIT")],
    ids=["admitted after the settlement", "queued after the settlement"],
)
def test_an_owed_settlement_settles_before_the_attempt_reads_its_admission(
    kind: str, free: str, row: str, status: str
) -> None:
    """DL-340, DL-349: rm's SUCCESS completes rb, which restarts and attempts
    rm inside rm's own box rules, before rm's `settle`. The attempt takes
    job.401: the ended run settles by the end it made, and the same attempt,
    sent again, reads the admission the settlement left. rm's own `settle`
    then finds the run settled (job.418 or job.406), and settles nothing."""
    jil = _RESTART.format(kind=kind, free=free)
    paired = _paired(jil, [ev("FORCE_STARTJOB", 1, job="rb"), ev("FORCE_STARTJOB", 2, job="rm")])
    seen = _spied(paired)
    paired.feed(ev("STATUS", 3, job="rm", exit_code=0))
    mine = [(name, rows) for job, name, rows in _items(seen) if job == "rm"]
    at = mine.index(("start", ("job.401",)))
    settled = "job.304" if kind == "R" else "job.306"
    assert mine[at + 1 : at + 3] == [
        ("leave STARTING or RUNNING", (settled,)),
        ("start", (row,)),
    ]
    assert paired.store.job["rm"].status == status
    late = [rows for name, rows in mine[at:] if name == "leave STARTING or RUNNING"]
    assert late == [(settled,)]


def test_a_settlement_row_that_holds_another_state_than_the_verb_writes_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settlement row the chart fires reads the policy when it pops, and
    the store's verb decides the move from the units. Planted: every end
    reads `keep`, where the policy frees: the row holds the units, the verb frees
    them, and the macrostep's end finds the instance off its row, a
    statechart error."""
    jil = "insert_resource: R\nres_type: R\namount: 1\n\n" + _cmd(
        "j", "resources: (R, QUANTITY=1, FREE=Y)\n"
    )
    oracle = Oracle(lower_source(jil))
    oracle.feed(ev("STARTJOB", 1, job="j"))
    monkeypatch.setattr(RowUnits, "release", lambda self, success: "keep")
    with pytest.raises(StatechartError, match="'j': at the macrostep's end its instance is in"):
        oracle.feed(ev("STATUS", 2, job="j", exit_code=0))


def test_a_settlement_with_no_handle_is_a_statechart_error_before_its_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DL-349: a settlement built with no handle on the job's units has no
    policy; the guard's read raises, with the instance and the item, before
    the release."""
    jil = "insert_resource: K\nres_type: R\namount: 1\n\n" + _cmd(
        "keep", "resources: (K, QUANTITY=1, FREE=N)\n"
    )
    oracle = Oracle(lower_source(jil))
    oracle.feed(ev("STARTJOB", 1, job="keep"))
    monkeypatch.setattr(OracleCascade, "_units", lambda self, job: None)
    with pytest.raises(StatechartError, match="no release and no handle") as raised:
        oracle.feed(ev("STATUS", 2, job="keep", exit_code=0))
    assert ("item", "the event leave STARTING or RUNNING") in raised.value.facts
    assert oracle.store.job["keep"].reservations


def test_a_settlement_with_no_move_is_a_statechart_error_before_its_write() -> None:
    """The verb that settles a run's units writes the move the settlement
    names; one that names none has no write, and the units stay held."""

    class Moveless(Oracle):
        def _perform(self, outcome: Outcome) -> None:
            if outcome.event.name == "leave STARTING or RUNNING":
                outcome.event.payload.move = None
            super()._perform(outcome)

    jil = "insert_resource: K\nres_type: R\namount: 1\n\n" + _cmd(
        "keep", "resources: (K, QUANTITY=1, FREE=N)\n"
    )
    oracle = Moveless(lower_source(jil))
    oracle.feed(ev("STARTJOB", 1, job="keep"))
    with pytest.raises(StatechartError, match="'keep': leave STARTING or RUNNING fired job.30"):
        oracle.feed(ev("STATUS", 2, job="keep", exit_code=0))
    assert oracle.store.job["keep"].reservations


def test_job_401_with_no_settlement_owed_is_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chart that takes job.401 where nothing is owed is caught at once."""
    from dsl41.oracle_facts import StartFacts

    monkeypatch.setattr(StartFacts, "settlement_owed", property(lambda self: True))
    oracle = Oracle(lower_source(_cmd("j")))
    with pytest.raises(StatechartError, match="'j': job.401 fired, and no settlement of run 0"):
        oracle.feed(ev("STARTJOB", 1, job="j"))
    assert oracle.store.job["j"].status == "INACTIVE"


# ---- the order: each case where the worklist could part from the recursion


def _sent(seen: list[Outcome], at: int) -> list[tuple[str, str]]:
    """What the step at `at` sent: the outcomes one deeper that pop before the
    next item as shallow as it, the moment's own next step left out."""
    step = seen[at]
    found: list[tuple[str, str]] = []
    for later in seen[at + 1 :]:
        if later.depth <= step.depth:
            break
        if later.depth == step.depth + 1 and later.event.name != step.event.name:
            found.append((_name(later.key), later.event.name))
    return found


def _steps(seen: list[Outcome], name: str, job: str) -> list[int]:
    """Where each step of the moment `name` on `job` popped."""
    key = f"job:{job}"
    return [at for at, o in enumerate(seen) if o.event.name == name and str(o.key) == key]


def _children(seen: list[Outcome], name: str, job: str) -> list[tuple[str, str]]:
    """What every step of the moment `name` on `job` sent, in pop order."""
    return [sent for at in _steps(seen, name, job) for sent in _sent(seen, at)]


def test_the_batch_s_second_phase_skips_a_row_a_wake_started_again() -> None:
    """The batch INACTIVE path: B and its running member m1 are written and
    settled first (m1 keeps its renewable unit). B's own wake in the second
    phase starts B again (`n(B)`), and B's run starts m1: m1 has moved on, so
    the phase gives it no box rules and no wake, and the unit its first phase
    released owes the queue a scan, at the batch's end."""
    jil = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        + _box("B", "condition: n(B)\n")
        + _cmd("m1", "box_name: B\nresources: (R, QUANTITY=1, FREE=Y)\n")
    )
    paired = _paired(jil, [ev("FORCE_STARTJOB", 1, job="B")])
    seen = _spied(paired)
    paired.feed(ev("STATUS", 2, job="B", status="INACTIVE"))
    # B's own wake starts B; B's STARTING is its completion's move (DL-364)
    assert _children(seen, "notify step", "B") == [
        ("m1", "box run ends"),
        ("B", "box rules"),
        ("B", "wake"),
        (SCAN_KEY, "scan"),
    ]
    rows = paired.store.job
    assert (rows["B"].run_number, rows["m1"].status, rows["m1"].run_number) == (2, "RUNNING", 2)


def test_a_scan_requested_inside_a_pass_asks_nothing_and_the_pass_goes_on() -> None:
    """The queue's scan: W1's readmission completes the empty box at its start,
    and its release asks a scan inside the running pass. That scan latches a
    full pass (queue_scan.02) and sends nothing, and the completion that waits
    for W1's cascade is still queued: W1 moved, so the pass ends and the next
    admits W2 with the unit W1 gave back; the third finds no waiter."""
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
    paired = _paired(jil, setup)
    seen = _spied(paired)
    paired.feed(ev("RELEASE_RESOURCE", 5, job="h"))
    scan = [
        (_name(o.key), *o.rows, *o.branches)
        for o in seen
        if _name(o.key) == SCAN_KEY or o.rows == ("job.108",)
    ]
    assert scan == [
        (SCAN_KEY, "queue_scan.01"),
        (SCAN_KEY, "queue_scan.04", "queue_scan.10"),
        ("W1", "job.108"),
        (SCAN_KEY, "queue_scan.02"),
        (SCAN_KEY, "queue_scan.05"),
        (SCAN_KEY, "queue_scan.08"),
        (SCAN_KEY, "queue_scan.04", "queue_scan.10"),
        ("W2", "job.108"),
        (SCAN_KEY, "queue_scan.05"),
        (SCAN_KEY, "queue_scan.08"),
        (SCAN_KEY, "queue_scan.04", "queue_scan.11"),
        (SCAN_KEY, "queue_scan.09"),
    ]
    latch = next(at for at, o in enumerate(seen) if o.rows == ("queue_scan.02",))
    assert _sent(seen, latch) == []
    rows = paired.store.job
    assert (rows["W1"].status, rows["W2"].status) == ("SUCCESS", "RUNNING")


_WALK = (
    _cmd("x")
    + _box("G", "condition: s(G)\nbox_success: s(x)\n")
    + _box("P", "box_name: G\n")
    + _cmd("M", "box_name: P\n")
)
_WALK_SETUP = [ev("STATUS", 1, job="x", status="SUCCESS"), ev("FORCE_STARTJOB", 2, job="G")]


def test_the_walk_up_skips_an_ancestor_restarted_inside_it() -> None:
    """The walk up: M's SUCCESS completes P, P's SUCCESS
    completes G by its override, and G's wakes start it again, all inside M's
    box rules. M's walk up then finds G in a later run and reads none of its
    overrides: the run belongs to the earlier move (SEM-12)."""
    paired = _paired(_WALK, _WALK_SETUP)
    seen = _spied(paired)
    paired.feed(ev("STATUS", 3, job="M", status="SUCCESS"))
    # the SUCCESS's own walk pops last; M's moves in G's new run walk too
    assert _sent(seen, _steps(seen, "walk up", "M")[-1]) == []
    assert paired.store.job["G"].run_number == 2


def test_a_kill_inside_the_terminator_steps_ends_them_where_the_box_runs_again() -> None:
    """The box's end (`box ends step`): the kill of bx reaches `a`, whose
    TERMINATED starts bx again (`t(a)`); the next step reads the new run and
    sends `b`, carried RUNNING from the run that ended, nothing (DL-348)."""
    jil = (
        _box("bx", "condition: t(a)\n")
        + _cmd("a", "box_name: bx\njob_terminator: 1\n")
        + _cmd("b", "box_name: bx\nbox_terminator: 1\njob_terminator: 1\n")
    )
    paired = _paired(jil, [ev("FORCE_STARTJOB", 1, job="bx")])
    seen = _spied(paired)
    paired.feed(ev("KILLJOB", 2, job="bx"))
    assert _children(seen, "box ends step", "bx") == [("a", "box ends FAILURE or TERMINATED")]
    rows = paired.store.job
    assert (rows["b"].status, rows["b"].run_number) == ("RUNNING", 1)


_DAILY = 'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
_SKIPPED = _DAILY + 'run_window: "06:00-07:00"\n'


def test_a_box_started_inside_another_s_window_pass_runs_its_own() -> None:
    """The outermost window pass: O's pass skips its member, which completes O,
    whose SUCCESS starts X inside the pass. The pass was closed when its list
    was read, so X's RUNNING opens a pass of its own and runs it there,
    before O's ends (DL-246)."""
    jil = (
        _box("O")
        + _cmd("w", "box_name: O\n" + _SKIPPED)
        + _box("X", "condition: s(O)\n")
        + _cmd("xw", "box_name: X\n" + _SKIPPED)
    )
    paired = _paired(jil, [])
    seen = _spied(paired)
    paired.feed(ev("FORCE_STARTJOB", 1, job="O"))
    passes = [(str(o.key)[4:], o.depth) for o in seen if o.event.name == "window pass"]
    first_x = next(i for i, (job, _depth) in enumerate(passes) if job == "X")
    last_o = max(i for i, (job, _depth) in enumerate(passes) if job == "O")
    assert first_x < last_o
    assert _children(seen, "window pass", "X") == [("xw", "start")]
    rows = paired.store.job
    assert (rows["O"].status, rows["X"].status) == ("SUCCESS", "SUCCESS")


def test_a_freed_excess_s_scan_comes_before_the_load_mark_and_the_owed_scan_runs() -> None:
    """An admitted start's extent: an admission on held units the catalog no longer
    asks for frees the excess, whose scan admits the waiter, and only then is
    the input's admit-only scan owed for the load the start takes (DL-255):
    the input ends with that scan as a root item of its own. The scan and the
    load mark follow `started` from ADMITTED's completion, the extent's last
    item (DL-364)."""
    jil = (
        "insert_machine: box1\ntype: a\nnode_name: box1\nmax_load: 2\n\n"
        "insert_resource: OLD\nres_type: R\namount: 1\n\n"
        "insert_resource: NEW\nres_type: R\namount: 1\n\n"
        "insert_job: ha\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n"
        "resources: (OLD, QUANTITY=1, FREE=N)\n\n"
        "insert_job: wa\njob_type: c\ncommand: y\nmachine: m1\nresources: (OLD, QUANTITY=1)\n\n"
        "insert_job: wl\njob_type: c\ncommand: z\nmachine: box1\njob_load: 3\npriority: 1\n"
    )
    setup = [
        ev("STARTJOB", 1, job="ha"),
        ev("STATUS", 2, job="ha", status="FAILURE"),
        ev("STARTJOB", 3, job="wa"),
        ev("STARTJOB", 3, job="wl"),
    ]
    paired = _paired(jil, setup)
    moved = lower_source(jil.replace("(OLD, QUANTITY=1, FREE=N)", "(NEW, QUANTITY=1, FREE=N)"))
    from dsl41.capacity import CapacityPool

    for side in (paired,):
        side.catalog = moved
        side._pool = CapacityPool(moved)
    seen = _spied(paired)
    paired.feed(ev("STARTJOB", 4, job="ha"))
    assert _children(seen, "start admitted", "ha")[-1] == ("ha", COMPLETION)
    starting = _children(seen, COMPLETION, "ha")
    assert starting[-3:] == [("ha", "started"), (SCAN_KEY, "scan"), ("ha", "load taken")]
    roots = [at for at, o in enumerate(seen) if o.depth == 0]
    owed = seen[roots[-1]]
    assert (_name(owed.key), owed.event.name) == (SCAN_KEY, "scan")
    # the mode the root sent: each readmission of its first pass carries it
    first = next(o for o in seen[roots[-1] :] if o.event.name == "readmit")
    assert first.event.payload.keep_stopped
    rows = paired.store.job
    assert (rows["wa"].status, rows["wl"].status) == ("RUNNING", "QUE_WAIT")


def _door(seen: list[Outcome], *jobs: str) -> list[tuple[str, tuple[str, ...]]]:
    """(box, rows) of each visit of a box's completion door and of its phases' completions,
    which carry the visit's payload (DL-371), in pop order."""
    return [
        (str(o.key).partition(":")[2], o.rows)
        for o in seen
        if isinstance(o.event.payload, DoorFacts) and str(o.key).partition(":")[2] in jobs
    ]


@pytest.mark.parametrize(
    ("first", "door"),
    [
        ("SUCCESS", ["job.455", "job.458", "job.454", "job.126"]),
        ("FAILURE", ["job.455", "job.458", "job.454", "job.456", "job.458"]),
    ],
    ids=["an override fires", "no override fires"],
)
def test_the_fold_follows_a_member_s_every_move_unless_an_override_ended_the_box(
    first: str, door: list[str]
) -> None:
    """The completion door: m's STARTING is no move the overrides
    read, so the fold alone follows it (job.455); m's RUNNING reads the
    box_success, which names m (job.454), and the fold follows only if it
    did not end the box (job.456)."""
    jil = _cmd("x") + _box("B", "box_success: s(m) | s(x)\n") + _cmd("m", "box_name: B\n")
    paired = _paired(jil, [ev("STATUS", 1, job="x", status=first)])
    seen = _spied(paired)
    paired.feed(ev("FORCE_STARTJOB", 2, job="B"))
    assert _children(seen, "box rules", "m") == [("B", "member moves"), ("B", "member moves")]
    assert [row for _box_name, rows in _door(seen, "B") for row in rows] == door
    assert paired.store.job["B"].status == ("SUCCESS" if first == "SUCCESS" else "RUNNING")


def _moved(paired: Oracle, traced: int) -> list[tuple[str, str]]:
    return [(t.job, t.transition) for t in paired.trace()[traced:]]


def test_a_member_killed_while_starting_ignores_its_own_started() -> None:
    """job.183 (`Started.current`): J's STARTING wakes X, which
    bypasses; X's SUCCESS meets B's box_failure, and B's end kills J, a
    job_terminator, while J is STARTING. J's `started` then pops on a
    TERMINATED job and is ignored."""
    jil = (
        _box("B", "box_failure: s(X)\n")
        + _cmd("J", "box_name: B\njob_terminator: 1\ncondition: s(Z)\n")
        + _cmd("X", "box_name: B\ncondition: s(J) | s(Z)\nstatus: ON_NOEXEC\n")
        + _cmd("Z")
    )
    paired = _paired(jil, [ev("FORCE_STARTJOB", 1, job="B"), ev("STARTJOB", 2, job="Z")])
    seen = _spied(paired)
    paired.feed(ev("STATUS", 3, job="Z", exit_code=0))
    assert ("J", "started", ("job.183",)) in _items(seen, "started")
    statuses = {job: paired.store.job[job].status for job in ("B", "J")}
    assert statuses == {"B": "FAILURE", "J": "TERMINATED"}


def test_an_empty_box_set_inactive_ends_no_member_s_run() -> None:
    """The batch INACTIVE path on a box with no member and no subbox: there is
    no run of a member to end, so the batch sends no disarm."""
    paired = _paired(_box("e"), [ev("STARTJOB", 1, job="e")])
    seen = _spied(paired)
    paired.feed(ev("STATUS", 2, job="e", status="INACTIVE"))
    assert not _items(seen, "box run ends")
    assert paired.store.job["e"].status == "INACTIVE"


def test_an_ice_in_a_subbox_walks_up_while_the_subbox_still_runs() -> None:
    """An ice's completion moment: the iced member is out of its box's run, a
    completion moment for the box (DL-285); the box still runs `m3`, so the
    walk up reaches the outer box, which the fold leaves RUNNING."""
    jil = (
        _box("O")
        + _box("I", "box_name: O\n")
        + _cmd("m1", "box_name: I\n")
        + _cmd("m2", "box_name: I\ncondition: s(x)\n")
        + _cmd("m3", "box_name: I\n")
        + _cmd("x")
    )
    paired = _paired(jil, [ev("STARTJOB", 1, job="O"), ev("STATUS", 2, job="m1", exit_code=0)])
    seen = _spied(paired)
    paired.feed(ev("ON_ICE", 3, job="m2"))
    # I's visit reads its overrides, then folds; the walk up reads O's overrides alone
    assert _door(seen, "I", "O") == [
        ("I", ("job.454",)),
        ("I", ("job.456",)),
        ("I", ("job.458",)),
        ("O", ("job.454",)),
        ("O", ("job.457",)),
    ]
    assert (paired.store.job["I"].status, paired.store.job["O"].status) == (
        "RUNNING",
        "RUNNING",
    )


#: H > G > I > M: G is a job_terminator of H, I is iced with a box_success that never holds,
#: and H fails on `s(M)`; M runs, started before H, which keeps it live.
_VISITS = (
    _box("H", "box_failure: s(M)\n")
    + _box("G", "box_name: H\njob_terminator: 1\n")
    + _box("I", "box_name: G\nbox_success: s(N)\n")
    + _cmd("M", "box_name: I\n")
    + _cmd("N", "condition: s(N)\n")
)
_VISITS_SETUP = [
    ev("ON_ICE", 1, job="I"),
    ev("FORCE_STARTJOB", 2, job="M"),
    ev("FORCE_STARTJOB", 3, job="H"),
]
_VISITS_LAST = ev("STATUS", 4, job="M", status="SUCCESS")


def test_the_walk_up_reads_an_ancestor_s_overrides_and_never_folds_it() -> None:
    """The three visits stay distinct (DL-371): H > G > I > M, G a job_terminator of H, I iced
    with a box_success that never holds, H failing on `s(M)`. M's SUCCESS cannot recompute
    idle I, whose unmet override suppresses the default. The walk up visits G for its
    overrides alone (job.454, job.457), then H, whose box_failure ends it and terminates G. A
    fold at G's visit would find iced I out of the run and end G SUCCESS first, so H's
    terminator would meet a terminal G."""
    paired = _paired(_VISITS, _VISITS_SETUP)
    statuses = {job: paired.store.job[job].status for job in "HGIM"}
    assert statuses == {"H": "RUNNING", "G": "RUNNING", "I": "INACTIVE", "M": "RUNNING"}
    seen = _spied(paired)
    paired.feed(_VISITS_LAST)
    assert _door(seen, "G", "H") == [
        ("G", ("job.454",)),
        ("G", ("job.457",)),
        ("H", ("job.454",)),
        ("H", ("job.127",)),
    ]
    [visit] = [o.event.payload for o in seen if o.event.name == "member moves" and o.key == "job:G"]
    with pytest.raises(StatechartError, match="the fold read before its phase"):
        _ = visit.fold
    statuses = {job: paired.store.job[job].status for job in "HGIM"}
    assert statuses == {"H": "FAILURE", "G": "TERMINATED", "I": "INACTIVE", "M": "SUCCESS"}


def test_a_walk_up_that_folds_diverges() -> None:
    """Mutant: the walk up's visit folds after the overrides, as the full door does. G ends
    SUCCESS in the counter-example above, where the oracle terminates it."""

    def folding(self: Oracle, box: str, *args: Any, **kw: Any) -> Any:
        return OracleCascade._door(self, box, *args, **{**kw, "folds": True})

    mutant = _paired(_VISITS, [*_VISITS_SETUP, _VISITS_LAST], factory=_with(_door=folding))
    assert mutant.store.job["G"].status == "SUCCESS"
    _diverges(_with(_door=folding), _VISITS, _VISITS_SETUP, _VISITS_LAST)


_LEAVING = "insert_resource: Q\nres_type: R\namount: 1\n\n" + _cmd(
    "h", "resources: (Q, QUANTITY=1)\n"
)


def test_a_waiter_its_own_leave_started_again_takes_nothing_more_from_it() -> None:
    """A waiter's leave under `queued-recheck=1`: W's condition is
    false, so it leaves the queue INACTIVE; its own INACTIVE starts R, then
    x, whose SUCCESS starts W again. The leave's tail reads W moved on and
    does nothing more."""
    jil = (
        _LEAVING
        + _box("R", "condition: n(W)\n")
        + _box("x", "condition: s(R)\n")
        + _cmd("W", "condition: s(x)\nresources: (Q, QUANTITY=1)\n")
    )
    setup = [
        ev("STARTJOB", 1, job="h"),
        ev("FORCE_STARTJOB", 2, job="x"),
        ev("STARTJOB", 3, job="W"),
        ev("STATUS", 4, job="x", status="INACTIVE"),
    ]
    paired = _paired(jil, setup, semantics=resolve({"queued-recheck": "1"}))
    assert paired.store.job["W"].status == "QUE_WAIT"
    traced = len(paired.trace())
    seen = _spied(paired)
    paired.feed(ev("STATUS", 5, job="h", status="SUCCESS"))
    moved = [move for job, move in _moved(paired, traced) if job == "W"]
    assert moved == ["QUE_WAIT->INACTIVE", "INACTIVE->STARTING", "STARTING->RUNNING"]
    leave = _steps(seen, "after leave", "W")
    assert len(leave) == 1 and _sent(seen, leave[0]) == []


def test_a_member_whose_leave_ended_its_box_s_run_takes_nothing_more_from_it() -> None:
    """SEM-20, DL-257: `c`, off ice in its box's run and forced, queues; its
    condition is false when it leaves the queue, so it goes INACTIVE, and
    with `k` ended its exit completes the box. The leave's tail reads the
    box's run over and does nothing more."""
    jil = (
        _LEAVING
        + _box("bx")
        + _cmd("a", "box_name: bx\n")
        + _cmd("c", "box_name: bx\ncondition: s(a)\nresources: (Q, QUANTITY=1)\n")
        + _cmd("k", "box_name: bx\n")
    )
    setup = [
        ev("STARTJOB", 0, job="h"),
        ev("ON_ICE", 0, job="c"),
        ev("STARTJOB", 1, job="bx"),
        ev("STATUS", 2, job="a", status="SUCCESS"),
        ev("OFF_ICE", 3, job="c"),
        ev("FORCE_STARTJOB", 4, job="c"),
        ev("STATUS", 5, job="a", status="INACTIVE"),
        ev("STATUS", 6, job="k", status="SUCCESS"),
    ]
    paired = _paired(jil, setup, semantics=resolve({"queued-recheck": "1"}))
    assert paired.store.job["c"].status == "QUE_WAIT"
    traced = len(paired.trace())
    seen = _spied(paired)
    paired.feed(ev("STATUS", 7, job="h", status="SUCCESS"))
    assert _moved(paired, traced)[-2:] == [("c", "QUE_WAIT->INACTIVE"), ("bx", "RUNNING->SUCCESS")]
    leave = _steps(seen, "after leave", "c")
    assert len(leave) == 1 and _sent(seen, leave[0]) == []


def test_a_release_inside_an_admit_only_scan_runs_a_full_pass_next() -> None:
    """DL-247: the box-stop scan only admits. It starts X, X's start fails
    its box, and job_terminator ends X, releasing its load: the release asks
    a full scan, which the running scan takes up as its next pass, and M,
    queued in the stopped box B, is cancelled there."""
    jil = (
        "insert_machine: up\ntype: a\nnode_name: up\nmax_load: 80\n\n"
        "insert_job: H\njob_type: c\ncommand: h\nmachine: up\njob_load: 30\npriority: 1\n\n"
        + _box("B")
        + "insert_job: M\njob_type: c\ncommand: m\nmachine: up\nbox_name: B\n"
        "job_load: 60\npriority: 5\n\n"
        + _box("Q", "box_failure: s(X) | v(GO) = 1\n")
        + "insert_job: X\njob_type: c\ncommand: x\nmachine: up\nbox_name: Q\n"
        "job_load: 30\npriority: 9\njob_terminator: 1\n"
    )
    setup = [
        ev("STARTJOB", 0, job="H"),
        ev("STARTJOB", 1, job="B"),
        ev("STARTJOB", 2, job="Q"),
        ev("SET_GLOBAL", 3, name="GO", value="1"),
    ]
    paired = _paired(jil, setup)
    traced = len(paired.trace())
    paired.feed(ev("KILLJOB", 4, job="B"))
    assert _moved(paired, traced)[-1] == ("M", "QUE_WAIT->INACTIVE")
    statuses = {job: paired.store.job[job].status for job in ("Q", "X", "M")}
    assert statuses == {"Q": "FAILURE", "X": "TERMINATED", "M": "INACTIVE"}


@pytest.mark.parametrize("mode", ["wait", "complete"])
def test_a_window_skip_that_ends_an_outer_run_decides_nothing_more_in_it(mode: str) -> None:
    """DL-246: the pass's skip of `mI` completes I, and O's box_success
    names I, so O ends too. O's own waiting member is decided no more, and
    under `box-start-all-members-out=complete` no box run of the pass is a
    completion moment again."""
    window = (
        'date_conditions: 1\ndays_of_week: all\nstart_times: "07:00"\nrun_window: "07:00-07:30"\n'
    )
    jil = (
        _box("O", "box_success: s(I)\n")
        + _box("I", "box_name: O\n")
        + _cmd("mI", "box_name: I\n" + window)
        + _cmd("mO", "box_name: O\n" + window)
    )
    paired = _paired(jil, [], semantics=resolve({"box-start-all-members-out": mode}))
    seen = _spied(paired)
    paired.feed(ev("STARTJOB", 1, job="O"))
    decided = [o for o in seen if o.event.name == "start" and o.depth > 0]
    assert [str(o.key) for o in decided if "job.404" in o.rows] == ["job:mI"]
    assert [job for job, _ in _moved(paired, 0) if job == "mO"] == []
    assert paired.store.job["O"].status == "SUCCESS"


# ---- the audiences (DL-346, DL-348): each gate, and a mutant that widens it


def _diverges(factory: type[Oracle], jil: str, setup: list[Event], last: Event) -> None:
    """The mutant leaves what the oracle leaves on `setup`, and parts from it
    on `last`: the trace, the events, the rows, the revisions or the
    timers it leaves differ (tests/oracle_inputs.py's renderings)."""
    found = observations(factory(lower_source(jil)), [*setup, last])
    expected = observations(Oracle(lower_source(jil)), [*setup, last])
    assert found[:-1] == expected[:-1]
    assert found[-1] != expected[-1]


_CARRIED = (
    _box("O", "condition: s(O)\nbox_success: s(o1)\n")
    + _cmd("o1", "box_name: O\n")
    + _box("S", "box_name: O\n")
    + _cmd("s1", "box_name: S\n")
    + _cmd("s2", "box_name: S\n")
)
_CARRIED_SETUP = [ev("FORCE_STARTJOB", 1, job="O"), ev("STATUS", 2, job="s1", status="SUCCESS")]
_CARRIED_LAST = ev("STATUS", 3, job="o1", status="SUCCESS")


def test_a_box_start_resets_no_job_of_a_live_subbox() -> None:
    """O's override ends it while its subbox S still runs s2; `s(O)` starts O
    again, and its reset leaves S's subtree alone: s1 keeps its SUCCESS."""
    paired = _paired(_CARRIED, _CARRIED_SETUP)
    seen = _spied(paired)
    paired.feed(_CARRIED_LAST)
    reset = [str(o.key)[4:] for o in seen if o.event.name == "box start"]
    assert reset == ["o1"]
    rows = paired.store.job
    assert (rows["S"].status, rows["s1"].status) == ("RUNNING", "SUCCESS")


def test_a_reset_that_reaches_a_live_subbox_s_jobs_diverges() -> None:
    """Mutant: the reset's audience is every job the box contains."""
    widened = _with(_reset_audience=lambda self, box: self._contained(box, skip_live=False))
    _diverges(widened, _CARRIED, _CARRIED_SETUP, _CARRIED_LAST)


_QUEUED = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    + _cmd("h", "resources: (R, QUANTITY=1, FREE=Y)\n")
    + _box("B", "box_success: n(q)\n")
    + _cmd("q", "box_name: B\nresources: (R, QUANTITY=1, FREE=Y)\n")
)


def test_a_queue_move_reads_no_override_of_its_box() -> None:
    """q queues in B's run: QUE_WAIT is no move the door reads overrides on,
    so `n(q)` is not read and B keeps running; the fold alone follows (job.455)."""
    paired = _paired(_QUEUED, [ev("STARTJOB", 1, job="h")])
    seen = _spied(paired)
    paired.feed(ev("FORCE_STARTJOB", 2, job="B"))
    assert _children(seen, "box rules", "q") == [("B", "member moves")]
    assert _door(seen, "B") == [("B", ("job.455",)), ("B", ("job.458",))]
    assert paired.store.job["B"].status == "RUNNING"


def test_a_door_that_reads_overrides_on_every_move_diverges() -> None:
    """Mutant: `member moves` on a QUE_WAIT move too."""
    widened = _with(_reads_overrides=staticmethod(lambda new, resolved: True))
    _diverges(widened, _QUEUED, [ev("STARTJOB", 1, job="h")], ev("FORCE_STARTJOB", 2, job="B"))


_ICED_WAITER = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    + _cmd("h", "resources: (R, QUANTITY=1, FREE=Y)\n")
    + _box("B", "box_success: s(k)\n")
    + _cmd("k", "box_name: B\n")
    + _cmd("q", "box_name: B\nresources: (R, QUANTITY=1, FREE=Y)\n")
)
_ICED_SETUP = [
    ev("STARTJOB", 1, job="h"),
    ev("FORCE_STARTJOB", 2, job="B"),
    ev("STATUS", 3, job="k", status="SUCCESS"),
    ev("STATUS", 4, job="k", status="FAILURE"),
]


def test_an_iced_waiter_s_move_recomputes_no_idle_box() -> None:
    """B ended SUCCESS by its override while q waits; k's later FAILURE meets
    q still waiting. The ice that takes q out of the queue is no terminal
    move, so B re-derives nothing (SEM-15)."""
    paired = _paired(_ICED_WAITER, _ICED_SETUP)
    seen = _spied(paired)
    paired.feed(ev("ON_ICE", 5, job="q"))
    assert _children(seen, "box rules", "q") == []
    assert paired.store.job["B"].status == "SUCCESS"


def test_a_recompute_sent_on_every_move_of_an_idle_box_s_member_diverges() -> None:
    """Mutant: `box recompute` on a member's non-terminal move."""
    widened = _with(_recomputes=staticmethod(lambda status, new: status not in ("STARTING",)))
    _diverges(widened, _ICED_WAITER, _ICED_SETUP, ev("ON_ICE", 5, job="q"))


def test_a_walk_up_that_reads_no_run_diverges() -> None:
    """Mutant: the walk up reaches a box above in any RUNNING run."""
    widened = _with(_in_run=lambda self, box, run: self._runtime(box).status == "RUNNING")
    _diverges(widened, _WALK, _WALK_SETUP, ev("STATUS", 3, job="M", status="SUCCESS"))


_RESTARTED_PARENT = _box("P", "condition: n(m)\n") + _cmd("m", "box_name: P\nstatus: ON_NOEXEC\n")


def test_an_operator_s_inactive_recomputes_no_parent_its_wakes_started_again() -> None:
    """m's INACTIVE wakes P (`n(m)`), whose new run bypasses m and folds
    SUCCESS, all inside the INACTIVE's cascade: P is idle again, in a later
    run, and the INACTIVE's own recompute does not reach it. The gate's run
    compare decides alone here, and a recompute of the new run would derive
    the same SUCCESS (job.191): this shape does not part a mutant of the
    gate, the next one does."""
    setup = [ev("FORCE_STARTJOB", 1, job="P")]
    paired = _paired(_RESTARTED_PARENT, setup)
    seen = _spied(paired)
    paired.feed(ev("STATUS", 2, job="m", status="INACTIVE"))
    assert _children(seen, "after inject", "m") == []
    rows = paired.store.job
    assert rows["P"].status == "SUCCESS" and rows["P"].run_number > 1
    widened = _with(_parent_recomputes=lambda self, box, status, run: True)
    paired = _paired(_RESTARTED_PARENT, setup, widened)
    seen = _spied(paired)
    paired.feed(ev("STATUS", 2, job="m", status="INACTIVE"))
    assert _children(seen, "after inject", "m") == [("P", "box recompute")]
    assert [o.rows for o in seen if o.event.name == "box recompute"] == [("job.191",)]


#: m's INACTIVE wakes W (`n(m)`), which bypasses and completes WB; W's
#: SUCCESS starts P, whose run bypasses m, folds SUCCESS and wakes ext, which
#: bypasses. P's run moved inside the INACTIVE's cascade, and P's box_failure
#: turned true after P's door read it.
_MOVED_OVERRIDE = (
    _box("P", "condition: s(W)\nbox_failure: s(ext)\n")
    + _cmd("m", "box_name: P\n")
    + _box("WB")
    + _cmd("W", "box_name: WB\ncondition: n(m)\nstatus: ON_NOEXEC\n")
    + _cmd("ext", "condition: s(P)\nstatus: ON_NOEXEC\n")
)
_MOVED_OVERRIDE_SETUP = [
    ev("ON_NOEXEC", 1, job="m"),
    ev("STATUS", 2, job="m", status="RUNNING"),
    ev("STARTJOB", 3, job="WB"),
]


def _no_run_compare(self: Oracle, box: str, status: str, run: int) -> bool:
    """Mutant: the parent's status gates kept, its run compare dropped."""
    settled = ("RUNNING", "STARTING", "TERMINATED")
    return status not in settled and self._runtime(box).status not in settled


def test_a_parent_recompute_that_reads_no_run_diverges() -> None:
    """Gate 5's mutant: without the run compare, m's INACTIVE recomputes P in
    the run its wakes began; box_failure s(ext) is true by then, so the chart
    writes P SUCCESS->FAILURE (job.447), where the oracle writes nothing."""
    last = ev("STATUS", 4, job="m", status="INACTIVE")
    paired = _paired(_MOVED_OVERRIDE, _MOVED_OVERRIDE_SETUP)
    paired.feed(last)
    assert paired.store.job["P"].status == "SUCCESS"
    _diverges(
        _with(_parent_recomputes=_no_run_compare), _MOVED_OVERRIDE, _MOVED_OVERRIDE_SETUP, last
    )


#: The box's kill reaches `a`, whose TERMINATED starts the box again; `b`,
#: carried RUNNING into the new run, has the job_terminator alone.
_MID = (
    _box("bx", "condition: t(a)\n")
    + _cmd("a", "box_name: bx\njob_terminator: 1\n")
    + _cmd("b", "box_name: bx\njob_terminator: 1\n")
)
#: The box's kill starts it again (`t(bx)`); `k` is carried RUNNING into the
#: new run.
_SELF_RESTART = _box("bx", "condition: t(bx)\n") + _cmd("k", "box_name: bx\njob_terminator: 1\n")


def _ends_read_late(self: Oracle, box: str, run: int | None) -> Any:
    """Mutant: the run the cascade compares is read at its first step, after
    the end's wakes, not before the write (Opus R1's shape, DL-348)."""
    run = self._runtime(box).run_number
    yield from OracleCascade._box_ends(self, box, run)


@pytest.mark.parametrize(
    ("mutant", "jil", "last"),
    [
        (
            _with(_still_the_run=lambda self, box, run: True),
            _MID,
            ev("KILLJOB", 2, job="bx"),
        ),
        (
            _with(_box_ends=_ends_read_late),
            _SELF_RESTART,
            ev("KILLJOB", 2, job="bx"),
        ),
    ],
    ids=["no per-member check", "the run read at the first step"],
)
def test_a_terminator_cascade_that_leaves_the_run_that_ended_diverges(
    mutant: type[Oracle], jil: str, last: Event
) -> None:
    """Mutants of DL-348's audience: the box's run not compared before each
    member, or read when the cascade begins rather than before the write."""
    _diverges(mutant, jil, [ev("FORCE_STARTJOB", 1, job="bx")], last)


#: An outer box `po` that starts again on its own end, and its subbox `pb`
#: with box_terminator, which starts on `s(pa)`, a job outside both; `pa`
#: runs again before `pb` ends, so `po`'s restart resets `pb` to INACTIVE and
#: does not start it. `pm`, with job_terminator, runs in `pb`.
_PARENT_RESET = (
    _cmd("pa")
    + _box("po", "condition: d(po)\n")
    + _box("pb", "box_name: po\nbox_terminator: 1\ncondition: s(pa)\n")
    + _cmd("pm", "box_name: pb\njob_terminator: 1\n")
)


def test_a_terminator_cascade_past_a_parent_s_reset_diverges() -> None:
    """A mutant of the reset check (DL-360): the box's run compared alone.
    KILLJOB ends `pb`, whose box_terminator ends `po`, whose restart resets
    `pb` without moving its run; the mutant then kills `pm`, which the oracle
    leaves running."""
    setup = [
        ev("STARTJOB", 0, job="pa"),
        ev("STATUS", 1, job="pa", status="SUCCESS"),
        ev("FORCE_STARTJOB", 2, job="po"),
        ev("STARTJOB", 3, job="pa"),
    ]
    run_only = _with(_still_the_run=lambda self, box, run: self._runtime(box).run_number == run)
    _diverges(run_only, _PARENT_RESET, setup, ev("KILLJOB", 4, job="pb"))
