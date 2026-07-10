"""Markdown/Mermaid rendering of the derived graph (phase 6, CLAUDE.md / DL-03).

Spec (ir-design ss1 pipeline + phase list, amended DL-35): IR-G -> a Markdown
report of per-component Mermaid charts, plus appendices that materialize
everything the charts thin out or drop. Pure function of DerivedGraph;
deterministic output for identical input.

Rendering decisions (each with a test):
- Node ids are generated (n0, n1, ...) in first-need order; real names live
  in labels. JIL names may contain '.', '#', ':', '^' -- none Mermaid-id-safe.
- Visual grammar (DL-35): shape/line-style is the primary channel, color is
  redundant reinforcement, Unicode symbols not FontAwesome (hosts don't ship
  FA CSS). Command job = plain rect (unmarked default). File watcher =
  stadium + U+1F4C4 prefix (a source/trigger). Scheduled node = second label
  line with U+23F0 + trigger digest (from node_meta; run_window/must_* are
  excluded upstream). Cross-instance producer = hexagon (frees [[..]] for
  collapsed boxes exclusively). Undefined producer = U+26A0 prefix + red
  dash (two channels). Global variable = parallelogram. Every classDef
  carries an explicit color for dark-mode hosts.
- Edge arrow encodes the E/A/R class: exact -->, assumed -.->, redesign ==>
  plus a red linkStyle (edge emission order is deterministic, so indices are
  safe). Labels are thinned (DL-35): via letter only when via != success,
  lookback raw token always (semantically load-bearing), mapping row only on
  redesign edges -- assumed rows/assumptions live in Appendix B instead.
- Boxes render as subgraphs, nested boxes as nested subgraphs (SEM-17);
  subgraph TITLES stay one-line (middle-dot separators -- hosts render
  <br/> in titles inconsistently, DL-35a). A box whose DIRECT member count
  exceeds the collapse threshold renders as a single [[..]] node whose
  label counts hidden scheduled/watcher members (DL-35a); members anchor to
  it, edges re-anchor (deduped), and intra-box edges vanish.
- Mutex (DL-35): pairs render x-. lock .-x (pairs are what the JIL states,
  M07, non-transitive). A complete clique of >=3 pairwise-mutexed jobs
  renders as one shared lock node with dotted links -- k(k-1)/2 undirected
  links wreck dagre ranking; completeness is checked, so the hub never
  claims an exclusion the JIL doesn't state. Self-mutex renders as a label
  badge, not a self-loop. A member missing from the catalog (dangling n(),
  L001's finding) renders as an undefined pseudo-node in its partner's
  chart (DL-35a). The report's "Locks" section enumerates EVERY group with
  its charts, so no exclusion hides in a collapsed box or between
  workflows.
- Components (DL-35): connectivity is dependency edges between catalog jobs
  plus box co-membership. Mutex links do NOT connect components (a shared
  lock would glue unrelated streams). Pseudo-sources replicate per
  component.
- Standalone jobs (size-1 component, no edges, no mutex membership) are
  dropped from charts and enumerated in Appendix A with kind/schedule/detail
  -- the drop is loud and reversible (include_singletons), per the no-silent-
  loss discipline.
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

_REDESIGN_LINKSTYLE = "stroke:#b91c1c,stroke-width:2px"

_CLASS_DEFS = {
    "trigger": "fill:#def7ec,stroke:#046c4e,color:#111",
    "globalvar": "fill:#fdf6b2,stroke:#8a6d00,color:#111",
    "external": "fill:#e0ecff,stroke:#1d4ed8,color:#111",
    "undefined": "fill:#fde2e2,stroke:#b91c1c,color:#111,stroke-dasharray: 4 3",
    "collapsedBox": "fill:#ece9fd,stroke:#5b21b6,color:#111",
    "lockNode": "fill:#f3f4f6,stroke:#6b7280,color:#111,stroke-dasharray: 2 2",
}

_ELK_FRONTMATTER = "---\nconfig:\n  layout: elk\n---\n"


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
    """Thinned label (DL-35): letter iff via != success, lookback always,
    mapping row only on redesign edges (Appendix B carries the rest)."""
    parts: list[str] = []
    if edge.via != "success" or edge.cls == "redesign":
        parts.append(_VIA_LETTER[edge.via])
    if edge.lookback is not None:
        parts.append(edge.lookback.raw)
    label = ", ".join(parts)
    if edge.cls == "redesign":
        label = f"{label} {edge.mapping_row}" if label else edge.mapping_row
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


# ------------------------------------------------------------------- components


def _box_members(tree: BoxTree, box: str) -> set[str]:
    """Transitive membership, box included."""
    out = {box}
    for member in tree.children.get(box, []):
        out |= _box_members(tree, member) if member in tree.children else {member}
    return out


def split_components(graph: DerivedGraph) -> list[list[str]]:
    """Connected components over dependency edges between catalog jobs plus
    box co-membership; mutex and pseudo-sources do NOT connect (DL-35).
    Members in graph.nodes (catalog) order; components ordered by descending
    size, ties by first member's catalog position."""
    index = {name: i for i, name in enumerate(graph.nodes)}
    parent: dict[str, str | None] = dict.fromkeys(graph.nodes)

    def find(name: str) -> str:
        while parent[name] is not None:
            up = parent[name]
            assert up is not None
            name = up
        return name

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in graph.edges:
        if edge.src in index and edge.dst in index:
            union(edge.src, edge.dst)
    for box, members in graph.box_tree.children.items():
        for member in members:
            union(box, member)

    grouped: dict[str, list[str]] = {}
    for name in graph.nodes:
        grouped.setdefault(find(name), []).append(name)
    return sorted(grouped.values(), key=lambda comp: (-len(comp), index[comp[0]]))


