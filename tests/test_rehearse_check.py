"""`rehearse --check-cadence` (DL-182/DL-184): rehearse_check.py + its CLI
wiring in cli_run.py (`--check-cadence`, `--cadence-policy`,
`_emit_cadence_check`).

Normative spec: rehearse_check.py's own module docstring and the DL-184
decision-log entry. House style follows test_runner.py/test_runner_control.py:
inline `lower_source(text)` estates (no new tests/corpus files -- runner-side
convention), naive datetimes, CLI tests via `typer.testing.CliRunner` (the
lighter test_runner_control.py convention over subprocess).

Every expected outcome here was verified empirically against the real
rehearse_check.py before the assertion was written (CLAUDE.md: fidelity is
tested, not asserted) -- see the final report for anything that surprised us
or contradicted the design doc.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.conditions import StatusAtom, iter_atoms
from dsl41.derive import derive_graph
from dsl41.equiv import global_regions
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.rehearse_check import (
    FLAG_CASE_CEILING,
    CadenceCheckError,
    CadencePolicy,
    JobPolicy,
    case_fail_entry,
    check_adapter,
    genesis_truth,
    compare,
    expected_bounds,
    fail_sweep_producers,
    failure_exit,
    flag_cases,
    load_policy,
    play_once,
    render_text,
    run_fail_sweep,
    run_flag_sweep,
    scenario_budgets,
    scheduled_ticks,
    success_exit,
)
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import ZeroDelayCycleError

START = datetime(2026, 9, 1, 0, 0)
HORIZON = START + timedelta(hours=24)

cli_runner = CliRunner()

#: The smoke estate (DL-184 review): a scheduled job with two ticks/day, a
#: single-tick sibling, a condition-only chain, an unqualified OR join (the
#: DL-180 multi-fire shape), a box + member, and a file watcher. Reused
#: verbatim across the unit- and CLI-level tests below.
SMOKE_JIL = """
insert_job: cs_a
job_type: c
command: /usr/bin/true
machine: m1
date_conditions: 1
days_of_week: all
start_times: "01:00, 13:00"

insert_job: cs_b
job_type: c
command: /usr/bin/true
machine: m1
date_conditions: 1
days_of_week: all
start_times: "02:00"

insert_job: cs_chain
job_type: c
command: /usr/bin/true
machine: m1
condition: s(cs_a)

insert_job: cs_either
job_type: c
command: /usr/bin/true
machine: m1
condition: s(cs_a) | s(cs_b)

insert_job: cs_box
job_type: b
date_conditions: 1
days_of_week: all
start_times: "03:00"

insert_job: cs_member
job_type: c
command: /usr/bin/true
machine: m1
box_name: cs_box

