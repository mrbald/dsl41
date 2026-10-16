"""Markdown/Mermaid rendering of the derived graph (DL-03, DL-35, DL-73, ir-design ss1).

IR-G becomes a Markdown report of per-component Mermaid charts plus appendices that list
everything the charts thin out or drop. The output is a pure function of (catalog, graph):
IR-G for the analysis, IR-F for the per-node display facts (DL-73).

- Node ids are generated (n0, n1, ...) in first-need order; real names live in labels,
  because JIL names may contain '.', '#', ':', '^'.
- Shape and line style are the primary channel, colour is reinforcement, symbols are Unicode
  (DL-35). Command job: plain rect. File watcher: stadium with U+1F4C4. Scheduled node: a
  second label line with U+23F0 and the trigger digest (trigger fields only: run_window is a
  gate, SEM-33; must_* are alarms, SEM-34). Cross-instance producer: hexagon. Undefined
  producer: U+26A0 and a red dash. Global variable: parallelogram. Every classDef sets an
  explicit colour for dark-mode hosts.
- The arrow encodes the E/A/R class: exact -->, assumed -.->, redesign ==> with a red
  linkStyle. Labels are thinned (DL-35): the via letter only when via != success, the
  lookback always, the mapping row only on redesign edges; assumed rows live in Appendix B.
- Boxes are subgraphs, nested boxes nested subgraphs (SEM-17); titles stay one line (DL-35a).
  A box whose direct member count exceeds the collapse threshold is one [[..]] node whose
  label counts hidden scheduled and watcher members (DL-35a); edges re-anchor, deduped, and
  intra-box edges vanish.
- Mutex pairs render x-. lock .-x (M07, non-transitive). A complete clique of >=3 renders as
  one shared lock node; completeness is checked, so the hub never claims an exclusion the
  JIL does not state (DL-35). Self-mutex is a label badge. A member missing from the catalog
  (L001) renders as an undefined pseudo-node (DL-35a). The "Locks" section lists every group.
- Components are dependency edges between catalog jobs plus box co-membership. Mutex links
  do not connect components; pseudo-sources replicate per component (DL-35).
- Standalone jobs (size-1 component, no edges, no mutex) are dropped from charts and listed
  in Appendix A; `include_singletons` restores them (no silent loss).
- Quotes in names escape to #quot; inside Mermaid string labels.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from dsl41.conditions import STATUS_LETTER, GlobalAtom
from dsl41.derive import (
    BoxTree,
    DerivedEdge,
    DerivedGraph,
    components,
    derive_graph,
    local_producer,
)
from dsl41.ir import CatalogIR, FwSpec, JobIR, ScheduleBlock

#: Via -> chart letter. The status half is conditions.STATUS_LETTER under the lowercase Via
#: spelling (DL-72); only the two non-status vias are spelled out here.
_VIA_LETTER = {
    **{status.lower(): letter for status, letter in STATUS_LETTER.items()},
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


def _frontmatter(*, elk: bool, fixed_scale: bool) -> str:
    """One merged per-chart YAML frontmatter block; `layout:` precedes `flowchart:`."""
    if not (elk or fixed_scale):
        return ""
    lines = ["---", "config:"]
    if elk:
        lines.append("  layout: elk")
    if fixed_scale:
        lines.append("  flowchart:")
        lines.append("    useMaxWidth: false")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _esc(name: str) -> str:
    """Escape for a double-quoted Mermaid string label."""
    return name.replace('"', "#quot;")


#: `_Ids` key: a catalog job's own name (`str`) or a pseudo-source's namespaced key (a
#: `(str, str)` tuple, `_pseudo_key`). A `str` never equals a 2-tuple, so the key spaces
#: cannot collide whatever characters a job name holds (DL-175).
IdKey = str | tuple[str, str]


class _Ids:
    def __init__(self) -> None:
        self._by_key: dict[IdKey, str] = {}

    def __call__(self, key: IdKey) -> str:
        if key not in self._by_key:
            self._by_key[key] = f"n{len(self._by_key)}"
        return self._by_key[key]


def _pseudo_key(name: str) -> IdKey:
    """`_Ids` key for a pseudo-source, namespaced by type from a job's own key (DL-175).

    A cross-instance display form (`name^INST`) can spell like a local job (DL-162a)."""
    return ("pseudo", name)


def edge_label(edge: DerivedEdge) -> str:
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


# ------------------------------------------------------------ node display facts
#
# Display facts (DL-35) are read straight off IR-F by whichever emitter needs them (DL-73).
# Kind is JobIR.job_type; detail is the command for CMD jobs and the watched path for FW
# jobs; the schedule digest covers derive._TRIGGER_FIELDS only (SEM-33, SEM-34).
# job_kind, job_schedule and job_detail are public like report_content and edge_label
# (DL-72): the three emitters share them.


def _schedule_digest(schedule: ScheduleBlock) -> str:
    """Human one-liner over the same trigger fields as derive._trigger_signature."""
    parts: list[str] = []
    if schedule.start_times is not None:
        parts.append(",".join(f"{t.hour:02d}:{t.minute:02d}" for t in schedule.start_times))
    if schedule.start_mins is not None:
        parts.append(",".join(f":{m:02d}" for m in schedule.start_mins))
    if schedule.days_of_week is not None:
        parts.append(",".join(schedule.days_of_week))
    if schedule.run_calendar is not None:
        parts.append(f"cal {schedule.run_calendar}")
    if schedule.exclude_calendar is not None:
        parts.append(f"excl {schedule.exclude_calendar}")
    if schedule.timezone is not None:
        parts.append(schedule.timezone)
    return " ".join(parts) if parts else "scheduled"


def job_kind(job: JobIR | None) -> str | None:
    """The job type, or None when the name is not a catalog job (a pseudo-source)."""
    return job.job_type if job is not None else None


def job_schedule(job: JobIR | None) -> str | None:
    """Trigger digest, or None when the job carries no schedule block."""
    return None if job is None or job.schedule is None else _schedule_digest(job.schedule)


def job_detail(job: JobIR | None) -> str | None:
    """Command for CMD jobs, watched path for FW jobs, None for boxes."""
    if job is None:
        return None
    if isinstance(job.exec_, FwSpec):
        return job.exec_.watch_file
    return job.exec_.command if job.exec_ is not None else None


def _anchors(tree: BoxTree, threshold: int) -> tuple[dict[str, str], set[str]]:
    """(anchor map, collapsed boxes); members of a collapsed box anchor to that box."""
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


def _local_graph(catalog: CatalogIR, graph: DerivedGraph) -> DerivedGraph:
    """`graph` with each edge `src` resolved to its real local producer, else a sentinel.

    `derive.components` reads `edge.src in nodes` and is sound only for a real catalog job
    (DL-162a), and a cross-instance display form (`name^INST`) can spell like a local job.
    `report_content` passes this copy to the helpers that ask whether a catalog job reaches
    another. `dst` is untouched, so a job with only a foreign predecessor stays touched.
    The sentinel is checked against this catalog's finite job-name set (DL-175).
    `_render_chart` keeps the real graph to draw the foreign producer."""
    sentinel = "\x00pseudo"
    while sentinel in catalog.jobs:
        sentinel += "\x00"
    resolved = [
        e if local_producer(e, catalog) is not None else e.model_copy(update={"src": sentinel})
        for e in graph.edges
    ]
    return graph.model_copy(update={"edges": resolved})


def split_components(graph: DerivedGraph) -> list[list[str]]:
    """Connected components over dependency edges plus box co-membership (DL-35).

    Mutex and pseudo-sources do not connect. Members follow graph.nodes order; components
    sort by descending size, then first member's catalog position. Generic over
    `graph.edges` (DL-162a): the production caller passes a `_local_graph` copy, else a
    foreign display form spelling a local job would union unrelated jobs (DL-175)."""
    index = {name: i for i, name in enumerate(graph.nodes)}
    grouped = components(graph.nodes, graph.edges, bind_box_members=graph.box_tree)
    return sorted(grouped, key=lambda comp: (-len(comp), index[comp[0]]))


def _incident_nodes(graph: DerivedGraph) -> set[str]:
    """Catalog jobs touching any edge (either end) or any mutex group.

    Takes a `_local_graph` copy from `report_content` (DL-175)."""
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
    """Top-level box name if the component has one, else the first source node.

    '(+n more)' marks a name that covers only part of the component. Takes a `_local_graph`
    copy (DL-175)."""
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
    """TD when the component is wider than deep (BFS levels over local edges), else LR.

    Takes a `_local_graph` copy so a foreign display form cannot add a false edge (DL-175)."""
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


def mutex_plan(
    graph: DerivedGraph, members: set[str] | None
) -> tuple[set[str], list[list[str]], list[tuple[str, str]]]:
    """(self-locked nodes, complete cliques >=3, remaining pairs), filtered to `members`.

    A clique must be complete (all k(k-1)/2 pairs stated), so the hub never claims an
    exclusion the JIL does not (DL-35). Public so the explore page decides completeness
    the same way (DL-192). A member missing from the catalog (L001) is in scope wherever its
    partner is (DL-35a). Assumes each pair sorted and groups deduped, as derive gives them."""
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


#: Label-line budget: Mermaid wraps HTML labels at ~200px and the wrapped text overflows
#: stadium shapes (GitHub renderer), so the label owns its breaks via no-break spaces.
_SCHEDULE_LINE_BUDGET = 28

_NBSP = "\N{NO-BREAK SPACE}"  # survives htmlLabels; degrades to a space in SVG text


def _schedule_label_lines(digest: str) -> list[str]:
    """Non-breaking label lines from a schedule digest, packed under the budget.

    Lines break only at part boundaries; an oversized part stays whole."""
    fused = _esc(digest).replace("cal ", f"cal{_NBSP}").replace("excl ", f"excl{_NBSP}")
    lines: list[str] = []
    for part in fused.split(" "):
        packed = f"{lines[-1]} {part}" if lines else part
        if lines and len(packed.replace(_NBSP, " ")) <= _SCHEDULE_LINE_BUDGET:
            lines[-1] += f"{_NBSP}{part}"
        else:
            lines.append(part)
    return lines


def _node_label(
    name: str,
    catalog: CatalogIR,
    strip: str,
    self_locked: set[str],
    *,
    multiline: bool = True,
) -> str:
    """The node label; multiline=False gives a one-line separator for subgraph titles."""
    sep = "<br/>" if multiline else " \N{MIDDLE DOT} "
    job = catalog.jobs.get(name)
    display = name[len(strip) :] if strip and name.startswith(strip) else name
    label = _esc(display)
    if job_kind(job) == "FW":
        label = f"\N{PAGE FACING UP}{_NBSP}{label}"
    if (digest := job_schedule(job)) is not None:
        first, *rest = _schedule_label_lines(digest)
        label += f"{sep}\N{ALARM CLOCK}{_NBSP}{first}"
        for line in rest:
            label += f"{sep}{line}"
    if name in self_locked:
        label += f"{sep}\N{LOCK}{_NBSP}single-instance"
    return label


def _render_chart(
    catalog: CatalogIR,
    graph: DerivedGraph,
    members: set[str] | None,
    *,
    collapse_threshold: int,
    direction: Direction,
    strip_prefix: str = "",
) -> str:
    """One Mermaid flowchart body: the whole graph (members=None) or one component."""
    ids = _Ids()
    anchor, collapsed = _anchors(graph.box_tree, collapse_threshold)
    self_locked, cliques, mutex_pairs = mutex_plan(graph, members)

    def in_scope(name: str) -> bool:
        return members is None or name in members

    def target(name: str) -> str:
        return anchor.get(name, name)

    def visible(name: str) -> bool:
        return target(name) == name

    def label(name: str) -> str:
        return _node_label(name, catalog, strip_prefix, self_locked)

    def src_key(edge: DerivedEdge) -> IdKey:
        """`_Ids` key for this edge's source: a local producer's own name, else a pseudo key.

        A foreign display form never aliases a local job spelled the same (DL-175, DL-162a)."""
        local = local_producer(edge, catalog)
        return target(local) if local is not None else _pseudo_key(edge.src)

    def node_key(name: str) -> IdKey:
        """`_Ids` key for a mutex member: a catalog job, or a dangling n() (L001) as a pseudo.

        It must match the key the declaration used (DL-175)."""
        return target(name) if name in graph.nodes else _pseudo_key(name)

    lines: list[str] = [f"flowchart {direction}"]
    box_nodes: set[str] = set(graph.box_tree.children)
    trigger_nodes: list[str] = []

    def emit_node(name: str, indent: str) -> None:
        if job_kind(catalog.jobs.get(name)) == "FW":
            trigger_nodes.append(name)
            lines.append(f'{indent}{ids(name)}(["{label(name)}"])')
        else:
            lines.append(f'{indent}{ids(name)}["{label(name)}"]')

    def emit_box(box: str, indent: str) -> None:
        if box in collapsed:
            # hidden trigger facts stay loud on the folded node (DL-35a)
            inside = _box_members(graph.box_tree, box) - {box}
            jobs = [j for n in inside if (j := catalog.jobs.get(n)) is not None]
            extras = f"{len(graph.box_tree.children[box])} members"
            if scheduled := sum(1 for j in jobs if j.schedule is not None):
                extras += f", {scheduled} \N{ALARM CLOCK}"
            if watchers := sum(1 for j in jobs if j.job_type == "FW"):
                extras += f", {watchers} \N{PAGE FACING UP}"
            lines.append(f'{indent}{ids(box)}[["{label(box)} ({extras})"]]')
            return
        title = _node_label(box, catalog, strip_prefix, self_locked, multiline=False)
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

    # pseudo-sources: globals, cross-instance producers, undefined locals. They are classified
    # off the edge's atom, never off the shape of `edge.src` (DL-175, DL-162a).
    global_srcs: list[str] = []
    external_srcs: list[str] = []
    undefined_srcs: list[str] = []
    for edge in graph.edges:
        if not in_scope(edge.dst) or local_producer(edge, catalog) is not None:
            continue
        atom = edge.atom
        node_id = ids(_pseudo_key(edge.src))
        if isinstance(atom, GlobalAtom):
            bucket, shape = global_srcs, f'    {node_id}[/"{_esc(edge.src)}"/]'
        elif atom.job.instance is not None:
            bucket, shape = external_srcs, f'    {node_id}{{{{"{_esc(edge.src)}"}}}}'
        else:
            bucket, shape = undefined_srcs, f'    {node_id}["\N{WARNING SIGN} {_esc(edge.src)}"]'
        if edge.src not in bucket:
            bucket.append(edge.src)
            lines.append(shape)

    # dangling n() members (L001) render like undefined producers; the pseudo prefix keeps one
    # id scheme across the bucket (DL-175).
    lock_members = [n for pair in mutex_pairs for n in pair]
    lock_members += [n for clique in cliques for n in clique]
    for name in lock_members:
        if name not in graph.nodes and name not in undefined_srcs:
            undefined_srcs.append(name)
            lines.append(f'    {ids(node_key(name))}["\N{WARNING SIGN} {_esc(name)}"]')

    link_index = 0
    redesign_links: list[int] = []
    seen_edges: set[tuple[IdKey, str, str, str]] = set()
    for edge in graph.edges:
        if not in_scope(edge.dst):
            continue
        src, dst = src_key(edge), target(edge.dst)
        if src == dst and edge.src != edge.dst:
            continue  # both endpoints inside one collapsed box
        text = edge_label(edge)
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

    seen_locks: set[tuple[IdKey, IdKey]] = set()
    for a, b in mutex_pairs:
        at, bt = node_key(a), node_key(b)
        if (at == bt and a != b) or (at, bt) in seen_locks:
            continue  # exclusion internal to a collapsed box / re-anchored dup
        seen_locks.add((at, bt))
        lines.append(f"    {ids(at)} x-. lock .-x {ids(bt)}")
    lock_ids: list[str] = []
    for clique in cliques:
        lock = f"lock:{'+'.join(clique)}"
        lock_ids.append(ids(lock))
        lines.append(f'    {ids(lock)}(("\N{LOCK}"))')
        for member_anchor in dict.fromkeys(node_key(m) for m in clique):
            lines.append(f"    {ids(lock)} -.- {ids(member_anchor)}")

    if redesign_links:
        joined = ",".join(str(i) for i in redesign_links)
        lines.append(f"    linkStyle {joined} {_REDESIGN_LINKSTYLE}")

    def class_line(cls: str, names: list[IdKey]) -> str | None:
        # a pseudo-source key (a tuple) is never a box member, so it is always kept
        rendered = [ids(n) for n in names if isinstance(n, tuple) or visible(n) or n not in anchor]
        return f"    class {','.join(rendered)} {cls}" if rendered else None

    # (class name, node names), one row per node kind; collapsed names are sorted, the others
    # keep first-seen order. Pseudo-source rows map through the `_pseudo_key` the declarations
    # used (DL-175).
    style_rows: list[tuple[str, list[IdKey]]] = [
        ("trigger", [n for n in trigger_nodes if visible(n)]),
        ("globalvar", [_pseudo_key(n) for n in global_srcs]),
        ("external", [_pseudo_key(n) for n in external_srcs]),
        ("undefined", [_pseudo_key(n) for n in undefined_srcs]),
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
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction = "LR",
    elk: bool = False,
    fixed_scale: bool = False,
) -> str:
    """Render the whole derived graph as one Mermaid flowchart body."""
    if graph is None:
        graph = derive_graph(catalog)
    body = _render_chart(
        catalog, graph, None, collapse_threshold=collapse_threshold, direction=direction
    )
    return _frontmatter(elk=elk, fixed_scale=fixed_scale) + body


# ------------------------------------------------------------- report content
#
# The legend/prose/rows split keeps the Markdown report and the HTML page on the same
# content. to_markdown owns every Markdown literal; report_content owns every decision about
# what the report says. These names are public (DL-72): report_content, ReportContent,
# ChartSection, edge_label and the legend/locks prose are what the three emitters (viz,
# viz_html, viz_explore) share.

LEGEND_CHART = """\
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
"""

LEGEND_PROSE = """\
Solid arrow = exact mapping; dashed = assumed (assumption in Appendix B);
thick red = needs redesign (M-row on the edge). Edge letters: f failure,
d done, t terminated, n notrunning, e exitcode, v global variable;
unmarked = success. `(HH:MM)` etc. are lookback qualifiers. `lock` links
and \N{LOCK} hubs are mutual exclusion, not flow.
"""

_LEGEND = (
    "<details>\n<summary>Legend</summary>\n\n```mermaid\n"
    + LEGEND_CHART
    + "```\n\n"
    + LEGEND_PROSE
    + "\n</details>\n"
)

LOCKS_PROSE = (
    "Every stated mutual exclusion. Drawn in charts as lock links, hubs,"
    " or single-instance badges; enumerated here so none hides in a"
    " collapsed box or between workflows (DL-35a)."
)


class ChartSection(NamedTuple):
    """One charted workflow: heading parts plus the bare chart body."""

    wid: str  # "W1"
    comp_title: str  # _component_title, raw
    count_label: str  # "5 jobs" / "1 job"
    prefix: str  # stripped common name prefix, "" if none
    chart: str  # _render_chart body, no frontmatter


# Appendix A/B/C headers: one spelling for to_markdown and viz_html.to_html (DL-178u).
APPENDIX_A_HEADERS = ("job", "kind", "schedule", "command / watched file")
APPENDIX_B_HEADERS = ("producer", "consumer", "via", "lookback", "class", "row", "assumption")
REDESIGN_FLAG_HEADERS = ("job", "row", "reason")
OR_SHAPE_HEADERS = ("job", "attr", "kind", "suggested lowering")


class LockRow(NamedTuple):
    """One Locks-table row: a mutex group and where it is charted."""

    members: str  # joined with x
    kind: str
    charts: str


class StandaloneRow(NamedTuple):
    """One Appendix A row: a job with no wiring to any charted workflow."""

    name: str
    kind: str | None
    schedule: str | None
    command: str | None


class AnnotatedRow(NamedTuple):
    """One Appendix B row: a non-exact edge and how it got there."""

    producer: str
    consumer: str
    via: str
    lookback: str
    cls: str
    mapping_row: str
    assumption: str | None


class RedesignFlagRow(NamedTuple):
    """One Appendix C redesign-flags row."""

    job: str
    mapping_row: str
    reason: str


class OrShapeRow(NamedTuple):
    """One Appendix C OR-shapes row (M12)."""

    job: str
    attr: str
    kind: str
    suggestion: str


class ReportContent(NamedTuple):
    """Everything the report says, before either emitter formats it.

    Rows carry raw strings; each emitter escapes its own. Appendix A/B/C rows are computed
    once here so both emitters render the same data (DL-178u)."""

    summary: str
    sections: list[ChartSection]
    standalone_chart: str | None  # only when include_singletons and any exist
    lock_rows: list[LockRow]
    standalone_rows: list[StandaloneRow]  # Appendix A
    annotated_rows: list[AnnotatedRow]  # Appendix B
    redesign_flag_rows: list[RedesignFlagRow]  # Appendix C
    or_shape_rows: list[OrShapeRow]  # Appendix C
    cycle_rows: list[str]  # Appendix C: arrow-joined job chains


def report_content(
    catalog: CatalogIR,
    graph: DerivedGraph,
    *,
    collapse_threshold: int,
    direction: Direction | Literal["auto"],
    include_singletons: bool,
) -> ReportContent:
    # connectivity and titles ask whether a catalog job reaches another (DL-175);
    # _render_chart keeps the real graph to draw a foreign producer's node
    local_graph = _local_graph(catalog, graph)
    comps = split_components(local_graph)
    touched = _incident_nodes(local_graph)
    standalone = [comp[0] for comp in comps if _is_standalone(comp, touched)]
    charted = [comp for comp in comps if not _is_standalone(comp, touched)]

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

    comp_of: dict[str, str] = {}
    sections: list[ChartSection] = []
    for i, comp in enumerate(charted, start=1):
        wid = f"W{i}"
        for name in comp:
            comp_of[name] = wid
        prefix = _common_prefix(comp)
        chart_dir = _auto_direction(comp, local_graph) if direction == "auto" else direction
        sections.append(
            ChartSection(
                wid=wid,
                comp_title=_component_title(comp, local_graph),
                count_label=f"{len(comp)} job" + ("s" if len(comp) != 1 else ""),
                prefix=prefix,
                chart=_render_chart(
                    catalog,
                    graph,
                    set(comp),
                    collapse_threshold=collapse_threshold,
                    direction=chart_dir,
                    strip_prefix=prefix,
                ),
            )
        )

    standalone_chart: str | None = None
    if include_singletons and standalone:
        sub = DerivedGraph(nodes=standalone)
        standalone_chart = _render_chart(
            catalog, sub, None, collapse_threshold=collapse_threshold, direction="LR"
        )

    lock_rows: list[LockRow] = []
    for group in graph.mutex_groups:
        kind = "self" if len(group) == 1 else "pair"
        charts = ", ".join(dict.fromkeys(comp_of.get(m, "not in catalog") for m in group))
        lock_rows.append(LockRow(" \N{MULTIPLICATION SIGN} ".join(group), kind, charts))

    standalone_rows: list[StandaloneRow] = []
    for name in standalone:
        job = catalog.jobs.get(name)
        standalone_rows.append(
            StandaloneRow(name, job_kind(job), job_schedule(job), job_detail(job))
        )

    annotated_rows: list[AnnotatedRow] = []
    for edge in graph.edges:
        if edge.cls == "exact":
            continue
        lookback = edge.lookback.raw if edge.lookback is not None else ""
        annotated_rows.append(
            AnnotatedRow(
                edge.src, edge.dst, edge.via, lookback, edge.cls, edge.mapping_row, edge.assumption
            )
        )

    redesign_flag_rows: list[RedesignFlagRow] = [
        RedesignFlagRow(f.job, f.mapping_row, f.reason) for f in graph.redesign_flags
    ]
    or_shape_rows: list[OrShapeRow] = [
        OrShapeRow(s.job, s.attr, s.kind, s.lowering) for s in graph.or_shapes
    ]
    cycle_rows = [" \N{RIGHTWARDS ARROW} ".join(cycle) for cycle in graph.cycles]

    return ReportContent(
        summary=summary,
        sections=sections,
        standalone_chart=standalone_chart,
        lock_rows=lock_rows,
        standalone_rows=standalone_rows,
        annotated_rows=annotated_rows,
        redesign_flag_rows=redesign_flag_rows,
        or_shape_rows=or_shape_rows,
        cycle_rows=cycle_rows,
    )


# ------------------------------------------------------------- markdown report


def _cell(text: str | None) -> str:
    """Markdown table cell: escape pipes and newlines; never truncates, since assumptions
    and reasons appear nowhere else."""
    if text is None:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")


def truncate_cell(text: str) -> str:
    """60-char ellipsis for a command/path cell; shared with `viz_html._code` (DL-178h)."""
    if len(text) > 60:
        return text[:59] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def _code_cell(text: str | None) -> str:
    """Command/path cell: a truncated code span."""
    if not text:
        return ""
    flat = truncate_cell(_cell(text.replace("`", "'")))
    return f"`{flat}`"


def _table_header(headers: tuple[str, ...]) -> str:
    """Markdown header row for one of the shared APPENDIX_*_HEADERS tuples."""
    return "| " + " | ".join(headers) + " |"


def _table_separator(width: int) -> str:
    """Markdown separator row matching `_table_header`'s column count."""
    return "|" + "---|" * width


