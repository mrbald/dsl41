"""The composite `job` statechart (docs/statechart-engine.md §11, §13, DL-337).

The chart is the one declaration of a job's moves; the oracle drives it and
writes what each fired row reached. These tests read the chart:

- the declaration is well formed, draws no warning, and is registered; its
  marked rows are unreachable cells, each with its reason;
- the rows the cascade and the admission add beside the status, flag and
  holding moves relay an attempt, are moments of the cascade, move an
  admission to STARTING, or settle;
- the generated completeness test (§13) over a declared abstraction;
- a sweep of every configuration the rules can build, every event and the
  payload partitions, read with `Executor.resolve`: no cell falls through or
  is ambiguous, and every row is selected somewhere;
- one cell per status, flag and holding row and each of its sources,
  dispatched with its chained items, fires the row and ends in its target;
- the two orders of a run's settlement in the rows the oracle writes, and
  a status rule that reads a flag running after the flag's move;
- the settlement's policy read from the handle its sender's payload carries,
  an owed settlement before an attempt's admission, and an admitted start's
  moves following from its `start admitted` (DL-349);
- a box's ON_NOEXEC on the jobs it contains, the rows the oracle writes.

The oracle's behaviour over the harness's sample is the goldens'
(tests/oracle_goldens.py); the chart's cells here are built by hand.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41 import job_chart
from dsl41.ir import lower_source
from dsl41.job_chart import (
    ACTUAL_START,
    ADMITTED_START,
    BOX_ENDS,
    BOX_OFF_NOEXEC,
    BOX_ON_NOEXEC,
    BOX_RECOMPUTE,
    BOX_RUN_ENDS,
    BOX_RUNNING,
    BOX_SET_INACTIVE,
    BOX_START,
    COMPLETION_MOMENT,
    DISARM,
    FLAG_MOVED,
    FORCE_STARTJOB,
    JOB,
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
    Admission,
    Admitted,
    Attempt,
    BoxVerdict,
    Deadline,
    FlagMoved,
    Injection,
    JobData,
    Noexec,
    Payload,
    Readmission,
    Release,
    Settlement,
    Terminator,
    Verdict,
    configuration,
    projection,
    row_configuration,
)
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import (
    LIVE,
    CapacityReservation,
    Event,
    JobRuntime,
)
from dsl41.state_machine import (
    HITS_ENV,
    Context,
    Executor,
    Row,
    StatechartError,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = JOB
STATUSES = ("INACTIVE", "QUE_WAIT", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
ROWS = {row.id: row for row in CHART.rows}

#: A move with several sources: the row of its first source, and the rows of
#: its other sources, which take the same cell.
SIBLINGS: dict[str, tuple[str, ...]] = {
    "job.123": ("job.159", "job.160"),
    "job.131": ("job.161", "job.162"),
    "job.132": ("job.163", "job.164"),
    "job.133": ("job.165", "job.166"),
    "job.134": ("job.167", "job.168"),
    "job.136": ("job.169", "job.170"),
    "job.201": ("job.218",),
    "job.202": ("job.219",),
    "job.204": ("job.220",),
    "job.205": ("job.221",),
    "job.209": ("job.222",),
    "job.210": ("job.223",),
    "job.213": ("job.224",),
    "job.214": ("job.225",),
    "job.217": ("job.226",),
}
#: The rows the cascade and the admission add, by why they exist.
RELAYS = ("job.177", "job.178", "job.179")
RELABEL = "job.319"
#: The start of a member that its box restarted before the member's late settlement.
LATE_SETTLEMENT = "job.312"
#: An arm the queued recheck finds already off reports the status move all the same.
FLAG_REPORT = "job.230"
#: The per-job start bound (DL-304).
START_BOUND = "job.100"
#: A box's ON_NOEXEC on each job it contains (DL-347), and the flag row of
#: the job's own ON_NOEXEC each moves as.
BOX_NOEXEC = {"job.231": "job.209", "job.232": "job.207", "job.233": "job.209"}
#: An attempt whose run's settlement is owed, and an admitted start's moves (DL-349).
SETTLE_FIRST = "job.401"
EXTENT = "job.402"
STARTING_ROW = "job.403"
SKIP_IN_BOX = "job.404"
SETTLE_LIVE = "job.418"
#: The rows that admit a start: each sends `start admitted` first.
ADMITTING = ("job.101", "job.102", "job.103", "job.104", "job.124")


@dataclass(frozen=True)
class Policy:
    """A fixed release policy as a tier's handle on a job's units (`Units`),
    for a chart that no oracle drives: `on_success` after a SUCCESS,
    `otherwise` after any other end."""

    on_success: Release = "free"
    otherwise: Release = "free"

    def release(self, success: bool) -> Release:
        return self.on_success if success else self.otherwise

    def consequences(self, ctx: Context, kind: str) -> None:
        """A chart no tier drives: a row sends nothing beyond its own move."""


@dataclass(frozen=True)
class Still:
    """A moment's payload, and `started`'s, for a chart no tier drives: its
    step sends nothing, and its run is `current` or not (DL-350)."""

    current: bool = True
    units: Policy = Policy()

    def step(self, ctx: Context) -> None:
        return None


def _tree() -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Each state's leaves, and each leaf's region: `status`, a flag, or `holding`."""
    leaves: dict[str, tuple[str, ...]] = {}
    region: dict[str, str] = {}

    def walk(state: Any, where: str) -> tuple[str, ...]:
        found: tuple[str, ...] = ()
        for sub in state.regions:
            name = where or sub.name
            for child in sub.states:
                found += walk(child, name if state.name != "flags" else sub.name)
        if not state.regions:
            found = (state.name,)
            region[state.name] = where
        leaves[state.name] = found
        return found

    for top in CHART.root.regions:
        for state in top.states:
            walk(state, top.name)
    return leaves, region


LEAVES, REGION = _tree()


def _leaf(active: frozenset[str], region: str) -> str:
    return next(s for s in active if REGION.get(s) == region)


#: The admitting rows that end in the transient `ADMITTED`, which `starting`
#: (job.403) leaves for STARTING once the admission's units and arm are written.
ADMISSIONS = ("job.102", "job.104", "job.108", "job.124")


def _targets(row: Row) -> set[str]:
    """The leaves a row may end in; a row with no target stays where it is. An
    admission's is STARTING, which it reaches through `ADMITTED` (job.403)."""
    if row.target == "ADMITTED":
        return {"STARTING"}
    choice = {c.name: c for c in CHART.choices}.get(row.target)
    if choice is not None:
        return {branch.target for branch in choice.branches}
    if not row.target:
        return set(LEAVES[row.source])
    return set(LEAVES[row.target])


@pytest.fixture(autouse=True)
def _unrecorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cells here are built by hand, by verb orders and payloads no
    production path makes, so their hits stay out of the session's record:
    the transition gate counts the rows the oracle's own tests fire."""
    monkeypatch.delenv(HITS_ENV, raising=False)


# ---- the declaration


