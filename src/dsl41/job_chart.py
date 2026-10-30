"""The composite `job` statechart: one job's status, flags and holding in one declaration
(docs/statechart-engine.md §11, DL-337), registered in `machines.MACHINES`.

The oracle (`oracle`, DL-341) drives it for every input and its whole cascade (DL-350) and
writes what each fired row reached. Each status state's entry action (`writes_status`) reports
the status it is; the oracle's hook writes it with the line of the row that entered it.

Shape. The root `job` has three orthogonal regions: `status`, `flags` and `holding`. `status`
holds `lifecycle`, the superstate of every status, and inside it the sets the rules name:
`IDLE` (INACTIVE and `TERMINAL`), `TERMINAL` (SUCCESS and `FAILED`), `FAILED` (FAILURE and
TERMINATED) and `LIVE` (STARTING and RUNNING), with QUE_WAIT beside them. A row for every
status of one set has that set as its source. `flags` holds one composite state with three
orthogonal regions, `ice`, `hold` and `noexec`, each `off` or `on`; a flag seeded `on` (SEM-24)
is a restore, not a move. `holding` is `none`, `reserved` (a run's units) or `held` (units a
renewable kept after the run, DL-256).

The scheduled tick's arm (SEM-32, DL-54) is no region, since no row fires on it: it is a fact
of the payloads whose rows read it (`armed`), read from the job's row (DL-365). A start's gates
read it; the rows of `actual start`, DISARM, `box run ends`, KILLJOB on a waiter and a
waiter's failed recheck are guarded on it where its value parts two effects, and the hook
writes the arm they set or clear.

A box is an instance of this chart whose data says so (`JobData.box`); the box rules are rows
guarded by it. RUNNING holds one region, `door`: a RUNNING box's completion door (SEM-11,
SEM-12, DL-371). It rests in `settled`; a visit (`member moves`) enters `overriding`, whose entry
builds the overrides' facts, or `folding`, whose entry builds the fold's, and each phase's
completion decides with the visit's payload: a status, the fold, or `settled`. A job that is not
a box rests in `settled`.

Ids carry the chart's name, and the hundred names the region: `job.1NN` and `job.4NN` are
status rows, `job.2NN` flag rows, `job.3NN` holding rows (DL-337, DL-349). A box's ON_NOEXEC on
the jobs it contains is the event `ON_NOEXEC on its box`, beside its OFF_NOEXEC twin; its rows
are job.231 to job.233 (DL-347).

Guards read the instance's configuration, its data (`JobData`) and the event's payload. The
payload carries the facts the rule code reads outside the job's own row: its box's run,
condition truth, the run window, admission, the members' verdicts and the policy of a run's
end; each payload class names the oracle predicate behind each field. It also carries the
tier's handle on the job's units (`Payload.units`), from which an action builds the settlement
it sends, so the policy is read when the settlement pops (DL-349).

One input can move several regions of one job, and the order of those moves is the contract,
not the region order. Two forms declare it:

- a chained item that a row's action reports to the same instance (statechart-engine §8),
  wherever a later step reads an earlier step's write; it reads the configuration at its own
  time. A flag move reports its own event, and the status rule that reads the flag follows:
  ON_ICE's `ice moved` dequeues a waiter, ON_NOEXEC's `noexec moved` settles the status. An
  admitting verdict sends `start admitted` first, and the rest of the start follows from it:
  an actual start consumes the arm (`actual start`); an admitted start reserves or takes over
  units first (`admitted start`). STARTJOB, FORCE_STARTJOB and a window-postponed start relay
  the attempt to `start`, after the forced clears.
- rows of a sibling region on the same event, only where every step reads the configuration
  before the input: a forced start clears ice and hold, an injected live status makes held
  units the run's.

The settlement of a run's units has two orders. A move on its own (`OracleCascade._after`)
runs the box rules, then the settlement, then the wakes: the status row sends the moments
`box rules`, `settle` and `wake`, and `settle` sends `leave STARTING or RUNNING`. A move of
the batch INACTIVE path (`OracleCascade._on_cascades`) settles first: its row sends `leave
STARTING or RUNNING` ahead of anything else. Either way the settlement carries the end the
move made, not the status it finds. A box restarted by its own completion can start the member
again inside the member's box rules, before its `settle` (DL-340). Past the bound, that attempt
finds its run's settlement owed (`StartAttempt.settlement_owed`) and takes the internal row
job.401, which moves nothing: its action sends the ended run's settlement, by the end it made,
then the same attempt again, so `StartAttempt.admissible` is read after the settlement
(DL-349). The member's `settle`, finding it live, moves nothing (job.418). No start meets an
unsettled run, so job.312 is never reached and is marked unreachable.

The per-job start bound (DL-304) is the refusal row job.100: an attempt past the gates and the
window that descends from `MAX_NESTED_STARTS` admitted starts of its own job (`start admitted`
items on its instance, in its ancestry) is refused with a trace line whoever asked.

The cascade between instances is declared too (DL-350): a box's rules on its members, a
member's moves on its box, the wakes, the queue's scan, sent to the `queue_scan` coordinator
(`scan_chart`, DL-372), and the start retries. A row's action
sends what its move causes through the tier's handle (`Units.consequences`). A call-then-
continue site is a moment: a self-event whose payload carries the tier's step (`Moment`), run
as a lifecycle row that moves nothing. An admitted start enters the transient state ADMITTED
from its `start admitted` (job.402, job.426), or a waiter from its readmission (job.108). The
entry action sends the units, the run's start and a box's resets; ADMITTED's completion, which
carries the payload that entered it, moves to STARTING after their cascades (DL-364). No state
defers (§12).

The walks that check this chart carry no run window and no true condition; synthesized cells
and one windowed replay check the facts of those paths.

Modules. The events, data and payloads are in `job_chart_events`; the states, rows and the
configuration a row projects to in `job_chart_rows`; the guards, actions and the chart `JOB`
here. Each importer imports a name from the module that defines it (DL-74); this docstring is
the contract of all three.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final, Literal

from dsl41.job_chart_events import (
    ADMITTED_START,
    FORCE_STARTJOB,
    ICE_MOVED,
    LEAVE_LIVE,
    START,
    START_ADMITTED,
    Admission,
    Admitted,
    StartAttempt,
    Arm,
    BoxVerdict,
    Deadline,
    Door,
    Injection,
    JobData,
    Moment,
    Noexec,
    Payload,
    Readmission,
    Release,
    Settlement,
    Terminator,
    Units,
)
from dsl41.job_chart_rows import CHOICES, ROOT, ROWS
from dsl41.oracle_state import LIVE, LIVE_OR_QUEUED
from dsl41.state_machine import INITIAL, Context, Effects, Event, Statechart, StatechartError

#: DL-304: how many admitted starts of a job the ancestry of an attempt of it may hold
#: (job.100). A start's own cascade may start the job once more: a box start's window pass can
#: skip-complete run one and its wakes start run two (DL-246). A start nested inside two
#: starts of the same job is a re-trigger loop (L010's pattern) and gets a START_REFUSED line.
#: Starts that follow one another are never refused.
MAX_NESTED_STARTS: Final = 2


def _data(ctx: Context) -> JobData:
    data: JobData = ctx.data
    return data


def _injected(ctx: Context) -> str | None:
    injection: Injection = ctx.event.payload
    return injection.status


def _units(ctx: Context) -> Units:
    """The tier's handle on the job that the item's payload carries (`Payload.units`); a
    payload with none is a statechart error, since the chart has no consequences of its own
    (DL-356)."""
    units: Units | None = getattr(ctx.event.payload, "units", None)
    if units is None:
        raise StatechartError(
            f"{ctx.event.name}: a payload with no handle on the job's units, so what the"
            " row sends is unknown"
        )
    return units


def _consequences(ctx: Context, action: str) -> Effects | None:
    """What the tier's handle says `action` sends beyond its own move (`Units.consequences`)."""
    return _units(ctx).consequences(ctx, action)


