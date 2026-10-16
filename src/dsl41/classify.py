"""Boundary classification: what a period change does to live work.

Normative spec: `docs/period-model.md` ss10 (ss10.1 the three tiers, ss10.2 the classification
graph, ss10.3 the named cases, ss10.4 the armed latch). Built by DL-131. Obligations: PR-37,
PR-37a, PR-38, PR-39, PR-39a, PR-40 through PR-44. PR-39b is readiness's: a `next_period` with
a foreign `state_machine_version` is refused before anything here runs (ss2.1,
`test_period_identity.py`).

The question, asked once per boundary: C1 is open and holds live work, C2 is staged; may the
boundary commit, and what must the seal record about what it carried? Three answers per job:

- **R**: refuse. Something the job is executing against changed. The seal does not commit
  until the run is done or killed.
- **A**: assumed, with a named sentence. The job holds latent intent (an armed latch, a
  QUE_WAIT rank, a live timer) and something it depends on changed. The intent survives the
  boundary (ss10.4) and the assumption rides in the seal.
- **carry**: nothing live. The row crosses unchanged; if its closure moved it is listed in
  the report.

**Pure analysis.** Nothing here reads a disk, a socket or a clock. The inputs are two
`(catalog, profile)` pairs and one carried snapshot, and the same inputs give the same verdicts
byte for byte, so phase 2 can recompute the map the sidecar commits (spec ss7). The seal
operation, the snapshot model and ss10.4's runtime obligation are not here.

**Two directions, both computed** (ss10.2). The R gate reads a job's forward closure: is
anything a live job depends on changed? The boundary-truth diff reads a node's reverse closure,
then evaluates condition truth under C1 and C2 at the carried state: whose readiness flips?

**Edges run from a job to what it depends on**, profile fields included. A live CMD's forward
closure reaches `profile:cmd_grace_us`, so a C2 that changes only the kill ladder's grace
cannot commit over a live run (PR-37a). The reversed spelling reaches no profile field from
any job; a test pins the direction.

**One authority for condition truth.** The readiness diff evaluates conditions through the
interpreter (`_TruthOracle`, a throwaway `OracleReads` seeded with the carried rows), never a
second evaluator: SEM-05's iced-predecessor rule, SEM-06's undefined-is-false rule and the
lookback ladder are semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.autocal import CalendarRuleError, semantic_key, standard_rows
from dsl41.capacity import CapacityPool, resource_type
from dsl41.conditions import Cond, GlobalAtom, iter_atoms
from dsl41.derive import BoxTree, derive_graph
from dsl41.equiv import canonical_cond
from dsl41.ir import CatalogIR, CondAttr, JobIR, unquote_jil_value
from dsl41.oracle import Oracle
from dsl41.oracle_reads import OracleReads
from dsl41.oracle_state import RESOURCE_BUCKET, TERMINAL, JobRuntime
from dsl41.period import (
    RuntimeProfile,
    job_fingerprints,
    oracle_reading,
    switches_of,
)
from dsl41.semantics import REGISTRY as SWITCH_REGISTRY

# ------------------------------------------------------------------- nodes
#
# ss10.2's node table, one prefix per kind, so `resource:FUEL` and a job called `FUEL` differ in
# the list an operator reads.

JOB: Final = "job:"
BOX: Final = "box:"  # a box's containment, at every nesting depth
GLOBAL: Final = "global:"
XINST: Final = "xinst:"
RESOURCE: Final = "resource:"
MACHINE: Final = "machine:"
CALENDAR: Final = "calendar:"
CYCLE: Final = "cycle:"
PROFILE: Final = "profile:"
#: the timezone basis is one node for the estate: `default_tz` plus the alias table
TZ_BASIS: Final = "tz:basis"

#: ss10.2's profile mapping: which jobs each field reaches. `retry_horizon_us` is boundary
#: policy and reaches no job. `semantics` is placed per switch (DL-252): each switch is a node
#: (`SWITCH` + name, at its effective value), and a job depends on it when the registry entry's
#: `affects` says so, or through a calendar its `affects_calendar` names (DL-259).
PROFILE_SCHEDULED: Final = ("default_tz", "tz_aliases")
PROFILE_CMD: Final = (
    "as_machine",
    "machine_policy",
    "execution_mode",
    "deadman_us",
    "cmd_grace_us",
    "reconcile_settle_us",
    "spawn_window_us",
)
PROFILE_FW: Final = ("fw_default_interval_us",)
PROFILE_NO_JOB: Final = ("retry_horizon_us",)
PROFILE_SWITCHED: Final = ("semantics",)
#: one node per semantic switch; see PROFILE_SWITCHED
SWITCH: Final = "profile:semantics."


def _switches_of(job_ir: JobIR, catalog: CatalogIR) -> tuple[str, ...]:
    """The semantic switches whose flip can change this job in `catalog` (DL-252)."""
    return tuple(
        name for name, switch in SWITCH_REGISTRY.items() if switch.affects(job_ir, catalog)
    )


def is_scheduled(job_ir: JobIR) -> bool:
    """ss10.2's "every job with `start_times`, `start_mins` or a calendar": ticks that resolve
    through the timezone basis.

    A calendar includes `exclude_calendar`, because exclusion is a local-day question. A job
    with only an exclusion over-approximates, in the safe direction.
    """
    schedule = job_ir.schedule
    if schedule is None:
        return False
    return bool(
        schedule.start_times
        or schedule.start_mins
        or schedule.run_calendar
        or schedule.exclude_calendar
    )


def reads_zone(job_ir: JobIR) -> bool:
    """ss10.2 with DL-253: the jobs whose interpretation reads the timezone basis.

    These are the scheduled jobs (`is_scheduled`) and every job with a `run_window`, whose
    endpoints are read in the job's zone (SEM-33, SEM-35), or the run's base zone when it has
    no `timezone:`. Absolute must times need start_times, so they ride `is_scheduled`. A job
    with its own `timezone:` keeps the edge, because the alias table resolves its name.
    """
    if is_scheduled(job_ir):
        return True
    return job_ir.schedule is not None and job_ir.schedule.run_window is not None


class Baseline(BaseModel):
    """One period's definition, C1 or C2 in ss10: the catalog and the launch options.

    Both halves, because identical JIL under two timezones is one `catalog_hash` and two sets
    of ticks (ss1.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: CatalogIR
    profile: RuntimeProfile = RuntimeProfile()


# ------------------------------------------------------------ carried state


class CarriedJob(BaseModel):
    """One job's liveness at T, as ss10.1 keys on it.

    `row` is the authoritative `JobRuntime` the seal carries. The four flags beside it are what
    the row cannot say: execution lifecycle lives in the seal's `executions` (ss3.5) and a
    timer lives in the timer heap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: JobRuntime = JobRuntime()
    #: ss3.5's three execution kinds, as booleans for this job's current run
    pending_spawn: bool = False
    bound: bool = False
    fw_watch: bool = False
    #: a non-stale authoritative timer is pending; `Oracle.pending_timers` discards stale entries
    timer: bool = False

    @property
    def executes(self) -> bool:
        """The executing tier's job-local half; the box half needs the tree.

        `pending_spawn` counts (PR-39a): `_apply_spawn` reads the current catalog at dispatch,
        so a pending SPAWN classified latent would run C2's command under C1's run number and
        reservations.
        """
        return (
            self.row.status in ("RUNNING", "STARTING")
            or self.pending_spawn
            or self.bound
            or self.fw_watch
        )

    @property
    def latent(self) -> bool:
        """Latent intent: an armed latch (ss10.4), a QUE_WAIT rank or a live timer; R beats A."""
        return self.row.armed or self.row.status == "QUE_WAIT" or self.timer


class CarriedState(BaseModel):
    """The snapshot the classifier reads: rows, globals, spent units and the observation instant.

    U6 builds it from a seal; `carried_from_oracle` builds it from a live engine.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, CarriedJob] = {}
    globals_: dict[str, str] = {}
    #: SEM-16's irreversible depletion per bucket key (`r:NAME`, `m:NAME`)
    consumed: dict[str, int] = {}
    #: T. `None` falls back to the newest carried `status_at` -- the latest
    #: instant the state itself knows about -- so a window lookback in a
    #: readiness diff has an anchor instead of an assertion.
    now: datetime | None = None

    @property
    def rows(self) -> dict[str, JobRuntime]:
        return {name: carried.row for name, carried in self.jobs.items()}


