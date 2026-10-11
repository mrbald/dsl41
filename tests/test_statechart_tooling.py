"""The statechart executor's tooling: well-formedness, coverage hits, the generated
completeness test, and the structured error report (docs/statechart-engine.md §5,
§9, §10, §13)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import statechart_completeness as sc
from dsl41 import state_machine as sm
from dsl41.state_machine import (
    COMPLETION,
    HITS_ENV,
    OVERFLOWED,
    STATECHART_LOG_PREFIX,
    AmbiguousTransition,
    Branch,
    Choice,
    Context,
    Effects,
    Event,
    Exclusion,
    Executor,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
    TimeEvent,
    UnhandledEvent,
    alphabet,
    chart_problems,
    chart_warnings,
    coverage_items,
    well_formed,
)


def _ok(ctx: Context) -> bool:
    return bool(ctx.event.payload)


def _act(ctx: Context) -> None:
    return None


BASE_ROOT = State(
    "base",
    regions=(
        Region(
            "main",
            "idle",
            (
                State("idle", entry="act"),
                State("busy", defer=frozenset({"poke"}), wait=frozenset({"finish"})),
                State("done", final=True),
            ),
        ),
    ),
)
BASE_ROWS = (
    Row("base.01", "idle", "go", "busy", guard="ok", action="act"),
    Row("base.02", "busy", "finish", "done"),
    Row("base.03", "base", "poke", kind="ignored"),
    Row("base.04", "base", "go", kind="ignored"),
    Row("base.05", "base", "finish", kind="ignored"),
    Row("base.06", "busy", OVERFLOWED, kind="internal"),
    Row("base.07", "idle", "pick", "which"),
    Row("base.08", "base", "pick", kind="ignored"),
    Row("base.09", "base", "tick", kind="refusal", code="late"),
)
BASE = Statechart(
    "base",
    BASE_ROOT,
    BASE_ROWS,
    choices=(Choice("which", (Branch("base.11", "busy", guard="ok"), Branch("base.12", "done"))),),
    guards={"ok": _ok},
    actions={"act": _act},
    time_events=(TimeEvent("tick"),),
    clock="logical",
    defer_bound=2,
    overflow="drop",
)


def _with_state(state: State) -> State:
    main = BASE_ROOT.regions[0]
    return replace(BASE_ROOT, regions=(replace(main, states=(*main.states, state)),))


def _row(index: int, **change: Any) -> tuple[Row, ...]:
    rows = list(BASE_ROWS)
    rows[index] = replace(rows[index], **change)
    return tuple(rows)


def _branches(*branches: Branch) -> tuple[Choice, ...]:
    return (Choice("which", branches),)


def test_the_base_chart_is_well_formed_and_well_formed_dispatches_on_it() -> None:
    assert chart_problems(BASE) == []
    assert well_formed(BASE) == []
    assert chart_warnings(BASE) == []
    assert alphabet(BASE) == ["finish", "go", "pick", "poke", "tick"]


PROBLEMS: list[tuple[dict[str, Any], str]] = [
    ({"name": "Bad"}, "machine name 'Bad' does not match"),
    ({"rows": _row(0, id="base.1")}, "id 'base.1' is not base.<two or three digits>"),
    ({"rows": (*BASE_ROWS, BASE_ROWS[0])}, "duplicate id base.01"),
    ({"root": _with_state(State("idle"))}, "duplicate state or choice name idle"),
    ({"root": replace(BASE_ROOT, final=True)}, "the root state base is final"),
    ({"clock": ""}, "the chart declares no clock"),
    (
        {"root": replace(BASE_ROOT, regions=(replace(BASE_ROOT.regions[0], initial="nope"),))},
        "region base/main: initial 'nope' is not one of its states",
    ),
    ({"root": _with_state(State("end", final=True, wait=frozenset({"x"})))}, "final state end has"),
    ({"root": _with_state(State("odd", exit="gone"))}, "state odd: action gone is not in"),
    ({"rows": _row(0, guard="nope")}, "base.01: guard nope is not in the registry"),
    ({"rows": _row(0, action="nope")}, "base.01: action nope is not in the registry"),
    ({"rows": _row(1, source="ghost")}, "base.02: source ghost is not a declared state"),
    ({"rows": (*BASE_ROWS, Row("base.20", "done", "go", "idle"))}, "final state done has a row"),
    ({"rows": _row(1, target="ghost")}, "target 'ghost' is not a declared state or choice"),
    ({"rows": _row(2, target="idle")}, "base.03: an ignored row has no target"),
    ({"rows": _row(2, action="act")}, "base.03: an ignored row runs no action"),
    ({"rows": _row(8, code="")}, "base.09: a refusal row, and only a refusal row, has a code"),
    ({"rows": _row(2, code="x")}, "base.03: a refusal row, and only a refusal row, has a code"),
    (
        {"rows": (*BASE_ROWS, Row("base.20", "idle", COMPLETION, kind="internal"))},
        "base.20: a completion row is external",
    ),
    ({"rows": _row(5, kind="external", target="idle")}, "base.06: an overflowed row is internal"),
    ({"rows": _row(1, false_unreachable="why")}, "base.02: false_unreachable without a guard"),
    ({"choices": _branches(Branch("base.11", "busy", guard="ok"))}, "which has no else branch"),
    (
        {"choices": _branches(Branch("base.12", "done"), Branch("base.11", "busy", guard="ok"))},
        "the else branch is not the only last one",
    ),
    (
        {"choices": _branches(Branch("base.11", "busy", guard="ok"), Branch("base.12", "ghost"))},
        "base.12: target 'ghost' is not a declared state",
    ),
    (
        {"choices": _branches(Branch("base.11", "busy", guard="x"), Branch("base.12", "done"))},
        "base.11: guard x is not in the registry",
    ),
    (
        {
            "choices": _branches(
                Branch("base.11", "busy", guard="ok"),
                Branch("base.12", "done", false_unreachable="w"),
            )
        },
        "base.12: false_unreachable without a guard",
    ),
    (
        {"choices": (*BASE.choices, Choice("lonely", (Branch("base.13", "idle"),)))},
        "choice lonely is the target of no row",
    ),
    ({"defer_bound": 0}, "a state defers, so the chart declares defer_bound >= 1"),
    ({"overflow": ""}, "a state defers, so the chart declares defer_bound >= 1"),
    (
        {"rows": tuple(r for r in BASE_ROWS if r.id != "base.06")},
        "busy defers under drop and declares no overflowed row",
    ),
    (
        {"rows": (*BASE_ROWS, Row("base.20", "idle", OVERFLOWED, kind="internal"))},
        "base.20: an overflowed row on idle, which defers nothing",
    ),
    (
        {"root": _with_state(State("lag", wait=frozenset({"tick"})))},
        "wait state lag has no external row on tick and no stays",
    ),
    ({"root": _with_state(State("orphan"))}, "state orphan is not reachable"),
    (
        {"rows": tuple(r for r in BASE_ROWS if r.id != "base.05")},
        "event finish falls through in configuration {idle}",
    ),
    ({"exclusions": (Exclusion("ghost", "go", "why"),)}, "needs a declared state and a reason"),
    ({"exclusions": (Exclusion("idle", "go", ""),)}, "needs a declared state and a reason"),
]


@pytest.mark.parametrize(("change", "problem"), PROBLEMS, ids=[p for _, p in PROBLEMS])
def test_well_formed_reports_each_problem(change: dict[str, Any], problem: str) -> None:
    problems = chart_problems(replace(BASE, **change))
    assert any(problem in line for line in problems), problems


def test_a_wait_state_with_a_reason_to_stay_is_well_formed() -> None:
    chart = replace(BASE, root=_with_state(State("lag", wait=frozenset({"tick"}), stays="why")))
    chart = replace(chart, rows=(*BASE_ROWS, Row("base.20", "idle", "lag_in", "lag")))
    chart = replace(chart, rows=(*chart.rows, Row("base.21", "base", "lag_in", kind="ignored")))
    assert chart_problems(chart) == []


def test_completeness_is_judged_over_every_region_together() -> None:
    def chart(*rows: Row) -> Statechart:
        root = State(
            "duo",
            regions=(
                Region("left", "l", (State("l"), State("l2"))),
                Region("right", "r", (State("r"),)),
            ),
        )
        return Statechart("duo", root, rows, clock="logical")

    # in l2 no state handles x, and nothing in the right region does either
    assert "event x falls through in configuration {l2, r}" in chart_problems(
        chart(Row("duo.01", "l", "x", "l2"))
    )
    # a row in the right region covers every configuration of the left one
    assert (
        chart_problems(
            chart(Row("duo.01", "l", "x", "l2"), Row("duo.02", "r", "x", kind="internal"))
        )
        == []
    )


def test_the_warning_names_the_second_row_when_only_it_leaves() -> None:
    root = State(
        "w",
        regions=(
            Region(
                "main",
                "par",
                (
                    State(
                        "par",
                        regions=(
                            Region("r1", "a", (State("a"),)),
                            Region("r2", "b", (State("b"),)),
                        ),
                    ),
                    State("out"),
                ),
            ),
        ),
    )
    chart = Statechart(
        "w",
        root,
        (
            Row("w.01", "a", "x", kind="internal"),
            Row("w.02", "b", "x", "out"),
            Row("w.03", "w", "x", kind="ignored"),
        ),
        clock="logical",
    )
    assert chart_warnings(chart) == [
        "w.01 and w.02: rows for x in two regions of par, and w.02 leaves par"
    ]


def test_the_initial_entry_name_is_reserved() -> None:
    chart = replace(BASE, rows=(*BASE_ROWS, Row("base.20", "base", sm.INITIAL, kind="ignored")))
    assert "the event name <initial> is reserved for the initial entry" in chart_problems(chart)
    executor = Executor()
    executor.restore("b", BASE, {"base", "idle"})
    with pytest.raises(StatechartError, match="is not an event to post"):
        executor.post("b", Event(sm.INITIAL))


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_chart_of_the_by_path_module_is_read_as_a_statechart() -> None:
    """The supervisor loads state_machine.py by path: its charts are another module
    object's classes. The checks, the gate and the renderer read them alike."""
    root = Path(__file__).resolve().parent.parent
    twin = _load("state_machine_twin", Path(sm.__file__))
    tc = _load("transition_coverage_twin_test", root / "scripts" / "transition_coverage.py")
    rsm = _load("render_state_machines_twin_test", root / "scripts" / "render_state_machines.py")
    chart = twin.Statechart(
        "lease",
        twin.State("lease", regions=(twin.Region("main", "held", (twin.State("held"),)),)),
        (twin.Row("lease.01", "held", "renew", kind="internal"),),
        clock="monotonic",
    )
    assert not isinstance(chart, Statechart) and sm.is_statechart(chart)
    assert well_formed(chart) == []
    assert tc.report([chart], [])[0][0] == "lease: 0/1"
    assert "\n## lease\n\nClock: monotonic." in rsm.render([chart])


