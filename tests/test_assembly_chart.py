"""runtime_assembly as a statechart (docs/statechart-engine.md §16, DL-335).

The chart is well formed and draws no warning. The generated completeness
test runs a witness for each of the eight phases, built through the store's
own verbs, and posts each of the five verbs, against a table written from the
constructor's order (period-model §7) and DL-132, not from the rows. The state
owner holds the instance from construction: the configuration is the phase,
a fork and a rebuild restore it, and a refusal raises the class and text the
verb always raised. A gap in the chart is a `StatechartError`, met by the
constructor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, get_args

import pytest
import statechart_completeness as sc

from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    ASSEMBLY_KEY,
    RUNTIME_ASSEMBLY,
    AssemblyPhase,
    CarriedRows,
    HostRuntime,
    OracleError,
    RuntimeState,
)
from dsl41.state_machine import (
    Executor,
    Row,
    StatechartError,
    UnhandledEvent,
    Violation,
    alphabet,
    chart_warnings,
    well_formed,
)

CHART = RUNTIME_ASSEMBLY
PHASES = tuple(get_args(AssemblyPhase.__value__))
T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
VERBS = ("install", "begin_input", "commit_input", "finish_genesis", "seed_period")


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert alphabet(CHART) == sorted(VERBS)
    assert not CHART.actions  # the tier's work follows the microstep
    assert not CHART.guards  # a phase decides; a period below one is the verb's own check
    assert {row.code for row in CHART.rows if row.kind == "refusal"} == {
        "install_on_used_state",
        "input_already_open",
        "no_input_open",
        "inside_genesis_input",
        "construction_finished",
        "construction_after_seed",
        "seed_inside_input",
        "seed_on_used_state",
    }
    assert [row.id for row in CHART.rows if row.kind != "refusal"] == [
        f"runtime_assembly.{n:02d}" for n in range(1, 16)
    ]
    assert len(CHART.rows) == 40  # every phase by verb has its own row
    assert [row for row in CHART.rows if row.mark] == []


def test_the_phases_are_the_chart_s_leaves() -> None:
    leaves = {state.name for region in CHART.root.regions for state in region.states}
    assert leaves == set(PHASES)
    store = RuntimeState()
    assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", "fresh"}
    assert store._phase == "fresh"
    with pytest.raises(AttributeError):
        store._phase = "live"  # type: ignore[misc]


# ---- the abstraction (§13)

#: the verb orders that build each phase through the store's own verbs
BUILDS: dict[str, tuple[str, ...]] = {
    "fresh": (),
    "installed": ("install",),
    "genesis_input": ("begin_input",),
    "genesis": ("begin_input", "commit_input"),
    "constructed": ("finish_genesis",),
    "seeded": ("seed_period",),
    "input": ("finish_genesis", "begin_input"),
    "live": ("finish_genesis", "begin_input", "commit_input"),
}


def _verb(store: RuntimeState, name: str) -> None:
    if name == "install":
        store.install(CarriedRows())
    elif name == "seed_period":
        store.seed_period(1)
    else:
        getattr(store, name)()


def _built(phase: str) -> RuntimeState:
    store = RuntimeState()
    for name in BUILDS[phase]:
        _verb(store, name)
    assert store._phase == phase
    return store


def _witness(phase: str) -> sc.Builder:
    def build() -> tuple[Any, Any]:
        return _built(phase).machines, ASSEMBLY_KEY

    return build


ABSTRACTION = sc.Abstraction(witnesses={phase: _witness(phase) for phase in PHASES})

#: Each cell as the constructor's order and DL-132 read it, without the rows:
#: the genesis seed is one input (begin, commit) after an optional install,
#: construction ends once and before the seed, the seed is legal once and
#: before any committed input, and a commit needs an input open. A cell is
#: the row that fires and the phase it reaches, or the refusal code. The row
#: ids are the cell's name in the table; the targets are the phases.
type Cell = tuple[str, str] | str
CELLS: dict[str, dict[str, Cell]] = {
    "fresh": {
        "install": ("01", "installed"),
        "begin_input": ("02", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("04", "constructed"),
        "seed_period": ("05", "seeded"),
    },
    "installed": {
        "install": "install_on_used_state",
        "begin_input": ("08", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("10", "constructed"),
        "seed_period": ("12", "seeded"),
    },
    "genesis_input": {
        "install": "install_on_used_state",
        "begin_input": "input_already_open",
        "commit_input": ("03", "genesis"),
        "finish_genesis": "inside_genesis_input",
        "seed_period": "seed_inside_input",
    },
    "genesis": {
        "install": "install_on_used_state",
        "begin_input": ("09", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("11", "constructed"),
        "seed_period": "seed_on_used_state",
    },
    "constructed": {
        "install": "install_on_used_state",
        "begin_input": ("06", "input"),
        "commit_input": "no_input_open",
        "finish_genesis": "construction_finished",
        "seed_period": ("13", "seeded"),
    },
    "seeded": {
        "install": "install_on_used_state",
        "begin_input": ("14", "input"),
        "commit_input": "no_input_open",
        "finish_genesis": "construction_after_seed",
        "seed_period": "seed_on_used_state",
    },
    "input": {
        "install": "install_on_used_state",
        "begin_input": "input_already_open",
        "commit_input": ("07", "live"),
        "finish_genesis": "construction_finished",
        "seed_period": "seed_inside_input",
    },
    "live": {
        "install": "install_on_used_state",
        "begin_input": ("15", "input"),
        "commit_input": "no_input_open",
        "finish_genesis": "construction_finished",
        "seed_period": "seed_on_used_state",
    },
}


def _expected(cell: Cell) -> str:
    return f"refused {cell}" if isinstance(cell, str) else f"runtime_assembly.{cell[0]}"


def test_the_completeness_table_is_the_constructor_s_order() -> None:
    """Every cell: eight phases by five verbs. No exclusion."""
    assert CHART.exclusions == ()
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert table == {
        (phase, verb, sc.ANY): _expected(CELLS[phase][verb]) for phase in PHASES for verb in VERBS
    }
    assert len(table) == 40


@pytest.mark.parametrize("phase", PHASES)
def test_a_verb_moves_the_phase_as_the_table_says_or_refuses_it(phase: str) -> None:
    """The store's own verbs on each witness: a fired cell reaches the phase
    it names, a refusal moves nothing and raises the class and text of the
    verb that has always raised it (`ValueError` for a guard, `OracleError`
    for a move the old table left out)."""
    for verb in VERBS:
        store = _built(phase)
        cell = CELLS[phase][verb]
        if isinstance(cell, tuple):
            _verb(store, verb)
            assert store._phase == cell[1], (phase, verb)
            assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", cell[1]}
            continue
        with pytest.raises(ValueError) as raised:
            _verb(store, verb)
        assert (type(raised.value), str(raised.value)) == TEXTS[cell](phase), (phase, verb)
        assert store._phase == phase


def _move(verb: str, rule: str) -> str:
    return f"{verb} in assembly phase {{phase}}: {{phase}} is not a source of {rule}"


#: the sentence each code raises: the exception class and the text
TEXTS: dict[str, Any] = {
    "install_on_used_state": lambda phase: (
        ValueError,
        "install on a used state: carried rows are assembly's first act, and a"
        " live state advances through its own inputs alone (period-model ss7)",
    ),
    "input_already_open": lambda phase: (OracleError, "input already open: inputs do not nest"),
    "no_input_open": lambda phase: (
        OracleError,
        _move("commit_input", "runtime_assembly.07").format(phase=phase),
    ),
    "inside_genesis_input": lambda phase: (
        OracleError,
        _move("finish_genesis", "runtime_assembly.04").format(phase=phase),
    ),
    "construction_finished": lambda phase: (
        ValueError,
        "finish_genesis twice: construction happens once",
    ),
    "construction_after_seed": lambda phase: (
        ValueError,
        "finish_genesis after seed_period: construction comes first",
    ),
    "seed_inside_input": lambda phase: (
        ValueError,
        "seed_period inside an input: assembly precedes inputs",
    ),
    "seed_on_used_state": lambda phase: (
        ValueError,
        "seed_period on a used state: seeding is assembly's first act, and a"
        " live state never moves its period (I2)",
    ),
}


def test_a_period_below_one_is_the_verb_s_own_check_after_the_chart() -> None:
    """The chart is read first: on a used state the refusal wins over the
    period; on a fresh one the period fails and the phase stays."""
    live = _built("live")
    with pytest.raises(ValueError, match="used state"):
        live.seed_period(0)
    fresh = RuntimeState()
    with pytest.raises(ValueError, match="periods count from 1"):
        fresh.seed_period(0)
    assert fresh._phase == "fresh"
    fresh.seed_period(2)
    assert (fresh._phase, fresh.period_id) == ("seeded", 2)


# ---- the store's executor (statechart-engine §11)


@pytest.mark.parametrize(
    ("row", "carried"),
    [("runtime_assembly.02", None), ("runtime_assembly.08", CarriedRows(period_id=2))],
)
def test_a_gap_in_the_chart_is_a_statechart_error_through_the_constructor(
    monkeypatch: pytest.MonkeyPatch, row: str, carried: CarriedRows | None
) -> None:
    """A chart copy without the row that opens the genesis seed (from `fresh`,
    or from `installed` when rows are carried): the constructor raises the
    executor's own error, not an `OracleError`."""
    rows = tuple(r for r in CHART.rows if r.id != row)
    monkeypatch.setattr("dsl41.oracle_state.RUNTIME_ASSEMBLY", replace(CHART, rows=rows))
    with pytest.raises(UnhandledEvent, match="no row and no deferral for begin_input"):
        Oracle(lower_source(_JIL), carried=carried)