def carried_from_oracle(
    oracle: Oracle,
    *,
    now: datetime | None = None,
    pending_spawn: Iterable[str] = (),
    bound: Iterable[str] = (),
    fw_watch: Iterable[str] = (),
    timers: Iterable[str] | None = None,
) -> CarriedState:
    """Build a `CarriedState` from a live interpreter.

    The three execution sets are parameters, because their evidence is not the oracle's: a
    pending SPAWN is an outbox entry, a bound run is the supervisor's spool binding, an FW
    watch is `runs/<job>.<n>/watch.jsonl` (ss3.5). `timers` defaults to the jobs
    `Oracle.pending_timers` reports.
    """
    if timers is None:
        timers = [job for _, job, _ in oracle.pending_timers()]
    spawning, binding, watching = set(pending_spawn), set(bound), set(fw_watch)
    timing = set(timers)
    return CarriedState(
        jobs={
            name: CarriedJob(
                row=row,
                pending_spawn=name in spawning,
                bound=name in binding,
                fw_watch=name in watching,
                timer=name in timing,
            )
            for name, row in oracle.store.job.items()
        },
        globals_={name: row.value for name, row in oracle.store.globals_.items()},
        consumed=dict(oracle.store.consumed),
        now=now,
    )


# ------------------------------------------------------------------- graph


