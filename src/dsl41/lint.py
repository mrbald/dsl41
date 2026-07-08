"""Linter: findings, not treatments (ir-design ss9).

Phases 4+5 of the implementation order (CLAUDE.md / DL-03): the Violation
model (stable codes, exit_code(strict)), the pure IR-F rules L001-L005 and
L015 (phase 4), and the graph rules L008-L014 over the derived graph
(phase 5). L006/L007 (contradiction/tautology) need the tier-b truth-table
engine and join in phase 8 (equiv).

Rules are pure functions CatalogIR -> list[Violation] (graph rules receive
the pre-derived DerivedGraph too); lint_catalog derives the graph once and
runs the registry in code order and jobs in catalog (source) order, so
output is deterministic for identical input.

Decisions pinned here (each with a test):
- L001 cross-instance reading (SEM-06 vs SEM-07): a local job ref must exist
  in the catalog; a cross-instance ref (`job^INST`) cannot be resolved against
  the local catalog, so it fires only when INST itself is not declared via
  insert_xinst -- declared-instance refs are external boundary markers
  (M33 territory, phase 5), not dangling references.
- L002 producers: a `$$VAR` site resolves if the catalog declares the global
  (insert_global) or some job command embeds a `sendevent -E SET_GLOBAL` for
  it. Producer detection is a textual heuristic over command strings and
  over-approximates (any `-G NAME=` in a SET_GLOBAL-mentioning command
  counts) -- the conservative direction for an error-severity rule. value()
  atoms are deliberately NOT checked: SET_GLOBAL at runtime from outside the
  catalog is routine, and the rule's normative text scopes it to `$$VAR`.
- L003/L004 are enforced upstream and kept registered so the stable code
  space matches ir-design ss9 verbatim, but their reachability differs:
  L004 is a true defensive scan -- a SEM-31-violating ScheduleBlock survives
  only if model_construct is used at EVERY containing level (pydantic
  revalidates nested instances on normal construction), and the scan then
  catches it. L003 is a pure tripwire: the grammar lexically excludes
  lookback on value() and GlobalAtom has no lookback field, so even
  model_construct drops the kwarg -- the scan can only fire if the model
  ever grows the field.
- L005 reads the SEM-30 dead-config routing decision from lowering: time
  attributes with falsy/absent date_conditions sit verbatim in
  JobIR.passthrough, which is exactly where this rule looks.

Phase-5 graph-rule readings (each with a test):
- L008 fires on M16-classified box-override edges (non-member, global, or
  cross-instance refs -- derive's own SEM-12 detection); M15 (member ref)
  is the legitimate early-exit shape and stays quiet.
- L009 "unqualified s() feeding a scheduled consumer": an M01/M02 success
  edge with no lookback whose consumer has date_conditions scheduling. The
  stale-latch reading (SEM-01/R1): the consumer's time trigger can fire on a
  latch left over from a previous producer run. Producer-side scheduling is
  irrelevant; M01 same-cycle classification does not exempt -- the latch is
  still indefinite at the JIL level (the assumption is exactly what L009
  asks a human to confirm).
- L010 reports derive's SCC cycles (legal AutoSys, possible re-trigger
  pattern); one violation per cycle naming the sorted node set.
- L011 dangling job: no schedule, no derived edges in or out (global/mutex
  participation counts as wiring), not in a box, not a box with members,
  and not an FW source. Purely-manual utility jobs are the accepted false
  positive (hygiene warn).
- L012 info: one finding per mutex group (M07), suggesting the UC construct
  (Mutually Exclusive Tasks / Virtual Resource; Instance Wait for n(self)).
- L013 box member with own date_conditions schedule (SEM-31 note: double
  gate -- member still needs the box RUNNING; often unintended).
- L014 UC-side name collision (UCS-12): lowering already refuses exact
  duplicates, so the linter's residual check is case-insensitive collision
  (UC name addressing is the migration hazard); error severity per ss9.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from dsl41.ast_jil import SourceSpan
from dsl41.conditions import GlobalAtom, iter_atoms, lookback_pitfalls
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import TIME_CLUSTER, CatalogIR, ExecSpec, FwSpec

Severity = Literal["error", "warn", "info"]

_SEVERITY_RANK: dict[Severity, int] = {"error": 2, "warn": 1, "info": 0}


class Violation(BaseModel):
    code: str  # stable "Lnnn" -- never renumber (ir-design ss9)
    severity: Severity
    message: str
    jobs: list[str] = []  # affected job names; empty for catalog-level findings
    span: SourceSpan | None = None
    detail: str | None = None  # machine-usable hook (referenced name, attr, token)

    def render(self) -> str:
        loc = f"{self.span.file}:{self.span.line_start}: " if self.span else ""
        return f"{loc}{self.code} {self.severity}: {self.message}"


class LintReport(BaseModel):
    violations: list[Violation] = []

    def exit_code(self, strict: bool = False) -> int:
        """0 clean; 1 if any error, or (with strict) any warning. Info never
        affects the exit code. Parse/lowering failures are the CLI's exit 2."""
        threshold = _SEVERITY_RANK["warn"] if strict else _SEVERITY_RANK["error"]
        if any(_SEVERITY_RANK[v.severity] >= threshold for v in self.violations):
            return 1
        return 0

    def by_code(self, code: str) -> list[Violation]:
        return [v for v in self.violations if v.code == code]


