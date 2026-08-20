"""Boundary classification: what a period change does to live work.

Normative spec: `docs/period-model.md` ss10 (ss10.1 the three tiers, ss10.2
the classification graph, ss10.3 the named cases, ss10.4 the armed latch).
Built by DL-131. Obligations PR-37, PR-37a, PR-38, PR-39, PR-39a and
PR-40 through PR-44. PR-39b is readiness's, not the classifier's -- a
`next_period` with a foreign `state_machine_version` is refused before
anything here runs (ss2.1, `test_period_identity.py`).

The question this module answers is asked once per boundary: **C1 is open
and holds live work; C2 is staged; may the boundary commit, and what must
the seal record about what it carried?** Three answers per job.

- **R** -- refuse. Something the job is EXECUTING against changed. The seal
  does not commit until the run is done or killed.
- **A** -- assumed, with a named sentence. The job holds latent intent (an
  armed latch, a QUE_WAIT rank, a live timer) and something it depends on
  changed. The intent survives the boundary deliberately (ss10.4), and the
  assumption rides in the seal so that a reader knows what was assumed.
- **carry** -- nothing live. The row crosses unchanged, and if its closure
  moved it is listed in the report so the change is visible.

**Pure analysis.** Nothing here reads a disk, a socket or a clock: the
inputs are two `(catalog, profile)` pairs and one carried snapshot, and the
same inputs always give the same verdicts byte for byte -- which is what
lets phase 2 recompute the map the sidecar commits (spec ss7). The seal
operation that calls this, the snapshot model it builds `CarriedState`
from, and ss10.4's runtime obligation are not here.

**Two directions, both computed** (ss10.2). The R gate asks "is anything
live job J depends on changed?" and reads J's FORWARD closure. The
boundary-truth diff asks "whose readiness flips because X changed?" and
reads X's REVERSE closure, then evaluates condition truth under C1 and C2
at the carried state. Neither substitutes for the other, and this module
returns both.

**The edges run from a job to what it depends on** -- the profile fields
included. A live CMD's forward closure reaches `profile:cmd_grace_us`, so a
C2 that changes only the kill ladder's grace cannot commit over a live run
and then kill it under a ladder the run never agreed to. The reversed
spelling ("field -> job") passes every other obligation and reaches no
profile field from any job (PR-37a), which is why the direction is stated
here and tested directly.

**One authority for condition truth.** The readiness diff evaluates
conditions through the interpreter itself (`_TruthOracle`, a throwaway
`Oracle` seeded with the carried rows), never through a second evaluator
written here: SEM-05's iced-predecessor rule, SEM-06's undefined-is-false
rule and the lookback ladder are semantics, and a classifier that
re-implemented them would answer a question the engine does not ask.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.autocal import CalendarRuleError, semantic_key, standard_rows
from dsl41.capacity import CapacityPool
from dsl41.conditions import GlobalAtom, iter_atoms
from dsl41.derive import BoxTree, derive_graph
from dsl41.equiv import canonical_cond
from dsl41.ir import CatalogIR, CondAttr, JobIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import TERMINAL, JobRuntime
from dsl41.period import RuntimeProfile, job_fingerprints

# ------------------------------------------------------------------- nodes
#
# ss10.2's node table, one prefix per kind. The prefix is not decoration: a
# verdict names the nodes that moved in a job's closure and an operator has
# to read that list, so `resource:FUEL` and a job called `FUEL` must not be
# the same string.

JOB: Final = "job:"
BOX: Final = "box:"  # a box's containment, at every nesting depth
GLOBAL: Final = "global:"
XINST: Final = "xinst:"
RESOURCE: Final = "resource:"
MACHINE: Final = "machine:"
CALENDAR: Final = "calendar:"
CYCLE: Final = "cycle:"
PROFILE: Final = "profile:"
#: the timezone basis is one node for the whole estate: `default_tz` plus
#: the alias table, the pair every scheduled tick resolves through
TZ_BASIS: Final = "tz:basis"

#: ss10.2's profile mapping, EXACTLY -- which jobs each field reaches.
#: `retry_horizon_us` is boundary policy and reaches no job: a field that
#: reached every job would turn a horizon tweak into a full live-work drain.
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


def is_scheduled(job_ir: JobIR) -> bool:
    """ss10.2's "every job with `start_times`, `start_mins` or a calendar" --
    the jobs whose ticks resolve through the timezone basis.

    "A calendar" includes `exclude_calendar`: exclusion membership is a
    LOCAL-day question, so the timezone decides which tick a date row
    blocks. A job with only an exclusion and no trigger never ticks at all
    -- the edge then over-approximates in the safe direction, which is the
    union rule this graph already follows."""
    schedule = job_ir.schedule
    if schedule is None:
        return False
    return bool(
        schedule.start_times
        or schedule.start_mins
        or schedule.run_calendar
        or schedule.exclude_calendar
    )


class Baseline(BaseModel):
    """One period's definition: the catalog it interprets and the launch
    options it interprets under -- C1 and C2 in ss10's language.

    Both halves, because half of them is not a period: identical JIL under
    two timezones is one `catalog_hash` and two sets of ticks (ss1.1), and a
    classifier given only the catalog would report nothing changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    catalog: CatalogIR
    profile: RuntimeProfile = RuntimeProfile()


