"""The block cards index every card and every declared state machine.

docs/blocks/README.md lists each card in its "Cards" section. Its
"Machines" table names, once for each machine in `dsl41.machines.MACHINES`,
the cards that explain it. A card named there links that machine's heading
in docs/state-machines.md, which holds the generated diagram and transition
table, and has no Mermaid block of its own. No page under docs/blocks/
draws a state diagram: one appears only where a declared, tested machine
backs it, and those are generated. A card without a machine may draw a
flowchart. A card is at most 120 lines. Whether a card's prose matches the
table it links is checked in review, not here.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_docs_hygiene import ROOT
from test_docs_links import anchors, prose, targets

from dsl41.machines import MACHINES

BLOCKS = ROOT / "docs" / "blocks"
MAX_LINES = 120
MACHINE_LINK = re.compile(r"^\.\./state-machines\.md#(\w+)$")
CARD_LINK = re.compile(r"^([\w-]+\.md)$")
#: A fenced block whose info string is `mermaid`, with either fence
#: character and any spacing before the word.
MERMAID_FENCE = re.compile(r"^[ \t]*(?:```|~~~)[ \t]*mermaid\b", re.MULTILINE)


def machine_links(text: str) -> set[str]:
    """The machine anchors a card's prose links."""
    return {m.group(1) for t in targets(text) if (m := MACHINE_LINK.match(t))}


def section(text: str, heading: str) -> str:
    """The body of the level-2 section `heading`, up to the next one."""
    return text.partition(f"\n## {heading}\n")[2].partition("\n## ")[0]


def machine_rows(readme: str) -> list[tuple[str, set[str]]]:
    """The README's "Machines" table, row by row: a machine and its cards."""
    rows: list[tuple[str, set[str]]] = []
    for line in section(readme, "Machines").splitlines():
        if not line.startswith("| [") or "|" not in line[1:]:
            continue
        first, _, rest = line[1:].partition("|")
        cards = {m.group(1) for t in targets(rest) if (m := CARD_LINK.match(t))}
        rows += [(name, cards) for name in sorted(machine_links(first))]
    return rows


def machine_index(readme: str) -> dict[str, set[str]]:
    """The "Machines" table as a map from each machine to its cards."""
    return dict(machine_rows(readme))


def has_mermaid(text: str) -> bool:
    return MERMAID_FENCE.search(text) is not None


def card_problems(blocks: Path, machines: set[str]) -> list[str]:
    """Every way the cards under `blocks` break the module docstring's rules."""
    readme_path = blocks / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    cards = sorted(p for p in blocks.glob("*.md") if p != readme_path)
    listed = set(targets(section(prose(readme_path), "Cards")))
    headings = anchors(blocks.parent / "state-machines.md")
    problems: list[str] = []
    for page in sorted(blocks.glob("*.md")):
        if "stateDiagram" in page.read_text(encoding="utf-8"):
            problems.append(f"{page.name}: draws a state diagram")
    for card in cards:
        if card.name not in listed:
            problems.append(f"{card.name}: not listed in README.md")
        lines = len(card.read_text(encoding="utf-8").splitlines())
        if lines > MAX_LINES:
            problems.append(f"{card.name}: {lines} lines, more than {MAX_LINES}")
    rows = machine_rows(readme)
    names = [name for name, _ in rows]
    problems += [f"{name}: indexed twice" for name in sorted(set(names)) if names.count(name) > 1]
    index = dict(rows)
    problems += [
        f"{name}: not in the README's machine index" for name in sorted(machines - set(index))
    ]
    problems += [f"{name}: indexed, not declared" for name in sorted(set(index) - machines)]
    for name, named in sorted(index.items()):
        if name not in headings:
            problems.append(f"{name}: no heading in state-machines.md")
        if not named:
            problems.append(f"{name}: no card named in the index")
        for card_name in sorted(named):
            card = blocks / card_name
            if not card.exists():
                problems.append(f"{name}: {card_name} does not exist")
                continue
            if name not in machine_links(prose(card)):
                problems.append(f"{name}: {card_name} does not link it")
            if has_mermaid(card.read_text(encoding="utf-8")):
                problems.append(f"{name}: {card_name} draws a diagram of its own")
    return problems


