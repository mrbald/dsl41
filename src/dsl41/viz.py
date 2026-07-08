"""Mermaid rendering of the derived graph (phase 6, CLAUDE.md / DL-03).

Spec (ir-design ss1 pipeline + phase list): IR-G -> Mermaid with boxes as
subgraphs, predicate-labeled edges, and a collapse threshold. Pure function
of DerivedGraph; deterministic output for identical input.

Rendering decisions (each with a test):
- Node ids are generated (n0, n1, ...) in first-need order (graph.nodes,
  then edge pseudo-sources, then mutex members); real names live in labels.
  JIL names may contain '.', '#', ':', '^' -- none are Mermaid-id-safe.
- Edge arrow encodes the E/A/R class: exact -->, assumed -.->, redesign ==>.
  Labels carry the predicate letter (s/f/d/t/n/e/v), the raw lookback token
  when present, and the mapping row for non-exact edges.
- Boxes render as subgraphs, nested boxes as nested subgraphs (SEM-17). A
  box whose DIRECT member count exceeds the collapse threshold renders as a
  single node instead; everything transitively inside anchors to it, edges
  re-anchor to the collapsed node (deduped), and intra-box edges vanish.
- Pseudo-nodes get shapes + classes: global variables [/name/] (via=global
  srcs), cross-instance producers [[name^INST]], undefined local producers
  a red-dashed class. Mutex groups render as dotted x-.-x links labeled
  'mutex' (self-exclusion as a self-link).
- Quotes in names escape to #quot; inside Mermaid string labels.
"""

from __future__ import annotations

from typing import Literal

from dsl41.derive import BoxTree, DerivedEdge, DerivedGraph

_VIA_LETTER = {
    "success": "s",
    "failure": "f",
    "done": "d",
    "terminated": "t",
    "notrunning": "n",
    "exitcode": "e",
    "global": "v",
}

_ARROW = {"exact": "-->", "assumed": "-.->", "redesign": "==>"}

Direction = Literal["LR", "TD"]

DEFAULT_COLLAPSE_THRESHOLD = 12


def _esc(name: str) -> str:
    """Escape for a double-quoted Mermaid string label."""
    return name.replace('"', "#quot;")


class _Ids:
    def __init__(self) -> None:
        self._by_name: dict[str, str] = {}

    def __call__(self, name: str) -> str:
        if name not in self._by_name:
            self._by_name[name] = f"n{len(self._by_name)}"
        return self._by_name[name]


def _edge_label(edge: DerivedEdge) -> str:
    label = _VIA_LETTER[edge.via]
    if edge.lookback is not None:
        label += f", {edge.lookback.raw}"
    if edge.cls != "exact":
        label += f" {edge.mapping_row}"
    return label


def _anchors(tree: BoxTree, threshold: int) -> tuple[dict[str, str], set[str]]:
    """(anchor map, collapsed boxes). anchor[name] == name for everything
    rendered; members (transitive) of a collapsed box anchor to that box."""
    anchor: dict[str, str] = {}
    collapsed: set[str] = set()

    def visit(box: str, enclosing: str | None) -> None:
        if enclosing is None and len(tree.children[box]) > threshold:
            collapsed.add(box)
            inside: str | None = box
        else:
            inside = enclosing
        anchor[box] = enclosing or box
        for member in tree.children[box]:
            if member in tree.children:
                visit(member, inside)
            else:
                anchor[member] = inside or member

    for root in tree.roots:
        visit(root, None)
    return anchor, collapsed


def to_mermaid(
    graph: DerivedGraph,
    *,
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction = "LR",
) -> str:
    """Render the derived graph as a Mermaid flowchart (see module docstring)."""
    ids = _Ids()
    anchor, collapsed = _anchors(graph.box_tree, collapse_threshold)

    def target(name: str) -> str:
        return anchor.get(name, name)

    def visible(name: str) -> bool:
        return target(name) == name

    lines: list[str] = [f"flowchart {direction}"]
    box_nodes: set[str] = set(graph.box_tree.children)

    def emit_box(box: str, indent: str) -> None:
        if box in collapsed:
            count = len(graph.box_tree.children[box])
            lines.append(f'{indent}{ids(box)}[["{_esc(box)} ({count} members)"]]')
            return
        lines.append(f'{indent}subgraph {ids(box)}["{_esc(box)}"]')
        for member in graph.box_tree.children[box]:
            if member in box_nodes:
                emit_box(member, indent + "    ")
            else:
                lines.append(f'{indent}    {ids(member)}["{_esc(member)}"]')
        lines.append(f"{indent}end")

    for root in graph.box_tree.roots:
        emit_box(root, "    ")
    for name in graph.nodes:
        if name not in box_nodes and graph.box_tree.parent.get(name) is None:
            lines.append(f'    {ids(name)}["{_esc(name)}"]')

    # pseudo-sources: globals, cross-instance producers, undefined locals
    global_srcs: list[str] = []
    external_srcs: list[str] = []
    undefined_srcs: list[str] = []
    for edge in graph.edges:
        if edge.src in graph.nodes:
            continue
        if edge.via == "global":
            bucket, shape = global_srcs, f'    {ids(edge.src)}[/"{_esc(edge.src)}"/]'
        elif "^" in edge.src:
            bucket, shape = external_srcs, f'    {ids(edge.src)}[["{_esc(edge.src)}"]]'
        else:
            bucket, shape = undefined_srcs, f'    {ids(edge.src)}["{_esc(edge.src)}"]'
        if edge.src not in bucket:
            bucket.append(edge.src)
            lines.append(shape)

    seen_edges: set[tuple[str, str, str, str]] = set()
    for edge in graph.edges:
        src, dst = target(edge.src), target(edge.dst)
        if src == dst and edge.src != edge.dst:
            continue  # both endpoints inside one collapsed box
        key = (src, dst, edge.cls, _edge_label(edge))
        if key in seen_edges:
            continue  # re-anchored duplicates collapse to one rendered edge
        seen_edges.add(key)
        lines.append(f'    {ids(src)} {_ARROW[edge.cls]}|"{_esc(_edge_label(edge))}"| {ids(dst)}')

    for group in graph.mutex_groups:
        a = target(group[0])
        b = target(group[-1])  # single-element group -> self-link
        lines.append(f"    {ids(a)} x-. mutex .-x {ids(b)}")

    # Edges are styled by arrow type (exact/assumed/redesign); only nodes
    # carry classes.
    def class_line(cls: str, names: list[str]) -> str | None:
        rendered = [ids(n) for n in names if visible(n) or n not in anchor]
        return f"    class {','.join(rendered)} {cls}" if rendered else None

    # (class name, classDef CSS, node names) -- one row per pseudo-node kind
    # plus collapsed boxes; collapsed's names are sorted (deterministic order
    # over a set), the others keep first-seen order from the loops above.
    style_rows: list[tuple[str, str, list[str]]] = [
        ("globalvar", "fill:#fdf6b2,stroke:#8a6d00", global_srcs),
        ("external", "fill:#e0ecff,stroke:#1d4ed8", external_srcs),
        ("undefined", "fill:#fde2e2,stroke:#b91c1c,stroke-dasharray: 4 3", undefined_srcs),
        ("collapsedBox", "fill:#ece9fd,stroke:#5b21b6", sorted(collapsed)),
    ]
    style_block: list[str] = []
    for cls, css, names in style_rows:
        if not names:
            continue
        style_block.append(f"    classDef {cls} {css}")
        line = class_line(cls, names)
        if line:
            style_block.append(line)
    lines.extend(style_block)
    return "\n".join(lines) + "\n"
