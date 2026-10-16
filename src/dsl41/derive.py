"""IR-G derivation: pure analysis passes IR-F -> DerivedGraph (ir-design ss5).

IR-G is derived, never authoritative: regenerate it from IR-F (ir-design ss1, DL-03). Every loss is
materialized as an annotation. Every A-classified edge records its assumption; R-classified
edges are what the UC backend refuses to compile; mapping rows come from the M01-M36 table.

Contracts:
- A global atom is an edge with src = the variable name and via="global". Globals are not in
  `nodes`; a consumer treats via=="global" srcs as pseudo-nodes.
- A cross-instance ref yields an external_boundary entry and a redesign edge (M33 from
  `condition`, M16 from box overrides).
- An edge to a job missing from the catalog is kept (src not in `nodes`) as REDESIGN on M02,
  with the brokenness in the assumption (DL-12); L001 owns the loud finding.
- Local unqualified n() atoms in `condition` are mutex candidate pairs (M07), sorted and
  deduped, never merged into components: mutual exclusion is not transitive. n(self) is a
  single-element group (UCS-09). n() with a lookback stays an edge (M03; DL-12). n() under
  box_success/box_failure or with a cross-instance ref stays an edge: it is a completion
  predicate there.
- The same-cycle detector uses a job's own schedule trigger signature (run_window is a gate,
  SEM-33; must_* are alarms, SEM-34; both excluded), else the nearest box ancestor's, else the
  unanimous cadence of its condition predecessors (fixpoint); an unscheduled FW job is its own
  cadence. Same cycle means the same top-level box, or equal cadence with both jobs unboxed
  (DL-12). Unknown cadence is not the same cycle (M02). The detector splits M01 from M02 and
  picks the M04/M05 assumption flavor (DL-153): f()/d() latch like s(), so a same-stream edge
  carries the staleness assumption and a cross-stream one the Task-Monitor assumption (UCS-13).
- Box-override refs derive edges (ir-design D1): a ref transitively inside the box (SEM-12) is
  M15 assumed; a non-member, global or cross-instance ref is M16 redesign (SEM-12 hung-RUNNING).
- DerivedEdge.source_atom is Optional; derive populates it for parsed catalogs.
- DerivedEdge.atom is a copy of the condition node (DL-73), so a consumer reads op/value for
  M08 exit codes and M09 globals off the edge. IR-G never aliases IR-F's mutable AST nodes;
  `lookback` is copied the same way.
- `via` and the atom kind agree (global iff GlobalAtom, exitcode iff ExitCodeAtom); the model
  validates it because the UC backend narrows on it (DL-73).
- `assumption` is required for assumed, forbidden for exact, optional for redesign (ss5).
- Structural passes (chains, parallel groups, cycles) read local job->job `condition` edges
  only; box-override edges describe completion folding, not flow.
- `components` is the one connectivity implementation (viz and the UC workflow split); box
  co-membership is its named argument (DL-72).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel, field_validator, model_validator

from dsl41.ast_jil import SourceSpan
from dsl41.conditions import (
    And,
    Atom,
    Cond,
    ExitCodeAtom,
    GlobalAtom,
    JobRef,
    Lookback,
    Or,
    Paren,
    Status,
    StatusAtom,
    iter_atoms,
)
from dsl41.ir import CatalogIR, FwSpec, ScheduleBlock

EdgeClass = Literal["exact", "assumed", "redesign"]  # E/A/R from the mapping table
Via = Literal["success", "failure", "done", "terminated", "notrunning", "exitcode", "global"]


def _status_via(status: Status) -> Via:
    """A status atom's Via is its lowercased status; Via adds exitcode and global (DL-72)."""
    return cast(Via, status.lower())