# ------------------------------------------------------------ carried state


class CarriedJob(BaseModel):
    """One job's liveness at T, as ss10.1 keys on it.

    `row` is the authoritative `JobRuntime` the seal carries -- not a copy of
    three of its fields, because a second spelling of a carried row is a
    second authority for what the state IS. The four flags beside it are
    what the row cannot say: an execution's lifecycle lives in the seal's
    `executions` (ss3.5) and a timer lives in the timer heap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: JobRuntime = JobRuntime()
    #: ss3.5's three execution kinds, as booleans for this job's current run
    pending_spawn: bool = False
    bound: bool = False
    fw_watch: bool = False
    #: a NON-STALE authoritative timer is pending for this job. Staleness is
    #: the interpreter's rule, not this module's: `Oracle.pending_timers`
    #: already discards a heap entry a fire would drop, and
    #: `carried_from_oracle` reads that list rather than the raw heap.
    timer: bool = False

    @property
    def executes(self) -> bool:
        """The executing tier's job-local half (the box half needs the tree).

        `pending_spawn` counts (PR-39a). The oracle reaches RUNNING before
        the shell plans the SPAWN, so the row is already RUNNING and the two
        sets overlap anyway; and the effect carries no frozen command --
        `_apply_spawn` reads the CURRENT catalog at dispatch, so a pending
        SPAWN classified latent would execute C2's command under C1's run
        number and reservations."""
        return (
            self.row.status in ("RUNNING", "STARTING")
            or self.pending_spawn
            or self.bound
            or self.fw_watch
        )

    @property
    def latent(self) -> bool:
        """Latent intent: an armed latch (ss10.4), a QUE_WAIT rank, or a live
        timer. Read only where `executes` is false -- R beats A."""
        return self.row.armed or self.row.status == "QUE_WAIT" or self.timer


class CarriedState(BaseModel):
    """The snapshot the classifier reads: rows, globals, spent units and the
    instant the state is observed at.

    U6 builds this from a seal (`state.jobs`, `state.globals`,
    `state.consumed`, `state.now` plus `executions` and the timer heap);
    `carried_from_oracle` builds it from a live engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, CarriedJob] = {}
    globals_: dict[str, str] = {}
    #: SEM-16's irreversible depletion, per bucket key (`r:NAME`, `m:NAME`).
    #: Held units are on the rows; the two are added only where ss5 adds them.
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
    """Build a `CarriedState` from a live interpreter -- the convenience the
    tests and the live sealer use.

    The three execution sets are passed in rather than discovered, because
    the evidence for them is not the oracle's: a pending SPAWN is an outbox
    entry, a bound run is the supervisor's spool binding, and an FW watch is
    `runs/<job>.<n>/watch.jsonl` (ss3.5). Keeping them parameters is what
    lets this module stay a pure analysis pass -- it imports no runner.

    `timers` defaults to the jobs `Oracle.pending_timers` reports, which is
    the interpreter's own liveness rule for a heap entry."""
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
    """The containment RELATION under `box`: every (member, its box) pair at
    any nesting depth, sorted. This is the box node's value.

    The pairs, not the member set: ss10.2 says containment moves when
    `box_name` moves "at any nesting depth", and moving a leaf from an inner
    box up to the outer one leaves the outer box's member set identical
    while changing exactly what the rule is about. The set alone would call
    that no change at all."""
    out: set[tuple[str, str]] = set()
    stack = [(member, box) for member in tree.children.get(box, ())]
    while stack:
        pair = stack.pop()
        if pair in out:
            continue  # the catalog validator rules out cycles; be safe anyway
        out.add(pair)
        stack.extend((member, pair[0]) for member in tree.children.get(pair[0], ()))
    return tuple(sorted(out))


