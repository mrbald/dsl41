"""UC backend: edge classification, migration report and base record emission (DL-03).

U3 is split on the U6/Q2 pattern (DL-55): U3a, the create-only whole-record shape for
workflows whose edges carry the base tokens Success / Failure / Success/Failure, is frozen in
docs/uc-edge-schema.md, and compile_to_uc() emits exactly that subset. U3b stays open
(# PENDING: U3b below): rich condition forms, the live OpenAPI pull and write-path
verification. The API client is generated from OpenAPI, never hand-written; this module
emits records, not calls (DL-08).

Contracts (stonebranch-semantics Part II; DL-15, DL-55):
1. Every Layer-G edge carries its M-row, supplied by derive.
2. The backend refuses R rows (they become migration-report items), compiles A rows with an
   assumption record, and compiles E rows silently (DL-04).
3. The migration report is a first-class artifact: all A assumptions, all R redesigns, all
   [?]-dependent mappings. It never fails on R rows; it is the loud error channel, and
   `dsl41 report` exits 0 on a generated report.
- compile_to_uc() serializes the twin model (DL-16) into a self-describing UcBundle: wire
  records, quarantine ledger, apply-time notes and the catalog hash.
- A workflow is quarantined whole, never partly emitted (safe-freeze, DL-55), for an edge the
  base schema cannot express (the twin's `cancelled`, M06 t(); any var_condition, U3b) or a
  record name another workflow also serializes to (UC folds case, UCS-12/L014). Every cause
  is listed (DL-04).
- Records pin retainSysIds=false, omit sysId/version/exportTable/exportReleaseLevel, use
  explicit string vertexIds and deterministic layout (uc-edge-schema.md).
- The report pins catalog_hash (ir-design ss8) and the tool version, and carries no timestamps.
- Beyond R/A edges the report carries M27 run_window flags, M07 mutex groups, M12 OR shapes
  with suggested lowering, M33 boundary refs, and the open-question ledger (each U-question
  whose resolution changes a mapping the catalog uses). U3b gates emission, not a mapping row,
  so it surfaces through the quarantine section and the footer (DL-55).
- Per-job non-edge constructs (M17, M19/M20/M21, M34) have one channel: `_per_job_exclusions`
  feeds `UcModel.excluded_entries`, shipped in the bundle; the report renders the same ledger
  (DL-164). `ExclusionKind` tags each entry so a construct shows in one report section.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from dsl41.conditions import ExitCodeAtom, GlobalAtom
from dsl41.derive import DerivedEdge, DerivedGraph, components, derive_graph
from dsl41.equiv import catalog_hash
from dsl41.ir import CatalogIR, InitialStatus, JobIR, render_code_ranges, tool_version


class CompilePlan(BaseModel):
    """Edge partition (Part II): compiles silently (E), with an assumption record (A), or is
    refused (R)."""

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


#: Open questions keyed by the M-rows that depend on them (stonebranch-semantics Part III). The
#: report lists a question iff the catalog uses one of its rows. U2, U4, U5, U6a, U7 and U8 are
#: resolved (DL-53); their content lives in the per-edge assumption strings (derive.py).
_U_QUESTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("U1", ("M12",), "native OR-join / 'Any' completion criteria decide the M12 lowering"),
    (
        "U6b",
        ("M24",),
        "calendar parity with AutoSys extended calendars (per-trigger timezone U6a resolved, DL-53)",
    ),
)


def _edge_line(edge: DerivedEdge) -> str:
    lookback = ""
    if edge.lookback is not None:
        lookback = f", lookback {edge.lookback.token}"
    where = ""
    if edge.source_atom is not None:
        where = f" — `{edge.source_atom.at}`"
    line = f"- **{edge.mapping_row}** `{edge.src}` →({edge.via}{lookback})→ `{edge.dst}`{where}"
    if edge.assumption:
        line += f"\n  - {edge.assumption}"
    return line


def _bundle_ledger_lines(other_exclusions: list[str], notes: list[str]) -> list[str]:
    """The bundle ledger content no other report section shows: the twin's other exclusions
    and the apply notes (DL-55).

    `other_exclusions` omits the kinds with their own heading (`refused_edge`, `redesign_flag`,
    `per_job`), so nothing prints twice (DL-164)."""
    lines: list[str] = []
    if other_exclusions:
        lines += [
            "",
            "## Twin exclusions (recorded, never compiled)",
            "",
            "The twin lowering's own ledger, verbatim -- every construct it"
            " recorded instead of compiling (no silent loss, DL-04) that is"
            " not already shown above under its own heading.",
            "",
            *[f"- {item}" for item in other_exclusions],
        ]
    if notes:
        lines += [
            "",
            "## Apply notes (what a workflow record cannot carry)",
            "",
            "These travel in the `dsl41 uc` bundle beside the records, so the"
            " records cannot be applied without them (DL-55).",
            "",
            *[f"- {note}" for note in notes],
        ]
    return lines


def _has_terminators(job: JobIR) -> bool:
    """M17 (SEM-14): the box- or job-terminator flag is set on this job."""
    return job.box.box_terminator or job.box.job_terminator


def render_migration_report(catalog: CatalogIR, graph: DerivedGraph | None = None) -> str:
    """Per-catalog markdown migration report (Part II requirement 3).

    Deterministic; sections appear only when non-empty, except the summary."""
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
    # Calendars are external autocal objects, so the linter cannot decide "unknown calendar";
    # the report inventories them (DL-25) and surfaces U6b via M24. Only live schedules count;
    # dead-config calendars are L005's.
    calendars: dict[str, list[str]] = {}
    for name, job in catalog.jobs.items():
        schedule = job.schedule
        if schedule is None:
            continue
        for calendar in (schedule.run_calendar, schedule.exclude_calendar):
            if calendar:
                calendars.setdefault(calendar, []).append(name)
    if calendars:
        used_rows.add("M24")

    twin = compile_twin(catalog, graph)
    bundle = compile_to_uc(catalog, graph, twin)
    lines: list[str] = [
        "# Migration report",
        "",
        f"- catalog hash: `{bundle.catalog_hash}`",
        f"- tool version: `{tool_version()}`",
        f"- jobs: {len(catalog.jobs)}, derived edges: {len(graph.edges)}"
        f" (exact {counts['exact']}, assumed {counts['assumed']},"
        f" refused {counts['refused']})",
        f"- UC base serialization (U3a): {len(bundle.records)} of"
        f" {len(bundle.records) + len(bundle.quarantined)} workflows emit;"
        f" {len(bundle.quarantined)} quarantined",
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
                where = f" — `{flag.span.at}`"
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
    per_job_exclusions = [entry.text for entry in twin.excluded_entries if entry.kind == "per_job"]
    if any(_has_terminators(job) for job in catalog.jobs.values()):
        used_rows.add("M17")
    if per_job_exclusions:
        lines += [
            "",
            "## Per-job non-edge constructs (recorded in the bundle ledger)",
            "",
            "Definition-time status (M19/M20/M21, SEM-24), resource"
            " requirements (M34, DL-21), and terminators (M17, SEM-14) are"
            " per-job, not edges. A workflow record has no field for any of"
            " them, so they travel in the `dsl41 uc` bundle's exclusion"
            " ledger instead, verbatim (DL-164). The M17 row is A/R per"
            " case with no discriminator, so each line is a human decision.",
            "",
        ]
        lines += [f"- {entry}" for entry in per_job_exclusions]
    if graph.external_boundary:
        lines += ["", "## External boundary (M33 — cross-instance producers)", ""]
        for ref in graph.external_boundary:
            lines.append(f"- `{ref.key}`")
    if calendars:
        lines += [
            "",
            "## Calendars (M24 — autocal definitions)",
            "",
            "Calendars are autocal objects; recreate each in UC and verify parity"
            " per calendar (U6b). Definitions travel as autocal_asc exports (DL-36);"
            " a referenced calendar without one in the compilation set is flagged"
            " here (and by L018 once the set carries any calendar).",
            "",
        ]
        for calendar in sorted(calendars):
            jobs_list = ", ".join(f"`{j}`" for j in sorted(calendars[calendar]))
            defined = catalog.calendars.get(calendar)
            status = f"{defined.kind}, defined in set" if defined else "NO DEFINITION in set"
            lines.append(f"- `{calendar}` ({status}) — used by {jobs_list}")
    if bundle.quarantined:
        lines += [
            "",
            "## Quarantined workflows (withheld from the U3a bundle)",
            "",
            "Two causes: the base schema cannot express one of the workflow's"
            " edges, or two workflows serialize to one UC record name. Withheld"
            " WHOLE from the `dsl41 uc` bundle -- no partial workflow ever ships"
            " (DL-55). Each cause:",
            "",
        ]
        for workflow in bundle.quarantined:
            lines.append(f"- `{workflow.name}`")
            lines += [f"  - {reason}" for reason in workflow.reasons]
    other_exclusions = [entry.text for entry in twin.excluded_entries if entry.kind == "other"]
    lines += _bundle_ledger_lines(other_exclusions, bundle.notes)
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
        "Base record emission is available via `dsl41 uc` (**U3a**, the"
        " doc-frozen CREATE-ONLY subset in `docs/uc-edge-schema.md`). Rich"
        " condition forms, the OpenAPI pull, and write-path verification"
        " remain blocked on **U3b** (live controller; the client stays"
        " generated-not-hand-written, DL-08).",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------ UC twin model (compile target)

# The in-memory UC workflow model. compile_to_uc serializes it (DL-55) and tests/uc_oracle.py
# interprets it for the P-Mxx expected-divergence pairs (stonebranch Part IV). Sources: UCS-01
# edge conditions, UCS-02 skip propagation, UCS-03 joins, UCS-09 mutual exclusion, UCS-13
# within-run evaluation.

#: UCS-01/M06: UC separates Cancelled from Failed -- a `failure` edge must
#: NOT fire on Cancelled; `cancelled` carries the t() mapping.
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
    #: M03: the lookback token this edge dropped. UC has no window on an edge, so the bundle
    #: carries it as an apply note (DL-04). Not a record field; U3a is frozen.
    lookback: str | None = None


class UcWorkflow(BaseModel):
    name: str
    tasks: list[str]  # task names, catalog order
    edges: list[UcEdge] = []
    #: names that also launch this workflow (UCS-0: workflows are tasks); nested box names
    #: alias to the flattened top workflow
    aliases: list[str] = []


#: Report section of one `ExclusionEntry`, tagged in `compile_twin` (DL-164), so a construct
#: prints in exactly one section: `refused_edge` and `redesign_flag` have their own sections,
#: `per_job` the per-job section, `other` the Twin exclusions section.
ExclusionKind = Literal["refused_edge", "redesign_flag", "per_job", "other"]


class ExclusionEntry(BaseModel):
    """One ledger line: its text and report kind (DL-164).

    `extra="forbid"` is the DL-82 typo guard."""

    model_config = ConfigDict(extra="forbid")

    text: str
    kind: ExclusionKind


class UcModel(BaseModel):
    """One catalog compiled to UC shapes (E/A rows only).

    `extra="forbid"` is the DL-82 typo guard."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[UcWorkflow] = []
    mutex_groups: list[list[str]] = []  # UCS-09 Mutually Exclusive Tasks
    max_exit_success: dict[str, int] = {}  # M31 assumed: same boundary as AutoSys
    #: explicit exit-code sets ride the same-boundary assumption (M31, DL-33, U4); the verdict
    #: is ir.exit_is_success
    success_codes: dict[str, list[tuple[int, int]]] = {}
    fail_codes: dict[str, list[tuple[int, int]]] = {}
    #: Ledger of everything not compiled (DL-164). `UcBundle.excluded` copies each `text`; the
    #: bundle never carries `kind` (U3a is frozen).
    excluded_entries: list[ExclusionEntry] = []