insert_job: cs_watch
job_type: f
machine: m1
watch_file: /tmp/cs.landing
"""

#: A two-job condition SCC (DL-184): both wake each other, no finite bound.
CYCLE_JIL = (
    "insert_job: cy_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cy_b)\n\n"
    "insert_job: cy_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cy_a)\n"
)

#: The DL-180 recovery, smoke-verified end to end (DL-184 item 8): fa/fb wake
#: an unqualified AND-plus-global join. Set FLAG='1' at start with no reset
#: and the join fires 3 times over 48h against a bound of 2 -- the stale
#: latch double fire the flag sweep exists to catch.
FLAG_SMOKE_JIL = (
    "insert_job: fa\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
    "insert_job: fb\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n\n'
    "insert_job: flag_join\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: s(fa) & s(fb) & v(FLAG) = 1\n"
)


def _bounds(text: str, **kw: object) -> dict:
    """One catalog's expected_bounds over [START, HORIZON], the shared setup
    every expected_bounds test below repeats."""
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    return expected_bounds(catalog, graph, ticks, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------- global_regions


def test_global_regions_numeric_literal_has_numeric_and_string_cutpoints() -> None:
    """v(F) = 1: numeric cutpoints v-1/v/v+1, string cutpoints ""/lit/lit+NUL,
    and the unset None (DL-184 item 1)."""
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(F) = 1\n"
    )
    conds = [
        job.sem.condition.cond for job in catalog.jobs.values() if job.sem.condition is not None
    ]
    regions = global_regions(conds)
    assert set(regions["F"]) == {"", "0", "1", "1\x00", "2", None}


def test_global_regions_non_numeric_literal_carries_no_numeric_cutpoints() -> None:
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(G) = go\n"
    )
    conds = [
        job.sem.condition.cond for job in catalog.jobs.values() if job.sem.condition is not None
    ]
    regions = global_regions(conds)
    assert set(regions["G"]) == {"", "go", "go\x00", None}


# ------------------------------------------------------------------ flag_cases


def test_flag_cases_one_global_one_literal_is_ten_deterministic_cases_no_none() -> None:
    """v(F) = 1: 5 non-None region values x set/reset == 10 cases, in region
    order, and no case carries the unset None -- SET_GLOBAL cannot produce it
    and the happy path already plays it (DL-184). Cases are whole
    ASSIGNMENTS since the slice review's compound-gate blocker; a
    single-global component degenerates to one-entry assignments."""
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(F) = 1\n"
    )
    cases, uncovered = flag_cases(catalog, derive_graph(catalog))
    assert uncovered == []
    assert cases == [
        ({"F": ""}, False),
        ({"F": ""}, True),
        ({"F": "0"}, False),
        ({"F": "0"}, True),
        ({"F": "1"}, False),
        ({"F": "1"}, True),
        ({"F": "1\x00"}, False),
        ({"F": "1\x00"}, True),
        ({"F": "2"}, False),
        ({"F": "2"}, True),
    ]
    assert all(None not in assignment.values() for assignment, _reset in cases)


def test_flag_cases_ceiling_drops_later_globals_whole_and_caps_the_total() -> None:
    """8 globals x 5 OR'd numeric literals each: G1/G2's regions alone reach
    52 cases (inside the 64 ceiling); G3..G8 would push past it and are
    dropped WHOLE, in sorted order, never partially (DL-184)."""
    text = "".join(
        f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"condition: {' | '.join(f'v(G{i}) = {v}' for v in range(1, 6))}\n\n"
        for i in range(1, 9)
    )
    catalog = lower_source(text)
    cases, uncovered = flag_cases(catalog, derive_graph(catalog))
    assert len(cases) <= FLAG_CASE_CEILING
    assert uncovered == ["G3", "G4", "G5", "G6", "G7", "G8"]
    by_global: dict[str, int] = {}
    for assignment, _reset in cases:
        (gname,) = assignment  # single-global components: one-entry assignments
        by_global[gname] = by_global.get(gname, 0) + 1
    assert by_global == {"G1": 26, "G2": 26}  # earlier globals keep the full case list


# ------------------------------------------------------------- scheduled_ticks


def test_scheduled_ticks_two_per_day_scales_with_the_horizon() -> None:
    """Two start_times/day: 2 ticks over 24h, 4 over 48h (DL-184 item 1)."""
    text = (
        "insert_job: tk\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n'
    )
    catalog = lower_source(text)
    assert scheduled_ticks(catalog, start=START, horizon=START + timedelta(hours=24)) == {"tk": 2}
    assert scheduled_ticks(catalog, start=START, horizon=START + timedelta(hours=48)) == {"tk": 4}


def test_scheduled_ticks_tick_at_start_and_at_horizon_are_both_inclusive() -> None:
    """A single midnight tick, start at midnight, 24h horizon: the tick AT
    start and the tick AT the horizon both count -- 2, not 1 (DL-184)."""
    text = (
        "insert_job: midnight\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "00:00"\n'
    )
    catalog = lower_source(text)
    ticks = scheduled_ticks(catalog, start=START, horizon=START + timedelta(hours=24))
    assert ticks == {"midnight": 2}


# ------------------------------------------------------------- expected_bounds


def test_expected_bounds_scheduled_unboxed_job_bound_is_its_tick_count() -> None:
    catalog = lower_source(
        "insert_job: sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00"\n'
    )
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    bounds = expected_bounds(catalog, graph, ticks)
    assert bounds["sched"].expected == 1
    assert "1" in bounds["sched"].provenance


def test_expected_bounds_condition_only_chain_bound_is_the_producer_bound() -> None:
    """s(A): bound(chain) == bound(A)."""
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: chain\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n"
    )
    bounds = _bounds(text)
    assert bounds["prod"].expected == 2
    assert bounds["chain"].expected == 2


def test_expected_bounds_or_join_bound_is_max_not_sum() -> None:
    """s(A)|s(B): bound is max(A, B), never A+B -- the DL-182 default."""
    text = (
        "insert_job: prod_a\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: prod_b\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n\n'
        "insert_job: either\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(prod_a) | s(prod_b)\n"
    )
    bounds = _bounds(text)
    assert bounds["prod_a"].expected == 2
    assert bounds["prod_b"].expected == 1
    assert bounds["either"].expected == 2  # max(2, 1), not 3


def test_expected_bounds_bare_notrunning_guard_counts_as_a_wake_source() -> None:
    """A bare n(guard) consumer: guard's bound feeds the max (lint.py L021's
    own wake-source reading, DL-184). This test found the first draft
    reading only start-gate edges -- mutex classification keeps bare n()
    out of the edge set, so the wake walk now reads graph.bare_notrunning
    too, exactly as rule_l021 does."""
    text = (
        "insert_job: guard\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: watcher\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(guard)\n"
    )
    bounds = _bounds(text)
    assert bounds["guard"].expected == 2
    assert bounds["watcher"].expected == 2  # guard's bound, per L021's reading


def test_expected_bounds_unscheduled_box_member_bound_is_the_box_bound() -> None:
    text = (
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
    )
    bounds = _bounds(text)
    assert bounds["bx"].expected == 2
    assert bounds["mem"].expected == 2
    assert bounds["mem"].provenance == "member: box bound"


def test_expected_bounds_scheduled_member_bound_is_min_when_own_ticks_are_fewer() -> None:
    """SEM-10 min-compose: a scheduled member's bound is min(box, own ticks),
    never summed -- summing would hide the double run the check hunts."""
    text = (
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00"\n'
    )
    bounds = _bounds(text)
    assert bounds["bx"].expected == 2
    assert bounds["mem"].expected == 1  # min(2, 1)
    assert bounds["mem"].provenance == "member: min(box bound, own ticks)"


def test_expected_bounds_scheduled_member_bound_is_min_when_the_box_bound_is_fewer() -> None:
    text = (
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00, 09:00, 13:00"\n'
    )
    bounds = _bounds(text)
    assert bounds["bx"].expected == 1
    assert bounds["mem"].expected == 1  # min(1, 3)


def test_expected_bounds_injected_start_adds_one_to_scheduled_and_condition_only_jobs() -> None:
    text = (
        "insert_job: sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(prod)\n"
    )
    bounds = _bounds(text, injected_start={"sched": 1, "cons": 1})
    assert bounds["sched"].expected == 2  # 1 own tick + 1 injected
    assert bounds["prod"].expected == 0  # unscheduled, no wake sources, no injection
    assert bounds["cons"].expected == 1  # max(prod=0) + 1 injected
    assert "injected" in bounds["sched"].provenance
    assert "injected" in bounds["cons"].provenance


def test_expected_bounds_injected_force_adds_after_the_member_min() -> None:
    """A FORCE_STARTJOB bypasses the gates the min models, so its budget is
    added AFTER min(box bound, own ticks), never folded into it."""
    text = (
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
    )
    bounds = _bounds(text, injected_force={"mem": 1})
    assert bounds["bx"].expected == 1
    assert bounds["mem"].expected == 2  # box bound (1) + 1 forced, after the min


def test_expected_bounds_policy_max_runs_pins_the_job_and_propagates_to_its_consumer() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(prod)\n"
    )
    policy = CadencePolicy(
        schema_version=1, policies={"prod": JobPolicy(max_runs=9, reason="alert bound")}
    )
    bounds = _bounds(text, policy=policy)
    assert bounds["prod"].expected == 9
    assert bounds["prod"].provenance == "policy: alert bound"
    assert bounds["cons"].expected == 9  # the pinned value propagates through the max


def test_expected_bounds_policy_null_max_runs_is_unchecked_with_the_reason() -> None:
    text = "insert_job: flaky\njob_type: c\ncommand: x\nmachine: m1\n"
    policy = CadencePolicy(
        schema_version=1, policies={"flaky": JobPolicy(max_runs=None, reason="known flaky")}
    )
    bounds = _bounds(text, policy=policy)
    assert bounds["flaky"].expected is None
    assert bounds["flaky"].provenance == "policy: known flaky"


def test_expected_bounds_a_parked_file_watcher_infects_its_start_gate_consumer() -> None:
    text = (
        "insert_job: fw\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n\n"
        "insert_job: cons\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(fw)\n"
    )
    bounds = _bounds(text, parked=["fw"])
    assert bounds["fw"].expected is None
    assert bounds["cons"].expected is None
    assert "wake source" in bounds["cons"].provenance
    assert "unchecked" in bounds["cons"].provenance


def test_expected_bounds_an_unchecked_box_infects_its_member() -> None:
    """A global-gated box (unchecked on its own) absorbs its member too."""
    text = (
        "insert_job: bx\njob_type: b\ncondition: v(FLAG) = 1\n\n"
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
    )
    bounds = _bounds(text)
    assert bounds["bx"].expected is None
    assert bounds["mem"].expected is None
    assert "box" in bounds["mem"].provenance
    assert "unchecked" in bounds["mem"].provenance


def test_expected_bounds_global_gated_consumer_is_unchecked() -> None:
    text = "insert_job: gated\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = 1\n"
    bounds = _bounds(text)
    assert bounds["gated"].expected is None
    assert "global-gated" in bounds["gated"].provenance


def test_expected_bounds_wake_reasons_spare_jobs_whose_bound_never_reads_wakes() -> None:
    """Review find: a scheduled job's condition can only release an
    armed tick (SEM-30/31) and a member runs at most once per box execution
    (SEM-10), so a parked-FW gate or a global gate on THOSE jobs must not
    blank them -- their bounds are sound regardless of the wake sources."""
    text = (
        "insert_job: fw\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n\n"
        "insert_job: sched\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(fw)\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00"\n\n'
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "06:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bx\n"
        "condition: v(FLAG) = 1\n"
    )
    bounds = _bounds(text, parked=["fw"])
    assert bounds["sched"].expected == 1  # own ticks, not blanked by the FW
    assert bounds["mem"].expected == 1  # box bound, not blanked by the global


def test_expected_bounds_box_success_global_does_not_blank_the_box() -> None:
    """Review find: a v() inside box_success folds a running box and
    starts nothing -- only the START condition's globals mark unchecked."""
    text = (
        "insert_job: bx\njob_type: b\nbox_success: v(OK) = 1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "06:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
    )
    bounds = _bounds(text)
    assert bounds["bx"].expected == 1
    assert bounds["mem"].expected == 1