def _trimmed(value: str | None) -> str | None:
    """An attribute as the node value reads it: surrounding whitespace is
    not a change."""
    return None if value is None else value.strip()


def _named_ref(attrs: Mapping[str, str], key: str) -> str | None:
    """The record `attrs[key]` NAMES, or None.

    Attribute values are verbatim JIL and a calendar reference is often
    quoted (`cyccal: "q1"`), so the quotes come off here. ONE wrapping pair
    of DOUBLE quotes, which is what `ir._unquote` and `autocal._unquote` --
    the latter resolving this same `holcal` attribute -- both do; a third
    rule that also stripped `'...'` would make one JIL spelling mean two
    things. `ir._unquote` is private and DL-75's gate forbids a new
    cross-module private import, so this stays local rather than a sixth
    import site nobody may add."""
    raw = attrs.get(key, "").strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return raw or None


def _cycle_periods(periods: Iterable[Any]) -> tuple[Any, ...]:
    """Cycle periods as autocal PARSES them (`%m/%d/%Y`, which accepts an
    unpadded month or day) -- `1/1/2026` and `01/01/2026` are one date.
    Order is PRESERVED: period position numbers the chunks cycle-scoped
    tokens count. An unparseable pair keeps its raw spelling -- the loud
    refusal is autocal's."""
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
    """The calendar's rows, normalized as `autocal.standard_rows` resolves
    them: day -> tick set, sorted -- reordering and duplicating rows is
    spelling. A row set the resolver refuses falls back to the sorted raw
    rows: the classifier must not crash where preflight is the loud gate."""
    try:
        rows = standard_rows(calendar)
    except (ValueError, CalendarRuleError):
        return tuple(sorted(calendar.dates))
    return tuple((day.isoformat(), tuple(sorted(ticks))) for day, ticks in sorted(rows.items()))


def _capacity(units: Callable[[], int | None]) -> int | None:
    """`ResourceIR.capacity_units` with a malformed `amount` read as absent.
    A malformed value is preflight's loud refusal (DL-50), not a crash in
    the classifier -- and "unsized" is the same node value either way.

    Typed rather than asserted: an `assert` vanishes under `-O`, so a check
    that carries a guarantee must not be one (`oracle._schedule_timer`)."""
    try:
        return units()
    except ValueError:
        return None


def _node_values(side: Baseline) -> dict[str, Any]:
    """Every node's value under one baseline, as ss10.2's "changed when"
    column defines it. A node absent from a side has no entry, so a job,
    resource or box that C2 removes changes by comparing against `None`."""
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
        # amount, res_type, and the release-policy default -- which IS
        # res_type (`capacity._release_policy`: D depletes, anything else
        # renews), so the pair says all three things ss10.2 names
        values[RESOURCE + name] = (
            _capacity(resource.capacity_units),
            # exactly as `capacity._release_policy` reads it: stripped and
            # upper-cased, so `r` and `R` are one renewable policy
            (resource.res_type or "").strip().upper() or None,
        )
    for name, machine in catalog.machines.items():
        # "the fields resolution actually reads": the load bucket's size,
        # the type that decides whether there is one, the node the runner
        # dispatches to, and the membership
        values[MACHINE + name] = (
            # each field as RESOLUTION reads it, so a respelling that
            # resolves identically is not a change: type case-folds
            # (preflight compares `.lower()`), node_name unquotes (a
            # hostname never carries quotes), max_load parses (08 is 8; a
            # malformed value is preflight's loud refusal and one node
            # value here)
            (machine.machine_type or "").lower() or None,
            _capacity(machine.max_load_units),
            _named_ref(machine.attrs, "node_name"),
            # SORTED: resolution folds the pool any-of (`runner_preflight`),
            # so member order is spelling, not semantics
            tuple(sorted({member.name for member in machine.members})),
        )
    for name, calendar in catalog.calendars.items():
        # the referenced DATE SET, as the RESOLVER reads it. A standard
        # calendar compares as `autocal.standard_rows` -- so a reordered or
        # duplicated row is spelling, not schedule. An extended one compares
        # as what the rule engine derives dates from: its conditions and the
        # six attributes autocal reads; descriptive keys are not a change.
        # the whole extended surface canonicalizes through the rule
        # engine's OWN parsers (`autocal.semantic_key`) -- one authority, so
        # two spellings this engine derives identical dates from are one
        # node value; standard rows normalize the same way through
        # `standard_rows`
        values[CALENDAR + name] = (
            calendar.kind,
            _calendar_dates(calendar),
            semantic_key(calendar, catalog),
        )
    for name, cycle in catalog.cycles.items():
        # autocal's `_period_of` walks the PERIODS and nothing else: the
        # attrs map is descriptive, and a description edit on a cycle must
        # not refuse a live schedule. The dates compare PARSED (see
        # `_cycle_periods`), in order -- position numbers the chunks.
        values[CYCLE + name] = _cycle_periods(cycle.periods)
    values[TZ_BASIS] = (profile.default_tz, tuple(sorted(profile.tz_aliases.items())))
    for field in RuntimeProfile.model_fields:
        raw = getattr(profile, field)
        values[PROFILE + field] = tuple(sorted(raw.items())) if isinstance(raw, dict) else raw
    return values