def test_a_gap_is_met_by_a_verb_the_constructor_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit of an input, from the store that reached `input`."""
    rows = tuple(r for r in CHART.rows if r.id != "runtime_assembly.07")
    monkeypatch.setattr("dsl41.oracle_state.RUNTIME_ASSEMBLY", replace(CHART, rows=rows))
    store = _built("input")
    with pytest.raises(UnhandledEvent) as raised:
        store.commit_input()
    assert ("machine", "runtime_assembly") in raised.value.facts


def test_a_refusal_code_the_state_owner_has_no_sentence_for_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tier names every code; a chart refusal with another code is a
    disagreement between the two, never a refusal under a borrowed text."""
    refusal = Row(
        "runtime_assembly.23",
        "fresh",
        "commit_input",
        kind="refusal",
        code="input_not_open",
    )
    rows = tuple(refusal if row.id == "runtime_assembly.23" else row for row in CHART.rows)
    monkeypatch.setattr("dsl41.oracle_state.RUNTIME_ASSEMBLY", replace(CHART, rows=rows))
    store = RuntimeState()
    with pytest.raises(StatechartError, match="the refusal code 'input_not_open' has no") as raised:
        store.commit_input()
    assert ("machine", "runtime_assembly") in raised.value.facts
    assert ("active", "fresh, runtime_assembly") in raised.value.facts
    assert store._phase == "fresh"


