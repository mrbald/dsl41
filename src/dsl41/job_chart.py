"""The composite `job` statechart: one job's status, flags and holding in one
declaration (docs/statechart-engine.md §11, DL-337).

The chart is declared and checked, and nothing drives it yet. The oracle still
moves a job through the three tables in `oracle_state` (`job_status`,
`job_flags`, `job_holding`), and tests/test_job_chart.py checks this chart
against them cell by cell. The chart joins `machines.MACHINES` when the second
oracle drives it.

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
others take new numbers in the same hundred. Internal rows stay internal.

Guards read three things: the instance's configuration, its data (`JobData`),
and the event's payload. The payload carries the facts the rule code reads
outside the job's own row: its box's run, condition truth, the run window,
admission, the members' verdicts. Each payload class names the oracle
predicate behind each field.

One input can move several regions of one job. Today's order of those moves
is the contract, and the region order is not load-bearing. Two forms declare
it:

- a chained item that a row's action reports to the same instance
  (statechart-engine §8). It is the form wherever a later step reads an
  earlier step's write: the chained item reads the configuration at its own
  time. A flag move reports `flag moved`, and the status rule that reads the
  flag follows it: ON_ICE dequeues a waiter, KILLJOB terminates one, a waiter
  that fails the queued recheck leaves the queue, ON_NOEXEC settles the
  status. An actual start consumes the arm (`actual start`); an admitted start
  reserves or takes over units first (`admitted start`). STARTJOB,
  FORCE_STARTJOB and a window-postponed start relay the attempt to `start`,
  after the forced clears.
- rows of a sibling region on the same event, only where every step reads
  the configuration before the input: a forced start clears ice and hold, a
  scheduled tick arms, an injected live status makes held units the run's.

The settlement of a run's units keeps today's two orders. A move on its own
(`Oracle._after_transition`) runs the box rules, then the settlement, then the
wakes: a status move that may end a run requests the continuation `settle`
(or `settle_success` for an end in SUCCESS), which runs after every item the
row reports and reports `leave STARTING or RUNNING`; the wakes follow it. A
move of the batch INACTIVE path (`Oracle._set_inactive_batch`) settles first:
its row reports `leave STARTING or RUNNING` ahead of anything else. As today,
the settlement carries the end the move made, not the status it finds: a box
restarted by its own completion can start the member again before the
member's late settlement, which then frees the new run's units (job.312, a
pre-existing oracle bug that a follow-up fix removes).

Not declared here, and left to the entry that drives the chart: the cascade
between instances (a box's rules on its members, a member's moves on its box,
wakes), the per-job start bound (it attaches to the `start` rows), and the
start retry that OFF_HOLD and ON_NOEXEC make. No state defers (§12).

The walks that check this chart carry no run window and no true condition;
the facts of those paths are checked by synthesized cells and one windowed
replay, and the old-vs-new harness is their proof. `JobData`'s release fields
are the current run's: the tier refreshes them when a start takes units.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, Literal

from dsl41.oracle_state import LIVE, JobRuntime, JobStatus
from dsl41.state_machine import (
    Branch,
    Choice,
    Context,
    Effects,
    Event,
    Region,
    Row,
    State,
    Statechart,
)

# ---- events

#: The oracle's inputs that move a job row.
STATUS: Final = "STATUS"
STARTJOB: Final = "STARTJOB"
FORCE_STARTJOB: Final = "FORCE_STARTJOB"
KILLJOB: Final = "KILLJOB"
ON_ICE: Final = "ON_ICE"
OFF_ICE: Final = "OFF_ICE"
ON_HOLD: Final = "ON_HOLD"
OFF_HOLD: Final = "OFF_HOLD"
ON_NOEXEC: Final = "ON_NOEXEC"
OFF_NOEXEC: Final = "OFF_NOEXEC"
DISARM: Final = "DISARM"
RELEASE_RESOURCE: Final = "RELEASE_RESOURCE"
TERM_RUN_TIME: Final = "TIMER term_run_time"
#: A window-postponed start fires (SEM-33), or a scan for the next eligible
#: day resumes (DL-257).
RUN_WINDOW: Final = "TIMER run_window"

#: The moments the rules name inside one input.
START: Final = "start"
STARTED: Final = "started"
READMIT: Final = "readmit"
ACTUAL_START: Final = "actual start"
ADMITTED_START: Final = "admitted start"
LEAVE_LIVE: Final = "leave STARTING or RUNNING"
#: A flag move whose status rule reads the flag: it follows the move.
FLAG_MOVED: Final = "flag moved"
MEMBER_MOVES: Final = "member moves"
COMPLETION_MOMENT: Final = "completion moment"
MEMBER_ENDS: Final = "member ends FAILURE or TERMINATED"
BOX_RECOMPUTE: Final = "box recompute"
BOX_START: Final = "box start"
BOX_RUNNING: Final = "box running"
BOX_SET_INACTIVE: Final = "box set INACTIVE"
BOX_RUN_ENDS: Final = "box run ends"
BOX_ENDS: Final = "box ends FAILURE or TERMINATED"
BOX_OFF_NOEXEC: Final = "OFF_NOEXEC on its box"
PERIOD_OPENING: Final = "first input of a period"

# ---- data and payloads

#: What leaving a run does with its units (DL-50, DL-256, SEM-16).
type Release = Literal["free", "keep", "spend"]
#: A member's view of its box's run, for a start attempt; `none` for a job in no box.
type BoxGate = Literal["none", "running", "idle", "ran", "iced_out"]
#: `Oracle._window_side`: the SEM-33 closer-edge rule at this instant.
type WindowSide = Literal["inside", "defer", "skip"]
type HoldingState = Literal["none", "reserved", "held"]


@dataclass(frozen=True)
class JobData:
    """What a job instance's guards read besides its configuration and the event.

    `box`: the job type is BOX. `catalog`: the job has a catalog entry; a row
    can outlive its entry, and STATUS can create one (DL-264).
    `job_terminator` and `auto_hold`: the member's attributes the box's rules
    test (SEM-14, `_cascade_job_terminators`; `_on_box_started`). The release
    fields say what leaving the run does with the units the run holds, after a
    SUCCESS and after any other end (`RuntimeState.release_reservations` with
    the pool's `keeps_held`). They are the current run's: a start that takes
    units, or held units an injected live status makes the run's, changes them.
    """

    box: bool = False
    catalog: bool = True
    job_terminator: bool = False
    auto_hold: bool = False
    release_on_success: Release = "free"
    release_otherwise: Release = "free"


@dataclass(frozen=True)
class Attempt:
    """One start attempt: `Oracle._attempt_start`, `_start` and `_start_admitted`.

    `force`, `explicit` and `scheduled` say who asks: a relay from STARTJOB,
    FORCE_STARTJOB or a window-postponed start sets all three as `_dispatch`
    passes them; an attempt a wake or a retry makes leaves them false, and its
    refusals go untraced (DL-81). `stale`: a postponed start no longer belongs
    to its box run (`_deferral_is_stale`), or a scan continuation is
    superseded (`_rescan_superseded`). `rescan`: the timer resumes a scan and
    attempts nothing. `dated`: the job has date conditions (`schedule`).
    `box`: the member's gate on its box's run (SEM-10, SEM-20). `condition`:
    the job's condition holds, or it has none. `window`: `_window_side`.
    `box_noexec`: a box above the job is ON_NOEXEC (`_noexec_bypasses`).
    `admissible`: `_admissible`. `demand`: the demand vector is not empty.
    """

    force: bool = False
    explicit: bool = False
    scheduled: bool = False
    stale: bool = False
    rescan: bool = False
    dated: bool = False
    box: BoxGate = "none"
    condition: bool = True
    window: WindowSide = "inside"
    box_noexec: bool = False
    admissible: bool = True
    demand: bool = True


@dataclass(frozen=True)
class Readmission:
    """One queue scan's attempt for a waiter: `Oracle._readmit`.

    `box`: the member's box is RUNNING or not; `none` for a job in no box.
    `keep_stopped`: an admit-only scan keeps a member of a stopped box queued
    (DL-247, DL-255). `admissible`: `_admissible`. `recheck_fails`:
    `_queued_recheck` names a failed check (DL-257). `demand` as for `Attempt`.
    """

    box: Literal["none", "running", "idle"] = "none"
    keep_stopped: bool = False
    admissible: bool = True
    recheck_fails: bool = False
    demand: bool = True


@dataclass(frozen=True)
class Injection:
    """An injected STATUS: a status, or with none an exit code, read by
    `Semantics.exit_is_success` as `success` (SEM-09)."""

    status: str | None = None
    success: bool = False


@dataclass(frozen=True)
class Noexec:
    """ON_NOEXEC on a box (DL-254): `tree_busy`, a job it contains is on ice,
    live or queued (`_oob_ignored`); `change`, its parent is RUNNING or a job
    it contains is not INACTIVE. `_noexec_box` also counts the box's own
    status, which the configuration decides."""

    tree_busy: bool = False
    change: bool = True


@dataclass(frozen=True)
class Deadline:
    """A term_run_time timer: `current`, its run is the row's run."""

    current: bool = True


@dataclass(frozen=True)
class Verdict:
    """One step of a RUNNING box's completion door: `target`, the status it
    decides, or none. `member moves` reads the overrides
    (`_apply_box_overrides`), `completion moment` the default fold
    (`_all_members_done`, `_fold_box_default`)."""

    target: Literal["SUCCESS", "FAILURE"] | None = None


@dataclass(frozen=True)
class BoxVerdict:
    """An idle box's recompute (`_idle_box_recompute`, SEM-15): `target`, the
    status it decides, or none; `override`, a box_success or box_failure
    condition decided it rather than the default verdict."""

    target: Literal["SUCCESS", "FAILURE"] | None = None
    override: bool = False


@dataclass(frozen=True)
class FlagMoved:
    """The flag move a status rule follows: `cause` is the input that moved it,
    and `noexec` ON_NOEXEC's payload, passed on."""

    cause: Literal["ON_ICE", "KILLJOB", "readmit", "ON_NOEXEC"]
    noexec: Noexec = Noexec()


@dataclass(frozen=True)
class Terminator:
    """A member's end at its box: `fires`, `_box_terminator_fires` (the
    member's box_terminator, its end, `box-terminator-on-terminated`)."""

    fires: bool = True


@dataclass(frozen=True)
class Settlement:
    """The end of a run whose units settle: `success`, the move ended in
    SUCCESS, which frees a `success` policy's units (`release_reservations`)."""

    success: bool = False


@dataclass(frozen=True)
class Admission:
    """The units of an admitted start (`_acquire`, `_start_on_held`):
    `on_held`, a FORCE_STARTJOB starts on held units (DL-256); `demand` as
    for `Attempt`."""

    on_held: bool = False
    demand: bool = True


# ---- configuration

#: Each status's ancestors inside the `status` region, outermost first.
_STATUS_PATH: Final[dict[str, tuple[str, ...]]] = {
    "INACTIVE": ("lifecycle", "IDLE"),
    "SUCCESS": ("lifecycle", "IDLE", "TERMINAL"),
    "FAILURE": ("lifecycle", "IDLE", "TERMINAL", "FAILED"),
    "TERMINATED": ("lifecycle", "IDLE", "TERMINAL", "FAILED"),
    "QUE_WAIT": ("lifecycle",),
    "STARTING": ("lifecycle", "LIVE"),
    "RUNNING": ("lifecycle", "LIVE"),
}
_FLAGS: Final = ("ice", "hold", "noexec", "arm")


def configuration(
    status: JobStatus,
    *,
    ice: bool = False,
    hold: bool = False,
    noexec: bool = False,
    armed: bool = False,
    holding: HoldingState = "none",
) -> frozenset[str]:
    """The active states of an instance with this status, these flags and this holding."""
    flags = {
        f"{name}_{'on' if on else 'off'}" for name, on in zip(_FLAGS, (ice, hold, noexec, armed))
    }
    return frozenset({"job", *_STATUS_PATH[status], status, "flags", *flags, holding})


def row_holding(row: JobRuntime) -> HoldingState:
    """A row's holding state: its units are a run's while it is live, and held after."""
    if not row.reservations:
        return "none"
    return "reserved" if row.status in LIVE else "held"


def row_configuration(row: JobRuntime) -> frozenset[str]:
    """The configuration a job row projects from."""
    return configuration(
        row.status,
        ice=row.on_ice,
        hold=row.on_hold,
        noexec=row.on_noexec,
        armed=row.armed,
        holding=row_holding(row),
    )


def projection(active: frozenset[str]) -> tuple[str, bool, bool, bool, bool, str]:
    """The row fields a configuration stands for: status, the four flags, holding."""
    status = next(s for s in _STATUS_PATH if s in active)
    holding = next(h for h in ("none", "reserved", "held") if h in active)
    ice, hold, noexec, armed = (f"{name}_on" in active for name in _FLAGS)
    return status, ice, hold, noexec, armed, holding


def _data(ctx: Context) -> JobData:
    data: JobData = ctx.data
    return data


def _injected(ctx: Context) -> str | None:
    injection: Injection = ctx.event.payload
    return injection.status


# ---- the start attempt (SEM-10, SEM-20 to SEM-23, SEM-32, SEM-33, DL-50)

type StartVerdict = Literal[
    "refused", "ignored", "arm", "bypass", "on_held", "queue", "admit", "skip", "skip_member"
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
    """The payload's demand vector is not empty."""
    admission: Admission = ctx.event.payload
    return admission.demand


def no_demand(ctx: Context) -> bool:
    """The payload's demand vector is empty: reserving nothing is no move."""
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
    """The data's release for the end the payload names."""
    settlement: Settlement = ctx.event.payload
    data = _data(ctx)
    return data.release_on_success if settlement.success else data.release_otherwise


def frees_all(ctx: Context) -> bool:
    """The payload's end and the data: the policy frees every unit."""
    return _release(ctx) == "free"


def keeps_held(ctx: Context) -> bool:
    """The payload's end and the data: a renewable's unit is kept."""
    return _release(ctx) == "keep"


def spends(ctx: Context) -> bool:
    """The payload's end and the data: a unit is spent, none kept."""
    return _release(ctx) == "spend"


GUARDS: Final = {
    guard.__name__: guard
    for guard in (
        start_refused,
        start_moves_nothing,
        start_arms,
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
    attempt = replace(
        ctx.event.payload,
        force=ctx.event.name == FORCE_STARTJOB,
        explicit=True,
        scheduled=True,
    )
    return Effects(send=((ctx.key, Event(START, attempt)),))


def consume_arm(ctx: Context) -> Effects:
    """An actual start that takes no units (a bypass) consumes the arm."""
    return Effects(send=((ctx.key, Event(ACTUAL_START)),))


def admit(ctx: Context) -> Effects:
    """An admitted start reserves its units, then consumes the arm, in the
    order `_start_admitted` and `_readmit` write them."""
    admission = Admission(demand=ctx.event.payload.demand)
    return Effects(
        send=((ctx.key, Event(ADMITTED_START, admission)), (ctx.key, Event(ACTUAL_START)))
    )


def admit_on_held(ctx: Context) -> Effects:
    """A forced start on held units takes them over, then consumes the arm."""
    admission = Admission(on_held=True)
    return Effects(
        send=((ctx.key, Event(ADMITTED_START, admission)), (ctx.key, Event(ACTUAL_START)))
    )


def settle_after(ctx: Context) -> Effects:
    """A move on its own (`_after_transition`) that may end a run in a status
    other than SUCCESS: the settlement is the continuation `settle`, after the
    box rules this row reports; the wakes follow the settlement."""
    return Effects(then="settle")


def settle_after_success(ctx: Context) -> Effects:
    """As `settle_after`, for an end in SUCCESS: the continuation `settle_success`."""
    return Effects(then="settle_success")


def settle(ctx: Context) -> Effects:
    """The run's units settle (`_settle_row`) for an end other than SUCCESS:
    `leave STARTING or RUNNING`. A row of the batch INACTIVE path
    (`_set_inactive_batch`) reports it first."""
    return Effects(send=((ctx.key, Event(LEAVE_LIVE, Settlement())),))


def settle_success(ctx: Context) -> Effects:
    """The run's units settle for an end in SUCCESS."""
    return Effects(send=((ctx.key, Event(LEAVE_LIVE, Settlement(success=True))),))


def report_flag_move(ctx: Context) -> Effects:
    """A flag moved, and the status rule that reads it follows as its own item."""
    name = ctx.event.name
    if name == ON_NOEXEC:
        moved = FlagMoved("ON_NOEXEC", ctx.event.payload)
    else:
        moved = FlagMoved("readmit" if name == READMIT else name)  # type: ignore[arg-type]
    return Effects(send=((ctx.key, Event(FLAG_MOVED, moved)),))


ACTIONS: Final = {
    action.__name__: action
    for action in (
        relay_start,
        consume_arm,
        admit,
        admit_on_held,
        settle_after,
        settle_after_success,
        settle,
        settle_success,
        report_flag_move,
    )
}

# ---- the chart

_SEM10 = "SEM-10"
_DL254 = "DL-254"
_DL256 = "DL-256"
_SEM15 = "SEM-15"
_SS5 = "autosys-semantics ss5"
_DL264 = "DL-13, DL-264"
_DL242 = "DL-242, DL-235"


def _never(ident: str, source: str, event: str, why: str, guard: str = "") -> Row:
    """A cell the rules never post: an ignored row marked unreachable, with the reason."""
    return Row(ident, source, event, guard=guard, kind="ignored", mark="unreachable", note=why)


def _flag(name: str) -> Region:
    return Region(name, f"{name}_off", (State(f"{name}_off"), State(f"{name}_on")))


_FAILED: Final = State(
    "FAILED", regions=(Region("failed", "FAILURE", (State("FAILURE"), State("TERMINATED"))),)
)
_TERMINAL: Final = State(
    "TERMINAL", regions=(Region("terminal", "SUCCESS", (State("SUCCESS"), _FAILED)),)
)
_IDLE: Final = State("IDLE", regions=(Region("idle", "INACTIVE", (State("INACTIVE"), _TERMINAL)),))
_LIVE: Final = State(
    "LIVE",
    regions=(Region("live", "STARTING", (State("STARTING"), State("RUNNING"))),),
)
#: Every status. Its rows are the ones whose old source was every status.
_LIFECYCLE: Final = State(
    "lifecycle", regions=(Region("status", "IDLE", (_IDLE, State("QUE_WAIT"), _LIVE)),)
)
_ROOT: Final = State(
    "job",
    regions=(
        Region("status", "lifecycle", (_LIFECYCLE,)),
        Region("flags", "flags", (State("flags", regions=tuple(_flag(f) for f in _FLAGS)),)),
        Region("holding", "none", (State("none"), State("reserved"), State("held"))),
    ),
)

#: The status rows, `job.1NN`. 101 to 158 are `job_status.01` to `.58`; 159
#: and above are the rows a split source or a cell the table lacked adds.
_STATUS_ROWS: Final = (
    Row("job.101", "IDLE", START, "SUCCESS", "start_bypasses", "consume_arm", cite="SEM-22, DL-54"),
    Row("job.102", "IDLE", START, "STARTING", "start_admits_job", "admit", cite="SEM-10, DL-120"),
    Row("job.103", "IDLE", START, "QUE_WAIT", "start_queues", cite="DL-50, DL-247, DL-255"),
    Row("job.104", "FAILED", START, "STARTING", "start_on_held", "admit_on_held", cite=_DL256),
    Row("job.105", "STARTING", STARTED, "RUNNING", "is_job", cite=_SEM10),
    Row("job.106", "TERMINAL", START, "INACTIVE", "start_skips", cite="SEM-33, DL-246"),
    Row("job.107", "TERMINAL", START, "INACTIVE", "start_skips_member", cite="SEM-33, DL-154"),
    Row(
        "job.108", "QUE_WAIT", READMIT, "STARTING", "readmit_admits", "admit", cite="DL-50, DL-257"
    ),
    Row("job.109", "QUE_WAIT", READMIT, "INACTIVE", "readmit_cancels", cite="DL-50, DL-247"),
    Row("job.110", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "left_waiter", cite="DL-257"),
    Row("job.111", "QUE_WAIT", FLAG_MOVED, "TERMINATED", "killed_waiter", cite="DL-50, DL-54"),
    Row("job.112", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "iced_waiter", cite="DL-50, DL-285"),
    Row("job.113", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "noexec_settles_job", cite=_DL254),
    Row("job.114", "LIVE", KILLJOB, "TERMINATED", action="settle_after", cite="SEM-14"),
    Row(
        "job.115", "RUNNING", TERM_RUN_TIME, "TERMINATED", "current_run", "settle_after", cite=_SS5
    ),
    Row("job.116", "LIVE", BOX_ENDS, "TERMINATED", "is_terminator", "settle_after", cite="SEM-14"),
    Row(
        "job.117",
        "lifecycle",
        STATUS,
        "SUCCESS",
        "exit_success",
        "settle_after_success",
        cite="SEM-09",
    ),
    Row("job.118", "lifecycle", STATUS, "FAILURE", "exit_failure", "settle_after", cite="SEM-09"),
    Row("job.119", "lifecycle", STATUS, "injected status", "injects", cite=_DL264),
    Row(
        "job.120",
        "lifecycle",
        STATUS,
        "INACTIVE",
        "injects_inactive_job",
        "settle_after",
        cite=_DL242,
    ),
    Row("job.121", "FAILED", FLAG_MOVED, "INACTIVE", "noexec_settles_job", cite="SEM-22, DL-243"),
    Row("job.122", "TERMINAL", BOX_START, "INACTIVE", cite="SEM-10, DL-242", note="reset"),
    Row("job.123", "QUE_WAIT", BOX_SET_INACTIVE, "INACTIVE", cite="SEM-18, DL-242", note="cascade"),
    Row("job.124", "IDLE", START, "STARTING", "start_admits_box", "admit", cite="SEM-10, DL-242"),
    Row("job.125", "STARTING", STARTED, "RUNNING", "is_box", cite="SEM-10, SEM-33, DL-246"),
    Row(
        "job.126",
        "RUNNING",
        MEMBER_MOVES,
        "SUCCESS",
        "verdict_success",
        "settle_after_success",
        cite="SEM-12",
    ),
    Row(
        "job.127",
        "RUNNING",
        MEMBER_MOVES,
        "FAILURE",
        "verdict_failure",
        "settle_after",
        cite="SEM-12, SEM-14",
    ),
    Row(
        "job.128",
        "RUNNING",
        COMPLETION_MOMENT,
        "SUCCESS",
        "verdict_success",
        "settle_after_success",
        cite="SEM-11",
    ),
    Row(
        "job.129",
        "RUNNING",
        COMPLETION_MOMENT,
        "FAILURE",
        "verdict_failure",
        "settle_after",
        cite="SEM-11, SEM-14",
    ),
    Row(
        "job.130",
        "RUNNING",
        MEMBER_ENDS,
        "TERMINATED",
        "terminator_fires",
        "settle_after",
        cite="SEM-14",
    ),
    Row("job.131", "INACTIVE", BOX_RECOMPUTE, "SUCCESS", "overrides_success", cite=_SEM15),
    Row("job.132", "INACTIVE", BOX_RECOMPUTE, "FAILURE", "overrides_failure", cite=_SEM15),
    Row("job.133", "INACTIVE", BOX_RECOMPUTE, "SUCCESS", "derives_success", cite=_SEM15),
    Row("job.134", "INACTIVE", BOX_RECOMPUTE, "FAILURE", "derives_failure", cite=_SEM15),
    Row(
        "job.135", "lifecycle", STATUS, "INACTIVE", "injects_inactive_box", "settle", cite="SEM-18"
    ),
    Row("job.136", "IDLE", FLAG_MOVED, "INACTIVE", "noexec_settles_box", cite=_DL254),
    *(
        Row(
            f"job.{137 + i}", s, KILLJOB, kind="internal", cite="DL-64, DL-81", note="EVENT_IGNORED"
        )
        for i, s in enumerate(("INACTIVE", "SUCCESS", "FAILURE", "TERMINATED"))
    ),
    *(
        Row(
            f"job.{141 + i}", s, START, guard="start_refused", kind="internal", cite="DL-81, DL-246"
        )
        for i, s in enumerate(
            ("INACTIVE", "QUE_WAIT", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
        )
    ),
    Row("job.148", "STARTING", ON_ICE, kind="internal", cite=_DL254, note="EVENT_IGNORED"),
    Row("job.149", "RUNNING", ON_ICE, kind="internal", cite=_DL254, note="EVENT_IGNORED"),
    Row("job.150", "STARTING", ON_HOLD, kind="internal", cite=_DL254, note="EVENT_IGNORED"),
    Row("job.151", "RUNNING", ON_HOLD, kind="internal", cite=_DL254, note="EVENT_IGNORED"),
    *(
        Row(f"job.{152 + i}", s, ON_NOEXEC, guard="noexec_ignored", kind="internal", cite=_DL254)
        for i, s in enumerate(
            ("INACTIVE", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED", "QUE_WAIT")
        )
    ),
    # a split source: job.123's, job.131 to job.134's and job.136's other sources
    Row("job.159", "LIVE", BOX_SET_INACTIVE, "INACTIVE", action="settle", cite="SEM-18, DL-242"),
    Row("job.160", "TERMINAL", BOX_SET_INACTIVE, "INACTIVE", cite="SEM-18", note="as job.123"),
    Row("job.161", "QUE_WAIT", BOX_RECOMPUTE, "SUCCESS", "overrides_success", cite=_SEM15),
    Row("job.162", "FAILURE", BOX_RECOMPUTE, "SUCCESS", "overrides_success", cite=_SEM15),
    Row("job.163", "QUE_WAIT", BOX_RECOMPUTE, "FAILURE", "overrides_failure", cite=_SEM15),
    Row("job.164", "SUCCESS", BOX_RECOMPUTE, "FAILURE", "overrides_failure", cite=_SEM15),
    Row("job.165", "QUE_WAIT", BOX_RECOMPUTE, "SUCCESS", "derives_success", cite=_SEM15),
    Row("job.166", "FAILURE", BOX_RECOMPUTE, "SUCCESS", "derives_success", cite=_SEM15),
    Row("job.167", "QUE_WAIT", BOX_RECOMPUTE, "FAILURE", "derives_failure", cite=_SEM15),
    Row("job.168", "SUCCESS", BOX_RECOMPUTE, "FAILURE", "derives_failure", cite=_SEM15),
    Row("job.169", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "noexec_settles_box", cite=_DL254),
    Row("job.170", "STARTING", FLAG_MOVED, "INACTIVE", "noexec_settles_box", "settle", cite=_DL254),
    # 171 to 176 are job.119's choice branches
    # the inputs that relay a start attempt (statechart-engine §8)
    Row("job.177", "lifecycle", STARTJOB, action="relay_start", kind="internal", cite=_SEM10),
    Row(
        "job.178", "lifecycle", FORCE_STARTJOB, action="relay_start", kind="internal", cite="SEM-23"
    ),
    Row("job.179", "lifecycle", RUN_WINDOW, action="relay_start", kind="internal", cite="SEM-33"),
    # cells the table had no row for: the rules move nothing there
    Row(
        "job.180",
        "lifecycle",
        START,
        guard="start_moves_nothing",
        kind="ignored",
        cite="SEM-20, SEM-21, SEM-32, SEM-33",
        note="A gate holds the attempt silently, a tick arms, or a postponed start waits.",
    ),
    Row(
        "job.181",
        "QUE_WAIT",
        READMIT,
        guard="readmit_keeps",
        kind="ignored",
        cite="DL-50, DL-247",
        note="The waiter stays queued.",
    ),
    _never("job.182", "lifecycle", READMIT, "A queue scan addresses a waiter.", "not_queued"),
    _never(
        "job.183", "lifecycle", STARTED, "The rules post it only while the run is still STARTING."
    ),
    Row(
        "job.184",
        "lifecycle",
        TERM_RUN_TIME,
        kind="ignored",
        cite="SEM-14",
        note="The deadline is an earlier run's, or the run is no longer RUNNING.",
    ),
    Row(
        "job.185",
        "RUNNING",
        MEMBER_MOVES,
        guard="decides_nothing",
        kind="ignored",
        cite="SEM-11, SEM-12",
        note="No override holds; the default fold may follow.",
    ),
    _never(
        "job.186",
        "lifecycle",
        MEMBER_MOVES,
        "Only a RUNNING box's completion door reads its overrides.",
    ),
    Row(
        "job.187",
        "RUNNING",
        COMPLETION_MOMENT,
        guard="decides_nothing",
        kind="ignored",
        cite="SEM-11, SEM-12",
        note="A member is not done, or an unmet override suppresses the default.",
    ),
    _never("job.188", "lifecycle", COMPLETION_MOMENT, "Only a RUNNING box folds."),
    Row(
        "job.189",
        "lifecycle",
        MEMBER_ENDS,
        guard="is_box",
        kind="ignored",
        cite="SEM-13, SEM-14",
        note="A terminator ends only a RUNNING box; the door or the recompute follows.",
    ),
    _never("job.190", "lifecycle", MEMBER_ENDS, "Only a box has members.", guard="is_job"),
    Row(
        "job.191",
        "lifecycle",
        BOX_RECOMPUTE,
        guard="is_box",
        kind="ignored",
        cite=_SEM15,
        note="No verdict, or the box already has it.",
    ),
    _never("job.192", "lifecycle", BOX_RECOMPUTE, "Only a box has members.", guard="is_job"),
    _never("job.193", "LIVE", BOX_RECOMPUTE, "A live box is not idle; its own end decides it."),
    _never(
        "job.194",
        "TERMINATED",
        BOX_RECOMPUTE,
        "TERMINATED is sticky until the next box start (SEM-13).",
    ),
    Row(
        "job.195",
        "lifecycle",
        BOX_START,
        kind="ignored",
        cite="SEM-10, DL-242",
        note="INACTIVE loses its exit code by a plain write; a live or queued job keeps its run.",
    ),
    Row(
        "job.196",
        "INACTIVE",
        BOX_SET_INACTIVE,
        kind="ignored",
        cite="SEM-18",
        note="Already INACTIVE: the cascade leaves it out.",
    ),
    Row(
        "job.197",
        "lifecycle",
        BOX_ENDS,
        kind="ignored",
        cite="SEM-14",
        note="Not live, or no job_terminator: nothing to terminate.",
    ),
    Row(
        "job.198",
        "lifecycle",
        FLAG_MOVED,
        guard="flag_moves_nothing",
        kind="ignored",
        cite="SEM-20, DL-254",
        note="Not queued, or no status move: the member's completion moment and the wakes follow.",
    ),
    _never(
        "job.199",
        "lifecycle",
        FLAG_MOVED,
        "Only a waiter's kill or failed recheck reports it; an ignored ON_NOEXEC moves no flag.",
        "flag_move_unposted",
    ),
)

#: The flag rows, `job.2NN`: 201 to 217 are `job_flags.01` to `.17`.
_FLAG_ROWS: Final = (
    Row(
        "job.201",
        "ice_off",
        ON_ICE,
        "ice_on",
        "not_live",
        "report_flag_move",
        cite="SEM-20, DL-254, DL-285",
    ),
    Row("job.202", "ice_on", OFF_ICE, "ice_off", cite="SEM-20", note="no re-evaluation"),
    Row("job.203", "ice_on", FORCE_STARTJOB, "ice_off", "force_clears", cite="SEM-23, DL-243"),
    Row("job.204", "hold_off", ON_HOLD, "hold_on", "not_live", cite="SEM-21, DL-254, DL-247"),
    Row("job.205", "hold_on", OFF_HOLD, "hold_off", cite="SEM-21, DL-50", note="attempt the start"),
    Row("job.206", "hold_on", FORCE_STARTJOB, "hold_off", "force_clears", cite="SEM-23, DL-243"),
    Row("job.207", "hold_on", ON_NOEXEC, "hold_off", "noexec_applies", cite=_DL254),
    Row(
        "job.208", "hold_off", BOX_RUNNING, "hold_on", "has_auto_hold", cite="autosys-semantics ss5"
    ),
    Row(
        "job.209",
        "noexec_off",
        ON_NOEXEC,
        "noexec_on",
        "noexec_applies",
        "report_flag_move",
        cite="SEM-22, DL-254",
    ),
    Row("job.210", "noexec_on", OFF_NOEXEC, "noexec_off", cite="DL-243"),
    Row("job.211", "noexec_on", BOX_OFF_NOEXEC, "noexec_off", cite=_DL254),
    Row("job.212", "arm_off", START, "arm_on", "start_arms", cite="SEM-32, DL-54"),
    Row("job.213", "arm_on", ACTUAL_START, "arm_off", cite="DL-54"),
    Row("job.214", "arm_on", DISARM, "arm_off", cite="DL-158"),
    Row("job.215", "arm_on", BOX_RUN_ENDS, "arm_off", cite="DL-54", note="SCHED_DISARM"),
    Row(
        "job.216", "arm_on", READMIT, "arm_off", "readmit_leaves", "report_flag_move", cite="DL-257"
    ),
    Row("job.217", "arm_on", KILLJOB, "arm_off", "queued", "report_flag_move", cite="DL-54"),
    # a split source: the second source of job.201, .202, .204, .205, .209, .210, .213, .214, .217
    Row("job.218", "ice_on", ON_ICE, "ice_on", "not_live", cite="SEM-20", note="as job.201"),
    Row("job.219", "ice_off", OFF_ICE, "ice_off", cite="SEM-20", note="as job.202"),
    Row("job.220", "hold_on", ON_HOLD, "hold_on", "not_live", cite="SEM-21", note="as job.204"),
    Row("job.221", "hold_off", OFF_HOLD, "hold_off", cite="SEM-21", note="as job.205"),
    Row(
        "job.222",
        "noexec_on",
        ON_NOEXEC,
        "noexec_on",
        "noexec_applies",
        "report_flag_move",
        cite="SEM-22, DL-254",
    ),
    Row("job.223", "noexec_off", OFF_NOEXEC, "noexec_off", cite="DL-243", note="as job.210"),
    Row("job.224", "arm_off", ACTUAL_START, "arm_off", cite="DL-54", note="as job.213"),
    Row("job.225", "arm_off", DISARM, "arm_off", cite="DL-158", note="as job.214"),
    Row("job.226", "arm_off", KILLJOB, "arm_off", "queued", "report_flag_move", cite="DL-54"),
    # cells the table had no row for
    Row("job.227", "flags", BOX_RUNNING, kind="ignored", note="Not auto_hold, or already held."),
    Row("job.228", "arm_off", BOX_RUN_ENDS, kind="ignored", note="Only an armed member disarms."),
    Row("job.229", "noexec_off", BOX_OFF_NOEXEC, kind="ignored", note="Only a flagged job clears."),
    Row(
        "job.230",
        "arm_off",
        READMIT,
        guard="readmit_leaves",
        action="report_flag_move",
        kind="internal",
        cite="DL-257",
        note="No arm to drop: the status move follows.",
    ),
)

#: The holding rows, `job.3NN`: 301 to 308 are `job_holding.01` to `.08`.
_HOLDING_ROWS: Final = (
    Row("job.301", "none", ADMITTED_START, "reserved", "has_demand", cite="DL-120"),
    Row("job.302", "held", ADMITTED_START, "taken over", "takes_over", cite=_DL256),
    Row("job.303", "held", ADMITTED_START, "reserved", "on_held_units", cite=_DL256),
    Row("job.304", "reserved", LEAVE_LIVE, "none", "frees_all", cite="DL-120"),
    Row("job.305", "reserved", LEAVE_LIVE, "held", "keeps_held", cite=_DL256),
    Row("job.306", "reserved", LEAVE_LIVE, "none", "spends", cite="SEM-16, DL-120"),
    Row("job.307", "held", RELEASE_RESOURCE, "none", cite=_DL256, note="wake the waiters"),
    Row("job.308", "held", PERIOD_OPENING, "none", "left_catalog", cite=_DL256),
    # 309 and 310 are job.302's choice branches; cells the table had no row for
    Row("job.311", "none", ADMITTED_START, guard="no_demand", kind="ignored", note="No units."),
    Row(
        "job.312",
        "reserved",
        ADMITTED_START,
        "taken over",
        "takes_over",
        cite=_DL256,
        note=(
            "Today's behaviour, a pre-existing oracle bug: a box restarted by its own"
            " completion starts the member again before the member's late settlement, the"
            " start takes the old run's units over, and the late settlement then frees the"
            " new run's. A follow-up fix removes the cell."
        ),
    ),
    Row("job.313", "none", LEAVE_LIVE, kind="ignored", note="The run held nothing."),
    Row("job.314", "held", LEAVE_LIVE, kind="ignored", note="An earlier run's units stay held."),
    Row("job.315", "none", RELEASE_RESOURCE, kind="ignored", cite=_DL256, note="Nothing held."),
    Row(
        "job.316",
        "reserved",
        RELEASE_RESOURCE,
        kind="ignored",
        cite=_DL256,
        note="The run's units go back when it ends.",
    ),
    _never(
        "job.317",
        "none",
        PERIOD_OPENING,
        "The opening addresses only a removed job that holds units.",
    ),
    _never(
        "job.318",
        "reserved",
        PERIOD_OPENING,
        "The boundary refuses a live removed job (period-model ss10.1).",
    ),
    Row(
        "job.319",
        "held",
        STATUS,
        "reserved",
        "injects_live",
        cite="DL-120, DL-256",
        note="Units on a live row are the run's: they settle when it ends.",
    ),
    _never(
        "job.320",
        "reserved",
        ADMITTED_START,
        "A forced start on held units starts from FAILED, never mid-cascade.",
        "on_held_units",
    ),
    _never(
        "job.321",
        "held",
        PERIOD_OPENING,
        "The opening addresses only a job the catalog no longer defines.",
        "in_catalog",
    ),
)

JOB: Final = Statechart(
    name="job",
    root=_ROOT,
    rows=(*_STATUS_ROWS, *_FLAG_ROWS, *_HOLDING_ROWS),
    choices=(
        Choice(
            "injected status",
            (
                Branch("job.171", "INACTIVE", "injected_inactive", "settle_after"),
                Branch("job.172", "STARTING", "injected_starting"),
                Branch("job.173", "RUNNING", "injected_running"),
                Branch("job.174", "SUCCESS", "injected_success", "settle_after_success"),
                Branch("job.175", "FAILURE", "injected_failure", "settle_after"),
                Branch("job.176", "TERMINATED", action="settle_after"),
            ),
        ),
        Choice(
            "taken over",
            (Branch("job.309", "reserved", "has_demand"), Branch("job.310", "none")),
        ),
    ),
    guards=GUARDS,
    actions=ACTIONS,
    clock="logical",
)