class ClassificationGraph:
    """ss10.2's graph over both baselines: the edges of C1 and C2 together,
    and the set of nodes that moved between them.

    Both edge sets, because a job's dependencies are not the same on the two
    sides and the closure must see either: a job whose C2 `machine:` is new
    depends on the new machine, and a job C2 removes still depended on what
    C1 gave it. A union over-approximates in the safe direction -- it can
    only add a changed node to a closure, never hide one."""

    def __init__(self, closing: Baseline, opening: Baseline) -> None:
        self._deps: dict[str, set[str]] = {}
        self._rdeps: dict[str, set[str]] = {}
        for side in (closing, opening):
            self._add_side(side)
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
        # (1) condition atoms, walked DIRECTLY off each job's condition --
        # not off IR-G's edge list, deliberately: IR-G diverts a local
        # unqualified n() atom into `mutex_groups` (M07) and keeps no edge
        # for it, so a classifier reading its edges would carry a boundary
        # over `b: condition: n(a)` while `b` executes and C2 changes `a`.
        # The spec's "(IR-G, reversed)" names the ATOMS; the walk is the
        # atoms, every one of them. A `name^INST` atom depends on the
        # boundary declaration -- the node whose change this catalog can
        # see; the foreign job's own definition is not in this estate.
        for name, job_ir in catalog.jobs.items():
            # EVERY condition the job carries -- `condition`, and a box's
            # `box_success`/`box_failure` (SEM-12) -- through the canonical
            # walker the linter shares. A box gated by s(a) depends on `a`
            # exactly as a start condition would.
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
        # (2) box containment, both directions and nested. A member depends
        # on TWO facts about its box -- the containment set (`box:B`, which
        # moves when any `box_name` at any depth moves) and the box's own
        # definition (`job:B`, whose condition and schedule gate the member)
        # -- and the box depends on every member. That is what makes a box's
        # forward closure reach its members (PR-42) and a member's reach its
        # siblings -- no box run may observe two versions of anything in it.
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
            # (6) the timezone basis, for scheduled jobs only
            if is_scheduled(job_ir):
                self._edge(node, TZ_BASIS)
            # (7) the runtime-profile fields this job's KIND reads
            for field in self._profile_fields(job_ir):
                self._edge(node, PROFILE + field)
        for name, machine in catalog.machines.items():
            for component in machine.members:
                self._edge(MACHINE + name, MACHINE + component.name)
        for name, calendar in catalog.calendars.items():
            holcal = _named_ref(calendar.attrs, "holcal")
            if holcal is not None:
                self._edge(CALENDAR + name, CALENDAR + holcal)
            cyccal = _named_ref(calendar.attrs, "cyccal")
            if cyccal is not None:
                self._edge(CALENDAR + name, CYCLE + cyccal)

    @staticmethod
    def _profile_fields(job_ir: JobIR) -> tuple[str, ...]:
        fields: tuple[str, ...] = ()
        if is_scheduled(job_ir):
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
        """What `node` depends on, transitively, `node` included -- the R
        gate's question."""
        return self._walk(node, self._deps)

    def reverse(self, node: str) -> frozenset[str]:
        """What depends on `node`, transitively, `node` included -- the
        boundary-truth diff's question."""
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


class JobVerdict(BaseModel):
    """One job's answer. `verdict` and `assumption` are the two fields the
    seal's `classification` map carries per job (ss3.1); `tier` and
    `changed` are why, and they stay in the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    tier: Tier
    verdict: Verdict
    #: the changed nodes in this job's forward closure, sorted
    changed: tuple[str, ...] = ()
    #: the named A sentence. Non-null exactly when `verdict == "A"`.
    assumption: str | None = None


class ReadinessFlip(BaseModel):
    """One job whose start condition evaluates differently under C2 than
    under C1 at the SAME carried state -- the boundary-truth diff (PR-44)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job: str
    before: bool
    after: bool


