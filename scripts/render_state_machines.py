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
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterable
from typing import Any

from dsl41.machines import MACHINES
from dsl41.state_machine import StateMachine, Transition, state_name

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


def render(machines: Iterable[StateMachine[Any]]) -> str:
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
        lines += ["", f"## {machine.name}", "", *diagram(machine), "", *table(machine)]
    return "\n".join(lines) + "\n"


def is_current(
    root: pathlib.Path = ROOT, machines: Iterable[StateMachine[Any]] | None = None
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