def _box_tree(catalog: CatalogIR) -> BoxTree:
    return derive_graph(catalog).box_tree


def _containment(tree: BoxTree, box: str) -> tuple[tuple[str, str], ...]:
    """Every (member, its box) pair under `box` at any nesting depth, sorted: the box node's value.

    Pairs, not the member set: moving a leaf from an inner box to the outer one leaves the outer
    member set identical, but ss10.2 counts it as a containment move.
    """
    out: set[tuple[str, str]] = set()
    stack = [(member, box) for member in tree.children.get(box, ())]
    while stack:
        pair = stack.pop()
        if pair in out:
            continue  # the catalog validator rules out cycles
        out.add(pair)
        stack.extend((member, pair[0]) for member in tree.children.get(pair[0], ()))
    return tuple(sorted(out))


def _trimmed(value: str | None) -> str | None:
    """An attribute as the node value reads it; surrounding whitespace is not a change."""
    return None if value is None else value.strip()


def _named_ref(attrs: Mapping[str, str], key: str) -> str | None:
    """The record `attrs[key]` names, or None.

    Quotes come off through `ir.unquote_jil_value` (jil-statement-syntax rule 7), the function
    that produced the calendar catalog key, and that autocal's holcal/cyccal resolution and
    L018 call (DL-178q).
    """
    return unquote_jil_value(attrs.get(key, "")) or None


def _cycle_periods(periods: Iterable[Any]) -> tuple[Any, ...]:
    """Cycle periods as autocal parses them (`%m/%d/%Y`): `1/1/2026` and `01/01/2026` are one date.

    Order is kept, because period position numbers the chunks. An unparseable pair keeps its
    raw spelling; autocal refuses it.
    """
    from datetime import datetime as _dt

    out: list[Any] = []
    for pair in periods:
        try:
            start_raw, end_raw = pair
            out.append(
                (
                    _dt.strptime(str(start_raw).strip(), "%m/%d/%Y").date(),
                    _dt.strptime(str(end_raw).strip(), "%m/%d/%Y").date(),
                )
            )
        except (TypeError, ValueError):
            out.append(pair)
    return tuple(out)


def _calendar_dates(calendar: Any) -> tuple[Any, ...]:
    """The calendar's rows as `autocal.standard_rows` resolves them: day -> tick set, sorted.

    A row set the resolver refuses falls back to the sorted raw rows; preflight is the gate.
    """
    try:
        rows = standard_rows(calendar)
    except (ValueError, CalendarRuleError):
        return tuple(sorted(calendar.dates))
    return tuple((day.isoformat(), tuple(sorted(ticks))) for day, ticks in sorted(rows.items()))


def _capacity(units: Callable[[], int | None]) -> int | None:
    """`ResourceIR.capacity_units` with a malformed `amount` read as absent.

    A malformed value is preflight's refusal (DL-50), and "unsized" is the same node value.
    The check is typed rather than asserted, because `assert` vanishes under `-O`
    (`oracle._schedule_timer`).
    """
    try:
        return units()
    except ValueError:
        return None