class Classification(BaseModel):
    """The whole answer: a verdict per job, the two report lists ss10.1
    requires, the changed node set, and the readiness diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: tuple[JobVerdict, ...] = ()
    #: removed AND not live: retained, listed, and referenced by no
    #: condition (L001 refuses that separately)
    ghosts: tuple[str, ...] = ()
    #: not live, and something in its closure moved -- named so that
    #: "nothing was live" is not read as "nothing changed". Holds a job C2
    #: ADDS too: its own node moved from absent, and a new job is exactly
    #: what an operator reading this list wants told. Disjoint from
    #: `ghosts`, which is the same statement about a job C2 dropped.
    changed_not_live: tuple[str, ...] = ()
    changed_nodes: tuple[str, ...] = ()
    readiness_flips: tuple[ReadinessFlip, ...] = ()

    @property
    def by_job(self) -> dict[str, JobVerdict]:
        return {verdict.job: verdict for verdict in self.verdicts}

    @property
    def refused(self) -> tuple[str, ...]:
        """The jobs that make the boundary refuse -- the R gate's output."""
        return tuple(v.job for v in self.verdicts if v.verdict == "R")


def _tiers(catalog: CatalogIR, carried: CarriedState, names: Iterable[str]) -> dict[str, Tier]:
    """Each job's tier, with the box half of the executing rule resolved
    against C1's tree: a member of an executing box is executing whatever
    its own row says, at any nesting depth (E19)."""
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
    """The resource nodes C2 lowers below the carried `consumed + held`
    (ss10.3). `CapacityPool.used` is the one place those two facts are added
    (ss5), so it is asked rather than re-derived here."""
    pool = CapacityPool(opening.catalog)
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
    """The A sentence for one latent job: ss10.3's named cases in order,
    then the general one. Order is by what an operator most needs told --
    the latch that will fire first, then the admission it will wait on, then
    the flags it will keep. Each case is NARROW, so two of them rarely both
    fire and the order rarely decides anything."""
    row = carried.jobs.get(name, CarriedJob()).row
    before = closing.catalog.jobs.get(name)
    after = opening.catalog.jobs.get(name)
    if row.armed and before is not None and after is not None and _trigger_moved(before, after):
        return ARMED_ASSUMPTION
    if short & set(changed):
        return RESOURCE_ASSUMPTION
    if before is not None and after is not None:
        initial = after.sem.initial_status
        if before.sem.initial_status != initial and not _row_agrees(row, initial):
            return INITIAL_STATUS_ASSUMPTION
    return LATENT_ASSUMPTION


def _trigger_moved(before: JobIR, after: JobIR) -> bool:
    """Whether an armed latch's own trigger moved -- ss10.3 names exactly two
    things: the schedule and the condition.

    The condition is compared through `equiv.canonical_cond`, which is the
    project's answer to "is this the same condition": it erases spans and
    parentheses and sorts operands, so a reordered `a & b` is not reported to
    an operator as a changed gate."""
    if before.schedule != after.schedule:
        return True
    return _cond_key(before.sem.condition) != _cond_key(after.sem.condition)


def _cond_key(attr: CondAttr | None) -> Any:
    return None if attr is None else canonical_cond(attr.cond).model_dump(mode="json")


