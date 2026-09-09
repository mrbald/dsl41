"""Interactive exploration page of the derived graph (DL-71).

A navigation LENS over the whole graph, not the artifact of record: the
Markdown/HTML report keeps the appendices and stays the no-silent-loss
carrier. What the page must still honor is the report's content policy for
what it does show -- every edge annotation is reachable via the details
panel, and assumptions are never truncated.

The emitter goes straight from (catalog, graph) to cytoscape.js elements
JSON -- no Mermaid text anywhere. The page lays out with ELK (cytoscape-elk
driving elk.bundled.js on the main thread: no Worker, no fetch, offline like
DL-70's report page) and re-layouts the visible subset on focus.

Emission decisions (each with a test):
- Nodes carry id/label plus kind/schedule/detail read off IR-F through
  viz's display-facts helpers, and `parent` from box_tree.parent -- boxes
  become cytoscape compound nodes. With a collapse threshold, a top-level
  box with more direct members than it carries `collapsed: true`, the
  report's own rule; the page folds those before its first picture
  (DL-190).
- Edge endpoints outside the catalog (undefined producers, externals
  "name^INST", global variable names) synthesize EXT nodes, class `ext`
  plus `global` when some referencing edge has via=="global"; locality is
  the atom's `instance` fact, never `edge.src`'s spelling, and an EXT id
  is namespaced when it would collide with a same-spelled local job's own
  id (DL-176).
- Edges carry via/lookback/cls/mapping_row/assumption; cls doubles as the
  style class (exact/assumed/redesign); the canvas label reuses
  viz.edge_label, so the DL-35 thinning grammar is re-expressed, not forked.
- Nodes also carry the condition TEXT (condition/box_success/box_failure)
  and its structure -- cond_shape and cond_tree, keyed by attribute -- and
  every edge carries the `attr` it derives from, an edge under an OR its
  `branch` and the `branch_key` that keeps two attributes' branches apart
  (DL-191). Incoming arrows are an
  AND unless the branch says otherwise; a bare local n() is a lock and has
  no arrow at all. Every edge is matched back to the atom it derives from,
  and an unmatched edge or atom raises rather than drawing a condition the
  page cannot account for.
- Locks are elements of their own (DL-192), excluded from the layout and
  placed on their members: a resource semaphore is one hub per CONSUMED
  `insert_resource` (resources are not in IR-G, so they are read off IR-F,
  the DL-73 display-facts stance), and a mutex keeps the report's grammar
  through `viz.mutex_plan` -- pair link, complete-clique hub, self badge.
- The elements JSON embeds with DL-70's rule: every "<" becomes \\u003c
  (valid JSON, neutralizes </script and <!-- in one rule).
- Template substitution is single-pass unique-marker (viz_html.substitute):
  replaced content is never re-scanned, so marker-shaped job/file names
  cannot splice a later payload into the page.
- Two vendored payloads embed, in this order: the customElements polyfill,
  then the cytoscape bundle. cytoscape-context-menus builds its menu out of
  customized built-in elements, which WebKit has never implemented -- the
  polyfill has to be defined before the plugin registers them (DL-77).
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Iterator
from importlib.resources import files
from typing import Literal, NamedTuple, cast

from dsl41.capacity import release_policy
from dsl41.conditions import And, Atom, Cond, Or, Paren, iter_atoms
from dsl41.derive import (
    BoxTree,
    DerivedEdge,
    DerivedGraph,
    derive_graph,
    is_mutex_atom,
    local_producer,
)

# module level, not lazy: cli_compile imports THIS module on demand, so
# only `--format explore` pays for the decompiler surface (review NIT)
from dsl41.dsl import cond_to_source
from dsl41.ir import CatalogIR, ResourceRef
from dsl41.viz import Direction, edge_label, job_detail, job_kind, job_schedule, mutex_plan
from dsl41.viz_html import substitute

_KIND_CLASS = {"BOX": "box", "FW": "fw"}  # anything else renders as a command

# --------------------------------------------------- condition structure (DL-191)
#
# The page draws one arrow per condition ATOM and used to say nothing about how
# the atoms combine: `s(A) & (s(B) | f(C))` was three arrows and no operator
# anywhere. These helpers read the boolean structure off the same IR-F Cond
# trees derive walked in pass 1, and hand the page three facts per
# condition-bearing attribute: the source text, a SHAPE it can badge, and a
# per-atom BRANCH it can label, colour and group. IR-G is untouched -- the map
# from an edge back to its atom is recomputed here, never persisted.

#: What the canvas can draw as branches. One alternation deep is drawable;
#: `complex` is the honest fallback -- the badge says so and the panel shows
#: the tree and the text, rather than a picture that flattens the nesting.
CondShape = Literal["single", "all", "any", "all-of-any", "any-of-all", "complex"]


def _is_atom(cond: Cond) -> bool:
    return not isinstance(cond, (And, Or, Paren))


def _atom_count(cond: Cond) -> int:
    return sum(1 for _ in iter_atoms(cond))


def _flatten(cond: Cond) -> Cond:
    """Paren-stripped, same-op-flattened copy: the LOGICAL structure.

    Parens are fidelity only (erased in canonical form, conditions.py), and
    & and | are each associative, so `a & (b & c)` is one three-operand AND.
    Without the flattening it would classify as `complex` and the page would
    refuse to draw a shape it draws perfectly well. Atom nodes carry over
    unchanged, spans included: the edge match below is by value."""
    if isinstance(cond, Paren):
        return _flatten(cond.inner)
    if isinstance(cond, (And, Or)):
        operands: list[Cond] = []
        for operand in cond.operands:
            flat = _flatten(operand)
            if type(flat) is type(cond):
                operands.extend(cast("And | Or", flat).operands)
            else:
                operands.append(flat)
        return And(operands=operands) if isinstance(cond, And) else Or(operands=operands)
    return cond


def _is_flat_group(cond: Cond, kind: type[And] | type[Or]) -> bool:
    """A one-level AND (or OR) whose own operands are all atoms."""
    return (
        isinstance(cond, (And, Or)) and isinstance(cond, kind) and all(map(_is_atom, cond.operands))
    )


def _shape(cond: Cond) -> CondShape:
    """Classify a FLATTENED tree (`_flatten` first, always)."""
    if isinstance(cond, Or):
        if all(map(_is_atom, cond.operands)):
            return "any"
        if all(_is_atom(op) or _is_flat_group(op, And) for op in cond.operands):
            return "any-of-all"
        return "complex"
    if isinstance(cond, And):
        if all(map(_is_atom, cond.operands)):
            return "all"
        if all(_is_atom(op) or _is_flat_group(op, Or) for op in cond.operands):
            return "all-of-any"
        return "complex"
    return "single"


def _letter(index: int) -> str:
    """a..z, then aa, ab, ...: one OR's name inside an all-of-any."""
    name = ""
    while True:
        name = chr(ord("a") + index % 26) + name
        index = index // 26 - 1
        if index < 0:
            return name


