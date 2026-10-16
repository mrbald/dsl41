"""The `job` chart's states and rows, and the configuration a job row projects to (DL-337).

`job_chart` states the contract and the ids, and builds the chart from these rows."""

from __future__ import annotations

from typing import Final, Literal

from dsl41.job_chart_events import (
    ACTUAL_START,
    ADMITTED_START,
    BOX_ENDS,
    BOX_OFF_NOEXEC,
    BOX_ON_NOEXEC,
    BOX_RECOMPUTE,
    BOX_RUNNING,
    BOX_RUN_ENDS,
    BOX_SET_INACTIVE,
    BOX_START,
    COMPLETION_MOMENT,
    DISARM,
    FLAG_MOVED,
    FORCE_STARTJOB,
    KILLJOB,
    LEAVE_LIVE,
    MEMBER_ENDS,
    MEMBER_MOVES,
    MOMENTS,
    OFF_HOLD,
    OFF_ICE,
    OFF_NOEXEC,
    ON_HOLD,
    ON_ICE,
    ON_NOEXEC,
    PERIOD_OPENING,
    READMIT,
    RELEASE_RESOURCE,
    RUN_WINDOW,
    SETTLE,
    START,
    START_ADMITTED,
    STARTED,
    STARTING,
    STARTJOB,
    STATUS,
    TERM_RUN_TIME,
)
from dsl41.oracle_state import LIVE, JobRuntime, JobStatus
from dsl41.state_machine import Branch, Choice, Region, Row, State

# ---- configuration

#: The holding region's state: a run's units, or units kept after it (DL-256).
type HoldingState = Literal["none", "reserved", "held"]

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


# ---- the chart

_SEM10 = "SEM-10"
_DL254 = "DL-254"
_BOX_NOEXEC = "SEM-22, DL-254, DL-347"
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
#: An admitted start between its admission and its STARTING (DL-350): transient, left by
#: `starting` once its units, arm and a box's resets are written. No row projects to it.
_ADMITTED: Final = State("ADMITTED")
#: Every status; the source of the rows that apply to all of them.
_LIFECYCLE: Final = State(
    "lifecycle", regions=(Region("status", "IDLE", (_IDLE, State("QUE_WAIT"), _LIVE, _ADMITTED)),)
)
ROOT: Final = State(
    "job",
    regions=(
        Region("status", "lifecycle", (_LIFECYCLE,)),
        Region("flags", "flags", (State("flags", regions=tuple(_flag(f) for f in _FLAGS)),)),
        Region("holding", "none", (State("none"), State("reserved"), State("held"))),
    ),
)