def test_expected_bounds_cross_instance_wake_is_unchecked() -> None:
    """s(job^INST) with a declared xinst: unchecked (DL-162a)."""
    text = (
        "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
        "insert_job: xi\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(remote^PRD)\n"
    )
    bounds = _bounds(text)
    assert bounds["xi"].expected is None
    assert "cross-instance" in bounds["xi"].provenance
    assert "DL-162a" in bounds["xi"].provenance


def test_expected_bounds_condition_cycle_members_are_unchecked_and_the_call_returns() -> None:
    """A condition SCC (a: s(b), b: s(a)) has no finite bound: both members
    go unchecked, and expected_bounds returns rather than spinning -- the
    fixpoint would otherwise diverge by +1 per trip around the cycle."""
    bounds = _bounds(CYCLE_JIL)
    assert bounds["cy_a"].expected is None
    assert bounds["cy_b"].expected is None
    assert "wake cycle" in bounds["cy_a"].provenance
    assert "wake cycle" in bounds["cy_b"].provenance


def test_expected_bounds_mutual_notrunning_pair_is_a_wake_cycle() -> None:
    """M07 mutex idlers (a: n(b), b: n(a)) form a wake cycle graph.cycles
    cannot see -- mutex refs are not edges -- so the SCC guard runs over
    the wake adjacency instead (DL-184). Both go unchecked; without the
    guard an injected +1 diverges the fixpoint exactly like the s() cycle."""
    text = (
        "insert_job: mx_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx_b)\n\n"
        "insert_job: mx_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx_a)\n"
    )
    bounds = _bounds(text, injected_start={"mx_a": 1})
    assert bounds["mx_a"].expected is None
    assert bounds["mx_b"].expected is None
    assert "wake cycle" in bounds["mx_a"].provenance


def test_expected_bounds_condition_cycle_terminates_with_an_injected_start() -> None:
    """Regression pin: during implementation, an injected +1 budget on a
    cycle member fed back around the SCC and diverged the fixpoint. The
    guard is the SCC pass plus the convergence cap (which raises, never
    hangs), so returning at all with both members unchecked is the pin --
    no wall-clock assertion (flaky-prone, and it guarded nothing)."""
    bounds = _bounds(CYCLE_JIL, injected_start={"cy_a": 1})
    assert bounds["cy_a"].expected is None
    assert bounds["cy_b"].expected is None


# ------------------------------------------- expected_bounds: scripted_globals


def test_expected_bounds_scripted_globals_lifts_a_pure_global_gate() -> None:
    """v(F) = 1 alone: scripting F CHECKS the consumer, budgeted 0 wakes + 1
    satisfying set; a falsifying scripted value budgets 0 (DL-184 item 4)."""
    text = "insert_job: gated\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(F) = 1\n"
    satisfying = _bounds(text, scripted_globals=[("F", "1", True)])
    assert satisfying["gated"].expected == 1
    falsifying = _bounds(text, scripted_globals=[("F", "0", True)])
    assert falsifying["gated"].expected == 0


def test_expected_bounds_scripted_globals_at_start_set_is_zero_for_latch_gated_consumer() -> None:
    """s(A) & v(F) = 1, A scheduled twice over the window: an at-start
    SET_GLOBAL budgets 0 extra for a latch-gated consumer -- its latches
    are false before the first tick, so the set cannot fire it; a
    mid-window set (the reset variant) budgets +1 (DL-184 item 5, the
    normative rule in expected_bounds' own docstring)."""
    text = (
        "insert_job: A\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(A) & v(F) = 1\n"
    )
    at_start = _bounds(text, scripted_globals=[("F", "1", True)])
    assert at_start["cons"].expected == 2  # 2 (own wake bound) + 0
    mid_window = _bounds(text, scripted_globals=[("F", "1", False)])
    assert mid_window["cons"].expected == 3  # 2 + 1


def test_expected_bounds_scripted_globals_partial_scripting_stays_unchecked() -> None:
    """A consumer referencing F and H: scripting only F leaves it
    global-gated -- every referenced global must be scripted (DL-184 item 6)."""
    text = (
        "insert_job: gated\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(F) = 1 & v(H) = 2\n"
    )
    bounds = _bounds(text, scripted_globals=[("F", "1", True)])
    assert bounds["gated"].expected is None
    assert "global-gated" in bounds["gated"].provenance


def test_expected_bounds_scripted_globals_ordered_operator_is_checked_per_literal() -> None:
    """v(F) > 5 on a latch-gated consumer: the region value 6 satisfies the
    ordered comparison and budgets +1; 4 does not and budgets +0 -- the
    literal-by-literal miss the region fix exists for (DL-184 item 7)."""
    text = (
        "insert_job: A\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(A) & v(F) > 5\n"
    )
    satisfying = _bounds(text, scripted_globals=[("F", "6", False)])
    assert satisfying["cons"].expected == 2  # 1 (own wake bound) + 1
    not_satisfying = _bounds(text, scripted_globals=[("F", "4", False)])
    assert not_satisfying["cons"].expected == 1  # 1 + 0


# ------------------------------------------------------------------ success_exit


def test_success_exit_plain_job_is_zero() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    assert success_exit(catalog.jobs["j"]) == 0


def test_success_exit_fail_codes_covering_zero_gives_the_next_code() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0\n")
    assert success_exit(catalog.jobs["j"]) == 1


def test_success_exit_fail_codes_covering_the_whole_vocabulary_gives_none() -> None:
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0-255\n"
    )
    assert success_exit(catalog.jobs["j"]) is None


# ----------------------------------------------------------------- check_adapter


def test_check_adapter_auto_parks_unscripted_file_watchers_not_scripted_ones() -> None:
    text = (
        "insert_job: unscripted_fw\njob_type: f\nmachine: m1\nwatch_file: /tmp/a\n\n"
        "insert_job: scripted_fw\njob_type: f\nmachine: m1\nwatch_file: /tmp/b\n"
    )
    catalog = lower_source(text)
    base = FakeAdapter({("scripted_fw", 1): (0.0, 0)}, default=(0.0, 0))
    adapter, parked_fw, _ = check_adapter(catalog, base)
    assert parked_fw == frozenset({"unscripted_fw"})
    assert "unscripted_fw" in adapter.park
    assert "scripted_fw" not in adapter.park


def test_check_adapter_box_jobs_get_no_job_default() -> None:
    catalog = lower_source("insert_job: bx\njob_type: b\n")
    adapter, _, _ = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    assert "bx" not in adapter.job_default


