"""runtime_assembly as a statechart (docs/statechart-engine.md §16, DL-335).

The chart is well formed and draws no warning. The generated completeness
test runs a witness for each of the eight phases, built through the store's
own verbs, and posts each of the five verbs, against a table written from the
constructor's order (period-model §7) and DL-132, not from the rows. The state
owner holds the instance from construction: the configuration is the phase
with its group, a fork copies it (DL-339) and a rebuild restores it, and a refusal raises the
class and text the verb always raised. A gap in the chart is a `StatechartError`, met by the
constructor. A rule is one row on the group whose phases all take it (DL-373): every cell
answers as the flat chart of DL-335 did, and a row deleted leaves a gap.
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
    ASSEMBLY_PHASES,
    RUNTIME_ASSEMBLY,
    AssemblyPhase,
    CarriedRows,
    HostRuntime,
    OracleError,
    RuntimeState,
)
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import (
    Executor,
    Row,
    State,
    StatechartError,
    UnhandledEvent,
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
        f"runtime_assembly.{n:02d}" for n in (1, 3, 4, 5, 7, 12, 13, 41, 42)
    ]
    assert len(CHART.rows) == 25
    assert {row.id for row in CHART.rows if row.source in GROUPS} == GROUP_ROWS
    assert {row.id for row in CHART.rows} & RETIRED == set()  # a retired id stays out (DL-364)
    assert [row for row in CHART.rows if row.mark] == []


#: the group above each phase (DL-373)
GROUP: dict[str, str] = {
    "fresh": "unbuilt",
    "installed": "unbuilt",
    "genesis": "unbuilt",
    "genesis_input": "open",
    "input": "open",
    "constructed": "built",
    "seeded": "built",
    "live": "built",
}
GROUPS = frozenset(GROUP.values())
#: the rows whose source is a group: a rule every member takes, with no inner row for the verb
GROUP_ROWS = {f"runtime_assembly.{n:02d}" for n in (4, 41, 42, 43, 44, 45, 46, 47, 48)}
#: the ids the group rows retired
RETIRED = {
    f"runtime_assembly.{n:02d}"
    for n in (2, 6, 8, 9, 10, 11, 14, 15, 17, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30)
    + (36, 39)
}


def _parents(state: State) -> dict[str, str]:
    """Each state below `state` to its parent."""
    found: dict[str, str] = {}
    for region in state.regions:
        for child in region.states:
            found[child.name] = state.name
            found |= _parents(child)
    return found


def test_the_phases_are_the_chart_s_leaves_and_each_sits_in_one_group() -> None:
    parents = _parents(CHART.root)
    leaves = {name for name in parents if name not in parents.values()}
    assert leaves == set(PHASES) == ASSEMBLY_PHASES
    assert {name: parent for name, parent in parents.items() if name in leaves} == GROUP
    assert {parents[group] for group in GROUPS} == {"runtime_assembly"}
    store = RuntimeState()
    assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", "unbuilt", "fresh"}
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
        "begin_input": ("41", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("04", "constructed"),
        "seed_period": ("05", "seeded"),
    },
    "installed": {
        "install": "install_on_used_state",
        "begin_input": ("41", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("04", "constructed"),
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
        "begin_input": ("41", "genesis_input"),
        "commit_input": "no_input_open",
        "finish_genesis": ("04", "constructed"),
        "seed_period": "seed_on_used_state",
    },
    "constructed": {
        "install": "install_on_used_state",
        "begin_input": ("42", "input"),
        "commit_input": "no_input_open",
        "finish_genesis": "construction_finished",
        "seed_period": ("13", "seeded"),
    },
    "seeded": {
        "install": "install_on_used_state",
        "begin_input": ("42", "input"),
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
        "begin_input": ("42", "input"),
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


#: Each cell as the flat chart of DL-335 answered it, one fired row per cell (kind `fired`):
#: that row's id and the phase reached or the refusal code. Generated once from that declaration's executor; the
#: grouping (DL-373) must answer every cell the same, under `MERGED`.
FLAT: dict[tuple[str, str], tuple[str, str]] = {
    ("fresh", "install"): ("01", "installed"),
    ("fresh", "begin_input"): ("02", "genesis_input"),
    ("fresh", "commit_input"): ("23", "refused no_input_open"),
    ("fresh", "finish_genesis"): ("04", "constructed"),
    ("fresh", "seed_period"): ("05", "seeded"),
    ("installed", "install"): ("16", "refused install_on_used_state"),
    ("installed", "begin_input"): ("08", "genesis_input"),
    ("installed", "commit_input"): ("24", "refused no_input_open"),
    ("installed", "finish_genesis"): ("10", "constructed"),
    ("installed", "seed_period"): ("12", "seeded"),
    ("genesis_input", "install"): ("17", "refused install_on_used_state"),
    ("genesis_input", "begin_input"): ("29", "refused input_already_open"),
    ("genesis_input", "commit_input"): ("03", "genesis"),
    ("genesis_input", "finish_genesis"): ("33", "refused inside_genesis_input"),
    ("genesis_input", "seed_period"): ("36", "refused seed_inside_input"),
    ("genesis", "install"): ("18", "refused install_on_used_state"),
    ("genesis", "begin_input"): ("09", "genesis_input"),
    ("genesis", "commit_input"): ("25", "refused no_input_open"),
    ("genesis", "finish_genesis"): ("11", "constructed"),
    ("genesis", "seed_period"): ("37", "refused seed_on_used_state"),
    ("constructed", "install"): ("19", "refused install_on_used_state"),
    ("constructed", "begin_input"): ("06", "input"),
    ("constructed", "commit_input"): ("26", "refused no_input_open"),
    ("constructed", "finish_genesis"): ("31", "refused construction_finished"),
    ("constructed", "seed_period"): ("13", "seeded"),
    ("seeded", "install"): ("20", "refused install_on_used_state"),
    ("seeded", "begin_input"): ("14", "input"),
    ("seeded", "commit_input"): ("27", "refused no_input_open"),
    ("seeded", "finish_genesis"): ("32", "refused construction_after_seed"),
    ("seeded", "seed_period"): ("38", "refused seed_on_used_state"),
    ("input", "install"): ("21", "refused install_on_used_state"),
    ("input", "begin_input"): ("30", "refused input_already_open"),
    ("input", "commit_input"): ("07", "live"),
    ("input", "finish_genesis"): ("34", "refused construction_finished"),
    ("input", "seed_period"): ("39", "refused seed_inside_input"),
    ("live", "install"): ("22", "refused install_on_used_state"),
    ("live", "begin_input"): ("15", "input"),
    ("live", "commit_input"): ("28", "refused no_input_open"),
    ("live", "finish_genesis"): ("35", "refused construction_finished"),
    ("live", "seed_period"): ("40", "refused seed_on_used_state"),
}
#: Each group row and the flat rows whose cells it takes; every other flat row kept its id.
MERGED: dict[str, tuple[str, ...]] = {
    "04": ("04", "10", "11"),
    "41": ("02", "08", "09"),
    "42": ("06", "14", "15"),
    "43": ("17", "21"),
    "44": ("19", "20", "22"),
    "45": ("23", "24", "25"),
    "46": ("26", "27", "28"),
    "47": ("29", "30"),
    "48": ("36", "39"),
}


def _restored(phase: str, chart: Any = CHART) -> Executor:
    executor = Executor()
    executor.restore(ASSEMBLY_KEY, chart, {"runtime_assembly", GROUP[phase], phase})
    return executor


def test_every_cell_answers_as_the_flat_chart_did() -> None:
    """40 of 40 cells: the same outcome and the same phase, the configuration is the phase
    with its group, and the row is the flat one's or the group row that took its cell."""
    renamed = {old: new for new, olds in MERGED.items() for old in olds}
    assert set(renamed.values()) == {row.removeprefix("runtime_assembly.") for row in GROUP_ROWS}
    assert {f"runtime_assembly.{old}" for old in renamed} - {"runtime_assembly.04"} == RETIRED
    for (phase, verb), (flat_row, answer) in FLAT.items():
        executor = _restored(phase)
        (outcome,) = executor.dispatch(ASSEMBLY_KEY, ChartEvent(verb))
        assert outcome.kind == "fired"  # every flat cell fired its one row
        assert outcome.rows == (f"runtime_assembly.{renamed.get(flat_row, flat_row)}",)
        reached = phase if outcome.refusals else answer
        assert outcome.refusals == ((answer.removeprefix("refused "),) if outcome.refusals else ())
        assert answer.startswith("refused ") is bool(outcome.refusals), (phase, verb)
        assert executor.active(ASSEMBLY_KEY) == {"runtime_assembly", GROUP[reached], reached}
    assert len(FLAT) == 40