def _incident_nodes(graph: DerivedGraph) -> set[str]:
    """Catalog jobs touching any edge (either end) or any mutex group."""
    touched: set[str] = set()
    for edge in graph.edges:
        touched.add(edge.src)
        touched.add(edge.dst)
    for group in graph.mutex_groups:
        touched.update(group)
    return touched


def _is_standalone(comp: list[str], touched: set[str]) -> bool:
    return len(comp) == 1 and comp[0] not in touched


def _component_title(comp: list[str], graph: DerivedGraph) -> str:
    """Top-level box name if the component has one, else the first source
    node; '(+n more)' when the name covers only part of the component."""
    comp_set = set(comp)
    roots_in = [r for r in graph.box_tree.roots if r in comp_set]
    if roots_in:
        name = roots_in[0]
        rest = len(comp) - len(_box_members(graph.box_tree, name) & comp_set)
    else:
        with_in_edges = {e.dst for e in graph.edges if e.src in comp_set}
        sources = [n for n in comp if n not in with_in_edges]
        name = sources[0] if sources else comp[0]
        rest = len(comp) - 1
    return f"{name} (+{rest} more)" if rest else name


def _common_prefix(comp: list[str]) -> str:
    """Longest common name prefix cut back to its last separator; '' unless
    it is >=4 chars and every name is strictly longer (lossless strip)."""
    if len(comp) < 2:
        return ""
    low, high = min(comp), max(comp)
    lcp = 0
    while lcp < len(low) and low[lcp] == high[lcp]:
        lcp += 1
    cut = 0
    for i in range(lcp):
        if low[i] in "_.-":
            cut = i + 1
    prefix = low[:cut]
    if len(prefix) < 4 or any(len(n) <= len(prefix) for n in comp):
        return ""
    return prefix


def _auto_direction(comp: list[str], graph: DerivedGraph) -> Direction:
    """TD when the component is wider than deep (BFS levels over local
    job->job edges), else LR. Deterministic."""
    comp_set = set(comp)
    succs: dict[str, list[str]] = {n: [] for n in comp}
    has_in: set[str] = set()
    for edge in graph.edges:
        if edge.src in comp_set and edge.dst in comp_set and edge.src != edge.dst:
            succs[edge.src].append(edge.dst)
            has_in.add(edge.dst)
    level = [n for n in comp if n not in has_in] or comp[:1]
    seen = set(level)
    depth, width = 0, 0
    while level:
        depth += 1
        width = max(width, len(level))
        following: list[str] = []
        for name in level:
            for succ in succs[name]:
                if succ not in seen:
                    seen.add(succ)
                    following.append(succ)
        level = following
    return "TD" if width > depth else "LR"


# ------------------------------------------------------------------ mutex plan


