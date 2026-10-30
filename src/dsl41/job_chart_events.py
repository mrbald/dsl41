"""The `job` chart's vocabulary: its events, its instances' data and its events' payloads
(DL-337). The chart, its contract and its guards are in `job_chart`.

A payload whose facts the oracle computes on a guard's first read is a `Protocol`
(`StartAttempt`, `Readmission`, `Noexec`, `Door`, `Verdict`, `BoxVerdict`, `Arm`) implemented by
`oracle_facts` and `oracle_intents`; a payload the tier builds whole is a dataclass (DL-356)."""

from __future__ import annotations

from dataclasses import dataclass, field
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
#: A window-postponed start fires (SEM-33), or a scan for the next eligible day resumes
#: (DL-257).
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
#: A flag move whose status rule reads the flag follows the move as its own item: ON_ICE's on
#: a job not live (job.201, job.234), ON_NOEXEC's where it applies (job.209, job.222).
ICE_MOVED: Final = "ice moved"
NOEXEC_MOVED: Final = "noexec moved"
#: A visit of a RUNNING box's completion door (`Door`): it enters the box's `door` region.
MEMBER_MOVES: Final = "member moves"
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

#: The moments of a cascade (DL-350): self-events whose payload is the tier's step
#: (`Moment`). A move's box rules, its settlement and its wakes.
BOX_RULES: Final = "box rules"
SETTLE: Final = "settle"
WAKE: Final = "wake"
#: The ancestors' overrides after a member's move, and the boxes an iced holder leaves.
WALK_UP: Final = "walk up"
HOLDERS: Final = "holders may leave"
#: The load an admitted start takes, which owes the input the queue's scan (DL-255).
LOAD_TAKEN: Final = "load taken"
#: A box's end reaches its job_terminators, one member at a time (DL-348).
BOX_ENDS_STEP: Final = "box ends step"
#: The batch INACTIVE path's second phase (DL-242).
NOTIFY_STEP: Final = "notify step"
#: The window decisions at a box start (DL-246).
WINDOW_PASS: Final = "window pass"
#: An idle parent's recompute after an operator's INACTIVE (SEM-15); a waiter's disposition
#: after it leaves the queue unstarted (DL-257).
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

    `box`: the job type is BOX. `catalog`: the job has a catalog entry; a row can outlive its
    entry, and STATUS can create one (DL-264). `job_terminator` and `auto_hold`: the member's
    attributes the box's rules test (SEM-14, `OracleCascade._box_ends`; `_box_running`). All
    four are the catalog's and change only when a catalog is installed. What leaving a run does
    with its units is a fact of the settlement (`Settlement`, DL-349).
    """

    box: bool = False
    catalog: bool = True
    job_terminator: bool = False
    auto_hold: bool = False


class Units(Protocol):
    """A tier's handle on one job, which its payloads carry.

    `release`: what leaving its run does with the units its row holds, after a SUCCESS
    (`success`) or any other end, by the store's verb (`oracle_state.unreleased`). It reads
    the row when asked, so the policy is that of the units held at that moment (DL-349).

    `consequences`: what the row whose action is `action` sends and reports beyond its own
    move, in order, read from the tier's facts when the row runs (DL-350). `action` names the
    chart's delegating action. None sends nothing."""

    def release(self, success: bool) -> Release: ...

    def consequences(self, ctx: Context, action: str) -> Effects | None: ...


class Moment(Protocol):
    """A moment's payload: the tier's step, run when the moment pops (DL-350). It reads the
    tier's facts then and reports the items of this step, and itself again for the next step
    after their cascade, or nothing when done. It writes nothing: a write it needs is an
    intent the tier's hook performs."""

    def step(self, ctx: Context) -> Effects | None: ...


@dataclass(frozen=True)
class Payload:
    """What a payload carries besides its facts: `units`, the tier's handle on the job's
    units, which an action asks what its row sends (`Units.consequences`) and builds its
    settlement from (`Settlement`). A row whose action meets a payload with none raises a
    `StatechartError`, since the chart has no consequences of its own (DL-356). An event whose
    rows read no fact carries a bare `Payload`; `first input of a period` carries none. The
    handle is neither compared nor printed."""

    units: Units | None = field(default=None, kw_only=True, compare=False, repr=False)


