"""Linter: findings, not treatments (ir-design ss9).

Phases 4+5 of the implementation order (CLAUDE.md / DL-03): the Violation
model (stable codes, exit_code(strict)), the pure IR-F rules L001-L005 and
L015 (phase 4), and the graph rules L008-L014 over the derived graph
(phase 5). L006/L007 (contradiction/tautology) need the tier-b truth-table
engine and join in phase 8 (equiv).

Rules are pure functions CatalogIR -> list[Violation] (graph rules receive
the pre-derived DerivedGraph too); lint_catalog derives the graph once and
runs the registry in code order and jobs in catalog (source) order, so
output is deterministic for identical input. L023 also takes a base zone and
a reference date, so its output is deterministic for a given reference date.

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
- L020 iced consumer (M19/M21, the last Part II requirement-3 detector)
  reads the consumer's condition tree, not the derived graph, so it is an
  IR-F rule (DL-243, DL-281). When every immediate predecessor translates
  to a UC Skip, AutoSys runs the consumer (SEM-05/SEM-20/SEM-22) while UC
  cascades the skip (UCS-02). One live predecessor converges, so the rule
  needs ALL of them.

Phase-5 graph-rule readings (each with a test):
- What counts as a start gate is `DerivedEdge.is_start_gate`, in derive, and
  no rule here restates it (DL-162: L009 and L020 were partitioning the same
  set from opposite ends). L008 and L011 deliberately ask other questions --
  one row, and any wiring at all -- and each says so.
- L008 fires on M16-classified box-override edges (non-member, global, or
  cross-instance refs -- derive's own SEM-12 detection); M15 (member ref)
  is the legitimate early-exit shape and stays quiet.
- L009 "unqualified s() feeding a scheduled consumer": a start-gate success
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
- L021 condition-only multi-fire (DL-180): an unscheduled, unboxed consumer
  with >=2 wake sources and >=1 unqualified latch can fire more than once
  per cycle -- the s(A)&s(B) double fire, and bare n() as guard-turned-
  trigger. Wake sources add `graph.bare_notrunning` to the start-gate
  edges, because mutex classification removes bare n() from the edge set
  and no edge reader sees that its targets still WAKE the consumer.
- L022 stranded-on-failure consumer (DL-181), L021's under-fire twin at
  info tier: a condition-only, unboxed consumer whose condition cannot
  survive a producer's FAILURE (tier-b, producer pinned to FAILURE --
  so OR escapes and f/d/e gates stay quiet), where no live consumer's
  START GATE reads that failure (f/d/t/e from the producer or an
  ancestor box, the SEM-11 default fold). The miss is at least one
  cycle; indefinite only when the producer has no schedule of its own.
  Alarms cannot exempt: observability, not control flow (DL-32).
- L023 schedule time changed by a DST gap or overlap (SEM-32, DL-249,
  DL-253, DL-260): an IR-F rule. It finds the days the zone's clock skips or
  repeats minutes, runs each configured time through the runner's own time
  functions (timezones.py) and reports a time whose result is not one
  instant at its written wall time. It restates no runner rule, so the
  message prints what the runner computes, and says so where the docs mark
  the change shape unverified. It never changes what the runner does.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal, NamedTuple

from pydantic import BaseModel

from dsl41.ast_jil import SourceSpan
from dsl41.backend_uc import SKIP_TRANSLATED
from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    GlobalAtom,
    Or,
    Paren,
    StatusAtom,
    iter_atoms,
    lookback_pitfalls,
)
from dsl41.derive import DerivedGraph, derive_graph, local_job, local_producer, start_gates
from dsl41.ir import (
    TIME_CLUSTER,
    CatalogIR,
    ExecSpec,
    FwSpec,
    ScheduleBlock,
    unquote_jil_value,
)
from dsl41.semantics import DEFAULTS, iced_atom_truth
from dsl41.timezones import (
    DstWindow,
    dst_windows,
    must_instant,
    resolve_timezone,
    start_mins_instants,
    start_time_instants,
    to_local,
    to_utc,
    wall_window_intervals,
    window_spans_near,
)

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
        loc = f"{self.span.at}: " if self.span else ""
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

    def suppress(self, codes: Iterable[str]) -> LintReport:
        """A copy without the given rule codes (DL-23: estate-wide accepted
        findings, e.g. a timezone-on-every-job convention drowning L005).
        Callers validate codes against RULE_CODES first -- a typo silently
        suppressing nothing would be its own silent loss."""
        dropped = set(codes)
        return LintReport(violations=[v for v in self.violations if v.code not in dropped])


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
                            detail=ref.key,
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
    """Unresolved global references (SEM-08): no insert_global and no
    SET_GLOBAL producer anywhere in the catalog. Two read sites, same
    dangling-name class but different severities (DL-25): `$$VAR`
    substitution sites are ERRORS (an empty/stale value lands in a command
    line -- broken), while `v(NAME)` condition atoms are WARN -- a
    comparison that stays false until something external sets the global
    can be an INTENDED cross-system gate (sem12_external_gate models
    exactly that), so it is surfaced for confirmation, not condemned."""
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
        seen_reads: set[tuple[str, str]] = set()
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                if not isinstance(atom, GlobalAtom):
                    continue
                if atom.name in catalog.globals_declared or atom.name in producers:
                    continue
                if (attr_name, atom.name) in seen_reads:
                    continue
                seen_reads.add((attr_name, atom.name))
                out.append(
                    Violation(
                        code="L002",
                        severity="warn",
                        message=(
                            f"{attr_name} of {job.name!r} reads global ${atom.name} (v())"
                            f" but the catalog neither declares it (insert_global) nor"
                            f" produces it (SET_GLOBAL) -- the comparison stays false"
                            f" until something external sets it; confirm that is the"
                            f" intended cross-system gate"
                        ),
                        jobs=[job.name],
                        span=span,
                        detail=atom.name,
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
    shape into passthrough, which is where we look. Re-verified for timezone
    against TechDocs 2026-07-09 (SEM-35 note); the old Q2-adjacent caveat
    (midnight-anchor re-basing) dissolved with Q2a (DL-54) -- the
    since-last-end anchor is tz-independent, so timezone here is
    unconditionally dead. Estates that carry these attrs as a convention can
    drop the code via `dsl41 lint --suppress L005` (DL-23)."""
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
    """Lookback raw-format pitfalls (SEM-04): valid-but-suspicious shapes.
    Severity is per shape (DL-24 field calibration): a bare-hours window
    (`12` = 12 hours) is valid, documented, and unambiguous to AutoSys --
    the only risk is an author who believed minutes -- so it is INFO
    (visible, never gates the exit code, --strict included). Single-digit
    minutes (`2.5` = 2h05m, NOT two-and-a-half hours) stays WARN: the token
    genuinely reads as a decimal. The shape facts come from
    conditions.lookback_pitfalls at parse time."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                lookback = getattr(atom, "lookback", None)
                if lookback is None:
                    continue
                # a bare-digits window raw only ever yields the bare-hours
                # pitfall, so the per-lookback severity split is exact
                bare_hours = lookback.kind == "window" and (lookback.raw or "").isdigit()
                for pitfall in lookback_pitfalls(lookback):
                    out.append(
                        Violation(
                            code="L015",
                            severity="info" if bare_hours else "warn",
                            message=f"{attr_name} of {job.name!r}: {pitfall}",
                            jobs=[job.name],
                            span=span,
                            detail=lookback.raw,
                        )
                    )
    return out


def rule_l016(catalog: CatalogIR) -> list[Violation]:
    """Dangling resource reference (DL-25): a `resources:` group names a
    resource with no insert_resource in the compilation set. Lowering
    deliberately does not enforce this (DL-21: definitions may live in a
    file outside the set); the assembled catalog is where it is decidable.
    warn, not error: AutoSys itself resolves against its database -- but
    the UC backend cannot create or size the Virtual Resource (UCS-09/M34)
    without the definition, so the migration set is incomplete."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        for ref in job.resources:
            if ref.name not in catalog.resources:
                out.append(
                    Violation(
                        code="L016",
                        severity="warn",
                        message=(
                            f"{job.name!r} requires resource {ref.name!r}"
                            f" (QUANTITY={ref.quantity}) but the compilation set has no"
                            f" insert_resource for it -- the UC backend cannot create or"
                            f" size the Virtual Resource (M34/UCS-09)"
                        ),
                        jobs=[job.name],
                        span=job.span,
                        detail=ref.name,
                    )
                )
    return out


