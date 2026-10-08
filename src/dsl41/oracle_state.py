"""The oracle's state, and the vocabulary of the events that move it.

Split out of oracle.py by DL-91. The seam is not "extract the state owner"
-- `RuntimeState`'s timer heap holds `Event`s, so `Event` comes with it and
brings `EventKind` -- it is the pair the interpreter always was: the MODEL
and the machine that moves it. `oracle.py` imports this; nothing here
imports `oracle.py`, and that direction is the whole point.

The import table said it from outside before the split did: ten modules
import from `oracle`, eight of them want `Event` and six want `Oracle`, so
`runner_scheduler` was dragging an 854-line interpreter in to name a
timestamped event.

What is here:

- **The vocabulary.** `JobStatus`, `TERMINAL`, `EventKind`, `Event`,
  `TraceEntry`. `Event` is the oracle's input alphabet and, with `source`,
  its provenance (DL-68).
- **The rows.** `JobRuntime`, `GlobalRuntime` and `HostRuntime`, all FROZEN
  (DL-86, DL-94), and the semantic projection that decides when a revision
  moves.
- **The owner.** `RuntimeState`: private maps, typed verbs, the timer heap
  with its ordering token, and the input transaction that gives one
  committed input exactly one revision per changed entity
  (concurrency-model ss3).

- **`OracleError`**, raised on both sides of the split, so it is defined on
  the side that has no dependencies.
- **The job's machines.** `job_status`, `job_flags` and `job_holding`
  declare every move of a job row; `runtime_assembly` declares how a state
  is assembled and when an input is open (docs/state-machines.md).
- **The violation channel.** `RuntimeState.note_violation` collects the
  `Violation`s that `StateMachine.take` returns during an input, and the
  input's commit drains them (`InputBatch`). A check never raises in
  production; the engine decides what a violation costs (concurrency-model
  ss4).

`HostRuntime` is here and NOT in `oracle.py` for a reason the split makes
enforceable (DL-93): a job's condition truth cannot depend on where its
machine routes, so the interpreter must never read a host row. It lives
under the same owner because it is published state with a `state_rev` that
an `expect` names -- a routing table with a revision counter of its own
would be the same concept spelled a second way.

`InputBatch` stayed with `Oracle`: it drives the interpreter's clock and
drain, which is the interpreter's business, not the state's.

What is NOT here: `_N_FALSE_STATUSES` (SEM-02's n() rule is interpretation,
not state) and every SEM rule that reads or writes these rows.
"""

from __future__ import annotations

import copy
import heapq
import os
import sys

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from dsl41.state_machine import (
    STRICT_ENV,
    VIOLATION_LOG_PREFIX,
    StateMachine,
    Transition,
    TransitionError,
    Violation,
)


class OracleError(ValueError):
    pass


JobStatus = Literal[
    "INACTIVE",
    "QUE_WAIT",
    "STARTING",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
]

#: Every job status: the state set of the `job_status` machine.
JOB_STATUSES: frozenset[JobStatus] = frozenset(get_args(JobStatus))

#: The statuses an injected STATUS event may carry: every `JobStatus` except
#: QUE_WAIT. QUE_WAIT belongs to the capacity owner (DL-50): the queue
#: assigns it with a waiter rank and clears it on admission, and an operator
#: never injects it (DL-264). The oracle's STATUS handler and the control
#: server's CHANGE_STATUS check both read this one set, so framing cannot
#: admit a status the oracle refuses.
INJECTABLE_STATUSES: frozenset[JobStatus] = JOB_STATUSES - {"QUE_WAIT"}

TERMINAL: frozenset[JobStatus] = frozenset({"SUCCESS", "FAILURE", "TERMINATED"})
#: The terminal statuses that are not SUCCESS, named once: a failed vote in
#: the SEM-11 fold and the SEM-15 recompute, the box end that cascades to
#: job_terminator members (SEM-14), the FORCE_STARTJOB that starts on held
#: units (DL-256), and the CLI's `is-failed` predicate.
FAILED: frozenset[JobStatus] = frozenset({"FAILURE", "TERMINATED"})
# A run's reservations are taken while a row is in LIVE (period-model ss5)
# and released on leaving it, except the units a renewable's policy does not
# free: those stay on a row that is not live (DL-256, `may_outlive_run`). The
# release edge (DL-120), the SPAWN edge (DL-232) and the completion gate
# (DL-235) read it.
LIVE: frozenset[JobStatus] = frozenset({"STARTING", "RUNNING"})
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


#: The engine's ss7 input alphabet: where an externally injected event came
#: from. Named (DL-209) because the stamps live in call keywords AND in
#: parameter defaults -- `adapter` exists only as `Engine._enqueue`'s default
#: -- so an AST sweep for one spelling misses the other, and `adapter` is
#: the stamp that makes an event a COMPLETION subject to the ss4 stale gate.
EventSource = Literal["scheduler", "control", "adapter", "reconcile"]


class Event(BaseModel):
    at: datetime
    kind: EventKind
    payload: dict[str, object] = {}
    #: provenance of an externally injected event -- the engine's ss7 input
    #: alphabet; None for oracle-internal and script events. Start causes
    #: surface it (DL-68).
    #: DELIBERATELY `str`, not `EventSource`: this field is persisted (WAL
    #: attempts, scenario files) and `docs/protocol-evolution.md` holds a
    #: long-lived artifact to its own schema. A foreign or older journal
    #: naming a provenance this build does not know must still REPLAY; the
    #: Literal above is what the engine annotates and what the coverage
    #: register derives from, not a validation gate on the wire.
    source: str | None = None

    def job(self) -> str | None:
        job = self.payload.get("job")
        return job if isinstance(job, str) else None


class TraceEntry(BaseModel):
    at: datetime
    job: str
    transition: str  # "OLD->NEW" or an out-of-band marker like "ON_ICE"
    cause: str


#: The trace marker of a move that broke its declared transition
#: (concurrency-model ss4). The entry's `job` names the entity the move
#: belongs to, and its `cause` names the transition and the reason.
VIOLATION_MARKER = "TRANSITION_VIOLATION"


#: DL-50's three release policies, named once: `CapacityReservation` stores
#: one and `capacity.py` derives it from res_type + FREE.
ReleasePolicy = Literal["completion", "success", "never"]


class CapacityReservation(BaseModel):
    """One bucket's units, held by one `(job, run_number)` (DL-120,
    period-model ss5). FROZEN, for `JobRuntime`'s reason: it rides on that row
    and a change to it is a replacement of the row.

    The vector is frozen at acquisition and never recomputed from the current
    catalog: a re-baseline that raises the job's QUANTITY must still release
    what the live run actually took (PR-20)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: `m:<machine>` (max_load) or `r:<name>` (resource amount), as
    #: `CapacityPool` keys them.
    bucket: str
    units: int = Field(gt=0)
    #: DL-50: when the units go back. `never` and an unmet `success` are the
    #: two that do not free them: a depletable's are spent (SEM-16), and a
    #: renewable's stay held by the job (DL-256).
    release_policy: ReleasePolicy


#: The bucket prefix of a named resource; `m:` is a machine's load.
RESOURCE_BUCKET = "r:"


def may_outlive_run(reservation: CapacityReservation) -> bool:
    """DL-256: whether this reservation may stay on a row that is not live.
    Only a named resource's units under a policy that does not free on every
    completion can: the units a renewable kept after its run, held until
    RELEASE_RESOURCE or the job's next run. A machine load and a
    `completion` reservation are always released at the run's end. The
    seal loader and the store's invariant check share this one rule."""
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
    #: which PERIOD this run started in (period-model ss3.5, DL-132): set
    #: beside run_number at the actual start, so a run that crosses a seal
    #: -- CMD, FW, a SPAWN pending across periods, or a box, which has no
    #: execution entry at all -- stays attributable to the period that
    #: started it (PR-50). 1 for every row a pre-period build wrote.
    start_period: int = 1
    #: SEM-10 at-most-once bookkeeping: the members already run in THIS box's
    #: current execution. Only a BOX row ever carries entries; it was the loose
    #: `_box_ran` map until DL-86 moved it onto the entity it describes.
    ran_members: frozenset[str] = frozenset()
    #: Every resolution in THIS box execution: the members whose INACTIVE
    #: is an explicit verdict, a run_window skip (SEM-33/DL-154) or an
    #: operator's injected STATUS INACTIVE (SEM-11, DL-242). The SEM-11 fold
    #: completes past a resolved member that is not live. A member that
    #: merely waits in the run is INACTIVE too, but carries no mark. The
    #: member's own later start voids the mark. Only a BOX row ever carries
    #: entries; reset beside `ran_members` on box start. The name predates
    #: DL-242 and stays: sealed periods and their attestations carry it.
    window_skipped_members: frozenset[str] = frozenset()
    #: The members taken off ice during THIS box execution before they ran
    #: in it, under `off-ice-in-running-box=next-run` (SEM-20). They sit
    #: the run out: the SEM-11 fold skips them while they are not live or
    #: queued, and a plain start of one is refused until the box's next
    #: run. Unlike a resolution mark, the mark keeps a plain start out.
    #: FORCE_STARTJOB starts the member anyway: the start voids the mark,
    #: and a forced attempt that queues keeps the box waiting. An attempt
    #: that leaves the queue unstarted keeps the mark: the force was a
    #: one-shot override. Only a BOX row ever carries entries; reset
    #: beside `ran_members` on box start.
    iced_out_members: frozenset[str] = frozenset()
    #: DL-120: the capacity vector THIS run acquired, held while STARTING or
    #: RUNNING. After the run, the row keeps only a renewable's units its
    #: policy did not free (DL-256, `may_outlive_run`), until RELEASE_RESOURCE
    #: or the job's next run takes them over. It belongs on the row rather
    #: than in a map beside it -- which is also what makes it reconstructible
    #: from the rows a seal carries (period-model ss5).
    reservations: tuple[CapacityReservation, ...] = ()
    #: DL-120: this job's rank in the QUE_WAIT queue, non-null iff QUE_WAIT.
    #: The rank is allocated from `RuntimeState.enqueue_counter` and rides on
    #: the row so that admission ORDER survives a boundary (PR-21).
    waiter_seq: int | None = None
    #: DL-87: this entity's optimistic-locking revision. Incremented at most
    #: once per committed input, and only when the SEMANTIC projection below
    #: changed. Deliberately last, and deliberately excluded from that
    #: projection -- a revision that counted itself would justify its own
    #: next increment (concurrency-model ss3).
    state_rev: int = 0