# ------------------------------------------------------------------------- the rules


def rule_l001(catalog: CatalogIR) -> list[Violation]:
    """Condition references undefined job (SEM-06): the atom evaluates false,
    permanently and silently -- the dependent job never auto-starts. Local
    refs must exist in the catalog; cross-instance refs only need their
    instance declared (module docstring)."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        seen: set[tuple[str, str, str | None]] = set()
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                if isinstance(atom, GlobalAtom):
                    continue
                ref = atom.job
                key = (attr_name, ref.name, ref.instance)
                if key in seen:
                    continue
                seen.add(key)
                # SEM-06 consequence differs by attr: a false condition means
                # the job never auto-starts; a false box override never fires
                # (SEM-12 hung-RUNNING risk; L008 covers the non-member case).
                consequence = (
                    f"{job.name!r} will never auto-start on it"
                    if attr_name == "condition"
                    else f"the {attr_name} override never fires (hung-RUNNING risk, SEM-12)"
                )
                if ref.instance is None:
                    if ref.name not in catalog.jobs:
                        out.append(
                            Violation(
                                code="L001",
                                severity="error",
                                message=(
                                    f"{attr_name} of {job.name!r} references undefined job"
                                    f" {ref.name!r} (SEM-06: permanently false -- {consequence})"
                                ),
                                jobs=[job.name],
                                span=span,
                                detail=ref.name,
                            )
                        )
                elif ref.instance not in catalog.external_instances:
                    out.append(
                        Violation(
                            code="L001",
                            severity="error",
                            message=(
                                f"{attr_name} of {job.name!r} references job {ref.name!r} on"
                                f" undeclared external instance {ref.instance!r}"
                                f" (SEM-07: no insert_xinst for it in the catalog)"
                            ),
                            jobs=[job.name],
                            span=span,
                            detail=f"{ref.name}^{ref.instance}",
                        )
                    )
    return out


#: Producer heuristic (module docstring): a command mentioning SET_GLOBAL
#: produces every `-G NAME=`-shaped assignment it contains.
_SET_GLOBAL_ASSIGN_RE = re.compile(r"-G\s+\"?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _set_global_producers(catalog: CatalogIR) -> set[str]:
    producers: set[str] = set()
    for job in catalog.jobs.values():
        if isinstance(job.exec_, ExecSpec) and "SET_GLOBAL" in job.exec_.command:
            producers.update(m.group(1) for m in _SET_GLOBAL_ASSIGN_RE.finditer(job.exec_.command))
    return producers


def rule_l002(catalog: CatalogIR) -> list[Violation]:
    """Unresolved `$$VAR` (SEM-08): no insert_global and no SET_GLOBAL
    producer anywhere in the catalog. Substitution would yield an empty/
    stale value at runtime."""
    producers = _set_global_producers(catalog)
    out: list[Violation] = []
    for job in catalog.jobs.values():
        attrs_by_name: dict[str, list[str]] = {}  # insertion order = site order
        for site in job.var_sites:
            if site.name not in catalog.globals_declared and site.name not in producers:
                attrs = attrs_by_name.setdefault(site.name, [])
                if site.attr not in attrs:
                    attrs.append(site.attr)
        for name, attrs in attrs_by_name.items():
            out.append(
                Violation(
                    code="L002",
                    severity="error",
                    message=(
                        f"{job.name!r} substitutes $${name} (in {', '.join(attrs)}) but the"
                        f" catalog neither declares it (insert_global) nor produces it"
                        f" (SET_GLOBAL)"
                    ),
                    jobs=[job.name],
                    span=job.span,
                    detail=name,
                )
            )
    return out


def rule_l003(catalog: CatalogIR) -> list[Violation]:
    """Lookback on a value() atom (SEM-04): enforced upstream -- the grammar
    excludes it lexically and GlobalAtom carries no lookback field (even
    model_construct drops the kwarg), so this is a pure tripwire that fires
    only if the model ever grows the field."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                if isinstance(atom, GlobalAtom) and getattr(atom, "lookback", None) is not None:
                    out.append(
                        Violation(
                            code="L003",
                            severity="error",
                            message=(
                                f"{attr_name} of {job.name!r} applies a lookback to"
                                f" value({atom.name}) (SEM-04: lookback never applies to"
                                f" global-variable atoms)"
                            ),
                            jobs=[job.name],
                            span=span,
                            detail=atom.name,
                        )
                    )
    return out