@pytest.mark.parametrize("row", sorted(GROUP_ROWS))
def test_a_group_row_deleted_leaves_a_gap_at_every_member(
    monkeypatch: pytest.MonkeyPatch, row: str
) -> None:
    """No group row has an inner row beside it for its verb, so a chart copy without it
    raises the executor's own error at each phase it covers, through the store's verb,
    and never falls into another rule's refusal (DL-335)."""
    (rule,) = (r for r in CHART.rows if r.id == row)
    members = [phase for phase in PHASES if GROUP[phase] == rule.source]
    assert len(members) >= 2
    gapped = replace(CHART, rows=tuple(r for r in CHART.rows if r.id != row))
    for phase in members:
        store = _built(phase)
        monkeypatch.setattr("dsl41.oracle_state.RUNTIME_ASSEMBLY", gapped)
        store.machines = store.rebuild_machines()
        with pytest.raises(UnhandledEvent, match=f"no row and no deferral for {rule.event}"):
            _verb(store, rule.event)
        assert store._phase == phase
        monkeypatch.undo()


def test_no_row_deleted_falls_into_another_row() -> None:
    """Every row of the chart: without it, each cell it took is a gap and every other cell
    answers as before."""
    for row in CHART.rows:
        gapped = replace(CHART, rows=tuple(r for r in CHART.rows if r.id != row.id))
        for phase in PHASES:
            for verb in VERBS:
                (outcome,) = _restored(phase).dispatch(ASSEMBLY_KEY, ChartEvent(verb))
                if outcome.rows != (row.id,):
                    (other,) = _restored(phase, gapped).dispatch(ASSEMBLY_KEY, ChartEvent(verb))
                    assert other.rows == outcome.rows
                    continue
                with pytest.raises(UnhandledEvent):
                    _restored(phase, gapped).dispatch(ASSEMBLY_KEY, ChartEvent(verb))


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
            assert store.machines.active(ASSEMBLY_KEY) == {
                "runtime_assembly",
                GROUP[cell[1]],
                cell[1],
            }
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