def _nested_starts(ctx: Context) -> int:
    """The `start admitted` items on the job's own instance in the item's ancestry (DL-304,
    DL-350)."""
    return ctx.count((ctx.key, START_ADMITTED))


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
    "skip_inactive_member",
]


def start_verdict(ctx: Context) -> StartVerdict:
    """What a start attempt comes to, in the order the rule code tests it.

    Reads the payload (`StartAttempt`), the data (`box`, `catalog`), the configuration and the
    item's ancestry (`MAX_NESTED_STARTS`, DL-304). A forced attempt arrives with ice and hold
    cleared by FORCE_STARTJOB's own rows, so it reads neither gate. A decided attempt reads the
    window alone (`_window_side`). Each test is a named condition below; the order of the tests
    is the precedence, and the lazy facts they read are read in that order (DL-370).
    """
    attempt: StartAttempt = ctx.event.payload
    data: JobData = ctx.data
    active = ctx.active
    if attempt.decided:
        if _inside_window(attempt) or _live_or_queued(active):
            return "ignored"
        return _skipped(attempt, active)
    if _superseded(attempt, active):
        return "refused"
    if attempt.rescan or not data.catalog:
        return "ignored"
    if _live_or_queued(active):
        return "refused" if attempt.explicit else "ignored"
    if not attempt.force:
        gated = _gated(attempt, active)
        if gated is not None:
            return gated
    if not _inside_window(attempt):
        return _skipped(attempt, active)
    if _nested_starts(ctx) >= MAX_NESTED_STARTS:
        return "bounded"
    if attempt.settlement_owed:
        return "settle_first"
    if _bypasses_on_noexec(attempt, active, data.box):
        return "bypass"
    if _forced_on_held_units(attempt, active):
        return "on_held"
    return "admit" if attempt.admissible else "queue"


