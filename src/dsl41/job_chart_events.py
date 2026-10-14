"""The `job` chart's vocabulary: its events, its instances' data and its
events' payloads (DL-337). The chart, its contract and its guards are in
`job_chart`, which re-exports every name here."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol

from dsl41.state_machine import Context, Effects, StatechartError

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
#: A start past the gates and the bound: its extent (`OracleCascade._extent`).
START_ADMITTED: Final = "start admitted"
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
BOX_ON_NOEXEC: Final = "ON_NOEXEC on its box"
PERIOD_OPENING: Final = "first input of a period"
#: An admitted start leaves the transient `ADMITTED` for STARTING, once its
#: units, its arm and, for a box, its resets are written (DL-350).
STARTING: Final = "starting"

#: The moments of a cascade (DL-350): self-events whose payload is the
#: tier's step (`Moment`). A move's box rules, its settlement and its wakes.
BOX_RULES: Final = "box rules"
SETTLE: Final = "settle"
WAKE: Final = "wake"
#: The ancestors' overrides after a member's move, and the boxes an iced
#: holder leaves the run of.
WALK_UP: Final = "walk up"
HOLDERS: Final = "holders may leave"
#: The queue's scan, and the load an admitted start takes (DL-255).
SCAN: Final = "scan"
LOAD_TAKEN: Final = "load taken"
#: A box's end reaches its job_terminators, one member at a time (DL-348).
BOX_ENDS_STEP: Final = "box ends step"
#: The batch INACTIVE path's second phase (DL-242).
NOTIFY_STEP: Final = "notify step"
#: The window decisions at a box start (DL-246).
WINDOW_PASS: Final = "window pass"
#: An idle parent's recompute after an operator's INACTIVE (SEM-15); a
#: waiter's disposition after it leaves the queue unstarted (DL-257).
AFTER_INJECT: Final = "after inject"
AFTER_LEAVE: Final = "after leave"
#: ON_NOEXEC's start retries (DL-254).
RETRY_STEP: Final = "retry step"
#: Every moment, in the order the chart declares their rows.
MOMENTS: Final = (
    BOX_RULES,
    SETTLE,
    WAKE,
    WALK_UP,
    HOLDERS,
    SCAN,
    LOAD_TAKEN,
    BOX_ENDS_STEP,
    NOTIFY_STEP,
    WINDOW_PASS,
    AFTER_INJECT,
    AFTER_LEAVE,
    RETRY_STEP,
)

# ---- data and payloads

#: What leaving a run does with its units (DL-50, DL-256, SEM-16).
type Release = Literal["free", "keep", "spend"]
#: A member's view of its box's run, for a start attempt; `none` for a job in no box.
type BoxGate = Literal["none", "running", "idle", "ran", "iced_out"]
#: `Oracle._window_side`: the SEM-33 closer-edge rule at this instant.
type WindowSide = Literal["inside", "defer", "skip"]


@dataclass(frozen=True)
class JobData:
    """What a job instance's guards read besides its configuration and the event.

    `box`: the job type is BOX. `catalog`: the job has a catalog entry; a row
    can outlive its entry, and STATUS can create one (DL-264).
    `job_terminator` and `auto_hold`: the member's attributes the box's rules
    test (SEM-14, `OracleCascade._box_ends`; `_box_running`). All four are
    the catalog's, so they change only when a catalog is installed. What
    leaving a run does with its units is the run's, a fact of the settlement
    (`Settlement`, DL-349).
    """

    box: bool = False
    catalog: bool = True
    job_terminator: bool = False
    auto_hold: bool = False


class Units(Protocol):
    """A tier's handle on one job, which its payloads carry.

    `release`: what leaving its run does with the units its row holds, after
    a SUCCESS (`success`) or after any other end, as
    `RuntimeState.release_reservations` decides it with the pool's
    `keeps_held`. It reads the row when asked, so the policy is the one of the
    units the row holds at that moment (DL-349).

    `consequences`: what a row of `kind` sends and reports beyond its own
    move, in order, read from the tier's facts when the row runs
    (DL-350). The kinds are the chart's actions that delegate: a status
    move's box rules, settlement and wakes, a box's end, a flag's wakes and
    retries, an admitted start's extent. None sends nothing."""

    def release(self, success: bool) -> Release: ...

    def consequences(self, ctx: Context, kind: str) -> Effects | None: ...


