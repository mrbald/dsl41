"""UC backend: edge classification + migration report + base record emission.

Phase 9 of the implementation order (CLAUDE.md / DL-03). U3 is SPLIT
(DL-55, the U6/Q2 pattern): U3a -- the CREATE-ONLY whole-record shape for
workflows whose edges carry the three base condition tokens (Success /
Failure / Success/Failure) -- is doc-frozen in docs/uc-edge-schema.md, and
compile_to_uc() emits exactly that subset. U3b stays open (# PENDING: U3b
below): rich condition forms (Exit Code / Step Condition / Variable +
variableCondition, vertex conditionExpression), the live OpenAPI pull, and
write-path verification. DL-08 still stands: the API *client* is generated
from OpenAPI, never hand-written -- this module emits records, not calls.

Mapping-driven compiler requirements (stonebranch-semantics Part II):
1. Every Layer-G edge carries its M-row (derive supplies it).
2. The UC backend refuses to compile R rows -- they become migration-report
   items; A rows compile WITH an assumption record; only E rows compile
   silently. This is DL-04's "failed translation is a loud, classified
   error" made granular.
3. The migration report is a first-class output artifact (per-catalog
   markdown): all A assumptions, all R redesigns, all [?]-dependent
   mappings.

Decisions pinned here (each with a test; recorded as DL-15 + DL-55):
- compile_to_uc() serializes the TWIN model (DL-16: UcModel is "the
  structure the backend serializes post-U3") into a self-describing
  UcBundle: wire records + quarantine ledger + apply-time notes + the
  catalog hash, so a pipeline cannot apply the records while missing what
  they exclude.
- QUARANTINE, never partial-emit (the safe-freeze rule, DL-55): a workflow
  is withheld WHOLE for either of two causes -- an edge the base schema
  cannot express (the twin's `cancelled`, M06 t(); or any var_condition,
  U3b rich forms), or a record name a second workflow also serializes to
  (UC folds case when it addresses a name, UCS-12/L014). Every cause is
  listed. No silent drop (DL-04).
- CREATE-ONLY hygiene (uc-edge-schema.md): records pin retainSysIds=false
  and omit sysId/version/exportTable/exportReleaseLevel; vertexIds are
  explicit strings; layout coordinates are deterministic.
- The report pins catalog_hash (ir-design ss8: "pin what was verified") and
  the tool version; it is deterministic for identical input -- no
  timestamps (callers stamp their own).
- Beyond R/A edges, the report carries every construct Part II routes to a
  human: M27 run_window flags (pass 6), M07 mutex groups (edge-less
  constructs), M12 OR shapes with their suggested lowering, M33 external
  boundary refs, and the open-question ledger (each U-question whose
  resolution changes a mapping the catalog actually uses). U3b is NOT in
  that ledger: it gates emission, not a mapping row -- it surfaces through
  the quarantine section and the footer instead (DL-55).
- Report generation never fails on R rows -- the report IS the loud error
  channel; the `dsl41 report` CLI always exits 0 on a generated report
  (the linter is the gate; documented in the command help).
- Per-job non-edge constructs (M17 terminators, M19/M20/M21 definition-time
  status, M34 resources) have ONE channel: `_per_job_exclusions` feeds
  `UcModel.excluded_entries`, whose text ships in the `dsl41 uc` bundle. The
  report renders the same ledger; it does not keep a report-only copy
  (DL-164, paying the second DL-152 deferred slice). Each entry carries a
  construction-time kind tag (`ExclusionKind`, part of `ExclusionEntry`,
  never on `UcBundle`) so the report can show a construct in exactly one
  section without parsing ledger text.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from dsl41.conditions import ExitCodeAtom, GlobalAtom
from dsl41.derive import DerivedEdge, DerivedGraph, components, derive_graph
from dsl41.equiv import catalog_hash
from dsl41.ir import CatalogIR, InitialStatus, JobIR, tool_version


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


#: Open questions whose resolution changes a mapping, keyed by the M-rows
#: that depend on them (stonebranch-semantics Part III). The report lists a
#: question iff the catalog uses one of its rows. DL-53 (2026-07-28) closed
#: U2/U4/U5/U7/U8 (and split U6 -- per-trigger timezone U6a resolved, calendar
#: parity U6b stays open): resolved questions leave this ledger and their
#: content lives in the per-edge assumption strings instead (derive.py).
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
        where = f" — `{edge.source_atom.file}:{edge.source_atom.line_start}`"
    line = f"- **{edge.mapping_row}** `{edge.src}` →({edge.via}{lookback})→ `{edge.dst}`{where}"
    if edge.assumption:
        line += f"\n  - {edge.assumption}"
    return line


def _bundle_ledger_lines(other_exclusions: list[str], notes: list[str]) -> list[str]:
    """The bundle's remaining ledger content, rendered into the report:
    what the twin lowering recorded that no other section already shows,
    and what a workflow record cannot carry. Both travel IN the bundle
    (DL-55).

    `other_exclusions` excludes the three kinds the report renders under
    their own heading -- R-class edges (`refused_edge`), redesign flags
    (`redesign_flag`), and per-job non-edge constructs (`per_job`) -- so
    nothing here prints twice (DL-164)."""
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
    # linter. The report inventories them instead (and surfaces U6b via the
    # M24 row; per-trigger timezone U6a is resolved, DL-53). Only LIVE
    # schedules count; dead-config calendars are L005's business.
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
            lines.append(f"- `{ref.name}^{ref.instance}`")
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

# The in-memory UC workflow model. compile_to_uc serializes it to the U3a
# base record bundle (DL-55), and the minimal UC interpreter (uc_oracle.py)
# interprets it for the P-Mxx expected-divergence pairs (stonebranch
# Part IV). Semantics sources are all public [V] entries:
# UCS-01 edge conditions, UCS-02 skip propagation, UCS-03 joins, UCS-09
# mutual exclusion, UCS-13 within-run evaluation.

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
    #: M03 provenance: the AutoSys lookback token this edge dropped. UC has
    #: no window on an edge, so the base record spells the plain condition
    #: and the bundle carries the window as an apply note (never silently
    #: lost, DL-04). Not a record field -- U3a is frozen.
    lookback: str | None = None


class UcWorkflow(BaseModel):
    name: str
    tasks: list[str]  # task names, catalog order
    edges: list[UcEdge] = []
    #: names that ALSO launch this workflow (UCS-0 "workflows are themselves
    #: tasks"): the box name is the workflow name; nested box names alias to
    #: the flattened top workflow
    aliases: list[str] = []


#: Report-only classification of one `ExclusionEntry`, tagged at
#: construction time in `compile_twin` (DL-164). The report reads the tag
#: instead of parsing ledger text, so a construct prints in exactly one
#: section: `refused_edge` and `redesign_flag` already have their own rich
#: section (Refused constructs, Redesign flags); `per_job` gets the new
#: per-job section; `other` is everything Twin exclusions still shows.
ExclusionKind = Literal["refused_edge", "redesign_flag", "per_job", "other"]


class ExclusionEntry(BaseModel):
    """One ledger line: the human-readable text and its report-rendering
    kind, together -- text and kind used to be two same-length lists with a
    validator holding them in step (DL-164); one entry per line removes the
    invariant instead of guarding it.

    `extra="forbid"` is the DL-82 typo guard (oracle_state.py:142): a
    keyword typo now raises at construction instead of being ignored."""

    model_config = ConfigDict(extra="forbid")

    text: str
    kind: ExclusionKind


class UcModel(BaseModel):
    """One catalog compiled to UC shapes (E/A rows only).

    `extra="forbid"` is the DL-82 typo guard (oracle_state.py:142): a
    keyword typo now raises at construction instead of being ignored -- the
    residual guarantee `_kinds_cover_the_ledger` used to provide alongside
    the invariant it held (DL-164), now that the invariant itself is gone."""

    model_config = ConfigDict(extra="forbid")

    workflows: list[UcWorkflow] = []
    mutex_groups: list[list[str]] = []  # UCS-09 Mutually Exclusive Tasks
    max_exit_success: dict[str, int] = {}  # M31 assumed: same boundary as AutoSys
    #: M31/DL-33: explicit exit-code sets ride the same same-boundary
    #: assumption (U4); verdict shared via ir.exit_is_success.
    success_codes: dict[str, list[tuple[int, int]]] = {}
    fail_codes: dict[str, list[tuple[int, int]]] = {}
    #: Ledger of everything NOT compiled, one entry per construct (DL-164).
    #: `UcBundle.excluded` copies each entry's `text` alone -- the bundle
    #: file never carries `kind` (U3a stays frozen).
    excluded_entries: list[ExclusionEntry] = []


#: Which UC control a SEM-24 definition-time status maps to at cutover.
#: "none" is the implicit default, which reaches no control at all.
UcInitialControl = Literal["none", "skip", "hold"]


class InitialStatusRow(NamedTuple):
    """One row of `INITIAL_STATUS_CONTROL`."""

    mapping_row: str  # "" for the default, which no mapping row governs
    control: UcInitialControl
    cutover: str  # how the migration report words the manual step


#: SEM-24 definition-time status -> the UC control it maps to at cutover.
#: Each status has its OWN row: ON_HOLD is M20 Hold, ON_ICE is M19 Skip,
#: ON_NOEXEC is M21 path-level Skip.
#:
#: TOTAL over `ir.InitialStatus`, and the ONE place the fact is written
#: (DL-152): the twin's exclusions read it, `lint.rule_l020` derives its
#: skip set from `control` rather than hand-listing two statuses, and
#: `uc_oracle`'s module prose points here instead of restating it. A new
#: `InitialStatus` member with no row here fails
#: `test_every_initial_status_names_a_uc_control`, where it used to be a
#: KeyError in the middle of a compile.
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
    Every compiled edge sets it, exitcode edges included: those quarantine
    their workflow today, but the day U3b unfreezes the rich forms an unset
    field would be a silent window loss.

    Named for the EDGE: `dsl._lookback_token` is a different rule over a
    different type -- it renders a JIL-valid token and never answers None."""
    return None if edge.lookback is None else edge.lookback.token


