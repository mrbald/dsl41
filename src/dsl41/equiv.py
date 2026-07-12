"""Equivalence validator: canonical form + tiers a/b/c (ir-design ss6, ss8).

Phase 8 of the implementation order (CLAUDE.md / DL-03).

Tier (a): structural equality of canonical IR-F (pydantic model equality).
Tier (b): per-job condition equivalence by finite-state enumeration, plus a
          canonical derived-graph comparison.
Tier (c): oracle trace comparison on shared event scripts (the honest core:
          two catalogs are equivalent when no script tells them apart).

Decisions pinned here (each with a test; recorded as DL-14 + amendment):
- Tier b enumerates per-job STATE SPACE, not independent atom booleans. The
  ss6 sketch says "truth-table over the atom alphabet", but independent
  atoms cannot detect L006's own flagship contradiction s(x)&f(x). Each
  referenced job scope contributes (status in NEVER_RAN/RUNNING/SUCCESS/
  FAILURE/TERMINATED) x (iced flag -- SEM-05 makes every atom true for an
  iced non-running job, oracle parity) x (age bucket cut by the referenced
  lookback windows) x (same-day flag when zero-lookbacks appear, sharing
  the oracle's Q2 anchor switch) x (last exit code over comparison
  cutpoints, None == never completed); each referenced global contributes
  literal + numeric-cutpoint + string-cutpoint ("", lit+NUL) + UNSET
  domains, covering BOTH comparison behaviors of conditions.compare_value
  (int when both sides parse, else string order). Atoms
  evaluate as functions of the state, so s&f exclusion, d == s|f|t,
  n == not-running, and window nesting hold by construction.
- The state model is deliberately DECOUPLED where AutoSys couples weakly:
  status and last exit code vary independently (TERMINATED keeps an old
  code; SEM-09 boundaries would couple SUCCESS <-> code <= max_exit_success
  only for completion-by-code). Extra unreachable states can only produce
  false INEQUIVALENCE (a human looks) or a missed L006/L007 warn -- never a
  false claim of equivalence. Conservative direction, documented.
- Tier b compares CONDITIONS + the derived graph only (ss6 scope);
  schedules are tier a's to compare, and the calendar-free oracle makes
  tier c schedule-blind too -- run tier a for schedule differences.
- Ceiling: state spaces beyond 2^18 report "too_large" (tier-c only). The
  ss6 BDD fallback (`dd`) is deliberately not taken v1: no new dependency
  for a path the corpus has never needed (DL-14).
- Canonicalization drops: spans, Lookback.raw (kind+minutes stay), Paren,
  annotations (softer tier per ss6), var_sites (regenerable index), meta.
  It normalizes: nested same-op flattening + operand sort by structural
  key, schedule list sort+dedup, duplicate-operand dedup. passthrough is
  KEPT verbatim: it is the semantic firewall's cargo, not decoration.
- Rename maps apply to job names, box_name links, and job refs inside all
  three condition attributes; globals and external instances are identity
  v1 (rename them by hand if an estate renames globals -- documented).
- case_fold lowercases job names AND job refs for comparison (ir-design
  ss6 escape hatch); collisions after folding raise loudly.
- Tier c compares (at, job, transition) sequences with the rename applied
  to the left trace; `cause` strings are excluded (they embed names and
  wording, not semantics). Scripts come from the caller; equiv_scripts()
  offers a deterministic seeded generator so CLI runs are reproducible.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from typing import Literal

from pydantic import BaseModel

from dsl41.conditions import (
    And,
    Cond,
    ExitCodeAtom,
    GlobalAtom,
    Lookback,
    Or,
    Paren,
    StatusAtom,
    compare_int,
    compare_value,
    iter_atoms,
)
from dsl41.derive import derive_graph
from dsl41.ir import CatalogIR, JobIR, MachineIR, Time
from dsl41.oracle import Event, Oracle, TraceEntry

STATE_CEILING = 2**18

# ------------------------------------------------------------- canonical form (ss6)


def canonical_cond(cond: Cond) -> Cond:
    """C() for conditions: erase Paren, flatten same-op nests, dedup + sort
    operands by structural key, drop spans, normalize lookback (raw dropped)."""
    return _canon(cond)


def _canon(cond: Cond) -> Cond:
    if isinstance(cond, Paren):
        return _canon(cond.inner)
    if isinstance(cond, (And, Or)):
        op_type = type(cond)
        flat: list[Cond] = []
        for operand in cond.operands:
            canned = _canon(operand)
            if isinstance(canned, op_type):
                flat.extend(canned.operands)  # And(And(..)) flattening
            else:
                flat.append(canned)
        deduped: list[Cond] = []
        seen: set[str] = set()
        for operand in sorted(flat, key=_structural_key):
            key = _structural_key(operand)
            if key not in seen:
                seen.add(key)
                deduped.append(operand)
        if len(deduped) == 1:
            return deduped[0]
        return op_type(operands=deduped, span=None)
    if isinstance(cond, StatusAtom):
        return StatusAtom(
            job=cond.job, status=cond.status, lookback=_canon_lookback(cond.lookback), span=None
        )
    if isinstance(cond, ExitCodeAtom):
        return ExitCodeAtom(
            job=cond.job,
            op=cond.op,
            value=cond.value,
            lookback=_canon_lookback(cond.lookback),
            span=None,
        )
    return GlobalAtom(name=cond.name, op=cond.op, value=cond.value, span=None)


def _canon_lookback(lookback: Lookback | None) -> Lookback | None:
    if lookback is None or lookback.kind == "indefinite":
        return None  # explicit 9999 == no qualifier (SEM-04)
    return Lookback(kind=lookback.kind, minutes=lookback.minutes, raw="")


def _structural_key(cond: Cond) -> str:
    return json.dumps(cond.model_dump(exclude={"span"}, mode="json"), sort_keys=True)


class RenameError(ValueError):
    pass


def _apply_rename(name: str, rename: dict[str, str]) -> str:
    return rename.get(name, name)


def _fold(name: str, case_fold: bool) -> str:
    return name.lower() if case_fold else name


def _map_cond(cond: Cond, rename: dict[str, str], case_fold: bool) -> Cond:
    if isinstance(cond, Paren):
        return Paren(inner=_map_cond(cond.inner, rename, case_fold), span=cond.span)
    if isinstance(cond, (And, Or)):
        mapped = [_map_cond(op, rename, case_fold) for op in cond.operands]
        return type(cond)(operands=mapped, span=cond.span)
    if isinstance(cond, GlobalAtom):
        return cond  # globals are identity under rename v1 (module docstring)
    job = cond.job
    if job.instance is None:  # cross-instance refs are identity too
        job = job.model_copy(update={"name": _fold(_apply_rename(job.name, rename), case_fold)})
    return cond.model_copy(update={"job": job})


def canonical_catalog(
    catalog: CatalogIR,
    *,
    rename: dict[str, str] | None = None,
    case_fold: bool = False,
) -> CatalogIR:
    """C() for catalogs: rename bijection applied first, then per-job
    canonicalization (module docstring lists exactly what is dropped)."""
    rename = rename or {}
    jobs: dict[str, JobIR] = {}
    for name, job in catalog.jobs.items():
        new_name = _fold(_apply_rename(name, rename), case_fold)
        if new_name in jobs:
            raise RenameError(f"job name collision after rename/fold: {new_name!r}")
        sem = job.sem.model_copy(
            update={
                "condition": _canon(_map_cond(job.sem.condition, rename, case_fold))
                if job.sem.condition is not None
                else None,
                "box_success": _canon(_map_cond(job.sem.box_success, rename, case_fold))
                if job.sem.box_success is not None
                else None,
                "box_failure": _canon(_map_cond(job.sem.box_failure, rename, case_fold))
                if job.sem.box_failure is not None
                else None,
                "condition_span": None,
                "box_success_span": None,
                "box_failure_span": None,
            }
        )
        schedule = job.schedule
        if schedule is not None:
            start_times = None
            if schedule.start_times is not None:
                unique = sorted({(t.hour, t.minute) for t in schedule.start_times})
                start_times = [Time(hour=h, minute=m) for h, m in unique]
            schedule = schedule.model_copy(
                update={
                    "start_times": start_times,
                    "start_mins": sorted(set(schedule.start_mins))
                    if schedule.start_mins is not None
                    else None,
                    "days_of_week": sorted(set(schedule.days_of_week))
                    if schedule.days_of_week is not None
                    else None,
                }
            )
        box = job.box.model_copy(
            update={
                "box_name": _fold(_apply_rename(job.box.box_name, rename), case_fold)
                if job.box.box_name is not None
                else None
            }
        )
        jobs[new_name] = job.model_copy(
            update={
                "name": new_name,
                "box": box,
                "schedule": schedule,
                "sem": sem,
                "annotations": {},  # softer tier (ss6)
                "var_sites": [],  # regenerable index
                "span": None,
            }
        )
    machines = {
        name: machine.model_copy(update={"span": None})
        for name, machine in catalog.machines.items()
    }
    resources = {
        name: resource.model_copy(update={"span": None})
        for name, resource in catalog.resources.items()
    }
    external_instances = {
        name: xinst.model_copy(update={"span": None})
        for name, xinst in catalog.external_instances.items()
    }
    calendars = {
        name: calendar.model_copy(update={"span": None})
        for name, calendar in catalog.calendars.items()
    }
    cycles = {
        name: cycle.model_copy(update={"span": None}) for name, cycle in catalog.cycles.items()
    }
    canonical = catalog.model_copy(
        update={
            "jobs": jobs,
            "machines": machines,
            "resources": resources,
            "calendars": calendars,
            "cycles": cycles,
            "external_instances": external_instances,
            "meta": type(catalog.meta)(),
        }
    )
    return canonical


def catalog_hash(catalog: CatalogIR) -> str:
    """sha256 over canonical IR-F JSON (ir-design ss8): the short-circuit
    identity for the equivalence CLI and the migration report's pin."""
    canonical = canonical_catalog(catalog)
    payload = json.dumps(canonical.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TierAResult(BaseModel):
    equivalent: bool
    left_only: list[str] = []  # job names present only in A
    right_only: list[str] = []
    differing: list[str] = []  # common names whose canonical JobIR differs
    detail: dict[str, str] = {}  # job -> one-line what-differs summary


def equivalent_tier_a(
    a: CatalogIR,
    b: CatalogIR,
    *,
    rename: dict[str, str] | None = None,
    case_fold: bool = False,
) -> TierAResult:
    """Tier (a): C(A) == C(B) structural equality, with per-job diffing."""
    ca = canonical_catalog(a, rename=rename, case_fold=case_fold)
    cb = canonical_catalog(b, case_fold=case_fold)
    left_only = sorted(set(ca.jobs) - set(cb.jobs))
    right_only = sorted(set(cb.jobs) - set(ca.jobs))
    differing: list[str] = []
    detail: dict[str, str] = {}
    for name in sorted(set(ca.jobs) & set(cb.jobs)):
        ja, jb = ca.jobs[name], cb.jobs[name]
        if ja != jb:
            differing.append(name)
            fields = [
                field
                for field in ja.model_fields_set | jb.model_fields_set
                if getattr(ja, field, None) != getattr(jb, field, None)
            ]
            detail[name] = "differs in: " + ", ".join(sorted(fields))
    globals_equal = a.globals_declared == b.globals_declared
    if not globals_equal:
        detail["<globals>"] = "globals_declared differ"
    def _machine_key(v: MachineIR) -> tuple:
        # members are ordered (DL-49); compare order-sensitively -- a reorder
        # or a dropped component is a real difference, never false-equal.
        return (v.machine_type, v.attrs, [(mm.name, mm.attrs) for mm in v.members])

    machines_equal = {m: _machine_key(v) for m, v in a.machines.items()} == {
        m: _machine_key(v) for m, v in b.machines.items()
    }
    if not machines_equal:
        detail["<machines>"] = "machine definitions differ"
    # Calendar/cycle definitions drive run-day semantics (M24): the same
    # run_calendar name over different dates is a different schedule (DL-36).
    calendars_equal = {c: (v.kind, v.attrs, v.dates) for c, v in a.calendars.items()} == {
        c: (v.kind, v.attrs, v.dates) for c, v in b.calendars.items()
    }
    if not calendars_equal:
        detail["<calendars>"] = "calendar definitions differ"
    cycles_equal = {c: v.attrs for c, v in a.cycles.items()} == {
        c: v.attrs for c, v in b.cycles.items()
    }
    if not cycles_equal:
        detail["<cycles>"] = "cycle definitions differ"
    # Resources and external instances joined tier a with DL-37a: catalog_hash
    # already covered them, so a difference used to be a hash mismatch with no
    # tier-a detail -- the ss8 short-circuit and tier (a) disagreed.
    resources_equal = {r: (v.res_type, v.attrs) for r, v in a.resources.items()} == {
        r: (v.res_type, v.attrs) for r, v in b.resources.items()
    }
    if not resources_equal:
        detail["<resources>"] = "resource definitions differ"
    xinsts_equal = {x: (v.xtype, v.attrs) for x, v in a.external_instances.items()} == {
        x: (v.xtype, v.attrs) for x, v in b.external_instances.items()
    }
    if not xinsts_equal:
        detail["<external_instances>"] = "external-instance definitions differ"
    equivalent = (
        not left_only
        and not right_only
        and not differing
        and globals_equal
        and machines_equal
        and calendars_equal
        and cycles_equal
        and resources_equal
        and xinsts_equal
    )
    return TierAResult(
        equivalent=equivalent,
        left_only=left_only,
        right_only=right_only,
        differing=differing,
        detail=detail,
    )


# ----------------------------------------------------- tier b: condition equivalence

_STATUSES = ("NEVER_RAN", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")

_UNSET = "<unset>"


class _JobScope(BaseModel):
    """State dimensions contributed by one referenced job (module docstring)."""

    key: str  # job name or name^INST
    windows: list[int] = []  # sorted distinct window minutes
    has_zero: bool = False  # a zero-lookback atom references this job (Q2)
    exit_cutpoints: list[int] = []  # candidate last-exit-code values
    has_exit: bool = False


class _Alphabet(BaseModel):
    jobs: dict[str, _JobScope] = {}
    globals_: dict[str, list[str]] = {}  # name -> candidate values (incl UNSET/OTHER)


def _job_key(atom: StatusAtom | ExitCodeAtom) -> str:
    if atom.job.instance is None:
        return atom.job.name
    return f"{atom.job.name}^{atom.job.instance}"


def _alphabet(conds: list[Cond]) -> _Alphabet:
    jobs: dict[str, _JobScope] = {}
    global_values: dict[str, set[str]] = {}
    global_numeric: dict[str, bool] = {}
    for cond in conds:
        for atom in iter_atoms(cond):
            if isinstance(atom, GlobalAtom):
                values = global_values.setdefault(atom.name, set())
                values.add(atom.value)
                is_num = atom.value.lstrip("-").isdigit()
                global_numeric[atom.name] = global_numeric.get(atom.name, True) and is_num
                continue
            scope = jobs.setdefault(_job_key(atom), _JobScope(key=_job_key(atom)))
            lookback = atom.lookback
            if lookback is not None and lookback.kind == "window":
                assert lookback.minutes is not None
                if lookback.minutes not in scope.windows:
                    scope.windows.append(lookback.minutes)
            elif lookback is not None and lookback.kind == "zero":
                scope.has_zero = True
            if isinstance(atom, ExitCodeAtom):
                scope.has_exit = True
                for candidate in (atom.value - 1, atom.value, atom.value + 1):
                    if candidate not in scope.exit_cutpoints:
                        scope.exit_cutpoints.append(candidate)
    for scope in jobs.values():
        scope.windows.sort()
        scope.exit_cutpoints.sort()
    globals_: dict[str, list[str]] = {}
    for name, values in global_values.items():
        # Region representatives for BOTH comparison behaviors
        # conditions.compare_value exhibits (int when both sides parse, else
        # string order): numeric cutpoints v-1/v/v+1 for int-able literals, string
        # cutpoints ""/lit/lit+"\\x00" for every literal ("" sits below all
        # nonempty strings; lit+"\\x00" sits strictly between lit and any
        # greater string -- JIL text cannot carry NUL, so no literal
        # collides). The old single OTHER token satisfied only "!=" and
        # made every string ordering comparison vacuously false (review
        # BLOCKER, DL-14 amendment).
        points: set[str] = set(values)
        points.add("")
        for literal in values:
            points.add(literal + "\x00")
            if literal.lstrip("-").isdigit():
                value = int(literal)
                points.update(str(p) for p in (value - 1, value + 1))
        globals_[name] = [*sorted(points), _UNSET]
    return _Alphabet(jobs=jobs, globals_=globals_)


class _State(BaseModel):
    """One point of the enumeration: per-job (status, iced flag, age bucket
    index, same-day flag, last exit code) + global values."""

    job_status: dict[str, str]
    job_iced: dict[str, bool]  # SEM-05/SEM-20: iced satisfies every atom
    job_age_bucket: dict[str, int]  # index into windows; len(windows) == beyond all
    job_same_day: dict[str, bool]
    job_exit: dict[str, int | None]
    globals_: dict[str, str]


def _state_count(alphabet: _Alphabet) -> int:
    total = 1
    for scope in alphabet.jobs.values():
        per_status = len(_STATUSES) * 2  # x2: iced flag (SEM-05/SEM-20)
        per_age = len(scope.windows) + 1 if scope.windows else 1
        per_day = 2 if scope.has_zero else 1
        per_exit = len(scope.exit_cutpoints) + 1 if scope.has_exit else 1  # +1: None
        total *= per_status * per_age * per_day * per_exit
        if total > STATE_CEILING:
            return total
    for domain in alphabet.globals_.values():
        total *= len(domain)
        if total > STATE_CEILING:
            return total
    return total


def _iter_states(alphabet: _Alphabet):
    job_axes: list[list[tuple[str, str, bool, int, bool, int | None]]] = []
    for scope in alphabet.jobs.values():
        axis: list[tuple[str, str, bool, int, bool, int | None]] = []
        ages = range(len(scope.windows) + 1) if scope.windows else [0]
        days = (True, False) if scope.has_zero else (True,)
        exits: list[int | None] = [None, *scope.exit_cutpoints] if scope.has_exit else [None]
        for status in _STATUSES:
            for iced in (False, True):
                for age in ages:
                    for day in days:
                        for code in exits:
                            axis.append((scope.key, status, iced, age, day, code))
        job_axes.append(axis)
    global_axes = [
        [(name, value) for value in domain] for name, domain in alphabet.globals_.items()
    ]
    for job_choice in itertools.product(*job_axes):
        for global_choice in itertools.product(*global_axes):
            yield _State(
                job_status={key: status for key, status, _, _, _, _ in job_choice},
                job_iced={key: iced for key, _, iced, _, _, _ in job_choice},
                job_age_bucket={key: age for key, _, _, age, _, _ in job_choice},
                job_same_day={key: day for key, _, _, _, day, _ in job_choice},
                job_exit={key: code for key, _, _, _, _, code in job_choice},
                globals_={name: value for name, value in global_choice},
            )


def _eval_cond(cond: Cond, state: _State, alphabet: _Alphabet) -> bool:
    if isinstance(cond, And):
        return all(_eval_cond(op, state, alphabet) for op in cond.operands)
    if isinstance(cond, Or):
        return any(_eval_cond(op, state, alphabet) for op in cond.operands)
    if isinstance(cond, Paren):
        return _eval_cond(cond.inner, state, alphabet)
    if isinstance(cond, GlobalAtom):
        actual = state.globals_.get(cond.name, _UNSET)
        if actual == _UNSET:
            return False
        return compare_value(actual, cond.op, cond.value)
    key = _job_key(cond)
    status = state.job_status.get(key, "NEVER_RAN")
    if state.job_iced.get(key, False) and status != "RUNNING":
        # SEM-05/SEM-20 oracle parity: an iced job satisfies EVERY atom kind,
        # lookback ignored; ice on a running job takes effect at completion.
        return True
    if isinstance(cond, ExitCodeAtom):
        code = state.job_exit.get(key)
        if code is None or not _lookback_holds(cond.lookback, key, state, alphabet):
            return False
        return compare_int(code, cond.op, cond.value)
    wanted = cond.status
    if wanted == "NOTRUNNING":
        hit = status != "RUNNING"
        if status == "NEVER_RAN":
            return hit  # no timestamp: lookback trivially holds (oracle parity)
    elif wanted == "DONE":
        hit = status in ("SUCCESS", "FAILURE", "TERMINATED")
    else:
        hit = status == wanted
    return hit and _lookback_holds(cond.lookback, key, state, alphabet)


def _lookback_holds(
    lookback: Lookback | None, key: str, state: _State, alphabet: _Alphabet
) -> bool:
    if lookback is None or lookback.kind == "indefinite":
        return True
    if lookback.kind == "zero":
        # PENDING: Q2 -- shares the oracle's anchor switch so tier b and
        # tier c never disagree on zero-lookback semantics
        from dsl41 import oracle as oracle_module

        if oracle_module.ORACLE_ZERO_LOOKBACK_ANCHOR == "midnight":
            return state.job_same_day.get(key, True)
        return True  # "last_change": the latched status itself qualifies
    assert lookback.minutes is not None
    scope = alphabet.jobs[key]
    bucket = state.job_age_bucket.get(key, 0)
    # bucket i means age in (w_{i-1}, w_i]; window w holds iff age <= w
    return bucket <= scope.windows.index(lookback.minutes)


TierBVerdict = Literal["equivalent", "divergent", "too_large"]


class TierBResult(BaseModel):
    verdict: TierBVerdict
    state_count: int
    counterexample: dict[str, str] | None = None  # human-readable state assignment


def _describe(state: _State) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, status in state.job_status.items():
        bits = [status]
        if state.job_iced.get(key, False):
            bits.append("ON_ICE")
        if state.job_age_bucket.get(key):
            bits.append(f"age_bucket={state.job_age_bucket[key]}")
        if not state.job_same_day.get(key, True):
            bits.append("different_day")
        if state.job_exit.get(key) is not None:
            bits.append(f"exit={state.job_exit[key]}")
        out[key] = ",".join(bits)
    for name, value in state.globals_.items():
        out[f"${name}"] = value
    return out


def conds_equivalent(a: Cond | None, b: Cond | None) -> TierBResult:
    """Tier (b) per-condition equivalence over the shared state space. A
    None condition is the constant TRUE (no gate)."""
    conds = [c for c in (a, b) if c is not None]
    alphabet = _alphabet(conds)
    count = _state_count(alphabet)
    if count > STATE_CEILING:
        return TierBResult(verdict="too_large", state_count=count)
    for state in _iter_states(alphabet):
        va = True if a is None else _eval_cond(a, state, alphabet)
        vb = True if b is None else _eval_cond(b, state, alphabet)
        if va != vb:
            return TierBResult(
                verdict="divergent", state_count=count, counterexample=_describe(state)
            )
    return TierBResult(verdict="equivalent", state_count=count)


def cond_truth_profile(
    cond: Cond,
    *,
    fixed_status: dict[str, set[str] | str] | None = None,
    include_ice: bool = False,
) -> tuple[bool, bool] | None:
    """(satisfiable, falsifiable) over the state space; None == too large.
    The L006/L007 engine: contradiction == not satisfiable; tautology ==
    not falsifiable. `fixed_status` restricts job scopes to a status (or a
    set of allowed statuses; their age/day/exit axes still vary) -- L007
    uses it to model the box-start moment. `include_ice` defaults to False:
    the lint rules ask the ice-free question ("can this ever fire without
    operator intervention?"), while conds_equivalent always enumerates ice
    states for soundness (DL-14 amendment). In the ice-free model every
    condition is falsifiable (the all-RUNNING/unset-globals state), so
    unpinned tautology is vacuous by construction."""
    fixed_status = fixed_status or {}
    alphabet = _alphabet([cond])
    if _state_count(alphabet) > STATE_CEILING:
        return None
    satisfiable = False
    falsifiable = False
    for state in _iter_states(alphabet):
        if not include_ice and any(state.job_iced.values()):
            continue
        skip = False
        for key, allowed in fixed_status.items():
            if key not in state.job_status:
                # DL-26: a pin on a job the condition never references is
                # vacuous -- it cannot affect this condition's truth. The old
                # `.get(key) not in allowed` treated it as unsatisfiable and
                # skipped EVERY state, turning any box member whose condition
                # ignores at least one sibling into a false L007 tautology
                # (e.g. every vanilla s(prev) chain in a 3+-member box).
                continue
            allowed_set = {allowed} if isinstance(allowed, str) else allowed
            if state.job_status[key] not in allowed_set:
                skip = True
                break
        if skip:
            continue
        if _eval_cond(cond, state, alphabet):
            satisfiable = True
        else:
            falsifiable = True
        if satisfiable and falsifiable:
            break
    return satisfiable, falsifiable


class TierBCatalogResult(BaseModel):
    equivalent: bool  # no divergence found; too_large conditions are DEFERRED
    divergent_jobs: dict[str, str] = {}  # job -> attr + counterexample summary
    too_large_jobs: list[str] = []  # inconclusive -- "tier-c only" (ss6), not divergent
    graph_equal: bool = True
    graph_detail: str | None = None


def equivalent_tier_b(
    a: CatalogIR,
    b: CatalogIR,
    *,
    rename: dict[str, str] | None = None,
    case_fold: bool = False,
) -> TierBCatalogResult:
    """Tier (b) catalog-level: per-common-job condition equivalence on all
    three condition attributes, plus canonical derived-graph comparison
    (edge multisets, mutex groups, box tree -- the v1 stand-in for the ss6
    bisimulation check, DL-14)."""
    ca = canonical_catalog(a, rename=rename, case_fold=case_fold)
    cb = canonical_catalog(b, case_fold=case_fold)
    divergent: dict[str, str] = {}
    too_large: list[str] = []
    for name in sorted(set(ca.jobs) & set(cb.jobs)):
        for attr in ("condition", "box_success", "box_failure"):
            left: Cond | None = getattr(ca.jobs[name].sem, attr)
            right: Cond | None = getattr(cb.jobs[name].sem, attr)
            if left is None and right is None:
                continue
            result = conds_equivalent(left, right)
            if result.verdict == "too_large":
                too_large.append(f"{name}.{attr}")
            elif result.verdict == "divergent":
                divergent[name] = f"{attr} diverges at state {result.counterexample}"
                break
    ga, gb = derive_graph(ca), derive_graph(cb)
    edges_a = sorted(
        (e.src, e.dst, e.via, e.mapping_row, e.cls, _lb_key(e.lookback)) for e in ga.edges
    )
    edges_b = sorted(
        (e.src, e.dst, e.via, e.mapping_row, e.cls, _lb_key(e.lookback)) for e in gb.edges
    )
    graph_equal = (
        edges_a == edges_b and ga.mutex_groups == gb.mutex_groups and ga.box_tree == gb.box_tree
    )
    graph_detail = None
    if not graph_equal:
        only_a = [e for e in edges_a if e not in edges_b]
        only_b = [e for e in edges_b if e not in edges_a]
        graph_detail = f"edges only in A: {only_a}; only in B: {only_b}"
        if ga.mutex_groups != gb.mutex_groups:
            graph_detail += f"; mutex A={ga.mutex_groups} B={gb.mutex_groups}"
        if ga.box_tree != gb.box_tree:
            graph_detail += "; box trees differ"
    return TierBCatalogResult(
        # too_large is inconclusive ("tier-c only", ss6/DL-14), not a
        # divergence -- it is reported but does not fail the tier
        equivalent=not divergent and graph_equal,
        divergent_jobs=divergent,
        too_large_jobs=too_large,
        graph_equal=graph_equal,
        graph_detail=graph_detail,
    )


def _lb_key(lookback: Lookback | None) -> str:
    if lookback is None or lookback.kind == "indefinite":
        return "-"
    if lookback.kind == "zero":
        return "0"
    return str(lookback.minutes)


# ------------------------------------------------------ tier c: oracle trace compare


class TierCResult(BaseModel):
    equivalent: bool
    scripts_run: int
    first_divergence: str | None = None  # human-readable description


def _trace_key(
    trace: list[TraceEntry], rename: dict[str, str], case_fold: bool
) -> list[tuple[str, str, str]]:
    return [
        (
            entry.at.isoformat(),
            _fold(_apply_rename(entry.job, rename), case_fold),
            entry.transition,
        )
        for entry in trace
    ]


def equivalent_tier_c(
    a: CatalogIR,
    b: CatalogIR,
    scripts: list[list[Event]],
    *,
    rename: dict[str, str] | None = None,
    case_fold: bool = False,
) -> TierCResult:
    """Tier (c): run both oracles on each script; equivalent iff every trace
    matches on (at, renamed job, transition). Script events target A's job
    names; the rename maps them for B."""
    rename = rename or {}
    mapped_names = [_fold(_apply_rename(name, rename), case_fold) for name in a.jobs]
    if len(set(mapped_names)) != len(mapped_names):
        raise RenameError("job name collision after rename/fold (tier c)")
    for index, script in enumerate(scripts):
        oracle_a = Oracle(a)
        oracle_b = Oracle(b)
        mapped_script = [
            ev.model_copy(
                update={
                    "payload": {
                        **ev.payload,
                        **(
                            {"job": _fold(_apply_rename(str(ev.payload["job"]), rename), case_fold)}
                            if "job" in ev.payload
                            else {}
                        ),
                    }
                }
            )
            for ev in script
        ]
        trace_a = _trace_key(oracle_a.run_script(script), rename, case_fold)
        trace_b = _trace_key(oracle_b.run_script(mapped_script), {}, case_fold)
        if trace_a != trace_b:
            spot = next(
                (i for i, (ea, eb) in enumerate(zip(trace_a, trace_b)) if ea != eb),
                min(len(trace_a), len(trace_b)),
            )
            left = trace_a[spot] if spot < len(trace_a) else "<trace ended>"
            right = trace_b[spot] if spot < len(trace_b) else "<trace ended>"
            return TierCResult(
                equivalent=False,
                scripts_run=index + 1,
                first_divergence=f"script {index}, entry {spot}: A={left} B={right}",
            )
    return TierCResult(equivalent=True, scripts_run=len(scripts))


def equiv_scripts(
    catalog: CatalogIR, *, scripts: int = 20, events_per_script: int = 12, seed: int = 41
) -> list[list[Event]]:
    """Deterministic seeded script generator for tier (c). Coverage follows
    the review findings (DL-14 amendment): every out-of-band kind the
    oracle distinguishes (ICE/HOLD/NOEXEC/KILLJOB/FORCE) appears with small
    probability; SET_GLOBAL targets declared globals AND globals referenced
    only in conditions (runtime-set globals are routine, SEM-08/L002), with
    values drawn from the compared literals plus off-literal probes."""
    from datetime import datetime, timedelta

    rng = random.Random(seed)
    jobs = list(catalog.jobs)
    global_names: set[str] = set(catalog.globals_declared)
    global_values: set[str] = {"1", "0", "go", ""}
    for job_ir in catalog.jobs.values():
        for cond in (job_ir.sem.condition, job_ir.sem.box_success, job_ir.sem.box_failure):
            if cond is None:
                continue
            for atom in iter_atoms(cond):
                if isinstance(atom, GlobalAtom):
                    global_names.add(atom.name)
                    global_values.add(atom.value)
                    global_values.add(atom.value + "x")  # off-literal probe
    globals_ = sorted(global_names)
    values = sorted(global_values)
    oob_kinds = ["ON_ICE", "OFF_ICE", "ON_HOLD", "OFF_HOLD", "ON_NOEXEC", "KILLJOB"]
    base = datetime(2026, 1, 1, 8, 0)
    out: list[list[Event]] = []
    for _ in range(scripts):
        minute = 0
        script: list[Event] = []
        for _ in range(events_per_script):
            minute += rng.randrange(0, 90)
            at = base + timedelta(minutes=minute)
            roll = rng.random()
            if roll < 0.5 or not globals_:
                job = rng.choice(jobs)
                sub = rng.random()
                if sub < 0.2:
                    script.append(Event(at=at, kind="STARTJOB", payload={"job": job}))
                elif sub < 0.3:
                    script.append(Event(at=at, kind="FORCE_STARTJOB", payload={"job": job}))
                else:
                    status = rng.choice(["SUCCESS", "SUCCESS", "FAILURE"])
                    script.append(
                        Event(at=at, kind="STATUS", payload={"job": job, "status": status})
                    )
            elif roll < 0.7:
                kind = rng.choice(oob_kinds)
                script.append(
                    Event(at=at, kind=kind, payload={"job": rng.choice(jobs)})  # type: ignore[arg-type]
                )
            else:
                script.append(
                    Event(
                        at=at,
                        kind="SET_GLOBAL",
                        payload={"name": rng.choice(globals_), "value": rng.choice(values)},
                    )
                )
        out.append(script)
    return out


__all__ = [
    "STATE_CEILING",
    "RenameError",
    "TierAResult",
    "TierBCatalogResult",
    "TierBResult",
    "TierCResult",
    "canonical_catalog",
    "canonical_cond",
    "catalog_hash",
    "cond_truth_profile",
    "conds_equivalent",
    "equiv_scripts",
    "equivalent_tier_a",
    "equivalent_tier_b",
    "equivalent_tier_c",
]