def _all_of_any_labels(cond: And) -> list[str | None]:
    """Branch labels for an AND of atoms and flat ORs.

    One OR: `|k` alone, k the operand index inside it -- there is nothing to
    confuse it with. Several: a letter per OR in operand order, so `a|1` and
    `b|1` read as two different alternations, not one twice."""
    ors = [operand for operand in cond.operands if isinstance(operand, Or)]
    labels: list[str | None] = []
    seen = 0
    for operand in cond.operands:
        if isinstance(operand, Or):
            prefix = _letter(seen) if len(ors) > 1 else ""
            seen += 1
            for k, inner in enumerate(operand.operands, 1):
                labels.extend([f"{prefix}|{k}"] * _atom_count(inner))
        else:
            labels.extend([None] * _atom_count(operand))
    return labels


def _branch_labels(cond: Cond, shape: CondShape) -> list[str | None]:
    """One branch label per atom, in `iter_atoms` order; None wherever the
    atom is not under an OR -- an unlabelled arrow is an AND arrow, which is
    the page's default reading. `complex` labels nothing: a branch the page
    cannot draw honestly is better left unnamed."""
    if shape in ("any", "any-of-all"):
        labels: list[str | None] = []
        for k, operand in enumerate(cast("Or", cond).operands, 1):
            labels.extend([f"|{k}"] * _atom_count(operand))
        return labels
    if shape == "all-of-any":
        return _all_of_any_labels(cast("And", cond))
    return [None] * _atom_count(cond)