def compile_twin(catalog: CatalogIR, graph: DerivedGraph | None = None) -> UcModel:
    """Lower the derived graph to the in-memory UC model (E/A rows only).

    Lowering choices (DL-16, each with a test):
    - R-classified edges and redesign flags are EXCLUDED and recorded in
      `excluded_entries` -- the twin interprets what the backend would compile, and
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
      U1-gated per-case decisions (recorded in `excluded_entries` as a note when an
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
    excluded_entries: list[ExclusionEntry] = []

    def _exclude(text: str, kind: ExclusionKind) -> None:
        """Append one ledger entry: text and kind together (DL-164)."""
        excluded_entries.append(ExclusionEntry(text=text, kind=kind))

    compiled: list[UcEdge] = []
    global_gates: dict[str, list[UcVarCondition]] = {}  # consumer -> var conds
    for edge in graph.edges:
        if edge.cls == "redesign":
            _exclude(f"{edge.mapping_row} edge {edge.src} -> {edge.dst} (R-class)", "refused_edge")
            continue
        if edge.via == "global":
            # via=="global" iff the edge's atom IS the GlobalAtom (derive's
            # first pinned decision, enforced by DerivedEdge._via_matches_atom),
            # so op/value come off the edge (DL-73)
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
            # via=="exitcode" iff ExitCodeAtom (_via_matches_atom); this edge's
            # OWN op/value, not the first atom naming the producer (DL-73)
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
    # attach global gates to the consumer's edges; anything that cannot be
    # carried is RECORDED -- never silently dropped (DL-04)
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
    # workflows: boxes first (nested flatten to top), then edge components.
    # Every `e.src`/`e.dst` membership test below (here and at the loose-task
    # `components()` call) is sound: `compiled` never carries a redesign
    # (M33/M16 cross-instance) edge -- excluded above, before `compiled` is
    # built -- so a caret-spelled `e.src` in `compiled` can only be a
    # genuine local job (DL-175, the S-EDGE class; DL-162a). Verified, not
    # fixed here: this site predates DL-162 and was never unsound.
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
    # box co-membership is deliberately NOT bound in (derive.components,
    # DL-72): boxes already became workflows above, so only the loose tasks
    # group by edges. components' own order -- groups by first member, in
    # `nodes` order -- is exactly the wanted workflow order, so no re-sort.
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
            # the box IS the workflow, never a task vertex, so a member ->
            # box override edge has no wire form. It is M15 restructuring,
            # not the cross-workflow Task Monitor case below.
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
    """Per-JOB constructs the twin does not model, in catalog order.

    SEM-24/DL-18 definition-time state: recorded, never silently dropped --
    the AutoSys-vs-twin comparator diverging on such catalogs is the correct
    polarity. Each status keeps its OWN row and its own cutover control
    (INITIAL_STATUS_CONTROL). M34 resources ride here too (DL-21), and so
    does M17 (box_terminator/job_terminator, SEM-14) since DL-164: it used
    to be a report-only section (`_terminator_lines`, deleted) that never
    reached the `dsl41 uc` bundle, so a bundle consumer had no way to learn
    a catalog carried terminators. One channel now: this ledger."""
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

#: UcEdgeCondition -> wire token, base subset only (uc-edge-schema.md).
#: `cancelled` is deliberately absent: UC documents no Cancelled edge
#: condition, so M06 t() edges quarantine their workflow.
_BASE_WIRE_TOKENS: dict[UcEdgeCondition, str] = {
    "success": "Success",
    "failure": "Failure",
    "done": "Success/Failure",
}

# PENDING: U3b -- rich condition forms (Exit Code / Step Condition /
# Variable + variableCondition, vertex conditionExpression), the live
# /resources/openapi.json pull, and one live POST + GET readback. Until
# then every edge outside _BASE_WIRE_TOKENS quarantines its workflow.


class QuarantinedWorkflow(BaseModel):
    """One workflow withheld WHOLE from the bundle, with every cause listed
    -- an edge the base schema cannot express, or a record name another
    workflow also serializes to (no silent drop, DL-04; safe-freeze rule,
    DL-55)."""

    name: str
    reasons: list[str]


class UcBundle(BaseModel):
    """Self-describing serialization artifact: the records AND their own
    exclusion ledgers, so applying the records without reading what they
    exclude is impossible by construction (everything travels in one file).
    `excluded` is the twin lowering's ledger verbatim (R-class edges, M27
    windows, resources, ...); `notes` are serialization-time facts (M07
    mutex groups, M31 exit-code boundaries, synthesized names, the apply
    worklist) -- DL-55 review amendment."""

    catalog_hash: str
    tool_version: str
    records: list[dict[str, object]] = []
    quarantined: list[QuarantinedWorkflow] = []
    excluded: list[str] = []
    notes: list[str] = []


def _vertex_layout(workflow: UcWorkflow) -> dict[str, tuple[int, int]]:
    """Deterministic layered layout: y grows with longest-path depth over
    the DFS-forward subgraph -- an edge whose source is on the DFS stack is
    a back edge and contributes nothing (cycles are legal AutoSys, L010;
    any layering of a cycle necessarily leaves one edge pointing up, which
    break is cosmetic, review-accepted in DL-55) -- x by arrival order
    within a layer. Iterative DFS: a chain's depth is bounded by catalog
    size, not the interpreter's recursion limit."""
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
    """One CREATE-ONLY taskWorkflow record, exactly the uc-edge-schema.md
    shape: explicit string vertexIds, value-wrapper references,
    retainSysIds=false, no sysId/version/export attributes."""
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


