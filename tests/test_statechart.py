"""Conformance of the statechart executor to docs/statechart-engine.md, one rule per test.

Each test is named after the section of the contract it holds the executor to.
The machines are synthetic. `test_a_mutant_executor_fails_the_suite` breaks one
rule of the executor at a time and runs the section tests again: each mutant
must fail at least one of them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41 import state_machine as sm
from dsl41.state_machine import (
    COMPLETION,
    Ancestry,
    INITIAL,
    OVERFLOWED,
    START,
    AmbiguousTransition,
    Branch,
    Choice,
    Context,
    DeferralOverflow,
    Effects,
    Event,
    Executor,
    Region,
    ReentrantDispatch,
    Row,
    State,
    Statechart,
    StatechartError,
    TimeEvent,
    UnhandledEvent,
    chart_warnings,
    well_formed,
)


def leaf(name: str, **kw: Any) -> State:
    return State(name, **kw)


def composite(name: str, *regions: Region, **kw: Any) -> State:
    return State(name, regions=regions, **kw)


def region(name: str, initial: str, *states: State) -> Region:
    return Region(name, initial, states)


def chart(name: str, root: State, rows: list[Row], **kw: Any) -> Statechart:
    kw.setdefault("clock", "logical")
    return Statechart(name, root, tuple(rows), **kw)


def logging(*names: str) -> dict[str, Callable[[Context], Effects | None]]:
    """Actions that append their own name to the instance's data, a list."""

    def make(name: str) -> Callable[[Context], Effects | None]:
        def action(ctx: Context) -> None:
            ctx.data.append(name)

        return action

    return {name: make(name) for name in names}


def kinds(outcomes: list[sm.Outcome]) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(o.event.name, o.kind, o.rows) for o in outcomes]


# ---- §3 selection

TASK_ROOT = composite(
    "task",
    region("status", "waiting", leaf("waiting", defer=frozenset({"start"})), leaf("running")),
    region("flags", "clear", leaf("clear"), leaf("held")),
)
TASK_ROWS = [
    Row("task.01", "waiting", "go", "running"),
    Row("task.02", "task", "start", kind="ignored"),
    Row("task.03", "clear", "hold", "held"),
    Row("task.04", "running", "start", kind="internal"),
    Row("task.05", "task", "go", kind="ignored"),
    Row("task.06", "task", "hold", kind="ignored"),
]


def _job(*extra: Row) -> Executor:
    executor = Executor()
    machine = chart("task", TASK_ROOT, [*TASK_ROWS, *extra], defer_bound=4, overflow="stop")
    assert well_formed(machine) == []
    executor.restore("j", machine, {"task", "waiting", "clear"})
    return executor


def test_s3_an_ancestor_ignore_row_yields_to_a_substate_deferral_in_another_region() -> None:
    executor = _job()
    assert kinds(executor.dispatch("j", Event("start"))) == [("start", "deferred", ())]
    assert executor.deferred("j") == (Event("start"),)


def test_s3_a_sibling_regions_own_row_still_consumes_the_event() -> None:
    executor = _job(Row("task.07", "clear", "start", kind="internal"))
    assert kinds(executor.dispatch("j", Event("start"))) == [("start", "fired", ("task.07",))]
    assert executor.deferred("j") == ()


def test_s3_a_shared_ancestor_row_reached_by_two_walks_is_one_candidate() -> None:
    executor = _job()
    executor.restore("k", executor._instances["j"].chart, {"task", "running", "held"})
    # both walks reach the root's ignore row for `hold`; it fires once
    assert kinds(executor.dispatch("k", Event("hold"))) == [("hold", "fired", ("task.06",))]


def test_s3_an_ignore_row_yields_to_another_regions_row() -> None:
    """The flags walk reaches the root's ignore row; the status walk's task.01 consumes the
    event, so the ignore row yields and writes no trace line."""
    executor = _job()
    outcome = executor.dispatch("j", Event("go"))[0]
    assert (outcome.kind, outcome.rows, outcome.ignored) == ("fired", ("task.01",), ())


def test_s3_ignore_rows_of_two_regions_both_fire_when_nothing_else_does() -> None:
    executor = _two_regions(
        [
            Row("duo.01", "l", "noise", kind="ignored"),
            Row("duo.02", "r", "noise", kind="ignored"),
        ]
    )
    outcome = executor.dispatch("d", Event("noise"))[0]
    assert outcome.ignored == ("duo.01", "duo.02")


def test_s3_a_refusal_beside_another_regions_candidate_raises() -> None:
    executor = _two_regions(
        [
            Row("duo.01", "l", "enter", kind="refusal", code="stale"),
            Row("duo.02", "r", "enter", kind="internal"),
        ]
    )
    machine = executor._instances["d"].chart
    assert chart_warnings(machine) == [
        "duo.01 and duo.02: a refusal and another region's row for enter;"
        " both enabled raise AmbiguousTransition"
    ]
    with pytest.raises(AmbiguousTransition, match="refusal duo.01 beside duo.02"):
        executor.dispatch("d", Event("enter"))


def test_s3_a_refusal_beside_an_ignore_row_stands() -> None:
    executor = _two_regions(
        [
            Row("duo.01", "l", "enter", kind="refusal", code="stale"),
            Row("duo.02", "r", "enter", kind="ignored"),
        ]
    )
    assert chart_warnings(executor._instances["d"].chart) == []
    outcome = executor.dispatch("d", Event("enter"))[0]
    assert (outcome.rows, outcome.refusals) == (("duo.01",), ("stale",))


def test_s3_a_shared_ancestors_refusal_beside_a_regions_row_is_warned() -> None:
    """A root refusal is reached by the right region's walk while the left one has a row."""
    executor = _two_regions(
        [
            Row("duo.01", "duo", "enter", kind="refusal", code="closed"),
            Row("duo.02", "l", "enter", kind="internal"),
        ]
    )
    assert len(chart_warnings(executor._instances["d"].chart)) == 1
    with pytest.raises(AmbiguousTransition, match="refusal duo.01 beside duo.02"):
        executor.dispatch("d", Event("enter"))


def test_s3_no_refusal_warning_on_one_regions_line() -> None:
    machine = chart(
        "line",
        composite("line", region("main", "a", composite("a", region("r", "b", leaf("b"))))),
        [
            Row("line.01", "line", "enter", kind="refusal", code="closed"),
            Row("line.02", "b", "enter", kind="internal", guard="open"),
            Row("line.03", "b", "enter", kind="refusal", code="shut", guard="shut"),
        ],
    )
    # one walk: the inner state decides; one state: its guards partition the cases
    assert chart_warnings(machine) == []


NEST = chart(
    "nest",
    composite(
        "nest",
        region(
            "main",
            "outer",
            composite(
                "outer",
                region("inner", "a", leaf("a"), leaf("b", defer=frozenset({"poke"}))),
                defer=frozenset({"ping"}),
            ),
        ),
    ),
    [
        Row("nest.01", "a", "ping", kind="internal"),
        Row("nest.02", "a", "move", "b"),
        Row("nest.03", "nest", "poke", kind="ignored"),
        Row("nest.04", "nest", "move", kind="ignored"),
    ],
    defer_bound=2,
    overflow="stop",
)


def _nest(state: str) -> Executor:
    executor = Executor()
    executor.restore("n", NEST, {"nest", "outer", state})
    return executor


def test_s3_inner_row_beats_outer_deferral() -> None:
    assert kinds(_nest("a").dispatch("n", Event("ping"))) == [("ping", "fired", ("nest.01",))]


def test_s3_inner_deferral_beats_outer_row() -> None:
    executor = _nest("b")
    assert kinds(executor.dispatch("n", Event("poke"))) == [("poke", "deferred", ())]
    assert kinds(_nest("a").dispatch("n", Event("poke"))) == [("poke", "fired", ("nest.03",))]


def test_s3_outer_deferral_applies_where_no_inner_row_exists() -> None:
    assert kinds(_nest("b").dispatch("n", Event("ping"))) == [("ping", "deferred", ())]


def _guarded(first: bool, second: bool, calls: list[str]) -> Executor:
    def guard(name: str, value: bool) -> Callable[[Context], bool]:
        def check(ctx: Context) -> bool:
            calls.append(name)
            return value

        return check

    machine = chart(
        "pick",
        composite("pick", region("main", "s", leaf("s"), leaf("t"), leaf("u"))),
        [
            Row("pick.01", "s", "go", "t", guard="first"),
            Row("pick.02", "s", "go", "u", guard="second"),
        ],
        guards={"first": guard("first", first), "second": guard("second", second)},
    )
    executor = Executor()
    executor.restore("p", machine, {"pick", "s"})
    return executor


def test_s3_two_enabled_rows_in_one_state_raise() -> None:
    with pytest.raises(AmbiguousTransition, match="pick.01, pick.02"):
        _guarded(True, True, []).dispatch("p", Event("go"))


def test_s3_every_guard_of_the_state_is_evaluated() -> None:
    calls: list[str] = []
    assert kinds(_guarded(True, False, calls).dispatch("p", Event("go"))) == [
        ("go", "fired", ("pick.01",))
    ]
    assert calls == ["first", "second"]


def _cross(deep: bool, third: bool = False) -> Executor:
    a1 = composite("a1", region("r", "a2", leaf("a2"))) if deep else leaf("a1")
    regions = [region("r1", "a1", a1), region("r2", "b1", leaf("b1"))]
    rows = [
        Row("cross.01", "a2" if deep else "a1", "x", "out"),
        Row("cross.02", "b1", "x", "out"),
    ]
    if third:
        regions.append(region("r3", "c1", leaf("c1")))
        rows.append(Row("cross.03", "c1", "x", "out"))
    machine = chart(
        "cross",
        composite("cross", region("main", "par", composite("par", *regions), leaf("out"))),
        [*rows, Row("cross.09", "cross", "x", kind="ignored")],
    )
    active = (
        {"cross", "par", "a1", "b1"} | ({"a2"} if deep else set()) | ({"c1"} if third else set())
    )
    executor = Executor()
    executor.restore("c", machine, active)
    return executor


def test_s3_equal_depth_crossing_exit_sets_raise() -> None:
    with pytest.raises(AmbiguousTransition, match="cross.01 and cross.02"):
        _cross(deep=False).dispatch("c", Event("x"))