def _superseded(attempt: StartAttempt, active: frozenset[str]) -> bool:
    """A postponed start whose box run has moved on, a scan continuation that a newer one
    replaced (`stale`), or a scan's timer for a job no longer INACTIVE (`rescan`)."""
    return attempt.stale or (attempt.rescan and "INACTIVE" not in active)


def _live_or_queued(active: frozenset[str]) -> bool:
    """The configuration's status is STARTING, RUNNING or QUE_WAIT: no start meets it."""
    return bool(active & LIVE_OR_QUEUED)


def _inside_window(attempt: StartAttempt) -> bool:
    """The job's run window is open now (SEM-33)."""
    return attempt.window == "inside"


def _gate_iced(active: frozenset[str]) -> bool:
    """The job is ON_ICE (SEM-20)."""
    return "ice_on" in active


def _gate_held(active: frozenset[str]) -> bool:
    """The job is ON_HOLD (SEM-21)."""
    return "hold_on" in active


def _gate_unarmed_tick(attempt: StartAttempt) -> bool:
    """A dated job is asked to start by anything but its scheduled tick, with no arm latched
    (SEM-32, DL-54)."""
    return attempt.dated and not attempt.scheduled and not attempt.armed


def _gate_box_closed(attempt: StartAttempt) -> bool:
    """The job's box is not RUNNING, or the job already ran in it or was iced out of it
    (SEM-10, SEM-20)."""
    return attempt.box in ("idle", "ran", "iced_out")


def _gate_condition_false(attempt: StartAttempt) -> bool:
    """The job has a condition and it does not hold."""
    return not attempt.condition


def _gated(attempt: StartAttempt, active: frozenset[str]) -> StartVerdict | None:
    """The gates a plain attempt meets, in order, each read only when the ones before it hold;
    None when all hold."""
    if _gate_iced(active):
        return "ignored"
    if _gate_held(active):
        return _latched(attempt)
    if _gate_unarmed_tick(attempt):
        return "ignored"
    if _gate_box_closed(attempt):
        return "refused" if attempt.explicit else "ignored"
    if _gate_condition_false(attempt):
        return _latched(attempt)
    return None


def _tick_latches(attempt: StartAttempt) -> bool:
    """A scheduled tick of a dated job latches the arm, unless it is armed already or its
    box is not RUNNING (`_w_arm`)."""
    return attempt.scheduled and attempt.dated and not attempt.armed and attempt.box != "idle"


def _latched(attempt: StartAttempt) -> StartVerdict:
    """A releasable gate blocks the attempt: the tick arms, or nothing moves."""
    return "arm" if _tick_latches(attempt) else "ignored"


def _window_postponed(attempt: StartAttempt) -> bool:
    """The window is not open yet: the start is postponed and moves nothing (DL-246)."""
    return attempt.window == "defer"


def _member_of_running_box(attempt: StartAttempt) -> bool:
    """The job is a member of a RUNNING box it has not run in, or was iced out of (DL-154)."""
    return attempt.box in ("running", "iced_out")


def _inactive(active: frozenset[str]) -> bool:
    """The configuration's status is INACTIVE."""
    return "INACTIVE" in active


def _in_no_box(attempt: StartAttempt) -> bool:
    """The job is a member of no box."""
    return attempt.box == "none"


def _bypasses_on_noexec(attempt: StartAttempt, active: frozenset[str], box: bool) -> bool:
    """A job that is not a box is ON_NOEXEC, or a box above it is (SEM-22)."""
    return not box and ("noexec_on" in active or attempt.box_noexec)


def _forced_on_held_units(attempt: StartAttempt, active: frozenset[str]) -> bool:
    """A forced start of a FAILED job whose units are `held` (DL-256)."""
    return attempt.force and "held" in active and "FAILED" in active


def _skipped(attempt: StartAttempt, active: frozenset[str]) -> StartVerdict:
    """Outside the run window: a postponed start moves nothing; a skip moves a completed job
    to INACTIVE, and a member only in a RUNNING box it has not run in (job.107); an INACTIVE
    member of such a box has no move to ride, and its box rules run without one."""
    if _window_postponed(attempt):
        return "ignored"
    in_run = _member_of_running_box(attempt)
    if _inactive(active):
        return "skip_inactive_member" if in_run else "ignored"
    if _in_no_box(attempt):
        return "skip"
    return "skip_member" if in_run else "ignored"


def start_refused(ctx: Context) -> bool:
    """`start_verdict` is `refused`: a START_REFUSED line, no move."""
    return start_verdict(ctx) == "refused"


def start_moves_nothing(ctx: Context) -> bool:
    """`start_verdict` is `ignored`: the status does not move, and nothing latches."""
    return start_verdict(ctx) == "ignored"


def start_arms(ctx: Context) -> bool:
    """`start_verdict` is `arm`: the tick latches (SEM-32, DL-54)."""
    return start_verdict(ctx) == "arm"


def start_bounded(ctx: Context) -> bool:
    """`start_verdict` is `bounded`: the item descends from `MAX_NESTED_STARTS`
    admitted starts of the job (DL-304), so a START_REFUSED line, whoever
    asked, and no move."""
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
    return start_verdict(ctx) == "admit" and is_job(ctx)