def test_the_chart_is_well_formed_draws_no_warning_and_is_registered() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert len(CHART.rows) == 169
    assert [b.id for c in CHART.choices for b in c.branches] == [
        *(f"job.{n}" for n in range(171, 177)),
        "job.309",
        "job.310",
    ]
    assert not any(state.defer for state in _states(CHART.root))  # §12: no oracle machine defers
    assert alphabet(CHART) == sorted(
        [
            STATUS, STARTJOB, FORCE_STARTJOB, KILLJOB, ON_ICE, OFF_ICE, ON_HOLD, OFF_HOLD,
            ON_NOEXEC, OFF_NOEXEC, DISARM, RELEASE_RESOURCE, TERM_RUN_TIME, RUN_WINDOW, START,
            STARTED, READMIT, ACTUAL_START, ADMITTED_START, LEAVE_LIVE, MEMBER_MOVES,
            COMPLETION_MOMENT, MEMBER_ENDS, BOX_RECOMPUTE, BOX_START, BOX_RUNNING,
            BOX_SET_INACTIVE, BOX_RUN_ENDS, BOX_ENDS, BOX_OFF_NOEXEC, BOX_ON_NOEXEC, PERIOD_OPENING,
            FLAG_MOVED, START_ADMITTED, STARTING, *MOMENTS,
        ]
    )  # fmt: skip


def _states(state: Any) -> Iterator[Any]:
    yield state
    for region in state.regions:
        for sub in region.states:
            yield from _states(sub)


def test_every_guard_and_action_says_what_it_reads() -> None:
    named = {row.guard for row in CHART.rows} | {b.guard for c in CHART.choices for b in c.branches}
    assert named - {""} == set(CHART.guards)
    actions = {row.action for row in CHART.rows} | {
        b.action for c in CHART.choices for b in c.branches
    }
    assert actions - {""} == set(CHART.actions)
    assert not any(state.entry or state.exit for state in _states(CHART.root))
    for name, function in (*CHART.guards.items(), *CHART.actions.items()):
        assert function.__doc__, name


def test_every_marked_row_is_unreachable_with_its_reason() -> None:
    """Each marked row but one is an ignored cell no input builds. job.314, a
    settlement that meets held units, is one of them, marked by DL-353 with
    its own reason: a live status never stands on held units, and a run's
    settlement is sent once. The one that is not ignored is job.312, a start
    on a run's unsettled units: the admission the oracle builds carries no
    previous end, since job.401 settles first."""
    marked = [row for row in CHART.rows if row.mark]
    assert [row.id for row in marked] == [
        "job.182", "job.186", "job.188", "job.190", "job.192", "job.193",
        "job.194", "job.199", "job.312", "job.314", "job.317", "job.318", "job.320", "job.321",
        "job.419", "job.420", "job.421", "job.422", "job.423",
    ]  # fmt: skip
    cells = [row for row in marked if row.id != LATE_SETTLEMENT]
    assert all(row.mark == "unreachable" and row.kind == "ignored" and row.note for row in cells)
    late = ROWS[LATE_SETTLEMENT]
    assert (late.mark, late.kind, late.action) == ("unreachable", "internal", "settle_ended_run")
    assert "carries no previous end" in late.note


# ---- the rows beside the moves


def _moves() -> set[str]:
    """The status, flag and holding moves: each cell's row and its siblings."""
    return {ident for primary in CELLS for ident in (primary, *SIBLINGS.get(primary, ()))}


def test_every_other_row_moves_nothing_relays_an_attempt_or_settles_held_units() -> None:
    moves = _moves()
    others = [row for row in CHART.rows if row.id not in moves]
    assert len(moves) == 83 + 21 and len(others) == 65
    moments = {f"job.{405 + i}": event for i, event in enumerate(MOMENTS)}
    for row in others:
        if row.id in RELAYS:
            assert row.kind == "internal" and row.action == "relay_start"
        elif row.id == FLAG_REPORT:
            assert row.kind == "internal" and row.action == "waiter_left"
        elif row.id == STARTING_ROW:
            assert (row.source, row.event, row.target) == ("ADMITTED", STARTING, "STARTING")
            assert row.action == "moved" and not row.guard
        elif row.id == SKIP_IN_BOX:
            assert (row.source, row.event, row.kind) == ("INACTIVE", START, "internal")
            assert row.guard == "start_skips_inactive_member" and row.action == "skips_in_box"
        elif row.id in moments:
            assert (row.source, row.event, row.kind) == ("lifecycle", moments[row.id], "internal")
            assert row.action == "moment" and not row.guard
        elif row.id == SETTLE_LIVE:
            assert (row.source, row.event, row.kind) == ("LIVE", SETTLE, "internal")
            assert not (row.guard or row.action) and "started again" in row.note
        elif row.id == LATE_SETTLEMENT:
            assert (row.source, row.event, row.kind) == ("reserved", ADMITTED_START, "internal")
            assert row.action == "settle_ended_run" and "settles first" in row.note
        elif row.id == RELABEL:
            assert (row.source, row.event, row.target) == ("held", STATUS, "reserved")
        elif row.id == START_BOUND:
            assert (row.source, row.event, row.kind) == ("IDLE", START, "internal")
            assert row.guard == "start_bounded" and "L010" in row.note
        elif row.id == SETTLE_FIRST:
            assert (row.source, row.event, row.kind) == ("IDLE", START, "internal")
            assert row.guard == "start_settles_first" and row.action == "settles_first"
        elif row.id == EXTENT:
            assert (row.source, row.event, row.kind) == ("lifecycle", START_ADMITTED, "internal")
            assert row.action == "start_extent" and not row.guard
        elif row.id in BOX_NOEXEC:
            own = [ROWS[i] for i in (BOX_NOEXEC[row.id], *SIBLINGS.get(BOX_NOEXEC[row.id], ()))]
            assert (row.event, row.kind) == (BOX_ON_NOEXEC, "external")
            assert not (row.guard or row.action), row.id
            assert row.source in {leaf for r in own for leaf in LEAVES[r.source]}
            assert _targets(row) <= {leaf for r in own for leaf in _targets(r)}
        else:
            assert row.kind == "ignored" and row.note, row.id


@pytest.mark.parametrize("moment", MOMENTS, ids=lambda event: event)
def test_a_moment_with_no_step_of_the_tier_s_is_a_statechart_error(moment: str) -> None:
    """A moment's row runs the step its payload carries; a payload with none
    is a statechart error with the moment named, not a move."""
    executor = Executor()
    executor.restore("j", CHART, configuration("INACTIVE"), JOB_DATA)
    with pytest.raises(StatechartError, match=f"{moment}: a moment with no step"):
        executor.dispatch("j", ChartEvent(moment, Payload()))


def test_only_the_status_rows_of_a_start_report_items() -> None:
    """An item a row's action reports pops before the items a later region's
    rows report, so on `start` and `start admitted` only the status region
    acts: the admitting rows send `start admitted` first, its row the
    admission's moves; the flags and holding rows of a start report nothing
    (DL-349)."""
    for row in CHART.rows:
        if row.event in (START, START_ADMITTED) and row.action:
            assert {REGION[leaf] for leaf in LEAVES[row.source]} == {"status"}, row.id
    acting = {row.id: row.action for row in CHART.rows if row.event == START and row.action}
    assert acting == {
        "job.101": "extent_bypass",
        "job.102": "extent_admit",
        "job.103": "extent_queue",
        "job.104": "extent_on_held",
        "job.106": "moved",
        "job.107": "moved",
        "job.124": "extent_admit",
        SETTLE_FIRST: "settles_first",
        SKIP_IN_BOX: "skips_in_box",
    }
    assert [row.id for row in CHART.rows if row.event == START_ADMITTED] == [EXTENT]


# ---- configurations, data and partitions

#: Every holding a row can carry with its status: a live row's units are its
#: run's. `reserved` beside a status that is not live is the moment between a
#: run's end and its settlement, when the box rules run (`OracleCascade._after`).
_HOLDINGS = {
    s: ("none", "reserved") if s in LIVE else ("none", "held", "reserved") for s in STATUSES
}