class GlobalRuntime(BaseModel):
    """One global entity's row (SEM-06 latching semantics). A row rather than
    a bare string because the concurrency model gives globals their own
    identity and their own `state_rev` (concurrency-model ss2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    state_rev: int = 0


#: concurrency-model ss8's four routing states, in the order that table
#: lists them. `quarantined` is the leader's own, set automatically on
#: unreachability (ss7); the other three are the operator's.
HostState = Literal["active", "passive", "quarantined", "evicted"]


class HostRuntime(BaseModel):
    """One execution host's routing row (concurrency-model ss8). FROZEN, for
    the reason the other two rows are: a change is a REPLACEMENT, so "this
    entity changed" is one observable act.

    A host is a RELAY, not a machine -- ss2 gives it `host_id` and
    `generation`, and machine names resolve TO one. The row therefore holds
    only what decides routing and what proves an eviction. What the host is
    RUNNING is not here: that is the outbox's business (S5c), and a second
    durable record of "did this run start" is exactly the parallel model
    DL-91 exists to catch.

    The oracle never reads this row (DL-93). It is published state and it
    carries a `state_rev`, so it belongs to ss3's owner; it is not oracle
    vocabulary, so `HOST` is not an `EventKind` and `oracle.py` does not
    name this class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: HostState = "active"
    #: ss8's eviction fence. Eviction bumps it; a returning relay presenting
    #: a stale one is refused registration and must self-fence before it may
    #: re-register (CM-12, stage S5d).
    generation: int = 0
    #: ss8's deadman interval, in seconds. OPT-IN PER RUN ROOT, because it
    #: costs something real: a supervisor tolerating an absent controller
    #: indefinitely is what lets an engine crash and resume with its runs
    #: intact (DL-79). None = this host runs no deadman, so nothing bounds
    #: when its wrappers die and it is never reroutable except by force.
    #: S5b supplies the mechanism; the refusal it justifies is checkable the
    #: day this field exists.
    deadman_s: float | None = None
    #: when the leader last had positive contact with this host. Stamped at
    #: registration and kept fresh by the S5b deadman's own traffic. It is
    #: the only clock in ss8's eviction bound, so a host that never reports
    #: is never evictable on time -- which is the correct direction.
    last_contact: datetime | None = None
    #: non-null iff the CURRENT `evicted` state was reached by `--force`,
    #: carrying the actor that claimed it. Not a copy of the log's actor
    #: field: the log records who sent every command, this records the one
    #: fact that changes how the whole estate must be read -- work was
    #: rerouted without proof the old executor was dead. ss8 promises that
    #: is "loud, durable and attributable", and a fact you have to grep a
    #: WAL for is not loud.
    forced_by: str | None = None
    #: what quarantine interrupted, so that clearing it restores the
    #: OPERATOR's intent rather than overriding it (DL-97). ss8 gives
    #: `quarantined` to the leader and the other states to the operator, and
    #: a host that was drained before it stopped answering must still be
    #: drained when it answers again -- otherwise a network blip silently
    #: undoes a maintenance window. Non-null only while `state` is
    #: `quarantined`.
    state_before_quarantine: HostState | None = None
    #: DL-94: this entity's revision, on the same rule as the rows above.
    state_rev: int = 0


#: The fields whose change makes an entity's revision move. DERIVED as
#: "everything on the model except these", not enumerated, so a field added
#: later is projected by DEFAULT -- over-approximating the projection costs a
#: spurious revision, under-approximating loses a conflict (concurrency-model
#: ss3, and the DL-83 discipline that a gate must not silently narrow).
#:
#: `state_rev` is the only exclusion the models carry today. The others ss3
#: names -- `watching`, log locations, catalog metadata, `spec_drift` -- are
#: effect or disk state that lives runner-side and never entered these rows;
#: if one ever does, its name belongs here with a reason.
_UNPROJECTED: frozenset[str] = frozenset({"state_rev"})
#: ss3's other exclusion class, reaching the host row (DL-95): `last_contact`
#: is liveness that moves with relay traffic and no committed input -- the
#: same category ss3 names `watching` for. Projecting it would put a revision
#: on every lease renewal, which makes an operator's `expect` on a host
#: unholdable and the WAL a heartbeat log. Excluding it is safe in the
#: direction that matters: a fresher contact only ever DELAYS an eviction,
#: and a replay re-seeds it at resume time, which is fresher still.
#: and DL-133's other half (period-model ss3.3, PR-24b): `deadman_s` is the
#: OBSERVED liveness configuration, read back from the host and never
#: declared by the leader. `register_host` moves it, startup registers with
#: no journal record, and a projected `deadman_s` therefore moves a revision
#: audit cannot derive -- replaying from a seal that says revision 5, audit
#: could not produce the 6 the next seal carries. Nothing an operator holds
#: an `expect` against depends on it, and the eviction gate reads the
#: current row value regardless of revision.
_UNPROJECTED_HOST: frozenset[str] = _UNPROJECTED | {"last_contact", "deadman_s"}
_PROJECTED_JOB_FIELDS: tuple[str, ...] = tuple(
    name for name in JobRuntime.model_fields if name not in _UNPROJECTED
)
_PROJECTED_HOST_FIELDS: tuple[str, ...] = tuple(
    name for name in HostRuntime.model_fields if name not in _UNPROJECTED_HOST
)
_DEFAULT_JOB = JobRuntime()


def _timer_order(entry: tuple[datetime, int, Event] | tuple[datetime, int]) -> tuple[datetime, int]:
    """ss3.2's timer order, spelled ONCE (DL-145): `(due, token)` and
    nothing after it.

    The heap, the carry's install and both timer readers used to spell it
    three ways -- an explicit key, a bare `sorted` over the whole triple,
    and a projection sorted as a pair. The bare one reached the EVENT on a
    tie, which the token makes impossible today and which no reader should
    have to prove again to change a field on `Event` (DL-143's area)."""
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
    hosts: dict[str, HostRuntime] = {}
    #: ss3.2's order: `(due, token)`, never heap-array layout
    timers: tuple[tuple[datetime, int, Event], ...] = ()
    timer_seq: int = 0
    consumed: dict[str, int] = {}
    enqueue_counter: int = 0
    period_id: int = 1
    #: T. Feed times must be non-decreasing across the boundary, so the
    #: opened interpreter starts from the instant the seal was taken at
    now: datetime | None = None


# ------------------------------------------------------------- the job's machines
#
# A job row has three regions, each a declared machine (docs/state-machines.md):
# its status, its flags (ice, hold, noexec and the scheduled-tick arm), and the
# capacity it holds. Box execution and the capacity waiter are rows of the
# status table: a box has no state field of its own, and the waiter is the
# QUE_WAIT status with its rank. The rule code in `oracle.py` decides each move
# and names the transition it takes; the verbs below check it against the
# table and note a mismatch on the violation channel. They never refuse.

_ANY: frozenset[JobStatus] = JOB_STATUSES
_QUEUED: frozenset[JobStatus] = frozenset({"QUE_WAIT"})
_STARTING: frozenset[JobStatus] = frozenset({"STARTING"})
_RUNNING: frozenset[JobStatus] = frozenset({"RUNNING"})
#: a box that is not running and not sticky TERMINATED (SEM-13, SEM-15)
_IDLE_BOX: frozenset[JobStatus] = frozenset({"INACTIVE", "QUE_WAIT", "SUCCESS", "FAILURE"})