def _mutex_plan(
    graph: DerivedGraph, members: set[str] | None
) -> tuple[set[str], list[list[str]], list[tuple[str, str]]]:
    """(self-locked nodes, complete cliques >=3, remaining pairs), filtered
    to `members` when given. Cliques must be COMPLETE (all k(k-1)/2 pairs
    stated) -- the hub encoding never claims an exclusion the JIL doesn't.
    A member missing from the catalog (dangling n(), L001's finding) counts
    as in scope wherever its partner is, like edge pseudo-sources (DL-35a).
    Assumes derive's invariants: each pair sorted, groups deduped."""
    catalog = set(graph.nodes)

    def in_scope(name: str) -> bool:
        return members is None or name in members or name not in catalog

    self_locked: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for group in graph.mutex_groups:
        if len(group) == 1:
            if in_scope(group[0]):
                self_locked.add(group[0])
        else:
            a, b = group[0], group[1]
            if in_scope(a) and in_scope(b):
                pairs.append((a, b))

    adjacency: dict[str, set[str]] = {}
    for a, b in pairs:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    cliques: list[list[str]] = []
    clique_pairs: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack: list[str] = [start]
        connected: set[str] = {start}
        while stack:
            for other in adjacency[stack.pop()]:
                if other not in connected:
                    connected.add(other)
                    stack.append(other)
        seen |= connected
        size = len(connected)
        stated = sum(1 for a, b in pairs if a in connected and b in connected)
        if size >= 3 and stated == size * (size - 1) // 2:
            clique = sorted(connected)
            cliques.append(clique)
            clique_pairs.update((a, b) for i, a in enumerate(clique) for b in clique[i + 1 :])
    remaining = [p for p in pairs if p not in clique_pairs]
    return self_locked, cliques, remaining


# ---------------------------------------------------------------- chart render


def _node_label(
    name: str,
    graph: DerivedGraph,
    strip: str,
    self_locked: set[str],
    *,
    multiline: bool = True,
) -> str:
    """multiline=False for subgraph titles: hosts render <br/> in titles
    inconsistently (GitHub), so expanded boxes get a one-line separator."""
    sep = "<br/>" if multiline else " \N{MIDDLE DOT} "
    meta = graph.node_meta.get(name)
    display = name[len(strip) :] if strip and name.startswith(strip) else name
    label = _esc(display)
    if meta is not None and meta.kind == "FW":
        label = f"\N{PAGE FACING UP} {label}"
    if meta is not None and meta.schedule is not None:
        label += f"{sep}\N{ALARM CLOCK} {_esc(meta.schedule)}"
    if name in self_locked:
        label += f"{sep}\N{LOCK} single-instance"
    return label