def test_s3_the_deeper_source_wins_a_crossing_exit_set() -> None:
    executor = _cross(deep=True)
    assert kinds(executor.dispatch("c", Event("x"))) == [("x", "fired", ("cross.01",))]
    assert executor.active("c") == {"cross", "out"}


def test_s3_a_deeper_candidate_drops_both_of_an_equal_depth_pair_first() -> None:
    """From the deepest outward: cross.01 beats cross.02 and cross.03, so the equal-depth
    pair is never judged."""
    executor = _cross(deep=True, third=True)
    assert kinds(executor.dispatch("c", Event("x"))) == [("x", "fired", ("cross.01",))]


def test_s3_well_formed_warns_where_one_of_two_regions_rows_leaves_the_shared_ancestor() -> None:
    machine = _cross(deep=False)._instances["c"].chart
    assert chart_warnings(machine) == [
        "cross.01 and cross.02: rows for x in two regions of par, and cross.01 leaves par"
    ]


def test_s3_no_warning_for_two_regions_rows_that_stay_inside() -> None:
    machine = chart(
        "calm",
        composite(
            "calm",
            region("r1", "a", leaf("a"), leaf("a2")),
            region("r2", "b", leaf("b"), leaf("b2")),
        ),
        [
            Row("calm.01", "a", "x", "a2"),
            Row("calm.02", "b", "x", "b2"),
            Row("calm.03", "a", "y", kind="internal"),
            Row("calm.04", "a2", "x", kind="internal"),
        ],
    )
    assert chart_warnings(machine) == []


def test_s3_a_sibling_region_target_is_refused() -> None:
    machine = chart(
        "sib",
        composite("sib", region("r1", "a", leaf("a")), region("r2", "b", leaf("b"))),
        [Row("sib.01", "a", "x", "b"), Row("sib.02", "sib", "x", kind="ignored")],
    )
    assert "sib.01: target b is in a sibling region of a" in well_formed(machine)


def _leaving(order: str) -> Executor:
    """P holds two regions; b1's row leaves P on `e`, a1's internal row is on `e` too."""
    regions = [region("ra", "a1", leaf("a1")), region("rb", "b1", leaf("b1"))]
    if order == "b_first":
        regions.reverse()
    machine = chart(
        "leave",
        composite("leave", region("main", "p", composite("p", *regions), leaf("q"))),
        [
            Row("leave.01", "b1", "e", "q"),
            Row("leave.02", "a1", "e", kind="internal", action="inner"),
            Row("leave.03", "leave", "e", kind="ignored"),
        ],
        actions=logging("inner"),
    )
    executor = Executor()
    executor.restore("l", machine, {"leave", "p", "a1", "b1"}, data=[])
    return executor


@pytest.mark.parametrize("order", ["a_first", "b_first"])
def test_s3_a_candidate_whose_source_another_candidate_exits_is_dropped(order: str) -> None:
    executor = _leaving(order)
    assert kinds(executor.dispatch("l", Event("e"))) == [("e", "fired", ("leave.01",))]
    assert executor.data("l") == []
    assert executor.active("l") == {"leave", "q"}


def test_s3_well_formed_warns_where_a_row_leaves_an_internal_rows_shared_source() -> None:
    machine = chart(
        "deep",
        composite(
            "deep",
            region(
                "main",
                "p",
                composite("p", region("ra", "a", leaf("a")), region("rb", "b", leaf("b"))),
                leaf("out"),
            ),
        ),
        [
            Row("deep.01", "p", "e", kind="internal", action="inner"),
            Row("deep.02", "a", "e", "out"),
            Row("deep.03", "deep", "e", kind="ignored"),
        ],
        actions=logging("inner"),
    )
    assert chart_warnings(machine) == [
        "deep.01 and deep.02: deep.02 leaves p, so deep.01 is dropped when both are enabled"
    ]
    executor = Executor()
    executor.restore("d", machine, {"deep", "p", "a", "b"}, data=[])
    assert kinds(executor.dispatch("d", Event("e"))) == [("e", "fired", ("deep.02",))]
    assert executor.data("d") == []


def _two_regions(rows: list[Row], **kw: Any) -> Executor:
    machine = chart(
        "duo",
        composite("duo", region("left", "l", leaf("l")), region("right", "r", leaf("r"))),
        rows,
        **kw,
    )
    executor = Executor()
    executor.restore("d", machine, {"duo", "l", "r"}, data=[])
    return executor


def test_s3_the_firing_set_runs_in_region_declaration_order() -> None:
    executor = _two_regions(
        [
            Row("duo.01", "r", "both", kind="internal", action="right"),
            Row("duo.02", "l", "both", kind="internal", action="left"),
        ],
        actions=logging("left", "right"),
    )
    executor.dispatch("d", Event("both"))
    assert executor.data("d") == ["left", "right"]


def test_s3_the_firing_set_is_final_before_any_action_runs() -> None:
    def close(ctx: Context) -> None:
        ctx.data.append("closed")

    executor = _two_regions(
        [
            Row("duo.01", "l", "both", kind="internal", action="close"),
            Row("duo.02", "r", "both", kind="internal", guard="open", action="right"),
        ],
        actions={"close": close, **logging("right")},
        guards={"open": lambda ctx: "closed" not in ctx.data},
    )
    assert kinds(executor.dispatch("d", Event("both"))) == [("both", "fired", ("duo.01", "duo.02"))]
    assert executor.data("d") == ["closed", "right"]


def test_s3_a_region_with_neither_row_nor_deferral_takes_no_part() -> None:
    executor = _two_regions([Row("duo.01", "l", "only", kind="internal")])
    assert well_formed(executor._instances["d"].chart) == []
    assert kinds(executor.dispatch("d", Event("only"))) == [("only", "fired", ("duo.01",))]


# ---- §4 execution of one row

ORDER_ROOT = composite(
    "ord",
    region(
        "main",
        "a",
        composite("a", region("r", "a1", leaf("a1", entry="in_a1", exit="out_a1")), exit="out_a"),
        composite("b", region("r", "b1", leaf("b1", entry="in_b1")), entry="in_b"),
    ),
)
ORDER_ACTIONS = logging("in_a1", "out_a1", "out_a", "in_b", "in_b1", "act", "branch")


def _order(*rows: Row, guards: dict[str, Callable[[Context], bool]] | None = None) -> Executor:
    machine = chart(
        "ord",
        ORDER_ROOT,
        list(rows),
        actions=ORDER_ACTIONS,
        guards=guards or {},
        choices=(
            Choice(
                "which",
                (
                    Branch("ord.11", "b", guard="big", action="branch"),
                    Branch("ord.12", "a1"),
                ),
            ),
        ),
    )
    executor = Executor()
    executor.restore("o", machine, {"ord", "a", "a1"}, data=[])
    return executor


def test_s4_an_external_row_exits_inner_to_outer_acts_then_enters_outer_to_inner() -> None:
    executor = _order(Row("ord.01", "a1", "go", "b", action="act"))
    executor.dispatch("o", Event("go"))
    assert executor.data("o") == ["out_a1", "out_a", "act", "in_b", "in_b1"]
    assert executor.active("o") == {"ord", "b", "b1"}


def test_s4_an_internal_row_runs_its_action_only() -> None:
    executor = _order(Row("ord.01", "a1", "tick", kind="internal", action="act"))
    executor.dispatch("o", Event("tick"))
    assert executor.data("o") == ["act"]
    assert executor.active("o") == {"ord", "a", "a1"}


def test_s4_an_external_self_transition_leaves_and_reenters_its_state_only() -> None:
    executor = _order(Row("ord.01", "a1", "again", "a1", action="act"))
    executor.dispatch("o", Event("again"))
    assert executor.data("o") == ["out_a1", "act", "in_a1"]


def test_s4_a_row_from_a_composite_to_its_substate_leaves_the_composite() -> None:
    executor = _order(Row("ord.01", "a", "down", "a1", action="act"))
    executor.dispatch("o", Event("down"))
    assert executor.data("o") == ["out_a1", "out_a", "act", "in_a1"]


def test_s4_a_row_from_a_substate_to_its_composite_reenters_it() -> None:
    executor = _order(Row("ord.01", "a1", "up", "a", action="act"))
    executor.dispatch("o", Event("up"))
    assert executor.data("o") == ["out_a1", "out_a", "act", "in_a1"]


def _big(ctx: Context) -> bool:
    return bool(ctx.data and ctx.data[-1] == "act") and ctx.event.payload > 5


def test_s4_a_choice_is_resolved_after_the_action_and_before_entry() -> None:
    executor = _order(Row("ord.01", "a1", "pick", "which", action="act"), guards={"big": _big})
    outcome = executor.dispatch("o", Event("pick", 9))[0]
    assert outcome.branches == ("ord.11",)
    assert executor.data("o") == ["out_a1", "out_a", "act", "branch", "in_b", "in_b1"]


def test_s4_a_choice_takes_its_else_branch_when_no_guard_holds() -> None:
    executor = _order(Row("ord.01", "a1", "pick", "which", action="act"), guards={"big": _big})
    outcome = executor.dispatch("o", Event("pick", 1))[0]
    assert outcome.branches == ("ord.12",)
    assert executor.data("o") == ["out_a1", "out_a", "act", "in_a1"]
    assert executor.active("o") == {"ord", "a", "a1"}


def test_s4_a_choice_with_no_enabled_branch_raises() -> None:
    machine = chart(
        "nochoice",
        composite("nochoice", region("main", "s", leaf("s"), leaf("t"))),
        [Row("nochoice.01", "s", "go", "c")],
        choices=(Choice("c", (Branch("nochoice.02", "t", guard="no"),)),),
        guards={"no": lambda ctx: False},
    )
    assert "choice c has no else branch" in well_formed(machine)
    executor = Executor()
    executor.restore("x", machine, {"nochoice", "s"})
    with pytest.raises(UnhandledEvent, match="choice c has no enabled branch"):
        executor.dispatch("x", Event("go"))


FINALS = chart(
    "fin",
    composite(
        "fin",
        region(
            "main",
            "work",
            composite(
                "work",
                region("r1", "w1", leaf("w1"), leaf("w1done", final=True)),
                region("r2", "w2", leaf("w2"), leaf("w2done", final=True)),
            ),
            leaf("after"),
        ),
    ),
    [
        Row("fin.01", "w1", "d1", "w1done"),
        Row("fin.02", "w2", "d2", "w2done"),
        Row("fin.03", "work", COMPLETION, "after"),
        Row("fin.04", "fin", "d1", kind="ignored"),
        Row("fin.05", "fin", "d2", kind="ignored"),
    ],
)


