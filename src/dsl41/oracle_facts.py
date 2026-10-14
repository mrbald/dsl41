"""The payloads the oracle gives the `job` chart, and a job instance's
data (docs/statechart-engine.md §11, DL-342, DL-344, DL-345).

The oracle dispatches each input to the job's instance with a payload whose
fields are oracle facts. A fact is computed on the guards' first
read and kept, in the order the rules test it, so an input computes only
the facts its rules read. Every fact is a read: it writes nothing to
the store, the trace, the timers or the resolutions.

- `StartFacts`: a `start` attempt's payload, `Attempt`'s fields (DL-342).
- `NoexecFacts`: ON_NOEXEC's payload, `Noexec`'s fields (DL-344).
- `ReadmitFacts`: a `readmit` payload, `Readmission`'s fields (DL-345).
- `OverrideFacts`, `FoldFacts` and `RecomputeFacts`: the box rules' payloads,
  `Verdict`'s and `BoxVerdict`'s fields (DL-346).
- `Started`: `started`'s payload, whether the admitted run is still the
  row's run (DL-350).
- `RowUnits`: the tier's handle on a job (`Units`) that a payload carries:
  a settlement reads its policy from it when it pops (DL-349), and a row's
  action reads from it what the row sends beyond its own move (DL-350).
- `job_data`: a job instance's `JobData`, from its catalog entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from dsl41.capacity import DemandEntry, to_reservations
from dsl41.conditions import Cond, GlobalAtom, iter_atoms
from dsl41.ir import JobIR
from dsl41.job_chart import BoxGate, JobData, Release, WindowSide
from dsl41.oracle_state import FAILED, LIVE, TERMINAL, JobStatus
from dsl41.state_machine import Context, Effects

if TYPE_CHECKING:
    from dsl41.oracle_cascade import OracleCascade

#: A box rule's verdict: the status it decides, or none.
type Target = Literal["SUCCESS", "FAILURE"] | None


class StartFacts:
    """A `start` payload: `Attempt`'s fields, each oracle fact computed on its
    first read and kept.

    `force`, `explicit`, `scheduled`, `stale`, `rescan`, `decided` and `dated`
    are eager: who asks, the window timer's own facts (DL-343), which are read
    before any gate, and an attribute of the job. `box`,
    `condition`, `window`, `settlement_owed`, `box_noexec`, `admissible` and
    `demand` are lazy. `cause` is the attempt's cause text, which its writes
    name; `refusal` the text a stale window timer's refusal writes in place of
    the rule's reason (DL-343). The guards read them in `start_verdict`'s
    order, so an attempt computes the facts its verdict reads and no other: an iced job's condition is never evaluated, nor the window of a
    job whose condition is false. Every fact is a read: `_runtime` of the
    box, `_cond_true`, `_window_side`, `_late_settlements`,
    `_noexec_above`, the pool's `demand_vector` and `_admissible` write
    nothing to the store, the trace, the timers or the resolutions
    (`_window_side` fills the zone cache). `units` is the
    job's handle (`RowUnits`)."""

    __slots__ = (
        "force",
        "explicit",
        "scheduled",
        "stale",
        "rescan",
        "decided",
        "dated",
        "cause",
        "refusal",
        "units",
        "_oracle",
        "_job",
        "_job_ir",
        "_known",
    )

    def __init__(
        self,
        oracle: OracleCascade,
        job: str,
        job_ir: JobIR | None,
        *,
        cause: str = "",
        force: bool = False,
        explicit: bool = False,
        scheduled: bool = False,
        stale: bool = False,
        rescan: bool = False,
        decided: bool = False,
        refusal: str | None = None,
        known: dict[str, Any] | None = None,
    ) -> None:
        self.force = force
        self.explicit = explicit
        self.scheduled = scheduled
        self.stale = stale
        self.rescan = rescan
        self.decided = decided
        self.dated = job_ir is not None and job_ir.schedule is not None
        self.cause = cause
        self.refusal = refusal
        self.units = RowUnits(oracle, job)
        self._oracle = oracle
        self._job = job
        self._job_ir = job_ir
        self._known: dict[str, Any] = {} if known is None else known

    def __repr__(self) -> str:
        known = ", ".join(f"{name}={value!r}" for name, value in self._known.items())
        return f"StartFacts({self._job!r}, force={self.force}, explicit={self.explicit}, {known})"

    def _again(self, **changes: Any) -> StartFacts:
        fields: dict[str, Any] = {
            "cause": self.cause,
            "force": self.force,
            "explicit": self.explicit,
            "scheduled": self.scheduled,
            "stale": self.stale,
            "rescan": self.rescan,
            "decided": self.decided,
            "refusal": self.refusal,
        }
        fields.update(changes)
        return StartFacts(self._oracle, self._job, self._job_ir, **fields)

    def relayed(self, *, force: bool) -> StartFacts:
        """`Attempt.relayed`: the attempt a relay passes on."""
        return self._again(force=force, explicit=True, scheduled=True)

    def settled(self) -> StartFacts:
        """The same attempt after its run's owed settlement (`start_settles_first`,
        DL-340): the facts read before it carry over, since a settlement moves
        none of them, and the owed settlement and everything after it are read
        again."""
        known = {name: value for name, value in self._known.items() if name != "settlement_owed"}
        return self._again(known=known)

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    def _job_ir_known(self) -> JobIR:
        assert self._job_ir is not None, "a fact of a job outside the catalog"
        return self._job_ir

    @property
    def box(self) -> BoxGate:
        """The member's gate on its box's run (SEM-10, SEM-20)."""
        if "box" not in self._known:
            box = self._job_ir_known().box.box_name
            gate: BoxGate = "none"
            if box is not None:
                row = self._oracle._runtime(box)
                if row.status != "RUNNING":
                    gate = "idle"
                elif self._job in row.ran_members:
                    gate = "ran"
                else:
                    gate = "iced_out" if self._job in row.iced_out_members else "running"
            self._known["box"] = gate
        return cast(BoxGate, self._known["box"])

    @property
    def condition(self) -> bool:
        """The job's condition holds, or it has none (`_cond_true`)."""
        if "condition" not in self._known:
            gate = self._job_ir_known().sem.condition
            self._known["condition"] = gate is None or self._oracle._cond_true(gate.cond, self._job)
        return cast(bool, self._known["condition"])

    @property
    def window(self) -> WindowSide:
        """The SEM-33 closer-edge side of the job's window (`_window_side`)."""
        if "window" not in self._known:
            side, next_open = self._oracle._window_side(self._job_ir_known())
            self._known["window"] = side
            self._known["next_open"] = next_open
        return cast(WindowSide, self._known["window"])

    @property
    def settlement_owed(self) -> bool:
        """A run of the job ended inside a cascade still running its box rules,
        and its settlement waits (job.401, DL-340): the late
        settlement of the row's run is registered and not done."""
        if "settlement_owed" not in self._known:
            run = self._oracle._runtime(self._job).run_number
            lates = self._oracle._late_settlements.get(self._job, ())
            self._known["settlement_owed"] = any(x.run == run and not x.done for x in lates)
        return cast(bool, self._known["settlement_owed"])

    @property
    def box_noexec(self) -> bool:
        """`_noexec_above`. The guard reads it only for a job that is not a
        box and has no ON_NOEXEC of its own, where it is the boxes above."""
        if "box_noexec" not in self._known:
            job_ir = self._job_ir_known()
            self._known["box_noexec"] = self._oracle._noexec_above(job_ir)
        return cast(bool, self._known["box_noexec"])

    @property
    def admissible(self) -> bool:
        """`_admissible` over the demand vector."""
        if "admissible" not in self._known:
            job_ir = self._job_ir_known()
            self._known["admissible"] = self._oracle._admissible(
                job_ir, self.vector(), force=self.force
            )
        return cast(bool, self._known["admissible"])

    @property
    def demand(self) -> bool:
        """The demand vector reserves units, as `reserve` and `take_over_held`
        read it (`to_reservations`)."""
        return bool(to_reservations(self.vector()))

    def vector(self) -> list[DemandEntry]:
        """The pool's `demand_vector`, which the admission reads."""
        if "vector" not in self._known:
            self._known["vector"] = self._oracle._pool.demand_vector(self._job_ir_known())
        return cast(list[DemandEntry], self._known["vector"])


