"""Minimal Universal Controller workflow interpreter (the oracle's UC twin).

Completes the tier-c scope from ir-design ss7 ("the expected-divergence
pairs (P-Mxx) against the minimal UC interpreter") and stonebranch Part IV.
Interprets the in-memory UcModel that backend_uc.compile_twin produces --
i.e. what the backend WOULD emit -- so the P-Mxx pairs can show precisely
where compiled UC behavior diverges from AutoSys semantics. Shares Event and
TraceEntry with the AutoSys oracle (ir-design D4: one comparator).

Semantics sources are public [V]/[C] UCS entries only; U-questions stay
open with documented defaults:
- UCS-01: edges carry success/failure/done conditions plus an optional
  variable condition evaluated WHEN THE PREDECESSOR COMPLETES -- never on
  SET_GLOBAL (that timing gap is the M09 divergence). U8's ordering
  property default: variables are read as-is at evaluation time.
- UCS-02: when a predecessor finishes, non-matching edges skip their
  successor paths; a task with ALL incoming edges skipped is Skipped
  (cascades); >= 1 satisfied edge and none pending -> it runs -- skipped
  predecessors do not block.
- UCS-03: joins are conjunctive over non-skipped incoming edges.
- UCS-09: Mutually Exclusive Tasks -- an eligible task with a mutex partner
  currently Running waits in Exclusive Wait, released FIFO when the
  partner completes (the P-M07 divergence: AutoSys n() abandons, UC queues).
  Self-exclusion (n(self)) never reaches the mutex path v1: the one-open-
  instance-per-workflow rule already serializes successive runs (Instance
  Wait, UCS-09) before a self-partner could be seen Running (review N-1).
- UCS-13: all evaluation is within the workflow INSTANCE -- no latching, no
  lookback (the P-M01 divergence).

Interpreter decisions (each with a test; recorded as DL-16):
- STARTJOB/TIMER on a task launches a new instance of its containing
  workflow (UC triggers launch workflows, not tasks): every task resets to
  Waiting, then source tasks (no incoming edges) start. One open instance
  per workflow v1; a STARTJOB while one is open is recorded as a trace
  marker and ignored (Instance Wait territory, UCS-09).
- Completion is script-driven like the AutoSys oracle: STATUS carries an
  explicit status (SUCCESS->Success, FAILURE->Failed, TERMINATED->
  Cancelled) or an exit_code judged against max_exit_success (M31/U4
  default: same boundary as AutoSys). The last exit code is published as
  pseudo-variable "exit:<task>" for M08 var-condition edges.
- ON_ICE marks a task Skip-at-start (M19): when it would otherwise start
  (instance launch or edges satisfied) it goes Skipped and the skip
  cascades. OFF_ICE unmarks. ON_HOLD holds an eligible task (Held blocks
  the instance from completing, M20); OFF_HOLD starts it if its edges were
  already satisfied. KILLJOB -> Cancelled (M23: Force Finish/Cancel
  analog). FORCE_STARTJOB starts the task inside the open instance
  regardless of edges (M22: forced runs feed no latches -- there are none).
- Workflow completion: every task terminal (Success/Failed/Skipped/
  Cancelled) -> instance closes; the workflow itself gets a trace entry,
  Failed if any task Failed/Cancelled else Success (UCS-04 approximation;
  U2 keeps the exact derivation open). Synthetic wf_* workflows emit trace
  entries too; the comparator ignores names absent from the AutoSys side.
- No run_window analog exists (M27 is refused by compile_twin) -- the
  P-M27 pair shows the divergence that absence causes.

The comparator (`job_outcomes`, `first_divergence`) normalizes UC statuses
into the AutoSys transition vocabulary (Success->SUCCESS, Failed->FAILURE,
Cancelled->TERMINATED, Skipped->SKIPPED) and compares per-job transition
sequences; SKIPPED has no AutoSys analog by design -- a SKIPPED-vs-ran
mismatch IS a reportable divergence.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel

from dsl41.backend_uc import UcEdge, UcModel, UcVarCondition
from dsl41.conditions import CmpOp, compare_value
from dsl41.oracle import Event, TraceEntry

UcTaskStatus = Literal[
    "Defined",  # no open instance contains the task
    "Waiting",
    "Held",
    "ExclusiveWait",
    "Running",
    "Success",
    "Failed",
    "Cancelled",
    "Skipped",
]

_TERMINAL: frozenset[str] = frozenset({"Success", "Failed", "Cancelled", "Skipped"})

_TO_AUTOSYS = {
    "Running": "RUNNING",
    "Success": "SUCCESS",
    "Failed": "FAILURE",
    "Cancelled": "TERMINATED",
    "Skipped": "SKIPPED",
}


class UcOracleError(ValueError):
    pass


class _EdgeState(BaseModel):
    edge: UcEdge
    state: Literal["pending", "satisfied", "skipped"] = "pending"


class _Instance(BaseModel):
    workflow: str
    number: int
    task_status: dict[str, str]
    edges: list[_EdgeState]
    open: bool = True


class UcOracle:
    """Deterministic interpreter over one UcModel (see module docstring)."""

    def __init__(self, model: UcModel) -> None:
        self.model = model
        self.variables: dict[str, str] = {}
        self._trace: list[TraceEntry] = []
        self._now: datetime | None = None
        self._instances: dict[str, _Instance] = {}  # workflow -> open instance
        self._instance_counter = 0
        self._iced: set[str] = set()
        self._held: set[str] = set()
        self._exclusive_wait: deque[str] = deque()
        self._workflow_of: dict[str, str] = {}
        for workflow in model.workflows:
            # UCS-0 "workflows are themselves tasks" (review M-2): the
            # workflow launches by its own (box) name and by nested-box
            # aliases, so AutoSys-style STARTJOB(box) scripts work unchanged
            self._workflow_of[workflow.name] = workflow.name
            for alias in workflow.aliases:
                self._workflow_of[alias] = workflow.name
            for task in workflow.tasks:
                self._workflow_of[task] = workflow.name
        self._mutex_of: dict[str, set[str]] = {}
        for group in model.mutex_groups:
            for task in group:
                partners = set(group) - {task} or {task}  # self-exclusion partners itself
                self._mutex_of.setdefault(task, set()).update(partners)

    # ------------------------------------------------------------------ plumbing

    def trace(self) -> list[TraceEntry]:
        return [entry.model_copy() for entry in self._trace]

    def _record(self, job: str, transition: str, cause: str) -> None:
        assert self._now is not None
        self._trace.append(TraceEntry(at=self._now, job=job, transition=transition, cause=cause))

    def run_script(self, events: list[Event]) -> list[TraceEntry]:
        for ev in events:
            self.feed(ev)
        return self.trace()

    def feed(self, ev: Event) -> None:
        if self._now is not None and ev.at < self._now:
            raise UcOracleError(f"feed time went backwards: {ev.at} < {self._now}")
        self._now = ev.at
        kind = ev.kind
        if kind in ("STARTJOB", "TIMER", "FORCE_STARTJOB"):
            job = self._required_job(ev)
            if kind == "FORCE_STARTJOB":
                self._force_start(job)
            else:
                self._launch_workflow_for(job)
        elif kind == "STATUS":
            self._handle_status(ev)
        elif kind == "SET_GLOBAL":
            name = ev.payload.get("name")
            if not isinstance(name, str):
                raise UcOracleError("SET_GLOBAL requires payload.name")
            self.variables[name] = str(ev.payload.get("value"))
            # deliberately NO re-evaluation: UC edge variable conditions are
            # read at predecessor completion (UCS-01) -- the M09 divergence
        elif kind == "ON_ICE":
            self._iced.add(self._required_job(ev))
            self._record(self._required_job(ev), "SKIP_FLAG", "M19: definition-level Skip")
        elif kind == "OFF_ICE":
            self._iced.discard(self._required_job(ev))
            self._record(self._required_job(ev), "SKIP_FLAG_CLEARED", "M19")
        elif kind == "ON_HOLD":
            self._held.add(self._required_job(ev))
            self._record(self._required_job(ev), "HELD", "M20: Hold task")
        elif kind == "OFF_HOLD":
            job = self._required_job(ev)
            self._held.discard(job)
            self._record(job, "RELEASED", "M20: Release")
            instance = self._open_instance_of(job)
            if instance is not None and instance.task_status.get(job) == "Held":
                instance.task_status[job] = "Waiting"
                self._try_start(instance, job, cause="released from hold")
                self._close_if_done(instance)
        elif kind == "KILLJOB":
            job = self._required_job(ev)
            instance = self._open_instance_of(job)
            if instance is not None and instance.task_status.get(job) == "Running":
                self._complete(instance, job, "Cancelled", cause="KILLJOB (M23)")
        else:
            raise UcOracleError(f"uninjectable event kind for the UC twin: {kind!r}")

    def _required_job(self, ev: Event) -> str:
        job = ev.job()
        if job is None:
            raise UcOracleError(f"{ev.kind} requires payload.job")
        return job

    # ------------------------------------------------------------ instance launch

    def _launch_workflow_for(self, job: str) -> None:
        workflow_name = self._workflow_of.get(job)
        if workflow_name is None:
            self._record(job, "NO_WORKFLOW", "task not in the compiled model")
            return
        if workflow_name in self._instances and self._instances[workflow_name].open:
            self._record(
                workflow_name,
                "INSTANCE_OPEN",
                "STARTJOB ignored: one open instance per workflow v1 (UCS-09"
                " Instance Wait territory)",
            )
            return
        workflow = next(w for w in self.model.workflows if w.name == workflow_name)
        self._instance_counter += 1
        instance = _Instance(
            workflow=workflow_name,
            number=self._instance_counter,
            task_status={task: "Waiting" for task in workflow.tasks},
            edges=[_EdgeState(edge=e) for e in workflow.edges],
        )
        self._instances[workflow_name] = instance
        # "INSTANCE->Running" normalizes to the RUNNING milestone so a
        # box-named workflow compares cleanly against the AutoSys box's
        # RUNNING+terminal shape (review C-1)
        self._record(workflow_name, "INSTANCE->Running", f"trigger via {job!r}")
        for task in workflow.tasks:
            if not self._incoming(instance, task):
                self._try_start(instance, task, cause="source task at instance launch")
        self._close_if_done(instance)

    def _incoming(self, instance: _Instance, task: str) -> list[_EdgeState]:
        return [es for es in instance.edges if es.edge.dst == task]

    def _open_instance_of(self, task: str) -> _Instance | None:
        workflow_name = self._workflow_of.get(task)
        if workflow_name is None:
            return None
        instance = self._instances.get(workflow_name)
        if instance is None or not instance.open:
            return None
        return instance

    # -------------------------------------------------------------- task starting

    def _try_start(self, instance: _Instance, task: str, cause: str) -> None:
        if instance.task_status.get(task) != "Waiting":
            return
        if task in self._iced:
            self._skip(instance, task, cause="Skip flag set (M19)")
            return
        if task in self._held:
            instance.task_status[task] = "Held"
            self._record(task, "Waiting->Held", "held (M20)")
            return
        blockers = self._mutex_of.get(task, set())
        if any(self._is_running_anywhere(partner) for partner in blockers):
            instance.task_status[task] = "ExclusiveWait"
            self._exclusive_wait.append(task)
            self._record(task, "Waiting->ExclusiveWait", "mutex partner running (UCS-09)")
            return
        instance.task_status[task] = "Running"
        self._record(task, "Waiting->Running", cause)

    def _is_running_anywhere(self, task: str) -> bool:
        for instance in self._instances.values():
            if instance.open and instance.task_status.get(task) == "Running":
                return True
        return False

    def _force_start(self, task: str) -> None:
        instance = self._open_instance_of(task)
        if instance is None:
            # M22/review E-1: UC's Launch-task analog -- open the containing
            # workflow instance first, then clear the task's dependencies
            self._launch_workflow_for(task)
            instance = self._open_instance_of(task)
            if instance is None:
                self._record(task, "NO_WORKFLOW", "FORCE ignored: task not in the model")
                return
        if instance.task_status.get(task) in ("Waiting", "Held", "ExclusiveWait"):
            instance.task_status[task] = "Running"
            self._record(task, "FORCED->Running", "M22: Launch/Clear Dependencies analog")

    def _skip(self, instance: _Instance, task: str, cause: str) -> None:
        instance.task_status[task] = "Skipped"
        self._record(task, "Waiting->Skipped", cause)
        for es in instance.edges:
            if es.edge.src == task and es.state == "pending":
                es.state = "skipped"
        self._propagate(instance)

    # ------------------------------------------------------------------ completion

    def _handle_status(self, ev: Event) -> None:
        task = self._required_job(ev)
        instance = self._open_instance_of(task)
        if instance is None or instance.task_status.get(task) != "Running":
            self._record(task, "STATUS_IGNORED", "no running task instance for it")
            return
        status = ev.payload.get("status")
        exit_code = ev.payload.get("exit_code")
        if isinstance(exit_code, int):
            self.variables[f"exit:{task}"] = str(exit_code)
        if status is None:
            if not isinstance(exit_code, int):
                raise UcOracleError("STATUS requires payload.status or integer exit_code")
            ceiling = self.model.max_exit_success.get(task, 0)
            status = "SUCCESS" if exit_code <= ceiling else "FAILURE"
        uc_status = {"SUCCESS": "Success", "FAILURE": "Failed", "TERMINATED": "Cancelled"}.get(
            str(status)
        )
        if uc_status is None:
            raise UcOracleError(f"unmappable status {status!r} for the UC twin")
        self._complete(instance, task, uc_status, cause="injected STATUS")

    def _complete(self, instance: _Instance, task: str, uc_status: str, cause: str) -> None:
        instance.task_status[task] = uc_status
        self._record(task, f"Running->{uc_status}", cause)
        self._release_exclusive(task)
        # UCS-02: evaluate outgoing edges at completion time
        for es in instance.edges:
            if es.edge.src != task or es.state != "pending":
                continue
            if self._edge_matches(es.edge, uc_status):
                es.state = "satisfied"
            else:
                es.state = "skipped"
        self._propagate(instance)
        self._close_if_done(instance)

    def _edge_matches(self, edge: UcEdge, uc_status: str) -> bool:
        # UCS-01/M06 (review M-1): Cancelled is NOT Failed -- a failure edge
        # stays unsatisfied on Cancelled, matching AutoSys f() vs TERMINATED
        matched = (
            (edge.condition == "success" and uc_status == "Success")
            or (edge.condition == "failure" and uc_status == "Failed")
            or (edge.condition == "cancelled" and uc_status == "Cancelled")
            or (edge.condition == "done" and uc_status in ("Success", "Failed", "Cancelled"))
        )
        if not matched:
            return False
        if edge.var_condition is not None:
            return self._var_condition_true(edge.var_condition)
        return True

    def _var_condition_true(self, condition: UcVarCondition) -> bool:
        actual = self.variables.get(condition.name)
        if actual is None:
            return False
        return compare_value(actual, cast(CmpOp, condition.op), condition.value)

    def _release_exclusive(self, completed: str) -> None:
        partners = self._mutex_of.get(completed, set())
        released: list[str] = []
        for waiting in list(self._exclusive_wait):
            if waiting not in partners and completed not in self._mutex_of.get(waiting, set()):
                continue
            instance = self._open_instance_of(waiting)
            if instance is None or instance.task_status.get(waiting) != "ExclusiveWait":
                released.append(waiting)  # stale entry
                continue
            blockers = self._mutex_of.get(waiting, set())
            if any(self._is_running_anywhere(p) for p in blockers):
                continue
            instance.task_status[waiting] = "Running"
            self._record(waiting, "ExclusiveWait->Running", "mutex released (UCS-09)")
            released.append(waiting)
        for task in released:
            try:
                self._exclusive_wait.remove(task)
            except ValueError:
                pass

    # ------------------------------------------------------------ skip propagation

    def _propagate(self, instance: _Instance) -> None:
        """UCS-02 fixpoint: tasks whose incoming edges are all resolved start
        or skip; skips cascade."""
        changed = True
        while changed:
            changed = False
            for task, status in list(instance.task_status.items()):
                if status != "Waiting":
                    continue
                incoming = self._incoming(instance, task)
                if not incoming or any(es.state == "pending" for es in incoming):
                    continue
                if all(es.state == "skipped" for es in incoming):
                    if instance.task_status[task] == "Waiting":
                        self._skip(instance, task, cause="all predecessors skipped (UCS-02)")
                        changed = True
                else:  # >= 1 satisfied: skipped predecessors do not block
                    self._try_start(instance, task, cause="dependencies satisfied (UCS-02)")
                    changed = instance.task_status[task] != "Waiting"

    def _close_if_done(self, instance: _Instance) -> None:
        if not instance.open:
            return
        if all(status in _TERMINAL for status in instance.task_status.values()):
            instance.open = False
            failed = any(
                status in ("Failed", "Cancelled") for status in instance.task_status.values()
            )
            outcome = "Failed" if failed else "Success"
            self._record(
                instance.workflow,
                f"INSTANCE->{outcome}",
                "all tasks terminal (UCS-04 approximation; U2 open)",
            )


# ---------------------------------------------------------------------- comparator


def normalize_transition(transition: str) -> str | None:
    """Map a UC trace transition into the AutoSys vocabulary; None for
    UC-internal markers the comparator ignores."""
    if "->" not in transition:
        return None
    target = transition.split("->")[-1]
    return _TO_AUTOSYS.get(target)


_AUTOSYS_MILESTONES = frozenset({"RUNNING", "SUCCESS", "FAILURE", "TERMINATED", "SKIPPED"})


def job_outcomes(trace: list[TraceEntry]) -> dict[str, list[str]]:
    """job -> normalized milestone targets, in order. Works for both
    engines: AutoSys transitions keep their targets, UC ones map through
    _TO_AUTOSYS. STARTING is deliberately dropped -- AutoSys's
    STARTING->RUNNING pair vs UC's Waiting->Running would make every pair
    diverge cosmetically; RUNNING is the shared 'it actually ran'
    milestone."""
    out: dict[str, list[str]] = {}
    for entry in trace:
        if "->" not in entry.transition:
            continue
        target = entry.transition.split("->")[-1]
        normalized = _TO_AUTOSYS.get(target, target if target in _AUTOSYS_MILESTONES else None)
        if normalized is None:
            continue
        out.setdefault(entry.job, []).append(normalized)
    return out


class Divergence(BaseModel):
    job: str
    autosys: list[str]
    uc: list[str]


def first_divergence(
    autosys_trace: list[TraceEntry],
    uc_trace: list[TraceEntry],
    jobs: list[str],
) -> Divergence | None:
    """First job (in the given order) whose normalized outcome sequences
    differ between the engines. SKIPPED entries are dropped for the
    comparison -- an explicit UC Skip and an AutoSys job that was simply
    never evaluated are the same observable outcome ("did not run"), while
    SKIPPED-vs-ran still diverges. The Divergence payload keeps the RAW
    sequences (SKIPPED included) for reporting. The P-Mxx pair tests assert
    this is not None -- divergence is the EXPECTED, documented outcome."""
    a_outcomes = job_outcomes(autosys_trace)
    b_outcomes = job_outcomes(uc_trace)
    for job in jobs:
        left = a_outcomes.get(job, [])
        right = b_outcomes.get(job, [])
        if [t for t in left if t != "SKIPPED"] != [t for t in right if t != "SKIPPED"]:
            return Divergence(job=job, autosys=left, uc=right)
    return None