def test_s4_a_composite_completes_when_every_region_is_final() -> None:
    executor = Executor()
    executor.start("f", FINALS)
    executor.run()
    # the other region's walk reaches the root's ignore row, which yields
    assert kinds(executor.dispatch("f", Event("d1"))) == [("d1", "fired", ("fin.01",))]
    assert kinds(executor.dispatch("f", Event("d2"))) == [
        ("d2", "fired", ("fin.02",)),
        (COMPLETION, "fired", ("fin.03",)),
    ]
    assert executor.active("f") == {"fin", "after"}


def _relay(transient_entry: str = "", **kw: Any) -> Executor:
    """`src` sends to `dst`; `dst` passes a transient state `t` whose completion moves on."""
    rows = [
        Row("relay.01", "idle", "go", "t", action="go"),
        Row("relay.02", "t", COMPLETION, "done", guard="ready", action="completed"),
        Row("relay.03", "idle", "ping", "t"),
        Row("relay.04", "t", "leave", "idle"),
        Row("relay.05", "relay", "note", kind="internal", action="note"),
        Row("relay.06", "t", "go", kind="ignored"),
    ]
    machine = chart(
        "relay",
        composite(
            "relay",
            region("main", "idle", leaf("idle"), leaf("t", entry=transient_entry), leaf("done")),
        ),
        rows,
        **kw,
    )
    executor = Executor()
    # one store both instances share, as a tier object's state is shared
    store: dict[str, Any] = {}
    executor.restore("src", machine, {"relay", "idle"}, data={"log": [], "store": store})
    executor.restore("dst", machine, {"relay", "idle"}, data={"log": [], "store": store})
    return executor


def _log(name: str) -> Callable[[Context], None]:
    def action(ctx: Context) -> None:
        ctx.data["log"].append((ctx.key, name, ctx.depth))

    return action


def test_s4_a_completion_is_discarded_after_its_state_was_left() -> None:
    def leave(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("leave")),))

    executor = _relay(
        "leave",
        actions={"leave": leave, "go": _log("go"), "completed": _log("completed")},
        guards={"ready": lambda ctx: True},
    )
    assert kinds(executor.dispatch("src", Event("go"))) == [
        ("go", "fired", ("relay.01",)),
        ("leave", "fired", ("relay.04",)),
        (COMPLETION, "discarded", ()),
    ]
    assert executor.active("src") == {"relay", "idle"}


def test_s4_a_composite_completion_is_discarded_after_its_state_was_left_and_reentered() -> None:
    def kick(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("kick")),))

    machine = chart(
        "again",
        composite(
            "again",
            region(
                "main",
                "c",
                composite("c", region("r1", "a", leaf("a"), leaf("af", final=True))),
                leaf("d"),
            ),
        ),
        [
            Row("again.01", "a", "go", "af", action="kick"),
            Row("again.02", "c", "kick", "c"),
            Row("again.03", "c", COMPLETION, "d"),
            Row("again.04", "again", "go", kind="ignored"),
            Row("again.05", "again", "kick", kind="ignored"),
        ],
        actions={"kick": kick},
    )
    assert well_formed(machine) == []
    executor = Executor()
    executor.restore("x", machine, {"again", "c", "a"})
    assert kinds(executor.dispatch("x", Event("go"))) == [
        ("go", "fired", ("again.01",)),
        ("kick", "fired", ("again.02",)),
        (COMPLETION, "discarded", ()),
    ]
    assert executor.active("x") == {"again", "c", "a"}


def test_s4_a_completion_with_no_enabled_row_raises() -> None:
    executor = _relay(actions={"go": _log("go")}, guards={"ready": lambda ctx: False})
    with pytest.raises(UnhandledEvent, match="t completed"):
        executor.dispatch("src", Event("go"))


def test_s4_completion_and_chained_items_precede_pool_events() -> None:
    def go(ctx: Context) -> Effects:
        return Effects(send=(("dst", Event("ping")),))

    executor = _relay(
        actions={"go": go, "completed": _log("completed"), "note": _log("note")},
        guards={"ready": lambda ctx: True},
    )
    executor.post("src", Event("go"))
    executor.post("dst", Event("note"))
    assert [(o.key, o.event.name, o.kind) for o in executor.run()] == [
        ("src", "go", "fired"),
        ("dst", "ping", "fired"),
        ("dst", COMPLETION, "fired"),
        ("src", COMPLETION, "fired"),
        ("dst", "note", "fired"),
    ]


# ---- §5 the step and the worklist


def test_s5_a_chained_event_to_another_instance_runs_its_completion_on_the_target() -> None:
    def go(ctx: Context) -> Effects:
        return Effects(send=(("dst", Event("ping")),))

    executor = _relay(
        actions={"go": go, "completed": _log("completed")}, guards={"ready": lambda ctx: True}
    )
    outcomes = executor.dispatch("src", Event("go"))
    assert [(o.key, o.event.name, o.depth) for o in outcomes] == [
        ("src", "go", 0),
        ("dst", "ping", 1),
        ("dst", COMPLETION, 2),
        ("src", COMPLETION, 1),
    ]
    assert executor.active("dst") == {"relay", "done"}
    assert executor.data("dst")["log"] == [("dst", "completed", 2)]


def test_s5_a_completion_is_queued_behind_the_items_its_microstep_reported() -> None:
    def go(ctx: Context) -> Effects:
        return Effects(send=(("dst", Event("note")),), then="check")

    executor = _relay(
        actions={"go": go, "check": _log("check"), "note": _log("note"), "completed": _log("c")},
        guards={"ready": lambda ctx: True},
    )
    outcomes = executor.dispatch("src", Event("go"))
    assert [(o.key, o.event.name, o.kind) for o in outcomes] == [
        ("src", "go", "fired"),
        ("dst", "note", "fired"),
        ("src", "go", "continued"),
        ("src", COMPLETION, "fired"),
    ]
    assert outcomes[2].action == "check"


def test_s5_a_continuation_reads_the_state_after_the_cascade() -> None:
    """The continuation reads the shared store, never the executor (§8)."""
    seen: list[Any] = []

    def go(ctx: Context) -> Effects:
        return Effects(send=(("dst", Event("ping")),), then="check")

    def completed(ctx: Context) -> None:
        ctx.data["store"][ctx.key] = ctx.event.payload  # a completion's payload is its state

    def check(ctx: Context) -> Effects:
        seen.append(dict(ctx.data["store"]))
        return Effects(intents=("checked",))

    executor = _relay(
        actions={"go": go, "check": check, "completed": completed},
        guards={"ready": lambda ctx: True},
    )
    outcomes = executor.dispatch("src", Event("go"))
    # dst went through t and its completion before the continuation ran
    assert seen == [{"dst": "t"}]
    assert [o.intents for o in outcomes if o.kind == "continued"] == [("checked",)]


def test_s5_a_continuation_waits_for_items_a_later_action_of_its_microstep_reported() -> None:
    def go(ctx: Context) -> Effects:
        return Effects(then="k")

    def ping(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("ping")),))

    def k(ctx: Context) -> None:
        ctx.data.append(("k sees", list(ctx.data)))

    machine = chart(
        "cont",
        composite("cont", region("main", "s", leaf("s"), leaf("t", entry="ping"))),
        [
            Row("cont.01", "s", "go", "t", action="go"),
            Row("cont.02", "t", "ping", kind="internal", action="pinged"),
            Row("cont.03", "cont", "go", kind="ignored"),
        ],
        actions={"go": go, "ping": ping, "k": k, **logging("pinged")},
    )
    executor = Executor()
    executor.restore("c", machine, {"cont", "s"}, data=[])
    outcomes = executor.dispatch("c", Event("go"))
    assert [(o.event.name, o.kind) for o in outcomes] == [
        ("go", "fired"),
        ("ping", "fired"),
        ("go", "continued"),
    ]
    assert executor.data("c") == ["pinged", ("k sees", ["pinged"])]


def test_s5_reported_items_run_depth_first_in_reported_order() -> None:
    def fan(ctx: Context) -> Effects | None:
        if ctx.event.payload == "root":
            return Effects(send=((ctx.key, Event("hop", "a")), (ctx.key, Event("hop", "b"))))
        ctx.data.append(ctx.event.payload)
        if ctx.event.payload == "a":
            return Effects(send=((ctx.key, Event("hop", "a1")),))
        return None

    machine = chart(
        "fan",
        composite("fan", region("main", "s", leaf("s"))),
        [Row("fan.01", "s", "hop", kind="internal", action="fan")],
        actions={"fan": fan},
    )
    executor = Executor()
    executor.restore("f", machine, {"fan", "s"}, data=[])
    executor.dispatch("f", Event("hop", "root"))
    assert executor.data("f") == ["a", "a1", "b"]


def _reentrant(call: str) -> Executor:
    def bad(ctx: Context) -> None:
        executor: Executor = ctx.data
        if call == "post":
            executor.post(ctx.key, Event("again"))
        elif call == "run":
            executor.run()
        else:
            executor.start("other", ctx_chart[0])

    ctx_chart: list[Statechart] = []
    machine = chart(
        "loop",
        composite("loop", region("main", "s", leaf("s"))),
        [
            Row("loop.01", "s", "bad", kind="internal", action="bad"),
            Row("loop.02", "s", "again", kind="internal"),
        ],
        actions={"bad": bad},
    )
    ctx_chart.append(machine)
    executor = Executor()
    executor.restore("l", machine, {"loop", "s"}, data=executor)
    return executor


@pytest.mark.parametrize("call", ["post", "run", "start"])
def test_s5_a_reentrant_call_from_an_action_raises(call: str) -> None:
    executor = _reentrant(call)
    with pytest.raises(ReentrantDispatch):
        executor.dispatch("l", Event("bad"))
    # the executor is idle again, and the chain works
    assert kinds(executor.dispatch("l", Event("again"))) == [("again", "fired", ("loop.02",))]


