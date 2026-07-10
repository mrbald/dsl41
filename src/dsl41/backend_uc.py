"""UC backend, U3-independent slice: edge classification + migration report.

Phase 9 of the implementation order (CLAUDE.md / DL-03). Record emission is
BLOCKED on U3 (pull /resources/openapi.json from the live controller, freeze
docs/uc-edge-schema.md, generate the client -- DL-08); until then this module
implements exactly the two things the plan allows: the migration-report
emitter and the edge-classification plumbing.

Mapping-driven compiler requirements (stonebranch-semantics Part II):
1. Every Layer-G edge carries its M-row (derive supplies it).
2. The UC backend refuses to compile R rows -- they become migration-report
   items; A rows compile WITH an assumption record; only E rows compile
   silently. This is DL-04's "failed translation is a loud, classified
   error" made granular.
3. The migration report is a first-class output artifact (per-catalog
   markdown): all A assumptions, all R redesigns, all [?]-dependent
   mappings.

Decisions pinned here (each with a test; recorded as DL-15):
- compile_to_uc() raises BlockedOnU3 unconditionally: emitting records
  against a guessed schema would be silent loss with extra steps. The
  error names U3 and the unblock path.
- The report pins catalog_hash (ir-design ss8: "pin what was verified") and
  the tool version; it is deterministic for identical input -- no
  timestamps (callers stamp their own).
- Beyond R/A edges, the report carries every construct Part II routes to a
  human: M27 run_window flags (pass 6), M07 mutex groups (edge-less
  constructs), M12 OR shapes with their suggested lowering, M33 external
  boundary refs, and the open-question ledger (each U-question whose
  resolution changes a mapping the catalog actually uses).
- Report generation never fails on R rows -- the report IS the loud error
  channel; the `dsl41 report` CLI always exits 0 on a generated report
  (the linter is the gate; documented in the command help).
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel

from dsl41.derive import DerivedEdge, DerivedGraph, derive_graph
from dsl41.equiv import catalog_hash
from dsl41.ir import CatalogIR, tool_version


class BlockedOnU3(NotImplementedError):
    """UC record emission is blocked until the edge schema is frozen."""

    def __init__(self) -> None:
        super().__init__(
            "UC record emission is BLOCKED on U3: pull /resources/openapi.json"
            " from the live controller, freeze docs/uc-edge-schema.md, and"
            " generate the client (DL-08). Until then use the migration report"
            " (dsl41 report) and the linter."
        )


class CompilePlan(BaseModel):
    """Edge partition per the Part II requirement: what would compile
    silently (E), what compiles with an assumption record (A), and what the
    backend refuses (R)."""

    exact: list[DerivedEdge] = []
    assumed: list[DerivedEdge] = []
    refused: list[DerivedEdge] = []

    def counts(self) -> dict[str, int]:
        return {
            "exact": len(self.exact),
            "assumed": len(self.assumed),
            "refused": len(self.refused),
        }


def classify_edges(graph: DerivedGraph) -> CompilePlan:
    plan = CompilePlan()
    for edge in graph.edges:
        if edge.cls == "exact":
            plan.exact.append(edge)
        elif edge.cls == "assumed":
            plan.assumed.append(edge)
        else:
            plan.refused.append(edge)
    return plan


def compile_to_uc(catalog: CatalogIR) -> None:
    """PENDING: U3 -- always raises; see BlockedOnU3."""
    raise BlockedOnU3()


#: Open questions whose resolution changes a mapping, keyed by the M-rows
#: that depend on them (stonebranch-semantics Part III). The report lists a
#: question iff the catalog uses one of its rows.
_U_QUESTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("U1", ("M12",), "native OR-join / 'Any' completion criteria decide the M12 lowering"),
    ("U2", ("M15",), "exact workflow-status derivation rules (box_success restructuring)"),
    ("U4", ("M08", "M31"), "per-task-type exit-code -> status configuration"),
    ("U5", ("M02", "M03"), "Time Scope bounds on Task Monitors (max lookback window)"),
    ("U6", ("M24", "M26"), "trigger timezone + calendar parity with AutoSys calendars"),
    ("U7", ("M29", "M30"), "Maximum Run Time auto-cancel config; retry trigger set"),
    ("U8", ("M09", "M10"), "Set Variable Actions ordering property default (UCS-08)"),
)


def _edge_line(edge: DerivedEdge) -> str:
    lookback = ""
    if edge.lookback is not None:
        token = edge.lookback.raw or edge.lookback.kind
        lookback = f", lookback {token}"
    where = ""
    if edge.source_atom is not None:
        where = f" — `{edge.source_atom.file}:{edge.source_atom.line_start}`"
    line = f"- **{edge.mapping_row}** `{edge.src}` →({edge.via}{lookback})→ `{edge.dst}`{where}"
    if edge.assumption:
        line += f"\n  - {edge.assumption}"
    return line


def render_migration_report(catalog: CatalogIR, graph: DerivedGraph | None = None) -> str:
    """Per-catalog markdown migration report (Part II requirement 3).

    Deterministic for identical input; sections appear only when non-empty
    (except the summary, which always states the totals)."""
    if graph is None:
        graph = derive_graph(catalog)
    plan = classify_edges(graph)
    counts = plan.counts()
    used_rows = {edge.mapping_row for edge in graph.edges}
    used_rows.update(flag.mapping_row for flag in graph.redesign_flags)
    if graph.mutex_groups:
        used_rows.add("M07")
    if graph.or_shapes:
        used_rows.add("M12")
    # DL-25: calendars are external named dependencies -- autocal territory,
    # not definable in JIL, so "unknown calendar" is undecidable for the
    # linter. The report inventories them instead (and surfaces U6 via the
    # M24/M26 rows). Only LIVE schedules count; dead-config calendars are
    # L005's business.
    calendars: dict[str, list[str]] = {}
    for name, job in catalog.jobs.items():
        schedule = job.schedule
        if schedule is None:
            continue
        for calendar in (schedule.run_calendar, schedule.exclude_calendar):
            if calendar:
                calendars.setdefault(calendar, []).append(name)
        if schedule.timezone:
            used_rows.add("M26")
    if calendars:
        used_rows.add("M24")

    lines: list[str] = [
        "# Migration report",
        "",
        f"- catalog hash: `{catalog_hash(catalog)}`",
        f"- tool version: `{tool_version()}`",
        f"- jobs: {len(catalog.jobs)}, derived edges: {len(graph.edges)}"
        f" (exact {counts['exact']}, assumed {counts['assumed']},"
        f" refused {counts['refused']})",
    ]
    row_counts = Counter(edge.mapping_row for edge in graph.edges)
    if row_counts:
        rows = ", ".join(f"{row} ×{n}" for row, n in sorted(row_counts.items()))
        lines.append(f"- mapping rows in use: {rows}")

    if plan.refused:
        lines += [
            "",
            "## Refused constructs (R-class — redesign required)",
            "",
            "The UC backend will NOT compile these; each needs a human"
            " redesign decision (Part II requirement 1).",
            "",
        ]
        lines += [_edge_line(edge) for edge in plan.refused]
    if graph.redesign_flags:
        lines += ["", "## Redesign flags (non-edge constructs)", ""]
        for flag in graph.redesign_flags:
            where = ""
            if flag.span is not None:
                where = f" — `{flag.span.file}:{flag.span.line_start}`"
            lines.append(f"- **{flag.mapping_row}** `{flag.job}`{where}\n  - {flag.reason}")
    if plan.assumed:
        lines += [
            "",
            "## Assumptions (A-class — compile with these recorded)",
            "",
        ]
        lines += [_edge_line(edge) for edge in plan.assumed]
    if graph.mutex_groups:
        lines += [
            "",
            "## Mutual exclusion (M07 — resources, not edges)",
            "",
        ]
        for group in graph.mutex_groups:
            if len(group) == 1:
                lines.append(f"- `{group[0]}` self-exclusion → UC Instance Wait (serialize runs)")
            else:
                names = ", ".join(f"`{name}`" for name in group)
                lines.append(f"- {names} → Mutually Exclusive Tasks / Virtual Resource")
    if graph.or_shapes:
        lines += ["", "## OR shapes (M12 — per-case lowering decisions)", ""]
        for shape in graph.or_shapes:
            branches = "; ".join("{" + ", ".join(branch) + "}" for branch in shape.branches)
            lines.append(f"- `{shape.job}`.{shape.attr} ({shape.kind}) branches: {branches}")
            lines.append(f"  - {shape.lowering}")
    if graph.external_boundary:
        lines += ["", "## External boundary (M33 — cross-instance producers)", ""]
        for ref in graph.external_boundary:
            lines.append(f"- `{ref.name}^{ref.instance}`")
    if calendars:
        lines += [
            "",
            "## Calendars (M24 — external definitions, not in JIL)",
            "",
            "Calendars live in autocal, not JIL; recreate each in UC and verify"
            " parity per calendar (U6).",
            "",
        ]
        for calendar in sorted(calendars):
            jobs_list = ", ".join(f"`{j}`" for j in sorted(calendars[calendar]))
            lines.append(f"- `{calendar}` — used by {jobs_list}")
    open_questions = [
        (question, dep_rows, why)
        for question, dep_rows, why in _U_QUESTIONS
        if used_rows.intersection(dep_rows)
    ]
    if open_questions:
        lines += [
            "",
            "## Open questions this catalog depends on (verify on the live controller)",
            "",
        ]
        for question, dep_rows, why in open_questions:
            affected = ", ".join(sorted(used_rows.intersection(dep_rows)))
            lines.append(f"- **{question}** ({affected}): {why}")
    lines += [
        "",
        "---",
        "",
        "Record emission is blocked on **U3** (freeze `docs/uc-edge-schema.md`"
        " from the live controller's `/resources/openapi.json`; DL-08).",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------ UC twin model (compile target)

# The in-memory UC workflow model the backend will serialize once U3 freezes
# the record schema. Until then it feeds the minimal UC interpreter
# (uc_oracle.py) that powers the P-Mxx expected-divergence pairs
# (stonebranch Part IV). Semantics sources are all public [V] entries:
# UCS-01 edge conditions, UCS-02 skip propagation, UCS-03 joins, UCS-09
# mutual exclusion, UCS-13 within-run evaluation.

#: UCS-01/M06: UC separates Cancelled from Failed -- a `failure` edge must
#: NOT fire on Cancelled (review M-1); `cancelled` carries the t() mapping.
UcEdgeCondition = Literal["success", "failure", "done", "cancelled"]


class UcVarCondition(BaseModel):
    """UCS-01 variable condition: evaluated when the predecessor completes
    (NOT on SET_GLOBAL -- that timing gap IS the M09 divergence)."""

    name: str
    op: str  # =, !=, <, >, <=, >=
    value: str


class UcEdge(BaseModel):
    src: str
    dst: str
    condition: UcEdgeCondition
    var_condition: UcVarCondition | None = None
    mapping_row: str  # provenance: the M-row that produced this edge


class UcWorkflow(BaseModel):
    name: str
    tasks: list[str]  # task names, catalog order
    edges: list[UcEdge] = []
    #: names that ALSO launch this workflow (UCS-0 "workflows are themselves
    #: tasks"): the box name is the workflow name; nested box names alias to
    #: the flattened top workflow (review M-2)
    aliases: list[str] = []


class UcModel(BaseModel):
    """One catalog compiled to UC shapes (E/A rows only)."""

    workflows: list[UcWorkflow] = []
    mutex_groups: list[list[str]] = []  # UCS-09 Mutually Exclusive Tasks
    max_exit_success: dict[str, int] = {}  # M31 assumed: same boundary as AutoSys
    #: M31/DL-33: explicit exit-code sets ride the same same-boundary
    #: assumption (U4); verdict shared via ir.exit_is_success.
    success_codes: dict[str, list[tuple[int, int]]] = {}
    fail_codes: dict[str, list[tuple[int, int]]] = {}
    excluded: list[str] = []  # human-readable ledger of everything NOT compiled


_VIA_TO_UC: dict[str, UcEdgeCondition] = {
    "success": "success",
    "failure": "failure",
    "done": "done",
    "terminated": "cancelled",  # M06: t() maps to the Cancelled condition
}


def compile_twin(catalog: CatalogIR, graph: DerivedGraph | None = None) -> UcModel:
    """Lower the derived graph to the in-memory UC model (E/A rows only).

    Lowering choices (DL-16, each with a test):
    - R-classified edges and redesign flags are EXCLUDED and recorded in
      `excluded` -- the twin interprets what the backend would compile, and
      the backend refuses R rows (Part II requirement 1). run_window (M27)
      is likewise absent from the model: the P-M27 pair shows the
      divergence that absence causes.
    - M09 global edges become var-condition edges from the consumer's OTHER
      predecessors? No -- a global atom has no producer task; it becomes a
      var_condition attached to EVERY compiled edge into that consumer, or,
      when the consumer has no compiled predecessor edges, it is excluded
      (recorded): a UC edge cannot exist without a predecessor vertex
      (UCS-01), which is exactly why async global gates are M09/R-adjacent.
    - M12 Or: the NAIVE lowering -- each Or branch's edges attach to the
      consumer, and UC's conjunctive-over-non-skipped join (UCS-02/03)
      applies. That reproduces AutoSys `|` only for common-ancestor
      diamonds (skip drops the untaken branch); for independent branches it
      is an AND -- exactly the divergence P-M12 exists to document. The
      restructure / Task-Monitor / duplicate-successor lowerings are
      U1-gated per-case decisions (recorded in `excluded` as a note when an
      or_shape is present).
    - exitcode atoms (M08) become var-condition edges on the producer edge
      reading the twin's per-task last-exit-code pseudo-variable
      "exit:<task>" (U4 default).
    - Boxes -> workflows (M13/M18: nested boxes flatten into the top-level
      workflow's task set v1 -- ACTIVATED-style nesting is out of scope);
      standalone tasks group into workflows by weakly-connected components
      over compiled edges; isolated tasks become singleton workflows.
    - Mutex groups pass through (M07 -> UCS-09).
    - n()-via edges (lookback-qualified notrunning, M03) are excluded: no
      UC edge condition reads "not running" (recorded).
    """
    if graph is None:
        graph = derive_graph(catalog)
    excluded: list[str] = []
    compiled: list[UcEdge] = []
    global_gates: dict[str, list[UcVarCondition]] = {}  # consumer -> var conds
    for edge in graph.edges:
        if edge.cls == "redesign":
            excluded.append(f"{edge.mapping_row} edge {edge.src} -> {edge.dst} (R-class)")
            continue
        if edge.via == "global":
            op_value = _split_global_edge(edge, catalog)
            if op_value is None:
                excluded.append(
                    f"{edge.mapping_row} global gate ${edge.src} -> {edge.dst}"
                    " (no recoverable op/value)"
                )
                continue
            op, value = op_value
            global_gates.setdefault(edge.dst, []).append(
                UcVarCondition(name=edge.src, op=op, value=value)
            )
            continue
        if edge.via == "notrunning":
            excluded.append(
                f"{edge.mapping_row} edge {edge.src} -> {edge.dst}"
                " (notrunning has no UC edge condition)"
            )
            continue
        if edge.via == "exitcode":
            var_condition = _exitcode_var_condition(edge, catalog)
            compiled.append(
                UcEdge(
                    src=edge.src,
                    dst=edge.dst,
                    condition="done",
                    var_condition=var_condition,
                    mapping_row=edge.mapping_row,
                )
            )
            continue
        compiled.append(
            UcEdge(
                src=edge.src,
                dst=edge.dst,
                condition=_VIA_TO_UC[edge.via],
                mapping_row=edge.mapping_row,
            )
        )
    # attach global gates to the consumer's edges; anything that cannot be
    # carried is RECORDED -- never silently dropped (review M-3, DL-04)
    for consumer, conditions in sorted(global_gates.items()):
        edges_in = [e for e in compiled if e.dst == consumer]
        if not edges_in:
            for condition in conditions:
                excluded.append(
                    f"M09 global gate ${condition.name} -> {consumer}"
                    " (consumer has no compiled predecessor edge; async global"
                    " gates need a redesign, UCS-01)"
                )
            continue
        primary = conditions[0]
        attached = False
        ungated = 0
        for uc_edge in edges_in:
            if uc_edge.var_condition is None:
                uc_edge.var_condition = primary
                attached = True
            else:
                ungated += 1  # slot already taken (M08 exitcode var-cond)
        if not attached:
            excluded.append(
                f"M09 global gate ${primary.name} -> {consumer} (every predecessor"
                " edge already carries an M08 var_condition; one var_condition per"
                " edge v1 -- gate needs a redesign)"
            )
        elif ungated:
            excluded.append(
                f"M09 global gate ${primary.name} -> {consumer} not on every path"
                f" ({ungated} edge(s) already carry M08 var_conditions; the >=1-"
                "satisfied join can bypass the gate, UCS-02)"
            )
        for extra in conditions[1:]:
            excluded.append(
                f"M09 extra global gate ${extra.name} -> {consumer} (one"
                " var_condition per edge v1; recorded, not compiled)"
            )
    for flag in graph.redesign_flags:
        excluded.append(f"{flag.mapping_row} {flag.job}: {flag.reason}")
    if graph.or_shapes:
        excluded.append(
            "M12 OR shapes present: duplicate-successor join semantics apply"
            " (UCS-03); alternative lowerings are U1-gated"
        )
    # workflows: boxes first (nested flatten to top), then edge components
    workflows: list[UcWorkflow] = []
    in_box: set[str] = set()
    for root in graph.box_tree.roots:
        members = _transitive_members(graph, root)
        nested_boxes = [
            b for b in graph.box_tree.children if b != root and graph.box_tree.parent.get(b)
        ]
        in_box.update(members)
        in_box.add(root)
        in_box.update(nested_boxes)
        workflows.append(
            UcWorkflow(
                name=root,
                tasks=[t for t in members if t in catalog.jobs],
                edges=[e for e in compiled if e.src in members and e.dst in members],
                aliases=[b for b in nested_boxes if _top_of(graph, b) == root],
            )
        )
    component = _components(
        [t for t in graph.nodes if t not in in_box and catalog.jobs[t].job_type != "BOX"],
        [e for e in compiled if e.src not in in_box and e.dst not in in_box],
    )
    for tasks in component:
        name = f"wf_{tasks[0]}"
        workflows.append(
            UcWorkflow(
                name=name,
                tasks=tasks,
                edges=[e for e in compiled if e.src in tasks and e.dst in tasks],
            )
        )
    cross = [
        e for e in compiled if not any(e.src in wf.tasks and e.dst in wf.tasks for wf in workflows)
    ]
    for e in cross:
        excluded.append(
            f"{e.mapping_row} edge {e.src} -> {e.dst} spans workflows"
            " (Task Monitor territory, M02/M03; not modeled v1)"
        )
    # SEM-24/DL-18: definition-time state is not modeled in the twin v1; the
    # eventual mapping is M20 Hold ("Hold on Start", E-class). Recorded, never
    # silently dropped -- the AutoSys-vs-twin comparator diverging on such
    # catalogs is the correct polarity.
    for name, job in catalog.jobs.items():
        initial = job.sem.initial_status
        if initial is not None and initial != "INACTIVE":
            excluded.append(
                f"M20 {name}: definition-time status {initial} not modeled in the"
                " twin v1 (map via UC Hold on Start at cutover, SEM-24)"
            )
        if job.resources:
            groups = ", ".join(
                f"{r.name} x{r.quantity}" + (f" FREE={r.free}" if r.free else "")
                for r in job.resources
            )
            excluded.append(
                f"M34 {name}: resource requirements ({groups}) not modeled in the"
                " twin v1 (map to UC Virtual Resources, UCS-09; DL-21)"
            )
    return UcModel(
        workflows=workflows,
        mutex_groups=[list(g) for g in graph.mutex_groups],
        max_exit_success={
            name: job.sem.max_exit_success
            for name, job in catalog.jobs.items()
            if job.sem.max_exit_success
        },
        success_codes={
            name: job.sem.success_codes
            for name, job in catalog.jobs.items()
            if job.sem.success_codes is not None
        },
        fail_codes={
            name: job.sem.fail_codes
            for name, job in catalog.jobs.items()
            if job.sem.fail_codes is not None
        },
        excluded=excluded,
    )


def _top_of(graph: DerivedGraph, box: str) -> str:
    current = box
    while (up := graph.box_tree.parent.get(current)) is not None:
        current = up
    return current


def _transitive_members(graph: DerivedGraph, box: str) -> list[str]:
    out: list[str] = []
    stack = [box]
    while stack:
        current = stack.pop(0)
        for member in graph.box_tree.children.get(current, []):
            if member in graph.box_tree.children:  # nested box: flatten (M18 v1)
                stack.append(member)
            else:
                out.append(member)
    return out


def _components(tasks: list[str], edges: list[UcEdge]) -> list[list[str]]:
    parent: dict[str, str] = {t: t for t in tasks}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        if e.src in parent and e.dst in parent:
            parent[find(e.src)] = find(e.dst)
    groups: dict[str, list[str]] = {}
    for t in tasks:
        groups.setdefault(find(t), []).append(t)
    return [groups[root] for root in sorted(groups, key=tasks.index)]


def _split_global_edge(edge: DerivedEdge, catalog: CatalogIR) -> tuple[str, str] | None:
    """Recover (op, value) for a via=global edge by finding the GlobalAtom in
    the consumer's condition (derive keeps src=name only)."""
    from dsl41.conditions import GlobalAtom, iter_atoms

    consumer = catalog.jobs.get(edge.dst)
    if consumer is None or consumer.sem.condition is None:
        return None
    for atom in iter_atoms(consumer.sem.condition):
        if isinstance(atom, GlobalAtom) and atom.name == edge.src:
            return atom.op, atom.value
    return None


def _exitcode_var_condition(edge: DerivedEdge, catalog: CatalogIR) -> UcVarCondition | None:
    from dsl41.conditions import ExitCodeAtom, iter_atoms

    consumer = catalog.jobs.get(edge.dst)
    if consumer is None or consumer.sem.condition is None:
        return None
    for atom in iter_atoms(consumer.sem.condition):
        if isinstance(atom, ExitCodeAtom) and atom.job.name == edge.src:
            return UcVarCondition(name=f"exit:{edge.src}", op=atom.op, value=str(atom.value))
    return None
