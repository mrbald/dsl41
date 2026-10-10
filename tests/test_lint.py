"""Linter (Violation/LintReport, L001-L005, L015) + minimal CLI tests (phase 4).

Normative spec: dsl41.lint's own module docstring pins the phase-4 decisions --
L001 cross-instance reading (SEM-06/07), L002 producer heuristic (SEM-08),
L003/L004 defensive status (SEM-04/SEM-31, enforced upstream), L005 passthrough
reading (SEM-30) -- every one gets a test here. Also docs/ir-design.md ss9 (rule
table with severities) and dsl41.cli's exit-code contract (0/1/2).

Condition/IR-F fixtures and corpus-lowering conventions mirror test_ir.py:
LOWERABLE_CORPUS excludes sem31_xor.jil (a deliberate SEM-31 lowering failure),
so every "whole corpus" lint pass here uses the same set test_ir.py lowers.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from dsl41.ast_jil import SourceSpan, parse_file
from dsl41.cli import app
from dsl41.derive import derive_graph
from dsl41.ir import (
    BoxLinkage,
    CatalogIR,
    ExecSpec,
    JobIR,
    ScheduleBlock,
    Semantics,
    Time,
    lower_catalog,
    lower_source,
)
from dsl41.lint import (
    GRAPH_RULES,
    RULES,
    LintReport,
    Severity,
    Violation,
    lint_catalog,
    rule_l001,
    rule_l002,
    rule_l003,
    rule_l004,
    rule_l005,
    rule_l015,
    rule_l016,
    rule_l017,
    rule_l018,
    rule_l019,
    rule_l023,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS = sorted(CORPUS_DIR.glob("*.jil"))

#: sem31_xor.jil is a deliberate SEM-31 mutual-exclusivity violation; it fails
#: lowering (test_ir.py pins the exact LoweringError shape), so it never
#: reaches the linter. Excluded from every whole-corpus lint pass below,
#: mirroring test_ir.py's LOWERABLE_CORPUS exactly.
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in CORPUS if p.name not in EXPECT_LOWER_ERROR]

# Fixtures referenced from more than one test.
SEM06_DANGLING = CORPUS_DIR / "sem06_dangling.jil"
MACHINES_XINST = CORPUS_DIR / "machines_xinst.jil"
SEM10_BOX_BASIC = CORPUS_DIR / "sem10_box_basic.jil"
SEM08_GLOBALS = CORPUS_DIR / "sem08_globals.jil"
SEM30_DEAD_CONFIG = CORPUS_DIR / "sem30_dead_config.jil"
SEM30_SCHEDULE = CORPUS_DIR / "sem30_schedule.jil"
SEM04_LOOKBACK_PITFALL = CORPUS_DIR / "sem04_lookback_pitfall.jil"
SEM04_LOOKBACK = CORPUS_DIR / "sem04_lookback.jil"
SEM31_XOR = CORPUS_DIR / "sem31_xor.jil"


# ------------------------------------------------------- 1. Violation & LintReport


def test_violation_render_with_span_matches_the_documented_format() -> None:
    span = SourceSpan(file="f.jil", line_start=3, line_end=3, byte_start=0, byte_end=10)
    v = Violation(code="L001", severity="error", message="boom", span=span)
    assert v.render() == "f.jil:3: L001 error: boom"


def test_violation_render_without_span_omits_the_location_prefix() -> None:
    v = Violation(code="L004", severity="error", message="boom")
    assert v.render() == "L004 error: boom"


def _violation(severity: Severity) -> Violation:
    return Violation(code="L000", severity=severity, message="test")


@pytest.mark.parametrize(
    ("severities", "strict", "expected"),
    [
        ([], False, 0),
        ([], True, 0),
        (["warn"], False, 0),
        (["warn"], True, 1),
        (["error"], False, 1),
        (["error"], True, 1),
        (["info"], False, 0),
        (["info"], True, 0),
    ],
    ids=[
        "clean-not-strict",
        "clean-strict",
        "warn-only-not-strict",
        "warn-only-strict",
        "error-not-strict",
        "error-strict",
        "info-only-not-strict",
        "info-only-strict",
    ],
)
def test_exit_code_matrix(severities: list[Severity], strict: bool, expected: int) -> None:
    report = LintReport(violations=[_violation(s) for s in severities])
    assert report.exit_code(strict=strict) == expected


def test_by_code_filters_and_preserves_source_order() -> None:
    v1 = Violation(code="L001", severity="error", message="a")
    v2 = Violation(code="L002", severity="error", message="b")
    v3 = Violation(code="L001", severity="error", message="c")
    report = LintReport(violations=[v1, v2, v3])
    assert report.by_code("L001") == [v1, v3]
    assert report.by_code("L999") == []


# --------------------------------------------------------------------- 2. L001


def test_l001_sem06_dangling_alone_reports_local_and_cross_instance_refs() -> None:
    catalog = lower_catalog([parse_file(SEM06_DANGLING)])
    violations = rule_l001(catalog)
    assert len(violations) == 2
    by_detail = {v.detail: v for v in violations}
    assert set(by_detail) == {"THIS_JOB_DOES_NOT_EXIST", "also_missing^PRD"}
    for v in violations:
        assert v.code == "L001"
        assert v.severity == "error"
        assert v.span is not None
        assert v.span.line_start == 6
        assert v.span.file == str(SEM06_DANGLING)


def test_l001_cross_instance_ref_quiets_once_the_instance_is_declared() -> None:
    """sem06_dangling.jil + machines_xinst.jil as one catalog: PRD is now
    declared via insert_xinst, so only the local dangling ref still fires."""
    catalog = lower_catalog([parse_file(SEM06_DANGLING), parse_file(MACHINES_XINST)])
    violations = rule_l001(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "THIS_JOB_DOES_NOT_EXIST"


def test_l001_quiet_on_a_catalog_with_only_defined_refs() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    assert rule_l001(catalog) == []


def test_l001_box_success_referencing_an_undefined_job_names_the_attr() -> None:
    text = "insert_job: b1\njob_type: b\nbox_success: s(nope)\n"
    catalog = lower_source(text)
    violations = rule_l001(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "nope"
    assert violations[0].message.startswith("box_success")


def test_l001_duplicate_refs_in_one_condition_dedup_to_one() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(gone) | s(gone)\n"
    catalog = lower_source(text)
    violations = rule_l001(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "gone"


def test_l001_same_name_in_condition_and_box_success_reports_both() -> None:
    """Dedup is keyed by (attr_name, ref) -- condition and box_success are
    different attrs of the same job, so the same undefined name fires once
    per attr, not once per job."""
    text = "insert_job: b1\njob_type: b\ncondition: s(gone)\nbox_success: s(gone)\n"
    catalog = lower_source(text)
    violations = rule_l001(catalog)
    assert len(violations) == 2
    assert all(v.detail == "gone" for v in violations)
    attrs = {v.message.split(" ", 1)[0] for v in violations}
    assert attrs == {"condition", "box_success"}


# --------------------------------------------------------------------- 3. L002


def test_l002_sem08_globals_reports_the_one_unresolved_var() -> None:
    catalog = lower_catalog([parse_file(SEM08_GLOBALS)])
    violations = rule_l002(catalog)
    assert len(violations) == 1
    v = violations[0]
    assert v.detail == "Today"
    assert "std_err_file" in v.message


def test_l002_set_global_producer_in_the_same_catalog_quiets_the_consumer() -> None:
    text = (
        "insert_job: producer\njob_type: c\n"
        "command: sendevent -E SET_GLOBAL -G Today=20260703\nmachine: m1\n\n"
        "insert_job: consumer\njob_type: c\ncommand: echo $$Today\nmachine: m1\n"
    )
    catalog = lower_source(text)
    assert rule_l002(catalog) == []


def test_l002_consumer_with_no_producer_fires() -> None:
    text = "insert_job: j\njob_type: c\ncommand: echo $$X\nmachine: m1\n"
    catalog = lower_source(text)
    violations = rule_l002(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "X"
    assert violations[0].jobs == ["j"]


def test_l002_two_sites_of_the_same_var_in_one_job_dedup_to_one() -> None:
    text = (
        "insert_job: j\njob_type: c\ncommand: echo $$X\nmachine: m1\nstd_out_file: /tmp/$$X.log\n"
    )
    catalog = lower_source(text)
    violations = rule_l002(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "X"


def test_l002_same_var_in_two_jobs_reports_both_dedup_is_per_job() -> None:
    text = (
        "insert_job: j1\njob_type: c\ncommand: echo $$X\nmachine: m1\n\n"
        "insert_job: j2\njob_type: c\ncommand: echo $$X\nmachine: m1\n"
    )
    catalog = lower_source(text)
    violations = rule_l002(catalog)
    assert len(violations) == 2
    assert {v.jobs[0] for v in violations} == {"j1", "j2"}
    assert all(v.detail == "X" for v in violations)


# ------------------------------------------------------------- 4. L003 / L004


def test_l003_is_structurally_unreachable_on_the_whole_corpus() -> None:
    """L003 (lookback on a value() atom) can never fire through normal
    lowering: the condition grammar lexically excludes lookback tokens on
    global atoms, and GlobalAtom carries no lookback field at all (lint.py
    module docstring) -- pinned empty on real input as the defensive-scan
    contract it is."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert rule_l003(catalog) == []