class DerivedEdge(BaseModel):
    src: str  # producer job name, global name (via=="global"), or "name^INST"
    dst: str  # consumer: the job whose condition/box override references src
    via: Via
    #: the IR-F condition node this edge came from; carries an exit-code or global
    #: comparison's op/value (DL-73)
    atom: StatusAtom | ExitCodeAtom | GlobalAtom
    lookback: Lookback | None = None
    cls: EdgeClass
    mapping_row: str  # "M01".."M36"
    assumption: str | None = None  # human-readable; required iff cls=="assumed"
    source_atom: SourceSpan | None = None  # provenance: owning attr's span

    @field_validator("atom", "lookback", mode="after")
    @classmethod
    def _detach_from_ir_f(cls, node: BaseModel | None) -> BaseModel | None:
        """The edge copies the node; IR-G never aliases IR-F's condition AST."""
        return node if node is None else node.model_copy(deep=True)

    @model_validator(mode="after")
    def _via_matches_atom(self) -> DerivedEdge:
        """global iff GlobalAtom, exitcode iff ExitCodeAtom (DL-73)."""
        special = ("global", "exitcode")
        if (self.via in special or self.atom.kind in special) and self.via != self.atom.kind:
            raise ValueError(f"via {self.via!r} does not match atom kind {self.atom.kind!r}")
        return self

    @model_validator(mode="after")
    def _assumption_matches_class(self) -> DerivedEdge:
        if self.cls == "assumed" and not self.assumption:
            raise ValueError("assumed edge requires a recorded assumption (ir-design ss5)")
        if self.cls == "exact" and self.assumption is not None:
            raise ValueError("exact edge must not carry an assumption")
        return self

    @property
    def is_start_gate(self) -> bool:
        """True when the edge names a producer job that gates the consumer's start (DL-162).

        A box-override edge (M15/M16) is a completion predicate (SEM-12) and a global edge
        names no job; neither is a start gate. An undefined (M02-R) or cross-instance (M33)
        producer is a start gate whose job the caller cannot look up; use `local_producer`,
        not `src in catalog.jobs` (DL-162a). L011 is wider (any wiring) and L008 narrower
        (row M16); neither reads this."""
        return self.via != "global" and self.mapping_row not in ("M15", "M16")


def start_gates(graph: DerivedGraph) -> dict[str, list[DerivedEdge]]:
    """The start-gate edges indexed by consumer, for L021, L022 and the cadence wake walk
    (DL-185). L020 reads the condition tree instead (DL-243, DL-281)."""
    gates: dict[str, list[DerivedEdge]] = {}
    for edge in graph.edges:
        if edge.is_start_gate:
            gates.setdefault(edge.dst, []).append(edge)
    return gates


def local_producer(edge: DerivedEdge, catalog: CatalogIR) -> str | None:
    """The catalog job this edge's producer names, or None.

    Read off the atom, not `src`: a cross-instance `src` is the display form `name^INST`, which
    can collide with a local job named `foo^PRD` (DL-162a). None for a global, a cross-instance
    producer, and a job the catalog does not define."""
    return local_job(edge.atom, catalog)


def local_job(atom: Atom, catalog: CatalogIR) -> str | None:
    """`local_producer` on a bare atom (DL-162a)."""
    if isinstance(atom, GlobalAtom) or atom.job.instance is not None:
        return None
    return atom.job.name if atom.job.name in catalog.jobs else None


class OrShape(BaseModel):
    """M12 classifier output for one Or node (UCS-03 lowering patterns).

    Lowering suggestions assume UC has no native OR-join.
    PENDING: U1 -- native "Any" completion criteria would change the suggestion texts.
    """

    job: str  # the consumer whose condition/box override contains the Or
    attr: Literal["condition", "box_success", "box_failure"] = "condition"
    kind: Literal["common_ancestor", "independent", "mixed"]
    branches: list[list[str]]  # per operand: local job refs it mentions
    lowering: str  # suggested M12 lowering choice
    span: SourceSpan | None = None


