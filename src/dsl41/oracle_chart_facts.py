"""The payloads the chart oracle gives the `job` chart, and a job instance's
data (docs/statechart-engine.md §11, DL-342, DL-344).

`oracle_chart` dispatches each converted input to the job's instance with a
payload whose fields are oracle facts. A fact is computed on the guards' first
read and kept, in the order today's rules test it, so an input computes only
the facts the old oracle computes. Every fact is a read: it writes nothing to
the store, the trace, the timers or the resolutions.

- `StartFacts`: a `start` attempt's payload, `Attempt`'s fields (DL-342).
- `NoexecFacts`: ON_NOEXEC's payload, `Noexec`'s fields (DL-344).
- `job_data`: a job instance's `JobData`, from its catalog entry and the
  units its row holds.
"""

from __future__ import annotations

from typing import Any, cast

from dsl41.capacity import DemandEntry, to_reservations
from dsl41.ir import JobIR
from dsl41.job_chart import BoxGate, JobData, Release, WindowSide
from dsl41.oracle import MAX_NESTED_STARTS, Oracle
from dsl41.oracle_state import LIVE, JobRuntime


class StartFacts:
    """A `start` payload: `Attempt`'s fields, each oracle fact computed on its
    first read and kept.

    `force`, `explicit`, `scheduled`, `stale`, `rescan` and `dated` are eager:
    who asks, the window timer's own facts (DL-343), which today's code
    reads before any gate, and an attribute of the job. `box`, `condition`,
    `window`, `bounded`, `box_noexec`, `admissible` and `demand` are lazy. The
    guards read them in `start_verdict`'s order, which is the order today's
    `_attempt_start`, `_start` and `_start_admitted` test them, so an attempt
    computes the facts the old oracle computes and no other: an iced job's
    condition is never evaluated, nor the window of a job whose condition is
    false. Every fact is a read: `_runtime` of the box, `_cond_true`,
    `_window_side`, `_noexec_bypasses`, the pool's `demand_vector` and
    `_admissible` write nothing to the store, the trace, the timers or the
    resolutions (`_window_side` fills the zone cache, as it does today)."""

    __slots__ = (
        "force",
        "explicit",
        "scheduled",
        "stale",
        "rescan",
        "dated",
        "_oracle",
        "_job",
        "_job_ir",
        "_known",
        "_probe",
    )

    def __init__(
        self,
        oracle: Oracle,
        job: str,
        job_ir: JobIR | None,
        *,
        force: bool = False,
        explicit: bool = False,
        scheduled: bool = False,
        stale: bool = False,
        rescan: bool = False,
        known: dict[str, Any] | None = None,
        probe: bool = False,
    ) -> None:
        self.force = force
        self.explicit = explicit
        self.scheduled = scheduled
        self.stale = stale
        self.rescan = rescan
        self.dated = job_ir is not None and job_ir.schedule is not None
        self._oracle = oracle
        self._job = job
        self._job_ir = job_ir
        self._known: dict[str, Any] = {} if known is None else known
        self._probe = probe

    def __repr__(self) -> str:
        known = ", ".join(f"{name}={value!r}" for name, value in self._known.items())
        return f"StartFacts({self._job!r}, force={self.force}, explicit={self.explicit}, {known})"

    def relayed(self, *, force: bool) -> StartFacts:
        """`Attempt.relayed`: the attempt a relay passes on."""
        return StartFacts(
            self._oracle,
            self._job,
            self._job_ir,
            force=force,
            explicit=True,
            scheduled=True,
            stale=self.stale,
            rescan=self.rescan,
        )

    def assuming(self) -> StartFacts:
        """This attempt, sharing its facts, with the two that today's code
        reads after a settlement (`_settle_ended_run`) taken as admitting:
        a read that says whether the attempt gets that far."""
        return StartFacts(
            self._oracle,
            self._job,
            self._job_ir,
            force=self.force,
            explicit=self.explicit,
            scheduled=self.scheduled,
            stale=self.stale,
            rescan=self.rescan,
            known=self._known,
            probe=True,
        )

    def known(self, name: str) -> Any:
        """A fact if it was computed, else None."""
        return self._known.get(name)

    def _job_ir_known(self) -> JobIR:
        assert self._job_ir is not None, "a fact of a job outside the catalog"
        return self._job_ir

    @property
    def box(self) -> BoxGate:
        """The member's gate on its box's run, as `_attempt_start` reads it."""
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
        """`_window_side`, which `_run_window_permits` calls."""
        if "window" not in self._known:
            side, next_open = self._oracle._window_side(self._job_ir_known())
            self._known["window"] = side
            self._known["next_open"] = next_open
        return cast(WindowSide, self._known["window"])

    @property
    def bounded(self) -> bool:
        """The job has `MAX_NESTED_STARTS` starts in progress (`_start`, DL-304)."""
        if "bounded" not in self._known:
            depth = self._oracle._starts_in_progress.get(self._job, 0)
            self._known["bounded"] = depth >= MAX_NESTED_STARTS
        return cast(bool, self._known["bounded"])

    @property
    def box_noexec(self) -> bool:
        """`_noexec_bypasses`. The guard reads it only for a job that is not a
        box and has no ON_NOEXEC of its own, where it is the boxes above."""
        if self._probe:
            return False
        if "box_noexec" not in self._known:
            job_ir = self._job_ir_known()
            self._known["box_noexec"] = self._oracle._noexec_bypasses(job_ir)
        return cast(bool, self._known["box_noexec"])

    @property
    def admissible(self) -> bool:
        """`_admissible` over the demand vector."""
        if self._probe:
            return True
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
        """The pool's `demand_vector`, which `_start_admitted` reads."""
        if "vector" not in self._known:
            self._known["vector"] = self._oracle._pool.demand_vector(self._job_ir_known())
        return cast(list[DemandEntry], self._known["vector"])