def _node_values(side: Baseline) -> dict[str, Any]:
    """Every node's value under one baseline, per ss10.2's "changed when" column.

    A node absent from a side has no entry, so a removal compares against `None`.
    """
    catalog, profile = side.catalog, side.profile
    values: dict[str, Any] = {}
    for name, digest in job_fingerprints(catalog).items():
        values[JOB + name] = digest
    tree = _box_tree(catalog)
    for box in tree.children:
        values[BOX + box] = _containment(tree, box)
    for name, default in catalog.globals_declared.items():
        values[GLOBAL + name] = default
    for name, xinst in catalog.external_instances.items():
        values[XINST + name] = (xinst.xtype, tuple(sorted(xinst.attrs.items())))
    for name, resource in catalog.resources.items():
        # amount and res_type. res_type decides the release-policy default
        # (`capacity.release_policy`: D never frees; a renewable reads the `renewable-free`
        # switch, a node of its own, DL-256) and whether unfreed units are spent or held
        # (`keeps_held`)
        values[RESOURCE + name] = (
            _capacity(resource.capacity_units),
            # `capacity.resource_type` strips and upper-cases, so `r` and `R` are one policy
            resource_type(resource) or None,
        )
    for name, machine in catalog.machines.items():
        # the fields resolution reads: the load bucket's size, its type, the dispatch node
        # and the membership
        values[MACHINE + name] = (
            # each field as resolution reads it, so a respelling is not a change: type
            # case-folds, node_name unquotes, max_load parses (08 is 8; a malformed value is
            # preflight's refusal and one node value)
            (machine.machine_type or "").lower() or None,
            _capacity(machine.max_load_units),
            _named_ref(machine.attrs, "node_name"),
            # sorted: resolution folds the pool any-of (`runner_preflight`)
            tuple(sorted({member.name for member in machine.members})),
        )
    for name, calendar in catalog.calendars.items():
        # the referenced date set, as the resolver reads it. A standard calendar compares as
        # `autocal.standard_rows`. An extended one compares as what the rule engine derives dates
        # from: its conditions and the six attributes autocal reads, canonicalized through
        # `autocal.semantic_key`; descriptive keys are not a change
        values[CALENDAR + name] = (
            calendar.kind,
            _calendar_dates(calendar),
            semantic_key(calendar, catalog, switches_of(profile)),
        )
    for name, cycle in catalog.cycles.items():
        # autocal's `_period_of` walks the periods only, so a description edit on a cycle is not a
        # change; dates compare parsed (`_cycle_periods`), in order
        values[CYCLE + name] = _cycle_periods(cycle.periods)
    values[TZ_BASIS] = (profile.default_tz, tuple(sorted(profile.tz_aliases.items())))
    for field in RuntimeProfile.model_fields:
        raw = getattr(profile, field)
        values[PROFILE + field] = tuple(sorted(raw.items())) if isinstance(raw, dict) else raw
    switches = switches_of(profile)
    for name in SWITCH_REGISTRY:
        values[SWITCH + name] = switches.value(name)
    return values


