"""`Executor.resolve`, the read of the selection (docs/statechart-engine.md §5, DL-335).

It never mutates an instance, the history ring or the hits, and it agrees with
the first microstep of a dispatch on every cell of the assembly chart, on the
host chart (guards, an ignored row, a choice) and on a small chart with a
deferral and a re-armable time event.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41.oracle_state import (
    FORCE_EVICT,
    HOST,
    REGISTER,
    RUNTIME_ASSEMBLY,
    HostData,
    RuntimeState,
)
from dsl41.state_machine import (
    HITS_ENV,
    AmbiguousTransition,
    Executor,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
    TimeEvent,
    UnhandledEvent,
    alphabet,
)
from dsl41.state_machine import Event as ChartEvent
from test_assembly_chart import ABSTRACTION as ASSEMBLY
from test_host_chart import ABSTRACTION as HOST_WITNESSES


def _agrees(chart: Statechart, abstraction: sc.Abstraction) -> int:
    """For every witness and event: resolve, then dispatch an identical witness
    and compare the outcome. Returns the cells compared."""
    cells = 0
    for build in abstraction.witnesses.values():
        for event in alphabet(chart):
            executor, key = build()
            if any(x.state in executor.active(key) and x.event == event for x in chart.exclusions):
                continue
            before = (executor.active(key), executor.deferred(key))
            posted = ChartEvent(event)
            read = executor.resolve(key, posted)
            assert (executor.active(key), executor.deferred(key)) == before
            (done, *_) = build()[0].dispatch(key, posted)
            assert (read.kind, read.rows, read.ignored, read.refusals) == (
                done.kind,
                done.rows,
                done.ignored,
                done.refusals,
            )
            cells += 1
    return cells


def test_it_agrees_with_dispatch_on_every_cell_of_the_assembly_chart() -> None:
    assert _agrees(RUNTIME_ASSEMBLY, ASSEMBLY) == 40


def test_it_agrees_with_dispatch_on_the_host_chart() -> None:
    """Guards (`not_evicted`, `remembered_passive`), an ignored row and a choice:
    a choice's branches are the one thing a read does not name."""
    assert _agrees(HOST, HOST_WITNESSES) == 34


def test_it_names_no_branch() -> None:
    """A choice resolves as its row runs: the read names the row, not the branch."""
    store = RuntimeState()
    store.begin_input()
    store.register_host("h", deadman_s=60.0, at=None)
    store.witness_host("h", state="quarantined", state_before_quarantine="passive")
    key = store.host_key("h")
    read = store.machines.resolve(key, ChartEvent("reinstate"))
    assert (read.rows, read.branches) == (("host.07",), ())
    assert store.machines.active(key) == {"host", "quarantined"}
    assert store.machines.data(key) == HostData(store, "h")


def test_it_records_no_hit_and_no_ring_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    store = RuntimeState()
    store.begin_input()
    store.register_host("h", deadman_s=60.0, at=None)  # a guardless dispatch
    store.commit_input()
    for path in directory.glob("hits-*.jsonl"):
        path.unlink()
    key = store.host_key("h")
    ring = list(store.machines._recent["host"])
    for event in alphabet(HOST):
        store.machines.resolve(key, ChartEvent(event))  # force_evict's guard runs
    assert list(directory.glob("hits-*.jsonl")) == []
    assert list(store.machines._recent["host"]) == ring
    store.machines.dispatch(key, ChartEvent(FORCE_EVICT))
    kinds = {
        (r["kind"], r["id"])
        for path in directory.glob("hits-*.jsonl")
        for r in map(json.loads, path.read_text().splitlines())
    }
    assert ("guard", "host.11") in {(k, i) for k, i in kinds}  # a dispatch does record it


def test_a_scratch_instance_of_a_chart_with_guards() -> None:
    """The host chart through a scratch instance, as a tier's gate would build it."""
    scratch = Executor()
    store = RuntimeState()
    store.begin_input()
    store.register_host("h", deadman_s=60.0, at=None)
    key = store.host_key("h")
    scratch.restore(key, HOST, {"host", "evicted"}, HostData(store, "h"))
    assert scratch.resolve(key, ChartEvent(FORCE_EVICT)).refusals == ("host_already_evicted",)
    assert scratch.active(key) == {"host", "evicted"}
    scratch.restore("other", HOST, {"host", "routed", "active"}, HostData(store, "h"))
    assert scratch.resolve("other", ChartEvent(REGISTER)).ignored == ("host.22",)


# ---- a small chart: an action, deferral, a re-armable time event, and the errors

RAN: list[str] = []


def _act(ctx: Any) -> None:
    RAN.append(ctx.event.name)