def test_l004_normal_jobir_construction_still_catches_a_bad_schedule() -> None:
    """Pins the pydantic behavior the model_construct helper below has to
    work around: embedding an already-built (invalid) ScheduleBlock as the
    schedule= kwarg of a *normally*-constructed JobIR(...) re-runs
    ScheduleBlock's own SEM-31 validator and raises -- it does not silently
    smuggle the bad instance through. Defense in depth, not a bypass."""
    bad = ScheduleBlock.model_construct(start_times=[Time(hour=10, minute=0)], start_mins=[15])
    with pytest.raises(ValidationError, match="SEM-31"):
        JobIR(name="j", job_type="CMD", exec_=ExecSpec(command="x"), schedule=bad)


def _job_with_bad_schedule(name: str, **schedule_fields: Any) -> JobIR:
    """A JobIR carrying a ScheduleBlock that violates SEM-31, bypassing
    pydantic validation at both levels via model_construct -- i.e. genuinely
    hand-built IR of the kind lint.py's module docstring says L004 exists to
    catch (see test_l004_normal_jobir_construction_still_catches_a_bad_schedule
    for why JobIR.model_construct is needed here too, not just ScheduleBlock's)."""
    schedule = ScheduleBlock.model_construct(**schedule_fields)
    return JobIR.model_construct(
        name=name,
        job_type="CMD",
        box=BoxLinkage(),
        schedule=schedule,
        exec_=ExecSpec(command="x"),
        sem=Semantics(),
        annotations={},
        passthrough={},
        var_sites=[],
        span=None,
    )


def test_l004_start_times_and_start_mins_both_set() -> None:
    job = _job_with_bad_schedule("j1", start_times=[Time(hour=10, minute=0)], start_mins=[15])
    catalog = CatalogIR(jobs={"j1": job})  # CatalogIR itself IS normally constructed
    violations = rule_l004(catalog)
    assert len(violations) == 1
    v = violations[0]
    assert v.code == "L004"
    assert v.severity == "error"
    assert v.detail == "start_times+start_mins"
    assert v.jobs == ["j1"]


def test_l004_days_of_week_and_run_calendar_both_set() -> None:
    job = _job_with_bad_schedule("j2", days_of_week=["mo"], run_calendar="monthly_cal")
    catalog = CatalogIR(jobs={"j2": job})
    violations = rule_l004(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "days_of_week+run_calendar"


def test_l004_quiet_on_the_whole_normally_lowered_corpus() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert rule_l004(catalog) == []


# --------------------------------------------------------------------- 5. L005


def test_l005_dead_scheduler_reports_sorted_time_attrs_in_message_and_detail() -> None:
    catalog = lower_catalog([parse_file(SEM30_DEAD_CONFIG)])
    violations = rule_l005(catalog)
    assert len(violations) == 1
    v = violations[0]
    assert v.severity == "warn"
    assert v.jobs == ["dead_scheduler"]
    assert v.detail == "days_of_week,start_times"  # sorted
    assert "days_of_week, start_times" in v.message


def test_l005_quiet_when_date_conditions_is_truthy() -> None:
    catalog = lower_catalog([parse_file(SEM30_SCHEDULE)])
    assert rule_l005(catalog) == []


def test_l005_falsy_date_conditions_inline_fires() -> None:
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 0\nstart_times: "10:00"\n'
    )
    catalog = lower_source(text)
    violations = rule_l005(catalog)
    assert len(violations) == 1
    assert violations[0].detail == "start_times"