def _buildable() -> Iterator[frozenset[str]]:
    """Every configuration the rules can build, and the run's end before its
    settlement. A waiter is never iced: ON_ICE takes a job out of the queue,
    and an iced job never queues."""
    for status in STATUSES:
        for holding in _HOLDINGS[status]:
            for ice, hold, noexec, armed in itertools.product((False, True), repeat=4):
                if status == "QUE_WAIT" and ice:
                    continue
                yield configuration(
                    status, ice=ice, hold=hold, noexec=noexec, armed=armed, holding=holding
                )


def _admitted(holding: str = "none", *, armed: bool = False) -> frozenset[str]:
    """An admitted start's transient configuration, between its admission and
    its STARTING (DL-350); no row projects to it."""
    flags = {"ice_off", "hold_off", "noexec_off", "arm_on" if armed else "arm_off"}
    return frozenset({"job", "lifecycle", "ADMITTED", "flags", *flags, holding})


JOB_DATA = JobData()
BOX_DATA = JobData(box=True)
NO_CATALOG = JobData(catalog=False)
MEMBER_DATA = JobData(job_terminator=True, auto_hold=True)
_RELEASES: tuple[Release, ...] = ("free", "keep", "spend")

#: Who makes an attempt: a wake or a retry, then the three relays as
#: `relay_start` passes them on (a timer's attempt is stale, or a rescan).
_WHO = (
    Attempt(),
    Attempt(explicit=True, scheduled=True),
    Attempt(force=True, explicit=True, scheduled=True),
    Attempt(explicit=True, scheduled=True, stale=True),
    Attempt(explicit=True, scheduled=True, rescan=True),
)


def _attempts() -> list[Attempt]:
    found = [
        replace(who, dated=dated, box=box, condition=condition, window=window, admissible=fits)
        for who in _WHO
        for dated in (False, True)
        for box in ("none", "running", "idle", "ran", "iced_out")
        for condition in (True, False)
        for window in ("inside", "defer", "skip")
        for fits in (True, False)
    ]
    return (
        found
        + [
            variant
            for who in _WHO
            for variant in (
                replace(who, box_noexec=True, demand=False),
                replace(who, settlement_owed=True),
            )
        ]
        + [
            Attempt(decided=True, window=window, box=box)
            for window in ("inside", "defer", "skip")
            for box in ("none", "running", "idle", "ran", "iced_out")
        ]
    )


_NOEXECS = [Noexec(busy, change) for busy in (False, True) for change in (True, False)]
_STEPS = [Verdict(None), Verdict("SUCCESS"), Verdict("FAILURE")]
#: The payload partitions of each event whose guards read one (§13). A door
#: step and an idle recompute have their own payload types, so no payload an
#: event cannot carry selects a marked row.
PARTITIONS: dict[str, list[Any]] = {
    STATUS: [Injection(status=s) for s in STATUSES if s != "QUE_WAIT"]
    + [Injection(success=True), Injection(success=False)],
    START: _attempts(),
    STARTJOB: [Attempt()],
    FORCE_STARTJOB: [Attempt()],
    RUN_WINDOW: [Attempt(), Attempt(stale=True)],
    READMIT: [
        Readmission(box=box, keep_stopped=keep, admissible=fits, recheck_fails=fails)
        for box in ("none", "running", "idle")
        for keep in (False, True)
        for fits in (True, False)
        for fails in (False, True)
    ],
    ON_NOEXEC: _NOEXECS,
    FLAG_MOVED: [FlagMoved("ON_ICE"), FlagMoved("KILLJOB"), FlagMoved("readmit")]
    + [FlagMoved("ON_NOEXEC", noexec) for noexec in _NOEXECS],
    TERM_RUN_TIME: [Deadline(True), Deadline(False)],
    MEMBER_MOVES: _STEPS,
    COMPLETION_MOMENT: _STEPS,
    BOX_RECOMPUTE: [BoxVerdict(t, o) for t in (None, "SUCCESS", "FAILURE") for o in (False, True)],
    MEMBER_ENDS: [Terminator(True), Terminator(False)],
    ADMITTED_START: [Admission(held, demand) for held in (False, True) for demand in (True, False)],
    LEAVE_LIVE: [
        Settlement(success, release) for success in (True, False) for release in _RELEASES
    ],
    START_ADMITTED: [Admitted(verdict) for verdict in ("bypass", "queue", "admit", "on_held")],
    STARTED: [Still(True), Still(False)],
    STARTING: [Still()],
    **{moment: [Still()] for moment in MOMENTS},
}


def test_the_sweep_of_every_buildable_cell_raises_no_selection_error_and_selects_every_row() -> (
    None
):
    """Every configuration of `_buildable`; a job, a box, a member with
    job_terminator and auto_hold, and a job with no catalog entry; every event
    and its partitions (a start's fewer for the last two). `Executor.resolve`
    raises no `StatechartError` in any cell: no cell falls through, and none
    is ambiguous. Every row of the chart is selected in some cell, and no
    cell selects two rows that both carry an action: an event acts in one
    region only, so no region's items pop ahead of another's (the region
    condition, DL-350). It does not compare a cell with the rule code:
    the replays below do that."""
    executor = Executor()
    selected: set[str] = set()
    acting: list[tuple[str, tuple[str, ...]]] = []
    # a row whose target is a choice acts when any branch it may take does
    branching = {choice.name for choice in CHART.choices if any(b.action for b in choice.branches)}

    def acts(row: str) -> bool:
        return bool(ROWS[row].action) or ROWS[row].target in branching

    cells = 0
    kinds = (("job", JOB_DATA), ("box", BOX_DATA), ("member", MEMBER_DATA), ("none", NO_CATALOG))
    admitted = [
        _admitted(holding, armed=armed)
        for holding in _HOLDINGS["INACTIVE"]
        for armed in (False, True)
    ]
    for index, active in enumerate([*_buildable(), *admitted]):
        for kind, data in kinds:
            key = (index, kind)
            executor.restore(key, CHART, active, data)
            for event in alphabet(CHART):
                payloads = PARTITIONS.get(event, [None])
                if kind in ("member", "none") and event == START:
                    payloads = list(_WHO)
                for payload in payloads:
                    rows = executor.resolve(key, ChartEvent(event, payload)).rows
                    selected.update(rows)
                    if sum(1 for row in rows if acts(row)) > 1:
                        acting.append((event, rows))
                    cells += 1
    # the bound reads the item's ancestry, which a read never has: the
    # ancestry test reaches it
    assert selected == set(ROWS) - {START_BOUND}
    assert acting == []
    assert cells > 250_000


