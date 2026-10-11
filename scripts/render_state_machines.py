"""Write `docs/state-machines.md`: a diagram and a transition table per machine.

Run it after a machine in `dsl41.machines.MACHINES` changes:

    uv run python scripts/render_state_machines.py

`tests/test_state_machines_doc.py` fails when the committed file and the
rendering differ.

Each state gets a generated id (`s0`, ...) and its name is the label, because a raw
name can be a Mermaid keyword or contain characters Mermaid rejects. A set target (the
states a payload may choose) is drawn as an edge into one choice node per transition
(`c0`, ...), with one edge from it to each member. Its table cell lists the members in
sorted order.

A statechart (docs/statechart-engine.md) renders its composite states as nested
blocks, its regions split by `--`, its choices as choice nodes with one edge per
branch, and its deferrals as notes. Internal, ignored and refusal rows have no
edge; the row table lists every row, then come a branch table and a state table.
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from collections.abc import Iterable
from typing import Any

from dsl41.machines import MACHINES
from dsl41.state_machine import (
    COMPLETION,
    Row,
    State,
    StateMachine,
    Statechart,
    Transition,
    is_statechart,
    state_name,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "state-machines.md"

REGENERATE = "uv run python scripts/render_state_machines.py"


def _text(value: str) -> str:
    """One line, with the characters Mermaid reads as syntax replaced."""
    return " ".join(value.split()).replace(";", ",")


def edge_label(t: Transition[Any]) -> str:
    """UML form: `id trigger [guard] / effect`."""
    label = f"{t.id} {t.trigger}"
    if t.guard:
        label += f" [{t.guard}]"
    if t.effect:
        label += f" / {t.effect}"
    return _text(label)


def state_ids(machine: StateMachine[Any]) -> dict[str, str]:
    """A generated Mermaid id (`s0`, `s1`, ...) for each state name, in sorted name order.

    A raw name is never an id: Mermaid rejects `a-b` and reads `end` and `note` as
    keywords. Choice nodes are `c0`, `c1`, ... by table position, so ids cannot collide.
    """
    names = {state_name(s) for s in (*machine.states, *machine.finals)}
    if machine.initial is not None:
        names.add(state_name(machine.initial))
    for t in machine.transitions:
        names.update(state_name(s) for s in (*t.source, *t.targets()))
    return {name: f"s{index}" for index, name in enumerate(sorted(names))}


def diagram(machine: StateMachine[Any]) -> list[str]:
    ids = state_ids(machine)
    lines = ["```mermaid", "stateDiagram-v2"]
    lines += [f'    state "{name.replace(chr(34), chr(39))}" as {ids[name]}' for name in ids]
    choices = {t.id: f"c{index}" for index, t in enumerate(machine.transitions)}
    for t in machine.transitions:
        if isinstance(t.target, frozenset):
            lines.append(f"    state {choices[t.id]} <<choice>>")
    if machine.initial is not None:
        lines.append(f"    [*] --> {ids[state_name(machine.initial)]}")
    for t in machine.transitions:
        sources = sorted(state_name(s) for s in t.source)
        if isinstance(t.target, frozenset):
            node = choices[t.id]
            lines += [f"    {ids[s]} --> {node} : {edge_label(t)}" for s in sources]
            lines += [f"    {node} --> {ids[m]}" for m in sorted(state_name(m) for m in t.target)]
        else:
            target = ids[state_name(t.target)]
            lines += [f"    {ids[s]} --> {target} : {edge_label(t)}" for s in sources]
    lines += [f"    {ids[state_name(f)]} --> [*]" for f in sorted(machine.finals)]
    lines.append("```")
    return lines


def _cell(value: str) -> str:
    return _text(value).replace("|", "\\|")


def table(machine: StateMachine[Any]) -> list[str]:
    lines = [
        "| Id | Source | Trigger | Guard | Effect | Target | Cite | Mark |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in machine.transitions:
        cells = [
            t.id,
            ", ".join(sorted(state_name(s) for s in t.source)),
            t.trigger,
            t.guard,
            t.effect,
            ", ".join(sorted(state_name(s) for s in t.targets())),
            t.cite,
            t.mark or "",
        ]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return lines


def _chart_states(state: State, parent: str = "", region: str = "") -> list[tuple[State, str]]:
    """Every state with the `parent/region` it lives in, in declaration order."""
    found = [(state, f"{parent}/{region}" if parent else "")]
    for each in state.regions:
        for sub in each.states:
            found += _chart_states(sub, state.name, each.name)
    return found


def _row_label(row: Row) -> str:
    label = row.id if row.event == COMPLETION else f"{row.id} {row.event}"
    if row.guard:
        label += f" [{row.guard}]"
    if row.action:
        label += f" / {row.action}"
    return _text(label)


def _block(state: State, ids: dict[str, str], indent: str) -> list[str]:
    """A state's declaration; a composite opens a block with one part per region."""
    lines = [f'{indent}state "{state.name.replace(chr(34), chr(39))}" as {ids[state.name]}']
    if not state.regions:
        return lines
    lines.append(f"{indent}state {ids[state.name]} {{")
    for index, region in enumerate(state.regions):
        if index:
            lines.append(f"{indent}    --")
        lines.append(f"{indent}    [*] --> {ids[region.initial]}")
        for sub in region.states:
            lines += _block(sub, ids, indent + "    ")
        lines += [f"{indent}    {ids[s.name]} --> [*]" for s in region.states if s.final]
    lines.append(f"{indent}}}")
    return lines