class ClassificationGraph:
    """ss10.2's graph over both baselines: the edges of C1 and C2 together, and the moved nodes.

    The union over-approximates in the safe direction: a job whose C2 `machine:` is new depends
    on the new machine, and a job C2 removes still depended on what C1 gave it.

    A carried row's held units are edges too (DL-256): a job holding units of a resource depends
    on it whether or not it still declares it, so a dependent box's forward closure reaches a
    resource change through the holder.
    """

    def __init__(
        self, closing: Baseline, opening: Baseline, *, carried: CarriedState | None = None
    ) -> None:
        self._deps: dict[str, set[str]] = {}
        self._rdeps: dict[str, set[str]] = {}
        for side in (closing, opening):
            self._add_side(side)
        if carried is not None:
            for name, carried_job in carried.jobs.items():
                for resource_node in _held_resources(carried_job.row):
                    self._edge(JOB + name, resource_node)
        before, after = _node_values(closing), _node_values(opening)
        self.changed: frozenset[str] = frozenset(
            node for node in set(before) | set(after) if before.get(node) != after.get(node)
        )

    # -------------------------------------------------------------- edges

    def _edge(self, src: str, dst: str) -> None:
        self._deps.setdefault(src, set()).add(dst)
        self._rdeps.setdefault(dst, set()).add(src)

    def _add_side(self, side: Baseline) -> None:
        catalog = side.catalog
        graph = derive_graph(catalog)
        # (1) condition atoms, walked off each job's condition, not IR-G's edge list: IR-G diverts
        # a local unqualified n() atom into `mutex_groups` (M07) and keeps no edge, so edges would
        # carry a boundary over `b: condition: n(a)` while `b` executes and C2 changes `a`. A
        # `name^INST` atom depends on the boundary declaration, the node this catalog can see
        for name, job_ir in catalog.jobs.items():
            # every condition the job carries, `condition` and a box's `box_success` and
            # `box_failure` (SEM-12), through the walker the linter shares
            for _kind, cond, _span in job_ir.iter_conditions():
                for atom in iter_atoms(cond):
                    if isinstance(atom, GlobalAtom):
                        self._edge(JOB + name, GLOBAL + atom.name)
                        continue
                    ref = getattr(atom, "job", None)
                    if ref is None:
                        continue  # an atom kind with no job reference
                    if ref.instance is not None:
                        self._edge(JOB + name, XINST + ref.instance)
                    else:
                        self._edge(JOB + name, JOB + ref.name)
        # (2) box containment, both directions and nested. A member depends on the containment
        # (`box:B`, which moves when any `box_name` at any depth moves) and on the box's own
        # definition (`job:B`, whose condition and schedule gate the member); the box depends on
        # every member. So a box's forward closure reaches its members (PR-42) and a member's
        # reaches its siblings
        tree = graph.box_tree
        for box, members in tree.children.items():
            self._edge(JOB + box, BOX + box)
            for member in members:
                self._edge(JOB + member, BOX + box)
                self._edge(JOB + member, JOB + box)
                self._edge(BOX + box, JOB + member)
        for name, job_ir in catalog.jobs.items():
            node = JOB + name
            # (3) resources
            for ref in job_ir.resources:
                self._edge(node, RESOURCE + ref.name)
            # (4) the machine, and that machine's members
            spec = job_ir.exec_
            if spec is not None and spec.machine is not None:
                self._edge(node, MACHINE + spec.machine)
            # (5) calendars and cycles
            schedule = job_ir.schedule
            if schedule is not None:
                for cal in (schedule.run_calendar, schedule.exclude_calendar):
                    if cal is not None:
                        self._edge(node, CALENDAR + cal)
            # (6) the timezone basis, for the jobs that read it (DL-253)
            if reads_zone(job_ir):
                self._edge(node, TZ_BASIS)
            # (7) the runtime-profile fields this job's KIND reads
            for field in self._profile_fields(job_ir):
                self._edge(node, PROFILE + field)
            # (8) the semantic switches whose reading this job's conditions
            # depend on (DL-252)
            for switch in _switches_of(job_ir, catalog):
                self._edge(node, SWITCH + switch)
        for name, machine in catalog.machines.items():
            for component in machine.members:
                self._edge(MACHINE + name, MACHINE + component.name)
        for name, calendar in catalog.calendars.items():
            # (9) the semantic switches whose reading changes this
            # calendar's days; a job reaches them through its calendar edge
            for switch_name, entry in SWITCH_REGISTRY.items():
                if entry.affects_calendar(calendar):
                    self._edge(CALENDAR + name, SWITCH + switch_name)
            holcal = _named_ref(calendar.attrs, "holcal")
            if holcal is not None:
                self._edge(CALENDAR + name, CALENDAR + holcal)
            cyccal = _named_ref(calendar.attrs, "cyccal")
            if cyccal is not None:
                self._edge(CALENDAR + name, CYCLE + cyccal)

    @staticmethod
    def _profile_fields(job_ir: JobIR) -> tuple[str, ...]:
        fields: tuple[str, ...] = ()
        if reads_zone(job_ir):
            fields += PROFILE_SCHEDULED
        kind = job_ir.job_type.upper()
        if kind == "CMD":
            fields += PROFILE_CMD
        elif kind == "FW":
            fields += PROFILE_FW
        return fields

    # ------------------------------------------------------------ closures

    def _walk(self, node: str, edges: Mapping[str, set[str]]) -> frozenset[str]:
        seen = {node}
        stack = [node]
        while stack:
            for nxt in edges.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return frozenset(seen)

    def forward(self, node: str) -> frozenset[str]:
        """What `node` depends on, transitively, `node` included: the R gate's question."""
        return self._walk(node, self._deps)

    def reverse(self, node: str) -> frozenset[str]:
        """What depends on `node`, transitively, `node` included: the boundary-truth question."""
        return self._walk(node, self._rdeps)

    def moved(self, node: str) -> tuple[str, ...]:
        """The changed nodes inside `node`'s forward closure, sorted."""
        return tuple(sorted(self.forward(node) & self.changed))