def rule_l017(catalog: CatalogIR) -> list[Violation]:
    """Dangling machine reference (DL-25), fired ONLY when the compilation
    set defines at least one machine. Estates routinely lint job-only
    slices with machine records deliberately out of scope, so zero
    insert_machine keeps the rule quiet; once the set DOES carry machine
    records, a `machine:` value outside them is a real smell (typo, or a
    forgotten infra file). Comma lists (legacy load-balancing machine
    lists, dossier ss5) are checked per name. Boxes are skipped: their
    machine attr is inert passthrough (SEM-10)."""
    if not catalog.machines:
        return []
    out: list[Violation] = []
    for job in catalog.jobs.values():
        raw = job.exec_.machine if job.exec_ is not None else None
        if not raw:
            continue
        missing = [
            name
            for name in (part.strip() for part in raw.split(","))
            if name and name not in catalog.machines
        ]
        if missing:
            out.append(
                Violation(
                    code="L017",
                    severity="warn",
                    message=(
                        f"{job.name!r} targets machine(s) {', '.join(repr(m) for m in missing)}"
                        f" not defined in the compilation set (which defines"
                        f" {len(catalog.machines)} machine(s) -- typo, or a forgotten"
                        f" infra file?)"
                    ),
                    jobs=[job.name],
                    span=job.span,
                    detail=",".join(missing),
                )
            )
    return out


def rule_l018(catalog: CatalogIR) -> list[Violation]:
    """Dangling calendar reference (DL-36), fired ONLY when the compilation
    set carries at least one calendar or cycle definition (the L017
    convention: job-only slices keep autocal exports out of scope and stay
    quiet; once the set claims calendar coverage, an unresolved name is a
    typo or a forgotten export file). Checks job run_calendar /
    exclude_calendar against the calendar namespace (standard + extended)
    and, inside extended-calendar definitions, holcal (a calendar) and
    cyccal (a cycle). warn, not error: AutoSys resolves calendars against
    its database -- but the UC backend cannot reproduce the run days
    without the definition (M24), so the migration set is incomplete."""
    if not catalog.calendars and not catalog.cycles:
        return []
    out: list[Violation] = []
    for job in catalog.jobs.values():
        schedule = job.schedule
        if schedule is None:
            continue
        for attr in ("run_calendar", "exclude_calendar"):
            ref = getattr(schedule, attr)
            if ref and ref not in catalog.calendars:
                out.append(
                    Violation(
                        code="L018",
                        severity="warn",
                        message=(
                            f"{job.name!r} {attr} names calendar {ref!r} with no definition"
                            f" in the compilation set (which defines"
                            f" {len(catalog.calendars)} calendar(s)) -- typo, or a missing"
                            f" autocal export? (M24)"
                        ),
                        jobs=[job.name],
                        span=job.span,
                        detail=ref,
                    )
                )
    for calendar in catalog.calendars.values():
        if calendar.kind != "extended":
            continue
        for attr, defined, what in (
            ("holcal", catalog.calendars, "calendar"),
            ("cyccal", catalog.cycles, "cycle"),
        ):
            ref = unquote_jil_value(calendar.attrs.get(attr, ""))
            if ref and ref not in defined:
                out.append(
                    Violation(
                        code="L018",
                        severity="warn",
                        message=(
                            f"extended calendar {calendar.name!r} {attr} names {what}"
                            f" {ref!r} with no definition in the compilation set -- its"
                            f" generated dates cannot be reproduced (M24)"
                        ),
                        span=calendar.span,
                        detail=ref,
                    )
                )
    return out


def rule_l019(catalog: CatalogIR) -> list[Violation]:
    """Schedule + condition composition (the ir-design ss11 impact-ledger
    rule; Q3 resolved by citation, DL-58): a date_conditions job that also
    carries a `condition` ARMS on a tick blocked by a false condition
    (SEM-32 arm-and-wait) and fires late, whenever the condition next
    comes true -- a no-expiry latch. The migration hazard survives Q3's
    resolution: UC has no arm concept (M02), so every such job remains a
    per-estate migration-attention item."""
    out: list[Violation] = []
    for job in catalog.jobs.values():
        if job.schedule is None or job.sem.condition is None:
            continue
        out.append(
            Violation(
                code="L019",
                severity="warn",
                message=(
                    f"{job.name!r} combines date_conditions scheduling with a"
                    f" condition -- a tick with a false condition arms and fires"
                    f" late, without expiry (SEM-32 arm-and-wait, cited DL-58);"
                    f" the UC mapping has no arm concept (M02): review before"
                    f" migrating"
                ),
                jobs=[job.name],
                span=job.span,
            )
        )
    return out