def _render_chart(
    graph: DerivedGraph,
    members: set[str] | None,
    *,
    collapse_threshold: int,
    direction: Direction,
    strip_prefix: str = "",
) -> str:
    """One Mermaid flowchart body: the whole graph (members=None) or one
    component. Pseudo-sources and mutex render only where their consumers/
    holders are."""
    ids = _Ids()
    anchor, collapsed = _anchors(graph.box_tree, collapse_threshold)
    self_locked, cliques, mutex_pairs = _mutex_plan(graph, members)

    def in_scope(name: str) -> bool:
        return members is None or name in members

    def target(name: str) -> str:
        return anchor.get(name, name)

    def visible(name: str) -> bool:
        return target(name) == name

    def label(name: str) -> str:
        return _node_label(name, graph, strip_prefix, self_locked)

    lines: list[str] = [f"flowchart {direction}"]
    box_nodes: set[str] = set(graph.box_tree.children)
    trigger_nodes: list[str] = []

    def emit_node(name: str, indent: str) -> None:
        meta = graph.node_meta.get(name)
        if meta is not None and meta.kind == "FW":
            trigger_nodes.append(name)
            lines.append(f'{indent}{ids(name)}(["{label(name)}"])')
        else:
            lines.append(f'{indent}{ids(name)}["{label(name)}"]')

    def emit_box(box: str, indent: str) -> None:
        if box in collapsed:
            # hidden trigger facts stay loud on the folded node (DL-35a)
            inside = _box_members(graph.box_tree, box) - {box}
            metas = [m for n in inside if (m := graph.node_meta.get(n)) is not None]
            extras = f"{len(graph.box_tree.children[box])} members"
            if scheduled := sum(1 for m in metas if m.schedule is not None):
                extras += f", {scheduled} \N{ALARM CLOCK}"
            if watchers := sum(1 for m in metas if m.kind == "FW"):
                extras += f", {watchers} \N{PAGE FACING UP}"
            lines.append(f'{indent}{ids(box)}[["{label(box)} ({extras})"]]')
            return
        title = _node_label(box, graph, strip_prefix, self_locked, multiline=False)
        lines.append(f'{indent}subgraph {ids(box)}["{title}"]')
        for member in graph.box_tree.children[box]:
            if member in box_nodes:
                emit_box(member, indent + "    ")
            else:
                emit_node(member, indent + "    ")
        lines.append(f"{indent}end")

    for root in graph.box_tree.roots:
        if in_scope(root):
            emit_box(root, "    ")
    for name in graph.nodes:
        if name not in box_nodes and graph.box_tree.parent.get(name) is None and in_scope(name):
            emit_node(name, "    ")

    # pseudo-sources: globals, cross-instance producers, undefined locals
    global_srcs: list[str] = []
    external_srcs: list[str] = []
    undefined_srcs: list[str] = []
    for edge in graph.edges:
        if edge.src in graph.nodes or not in_scope(edge.dst):
            continue
        if edge.via == "global":
            bucket, shape = global_srcs, f'    {ids(edge.src)}[/"{_esc(edge.src)}"/]'
        elif "^" in edge.src:
            bucket, shape = external_srcs, f'    {ids(edge.src)}{{{{"{_esc(edge.src)}"}}}}'
        else:
            bucket, shape = (
                undefined_srcs,
                f'    {ids(edge.src)}["\N{WARNING SIGN} {_esc(edge.src)}"]',
            )
        if edge.src not in bucket:
            bucket.append(edge.src)
            lines.append(shape)

    # dangling n() members (L001's finding) render like undefined producers
    lock_members = [n for pair in mutex_pairs for n in pair]
    lock_members += [n for clique in cliques for n in clique]
    for name in lock_members:
        if name not in graph.nodes and name not in undefined_srcs:
            undefined_srcs.append(name)
            lines.append(f'    {ids(name)}["\N{WARNING SIGN} {_esc(name)}"]')

    link_index = 0
    redesign_links: list[int] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    for edge in graph.edges:
        if not in_scope(edge.dst):
            continue
        src, dst = target(edge.src), target(edge.dst)
        if src == dst and edge.src != edge.dst:
            continue  # both endpoints inside one collapsed box
        text = _edge_label(edge)
        key = (src, dst, edge.cls, text)
        if key in seen_edges:
            continue  # re-anchored duplicates collapse to one rendered edge
        seen_edges.add(key)
        arrow = _ARROW[edge.cls]
        if text:
            lines.append(f'    {ids(src)} {arrow}|"{_esc(text)}"| {ids(dst)}')
        else:
            lines.append(f"    {ids(src)} {arrow} {ids(dst)}")
        if edge.cls == "redesign":
            redesign_links.append(link_index)
        link_index += 1

    seen_locks: set[tuple[str, str]] = set()
    for a, b in mutex_pairs:
        at, bt = target(a), target(b)
        if (at == bt and a != b) or (at, bt) in seen_locks:
            continue  # exclusion internal to a collapsed box / re-anchored dup
        seen_locks.add((at, bt))
        lines.append(f"    {ids(at)} x-. lock .-x {ids(bt)}")
        link_index += 1
    lock_ids: list[str] = []
    for clique in cliques:
        lock = f"lock:{'+'.join(clique)}"
        lock_ids.append(ids(lock))
        lines.append(f'    {ids(lock)}(("\N{LOCK}"))')
        for member_anchor in dict.fromkeys(target(m) for m in clique):
            lines.append(f"    {ids(lock)} -.- {ids(member_anchor)}")
            link_index += 1

    if redesign_links:
        joined = ",".join(str(i) for i in redesign_links)
        lines.append(f"    linkStyle {joined} {_REDESIGN_LINKSTYLE}")

    def class_line(cls: str, names: list[str]) -> str | None:
        rendered = [ids(n) for n in names if visible(n) or n not in anchor]
        return f"    class {','.join(rendered)} {cls}" if rendered else None

    # (class name, node names) -- one row per node kind; collapsed's names
    # are sorted (deterministic order over a set), the others keep
    # first-seen order from the loops above.
    style_rows: list[tuple[str, list[str]]] = [
        ("trigger", [n for n in trigger_nodes if visible(n)]),
        ("globalvar", global_srcs),
        ("external", external_srcs),
        ("undefined", undefined_srcs),
        ("collapsedBox", sorted(c for c in collapsed if in_scope(c))),
    ]
    style_block: list[str] = []
    for cls, names in style_rows:
        if not names:
            continue
        style_block.append(f"    classDef {cls} {_CLASS_DEFS[cls]}")
        line = class_line(cls, names)
        if line:
            style_block.append(line)
    if lock_ids:
        style_block.append(f"    classDef lockNode {_CLASS_DEFS['lockNode']}")
        style_block.append(f"    class {','.join(lock_ids)} lockNode")
    lines.extend(style_block)
    return "\n".join(lines) + "\n"