class RowUnits:
    """The tier's handle on one job (`Units`), which the oracle's payloads
    carry. `release` reads the row's units when the settlement pops, and decides as `RuntimeState.release_reservations`
    does with the pool's `keeps_held`: a `completion` unit is freed by every
    end and a `success` unit by SUCCESS; what is not freed is kept on a
    renewable and spent on a depletable, and one kept unit makes the end keep.
    A row that holds nothing frees nothing (DL-349). `consequences` reads what
    a row of the job sends beyond its own move from the oracle
    (`OracleCascade._consequences`, DL-350). Both are reads."""

    __slots__ = ("_oracle", "_job")

    def __init__(self, oracle: OracleCascade, job: str) -> None:
        self._oracle = oracle
        self._job = job

    def __repr__(self) -> str:
        return f"RowUnits({self._job!r})"

    def release(self, success: bool) -> Release:
        """What leaving the run does with the units the row holds now."""
        kept = spent = False
        for unit in self._oracle._runtime(self._job).reservations:
            if unit.release_policy == "completion" or (
                unit.release_policy == "success" and success
            ):
                continue
            if self._oracle._pool.keeps_held(unit.bucket):
                kept = True
            else:
                spent = True
        return "keep" if kept else "spend" if spent else "free"

    def consequences(self, ctx: Context, kind: str) -> Effects | None:
        """What a row of `kind` of the job sends and reports beyond its move."""
        return self._oracle._consequences(self._job, ctx, kind)