#: Which UC control a SEM-24 definition-time status maps to at cutover.
#: "none" is the implicit default, which reaches no control at all.
UcInitialControl = Literal["none", "skip", "hold"]


class InitialStatusRow(NamedTuple):
    """One row of `INITIAL_STATUS_CONTROL`."""

    mapping_row: str  # "" for the default, which no mapping row governs
    control: UcInitialControl
    cutover: str  # the report's wording of the manual step


#: SEM-24 definition-time status -> the UC control it maps to at cutover. ON_HOLD is M20 Hold,
#: ON_ICE is M19 Skip, ON_NOEXEC is M21 path-level Skip.
#:
#: Total over `ir.InitialStatus` and the one place the fact is written (DL-152): the twin's
#: exclusions read it and `lint.rule_l020` keys its trigger on the skip set derived from
#: `control` (DL-281). A member with no row fails
#: `test_every_initial_status_names_a_uc_control`.
INITIAL_STATUS_CONTROL: dict[InitialStatus, InitialStatusRow] = {
    "INACTIVE": InitialStatusRow("", "none", ""),
    "ON_HOLD": InitialStatusRow("M20", "hold", "map via UC Hold on Start at cutover"),
    "ON_ICE": InitialStatusRow(
        "M19", "skip", "map via UC Skip at cutover; L020 flags the all-skipped cascade"
    ),
    "ON_NOEXEC": InitialStatusRow(
        "M21", "skip", "map via UC Skip (path-level) at cutover; the M19 caveat applies"
    ),
}