def start_admits_box(ctx: Context) -> bool:
    """`start_verdict` is `admit`, and the data says the job is a box."""
    return start_verdict(ctx) == "admit" and is_box(ctx)


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


def start_skips_inactive_member(ctx: Context) -> bool:
    """`start_verdict` is `skip_inactive_member`: an INACTIVE member of a
    RUNNING box it has not run in (DL-154): resolved with no move."""
    return start_verdict(ctx) == "skip_inactive_member"


# ---- the queue scan (DL-50, DL-247, DL-257)

type ReadmitVerdict = Literal["admit", "cancel", "leave", "keep"]


def readmit_verdict(ctx: Context) -> ReadmitVerdict:
    """What one admission attempt does to a waiter, in the rule's order.

    Reads the payload (`Readmission`) and hold; the rows that read it are QUE_WAIT's. No
    verdict reads ice, since ON_ICE takes a waiter out of the queue (job.112).
    """
    scan: Readmission = ctx.event.payload
    active = ctx.active
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


def readmit_leaves_armed(ctx: Context) -> bool:
    """`readmit_verdict` is `leave`: the queued recheck failed (DL-257); the payload: the
    waiter is armed."""
    return readmit_verdict(ctx) == "leave" and armed(ctx)


def readmit_leaves_unarmed(ctx: Context) -> bool:
    """`readmit_verdict` is `leave`; the payload: the waiter is not armed."""
    return readmit_verdict(ctx) == "leave" and unarmed(ctx)


def readmit_keeps(ctx: Context) -> bool:
    """`readmit_verdict` is `keep`: held, not admissible, or kept by an admit-only scan."""
    return readmit_verdict(ctx) == "keep"


# ---- the admitted start's extent (DL-349, DL-350)


def _admitted_verdict(ctx: Context) -> str:
    admitted: Admitted = ctx.event.payload
    return admitted.verdict


def admitted_bypass(ctx: Context) -> bool:
    """The payload: the admitting row was the bypass (job.101)."""
    return _admitted_verdict(ctx) == "bypass"


def admitted_queue(ctx: Context) -> bool:
    """The payload: the admitting row was the queue (job.103)."""
    return _admitted_verdict(ctx) == "queue"


def admitted_on_held(ctx: Context) -> bool:
    """The payload: the admitting row was the forced start on held units (job.104)."""
    return _admitted_verdict(ctx) == "on_held"


def admitted_admit(ctx: Context) -> bool:
    """The payload: the admitting row was an admission (job.102, job.124)."""
    return _admitted_verdict(ctx) == "admit"


def admitted_by_start(ctx: Context) -> bool:
    """The payload that entered ADMITTED is a start's `start admitted` (job.402, job.426)."""
    return isinstance(ctx.event.payload, Admitted)


def admitted_by_readmit(ctx: Context) -> bool:
    """The payload that entered ADMITTED is a waiter's `readmit` (job.108), the one other row
    that enters it."""
    return not admitted_by_start(ctx)


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
    return injected_inactive(ctx) and in_catalog(ctx) and is_job(ctx)


def injects_inactive_box(ctx: Context) -> bool:
    """The payload's status is INACTIVE; the data: a box."""
    return injected_inactive(ctx) and is_box(ctx)


def injects_hold(ctx: Context) -> bool:
    """The payload is the engine's interruption hold: a FAILURE that holds the job in the same
    transition (DL-424, spec-crash)."""
    injection: Injection = ctx.event.payload
    return injection.hold


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
    queued (`start_verdict` refuses those first)."""
    return in_catalog(ctx) and not_live_or_queued(ctx)


def queued(ctx: Context) -> bool:
    """The configuration's status is QUE_WAIT."""
    return "QUE_WAIT" in ctx.active


def not_live_or_queued(ctx: Context) -> bool:
    """The configuration's status is neither live nor QUE_WAIT: an IDLE status, or the
    transient ADMITTED, which no operator's input meets."""
    return not _live_or_queued(ctx.active)


def not_queued(ctx: Context) -> bool:
    """The configuration's status is not QUE_WAIT."""
    return not queued(ctx)


def armed(ctx: Context) -> bool:
    """The payload: the job's arm is latched (`Arm`, SEM-32, DL-54)."""
    arm: Arm = ctx.event.payload
    return arm.armed


def unarmed(ctx: Context) -> bool:
    """The payload: the job's arm is not latched."""
    return not armed(ctx)


def noexec_own_reason(active: frozenset[str], box: bool) -> str | None:
    """Why the scheduler ignores ON_NOEXEC for the job's own ice and status, or None (DL-254):
    ice on, RUNNING, or STARTING on a job that is not a box. The guard and the oracle's ignored
    line (`_oob_ignored`) both read it."""
    if "ice_on" in active:
        return "the job is ON_ICE"
    if "RUNNING" in active:
        return "the job is RUNNING"
    if not box and "STARTING" in active:
        return "the job is STARTING"
    return None