def to_mermaid(
    graph: DerivedGraph,
    *,
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction = "LR",
) -> str:
    """Render the whole derived graph as one Mermaid flowchart body."""
    return _render_chart(graph, None, collapse_threshold=collapse_threshold, direction=direction)


# ------------------------------------------------------------- markdown report

_LEGEND = """\
<details>
<summary>Legend</summary>

```mermaid
flowchart LR
    cmd["command job"] --> dep1["exact dependency"]
    fw(["\N{PAGE FACING UP} file watcher"]) -.-> dep2["assumed dependency"]
    sched["scheduled job<br/>\N{ALARM CLOCK} 06:00 mo"] ==> dep3["redesign-needed (M-row)"]
    ext{{"producer^INST"}} ~~~ gv[/"GLOBAL_VAR"/]
    und["\N{WARNING SIGN} undefined producer"] ~~~ cbox[["collapsed box (n members)"]]
    la["job A"] x-. lock .-x lb["job B"]
    lk(("\N{LOCK}")) -.- la
    lk -.- lb
    linkStyle 2 stroke:#b91c1c,stroke-width:2px
    classDef trigger fill:#def7ec,stroke:#046c4e,color:#111
    class fw trigger
    classDef globalvar fill:#fdf6b2,stroke:#8a6d00,color:#111
    class gv globalvar
    classDef external fill:#e0ecff,stroke:#1d4ed8,color:#111
    class ext external
    classDef undefined fill:#fde2e2,stroke:#b91c1c,color:#111,stroke-dasharray: 4 3
    class und undefined
    classDef collapsedBox fill:#ece9fd,stroke:#5b21b6,color:#111
    class cbox collapsedBox
    classDef lockNode fill:#f3f4f6,stroke:#6b7280,color:#111,stroke-dasharray: 2 2
    class lk lockNode
```

Solid arrow = exact mapping; dashed = assumed (assumption in Appendix B);
thick red = needs redesign (M-row on the edge). Edge letters: f failure,
d done, t terminated, n notrunning, e exitcode, v global variable;
unmarked = success. `(HH:MM)` etc. are lookback qualifiers. `lock` links
and \N{LOCK} hubs are mutual exclusion, not flow.

</details>
"""


def _cell(text: str | None) -> str:
    """Markdown table cell: escape pipes and newlines (never truncate --
    assumptions/reasons appear nowhere else in the report)."""
    if text is None:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")


def _code_cell(text: str | None) -> str:
    """Command/path cell: code span, truncated -- full text is IR-F's, not
    the report's, responsibility."""
    if not text:
        return ""
    flat = _cell(text.replace("`", "'"))
    if len(flat) > 60:
        flat = flat[:59] + "\N{HORIZONTAL ELLIPSIS}"
    return f"`{flat}`"


