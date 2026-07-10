"""IR-G derivation: pure analysis passes IR-F -> DerivedGraph (ir-design ss5).

Phase 5 of the implementation order (CLAUDE.md / DL-03). IR-G is derived,
never authoritative: regenerate from IR-F, never edit or persist as truth
(ir-design ss1). Explicitly lossy -- but every loss is materialized as an
annotation. Every A-classified edge records its assumption; R-classified
edges are what the UC backend will refuse to compile (stonebranch Part II
requirement 1); mapping rows come from the M01-M36 table.

Decisions pinned here (each with a test):
- Global atoms become edges with src = the global variable name and
  via="global" (the ss5 sketch's via Literal includes it). Globals are NOT
  listed in `nodes`; consumers must treat via=="global" srcs as pseudo-nodes.
- Cross-instance refs yield BOTH an external_boundary entry and a redesign
  edge (M33 from `condition`, M16 from box overrides), so the migration
  report can be generated uniformly by iterating edges.
- Edges to jobs missing from the catalog are kept (src not in `nodes`),
  classified REDESIGN on row M02 with the brokenness named in the
  assumption field (DL-12: compiling an A-row edge to a nonexistent vertex
  would be silent loss); the L001 linter rule owns the loud finding.
- Local UNQUALIFIED n() atoms in `condition` become mutex candidate PAIRS
  (M07), sorted and deduped, never merged into connected components: mutual
  exclusion is not transitive, and pairs are exactly what the JIL states.
  n(self) becomes a single-element group (self-serialization; UC Instance
  Wait, UCS-09). n() with a lookback stays an edge (M03; mutex_groups
  cannot carry the qualifier -- DL-12); n() under box_success/box_failure
  or with a cross-instance ref stays an edge (it is a completion predicate
  there, not a start gate).
- Same-cycle detector (M01 vs M02): a job's trigger cadence is its own
  schedule signature (trigger fields only -- run_window is a gate per SEM-33
  and must_* are alarms per SEM-34, both excluded), else the nearest box
  ancestor's, else the unanimous cadence of its condition predecessors
  (fixpoint); FW jobs with no schedule are their own source cadence. Same
  cycle == same top-level box, or equal cadence with BOTH jobs unboxed
  (DL-12: two identically scheduled boxes are two UC workflows -- a
  signature collision is not a stream). Unknown-vs-anything -> NOT same
  cycle (M02, conservative).
- Box-override refs derive edges too (ir-design D1, resolved per its own
  "probably yes"): ref TRANSITIVELY inside the box (SEM-12 "inside") ->
  M15 assumed; non-member, global, or cross-instance ref -> M16 redesign
  (SEM-12 hung-RUNNING).
- DerivedEdge.source_atom is Optional (spans are Optional codebase-wide;
  the ss5 sketch's bare SourceSpan is read as "populated whenever lowering
  supplied one" -- derive always populates it for parsed catalogs).
- assumption field: required for assumed, forbidden for exact, optional for
  redesign (ss5's "mandatory iff assumed" read as one-directional so R rows
  may carry context notes).
- Structural passes (chains, parallel groups, cycles) run over local
  job->job edges of `condition` origin only: box-override edges describe
  completion folding, not flow, and would fabricate cycles out of normal
  box behavior.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal, NamedTuple

from pydantic import BaseModel, model_validator

from dsl41.ast_jil import SourceSpan
from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    GlobalAtom,
    JobRef,
    Lookback,
    Or,
    Paren,
    StatusAtom,
    iter_atoms,
)
from dsl41.ir import CatalogIR, FwSpec, ScheduleBlock

EdgeClass = Literal["exact", "assumed", "redesign"]  # E/A/R from the mapping table
Via = Literal["success", "failure", "done", "terminated", "notrunning", "exitcode", "global"]

_STATUS_TO_VIA: dict[str, Via] = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "DONE": "done",
    "TERMINATED": "terminated",
    "NOTRUNNING": "notrunning",
}


class DerivedEdge(BaseModel):
    src: str  # producer job name, global name (via=="global"), or "name^INST"
    dst: str  # consumer: the job whose condition/box override references src
    via: Via
    lookback: Lookback | None = None
    cls: EdgeClass
    mapping_row: str  # "M01".."M36"
    assumption: str | None = None  # human-readable; required iff cls=="assumed"
    source_atom: SourceSpan | None = None  # provenance: owning attr's span

    @model_validator(mode="after")
    def _assumption_matches_class(self) -> DerivedEdge:
        if self.cls == "assumed" and not self.assumption:
            raise ValueError("assumed edge requires a recorded assumption (ir-design ss5)")
        if self.cls == "exact" and self.assumption is not None:
            raise ValueError("exact edge must not carry an assumption")
        return self


class OrShape(BaseModel):
    """M12 classifier output for one Or node (UCS-03 lowering patterns).

    Lowering suggestions assume UC has no native OR-join.
    PENDING: U1 -- if 7.x native "Any" completion criteria exist, the
    suggestion texts (and possibly the whole M12 lowering) change.
    """

    job: str  # the consumer whose condition/box override contains the Or
    attr: Literal["condition", "box_success", "box_failure"] = "condition"
    kind: Literal["common_ancestor", "independent", "mixed"]
    branches: list[list[str]]  # per operand: local job refs it mentions
    lowering: str  # suggested M12 lowering choice
    span: SourceSpan | None = None


class BoxTree(BaseModel):
    """Materialized from JobIR.box.box_name; soundness is guaranteed by the
    CatalogIR validator (exists / is-box / acyclic), so no re-checking here."""

    roots: list[str] = []  # top-level boxes, catalog order
    children: dict[str, list[str]] = {}  # box -> members, catalog order
    parent: dict[str, str] = {}  # member -> immediate box

    def top(self, name: str) -> str | None:
        """Root box of `name`'s containment chain; the box itself if it is a
        top-level box; None for jobs outside any box."""
        current = name
        seen_box = current in self.children and current not in self.parent
        while (up := self.parent.get(current)) is not None:
            current = up
            seen_box = True
        return current if (seen_box or current != name) else None


class RedesignFlag(BaseModel):
    """Pass-6 output: per-job R-classified constructs that are not edges."""

    job: str
    mapping_row: str  # e.g. "M27"
    reason: str
    span: SourceSpan | None = None


class DerivedGraph(BaseModel):
    nodes: list[str] = []  # catalog job names, source order
    edges: list[DerivedEdge] = []
    mutex_groups: list[list[str]] = []  # n() detector pairs (M07)
    or_shapes: list[OrShape] = []  # M12 classifier output
    box_tree: BoxTree = BoxTree()
    external_boundary: list[JobRef] = []  # cross-instance refs (M33)
    # ss5 pass-6/7 outputs (sketch completion, documented):
    redesign_flags: list[RedesignFlag] = []  # M27 run_window v1
    chains: list[list[str]] = []  # maximal linear chains (feeds DSL decompiler)
    parallel_groups: list[list[str]] = []  # same-(preds,succs) sibling groups
    cycles: list[list[str]] = []  # SCCs >1 + self-loops, sorted node sets (L010)


# ---------------------------------------------------------------------- extraction


class _RawRef(NamedTuple):
    """One atom occurrence, pre-classification (pass 1)."""

    dst: str
    origin: Literal["condition", "box_success", "box_failure"]
    atom: StatusAtom | ExitCodeAtom | GlobalAtom
    span: SourceSpan | None


def _extract_refs(catalog: CatalogIR) -> list[_RawRef]:
    refs: list[_RawRef] = []
    for job in catalog.jobs.values():
        for origin, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                refs.append(_RawRef(dst=job.name, origin=origin, atom=atom, span=span))
    return refs


def _is_mutex_ref(ref: _RawRef) -> bool:
    """Pass 2 predicate: local UNQUALIFIED n() in `condition` is a mutex
    candidate, not an edge (M07 / dossier R6: translating them as edges
    creates false ordering). A lookback-qualified n() stays an edge (M03):
    the instantaneous mutual-exclusion reading applies only to the bare
    form, and mutex_groups cannot carry the qualifier -- dropping it would
    be silent loss (DL-12)."""
    return (
        ref.origin == "condition"
        and isinstance(ref.atom, StatusAtom)
        and ref.atom.status == "NOTRUNNING"
        and ref.atom.job.instance is None
        and ref.atom.lookback is None
    )


def _mutex_groups(refs: list[_RawRef]) -> list[list[str]]:
    groups: set[tuple[str, ...]] = set()
    for ref in refs:
        if _is_mutex_ref(ref):
            assert isinstance(ref.atom, StatusAtom)
            other = ref.atom.job.name
            pair = (ref.dst,) if other == ref.dst else tuple(sorted((ref.dst, other)))
            groups.add(pair)
    return [list(g) for g in sorted(groups)]


# ------------------------------------------------------------- cadence (pass 3 core)

_TRIGGER_FIELDS = (
    "days_of_week",
    "run_calendar",
    "exclude_calendar",
    "start_times",
    "start_mins",
    "timezone",
)


def _trigger_signature(schedule: ScheduleBlock) -> str:
    """Comparable trigger-cadence key: trigger fields only (run_window is a
    gate per SEM-33; must_* are alarms per SEM-34; both excluded)."""
    dump = schedule.model_dump(mode="json")
    return "sched:" + json.dumps({k: dump[k] for k in _TRIGGER_FIELDS}, sort_keys=True)


def _condition_pred_map(catalog: CatalogIR, refs: list[_RawRef]) -> dict[str, set[str]]:
    """dst -> local job srcs of its `condition` (mutex refs excluded; global
    atoms excluded -- SET_GLOBAL events carry no cadence)."""
    preds: dict[str, set[str]] = {name: set() for name in catalog.jobs}
    for ref in refs:
        if ref.origin != "condition" or _is_mutex_ref(ref) or isinstance(ref.atom, GlobalAtom):
            continue
        if ref.atom.job.instance is None:
            preds[ref.dst].add(ref.atom.job.name)
    return preds


def _cadences(catalog: CatalogIR, cond_preds: dict[str, set[str]]) -> dict[str, str | None]:
    cadence: dict[str, str | None] = {}
    for name, job in catalog.jobs.items():
        if job.schedule is not None:
            cadence[name] = _trigger_signature(job.schedule)
        elif isinstance(job.exec_, FwSpec):
            cadence[name] = f"fw:{name}"  # a file watcher is its own source cadence
        else:
            cadence[name] = None
    changed = True
    while changed:  # fixpoint; bounded by |jobs| improvements
        changed = False
        for name, job in catalog.jobs.items():
            if cadence[name] is not None:
                continue
            inherited: str | None = None
            box = job.box.box_name
            if box is not None and cadence.get(box) is not None:
                inherited = cadence[box]  # member runs when its box runs (SEM-10)
            else:
                pred_cadences = {cadence.get(p) for p in cond_preds[name]}
                if len(pred_cadences) == 1 and (only := next(iter(pred_cadences))) is not None:
                    inherited = only
            if inherited is not None:
                cadence[name] = inherited
                changed = True
    return cadence


def _same_cycle(src: str, dst: str, tree: BoxTree, cadence: dict[str, str | None]) -> bool:
    top_src, top_dst = tree.top(src), tree.top(dst)
    if top_src is not None or top_dst is not None:
        # Boxes are streams: same top-level box == one cycle; different (or
        # only one) box == different UC workflows even under identical
        # schedules -- a trigger-signature collision is not a stream (M14
        # note: member conditions referencing jobs outside the box -> M02).
        return top_src == top_dst and top_src is not None
    return cadence.get(src) is not None and cadence.get(src) == cadence.get(dst)


def _is_inside(tree: BoxTree, job: str, box: str) -> bool:
    """SEM-12 'inside the box' is transitive: any ancestor container counts."""
    current = job
    while (up := tree.parent.get(current)) is not None:
        if up == box:
            return True
        current = up
    return False


# --------------------------------------------------------- edge classification (pass 3)


def _classify_condition_edge(
    ref: _RawRef,
    catalog: CatalogIR,
    tree: BoxTree,
    cadence: dict[str, str | None],
) -> DerivedEdge:
    atom = ref.atom
    assert not isinstance(atom, GlobalAtom)
    src = atom.job.name
    via: Via = "exitcode" if isinstance(atom, ExitCodeAtom) else _STATUS_TO_VIA[atom.status]
    lookback = atom.lookback

    def edge(cls: EdgeClass, row: str, assumption: str | None = None) -> DerivedEdge:
        return DerivedEdge(
            src=src,
            dst=ref.dst,
            via=via,
            lookback=lookback,
            cls=cls,
            mapping_row=row,
            assumption=assumption,
            source_atom=ref.span,
        )

    if src not in catalog.jobs:
        # redesign, not assumed: compiling an A-row edge to a nonexistent
        # vertex would be silent loss; L001 carries the error (DL-12).
        return edge(
            "redesign",
            "M02",
            "producer is not defined in the catalog (SEM-06: atom is permanently"
            " false; see L001) -- latching semantics cannot be assessed",
        )
    if lookback is not None:
        assumption = (
            "lookback window compiles to a Task Monitor Time Scope; window anchoring"
            " differs per case (SEM-04)"
        )
        if lookback.kind == "zero":
            # PENDING: Q2 -- zero-lookback anchoring unresolved; flag every use.
            assumption += "; zero-lookback anchoring is unresolved (Q2)"
        # PENDING: U5 -- Time Scope bounds on Task Monitors not yet pinned.
        return edge("assumed", "M03", assumption)
    if via == "success":
        if _same_cycle(src, ref.dst, tree, cadence):
            return edge(
                "assumed",
                "M01",
                "producer and consumer share one trigger cadence; assumes no"
                " cross-run staleness is relied upon (SEM-01/R1)",
            )
        return edge(
            "assumed",
            "M02",
            "cross-stream latching dependency: compiles to a Task Monitor;"
            " Time Scope bounds differ from an indefinite latch (UCS-06)",
        )
    if via == "failure":
        return edge("exact", "M04")
    if via == "done":
        return edge("exact", "M05")
    if via == "terminated":
        return edge(
            "assumed",
            "M06",
            "UC separates Cancelled from Failed; t() maps to the Cancelled-"
            "inclusive reading -- document the choice per estate",
        )
    if via == "exitcode":
        # PENDING: U4 -- per-task-type exit-code -> status mechanism not pinned.
        return edge(
            "assumed",
            "M08",
            "exit-code comparison compiles to an edge variable condition or a"
            " task-level exit-code mapping; exact mechanism per task type (U4)",
        )
    if via == "notrunning":
        # Reachable only with a lookback (bare local n() is mutex-classified
        # and never enters here), and the lookback branch above returns M03
        # first -- keep a loud guard rather than dead fall-through.
        raise AssertionError("unqualified notrunning atom escaped mutex classification")
    raise AssertionError(f"unclassified local condition atom via={via!r}")


def _edge_for_ref(
    ref: _RawRef,
    catalog: CatalogIR,
    tree: BoxTree,
    cadence: dict[str, str | None],
    boundary: list[JobRef],
) -> DerivedEdge:
    atom = ref.atom
    if isinstance(atom, GlobalAtom):
        if ref.origin != "condition":
            return DerivedEdge(
                src=atom.name,
                dst=ref.dst,
                via="global",
                cls="redesign",
                mapping_row="M16",
                assumption=(
                    "box completion gated on a global variable: external-reference"
                    " gating has no UC analog (SEM-12 hung-RUNNING pattern)"
                ),
                source_atom=ref.span,
            )
        # PENDING: U8 -- Set Variable Actions ordering property not recorded yet.
        return DerivedEdge(
            src=atom.name,
            dst=ref.dst,
            via="global",
            cls="assumed",
            mapping_row="M09",
            assumption=(
                "AutoSys re-evaluates on SET_GLOBAL events; UC edge variable"
                " conditions evaluate at predecessor completion -- redesign if the"
                " global is used as an async gate (UCS-08)"
            ),
            source_atom=ref.span,
        )
    if atom.job.instance is not None:
        boundary.append(atom.job)
        via = "exitcode" if isinstance(atom, ExitCodeAtom) else _STATUS_TO_VIA[atom.status]
        if ref.origin != "condition":
            row, why = (
                "M16",
                "box completion gated on a cross-instance job: external-reference"
                " gating has no UC analog (SEM-12 hung-RUNNING pattern)",
            )
        else:
            row, why = (
                "M33",
                "producer lives on an external instance; consolidating instances is"
                " a migration design decision, not a translation (SEM-07)",
            )
        return DerivedEdge(
            src=f"{atom.job.name}^{atom.job.instance}",
            dst=ref.dst,
            via=via,
            lookback=atom.lookback,
            cls="redesign",
            mapping_row=row,
            assumption=why,
            source_atom=ref.span,
        )
    if ref.origin != "condition":
        via = "exitcode" if isinstance(atom, ExitCodeAtom) else _STATUS_TO_VIA[atom.status]
        src = atom.job.name
        if _is_inside(tree, src, ref.dst):
            # PENDING: U2 -- exact workflow-status derivation rules not pinned.
            return DerivedEdge(
                src=src,
                dst=ref.dst,
                via=via,
                lookback=atom.lookback,
                cls="assumed",
                mapping_row="M15",
                assumption=(
                    f"{ref.origin} references member {src!r}: early-exit completion"
                    " override needs explicit Skip-path restructuring (UCS-04/U2)"
                ),
                source_atom=ref.span,
            )
        return DerivedEdge(
            src=src,
            dst=ref.dst,
            via=via,
            lookback=atom.lookback,
            cls="redesign",
            mapping_row="M16",
            assumption=(
                f"{ref.origin} references {src!r}, which is not a member of"
                f" {ref.dst!r}: hung-RUNNING external gate (SEM-12), no UC analog"
            ),
            source_atom=ref.span,
        )
    return _classify_condition_edge(ref, catalog, tree, cadence)


# ------------------------------------------------------------- OR shapes (pass 4)


def _iter_or_nodes(cond: Cond) -> list[Or]:
    out: list[Or] = []
    stack: list[Cond] = [cond]
    while stack:
        node = stack.pop()
        if isinstance(node, Or):
            out.append(node)
            stack.extend(reversed(node.operands))
        elif isinstance(node, And):
            stack.extend(reversed(node.operands))
        elif isinstance(node, Paren):
            stack.append(node.inner)
    return out


def _branch_local_srcs(operand: Cond, catalog: CatalogIR) -> list[str]:
    srcs: list[str] = []
    for atom in iter_atoms(operand):
        if isinstance(atom, GlobalAtom) or atom.job.instance is not None:
            continue
        if atom.job.name not in srcs:
            srcs.append(atom.job.name)
    return srcs


def _ancestor_sets(roots: Iterable[str], preds: dict[str, set[str]]) -> dict[str, set[str]]:
    """root -> {root} + everything reachable backwards over condition edges.

    Computed ONLY for the requested roots (the Or-branch producers _or_shapes
    compares) and iteratively -- the previous version walked every catalog
    job recursively, which was Theta(n^2) memory on chain-shaped estates
    (the `dsl41 lint` OOM kill) and blew the Python stack when a long chain
    was declared consumer-first (DL-20). Cycles are fine: each BFS visits a
    node once, and unlike the old memoized recursion the closures it returns
    are complete on cyclic graphs regardless of visit order."""
    out: dict[str, set[str]] = {}
    for root in roots:
        seen = {root}
        stack = [root]
        while stack:
            for p in preds.get(stack.pop(), ()):  # undefined srcs have no preds
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        out[root] = seen
    return out


def _or_shapes(catalog: CatalogIR, preds: dict[str, set[str]]) -> list[OrShape]:
    # PENDING: U1 -- lowering texts assume no native OR-join (see OrShape).
    # Two passes: collect every Or node's branches first, so ancestor sets
    # are computed only for the branch producers that need them (DL-20 --
    # a catalog with no `|` pays nothing here).
    found: list[
        tuple[
            str,
            Literal["condition", "box_success", "box_failure"],
            SourceSpan | None,
            list[list[str]],
        ]
    ] = []
    roots: set[str] = set()
    for job in catalog.jobs.values():
        for attr, cond, span in job.iter_conditions():
            for or_node in _iter_or_nodes(cond):
                branches = [_branch_local_srcs(op, catalog) for op in or_node.operands]
                found.append((job.name, attr, span, branches))
                if all(branches):
                    roots.update(src for branch in branches for src in branch)
    ancestors = _ancestor_sets(roots, preds)
    shapes: list[OrShape] = []
    for job_name, attr, span, branches in found:
        if any(not b for b in branches):
            kind: Literal["common_ancestor", "independent", "mixed"] = "mixed"
            lowering = (
                "per-case M12 decision: at least one branch has no local job"
                " producer (global/cross-instance atoms)"
            )
        else:
            branch_ancestors = [set().union(*(ancestors.get(s, {s}) for s in b)) for b in branches]
            # undefined names cannot anchor a restructure (L001 owns
            # the finding); a diamond over one is independent here
            common = set.intersection(*branch_ancestors) & set(catalog.jobs)
            if common:
                kind = "common_ancestor"
                lowering = (
                    "restructure as conditional paths from common ancestor(s)"
                    f" {sorted(common)} (UCS-03 pattern a)"
                )
            else:
                kind = "independent"
                lowering = (
                    "Task Monitor OR-listener or duplicate the successor per"
                    " branch (UCS-03 patterns b/c)"
                )
        shapes.append(
            OrShape(
                job=job_name,
                attr=attr,
                kind=kind,
                branches=branches,
                lowering=lowering,
                span=span,
            )
        )
    return shapes


# ------------------------------------------------------- structural passes (pass 7)


def _local_condition_adjacency(
    catalog: CatalogIR, edges: list[DerivedEdge]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(preds, succs) over deduped local job->job `condition` edges only
    (module docstring: box-override edges describe completion folding)."""
    preds: dict[str, set[str]] = {n: set() for n in catalog.jobs}
    succs: dict[str, set[str]] = {n: set() for n in catalog.jobs}
    for e in edges:
        if e.mapping_row in ("M15", "M16"):
            continue  # box-override edges
        if e.via == "global" or e.src not in catalog.jobs:
            continue
        preds[e.dst].add(e.src)
        succs[e.src].add(e.dst)
    return preds, succs