class StartAttempt(Protocol):
    """One start attempt, decided by `start_verdict` (`oracle_facts.StartFacts`).

    `force`, `explicit` and `scheduled` say who asks: a relay from STARTJOB, FORCE_STARTJOB or
    a window-postponed start sets all three as `_dispatch` passes them; an attempt a wake or
    a retry makes leaves them false, and its refusals go untraced (DL-81). `stale`: a
    postponed start whose box run has moved on (`_deferral_is_stale`), or a scan continuation
    that a newer one replaced (`_rescan_superseded`). `rescan`: the timer resumes a scan and
    attempts nothing. `dated`: the job has date conditions (`schedule`). `armed`: a scheduled
    tick latched the job at a releasable gate (SEM-32, DL-54). `box`: the member's gate on its
    box's run (SEM-10, SEM-20). `condition`: the job's condition holds, or it has
    none. `window`: `_window_side`. `decided`: the window alone decides the attempt, as for a
    box start's pass and a waiter that left the queue for its window (DL-246, DL-257); inside
    the window nothing moves. The start bound is no fact: job.100 counts the item's ancestry
    (DL-350). `settlement_owed`: a run of the job ended inside a cascade still running its box
    rules, and its settlement waits (job.401, DL-340). `box_noexec`: a box above the job is
    ON_NOEXEC (`_noexec_above`). `admissible`: `_admissible`. `demand`: the demand vector
    reserves units (`to_reservations`; a gate entry reserves nothing).
    The guards read the fields in `start_verdict`'s order, so a tier computes only the facts
    the rule reads (`oracle_facts.StartFacts`).
    """

    @property
    def force(self) -> bool: ...
    @property
    def explicit(self) -> bool: ...
    @property
    def scheduled(self) -> bool: ...
    @property
    def stale(self) -> bool: ...
    @property
    def rescan(self) -> bool: ...
    @property
    def dated(self) -> bool: ...
    @property
    def armed(self) -> bool: ...
    @property
    def box(self) -> BoxGate: ...
    @property
    def condition(self) -> bool: ...
    @property
    def window(self) -> WindowSide: ...
    @property
    def decided(self) -> bool: ...
    @property
    def settlement_owed(self) -> bool: ...
    @property
    def box_noexec(self) -> bool: ...
    @property
    def admissible(self) -> bool: ...
    @property
    def demand(self) -> bool: ...
    @property
    def units(self) -> Units: ...

    def relayed(self, *, force: bool) -> StartAttempt:
        """The attempt a relay passes on (`relay_start`): scheduled, refusal traced, forced
        for FORCE_STARTJOB."""
        ...


class Readmission(Protocol):
    """One queue scan's attempt for a waiter, decided by `readmit_verdict`
    (`oracle_facts.ReadmitFacts`).

    `box`: the member's box is RUNNING or not; `none` for a job in no box. `keep_stopped`: an
    admit-only scan keeps a member of a stopped box queued (DL-247, DL-255). `admissible`:
    `_admissible`. `recheck_fails`: `_queued_recheck` names a failed check (DL-257). `demand`
    and `armed` as for `StartAttempt`.
    """

    @property
    def box(self) -> Literal["none", "running", "idle"]: ...
    @property
    def keep_stopped(self) -> bool: ...
    @property
    def admissible(self) -> bool: ...
    @property
    def recheck_fails(self) -> bool: ...
    @property
    def demand(self) -> bool: ...
    @property
    def armed(self) -> bool: ...
    @property
    def units(self) -> Units: ...


@dataclass(frozen=True)
class Injection(Payload):
    """An injected STATUS: a status, or with none an exit code that `Semantics.exit_is_success`
    reads as `success` (SEM-09)."""

    status: str | None = None
    success: bool = False
    #: the engine's interruption hold: the FAILURE also holds the job (DL-424)
    hold: bool = False


class Noexec(Protocol):
    """ON_NOEXEC's payload, which `noexec moved` carries on (DL-254, `oracle_facts.NoexecFacts`).
    On a box: `tree_busy`, a job it contains is on ice, live or queued (`_oob_ignored`);
    `change`, its parent is RUNNING or a job it contains is not INACTIVE. `noexec_settles_box`
    also counts the box's own status."""

    @property
    def tree_busy(self) -> bool: ...
    @property
    def change(self) -> bool: ...
    @property
    def units(self) -> Units: ...


