"""The oracle's state and the vocabulary of the events that move it (DL-91).

`oracle.py` imports this module; nothing here imports `oracle.py`.

- The vocabulary: `JobStatus`, `TERMINAL`, `EventKind`, `Event`, `TraceEntry`. `Event` is the
  oracle's input alphabet and, with `source`, its provenance (DL-68).
- The rows: `JobRuntime` and `GlobalRuntime`, both frozen (DL-86), and the semantic projection
  that decides when a revision moves. `HostRuntime` is the dormant routing row: the store keeps
  an empty table of it (`RuntimeState._hosts`) and no tier writes or reads one.
- The owner: `RuntimeState` holds private maps, typed verbs, the timer heap with its ordering
  token, and the input transaction that gives one committed input one revision per changed
  entity (concurrency-model ss3).
- `OracleError`, defined here because it has no dependencies.
- The machines the owner holds: `runtime_assembly` declares assembly and the open input
  (docs/state-machines.md), and the `job` chart (`job_chart`) declares every move of a job row. The oracle drives the chart and writes what it reached
  through the verbs here.

`InputBatch` stays with `Oracle`. `_N_FALSE_STATUSES` (SEM-02's n() rule) is interpretation, not state.
"""

from __future__ import annotations

import copy
import heapq

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from dsl41.ir import ResourceIR
from dsl41.scan_chart import IDLE as SCAN_IDLE
from dsl41.scan_chart import QUEUE_SCAN, SCAN_KEY
from dsl41.state_machine import (
    Executor,
    Outcome,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
)
from dsl41.state_machine import Event as ChartEvent


class OracleError(ValueError):
    pass


class CascadeDepthError(OracleError, RecursionError):
    """An instant cascade that did not end.

    The hook stops a root dispatch at the item cap for the estate's job count (`jobs` given,
    DL-351); the message names the cap, the job count and the root item. A deep condition can
    pass the recursion limit instead (`jobs` None); the message then names the RecursionError
    and the root's job. The same input raises it again on every replay.
    """

    def __init__(self, job: str, cause: str, limit: int, *, jobs: int | None = None) -> None:
        if jobs is None:
            message = (
                f"RecursionError: an instant cascade passed the recursion limit of {limit};"
                f" its first start was {job} ({cause})"
            )
        else:
            message = (
                f"an instant cascade reached the item cap of {limit} for an estate of"
                f" {jobs} jobs; its root item was {cause} on {job}"
            )
        super().__init__(message)
        self.job = job


JobStatus = Literal[
    "INACTIVE",
    "QUE_WAIT",
    "STARTING",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
]

#: Every job status: the leaves of the `job` chart's status region.
JOB_STATUSES: frozenset[JobStatus] = frozenset(get_args(JobStatus))

#: The statuses an injected STATUS event may carry: every `JobStatus` except QUE_WAIT, which the
#: capacity owner assigns and clears (DL-50, DL-264). The oracle's STATUS handler and the control
#: server's CHANGE_STATUS check share this set.
INJECTABLE_STATUSES: frozenset[JobStatus] = JOB_STATUSES - {"QUE_WAIT"}

TERMINAL: frozenset[JobStatus] = frozenset({"SUCCESS", "FAILURE", "TERMINATED"})
#: The terminal statuses that are not SUCCESS: a failed vote in the SEM-11 fold and the SEM-15
#: recompute, the box end that cascades to job_terminator members (SEM-14), the FORCE_STARTJOB
#: that starts on held units (DL-256), and the CLI's `is-failed` predicate.
FAILED: frozenset[JobStatus] = frozenset({"FAILURE", "TERMINATED"})
# A run's reservations are held while a row is in LIVE (period-model ss5) and released on
# leaving it, except the units a renewable's policy does not free (DL-256, `may_outlive_run`).
# The release edge (DL-120), the SPAWN edge (DL-232) and the completion gate (DL-235) read it.
LIVE: frozenset[JobStatus] = frozenset({"STARTING", "RUNNING"})
#: A job with a run in progress or a place in the queue.
LIVE_OR_QUEUED: frozenset[JobStatus] = LIVE | {"QUE_WAIT"}
#: A job that may start: INACTIVE or terminal.
IDLE: frozenset[JobStatus] = frozenset({"INACTIVE", "SUCCESS", "FAILURE", "TERMINATED"})

EventKind = Literal[
    "STATUS",
    "STARTJOB",
    "FORCE_STARTJOB",
    "SET_GLOBAL",
    "ON_ICE",
    "OFF_ICE",
    "ON_HOLD",
    "OFF_HOLD",
    "ON_NOEXEC",
    "OFF_NOEXEC",
    "DISARM",
    "RELEASE_RESOURCE",
    "KILLJOB",
    "TIMER",
    "MUST_START_ALARM",
    "MUST_COMPLETE_ALARM",
]


#: The engine's ss7 input alphabet: where an externally injected event came from. `adapter` is
#: the stamp that makes an event a COMPLETION subject to the ss4 stale gate. Stamps live in call
#: keywords and parameter defaults (DL-209).
EventSource = Literal["scheduler", "control", "adapter", "reconcile"]


class Event(BaseModel):
    at: datetime
    kind: EventKind
    payload: dict[str, object] = {}
    #: Provenance of an externally injected event (ss7); None for oracle-internal and script
    #: events. Start causes surface it (DL-68). It is `str`, not `EventSource`: the field is
    #: persisted and `docs/protocol-evolution.md` holds it to its own schema, so a journal naming
    #: an unknown provenance still replays.
    source: str | None = None

    def job(self) -> str | None:
        job = self.payload.get("job")
        return job if isinstance(job, str) else None


class TraceEntry(BaseModel):
    at: datetime
    job: str
    transition: str  # "OLD->NEW" or an out-of-band marker like "ON_ICE"
    cause: str


#: DL-50's three release policies: `CapacityReservation` stores one, `capacity.py` derives it.
ReleasePolicy = Literal["completion", "success", "never"]