def test_every_card_and_every_machine_is_indexed_and_linked() -> None:
    assert len(MACHINES) >= 14
    assert card_problems(BLOCKS, {m.name for m in MACHINES}) == []


def test_a_mermaid_block_is_found_with_either_fence_and_any_spacing() -> None:
    assert has_mermaid("x\n```mermaid\nflowchart TD\n```\n")
    assert has_mermaid("x\n``` mermaid\n```\n")
    assert has_mermaid("x\n~~~mermaid\n~~~\n")
    assert not has_mermaid("x\n```python\nmermaid = 1\n```\nthe word mermaid\n")


def _fixture(tmp_path: Path, cards: dict[str, str], readme: str) -> Path:
    (tmp_path / "state-machines.md").write_text(
        "# State machines\n\n## one\n\n## two\n\n## five\n\n## six\n", encoding="utf-8"
    )
    blocks = tmp_path / "blocks"
    blocks.mkdir()
    (blocks / "README.md").write_text(readme, encoding="utf-8")
    for name, text in cards.items():
        (blocks / name).write_text(text, encoding="utf-8")
    return blocks


INDEX = (
    "# Block cards\n\nThe shape names [B](b.md) and [C](c.md) as examples.\n\n"
    "## Cards\n\n- [A](a.md)\n- [B](b.md)\n\n## Machines\n\n"
    "| Machine | Card |\n| --- | --- |\n"
    "| [one](../state-machines.md#one) | [A](a.md) |\n"
    "| [two](../state-machines.md#two) | [A](a.md); [B](b.md) |\n"
)


def test_cards_that_index_and_link_every_machine_report_nothing(tmp_path: Path) -> None:
    linked = "[one](../state-machines.md#one) and [two](../state-machines.md#two)\n"
    blocks = _fixture(
        tmp_path,
        {
            "a.md": linked,
            "b.md": "[two](../state-machines.md#two)\n",
            "c.md": "```mermaid\nflowchart TD\n    a --> b\n```\n",
        },
        INDEX.replace("- [B](b.md)\n", "- [B](b.md)\n- [C](c.md)\n"),
    )
    assert card_problems(blocks, {"one", "two"}) == []
    assert machine_index(INDEX) == {"one": {"a.md"}, "two": {"a.md", "b.md"}}


def test_each_broken_rule_is_reported(tmp_path: Path) -> None:
    """b.md and c.md are linked above the "Cards" section, which does not
    list them."""
    readme = INDEX.replace("- [B](b.md)\n", "").replace(
        "| [two]",
        "| [three](../state-machines.md#three) | [A](a.md) |\n"
        "| [five](../state-machines.md#five) | [Z](z.md) |\n"
        "| [six](../state-machines.md#six) | none |\n"
        "| [one](../state-machines.md#one) | [B](b.md) |\n"
        "| [two]",
    )
    blocks = _fixture(
        tmp_path,
        {
            "a.md": "[one](../state-machines.md#one)\n`[two](../state-machines.md#two)`\n"
            "~~~mermaid\nflowchart TD\n~~~\n",
            "b.md": "[two](../state-machines.md#two)\n\n``` mermaid\nflowchart TD\n```\n",
            "c.md": "```mermaid\nstateDiagram-v2\n```\n" + "x\n" * MAX_LINES,
        },
        readme,
    )
    assert card_problems(blocks, {"one", "two", "four", "five", "six"}) == [
        "c.md: draws a state diagram",
        "b.md: not listed in README.md",
        "c.md: not listed in README.md",
        f"c.md: {MAX_LINES + 3} lines, more than {MAX_LINES}",
        "one: indexed twice",
        "four: not in the README's machine index",
        "three: indexed, not declared",
        "five: z.md does not exist",
        "one: b.md does not link it",
        "one: b.md draws a diagram of its own",
        "six: no card named in the index",
        "three: no heading in state-machines.md",
        "three: a.md does not link it",
        "three: a.md draws a diagram of its own",
        "two: a.md does not link it",
        "two: a.md draws a diagram of its own",
        "two: b.md draws a diagram of its own",
    ]
