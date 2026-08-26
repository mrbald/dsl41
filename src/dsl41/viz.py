"""Markdown/Mermaid rendering of the derived graph (phase 6, CLAUDE.md / DL-03).

Spec (ir-design ss1 pipeline + phase list, amended DL-35/DL-73): IR-G -> a Markdown
report of per-component Mermaid charts, plus appendices that materialize
everything the charts thin out or drop. Pure function of (catalog, graph) --
IR-G for the analysis, IR-F for the per-node display facts (DL-73);
deterministic output for identical input.

Rendering decisions (each with a test):
- Node ids are generated (n0, n1, ...) in first-need order; real names live
  in labels. JIL names may contain '.', '#', ':', '^' -- none Mermaid-id-safe.
- Visual grammar (DL-35): shape/line-style is the primary channel, color is
  redundant reinforcement, Unicode symbols not FontAwesome (hosts don't ship
  FA CSS). Command job = plain rect (unmarked default). File watcher =
  stadium + U+1F4C4 prefix (a source/trigger). Scheduled node = second label
  line with U+23F0 + trigger digest (_schedule_digest, trigger fields only:
  run_window is a gate per SEM-33 and must_* are alarms per SEM-34, both
  excluded). Cross-instance producer = hexagon (frees [[..]] for
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

#: Via -> chart letter. The status half IS conditions.STATUS_LETTER under the
#: lowercase Via spelling (one status vocabulary, DL-72); only the two vias
#: that are not statuses are spelled out here.
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
    """One merged per-chart YAML frontmatter block. `layout:` precedes
    `flowchart:` so elk-only output stays byte-identical to the historical
    constant (renderer-facing bytes are pinned by tests)."""
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


#: `_Ids` key: a real catalog job's own name (a plain `str`), or a pseudo-
#: source's namespaced key (a `(str, str)` tuple, `_pseudo_key` below). A
#: `str` can never equal a 2-tuple, so the two key spaces cannot collide
#: regardless of what characters a job name contains -- unlike a string
#: prefix, which would need an assumption about the JIL name alphabet
#: (DL-175, found unsound by its own adversarial review: a NUL-prefixed
#: string is not provably distinct from a job name the scanner never
#: promised to reject).
IdKey = str | tuple[str, str]


class _Ids:
    def __init__(self) -> None:
        self._by_key: dict[IdKey, str] = {}

    def __call__(self, key: IdKey) -> str:
        if key not in self._by_key:
            self._by_key[key] = f"n{len(self._by_key)}"
        return self._by_key[key]


def _pseudo_key(name: str) -> IdKey:
    """`_Ids` lookup key for a pseudo-source node, namespaced apart from a
    real catalog job's own key (a plain `str`) by TYPE, not by content. A
    cross-instance producer's display form (`name^INST`) can be spelled
    exactly like a local job (DL-162a); without this, the two would fold
    onto ONE Mermaid vertex id (DL-175, the S-EDGE class)."""
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
# Per-node display facts (DL-35), read straight off IR-F by whichever emitter
# needs them: IR-G used to carry a verbatim copy (DerivedGraph.node_meta),
# which made the analysis layer own display needs it derives nothing for
# (DL-73). Kind is JobIR.job_type verbatim; detail is the command for CMD
# jobs and the watched path for FW jobs; the schedule digest covers the
# TRIGGER fields only -- derive._TRIGGER_FIELDS, i.e. run_window excluded as
# a gate (SEM-33) and must_* excluded as alarms (SEM-34).
#
# job_kind/job_schedule/job_detail are PUBLIC like report_content and
# edge_label (DL-72): the three emitters share them.


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
    """'CMD' | 'BOX' | 'FW' + extensible, as JobIR.job_type. None = the name
    is not a catalog job (a pseudo-source: global, external, undefined)."""
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


def _local_graph(catalog: CatalogIR, graph: DerivedGraph) -> DerivedGraph:
    """`graph` with every edge's `src` resolved to its real local producer,
    or -- when there is none -- REWRITTEN to a namespaced sentinel that can
    never equal a catalog job's own name. `derive.components` is generic
    (DL-162a) and its union reads `edge.src in nodes`, sound only when `src`
    is known to be a real catalog job; `split_components`, `_incident_nodes`,
    `_auto_direction` and `_component_title` all ask "does this catalog job
    reach another", never "does this display string reach another", so they
    take this resolved copy from their one caller (`report_content`) instead
    of each re-deriving locality. `derive.local_producer` is the sound test
    (DL-162a); a cross-instance producer's display form (`name^INST`) can be
    spelled exactly like a local job.

    REWRITE, not drop: `dst` stays whatever it already was (always a real
    catalog job), so a job with only a foreign/global/undefined predecessor
    stays correctly "touched" in `_incident_nodes` -- it is not wired to
    another LOCAL job, but it is not unwired either. Only the SRC identity
    is what must never collide (DL-175, the S-EDGE class). `_render_chart`
    keeps the REAL graph: it still has to draw the foreign producer as its
    own pseudo-source.

    `DerivedEdge.src` is a `str` field, so this cannot use `_pseudo_key`'s
    tuple trick (that one is for `_Ids`, an internal lookup structure with
    no such constraint). Instead the sentinel is checked against THIS
    catalog's actual, finite job-name set before use -- proven, not
    assumed, distinct (DL-175, found unsound by its own adversarial review:
    a string prefix is only as safe as an unstated assumption about the
    JIL name alphabet)."""
    sentinel = "\x00pseudo"
    while sentinel in catalog.jobs:
        sentinel += "\x00"
    resolved = [
        e if local_producer(e, catalog) is not None else e.model_copy(update={"src": sentinel})
        for e in graph.edges
    ]
    return graph.model_copy(update={"edges": resolved})


def split_components(graph: DerivedGraph) -> list[list[str]]:
    """Connected components over dependency edges between catalog jobs plus
    box co-membership; mutex and pseudo-sources do NOT connect (DL-35).
    Members in graph.nodes (catalog) order; components ordered by descending
    size, ties by first member's catalog position.

    Generic over `graph.edges` -- no catalog, no locality resolution (DL-
    162a): a caller wanting IN-CATALOG-ONLY connectivity (its one production
    caller, `report_content`) passes a `_local_graph`-resolved copy, else a
    foreign display form colliding with a local job's own spelling would
    union two unrelated jobs (DL-175). Not a precondition this function
    enforces or can check -- callers testing raw union mechanics over a
    plain graph (this module's own tests) are exercising a different,
    equally legitimate question."""
    index = {name: i for i, name in enumerate(graph.nodes)}
    grouped = components(graph.nodes, graph.edges, bind_box_members=graph.box_tree)
    return sorted(grouped, key=lambda comp: (-len(comp), index[comp[0]]))


def _incident_nodes(graph: DerivedGraph) -> set[str]:
    """Catalog jobs touching any edge (either end) or any mutex group.

    Same genericity as `split_components` (DL-175): `report_content` is the
    caller that needs `edge.src` to never be a foreign display form
    spelling a local job's own name, and it is the one that passes a
    `_local_graph`-resolved graph to get that."""
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
    node; '(+n more)' when the name covers only part of the component.

    Same genericity as `split_components` (DL-175): `e.src in comp_set`
    below reads whatever `graph` it is given; `report_content` is the
    caller that needs it locally-resolved and passes a `_local_graph`
    copy."""
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
    job->job edges), else LR. Deterministic.

    Same genericity as `split_components` (DL-175): `report_content` passes
    a `_local_graph`-resolved graph so a foreign display form in `comp_set`
    cannot manufacture a false succ edge; this function itself does not
    enforce that."""
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


#: deliberate label-line budget: Mermaid auto-wraps HTML labels at ~200px and
#: the wrapped text overflows stadium/trigger shapes (GitHub renderer), so the
#: label owns its line breaks and forbids all others via no-break spaces
_SCHEDULE_LINE_BUDGET = 28

_NBSP = "\N{NO-BREAK SPACE}"  # survives htmlLabels; degrades to a space in SVG text


def _schedule_label_lines(digest: str) -> list[str]:
    """Non-breaking label lines from a schedule digest: parts (times, days,
    calendars, timezone) pack left-to-right under the budget, breaking only
    at part boundaries. A single oversized part stays whole -- a wide node
    beats a mid-token wrap."""
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
    """multiline=False for subgraph titles: hosts render <br/> in titles
    inconsistently (GitHub), so expanded boxes get a one-line separator."""
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
        return _node_label(name, catalog, strip_prefix, self_locked)

    def src_key(edge: DerivedEdge) -> IdKey:
        """`_Ids` lookup key for this edge's SOURCE. A resolved local
        producer keys off its own (target-resolved) name, matching its own
        node's declaration; any other edge -- global, cross-instance,
        undefined -- keys off the namespaced pseudo-source id instead, so a
        foreign display form can never alias a local job spelled the same
        way (DL-175, the S-EDGE class; DL-162a)."""
        local = local_producer(edge, catalog)
        return target(local) if local is not None else _pseudo_key(edge.src)

    def node_key(name: str) -> IdKey:
        """`_Ids` lookup key for a mutex pair/clique member: a real catalog
        job (target-resolved, same as any other node) or a dangling n()
        reference (L001's finding), which is declared as a pseudo-source
        below and must be looked up with the SAME namespaced key it was
        declared with -- `target(name)` alone would mint a second, never-
        declared id for it (DL-175, found by adversarial review of this
        entry: a dangling lock member rendered its link to a phantom
        vertex)."""
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

    # pseudo-sources: globals, cross-instance producers, undefined locals.
    # Classified off the edge's ATOM facts, never off `edge.src`'s shape
    # (DL-175, the S-EDGE class): `local_producer` resolving means this is
    # already a real node above, and a cross-instance atom means external
    # regardless of whether its display form happens to spell a local job's
    # name (DL-162a) -- the "^" shape stays rendering TEXT only, never the
    # decider.
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

    # dangling n() members (L001's finding) render like undefined producers.
    # Always local vocabulary (the condition grammar bans "^" in n()), so no
    # collision is possible here -- prefixed anyway, for one consistent id
    # scheme across the whole "undefined" bucket (DL-175).
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
        # a pseudo-source key (a tuple) is never a box member, so it is
        # always kept -- matching the plain-string check's own effect on a
        # name that was never in `anchor` either.
        rendered = [ids(n) for n in names if isinstance(n, tuple) or visible(n) or n not in anchor]
        return f"    class {','.join(rendered)} {cls}" if rendered else None

    # (class name, node names) -- one row per node kind; collapsed's names
    # are sorted (deterministic order over a set), the others keep
    # first-seen order from the loops above. The three pseudo-source rows
    # carry raw display names, mapped through the SAME `_pseudo_key` the
    # declaration loops above keyed their `ids()` calls with (DL-175).
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
# The legend/prose/rows split exists so the Markdown report and the HTML page
# (viz_html) format the SAME content -- parity drift between emitters is the
# no-silent-loss failure mode. to_markdown owns every Markdown literal;
# report_content owns every decision about what the report says.
#
# These names are PUBLIC on purpose (DL-72): report_content, ReportContent,
# ChartSection, edge_label and the legend/locks prose are the contract the
# three emitters (viz, viz_html, viz_explore) share, not private borrowings.

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


class ReportContent(NamedTuple):
    """Everything the report says, before either emitter formats it. Rows
    carry RAW strings; each emitter applies its own escaping. Appendix C
    (redesign flags / OR shapes / cycles) is read off the graph directly."""

    summary: str
    sections: list[ChartSection]
    standalone: list[str]  # Appendix A job names
    standalone_chart: str | None  # only when include_singletons and any exist
    lock_rows: list[tuple[str, str, str]]  # (members joined with x, kind, charts)
    annotated: list[DerivedEdge]  # Appendix B rows


def report_content(
    catalog: CatalogIR,
    graph: DerivedGraph,
    *,
    collapse_threshold: int,
    direction: Direction | Literal["auto"],
    include_singletons: bool,
) -> ReportContent:
    local_graph = _local_graph(catalog, graph)  # DL-175: connectivity/title
    # passes below ask "does this catalog job reach another"; _render_chart
    # keeps the REAL `graph` -- it still draws a foreign producer's node.
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

    lock_rows: list[tuple[str, str, str]] = []
    for group in graph.mutex_groups:
        kind = "self" if len(group) == 1 else "pair"
        charts = ", ".join(dict.fromkeys(comp_of.get(m, "not in catalog") for m in group))
        lock_rows.append((" \N{MULTIPLICATION SIGN} ".join(group), kind, charts))

    return ReportContent(
        summary=summary,
        sections=sections,
        standalone=standalone,
        standalone_chart=standalone_chart,
        lock_rows=lock_rows,
        annotated=[e for e in graph.edges if e.cls != "exact"],
    )


# ------------------------------------------------------------- markdown report


def _cell(text: str | None) -> str:
    """Markdown table cell: escape pipes and newlines (never truncate --
    assumptions/reasons appear nowhere else in the report)."""
    if text is None:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")


def truncate_cell(text: str) -> str:
    """60-char ellipsis for a command/path cell: full text is IR-F's, not
    the report's, responsibility. Shared by `_code_cell` (markdown) and
    `viz_html._code` (HTML), so the two emitters cut at the same place
    (DL-178h)."""
    if len(text) > 60:
        return text[:59] + "\N{HORIZONTAL ELLIPSIS}"
    return text


def _code_cell(text: str | None) -> str:
    """Command/path cell: code span, truncated -- full text is IR-F's, not
    the report's, responsibility."""
    if not text:
        return ""
    flat = truncate_cell(_cell(text.replace("`", "'")))
    return f"`{flat}`"


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
    """Full Markdown report: summary, legend, one chart per component,
    shared-locks section, appendices A (standalone jobs) / B (non-exact
    edges) / C (redesign flags, OR shapes, cycles)."""
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
        out.append(f"## Standalone jobs ({len(content.standalone)})")
        out.append("")
        out.extend(fence(content.standalone_chart))

    if content.lock_rows:
        out.append("## Locks")
        out.append("")
        out.append(LOCKS_PROSE)
        out.append("")
        out.append("| lock | kind | charts |")
        out.append("|---|---|---|")
        for joined, kind, charts in content.lock_rows:
            out.append(f"| {_cell(joined)} | {kind} | {charts} |")
        out.append("")

    out.append("## Appendix A \N{EM DASH} standalone jobs (not part of any workflow)")
    out.append("")
    if content.standalone:
        out.append("| job | kind | schedule | command / watched file |")
        out.append("|---|---|---|---|")
        for name in content.standalone:
            job = catalog.jobs.get(name)
            out.append(
                f"| {_cell(name)} | {_cell(job_kind(job))}"
                f" | {_cell(job_schedule(job))}"
                f" | {_code_cell(job_detail(job))} |"
            )
    else:
        out.append("None.")
    out.append("")

    out.append("## Appendix B \N{EM DASH} edge annotations")
    out.append("")
    if content.annotated:
        out.append("| producer | consumer | via | lookback | class | row | assumption |")
        out.append("|---|---|---|---|---|---|---|")
        for e in content.annotated:
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