def _hooked() -> tuple[Executor, list[tuple[str, Any]]]:
    """`_relay` with each action and the hook logging into one list."""
    seen: list[tuple[str, Any]] = []

    def go(ctx: Context) -> Effects:
        seen.append(("action", "go"))
        return Effects(send=(("dst", Event("ping")),), intents=("pinged",))

    def completed(ctx: Context) -> None:
        seen.append(("action", "completed"))

    actions = {"go": go, "completed": completed, "note": lambda ctx: None}
    executor = _relay(actions=actions, guards={"ready": lambda c: True})
    return executor, seen


def test_s5_the_hook_sees_every_outcome_in_order_before_the_next_item_pops() -> None:
    """DL-350: the tier's hook is called with each outcome as its
    microstep ends, before the next item's action runs, and reads the
    configuration the microstep left."""
    executor, seen = _hooked()

    def after(outcome: sm.Outcome) -> None:
        seen.append(("hook", (outcome.key, outcome.event.name or "completion", outcome.intents)))
        seen.append(("active", sorted(executor.active(outcome.key))))

    outcomes = executor.dispatch("src", Event("go"), after)
    assert [(o.key, o.event.name or "completion") for o in outcomes] == [
        ("src", "go"),
        ("dst", "ping"),
        ("dst", "completion"),
        ("src", "completion"),
    ]
    assert seen == [
        ("action", "go"),
        ("hook", ("src", "go", ("pinged",))),
        ("active", ["relay", "t"]),
        ("hook", ("dst", "ping", ())),
        ("active", ["relay", "t"]),
        ("action", "completed"),
        ("hook", ("dst", "completion", ())),
        ("active", ["done", "relay"]),
        ("action", "completed"),
        ("hook", ("src", "completion", ())),
        ("active", ["done", "relay"]),
    ]


def test_s5_run_takes_the_hook_for_every_item_of_the_pool() -> None:
    executor, seen = _hooked()
    executor.post("src", Event("note"))
    executor.post("dst", Event("note"))
    keys: list[Any] = []
    executor.run(lambda outcome: keys.append(outcome.key))
    assert keys == ["src", "dst"]


@pytest.mark.parametrize("call", ["post", "dispatch", "resolve", "fork", "explain"])
def test_s5_a_call_into_the_executor_from_the_hook_raises(call: str) -> None:
    executor, _seen = _hooked()

    def after(outcome: sm.Outcome) -> None:
        if call == "post":
            executor.post("dst", Event("note"))
        elif call == "dispatch":
            executor.dispatch("dst", Event("note"))
        elif call == "resolve":
            executor.resolve("dst", Event("note"))
        elif call == "fork":
            executor.fork()
        else:
            executor.explain(StatechartError("x"), "dst", Event("note"))

    with pytest.raises(ReentrantDispatch):
        executor.dispatch("src", Event("go"), after)
    # the run ended and emptied the worklist: the executor is idle, the chain works
    assert kinds(executor.dispatch("dst", Event("note"))) == [("note", "fired", ("relay.05",))]


def test_s5_an_error_from_the_hook_carries_the_item_s_facts() -> None:
    executor, _seen = _hooked()

    def after(outcome: sm.Outcome) -> None:
        if outcome.event.name == "ping":
            raise StatechartError("planted in the hook")

    with pytest.raises(StatechartError, match="planted in the hook") as raised:
        executor.dispatch("src", Event("go"), after)
    facts = dict(raised.value.facts)
    assert facts["instance"] == "'dst'"
    assert facts["item"] == "the event ping"
    assert facts["ancestry"] == "'src' go"
    # the completion of `t` never ran: the worklist was emptied
    assert executor.active("dst") == {"relay", "t"}
    assert executor.run() == []


def test_s5_any_exception_from_the_hook_ends_the_run_and_empties_the_worklist() -> None:
    executor, seen = _hooked()

    def after(outcome: sm.Outcome) -> None:
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError):
        executor.dispatch("src", Event("go"), after)
    assert seen == [("action", "go")]
    assert executor.run() == []


def test_s5_a_fork_takes_its_own_hook_and_the_parent_s_sees_nothing_of_it() -> None:
    """A hook is a parameter of the call, not of the executor: a fork made
    between runs carries none, and each side's dispatch takes its own."""
    executor, _seen = _hooked()
    parent: list[Any] = []
    executor.dispatch("dst", Event("note"), lambda o: parent.append(o.key))
    twin = executor.fork()
    forked: list[Any] = []
    twin.dispatch("src", Event("go"), lambda o: forked.append((o.key, o.event.name)))
    assert parent == ["dst"]
    assert forked == [("src", "go"), ("dst", "ping"), ("dst", ""), ("src", "")]
    assert executor.active("src") == {"relay", "idle"}
    assert twin.active("dst") == {"relay", "done"}


def test_s5_an_exception_out_of_an_action_ends_the_run_and_empties_the_worklist() -> None:
    def boom(ctx: Context) -> Effects:
        if ctx.event.payload == "first":
            return Effects(send=((ctx.key, Event("tick", "second")),), then="after")
        raise RuntimeError("action failed")

    machine = chart(
        "boom",
        composite("boom", region("main", "s", leaf("s"))),
        [Row("boom.01", "s", "tick", kind="internal", action="boom")],
        actions={"boom": boom, "after": lambda ctx: None},
    )
    executor = Executor()
    executor.restore("b", machine, {"boom", "s"})
    executor.post("b", Event("tick", "first"))
    with pytest.raises(RuntimeError):
        executor.run()
    assert executor.run() == []


def test_s5_every_chained_item_carries_the_ancestry_of_its_microstep() -> None:
    seen: list[tuple[Any, ...]] = []

    def go(ctx: Context) -> Effects:
        seen.append(ctx.ancestry)
        return Effects(send=(("dst", Event("ping")),))

    def completed(ctx: Context) -> None:
        seen.append(ctx.ancestry)

    executor = _relay(actions={"go": go, "completed": completed}, guards={"ready": lambda c: True})
    executor.dispatch("src", Event("go"))
    assert seen == [
        (),
        (("src", "go"), ("dst", "ping")),
        (("src", "go"),),
    ]


def _frame_depth() -> int:
    depth = 0
    frame = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back  # type: ignore[assignment]
    return depth


HOPS = 6


def _hop_executor(depths: list[int]) -> Executor:
    def hop(ctx: Context) -> Effects | None:
        depths.append(_frame_depth())
        left = ctx.event.payload
        if left == 0:
            return None
        return Effects(send=(((ctx.key + 1) % HOPS, Event("hop", left - 1)),))

    machine = chart(
        "hop",
        composite("hop", region("main", "s", leaf("s"), leaf("t"))),
        [
            Row("hop.01", "s", "hop", "t", action="hop"),
            Row("hop.02", "t", COMPLETION, "s"),
            Row("hop.03", "t", "hop", kind="internal", action="hop"),
        ],
        actions={"hop": hop},
    )
    executor = Executor()
    for key in range(HOPS):
        executor.restore(key, machine, {"hop", "s"})
    return executor


@settings(max_examples=40, deadline=None)
@given(st.lists(st.tuples(st.integers(0, HOPS - 1), st.integers(0, 60)), max_size=8))
def test_s5_any_sequence_of_root_items_drains_without_recursion(
    roots: list[tuple[int, int]],
) -> None:
    depths: list[int] = []
    executor = _hop_executor(depths)
    for key, hops in roots:
        executor.post(key, Event("hop", hops))
    outcomes = executor.run()
    assert executor.run() == []
    assert sum(1 for o in outcomes if o.kind == "fired" and o.event.name == "hop") == sum(
        hops + 1 for _, hops in roots
    )
    # every action runs at one stack depth, however long the chain
    assert len(set(depths)) <= 1


# ---- §6 deferred events

QUEUE_ROWS = [
    Row("queue.01", "idle", "work", "busy"),
    Row("queue.02", "busy", "done", "idle"),
    Row("queue.03", "idle", "job", kind="internal", action="job"),
    Row("queue.04", "queue", "done", kind="ignored"),
    Row("queue.05", "busy", "work", kind="ignored"),
]


def _queue(*rows: Row, **kw: Any) -> Executor:
    kw.setdefault("defer_bound", 4)
    kw.setdefault("overflow", "stop")
    kw.setdefault("actions", {"job": _job_action})
    machine = chart(
        "queue",
        composite(
            "queue",
            region("main", "idle", leaf("idle"), leaf("busy", defer=frozenset({"job"}))),
        ),
        [*QUEUE_ROWS, *rows] if rows else QUEUE_ROWS,
        **kw,
    )
    executor = Executor()
    executor.restore("q", machine, {"queue", "busy"}, data=[])
    return executor


def _job_action(ctx: Context) -> Effects | None:
    ctx.data.append(ctx.event.payload)
    if ctx.event.payload == "chain":
        return Effects(send=((ctx.key, Event("job", "chained")),))
    return None


def test_s6_release_runs_in_order_when_the_macrostep_ends_and_ahead_of_the_pool() -> None:
    executor = _queue()
    executor.dispatch("q", Event("job", 1))
    executor.dispatch("q", Event("job", 2))
    assert [e.payload for e in executor.deferred("q")] == [1, 2]
    executor.post("q", Event("done"))
    executor.post("q", Event("job", 3))
    outcomes = executor.run()
    assert [(o.event.name, o.event.payload, o.released) for o in outcomes] == [
        ("done", None, False),
        ("job", 1, True),
        ("job", 2, True),
        ("job", 3, False),
    ]
    assert executor.data("q") == [1, 2, 3]


def test_s6_the_hook_sees_each_released_item_in_order_before_the_next_pops() -> None:
    """DL-350: a released deferred event runs through the same loop as
    any item, so the tier's hook gets its outcome, in release order, before
    the next item's action runs."""
    executor = _queue()
    executor.dispatch("q", Event("job", 1))
    executor.dispatch("q", Event("job", 2))
    executor.post("q", Event("done"))
    executor.post("q", Event("job", 3))
    seen: list[tuple[str, Any, bool, list[Any]]] = []

    def after(outcome: sm.Outcome) -> None:
        data = list(executor.data(outcome.key))
        seen.append((outcome.event.name, outcome.event.payload, outcome.released, data))

    executor.run(after)
    assert seen == [
        ("done", None, False, []),
        ("job", 1, True, [1]),
        ("job", 2, True, [1, 2]),
        ("job", 3, False, [1, 2, 3]),
    ]