def job_data(oracle: Oracle, job: str, row: JobRuntime) -> JobData:
    """A job instance's data: its catalog entry's facts, and what leaving the
    run does with the units the row holds after a SUCCESS and after any other
    end, as `RuntimeState.release_reservations` decides it with the pool's
    `keeps_held`. A row that holds nothing frees nothing."""
    job_ir = oracle.catalog.jobs.get(job)

    def release(success: bool) -> Release:
        kept = spent = False
        for unit in row.reservations:
            if unit.release_policy == "completion" or (
                unit.release_policy == "success" and success
            ):
                continue
            if oracle._pool.keeps_held(unit.bucket):
                kept = True
            else:
                spent = True
        return "keep" if kept else "spend" if spent else "free"

    if job_ir is None:
        return JobData(
            catalog=False, release_on_success=release(True), release_otherwise=release(False)
        )
    return JobData(
        box=job_ir.job_type == "BOX",
        job_terminator=job_ir.box.job_terminator,
        auto_hold=job_ir.sem.auto_hold,
        release_on_success=release(True),
        release_otherwise=release(False),
    )


class NoexecFacts:
    """ON_NOEXEC's payload (`Noexec`), each fact computed when a guard first
    reads it (DL-344). None is eager.

    `tree_busy`: a job the box contains, at any depth, is on ice, STARTING,
    RUNNING or QUE_WAIT, as `_oob_ignored` reads its contained jobs. The guard
    reads it only for a box that is neither on ice nor RUNNING, where today's
    rule reaches that loop. `change`: the box's parent is RUNNING, or a job
    the box contains is not INACTIVE, as `_noexec_box` decides whether the
    box's INACTIVE runs; the guard reads it only for a box that is INACTIVE
    itself (any other status decides it). Neither is read for a job that is
    not a box. Both read rows that exist: a contained job is in the catalog."""

    __slots__ = ("_oracle", "_job", "_known", "_tree")

    def __init__(self, oracle: Oracle, job: str) -> None:
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