#: The status rows, `job.1NN`: 100 is the start bound (DL-304), 101 to 158 are the status
#: moves, 159 and above are a split source's further rows and the cells the rules move nothing in.
_STATUS_ROWS: Final = (
    Row(
        "job.100",
        "IDLE",
        START,
        guard="start_bounded",
        kind="internal",
        cite="DL-304",
        note=(
            "L010: a re-trigger loop inside one instant. A start that descends from"
            " MAX_NESTED_STARTS admitted starts of the same job, its `start admitted` items"
            " in the attempt's ancestry, is refused with a START_REFUSED line."
        ),
    ),
    Row(
        "job.101", "IDLE", START, "SUCCESS", "start_bypasses", "extent_bypass", cite="SEM-22, DL-54"
    ),
    Row(
        "job.102",
        "IDLE",
        START,
        "ADMITTED",
        "start_admits_job",
        "extent_admit",
        cite="SEM-10, DL-120",
    ),
    Row(
        "job.103",
        "IDLE",
        START,
        "QUE_WAIT",
        "start_queues",
        "extent_queue",
        cite="DL-50, DL-247, DL-255",
    ),
    Row("job.104", "FAILED", START, "ADMITTED", "start_on_held", "extent_on_held", cite=_DL256),
    Row("job.105", "STARTING", STARTED, "RUNNING", "started_job", "moved", cite=_SEM10),
    Row("job.106", "TERMINAL", START, "INACTIVE", "start_skips", "moved", cite="SEM-33, DL-246"),
    Row(
        "job.107",
        "TERMINAL",
        START,
        "INACTIVE",
        "start_skips_member",
        "moved",
        cite="SEM-33, DL-154",
    ),
    Row(
        "job.108", "QUE_WAIT", READMIT, "ADMITTED", "readmit_admits", "admit", cite="DL-50, DL-257"
    ),
    Row(
        "job.109",
        "QUE_WAIT",
        READMIT,
        "INACTIVE",
        "readmit_cancels",
        "cancelled",
        cite="DL-50, DL-247",
    ),
    Row("job.110", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "left_waiter", "left_queue", cite="DL-257"),
    Row(
        "job.111",
        "QUE_WAIT",
        FLAG_MOVED,
        "TERMINATED",
        "killed_waiter",
        "moved",
        cite="DL-50, DL-54",
    ),
    Row(
        "job.112", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "iced_waiter", "moved", cite="DL-50, DL-285"
    ),
    Row(
        "job.113",
        "QUE_WAIT",
        FLAG_MOVED,
        "INACTIVE",
        "noexec_settles_job",
        "injected",
        cite=_DL254,
    ),
    Row("job.114", "LIVE", KILLJOB, "TERMINATED", action="terminated", cite="SEM-14"),
    Row("job.115", "RUNNING", TERM_RUN_TIME, "TERMINATED", "current_run", "terminated", cite=_SS5),
    Row("job.116", "LIVE", BOX_ENDS, "TERMINATED", "is_terminator", "terminated", cite="SEM-14"),
    Row("job.117", "lifecycle", STATUS, "SUCCESS", "exit_success", "moved", cite="SEM-09"),
    Row("job.118", "lifecycle", STATUS, "FAILURE", "exit_failure", "moved", cite="SEM-09"),
    Row("job.119", "lifecycle", STATUS, "injected status", "injects", cite=_DL264),
    Row(
        "job.120",
        "lifecycle",
        STATUS,
        "INACTIVE",
        "injects_inactive_job",
        "injected",
        cite=_DL242,
    ),
    Row(
        "job.121",
        "FAILED",
        FLAG_MOVED,
        "INACTIVE",
        "noexec_settles_job",
        "injected",
        cite="SEM-22, DL-243",
    ),
    Row("job.122", "TERMINAL", BOX_START, "INACTIVE", action="batched", cite="SEM-10, DL-242"),
    Row(
        "job.123", "QUE_WAIT", BOX_SET_INACTIVE, "INACTIVE", action="batched", cite="SEM-18, DL-242"
    ),
    Row(
        "job.124",
        "IDLE",
        START,
        "ADMITTED",
        "start_admits_box",
        "extent_admit",
        cite="SEM-10, DL-242",
    ),
    Row(
        "job.125",
        "STARTING",
        STARTED,
        "RUNNING",
        "started_box",
        "runs_box",
        cite="SEM-10, SEM-33, DL-246",
    ),
    Row(
        "job.126",
        "RUNNING",
        MEMBER_MOVES,
        "SUCCESS",
        "verdict_success",
        "completed",
        cite="SEM-12",
    ),
    Row(
        "job.127",
        "RUNNING",
        MEMBER_MOVES,
        "FAILURE",
        "verdict_failure",
        "completed",
        cite="SEM-12, SEM-14",
    ),
    Row(
        "job.128",
        "RUNNING",
        COMPLETION_MOMENT,
        "SUCCESS",
        "verdict_success",
        "completed",
        cite="SEM-11",
    ),
    Row(
        "job.129",
        "RUNNING",
        COMPLETION_MOMENT,
        "FAILURE",
        "verdict_failure",
        "completed",
        cite="SEM-11, SEM-14",
    ),
    Row(
        "job.130",
        "RUNNING",
        MEMBER_ENDS,
        "TERMINATED",
        "terminator_fires",
        "terminated",
        cite="SEM-14",
    ),
    Row("job.131", "INACTIVE", BOX_RECOMPUTE, "SUCCESS", "overrides_success", "moved", cite=_SEM15),
    Row("job.132", "INACTIVE", BOX_RECOMPUTE, "FAILURE", "overrides_failure", "moved", cite=_SEM15),
    Row("job.133", "INACTIVE", BOX_RECOMPUTE, "SUCCESS", "derives_success", "moved", cite=_SEM15),
    Row("job.134", "INACTIVE", BOX_RECOMPUTE, "FAILURE", "derives_failure", "moved", cite=_SEM15),
    Row(
        "job.135",
        "lifecycle",
        STATUS,
        "INACTIVE",
        "injects_inactive_box",
        "cascades",
        cite="SEM-18",
    ),
    Row("job.136", "IDLE", FLAG_MOVED, "INACTIVE", "noexec_settles_box", "cascades", cite=_DL254),
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
        Row(
            f"job.{152 + i}",
            s,
            ON_NOEXEC,
            guard="noexec_ignored",
            kind="internal",
            cite=_DL254,
            false_unreachable=(
                "the vendor ignores ON_NOEXEC on every RUNNING job and box"
                if s == "RUNNING"
                else ""
            ),
        )
        for i, s in enumerate(
            ("INACTIVE", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED", "QUE_WAIT")
        )
    ),
    # a split source: the other sources of job.123, job.131 to job.134 and job.136
    Row("job.159", "LIVE", BOX_SET_INACTIVE, "INACTIVE", action="batched", cite="SEM-18, DL-242"),
    Row("job.160", "TERMINAL", BOX_SET_INACTIVE, "INACTIVE", action="batched", cite="SEM-18"),
    Row("job.161", "QUE_WAIT", BOX_RECOMPUTE, "SUCCESS", "overrides_success", "moved", cite=_SEM15),
    Row("job.162", "FAILURE", BOX_RECOMPUTE, "SUCCESS", "overrides_success", "moved", cite=_SEM15),
    Row("job.163", "QUE_WAIT", BOX_RECOMPUTE, "FAILURE", "overrides_failure", "moved", cite=_SEM15),
    Row("job.164", "SUCCESS", BOX_RECOMPUTE, "FAILURE", "overrides_failure", "moved", cite=_SEM15),
    Row("job.165", "QUE_WAIT", BOX_RECOMPUTE, "SUCCESS", "derives_success", "moved", cite=_SEM15),
    Row("job.166", "FAILURE", BOX_RECOMPUTE, "SUCCESS", "derives_success", "moved", cite=_SEM15),
    Row("job.167", "QUE_WAIT", BOX_RECOMPUTE, "FAILURE", "derives_failure", "moved", cite=_SEM15),
    Row("job.168", "SUCCESS", BOX_RECOMPUTE, "FAILURE", "derives_failure", "moved", cite=_SEM15),
    Row(
        "job.169", "QUE_WAIT", FLAG_MOVED, "INACTIVE", "noexec_settles_box", "cascades", cite=_DL254
    ),
    Row(
        "job.170",
        "STARTING",
        FLAG_MOVED,
        "INACTIVE",
        "noexec_settles_box",
        "cascades",
        cite=_DL254,
        false_unreachable=(
            "at STARTING only a box's applied ON_NOEXEC reports a flag move, and the box is not"
            " INACTIVE"
        ),
    ),
    # 171 to 176 are job.119's choice branches
    # the inputs that relay a start attempt (statechart-engine §8)
    Row("job.177", "lifecycle", STARTJOB, action="relay_start", kind="internal", cite=_SEM10),
    Row(
        "job.178", "lifecycle", FORCE_STARTJOB, action="relay_start", kind="internal", cite="SEM-23"
    ),
    Row("job.179", "lifecycle", RUN_WINDOW, action="relay_start", kind="internal", cite="SEM-33"),
    # cells the rules move nothing in
    Row(
        "job.180",
        "lifecycle",
        START,
        guard="start_moves_nothing",
        kind="ignored",
        cite="SEM-20, SEM-21, SEM-32, SEM-33",
        note="A gate holds the attempt silently, a tick arms, or a postponed start waits.",
        false_unreachable="the catch-all of `start`: an attempt that moves the status takes an"
        " inner row, and no row is a gap (§10)",
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
    Row(
        "job.183",
        "lifecycle",
        STARTED,
        kind="ignored",
        cite="SEM-10, DL-350",
        note=(
            "The admitted run is no longer STARTING: a wake of its STARTING ended it, or"
            " started the job again (`Started.current`)."
        ),
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
        false_unreachable="its complement is job.190's, a marked row: only a box has members",
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
        false_unreachable="its complement is job.192's, a marked row: only a box has members",
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
        false_unreachable="its complement is job.199's, a marked row",
    ),
    _never(
        "job.199",
        "lifecycle",
        FLAG_MOVED,
        "Only a waiter's kill or failed recheck reports it; an ignored ON_NOEXEC moves no flag.",
        "flag_move_unposted",
    ),
)

#: The further status rows, `job.4NN`: an attempt's owed settlement (DL-340), the extent of an
#: admitted start, its move to STARTING, a window skip of an inactive member, the cascade's
#: moments, and the cells no input reaches (DL-350).
_MOMENT_ROWS: Final = (
    Row(
        "job.401",
        "IDLE",
        START,
        guard="start_settles_first",
        action="settles_first",
        kind="internal",
        cite="DL-340, DL-349",
        note=(
            "Past the bound, the job's run ended inside a cascade whose box rules hold its"
            " settlement back. The row sends that run's settlement, by the end it made, and"
            " the same attempt again, which reads the admission after the settlement."
        ),
    ),
    Row(
        "job.402",
        "lifecycle",
        START_ADMITTED,
        action="start_extent",
        kind="internal",
        cite="DL-304, DL-349",
        note=(
            "Everything an admitted start does follows from this item: the units it reserves"
            " or takes over, the arm it consumes, the run's start, a box's resets, its move"
            " to STARTING and its RUNNING."
        ),
    ),
    Row(
        "job.403",
        "ADMITTED",
        STARTING,
        "STARTING",
        action="moved",
        cite="SEM-10, DL-242, DL-350",
    ),
    Row(
        "job.404",
        "INACTIVE",
        START,
        guard="start_skips_inactive_member",
        action="skips_in_box",
        kind="internal",
        cite="SEM-33, DL-154",
    ),
    *(
        Row(f"job.{405 + i}", "lifecycle", event, action="moment", kind="internal")
        for i, event in enumerate(MOMENTS)
    ),
    Row(
        f"job.{405 + len(MOMENTS)}",
        "LIVE",
        SETTLE,
        kind="internal",
        cite="DL-340, DL-350",
        note=(
            "The job is live when its move's settlement pops: the move entered STARTING or"
            " RUNNING, or the job started again inside its move's cascade (DL-340). Nothing"
            " settles; a rank the move left still drops."
        ),
    ),
    _never(
        f"job.{406 + len(MOMENTS)}",
        "lifecycle",
        STARTING,
        "Only an admitted start sends it, to its own job, which is ADMITTED until it pops.",
    ),
    *(
        _never(
            f"job.{407 + len(MOMENTS) + i}",
            "ADMITTED",
            event,
            "An admitted start leaves ADMITTED inside its own extent, before any cascade reaches it.",
        )
        for i, event in enumerate((BOX_SET_INACTIVE, START, KILLJOB, ON_NOEXEC))
    ),
)

#: The flag rows, `job.2NN`: 201 to 217 are the flag moves.
_FLAG_ROWS: Final = (
    Row("job.201", "ice_off", ON_ICE, "ice_on", "not_live", "iced", cite="SEM-20, DL-254, DL-285"),
    Row("job.202", "ice_on", OFF_ICE, "ice_off", cite="SEM-20", note="no re-evaluation"),
    Row("job.203", "ice_on", FORCE_STARTJOB, "ice_off", "force_clears", cite="SEM-23, DL-243"),
    Row(
        "job.204", "hold_off", ON_HOLD, "hold_on", "not_live", "held", cite="SEM-21, DL-254, DL-247"
    ),
    Row("job.205", "hold_on", OFF_HOLD, "hold_off", action="released_hold", cite="SEM-21, DL-50"),
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
        "noexec",
        cite="SEM-22, DL-254",
    ),
    Row("job.210", "noexec_on", OFF_NOEXEC, "noexec_off", action="cleared_noexec", cite="DL-243"),
    Row("job.211", "noexec_on", BOX_OFF_NOEXEC, "noexec_off", cite=_DL254),
    Row("job.212", "arm_off", START, "arm_on", "start_arms", cite="SEM-32, DL-54"),
    Row("job.213", "arm_on", ACTUAL_START, "arm_off", cite="DL-54"),
    Row("job.214", "arm_on", DISARM, "arm_off", cite="DL-158"),
    Row("job.215", "arm_on", BOX_RUN_ENDS, "arm_off", cite="DL-54", note="SCHED_DISARM"),
    Row("job.216", "arm_on", READMIT, "arm_off", "readmit_leaves", "waiter_left", cite="DL-257"),
    Row("job.217", "arm_on", KILLJOB, "arm_off", "queued", "report_flag_move", cite="DL-54"),
    # a split source: the second source of job.201, .202, .204, .205, .209, .210, .213, .214, .217
    Row("job.218", "ice_on", ON_ICE, "ice_on", "not_live", "iced_again", cite="SEM-20"),
    Row("job.219", "ice_off", OFF_ICE, "ice_off", cite="SEM-20", note="as job.202"),
    Row("job.220", "hold_on", ON_HOLD, "hold_on", "not_live", "held", cite="SEM-21"),
    Row("job.221", "hold_off", OFF_HOLD, "hold_off", action="released_hold", cite="SEM-21"),
    Row(
        "job.222",
        "noexec_on",
        ON_NOEXEC,
        "noexec_on",
        "noexec_applies",
        "noexec",
        cite="SEM-22, DL-254",
    ),
    Row("job.223", "noexec_off", OFF_NOEXEC, "noexec_off", action="cleared_noexec", cite="DL-243"),
    Row("job.224", "arm_off", ACTUAL_START, "arm_off", cite="DL-54", note="as job.213"),
    Row("job.225", "arm_off", DISARM, "arm_off", cite="DL-158", note="as job.214"),
    Row("job.226", "arm_off", KILLJOB, "arm_off", "queued", "report_flag_move", cite="DL-54"),
    # cells the rules move nothing in
    Row("job.227", "flags", BOX_RUNNING, kind="ignored", note="Not auto_hold, or already held."),
    Row("job.228", "arm_off", BOX_RUN_ENDS, kind="ignored", note="Only an armed member disarms."),
    Row("job.229", "noexec_off", BOX_OFF_NOEXEC, kind="ignored", note="Only a flagged job clears."),
    Row(
        "job.230",
        "arm_off",
        READMIT,
        guard="readmit_leaves",
        action="waiter_left",
        kind="internal",
        cite="DL-257",
        note="No arm to drop: the status move follows.",
    ),
    # a box's ON_NOEXEC on each job it contains: the flag on, from either value as job.209 and
    # job.222, and the hold it supersedes off (job.207); the hold region of a job not held
    # takes no part (statechart-engine §3)
    Row("job.231", "noexec_off", BOX_ON_NOEXEC, "noexec_on", cite=_BOX_NOEXEC, note="as job.209"),
    Row(
        "job.232",
        "hold_on",
        BOX_ON_NOEXEC,
        "hold_off",
        cite=_BOX_NOEXEC,
        note="As job.207: ON_NOEXEC supersedes ON_HOLD, and the job retries its start.",
    ),
    Row("job.233", "noexec_on", BOX_ON_NOEXEC, "noexec_on", cite=_BOX_NOEXEC, note="as job.231"),
)

