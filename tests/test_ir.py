"""IR-F entity-model + AST->IR-F lowering tests (phase 3).

Normative spec: docs/ir-design.md ss3-4 (models are the API) and ss8
(serialization); docs/autosys-semantics.md SEM-08/09/10/12/30/31/32/33/34.
ir.py's own module docstring lists the decisions pinned during this phase --
every one gets a test here: SEM-30 dead-config routing, SEM-34 count-match-
or-single-broadcast, subcommand support v1 (insert_job/insert_global/
insert_machine/insert_xinst only), required job_type, type-inapplicable exec
attributes, and duplicate-key/duplicate-name rejection.

Condition *algebra* shapes (atoms, precedence, lookback) are covered by
test_conditions.py; this module only tests how lowering wires a parsed Cond
into Semantics/JobIR and how lowering errors surface (messages, spans,
accumulation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsl41.ast_jil import parse_file
from dsl41.conditions import And, JobRef, StatusAtom, parse_condition
from dsl41.ir import (
    BoxLinkage,
    CatalogIR,
    ExecSpec,
    FwSpec,
    JobIR,
    LoweringError,
    MachineIR,
    ScheduleBlock,
    Semantics,
    SlaSpec,
    Time,
    dump_catalog,
    load_catalog,
    lower_catalog,
    lower_source,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS = sorted(CORPUS_DIR.glob("*.jil"))

#: sem31_xor.jil is a deliberate SEM-31 mutual-exclusivity violation fixture
#: (a future L004 linter trigger); it must fail lowering, so every "whole
#: corpus" / "lowers individually" test below excludes it -- section 3 tests
#: its failure shape directly.
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in CORPUS if p.name not in EXPECT_LOWER_ERROR]


# ---------------------------------------------------------------- 1. model validators


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hour": 24, "minute": 0},
        {"hour": 0, "minute": 60},
    ],
    ids=["hour-24", "minute-60"],
)
def test_time_bounds_rejected(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Time(**kwargs)


def test_time_parse_rejects_garbage() -> None:
    # Time.parse only checks lexical shape (regex); a non-matching string is a
    # plain ValueError, raised directly -- not a pydantic ValidationError.
    with pytest.raises(ValueError, match="expected HH:MM"):
        Time.parse("garbage")


def test_time_parse_defers_range_checking_to_the_model() -> None:
    """A syntactically HH:MM-shaped but out-of-range hour parses lexically,
    then fails the Field constraint on construction (ValidationError, not the
    ValueError that malformed text raises)."""
    with pytest.raises(ValidationError):
        Time.parse("25:00")


def test_slaspec_absolute_missing_times_rejected() -> None:
    with pytest.raises(ValidationError, match="SEM-34"):
        SlaSpec(kind="absolute")


def test_slaspec_absolute_with_offsets_cross_set_rejected() -> None:
    with pytest.raises(ValidationError, match="SEM-34"):
        SlaSpec(kind="absolute", times=[Time(hour=10, minute=0)], offsets_min=[5])


def test_slaspec_relative_missing_offsets_rejected() -> None:
    with pytest.raises(ValidationError, match="SEM-34"):
        SlaSpec(kind="relative")


def test_slaspec_relative_with_times_cross_set_rejected() -> None:
    with pytest.raises(ValidationError, match="SEM-34"):
        SlaSpec(kind="relative", offsets_min=[5], times=[Time(hour=10, minute=0)])


def test_slaspec_absolute_and_relative_happy_paths() -> None:
    absolute = SlaSpec(kind="absolute", times=[Time(hour=10, minute=0)])
    assert absolute.times == [Time(hour=10, minute=0)]
    relative = SlaSpec(kind="relative", offsets_min=[5])
    assert relative.offsets_min == [5]


def test_schedule_block_start_times_and_start_mins_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="SEM-31"):
        ScheduleBlock(start_times=[Time(hour=10, minute=0)], start_mins=[15])


def test_schedule_block_days_of_week_and_run_calendar_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="SEM-31"):
        ScheduleBlock(days_of_week=["mo"], run_calendar="monthly_cal")


@pytest.mark.parametrize("bad_minute", [60, -1, 100], ids=["60", "-1", "100"])
def test_schedule_block_start_mins_out_of_range_rejected(bad_minute: int) -> None:
    with pytest.raises(ValidationError, match="0-59"):
        ScheduleBlock(start_mins=[bad_minute])


def test_sem12_box_success_rejected_on_non_box_job() -> None:
    with pytest.raises(ValidationError, match="SEM-12"):
        JobIR(name="x", job_type="CMD", sem=Semantics(box_success=parse_condition("s(a)")))


def test_sem12_box_failure_rejected_on_non_box_job() -> None:
    with pytest.raises(ValidationError, match="SEM-12"):
        JobIR(name="x", job_type="CMD", sem=Semantics(box_failure=parse_condition("f(a)")))


def test_sem12_box_success_accepted_on_box_job() -> None:
    job = JobIR(name="x", job_type="BOX", sem=Semantics(box_success=parse_condition("s(a)")))
    assert isinstance(job.sem.box_success, StatusAtom)
    assert job.sem.box_success.job == JobRef(name="a")


def test_catalog_box_name_targets_missing_job() -> None:
    jobs = {"a": JobIR(name="a", job_type="CMD", box=BoxLinkage(box_name="ghost"))}
    with pytest.raises(ValidationError, match="not defined in the catalog"):
        CatalogIR(jobs=jobs)


def test_catalog_box_name_targets_a_non_box_job() -> None:
    jobs = {
        "a": JobIR(name="a", job_type="CMD", box=BoxLinkage(box_name="b")),
        "b": JobIR(name="b", job_type="CMD"),
    }
    with pytest.raises(ValidationError, match="not a box"):
        CatalogIR(jobs=jobs)


def test_catalog_box_tree_two_cycle_rejected() -> None:
    jobs = {
        "a": JobIR(name="a", job_type="BOX", box=BoxLinkage(box_name="b")),
        "b": JobIR(name="b", job_type="BOX", box=BoxLinkage(box_name="a")),
    }
    with pytest.raises(ValidationError, match="cycle"):
        CatalogIR(jobs=jobs)


def test_catalog_box_tree_self_reference_rejected() -> None:
    jobs = {"a": JobIR(name="a", job_type="BOX", box=BoxLinkage(box_name="a"))}
    with pytest.raises(ValidationError, match="cycle"):
        CatalogIR(jobs=jobs)


# ----------------------------------------------------------------- 2. corpus lowering


def test_whole_corpus_lowers_as_one_catalog() -> None:
    """Job set recomputed empirically for phase 5 (CLAUDE.md task): includes
    m07_mutex.jil's mutex_a/mutex_b/mutex_serial/mutex_feeder (M07 detector,
    L012 -- renamed from job_a/job_b/job_serial/feeder to avoid colliding
    with sem10_box_basic.jil's job_a/job_b in this combined catalog) and
    sem12_external_gate.jil's gate_box/gate_member_a/gate_member_b/
    gate_outside_job (SEM-12 external gating, M16/L008)."""
    files = [parse_file(p) for p in LOWERABLE_CORPUS]
    catalog = lower_catalog(files)
    assert set(catalog.jobs) == {
        "box_a",
        "colon_torture",
        "commented",
        "consumer_hourly",
        "consumer_stale",
        "consumer_window",
        "dead_scheduler",
        "gate_box",
        "gate_member_a",
        "gate_member_b",
        "gate_outside_job",
        "glob_shell",
        "job_a",
        "job_b",
        "long_lists",
        "mutex_a",
        "mutex_b",
        "mutex_feeder",
        "mutex_serial",
        "orphan_consumer",
        "pitfall_bare_hours",
        "pitfall_single_digit",
        "quarter_past",
        "template",
        "test_must_start_complete",
        "upstream_daily",
        "upstream_feed",
        "uses_global",
    }


@pytest.mark.parametrize("path", LOWERABLE_CORPUS, ids=[p.name for p in LOWERABLE_CORPUS])
def test_every_non_error_corpus_file_lowers_individually(path: Path) -> None:
    """Conditions referencing undefined jobs are legal at the IR level (SEM-06;
    the linter's L001 is what flags them later) and the only box relationship
    in the corpus (sem10_box_basic.jil) is self-contained, so no file needs
    another file's statements to lower cleanly -- every non-EXPECT file lowers
    alone, not just as part of the combined catalog."""
    catalog = lower_catalog([parse_file(path)])
    assert isinstance(catalog, CatalogIR)


def test_sem08_globals_targeted() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem08_globals.jil")])
    assert catalog.globals_declared == {"BillID": "0"}
    job = catalog.jobs["uses_global"]
    sites = {(v.attr, v.name, v.braced) for v in job.var_sites}
    assert ("command", "BillID", False) in sites
    assert ("std_err_file", "Today", True) in sites


def test_sem30_schedule_targeted() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem30_schedule.jil")])
    tmsc = catalog.jobs["test_must_start_complete"]
    assert tmsc.schedule is not None
    assert tmsc.schedule.start_times == [
        Time(hour=10, minute=0),
        Time(hour=11, minute=0),
        Time(hour=12, minute=0),
    ]
    assert tmsc.schedule.must_start == SlaSpec(kind="relative", offsets_min=[3])
    assert tmsc.schedule.must_complete == SlaSpec(kind="relative", offsets_min=[8])

    quarter_past = catalog.jobs["quarter_past"]
    assert quarter_past.schedule is not None
    assert quarter_past.schedule.start_mins == [15, 30]
    assert quarter_past.schedule.run_window == (Time(hour=14, minute=0), Time(hour=18, minute=0))
    assert quarter_past.schedule.days_of_week == ["mo", "tu", "we", "th", "fr"]


def test_continuation_multiline_targeted() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "continuation_multiline.jil")])
    job = catalog.jobs["long_lists"]
    assert job.schedule is not None
    assert job.schedule.start_times == [
        Time(hour=8, minute=0),
        Time(hour=9, minute=0),
        Time(hour=10, minute=0),
        Time(hour=11, minute=0),
    ]
    assert job.schedule.must_start == SlaSpec(kind="relative", offsets_min=[2, 2, 2, 2])


def test_sem10_box_basic_targeted() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    assert catalog.jobs["job_a"].box.box_name == "box_a"
    assert catalog.jobs["job_b"].box.box_name == "box_a"
    box_a = catalog.jobs["box_a"]
    assert box_a.job_type == "BOX"
    box_success = box_a.sem.box_success
    assert isinstance(box_success, StatusAtom)
    assert box_success.job == JobRef(name="job_a")
    assert box_success.status == "SUCCESS"


def test_machines_xinst_targeted() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "machines_xinst.jil")])
    assert set(catalog.machines) == {"unixagent", "virt_pool"}
    unixagent = catalog.machines["unixagent"]
    assert isinstance(unixagent, MachineIR)
    assert unixagent.machine_type == "a"
    assert unixagent.attrs == {
        "node_name": "unixagent.example.com",
        "max_load": "100",
        "factor": "1.0",
    }
    assert catalog.machines["virt_pool"].machine_type == "v"
    assert catalog.external_instances == {"PRD": "a"}


# ------------------------------------------------------------ 3. sem31_xor.jil (SEM-31)


def test_sem31_xor_lowering_raises_exactly_two_findings() -> None:
    path = CORPUS_DIR / "sem31_xor.jil"
    with pytest.raises(LoweringError) as exc_info:
        lower_catalog([parse_file(path)])
    findings = exc_info.value.findings
    assert len(findings) == 2
    for finding in findings:
        assert "SEM-31" in finding.message
        assert finding.span is not None
        assert finding.span.file == str(path)
    # Findings point at the conflicting attribute lines: both_time_forms'
    # start_mins (line 8) and both_day_forms' run_calendar (line 16).
    assert [f.span.line_start for f in findings if f.span is not None] == [8, 16]


# -------------------------------------------------------------------- 4. DL-07 firewall

_UNKNOWN_ATTR_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfrobnicate: 1\n"


def test_unknown_attribute_is_a_lowering_error() -> None:
    with pytest.raises(LoweringError) as exc_info:
        lower_source(_UNKNOWN_ATTR_JIL)
    message = str(exc_info.value)
    assert "frobnicate" in message
    assert "permit-unknown" in message


def test_permit_unknown_carries_the_attribute_verbatim() -> None:
    (job,) = lower_source(_UNKNOWN_ATTR_JIL, permit_unknown=True).jobs.values()
    assert job.passthrough["frobnicate"] == "1"


def test_alarm_if_fail_becomes_an_annotation() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nalarm_if_fail: 1\n"
    (job,) = lower_source(text).jobs.values()
    assert job.annotations == {"alarm_if_fail": "1"}
    assert job.passthrough == {}


def test_auto_delete_is_passthrough_with_no_error_and_no_permit_needed() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nauto_delete: 1\n"
    (job,) = lower_source(text).jobs.values()  # permit_unknown NOT set
    assert job.passthrough == {"auto_delete": "1"}


# ----------------------------------------------------------------- 5. SEM-30 dead config


def test_no_date_conditions_start_times_go_to_passthrough_schedule_none() -> None:
    text = 'insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nstart_times: "10:00"\n'
    (job,) = lower_source(text).jobs.values()
    assert job.schedule is None
    assert job.passthrough == {"start_times": '"10:00"'}


def test_falsy_date_conditions_carries_switch_and_time_attrs_to_passthrough() -> None:
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 0\nstart_times: "10:00"\n'
    )
    (job,) = lower_source(text).jobs.values()
    assert job.schedule is None
    assert job.passthrough == {"date_conditions": "0", "start_times": '"10:00"'}


def test_truthy_date_conditions_with_no_time_attrs_is_an_empty_schedule_block() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
    (job,) = lower_source(text).jobs.values()
    assert job.schedule is not None
    assert job.schedule == ScheduleBlock()  # not None: the truthy switch is real config
    assert job.passthrough == {}


# ---------------------------------------------------------------- 6. SEM-34 via lowering

_SEM34_OK_CASES: list[tuple[str, str, SlaSpec]] = [
    (
        "absolute-count-matches-start-times",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00"\nmust_start_times: "10:05, 11:05"\n',
        SlaSpec(kind="absolute", times=[Time(hour=10, minute=5), Time(hour=11, minute=5)]),
    ),
    (
        "relative-single-offset-broadcasts",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00, 12:00"\nmust_start_times: +5\n',
        SlaSpec(kind="relative", offsets_min=[5]),
    ),
    (
        "relative-exact-count",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00, 12:00"\nmust_start_times: +5, +10, +15\n',
        SlaSpec(kind="relative", offsets_min=[5, 10, 15]),
    ),
]


@pytest.mark.parametrize(
    "text,expected", [c[1:] for c in _SEM34_OK_CASES], ids=[c[0] for c in _SEM34_OK_CASES]
)
def test_sem34_must_start_times_ok_shapes(text: str, expected: SlaSpec) -> None:
    (job,) = lower_source(text).jobs.values()
    assert job.schedule is not None
    assert job.schedule.must_start == expected


_SEM34_ERROR_CASES = [
    (
        "absolute-count-mismatch",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00"\nmust_start_times: "10:05"\n',
    ),
    (
        "relative-wrong-count",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00, 12:00"\nmust_start_times: +5, +10\n',
    ),
    (
        "mixed-absolute-and-relative",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        'start_times: "10:00, 11:00"\nmust_start_times: +3, 10:00\n',
    ),
    (
        "missing-start-times",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
        "must_start_times: +5\n",
    ),
]


@pytest.mark.parametrize(
    "text", [c[1] for c in _SEM34_ERROR_CASES], ids=[c[0] for c in _SEM34_ERROR_CASES]
)
def test_sem34_must_start_times_error_shapes(text: str) -> None:
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "SEM-34" in str(exc_info.value)


# ----------------------------------------------------------------- 7. exec/type rules

_EXEC_TYPE_ERROR_CASES = [
    ("cmd-without-command", "insert_job: j\njob_type: c\nmachine: m1\n", "requires a command"),
    (
        "watch-file-on-cmd",
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nwatch_file: /tmp/f\n",
        "file-watcher attribute on a CMD job",
    ),
    (
        "command-on-fw",
        "insert_job: j\njob_type: f\nwatch_file: /tmp/f\ncommand: x\n",
        "not valid on an FW job",
    ),
    ("box-with-command", "insert_job: j\njob_type: b\ncommand: x\n", "not valid on a BOX job"),
    ("missing-job-type", "insert_job: j\ncommand: x\nmachine: m1\n", "job_type is required"),
    ("job-type-sql-unsupported", "insert_job: j\njob_type: sql\n", "not supported by lowering v1"),
    (
        "inline-attr-job-type-conflict",
        "insert_job: j   job_type: c\njob_type: b\ncommand: x\nmachine: m1\n",
        "conflicting job_type",
    ),
]


@pytest.mark.parametrize(
    "text,expected_substr",
    [c[1:] for c in _EXEC_TYPE_ERROR_CASES],
    ids=[c[0] for c in _EXEC_TYPE_ERROR_CASES],
)
def test_exec_type_error_shapes(text: str, expected_substr: str) -> None:
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert expected_substr in str(exc_info.value)


def test_fw_happy_path() -> None:
    text = "insert_job: j\njob_type: f\nwatch_file: /tmp/f\nwatch_interval: 30\nmachine: m1\n"
    (job,) = lower_source(text).jobs.values()
    assert isinstance(job.exec_, FwSpec)
    assert job.exec_.watch_file == "/tmp/f"
    assert job.exec_.watch_interval == 30
    assert job.exec_.machine == "m1"


def test_box_with_machine_and_owner_is_inert_passthrough_and_exec_none() -> None:
    text = "insert_job: j\njob_type: b\nmachine: m1\nowner: bob\n"
    (job,) = lower_source(text).jobs.values()
    assert job.exec_ is None
    assert job.passthrough == {"machine": "m1", "owner": "bob"}


def test_inline_and_attribute_job_type_agree_case_insensitively() -> None:
    text = "insert_job: j   job_type: c\njob_type: C\ncommand: x\nmachine: m1\n"
    (job,) = lower_source(text).jobs.values()
    assert job.job_type == "CMD"
    assert isinstance(job.exec_, ExecSpec)
    assert job.exec_.command == "x"


# ------------------------------------------------------------- 8. condition integration


def test_bad_condition_is_a_lowering_error_naming_the_attr() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(\n"
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "condition" in str(exc_info.value)


def test_condition_span_matches_the_attribute_line() -> None:
    path = CORPUS_DIR / "sem08_globals.jil"
    job = lower_catalog([parse_file(path)]).jobs["uses_global"]
    span = job.sem.condition_span
    assert span is not None
    assert span.line_start == 10
    assert span.line_end == 10
    assert span.file == str(path)


def test_condition_parses_into_a_sane_structure_on_a_happy_path() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(a) & f(b)\n"
    (job,) = lower_source(text).jobs.values()
    cond = job.sem.condition
    assert isinstance(cond, And)
    left, right = cond.operands
    assert (
        isinstance(left, StatusAtom) and left.job == JobRef(name="a") and left.status == "SUCCESS"
    )
    assert (
        isinstance(right, StatusAtom)
        and right.job == JobRef(name="b")
        and right.status == "FAILURE"
    )


# ---------------------------------------------------------------- 9. error accumulation


def test_two_independently_bad_statements_are_both_reported() -> None:
    text = (
        "insert_job: a\njob_type: c\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\nfrobnicate: 1\n"
    )
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert len(exc_info.value.findings) == 2
    message = str(exc_info.value)
    assert "requires a command" in message
    assert "frobnicate" in message


def test_duplicate_job_name_is_a_lowering_error() -> None:
    text = (
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: a\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "duplicate job name" in str(exc_info.value)


def test_duplicate_attribute_key_in_one_statement_is_a_lowering_error() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\ncommand: y\nmachine: m1\n"
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "duplicate attribute" in str(exc_info.value)


def test_duplicate_insert_global_is_a_lowering_error() -> None:
    text = "insert_global: G\nvalue: 1\n\ninsert_global: G\nvalue: 2\n"
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "duplicate insert_global" in str(exc_info.value)


# -------------------------------------------------------------- 10. other subcommands

_UNSUPPORTED_SUBCOMMAND_CASES = [
    ("update-job-unsupported", "update_job: j\ncommand: x\n", "not supported by lowering v1"),
    ("insert-global-missing-value", "insert_global: G\n", "missing value attribute"),
    ("insert-xinst-extra-attr", "insert_xinst: PRD\nxtype: a\nextra: 1\n", "unexpected attribute"),
    ("insert-xinst-missing-xtype", "insert_xinst: PRD\n", "missing xtype attribute"),
]


@pytest.mark.parametrize(
    "text,expected_substr",
    [c[1:] for c in _UNSUPPORTED_SUBCOMMAND_CASES],
    ids=[c[0] for c in _UNSUPPORTED_SUBCOMMAND_CASES],
)
def test_other_subcommand_error_shapes(text: str, expected_substr: str) -> None:
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert expected_substr in str(exc_info.value)


# ------------------------------------------------------------ 11. serialization (ss8)


def test_dump_catalog_is_deterministic() -> None:
    dump_a = dump_catalog(lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS]))
    dump_b = dump_catalog(lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS]))
    assert dump_a == dump_b


def test_load_dump_round_trips_the_full_corpus_catalog() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert load_catalog(dump_catalog(catalog)) == catalog


def test_dump_contains_ir_version_field() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert '"ir_version": "0.1"' in dump_catalog(catalog)


def test_dump_top_level_keys_are_sorted() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    top = json.loads(dump_catalog(catalog))
    assert list(top.keys()) == sorted(top.keys())


def test_meta_source_files_lists_the_inputs() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert catalog.meta.source_files == [str(p) for p in LOWERABLE_CORPUS]


# ----------------------------------------------------------------- 12. attr coercions


def test_n_retrys_non_integer_is_a_lowering_error() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nn_retrys: abc\n"
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "expected an integer" in str(exc_info.value)


def test_auto_hold_y_coerces_to_true() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nauto_hold: y\n"
    (job,) = lower_source(text).jobs.values()
    assert job.sem.auto_hold is True


def test_box_terminator_1_coerces_to_true() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nbox_terminator: 1\n"
    (job,) = lower_source(text).jobs.values()
    assert job.box.box_terminator is True


def test_term_run_time_maps_to_term_run_time_min() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 90\n"
    (job,) = lower_source(text).jobs.values()
    assert job.sem.term_run_time_min == 90


def test_max_exit_success_is_carried_through() -> None:
    # SEM-09: the success/failure boundary is per-job-configurable, never a
    # hardcoded exit-0 constant.
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nmax_exit_success: 2\n"
    (job,) = lower_source(text).jobs.values()
    assert job.sem.max_exit_success == 2


# ------------------------------------------------- 13. review-driven regressions

# Behaviors fixed after the phase-3 adversarial review; each test pins the
# corrected behavior so it cannot regress silently.


def test_quoted_box_name_resolves() -> None:
    """Typed-lane unquoting (statement-syntax rule 7): a JIL-quoted box_name
    must resolve against the unquoted box job name."""
    text = 'insert_job: b1\njob_type: b\ninsert_job: m1\njob_type: c\ncommand: x\nbox_name: "b1"\n'
    catalog = lower_source(text)
    assert catalog.jobs["m1"].box.box_name == "b1"


def test_quoted_global_value_matches_condition_comparand() -> None:
    """insert_global value and a value() condition comparand are both
    unquoted, so they compare equal (L002 resolution depends on this)."""
    text = 'insert_global: G\nvalue: "spaced value"\n'
    catalog = lower_source(text)
    assert catalog.globals_declared["G"] == "spaced value"


def test_fully_quoted_command_is_unquoted() -> None:
    text = 'insert_job: j\njob_type: c\ncommand: "/path with spaces/run.sh"\nmachine: m1\n'
    (job,) = lower_source(text).jobs.values()
    assert isinstance(job.exec_, ExecSpec)
    assert job.exec_.command == "/path with spaces/run.sh"


def test_partially_quoted_command_stays_verbatim() -> None:
    """Interior quoting belongs to the value (shell syntax), not JIL quoting."""
    text = 'insert_job: j\njob_type: c\ncommand: echo "a : b" done\nmachine: m1\n'
    (job,) = lower_source(text).jobs.values()
    assert isinstance(job.exec_, ExecSpec)
    assert job.exec_.command == 'echo "a : b" done'


def test_two_quote_pairs_are_not_stripped() -> None:
    text = 'insert_job: j\njob_type: c\ncommand: "a" && "b"\nmachine: m1\n'
    (job,) = lower_source(text).jobs.values()
    assert isinstance(job.exec_, ExecSpec)
    assert job.exec_.command == '"a" && "b"'


def test_annotations_and_passthrough_stay_verbatim_quoted() -> None:
    """annotations/passthrough are verbatim lanes (ir-design ss4 sketch):
    quotes survive there, unlike the typed lane."""
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        'description: "quoted note"\nauto_delete: "1"\n'
    )
    (job,) = lower_source(text).jobs.values()
    assert job.annotations["description"] == '"quoted note"'
    assert job.passthrough["auto_delete"] == '"1"'


def test_empty_box_name_is_a_loud_error() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nbox_name:\n"
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "box_name: empty value" in str(exc_info.value)


def test_schedule_reassignment_revalidates_sem31() -> None:
    block = ScheduleBlock(start_times=[Time(hour=10, minute=0)])
    with pytest.raises(ValidationError, match="SEM-31"):
        block.start_mins = [15]


def test_jobir_reassignment_revalidates_sem12() -> None:
    job = JobIR(name="j", job_type="CMD", exec_=ExecSpec(command="x"))
    with pytest.raises(ValidationError, match="SEM-12"):
        job.sem = Semantics(box_success=parse_condition("s(a)"))


def test_membership_problem_and_cycle_reported_together() -> None:
    """A dangling box_name must not mask an independent containment cycle
    (LoweringError carries every finding)."""
    text = (
        "insert_job: dangler\njob_type: c\ncommand: x\nbox_name: nowhere\n"
        "insert_job: c1\njob_type: b\nbox_name: c2\n"
        "insert_job: c2\njob_type: b\nbox_name: c1\n"
    )
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    rendered = str(exc_info.value)
    assert "'nowhere' is not defined" in rendered
    assert "box containment cycle" in rendered


def test_cycle_reported_once_not_per_participant() -> None:
    text = (
        "insert_job: c1\njob_type: b\nbox_name: c2\n"
        "insert_job: c2\njob_type: b\nbox_name: c1\n"
        "insert_job: inner\njob_type: c\ncommand: x\nbox_name: c1\n"
    )
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    cycle_findings = [f for f in exc_info.value.findings if "cycle" in f.message]
    assert len(cycle_findings) == 1


def test_duplicate_name_reported_even_with_duplicate_attr() -> None:
    """The duplicate-job-name check runs before attr collection, so a
    statement that both re-declares a name and duplicates an attribute
    reports the name collision (previously masked)."""
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        "insert_job: j\njob_type: c\ncommand: y\ncommand: z\n"
    )
    with pytest.raises(LoweringError) as exc_info:
        lower_source(text)
    assert "duplicate job name 'j'" in str(exc_info.value)
