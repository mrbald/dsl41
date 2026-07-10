"""Oracle discrete-event interpreter trace tests (phase 7).

Normative spec: docs/ir-design.md ss7 (oracle interface, determinism,
non-goals) and every SEM entry in docs/autosys-semantics.md; the trace-test
index is dossier ss8 (T01..T34) and each test below cites its T-number plus
the SEM entry it pins. oracle.py's own module docstring pins the interpreter
decisions (Q2 zero-lookback anchor, Q3 abandon-vs-arm-and-wait, the SEM-33
closer-edge midpoint tie-break) that these tests exercise.

Every expected outcome here was verified empirically against the real oracle
before the assertion was written (CLAUDE.md: fidelity is tested, not
asserted). One test (test_sem33_box_variant_two_members_deferred_member_is_
dropped_by_premature_fold) pins the SEM-33/docstring-documented behavior
("box context stays RUNNING overnight") against an oracle.py code path that
does not actually deliver it in a multi-member box; it is marked
xfail(strict=True) with the repro and citation in its docstring -- see the
final report for the SUSPECTED SRC BUG writeup.

T03 (SEM-03, precedence) is out of scope for the oracle: precedence is
pinned at parse time (condition.lark / CONDITION_PRECEDENCE), never seen by
the interpreter, which only walks whatever Cond tree the parser produced.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.ir import lower_source
from dsl41.oracle import Event, EventKind, Oracle, OracleError

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def oracle(jil_text: str) -> Oracle:
    return Oracle(lower_source(jil_text))


def transitions(o: Oracle, job: str) -> list[str]:
    return [t.transition for t in o.trace() if t.job == job]


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
    o.feed(ev("STARTJOB", 1, job="consumer_n2"))  # n(p2) false: p2 RUNNING -> abandoned
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


def test_sem04_zero_lookback_midnight_anchor_pins_q2_default() -> None:
    """T04 (SEM-04) + PENDING Q2: this pins ORACLE_ZERO_LOOKBACK_ANCHOR ==
    "midnight" (oracle.py's documented default reading, not verified against
    a live instance -- Q2 is still open per dossier ss9). Success at 23:50,
    evaluated at 00:10 the next calendar day -> s(job, 0) does NOT fire
    (different .date()). Contrast: success and evaluation on the same
    calendar day -> fires. Both sides use the ON_HOLD/OFF_HOLD pattern so
    the trivial same-instant true (elapsed==0 always satisfies any lookback)
    does not mask the cross-midnight check."""
    text = (
        "insert_job: prod_zero\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_zero\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_zero, 0)\n"
    )
    cross_midnight = oracle(text)
    day1_2350 = datetime(2026, 6, 30, 23, 50)
    day2_0010 = datetime(2026, 7, 1, 0, 10)
    cross_midnight.feed(Event(at=day1_2350, kind="ON_HOLD", payload={"job": "cons_zero"}))
    cross_midnight.feed(
        Event(at=day1_2350, kind="STATUS", payload={"job": "prod_zero", "status": "SUCCESS"})
    )
    cross_midnight.feed(Event(at=day2_0010, kind="OFF_HOLD", payload={"job": "cons_zero"}))
    assert transitions(cross_midnight, "cons_zero") == ["ON_HOLD", "OFF_HOLD"]

    same_day = oracle(text)
    success_at = datetime(2026, 7, 1, 8, 0)
    eval_at = datetime(2026, 7, 1, 9, 0)
    same_day.feed(Event(at=success_at, kind="ON_HOLD", payload={"job": "cons_zero"}))
    same_day.feed(
        Event(at=success_at, kind="STATUS", payload={"job": "prod_zero", "status": "SUCCESS"})
    )
    same_day.feed(Event(at=eval_at, kind="OFF_HOLD", payload={"job": "cons_zero"}))
    assert transitions(same_day, "cons_zero") == [
        "ON_HOLD",
        "OFF_HOLD",
        "INACTIVE->STARTING",
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
    assert o.store.globals_["FLAG3"] == "go"
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
    [(1, "FAILURE"), (2, "SUCCESS"), (0, "SUCCESS")],
    ids=["carved-out-below-threshold", "at-threshold", "zero"],
)
def test_sem09b_fail_codes_carve_failures_out_of_the_threshold(
    exit_code: int, expected_status: str
) -> None:
    """T09b (SEM-09/DL-33): fail_codes marks explicit codes FAILURE even
    below the max_exit_success threshold; unmatched codes fall through to
    the threshold (Q7 default)."""
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


def test_sem09d_fail_codes_win_over_success_codes() -> None:
    """T09d (SEM-09/DL-33): a code in BOTH lists is FAILURE -- fail-wins is
    the Q7 default (a false FAILURE is loud, a false SUCCESS silently
    satisfies downstream latches)."""
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


# --------------------------------------------------------- 17. SEM-32 (Q3 pending default)


def test_sem32_scheduled_startjob_with_false_condition_is_abandoned() -> None:
    """T32 (SEM-32), PENDING Q3: a scheduled STARTJOB (script-injected at the
    moment start_times/start_mins would fire) whose condition is currently
    false is ABANDONED -- the oracle's documented default reading -- not
    queued to arm-and-wait for the condition to later become true. Q3 is
    still open (dossier ss9); if AutoSys's live behavior turns out to be
    arm-and-wait, this branch (and this test) changes."""
    text = (
        "insert_job: job32\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(never_true32)\n\n"
        "insert_job: never_true32\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="job32"))  # the scheduler's tick, condition false
    assert transitions(o, "job32") == []
    assert o.store.job["job32"].status == "INACTIVE"


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
        "T03/SEM-03 operator precedence (Q1 pending) is resolved entirely at"
        " parse time by condition.lark / CONDITION_PRECEDENCE; the oracle"
        " only ever sees the already-built Cond tree and has no precedence"
        " concept of its own to trace-test. See"
        " test_condition_grammar.py::test_precedence_modes_differ_where_expected"
        " for the live Q1 sentinel."
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
@settings(max_examples=100, deadline=None)
def test_hypothesis_oracle_determinism_legality_and_monotonicity(script: list[Event]) -> None:
    """Tier (c) fuzz (ir-design ss6): random small scripts of STATUS
    SUCCESS/FAILURE + SET_GLOBAL over a fixed 3-job catalog, monotone
    minutes. (a) determinism: two fresh oracles fed the same script produce
    identical traces. (b) every traced transition is one of the edges
    actually reachable in oracle.py given this event vocabulary. (c) traces
    are time-monotone."""
    trace1 = oracle(_DIAMOND3_JIL).run_script(script)
    trace2 = oracle(_DIAMOND3_JIL).run_script(script)
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
    the alarm fires iff no new run began by tick+offset -- here the start
    was abandoned (false condition), which is exactly the alarm's point --
    and never affects control flow."""
    text = (
        "insert_job: ms_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_start_times: +5\ncondition: s(ms_gate)\n\n"
        "insert_job: ms_gate\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: ms_dummy\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o = oracle(text)
    o.feed(ev("STARTJOB", 0, job="ms_job"))  # condition false -> abandoned (Q3)
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