def noexec_ignored(ctx: Context) -> bool:
    """ON_NOEXEC is ignored: the configuration has ice on, RUNNING, or STARTING on a job that
    is not a box (`noexec_own_reason`); or the data says a box and the payload's `tree_busy`
    holds. The flag move does not change what it reads."""
    box = _data(ctx).box
    if noexec_own_reason(ctx.active, box) is not None:
        return True
    noexec: Noexec = ctx.event.payload
    return box and noexec.tree_busy


def noexec_applies(ctx: Context) -> bool:
    """Not `noexec_ignored`."""
    return not noexec_ignored(ctx)


def noexec_settles_job(ctx: Context) -> bool:
    """Not `noexec_ignored`; the data: a catalog job, not a box."""
    return noexec_applies(ctx) and in_catalog(ctx) and is_job(ctx)


def noexec_settles_box(ctx: Context) -> bool:
    """Not `noexec_ignored`; the data: a box; the configuration's status is not INACTIVE, or
    the payload's `change` holds (`NoexecFacts.change`)."""
    if not noexec_applies(ctx) or not is_box(ctx):
        return False
    noexec: Noexec = ctx.event.payload
    return "INACTIVE" not in ctx.active or noexec.change


def noexec_moves_nothing(ctx: Context) -> bool:
    """Not `noexec_ignored`, and not `noexec_settles_box`: no box settles here. A job's
    settlement is an inner row's (job.113, job.121), which shadows this one (§3)."""
    return noexec_applies(ctx) and not noexec_settles_box(ctx)


# ---- the job type and the box's rules (SEM-11 to SEM-15)


def is_box(ctx: Context) -> bool:
    """The data says the job is a box."""
    return _data(ctx).box


def is_job(ctx: Context) -> bool:
    """The data says the job is not a box."""
    return not is_box(ctx)


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
    return not in_catalog(ctx)


def current_run(ctx: Context) -> bool:
    """The payload: the deadline's run is the row's run."""
    deadline: Deadline = ctx.event.payload
    return deadline.current


def started_job(ctx: Context) -> bool:
    """The data: not a box; the payload: the admitted run is the row's run (`Started`)."""
    return is_job(ctx) and current_run(ctx)


def started_box(ctx: Context) -> bool:
    """The data: a box; the payload: the admitted run is the row's run (`Started`)."""
    return is_box(ctx) and current_run(ctx)


#: The statuses an idle box's recompute moves from (SEM-15). A live box's own end decides it,
#: and TERMINATED is sticky until the next box start (SEM-13).
_RECOMPUTED: Final = frozenset({"INACTIVE", "QUE_WAIT", "SUCCESS", "FAILURE"})


def _recomputes(ctx: Context) -> bool:
    """The data: a box; the configuration's status is one the recompute moves from
    (`_RECOMPUTED`); the payload names a verdict that is not that status."""
    if not (is_box(ctx) and ctx.active & _RECOMPUTED):
        return False
    verdict: BoxVerdict = ctx.event.payload
    return verdict.target is not None and verdict.target not in ctx.active


def _box_verdict(ctx: Context, target: str, override: bool) -> bool:
    verdict: BoxVerdict = ctx.event.payload
    return _recomputes(ctx) and verdict.target == target and verdict.override == override


def overrides_success(ctx: Context) -> bool:
    """`_recomputes`; the payload: box_success decides SUCCESS (SEM-15)."""
    return _box_verdict(ctx, "SUCCESS", True)


def overrides_failure(ctx: Context) -> bool:
    """`_recomputes`; the payload: box_failure decides FAILURE (SEM-15)."""
    return _box_verdict(ctx, "FAILURE", True)


def recomputes_nothing(ctx: Context) -> bool:
    """The data: a box; not `_recomputes` and not `live_or_terminated_box`: the payload names
    no verdict, the box already has it, or its status is the transient ADMITTED (SEM-15)."""
    return is_box(ctx) and not _recomputes(ctx) and not live_or_terminated_box(ctx)


def live_or_terminated_box(ctx: Context) -> bool:
    """The data: a box; the configuration's status is STARTING, RUNNING or TERMINATED."""
    return is_box(ctx) and bool(ctx.active & {"LIVE", "TERMINATED"})


def _door(ctx: Context) -> Door:
    door: Door = ctx.event.payload
    return door


def door_reads_overrides(ctx: Context) -> bool:
    """The data: a box; the payload's visit reads the overrides (SEM-12): a member's terminal,
    RUNNING or resolving move, an ice's completion moment, a box start's, or the walk up."""
    return is_box(ctx) and _door(ctx).overrides


def door_folds_only(ctx: Context) -> bool:
    """The data: a box; the payload's visit does not read the overrides: a member's move they
    do not read, which the default fold alone follows (SEM-11)."""
    return is_box(ctx) and not _door(ctx).overrides


def override_success(ctx: Context) -> bool:
    """The payload's overrides decide SUCCESS: box_success holds (SEM-12)."""
    return _door(ctx).override.target == "SUCCESS"


