"""Oracle discrete-event interpreter trace tests (phase 7).

Normative spec: docs/ir-design.md ss7 (oracle interface, determinism,
non-goals) and every SEM entry in docs/autosys-semantics.md; the trace-test
index is dossier ss8 (T01..T34) and each test below cites its T-number plus
the SEM entry it pins. oracle.py's own module docstring pins the interpreter
decisions (Q2 zero-lookback anchor, Q3 arm-and-wait -- both cited-resolved,
DL-54/DL-58 -- and the SEM-33 closer-edge midpoint tie-break) that these
tests exercise.

Every expected outcome here was verified empirically against the real oracle
before the assertion was written (CLAUDE.md: fidelity is tested, not
asserted). One test (test_sem33_box_variant_two_members_deferred_member_is_
dropped_by_premature_fold) pins the SEM-33/docstring-documented behavior
("box context stays RUNNING overnight") against an oracle.py code path that
does not actually deliver it in a multi-member box; it is marked
xfail(strict=True) with the repro and citation in its docstring -- see the
final report for the SUSPECTED SRC BUG writeup.

T03 (SEM-03, precedence) is out of scope for the oracle: precedence is
pinned at parse time (condition.lark, flat left-to-right per Q1/DL-53),
never seen by the interpreter, which only walks whatever Cond tree the
parser produced.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from bisim_harness import EngineHarness
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventKind, OracleError, TraceEntry
from dsl41.runner_clock import EngineError
from dsl41.semantics import SemanticSwitches, resolve as resolve_switches

T0 = datetime(2026, 7, 1, 8, 0)

#: flipped by the autouse fixture below; oracle() consults it
_ENGINE_PATH = False
_HARNESSES: list[EngineHarness] = []


@pytest.fixture(autouse=True, params=["direct", "engine"])
def sem_path(request: pytest.FixtureRequest) -> Iterator[str]:
    """Bisimulation gate (runner-design ss13, DL-41 decision 9): every SEM
    trace test in this module runs twice -- Oracle-direct and
    Engine(VirtualClock, inert FakeAdapter) -- and must behave identically;
    this is equivalence tier c between simulator and executor and phase
    11a's definition of done. oracle() below builds whichever path the
    param selects."""
    global _ENGINE_PATH
    _ENGINE_PATH = request.param == "engine"
    yield request.param
    _ENGINE_PATH = False
    _close_harnesses()


def _close_harnesses() -> None:
    """Close every registered harness even if one close() raises: aborting
    midway would leak the rest into the NEXT test's teardown, reporting the
    error against the wrong test."""
    errors: list[Exception] = []
    while _HARNESSES:
        try:
            _HARNESSES.pop().close()
        except Exception as exc:  # noqa: BLE001 -- collect, close the rest, re-raise
            errors.append(exc)
    if errors:
        raise errors[0]


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def oracle(
    jil_text: str,
    *,
    default_tz: str | None = None,
    tz_aliases: dict[str, str] | None = None,
    semantics: SemanticSwitches | None = None,
) -> Oracle | EngineHarness:
    catalog = lower_source(jil_text)
    if _ENGINE_PATH:
        if default_tz is not None or tz_aliases is not None:
            pytest.skip(
                "default_tz/tz_aliases are Oracle-construction knobs (DL-155): the"
                " engine's oracle takes them from its scheduler or its pinned profile"
                " (DL-253), and this harness wires neither; test_runner_scheduler"
                " covers the engine path"
            )
        harness = EngineHarness(catalog, semantics=semantics)
        _HARNESSES.append(harness)
        return harness
    return Oracle(catalog, default_tz=default_tz, tz_aliases=tz_aliases, semantics=semantics)


def transitions(o: Oracle | EngineHarness, job: str) -> list[str]:
    return [t.transition for t in o.trace() if t.job == job]


def pending_timer_jobs(o: Oracle | EngineHarness) -> set[str]:
    """pending_timers() is Oracle-only; the engine path's oracle lives at
    `.engine.oracle` on the harness (bisim_harness.EngineHarness)."""
    oracle_obj = o if isinstance(o, Oracle) else o.engine.oracle
    return {job for _, job, _ in oracle_obj.pending_timers()}


def test_bisim_gate_meta_all_tests_go_through_the_oracle_helper() -> None:
    """Gate-honesty guard: the DL-43 claim 'every SEM trace test runs twice'
    holds only if every test builds its interpreter through the oracle()
    helper. A future test constructing an Oracle directly would pass green
    under both params while never touching the engine -- silently shrinking
    the gate. Exactly one direct construction is allowed: the helper."""
    import pathlib
    import re

    source = pathlib.Path(__file__).read_text()
    constructions = re.findall(r"\bOracle\(", source)
    assert len(constructions) == 1, (
        "a test constructs an Oracle directly; route it through oracle() so "
        "the bisimulation gate covers it"
    )


# ------------------------------------------------------------ 1. SEM-01 latching


def test_sem01_direct_success_auto_starts_consumer_immediately() -> None:
    """T01 (SEM-01): the direct form -- A succeeds, B (condition s(A)) auto-
    starts immediately, no lookback qualifier needed."""
    text = (
        "insert_job: job_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: job_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(job_a)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="job_a", status="SUCCESS"))
    assert transitions(o, "job_b") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem01_latching_across_days_survives_hold_and_an_unrelated_clock_advance() -> None:
    """T01 (SEM-01): 'condition: s(JobA)' is satisfied by JobA's *current
    recorded status* regardless of when it was set. JobB is put ON_HOLD so
    it does not fire the instant JobA succeeds; an unrelated event (ticker)
    advances the clock 72h with no relation to job_a/job_b; OFF_HOLD then
    re-evaluates and JobB starts, proving the SUCCESS from 72h earlier still
    latches -- the single most important divergence from run-scoped DAGs."""
    text = (
        "insert_job: job_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: job_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(job_a)\n\n"
        "insert_job: ticker\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="job_b"))
    o.feed(ev("STATUS", 0, job="job_a", status="SUCCESS"))
    assert transitions(o, "job_b") == ["ON_HOLD"]  # held: does not fire at T0
    o.feed(ev("STATUS", 72 * 60, job="ticker", status="SUCCESS"))  # unrelated clock advance
    assert transitions(o, "job_b") == ["ON_HOLD"]  # still held, unaffected
    o.feed(ev("OFF_HOLD", 72 * 60, job="job_b"))
    assert transitions(o, "job_b") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


# ------------------------------------------------------------ 2. SEM-02 atom truth table


def test_sem02_atom_s_true_only_after_success() -> None:
    """T02 (SEM-02): s()/success() == status == SUCCESS, nothing else."""
    text = (
        "insert_job: prod_s\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_s\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_s)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod_s", status="FAILURE"))
    assert transitions(o, "cons_s") == []
    o.feed(ev("STATUS", 1, job="prod_s", status="SUCCESS"))
    assert transitions(o, "cons_s") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem02_atom_f_true_only_after_failure() -> None:
    """T02 (SEM-02): f()/failure() == status == FAILURE, nothing else."""
    text = (
        "insert_job: prod_f\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_f\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(prod_f)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod_f", status="SUCCESS"))
    assert transitions(o, "cons_f") == []
    o.feed(ev("STATUS", 1, job="prod_f", status="FAILURE"))
    assert transitions(o, "cons_f") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE", "TERMINATED"])
def test_sem02_atom_d_true_for_every_terminal_status(status: str) -> None:
    """T02 (SEM-02): d()/done() == terminal: SUCCESS, FAILURE, or TERMINATED."""
    text = (
        "insert_job: prod_d\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_d\njob_type: c\ncommand: y\nmachine: m1\ncondition: d(prod_d)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod_d", status=status))
    assert o.store.job["prod_d"].status == status
    assert transitions(o, "cons_d") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem02_atom_t_true_only_after_terminated() -> None:
    """T02 (SEM-02): t()/terminated() == status == TERMINATED; SUCCESS does
    not satisfy it (distinct from d())."""
    text = (
        "insert_job: prod_t\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_t\njob_type: c\ncommand: y\nmachine: m1\ncondition: t(prod_t)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod_t", status="SUCCESS"))
    assert transitions(o, "cons_t") == []
    o.feed(ev("STATUS", 1, job="prod_t", status="TERMINATED"))
    assert transitions(o, "cons_t") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem02_atom_n_true_for_a_never_run_job() -> None:
    """T02 (SEM-02): n()/notrunning() is true for INACTIVE (never ran).
    Re-evaluation is edge-triggered (DL-13) and a never-run producer emits
    no edges, so the consumer's own STARTJOB tick carries the evaluation
    (definition-time evaluation is not modeled; the script owns triggers)."""
    text = (
        "insert_job: p2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer_n2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(p2)\n\n"
        "insert_job: p3\njob_type: c\ncommand: z\nmachine: m1\n\n"
        "insert_job: consumer_n3\njob_type: c\ncommand: w\nmachine: m1\ncondition: n(p3)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="p2"))
    o.feed(ev("STARTJOB", 1, job="consumer_n2"))  # n(p2) false: p2 RUNNING -> no
    # start (and no schedule block -> nothing arms)
    o.feed(ev("STARTJOB", 1, job="consumer_n3"))  # n(p3) true: p3 never ran
    assert transitions(o, "consumer_n2") == []
    assert transitions(o, "consumer_n3") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


@pytest.mark.parametrize(
    ("terminal_status", "kill"),
    [("SUCCESS", False), ("FAILURE", False), ("TERMINATED", True)],
)
def test_sem02_atom_n_false_while_running_true_after_terminal(
    terminal_status: str, kill: bool
) -> None:
    """T02 (SEM-02): n() is false for STARTING/RUNNING and true again once
    the job reaches any terminal status (SUCCESS/FAILURE/TERMINATED)."""
    text = (
        "insert_job: p_n\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_n\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(p_n)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="p_n"))
    assert transitions(o, "cons_n") == []  # RUNNING -> n() false
    if kill:
        o.feed(ev("KILLJOB", 1, job="p_n"))
    else:
        o.feed(ev("STATUS", 1, job="p_n", status=terminal_status))
    assert o.store.job["p_n"].status == terminal_status
    assert transitions(o, "cons_n") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem02_atom_e_comparisons_and_failure_run_still_satisfies_them() -> None:
    """T02 (SEM-02): e()/exitcode() compares =, !=, >, <= against the last
    exit code. A FAILURE run (max_exit_success default 0, so exit_code=5 ->
    FAILURE) still carries an exit_code that e() comparisons can match."""
    text = (
        "insert_job: p_exit\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_eq\njob_type: c\ncommand: a\nmachine: m1\ncondition: e(p_exit) = 5\n\n"
        "insert_job: cons_ne\njob_type: c\ncommand: b\nmachine: m1\ncondition: e(p_exit) != 5\n\n"
        "insert_job: cons_gt\njob_type: c\ncommand: c\nmachine: m1\ncondition: e(p_exit) > 3\n\n"
        "insert_job: cons_le\njob_type: c\ncommand: d\nmachine: m1\ncondition: e(p_exit) <= 3\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="p_exit", exit_code=5))
    assert o.store.job["p_exit"].status == "FAILURE"
    assert o.store.job["p_exit"].exit_code == 5
    assert transitions(o, "cons_eq") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "cons_ne") == []
    assert transitions(o, "cons_gt") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "cons_le") == []


# ------------------------------------------------------------------ 3. SEM-04 lookback


def test_sem04a_lookback_window_in_fires_when_evaluated_inside_the_window() -> None:
    """T04a (SEM-04): s(job, 00.30) (30-minute window); success 5 minutes
    ago is inside the window -> fires. cons_window is held first so the
    trivial same-instant satisfaction at t=0 does not short-circuit the
    test; OFF_HOLD at +5min is the delayed evaluation."""
    text = (
        "insert_job: prod_lb\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_window\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_lb, 00.30)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="cons_window"))
    o.feed(ev("STATUS", 0, job="prod_lb", status="SUCCESS"))
    assert transitions(o, "cons_window") == ["ON_HOLD"]
    o.feed(ev("OFF_HOLD", 5, job="cons_window"))
    assert transitions(o, "cons_window") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem04b_lookback_window_out_does_not_fire() -> None:
    """T04b (SEM-04): s(job, 00.30); success 40 minutes ago is outside the
    30-minute window -> does not fire, using the ON_HOLD/OFF_HOLD pattern
    so OFF_HOLD's direct attempt_start gives the condition its strongest
    possible chance to fire and it still does not."""
    text = (
        "insert_job: prod_lb2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_window2\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_lb2, 00.30)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="cons_window2"))
    o.feed(ev("STATUS", 0, job="prod_lb2", status="SUCCESS"))
    o.feed(ev("OFF_HOLD", 40, job="cons_window2"))
    assert transitions(o, "cons_window2") == ["ON_HOLD", "OFF_HOLD"]
    assert o.store.job["cons_window2"].status == "INACTIVE"


def test_sem04c_lookback_9999_is_indefinite_ignores_the_window() -> None:
    """T04c (SEM-04): s(job, 9999) is explicit indefinite lookback (legacy
    4.5.1 default); success 40 minutes ago still fires even though 40 > any
    ordinary sub-day window, because 9999 carries no window at all."""
    text = (
        "insert_job: prod_lb3\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_indef\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_lb3, 9999)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="cons_indef"))
    o.feed(ev("STATUS", 0, job="prod_lb3", status="SUCCESS"))
    o.feed(ev("OFF_HOLD", 40, job="cons_indef"))
    assert transitions(o, "cons_indef") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem04_zero_lookback_since_last_end_anchor_pinned() -> None:
    """T04 (SEM-04), Q2a RESOLVED (DL-54): s(prod, 0) is satisfied iff prod
    ended at-or-after the CONSUMER'S OWN last end -- "examines the last end
    time of the job first. It then examines the last end time of the
    condition job" (TechDocs 12.0.01, condition attribute page). This is a
    pinning test in the test_sem03_* mold: the superseded midnight reading
    is discriminated below, not kept behind a switch. Script: prod succeeds
    08:00 -> consumer runs (first-run: no anchor yet) and completes 09:00.
    Now prod's 08:00 latch is STALE (before the consumer's own end) --
    held/off-held at 10:00/10:30 SAME DAY, the consumer does not restart
    (midnight would have fired here: same calendar day). prod succeeding
    again at 11:00 is fresh -- the consumer re-runs on the edge."""
    text = (
        "insert_job: prod_zero\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_zero\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_zero, 0)\n"
    )
    o = oracle(text)
    day = datetime(2026, 7, 1, 8, 0)

    def at(hour: int, minute: int = 0) -> datetime:
        return day.replace(hour=hour, minute=minute)

    o.feed(Event(at=at(8), kind="STATUS", payload={"job": "prod_zero", "status": "SUCCESS"}))
    o.feed(Event(at=at(9), kind="STATUS", payload={"job": "cons_zero", "status": "SUCCESS"}))
    o.feed(Event(at=at(10), kind="ON_HOLD", payload={"job": "cons_zero"}))
    o.feed(Event(at=at(10, 30), kind="OFF_HOLD", payload={"job": "cons_zero"}))
    assert transitions(o, "cons_zero") == [
        "INACTIVE->STARTING",  # 08:00 first-run: consumer never ended, unbounded
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",  # 09:00 -- this end is the anchor from here on
        "ON_HOLD",
        "OFF_HOLD",  # 10:30: prod's 08:00 latch predates the anchor -> stale, no start
    ]
    o.feed(Event(at=at(11), kind="STATUS", payload={"job": "prod_zero", "status": "SUCCESS"}))
    assert transitions(o, "cons_zero")[-2:] == [
        "SUCCESS->STARTING",  # 11:00 edge: fresh (at-or-after the 09:00 anchor)
        "STARTING->RUNNING",
    ]


def test_sem04_zero_lookback_first_run_corner_is_unbounded() -> None:
    """T04 (SEM-04), Q2b RESOLVED by citation (DL-58): a consumer that
    never ended has no anchor and the atom is satisfied, however old the
    predecessor's latch is -- CA support: "working as designed. When a new
    job is inserted it has no initial/previous end time". Cross-midnight,
    >24h stale: the superseded midnight reading pinned this exact script
    to no-start."""
    text = (
        "insert_job: prod_fr\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_fr\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_fr, 0)\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 6, 30, 23, 50), kind="ON_HOLD", payload={"job": "cons_fr"}))
    o.feed(
        Event(
            at=datetime(2026, 6, 30, 23, 50),
            kind="STATUS",
            payload={"job": "prod_fr", "status": "SUCCESS"},
        )
    )
    o.feed(Event(at=datetime(2026, 7, 2, 0, 10), kind="OFF_HOLD", payload={"job": "cons_fr"}))
    assert transitions(o, "cons_fr") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem04_zero_lookback_cross_midnight_fresh_predecessor_fires() -> None:
    """T04 (SEM-04), Q2a discrimination vs the superseded midnight reading,
    other direction: the consumer ended day 1, the predecessor succeeds at
    23:50 day 1, evaluation at 00:10 day 2. since-last-end: fresh (prod
    ended after the consumer's end) -> fires. midnight would have read the
    23:50 latch as a different calendar day -> stale. Together with the
    same-day-stale case above, the two pins separate the readings in both
    directions."""
    text = (
        "insert_job: prod_xm\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_xm\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_xm, 0)\n"
    )
    o = oracle(text)
    o.feed(
        Event(
            at=datetime(2026, 6, 30, 20, 0),
            kind="STATUS",
            payload={"job": "cons_xm", "status": "SUCCESS"},
        )
    )
    o.feed(Event(at=datetime(2026, 6, 30, 23, 40), kind="ON_HOLD", payload={"job": "cons_xm"}))
    o.feed(
        Event(
            at=datetime(2026, 6, 30, 23, 50),
            kind="STATUS",
            payload={"job": "prod_xm", "status": "SUCCESS"},
        )
    )
    o.feed(Event(at=datetime(2026, 7, 1, 0, 10), kind="OFF_HOLD", payload={"job": "cons_xm"}))
    assert transitions(o, "cons_xm") == [
        "INACTIVE->SUCCESS",  # 20:00 day 1: the consumer's anchor-setting end
        "ON_HOLD",
        "OFF_HOLD",
        "SUCCESS->STARTING",  # 00:10 day 2: 23:50 latch >= 20:00 anchor -> fresh
        "STARTING->RUNNING",
    ]


# --------------------------------------------------------- 4. SEM-05 iced + lookback


def test_sem05_iced_predecessor_satisfies_lookback_condition_regardless_of_age() -> None:
    """T05 (SEM-05): producer succeeded 10 days ago, outside a 1h lookback
    window -> s(prod, 01.00) does not fire (verified while off-hold, outside
    the window). ON_ICE the producer -> the atom evaluates true and the
    lookback is ignored entirely (interacts with SEM-20)."""
    text = (
        "insert_job: prod_ice\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer_ice\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_ice, 01.00)\n"
    )
    o = oracle(text)
    ten_days_ago = -10 * 24 * 60
    o.feed(ev("ON_HOLD", ten_days_ago, job="consumer_ice"))
    o.feed(ev("STATUS", ten_days_ago, job="prod_ice", status="SUCCESS"))
    o.feed(ev("OFF_HOLD", 0, job="consumer_ice"))
    assert transitions(o, "consumer_ice") == ["ON_HOLD", "OFF_HOLD"]  # outside window
    o.feed(ev("ON_ICE", 0, job="prod_ice"))
    assert transitions(o, "consumer_ice") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


# --------------------------------------------------------------- 5. SEM-06 undefined job


def test_sem06_undefined_job_never_fires_despite_many_unrelated_events() -> None:
    """T06 (SEM-06): a condition atom referencing a job absent from the
    catalog evaluates false, permanently and silently; the dependent job
    never auto-starts no matter how many events touch the system."""
    text = (
        "insert_job: cons_ghost\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ghost)\n\n"
        "insert_job: real_job\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="real_job"))
    o.feed(ev("OFF_ICE", 1, job="real_job"))
    o.feed(ev("SET_GLOBAL", 2, name="UNRELATED", value="1"))
    assert transitions(o, "cons_ghost") == []
    o.feed(ev("STATUS", 3, job="real_job", status="SUCCESS"))
    assert transitions(o, "cons_ghost") == []


def test_sem06_undefined_job_inside_or_still_fires_via_the_defined_branch() -> None:
    """T06 (SEM-06): s(ghost) | s(real) -- the undefined branch stays
    permanently false, but the Or still fires once the real branch does."""
    text = (
        "insert_job: real_job2\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_or_ghost\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(ghost) | s(real_job2)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="real_job2", status="SUCCESS"))
    assert transitions(o, "cons_or_ghost") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


# --------------------------------------------------------------- 6. SEM-08 globals


def test_sem08_set_global_triggers_reevaluation() -> None:
    """T08 (SEM-08): value(FLAG) = go; SET_GLOBAL FLAG=stop does not fire it,
    SET_GLOBAL FLAG=go does -- setting a global is itself a re-eval trigger."""
    text = "insert_job: cons_flag\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"
    o = oracle(text)
    o.feed(ev("SET_GLOBAL", 0, name="FLAG", value="stop"))
    assert transitions(o, "cons_flag") == []
    o.feed(ev("SET_GLOBAL", 1, name="FLAG", value="go"))
    assert transitions(o, "cons_flag") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem08_numeric_global_comparison() -> None:
    """T08 (SEM-08): value(N) > 5 fires when SET_GLOBAL pushes N above the
    threshold and does not fire when it stays at or below it."""
    text = (
        "insert_job: cons_gt5a\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(N1) > 5\n\n"
        "insert_job: cons_gt5b\njob_type: c\ncommand: y\nmachine: m1\ncondition: v(N2) > 5\n"
    )
    o = oracle(text)
    o.feed(ev("SET_GLOBAL", 0, name="N1", value="6"))
    assert transitions(o, "cons_gt5a") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("SET_GLOBAL", 1, name="N2", value="4"))
    assert transitions(o, "cons_gt5b") == []


def test_sem08_declared_insert_global_initial_value_satisfies_on_evaluation() -> None:
    """T08 (SEM-08): an insert_global's declared value is loaded into the
    store at Oracle construction (before any feed()) and latches exactly
    like SEM-01 job status. Re-evaluation is edge-triggered (DL-13), so the
    already-true condition fires when an edge carries the evaluation --
    here a SET_GLOBAL re-asserting the same value; an unrelated job's
    event does NOT wake it."""
    text = (
        "insert_global: FLAG3\nvalue: go\n\n"
        "insert_job: cons_flag3\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG3) = go\n\n"
        "insert_job: dummy3\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    assert o.store.global_value("FLAG3") == "go"
    o.feed(ev("STATUS", 0, job="dummy3", status="SUCCESS"))  # unrelated: no wake
    assert transitions(o, "cons_flag3") == []
    o.feed(ev("SET_GLOBAL", 1, name="FLAG3", value="go"))  # same-value edge
    assert transitions(o, "cons_flag3") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


# --------------------------------------------------------- 7. SEM-09 max_exit_success


@pytest.mark.parametrize(
    ("exit_code", "expected_status", "should_fire"),
    [(0, "SUCCESS", True), (2, "SUCCESS", True), (3, "FAILURE", False), (5, "FAILURE", False)],
    ids=["code-0", "code-2-boundary", "code-3-boundary-plus-1", "code-5"],
)
def test_sem09_max_exit_success_shifts_the_success_failure_boundary(
    exit_code: int, expected_status: str, should_fire: bool
) -> None:
    """T09 (SEM-09): max_exit_success: 2 records SUCCESS for exit codes <= 2
    and FAILURE above; a consumer's s(p) is only meaningful relative to the
    producer's configured boundary, never a hardcoded exit 0."""
    text = (
        "insert_job: prod9\njob_type: c\ncommand: x\nmachine: m1\nmax_exit_success: 2\n\n"
        "insert_job: cons9_s\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod9)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod9", exit_code=exit_code))
    assert o.store.job["prod9"].status == expected_status
    fired = transitions(o, "cons9_s") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert fired is should_fire


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(1, "FAILURE"), (2, "SUCCESS"), (0, "SUCCESS"), (3, "SUCCESS")],
    ids=["listed-below-threshold", "at-threshold", "zero", "above-threshold"],
)
def test_sem09b_fail_codes_decide_alone(exit_code: int, expected_status: str) -> None:
    """T09b (SEM-09, amended DL-58 per KB 408778): a present fail_codes is
    the only verdict source -- listed codes are FAILURE even below the
    max_exit_success threshold, and EVERY unlisted code is SUCCESS, the
    threshold included-and-ignored (exit 3 > max_exit_success 2 is still
    SUCCESS; the superseded Q7 pin called it FAILURE)."""
    text = (
        "insert_job: prod9b\njob_type: c\ncommand: x\nmachine: m1\n"
        "max_exit_success: 2\nfail_codes: 1\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod9b", exit_code=exit_code))
    assert o.store.job["prod9b"].status == expected_status


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(25, "SUCCESS"), (0, "FAILURE"), (31, "FAILURE")],
    ids=["in-range", "zero-not-listed-q7", "outside-range"],
)
def test_sem09c_success_codes_replace_the_success_rule(
    exit_code: int, expected_status: str
) -> None:
    """T09c (SEM-09/DL-33): a present success_codes REPLACES the default
    success rule -- even exit 0 is FAILURE unless listed, and the
    max_exit_success threshold is ignored (Q7 defaults, conservative
    direction)."""
    text = (
        "insert_job: prod9c\njob_type: c\ncommand: x\nmachine: m1\n"
        "success_codes: 20-30\nmax_exit_success: 2\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod9c", exit_code=exit_code))
    assert o.store.job["prod9c"].status == expected_status


def test_sem09d_fail_codes_present_ignores_success_codes() -> None:
    """T09d (SEM-09, amended DL-58 per KB 408778): with fail_codes present
    the success_codes list is IGNORED entirely -- a code in both lists is
    FAILURE (fail_codes decides), and a code in NEITHER list is SUCCESS
    even though success_codes would have rejected it (the superseded Q7
    pin consulted success_codes after a fail_codes miss)."""
    text = (
        "insert_job: prod9d\njob_type: c\ncommand: x\nmachine: m1\n"
        "success_codes: 1-10\nfail_codes: 5\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod9d", exit_code=5))
    assert o.store.job["prod9d"].status == "FAILURE"
    o2 = oracle(text)
    o2.feed(ev("STATUS", 0, job="prod9d", exit_code=6))
    assert o2.store.job["prod9d"].status == "SUCCESS"
    o3 = oracle(text)
    o3.feed(ev("STATUS", 0, job="prod9d", exit_code=11))  # outside BOTH lists
    assert o3.store.job["prod9d"].status == "SUCCESS"


# ------------------------------------------------------------------ 8. SEM-10 boxes


def test_sem10a_member_start_rules_at_most_once_then_restart_allows_rerun() -> None:
    """T10 (SEM-10): unconditioned member starts with the box; conditioned
    member waits for both box-RUNNING and its own condition; a member runs
    at most once per box execution (a fresh reevaluation while already
    ran-and-terminal does NOT restart it); restarting the box resets the
    per-run bookkeeping so members (even ones that already ran) can run
    again. The restart also resets each member's status to INACTIVE first
    (SEM-10, DL-242), so a rerun shows SUCCESS->INACTIVE->STARTING."""
    text = (
        "insert_job: box10\njob_type: b\n\n"
        "insert_job: mem_u\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box10\n\n"
        "insert_job: mem_c\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box10\n"
        "condition: s(trigger10)\n\n"
        "insert_job: trigger10\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box10"))
    assert transitions(o, "mem_u") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "mem_c") == []  # trigger10 has not fired yet

    o.feed(ev("STATUS", 1, job="trigger10", status="SUCCESS"))
    assert transitions(o, "mem_c") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 2, job="mem_c", status="SUCCESS"))
    mem_c_after_first_run = transitions(o, "mem_c")
    assert mem_c_after_first_run == ["INACTIVE->STARTING", "STARTING->RUNNING", "RUNNING->SUCCESS"]

    # force a second condition-true moment inside the same box run: trigger10
    # is still latched SUCCESS, so any global re-eval re-checks mem_c's
    # condition as true, but the at-most-once bookkeeping still blocks it.
    o.feed(ev("SET_GLOBAL", 3, name="DUMMY", value="1"))
    assert transitions(o, "mem_c") == mem_c_after_first_run  # unchanged

    o.feed(ev("STATUS", 4, job="mem_u", status="SUCCESS"))  # box now folds (SEM-11)
    assert transitions(o, "box10") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]

    o.feed(ev("STARTJOB", 5, job="box10"))  # restart: at-most-once resets
    assert transitions(o, "box10")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    rerun = ["SUCCESS->INACTIVE", "INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "mem_u")[-3:] == rerun
    assert transitions(o, "mem_c")[-3:] == rerun


def test_sem10b_member_does_not_start_when_its_box_is_not_running() -> None:
    """T10 (SEM-10): a member's condition becoming true is not enough; the
    containing box must also be RUNNING. Here the box is never started at
    all, so the member stays INACTIVE despite its condition firing true."""
    text = (
        "insert_job: box_idle\njob_type: b\n\n"
        "insert_job: mem_idle\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_idle\n"
        "condition: s(trigger2)\n\n"
        "insert_job: trigger2\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="trigger2", status="SUCCESS"))
    assert o.store.job["box_idle"].status == "INACTIVE"
    assert transitions(o, "mem_idle") == []


def test_explicit_startjob_against_a_live_job_leaves_a_trace_record() -> None:
    """DL-81, DL-64's remaining silent corner. Two operators racing a start on
    the same idle job both get an ok from the control socket: one start
    happens, and the other used to vanish -- no transition, no record, nothing
    in the trace to show a second attempt was ever made. The engine's arbitration
    (total order, then re-evaluation against current state) was right; only its
    visibility was missing.

    The live-job guard sits ABOVE the force branch in _attempt_start, so a
    FORCE_STARTJOB against a running job records too -- that one matters more,
    because the operator explicitly forced and still got nothing. Internal
    probes stay silent: they discard the return value."""
    text = (
        "insert_job: solo81\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: down81\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(solo81)\n"
    )

    def refusals(o: Oracle | EngineHarness) -> list[TraceEntry]:
        return [t for t in o.trace() if t.transition == "START_REFUSED"]

    def statuses(o: Oracle | EngineHarness, job: str) -> list[str]:
        return [t for t in transitions(o, job) if "->" in t]

    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="solo81"))
    assert statuses(o, "solo81") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert refusals(o) == []

    # the loser of the race: recorded, and it names the state that beat it
    o.feed(ev("STARTJOB", 0, job="solo81"))
    assert [t.job for t in refusals(o)] == ["solo81"]
    assert "already RUNNING" in refusals(o)[0].cause
    assert "STARTJOB event" in refusals(o)[0].cause  # provenance survives (DL-68)
    assert statuses(o, "solo81") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # no re-run

    # FORCE is not exempt: the guard precedes the force branch
    o.feed(ev("FORCE_STARTJOB", 1, job="solo81"))
    assert [t.job for t in refusals(o)] == ["solo81", "solo81"]
    assert "FORCE_STARTJOB event" in refusals(o)[1].cause

    # an internal condition edge probing a live job stays silent: solo81's
    # SUCCESS wakes down81, which starts; re-waking it records no refusal
    o.feed(ev("STATUS", 2, job="solo81", status="SUCCESS"))
    assert statuses(o, "down81") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 3, job="solo81", status="SUCCESS"))
    assert [t.job for t in refusals(o)] == ["solo81", "solo81"]  # down81 never appears


def test_sem10c_explicit_startjob_refused_at_a_sem10_gate_leaves_a_trace_record() -> None:
    """DL-64: an operator's plain STARTJOB dying at either SEM-10 gate used
    to be fully silent -- no transition, no record, untrainable. Both gates
    now leave a START_REFUSED trace record naming FORCE_STARTJOB, for the
    EXPLICIT event only: internal condition-edge re-evaluations probing
    members of non-running boxes stay silent (sem10b's scenario records
    nothing), and FORCE itself never reaches the gates."""
    text = (
        "insert_job: box10c\njob_type: b\n\n"
        "insert_job: mem_r\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box10c\n\n"
        "insert_job: mem_keep\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box10c\n\n"
        "insert_job: mem_g\njob_type: c\ncommand: z\nmachine: m1\nbox_name: box10c\n"
        "condition: v(GO10C) = 1\n"
    )

    def refusals(o: Oracle | EngineHarness) -> list[str]:
        return [t.job for t in o.trace() if t.transition == "START_REFUSED"]

    o = oracle(text)
    # internal wake at the box-not-RUNNING gate: silent (edge-triggered
    # re-evaluation is not an operator's start attempt)
    o.feed(ev("SET_GLOBAL", 0, name="GO10C", value="1"))
    assert refusals(o) == []

    # gate 1, explicit: member of a non-RUNNING box
    o.feed(ev("STARTJOB", 1, job="mem_r"))
    assert transitions(o, "mem_r") == ["START_REFUSED"]  # recorded, not started
    refused = [t for t in o.trace() if t.transition == "START_REFUSED"]
    assert "FORCE_STARTJOB" in refused[0].cause and "SEM-10" in refused[0].cause

    # gate 2, explicit: already ran in this box execution (mem_keep still
    # RUNNING keeps the box open, so the box gate passes)
    o.feed(ev("STARTJOB", 2, job="box10c"))
    o.feed(ev("STATUS", 3, job="mem_r", status="SUCCESS"))
    o.feed(ev("STARTJOB", 4, job="mem_r"))
    assert refusals(o) == ["mem_r", "mem_r"]
    assert "already ran" in [t for t in o.trace() if t.transition == "START_REFUSED"][1].cause

    # FORCE_STARTJOB bypasses both gates and records no refusal (SEM-23)
    o.feed(ev("FORCE_STARTJOB", 5, job="mem_r"))
    assert transitions(o, "mem_r")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    assert refusals(o) == ["mem_r", "mem_r"]


_CHAIN_JIL = (
    "insert_job: box_ch\njob_type: b\n\n"
    "insert_job: ch_a\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box_ch\n\n"
    "insert_job: ch_b\njob_type: c\ncommand: b\nmachine: m1\nbox_name: box_ch\n"
    "condition: s(ch_a)\n\n"
    "insert_job: ch_c\njob_type: c\ncommand: c\nmachine: m1\nbox_name: box_ch\n"
    "condition: s(ch_b)\n"
)


def _status(o: Oracle | EngineHarness, *jobs: str) -> list[str]:
    return [o.store.job[j].status for j in jobs]


def test_sem10_second_box_run_starts_only_the_head_of_a_plain_chain() -> None:
    """T10 (SEM-10 [V], DL-242): "When a box starts running, the status of all
    the jobs it contains (including subboxes) changes to ACTIVATED ...
    jobs in boxes do not retain their statuses from previous box cycles."
    Run two of the chain A -> B -> C starts A only; B and C wait for
    current-run predecessors. The oracle has no ACTIVATED status: a waiting
    member reads INACTIVE and carries no resolution mark. The reset keeps
    the previous run's last end and clears its exit code."""
    o = oracle(_CHAIN_JIL)
    o.feed(ev("STARTJOB", 0, job="box_ch"))
    for minute, job in ((1, "ch_a"), (2, "ch_b"), (3, "ch_c")):
        o.feed(ev("STATUS", minute, job=job, status="SUCCESS", exit_code=0))
    assert o.store.job["box_ch"].status == "SUCCESS"

    o.feed(ev("STARTJOB", 10, job="box_ch"))
    assert _status(o, "box_ch", "ch_a", "ch_b", "ch_c") == [
        "RUNNING",
        "RUNNING",
        "INACTIVE",
        "INACTIVE",
    ]
    assert o.store.job["box_ch"].window_skipped_members == frozenset()  # waiting, not resolved
    reset = [t for t in o.trace() if t.job == "ch_b" and t.transition == "SUCCESS->INACTIVE"]
    assert len(reset) == 1 and reset[0].at == T0 + timedelta(minutes=10)
    assert reset[0].cause.startswith("box 'box_ch' started")
    assert "SEM-10" in reset[0].cause
    assert o.store.job["ch_b"].last_end_at == T0 + timedelta(minutes=2)  # kept
    assert o.store.job["ch_b"].exit_code is None  # the previous cycle's result

    o.feed(ev("STATUS", 11, job="ch_a", status="SUCCESS"))
    assert _status(o, "ch_b", "ch_c") == ["RUNNING", "INACTIVE"]
    o.feed(ev("STATUS", 12, job="ch_b", status="SUCCESS"))
    assert _status(o, "box_ch", "ch_c") == ["RUNNING", "RUNNING"]
    o.feed(ev("STATUS", 13, job="ch_c", status="SUCCESS"))
    assert o.store.job["box_ch"].status == "SUCCESS"


def test_sem10_box_start_reset_leaves_a_live_member_running() -> None:
    """T10 (SEM-10, DL-242): the box-start reset skips a job that is live.
    A member forced while its box was idle keeps its run; its sibling is
    reset and starts with the box."""
    o = oracle(_CHAIN_JIL.replace("condition: s(ch_a)\n", "").replace("condition: s(ch_b)\n", ""))
    o.feed(ev("STARTJOB", 0, job="box_ch"))
    for minute, job in ((1, "ch_a"), (2, "ch_b"), (3, "ch_c")):
        o.feed(ev("STATUS", minute, job=job, status="SUCCESS"))
    o.feed(ev("FORCE_STARTJOB", 5, job="ch_a"))
    o.feed(ev("STARTJOB", 10, job="box_ch"))
    assert transitions(o, "ch_a")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "ch_b")[-3:] == [
        "SUCCESS->INACTIVE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem10_second_box_run_does_not_start_on_a_stale_exit_code() -> None:
    """T10 (SEM-10, DL-242): the reset clears the exit code with the
    status, so an e() consumer does not start on run one's exit code."""
    o = oracle(_CHAIN_JIL.replace("condition: s(ch_b)", "condition: e(ch_b) = 0"))
    o.feed(ev("STARTJOB", 0, job="box_ch"))
    for minute, job in ((1, "ch_a"), (2, "ch_b"), (3, "ch_c")):
        o.feed(ev("STATUS", minute, job=job, exit_code=0))
    assert o.store.job["box_ch"].status == "SUCCESS"
    o.feed(ev("STARTJOB", 10, job="box_ch"))
    assert _status(o, "ch_a", "ch_b", "ch_c") == ["RUNNING", "INACTIVE", "INACTIVE"]
    o.feed(ev("STATUS", 11, job="ch_a", exit_code=0))
    o.feed(ev("STATUS", 12, job="ch_b", exit_code=0))
    assert o.store.job["ch_c"].status == "RUNNING"


def test_sem10_box_start_reset_wakes_an_outside_lookback_consumer() -> None:
    """T10 (SEM-10, DL-242): the reset is a real transition for every reader
    outside the box. It moves `status_at`, so a consumer on n(member, 01.00)
    whose window had closed starts at the reset itself."""
    text = (
        "insert_job: box10n\njob_type: b\n\n"
        "insert_job: mm10n\njob_type: c\ncommand: m\nmachine: m1\nbox_name: box10n\n\n"
        "insert_job: cons10n\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: n(mm10n, 01.00)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="cons10n"))
    o.feed(ev("STARTJOB", 0, job="box10n"))
    o.feed(ev("STATUS", 1, job="mm10n", status="SUCCESS"))
    o.feed(ev("OFF_HOLD", 200, job="cons10n"))
    assert o.store.job["cons10n"].status == "INACTIVE"  # the window has closed
    o.feed(ev("STARTJOB", 210, job="box10n"))
    assert o.store.job["cons10n"].status == "RUNNING"
    [start] = [t for t in o.trace() if t.job == "cons10n" and t.transition == "INACTIVE->STARTING"]
    assert start.cause == "status of 'mm10n' changed to INACTIVE"


def test_sem10_box_start_trace_order_is_reset_then_starting_then_running() -> None:
    """T10 (SEM-10, DL-242), the control: the reset rows are written before
    the box's STARTING transition, and a normal start is otherwise the
    trace it always was."""
    text = (
        "insert_job: box10o\njob_type: b\n\n"
        "insert_job: m10o\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box10o\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box10o"))
    first = [(t.job, t.transition) for t in o.trace()]
    assert first == [
        ("box10o", "INACTIVE->STARTING"),
        ("box10o", "STARTING->RUNNING"),
        ("m10o", "INACTIVE->STARTING"),
        ("m10o", "STARTING->RUNNING"),
    ]
    o.feed(ev("STATUS", 1, job="m10o", status="SUCCESS"))
    o.feed(ev("STARTJOB", 2, job="box10o"))
    second = [(t.job, t.transition) for t in o.trace()][len(first) + 2 :]
    assert second == [
        ("m10o", "SUCCESS->INACTIVE"),
        ("box10o", "SUCCESS->STARTING"),
        ("box10o", "STARTING->RUNNING"),
        ("m10o", "INACTIVE->STARTING"),
        ("m10o", "STARTING->RUNNING"),
    ]


def test_sem10_the_box_starting_wake_reads_the_new_cycle() -> None:
    """T10 (SEM-10, DL-242): the box's own STARTING transition wakes X on
    `s(m) | t(S)`. The reset is written first, so m's previous-cycle
    SUCCESS no longer satisfies X, and X does not run again."""
    text = (
        "insert_job: s10w\njob_type: b\n\n"
        "insert_job: m10w\njob_type: c\ncommand: x\nmachine: m1\nbox_name: s10w\n\n"
        "insert_job: x10w\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(m10w) | t(s10w)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="s10w"))
    o.feed(ev("STATUS", 1, job="m10w", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="x10w", status="SUCCESS"))
    assert o.store.job["s10w"].status == "SUCCESS"
    runs = o.store.job["x10w"].run_number
    o.feed(ev("STARTJOB", 10, job="s10w"))
    assert o.store.job["x10w"].run_number == runs
    assert o.store.job["x10w"].status == "SUCCESS"


def test_sem10_a_box_its_start_terminated_is_not_overwritten_running() -> None:
    """T10 (SEM-10, DL-242) with SEM-14: the reset of B's member wakes an
    ON_NOEXEC sibling in the parent P; its bypass SUCCESS meets P's
    box_failure, P fails, and job_terminator TERMINATES B while B is
    STARTING. B's start stops there: B stays TERMINATED and none of its
    members start."""
    text = (
        "insert_job: p10t\njob_type: b\nbox_failure: s(nx10t)\n\n"
        "insert_job: b10t\njob_type: b\nbox_name: p10t\njob_terminator: 1\n\n"
        "insert_job: b1_10t\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b10t\n\n"
        "insert_job: nx10t\njob_type: c\ncommand: n\nmachine: m1\nbox_name: p10t\n"
        "condition: n(b1_10t, 00.01) & v(GO10T) = 1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="nx10t"))
    o.feed(ev("STARTJOB", 0, job="p10t"))
    o.feed(ev("STATUS", 1, job="b1_10t", status="SUCCESS"))
    o.feed(ev("SET_GLOBAL", 10, name="GO10T", value="1"))
    assert _status(o, "p10t", "b10t", "nx10t") == ["RUNNING", "SUCCESS", "INACTIVE"]
    o.feed(ev("FORCE_STARTJOB", 20, job="b10t"))
    assert _status(o, "p10t", "b10t", "b1_10t") == ["FAILURE", "TERMINATED", "INACTIVE"]
    assert transitions(o, "b10t")[-2:] == ["SUCCESS->STARTING", "STARTING->TERMINATED"]
    assert transitions(o, "b1_10t")[-1] == "SUCCESS->INACTIVE"  # reset, never started


def test_sem10_box_start_clears_the_exit_code_of_a_row_already_inactive() -> None:
    """T10 (SEM-10, DL-242): A ended with exit code 0 and was then set
    INACTIVE by the operator. The next box start clears its exit code with
    a plain store write, so the sibling on `e(A) = 0` waits for A's new
    run."""
    text = (
        "insert_job: box10x\njob_type: b\n\n"
        "insert_job: a10x\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box10x\n\n"
        "insert_job: z10x\njob_type: c\ncommand: z\nmachine: m1\nbox_name: box10x\n"
        "condition: e(a10x) = 0\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box10x"))
    o.feed(ev("STATUS", 1, job="a10x", exit_code=0))
    o.feed(ev("STATUS", 2, job="z10x", exit_code=0))
    assert o.store.job["box10x"].status == "SUCCESS"
    o.feed(ev("STATUS", 3, job="a10x", status="INACTIVE"))
    assert o.store.job["a10x"].exit_code == 0
    o.feed(ev("STARTJOB", 10, job="box10x"))
    assert _status(o, "a10x", "z10x") == ["RUNNING", "INACTIVE"]
    assert o.store.job["a10x"].exit_code is None
    o.feed(ev("STATUS", 11, job="a10x", exit_code=0))
    assert o.store.job["z10x"].status == "RUNNING"


def test_sem10_second_box_run_resets_a_grandchild_before_an_outer_consumer_reads_it() -> None:
    """T10 (SEM-10 [V], DL-242), nested: the reset reaches every job the box
    contains, "including subboxes". The outer consumer is in catalog order
    before the inner box, so it is attempted first at run two's start; a
    stale SUCCESS on the grandchild would start it there."""
    text = (
        "insert_job: ob10\njob_type: b\n\n"
        "insert_job: k10\njob_type: c\ncommand: k\nmachine: m1\nbox_name: ob10\n"
        "condition: s(g10)\n\n"
        "insert_job: ib10\njob_type: b\nbox_name: ob10\n\n"
        "insert_job: g10\njob_type: c\ncommand: g\nmachine: m1\nbox_name: ib10\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ob10"))
    o.feed(ev("STATUS", 1, job="g10", status="SUCCESS"))
    assert _status(o, "ib10", "k10") == ["SUCCESS", "RUNNING"]
    o.feed(ev("STATUS", 2, job="k10", status="SUCCESS"))
    assert o.store.job["ob10"].status == "SUCCESS"

    o.feed(ev("STARTJOB", 10, job="ob10"))
    assert _status(o, "ob10", "ib10", "g10", "k10") == [
        "RUNNING",
        "RUNNING",
        "RUNNING",
        "INACTIVE",
    ]
    assert transitions(o, "k10")[-1] == "SUCCESS->INACTIVE"
    o.feed(ev("STATUS", 11, job="g10", status="SUCCESS"))
    assert _status(o, "ib10", "k10") == ["SUCCESS", "RUNNING"]


def test_sem10_second_box_run_a_held_predecessor_still_blocks_its_consumer() -> None:
    """T10 (SEM-10, DL-242) with SEM-21: an auto_hold member is held again at
    run two's start and reset to INACTIVE, so its consumer, which its
    run-one SUCCESS satisfied, waits."""
    text = (
        "insert_job: box_ah\njob_type: b\n\n"
        "insert_job: ah_a\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box_ah\n\n"
        "insert_job: ah_b\njob_type: c\ncommand: b\nmachine: m1\nbox_name: box_ah\n"
        "condition: s(ah_a)\nauto_hold: 1\n\n"
        "insert_job: ah_c\njob_type: c\ncommand: c\nmachine: m1\nbox_name: box_ah\n"
        "condition: s(ah_b)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box_ah"))
    o.feed(ev("STATUS", 1, job="ah_a", status="SUCCESS"))
    assert o.store.job["ah_b"].status == "INACTIVE"  # held
    o.feed(ev("OFF_HOLD", 2, job="ah_b"))
    o.feed(ev("STATUS", 3, job="ah_b", status="SUCCESS"))
    o.feed(ev("STATUS", 4, job="ah_c", status="SUCCESS"))
    assert o.store.job["box_ah"].status == "SUCCESS"

    o.feed(ev("STARTJOB", 10, job="box_ah"))
    assert o.store.job["ah_b"].on_hold
    assert _status(o, "box_ah", "ah_a", "ah_b", "ah_c") == [
        "RUNNING",
        "RUNNING",
        "INACTIVE",
        "INACTIVE",
    ]
    o.feed(ev("STATUS", 11, job="ah_a", status="SUCCESS"))
    assert _status(o, "box_ah", "ah_b", "ah_c") == ["RUNNING", "INACTIVE", "INACTIVE"]


def test_sem10_second_box_run_resets_an_on_noexec_member_then_bypasses_it() -> None:
    """T10 (SEM-10, DL-242) with SEM-22: "When the box is scheduled to run, the
    statuses of ON_NOEXEC jobs in the box change to ACTIVATED." The
    ON_NOEXEC member is reset like any other and keeps its flag; it
    bypasses to SUCCESS only when its own condition holds in run two."""
    text = (
        "insert_job: box_nx\njob_type: b\n\n"
        "insert_job: nx_a\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box_nx\n\n"
        "insert_job: nx_n\njob_type: c\ncommand: n\nmachine: m1\nbox_name: box_nx\n"
        "condition: s(nx_a)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="nx_n"))
    o.feed(ev("STARTJOB", 0, job="box_nx"))
    o.feed(ev("STATUS", 1, job="nx_a", status="SUCCESS"))
    assert _status(o, "box_nx", "nx_n") == ["SUCCESS", "SUCCESS"]  # bypassed

    o.feed(ev("STARTJOB", 10, job="box_nx"))
    assert o.store.job["nx_n"].on_noexec
    assert _status(o, "box_nx", "nx_a", "nx_n") == ["RUNNING", "RUNNING", "INACTIVE"]
    o.feed(ev("STATUS", 11, job="nx_a", status="SUCCESS"))
    assert transitions(o, "nx_n")[-2:] == ["SUCCESS->INACTIVE", "INACTIVE->SUCCESS"]
    assert o.store.job["box_nx"].status == "SUCCESS"


# ------------------------------------------------------------------ 9. SEM-11 box fold


def test_sem11_box_stays_running_between_first_failure_and_last_completion() -> None:
    """T11 (SEM-11): the box cannot complete until ALL members have run; a
    member failing does not fold the box while a sibling is still RUNNING --
    only once the last member completes does the default FAILURE fold fire."""
    text = (
        "insert_job: box11\njob_type: b\n\n"
        "insert_job: mem_x\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11\n\n"
        "insert_job: mem_y\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box11\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11"))
    o.feed(ev("STATUS", 1, job="mem_x", status="FAILURE"))
    assert transitions(o, "box11") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # still RUNNING
    o.feed(ev("STATUS", 2, job="mem_y", status="SUCCESS"))
    assert transitions(o, "box11") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->FAILURE",
    ]


def test_sem11_default_fold_all_success() -> None:
    """T11 (SEM-11): default fold -- box SUCCESS iff every member ended
    SUCCESS."""
    text = (
        "insert_job: box11b\njob_type: b\n\n"
        "insert_job: mem_p\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11b\n\n"
        "insert_job: mem_q\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box11b\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11b"))
    o.feed(ev("STATUS", 1, job="mem_p", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="mem_q", status="SUCCESS"))
    assert transitions(o, "box11b") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem11_member_set_inactive_completes_a_running_box() -> None:
    """T11 (SEM-11 [V], DL-242): "Using the sendevent command to change the
    state of a job in a box to INACTIVE affects the box's completion status
    as if the INACTIVE job returned a status of SUCCESS." The member is
    marked resolved on the box's row, and the box completes at once. DL-235
    still holds for the launched run: no kill is implied."""
    text = (
        "insert_job: box11i\njob_type: b\n\n"
        "insert_job: m11i\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11i\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11i"))
    assert o.store.job["m11i"].status == "RUNNING"
    o.feed(ev("STATUS", 1, job="m11i", status="INACTIVE"))
    assert _status(o, "box11i", "m11i") == ["SUCCESS", "INACTIVE"]
    assert o.store.job["box11i"].window_skipped_members == frozenset({"m11i"})
    [fold] = [t for t in o.trace() if t.job == "box11i" and t.transition == "RUNNING->SUCCESS"]
    assert fold.cause == "default box fold: all members SUCCESS (SEM-11)"


def test_sem11_failed_member_set_inactive_folds_as_success() -> None:
    """T11 (SEM-11 [V], DL-242): a member that ran and failed, then was set
    INACTIVE, counts as SUCCESS in the fold. The box stays RUNNING while
    the sibling runs, then folds SUCCESS."""
    text = (
        "insert_job: box11f\njob_type: b\n\n"
        "insert_job: p11f\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11f\n\n"
        "insert_job: q11f\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box11f\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11f"))
    o.feed(ev("STATUS", 1, job="p11f", status="FAILURE"))
    o.feed(ev("STATUS", 2, job="p11f", status="INACTIVE"))
    assert o.store.job["box11f"].status == "RUNNING"  # q11f still runs
    o.feed(ev("STATUS", 3, job="q11f", status="SUCCESS"))
    assert o.store.job["box11f"].status == "SUCCESS"


def test_sem11_waiting_member_set_inactive_waits_for_a_running_sibling() -> None:
    """T11 (SEM-11 [V], DL-242): a member still waiting on its condition, set
    INACTIVE by the operator, is resolved -- "the same job is being updated
    to INACTIVE" -- but the box stays RUNNING until its running sibling
    ends. The injected STATUS records the INACTIVE->INACTIVE transition,
    which carries the completion check."""
    text = (
        "insert_job: box11w\njob_type: b\n\n"
        "insert_job: r11w\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11w\n\n"
        "insert_job: w11w\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box11w\n"
        "condition: s(never11w)\n\n"
        "insert_job: never11w\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11w"))
    o.feed(ev("STATUS", 1, job="w11w", status="INACTIVE"))
    assert transitions(o, "w11w") == ["INACTIVE->INACTIVE"]
    assert o.store.job["box11w"].window_skipped_members == frozenset({"w11w"})
    assert o.store.job["box11w"].status == "RUNNING"
    o.feed(ev("STATUS", 2, job="r11w", status="SUCCESS"))
    assert o.store.job["box11w"].status == "SUCCESS"


def test_sem11_waiting_member_still_hangs_the_box() -> None:
    """T11 (SEM-11; DL-13, kept by DL-242): a member whose condition never fires
    and that no operator resolves keeps the box RUNNING. Waiting is
    INACTIVE without a resolution mark, so the hung-box pattern stays."""
    text = (
        "insert_job: box11h\njob_type: b\n\n"
        "insert_job: r11h\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box11h\n\n"
        "insert_job: w11h\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box11h\n"
        "condition: s(never11h)\n\n"
        "insert_job: never11h\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box11h"))
    o.feed(ev("STATUS", 1, job="r11h", status="SUCCESS"))
    o.feed(ev("SET_GLOBAL", 600, name="TICK11H", value="1"))
    assert _status(o, "box11h", "w11h") == ["RUNNING", "INACTIVE"]
    assert o.store.job["box11h"].window_skipped_members == frozenset()


# ------------------------------------------------------- 10. SEM-12 box_success/failure


def test_sem12a_internal_box_success_fires_immediately_other_members_still_running() -> None:
    """T12a (SEM-12): box_success referencing a member inside the box is
    evaluated the instant that member enters the specified state, regardless
    of other members still RUNNING."""
    text = (
        "insert_job: box12a\njob_type: b\nbox_success: s(mem_a12)\n\n"
        "insert_job: mem_a12\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box12a\n\n"
        "insert_job: mem_b12\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box12a\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box12a"))
    o.feed(ev("STATUS", 1, job="mem_a12", status="SUCCESS"))
    box_entries = [t for t in o.trace() if t.job == "box12a"]
    assert transitions(o, "box12a") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    assert "SEM-12" in box_entries[-1].cause
    assert o.store.job["mem_b12"].status == "RUNNING"  # unaffected, still mid-run


def test_sem12b_external_box_success_hung_running_then_fires_when_member_completes_after() -> None:
    """T12b (SEM-12): the hung-RUNNING pattern, reproduced as the documented
    scenario pair. Pair 1: members complete BEFORE the external condition
    becomes true -> the box does not get evaluated and stays RUNNING
    (a classic production incident). Pair 2 (fresh scenario): the external
    condition becomes true FIRST, then a member completes AFTER -> the box
    override fires SUCCESS right there, even with a sibling still RUNNING."""
    hung_text = (
        "insert_job: box12b_1\njob_type: b\nbox_success: s(ext_job)\n\n"
        "insert_job: mem_c12\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box12b_1\n\n"
        "insert_job: mem_d12\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box12b_1\n\n"
        "insert_job: ext_job\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    hung = oracle(hung_text)
    hung.feed(ev("STARTJOB", 0, job="box12b_1"))
    hung.feed(ev("STATUS", 1, job="mem_c12", status="SUCCESS"))
    hung.feed(ev("STATUS", 2, job="mem_d12", status="SUCCESS"))
    assert transitions(hung, "box12b_1") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # hung

    fires_text = (
        "insert_job: box12b_2\njob_type: b\nbox_success: s(ext_job2)\n\n"
        "insert_job: mem_e12\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box12b_2\n\n"
        "insert_job: mem_f12\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box12b_2\n\n"
        "insert_job: ext_job2\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    fires = oracle(fires_text)
    fires.feed(ev("STARTJOB", 0, job="box12b_2"))
    fires.feed(ev("STATUS", 1, job="ext_job2", status="SUCCESS"))  # external true FIRST
    assert transitions(fires, "box12b_2") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # not yet
    fires.feed(ev("STATUS", 2, job="mem_e12", status="SUCCESS"))  # member completes AFTER
    assert transitions(fires, "box12b_2") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    assert fires.store.job["mem_f12"].status == "RUNNING"  # sibling still mid-run


def test_sem12_unmet_box_success_with_a_member_failure_falls_back_to_default_failure() -> None:
    """T12 (SEM-12 third bullet): box_success specified but never met, and
    box_failure unspecified -> default FAILURE logic applies once a member
    has failed and all members complete."""
    text = (
        "insert_job: box12c\njob_type: b\nbox_success: s(ext_job3)\n\n"
        "insert_job: mem_g12\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box12c\n\n"
        "insert_job: mem_h12\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box12c\n\n"
        "insert_job: ext_job3\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box12c"))
    o.feed(ev("STATUS", 1, job="mem_g12", status="FAILURE"))
    o.feed(ev("STATUS", 2, job="mem_h12", status="SUCCESS"))
    assert transitions(o, "box12c") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->FAILURE",
    ]


def test_sem12_unmet_box_success_no_failures_stays_running_indefinitely() -> None:
    """T12 (SEM-12 third bullet): neither override fires (box_success unmet,
    box_failure unspecified) and no member failed -> the box remains RUNNING
    indefinitely; the default SUCCESS fold is suppressed by the specified-
    but-unmet box_success."""
    text = (
        "insert_job: box12d\njob_type: b\nbox_success: s(ext_job4)\n\n"
        "insert_job: mem_i12\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box12d\n\n"
        "insert_job: mem_j12\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box12d\n\n"
        "insert_job: ext_job4\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box12d"))
    o.feed(ev("STATUS", 1, job="mem_i12", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="mem_j12", status="SUCCESS"))
    assert transitions(o, "box12d") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem12c_box_success_over_a_grandchild_fires_transitively() -> None:
    """T12c (SEM-12): "inside the box" is transitive -- derive._is_inside
    counts any ancestor container, and the oracle must agree. The OUTER
    box's box_success names a grandchild (a member of the inner box), so it
    is evaluated the moment that grandchild succeeds, "regardless of other
    members": the inner box is still RUNNING with a sibling mid-run, and the
    outer box completes anyway. Before the fix only the direct parent was
    informed of a member transition, so the outer box hung RUNNING for ever
    while the static classification said the edge was there."""
    text = (
        "insert_job: outer12c\njob_type: b\nbox_success: s(grand12c)\n\n"
        "insert_job: inner12c\njob_type: b\nbox_name: outer12c\n\n"
        "insert_job: grand12c\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner12c\n\n"
        "insert_job: sib12c\njob_type: c\ncommand: y\nmachine: m1\nbox_name: inner12c\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="outer12c"))
    assert transitions(o, "outer12c") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 1, job="grand12c", status="SUCCESS"))
    assert transitions(o, "outer12c") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    outer_entries = [t for t in o.trace() if t.job == "outer12c"]
    assert "SEM-12" in outer_entries[-1].cause
    assert o.store.job["inner12c"].status == "RUNNING"  # the direct parent is unaffected
    assert o.store.job["sib12c"].status == "RUNNING"  # and its other member still runs


def test_sem12c_an_ancestor_override_that_is_unmet_leaves_the_box_running() -> None:
    """T12c (SEM-12, the non-triggering half -- green before the transitive
    walk existed too, and that is its point): the walk EVALUATES the
    ancestor's override, it never folds the ancestor by itself. The
    grandchild FAILs, so box_success: s(grand12e) is unmet; the inner box
    keeps a second member running, so nothing folds anywhere and the outer
    box stays RUNNING -- the specified-but-unmet override suppresses the
    default fold exactly as it does for a direct member."""
    text = (
        "insert_job: outer12e\njob_type: b\nbox_success: s(grand12e)\n\n"
        "insert_job: inner12e\njob_type: b\nbox_name: outer12e\n\n"
        "insert_job: grand12e\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner12e\n\n"
        "insert_job: sib12e\njob_type: c\ncommand: y\nmachine: m1\nbox_name: inner12e\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="outer12e"))
    o.feed(ev("STATUS", 1, job="grand12e", status="FAILURE"))
    assert transitions(o, "outer12e") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "inner12e") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


# ------------------------------------------------------------ 11. SEM-13 sticky TERMINATED


def test_sem13_terminated_box_is_sticky_then_restarts_fresh() -> None:
    """T13 (SEM-13): KILLJOB-ing a RUNNING box moves it to TERMINATED, which
    is sticky -- a member STATUS change afterward does not alter the box.
    The member without job_terminator survives the kill (stays RUNNING);
    the never-run member stays INACTIVE and cannot start while the box is
    TERMINATED even once its own condition becomes true. The next STARTJOB
    of the box starts it fresh: the already-SUCCESS member is reset to
    INACTIVE (SEM-10, DL-242) and runs again, and the previously-INACTIVE
    member (whose condition is now satisfied) runs for the first time."""
    text = (
        "insert_job: box13\njob_type: b\n\n"
        "insert_job: mem13a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box13\n\n"
        "insert_job: mem13b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box13\n"
        "condition: s(trigger13)\n\n"
        "insert_job: trigger13\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box13"))
    o.feed(ev("KILLJOB", 1, job="box13"))
    assert transitions(o, "box13") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->TERMINATED",
    ]
    assert o.store.job["mem13a"].status == "RUNNING"  # no job_terminator: survives
    assert o.store.job["mem13b"].status == "INACTIVE"  # never got a chance to run

    o.feed(ev("STATUS", 2, job="mem13a", status="SUCCESS"))
    assert transitions(o, "box13") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->TERMINATED",
    ]  # unchanged: sticky
    o.feed(ev("STATUS", 2, job="trigger13", status="SUCCESS"))
    assert transitions(o, "mem13b") == []  # box not RUNNING -> still blocked

    o.feed(ev("STARTJOB", 3, job="box13"))
    assert transitions(o, "box13")[-2:] == ["TERMINATED->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "mem13a")[-3:] == [
        "SUCCESS->INACTIVE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    assert transitions(o, "mem13b") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


# ------------------------------------------------------- 12. SEM-14 terminator cascade


def test_sem14_terminator_cascade_both_directions() -> None:
    """T14 (SEM-14): a box_terminator member's FAILURE kills the containing
    box; job_terminator members die with the box; a plain member (neither
    flag) survives. Members killed this way get TERMINATED, which a t()
    consumer outside the box picks up."""
    text = (
        "insert_job: box14\njob_type: b\n\n"
        "insert_job: mem_bt14\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box14\n"
        "box_terminator: 1\n\n"
        "insert_job: mem_jt14\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box14\n"
        "job_terminator: 1\n\n"
        "insert_job: mem_plain14\njob_type: c\ncommand: z\nmachine: m1\nbox_name: box14\n\n"
        "insert_job: cons14_t\njob_type: c\ncommand: w\nmachine: m1\ncondition: t(mem_jt14)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box14"))
    o.feed(ev("STATUS", 1, job="mem_bt14", status="FAILURE"))
    box_entries = [t for t in o.trace() if t.job == "box14"]
    assert transitions(o, "box14") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->TERMINATED",
    ]
    assert "box_terminator" in box_entries[-1].cause
    assert o.store.job["mem_jt14"].status == "TERMINATED"
    assert o.store.job["mem_plain14"].status == "RUNNING"  # survives
    assert transitions(o, "cons14_t") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


# --------------------------------------------------------------------- 13. SEM-20 ON_ICE


def test_sem20a_iced_sibling_unblocks_dependent_and_box_folds_ignoring_it() -> None:
    """T20a (SEM-20): a member depending on an iced sibling starts
    immediately when the box runs (iced -> downstream-satisfied); the iced
    job itself never runs; the box folds (SEM-11) ignoring the iced member
    entirely."""
    text = (
        "insert_job: box20a\njob_type: b\n\n"
        "insert_job: sib_iced20a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box20a\n\n"
        "insert_job: mem_dep20a\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box20a\n"
        "condition: s(sib_iced20a)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="sib_iced20a"))
    o.feed(ev("STARTJOB", 1, job="box20a"))
    assert transitions(o, "sib_iced20a") == ["ON_ICE"]  # never runs
    assert transitions(o, "mem_dep20a") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 2, job="mem_dep20a", status="SUCCESS"))
    assert transitions(o, "box20a") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem20b_off_ice_does_not_immediately_run_but_fires_when_condition_reoccurs() -> None:
    """T20b (SEM-20): OFF_ICE does not itself re-evaluate -- a consumer that
    was iced while its condition was already true stays INACTIVE right after
    OFF_ICE. It runs only once the condition genuinely reoccurs (the
    producer runs and succeeds again)."""
    text = (
        "insert_job: prod20b\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons20b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod20b)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="cons20b"))
    o.feed(ev("STATUS", 1, job="prod20b", status="SUCCESS"))  # condition true while iced
    o.feed(ev("OFF_ICE", 2, job="cons20b"))
    assert transitions(o, "cons20b") == ["ON_ICE", "OFF_ICE"]  # does not run yet
    o.feed(ev("STARTJOB", 3, job="prod20b"))  # producer re-runs
    o.feed(ev("STATUS", 4, job="prod20b", status="SUCCESS"))  # condition reoccurs
    assert transitions(o, "cons20b") == [
        "ON_ICE",
        "OFF_ICE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


@pytest.mark.parametrize(
    ("atom_expr", "expect_true"),
    [
        ("s(prod_f17)", True),
        ("d(prod_f17)", True),
        ("n(prod_f17)", True),
        ("f(prod_f17)", False),
        ("t(prod_f17)", False),
        ("e(prod_f17) = 0", False),
    ],
)
def test_sem20_ordinary_atoms_on_an_iced_job_follow_the_vendor_table(
    atom_expr: str, expect_true: bool
) -> None:
    """SEM-20, DL-243: Start Conditions page, ON_ICE truth table for
    downstream conditions -- success TRUE, failure FALSE, terminated FALSE,
    done TRUE, notrunning TRUE, exitcode FALSE. An ORDINARY atom (no
    lookback qualifier at all, `atom.lookback is None`) on a non-live iced
    job follows this narrower table instead of the DL-13 blanket-true pin
    (which stays the rule for a lookback-qualified atom, see the next
    test)."""
    text = (
        "insert_job: prod_f17\njob_type: c\ncommand: x\nmachine: m1\n\n"
        f"insert_job: cons_f17\njob_type: c\ncommand: y\nmachine: m1\ncondition: {atom_expr}\n"
    )
    o = oracle(text)
    # ON_ICE itself wakes referencers (SEM-20): a true atom fires right here,
    # so no separate STARTJOB is needed (and none should be -- a second
    # attempt on an already-started consumer would add its own refusal
    # record and corrupt the comparison below).
    o.feed(ev("ON_ICE", 0, job="prod_f17"))
    expected = ["INACTIVE->STARTING", "STARTING->RUNNING"] if expect_true else []
    assert transitions(o, "cons_f17") == expected


@pytest.mark.parametrize(
    ("atom_expr",),
    [
        ("f(prod_f17lb, 0)",),  # zero form
        ("t(prod_f17lb, 9999)",),  # explicit indefinite form
        ("e(prod_f17lb, 01.00) = 0",),  # window form
    ],
)
def test_sem20_lookback_atoms_on_an_iced_job_stay_true(atom_expr: str) -> None:
    """Q10 (SEM-20 section 9, DL-243, DL-252): the AutoSys 24.2 "condition
    Attribute" page says a look-back condition on an ON_ICE predecessor
    "always evaluates to true" and the look-back is ignored; the Start
    Conditions ON_ICE table does not separate lookback atoms. The default
    `ice-lookback=true` follows the condition Attribute page (the DL-13
    blanket-true reading: every atom kind true, lookback ignored), and which
    page a live instance follows stays open. f()/t()/exitcode() each read
    true here despite reading false in the ordinary-atom table above,
    because each carries a lookback qualifier (any kind, the zero form
    included)."""
    text = (
        "insert_job: prod_f17lb\njob_type: c\ncommand: x\nmachine: m1\n\n"
        f"insert_job: cons_f17lb\njob_type: c\ncommand: y\nmachine: m1\ncondition: {atom_expr}\n"
    )
    o = oracle(text)
    # ON_ICE wakes referencers itself (SEM-20); the atom is true immediately,
    # so this one event is the whole scenario.
    o.feed(ev("ON_ICE", 0, job="prod_f17lb"))
    assert transitions(o, "cons_f17lb") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


_ICE_LOOKBACK_ATOMS = [
    ("s(prod_f17sw, 0)", True),
    ("d(prod_f17sw, 01.00)", True),
    ("n(prod_f17sw, 9999)", True),
    ("f(prod_f17sw, 0)", False),
    ("t(prod_f17sw, 9999)", False),
    ("e(prod_f17sw, 01.00) = 0", False),
]


@pytest.mark.parametrize(("atom_expr", "ordinary_true"), _ICE_LOOKBACK_ATOMS)
@pytest.mark.parametrize("switch", ["default", "true", "ordinary"])
def test_sem20_ice_lookback_switch_selects_the_q10_reading(
    atom_expr: str, ordinary_true: bool, switch: str
) -> None:
    """Q10, DL-252: the `ice-lookback` semantic switch. At its default, and
    when set to `true`, every lookback atom on a non-live iced predecessor
    reads true. Set to `ordinary`, the qualifier is dropped and the
    ordinary ON_ICE table applies: s, d, n true; f, t, exitcode false."""
    text = (
        "insert_job: prod_f17sw\njob_type: c\ncommand: x\nmachine: m1\n\n"
        f"insert_job: cons_f17sw\njob_type: c\ncommand: y\nmachine: m1\ncondition: {atom_expr}\n"
    )
    chosen = None if switch == "default" else resolve_switches({"ice-lookback": switch})
    o = oracle(text, semantics=chosen)
    o.feed(ev("ON_ICE", 0, job="prod_f17sw"))
    expect_true = ordinary_true if switch == "ordinary" else True
    expected = ["INACTIVE->STARTING", "STARTING->RUNNING"] if expect_true else []
    assert transitions(o, "cons_f17sw") == expected


def test_sem20_ice_lookback_ordinary_leaves_a_live_iced_job_alone() -> None:
    """DL-252 control: the switch changes only the non-live iced case. A
    RUNNING iced predecessor is still read by its real status, so a
    lookback s() on it stays false under `ordinary` until it ends."""
    text = (
        "insert_job: prod_f17swl\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_f17swl\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_f17swl, 0)\n"
    )
    o = oracle(text, semantics=resolve_switches({"ice-lookback": "ordinary"}))
    o.feed(ev("STARTJOB", 0, job="prod_f17swl"))
    o.feed(ev("ON_ICE", 1, job="prod_f17swl"))
    assert transitions(o, "cons_f17swl") == []


def test_sem20_ordinary_atom_on_an_undefined_iced_lookalike_stays_false() -> None:
    """Control: a condition naming a job the catalog does not have reads
    false forever (SEM-06), independent of ON_ICE -- icing an unrelated
    real job must not make the undefined reference true."""
    text = (
        "insert_job: cons_f17u\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(ghost_f17)\n\n"
        "insert_job: real_f17u\njob_type: c\ncommand: x\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="real_f17u"))
    o.feed(ev("STARTJOB", 1, job="cons_f17u"))
    assert transitions(o, "cons_f17u") == []


#: DL-254 fixtures: a producer p254 that is live when an operator event
#: lands, and two consumers that read its completion. The box shape makes
#: p254 a box whose one member m254 completes it.
_CONSUMERS_254 = (
    "insert_job: cs254\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(p254)\n\n"
    "insert_job: cf254\njob_type: c\ncommand: z\nmachine: m1\ncondition: f(p254)\n"
)
_PRODUCER_254 = {
    "job": "insert_job: p254\njob_type: c\ncommand: x\nmachine: m1\n\n",
    "box": (
        "insert_job: p254\njob_type: b\n\n"
        "insert_job: m254\njob_type: c\ncommand: w\nmachine: m1\nbox_name: p254\n\n"
    ),
}
_LIVE_254 = [
    ("job", "STARTING"),
    ("job", "RUNNING"),
    ("box", "STARTING"),
    ("box", "RUNNING"),
]


def _live_producer_254(shape: str, live: str) -> Oracle | EngineHarness:
    """RUNNING through a real start; STARTING through an injected STATUS,
    the only way a row rests in STARTING between events."""
    o = oracle(_PRODUCER_254[shape] + _CONSUMERS_254)
    if live == "RUNNING":
        o.feed(ev("STARTJOB", 0, job="p254"))
    else:
        o.feed(ev("STATUS", 0, job="p254", status="STARTING"))
    assert o.store.job["p254"].status == live
    return o


def _assert_ignored(o: Oracle | EngineHarness, kind: EventKind, job: str, at: float) -> None:
    """DL-254: the event changes nothing -- the row is identical, the trace
    gains one EVENT_IGNORED line and nothing else, and on the engine path
    no effect is planned."""
    row = o.store.job[job]
    trace_len = len(o.trace())
    effects = len(list(o.engine.outbox.effects())) if isinstance(o, EngineHarness) else 0
    o.feed(ev(kind, at, job=job))
    assert o.store.job[job] == row
    added = o.trace()[trace_len:]
    assert [(t.job, t.transition) for t in added] == [(job, "EVENT_IGNORED")]
    assert added[0].cause.startswith(f"sendevent {kind} ignored")
    if isinstance(o, EngineHarness):
        assert len(list(o.engine.outbox.effects())) == effects


def _complete_with_failure_254(o: Oracle | EngineHarness, shape: str, at: float) -> None:
    """The live run ends FAILURE; a running box ends through its member."""
    if shape == "box" and o.store.job["p254"].status == "RUNNING":
        o.feed(ev("STATUS", at, job="m254", status="FAILURE"))
    else:
        o.feed(ev("STATUS", at, job="p254", status="FAILURE"))
    assert o.store.job["p254"].status == "FAILURE"
    # the real failure reads normally: f() true, s() false. Had the event
    # taken effect, the ON_ICE table would read the reverse (DL-243).
    assert _status(o, "cf254", "cs254") == ["RUNNING", "INACTIVE"]


def test_sem20_ordinary_atom_on_a_live_iced_job_reads_the_real_in_flight_status() -> None:
    """Control: ON_ICE on a RUNNING job is ignored (DL-254), so there is no
    live iced job and s() reads the real in-flight RUNNING as false. The
    name predates DL-254, when the flag was set and read only at
    completion; DL-243 cites the name."""
    text = (
        "insert_job: prod_f17live\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_f17live\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_f17live)\n"
    )
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="prod_f17live"))
    o.feed(ev("ON_ICE", 1, job="prod_f17live"))
    assert not o.store.job["prod_f17live"].on_ice
    o.feed(ev("STARTJOB", 2, job="cons_f17live"))
    assert transitions(o, "cons_f17live") == []  # still RUNNING for real: s() false


@pytest.mark.parametrize(("shape", "live"), _LIVE_254)
def test_sem20_on_ice_on_a_live_job_is_ignored(shape: str, live: str) -> None:
    """SEM-20 (DL-254): "Change the Executable Status of a Job" (AutoSys
    24.2) on JOB_ON_ICE: "The event has no effect on jobs with a status of
    STARTING or RUNNING." Box or not, no flag is set; the later completion
    reads normally downstream, and the next plain start runs."""
    o = _live_producer_254(shape, live)
    _assert_ignored(o, "ON_ICE", "p254", 1)
    _complete_with_failure_254(o, shape, 2)
    o.feed(ev("STARTJOB", 3, job="p254"))
    assert o.store.job["p254"].status == "RUNNING"


def test_sem20_off_ice_later_reads_the_real_status_not_the_vendor_table() -> None:
    """Control: once OFF_ICE lifts the flag, an ordinary atom goes back to
    reading the job's real status -- f() becomes true here although the
    ordinary-atom table would have read it false while iced. cons_f17off is held
    across the producer's real FAILURE so that transition's own wake does
    not settle the question before icing is even in the picture."""
    text = (
        "insert_job: prod_f17off\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_f17off\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: f(prod_f17off)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="cons_f17off"))
    o.feed(ev("FORCE_STARTJOB", 1, job="prod_f17off"))
    o.feed(ev("STATUS", 2, job="prod_f17off", status="FAILURE"))
    o.feed(ev("ON_ICE", 3, job="prod_f17off"))
    o.feed(ev("OFF_HOLD", 4, job="cons_f17off"))  # SEM-21 re-attempt while prod stays iced
    assert transitions(o, "cons_f17off") == ["ON_HOLD", "OFF_HOLD"]  # ordinary f() false while iced
    o.feed(ev("OFF_ICE", 5, job="prod_f17off"))
    o.feed(ev("STARTJOB", 6, job="cons_f17off"))  # fresh attempt reads the real FAILURE
    assert transitions(o, "cons_f17off") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


# --------------------------------------------------------------------- 14. SEM-21 ON_HOLD


def test_sem21a_hold_blocks_downstream_the_held_jobs_own_status_never_changes() -> None:
    """T21a (SEM-21): a held job does not start even once its own condition
    is satisfied (nor via a direct manual STARTJOB attempt while held); its
    own status stays INACTIVE, and downstream conditions on it (s(held))
    never become true because it never actually runs."""
    text = (
        "insert_job: held21a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(trigger21a)\n\n"
        "insert_job: cons21a\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(held21a)\n\n"
        "insert_job: trigger21a\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="held21a"))
    o.feed(ev("STATUS", 1, job="trigger21a", status="SUCCESS"))
    assert transitions(o, "held21a") == ["ON_HOLD"]
    assert o.store.job["held21a"].status == "INACTIVE"
    o.feed(ev("STARTJOB", 2, job="held21a"))  # manual attempt while held: still blocked
    assert transitions(o, "held21a") == ["ON_HOLD"]
    assert transitions(o, "cons21a") == []


def test_sem21b_off_hold_runs_immediately_if_conditions_already_satisfied() -> None:
    """T21b (SEM-21): OFF_HOLD re-evaluates the held job's start immediately;
    if its condition became true while held, it runs right away (missed runs
    during hold collapse to at most one run)."""
    text = (
        "insert_job: held21b\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(trigger21b)\n\n"
        "insert_job: trigger21b\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="held21b"))
    o.feed(ev("STATUS", 1, job="trigger21b", status="SUCCESS"))
    o.feed(ev("OFF_HOLD", 2, job="held21b"))
    assert transitions(o, "held21b") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem21_held_member_prevents_box_completion() -> None:
    """T21 (SEM-21): inside a box, a held member holds the whole stream --
    the box cannot fold while a member-not-yet-run is ON_HOLD, even if every
    other member has completed. Once OFF_HOLD lets it run and complete, the
    box folds normally."""
    text = (
        "insert_job: box21\njob_type: b\n\n"
        "insert_job: mem_free21\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box21\n\n"
        "insert_job: mem_held21\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box21\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="mem_held21"))
    o.feed(ev("STARTJOB", 1, job="box21"))
    assert transitions(o, "mem_free21") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "mem_held21") == ["ON_HOLD"]
    o.feed(ev("STATUS", 2, job="mem_free21", status="SUCCESS"))
    assert transitions(o, "box21") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # still RUNNING
    o.feed(ev("OFF_HOLD", 3, job="mem_held21"))
    o.feed(ev("STATUS", 4, job="mem_held21", status="SUCCESS"))
    assert transitions(o, "box21") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


@pytest.mark.parametrize(("shape", "live"), _LIVE_254)
def test_sem21_on_hold_on_a_live_job_is_ignored(shape: str, live: str) -> None:
    """SEM-21 (DL-254): "Change the Executable Status of a Job" (AutoSys
    24.2) on JOB_ON_HOLD: "The event has no effect on jobs with a status of
    STARTING or RUNNING." Box or not, no flag is set; the later completion
    reads normally downstream, and the next plain start is not held."""
    o = _live_producer_254(shape, live)
    _assert_ignored(o, "ON_HOLD", "p254", 1)
    _complete_with_failure_254(o, shape, 2)
    o.feed(ev("STARTJOB", 3, job="p254"))
    assert o.store.job["p254"].status == "RUNNING"


@pytest.mark.parametrize("kind", ["ON_ICE", "ON_HOLD", "ON_NOEXEC"])
@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE"])
def test_sem21_events_on_a_completed_job_still_apply(kind: EventKind, status: str) -> None:
    """Control (DL-254): the vendor names STARTING and RUNNING only. On a
    completed job each event still sets its flag and records its own
    marker, and the next plain start does not run for real."""
    o = oracle(_PRODUCER_254["job"] + _CONSUMERS_254)
    o.feed(ev("STATUS", 0, job="p254", status=status))
    o.feed(ev(kind, 1, job="p254"))
    # ON_NOEXEC on FAILURE also moves the job to INACTIVE (DL-243)
    assert kind in transitions(o, "p254")
    assert "EVENT_IGNORED" not in transitions(o, "p254")
    row = o.store.job["p254"]
    assert (row.on_ice, row.on_hold, row.on_noexec) == (
        kind == "ON_ICE",
        kind == "ON_HOLD",
        kind == "ON_NOEXEC",
    )
    o.feed(ev("STARTJOB", 2, job="p254"))
    # iced or held: no start; noexec: the start bypasses to SUCCESS
    assert o.store.job["p254"].status != "RUNNING"


#: a lock holder h254 and a job q254 queued behind it
_QUEUED_254 = (
    "insert_resource: LOCK254\nres_type: R\namount: 1\n\n"
    "insert_job: h254\njob_type: c\ncommand: x\nmachine: m1\n"
    "resources: (LOCK254, QUANTITY=1)\n\n"
    "insert_job: q254\njob_type: c\ncommand: y\nmachine: m1\n"
    "resources: (LOCK254, QUANTITY=1)\n"
)


@pytest.mark.parametrize("kind", ["ON_ICE", "ON_HOLD"])
def test_sem21_events_on_a_queued_job_keep_their_handling(kind: EventKind) -> None:
    """DL-254: the vendor does not name QUE_WAIT, so a queued job keeps the
    existing handling. ON_ICE dequeues it to INACTIVE (DL-50, Qr5), ON_HOLD
    keeps it queued and held. ON_NOEXEC has its own test below."""
    o = oracle(_QUEUED_254)
    o.feed(ev("STARTJOB", 0, job="h254"))
    o.feed(ev("STARTJOB", 1, job="q254"))
    assert o.store.job["q254"].status == "QUE_WAIT"
    o.feed(ev(kind, 2, job="q254"))
    assert kind in transitions(o, "q254")
    assert "EVENT_IGNORED" not in transitions(o, "q254")
    row = o.store.job["q254"]
    assert (row.on_ice, row.on_hold) == (kind == "ON_ICE", kind == "ON_HOLD")
    assert row.status == ("INACTIVE" if kind == "ON_ICE" else "QUE_WAIT")


# -------------------------------------------- 14b. SEM-24 status: at definition time


def test_sem24a_initial_on_hold_blocks_then_off_hold_releases() -> None:
    """T24a (SEM-24): a job defined with `status: ON_HOLD` behaves exactly as
    if it had been inserted and immediately held -- its condition satisfying
    does not start it (and leaves no trace entry: definition state, not a
    transition); OFF_HOLD with the condition already satisfied runs it
    immediately (SEM-21 collapse-to-one)."""
    text = (
        "insert_job: seed24\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: held24\njob_type: c\ncommand: y\nmachine: m1\n"
        "status: ON_HOLD\ncondition: s(seed24)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="seed24", status="SUCCESS"))
    assert transitions(o, "held24") == []  # held at definition: no start, no trace
    assert o.store.job["held24"].status == "INACTIVE"
    o.feed(ev("OFF_HOLD", 5, job="held24"))
    assert transitions(o, "held24") == [
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem24b_initial_on_ice_satisfies_downstream_and_never_starts() -> None:
    """T24b (SEM-24/SEM-20): a job defined with `status: ON_ICE` is excised --
    a downstream job conditioned on it starts as though the iced job
    succeeded, and the iced job itself never starts."""
    text = (
        "insert_job: iced24\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: down24\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(iced24)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="down24"))
    assert transitions(o, "down24") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STARTJOB", 1, job="iced24"))
    assert transitions(o, "iced24") == []  # iced at definition: never starts


# -------------------------------------------------------------------- 15. SEM-22 ON_NOEXEC


def test_sem22_noexec_bypass_job_and_box_member_fold_normally() -> None:
    """T22 (SEM-22): an ON_NOEXEC job goes straight to SUCCESS on its start
    attempt, with no STARTING/RUNNING in its trace; downstream fires
    normally. A box containing a noexec member bypasses that member to
    SUCCESS as its turn to start comes up, and folds (SEM-11) normally."""
    solo_text = (
        "insert_job: noexec_job22\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons22\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(noexec_job22)\n"
    )
    solo = oracle(solo_text)
    solo.feed(ev("ON_NOEXEC", 0, job="noexec_job22"))
    solo.feed(ev("STARTJOB", 1, job="noexec_job22"))
    # ON_NOEXEC marker, then straight to SUCCESS -- no STARTING/RUNNING in between
    assert transitions(solo, "noexec_job22") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    assert transitions(solo, "cons22") == ["INACTIVE->STARTING", "STARTING->RUNNING"]

    box_text = (
        "insert_job: box22\njob_type: b\n\n"
        "insert_job: mem_noexec22\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box22\n\n"
        "insert_job: mem_normal22\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box22\n"
    )
    boxed = oracle(box_text)
    boxed.feed(ev("ON_NOEXEC", 0, job="mem_noexec22"))
    boxed.feed(ev("STARTJOB", 1, job="box22"))
    assert transitions(boxed, "mem_noexec22") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    boxed.feed(ev("STATUS", 2, job="mem_normal22", status="SUCCESS"))
    assert transitions(boxed, "box22") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem22_noexec_box_goes_running_and_every_member_bypasses() -> None:
    """T22b (SEM-22): "Box in ON_NOEXEC scheduled to run -> goes RUNNING,
    members are bypassed to SUCCESS as their conditions are met." The box
    itself does NOT bypass; each member does, including a member whose
    condition is only met by an earlier member's bypass. Each member also
    took the flag itself when the box was put ON_NOEXEC (DL-254). The box then folds normally (SEM-11) -- it waits for
    every member's bypass, it does not complete on the first one."""
    text = (
        "insert_job: box22b\njob_type: b\n\n"
        "insert_job: mem_a22b\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box22b\n\n"
        "insert_job: mem_b22b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box22b\n"
        "condition: s(mem_a22b)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="box22b"))
    o.feed(ev("STARTJOB", 1, job="box22b"))
    assert transitions(o, "box22b") == [
        "ON_NOEXEC",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    # each member bypassed: SUCCESS with no STARTING/RUNNING of its own
    assert transitions(o, "mem_a22b") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    assert transitions(o, "mem_b22b") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    bypass = [t for t in o.trace() if t.job == "mem_b22b"][-1]
    assert "ON_NOEXEC bypass" in bypass.cause


def test_sem22_noexec_box_member_whose_condition_never_fires_keeps_the_box_running() -> None:
    """T22b (SEM-22 x SEM-11 literal, DL-13): members bypass "as their
    conditions are met" -- a member whose condition never becomes true is
    never bypassed, so the ON_NOEXEC box stays RUNNING just as it would for
    a member that never ran. The bypass is a start, not an exemption from
    the fold."""
    text = (
        "insert_job: box22c\njob_type: b\n\n"
        "insert_job: mem_a22c\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box22c\n\n"
        "insert_job: mem_b22c\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box22c\n"
        "condition: s(never22c)\n\n"
        "insert_job: never22c\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="box22c"))
    o.feed(ev("STARTJOB", 1, job="box22c"))
    assert transitions(o, "mem_a22c") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    assert transitions(o, "mem_b22c") == ["ON_NOEXEC"]
    assert transitions(o, "box22c") == ["ON_NOEXEC", "INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 2, job="never22c", status="SUCCESS"))  # the condition finally fires
    assert transitions(o, "mem_b22c") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    assert transitions(o, "box22c") == [
        "ON_NOEXEC",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem22_bypass_is_the_ticks_run_so_no_must_start_alarm_follows() -> None:
    """T22b (SEM-22 x SEM-34): the bypass IS the tick's run (the Q3/DL-54
    reading the arm already used), so SEM-34's "no new run has begun by
    tick+offset" is satisfied and no MUST_START_ALARM is emitted. A
    deliberately bypassed job does not alarm for not running."""
    text = (
        "insert_job: nx22\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\nmust_start_times: +5\n\n'
        "insert_job: dummy22\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="nx22"))
    o.feed(ev("STARTJOB", 0, job="nx22"))
    assert transitions(o, "nx22") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    emitted = o.feed(ev("STATUS", 6, job="dummy22", status="SUCCESS"))  # past the +5 deadline
    assert all(e.kind != "MUST_START_ALARM" for e in emitted)
    assert "MUST_START_ALARM" not in transitions(o, "nx22")


def test_sem22_noexec_box_bypasses_a_nested_member_box_level_by_level() -> None:
    """T22b (SEM-22, nesting): the box sentence is applied at each box
    level, so a member BOX of an ON_NOEXEC box goes RUNNING too and its own
    members bypass. The dry-run walks the whole tree; nothing runs."""
    text = (
        "insert_job: outer22d\njob_type: b\n\n"
        "insert_job: inner22d\njob_type: b\nbox_name: outer22d\n\n"
        "insert_job: grand22d\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner22d\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="outer22d"))
    o.feed(ev("STARTJOB", 1, job="outer22d"))
    assert transitions(o, "grand22d") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    for box in ("inner22d", "outer22d"):
        assert transitions(o, box)[-3:] == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
            "RUNNING->SUCCESS",
        ]


def test_sem22_noexec_on_a_failed_job_transitions_to_inactive() -> None:
    """DL-243 REWRITE: the read-time projection is replaced by an
    EVENT-TIME transition. Job States page: "the scheduler places the job
    in the ON_NOEXEC status and the effect is the same as sending the
    CHANGE_STATUS event to INACTIVE for the job. The scheduler does not
    immediately schedule downstream jobs ... Instead, the scheduler
    evaluates the conditions of downstream dependent jobs as if the
    predecessor job is set to the INACTIVE status." A FAILURE job put
    ON_NOEXEC is moved to INACTIVE through DL-242's operator-INACTIVE path
    (`_inject_inactive`), exit code cleared: the STORED row changes, not
    just the read. f()/t()/d()/exitcode() read false, n() reads true, s()
    stays false. Each watcher is held across the producer's real FAILURE
    transition so that edge's own wake does not settle anything before
    ON_NOEXEC is even sent."""
    text = (
        "insert_job: prod_nx_f\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_nx_f_f\njob_type: c\ncommand: a\nmachine: m1\ncondition: f(prod_nx_f)\n\n"
        "insert_job: watch_nx_f_t\njob_type: c\ncommand: b\nmachine: m1\ncondition: t(prod_nx_f)\n\n"
        "insert_job: watch_nx_f_d\njob_type: c\ncommand: c\nmachine: m1\ncondition: d(prod_nx_f)\n\n"
        "insert_job: watch_nx_f_n\njob_type: c\ncommand: d\nmachine: m1\ncondition: n(prod_nx_f)\n\n"
        "insert_job: watch_nx_f_s\njob_type: c\ncommand: e\nmachine: m1\ncondition: s(prod_nx_f)\n\n"
        "insert_job: watch_nx_f_e\njob_type: c\ncommand: f\nmachine: m1\ncondition: e(prod_nx_f) = 7\n"
    )
    watchers = (
        "watch_nx_f_f",
        "watch_nx_f_t",
        "watch_nx_f_d",
        "watch_nx_f_n",
        "watch_nx_f_s",
        "watch_nx_f_e",
    )
    o = oracle(text)
    for w in watchers:
        o.feed(ev("ON_HOLD", 0, job=w))
    o.feed(ev("FORCE_STARTJOB", 1, job="prod_nx_f"))
    o.feed(ev("STATUS", 2, job="prod_nx_f", status="FAILURE", exit_code=7))
    o.feed(ev("ON_NOEXEC", 3, job="prod_nx_f"))
    assert o.store.job["prod_nx_f"].status == "INACTIVE"  # the stored row moved
    assert o.store.job["prod_nx_f"].exit_code is None  # cleared, not just unread
    assert "ON_NOEXEC" in transitions(o, "prod_nx_f")
    assert "FAILURE->INACTIVE" in transitions(o, "prod_nx_f")
    for w in watchers:
        o.feed(ev("OFF_HOLD", 4, job=w))
    for w in ("watch_nx_f_f", "watch_nx_f_t", "watch_nx_f_d", "watch_nx_f_s", "watch_nx_f_e"):
        assert transitions(o, w) == ["ON_HOLD", "OFF_HOLD"]  # f/t/d/s/exitcode all false
    assert transitions(o, "watch_nx_f_n") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]  # n() reads true


def test_sem22_noexec_on_a_terminated_job_transitions_to_inactive() -> None:
    """DL-243 REWRITE, TERMINATED twin: a killed job put ON_NOEXEC
    moves to INACTIVE (exit code cleared) the same way as the FAILURE
    case -- f()/t()/d()/exitcode() false, n() true, s() false."""
    text = (
        "insert_job: prod_nx_t\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_nx_t_f\njob_type: c\ncommand: a\nmachine: m1\ncondition: f(prod_nx_t)\n\n"
        "insert_job: watch_nx_t_t\njob_type: c\ncommand: b\nmachine: m1\ncondition: t(prod_nx_t)\n\n"
        "insert_job: watch_nx_t_d\njob_type: c\ncommand: c\nmachine: m1\ncondition: d(prod_nx_t)\n\n"
        "insert_job: watch_nx_t_n\njob_type: c\ncommand: d\nmachine: m1\ncondition: n(prod_nx_t)\n\n"
        "insert_job: watch_nx_t_s\njob_type: c\ncommand: e\nmachine: m1\ncondition: s(prod_nx_t)\n\n"
        "insert_job: watch_nx_t_e\njob_type: c\ncommand: f\nmachine: m1\ncondition: e(prod_nx_t) = 0\n"
    )
    watchers = (
        "watch_nx_t_f",
        "watch_nx_t_t",
        "watch_nx_t_d",
        "watch_nx_t_n",
        "watch_nx_t_s",
        "watch_nx_t_e",
    )
    o = oracle(text)
    for w in watchers:
        o.feed(ev("ON_HOLD", 0, job=w))
    o.feed(ev("FORCE_STARTJOB", 1, job="prod_nx_t"))
    o.feed(ev("KILLJOB", 2, job="prod_nx_t"))
    o.feed(ev("ON_NOEXEC", 3, job="prod_nx_t"))
    assert o.store.job["prod_nx_t"].status == "INACTIVE"
    assert o.store.job["prod_nx_t"].exit_code is None
    for w in watchers:
        o.feed(ev("OFF_HOLD", 4, job=w))
    for w in ("watch_nx_t_f", "watch_nx_t_t", "watch_nx_t_d", "watch_nx_t_s", "watch_nx_t_e"):
        assert transitions(o, w) == ["ON_HOLD", "OFF_HOLD"]  # f/t/d/s/exitcode all false
    assert transitions(o, "watch_nx_t_n") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]  # n() reads true


def test_sem22_noexec_keeps_a_success_visible() -> None:
    """DL-243 control: SUCCESS is the documented exception -- "the job
    retains its current status" -- so a SUCCESS job put ON_NOEXEC is left
    alone (no `_inject_inactive` call): s() stays true, f() stays false."""
    text = (
        "insert_job: prod_nx_s\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_nx_s_s\njob_type: c\ncommand: a\nmachine: m1\ncondition: s(prod_nx_s)\n\n"
        "insert_job: watch_nx_s_f\njob_type: c\ncommand: b\nmachine: m1\ncondition: f(prod_nx_s)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="watch_nx_s_s"))
    o.feed(ev("ON_HOLD", 0, job="watch_nx_s_f"))
    o.feed(ev("FORCE_STARTJOB", 1, job="prod_nx_s"))
    o.feed(ev("STATUS", 2, job="prod_nx_s", status="SUCCESS"))
    o.feed(ev("ON_NOEXEC", 3, job="prod_nx_s"))
    assert o.store.job["prod_nx_s"].status == "SUCCESS"  # untouched
    o.feed(ev("OFF_HOLD", 4, job="watch_nx_s_s"))
    o.feed(ev("OFF_HOLD", 4, job="watch_nx_s_f"))
    assert transitions(o, "watch_nx_s_s") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    assert transitions(o, "watch_nx_s_f") == ["ON_HOLD", "OFF_HOLD"]


def test_sem22_noexec_bypasses_to_success_on_its_next_start() -> None:
    """A FAILURE job put ON_NOEXEC moves to INACTIVE but keeps its
    `on_noexec` flag; its NEXT start bypasses to SUCCESS (SEM-22) instead
    of running, and an s() consumer then starts against that SUCCESS."""
    text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_s\njob_type: c\ncommand: a\nmachine: m1\ncondition: s(p)\n"
    )
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="p"))
    o.feed(ev("STATUS", 1, job="p", status="FAILURE"))
    o.feed(ev("ON_NOEXEC", 2, job="p"))
    assert o.store.job["p"].status == "INACTIVE"
    assert o.store.job["p"].on_noexec
    o.feed(ev("STARTJOB", 3, job="p"))  # next start: ON_NOEXEC bypass, not a real run
    assert transitions(o, "p")[-2:] == ["FAILURE->INACTIVE", "INACTIVE->SUCCESS"]
    assert o.store.job["p"].on_noexec  # the flag persists across the bypass
    assert transitions(o, "watch_s") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem22_noexec_off_noexec_then_release_a_held_f_consumer_stays_blocked() -> None:
    """Fail p, ON_NOEXEC, OFF_NOEXEC, then release a held f(p) consumer: it
    does not start. p is really INACTIVE by the time OFF_NOEXEC runs
    (Events page: OFF_NOEXEC "places the job in the INACTIVE, ACTIVATED, or
    SUCCESS status", and here it already is), so f(p) reads false
    normally."""
    text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_f\njob_type: c\ncommand: a\nmachine: m1\ncondition: f(p)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="watch_f"))
    o.feed(ev("FORCE_STARTJOB", 1, job="p"))
    o.feed(ev("STATUS", 2, job="p", status="FAILURE"))
    o.feed(ev("ON_NOEXEC", 3, job="p"))
    o.feed(ev("OFF_NOEXEC", 4, job="p"))
    o.feed(ev("OFF_HOLD", 5, job="watch_f"))
    assert transitions(o, "watch_f") == ["ON_HOLD", "OFF_HOLD"]  # stays blocked
    assert o.store.job["p"].status == "INACTIVE"
    assert o.store.job["p"].exit_code is None


def test_sem22_noexec_while_running_then_real_failure_is_not_hidden() -> None:
    """ON_NOEXEC sent while p is RUNNING is ignored (DL-254): no flag, no
    transition. The real failure that follows is therefore not hidden:
    f(p) sees it fresh and starts. watch_n started
    earlier, before p was even forced, because p's initial INACTIVE already reads
    n(p) true; with no completion script of its own it is still running
    that first attempt, so the later true-again edge from p's failure finds
    it already live and refuses a restart -- not because n(p) reads false."""
    text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_f\njob_type: c\ncommand: a\nmachine: m1\ncondition: f(p)\n\n"
        "insert_job: watch_n\njob_type: c\ncommand: b\nmachine: m1\ncondition: n(p)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="watch_n"))  # n(p) already true: p is INACTIVE
    assert o.store.job["watch_n"].status == "RUNNING"
    o.feed(ev("FORCE_STARTJOB", 1, job="p"))
    o.feed(ev("ON_NOEXEC", 2, job="p"))  # p is RUNNING: ignored (DL-254)
    assert o.store.job["p"].status == "RUNNING"
    assert not o.store.job["p"].on_noexec
    assert transitions(o, "p")[-1] == "EVENT_IGNORED"
    o.feed(ev("STATUS", 3, job="p", status="FAILURE"))
    assert o.store.job["p"].status == "FAILURE"  # the real failure is not hidden
    assert transitions(o, "watch_f") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "watch_n") == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # no restart


def test_sem22_noexec_on_an_iced_job_is_ignored_and_the_job_stays_failure() -> None:
    """DL-243, DL-254: "Change the Executable Status of a Job" page -- "The
    scheduler ignores the JOB_ON_NOEXEC event, if sent to: A non-box job
    that is in the STARTING, RUNNING, or ON_ICE status." p fails, is put
    ON_ICE, then ON_NOEXEC: the event is ignored, no flag is set, and p
    stays FAILURE. Once OFF_ICE lifts the ice, a held f(p) consumer
    released afterward starts normally against the real FAILURE."""
    text = (
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watch_f\njob_type: c\ncommand: a\nmachine: m1\ncondition: f(p)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="watch_f"))
    o.feed(ev("FORCE_STARTJOB", 1, job="p"))
    o.feed(ev("STATUS", 2, job="p", status="FAILURE"))
    o.feed(ev("ON_ICE", 3, job="p"))
    o.feed(ev("ON_NOEXEC", 4, job="p"))
    assert o.store.job["p"].status == "FAILURE"  # the event is ignored while iced
    assert not o.store.job["p"].on_noexec
    o.feed(ev("OFF_ICE", 5, job="p"))
    o.feed(ev("OFF_HOLD", 6, job="watch_f"))
    assert transitions(o, "watch_f") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem22_noexec_on_a_failed_box_member_completes_the_box() -> None:
    """A member of a RUNNING box put ON_NOEXEC after failing completes the
    box. `box_failure` is specified but never met, so the member's real
    FAILURE alone leaves the box hung RUNNING (SEM-12 third bullet, the
    specified-but-unmet-override case). ON_NOEXEC settles the member to
    INACTIVE (DL-242/DL-243), which resolves it "as if ... SUCCESS"
    (SEM-11) and completes the box."""
    text = (
        "insert_job: box1\njob_type: b\nbox_failure: v(NEVERSET) = 1\n\n"
        "insert_job: mem1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box1"))
    o.feed(ev("STATUS", 1, job="mem1", status="FAILURE"))
    assert o.store.job["box1"].status == "RUNNING"  # suppressed default: hung
    o.feed(ev("ON_NOEXEC", 2, job="mem1"))
    assert o.store.job["mem1"].status == "INACTIVE"
    assert o.store.job["box1"].status == "SUCCESS"  # the box completes


@pytest.mark.parametrize(
    ("shape", "live"), [("job", "STARTING"), ("job", "RUNNING"), ("box", "RUNNING")]
)
def test_sem22_on_noexec_on_a_live_job_is_ignored(shape: str, live: str) -> None:
    """SEM-22 (DL-254): "Change the Executable Status of a Job" (AutoSys
    24.2): "The scheduler ignores the JOB_ON_NOEXEC event, if sent to: A
    non-box job that is in the STARTING, RUNNING, or ON_ICE status; A box
    job that is in the ON_ICE or RUNNING status." No flag is set, so the
    real failure reads normally and the next start runs instead of
    bypassing to SUCCESS."""
    o = _live_producer_254(shape, live)
    _assert_ignored(o, "ON_NOEXEC", "p254", 1)
    _complete_with_failure_254(o, shape, 2)
    o.feed(ev("STARTJOB", 3, job="p254"))
    assert o.store.job["p254"].status == "RUNNING"
    if shape == "box":
        assert o.store.job["m254"].status == "RUNNING"  # not bypassed


def test_sem22_on_noexec_on_a_starting_box_still_sets_the_flag() -> None:
    """Control (DL-254): the vendor's box list names ON_ICE and RUNNING, not
    STARTING, so a box resting in STARTING (an injected STATUS) is not
    ignored. It takes the flag and the box INACTIVE path, like any box."""
    o = _live_producer_254("box", "STARTING")
    o.feed(ev("ON_NOEXEC", 1, job="p254"))
    assert transitions(o, "p254")[-2:] == ["ON_NOEXEC", "STARTING->INACTIVE"]
    assert o.store.job["p254"].on_noexec
    assert o.store.job["m254"].on_noexec


@pytest.mark.parametrize("shape", ["job", "box"])
def test_sem22_on_noexec_on_an_iced_job_is_ignored(shape: str) -> None:
    """SEM-22 (DL-254): ON_NOEXEC on an iced job or box is ignored: "The
    JOB_ON_NOEXEC event does not supersede the JOB_ON_ICE event and does not
    overwrite the ON_ICE status with the ON_NOEXEC status." No noexec flag
    survives the ice, so after OFF_ICE the next start runs for real."""
    o = oracle(_PRODUCER_254[shape] + _CONSUMERS_254)
    o.feed(ev("ON_ICE", 0, job="p254"))
    _assert_ignored(o, "ON_NOEXEC", "p254", 1)
    assert not o.store.job["p254"].on_noexec
    o.feed(ev("OFF_ICE", 2, job="p254"))
    o.feed(ev("STARTJOB", 3, job="p254"))
    assert o.store.job["p254"].status == "RUNNING"
    if shape == "box":
        assert o.store.job["m254"].status == "RUNNING"  # not bypassed


def test_sem22_on_noexec_on_a_running_box_does_not_bypass_its_waiting_members() -> None:
    """SEM-22 (DL-254): ON_NOEXEC on a RUNNING box is ignored, so a member
    still waiting on its condition runs for real when the condition fires,
    instead of bypassing to SUCCESS."""
    text = (
        "insert_job: b254\njob_type: b\n\n"
        "insert_job: a254\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b254\n\n"
        "insert_job: w254\njob_type: c\ncommand: y\nmachine: m1\nbox_name: b254\n"
        "condition: s(a254)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="b254"))
    assert _status(o, "b254", "a254", "w254") == ["RUNNING", "RUNNING", "INACTIVE"]
    _assert_ignored(o, "ON_NOEXEC", "b254", 1)
    o.feed(ev("STATUS", 2, job="a254", status="SUCCESS"))
    assert transitions(o, "w254") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def _assert_out_of_the_queue(o: Oracle | EngineHarness, status: str) -> None:
    row = o.store.job["q254"]
    assert (row.status, row.on_noexec, row.on_hold) == (status, True, False)
    assert row.reservations == ()
    assert row.waiter_seq is None
    assert "QUE_WAIT->INACTIVE" in transitions(o, "q254")
    assert o.store.job["h254"].status == "RUNNING"  # still holds the lock
    if isinstance(o, EngineHarness):
        assert [e.job for e in o.engine.outbox.effects() if e.kind == "SPAWN"] == ["h254"]


def test_sem22_on_noexec_on_a_queued_job_takes_it_out_of_the_queue() -> None:
    """SEM-22 (DL-254): "If the job is in the QUEWAIT or RESWAIT status, the
    scheduler removes the job from the load balancing and resource wait
    queues before placing it in the ON_NOEXEC status." The job leaves the
    queue for INACTIVE with the flag and no reservation. Its start is then
    retried: it met its starting conditions, so it bypasses to SUCCESS at
    once, and no SPAWN is ever planned for it."""
    o = oracle(_QUEUED_254)
    o.feed(ev("STARTJOB", 0, job="h254"))
    o.feed(ev("STARTJOB", 1, job="q254"))
    assert o.store.job["q254"].status == "QUE_WAIT"
    o.feed(ev("ON_NOEXEC", 2, job="q254"))
    assert transitions(o, "q254")[-2:] == ["QUE_WAIT->INACTIVE", "INACTIVE->SUCCESS"]
    _assert_out_of_the_queue(o, "SUCCESS")


def test_sem22_a_dequeued_noexec_job_whose_condition_went_false_waits_for_it() -> None:
    """SEM-22 (DL-254): the retry reads the conditions now. q254's condition
    turned false while it queued (conditions are not re-checked in the
    queue, Qr6), so after ON_NOEXEC it waits INACTIVE and bypasses when the
    condition fires again."""
    o = oracle(_QUEUED_254 + "condition: s(x254)\n\ninsert_job: x254\njob_type: c\ncommand: v\n")
    o.feed(ev("STARTJOB", 0, job="h254"))
    o.feed(ev("STATUS", 1, job="x254", status="SUCCESS"))
    assert o.store.job["q254"].status == "QUE_WAIT"
    o.feed(ev("STATUS", 2, job="x254", status="FAILURE"))
    o.feed(ev("ON_NOEXEC", 3, job="q254"))
    _assert_out_of_the_queue(o, "INACTIVE")
    o.feed(ev("STATUS", 4, job="x254", status="SUCCESS"))
    assert transitions(o, "q254")[-1] == "INACTIVE->SUCCESS"


def test_sem22_on_noexec_supersedes_the_hold_on_a_queued_job() -> None:
    """SEM-22 (DL-254): "The JOB_ON_NOEXEC event supersedes the JOB_ON_HOLD
    event effectively overwriting the ON_HOLD status with the ON_NOEXEC
    status." A held queued job loses the hold, recorded as an OFF_HOLD,
    leaves the queue and bypasses."""
    o = oracle(_QUEUED_254)
    o.feed(ev("STARTJOB", 0, job="h254"))
    o.feed(ev("STARTJOB", 1, job="q254"))
    o.feed(ev("ON_HOLD", 2, job="q254"))
    o.feed(ev("ON_NOEXEC", 3, job="q254"))
    assert transitions(o, "q254")[-5:] == [
        "ON_HOLD",
        "ON_NOEXEC",
        "OFF_HOLD",
        "QUE_WAIT->INACTIVE",
        "INACTIVE->SUCCESS",
    ]
    _assert_out_of_the_queue(o, "SUCCESS")


@pytest.mark.parametrize("met", [True, False], ids=["condition-met", "condition-unmet"])
def test_sem22_on_noexec_supersedes_on_hold(met: bool) -> None:
    """SEM-22 (DL-254): ON_NOEXEC on a held job clears the hold, recorded as
    an OFF_HOLD with a cause naming ON_NOEXEC, and retries the start as
    OFF_HOLD does. cs254 waits on s(p254): with the condition met it
    bypasses at once; otherwise it waits, unheld, and bypasses when the
    condition fires."""
    o = oracle(_PRODUCER_254["job"] + _CONSUMERS_254)
    o.feed(ev("ON_HOLD", 0, job="cs254"))
    if met:
        o.feed(ev("STATUS", 1, job="p254", status="SUCCESS"))
        assert o.store.job["cs254"].status == "INACTIVE"  # held
    o.feed(ev("ON_NOEXEC", 2, job="cs254"))
    assert transitions(o, "cs254")[1:3] == ["ON_NOEXEC", "OFF_HOLD"]
    clear = [t for t in o.trace() if t.job == "cs254" and t.transition == "OFF_HOLD"]
    assert clear[0].cause == "ON_NOEXEC supersedes ON_HOLD (SEM-22, DL-254)"
    row = o.store.job["cs254"]
    assert (row.on_hold, row.on_noexec) == (False, True)
    if not met:
        assert row.status == "INACTIVE"
        o.feed(ev("STATUS", 3, job="p254", status="SUCCESS"))
    assert transitions(o, "cs254")[-1] == "INACTIVE->SUCCESS"


def test_sem22_on_noexec_on_a_held_member_bypasses_and_completes_the_box() -> None:
    """SEM-22 (DL-254): box B runs a, and h waits on s(a) under a hold; d
    outside waits on s(h). After a succeeds, ON_NOEXEC h clears the hold
    and h bypasses at once, so B completes and d starts."""
    o = oracle(
        "insert_job: b256\njob_type: b\n\n"
        "insert_job: a256\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b256\n\n"
        "insert_job: h256\njob_type: c\ncommand: y\nmachine: m1\nbox_name: b256\n"
        "condition: s(a256)\n\n"
        "insert_job: d256\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(h256)\n"
    )
    o.feed(ev("ON_HOLD", 0, job="h256"))
    o.feed(ev("STARTJOB", 1, job="b256"))
    o.feed(ev("STATUS", 2, job="a256", status="SUCCESS"))
    assert _status(o, "b256", "h256", "d256") == ["RUNNING", "INACTIVE", "INACTIVE"]
    o.feed(ev("ON_NOEXEC", 3, job="h256"))
    assert _status(o, "b256", "h256", "d256") == ["SUCCESS", "SUCCESS", "RUNNING"]


def test_sem22_on_noexec_on_a_waiting_subbox_resolves_its_running_parent() -> None:
    """SEM-22 (DL-254, DL-242): the box event is CHANGE_STATUS INACTIVE on
    the box. outer runs; its only member, inner, waits on a false
    condition, so its whole tree is already INACTIVE. ON_NOEXEC inner still
    runs the INACTIVE->INACTIVE transition, so outer records the resolution
    and completes, as it does under CHANGE_STATUS inner INACTIVE."""
    o = oracle(
        "insert_job: outer257\njob_type: b\n\n"
        "insert_job: inner257\njob_type: b\nbox_name: outer257\ncondition: s(never257)\n\n"
        "insert_job: leaf257\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner257\n\n"
        "insert_job: never257\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o.feed(ev("STARTJOB", 0, job="outer257"))
    assert _status(o, "outer257", "inner257", "leaf257") == ["RUNNING", "INACTIVE", "INACTIVE"]
    o.feed(ev("ON_NOEXEC", 1, job="inner257"))
    assert transitions(o, "inner257")[-1] == "INACTIVE->INACTIVE"
    assert o.store.job["outer257"].status == "SUCCESS"


def test_sem22_on_noexec_on_a_box_clears_the_exit_code_of_an_inactive_member() -> None:
    """SEM-22 (DL-254): the box event clears the exit code on every row it
    covers, a member already INACTIVE included. m failed with exit 7 and
    was set INACTIVE, keeping the code; after ON_NOEXEC on its box a held
    e(m) = 7 consumer released later does not start."""
    o = oracle(
        "insert_job: b258\njob_type: b\n\n"
        "insert_job: m258\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b258\n\n"
        "insert_job: c258\njob_type: c\ncommand: y\nmachine: m1\ncondition: e(m258) = 7\n"
    )
    o.feed(ev("ON_HOLD", 0, job="c258"))
    o.feed(ev("STARTJOB", 1, job="b258"))
    o.feed(ev("STATUS", 2, job="m258", exit_code=7))
    o.feed(ev("STATUS", 3, job="m258", status="INACTIVE"))
    assert (o.store.job["m258"].status, o.store.job["m258"].exit_code) == ("INACTIVE", 7)
    o.feed(ev("ON_NOEXEC", 4, job="b258"))
    assert o.store.job["m258"].exit_code is None
    o.feed(ev("OFF_HOLD", 5, job="c258"))
    assert o.store.job["c258"].status == "INACTIVE"


@pytest.mark.parametrize("shape", ["job", "box"])
def test_sem22_the_release_retry_does_not_repeat_a_start_the_event_already_made(
    shape: str,
) -> None:
    """SEM-22 (DL-254): q waits on e(a) = 7 and a on n(q). ON_NOEXEC on the
    held, failed q clears the hold and moves q INACTIVE; that wakes a, and
    each of a's restart transitions re-fires e(a) = 7, so q bypasses (a box
    q runs its whole bypass cycle) once per transition, as any consumer
    re-runs on a fresh satisfaction (DL-13). The release retry that follows
    sees q's run number moved and adds no start of its own."""
    q = (
        "insert_job: q259\njob_type: b\ncondition: e(a259) = 7\n\n"
        "insert_job: qm259\njob_type: c\ncommand: w\nmachine: m1\nbox_name: q259\n\n"
        if shape == "box"
        else "insert_job: q259\njob_type: c\ncommand: x\nmachine: m1\ncondition: e(a259) = 7\n\n"
    )
    o = oracle(q + "insert_job: a259\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(q259)\n")
    o.feed(ev("ON_HOLD", 0, job="q259"))
    o.feed(ev("STATUS", 1, job="q259", status="FAILURE"))
    o.feed(ev("STATUS", 2, job="a259", status="SUCCESS", exit_code=7))
    before = len(o.trace())
    run = o.store.job["q259"].run_number
    o.feed(ev("ON_NOEXEC", 3, job="q259"))
    event = o.trace()[before:]
    a_moves = [t for t in event if t.job == "a259"]
    assert [t.transition for t in a_moves] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    q_starts = [
        t
        for t in event
        if t.job == "q259"
        and "bypass" in t.cause
        or (t.job == "q259" and t.transition.endswith("->STARTING"))
    ]
    assert len(q_starts) == 2  # one per a259 transition
    assert all("status of 'a259' changed" in t.cause for t in q_starts)
    assert o.store.job["q259"].run_number == run + 2
    assert o.store.job["q259"].status == "SUCCESS"


def _noexec_box_tree() -> Oracle | EngineHarness:
    """ob255 > {m255, ib255 > {g255, k255}}; f(m255) watches outside."""
    return oracle(
        "insert_job: ob255\njob_type: b\nbox_failure: f(m255)\n\n"
        "insert_job: m255\njob_type: c\ncommand: x\nmachine: m1\nbox_name: ob255\n\n"
        "insert_job: ib255\njob_type: b\nbox_name: ob255\n\n"
        "insert_job: g255\njob_type: c\ncommand: y\nmachine: m1\nbox_name: ib255\n\n"
        "insert_job: k255\njob_type: c\ncommand: z\nmachine: m1\nbox_name: ib255\n\n"
        "insert_job: wf255\njob_type: c\ncommand: w\nmachine: m1\ncondition: f(m255)\n"
    )


def test_sem22_on_noexec_on_a_box_cascades_inactive_and_flags_every_level() -> None:
    """SEM-22 (DL-254): "If you send the JOB_ON_NOEXEC event to a box, the
    effect is the same as sending the CHANGE_STATUS event to INACTIVE for a
    box. The box enters the ON_NOEXEC status and the scheduler sets the
    status of all jobs in the box (including all jobs contained in lower
    level boxes within the box) at all levels to ON_NOEXEC." A completed
    box goes INACTIVE with every job it holds (the SEM-18 cascade, exit
    codes cleared), and every level takes the flag. A held member loses its
    hold. A failed member reads INACTIVE, as DL-243 would have it alone.
    On the engine path nothing is planned."""
    o = _noexec_box_tree()
    o.feed(ev("ON_HOLD", 0, job="k255"))
    o.feed(ev("STARTJOB", 1, job="ob255"))
    o.feed(ev("STATUS", 2, job="g255", status="SUCCESS"))
    o.feed(ev("STATUS", 3, job="m255", exit_code=1))  # FAILURE; box_failure fires
    assert _status(o, "ob255", "m255", "ib255", "g255", "k255") == [
        "FAILURE",
        "FAILURE",
        "RUNNING",
        "SUCCESS",
        "INACTIVE",
    ]
    o.feed(ev("STATUS", 4, job="ib255", status="SUCCESS"))
    effects = len(list(o.engine.outbox.effects())) if isinstance(o, EngineHarness) else 0
    o.feed(ev("ON_NOEXEC", 5, job="ob255"))
    tree = ("ob255", "m255", "ib255", "g255", "k255")
    assert _status(o, *tree) == ["INACTIVE"] * 5
    assert all(o.store.job[j].on_noexec for j in tree)
    assert not o.store.job["k255"].on_hold
    assert o.store.job["m255"].exit_code is None
    assert o.store.job["wf255"].status == "RUNNING"  # woke on the real FAILURE earlier
    if isinstance(o, EngineHarness):
        assert len(list(o.engine.outbox.effects())) == effects
    o.feed(ev("STARTJOB", 6, job="ob255"))  # the dry run: every member bypasses
    assert [transitions(o, j)[-1] for j in ("m255", "g255", "k255")] == ["INACTIVE->SUCCESS"] * 3


def test_sem22_on_noexec_on_an_inactive_box_moves_no_status() -> None:
    """SEM-22 (DL-254): a box whose whole tree is already INACTIVE keeps its
    status, as an INACTIVE job does; only the flags move."""
    o = _noexec_box_tree()
    o.feed(ev("ON_NOEXEC", 0, job="ob255"))
    tree = ("ob255", "m255", "ib255", "g255", "k255")
    assert all(transitions(o, j) == ["ON_NOEXEC"] for j in tree)
    assert all(o.store.job[j].on_noexec for j in tree)


def test_sem22_off_noexec_on_a_box_clears_every_level() -> None:
    """SEM-22 (DL-254): "If you send the JOB_OFF_NOEXEC to a box, all jobs
    in the box (including all jobs that are contained in lower level boxes
    within the box) are reset". Every flag clears, so the next box run
    executes its members."""
    o = _noexec_box_tree()
    o.feed(ev("ON_NOEXEC", 0, job="ob255"))
    o.feed(ev("OFF_NOEXEC", 1, job="ob255"))
    tree = ("ob255", "m255", "ib255", "g255", "k255")
    assert not any(o.store.job[j].on_noexec for j in tree)
    assert all(transitions(o, j)[-1] == "OFF_NOEXEC" for j in tree)
    o.feed(ev("STARTJOB", 2, job="ob255"))
    assert _status(o, "m255", "g255", "k255") == ["RUNNING"] * 3


@pytest.mark.parametrize("inner", ["iced", "running"])
def test_sem22_on_noexec_on_a_box_with_a_contained_job_in_another_status_is_ignored(
    inner: str,
) -> None:
    """SEM-22 (DL-254): the vendor also ignores ON_NOEXEC for "A box job
    with jobs (including the jobs contained in lower level boxes) in a
    status other than the following status: ON_HOLD, ON_NOEXEC, INACTIVE,
    SUCCESS, FAILURE, ACTIVATED, or TERMINATED." The box is idle; the job
    two levels down is iced, or RUNNING through a FORCE."""
    text = (
        "insert_job: ob254\njob_type: b\n\n"
        "insert_job: ib254\njob_type: b\nbox_name: ob254\n\n"
        "insert_job: g254\njob_type: c\ncommand: x\nmachine: m1\nbox_name: ib254\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE" if inner == "iced" else "FORCE_STARTJOB", 0, job="g254"))
    assert o.store.job["ob254"].status == "INACTIVE"
    _assert_ignored(o, "ON_NOEXEC", "ob254", 1)
    assert "'g254'" in o.trace()[-1].cause


# ------------------------------------------------------------- 16. SEM-23 FORCE_STARTJOB


def test_sem23_force_startjob_ignores_condition_and_hold_and_satisfies_downstream() -> None:
    """T23 (SEM-23): FORCE_STARTJOB starts the job regardless of a false
    condition AND regardless of ON_HOLD; the forced run still emits normal
    status events, so its SUCCESS satisfies a downstream latching
    condition just like a normal run would.

    DL-243 REWRITE: FORCE_STARTJOB on a non-live ON_HOLD job now clears the
    flag too (sendevent Start Jobs page), recorded like an OFF_HOLD -- the
    old expectation omitted that trace entry and never checked the flag
    itself; both are added here, the rest of the scenario is unchanged."""
    text = (
        "insert_job: held_false23\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(never_true23)\n\n"
        "insert_job: never_true23\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: cons23\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(held_false23)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="held_false23"))
    o.feed(ev("FORCE_STARTJOB", 1, job="held_false23"))
    assert transitions(o, "held_false23") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    assert not o.store.job["held_false23"].on_hold
    o.feed(ev("STATUS", 2, job="held_false23", status="SUCCESS"))
    assert transitions(o, "cons23") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem23_force_start_clears_ice_and_runs() -> None:
    """SEM-23, DL-243: sendevent Start Jobs page -- forcing a
    non-executable (ON_HOLD/ON_ICE) job "returns it to an executable state,
    runs, and does not revert to the previous ... state." FORCE_STARTJOB on
    a non-live iced job clears ON_ICE (recorded like an OFF_ICE, with a
    cause naming the force) and starts it; the flag stays cleared after the
    run completes."""
    text = "insert_job: force_ice23\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="force_ice23"))
    o.feed(ev("FORCE_STARTJOB", 1, job="force_ice23"))
    assert transitions(o, "force_ice23") == [
        "ON_ICE",
        "OFF_ICE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    off_ice = next(t for t in o.trace() if t.job == "force_ice23" and t.transition == "OFF_ICE")
    assert "FORCE_STARTJOB" in off_ice.cause
    assert not o.store.job["force_ice23"].on_ice
    o.feed(ev("STATUS", 2, job="force_ice23", status="SUCCESS"))
    assert not o.store.job["force_ice23"].on_ice  # stays cleared after the run


def test_sem23_force_start_clears_hold_and_runs() -> None:
    """SEM-23, DL-243: the same FORCE_STARTJOB rule for ON_HOLD --
    cleared and recorded like an OFF_HOLD, with a cause naming the force,
    and the flag stays cleared after the run."""
    text = "insert_job: force_hold23\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="force_hold23"))
    o.feed(ev("FORCE_STARTJOB", 1, job="force_hold23"))
    assert transitions(o, "force_hold23") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    off_hold = next(t for t in o.trace() if t.job == "force_hold23" and t.transition == "OFF_HOLD")
    assert "FORCE_STARTJOB" in off_hold.cause
    assert not o.store.job["force_hold23"].on_hold
    o.feed(ev("STATUS", 2, job="force_hold23", status="SUCCESS"))
    assert not o.store.job["force_hold23"].on_hold  # stays cleared after the run


def test_sem23_force_start_on_a_live_job_is_still_refused() -> None:
    """SEM-23/DL-243: a job that is already STARTING/RUNNING/QUE_WAIT is
    refused for FORCE_STARTJOB same as a plain STARTJOB -- "concurrent runs
    of [a] process are not supported" (sendevent Start Jobs page). The
    live-job guard sits above the ice/hold-clearing branch in
    _attempt_start, so this is unaffected by DL-243."""
    text = "insert_job: force_live23\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="force_live23"))
    assert transitions(o, "force_live23") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("FORCE_STARTJOB", 1, job="force_live23"))
    statuses = [t for t in transitions(o, "force_live23") if "->" in t]
    assert statuses == ["INACTIVE->STARTING", "STARTING->RUNNING"]  # no second start
    refused = [t for t in o.trace() if t.job == "force_live23" and t.transition == "START_REFUSED"]
    assert len(refused) == 1
    assert "already RUNNING" in refused[0].cause
    assert "FORCE_STARTJOB event" in refused[0].cause


def test_sem23_after_force_clears_ice_a_later_plain_start_needs_no_off_event() -> None:
    """DL-243: once FORCE_STARTJOB has cleared ON_ICE, the job is a normal
    job going forward -- a later re-run through a plain STARTJOB needs no
    OFF_ICE of its own, because there is nothing left to clear."""
    text = "insert_job: force_ice23b\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="force_ice23b"))
    o.feed(ev("FORCE_STARTJOB", 1, job="force_ice23b"))
    o.feed(ev("STATUS", 2, job="force_ice23b", status="SUCCESS"))
    o.feed(ev("STARTJOB", 3, job="force_ice23b"))
    assert transitions(o, "force_ice23b") == [
        "ON_ICE",
        "OFF_ICE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
        "SUCCESS->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem23_force_start_clears_ice_even_when_run_window_then_refuses() -> None:
    """DL-243: "it returns to an executable state" is the event's OWN
    effect -- a FORCE_STARTJOB that goes on to lose at run_window still
    leaves ON_ICE cleared, because the return to an executable state
    already happened before that later gate was even reached."""
    text = (
        "insert_job: force_ice_rw23\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n'
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 0, 30), kind="ON_ICE", payload={"job": "force_ice_rw23"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 10),
            kind="FORCE_STARTJOB",
            payload={"job": "force_ice_rw23"},
        )
    )
    assert transitions(o, "force_ice_rw23") == ["ON_ICE", "OFF_ICE", "RUN_WINDOW_SKIP"]
    assert o.store.job["force_ice_rw23"].status == "INACTIVE"
    assert not o.store.job["force_ice_rw23"].on_ice  # cleared regardless of the later refusal


# --------------------------------------------------------- 17. SEM-32 arm-and-wait


def test_sem32_scheduled_startjob_with_false_condition_arms_and_waits() -> None:
    """T32 (SEM-32, Q3 RESOLVED by citation DL-58): a scheduled STARTJOB
    whose condition is currently false ARMS the job -- "the STARTJOB event
    being processed satisfies the start_times/run_calendar dependency" --
    and the condition edge later starts it through the schedule gate. The
    start consumes ("resets") the arm -- a second satisfaction of the
    condition does not re-run the job without a new tick -- and an
    unconsumed arm never expires (no-expiry cited; abandon switch deleted
    per the DL-06 protocol)."""
    text = (
        "insert_job: job32\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(gate32)\n\n"
        "insert_job: gate32\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="job32"))  # the scheduler's tick, condition false
    assert transitions(o, "job32") == ["SCHED_ARM"]
    assert o.store.job["job32"].status == "INACTIVE"  # armed is not a status
    assert o.store.job["job32"].armed
    o.feed(ev("STATUS", 5, job="gate32", status="SUCCESS"))  # the condition edge
    assert transitions(o, "job32") == ["SCHED_ARM", "INACTIVE->STARTING", "STARTING->RUNNING"]
    assert not o.store.job["job32"].armed  # the start consumed the arm
    o.feed(ev("STATUS", 6, job="job32", status="SUCCESS"))
    o.feed(ev("STATUS", 7, job="gate32", status="SUCCESS"))  # fresh edge, no tick
    assert o.store.job["job32"].status == "SUCCESS"  # unarmed: schedule gate holds


# ------------------------------------------------------------------ 18. SEM-33 run_window


def test_sem33_inside_window_starts_normally() -> None:
    """T33 (SEM-33): a start attempt inside the run_window proceeds exactly
    like an unrestricted start -- no DEFER/SKIP marker at all."""
    text = (
        "insert_job: rw_inside\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "03:00"\n'
        'run_window: "02:00-04:00"\n'
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 3, 0), kind="STARTJOB", payload={"job": "rw_inside"}))
    assert transitions(o, "rw_inside") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem33a_closer_to_next_opening_defers_then_starts_when_window_opens() -> None:
    """T33a (SEM-33): a start attempt 10 minutes before the window opens
    (and 22h50m after the previous close) is closer to the next opening ->
    RUN_WINDOW_DEFER is recorded and a TIMER STARTJOB is queued for window
    open; the job actually starts once the clock reaches that point, driven
    by an unrelated later event (feed()'s timer heap, not a second manual
    STARTJOB)."""
    text = (
        "insert_job: rw_defer\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n\n'
        "insert_job: dummy_rw\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "rw_defer"}))
    assert transitions(o, "rw_defer") == ["RUN_WINDOW_DEFER"]
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 10, 1),
            kind="STATUS",
            payload={"job": "dummy_rw", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw_defer") == [
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    start_entry = next(
        t for t in o.trace() if t.job == "rw_defer" and t.transition.endswith("STARTING")
    )
    assert start_entry.at == datetime(2026, 7, 1, 10, 0)  # window-open time, not the later event's


def test_sem33b_closer_to_previous_close_skips_and_never_starts() -> None:
    """T33b (SEM-33): a start attempt 10 minutes after the window closed is
    closer to the previous close -> RUN_WINDOW_SKIP, no timer is queued, and
    the job stays INACTIVE forever (unlike the DEFER case)."""
    text = (
        "insert_job: rw_skip\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: dummy_rw2\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="STARTJOB", payload={"job": "rw_skip"}))
    assert transitions(o, "rw_skip") == ["RUN_WINDOW_SKIP"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 10),
            kind="STATUS",
            payload={"job": "dummy_rw2", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw_skip") == ["RUN_WINDOW_SKIP"]  # still never started
    assert o.store.job["rw_skip"].status == "INACTIVE"


def test_sem33_run_window_crossing_midnight() -> None:
    """T33 (SEM-33): run_window "22:00-02:00" crosses midnight; 23:00 is
    inside, 03:00 is outside (and, per the closer-edge rule, 03:00 -> 22:00
    is 19h away vs. only 1h since the 02:00 close, so it SKIPs)."""
    inside_text = (
        "insert_job: rw_mid_in\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\n'
        'run_window: "22:00-02:00"\n'
    )
    inside = oracle(inside_text)
    inside.feed(
        Event(at=datetime(2026, 7, 1, 23, 0), kind="STARTJOB", payload={"job": "rw_mid_in"})
    )
    assert transitions(inside, "rw_mid_in") == ["INACTIVE->STARTING", "STARTING->RUNNING"]

    outside_text = (
        "insert_job: rw_mid_out\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "03:00"\n'
        'run_window: "22:00-02:00"\n'
    )
    outside = oracle(outside_text)
    outside.feed(
        Event(at=datetime(2026, 7, 1, 3, 0), kind="STARTJOB", payload={"job": "rw_mid_out"})
    )
    assert transitions(outside, "rw_mid_out") == ["RUN_WINDOW_SKIP"]


def test_sem33_run_window_exact_midpoint_ties_to_next_opening() -> None:
    """T33 (SEM-33), documented [?]: the undocumented exact-midpoint tie is
    pinned here as "next opening wins" (oracle.py's `to_open <= since_close`
    check). Window 10:00-11:00: previous close 11:00, next open 10:00 the
    following day -- a 23h gap whose midpoint is 22:30. One minute either
    side of the midpoint flips the outcome, confirming this is the exact
    boundary and not an off-by-one in the derivation."""
    text = (
        "insert_job: rw_tie\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n'
    )
    at_midpoint = oracle(text)
    at_midpoint.feed(
        Event(at=datetime(2026, 7, 1, 22, 30), kind="STARTJOB", payload={"job": "rw_tie"})
    )
    assert transitions(at_midpoint, "rw_tie") == ["RUN_WINDOW_DEFER"]

    just_before = oracle(text)
    just_before.feed(
        Event(at=datetime(2026, 7, 1, 22, 29), kind="STARTJOB", payload={"job": "rw_tie"})
    )
    assert transitions(just_before, "rw_tie") == ["RUN_WINDOW_SKIP"]

    just_after = oracle(text)
    just_after.feed(
        Event(at=datetime(2026, 7, 1, 22, 31), kind="STARTJOB", payload={"job": "rw_tie"})
    )
    assert transitions(just_after, "rw_tie") == ["RUN_WINDOW_DEFER"]


def test_sem33_box_variant_sole_deferred_member_keeps_box_running_until_it_completes() -> None:
    """T33 box variant (SEM-33 "Box interaction" note, DL-246): a
    run_window-gated member deferred to the next window opening keeps the
    containing box RUNNING overnight. The box start itself decides the
    deferral, before the member's own schedule gate: "the product issues a
    future STARTJOB event for the job for the next run_window" (TechDocs
    24.2, run_window page). The member's own tick at the same instant adds
    no second deferral; the box folds only once the deferred member starts
    (via its queued timer) and completes."""
    text = (
        "insert_job: box_rw33\njob_type: b\n\n"
        "insert_job: rw_member33\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:50"\n'
        'run_window: "10:00-11:00"\n'
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "box_rw33"}))
    assert transitions(o, "box_rw33") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "rw_member33") == ["RUN_WINDOW_DEFER"]  # decided at the box start
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "rw_member33"}))
    assert transitions(o, "rw_member33") == ["RUN_WINDOW_DEFER"]  # one deferral per opening
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 10, 30),
            kind="STATUS",
            payload={"job": "rw_member33", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw_member33") == [
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    assert transitions(o, "box_rw33") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem33_box_variant_two_members_deferred_member_keeps_box_open() -> None:
    """With a normal member plus a run_window-DEFERRED member, the
    normal member's completion must NOT fold the box -- SEM-11's literal
    gate (DL-13) keeps it RUNNING until the deferred member has run. The
    deferred member's queued timer then fires at window-open into a
    still-RUNNING box, runs, completes, and only then does the box fold."""
    text = (
        "insert_job: box_rw33b\njob_type: b\n\n"
        "insert_job: rw_member33b\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:50"\n'
        'run_window: "10:00-11:00"\n\n'
        "insert_job: normal_member33b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33b\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "box_rw33b"}))
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "rw_member33b"}))
    assert transitions(o, "rw_member33b") == ["RUN_WINDOW_DEFER"]
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 55),
            kind="STATUS",
            payload={"job": "normal_member33b", "status": "SUCCESS"},
        )
    )
    # the deferred member has not had its chance yet: box still RUNNING
    assert transitions(o, "box_rw33b") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 10, 30),
            kind="STATUS",
            payload={"job": "rw_member33b", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw_member33b") == [
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    assert transitions(o, "box_rw33b") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


_SKIP_BOX_JIL = (
    "insert_job: box_rw33c\njob_type: b\n\n"
    "insert_job: rw_member33c\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33c\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
    'run_window: "02:00-04:00"\n\n'
    "insert_job: normal_member33c\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33c\n"
)


def test_sem33_box_skip_bypasses_member_and_box_completes() -> None:
    """T33b box variant (SEM-33 [V], DL-154, DL-246): a box started after
    its member's window closed, closer to that close, SKIPS the member at
    the box start as a bypass -- TechDocs 12.1, run_window page: "the job's
    status changes to INACTIVE. The box job can still run to completion."
    No member tick is needed. The member stays out of the ran set (no vote
    in the SEM-11 fold), the sibling's completion folds the box, and the
    skip queues no RUN_WINDOW_DEFER timer."""
    o = oracle(_SKIP_BOX_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 30), kind="STARTJOB", payload={"job": "box_rw33c"}))
    assert transitions(o, "rw_member33c") == ["RUN_WINDOW_SKIP"]
    [skip] = [t for t in o.trace() if t.job == "rw_member33c"]
    assert skip.at == datetime(2026, 7, 1, 4, 30)  # at the box start
    assert o.store.job["rw_member33c"].status == "INACTIVE"
    assert list(o.store.timers()) == []  # no defer timer queued (no tick armed a deadline here)
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 40),
            kind="STATUS",
            payload={"job": "normal_member33c", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33c"].status == "SUCCESS"
    assert "rw_member33c" not in o.store.job["box_rw33c"].ran_members  # no vote in the fold
    fold = next(t for t in o.trace() if t.job == "box_rw33c" and t.transition == "RUNNING->SUCCESS")
    assert fold.cause == "default box fold: all members SUCCESS (SEM-11)"


def test_sem33_box_skip_on_last_outstanding_member_folds_the_box() -> None:
    """T33b box variant (SEM-33, DL-154): when the skip resolves the LAST
    outstanding member, the bypass itself re-runs the box completion check
    -- the box folds on the skip, not on some later unrelated transition.
    Before DL-154 this box hung RUNNING forever (the DL-13 literal reading
    of SEM-11, now carved out for the explicit INACTIVE verdict). The box
    starts inside the member's window, so the box start decides nothing
    (DL-246) and the member's later tick meets the skip branch."""
    o = oracle(_SKIP_BOX_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 3, 50), kind="STARTJOB", payload={"job": "box_rw33c"}))
    assert transitions(o, "rw_member33c") == []  # inside the window: waits for its tick
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 35),
            kind="STATUS",
            payload={"job": "normal_member33c", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33c"].status == "RUNNING"  # rw member still owed a verdict
    o.feed(Event(at=datetime(2026, 7, 1, 4, 36), kind="STARTJOB", payload={"job": "rw_member33c"}))
    assert transitions(o, "rw_member33c") == ["RUN_WINDOW_SKIP"]
    assert o.store.job["box_rw33c"].status == "SUCCESS"  # folded on the skip


def test_sem33_box_skip_on_a_rerun_member_follows_the_box_start_reset() -> None:
    """T33b box variant (SEM-33, DL-154; SEM-10, DL-242): a member that
    ended SUCCESS in run one shows SUCCESS->INACTIVE at run two's box
    start -- "jobs in boxes do not retain their statuses from previous box
    cycles" -- so the skip that follows has no edge of its own to show.
    The sibling completes FIRST here, so the skip resolves the last
    outstanding member on an already-INACTIVE row: the bypass runs the
    completion door itself and the box folds SUCCESS. Run two starts
    inside the window, so the box start decides nothing (DL-246) and the
    member's tick after the close meets the skip. The transition ride
    is pinned by `test_sem33_box_skip_transition_route_evaluates_the_
    override_too`."""
    o = oracle(_SKIP_BOX_JIL)
    # run 1: everything inside the window
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "box_rw33c"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "rw_member33c"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 10),
            kind="STATUS",
            payload={"job": "rw_member33c", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 11),
            kind="STATUS",
            payload={"job": "normal_member33c", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33c"].status == "SUCCESS"
    # run 2: box re-started inside the window; the sibling completes first,
    # then the member tick after the close skips as the last outstanding member
    o.feed(
        Event(at=datetime(2026, 7, 2, 3, 50), kind="FORCE_STARTJOB", payload={"job": "box_rw33c"})
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 35),
            kind="STATUS",
            payload={"job": "normal_member33c", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33c"].status == "RUNNING"  # rw member still owed a verdict
    o.feed(Event(at=datetime(2026, 7, 2, 4, 36), kind="STARTJOB", payload={"job": "rw_member33c"}))
    assert transitions(o, "rw_member33c")[-2:] == ["SUCCESS->INACTIVE", "RUN_WINDOW_SKIP"]
    [reset] = [
        t for t in o.trace() if t.job == "rw_member33c" and t.transition.endswith("INACTIVE")
    ]
    assert reset.at == datetime(2026, 7, 2, 3, 50)  # at the box start, not at the skip
    assert reset.cause.startswith("box 'box_rw33c' started")
    assert o.store.job["box_rw33c"].status == "SUCCESS"  # folded through the bypass door


def test_sem33_box_started_with_window_far_defers_and_box_stays_running_overnight() -> None:
    """T33a box variant (SEM-33 [V], DL-154): the OTHER closer-edge branch
    of the vendor's Box1 example -- the attempt lands closer to the NEXT
    opening, so a STARTJOB is queued for window open and the box stays
    RUNNING overnight; the member runs the next day and only then does the
    box fold. Existing behavior, pinned against the TechDocs 12.1
    run_window page's two-outcome box example."""
    text = (
        "insert_job: box_rw33d\njob_type: b\n\n"
        "insert_job: rw_member33d\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33d\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: dummy33d\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 23, 0), kind="STARTJOB", payload={"job": "box_rw33d"}))
    o.feed(Event(at=datetime(2026, 7, 1, 23, 0), kind="STARTJOB", payload={"job": "rw_member33d"}))
    # 3h to the 02:00 opening vs 19h since the 04:00 close -> DEFER, no skip
    assert transitions(o, "rw_member33d") == ["RUN_WINDOW_DEFER"]
    assert o.store.job["box_rw33d"].status == "RUNNING"
    o.feed(  # an unrelated next-day event drives the timer heap past window open
        Event(
            at=datetime(2026, 7, 2, 2, 30),
            kind="STATUS",
            payload={"job": "dummy33d", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33d"].status == "RUNNING"  # still: member now running
    assert o.store.job["rw_member33d"].status == "RUNNING"
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 2, 45),
            kind="STATUS",
            payload={"job": "rw_member33d", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33d"].status == "SUCCESS"


def test_sem33_every_member_skipped_folds_the_box_over_an_empty_vote() -> None:
    """T33b box variant (SEM-33, DL-154): a box whose EVERY member is
    window-skipped completes through the existing SEM-11 default fold over
    an empty ran set -- no member failed, box_success unspecified, so the
    box ends SUCCESS. That empty-vote rule predates DL-154; this pins it
    for the all-bypassed shape rather than inventing a new one. Since
    DL-246 the box start skips both members, so the box completes at its
    own start with no member tick."""
    text = (
        "insert_job: box_rw33e\njob_type: b\n\n"
        "insert_job: rw_e1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33e\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: rw_e2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33e\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n'
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 30), kind="STARTJOB", payload={"job": "box_rw33e"}))
    assert transitions(o, "rw_e1") == ["RUN_WINDOW_SKIP"]
    assert transitions(o, "rw_e2") == ["RUN_WINDOW_SKIP"]
    assert o.store.job["box_rw33e"].status == "SUCCESS"
    assert o.store.job["box_rw33e"].ran_members == frozenset()  # nobody voted
    fold = next(t for t in o.trace() if t.job == "box_rw33e" and t.transition == "RUNNING->SUCCESS")
    assert fold.cause == "default box fold: all members SUCCESS (SEM-11)"


def test_sem33_mid_run_condition_edge_lands_on_skip_branch_and_bypasses_identically() -> None:
    """T33b box variant (SEM-33, DL-154 documented default): the vendor
    anchors INACTIVE at box start; a condition edge that lands on the skip
    branch MID-RUN bypasses identically -- the closer-edge rule applies at
    the attempt's own moment. The Q3c interaction is pinned unchanged: the
    skip does not consume the member's armed latch (only a real start
    does), and the arm then dies with the box run (SCHED_DISARM at the
    fold, the DL-54 scope pin)."""
    text = (
        "insert_job: box_rw33f\njob_type: b\n\n"
        "insert_job: gate33f\njob_type: c\ncommand: g\nmachine: m1\nbox_name: box_rw33f\n\n"
        "insert_job: rw_member33f\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33f\n"
        "condition: s(gate33f)\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: slow33f\njob_type: c\ncommand: s\nmachine: m1\nbox_name: box_rw33f\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "box_rw33f"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "rw_member33f"}))
    assert transitions(o, "rw_member33f") == ["SCHED_ARM"]  # s(gate33f) false: tick latches
    # the gate completes past the window close -- the released edge SKIPs
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 31),
            kind="STATUS",
            payload={"job": "gate33f", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw_member33f") == ["SCHED_ARM", "RUN_WINDOW_SKIP"]
    assert o.store.job["box_rw33f"].status == "RUNNING"  # slow33f still running
    assert o.store.job["rw_member33f"].armed  # the skip did not consume the arm
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 40),
            kind="STATUS",
            payload={"job": "slow33f", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33f"].status == "SUCCESS"  # fold past the bypassed member
    assert not o.store.job["rw_member33f"].armed  # unconsumed arm died with the run (Q3c)
    assert transitions(o, "rw_member33f") == ["SCHED_ARM", "RUN_WINDOW_SKIP", "SCHED_DISARM"]


def test_sem33_box_skip_resolution_evaluates_a_satisfied_external_override() -> None:
    """T33b box variant (SEM-33/SEM-12, DL-154): the skip resolution is a
    COMPLETION MOMENT, so the box runs through the full completion door --
    a box_success over an external job that became true earlier fires the
    moment the skip resolves the last outstanding member. Without the door
    the override is never evaluated (INACTIVE is not a completion status)
    and the box hangs RUNNING with its override satisfied. The box starts
    inside the member's window, so the member's tick after the close is
    what skips it (DL-246 decides nothing at an in-window box start)."""
    text = (
        "insert_job: box_rw33g\njob_type: b\nbox_success: s(ext33g)\n\n"
        "insert_job: m33g\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33g\n\n"
        "insert_job: rw_member33g\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33g\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: ext33g\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 3, 50), kind="STARTJOB", payload={"job": "box_rw33g"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 31),
            kind="STATUS",
            payload={"job": "m33g", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 32),
            kind="STATUS",
            payload={"job": "ext33g", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33g"].status == "RUNNING"  # external ref: no member completed since
    o.feed(Event(at=datetime(2026, 7, 1, 4, 33), kind="STARTJOB", payload={"job": "rw_member33g"}))
    assert transitions(o, "rw_member33g") == ["RUN_WINDOW_SKIP"]
    assert o.store.job["box_rw33g"].status == "SUCCESS"
    fold = next(t for t in o.trace() if t.job == "box_rw33g" and t.transition == "RUNNING->SUCCESS")
    assert fold.cause == "box_success override met (SEM-12)"


@pytest.mark.parametrize("operator_status", ["SUCCESS", "INACTIVE"])
def test_sem33_box_skip_member_later_set_by_the_operator_stays_settled(
    operator_status: str,
) -> None:
    """T33b box variant (SEM-33, DL-154; SEM-11, DL-242): a window-skipped
    member is resolved. An operator status on it that is not live -- here
    SUCCESS or INACTIVE -- keeps it settled, so the box completes when its
    running sibling ends. The fold still votes over ran members only."""
    o = oracle(_SKIP_BOX_JIL)
    at = datetime(2026, 7, 2, 4, 30)
    o.feed(Event(at=at, kind="FORCE_STARTJOB", payload={"job": "box_rw33c"}))
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "rw_member33c"}))
    assert transitions(o, "rw_member33c")[-1] == "RUN_WINDOW_SKIP"
    o.feed(
        Event(
            at=at + timedelta(minutes=1),
            kind="STATUS",
            payload={"job": "rw_member33c", "status": operator_status},
        )
    )
    assert o.store.job["box_rw33c"].status == "RUNNING"  # the sibling still runs
    o.feed(
        Event(
            at=at + timedelta(minutes=2),
            kind="STATUS",
            payload={"job": "normal_member33c", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33c"].status == "SUCCESS"


@pytest.mark.parametrize("override", ["n(a33w)", "s(ext33w)"])
def test_sem33_box_skip_on_an_inactive_member_reaches_ancestor_overrides(override: str) -> None:
    """T33b box variant (SEM-33/SEM-12, DL-154, DL-242, DL-246): after the
    box-start reset a skip usually lands on a member already INACTIVE, so
    there is no transition to ride. The bypass still runs the ancestors'
    transitive overrides as a completion moment, as an operator's INACTIVE
    on the same member does. Here IN's own start, inside OUT's, skips the
    member (DL-246): OUT completes by its override at that instant while
    IN's other member still runs."""
    text = (
        f"insert_job: out33w\njob_type: b\nbox_success: {override}\n\n"
        "insert_job: in33w\njob_type: b\nbox_name: out33w\n\n"
        "insert_job: a33w\njob_type: c\ncommand: a\nmachine: m1\nbox_name: in33w\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: wait33w\njob_type: c\ncommand: w\nmachine: m1\nbox_name: in33w\n\n"
        "insert_job: ext33w\njob_type: c\ncommand: e\nmachine: m1\n"
    )
    o = oracle(text)
    at = datetime(2026, 7, 2, 4, 30)
    o.feed(Event(at=at, kind="STATUS", payload={"job": "ext33w", "status": "SUCCESS"}))
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "out33w"}))
    assert transitions(o, "a33w") == ["RUN_WINDOW_SKIP"]
    assert _status(o, "out33w", "in33w", "a33w", "wait33w") == [
        "SUCCESS",
        "RUNNING",
        "INACTIVE",
        "RUNNING",
    ]
    [fold] = [t for t in o.trace() if t.job == "out33w" and t.transition == "RUNNING->SUCCESS"]
    assert fold.cause == "box_success override met (SEM-12)"


def test_sem33_box_skip_transition_route_evaluates_the_override_too() -> None:
    """T33b box variant (SEM-33/SEM-12, DL-154): the completion door also
    rides the SUCCESS->INACTIVE transition of a skipped member -- the skip
    mark is visible to _on_member_transition, which treats the edge as the
    member's resolution moment and evaluates the external override there.
    Since DL-242 a box start resets a rerun member to INACTIVE, so run two
    gives the member a SUCCESS by CHANGE_STATUS before its skip; the
    external ref is false at that completion moment and turns true only
    between it and the skip. Run two starts inside the window, so its box
    start decides nothing (DL-246) and the tick after the close is the
    skip. Reordering the mark after the write leaves this box hanging
    RUNNING."""
    text = (
        "insert_job: box_rw33h\njob_type: b\nbox_success: s(ext33h)\n\n"
        "insert_job: rw_member33h\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33h\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: ext33h\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    # run 1: inside the window; the override fires at the member's completion
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "box_rw33h"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "rw_member33h"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 35),
            kind="STATUS",
            payload={"job": "ext33h", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 40),
            kind="STATUS",
            payload={"job": "rw_member33h", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33h"].status == "SUCCESS"
    # run 2: the external ref is false at the box start and at the
    # operator's SUCCESS on the waiting member, so neither decides the box
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 3, 49),
            kind="STATUS",
            payload={"job": "ext33h", "status": "FAILURE"},
        )
    )
    o.feed(
        Event(at=datetime(2026, 7, 2, 3, 50), kind="FORCE_STARTJOB", payload={"job": "box_rw33h"})
    )
    assert transitions(o, "rw_member33h")[-1] == "SUCCESS->INACTIVE"  # reset, no decision
    assert o.store.job["rw_member33h"].status == "INACTIVE"  # SEM-10 reset
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 31),
            kind="STATUS",
            payload={"job": "rw_member33h", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 32),
            kind="STATUS",
            payload={"job": "ext33h", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33h"].status == "RUNNING"  # external: no completion moment yet
    # the skip lands on the sole member -- SUCCESS->INACTIVE edge, and
    # s(ext33h) now holds, so the override decides the box at that edge
    o.feed(Event(at=datetime(2026, 7, 2, 4, 33), kind="STARTJOB", payload={"job": "rw_member33h"}))
    assert transitions(o, "rw_member33h")[-2:] == ["RUN_WINDOW_SKIP", "SUCCESS->INACTIVE"]
    assert o.store.job["box_rw33h"].status == "SUCCESS"
    folds = [t for t in o.trace() if t.job == "box_rw33h" and t.transition.endswith("->SUCCESS")]
    assert folds[-1].cause == "box_success override met (SEM-12)"


def test_sem33_box_skip_with_unmet_override_still_hangs() -> None:
    """T33b box variant (SEM-33/SEM-12, DL-154): the composition of two
    pinned rules. The skip resolves its member (vendor INACTIVE verdict),
    but a specified-and-UNMET box_success still suppresses the default fold
    (SEM-12 third bullet) -- so the box hangs RUNNING. DL-154 completes the
    member, not the box. The box start decides the skip (DL-246)."""
    text = (
        "insert_job: box_rw33i\njob_type: b\nbox_success: s(ext33i)\n\n"
        "insert_job: m33i\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33i\n\n"
        "insert_job: rw_member33i\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33i\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: ext33i\njob_type: c\ncommand: z\nmachine: m1\n\n"
        "insert_job: idle33i\njob_type: c\ncommand: i\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 30), kind="STARTJOB", payload={"job": "box_rw33i"}))
    assert transitions(o, "rw_member33i") == ["RUN_WINDOW_SKIP"]  # at the box start
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 31),
            kind="STATUS",
            payload={"job": "m33i", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33i"].status == "RUNNING"  # ext33i never ran: override unmet
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 5, 0),
            kind="STATUS",
            payload={"job": "idle33i", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33i"].status == "RUNNING"  # still: the hang is the rule


def test_sem33_box_skip_clears_a_stale_failure_latch_and_flips_the_fold() -> None:
    """T33b box variant (SEM-33, DL-154 -- the verdict flip, on the record):
    the INACTIVE write is vendor-pinned ("the job's status changes to
    INACTIVE"), and it CLEARS a stale latch from a previous run. Run one:
    the member fails, box_failure: f(member) fires, box FAILURE. Run two:
    the box start resets the member -- FAILURE->INACTIVE (SEM-10, DL-242)
    -- and the member is then skipped, so f(member) reads false at the
    sibling's completion and the default fold gives SUCCESS. Before DL-154
    the stale FAILURE decided run two as FAILURE. DL-153 ruled the
    stale-latch ground the same day, which is why this flip is pinned here
    rather than left implicit. Since DL-242 the latch clears at the box
    start rather than at the skip, and since DL-246 the same box start
    decides the skip."""
    text = (
        "insert_job: box_rw33j\njob_type: b\nbox_failure: f(rw_member33j)\n\n"
        "insert_job: m33j\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33j\n\n"
        "insert_job: rw_member33j\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33j\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n'
    )
    o = oracle(text)
    # run 1: member fails inside the window; the override decides FAILURE
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "box_rw33j"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "rw_member33j"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 35),
            kind="STATUS",
            payload={"job": "m33j", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 40),
            kind="STATUS",
            payload={"job": "rw_member33j", "status": "FAILURE"},
        )
    )
    assert o.store.job["box_rw33j"].status == "FAILURE"
    # run 2: the box start clears the latch -- FAILURE->INACTIVE -- the
    # skip resolves the member, and the fold over the sibling's SUCCESS
    # flips the verdict
    o.feed(
        Event(at=datetime(2026, 7, 2, 4, 30), kind="FORCE_STARTJOB", payload={"job": "box_rw33j"})
    )
    assert transitions(o, "rw_member33j")[-2:] == ["FAILURE->INACTIVE", "RUN_WINDOW_SKIP"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 40),
            kind="STATUS",
            payload={"job": "m33j", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33j"].status == "SUCCESS"  # was FAILURE pre-DL-154


def test_sem33_box_skip_after_the_box_start_reset_leaves_downstream_atoms_false() -> None:
    """T33b box variant (SEM-33/SEM-01, DL-154 -- estate-wide effect, on
    the record): the INACTIVE write makes every downstream s()/f()/d() atom
    on the skipped member read FALSE -- vendor-consistent (SEM-01 atoms
    read current status; INACTIVE satisfies none of them), and larger than
    the box fold: a consumer holding the member's run-one SUCCESS in an
    AND that completes later never starts. Since DL-242 the write lands at
    run two's box start (SEM-10), and since DL-246 that box start also
    decides the skip."""
    text = (
        "insert_job: box_rw33k\njob_type: b\n\n"
        "insert_job: m33k\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33k\n\n"
        "insert_job: drw33k\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33k\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: gate33k\njob_type: c\ncommand: g\nmachine: m1\n\n"
        "insert_job: consumer33k\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: s(drw33k) & s(gate33k)\n"
    )
    o = oracle(text)
    # run 1: drw33k ends SUCCESS; the consumer's other conjunct is not met
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "box_rw33k"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 30), kind="STARTJOB", payload={"job": "drw33k"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 40),
            kind="STATUS",
            payload={"job": "drw33k", "status": "SUCCESS"},
        )
    )
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 2, 41),
            kind="STATUS",
            payload={"job": "m33k", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw33k"].status == "SUCCESS"
    # run 2: the box start un-latches s(drw33k); the gate completing after
    # can no longer start the consumer
    o.feed(
        Event(at=datetime(2026, 7, 2, 4, 30), kind="FORCE_STARTJOB", payload={"job": "box_rw33k"})
    )
    assert transitions(o, "drw33k")[-2:] == ["SUCCESS->INACTIVE", "RUN_WINDOW_SKIP"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 4, 40),
            kind="STATUS",
            payload={"job": "gate33k", "status": "SUCCESS"},
        )
    )
    assert o.store.job["consumer33k"].status == "INACTIVE"  # never starts: s(drw33k) is false


def test_sem33_box_skip_keeps_the_ticks_must_start_deadline_armed() -> None:
    """T33b x T34 (SEM-33/SEM-34, DL-154): the skip is NOT the tick's run
    -- unlike the ON_NOEXEC bypass (SEM-22) -- so the must_start deadline
    the member's own STARTJOB tick armed stays armed and ALARMS at
    tick+offset: SEM-34's "armed even when the start is abandoned" pin,
    deliberately unchanged by the bypass. The box starts inside the
    window, so the tick after the close is what skips (DL-246)."""
    text = (
        "insert_job: box_rw33m\njob_type: b\n\n"
        "insert_job: rw_member33m\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw33m\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\nmust_start_times: +5\n\n'
        "insert_job: idle33m\njob_type: c\ncommand: i\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 3, 50), kind="STARTJOB", payload={"job": "box_rw33m"}))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 31), kind="STARTJOB", payload={"job": "rw_member33m"}))
    assert transitions(o, "rw_member33m") == ["RUN_WINDOW_SKIP"]
    assert o.store.job["box_rw33m"].status == "SUCCESS"  # sole member skipped: empty-vote fold
    deadlines = [ev for _, _, ev in o.store.timers() if ev.payload.get("check") == "must_start"]
    assert len(deadlines) == 1  # the tick's deadline survives the skip
    emitted = o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 40),
            kind="STATUS",
            payload={"job": "idle33m", "status": "SUCCESS"},
        )
    )
    assert any(e.kind == "MUST_START_ALARM" and e.job() == "rw_member33m" for e in emitted)
    assert "MUST_START_ALARM" in transitions(o, "rw_member33m")


def test_sem11_window_skip_leaves_the_run_but_a_never_fired_condition_still_hangs() -> None:
    """T11 (SEM-11 amended by DL-154): the carve-out is EXACTLY the explicit
    INACTIVE verdict of a run_window skip -- DL-13's literal pin is
    otherwise untouched. In one box: a skipped member leaves the run, yet
    the box stays RUNNING because a sibling's condition has never fired --
    the real hung-box pattern. Only when that condition fires and the
    sibling completes does the box fold. The box start decides the skip
    (DL-246)."""
    text = (
        "insert_job: box_rw11\njob_type: b\n\n"
        "insert_job: rw_member11\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw11\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: cond_member11\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_rw11\n"
        "condition: s(ext11)\n\n"
        "insert_job: ext11\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 30), kind="STARTJOB", payload={"job": "box_rw11"}))
    assert transitions(o, "rw_member11") == ["RUN_WINDOW_SKIP"]
    # the skip left the run, but cond_member11 has neither run nor been
    # bypassed: the box stays RUNNING (DL-13, the hung-box pattern)
    assert o.store.job["box_rw11"].status == "RUNNING"
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 40),
            kind="STATUS",
            payload={"job": "ext11", "status": "SUCCESS"},
        )
    )
    assert o.store.job["cond_member11"].status == "RUNNING"
    o.feed(
        Event(
            at=datetime(2026, 7, 1, 4, 50),
            kind="STATUS",
            payload={"job": "cond_member11", "status": "SUCCESS"},
        )
    )
    assert o.store.job["box_rw11"].status == "SUCCESS"


# DL-246: the box start decides a run_window member's disposition (TechDocs
# 24.2, run_window page, Box1 example), and a standalone skip moves the job
# to INACTIVE.

_BOX1_JIL = (
    "insert_job: box1_33v\njob_type: b\n\n"
    "insert_job: joba_33v\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box1_33v\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
    'run_window: "02:00-04:00"\n\n'
    "insert_job: jobb_33v\njob_type: c\ncommand: b\nmachine: m1\nbox_name: box1_33v\n\n"
    "insert_job: jobc_33v\njob_type: c\ncommand: c\nmachine: m1\nbox_name: box1_33v\n\n"
    "insert_job: clock_33v\njob_type: c\ncommand: k\nmachine: m1\n"
)


def ev_at(at: datetime, kind: EventKind, **payload: object) -> Event:
    return Event(at=at, kind=kind, payload=payload)


def _timers(o: Oracle | EngineHarness, job: str) -> list[tuple[datetime, str, str]]:
    oracle_obj = o if isinstance(o, Oracle) else o.engine.oracle
    return [t for t in oracle_obj.pending_timers() if t[1] == job]


def test_sem33_vendor_box1_started_at_0405_skips_joba_and_completes() -> None:
    """T33 (SEM-33 [V], DL-246): the vendor's Box1 example, first half.
    "If Box1 starts at 04:05, JobB and JobC can run and JobA becomes
    INACTIVE so that the box can complete that day" (TechDocs 24.2,
    run_window page). The box start decides JobA's disposition before its
    own schedule gate, so no tick is injected for JobA at all."""
    o = oracle(_BOX1_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "box1_33v"}))
    assert transitions(o, "joba_33v") == ["RUN_WINDOW_SKIP"]
    assert _status(o, "joba_33v", "jobb_33v", "jobc_33v") == ["INACTIVE", "RUNNING", "RUNNING"]
    assert _timers(o, "joba_33v") == []
    o.feed(ev_at(datetime(2026, 7, 1, 4, 10), "STATUS", job="jobb_33v", status="SUCCESS"))
    assert o.store.job["box1_33v"].status == "RUNNING"  # JobC still runs
    o.feed(ev_at(datetime(2026, 7, 1, 4, 15), "STATUS", job="jobc_33v", status="SUCCESS"))
    assert o.store.job["box1_33v"].status == "SUCCESS"
    assert "joba_33v" not in o.store.job["box1_33v"].ran_members
    assert transitions(o, "joba_33v") == ["RUN_WINDOW_SKIP"]  # nothing started


def test_sem33_vendor_box1_started_at_1605_defers_joba_to_0200_next_day() -> None:
    """T33 (SEM-33 [V], DL-246): the vendor's Box1 example, second half.
    "If Box1 instead starts at 16:05, JobA will have a STARTJOB event set
    for 02:00 the next day, and the box continues running until the job
    starts the next day." Exactly one deferred start is queued at the box
    start; at 02:00 JobA starts, and the box completes after it."""
    o = oracle(_BOX1_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 16, 5), kind="STARTJOB", payload={"job": "box1_33v"}))
    assert transitions(o, "joba_33v") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "joba_33v") == [(datetime(2026, 7, 2, 2, 0), "joba_33v", "run_window")]
    o.feed(ev_at(datetime(2026, 7, 1, 16, 10), "STATUS", job="jobb_33v", status="SUCCESS"))
    o.feed(ev_at(datetime(2026, 7, 1, 16, 15), "STATUS", job="jobc_33v", status="SUCCESS"))
    assert o.store.job["box1_33v"].status == "RUNNING"  # JobA still owed its start
    o.feed(ev_at(datetime(2026, 7, 2, 2, 0), "STATUS", job="clock_33v", status="SUCCESS"))
    assert transitions(o, "joba_33v") == [
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    start = next(t for t in o.trace() if t.job == "joba_33v" and t.transition.endswith("STARTING"))
    assert start.at == datetime(2026, 7, 2, 2, 0)
    assert o.store.job["box1_33v"].status == "RUNNING"
    o.feed(ev_at(datetime(2026, 7, 2, 2, 30), "STATUS", job="joba_33v", status="SUCCESS"))
    assert o.store.job["box1_33v"].status == "SUCCESS"


def test_sem33_box_start_inside_the_members_window_decides_nothing() -> None:
    """T33 (SEM-33, DL-246): a box started inside a member's window leaves
    the member as before: it waits for its own schedule tick (SEM-31's
    double gate), no deferral or skip is recorded, and the tick starts it."""
    o = oracle(_BOX1_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 3, 0), kind="STARTJOB", payload={"job": "box1_33v"}))
    assert transitions(o, "joba_33v") == []
    assert _timers(o, "joba_33v") == []
    assert o.store.job["joba_33v"].status == "INACTIVE"
    o.feed(Event(at=datetime(2026, 7, 1, 3, 1), kind="STARTJOB", payload={"job": "joba_33v"}))
    assert transitions(o, "joba_33v") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem33_box_start_passes_over_a_member_once_a_skip_completed_its_box() -> None:
    """T33 (SEM-33/SEM-12, DL-246): both members are outside their window at
    the box start. The first skip is a completion moment, and the box's
    satisfied external override completes the box there. The second member
    is then passed over: its box is no longer RUNNING, so nothing is
    recorded for it."""
    text = (
        "insert_job: ext33z\njob_type: c\ncommand: e\nmachine: m1\n\n"
        "insert_job: bx33z\njob_type: b\nbox_success: s(ext33z)\n\n"
        "insert_job: a33z\njob_type: c\ncommand: a\nmachine: m1\nbox_name: bx33z\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: b33z\njob_type: c\ncommand: b\nmachine: m1\nbox_name: bx33z\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n'
    )
    o = oracle(text)
    o.feed(ev_at(datetime(2026, 7, 1, 4, 0), "STATUS", job="ext33z", status="SUCCESS"))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "bx33z"}))
    assert transitions(o, "a33z") == ["RUN_WINDOW_SKIP"]
    assert transitions(o, "b33z") == []
    assert _status(o, "bx33z", "b33z") == ["SUCCESS", "INACTIVE"]
    [fold] = [t for t in o.trace() if t.job == "bx33z" and t.transition == "RUNNING->SUCCESS"]
    assert fold.cause == "box_success override met (SEM-12)"


def test_sem33_box_start_leaves_a_held_member_to_its_own_path() -> None:
    """T33 (SEM-33, DL-246 decision): a held member is not decided at the
    box start, as a held job does not take the box start's status change.
    It keeps its hold, records no deferral or skip, and keeps the box
    RUNNING as any held member does (SEM-21)."""
    o = oracle(_BOX1_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 0), kind="ON_HOLD", payload={"job": "joba_33v"}))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "box1_33v"}))
    assert transitions(o, "joba_33v") == ["ON_HOLD"]
    o.feed(ev_at(datetime(2026, 7, 1, 4, 10), "STATUS", job="jobb_33v", status="SUCCESS"))
    o.feed(ev_at(datetime(2026, 7, 1, 4, 15), "STATUS", job="jobc_33v", status="SUCCESS"))
    assert o.store.job["box1_33v"].status == "RUNNING"


def _ordered(*blocks: str, first: str) -> str:
    """The blocks with the one whose first line names `first` moved to the front."""
    lead = [b for b in blocks if b.startswith(f"insert_job: {first}\n")]
    return "\n\n".join(lead + [b for b in blocks if b not in lead]) + "\n"


@pytest.mark.parametrize("first", ["a33o", "b33o"])
def test_sem33_box_start_window_decision_does_not_depend_on_member_order(first: str) -> None:
    """T33 (SEM-33/SEM-12, DL-246): the box start decides the window after
    every member's start attempt. The skip is a completion moment, and the
    box's satisfied external override fires there, but only after the
    plain sibling has started. Both catalog orders give the same result."""
    member_a = (
        "insert_job: a33o\njob_type: c\ncommand: a\nmachine: m1\nbox_name: bx33o\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"'
    )
    member_b = "insert_job: b33o\njob_type: c\ncommand: b\nmachine: m1\nbox_name: bx33o"
    text = (
        "insert_job: ext33o\njob_type: c\ncommand: e\nmachine: m1\n\n"
        "insert_job: bx33o\njob_type: b\nbox_success: s(ext33o)\n\n"
        + _ordered(member_a, member_b, first=first)
    )
    o = oracle(text)
    o.feed(ev_at(datetime(2026, 7, 1, 4, 0), "STATUS", job="ext33o", status="SUCCESS"))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "bx33o"}))
    assert transitions(o, "b33o") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "a33o") == ["RUN_WINDOW_SKIP"]
    assert _status(o, "bx33o", "a33o", "b33o") == ["SUCCESS", "INACTIVE", "RUNNING"]


@pytest.mark.parametrize("first", ["in33o", "c33o"])
def test_sem33_subbox_skip_waits_for_the_outer_boxs_direct_member(first: str) -> None:
    """T33 (SEM-33/SEM-12, DL-246), nested: the subbox's skip reaches the
    outer box's override through the ancestor walk. The decision runs after
    every attempt in the started subtree, so the outer box's direct member
    starts whether it is listed after the subbox or before it."""
    sub = "insert_job: in33o\njob_type: b\nbox_name: out33o"
    member_a = (
        "insert_job: a33p\njob_type: c\ncommand: a\nmachine: m1\nbox_name: in33o\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"'
    )
    member_w = "insert_job: w33p\njob_type: c\ncommand: w\nmachine: m1\nbox_name: in33o"
    member_c = "insert_job: c33o\njob_type: c\ncommand: c\nmachine: m1\nbox_name: out33o"
    text = (
        "insert_job: ext33p\njob_type: c\ncommand: e\nmachine: m1\n\n"
        "insert_job: out33o\njob_type: b\nbox_success: s(ext33p)\n\n"
        + _ordered(sub, member_a, member_w, member_c, first=first)
    )
    o = oracle(text)
    o.feed(ev_at(datetime(2026, 7, 1, 4, 0), "STATUS", job="ext33p", status="SUCCESS"))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "out33o"}))
    assert transitions(o, "c33o") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "w33p") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "a33p") == ["RUN_WINDOW_SKIP"]
    assert _status(o, "out33o", "in33o") == ["SUCCESS", "RUNNING"]


@pytest.mark.parametrize("first", ["sub33x", "wk33x"])
def test_sem33_running_wakes_join_the_box_starts_window_pass(first: str) -> None:
    """T33 (SEM-33/SEM-12, DL-246): the outer box's RUNNING transition wakes
    a subbox and a worker that both reference the outer box. The subbox's
    window skip completes the outer box through its override, but only in
    the single pass after every attempt, so the worker starts in both
    catalog orders."""
    sub = "insert_job: sub33x\njob_type: b\nbox_name: out33x\ncondition: n(out33x) | s(ext33x)"
    member = (
        "insert_job: a33x\njob_type: c\ncommand: a\nmachine: m1\nbox_name: sub33x\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"'
    )
    worker = (
        "insert_job: wk33x\njob_type: c\ncommand: w\nmachine: m1\nbox_name: out33x\n"
        "condition: n(out33x) | s(ext33x)"
    )
    text = (
        "insert_job: ext33x\njob_type: c\ncommand: e\nmachine: m1\n\n"
        "insert_job: out33x\njob_type: b\nbox_success: s(ext33x)\n\n"
        + _ordered(sub, member, worker, first=first)
    )
    o = oracle(text)
    o.feed(ev_at(datetime(2026, 7, 1, 4, 0), "STATUS", job="ext33x", status="SUCCESS"))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 5), kind="STARTJOB", payload={"job": "out33x"}))
    assert transitions(o, "wk33x") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "a33x") == ["RUN_WINDOW_SKIP"]
    assert _status(o, "out33x", "sub33x", "wk33x") == ["SUCCESS", "SUCCESS", "RUNNING"]


def test_sem33_a_window_pass_never_decides_for_a_later_box_run() -> None:
    """T33 (SEM-33, DL-246): each window decision is bound to its box run.
    Run one's first skip completes the box through its override; an
    ON_NOEXEC relay then starts run two at once, inside that skip. Run two
    decides both members itself and stays RUNNING beside the worker kept
    from run one, and a marker then makes the override true. Run one's
    pass resumes with its second member and does nothing: no duplicate
    skip, and run two is not completed by run one's pass."""
    text = (
        "insert_job: ext33r\njob_type: c\ncommand: e\nmachine: m1\ncondition: s(relay33r)\n\n"
        "insert_job: relay33r\njob_type: c\ncommand: r\nmachine: m1\n"
        "condition: s(bx33r) & s(ext33r)\n\n"
        "insert_job: bx33r\njob_type: b\ncondition: s(relay33r)\n"
        "box_success: s(ext33r) | s(mk33r)\n\n"
        "insert_job: a33r\njob_type: c\ncommand: a\nmachine: m1\nbox_name: bx33r\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: b33r\njob_type: c\ncommand: b\nmachine: m1\nbox_name: bx33r\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: wk33r\njob_type: c\ncommand: w\nmachine: m1\nbox_name: bx33r\n\n"
        "insert_job: mk33r\njob_type: c\ncommand: m\nmachine: m1\ncondition: s(relay33r)\n"
    )
    o = oracle(text)
    at = datetime(2026, 7, 1, 4, 0)
    o.feed(Event(at=at, kind="ON_NOEXEC", payload={"job": "relay33r"}))
    o.feed(Event(at=at, kind="ON_NOEXEC", payload={"job": "mk33r"}))
    o.feed(ev_at(at, "STATUS", job="ext33r", status="SUCCESS"))
    o.feed(Event(at=at + timedelta(minutes=5), kind="FORCE_STARTJOB", payload={"job": "bx33r"}))
    runs = [t.transition for t in o.trace() if t.job == "bx33r"]
    assert runs == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
        "SUCCESS->STARTING",
        "STARTING->RUNNING",
    ]
    assert o.store.job["bx33r"].run_number == 2
    assert _status(o, "ext33r", "mk33r", "wk33r") == ["RUNNING", "SUCCESS", "RUNNING"]
    assert transitions(o, "a33r") == ["RUN_WINDOW_SKIP", "RUN_WINDOW_SKIP"]
    assert transitions(o, "b33r") == ["RUN_WINDOW_SKIP"]  # run two's only
    assert o.store.job["bx33r"].status == "RUNNING"


def test_sem33_box_start_deferral_is_not_swallowed_by_a_deadline_timer() -> None:
    """T33 x T34 (SEM-33/SEM-34, DL-246): the member's tick before the box
    starts is refused but arms a must_start deadline at 02:00 the next day.
    The box start's deferral to the same 02:00 opening is still queued,
    because only a deferred start counts as one; at 02:00 the member starts
    and the box completes after it."""
    text = (
        "insert_job: box33d2\njob_type: b\n\n"
        "insert_job: a33d2\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box33d2\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "16:00"\n'
        'run_window: "02:00-04:00"\nmust_start_times: +600\n\n'
        "insert_job: clock33d2\njob_type: c\ncommand: k\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 16, 0), kind="STARTJOB", payload={"job": "a33d2"}))
    assert _timers(o, "a33d2") == [(datetime(2026, 7, 2, 2, 0), "a33d2", "must_start")]
    o.feed(Event(at=datetime(2026, 7, 1, 16, 5), kind="STARTJOB", payload={"job": "box33d2"}))
    assert transitions(o, "a33d2")[-1] == "RUN_WINDOW_DEFER"
    assert sorted(kind for _, _, kind in _timers(o, "a33d2")) == ["must_start", "run_window"]
    o.feed(ev_at(datetime(2026, 7, 2, 2, 0), "STATUS", job="clock33d2", status="SUCCESS"))
    assert o.store.job["a33d2"].status == "RUNNING"
    o.feed(ev_at(datetime(2026, 7, 2, 2, 30), "STATUS", job="a33d2", status="SUCCESS"))
    assert o.store.job["box33d2"].status == "SUCCESS"


def test_sem33_deferral_from_an_earlier_box_run_is_refused_after_a_restart() -> None:
    """T33 (SEM-33, DL-246): a deferral belongs to the box run that queued
    it. The box completes and starts again before the opening; the new run
    records its own deferral, the old timer is not pending, and at the
    opening the old one is refused with a record while the new one starts
    the member."""
    o = oracle(_BOX1_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 16, 5), kind="STARTJOB", payload={"job": "box1_33v"}))
    o.feed(ev_at(datetime(2026, 7, 1, 17, 0), "STATUS", job="box1_33v", status="SUCCESS"))
    o.feed(
        Event(at=datetime(2026, 7, 1, 20, 0), kind="FORCE_STARTJOB", payload={"job": "box1_33v"})
    )
    assert transitions(o, "joba_33v") == ["RUN_WINDOW_DEFER", "RUN_WINDOW_DEFER"]
    assert _timers(o, "joba_33v") == [(datetime(2026, 7, 2, 2, 0), "joba_33v", "run_window")]
    o.feed(ev_at(datetime(2026, 7, 2, 2, 0), "STATUS", job="clock_33v", status="SUCCESS"))
    assert transitions(o, "joba_33v")[2:] == [
        "START_REFUSED",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    [refused] = [t for t in o.trace() if t.job == "joba_33v" and t.transition == "START_REFUSED"]
    assert "the box has started again since" in refused.cause
    assert o.store.job["joba_33v"].run_number == 1  # one start, by the new run's deferral


@pytest.mark.parametrize(
    ("hour", "outer", "joba"),
    [(4, "SUCCESS", "RUN_WINDOW_SKIP"), (16, "RUNNING", "RUN_WINDOW_DEFER")],
)
def test_sem33_box_start_decides_a_member_of_a_subbox(hour: int, outer: str, joba: str) -> None:
    """T33 (SEM-33, DL-246), nested: the member sits in a subbox of the
    started box. The subbox starts with the outer box, and its own start
    decides the member: at 04:05 the skip lets both boxes complete, at
    16:05 the deferral keeps both RUNNING with one start queued."""
    text = (
        "insert_job: out33n\njob_type: b\n\n"
        "insert_job: sub33n\njob_type: b\nbox_name: out33n\n\n"
        "insert_job: joba33n\njob_type: c\ncommand: a\nmachine: m1\nbox_name: sub33n\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: jobb33n\njob_type: c\ncommand: b\nmachine: m1\nbox_name: sub33n\n\n"
        "insert_job: jobc33n\njob_type: c\ncommand: c\nmachine: m1\nbox_name: out33n\n"
    )
    o = oracle(text)
    start = datetime(2026, 7, 1, hour, 5)
    o.feed(Event(at=start, kind="STARTJOB", payload={"job": "out33n"}))
    assert transitions(o, "joba33n") == [joba]
    o.feed(ev_at(start + timedelta(minutes=5), "STATUS", job="jobb33n", status="SUCCESS"))
    o.feed(ev_at(start + timedelta(minutes=10), "STATUS", job="jobc33n", status="SUCCESS"))
    assert _status(o, "out33n", "sub33n") == [outer, outer]
    expected = [] if outer == "SUCCESS" else [(datetime(2026, 7, 2, 2, 0), "joba33n", "run_window")]
    assert _timers(o, "joba33n") == expected


def test_sem33_deferred_box_start_at_the_opening_arms_on_a_false_condition() -> None:
    """T33 (SEM-33, DL-246 provisional): the deferred STARTJOB is a start
    attempt with a schedule tick's standing, through the normal gates. At
    the opening JobA's condition is false, so the attempt arms (SEM-32),
    and the condition edge inside the window starts it."""
    text = (
        "insert_job: box33p\njob_type: b\n\n"
        "insert_job: joba33p\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box33p\n"
        "condition: s(feed33p)\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: feed33p\njob_type: c\ncommand: f\nmachine: m1\n\n"
        "insert_job: clock33p\njob_type: c\ncommand: k\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 16, 5), kind="STARTJOB", payload={"job": "box33p"}))
    assert transitions(o, "joba33p") == ["RUN_WINDOW_DEFER"]
    o.feed(ev_at(datetime(2026, 7, 2, 2, 0), "STATUS", job="clock33p", status="SUCCESS"))
    assert transitions(o, "joba33p") == ["RUN_WINDOW_DEFER", "SCHED_ARM"]
    o.feed(ev_at(datetime(2026, 7, 2, 2, 20), "STATUS", job="feed33p", status="SUCCESS"))
    assert transitions(o, "joba33p")[-2:] == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert o.store.job["box33p"].status == "RUNNING"


def test_sem33_deferred_box_start_counts_as_the_members_run_for_the_box_execution() -> None:
    """T33 (SEM-33, DL-246 provisional): at most one start per box run
    still holds. The deferred start at 02:00 runs JobA; its own 02:30 tick
    in the same box execution is refused (SEM-10)."""
    text = (
        "insert_job: box33q\njob_type: b\n\n"
        "insert_job: joba33q\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box33q\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:30"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: jobb33q\njob_type: c\ncommand: b\nmachine: m1\nbox_name: box33q\n\n"
        "insert_job: clock33q\njob_type: c\ncommand: k\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 16, 5), kind="STARTJOB", payload={"job": "box33q"}))
    o.feed(ev_at(datetime(2026, 7, 2, 2, 0), "STATUS", job="clock33q", status="SUCCESS"))
    o.feed(ev_at(datetime(2026, 7, 2, 2, 10), "STATUS", job="joba33q", status="SUCCESS"))
    assert o.store.job["box33q"].status == "RUNNING"  # JobB still runs
    o.feed(Event(at=datetime(2026, 7, 2, 2, 30), kind="STARTJOB", payload={"job": "joba33q"}))
    assert transitions(o, "joba33q") == [
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
        "START_REFUSED",
    ]
    refused = [t for t in o.trace() if t.job == "joba33q" and t.transition == "START_REFUSED"]
    assert "already ran in this 'box33q' execution" in refused[0].cause


_STANDALONE_SKIP_JIL = (
    "insert_job: rw33s\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
    'run_window: "02:00-04:00"\n\n'
    "insert_job: gate33s\njob_type: c\ncommand: g\nmachine: m1\n\n"
    "insert_job: cs33s\njob_type: c\ncommand: s\nmachine: m1\n"
    "condition: s(rw33s) & s(gate33s)\n\n"
    "insert_job: cf33s\njob_type: c\ncommand: f\nmachine: m1\n"
    "condition: f(rw33s) & s(gate33s)\n\n"
    "insert_job: cn33s\njob_type: c\ncommand: n\nmachine: m1\n"
    "condition: n(rw33s, 00.05) & v(GO33S) = 1\n"
)


@pytest.mark.parametrize(("code", "prior"), [(0, "SUCCESS"), (1, "FAILURE")])
def test_sem33_standalone_skip_moves_a_prior_result_to_inactive(code: int, prior: str) -> None:
    """T33b (SEM-33 [V], DL-246): "When the current time is closer to the
    end of the previous run window, the product does not start the job and
    changes its status to INACTIVE" (TechDocs 24.2, run_window page). A
    standalone job's prior SUCCESS or FAILURE moves to INACTIVE at the
    skip, so the s() and f() consumers read false when their other
    conjunct completes. The transition wakes referencers as an injected
    INACTIVE does: the n() consumer with a lookback reads the moved status
    time and starts. The exit code stays, as an injected INACTIVE keeps
    it. Nothing starts the skipped job."""
    o = oracle(_STANDALONE_SKIP_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "rw33s"}))
    o.feed(ev_at(datetime(2026, 7, 1, 2, 10), "STATUS", job="rw33s", exit_code=code))
    assert o.store.job["rw33s"].status == prior
    o.feed(ev_at(datetime(2026, 7, 1, 3, 0), "SET_GLOBAL", name="GO33S", value="1"))
    assert o.store.job["cn33s"].status == "INACTIVE"  # the lookback has lapsed
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="STARTJOB", payload={"job": "rw33s"}))
    assert transitions(o, "rw33s")[-2:] == ["RUN_WINDOW_SKIP", f"{prior}->INACTIVE"]
    assert o.store.job["rw33s"].exit_code == code
    assert _timers(o, "rw33s") == []
    [woken] = [t for t in o.trace() if t.job == "cn33s" and t.transition.endswith("STARTING")]
    assert woken.cause == "status of 'rw33s' changed to INACTIVE"
    o.feed(ev_at(datetime(2026, 7, 1, 4, 20), "STATUS", job="gate33s", status="SUCCESS"))
    assert _status(o, "cs33s", "cf33s") == ["INACTIVE", "INACTIVE"]
    assert transitions(o, "cs33s") == transitions(o, "cf33s") == []
    starts = [t for t in transitions(o, "rw33s") if t.endswith("->STARTING")]
    assert starts == ["INACTIVE->STARTING"]  # the 02:00 run only


def test_sem33_standalone_skip_on_a_fresh_inactive_job_records_no_transition() -> None:
    """T33b (SEM-33, DL-246): a standalone job that is already INACTIVE
    stays as it is at a skip. Only the RUN_WINDOW_SKIP record appears, and
    the status time does not move."""
    o = oracle(_STANDALONE_SKIP_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="STARTJOB", payload={"job": "rw33s"}))
    assert transitions(o, "rw33s") == ["RUN_WINDOW_SKIP"]
    assert o.store.job["rw33s"].status == "INACTIVE"
    assert o.store.job["rw33s"].status_at is None


def test_sem33_standalone_skip_moves_a_prior_terminated_to_inactive() -> None:
    """T33b (SEM-33, DL-246): a killed standalone job's TERMINATED moves to
    INACTIVE at a skip, as SUCCESS and FAILURE do."""
    o = oracle(_STANDALONE_SKIP_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "rw33s"}))
    o.feed(Event(at=datetime(2026, 7, 1, 2, 5), kind="KILLJOB", payload={"job": "rw33s"}))
    assert o.store.job["rw33s"].status == "TERMINATED"
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="STARTJOB", payload={"job": "rw33s"}))
    assert transitions(o, "rw33s")[-2:] == ["RUN_WINDOW_SKIP", "TERMINATED->INACTIVE"]


def test_sem33_force_start_on_a_held_standalone_job_meets_the_skip() -> None:
    """T33b (SEM-33/SEM-23, DL-246): a FORCE_STARTJOB passes the hold but
    not run_window. On the skip branch the held job's prior SUCCESS moves to
    INACTIVE and nothing starts. The hold stays cleared: DL-243 makes the
    return to an executable state the event's own effect."""
    o = oracle(_STANDALONE_SKIP_JIL)
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "rw33s"}))
    o.feed(ev_at(datetime(2026, 7, 1, 2, 10), "STATUS", job="rw33s", status="SUCCESS"))
    o.feed(Event(at=datetime(2026, 7, 1, 3, 0), kind="ON_HOLD", payload={"job": "rw33s"}))
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="FORCE_STARTJOB", payload={"job": "rw33s"}))
    assert transitions(o, "rw33s")[-3:] == ["OFF_HOLD", "RUN_WINDOW_SKIP", "SUCCESS->INACTIVE"]
    assert not o.store.job["rw33s"].on_hold
    assert o.store.job["rw33s"].run_number == 1


def test_sem33_standalone_box_skip_moves_only_the_box_to_inactive() -> None:
    """T33b (SEM-33, DL-246): a standalone box on the skip branch takes the
    plain INACTIVE transition. It does not cascade (SEM-18 is the
    operator's rule), so its member keeps the last run's SUCCESS."""
    text = (
        "insert_job: sbx33s\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
        'run_window: "02:00-04:00"\n\n'
        "insert_job: m33s\njob_type: c\ncommand: m\nmachine: m1\nbox_name: sbx33s\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 2, 0), kind="STARTJOB", payload={"job": "sbx33s"}))
    o.feed(ev_at(datetime(2026, 7, 1, 2, 10), "STATUS", job="m33s", status="SUCCESS"))
    assert o.store.job["sbx33s"].status == "SUCCESS"
    o.feed(Event(at=datetime(2026, 7, 1, 4, 10), kind="STARTJOB", payload={"job": "sbx33s"}))
    assert transitions(o, "sbx33s")[-2:] == ["RUN_WINDOW_SKIP", "SUCCESS->INACTIVE"]
    assert _status(o, "sbx33s", "m33s") == ["INACTIVE", "SUCCESS"]
    assert transitions(o, "m33s")[-1] == "RUNNING->SUCCESS"


def test_sem33_window_is_read_in_the_job_timezone_not_the_engine_clock() -> None:
    """T33c (SEM-33 x SEM-35): `timezone:` re-bases every time attribute of
    the job, run_window included. The engine clock is naive UTC (the
    scheduler converts local ticks to it and nothing converted them back),
    so a New York job with run_window "08:00-10:00" is INSIDE its window at
    13:00 UTC -- 09:00 local -- and OUTSIDE it at 09:00 UTC, which is 05:00
    local: three hours from the next opening, nineteen from the previous
    close, so the closer edge defers it. Same catalog, same window, two
    engine instants; the clock domain is the whole difference."""
    text = (
        "insert_job: rw_tz\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "08:00-10:00"\ntimezone: America/New_York\n'
    )
    inside = oracle(text)  # 13:00 UTC == 09:00 EDT: inside
    inside.feed(Event(at=datetime(2026, 7, 1, 13, 0), kind="STARTJOB", payload={"job": "rw_tz"}))
    assert transitions(inside, "rw_tz") == ["INACTIVE->STARTING", "STARTING->RUNNING"]

    outside = oracle(text)  # 09:00 UTC == 05:00 EDT: outside, closer to the next opening
    outside.feed(Event(at=datetime(2026, 7, 1, 9, 0), kind="STARTJOB", payload={"job": "rw_tz"}))
    assert transitions(outside, "rw_tz") == ["RUN_WINDOW_DEFER"]


def test_sem33_window_without_a_timezone_stays_on_the_engine_clock() -> None:
    """T33c (SEM-33 x SEM-35, the control): the same window on a job that
    declares no `timezone:` is compared on the engine clock unchanged --
    13:00 is outside "08:00-10:00" and 09:00 is inside. Pins that the tz
    re-basing is per-job and does not leak into the ordinary case."""
    text = (
        "insert_job: rw_utc\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "08:00-10:00"\n'
    )
    inside = oracle(text)
    inside.feed(Event(at=datetime(2026, 7, 1, 9, 0), kind="STARTJOB", payload={"job": "rw_utc"}))
    assert transitions(inside, "rw_utc") == ["INACTIVE->STARTING", "STARTING->RUNNING"]

    outside = oracle(text)
    outside.feed(Event(at=datetime(2026, 7, 1, 13, 0), kind="STARTJOB", payload={"job": "rw_utc"}))
    assert transitions(outside, "rw_utc") == ["RUN_WINDOW_SKIP"]


def test_sem33_no_timezone_job_reads_run_window_in_default_tz() -> None:
    """T33c (SEM-33 x SEM-35, DL-155): a job that declares no `timezone:`
    reads its run_window in the constructor's `default_tz` -- the vendor's
    "time zone under which the scheduler is running" (TechDocs 12.0.01,
    timezone attribute). Mixed catalog, Tokyo default: 00:30 UTC is 09:30
    Tokyo, INSIDE "08:00-10:00" for the bare job, where the engine clock
    (the default_tz=None basis, pinned by the control test above) would
    defer it -- 7.5h to the next opening against 14.5h since the previous
    close. The NY job is untouched: its own `timezone:` outranks the
    default -- 13:00 UTC is 09:00 New York, inside, although it is 22:00
    Tokyo."""
    text = (
        "insert_job: rw_bare\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "08:00-10:00"\n\n'
        "insert_job: rw_ny\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "08:00-10:00"\ntimezone: America/New_York\n'
    )
    o = oracle(text, default_tz="Asia/Tokyo")
    o.feed(Event(at=datetime(2026, 7, 1, 0, 30), kind="STARTJOB", payload={"job": "rw_bare"}))
    assert transitions(o, "rw_bare") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(Event(at=datetime(2026, 7, 1, 13, 0), kind="STARTJOB", payload={"job": "rw_ny"}))
    assert transitions(o, "rw_ny") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem35_default_tz_resolves_through_the_alias_map_like_a_job_timezone() -> None:
    """DL-155 x DL-151: `default_tz` walks the same SEM-35 ladder as a
    job's own `timezone:` -- a ujo_timezones alias resolves through the
    supplied map, and an unresolvable name refuses with the same named
    OracleError a job's own bad zone raises. A second resolution path
    would reopen the DL-151 divergence class."""
    text = (
        "insert_job: rw_alias\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        'run_window: "08:00-10:00"\n'
    )
    aliased = oracle(text, default_tz="tokyo", tz_aliases={"tokyo": "Asia/Tokyo"})
    aliased.feed(
        Event(at=datetime(2026, 7, 1, 0, 30), kind="STARTJOB", payload={"job": "rw_alias"})
    )
    assert transitions(aliased, "rw_alias") == ["INACTIVE->STARTING", "STARTING->RUNNING"]

    broken = oracle(text, default_tz="Notaplace/Nowhere")
    with pytest.raises(OracleError, match="not resolvable"):
        broken.feed(
            Event(at=datetime(2026, 7, 1, 0, 30), kind="STARTJOB", payload={"job": "rw_alias"})
        )


def test_sem33_deferred_start_timer_is_the_engine_instant_of_the_local_opening() -> None:
    """T33c (SEM-33 x SEM-35): the closer-edge DEFER is decided on local
    wall time but the timer it queues is an ENGINE instant. A Tokyo job
    (UTC+9, no DST) attempting at 00:50 local -- 15:50 UTC the day before --
    is 10 minutes from the 01:00 local opening, so the STARTJOB is queued
    for 16:00 UTC, and that is when the job actually starts."""
    text = (
        "insert_job: rw_tokyo\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "00:50"\n'
        'run_window: "01:00-02:00"\ntimezone: Asia/Tokyo\n\n'
        "insert_job: dummy_tokyo\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 6, 30, 15, 50), kind="STARTJOB", payload={"job": "rw_tokyo"}))
    assert transitions(o, "rw_tokyo") == ["RUN_WINDOW_DEFER"]
    o.feed(
        Event(
            at=datetime(2026, 6, 30, 16, 5),
            kind="STATUS",
            payload={"job": "dummy_tokyo", "status": "SUCCESS"},
        )
    )
    start = next(t for t in o.trace() if t.job == "rw_tokyo" and t.transition.endswith("STARTING"))
    assert start.at == datetime(2026, 6, 30, 16, 0)  # 01:00 Tokyo, on the engine clock


# DL-249: run_window endpoints across a DST change, America/New_York. The
# vendor text is TechDocs 12.1 and 24.2, "Daylight Time Changes" and
# "Standard Time Changes". 2026-03-08: 02:00 EST jumps to 03:00 EDT at 07:00
# UTC. 2026-11-01: 02:00 EDT falls back to 01:00 EST at 06:00 UTC.


def _dst_window_jil(window: str, zone: str = "America/New_York") -> str:
    return (
        "insert_job: rw_dst\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "00:00"\n'
        f'run_window: "{window}"\ntimezone: {zone}\n\n'
        "insert_job: dummy_dst\njob_type: c\ncommand: y\nmachine: m1\n"
    )


def _dst_attempt(window: str, at: datetime, zone: str = "America/New_York") -> Oracle:
    o = oracle(_dst_window_jil(window, zone))
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "rw_dst"}))
    return o


_STARTED = ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_sem33_spring_close_in_the_missing_hour_keeps_the_window_length() -> None:
    """SEM-33, DL-249: "the product recalculates a run window of 1:00 - 2:30
    so that the window ends at 3:30 and the run window remains open for 90
    minutes". 03:15 EDT is inside; 03:30 EDT is the inclusive close, and
    03:31 EDT is closer to that close, so the attempt is skipped."""
    assert transitions(_dst_attempt("01:00-02:30", datetime(2026, 3, 8, 7, 15)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("01:00-02:30", datetime(2026, 3, 8, 7, 30)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("01:00-02:30", datetime(2026, 3, 8, 7, 31)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_spring_opening_in_the_missing_hour_moves_to_0300() -> None:
    """SEM-33, DL-249: "a run window of 2:45 - 3:45 becomes 3:00 - 3:45".
    At 01:59 EST the next opening is 03:00 EDT, one minute away, so the
    deferred start is queued there, not at 03:45 EDT, where 02:45 EST maps.
    03:00 EDT is inside, 03:45 EDT is the close, and 03:46 EDT is skipped."""
    o = _dst_attempt("02:45-03:45", datetime(2026, 3, 8, 6, 59))
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 3, 8, 7, 0), "rw_dst", "run_window")]
    o.feed(ev_at(datetime(2026, 3, 8, 7, 5), "STATUS", job="dummy_dst", status="SUCCESS"))
    start = next(t for t in o.trace() if t.job == "rw_dst" and t.transition.endswith("STARTING"))
    assert start.at == datetime(2026, 3, 8, 7, 0)
    assert transitions(_dst_attempt("02:45-03:45", datetime(2026, 3, 8, 7, 0)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("02:45-03:45", datetime(2026, 3, 8, 7, 45)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("02:45-03:45", datetime(2026, 3, 8, 7, 46)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_spring_window_wholly_in_the_missing_hour_becomes_0300_to_0345() -> None:
    """SEM-33, DL-249: "When both the start time and the end time of the run
    window, fall during the missing hour, AutoSys Workload Automation moves
    the start time to the first minute after 3:00 and the end time to one
    hour later ... a run window of 2:15 - 2:45 becomes 3:00 - 3:45". At
    01:30 EST the deferral goes to 03:00 EDT. 03:00 and 03:45 EDT are
    inside; 03:46 EDT is skipped."""
    o = _dst_attempt("02:15-02:45", datetime(2026, 3, 8, 6, 30))
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 3, 8, 7, 0), "rw_dst", "run_window")]
    for inside in (datetime(2026, 3, 8, 7, 0), datetime(2026, 3, 8, 7, 45)):
        assert transitions(_dst_attempt("02:15-02:45", inside), "rw_dst") == _STARTED
    assert transitions(_dst_attempt("02:15-02:45", datetime(2026, 3, 8, 7, 46)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_fall_close_in_the_repeated_hour_is_the_daylight_pass() -> None:
    """SEM-33, DL-249: "a run window of 11:30 - 1:30 ends at 1:30 DT, not
    1:30 ST". 01:30 EDT is the inclusive close. 01:31 EDT is skipped, and so
    is 01:20 EST, whose wall time reads inside the window but whose instant
    is fifty minutes past the close."""
    assert transitions(_dst_attempt("23:30-01:30", datetime(2026, 11, 1, 5, 20)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("23:30-01:30", datetime(2026, 11, 1, 5, 30)), "rw_dst") == (
        _STARTED
    )
    for after in (datetime(2026, 11, 1, 5, 31), datetime(2026, 11, 1, 6, 20)):
        assert transitions(_dst_attempt("23:30-01:30", after), "rw_dst") == ["RUN_WINDOW_SKIP"]


def test_sem33_fall_opening_in_the_repeated_hour_is_the_standard_pass() -> None:
    """SEM-33, DL-249: "a run window of 1:45 - 2:45 becomes 1:45 ST - 2:45
    ST". At 01:50 EDT the window is not open yet: the opening is 01:45 EST,
    55 minutes on, so the start is deferred there. 01:45 EST and 02:45 EST
    are inside; 02:46 EST is skipped."""
    o = _dst_attempt("01:45-02:45", datetime(2026, 11, 1, 5, 50))
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 11, 1, 6, 45), "rw_dst", "run_window")]
    for inside in (datetime(2026, 11, 1, 6, 45), datetime(2026, 11, 1, 7, 45)):
        assert transitions(_dst_attempt("01:45-02:45", inside), "rw_dst") == _STARTED
    assert transitions(_dst_attempt("01:45-02:45", datetime(2026, 11, 1, 7, 46)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_fall_window_wholly_in_the_repeated_hour_opens_in_the_second() -> None:
    """SEM-33, DL-249: "When both the specified start and end of the run
    window occur during the repeated hour, the run window opens during the
    second, standard time hour." 01:10-01:40 is 01:10-01:40 EST. At 01:20
    EDT the start is deferred to 01:10 EST; 01:20 EST is inside, and 01:41
    EST is skipped."""
    o = _dst_attempt("01:10-01:40", datetime(2026, 11, 1, 5, 20))
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 11, 1, 6, 10), "rw_dst", "run_window")]
    assert transitions(_dst_attempt("01:10-01:40", datetime(2026, 11, 1, 6, 20)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("01:10-01:40", datetime(2026, 11, 1, 6, 41)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_dst_window_control_on_ordinary_days_is_unchanged() -> None:
    """SEM-33, DL-249, the control: a week before the change (the wall-time
    path, EST) and the day after it (the interval path, EDT, no endpoint in
    a missing hour), 01:00-02:30 is 01:00-02:30 local. 02:15 is inside, and
    02:31 and 03:15 are skipped."""
    for day, utc_offset in ((datetime(2026, 3, 1), 5), (datetime(2026, 3, 9), 4)):

        def at(
            hour: int, minute: int, day: datetime = day, utc_offset: int = utc_offset
        ) -> datetime:
            return day + timedelta(hours=hour + utc_offset, minutes=minute)

        assert transitions(_dst_attempt("01:00-02:30", at(2, 15)), "rw_dst") == _STARTED
        for after in (at(2, 31), at(3, 15)):
            assert transitions(_dst_attempt("01:00-02:30", after), "rw_dst") == ["RUN_WINDOW_SKIP"]


def test_sem33_dst_window_in_a_zone_without_dst_is_unchanged() -> None:
    """SEM-33, DL-249, the control: America/Phoenix keeps MST (UTC-7) on
    2026-03-08, so 01:00-02:30 is not lengthened there. 02:15 is inside and
    03:15 is skipped."""
    phoenix = "America/Phoenix"
    assert (
        transitions(_dst_attempt("01:00-02:30", datetime(2026, 3, 8, 9, 15), phoenix), "rw_dst")
        == _STARTED
    )
    assert transitions(
        _dst_attempt("01:00-02:30", datetime(2026, 3, 8, 10, 15), phoenix), "rw_dst"
    ) == ["RUN_WINDOW_SKIP"]


def test_sem33_box_start_on_a_spring_change_defers_to_0300() -> None:
    """SEM-33, DL-246 x DL-249: a box start decides its member's window on
    the change day with the same endpoints. Started at 01:30 EST, the member
    with "02:45-03:45" is deferred to 03:00 EDT, where it starts."""
    text = (
        "insert_job: box_dst\njob_type: b\n\n"
        "insert_job: m_dst\njob_type: c\ncommand: a\nmachine: m1\nbox_name: box_dst\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:45"\n'
        'run_window: "02:45-03:45"\ntimezone: America/New_York\n\n'
        "insert_job: clock_dst\njob_type: c\ncommand: k\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 3, 8, 6, 30), kind="STARTJOB", payload={"job": "box_dst"}))
    assert transitions(o, "m_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "m_dst") == [(datetime(2026, 3, 8, 7, 0), "m_dst", "run_window")]
    o.feed(ev_at(datetime(2026, 3, 8, 7, 5), "STATUS", job="clock_dst", status="SUCCESS"))
    assert transitions(o, "m_dst") == ["RUN_WINDOW_DEFER", *_STARTED]
    start = next(t for t in o.trace() if t.job == "m_dst" and t.transition.endswith("STARTING"))
    assert start.at == datetime(2026, 3, 8, 7, 0)
    assert o.store.job["box_dst"].status == "RUNNING"


def test_sem33_dst_equal_endpoints_stay_one_instant() -> None:
    """SEM-33, DL-249: the vendor's DST rules describe two distinct
    endpoints, so the zero-width pin stands on a change day. "02:30-02:30"
    on the spring change is the one instant 03:00 EDT: 03:00 starts, and
    03:15 is skipped. "01:30-01:30" on the fall change opens in the second
    pass, so 01:30 EDT defers to 01:30 EST, which starts."""
    assert transitions(_dst_attempt("02:30-02:30", datetime(2026, 3, 8, 7, 0)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("02:30-02:30", datetime(2026, 3, 8, 7, 15)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]
    o = _dst_attempt("01:30-01:30", datetime(2026, 11, 1, 5, 30))
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 11, 1, 6, 30), "rw_dst", "run_window")]
    assert transitions(_dst_attempt("01:30-01:30", datetime(2026, 11, 1, 6, 30)), "rw_dst") == (
        _STARTED
    )


def test_sem33_spring_window_crossing_midnight_into_the_missing_hour() -> None:
    """SEM-33, DL-249: "22:00-02:30" opens at 22:00 EST the evening before
    and its close in the missing hour keeps the length, so it ends at 03:30
    EDT. 03:15 EDT is inside and 03:31 EDT is skipped."""
    assert transitions(_dst_attempt("22:00-02:30", datetime(2026, 3, 8, 7, 15)), "rw_dst") == (
        _STARTED
    )
    assert transitions(_dst_attempt("22:00-02:30", datetime(2026, 3, 8, 7, 31)), "rw_dst") == [
        "RUN_WINDOW_SKIP"
    ]


def test_sem33_dst_window_at_the_guards_edges_is_ordinary() -> None:
    """SEM-33, DL-249: two days either side of the change take the interval
    path and three days take the wall-time path. On all four days
    01:00-02:30 is 01:00-02:30 local: 02:15 is inside and 03:15 skipped."""
    for day, utc_offset in (
        (datetime(2026, 3, 5), 5),
        (datetime(2026, 3, 6), 5),
        (datetime(2026, 3, 10), 4),
        (datetime(2026, 3, 11), 4),
    ):
        inside = day + timedelta(hours=2 + utc_offset, minutes=15)
        after = day + timedelta(hours=3 + utc_offset, minutes=15)
        assert transitions(_dst_attempt("01:00-02:30", inside), "rw_dst") == _STARTED
        assert transitions(_dst_attempt("01:00-02:30", after), "rw_dst") == ["RUN_WINDOW_SKIP"]


def test_sem33_dst_rules_follow_the_offset_shape_not_the_zone_name() -> None:
    """SEM-33, DL-249: coverage is the change's shape. Australia/Sydney's
    spring change (2026-10-04, 02:00 AEST to 03:00 AEDT) has it, so
    01:00-02:30 is open at 03:15 AEDT and closed at 03:31. Europe/London's
    fall change (2026-10-25, 02:00 BST to 01:00 GMT) has it too, so at 01:50
    BST the window 01:45-02:45 defers to 01:45 GMT."""
    sydney = "Australia/Sydney"
    assert transitions(
        _dst_attempt("01:00-02:30", datetime(2026, 10, 3, 16, 15), sydney), "rw_dst"
    ) == (_STARTED)
    assert transitions(
        _dst_attempt("01:00-02:30", datetime(2026, 10, 3, 16, 31), sydney), "rw_dst"
    ) == ["RUN_WINDOW_SKIP"]
    o = _dst_attempt("01:45-02:45", datetime(2026, 10, 25, 0, 50), "Europe/London")
    assert transitions(o, "rw_dst") == ["RUN_WINDOW_DEFER"]
    assert _timers(o, "rw_dst") == [(datetime(2026, 10, 25, 1, 45), "rw_dst", "run_window")]


@pytest.mark.parametrize("zone", [None, "America/New_York"])
@pytest.mark.parametrize(
    "at",
    [
        datetime(1, 1, 1, 12, 0),
        datetime(1, 1, 2, 12, 0),
        datetime(9999, 12, 30, 12, 0),
        datetime(9999, 12, 31, 12, 0),
    ],
)
def test_sem33_dst_guard_at_the_ends_of_the_date_range(at: datetime, zone: str | None) -> None:
    """SEM-33, DL-249: the DST guard looks two days either side of the
    attempt. A day outside the calendar's range counts as no change, so an
    all-day window still admits a start at both ends, with or without a
    zone."""
    text = (
        "insert_job: rw_end\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "00:00"\n'
        'run_window: "00:00-23:59"\n' + (f"timezone: {zone}\n" if zone else "")
    )
    o = oracle(text)
    o.feed(Event(at=at, kind="STARTJOB", payload={"job": "rw_end"}))
    assert transitions(o, "rw_end") == _STARTED


# --------------------------------------------------------------------- 19. SEM-34 must_*


def test_sem34c_each_start_time_arms_its_own_must_complete_offset() -> None:
    """T34c (SEM-34): "+n minutes from each start time" under the strict
    count match -- two start_times with two offsets pair BY POSITION. The
    08:00 tick's must_complete deadline is +7, the 09:00 tick's is +40: the
    second run is still quiet at 09:07, where the first run alarmed at
    08:07. Before the fix every tick read offsets_min[0], so the second
    start inherited the first slot's deadline and every later offset was
    dead configuration."""
    text = (
        "insert_job: sla34c\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00, 09:00"\n'
        "must_complete_times: +7, +40\n\n"
        "insert_job: dummy34c\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    first = oracle(text)
    first.feed(Event(at=datetime(2026, 7, 1, 8, 0), kind="STARTJOB", payload={"job": "sla34c"}))
    assert transitions(first, "sla34c") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    alarms = first.feed(
        Event(
            at=datetime(2026, 7, 1, 8, 7),
            kind="STATUS",
            payload={"job": "dummy34c", "status": "SUCCESS"},
        )
    )
    assert any(e.kind == "MUST_COMPLETE_ALARM" and e.payload.get("job") == "sla34c" for e in alarms)

    second = oracle(text)
    second.feed(Event(at=datetime(2026, 7, 1, 9, 0), kind="STARTJOB", payload={"job": "sla34c"}))
    quiet = second.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 7),  # the FIRST slot's offset: not this tick's
            kind="STATUS",
            payload={"job": "dummy34c", "status": "SUCCESS"},
        )
    )
    assert all(e.kind != "MUST_COMPLETE_ALARM" for e in quiet)
    alarms = second.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 40),
            kind="STATUS",
            payload={"job": "dummy34c", "status": "FAILURE"},
        )
    )
    assert any(e.kind == "MUST_COMPLETE_ALARM" and e.payload.get("job") == "sla34c" for e in alarms)


def test_sem34c_must_start_alarm_uses_the_slot_offset_of_its_own_tick() -> None:
    """T34c (SEM-34): the same pairing on the must_start half. The job's
    condition never becomes true, so no run begins and the alarm is due --
    at tick+30 for the 09:00 slot, and NOT at tick+5, which is the 08:00
    slot's offset and the value every tick used before the fix."""
    text = (
        "insert_job: sla34d\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00, 09:00"\n'
        "must_start_times: +5, +30\ncondition: s(gate34d)\n\n"
        "insert_job: gate34d\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: dummy34d\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 9, 0), kind="STARTJOB", payload={"job": "sla34d"}))
    quiet = o.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 5),  # the first slot's offset
            kind="STATUS",
            payload={"job": "dummy34d", "status": "SUCCESS"},
        )
    )
    assert all(e.kind != "MUST_START_ALARM" for e in quiet)
    alarms = o.feed(
        Event(
            at=datetime(2026, 7, 1, 9, 30),
            kind="STATUS",
            payload={"job": "dummy34d", "status": "FAILURE"},
        )
    )
    assert any(e.kind == "MUST_START_ALARM" and e.payload.get("job") == "sla34d" for e in alarms)
    # SCHED_ARM is the SEM-32 latch of the blocked tick; the alarm adds no
    # control flow of its own -- the job never started
    assert transitions(o, "sla34d") == ["SCHED_ARM", "MUST_START_ALARM"]


def test_sem34c_a_single_offset_still_broadcasts_over_every_start_time() -> None:
    """T34c (SEM-34, the exception left alone): one relative offset against
    several start_times is the dossier's own [?] corner -- the strict count
    rule and TechDocs' worked example disagree, and it stays a broadcast
    until a live instance decides. The 09:00 tick alarms at +5, the same
    offset the 08:00 tick gets."""
    text = (
        "insert_job: sla34e\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00, 09:00"\n'
        "must_complete_times: +5\n\n"
        "insert_job: dummy34e\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    for hour in (8, 9):
        o = oracle(text)
        o.feed(Event(at=datetime(2026, 7, 1, hour, 0), kind="STARTJOB", payload={"job": "sla34e"}))
        alarms = o.feed(
            Event(
                at=datetime(2026, 7, 1, hour, 5),
                kind="STATUS",
                payload={"job": "dummy34e", "status": "SUCCESS"},
            )
        )
        assert any(
            e.kind == "MUST_COMPLETE_ALARM" and e.payload.get("job") == "sla34e" for e in alarms
        )


def test_sem34a_must_complete_alarm_not_emitted_when_job_finishes_in_time() -> None:
    """T34a (SEM-34): must_complete_times: +5 arms a deadline timer relative
    to the tick (DL-248), here also the start. Completing at +2 (before the
    deadline) means the timer, when it eventually pops at +5, finds the
    tick's run complete -> no alarm ever, no matter how much later the
    clock advances."""
    text = (
        "insert_job: mc34\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +5\n\n"
        "insert_job: dummy34a\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="mc34"))
    o.feed(ev("STATUS", 2, job="mc34", status="SUCCESS"))
    emitted = o.feed(ev("STATUS", 10, job="dummy34a", status="SUCCESS"))  # past the +5 deadline
    assert all(e.kind != "MUST_COMPLETE_ALARM" for e in emitted)
    assert "MUST_COMPLETE_ALARM" not in transitions(o, "mc34")
    assert transitions(o, "mc34") == ["INACTIVE->STARTING", "STARTING->RUNNING", "RUNNING->SUCCESS"]


def test_sem34b_must_complete_alarm_fires_and_job_keeps_running() -> None:
    """T34b (SEM-34): still RUNNING when the +5 deadline is reached ->
    MUST_COMPLETE_ALARM is both emitted (as an Event) and recorded in the
    trace; it is an SLA annotation only -- the job is left RUNNING, no
    control-flow effect."""
    text = (
        "insert_job: mc34b\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +5\n\n"
        "insert_job: dummy34b\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="mc34b"))
    emitted = o.feed(ev("STATUS", 6, job="dummy34b", status="SUCCESS"))
    assert any(e.kind == "MUST_COMPLETE_ALARM" and e.payload.get("job") == "mc34b" for e in emitted)
    alarm_entries = [
        t for t in o.trace() if t.job == "mc34b" and t.transition == "MUST_COMPLETE_ALARM"
    ]
    assert len(alarm_entries) == 1
    assert "SEM-34" in alarm_entries[0].cause
    assert o.store.job["mc34b"].status == "RUNNING"  # no control flow


# DL-248: a relative must_complete deadline belongs to the schedule slot. The
# tick arms it at tick+offset; the run it asks for is the first to begin after
# the tick; a late start does not move it. TechDocs 24.2, must_complete_times:
# "The must complete times are calculated relative to the start_mins or
# start_times attributes."


def _mc_alarm_times(o: Oracle | EngineHarness, job: str) -> list[datetime]:
    return [t.at for t in o.trace() if t.job == job and t.transition == "MUST_COMPLETE_ALARM"]


def _mc_gated(name: str, *, start: str = 'start_times: "08:00"', offsets: str = "+8") -> str:
    """One gated job with a relative must_complete, its gate, and an idle
    job whose STATUS moves the clock without touching either."""
    return (
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"date_conditions: 1\ndays_of_week: all\n{start}\n"
        f"must_complete_times: {offsets}\ncondition: s({name}_gate)\n\n"
        f"insert_job: {name}_gate\njob_type: c\ncommand: y\nmachine: m1\n\n"
        f"insert_job: {name}_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )


def test_sem34_must_complete_alarms_at_tick_plus_offset_when_blocked_throughout() -> None:
    """T34 (SEM-34, DL-248): the 08:00 tick is blocked by its condition and
    the job never starts. The tick armed the deadline, so the alarm fires at
    08:08. The job stays INACTIVE: the alarm has no control flow."""
    o = oracle(_mc_gated("mcb"))
    o.feed(ev("STARTJOB", 0, job="mcb"))
    o.feed(ev("STATUS", 30, job="mcb_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcb") == [T0 + timedelta(minutes=8)]
    assert transitions(o, "mcb") == ["SCHED_ARM", "MUST_COMPLETE_ALARM"]
    assert o.store.job["mcb"].status == "INACTIVE"


def test_sem34_must_complete_late_start_alarms_at_the_slot_deadline_only() -> None:
    """T34 (SEM-34, DL-248): the vendor's "within 8 minutes after each start
    time". The 08:00 tick is blocked until 08:10. The deadline stays at
    08:08, where the alarm fires; the 08:10 start arms nothing, so 08:18
    is quiet. The run itself is unaffected and completes normally."""
    o = oracle(_mc_gated("mcl"))
    o.feed(ev("STARTJOB", 0, job="mcl"))
    o.feed(ev("STATUS", 10, job="mcl_gate", status="SUCCESS"))
    assert o.store.job["mcl"].status == "RUNNING"
    o.feed(ev("STATUS", 30, job="mcl_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcl") == [T0 + timedelta(minutes=8)]
    o.feed(ev("STATUS", 31, job="mcl", status="SUCCESS"))
    assert transitions(o, "mcl") == [
        "SCHED_ARM",
        "MUST_COMPLETE_ALARM",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_sem34_must_complete_late_start_that_completes_in_time_is_quiet() -> None:
    """T34 (SEM-34, DL-248): blocked at the 08:00 tick, started at 08:03,
    complete at 08:06. The run the slot asked for completed before 08:08,
    so no alarm, however late the clock runs."""
    o = oracle(_mc_gated("mcq"))
    o.feed(ev("STARTJOB", 0, job="mcq"))
    o.feed(ev("STATUS", 3, job="mcq_gate", status="SUCCESS"))
    o.feed(ev("STATUS", 6, job="mcq", status="FAILURE"))
    o.feed(ev("STATUS", 60, job="mcq_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcq") == []


def test_sem34_must_complete_distinct_offsets_anchor_to_their_own_tick() -> None:
    """T34c (SEM-34, DL-248): start_times 08:00 and 09:00 with +7 and +40.
    The 09:00 tick is blocked until 09:20 and the run is still RUNNING at
    09:40, its slot's deadline: the alarm is at 09:40, not at 10:00, which
    is the actual start plus 40."""
    o = oracle(_mc_gated("mcd", start='start_times: "08:00, 09:00"', offsets="+7, +40"))
    o.feed(ev("STARTJOB", 60, job="mcd"))
    o.feed(ev("STATUS", 80, job="mcd_gate", status="SUCCESS"))
    o.feed(ev("STATUS", 130, job="mcd_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcd") == [T0 + timedelta(minutes=100)]
    assert o.store.job["mcd"].status == "RUNNING"


def test_sem34_must_complete_with_start_mins_the_2_10_run_is_due_at_2_17() -> None:
    """T34 (SEM-34, DL-248): the vendor's start_mins example. Runs every 10
    minutes with +7: "the 2:10 p.m. job run must complete by 2:17 p.m."
    The 14:10 tick is blocked until 14:12; still RUNNING at 14:17, it
    alarms then. A second job of the same shape completes at 14:16 and is
    quiet."""
    start = "start_mins: 0, 10, 20, 30, 40, 50"
    tick = datetime(2026, 7, 1, 14, 10)
    late = oracle(_mc_gated("mcm", start=start, offsets="+7"))
    late.feed(ev_at(tick, "STARTJOB", job="mcm"))
    late.feed(ev_at(tick + timedelta(minutes=2), "STATUS", job="mcm_gate", status="SUCCESS"))
    late.feed(ev_at(tick + timedelta(minutes=9), "STATUS", job="mcm_idle", status="SUCCESS"))
    assert _mc_alarm_times(late, "mcm") == [datetime(2026, 7, 1, 14, 17)]

    quiet = oracle(_mc_gated("mcm", start=start, offsets="+7"))
    quiet.feed(ev_at(tick, "STARTJOB", job="mcm"))
    quiet.feed(ev_at(tick + timedelta(minutes=2), "STATUS", job="mcm_gate", status="SUCCESS"))
    quiet.feed(ev_at(tick + timedelta(minutes=6), "STATUS", job="mcm", status="SUCCESS"))
    quiet.feed(ev_at(tick + timedelta(minutes=9), "STATUS", job="mcm_idle", status="SUCCESS"))
    assert _mc_alarm_times(quiet, "mcm") == []


def test_sem34_must_complete_second_tick_while_the_first_deadline_is_pending() -> None:
    """T34 (SEM-34, DL-248): at most one pending deadline per job. Ticks at
    08:00 and 08:05 (+8) are both blocked; the 08:05 tick finds the 08:08
    deadline pending and arms nothing. The latched arm starts one run at
    08:06, which completes at 08:10, after 08:08: one alarm, at 08:08."""
    o = oracle(_mc_gated("mc2", start='start_times: "08:00, 08:05"'))
    o.feed(ev("STARTJOB", 0, job="mc2"))
    o.feed(ev("STARTJOB", 5, job="mc2"))
    o.feed(ev("STATUS", 6, job="mc2_gate", status="SUCCESS"))
    o.feed(ev("STATUS", 10, job="mc2", status="SUCCESS"))
    o.feed(ev("STATUS", 30, job="mc2_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mc2") == [T0 + timedelta(minutes=8)]


def test_sem34_must_complete_a_tick_on_a_live_job_arms_nothing() -> None:
    """T34 (SEM-34, DL-248): one pending deadline per job. The vendor inserts
    the next CHK_COMPLETE only "after the job completes". Runs every 10
    minutes with +7; the 14:00 run lasts to 14:12. It alarms at 14:07. The
    14:10 tick finds the job live, is refused, and arms nothing, so 14:17 is
    quiet."""
    o = oracle(
        "insert_job: mcr\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0, 10, 20, 30, 40, 50\n"
        "must_complete_times: +7\n\n"
        "insert_job: mcr_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    tick = datetime(2026, 7, 1, 14, 0)
    o.feed(ev_at(tick, "STARTJOB", job="mcr"))
    o.feed(ev_at(tick + timedelta(minutes=10), "STARTJOB", job="mcr"))
    assert "START_REFUSED" in transitions(o, "mcr")
    o.feed(ev_at(tick + timedelta(minutes=12), "STATUS", job="mcr", status="SUCCESS"))
    o.feed(ev_at(tick + timedelta(minutes=19), "STATUS", job="mcr_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcr") == [datetime(2026, 7, 1, 14, 7)]


def test_sem34_must_complete_two_latched_ticks_and_one_late_run_alarm_once() -> None:
    """T34 (SEM-34, DL-248): ticks at 08:00 and 08:05 (+8) are both blocked.
    The 08:05 tick finds the 08:08 deadline pending and arms nothing. The
    latched run starts at 08:06 and ends at 08:15: one alarm, at 08:08."""
    o = oracle(_mc_gated("mc1", start='start_times: "08:00, 08:05"'))
    o.feed(ev("STARTJOB", 0, job="mc1"))
    o.feed(ev("STARTJOB", 5, job="mc1"))
    o.feed(ev("STATUS", 6, job="mc1_gate", status="SUCCESS"))
    o.feed(ev("STATUS", 15, job="mc1", status="SUCCESS"))
    o.feed(ev("STATUS", 30, job="mc1_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mc1") == [T0 + timedelta(minutes=8)]


def test_sem34_must_complete_a_later_run_meets_the_deadline() -> None:
    """T34 (SEM-34, DL-248): the 08:00 tick's run ends at 08:02 and a forced
    second run begins at 08:04, still RUNNING at 08:08. A later run began,
    so the run the tick asked for had ended: no alarm."""
    o = oracle(_mc_gated("mcn"))
    o.feed(ev("STATUS", -1, job="mcn_gate", status="SUCCESS"))
    o.feed(ev("STARTJOB", 0, job="mcn"))
    o.feed(ev("STATUS", 2, job="mcn", status="SUCCESS"))
    o.feed(ev("FORCE_STARTJOB", 4, job="mcn"))
    o.feed(ev("STATUS", 30, job="mcn_idle", status="SUCCESS"))
    assert o.store.job["mcn"].status == "RUNNING"
    assert _mc_alarm_times(o, "mcn") == []


def test_sem34_must_complete_off_hold_release_arms_nothing() -> None:
    """T34 x SEM-21 (DL-248): held through its 08:08 deadline, the job
    alarms then. OFF_HOLD at 08:10 starts it and the run lasts to 08:30. The
    release is no tick, so 08:18 is quiet."""
    o = oracle(
        "insert_job: mcu\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +8\n\n"
        "insert_job: mcu_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o.feed(ev("ON_HOLD", 0, job="mcu"))
    o.feed(ev("STARTJOB", 0, job="mcu"))
    o.feed(ev("OFF_HOLD", 10, job="mcu"))
    assert o.store.job["mcu"].status == "RUNNING"
    o.feed(ev("STATUS", 30, job="mcu", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcu") == [T0 + timedelta(minutes=8)]


def test_sem34_must_complete_run_window_deferred_start_arms_nothing() -> None:
    """T34 x SEM-33 (DL-248): the 01:50 tick is deferred to the 02:00
    opening, so its +5 deadline alarms at 01:55. The deferred start runs to
    02:30 and is no tick, so 02:05 is quiet."""
    o = oracle(
        "insert_job: mcw\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:50"\n'
        'run_window: "02:00-03:00"\nmust_complete_times: +5\n\n'
        "insert_job: mcw_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    tick = datetime(2026, 7, 1, 1, 50)
    o.feed(ev_at(tick, "STARTJOB", job="mcw"))
    assert transitions(o, "mcw") == ["RUN_WINDOW_DEFER"]
    o.feed(ev_at(tick + timedelta(minutes=12), "STATUS", job="mcw_idle", status="SUCCESS"))
    assert o.store.job["mcw"].status == "RUNNING"
    o.feed(ev_at(tick + timedelta(minutes=40), "STATUS", job="mcw", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcw") == [datetime(2026, 7, 1, 1, 55)]


def test_sem34_must_complete_on_noexec_bypass_meets_the_deadline() -> None:
    """T34 x SEM-22 (DL-248): the bypass is the tick's run and ends SUCCESS
    at once, so the deadline is met."""
    o = oracle(
        "insert_job: mce\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +8\n\n"
        "insert_job: mce_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o.feed(ev("ON_NOEXEC", 0, job="mce"))
    o.feed(ev("STARTJOB", 0, job="mce"))
    o.feed(ev("STATUS", 30, job="mce_idle", status="SUCCESS"))
    assert o.store.job["mce"].status == "SUCCESS"
    assert _mc_alarm_times(o, "mce") == []


def test_sem34_must_complete_a_run_ended_during_its_starting_meets_the_deadline() -> None:
    """T34 x SEM-14 (DL-248), on the shape of
    test_sem10_a_box_its_start_terminated_is_not_overwritten_running: the
    20-minute tick on b is refused (it already ran this box execution) and
    arms a +5 deadline. A forced start of b is TERMINATED while STARTING by
    its parent's job_terminator cascade. That run ended, so 25 is quiet."""
    text = (
        "insert_job: p34s\njob_type: b\nbox_failure: s(nx34s)\n\n"
        "insert_job: b34s\njob_type: b\nbox_name: p34s\njob_terminator: 1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +5\n\n"
        "insert_job: b1_34s\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b34s\n\n"
        "insert_job: nx34s\njob_type: c\ncommand: n\nmachine: m1\nbox_name: p34s\n"
        "condition: n(b1_34s, 00.01) & v(GO34S) = 1\n\n"
        "insert_job: idle34s\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_NOEXEC", 0, job="nx34s"))
    o.feed(ev("STARTJOB", 0, job="p34s"))
    o.feed(ev("STARTJOB", 0, job="b34s"))
    o.feed(ev("STATUS", 1, job="b1_34s", status="SUCCESS"))
    o.feed(ev("SET_GLOBAL", 10, name="GO34S", value="1"))
    assert _status(o, "p34s", "b34s", "nx34s") == ["RUNNING", "SUCCESS", "INACTIVE"]
    o.feed(ev("STARTJOB", 20, job="b34s"))
    assert transitions(o, "b34s")[-1] == "START_REFUSED"
    o.feed(ev("FORCE_STARTJOB", 21, job="b34s"))
    assert transitions(o, "b34s")[-2:] == ["SUCCESS->STARTING", "STARTING->TERMINATED"]
    o.feed(ev("STATUS", 30, job="idle34s", status="SUCCESS"))
    assert _mc_alarm_times(o, "b34s") == []


def test_sem34_must_complete_prior_terminal_history_does_not_satisfy_a_later_tick() -> None:
    """T34 (SEM-34, DL-248): yesterday's run ended SUCCESS. Today's 08:00
    tick is blocked and the job never starts, so the SUCCESS it still shows
    is no completion of the run this tick asked for: the alarm fires."""
    o = oracle(_mc_gated("mch"))
    o.feed(ev("STATUS", -1440, job="mch_gate", status="SUCCESS"))
    o.feed(ev("FORCE_STARTJOB", -1439, job="mch"))
    o.feed(ev("STATUS", -1430, job="mch", status="SUCCESS"))
    o.feed(ev("STATUS", -1, job="mch_gate", status="FAILURE"))
    o.feed(ev("STARTJOB", 0, job="mch"))
    o.feed(ev("STATUS", 30, job="mch_idle", status="SUCCESS"))
    assert o.store.job["mch"].status == "SUCCESS"
    assert _mc_alarm_times(o, "mch") == [T0 + timedelta(minutes=8)]


def test_sem34_must_complete_held_at_the_tick_is_judged_like_any_late_start() -> None:
    """T34 x SEM-21 (DL-248): a held job's tick latches (Q3) and arms the
    deadline. Released at 08:02 and complete at 08:05, it is quiet;
    released at 08:10, it alarms at 08:08."""
    text = (
        "insert_job: mco\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +8\n\n"
        "insert_job: mco_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    for released, finished, expected in ((2, 5, []), (10, 15, [T0 + timedelta(minutes=8)])):
        o = oracle(text)
        o.feed(ev("ON_HOLD", 0, job="mco"))
        o.feed(ev("STARTJOB", 0, job="mco"))
        o.feed(ev("OFF_HOLD", released, job="mco"))
        assert o.store.job["mco"].status == "RUNNING"
        o.feed(ev("STATUS", finished, job="mco", status="SUCCESS"))
        o.feed(ev("STATUS", 30, job="mco_idle", status="SUCCESS"))
        assert _mc_alarm_times(o, "mco") == expected


def test_sem34_must_complete_iced_or_box_not_running_at_the_tick_still_alarms() -> None:
    """T34 x SEM-20/SEM-10 (DL-248): the deadline belongs to the tick, as
    must_start's does, so a tick that cannot start the job still arms it.
    An iced job and a member whose box is not RUNNING both alarm at 08:08."""
    text = (
        "insert_job: mci\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +8\n\n"
        "insert_job: mcx_box\njob_type: b\n\n"
        "insert_job: mcx\njob_type: c\ncommand: x\nmachine: m1\nbox_name: mcx_box\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_complete_times: +8\n\n"
        "insert_job: mci_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="mci"))
    o.feed(ev("STARTJOB", 0, job="mci"))
    o.feed(ev("STARTJOB", 0, job="mcx"))
    assert transitions(o, "mcx") == ["START_REFUSED"]
    o.feed(ev("STATUS", 30, job="mci_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mci") == [T0 + timedelta(minutes=8)]
    assert _mc_alarm_times(o, "mcx") == [T0 + timedelta(minutes=8)]


def test_sem34_must_complete_force_start_is_no_tick_and_arms_no_deadline() -> None:
    """T34 x SEM-23 (DL-248): a FORCE_STARTJOB names no slot, so it arms no
    relative completion deadline; a run left RUNNING alarms never. A forced
    run is still a run: begun after a blocked tick and complete in time, it
    satisfies that tick's deadline."""
    o = oracle(_mc_gated("mcf"))
    o.feed(ev("FORCE_STARTJOB", 0, job="mcf"))
    assert _timers(o, "mcf") == []
    o.feed(ev("STATUS", 30, job="mcf_idle", status="SUCCESS"))
    assert _mc_alarm_times(o, "mcf") == []
    assert o.store.job["mcf"].status == "RUNNING"

    satisfied = oracle(_mc_gated("mcf"))
    satisfied.feed(ev("STARTJOB", 0, job="mcf"))
    satisfied.feed(ev("FORCE_STARTJOB", 2, job="mcf"))
    satisfied.feed(ev("STATUS", 5, job="mcf", status="SUCCESS"))
    satisfied.feed(ev("STATUS", 30, job="mcf_idle", status="SUCCESS"))
    assert _mc_alarm_times(satisfied, "mcf") == []


def test_sem34_must_complete_deadline_of_a_never_started_job_is_pending() -> None:
    """DL-248 x DL-46: pending_timers mirrors the fire rule. A blocked tick's
    completion deadline is live before any run begins, and stays live while
    the late run is RUNNING; the completion retires it."""
    o = oracle(_mc_gated("mcp"))
    o.feed(ev("STARTJOB", 0, job="mcp"))
    deadline = (T0 + timedelta(minutes=8), "mcp", "must_complete")
    assert _timers(o, "mcp") == [deadline]
    o.feed(ev("STATUS", 2, job="mcp_gate", status="SUCCESS"))
    assert _timers(o, "mcp") == [deadline]
    o.feed(ev("STATUS", 4, job="mcp", status="SUCCESS"))
    assert _timers(o, "mcp") == []


# DL-253: absolute must times are armed. TechDocs 24.2, must_start_times and
# must_complete_times: absolute times in 24-hour format, "Limits: 00:00-71:59
# (2 calendar days ahead of the current calendar day)", paired by position
# with start_times. "How Must Start Times and Must Complete Times Work": the
# CHK_START and CHK_COMPLETE events for the next must times are inserted with
# the job, and the next ones only "After the job completes".


def _abs_jil(
    name: str,
    starts: str,
    *,
    must_start: str | None = None,
    must_complete: str | None = None,
    zone: str | None = None,
    gated: bool = True,
) -> str:
    """One job with absolute must times, an optional gate that keeps it from
    starting, and an idle job whose STATUS moves the clock."""
    lines = [
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n",
        f'date_conditions: 1\ndays_of_week: all\nstart_times: "{starts}"\n',
    ]
    if must_start is not None:
        lines.append(f'must_start_times: "{must_start}"\n')
    if must_complete is not None:
        lines.append(f'must_complete_times: "{must_complete}"\n')
    if zone is not None:
        lines.append(f"timezone: {zone}\n")
    if gated:
        lines.append(f"condition: s({name}_gate)\n\n")
        lines.append(f"insert_job: {name}_gate\njob_type: c\ncommand: y\nmachine: m1\n")
    lines.append(f"\ninsert_job: {name}_idle\njob_type: c\ncommand: z\nmachine: m1\n")
    return "".join(lines)


def _alarm_times(o: Oracle | EngineHarness, job: str, kind: str) -> list[datetime]:
    return [t.at for t in o.trace() if t.job == job and t.transition == kind]


_VENDOR_STARTS = "10:00, 11:00, 12:00"
_DAY = datetime(2026, 7, 1)


def test_sem34_absolute_vendor_example_runs_on_time_are_quiet() -> None:
    """SEM-34, DL-253: the vendor's example. A job runs at 10:00, 11:00 and
    12:00; it "must start by 10:02 a.m., 11:02 a.m., and 12:02 p.m." and must
    complete by 10:08, 11:08 and 12:08. Each run starts at its tick and ends
    at five past: no alarm. After the 10:00 tick the 10:08 deadline is
    pending; the 10:02 one is already met by the run."""
    o = oracle(
        _abs_jil(
            "va",
            _VENDOR_STARTS,
            must_start="10:02, 11:02, 12:02",
            must_complete="10:08, 11:08, 12:08",
            gated=False,
        )
    )
    for hour in (10, 11, 12):
        o.feed(ev_at(_DAY.replace(hour=hour), "STARTJOB", job="va"))
        if hour == 10:
            assert _timers(o, "va") == [(_DAY.replace(hour=10, minute=8), "va", "must_complete")]
        o.feed(ev_at(_DAY.replace(hour=hour, minute=5), "STATUS", job="va", status="SUCCESS"))
    o.feed(ev_at(_DAY.replace(hour=13), "STATUS", job="va_idle", status="SUCCESS"))
    assert _alarm_times(o, "va", "MUST_START_ALARM") == []
    assert _alarm_times(o, "va", "MUST_COMPLETE_ALARM") == []


def test_sem34_absolute_vendor_example_alarms_for_each_missed_slot() -> None:
    """SEM-34, DL-253: the same job, gated so it never starts. "Otherwise,
    an alarm is issued for each missed start time." Each deadline fires
    before the next tick, so each tick arms its own slot's pair."""
    o = oracle(
        _abs_jil(
            "vm",
            _VENDOR_STARTS,
            must_start="10:02, 11:02, 12:02",
            must_complete="10:08, 11:08, 12:08",
        )
    )
    for hour in (10, 11, 12):
        o.feed(ev_at(_DAY.replace(hour=hour), "STARTJOB", job="vm"))
    o.feed(ev_at(_DAY.replace(hour=13), "STATUS", job="vm_idle", status="SUCCESS"))
    assert _alarm_times(o, "vm", "MUST_START_ALARM") == [
        _DAY.replace(hour=h, minute=2) for h in (10, 11, 12)
    ]
    assert _alarm_times(o, "vm", "MUST_COMPLETE_ALARM") == [
        _DAY.replace(hour=h, minute=8) for h in (10, 11, 12)
    ]
    assert o.store.job["vm"].status == "INACTIVE"  # alarms only, no control flow


@pytest.mark.parametrize(
    "start,must,due",
    [
        ("11:00", "34:00", datetime(2026, 7, 2, 10, 0)),
        ("23:00", "71:59", datetime(2026, 7, 3, 23, 59)),
    ],
    ids=["next-day-34-00", "two-days-71-59"],
)
def test_sem34_absolute_hours_past_23_land_on_the_days_after(
    start: str, must: str, due: datetime
) -> None:
    """SEM-34, DL-253: "suppose that you define a job that starts at 11:00
    a.m and you want to specify a must start time of 10:00 a.m. the next
    day ... you must specify the must start time as 34:00". A job that never
    starts is quiet one minute before and alarms at that instant. 71:59 is
    the last minute of the second day after."""
    tick = _DAY.replace(hour=int(start[:2]))
    o = oracle(_abs_jil("nd", start, must_start=must))
    o.feed(ev_at(tick, "STARTJOB", job="nd"))
    assert _timers(o, "nd") == [(due, "nd", "must_start")]
    o.feed(ev_at(due - timedelta(minutes=1), "STATUS", job="nd_idle", status="SUCCESS"))
    assert _alarm_times(o, "nd", "MUST_START_ALARM") == []
    o.feed(ev_at(due + timedelta(minutes=1), "STATUS", job="nd_idle", status="FAILURE"))
    assert _alarm_times(o, "nd", "MUST_START_ALARM") == [due]


def test_sem34_absolute_late_start_after_the_must_start_time_alarms() -> None:
    """SEM-34, DL-253: blocked at the 10:00 tick, started at 10:05, after
    the 10:02 must start time: the alarm fires at 10:02 and the run goes on.
    A start at 10:01 meets the deadline and is quiet."""
    late = oracle(_abs_jil("ls", "10:00", must_start="10:02"))
    late.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="ls"))
    late.feed(ev_at(_DAY.replace(hour=10, minute=5), "STATUS", job="ls_gate", status="SUCCESS"))
    assert _alarm_times(late, "ls", "MUST_START_ALARM") == [_DAY.replace(hour=10, minute=2)]
    assert late.store.job["ls"].status == "RUNNING"

    early = oracle(_abs_jil("ls", "10:00", must_start="10:02"))
    early.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="ls"))
    early.feed(ev_at(_DAY.replace(hour=10, minute=1), "STATUS", job="ls_gate", status="SUCCESS"))
    early.feed(ev_at(_DAY.replace(hour=11), "STATUS", job="ls_idle", status="SUCCESS"))
    assert _alarm_times(early, "ls", "MUST_START_ALARM") == []


def test_sem34_absolute_completion_before_the_must_complete_time_is_quiet() -> None:
    """SEM-34, DL-253: a run that ends at 10:07 meets the 10:08 must complete
    time; one still RUNNING at 10:08 alarms then and keeps running."""
    quiet = oracle(_abs_jil("cq", "10:00", must_complete="10:08", gated=False))
    quiet.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="cq"))
    quiet.feed(ev_at(_DAY.replace(hour=10, minute=7), "STATUS", job="cq", status="FAILURE"))
    quiet.feed(ev_at(_DAY.replace(hour=11), "STATUS", job="cq_idle", status="SUCCESS"))
    assert _alarm_times(quiet, "cq", "MUST_COMPLETE_ALARM") == []

    late = oracle(_abs_jil("cq", "10:00", must_complete="10:08", gated=False))
    late.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="cq"))
    late.feed(ev_at(_DAY.replace(hour=10, minute=9), "STATUS", job="cq_idle", status="SUCCESS"))
    assert _alarm_times(late, "cq", "MUST_COMPLETE_ALARM") == [_DAY.replace(hour=10, minute=8)]
    assert late.store.job["cq"].status == "RUNNING"


def test_sem34_absolute_tick_at_no_start_time_arms_nothing() -> None:
    """SEM-34, DL-253: absolute must times pair with start_times by position.
    An operator's STARTJOB at 10:30 is no start time, so it names no slot
    and arms no absolute deadline; the relative form keeps its first-offset
    pin (DL-248)."""
    o = oracle(_abs_jil("un", "10:00", must_start="10:02", must_complete="10:08"))
    o.feed(ev_at(_DAY.replace(hour=10, minute=30), "STARTJOB", job="un"))
    assert _timers(o, "un") == []


@pytest.mark.parametrize("musts", ['"10:02, 10:32"', "+2"], ids=["absolute", "relative"])
def test_sem34_must_start_a_tick_on_a_live_job_arms_nothing(musts: str) -> None:
    """SEM-34, DL-253: one must_start deadline at a time. The vendor inserts
    the next CHK_START only "After the job completes". The 10:00 run lasts
    to 10:40, so the 10:30 tick is refused and arms nothing, and 10:32
    stays quiet. Before DL-253 that tick armed its own deadline and the
    refused start alarmed at 10:32."""
    o = oracle(
        "insert_job: ml\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00, 10:30"\n'
        f"must_start_times: {musts}\n\n"
        "insert_job: ml_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="ml"))
    o.feed(ev_at(_DAY.replace(hour=10, minute=30), "STARTJOB", job="ml"))
    assert "START_REFUSED" in transitions(o, "ml")
    assert _timers(o, "ml") == []
    o.feed(ev_at(_DAY.replace(hour=10, minute=40), "STATUS", job="ml", status="SUCCESS"))
    o.feed(ev_at(_DAY.replace(hour=11), "STATUS", job="ml_idle", status="SUCCESS"))
    assert _alarm_times(o, "ml", "MUST_START_ALARM") == []


def test_sem34_must_start_a_tick_while_a_deadline_is_pending_arms_nothing() -> None:
    """SEM-34, DL-253: ticks at 10:00 and 10:01 are both blocked. The 10:01
    tick finds the 10:05 deadline pending and arms nothing: one alarm, at
    10:05. Before DL-253 each tick armed one and the job alarmed twice. Only
    the relative form reaches this: lowering refuses an absolute must time
    that is not earlier than the next run's start, so an absolute deadline
    fires before the next tick."""
    musts = "+5"
    o = oracle(
        "insert_job: mp\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00, 10:01"\n'
        f"must_start_times: {musts}\ncondition: s(mp_gate)\n\n"
        "insert_job: mp_gate\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: mp_idle\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o.feed(ev_at(_DAY.replace(hour=10), "STARTJOB", job="mp"))
    o.feed(ev_at(_DAY.replace(hour=10, minute=1), "STARTJOB", job="mp"))
    o.feed(ev_at(_DAY.replace(hour=11), "STATUS", job="mp_idle", status="SUCCESS"))
    assert _alarm_times(o, "mp", "MUST_START_ALARM") == [_DAY.replace(hour=10, minute=5)]


# DL-253 x DST, America/New_York. 2026-03-08: 02:00 EST jumps to 03:00 EDT at
# 07:00 UTC. 2026-11-01: 02:00 EDT falls back to 01:00 EST at 06:00 UTC.
_NY = "America/New_York"


def test_sem34_spring_must_times_in_the_missing_hour_move_to_the_next_minute() -> None:
    """SEM-34, DL-253, "Daylight Time Changes": "a job that must start by
    2:05 and must complete by 2:45 generates an alarm if the job does not
    start by 3:00:05 or if it does not complete by 3:00:45". The 01:00 EST
    tick is blocked and the job never starts."""
    o = oracle(_abs_jil("sp", "01:00", must_start="02:05", must_complete="02:45", zone=_NY))
    o.feed(ev_at(datetime(2026, 3, 8, 6, 0), "STARTJOB", job="sp"))
    o.feed(ev_at(datetime(2026, 3, 8, 8, 0), "STATUS", job="sp_idle", status="SUCCESS"))
    assert _alarm_times(o, "sp", "MUST_START_ALARM") == [datetime(2026, 3, 8, 7, 0, 5)]
    assert _alarm_times(o, "sp", "MUST_COMPLETE_ALARM") == [datetime(2026, 3, 8, 7, 0, 45)]


def test_sem34_spring_must_time_before_a_missing_hour_start_moves_to_3_00_59() -> None:
    """SEM-34, DL-253, the special case: "Suppose a job is scheduled to run
    at 2:45 with a must complete time of 3:00 ... the job that is scheduled
    at 2:45 runs at 3:00:45 ... the job generates an alarm if it does not
    complete by 3:00:59." A must time after that minute is unchanged."""
    from zoneinfo import ZoneInfo

    from dsl41.ir import MustTime, Time
    from dsl41.oracle import _must_instant

    tz = ZoneInfo(_NY)
    day = datetime(2026, 3, 8).date()
    start = Time(hour=2, minute=45)
    assert _must_instant(day, start, MustTime(hour=3, minute=0), tz) == datetime(
        2026, 3, 8, 7, 0, 59
    )
    assert _must_instant(day, start, MustTime(hour=2, minute=50), tz) == (
        datetime(2026, 3, 8, 7, 0, 50)
    )
    assert _must_instant(day, start, MustTime(hour=3, minute=10), tz) == datetime(2026, 3, 8, 7, 10)


def test_sem34_spring_missing_hour_start_ticks_late_and_its_deadline_is_the_tick() -> None:
    """SEM-34, DL-253 x runner-design E10: the scheduler ticks a 02:45 start
    at fold=0, 03:45 EDT, which still names the 02:45 slot. Its must
    complete time, 3:00:59 by the vendor's rule, is already past, so it is
    due at the tick: a run that began there has not completed and alarms at
    03:45 EDT."""
    o = oracle(_abs_jil("sg", "02:45", must_complete="03:00", zone=_NY, gated=False))
    tick = datetime(2026, 3, 8, 7, 45)
    o.feed(ev_at(tick, "STARTJOB", job="sg"))
    assert _timers(o, "sg") == [(tick, "sg", "must_complete")]
    o.feed(ev_at(tick + timedelta(minutes=5), "STATUS", job="sg", status="SUCCESS"))
    assert _alarm_times(o, "sg", "MUST_COMPLETE_ALARM") == [tick]


def test_sem34_fall_must_time_in_the_repeated_hour_takes_the_first_pass() -> None:
    """SEM-34, DL-253, "Standard Time Changes": "a job that is scheduled to
    run at midnight and must complete at 1:30 generates an alarm if the job
    has not completed by 1:30 DT, not 1:30 ST". 01:30 EDT is 05:30 UTC."""
    o = oracle(_abs_jil("fa", "00:00", must_complete="01:30", zone=_NY))
    o.feed(ev_at(datetime(2026, 11, 1, 4, 0), "STARTJOB", job="fa"))
    o.feed(ev_at(datetime(2026, 11, 1, 8, 0), "STATUS", job="fa_idle", status="SUCCESS"))
    assert _alarm_times(o, "fa", "MUST_COMPLETE_ALARM") == [datetime(2026, 11, 1, 5, 30)]


def test_sem34_fall_start_and_must_time_in_the_repeated_hour_take_the_second_pass() -> None:
    """SEM-34, DL-253: "When the specified start of the job and either the
    must start or must complete times or both occur during the repeated
    hour, CA Workload Automation raises alarms during the second standard
    time hour." The 01:15 start ticks at fold=0 (E10), 05:15 UTC; its 01:30
    must start time is 01:30 EST, 06:30 UTC."""
    o = oracle(_abs_jil("fb", "01:15", must_start="01:30", zone=_NY))
    o.feed(ev_at(datetime(2026, 11, 1, 5, 15), "STARTJOB", job="fb"))
    o.feed(ev_at(datetime(2026, 11, 1, 8, 0), "STATUS", job="fb_idle", status="SUCCESS"))
    assert _alarm_times(o, "fb", "MUST_START_ALARM") == [datetime(2026, 11, 1, 6, 30)]


# --------------------------------------------------------------------- 20. term_run_time


def test_term_run_time_auto_terminates_and_downstream_terminated_consumer_fires() -> None:
    """dossier ss5: term_run_time is control flow (unlike must_*_times) --
    the oracle auto-TERMINATEs a job once its run exceeds the limit, checked
    lazily as the clock advances; a t() consumer downstream picks it up."""
    text = (
        "insert_job: trt_job\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        "insert_job: trt_consumer\njob_type: c\ncommand: y\nmachine: m1\ncondition: t(trt_job)\n\n"
        "insert_job: dummy_trt\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="trt_job"))
    o.feed(ev("STATUS", 6, job="dummy_trt", status="SUCCESS"))  # past the 5-minute limit
    trt_entries = [t for t in o.trace() if t.job == "trt_job"]
    assert transitions(o, "trt_job") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->TERMINATED",
    ]
    assert "term_run_time" in trt_entries[-1].cause
    assert transitions(o, "trt_consumer") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


def test_term_run_time_no_terminate_when_job_completes_before_the_limit() -> None:
    """dossier ss5: completing before term_run_time elapses means the lazy
    deadline check finds the job no longer RUNNING -> no auto-terminate."""
    text = (
        "insert_job: trt_job2\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        "insert_job: dummy_trt2\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="trt_job2"))
    o.feed(ev("STATUS", 2, job="trt_job2", status="SUCCESS"))
    o.feed(ev("STATUS", 10, job="dummy_trt2", status="SUCCESS"))
    assert transitions(o, "trt_job2") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]


def test_term_run_time_zero_runs_to_scripted_completion_with_no_pending_timer() -> None:
    """term_run_time: 0 is the vendor default, "run forever" (DL-241) -- it
    arms no timer, so the job runs to its scripted completion with no
    TERMINATED anywhere in its trace and no pending timer for it at any
    point. test_term_run_time_auto_terminates_and_downstream_terminated_consumer_fires
    is the positive-limit control."""
    text = (
        "insert_job: trt_job0\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 0\n\n"
        "insert_job: dummy_trt0\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="trt_job0"))
    assert "trt_job0" not in pending_timer_jobs(o)
    o.feed(ev("STATUS", 10, job="dummy_trt0", status="SUCCESS"))
    assert "trt_job0" not in pending_timer_jobs(o)
    o.feed(ev("STATUS", 20, job="trt_job0", status="SUCCESS"))
    assert transitions(o, "trt_job0") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    assert "trt_job0" not in pending_timer_jobs(o)


# --------------------------------------------------------- 21. determinism + cascade order


def test_determinism_same_script_twice_yields_identical_traces() -> None:
    """ir-design ss7: the oracle is deterministic -- feeding the same script
    to two fresh oracles over the same catalog produces byte-identical
    traces."""
    text = (
        "insert_job: det_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: det_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(det_a)\n\n"
        "insert_job: det_c\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(det_a) | f(det_a)\n"
    )
    script = [
        ev("STATUS", 0, job="det_a", status="SUCCESS"),
        ev("SET_GLOBAL", 1, name="X", value="1"),
        ev("STATUS", 2, job="det_b", status="FAILURE"),
    ]
    trace1 = oracle(text).run_script(script)
    trace2 = oracle(text).run_script(script)
    assert [t.model_dump() for t in trace1] == [t.model_dump() for t in trace2]


def test_cascade_order_two_consumers_of_one_producer_start_in_catalog_order() -> None:
    """ir-design ss7: same-timestamp cascades are ordered deterministically
    by catalog order (insertion sequence as the tie-break). Both consumers
    fire at the same instant; the one declared first in the JIL begins
    starting first."""
    text = (
        "insert_job: prod_casc\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consumer1_casc\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_casc)\n\n"
        "insert_job: consumer2_casc\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(prod_casc)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="prod_casc", status="SUCCESS"))
    starts = [t.job for t in o.trace() if t.transition == "INACTIVE->STARTING"]
    assert starts == ["consumer1_casc", "consumer2_casc"]


# --------------------------------------------------------------------------- 22. errors


def test_error_feed_time_going_backwards_raises() -> None:
    """Refused by whichever layer meets it first, and the engine meets it
    earlier: the ss4 admission frontier stamps monotonically, so a backwards
    input is refused before anything is appended, where the oracle refuses it
    at apply. Both say "backwards"; only the type differs, and asserting one
    of them here would pin the wrong thing about the other path."""
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("STATUS", 5, job="solo", status="SUCCESS"))
    with pytest.raises((OracleError, EngineError), match="backwards"):
        o.feed(ev("STATUS", 0, job="solo", status="SUCCESS"))


def test_error_status_without_job_raises() -> None:
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    with pytest.raises(OracleError, match="requires payload.job"):
        o.feed(Event(at=T0, kind="STATUS", payload={"status": "SUCCESS"}))


def test_error_set_global_without_name_raises() -> None:
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    with pytest.raises(OracleError, match="SET_GLOBAL requires payload.name"):
        o.feed(Event(at=T0, kind="SET_GLOBAL", payload={"value": "x"}))


def test_error_uninjectable_event_kind_raises() -> None:
    """MUST_START_ALARM is an oracle-emitted event kind (dossier), not an
    injectable one -- feeding it directly is refused."""
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    with pytest.raises(OracleError, match="uninjectable"):
        o.feed(Event(at=T0, kind="MUST_START_ALARM", payload={}))


# ------------------------------------------------------------------- 23. not covered


@pytest.mark.skip(
    reason=(
        "T03/SEM-03 operator precedence (Q1 resolved: DL-53) is pinned entirely"
        " at parse time by condition.lark (flat left-to-right); the oracle only"
        " ever sees the already-built Cond tree and has no precedence concept"
        " of its own to trace-test. See test_condition_grammar.py::"
        "test_sem03_flat_left_to_right_precedence_pinned for the pinning test."
    )
)
def test_sem03_precedence_is_not_applicable_at_the_oracle_layer() -> None:
    pass


# ---------------------------------------------------------------- 24. hypothesis (tier c)

_DIAMOND3_JIL = (
    "insert_job: dj_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: dj_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(dj_a)\n\n"
    "insert_job: dj_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(dj_b) | f(dj_a)\n"
)
_DIAMOND3_JOBS = ["dj_a", "dj_b", "dj_c"]

#: Legal (old, new) status edges reachable from THIS generator's vocabulary
#: (STATUS SUCCESS/FAILURE injections + SET_GLOBAL only -- no STARTJOB,
#: KILLJOB, boxes, or term_run_time/must_* in the fixed catalog, so
#: TERMINATED and manual restarts never arise). Derived from oracle.py's
#: actual behavior, not assumed: (INACTIVE, STARTING) and (STARTING,
#: RUNNING) are the only internally-driven transitions reachable here
#: (conditioned boxless jobs are excluded from re-auto-start once terminal,
#: per _reevaluate_all, and this script never sends STARTJOB/FORCE_STARTJOB
#: to manually restart one); injected STATUS is unconditional in
#: _handle_status/_set_status, so ANY current status can be overwritten
#: directly to SUCCESS or FAILURE regardless of what it was. Terminal ->
#: STARTING is legal too: edge-triggered re-evaluation (DL-13) re-runs a
#: completed consumer when its producer re-succeeds.
_LEGAL_EDGES = frozenset(
    {("INACTIVE", "STARTING"), ("STARTING", "RUNNING")}
    | {(old, "STARTING") for old in ("SUCCESS", "FAILURE", "TERMINATED")}
    | {
        (old, new)
        for old in ("INACTIVE", "STARTING", "RUNNING", "SUCCESS", "FAILURE", "TERMINATED")
        for new in ("SUCCESS", "FAILURE")
    }
)


@st.composite
def _random_diamond_script(draw: st.DrawFn) -> list[Event]:
    n = draw(st.integers(min_value=0, max_value=8))
    events: list[Event] = []
    minute = 0.0
    for _ in range(n):
        minute += draw(st.integers(min_value=0, max_value=5))  # monotone, non-decreasing
        if draw(st.booleans()):
            job = draw(st.sampled_from(_DIAMOND3_JOBS))
            status = draw(st.sampled_from(["SUCCESS", "FAILURE"]))
            events.append(ev("STATUS", minute, job=job, status=status))
        else:
            value = draw(st.sampled_from(["go", "stop"]))
            events.append(ev("SET_GLOBAL", minute, name="FLAG", value=value))
    return events


@given(_random_diamond_script())
@settings(
    max_examples=100,
    deadline=None,
    # the sem_path fixture's path flag is constant across examples, and the
    # harnesses each example creates are closed IN the example body below
    # (the try/finally), so no per-example state leaks to fixture teardown
    # -- which is what makes suppressing the guard honest
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_hypothesis_oracle_determinism_legality_and_monotonicity(script: list[Event]) -> None:
    """Tier (c) fuzz (ir-design ss6): random small scripts of STATUS
    SUCCESS/FAILURE + SET_GLOBAL over a fixed 3-job catalog, monotone
    minutes. (a) determinism: two fresh oracles fed the same script produce
    identical traces. (b) every traced transition is one of the edges
    actually reachable in oracle.py given this event vocabulary. (c) traces
    are time-monotone."""
    try:
        trace1 = oracle(_DIAMOND3_JIL).run_script(script)
        trace2 = oracle(_DIAMOND3_JIL).run_script(script)
    finally:
        # per-example cleanup: on the engine param each example registers
        # two live harnesses (parked adapter tasks on the shared loop);
        # a 100-example run -- or a shrink phase -- must not accumulate them
        _close_harnesses()
    assert [t.model_dump() for t in trace1] == [t.model_dump() for t in trace2]

    times = [t.at for t in trace1]
    assert times == sorted(times)

    for entry in trace1:
        if "->" in entry.transition:
            old, new = entry.transition.split("->", 1)
            assert (old, new) in _LEGAL_EDGES, f"illegal edge {entry.transition} ({entry.cause})"


# ---------------------------------------------- 22. review-driven regressions (DL-13)

# Behaviors fixed below; each test pins the corrected reading so it cannot
# regress silently.


def test_completed_consumer_reruns_on_each_fresh_producer_success() -> None:
    """Edge-triggered re-evaluation (DL-13) -- every new
    satisfaction of the condition re-launches a completed consumer (dossier
    ss0 re-evaluates on each relevant event; SEM-01)."""
    text = (
        "insert_job: rr_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: rr_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(rr_a)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="rr_a", status="SUCCESS"))
    o.feed(ev("STATUS", 1, job="rr_b", status="SUCCESS"))
    assert o.store.job["rr_b"].run_number == 1
    o.feed(ev("STATUS", 2, job="rr_a", status="SUCCESS"))  # fresh satisfaction
    assert o.store.job["rr_b"].status == "RUNNING"
    assert o.store.job["rr_b"].run_number == 2
    # but rr_b's OWN completion does not re-trigger rr_b (no self-reference)
    o.feed(ev("STATUS", 3, job="rr_b", status="SUCCESS"))
    assert o.store.job["rr_b"].run_number == 2


def test_unrelated_events_do_not_wake_consumers() -> None:
    """Edge-triggering (DL-13): only changes to referenced entities wake a
    consumer; an unrelated job's transition does not."""
    text = (
        "insert_job: uw_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: uw_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(uw_a)\n\n"
        "insert_job: uw_other\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="uw_a", status="SUCCESS"))
    o.feed(ev("STATUS", 1, job="uw_b", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="uw_other", status="SUCCESS"))  # unrelated
    assert o.store.job["uw_b"].status == "SUCCESS"  # not re-launched
    assert o.store.job["uw_b"].run_number == 1


def test_hung_box_member_with_false_condition_blocks_completion() -> None:
    """SEM-11 literal (DL-13): a member whose condition is
    false when its sibling completes has neither run nor been bypassed, so
    the box stays RUNNING -- the real hung-box pattern. The condition
    becoming true later (external producer) still starts it, and only then
    does the box fold."""
    text = (
        "insert_job: hb_box\njob_type: b\n\n"
        "insert_job: hb_m1\njob_type: c\ncommand: a\nmachine: m1\nbox_name: hb_box\n\n"
        "insert_job: hb_m2\njob_type: c\ncommand: b\nmachine: m1\nbox_name: hb_box\n"
        "condition: s(hb_ext)\n\n"
        "insert_job: hb_ext\njob_type: c\ncommand: c\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="hb_box"))
    o.feed(ev("STATUS", 1, job="hb_m1", status="SUCCESS"))
    assert o.store.job["hb_box"].status == "RUNNING"  # NOT folded: hb_m2 pending
    assert o.store.job["hb_m2"].status == "INACTIVE"
    o.feed(ev("STATUS", 2, job="hb_ext", status="SUCCESS"))  # condition reoccurs
    assert o.store.job["hb_m2"].status == "RUNNING"
    o.feed(ev("STATUS", 3, job="hb_m2", status="SUCCESS"))
    assert o.store.job["hb_box"].status == "SUCCESS"


def test_scheduled_member_waits_for_its_own_tick_l013_double_gate() -> None:
    """SEM-31/L013 (DL-13): a date_conditions member of a
    RUNNING box starts only on its own schedule tick, not with the box."""
    text = (
        "insert_job: dg_box\njob_type: b\n\n"
        "insert_job: dg_member\njob_type: c\ncommand: x\nmachine: m1\nbox_name: dg_box\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "12:00"\n'
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="dg_box"))
    assert o.store.job["dg_member"].status == "INACTIVE"  # double gate holds
    assert o.store.job["dg_box"].status == "RUNNING"  # member pending, no fold
    o.feed(ev("STARTJOB", 5, job="dg_member"))  # its tick, box RUNNING
    assert o.store.job["dg_member"].status == "RUNNING"
    o.feed(ev("STATUS", 6, job="dg_member", status="SUCCESS"))
    assert o.store.job["dg_box"].status == "SUCCESS"


def test_must_start_alarm_fires_when_no_run_began_by_deadline() -> None:
    """SEM-34: must_start_times arms on the STARTJOB tick;
    the alarm fires iff no new run began by tick+offset -- here the tick
    only ARMED the job (false condition, Q3/DL-54), no run began, which is
    exactly the alarm's point -- and never affects control flow."""
    text = (
        "insert_job: ms_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_start_times: +5\ncondition: s(ms_gate)\n\n"
        "insert_job: ms_gate\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: ms_dummy\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ms_job"))  # condition false -> armed, no run (Q3, DL-54)
    emitted = o.feed(ev("STATUS", 10, job="ms_dummy", status="SUCCESS"))
    assert any(e.kind == "MUST_START_ALARM" and e.job() == "ms_job" for e in emitted)
    assert o.store.job["ms_job"].status == "INACTIVE"  # alarm, no control flow


def test_must_start_alarm_quiet_when_the_run_began_in_time() -> None:
    text = (
        "insert_job: ms_ok\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_start_times: +5\n\n"
        "insert_job: ms_dummy2\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ms_ok"))  # starts immediately
    emitted = o.feed(ev("STATUS", 10, job="ms_dummy2", status="SUCCESS"))
    assert all(e.kind != "MUST_START_ALARM" for e in emitted)


def test_ice_on_a_running_job_takes_effect_at_completion() -> None:
    """SEM-20 (DL-254): ON_ICE has "no effect on jobs with a status of
    STARTING or RUNNING". The run's FAILURE is the job's status, so the
    ordinary s() consumer stays INACTIVE. The name records the DL-13
    behavior DL-254 replaced, where the ice took effect at completion and
    the ON_ICE table read s() true; DL-243 cites the name."""
    text = (
        "insert_job: ir_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: ir_c\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(ir_p)\n"
    )
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="ir_p"))
    o.feed(ev("ON_ICE", 1, job="ir_p"))
    assert not o.store.job["ir_p"].on_ice
    assert o.store.job["ir_c"].status == "INACTIVE"
    o.feed(ev("STATUS", 2, job="ir_p", status="FAILURE"))
    assert o.store.job["ir_c"].status == "INACTIVE"


def test_sem15_idle_box_recompute_derives_status_from_member_changes() -> None:
    """SEM-15 [C]: terminal member transitions on a
    non-running box re-derive its status once every member that is not
    INACTIVE is terminal (DL-242) --
    a completed box flips when a member is CHANGE_STATUSed, and a
    never-started box derives a status when its members are forced."""
    text = (
        "insert_job: ib_box\njob_type: b\n\n"
        "insert_job: ib_m1\njob_type: c\ncommand: a\nmachine: m1\nbox_name: ib_box\n\n"
        "insert_job: ib_watch\njob_type: c\ncommand: w\nmachine: m1\ncondition: f(ib_box)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ib_box"))
    o.feed(ev("STATUS", 1, job="ib_m1", status="SUCCESS"))
    assert o.store.job["ib_box"].status == "SUCCESS"
    o.feed(ev("STATUS", 2, job="ib_m1", status="FAILURE"))  # CHANGE_STATUS analog
    assert o.store.job["ib_box"].status == "FAILURE"  # idle recompute (SEM-15)
    assert o.store.job["ib_watch"].status == "RUNNING"  # downstream woke on it


def test_sem13_sticky_terminated_survives_idle_recompute() -> None:
    """SEM-13 stays senior to SEM-15: member changes on a TERMINATED box do
    not re-derive it."""
    text = (
        "insert_job: st_box\njob_type: b\n\n"
        "insert_job: st_m1\njob_type: c\ncommand: a\nmachine: m1\nbox_name: st_box\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="st_box"))
    o.feed(ev("KILLJOB", 1, job="st_box"))
    assert o.store.job["st_box"].status == "TERMINATED"
    o.feed(ev("STATUS", 2, job="st_m1", status="SUCCESS"))
    assert o.store.job["st_box"].status == "TERMINATED"


_IDLE4_JIL = "insert_job: box15\njob_type: b\n\n" + "".join(
    f"insert_job: m15_{i}\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box15\n\n"
    for i in range(4)
)


def test_sem15_idle_box_ignores_inactive_members() -> None:
    """T15 (SEM-15 [V], DL-242), the vendor's worked example: "Any jobs in the
    box with a status of INACTIVE are ignored when the status of the box is
    being re-evaluated." An INACTIVE box with four INACTIVE members: one is
    forced and completes SUCCESS, so the box is SUCCESS."""
    o = oracle(_IDLE4_JIL)
    o.feed(ev("FORCE_STARTJOB", 0, job="m15_0"))
    o.feed(ev("STATUS", 1, job="m15_0", status="SUCCESS"))
    assert _status(o, "box15", "m15_1") == ["SUCCESS", "INACTIVE"]
    [derive] = [t for t in o.trace() if t.job == "box15"]
    assert derive.transition == "INACTIVE->SUCCESS"
    assert derive.cause.startswith("idle-box recompute (SEM-15)")


def test_sem15_all_inactive_members_and_an_injected_inactive_derive_success() -> None:
    """T15 (SEM-15 [V], DL-242): "if the status of the same job is being updated
    to INACTIVE and all the other jobs inside the box are already in
    INACTIVE status, the box status is re-evaluated and returns a SUCCESS
    status as it ignores all the jobs that are in INACTIVE status." """
    o = oracle(_IDLE4_JIL)
    o.feed(ev("STATUS", 0, job="m15_2", status="INACTIVE"))
    assert o.store.job["box15"].status == "SUCCESS"


def test_sem15_failed_box_becomes_success_when_its_failed_member_is_set_inactive() -> None:
    """T15 (SEM-15 [V], DL-242): a FAILURE box whose failed member is set
    INACTIVE re-derives over the remaining members, all SUCCESS."""
    text = (
        "insert_job: box15f\njob_type: b\n\n"
        "insert_job: p15f\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box15f\n\n"
        "insert_job: q15f\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box15f\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box15f"))
    o.feed(ev("STATUS", 1, job="p15f", status="SUCCESS"))
    o.feed(ev("STATUS", 2, job="q15f", status="FAILURE"))
    assert o.store.job["box15f"].status == "FAILURE"
    o.feed(ev("STATUS", 3, job="q15f", status="INACTIVE"))
    assert o.store.job["box15f"].status == "SUCCESS"
    assert o.store.job["box15f"].window_skipped_members == frozenset()  # not a box run


def _box_in_state(o: Oracle | EngineHarness, box_status: str) -> None:
    """Drive the one-member box `box15t` to `box_status`."""
    if box_status == "INACTIVE":
        return
    o.feed(ev("STARTJOB", 0, job="box15t"))
    if box_status == "TERMINATED":
        o.feed(ev("KILLJOB", 1, job="box15t"))
    else:
        o.feed(ev("STATUS", 1, job="m15t", status=box_status))
    assert o.store.job["box15t"].status == box_status


@pytest.mark.parametrize(
    ("box_status", "member_status", "expected"),
    [
        ("SUCCESS", "TERMINATED", "FAILURE"),
        ("SUCCESS", "FAILURE", "FAILURE"),
        ("FAILURE", "INACTIVE", "SUCCESS"),
        ("FAILURE", "SUCCESS", "SUCCESS"),
        ("FAILURE", "FAILURE", "FAILURE"),
        ("INACTIVE", "INACTIVE", "SUCCESS"),
        ("INACTIVE", "SUCCESS", "SUCCESS"),
        ("INACTIVE", "TERMINATED", "FAILURE"),
        ("INACTIVE", "FAILURE", "FAILURE"),
        ("TERMINATED", "SUCCESS", "TERMINATED"),
        ("TERMINATED", "INACTIVE", "TERMINATED"),
        ("TERMINATED", "FAILURE", "TERMINATED"),
    ],
)
def test_sem15_single_member_table_follows_the_vendor_rule(
    box_status: str, member_status: str, expected: str
) -> None:
    """T15 (SEM-15 [V], DL-242), the vendor's single-member table for a box
    that is not running whose member changes status by FORCE_STARTJOB or
    CHANGE_STATUS. TERMINATED stays sticky (SEM-13)."""
    text = (
        "insert_job: box15t\njob_type: b\n\n"
        "insert_job: m15t\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box15t\n"
    )
    o = oracle(text)
    _box_in_state(o, box_status)
    o.feed(ev("STATUS", 5, job="m15t", status=member_status))
    assert o.store.job["box15t"].status == expected


def test_sem18_box_set_inactive_cascades_to_members() -> None:
    """T18 (SEM-18 [V], DL-242): "Using the sendevent command to change the
    state of a box to INACTIVE changes the state of all the jobs it
    contains to INACTIVE." The cascade runs top-down -- the box, then a
    subbox before its own member -- and marks nothing resolved. On the
    engine path the live members plan no KILL (DL-235); their processes run
    on in the shell. The orphan's later exit is pinned in test_effects.py
    (`test_a_box_set_inactive_cascades_without_a_kill_and_its_orphans_exit_is_rejected`)."""
    text = (
        "insert_job: ob18\njob_type: b\n\n"
        "insert_job: m18\njob_type: c\ncommand: x\nmachine: m1\nbox_name: ob18\n\n"
        "insert_job: ib18\njob_type: b\nbox_name: ob18\n\n"
        "insert_job: g18\njob_type: c\ncommand: y\nmachine: m1\nbox_name: ib18\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ob18"))
    assert _status(o, "ob18", "m18", "ib18", "g18") == ["RUNNING"] * 4
    o.feed(ev("STATUS", 1, job="ob18", status="INACTIVE"))
    assert _status(o, "ob18", "m18", "ib18", "g18") == ["INACTIVE"] * 4
    assert o.store.job["ob18"].window_skipped_members == frozenset()
    assert o.store.job["ib18"].window_skipped_members == frozenset()
    cascade = [t for t in o.trace() if t.transition == "RUNNING->INACTIVE"]
    assert [t.job for t in cascade] == ["ob18", "m18", "ib18", "g18"]
    assert cascade[0].cause == "injected STATUS"
    assert all(t.cause.startswith("box 'ob18' set INACTIVE") for t in cascade[1:])
    if isinstance(o, EngineHarness):
        assert all(e.kind != "KILL" for e in o.engine.outbox.effects())
        assert o.engine.live_jobs() == {"m18", "g18"}


@pytest.mark.parametrize("subbox_first", [False, True], ids=["m1-first", "subbox-first"])
def test_sem18_cascade_writes_every_row_before_it_wakes_anything(subbox_first: bool) -> None:
    """T18 (SEM-18, DL-242): the cascade is one batch. Every contained job
    is INACTIVE before any wake runs, so g -- waiting on n(m1) inside the
    subbox -- is refused by its box's RUNNING gate when m1's change wakes
    it. Catalog order decides whether g ran at the box start (n(m1) holds
    before m1 starts); it does not change what the cascade does."""
    m1 = "insert_job: m1_18b\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b18b\n\n"
    sub = (
        "insert_job: s18b\njob_type: b\nbox_name: b18b\n\n"
        "insert_job: g18b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: s18b\n"
        "condition: n(m1_18b)\n\n"
    )
    o = oracle("insert_job: b18b\njob_type: b\n\n" + (sub + m1 if subbox_first else m1 + sub))
    o.feed(ev("STARTJOB", 0, job="b18b"))
    assert _status(o, "m1_18b", "s18b") == ["RUNNING", "RUNNING"]
    runs = o.store.job["g18b"].run_number
    o.feed(ev("STATUS", 1, job="b18b", status="INACTIVE"))
    assert _status(o, "b18b", "m1_18b", "s18b", "g18b") == ["INACTIVE"] * 4
    at_cascade = T0 + timedelta(minutes=1)
    moves = [t.transition for t in o.trace() if t.job == "g18b" and t.at == at_cascade]
    assert "INACTIVE->STARTING" not in moves  # no phantom run
    assert o.store.job["g18b"].run_number == runs


def test_sem18_cascade_ends_the_box_run_so_member_arms_die() -> None:
    """T18 (SEM-18, DL-242) with Q3c: the cascade ends the box run, so an
    unconsumed member arm dies with it, as at a terminal box transition."""
    text = (
        "insert_job: b18a\njob_type: b\n\n"
        "insert_job: arm18a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b18a\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "condition: s(never18a)\n\n"
        "insert_job: never18a\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="b18a"))
    o.feed(ev("STARTJOB", 0, job="arm18a"))
    assert o.store.job["arm18a"].armed
    o.feed(ev("STATUS", 1, job="b18a", status="INACTIVE"))
    assert not o.store.job["arm18a"].armed
    [disarm] = [t for t in o.trace() if t.transition == "SCHED_DISARM"]
    assert disarm.cause == "unconsumed arm dies with box 'b18a' run (Q3c pin, DL-54/58)"


def test_sem18_a_wake_during_the_cascade_restarts_the_whole_subtree_together() -> None:
    """T18 (SEM-18, DL-242): O waits on n(I), where I is O's own subbox.
    The cascade sets O, I and J INACTIVE; I's change then wakes O, which
    starts a new execution with I and J in it. The cascade must not reach
    into that new execution: the three end live together, never mixed."""
    text = (
        "insert_job: o18c\njob_type: b\ncondition: n(i18c)\n\n"
        "insert_job: i18c\njob_type: b\nbox_name: o18c\n\n"
        "insert_job: j18c\njob_type: c\ncommand: x\nmachine: m1\nbox_name: i18c\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="o18c"))
    assert _status(o, "o18c", "i18c", "j18c") == ["RUNNING"] * 3
    o.feed(ev("STATUS", 1, job="o18c", status="INACTIVE"))
    assert _status(o, "o18c", "i18c", "j18c") == ["RUNNING"] * 3
    assert [o.store.job[j].run_number for j in ("o18c", "i18c", "j18c")] == [2, 2, 2]
    assert o.store.job["o18c"].ran_members == frozenset({"i18c"})
    assert o.store.job["i18c"].ran_members == frozenset({"j18c"})


def test_sem18_a_restart_during_the_cascade_still_wakes_the_resource_waiters() -> None:
    """T18 (SEM-18, DL-242) with DL-50: the cascade releases m's lock in
    phase 1. A phase-2 wake restarts b (`n(b)`), and m queues again: its
    depletable FUEL was used up by its first run. m's own notification is
    skipped because its row moved on. The release still owes the waiters
    their wake, so w is admitted. (The restart used to bypass m through
    ON_NOEXEC; DL-254 ignores that event on a RUNNING job or box.)"""
    text = (
        "insert_resource: LOCK18R\nres_type: R\namount: 1\n\n"
        "insert_resource: FUEL18R\nres_type: D\namount: 1\n\n"
        "insert_job: b18r\njob_type: b\ncondition: n(b18r)\n\n"
        "insert_job: m18r\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b18r\n"
        "resources: (LOCK18R, QUANTITY=1) and (FUEL18R, QUANTITY=1)\n\n"
        "insert_job: h18r\njob_type: c\ncommand: y\nmachine: m1\nbox_name: b18r\n\n"
        "insert_job: w18r\njob_type: c\ncommand: z\nmachine: m1\n"
        "resources: (LOCK18R, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="h18r"))
    o.feed(ev("STARTJOB", 0, job="b18r"))
    o.feed(ev("STARTJOB", 1, job="w18r"))
    assert _status(o, "b18r", "m18r", "w18r") == ["RUNNING", "RUNNING", "QUE_WAIT"]
    o.feed(ev("STATUS", 3, job="b18r", status="INACTIVE"))
    assert _status(o, "b18r", "m18r", "w18r") == ["RUNNING", "QUE_WAIT", "RUNNING"]


def test_sem15_recompute_skips_a_parent_the_injection_itself_started() -> None:
    """T15 (SEM-15, DL-242): the parent is re-read after the injected
    transition. Here B waits on n(J), so setting its held member J INACTIVE
    starts B; an idle recompute must not then fold the running box."""
    text = (
        "insert_job: b15s\njob_type: b\ncondition: n(j15s)\n\n"
        "insert_job: j15s\njob_type: c\ncommand: x\nmachine: m1\nbox_name: b15s\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="j15s"))
    o.feed(ev("STATUS", 1, job="j15s", status="INACTIVE"))
    assert _status(o, "b15s", "j15s") == ["RUNNING", "INACTIVE"]


@pytest.mark.parametrize("nested", [True, False], ids=["leaf-in-subbox", "leaf-direct"])
def test_sem12_a_resolved_member_reaches_every_ancestor_override(nested: bool) -> None:
    """T12c (SEM-12, DL-242): "inside" is transitive, and an operator's
    INACTIVE resolves the member, so the moment is a completion moment for
    every running ancestor. OUT's box_success: n(LEAF) fires whether LEAF
    sits in OUT directly or in its subbox IN."""
    parent = "in12r" if nested else "out12r"
    text = (
        "insert_job: out12r\njob_type: b\nbox_success: n(leaf12r)\n\n"
        + ("insert_job: in12r\njob_type: b\nbox_name: out12r\n\n" if nested else "")
        + f"insert_job: leaf12r\njob_type: c\ncommand: x\nmachine: m1\nbox_name: {parent}\n\n"
        + f"insert_job: wait12r\njob_type: c\ncommand: y\nmachine: m1\nbox_name: {parent}\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="out12r"))
    assert _status(o, "leaf12r", "wait12r") == ["RUNNING", "RUNNING"]
    o.feed(ev("STATUS", 1, job="leaf12r", status="INACTIVE"))
    assert o.store.job["out12r"].status == "SUCCESS"
    [fold] = [t for t in o.trace() if t.job == "out12r" and t.transition == "RUNNING->SUCCESS"]
    assert fold.cause == "box_success override met (SEM-12)"


def test_trace_returns_copies_not_aliases() -> None:
    """Mutating a returned TraceEntry must not corrupt the
    oracle's internal trace."""
    o = oracle("insert_job: tc_j\njob_type: c\ncommand: x\nmachine: m1\n")
    o.feed(ev("FORCE_STARTJOB", 0, job="tc_j"))
    first = o.trace()
    first[0].job = "vandalized"
    assert o.trace()[0].job == "tc_j"


# ------------------------------------------------- DL-50 resources / load / QUE_WAIT
#
# Every test here builds through oracle(), so the autouse fixture runs it under
# BOTH the direct Oracle and Engine(VirtualClock, inert FakeAdapter) -- the
# bisimulation gate covers resource admission for free. Statuses are read via
# .store (proxied by the harness); bucket internals are never poked.


def _statuses(o, *jobs: str) -> dict[str, str]:
    return {j: o.store.job[j].status for j in jobs}


def test_dl50_mutex_second_requester_queues_then_admits_on_release() -> None:
    """A QUANTITY=1 shared resource is a mutex: the second requester enters
    QUE_WAIT and is admitted the instant the holder reaches a terminal state."""
    text = (
        "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="mx1"))
    o.feed(ev("STARTJOB", 0, job="mx2"))
    assert _statuses(o, "mx1", "mx2") == {"mx1": "RUNNING", "mx2": "QUE_WAIT"}
    assert transitions(o, "mx2") == ["INACTIVE->QUE_WAIT"]
    o.feed(ev("STATUS", 1, job="mx1", status="SUCCESS"))
    assert _statuses(o, "mx1", "mx2") == {"mx1": "SUCCESS", "mx2": "RUNNING"}
    assert transitions(o, "mx2") == [
        "INACTIVE->QUE_WAIT",
        "QUE_WAIT->STARTING",
        "STARTING->RUNNING",
    ]


def test_dl50_counting_pool_admits_up_to_capacity_then_queues() -> None:
    """A pool of amount=2 admits two concurrent QUANTITY=1 holders; the third
    queues and is admitted when one of the two completes."""
    text = (
        "insert_resource: POOL\nres_type: R\namount: 2\n\n"
        "insert_job: p1\njob_type: c\ncommand: x\nmachine: m1\nresources: (POOL, QUANTITY=1)\n\n"
        "insert_job: p2\njob_type: c\ncommand: x\nmachine: m1\nresources: (POOL, QUANTITY=1)\n\n"
        "insert_job: p3\njob_type: c\ncommand: x\nmachine: m1\nresources: (POOL, QUANTITY=1)\n"
    )
    o = oracle(text)
    for j in ("p1", "p2", "p3"):
        o.feed(ev("STARTJOB", 0, job=j))
    assert _statuses(o, "p1", "p2", "p3") == {"p1": "RUNNING", "p2": "RUNNING", "p3": "QUE_WAIT"}
    o.feed(ev("STATUS", 1, job="p1", status="SUCCESS"))
    assert o.store.job["p3"].status == "RUNNING"


def test_dl50_renewable_default_releases_on_failure() -> None:
    """FREE absent on a renewable resource frees units on ANY completion, so a
    FAILED holder still releases -- a waiter admits (# PENDING: Qr1 default)."""
    text = (
        "insert_resource: RLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: rf1\njob_type: c\ncommand: x\nmachine: m1\nresources: (RLOCK, QUANTITY=1)\n\n"
        "insert_job: rf2\njob_type: c\ncommand: y\nmachine: m1\nresources: (RLOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="rf1"))
    o.feed(ev("STARTJOB", 0, job="rf2"))
    o.feed(ev("STATUS", 1, job="rf1", status="FAILURE"))
    assert o.store.job["rf2"].status == "RUNNING"


def test_dl50_free_y_holds_the_lock_on_failure() -> None:
    """FREE=Y frees only on SUCCESS: a FAILED holder keeps the units, so the
    waiter stays QUE_WAIT (faithful hold-on-failure, not a release)."""
    text = (
        "insert_resource: YLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: fy1\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (YLOCK, QUANTITY=1, FREE=Y)\n\n"
        "insert_job: fy2\njob_type: c\ncommand: y\nmachine: m1\nresources: (YLOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="fy1"))
    o.feed(ev("STARTJOB", 0, job="fy2"))
    o.feed(ev("STATUS", 1, job="fy1", status="FAILURE"))
    assert o.store.job["fy2"].status == "QUE_WAIT"  # held on failure


def test_dl50_free_a_releases_on_failure_unlike_free_y() -> None:
    """FREE=A frees unconditionally: a FAILED holder releases and the waiter
    admits -- the contrast case to FREE=Y above."""
    text = (
        "insert_resource: ALOCK\nres_type: R\namount: 1\n\n"
        "insert_job: fa1\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (ALOCK, QUANTITY=1, FREE=A)\n\n"
        "insert_job: fa2\njob_type: c\ncommand: y\nmachine: m1\nresources: (ALOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="fa1"))
    o.feed(ev("STARTJOB", 0, job="fa2"))
    o.feed(ev("STATUS", 1, job="fa1", status="FAILURE"))
    assert o.store.job["fa2"].status == "RUNNING"


def test_dl50_threshold_resource_is_a_gate_not_a_consumable() -> None:
    """res_type T is a LEVEL gate that never acquires: three QUANTITY=1 jobs
    against an amount=2 threshold ALL run (nothing is consumed), where the same
    shape as renewable would queue the third."""
    text = (
        "insert_resource: THR\nres_type: T\namount: 2\n\n"
        "insert_job: t1\njob_type: c\ncommand: x\nmachine: m1\nresources: (THR, QUANTITY=1)\n\n"
        "insert_job: t2\njob_type: c\ncommand: x\nmachine: m1\nresources: (THR, QUANTITY=1)\n\n"
        "insert_job: t3\njob_type: c\ncommand: x\nmachine: m1\nresources: (THR, QUANTITY=1)\n"
    )
    o = oracle(text)
    for j in ("t1", "t2", "t3"):
        o.feed(ev("STARTJOB", 0, job=j))
    assert _statuses(o, "t1", "t2", "t3") == {"t1": "RUNNING", "t2": "RUNNING", "t3": "RUNNING"}


def test_dl50_machine_load_throttles_by_job_load_vs_max_load() -> None:
    """A machine max_load caps concurrent job_load: two job_load=1 jobs run on a
    max_load=2 machine, the third queues, and admits on a release. Each job
    sets a positive priority, which load queueing needs (DL-247)."""
    text = (
        "insert_machine: box1\ntype: a\nnode_name: box1\nmax_load: 2\n\n"
        "insert_job: ml1\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n\n"
        "insert_job: ml2\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n\n"
        "insert_job: ml3\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\npriority: 1\n"
    )
    o = oracle(text)
    for j in ("ml1", "ml2", "ml3"):
        o.feed(ev("STARTJOB", 0, job=j))
    assert _statuses(o, "ml1", "ml2", "ml3") == {
        "ml1": "RUNNING",
        "ml2": "RUNNING",
        "ml3": "QUE_WAIT",
    }
    o.feed(ev("STATUS", 1, job="ml1", status="SUCCESS"))
    assert o.store.job["ml3"].status == "RUNNING"


def test_dl50_queued_box_member_keeps_the_box_running_until_admitted() -> None:
    """A box member that queues for a resource holds the box in RUNNING (the
    SEM-11 literal fold gate: an un-run member blocks completion). The box folds
    only once the member is admitted, runs, and reaches terminal."""
    text = (
        "insert_resource: BLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: hog\njob_type: c\ncommand: x\nmachine: m1\nresources: (BLOCK, QUANTITY=1)\n\n"
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: mem\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bx\n"
        "resources: (BLOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="hog"))
    o.feed(ev("STARTJOB", 0, job="bx"))
    assert _statuses(o, "bx", "mem") == {"bx": "RUNNING", "mem": "QUE_WAIT"}
    o.feed(ev("STATUS", 1, job="hog", status="SUCCESS"))  # frees BLOCK -> mem admits
    assert o.store.job["mem"].status == "RUNNING"
    assert o.store.job["bx"].status == "RUNNING"  # member RUNNING, box not folded yet
    o.feed(ev("STATUS", 2, job="mem", status="SUCCESS"))
    assert o.store.job["bx"].status == "SUCCESS"  # now folds


def test_dl50_waiters_admit_in_priority_order() -> None:
    """When one slot frees, the higher-priority waiter (lower number, DL-247)
    admits and the lower-priority one stays queued."""
    text = (
        "insert_resource: ONE\nres_type: R\namount: 1\n\n"
        "insert_job: holder\njob_type: c\ncommand: x\nmachine: m1\nresources: (ONE, QUANTITY=1)\n\n"
        "insert_job: w_lo\njob_type: c\ncommand: x\nmachine: m1\npriority: 9\n"
        "resources: (ONE, QUANTITY=1)\n\n"
        "insert_job: w_hi\njob_type: c\ncommand: x\nmachine: m1\npriority: 1\n"
        "resources: (ONE, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="holder"))
    o.feed(ev("STARTJOB", 0, job="w_lo"))  # enqueued first...
    o.feed(ev("STARTJOB", 0, job="w_hi"))  # ...but higher priority
    o.feed(ev("STATUS", 1, job="holder", status="SUCCESS"))  # one slot frees
    assert _statuses(o, "w_hi", "w_lo") == {"w_hi": "RUNNING", "w_lo": "QUE_WAIT"}


def test_dl50_killing_a_holder_releases_its_units() -> None:
    """KILLJOB on a RUNNING holder terminates it, and TERMINATED frees units
    under the default policy, so the waiter admits."""
    text = (
        "insert_resource: KLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: kh\njob_type: c\ncommand: x\nmachine: m1\nresources: (KLOCK, QUANTITY=1)\n\n"
        "insert_job: kw\njob_type: c\ncommand: y\nmachine: m1\nresources: (KLOCK, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="kh"))
    o.feed(ev("STARTJOB", 0, job="kw"))
    o.feed(ev("KILLJOB", 1, job="kh"))
    assert o.store.job["kh"].status == "TERMINATED"
    assert o.store.job["kw"].status == "RUNNING"


def test_dl50_self_retriggering_holder_does_not_leak_its_semaphore() -> None:
    """A resource holder that re-triggers itself
    inside its own completion cascade (the L010 tight-loop) must release run N
    BEFORE run N+1 re-acquires, or a unit is stranded forever. `sl` self-loops
    via `condition: s(sl)`; after it finally FAILs (breaking s(sl)) the pool is
    fully free, so `big` (needs the whole amount=2) MUST run -- it wedges in
    QUE_WAIT under the leak bug."""
    text = (
        "insert_resource: R\nres_type: R\namount: 2\n\n"
        "insert_job: sl\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1)\ncondition: s(sl)\n\n"
        "insert_job: big\njob_type: c\ncommand: b\nmachine: m1\nresources: (R, QUANTITY=2)\n"
    )
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="sl"))  # seed run 1 (s(sl) false at first)
    o.feed(ev("STATUS", 1, job="sl", status="SUCCESS"))  # completes r1, s(sl) -> re-runs r2
    o.feed(ev("STATUS", 2, job="sl", status="FAILURE"))  # r2 fails, s(sl) false -> stops
    o.feed(ev("STARTJOB", 3, job="big"))
    assert o.store.job["big"].status == "RUNNING"  # pool fully freed; no strand


def test_dl50_killing_a_queued_job_removes_it_and_it_never_runs() -> None:
    """KILLJOB on a QUE_WAIT (standalone) job must
    dequeue and TERMINATE it -- not be silently ignored and then admitted on
    the next release, running despite the operator's kill."""
    text = (
        "insert_resource: K\nres_type: R\namount: 1\n\n"
        "insert_job: ka\njob_type: c\ncommand: x\nmachine: m1\nresources: (K, QUANTITY=1)\n\n"
        "insert_job: kb\njob_type: c\ncommand: y\nmachine: m1\nresources: (K, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ka"))
    o.feed(ev("STARTJOB", 0, job="kb"))
    o.feed(ev("KILLJOB", 1, job="kb"))
    assert o.store.job["kb"].status == "TERMINATED"
    o.feed(ev("STATUS", 2, job="ka", status="SUCCESS"))  # frees K
    assert o.store.job["kb"].status == "TERMINATED"  # stayed dead, did NOT run


def test_dl50_icing_a_queued_job_dequeues_it_immediately() -> None:
    """ON_ICE on a QUE_WAIT job settles it to INACTIVE
    now (an iced job never runs), not lingering QUE_WAIT until a later release."""
    text = (
        "insert_resource: I\nres_type: R\namount: 1\n\n"
        "insert_job: ia\njob_type: c\ncommand: x\nmachine: m1\nresources: (I, QUANTITY=1)\n\n"
        "insert_job: ib\njob_type: c\ncommand: y\nmachine: m1\nresources: (I, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ia"))
    o.feed(ev("STARTJOB", 0, job="ib"))
    o.feed(ev("ON_ICE", 1, job="ib"))
    assert o.store.job["ib"].status == "INACTIVE"  # not lingering QUE_WAIT
    o.feed(ev("STATUS", 2, job="ia", status="SUCCESS"))  # frees I
    assert o.store.job["ib"].status == "INACTIVE"  # iced, did NOT run


# ------------------------------------- DL-247 machine-load bypass and priority blocking
#
# A machine with max_load 1 and a running holder that takes its one unit.
# `kind` names the contender: an unset priority, priority 0, priority 1 by
# STARTJOB, and priority 1 by FORCE_STARTJOB.

_FULL_MACHINE = (
    "insert_machine: m247\ntype: a\nnode_name: m247\nmax_load: 1\n\n"
    "insert_resource: L247\nres_type: R\namount: 1\n\n"
    "insert_job: hold247\njob_type: c\ncommand: x\nmachine: m247\n"
    "job_load: 1\npriority: 1\n\n"
    "insert_job: lock247\njob_type: c\ncommand: x\nmachine: m9\n"
    "resources: (L247, QUANTITY=1)\n\n"
)

#: kind -> (priority line, event kind, the contender's status on a full machine)
_CONTENDERS = {
    "unset": ("", "STARTJOB", "RUNNING"),
    "zero": ("priority: 0\n", "STARTJOB", "RUNNING"),
    "positive": ("priority: 1\n", "STARTJOB", "QUE_WAIT"),
    "forced": ("priority: 1\n", "FORCE_STARTJOB", "RUNNING"),
}


def _contender(kind: str, *, locked: bool) -> str:
    prio, _, _ = _CONTENDERS[kind]
    lock = "resources: (L247, QUANTITY=1)\n" if locked else ""
    return (
        _FULL_MACHINE
        + f"insert_job: c247\njob_type: c\ncommand: y\nmachine: m247\njob_load: 1\n{prio}{lock}"
    )


@pytest.mark.parametrize("kind", list(_CONTENDERS))
def test_dl247_machine_load_applies_only_to_a_positive_priority_unforced_start(
    kind: str,
) -> None:
    """The vendor: an unset or zero priority "runs immediately on a machine if
    resource dependencies permit", and the scheduler "ignores any load unit
    values" for it. A forced job "runs even if its load exceeds the machine's
    max_load value". Only the plain start of a priority-1 job queues."""
    _, event, expected = _CONTENDERS[kind]
    o = oracle(_contender(kind, locked=False))
    o.feed(ev("STARTJOB", 0, job="hold247"))
    o.feed(ev(event, 1, job="c247"))
    assert _statuses(o, "hold247", "c247") == {"hold247": "RUNNING", "c247": expected}


@pytest.mark.parametrize("kind", list(_CONTENDERS))
def test_dl247_a_named_resource_still_gates_every_contender(kind: str) -> None:
    """The load bypass leaves named resources alone: "If the job has resource
    dependencies that are not met, it is queued until resources are available
    even when the priority attribute is set to 0". A FORCE start is gated
    too. The machine has room here, so only the lock holds the job."""
    _, event, _ = _CONTENDERS[kind]
    o = oracle(_contender(kind, locked=True))
    o.feed(ev("STARTJOB", 0, job="lock247"))
    o.feed(ev(event, 1, job="c247"))
    assert o.store.job["c247"].status == "QUE_WAIT"
    o.feed(ev("STATUS", 2, job="lock247", status="SUCCESS"))
    assert o.store.job["c247"].status == "RUNNING"


def test_dl247_a_forced_start_still_takes_its_load() -> None:
    """A forced job is over the machine's limit but its load is real: while it
    runs, a priority-1 job still finds the machine full after the first
    holder ends, and starts once the forced job ends."""
    text = _contender("forced", locked=False) + (
        "\ninsert_job: next247\njob_type: c\ncommand: z\nmachine: m247\njob_load: 1\npriority: 1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="hold247"))
    o.feed(ev("FORCE_STARTJOB", 1, job="c247"))
    o.feed(ev("STARTJOB", 2, job="next247"))
    o.feed(ev("STATUS", 3, job="hold247", status="SUCCESS"))
    assert _statuses(o, "c247", "next247") == {"c247": "RUNNING", "next247": "QUE_WAIT"}
    o.feed(ev("STATUS", 4, job="c247", status="SUCCESS"))
    assert o.store.job["next247"].status == "RUNNING"


def test_dl247_unset_and_zero_priority_skip_the_check_but_hold_their_load() -> None:
    """Both vendor sentences hold. The priority page: "the scheduler ignores
    any load unit values ... when the job has a priority value of zero", so
    an unset or zero priority starts on a full machine. The queueing page:
    "even when jobs have a priority of 0, AutoSys Workload Automation tracks
    job loads on each machine so that jobs with non-zero priorities can be
    queued", so the priority-1 job behind them queues until they end."""
    text = (
        "insert_machine: z247\ntype: a\nnode_name: z247\nmax_load: 1\n\n"
        "insert_job: u247\njob_type: c\ncommand: x\nmachine: z247\njob_load: 1\n\n"
        "insert_job: o247\njob_type: c\ncommand: x\nmachine: z247\njob_load: 1\npriority: 0\n\n"
        "insert_job: p247\njob_type: c\ncommand: x\nmachine: z247\njob_load: 1\npriority: 1\n"
    )
    o = oracle(text)
    for job in ("u247", "o247", "p247"):
        o.feed(ev("STARTJOB", 0, job=job))
    assert _statuses(o, "u247", "o247", "p247") == {
        "u247": "RUNNING",
        "o247": "RUNNING",
        "p247": "QUE_WAIT",
    }
    o.feed(ev("STATUS", 1, job="u247", status="SUCCESS"))
    assert o.store.job["p247"].status == "QUE_WAIT"  # o247 still holds the unit
    o.feed(ev("STATUS", 2, job="o247", status="SUCCESS"))
    assert o.store.job["p247"].status == "RUNNING"


@pytest.mark.parametrize(
    ("priority", "expected"),
    [("priority: 3\n", "QUE_WAIT"), ("priority: 0\n", "RUNNING")],
    ids=["positive", "zero"],
)
def test_dl247_a_running_priority_zero_job_counts_against_the_machine(
    priority: str, expected: str
) -> None:
    """A running priority-0 job with job_load 50 on an 80-unit machine leaves
    30 units: a positive-priority 50-unit arrival queues, and a priority-0
    50-unit arrival still starts, over the limit, as a forced one would."""
    text = (
        "insert_machine: c247\ntype: a\nnode_name: c247\nmax_load: 80\n\n"
        "insert_job: zr247\njob_type: c\ncommand: x\nmachine: c247\njob_load: 50\npriority: 0\n\n"
        "insert_job: ar247\njob_type: c\ncommand: y\nmachine: c247\njob_load: 50\n" + priority
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="zr247"))
    o.feed(ev("STARTJOB", 1, job="ar247"))
    assert _statuses(o, "zr247", "ar247") == {"zr247": "RUNNING", "ar247": expected}


#: The vendor's queueing example: machine cheetah, max_load 80.
_CHEETAH = (
    "insert_machine: cheetah\ntype: a\nnode_name: cheetah\nmax_load: 80\n\n"
    "insert_job: JobA\njob_type: c\ncommand: a\nmachine: cheetah\njob_load: 50\npriority: 70\n\n"
    "insert_job: JobB\njob_type: c\ncommand: b\nmachine: cheetah\njob_load: 50\npriority: 50\n\n"
    "insert_job: JobC\njob_type: c\ncommand: c\nmachine: cheetah\njob_load: 30\npriority: 60\n\n"
    "insert_job: JobD\njob_type: c\ncommand: d\nmachine: cheetah\njob_load: 30\npriority: 80\n"
)


def _cheetah_running_b_and_c() -> Oracle | EngineHarness:
    """JobB and JobC run; JobA and JobD wait in QUE_WAIT."""
    o = oracle(_CHEETAH)
    for job in ("JobB", "JobC", "JobA", "JobD"):
        o.feed(ev("STARTJOB", 0, job=job))
    assert _statuses(o, "JobA", "JobB", "JobC", "JobD") == {
        "JobA": "QUE_WAIT",
        "JobB": "RUNNING",
        "JobC": "RUNNING",
        "JobD": "QUE_WAIT",
    }
    return o


def test_dl247_cheetah_jobb_first_runs_joba_then_jobd() -> None:
    """The vendor: "If JobB finishes first, 50 load units become available,
    so JobA runs. After JobA or JobB complete, sufficient load units become
    available, so JobD runs." JobB is already done here, so JobC's end is
    the second completion."""
    o = _cheetah_running_b_and_c()
    o.feed(ev("STATUS", 1, job="JobB", status="SUCCESS"))
    assert _statuses(o, "JobA", "JobD") == {"JobA": "RUNNING", "JobD": "QUE_WAIT"}
    o.feed(ev("STATUS", 2, job="JobC", status="SUCCESS"))
    assert o.store.job["JobD"].status == "RUNNING"


def test_dl247_cheetah_jobc_first_keeps_both_queued_until_jobb_ends() -> None:
    """The vendor: "If JobC finishes first, only 30 load units become
    available, so JobA and JobD remain queued until JobB completes." JobD's
    30 units fit, but JobA waits for load at a higher priority and blocks it.
    "After JobB completes ... Because JobA has a higher priority, it runs
    first. JobD runs shortly after." """
    o = _cheetah_running_b_and_c()
    o.feed(ev("STATUS", 1, job="JobC", status="SUCCESS"))
    assert _statuses(o, "JobA", "JobD") == {"JobA": "QUE_WAIT", "JobD": "QUE_WAIT"}
    o.feed(ev("STATUS", 2, job="JobB", status="SUCCESS"))
    assert _statuses(o, "JobA", "JobD") == {"JobA": "RUNNING", "JobD": "RUNNING"}
    starts = [t.job for t in o.trace() if t.transition == "QUE_WAIT->STARTING"]
    assert starts == ["JobA", "JobD"]


#: A holder takes 50 of m1's 80 units and `hi247` (priority 5, load 50)
#: waits for load. Each arrival names its own priority, load and machine.
_BLOCKING = (
    "insert_machine: bm247\ntype: a\nnode_name: bm247\nmax_load: 80\n\n"
    "insert_machine: om247\ntype: a\nnode_name: om247\nmax_load: 80\n\n"
    "insert_job: own247\njob_type: c\ncommand: x\nmachine: bm247\njob_load: 50\npriority: 1\n\n"
    "insert_job: hi247\njob_type: c\ncommand: x\nmachine: bm247\njob_load: 50\npriority: 5\n\n"
)


def _arrival(priority: str, machine: str = "bm247") -> str:
    return (
        _BLOCKING + "insert_job: new247\njob_type: c\ncommand: y\n"
        f"machine: {machine}\njob_load: 30\n{priority}"
    )


@pytest.mark.parametrize(
    ("priority", "machine", "expected"),
    [
        ("priority: 9\n", "bm247", "QUE_WAIT"),  # lower priority, same machine
        ("priority: 9\n", "om247", "RUNNING"),  # another machine
        ("priority: 5\n", "bm247", "RUNNING"),  # equal priority
        ("priority: 2\n", "bm247", "RUNNING"),  # higher priority
        ("priority: 0\n", "bm247", "RUNNING"),  # takes no load
        ("", "bm247", "RUNNING"),  # unset: takes no load
    ],
    ids=["lower", "other-machine", "equal", "higher", "zero", "unset"],
)
def test_dl247_a_load_waiter_blocks_only_lower_priority_on_its_machine(
    priority: str, machine: str, expected: str
) -> None:
    """The vendor: "A job in the QUE_WAIT state for one machine attribute value
    automatically blocks all the lower priority jobs that specify the same
    machine attribute value. It does not automatically block higher or equal
    priority jobs ... or a job that specifies a different machine attribute
    value." The arrival's 30 units fit beside the holder every time."""
    o = oracle(_arrival(priority, machine))
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("STARTJOB", 2, job="new247"))
    assert _statuses(o, "hi247", "new247") == {"hi247": "QUE_WAIT", "new247": expected}


def test_dl247_a_blocked_arrival_starts_after_the_waiter_ahead_of_it() -> None:
    """The blocked lower-priority arrival queues, and starts once the waiter
    ahead of it has started and room is left: the holder's end admits
    `hi247` (50 of 80), then `new247` (30) fits."""
    o = oracle(_arrival("priority: 9\n"))
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("STARTJOB", 2, job="new247"))
    o.feed(ev("STATUS", 3, job="own247", status="SUCCESS"))
    assert _statuses(o, "hi247", "new247") == {"hi247": "RUNNING", "new247": "RUNNING"}
    starts = [t.job for t in o.trace() if t.transition == "QUE_WAIT->STARTING"]
    assert starts == ["hi247", "new247"]


def test_dl247_a_held_waiter_does_not_block() -> None:
    """A queued job put ON_HOLD is not waiting for load: it does not block a
    lower-priority arrival on its machine."""
    o = oracle(_arrival("priority: 9\n"))
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("ON_HOLD", 2, job="hi247"))
    o.feed(ev("STARTJOB", 3, job="new247"))
    assert _statuses(o, "hi247", "new247") == {"hi247": "QUE_WAIT", "new247": "RUNNING"}


@pytest.mark.parametrize(
    ("priority", "expected"),
    [("priority: 5\n", "RUNNING"), ("priority: 6\n", "QUE_WAIT"), ("", "RUNNING")],
    ids=["equal", "lower", "unset"],
)
def test_dl247_a_positive_priority_without_job_load_is_still_blocked(
    priority: str, expected: str
) -> None:
    """The vendor blocks "all the lower priority jobs that specify the same
    machine attribute value", and priority is itself a load-balancing
    attribute. A job with no job_load on the waiter's machine is blocked at a
    lower priority, not at an equal one, and never with no priority."""
    text = _BLOCKING + (f"insert_job: nl247\njob_type: c\ncommand: y\nmachine: bm247\n{priority}")
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("STARTJOB", 2, job="nl247"))
    assert _statuses(o, "hi247", "nl247") == {"hi247": "QUE_WAIT", "nl247": expected}


def test_dl247_a_blocked_job_without_job_load_starts_once_the_waiter_starts() -> None:
    """The blocked no-load job is admitted in the same scan that starts the
    waiter ahead of it."""
    text = _BLOCKING + "insert_job: nl247\njob_type: c\ncommand: y\nmachine: bm247\npriority: 6\n"
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("STARTJOB", 2, job="nl247"))
    o.feed(ev("STATUS", 3, job="own247", status="SUCCESS"))
    assert _statuses(o, "hi247", "nl247") == {"hi247": "RUNNING", "nl247": "RUNNING"}
    starts = [t.job for t in o.trace() if t.transition == "QUE_WAIT->STARTING"]
    assert starts == ["hi247", "nl247"]


@pytest.mark.parametrize("lift", ["KILLJOB", "ON_ICE", "ON_HOLD"])
def test_dl247_a_block_lifts_when_the_waiter_leaves_the_queue(lift: str) -> None:
    """No capacity is freed, but the blocker leaves the queue or stops
    counting: the blocked arrival, whose 30 units fit, starts at once."""
    o = oracle(_arrival("priority: 9\n"))
    o.feed(ev("STARTJOB", 0, job="own247"))
    o.feed(ev("STARTJOB", 1, job="hi247"))
    o.feed(ev("STARTJOB", 2, job="new247"))
    assert o.store.job["new247"].status == "QUE_WAIT"
    o.feed(ev(lift, 3, job="hi247"))
    assert o.store.job["new247"].status == "RUNNING"


#: A 30-unit holder on bm's 80 units; box member M (priority 5, load 60)
#: waits for load and blocks X (priority 9, load 30), whose load fits.
_BOX_BLOCK = (
    "insert_machine: bx247m\ntype: a\nnode_name: bx247m\nmax_load: 80\n\n"
    "insert_job: bh247\njob_type: c\ncommand: h\nmachine: bx247m\njob_load: 30\npriority: 1\n\n"
    "insert_job: bb247\njob_type: b\n\n"
    "insert_job: bm247m\njob_type: c\ncommand: m\nmachine: bx247m\nbox_name: bb247\n"
    "job_load: 60\npriority: 5\n\n"
    "insert_job: bxx247\njob_type: c\ncommand: x\nmachine: bx247m\njob_load: 30\npriority: 9\n\n"
    "insert_job: byy247\njob_type: c\ncommand: y\nmachine: bx247m\njob_load: 30\npriority: 9\n"
)


def _box_member_blocking() -> Oracle | EngineHarness:
    o = oracle(_BOX_BLOCK)
    o.feed(ev("STARTJOB", 0, job="bh247"))
    o.feed(ev("STARTJOB", 1, job="bb247"))
    o.feed(ev("STARTJOB", 2, job="bxx247"))
    assert _statuses(o, "bm247m", "bxx247") == {"bm247m": "QUE_WAIT", "bxx247": "QUE_WAIT"}
    return o


def test_dl247_a_stopped_box_lifts_its_members_block_at_once() -> None:
    """KILLJOB on the box stops its member from blocking. The queue is
    scanned then, so X starts without waiting for a release; the member
    itself stays queued until a release cancels it (DL-158, DL-54)."""
    o = _box_member_blocking()
    o.feed(ev("KILLJOB", 3, job="bb247"))
    assert _statuses(o, "bb247", "bm247m", "bxx247") == {
        "bb247": "TERMINATED",
        "bm247m": "QUE_WAIT",
        "bxx247": "RUNNING",
    }
    o.feed(ev("STATUS", 4, job="bh247", status="SUCCESS"))
    assert o.store.job["bm247m"].status == "INACTIVE"  # the release cancels it


def test_dl247_a_later_arrival_does_not_overtake_after_a_box_stops() -> None:
    """After the box stops, X starts first; Y, with X's priority and load,
    arrives later and finds the machine full (30 + 30 + 30 > 80)."""
    o = _box_member_blocking()
    o.feed(ev("KILLJOB", 3, job="bb247"))
    o.feed(ev("STARTJOB", 4, job="byy247"))
    assert _statuses(o, "bxx247", "byy247") == {"bxx247": "RUNNING", "byy247": "QUE_WAIT"}


def test_dl247_the_box_stop_scan_waits_for_the_release_and_the_referencers() -> None:
    """DL-50's order is release, condition referencers, waiters. C's end
    folds box B (box_success: s(C)) to SUCCESS, which lifts M's block on X;
    but C's own release and its referencer Y come first. Y (priority 1,
    load 50) takes the room C freed, and X stays queued."""
    text = (
        "insert_machine: ow247\ntype: a\nnode_name: ow247\nmax_load: 80\n\n"
        "insert_job: oh247\njob_type: c\ncommand: h\nmachine: ow247\njob_load: 30\npriority: 1\n\n"
        "insert_job: ob247\njob_type: b\nbox_success: s(oc247)\n\n"
        "insert_job: oc247\njob_type: c\ncommand: c\nmachine: ow247\nbox_name: ob247\n"
        "job_load: 20\npriority: 1\n\n"
        "insert_job: om247\njob_type: c\ncommand: m\nmachine: ow247\nbox_name: ob247\n"
        "job_load: 60\npriority: 5\n\n"
        "insert_job: ox247\njob_type: c\ncommand: x\nmachine: ow247\njob_load: 30\npriority: 9\n\n"
        "insert_job: oy247\njob_type: c\ncommand: y\nmachine: ow247\njob_load: 50\npriority: 1\n"
        "condition: s(oc247)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="oh247"))
    o.feed(ev("STARTJOB", 1, job="ob247"))
    o.feed(ev("STARTJOB", 2, job="ox247"))
    assert _statuses(o, "oc247", "om247", "ox247") == {
        "oc247": "RUNNING",
        "om247": "QUE_WAIT",
        "ox247": "QUE_WAIT",
    }
    o.feed(ev("STATUS", 3, job="oc247", status="SUCCESS"))
    assert _statuses(o, "ob247", "oy247", "ox247") == {
        "ob247": "SUCCESS",
        "oy247": "RUNNING",
        "ox247": "QUE_WAIT",
    }


def test_dl247_a_release_inside_the_box_stop_scan_still_cancels() -> None:
    """The box-stop scan only admits. Here it starts X, a member of box Q;
    X's start fails Q (box_failure: v(GO) = 1), and job_terminator ends X,
    releasing its units. That release asks for a full scan, which the
    running scan takes up as its next pass: M, queued in the stopped box B,
    is cancelled on the release, as DL-158 says a release does."""
    text = (
        "insert_machine: up247\ntype: a\nnode_name: up247\nmax_load: 80\n\n"
        "insert_job: uh247\njob_type: c\ncommand: h\nmachine: up247\njob_load: 30\npriority: 1\n\n"
        "insert_job: ub247\njob_type: b\n\n"
        "insert_job: um247\njob_type: c\ncommand: m\nmachine: up247\nbox_name: ub247\n"
        "job_load: 60\npriority: 5\n\n"
        "insert_job: uq247\njob_type: b\nbox_failure: s(ux247) | v(GO247) = 1\n\n"
        "insert_job: ux247\njob_type: c\ncommand: x\nmachine: up247\nbox_name: uq247\n"
        "job_load: 30\npriority: 9\njob_terminator: 1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="uh247"))
    o.feed(ev("STARTJOB", 1, job="ub247"))
    o.feed(ev("STARTJOB", 2, job="uq247"))
    assert _statuses(o, "um247", "ux247") == {"um247": "QUE_WAIT", "ux247": "QUE_WAIT"}
    o.feed(ev("SET_GLOBAL", 3, name="GO247", value="1"))
    assert o.store.job["uq247"].status == "RUNNING"
    o.feed(ev("KILLJOB", 4, job="ub247"))
    assert _statuses(o, "uq247", "ux247", "um247") == {
        "uq247": "FAILURE",
        "ux247": "TERMINATED",
        "um247": "INACTIVE",
    }


def test_dl247_a_waiter_short_only_on_a_named_resource_does_not_block() -> None:
    """A queued job whose load fits waits on a named resource, and the vendor
    says such a job does "not automatically block lower priority jobs that
    specify the same machine attribute value and ... do not specify the
    resource attribute"."""
    text = (
        "insert_machine: rm247\ntype: a\nnode_name: rm247\nmax_load: 80\n\n"
        "insert_resource: R247\nres_type: R\namount: 1\n\n"
        "insert_job: rh247\njob_type: c\ncommand: x\nmachine: m9\nresources: (R247, QUANTITY=1)\n\n"
        "insert_job: rw247\njob_type: c\ncommand: x\nmachine: rm247\njob_load: 10\npriority: 1\n"
        "resources: (R247, QUANTITY=1)\n\n"
        "insert_job: rn247\njob_type: c\ncommand: y\nmachine: rm247\njob_load: 10\npriority: 9\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="rh247"))
    o.feed(ev("STARTJOB", 1, job="rw247"))
    o.feed(ev("STARTJOB", 2, job="rn247"))
    assert _statuses(o, "rw247", "rn247") == {"rw247": "QUE_WAIT", "rn247": "RUNNING"}


# ------------------------------------------------- DL-255 named-resource priority blocking
#
# R255 has 3 units and a holder with no priority takes 2. `hi255`
# (priority 5) wants 2 and waits on R255. Each arrival names its own
# priority and resource; its 1 unit fits every time.

_RES_BLOCK = (
    "insert_resource: R255\nres_type: R\namount: 3\n\n"
    "insert_resource: S255\nres_type: R\namount: 3\n\n"
    "insert_job: rh255\njob_type: c\ncommand: h\nmachine: m9\nresources: (R255, QUANTITY=2)\n\n"
    "insert_job: hi255\njob_type: c\ncommand: w\nmachine: m9\npriority: 5\n"
    "resources: (R255, QUANTITY=2)\n\n"
)


def _res_arrival(priority: str, resource: str = "R255") -> str:
    return (
        _RES_BLOCK + "insert_job: new255\njob_type: c\ncommand: y\nmachine: m9\n"
        f"{priority}resources: ({resource}, QUANTITY=1)\n"
    )


def _res_blocked(text: str, event: str = "STARTJOB") -> Oracle | EngineHarness:
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="rh255"))
    o.feed(ev("STARTJOB", 1, job="hi255"))
    o.feed(ev(event, 2, job="new255"))
    return o


@pytest.mark.parametrize(
    ("priority", "resource", "event", "expected"),
    [
        ("priority: 9\n", "R255", "STARTJOB", "QUE_WAIT"),
        ("priority: 9\n", "R255", "FORCE_STARTJOB", "QUE_WAIT"),
        ("priority: 9\n", "S255", "STARTJOB", "RUNNING"),
        ("priority: 5\n", "R255", "STARTJOB", "RUNNING"),
        ("priority: 2\n", "R255", "STARTJOB", "RUNNING"),
        ("priority: 0\n", "R255", "STARTJOB", "RUNNING"),
        ("", "R255", "STARTJOB", "RUNNING"),
    ],
    ids=["lower", "lower-forced", "other-resource", "equal", "higher", "zero", "unset"],
)
def test_dl255_a_resource_waiter_blocks_only_lower_priority_naming_it(
    priority: str, resource: str, event: str, expected: str
) -> None:
    """The vendor: "A job in the RESWAIT state for one resource name
    automatically blocks all the lower priority jobs that specify the same
    resource name. It does not automatically block higher or equal priority
    jobs that specify the same resource name or a job that specifies a
    different resource name." An unset or zero priority "is not queued
    behind other jobs". Force skips the load check only, so a forced lower
    priority is blocked too."""
    o = _res_blocked(_res_arrival(priority, resource), event)
    assert _statuses(o, "hi255", "new255") == {"hi255": "QUE_WAIT", "new255": expected}


def test_dl255_a_blocked_arrival_starts_after_the_waiter_ahead_of_it() -> None:
    """The holder's end frees 2 units: `hi255` takes them, then the blocked
    arrival's 1 unit fits, in the same scan."""
    o = _res_blocked(_res_arrival("priority: 9\n"))
    o.feed(ev("STATUS", 3, job="rh255", status="SUCCESS"))
    assert _statuses(o, "hi255", "new255") == {"hi255": "RUNNING", "new255": "RUNNING"}
    starts = [t.job for t in o.trace() if t.transition == "QUE_WAIT->STARTING"]
    assert starts == ["hi255", "new255"]


@pytest.mark.parametrize("lift", ["KILLJOB", "ON_ICE", "ON_HOLD"])
def test_dl255_a_block_lifts_when_the_waiter_leaves_the_queue(lift: str) -> None:
    """No unit is freed, but the blocker leaves the queue or stops counting:
    the blocked arrival, whose unit fits, starts at once."""
    o = _res_blocked(_res_arrival("priority: 9\n"))
    assert o.store.job["new255"].status == "QUE_WAIT"
    o.feed(ev(lift, 3, job="hi255"))
    assert o.store.job["new255"].status == "RUNNING"


def test_dl255_a_waiter_short_on_one_resource_blocks_on_every_resource_it_names() -> None:
    """AutoSys KB 240816 ("AutoSys jobs/resources issue: job stuck in
    RESWAIT", AutoSys 12.0): a higher-priority job needing two resources and
    waiting for the second blocked lower-priority jobs needing only the
    first, which was free, because "A job in the RESWAIT state for one
    resource name automatically blocks all the lower priority jobs that
    specify the same resource name". `hi255` wants 2 units of R255 (1 free)
    and 1 of S255 (free); `new255` (priority 9) wants 1 of S255 and queues.
    It starts once `hi255` is admitted."""
    text = _RES_BLOCK.replace(
        "priority: 5\nresources: (R255, QUANTITY=2)",
        "priority: 5\nresources: (R255, QUANTITY=2) AND (S255, QUANTITY=1)",
    ) + (
        "insert_job: new255\njob_type: c\ncommand: y\nmachine: m9\npriority: 9\n"
        "resources: (S255, QUANTITY=1)\n"
    )
    o = _res_blocked(text)
    assert _statuses(o, "hi255", "new255") == {"hi255": "QUE_WAIT", "new255": "QUE_WAIT"}
    o.feed(ev("STATUS", 3, job="rh255", status="SUCCESS"))
    assert _statuses(o, "hi255", "new255") == {"hi255": "RUNNING", "new255": "RUNNING"}
    starts = [t.job for t in o.trace() if t.transition == "QUE_WAIT->STARTING"]
    assert starts == ["hi255", "new255"]


def test_dl255_a_stopped_box_lifts_a_resource_only_block_at_once() -> None:
    """A box leaving RUNNING owes the admit-only scan when a queued job has a
    positive priority on a sized resource, not only on a sized machine. Box
    member `bhi255` (priority 1) waits for 2 units of R255 and blocks
    `blo255` (priority 9, 1 unit). KILLJOB on the box lifts the block, so
    `blo255` starts at once; the member stays queued for a release to
    cancel (DL-158, DL-54)."""
    text = (
        "insert_resource: R255\nres_type: R\namount: 3\n\n"
        "insert_job: bh255\njob_type: c\ncommand: h\nmachine: m9\nresources: (R255, QUANTITY=2)\n\n"
        "insert_job: bx255\njob_type: b\n\n"
        "insert_job: bhi255\njob_type: c\ncommand: m\nmachine: m9\nbox_name: bx255\n"
        "priority: 1\nresources: (R255, QUANTITY=2)\n\n"
        "insert_job: blo255\njob_type: c\ncommand: l\nmachine: m9\npriority: 9\n"
        "resources: (R255, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="bh255"))
    o.feed(ev("STARTJOB", 1, job="bx255"))
    o.feed(ev("STARTJOB", 2, job="blo255"))
    assert _statuses(o, "bhi255", "blo255") == {"bhi255": "QUE_WAIT", "blo255": "QUE_WAIT"}
    o.feed(ev("KILLJOB", 3, job="bx255"))
    assert _statuses(o, "bx255", "bhi255", "blo255") == {
        "bx255": "TERMINATED",
        "bhi255": "QUE_WAIT",
        "blo255": "RUNNING",
    }


#: Machine rl255 has 80 units and a 30-unit holder; R255 has 2 units, one
#: held from another machine. `rw255` (priority 5) wants both and loads
#: rl255; `rn255` (priority 9) wants one, which fits.
_RES_LOAD = (
    "insert_machine: rl255\ntype: a\nnode_name: rl255\nmax_load: 80\n\n"
    "insert_resource: R255\nres_type: R\namount: 2\n\n"
    "insert_job: lh255\njob_type: c\ncommand: h\nmachine: rl255\njob_load: 30\npriority: 1\n\n"
    "insert_job: rh255\njob_type: c\ncommand: h\nmachine: m9\nresources: (R255, QUANTITY=1)\n\n"
    "insert_job: rn255\njob_type: c\ncommand: y\nmachine: m9\npriority: 9\n"
    "resources: (R255, QUANTITY=1)\n\n"
)


def _rw255(load: int) -> str:
    return (
        "insert_job: rw255\njob_type: c\ncommand: w\nmachine: rl255\n"
        f"job_load: {load}\npriority: 5\nresources: (R255, QUANTITY=2)\n\n"
    )


def test_dl255_a_load_fitting_resource_waiter_holds_no_load() -> None:
    """The vendor: jobs "that enter the RESWAIT state after the load
    balancing attributes are successfully evaluated do not consume any load
    units. These jobs do not automatically block lower priority jobs that
    specify the same machine attribute value and either do not specify the
    resource attribute or specify a different resource attribute value."
    `rw255`'s 40 units fit beside the holder's 30 but are not held, so a
    lower priority's 40 units fit too."""
    text = (
        _RES_LOAD
        + _rw255(40)
        + (
            "insert_job: rx255\njob_type: c\ncommand: x\nmachine: rl255\njob_load: 40\npriority: 9\n"
        )
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="lh255"))
    o.feed(ev("STARTJOB", 0, job="rh255"))
    o.feed(ev("STARTJOB", 1, job="rw255"))
    assert o.store.job["rw255"].status == "QUE_WAIT"
    assert o.store.job["rw255"].reservations == ()
    o.feed(ev("STARTJOB", 2, job="rx255"))
    assert _statuses(o, "rw255", "rx255") == {"rw255": "QUE_WAIT", "rx255": "RUNNING"}
    o.feed(ev("STATUS", 3, job="rh255", status="SUCCESS"))
    assert o.store.job["rw255"].status == "QUE_WAIT"  # 30 + 40 + 40 > 80 now
    o.feed(ev("STATUS", 4, job="rx255", status="SUCCESS"))
    assert o.store.job["rw255"].status == "RUNNING"


def test_dl255_a_waiter_still_short_on_load_does_not_block_on_its_resource() -> None:
    """The vendor: resources are evaluated "after the load balancing
    attributes are evaluated and the machine has available load units", and
    jobs that "enter the QUE_WAIT state ... do not automatically block lower
    priority jobs that specify the same resource attribute and a different
    machine attribute value". `rw255`'s 60 units do not fit beside 30, so it
    does not block `rn255` on R255."""
    o = oracle(_RES_LOAD + _rw255(60))
    o.feed(ev("STARTJOB", 0, job="lh255"))
    o.feed(ev("STARTJOB", 0, job="rh255"))
    o.feed(ev("STARTJOB", 1, job="rw255"))
    o.feed(ev("STARTJOB", 2, job="rn255"))
    assert _statuses(o, "rw255", "rn255") == {"rw255": "QUE_WAIT", "rn255": "RUNNING"}


def test_dl255_a_load_blocked_waiter_does_not_block_on_its_resource() -> None:
    """`rw255`'s 10 units fit, but `lw255` (priority 2, 60 units) waits for
    load ahead of it on rl255, so `rw255` is still at its load check and
    does not block `rn255` on R255."""
    text = (
        _RES_LOAD
        + _rw255(10)
        + (
            "insert_job: lw255\njob_type: c\ncommand: l\nmachine: rl255\njob_load: 60\npriority: 2\n"
        )
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="lh255"))
    o.feed(ev("STARTJOB", 0, job="lw255"))
    o.feed(ev("STARTJOB", 0, job="rh255"))
    o.feed(ev("STARTJOB", 1, job="rw255"))
    o.feed(ev("STARTJOB", 2, job="rn255"))
    assert _statuses(o, "lw255", "rw255", "rn255") == {
        "lw255": "QUE_WAIT",
        "rw255": "QUE_WAIT",
        "rn255": "RUNNING",
    }


@pytest.mark.parametrize(
    ("load", "priority", "expected"),
    [(40, 0, "RUNNING"), (60, 2, "QUE_WAIT")],
    ids=["start", "load-waiter"],
)
def test_dl255_taking_the_load_lifts_the_resource_block_at_once(
    load: int, priority: int, expected: str
) -> None:
    """`rw255` (40 units) is past its load check and short on R255, so it
    blocks `rn255`. A priority-0 start that takes 40 units sends it back to
    its load check (30 + 40 + 40 > 80), and so does a priority-2 job queued
    for 60 units ahead of it. Either way `rn255` starts at once, with no
    unit freed."""
    taker = (
        "insert_job: zt255\njob_type: c\ncommand: z\nmachine: rl255\n"
        f"job_load: {load}\npriority: {priority}\n"
    )
    o = oracle(_RES_LOAD + _rw255(40) + taker)
    o.feed(ev("STARTJOB", 0, job="lh255"))
    o.feed(ev("STARTJOB", 0, job="rh255"))
    o.feed(ev("STARTJOB", 1, job="rw255"))
    o.feed(ev("STARTJOB", 2, job="rn255"))
    assert _statuses(o, "rw255", "rn255") == {"rw255": "QUE_WAIT", "rn255": "QUE_WAIT"}
    o.feed(ev("STARTJOB", 3, job="zt255"))
    assert _statuses(o, "zt255", "rw255", "rn255") == {
        "zt255": expected,
        "rw255": "QUE_WAIT",
        "rn255": "RUNNING",
    }


def _referencer_order(between: str) -> str:
    """R255 has 2 units, one held. `qh255` (priority 5, load 1 on q255m,
    max_load 1) waits for both units and blocks `ql255` (priority 9, other
    machine, 1 unit). SET_GLOBAL wakes, in catalog order, `qt255` (priority
    0, load 1 on q255m), then `between`, then `qc255` (priority 0, 1 unit)."""
    return (
        "insert_machine: q255m\ntype: a\nnode_name: q255m\nmax_load: 1\n\n"
        "insert_resource: R255\nres_type: R\namount: 2\n\n"
        "insert_job: qr255\njob_type: c\ncommand: h\nmachine: m9\nresources: (R255, QUANTITY=1)\n\n"
        "insert_job: qh255\njob_type: c\ncommand: w\nmachine: q255m\njob_load: 1\npriority: 5\n"
        "resources: (R255, QUANTITY=2)\n\n"
        "insert_job: ql255\njob_type: c\ncommand: l\nmachine: m9\npriority: 9\n"
        "resources: (R255, QUANTITY=1)\n\n"
        "insert_job: qt255\njob_type: c\ncommand: t\nmachine: q255m\njob_load: 1\npriority: 0\n"
        "condition: v(G255) = 1\n\n" + between + "insert_job: qc255\njob_type: c\ncommand: c\n"
        "machine: m9\npriority: 0\nresources: (R255, QUANTITY=1)\ncondition: v(G255) = 1\n"
    )


@pytest.mark.parametrize(
    "between",
    [
        "",
        "insert_job: qu255\njob_type: c\ncommand: u\nmachine: m9\ncondition: v(G255) = 1\n\n",
        "insert_job: qb255\njob_type: b\ncondition: v(G255) = 1\n\n"
        "insert_job: qm255\njob_type: c\ncommand: m\nmachine: m9\nbox_name: qb255\n\n",
    ],
    ids=["no-other-referencer", "unrelated-job", "box-start"],
)
def test_dl255_an_owed_scan_waits_for_every_referencer_of_the_input(between: str) -> None:
    """`qt255`'s start takes q255m's only unit, which sends `qh255` back to
    its load check and lifts its block on `ql255`: a scan is owed. The input
    pays it at its waiter step, after every referencer it woke (DL-50's
    order), so `qc255` takes the last unit first and `ql255` stays queued.
    A later referencer's own transition, or a box-start reset, does not pay
    it early."""
    o = oracle(_referencer_order(between))
    o.feed(ev("STARTJOB", 0, job="qr255"))
    o.feed(ev("STARTJOB", 1, job="qh255"))
    o.feed(ev("STARTJOB", 2, job="ql255"))
    assert _statuses(o, "qh255", "ql255") == {"qh255": "QUE_WAIT", "ql255": "QUE_WAIT"}
    o.feed(ev("SET_GLOBAL", 3, name="G255", value="1"))
    assert _statuses(o, "qt255", "qc255", "ql255", "qh255") == {
        "qt255": "RUNNING",
        "qc255": "RUNNING",
        "ql255": "QUE_WAIT",
        "qh255": "QUE_WAIT",
    }


# ------------------------------------------------ DL-54 Q2/Q3 additional trace tests


def test_sem21_scheduled_hold_arm_off_hold_starts_only_if_ticked() -> None:
    """T21/T32 (SEM-21/Q3, DL-54): a scheduled job's tick landing while it is
    ON_HOLD latches (SCHED_ARM), and OFF_HOLD starts it immediately through
    the still-armed schedule gate -- SEM-21's verbatim-pinned "start
    immediately after they are taken off hold" reading. A sibling held the
    whole time with NO tick ever arriving stays blocked at OFF_HOLD: the
    schedule gate still needs either a tick or a latched arm."""
    text = (
        "insert_job: hold_ticked\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: hold_unticked\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="hold_ticked"))
    o.feed(ev("STARTJOB", 1, job="hold_ticked"))
    assert transitions(o, "hold_ticked") == ["ON_HOLD", "SCHED_ARM"]
    assert o.store.job["hold_ticked"].armed
    o.feed(ev("OFF_HOLD", 2, job="hold_ticked"))
    assert transitions(o, "hold_ticked") == [
        "ON_HOLD",
        "SCHED_ARM",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]

    o.feed(ev("ON_HOLD", 3, job="hold_unticked"))
    o.feed(ev("OFF_HOLD", 4, job="hold_unticked"))
    assert transitions(o, "hold_unticked") == ["ON_HOLD", "OFF_HOLD"]
    assert o.store.job["hold_unticked"].status == "INACTIVE"


def test_sem20_scheduled_ice_never_arms_and_off_ice_condition_edge_does_not_start() -> None:
    """T20/T32 (SEM-20/Q3, DL-54): a scheduled tick blocked at ON_ICE is a
    PINNED non-arming gate -- no SCHED_ARM, no start. OFF_ICE does not
    re-evaluate on its own (SEM-20: conditions must reoccur); the fresh
    condition edge that follows still cannot start it, because it was never
    armed and it is not itself a scheduler tick."""
    text = (
        "insert_job: iced_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(gate20q)\n\n"
        "insert_job: gate20q\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("ON_ICE", 0, job="iced_sched"))
    o.feed(ev("STARTJOB", 1, job="iced_sched"))
    assert transitions(o, "iced_sched") == ["ON_ICE"]  # no SCHED_ARM at all
    assert not o.store.job["iced_sched"].armed
    o.feed(ev("OFF_ICE", 2, job="iced_sched"))
    o.feed(ev("STATUS", 3, job="gate20q", status="SUCCESS"))
    assert transitions(o, "iced_sched") == ["ON_ICE", "OFF_ICE"]
    assert o.store.job["iced_sched"].status == "INACTIVE"


def test_sem32_box_member_tick_while_box_not_running_does_not_arm() -> None:
    """T10/T32 (SEM-10/31 double gate + Q3, DL-54): a scheduled box member's
    tick while its box is not yet RUNNING is a PINNED non-arming gate -- the
    box-not-RUNNING check is reached and returns before the arm call. The
    dead tick is VISIBLE as a START_REFUSED record (DL-64: the explicit
    event path surfaces SEM-10 refusals; this is observability, not a
    semantic change) but arms nothing. When the box later starts, the
    member does not start from that dead tick (only a fresh tick or a
    latched arm would let it in)."""
    text = (
        "insert_job: box_ng\njob_type: b\n\n"
        "insert_job: mem_ng\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_ng\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="mem_ng"))
    assert transitions(o, "mem_ng") == ["START_REFUSED"]  # recorded, nothing else
    assert not o.store.job["mem_ng"].armed
    o.feed(ev("STARTJOB", 1, job="box_ng"))
    assert transitions(o, "mem_ng") == ["START_REFUSED"]  # the dead tick stays dead
    assert o.store.job["mem_ng"].status == "INACTIVE"
    assert o.store.job["box_ng"].status == "RUNNING"  # hung: sole member never ran


def test_sem32_force_startjob_consumes_the_arm_blocking_a_later_condition_restart() -> None:
    """T32 (SEM-32/Q3, DL-54): a scheduled tick with a false condition arms
    the job; FORCE_STARTJOB then runs it regardless of the still-false
    condition (SEM-23) and consumes the arm just like any other start
    ("FORCE included"). After it completes, a fresh condition edge cannot
    restart it -- the schedule gate is closed again."""
    text = (
        "insert_job: force_arm\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(gate_fa)\n\n"
        "insert_job: gate_fa\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="force_arm"))  # condition false: arms
    assert transitions(o, "force_arm") == ["SCHED_ARM"]
    o.feed(ev("FORCE_STARTJOB", 1, job="force_arm"))
    assert transitions(o, "force_arm") == ["SCHED_ARM", "INACTIVE->STARTING", "STARTING->RUNNING"]
    assert not o.store.job["force_arm"].armed  # forced start consumed it
    o.feed(ev("STATUS", 2, job="force_arm", status="SUCCESS"))
    o.feed(ev("STATUS", 3, job="gate_fa", status="SUCCESS"))  # fresh condition edge
    assert transitions(o, "force_arm")[-1] == "RUNNING->SUCCESS"  # unchanged: no restart


def test_sem32_repeated_false_condition_ticks_arm_exactly_once() -> None:
    """T32 (SEM-32/Q3, DL-54): a second scheduled tick while the condition is
    still false does not re-arm or double-record -- `_arm` is a no-op once
    `armed` is already set. Exactly one SCHED_ARM trace entry survives two
    ticks, and the eventual condition edge still produces exactly one start."""
    text = (
        "insert_job: idem_arm\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(gate_idem)\n\n"
        "insert_job: gate_idem\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="idem_arm"))
    o.feed(ev("STARTJOB", 1, job="idem_arm"))
    assert transitions(o, "idem_arm") == ["SCHED_ARM"]  # not two, despite two ticks
    o.feed(ev("STATUS", 2, job="gate_idem", status="SUCCESS"))
    assert transitions(o, "idem_arm") == [
        "SCHED_ARM",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem33_run_window_defer_after_arm_starts_at_window_open() -> None:
    """T32/T33 (SEM-32/33, DL-54): a scheduled tick lands INSIDE the
    run_window with the condition still false -- it arms before run_window is
    even reached (the condition-false branch returns first). The armed job's
    later condition edge, arriving OUTSIDE the window and closer to the next
    opening than the previous close, passes the schedule gate on the arm and
    then hits SEM-33's closer-edge rule: RUN_WINDOW_DEFER, a TIMER queued for
    window open, and the actual start happens there -- run_window gates the
    armed start exactly like an unarmed one."""
    text = (
        "insert_job: job_rw_arm\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n'
        "condition: s(gate_rw_arm)\n\n"
        "insert_job: gate_rw_arm\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: dummy_rw_arm\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 10, 5), kind="STARTJOB", payload={"job": "job_rw_arm"}))
    assert transitions(o, "job_rw_arm") == ["SCHED_ARM"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 9, 50),
            kind="STATUS",
            payload={"job": "gate_rw_arm", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "job_rw_arm") == ["SCHED_ARM", "RUN_WINDOW_DEFER"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 10, 1),
            kind="STATUS",
            payload={"job": "dummy_rw_arm", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "job_rw_arm") == [
        "SCHED_ARM",
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
    start_entry = next(
        t for t in o.trace() if t.job == "job_rw_arm" and t.transition.endswith("STARTING")
    )
    assert start_entry.at == datetime(2026, 7, 2, 10, 0)  # window-open time, run_window applied


def test_sem04_zero_lookback_box_anchor_is_the_box_own_last_end() -> None:
    """T04/T12 (SEM-04/SEM-12, DL-54): for a box override the zero-lookback
    evaluator is the BOX itself, not the member that completes -- "for box
    overrides the box itself is the evaluator/anchor." Run 1: ext7 succeeds
    while the box has never completed (Q2b unbounded) -> box_success fires
    on the run's last member transition, setting the box's OWN last_end_at.
    Run 2: that same ext7 latch is now STALE relative to the box's own
    last_end_at from run 1 -- the override does NOT fire on mem7a's
    completion. A fresh ext7 success (after the box's last_end_at) DOES fire
    it on mem7b's completion, proving the anchor tracks the box, not either
    member."""
    text = (
        "insert_job: box7\njob_type: b\nbox_success: s(ext7, 0)\n\n"
        "insert_job: mem7a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box7\n\n"
        "insert_job: mem7b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box7\n\n"
        "insert_job: ext7\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="box7"))
    o.feed(ev("STATUS", 5, job="mem7a", status="SUCCESS"))
    assert transitions(o, "box7") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 7, job="ext7", status="SUCCESS"))  # the latch that becomes stale
    o.feed(ev("STATUS", 10, job="mem7b", status="SUCCESS"))
    assert transitions(o, "box7") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",  # Q2b unbounded: the box never ended before this
    ]
    box_entries = [t for t in o.trace() if t.job == "box7"]
    assert "box_success" in box_entries[-1].cause

    o.feed(ev("STARTJOB", 15, job="box7"))
    assert transitions(o, "box7")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 20, job="mem7a", status="FAILURE"))
    assert transitions(o, "box7")[-2:] == [
        "SUCCESS->STARTING",
        "STARTING->RUNNING",
    ]  # stale: no fire
    o.feed(ev("STATUS", 25, job="ext7", status="SUCCESS"))  # fresh: after the box's last_end_at
    o.feed(ev("STATUS", 30, job="mem7b", status="SUCCESS"))
    assert transitions(o, "box7")[-1] == "RUNNING->SUCCESS"
    assert len(transitions(o, "box7")) == 6


def test_sem04_zero_lookback_exact_tie_at_the_anchor_instant_is_satisfied() -> None:
    """T04 (SEM-04), Q2a: `>=` is inclusive at the exact instant. cons8's OWN
    prior completion and pred8's success are engineered onto the identical
    datetime (an ON_NOEXEC bypass gives cons8 an instant first-run completion,
    then a separately-timed producer success lands on that exact same
    instant): pred8.status_at == cons8.last_end_at, not merely close, and the
    zero-lookback atom still fires."""
    text = (
        "insert_job: pred8\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons8\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(pred8, 0)\n"
    )
    o = oracle(text)
    tie = datetime(2026, 7, 1, 9, 0)
    o.feed(Event(at=tie, kind="ON_NOEXEC", payload={"job": "cons8"}))
    o.feed(Event(at=tie, kind="FORCE_STARTJOB", payload={"job": "cons8"}))
    assert transitions(o, "cons8") == ["ON_NOEXEC", "INACTIVE->SUCCESS"]
    assert o.store.job["cons8"].last_end_at == tie
    o.feed(Event(at=tie, kind="OFF_NOEXEC", payload={"job": "cons8"}))
    o.feed(Event(at=tie, kind="STATUS", payload={"job": "pred8", "status": "SUCCESS"}))
    assert o.store.job["pred8"].status_at == tie == o.store.job["cons8"].last_end_at
    assert transitions(o, "cons8") == [
        "ON_NOEXEC",
        "INACTIVE->SUCCESS",
        "OFF_NOEXEC",
        "SUCCESS->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem32_armed_survives_ice_off_ice_cycle_then_starts_on_fresh_edge() -> None:
    """T32/T20 (SEM-32/SEM-20, Q3, DL-54): an arm latched by a scheduled tick
    is untouched by a subsequent ON_ICE/OFF_ICE cycle -- ON_ICE's early
    return in _attempt_start never reaches the arm, and OFF_ICE only clears
    on_ice, not armed. The job stays blocked (iced) throughout, then a fresh
    condition edge after OFF_ICE starts it THROUGH the still-latched arm --
    exactly the schedule-gate bypass a bare condition edge could not achieve
    on its own (contrast: the never-armed ice-no-arm test above)."""
    text = (
        "insert_job: ice_arm_survives\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(gate_ias)\n\n"
        "insert_job: gate_ias\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ice_arm_survives"))  # condition false: arms
    assert transitions(o, "ice_arm_survives") == ["SCHED_ARM"]
    o.feed(ev("ON_ICE", 1, job="ice_arm_survives"))
    assert o.store.job["ice_arm_survives"].armed  # ice does not clear it
    o.feed(ev("OFF_ICE", 2, job="ice_arm_survives"))
    assert o.store.job["ice_arm_survives"].armed  # off-ice does not clear it either
    assert transitions(o, "ice_arm_survives") == ["SCHED_ARM", "ON_ICE", "OFF_ICE"]
    o.feed(ev("STATUS", 3, job="gate_ias", status="SUCCESS"))  # fresh condition edge
    assert transitions(o, "ice_arm_survives") == [
        "SCHED_ARM",
        "ON_ICE",
        "OFF_ICE",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


# ------------------------- DL-158 operator DISARM (period-model ss10.4)


_DISARM_JIL = (
    "insert_job: dis158\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    "condition: s(gate158)\n\n"
    "insert_job: gate158\njob_type: c\ncommand: y\nmachine: m1\n\n"
    "insert_job: down158\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(dis158)\n"
)


def test_dl158_disarm_drops_the_latch_and_the_later_edge_does_not_start() -> None:
    """period-model ss10.4 (DL-158): the operator DISARM clears the SEM-32
    armed latch and does nothing else. The later condition edge meets the
    schedule gate unarmed and produces NO start. The drop moves exactly one
    revision (armed is projected state an expect can be composed against),
    and a downstream referencer of the disarmed job sees nothing -- the
    verb wakes nobody."""
    o = oracle(_DISARM_JIL)
    o.feed(ev("STARTJOB", 0, job="dis158"))  # tick, condition false -> arms
    assert o.store.job["dis158"].armed
    rev = o.store.revision("job:dis158")
    o.feed(ev("DISARM", 1, job="dis158"))
    assert not o.store.job["dis158"].armed
    assert o.store.job["dis158"].status == "INACTIVE"  # no status move
    assert transitions(o, "dis158") == ["SCHED_ARM", "DISARM"]
    assert o.store.revision("job:dis158") == rev + 1  # the drop, exactly once
    o.feed(ev("STATUS", 2, job="gate158", status="SUCCESS"))  # the condition edge
    assert transitions(o, "dis158") == ["SCHED_ARM", "DISARM"]  # no start
    assert o.store.job["dis158"].status == "INACTIVE"
    assert transitions(o, "down158") == []  # referencer of dis158 never woke to anything


def test_dl158_disarm_on_an_unarmed_job_is_an_accepted_recorded_no_op() -> None:
    """DL-158: an unarmed target accepts the DISARM as a recorded no-op --
    the OFF_HOLD shape. The trace carries the marker; the projection does
    not change, so the revision does not move either."""
    o = oracle(_DISARM_JIL)
    o.feed(ev("DISARM", 0, job="dis158"))
    assert transitions(o, "dis158") == ["DISARM"]  # recorded
    assert not o.store.job["dis158"].armed
    assert o.store.job["dis158"].status == "INACTIVE"
    assert o.store.revision("job:dis158") == 0  # nothing changed, nothing moved


def test_dl158_disarm_is_legal_at_any_time_a_running_job_just_keeps_running() -> None:
    """DL-158: the verb is allowed at any time, not only pre-seal and not
    only while INACTIVE. On a RUNNING job it records the marker, drops
    nothing (a started job consumed its arm already), and the run completes
    exactly as it would have."""
    text = "insert_job: live158\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="live158"))
    assert o.store.job["live158"].status == "RUNNING"
    o.feed(ev("DISARM", 1, job="live158"))
    assert o.store.job["live158"].status == "RUNNING"  # no status move
    o.feed(ev("STATUS", 2, job="live158", status="SUCCESS"))
    assert transitions(o, "live158") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "DISARM",
        "RUNNING->SUCCESS",
    ]


def test_dl158_operator_disarm_before_the_fold_leaves_no_sched_disarm() -> None:
    """DL-158 x Q3c: the box fold drops an unconsumed member arm under the
    ENGINE's marker (SCHED_DISARM, the Q3c pin). An operator DISARM that
    lands first leaves the fold nothing to drop: the trace shows the
    operator marker and NO engine one, so an audit reader can attribute
    the drop. The fold's own behavior is untouched
    (test_sem32_member_arm_dies_with_its_box_run pins it)."""
    text = (
        "insert_job: nightly158\njob_type: b\nbox_success: s(anchor158)\n\n"
        "insert_job: anchor158\njob_type: c\ncommand: a\nmachine: m1\nbox_name: nightly158\n\n"
        "insert_job: late158\njob_type: c\ncommand: b\nmachine: m1\nbox_name: nightly158\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(feed158)\n\n"
        "insert_job: feed158\njob_type: c\ncommand: f\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="nightly158"))  # box run 1
    o.feed(ev("STARTJOB", 60, job="late158"))  # member tick, condition false -> arms
    assert o.store.job["late158"].armed
    o.feed(ev("DISARM", 90, job="late158"))  # the operator gets there first
    assert not o.store.job["late158"].armed
    o.feed(ev("STATUS", 120, job="anchor158", status="SUCCESS"))  # box_success folds the box
    assert o.store.job["nightly158"].status == "SUCCESS"
    assert transitions(o, "late158") == ["SCHED_ARM", "DISARM"]  # no SCHED_DISARM: nothing to drop


def test_dl158_disarm_leaves_timers_alone_the_must_start_alarm_still_fires() -> None:
    """DL-158: the drop touches no timer. A tick arms the SEM-34 must_start
    deadline AND the SEM-32 latch; the DISARM drops the latch and the
    deadline still fires its alarm on schedule -- disarming is not
    unscheduling."""
    text = (
        "insert_job: ms158\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "must_start_times: +30\ncondition: s(gate_ms158)\n\n"
        "insert_job: gate_ms158\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ms158"))  # arms latch + must_start deadline
    assert o.store.job["ms158"].armed
    timers_before = list(o.store.timers())
    o.feed(ev("DISARM", 5, job="ms158"))
    assert list(o.store.timers()) == timers_before  # no timer mutated
    o.feed(ev("STATUS", 40, job="gate_ms158", status="FAILURE"))  # clock passes tick+30
    assert "MUST_START_ALARM" in transitions(o, "ms158")


def test_dl233_second_disarm_at_its_own_deadline_still_reads_no_latch() -> None:
    """DL-233: the revisions map is not the audit discriminator. A
    must_start deadline due at the SAME instant as a second DISARM fires
    inside that DISARM's own batch (one input covers the fired timers and
    the feed, concurrency-model ss0), moving the job's revision even
    though the first DISARM already dropped the latch. The trace reason
    on the second DISARM still reads '(no latch)'."""
    text = (
        "insert_job: ms233\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "must_start_times: +30\ncondition: s(gate_ms233)\n\n"
        "insert_job: gate_ms233\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ms233"))  # arms latch + must_start deadline at t+30
    assert o.store.job["ms233"].armed
    o.feed(ev("DISARM", 5, job="ms233"))  # first DISARM: the real drop
    assert not o.store.job["ms233"].armed
    rev = o.store.revision("job:ms233")
    o.feed(ev("DISARM", 30, job="ms233"))  # second DISARM, at the deadline's own due instant
    assert "MUST_START_ALARM" in transitions(o, "ms233")  # the deadline fired in this batch
    assert o.store.revision("job:ms233") == rev + 1  # moved by the alarm, not by the DISARM
    disarms = [t for t in o.trace() if t.job == "ms233" and t.transition == "DISARM"]
    assert disarms[-1].cause == "sendevent DISARM (no latch)"


def test_dl158_disarm_does_not_cancel_a_run_window_deferred_start() -> None:
    """DL-158: a deferred start is already out of the latch -- it rides a
    RUN_WINDOW_DEFER timer, and the disarm drops only the latch visible at
    application time. The deferred start still fires at window open."""
    text = (
        "insert_job: rw158\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n'
        "condition: s(gate_rw158)\n\n"
        "insert_job: gate_rw158\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: dummy_rw158\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 10, 5), kind="STARTJOB", payload={"job": "rw158"}))
    assert transitions(o, "rw158") == ["SCHED_ARM"]
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 9, 50),
            kind="STATUS",
            payload={"job": "gate_rw158", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw158") == ["SCHED_ARM", "RUN_WINDOW_DEFER"]
    o.feed(Event(at=datetime(2026, 7, 2, 9, 51), kind="DISARM", payload={"job": "rw158"}))
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 10, 0, 1),
            kind="STATUS",
            payload={"job": "dummy_rw158", "status": "SUCCESS"},
        )
    )
    assert "INACTIVE->STARTING" in transitions(o, "rw158")  # the deferred start still landed


def test_dl158_disarm_leaves_a_que_wait_job_queued() -> None:
    """DL-158: no waiter wakeup and no dequeue. A DISARM on a QUE_WAIT job
    records the marker, the rank survives, and the queued start still
    happens when the holder releases."""
    text = (
        "insert_resource: R158\nres_type: R\namount: 1\n\n"
        "insert_job: holder158\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R158, QUANTITY=1)\n\n"
        "insert_job: queued158\njob_type: c\ncommand: y\nmachine: m1\n"
        "resources: (R158, QUANTITY=1)\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="holder158"))
    o.feed(ev("STARTJOB", 1, job="queued158"))
    assert o.store.job["queued158"].status == "QUE_WAIT"
    rank = o.store.job["queued158"].waiter_seq
    o.feed(ev("DISARM", 2, job="queued158"))
    assert o.store.job["queued158"].status == "QUE_WAIT"  # still queued
    assert o.store.job["queued158"].waiter_seq == rank  # same rank
    o.feed(ev("STATUS", 3, job="holder158", status="SUCCESS"))  # release wakes the queue
    assert o.store.job["queued158"].status == "RUNNING"


def test_dl158_noop_disarm_then_a_tick_ends_armed_applied_is_not_inhibition() -> None:
    """DL-158 race pin, deterministic by admission order: a no-op DISARM
    applied at revision N followed by a scheduler tick ends ARMED --
    `applied` means the latch visible then was dropped (there was none),
    not that future arms are inhibited. The reverse order is the stale
    expect rejection, pinned at the control tier."""
    o = oracle(_DISARM_JIL)
    o.feed(ev("DISARM", 0, job="dis158"))  # no-op: nothing latched yet
    o.feed(ev("STARTJOB", 0, job="dis158"))  # the tick lands right after
    assert o.store.job["dis158"].armed  # armed won: the disarm inhibited nothing
    assert transitions(o, "dis158") == ["DISARM", "SCHED_ARM"]


def test_dl158_disarm_wakes_no_referencer_and_starts_nothing() -> None:
    """DL-158 vacuous-pin closure (review findings 1 and 2): both jobs are
    COMPLETED on conditions that are still true, so a stray wake or a stray
    start attempt in the DISARM branch would re-run one of them -- an
    edge-triggered wake would restart the downstream (SUCCESS->STARTING),
    and an OFF_HOLD-shaped start attempt would restart the disarmed job
    itself. Neither may move."""
    text = (
        "insert_job: a158\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b158\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(a158)\n"
    )
    done = ["INACTIVE->STARTING", "STARTING->RUNNING", "RUNNING->SUCCESS"]
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="a158"))
    o.feed(ev("STATUS", 1, job="a158", status="SUCCESS"))  # wakes b158: it runs
    o.feed(ev("STATUS", 2, job="b158", status="SUCCESS"))
    assert transitions(o, "b158") == done
    o.feed(ev("DISARM", 3, job="a158"))  # upstream: a stray wake would re-run b158
    assert transitions(o, "a158") == [*done, "DISARM"]  # and a stray start, a158 itself
    assert transitions(o, "b158") == done  # no wake rode the disarm
    o.feed(ev("DISARM", 4, job="b158"))  # self: a stray start attempt would re-run b158
    assert transitions(o, "b158") == [*done, "DISARM"]  # recorded, nothing started
    assert o.store.job["a158"].status == "SUCCESS"
    assert o.store.job["b158"].status == "SUCCESS"


def test_dl158_disarm_wakes_no_waiter_where_a_wake_would_act() -> None:
    """DL-158 vacuous-pin closure (review finding 3): after the box dies,
    its queued member is CANCELLABLE at the very next wake scan (_readmit:
    box no longer RUNNING), so a stray _wake_waiters() in the DISARM branch
    would be observable here as a QUE_WAIT -> INACTIVE cancellation. The
    disarm must leave the row queued; only the holder's release scans the
    queue (the SEM-32 que-wait-and-box-death shape with the disarm inserted
    into the window)."""
    text = (
        "insert_resource: POOL158\nres_type: R\namount: 1\n\n"
        "insert_job: hog158\njob_type: c\ncommand: h\nmachine: m1\n"
        "resources: (POOL158, QUANTITY=1)\n\n"
        "insert_job: qbx158\njob_type: b\n\n"
        "insert_job: qm158\njob_type: c\ncommand: x\nmachine: m1\nbox_name: qbx158\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "condition: s(qgate158)\nresources: (POOL158, QUANTITY=1)\n\n"
        "insert_job: qgate158\njob_type: c\ncommand: g\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="hog158"))  # saturate the pool
    o.feed(ev("STARTJOB", 1, job="qbx158"))  # box run 1
    o.feed(ev("STARTJOB", 2, job="qm158"))  # its tick: condition false -> arms
    o.feed(ev("STATUS", 3, job="qgate158", status="SUCCESS"))  # edge -> start -> QUE_WAIT
    assert o.store.job["qm158"].status == "QUE_WAIT"
    o.feed(ev("KILLJOB", 4, job="qbx158"))  # box dies; the member stays queued until a scan
    assert o.store.job["qm158"].status == "QUE_WAIT"
    o.feed(ev("DISARM", 5, job="qm158"))  # a stray wake scan would cancel it right here
    assert o.store.job["qm158"].status == "QUE_WAIT"  # still queued: no scan rode the disarm
    o.feed(ev("STATUS", 6, job="hog158", status="SUCCESS"))  # the real scan
    assert o.store.job["qm158"].status == "INACTIVE"  # cancelled by the release, not the DISARM


# ------------------------------------------- DL-54 fix pins


def test_sem32_member_arm_dies_with_its_box_run() -> None:
    """DL-54: a member's arm is scoped to the box run that armed
    it. Armed in run 1 (tick with false condition), the box completes via
    box_success with the member unrun -> SCHED_DISARM; the condition edge
    while the box is down cannot start it, and -- the actual defect -- the
    START of box run 2 must not auto-start it either. Its real run-2 tick,
    with the condition now latched true, starts it normally."""
    text = (
        "insert_job: nightly54\njob_type: b\nbox_success: s(anchor54)\n\n"
        "insert_job: anchor54\njob_type: c\ncommand: a\nmachine: m1\nbox_name: nightly54\n\n"
        "insert_job: late54\njob_type: c\ncommand: b\nmachine: m1\nbox_name: nightly54\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(feed54)\n\n"
        "insert_job: feed54\njob_type: c\ncommand: f\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="nightly54"))  # box run 1; anchor54 starts, late54 double-gated
    o.feed(ev("STARTJOB", 60, job="late54"))  # its tick: box RUNNING, s(feed54) false
    assert transitions(o, "late54") == ["SCHED_ARM"]
    o.feed(ev("STATUS", 120, job="anchor54", status="SUCCESS"))  # box_success folds the box
    assert o.store.job["nightly54"].status == "SUCCESS"
    assert transitions(o, "late54") == ["SCHED_ARM", "SCHED_DISARM"]
    assert not o.store.job["late54"].armed
    o.feed(ev("STATUS", 840, job="feed54", status="SUCCESS"))  # edge while box down: no start
    assert o.store.job["late54"].status == "INACTIVE"
    o.feed(ev("STARTJOB", 1440, job="nightly54"))  # box run 2 START: no stale-arm auto-start
    assert o.store.job["late54"].status == "INACTIVE"
    o.feed(ev("STARTJOB", 1500, job="late54"))  # its real run-2 tick: s(feed54) latched true
    assert transitions(o, "late54") == [
        "SCHED_ARM",
        "SCHED_DISARM",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem32_held_member_of_idle_box_does_not_arm() -> None:
    """DL-54: the hold gate precedes the box gate, so _arm must
    re-check box state -- a HELD member of a NOT-running box gets no arm from
    its tick, and off-hold inside a later box run cannot start it from that
    dead tick."""
    text = (
        "insert_job: bx54\njob_type: b\n\n"
        "insert_job: hm54\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx54\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="hm54"))
    o.feed(ev("STARTJOB", 1, job="hm54"))  # its tick: held AND box not running
    assert transitions(o, "hm54") == ["ON_HOLD"]  # no SCHED_ARM
    assert not o.store.job["hm54"].armed
    o.feed(ev("STARTJOB", 2, job="bx54"))  # box runs; hm54 held through the member launch
    o.feed(ev("OFF_HOLD", 3, job="hm54"))  # never armed -> schedule gate blocks
    assert o.store.job["hm54"].status == "INACTIVE"
    assert transitions(o, "hm54") == ["ON_HOLD", "OFF_HOLD"]


def test_sem32_held_member_of_running_box_arms_and_off_hold_starts() -> None:
    """DL-54: the counterpart pin -- a held member of a RUNNING box does arm
    from its tick (SEM-21's off-hold start applies within the box run)."""
    text = (
        "insert_job: bx54r\njob_type: b\n\n"
        "insert_job: hm54r\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx54r\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="bx54r"))  # box run 1; member double-gated, waits for tick
    o.feed(ev("ON_HOLD", 1, job="hm54r"))
    o.feed(ev("STARTJOB", 2, job="hm54r"))  # its tick: held, box RUNNING -> arms
    assert transitions(o, "hm54r") == ["ON_HOLD", "SCHED_ARM"]
    o.feed(ev("OFF_HOLD", 3, job="hm54r"))
    assert transitions(o, "hm54r") == [
        "ON_HOLD",
        "SCHED_ARM",
        "OFF_HOLD",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]


def test_sem32_que_wait_enqueue_keeps_arm_and_box_death_disarms() -> None:
    """DL-54: the ACTUAL start consumes the arm, not the QUE_WAIT
    enqueue -- and when the box run dies with the member still queued, the
    queue attempt is cancelled AND the arm dies with the box run (zero runs
    from the tick, arm accounted for by SCHED_DISARM, nothing latched)."""
    text = (
        "insert_resource: POOL54\nres_type: R\namount: 1\n\n"
        "insert_job: hog54\njob_type: c\ncommand: h\nmachine: m1\n"
        "resources: (POOL54, QUANTITY=1)\n\n"
        "insert_job: qbx54\njob_type: b\n\n"
        "insert_job: qm54\njob_type: c\ncommand: x\nmachine: m1\nbox_name: qbx54\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "condition: s(qgate54)\nresources: (POOL54, QUANTITY=1)\n\n"
        "insert_job: qgate54\njob_type: c\ncommand: g\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="hog54"))  # saturate the pool
    o.feed(ev("STARTJOB", 1, job="qbx54"))  # box run 1
    o.feed(ev("STARTJOB", 2, job="qm54"))  # its tick: condition false -> arms
    assert transitions(o, "qm54") == ["SCHED_ARM"]
    o.feed(ev("STATUS", 3, job="qgate54", status="SUCCESS"))  # edge -> start -> QUE_WAIT
    assert o.store.job["qm54"].status == "QUE_WAIT"
    assert o.store.job["qm54"].armed  # the enqueue did NOT consume the arm
    o.feed(ev("KILLJOB", 4, job="qbx54"))  # box dies: the member's arm dies with the run
    assert not o.store.job["qm54"].armed
    assert transitions(o, "qm54")[-1] == "SCHED_DISARM"
    o.feed(ev("STATUS", 5, job="hog54", status="SUCCESS"))  # release scans the queue
    assert o.store.job["qm54"].status == "INACTIVE"  # cancelled: box no longer RUNNING
    assert o.store.job["qm54"].run_number == 0  # zero runs from the tick -- accounted, not eaten


def test_sem04_zero_lookback_n_atom_ignores_non_end_transitions() -> None:
    """DL-54: BOTH sides of the Q2a citation are END times. An
    n(p, 0) predecessor bounced to INACTIVE by an injected status has not
    "run since" anything -- its last_end_at is unchanged -- so the consumer
    must not restart; a real completed run afterwards does refresh it."""
    text = (
        "insert_job: p54n\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: c54n\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(p54n, 0)\n"
    )
    o = oracle(text)
    o.feed(ev("STATUS", 0, job="p54n", status="SUCCESS"))  # p ends 00:00; c first-run starts
    o.feed(ev("STATUS", 60, job="c54n", status="SUCCESS"))  # c's anchor: 01:00
    o.feed(ev("STATUS", 120, job="p54n", status="INACTIVE"))  # NOT an end: no restart
    assert transitions(o, "c54n") == [
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
        "RUNNING->SUCCESS",
    ]
    o.feed(ev("STATUS", 180, job="p54n", status="SUCCESS"))  # a real end at 03:00: fresh
    assert transitions(o, "c54n")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]


def test_sem33_armed_repeat_edges_queue_one_defer_timer() -> None:
    """DL-54: an armed job whose condition keeps re-latching
    outside the run_window records ONE defer (one pending timer per opening
    instant), not one per edge, and starts exactly once at window open."""
    text = (
        "insert_job: rw54\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        'run_window: "02:00-04:00"\ncondition: s(g54)\n\n'
        "insert_job: g54\njob_type: c\ncommand: g\nmachine: m1\n\n"
        "insert_job: idle54\njob_type: c\ncommand: i\nmachine: m1\n"
    )
    o = oracle(text)
    base = datetime(2026, 7, 1, 8, 0)
    o.feed(Event(at=base, kind="STARTJOB", payload={"job": "rw54"}))  # tick: cond false, arms
    for minute in (0, 15, 30):  # three edges at 23:00/23:15/23:30, closer to next opening
        o.feed(
            Event(
                at=base.replace(hour=23, minute=minute),
                kind="STATUS",
                payload={"job": "g54", "status": "SUCCESS"},
            )
        )
    defers = [t for t in transitions(o, "rw54") if t == "RUN_WINDOW_DEFER"]
    assert defers == ["RUN_WINDOW_DEFER"]  # deduped: one per opening instant
    o.feed(
        Event(
            at=datetime(2026, 7, 2, 2, 30),
            kind="STATUS",
            payload={"job": "idle54", "status": "SUCCESS"},
        )
    )
    assert transitions(o, "rw54") == [
        "SCHED_ARM",
        "RUN_WINDOW_DEFER",
        "INACTIVE->STARTING",
        "STARTING->RUNNING",
    ]