def _canvas_suffix(shape: CondShape, branch: str) -> str:
    """What the EDGE LABEL says about a branch, which is less than the branch
    itself. The hollow arrowhead already says "one alternative", so a suffix
    earns its place on the canvas only where it GROUPS arrows: an any-of-all
    shares `|k` across a whole alternative's atoms, and several ORs under one
    AND name which OR (`|a`, `|b`). A flat any and a single OR under an AND
    add one label per arrow that groups nothing, and the suffixes collide on
    the taxi edges' shared column (visual check, DL-191). The full branch
    stays in the data, the tree, the paint and the panel."""
    if shape == "any-of-all":
        return branch
    if shape == "all-of-any" and not branch.startswith("|"):
        return "|" + branch.split("|", 1)[0]
    return ""


class _EdgeCond(NamedTuple):
    """What one edge's own atom says about it: the attribute it came from,
    the branch of that attribute's OR it is (None outside one), the key that
    keeps two attributes' branches apart, and the canvas label's suffix."""

    attr: str
    branch: str | None
    branch_key: str | None
    suffix: str


def _cond_tree(cond: Cond, leaves: Iterator[dict[str, object]]) -> dict[str, object]:
    """The flattened tree as the JSON the panel renders: groups carry
    op/items, leaves come from `leaves` in `iter_atoms` order."""
    if isinstance(cond, (And, Or)):
        return {
            "op": "and" if isinstance(cond, And) else "or",
            "items": [_cond_tree(operand, leaves) for operand in cond.operands],
        }
    return next(leaves)


def _take_edge(queue: list[int], graph: DerivedGraph, atom: Atom, where: str) -> int:
    """Consume the queued edge this atom derived: the first unclaimed one
    whose own `atom` equals it (DerivedEdge holds a deep COPY of the IR-F
    node, so equality is by value, spans included -- two identical atoms at
    different positions stay distinct). Raising is the point: an atom whose
    arrow the page cannot name is the silent loss this rule exists against."""
    for position, index in enumerate(queue):
        if graph.edges[index].atom == atom:
            return queue.pop(position)
    raise ValueError(f"{where}: no derived edge for atom {cond_to_source(atom)!r}")


# ----------------------------------------------------------------- locks (DL-192)
#
# Two kinds of lock exist and the page drew neither. A MUTEX is derive's M07
# record: a bare local n() in `condition` is an exclusion, not an edge, so it
# was on the canvas nowhere at all. A RESOURCE semaphore is `insert_resource`
# plus the `resources:` requirements that draw on it; resources are not in
# IR-G, so the emitter reads them off IR-F -- the DL-73 display-facts stance,
# the same one job kind and schedule already take.
#
# The mutex shapes are the report's, decided by `viz.mutex_plan` and not by a
# second rule here: pairs are what the JIL states, a COMPLETE clique of three
# or more is one hub, and a self-mutex is a badge (DL-35 item 6).

_LOCK_GLYPH = "\N{LOCK}"

#: How a release policy reads in the details panel. `capacity.release_policy`
#: owns the table that PICKS one (DL-50); this only spells the answer.
_POLICY_WORDS = {
    "completion": "released on completion",
    "success": "released on success",
    "never": "never released",
}


def _box_chain(tree: BoxTree, name: str) -> list[str]:
    """This job's enclosing boxes, innermost first. The page resolves a lock
    member to the first of [member, *chain] the graph still has: a collapse
    takes the member out and leaves its box standing."""
    chain: list[str] = []
    current = tree.parent.get(name)
    while current is not None:
        chain.append(current)
        current = tree.parent.get(current)
    return chain


def _lock_id(kind: str, name: str, taken: set[str]) -> str:
    """A namespaced lock id, widened until free. A job may legally be named
    `lock:m:A+B`, and cytoscape ids are one namespace over every element."""
    candidate = f"lock:{kind}:{name}"
    while candidate in taken:
        candidate = "_" + candidate
    taken.add(candidate)
    return candidate