# ------------------------------------------------------------------ verdict

Tier = Literal["executing", "latent", "not_live"]
Verdict = Literal["R", "A", "carry"]

#: ss10.3's assumption sentences, verbatim where the spec writes one.
ARMED_ASSUMPTION: Final = "the C1 trigger survives under C2 gating"
LATENT_ASSUMPTION: Final = "the C1 latent intent survives under C2 gating"
RESOURCE_ASSUMPTION: Final = "admission refuses until releases catch up"
INITIAL_STATUS_ASSUMPTION: Final = (
    "genesis seeding applies to new rows only: the carried row keeps its C1 flags"
)
#: DL-256: a job holds units of a resource C2 changes; they stay on its row, frozen at
#: acquisition, until RELEASE_RESOURCE or its next run.
HELD_ASSUMPTION: Final = "the units it holds stay held under C2's resource definition"


def _held_resources(row: JobRuntime) -> frozenset[str]:
    """The resource nodes whose units `row` holds, live or held (DL-256).

    A job depends on these as well as its declared resources: FORCE_STARTJOB re-uses held
    units the job may not declare, and their fate at run end reads the resource's type.
    """
    return frozenset(
        RESOURCE + reservation.bucket.removeprefix(RESOURCE_BUCKET)
        for reservation in row.reservations
        if reservation.bucket.startswith(RESOURCE_BUCKET)
    )


class JobVerdict(BaseModel):
    """One job's answer. `verdict` and `assumption` go in the seal's `classification` map
    (ss3.1); `tier` and `changed` stay in the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    tier: Tier
    verdict: Verdict
    #: the changed nodes in this job's forward closure, sorted
    changed: tuple[str, ...] = ()
    #: the named A sentence. Non-null exactly when `verdict == "A"`.
    assumption: str | None = None


class ReadinessFlip(BaseModel):
    """One job whose start condition evaluates differently under C2 than C1 at the same carried
    state: the boundary-truth diff (PR-44)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    before: bool
    after: bool


class Classification(BaseModel):
    """A verdict per job, the two ss10.1 report lists, the changed nodes and the readiness diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[JobVerdict, ...] = ()
    #: removed and not live: retained, listed, and referenced by no condition (L001 refuses that)
    ghosts: tuple[str, ...] = ()
    #: not live, and something in its closure moved, so "nothing was live" is not read as
    #: "nothing changed". Includes a job C2 adds. Disjoint from `ghosts`, the same statement
    #: about a job C2 dropped
    changed_not_live: tuple[str, ...] = ()
    changed_nodes: tuple[str, ...] = ()
    readiness_flips: tuple[ReadinessFlip, ...] = ()

    @property
    def by_job(self) -> dict[str, JobVerdict]:
        return {verdict.job: verdict for verdict in self.verdicts}

    @property
    def refused(self) -> tuple[str, ...]:
        """The jobs that make the boundary refuse: the R gate's output."""
        return tuple(v.job for v in self.verdicts if v.verdict == "R")


def _tiers(catalog: CatalogIR, carried: CarriedState, names: Iterable[str]) -> dict[str, Tier]:
    """Each job's tier. A member of an executing box executes at any depth (E19), by C1's tree."""
    tree = _box_tree(catalog)
    empty = CarriedJob()
    tiers: dict[str, Tier] = {}
    for name in names:
        row = carried.jobs.get(name, empty)
        executing = row.executes
        ancestor = tree.parent.get(name)
        while not executing and ancestor is not None:
            executing = carried.jobs.get(ancestor, empty).executes
            ancestor = tree.parent.get(ancestor)
        if executing:
            tiers[name] = "executing"
        elif row.latent:
            tiers[name] = "latent"
        else:
            tiers[name] = "not_live"
    return tiers


def _oversubscribed(opening: Baseline, carried: CarriedState) -> set[str]:
    """The resource nodes C2 lowers below the carried `consumed + held` (ss10.3, ss5)."""
    pool = CapacityPool(opening.catalog, switches_of(opening.profile))
    used = pool.used(carried.rows, carried.consumed)
    short: set[str] = set()
    for name, resource in opening.catalog.resources.items():
        capacity = _capacity(resource.capacity_units)
        if capacity is not None and used.get(f"r:{name}", 0) > capacity:
            short.add(RESOURCE + name)
    return short