def job_data(oracle: OracleCascade, job: str) -> JobData:
    """A job instance's data: its catalog entry's facts, which change only
    when a catalog is installed. A row whose job the catalog does not define
    has `catalog=False`."""
    job_ir = oracle.catalog.jobs.get(job)
    if job_ir is None:
        return JobData(catalog=False)
    return JobData(
        box=job_ir.job_type == "BOX",
        job_terminator=job_ir.box.job_terminator,
        auto_hold=job_ir.sem.auto_hold,
    )


class NoexecFacts:
    """ON_NOEXEC's payload (`Noexec`), each fact computed when a guard first
    reads it (DL-344). None is eager.

    `tree_busy`: a job the box contains, at any depth, is on ice, STARTING,
    RUNNING or QUE_WAIT, as `_oob_ignored` reads its contained jobs. The guard
    reads it only for a box that is neither on ice nor RUNNING, where the
    rule reaches that loop. `change`: the box's parent is RUNNING, or a job
    the box contains is not INACTIVE, as `noexec_settles_box` decides whether
    the box's INACTIVE runs; the guard reads it only for a box that is INACTIVE
    itself (any other status decides it). Neither is read for a job that is
    not a box. Both read rows that exist: a contained job is in the catalog.
    `units` is the box's or the job's handle (`RowUnits`)."""

    __slots__ = ("units", "_oracle", "_job", "_known", "_tree")

    def __init__(self, oracle: OracleCascade, job: str) -> None:
        self.units = RowUnits(oracle, job)
        self._oracle = oracle
        self._job = job
        self._known: dict[str, Any] = {}
        self._tree: list[str] | None = None

    def __repr__(self) -> str:
        known = (f"{name}={value!r}" for name, value in self._known.items())
        return f"NoexecFacts({', '.join((repr(self._job), *known))})"

    def _contained(self) -> list[str]:
        """The jobs the box contains, read once for both facts."""
        if self._tree is None:
            self._tree = self._oracle._contained(self._job, skip_live=False)
        return self._tree

    @property
    def tree_busy(self) -> bool:
        """A contained job is on ice, live or queued."""
        if "tree_busy" not in self._known:
            rows = (self._oracle._runtime(member) for member in self._contained())
            self._known["tree_busy"] = any(
                row.on_ice or row.status in LIVE | {"QUE_WAIT"} for row in rows
            )
        return cast(bool, self._known["tree_busy"])

    @property
    def change(self) -> bool:
        """The box's parent is RUNNING, or a contained job is not INACTIVE.
        One guard reads it, once (`noexec_settles_box`), so it is kept only
        for the payload's summary."""
        oracle = self._oracle
        parent = oracle.catalog.jobs[self._job].box.box_name
        change = (parent is not None and oracle._status(parent) == "RUNNING") or any(
            oracle._status(member) != "INACTIVE" for member in self._contained()
        )
        self._known["change"] = change
        return change