def test_check_adapter_default_null_suppresses_synthesis_entirely() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    adapter, _, _ = check_adapter(catalog, FakeAdapter({}, default=None))
    assert adapter.job_default == {}


def test_check_adapter_fail_codes_zero_zero_synthesizes_exit_one() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0\n")
    adapter, _, no_success = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    assert adapter.job_default["j"] == (0.0, 1)
    assert "j" not in no_success


def test_check_adapter_fail_codes_covering_everything_parks_and_marks_no_success() -> None:
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0-255\n"
    )
    adapter, _, no_success = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    assert no_success == frozenset({"j"})
    assert adapter.job_default["j"] is None  # park: it cannot complete SUCCESS


def test_check_adapter_preserves_the_scenario_default_duration() -> None:
    """Review find (blocker): DL-184 authorizes synthesizing the EXIT, never the
    duration -- a scenario default of [1800, 0] must still take 1800s per
    run, or the check measures a different estate (durations drive n()
    windows, resources and box overlap)."""
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0\n")
    adapter, _, _ = check_adapter(catalog, FakeAdapter({}, default=(1800.0, 0)))
    assert adapter.job_default["j"] == (1800.0, 1)


# --------------------------------------------------------------- scenario_budgets


def test_scenario_budgets_counts_startjob_and_force_startjob_per_target() -> None:
    events = [
        Event(at=START, kind="STARTJOB", payload={"job": "a"}),
        Event(at=START, kind="STARTJOB", payload={"job": "a"}),
        Event(at=START, kind="FORCE_STARTJOB", payload={"job": "b"}),
    ]
    injected_start, injected_force = scenario_budgets(events)
    assert injected_start == {"a": 2}
    assert injected_force == {"b": 1}


def test_scenario_budgets_set_global_passes_with_no_budget() -> None:
    events = [Event(at=START, kind="SET_GLOBAL", payload={"name": "G", "value": "1"})]
    injected_start, injected_force = scenario_budgets(events)
    assert injected_start == {}
    assert injected_force == {}


def test_scenario_budgets_status_refuses_naming_the_allowlist() -> None:
    events = [Event(at=START, kind="STATUS", payload={"job": "a", "status": "SUCCESS"})]
    with pytest.raises(CadenceCheckError, match="STATUS.*allowlist"):
        scenario_budgets(events)


def test_scenario_budgets_on_ice_refuses_naming_the_allowlist() -> None:
    events = [Event(at=START, kind="ON_ICE", payload={"job": "a"})]
    with pytest.raises(CadenceCheckError, match="ON_ICE.*allowlist"):
        scenario_budgets(events)


# --------------------------------------------------------------------- load_policy


def _catalog_one_job() -> object:
    return lower_source("insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n")


def test_load_policy_valid_file_parses(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"schema_version": 1, "policies": {"j1": {"max_runs": 3, "reason": "x"}}})
    )
    policy = load_policy(path, catalog)
    assert policy.policies["j1"].max_runs == 3
    assert policy.policies["j1"].reason == "x"


def test_load_policy_refuses_an_unknown_job_name(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"schema_version": 1, "policies": {"nope": {"max_runs": 3, "reason": "x"}}})
    )
    with pytest.raises(CadenceCheckError, match="does not define"):
        load_policy(path, catalog)


def test_load_policy_refuses_an_extra_top_level_key(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 1, "policies": {}, "bogus": 1}))
    with pytest.raises(CadenceCheckError, match="bogus"):
        load_policy(path, catalog)


def test_load_policy_refuses_an_extra_per_job_key(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "policies": {"j1": {"max_runs": 3, "reason": "x", "bogus": 1}}}
        )
    )
    with pytest.raises(CadenceCheckError, match="bogus"):
        load_policy(path, catalog)


def test_load_policy_refuses_a_missing_reason(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 1, "policies": {"j1": {"max_runs": 3}}}))
    with pytest.raises(CadenceCheckError, match="reason"):
        load_policy(path, catalog)


def test_load_policy_refuses_an_empty_reason(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"schema_version": 1, "policies": {"j1": {"max_runs": 3, "reason": ""}}})
    )
    with pytest.raises(CadenceCheckError, match="reason"):
        load_policy(path, catalog)


def test_load_policy_refuses_schema_version_2(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 2, "policies": {}}))
    with pytest.raises(CadenceCheckError, match="schema_version"):
        load_policy(path, catalog)


def test_load_policy_refuses_non_json_bytes(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    path = tmp_path / "policy.json"
    path.write_bytes(b"not json at all {{{")
    with pytest.raises(CadenceCheckError, match="not JSON"):
        load_policy(path, catalog)


def test_load_policy_refuses_a_missing_file(tmp_path: Path) -> None:
    catalog = _catalog_one_job()
    with pytest.raises(CadenceCheckError):
        load_policy(tmp_path / "nowhere.json", catalog)


# --------------------------------------------------------------- compare/render_text


def test_compare_over_run_deviates_with_a_multi_fire_finding() -> None:
    text = "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n"
    bounds = _bounds(
        text,
        policy=CadencePolicy(
            schema_version=1, policies={"prod": JobPolicy(max_runs=2, reason="bound")}
        ),
    )
    catalog = lower_source(text)
    check = compare(catalog, bounds, {"prod": 5}, start=START, horizon=HORIZON)
    assert check.jobs["prod"].deviation is True
    (finding,) = check.findings
    assert finding.kind == "multi_fire"
    assert finding.jobs == ["prod"]


def test_compare_under_run_is_not_a_deviation() -> None:
    """An f/d/e-gated consumer legitimately observes 0 on the happy path
    (DL-184): observed < expected never deviates."""
    text = "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n"
    bounds = _bounds(
        text,
        policy=CadencePolicy(
            schema_version=1, policies={"prod": JobPolicy(max_runs=5, reason="bound")}
        ),
    )
    catalog = lower_source(text)
    check = compare(catalog, bounds, {"prod": 0}, start=START, horizon=HORIZON)
    assert check.jobs["prod"].deviation is False
    assert check.findings == []


def test_compare_zero_delay_cycle_finding_is_reported_first() -> None:
    text = "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n"
    bounds = _bounds(
        text,
        policy=CadencePolicy(
            schema_version=1, policies={"prod": JobPolicy(max_runs=1, reason="bound")}
        ),
    )
    catalog = lower_source(text)
    cycle = ZeroDelayCycleError("spin", instant=START + timedelta(hours=1), jobs=("cy_a", "cy_b"))
    check = compare(catalog, bounds, {"prod": 9}, start=START, horizon=HORIZON, cycle=cycle)
    assert [f.kind for f in check.findings] == ["zero_delay_cycle", "multi_fire"]
    assert check.findings[0].jobs == ["cy_a", "cy_b"]


def test_render_text_emits_the_table_the_coverage_line_and_the_note() -> None:
    text = "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n"
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    bounds = expected_bounds(catalog, graph, ticks)
    check = compare(catalog, bounds, {"prod": 0}, start=START, horizon=HORIZON)
    lines = render_text(check)
    assert lines[0] == "-- cadence check: happy --"
    assert any(line.startswith("prod") for line in lines)
    # the coverage line separates checked from exercised (review find:
    # "checked 5" over an estate where nothing ran misled)
    assert any(
        line.startswith("checked ") and "ran 0 of 1" in line and "findings" in line
        for line in lines
    )
    assert any("counts assume no retries" in line for line in lines)


# ---------------------------------------------------------------------- play_once


def test_play_once_smoke_estate_run_counts() -> None:
    """The DL-180 OR multi-fire, caught dynamically: cs_either's 3 runs
    exceed its bound of 2 -- the check's intended true positive."""
    catalog = lower_source(SMOKE_JIL)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, _parked_fw, _no_success = check_adapter(catalog, base)
    result = play_once(catalog, start=START, horizon=HORIZON, adapter=adapter)
    assert result.runs == {
        "cs_a": 2,
        "cs_b": 1,
        "cs_box": 1,
        "cs_chain": 2,
        "cs_either": 3,
        "cs_member": 1,
        "cs_watch": 0,
    }
    assert result.cycle is None


# The play_once spin conversion (a FORCE_STARTJOB kick on CYCLE_JIL) is
# pinned inside the fail-sweep guard test below -- run_fail_sweep can only
# see result.cycle because play_once caught it, so one spin covers both
# (this slice's review: two ~13s spins were 98% of the file's runtime).


# --------------------------------------------------------------------- CLI: rehearse


def _write_smoke(tmp_path: Path) -> Path:
    jil = tmp_path / "smoke.jil"
    jil.write_text(SMOKE_JIL, encoding="utf-8")
    return jil


def _write_flag_smoke(tmp_path: Path) -> Path:
    jil = tmp_path / "flag_smoke.jil"
    jil.write_text(FLAG_SMOKE_JIL, encoding="utf-8")
    return jil


def test_cli_check_cadence_text_deviation_exits_3(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--format",
            "text",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "cs_either" in result.output
    assert "DEVIATION" in result.output
    assert "finding [multi_fire] cs_either:" in result.output


def test_cli_check_cadence_policy_declares_the_exception_and_exits_0(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policies": {"cs_either": {"max_runs": 3, "reason": "OR join documented"}},
            }
        )
    )
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--cadence-policy",
            str(policy),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "policy: OR join documented" in result.output


