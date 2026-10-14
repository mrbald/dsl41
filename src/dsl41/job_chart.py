"""The composite `job` statechart: one job's status, flags and holding in one
declaration (docs/statechart-engine.md §11, DL-337).

The chart is declared and checked. The oracle moves a job through the three
tables in `oracle_state` (`job_status`, `job_flags`, `job_holding`), and
tests/test_job_chart.py checks this chart against them cell by cell. The second
oracle (`oracle_chart`, DL-341) drives it for the input families converted
so far, and the chart joins `machines.MACHINES` when the last one converts.

Shape. The root `job` has three orthogonal regions, in this order: `status`,
`flags` and `holding`. `status` holds `lifecycle`, the superstate of every
status, and inside it the status sets the rules name: `IDLE` (INACTIVE and
`TERMINAL`), `TERMINAL` (SUCCESS and `FAILED`), `FAILED` (FAILURE and
TERMINATED) and `LIVE` (STARTING and RUNNING), with QUE_WAIT beside them. A row
whose old source was one of those sets keeps one source. `flags` holds one
composite state with four orthogonal regions, `ice`, `hold`, `noexec` and
`arm`, each `off` or `on`. Each starts `off`: a flag seeded `on` (SEM-24) is a
restore, not a move. `holding` is `none`, `reserved` (a run's units) or `held`
(units a renewable kept after the run, DL-256).

The box is not a separate chart. A box is an instance of this chart whose data
says so (`JobData.box`), and the box rules are rows guarded by it.

Ids. Every id carries the chart's name, so each old table's id space is a
hundred: `job.1NN` is `job_status.NN`, `job.2NN` is `job_flags.NN` and
`job.3NN` is `job_holding.NN`, with the same number and meaning. A row that had
several sources is one row per source: the first keeps the number, and the
others take new numbers in the same hundred. Internal rows stay internal. A
box's ON_NOEXEC on the jobs it contains, which `job_flags.09` and `.07` take
under the job's own ON_NOEXEC, is an event of its own, `ON_NOEXEC on its box`,
as its OFF_NOEXEC twin is in the table (`job_flags.11`); its rows take new
numbers (job.231 to job.233, DL-347). The status rows no table declared fill
job.159 to job.199, and `job.4NN` continues them (DL-349).

Guards read three things: the instance's configuration, its data (`JobData`),
and the event's payload. The payload carries the facts the rule code reads
outside the job's own row: its box's run, condition truth, the run window,
admission, the members' verdicts, the policy of a run's end. Each payload
class names the oracle predicate behind each field. A payload also carries
the tier's handle on the job's units (`Payload.units`), from which an action
builds the settlement it sends, so the policy is read when the settlement
pops, from the units the row holds then (DL-349).

One input can move several regions of one job. Today's order of those moves
is the contract, and the region order is not load-bearing. Two forms declare
it:

- a chained item that a row's action reports to the same instance
  (statechart-engine §8). It is the form wherever a later step reads an
  earlier step's write: the chained item reads the configuration at its own
  time. A flag move reports `flag moved`, and the status rule that reads the
  flag follows it: ON_ICE dequeues a waiter, KILLJOB terminates one, a waiter
  that fails the queued recheck leaves the queue, ON_NOEXEC settles the
  status. An admitting verdict sends `start admitted` first, and everything
  the start does after its status move follows from it: an actual start
  consumes the arm (`actual start`); an admitted start reserves or takes
  over units first (`admitted start`). STARTJOB, FORCE_STARTJOB and a
  window-postponed start relay the attempt to `start`, after the forced
  clears.
- rows of a sibling region on the same event, only where every step reads
  the configuration before the input: a forced start clears ice and hold, a
  scheduled tick arms, an injected live status makes held units the run's.

The settlement of a run's units keeps today's two orders. A move on its own
(`Oracle._after_transition`) runs the box rules, then the settlement, then the
wakes: a status move that may end a run requests the continuation `settle_late`
(or `settle_late_success` for an end in SUCCESS), which runs after every item the
row reports and reports `leave STARTING or RUNNING`; the wakes follow it. A
move of the batch INACTIVE path (`Oracle._set_inactive_batch`) settles first:
its row reports `leave STARTING or RUNNING` ahead of anything else. As today,
the settlement carries the end the move made, not the status it finds. One
case reads the status: a box restarted by its own completion can start the
member again before the member's late settlement (job.312). That start settles
the run that ended first, by the end it made (`Admission.previous`), and then
reserves or takes over as any start does; the late settlement, finding the
member live, does nothing (DL-340). A tier that settles that run itself
declares it on the attempt: past the bound, an attempt whose run's
settlement is owed (`Attempt.settlement_owed`) takes the internal row job.401
and moves nothing; the tier settles the run and makes the same attempt again,
so `Attempt.admissible` is read after the settlement (DL-349).

The per-job start bound (DL-304) is the refusal row job.100: an attempt past
the gates and the window, for a job with `MAX_NESTED_STARTS` starts already in
progress, is refused with a trace line whoever asked (`Attempt.bounded`). Not
declared here, and left to the entries that drive the chart: the cascade
between instances (a box's rules on its members, a member's moves on its box,
wakes), and the start retry that OFF_HOLD and ON_NOEXEC make. No state
defers (§12).

The walks that check this chart carry no run window and no true condition;
the facts of those paths are checked by synthesized cells and one windowed
replay, and the old-vs-new harness is their proof.

Modules. The declaration has three parts: the events, the data and the
payloads (`job_chart_events`); the states, the rows and the configuration a
row projects to (`job_chart_rows`); and here the guards, the actions and the
chart `JOB`. This module re-exports the other two's names, so `job_chart`
stays the one import, and this docstring is the contract of all three.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final, Literal

from dsl41.job_chart_events import (
    ACTUAL_START,
    ADMITTED_START,
    FLAG_MOVED,
    FORCE_STARTJOB,
    LEAVE_LIVE,
    ON_NOEXEC,
    READMIT,
    START,
    START_ADMITTED,
    Admission,
    Admitted,
    Attempt,
    BoxVerdict,
    Deadline,
    FlagMoved,
    Injection,
    JobData,
    Noexec,
    Readmission,
    Release,
    Settlement,
    Terminator,
    Units,
    Verdict,
)

# the names `job_chart` re-exports for its importers (`X as X`)
from dsl41.job_chart_events import (
    BOX_ENDS as BOX_ENDS,
    BOX_OFF_NOEXEC as BOX_OFF_NOEXEC,
    BOX_ON_NOEXEC as BOX_ON_NOEXEC,
    BOX_RECOMPUTE as BOX_RECOMPUTE,
    BOX_RUNNING as BOX_RUNNING,
    BOX_RUN_ENDS as BOX_RUN_ENDS,
    BOX_SET_INACTIVE as BOX_SET_INACTIVE,
    BOX_START as BOX_START,
    COMPLETION_MOMENT as COMPLETION_MOMENT,
    DISARM as DISARM,
    KILLJOB as KILLJOB,
    MEMBER_ENDS as MEMBER_ENDS,
    MEMBER_MOVES as MEMBER_MOVES,
    OFF_HOLD as OFF_HOLD,
    OFF_ICE as OFF_ICE,
    OFF_NOEXEC as OFF_NOEXEC,
    ON_HOLD as ON_HOLD,
    ON_ICE as ON_ICE,
    PERIOD_OPENING as PERIOD_OPENING,
    RELEASE_RESOURCE as RELEASE_RESOURCE,
    RUN_WINDOW as RUN_WINDOW,
    STARTED as STARTED,
    STARTJOB as STARTJOB,
    STATUS as STATUS,
    TERM_RUN_TIME as TERM_RUN_TIME,
    BoxGate as BoxGate,
    Payload as Payload,
    WindowSide as WindowSide,
)
from dsl41.job_chart_rows import CHOICES, ROOT, ROWS
from dsl41.job_chart_rows import (
    HoldingState as HoldingState,
    configuration as configuration,
    projection as projection,
    row_configuration as row_configuration,
    row_holding as row_holding,
)
from dsl41.oracle_state import LIVE
from dsl41.state_machine import Context, Effects, Event, Statechart


def _data(ctx: Context) -> JobData:
    data: JobData = ctx.data
    return data


def _injected(ctx: Context) -> str | None:
    injection: Injection = ctx.event.payload
    return injection.status


def _units(ctx: Context) -> Units | None:
    """The tier's handle on the job's units that the item's payload carries
    (`Payload.units`), or None."""
    units: Units | None = getattr(ctx.event.payload, "units", None)
    return units


# ---- the start attempt (SEM-10, SEM-20 to SEM-23, SEM-32, SEM-33, DL-50)

type StartVerdict = Literal[
    "refused",
    "ignored",
    "arm",
    "bounded",
    "settle_first",
    "bypass",
    "on_held",
    "queue",
    "admit",
    "skip",
    "skip_member",
]
_LIVE_OR_QUEUED: Final = frozenset({"STARTING", "RUNNING", "QUE_WAIT"})


def start_verdict(ctx: Context) -> StartVerdict:
    """What a start attempt comes to, in the order the rule code tests it.

    Reads the payload (`Attempt`), the data (`box`, `catalog`) and the
    configuration: the status, ice, hold, noexec, the arm and `held`. A forced
    attempt arrives with ice and hold already cleared by FORCE_STARTJOB's own
    rows, so it reads neither gate.
    """
    attempt: Attempt = ctx.event.payload
    data: JobData = ctx.data
    active = ctx.active
    if attempt.stale or (attempt.rescan and "INACTIVE" not in active):
        return "refused"
    if attempt.rescan or not data.catalog:
        return "ignored"
    if active & _LIVE_OR_QUEUED:
        return "refused" if attempt.explicit else "ignored"
    if not attempt.force:
        gated = _gated(attempt, active)
        if gated is not None:
            return gated
    if attempt.window != "inside":
        return _skipped(attempt, active)
    if attempt.bounded:
        return "bounded"
    if attempt.settlement_owed:
        return "settle_first"
    if not data.box and ("noexec_on" in active or attempt.box_noexec):
        return "bypass"
    if attempt.force and "held" in active and "FAILED" in active:
        return "on_held"
    return "admit" if attempt.admissible else "queue"


def _gated(attempt: Attempt, active: frozenset[str]) -> StartVerdict | None:
    """The gates a plain attempt meets, in order; None when all hold."""
    if "ice_on" in active:
        return "ignored"
    if "hold_on" in active:
        return _latched(attempt, active)
    if attempt.dated and not attempt.scheduled and "arm_off" in active:
        return "ignored"
    if attempt.box in ("idle", "ran", "iced_out"):
        return "refused" if attempt.explicit else "ignored"
    if not attempt.condition:
        return _latched(attempt, active)
    return None


def _latched(attempt: Attempt, active: frozenset[str]) -> StartVerdict:
    """A releasable gate blocks the attempt: a scheduled tick of a dated job
    arms, unless armed already or its box is not RUNNING (`Oracle._arm`)."""
    if attempt.scheduled and attempt.dated and "arm_off" in active and attempt.box != "idle":
        return "arm"
    return "ignored"


def _skipped(attempt: Attempt, active: frozenset[str]) -> StartVerdict:
    """Outside the run window: a postponed start moves nothing; a skip moves a
    completed job to INACTIVE, and a member only in a RUNNING box it has not
    run in (`_window_skip_bypass`)."""
    if attempt.window == "defer" or "INACTIVE" in active:
        return "ignored"
    if attempt.box == "none":
        return "skip"
    return "skip_member" if attempt.box in ("running", "iced_out") else "ignored"


def start_refused(ctx: Context) -> bool:
    """`start_verdict` is `refused`: a START_REFUSED line, no move."""
    return start_verdict(ctx) == "refused"


def start_moves_nothing(ctx: Context) -> bool:
    """`start_verdict` is `ignored` or `arm`: the status does not move."""
    return start_verdict(ctx) in ("ignored", "arm")


def start_arms(ctx: Context) -> bool:
    """`start_verdict` is `arm`: the tick latches (SEM-32, DL-54)."""
    return start_verdict(ctx) == "arm"


def start_bounded(ctx: Context) -> bool:
    """`start_verdict` is `bounded`: the job has `MAX_NESTED_STARTS` starts in
    progress (DL-304), so a START_REFUSED line, whoever asked, and no move."""
    return start_verdict(ctx) == "bounded"


def start_settles_first(ctx: Context) -> bool:
    """`start_verdict` is `settle_first`: past the bound, a run of the job
    ended and its settlement still waits behind the box rules (DL-340). The
    tier settles it, and the same attempt is made again."""
    return start_verdict(ctx) == "settle_first"


def start_bypasses(ctx: Context) -> bool:
    """`start_verdict` is `bypass`: ON_NOEXEC on the job or a box above it (SEM-22)."""
    return start_verdict(ctx) == "bypass"


def start_admits_job(ctx: Context) -> bool:
    """`start_verdict` is `admit`, and the data says the job is not a box."""
    return start_verdict(ctx) == "admit" and not _data(ctx).box


def start_admits_box(ctx: Context) -> bool:
    """`start_verdict` is `admit`, and the data says the job is a box."""
    return start_verdict(ctx) == "admit" and _data(ctx).box


def start_queues(ctx: Context) -> bool:
    """`start_verdict` is `queue`: not admissible (DL-50)."""
    return start_verdict(ctx) == "queue"


def start_on_held(ctx: Context) -> bool:
    """`start_verdict` is `on_held`: forced, FAILED, and `held` (DL-256)."""
    return start_verdict(ctx) == "on_held"


def start_skips(ctx: Context) -> bool:
    """`start_verdict` is `skip`: a job in no box, outside its window (SEM-33)."""
    return start_verdict(ctx) == "skip"


def start_skips_member(ctx: Context) -> bool:
    """`start_verdict` is `skip_member`: a member of a RUNNING box (DL-154)."""
    return start_verdict(ctx) == "skip_member"


# ---- the queue scan (DL-50, DL-247, DL-257)

type ReadmitVerdict = Literal["admit", "cancel", "leave", "keep", "none"]


def readmit_verdict(ctx: Context) -> ReadmitVerdict:
    """What one admission attempt does to a waiter, in `_readmit`'s order.

    Reads the payload (`Readmission`) and the configuration: QUE_WAIT and
    hold. `none` for a job that is not queued, which no scan addresses. A
    waiter is never iced, so `_readmit`'s unreachable ice branch has no
    counterpart here.
    """
    scan: Readmission = ctx.event.payload
    active = ctx.active
    if "QUE_WAIT" not in active:
        return "none"
    if scan.box == "idle":
        return "keep" if scan.keep_stopped else "cancel"
    if "hold_on" in active or not scan.admissible:
        return "keep"
    return "leave" if scan.recheck_fails else "admit"


def readmit_admits(ctx: Context) -> bool:
    """`readmit_verdict` is `admit`."""
    return readmit_verdict(ctx) == "admit"


def readmit_cancels(ctx: Context) -> bool:
    """`readmit_verdict` is `cancel`: the member's box stopped running."""
    return readmit_verdict(ctx) == "cancel"