@pytest.mark.parametrize(
    ("active", "data", "attempt", "rows"),
    [
        (
            configuration("INACTIVE", noexec=True),
            JOB_DATA,
            Attempt(),
            ["job.101", EXTENT, "job.224"],
        ),
        (configuration("INACTIVE"), JOB_DATA, Attempt(), ["job.102", EXTENT, "job.301", "job.224"]),
        (
            configuration("INACTIVE"),
            BOX_DATA,
            Attempt(demand=False),
            ["job.124", EXTENT, "job.311", "job.224"],
        ),
        (configuration("INACTIVE"), JOB_DATA, Attempt(admissible=False), ["job.103", EXTENT]),
        (
            configuration("FAILURE", holding="held"),
            JOB_DATA,
            Attempt(force=True),
            ["job.104", EXTENT, "job.303", "job.224"],
        ),
        (configuration("INACTIVE"), JOB_DATA, Attempt(settlement_owed=True), [SETTLE_FIRST]),
    ],
    ids=["bypass", "admit", "admit a box", "queue", "on held units", "settle first"],
)
def test_an_admitting_row_sends_start_admitted_before_the_admissions_moves(
    active: frozenset[str], data: JobData, attempt: Attempt, rows: list[str]
) -> None:
    """Each admitting row's status move comes first, then `start admitted`,
    whose row sends the admission's moves: they are its descendants. An owed
    settlement is read past the bound and moves nothing (job.401)."""
    executor = Executor()
    executor.restore("j", CHART, active, data)
    outcomes = executor.dispatch("j", ChartEvent(START, attempt))
    assert [ident for outcome in outcomes for ident in outcome.rows] == rows
    depths = [outcome.depth for outcome in outcomes]
    assert depths == [0, 1, *([2] * (len(rows) - 2))][: len(rows)]


@pytest.mark.parametrize(
    ("ancestry", "verdict"),
    [
        ((), "admit"),
        ((("j", START_ADMITTED),), "admit"),
        ((("j", START_ADMITTED), ("k", START_ADMITTED), ("j", START)), "admit"),
        ((("j", START_ADMITTED), ("j", START), ("j", START_ADMITTED)), "bounded"),
        ((("j", START_ADMITTED),) * 3, "bounded"),
    ],
    ids=["root", "one below", "another job's", "at the bound", "past it"],
)
def test_the_start_bound_counts_the_job_s_admitted_starts_in_the_ancestry(
    ancestry: tuple[tuple[str, str], ...], verdict: str
) -> None:
    """job.100 (DL-304, DL-350): an attempt that descends from
    `MAX_NESTED_STARTS` admitted starts of its own job is refused; another
    job's admitted start, and the job's own attempts, do not count."""
    ctx = Context(
        key="j",
        event=ChartEvent(START, Attempt()),
        active=configuration("INACTIVE"),
        data=JOB_DATA,
        ancestry=ancestry,
    )
    assert job_chart.start_verdict(ctx) == verdict
    assert job_chart.start_bounded(ctx) == (verdict == "bounded")


# ---- the generated completeness test (§13)


def _witness(active: frozenset[str], data: JobData) -> sc.Builder:
    def build() -> tuple[Executor, str]:
        executor = Executor()
        executor.restore("j", CHART, active, data)
        return executor, "j"

    return build


def _witnesses() -> dict[str, sc.Builder]:
    """A job and a box in each status with no flag and no units; each flag on;
    every flag on; held and reserved units, and a run ended before its
    settlement; a member with job_terminator and auto_hold; a job with no
    catalog entry, and one that holds units. A settlement's policy is its
    payload's (`Settlement.release`)."""
    found: dict[str, sc.Builder] = {}
    for status in STATUSES:
        found[f"job {status}"] = _witness(configuration(status), JOB_DATA)
        found[f"box {status}"] = _witness(configuration(status), BOX_DATA)
    for flag in ("ice", "hold", "noexec", "armed"):
        found[f"job INACTIVE {flag}"] = _witness(
            configuration("INACTIVE", **{flag: True}), JOB_DATA
        )
    found["job QUE_WAIT hold"] = _witness(configuration("QUE_WAIT", hold=True), JOB_DATA)
    every = {"ice": True, "hold": True, "noexec": True, "armed": True}
    found["job FAILURE every flag held"] = _witness(
        configuration("FAILURE", **every, holding="held"), JOB_DATA
    )
    found["box SUCCESS held"] = _witness(configuration("SUCCESS", holding="held"), BOX_DATA)
    found["job RUNNING reserved"] = _witness(configuration("RUNNING", holding="reserved"), JOB_DATA)
    found["job TERMINATED reserved"] = _witness(
        configuration("TERMINATED", holding="reserved"), JOB_DATA
    )
    found["job INACTIVE reserved"] = _witness(
        configuration("INACTIVE", holding="reserved"), JOB_DATA
    )
    found["box STARTING reserved"] = _witness(
        configuration("STARTING", holding="reserved"), BOX_DATA
    )
    found["member RUNNING"] = _witness(configuration("RUNNING"), MEMBER_DATA)
    found["member INACTIVE"] = _witness(configuration("INACTIVE"), MEMBER_DATA)
    found["no catalog INACTIVE"] = _witness(configuration("INACTIVE"), NO_CATALOG)
    found["no catalog FAILURE held"] = _witness(
        configuration("FAILURE", holding="held"), NO_CATALOG
    )
    found["job ADMITTED"] = _witness(_admitted(), JOB_DATA)
    found["box ADMITTED reserved"] = _witness(_admitted("reserved"), BOX_DATA)
    return found


def _handled(payload: Any) -> Any:
    """The payload with a handle on the job's units (`Policy()`, which frees):
    a settlement the cell's rows send must know its policy (DL-349). The
    handle is not printed, so the cell's label keeps."""
    if isinstance(payload, Admitted):
        return replace(payload, admission=_handled(payload.admission), units=Policy())
    if isinstance(payload, Payload):
        return replace(payload, units=Policy())
    return payload


#: The events whose rows read no fact: a bare payload carries the handle.
_BARE = {sc.ANY: Payload(units=Policy())}

ABSTRACTION = sc.Abstraction(
    witnesses=_witnesses(),
    partitions={event: _BARE for event in alphabet(CHART) if event not in PARTITIONS}
    | {
        event: {repr(payload): _handled(payload) for payload in payloads}
        for event, payloads in PARTITIONS.items()
        if event != START
    }
    | {
        START: {
            repr(payload): _handled(payload)
            for payload in (
                *_WHO,
                Attempt(box="idle"),
                Attempt(explicit=True, box="ran"),
                Attempt(scheduled=True, dated=True, condition=False),
                Attempt(dated=True),
                Attempt(window="defer"),
                Attempt(window="skip"),
                Attempt(window="skip", box="running"),
                Attempt(box_noexec=True),
                Attempt(settlement_owed=True),
                Attempt(force=True),
                Attempt(admissible=False),
                Attempt(window="skip", box="iced_out"),
                Attempt(decided=True, window="skip", box="running"),
                Attempt(decided=True, window="defer", box="running"),
                Attempt(decided=True),
            )
        }
    },
)