def test_cli_cadence_policy_without_check_cadence_exits_2(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema_version": 1, "policies": {}}))
    result = cli_runner.invoke(app, ["rehearse", str(jil), "--cadence-policy", str(policy)])
    assert result.exit_code == 2
    assert "requires" in result.output
    assert "--check-cadence" in result.output


def test_cli_check_cadence_status_scenario_event_refuses_exits_2(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "at": "2026-09-01T00:30:00",
                        "kind": "STATUS",
                        "payload": {"job": "cs_a", "status": "SUCCESS"},
                    }
                ]
            }
        )
    )
    result = cli_runner.invoke(
        app, ["rehearse", str(jil), "--scenario", str(scenario), "--check-cadence"]
    )
    assert result.exit_code == 2
    assert "allowlist" in result.output


def test_cli_check_cadence_format_json_carries_legacy_shape_and_cadence_check(
    tmp_path: Path,
) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 3, result.output
    doc = json.loads(result.output)
    assert set(doc) == {"trace", "jobs", "cadence_check"}
    assert doc["jobs"]["cs_a"] == {"runs": 2, "final_status": "SUCCESS"}  # legacy shape, unchanged
    cc = doc["cadence_check"]
    assert cc["schema_version"] == 1
    assert cc["jobs"]["cs_either"] == {
        "expected": 2,
        "provenance": "max over wake sources",
        "observed": 3,
        "deviation": True,
    }
    assert cc["findings"][0]["kind"] == "multi_fire"


def test_cli_check_cadence_format_summary_is_trace_lines_then_the_table(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--format",
            "summary",
        ],
    )
    assert result.exit_code == 3, result.output
    lines = result.output.splitlines()
    table_start = next(i for i, line in enumerate(lines) if line.startswith("-- cadence check"))
    assert table_start > 0  # trace lines precede the table
    assert "->" in lines[0]  # the first line is a trace transition, not a table row


def test_cli_plain_rehearse_output_is_unchanged_by_the_cadence_check_refactor(
    tmp_path: Path,
) -> None:
    """Regression guard on the `_emit_rehearsal` refactor (DL-184): with no
    --check-cadence flag, text is trace lines only and --format summary
    keeps its own header, exactly as before this feature existed."""
    jil = _write_smoke(tmp_path)
    text_result = cli_runner.invoke(
        app, ["rehearse", str(jil), "--start", "2026-09-01T00:00:00", "--hours", "24"]
    )
    assert text_result.exit_code == 0, text_result.output
    assert "-- cadence check" not in text_result.output
    assert all(
        line == "" or "->" in line or "[" in line for line in text_result.output.splitlines()
    )

    summary_result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--format",
            "summary",
        ],
    )
    assert summary_result.exit_code == 0, summary_result.output
    assert "-- summary: runs per job --" in summary_result.output
    assert "cs_a runs=2 final=SUCCESS" in summary_result.output


# --------------------------------------------------------------- failure_exit


def test_failure_exit_plain_job_is_one() -> None:
    """max_exit_success 0: exit 0 is SUCCESS, exit 1 is the smallest FAILURE."""
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    assert failure_exit(catalog.jobs["j"]) == 1


def test_failure_exit_fail_codes_covering_zero_gives_zero() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: 0\n")
    assert failure_exit(catalog.jobs["j"]) == 0


def test_failure_exit_success_codes_covering_the_whole_vocabulary_gives_none() -> None:
    """Every code in 0..255 is a SUCCESS: a fail sweep cannot fail this
    producer through its exit -- inconclusive (DL-184)."""
    catalog = lower_source(
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nsuccess_codes: 0-255\n"
    )
    assert failure_exit(catalog.jobs["j"]) is None


# ----------------------------------------------------------- fail_sweep_producers