def _assumption(
    name: str,
    changed: tuple[str, ...],
    *,
    closing: Baseline,
    opening: Baseline,
    carried: CarriedState,
    short: set[str],
) -> str:
    """The A sentence for one latent job: ss10.3's named cases in order, then the general one.

    The order is the latch that fires first, the admission it waits on, then the flags it keeps.
    """
    row = carried.jobs.get(name, CarriedJob()).row
    before = closing.catalog.jobs.get(name)
    after = opening.catalog.jobs.get(name)
    if row.armed and before is not None and after is not None and _trigger_moved(before, after):
        return ARMED_ASSUMPTION
    switched = (
        set() if after is None else {SWITCH + s for s in _switches_of(after, opening.catalog)}
    )
    if row.armed and switched & set(changed):
        # a flipped switch changes how the latch's own gate reads (DL-252)
        return ARMED_ASSUMPTION
    if short & set(changed):
        return RESOURCE_ASSUMPTION
    if _held_resources(row) & set(changed):
        return HELD_ASSUMPTION
    if before is not None and after is not None:
        initial = after.sem.initial_status
        if before.sem.initial_status != initial and not _row_agrees(row, initial):
            return INITIAL_STATUS_ASSUMPTION
    return LATENT_ASSUMPTION


def _trigger_moved(before: JobIR, after: JobIR) -> bool:
    """Whether an armed latch's trigger moved: the schedule or the condition (ss10.3).

    Conditions compare through `equiv.canonical_cond`, so a reordered `a & b` is no change.
    """
    if before.schedule != after.schedule:
        return True
    return _cond_key(before.sem.condition) != _cond_key(after.sem.condition)


def _cond_key(attr: CondAttr | None) -> Cond | None:
    return None if attr is None else canonical_cond(attr.cond)


def _row_agrees(row: JobRuntime, initial: str | None) -> bool:
    """Whether the carried row already holds what C2's `initial_status` would seed.

    Genesis seeding writes new rows only, so a disagreement is an assumption. The whole
    three-flag vector is compared: HOLD+ICE against a seed of ICE alone disagrees.
    """
    seeded = {
        "ON_HOLD": (True, False, False),
        "ON_ICE": (False, True, False),
        "ON_NOEXEC": (False, False, True),
    }.get(initial or "", (False, False, False))
    return (row.on_hold, row.on_ice, row.on_noexec) == seeded


def classify(
    *,
    closing: Baseline,
    opening: Baseline,
    carried: CarriedState,
) -> Classification:
    """ss10's verdict over one boundary: C1 (`closing`) against C2 (`opening`) at the carried state.

    Deterministic and total: every job of either catalog and every carried row gets one verdict,
    in name order, so audit can reproduce the sealed map byte for byte. Carried rows are in the
    union because a retained ghost is in neither catalog at the next boundary.
    """
    graph = ClassificationGraph(closing, opening, carried=carried)
    names = sorted(set(closing.catalog.jobs) | set(opening.catalog.jobs) | set(carried.jobs))
    tiers = _tiers(closing.catalog, carried, names)
    short = _oversubscribed(opening, carried)
    verdicts: list[JobVerdict] = []
    ghosts: list[str] = []
    changed_not_live: list[str] = []
    for name in names:
        tier = tiers[name]
        # DL-256: the resources the row holds are dependencies; the graph carries them as edges
        held_moved = _held_resources(carried.jobs.get(name, CarriedJob()).row) & graph.changed
        changed = graph.moved(JOB + name)
        #: "removed" is about what C2 can dispatch, so it reads the opening catalog alone
        removed = name not in opening.catalog.jobs
        verdict: Verdict = "carry"
        assumption: str | None = None
        if tier == "executing" and (removed or changed):
            # a removed job is not in `dispatchable`, so a KILL plans no effect and the run C1
            # started cannot end; the boundary must not open over it
            verdict = "R"
        elif tier == "latent" and removed:
            verdict = "R"  # PR-40: the latch has nothing left to start
        elif tier == "latent" and changed:
            verdict = "A"
            assumption = _assumption(
                name, changed, closing=closing, opening=opening, carried=carried, short=short
            )
        elif held_moved and not removed:
            # a completed job holding units of a resource C2 changes: carried,
            # with the assumption said out loud (DL-256). A removed holder is
            # a ghost, and the opening gives its units back
            verdict = "A"
            assumption = HELD_ASSUMPTION
        elif removed:
            ghosts.append(name)
        elif changed:
            changed_not_live.append(name)
        verdicts.append(
            JobVerdict(job=name, tier=tier, verdict=verdict, changed=changed, assumption=assumption)
        )
    return Classification(
        verdicts=tuple(verdicts),
        ghosts=tuple(ghosts),
        changed_not_live=tuple(changed_not_live),
        changed_nodes=tuple(sorted(graph.changed)),
        readiness_flips=readiness_flips(closing, opening, carried, graph),
    )