def _row_agrees(row: JobRuntime, initial: str | None) -> bool:
    """Whether the carried row already holds what C2's `initial_status`
    would seed. Genesis seeding writes NEW rows only, so a disagreement is
    an assumption and never a silent rewrite.

    The WHOLE three-flag vector is compared, not the one flag C2 names: a
    row carrying HOLD+ICE against a seed of ICE alone still disagrees --
    the retained HOLD is exactly what the operator is being told about."""
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
    graph: ClassificationGraph | None = None,
) -> Classification:
    """ss10's verdict over one boundary: C1 (`closing`) against C2
    (`opening`) at the carried state.

    Deterministic and total: every job of either catalog AND every carried
    row gets exactly one verdict, in name order, so the map phase 2 commits
    into the seal is reproducible byte for byte by audit. The carried rows
    are in that union because a ghost is retained: at the next boundary it
    is in neither catalog, and a classifier reading the catalogs alone would
    stop listing it while it was still there."""
    if graph is None:
        graph = ClassificationGraph(closing, opening)
    names = sorted(set(closing.catalog.jobs) | set(opening.catalog.jobs) | set(carried.jobs))
    tiers = _tiers(closing.catalog, carried, names)
    short = _oversubscribed(opening, carried)
    verdicts: list[JobVerdict] = []
    ghosts: list[str] = []
    changed_not_live: list[str] = []
    for name in names:
        tier = tiers[name]
        changed = graph.moved(JOB + name)
        #: "removed" is about what C2 can dispatch, so it reads the OPENING
        #: catalog alone: a job C1 dropped and a job dropped two periods ago
        #: are the same fact to a row that is still live under it
        removed = name not in opening.catalog.jobs
        verdict: Verdict = "carry"
        assumption: str | None = None
        if tier == "executing" and (removed or changed):
            # a removed job is not in `dispatchable`, so a KILL for it plans
            # no effect and KILLJOB would stop nothing -- there is no way to
            # end the run C1 started, and the boundary must not open over it
            verdict = "R"
        elif tier == "latent" and removed:
            verdict = "R"  # PR-40: the latch has nothing left to start
        elif tier == "latent" and changed:
            verdict = "A"
            assumption = _assumption(
                name, changed, closing=closing, opening=opening, carried=carried, short=short
            )
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


class _TruthOracle(Oracle):
    """A throwaway interpreter used for one question: does this job's
    `condition:` hold at the carried state?

    A subclass rather than a second evaluator, and rather than a public
    query on the interpreter itself: the classifier is the only caller that
    ever asks about a state it did not produce, and condition truth is
    interpreter semantics (SEM-05's iced predecessor, SEM-06's undefined
    atom, the lookback ladder). One authority, borrowed."""

    def condition_holds(self, job: str) -> bool:
        job_ir = self.catalog.jobs.get(job)
        attr = None if job_ir is None else job_ir.sem.condition
        return True if attr is None else self._cond_true(attr.cond, job)


def _eval_clock(carried: CarriedState) -> datetime:
    if carried.now is not None:
        return carried.now
    stamps = [row.status_at for row in carried.rows.values() if row.status_at is not None]
    return max(stamps) if stamps else datetime(1970, 1, 1)


def _seeded(catalog: CatalogIR, carried: CarriedState) -> _TruthOracle:
    """An interpreter over `catalog` holding the carried rows.

    Seeded through `RuntimeState`'s verbs, which are the only write path
    (DL-86). Two of them are ordering, not ceremony: a `last_end_at` latches
    on a TERMINAL edge only, so a row that ended and started again replays
    its end first or an n() lookback loses its anchor; and a rank is held
    exactly while QUE_WAIT, so a carried waiter is enqueued or the input
    refuses to close.

    Every carried row is seeded, including a ghost's -- the row is retained
    across the boundary (ss10.1), so an atom naming it must read what the
    row says. L001 refuses a condition naming a job the catalog does not
    have, so a valid estate never asks."""
    oracle = _TruthOracle(catalog)
    store = oracle.store
    store.begin_input()
    for name in sorted(carried.jobs):
        row = carried.jobs[name].row
        store.set_flags(name, on_ice=row.on_ice, on_hold=row.on_hold, on_noexec=row.on_noexec)
        if row.last_end_at is not None and row.status not in TERMINAL:
            store.transition(name, "SUCCESS", row.last_end_at)
        if row.status == "QUE_WAIT":
            store.enqueue_waiter(name)
        store.transition(name, row.status, row.status_at, row.exit_code)
        if row.armed:
            store.set_armed(name, True)
    for name, value in sorted(carried.globals_.items()):
        store.set_global(name, value)
    store.commit_input()
    oracle.advance(_eval_clock(carried))
    return oracle


def readiness_flips(
    closing: Baseline,
    opening: Baseline,
    carried: CarriedState,
    graph: ClassificationGraph,
) -> tuple[ReadinessFlip, ...]:
    """Whose readiness flips because something changed (ss10.2, PR-44).

    The candidates come from the REVERSE closure of every changed node --
    "who depends on X" -- and each is then evaluated under both catalogs at
    the one carried state. A job only C2 has is not here: it has no C1 truth
    to differ from, and genesis seeding gives it its row when C2 opens."""
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
    before = _seeded(closing.catalog, carried)
    after = _seeded(opening.catalog, carried)
    return tuple(
        ReadinessFlip(job=name, before=was, after=now)
        for name, was, now in (
            (name, before.condition_holds(name), after.condition_holds(name)) for name in names
        )
        if was != now
    )