def override_failure(ctx: Context) -> bool:
    """The payload's overrides decide FAILURE: box_failure holds (SEM-12)."""
    return _door(ctx).override.target == "FAILURE"


def _override_unmet(ctx: Context) -> bool:
    """No override of the payload's holds."""
    return _door(ctx).override.target is None


def override_unmet_folds(ctx: Context) -> bool:
    """No override holds, and the payload's visit folds (SEM-11)."""
    return _override_unmet(ctx) and _door(ctx).folds


def override_unmet_settles(ctx: Context) -> bool:
    """No override holds, and the payload's visit does not fold: the walk up (SEM-12)."""
    return _override_unmet(ctx) and not _door(ctx).folds


def fold_success(ctx: Context) -> bool:
    """The payload's fold decides SUCCESS: every member is done, none failed, and no
    box_success is declared (SEM-11)."""
    return _door(ctx).fold.target == "SUCCESS"


def fold_failure(ctx: Context) -> bool:
    """The payload's fold decides FAILURE: every member is done, one failed, and no
    box_failure is declared (SEM-11)."""
    return _door(ctx).fold.target == "FAILURE"


def fold_decides_nothing(ctx: Context) -> bool:
    """The payload's fold decides nothing: a member is not done, or an unmet override
    suppresses the default (SEM-11)."""
    return _door(ctx).fold.target is None


def derives_success(ctx: Context) -> bool:
    """`_recomputes`; the payload: no member failed, and no box_success suppresses the default
    SUCCESS (SEM-15)."""
    return _box_verdict(ctx, "SUCCESS", False)


def derives_failure(ctx: Context) -> bool:
    """`_recomputes`; the payload: a member failed, and no box_failure suppresses the default
    FAILURE (SEM-15)."""
    return _box_verdict(ctx, "FAILURE", False)


def terminator_fires(ctx: Context) -> bool:
    """The data: a box; the payload's `fires` (SEM-14)."""
    terminator: Terminator = ctx.event.payload
    return is_box(ctx) and terminator.fires


# ---- the units (DL-50, DL-120, DL-256)


def has_demand(ctx: Context) -> bool:
    """The payload's demand vector reserves units."""
    admission: Admission = ctx.event.payload
    return admission.demand


def no_demand(ctx: Context) -> bool:
    """The payload's demand vector reserves nothing: reserving nothing is no move."""
    return not has_demand(ctx)


def takes_over(ctx: Context) -> bool:
    """The payload: an admitted start that is not on held units."""
    return not on_held_units(ctx)


def on_held_units(ctx: Context) -> bool:
    """The payload: a FORCE_STARTJOB starts on the held units (DL-256)."""
    admission: Admission = ctx.event.payload
    return admission.on_held


def _release(ctx: Context) -> Release:
    """The settlement's release, read from the units the row holds when it pops (DL-349)."""
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
        start_skips_inactive_member,
        readmit_admits,
        readmit_cancels,
        readmit_leaves_armed,
        readmit_leaves_unarmed,
        readmit_keeps,
        admitted_bypass,
        admitted_queue,
        admitted_on_held,
        admitted_admit,
        admitted_by_start,
        admitted_by_readmit,
        exit_success,
        exit_failure,
        injects,
        injects_inactive_job,
        injects_inactive_box,
        injects_live,
        injects_hold,
        injected_inactive,
        injected_starting,
        injected_running,
        injected_success,
        injected_failure,
        not_live,
        force_clears,
        queued,
        not_live_or_queued,
        not_queued,
        armed,
        unarmed,
        noexec_ignored,
        noexec_applies,
        noexec_settles_job,
        noexec_settles_box,
        noexec_moves_nothing,
        is_box,
        is_job,
        is_terminator,
        has_auto_hold,
        in_catalog,
        left_catalog,
        current_run,
        started_job,
        started_box,
        door_reads_overrides,
        door_folds_only,
        override_success,
        override_failure,
        override_unmet_folds,
        override_unmet_settles,
        fold_success,
        fold_failure,
        fold_decides_nothing,
        overrides_success,
        overrides_failure,
        recomputes_nothing,
        live_or_terminated_box,
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
    attempt: StartAttempt = ctx.event.payload
    return Effects(
        send=((ctx.key, Event(START, attempt.relayed(force=ctx.event.name == FORCE_STARTJOB))),)
    )


def extent_bypass(ctx: Context) -> Effects | None:
    """A bypass (job.101) sends `start admitted` first, and the rest of the
    start follows from it (DL-349); the tier's handle gives it the record of
    the move to SUCCESS, whose box rules and wakes the extent sends."""
    return _consequences(ctx, "extent_bypass")


def extent_queue(ctx: Context) -> Effects | None:
    """A queued start (job.103) sends `start admitted`, which sends the move to
    QUE_WAIT's box rules and wakes: the waiter's units wait for its
    readmission."""
    return _consequences(ctx, "extent_queue")