#: The holding rows, `job.3NN`: 301 to 308 are the holding moves.
_HOLDING_ROWS: Final = (
    Row("job.301", "none", ADMITTED_START, "reserved", "has_demand", cite="DL-120"),
    Row("job.302", "held", ADMITTED_START, "taken over", "takes_over", cite=_DL256),
    Row("job.303", "held", ADMITTED_START, "reserved", "on_held_units", cite=_DL256),
    Row("job.304", "reserved", LEAVE_LIVE, "none", "frees_all", cite="DL-120"),
    Row("job.305", "reserved", LEAVE_LIVE, "held", "keeps_held", cite=_DL256),
    Row("job.306", "reserved", LEAVE_LIVE, "none", "spends", cite="SEM-16, DL-120"),
    Row("job.307", "held", RELEASE_RESOURCE, "none", action="released_held", cite=_DL256),
    Row(
        "job.308",
        "held",
        PERIOD_OPENING,
        "none",
        "left_catalog",
        cite=_DL256,
        false_unreachable="its complement is job.321's, a marked row: the opening addresses only"
        " a removed job",
    ),
    # 309 and 310 are job.302's choice branches; then the cells the rules move nothing in
    Row("job.311", "none", ADMITTED_START, guard="no_demand", kind="ignored", note="No units."),
    Row(
        "job.312",
        "reserved",
        ADMITTED_START,
        "",
        "takes_over",
        "settle_ended_run",
        kind="internal",
        cite=_DL256,
        mark="unreachable",
        note=(
            "A box restarted by its own completion starts the member again before the"
            " member's late settlement. The run that ended settles first, by the end it"
            " made (frees, keeps, or spends); the start is then admitted again, to reserve"
            " or to take over what was kept. Unreachable: the admission the oracle builds"
            " carries no previous end, since job.401 settles the ended run before the"
            " start is admitted."
        ),
    ),
    Row("job.313", "none", LEAVE_LIVE, kind="ignored", note="The run held nothing."),
    Row(
        "job.314",
        "held",
        LEAVE_LIVE,
        kind="ignored",
        mark="unreachable",
        note=(
            "An earlier run's units stay held. Unreachable: a live status never stands on held"
            " units (an admission takes them over first, job.302 and job.303, and an injected"
            " live status makes them the run's, job.319), and a run's settlement is sent once."
        ),
    ),
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

ROWS: Final = (*_STATUS_ROWS, *_FLAG_ROWS, *_HOLDING_ROWS, *_MOMENT_ROWS)
#: The choices: job.119's injected status and job.302's take-over.
CHOICES: Final = (
    Choice(
        "injected status",
        (
            Branch("job.171", "INACTIVE", "injected_inactive", "moved"),
            Branch("job.172", "STARTING", "injected_starting", "moved"),
            Branch("job.173", "RUNNING", "injected_running", "moved"),
            Branch("job.174", "SUCCESS", "injected_success", "moved"),
            Branch("job.175", "FAILURE", "injected_failure", "moved"),
            Branch("job.176", "TERMINATED", action="moved"),
        ),
    ),
    Choice(
        "taken over",
        (Branch("job.309", "reserved", "has_demand"), Branch("job.310", "none")),
    ),
)