def test_s6_each_released_item_is_its_own_macrostep() -> None:
    executor = _queue()
    executor.dispatch("q", Event("job", "chain"))
    executor.dispatch("q", Event("job", 2))
    executor.dispatch("q", Event("done"))
    # the chain of the first released item runs before the second is released
    assert executor.data("q") == ["chain", "chained", 2]


def _hold_queue() -> Executor:
    def back(ctx: Context) -> None:
        ctx.data.append(ctx.event.payload)

    rows = [
        Row("hold.01", "idle", "work", "busy"),
        Row("hold.02", "busy", "done", "idle"),
        Row("hold.03", "idle", "job", "busy", action="back"),
        Row("hold.04", "hold", "done", kind="ignored"),
        Row("hold.05", "busy", "work", kind="ignored"),
    ]
    machine = chart(
        "hold",
        composite(
            "hold",
            region("main", "idle", leaf("idle"), leaf("busy", defer=frozenset({"job"}))),
        ),
        rows,
        actions={"back": back},
        defer_bound=4,
        overflow="stop",
    )
    assert well_formed(machine) == []
    executor = Executor()
    executor.restore("h", machine, {"hold", "busy"}, data=[])
    return executor


def test_s6_a_released_item_deferred_again_keeps_the_head_of_the_queue() -> None:
    executor = _hold_queue()
    executor.dispatch("h", Event("job", 1))
    executor.dispatch("h", Event("job", 2))
    outcomes = executor.dispatch("h", Event("done"))
    # job 1 takes idle back to busy; job 2 is deferred again and waits at the head
    assert [(o.event.name, o.event.payload) for o in outcomes] == [("done", None), ("job", 1)]
    assert executor.deferred("h") == (Event("job", 2),)
    assert executor.run() == []
    assert [(o.event.payload, o.released) for o in executor.dispatch("h", Event("done"))] == [
        (None, False),
        (2, True),
    ]


def test_s6_a_held_head_in_one_instance_does_not_stop_release_in_another() -> None:
    executor = _hold_queue()
    executor.restore("other", executor._instances["h"].chart, {"hold", "busy"}, data=[])
    executor.dispatch("h", Event("job", 1))
    executor.dispatch("other", Event("job", "o"))
    outcomes = executor.dispatch("other", Event("done"))
    assert [(o.key, o.event.payload, o.released) for o in outcomes] == [
        ("other", None, False),
        ("other", "o", True),
    ]
    assert executor.deferred("h") == (Event("job", 1),)


def test_s6_a_deferral_on_a_shared_ancestor_is_one_deferral() -> None:
    machine = chart(
        "share",
        composite(
            "share",
            region("left", "l", leaf("l")),
            region("right", "r", leaf("r")),
            defer=frozenset({"x"}),
        ),
        [Row("share.01", "l", "y", kind="internal")],
        defer_bound=2,
        overflow="stop",
    )
    assert well_formed(machine) == []
    executor = Executor()
    executor.restore("s", machine, {"share", "l", "r"})
    assert kinds(executor.dispatch("s", Event("x"))) == [("x", "deferred", ())]
    assert executor._instances["s"].deferred[0].states == ("share",)


def test_s6_a_full_queue_under_stop_raises() -> None:
    executor = _queue(defer_bound=1)
    executor.dispatch("q", Event("job", 1))
    with pytest.raises(DeferralOverflow, match="queue is full"):
        executor.dispatch("q", Event("job", 2))


def _dropping(guard: bool) -> Executor:
    def record(ctx: Context) -> Effects:
        ctx.data.append(("dropped", ctx.event.payload))
        return Effects(intents=("dropped",))

    return _queue(
        Row("queue.06", "busy", OVERFLOWED, kind="internal", guard="room", action="record"),
        defer_bound=1,
        overflow="drop",
        actions={"job": _job_action, "record": record},
        guards={"room": lambda ctx: guard},
    )


def test_s6_a_full_queue_under_drop_runs_the_overflowed_row_in_the_same_microstep() -> None:
    executor = _dropping(True)
    executor.dispatch("q", Event("job", 1))
    outcomes = executor.dispatch("q", Event("job", 2))
    assert kinds(outcomes) == [("job", "overflowed", ("queue.06",))]
    assert outcomes[0].intents == ("dropped",)
    assert executor.data("q") == [("dropped", 2)]
    assert executor.deferred("q") == (Event("job", 1),)


def test_s6_a_drop_with_no_enabled_overflowed_row_raises() -> None:
    executor = _dropping(False)
    executor.dispatch("q", Event("job", 1))
    with pytest.raises(UnhandledEvent, match="overflowed"):
        executor.dispatch("q", Event("job", 2))


def test_s6_a_released_event_the_new_state_does_not_handle_raises() -> None:
    machine = chart(
        "gap",
        composite(
            "gap",
            region("main", "busy", leaf("busy", defer=frozenset({"job"})), leaf("idle")),
        ),
        [Row("gap.01", "busy", "done", "idle")],
        defer_bound=2,
        overflow="stop",
    )
    executor = Executor()
    executor.restore("g", machine, {"gap", "busy"})
    executor.dispatch("g", Event("job"))
    with pytest.raises(UnhandledEvent, match="no row and no deferral for job"):
        executor.dispatch("g", Event("done"))


# ---- §7 clocks and time events

LEASE_ROWS = [
    Row("lease.01", "held", "expire", "lost", action="note"),
    Row("lease.02", "held", "renew", kind="internal", action="renew"),
    Row("lease.03", "lease", "tick", kind="internal", action="note"),
    Row("lease.04", "lost", "expire", kind="ignored"),
    Row("lease.05", "lost", "renew", kind="ignored"),
    Row("lease.06", "lost", "tick", kind="ignored"),
]


def _lease(**kw: Any) -> Executor:
    def renew(ctx: Context) -> Effects:
        return Effects(rearm=(ctx.event.payload or "expire",))

    machine = chart(
        "lease",
        composite("lease", region("main", "held", leaf("held"), leaf("lost"))),
        LEASE_ROWS,
        time_events=(TimeEvent("expire", generation=True), TimeEvent("tick")),
        actions={"renew": renew, **logging("note")},
        clock="monotonic",
        **kw,
    )
    executor = Executor()
    executor.restore("l", machine, {"lease", "held"}, data=[])
    return executor


def test_s7_a_stale_generation_is_discarded_before_selection() -> None:
    executor = _lease()
    executor.dispatch("l", Event("renew"))
    assert executor.generation("l", "expire") == 1
    assert kinds(executor.dispatch("l", Event("expire", generation=0))) == [
        ("expire", "discarded", ())
    ]
    assert kinds(executor.dispatch("l", Event("expire", generation=1))) == [
        ("expire", "fired", ("lease.01",))
    ]


def test_s7_a_time_event_without_a_generation_is_handled_by_rows_only() -> None:
    executor = _lease()
    assert kinds(executor.dispatch("l", Event("tick", generation=7))) == [
        ("tick", "fired", ("lease.03",))
    ]


def test_s7_a_re_armable_time_event_needs_its_generation() -> None:
    with pytest.raises(StatechartError, match="needs its generation"):
        _lease().post("l", Event("expire"))


def test_s7_re_arming_an_event_without_a_generation_raises() -> None:
    with pytest.raises(StatechartError, match="tick is not a re-armable time event"):
        _lease().dispatch("l", Event("renew", "tick"))


def test_s7_a_deferred_time_event_gone_stale_is_released_and_discarded() -> None:
    def renew(ctx: Context) -> Effects:
        return Effects(rearm=("expire",))

    machine = chart(
        "timer",
        composite(
            "timer",
            region(
                "main",
                "busy",
                leaf("busy", defer=frozenset({"expire"})),
                leaf("idle"),
            ),
        ),
        [
            Row("timer.01", "busy", "done", "idle", action="renew"),
            Row("timer.02", "idle", "expire", kind="internal"),
            Row("timer.03", "timer", "done", kind="ignored"),
        ],
        time_events=(TimeEvent("expire", generation=True),),
        actions={"renew": renew},
        defer_bound=2,
        overflow="stop",
    )
    executor = Executor()
    executor.restore("t", machine, {"timer", "busy"})
    executor.dispatch("t", Event("expire", generation=0))
    outcomes = executor.dispatch("t", Event("done"))
    assert [(o.event.name, o.kind, o.released) for o in outcomes] == [
        ("done", "fired", False),
        ("expire", "discarded", True),
    ]


# ---- §8 chaining and continuations


def test_s8_a_retrigger_bound_is_a_row_guarded_on_ancestry_depth() -> None:
    def again(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("poke")), (ctx.key, Event("poke"))))

    machine = chart(
        "bound",
        composite("bound", region("main", "s", leaf("s"))),
        [
            Row("bound.01", "s", "poke", kind="internal", guard="shallow", action="again"),
            Row("bound.02", "s", "poke", kind="ignored", guard="deep"),
        ],
        guards={"shallow": lambda ctx: ctx.depth < 2, "deep": lambda ctx: ctx.depth >= 2},
        actions={"again": again},
    )
    executor = Executor()
    executor.restore("b", machine, {"bound", "s"})
    outcomes = executor.dispatch("b", Event("poke"))
    # siblings are not counted against each other: each level doubles, only chains deepen
    assert [(o.depth, o.rows) for o in outcomes] == [
        (0, ("bound.01",)),
        (1, ("bound.01",)),
        (2, ("bound.02",)),
        (2, ("bound.02",)),
        (1, ("bound.01",)),
        (2, ("bound.02",)),
        (2, ("bound.02",)),
    ]


def test_s8_a_chained_item_s_ancestry_is_a_shared_link_that_reads_as_its_tuple() -> None:
    """An item extends its parent's ancestry by one link, and the items one
    microstep chains share it; the link reads, compares and hashes as the
    tuple of its steps (DL-351)."""
    seen: list[Any] = []

    def go(ctx: Context) -> Effects:
        seen.append(ctx.ancestry)
        if ctx.depth < 2:
            return Effects(send=((ctx.key, Event("go")), (ctx.key, Event("go"))))
        return Effects()

    machine = chart(
        "links",
        composite("links", region("main", "s", leaf("s"))),
        [Row("links.01", "s", "go", kind="internal", action="go")],
        actions={"go": go},
    )
    executor = Executor()
    executor.restore("k", machine, {"links", "s"})
    executor.dispatch("k", Event("go"))
    root, first, deep, _, second = seen[:5]
    assert root == ()
    assert isinstance(first, Ancestry) and first is second
    assert first == (("k", "go"),) and (("k", "go"),) == first
    assert deep == (("k", "go"), ("k", "go")) and deep.parent is first and first.parent is None
    assert deep.step == ("k", "go") and len(deep) == 2
    assert deep[0] == ("k", "go") and deep[:1] == (("k", "go"),)
    assert hash(deep) == hash(deep.steps()) and repr(deep) == repr(deep.steps())
    assert deep != [("k", "go"), ("k", "go")]


