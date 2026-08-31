"""`rehearse --check-cadence`: the DL-182 dynamic estate check.

Drive the estate through the rehearse engine, compare observed per-job run
counts against a typed expectation, and report deviations. The rulings are
DL-184's; the stance that decides every corner here: the expectation is a
POLICY BOUND -- the DL-182 default, "at most once per own cadence" -- not a
soundness proof. An estate that legitimately exceeds it (the unqualified OR
join firing once per wake, the DL-180 shape) is the check's intended true
positive; `--cadence-policy` is the declared-exception channel. Deviations
are triage findings.

Two counters, deliberately different:
- EXPECTED comes from a fresh Scheduler anchored at the same start with the
  same construction arguments the engine fires from, so expectation and
  observation cannot drift on calendar arithmetic.
- OBSERVED is a run_number delta read from the oracle store, never a count
  of STARTING trace transitions: ON_NOEXEC bypasses STARTING yet consumes
  the tick (Q3/DL-54), and injected events could forge trace lines.

`unchecked` is ABSORBING, but only along paths the bound actually reads: a
job whose bound cannot be honestly computed (global-gated, cross-instance
wake, parked file watcher, wake-cycle member, policy null) infects its box
members and its wake-DEPENDENT consumers -- condition-only unboxed jobs,
the only ones whose bound reads wakes -- each with the reason chain's first
link named. Scheduled jobs and box members stay checked whatever their
wake sources do: an armed tick and the once-per-box-execution gate bound
them regardless (SEM-30/31, SEM-10). Exit-0 claims are scoped to checked
jobs and the sweeps actually run (the report says which).
"""

from __future__ import annotations

import asyncio
import json

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.conditions import (
    And,
    ExitCodeAtom,
    GlobalAtom,
    Or,
    Paren,
    StatusAtom,
    compare_value,
    iter_atoms,
)
from dsl41.derive import DerivedGraph, cycles, local_producer, start_gates
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock, ZeroDelayCycleError
from dsl41.runner_scheduler import Scheduler

#: The check-mode scenario event allowlist (DL-184): injected starts carry a
#: +1 budget on their target; a scripted STATUS forges the counts under test.
CHECK_EVENT_KINDS = frozenset({"STARTJOB", "FORCE_STARTJOB", "SET_GLOBAL"})

#: Report footer (the backend_uc assumptions precedent): what the oracle's
#: model -- and therefore this check -- does not see (DL-53).
UNMODELED_NOTE = (
    "counts assume no retries: n_retrys and the other DL-53 unmodeled"
    " attributes are outside the oracle's model"
)


class CadenceCheckError(ValueError):
    """A check-mode refusal (policy file, scenario allowlist); the CLI owns
    the exit code."""


# ------------------------------------------------------------------ policy


class JobPolicy(BaseModel):
    """One declared exception. `max_runs` null means unchecked; the reason
    is mandatory and prints in the report (the L021-qualifier precedent:
    exceptions are documented, never silent)."""

    model_config = ConfigDict(extra="forbid")

    max_runs: int | None = Field(ge=0)
    reason: str = Field(min_length=1)


class CadencePolicy(BaseModel):
    """The `--cadence-policy` file (DL-184). Strict on unknown keys; unknown
    job names refuse in `load_policy` (no silent loss)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    policies: dict[str, JobPolicy] = {}


def load_policy(path: Path, catalog: CatalogIR) -> CadencePolicy:
    """Read and validate a policy file against the catalog, or refuse."""
    try:
        data = json.loads(path.read_bytes())
    except OSError as exc:
        raise CadenceCheckError(str(exc)) from exc
    except ValueError as exc:
        raise CadenceCheckError(f"not JSON: {exc}") from exc
    try:
        policy = CadencePolicy.model_validate(data)
    except ValidationError as exc:
        raise CadenceCheckError(str(exc)) from exc
    unknown = sorted(set(policy.policies) - set(catalog.jobs))
    if unknown:
        raise CadenceCheckError(
            f"policies name jobs the catalog does not define: {', '.join(unknown)}"
        )
    return policy


# ------------------------------------------------------------------ report


class Bound(BaseModel):
    """One job's typed expectation: policy(N) with its provenance named, or
    unchecked (`expected is None`) with the reason (DL-184)."""

    expected: int | None
    provenance: str


class CheckFinding(BaseModel):
    kind: Literal["multi_fire", "zero_delay_cycle"]
    jobs: list[str]
    detail: str
    #: which play raised it: None = the happy path, "fail:<producer>" = that
    #: fail-sweep case (additive within schema_version 1)
    case: str | None = None


class JobCheck(Bound):
    """One job's row: its Bound plus what the play observed."""

    observed: int
    deviation: bool


class SweepCase(BaseModel):
    """One fail-sweep case: the producer's first run scripted to FAILURE,
    and what changed against the happy-path baseline. `suppressed` is the
    dynamic L022 inventory -- info tier, it never exits 3 by itself;
    `deviations`/`cycle` findings land in the report's findings and do."""

    producer: str
    outcome: Literal[
        "ran",
        "skipped_box",  # no adapter runs a BOX (DL-184)
        "skipped_parked",  # a parked producer never completes at all
        "inconclusive_no_fail_exit",  # SEM-09 boundary cannot say FAILURE
        "inconclusive_retries",  # n_retrys > 0: retries unmodeled (DL-53)
        "inconclusive_not_reached",  # zero baseline runs: the failure never fires
    ]
    suppressed: dict[str, int] = {}  # job -> baseline runs minus this case's
    cycle: bool = False