def _cycles(nodes: list[str], succs: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCCs (iterative); SCCs of size >1 plus self-loops, as sorted
    node lists, list sorted for determinism. Legal AutoSys, L010 warns."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    result: list[list[str]] = []

    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, list[str], int]] = [(root, sorted(succs[root]), 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children, child_i = work.pop()
            if child_i < len(children):
                work.append((node, children, child_i + 1))
                child = children[child_i]
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(succs[child]), 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                if len(scc) > 1 or node in succs[node]:
                    result.append(sorted(scc))
    return sorted(result)


def _chains(
    nodes: list[str],
    preds: dict[str, set[str]],
    succs: dict[str, set[str]],
    cycle_members: set[str],
) -> list[list[str]]:
    """Maximal linear chains: consecutive (a, b) where b is a's only successor
    and a is b's only predecessor. Length >= 2 reported, source order. Cycle
    members are excluded -- a chain inside an SCC would double-represent
    those nodes to the DSL decompiler (the cycle report owns them)."""

    def linked(a: str, b: str) -> bool:
        return succs[a] == {b} and preds[b] == {a}

    chains: list[list[str]] = []
    in_chain: set[str] = set(cycle_members)
    for node in nodes:
        if node in in_chain:
            continue
        nxt = next(iter(succs[node])) if len(succs[node]) == 1 else None
        starts = nxt is not None and linked(node, nxt)
        prev = next(iter(preds[node])) if len(preds[node]) == 1 else None
        continues = prev is not None and linked(prev, node)
        if not starts or continues:
            continue  # not a chain head
        chain = [node]
        current = node
        while len(succs[current]) == 1:
            nxt = next(iter(succs[current]))
            if not linked(current, nxt) or nxt in in_chain or nxt == chain[0]:
                break
            chain.append(nxt)
            current = nxt
        if len(chain) >= 2:
            chains.append(chain)
            in_chain.update(chain)
    return chains


def _parallel_groups(
    nodes: list[str], preds: dict[str, set[str]], succs: dict[str, set[str]]
) -> list[list[str]]:
    """Sibling groups with identical (preds, succs) signatures, at least one
    side nonempty -- antichains sharing the same fan-out and/or fan-in point
    (source-only groups share a sink; sink-only groups share a source)."""
    by_signature: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for node in nodes:
        if not preds[node] and not succs[node]:
            continue
        key = (tuple(sorted(preds[node])), tuple(sorted(succs[node])))
        by_signature.setdefault(key, []).append(node)
    return sorted(group for group in by_signature.values() if len(group) >= 2)


# ----------------------------------------------------------------------- the driver


def _build_box_tree(catalog: CatalogIR) -> BoxTree:
    tree = BoxTree()
    for name, job in catalog.jobs.items():
        if job.job_type == "BOX":
            tree.children.setdefault(name, [])
        box = job.box.box_name
        if box is not None:
            tree.parent[name] = box
            tree.children.setdefault(box, []).append(name)
    tree.roots = [b for b in tree.children if b not in tree.parent]
    return tree


def derive_graph(catalog: CatalogIR) -> DerivedGraph:
    """Pure IR-F -> IR-G derivation, ss5 passes 1-7 in order."""
    tree = _build_box_tree(catalog)
    refs = _extract_refs(catalog)  # pass 1
    mutex_groups = _mutex_groups(refs)  # pass 2
    cond_preds = _condition_pred_map(catalog, refs)
    cadence = _cadences(catalog, cond_preds)  # pass 3 core
    boundary: list[JobRef] = []
    edges = [
        _edge_for_ref(ref, catalog, tree, cadence, boundary)  # passes 3 + 5
        for ref in refs
        if not _is_mutex_ref(ref)
    ]
    or_shapes = _or_shapes(catalog, cond_preds)  # pass 4 (ancestors computed inside)
    redesign_flags = [
        RedesignFlag(
            job=job.name,
            mapping_row="M27",
            reason=(
                "run_window is a gate with a closer-edge rule (SEM-33/R5); no UC"
                " analog -- redesign per job"
            ),
            span=job.span,
        )
        for job in catalog.jobs.values()  # pass 6
        if job.schedule is not None and job.schedule.run_window is not None
    ]
    nodes = list(catalog.jobs)
    preds, succs = _local_condition_adjacency(catalog, edges)
    deduped_boundary: list[JobRef] = []
    for ref_ in boundary:
        if ref_ not in deduped_boundary:
            # copy: IR-G must not alias IR-F's mutable condition AST nodes
            deduped_boundary.append(ref_.model_copy())
    cycles = _cycles(nodes, succs)  # pass 7
    return DerivedGraph(
        nodes=nodes,
        edges=edges,
        mutex_groups=mutex_groups,
        or_shapes=or_shapes,
        box_tree=tree,
        external_boundary=deduped_boundary,
        redesign_flags=redesign_flags,
        chains=_chains(nodes, preds, succs, {n for scc in cycles for n in scc}),
        parallel_groups=_parallel_groups(nodes, preds, succs),
        cycles=cycles,
    )