class CapacityReservation(BaseModel):
    """One bucket's units, held by one `(job, run_number)` (DL-120,
    period-model ss5). FROZEN, for `JobRuntime`'s reason: it rides on that row
    and a change to it is a replacement of the row.

    The vector is frozen at acquisition and never recomputed from the current
    catalog: a re-baseline that raises the job's QUANTITY must still release
    what the live run actually took (PR-20)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: `m:<machine>` (max_load) or `r:<name>` (resource amount), as `CapacityPool` keys them.
    bucket: str
    units: int = Field(gt=0)
    #: DL-50: when the units go back. `never` and an unmet `success` do not free them: a
    #: depletable's are spent (SEM-16), a renewable's stay held by the job (DL-256).
    release_policy: ReleasePolicy


def unreleased(
    reservations: Sequence[CapacityReservation],
    success: bool,
    keeps_held: Callable[[str], bool] | None,
) -> tuple[tuple[CapacityReservation, ...], tuple[CapacityReservation, ...]]:
    """The units a run's end does not free, as (kept, spent) (DL-120, DL-256).

    Every end frees a `completion` unit and a SUCCESS a `success` unit. The rest stays held where
    `keeps_held(bucket)` says so and is spent otherwise; with no `keeps_held`, all of it is
    spent. `RuntimeState.release_reservations` and `RowUnits.release` apply it (DL-356).
    """
    kept: list[CapacityReservation] = []
    spent: list[CapacityReservation] = []
    for reservation in reservations:
        policy = reservation.release_policy
        if policy == "completion" or (policy == "success" and success):
            continue
        if keeps_held is not None and keeps_held(reservation.bucket):
            kept.append(reservation)
        else:
            spent.append(reservation)
    return tuple(kept), tuple(spent)


#: The bucket prefix of a named resource; `m:` is a machine's load.
RESOURCE_BUCKET = "r:"


def may_outlive_run(reservation: CapacityReservation) -> bool:
    """DL-256: whether this reservation may stay on a row that is not live.

    Only a named resource's units under a policy that does not free on every completion can.
    The seal loader and the store's invariant check share this rule.
    """
    return (
        reservation.bucket.startswith(RESOURCE_BUCKET)
        and reservation.release_policy != "completion"
    )


class JobRuntime(BaseModel):
    """One job entity's authoritative runtime row. FROZEN (DL-86): a change
    is a REPLACEMENT, so "this entity changed" is one observable act rather
    than a field write nobody watched.

    `extra="forbid"` keeps the DL-82 typo guard alive across that change: the
    old store wrote fields with setattr, which raises on an undeclared name,
    where rebuilding a row from a dict would DEFAULT to dropping one in
    silence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: JobStatus = "INACTIVE"
    status_at: datetime | None = None
    last_end_at: datetime | None = None  # last transition into a terminal status (Q2 anchor)
    exit_code: int | None = None
    run_number: int = 0
    on_ice: bool = False
    on_hold: bool = False
    on_noexec: bool = False
    armed: bool = False  # a scheduled tick latched at a releasable gate (Q3, DL-54)
    started_by: str | None = None  # trace cause of the most recent actual start (DL-68)
    #: The period this run started in (period-model ss3.5, DL-132), set with run_number at the
    #: actual start (PR-50). 1 for every row a pre-period build wrote.
    start_period: int = 1
    #: SEM-10 at-most-once bookkeeping: the members already run in this box's current execution.
    #: Only a BOX row carries entries; `start_run` resets it.
    ran_members: frozenset[str] = frozenset()
    #: Members whose INACTIVE in this box execution is an explicit verdict: a run_window skip
    #: (SEM-33, DL-154) or an injected STATUS INACTIVE (SEM-11, DL-242). The SEM-11 fold
    #: completes past a resolved member that is not live. The member's own start voids the mark.
    #: Only a BOX row carries entries; `start_run` resets it. The name is carried by sealed
    #: periods and their attestations.
    window_skipped_members: frozenset[str] = frozenset()
    #: Members taken off ice in this box execution before they ran in it, under
    #: `off-ice-in-running-box=next-run` (SEM-20). The SEM-11 fold skips them while they are not
    #: live or queued, and a plain start is refused until the box's next run. FORCE_STARTJOB
    #: starts the member anyway and voids the mark; an attempt that leaves the queue unstarted
    #: keeps it. Only a BOX row carries entries; `start_run` resets it.
    iced_out_members: frozenset[str] = frozenset()
    #: DL-120: the capacity vector this run acquired, held while STARTING or RUNNING. After the
    #: run the row keeps only a renewable's units its policy did not free (DL-256,
    #: `may_outlive_run`), until RELEASE_RESOURCE or the job's next run (period-model ss5).
    reservations: tuple[CapacityReservation, ...] = ()
    #: DL-120: this job's rank in the QUE_WAIT queue, non-null iff QUE_WAIT. Allocated from
    #: `RuntimeState.enqueue_counter`; it keeps admission order across a boundary (PR-21).
    waiter_seq: int | None = None
    #: DL-87: this entity's optimistic-locking revision. Incremented at most once per committed
    #: input, and only when the semantic projection changed. It is excluded from that projection
    #: (concurrency-model ss3).
    state_rev: int = 0


class GlobalRuntime(BaseModel):
    """One global entity's row (SEM-06 latching semantics). A row rather than
    a bare string because the concurrency model gives globals their own
    identity and their own `state_rev` (concurrency-model ss2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    state_rev: int = 0


#: concurrency-model ss8's four routing states. `quarantined` is the leader's own, set on
#: unreachability (ss7); the other three are the operator's.
HostState = Literal["active", "passive", "quarantined", "evicted"]


class HostRuntime(BaseModel):
    """One execution host's routing row (concurrency-model ss8), dormant: one engine runs every
    job on its own host, so the store's table of these stays empty and no tier reads it. The
    oracle never reads this row (DL-93)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: HostState = "active"
    #: ss8's eviction fence. Eviction bumps it; a returning relay presenting a stale one is
    #: refused registration and must self-fence first (CM-12, stage S5d).
    generation: int = 0
    #: ss8's deadman interval, in seconds, opt-in per run root so that a supervisor can tolerate
    #: an absent controller (DL-79). None means no deadman: the host is reroutable only by force.
    deadman_s: float | None = None
    #: When the leader last had positive contact with this host: stamped at registration and
    #: kept fresh by the S5b deadman's traffic. It is the only clock in ss8's eviction bound.
    last_contact: datetime | None = None
    #: Non-null iff the current `evicted` state was reached by `--force`; carries the actor.
    #: ss8 requires such a reroute to be loud, durable and attributable.
    forced_by: str | None = None
    #: What quarantine interrupted, so that clearing it restores the operator's intent (DL-97):
    #: a drained host stays drained. Non-null only while `state` is `quarantined`.
    state_before_quarantine: HostState | None = None
    #: DL-94: this entity's revision, on the same rule as the rows above.
    state_rev: int = 0


#: The fields whose change moves an entity's revision: every model field except these, so a
#: field added later is projected by default (concurrency-model ss3, DL-83).
#:
#: `state_rev` is the only exclusion the models carry. Effect and disk state (`watching`, log
#: locations, catalog metadata, `spec_drift`) lives runner-side.
_UNPROJECTED: frozenset[str] = frozenset({"state_rev"})
_PROJECTED_JOB_FIELDS: tuple[str, ...] = tuple(
    name for name in JobRuntime.model_fields if name not in _UNPROJECTED
)
_DEFAULT_JOB = JobRuntime()


def _timer_order(entry: tuple[datetime, int, Event] | tuple[datetime, int]) -> tuple[datetime, int]:
    """ss3.2's timer order, spelled once (DL-145, DL-143): `(due, token)` and nothing after it."""
    return (entry[0], entry[1])