#: the statuses UC turns into a Skip -- what L020's cascade rule keys on
SKIP_TRANSLATED: frozenset[str] = frozenset(
    status for status, row in INITIAL_STATUS_CONTROL.items() if row.control == "skip"
)


_VIA_TO_UC: dict[str, UcEdgeCondition] = {
    "success": "success",
    "failure": "failure",
    "done": "done",
    "terminated": "cancelled",  # M06: t() maps to the Cancelled condition
}


def _edge_lookback_token(edge: DerivedEdge) -> str | None:
    """The lookback token an edge carries, for the twin's provenance field.

    Every compiled edge sets it, exitcode edges included, so unfreezing U3b loses no window."""
    return None if edge.lookback is None else edge.lookback.token


def compile_twin(catalog: CatalogIR, graph: DerivedGraph | None = None) -> UcModel:
    """Lower the derived graph to the in-memory UC model (E/A rows only).

    Lowering choices (DL-16):
    - R-classified edges and redesign flags are excluded and recorded in `excluded_entries`
      (Part II requirement 1). run_window (M27) is absent; the P-M27 pair shows the divergence.
    - An M09 global atom has no producer task: it becomes a var_condition on every compiled
      edge into the consumer, or is recorded as excluded when there is none (UCS-01).
    - M12 Or lowers naively: each branch's edges attach to the consumer and UC's conjunctive
      join over non-skipped edges applies (UCS-02/03). That matches `|` only for
      common-ancestor diamonds; independent branches read as an AND (P-M12). The U1-gated
      restructure lowerings are not emitted and a note is recorded.
    - An exitcode atom (M08) is a var-condition edge reading "exit:<task>" (U4).
    - Boxes become workflows (M13/M18); nested boxes flatten into the top-level task set.
      Standalone tasks group by weakly-connected components over compiled edges; isolated
      tasks become singleton workflows.
    - Mutex groups pass through (M07 -> UCS-09).
    - n()-via edges (lookback-qualified notrunning, M03) are excluded and recorded: no UC edge
      condition reads "not running".
    """
    if graph is None:
        graph = derive_graph(catalog)
    excluded_entries: list[ExclusionEntry] = []

    def _exclude(text: str, kind: ExclusionKind) -> None:
        """Append one ledger entry (DL-164)."""
        excluded_entries.append(ExclusionEntry(text=text, kind=kind))

    compiled: list[UcEdge] = []
    global_gates: dict[str, list[UcVarCondition]] = {}  # consumer -> var conds
    for edge in graph.edges:
        if edge.cls == "redesign":
            _exclude(f"{edge.mapping_row} edge {edge.src} -> {edge.dst} (R-class)", "refused_edge")
            continue
        if edge.via == "global":
            # via=="global" iff the atom is a GlobalAtom (DerivedEdge._via_matches_atom), so
            # op/value come off the edge (DL-73)
            assert isinstance(edge.atom, GlobalAtom)  # narrowing only
            global_gates.setdefault(edge.dst, []).append(
                UcVarCondition(name=edge.src, op=edge.atom.op, value=edge.atom.value)
            )
            continue
        if edge.via == "notrunning":
            _exclude(
                f"{edge.mapping_row} edge {edge.src} -> {edge.dst}"
                " (notrunning has no UC edge condition)",
                "other",
            )
            continue
        if edge.via == "exitcode":
            # via=="exitcode" iff ExitCodeAtom; this edge's own op/value (DL-73)
            assert isinstance(edge.atom, ExitCodeAtom)  # narrowing only
            compiled.append(
                UcEdge(
                    src=edge.src,
                    dst=edge.dst,
                    condition="done",
                    var_condition=UcVarCondition(
                        name=f"exit:{edge.src}",
                        op=edge.atom.op,
                        value=str(edge.atom.value),
                    ),
                    mapping_row=edge.mapping_row,
                    lookback=_edge_lookback_token(edge),
                )
            )
            continue
        compiled.append(
            UcEdge(
                src=edge.src,
                dst=edge.dst,
                condition=_VIA_TO_UC[edge.via],
                mapping_row=edge.mapping_row,
                lookback=_edge_lookback_token(edge),
            )
        )
    # attach global gates to the consumer's edges; what cannot be carried is recorded (DL-04)
    for consumer, conditions in sorted(global_gates.items()):
        edges_in = [e for e in compiled if e.dst == consumer]
        if not edges_in:
            for condition in conditions:
                _exclude(
                    f"M09 global gate ${condition.name} -> {consumer}"
                    " (consumer has no compiled predecessor edge; async global"
                    " gates need a redesign, UCS-01)",
                    "other",
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
            _exclude(
                f"M09 global gate ${primary.name} -> {consumer} (every predecessor"
                " edge already carries an M08 var_condition; one var_condition per"
                " edge v1 -- gate needs a redesign)",
                "other",
            )
        elif ungated:
            _exclude(
                f"M09 global gate ${primary.name} -> {consumer} not on every path"
                f" ({ungated} edge(s) already carry M08 var_conditions; the >=1-"
                "satisfied join can bypass the gate, UCS-02)",
                "other",
            )
        for extra in conditions[1:]:
            _exclude(
                f"M09 extra global gate ${extra.name} -> {consumer} (one"
                " var_condition per edge v1; recorded, not compiled)",
                "other",
            )
    for flag in graph.redesign_flags:
        _exclude(f"{flag.mapping_row} {flag.job}: {flag.reason}", "redesign_flag")
    if graph.or_shapes:
        _exclude(
            "M12 OR shapes present: the NAIVE lowering ships -- every branch"
            " attaches to ONE successor, and UC joins conjunctively over"
            " non-skipped incoming edges (UCS-02/03), so independent branches"
            " read as an AND. It reproduces `|` only for common-ancestor"
            " diamonds. The restructure / Task-Monitor / duplicate-successor"
            " lowerings are U1-gated and NOT emitted",
            "other",
        )
    # workflows: boxes first (nested flatten to top), then edge components. The `e.src`/`e.dst`
    # membership tests below are sound: `compiled` carries no redesign edge (M33/M16), so a
    # caret-spelled `e.src` cannot occur (DL-175, DL-162, DL-162a).
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
    # box co-membership is not bound in (DL-72): boxes are workflows already, so only loose
    # tasks group by edges, in components' own order
    component = components(
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
    workflow_task_sets = [set(wf.tasks) for wf in workflows]
    cross = [
        e
        for e in compiled
        if not any(e.src in tasks and e.dst in tasks for tasks in workflow_task_sets)
    ]
    for e in cross:
        if e.mapping_row == "M15":
            # the box is the workflow, not a task vertex, so a member -> box override edge has
            # no wire form; this is M15 restructuring, not the Task Monitor case below
            _exclude(
                f"M15 edge {e.src} -> {e.dst}: box completion override on a"
                " transitive member; the box is the workflow itself, not a task"
                " vertex, so the early exit needs explicit Skip-path"
                " restructuring at cutover (SEM-12/UCS-04)",
                "other",
            )
            continue
        _exclude(
            f"{e.mapping_row} edge {e.src} -> {e.dst} spans workflows"
            " (Task Monitor territory, M02/M03; not modeled v1)",
            "other",
        )
    for text in _per_job_exclusions(catalog):
        _exclude(text, "per_job")
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
        excluded_entries=excluded_entries,
    )


def _per_job_exclusions(catalog: CatalogIR) -> list[str]:
    """Per-job constructs the twin does not model, in catalog order, recorded not dropped.

    Covers SEM-24/DL-18 definition-time status (one row and cutover control per status,
    INITIAL_STATUS_CONTROL), M34 resources (DL-21) and M17 terminators (SEM-14). This ledger is
    the only channel to the bundle (DL-164)."""
    out: list[str] = []
    for name, job in catalog.jobs.items():
        initial = job.sem.initial_status
        row = None if initial is None else INITIAL_STATUS_CONTROL[initial]
        if row is not None and row.control != "none":
            out.append(
                f"{row.mapping_row} {name}: definition-time status {initial} not modeled in the"
                f" twin v1 ({row.cutover}; SEM-24)"
            )
        if job.resources:
            groups = ", ".join(
                f"{r.name} x{r.quantity}" + (f" FREE={r.free}" if r.free else "")
                for r in job.resources
            )
            out.append(
                f"M34 {name}: resource requirements ({groups}) not modeled in the"
                " twin v1 (map to UC Virtual Resources, UCS-09; DL-21)"
            )
        if _has_terminators(job):
            flags = ", ".join(
                flag
                for flag, on in (
                    ("box_terminator", job.box.box_terminator),
                    ("job_terminator", job.box.job_terminator),
                )
                if on
            )
            out.append(
                f"M17 {name}: {flags} not modeled in the twin v1 (no UC"
                " kill-siblings-on-failure link; emulate with task-level failure"
                " handling plus workflow Cancel actions; A/R per case, SEM-14)"
            )
    return out


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


# ------------------------------------------- U3a base record emission (DL-55)

#: UcEdgeCondition -> wire token, base subset only (uc-edge-schema.md). `cancelled` is absent:
#: UC documents no Cancelled edge condition, so M06 t() edges quarantine their workflow.
_BASE_WIRE_TOKENS: dict[UcEdgeCondition, str] = {
    "success": "Success",
    "failure": "Failure",
    "done": "Success/Failure",
}

# PENDING: U3b -- rich condition forms (Exit Code / Step Condition / Variable +
# variableCondition, vertex conditionExpression), the live /resources/openapi.json pull and
# one live POST + GET readback. Until then an edge outside _BASE_WIRE_TOKENS quarantines its
# workflow.


class QuarantinedWorkflow(BaseModel):
    """One workflow withheld whole from the bundle, every cause listed (DL-04, DL-55)."""

    name: str
    reasons: list[str]


class UcBundle(BaseModel):
    """Self-describing artifact: the records and their exclusion ledgers in one file (DL-55).

    `excluded` is the twin's ledger verbatim; `notes` are serialization-time facts (M07 mutex
    groups, M31 exit-code boundaries, synthesized names, the apply worklist)."""

    catalog_hash: str
    tool_version: str
    records: list[dict[str, object]] = []
    quarantined: list[QuarantinedWorkflow] = []
    excluded: list[str] = []
    notes: list[str] = []


def _vertex_layout(workflow: UcWorkflow) -> dict[str, tuple[int, int]]:
    """Deterministic layered layout: y is longest-path depth over the DFS-forward subgraph, x
    is arrival order within a layer.

    A back edge on a cycle contributes nothing (cycles are legal, L010; the upward edge is
    cosmetic, DL-55). The DFS is iterative, so depth is not bounded by the recursion limit."""
    preds: dict[str, list[str]] = {t: [] for t in workflow.tasks}
    for edge in workflow.edges:
        preds[edge.dst].append(edge.src)
    depth: dict[str, int] = {}

    for root in workflow.tasks:
        if root in depth:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]  # (task, next-pred index)
        on_stack = {root}
        while stack:
            task, i = stack[-1]
            pushed = False
            while i < len(preds[task]):
                pred = preds[task][i]
                i += 1
                # on-stack pred = back edge on a cycle: no contribution
                if pred not in on_stack and pred not in depth:
                    stack[-1] = (task, i)
                    stack.append((pred, 0))
                    on_stack.add(pred)
                    pushed = True
                    break
            if pushed:
                continue
            stack.pop()
            on_stack.discard(task)
            depth[task] = max((depth[p] + 1 for p in preds[task] if p in depth), default=0)
    columns: dict[int, int] = {}
    layout: dict[str, tuple[int, int]] = {}
    for task in workflow.tasks:
        column = columns.get(depth[task], 0)
        columns[depth[task]] = column + 1
        layout[task] = (90 + 180 * column, 90 + 180 * depth[task])
    return layout