class Arm(Protocol):
    """A payload whose rows read the job's arm (DL-365): DISARM's, KILLJOB's, `actual start`'s
    and `box run ends`'. `armed`: a scheduled tick latched the job (SEM-32, DL-54), read from
    the job's row when a guard reads it."""

    @property
    def armed(self) -> bool: ...


@dataclass(frozen=True)
class Deadline(Payload):
    """A term_run_time timer: `current`, its run is the row's run."""

    current: bool = True


class Verdict(Protocol):
    """The facts one phase of a RUNNING box's completion door reads: `target`, the status it
    decides, or none. The overrides' are `oracle_facts.OverrideFacts`, the default fold's
    `oracle_facts.FoldFacts` (`_all_members_done`)."""

    @property
    def target(self) -> Literal["SUCCESS", "FAILURE"] | None: ...


class Door(Protocol):
    """`member moves`' payload: one visit of a RUNNING box's completion door (SEM-11, SEM-12),
    which the completions of its phases carry (DL-364; `oracle_facts.DoorFacts`).

    `overrides`: the visit reads the overrides, in `overriding`; `folds`: after no override
    holds, it folds, in `folding`. A member's move at its box reads both, or the fold alone on a
    move the overrides do not read; the walk up reads the overrides alone (SEM-12). `override`
    and `fold`: each phase's facts (`Verdict`), built by the phase's entry action
    (`begin_override`, `begin_fold`) and read by its completion's guards; a read before its
    phase began is a statechart error."""

    @property
    def overrides(self) -> bool: ...
    @property
    def folds(self) -> bool: ...
    @property
    def override(self) -> Verdict: ...
    @property
    def fold(self) -> Verdict: ...
    @property
    def units(self) -> Units: ...

    def begin_override(self) -> None:
        """`overriding`'s entry: the overrides' facts are built now."""
        ...

    def begin_fold(self) -> None:
        """`folding`'s entry: the fold's facts are built now."""
        ...


class BoxVerdict(Protocol):
    """An idle box's recompute (`oracle_facts.RecomputeFacts`, SEM-15): `target`, the status it
    decides, or none; `override`, a box_success or box_failure condition decided it."""

    @property
    def target(self) -> Literal["SUCCESS", "FAILURE"] | None: ...
    @property
    def override(self) -> bool: ...
    @property
    def units(self) -> Units: ...


@dataclass(frozen=True)
class Terminator(Payload):
    """A member's end at its box: `fires`, `_box_terminator_fires` (the
    member's box_terminator, its end, `box-terminator-on-terminated`)."""

    fires: bool = True


class Settlement:
    """`leave STARTING or RUNNING`'s payload, the end of a run whose units settle: `success`,
    the move ended in SUCCESS, which frees a `success` policy's units (`release_reservations`);
    `release`, what leaving the run does with the units its row holds for that end.

    `release` is given, or computed and kept on its first read from the handle (`Units`) the
    sending action took from its own payload (DL-349); the holding rows read it when the
    settlement pops. With neither, its first read raises a `StatechartError` rather than free
    a unit the run may keep or spend."""

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
        #: the tier's record of the move whose run settles, for its hook; not compared
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
    """The units of an admitted start (`_w_reserve`, `_w_on_held`): `on_held`, a FORCE_STARTJOB
    starts on held units (DL-256); `demand` as for `StartAttempt`."""

    on_held: bool = False
    demand: bool = True
    #: the end of the run whose units are still on the row when a box restarted by its own
    #: completion starts the member again (job.312); not compared
    previous: Settlement = field(default=Settlement(), compare=False)


#: The verdicts of a start past the gates and the bound (`start_verdict`).
type Admitting = Literal["bypass", "queue", "admit", "on_held"]


@dataclass(frozen=True)
class Admitted(Payload):
    """`start admitted`'s payload: the verdict of the start the admitting row moved
    (`verdict`), and the units an admission or a start on held units reserves or takes over
    (`admission`); a bypass and a queue reserve nothing. `move` is the tier's record of a
    bypass's or a queue's status move, whose box rules and wakes the extent sends; `attempt`
    is the start's facts, which the extent's writes name (DL-350). It is ADMITTED's data: the
    state's entry and its completion read it (DL-364)."""

    verdict: Admitting
    admission: Admission = Admission()
    move: Any = field(default=None, compare=False)
    attempt: Any = field(default=None, compare=False)