def readmit_leaves(ctx: Context) -> bool:
    """`readmit_verdict` is `leave`: the queued recheck failed (DL-257)."""
    return readmit_verdict(ctx) == "leave"


def readmit_keeps(ctx: Context) -> bool:
    """`readmit_verdict` is `keep`: held, not admissible, or kept by an admit-only scan."""
    return readmit_verdict(ctx) == "keep"


# ---- STATUS (SEM-09, DL-13, DL-242, DL-264)

_INJECTABLE: Final = frozenset(
    {"INACTIVE", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED"}
)


def exit_success(ctx: Context) -> bool:
    """The payload has no status, and its exit code is a success."""
    injection: Injection = ctx.event.payload
    return injection.status is None and injection.success


def exit_failure(ctx: Context) -> bool:
    """The payload has no status, and its exit code is not a success."""
    injection: Injection = ctx.event.payload
    return injection.status is None and not injection.success


def injects(ctx: Context) -> bool:
    """The payload's status is injectable and not INACTIVE, or the data says
    the job has no catalog entry."""
    status = _injected(ctx)
    return status in _INJECTABLE and (status != "INACTIVE" or not _data(ctx).catalog)


def injects_inactive_job(ctx: Context) -> bool:
    """The payload's status is INACTIVE; the data: a catalog job, not a box."""
    return _injected(ctx) == "INACTIVE" and _data(ctx).catalog and not _data(ctx).box


def injects_inactive_box(ctx: Context) -> bool:
    """The payload's status is INACTIVE; the data: a box."""
    return _injected(ctx) == "INACTIVE" and _data(ctx).box


def injects_live(ctx: Context) -> bool:
    """The payload's status is STARTING or RUNNING: units the job holds are the run's."""
    return _injected(ctx) in LIVE


def injected_inactive(ctx: Context) -> bool:
    """The payload's status is INACTIVE."""
    return _injected(ctx) == "INACTIVE"


def injected_starting(ctx: Context) -> bool:
    """The payload's status is STARTING."""
    return _injected(ctx) == "STARTING"


def injected_running(ctx: Context) -> bool:
    """The payload's status is RUNNING."""
    return _injected(ctx) == "RUNNING"


def injected_success(ctx: Context) -> bool:
    """The payload's status is SUCCESS."""
    return _injected(ctx) == "SUCCESS"


def injected_failure(ctx: Context) -> bool:
    """The payload's status is FAILURE."""
    return _injected(ctx) == "FAILURE"


# ---- the operator's flags (SEM-20 to SEM-23, DL-243, DL-254)


def not_live(ctx: Context) -> bool:
    """The configuration's status is not STARTING or RUNNING."""
    return "LIVE" not in ctx.active


def force_clears(ctx: Context) -> bool:
    """The data: a catalog job; the configuration's status is not live or
    queued (`_attempt_start` refuses those first)."""
    return _data(ctx).catalog and not (ctx.active & _LIVE_OR_QUEUED)


def queued(ctx: Context) -> bool:
    """The configuration's status is QUE_WAIT."""
    return "QUE_WAIT" in ctx.active


def not_queued(ctx: Context) -> bool:
    """The configuration's status is not QUE_WAIT."""
    return "QUE_WAIT" not in ctx.active


def _noexec(ctx: Context) -> Noexec:
    """ON_NOEXEC's payload, on the input or passed on by `flag moved`."""
    payload = ctx.event.payload
    return payload.noexec if isinstance(payload, FlagMoved) else payload


def noexec_ignored(ctx: Context) -> bool:
    """`_oob_ignored` for ON_NOEXEC: the configuration has ice on, RUNNING, or
    STARTING on a job that is not a box; or the data says a box and the
    payload's `tree_busy` holds. The flag move does not change what it reads."""
    active = ctx.active
    if "ice_on" in active or "RUNNING" in active:
        return True
    if not _data(ctx).box:
        return "STARTING" in active
    return _noexec(ctx).tree_busy


def noexec_applies(ctx: Context) -> bool:
    """Not `noexec_ignored`."""
    return not noexec_ignored(ctx)


def _cause(ctx: Context) -> str:
    moved: FlagMoved = ctx.event.payload
    return moved.cause


def iced_waiter(ctx: Context) -> bool:
    """The payload: ON_ICE moved the flag (the configuration has it on)."""
    return _cause(ctx) == "ON_ICE"


def killed_waiter(ctx: Context) -> bool:
    """The payload: KILLJOB disarmed a waiter."""
    return _cause(ctx) == "KILLJOB"


def left_waiter(ctx: Context) -> bool:
    """The payload: the queued recheck failed, and the arm is gone (DL-257)."""
    return _cause(ctx) == "readmit"


def noexec_settles_job(ctx: Context) -> bool:
    """The payload: ON_NOEXEC; not `noexec_ignored`; the data: a catalog job,
    not a box (`_noexec_job`)."""
    data = _data(ctx)
    return _cause(ctx) == "ON_NOEXEC" and noexec_applies(ctx) and data.catalog and not data.box


def noexec_settles_box(ctx: Context) -> bool:
    """The payload: ON_NOEXEC; not `noexec_ignored`; the data: a box; the
    configuration's status is not INACTIVE, or the payload's `change` holds
    (`_noexec_box`)."""
    if _cause(ctx) != "ON_NOEXEC" or not noexec_applies(ctx) or not _data(ctx).box:
        return False
    return "INACTIVE" not in ctx.active or _noexec(ctx).change


def flag_moves_nothing(ctx: Context) -> bool:
    """The payload: ON_ICE, or ON_NOEXEC that is not `noexec_ignored`: no
    status follows the flag here."""
    cause = _cause(ctx)
    return cause == "ON_ICE" or (cause == "ON_NOEXEC" and noexec_applies(ctx))


def flag_move_unposted(ctx: Context) -> bool:
    """Not `flag_moves_nothing`: only a waiter's kill or recheck reports it,
    and an ignored ON_NOEXEC moves no flag."""
    return not flag_moves_nothing(ctx)


# ---- the job type and the box's rules (SEM-11 to SEM-15)


def is_box(ctx: Context) -> bool:
    """The data says the job is a box."""
    return _data(ctx).box


def is_job(ctx: Context) -> bool:
    """The data says the job is not a box."""
    return not _data(ctx).box


def is_terminator(ctx: Context) -> bool:
    """The data: a member with job_terminator (SEM-14)."""
    return _data(ctx).job_terminator


def has_auto_hold(ctx: Context) -> bool:
    """The data: a member with auto_hold."""
    return _data(ctx).auto_hold


def in_catalog(ctx: Context) -> bool:
    """The data: the job has a catalog entry."""
    return _data(ctx).catalog


def left_catalog(ctx: Context) -> bool:
    """The data: the job left the catalog (DL-256)."""
    return not _data(ctx).catalog


def current_run(ctx: Context) -> bool:
    """The payload: the deadline's run is the row's run."""
    deadline: Deadline = ctx.event.payload
    return deadline.current


def _box_verdict(ctx: Context, target: str | None, override: bool) -> bool:
    verdict: BoxVerdict = ctx.event.payload
    return _data(ctx).box and verdict.target == target and verdict.override == override


def overrides_success(ctx: Context) -> bool:
    """The data: a box; the payload: box_success decides SUCCESS (SEM-15)."""
    return _box_verdict(ctx, "SUCCESS", True)


def overrides_failure(ctx: Context) -> bool:
    """The data: a box; the payload: box_failure decides FAILURE (SEM-15)."""
    return _box_verdict(ctx, "FAILURE", True)


def _door(ctx: Context, target: str | None) -> bool:
    verdict: Verdict = ctx.event.payload
    return _data(ctx).box and verdict.target == target


def verdict_success(ctx: Context) -> bool:
    """The data: a box; the payload's step decides SUCCESS: box_success holds
    (SEM-12), or the default fold with no box_success (SEM-11)."""
    return _door(ctx, "SUCCESS")


def verdict_failure(ctx: Context) -> bool:
    """The data: a box; the payload's step decides FAILURE: box_failure holds
    (SEM-12), or the default fold over a failed vote with no box_failure."""
    return _door(ctx, "FAILURE")


def decides_nothing(ctx: Context) -> bool:
    """The data: a box; the payload's step decides nothing."""
    return _door(ctx, None)


def derives_success(ctx: Context) -> bool:
    """The data: a box; the payload: no member failed, and no box_success
    suppresses the default SUCCESS (SEM-15)."""
    return _box_verdict(ctx, "SUCCESS", False)


def derives_failure(ctx: Context) -> bool:
    """The data: a box; the payload: a member failed, and no box_failure
    suppresses the default FAILURE (SEM-15)."""
    return _box_verdict(ctx, "FAILURE", False)


def terminator_fires(ctx: Context) -> bool:
    """The data: a box; the payload's `fires` (SEM-14)."""
    terminator: Terminator = ctx.event.payload
    return _data(ctx).box and terminator.fires


# ---- the units (DL-50, DL-120, DL-256)


def has_demand(ctx: Context) -> bool:
    """The payload's demand vector reserves units."""
    admission: Admission = ctx.event.payload
    return admission.demand


def no_demand(ctx: Context) -> bool:
    """The payload's demand vector reserves nothing: reserving nothing is no move."""
    admission: Admission = ctx.event.payload
    return not admission.demand


def takes_over(ctx: Context) -> bool:
    """The payload: an admitted start that is not on held units."""
    admission: Admission = ctx.event.payload
    return not admission.on_held


def on_held_units(ctx: Context) -> bool:
    """The payload: a FORCE_STARTJOB starts on the held units (DL-256)."""
    admission: Admission = ctx.event.payload
    return admission.on_held


def _release(ctx: Context) -> Release:
    """The settlement's release, read from the units the row holds when the
    settlement pops (DL-349)."""
    settlement: Settlement = ctx.event.payload
    return settlement.release


def frees_all(ctx: Context) -> bool:
    """The payload: the policy of the run's end frees every unit."""
    return _release(ctx) == "free"


def keeps_held(ctx: Context) -> bool:
    """The payload: the policy of the run's end keeps a renewable's unit."""
    return _release(ctx) == "keep"


def spends(ctx: Context) -> bool:
    """The payload: the policy of the run's end spends a unit, and keeps none."""
    return _release(ctx) == "spend"


GUARDS: Final = {
    guard.__name__: guard
    for guard in (
        start_refused,
        start_moves_nothing,
        start_arms,
        start_bounded,
        start_settles_first,
        start_bypasses,
        start_admits_job,
        start_admits_box,
        start_queues,
        start_on_held,
        start_skips,
        start_skips_member,
        readmit_admits,
        readmit_cancels,
        readmit_leaves,
        readmit_keeps,
        exit_success,
        exit_failure,
        injects,
        injects_inactive_job,
        injects_inactive_box,
        injects_live,
        injected_inactive,
        injected_starting,
        injected_running,
        injected_success,
        injected_failure,
        not_live,
        force_clears,
        queued,
        not_queued,
        noexec_ignored,
        noexec_applies,
        iced_waiter,
        killed_waiter,
        left_waiter,
        noexec_settles_job,
        noexec_settles_box,
        flag_moves_nothing,
        flag_move_unposted,
        is_box,
        is_job,
        is_terminator,
        has_auto_hold,
        in_catalog,
        left_catalog,
        current_run,
        verdict_success,
        verdict_failure,
        decides_nothing,
        overrides_success,
        overrides_failure,
        derives_success,
        derives_failure,
        terminator_fires,
        has_demand,
        no_demand,
        takes_over,
        on_held_units,
        frees_all,
        keeps_held,
        spends,
    )
}

# ---- actions: the chained events of one input (statechart-engine §8)


def relay_start(ctx: Context) -> Effects:
    """STARTJOB, FORCE_STARTJOB or a fired run_window timer: the attempt
    itself, as `_dispatch` makes it: scheduled, its refusal traced, forced
    for FORCE_STARTJOB. The payload's facts pass on."""
    attempt: Attempt = ctx.event.payload
    return Effects(
        send=((ctx.key, Event(START, attempt.relayed(force=ctx.event.name == FORCE_STARTJOB))),)
    )


def _admitted(ctx: Context, admitted: Admitted) -> Effects:
    return Effects(send=((ctx.key, Event(START_ADMITTED, admitted)),))


def extent_bypass(ctx: Context) -> Effects:
    """A bypass (job.101) sends `start admitted` first, and the rest of the
    start follows from it (DL-349)."""
    return _admitted(ctx, Admitted("bypass"))


def extent_queue(ctx: Context) -> Effects:
    """A queued start (job.103) sends `start admitted`, which does nothing
    more: the waiter's units wait for its readmission."""
    return _admitted(ctx, Admitted("queue"))


def extent_admit(ctx: Context) -> Effects:
    """An admission (job.102, job.124) sends `start admitted` first, with the
    units it reserves (`demand`) and the payload's handle on the job's units."""
    attempt: Attempt = ctx.event.payload
    admission = Admission(demand=attempt.demand, units=_units(ctx))
    return _admitted(ctx, Admitted("admit", admission))


def extent_on_held(ctx: Context) -> Effects:
    """A forced start on held units (job.104) sends `start admitted` first,
    with the held units it takes over."""
    return _admitted(ctx, Admitted("on_held", Admission(on_held=True, units=_units(ctx))))


def start_extent(ctx: Context) -> Effects | None:
    """`start admitted`: what the admitted start does, in the order
    `_start_admitted` writes it. A bypass consumes the arm; an admission or a
    start on held units reserves or takes over its units, then consumes the
    arm; a queue does nothing more."""
    admitted: Admitted = ctx.event.payload
    if admitted.verdict == "queue":
        return None
    actual = (ctx.key, Event(ACTUAL_START))
    if admitted.verdict == "bypass":
        return Effects(send=(actual,))
    return Effects(send=((ctx.key, Event(ADMITTED_START, admitted.admission)), actual))


def admit(ctx: Context) -> Effects:
    """A readmitted waiter reserves its units, then consumes the arm, in the
    order `_readmit` writes them."""
    admission = Admission(demand=ctx.event.payload.demand)
    return Effects(
        send=((ctx.key, Event(ADMITTED_START, admission)), (ctx.key, Event(ACTUAL_START)))
    )


def settle_ended_run(ctx: Context) -> Effects:
    """A start meets the units of a run whose settlement is still owed
    (`_settle_ended_run`): that run settles first, by the end it made, then
    the start is admitted again against what the settlement left. The
    settlement's policy is read from the admission's handle (DL-349)."""
    admission: Admission = ctx.event.payload
    previous = admission.previous
    if admission.units is not None:
        previous = Settlement(previous.success, units=admission.units)
    return Effects(
        send=(
            (ctx.key, Event(LEAVE_LIVE, previous)),
            (ctx.key, Event(ADMITTED_START, replace(admission, previous=Settlement()))),
        )
    )


def settle_after(ctx: Context) -> Effects:
    """A move on its own (`_after_transition`) that may end a run in a status
    other than SUCCESS: the settlement is the continuation `settle_late`, after
    the box rules this row reports; the wakes follow the settlement."""
    return Effects(then="settle_late")


def settle_after_success(ctx: Context) -> Effects:
    """As `settle_after`, for an end in SUCCESS: the continuation `settle_late_success`."""
    return Effects(then="settle_late_success")


def settle(ctx: Context) -> Effects:
    """The run's units settle (`_settle_row`) for an end other than SUCCESS:
    `leave STARTING or RUNNING`, built with the handle the item's payload
    carries (DL-349). A row of the batch INACTIVE path
    (`_set_inactive_batch`) reports it first."""
    return Effects(send=((ctx.key, Event(LEAVE_LIVE, Settlement(units=_units(ctx)))),))


def settle_late(ctx: Context) -> Effects | None:
    """The settlement `settle_after` asked for, an end other than SUCCESS. A
    live configuration means the member has started again since the end: the
    units are the new run's, and nothing settles (the run guard of
    `_settle_row`)."""
    if "LIVE" in ctx.active:
        return None
    return settle(ctx)


def settle_late_success(ctx: Context) -> Effects | None:
    """The settlement `settle_after_success` asked for, an end in SUCCESS,
    unless the member is live again (`settle_late`)."""
    if "LIVE" in ctx.active:
        return None
    settlement = Settlement(True, units=_units(ctx))
    return Effects(send=((ctx.key, Event(LEAVE_LIVE, settlement)),))


def report_flag_move(ctx: Context) -> Effects:
    """A flag moved, and the status rule that reads it follows as its own
    item, with the payload's handle on the units."""
    name = ctx.event.name
    if name == ON_NOEXEC:
        moved = FlagMoved("ON_NOEXEC", ctx.event.payload, units=_units(ctx))
    else:
        cause = "readmit" if name == READMIT else name
        moved = FlagMoved(cause, units=_units(ctx))  # type: ignore[arg-type]
    return Effects(send=((ctx.key, Event(FLAG_MOVED, moved)),))


ACTIONS: Final = {
    action.__name__: action
    for action in (
        relay_start,
        extent_bypass,
        extent_queue,
        extent_admit,
        extent_on_held,
        start_extent,
        admit,
        settle_ended_run,
        settle_after,
        settle_after_success,
        settle,
        settle_late,
        settle_late_success,
        report_flag_move,
    )
}

JOB: Final = Statechart(
    name="job",
    root=ROOT,
    rows=ROWS,
    choices=CHOICES,
    guards=GUARDS,
    actions=ACTIONS,
    clock="logical",
)