def _workflow_record(workflow: UcWorkflow) -> dict[str, object]:
    """One create-only taskWorkflow record in the uc-edge-schema.md shape."""
    vertex_ids = {task: str(i + 1) for i, task in enumerate(workflow.tasks)}
    layout = _vertex_layout(workflow)
    return {
        "type": "taskWorkflow",
        "name": workflow.name,
        "retainSysIds": False,
        "workflowVertices": [
            {
                "task": {"value": task},
                "vertexId": vertex_ids[task],
                "vertexX": str(layout[task][0]),
                "vertexY": str(layout[task][1]),
            }
            for task in workflow.tasks
        ],
        "workflowEdges": [
            {
                "condition": {"value": _BASE_WIRE_TOKENS[edge.condition]},
                "sourceId": {"value": vertex_ids[edge.src]},
                "targetId": {"value": vertex_ids[edge.dst]},
                "straightEdge": True,
            }
            for edge in workflow.edges
        ],
    }


def _quarantine_reasons(workflow: UcWorkflow) -> list[str]:
    reasons: list[str] = []
    for edge in workflow.edges:
        if edge.condition not in _BASE_WIRE_TOKENS:
            reasons.append(
                f"{edge.mapping_row} edge {edge.src} -> {edge.dst}: condition"
                f" '{edge.condition}' has no base wire token (M06: UC documents"
                " no Cancelled edge condition)"
            )
        if edge.var_condition is not None:
            reasons.append(
                f"{edge.mapping_row} edge {edge.src} -> {edge.dst}: variable"
                f" condition on ${edge.var_condition.name} is a U3b rich form"
                " (variableCondition)"
            )
    return reasons


