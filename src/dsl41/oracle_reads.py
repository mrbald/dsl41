"""The oracle's reads of its store and its catalog (ir-design ss7; docs/statechart-engine.md ss11).

`OracleReads` is the base of `oracle.Oracle`. It builds the store from the catalog and the
carried rows, and keeps the trace, the emitted events, the referencer index and the capacity
pool. Its methods answer what the job chart's guards and the cascade's moments ask: a
condition's truth (SEM-01 to SEM-07), a box's members and ancestors, whether a member is out of
its box's run, a box's terminator and resolution rules, an admission against the pool (DL-50),
and why an operator's flag input is ignored (DL-254). A read writes nothing but the zone and
calendar caches; `_record` and `_emit` write the trace and the emitted events.
"""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping
from datetime import date, datetime, timedelta, tzinfo

from dsl41.autocal import CompiledCalendar
from dsl41.capacity import CapacityPool, DemandEntry, checks_load, without_machine_load
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
)
from dsl41.ir import CatalogIR, JobIR
from dsl41.oracle_state import (
    LIVE,
    LIVE_OR_QUEUED,
    TERMINAL,
    CarriedRows,
    Event,
    EventKind,
    JobRuntime,
    OracleError,
    RuntimeState,
    TraceEntry,
)
from dsl41.semantics import DEFAULTS as DEFAULT_SWITCHES
from dsl41.semantics import SemanticSwitches, iced_atom_truth

#: SEM-02: n() is true unless the job is in one of these (WAIT_REPLY/RESTART/SUSPENDED are
#: out-of-scope states the oracle never produces). QUE_WAIT is absent on purpose (DL-50): a
#: resource-queued job is not running, so n() is true for it, and every status/exitcode atom
#: reads false (it never ran).
_N_FALSE_STATUSES: frozenset[str] = frozenset({"STARTING", "RUNNING"})