def test_fail_sweep_producers_are_distinct_and_sorted() -> None:
    text = (
        "insert_job: zprod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: aprod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons1\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(zprod)\n\n"
        "insert_job: cons2\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(zprod) | s(aprod)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    assert fail_sweep_producers(catalog, graph) == ["aprod", "zprod"]


def test_fail_sweep_producers_bare_notrunning_target_is_not_a_producer() -> None:
    """A bare n() target is a mutex ref, not failure consumption (DL-181):
    it must not appear in the fail sweep's case list."""
    text = (
        "insert_job: guard\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: watcher\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(guard)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    assert fail_sweep_producers(catalog, graph) == []


def test_fail_sweep_producers_cross_instance_producer_contributes_nothing() -> None:
    text = (
        "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
        "insert_job: xi\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(remote^PRD)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    assert fail_sweep_producers(catalog, graph) == []


def test_fail_sweep_producers_f_and_d_gated_producer_is_included() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: consf\njob_type: c\ncommand: x\nmachine: m1\ncondition: f(prod)\n\n"
        "insert_job: consd\njob_type: c\ncommand: x\nmachine: m1\ncondition: d(prod)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    assert fail_sweep_producers(catalog, graph) == ["prod"]


# --------------------------------------------------------------- run_fail_sweep


def test_run_fail_sweep_outcomes_box_parked_retries_no_fail_exit_and_ran() -> None:
    """One replay per producer, five distinct outcomes (DL-184): a BOX has no
    adapter; a parked FW never completes; n_retrys > 0 is unmodeled (DL-53);
    an all-SUCCESS exit vocabulary cannot be failed through the exit; a plain
    producer runs."""
    text = (
        'insert_job: bx\njob_type: b\ndate_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n\n"
        "insert_job: cons_bx\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(bx)\n\n"
        "insert_job: fw\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n\n"
        "insert_job: cons_fw\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(fw)\n\n"
        "insert_job: retryer\njob_type: c\ncommand: x\nmachine: m1\nn_retrys: 2\n\n"
        "insert_job: cons_retryer\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(retryer)\n\n"
        "insert_job: unfailable\njob_type: c\ncommand: x\nmachine: m1\nsuccess_codes: 0-255\n\n"
        "insert_job: cons_unfailable\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(unfailable)\n\n"
        "insert_job: plain\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "04:00"\n\n'
        "insert_job: cons_plain\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(plain)\n\n"
        "insert_job: unreached\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_unreached\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(unreached)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    bounds = expected_bounds(catalog, graph, ticks, parked=parked_fw, no_success_exit=no_success)
    producers = fail_sweep_producers(catalog, graph)
    assert producers == ["bx", "fw", "plain", "retryer", "unfailable", "unreached"]
    baseline = {name: 0 for name in catalog.jobs}
    baseline["plain"] = 1  # the one producer the happy path actually reached
    cases, findings = run_fail_sweep(
        catalog,
        baseline,
        bounds,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=producers,
        parked=parked_fw,
    )
    outcomes = {case.producer: case.outcome for case in cases}
    assert outcomes == {
        "bx": "skipped_box",
        "fw": "skipped_parked",
        "plain": "ran",
        "retryer": "inconclusive_retries",
        "unfailable": "inconclusive_no_fail_exit",
        "unreached": "inconclusive_not_reached",  # zero baseline runs: never fires
    }
    assert findings == []


def test_run_fail_sweep_suppresses_the_release_consumer_and_wakes_the_failure_consumer() -> None:
    """P scheduled twice/day; C (s(P)) is the L022 story dynamically caught:
    P's first run fails, C's second start still fires, so the fail:P case
    suppresses C by exactly 1 -- names are examples, the dict is
    positive-only.

    C2 (f(P), ALSO scheduled with its own start_time) is the strand static
    L022 cannot see for scheduled consumers: it runs 0 in the baseline and
    1 in the fail:P case (a released run, not a suppressed one -- negative
    diffs are dropped from `suppressed`), and its bound (own ticks == 1)
    covers it, so this is not a deviation either."""
    text = (
        "insert_job: P\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: C\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(P)\n\n"
        "insert_job: C2\njob_type: c\ncommand: z\nmachine: m1\ncondition: f(P)\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00"\n'
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    bounds = expected_bounds(catalog, graph, ticks, parked=parked_fw, no_success_exit=no_success)
    assert bounds["C2"].expected == 1
    assert bounds["C2"].provenance == "own ticks (1)"
    baseline = play_once(catalog, start=START, horizon=HORIZON, adapter=adapter)
    assert baseline.runs == {"P": 2, "C": 2, "C2": 0}
    producers = fail_sweep_producers(catalog, graph)
    assert producers == ["P"]
    cases, findings = run_fail_sweep(
        catalog,
        baseline.runs,
        bounds,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=producers,
        parked=parked_fw,
    )
    (case,) = cases
    assert case.outcome == "ran"
    assert case.suppressed == {"C": 1}  # C2's release (0 -> 1) is a negative
    assert findings == []  # diff and never appears here

    # Observe C2's own run count directly: run_fail_sweep's public result
    # cannot show it (a released run has no positive diff to report), so
    # build the SAME case adapter run_fail_sweep builds internally and play
    # it once more (DL-184 mechanics: the sweep entry overrides the key).
    fail_code = failure_exit(catalog.jobs["P"])
    assert fail_code is not None
    script = dict(adapter.script)
    script[("P", 1)] = (0.0, fail_code)
    case_adapter = FakeAdapter(
        script, default=adapter.default, park=adapter.park, job_default=adapter.job_default
    )
    result = play_once(catalog, start=START, horizon=HORIZON, adapter=case_adapter)
    assert result.runs["C2"] == 1  # released, and at most once (its own tick caps it)


def test_run_fail_sweep_sweep_entry_overrides_the_scenario_script() -> None:
    """A scenario script that already pins (P, 1) to SUCCESS is overridden by
    the sweep's own FAILURE entry for that case (DL-184): C still gets
    suppressed."""
    text = (
        "insert_job: P\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: C\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(P)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base_adapter = FakeAdapter({("P", 1): (0.0, 0)}, default=(0.0, 0))
    bounds = expected_bounds(catalog, graph, ticks)
    baseline = play_once(catalog, start=START, horizon=HORIZON, adapter=base_adapter)
    assert baseline.runs == {"P": 2, "C": 2}
    producers = fail_sweep_producers(catalog, graph)
    cases, _findings = run_fail_sweep(
        catalog,
        baseline.runs,
        bounds,
        base_adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=producers,
    )
    (case,) = cases
    assert case.suppressed == {"C": 1}  # the case script overwrote (P, 1)


def test_case_fail_entry_prefers_the_scripted_run_1_duration() -> None:
    """DL-184 authorizes synthesizing the EXIT, never the duration --
    asserted where the duration lives (this slice's review found the
    suppression-math version of this test blind: the counts are identical
    at 600s and 0s). Preference order: the scenario's own (P, 1) entry,
    then the job default, then the estate default; a scripted run-1 PARK
    carries no duration and falls through."""
    scripted = FakeAdapter({("P", 1): (600.0, 0)}, default=(5.0, 0))
    assert case_fail_entry(scripted, "P", 7) == (600.0, 7)
    job_defaulted = FakeAdapter({}, default=(5.0, 0), job_default={"P": (300.0, 0)})
    assert case_fail_entry(job_defaulted, "P", 7) == (300.0, 7)
    parked_run = FakeAdapter({("P", 1): None}, default=(5.0, 0))
    assert case_fail_entry(parked_run, "P", 7) == (5.0, 7)
    bare = FakeAdapter({}, default=None)
    assert case_fail_entry(bare, "P", 7) == (0.0, 7)


def test_run_fail_sweep_suppression_math_at_a_nonzero_duration() -> None:
    """The behavioral companion: with a 600s scenario default the sweep's
    case still fails P's first run and suppresses its consumer by one."""
    text = (
        "insert_job: P\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00, 13:00"\n\n'
        "insert_job: C\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(P)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(600.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    assert adapter.job_default["P"] == (600.0, 0)  # the scenario duration survives synthesis
    bounds = expected_bounds(catalog, graph, ticks, parked=parked_fw, no_success_exit=no_success)
    baseline = play_once(catalog, start=START, horizon=HORIZON, adapter=adapter)
    assert baseline.runs == {"P": 2, "C": 2}
    producers = fail_sweep_producers(catalog, graph)
    cases, _findings = run_fail_sweep(
        catalog,
        baseline.runs,
        bounds,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=producers,
        parked=parked_fw,
    )
    (case,) = cases
    assert case.suppressed == {"C": 1}


def test_run_fail_sweep_findings_carry_the_case_tag_happy_path_carries_none() -> None:
    """The DL-180 OR multi-fire on cs_either is the happy path's finding
    (case=None) AND the fail:cs_b case's finding (case="fail:cs_b", DL-184)
    -- cs_b's own failure does not suppress it because the OR join still
    fires on cs_a's two wakes alone."""
    catalog = lower_source(SMOKE_JIL)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    bounds = expected_bounds(catalog, graph, ticks, parked=parked_fw, no_success_exit=no_success)
    baseline = play_once(catalog, start=START, horizon=HORIZON, adapter=adapter)
    happy = compare(catalog, bounds, baseline.runs, start=START, horizon=HORIZON)
    (happy_finding,) = happy.findings
    assert happy_finding.case is None

    producers = fail_sweep_producers(catalog, graph)
    _cases, findings = run_fail_sweep(
        catalog,
        baseline.runs,
        bounds,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=producers,
        parked=parked_fw,
    )
    (sweep_finding,) = findings
    assert sweep_finding.kind == "multi_fire"
    assert sweep_finding.jobs == ["cs_either"]
    assert sweep_finding.case == "fail:cs_b"


def test_run_fail_sweep_a_case_that_trips_the_zero_delay_guard_tags_the_finding() -> None:
    """A scheduled `kicker` feeds BOTH members of a two-job condition SCC
    (the CYCLE_JIL shape) via s(kicker). Failing kicker's own run is quiet:
    neither cy_a nor cy_b is ever woken, so nothing spins. But in ANY OTHER
    case kicker is unscripted and completes normally, and its SUCCESS kicks
    both members at once -- one of them then re-triggers the other via
    their mutual s() forever at zero duration, tripping the guard inside
    THAT case's play. `case.cycle` is set and the zero_delay_cycle finding
    carries the case tag.

    NOTE: the fail:cy_a case spins ~13s of virtual-time before the guard
    trips -- deliberately the ONE spin test in the suite (this slice's
    review folded play_once's own guard test into it: run_fail_sweep can
    only see result.cycle because play_once caught it, so this pins the
    catch, the jobs/instant payload, AND the case tagging in one spin).
    baseline_runs is a stub pinning both producers as reached: a real
    baseline here would ALSO spin, doubling the runtime for no additional
    assertion -- this test checks case.cycle/findings, not suppression."""
    text = (
        "insert_job: kicker\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: cy_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cy_b) | s(kicker)\n\n"
        "insert_job: cy_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cy_a) | s(kicker)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    bounds = expected_bounds(catalog, graph, ticks, parked=parked_fw, no_success_exit=no_success)
    baseline = {"kicker": 1, "cy_a": 1}  # both reached, else inconclusive_not_reached
    cases, findings = run_fail_sweep(
        catalog,
        baseline,
        bounds,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        producers=["kicker", "cy_a"],
        parked=parked_fw,
    )
    by_producer = {case.producer: case for case in cases}
    assert by_producer["kicker"].outcome == "ran"
    assert by_producer["kicker"].cycle is False  # failing the kicker is quiet
    assert by_producer["cy_a"].outcome == "ran"
    assert by_producer["cy_a"].cycle is True
    (cycle_finding,) = [f for f in findings if f.kind == "zero_delay_cycle"]
    assert cycle_finding.case == "fail:cy_a"
    # the ZeroDelayCycleError payload survived the conversion: the spinning
    # jobs and the frozen instant (kicker completes at its 01:00 tick)
    assert cycle_finding.jobs == ["cy_a", "cy_b", "kicker"]
    assert "2026-09-01 01:00:00" in cycle_finding.detail
    (finding,) = findings
    assert finding.kind == "zero_delay_cycle"
    assert finding.case == "fail:cy_a"
    assert finding.jobs == ["cy_a", "cy_b", "kicker"]


# --------------------------------------------------------------- run_flag_sweep


def test_run_flag_sweep_dl_180_recovery_deviates_on_the_stale_flag_case_only() -> None:
    """FLAG_SMOKE_JIL over 48h (DL-184 item 8, smoke-verified end to end):
    the flags:FLAG='1' case with no reset observes flag_join firing 3 times
    against a bound of 2 -- exactly one multi_fire finding, tagged with that
    case. Every other case (the +reset variant, every falsifying value)
    produces no findings. All 10 FlagCase rows check one consumer each; only
    the deviating case counts a deviation."""
    horizon = START + timedelta(hours=48)
    catalog = lower_source(FLAG_SMOKE_JIL)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=horizon)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    fcases, findings, uncovered = run_flag_sweep(
        catalog,
        graph,
        ticks,
        adapter,
        [],
        start=START,
        horizon=horizon,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    assert uncovered == []
    assert len(fcases) == 10
    assert all(c.checked == 1 for c in fcases)
    by_case = {(c.assignment["FLAG"], c.reset): c for c in fcases}
    assert by_case[("1", False)].deviations == 1
    assert [c.deviations for (v, r), c in by_case.items() if (v, r) != ("1", False)] == [0] * 9
    (finding,) = findings
    assert finding.kind == "multi_fire"
    assert finding.jobs == ["flag_join"]
    assert finding.case == "flags:FLAG='1'"
    assert "observed 3 runs, expected at most 2" in finding.detail


def test_flag_cases_compound_gate_gets_whole_component_assignments() -> None:
    """The slice review's first blocker: one global per case never lifted a
    compound multi-global gate. F1 and F2 co-refer through `compound`, so
    cases are whole assignments over both, including the whole-condition
    SATISFYING one the bounded product search finds -- and the consumer is
    actually checked in them."""
    text = (
        "insert_job: ca\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: compound\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(ca) & v(F1) = 1 & v(F2) = go\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    cases, uncovered = flag_cases(catalog, graph)
    assert uncovered == []
    assert all(set(assignment) == {"F1", "F2"} for assignment, _reset in cases)
    assert ({"F1": "1", "F2": "go"}, False) in cases  # the satisfying assignment
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    adapter, parked_fw, no_success = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    fcases, findings, _uncovered = run_flag_sweep(
        catalog,
        graph,
        ticks,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    assert all(c.checked == 1 for c in fcases)  # compound IS checked now
    assert findings == []  # and clean: one wake per ca completion, in bound


def test_flag_sweep_genesis_true_atoms_earn_the_at_start_credit() -> None:
    """The slice review's second blocker: bare n() is TRUE at genesis (a
    never-run partner is notrunning) and an ON_ICE seed satisfies every
    atom naming it (SEM-05/SEM-20), so the at-start satisfying set CAN fire
    `n_flag` legitimately -- the could-fire credit covers it and a clean
    estate stays clean (the flat latch-counting draft manufactured exit-3
    findings here)."""
    text = (
        "insert_job: guard_ice\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: n_flag\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: n(guard_ice) & v(F3) = 1\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    adapter, parked_fw, no_success = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    fcases, findings, _uncovered = run_flag_sweep(
        catalog,
        graph,
        ticks,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    assert fcases and findings == []
    truth = genesis_truth(catalog)
    text2 = "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(q) & n(r)\n"
    atoms = {
        a.status: a
        for a in iter_atoms(lower_source(text2).jobs["p"].sem.condition.cond)  # type: ignore[union-attr]
        if isinstance(a, StatusAtom)
    }
    assert truth(atoms["NOTRUNNING"]) is True  # never-run partner is notrunning
    assert truth(atoms["SUCCESS"]) is False  # SEM-24 cannot seed a terminal


def test_flag_case_checked_counts_only_wake_dependent_global_consumers() -> None:
    """The arch review's verified over-count (DL-185): a SCHEDULED job with
    a v() condition is bounded by its own ticks before the sweep and after
    it -- only the wake-dependent consumer moved, so only it counts in
    FlagCase.checked. The third hand copy of the global-gated predicate
    counted both."""
    text = (
        "insert_job: fa\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "01:00"\n\n'
        "insert_job: flag_join\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(fa) & v(FLAG) = 1\n\n"
        "insert_job: sched_flag\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: v(FLAG) = 1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "02:00"\n'
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    adapter, parked_fw, no_success = check_adapter(catalog, FakeAdapter({}, default=(0.0, 0)))
    fcases, _findings, _uncovered = run_flag_sweep(
        catalog,
        graph,
        ticks,
        adapter,
        [],
        start=START,
        horizon=HORIZON,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    assert fcases and all(c.checked == 1 for c in fcases)  # flag_join alone


def test_run_flag_sweep_base_scenario_set_global_joins_the_scripted_set() -> None:
    """A base-scenario SET_GLOBAL plays in every case and joins the
    scripted set (DL-184 item 9). Since the compound-gate blocker fix, F
    and H co-refer through `cons`, so every case is a whole {F, H}
    assignment and cons is checked in each; the credit half is pinned at
    the expected_bounds level -- the base set arrives first (condition not
    yet true: no credit), the case's satisfying sets then buy one wake
    each."""
    text = (
        "insert_job: cons\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(F) = 1 & v(H) = 2\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    base = FakeAdapter({}, default=(0.0, 0))
    adapter, parked_fw, no_success = check_adapter(catalog, base)
    events = [Event(at=START, kind="SET_GLOBAL", payload={"name": "H", "value": "2"})]
    fcases, _findings, _uncovered = run_flag_sweep(
        catalog,
        graph,
        ticks,
        adapter,
        events,
        start=START,
        horizon=HORIZON,
        parked=parked_fw,
        no_success_exit=no_success,
    )
    fh_cases = [c for c in fcases if set(c.assignment) == {"F", "H"}]
    assert fh_cases and all(c.checked == 1 for c in fh_cases)
    bounds = expected_bounds(
        catalog,
        graph,
        ticks,
        scripted_globals=[("H", "2", True), ("F", "1", True), ("H", "2", True)],
    )
    # base H first: F unset, condition false, no credit; F=1 makes it true
    # (+1); the second H=2 re-wakes while still true (+1)
    assert bounds["cons"].expected == 2


# --------------------------------------------------------------- CLI: --sweep fail


def test_cli_sweep_fail_without_check_cadence_exits_2(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(app, ["rehearse", str(jil), "--sweep", "fail"])
    assert result.exit_code == 2
    assert "requires" in result.output
    assert "--check-cadence" in result.output


def test_cli_sweep_fail_with_run_root_exits_2(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--check-cadence",
            "--sweep",
            "fail",
            "--run-root",
            str(tmp_path / "rr"),
        ],
    )
    assert result.exit_code == 2
    assert "journal" in result.output


def test_cli_sweep_fail_format_text_smoke(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--sweep",
            "fail",
            "--format",
            "text",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "-- cadence check: happy, fail --" in result.output
    assert "-- fail sweep: 2 producers --" in result.output
    assert "fail cs_a: suppressed cs_chain -1, cs_either -1" in result.output
    assert "fail cs_b: no suppressed runs" in result.output
    assert "finding [multi_fire] (fail:cs_b) cs_either:" in result.output


def test_cli_sweep_fail_format_json_smoke(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--sweep",
            "fail",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 3, result.output
    # stdout only: result.output interleaves the sweep's stderr progress
    # lines ahead of the json document (item 13's stream separation).
    doc = json.loads(result.stdout)
    cc = doc["cadence_check"]
    assert cc["sweeps"] == ["happy", "fail"]
    assert cc["fail_sweep"] == [
        {
            "producer": "cs_a",
            "outcome": "ran",
            "suppressed": {"cs_chain": 1, "cs_either": 1},
            "cycle": False,
        },
        {"producer": "cs_b", "outcome": "ran", "suppressed": {}, "cycle": False},
    ]
    assert [f["case"] for f in cc["findings"]] == [None, "fail:cs_b"]


def test_cli_sweep_fail_progress_lines_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--sweep",
            "fail",
            "--format",
            "text",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "sweep fail cs_a" not in result.stdout
    assert "sweep fail cs_b" not in result.stdout
    assert "sweep fail cs_a" in result.stderr
    assert "sweep fail cs_b" in result.stderr


# -------------------------------------------------------------- CLI: --sweep flags


def test_cli_sweep_flags_format_text_smoke(tmp_path: Path) -> None:
    jil = _write_flag_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "48",
            "--check-cadence",
            "--sweep",
            "flags",
            "--format",
            "text",
        ],
    )
    assert result.exit_code == 3, result.output
    assert "-- flag sweep: 10 cases --" in result.output
    assert "flags FLAG: 10 cases, 1 deviations" in result.output
    finding_line = next(
        line for line in result.output.splitlines() if line.startswith("finding [multi_fire]")
    )
    assert "(flags:FLAG='1')" in finding_line


def test_cli_sweep_flags_format_json_smoke(tmp_path: Path) -> None:
    jil = _write_flag_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "48",
            "--check-cadence",
            "--sweep",
            "flags",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 3, result.output
    doc = json.loads(result.stdout)
    cc = doc["cadence_check"]
    assert cc["sweeps"] == ["happy", "flags"]
    assert len(cc["flag_sweep"]) == 10
    row_keys = {"assignment", "reset", "checked", "deviations", "cycle"}
    assert all(row_keys <= set(row) for row in cc["flag_sweep"])
    assert cc["flag_uncovered"] == []


def test_cli_sweep_fail_and_flags_together_populate_both_blocks(tmp_path: Path) -> None:
    """Both sweeps dispatch on the slice-2 smoke estate (DL-184 item 12): the
    fail sweep's two start-gate producers populate `fail_sweep`; the estate
    carries no v() condition, so `flag_sweep` is legitimately empty -- the
    sweeps list, not the row count, is what proves the flag sweep ran.
    Asserts the document only; a deviation from the OR-join finding still
    exits 3, but that is not what this test is pinning."""
    jil = _write_smoke(tmp_path)
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            "2026-09-01T00:00:00",
            "--hours",
            "24",
            "--check-cadence",
            "--sweep",
            "fail",
            "--sweep",
            "flags",
            "--format",
            "json",
        ],
    )
    doc = json.loads(result.stdout)
    cc = doc["cadence_check"]
    assert cc["sweeps"] == ["happy", "fail", "flags"]
    assert len(cc["fail_sweep"]) == 2
    assert cc["flag_sweep"] == []
    assert cc["flag_uncovered"] == []