def rule_l004(catalog: CatalogIR) -> list[Violation]:
    """SEM-31 mutual exclusivity: enforced at lowering and by the
    ScheduleBlock validator on construction/load; this defensive scan catches
    hand-built IR that bypassed validation (model_construct at every
    containing level -- nested instances are revalidated otherwise)."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        schedule = job.schedule
        if schedule is None:
            continue
        pairs = (
            ("start_times", schedule.start_times, "start_mins", schedule.start_mins),
            ("days_of_week", schedule.days_of_week, "run_calendar", schedule.run_calendar),
        )
        for a_name, a_val, b_name, b_val in pairs:
            if a_val is not None and b_val is not None:
                out.append(
                    Violation(
                        code="L004",
                        severity="error",
                        message=(
                            f"{job.name!r} sets both {a_name} and {b_name}"
                            f" (SEM-31: mutually exclusive; AutoSys rejects the JIL)"
                        ),
                        jobs=[job.name],
                        span=job.span,
                        detail=f"{a_name}+{b_name}",
                    )
                )
    return out


def rule_l005(catalog: CatalogIR) -> list[Violation]:
    """Time attributes present while date_conditions is falsy/absent (SEM-30):
    AutoSys ignores them -- dead configuration. Lowering routes exactly this
    shape into passthrough, which is where we look."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        dead = sorted(k for k in job.passthrough if k.lower() in TIME_CLUSTER)
        if dead:
            out.append(
                Violation(
                    code="L005",
                    severity="warn",
                    message=(
                        f"{job.name!r} carries time attributes ({', '.join(dead)}) but"
                        f" date_conditions is falsy/absent (SEM-30: they are ignored --"
                        f" dead configuration)"
                    ),
                    jobs=[job.name],
                    span=job.span,
                    detail=",".join(dead),
                )
            )
    return out