class ReadmitFacts:
    """A `readmit` payload (`Readmission`): one queue scan's attempt for a
    waiter (DL-345), each fact computed when a guard first reads it.

    `keep_stopped` is eager: it is the scan's own mode, not a fact. `box`,
    `admissible`, `recheck_fails` and `demand` are lazy. `readmit_verdict`
    reads them in the rule's order, so a readmission computes the facts its
    verdict reads and no other: the box's status for a member
    only, the demand vector and `_admissible` only for a waiter that is not
    held and whose box runs, `_queued_recheck` only for one that fits, and the
    reservations only for one admitted. Every fact is a read: `_runtime` of
    the box, the pool's `demand_vector`, `_admissible` and `_queued_recheck`
    write nothing to the store, the trace, the timers or the resolutions
    (`_queued_recheck` fills the calendar and zone caches).
    The order of the waiters is not a fact: the scan reads it off the rows
    (`CapacityPool.sorted_waiters`), and each waiter gets its own `readmit`."""

    __slots__ = ("keep_stopped", "units", "_oracle", "_job", "_job_ir", "_known")

    def __init__(
        self, oracle: OracleCascade, job: str, job_ir: JobIR, *, keep_stopped: bool
    ) -> None:
        self.keep_stopped = keep_stopped
        self.units = RowUnits(oracle, job)
        self._oracle = oracle
        self._job = job
        self._job_ir = job_ir
        self._known: dict[str, Any] = {}

    def __repr__(self) -> str:
        known = (f"{name}={value!r}" for name, value in self._known.items())
        return f"ReadmitFacts({', '.join((repr(self._job), f'keep_stopped={self.keep_stopped}', *known))})"

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    @property
    def box(self) -> Literal["none", "running", "idle"]:
        """The member's box is RUNNING or not; `none` for a job in no box."""
        if "box" not in self._known:
            box = self._job_ir.box.box_name
            running = box is not None and self._oracle._runtime(box).status == "RUNNING"
            self._known["box"] = "none" if box is None else "running" if running else "idle"
        return cast(Literal["none", "running", "idle"], self._known["box"])

    @property
    def admissible(self) -> bool:
        """`_admissible` over the demand vector, unforced."""
        if "admissible" not in self._known:
            self._known["admissible"] = self._oracle._admissible(
                self._job_ir, self.vector(), force=False
            )
        return cast(bool, self._known["admissible"])

    @property
    def recheck_fails(self) -> bool:
        """`_queued_recheck` names a failed check (DL-257); the check and its
        reason are kept for the write (`known("recheck")`)."""
        if "recheck" not in self._known:
            self._known["recheck"] = self._oracle._queued_recheck(self._job_ir)
        return self._known["recheck"] is not None

    @property
    def demand(self) -> bool:
        """The demand vector reserves units (`to_reservations`)."""
        return bool(to_reservations(self.vector()))

    def vector(self) -> list[DemandEntry]:
        """The pool's `demand_vector`, which the readmission reads."""
        if "vector" not in self._known:
            self._known["vector"] = self._oracle._pool.demand_vector(self._job_ir)
        return cast(list[DemandEntry], self._known["vector"])


