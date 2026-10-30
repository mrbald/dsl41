"""Linter: findings, not treatments (ir-design ss9).

The rules are phases 4 and 5 of the DL-03 order. A rule is a pure function
CatalogIR -> list[Violation]; graph rules also take the DerivedGraph. lint_catalog derives the
graph once and runs the registry in code order, jobs in catalog order, so output is
deterministic. L023 also takes a base zone and a reference date. L006 and L007 use the tier-b
engine in equiv.

Rule readings:
- L001: a local job ref must exist. A cross-instance ref (`job^INST`) fires only when INST is
  not declared via insert_xinst; declared instances are external boundary markers (M33), not
  dangling references (SEM-06, SEM-07).
- L002: a `$$VAR` site resolves if the catalog declares the global or some job command embeds
  `sendevent -E SET_GLOBAL` for it. Producer detection is a textual heuristic that
  over-approximates, the conservative direction for an error rule. value() atoms are not
  checked: SET_GLOBAL from outside the catalog is routine.
- L003 and L004 are enforced upstream and stay registered so the code space matches ss9.
  L004 is a defensive scan: a SEM-31-violating ScheduleBlock survives only if model_construct
  is used at every containing level. L003 is a tripwire: GlobalAtom has no lookback field.
- L005 reads the SEM-30 dead-config routing: time attributes with falsy or absent
  date_conditions sit verbatim in JobIR.passthrough.
- L020 iced consumer (M19/M21) reads the consumer's condition tree, so it is an IR-F rule
  (DL-243, DL-281). When every immediate predecessor translates to a UC Skip, AutoSys runs
  the consumer (SEM-05/SEM-20/SEM-22) and UC cascades the skip (UCS-02). One live predecessor
  converges, so the rule needs all of them.

Graph-rule readings:
- A start gate is `DerivedEdge.is_start_gate`, in derive; no rule here restates it (DL-162).
  L008 and L011 ask other questions (one row; any wiring at all) and say so.
- L008 fires on M16 box-override edges (non-member, global or cross-instance refs, by derive's
  SEM-12 detection). M15 (member ref) is the legitimate early-exit shape and stays quiet.
- L009: a start-gate success edge with no lookback whose consumer has date_conditions
  scheduling. The consumer's time trigger can fire on a latch left from a previous producer
  run (SEM-01/R1). Producer-side scheduling is irrelevant; M01 same-cycle classification does
  not exempt it.
- L010 reports derive's SCC cycles, one violation per cycle naming the sorted node set.
- L011: no schedule, no derived edges in or out (global and mutex participation count as
  wiring), not in a box, not a box with members, not an FW source. Purely manual utility
  jobs are the accepted false positive.
- L012 info: one finding per mutex group (M07), suggesting the UC construct.
- L013: a box member with its own date_conditions schedule needs the box RUNNING too (SEM-31).
- L014 UC-side name collision (UCS-12): lowering refuses exact duplicates, so the rule checks
  case-insensitive collision. Error severity per ss9.
- L021 condition-only multi-fire (DL-180): an unscheduled, unboxed consumer with >=2 wake
  sources and >=1 unqualified latch can fire more than once per cycle. Wake sources add
  `graph.bare_notrunning` to the start-gate edges, because mutex classification removes bare
  n() from the edge set.
- L022 stranded-on-failure consumer (DL-181), info tier: a condition-only, unboxed consumer
  whose condition cannot survive a producer's FAILURE (tier-b, producer pinned to FAILURE),
  where no live consumer's start gate reads that failure (the SEM-11 default fold). Alarms
  cannot exempt: observability, not control flow (DL-32).
- L023 schedule time changed by a DST gap or overlap (SEM-32, DL-249, DL-253, DL-260): an
  IR-F rule. It runs each configured time through the runner's time functions (timezones.py)
  and reports a time whose result is not one instant at its written wall time. The message
  prints what the runner computes and says so where the docs mark the change shape unverified.
  It never changes what the runner does.
- L024 and L025 read the idempotence sidecar (DL-424), which lint receives as `exceptions`; with
  None both are inert. L024 is an info finding per job the file names, so an operator sees
  every job whose interrupted attempt waits for a decision; info never fails `--strict`. L025
  is an error for a name in the file that the catalog does not define or defines as a box or a
  file watcher.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal, NamedTuple

from pydantic import BaseModel

from dsl41.ast_jil import SourceSpan
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
from dsl41.idempotence import EXCEPTIONS_FILE, Exceptions
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

#: The initial statuses UC translates to a Skip (M19 ON_ICE, M21 ON_NOEXEC; DL-152). ON_HOLD is
#: M20 Hold, which blocks downstream on both sides.
SKIP_TRANSLATED: frozenset[str] = frozenset({"ON_ICE", "ON_NOEXEC"})


Severity = Literal["error", "warn", "info"]

_SEVERITY_RANK: dict[Severity, int] = {"error": 2, "warn": 1, "info": 0}


class Violation(BaseModel):
    code: str  # stable "Lnnn", never renumbered (ir-design ss9)
    severity: Severity
    message: str
    jobs: list[str] = []  # empty for catalog-level findings
    span: SourceSpan | None = None
    detail: str | None = None  # machine-usable hook: referenced name, attr or token

    def render(self) -> str:
        loc = f"{self.span.at}: " if self.span else ""
        return f"{loc}{self.code} {self.severity}: {self.message}"


class LintReport(BaseModel):
    violations: list[Violation] = []

    def exit_code(self, strict: bool = False) -> int:
        """0 clean; 1 on any error, or on any warning when strict. Info never counts."""
        threshold = _SEVERITY_RANK["warn"] if strict else _SEVERITY_RANK["error"]
        if any(_SEVERITY_RANK[v.severity] >= threshold for v in self.violations):
            return 1
        return 0

    def by_code(self, code: str) -> list[Violation]:
        return [v for v in self.violations if v.code == code]

    def suppress(self, codes: Iterable[str]) -> LintReport:
        """A copy without the given rule codes (DL-23); callers validate them against RULE_CODES."""
        dropped = set(codes)
        return LintReport(violations=[v for v in self.violations if v.code not in dropped])


# ------------------------------------------------------------------------- the rules


def rule_l001(catalog: CatalogIR) -> list[Violation]:
    """Condition references an undefined job (SEM-06): the atom is permanently false."""
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
                # a false condition never auto-starts the job; a false box override never
                # fires (SEM-12; L008 covers the non-member case)
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


#: A command mentioning SET_GLOBAL produces every `-G NAME=` assignment it contains.
_SET_GLOBAL_ASSIGN_RE = re.compile(r"-G\s+\"?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _set_global_producers(catalog: CatalogIR) -> set[str]:
    producers: set[str] = set()
    for job in catalog.jobs.values():
        if isinstance(job.exec_, ExecSpec) and "SET_GLOBAL" in job.exec_.command:
            producers.update(m.group(1) for m in _SET_GLOBAL_ASSIGN_RE.finditer(job.exec_.command))
    return producers


def rule_l002(catalog: CatalogIR) -> list[Violation]:
    """Unresolved global (SEM-08): no insert_global and no SET_GLOBAL producer in the catalog.

    A `$$VAR` site is an error. A `v(NAME)` atom is a warn, because a comparison that stays
    false until something external sets the global can be an intended gate (DL-25).
    """
    producers = _set_global_producers(catalog)
    out: list[Violation] = []
    for job in catalog.jobs.values():
        attrs_by_name: dict[str, list[str]] = {}  # in site order
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
    """Lookback on a value() atom (SEM-04): a tripwire; GlobalAtom has no lookback field."""
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
    """SEM-31 mutual exclusivity: catches hand-built IR that bypassed ScheduleBlock validation."""
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
    """Time attributes present while date_conditions is falsy or absent (SEM-30): dead config.

    Timezone is dead here too, because the since-last-end anchor is tz-independent (SEM-35,
    Q2, Q2a, DL-54). `dsl41 lint --suppress L005` drops the code (DL-23).
    """
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
    """Lookback raw-format pitfalls (SEM-04): valid but suspicious shapes.

    Severity is per shape (DL-24). A bare-hours window (`12`) is info. Single-digit minutes
    (`2.5` is 2h05m) are warn. The shapes come from conditions.lookback_pitfalls.
    """
    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            for atom in iter_atoms(cond):
                lookback = getattr(atom, "lookback", None)
                if lookback is None:
                    continue
                # a bare-digits window yields only the bare-hours pitfall
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
    """Dangling resource reference (DL-25): no insert_resource in the compilation set.

    Lowering does not enforce this (DL-21); the assembled catalog decides it. Warn: the UC
    backend cannot size the Virtual Resource without the definition (UCS-09/M34).
    """
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
    """Dangling machine reference (DL-25), only when the set defines at least one machine.

    Comma lists are checked per name. Boxes are skipped: their machine attr is inert
    passthrough (SEM-10).
    """
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
    """Dangling calendar reference (DL-36), only when the set carries a calendar or cycle.

    Checks run_calendar and exclude_calendar against the calendar namespace, and holcal and
    cyccal inside extended calendars. Warn: the UC backend cannot reproduce the run days
    without the definition (M24).
    """
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
    """Schedule plus condition (ir-design ss11; Q3 resolved by DL-58).

    A date_conditions job with a `condition` arms on a tick blocked by a false condition
    (SEM-32) and fires late, with no expiry. UC has no arm concept (M02).
    """
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


# ---------------------------------------------------------------------- tier-b rules (equiv)


def rule_l006(catalog: CatalogIR) -> list[Violation]:
    """Contradiction (e.g. s(x)&f(x) in one lookback scope): unsatisfiable over the ice-free
    tier-b state space.

    Icing can rescue a lookback-qualified pair (SEM-05, DL-243) but not an ordinary pair
    (SEM-20, DL-243). The ice-free framing holds either way: icing is intervention, not
    scheduling (DL-14). Conditions too large for tier b are skipped.
    """
    from dsl41.equiv import cond_truth_profile

    out: list[Violation] = []
    for job in catalog.jobs.values():
        for attr_name, cond, span in job.iter_conditions():
            profile = cond_truth_profile(cond)
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
    """Tautology at box start (ss9): a box member whose condition is true in every state
    reachable when the box first evaluates it gates nothing.

    Only the first evaluation is pinned: a member runs at most once per box execution
    (SEM-10), so a condition that cannot be false then starts it at once. The model follows
    the oracle's catalog-order member starts (DL-14): earlier siblings may be NEVER_RAN or
    RUNNING, later ones are NEVER_RAN. Only box members are examined, because an unpinned
    tautology is vacuous.
    """
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
            # the usual cause: n() on a sibling that cannot have started yet
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


# ------------------------------------------------------------------- graph rules (IR-G)


def rule_l008(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """box_success/box_failure references a non-member (SEM-12): the box can hang RUNNING.

    Fires on derive's M16 box-override edges only, global gates included. It stays off M15
    (early exit) and M09 (a start gate), so it is narrower than the complement of
    `DerivedEdge.is_start_gate` (DL-162, DL-162a). The external-or-member wording reads the
    atom's `instance`, not the shape of `edge.src` (DL-175).
    """
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
    """Unqualified s() feeding a scheduled consumer (SEM-01/R1 stale latch).

    The time trigger can fire on a success from a previous producer run, or block on a left
    failure. Lookback-qualified atoms are exempt.
    """
    out: list[Violation] = []
    for edge in graph.edges:
        if edge.via != "success" or edge.lookback is not None:
            continue
        if not edge.is_start_gate:
            continue  # a box override folds a box; L008 owns the M16 half
        src = local_producer(edge, catalog)
        if src is None:
            continue  # an undefined producer is L001's; a cross-instance one is out of scope (M33)
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
    """Derived-graph cycle (ss5 pass 7): legal, since statuses latch, but a re-trigger pattern."""
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
    """Dangling job: no schedule, no derived wiring in or out, no box membership, not an FW source.

    Wider than `DerivedEdge.is_start_gate` and the one reader that must not use it (DL-162): a
    job gated on a global or named by a `box_success` is wired. Only the src side skips
    globals, which are pseudo-nodes. The src side wires the resolved local producer
    (`local_producer`), so a cross-instance job spelled like a local one cannot shield it
    (DL-175, DL-162a).
    """
    wired: set[str] = set()
    for edge in graph.edges:
        wired.add(edge.dst)
        if edge.via != "global":
            local = local_producer(edge, catalog)
            if local is not None:
                wired.add(local)  # an unresolved src is a pseudo-node or foreign
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
    """n() mutex candidates (M07, R6): not dependencies. Suggests the UC construct per group."""
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
    """Box member with its own date_conditions schedule (SEM-31): schedule and box gate compose
    with AND, so a scheduled member of a non-running box does not fire."""
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
    """UC-side name collision (UCS-12): names equal under case-insensitive comparison.

    Lowering refuses exact duplicates; JIL names are case-sensitive on UNIX (ir-design ss6).
    UC charset and length limits have no documented spec (DL-55): names pass through verbatim
    and an invalid one fails at create time. Extend only if the U3b live pull finds limits.
    """
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
    """ "true", "false" or "unknown" for an atom whose local producer is seeded ON_ICE or ON_NOEXEC.

    Every other producer (live, undefined, cross-instance) is unknown (DL-162a: a
    cross-instance `src` is read off the atom's `instance`). An ON_ICE producer reads the iced
    row (`iced_atom_truth`, DL-243, DL-252). An ON_NOEXEC producer reads its bypass projection
    (SEM-22, DL-281): a bypass ends in SUCCESS with no exit code, so exit-code, failure and
    terminated atoms are false whatever the lookback, and every other atom is true.
    """
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
    """Three-valued evaluation of `cond` under the ice/noexec substitution (`_ice_atom_value`).

    An And is false as soon as one operand is false, and an Or is true as soon as one is true,
    whatever an unknown sibling is (DL-243). So `f(ice,9999) & f(ice) & s(live)` is false, and
    `f(ice) | s(live)` is unknown.

    A GlobalAtom reads true (DL-162): a global gate is not a predecessor and never decides the
    verdict alone, so `s(icy) & v(G)=1` and `f(icy) & v(G)=1` keep their verdicts.
    """
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
    """Iced or noexec consumer (M19/M21), decided from the job's own `condition:` tree.

    The tree is evaluated under the ON_ICE truth table (SEM-20, DL-243) and the ON_NOEXEC
    bypass projection (SEM-22, DL-281). Box-override attrs are never read.

    The Skip-translated statuses are `SKIP_TRANSLATED`: ON_ICE and ON_NOEXEC (DL-152).
    ON_HOLD is M20 Hold, which blocks downstream on both sides, so the rule ignores it.

    Two divergences, read off `_ice_cond_value`:
    (1) The condition is true and every local producer it names is iced or noexec. AutoSys runs
    the consumer, while UC skips a task whose incoming edges are all skipped (UCS-02). One live
    predecessor converges, so the rule needs all of them. An undefined or cross-instance
    reference is unknown and does not count as a producer, but can make the whole condition
    unknown.
    (2) The condition is false: an iced or noexec producer is gated by a failure, terminated or
    exitcode atom (for an iced producer, one without lookback) and no alternative can rescue
    it. AutoSys never starts the consumer, while UC's skip cascade may. The message names each
    producer's seeded status.
    """
    out: list[Violation] = []
    for name, job in catalog.jobs.items():
        if job.sem.initial_status in SKIP_TRANSLATED:
            continue  # skipped on both sides: no divergence
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
            continue  # (2) fired, so (1) cannot also hold
        if value != "true":
            continue
        sources = sorted({local for _atom, local in local_atoms})
        if not all(catalog.jobs[src].sem.initial_status in SKIP_TRANSLATED for src in sources):
            continue  # a live predecessor converges with UC
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
    """Condition-only multi-fire (SEM-01 latch, DL-13 event wake, DL-180).

    An unscheduled, unboxed consumer starts on every event that finds its condition true. With
    two or more wake sources and one unqualified latch, a cycle can start it more than once:
    the s(A) & s(B) double fire, and a bare n(C) guard that every completion of C turns into a
    trigger.

    Exempt: a scheduled consumer (one start per tick, SEM-32; L009 covers staleness) and a box
    member (one start per box execution, SEM-10). Lookback-qualified atoms are not latches
    (SEM-04) but their producers still wake. A condition of bare n() guards alone stays quiet
    (M07, R6): n() is current truth, so a wake source and never a latch, and the cross-instance
    bare form (an edge under M33) reads the same. Global gates are outside the rule (L002's,
    SEM-08): staleness of a v() flag is reset discipline in the setting system.

    Wake sources: is_start_gate edges (an undefined local producer emits no events and is
    L001's; a cross-instance one counts, SEM-07) and the consumer's bare n() targets from
    graph.bare_notrunning. Self counts: n(self) beside a latch re-triggers the job on its own
    completion, a loop L010 cannot see.
    """
    gates = start_gates(graph)
    out: list[Violation] = []
    for name, job in catalog.jobs.items():
        if job.schedule is not None or job.box.box_name is not None:
            continue
        if job.sem.initial_status in SKIP_TRANSLATED:
            continue  # an iced or noexec consumer cannot start (SEM-20)
        wakes: set[str] = set()
        latches: set[str] = set()
        span: SourceSpan | None = None
        for edge in gates.get(name, ()):
            atom = edge.atom
            assert not isinstance(atom, GlobalAtom)  # is_start_gate excludes via=="global"
            # the atom's instance splits local_producer's None (DL-162a): a cross-instance
            # producer stays a wake source here, while L022 drops it
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


#: L022: the via kinds whose start gate means a producer's FAILURE releases something. f(), t(),
#: d() and e() qualify. notrunning does not: a mutex partner freed by the failure is no reaction.
_FAILURE_CONSUMING_VIA: frozenset[str] = frozenset({"failure", "terminated", "done", "exitcode"})


def rule_l022(catalog: CatalogIR, graph: DerivedGraph) -> list[Violation]:
    """Stranded-on-failure consumer (DL-181), info tier.

    When a producer a condition-only, unboxed job cannot do without fails, the job misses at
    least one cycle: the FAILURE latch satisfies nothing it gates on (SEM-01), and release
    waits for the producer's next SUCCESS. The wait is indefinite when the producer has no
    schedule. Alarms cannot exempt: they are observability (DL-32). SEM-14 terminators only
    kill, so they neither exempt nor contradict.

    "Cannot do without" is tier-b: the producer is pinned to FAILURE and `cond_truth_profile`
    asks whether any state satisfies the condition. An OR escape s(a)|s(b) stays quiet, as do
    f()/d()/e()-gated consumers. A condition too large for tier b is skipped.

    "Reads the failure" means a start gate of a live consumer. Box-override edges do not count.
    Reads of an ancestor box count, because a member's failure folds its box FAILURE (SEM-11);
    overrides that defeat the fold are not modelled. Skipped producers: cross-instance jobs
    (DL-162a) and skip-translated ones (L020's). Skipped consumers: scheduled jobs (L019's)
    and box members (a wait on a failed external producer hangs the box, SEM-12; L008's).
    """
    from dsl41.equiv import cond_truth_profile

    # keyed on resolved catalog names, never edge.src, which is "name^INST" for a
    # cross-instance atom (DL-162a); local_producer drops undefined and remote reads
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
            continue  # the consumer never runs (SEM-20)
        candidates: set[str] = set()
        span: SourceSpan | None = None
        for edge in gates.get(name, ()):
            producer = local_producer(edge, catalog)
            if producer is None:
                continue  # undefined local (L001's error) or cross-instance (DL-162a)
            if catalog.jobs[producer].sem.initial_status in SKIP_TRANSLATED:
                continue  # cannot run, cannot fail (L020)
            candidates.add(producer)
            if span is None:
                span = edge.source_atom
        stranding: list[str] = []
        for producer in sorted(candidates):
            profile = cond_truth_profile(attr.cond, fixed_status={producer: "FAILURE"})
            if profile is None:
                # the state ceiling reads the condition alone, so the first answer decides for
                # every producer
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


# ---------------------------------------------------------------------- L023: DST hours


def _reference_today() -> date:
    """The reference date L023 reads when the caller gives none."""
    return date.today()


class _Outcome(NamedTuple):
    """What the runner does with one configured time on a DST change day."""

    kind: Literal["same", "shifted", "first", "second", "none", "twice"]
    text: str


def _clock(moment: datetime) -> str:
    return f"{moment:%H:%M:%S}" if moment.second else f"{moment:%H:%M}"


def _utc_label(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _wall_outcome(naive: datetime, instant: datetime, tz: tzinfo, verb: str) -> _Outcome:
    """One engine instant for the wall time `naive`: unchanged, moved, or a repeated-hour pass."""
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
    """The default (`vendor`) result per start_times entry on `day`; shared instants merge."""
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
    """The deadline of one absolute must time for start_times entry `index` (DL-253), or none."""
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


def _window_end(moment: datetime, day: date, tz: tzinfo) -> str:
    """One end of a window: the local clock, its day against `day`, and the repeated-hour pass."""
    where = (
        "" if moment.date() == day else (" next day" if moment.date() > day else " previous day")
    )
    first = moment.replace(tzinfo=tz, fold=0).utcoffset()
    second = moment.replace(tzinfo=tz, fold=1).utcoffset()
    if first is not None and second is not None and first > second:
        kind, offset = ("second", second) if moment.fold else ("first", first)
        return f"{_clock(moment)}{where}, in the {kind} pass ({_utc_label(offset)})"
    return f"{_clock(moment)}{where}"


def _window_text(verb: str, opens: datetime, closes: datetime, day: date, tz: tzinfo) -> str:
    """One opening of a window in local wall times; one spanning the change day says so."""
    text = f"{verb} {_window_end(opens, day, tz)} and closes at {_window_end(closes, day, tz)}"
    return f"does not close that day: it {text}" if opens.date() < day < closes.date() else text


def _window_outcomes(day: date, lo: time, hi: time, tz: tzinfo) -> list[str]:
    """How run_window `lo`-`hi` differs from its endpoints on `day`.

    Uses the vendor's endpoint rules where `dst_change` names the shape (DL-249), a wall-time
    comparison elsewhere.
    """
    spans = window_spans_near(day, lo, hi, tz)
    texts: list[str] = []
    if spans is not None:
        for opening, (opens, closes) in zip((day - timedelta(days=1), day), spans[1:3]):
            naive_open = datetime.combine(opening, lo)
            naive_close = datetime.combine(opening if lo <= hi else opening + timedelta(days=1), hi)
            opened = _wall_outcome(naive_open, opens, tz, "opens at")
            closed = _wall_outcome(naive_close, closes, tz, "closes at")
            if opened.kind != "same" or closed.kind != "same":
                local_open, local_close = to_local(opens, tz), to_local(closes, tz)
                texts.append(_window_text("opens at", local_open, local_close, day, tz))
        return texts
    week = timedelta(days=7)
    one = timedelta(days=1)
    here = wall_window_intervals(day - one, day + one, lo, hi, tz)

    def wall(moment: datetime) -> tuple[datetime, int]:
        # datetime equality ignores fold; the second pass of a repeated hour must differ
        return moment, moment.fold

    usual = {
        (wall(start + week), wall(end + week))
        for start, end in wall_window_intervals(day - 8 * one, day - 6 * one, lo, hi, tz)
    }
    # only wall times and the number of openings count: a longer or shorter day leaves an
    # overnight window alone
    for number, (start, end) in enumerate(here):
        if (wall(start), wall(end)) in usual:
            continue
        again = any(
            earlier_end >= start or earlier_start.date() == start.date()
            for earlier_start, earlier_end in here[:number]
        )
        verb = "opens a second time at" if again else "opens at"
        texts.append(_window_text(verb, start, end, day, tz))
    if not texts and usual - {(wall(start), wall(end)) for start, end in here}:
        texts.append("never opens")
    return texts


def _start_mins_clause(day: date, minutes: list[int], tz: tzinfo) -> str:
    """The start_mins ticks on `day` that the runner does not run once at their written time."""
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
    """(attribute, written time) -> one clause per change day that moves it."""
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
            texts = _window_outcomes(
                window.day, time(lo.hour, lo.minute), time(hi.hour, hi.minute), tz
            )
            if texts:
                note("run_window", shown, window, ", then ".join(texts), "the window")
    return found


def rule_l023(
    catalog: CatalogIR,
    base_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    today: date | None = None,
) -> list[Violation]:
    """A schedule time that a DST change moves, drops, merges or runs twice (SEM-32, DL-249,
    DL-253, DL-260).

    The zone is the job's `timezone`, else `base_tz` (UTC when None), resolved as
    `dsl41 run --timezone` does; an unresolvable name is skipped. On each clock-change day in
    the reference year (`today`, default the current date) and the next, each time goes
    through the runner's functions (`start_time_instants`, `start_mins_instants`,
    `must_instant`, `window_spans_near`, `wall_window_contains`). A time is flagged when the
    result is not one instant at its written wall time, and the message prints that result.
    Each flagged time is a warn; a `start_mins` job is one info.
    """
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


# --------------------------------------------------------------- the idempotence sidecar


#: Job types with no attempt to interrupt: a box has no process, a file watcher resumes from its
#: own log (DL-424).
NO_ATTEMPT_TYPES: frozenset[str] = frozenset({"BOX", "FW"})


def rule_l024(catalog: CatalogIR, exceptions: Exceptions | None = None) -> list[Violation]:
    """One info per job the exceptions file names and the catalog defines (DL-424)."""
    if exceptions is None:
        return []
    out: list[Violation] = []
    for job in catalog.jobs.values():
        reason = exceptions.reason(job.name)
        if reason is None or job.job_type in NO_ATTEMPT_TYPES:
            continue
        out.append(
            Violation(
                code="L024",
                severity="info",
                message=(
                    f"job {job.name!r} is declared non-idempotent ({reason}): an interrupted"
                    " attempt waits for the operator"
                ),
                jobs=[job.name],
                span=job.span,
                detail=job.name,
            )
        )
    return out


def rule_l025(catalog: CatalogIR, exceptions: Exceptions | None = None) -> list[Violation]:
    """One error per name in the exceptions file that the catalog does not define, or defines
    as a box or a file watcher, which have no attempt to interrupt (DL-424)."""
    if exceptions is None:
        return []
    out: list[Violation] = []
    for name in exceptions.names:
        job = catalog.jobs.get(name)
        if job is None:
            message = f"{EXCEPTIONS_FILE} names {name!r}, which the estate does not define"
        elif job.job_type in NO_ATTEMPT_TYPES:
            message = (
                f"{EXCEPTIONS_FILE}: {name!r} names a box or a file watcher; only command jobs"
                " can be declared"
            )
        else:
            continue
        out.append(Violation(code="L025", severity="error", message=message, detail=name))
    return out


# ------------------------------------------------------------------------------ registry

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
    ("L024", rule_l024),
    ("L025", rule_l025),
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


#: Every registered rule code (ir-design ss9); the CLI validates --suppress against it.
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
    exceptions: Exceptions | None = None,
) -> LintReport:
    """Run every registered rule: IR-F rules first, then graph rules, each in code order.

    The derived graph is computed once unless the caller passes it. `base_tz`, `tz_aliases`
    and `today` reach L023 only: the zone for a schedule without `timezone` (UTC when None),
    the `--timezone-map` table, and the reference date. `exceptions` reaches L024 and L025 only;
    with None both are inert.
    """
    if graph is None:
        graph = derive_graph(catalog)
    violations: list[Violation] = []
    for code, rule in RULES:
        if code == "L023":
            violations.extend(
                rule_l023(catalog, base_tz=base_tz, tz_aliases=tz_aliases, today=today)
            )
        elif code == "L024":
            violations.extend(rule_l024(catalog, exceptions))
        elif code == "L025":
            violations.extend(rule_l025(catalog, exceptions))
        else:
            violations.extend(rule(catalog))
    for _code, graph_rule in GRAPH_RULES:
        violations.extend(graph_rule(catalog, graph))
    return LintReport(violations=violations)