def _link_label(ref: ResourceRef) -> str:
    """What one requirement draws: the quantity when it is more than one
    unit, plus the FREE letter when the job states one. Both silent in the
    ordinary case -- one unit, engine default -- like the DL-35 thinning."""
    parts = [str(ref.quantity)] if ref.quantity > 1 else []
    if ref.free is not None:
        parts.append(ref.free)
    return " ".join(parts)


def _resource_locks(
    catalog: CatalogIR, graph: DerivedGraph, taken: set[str]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """One hub per CONSUMED resource, in first-consumption order, plus one
    undirected link per requirement. A resource nothing consumes is not
    drawn -- the summary counts those, so the omission is stated, not
    silent."""
    consumers: dict[str, list[tuple[str, ResourceRef]]] = {}
    for name, job in catalog.jobs.items():
        for ref in job.resources:
            consumers.setdefault(ref.name, []).append((name, ref))
    nodes: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    for res_name, refs in consumers.items():
        resource = catalog.resources.get(res_name)
        try:
            capacity = resource.capacity_units() if resource is not None else None
        except ValueError:
            # a malformed `amount` is preflight's loud refusal (DL-50), never a
            # crash in a lens: the hub reads unsized, like an undeclared one
            capacity = None
        res_type = (resource.res_type or "").strip().upper() if resource is not None else ""
        hub = _lock_id("r", res_name, taken)
        members: list[dict[str, object]] = []
        for job_name, ref in refs:
            policy = release_policy(res_type, ref.free)
            members.append(
                {
                    "id": job_name,
                    "job": job_name,
                    "boxes": _box_chain(graph.box_tree, job_name),
                    "quantity": ref.quantity,
                    "free": ref.free,
                    "policy": policy,
                }
            )
            links.append(
                {
                    "data": {
                        "source": hub,
                        "target": job_name,
                        "lock": "resource",
                        "resource": res_name,
                        "quantity": ref.quantity,
                        "free": ref.free,
                        "policy": policy,
                        "label": _link_label(ref),
                    },
                    "classes": "lock resource member",
                }
            )
        nodes.append(
            {
                "data": {
                    "id": hub,
                    "label": f"{_LOCK_GLYPH} {res_name} ({capacity if capacity is not None else '?'})",
                    "kind": "LOCK",
                    "lock": "resource",
                    "resource": res_name,
                    "capacity": capacity,
                    "members": members,
                },
                "classes": "lock resource",
            }
        )
    return nodes, links


def _mutex_how(member: str, others: list[str], names: Callable[[str, str], bool]) -> str:
    """One clique member's side of the exclusion, in words. `mutual` only
    when every direction is stated both ways -- a clique is complete in
    PAIRS, and a pair forms from one reference (DL-35 item 6)."""
    waits = [other for other in others if names(member, other)]
    blocked = [other for other in others if names(other, member)]
    if waits == others and blocked == others:
        return "mutual"
    parts: list[str] = []
    if waits:
        parts.append("waits while " + ", ".join(waits) + (" run" if len(waits) > 1 else " runs"))
    if blocked:
        verb = " wait" if len(blocked) > 1 else " waits"
        parts.append(", ".join(blocked) + verb + " while it runs")
    return "; ".join(parts)


def _mutex_locks(
    graph: DerivedGraph,
    plan: tuple[set[str], list[list[str]], list[tuple[str, str]]],
    node_id: Callable[[str], str],
    taken: set[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """The pair links and clique hubs of `viz.mutex_plan`. The tee marks the
    end that WAITS: `bare_notrunning` keeps who names whom, which the
    undirected `mutex_groups` projection cannot (a pair forms from ONE
    reference, so one-way is the common case)."""
    _, cliques, pairs = plan
    bare = graph.bare_notrunning

    def names(a: str, b: str) -> bool:
        return b in bare.get(a, [])

    nodes: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    for a, b in pairs:
        directions = [f"{x} waits while {y} runs" for x, y in ((a, b), (b, a)) if names(x, y)]
        links.append(
            {
                "data": {
                    "source": node_id(a),
                    "target": node_id(b),
                    "lock": "mutex",
                    "source_tee": names(a, b),
                    "target_tee": names(b, a),
                    "directions": directions,
                    "how": "mutual" if len(directions) == 2 else "one-way",
                    "label": "",
                },
                "classes": "lock mutex pair",
            }
        )
    for clique in cliques:
        hub = _lock_id("m", "+".join(clique), taken)
        members: list[dict[str, object]] = []
        for member in clique:
            others = [other for other in clique if other != member]
            how = _mutex_how(member, others, names)
            members.append(
                {
                    "id": node_id(member),
                    "job": member,
                    "boxes": _box_chain(graph.box_tree, member),
                    "how": how,
                }
            )
            links.append(
                {
                    "data": {
                        "source": hub,
                        "target": node_id(member),
                        "lock": "mutex",
                        "target_tee": any(names(member, other) for other in others),
                        "how": how,
                        "label": "",
                    },
                    "classes": "lock mutex member",
                }
            )
        nodes.append(
            {
                "data": {
                    "id": hub,
                    "label": f"{_LOCK_GLYPH} mutex",
                    "kind": "LOCK",
                    "lock": "mutex",
                    "capacity": None,
                    "members": members,
                },
                "classes": "lock mutex",
            }
        )
    return nodes, links


def _lock_elements(
    catalog: CatalogIR,
    graph: DerivedGraph,
    plan: tuple[set[str], list[list[str]], list[tuple[str, str]]],
    node_id: Callable[[str], str],
    taken: set[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Every lock node and link: resources first, then mutex. Link ids are
    minted last, in the same namespace as everything else."""
    res_nodes, res_links = _resource_locks(catalog, graph, taken)
    mutex_nodes, mutex_links = _mutex_locks(graph, plan, node_id, taken)
    links = res_links + mutex_links
    for i, link in enumerate(links):
        data = link["data"]
        assert isinstance(data, dict)
        data["id"] = _lock_id("e", str(i), taken)
    return res_nodes + mutex_nodes, links


def unused_resources(catalog: CatalogIR) -> list[str]:
    """Declared `insert_resource` records no job draws on, in catalog order.
    The page draws no hub for them -- nothing would join it -- so the
    summary counts them instead (DL-07's spirit: state the omission)."""
    consumed = {ref.name for job in catalog.jobs.values() for ref in job.resources}
    return [name for name in catalog.resources if name not in consumed]


def _condition_facts(
    catalog: CatalogIR, graph: DerivedGraph, edge_ids: list[str]
) -> tuple[dict[str, dict[str, object]], dict[int, _EdgeCond]]:
    """Per catalog job: its condition texts, each one's shape, and the tree
    the details panel renders. Second return: per edge INDEX, the attribute
    and branch facts that edge's own atom carries.

    `cond_shape` and `cond_tree` are keyed by ATTRIBUTE rather than scalar,
    because box_success/box_failure atoms derive edges too (M15/M16): a
    scalar could explain only one attribute of the three, and the arrows
    into a box whose box_success is an OR would carry a branch label nothing
    on the page accounted for.

    Every edge is matched back to the atom it derives from, per consumer and
    in source order -- `derive_graph` emits exactly one edge per non-mutex
    atom occurrence, in that order, and the bare local n() atoms it turns
    into mutex records instead are skipped here by the same predicate
    (`is_mutex_atom`), never by a restatement of it. An unmatched edge, or a
    non-mutex atom with no edge, raises: a lens that quietly dropped either
    would be drawing a condition it cannot account for (DL-07's spirit)."""
    queues: dict[str, list[int]] = {}
    for index, edge in enumerate(graph.edges):
        queues.setdefault(edge.dst, []).append(index)
    facts: dict[str, dict[str, object]] = {}
    edge_cond: dict[int, _EdgeCond] = {}
    for name, job in catalog.jobs.items():
        queue = queues.get(name, [])
        data: dict[str, object] = {"condition": None, "box_success": None, "box_failure": None}
        shapes: dict[str, CondShape] = {}
        trees: dict[str, object] = {}
        for origin, cond, _span in job.iter_conditions():
            flat = _flatten(cond)
            shape = _shape(flat)
            leaves: list[dict[str, object]] = []
            for atom, branch in zip(iter_atoms(flat), _branch_labels(flat, shape), strict=True):
                lock = is_mutex_atom(origin, atom)
                at = None if lock else _take_edge(queue, graph, atom, f"{name} {origin}")
                # the branch NUMBER restarts per attribute, so the identity the
                # page groups and paints by carries the attribute too: a box
                # whose condition and box_success are both ORs states two
                # different alternations, not one twice (review MAJOR)
                key = None if branch is None else f"{origin}:{branch}"
                if at is not None:
                    edge_cond[at] = _EdgeCond(
                        attr=origin,
                        branch=branch,
                        branch_key=key,
                        suffix="" if branch is None else _canvas_suffix(shape, branch),
                    )
                leaves.append(
                    {
                        "atom": cond_to_source(atom),
                        "edge": None if at is None else edge_ids[at],
                        "branch": branch,
                        "branch_key": key,
                        "lock": lock,
                    }
                )
            data[origin] = cond_to_source(cond)
            shapes[origin] = shape
            trees[origin] = _cond_tree(flat, iter(leaves))
        data["cond_shape"] = shapes
        data["cond_tree"] = trees
        facts[name] = data
    unmatched = [index for queue in queues.values() for index in queue]
    if unmatched:
        stray = graph.edges[unmatched[0]]
        raise ValueError(
            f"{len(unmatched)} derived edge(s) match no condition atom; first"
            f" {stray.src} -> {stray.dst} ({stray.mapping_row})"
        )
    return facts, edge_cond


def _elements(
    catalog: CatalogIR, graph: DerivedGraph, *, collapse_threshold: int | None = None
) -> dict[str, list[dict[str, object]]]:
    """Cytoscape elements for the whole graph. Pure function; deterministic
    for identical input (catalog nodes in source order, EXT nodes in
    first-reference order, edges in derivation order).

    `collapse_threshold` marks the boxes the page folds before its first
    layout (DL-190): a TOP-LEVEL box with more direct members than the
    threshold carries `collapsed: true`, the same rule `viz._anchors` folds
    the report's charts by. A nested box goes with its parent, so it is
    never marked itself. None (the CLI default for this format) marks
    nothing: the page opens on the whole graph, as DL-71 built it."""
    nodes: list[dict[str, object]] = []
    catalog_data: dict[str, dict[str, object]] = {}
    for name in graph.nodes:
        job = catalog.jobs.get(name)
        kind = job_kind(job) or "CMD"
        data: dict[str, object] = {
            "id": name,
            "label": name,
            "kind": kind,
            "schedule": job_schedule(job),
            "detail": job_detail(job),
        }
        catalog_data[name] = data
        parent = graph.box_tree.parent.get(name)
        if parent is not None:
            data["parent"] = parent
        if (
            kind == "BOX"
            and collapse_threshold is not None
            and parent is None
            and len(graph.box_tree.children.get(name, [])) > collapse_threshold
        ):
            data["collapsed"] = True
        nodes.append({"data": data, "classes": _KIND_CLASS.get(kind, "cmd")})

    # The mutex plan decides the lock shapes AND which n() targets dangle:
    # a dangling one becomes an EXT node exactly as a dangling producer does
    # (DL-35a's rule, carried to this page).
    plan = mutex_plan(graph, None)
    self_locked, cliques, pairs = plan
    lock_members = {n for clique in cliques for n in clique} | {n for p in pairs for n in p}
    dangling = [n for n in sorted(lock_members) if n not in catalog.jobs]
    taken_ids = {name for name in catalog_data}
    ext_nodes, ext_id = _ext_nodes(catalog, graph, taken_ids, dangling)
    nodes.extend(ext_nodes)
    for name in self_locked:
        catalog_data[name]["self_lock"] = True

    # cytoscape ids are unique across ALL elements and node ids are raw job
    # names, so a job literally named "e0" would silently swallow an edge at
    # cytoscape init (review finding) -- prefix until the id is free; distinct
    # tails keep prefixed ids distinct from each other.
    node_ids = {node["data"]["id"] for node in nodes}  # type: ignore[index]

    def edge_id(i: int) -> str:
        candidate = f"e{i}"
        while candidate in node_ids:
            candidate = "_" + candidate
        return candidate

    # The condition structure (DL-191) needs the edge ids, so they are
    # assigned before the edges are built; every node the catalog defines
    # then carries its own condition texts, shapes and trees.
    edge_ids = [edge_id(i) for i in range(len(graph.edges))]
    facts, edge_cond = _condition_facts(catalog, graph, edge_ids)
    for name, data in catalog_data.items():
        data.update(facts[name])

    def node_id(name: str) -> str:
        return name if name in catalog.jobs else ext_id[name]

    lock_nodes, lock_links = _lock_elements(
        catalog, graph, plan, node_id, taken_ids | set(edge_ids)
    )
    return {
        "nodes": nodes + lock_nodes,
        "edges": _edge_elements(catalog, graph, edge_ids, edge_cond, ext_id) + lock_links,
    }


def _ext_nodes(
    catalog: CatalogIR, graph: DerivedGraph, taken_ids: set[str], dangling: list[str]
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """The endpoints outside the catalog, in first-reference order, plus the
    cytoscape id each display name was assigned. `dangling` carries the lock
    members no job defines (DL-192), appended after the edge endpoints: a
    dangling n() target is as real a reference as a dangling producer, and
    L001 owns the finding either way.

    Producer locality is decided off the atom's `instance` fact
    (`derive.local_producer`), never off `edge.src`'s membership in
    `graph.nodes` -- a foreign M33 producer's composite display form
    ("name^INST") can be spelled exactly like a local job's own name
    (DL-162a), and a raw membership test folds the two into one cytoscape
    node (DL-175's own deferred seventh S-EDGE site, paid here, DL-176).
    `edge.dst` is always the local consumer whose own condition/box override
    this edge derives from (DerivedEdge.dst's docstring), so it is never a
    candidate for an EXT node and needs no such check."""
    ext_order: list[str] = []
    ext_global: dict[str, bool] = {}
    ext_id: dict[str, str] = {}  # display name -> assigned cytoscape id
    for edge in graph.edges:
        if local_producer(edge, catalog) is not None:
            continue
        endpoint = edge.src
        if endpoint not in ext_global:
            ext_order.append(endpoint)
            ext_global[endpoint] = False
            # the same collision one level down: a foreign producer's raw
            # display form can equal a local job's own id (DL-176's own
            # fixture has a local `foo^PRD`) -- widen with a leading
            # underscore until free, the same idiom `edge_id` uses.
            candidate = endpoint
            while candidate in taken_ids:
                candidate = "_" + candidate
            taken_ids.add(candidate)
            ext_id[endpoint] = candidate
        if edge.via == "global":
            ext_global[endpoint] = True
    for endpoint in dangling:
        if endpoint in ext_global:
            continue  # already an EXT node: one element, both references
        ext_order.append(endpoint)
        ext_global[endpoint] = False
        candidate = endpoint
        while candidate in taken_ids:
            candidate = "_" + candidate
        taken_ids.add(candidate)
        ext_id[endpoint] = candidate
    nodes: list[dict[str, object]] = [
        {
            "data": {
                "id": ext_id[name],
                "label": name,
                "kind": "EXT",
                "schedule": None,
                "detail": None,
            },
            "classes": "ext global" if ext_global[name] else "ext",
        }
        for name in ext_order
    ]
    return nodes, ext_id


def _edge_elements(
    catalog: CatalogIR,
    graph: DerivedGraph,
    edge_ids: list[str],
    edge_cond: dict[int, _EdgeCond],
    ext_id: dict[str, str],
) -> list[dict[str, object]]:
    """One cytoscape edge per derived edge, in derivation order."""

    def src_id(edge: DerivedEdge) -> str:
        """Cytoscape id for this edge's SOURCE endpoint: the resolved local
        producer's own (unnamespaced) id when there is one, else the
        namespaced EXT id `_ext_nodes` assigned -- never the raw `edge.src`
        string, which can collide with a same-spelled local job's id
        (DL-176)."""
        local = local_producer(edge, catalog)
        return local if local is not None else ext_id[edge.src]

    edges: list[dict[str, object]] = []
    for i, edge in enumerate(graph.edges):
        cond = edge_cond[i]
        label = edge_label(edge)
        if cond.suffix:
            # the DL-35 thinning grammar, then which alternation this arrow
            # belongs to: an empty thinned label leaves the suffix alone
            label = f"{label} {cond.suffix}" if label else cond.suffix
        edges.append(
            {
                "data": {
                    "id": edge_ids[i],
                    "source": src_id(edge),
                    "target": edge.dst,
                    "via": edge.via,
                    "lookback": edge.lookback.raw if edge.lookback is not None else None,
                    "cls": edge.cls,
                    "mapping_row": edge.mapping_row,
                    "assumption": edge.assumption,
                    "attr": cond.attr,
                    "branch": cond.branch,
                    "branch_key": cond.branch_key,
                    "label": label,
                },
                # cls stays the style class; `any` is the second, orthogonal
                # one: this arrow is one alternative, not a requirement
                "classes": f"{edge.cls} any" if cond.branch is not None else edge.cls,
            }
        )
    return edges


def to_explore_html(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    title: str = "catalog",
    direction: Direction | Literal["auto"] = "auto",
    collapse_threshold: int | None = None,
) -> str:
    """One self-contained offline HTML page: the whole graph, always ELK,
    fitted to the viewport and zoomed from there, singletons always present
    (search must find them). Boxes open expanded and the operator collapses
    and expands them on the page; `collapse_threshold` folds the report's
    over-threshold top-level boxes before the first picture (DL-190; None
    folds nothing). --direction maps auto/LR -> RIGHT, TD -> DOWN."""
    if graph is None:
        graph = derive_graph(catalog)
    elements = _elements(catalog, graph, collapse_threshold=collapse_threshold)
    boxes = sum(1 for n in elements["nodes"] if n["classes"] == "box")
    # every lock the page DRAWS: one per hub, one per stated pair, one per
    # self badge. The report counts stated exclusions instead (mutex_groups),
    # so a clique of three reads as 3 there and as 1 hub here.
    locks = (
        sum(1 for n in elements["nodes"] if "lock" in str(n["classes"]).split())
        + sum(1 for e in elements["edges"] if "pair" in str(e["classes"]).split())
        + sum(1 for n in elements["nodes"] if cast("dict[str, object]", n["data"]).get("self_lock"))
    )
    unused = len(unused_resources(catalog))
    # the DEPENDENCY edges: lock links share the element list and are not
    # dependencies -- the locks count below carries them
    flow_edges = sum(1 for e in elements["edges"] if "lock" not in str(e["classes"]).split())
    summary = (
        f"{len(graph.nodes)} jobs \N{MIDDLE DOT} {flow_edges} edges"
        f" \N{MIDDLE DOT} {boxes} boxes \N{MIDDLE DOT} {locks} locks"
        + (f" \N{MIDDLE DOT} {unused} unused" if unused else "")
    )
    payload = json.dumps(elements).replace("<", "\\u003c")

    package = files("dsl41")
    template = (package / "templates" / "viz_explore.html").read_text(encoding="utf-8")
    return substitute(
        template,
        {
            "__DSL41_TITLE__": html.escape(title),
            "__DSL41_SUMMARY__": html.escape(summary),
            "__DSL41_ELK_DIRECTION__": "DOWN" if direction == "TD" else "RIGHT",
            "__DSL41_ELEMENTS_JSON__": payload,
            "__DSL41_CUSTOM_ELEMENTS_JS__": (
                package / "_vendor" / "custom-elements.min.js"
            ).read_text(encoding="utf-8"),
            "__DSL41_CYTOSCAPE_JS__": (
                package / "_vendor" / "cytoscape-explore.iife.min.js"
            ).read_text(encoding="utf-8"),
        },
    )