def extent_admit(ctx: Context) -> Effects | None:
    """An admission (job.102, job.124) sends `start admitted` first, with the
    units it reserves and the payload's handle on the job's units."""
    return _consequences(ctx, "extent_admit")


def extent_on_held(ctx: Context) -> Effects | None:
    """A forced start on held units (job.104) sends `start admitted` first,
    with the held units it takes over."""
    return _consequences(ctx, "extent_on_held")


def bypass_extent(ctx: Context) -> Effects | None:
    """A bypass's `start admitted` (job.424): the run's start, which consumes the arm, then
    its move's box rules, settlement and wakes, as the tier's handle says (DL-350)."""
    return _consequences(ctx, "bypass_extent")


def queue_extent(ctx: Context) -> Effects | None:
    """A queue's `start admitted` (job.425): its move's box rules, settlement and wakes, then
    the load mark of a machine's load it takes (DL-255), as the tier's handle says."""
    return _consequences(ctx, "queue_extent")


def starts_run(ctx: Context) -> Effects | None:
    """ADMITTED's entry: what precedes STARTING, from the payload that entered it. The units
    it reserves or takes over, the run's start, which consumes the arm, then a box's resets of
    its jobs, as the tier's handle says (DL-256, DL-350, DL-364)."""
    return _consequences(ctx, "starts_run")


def starting_admitted(ctx: Context) -> Effects | None:
    """ADMITTED's completion after a start (job.427): the move to STARTING, its box rules,
    settlement and wakes, a box's reset notices, `started`, then the queue's scan when the held
    units an admission took over exceed its demand, and the load mark, as the tier's handle
    says (DL-256, DL-350, DL-364)."""
    return _consequences(ctx, "starting_admitted")


def starting_readmitted(ctx: Context) -> Effects | None:
    """ADMITTED's completion after a readmission (job.428): the move to STARTING, its box
    rules, settlement and wakes, a box's reset notices, then `started`, as the tier's handle
    says (DL-350, DL-364)."""
    return _consequences(ctx, "starting_readmitted")


def admit(ctx: Context) -> Effects | None:
    """A readmitted waiter (job.108) enters ADMITTED with its `readmit`, whose entry sends its
    extent: no `start admitted`, since a readmission is no start the bound counts (DL-350).
    The scan's pass hears that the waiter moved, as the tier's handle says."""
    return _consequences(ctx, "admit")


def settle_ended_run(ctx: Context) -> Effects:
    """A start meets the units of a run whose settlement is still owed
    (`OracleCascade._on_settles_first`): that run settles first, by the end it made, then
    the start is admitted again against what the settlement left. The
    settlement's policy is read from the admission's handle (DL-349). Its
    row, job.312, is marked unreachable: job.401 settles the run before the
    start is admitted, so the oracle's admission carries no previous end."""
    admission: Admission = ctx.event.payload
    previous = Settlement(admission.previous.success, units=_units(ctx))
    return Effects(
        send=(
            (ctx.key, Event(LEAVE_LIVE, previous)),
            (ctx.key, Event(ADMITTED_START, replace(admission, previous=Settlement()))),
        )
    )


def settles_first(ctx: Context) -> Effects | None:
    """An attempt whose run's settlement is owed (job.401, DL-340): that run
    settles by the end it made, and the same attempt is made again, so it
    reads the admission the settlement leaves, as the tier's handle says."""
    return _consequences(ctx, "settles_first")


def writes_status(ctx: Context) -> Effects | None:
    """A status state's entry: the status it is, which the hook writes with the line of the row
    that entered it, as the tier's handle says. The initial entry writes nothing: an instance
    starts in the status its row holds."""
    if ctx.event.name == INITIAL:
        return None
    return _consequences(ctx, "writes_status")


def reads_overrides(ctx: Context) -> None:
    """`overriding`'s entry: the visit's overrides' facts are built now, from the payload that
    entered the phase (`Door.begin_override`); it sends nothing (DL-371)."""
    _door(ctx).begin_override()


def reads_fold(ctx: Context) -> None:
    """`folding`'s entry: the visit's fold's facts are built now, after the overrides' phase
    when the visit read them (`Door.begin_fold`); it sends nothing (DL-371)."""
    _door(ctx).begin_fold()


def moved(ctx: Context) -> Effects | None:
    """A status move on its own (`OracleCascade._after`): the
    move's box rules, then its settlement, then its wakes, as the tier's
    handle says (DL-350)."""
    return _consequences(ctx, "moved")


def terminated(ctx: Context) -> Effects | None:
    """A kill: as `moved`, then a box's job_terminators, for the
    run read before the move (DL-348)."""
    return _consequences(ctx, "terminated")


def completed(ctx: Context) -> Effects | None:
    """A RUNNING box's end by an override or the default fold: as `moved`,
    then its job_terminators if it ended FAILURE, for the run it has then
    (`OracleCascade._box_ends`, DL-348)."""
    return _consequences(ctx, "completed")