class CarriedRows(BaseModel):
    """What a seal carries into the next period, as this module's own types
    (period-model ss3.3, ss7 phase 3 step 3).

    `seal.py` holds the ARTIFACT; this is the same facts in the shapes the
    owner can install. It is a separate model rather than the seal's
    `SealedState` because the artifact tier imports the classifier, the
    classifier imports the interpreter, and the interpreter importing the
    artifact would close that ring -- so the boundary translates once, at
    the seam, and the owner never learns what a sidecar is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: dict[str, JobRuntime] = {}
    globals_: dict[str, GlobalRuntime] = {}
    #: ss3.2's order: `(due, token)`, never heap-array layout.
    timers: tuple[tuple[datetime, int, Event], ...] = ()
    timer_seq: int = 0
    consumed: dict[str, int] = {}
    enqueue_counter: int = 0
    #: the rows updates removed, by job name (`RuntimeState.retire_job`)
    tombstones: dict[str, JobRuntime] = {}
    #: an update swapped the catalog and no input has run since: the next one scans the queue
    #: first (`Oracle._wake_after_swap`)
    wake_owed: bool = False
    #: the definitions of the resources an update dropped while a run holds their units or
    #: `consumed` has a bucket of them (`Oracle.ghost_resources`): the pool keeps their type
    ghosts: dict[str, ResourceIR] = {}
    period_id: int = 1
    #: T, the instant the seal was taken at. Feed times are non-decreasing across the boundary,
    #: so the opened interpreter starts from T.
    now: datetime | None = None


# ------------------------------------------------------------- the job's flags
#
# A job row has four flags. Ice, hold and noexec are orthogonal regions of the `job` chart
# (`job_chart`); the scheduled-tick arm is a fact its rows read (DL-365). The chart decides every
# move; `RuntimeState.move_flag` writes the value it reached.

#: One flag's value, as the `job` chart names it.
type FlagState = Literal[
    "ice_off", "ice_on", "hold_off", "hold_on", "noexec_off", "noexec_on", "arm_off", "arm_on"
]
#: Each flag state's row field and value.
FLAG_FIELDS: Final[Mapping[FlagState, tuple[str, bool]]] = MappingProxyType(
    {
        "ice_off": ("on_ice", False),
        "ice_on": ("on_ice", True),
        "hold_off": ("on_hold", False),
        "hold_on": ("on_hold", True),
        "noexec_off": ("on_noexec", False),
        "noexec_on": ("on_noexec", True),
        "arm_off": ("armed", False),
        "arm_on": ("armed", True),
    }
)

# --------------------------------------------------------------- assembly
#
# `RUNTIME_ASSEMBLY` is a statechart (docs/statechart-engine.md). The state owner holds its one
# instance, `ASSEMBLY_KEY`, on `RuntimeState.machines`; its configuration is the assembly phase
# and `_phase` reads its leaf. The rows have no actions: the tier's work follows the microstep.
#
# The phases group by what the verbs read (DL-373): `unbuilt` (construction can still happen,
# no input open), `open` (an input open) and `built` (construction can no longer happen, ended
# by finish_genesis or overtaken by the seed, and no input open). `_touch` snapshots while
# `open` is active.
#
# A verb the phase does not allow is a refusal row naming why. These moves are not inputs, so
# the verb raises (`_ASSEMBLY_REFUSALS`), as an anchor move does (DL-224). A phase-dependent
# refusal is read before any tier work; the one rule that reads a fact of the call, a period
# below one, is the verb's own check. A cell with no row is a gap: a `StatechartError`, which
# stops the tier (statechart-engine ss5).

type AssemblyPhase = Literal[
    "fresh", "installed", "genesis_input", "genesis", "constructed", "seeded", "input", "live"
]
#: The phases: the chart's leaves. The configuration also carries the group above the leaf.
ASSEMBLY_PHASES: Final[frozenset[AssemblyPhase]] = frozenset(get_args(AssemblyPhase.__value__))
#: The key of the assembly instance on the state owner's executor.
ASSEMBLY_KEY: Final = "assembly"

#: The events of `runtime_assembly`: the state owner's verbs.
INSTALL: Final = "install"
BEGIN_INPUT: Final = "begin_input"
COMMIT_INPUT: Final = "commit_input"
FINISH_GENESIS: Final = "finish_genesis"
SEED_PERIOD: Final = "seed_period"

#: What the tier raises for each refusal code: the class and the text. `{phase}` is the phase
#: the verb met. A code with no entry is a disagreement between chart and tier.
_ASSEMBLY_REFUSALS: Final[dict[str, tuple[type[ValueError], str]]] = {
    "install_on_used_state": (
        ValueError,
        "install on a used state: carried rows are assembly's first act, and a"
        " live state advances through its own inputs alone (period-model ss7)",
    ),
    "input_already_open": (OracleError, "input already open: inputs do not nest"),
    "no_input_open": (
        OracleError,
        "commit_input in assembly phase {phase}: {phase} is not a source of runtime_assembly.07",
    ),
    "inside_genesis_input": (
        OracleError,
        "finish_genesis in assembly phase {phase}: {phase} is not a source of runtime_assembly.04",
    ),
    "construction_finished": (ValueError, "finish_genesis twice: construction happens once"),
    "construction_after_seed": (
        ValueError,
        "finish_genesis after seed_period: construction comes first",
    ),
    "seed_inside_input": (ValueError, "seed_period inside an input: assembly precedes inputs"),
    "seed_on_used_state": (
        ValueError,
        "seed_period on a used state: seeding is assembly's first act, and a"
        " live state never moves its period (I2)",
    ),
}

#: A rule every phase of a group takes is one row on the group (.04, .41-.48), written only
#: where no phase in the group has its own row for the verb. A deleted row then leaves a gap
#: and never falls into another rule's refusal (DL-335, DL-373). Every phase by verb resolves
#: to a move or a refusal row, so a missing row is a gap.
RUNTIME_ASSEMBLY: Final = Statechart(
    name="runtime_assembly",
    root=State(
        "runtime_assembly",
        regions=(
            Region(
                "phase",
                "unbuilt",
                (
                    State(
                        "unbuilt",
                        regions=(
                            Region(
                                "construction",
                                "fresh",
                                (State("fresh"), State("installed"), State("genesis")),
                            ),
                        ),
                    ),
                    State(
                        "open",
                        regions=(
                            Region(
                                "inputs", "genesis_input", (State("genesis_input"), State("input"))
                            ),
                        ),
                    ),
                    State(
                        "built",
                        regions=(
                            Region(
                                "service",
                                "constructed",
                                (State("constructed"), State("seeded"), State("live")),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "runtime_assembly.01",
            "fresh",
            INSTALL,
            "installed",
            cite="period-model ss7",
            note="Nothing is installed, committed or seeded. Carried rows land verbatim, revisions included.",
        ),
        Row(
            "runtime_assembly.03",
            "genesis_input",
            COMMIT_INPUT,
            "genesis",
            cite="DL-87",
            note="One revision per changed entity.",
        ),
        Row(
            "runtime_assembly.04",
            "unbuilt",
            FINISH_GENESIS,
            "constructed",
            cite="DL-132",
            note="Construction ends. The genesis seed is not an input to the seed latch.",
        ),
        Row(
            "runtime_assembly.05",
            "fresh",
            SEED_PERIOD,
            "seeded",
            cite="period-model ss3.5, DL-132",
            note="Set the period id. A period below one is the verb's own check.",
        ),
        Row(
            "runtime_assembly.07",
            "input",
            COMMIT_INPUT,
            "live",
            cite="DL-87, DL-120",
            note="Check capacity. One revision per changed entity.",
        ),
        Row(
            "runtime_assembly.12",
            "installed",
            SEED_PERIOD,
            "seeded",
            cite="period-model ss3.5, DL-132",
            note="As runtime_assembly.05.",
        ),
        Row(
            "runtime_assembly.13",
            "constructed",
            SEED_PERIOD,
            "seeded",
            cite="period-model ss3.5, DL-132",
            note="As runtime_assembly.05.",
        ),
        Row(
            "runtime_assembly.41",
            "unbuilt",
            BEGIN_INPUT,
            "genesis_input",
            cite="DL-87",
            note="Construction is not finished. Open the genesis seed's input.",
        ),
        Row(
            "runtime_assembly.42",
            "built",
            BEGIN_INPUT,
            "input",
            cite="DL-87, concurrency-model ss3",
            note="Snapshot on first touch.",
        ),
        Row(
            "runtime_assembly.16",
            "installed",
            INSTALL,
            kind="refusal",
            code="install_on_used_state",
            cite="period-model ss7",
            note="Carried rows are assembly's first act: only a fresh state installs them.",
        ),
        Row(
            "runtime_assembly.18",
            "genesis",
            INSTALL,
            kind="refusal",
            code="install_on_used_state",
            cite="period-model ss7",
            note="Carried rows are assembly's first act: only a fresh state installs them.",
        ),
        Row(
            "runtime_assembly.31",
            "constructed",
            FINISH_GENESIS,
            kind="refusal",
            code="construction_finished",
            cite="DL-132",
            note="Construction ends once: a second call would launder a used state back to fresh.",
        ),
        Row(
            "runtime_assembly.32",
            "seeded",
            FINISH_GENESIS,
            kind="refusal",
            code="construction_after_seed",
            cite="DL-132",
            note="Construction comes before the seed.",
        ),
        Row(
            "runtime_assembly.33",
            "genesis_input",
            FINISH_GENESIS,
            kind="refusal",
            code="inside_genesis_input",
            cite="DL-87, DL-293",
            note="Construction ends after the genesis input commits.",
        ),
        Row(
            "runtime_assembly.34",
            "input",
            FINISH_GENESIS,
            kind="refusal",
            code="construction_finished",
            cite="DL-132",
            note="Construction ends once: a second call would launder a used state back to fresh.",
        ),
        Row(
            "runtime_assembly.35",
            "live",
            FINISH_GENESIS,
            kind="refusal",
            code="construction_finished",
            cite="DL-132",
            note="Construction ends once: a second call would launder a used state back to fresh.",
        ),
        Row(
            "runtime_assembly.37",
            "genesis",
            SEED_PERIOD,
            kind="refusal",
            code="seed_on_used_state",
            cite="period-model ss3.5, DL-132",
            note="Seeding is assembly's first act: a live state never moves its period.",
        ),
        Row(
            "runtime_assembly.38",
            "seeded",
            SEED_PERIOD,
            kind="refusal",
            code="seed_on_used_state",
            cite="period-model ss3.5, DL-132",
            note="Seeding is assembly's first act: a live state never moves its period.",
        ),
        Row(
            "runtime_assembly.40",
            "live",
            SEED_PERIOD,
            kind="refusal",
            code="seed_on_used_state",
            cite="period-model ss3.5, DL-132",
            note="Seeding is assembly's first act: a live state never moves its period.",
        ),
        Row(
            "runtime_assembly.43",
            "open",
            INSTALL,
            kind="refusal",
            code="install_on_used_state",
            cite="period-model ss7",
            note="Carried rows are assembly's first act: only a fresh state installs them.",
        ),
        Row(
            "runtime_assembly.44",
            "built",
            INSTALL,
            kind="refusal",
            code="install_on_used_state",
            cite="period-model ss7",
            note="Carried rows are assembly's first act: only a fresh state installs them.",
        ),
        Row(
            "runtime_assembly.45",
            "unbuilt",
            COMMIT_INPUT,
            kind="refusal",
            code="no_input_open",
            cite="DL-87, DL-293",
            note="A commit needs an input open.",
        ),
        Row(
            "runtime_assembly.46",
            "built",
            COMMIT_INPUT,
            kind="refusal",
            code="no_input_open",
            cite="DL-87, DL-293",
            note="A commit needs an input open.",
        ),
        Row(
            "runtime_assembly.47",
            "open",
            BEGIN_INPUT,
            kind="refusal",
            code="input_already_open",
            cite="DL-87",
            note="Inputs do not nest.",
        ),
        Row(
            "runtime_assembly.48",
            "open",
            SEED_PERIOD,
            kind="refusal",
            code="seed_inside_input",
            cite="DL-132",
            note="Assembly precedes inputs.",
        ),
    ),
    clock="logical",
)


#: What the job instances' tier gives the state owner for one job row (DL-341): the chart, the
#: configuration the row projects, and the instance's data.
type JobInstance = Callable[[str, JobRuntime], tuple[Statechart, frozenset[str], object]]


def _restore_job(machines: Executor, instance: JobInstance, job: str, row: JobRuntime) -> None:
    chart, active, data = instance(job, row)
    machines.restore(RuntimeState.job_key(job), chart, active, data)


class RuntimeState:
    """The authoritative state of one Oracle: job rows, global rows, and the timer heap.

    SEM-01 latching applies throughout. The rows are frozen, the maps are private and published
    as read-only views, and every write goes through a verb that names what changed
    (DL-82, DL-86): `transition`, `start_run`, `move_flag`, `set_global`, `enqueue_timer`, and
    the capacity verbs `reserve`, `release_reservations`, `enqueue_waiter`, `dequeue_waiter`,
    `seed_consumed` (DL-120), `take_over_held` and `release_held` (DL-256). The verbs that move
    a job's status, flags or held capacity write what the `job` chart reached. `install` and
    `seed_job` install rows; an update's `retire_job` and `admit_job` remove and add one between
    inputs (spec-reload). Optimistic locking needs one place where "this entity changed" is
    observable once per applied input (concurrency-model ss3).

    The timer heap lives here (concurrency-model ss3): an armed must_start deadline on a refused
    start changes no job row, so a projection over rows alone would replay a different schedule.
    The heap is ordered by `(due, insertion token)`, and every job's timers carry that token.

    The capacity state lives here (DL-120, period-model ss5): the held half is `reservations`
    on the rows, the spent half is `consumed` (per bucket), and the waiter rank allocator is
    `enqueue_counter`. No sum of the two is stored; `CapacityPool` computes usage.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobRuntime] = {}
        self._globals: dict[str, GlobalRuntime] = {}
        #: DL-94's routing table, dormant: one engine runs every job on its own host, so the
        #: table stays empty and no tier writes or reads it.
        self._hosts: dict[str, HostRuntime] = {}
        #: The state owner's executor (statechart-engine ss11): the `RUNTIME_ASSEMBLY` instance.
        #: A fork copies it (DL-339); an install, `restore_instances` and `keep_job_instances`
        #: rebuild it (`rebuild_machines`). The engine's executor is separate.
        self.machines = Executor()
        self.machines.start(ASSEMBLY_KEY, RUNTIME_ASSEMBLY)
        self.machines.run()
        #: The job instances (DL-341). None keeps none. The oracle sets it: every job row then
        #: has a `job` instance under its `job_key`, which each input's commit checks against
        #: its row (`commit_input`, DL-350).
        self.job_instance: JobInstance | None = None
        self._timers: list[tuple[datetime, int, Event]] = []  # heap of (due, token, ev)
        self._timer_seq = 0
        #: The period this state runs in (period-model ss3.5, DL-132). Set only by
        #: `seed_period`, 1 until then, and stamped onto every row's `start_period` at its
        #: actual start. A period opens in a new state over the carried rows (`Oracle`).
        self._period_id: int = 1
        #: DL-120: bucket key -> units permanently spent (SEM-16 depletion, and `never` or
        #: unmet-`success` holds a terminal transition kept). It belongs to no row. Keys survive
        #: their resource: a bucket the catalog does not size keeps its entry (PR-19a).
        self._consumed: dict[str, int] = {}
        #: DL-120: the waiter-rank allocator's high-water mark. Carried, never renormalised
        #: (period-model ss5).
        self._enqueue_counter = 0
        #: The last row of each job an update removed (spec-reload): its run number, flags and
        #: history fields, kept so a later update that adds the name back carries them. A
        #: tombstone is not a row: no verb, read or scan sees it (`retire_job`, `admit_job`).
        self._tombstones: dict[str, JobRuntime] = {}
        #: DL-87 input transaction: entity key -> its projection at first touch within the open
        #: input. Empty outside one.
        self._snapshots: dict[str, object] = {}

    @property
    def _phase(self) -> AssemblyPhase:
        """The `runtime_assembly` phase: the leaf of the assembly instance's configuration.

        Seed-versus-advance is explicit: `seed_period` is legal once, before any committed input.
        """
        (leaf,) = self.machines.active(ASSEMBLY_KEY) & ASSEMBLY_PHASES
        return cast(AssemblyPhase, leaf)

    def fork(self) -> RuntimeState:
        """An independent copy to dry-apply one input on (concurrency-model ss4).

        Shallow: rows are frozen and a write replaces a row, so copying each map and the heap list
        keeps every write on the copy. The twin's executor is `Executor.fork` of this one's
        (DL-339). Job instances share their value data, and `job_instance` is shared for the
        oracle's fork to rebind (DL-341).
        """
        twin = copy.copy(self)
        twin._jobs = dict(self._jobs)
        twin._globals = dict(self._globals)
        twin.machines = self.machines.fork()
        twin._timers = list(self._timers)
        twin._consumed = dict(self._consumed)
        twin._snapshots = dict(self._snapshots)
        twin._tombstones = dict(self._tombstones)
        return twin

    # ------------------------------------------------------------------- reads

    @staticmethod
    def job_key(job: str) -> str:
        """The namespaced key `expect` addresses an entity by (concurrency-model ss6)."""
        return f"job:{job}"

    @staticmethod
    def global_key(name: str) -> str:
        return f"global:{name}"

    def revision(self, key: str) -> int:
        """The revision an `expect` must name for `key`; an entity that does not exist reads 0.

        That makes S3's conditional create expressible: `expect {"global:X": 0}` means still absent.
        """
        namespace, _, name = key.partition(":")
        if namespace == "job":
            row = self._jobs.get(name)
            return 0 if row is None else row.state_rev
        if namespace == "global":
            grow = self._globals.get(name)
            return 0 if grow is None else grow.state_rev
        raise OracleError(f"unknown entity namespace in {key!r}")

    @property
    def job(self) -> Mapping[str, JobRuntime]:
        """Read-only view of the job rows; a write through it raises."""
        return MappingProxyType(self._jobs)

    @property
    def globals_(self) -> Mapping[str, GlobalRuntime]:
        return MappingProxyType(self._globals)

    def runtime(self, job: str) -> JobRuntime:
        """Read access. Creates the row on demand: the oracle addresses entities with no catalog
        entry (pseudo-entries `name^INST`, whatever a CHANGE_STATUS invents). A name an update
        removed is refused instead: its tombstone is not a row, and no input makes it one."""
        if job in self._tombstones:
            raise OracleError(f"{job!r} was removed by an update: no input reaches it")
        if job not in self._jobs:
            row = self._jobs[job] = JobRuntime()
            if self.job_instance is not None:
                _restore_job(self.machines, self.job_instance, job, row)
        return self._jobs[job]

    def global_value(self, name: str) -> str | None:
        row = self._globals.get(name)
        return None if row is None else row.value

    @property
    def tombstones(self) -> Mapping[str, JobRuntime]:
        """Read-only view of the rows updates removed, by job name (spec-reload)."""
        return MappingProxyType(self._tombstones)

    @property
    def consumed(self) -> Mapping[str, int]:
        """Read-only view of the permanently spent units per bucket (DL-120)."""
        return MappingProxyType(self._consumed)

    @property
    def enqueue_counter(self) -> int:
        """The last waiter rank allocated (DL-120); only `enqueue_waiter` allocates."""
        return self._enqueue_counter

    @property
    def timer_seq(self) -> int:
        """The last timer token allocated (period-model ss3.1); only `enqueue_timer` allocates.

        A seal carries it: an opener restarting from 0 would re-issue tokens the carried firing
        order was written in.
        """
        return self._timer_seq

    # ------------------------------------------------ the input transaction (DL-87)

    def _projection(self, key: str) -> object:
        """An entity's semantic value: what a revision is a revision of.

        A job's is its projected fields plus its own timers with their ordering tokens
        (concurrency-model ss3). A missing job projects as the default row; a missing global
        projects as None, so first-set is a change and `revision() == 0` means absent.
        """
        namespace, _, name = key.partition(":")
        if namespace == "job":
            row = self._jobs.get(name) or _DEFAULT_JOB
            fields = tuple(getattr(row, field) for field in _PROJECTED_JOB_FIELDS)
            return (fields, tuple(self.timers_for(name)))
        grow = self._globals.get(name)
        return None if grow is None else grow.value

    def _touch(self, key: str) -> None:
        """Record an entity's pre-input projection the first time an open input reaches it.

        The projection is a function of the rows and the heap, which only move through the
        mutators below, so the touched set is never under-approximated (ss3).
        """
        if "open" in self.machines.active(ASSEMBLY_KEY) and key not in self._snapshots:
            self._snapshots[key] = self._projection(key)

    def begin_input(self) -> None:
        """Open one input's transaction. Inputs do not nest: the cascade, fired timers and box
        folds of one input share its revision, so `expect` is checkable."""
        self._assemble(BEGIN_INPUT)
        self._snapshots = {}

    def commit_input(self) -> list[str]:
        """Close the transaction and increment each changed entity once; return the changed keys.

        `ApplyResult` reads the list.

        Committing with no input open is refused: the chart has no such move. Where job instances
        are kept, each touched job that has a row must project to its instance's configuration,
        or the commit raises a `StatechartError` (DL-350). The commit creates no row.
        """
        self._assemble(COMMIT_INPUT)
        snapshots, self._snapshots = self._snapshots, {}
        self._check_capacity(snapshots)
        changed = [key for key, before in snapshots.items() if self._projection(key) != before]
        for key in sorted(changed):
            namespace, _, name = key.partition(":")
            if namespace == "job":
                self._replace(name, state_rev=self.runtime(name).state_rev + 1)
            else:
                row = self._globals[name]
                self._globals[name] = GlobalRuntime(value=row.value, state_rev=row.state_rev + 1)
        if self.job_instance is not None:
            for key in snapshots:
                namespace, _, name = key.partition(":")
                # a timer alone touches its job's key: no row, nothing to check
                if namespace == "job" and name in self._jobs:
                    self._check_job_instance(name)
        return sorted(changed)

    def _check_job_instance(self, job: str) -> None:
        """The job's row projects to its instance's configuration (DL-350)."""
        instance = cast(JobInstance, self.job_instance)
        _chart, row_active, _data = instance(job, self._jobs[job])
        active = self.machines.active(self.job_key(job))
        if active != row_active:
            raise StatechartError(
                f"job {job!r}: at the input's commit its instance is in"
                f" {', '.join(sorted(active))}, and its row projects to"
                f" {', '.join(sorted(row_active))}"
            )

    def _check_capacity(self, snapshots: Mapping[str, object]) -> None:
        """The DL-120 capacity invariants, checked at the close of an input.

        Not inside the verbs: one input passes through states that break them, and only the
        boundary between inputs is observable. Only touched rows are checked.
        """
        for key in snapshots:
            namespace, _, name = key.partition(":")
            if namespace != "job":
                continue
            row = self._jobs.get(name)
            if row is None:
                continue
            live = row.status in LIVE
            if not live and not all(map(may_outlive_run, row.reservations)):
                raise OracleError(f"{name!r} holds capacity at status {row.status}")
            if (row.waiter_seq is not None) != (row.status == "QUE_WAIT"):
                raise OracleError(
                    f"{name!r} has waiter_seq {row.waiter_seq} at status {row.status}:"
                    " a rank is held exactly while QUE_WAIT"
                )
            if row.waiter_seq is not None and row.waiter_seq > self._enqueue_counter:
                raise OracleError(
                    f"{name!r} has waiter_seq {row.waiter_seq} above the allocator's"
                    f" {self._enqueue_counter}"
                )
        for bucket, units in self._consumed.items():
            if units < 0:
                raise OracleError(f"consumed[{bucket!r}] = {units}: units spent cannot be negative")

    # ------------------------------------------------------------------ writes

    def _replace(self, job: str, **fields: object) -> None:
        """The single write. `model_copy(update=)` does not validate, so the row is rebuilt
        through the model's constructor; an undeclared field name raises."""
        self._touch(self.job_key(job))
        self._jobs[job] = JobRuntime.model_validate({**dict(self.runtime(job)), **fields})

    def transition(
        self,
        job: str,
        status: JobStatus,
        at: datetime | None,
        exit_code: int | None = None,
        *,
        clear_exit_code: bool = False,
    ) -> None:
        """Record a status change: the status the `job` chart reached.

        Every status write is one of these, except an install. `last_end_at` latches on every
        terminal transition (Q2, DL-54); an exit code is written only when one was reported. A
        box-start reset clears it instead (`clear_exit_code`, SEM-10, DL-242).
        """
        fields: dict[str, object] = {"status": status, "status_at": at}
        if status in TERMINAL:
            fields["last_end_at"] = at
        if exit_code is not None:
            fields["exit_code"] = exit_code
        elif clear_exit_code:
            fields["exit_code"] = None
        self._replace(job, **fields)

    def start_run(self, job: str, *, cause: str, box: str | None, is_box: bool) -> None:
        """Everything one actual start changes: the run_number bump, the arm it consumes (Q3,
        DL-54, FORCE included), the run's provenance (DL-68), and the SEM-10 box bookkeeping.

        The member joins its box's ran set and a box starting resets its own; a box that is
        itself a member does both, to two different rows.
        """
        self.move_flag(job, "arm_off")
        self._replace(
            job,
            run_number=self.runtime(job).run_number + 1,
            started_by=cause,
            start_period=self._period_id,
        )
        if box is not None:
            # the member's own start voids its resolution mark (DL-242) and its off-ice mark
            # (SEM-20): a forced run votes in the fold
            box_rt = self.runtime(box)
            self._replace(
                box,
                ran_members=box_rt.ran_members | {job},
                window_skipped_members=box_rt.window_skipped_members - {job},
                iced_out_members=box_rt.iced_out_members - {job},
            )
        if is_box:
            # Reset before the caller's RUNNING transition: its re-evaluation may start members,
            # and they must land in the fresh per-run set (SEM-10). The resolution and off-ice
            # marks are per-execution too (DL-154, DL-242, SEM-20).
            self._replace(
                job,
                ran_members=frozenset(),
                window_skipped_members=frozenset(),
                iced_out_members=frozenset(),
            )

    def clear_exit_code(self, job: str) -> None:
        """SEM-10 (DL-242): a box-start reset drops the previous cycle's exit code from an
        INACTIVE row. Not a transition: the status and its time stay."""
        self._replace(job, exit_code=None)

    def record_resolution(self, box: str, member: str) -> None:
        """Mark `member` resolved for this box execution: its INACTIVE is an explicit verdict, a
        run_window skip (SEM-33, DL-154) or an injected STATUS INACTIVE (SEM-11, DL-242), so the
        SEM-11 fold completes past it. The box's start and the member's own start void the mark."""
        self._replace(
            box, window_skipped_members=self.runtime(box).window_skipped_members | {member}
        )

    def record_iced_out(self, box: str, member: str) -> None:
        """Mark `member` out of this box execution: taken off ice while `box` ran, before it ran
        there (SEM-20, `off-ice-in-running-box=next-run`). The box's start and the member's own
        forced start void the mark."""
        self._replace(box, iced_out_members=self.runtime(box).iced_out_members | {member})

    def void_resolution(self, box: str, member: str) -> None:
        """Drop `member`'s resolution mark in `box`: a fresh attempt that ends unresolved must not
        inherit an earlier verdict of the same box run (DL-257)."""
        marks = self.runtime(box).window_skipped_members
        if member in marks:
            self._replace(box, window_skipped_members=marks - {member})

    def move_flag(self, job: str, target: FlagState) -> None:
        """Set one SEM-20/21/22 flag, or the SEM-32 arm (DL-54), to the state the `job` chart
        reached. The state names the flag, so a caller cannot touch another."""
        field, value = FLAG_FIELDS[target]
        self._replace(job, **{field: value})

    def seed_job(
        self, job: str, *, on_ice: bool = False, on_hold: bool = False, on_noexec: bool = False
    ) -> None:
        """Install a row's definition-time flags (SEM-24) for the Oracle's genesis seed; not a
        move, so no machine is taken."""
        self._replace(job, on_ice=on_ice, on_hold=on_hold, on_noexec=on_noexec)

    def require_between_inputs(self, verb: str) -> None:
        """Raise unless no input is open: an update applies between inputs (spec-reload)."""
        if "open" in self.machines.active(ASSEMBLY_KEY):
            raise OracleError(f"{verb} inside an input: an update applies between inputs")

    def retire_job(self, job: str, keeps_held: Callable[[str], bool] | None) -> JobRuntime:
        """An update removed `job` (spec-reload): its row ends and becomes its tombstone.

        A live run's units settle as an end that is not SUCCESS (`release_reservations` with
        `keeps_held`), so a depletable's units are spent into `consumed`; what the row still
        holds after that goes back with the row, as a removed holder's units do at a period
        opening (DL-256). The queue rank and the job's timers go. The tombstone keeps the
        status, the run number, the flags and the times; it holds no units and no rank. Not an
        input: no revision moves. The caller rebuilds the instances (`restore_instances`).
        """
        self.require_between_inputs(f"retire_job({job!r})")
        if job not in self._jobs:
            raise OracleError(f"retire_job({job!r}): the job has no row")
        if self._jobs[job].status in LIVE:
            self.release_reservations(job, "TERMINATED", keeps_held)
        row = self._jobs.pop(job)
        tombstone = JobRuntime.model_validate({**dict(row), "reservations": (), "waiter_seq": None})
        self._tombstones[job] = tombstone
        self._timers = [entry for entry in self._timers if entry[2].payload.get("job") != job]
        heapq.heapify(self._timers)
        return tombstone

    def admit_job(
        self, job: str, *, on_ice: bool = False, on_hold: bool = False, on_noexec: bool = False
    ) -> None:
        """An update added `job` (spec-reload): a new INACTIVE row with these flags. A tombstone
        of the name gives the row its run number, so a run number is never reused, and its
        revision plus one, so an `expect` read before the removal fails (concurrency-model
        ss3); the tombstone is dropped. Not an input. The caller rebuilds the instances."""
        self.require_between_inputs(f"admit_job({job!r})")
        if job in self._jobs:
            raise OracleError(f"admit_job({job!r}): the job already has a row")
        tombstone = self._tombstones.pop(job, None)
        carried = {} if tombstone is None else {"run_number": tombstone.run_number}
        if tombstone is not None:
            carried["state_rev"] = tombstone.state_rev + 1
        self._jobs[job] = JobRuntime.model_validate(
            {**carried, "on_ice": on_ice, "on_hold": on_hold, "on_noexec": on_noexec}
        )

    def set_global(self, name: str, value: str) -> None:
        """Latch a global's value (SEM-06). Only commit_input() moves the revision, and only if
        the value changed; a same-value SET_GLOBAL is a real input."""
        self._touch(self.global_key(name))
        row = self._globals.get(name)
        self._globals[name] = GlobalRuntime(
            value=value, state_rev=0 if row is None else row.state_rev
        )

    # --------------------------------------------------------- capacity (DL-120)
    #
    # `consumed` and `enqueue_counter` have no key space of their own: each changes only inside
    # an input that also replaces the row of the job whose units or rank moved, so that job
    # carries the revision. They gain an `expect` namespace when an operator needs to address
    # them (for `consumed`, SEM-16 replenishment, period-model ss5).

    def reserve(self, job: str, reservations: Sequence[CapacityReservation]) -> None:
        """Record the capacity vector a start acquired (period-model ss5).

        Refuses a row that already holds one: the Oracle releases before it wakes anything, so a
        live record at a start means a missed release. A start of a job that still holds units
        from an earlier run goes through `take_over_held` (DL-256).
        """
        if self.runtime(job).reservations:
            raise OracleError(f"{job!r} already holds reservations: a start may not overwrite")
        self._replace(job, reservations=tuple(reservations))

    def take_over_held(self, job: str, reservations: Sequence[CapacityReservation]) -> None:
        """DL-256: a job that is not live and holds a renewable's units starts on them.

        The new vector replaces the held units, so nothing is counted twice. A live row is
        refused. Callers: an admitted start and a FORCE_STARTJOB on the held units.
        """
        row = self.runtime(job)
        if row.status in LIVE:
            raise OracleError(f"{job!r} is {row.status}: a start may not overwrite its run")
        self._replace(job, reservations=tuple(reservations))

    def release_reservations(
        self,
        job: str,
        new_status: str,
        keeps_held: Callable[[str], bool] | None = None,
    ) -> None:
        """Settle a run's vector on the edge that leaves STARTING/RUNNING (DL-120).

        What the policy frees goes back to the pool. What it does not free (`never` and an unmet
        `success`: SEM-16 depletion, hold-on-failure) is spent into `consumed`, or stays on the
        row when `keeps_held(bucket)` says so (DL-256); with no `keeps_held`, all of it is
        spent. `new_status` is the status the row moves to, INACTIVE for an injected STATUS on a
        live holder. The row write and the spend are one act in one input, so a crash between
        them cannot leave units both held and spent or neither.
        """
        row = self.runtime(job)
        if not row.reservations:
            return
        kept, spent = unreleased(row.reservations, new_status == "SUCCESS", keeps_held)
        self._replace(job, reservations=kept)  # validates first; the spend cannot raise
        for reservation in spent:
            bucket = reservation.bucket
            self._consumed[bucket] = self._consumed.get(bucket, 0) + reservation.units

    def release_held(self, job: str) -> tuple[CapacityReservation, ...]:
        """RELEASE_RESOURCE (DL-256): give back every unit a job that is not live still holds,
        and return them. A live row is refused; the Oracle records that case as a no-op. Callers:
        the operator's verb and a period opening that gives back a removed job's units."""
        row = self.runtime(job)
        if row.status in LIVE:
            raise OracleError(f"{job!r} is {row.status}: its run's units release at its end")
        self._replace(job, reservations=())
        return row.reservations

    def enqueue_waiter(self, job: str) -> None:
        """Allocate this job's QUE_WAIT rank. Idempotent: a queued job keeps its rank."""
        if self.runtime(job).waiter_seq is not None:
            return
        rank = self._enqueue_counter + 1
        self._replace(job, waiter_seq=rank)
        self._enqueue_counter = rank

    def dequeue_waiter(self, job: str) -> None:
        """Drop a job out of the queue (admitted, killed, iced or cancelled). The counter is a
        high-water allocator and does not go back."""
        self._replace(job, waiter_seq=None)

    def seed_consumed(self, consumed: Mapping[str, int]) -> None:
        """Open the map from a carried seal (period-model ss3.3). A negative value is refused
        here as well as by the loader (PR-22)."""
        for bucket, units in consumed.items():
            if units < 0:
                raise OracleError(f"consumed[{bucket!r}] = {units}: units spent cannot be negative")
        self._consumed = dict(consumed)

    def rebuild_machines(self) -> Executor:
        """A new executor holding the assembly instance in the store's phase (statechart-engine
        ss11) and, with `job_instance` set, one `job` instance per job row (DL-341) and the queue
        scan's coordinator, idle between inputs (DL-372).

        An install, `restore_instances` and `keep_job_instances` build theirs here; no action
        runs.
        """
        machines = Executor()
        machines.restore(ASSEMBLY_KEY, RUNTIME_ASSEMBLY, self.machines.active(ASSEMBLY_KEY))
        if self.job_instance is not None:
            machines.restore(SCAN_KEY, QUEUE_SCAN, SCAN_IDLE)
            for job, jrow in self._jobs.items():
                _restore_job(machines, self.job_instance, job, jrow)
        return machines

    def keep_job_instances(self, instance: JobInstance) -> None:
        """Keep one `job` instance per job row, as `instance` gives it (DL-341): the executor is
        rebuilt with one for every row, and a row created later gets its own."""
        self.job_instance = instance
        self.machines = self.rebuild_machines()

    def restore_instances(self) -> None:
        """Rebuild the executor from the phase and the rows (`rebuild_machines`): after a
        macrostep that raised, every instance is its row's again (DL-350)."""
        self.machines = self.rebuild_machines()

    @property
    def period_id(self) -> int:
        return self._period_id

    def finish_genesis(self) -> None:
        """Mark the constructor's own seeding as not an input for the seed-versus-advance latch.

        Genesis seeding is identical on every replay and unjournaled, so a constructed state is
        fresh. One-shot: a second call after real inputs would launder a
        used state back to fresh. Refused inside the genesis input too.
        """
        self._assemble(FINISH_GENESIS)

    def _assemble(self, event: str) -> None:
        """Move the assembly instance by one verb. A phase the chart refuses it in raises before
        the phase changes: assembly is not an input (concurrency-model ss4)."""
        (outcome,) = self.machines.dispatch(ASSEMBLY_KEY, ChartEvent(event))
        self._refuse(event, outcome)

    def _read_assembly(self, event: str) -> None:
        """The chart's answer to a verb, read with `Executor.resolve`; it moves nothing. A verb
        with work that can fail reads first and moves the phase after."""
        self._refuse(event, self.machines.resolve(ASSEMBLY_KEY, ChartEvent(event)))

    def _refuse(self, event: str, outcome: Outcome) -> None:
        """Raise a refusal outcome as the class and text its code declares."""
        if not outcome.refusals:
            return
        code = outcome.refusals[0]
        if code not in _ASSEMBLY_REFUSALS:
            raise self.machines.explain(
                StatechartError(
                    f"{event} in assembly phase {self._phase}: the refusal code {code!r} has no"
                    " sentence in the state owner"
                ),
                ASSEMBLY_KEY,
                ChartEvent(event),
            )
        raised, text = _ASSEMBLY_REFUSALS[code]
        raise raised(text.format(phase=self._phase))

    def install(self, carried: CarriedRows) -> None:
        """Install carried rows verbatim, revisions included, as the first act of an assembly
        (period-model ss7 phase 3 step 3).

        Not an input: the rows keep the revisions the closing seal published, so an operator's
        `expect` against one stays unmoved; a construct-C2-then-overwrite opener would move every
        revision it touches. The period is not seeded here, because the
        constructor's catalog seed still runs over these rows; the assembler calls `seed_period`
        after (ss3.5, DL-132). The executor is rebuilt from the phase and the carried rows.
        """
        self._assemble(INSTALL)
        self._jobs = dict(carried.jobs)
        self._globals = dict(carried.globals_)
        self.machines = self.rebuild_machines()
        self._timers = sorted(carried.timers, key=_timer_order)
        heapq.heapify(self._timers)
        self._timer_seq = carried.timer_seq
        self._consumed = dict(carried.consumed)
        self._enqueue_counter = carried.enqueue_counter
        self._tombstones = dict(carried.tombstones)

    def seed_period(self, period_id: int) -> None:
        """Set the period a fresh state is being assembled into (period-model ss3.5, DL-132).

        The loader's first act, before any input. Explicit, never inferred from the rows. Legal
        once, and never after a committed input; the seal's I2 and lineage bounds hold the number
        to the lineage.
        """
        self._read_assembly(SEED_PERIOD)  # a refusal comes before the period is checked
        if period_id < 1:
            raise ValueError(f"seed_period({period_id}): periods count from 1 (I2)")
        self._assemble(SEED_PERIOD)
        self._period_id = period_id

    def enqueue_timer(self, due: datetime, ev: Event) -> int:
        """Arm a timer and return its ordering token. The token is one global counter, so
        equal-due timers keep their arming order across jobs."""
        job = ev.payload.get("job")
        if isinstance(job, str):
            self._touch(self.job_key(job))  # the heap IS part of the projection
        self._timer_seq += 1
        heapq.heappush(self._timers, (due, self._timer_seq, ev))
        return self._timer_seq

    def next_timer_due(self) -> datetime | None:
        return self._timers[0][0] if self._timers else None

    def pop_timer_due(self, at: datetime) -> tuple[datetime, Event] | None:
        """Pop the earliest timer due at or before `at`, or None."""
        if not self._timers or self._timers[0][0] > at:
            return None
        job = self._timers[0][2].payload.get("job")
        if isinstance(job, str):
            self._touch(self.job_key(job))  # a timer LEAVING the heap is a change too
        due, _, ev = heapq.heappop(self._timers)
        return due, ev

    def timers(self) -> list[tuple[datetime, int, Event]]:
        """Every armed timer as (due, token, event), in FIRING order."""
        return sorted(self._timers, key=_timer_order)

    def timers_for(self, job: str) -> list[tuple[datetime, int]]:
        """One job's armed timers as (due, token), in firing order: the entity-local half of the
        global ordering (concurrency-model ss3)."""

        return [
            (due, token)
            for due, token, ev in sorted(self._timers, key=_timer_order)
            if ev.payload.get("job") == job
        ]