# --------------------------------------------------------------------- 6. L015


def test_l015_pitfall_fixture_reports_bare_hours_and_single_digit_minutes() -> None:
    catalog = lower_catalog([parse_file(SEM04_LOOKBACK_PITFALL)])
    violations = rule_l015(catalog)
    assert len(violations) == 2
    by_detail = {v.detail: v for v in violations}
    assert set(by_detail) == {"30", "2.5"}
    # DL-24 severity split: bare-hours is valid/unambiguous -> info;
    # single-digit minutes genuinely reads as a decimal -> warn
    assert by_detail["30"].severity == "info"
    assert by_detail["2.5"].severity == "warn"
    bare_hours_span = by_detail["30"].span
    single_digit_span = by_detail["2.5"].span
    assert bare_hours_span is not None and bare_hours_span.line_start == 8
    assert single_digit_span is not None and single_digit_span.line_start == 14


def test_l015_pitfall_fixture_has_no_l001_interference() -> None:
    """upstream_feed (the job both pitfall conditions reference) is defined
    in the same fixture, so L001 stays quiet -- one rule per fixture."""
    catalog = lower_catalog([parse_file(SEM04_LOOKBACK_PITFALL)])
    assert rule_l001(catalog) == []


def test_l015_quiet_on_all_clean_lookback_forms() -> None:
    catalog = lower_catalog([parse_file(SEM04_LOOKBACK)])
    assert rule_l015(catalog) == []


# ------------------------------------------------- 6b. L016/L017 dangling names (DL-25)


def test_l016_fires_for_undefined_resource_and_names_it() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "l016_resource_ref.jil")])
    (violation,) = rule_l016(catalog)
    assert violation.severity == "warn"
    assert violation.jobs == ["l16_writer"]
    assert violation.detail == "L16_MISSING_POOL"
    assert "insert_resource" in violation.message


def test_l016_quiet_when_every_resource_is_defined() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem24_status_resource.jil")])
    assert rule_l016(catalog) == []


def test_l019_fires_for_schedule_plus_condition_composition() -> None:
    """L019 (Q3 cited-resolved, DL-58): a date_conditions job that also
    carries a condition arms and fires late without expiry -- a migration-
    attention item because the UC mapping (M02) has no arm concept."""
    text = (
        "insert_job: l19_both\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "condition: s(l19_gate)\n\n"
        "insert_job: l19_gate\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l019(catalog)
    assert violation.severity == "warn"
    assert violation.jobs == ["l19_both"]
    assert "arm-and-wait" in violation.message
    assert "no arm concept" in violation.message


def test_l019_quiet_for_schedule_only_and_condition_only_jobs() -> None:
    text = (
        "insert_job: l19_sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n\n'
        "insert_job: l19_cond\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(l19_sched)\n"
    )
    assert rule_l019(lower_source(text)) == []


def test_l017_fires_only_when_the_set_defines_machines() -> None:
    """DL-25 heuristic: a job-only slice (zero insert_machine) stays quiet;
    once machine records are in the set, a ref outside them is a smell."""
    jobs_only = "insert_job: j\njob_type: c\ncommand: x\nmachine: ghost\n"
    assert rule_l017(lower_source(jobs_only)) == []
    with_machines = jobs_only + "\ninsert_machine: real1\ntype: a\n"
    (violation,) = rule_l017(lower_source(with_machines))
    assert violation.severity == "warn"
    assert violation.detail == "ghost"


def test_l017_checks_comma_lists_per_name_and_accepts_defined() -> None:
    text = (
        "insert_machine: real1\ntype: a\n\n"
        "insert_job: lb\njob_type: c\ncommand: x\nmachine: real1, ghost2, ghost3\n"
    )
    (violation,) = rule_l017(lower_source(text))
    assert violation.detail == "ghost2,ghost3"
    clean = "insert_machine: real1\ntype: a\n\ninsert_job: ok\njob_type: c\ncommand: x\nmachine: real1\n"
    assert rule_l017(lower_source(clean)) == []


def test_l018_fires_only_when_the_set_carries_calendar_definitions() -> None:
    """DL-36, the L017 convention: a job-only slice (zero calendar/cycle
    definitions) stays quiet; once the set carries any, an unresolved
    run_calendar/exclude_calendar is a smell."""
    jobs_only = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nrun_calendar: ghost_cal\nstart_times: "01:00"\n'
    )
    assert rule_l018(lower_source(jobs_only)) == []
    with_calendars = jobs_only + "\nextended_calendar: real_cal\nadjust: 0\n"
    (violation,) = rule_l018(lower_source(with_calendars))
    assert violation.severity == "warn"
    assert violation.jobs == ["j"]
    assert violation.detail == "ghost_cal"


def test_l018_corpus_fixture_fires_for_the_missing_exclude_calendar() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "l018_calendar_ref.jil")])
    (violation,) = rule_l018(catalog)
    assert violation.jobs == ["l18_reporter"]
    assert violation.detail == "l18_missing_cal"
    assert "autocal export" in violation.message


def test_l018_quiet_when_every_calendar_is_defined() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "calendars_autocal.jil")])
    assert rule_l018(catalog) == []


def test_l018_checks_holcal_and_cyccal_inside_extended_definitions() -> None:
    """An extended calendar's own references resolve against the set:
    holcal against the calendar namespace, cyccal against cycles (DL-36)."""
    text = "extended_calendar: eom\nholcal: ghost_hols\ncyccal: ghost_cycle\nadjust: 0\n"
    violations = rule_l018(lower_source(text))
    assert [(v.jobs, v.detail) for v in violations] == [
        ([], "ghost_hols"),
        ([], "ghost_cycle"),
    ]
    resolved = (
        "calendar: real_hols\n01/01/2026 00:00\n\n"
        "cycle: real_cycle\nstart_date: 01/01/2026\nend_date: 01/05/2026\n\n"
        "extended_calendar: eom\nholcal: real_hols\ncyccal: real_cycle\nadjust: 0\n"
    )
    assert rule_l018(lower_source(resolved)) == []