def _range_text(ranges: list[tuple[int, int]]) -> str:
    return ",".join(str(low) if low == high else f"{low}-{high}" for low, high in ranges)


def _exit_boundary(twin: UcModel, task: str) -> str:
    """The M31 values a workflow record cannot carry, for one task (DL-33;
    Q7 cited-resolved DL-58: a present fail_codes decides alone)."""
    bits: list[str] = []
    if task in twin.max_exit_success:
        bits.append(f"max_exit_success={twin.max_exit_success[task]}")
    if task in twin.success_codes:
        bits.append(f"success_codes={_range_text(twin.success_codes[task])}")
    if task in twin.fail_codes:
        bits.append(f"fail_codes={_range_text(twin.fail_codes[task])}")
    return f"{task} ({', '.join(bits)})"


def _lookback_notes(workflows: list[UcWorkflow]) -> list[str]:
    """One note per emitted edge that carries a lookback window (M03).

    The base record has no field for a window -- the frozen schema spells
    three condition tokens and nothing else (U3b gates the rich forms) -- so
    the edge serializes as a plain wire edge. Without this note a pipeline
    applying the records alone would silently lose the window (DL-04)."""
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

    Deterministic for identical input -- no timestamps (callers stamp their
    own). A workflow is quarantined WHOLE for either cause -- an edge the
    base schema cannot express, or a record name that collides with another
    workflow's -- with a reason per cause; everything else becomes one
    CREATE-ONLY taskWorkflow record per docs/uc-edge-schema.md.

    `twin` lets a caller that already holds a `UcModel` (the report path)
    pass it down instead of triggering a second `compile_twin` over the same
    catalog -- one twin per report run. It is trusted, not re-checked against
    `catalog`: a mismatched pair is the caller's bug (this file's usual
    caller-trust style), not something this function verifies."""
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
    # duplicate record names silently clobber under any upsert wrapper --
    # every collision party quarantines (DL-55 review amendment). The
    # comparison folds case: UC addresses names case-insensitively, so WF_x
    # and wf_x are ONE record on the controller (UCS-12, the L014 reading).
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
    # constraints the twin models but a workflow record cannot carry: named
    # here so a mutex-only catalog cannot produce a clean-looking bundle
    # (review MAJOR 2 -- no silent loss, DL-04)
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