# ---- §13 coverage items and hits


def test_coverage_items_list_every_class() -> None:
    required, forbidden = coverage_items(
        replace(BASE, rows=_row(0, false_unreachable="the badge is always shown"))
    )
    assert ("row", "base.01", "") in required and ("branch", "base.12", "") in required
    assert ("guard", "base.01", "true") in required
    assert ("guard", "base.01", "false") not in required
    assert forbidden == [("guard", "base.01", "false")]
    assert ("guard", "base.11", "false") in required
    assert [i for i in required if i[0] in ("defer", "release")] == [
        ("defer", "busy", "poke"),
        ("release", "busy", "poke"),
    ]


def _hits(directory: Path) -> set[tuple[str, str, str, str]]:
    found = set()
    for path in directory.glob("hits-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            found.add((record["machine"], record["kind"], record["id"], record["detail"]))
    return found


def test_hits_are_recorded_for_every_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    executor = Executor()
    executor.restore("b", BASE, {"base", "idle"})
    executor.dispatch("b", Event("pick", False))  # branch else, guard false
    executor.restore("c", BASE, {"base", "idle"})
    executor.dispatch("c", Event("go", True))
    executor.dispatch("c", Event("poke"))
    executor.dispatch("c", Event("finish"))
    hits = _hits(tmp_path)
    assert ("base", "branch", "base.12", "") in hits
    assert ("base", "guard", "base.11", "false") in hits
    assert ("base", "guard", "base.01", "true") in hits
    assert ("base", "row", "base.07", "") in hits
    assert ("base", "defer", "busy", "poke") in hits
    assert ("base", "release", "busy", "poke") in hits


def test_a_hit_is_written_once_and_a_failed_write_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    results = iter([False, True])
    real = sm._append
    monkeypatch.setattr(sm, "_append", lambda path, record: next(results) and real(path, record))
    sm._hit(BASE, "row", "base.99")
    assert _hits(tmp_path) == set()
    sm._hit(BASE, "row", "base.99")
    sm._hit(BASE, "row", "base.99")  # already written: no third call
    assert _hits(tmp_path) == {("base", "row", "base.99", "")}


def test_nothing_is_recorded_without_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HITS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    sm._hit(BASE, "row", "base.98")
    assert os.listdir(tmp_path) == []


# ---- §13 the generated completeness test


def _badge(ctx: Context) -> bool:
    return bool(ctx.event.payload == "badge")


def _no_badge(ctx: Context) -> bool:
    return bool(ctx.event.payload != "badge")


DOOR = Statechart(
    "door",
    State(
        "door",
        regions=(
            Region("main", "closed", (State("closed"), State("opened", defer=frozenset({"lock"})))),
        ),
    ),
    (
        Row("door.01", "closed", "open", "opened", guard="badge"),
        Row("door.02", "closed", "open", kind="refusal", guard="no_badge", code="locked"),
        Row("door.03", "opened", "close", "closed"),
        Row("door.04", "opened", "auto_close", "closed"),
        Row("door.05", "closed", "lock", kind="internal"),
        Row("door.06", "door", "close", kind="ignored"),
        Row("door.07", "door", "auto_close", kind="ignored"),
        Row("door.08", "door", "open", kind="ignored"),
    ),
    guards={"badge": _badge, "no_badge": _no_badge},
    time_events=(TimeEvent("auto_close", generation=True),),
    clock="logical",
    defer_bound=2,
    overflow="stop",
    exclusions=(Exclusion("opened", "open", "no badge reader inside the room", "none"),),
)


def _witness(chart: Statechart, *active: str) -> sc.Builder:
    def build() -> tuple[Executor, str]:
        executor = Executor()
        executor.restore("w", chart, {chart.root.name, *active})
        return executor, "w"

    return build


def _door(chart: Statechart = DOOR, *witnesses: str) -> sc.Abstraction:
    names = witnesses or ("closed", "opened")
    return sc.Abstraction(
        witnesses={name: _witness(chart, name) for name in names},
        partitions={"open": {"badge": "badge", "none": None}},
    )


#: Written by hand from the declaration, so the generator is not checked against itself.
DOOR_TABLE = {
    ("closed", "auto_close", "any"): "ignored door.07",
    ("closed", "auto_close", "any/stale"): "discarded",
    ("closed", "close", "any"): "ignored door.06",
    ("closed", "lock", "any"): "door.05",
    ("closed", "open", "badge"): "door.01",
    ("closed", "open", "none"): "refused locked",
    ("opened", "auto_close", "any"): "door.04",
    ("opened", "auto_close", "any/stale"): "discarded",
    ("opened", "close", "any"): "door.03",
    ("opened", "lock", "any"): "deferred",
    ("opened", "open", "badge"): "ignored door.08",
}


def test_the_completeness_table_matches_the_hand_written_one() -> None:
    assert chart_problems(DOOR) == []
    assert sc.completeness_table(DOOR, _door()) == DOOR_TABLE


def test_the_completeness_test_fails_on_a_state_no_witness_holds() -> None:
    with pytest.raises(AssertionError, match="no witness has opened active"):
        sc.completeness_table(DOOR, _door(DOOR, "closed"))


def test_the_completeness_test_fails_on_a_cell_that_falls_through() -> None:
    gappy = replace(DOOR, rows=DOOR.rows[:-1])
    with pytest.raises(
        AssertionError, match=r"executor .UnhandledEvent: .*declaration .falls through \(open"
    ):
        sc.completeness_table(gappy, _door(gappy))


def test_the_completeness_test_fails_on_an_ambiguous_cell() -> None:
    both = replace(DOOR, guards={"badge": _badge, "no_badge": lambda ctx: True})
    with pytest.raises(AssertionError, match="AmbiguousTransition.*declaration 'ambiguous'"):
        sc.completeness_table(both, _door(both))


def test_the_completeness_test_reports_a_by_path_executors_error_as_a_cell() -> None:
    """The supervisor loads state_machine.py by path, so its executor raises
    another module object's StatechartError; the tool still reads it as the
    cell's value instead of letting it escape."""
    from dsl41 import runner_supervisor

    gappy = replace(DOOR, rows=DOOR.rows[:-1])
    by_path = sys.modules[runner_supervisor.Executor.__module__]

    def build() -> tuple[Any, str]:
        executor = runner_supervisor.Executor()
        executor.restore("w", gappy, {"door", "opened"})
        return executor, "w"

    assert by_path is not sm and by_path.UnhandledEvent is not sm.UnhandledEvent
    assert sc.is_statechart_error(by_path.UnhandledEvent("x"))
    assert not sc.is_statechart_error(ValueError("x"))
    with pytest.raises(
        AssertionError, match=r"executor .UnhandledEvent: .*declaration .falls through \(open"
    ):
        sc.completeness_table(gappy, sc.Abstraction(witnesses={"opened": build}))


@pytest.mark.parametrize("mutant", ["stale_kept", "deferral_lost"])
def test_the_completeness_test_fails_on_a_mutant_executor(
    mutant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if mutant == "stale_kept":
        monkeypatch.setattr(Executor, "_stale", staticmethod(lambda inst, event: False))
    else:
        real = sm._select
        monkeypatch.setattr(sm, "_select", lambda *a: (real(*a)[0], []))
    with pytest.raises(AssertionError, match="door completeness"):
        sc.completeness_table(DOOR, _door())


CROSS = Statechart(
    "cross",
    State(
        "cross",
        regions=(
            Region(
                "main",
                "par",
                (
                    State(
                        "par",
                        regions=(
                            Region(
                                "r1",
                                "a1",
                                (State("a1", regions=(Region("r", "a2", (State("a2"),)),)),),
                            ),
                            Region("r2", "b1", (State("b1"),)),
                        ),
                    ),
                    State("out"),
                ),
            ),
        ),
    ),
    (
        Row("cross.01", "a2", "x", "out"),
        Row("cross.02", "b1", "x", "out"),
        Row("cross.09", "cross", "x", kind="ignored"),
    ),
    clock="logical",
)


def test_the_completeness_test_resolves_crossing_regions_by_depth() -> None:
    abstraction = sc.Abstraction(
        witnesses={
            "par": _witness(CROSS, "par", "a1", "a2", "b1"),
            "out": _witness(CROSS, "out"),
        }
    )
    assert sc.completeness_table(CROSS, abstraction) == {
        ("par", "x", "any"): "cross.01",
        ("out", "x", "any"): "ignored cross.09",
    }


DUO = Statechart(
    "duo",
    State("duo", regions=(Region("left", "l", (State("l"),)), Region("right", "r", (State("r"),)))),
    (
        Row("duo.01", "l", "go", kind="internal"),
        Row("duo.02", "duo", "go", kind="ignored"),
        Row("duo.03", "duo", "stop", kind="refusal", code="closed"),
        Row("duo.04", "r", "hush", kind="ignored"),
        Row("duo.05", "l", "hush", kind="ignored"),
    ),
    clock="logical",
)


def test_the_completeness_test_lets_an_ignore_row_yield_across_regions() -> None:
    abstraction = sc.Abstraction(witnesses={"both": _witness(DUO, "l", "r")})
    assert sc.completeness_table(DUO, abstraction) == {
        ("both", "go", "any"): "duo.01",
        ("both", "hush", "any"): "ignored duo.04 + ignored duo.05",
        ("both", "stop", "any"): "refused closed",
    }


def test_the_completeness_test_fails_on_a_refusal_beside_another_row() -> None:
    clash = replace(DUO, rows=(*DUO.rows, Row("duo.06", "l", "stop", kind="internal")))
    with pytest.raises(AssertionError, match="AmbiguousTransition.*declaration 'ambiguous'"):
        sc.completeness_table(clash, sc.Abstraction(witnesses={"both": _witness(clash, "l", "r")}))


# ---- §5 the structured error


def _pick(history: int = sm.HISTORY) -> Executor:
    chart = Statechart(
        "pick",
        State("pick", regions=(Region("main", "s", (State("s"), State("t"))),)),
        (
            Row("pick.01", "s", "go", "t", guard="yes"),
            Row("pick.02", "s", "go", "t", guard="also"),
            Row("pick.03", "s", "tick", kind="internal"),
        ),
        guards={"yes": lambda ctx: True, "also": lambda ctx: True},
        clock="logical",
    )
    executor = Executor(history=history)
    executor.restore("p", chart, {"pick", "s"})
    return executor


def test_the_error_renders_every_fact_on_its_own_line() -> None:
    executor = _pick()
    executor.dispatch("p", Event("tick", {"n": 1}))
    with pytest.raises(AmbiguousTransition) as raised:
        executor.dispatch("p", Event("go", "x" * 200))
    lines = str(raised.value).splitlines()
    assert lines[0] == "pick: rows pick.01, pick.02 are all enabled in s for go"
    assert lines[1:9] == [
        "machine: pick",
        "instance: 'p'",
        "active: pick, s",
        "item: the event go",
        f"payload: {sm.reprlib.repr('x' * 200)}",
        "ancestry: none: a root item",
        "deferred: empty",
        "candidates: pick.01, pick.02",
    ]
    assert lines[9:] == [
        "recent microsteps: 1, oldest first",
        "microstep: 'p' tick fired rows pick.03 depth 0",
    ]
    assert all(
        line.startswith(f"{STATECHART_LOG_PREFIX}: ") for line in raised.value.report().splitlines()
    )


def test_the_ring_buffer_keeps_the_declared_number_of_microsteps() -> None:
    executor = _pick(history=2)
    for n in range(3):
        executor.dispatch("p", Event("tick", n))
    with pytest.raises(AmbiguousTransition) as raised:
        executor.dispatch("p", Event("go"))
    steps = [line for line in str(raised.value).splitlines() if line.startswith("microstep:")]
    assert len(steps) == 2 and "recent microsteps: 2, oldest first" in str(raised.value)


def test_an_error_outside_a_run_is_its_message_alone() -> None:
    with pytest.raises(StatechartError) as raised:
        Executor().post("nobody", Event("go"))
    assert str(raised.value) == "no instance 'nobody'"


def test_an_error_from_another_executor_keeps_its_own_report() -> None:
    inner = _pick()

    def call(ctx: Context) -> None:
        inner.dispatch("p", Event("go"))

    outer_chart = Statechart(
        "outer",
        State("outer", regions=(Region("main", "s", (State("s"),)),)),
        (Row("outer.01", "s", "go", kind="internal", action="call"),),
        actions={"call": call},
        clock="logical",
    )
    outer = Executor()
    outer.restore("o", outer_chart, {"outer", "s"})
    with pytest.raises(AmbiguousTransition) as raised:
        outer.dispatch("o", Event("go"))
    assert "machine: pick" in str(raised.value) and "machine: outer" not in str(raised.value)


def _relay() -> tuple[Executor, Statechart]:
    def go(ctx: Context) -> Effects:
        return Effects(send=((ctx.key, Event("ping")),), then="later")

    chart = Statechart(
        "relay",
        State("relay", regions=(Region("main", "s", (State("s"), State("t"))),)),
        (
            Row("relay.01", "s", "go", "t", action="go"),
            Row("relay.02", "t", COMPLETION, "s", guard="never"),
            Row("relay.03", "t", "ping", kind="internal"),
        ),
        guards={"never": lambda ctx: False},
        actions={"go": go},
        clock="logical",
    )
    executor = Executor()
    executor.restore("r", chart, {"relay", "s"})
    return executor, chart


def test_the_report_names_a_chained_item_its_ancestry_and_a_continuation() -> None:
    executor, _ = _relay()
    with pytest.raises(StatechartError) as raised:
        executor.dispatch("r", Event("go"))
    text = str(raised.value)
    # the continuation `later` is not in the registry; it runs before the completion
    assert "action 'later' is not in the registry" in text
    assert "item: the continuation later of go" in text
    assert "ancestry: 'r' go" in text


def test_the_report_names_a_completion() -> None:
    executor, chart = _relay()
    executor = Executor()
    executor.restore("r", replace(chart, actions={"go": lambda ctx: None}), {"relay", "s"})
    with pytest.raises(UnhandledEvent) as raised:
        executor.dispatch("r", Event("go"))
    assert "item: the completion of t" in str(raised.value)


def test_the_report_names_the_initial_entry_and_a_released_event() -> None:
    def enter(ctx: Context) -> None:
        raise StatechartError("an entry check failed")

    chart = Statechart(
        "boot",
        State("boot", regions=(Region("main", "s", (State("s", entry="enter"),)),)),
        (),
        actions={"enter": enter},
        clock="logical",
    )
    executor = Executor()
    executor.start("b", chart)
    with pytest.raises(StatechartError) as raised:
        executor.run()
    assert "item: the initial entry" in str(raised.value)

    gate = Statechart(
        "gate",
        State(
            "gate",
            regions=(
                Region("main", "busy", (State("busy", defer=frozenset({"job"})), State("idle"))),
            ),
        ),
        (
            Row("gate.01", "busy", "done", "idle"),
            Row("gate.02", "idle", "job", kind="internal", guard="yes"),
            Row("gate.03", "idle", "job", kind="internal", guard="yes"),
        ),
        guards={"yes": lambda ctx: True},
        clock="logical",
        defer_bound=1,
        overflow="stop",
    )
    executor = Executor()
    executor.restore("g", gate, {"gate", "busy"})
    executor.dispatch("g", Event("job"))
    with pytest.raises(AmbiguousTransition) as raised:
        executor.dispatch("g", Event("done"))
    assert "item: the released event job" in str(raised.value)
    assert "deferred: job None" in str(raised.value)


# ---- misuse


def test_misuse_raises_statechart_errors() -> None:
    executor = Executor()
    executor.restore("b", BASE, {"base", "idle"})
    with pytest.raises(StatechartError, match="instance 'b' already exists"):
        executor.restore("b", BASE, {"base", "idle"})
    for active, problem in [
        ({"base", "ghost"}, "ghost is not a declared state"),
        ({"idle"}, "the root state base is not active"),
        ({"base", "idle", "busy"}, "base has 2 active states in region main"),
    ]:
        with pytest.raises(StatechartError, match=problem):
            executor.restore("x", BASE, active)
    for name in (COMPLETION, OVERFLOWED):
        with pytest.raises(StatechartError, match="is not an event to post"):
            executor.post("b", Event(name))
    with pytest.raises(StatechartError, match="guard 'ok' is not in the registry"):
        bare = Executor()
        bare.restore("b", replace(BASE, guards={}), {"base", "idle"})
        bare.dispatch("b", Event("go"))
    assert executor.data("b") is None and executor.generation("b", "tick") == 0


def test_a_configuration_needs_each_active_parent() -> None:
    nested = Statechart(
        "nest",
        State(
            "nest",
            regions=(
                Region("main", "o", (State("o", regions=(Region("r", "i", (State("i"),)),)),)),
            ),
        ),
        (),
        clock="logical",
    )
    with pytest.raises(StatechartError, match="i is active and its parent o is not"):
        Executor().restore("n", nested, {"nest", "i"})