def _exit_boundary(twin: UcModel, task: str) -> str:
    """The M31 values a workflow record cannot carry, for one task (DL-33; a present fail_codes
    decides alone, Q7, DL-58)."""
    bits: list[str] = []
    if task in twin.max_exit_success:
        bits.append(f"max_exit_success={twin.max_exit_success[task]}")
    if task in twin.success_codes:
        bits.append(f"success_codes={render_code_ranges(twin.success_codes[task], sep=',')}")
    if task in twin.fail_codes:
        bits.append(f"fail_codes={render_code_ranges(twin.fail_codes[task], sep=',')}")
    return f"{task} ({', '.join(bits)})"


def _lookback_notes(workflows: list[UcWorkflow]) -> list[str]:
    """One note per emitted edge that carries a lookback window (M03).

    The frozen schema has no window field (U3b), so the edge serializes plain; the note keeps
    the window from being lost silently (DL-04)."""
    notes: list[str] = []
    for workflow in workflows:
        for edge in workflow.edges:
            if edge.lookback is None:
                continue
            notes.append(
                f"M03 edge {edge.src} -> {edge.dst} in workflow {workflow.name}"
                f" serializes as a plain '{_BASE_WIRE_TOKENS[edge.condition]}' edge:"
                f" its lookback window ({edge.lookback}) is NOT expressible in a"
                " workflow record -- rebuild it as a Task Monitor with Time Scope"
                " at cutover (SEM-04/M03)"
            )
    return notes