def test_s8_a_guard_counts_a_step_of_its_ancestry_in_constant_time() -> None:
    """`Context.count` under the executor equals the count over the ancestry's
    links, on every item of a tree whose branches repeat steps, siblings
    included; a context kept past its item counts its own ancestry."""
    counted: list[tuple[int, int]] = []
    kept: list[Context] = []

    def deep(ctx: Context) -> bool:
        counted.append((ctx.count(("a", "ping")), sum(s == ("a", "ping") for s in ctx.ancestry)))
        if kept:
            counted.append((kept[0].count(("a", "ping")), kept[0].ancestry.count(("a", "ping"))))
        kept.append(ctx)
        return ctx.depth >= 4

    def ping(ctx: Context) -> Effects:
        other = "b" if ctx.key == "a" else "a"
        return Effects(send=((other, Event("ping")), (ctx.key, Event("ping"))))

    machine = chart(
        "pings",
        composite("pings", region("main", "s", leaf("s"))),
        [
            Row("pings.01", "s", "ping", kind="internal", guard="shallow", action="ping"),
            Row("pings.02", "s", "ping", kind="ignored", guard="deep"),
        ],
        guards={"shallow": lambda ctx: not deep(ctx), "deep": lambda ctx: ctx.depth >= 4},
        actions={"ping": ping},
    )
    executor = Executor()
    for key in ("a", "b"):
        executor.restore(key, machine, {"pings", "s"})
    executor.dispatch("a", Event("ping"))
    assert len(counted) > 40
    assert all(fast == slow for fast, slow in counted)
    assert max(fast for fast, _ in counted) == 4
    plain = Context("a", Event("ping"), frozenset(), None, (("a", "ping"), ("b", "ping")))
    assert plain.count(("a", "ping")) == 1


def test_s8_the_count_of_the_item_in_hand_reads_no_link() -> None:
    """Under the executor `Context.count` reads the path's counts: a guard's
    count on the item in hand never walks the ancestry's links."""
    counted: list[int] = []

    def again(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("poke")),))

    def shallow(ctx: Context) -> bool:
        counted.append(ctx.count(("k", "poke")))
        return counted[-1] < 5

    machine = chart(
        "walkless",
        composite("walkless", region("main", "s", leaf("s"))),
        [
            Row("walkless.01", "s", "poke", kind="internal", guard="shallow", action="again"),
            Row("walkless.02", "s", "poke", kind="ignored", guard="deep"),
        ],
        guards={"shallow": shallow, "deep": lambda ctx: ctx.count(("k", "poke")) >= 5},
        actions={"again": again},
    )
    executor = Executor()
    executor.restore("k", machine, {"walkless", "s"})

    def walked(self: Ancestry) -> Any:
        raise AssertionError("a count walked the links")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Ancestry, "__iter__", walked)
        patch.setattr(Ancestry, "steps", walked)
        executor.dispatch("k", Event("poke"))
    assert counted == [0, 1, 2, 3, 4, 5]


def test_s8_an_ancestry_built_on_a_tuple_keeps_its_steps() -> None:
    """A tier that extends its own tuple of steps gets their links first."""
    built = Ancestry((("a", "x"), ("b", "y")), ("c", "z"))
    assert built == (("a", "x"), ("b", "y"), ("c", "z"))
    assert len(built) == 3 and built.parent == (("a", "x"), ("b", "y"))


# ---- §10 completeness at run time


def test_s10_an_event_whose_guards_are_all_false_is_unhandled() -> None:
    with pytest.raises(UnhandledEvent, match="no row and no deferral for go"):
        _guarded(False, False, []).dispatch("p", Event("go"))


def test_s10_a_refusal_row_reports_its_code_and_consumes_the_event() -> None:
    machine = chart(
        "gate",
        composite("gate", region("main", "s", leaf("s"))),
        [Row("gate.01", "s", "enter", kind="refusal", code="stale-token")],
    )
    executor = Executor()
    executor.restore("g", machine, {"gate", "s"})
    outcome = executor.dispatch("g", Event("enter"))[0]
    assert (outcome.kind, outcome.rows, outcome.refusals) == (
        "fired",
        ("gate.01",),
        ("stale-token",),
    )


# ---- §11 instances and boundaries


def test_s11_a_call_into_another_executor_is_not_reentrance() -> None:
    other = _two_regions([Row("duo.01", "l", "only", kind="internal")])

    def call_other(ctx: Context) -> None:
        ctx.data.append(kinds(other.dispatch("d", Event("only"))))

    machine = chart(
        "outer",
        composite("outer", region("main", "s", leaf("s"))),
        [Row("outer.01", "s", "go", kind="internal", action="call")],
        actions={"call": call_other},
    )
    executor = Executor()
    executor.restore("o", machine, {"outer", "s"}, data=[])
    executor.dispatch("o", Event("go"))
    assert executor.data("o") == [[("only", "fired", ("duo.01",))]]


def _ending() -> tuple[Executor, Statechart]:
    machine = chart(
        "ending",
        composite(
            "ending",
            region("main", "s", leaf("s"), leaf("end", final=True)),
            defer=frozenset({"late"}),
        ),
        [Row("ending.01", "s", "stop", "end"), Row("ending.02", "ending", "stop", kind="ignored")],
        defer_bound=1,
        overflow="stop",
    )
    executor = Executor()
    executor.restore("e", machine, {"ending", "s"})
    return executor, machine


def test_s11_forget_removes_an_instance_in_a_final_configuration() -> None:
    executor, _ = _ending()
    with pytest.raises(StatechartError, match="not in a final configuration"):
        executor.forget("e")
    executor.dispatch("e", Event("stop"))
    executor.forget("e")
    with pytest.raises(StatechartError, match="no instance 'e'"):
        executor.active("e")


def test_s11_forget_refuses_an_instance_with_events_still_to_handle() -> None:
    executor, _ = _ending()
    executor.dispatch("e", Event("stop"))
    executor.post("e", Event("stop"))
    with pytest.raises(StatechartError, match="events still to handle"):
        executor.forget("e")
    executor.run()
    executor.dispatch("e", Event("late"))  # the root defers it, even in the final state
    with pytest.raises(StatechartError, match="events still to handle"):
        executor.forget("e")


def test_s11_forget_refuses_a_root_without_regions() -> None:
    executor = Executor()
    executor.restore("s", chart("solo", leaf("solo"), []), {"solo"})
    with pytest.raises(StatechartError, match="not in a final configuration"):
        executor.forget("s")


def test_s11_start_enters_the_initial_configuration_and_runs_entry_actions() -> None:
    seen: list[str] = []

    def enter(ctx: Context) -> None:
        seen.append(ctx.event.name)

    machine = chart(
        "boot",
        composite(
            "boot",
            region("main", "t", leaf("t", entry="enter"), leaf("ready")),
            entry="enter",
        ),
        [Row("boot.01", "t", COMPLETION, "ready")],
        actions={"enter": enter},
    )
    executor = Executor()
    executor.start("b", machine)
    assert [(o.event.name, o.kind) for o in executor.run()] == [
        (INITIAL, "started"),
        (COMPLETION, "fired"),
    ]
    assert seen == [START.name, START.name]
    assert executor.active("b") == {"boot", "ready"}


# ---- §4 the row plan: an optimisation, never a rule

PLAN_ACTIONS = logging("out_x1", "out_x2", "out_y1", "out_y2", "out_x", "in_z")


def _plan_chart(name: str = "plan", rows: list[Row] | None = None) -> Statechart:
    """A composite `x` of two regions beside a leaf `z`; one row leaves `x`, one a leaf."""
    root = composite(
        name,
        region(
            "main",
            "x",
            composite(
                "x",
                region("p", "x1", leaf("x1", exit="out_x1"), leaf("x2", exit="out_x2")),
                region("q", "y1", leaf("y1", exit="out_y1"), leaf("y2", exit="out_y2")),
                exit="out_x",
            ),
            leaf("z", entry="in_z"),
        ),
    )
    ids = [f"{name}.{n:02d}" for n in (1, 2, 3)]
    return chart(
        name,
        root,
        rows
        if rows is not None
        else [
            Row(ids[0], "x", "go", "z"),
            Row(ids[1], "z", "back", "x"),
            Row(ids[2], "x1", "tick", kind="internal"),
        ],
        actions=PLAN_ACTIONS,
    )


def _count_plans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    built: list[str] = []
    real = sm._build_plan

    def counting(ix: sm._Index, row: Row) -> sm._Plan:
        built.append(row.id)
        return real(ix, row)

    monkeypatch.setattr(sm, "_build_plan", counting)
    return built


def test_plan_is_built_once_per_chart_and_shared_by_executors_and_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = _plan_chart()
    built = _count_plans(monkeypatch)
    for _ in range(2):
        executor = Executor()
        for key in ("i", "j", "k"):
            executor.restore(key, machine, {"plan", "x", "x1", "y1"}, data=[])
            executor.dispatch(key, Event("tick"))
            executor.dispatch(key, Event("go"))
            executor.dispatch(key, Event("back"))
            assert executor.active(key) == {"plan", "x", "x1", "y1"}
    # `go` and `back` are the external rows, and the internal row needs no plan
    assert sorted(built) == ["plan.01", "plan.02"]


def test_s4_a_plan_follows_each_instances_own_configuration() -> None:
    machine = _plan_chart()
    executor = Executor()
    executor.restore("a", machine, {"plan", "x", "x1", "y1"}, data=[])
    executor.restore("b", machine, {"plan", "x", "x2", "y2"}, data=[])
    executor.dispatch("a", Event("go"))
    executor.dispatch("b", Event("go"))
    assert executor.data("a") == ["out_x1", "out_y1", "out_x", "in_z"]
    assert executor.data("b") == ["out_x2", "out_y2", "out_x", "in_z"]