class Moment(Protocol):
    """A moment's payload: the tier's step, run when the moment pops
    (DL-350). It reads the tier's facts then and reports the items of
    this step, and itself again to take the next step after their cascade,
    or nothing once it is done. It writes nothing: a write it needs is an
    intent the tier's hook performs."""

    def step(self, ctx: Context) -> Effects | None: ...


@dataclass(frozen=True)
class Payload:
    """What a payload carries besides its facts: `units`, the tier's handle
    on the job's units, from which an action builds the settlement it sends
    (`Settlement`). None for a chart no tier drives: a settlement it sends
    then has no policy, and reading one is a `StatechartError`, so such a
    chart gives each settlement its release. An event whose rows read no fact
    carries a bare `Payload` where the tier has a handle to give. The handle
    is neither compared nor printed."""

    units: Units | None = field(default=None, kw_only=True, compare=False, repr=False)


@dataclass(frozen=True)
class Attempt(Payload):
    """One start attempt, decided by `start_verdict`.

    `force`, `explicit` and `scheduled` say who asks: a relay from STARTJOB,
    FORCE_STARTJOB or a window-postponed start sets all three as `_dispatch`
    passes them; an attempt a wake or a retry makes leaves them false, and its
    refusals go untraced (DL-81). `stale`: a postponed start no longer belongs
    to its box run (`_deferral_is_stale`), or a scan continuation is
    superseded (`_rescan_superseded`). `rescan`: the timer resumes a scan and
    attempts nothing. `dated`: the job has date conditions (`schedule`).
    `box`: the member's gate on its box's run (SEM-10, SEM-20). `condition`:
    the job's condition holds, or it has none. `window`: `_window_side`.
    `decided`: the window alone decides the attempt, as `_window_side`
    does for a box start's pass and for a waiter that left the queue for its
    window (DL-246, DL-257): no other gate is read, and inside the window
    nothing moves. The start bound is no fact: job.100 counts the item's
    ancestry (DL-350).
    `settlement_owed`: a run of the job ended inside a cascade still running
    its box rules, and its settlement waits (job.401, DL-340).
    `box_noexec`: a box above the job is ON_NOEXEC (`_noexec_above`).
    `admissible`: `_admissible`. `demand`: the demand vector reserves units
    (`to_reservations`; a gate entry reserves nothing, so `reserve` moves
    nothing for it).
    The guards read the fields in `start_verdict`'s order, so a tier that
    computes each fact on its first read computes only those the rule reads
    (`oracle_facts.StartFacts`).
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
    decided: bool = False
    settlement_owed: bool = False
    box_noexec: bool = False
    admissible: bool = True
    demand: bool = True

    def relayed(self, *, force: bool) -> Attempt:
        """The attempt a relay passes on (`relay_start`): scheduled, its
        refusal traced, forced for FORCE_STARTJOB."""
        return replace(self, force=force, explicit=True, scheduled=True)


@dataclass(frozen=True)
class Readmission:
    """One queue scan's attempt for a waiter, decided by `readmit_verdict`.

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
class Injection(Payload):
    """An injected STATUS: a status, or with none an exit code, read by
    `Semantics.exit_is_success` as `success` (SEM-09)."""

    status: str | None = None
    success: bool = False


@dataclass(frozen=True)
class Noexec(Payload):
    """ON_NOEXEC on a box (DL-254): `tree_busy`, a job it contains is on ice,
    live or queued (`_oob_ignored`); `change`, its parent is RUNNING or a job
    it contains is not INACTIVE. `noexec_settles_box` also counts the box's
    own status, which the configuration decides."""

    tree_busy: bool = False
    change: bool = True


@dataclass(frozen=True)
class Deadline(Payload):
    """A term_run_time timer: `current`, its run is the row's run."""

    current: bool = True