class OracleReads:
    """The store, the trace and the reads of one CatalogIR (ir-design ss7)."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        default_tz: str | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        self.catalog = catalog
        #: The semantic switches (runner-design ss8a, DL-252): a caller with a runtime profile
        #: passes the period's own (`period.switches_of`); a static tool gets the registry
        #: defaults.
        self.semantics: SemanticSwitches = semantics or DEFAULT_SWITCHES
        self.store = RuntimeState()
        if carried is not None:
            # period-model ss7 phase 3 step 3: carried rows install verbatim and before the
            # genesis seed, which skips them. A "construct then overwrite" opener would move
            # every carried revision and leave an operator's `expect` against a published
            # revision unholdable.
            self.store.install(carried)
        # DL-87: the catalog seed is an input, the genesis one. It makes `revision(key) == 0`
        # mean "absent" for a global, so a conditional create is expressible. A declared global
        # lands at revision 1; a never-declared name stays at 0.
        self.store.begin_input()
        for name, job_ir in catalog.jobs.items():
            if carried is not None and name in carried.jobs:
                continue  # ss7 phase 3 step 4: a carried row keeps its C1 flags
            # SEM-24: definition-time state seeds the SEM-20/21/22 flags
            initial = job_ir.sem.initial_status
            self.store.seed_job(
                name,
                on_hold=initial == "ON_HOLD",
                on_ice=initial == "ON_ICE",
                on_noexec=initial == "ON_NOEXEC",
            )
        for name, value in catalog.globals_declared.items():
            if carried is not None and name in carried.globals_:
                continue  # only GENUINELY NEW rows are seeded (ss7 phase 3 step 4)
            self.store.set_global(name, value)
        self.store.commit_input()
        # the constructor's seed is not an input to the seed/advance latch
        self.store.finish_genesis()
        if carried is not None:
            self.store.seed_period(carried.period_id)
        self._trace: list[TraceEntry] = []
        self._emitted: list[Event] = []
        #: ss3.3: feed times are non-decreasing across the boundary, so an opened interpreter
        #: starts from the instant the seal was taken.
        self._now: datetime | None = carried.now if carried is not None else None
        #: Edge-trigger index (DL-13): entity key -> jobs whose `condition` references it. Keys:
        #: job names (incl. "name^INST"), "g:NAME".
        self._referencers: dict[str, list[str]] = {}
        for name, job_ir in catalog.jobs.items():
            attr = job_ir.sem.condition
            if attr is None:
                continue
            for key in _entity_keys(attr.cond):
                self._referencers.setdefault(key, []).append(name)
        #: box -> the jobs it holds directly, in catalog order (`_members`).
        members: dict[str, list[str]] = {}
        for name, job_ir in catalog.jobs.items():
            if job_ir.box.box_name is not None:
                members.setdefault(job_ir.box.box_name, []).append(name)
        self._members_of = {box: tuple(names) for box, names in members.items()}
        #: DL-50 capacity buckets and the QUE_WAIT queue (DL-74): it decides admission and its
        #: order; the transitions are this class's. It holds no state (DL-120): the reservations
        #: and ranks are on the rows, the spent units are under the owner, and the pool is given
        #: all three.
        self._pool = CapacityPool(catalog, self.semantics)
        #: SEM-35 name -> zone, resolved once per name (the ladder walks the whole zoneinfo
        #: database for a city default).
        self._tz_cache: dict[str, tzinfo] = {}
        #: calendar name -> its days, built on first use by `queued-recheck` (DL-257): a standard
        #: calendar's day set, or a compiled extended one.
        self._calendars: dict[str, frozenset[date] | CompiledCalendar] = {}
        #: SEM-35's `ujo_timezones` table, as `--timezone-map` supplies it to the scheduler
        #: (DL-62). None means no map, which differs from an empty one: the ladder's unique-city
        #: default applies only when the estate supplied no table (`timezones`'
        #: `resolve_timezone`). The scheduler wires it at engine build, so a map-only name that
        #: preflight passed resolves at the first start (DL-151).
        self._tz_aliases: dict[str, str] | None = None if tz_aliases is None else dict(tz_aliases)
        #: E10's no-timezone half, closed by citation (DL-155): the vendor schedules a job that
        #: declares no `timezone:` "based on the time zone under which the scheduler is
        #: running". `default_tz` is that zone for a directly built oracle; None keeps the engine
        #: clock as the basis. The name resolves lazily in `_job_tz` through the same ladder and
        #: alias table as a job's own `timezone:` (DL-151). The runner passes nothing here:
        #: `--timezone` stays on the scheduler.
        self._default_tz: str | None = default_tz

    def fork(self) -> OracleReads:
        """An independent copy to dry-apply one input on (concurrency-model ss4).

        Not `deepcopy`, which would copy the catalog. The store forks itself; every other
        mutable container is copied, here and by each layer above, the memo caches included. The
        catalog, the switches, the capacity pool, the referencer index and the members index
        are shared: they are fixed once the constructor returns. The trace and the emitted log
        start empty, so a fork costs nothing in the period's length: both are append-only and an
        apply reads them only by slicing from a length it saved. A new attribute must be added
        to a fork; the fork test pins the attribute set.
        """
        twin = copy.copy(self)
        twin.store = self.store.fork()
        twin._trace = []
        twin._emitted = []
        twin._tz_cache = dict(self._tz_cache)
        twin._calendars = dict(self._calendars)
        return twin

    def _members(self, box: str) -> tuple[str, ...]:
        """The jobs the box holds directly, in catalog order."""
        return self._members_of.get(box, ())

    def trace(self) -> list[TraceEntry]:
        return [entry.model_copy() for entry in self._trace]  # no aliasing out

    def trace_since(self, since: int) -> tuple[int, list[TraceEntry]]:
        """The trace length and a copy of the entries after the first `since`; only the slice is
        copied."""
        return len(self._trace), [e.model_copy() for e in self._trace[max(since, 0) :]]

    def next_timer_due(self) -> datetime | None:
        """Read-only peek at the timer heap (runner-design ss3): the earliest scheduled TIMER's
        due time, or None. Timers fire inside feed()/advance(); a wall-clock shell uses this to
        know when to wake."""
        return self.store.next_timer_due()

    def _emit(self, kind: EventKind, **payload: object) -> None:
        assert self._now is not None
        self._emitted.append(Event(at=self._now, kind=kind, payload=dict(payload)))

    def _record(self, job: str, transition: str, cause: str) -> None:
        assert self._now is not None
        self._trace.append(TraceEntry(at=self._now, job=job, transition=transition, cause=cause))

    def _runtime(self, job: str) -> JobRuntime:
        return self.store.runtime(job)  # DL-82: the store owns creation too

    def _status(self, job: str) -> str:
        return self._runtime(job).status

    def _required_job(self, ev: Event) -> str:
        job = ev.job()
        if job is None:
            raise OracleError(f"{ev.kind} requires payload.job")
        return job

    def _ancestor_runs(self, job: str) -> dict[str, int]:
        """Each box above `job` with its run number, read before a moment's first box rule runs.

        A rule of that moment that completes a box can start it again in the same cascade; the
        walk up (SEM-12) and the holders' check (SEM-20) then leave the new run alone: "Jobs in
        a box run only once for each box execution" (Basic Box Job Concepts, AutoSys 12.0 and
        24.2).
        """
        return {box: self._runtime(box).run_number for box in self._ancestor_boxes(job)}

    def _ancestor_boxes(self, job: str) -> list[str]:
        """Containing boxes, innermost first (SEM-17). Lowering rejects containment cycles; the
        seen-set keeps a hand-built IR finite."""
        chain: list[str] = []
        seen = {job}
        job_ir = self.catalog.jobs.get(job)
        box = job_ir.box.box_name if job_ir is not None else None
        while box is not None and box not in seen:
            chain.append(box)
            seen.add(box)
            box_ir = self.catalog.jobs.get(box)
            box = box_ir.box.box_name if box_ir is not None else None
        return chain

    def _contained(self, box: str, *, skip_live: bool) -> list[str]:
        """Every job `box` contains, transitively, top-down: a subbox comes before its members.

        With `skip_live`, a job that is STARTING, RUNNING or QUE_WAIT is left out, and so is
        everything inside a live subbox. The seen-set keeps a hand-built IR finite.
        """
        out: list[str] = []
        seen = {box}
        pending = deque([box])
        while pending:
            for member in self._members(pending.popleft()):
                if member in seen:  # pragma: no cover -- see below
                    # Unreachable: the CatalogIR validator refuses a containment cycle, and each
                    # job has one box, so no member repeats.
                    continue
                seen.add(member)
                if skip_live and self._runtime(member).status in LIVE_OR_QUEUED:
                    continue
                out.append(member)
                if self.catalog.jobs[member].job_type == "BOX":
                    pending.append(member)
        return out

    @staticmethod
    def _lifts_a_block(old: str, new: str) -> bool:
        """DL-247: a job leaving QUE_WAIT can lift a priority block without freeing capacity, so
        it wakes the queue. QUE_WAIT -> STARTING is an admission, made by the scan already
        running, whose next pass sees the change; it requests nothing."""
        return old == "QUE_WAIT" and new not in ("QUE_WAIT", "STARTING")

    def _box_stopped(self, job: str, old: str, new: str) -> bool:
        """DL-247: a box leaving RUNNING. Its queued members stop blocking at once, so a job they
        blocked may now start."""
        job_ir = self.catalog.jobs.get(job)
        return old == "RUNNING" != new and job_ir is not None and job_ir.job_type == "BOX"

    def _oob_ignored(self, kind: EventKind, job: str) -> str | None:
        """Why the scheduler ignores this ON_ICE, ON_HOLD or ON_NOEXEC which the chart ignored,
        or None (DL-254).

        "Change the Executable Status of a Job" (AutoSys 24.2): ON_HOLD and ON_ICE have "no
        effect on jobs with a status of STARTING or RUNNING", box or not; the chart ignores them
        there only (job.148 to job.151). ON_NOEXEC is ignored for a non-box job that is
        STARTING, RUNNING or ON_ICE, for a box that is ON_ICE or RUNNING, and for "A box job with
        jobs (including the jobs contained in lower level boxes) in a status other than the
        following status: ON_HOLD, ON_NOEXEC, INACTIVE, SUCCESS, FAILURE, ACTIVATED, or
        TERMINATED", here a contained job that is iced, STARTING, RUNNING or QUE_WAIT. QUE_WAIT
        is not named for the job itself, so a queued job keeps the handling that applies it.
        """
        rt = self._runtime(job)
        if kind in ("ON_ICE", "ON_HOLD"):
            if rt.status in LIVE:
                return f"sendevent {kind} ignored: the job is {rt.status} (DL-254)"
            return None
        job_ir = self.catalog.jobs.get(job)
        is_box = job_ir is not None and job_ir.job_type == "BOX"
        if rt.on_ice:
            return "sendevent ON_NOEXEC ignored: the job is ON_ICE (DL-254)"
        if rt.status == "RUNNING" or (not is_box and rt.status == "STARTING"):
            return f"sendevent ON_NOEXEC ignored: the job is {rt.status} (DL-254)"
        if not is_box:
            return None
        for member in self._contained(job, skip_live=False):
            mrt = self._runtime(member)
            if mrt.on_ice:
                return f"sendevent ON_NOEXEC ignored: contained job {member!r} is ON_ICE (DL-254)"
            if mrt.status in LIVE_OR_QUEUED:
                return (
                    f"sendevent ON_NOEXEC ignored: contained job {member!r} is"
                    f" {mrt.status} (DL-254)"
                )
        return None

    def _atom_true(self, atom: StatusAtom | ExitCodeAtom, evaluator: str) -> bool:
        name = atom.job.key
        rt = self.store.job.get(name)
        if rt is None:
            return False  # SEM-06: undefined -> permanently, silently false
        if rt.on_ice:
            # The flag reads the same whatever the stored status: ON_ICE on a STARTING/RUNNING
            # job is ignored (DL-254) and a plain start refuses an iced job, so only an injected
            # STATUS can make an iced row live. The row and its open Q10 split live in
            # `iced_atom_truth`; the oracle passes its switch (DL-252).
            return iced_atom_truth(atom, self.semantics.ice_lookback)
        if isinstance(atom, ExitCodeAtom):
            if rt.exit_code is None or not self._lookback_ok(rt, atom.lookback, evaluator):
                return False
            return compare_int(rt.exit_code, atom.op, atom.value)
        wanted = atom.status
        actual = rt.status
        if wanted == "DONE":
            hit = actual in TERMINAL
        elif wanted == "NOTRUNNING":
            hit = actual not in _N_FALSE_STATUSES
        else:
            hit = actual == wanted
        if not hit:
            return False
        if wanted == "NOTRUNNING" and rt.status_at is None:
            return True  # never-run jobs are notrunning with no timestamp
        return self._lookback_ok(rt, atom.lookback, evaluator)

    def _lookback_ok(self, rt: JobRuntime, lookback: Lookback | None, evaluator: str) -> bool:
        if lookback is None or lookback.kind == "indefinite":
            return True
        if rt.status_at is None:  # pragma: no cover -- see below
            # Unreachable: `store.transition` stamps status_at on every status write. A row never
            # written is INACTIVE with no exit code, which no status atom matches except n(),
            # answered above, and an exit-code atom needs an exit code.
            return False
        assert self._now is not None
        if lookback.kind == "zero":
            # Q2a (DL-54): the predecessor qualifies iff its own last end is at or after the
            # evaluating job's last end. Both sides are end times ("examines the last end time
            # of the job first. It then examines the last end time of the condition job"), so an
            # n() predecessor bounced to INACTIVE does not read its non-end transition as a
            # fresh run.
            anchor_rt = self.store.job.get(evaluator)
            anchor = None if anchor_rt is None else anchor_rt.last_end_at
            if anchor is None:
                # Q2b resolved (DL-58): a never-ended evaluator has no anchor and the atom is
                # satisfied; a newly inserted job "has no initial/previous end time".
                return True
            if rt.last_end_at is None:
                return False  # the predecessor never ended: nothing ran "since"
            return rt.last_end_at >= anchor
        assert lookback.minutes is not None
        return rt.status_at >= self._now - timedelta(minutes=lookback.minutes)

    def _cond_true(self, cond: Cond, evaluator: str) -> bool:
        if isinstance(cond, And):
            return all(self._cond_true(op, evaluator) for op in cond.operands)
        if isinstance(cond, Or):
            return any(self._cond_true(op, evaluator) for op in cond.operands)
        if isinstance(cond, Paren):
            return self._cond_true(cond.inner, evaluator)
        if isinstance(cond, GlobalAtom):
            actual = self.store.global_value(cond.name)
            if actual is None:
                return False
            return compare_value(actual, cond.op, cond.value)
        return self._atom_true(cond, evaluator)

    def _noexec_above(self, job_ir: JobIR) -> bool:
        """SEM-22: a box that contains the job is ON_NOEXEC, so a member that is not a box and has
        no ON_NOEXEC of its own bypasses to SUCCESS instead of running.

        `start_verdict` reads its own flag and its type first. No vendor text states the
        inheritance (SEM-22 [?]). DL-254's cascade flags every job in the tree when a box is put
        ON_NOEXEC, so it decides only corners where a member lacks the flag under a flagged box:
        a box flagged at definition time whose members do not take the flag (SEM-24); a member
        taken OFF_NOEXEC alone under a flagged box; and a job that a later period's catalog adds
        to, or moves under, a flagged box. A box never bypasses: an ON_NOEXEC box "goes RUNNING,
        members are bypassed to SUCCESS as their conditions are met", so the rule is applied once
        per box level.
        """
        return any(self._runtime(box).on_noexec for box in self._ancestor_boxes(job_ir.name))

    def _admissible(self, job_ir: JobIR, vector: list[DemandEntry], *, force: bool) -> bool:
        """The admission test of a start, fresh or out of the queue (DL-247).

        A job with a positive priority must not be blocked by a higher-priority waiter on a
        named resource it names (DL-255); force does not lift that block, since the vendor's
        force rule is a load rule. A FORCE_STARTJOB checks the named resources only: the forced
        job "runs even if its load exceeds the machine's max_load value", so no load waiter
        blocks it. A job whose priority is unset or 0 does the same: the scheduler "ignores any
        load unit values" for it. Both still hold their load. Otherwise the job must not be
        blocked by a higher-priority load waiter on its machine, and every bucket must fit.
        Force lives on the event, not the row: a forced job that queues on a named resource is
        readmitted like any other.
        """
        rows, consumed = self.store.job, self.store.consumed
        # DL-256: a job's own held units are available to it, and only to it
        own = job_ir.name
        if self._pool.resource_blocked(job_ir, rows, consumed, self._counts_as_waiter):
            return False
        if force or not checks_load(job_ir):
            return self._pool.can_admit(without_machine_load(vector), rows, consumed, own=own)
        if self._pool.load_blocked(job_ir, rows, consumed, self._counts_as_waiter):
            return False
        return self._pool.can_admit(vector, rows, consumed, own=own)

    def _counts_as_waiter(self, waiter: str) -> bool:
        """A queued job that counts toward priority blocking (DL-247, DL-255): not held, and not a
        member whose box has stopped running. The readmission scan cancels the latter and keeps
        the former queued without trying it, so neither waits for load or a resource."""
        rt = self._runtime(waiter)
        job_ir = self.catalog.jobs.get(waiter)
        if rt.on_hold or job_ir is None:
            return False
        box = job_ir.box.box_name
        return box is None or self._runtime(box).status == "RUNNING"

    def _out_of_the_run(self, job: str) -> bool:
        """SEM-11, SEM-20: whether an iced member is out of its box's run.

        It is when it is on ice, its own status is not live or QUE_WAIT, and no job it contains,
        transitively, is live or QUE_WAIT. Basic Box Job Concepts (AutoSys 12.0, 24.2) keeps a
        box RUNNING "as long as there are jobs in it with ACTIVATED or RUNNING status". Three
        readers share it under both `box-start-all-members-out` values: the box-start trigger,
        the fold's ice skip, and the ON_ICE completion moment (DL-285). A member that is not out
        falls through to the fold's "not yet run" rule, so the box waits; the member's own
        terminal transition, or its subbox's idle recompute (SEM-15), runs the door again once
        the job inside ends.
        """
        rt = self._runtime(job)
        if not rt.on_ice or rt.status in LIVE_OR_QUEUED:
            return False
        return not any(
            self._runtime(inner).status in LIVE_OR_QUEUED
            for inner in self._contained(job, skip_live=False)
        )

    def _box_terminator_fires(self, member: str, new: str) -> bool:
        """SEM-14: whether `member` ending `new` triggers its box_terminator.

        "Force the Job or the Box to Stop Running" (AutoSys 12.1, 24.2): "if the job completes
        with a FAILURE or TERMINATED status, the box terminates." TERMINATED counts under the
        default `box-terminator-on-terminated=true`; `false` keeps FAILURE only.
        """
        if not self.catalog.jobs[member].box.box_terminator:
            return False
        if new == "TERMINATED":
            return self.semantics.box_terminator_on_terminated == "true"
        return new == "FAILURE"

    def _resolves(self, box: str, member: str, old: str, new: str) -> bool:
        """Whether an INACTIVE transition resolves `member` in a RUNNING run of its parent `box`
        (SEM-11's carve-outs): a window skip (DL-154) or an operator's INACTIVE (DL-242), both
        marked on the box row, or an ON_ICE that takes a queued member that has not run out of
        the queue (DL-285). Only that ice moves an iced row from QUE_WAIT."""
        box_rt = self._runtime(box)
        return (
            new == "INACTIVE"
            and box_rt.status == "RUNNING"
            and (
                member in box_rt.window_skipped_members
                or (
                    old == "QUE_WAIT"
                    and self._runtime(member).on_ice
                    and member not in box_rt.ran_members
                )
            )
        )

    def _off_ice_in_running_box(self, job: str) -> str | None:
        """SEM-20 under `off-ice-in-running-box=next-run`: a member taken off ice while its direct
        box is RUNNING, before it ran there, sits that run out.

        "Start Conditions" (AutoSys 24.2, 12.0): "If a job is contained in a running box when it
        is taken off ice, the scheduler does not restart the job until the following run of the
        box, even if its starting conditions recur during the existing run of the box." The ice
        already took it out of the fold (DL-285); the mark keeps it out and refuses a plain
        start of it in this run. A member that ran keeps its vote and needs no mark. The caller
        runs this only for a job that was on ice. `same-run` sets no mark. Returns the box it
        marked, or None.
        """
        if self.semantics.off_ice_in_running_box != "next-run":
            return None
        job_ir = self.catalog.jobs.get(job)
        box = job_ir.box.box_name if job_ir is not None else None
        if box is None:
            return None
        box_rt = self._runtime(box)
        if box_rt.status != "RUNNING" or job in box_rt.ran_members:
            return None
        self.store.record_iced_out(box, job)
        return box

    def _votes_as_it_stands(self, box: str, member: str) -> bool:
        """SEM-11 (DL-360): a direct member that has not run in this box run but holds a terminal
        status votes with it, unless it is out of the run on ice.

        Four sources give such a status: a member carried live from the run that ended and then
        ended; an operator's SUCCESS, FAILURE or TERMINATED on a member that waits; a KILLJOB on
        a member queued in this run, or on one an injected STARTING made live, which ends it
        TERMINATED; and a never-run subbox that the idle recompute (SEM-15) re-derived from
        statuses given to its members, a [C] reading, since SEM-15's table has no ACTIVATED or
        ON_HOLD row. A resolution or off-ice mark decides only while the member reads INACTIVE.
        """
        return (
            member not in self._runtime(box).ran_members
            and self._runtime(member).status in TERMINAL
            and not self._out_of_the_run(member)
        )

    def _all_members_done(self, box: str) -> bool:
        """SEM-11, literal (DL-13): the box cannot complete until every member is terminal or has
        been bypassed (iced, or resolved to INACTIVE by a window skip, DL-154, or by an injected
        STATUS INACTIVE, DL-242).

        A member that has not run in this box run is done when its status is terminal (DL-360).
        A member whose condition never fires inside the run, or whose run_window deferred it,
        reads INACTIVE and keeps the box RUNNING. An ON_NOEXEC member is not skipped: SEM-22
        bypasses it to SUCCESS "as [its] conditions are met", and that bypass joins the ran set
        like any start, so the fold waits for it.
        """
        box_rt = self._runtime(box)
        resolved = box_rt.window_skipped_members
        for member in self._members(box):
            rt = self._runtime(member)
            if self._out_of_the_run(member):
                # SEM-20: an iced member is out of the logic entirely, once nothing in it is
                # live or queued; until then it is not yet run (below) and the box waits
                continue
            if member in box_rt.iced_out_members and rt.status not in LIVE_OR_QUEUED:
                # SEM-20: taken off ice during this run, it sits the run out. A forced attempt
                # that queued still keeps the box waiting, as for a resolved member below.
                continue
            if member in resolved and rt.status not in LIVE_OR_QUEUED:
                # an explicit INACTIVE verdict this execution: a run_window skip (DL-154) or an
                # operator's (DL-242), which counts "as if the INACTIVE job returned a status of
                # SUCCESS". A later terminal status on it is done too, and votes as it stands
                # (DL-360). The member's own later start voids the mark.
                continue
            if rt.status not in TERMINAL:
                # live or queued, or INACTIVE: not yet run this box execution (incl. held); a
                # terminal member is done, ran or not (DL-360)
                return False
        return True


def _entity_keys(cond: Cond) -> set[str]:
    """Edge-trigger keys of a condition: local and instance-qualified job names plus "g:NAME" for
    globals (see Oracle._referencers)."""

    from dsl41.conditions import iter_atoms

    keys: set[str] = set()
    for atom in iter_atoms(cond):
        if isinstance(atom, GlobalAtom):
            keys.add(f"g:{atom.name}")
        else:
            keys.add(atom.job.key)
    return keys