def test_l002_v_read_of_undeclared_global_warns_but_declared_or_produced_stay_quiet() -> None:
    """DL-25: v() reads join L002 as WARN (an unset global can be an
    intended cross-system gate), unlike $$-substitution ERRORs."""
    dangling = "insert_job: g1\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = 1\n"
    (violation,) = rule_l002(lower_source(dangling))
    assert violation.severity == "warn"
    assert violation.detail == "FLAG"
    declared = dangling + "\ninsert_global: FLAG\nvalue: 0\n"
    assert rule_l002(lower_source(declared)) == []
    produced = (
        "insert_job: setter\njob_type: c\nmachine: m1\n"
        'command: sendevent -E SET_GLOBAL -G "FLAG=1"\n\n' + dangling
    )
    assert rule_l002(lower_source(produced)) == []


# ------------------------------------------------------ 7. lint_catalog integration


def test_every_registered_rule_emits_its_own_registry_code() -> None:
    """Guard, not a merge (arch-review 2026-08-26, DL-178o): `rule_lNNN`'s
    function name, the RULES/GRAPH_RULES tuple key, and the `code="LNNN"`
    inside every Violation it builds are three independent spellings of one
    fact, and nothing pinned that they agree. Runs every registered rule
    against the whole corpus and checks each violation it emits carries the
    code it is registered under."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    graph = derive_graph(catalog)
    for code, rule in RULES:
        for violation in rule(catalog):
            assert violation.code == code
    for code, graph_rule in GRAPH_RULES:
        for violation in graph_rule(catalog, graph):
            assert violation.code == code


def test_lint_catalog_runs_the_whole_lowerable_corpus_without_crashing() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    report = lint_catalog(catalog)
    assert isinstance(report, LintReport)


def test_lint_catalog_is_deterministic_across_runs() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    first = lint_catalog(catalog).violations
    second = lint_catalog(catalog).violations
    assert first == second


def test_lint_catalog_whole_corpus_fires_only_reachable_rules() -> None:
    """L003/L004 are defensive and structurally unreachable on real input
    (section 4 above); L006/L007 join in phase 8. Everything else can fire
    on today's corpus: the phase-4 IR-F rules plus the phase-5 graph rules
    with corpus triggers (L009 via sem04_lookback.jil's consumer_stale --
    its designed purpose --, L011 via genuinely unwired fixture jobs, L008
    via sem12_external_gate.jil's SEM-12 external gate, and L012 via
    m07_mutex.jil's n() mutex pair + self-exclusion -- the phase-5 fixtures
    added per CLAUDE.md's derive/lint test-suite task). L016 joined with
    l016_resource_ref.jil (DL-25); L017 is registered but quiet -- the
    corpus defines every machine it references (machines_base.jil). L018
    joined with l018_calendar_ref.jil (DL-36); calendars_autocal.jil arms
    it corpus-wide and resolves its own holcal/cyccal refs in-file. L019
    (Q3, DL-54/58) joined via consumer_stale -- sem04_lookback.jil's scheduled
    consumer with a condition is exactly the schedule+condition composition
    the rule flags (one job, two distinct hazards: L009 staleness, L019
    arm-and-wait with no UC arm concept). L020 (M19, DL-151) joined with
    l020_iced_consumer.jil's all-iced consumer. L021 (DL-180) joined with
    l021_multifire.jil's l21_daily -- and seven other fixtures whose
    condition-only consumers genuinely multi-fire (the counts test names
    them). L022 (DL-181) joined with l022_stranded.jil's l22_tail plus the
    corpus's other unread-failure tails."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    report = lint_catalog(catalog)
    assert {v.code for v in report.violations} <= {
        "L001",
        "L002",
        "L005",
        "L008",
        "L009",
        "L011",
        "L012",
        "L015",
        "L016",
        "L018",
        "L019",
        "L020",
        "L021",
        "L022",
    }


def test_lint_catalog_whole_corpus_exact_per_code_counts() -> None:
    """Pins the exact counts so a regression in any rule's matching logic
    surfaces immediately rather than as a vague 'something changed'.

    Recomputed empirically for phase 5 (CLAUDE.md task): L008 now fires
    twice (sem12_external_gate.jil's outside-job ref + global ref) and L012
    now fires twice (m07_mutex.jil's mutex pair + self-exclusion group).
    L001 dropped 7 -> 6 with the review fixes: upstream_daily is now defined
    in sem04_lookback.jil (on its own cadence) so consumer_stale's L009
    trigger survives L009's new undefined-producer skip. L005 1 -> 2 with
    sem24_status_resource.jil (DL-18): LOAD_C's timezone without
    date_conditions is the estate shape's deliberate dead config.
    L002 1 -> 3 with the DL-25 v() extension: the corpus already carried
    two dangling v() reads nothing checked (consumer_window's $REGION and
    gate_box's $ABORT_FLAG -- the latter IS sem12's intended external
    gate, which is exactly why v() reads are warn, not error). L016 x1 via
    l016_resource_ref.jil's missing pool. L018 x1 via l018_calendar_ref.jil's
    deliberately missing exclude_calendar (DL-36; the calendar fixtures added
    no other finding -- every other count is unchanged). L019 x1 via
    sem04_lookback.jil's consumer_stale, the corpus's one schedule+condition
    composition (Q3, DL-54). L020 x3 via l020_iced_consumer.jil: l20_consumer
    (DL-151, AutoSys runs it while UC skips), and since DL-243,
    l20_never_runs (the opposite direction: AutoSys can never run it while
    UC might, because its iced predecessor is gated by an ordinary f()
    atom) and l20_lookback_rescued (the original direction again: its
    f(l20_iced, 0) carries a lookback qualifier, which the Q10 pin reads
    true against the iced producer regardless of atom kind). l20_mixed and
    l20_live_failure are each the in-file non-trigger for one direction and
    add no other L020 finding. L021 x9: l21_daily
    (the designed trigger), plus eight condition-only consumers whose
    shapes genuinely multi-fire -- the fold_t003 OR-joins (fold_or_join,
    fold_or2_joina/b: either branch's completion finds the other's latch),
    l20_mixed (two unqualified latches), l20_never_runs since DL-243 (same
    shape: f(l20_iced) & s(l20_live), two unqualified wake sources), the
    two guard-as-trigger shapes the rule was built for, mutex_b and
    etl:load, and l022_stranded.jil's l22_either (an unqualified OR pair,
    DL-181). L012 3 -> 5 with l021_multifire.jil's two guard pairs
    (l21_daily and l21_fixed each name n(l21_guard)). L022 x18 (DL-181):
    l22_tail (the designed trigger), the fold_t003 fan-out members and
    fold_mixed_b stranding on their seeds, l21_daily/l21_fixed on their
    sources, the guard-shape consumers mutex_b and etl:load on their
    feeders, the sem04 pitfall pair on upstream_feed, and
    sem24_status_resource's NIGHT_SB on SEED_C -- every one a
    condition-only tail whose producer's failure nothing reads. l20_live is
    no longer one of them since DL-243: l20_live_failure now reads its
    failure (f(l20_live)), so it is no longer stranded."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    report = lint_catalog(catalog)
    counts = Counter(v.code for v in report.violations)
    assert counts == Counter(
        {
            "L001": 6,
            # +2 at DL-192: viz_locks.jil's lk_x3/lk_x4 hold a resource and
            # nothing else -- a requirement is not wiring
            "L011": 5,
            "L015": 2,
            "L008": 2,
            # +8 at DL-192: viz_locks.jil states seven mutex pairs (three of
            # them a complete clique, two an incomplete component) and one
            # self-exclusion
            "L012": 13,  # + names_colon_join.jil's etl:load/etl:probe pair (DL-39)
            "L002": 3,
            "L005": 2,
            "L009": 1,
            "L016": 1,
            "L018": 1,
            "L019": 1,
            # DL-243 added l20_never_runs (the opposite direction) and
            # l20_lookback_rescued (original direction, lookback-rescued)
            "L020": 3,
            # +2 and +5 at DL-192: viz_locks.jil's lk_seed fans out to five
            # condition-only consumers, and lk_a/lk_e guard theirs with n()
            "L021": 11,  # DL-243 added l20_never_runs (two unqualified sources)
            "L022": 18,  # DL-243: -1, l20_live_failure now reads l20_live's failure
        }
    )
    dangling = sorted(v.jobs[0] for v in report.by_code("L011"))
    assert dangling == ["commented", "dead_scheduler", "glob_shell", "lk_x3", "lk_x4"]
    (stale,) = report.by_code("L009")
    assert stale.jobs == ["consumer_stale"]  # the fixture's documented purpose


# --------------------------------------------------------------------- 8. CLI

runner = CliRunner()


def test_report_suppress_drops_code_from_findings_and_exit() -> None:
    """DL-23: suppression is a reporting choice on the complete report."""
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem24_status_resource.jil")])
    report = lint_catalog(catalog)
    assert report.by_code("L005")  # the fixture's deliberate dead-config timezone
    slim = report.suppress({"L005"})
    assert not slim.by_code("L005")
    assert len(slim.violations) == len(report.violations) - len(report.by_code("L005"))
    assert report.by_code("L005")  # original untouched (copy, not mutation)


def test_cli_suppress_l005_flips_strict_exit(tmp_path: Path) -> None:
    dead = tmp_path / "dead.jil"
    dead.write_text(
        "insert_job: tz_conv\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "10:00"\ntimezone: Zurich\n\n'
        "insert_job: tz_dead\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(tz_conv)\ntimezone: Zurich\n",
        encoding="utf-8",
    )
    strict = runner.invoke(app, ["lint", str(dead), "--strict"])
    assert strict.exit_code == 1
    assert "L005" in strict.stdout
    # tz_dead also draws L022 (DL-181: nothing reads f(tz_conv)) -- info
    # never moves the exit code, so only its line needs suppressing too
    # for the empty-stdout half of the assertion
    suppressed = runner.invoke(app, ["lint", str(dead), "--strict", "--suppress", "L005,L022"])
    assert suppressed.exit_code == 0
    assert suppressed.stdout == ""
    info_only = runner.invoke(app, ["lint", str(dead), "--strict", "--suppress", "L005"])
    assert info_only.exit_code == 0  # the remaining finding is info-tier
    assert "L022" in info_only.stdout


def test_cli_suppress_accepts_comma_lists_and_case(tmp_path: Path) -> None:
    dead = tmp_path / "dead.jil"
    dead.write_text(
        "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\ntimezone: Zurich\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["lint", str(dead), "--strict", "--suppress", "l005,L011"])
    assert result.exit_code == 0, result.output


def test_cli_suppress_unknown_code_is_a_loud_exit_2(tmp_path: Path) -> None:
    """DL-23: a typo silently suppressing nothing would be silent loss."""
    clean = tmp_path / "clean.jil"
    clean.write_text("insert_job: c1\njob_type: c\ncommand: x\nmachine: m1\n", encoding="utf-8")
    result = runner.invoke(app, ["lint", str(clean), "--suppress", "L999"])
    assert result.exit_code == 2
    assert "unknown rule code" in result.output


def test_cli_clean_file_exits_0_with_empty_stdout(tmp_path: Path) -> None:
    # Scheduled so L011 (dangling: no schedule, no wiring, no box) stays
    # quiet -- a bare one-job file is legitimately flagged since phase 5.
    clean = tmp_path / "clean.jil"
    clean.write_text(
        "insert_job: clean_job\njob_type: c\ncommand: echo hi\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "10:00"\n'
    )
    result = runner.invoke(app, ["lint", str(clean)])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_cli_bare_unwired_job_warns_l011(tmp_path: Path) -> None:
    bare = tmp_path / "bare.jil"
    bare.write_text("insert_job: bare_job\njob_type: c\ncommand: echo hi\nmachine: m1\n")
    result = runner.invoke(app, ["lint", str(bare)])
    assert result.exit_code == 0  # warn severity
    assert result.stdout.count("L011 warn:") == 1


def test_cli_lookback_pitfall_exits_0_with_two_l015_lines_on_stdout() -> None:
    result = runner.invoke(app, ["lint", str(SEM04_LOOKBACK_PITFALL)])
    assert result.exit_code == 0
    assert result.stdout.count("L015 info:") == 1  # bare hours (DL-24)
    assert result.stdout.count("L015 warn:") == 1  # single-digit minutes
    assert f"{SEM04_LOOKBACK_PITFALL}:8:" in result.stdout
    assert f"{SEM04_LOOKBACK_PITFALL}:14:" in result.stdout


def test_cli_lookback_pitfall_strict_exits_1_only_for_the_warn_shape() -> None:
    """DL-24: with --strict the single-digit-minutes WARN gates the exit;
    the bare-hours INFO alone must not (see the info-only test below)."""
    result = runner.invoke(app, ["lint", "--strict", str(SEM04_LOOKBACK_PITFALL)])
    assert result.exit_code == 1
    assert result.stdout.count("L015 warn:") == 1


def test_cli_bare_hours_alone_passes_strict(tmp_path: Path) -> None:
    """DL-24: an estate that uses intended bare-hours lookbacks everywhere
    is strict-clean without suppression."""
    jil = tmp_path / "bare_hours.jil"
    jil.write_text(
        "insert_job: feed\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "10:00"\n\n'
        "insert_job: consumer\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(feed, 12)\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["lint", "--strict", str(jil)])
    assert result.exit_code == 0
    assert result.stdout.count("L015 info:") == 1


def test_cli_sem06_dangling_exits_1_with_l001_lines() -> None:
    result = runner.invoke(app, ["lint", str(SEM06_DANGLING)])
    assert result.exit_code == 1
    assert result.stdout.count("L001 error:") == 2


def test_cli_sem31_xor_exits_2_with_sem31_on_stderr() -> None:
    """sem31_xor.jil fails at lowering, before the linter ever runs (it is
    excluded from LOWERABLE_CORPUS above, per test_ir.py). Verified
    empirically that this typer/click build always separates stdout/stderr
    on the Result object (no mix_stderr knob exists on this CliRunner)."""
    result = runner.invoke(app, ["lint", str(SEM31_XOR)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "SEM-31" in result.stderr


def test_cli_permit_unknown_allows_an_otherwise_refused_file(tmp_path: Path) -> None:
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "10:00"\nfrobnicate: 1\n'
    )
    path = tmp_path / "unknown.jil"
    path.write_text(text)

    refused = runner.invoke(app, ["lint", str(path)])
    assert refused.exit_code == 2
    assert "permit-unknown" in refused.stderr

    permitted = runner.invoke(app, ["lint", "--permit-unknown", str(path)])
    assert permitted.exit_code == 0
    assert permitted.stdout == ""


def test_cli_missing_file_exits_2_like_any_input_that_never_reached_the_linter() -> None:
    """Contract (cli.py docstring / DL-11): exit 2 means the input never
    reached the linter. That covers unreadable input (missing file, directory,
    non-UTF-8) exactly like a parse error or lowering refusal -- a filename
    typo must not masquerade as 'linter found problems' (exit 1)."""
    result = runner.invoke(app, ["lint", "/no/such/file/dsl41-test-never-exists.jil"])
    assert result.exit_code == 2
    assert "dsl41-test-never-exists.jil" in result.stderr
    assert result.stdout == ""


def test_cli_directory_input_exits_2() -> None:
    result = runner.invoke(app, ["lint", str(CORPUS_DIR)])
    assert result.exit_code == 2
    assert result.stdout == ""


def test_l001_box_override_message_states_the_sem12_consequence() -> None:
    """Review fix: an undefined box_success ref does not block auto-start;
    the override never fires (SEM-12 hung-RUNNING risk) -- the message must
    say the right consequence per attribute."""
    text = "insert_job: b\njob_type: b\nbox_success: s(ghost)\n"
    report = lint_catalog(lower_source(text))
    (violation,) = report.by_code("L001")
    assert "override never fires" in violation.message
    assert "auto-start" not in violation.message


def test_l002_lists_every_attribute_carrying_the_unresolved_var() -> None:
    """Review fix: one violation per (job, var) naming ALL attrs that carry
    the site, not just the first in field order."""
    text = (
        "insert_job: j\njob_type: c\nmachine: m1\n"
        "command: /opt/run.sh $$Region\n"
        "std_err_file: /tmp/$$Region.err\n"
    )
    report = lint_catalog(lower_source(text))
    (violation,) = report.by_code("L002")
    assert "std_err_file" in violation.message
    assert "command" in violation.message
    assert violation.detail == "Region"


# ------------------------------------------------------ 13. L023, DST hours (SEM-32)

#: A reference date for every L023 test, so no result reads today's date.
L023_TODAY = date(2026, 6, 1)
NY = "America/New_York"


def _sched(name: str, attrs: str, zone: str | None = None) -> str:
    tz = f"timezone: {zone}\n" if zone else ""
    return (
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"date_conditions: 1\n{attrs}\n{tz}\n"
    )


def _l023(attrs: str, zone: str | None = None, base_tz: str | None = None) -> list[Violation]:
    return rule_l023(lower_source(_sched("j", attrs, zone)), base_tz=base_tz, today=L023_TODAY)


def _by_detail(found: list[Violation]) -> dict[str, str]:
    return {v.detail or "": v.message for v in found}


def test_l023_london_start_time_in_the_shared_hour_is_one_finding_with_both_days() -> None:
    """London springs forward over 01:00-01:59 and falls back over the same
    wall hour. The spring shape is not the documented one; the runner moves
    the time past the gap at fold=0. The autumn time runs once, in the
    second pass."""
    (v,) = _l023('start_times: "01:30"', "Europe/London")
    assert (v.code, v.severity, v.jobs) == ("L023", "warn", ["j"])
    assert "Europe/London" in v.message
    assert "skips 01:00-01:59 (unverified shape) it runs at 02:30" in v.message
    assert "repeats 01:00-01:59 it runs at 01:30, once, in the second pass (UTC+00:00)" in v.message
    assert "\n" not in v.message


def test_l023_london_times_outside_the_change_hour_are_quiet() -> None:
    assert _l023('start_times: "00:59, 02:00, 10:00"', "Europe/London") == []


def test_l023_new_york_gap_prints_what_start_time_instants_computes() -> None:
    from dsl41.timezones import dst_windows, start_time_instants, to_local

    tz = ZoneInfo(NY)
    gap = next(w for w in dst_windows(tz, L023_TODAY) if w.kind == "gap")
    (index, instant), *_ = start_time_instants(gap.day, [(2, 5)], tz, dst="vendor")
    expected = to_local(instant, tz).strftime("%H:%M:%S")
    (v,) = _l023('start_times: "02:05"', NY)
    assert f"skips 02:00-02:59 it runs at {expected}" in v.message
    assert expected == "03:00:05"


def test_l023_a_second_start_time_in_the_missing_hour_does_not_run() -> None:
    found = _by_detail(_l023('start_times: "02:05, 02:25"', NY))
    assert "it runs at 03:00:05" in found["start_times 02:05"]
    assert "it does not run" in found["start_times 02:25"]


def test_l023_start_times_that_land_on_one_tick_merge() -> None:
    found = _by_detail(_l023('start_times: "02:00, 03:00"', NY))
    assert (
        "runs at 03:00, the same tick as the 03:00 start time (one run; unverified)"
        in found["start_times 02:00"]
    )
    assert (
        "the same tick as the 02:00 start time (one run; unverified)" in found["start_times 03:00"]
    )


def test_l023_new_york_overlap_start_time_runs_once_in_the_second_pass() -> None:
    (v,) = _l023('start_times: "01:05"', NY)
    assert "repeats 01:00-01:59 it runs at 01:05, once, in the second pass (UTC-05:00)" in v.message
    assert "skips" not in v.message


def test_l023_new_york_times_outside_the_change_hours_are_quiet() -> None:
    assert _l023('start_times: "00:59, 03:00, 12:00"', NY) == []


def test_l023_sydney_gap_is_documented_and_its_overlap_is_not() -> None:
    """Sydney skips 02:00-02:59 in October (the documented shape) and repeats
    the same hour in April, which is unverified."""
    (v,) = _l023('start_times: "02:30"', "Australia/Sydney")
    assert "skips 02:00-02:59 it runs at 03:00:30" in v.message
    assert "repeats 02:00-02:59 (unverified shape) it runs at 02:30, once, in the" in (v.message)


def test_l023_santiago_changes_at_midnight() -> None:
    found = _by_detail(_l023('start_times: "00:30, 23:30"', "America/Santiago"))
    assert "skips 00:00-00:59 (unverified shape) it runs at 01:30" in found["start_times 00:30"]
    assert (
        "repeats 23:00-23:59 (unverified shape) it runs at 23:30, once"
        in found["start_times 23:30"]
    )


def test_l023_absolute_must_times_follow_the_computed_deadline() -> None:
    found = _by_detail(
        _l023('start_times: "02:05"\nmust_start_times: "02:10"\nmust_complete_times: "02:30"', NY)
    )
    assert "it is due at 03:00:10" in found["must_start_times 02:10"]
    assert "it is due at 03:00:30" in found["must_complete_times 02:30"]


def test_l023_a_must_time_outside_the_hour_moves_when_its_start_runs_after_it() -> None:
    found = _by_detail(_l023('start_times: "02:30"\nmust_start_times: "03:00"', NY))
    assert "it runs at 03:00:30" in found["start_times 02:30"]
    assert "it is due at 03:00:59" in found["must_start_times 03:00"]


def test_l023_a_must_time_of_a_start_that_does_not_run_is_never_armed() -> None:
    found = _by_detail(_l023('start_times: "02:05, 02:25"\nmust_start_times: "02:10, 02:50"', NY))
    assert (
        "it is never armed: its start time does not run that day" in found["must_start_times 02:50"]
    )


def test_l023_a_must_time_of_a_merged_start_is_never_armed() -> None:
    found = _by_detail(_l023('start_times: "02:00, 03:00"\nmust_start_times: "02:30, 03:10"', NY))
    assert (
        "its start shares the tick of the 02:00 start (unverified)"
        in found["must_start_times 03:10"]
    )


def test_l023_a_must_time_in_the_repeated_hour_takes_the_pass_the_runner_computes() -> None:
    first = _by_detail(_l023('start_times: "00:30"\nmust_complete_times: "01:15"', NY))
    assert (
        "it is due at 01:15, once, in the first pass (UTC-04:00)"
        in first["must_complete_times 01:15"]
    )
    second = _by_detail(_l023('start_times: "01:05"\nmust_complete_times: "01:15"', NY))
    assert (
        "it is due at 01:15, once, in the second pass (UTC-05:00)"
        in second["must_complete_times 01:15"]
    )


def test_l023_a_must_time_past_midnight_lands_on_the_change_day() -> None:
    found = _by_detail(_l023('start_times: "23:00"\nmust_complete_times: "26:10"', NY))
    assert "it is due at 03:00:10" in found["must_complete_times 26:10"]


def test_l023_relative_must_times_and_unmoved_absolute_ones_are_quiet() -> None:
    assert _l023('start_times: "10:00"\nmust_start_times: +5\nmust_complete_times: +90', NY) == []
    assert _l023('start_times: "10:00"\nmust_start_times: "10:30"', NY) == []


def test_l023_run_window_in_the_missing_hour_prints_the_computed_window() -> None:
    (v,) = _l023('run_window: "02:15-02:45"', NY)
    assert "the window opens at 03:00 and closes at 03:45" in v.message
    (v,) = _l023('run_window: "02:30-02:30"', NY)
    assert "the window opens at 03:00 and closes at 03:00" in v.message


def test_l023_run_window_closing_in_the_repeated_hour_closes_in_the_first_pass() -> None:
    (v,) = _l023('run_window: "11:30-01:30"', NY)
    assert "closes at 01:30, once, in the first pass (UTC-04:00)" in v.message


def test_l023_run_window_outside_the_change_hours_is_quiet() -> None:
    assert _l023('run_window: "09:00-17:00"', NY) == []
    assert _l023('run_window: "09:00-17:00"', "Europe/London") == []


def test_l023_run_window_on_an_undocumented_shape_is_read_by_wall_time() -> None:
    """London's spring gap is not the documented shape, so the oracle compares
    wall times: the window closes when the wall clock jumps past its end."""
    (v,) = _l023('run_window: "00:30-01:30"', "Europe/London")
    assert "(unverified shape) the window opens at 00:30 and closes at 00:59" in v.message
    (v,) = _l023('run_window: "01:30-05:00"', "Europe/London")
    assert "the window opens at 02:00 and closes at 05:00" in v.message
    (v,) = _l023('run_window: "01:15-01:45"', "Europe/London")
    assert "the window never opens" in v.message


def test_l023_run_window_that_reopens_in_a_repeated_hour_of_another_shape() -> None:
    """Berlin repeats 02:00-02:59, an unverified shape: the wall
    comparison opens the window a second time."""
    (v,) = _l023('run_window: "23:00-02:30"', "Europe/Berlin")
    assert "the window opens a second time at 02:00 and closes at 02:30" in v.message


@pytest.mark.parametrize("zone", ["Europe/London", "Europe/Berlin", "Australia/Lord_Howe"])
@pytest.mark.parametrize("window", ["22:00-06:00", "00:30-03:00", "23:00-06:00"])
def test_l023_an_overnight_window_that_only_spans_a_change_is_quiet(zone: str, window: str) -> None:
    """Both ends stay at their written wall times and the window opens once
    each day, so a longer or shorter day is not a finding."""
    assert _l023(f'run_window: "{window}"', zone) == []


def test_l023_start_mins_is_one_info_finding_per_job() -> None:
    (v,) = _l023("start_mins: 0,30", NY)
    assert (v.severity, v.detail, v.jobs) == ("info", "start_mins", ["j"])
    assert "repeats 01:00-01:59, ticks run twice: 01:00, 01:30" in v.message
    assert "skips 02:00-02:59, ticks do not run: 02:00, 02:30" in v.message


def test_l023_start_mins_does_not_move_the_exit_code_even_strict() -> None:
    catalog = lower_source(_sched("ny", "start_mins: 0,30", NY))
    report = lint_catalog(catalog, today=L023_TODAY)
    assert report.by_code("L023")
    assert report.exit_code(strict=True) == 0


def test_l023_start_mins_on_half_hour_changes() -> None:
    """Lord Howe skips 02:00-02:29 and repeats 01:30-01:59, neither the
    documented shape: ticks convert at fold=0."""
    (v,) = _l023("start_mins: 10,40", "Australia/Lord_Howe")
    assert "ticks move: 02:10 runs at 02:40" in v.message
    assert "ticks run once, in the first pass: 01:40" in v.message


def test_l023_start_mins_that_miss_a_short_window_still_report_the_other() -> None:
    (v,) = _l023("start_mins: 35", "Australia/Lord_Howe")
    assert "skips" not in v.message
    assert "repeats 01:30-01:59 (unverified shape), ticks run once" in v.message


@pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo", "GMT+5"])
def test_l023_zones_without_dst_are_quiet(zone: str) -> None:
    attrs = (
        'start_times: "01:30, 02:30"\nrun_window: "01:15-02:45"\nmust_start_times: "01:35, 02:40"'
    )
    assert _l023(attrs, zone) == []
    assert _l023("start_mins: 0,30", zone) == []


def test_l023_a_job_without_a_schedule_is_quiet() -> None:
    catalog = lower_source("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n")
    assert rule_l023(catalog, today=L023_TODAY) == []


def test_l023_the_base_zone_applies_to_a_job_with_no_timezone_of_its_own() -> None:
    assert _l023('start_times: "02:05"') == []  # base UTC by default
    (v,) = _l023('start_times: "02:05"', base_tz=NY)
    assert NY in v.message
    assert "use UTC as the base zone (--timezone)" in v.message
    assert "around the clock across regions" in v.message


def test_l023_a_job_timezone_overrides_the_base_zone_both_ways() -> None:
    assert _l023('start_times: "02:05"', "Asia/Tokyo", base_tz=NY) == []
    (v,) = _l023('start_times: "02:05"', NY, base_tz="Asia/Tokyo")
    assert NY in v.message


def test_l023_advice_for_a_zoned_job_names_its_own_timezone() -> None:
    (v,) = _l023('start_times: "02:05"', NY, base_tz="UTC")
    assert "set this job's timezone to UTC if it need not follow local time" in v.message
    assert "base zone" not in v.message


def test_l023_an_unresolvable_zone_is_skipped() -> None:
    assert _l023('start_times: "02:05"', "Nowhere/Land") == []
    assert _l023('start_times: "02:05"', base_tz="Nowhere/Land") == []


def test_l023_a_map_alias_resolves_like_run() -> None:
    catalog = lower_source(_sched("mapped", 'start_times: "02:05"', "NYSITE"))
    assert rule_l023(catalog, today=L023_TODAY) == []
    found = rule_l023(catalog, tz_aliases={"nysite": NY}, today=L023_TODAY)
    assert len(found) == 1


def test_l023_output_is_the_same_on_any_date_of_the_same_reference_years() -> None:
    attrs = 'start_times: "01:05, 02:05"\nrun_window: "01:15-02:45"'
    catalog = lower_source(_sched("ny", attrs, NY))
    first = rule_l023(catalog, today=date(2026, 1, 1))
    assert first
    assert first == rule_l023(catalog, today=date(2026, 12, 31))


def test_l023_runs_inside_lint_catalog_with_the_base_zone() -> None:
    catalog = lower_source(_sched("bare", 'start_times: "01:30"'))
    assert not lint_catalog(catalog, today=L023_TODAY).by_code("L023")
    report = lint_catalog(catalog, base_tz=NY, today=L023_TODAY)
    assert [v.jobs for v in report.by_code("L023")] == [["bare"]]


@pytest.fixture
def _l023_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dsl41.lint._reference_today", lambda: L023_TODAY)


def test_l023_cli_timezone_sets_the_base_zone(tmp_path: Path, _l023_date: None) -> None:
    jil = tmp_path / "dst.jil"
    jil.write_text(_sched("bare", 'start_times: "02:05"'), encoding="utf-8")
    plain = runner.invoke(app, ["lint", str(jil), "--strict"])
    assert "L023" not in plain.stdout
    zoned = runner.invoke(app, ["lint", str(jil), "--strict", "--timezone", NY])
    assert "L023 warn" in zoned.stdout
    assert zoned.exit_code == 1
    suppressed = runner.invoke(
        app, ["lint", str(jil), "--strict", "--timezone", NY, "--suppress", "L023"]
    )
    assert "L023" not in suppressed.stdout


def test_l023_cli_timezone_map_resolves_a_site_alias(tmp_path: Path, _l023_date: None) -> None:
    jil = tmp_path / "dst.jil"
    jil.write_text(_sched("mapped", 'start_times: "02:05"', "NYSITE"), encoding="utf-8")
    table = tmp_path / "tz.txt"
    table.write_text("NYSITE America/New_York\n", encoding="utf-8")
    assert "L023" not in runner.invoke(app, ["lint", str(jil)]).stdout
    mapped = runner.invoke(app, ["lint", str(jil), "--timezone-map", str(table)])
    assert "L023 warn" in mapped.stdout


def test_l023_cli_refuses_an_unresolvable_base_zone(tmp_path: Path) -> None:
    jil = tmp_path / "dst.jil"
    jil.write_text(_sched("bare", 'start_times: "02:05"'), encoding="utf-8")
    result = runner.invoke(app, ["lint", str(jil), "--timezone", "Nowhere/Land"])
    assert result.exit_code == 2


def test_l023_cli_timezone_map_help_says_what_it_is_for() -> None:
    result = runner.invoke(app, ["lint", "--help"])
    assert "Used only by L023" in result.stdout.replace("\n", " ")
    assert "without a warning" in " ".join(result.stdout.split()).replace("│", "")