def _named_jobs(cond: Cond) -> set[str]:
    """The local jobs a condition names: an override reads its condition on
    their moves (SEM-12)."""
    return {
        atom.job.name
        for atom in iter_atoms(cond)
        if not isinstance(atom, GlobalAtom) and atom.job.instance is None
    }


class OverrideFacts:
    """`member moves`' payload (`Verdict`): the step of a RUNNING box's
    completion door that reads its overrides, for the direct parent and for
    each box above it (SEM-12). `target` is computed on the guards' first
    read, in the rule's order: box_success, then
    box_failure, each only when the box declares it and the moment evaluates
    it (the condition names the member, or the moment completes the member),
    the second only when the first does not hold. Each is `_cond_true`, a
    read. `units` is the box's handle (`RowUnits`)."""

    __slots__ = ("units", "_oracle", "_box_ir", "_member", "_completed", "_known")

    def __init__(
        self,
        oracle: OracleCascade,
        box_ir: JobIR,
        member: str | None,
        new: str | None,
        *,
        completion_moment: bool,
    ) -> None:
        self.units = RowUnits(oracle, box_ir.name)
        self._oracle = oracle
        self._box_ir = box_ir
        self._member = member
        self._completed = completion_moment or (new is not None and new in TERMINAL)
        self._known: dict[str, Any] = {}

    def __repr__(self) -> str:
        known = (f"{name}={value!r}" for name, value in self._known.items())
        return f"OverrideFacts({', '.join((repr(self._box_ir.name), *known))})"

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    @property
    def target(self) -> Target:
        """The status the first override that holds decides, or None."""
        if "target" not in self._known:
            sem = self._box_ir.sem
            target: Target = None
            for attr, status in ((sem.box_success, "SUCCESS"), (sem.box_failure, "FAILURE")):
                if attr is None:
                    continue
                # an internal reference is evaluated the moment its job moves;
                # an external or global one only at a member's completion moment
                named = self._member is not None and self._member in _named_jobs(attr.cond)
                if (named or self._completed) and self._oracle._cond_true(
                    attr.cond, self._box_ir.name
                ):
                    target = cast(Target, status)
                    break
            self._known["target"] = target
        return cast(Target, self._known["target"])


class FoldFacts:
    """`completion moment`'s payload (`Verdict`): the default fold of a
    RUNNING box's completion door (SEM-11), after no override fired. `target`
    is computed on the guards' first read, in the rule's order: whether every
    member is done (`_all_members_done`), and only then the vote of the
    members that ran: SUCCESS when none failed and no
    box_success is declared, FAILURE when one failed and no box_failure is
    declared, else None (an unmet override suppresses the default). `units`
    is the box's handle (`RowUnits`). `at_start`: the box start left no member
    in the run, which the fold's cause names."""

    __slots__ = ("units", "at_start", "_oracle", "_box_ir", "_known")

    def __init__(self, oracle: OracleCascade, box_ir: JobIR, *, at_start: bool = False) -> None:
        self.units = RowUnits(oracle, box_ir.name)
        self.at_start = at_start
        self._oracle = oracle
        self._box_ir = box_ir
        self._known: dict[str, Any] = {}

    def __repr__(self) -> str:
        known = (f"{name}={value!r}" for name, value in self._known.items())
        return f"FoldFacts({', '.join((repr(self._box_ir.name), *known))})"

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    @property
    def target(self) -> Target:
        """The fold's verdict, or None while a member is not done."""
        if "target" not in self._known:
            oracle = self._oracle
            box = self._box_ir.name
            target: Target = None
            self._known["done"] = done = oracle._all_members_done(box)
            if done:
                ran = oracle._runtime(box).ran_members
                statuses = [oracle._runtime(m).status for m in oracle._members(box) if m in ran]
                failed = any(s in FAILED for s in statuses)
                sem = self._box_ir.sem
                if not failed and sem.box_success is None:
                    target = "SUCCESS"
                elif failed and sem.box_failure is None:
                    target = "FAILURE"
            self._known["target"] = target
        return cast(Target, self._known["target"])


