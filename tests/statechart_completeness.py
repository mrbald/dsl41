"""The generated completeness test of a statechart (docs/statechart-engine.md §13).

A converted machine's test module calls `completeness_table(chart, abstraction)`.
For each witness configuration, each event of the chart's alphabet and each
payload partition, it posts the event to a fresh witness and compares the
executor's first outcome with the declaration's resolution of that cell. The
resolution is computed here, from the declaration alone, by a second reading of
§3 that shares no code with the executor's walk. Every mismatch, every
exception and every cell that falls through fails the test. The table it
returns lets a test pin the cells by hand.

A cell's value is `deferred`, `discarded`, or the rows that fire, sorted and
joined by ` + `: a row by its id, an ignored row as `ignored <id>`, a refusal
as `refused <code>`.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field
from typing import Any

from dsl41.state_machine import (
    Context,
    Event,
    Executor,
    Outcome,
    Row,
    State,
    Statechart,
    alphabet,
)

#: A configuration witness: a builder of an executor holding the instance `key`
#: in the witnessed configuration (DL-293's builders).
type Builder = Callable[[], tuple[Executor, Hashable]]

#: The partition label of an event the abstraction does not partition.
ANY = "any"
#: The suffix of a partition posted with a stale generation.
STALE = "/stale"


@dataclass(frozen=True)
class Abstraction:
    """The declared finite abstraction: witnesses by name, payload partitions by event."""

    witnesses: Mapping[str, Builder]
    partitions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _render(rows: list[Row]) -> str:
    def one(row: Row) -> str:
        if row.kind == "ignored":
            return f"ignored {row.id}"
        if row.kind == "refusal":
            return f"refused {row.code}"
        return row.id

    return " + ".join(sorted(one(row) for row in rows))


def observed(chart: Statechart, outcome: Outcome) -> str:
    """The cell value of the executor's outcome."""
    if outcome.kind in ("deferred", "discarded"):
        return outcome.kind
    by_id = {row.id: row for row in chart.rows}
    return _render([by_id[ident] for ident in outcome.rows])


class _Tree:
    """The declaration's tree, read independently of the executor."""

    def __init__(self, chart: Statechart) -> None:
        self.states: dict[str, State] = {}
        self.parent: dict[str, str] = {}
        stack = [chart.root]
        while stack:
            state = stack.pop()
            self.states[state.name] = state
            for region in state.regions:
                for sub in region.states:
                    self.parent[sub.name] = state.name
                    stack.append(sub)
        self.choices = {choice.name: choice for choice in chart.choices}

    def lineage(self, state: str) -> list[str]:
        """Root first, down to `state`."""
        line = [state]
        while line[-1] in self.parent:
            line.append(self.parent[line[-1]])
        return line[::-1]

    def region_of(self, state: str) -> int:
        parent = self.states[self.parent[state]]
        return next(i for i, r in enumerate(parent.regions) if state in {s.name for s in r.states})

    def left(self, row: Row, active: frozenset[str]) -> frozenset[str]:
        """The active states an external row leaves."""
        choice = self.choices.get(row.target)
        targets = [b.target for b in choice.branches] if choice else [row.target]
        lines = [self.lineage(s) for s in (row.source, *targets)]
        shared = 0
        while all(len(line) > shared for line in lines) and len({x[shared] for x in lines}) == 1:
            shared += 1
        source_line = lines[0]
        top = source_line[shared - 1]
        if all(len(line) > shared for line in lines):
            top = source_line[shared]
        return frozenset(s for s in active if top in self.lineage(s))


