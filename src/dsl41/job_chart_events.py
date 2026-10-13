"""The `job` chart's vocabulary: its events, its instances' data and its
events' payloads (DL-337). The chart, its contract and its guards are in
`job_chart`, which re-exports every name here."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final, Literal

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
BOX_ON_NOEXEC: Final = "ON_NOEXEC on its box"
PERIOD_OPENING: Final = "first input of a period"

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
    `bounded`: the job already has `MAX_NESTED_STARTS` starts in progress
    on the call stack (`_starts_in_progress`, the bound `_start` reads).
    `box_noexec`: a box above the job is ON_NOEXEC (`_noexec_bypasses`).
    `admissible`: `_admissible`. `demand`: the demand vector reserves units
    (`to_reservations`; a gate entry reserves nothing, so `reserve` moves
    nothing for it).
    The guards read the fields in `start_verdict`'s order, so a tier that
    computes each fact on its first read computes only those today's rule
    code reads (`oracle_chart`).
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
    bounded: bool = False
    box_noexec: bool = False
    admissible: bool = True
    demand: bool = True

    def relayed(self, *, force: bool) -> Attempt:
        """The attempt a relay passes on (`relay_start`): scheduled, its
        refusal traced, forced for FORCE_STARTJOB."""
        return replace(self, force=force, explicit=True, scheduled=True)


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
    #: the end of the run whose units are still on the row when a box restarted
    #: by its own completion starts the member again (job.312)
    previous: Settlement = Settlement()