class FlagCase(BaseModel):
    """One flag-sweep case: a whole ASSIGNMENT over one co-reference
    component's globals pinned via SET_GLOBAL at start (the reset variant
    sets off-values mid-window), the estate replayed, and the consumers
    gated on those globals checked against bounds whose wake credit is the
    could-fire evaluation, not a flat per-set +1. One global per case was
    the slice review's first blocker: a compound multi-global gate never
    had all its globals scripted, so it never got checked while the report
    claimed coverage."""

    assignment: dict[str, str]
    reset: bool  # the mid-window reset variant (DL-182 c: with/without)
    checked: int = 0  # consumers this case moved from unchecked to checked
    deviations: int = 0
    cycle: bool = False


def _multi_fire_findings(
    bounds: Mapping[str, Bound], runs: Mapping[str, int], *, case: str | None = None
) -> list[CheckFinding]:
    """The one spelling of "observed > bound becomes a multi_fire finding"
    -- the happy path and both sweeps carried a copy each, and the copies
    had begun to drift (DL-185)."""
    out: list[CheckFinding] = []
    for name in sorted(runs):
        bound = bounds.get(name)
        if bound is not None and bound.expected is not None and runs[name] > bound.expected:
            out.append(
                CheckFinding(
                    kind="multi_fire",
                    jobs=[name],
                    detail=(
                        f"observed {runs[name]} runs, expected at most"
                        f" {bound.expected} ({bound.provenance})"
                    ),
                    case=case,
                )
            )
    return out


def _cycle_finding(cycle: ZeroDelayCycleError, *, case: str | None = None) -> CheckFinding:
    """The one spelling of the guard-trip conversion (DL-185)."""
    return CheckFinding(
        kind="zero_delay_cycle", jobs=list(cycle.jobs), detail=str(cycle), case=case
    )


class CadenceCheck(BaseModel):
    """The nested `cadence_check` report block (versioned; the legacy
    rehearse rollup around it stays byte-identical, DL-184)."""

    schema_version: Literal[1] = 1
    start: datetime
    horizon: datetime
    sweeps: list[str] = ["happy"]
    jobs: dict[str, JobCheck]
    findings: list[CheckFinding]
    fail_sweep: list[SweepCase] = []
    flag_sweep: list[FlagCase] = []
    #: globals whose cases fell past FLAG_CASE_CEILING: their consumers
    #: stay unchecked and the coverage claim honestly excludes them
    flag_uncovered: list[str] = []
    note: str = UNMODELED_NOTE

    @property
    def checked(self) -> int:
        return sum(1 for j in self.jobs.values() if j.expected is not None)

    @property
    def unchecked(self) -> int:
        return len(self.jobs) - self.checked

    @property
    def ran(self) -> int:
        """Jobs the play actually exercised -- a coverage line claiming
        "checked" over an estate where nothing ran misled (review find)."""
        return sum(1 for j in self.jobs.values() if j.observed > 0)


# ----------------------------------------------------------- expectations