def chart_diagram(chart: Statechart) -> list[str]:
    states = [state for state, _ in _chart_states(chart.root)]
    ids = {name: f"s{index}" for index, name in enumerate(sorted(s.name for s in states))}
    choices = {choice.name: f"c{index}" for index, choice in enumerate(chart.choices)}
    lines = ["```mermaid", "stateDiagram-v2", *_block(chart.root, ids, "    ")]
    lines += [f"    state {choices[c.name]} <<choice>>" for c in chart.choices]
    for row in chart.rows:
        if row.kind == "external":
            target = choices.get(row.target) or ids[row.target]
            lines.append(f"    {ids[row.source]} --> {target} : {_row_label(row)}")
    for choice in chart.choices:
        for branch in choice.branches:
            label = f"{branch.id} [{branch.guard or 'else'}]"
            if branch.action:
                label += f" / {branch.action}"
            lines.append(f"    {choices[choice.name]} --> {ids[branch.target]} : {_text(label)}")
    for state in states:
        if state.defer:
            deferred = ", ".join(sorted(state.defer))
            lines.append(f"    note right of {ids[state.name]} : defer {deferred}")
    lines.append("```")
    return lines


def chart_table(chart: Statechart) -> list[str]:
    """The rows, the branches and the states, each in its own table."""
    lines = [
        "| Id | Source | Event | Guard | Action | Target | Kind | Cite |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in chart.rows:
        kind = f"{row.kind} {row.code}" if row.code else row.kind
        event = row.event or "(completion)"
        action = row.action or row.note
        cells = [row.id, row.source, event, row.guard, action, row.target, kind, row.cite]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    if chart.choices:
        lines += [
            "",
            "| Id | Choice | Guard | Action | Target |",
            "| --- | --- | --- | --- | --- |",
        ]
        for choice in chart.choices:
            for branch in choice.branches:
                cells = [
                    branch.id,
                    choice.name,
                    branch.guard or "else",
                    branch.action,
                    branch.target,
                ]
                lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    lines += [
        "",
        "| State | In | Entry | Exit | Defers | Waits |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for state, where in _chart_states(chart.root):
        name = f"{state.name} (final)" if state.final else state.name
        waits = ", ".join(sorted(state.wait))
        if state.stays:
            waits += f" (stays: {state.stays})"
        cells = [name, where, state.entry, state.exit, ", ".join(sorted(state.defer)), waits]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return lines + _prose_table(chart)


def _prose_table(chart: Statechart) -> list[str]:
    """The guards and actions with their docstrings, which are the rows' prose (§14)."""
    named = [("guard", name, fn) for name, fn in chart.guards.items()]
    named += [("action", name, fn) for name, fn in chart.actions.items()]
    if not named:
        return []
    lines = ["", "| Name | Kind | Prose |", "| --- | --- | --- |"]
    for kind, name, fn in named:
        lines.append(f"| {_cell(name)} | {kind} | {_cell(inspect.getdoc(fn) or '')} |")
    return lines


def chart_summary(chart: Statechart) -> str:
    """The chart's clock, deferred queue and time events, in one line."""
    parts = [f"Clock: {chart.clock or 'undeclared'}."]
    if chart.overflow:
        parts.append(f"Deferred queue: {chart.defer_bound}, overflow {chart.overflow}.")
    if chart.time_events:
        names = ", ".join(
            f"{t.name} (generation)" if t.generation else t.name for t in chart.time_events
        )
        parts.append(f"Time events: {names}.")
    return " ".join(parts)


def render(machines: Iterable[StateMachine[Any] | Statechart]) -> str:
    """The whole file."""
    lines = [
        "# State machines",
        "",
        f"This file is generated from `dsl41.machines`; regenerate it with `{REGENERATE}`.",
    ]
    declared = list(machines)
    if not declared:
        lines += ["", "No state machines are declared yet."]
    for machine in declared:
        if is_statechart(machine):
            lines += ["", f"## {machine.name}", "", chart_summary(machine), ""]
            lines += [*chart_diagram(machine), "", *chart_table(machine)]
        else:
            lines += ["", f"## {machine.name}", "", *diagram(machine), "", *table(machine)]
    return "\n".join(lines) + "\n"


def is_current(
    root: pathlib.Path = ROOT, machines: Iterable[StateMachine[Any] | Statechart] | None = None
) -> bool:
    """True when the committed file at `root` equals the rendering."""
    doc = root / "docs" / "state-machines.md"
    expected = render(MACHINES if machines is None else machines)
    return doc.exists() and doc.read_text(encoding="utf-8") == expected


def main() -> int:
    if is_current():
        print(f"{DOC.name}: already current")
        return 0
    DOC.write_text(render(MACHINES), encoding="utf-8")
    print(f"{DOC.name}: written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