def compile_to_uc(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    twin: UcModel | None = None,
) -> UcBundle:
    """Serialize the twin model to the U3a base record bundle (DL-55).

    Deterministic, no timestamps. A workflow is quarantined whole, with a reason per cause,
    for an edge the base schema cannot express or a colliding record name; the rest become
    create-only taskWorkflow records (docs/uc-edge-schema.md).

    `twin` is a `UcModel` the caller already holds (the report path); it is trusted, not
    checked against `catalog`."""
    if twin is None:
        if graph is None:
            graph = derive_graph(catalog)
        twin = compile_twin(catalog, graph)
    emitted: list[UcWorkflow] = []
    quarantined: list[QuarantinedWorkflow] = []
    for workflow in twin.workflows:
        reasons = _quarantine_reasons(workflow)
        if reasons:
            quarantined.append(QuarantinedWorkflow(name=workflow.name, reasons=reasons))
        else:
            emitted.append(workflow)
    # duplicate record names clobber under an upsert wrapper, so every collision party
    # quarantines (DL-55); the comparison folds case because UC addresses names
    # case-insensitively (UCS-12, L014)
    name_counts = Counter(workflow.name.lower() for workflow in emitted)
    collided = {folded for folded, n in name_counts.items() if n > 1}
    if collided:
        survivors: list[UcWorkflow] = []
        for workflow in emitted:
            folded = workflow.name.lower()
            if folded in collided:
                parties = ", ".join(sorted({w.name for w in emitted if w.name.lower() == folded}))
                quarantined.append(
                    QuarantinedWorkflow(
                        name=workflow.name,
                        reasons=[
                            f"record name collision: {name_counts[folded]}"
                            f" workflows serialize to one folded name '{folded}'"
                            f" ({parties}) -- UC name addressing folds case"
                            " (UCS-12/L014); one-POST-per-record fails the second"
                            " create and an upsert wrapper would silently clobber"
                        ],
                    )
                )
            else:
                survivors.append(workflow)
        emitted = survivors
    records = [_workflow_record(workflow) for workflow in emitted]
    notes: list[str] = []
    for workflow in emitted:
        if workflow.aliases:
            aliases = ", ".join(workflow.aliases)
            notes.append(
                f"workflow {workflow.name}: nested boxes flattened into the top-level"
                f" record (M18 v1, DL-16); the alias names ({aliases}) have no UC"
                " record of their own"
            )
    notes += _lookback_notes(emitted)
    referenced = sorted({task for workflow in emitted for task in workflow.tasks})
    if referenced:
        notes.append(
            f"referenced tasks must exist on the controller before applying"
            f" ({len(referenced)}): {', '.join(referenced)}"
        )
    if records:
        notes.append(
            "task names pass through verbatim: UC documents no character-set"
            " or length constraint for a task name (uc-edge-schema.md); an"
            " invalid name fails loudly at create time"
        )
    synthesized = [w.name for w in emitted if w.name not in catalog.jobs]
    if synthesized:
        notes.append(
            "synthesized workflow record name(s), not estate names (component"
            f" workflows are named wf_<first task>): {', '.join(synthesized)}"
        )
    # constraints a workflow record cannot carry are named so a mutex-only catalog does not
    # give a clean-looking bundle (DL-04)
    if twin.mutex_groups:
        rendered = "; ".join(
            "+".join(group) if len(group) > 1 else f"{group[0]} (self)"
            for group in twin.mutex_groups
        )
        notes.append(
            "M07 mutual exclusion is NOT expressible in workflow records --"
            " map to UC Mutually Exclusive Tasks / Virtual Resources at"
            f" cutover: {rendered}"
        )
    exit_tasks = sorted(set(twin.max_exit_success) | set(twin.success_codes) | set(twin.fail_codes))
    if exit_tasks:
        rendered = "; ".join(_exit_boundary(twin, task) for task in exit_tasks)
        notes.append(
            f"M31 exit-code success boundaries are NOT in workflow records --"
            f" configure per-task Exit Code Processing at cutover (task bodies"
            f" are estate work): {rendered}"
        )
    return UcBundle(
        catalog_hash=catalog_hash(catalog),
        tool_version=tool_version(),
        records=records,
        quarantined=quarantined,
        excluded=[e.text for e in twin.excluded_entries],
        notes=notes,
    )