def injected(ctx: Context) -> Effects | None:
    """An operator's INACTIVE on a job that is not a box (`_after_inject`):
    as `moved`, then the idle parent's recompute (SEM-15)."""
    return _consequences(ctx, "injected")


def cascades(ctx: Context) -> Effects | None:
    """An operator's INACTIVE on a box (SEM-18, DL-242): the batch INACTIVE
    path, its own settlement first, every job it contains, the disarms, then
    each row's box rules and wakes, then the idle parent's recompute."""
    return _consequences(ctx, "cascades")


def batched(ctx: Context) -> Effects | None:
    """A row of the batch INACTIVE path (a box start's reset, a box's
    cascade): it settles first; its box rules and wakes are the batch's
    second phase (`OracleCascade._notify`)."""
    return _consequences(ctx, "batched")


def cancelled(ctx: Context) -> Effects | None:
    """A waiter of a stopped box cancelled by the scan (job.109): as `moved`,
    and the scan's pass hears that the waiter moved."""
    return _consequences(ctx, "cancelled")


def left_queue(ctx: Context) -> Effects | None:
    """A waiter that left the queue unstarted (job.438, job.439, DL-257): as `moved`, then its
    window or day disposition, and the scan's pass hears that the waiter moved."""
    return _consequences(ctx, "left_queue")


def runs_box(ctx: Context) -> Effects | None:
    """A box's RUNNING (job.125): as `moved`, then its members' auto_hold and
    their starts, then the window decisions of the box starts in progress
    (`OracleCascade._box_running`, DL-246)."""
    return _consequences(ctx, "runs_box")


def skips_in_box(ctx: Context) -> Effects | None:
    """A window skip of an INACTIVE member (job.404, DL-154): no move to
    ride, so its box rules run for it, as an INACTIVE move's would."""
    return _consequences(ctx, "skips_in_box")


def report_ice(ctx: Context) -> Effects:
    """ON_ICE moved a waiter's flag (job.234): the status rule that takes it out of the queue
    follows as its own item (job.112), with the payload's handle on the units (DL-50)."""
    moved = Payload(units=_units(ctx))
    return Effects(send=((ctx.key, Event(ICE_MOVED, moved)),))


def iced(ctx: Context) -> Effects | None:
    """ON_ICE moved the flag of a job that is not a waiter (job.201): the status rule that
    reads it follows, then the box rules its ice may run and its referencers' wakes (SEM-20,
    DL-285), as the tier's handle says."""
    return _consequences(ctx, "iced")


def iced_again(ctx: Context) -> Effects | None:
    """ON_ICE on a job already on ice: its referencers wake again (SEM-20)."""
    return _consequences(ctx, "iced_again")


def held(ctx: Context) -> Effects | None:
    """ON_HOLD on a waiter (job.235, job.236): a held waiter blocks no one, so the queue is
    scanned (DL-247)."""
    return _consequences(ctx, "held")


def released_waiter(ctx: Context) -> Effects | None:
    """OFF_HOLD on a waiter (job.237, job.238): its queue is scanned (DL-50)."""
    return _consequences(ctx, "released_waiter")


def released_hold(ctx: Context) -> Effects | None:
    """OFF_HOLD on a job that is not a waiter (job.205, job.221): it attempts its start
    (SEM-21)."""
    return _consequences(ctx, "released_hold")


def noexec(ctx: Context) -> Effects | None:
    """ON_NOEXEC moved the flag: on a box, every job it contains takes it
    first (DL-347); the status rule that reads it follows; then the jobs it
    released retry their start (DL-254), as the tier's handle says."""
    return _consequences(ctx, "noexec")


def cleared_noexec(ctx: Context) -> Effects | None:
    """OFF_NOEXEC on a box clears every job it contains (DL-254)."""
    return _consequences(ctx, "cleared_noexec")


def released_held(ctx: Context) -> Effects | None:
    """RELEASE_RESOURCE gave held units back: the queue is scanned (DL-256)."""
    return _consequences(ctx, "released_held")


def moment(ctx: Context) -> Effects | None:
    """A moment of a cascade (DL-350): the tier's step its payload
    carries (`Moment`), run now."""
    payload: Moment = ctx.event.payload
    if not hasattr(payload, "step"):
        raise StatechartError(f"{ctx.event.name}: a moment with no step of the tier's")
    return payload.step(ctx)


ACTIONS: Final = {
    action.__name__: action
    for action in (
        relay_start,
        extent_bypass,
        extent_queue,
        extent_admit,
        extent_on_held,
        bypass_extent,
        queue_extent,
        starts_run,
        starting_admitted,
        starting_readmitted,
        admit,
        settle_ended_run,
        settles_first,
        writes_status,
        reads_overrides,
        reads_fold,
        moved,
        terminated,
        completed,
        injected,
        cascades,
        batched,
        cancelled,
        left_queue,
        runs_box,
        skips_in_box,
        report_ice,
        iced,
        iced_again,
        held,
        released_waiter,
        released_hold,
        noexec,
        cleared_noexec,
        released_held,
        moment,
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