class BoxTree(BaseModel):
    """Built from JobIR.box.box_name; the CatalogIR validator guarantees it is sound."""

    roots: list[str] = []  # top-level boxes, catalog order
    children: dict[str, list[str]] = {}  # box -> members, catalog order
    parent: dict[str, str] = {}  # member -> immediate box

    def top(self, name: str) -> str | None:
        """Root box of `name`'s chain; the box itself if top-level; None outside any box."""
        current = name
        seen_box = current in self.children and current not in self.parent
        while (up := self.parent.get(current)) is not None:
            current = up
            seen_box = True
        return current if (seen_box or current != name) else None


class RedesignFlag(BaseModel):
    """Per-job R-classified construct that is not an edge."""

    job: str
    mapping_row: str  # e.g. "M27"
    reason: str
    span: SourceSpan | None = None


class DerivedGraph(BaseModel):
    nodes: list[str] = []  # catalog job names, source order
    edges: list[DerivedEdge] = []
    mutex_groups: list[list[str]] = []  # n() detector pairs (M07)
    #: consumer -> the bare local n() targets in its own condition, self included; keeps the
    #: direction mutex_groups loses (L021 needs it)
    bare_notrunning: dict[str, list[str]] = {}
    or_shapes: list[OrShape] = []  # M12 classifier output
    box_tree: BoxTree = BoxTree()
    external_boundary: list[JobRef] = []  # cross-instance refs (M33)
    redesign_flags: list[RedesignFlag] = []  # M27 run_window v1
    chains: list[list[str]] = []  # maximal linear chains (feeds DSL decompiler)
    parallel_groups: list[list[str]] = []  # same-(preds,succs) sibling groups
    cycles: list[list[str]] = []  # SCCs >1 + self-loops, sorted node sets (L010)


# ---------------------------------------------------------------------- extraction


class _RawRef(NamedTuple):
    """One atom occurrence before classification."""

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


def is_mutex_atom(
    origin: Literal["condition", "box_success", "box_failure"],
    atom: StatusAtom | ExitCodeAtom | GlobalAtom,
) -> bool:
    """True when a local unqualified n() in `condition` is a mutex candidate, not an edge.

    Edges would create false ordering (M07, dossier R6). A lookback-qualified n() stays an
    edge (M03, DL-12). Public for readers that walk a condition tree beside the edges (DL-191)."""
    return (
        origin == "condition"
        and isinstance(atom, StatusAtom)
        and atom.status == "NOTRUNNING"
        and atom.job.instance is None
        and atom.lookback is None
    )


def _bare_notrunning(refs: list[_RawRef]) -> dict[str, list[str]]:
    """The one scan of the mutex predicate: the jobs each consumer names in bare local n()
    form, self included. `_mutex_groups` is its undirected projection."""
    out: dict[str, set[str]] = {}
    for ref in refs:
        if is_mutex_atom(ref.origin, ref.atom):
            assert isinstance(ref.atom, StatusAtom)
            out.setdefault(ref.dst, set()).add(ref.atom.job.name)
    return {dst: sorted(targets) for dst, targets in sorted(out.items())}


def _mutex_groups(bare_notrunning: dict[str, list[str]]) -> list[list[str]]:
    groups: set[tuple[str, ...]] = set()
    for dst, targets in bare_notrunning.items():
        for other in targets:
            groups.add((dst,) if other == dst else tuple(sorted((dst, other))))
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
    """Cadence key over trigger fields only (run_window is a gate, SEM-33; must_* are alarms,
    SEM-34)."""
    dump = schedule.model_dump(mode="json")
    return "sched:" + json.dumps({k: dump[k] for k in _TRIGGER_FIELDS}, sort_keys=True)


def _condition_pred_map(catalog: CatalogIR, refs: list[_RawRef]) -> dict[str, set[str]]:
    """dst -> local job srcs of its `condition`; mutex and global atoms carry no cadence."""
    preds: dict[str, set[str]] = {name: set() for name in catalog.jobs}
    for ref in refs:
        if (
            ref.origin != "condition"
            or is_mutex_atom(ref.origin, ref.atom)
            or isinstance(ref.atom, GlobalAtom)
        ):
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