def to_markdown(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    title: str = "catalog",
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction | Literal["auto"] = "auto",
    include_singletons: bool = False,
    elk: bool = False,
    fixed_scale: bool = False,
) -> str:
    """Full Markdown report: summary, legend, charts, locks, appendices A, B and C."""
    if graph is None:
        graph = derive_graph(catalog)
    content = report_content(
        catalog,
        graph,
        collapse_threshold=collapse_threshold,
        direction=direction,
        include_singletons=include_singletons,
    )
    frontmatter = _frontmatter(elk=elk, fixed_scale=fixed_scale)

    def fence(body: str) -> list[str]:
        return ["```mermaid", frontmatter + body.rstrip("\n"), "```", ""]

    out: list[str] = [f"# Workflow graph: {title}", "", content.summary, "", _LEGEND]

    for sec in content.sections:
        out.append(f"## {sec.wid} \N{EM DASH} {sec.comp_title} ({sec.count_label})")
        if sec.prefix:
            out.append("")
            out.append(f"All names share the prefix `{sec.prefix}` (stripped in the chart).")
        out.append("")
        out.extend(fence(sec.chart))

    if content.standalone_chart is not None:
        out.append(f"## Standalone jobs ({len(content.standalone_rows)})")
        out.append("")
        out.extend(fence(content.standalone_chart))

    if content.lock_rows:
        out.append("## Locks")
        out.append("")
        out.append(LOCKS_PROSE)
        out.append("")
        out.append("| lock | kind | charts |")
        out.append("|---|---|---|")
        for joined, lock_kind, lock_charts in content.lock_rows:
            out.append(f"| {_cell(joined)} | {lock_kind} | {lock_charts} |")
        out.append("")

    out.append("## Appendix A \N{EM DASH} standalone jobs (not part of any workflow)")
    out.append("")
    if content.standalone_rows:
        out.append(_table_header(APPENDIX_A_HEADERS))
        out.append(_table_separator(len(APPENDIX_A_HEADERS)))
        for name, kind, schedule, detail in content.standalone_rows:
            out.append(
                f"| {_cell(name)} | {_cell(kind)} | {_cell(schedule)} | {_code_cell(detail)} |"
            )
    else:
        out.append("None.")
    out.append("")

    out.append("## Appendix B \N{EM DASH} edge annotations")
    out.append("")
    if content.annotated_rows:
        out.append(_table_header(APPENDIX_B_HEADERS))
        out.append(_table_separator(len(APPENDIX_B_HEADERS)))
        for src, dst, via, lookback, cls, mapping_row, assumption in content.annotated_rows:
            out.append(
                f"| {_cell(src)} | {_cell(dst)} | {via} | {_cell(lookback)}"
                f" | {cls} | {mapping_row} | {_cell(assumption)} |"
            )
    else:
        out.append("None \N{EM DASH} every edge maps exactly.")
    out.append("")

    has_c = content.redesign_flag_rows or content.or_shape_rows or content.cycle_rows
    out.append("## Appendix C \N{EM DASH} redesign flags, OR shapes, cycles")
    out.append("")
    if not has_c:
        out.append("None.")
        out.append("")
    if content.redesign_flag_rows:
        out.append("### Redesign flags")
        out.append("")
        out.append(_table_header(REDESIGN_FLAG_HEADERS))
        out.append(_table_separator(len(REDESIGN_FLAG_HEADERS)))
        for job, mapping_row, reason in content.redesign_flag_rows:
            out.append(f"| {_cell(job)} | {mapping_row} | {_cell(reason)} |")
        out.append("")
    if content.or_shape_rows:
        out.append("### OR shapes (M12)")
        out.append("")
        out.append(_table_header(OR_SHAPE_HEADERS))
        out.append(_table_separator(len(OR_SHAPE_HEADERS)))
        for job, attr, kind, lowering in content.or_shape_rows:
            out.append(f"| {_cell(job)} | {attr} | {kind} | {_cell(lowering)} |")
        out.append("")
    if content.cycle_rows:
        out.append("### Cycles (L010)")
        out.append("")
        for cycle in content.cycle_rows:
            out.append(f"- {cycle}")
        out.append("")

    return "\n".join(out)