def rule_l015(catalog: CatalogIR) -> list[Violation]:
    """Lookback raw-format pitfalls (SEM-04): valid-but-suspicious shapes,
    e.g. bare `30` meaning 30 HOURS or single-digit minutes. The shape facts
    come from conditions.lookback_pitfalls at parse time."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                lookback = getattr(atom, "lookback", None)
                if lookback is None:
                    continue
                for pitfall in lookback_pitfalls(lookback):
                    out.append(
                        Violation(
                            code="L015",
                            severity="warn",
                            message=f"{attr_name} of {job.name!r}: {pitfall}",
                            jobs=[job.name],
                            span=span,
                            detail=lookback.raw,
                        )
                    )
    return out


# ------------------------------------------------------ tier-b rules (phase 8, equiv)


def rule_l006(catalog: CatalogIR) -> list[Violation]:
    """Contradiction (e.g. s(x)&f(x) same lookback scope): the condition is
    unsatisfiable over the ICE-FREE tier-b state space -- it can only fire
    if an operator ices a referenced job (SEM-05 makes every atom true
    then). The ice-free framing is deliberate (DL-14 amendment): icing is
    intervention, not scheduling. Too-large conditions are skipped silently
    (tier-c territory)."""
    from dsl41.equiv import cond_truth_profile

    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            profile = cond_truth_profile(cond)  # include_ice=False by default
            if profile is None:
                continue
            satisfiable, _ = profile
            if not satisfiable:
                out.append(
                    Violation(
                        code="L006",
                        severity="warn",
                        message=(
                            f"{attr_name} of {job.name!r} is a contradiction -- no status-"
                            f"store state short of ON_ICE on a referenced job satisfies it"
                            f" (tier-b state enumeration)"
                        ),
                        jobs=[job.name],
                        span=span,
                        detail=attr_name,
                    )
                )
    return out


def rule_l007(catalog: CatalogIR) -> list[Violation]:
    """Tautology at box start (ss9): a box member whose condition is true in
    EVERY state reachable at the moment the box first evaluates it gates
    nothing. The box-start model follows the oracle's catalog-order member
    starts (DL-14 amendment): siblings declared EARLIER may already be
    NEVER_RAN or RUNNING when this member is evaluated; siblings declared
    LATER are certainly NEVER_RAN. Unpinned tautology is vacuous by
    construction (every condition is falsifiable in the free model), so
    this rule only examines box members."""
    from dsl41.equiv import cond_truth_profile

    out: list[Violation] = []
    names_in_order = list(catalog.jobs)
    for job in catalog.jobs.values():
        box = job.box.box_name
        cond = job.sem.condition
        if box is None or cond is None:
            continue
        my_index = names_in_order.index(job.name)
        fixed: dict[str, set[str] | str] = {}
        for name, other in catalog.jobs.items():
            if other.box.box_name != box or name == job.name:
                continue
            if names_in_order.index(name) < my_index:
                fixed[name] = {"NEVER_RAN", "RUNNING"}  # may have started first
            else:
                fixed[name] = "NEVER_RAN"
        profile = cond_truth_profile(cond, fixed_status=fixed)
        if profile is None:
            continue
        _, falsifiable = profile
        if not falsifiable:
            out.append(
                Violation(
                    code="L007",
                    severity="warn",
                    message=(
                        f"condition of box member {job.name!r} is always true at box start"
                        f" (every sibling state reachable at first evaluation satisfies"
                        f" it) -- it gates nothing (tier-b)"
                    ),
                    jobs=[job.name],
                    span=job.sem.condition_span,
                    detail=box,
                )
            )
    return out


# -------------------------------------------------------- graph rules (phase 5, IR-G)


def rule_l008(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """box_success/box_failure references a non-member (SEM-12 gating): if all
    members complete before the external condition is true, the box hangs
    RUNNING. Fires on exactly derive's M16 box-override edges."""
    out: list[Violation] = []
    for edge in graph.edges:
        if edge.mapping_row != "M16":
            continue
        if edge.via == "global":
            gated_on = f"global variable {edge.src!r}"
        elif "^" in edge.src:
            gated_on = f"{edge.src!r} on an external instance"
        else:
            gated_on = f"{edge.src!r}, which is not one of its members"
        out.append(
            Violation(
                code="L008",
                severity="warn",
                message=(
                    f"box {edge.dst!r} completion is gated on {gated_on}"
                    f" (SEM-12: if members finish first the box hangs RUNNING;"
                    f" M16 -- no UC analog)"
                ),
                jobs=[edge.dst],
                span=edge.source_atom,
                detail=edge.src,
            )
        )
    return out