def _same_cycle(
    src: str, dst: str, tree: BoxTree, cadence: dict[str, str | None]
) -> Literal["box", "cadence"] | None:
    """Why the two jobs are one stream (M01), or None for cross-stream (M02).

    "box" is co-membership of one top-level box and needs no cadence; "cadence" is two unboxed
    jobs under one trigger signature (DL-12)."""
    top_src, top_dst = tree.top(src), tree.top(dst)
    if top_src is not None or top_dst is not None:
        # Boxes are streams: a different or single box means different UC workflows even
        # under identical schedules (M14: member conditions on outside jobs -> M02).
        return "box" if (top_src == top_dst and top_src is not None) else None
    if cadence.get(src) is not None and cadence.get(src) == cadence.get(dst):
        return "cadence"
    return None


def _is_inside(tree: BoxTree, job: str, box: str) -> bool:
    """SEM-12 'inside the box' is transitive: any ancestor box counts."""
    current = job
    while (up := tree.parent.get(current)) is not None:
        if up == box:
            return True
        current = up
    return False


# --------------------------------------------------------- edge classification (pass 3)

#: The same-stream staleness assumption for M01 and the same-cycle halves of M04/M05: f()/d()
#: latch as s() does (SEM-01/02, DL-153).
_BOX_STREAM_STALENESS = (
    "producer and consumer are members of one top-level box, so one"
    " box run is one cycle (neither job needs a cadence of its own);"
    " assumes no cross-run staleness is relied upon (SEM-01/R1)"
)
_CADENCE_STREAM_STALENESS = (
    "producer and consumer share one trigger cadence; assumes no"
    " cross-run staleness is relied upon (SEM-01/R1)"
)