# ------------------------------------------------------ tier-b rules (phase 8, equiv)


def rule_l006(catalog: CatalogIR) -> list[Violation]:
    """Contradiction (e.g. s(x)&f(x) same lookback scope): the condition is
    unsatisfiable over the ICE-FREE tier-b state space. Icing a referenced
    job can still rescue a LOOKBACK-qualified pair into satisfiability
    (SEM-05 keeps every atom kind true there, DL-243); an ORDINARY pair (no
    lookback at all) stays unsatisfiable even iced, since the vendor's
    ON_ICE table reads an ordinary f()/t()/exitcode() atom false there too
    (SEM-20, DL-243) -- so for that shape the ice-free framing changes
    nothing. The ice-free framing is deliberate either way (DL-14
    amendment): icing is intervention, not scheduling. Too-large conditions
    are skipped silently (tier-c territory)."""
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
    nothing. Pinning only the FIRST evaluation is sufficient: member
    conditions ARE re-evaluated event-driven during the box run (that is
    how in-box sequencing works), but a member runs at most once per box
    execution (SEM-10) -- a condition that cannot be false at first
    evaluation starts the member right then, and no later evaluation ever
    happens for it. The box-start model follows the oracle's catalog-order
    member starts (DL-14 amendment): siblings declared EARLIER may already
    be NEVER_RAN or RUNNING when this member is evaluated; siblings
    declared LATER are certainly NEVER_RAN. Unpinned tautology is vacuous
    by construction (every condition is falsifiable in the free model), so
    this rule only examines box members."""
    from dsl41.equiv import cond_truth_profile

    out: list[Violation] = []
    names_in_order = list(catalog.jobs)
    for job in catalog.jobs.values():
        box = job.box.box_name
        attr = job.sem.condition
        if box is None or attr is None:
            continue
        cond = attr.cond
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
            message = (
                f"condition of box member {job.name!r} can never be false when box"
                f" {box!r} first evaluates it, so the member starts with the box"
                f" right away -- and since a member runs at most once per box"
                f" execution (SEM-10), the mid-run re-evaluation that serves"
                f" conditions which START false never gets a turn for this one:"
                f" the condition gates nothing"
            )
            # The classic cause: n() on a sibling that cannot have started yet.
            n_never_ran = sorted(
                {
                    atom.job.name
                    for atom in iter_atoms(cond)
                    if not isinstance(atom, GlobalAtom)
                    and getattr(atom, "status", None) == "NOTRUNNING"
                    and atom.job.instance is None
                    and fixed.get(atom.job.name) == "NEVER_RAN"
                }
            )
            if n_never_ran:
                gates = ", ".join(f"n({name})" for name in n_never_ran)
                message += (
                    f"; likely cause: {gates} -- that sibling cannot have started"
                    " yet at first evaluation, and a condition only gates the"
                    " START (nothing re-checks it once the member is running), so"
                    " it is not an ongoing mutual exclusion (R6; use a"
                    " mutex/resource construct instead, M07)"
                )
            out.append(
                Violation(
                    code="L007",
                    severity="warn",
                    message=message,
                    jobs=[job.name],
                    span=attr.span,
                    detail=box,
                )
            )
    return out


# -------------------------------------------------------- graph rules (phase 5, IR-G)


def rule_l008(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """box_success/box_failure references a non-member (SEM-12 gating): if all
    members complete before the external condition is true, the box hangs
    RUNNING. Fires on exactly derive's M16 box-override edges.

    Deliberately narrower than the complement of `DerivedEdge.is_start_gate`
    (DL-162), and not a subset of it either way. The complement holds M15,
    M16 and M09; this rule wants M16 and only M16 -- every form of it, the
    global gate included, which is why the message below has a branch for
    one. What it must stay off is M15, the legitimate early-exit shape, and
    M09, which gates a start rather than a box. A row test says that; the
    predicate cannot (DL-162a). The message's external-vs-member wording
    reads the atom's `instance` fact, not `edge.src`'s shape (DL-175): the
    row is already M16-classified, so this decides WORDING only, but a
    string-shape decider is the same unsound spelling everywhere else in
    this codebase."""
    out: list[Violation] = []
    for edge in graph.edges:
        if edge.mapping_row != "M16":
            continue
        if edge.via == "global":
            gated_on = f"global variable {edge.src!r}"
        else:
            atom = edge.atom
            assert not isinstance(atom, GlobalAtom)  # via matches atom kind (DL-73)
            if atom.job.instance is not None:
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
        if not edge.is_start_gate:
            continue  # a box override folds a box, it does not start one (L008
            # owns the M16 half); via=="success" already excludes globals
        src = local_producer(edge, catalog)
        if src is None:
            continue  # undefined producer: L001's error, and a staleness warn
            # about a job that never ran would contradict it. Cross-instance:
            # derive placed that producer on another instance (M33), so this
            # catalog cannot say whether its latch is stale (DL-162a)
        consumer = catalog.jobs.get(edge.dst)
        if consumer is None or consumer.schedule is None:
            continue
        out.append(
            Violation(
                code="L009",
                severity="warn",
                message=(
                    f"scheduled job {edge.dst!r} depends on unqualified s({src})"
                    f" (SEM-01: the latch is indefinite -- a success from a previous"
                    f" cycle satisfies it; qualify with a lookback or confirm staleness"
                    f" is intended)"
                ),
                jobs=[edge.dst],
                span=edge.source_atom,
                detail=src,
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
    and not an FW source. Only reachable by manual sendevent.

    Deliberately wider than `DerivedEdge.is_start_gate` (DL-162), and the
    one reader of the four that must not use it: a job gated on a global,
    or named by a `box_success`, is wired -- something in this catalog
    reaches it, which is the whole question here. Only the SRC side skips
    globals, because a global is a pseudo-node and never a dangling job.
    The src side wires the RESOLVED local producer (`local_producer`), not
    the raw display form, so a cross-instance job spelled like a local one
    cannot shield it (DL-175, the S-EDGE class; DL-162a)."""
    wired: set[str] = set()
    for edge in graph.edges:
        wired.add(edge.dst)
        if edge.via != "global":
            local = local_producer(edge, catalog)
            if local is not None:
                wired.add(local)  # unresolved srcs are pseudo/foreign, not jobs
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
                f" or out (dangling container)"
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
    length) have NO documented spec (searched, NOT FOUND -- DL-55): names
    pass through verbatim and an invalid one fails loudly at create time;
    extend this rule only if the U3b live pull reveals real constraints."""
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


def _ice_atom_value(catalog: CatalogIR, atom: StatusAtom | ExitCodeAtom) -> str:
    """ "true"/"false"/"unknown" for one atom whose LOCAL producer is
    seeded ON_ICE or ON_NOEXEC at definition time. Every other producer
    (live, undefined, cross-instance) is UNKNOWN: this analysis resolves
    only the ice/noexec dimension, the conservative direction for
    everything else (DL-162a: a cross-instance `src` is the composite
    `name^INST`, read off the atom's `instance`, never off
    `src in catalog.jobs`).

    An ON_ICE producer reads the iced row at the default switch
    (`iced_atom_truth`, DL-243, DL-252). An ON_NOEXEC producer reads its
    bypass projection (SEM-22, DL-281): a bypass ends in SUCCESS and
    records no exit code, so an exit-code atom and a failure or
    terminated atom are false whatever the lookback, and every other
    atom is true."""
    local = local_job(atom, catalog)
    if local is None:
        return "unknown"
    status = catalog.jobs[local].sem.initial_status
    if status == "ON_ICE":
        return "true" if iced_atom_truth(atom, DEFAULTS.ice_lookback) else "false"
    if status != "ON_NOEXEC":
        return "unknown"
    if isinstance(atom, ExitCodeAtom) or atom.status in ("FAILURE", "TERMINATED"):
        return "false"
    return "true"


def _ice_cond_value(catalog: CatalogIR, cond: Cond) -> str:
    """3-valued (true/false/unknown) evaluation of `cond` under the
    ice/noexec substitution (`_ice_atom_value`), propagated through
    And/Or/Paren the way real boolean logic would: an And is false as soon
    as ONE operand is, whatever an unknown sibling might turn out to be,
    and symmetrically for Or.

    This replaces per-producer `any()` grouping over derived-graph edges
    (DL-243): that grouping let one SATISFIED atom on a producer hide
    another, FALSE, ordinary conjunct on the SAME producer --
    `f(ice,9999) & f(ice) & s(live)` read as no finding, though AutoSys's
    condition is permanently false there regardless of `live`. A full
    evaluation also drops a false positive on a disjunct: `f(ice) | s(live)`
    is UNKNOWN here (not false), because `s(live)` can still satisfy the Or
    -- both engines actually converge on that shape.

    A GlobalAtom reads TRUE (DL-162, settled, not reopened here): a global
    gate is not a predecessor, and it never decides the verdict on its own
    -- `s(icy) & v(G)=1` still fires the original direction (TRUE is the
    And identity) and `f(icy) & v(G)=1` still fires the blocking one (the
    And is false from `f(icy)` alone, whatever the global reads)."""
    if isinstance(cond, And):
        values = [_ice_cond_value(catalog, c) for c in cond.operands]
        if "false" in values:
            return "false"
        return "true" if all(v == "true" for v in values) else "unknown"
    if isinstance(cond, Or):
        values = [_ice_cond_value(catalog, c) for c in cond.operands]
        if "true" in values:
            return "true"
        return "false" if all(v == "false" for v in values) else "unknown"
    if isinstance(cond, Paren):
        return _ice_cond_value(catalog, cond.inner)
    if isinstance(cond, GlobalAtom):
        return "true"
    return _ice_atom_value(catalog, cond)


def rule_l020(catalog: CatalogIR) -> list[Violation]:
    """Iced/noexec consumer (M19/M21; Part II requirement 3's last
    detector), decided by evaluating the job's own `condition:` tree under
    the vendor's ON_ICE truth table (SEM-20, DL-243) and the ON_NOEXEC
    bypass projection (SEM-22, DL-281) rather than grouping
    `DerivedEdge.is_start_gate` edges per producer with `any()` (the prior
    design, which the docstring below's two divergences both broke). The
    condition tree alone is both necessary and sufficient once
    box-override attrs (`box_success`/`box_failure`, never a start gate)
    are excluded by reading `job.sem.condition` directly.

    Which statuses translate to UC Skip is NOT listed here: `backend_uc`'s
    `INITIAL_STATUS_CONTROL` says what UC does with each definition-time
    status, and `SKIP_TRANSLATED` is the "skip" half of that one table
    (DL-152). The trigger keys on that set. The atom value is AutoSys
    truth, so `_ice_atom_value` reads one row per status, ON_ICE and
    ON_NOEXEC (DL-281); `test_backend_uc.py` pins the set to those two.
    ON_HOLD is M20 Hold -- it blocks downstream on BOTH sides, so it is not
    this rule's business.

    Two divergences, opposite directions, both read off `_ice_cond_value`.
    (1) The whole condition evaluates to TRUE under the ice/noexec
    substitution, AND every local producer the condition names is itself
    iced/noexec-seeded (no live predecessor to converge with UC on): AutoSys
    runs the consumer, while in UC a task whose incoming edges are ALL
    skipped is itself Skipped, cascading on (UCS-02). One live predecessor
    is enough to converge, which is why this needs ALL of them -- an
    undefined or cross-instance reference is UNKNOWN to the evaluator (the
    conservative direction) and is excluded from the "every producer"
    check, so it cannot manufacture a false trigger on its own; it can
    still make the WHOLE condition unknown, correctly suppressing one. (2)
    The whole condition evaluates to FALSE: at least one iced/noexec
    producer is gated by a failure/terminated/exitcode atom (for an iced
    producer, one with no lookback) and no satisfying alternative ANYWHERE
    in the condition could rescue it, so AutoSys can never start the
    consumer at all (a definition-time seed never un-ices itself in a
    static catalog) --
    while UC's skip cascade, blind to which AutoSys atom kind an edge
    stands for, may still resolve the dependency and start it. The message
    names each such producer's actual seeded status (ON_ICE or
    ON_NOEXEC)."""
    out: list[Violation] = []
    for name, job in catalog.jobs.items():
        if job.sem.initial_status in SKIP_TRANSLATED:
            continue  # skipped on BOTH sides: no cascade divergence to flag
        if job.sem.condition is None:
            continue
        cond = job.sem.condition.cond
        local_atoms = [
            (atom, local)
            for atom in iter_atoms(cond)
            if not isinstance(atom, GlobalAtom) and (local := local_job(atom, catalog)) is not None
        ]
        iced_atoms = [
            (atom, local)
            for atom, local in local_atoms
            if catalog.jobs[local].sem.initial_status in SKIP_TRANSLATED
        ]
        if not iced_atoms:
            continue
        value = _ice_cond_value(catalog, cond)
        if value == "false":
            blocked = sorted(
                {local for atom, local in iced_atoms if _ice_atom_value(catalog, atom) == "false"}
            )
            if not blocked:
                continue
            named = ", ".join(
                f"{src!r} ({catalog.jobs[src].sem.initial_status})" for src in blocked
            )
            out.append(
                Violation(
                    code="L020",
                    severity="warn",
                    message=(
                        f"{name!r}'s condition can never be satisfied through {named}: a"
                        " failure/terminated/exitcode atom reads false against a"
                        " definition-time noexec producer, and against an iced one when it"
                        " has no lookback qualifier (DL-243, DL-281), so AutoSys never"
                        " starts the consumer through it, while UC's skip cascade may still"
                        " resolve the dependency and start it (M19/M21, UCS-02)"
                    ),
                    jobs=[name],
                    span=job.span,
                    detail=",".join(blocked),
                )
            )
            continue  # (2) fired; (1)'s premise (AutoSys runs it) cannot also hold
        if value != "true":
            continue
        sources = sorted({local for _atom, local in local_atoms})
        if not all(catalog.jobs[src].sem.initial_status in SKIP_TRANSLATED for src in sources):
            continue  # a live predecessor converges with UC -- not this divergence
        listed = ", ".join(repr(src) for src in sources)
        out.append(
            Violation(
                code="L020",
                severity="warn",
                message=(
                    f"every immediate predecessor of {name!r} ({listed}) translates to"
                    " a UC Skip (M19/M21): AutoSys runs the consumer -- an iced/noexec"
                    " producer satisfies a success/done/notrunning atom, and an iced one"
                    " also any lookback-qualified atom (DL-243, open question Q10) --"
                    " while UC cascades the skip onto it (UCS-02)"
                ),
                jobs=[name],
                span=job.span,
                detail=",".join(sources),
            )
        )
    return out


def rule_l021(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Condition-only multi-fire (SEM-01 latch x DL-13 event wake): an
    unscheduled, unboxed consumer starts on EVERY event that finds its
    condition true. With two or more wake sources and at least one
    unqualified latching atom, one estate cycle can start it more than
    once -- up to once per source when every latch is unqualified: the
    s(A) & s(B) double fire, either job's completion finding the other's
    latch still true, and the bare n(C) guard, which every completion of C
    turns into a TRIGGER while the other atoms stay latched. Both shapes
    fired one reported job twice a day (DL-180).

    Exemptions, each a cap this rule would otherwise restate: a scheduled
    consumer arms and runs at most once per tick (SEM-32; its staleness
    story is L009's), and a box member starts at most once per box
    execution (SEM-10). Lookback-qualified atoms are exempt as LATCHES --
    the qualifier is the fix, s(x,0) anchors to the consumer's own last
    end (SEM-04) -- but their producers still wake, so they still count as
    wake sources. A condition of bare n() guards ALONE stays quiet -- the
    M07 mutex idiom (R6): an n() satisfaction is current truth about the
    partner, not a recorded completion that outlives its cycle, so it is a
    wake source and never a latch (the cross-instance bare form, which
    stays an edge under M33, gets the same reading). Global gates
    are OUTSIDE the rule, as wake and as latch both: a v() flag does wake
    on SET_GLOBAL and never expires (SEM-08), but whether it is stale is
    reset discipline in the SETTING system, which this catalog cannot see
    -- the arm-on-flags pattern is common and intentional, and flagging
    every one would bury the s()&s() signal (DL-180; globals stay L002's).

    Wake sources: is_start_gate edges (an undefined local producer is
    dropped -- it emits no events, and L001 owns it; a cross-instance one
    counts, SEM-07) and the consumer's own bare n() targets from
    graph.bare_notrunning -- mutex-classified out of the edge set, so no
    edge reader sees that they wake. Self counts: n(self) next to a latch
    re-triggers the job on its own completion, a tight loop L010 cannot
    see because mutex refs are not edges."""
    gates = start_gates(graph)
    out: list[Violation] = []
    for name, job in catalog.jobs.items():
        if job.schedule is not None or job.box.box_name is not None:
            continue
        if job.sem.initial_status in SKIP_TRANSLATED:
            continue  # an iced/noexec consumer cannot start at all (SEM-20):
            # nothing to multi-fire (arch-review 2026-08-28)
        wakes: set[str] = set()
        latches: set[str] = set()
        span: SourceSpan | None = None
        for edge in gates.get(name, ()):
            atom = edge.atom
            assert not isinstance(atom, GlobalAtom)  # is_start_gate excludes via=="global"
            # not local_producer alone (DL-162a): the atom-level instance
            # fact splits its None -- a cross-instance producer stays a wake
            # source HERE, while L022 drops it (the remote estate's wiring
            # is invisible only for the consumption question)
            if atom.job.instance is None and local_producer(edge, catalog) is None:
                continue  # undefined local: emits no events, L001's error
            wakes.add(edge.src)
            if edge.lookback is None and edge.via != "notrunning":
                latches.add(edge.src)
            if span is None:
                span = edge.source_atom
        for target in graph.bare_notrunning.get(name, ()):
            if target in catalog.jobs:
                wakes.add(target)
        if len(wakes) < 2 or not latches:
            continue
        listed = ", ".join(repr(src) for src in sorted(wakes))
        stale = ", ".join(repr(src) for src in sorted(latches))
        out.append(
            Violation(
                code="L021",
                severity="warn",
                message=(
                    f"condition-only job {name!r} can fire more than once per cycle:"
                    f" any event on {listed} re-evaluates it, and an unqualified atom"
                    " stays satisfied (SEM-01/DL-13) -- a bare n() guard is also a"
                    f" trigger; qualify the latches ({stale}) with lookbacks (s(x,0))"
                    " or schedule the job (the SEM-32 arm caps runs at one per tick)"
                ),
                jobs=[name],
                span=span,
                detail=",".join(sorted(wakes)),
            )
        )
    return out