@pytest.mark.parametrize("carried", [None, CarriedRows(period_id=2)])
def test_a_gap_in_the_chart_is_a_statechart_error_through_the_constructor(
    monkeypatch: pytest.MonkeyPatch, carried: CarriedRows | None
) -> None:
    """A chart copy without the row that opens the genesis seed (from `fresh`,
    or from `installed` when rows are carried): the constructor raises the
    executor's own error, not an `OracleError`."""
    rows = tuple(r for r in CHART.rows if r.id != "runtime_assembly.41")
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
        "runtime_assembly.45",
        "unbuilt",
        "commit_input",
        kind="refusal",
        code="input_not_open",
    )
    rows = tuple(refusal if row.id == "runtime_assembly.45" else row for row in CHART.rows)
    monkeypatch.setattr("dsl41.oracle_state.RUNTIME_ASSEMBLY", replace(CHART, rows=rows))
    store = RuntimeState()
    with pytest.raises(StatechartError, match="the refusal code 'input_not_open' has no") as raised:
        store.commit_input()
    assert ("machine", "runtime_assembly") in raised.value.facts
    assert ("active", "fresh, runtime_assembly, unbuilt") in raised.value.facts
    assert store._phase == "fresh"


def test_a_fork_keeps_the_source_s_phase_in_its_own_executor() -> None:
    """At rest and mid-input: the twin carries the source's phase, its
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
    assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", "unbuilt", "installed"}
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
    assert store.machines.active(ASSEMBLY_KEY) == {"runtime_assembly", GROUP[phase], phase}


@pytest.mark.parametrize("phase", PHASES)
def test_a_fork_and_a_rebuild_carry_the_phase_with_its_group(phase: str) -> None:
    """The configuration carries the group; a fork and a rebuild restore the same
    configuration, and the phase is its leaf."""
    store = _built(phase)
    configuration = store.machines.active(ASSEMBLY_KEY)
    assert configuration == {"runtime_assembly", GROUP[phase], phase}
    assert store.fork().machines.active(ASSEMBLY_KEY) == configuration
    assert store.rebuild_machines().active(ASSEMBLY_KEY) == configuration


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