def test_plan_two_charts_of_one_shape_keep_their_own_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _plan_chart()
    second = _plan_chart()
    assert first is not second and first.rows == second.rows
    # the very row objects are shared, so only the chart can keep the plans apart
    shared = _plan_chart("plan", list(first.rows))
    twin = _plan_chart("plan", list(first.rows))
    assert shared.rows[0] is twin.rows[0]
    built = _count_plans(monkeypatch)
    executor = Executor()
    for machine in (first, second, shared, twin):
        executor.restore(machine, machine, {"plan", "x", "x1", "y1"}, data=[])
        executor.dispatch(machine, Event("go"))
        executor.dispatch(machine, Event("back"))
    assert built == ["plan.01", "plan.02"] * 4
    plans = [sm._index(machine).plans for machine in (first, second, shared, twin)]
    assert len({id(each) for each in plans}) == 4


def test_plan_equals_the_live_derivation_for_its_row() -> None:
    """Each plan against what the executor derives from a live configuration, and
    against the states a run really enters, not against the helpers that built it."""
    machine = _plan_chart()
    ix = sm._index(machine)
    live = {"plan.01": {"plan", "x", "x1", "y1"}, "plan.02": {"plan", "z"}}
    entered = {"plan.01": ("z",), "plan.02": ("x", "x1", "y1")}
    for row in machine.rows:
        if row.kind != "external":
            continue
        plan = sm._plan(ix, row)
        assert sm._plan(ix, row) is plan
        assert plan.top == sm._active_child(ix, live[row.id], *plan.domain)
        assert plan.domain == ("plan", 0)
        assert sm._entry_of(ix, plan, row.target) == entered[row.id]
        assert plan.leaving == (None if row.id == "plan.01" else ("z",))


def _choice_cache_run() -> None:
    """One external row ends in a choice of two branches; each branch's entry
    is cached under its own target."""
    executor = _order(
        Row("ord.01", "a1", "go", "which", action="act"),
        Row("ord.02", "b", "back", "a1"),
        guards={"big": _big},
    )
    machine = executor._instances["o"].chart
    executor.dispatch("o", Event("go", 10))
    assert executor.active("o") == {"ord", "b", "b1"}
    executor.dispatch("o", Event("back"))
    executor.dispatch("o", Event("go", 1))
    assert executor.active("o") == {"ord", "a", "a1"}
    assert executor.data("o") == [
        *("out_a1", "out_a", "act", "branch", "in_b", "in_b1"),
        "in_a1",
        *("out_a1", "out_a", "act", "in_a1"),
    ]
    entries = sm._plan(sm._index(machine), machine.rows[0]).entries
    assert entries == {"b": ("b", "b1"), "a1": ("a", "a1")}


def test_plan_a_choice_row_caches_each_branchs_entry_under_its_own_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _choice_cache_run()
    real = sm._entry_of

    def ignoring_the_target(ix: sm._Index, plan: sm._Plan, target: str) -> tuple[str, ...]:
        # the first entry order built for the row serves every later target
        return next(iter(plan.entries.values()), None) or real(ix, plan, target)

    monkeypatch.setattr(sm, "_entry_of", ignoring_the_target)
    with pytest.raises(AssertionError):
        _choice_cache_run()


def test_plan_cache_disabled_the_section_tests_still_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _count_plans(monkeypatch)
    monkeypatch.setattr(sm, "_plan", lambda ix, row: sm._build_plan(ix, row))
    machine = _plan_chart()
    executor = Executor()
    executor.restore("i", machine, {"plan", "x", "x1", "y1"}, data=[])
    for _ in range(3):
        executor.dispatch("i", Event("go"))
        executor.dispatch("i", Event("back"))
    # nothing is kept, and every dispatch of `go` rebuilt its plan at least once
    # (each lookup rebuilds; a dispatch makes several)
    assert sm._index(machine).plans == {}
    assert built.count("plan.01") >= 3
    built.clear()
    for test in _section_tests():
        test()
    assert built


# ---- a fork: a copy of an idle executor that shares its records (DL-339)


def _gate_work(ctx: Context) -> Effects:
    return Effects(intents=(ctx.event.payload,))


def _gate_renew(ctx: Context) -> Effects:
    return Effects(rearm=("expire",))


def _gate_relay(ctx: Context) -> Effects:
    return Effects(send=((ctx.event.payload, Event("shut")),))


#: A gate that defers `job` while shut, drops a job past its bound, re-arms
#: `expire`, passes through a transient state on `go` and relays `shut` to
#: another instance: every part of a record a microstep writes.
GATE = chart(
    "gate",
    composite(
        "gate",
        region(
            "main",
            "open",
            leaf("open"),
            leaf("shut", defer=frozenset({"job"})),
            leaf("pass"),
        ),
    ),
    [
        Row("gate.01", "open", "shut", "shut"),
        Row("gate.02", "shut", "open", "open"),
        Row("gate.03", "open", "go", "pass"),
        Row("gate.04", "pass", COMPLETION, "open"),
        Row("gate.05", "gate", "job", kind="internal", action="work"),
        Row("gate.06", "gate", "renew", kind="internal", action="renew"),
        Row("gate.07", "gate", "expire", kind="internal"),
        Row("gate.08", "gate", "shut", kind="ignored"),
        Row("gate.09", "gate", "open", kind="ignored"),
        Row("gate.10", "gate", "go", kind="ignored"),
        Row("gate.11", "shut", OVERFLOWED, kind="internal"),
        Row("gate.12", "gate", "relay", kind="internal", action="relay"),
    ],
    actions={"work": _gate_work, "renew": _gate_renew, "relay": _gate_relay},
    time_events=(TimeEvent("expire", generation=True),),
    defer_bound=2,
    overflow="drop",
)
GATE_KEYS = ("a", "b")


def _gates() -> Executor:
    executor = Executor()
    for key in GATE_KEYS:
        executor.restore(key, GATE, {"gate", "open"})
    return executor


def _drive(executor: Executor, inputs: list[tuple[str, Event]]) -> list[sm.Outcome]:
    return [outcome for key, event in inputs for outcome in executor.dispatch(key, event)]


def _records(executor: Executor) -> dict[Any, Any]:
    return dict(executor._instances)


def test_fork_the_gate_is_well_formed() -> None:
    assert well_formed(GATE) == []


def test_fork_a_dispatch_on_the_fork_leaves_every_parent_record_the_same_object() -> None:
    parent = _gates()
    before = _records(parent)
    twin = parent.fork()
    twin.dispatch("a", Event("shut"))
    assert all(parent._instances[key] is record for key, record in before.items())
    assert parent.active("a") == {"gate", "open"}
    assert twin.active("a") == {"gate", "shut"}
    assert twin._instances["b"] is before["b"]


def test_fork_a_dispatch_on_the_parent_leaves_every_fork_record_the_same_object() -> None:
    parent = _gates()
    twin = parent.fork()
    before = _records(twin)
    parent.dispatch("a", Event("shut"))
    assert all(twin._instances[key] is record for key, record in before.items())
    assert twin.active("a") == {"gate", "open"}
    assert parent.active("a") == {"gate", "shut"}


def test_fork_both_sides_diverge_and_each_equals_a_fresh_executor() -> None:
    prefix = [("a", Event("renew")), ("b", Event("go")), ("a", Event("shut"))]
    on_parent = [("a", Event("job", 1)), ("b", Event("relay", "a")), ("a", Event("open"))]
    on_twin = [("b", Event("shut")), ("a", Event("open")), ("b", Event("job", 2))]
    parent = _gates()
    _drive(parent, prefix)
    twin = parent.fork()
    parent_outcomes, twin_outcomes = _drive(parent, on_parent), _drive(twin, on_twin)
    fresh_parent, fresh_twin = _gates(), _gates()
    _drive(fresh_parent, prefix)
    _drive(fresh_twin, prefix)
    assert parent_outcomes == _drive(fresh_parent, on_parent)
    assert twin_outcomes == _drive(fresh_twin, on_twin)
    assert _records(parent) == _records(fresh_parent)
    assert _records(twin) == _records(fresh_twin)
    assert _records(parent) != _records(twin)


def test_fork_deferred_events_and_generations_survive_and_release_on_one_side_only() -> None:
    parent = _gates()
    _drive(parent, [("a", Event("renew")), ("a", Event("shut")), ("a", Event("job", 1))])
    assert parent.deferred("a") == (Event("job", 1),)
    twin = parent.fork()
    assert twin.deferred("a") == (Event("job", 1),)
    assert twin.generation("a", "expire") == 1
    released = twin.dispatch("a", Event("open"))
    assert [(o.event, o.released, o.intents) for o in released] == [
        (Event("open"), False, ()),
        (Event("job", 1), True, (1,)),
    ]
    assert twin.deferred("a") == ()
    assert parent.deferred("a") == (Event("job", 1),)
    assert parent.active("a") == {"gate", "shut"}
    assert kinds(twin.dispatch("a", Event("expire", generation=0))) == [("expire", "discarded", ())]
    assert kinds(parent.dispatch("a", Event("expire", generation=1))) == [
        ("expire", "fired", ("gate.07",))
    ]
    # the twin's release left the parent's queue, and its release scan, as they were
    assert [(o.event, o.released, o.intents) for o in parent.dispatch("a", Event("open"))] == [
        (Event("open"), False, ()),
        (Event("job", 1), True, (1,)),
    ]
    assert parent.deferred("a") == ()


def _seen(name: str, ctx: Context) -> tuple[str, str, tuple[str, ...]]:
    return (name, ctx.event.name, tuple(sorted(ctx.active)))


def _leave(ctx: Context) -> Effects:
    """The exit action of `a`: re-arm, and send an event to its own instance."""
    return Effects(
        intents=(_seen("leave", ctx),), rearm=("tick",), send=((ctx.key, Event("ping")),)
    )


def _arrive(ctx: Context) -> Effects:
    """The entry action of `b`: re-arm, and request a continuation."""
    return Effects(intents=(_seen("arrive", ctx),), rearm=("tock",), then="after")


def _report(ctx: Context) -> Effects:
    """`ping`'s action and the continuation: report what they see."""
    return Effects(intents=(_seen("report", ctx),))


