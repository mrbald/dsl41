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

import heapq

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict


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

TERMINAL: frozenset[str] = frozenset({"SUCCESS", "FAILURE", "TERMINATED"})

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
    "KILLJOB",
    "TIMER",
    "MUST_START_ALARM",
    "MUST_COMPLETE_ALARM",
]


class Event(BaseModel):
    at: datetime
    kind: EventKind
    payload: dict[str, object] = {}
    #: provenance of an externally injected event -- the engine's ss7 input
    #: alphabet (scheduler | control | adapter | reconcile); None for oracle-
    #: internal and script events. Start causes surface it (DL-68).
    source: str | None = None

    def job(self) -> str | None:
        job = self.payload.get("job")
        return job if isinstance(job, str) else None


class TraceEntry(BaseModel):
    at: datetime
    job: str
    transition: str  # "OLD->NEW" or an out-of-band marker like "ON_ICE"
    cause: str


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
    #: SEM-10 at-most-once bookkeeping: the members already run in THIS box's
    #: current execution. Only a BOX row ever carries entries; it was the loose
    #: `_box_ran` map until DL-86 moved it onto the entity it describes.
    ran_members: frozenset[str] = frozenset()
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
_PROJECTED_JOB_FIELDS: tuple[str, ...] = tuple(
    name for name in JobRuntime.model_fields if name not in _UNPROJECTED
)
_PROJECTED_HOST_FIELDS: tuple[str, ...] = tuple(
    name for name in HostRuntime.model_fields if name not in _UNPROJECTED
)
_DEFAULT_JOB = JobRuntime()


class RuntimeState:
    """The authoritative state of one Oracle: job rows, global rows, and the
    timer heap. SEM-01 latching applies throughout -- a recorded status is
    current regardless of age.

    DL-82 made this the single write path; DL-86 makes escape IMPOSSIBLE
    rather than merely detected. The rows are frozen, the maps are private
    and published only as read-only views, and every write goes through a
    verb that names what changed (`transition`, `start_run`, `set_flags`,
    `set_armed`, `set_global`, `enqueue_timer`). No caller assembles a field
    dict, so no caller can invent a field combination the verbs do not.

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
    recoverable per entity rather than only in aggregate."""

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
        #: DL-87 input transaction: entity key -> its projection at FIRST
        #: touch within the open input. Empty and inert outside one.
        self._snapshots: dict[str, object] = {}
        self._in_input = False

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
        if self._in_input and key not in self._snapshots:
            self._snapshots[key] = self._projection(key)

    def begin_input(self) -> None:
        """Open one input's transaction. Inputs do not nest: the oracle's
        cascade, its fired timers and its box folds are all consequences of
        ONE input and share its revision, which is what makes `expect`
        checkable -- a client that read revision 12 must be invalidated by
        the whole of the next input, not by its first transition."""
        if self._in_input:
            raise OracleError("input already open: inputs do not nest")
        self._in_input = True
        self._snapshots = {}

    def commit_input(self) -> list[str]:
        """Close the transaction and increment each CHANGED entity exactly
        once. Returns the changed keys in a stable order -- S2's outbox and
        `ApplyResult` need the list, not just the effect."""
        self._in_input = False
        snapshots, self._snapshots = self._snapshots, {}
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
        self, job: str, status: JobStatus, at: datetime | None, exit_code: int | None = None
    ) -> None:
        """Record a status change. `last_end_at` latches on every terminal
        transition -- the Q2 anchor is the job's OWN last end (DL-54) -- and
        an exit code is written only when one was reported."""
        fields: dict[str, object] = {"status": status, "status_at": at}
        if status in TERMINAL:
            fields["last_end_at"] = at
        if exit_code is not None:
            fields["exit_code"] = exit_code
        self._replace(job, **fields)

    def start_run(self, job: str, *, cause: str, box: str | None, is_box: bool) -> None:
        """Everything one actual start changes, in one act: the run_number
        bump, the arm it consumes (Q3/DL-54 -- the ACTUAL start consumes it,
        FORCE included), the provenance of THIS run (DL-68), and the SEM-10
        box bookkeeping on both sides -- the member joins its box's ran set,
        and a box starting resets its own.

        A box that is itself a member does both, to two different rows."""
        self._replace(
            job, armed=False, run_number=self.runtime(job).run_number + 1, started_by=cause
        )
        if box is not None:
            self._replace(box, ran_members=self.runtime(box).ran_members | {job})
        if is_box:
            # Reset BEFORE the caller's RUNNING transition: that transition's
            # own re-evaluation may already start members, and they must land
            # in the fresh per-run set (SEM-10 at-most-once bookkeeping).
            self._replace(job, ran_members=frozenset())

    def set_flags(
        self,
        job: str,
        *,
        on_ice: bool | None = None,
        on_hold: bool | None = None,
        on_noexec: bool | None = None,
    ) -> None:
        """Set the SEM-20/21/22 out-of-band flags. `None` means unchanged, so
        a caller naming one flag cannot silently clear the other two."""
        fields = {
            name: value
            for name, value in (
                ("on_ice", on_ice),
                ("on_hold", on_hold),
                ("on_noexec", on_noexec),
            )
            if value is not None
        }
        if fields:
            self._replace(job, **fields)

    def set_armed(self, job: str, armed: bool) -> None:
        """Latch or consume a scheduled tick (SEM-32 arm-and-wait, DL-54)."""
        self._replace(job, armed=armed)

    def set_global(self, name: str, value: str) -> None:
        """Latch a global's value (SEM-06). The revision carries over: only
        commit_input() moves it, and only if the value actually changed --
        AutoSys's same-value SET_GLOBAL is a real and common input."""
        self._touch(self.global_key(name))
        row = self._globals.get(name)
        self._globals[name] = GlobalRuntime(
            value=value, state_rev=0 if row is None else row.state_rev
        )

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

    def set_host_state(self, host_id: str, state: HostState) -> None:
        """Move a host between ss8's routing states. Eviction is NOT this
        verb: it carries a fence and an attribution, which is exactly what
        makes it the one state that can cause a double run."""
        self._require_host(host_id)
        self._replace_host(host_id, state=state)

    def evict_host(self, host_id: str, *, forced_by: str | None) -> None:
        """ss8: declare a host's work rerouteable. The only state that lets
        ANOTHER host run what was bound to this one, so it does two things
        no other transition does -- bump the `generation` a returning relay
        is fenced on, and record the actor of a `--force` that skipped the
        proof (None when the ss8 preconditions were met)."""
        row = self._require_host(host_id)
        self._replace_host(
            host_id, state="evicted", generation=row.generation + 1, forced_by=forced_by
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
        return sorted(self._timers)

    def timers_for(self, job: str) -> list[tuple[datetime, int]]:
        """One job's armed timers as (due, token), in firing order: the
        entity-local half of the global ordering (concurrency-model ss3).
        Sorting the union of these across jobs reproduces the heap's firing
        order exactly, which a per-job set digest cannot do."""
        return sorted(
            (due, token) for due, token, ev in self._timers if ev.payload.get("job") == job
        )