def to_markdown(
    graph: DerivedGraph,
    *,
    title: str = "catalog",
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction | Literal["auto"] = "auto",
    include_singletons: bool = False,
    elk: bool = False,
) -> str:
    """Full Markdown report: summary, legend, one chart per component,
    shared-locks section, appendices A (standalone jobs) / B (non-exact
    edges) / C (redesign flags, OR shapes, cycles)."""
    components = split_components(graph)
    touched = _incident_nodes(graph)
    standalone = [comp[0] for comp in components if _is_standalone(comp, touched)]
    charted = [comp for comp in components if not _is_standalone(comp, touched)]

    def fence(body: str) -> list[str]:
        return ["```mermaid", (_ELK_FRONTMATTER if elk else "") + body.rstrip("\n"), "```", ""]

    by_class = {"exact": 0, "assumed": 0, "redesign": 0}
    for edge in graph.edges:
        by_class[edge.cls] += 1
    summary = (
        f"{len(graph.nodes)} jobs \N{MIDDLE DOT} {len(graph.edges)} edges"
        f" ({by_class['exact']} exact, {by_class['assumed']} assumed,"
        f" {by_class['redesign']} redesign)"
        f" \N{MIDDLE DOT} {len(charted)} workflows"
        f" \N{MIDDLE DOT} {len(standalone)} standalone jobs"
        + ("" if include_singletons else " (Appendix A, not charted)")
        + f" \N{MIDDLE DOT} {len(graph.mutex_groups)} locks"
    )

    out: list[str] = [f"# Workflow graph: {title}", "", summary, "", _LEGEND]

    comp_of: dict[str, str] = {}
    for i, comp in enumerate(charted, start=1):
        wid = f"W{i}"
        for name in comp:
            comp_of[name] = wid
        comp_title = _component_title(comp, graph)
        prefix = _common_prefix(comp)
        count = f"{len(comp)} job" + ("s" if len(comp) != 1 else "")
        heading = f"## {wid} \N{EM DASH} {comp_title} ({count})"
        out.append(heading)
        if prefix:
            out.append("")
            out.append(f"All names share the prefix `{prefix}` (stripped in the chart).")
        out.append("")
        chart_dir = _auto_direction(comp, graph) if direction == "auto" else direction
        body = _render_chart(
            graph,
            set(comp),
            collapse_threshold=collapse_threshold,
            direction=chart_dir,
            strip_prefix=prefix,
        )
        out.extend(fence(body))

    if include_singletons and standalone:
        out.append(f"## Standalone jobs ({len(standalone)})")
        out.append("")
        sub = DerivedGraph(nodes=standalone, node_meta=graph.node_meta)
        out.extend(
            fence(_render_chart(sub, None, collapse_threshold=collapse_threshold, direction="LR"))
        )

    if graph.mutex_groups:
        out.append("## Locks")
        out.append("")
        out.append(
            "Every stated mutual exclusion. Drawn in charts as lock links, hubs,"
            " or single-instance badges; enumerated here so none hides in a"
            " collapsed box or between workflows (DL-35a)."
        )
        out.append("")
        out.append("| lock | kind | charts |")
        out.append("|---|---|---|")
        for group in graph.mutex_groups:
            kind = "self" if len(group) == 1 else "pair"
            charts = ", ".join(dict.fromkeys(comp_of.get(m, "not in catalog") for m in group))
            joined = " \N{MULTIPLICATION SIGN} ".join(group)
            out.append(f"| {_cell(joined)} | {kind} | {charts} |")
        out.append("")

    out.append("## Appendix A \N{EM DASH} standalone jobs (not part of any workflow)")
    out.append("")
    if standalone:
        out.append("| job | kind | schedule | command / watched file |")
        out.append("|---|---|---|---|")
        for name in standalone:
            meta = graph.node_meta.get(name)
            out.append(
                f"| {_cell(name)} | {_cell(meta.kind if meta else None)}"
                f" | {_cell(meta.schedule if meta else None)}"
                f" | {_code_cell(meta.detail if meta else None)} |"
            )
    else:
        out.append("None.")
    out.append("")

    annotated = [e for e in graph.edges if e.cls != "exact"]
    out.append("## Appendix B \N{EM DASH} edge annotations")
    out.append("")
    if annotated:
        out.append("| producer | consumer | via | lookback | class | row | assumption |")
        out.append("|---|---|---|---|---|---|---|")
        for e in annotated:
            lookback = e.lookback.raw if e.lookback is not None else ""
            out.append(
                f"| {_cell(e.src)} | {_cell(e.dst)} | {e.via} | {_cell(lookback)}"
                f" | {e.cls} | {e.mapping_row} | {_cell(e.assumption)} |"
            )
    else:
        out.append("None \N{EM DASH} every edge maps exactly.")
    out.append("")

    has_c = graph.redesign_flags or graph.or_shapes or graph.cycles
    out.append("## Appendix C \N{EM DASH} redesign flags, OR shapes, cycles")
    out.append("")
    if not has_c:
        out.append("None.")
        out.append("")
    if graph.redesign_flags:
        out.append("### Redesign flags")
        out.append("")
        out.append("| job | row | reason |")
        out.append("|---|---|---|")
        for flag in graph.redesign_flags:
            out.append(f"| {_cell(flag.job)} | {flag.mapping_row} | {_cell(flag.reason)} |")
        out.append("")
    if graph.or_shapes:
        out.append("### OR shapes (M12)")
        out.append("")
        out.append("| job | attr | kind | suggested lowering |")
        out.append("|---|---|---|---|")
        for shape in graph.or_shapes:
            out.append(
                f"| {_cell(shape.job)} | {shape.attr} | {shape.kind} | {_cell(shape.lowering)} |"
            )
        out.append("")
    if graph.cycles:
        out.append("### Cycles (L010)")
        out.append("")
        for cycle in graph.cycles:
            out.append(f"- {' \N{RIGHTWARDS ARROW} '.join(cycle)}")
        out.append("")

    return "\n".join(out)