def test_the_completeness_table_matches_the_declaration_in_every_cell() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert len(table) > 3000
    # pinned from the rule code, not from the rows; a
    # status move that reads a flag follows it as its own item, so a cell's
    # first outcome shows the flag move alone
    assert table[("job RUNNING reserved", KILLJOB, sc.ANY)] == "job.114"
    assert table[("job QUE_WAIT", KILLJOB, sc.ANY)] == "job.226"
    assert table[("job QUE_WAIT", FLAG_MOVED, repr(FlagMoved("KILLJOB")))] == "job.111"
    assert table[("job INACTIVE armed", KILLJOB, sc.ANY)] == "job.137"
    assert table[("job STARTING", ON_ICE, sc.ANY)] == "job.148"
    assert table[("job QUE_WAIT hold", ON_ICE, sc.ANY)] == "job.201"
    assert table[("job INACTIVE ice", FORCE_STARTJOB, repr(Attempt()))] == "job.178 + job.203"
    assert table[("job INACTIVE", START, repr(Attempt(window="defer")))] == "ignored job.180"
    # a skip of an INACTIVE member of a running box has no move to ride (DL-154)
    assert table[("job INACTIVE", START, repr(Attempt(window="skip", box="running")))] == (
        "job.404"
    )
    # a plain attempt meets the box gate before the window
    assert table[("job SUCCESS", START, repr(Attempt(window="skip", box="iced_out")))] == (
        "ignored job.180"
    )
    # a box start's window decision reads the window alone (DL-246)
    decided = Attempt(decided=True, window="skip", box="running")
    assert table[("job INACTIVE ice", START, repr(decided))] == "job.404"
    assert table[("job SUCCESS", START, repr(decided))] == "job.107"
    assert table[("job INACTIVE", START, repr(Attempt(decided=True)))] == "ignored job.180"
    # an owed settlement is read past the bound, before the bypass (DL-340)
    assert table[("job INACTIVE", START, repr(Attempt(settlement_owed=True)))] == "job.401"
    assert table[("job RUNNING", START, repr(Attempt(settlement_owed=True)))] == "ignored job.180"
    assert table[("job INACTIVE noexec", START, repr(Attempt(settlement_owed=True)))] == ("job.401")
    assert table[("job INACTIVE", START, repr(Attempt()))] == "job.102"
    assert table[("job SUCCESS", START_ADMITTED, repr(Admitted("bypass")))] == "job.402"
    # ON_NOEXEC bypasses before held units are read (SEM-22, `start_verdict`)
    assert table[("job FAILURE every flag held", START, repr(Attempt(force=True)))] == "job.101"
    assert table[("job FAILURE every flag held", START, repr(Attempt()))] == "ignored job.180"
    assert table[("job FAILURE every flag held", ON_NOEXEC, repr(Noexec()))] == "job.156"
    # `started` moves only the run the admission began (`Started.current`)
    assert table[("box RUNNING", STARTED, repr(Still(True)))] == "ignored job.183"
    assert table[("box STARTING", STARTED, repr(Still(True)))] == "job.125"
    assert table[("job STARTING", STARTED, repr(Still(True)))] == "job.105"
    assert table[("job STARTING", STARTED, repr(Still(False)))] == "ignored job.183"
    assert table[("job ADMITTED", STARTING, repr(Still()))] == "job.403"
    assert table[("job INACTIVE", STARTING, repr(Still()))] == "ignored job.419"
    # a job started again inside its move's cascade settles nothing (DL-340)
    assert table[("job RUNNING", SETTLE, repr(Still()))] == "job.418"
    assert table[("job TERMINATED reserved", SETTLE, repr(Still()))] == "job.406"
    assert table[("job FAILURE every flag held", STATUS, repr(Injection("STARTING")))] == (
        "job.119 + job.319"
    )
    assert table[("job TERMINATED reserved", LEAVE_LIVE, repr(Settlement(False, "keep")))] == (
        "job.305"
    )
    assert table[("job RUNNING reserved", LEAVE_LIVE, repr(Settlement(True, "keep")))] == "job.305"
    assert table[("job RUNNING reserved", LEAVE_LIVE, repr(Settlement(True, "spend")))] == (
        "job.306"
    )
    # the declaration's cell of job.312, which the oracle never builds (the row
    # is marked unreachable): a start that meets a run's unsettled units
    assert table[("job INACTIVE reserved", ADMITTED_START, repr(Admission(False, True)))] == (
        "job.312"
    )
    assert table[("job INACTIVE reserved", ADMITTED_START, repr(Admission(True, True)))] == (
        "ignored job.320"
    )
    assert table[("no catalog FAILURE held", PERIOD_OPENING, sc.ANY)] == "job.308"
    assert table[("box SUCCESS held", PERIOD_OPENING, sc.ANY)] == "ignored job.321"
    assert table[("member RUNNING", BOX_ENDS, sc.ANY)] == "job.116"
    assert table[("job RUNNING reserved", BOX_ENDS, sc.ANY)] == "ignored job.197"
    assert table[("member INACTIVE", BOX_RUNNING, sc.ANY)] == "job.208"
    # a box's ON_NOEXEC on a job it contains: the flag on from either value,
    # and the hold off; the hold region of a job not held takes no part
    assert table[("job SUCCESS", BOX_ON_NOEXEC, sc.ANY)] == "job.231"
    assert table[("job INACTIVE noexec", BOX_ON_NOEXEC, sc.ANY)] == "job.233"
    assert table[("job INACTIVE hold", BOX_ON_NOEXEC, sc.ANY)] == "job.231 + job.232"
    assert table[("job FAILURE every flag held", BOX_ON_NOEXEC, sc.ANY)] == "job.232 + job.233"
    assert table[("job INACTIVE", BOX_RUNNING, sc.ANY)] == "ignored job.227"


# ---- one cell per move and source

type Cell = Callable[[str], tuple[frozenset[str], JobData, ChartEvent]]


def _status(
    event: ChartEvent, data: JobData = JOB_DATA, holding: str = "none", **flags: bool
) -> Cell:
    """A cell whose source is a status."""

    def cell(source: str) -> tuple[frozenset[str], JobData, ChartEvent]:
        return configuration(source, holding=holding, **flags), data, event  # type: ignore[arg-type]

    return cell


def _flag(event: ChartEvent, status: str = "INACTIVE", data: JobData = JOB_DATA) -> Cell:
    """A cell whose source is a flag's state."""

    def cell(source: str) -> tuple[frozenset[str], JobData, ChartEvent]:
        name, value = source.split("_")
        flag = "armed" if name == "arm" else name
        return configuration(status, **{flag: value == "on"}), data, event  # type: ignore[arg-type]

    return cell


def _holding(event: ChartEvent, status: str, data: JobData = JOB_DATA) -> Cell:
    """A cell whose source is a holding state."""

    def cell(source: str) -> tuple[frozenset[str], JobData, ChartEvent]:
        return configuration(status, holding=source), data, event  # type: ignore[arg-type]

    return cell


def _start(**facts: Any) -> ChartEvent:
    return ChartEvent(STARTJOB, Attempt(**facts))


def _box(target: str | None, override: bool) -> BoxVerdict:
    return BoxVerdict(target, override)  # type: ignore[arg-type]


def _step(target: str | None) -> Verdict:
    return Verdict(target)  # type: ignore[arg-type]