def rule_l009(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Unqualified s() feeding a scheduled consumer (SEM-01/R1 stale latch):
    the consumer's time trigger can fire on a success recorded by a previous
    producer run -- or block on a FAILURE left from one. Lookback-qualified
    atoms are exempt (the qualifier is the fix)."""
    out: list[Violation] = []
    for edge in graph.edges:
        if edge.via != "success" or edge.lookback is not None:
            continue
        if edge.mapping_row not in ("M01", "M02"):
            continue  # box-override edges have their own rule (L008)
        if edge.src not in catalog.jobs:
            continue  # undefined producer: L001's error; a staleness warn
            # about a job that never ran would contradict it
        consumer = catalog.jobs.get(edge.dst)
        if consumer is None or consumer.schedule is None:
            continue
        out.append(
            Violation(
                code="L009",
                severity="warn",
                message=(
                    f"scheduled job {edge.dst!r} depends on unqualified s({edge.src})"
                    f" (SEM-01: the latch is indefinite -- a success from a previous"
                    f" cycle satisfies it; qualify with a lookback or confirm staleness"
                    f" is intended)"
                ),
                jobs=[edge.dst],
                span=edge.source_atom,
                detail=edge.src,
            )
        )
    return out


def rule_l010(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Derived-graph cycle (ss5 pass 7): legal AutoSys -- statuses latch, so a
    cycle is not a deadlock -- but a classic re-trigger / tight-loop pattern."""
    return [
        Violation(
            code="L010",
            severity="warn",
            message=(
                f"dependency cycle over derived condition edges: {' -> '.join(cycle)}"
                f" (legal in AutoSys; verify it is not an unintended re-trigger loop)"
            ),
            jobs=list(cycle),
            span=catalog.jobs[cycle[0]].span if cycle[0] in catalog.jobs else None,
            detail=",".join(cycle),
        )
        for cycle in graph.cycles
    ]


def rule_l011(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Dangling job (hygiene): no schedule, no derived wiring (edges in or
    out, incl. global/mutex participation), no box membership either way,
    and not an FW source. Only reachable by manual sendevent."""
    wired: set[str] = set()
    for edge in graph.edges:
        wired.add(edge.dst)
        if edge.via != "global":
            wired.add(edge.src)  # global srcs are pseudo-nodes, not jobs
    for group in graph.mutex_groups:
        wired.update(group)
    out: list[Violation] = []
    for job in catalog.jobs.values():
        if (
            job.schedule is not None
            or job.name in wired
            or job.box.box_name is not None
            or graph.box_tree.children.get(job.name)
            or isinstance(job.exec_, FwSpec)
        ):
            continue
        if job.job_type == "BOX":
            message = (
                f"box {job.name!r} has no members, no schedule, and no dependencies in"
                f" or out (an empty box never completes; dangling container)"
            )
        else:
            message = (
                f"{job.name!r} has no schedule, no dependencies in or out, and no box"
                f" -- it only runs via manual sendevent (dangling job, or an"
                f" intentional utility job)"
            )
        out.append(
            Violation(
                code="L011",
                severity="warn",
                message=message,
                jobs=[job.name],
                span=job.span,
            )
        )
    return out


def rule_l012(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """n() mutex candidates (M07, dossier R6): these are NOT dependencies --
    translating them as edges creates false ordering. Suggest the UC
    construct per group shape."""
    out: list[Violation] = []
    for group in graph.mutex_groups:
        if len(group) == 1:
            message = (
                f"{group[0]!r} declares n({group[0]}) self-exclusion: model as UC"
                f" Instance Wait (serialize successive runs; M07/UCS-09)"
            )
        else:
            message = (
                f"jobs {group[0]!r} and {group[1]!r} are mutually exclusive via n():"
                f" model as UC Mutually Exclusive Tasks or a Virtual Resource, not an"
                f" edge (M07/UCS-09; an edge would fabricate ordering)"
            )
        out.append(
            Violation(
                code="L012",
                severity="info",
                message=message,
                jobs=list(group),
                span=catalog.jobs[group[0]].span if group[0] in catalog.jobs else None,
                detail=",".join(group),
            )
        )
    return out


def rule_l013(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Box member with its own date_conditions schedule (SEM-31 note): the
    member still needs its box RUNNING -- schedule and box gate compose with
    AND. A scheduled member of a non-running box silently does not fire."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        if job.box.box_name is None or job.schedule is None:
            continue
        out.append(
            Violation(
                code="L013",
                severity="warn",
                message=(
                    f"{job.name!r} is a member of box {job.box.box_name!r} AND carries its"
                    f" own schedule (SEM-31: both gates must hold -- a scheduled member"
                    f" of a non-running box does not fire; often unintended)"
                ),
                jobs=[job.name],
                span=job.span,
                detail=job.box.box_name,
            )
        )
    return out


def rule_l014(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """UC-side name collision (UCS-12): lowering already refuses exact
    duplicates, so the residual hazard is names that collide once UC
    addresses them -- case-insensitive equality (JIL names are case-sensitive
    on UNIX targets, ir-design ss6). Fuller UC name constraints (charset,
    length) are unpinned until the U3 OpenAPI pull freezes the record
    schema; extend this rule then."""
    by_folded: dict[str, list[str]] = {}
    for name in catalog.jobs:
        by_folded.setdefault(name.lower(), []).append(name)
    out: list[Violation] = []
    for folded in sorted(by_folded):
        names = by_folded[folded]
        if len(names) < 2:
            continue
        out.append(
            Violation(
                code="L014",
                severity="error",
                message=(
                    f"job names {', '.join(repr(n) for n in names)} collide"
                    f" case-insensitively (UCS-12: UC name addressing treats them as"
                    f" one task; rename before migration)"
                ),
                jobs=names,
                span=catalog.jobs[names[0]].span,
                detail=folded,
            )
        )
    return out


# -------------------------------------------------------------------------- registry

RuleFn = Callable[[CatalogIR], list[Violation]]
GraphRuleFn = Callable[[CatalogIR, DerivedGraph], list[Violation]]

#: Code order == run order == report order. Codes are stable (ir-design ss9).
RULES: tuple[tuple[str, RuleFn], ...] = (
    ("L001", rule_l001),
    ("L002", rule_l002),
    ("L003", rule_l003),
    ("L004", rule_l004),
    ("L005", rule_l005),
    ("L006", rule_l006),
    ("L007", rule_l007),
    ("L015", rule_l015),
)

GRAPH_RULES: tuple[tuple[str, GraphRuleFn], ...] = (
    ("L008", rule_l008),
    ("L009", rule_l009),
    ("L010", rule_l010),
    ("L011", rule_l011),
    ("L012", rule_l012),
    ("L013", rule_l013),
    ("L014", rule_l014),
)


def lint_catalog(catalog: CatalogIR, graph: DerivedGraph | None = None) -> LintReport:
    """Run every registered rule; deterministic for identical input. The
    derived graph is computed once (or passed in by a caller that already
    has it); report order is IR-F rules first, then graph rules, each block
    in code order."""
    if graph is None:
        graph = derive_graph(catalog)
    violations: list[Violation] = []
    for _code, rule in RULES:
        violations.extend(rule(catalog))
    for _code, graph_rule in GRAPH_RULES:
        violations.extend(graph_rule(catalog, graph))
    return LintReport(violations=violations)
