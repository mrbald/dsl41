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
from dsl41.oracle import Event, EventKind, Oracle, OracleError, TraceEntry

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


def oracle(jil_text: str) -> Oracle | EngineHarness:
    catalog = lower_source(jil_text)
    if _ENGINE_PATH:
        harness = EngineHarness(catalog)
        _HARNESSES.append(harness)
        return harness
    return Oracle(catalog)


def transitions(o: Oracle | EngineHarness, job: str) -> list[str]:
    return [t.transition for t in o.trace() if t.job == job]


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
    again."""
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
    assert transitions(o, "mem_u")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "mem_c")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]


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


# ------------------------------------------------------------ 11. SEM-13 sticky TERMINATED


def test_sem13_terminated_box_is_sticky_then_restarts_fresh() -> None:
    """T13 (SEM-13): KILLJOB-ing a RUNNING box moves it to TERMINATED, which
    is sticky -- a member STATUS change afterward does not alter the box.
    The member without job_terminator survives the kill (stays RUNNING);
    the never-run member stays INACTIVE and cannot start while the box is
    TERMINATED even once its own condition becomes true. The next STARTJOB
    of the box starts it fresh: the already-SUCCESS member runs again, and
    the previously-INACTIVE member (whose condition is now satisfied) runs
    for the first time."""
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
    assert transitions(o, "mem13a")[-2:] == ["SUCCESS->STARTING", "STARTING->RUNNING"]
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


# ------------------------------------------------------------- 16. SEM-23 FORCE_STARTJOB


def test_sem23_force_startjob_ignores_condition_and_hold_and_satisfies_downstream() -> None:
    """T23 (SEM-23): FORCE_STARTJOB starts the job regardless of a false
    condition AND regardless of ON_HOLD; the forced run still emits normal
    status events, so its SUCCESS satisfies a downstream latching
    condition just like a normal run would."""
    text = (
        "insert_job: held_false23\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(never_true23)\n\n"
        "insert_job: never_true23\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: cons23\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(held_false23)\n"
    )
    o = oracle(text)
    o.feed(ev("ON_HOLD", 0, job="held_false23"))
    o.feed(ev("FORCE_STARTJOB", 1, job="held_false23"))
    assert transitions(o, "held_false23") == ["ON_HOLD", "INACTIVE->STARTING", "STARTING->RUNNING"]
    o.feed(ev("STATUS", 2, job="held_false23", status="SUCCESS"))
    assert transitions(o, "cons23") == ["INACTIVE->STARTING", "STARTING->RUNNING"]


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
    """T33 box variant (SEM-33 "Box interaction" note): a run_window-gated
    member deferred to the next window opening keeps the containing box
    RUNNING overnight. Under the SEM-31/L013 double gate (DL-13) the
    scheduled member no longer auto-starts with the box -- its own
    start-time tick (script STARTJOB at 09:50) is what meets the window
    gate and gets deferred; the box folds only once the deferred member
    eventually starts (via its queued timer) and completes."""
    text = (
        "insert_job: box_rw33\njob_type: b\n\n"
        "insert_job: rw_member33\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_rw33\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:50"\n'
        'run_window: "10:00-11:00"\n'
    )
    o = oracle(text)
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "box_rw33"}))
    assert transitions(o, "box_rw33") == ["INACTIVE->STARTING", "STARTING->RUNNING"]
    assert transitions(o, "rw_member33") == []  # double gate: waits for its tick
    o.feed(Event(at=datetime(2026, 7, 1, 9, 50), kind="STARTJOB", payload={"job": "rw_member33"}))
    assert transitions(o, "rw_member33") == ["RUN_WINDOW_DEFER"]
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
    """Regression pin for the phase-7 review BLOCKER (originally a strict
    xfail): with a normal member plus a run_window-DEFERRED member, the
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


# --------------------------------------------------------------------- 19. SEM-34 must_*


def test_sem34a_must_complete_alarm_not_emitted_when_job_finishes_in_time() -> None:
    """T34a (SEM-34): must_complete_times: +5 arms a deadline timer relative
    to the start. Completing at +2 (before the deadline) means the timer,
    when it eventually pops at +5, finds the job no longer RUNNING -> no
    alarm ever, no matter how much later the clock advances."""
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
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    o = oracle(text)
    o.feed(ev("STATUS", 5, job="solo", status="SUCCESS"))
    with pytest.raises(OracleError, match="backwards"):
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

# Behaviors fixed after the phase-7 adversarial review; each test pins the
# corrected reading so it cannot regress silently.


def test_completed_consumer_reruns_on_each_fresh_producer_success() -> None:
    """Review MAJOR: edge-triggered re-evaluation (DL-13) -- every new
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
    """Review BLOCKER (SEM-11 literal, DL-13): a member whose condition is
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
    """Review MAJOR (SEM-31/L013, DL-13): a date_conditions member of a
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
    """Review MINOR (SEM-34): must_start_times arms on the STARTJOB tick;
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
    """Review MINOR (DL-13): atoms read the real in-flight status of an
    iced-but-RUNNING job; the satisfied-by-ice reading applies only once
    the run completes."""
    text = (
        "insert_job: ir_p\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: ir_c\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(ir_p)\n"
    )
    o = oracle(text)
    o.feed(ev("FORCE_STARTJOB", 0, job="ir_p"))
    o.feed(ev("ON_ICE", 1, job="ir_p"))
    assert o.store.job["ir_c"].status == "INACTIVE"  # run still real: s(ir_p) false
    o.feed(ev("STATUS", 2, job="ir_p", status="FAILURE"))  # run completes (failed!)
    # now iced satisfies every atom kind (DL-13 reading): s(ir_p) true
    assert o.store.job["ir_c"].status == "RUNNING"


def test_sem15_idle_box_recompute_derives_status_from_member_changes() -> None:
    """Review MINOR (SEM-15 [C]): terminal member transitions on a
    non-running box re-derive its status once all members are terminal --
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


def test_trace_returns_copies_not_aliases() -> None:
    """Review NIT: mutating a returned TraceEntry must not corrupt the
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
    max_load=2 machine, the third queues, and admits on a release."""
    text = (
        "insert_machine: box1\ntype: a\nnode_name: box1\nmax_load: 2\n\n"
        "insert_job: ml1\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\n\n"
        "insert_job: ml2\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\n\n"
        "insert_job: ml3\njob_type: c\ncommand: x\nmachine: box1\njob_load: 1\n"
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
    """When one slot frees, the higher-priority waiter (lower number, # PENDING
    Qr2) admits and the lower-priority one stays queued."""
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
    """Adversarial-review BLOCKER: a resource holder that re-triggers itself
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
    """Adversarial-review MAJOR: KILLJOB on a QUE_WAIT (standalone) job must
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
    """Adversarial-review NIT: ON_ICE on a QUE_WAIT job settles it to INACTIVE
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


# ------------------------------------------- DL-54 adversarial-review fix pins


def test_sem32_member_arm_dies_with_its_box_run() -> None:
    """DL-54 review MAJOR: a member's arm is scoped to the box run that armed
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
    """DL-54 review MAJOR: the hold gate precedes the box gate, so _arm must
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
    """DL-54 review MINOR: the ACTUAL start consumes the arm, not the QUE_WAIT
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
    """DL-54 review MINOR: BOTH sides of the Q2a citation are END times. An
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
    """DL-54 review MINOR: an armed job whose condition keeps re-latching
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