def scheduled_ticks(
    catalog: CatalogIR,
    *,
    start: datetime,
    horizon: datetime,
    default_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Per-job tick counts in [start, horizon] from a FRESH Scheduler with
    the engine's own construction arguments: reset(start) is tick-at-start
    inclusive and run_until_quiescent is at-or-before-horizon inclusive, so
    the windows match by construction."""
    sched = Scheduler(catalog, start=start, default_tz=default_tz, tz_aliases=tz_aliases)
    counts: dict[str, int] = {}
    for ev in sched.pop_due(horizon):
        job = str(ev.payload["job"])
        counts[job] = counts.get(job, 0) + 1
    return counts


def scenario_budgets(events: Iterable[Event]) -> tuple[dict[str, int], dict[str, int]]:
    """Enforce the check-mode allowlist and return the injected-start
    budgets (STARTJOB, FORCE_STARTJOB) per target job. Any other kind
    refuses: a scripted STATUS forges the very counts under test (DL-184);
    SET_GLOBAL passes with no budget -- global-gated consumers are
    unchecked until the flag sweep."""
    injected_start: dict[str, int] = {}
    injected_force: dict[str, int] = {}
    for ev in events:
        if ev.kind not in CHECK_EVENT_KINDS:
            raise CadenceCheckError(
                f"scenario event kind {ev.kind!r} is outside the --check-cadence"
                f" allowlist ({', '.join(sorted(CHECK_EVENT_KINDS))}): it would"
                " forge the run counts under test (DL-184)"
            )
        if ev.kind in ("STARTJOB", "FORCE_STARTJOB"):
            target = str(ev.payload["job"])
            budget = injected_force if ev.kind == "FORCE_STARTJOB" else injected_start
            budget[target] = budget.get(target, 0) + 1
    return injected_start, injected_force


def success_exit(job: JobIR) -> int | None:
    """The smallest exit code this job's own SEM-09 boundary calls SUCCESS,
    or None when no code in 0..255 does (a fail_codes range can cover the
    whole vocabulary). The FakeAdapter global default of exit 0 is NOT a
    happy path: fail_codes can classify 0 as FAILURE (DL-184)."""
    for code in range(256):
        if job.sem.exit_is_success(code):
            return code
    return None


def failure_exit(job: JobIR) -> int | None:
    """The smallest exit code this job's own SEM-09 boundary calls FAILURE,
    or None when every code in 0..255 is a SUCCESS (a fail sweep cannot
    fail such a producer through its exit -- inconclusive, DL-184)."""
    for code in range(256):
        if not job.sem.exit_is_success(code):
            return code
    return None


def fail_sweep_producers(catalog: CatalogIR, graph: DerivedGraph) -> list[str]:
    """The fail sweep's case list: every distinct LOCAL producer of a live
    start gate (DL-184; cross-instance and undefined producers are
    unknowable here, and bare n() partners are not failure producers --
    the DL-181 reading keeps n() out of failure consumption). Box-override
    references (box_success/box_failure, M15/M16) are completion
    predicates, not start gates, so they contribute no producers either."""
    producers: set[str] = set()
    for edges in start_gates(graph).values():
        for edge in edges:
            lp = local_producer(edge, catalog)
            if lp is not None:
                producers.add(lp)
    return sorted(producers)


def case_fail_entry(adapter: FakeAdapter, producer: str, fail_code: int) -> tuple[float, int]:
    """The script entry a fail-sweep case writes over (producer, 1): the
    synthesized FAILURE exit with the duration the baseline would have used
    -- the scenario's own run-1 entry first, then the job default, then the
    estate default. DL-184 authorizes synthesizing the exit, never the
    duration (the slice-1 review's blocker); this slice's review found the
    first draft re-clobbering a scripted run-1 duration the same way. A
    scripted run-1 PARK falls through to the defaults: the sweep overrides
    the park (the sweep-entry-over-scenario-entry rule), and a park carries
    no duration to preserve."""
    scripted = adapter.script.get((producer, 1))
    if scripted is not None:
        return (scripted[0], fail_code)
    entry = adapter.job_default.get(producer, adapter.default)
    return (entry[0] if entry is not None else 0.0, fail_code)


def run_fail_sweep(
    catalog: CatalogIR,
    baseline_runs: Mapping[str, int],
    bounds: Mapping[str, Bound],
    adapter: FakeAdapter,
    events: Iterable[Event],
    *,
    start: datetime,
    horizon: datetime,
    default_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    producers: Iterable[str],
    parked: Collection[str] = (),
    progress: "Callable[[str], None] | None" = None,
) -> tuple[list[SweepCase], list[CheckFinding]]:
    """The dynamic L022 (DL-182 sweep a, ruled by DL-184): one replay per
    producer with that producer's FIRST run scripted to an exit its own
    SEM-09 boundary calls FAILURE (a blanket exit 1 is wrong under
    success_codes/fail_codes -- Q7's lesson); the sweep entry overrides any
    scenario entry. Jobs that dropped runs against the baseline are the
    suppressed-run inventory (info tier, never exit 3 alone); a multi-fire
    against the same bounds, or a tripped zero-delay guard, is a finding
    and does. Bounds stay valid across cases: they are path-independent
    upper bounds, and a failure can only redistribute which gates release.
    BOX producers have no adapter, parked producers never complete,
    n_retrys > 0 producers are unmodeled (DL-53), and a producer with ZERO
    baseline runs is never reached (the case would replay the baseline
    bit-identically -- its only difference is run 1's script entry) -- each
    skips with its outcome named rather than pretending coverage. A
    term_run_time shorter than the failing run's duration turns the FAILURE
    into TERMINATED (SEM-14): accepted -- still a non-success, though f()
    gates read FAILURE only, t()/d() read the kill."""
    events = list(events)  # one materialization, replayed per case
    cases: list[SweepCase] = []
    findings: list[CheckFinding] = []
    for producer in producers:
        job = catalog.jobs[producer]
        if job.job_type == "BOX":
            cases.append(SweepCase(producer=producer, outcome="skipped_box"))
            continue
        if producer in parked or producer in adapter.park:
            cases.append(SweepCase(producer=producer, outcome="skipped_parked"))
            continue
        if job.sem.n_retrys > 0:
            cases.append(SweepCase(producer=producer, outcome="inconclusive_retries"))
            continue
        fail_code = failure_exit(job)
        if fail_code is None:
            cases.append(SweepCase(producer=producer, outcome="inconclusive_no_fail_exit"))
            continue
        if baseline_runs.get(producer, 0) == 0:
            cases.append(SweepCase(producer=producer, outcome="inconclusive_not_reached"))
            continue
        if progress is not None:
            progress(f"sweep fail {producer}")
        script = dict(adapter.script)
        script[(producer, 1)] = case_fail_entry(adapter, producer, fail_code)
        case_adapter = FakeAdapter(
            script,
            default=adapter.default,
            park=adapter.park,
            job_default=adapter.job_default,
        )
        case_id = f"fail:{producer}"
        result = play_once(
            catalog,
            start=start,
            horizon=horizon,
            adapter=case_adapter,
            events=list(events),
            default_tz=default_tz,
            tz_aliases=tz_aliases,
        )
        suppressed = {
            name: baseline_runs.get(name, 0) - runs
            for name, runs in sorted(result.runs.items())
            if baseline_runs.get(name, 0) - runs > 0
        }
        cases.append(
            SweepCase(
                producer=producer,
                outcome="ran",
                suppressed=suppressed,
                cycle=result.cycle is not None,
            )
        )
        if result.cycle is not None:
            findings.append(_cycle_finding(result.cycle, case=case_id))
        findings.extend(_multi_fire_findings(bounds, result.runs, case=case_id))
    return cases, findings


#: Flag-sweep case ceiling: cases past it are dropped whole-component and
#: the report names the uncovered globals (DL-184: unchecked past the
#: ceiling). Components drop in sorted order -- deterministic simplicity;
#: revisit if a real estate loses its hot global to the ceiling.
FLAG_CASE_CEILING = 64

#: Bounded whole-condition assignment search per component: region-product
#: points probed before a consumer's satisfying assignment is declared not
#: found and its globals reported uncovered (DL-184's ceiling clause).
FLAG_ASSIGNMENT_PROBES = 4096


def _cond_could_fire(
    cond: object,
    globals_: Mapping[str, str],
    job_truth: Callable[[StatusAtom | ExitCodeAtom], bool],
) -> bool:
    """Whether the condition can evaluate TRUE under these global values
    (unset -> False for EVERY operator, the oracle's own reading) with
    job-atom truth supplied by the caller: the genesis truth for at-start
    wake credit, all-True optimism for mid-window credit."""
    if isinstance(cond, And):
        return all(_cond_could_fire(op, globals_, job_truth) for op in cond.operands)
    if isinstance(cond, Or):
        return any(_cond_could_fire(op, globals_, job_truth) for op in cond.operands)
    if isinstance(cond, Paren):
        return _cond_could_fire(cond.inner, globals_, job_truth)
    if isinstance(cond, GlobalAtom):
        actual = globals_.get(cond.name)
        if actual is None:
            return False
        return compare_value(actual, cond.op, cond.value)
    assert isinstance(cond, StatusAtom | ExitCodeAtom)
    return job_truth(cond)


def genesis_truth(catalog: CatalogIR) -> Callable[[StatusAtom | ExitCodeAtom], bool]:
    """Job-atom truth at a fresh genesis (the at-start wake-credit side of
    the flag sweep). SEM-24 can seed only INACTIVE/ON_HOLD/ON_ICE/ON_NOEXEC
    -- never a terminal -- so s/f/d/t and exit-code atoms are FALSE at
    genesis, with two exceptions the slice review caught the first draft
    missing: bare or qualified n() is TRUE (a never-run partner is
    notrunning, oracle's own reading), and an ON_ICE seed satisfies EVERY
    atom naming that job (SEM-05/SEM-20)."""

    def truth(atom: StatusAtom | ExitCodeAtom) -> bool:
        if atom.job.instance is None:
            job = catalog.jobs.get(atom.job.name)
            if job is not None and job.sem.initial_status == "ON_ICE":
                return True
        if isinstance(atom, ExitCodeAtom):
            return False
        return atom.status == "NOTRUNNING"

    return truth


def _optimistic_truth(_atom: StatusAtom | ExitCodeAtom) -> bool:
    """Mid-window job-atom truth: latches can be true by then, so the
    could-fire test leans TRUE -- credit granted, never a manufactured
    deviation (the safe direction; the review's m4 notes the over-credit
    for windows where no latch exists yet)."""
    return True


def _wake_dep_global_consumers(catalog: CatalogIR, graph: DerivedGraph) -> dict[str, set[str]]:
    """Wake-dependent (condition-only, unboxed) consumers with global atoms
    in their START condition, mapped to the referenced global names -- the
    only jobs whose bound the flag sweep can move, so the only ones whose
    globals earn cases (the review's m5: nightbank generated 18 cases for
    globals no such consumer reads)."""
    parent_of = graph.box_tree.parent
    out: dict[str, set[str]] = {}
    for name, job in catalog.jobs.items():
        if job.schedule is not None or parent_of.get(name) is not None:
            continue
        cond_attr = job.sem.condition
        if cond_attr is None:
            continue
        gnames = {a.name for a in iter_atoms(cond_attr.cond) if isinstance(a, GlobalAtom)}
        if gnames:
            out[name] = gnames
    return out


def flag_cases(
    catalog: CatalogIR, graph: DerivedGraph
) -> tuple[list[tuple[dict[str, str], bool]], list[str]]:
    """The flag sweep's case list, per CO-REFERENCE COMPONENT of globals
    (globals sharing a consumer merge; scripting one at a time never lifts
    a compound gate -- the slice review's first blocker):

    - a single-global component gets one case per region representative
      (equiv.global_regions -- numeric and string cutpoints, never
      literal-by-literal), set-only and set-plus-reset variants (DL-182 c);
    - a multi-global component gets, per consumer it gates, one
      whole-condition SATISFYING assignment (every global atom true) and
      one FALSIFYING assignment, found by a bounded lexicographic search
      over the region product (DL-184's own sentence), deduplicated, each
      in both variants.

    The unset None representative is excluded (SET_GLOBAL cannot produce
    it; the happy path already plays it). Whole components past
    FLAG_CASE_CEILING, and consumers whose bounded search finds no
    satisfying assignment, land their globals in `uncovered`."""
    from dsl41.equiv import global_regions

    conds = [
        job.sem.condition.cond for job in catalog.jobs.values() if job.sem.condition is not None
    ]
    regions = {
        name: [v for v in values if v is not None] for name, values in global_regions(conds).items()
    }
    consumers = _wake_dep_global_consumers(catalog, graph)
    # union-find over co-referenced globals
    parent: dict[str, str] = {}

    def find(g: str) -> str:
        root = g
        while parent.setdefault(root, root) != root:
            root = parent[root]
        parent[g] = root
        return root

    for gnames in consumers.values():
        first, *rest = sorted(gnames)
        for other in rest:
            parent[find(other)] = find(first)
    components: dict[str, list[str]] = {}
    for gname in sorted(set().union(*consumers.values())) if consumers else []:
        components.setdefault(find(gname), []).append(gname)

    cases: list[tuple[dict[str, str], bool]] = []
    uncovered: set[str] = set()
    for _root, comp in sorted(components.items(), key=lambda kv: kv[1]):
        comp_cases: list[dict[str, str]] = []
        if len(comp) == 1:
            gname = comp[0]
            comp_cases = [{gname: v} for v in regions.get(gname, [])]
        else:
            atoms_by_consumer = {
                name: [
                    a
                    for a in iter_atoms(catalog.jobs[name].sem.condition.cond)  # type: ignore[union-attr]
                    if isinstance(a, GlobalAtom)
                ]
                for name, gnames in consumers.items()
                if gnames <= set(comp)
            }
            for name in sorted(atoms_by_consumer):
                gatoms = atoms_by_consumer[name]
                satisfying = _search_assignment(comp, regions, gatoms, want=True)
                falsifying = _search_assignment(comp, regions, gatoms, want=False)
                if satisfying is None:
                    uncovered.update(comp)  # coverage incomplete for the component
                for found in (satisfying, falsifying):
                    if found is not None and found not in comp_cases:
                        comp_cases.append(found)
        variant_cases = [
            (assignment, reset) for assignment in comp_cases for reset in (False, True)
        ]
        if len(cases) + len(variant_cases) > FLAG_CASE_CEILING:
            uncovered.update(comp)
            continue
        cases.extend(variant_cases)
    return cases, sorted(uncovered)


def _search_assignment(
    comp: list[str],
    regions: Mapping[str, list[str]],
    gatoms: list[GlobalAtom],
    *,
    want: bool,
) -> dict[str, str] | None:
    """Bounded lexicographic search over the component's region product for
    an assignment where every atom is satisfied (want=True) or at least one
    is not (want=False). None past FLAG_ASSIGNMENT_PROBES."""
    names = sorted(comp)
    lists = [regions.get(name, []) for name in names]
    if any(not values for values in lists):
        return None
    indexes = [0] * len(names)
    probes = 0
    while probes < FLAG_ASSIGNMENT_PROBES:
        probes += 1
        assignment = {name: lists[i][indexes[i]] for i, name in enumerate(names)}
        all_true = all(
            compare_value(assignment[a.name], a.op, a.value) for a in gatoms if a.name in assignment
        )
        if all_true is want:
            return assignment
        pos = len(names) - 1
        while pos >= 0:
            indexes[pos] += 1
            if indexes[pos] < len(lists[pos]):
                break
            indexes[pos] = 0
            pos -= 1
        if pos < 0:
            return None  # product exhausted
    return None


def run_flag_sweep(
    catalog: CatalogIR,
    graph: DerivedGraph,
    ticks: Mapping[str, int],
    adapter: FakeAdapter,
    events: Iterable[Event],
    *,
    start: datetime,
    horizon: datetime,
    default_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
    injected_start: Mapping[str, int] | None = None,
    injected_force: Mapping[str, int] | None = None,
    policy: CadencePolicy | None = None,
    parked: Collection[str] = (),
    no_success_exit: Collection[str] = (),
    progress: Callable[[str], None] | None = None,
) -> tuple[list[FlagCase], list[CheckFinding], list[str]]:
    """The dynamic recovery of L021's globals exclusion (DL-182 sweep c,
    ruled by DL-184): per case, a whole assignment over one co-reference
    component pinned via SET_GLOBAL at start (the reset variant sets
    off-values mid-window -- "" or NUL, themselves values that can satisfy
    != and ordering atoms), the estate replayed, and the bounds recomputed
    with `scripted_globals` -- consumers gated only on scripted globals
    become checked, wake-budgeted by the could-fire evaluation. Base
    scenario SET_GLOBALs join the scripted set (they play in every case).
    Deviations and tripped guards are case-tagged findings and exit 3;
    everything else is report-only."""
    events = list(events)
    base_globals: list[tuple[str, str, bool]] = []
    for ev in events:
        if ev.kind == "SET_GLOBAL":
            base_globals.append((str(ev.payload["name"]), str(ev.payload["value"]), ev.at <= start))
    cases_spec, uncovered = flag_cases(catalog, graph)
    gated = _wake_dep_global_consumers(catalog, graph)
    mid = start + (horizon - start) / 2
    out: list[FlagCase] = []
    findings: list[CheckFinding] = []
    for assignment, reset in cases_spec:
        label = ",".join(f"{g}={v!r}" for g, v in sorted(assignment.items()))
        case_id = f"flags:{label}" + ("+reset" if reset else "")
        if progress is not None:
            progress(f"sweep {case_id}")
        case_events = [
            Event(at=start, kind="SET_GLOBAL", payload={"name": g, "value": v})
            for g, v in sorted(assignment.items())
        ]
        scripted = [*base_globals, *((g, v, True) for g, v in sorted(assignment.items()))]
        if reset:
            for g, v in sorted(assignment.items()):
                reset_value = "" if v != "" else "\x00"
                case_events.append(
                    Event(at=mid, kind="SET_GLOBAL", payload={"name": g, "value": reset_value})
                )
                scripted.append((g, reset_value, False))
        bounds = expected_bounds(
            catalog,
            graph,
            ticks,
            injected_start=injected_start,
            injected_force=injected_force,
            policy=policy,
            parked=parked,
            no_success_exit=no_success_exit,
            scripted_globals=scripted,
        )
        result = play_once(
            catalog,
            start=start,
            horizon=horizon,
            adapter=adapter,
            events=[*events, *case_events],
            default_tz=default_tz,
            tz_aliases=tz_aliases,
        )
        if result.cycle is not None:
            findings.append(_cycle_finding(result.cycle, case=case_id))
        case_findings = _multi_fire_findings(bounds, result.runs, case=case_id)
        findings.extend(case_findings)
        # ONE predicate decides who is a global-gated consumer (DL-185: the
        # third hand copy of this rule counted a scheduled job whose bound
        # never moved -- the house over-claim class)
        scripted_names = {n for n, _v, _s in scripted}
        checked = sum(
            1
            for name, gnames in gated.items()
            if gnames & set(assignment)  # this case's own globals, not
            and gnames <= scripted_names  # purely base-scripted rows
            and bounds[name].expected is not None
        )
        out.append(
            FlagCase(
                assignment=assignment,
                reset=reset,
                checked=checked,
                deviations=len(case_findings),
                cycle=result.cycle is not None,
            )
        )
    return out, findings, uncovered


def check_adapter(
    catalog: CatalogIR, base: FakeAdapter
) -> tuple[FakeAdapter, frozenset[str], frozenset[str]]:
    """Rebuild the scenario adapter for check mode: auto-park unscripted
    file watchers (their cadence is unknowable) and synthesize each
    remaining job's completion from its own SEM-09 boundary. Returns
    (adapter, parked_fw, no_success) -- both sets feed `unchecked`.

    A scenario `default: null` (the script drives every completion) is
    honored: no synthesis happens, because a per-job default would complete
    runs the scenario deliberately parked."""
    scripted = {job for (job, _run) in base.script}
    parked_fw = frozenset(
        name
        for name, job in catalog.jobs.items()
        if job.job_type == "FW" and name not in scripted and name not in base.park
    )
    job_default: dict[str, tuple[float, int] | None] = {}
    no_success: set[str] = set()
    if base.default is not None:
        duration_s = base.default[0]  # DL-184 synthesizes the EXIT, never the
        for name, job in catalog.jobs.items():  # scenario's default duration
            if job.job_type == "BOX" or name in base.park or name in parked_fw:
                continue  # boxes run no adapter; parked jobs stay parked
            code = success_exit(job)
            if code is None:
                no_success.add(name)
                job_default[name] = None  # park: it cannot complete SUCCESS
            else:
                job_default[name] = (duration_s, code)
    adapter = FakeAdapter(
        base.script,
        default=base.default,
        park=base.park | parked_fw,
        job_default=job_default,
    )
    return adapter, parked_fw, frozenset(no_success)


def expected_bounds(
    catalog: CatalogIR,
    graph: DerivedGraph,
    ticks: Mapping[str, int],
    *,
    injected_start: Mapping[str, int] | None = None,
    injected_force: Mapping[str, int] | None = None,
    policy: CadencePolicy | None = None,
    parked: Collection[str] = (),
    no_success_exit: Collection[str] = (),
    scripted_globals: Sequence[tuple[str, str, bool]] | None = None,
) -> dict[str, Bound]:
    """The DL-182 default as a typed bound per job (DL-184 mechanics):

    - scheduled, unboxed: own ticks + injected STARTJOBs.
    - box member: min-composed, never summed -- SEM-10 runs a member at
      most once per box execution, and a sum would hide exactly the double
      run the check hunts. Unscheduled member: bound(box). Scheduled:
      min(bound(box), own ticks + injected STARTJOBs).
    - condition-only: max over wake sources' bounds, to fixpoint over the
      SCC-free graph (a DAG plus the box tree, so it converges within one
      pass per propagation depth). Wake sources are ALL start-gate edges --
      lookback-qualified producers still wake and bare n() partners are
      wake sources and never latches (L021's readings). An undefined local
      producer contributes nothing: no job in this catalog can ever
      complete it during a rehearsal.
    - injected FORCE_STARTJOBs add AFTER the member min (force bypasses
      the gates the min models).
    - wake cycles -- SCCs over the wake adjacency (start gates plus bare
      n(), self-loops included; graph.cycles alone misses the n() loops) --
      have NO finite bound: one kick spins an edge-triggered loop forever,
      and a fixpoint over it diverges (+1 per trip), so members go
      unchecked and the runtime owns the story -- an actual spin trips the
      zero-delay guard and reports as a finding.
    - the wake-flavored unchecked reasons (global gate, cross-instance
      wake, wake cycle, unchecked wake source) apply ONLY to jobs whose
      bound actually reads wakes: condition-only AND unboxed. A scheduled
      job's condition can only release an armed tick (SEM-30/31,
      oracle-side), and a member runs at most once per box execution
      (SEM-10), so their bounds are sound whatever the wake sources do --
      absorbing through them blanked half an estate for nothing (the
      slice's adversarial review).
    - `scripted_globals` (the flag sweep, DL-184; entries are (name,
      value, at_start)) lifts the global gate: a consumer EVERY one of
      whose referenced globals is scripted by this play is checked. The
      wake budget is a COULD-FIRE evaluation per set on a referenced
      global: the whole condition, with globals at their values so far
      (unset -> False for every operator, the oracle's reading) and job
      atoms at their GENESIS truth for at-start sets -- bare/qualified n()
      is TRUE there and an ON_ICE seed satisfies every atom on that job
      (SEM-05/SEM-20); s/f/d/t and exit codes are false, SEM-24 cannot
      seed a terminal -- or optimistic-TRUE for mid-window sets (latches
      can be true by then; over-credit is the safe direction). A set whose
      whole condition cannot then be true buys no headroom: a flat +1
      would let the DL-180 stale-latch multi-fire slide under the bound.
      Accepted corner: a tick at exactly --start completing instantly can
      interleave with an at-start set and fire a latch once legitimately
      -- a rare false positive the policy file can declare.
    - unchecked, absorbing where the bound depends on it: parked file
      watchers, jobs with no SUCCESS exit, policy nulls; box members
      absorb an unchecked box; wake-dependent consumers absorb any of the
      above plus the wake-flavored reasons.
    - a policy max_runs pins the job's bound (frozen through the fixpoint,
      so a declared alert bound propagates to its consumers).
    """
    inj_s = dict(injected_start or {})
    inj_f = dict(injected_force or {})
    gates = start_gates(graph)
    parent_of = graph.box_tree.parent
    # ONE case analysis of what bounds a job, computed once and read by the
    # value AND provenance passes (DL-185: the (schedule, parent) pair was
    # re-derived at four sites): member beats scheduled beats wake-dependent
    kind = {
        name: (
            "member"
            if parent_of.get(name) is not None
            else ("scheduled" if job.schedule is not None else "wake")
        )
        for name, job in catalog.jobs.items()
    }
    # the jobs whose VALUE reads wake sources: every wake-flavored unchecked
    # reason is scoped to exactly this set
    wake_dep = {name for name, k in kind.items() if k == "wake"}

    unchecked: dict[str, str] = {}
    for name in parked:
        unchecked[name] = "file watcher parked: cadence unknowable unless scripted"
    for name in no_success_exit:
        unchecked[name] = "no exit code its SEM-09 boundary calls SUCCESS; parked"
    scripted = list(scripted_globals or ())
    scripted_names = {gname for gname, _value, _at_start in scripted}
    at_start_truth = genesis_truth(catalog)
    global_wakes: dict[str, int] = {}
    # ONE predicate decides who is a global-gated consumer (DL-185); the
    # START condition only -- a v() inside box_success folds a running box
    # and starts nothing
    for name, gnames in _wake_dep_global_consumers(catalog, graph).items():
        if name in unchecked:
            continue
        cond_attr = catalog.jobs[name].sem.condition
        assert cond_attr is not None  # membership implies a condition
        if gnames - scripted_names:
            unchecked[name] = "global-gated: wake budget unknowable until the flag sweep"
            continue
        # could-fire wake credit, in event order with at-start sets first: a
        # set on a referenced global buys one wake exactly when the WHOLE
        # condition can then be true -- job atoms at their genesis truth for
        # at-start sets (bare n() IS true there, an ON_ICE seed satisfies
        # everything -- the slice review's second blocker), optimistic-true
        # for mid-window sets (latches can be true by then)
        values: dict[str, str] = {}
        credit = 0
        for target_at_start in (True, False):
            truth = at_start_truth if target_at_start else _optimistic_truth
            for gname, gvalue, at_start in scripted:
                if at_start is not target_at_start:
                    continue
                values[gname] = gvalue
                if gname in gnames and _cond_could_fire(cond_attr.cond, values, truth):
                    credit += 1
        global_wakes[name] = credit
    for edge in graph.edges:
        if edge.dst not in wake_dep or edge.dst in unchecked:
            continue
        if edge.is_start_gate and not isinstance(edge.atom, GlobalAtom):
            if edge.atom.job.instance is not None:
                unchecked[edge.dst] = f"cross-instance wake {edge.src!r} (DL-162a)"
    # the wake adjacency: start-gate local producers PLUS bare n() targets --
    # mutex classification keeps bare n() out of the edge set, and no edge
    # reader sees that its targets still WAKE the consumer (L021's own
    # warning; the first draft here proved it by missing them, DL-184)
    wake_srcs: dict[str, set[str]] = {name: set() for name in catalog.jobs}
    for name in catalog.jobs:
        for edge in gates.get(name, ()):
            lp = local_producer(edge, catalog)
            if lp is not None:
                wake_srcs[name].add(lp)
        for target in graph.bare_notrunning.get(name, ()):
            if target in catalog.jobs:
                wake_srcs[name].add(target)  # self included: a self-loop cycles
    # feedback flows only through wake-dependent nodes (everyone else's
    # value ignores wakes), so SCCs are computed on that induced subgraph
    feedback = {name: {w for w in wake_srcs[name] if w in wake_dep} for name in wake_dep}
    for scc in cycles(sorted(wake_dep), feedback):
        for name in scc:
            if name not in unchecked:
                unchecked[name] = (
                    "wake cycle: no finite bound (an actual spin trips the"
                    " zero-delay guard and reports as a finding)"
                )

    frozen: set[str] = set()
    value: dict[str, int] = {}
    if policy is not None:
        for name, jp in policy.policies.items():
            if jp.max_runs is None:
                unchecked[name] = f"policy: {jp.reason}"
            else:
                unchecked.pop(name, None)  # the human declared it; policy wins
                value[name] = jp.max_runs
                frozen.add(name)
    for name in catalog.jobs:
        if name not in unchecked and name not in value:
            value[name] = 0

    passes = 0
    changed = True
    while changed:
        passes += 1
        if passes > len(catalog.jobs) + 2:
            # the SCC-free graph converges within one pass per level; only a
            # cycle that escaped the guard above can get here -- loud, never
            # a spin (the bug class this cap was added for)
            raise AssertionError(
                "expected_bounds fixpoint did not converge: a cycle escaped"
                " the wake-adjacency SCC guard"
            )
        changed = False
        for name in list(value):
            if name in frozen:
                continue
            parent = parent_of.get(name)
            if parent is not None and parent in unchecked:
                unchecked[name] = f"box {parent!r} unchecked"
                del value[name]
                changed = True
                continue
            if name not in wake_dep:
                continue  # this bound never reads wakes: sound regardless
            infected = next((w for w in sorted(wake_srcs[name]) if w in unchecked), None)
            if infected is not None:
                unchecked[name] = f"wake source {infected!r} unchecked"
                del value[name]
                changed = True
        for name in value:
            if name in frozen:
                continue
            own = ticks.get(name, 0) + inj_s.get(name, 0)
            if kind[name] == "member":
                box_bound = value.get(parent_of[name], 0)
                has_schedule = catalog.jobs[name].schedule is not None
                base = min(box_bound, own) if has_schedule else box_bound
            elif kind[name] == "scheduled":
                base = own
            else:
                wake = max((value.get(w, 0) for w in wake_srcs[name]), default=0)
                base = wake + inj_s.get(name, 0) + global_wakes.get(name, 0)
            v = base + inj_f.get(name, 0)
            if v > value[name]:
                value[name] = v
                changed = True

    bounds: dict[str, Bound] = {}
    for name, job in catalog.jobs.items():
        if name in unchecked:
            bounds[name] = Bound(expected=None, provenance=unchecked[name])
            continue
        if name in frozen:
            assert policy is not None
            bounds[name] = Bound(
                expected=value[name], provenance=f"policy: {policy.policies[name].reason}"
            )
            continue
        if kind[name] == "member":
            prov = (
                "member: min(box bound, own ticks)"
                if job.schedule is not None
                else "member: box bound"
            )
        elif kind[name] == "scheduled":
            prov = f"own ticks ({ticks.get(name, 0)})"
        else:
            prov = "max over wake sources"
        # a member's bound omits inj_s (SEM-10 caps it inside the min), so
        # its provenance must not claim the budget (the review's m2)
        injected = (
            inj_f.get(name, 0)
            if kind[name] == "member"
            else inj_s.get(name, 0) + inj_f.get(name, 0)
        )
        if injected:
            prov += f" +{injected} injected"
        if global_wakes.get(name):
            prov += f" +{global_wakes[name]} satisfying global sets"
        bounds[name] = Bound(expected=value[name], provenance=prov)
    return bounds


# ------------------------------------------------------------- comparison


def compare(
    catalog: CatalogIR,
    bounds: Mapping[str, Bound],
    observed: Mapping[str, int],
    *,
    start: datetime,
    horizon: datetime,
    sweeps: Iterable[str] = ("happy",),
    cycle: ZeroDelayCycleError | None = None,
) -> CadenceCheck:
    """Deviation = observed > bound, checked jobs only. Under-runs never
    deviate: an f/d/e-gated consumer legitimately observes 0 on the happy
    path (DL-184); the fail sweep owns the suppressed-run story. A
    ZeroDelayCycleError -- and only that EngineError subtype -- converts to
    an unbounded-multi-fire finding: the check FOUND what it hunts."""
    jobs: dict[str, JobCheck] = {}
    runs = {name: observed.get(name, 0) for name in catalog.jobs}
    for name in sorted(catalog.jobs):
        bound = bounds[name]
        obs = runs[name]
        jobs[name] = JobCheck(
            expected=bound.expected,
            provenance=bound.provenance,
            observed=obs,
            deviation=bound.expected is not None and obs > bound.expected,
        )
    findings: list[CheckFinding] = []
    if cycle is not None:
        findings.append(_cycle_finding(cycle))
    findings.extend(_multi_fire_findings(bounds, runs))
    return CadenceCheck(
        start=start, horizon=horizon, sweeps=list(sweeps), jobs=jobs, findings=findings
    )


def render_text(check: CadenceCheck) -> list[str]:
    """The --format text comparison table, one line per job plus the
    findings and the coverage/footer lines."""
    width = max((len(name) for name in check.jobs), default=3)
    lines = [f"-- cadence check: {', '.join(check.sweeps)} --"]
    lines.append(f"{'JOB':<{width}}  {'EXPECTED':>8}  {'OBSERVED':>8}  VERDICT    PROVENANCE")
    for name, job in check.jobs.items():
        expected = "-" if job.expected is None else str(job.expected)
        verdict = "DEVIATION" if job.deviation else ("unchecked" if job.expected is None else "ok")
        provenance = job.provenance
        if "flags" in check.sweeps and provenance.startswith("global-gated"):
            # the happy row keeps its own play's semantics; the pointer says
            # where this job DID get checked (review m2)
            provenance += " -- exercised by the flag sweep below"
        lines.append(
            f"{name:<{width}}  {expected:>8}  {job.observed:>8}  {verdict:<9}  {provenance}"
        )
    lines.append(
        f"checked {check.checked}, unchecked {check.unchecked},"
        f" ran {check.ran} of {len(check.jobs)}, findings {len(check.findings)}"
    )
    if "fail" in check.sweeps:
        lines.append(f"-- fail sweep: {len(check.fail_sweep)} producers --")
        for case in check.fail_sweep:
            if case.outcome != "ran":
                lines.append(f"fail {case.producer}: {case.outcome}")
            elif case.cycle:
                lines.append(f"fail {case.producer}: tripped the zero-delay guard")
            elif case.suppressed:
                drops = ", ".join(f"{name} -{n}" for name, n in case.suppressed.items())
                lines.append(f"fail {case.producer}: suppressed {drops}")
            else:
                lines.append(f"fail {case.producer}: no suppressed runs")
    if "flags" in check.sweeps:
        # keyed on the ONE encoding of "which sweeps ran" (DL-185); an empty
        # block then honestly prints its zero
        lines.append(f"-- flag sweep: {len(check.flag_sweep)} cases --")
        by_component: dict[str, list[FlagCase]] = {}
        for fcase in check.flag_sweep:
            by_component.setdefault(",".join(sorted(fcase.assignment)), []).append(fcase)
        for key, gcases in by_component.items():
            dev = sum(c.deviations for c in gcases)
            spins = sum(1 for c in gcases if c.cycle)
            line = f"flags {key}: {len(gcases)} cases, {dev} deviations"
            if spins:
                line += f", {spins} tripped the zero-delay guard"
            lines.append(line)
    if check.flag_uncovered:
        # outside the case guard: when EVERY component fell past the ceiling
        # there are zero cases yet the omission must still print (review find)
        lines.append("flag sweep uncovered (case ceiling): " + ", ".join(check.flag_uncovered))
    for finding in check.findings:
        where = f" ({finding.case})" if finding.case else ""
        lines.append(f"finding [{finding.kind}]{where} {', '.join(finding.jobs)}: {finding.detail}")
    lines.append(f"note: {check.note}")
    return lines


# ------------------------------------------------------------------ player


@dataclass
class PlayResult:
    """One play's evidence: run_number deltas (the observed counts) and the
    cycle refusal if the play tripped the instant budget -- exactly what a
    sweep case consumes. It carried the case label and the full trace too;
    nothing read either, and a trace per flag case is real memory (DL-185)."""

    runs: dict[str, int]
    cycle: ZeroDelayCycleError | None


def play_once(
    catalog: CatalogIR,
    *,
    start: datetime,
    horizon: datetime,
    adapter: FakeAdapter,
    events: Iterable[Event] = (),
    default_tz: str | None = None,
    tz_aliases: Mapping[str, str] | None = None,
) -> PlayResult:
    """One journal-free virtual-clock play: the reentrant player the sweeps
    and tests reuse (DL-184). A ZeroDelayCycleError is caught and returned
    on the result -- the check's own finding; every other EngineError
    propagates as the shell failure it is."""
    clock = VirtualClock(start)
    scheduler = Scheduler(catalog, start=start, default_tz=default_tz, tz_aliases=tz_aliases)
    engine = Engine(
        catalog, clock=clock, adapters={"CMD": adapter, "FW": adapter}, scheduler=scheduler
    )
    before = {name: engine.oracle.store.runtime(name).run_number for name in catalog.jobs}
    for ev in events:
        engine.inject(ev, source="control")
    cycle: ZeroDelayCycleError | None = None

    async def _play() -> None:
        nonlocal cycle
        try:
            await engine.run_until_quiescent(horizon)
        except ZeroDelayCycleError as exc:
            cycle = exc
        finally:
            await engine.shutdown()

    asyncio.run(_play())
    runs = {
        name: engine.oracle.store.runtime(name).run_number - before[name] for name in catalog.jobs
    }
    return PlayResult(runs=runs, cycle=cycle)