#: L022: the via kinds whose presence as a start gate means a producer's
#: FAILURE releases SOMETHING -- f()/t() directly, d() covers both ends,
#: e() reads the exit code a run recorded, failing runs included.
#: notrunning is deliberately absent: a mutex partner freed by the failure
#: is not a reaction to it.
_FAILURE_CONSUMING_VIA: frozenset[str] = frozenset({"failure", "terminated", "done", "exitcode"})


def rule_l022(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Stranded-on-failure consumer (DL-181): when a producer its condition
    cannot do without fails, a condition-only, unboxed job misses at least
    one cycle -- the FAILURE latch satisfies nothing it gates on (SEM-01),
    and if no condition reads that failure, no job starts because of it:
    release waits for that producer's next SUCCESS (its own next tick
    where it has one, a rerun, or an operator). A producer with no
    schedule of its own makes the wait indefinite. The vendor's JOBFAILURE
    alarm does fire (alarm_if_fail defaults on), but alarms are
    observability, not control flow (DL-32) -- which is why alarm
    attributes cannot exempt. SEM-14 terminators are failure control flow
    but they only KILL; they release no waiting consumer, so they neither
    exempt nor contradict.

    "Cannot do without" is tier-b, not shape-matching: the producer's
    status is PINNED to FAILURE and the condition asked whether any state
    still satisfies it (`cond_truth_profile`, the L006/L007 engine). So
    the OR escape s(a)|s(b) stays quiet -- either branch survives the
    other's failure -- and so do f()/d()/e()-gated consumers, whose atoms
    the failure itself satisfies. A too-large condition is skipped
    silently, like L006 (tier-c territory).

    "A condition reads the failure" means a START GATE of a live consumer
    -- a job that would run because of it. Box-override edges do not
    count: an override CLASSIFIES the box's fold, it starts nothing.
    Reads of an ancestor box count, because a member's failure folds its
    box FAILURE under the default fold (SEM-11) -- an approximation in
    the quiet direction: a box_success/box_failure override can defeat
    that fold, and this info-tier rule does not model overrides. Skipped
    as producers: cross-instance jobs (the remote estate's failure wiring
    is invisible, DL-162a's frame) and skip-translated ones (an iced
    producer cannot run, so it cannot fail -- its story is L020's).
    Skipped as consumers: scheduled jobs (the armed-tick wait is L019's
    migration-attention item) and box members (a member waiting on a
    failed external producer hangs its BOX -- SEM-12's family, L008's
    neighborhood). Severity is info: estates conventionally lean on the
    alarm plane, so this is an inventory of where control flow ends, not
    a defect list."""
    from dsl41.equiv import cond_truth_profile

    # keyed on RESOLVED catalog names, never edge.src (arch-review
    # 2026-08-28): src is the display form -- "name^INST" for a
    # cross-instance atom -- and querying it with catalog names is the
    # DL-162a unsoundness. local_producer drops what cannot be a local
    # producer anyway (undefined and remote reads credit nothing here).
    consumed: set[str] = {
        producer
        for edge in graph.edges
        if edge.via in _FAILURE_CONSUMING_VIA
        and edge.is_start_gate
        and (reader := catalog.jobs.get(edge.dst)) is not None
        and reader.sem.initial_status not in SKIP_TRANSLATED
        and (producer := local_producer(edge, catalog)) is not None
    }
    parents = graph.box_tree.parent

    def failure_is_read(producer: str) -> bool:
        current: str | None = producer
        while current is not None:
            if current in consumed:
                return True
            current = parents.get(current)
        return False

    gates = start_gates(graph)
    out: list[Violation] = []
    for name, job in catalog.jobs.items():
        attr = job.sem.condition
        if attr is None or job.schedule is not None or job.box.box_name is not None:
            continue
        if job.sem.initial_status in SKIP_TRANSLATED:
            continue  # the consumer itself never runs (SEM-20): no strand
        candidates: set[str] = set()
        span: SourceSpan | None = None
        for edge in gates.get(name, ()):
            producer = local_producer(edge, catalog)
            if producer is None:
                continue  # undefined local (L001's error) or cross-instance (DL-162a)
            if catalog.jobs[producer].sem.initial_status in SKIP_TRANSLATED:
                continue  # cannot run, cannot fail (L020's story)
            candidates.add(producer)
            if span is None:
                span = edge.source_atom
        stranding: list[str] = []
        for producer in sorted(candidates):
            profile = cond_truth_profile(attr.cond, fixed_status={producer: "FAILURE"})
            if profile is None:
                # the state ceiling reads the condition alone, never the pin,
                # so the FIRST answer decides for every producer: skip the
                # consumer whole (tier-c territory, the L006 precedent).
                # Nothing is appended before this break can fire.
                break
            satisfiable, _ = profile
            if not satisfiable and not failure_is_read(producer):
                stranding.append(producer)
        if not stranding:
            continue
        listed = ", ".join(repr(p) for p in stranding)
        out.append(
            Violation(
                code="L022",
                severity="info",
                message=(
                    f"condition-only job {name!r} misses at least one cycle if any of"
                    f" {listed} fails: the FAILURE latch satisfies nothing it gates on"
                    " (SEM-01), no start gate in the estate reads that failure (f/d/t/e),"
                    " and the JOBFAILURE alarm is observability, not control flow (DL-32)"
                    " -- release waits for the producer's next SUCCESS (its own next tick"
                    " where it has one, a rerun, or an operator); give the failure a"
                    " consumer if a silently missed cycle is not acceptable"
                ),
                jobs=[name],
                span=span,
                detail=",".join(stranding),
            )
        )
    return out


# ------------------------------------------------------------------ L023: DST hours


def _reference_today() -> date:
    """The reference date L023 reads when the caller gives none."""
    return date.today()


class _Outcome(NamedTuple):
    """What the runner does with one configured time on a DST change day,
    against the plain reading (one instant at the written wall time)."""

    kind: Literal["same", "shifted", "first", "second", "none", "twice"]
    text: str


def _clock(moment: datetime) -> str:
    return f"{moment:%H:%M:%S}" if moment.second else f"{moment:%H:%M}"


def _utc_label(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _wall_outcome(naive: datetime, instant: datetime, tz: tzinfo, verb: str) -> _Outcome:
    """One engine instant the runner computed for the wall time `naive`:
    unchanged, moved to another local time, or one pass of a repeated hour."""
    local = to_local(instant, tz)
    if local != naive:
        later = local.date() > naive.date()
        where = "" if local.date() == naive.date() else (" next day" if later else " previous day")
        return _Outcome("shifted", f"{verb} {_clock(local)}{where}")
    first = naive.replace(tzinfo=tz, fold=0).utcoffset()
    second = naive.replace(tzinfo=tz, fold=1).utcoffset()
    if first is not None and second is not None and first > second:
        if instant == to_utc(naive, tz):
            kind: Literal["first", "second"] = "first"
            offset = first
        else:
            kind = "second"
            offset = second
        return _Outcome(
            kind, f"{verb} {_clock(naive)}, once, in the {kind} pass ({_utc_label(offset)})"
        )
    return _Outcome("same", f"{verb} {_clock(naive)}")


def _start_time_outcomes(
    day: date, times: list[tuple[int, int]], tz: tzinfo
) -> dict[int, _Outcome]:
    """The default (`vendor`) result for each start_times entry on `day`,
    from `start_time_instants`; an entry that shares its instant with
    another is merged into one tick."""
    got = dict(start_time_instants(day, times, tz, dst="vendor"))
    out: dict[int, _Outcome] = {}
    for index, (hour, minute) in enumerate(times):
        naive = datetime.combine(day, time(hour, minute))
        if index not in got:
            out[index] = _Outcome("none", "does not run")
            continue
        outcome = _wall_outcome(naive, got[index], tz, "runs at")
        twins = [j for j in got if j != index and got[j] == got[index] and times[j] != times[index]]
        if twins:
            other = "%02d:%02d" % times[min(twins, key=lambda j: times[j])]
            outcome = _Outcome(
                "shifted",
                f"{outcome.text}, the same tick as the {other} start time (one run; unverified)",
            )
        out[index] = outcome
    return out


def _must_outcome(
    day: date,
    times: list[tuple[int, int]],
    index: int,
    must: tuple[int, int],
    tz: tzinfo,
) -> _Outcome:
    """The deadline of one absolute must time whose start is start_times
    entry `index` on `day` (DL-253), or why none is armed there."""
    starts = _start_time_outcomes(day, times, tz)
    if starts[index].kind == "none":
        return _Outcome("none", "is never armed: its start time does not run that day")
    got = dict(start_time_instants(day, times, tz, dst="vendor"))
    ahead = [j for j in got if got[j] == got[index] and times[j] < times[index]]
    if ahead:
        other = "%02d:%02d" % times[min(ahead, key=lambda j: times[j])]
        return _Outcome(
            "none", f"is never armed: its start shares the tick of the {other} start (unverified)"
        )
    days, hour = divmod(must[0], 24)
    naive = datetime.combine(day + timedelta(days=days), time(hour, must[1]))
    return _wall_outcome(naive, must_instant(day, times[index], must, tz), tz, "is due at")


def _window_outcomes(day: date, lo: time, hi: time, tz: tzinfo) -> list[str]:
    """How run_window `lo`-`hi` differs from its written endpoints around the
    change on `day`: the vendor's endpoint rules where `dst_change` names the
    shape (DL-249), the wall-time comparison elsewhere."""
    spans = window_spans_near(day, lo, hi, tz)
    texts: list[str] = []
    if spans is not None:
        for opening, (opens, closes) in zip((day - timedelta(days=1), day), spans[1:3]):
            naive_open = datetime.combine(opening, lo)
            naive_close = datetime.combine(opening if lo <= hi else opening + timedelta(days=1), hi)
            opened = _wall_outcome(naive_open, opens, tz, "opens at")
            closed = _wall_outcome(naive_close, closes, tz, "closes at")
            if opened.kind != "same" or closed.kind != "same":
                texts.append(f"{opened.text} and {closed.text}")
        return texts
    week = timedelta(days=7)
    one = timedelta(days=1)
    here = wall_window_intervals(day - one, day + one, lo, hi, tz)
    usual = {
        (start + week, end + week)
        for start, end in wall_window_intervals(day - 8 * one, day - 6 * one, lo, hi, tz)
    }
    # only the opening and closing wall times and the number of openings count:
    # a day a DST change makes longer or shorter leaves an overnight window alone
    for number, (start, end) in enumerate(here):
        if (start, end) in usual:
            continue
        again = any(earlier_end >= start for _, earlier_end in here[:number])
        verb = "opens a second time at" if again else "opens at"
        texts.append(f"{verb} {_clock(start)} and closes at {_clock(end)}")
    if not texts and usual - set(here):
        texts.append("never opens")
    return texts


def _start_mins_clause(day: date, minutes: list[int], tz: tzinfo) -> str:
    """The start_mins ticks of every hour on `day` that the runner does not
    run once at their written time, grouped by what happens to them."""
    groups: dict[str, list[str]] = {}
    for hour in range(24):
        for minute in minutes:
            naive = datetime.combine(day, time(hour, minute))
            got = start_mins_instants(day, [(hour, minute)], tz, dst="vendor")
            if not got:
                kind, text = "do not run", f"{naive:%H:%M}"
            elif len(got) > 1:
                kind, text = "run twice", f"{naive:%H:%M}"
            else:
                outcome = _wall_outcome(naive, got[0], tz, "runs at")
                if outcome.kind == "same":
                    continue
                if outcome.kind == "shifted":
                    kind, text = "move", f"{naive:%H:%M} {outcome.text}"
                else:
                    kind, text = f"run once, in the {outcome.kind} pass", f"{naive:%H:%M}"
            groups.setdefault(kind, []).append(text)
    return "; ".join(f"{kind}: {', '.join(ticks)}" for kind, ticks in groups.items())


def _day_note(window: DstWindow) -> str:
    skipped = window.kind == "gap"
    note = f"on a day the clock {'skips' if skipped else 'repeats'} {window.label}"
    return note if window.documented else f"{note} (unverified shape)"


def _l023_advice(own_zone: bool) -> str:
    if own_zone:
        return (
            "Move the time out of the affected hour, or set this job's timezone to UTC if it"
            " need not follow local time."
        )
    return (
        "Move the time out of the affected hour, or use UTC as the base zone (--timezone)"
        " for an estate that runs around the clock across regions."
    )


def _schedule_findings(
    schedule: ScheduleBlock, windows: tuple[DstWindow, ...], tz: tzinfo
) -> dict[tuple[str, str], list[str]]:
    """(attribute, written time) -> one clause per change day that moves it,
    for start_times, absolute must times and run_window."""
    found: dict[tuple[str, str], list[str]] = {}

    def note(attr: str, shown: str, window: DstWindow, text: str, subject: str = "it") -> None:
        found.setdefault((attr, shown), []).append(f"{_day_note(window)} {subject} {text}")

    starts = [(t.hour, t.minute) for t in schedule.start_times or []]
    for window in windows:
        for index, outcome in _start_time_outcomes(window.day, starts, tz).items():
            if outcome.kind != "same":
                note("start_times", "%02d:%02d" % starts[index], window, outcome.text)
        for attr, sla in (
            ("must_start_times", schedule.must_start),
            ("must_complete_times", schedule.must_complete),
        ):
            for index, must in enumerate((sla.times or [])[: len(starts)] if sla else []):
                lead = timedelta(days=must.hour // 24)
                for start_day in sorted({window.day, window.day - lead}):
                    result = _must_outcome(start_day, starts, index, (must.hour, must.minute), tz)
                    if result.kind != "same":
                        note(attr, "%02d:%02d" % (must.hour, must.minute), window, result.text)
        if schedule.run_window is not None:
            lo, hi = schedule.run_window
            shown = f"{lo.hour:02d}:{lo.minute:02d}-{hi.hour:02d}:{hi.minute:02d}"
            for text in _window_outcomes(
                window.day, time(lo.hour, lo.minute), time(hi.hour, hi.minute), tz
            ):
                note("run_window", shown, window, text, "the window")
    return found


def rule_l023(
    catalog: CatalogIR,
    base_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    today: date | None = None,
) -> list[Violation]:
    """A schedule time that a DST change moves, drops, merges or runs twice
    (SEM-32, DL-249, DL-253, DL-260). The runtime is settled; the rule tells
    the author what the runner does.

    The job's own `timezone` decides the zone, else `base_tz` (UTC when
    None), resolved as `dsl41 run --timezone` does; a name that does not
    resolve is skipped, other paths report it. On each day the zone's clock
    skips or repeats minutes, in the reference year (`today`, default the
    current date) and the next, each configured time goes through the
    runner's own functions: `start_time_instants` and `start_mins_instants`
    for start and tick times, `must_instant` for absolute must times, and
    `window_spans_near` and `wall_window_contains` for run_window. A time is
    flagged when the result is not one instant at its written wall time;
    the message prints the computed result. Output is deterministic for a
    given reference date. Each flagged time is a warn; a `start_mins` job
    is one info, because it crosses every hour."""
    reference = today if today is not None else _reference_today()
    base = base_tz if base_tz is not None else "UTC"
    zones: dict[str, tuple[tzinfo, tuple[DstWindow, ...]] | None] = {}

    def zone_of(name: str) -> tuple[tzinfo, tuple[DstWindow, ...]] | None:
        if name not in zones:
            resolved = resolve_timezone(name, tz_aliases)
            zones[name] = (
                None if resolved is None else (resolved.tz, dst_windows(resolved.tz, reference))
            )
        return zones[name]

    out: list[Violation] = []
    for job in catalog.jobs.values():
        schedule = job.schedule
        if schedule is None:
            continue
        zone_name = schedule.timezone if schedule.timezone is not None else base
        resolved_zone = zone_of(zone_name)
        if resolved_zone is None or not resolved_zone[1]:
            continue
        tz, windows = resolved_zone
        advice = _l023_advice(schedule.timezone is not None)
        for (attr, shown), clauses in _schedule_findings(schedule, windows, tz).items():
            out.append(
                Violation(
                    code="L023",
                    severity="warn",
                    message=(
                        f"{attr} {shown} of {job.name!r} is changed by DST in {zone_name}."
                        f" Under the runner's rules, {'; '.join(dict.fromkeys(clauses))}."
                        f" {advice}"
                    ),
                    jobs=[job.name],
                    span=job.span,
                    detail=f"{attr} {shown}",
                )
            )
        if schedule.start_mins:
            ticks = [
                f"{_day_note(window)}, ticks {clause}"
                for window in windows
                if (clause := _start_mins_clause(window.day, schedule.start_mins, tz))
            ]
            out.append(
                Violation(
                    code="L023",
                    severity="info",
                    message=(
                        f"start_mins of {job.name!r} repeats every hour, so a DST change in"
                        f" {zone_name} reaches it. Under the runner's rules,"
                        f" {'; '.join(ticks) or 'no tick is moved'}. {advice}"
                    ),
                    jobs=[job.name],
                    span=job.span,
                    detail="start_mins",
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
    ("L016", rule_l016),
    ("L017", rule_l017),
    ("L018", rule_l018),
    ("L019", rule_l019),
    ("L020", rule_l020),
    ("L023", rule_l023),
)

GRAPH_RULES: tuple[tuple[str, GraphRuleFn], ...] = (
    ("L008", rule_l008),
    ("L009", rule_l009),
    ("L010", rule_l010),
    ("L011", rule_l011),
    ("L012", rule_l012),
    ("L013", rule_l013),
    ("L014", rule_l014),
    ("L021", rule_l021),
    ("L022", rule_l022),
)


#: Every registered rule code (stable Lnnn identifiers, ir-design ss9);
#: the CLI validates --suppress values against this set.
RULE_CODES: frozenset[str] = frozenset(code for code, _ in RULES) | frozenset(
    code for code, _ in GRAPH_RULES
)


def lint_catalog(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    base_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    today: date | None = None,
) -> LintReport:
    """Run every registered rule; deterministic for identical input and
    reference date. The
    derived graph is computed once (or passed in by a caller that already
    has it); report order is IR-F rules first, then graph rules, each block
    in code order. `base_tz`, `tz_aliases` and `today` reach L023 only: the
    zone of a schedule that sets no `timezone` (UTC when None), the
    `--timezone-map` table, and the reference date for DST changes."""
    if graph is None:
        graph = derive_graph(catalog)
    violations: list[Violation] = []
    for code, rule in RULES:
        if code == "L023":
            violations.extend(
                rule_l023(catalog, base_tz=base_tz, tz_aliases=tz_aliases, today=today)
            )
        else:
            violations.extend(rule(catalog))
    for _code, graph_rule in GRAPH_RULES:
        violations.extend(graph_rule(catalog, graph))
    return LintReport(violations=violations)