CELLS: dict[str, Cell] = {
    "job.101": _status(_start(), noexec=True),
    "job.102": _status(_start()),
    "job.103": _status(_start(admissible=False)),
    "job.104": _status(ChartEvent(FORCE_STARTJOB, Attempt()), holding="held"),
    "job.105": _status(ChartEvent(STARTED, Still())),
    "job.106": _status(_start(window="skip")),
    "job.107": _status(_start(window="skip", box="running")),
    "job.108": _status(ChartEvent(READMIT, Readmission())),
    "job.109": _status(ChartEvent(READMIT, Readmission(box="idle"))),
    "job.110": _status(ChartEvent(READMIT, Readmission(recheck_fails=True))),
    "job.111": _status(ChartEvent(KILLJOB)),
    "job.112": _status(ChartEvent(ON_ICE)),
    "job.113": _status(ChartEvent(ON_NOEXEC, Noexec())),
    "job.114": _status(ChartEvent(KILLJOB)),
    "job.115": _status(ChartEvent(TERM_RUN_TIME, Deadline())),
    "job.116": _status(ChartEvent(BOX_ENDS), MEMBER_DATA),
    "job.117": _status(ChartEvent(STATUS, Injection(success=True))),
    "job.118": _status(ChartEvent(STATUS, Injection(success=False))),
    "job.119": _status(ChartEvent(STATUS, Injection("RUNNING"))),
    "job.120": _status(ChartEvent(STATUS, Injection("INACTIVE"))),
    "job.121": _status(ChartEvent(ON_NOEXEC, Noexec())),
    "job.122": _status(ChartEvent(BOX_START)),
    "job.123": _status(ChartEvent(BOX_SET_INACTIVE)),
    "job.124": _status(_start(), BOX_DATA),
    "job.125": _status(ChartEvent(STARTED, Still()), BOX_DATA),
    "job.126": _status(ChartEvent(MEMBER_MOVES, _step("SUCCESS")), BOX_DATA),
    "job.127": _status(ChartEvent(MEMBER_MOVES, _step("FAILURE")), BOX_DATA),
    "job.128": _status(ChartEvent(COMPLETION_MOMENT, _step("SUCCESS")), BOX_DATA),
    "job.129": _status(ChartEvent(COMPLETION_MOMENT, _step("FAILURE")), BOX_DATA),
    "job.130": _status(ChartEvent(MEMBER_ENDS, Terminator()), BOX_DATA),
    "job.131": _status(ChartEvent(BOX_RECOMPUTE, _box("SUCCESS", True)), BOX_DATA),
    "job.132": _status(ChartEvent(BOX_RECOMPUTE, _box("FAILURE", True)), BOX_DATA),
    "job.133": _status(ChartEvent(BOX_RECOMPUTE, _box("SUCCESS", False)), BOX_DATA),
    "job.134": _status(ChartEvent(BOX_RECOMPUTE, _box("FAILURE", False)), BOX_DATA),
    "job.135": _status(ChartEvent(STATUS, Injection("INACTIVE")), BOX_DATA),
    "job.136": _status(ChartEvent(ON_NOEXEC, Noexec()), BOX_DATA),
    **{f"job.{100 + n}": _status(ChartEvent(KILLJOB)) for n in range(37, 41)},
    **{f"job.{100 + n}": _status(_start(stale=True)) for n in range(41, 48)},
    "job.148": _status(ChartEvent(ON_ICE)),
    "job.149": _status(ChartEvent(ON_ICE)),
    "job.150": _status(ChartEvent(ON_HOLD)),
    "job.151": _status(ChartEvent(ON_HOLD)),
    **{
        f"job.{100 + n}": _status(ChartEvent(ON_NOEXEC, Noexec(tree_busy=True)), BOX_DATA)
        for n in range(52, 59)
    },
    "job.201": _flag(ChartEvent(ON_ICE)),
    "job.202": _flag(ChartEvent(OFF_ICE)),
    "job.203": _flag(ChartEvent(FORCE_STARTJOB, Attempt())),
    "job.204": _flag(ChartEvent(ON_HOLD)),
    "job.205": _flag(ChartEvent(OFF_HOLD)),
    "job.206": _flag(ChartEvent(FORCE_STARTJOB, Attempt())),
    "job.207": _flag(ChartEvent(ON_NOEXEC, Noexec())),
    "job.208": _flag(ChartEvent(BOX_RUNNING), data=MEMBER_DATA),
    "job.209": _flag(ChartEvent(ON_NOEXEC, Noexec())),
    "job.210": _flag(ChartEvent(OFF_NOEXEC)),
    "job.211": _flag(ChartEvent(BOX_OFF_NOEXEC)),
    "job.212": _flag(_start(dated=True, condition=False)),
    "job.213": _flag(_start()),
    "job.214": _flag(ChartEvent(DISARM)),
    "job.215": _flag(ChartEvent(BOX_RUN_ENDS)),
    "job.216": _flag(ChartEvent(READMIT, Readmission(recheck_fails=True)), "QUE_WAIT"),
    "job.217": _flag(ChartEvent(KILLJOB), "QUE_WAIT"),
    "job.301": _holding(_start(), "INACTIVE"),
    "job.302": _holding(_start(), "FAILURE"),
    "job.303": _holding(ChartEvent(FORCE_STARTJOB, Attempt()), "FAILURE"),
    # the tier sends a run's settlement after its move's box rules (DL-350)
    "job.304": _holding(ChartEvent(LEAVE_LIVE, Settlement(units=Policy())), "TERMINATED"),
    "job.305": _holding(
        ChartEvent(LEAVE_LIVE, Settlement(units=Policy(otherwise="keep"))), "TERMINATED"
    ),
    "job.306": _holding(
        ChartEvent(LEAVE_LIVE, Settlement(units=Policy(otherwise="spend"))), "TERMINATED"
    ),
    "job.307": _holding(ChartEvent(RELEASE_RESOURCE), "FAILURE"),
    "job.308": _holding(ChartEvent(PERIOD_OPENING), "FAILURE", NO_CATALOG),
}


def test_every_move_from_every_source_ends_in_its_target() -> None:
    """For each status, flag and holding move and each of its sources, one
    cell dispatched with its chained items: the row of that source fires, and
    its region ends in one of the row's targets. A move with several sources
    has a row per source, which take the same cell."""
    assert len(CELLS) == 83
    checked = 0
    for primary, cell in CELLS.items():
        for ident in (primary, *SIBLINGS.get(primary, ())):
            row = ROWS[ident]
            for source in sorted(leaf for leaf in LEAVES[row.source] if leaf != "ADMITTED"):
                active, data, event = cell(source)
                assert _leaf(active, REGION[source]) == source, (ident, source)
                executor = Executor()
                executor.restore("j", CHART, active, data)
                outcomes = _advance(executor, "j", event)
                fired = [fired for outcome in outcomes for fired in outcome.rows]
                assert ident in fired, (ident, source, fired)
                after = _leaf(executor.active("j"), REGION[source])
                assert after in _targets(row), (ident, source, after)
                checked += 1
    assert checked == 162


def _advance(executor: Executor, job: str, event: ChartEvent) -> list[Any]:
    """Dispatch `event`; an admission then leaves `ADMITTED` for STARTING
    (job.403), as the tier's extent sends `starting` once the admission's
    units and arm are written."""
    outcomes = executor.dispatch(job, event)
    if "ADMITTED" in executor.active(job):
        outcomes += executor.dispatch(job, ChartEvent(STARTING, Still()))
    return outcomes


# ---- the projection


def test_a_configuration_projects_to_the_row_it_stands_for() -> None:
    for active in _buildable():
        status, ice, hold, noexec, armed, holding = projection(active)
        assert (
            configuration(
                status,  # type: ignore[arg-type]
                ice=ice,
                hold=hold,
                noexec=noexec,
                armed=armed,
                holding=holding,  # type: ignore[arg-type]
            )
            == active
        )
    unit = CapacityReservation(bucket="r:X", units=1, release_policy="never")
    assert projection(row_configuration(JobRuntime(status="RUNNING", reservations=(unit,)))) == (
        "RUNNING", False, False, False, False, "reserved",
    )  # fmt: skip
    assert projection(row_configuration(JobRuntime(status="FAILURE", reservations=(unit,)))) == (
        "FAILURE", False, False, False, False, "held",
    )  # fmt: skip


def _rows(outcomes: list[Any]) -> list[tuple[str, str]]:
    """The (instance, row) of every row run, in the order the rows ran."""
    return [(o.key, ident) for o in outcomes for ident in o.rows]