@dataclass(frozen=True)
class Verdict(Payload):
    """One step of a RUNNING box's completion door: `target`, the status it
    decides, or none. `member moves` reads the overrides
    (`OverrideFacts`), `completion moment` the default fold
    (`_all_members_done`, `FoldFacts`)."""

    target: Literal["SUCCESS", "FAILURE"] | None = None


@dataclass(frozen=True)
class BoxVerdict:
    """An idle box's recompute (`RecomputeFacts`, SEM-15): `target`, the
    status it decides, or none; `override`, a box_success or box_failure
    condition decided it rather than the default verdict."""

    target: Literal["SUCCESS", "FAILURE"] | None = None
    override: bool = False


@dataclass(frozen=True)
class FlagMoved(Payload):
    """The flag move a status rule follows: `cause` is the input that moved it,
    and `noexec` ON_NOEXEC's payload, passed on with its `units`."""

    cause: Literal["ON_ICE", "KILLJOB", "readmit", "ON_NOEXEC"]
    noexec: Noexec = Noexec()


@dataclass(frozen=True)
class Terminator(Payload):
    """A member's end at its box: `fires`, `_box_terminator_fires` (the
    member's box_terminator, its end, `box-terminator-on-terminated`)."""

    fires: bool = True


class Settlement:
    """`leave STARTING or RUNNING`'s payload, the end of a run whose units
    settle: `success`, the move ended in SUCCESS, which frees a `success`
    policy's units (`release_reservations`); `release`, what leaving the run
    does with the units its row holds for that end.

    `success` is known when the payload is built. `release` is given, or
    computed on its first read from the handle (`Units`) the action that
    sends the settlement took from its own payload (DL-349), and kept: the
    holding rows read it when the settlement pops, against the units the row
    holds then. With neither, the policy is unknown: its first read raises a
    `StatechartError`, which the executor reports with the instance and the
    item, rather than free a unit the run may keep or spend."""

    __slots__ = ("success", "_units", "_release", "move")

    def __init__(
        self,
        success: bool = False,
        release: Release | None = None,
        *,
        units: Units | None = None,
        move: Any = None,
    ) -> None:
        self.success = success
        self._units = units
        self._release = release
        #: the tier's record of the move whose run settles, for its hook; it
        #: takes no part in a comparison
        self.move = move

    @property
    def release(self) -> Release:
        """What leaving the run does with its units: free, keep or spend."""
        if self._release is None:
            if self._units is None:
                raise StatechartError(
                    "a settlement with no release and no handle on the job's units:"
                    " its policy is unknown (DL-349)"
                )
            self._release = self._units.release(self.success)
        return self._release

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Settlement):
            return NotImplemented
        return (self.success, self.release) == (other.success, other.release)

    def __hash__(self) -> int:
        return hash((self.success, self.release))

    def __repr__(self) -> str:
        known = "" if self._release is None else f", release={self._release!r}"
        return f"Settlement(success={self.success}{known})"


@dataclass(frozen=True)
class Admission(Payload):
    """The units of an admitted start (`_w_reserve`, `_w_on_held`):
    `on_held`, a FORCE_STARTJOB starts on held units (DL-256); `demand` as
    for `Attempt`."""

    on_held: bool = False
    demand: bool = True
    #: the end of the run whose units are still on the row when a box restarted
    #: by its own completion starts the member again (job.312); not compared,
    #: so comparing two admissions reads no settlement
    previous: Settlement = field(default=Settlement(), compare=False)


#: The verdicts of a start past the gates and the bound (`start_verdict`).
type Admitting = Literal["bypass", "queue", "admit", "on_held"]


@dataclass(frozen=True)
class Admitted(Payload):
    """`start admitted`'s payload: the verdict of the start that the admitting
    row moved (`verdict`), and the units an admission or a start on held units
    reserves or takes over (`admission`, `Admission`). A bypass and a queue
    reserve nothing. `move` is the tier's record of a bypass's or a queue's
    status move, whose box rules and wakes the extent sends, and `attempt` the
    start's facts, which the extent's writes name (DL-350)."""

    verdict: Admitting
    admission: Admission = Admission()
    move: Any = field(default=None, compare=False)
    attempt: Any = field(default=None, compare=False)