JOB_BYPASS: Final = Transition[JobStatus](
    "job_status.01",
    IDLE,
    "start",
    "SUCCESS",
    guard="not a box; the job, or a box above it, is ON_NOEXEC",
    effect="start_run; no capacity is taken",
    cite="SEM-22, DL-54",
)
JOB_START: Final = Transition[JobStatus](
    "job_status.02",
    IDLE,
    "start",
    "STARTING",
    guard="not a box; the gates hold; inside the run window; admissible",
    effect="arm term_run_time; start_run; reserve, or take over held units",
    cite="SEM-10, DL-50, DL-120",
)
JOB_QUEUE: Final = Transition[JobStatus](
    "job_status.03",
    IDLE,
    "start",
    "QUE_WAIT",
    guard="the gates hold; inside the run window; not admissible",
    effect="enqueue_waiter allocates the rank",
    cite="DL-50, DL-247, DL-255",
)
JOB_FORCE_ON_HELD: Final = Transition[JobStatus](
    "job_status.04",
    FAILED,
    "FORCE_STARTJOB",
    "STARTING",
    guard="the job holds units from an earlier run",
    effect="start on the held units and the machine load; no admission test",
    cite="DL-256",
)
JOB_RUN: Final = Transition[JobStatus](
    "job_status.05",
    _STARTING,
    "started",
    "RUNNING",
    guard="not a box; still STARTING at this run",
    cite="SEM-10",
)
JOB_WINDOW_SKIP: Final = Transition[JobStatus](
    "job_status.06",
    TERMINAL,
    "start",
    "INACTIVE",
    guard="standalone; outside run_window, closer to the previous close",
    cite="SEM-33, DL-246",
)
JOB_WINDOW_SKIP_MEMBER: Final = Transition[JobStatus](
    "job_status.07",
    TERMINAL,
    "start",
    "INACTIVE",
    guard="its box is RUNNING and it has not run there; outside run_window,"
    " closer to the previous close",
    effect="record_resolution; the box's completion door runs",
    cite="SEM-33, DL-154",
)
JOB_READMIT: Final = Transition[JobStatus](
    "job_status.08",
    _QUEUED,
    "readmit",
    "STARTING",
    guard="its box is RUNNING; not held; admissible; queued-recheck passes",
    effect="dequeue_waiter; reserve; start_run",
    cite="DL-50, DL-257",
)
JOB_CANCEL_WAITER: Final = Transition[JobStatus](
    "job_status.09",
    _QUEUED,
    "readmit",
    "INACTIVE",
    guard="its box is not RUNNING; a full scan",
    effect="dequeue_waiter",
    cite="DL-50, DL-247",
)
JOB_LEAVE_QUEUE: Final = Transition[JobStatus](
    "job_status.10",
    _QUEUED,
    "readmit",
    "INACTIVE",
    guard="queued-recheck fails",
    effect="disarm; dequeue_waiter; void_resolution; then defer or skip",
    cite="DL-257",
)
JOB_KILL_QUEUED: Final = Transition[JobStatus](
    "job_status.11",
    _QUEUED,
    "KILLJOB",
    "TERMINATED",
    effect="dequeue_waiter; disarm",
    cite="DL-50, DL-54",
)
JOB_ICE_QUEUED: Final = Transition[JobStatus](
    "job_status.12",
    _QUEUED,
    "ON_ICE",
    "INACTIVE",
    effect="set ON_ICE; dequeue_waiter",
    cite="DL-50, DL-285",
)
JOB_NOEXEC_QUEUED: Final = Transition[JobStatus](
    "job_status.13",
    _QUEUED,
    "ON_NOEXEC",
    "INACTIVE",
    guard="not a box; not ignored",
    effect="set ON_NOEXEC; dequeue_waiter; clear the exit code; retry the start",
    cite="DL-254",
)
JOB_KILL: Final = Transition[JobStatus](
    "job_status.14",
    LIVE,
    "KILLJOB",
    "TERMINATED",
    effect="a box kills its job_terminator members",
    cite="SEM-14",
)
JOB_TERM_RUN_TIME: Final = Transition[JobStatus](
    "job_status.15",
    _RUNNING,
    "TIMER term_run_time",
    "TERMINATED",
    guard="the timer's run is the row's run",
    effect="a box kills its job_terminator members",
    cite="autosys-semantics ss5",
)
JOB_TERMINATOR: Final = Transition[JobStatus](
    "job_status.16",
    LIVE,
    "box ends FAILURE or TERMINATED",
    "TERMINATED",
    guard="a member with job_terminator",
    effect="a box kills its own job_terminator members",
    cite="SEM-14",
)
STATUS_EXIT_SUCCESS: Final = Transition[JobStatus](
    "job_status.17",
    _ANY,
    "STATUS exit_code",
    "SUCCESS",
    guard="exit_is_success",
    effect="release the run's units on leaving STARTING or RUNNING",
    cite="SEM-09",
)
STATUS_EXIT_FAILURE: Final = Transition[JobStatus](
    "job_status.18",
    _ANY,
    "STATUS exit_code",
    "FAILURE",
    guard="not exit_is_success",
    effect="release the run's units on leaving STARTING or RUNNING",
    cite="SEM-09",
)
STATUS_INJECTED: Final = Transition[JobStatus](
    "job_status.19",
    _ANY,
    "STATUS status",
    INJECTABLE_STATUSES,
    guard="the status is not INACTIVE, or the job has no catalog entry",
    effect="release the run's units on leaving STARTING or RUNNING",
    cite="DL-13, DL-264",
)
STATUS_INACTIVE: Final = Transition[JobStatus](
    "job_status.20",
    _ANY,
    "STATUS INACTIVE",
    "INACTIVE",
    guard="a catalog job, not a box",
    effect="record_resolution when its box is RUNNING; SEM-15 on an idle box",
    cite="DL-242, DL-235",
)
NOEXEC_COMPLETED: Final = Transition[JobStatus](
    "job_status.21",
    FAILED,
    "ON_NOEXEC",
    "INACTIVE",
    guard="not a box; not ignored",
    effect="set ON_NOEXEC; clear the exit code",
    cite="SEM-22, DL-243",
)
BOX_RESET: Final = Transition[JobStatus](
    "job_status.22",
    TERMINAL,
    "box start",
    "INACTIVE",
    guard="contained; not live or queued",
    effect="one batch around the box's STARTING; clear the exit code",
    cite="SEM-10, DL-242",
)
BOX_CASCADE: Final = Transition[JobStatus](
    "job_status.23",
    JOB_STATUSES - {"INACTIVE"},
    "box set INACTIVE",
    "INACTIVE",
    guard="contained",
    effect="one batch with the box's own row; the box runs inside lose their arms",
    cite="SEM-18, DL-242",
)
BOX_START: Final = Transition[JobStatus](
    "job_status.24",
    IDLE,
    "start",
    "STARTING",
    guard="a box; the gates hold; inside the run window; admissible",
    effect="start_run; reset the contained jobs around this write",
    cite="SEM-10, DL-242",
)
BOX_RUN: Final = Transition[JobStatus](
    "job_status.25",
    _STARTING,
    "started",
    "RUNNING",
    guard="a box; still STARTING at this run",
    effect="auto_hold members; attempt every member; decide the run windows",
    cite="SEM-10, SEM-33, DL-246",
)
BOX_SUCCESS_OVERRIDE: Final = Transition[JobStatus](
    "job_status.26",
    _RUNNING,
    "member moves",
    "SUCCESS",
    guard="box_success holds",
    cite="SEM-12",
)
BOX_FAILURE_OVERRIDE: Final = Transition[JobStatus](
    "job_status.27",
    _RUNNING,
    "member moves",
    "FAILURE",
    guard="box_failure holds",
    effect="kill the job_terminator members",
    cite="SEM-12, SEM-14",
)
BOX_FOLD_SUCCESS: Final = Transition[JobStatus](
    "job_status.28",
    _RUNNING,
    "completion moment",
    "SUCCESS",
    guard="every member done; no failed vote; no box_success",
    cite="SEM-11",
)
BOX_FOLD_FAILURE: Final = Transition[JobStatus](
    "job_status.29",
    _RUNNING,
    "completion moment",
    "FAILURE",
    guard="every member done; a failed vote; no box_failure",
    effect="kill the job_terminator members",
    cite="SEM-11, SEM-14",
)
BOX_TERMINATOR: Final = Transition[JobStatus](
    "job_status.30",
    _RUNNING,
    "member ends FAILURE or TERMINATED",
    "TERMINATED",
    guard="the member has box_terminator; a TERMINATED end only under"
    " box-terminator-on-terminated=true",
    effect="kill the job_terminator members",
    cite="SEM-14",
)
#: The idle-box re-derive by its verdict: the status differs, so each row's
#: sources leave out its own target (SEM-15)
IDLE_BOX_OVERRIDE: Final[Mapping[JobStatus, Transition[JobStatus]]] = MappingProxyType(
    {
        "SUCCESS": Transition[JobStatus](
            "job_status.31",
            _IDLE_BOX - {"SUCCESS"},
            "member ends, or a member set INACTIVE",
            "SUCCESS",
            guard="every member that is not INACTIVE is terminal; box_success holds",
            cite="SEM-15",
        ),
        "FAILURE": Transition[JobStatus](
            "job_status.32",
            _IDLE_BOX - {"FAILURE"},
            "member ends, or a member set INACTIVE",
            "FAILURE",
            guard="every member that is not INACTIVE is terminal; box_failure holds",
            cite="SEM-15",
        ),
    }
)
IDLE_BOX_DERIVE: Final[Mapping[JobStatus, Transition[JobStatus]]] = MappingProxyType(
    {
        "SUCCESS": Transition[JobStatus](
            "job_status.33",
            _IDLE_BOX - {"SUCCESS"},
            "member ends, or a member set INACTIVE",
            "SUCCESS",
            guard="every member that is not INACTIVE is terminal; no override holds;"
            " no member failed; no box_success",
            cite="SEM-15, DL-242",
        ),
        "FAILURE": Transition[JobStatus](
            "job_status.34",
            _IDLE_BOX - {"FAILURE"},
            "member ends, or a member set INACTIVE",
            "FAILURE",
            guard="every member that is not INACTIVE is terminal; no override holds;"
            " a member failed; no box_failure",
            cite="SEM-15, DL-242",
        ),
    }
)
BOX_STATUS_INACTIVE: Final = Transition[JobStatus](
    "job_status.35",
    _ANY,
    "STATUS INACTIVE",
    "INACTIVE",
    guard="a box",
    effect="the SEM-18 cascade, one batch",
    cite="SEM-18, DL-242",
)
BOX_NOEXEC: Final = Transition[JobStatus](
    "job_status.36",
    JOB_STATUSES - {"RUNNING"},
    "ON_NOEXEC",
    "INACTIVE",
    guard="a box; not ignored; a job in its tree is not INACTIVE, or its parent is RUNNING",
    effect="flag the tree; the SEM-18 cascade, one batch",
    cite="DL-254",
)


def _stays(
    first: int,
    trigger: str,
    statuses: tuple[JobStatus, ...],
    *,
    guard: str,
    effect: str,
    cite: str,
) -> Mapping[JobStatus, Transition[JobStatus]]:
    """Internal transitions (UML: an effect with no state change), one per
    status the event meets, each with that status as its source and its
    target. Ids run from `first` in the order given. `RuntimeState.stay`
    picks the row by the job's status."""
    return MappingProxyType(
        {
            status: Transition[JobStatus](
                f"job_status.{first + index}",
                frozenset({status}),
                trigger,
                status,
                guard=guard,
                effect=effect,
                cite=cite,
            )
            for index, status in enumerate(statuses)
        }
    )


_IDLE_ORDER: tuple[JobStatus, ...] = ("INACTIVE", "SUCCESS", "FAILURE", "TERMINATED")
_ALL_ORDER: tuple[JobStatus, ...] = (
    "INACTIVE",
    "QUE_WAIT",
    "STARTING",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
)

