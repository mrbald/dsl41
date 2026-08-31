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
from dsl41.derive import derive_graph
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.rehearse_check import (
    CadenceCheckError,
    CadencePolicy,
    JobPolicy,
    check_adapter,
    compare,
    expected_bounds,
    load_policy,
    play_once,
    render_text,
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


def _bounds(text: str, **kw: object) -> dict:
    """One catalog's expected_bounds over [START, HORIZON], the shared setup
    every expected_bounds test below repeats."""
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    ticks = scheduled_ticks(catalog, start=START, horizon=HORIZON)
    return expected_bounds(catalog, graph, ticks, **kw)  # type: ignore[arg-type]


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


def test_play_once_zero_delay_cycle_returns_the_cycle_on_the_result() -> None:
    """A FORCE_STARTJOB kick on a condition SCC spins the zero-delay guard.
    play_once catches it and returns rather than raising -- the check's own
    finding, not a shell failure (DL-184).

    NOTE: this spins ~10-15s through the instant budget before the guard
    trips. That is expected; this is the one such test in the suite."""
    catalog = lower_source(CYCLE_JIL)
    kick_at = START + timedelta(hours=1)
    kick = Event(at=kick_at, kind="FORCE_STARTJOB", payload={"job": "cy_a"})
    adapter = FakeAdapter({}, default=(0.0, 0))
    result = play_once(catalog, start=START, horizon=HORIZON, adapter=adapter, events=[kick])
    assert isinstance(result.cycle, ZeroDelayCycleError)
    assert result.cycle.jobs == ("cy_a", "cy_b")
    assert result.cycle.instant == kick_at


# --------------------------------------------------------------------- CLI: rehearse


def _write_smoke(tmp_path: Path) -> Path:
    jil = tmp_path / "smoke.jil"
    jil.write_text(SMOKE_JIL, encoding="utf-8")
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
