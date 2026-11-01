"""Read-only job navigation over derived IR-G (ir-design ss5)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from dsl41.conditions import GlobalAtom, JobRef, StatusAtom
from dsl41.derive import DerivedGraph
from dsl41.dsl import cond_to_source

ALIASES = {"upstream": "fanins", "downstream": "fanouts"}
VERBS = (
    "fanins",
    "fanouts",
    "box",
    "children",
    "siblings",
    "ancestors",
    "descendants",
    "path",
    "show",
)
DEPENDENCY_VERBS = {"fanins", "fanouts", "ancestors", "descendants", "path"}
type Node = tuple[str, bool]
type Adjacency = dict[Node, dict[Node, set[str]]]


class NavNotFound(LookupError):
    """A job or dependency path is absent from the selected graph."""


@dataclass(frozen=True)
class Match:
    name: str
    atom: str = ""


def validate_query(
    verb: str,
    target: str | None = None,
    *,
    depth: int | None = None,
    deep: bool = False,
    include_all: bool = False,
    explain: bool = False,
) -> str:
    """Return the canonical verb, or reject a usage error before loading an estate."""
    verb = ALIASES.get(verb, verb)
    if verb not in VERBS:
        raise ValueError(f"unknown nav verb {verb!r}; choose {', '.join(VERBS)}")
    if (verb == "path") != (target is not None):
        raise ValueError("path needs two job names; every other verb needs one")
    if depth is not None and (depth < 0 or verb not in {"ancestors", "descendants"}):
        raise ValueError("--depth needs a nonnegative integer and ancestors or descendants")
    if deep and verb != "children":
        raise ValueError("--deep needs children")
    if include_all and verb not in DEPENDENCY_VERBS:
        raise ValueError("--all needs a dependency verb")
    if explain and verb == "show":
        raise ValueError("--explain needs a relationship or path")
    return verb


def _adjacency(graph: DerivedGraph, *, reverse: bool, include_all: bool) -> Adjacency:
    nodes = set(graph.nodes)
    adjacency: Adjacency = {(name, False): {} for name in nodes}

    def add(source: Node, destination: Node, atom: str) -> None:
        if reverse:
            source, destination = destination, source
        adjacency.setdefault(source, {}).setdefault(destination, set()).add(atom)
        adjacency.setdefault(destination, {})

    for edge in graph.edges:
        if not edge.is_start_gate or isinstance(edge.atom, GlobalAtom):
            continue
        external = edge.atom.job.instance is not None or edge.atom.job.name not in nodes
        if external and not include_all:
            continue
        # Cross-instance display names may collide with local names (DL-162a).
        add((edge.src, external), (edge.dst, False), cond_to_source(edge.atom))
    if include_all:
        for consumer, producers in graph.bare_notrunning.items():
            for producer in producers:
                atom = StatusAtom(job=JobRef(name=producer), status="NOTRUNNING")
                add((producer, producer not in nodes), (consumer, False), cond_to_source(atom))
    return adjacency


def _node(adjacency: Adjacency, name: str) -> Node:
    for external in (False, True):
        if (name, external) in adjacency:
            return name, external
    raise NavNotFound(f"job {name!r} not found")


def _walk(adjacency: Adjacency, start: Node, depth: int | None) -> dict[Node, tuple[Node, str]]:
    """One breadth-first tree, with sorted ties and the atom on each discovery edge."""
    seen = {start}
    found: dict[Node, tuple[Node, str]] = {}
    pending = deque([(start, 0)])
    while pending:
        current, distance = pending.popleft()
        if depth is not None and distance >= depth:
            continue
        for neighbor, atoms in sorted(adjacency[current].items()):
            if neighbor in seen:
                continue
            seen.add(neighbor)
            found[neighbor] = current, "; ".join(sorted(atoms))
            pending.append((neighbor, distance + 1))
    return found


def _matches(found: dict[Node, tuple[Node, str]]) -> list[Match]:
    atoms: dict[str, set[str]] = {}
    for (name, _), (_, atom) in found.items():
        atoms.setdefault(name, set()).add(atom)
    return [Match(name, "; ".join(sorted(atoms[name]))) for name in sorted(atoms)]


def navigate(
    graph: DerivedGraph,
    verb: str,
    job: str,
    target: str | None = None,
    *,
    depth: int | None = None,
    deep: bool = False,
    include_all: bool = False,
) -> list[Match]:
    """Select sorted job names, or one shortest path in dependency order.

    Fanins are prerequisites; fanouts are consumers. Only condition-origin job edges
    participate. Mutex and unresolved references need include_all (ir-design ss5).
    """
    verb = validate_query(verb, target, depth=depth, deep=deep, include_all=include_all)
    if verb in DEPENDENCY_VERBS:
        adjacency = _adjacency(
            graph, reverse=verb in {"fanins", "ancestors"}, include_all=include_all
        )
        start = _node(adjacency, job)
        if verb in {"fanins", "fanouts"}:
            return _matches(
                {
                    node: (start, "; ".join(sorted(atoms)))
                    for node, atoms in adjacency[start].items()
                }
            )
        found = _walk(adjacency, start, depth)
        if verb != "path":
            return _matches(found)
        assert target is not None
        end = _node(adjacency, target)
        path: list[Match] = []
        current = end
        while current != start:
            if current not in found:
                raise NavNotFound(f"no dependency path from {job!r} to {target!r}")
            previous, atom = found[current]
            path.append(Match(current[0], atom))
            current = previous
        return [Match(job), *reversed(path)]
    if job not in graph.nodes:
        raise NavNotFound(f"job {job!r} not found")
    tree = graph.box_tree
    parent = tree.parent.get(job)
    if verb == "box":
        return [] if parent is None else [Match(parent, f"box_name: {parent}")]
    if verb == "siblings":
        return (
            []
            if parent is None
            else [
                Match(name, f"box_name: {parent}")
                for name in sorted(tree.children[parent])
                if name != job
            ]
        )
    if verb == "children":
        found_members: dict[str, Match] = {}
        pending = deque([job])
        while pending:
            box = pending.popleft()
            for name in tree.children.get(box, []):
                if name in found_members:
                    continue
                found_members[name] = Match(name, f"box_name: {box}")
                if deep:
                    pending.append(name)
        return [found_members[name] for name in sorted(found_members)]
    raise ValueError("show needs the IR-F definition; use the DSL renderer")