#: KILLJOB on a job that is not running or queued kills nothing
KILL_IGNORED: Final = _stays(
    37,
    "KILLJOB",
    _IDLE_ORDER,
    guard="not running or queued",
    effect="an EVENT_IGNORED trace line",
    cite="DL-64, DL-81",
)
#: an explicit start the SEM-10 gates refuse, and a deferred start or scan
#: that has gone stale
START_REFUSED: Final = _stays(
    41,
    "start refused",
    _ALL_ORDER,
    guard="already live or queued; a member whose box is not RUNNING, or that ran in this"
    " box execution, or was taken off ice in it; a stale deferred start or scan",
    effect="a START_REFUSED trace line",
    cite="DL-64, DL-81, DL-246, DL-257, SEM-20",
)
ICE_IGNORED: Final = _stays(
    48,
    "ON_ICE",
    ("STARTING", "RUNNING"),
    guard="the vendor ignores it for a live job",
    effect="an EVENT_IGNORED trace line",
    cite="DL-254",
)
HOLD_IGNORED: Final = _stays(
    50,
    "ON_HOLD",
    ("STARTING", "RUNNING"),
    guard="the vendor ignores it for a live job",
    effect="an EVENT_IGNORED trace line",
    cite="DL-254",
)
NOEXEC_IGNORED: Final = _stays(
    52,
    "ON_NOEXEC",
    ("INACTIVE", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED", "QUE_WAIT"),
    guard="the job is ON_ICE; a job STARTING or RUNNING; a box RUNNING, or with a job"
    " inside that is ON_ICE, live or queued (a queued box only so)",
    effect="an EVENT_IGNORED trace line",
    cite="DL-254",
)

JOB_STATUS: Final[StateMachine[JobStatus]] = StateMachine(
    name="job_status",
    states=JOB_STATUSES,
    initial="INACTIVE",
    finals=frozenset(),
    transitions=(
        JOB_BYPASS,
        JOB_START,
        JOB_QUEUE,
        JOB_FORCE_ON_HELD,
        JOB_RUN,
        JOB_WINDOW_SKIP,
        JOB_WINDOW_SKIP_MEMBER,
        JOB_READMIT,
        JOB_CANCEL_WAITER,
        JOB_LEAVE_QUEUE,
        JOB_KILL_QUEUED,
        JOB_ICE_QUEUED,
        JOB_NOEXEC_QUEUED,
        JOB_KILL,
        JOB_TERM_RUN_TIME,
        JOB_TERMINATOR,
        STATUS_EXIT_SUCCESS,
        STATUS_EXIT_FAILURE,
        STATUS_INJECTED,
        STATUS_INACTIVE,
        NOEXEC_COMPLETED,
        BOX_RESET,
        BOX_CASCADE,
        BOX_START,
        BOX_RUN,
        BOX_SUCCESS_OVERRIDE,
        BOX_FAILURE_OVERRIDE,
        BOX_FOLD_SUCCESS,
        BOX_FOLD_FAILURE,
        BOX_TERMINATOR,
        *IDLE_BOX_OVERRIDE.values(),
        *IDLE_BOX_DERIVE.values(),
        BOX_STATUS_INACTIVE,
        BOX_NOEXEC,
        *KILL_IGNORED.values(),
        *START_REFUSED.values(),
        *ICE_IGNORED.values(),
        *HOLD_IGNORED.values(),
        *NOEXEC_IGNORED.values(),
    ),
)

#: A `job_flags` state is one region's value. The four regions are orthogonal,
#: so the machine has no single initial state: each region starts off, unless
#: the catalog's initial status seeds it (SEM-24), and a seed is not a move.
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
_FLAG_STATE: Final[Mapping[tuple[str, bool], FlagState]] = MappingProxyType(
    {field: state for state, field in FLAG_FIELDS.items()}
)

ICE_ON: Final = Transition[FlagState](
    "job_flags.01",
    frozenset({"ice_off", "ice_on"}),
    "ON_ICE",
    "ice_on",
    guard="not STARTING or RUNNING (else ignored); a second ice changes nothing",
    effect="a queued job leaves the queue; a first ice on a member is a completion moment",
    cite="SEM-20, DL-254, DL-285",
)
ICE_OFF: Final = Transition[FlagState](
    "job_flags.02",
    frozenset({"ice_on", "ice_off"}),
    "OFF_ICE",
    "ice_off",
    effect="no re-evaluation: conditions must reoccur. From ice_on only, under"
    " off-ice-in-running-box=next-run: a member of a RUNNING box that has not run there"
    " sits that run out",
    cite="SEM-20",
)
FORCE_CLEARS_ICE: Final = Transition[FlagState](
    "job_flags.03",
    frozenset({"ice_on"}),
    "FORCE_STARTJOB",
    "ice_off",
    guard="not live",
    effect="the start goes on",
    cite="SEM-23, DL-243",
)
HOLD_ON: Final = Transition[FlagState](
    "job_flags.04",
    frozenset({"hold_off", "hold_on"}),
    "ON_HOLD",
    "hold_on",
    guard="not STARTING or RUNNING (else ignored)",
    effect="a held waiter blocks no one",
    cite="SEM-21, DL-254, DL-247",
)
HOLD_OFF: Final = Transition[FlagState](
    "job_flags.05",
    frozenset({"hold_on", "hold_off"}),
    "OFF_HOLD",
    "hold_off",
    effect="attempt the start, or wake the queue for a queued job",
    cite="SEM-21, DL-50",
)
FORCE_CLEARS_HOLD: Final = Transition[FlagState](
    "job_flags.06",
    frozenset({"hold_on"}),
    "FORCE_STARTJOB",
    "hold_off",
    guard="not live",
    effect="the start goes on",
    cite="SEM-23, DL-243",
)
NOEXEC_CLEARS_HOLD: Final = Transition[FlagState](
    "job_flags.07",
    frozenset({"hold_on"}),
    "ON_NOEXEC",
    "hold_off",
    guard="not ignored",
    effect="ON_NOEXEC supersedes ON_HOLD; the start is retried",
    cite="DL-254",
)
AUTO_HOLD: Final = Transition[FlagState](
    "job_flags.08",
    frozenset({"hold_off"}),
    "box start",
    "hold_on",
    guard="a member with auto_hold",
    cite="autosys-semantics ss5",
)
NOEXEC_ON: Final = Transition[FlagState](
    "job_flags.09",
    frozenset({"noexec_off", "noexec_on"}),
    "ON_NOEXEC",
    "noexec_on",
    guard="not ignored; the job, or every job in a box's tree",
    cite="SEM-22, DL-254",
)
NOEXEC_OFF: Final = Transition[FlagState](
    "job_flags.10",
    frozenset({"noexec_on", "noexec_off"}),
    "OFF_NOEXEC",
    "noexec_off",
    cite="DL-243",
)
BOX_NOEXEC_OFF: Final = Transition[FlagState](
    "job_flags.11",
    frozenset({"noexec_on"}),
    "OFF_NOEXEC on its box",
    "noexec_off",
    guard="contained in the box",
    cite="DL-254",
)
ARM: Final = Transition[FlagState](
    "job_flags.12",
    frozenset({"arm_off"}),
    "scheduled tick blocked",
    "arm_on",
    guard="held or its condition false; a schedule; a member's box is RUNNING",
    effect="a SCHED_ARM trace line",
    cite="SEM-32, DL-54",
)
START_CONSUMES_ARM: Final = Transition[FlagState](
    "job_flags.13",
    frozenset({"arm_on", "arm_off"}),
    "actual start",
    "arm_off",
    effect="start_run",
    cite="DL-54",
)
DISARM: Final = Transition[FlagState](
    "job_flags.14",
    frozenset({"arm_on", "arm_off"}),
    "DISARM",
    "arm_off",
    effect="a DISARM trace line",
    cite="DL-158",
)
BOX_END_DISARMS: Final = Transition[FlagState](
    "job_flags.15",
    frozenset({"arm_on"}),
    "box run ends",
    "arm_off",
    guard="a member; its box reaches a terminal status or is set INACTIVE",
    effect="a SCHED_DISARM trace line",
    cite="DL-54",
)
QUEUE_LEFT_DISARMS: Final = Transition[FlagState](
    "job_flags.16",
    frozenset({"arm_on"}),
    "leave QUE_WAIT unstarted",
    "arm_off",
    effect="a SCHED_DISARM trace line",
    cite="DL-257",
)
KILL_DISARMS: Final = Transition[FlagState](
    "job_flags.17",
    frozenset({"arm_on", "arm_off"}),
    "KILLJOB on QUE_WAIT",
    "arm_off",
    cite="DL-54",
)

JOB_FLAGS: Final[StateMachine[FlagState]] = StateMachine(
    name="job_flags",
    states=frozenset(FLAG_FIELDS),
    initial=None,
    finals=frozenset(),
    transitions=(
        ICE_ON,
        ICE_OFF,
        FORCE_CLEARS_ICE,
        HOLD_ON,
        HOLD_OFF,
        FORCE_CLEARS_HOLD,
        NOEXEC_CLEARS_HOLD,
        AUTO_HOLD,
        NOEXEC_ON,
        NOEXEC_OFF,
        BOX_NOEXEC_OFF,
        ARM,
        START_CONSUMES_ARM,
        DISARM,
        BOX_END_DISARMS,
        QUEUE_LEFT_DISARMS,
        KILL_DISARMS,
    ),
)

#: A `job_holding` state: `reserved` is a run's vector, frozen at its
#: admission; `held` is what a renewable's policy kept on the row after the
#: run (DL-256). The verbs that write `reservations` name the move.
type HoldingState = Literal["none", "reserved", "held"]

RESERVE: Final = Transition[HoldingState](
    "job_holding.01",
    frozenset({"none"}),
    "admitted start",
    "reserved",
    guard="the vector is not empty",
    effect="reserve",
    cite="DL-120",
)
TAKE_OVER_HELD: Final = Transition[HoldingState](
    "job_holding.02",
    frozenset({"held"}),
    "admitted start",
    frozenset({"reserved", "none"}),
    guard="the job holds units from an earlier run; none when the new vector is empty",
    effect="take_over_held: the new vector replaces the held units",
    cite="DL-256",
)
FORCE_ON_HELD_UNITS: Final = Transition[HoldingState](
    "job_holding.03",
    frozenset({"held"}),
    "FORCE_STARTJOB",
    "reserved",
    guard="FAILURE or TERMINATED",
    effect="take_over_held: the held units plus the machine load",
    cite="DL-256",
)
RELEASE_ALL: Final = Transition[HoldingState](
    "job_holding.04",
    frozenset({"reserved"}),
    "leave STARTING or RUNNING",
    "none",
    guard="the policy frees every unit",
    cite="DL-50, DL-120",
)
RELEASE_KEEP_HELD: Final = Transition[HoldingState](
    "job_holding.05",
    frozenset({"reserved"}),
    "leave STARTING or RUNNING",
    "held",
    guard="a renewable's unit is not freed and is kept",
    effect="what is neither freed nor kept is spent",
    cite="DL-256",
)
RELEASE_SPEND: Final = Transition[HoldingState](
    "job_holding.06",
    frozenset({"reserved"}),
    "leave STARTING or RUNNING",
    "none",
    guard="a unit is not freed and none is kept",
    effect="consumed += the units not freed",
    cite="SEM-16, DL-120",
)
HELD_RELEASED: Final = Transition[HoldingState](
    "job_holding.07",
    frozenset({"held"}),
    "RELEASE_RESOURCE",
    "none",
    guard="not live",
    effect="wake the waiters",
    cite="DL-256",
)
HELD_RELEASED_AT_OPENING: Final = Transition[HoldingState](
    "job_holding.08",
    frozenset({"held"}),
    "first input of a period",
    "none",
    guard="the job left the catalog",
    effect="wake the waiters",
    cite="DL-256",
)

JOB_HOLDING: Final[StateMachine[HoldingState]] = StateMachine(
    name="job_holding",
    states=frozenset({"none", "reserved", "held"}),
    initial="none",
    finals=frozenset(),
    transitions=(
        RESERVE,
        TAKE_OVER_HELD,
        FORCE_ON_HELD_UNITS,
        RELEASE_ALL,
        RELEASE_KEEP_HELD,
        RELEASE_SPEND,
        HELD_RELEASED,
        HELD_RELEASED_AT_OPENING,
    ),
)

# --------------------------------------------------------------- assembly
#
# How a `RuntimeState` is put together before it runs, and the open input.
# One phase replaces four flags. A move the table does not declare is
# refused: these moves are not inputs, so a violation has no channel to ride
# and raises, as an anchor move does (DL-224).

type AssemblyPhase = Literal[
    "fresh", "installed", "genesis_input", "genesis", "constructed", "seeded", "input", "live"
]
#: the phases with an input open
_OPEN: Final[frozenset[AssemblyPhase]] = frozenset({"genesis_input", "input"})

_INSTALL: Final = Transition[AssemblyPhase](
    "runtime_assembly.01",
    frozenset({"fresh"}),
    "install",
    "installed",
    guard="nothing installed, committed or seeded",
    effect="carried rows land verbatim, revisions included",
    cite="period-model ss7",
)
_BEGIN_GENESIS: Final = Transition[AssemblyPhase](
    "runtime_assembly.02",
    frozenset({"fresh", "installed", "genesis"}),
    "begin_input",
    "genesis_input",
    guard="construction is not finished",
    effect="open the genesis seed's input",
    cite="DL-87",
)
_COMMIT_GENESIS: Final = Transition[AssemblyPhase](
    "runtime_assembly.03",
    frozenset({"genesis_input"}),
    "commit_input",
    "genesis",
    effect="one revision per changed entity",
    cite="DL-87",
)
_FINISH_GENESIS: Final = Transition[AssemblyPhase](
    "runtime_assembly.04",
    frozenset({"fresh", "installed", "genesis"}),
    "finish_genesis",
    "constructed",
    guard="once; not seeded",
    effect="the genesis seed is not an input to the seed latch",
    cite="DL-132",
)
_SEED_PERIOD: Final = Transition[AssemblyPhase](
    "runtime_assembly.05",
    frozenset({"fresh", "installed", "constructed"}),
    "seed_period",
    "seeded",
    guard="the period is 1 or more",
    effect="set the period id",
    cite="period-model ss3.5, DL-132",
)
_BEGIN_INPUT: Final = Transition[AssemblyPhase](
    "runtime_assembly.06",
    frozenset({"constructed", "seeded", "live"}),
    "begin_input",
    "input",
    effect="drop orphan violations; snapshot on first touch",
    cite="DL-87, concurrency-model ss3",
)
_COMMIT_INPUT: Final = Transition[AssemblyPhase](
    "runtime_assembly.07",
    frozenset({"input"}),
    "commit_input",
    "live",
    effect="check capacity; one revision per changed entity",
    cite="DL-87, DL-120",
)

RUNTIME_ASSEMBLY: Final[StateMachine[AssemblyPhase]] = StateMachine(
    name="runtime_assembly",
    states=frozenset(
        {"fresh", "installed", "genesis_input", "genesis", "constructed", "seeded", "input", "live"}
    ),
    initial="fresh",
    finals=frozenset(),
    transitions=(
        _INSTALL,
        _BEGIN_GENESIS,
        _COMMIT_GENESIS,
        _FINISH_GENESIS,
        _SEED_PERIOD,
        _BEGIN_INPUT,
        _COMMIT_INPUT,
    ),
)


class RuntimeState:
    """The authoritative state of one Oracle: job rows, global rows, and the
    timer heap. SEM-01 latching applies throughout -- a recorded status is
    current regardless of age.

    DL-82 made this the single write path; DL-86 makes escape IMPOSSIBLE
    rather than merely detected. The rows are frozen, the maps are private
    and published only as read-only views, and every write goes through a
    verb that names what changed (`transition`, `start_run`, `move_flag`,
    `set_global`, `enqueue_timer`, and the capacity verbs: DL-120's
    `reserve`, `release_reservations`, `enqueue_waiter`, `dequeue_waiter`,
    `seed_consumed`, and DL-256's `take_over_held` and `release_held`). No
    caller assembles a field dict, so no caller can invent a field
    combination the verbs do not. The verbs that move a job's status, flags
    or held capacity take the declared transition of that move (the job's
    machines, above); `install` and `seed_job` install rows and take none.

    The reason is the concurrency model, not tidiness. Optimistic locking
    needs one place where "this entity changed" is observable exactly once
    per applied input; assignment sites scattered across the interpreter are
    not that place, and a before/after property test cannot substitute for
    it -- `_run` mutates armed, run_number and started_by and then sets
    status twice, so a single missed site stays invisible to any check that
    observes whole feeds rather than writes.

    **The timer heap lives here too** (concurrency-model ss3). It is
    authoritative state that a status projection does not cover: an armed
    must_start deadline on a REFUSED start changes no job row at all, so a
    projection over rows alone would replay a different schedule. The heap
    is ordered globally by `(due, insertion token)` and every job's own
    timers carry that token, so the cross-job order of equal-time timers --
    which decides resource release, box cascades and which job starts -- is
    recoverable per entity rather than only in aggregate.

    **The capacity state lives here too** (DL-120, period-model ss5). The
    held half is on the rows as `reservations`; the spent half is `consumed`,
    which belongs to a bucket rather than to any completed job; the waiter
    rank is on the row and its allocator is `enqueue_counter`. What is NOT
    here is any sum of the two: `CapacityPool` computes usage from these, so
    the number a seal carries is one nobody has to explain."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRuntime] = {}
        self._globals: dict[str, GlobalRuntime] = {}
        #: DL-94: the ss8 routing table. Under this owner rather than beside
        #: it because durability, replay, the one-increment-per-input rule,
        #: the ss0 precondition check and the read verbs are all machinery
        #: that already exists here and works on namespaced keys.
        self._hosts: dict[str, HostRuntime] = {}
        self._timers: list[tuple[datetime, int, Event]] = []  # heap of (due, token, ev)
        self._timer_seq = 0
        #: which period this state machine is running (period-model ss3.5,
        #: DL-132). PRIVATE, set only by `seed_period` -- 1 until a seal
        #: exists to open a later one -- and stamped onto every row's
        #: `start_period` at its actual start. Not per-input state: a period
        #: opens in a new state built over the carried rows (`Oracle`).
        self._period_id: int = 1
        #: the `runtime_assembly` phase: how far assembly has come, and
        #: whether an input is open. Seed-versus-advance is EXPLICIT, never
        #: inferred from the rows: `seed_period` is legal exactly once,
        #: before any committed input.
        self._phase: AssemblyPhase = "fresh"
        #: DL-120: bucket key -> units PERMANENTLY spent (SEM-16 depletion,
        #: and `never`/unmet-`success` holds a terminal transition kept). The
        #: held half lives on the rows; this half belongs to no row, which is
        #: exactly why a seal that recomputed usage from holders refunded it.
        #: Keys survive their resource: a bucket the catalog no longer sizes
        #: keeps its entry and still counts if the resource returns (PR-19a).
        self._consumed: dict[str, int] = {}
        #: DL-120: the waiter-rank allocator's high-water mark. Carried, never
        #: renormalised -- redefining the rank as `1 + max(active)` would buy
        #: one integer in exchange for proving that renormalisation equals
        #: genesis replay (period-model ss5).
        self._enqueue_counter = 0
        #: DL-87 input transaction: entity key -> its projection at FIRST
        #: touch within the open input. Empty and inert outside one.
        self._snapshots: dict[str, object] = {}
        #: the violation channel: (subject, violation) pairs since the last
        #: drain. `InputBatch` drains it; `begin_input` drops orphans.
        self._violations: list[tuple[str, Violation]] = []

    def fork(self) -> RuntimeState:
        """An independent copy to dry-apply one input on (concurrency-model
        ss4). Shallow: rows are frozen and a write replaces a row, so copying
        each map and the heap list keeps every write on the copy. Heap events
        are shared; no verb changes one in place. The violation channel starts
        empty: an orphan must not refuse the input being dry-applied."""
        twin = copy.copy(self)
        twin._jobs = dict(self._jobs)
        twin._globals = dict(self._globals)
        twin._hosts = dict(self._hosts)
        twin._timers = list(self._timers)
        twin._consumed = dict(self._consumed)
        twin._snapshots = dict(self._snapshots)
        twin._violations = []
        return twin

    # ------------------------------------------------------- the violation channel

    def note_violation(self, subject: str, violation: Violation | None) -> None:
        """Collect what `StateMachine.take` returned, so a call site is one
        line: `store.note_violation(job, MACHINE.take(t, old, new))`. None,
        a move that matched its declaration, is not collected.

        `subject` is the entity the move belongs to, as the trace names it:
        a job name, or a namespaced key such as `host:local` for a row that
        is not a job."""
        if violation is not None:
            self._violations.append((subject, violation))

    def drain_violations(self) -> list[tuple[str, Violation]]:
        """Hand over and forget every violation noted since the last drain."""
        drained, self._violations = self._violations, []
        return drained

    # ------------------------------------------------------------------- reads

    @staticmethod
    def job_key(job: str) -> str:
        """`expect` addresses entities by namespaced key (concurrency-model
        ss6). The transaction uses the same key space, so S3's precondition
        check is a dict lookup rather than a translation layer."""
        return f"job:{job}"

    @staticmethod
    def global_key(name: str) -> str:
        return f"global:{name}"

    @staticmethod
    def host_key(host_id: str) -> str:
        """The third ss6 namespace (DL-93). A drain is an externally
        requested mutation of published state like any other, so it names
        the revision it was composed against in the same key space."""
        return f"host:{host_id}"

    def revision(self, key: str) -> int:
        """The revision an `expect` must name for `key`. An entity that does
        not exist reads 0 -- which is what makes a conditional create
        expressible: `expect {"global:X": 0}` means "still absent", because
        anything that exists has been through an input and is at 1 or more
        (the catalog seed is itself one input, see Oracle.__init__)."""
        namespace, _, name = key.partition(":")
        if namespace == "job":
            row = self._jobs.get(name)
            return 0 if row is None else row.state_rev
        if namespace == "global":
            grow = self._globals.get(name)
            return 0 if grow is None else grow.state_rev
        if namespace == "host":
            hrow = self._hosts.get(name)
            return 0 if hrow is None else hrow.state_rev
        raise OracleError(f"unknown entity namespace in {key!r}")

    @property
    def job(self) -> Mapping[str, JobRuntime]:
        """Read-only view of the job rows. A proxy, not the map: an attempt
        to write through it raises rather than diverging silently."""
        return MappingProxyType(self._jobs)

    @property
    def globals_(self) -> Mapping[str, GlobalRuntime]:
        return MappingProxyType(self._globals)

    @property
    def hosts(self) -> Mapping[str, HostRuntime]:
        return MappingProxyType(self._hosts)

    def host(self, host_id: str) -> HostRuntime | None:
        """Read one routing row, or None. Deliberately NOT create-on-demand,
        unlike `runtime()`: the oracle addresses job entities that no catalog
        declares, but a host the table does not know is a host the ss7
        takeover barrier would never reconcile, so it must read as absent
        rather than spring into existence at its default `active`."""
        return self._hosts.get(host_id)

    def runtime(self, job: str) -> JobRuntime:
        """Read access. Creates the row on demand -- the oracle addresses
        entities with no catalog entry (pseudo-entries `name^INST`, and
        whatever a CHANGE_STATUS invents)."""
        if job not in self._jobs:
            self._jobs[job] = JobRuntime()
        return self._jobs[job]

    def global_value(self, name: str) -> str | None:
        row = self._globals.get(name)
        return None if row is None else row.value

    @property
    def consumed(self) -> Mapping[str, int]:
        """Read-only view of the permanently spent units per bucket (DL-120).
        A proxy for `job`'s reason: `CapacityPool` reads it on every admission
        test and must not be able to write it."""
        return MappingProxyType(self._consumed)

    @property
    def enqueue_counter(self) -> int:
        """The last waiter rank allocated (DL-120). Read-only: a rank is
        allocated by `enqueue_waiter` and by nothing else."""
        return self._enqueue_counter

    @property
    def timer_seq(self) -> int:
        """The last timer token allocated (period-model ss3.1). Read-only,
        for `enqueue_counter`'s reason -- a token is allocated by
        `enqueue_timer` and by nothing else -- and readable because a seal
        carries the high-water mark: the heap can be empty while the
        allocator stands at 41, and an opener that restarted from 0 would
        re-issue tokens the carried firing order was written in."""
        return self._timer_seq

    # ------------------------------------------------ the input transaction (DL-87)

    def _projection(self, key: str) -> object:
        """An entity's SEMANTIC value: what a revision is a revision OF.

        A job's is its projected fields plus its own timers WITH their
        ordering tokens -- an armed deadline is state that no status field
        records (concurrency-model ss3). A missing job projects as the
        default row, so merely reading an entity into existence is not a
        change; a missing global or host projects as None, so first-set IS
        one and `revision() == 0` can mean "absent"."""
        namespace, _, name = key.partition(":")
        if namespace == "job":
            row = self._jobs.get(name) or _DEFAULT_JOB
            fields = tuple(getattr(row, field) for field in _PROJECTED_JOB_FIELDS)
            return (fields, tuple(self.timers_for(name)))
        if namespace == "host":
            hrow = self._hosts.get(name)
            return (
                None
                if hrow is None
                else tuple(getattr(hrow, field) for field in _PROJECTED_HOST_FIELDS)
            )
        grow = self._globals.get(name)
        return None if grow is None else grow.value

    def _touch(self, key: str) -> None:
        """Record an entity's pre-input projection the first time an open
        input reaches it. Snapshot-on-first-touch rather than snapshot-all:
        the projection is a function of the rows and the heap, both of which
        only move through the mutators below, so the touched set cannot be
        under-approximated by construction -- which is the direction ss3
        says must not be got wrong."""
        if self._phase in _OPEN and key not in self._snapshots:
            self._snapshots[key] = self._projection(key)

    def begin_input(self) -> None:
        """Open one input's transaction. Inputs do not nest: the oracle's
        cascade, its fired timers and its box folds are all consequences of
        ONE input and share its revision, which is what makes `expect`
        checkable -- a client that read revision 12 must be invalidated by
        the whole of the next input, not by its first transition."""
        if self._phase in _OPEN:
            raise OracleError("input already open: inputs do not nest")
        self._drop_orphan_violations()
        constructing = self._phase in _BEGIN_GENESIS.source
        self._assemble(_BEGIN_GENESIS if constructing else _BEGIN_INPUT)
        self._snapshots = {}

    def _drop_orphan_violations(self) -> None:
        """Only an `InputBatch` drains the channel. A violation noted
        outside one (with no transaction open, or in a writer that opens its
        own: the Oracle's genesis seed, classify's seeding, the executor
        seed) would blame this input with a trace line replay never writes.
        Strict test runs raise, so the suite proves every `take` runs inside
        an `InputBatch`; production writes it once to stderr and drops it."""
        orphans = self.drain_violations()
        if not orphans:
            return
        if os.environ.get(STRICT_ENV):
            raise TransitionError(
                f"{len(orphans)} transition violation(s) noted outside an InputBatch, first"
                f" {orphans[0][1].transition} on {orphans[0][0]}"
            )
        for subject, violation in orphans:
            sys.stderr.write(
                f"{VIOLATION_LOG_PREFIX} noted outside an InputBatch, dropped:"
                f" {subject} {violation.transition} {violation.old}->{violation.new}:"
                f" {violation.reason}\n"
            )

    def commit_input(self) -> list[str]:
        """Close the transaction and increment each CHANGED entity exactly
        once. Returns the changed keys in a stable order -- S2's outbox and
        `ApplyResult` need the list, not just the effect. Committing with no
        input open is refused: the table has no such move."""
        genesis = self._phase == "genesis_input"
        self._assemble(_COMMIT_GENESIS if genesis else _COMMIT_INPUT)
        snapshots, self._snapshots = self._snapshots, {}
        self._check_capacity(snapshots)
        changed = [key for key, before in snapshots.items() if self._projection(key) != before]
        for key in sorted(changed):
            namespace, _, name = key.partition(":")
            if namespace == "job":
                self._replace(name, state_rev=self.runtime(name).state_rev + 1)
            elif namespace == "host":
                self._replace_host(name, state_rev=self._hosts[name].state_rev + 1)
            else:
                row = self._globals[name]
                self._globals[name] = GlobalRuntime(value=row.value, state_rev=row.state_rev + 1)
        return sorted(changed)

    def _check_capacity(self, snapshots: Mapping[str, object]) -> None:
        """The DL-120 capacity invariants, checked at the CLOSE of an input.

        Not inside the verbs: one input legitimately passes through states
        that break them -- a terminal transition lands the status before the
        release that follows it, an enqueue allocates the rank before the
        QUE_WAIT transition -- and only the boundary between inputs is a state
        anything else observes.

        Only the touched rows are checked. A row this input did not touch was
        checked when it was, and the counter only grows."""
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
        """The single write. `model_copy(update=)` does NOT validate -- it
        would happily store a str in `run_number` and leave the corruption
        to surface somewhere else entirely -- so the row is rebuilt through
        the model's own constructor. Pydantic refuses an undeclared field
        name, so a typo is loud rather than a silently created attribute
        nothing reads."""
        self._touch(self.job_key(job))
        self._jobs[job] = JobRuntime.model_validate({**dict(self.runtime(job)), **fields})

    def transition(
        self,
        job: str,
        t: Transition[JobStatus],
        status: JobStatus,
        at: datetime | None,
        exit_code: int | None = None,
        *,
        clear_exit_code: bool = False,
    ) -> None:
        """Record a status change, the declared `job_status` transition `t`.
        Every status write is one of these, except an install (`install`,
        `seed_job`). The check notes a mismatch on the violation channel
        and never refuses the write.

        `last_end_at` latches on every terminal transition -- the Q2 anchor
        is the job's OWN last end (DL-54) -- and an exit code is written
        only when one was reported. A box-start reset clears it instead
        (`clear_exit_code`, SEM-10, DL-242)."""
        self.note_violation(job, JOB_STATUS.take(t, self.runtime(job).status, status))
        fields: dict[str, object] = {"status": status, "status_at": at}
        if status in TERMINAL:
            fields["last_end_at"] = at
        if exit_code is not None:
            fields["exit_code"] = exit_code
        elif clear_exit_code:
            fields["exit_code"] = None
        self._replace(job, **fields)

    def stay(self, job: str, rows: Mapping[JobStatus, Transition[JobStatus]]) -> None:
        """Take an internal `job_status` transition: its effect runs, and
        the status and the row do not move (UML: an effect with no state
        change). `rows` holds one row per status the event may meet; the
        job's status picks it. A status with no row is noted against the
        first, as a move its table does not declare."""
        status = self.runtime(job).status
        t = rows.get(status) or next(iter(rows.values()))
        self.note_violation(job, JOB_STATUS.take(t, status, status))

    def start_run(self, job: str, *, cause: str, box: str | None, is_box: bool) -> None:
        """Everything one actual start changes, in one act: the run_number
        bump, the arm it consumes (Q3/DL-54 -- the ACTUAL start consumes it,
        FORCE included), the provenance of THIS run (DL-68), and the SEM-10
        box bookkeeping on both sides -- the member joins its box's ran set,
        and a box starting resets its own.

        A box that is itself a member does both, to two different rows."""
        self.move_flag(job, START_CONSUMES_ARM)
        self._replace(
            job,
            run_number=self.runtime(job).run_number + 1,
            started_by=cause,
            start_period=self._period_id,
        )
        if box is not None:
            # the member's own start voids its resolution mark (DL-242) and
            # its off-ice mark (SEM-20): a forced run votes in the fold
            box_rt = self.runtime(box)
            self._replace(
                box,
                ran_members=box_rt.ran_members | {job},
                window_skipped_members=box_rt.window_skipped_members - {job},
                iced_out_members=box_rt.iced_out_members - {job},
            )
        if is_box:
            # Reset BEFORE the caller's RUNNING transition: that transition's
            # own re-evaluation may already start members, and they must land
            # in the fresh per-run set (SEM-10 at-most-once bookkeeping).
            # The resolution and off-ice marks are per-execution too (DL-154,
            # DL-242, SEM-20).
            self._replace(
                job,
                ran_members=frozenset(),
                window_skipped_members=frozenset(),
                iced_out_members=frozenset(),
            )

    def clear_exit_code(self, job: str) -> None:
        """SEM-10 (DL-242): a box-start reset drops the previous cycle's exit
        code from a row that is already INACTIVE. Not a transition: the
        status and its time stay."""
        self._replace(job, exit_code=None)

    def record_resolution(self, box: str, member: str) -> None:
        """Mark `member` resolved for this box execution: its INACTIVE is an
        explicit verdict, a run_window skip (SEM-33/DL-154) or an injected
        STATUS INACTIVE (SEM-11, DL-242), so the SEM-11 fold completes past
        it. `start_run` on the box resets the marks; the member's own start
        voids its mark."""
        self._replace(
            box, window_skipped_members=self.runtime(box).window_skipped_members | {member}
        )

    def record_iced_out(self, box: str, member: str) -> None:
        """Mark `member` out of this box execution: it was taken off ice
        while `box` ran and before it ran there (SEM-20,
        `off-ice-in-running-box=next-run`). `start_run` on the box resets
        the marks; the member's own forced start voids its mark."""
        self._replace(box, iced_out_members=self.runtime(box).iced_out_members | {member})

    def void_resolution(self, box: str, member: str) -> None:
        """Drop `member`'s resolution mark in `box`: a fresh attempt by the
        member that ends unresolved must not inherit an earlier verdict of
        the same box run (DL-257)."""
        marks = self.runtime(box).window_skipped_members
        if member in marks:
            self._replace(box, window_skipped_members=marks - {member})

    def move_flag(self, job: str, t: Transition[FlagState]) -> None:
        """Set one SEM-20/21/22 flag, or the SEM-32 arm (DL-54), by the
        declared `job_flags` transition `t`. Its target names the flag and
        its value, so a caller cannot touch another flag."""
        target = cast(FlagState, t.target)
        field, value = FLAG_FIELDS[target]
        old = _FLAG_STATE[(field, getattr(self.runtime(job), field))]
        self.note_violation(job, JOB_FLAGS.take(t, old, target))
        self._replace(job, **{field: value})

    def seed_job(
        self,
        job: str,
        *,
        status: JobStatus = "INACTIVE",
        status_at: datetime | None = None,
        last_end_at: datetime | None = None,
        exit_code: int | None = None,
        on_ice: bool = False,
        on_hold: bool = False,
        on_noexec: bool = False,
        armed: bool = False,
    ) -> None:
        """Install a row's lifecycle fields as given: an install, like
        `install`, not a move, so no machine is taken. Two callers seed
        rows: the Oracle's genesis seed sets the SEM-24 definition-time
        flags of a new job, and classify's throwaway interpreter rebuilds
        carried rows to read condition truth. A QUE_WAIT row gets a waiter
        rank, so the input's commit finds the rank it requires."""
        self._replace(
            job,
            status=status,
            status_at=status_at,
            last_end_at=last_end_at,
            exit_code=exit_code,
            on_ice=on_ice,
            on_hold=on_hold,
            on_noexec=on_noexec,
            armed=armed,
        )
        if status == "QUE_WAIT":
            self.enqueue_waiter(job)

    def set_global(self, name: str, value: str) -> None:
        """Latch a global's value (SEM-06). The revision carries over: only
        commit_input() moves it, and only if the value actually changed --
        AutoSys's same-value SET_GLOBAL is a real and common input."""
        self._touch(self.global_key(name))
        row = self._globals.get(name)
        self._globals[name] = GlobalRuntime(
            value=value, state_rev=0 if row is None else row.state_rev
        )

    # --------------------------------------------------------- capacity (DL-120)
    #
    # `consumed` and `enqueue_counter` are authoritative state under this owner
    # with no key space of their own, and they do not need one: each changes
    # only inside an input that ALSO replaces the row of the job whose units or
    # rank moved -- a terminal transition for the first, a QUE_WAIT transition
    # for the second -- so the touched entity that carries the revision is that
    # job. They gain an `expect` namespace on the day an operator has a reason
    # to address them, which for `consumed` is SEM-16 replenishment
    # (period-model ss5).

    def reserve(self, job: str, reservations: Sequence[CapacityReservation]) -> None:
        """Record the capacity vector a start acquired (period-model ss5).

        Refuses a row that already holds one. The Oracle releases before it
        wakes anything, so a live record here at a start means a release was
        missed, and the old pool's forgiving `extend` turned that into a
        permanently stranded unit nobody could account for. A start of a job
        that still holds units from an earlier run goes through
        `take_over_held` instead (DL-256)."""
        if self.runtime(job).reservations:
            raise OracleError(f"{job!r} already holds reservations: a start may not overwrite")
        if reservations:  # reserving nothing is no move
            self.note_violation(job, JOB_HOLDING.take(RESERVE, "none", "reserved"))
        self._replace(job, reservations=tuple(reservations))

    def take_over_held(
        self,
        job: str,
        t: Transition[HoldingState],
        reservations: Sequence[CapacityReservation],
    ) -> None:
        """DL-256: a job that is not live and still holds a renewable's units
        from an earlier run starts on them. Its admission credited those
        units to it, so the new run's vector REPLACES them rather than adding
        to them, and nothing is counted twice. A live row is refused: its
        reservations are its own run's. `t` is the `job_holding` move: an
        admitted start, or a FORCE_STARTJOB on the held units."""
        row = self.runtime(job)
        if row.status in LIVE:
            raise OracleError(f"{job!r} is {row.status}: a start may not overwrite its run")
        old: HoldingState = "held" if row.reservations else "none"
        new: HoldingState = "reserved" if reservations else "none"
        self.note_violation(job, JOB_HOLDING.take(t, old, new))
        self._replace(job, reservations=tuple(reservations))

    def release_reservations(
        self,
        job: str,
        old_status: str,
        new_status: str,
        keeps_held: Callable[[str], bool] | None = None,
    ) -> None:
        """Settle a run's vector on the edge that leaves STARTING/RUNNING
        (DL-120). What the policy frees goes back to the pool. What it does
        not free is spent into `consumed`, or, when `keeps_held(bucket)` says
        so, stays on the row as units the job still holds (DL-256: a
        renewable resource's unreleased units belong to the job until
        RELEASE_RESOURCE or its next run).

        `old_status` is the status the row left: the row itself is already
        written, so only the edge says whether the units were a run's
        (`reserved`, leaving STARTING or RUNNING) or units already held.
        `new_status` is whatever the row is moving to -- a terminal status for
        every ordinary run, and INACTIVE for an injected STATUS on a live
        holder, which used to strand the units. The halves are one act
        within the input: the row write and the spend happen in the same
        input transaction, and replay re-applies the whole input, so a crash
        between them cannot leave units both held and spent or neither. `never`
        and an unmet `success` are the policies that do not free (SEM-16
        depletion, hold-on-failure); what is spent never comes back. With no
        `keeps_held`, nothing stays held: everything not freed is spent."""
        row = self.runtime(job)
        if not row.reservations:
            return
        spent: dict[str, int] = {}
        kept: list[CapacityReservation] = []
        for reservation in row.reservations:
            policy = reservation.release_policy
            if policy == "completion" or (policy == "success" and new_status == "SUCCESS"):
                continue
            if keeps_held is not None and keeps_held(reservation.bucket):
                kept.append(reservation)
            else:
                spent[reservation.bucket] = spent.get(reservation.bucket, 0) + reservation.units
        move = RELEASE_KEEP_HELD if kept else RELEASE_SPEND if spent else RELEASE_ALL
        held: HoldingState = "reserved" if old_status in LIVE else "held"
        self.note_violation(job, JOB_HOLDING.take(move, held, "held" if kept else "none"))
        self._replace(job, reservations=tuple(kept))  # validates first; the spend cannot raise
        for bucket, units in spent.items():
            self._consumed[bucket] = self._consumed.get(bucket, 0) + units

    def release_held(
        self, job: str, t: Transition[HoldingState]
    ) -> tuple[CapacityReservation, ...]:
        """RELEASE_RESOURCE (DL-256): give back every unit a job that is not
        live still holds, and return what was given back. A live row's
        reservations belong to its run and are released at its end, so it
        is refused here; the Oracle records that case as a no-op. `t` is
        the `job_holding` move: the operator's verb, or a period opening
        that gives back a removed job's units."""
        row = self.runtime(job)
        if row.status in LIVE:
            raise OracleError(f"{job!r} is {row.status}: its run's units release at its end")
        old: HoldingState = "held" if row.reservations else "none"
        self.note_violation(job, JOB_HOLDING.take(t, old, "none"))
        self._replace(job, reservations=())
        return row.reservations

    def enqueue_waiter(self, job: str) -> None:
        """Allocate this job's QUE_WAIT rank. Idempotent: a job already queued
        keeps the rank it was given, because its position was decided when it
        first failed admission."""
        if self.runtime(job).waiter_seq is not None:
            return
        rank = self._enqueue_counter + 1
        self._replace(job, waiter_seq=rank)
        self._enqueue_counter = rank

    def dequeue_waiter(self, job: str) -> None:
        """Drop a job out of the queue -- admitted, killed, iced or cancelled.
        The counter does not go back: it is a high-water allocator, not a
        length."""
        self._replace(job, waiter_seq=None)

    def seed_consumed(self, consumed: Mapping[str, int]) -> None:
        """Open the map from a carried seal (period-model ss3.3). A negative
        value would open the period with invented capacity, so it is refused
        here as well as by the loader (PR-22)."""
        for bucket, units in consumed.items():
            if units < 0:
                raise OracleError(f"consumed[{bucket!r}] = {units}: units spent cannot be negative")
        self._consumed = dict(consumed)

    # ------------------------------------------------------------------- hosts

    def _replace_host(self, host_id: str, **fields: object) -> None:
        """The single host write, on `_replace`'s rule and for its reason:
        rebuilt through the model's own constructor, because
        `model_copy(update=)` does not validate and an undeclared field name
        must be loud."""
        self._touch(self.host_key(host_id))
        current = self._hosts.get(host_id) or HostRuntime()
        self._hosts[host_id] = HostRuntime.model_validate({**dict(current), **fields})

    def _require_host(self, host_id: str) -> HostRuntime:
        row = self._hosts.get(host_id)
        if row is None:
            raise OracleError(f"no host {host_id!r} in the routing table")
        return row

    def register_host(
        self, host_id: str, *, deadman_s: float | None = None, at: datetime | None = None
    ) -> None:
        """Put a host in the routing table, or refresh the identity of one
        already in it (concurrency-model ss8).

        A NEW host lands `active` -- ss8's table says registration is one of
        the two things that sets that state. An EXISTING one keeps its
        routing state: ss8 makes the state durable precisely so that a
        failover does not undo a drain, and a relay that could undo one by
        re-registering would give back with one hand what that sentence
        takes with the other. What re-registration does refresh is identity
        -- the deadman it now runs, and the contact it just made."""
        row = self._hosts.get(host_id)
        self._replace_host(
            host_id,
            deadman_s=deadman_s,
            last_contact=at if at is not None else (row.last_contact if row else None),
        )

    @property
    def period_id(self) -> int:
        return self._period_id

    def finish_genesis(self) -> None:
        """Mark the constructor's own seeding as NOT-an-input for the
        seed-versus-advance latch. Genesis seeding (catalog rows, the local
        executor) is identical on every replay and deliberately unjournaled
        (`runner_hosts`), so a state that has only been constructed is a
        fresh one -- and without this, `seed_period` would refuse every
        assembly on a store the Oracle just built.

        ONE-SHOT: a second call after real inputs would launder a used
        state back to fresh and let `seed_period` skip a live lineage --
        the exact bypass the latch exists to close. Inside the genesis
        input it is refused too: construction ends after that input
        commits."""
        if self._phase in ("constructed", "input", "live"):
            raise ValueError("finish_genesis twice: construction happens once")
        if self._phase == "seeded":
            raise ValueError("finish_genesis after seed_period: construction comes first")
        self._assemble(_FINISH_GENESIS)

    def _assemble(self, t: Transition[AssemblyPhase]) -> None:
        """Take one `runtime_assembly` move. A move the table does not
        declare raises before the phase changes: assembly is not an input,
        so a violation has no channel to ride (concurrency-model ss4)."""
        new = cast(AssemblyPhase, t.target)
        violation = RUNTIME_ASSEMBLY.take(t, self._phase, new)
        if violation is not None:
            raise OracleError(f"{t.trigger} in assembly phase {self._phase}: {violation.reason}")
        self._phase = new

    def install(self, carried: CarriedRows) -> None:
        """Install carried rows VERBATIM -- revisions included -- as the
        FIRST act of an assembly (period-model ss7 phase 3 step 3).

        Not an input, and deliberately not expressible as one: the rows
        arrive with the revisions the closing seal published, and an
        operator holding an `expect` against one of those revisions must
        find it unmoved. A "construct C2 then overwrite" opener seeds
        carried entities through ordinary verbs, moves every revision it
        touches, and makes every published revision unholdable.

        The period is NOT seeded here: `finish_genesis` refuses a state
        that has already been seeded, and the constructor's own catalog
        seed still has to run over these rows. The assembler calls
        `seed_period` after it (ss3.5's latch, DL-132)."""
        if self._phase != "fresh":
            raise ValueError(
                "install on a used state: carried rows are assembly's first act, and a"
                " live state advances through its own inputs alone (period-model ss7)"
            )
        self._assemble(_INSTALL)
        self._jobs = dict(carried.jobs)
        self._globals = dict(carried.globals_)
        self._hosts = dict(carried.hosts)
        self._timers = sorted(carried.timers, key=_timer_order)
        heapq.heapify(self._timers)
        self._timer_seq = carried.timer_seq
        self._consumed = dict(carried.consumed)
        self._enqueue_counter = carried.enqueue_counter

    def seed_period(self, period_id: int) -> None:
        """Set the period a FRESH state is being assembled into (period-model
        ss3.5, DL-132): the loader's first act, before any input. Explicit,
        never inferred -- a state whose only mutations were globals, timers
        or host rows would look untouched to any job-row inference and let
        a live lineage skip. Legal exactly once, and never after a
        committed input; the seal's own I2 and lineage bounds hold the
        seeded number to the lineage. A period opens in a new state over
        the carried rows, so a live state never moves its period."""
        if self._phase in _OPEN:
            raise ValueError("seed_period inside an input: assembly precedes inputs")
        if self._phase not in _SEED_PERIOD.source:
            raise ValueError(
                "seed_period on a used state: seeding is assembly's first act, and a"
                " live state never moves its period (I2)"
            )
        if period_id < 1:
            raise ValueError(f"seed_period({period_id}): periods count from 1 (I2)")
        self._assemble(_SEED_PERIOD)
        self._period_id = period_id

    def touch_host(self, host_id: str, at: datetime) -> None:
        """Record positive contact with a host (concurrency-model ss8, S5b).

        Deliberately NOT an admitted input: `last_contact` is outside the
        semantic projection, so this moves no revision and leaves no log
        record, and a lease heartbeat every twenty seconds costs neither. A
        host the table does not know is ignored rather than created -- the
        table is an inventory, and contact with something not in it is not a
        registration."""
        if host_id in self._hosts:
            self._replace_host(host_id, last_contact=at)

    def set_host_state(self, host_id: str, state: HostState) -> None:
        """Move a host between ss8's routing states. Eviction is NOT this
        verb: it carries a fence and an attribution, which is exactly what
        makes it the one state that can cause a double run."""
        self._require_host(host_id)
        self._replace_host(host_id, state=state)

    def quarantine_host(self, host_id: str) -> None:
        """ss8: the leader's own state, set when a host stops answering.

        Remembers what it interrupted. A drained host that goes unreachable
        and comes back must still be drained -- the operator's intent is not
        the leader's to revoke, and a blip that silently ended a maintenance
        window would be the worst kind of automation."""
        row = self._require_host(host_id)
        if row.state == "quarantined":
            return  # idempotent: repeated unreachability is one fact
        self._replace_host(host_id, state="quarantined", state_before_quarantine=row.state)

    def reinstate_host(self, host_id: str) -> None:
        """ss8: the leader clears quarantine when the host answers again,
        putting back exactly the state it took away."""
        row = self._require_host(host_id)
        if row.state != "quarantined":
            return
        self._replace_host(
            host_id,
            state=row.state_before_quarantine or "active",
            state_before_quarantine=None,
        )

    def evict_host(self, host_id: str, *, forced_by: str | None) -> None:
        """ss8: declare a host's work rerouteable. The only state that lets
        ANOTHER host run what was bound to this one, so it does two things
        no other transition does -- bump the `generation` a returning relay
        is fenced on, and record the actor of a `--force` that skipped the
        proof (None when the ss8 preconditions were met).

        It also CLEARS what quarantine interrupted. A gated eviction can only
        start from `quarantined` (ss8 precondition 1), so leaving that field
        set would make every gated eviction violate the invariant this row
        documents -- non-null only while quarantined -- and would leave a
        state to "put back" for a host whose whole point is that it is not
        coming back at this generation (DL-111)."""
        row = self._require_host(host_id)
        self._replace_host(
            host_id,
            state="evicted",
            generation=row.generation + 1,
            forced_by=forced_by,
            state_before_quarantine=None,
        )

    # ------------------------------------------------------------------ timers

    def enqueue_timer(self, due: datetime, ev: Event) -> int:
        """Arm a timer and return its ordering token. The token is a single
        global counter, so equal-due timers keep their arming order ACROSS
        jobs -- which is what decides who starts when two deadlines land on
        the same instant."""
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
        """One job's armed timers as (due, token), in firing order: the
        entity-local half of the global ordering (concurrency-model ss3).
        Sorting the union of these across jobs reproduces the heap's firing
        order exactly, which a per-job set digest cannot do."""
        return [
            (due, token)
            for due, token, ev in sorted(self._timers, key=_timer_order)
            if ev.payload.get("job") == job
        ]