def test_a_settlement_reads_its_policy_once_from_its_handle() -> None:
    """`leave STARTING or RUNNING`'s payload knows its end when it is built;
    its policy is given, or read from the handle on its first read and kept
    (DL-349). With neither, the run frees. Two settlements are equal when
    their end and their policy are."""
    reads: list[bool] = []

    class Counted:
        def release(self, success: bool) -> Release:
            reads.append(success)
            return "keep"

    lazy = Settlement(True, units=Counted())
    assert repr(lazy) == "Settlement(success=True)" and reads == []
    assert lazy.release == "keep" and lazy.release == "keep" and reads == [True]
    assert repr(lazy) == "Settlement(success=True, release='keep')"
    assert lazy == Settlement(True, "keep") and hash(lazy) == hash(Settlement(True, "keep"))
    assert Settlement(False, "free") != Settlement(False, "spend")
    assert Settlement(False, "free") != (False, "free")
    # neither a release nor a handle: the policy is unknown, and reading it raises
    with pytest.raises(StatechartError, match="no release and no handle"):
        _ = Settlement().release
    assert repr(Settlement()) == "Settlement(success=False)"
    assert Admission() == Admission(previous=Settlement(True, "keep"))  # reads no settlement


@pytest.mark.parametrize(
    ("admission", "row"),
    [
        (Admission(previous=Settlement(True, "keep")), "job.305"),
        (Admission(previous=Settlement(True), units=Policy("spend", "free")), "job.306"),
        (Admission(), None),
    ],
    ids=["previous given", "previous read from the handle", "neither"],
)
def test_a_late_settlement_at_a_restart_needs_its_end_s_policy(
    admission: Admission, row: str | None
) -> None:
    """job.312's action settles the run that ended by the end it made, then
    admits the start again: with the admission's handle the policy is read
    for that end; without one, the previous settlement must carry it, and
    with neither the read raises (DL-349). The row is marked unreachable, so
    its action is called on its own and the chart never fires it here."""
    active = configuration("INACTIVE", holding="reserved")
    ctx = Context("j", ChartEvent(ADMITTED_START, admission), active, JOB_DATA)
    effects = job_chart.settle_ended_run(ctx)
    (key, settlement), (again_key, again) = effects.send
    assert (key, again_key, settlement.name) == ("j", "j", LEAVE_LIVE)
    assert again.name == ADMITTED_START and again.payload == admission
    assert repr(again.payload.previous) == repr(Settlement())  # the ended run's is spent
    executor = Executor()
    executor.restore("j", CHART, active, JOB_DATA)
    if row is None:
        with pytest.raises(StatechartError, match="no release and no handle"):
            executor.dispatch("j", settlement)
        return
    assert [ident for outcome in executor.dispatch("j", settlement) for ident in outcome.rows] == [
        row
    ]


def test_an_injected_live_status_keeps_the_run_s_units_and_settles_them_at_its_end() -> None:
    """An injected STARTING on a run settles nothing (its choice branch has no
    settlement, as the settlement skips a move into a live status); held units
    an injected live status meets become the run's (`job.319`) and are
    released by policy when it ends."""
    executor = Executor()
    executor.restore("j", CHART, configuration("RUNNING", holding="reserved"), JOB_DATA)
    outcomes = executor.dispatch("j", ChartEvent(STATUS, Injection("STARTING")))
    assert _rows(outcomes) == [("j", "job.119")]
    executor.restore("k", CHART, configuration("FAILURE", holding="held"), JOB_DATA)
    executor.dispatch("k", ChartEvent(STATUS, Injection("RUNNING")))
    assert projection(executor.active("k"))[-1] == "reserved"
    keep = Policy(otherwise="keep")
    outcomes = executor.dispatch("k", ChartEvent(STATUS, Injection(success=False, units=keep)))
    # the settlement is the tier's, after the move's box rules (DL-350)
    outcomes += executor.dispatch("k", ChartEvent(LEAVE_LIVE, Settlement(False, units=keep)))
    assert _rows(outcomes) == [("k", "job.118"), ("k", "job.305")]
    assert projection(executor.active("k"))[::5] == ("FAILURE", "held")


# ---- the two settlement orders (a move on its own, the batch INACTIVE path)

T0 = datetime(2026, 7, 1, 8, 0)
#: A box `ob` whose member `om` holds the only unit of R; `ox` needs R and
#: waits for `ob`'s SUCCESS.
_ORDER_JIL = (
    "insert_resource: R\nres_type: R\namount: 1\n\ninsert_job: ob\njob_type: b\n\n"
    "insert_job: om\njob_type: c\ncommand: m\nmachine: m1\nbox_name: ob\n"
    "resources: (R, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: ox\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ob)\n"
    "resources: (R, QUANTITY=1, FREE=Y)\n"
)
#: A box `ob` that holds a unit of R, with a member `om` that holds another.
_BATCH_JIL = (
    "insert_resource: R\nres_type: R\namount: 2\n\n"
    "insert_job: ob\njob_type: b\nresources: (R, QUANTITY=1, FREE=Y)\n\n"
    "insert_job: om\njob_type: c\ncommand: m\nmachine: m1\nbox_name: ob\n"
    "resources: (R, QUANTITY=1, FREE=Y)\n"
)


def _input(
    jil: str,
    setup: list[dict[str, Any]],
    event: dict[str, Any],
    rows: list[tuple[str, str]],
) -> Oracle:
    """One input on an oracle, its setup fed first, one minute apart. `rows`,
    the hook's record, starts at the input."""
    oracle = Oracle(lower_source(jil))
    for minute, step in enumerate(setup, 1):
        oracle.feed(Event(at=T0 + timedelta(minutes=minute), **step))
    del rows[:]
    oracle.feed(Event(at=T0 + timedelta(minutes=len(setup) + 1), **event))
    return oracle


def _chart_rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every row the oracle's hook writes, as (job, row), in order."""
    seen: list[tuple[str, str]] = []
    perform = Oracle._perform

    def spy(self: Oracle, outcome: Any) -> None:
        seen.extend((str(outcome.key).partition(":")[2], row) for row in outcome.rows)
        perform(self, outcome)

    monkeypatch.setattr(Oracle, "_perform", spy)
    return seen


_START_OB = [{"kind": "STARTJOB", "payload": {"job": "ob"}}]


def test_a_move_on_its_own_settles_after_the_box_rules_and_before_its_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """om's SUCCESS folds ob, ob's SUCCESS wakes ox, which finds R still held
    and queues; om's units go back only then, and the queue admits ox: the
    move's `settle` follows its `box rules`, and its `wake` the settlement
    (DL-350)."""
    event = {"kind": "STATUS", "payload": {"job": "om", "exit_code": 0}}
    rows = _chart_rows(monkeypatch)
    _input(_ORDER_JIL, _START_OB, event, rows)
    order = [("om", "job.117"), ("ob", "job.128"), ("ox", "job.103"), ("om", "job.304")]
    assert [row for row in rows if row in order] == order
    assert rows.index(("ox", "job.108")) > rows.index(("om", "job.304"))


