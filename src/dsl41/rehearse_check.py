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

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.derive import DerivedGraph, cycles, local_producer
from dsl41.ir import CatalogIR, JobIR
from dsl41.lint import start_gates
from dsl41.oracle_state import Event, TraceEntry
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


class JobCheck(Bound):
    """One job's row: its Bound plus what the play observed."""

    observed: int
    deviation: bool


class CadenceCheck(BaseModel):
    """The nested `cadence_check` report block (versioned; the legacy
    rehearse rollup around it stays byte-identical, DL-184)."""

    schema_version: Literal[1] = 1
    start: datetime
    horizon: datetime
    sweeps: list[str] = ["happy"]
    jobs: dict[str, JobCheck]
    findings: list[CheckFinding]
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
    # the jobs whose VALUE reads wake sources: condition-only and unboxed --
    # every wake-flavored unchecked reason is scoped to exactly this set
    wake_dep = {
        name
        for name, job in catalog.jobs.items()
        if job.schedule is None and parent_of.get(name) is None
    }

    unchecked: dict[str, str] = {}
    for name in parked:
        unchecked[name] = "file watcher parked: cadence unknowable unless scripted"
    for name in no_success_exit:
        unchecked[name] = "no exit code its SEM-09 boundary calls SUCCESS; parked"
    for name, job in catalog.jobs.items():
        if name not in wake_dep or name in unchecked:
            continue
        cond_attr = job.sem.condition
        if cond_attr is not None and any(
            isinstance(atom, GlobalAtom) for atom in iter_atoms(cond_attr.cond)
        ):
            # the START condition only -- a v() inside box_success folds a
            # running box and starts nothing (the review's m1)
            unchecked[name] = "global-gated: wake budget unknowable until the flag sweep"
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
            job = catalog.jobs[name]
            parent = parent_of.get(name)
            own = ticks.get(name, 0) + inj_s.get(name, 0)
            if parent is not None:
                box_bound = value.get(parent, 0)
                base = min(box_bound, own) if job.schedule is not None else box_bound
            elif job.schedule is not None:
                base = own
            else:
                wake = max((value.get(w, 0) for w in wake_srcs[name]), default=0)
                base = wake + inj_s.get(name, 0)
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
        parent = parent_of.get(name)
        if parent is not None:
            prov = (
                "member: min(box bound, own ticks)"
                if job.schedule is not None
                else "member: box bound"
            )
        elif job.schedule is not None:
            prov = f"own ticks ({ticks.get(name, 0)})"
        else:
            prov = "max over wake sources"
        # a member's bound omits inj_s (SEM-10 caps it inside the min), so
        # its provenance must not claim the budget (the review's m2)
        injected = (
            inj_f.get(name, 0) if parent is not None else inj_s.get(name, 0) + inj_f.get(name, 0)
        )
        if injected:
            prov += f" +{injected} injected"
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
    findings: list[CheckFinding] = []
    if cycle is not None:
        findings.append(
            CheckFinding(kind="zero_delay_cycle", jobs=list(cycle.jobs), detail=str(cycle))
        )
    for name in sorted(catalog.jobs):
        bound = bounds[name]
        obs = observed.get(name, 0)
        deviation = bound.expected is not None and obs > bound.expected
        jobs[name] = JobCheck(
            expected=bound.expected,
            provenance=bound.provenance,
            observed=obs,
            deviation=deviation,
        )
        if deviation:
            findings.append(
                CheckFinding(
                    kind="multi_fire",
                    jobs=[name],
                    detail=(
                        f"observed {obs} runs, expected at most"
                        f" {bound.expected} ({bound.provenance})"
                    ),
                )
            )
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
        lines.append(
            f"{name:<{width}}  {expected:>8}  {job.observed:>8}  {verdict:<9}  {job.provenance}"
        )
    lines.append(
        f"checked {check.checked}, unchecked {check.unchecked},"
        f" ran {check.ran} of {len(check.jobs)}, findings {len(check.findings)}"
    )
    for finding in check.findings:
        lines.append(f"finding [{finding.kind}] {', '.join(finding.jobs)}: {finding.detail}")
    lines.append(f"note: {check.note}")
    return lines


# ------------------------------------------------------------------ player


@dataclass
class PlayResult:
    """One play's evidence: run_number deltas (the observed counts), the
    trace, and the cycle refusal if the play tripped the instant budget."""

    case: str
    runs: dict[str, int]
    trace: list[TraceEntry]
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
    case: str = "happy",
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
    return PlayResult(case=case, runs=runs, trace=engine.oracle.trace(), cycle=cycle)