def _classify_condition_edge(
    ref: _RawRef,
    catalog: CatalogIR,
    tree: BoxTree,
    cadence: dict[str, str | None],
) -> DerivedEdge:
    atom = ref.atom
    assert not isinstance(atom, GlobalAtom)
    src = atom.job.name
    via: Via = "exitcode" if isinstance(atom, ExitCodeAtom) else _status_via(atom.status)
    lookback = atom.lookback

    def edge(cls: EdgeClass, row: str, assumption: str | None = None) -> DerivedEdge:
        return DerivedEdge(
            src=src,
            dst=ref.dst,
            via=via,
            atom=atom,
            lookback=lookback,
            cls=cls,
            mapping_row=row,
            assumption=assumption,
            source_atom=ref.span,
        )

    if src not in catalog.jobs:
        # redesign, not assumed: an A-row edge to a nonexistent vertex would be silent loss
        # (DL-12); L001 carries the error.
        return edge(
            "redesign",
            "M02",
            "producer is not defined in the catalog (SEM-06: atom is permanently"
            " false; see L001) -- latching semantics cannot be assessed",
        )
    if lookback is not None:
        assumption = (
            "lookback window compiles to a Task Monitor Time Scope (no documented"
            " max -- U5 resolved, DL-53); window anchoring differs per case (SEM-04)"
        )
        if lookback.kind == "zero":
            # the since-last-end anchor (Q2a, DL-54) is relational, so no fixed-window Time
            # Scope expresses it (Q2b, DL-58); every use is flagged.
            assumption += (
                "; zero-lookback anchors to the consumer's own last end"
                " (Q2a/Q2b, DL-54/DL-58) -- no fixed Time Scope expresses it"
            )
        return edge("assumed", "M03", assumption)
    if via == "success":
        stream = _same_cycle(src, ref.dst, tree, cadence)
        if stream == "box":
            return edge("assumed", "M01", _BOX_STREAM_STALENESS)
        if stream == "cadence":
            return edge("assumed", "M01", _CADENCE_STREAM_STALENESS)
        return edge(
            "assumed",
            "M02",
            "cross-stream latching dependency: compiles to a Task Monitor;"
            " Time Scope bounds differ from an indefinite latch (UCS-06)",
        )
    if via in ("failure", "done"):
        # f()/d() latch like s() (SEM-01/02), so every edge is A and the detector picks the
        # flavor: a stale FAILURE satisfies f() in AutoSys and no UC edge does (UCS-13,
        # DL-153)
        row = "M04" if via == "failure" else "M05"
        stream = _same_cycle(src, ref.dst, tree, cadence)
        if stream == "box":
            return edge("assumed", row, _BOX_STREAM_STALENESS)
        if stream == "cadence":
            return edge("assumed", row, _CADENCE_STREAM_STALENESS)
        watch = "Failed" if via == "failure" else "any completion"
        return edge(
            "assumed",
            row,
            "cross-stream latching dependency: compiles to a Task Monitor"
            f" watching {watch} with Time Scope; Time Scope bounds differ from"
            " an indefinite latch (UCS-06)",
        )
    if via == "terminated":
        return edge(
            "assumed",
            "M06",
            "UC separates Cancelled from Failed; t() maps to the Cancelled-"
            "inclusive reading -- document the choice per estate",
        )
    if via == "exitcode":
        # per-task Exit Code Processing, default Success Exitcode Range (U4, DL-53)
        return edge(
            "assumed",
            "M08",
            "exit-code comparison compiles to the per-task Exit Code Processing"
            " field (default Success Exitcode Range; mechanism resolved,"
            " U4/DL-53) -- the configured range is estate data, not recoverable"
            " from JIL alone",
        )
    if via == "notrunning":
        # bare local n() is mutex-classified and a lookback returns M03 above
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
                atom=atom,
                cls="redesign",
                mapping_row="M16",
                assumption=(
                    "box completion gated on a global variable: external-reference"
                    " gating has no UC analog (SEM-12 hung-RUNNING pattern)"
                ),
                source_atom=ref.span,
            )
        # before_wf_dependency_evaluation defaults true (U8, DL-53)
        return DerivedEdge(
            src=atom.name,
            dst=ref.dst,
            via="global",
            atom=atom,
            cls="assumed",
            mapping_row="M09",
            assumption=(
                "AutoSys re-evaluates on SET_GLOBAL events; UC edge variable"
                " conditions evaluate at predecessor completion by default"
                " (uc.perform_actions.before_wf_dependency_evaluation=true,"
                " U8/DL-53) -- redesign if the global is used as an async gate"
                " (UCS-08)"
            ),
            source_atom=ref.span,
        )
    if atom.job.instance is not None:
        boundary.append(atom.job)
        via = "exitcode" if isinstance(atom, ExitCodeAtom) else _status_via(atom.status)
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
            src=atom.job.key,
            dst=ref.dst,
            via=via,
            atom=atom,
            lookback=atom.lookback,
            cls="redesign",
            mapping_row=row,
            assumption=why,
            source_atom=ref.span,
        )
    if ref.origin != "condition":
        via = "exitcode" if isinstance(atom, ExitCodeAtom) else _status_via(atom.status)
        src = atom.job.name
        if _is_inside(tree, src, ref.dst):
            # a workflow is Running/Problems on member failure, never Failed (U2, DL-53,
            # UCS-04)
            return DerivedEdge(
                src=src,
                dst=ref.dst,
                via=via,
                atom=atom,
                lookback=atom.lookback,
                cls="assumed",
                mapping_row="M15",
                assumption=(
                    f"{ref.origin} references member {src!r}: early-exit completion"
                    " override needs explicit Skip-path restructuring (UCS-04: a"
                    " workflow goes Running/Problems on member failure, never Failed"
                    " -- U2 resolved, DL-53)"
                ),
                source_atom=ref.span,
            )
        return DerivedEdge(
            src=src,
            dst=ref.dst,
            via=via,
            atom=atom,
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
    """root -> {root} plus everything reachable backwards over condition edges.

    Computed iteratively for the requested roots only, to avoid quadratic memory and deep
    recursion on long chains (DL-20). Closures are complete on cyclic graphs."""
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
    # collect every Or node's branches first so ancestor sets cover only the producers that
    # need them (DL-20)
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
            # undefined names cannot anchor a restructure (L001 owns the finding)
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
    """(preds, succs) over deduped local job->job `condition` start-gate edges."""
    preds: dict[str, set[str]] = {n: set() for n in catalog.jobs}
    succs: dict[str, set[str]] = {n: set() for n in catalog.jobs}
    for e in edges:
        if not e.is_start_gate:
            continue
        src = local_producer(e, catalog)
        if src is None:
            continue
        preds[e.dst].add(src)
        succs[src].add(e.dst)
    return preds, succs


def cycles(nodes: list[str], succs: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCCs (iterative): SCCs of size >1 plus self-loops, as sorted lists, sorted.

    Legal AutoSys; L010 warns. Public for the cadence check, which runs it over its own wake
    adjacency, start gates plus bare n() (DL-184)."""
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
    """Maximal linear chains of length >= 2 in source order: b is a's only successor and a is
    b's only predecessor. Cycle members are excluded; the cycle report owns them."""

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
    """Sibling groups with identical (preds, succs) signatures, at least one side nonempty."""
    by_signature: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
    for node in nodes:
        if not preds[node] and not succs[node]:
            continue
        key = (tuple(sorted(preds[node])), tuple(sorted(succs[node])))
        by_signature.setdefault(key, []).append(node)
    return sorted(group for group in by_signature.values() if len(group) >= 2)


# ------------------------------------------------------------------ connectivity


class EdgeEnds(Protocol):
    """The connectivity view of an edge; DerivedEdge and the UC backend's UcEdge satisfy it."""

    src: str
    dst: str


def components(
    nodes: Sequence[str],
    edges: Iterable[EdgeEnds],
    *,
    bind_box_members: BoxTree | None = None,
) -> list[list[str]]:
    """Connected components over the edges with both ends in `nodes`.

    Mutex groups connect nothing (DL-35). `bind_box_members` (DL-72): pass a BoxTree to union
    each box with its members (viz), None to leave membership out (the UC backend). Members
    come back in `nodes` order, groups in first-member order.
    """
    parent: dict[str, str] = {name: name for name in nodes}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]  # path halving
            name = parent[name]
        return name

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for edge in edges:
        if edge.src in parent and edge.dst in parent:
            union(edge.src, edge.dst)
    if bind_box_members is not None:
        for box, members in bind_box_members.children.items():
            for member in members:
                union(box, member)

    grouped: dict[str, list[str]] = {}
    for name in nodes:
        grouped.setdefault(find(name), []).append(name)
    return list(grouped.values())


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
    bare_notrunning = _bare_notrunning(refs)  # pass 2
    mutex_groups = _mutex_groups(bare_notrunning)
    cond_preds = _condition_pred_map(catalog, refs)
    cadence = _cadences(catalog, cond_preds)  # pass 3 core
    boundary: list[JobRef] = []
    edges = [
        _edge_for_ref(ref, catalog, tree, cadence, boundary)  # passes 3 + 5
        for ref in refs
        if not is_mutex_atom(ref.origin, ref.atom)
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
            # copy: IR-G never aliases IR-F's AST nodes
            deduped_boundary.append(ref_.model_copy())
    sccs = cycles(nodes, succs)  # pass 7
    return DerivedGraph(
        nodes=nodes,
        edges=edges,
        mutex_groups=mutex_groups,
        bare_notrunning=bare_notrunning,
        or_shapes=or_shapes,
        box_tree=tree,
        external_boundary=deduped_boundary,
        redesign_flags=redesign_flags,
        chains=_chains(nodes, preds, succs, {n for scc in sccs for n in scc}),
        parallel_groups=_parallel_groups(nodes, preds, succs),
        cycles=sccs,
    )