def test_an_input_opened_twice_is_refused_and_leaves_the_pending_violations() -> None:
    """The read comes before the orphans are dropped: an open input's own
    violations are not drained by the refused begin."""
    violation = Violation("job_flags", "x", "a", "b", "pending")
    store = _built("live")
    store.begin_input()
    store.note_violation("j", violation)
    with pytest.raises(OracleError, match="input already open"):
        store.begin_input()
    assert store.drain_violations() == [("j", violation)]
    store.commit_input()


def test_a_fork_keeps_the_source_s_phase_in_its_own_executor() -> None:
    """At rest and mid-input: the twin restores the source's phase, its
    moves never reach the source's instance, and the source's never reach
    the twin's."""
    store = _built("live")
    rest = store.fork()
    assert rest.machines is not store.machines
    assert rest._phase == "live"
    rest.begin_input()
    assert (rest._phase, store._phase) == ("input", "live")
    rest.commit_input()
    store.begin_input()
    open_twin = store.fork()  # taken mid-input
    assert open_twin._phase == "input"
    open_twin.commit_input()
    assert (open_twin._phase, store._phase) == ("live", "input")
    with pytest.raises(OracleError, match="input already open"):
        store.begin_input()
    store.commit_input()
    assert store._phase == "live"


def test_an_install_rebuilds_the_executor_from_the_phase_and_the_carried_rows() -> None:
    store = RuntimeState()
    machines = store.machines
    store.install(CarriedRows(hosts={"h": HostRuntime(state="passive")}, period_id=2))
    assert store.machines is not machines
    assert store._phase == "installed"
    assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", "installed"}
    assert store.machines.active(store.host_key("h")) == {"host", "routed", "passive"}


@pytest.mark.parametrize("phase", ["installed", "genesis_input", "input", "live"])
def test_a_witness_rebuild_keeps_the_phase(phase: str) -> None:
    store = _built(phase)
    store.register_host("h", deadman_s=60.0, at=T0)
    machines = store.machines
    store.witness_host("h", state="quarantined", state_before_quarantine="active")
    assert store.machines is not machines
    assert store._phase == phase
    assert store.machines.active(store.host_key("h")) == {"host", "quarantined"}


# ---- the alphabet: every verb the state owner dispatches is in the chart


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The events dispatched to an assembly instance, in order. A read
    (`resolve`) is not a dispatch."""
    seen: list[str] = []
    dispatch = Executor.dispatch

    def record(self: Executor, key: object, event: Any) -> Any:
        if key == ASSEMBLY_KEY:
            seen.append(event.name)
        return dispatch(self, key, event)

    monkeypatch.setattr(Executor, "dispatch", record)
    return seen


def test_the_constructor_without_carried_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy(monkeypatch)
    oracle = Oracle(lower_source(_JIL))
    assert seen == ["begin_input", "commit_input", "finish_genesis"]
    assert oracle.store._phase == "constructed"


def test_the_constructor_with_carried_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reopen: install first, then the genesis seed, then the carried
    period (period-model §7 phase 3)."""
    seen = _spy(monkeypatch)
    oracle = Oracle(lower_source(_JIL), carried=CarriedRows(period_id=3))
    assert seen == ["install", "begin_input", "commit_input", "finish_genesis", "seed_period"]
    assert oracle.store._phase == "seeded"
    assert oracle.store.period_id == 3


def test_an_input_batch_is_the_input_pair_and_a_fork_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = Oracle(lower_source(_JIL))
    seen = _spy(monkeypatch)
    with oracle.batch(T0):
        assert oracle.store._phase == "input"
        assert oracle.fork().store._phase == "input"  # mid-input
    assert oracle.store._phase == "live"
    assert oracle.fork().store._phase == "live"  # at rest
    assert seen == ["begin_input", "commit_input"]


def test_every_event_the_state_owner_posts_is_in_the_alphabet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy(monkeypatch)
    Oracle(lower_source(_JIL), carried=CarriedRows(period_id=2))
    store = _built("live")
    store.begin_input()
    store.commit_input()
    assert set(seen) == set(alphabet(CHART))