def resolution(chart: Statechart, active: frozenset[str], ctx: Context) -> str:
    """The declaration's resolution of one cell (§3, §6, §7 aside, §10)."""
    tree = _Tree(chart)
    event = ctx.event.name
    guards = chart.guards

    def enabled(state: str) -> list[Row]:
        return [
            r
            for r in chart.rows
            if r.source == state and r.event == event and (not r.guard or guards[r.guard](ctx))
        ]

    leaves = [s for s in active if not tree.states[s].regions]

    def decides(state: str) -> bool:
        return bool(enabled(state)) or event in tree.states[state].defer

    def reached(state: str) -> bool:
        """Some walk arrives at `state`: a leaf below it with no decider on the way."""
        for leaf in leaves:
            line = tree.lineage(leaf)
            if state in line and not any(decides(s) for s in line[line.index(state) + 1 :]):
                return True
        return False

    on_walks = [s for s in active if reached(s) and decides(s)]
    if any(len(enabled(s)) > 1 for s in on_walks):
        return "ambiguous"
    deferrals = [s for s in on_walks if not enabled(s)]
    rows = [
        enabled(s)[0]
        for s in on_walks
        if enabled(s) and not any(s in tree.lineage(d)[:-1] for d in deferrals)
    ]
    if any(r.kind != "ignored" for r in rows):
        rows = [r for r in rows if r.kind != "ignored"]  # an ignore row yields
    if len(rows) > 1 and any(r.kind == "refusal" for r in rows):
        return "ambiguous"  # a refusal stands alone
    depth = {r.id: len(tree.lineage(r.source)) for r in rows}
    left = {r.id: tree.left(r, active) if r.kind == "external" else frozenset() for r in rows}
    beaten: set[str] = set()
    # from the deepest outward: a row beaten by a deeper survivor drops out first
    for level in sorted(set(depth.values()), reverse=True):
        here = [r for r in rows if depth[r.id] == level]
        survivors = [r for r in rows if depth[r.id] > level and r.id not in beaten]
        for r in here:
            if any(left[r.id] & left[s.id] for s in survivors):
                beaten.add(r.id)
        alive = [r for r in here if r.id not in beaten]
        if any(a.id < b.id and left[a.id] & left[b.id] for a in alive for b in alive):
            return "ambiguous"
    firing = [r for r in rows if r.id not in beaten]
    # a row whose source another surviving row leaves does not run
    firing = [r for r in firing if not any(r.source in left[o.id] for o in firing if o is not r)]
    if firing:
        return _render(firing)
    return "deferred" if deferrals else "unhandled"


def is_statechart_error(error: BaseException) -> bool:
    """True for a `StatechartError` of the package's module or of its by-path twin.

    The supervisor loads state_machine.py by path, so its executor raises another
    module object's class. Like `is_statechart`, this reads the class, not the
    identity: a class named StatechartError in either module's ancestry."""
    return any(
        cls.__name__ == "StatechartError"
        and cls.__module__ in ("dsl41.state_machine", "state_machine")
        for cls in type(error).__mro__
    )


def _excluded(chart: Statechart, active: frozenset[str], event: str, partition: str) -> bool:
    return any(
        x.state in active and x.event == event and x.partition in ("", partition)
        for x in chart.exclusions
    )


def completeness_table(
    chart: Statechart, abstraction: Abstraction
) -> dict[tuple[str, str, str], str]:
    """Run every cell and assert that the executor matches the declaration.

    Returns `(witness, event, partition)` to the cell value. Fails when a state
    is in no witness, when a cell falls through, raises, or differs.
    """
    generational = {t.name for t in chart.time_events if t.generation}
    covered: set[str] = set()
    table: dict[tuple[str, str, str], str] = {}
    failures: list[str] = []
    for witness, build in abstraction.witnesses.items():
        executor, key = build()
        covered |= executor.active(key)
        for event in alphabet(chart):
            for label, payload in abstraction.partitions.get(event, {ANY: None}).items():
                variants = [(label, 0)]
                if event in generational:
                    variants.append((label + STALE, -1))
                for name, offset in variants:
                    cell = (witness, event, name)
                    value = _cell(
                        chart, build, event, label, payload, offset, event in generational
                    )
                    if value is None:
                        continue
                    table[cell] = value[0]
                    if value[0] != value[1]:
                        failures.append(f"{cell}: executor {value[0]!r}, declaration {value[1]!r}")
    for state in sorted(set(_Tree(chart).states) - covered):
        failures.append(f"no witness has {state} active")
    assert not failures, f"{chart.name} completeness:\n" + "\n".join(failures)
    return table


def _cell(
    chart: Statechart,
    build: Builder,
    event: str,
    label: str,
    payload: Any,
    offset: int,
    generational: bool,
) -> tuple[str, str] | None:
    """(executor's value, declaration's value) of one cell, or None when it is excluded."""
    executor, key = build()
    active = executor.active(key)
    if _excluded(chart, active, event, label):
        return None
    generation = executor.generation(key, event) + offset if generational else None
    posted = Event(event, payload, generation)
    if offset:
        expected = "discarded"
    else:
        expected = resolution(chart, active, Context(key, posted, active, executor.data(key)))
    try:
        actual = observed(chart, executor.dispatch(key, posted)[0])
    except Exception as error:  # noqa: BLE001 -- only a statechart error is a cell value
        if not is_statechart_error(error):
            raise
        actual = f"{type(error).__name__}: {error.args[0]}"
    if expected == "unhandled":
        expected = f"falls through ({event} in {sorted(active)})"
    return actual, expected