class RecomputeFacts:
    """`box recompute`'s payload (`BoxVerdict`): an idle box re-derives its
    status from its members (SEM-15). `target` and `override` are computed
    together on the guards' first read of either, in the rule's order: the members that vote (INACTIVE ignored, and under
    `idle-box-iced-member=ignore` a member out of the run on ice); no verdict
    when the trigger is out of the run and nothing else votes, or when a voter
    is not terminal; then box_success and box_failure (`_cond_true`, the
    second only when the first does not hold), whose verdict stands whatever
    the box's status; then the default verdict, unless the override of its
    kind is declared. The box's own status is the configuration's. `cause`
    names the member's change, as the recompute's line does; `units` is the
    box's handle (`RowUnits`)."""

    __slots__ = ("cause", "units", "_oracle", "_box_ir", "_trigger", "_known")

    def __init__(self, oracle: OracleCascade, box_ir: JobIR, trigger: str, cause: str = "") -> None:
        self.cause = cause
        self.units = RowUnits(oracle, box_ir.name)
        self._oracle = oracle
        self._box_ir = box_ir
        self._trigger = trigger
        self._known: dict[str, Any] = {}

    def __repr__(self) -> str:
        known = (f"{name}={value!r}" for name, value in self._known.items())
        return f"RecomputeFacts({', '.join((repr(self._box_ir.name), *known))})"

    @property
    def target(self) -> Target:
        """The status the recompute decides, or None."""
        return self._verdict()[0]

    @property
    def override(self) -> bool:
        """box_success or box_failure decided it, not the default verdict."""
        return self._verdict()[1]

    def _verdict(self) -> tuple[Target, bool]:
        if "verdict" not in self._known:
            self._known["verdict"] = self._recompute()
        return cast(tuple[Target, bool], self._known["verdict"])

    def _recompute(self) -> tuple[Target, bool]:
        oracle = self._oracle
        box = self._box_ir.name
        # PENDING: Q16 -- whether an iced member votes in an idle box's recompute
        ignore_iced = oracle.semantics.idle_box_iced_member == "ignore"
        statuses: list[JobStatus] = [
            status
            for member in oracle._members(box)
            if (status := oracle._runtime(member).status) != "INACTIVE"
            and not (ignore_iced and oracle._out_of_the_run(member))
        ]
        if ignore_iced and not statuses and oracle._out_of_the_run(self._trigger):
            return None, False  # an iced member's own change is no verdict
        if not all(s in TERMINAL for s in statuses):
            return None, False
        sem = self._box_ir.sem
        for attr, status in ((sem.box_success, "SUCCESS"), (sem.box_failure, "FAILURE")):
            if attr is not None and oracle._cond_true(attr.cond, box):
                return cast(Target, status), True
        failed = any(s in FAILED for s in statuses)
        suppressed = sem.box_failure is not None if failed else sem.box_success is not None
        return (None if suppressed else "FAILURE" if failed else "SUCCESS"), False


class Started:
    """`started`'s payload (DL-350): whether the admitted run is still the
    row's run when it pops (`current`), and whether
    its admission had a demand, which the RUNNING line names. `units` is the
    job's handle (`RowUnits`)."""

    __slots__ = ("had_demand", "units", "_oracle", "_job", "_run")

    def __init__(self, oracle: OracleCascade, job: str, run: int, *, had_demand: bool) -> None:
        self.had_demand = had_demand
        self.units = RowUnits(oracle, job)
        self._oracle = oracle
        self._job = job
        self._run = run

    def __repr__(self) -> str:
        return f"Started({self._job!r}, run={self._run})"

    @property
    def current(self) -> bool:
        """The admitted run is the row's run."""
        return self._oracle._runtime(self._job).run_number == self._run