def test_the_batch_inactive_path_settles_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box set INACTIVE releases its own units before its members' rows
    move: its rows (`job.135`, `job.159`) send the settlement first."""
    event = {"kind": "STATUS", "payload": {"job": "ob", "status": "INACTIVE"}}
    rows = _chart_rows(monkeypatch)
    _input(_BATCH_JIL, _START_OB, event, rows)
    moves = [row for row in rows if row[1] in _moves()]
    assert moves == [("ob", "job.135"), ("ob", "job.305"), ("om", "job.159"), ("om", "job.305")]
    written = [row for row in rows if row[1] in ("job.135", "job.159", "job.305")]
    assert written == [("ob", "job.135"), ("ob", "job.305"), ("om", "job.159"), ("om", "job.305")]


# ---- a read after a write across regions is its own item


@pytest.mark.parametrize(
    ("row", "active", "event", "reads"),
    [
        ("job.112", configuration("QUE_WAIT"), ChartEvent(ON_ICE), {"ice_on"}),
        ("job.111", configuration("QUE_WAIT", armed=True), ChartEvent(KILLJOB), {"arm_off"}),
        (
            "job.110",
            configuration("QUE_WAIT", armed=True),
            ChartEvent(READMIT, Readmission(recheck_fails=True)),
            {"arm_off"},
        ),
        (
            "job.113",
            configuration("QUE_WAIT", hold=True),
            ChartEvent(ON_NOEXEC, Noexec()),
            {"noexec_on", "hold_off"},
        ),
        (
            "job.136",
            configuration("SUCCESS", hold=True),
            ChartEvent(ON_NOEXEC, Noexec(change=False)),
            {"noexec_on", "hold_off"},
        ),
    ],
)
def test_a_status_rule_that_reads_a_flag_sees_the_flag_moved(
    row: str, active: frozenset[str], event: ChartEvent, reads: set[str]
) -> None:
    """The flag is written first, then the status rule reads it
    (`_resolves` and `_out_of_the_run` read `on_ice`; a wake's attempt reads
    `armed`; the retry reads `on_hold` and `on_noexec`). The status row's
    action sees the flag write, because it runs as the flag row's chained item."""
    seen: list[frozenset[str]] = []

    def spy(ctx: Any) -> None:
        """Record the configuration the status row's action sees."""
        seen.append(ctx.active)

    chart = replace(
        CHART,
        rows=tuple(replace(r, action="spy") if r.id == row else r for r in CHART.rows),
        actions={**CHART.actions, "spy": spy},
    )
    executor = Executor()
    executor.restore("j", chart, active, BOX_DATA if row == "job.136" else JOB_DATA)
    rows = _rows(executor.dispatch("j", event))
    assert ("j", row) in rows
    assert len(seen) == 1 and reads <= seen[0]


#: A box `ob` whose condition reads its own SUCCESS, so its completion
#: restarts it; its member `om` holds the only unit of `Q`.
def _restart_jil(res_type: str, free: str) -> str:
    return (
        f"insert_resource: Q\nres_type: {res_type}\namount: 2\n\n"
        "insert_job: ob\njob_type: b\ncondition: s(ob)\n\n"
        "insert_job: om\njob_type: c\ncommand: m\nmachine: m1\nbox_name: ob\n"
        f"resources: (Q, QUANTITY=1{free})\n"
    )


@pytest.mark.parametrize(
    ("jil", "release", "settling", "reserving"),
    [
        (_restart_jil("R", ", FREE=Y"), "free", "job.304", "job.301"),
        (_restart_jil("R", ", FREE=N"), "keep", "job.305", "job.302"),
        (_restart_jil("D", ""), "spend", "job.306", "job.301"),
    ],
    ids=["free", "keep", "spend"],
)
def test_a_box_restarted_by_its_own_completion_meets_the_member_before_its_late_settlement(
    monkeypatch: pytest.MonkeyPatch,
    jil: str,
    release: Release,
    settling: str,
    reserving: str,
) -> None:
    """om's SUCCESS completes ob, ob restarts, resets om and starts it again.
    The start settles the run that ended first (job.401), by the policy of its
    end (free, keep or spend), and then reserves or takes over what was kept.
    om's own `settle` runs last, finds the job live again and settles nothing
    (job.418, DL-340)."""
    setup = [{"kind": "FORCE_STARTJOB", "payload": {"job": "ob"}}]
    event = {"kind": "STATUS", "payload": {"job": "om", "exit_code": 0}}
    rows = _chart_rows(monkeypatch)
    oracle = _input(jil, setup, event, rows)
    row = oracle.store.job["om"]
    assert (row.status, row.run_number) == ("RUNNING", 2)
    assert [(r.bucket, r.units) for r in row.reservations] == [("r:Q", 1)]
    assert dict(oracle.store.consumed) == ({"r:Q": 1} if release == "spend" else {})
    om = [ident for job, ident in rows if job == "om"]
    assert [ident for ident in om if ident in _moves() or ident == SETTLE_FIRST] == [
        "job.117",
        "job.122",
        SETTLE_FIRST,
        settling,
        "job.102",
        reserving,
        "job.224",
        "job.105",
    ]
    # the SUCCESS move's own settlement pops last and finds om live again
    settles = [ident for ident in om if ident in (SETTLE_LIVE, "job.406")]
    assert settles[-1] == SETTLE_LIVE
    assert not set(om[om.index("job.105") :]) & {"job.304", "job.305", "job.306"}


# ---- a box's ON_NOEXEC on the jobs it contains (DL-347)

#: A box `nb` and four jobs in it: one with no flag, one held, one ON_NOEXEC,
#: and one ON_NOEXEC and then held.
_NOEXEC_MEMBERS = ("n0", "nh", "nx", "nxh")
_NOEXEC_JIL = "insert_job: nb\njob_type: b\n\n" + "".join(
    f"insert_job: {job}\njob_type: c\ncommand: x\nmachine: m1\nbox_name: nb\n\n"
    for job in _NOEXEC_MEMBERS
)


def test_a_boxs_noexec_on_the_jobs_it_contains_replays_through_the_chart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A box's ON_NOEXEC sets each contained job's flag, from either value,
    and clears its hold, as the job's own ON_NOEXEC's flag rows do (job.209,
    job.207). Replayed through `ON_NOEXEC on its box` from each job's row
    before the input, a bare chart fires the rows the oracle wrote for that
    job (job.231 to job.233), and ends in the flags its row has after it."""
    rows = _chart_rows(monkeypatch)
    oracle = Oracle(lower_source(_NOEXEC_JIL))
    for minute, (kind, job) in enumerate(
        [("ON_HOLD", "nh"), ("ON_NOEXEC", "nx"), ("ON_NOEXEC", "nxh"), ("ON_HOLD", "nxh")], 1
    ):
        oracle.feed(Event(at=T0 + timedelta(minutes=minute), kind=kind, payload={"job": job}))  # type: ignore[arg-type]
    before = {job: row_configuration(oracle.store.runtime(job)) for job in _NOEXEC_MEMBERS}
    del rows[:]
    oracle.feed(Event(at=T0 + timedelta(minutes=5), kind="ON_NOEXEC", payload={"job": "nb"}))
    fired: dict[str, list[str]] = {}
    for job in _NOEXEC_MEMBERS:
        written = [ident for name, ident in rows if name == job and ident in BOX_NOEXEC]
        executor = Executor()
        executor.restore(job, CHART, before[job], JOB_DATA)
        outcomes = executor.dispatch(job, ChartEvent(BOX_ON_NOEXEC))
        fired[job] = [ident for outcome in outcomes for ident in outcome.rows]
        assert sorted(fired[job]) == sorted(written), job
        after = projection(row_configuration(oracle.store.runtime(job)))
        assert projection(executor.active(job)) == after, job
    assert fired == {
        "n0": ["job.231"],
        "nh": ["job.232", "job.231"],
        "nx": ["job.233"],
        "nxh": ["job.232", "job.233"],
    }