#: `go` leaves `a` through an exit action that re-arms `tick` and sends `ping`
#: to its own instance, and enters `b` through an entry action that re-arms
#: `tock` and requests the continuation `after`.
PROBE = chart(
    "probe",
    composite(
        "probe",
        region("main", "a", leaf("a", exit="leave"), leaf("b", entry="arrive")),
    ),
    [
        Row("probe.01", "a", "go", "b"),
        Row("probe.02", "probe", "ping", kind="internal", action="report"),
        Row("probe.03", "probe", "go", kind="ignored"),
        Row("probe.04", "probe", "tick", kind="ignored"),
        Row("probe.05", "probe", "tock", kind="ignored"),
    ],
    actions={"leave": _leave, "arrive": _arrive, "report": _report, "after": _report},
    time_events=(TimeEvent("tick", generation=True), TimeEvent("tock", generation=True)),
)


def test_fork_exit_and_entry_effects_on_their_own_instance_stay_on_the_fork() -> None:
    """The writes that follow an exit or an entry action re-read the record the
    action's own effects left: none is lost on the fork, and none reaches the
    parent."""
    assert well_formed(PROBE) == []
    parent = Executor()
    parent.restore("p", PROBE, {"probe", "a"})
    before = parent._instances["p"]
    twin = parent.fork()
    outcomes = twin.dispatch("p", Event("go"))
    assert [(o.event.name, o.kind, o.depth, o.action, o.intents) for o in outcomes] == [
        ("go", "fired", 0, "", (("leave", "go", ("a", "probe")), ("arrive", "go", ("b", "probe")))),
        ("ping", "fired", 1, "", (("report", "ping", ("b", "probe")),)),
        ("go", "continued", 1, "after", (("report", "go", ("b", "probe")),)),
    ]
    record = twin._instances["p"]
    assert (dict(record.generations), dict(record.entered)) == ({"tick": 1, "tock": 1}, {"b": 1})
    assert parent._instances["p"] is before
    assert parent.generation("p", "tick") == 0
    fresh = Executor()
    fresh.restore("p", PROBE, {"probe", "a"})
    assert fresh.dispatch("p", Event("go")) == outcomes
    assert _records(fresh) == _records(twin)
    assert parent.dispatch("p", Event("go")) == outcomes
    assert _records(parent) == _records(twin)


def test_fork_keeps_the_history_ring_and_the_addition_order() -> None:
    parent = _gates()
    parent.dispatch("a", Event("shut"))
    twin = parent.fork()
    assert twin._recent == parent._recent and twin._recent["gate"] is not parent._recent["gate"]
    twin.dispatch("a", Event("open"))
    assert len(twin._recent["gate"]) == 2 and len(parent._recent["gate"]) == 1
    twin.restore("c", GATE, {"gate", "open"})
    parent.restore("c", GATE, {"gate", "open"})
    assert twin._instances["c"].added == parent._instances["c"].added == 3


def test_fork_from_inside_an_action_raises_reentrant_dispatch() -> None:
    def fork_inside(ctx: Context) -> None:
        ctx.data[0].fork()

    machine = chart(
        "inner",
        composite("inner", region("main", "s", leaf("s"))),
        [Row("inner.01", "s", "go", kind="internal", action="fork")],
        actions={"fork": fork_inside},
    )
    executor = Executor()
    executor.restore("i", machine, {"inner", "s"}, data=[executor])
    with pytest.raises(ReentrantDispatch, match="a fork while a macrostep"):
        executor.dispatch("i", Event("go"))


def test_fork_with_a_root_item_in_the_pool_raises() -> None:
    executor = _gates()
    executor.post("a", Event("shut"))
    with pytest.raises(StatechartError, match="a fork with 1 root items still in the pool"):
        executor.fork()
    executor.run()
    executor.start("c", GATE)
    with pytest.raises(StatechartError, match="a fork with 1 root items still in the pool"):
        executor.fork()


def test_fork_of_200_instances_shares_every_record() -> None:
    parent = Executor()
    for n in range(200):
        parent.restore(n, GATE, {"gate", "open"})
    twin = parent.fork()
    assert twin._instances is not parent._instances
    assert len(twin._instances) == 200
    assert all(twin._instances[n] is parent._instances[n] for n in range(200))


def test_fork_gives_the_instances_it_names_their_new_data() -> None:
    parent = Executor()
    for key in GATE_KEYS:
        parent.restore(key, GATE, {"gate", "open"}, data=f"{key} of the parent")
    twin = parent.fork({"a": "a of the fork"})
    assert (twin.data("a"), parent.data("a")) == ("a of the fork", "a of the parent")
    assert twin._instances["b"] is parent._instances["b"]
    with pytest.raises(StatechartError, match="no instance 'x'"):
        parent.fork({"x": None})


GATE_EVENTS = st.sampled_from(
    [
        Event("shut"),
        Event("open"),
        Event("go"),
        Event("job", 1),
        Event("job", 2),
        Event("renew"),
        Event("expire", generation=0),
        Event("expire", generation=1),
        Event("relay", "a"),
        Event("relay", "b"),
    ]
)
GATE_INPUTS = st.lists(st.tuples(st.sampled_from(GATE_KEYS), GATE_EVENTS), max_size=8)


@settings(max_examples=60, deadline=None)
@given(GATE_INPUTS, GATE_INPUTS, GATE_INPUTS)
def test_fork_random_inputs_on_both_sides_equal_two_fresh_executors(
    prefix: list[tuple[str, Event]],
    on_parent: list[tuple[str, Event]],
    on_twin: list[tuple[str, Event]],
) -> None:
    parent = _gates()
    _drive(parent, prefix)
    twin = parent.fork()
    fresh_parent, fresh_twin = _gates(), _gates()
    _drive(fresh_parent, prefix)
    _drive(fresh_twin, prefix)
    assert _drive(parent, on_parent) == _drive(fresh_parent, on_parent)
    assert _drive(twin, on_twin) == _drive(fresh_twin, on_twin)
    assert _records(parent) == _records(fresh_parent)
    assert _records(twin) == _records(fresh_twin)


# ---- the mutant check

MUTANTS: dict[str, tuple[str, Any]] = {
    # a completion of an earlier occurrence of its state still fires
    "old_occurrence": ("_occurrence_gone", lambda inst, item: item.name not in inst.active),
    # a continuation runs ahead of the items its microstep reported
    "continuation_first": (
        "_queue_order",
        lambda step, completions: [*step.continuations, *step.reported, *completions],
    ),
    # a row whose source another row of the firing set exits still runs
    "exited_source_runs": ("_without_exited_sources", lambda ix, active, rows: rows),
    # an ignore row never yields and a refusal never raises
    "no_yield": ("_yielded", lambda chart, rows: rows),
    # an outer row of a shared ancestor wins over an inner deferral
    "deferral_loses": ("_below", lambda ix, state, ancestor: False),
    # candidates whose exit sets meet are all kept
    "no_conflicts": ("_without_conflicts", lambda chart, ix, active, rows: rows),
    # a completion is never queued
    "no_completion": ("_completions", lambda ix, active, step: []),
    # the first enabled row is taken without checking the others
    "first_row_wins": (
        "_enabled",
        lambda chart, ix, state, event, ctx: next(
            (r for r in ix.rows.get((state, event), ()) if sm._holds(chart, r.id, r.guard, ctx)),
            None,
        ),
    ),
}


def _section_tests() -> list[Callable[[], None]]:
    """Every zero-argument section test of this module, the property test aside."""
    return [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_s")
        and callable(value)
        and value.__code__.co_argcount == 0
        and not hasattr(value, "hypothesis")
    ]


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_a_mutant_executor_fails_the_suite(mutant: str, monkeypatch: pytest.MonkeyPatch) -> None:
    name, replacement = MUTANTS[mutant]
    monkeypatch.setattr(sm, name, replacement)
    failed = []
    for test in _section_tests():
        try:
            test()
        except (AssertionError, StatechartError, pytest.fail.Exception):
            failed.append(test.__name__)
    assert failed, f"mutant {mutant} passes every section test"


def test_a_stale_generation_mutant_fails_the_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Executor, "_stale", staticmethod(lambda inst, event: False))
    with pytest.raises(AssertionError):
        test_s7_a_stale_generation_is_discarded_before_selection()


def test_a_hook_called_after_the_run_fails_the_hook_order_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutant that calls the tier's hook once the run is over, as a tier
    reading the outcomes after `run` would: a chained item's action then runs
    before the write of the item that chained it."""
    original = Executor.run

    def late(self: Executor, after: Callable[[sm.Outcome], None] | None = None) -> list[sm.Outcome]:
        outcomes = original(self)
        for outcome in outcomes:
            if after is not None:
                after(outcome)
        return outcomes

    monkeypatch.setattr(Executor, "run", late)
    with pytest.raises(AssertionError):
        test_s5_the_hook_sees_every_outcome_in_order_before_the_next_item_pops()


# ---- a tier's own error about an instance (§5)


def _refusing() -> Statechart:
    """One state that refuses `go`."""
    root = composite("m", region("r", "a", leaf("a")))
    return chart("m", root, [Row("m.01", "a", "go", kind="refusal", code="no")])


def test_explain_gives_a_tier_s_error_the_facts_of_a_run_error() -> None:
    """The tier raises about an outcome after the run: the report names the
    machine, the instance, its states, the item, the candidates and the
    recent microsteps, as an error out of a run does."""
    executor = Executor()
    executor.restore("k", _refusing(), {"m", "a"})
    (outcome,) = executor.dispatch("k", Event("go"))
    error = executor.explain(StatechartError("refused", outcome.rows), "k", Event("go"))
    assert error.facts[:8] == [
        ("machine", "m"),
        ("instance", "'k'"),
        ("active", "a, m"),
        ("item", "the event go"),
        ("payload", "None"),
        ("ancestry", "none: a root item"),
        ("deferred", "empty"),
        ("candidates", "m.01"),
    ]
    assert str(error).splitlines()[-1] == "microstep: 'k' go fired rows m.01 depth 0"


def test_explain_keeps_an_error_s_own_facts_and_refuses_an_unknown_instance() -> None:
    executor = Executor()
    executor.restore("k", _refusing(), {"m", "a"})
    error = StatechartError("refused")
    error.facts = [("WAL input", "1: go")]
    assert executor.explain(error, "k", Event("go")).facts == [("WAL input", "1: go")]
    with pytest.raises(StatechartError, match="no instance 'x'"):
        executor.explain(StatechartError("refused"), "x", Event("go"))