# ---------------------------------------------------- the boundary-truth diff


class _TruthOracle(OracleReads):
    """A throwaway interpreter's reads for one question: does this job's `condition:` hold at
    the carried state?

    Condition truth is interpreter semantics (SEM-05, SEM-06, the lookback ladder), so this
    subclasses the oracle's reads instead of adding an evaluator. It applies no input and builds
    no job instance.
    """

    def at(self, now: datetime) -> None:
        """Read the conditions at `now`, as a time observation would."""
        self._now = now

    def condition_holds(self, job: str) -> bool:
        job_ir = self.catalog.jobs.get(job)
        attr = None if job_ir is None else job_ir.sem.condition
        return True if attr is None else self._cond_true(attr.cond, job)


def _eval_clock(carried: CarriedState) -> datetime:
    if carried.now is not None:
        return carried.now
    stamps = [row.status_at for row in carried.rows.values() if row.status_at is not None]
    return max(stamps) if stamps else datetime(1970, 1, 1)


def _seeded(
    catalog: CatalogIR, carried: CarriedState, profile: RuntimeProfile | None = None
) -> _TruthOracle:
    """An interpreter over `catalog` holding the carried rows.

    Rows are installed through `RuntimeState.seed_job`, which names no transition (DL-86). A
    terminal row's last end is its status time; a row that ended and started again keeps its
    earlier end, so an n() lookback keeps its anchor. A carried waiter gets a rank. Every
    carried row is seeded, a ghost's included (ss10.1), so an atom naming it reads the row.
    """
    # the period's own SEM-35 alias table (DL-151), so a `timezone:` only it resolves does not
    # raise, and its own semantic switches (DL-252), so each side's truth is its own reading
    oracle = _TruthOracle(catalog, **oracle_reading(profile))
    store = oracle.store
    store.begin_input()
    for name in sorted(carried.jobs):
        row = carried.jobs[name].row
        store.seed_job(
            name,
            status=row.status,
            status_at=row.status_at,
            last_end_at=row.status_at if row.status in TERMINAL else row.last_end_at,
            exit_code=row.exit_code,
            on_ice=row.on_ice,
            on_hold=row.on_hold,
            on_noexec=row.on_noexec,
            armed=row.armed,
        )
    for name, value in sorted(carried.globals_.items()):
        store.set_global(name, value)
    store.commit_input()
    oracle.at(_eval_clock(carried))
    return oracle


def readiness_flips(
    closing: Baseline,
    opening: Baseline,
    carried: CarriedState,
    graph: ClassificationGraph,
) -> tuple[ReadinessFlip, ...]:
    """Whose readiness flips because something changed (ss10.2, PR-44).

    Candidates come from the reverse closure of every changed node and are evaluated under both
    catalogs at the one carried state. A job only C2 has is not here: it has no C1 truth.
    """
    candidates: set[str] = set()
    for node in graph.changed:
        candidates |= {n for n in graph.reverse(node) if n.startswith(JOB)}
    names = sorted(
        name
        for name in (node[len(JOB) :] for node in candidates)
        if name in closing.catalog.jobs and name in opening.catalog.jobs
    )
    if not names:
        return ()
    before = _seeded(closing.catalog, carried, closing.profile)
    after = _seeded(opening.catalog, carried, opening.profile)
    return tuple(
        ReadinessFlip(job=name, before=was, after=now)
        for name, was, now in (
            (name, before.condition_holds(name), after.condition_holds(name)) for name in names
        )
        if was != now
    )