_CHART = Statechart(
    name="gadget",
    root=State(
        "gadget",
        regions=(
            Region(
                "r",
                "idle",
                (
                    State("idle", defer=frozenset({"later"})),
                    State("busy"),
                ),
            ),
        ),
    ),
    rows=(
        Row("gadget.01", "idle", "go", "busy", action="act"),
        Row("gadget.02", "idle", "tick", kind="ignored"),
        Row("gadget.03", "busy", "later", "idle"),
        Row("gadget.04", "idle", "twice", "busy", guard="yes"),
        Row("gadget.05", "idle", "twice", "busy", guard="yes"),
    ),
    guards={"yes": lambda ctx: True},
    actions={"act": _act},
    time_events=(TimeEvent("tick", generation=True),),
    defer_bound=1,
    overflow="stop",
    clock="logical",
)


def _gadget(chart: Statechart = _CHART) -> Executor:
    executor = Executor()
    executor.start("g", chart)
    executor.run()
    RAN.clear()
    return executor


def _events(executor: Executor) -> list[ChartEvent]:
    live = executor.generation("g", "tick")
    return [
        ChartEvent("go"),
        ChartEvent("later"),
        ChartEvent("tick", None, live),
        ChartEvent("tick", None, live + 1),
        ChartEvent("twice"),
        ChartEvent("nothing"),
    ]


def test_it_agrees_with_a_dispatch_on_a_twin_and_runs_no_action() -> None:
    """Every event of the gadget, by a read and by a dispatch on a twin build:
    the same outcome, or the same error with the same facts. A read runs no
    action, and the dispatch of `go` does."""
    for index in range(len(_events(_gadget()))):
        reader, twin = _gadget(), _gadget()
        event = _events(reader)[index]
        try:
            (done, *_) = twin.dispatch("g", event)
        except StatechartError as raised:
            with pytest.raises(type(raised)) as read_error:
                reader.resolve("g", event)
            assert read_error.value.facts == raised.facts, event
            continue
        read = reader.resolve("g", event)
        assert (read.kind, read.rows, read.ignored, read.refusals) == (
            done.kind,
            done.rows,
            done.ignored,
            done.refusals,
        )
        assert reader.active("g") == {"gadget", "idle"}
        assert reader.deferred("g") == ()
    reader = _gadget()
    reader.resolve("g", ChartEvent("go"))
    assert RAN == []
    reader.dispatch("g", ChartEvent("go"))
    assert RAN == ["go"]


def test_it_reads_the_errors_and_keeps_an_error_s_facts() -> None:
    executor = _gadget()
    with pytest.raises(AmbiguousTransition) as ambiguous:
        executor.resolve("g", ChartEvent("twice"))
    assert ("machine", "gadget") in ambiguous.value.facts
    with pytest.raises(UnhandledEvent):
        executor.resolve("g", ChartEvent("nothing"))
    with pytest.raises(StatechartError, match="no instance"):
        executor.resolve("nope", ChartEvent("go"))
    with pytest.raises(StatechartError, match="needs its generation"):
        executor.resolve("g", ChartEvent("tick"))

    def raises(ctx: Any) -> bool:
        error = StatechartError("from a nested executor")
        error.facts = [("machine", "elsewhere")]
        raise error

    chart = replace(_CHART, guards={"yes": raises})
    for drive in ("resolve", "dispatch"):
        executor = _gadget(chart)
        with pytest.raises(StatechartError) as raised:
            getattr(executor, drive)("g", ChartEvent("twice"))
        assert raised.value.facts == [("machine", "elsewhere")], drive  # kept, as `run` keeps it


def test_it_refuses_an_instance_with_a_deferred_event_and_stays_usable() -> None:
    """A dispatch would release the deferred event first; the read answers
    only the posted event, so it refuses rather than answer for a dispatch."""
    executor = _gadget()
    executor.dispatch("g", ChartEvent("later"))
    assert len(executor.deferred("g")) == 1
    with pytest.raises(
        StatechartError, match="exact only with nothing deferred; 1 deferred"
    ) as raised:
        executor.resolve("g", ChartEvent("go"))
    assert ("machine", "gadget") in raised.value.facts
    assert len(executor.deferred("g")) == 1
    assert executor.dispatch("g", ChartEvent("go"))[0].rows == ("gadget.01",)  # not left in flight


@pytest.mark.parametrize("drive", ["dispatch", "resolve"])
def test_a_guard_that_reaches_into_the_executor_is_re_entrance_under_either(drive: str) -> None:
    from dsl41.state_machine import ReentrantDispatch

    seen: list[Any] = []

    def reads(ctx: Any) -> bool:
        for nested in (executor.resolve, executor.dispatch):
            try:
                nested("g", ChartEvent("go"))
            except ReentrantDispatch as error:
                seen.append(error)
        return True

    chart = replace(_CHART, guards={"yes": reads}, rows=_CHART.rows[:1] + _CHART.rows[3:4])
    executor = _gadget(chart)
    getattr(executor, drive)("g", ChartEvent("twice"))
    assert len(seen) == 2
    assert executor.active("g") == (
        {"gadget", "busy"} if drive == "dispatch" else {"gadget", "idle"}
    )
