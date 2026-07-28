"""UC-backend U3-independent slice tests (phase 9): edge classification +
migration report + `dsl41 report` CLI + the U3 block itself.

Normative spec: docs/stonebranch-semantics.md Part II "Mapping-driven
compiler requirements" 1-3; backend_uc.py's module docstring (DL-15);
CLAUDE.md phase 9 ("BLOCKED on U3 ... only the migration-report emitter and
edge-classification plumbing").
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.ast_jil import parse_file
from dsl41.backend_uc import (
    BlockedOnU3,
    classify_edges,
    compile_to_uc,
    compile_twin,
    render_migration_report,
)
from dsl41.cli import app
from dsl41.derive import derive_graph
from dsl41.equiv import catalog_hash
from dsl41.ir import CatalogIR, lower_catalog, lower_source

CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in sorted(CORPUS_DIR.glob("*.jil")) if p.name not in EXPECT_LOWER_ERROR]

runner = CliRunner()


def _corpus_catalog() -> CatalogIR:
    return lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])


# ------------------------------------------------------------- edge classification


def test_compile_twin_records_definition_time_status_in_exclusion_ledger() -> None:
    """SEM-24/DL-18: the twin does not model definition-time state v1; a job
    inserted ON_HOLD must land in the exclusion ledger (M20), never be
    silently compiled as an ordinary task."""
    catalog = lower_source(
        "insert_job: seedx\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: heldx\njob_type: c\ncommand: y\nmachine: m1\n"
        "status: ON_HOLD\ncondition: s(seedx)\n"
    )
    model = compile_twin(catalog)
    (entry,) = [e for e in model.excluded if "heldx" in e]
    assert entry.startswith("M20 heldx:")
    assert "ON_HOLD" in entry
    # INACTIVE is the implicit default: not ledger-worthy
    inactive = lower_source(
        "insert_job: quietx\njob_type: c\ncommand: x\nmachine: m1\nstatus: INACTIVE\n"
    )
    assert not [e for e in compile_twin(inactive).excluded if "quietx" in e]


def test_compile_twin_records_resource_requirements_in_exclusion_ledger() -> None:
    """DL-21: `resources:` requirements are not modeled in the twin v1; they
    must land on the M34 ledger row (UC Virtual Resources), never vanish."""
    catalog = lower_source(
        "insert_job: resx\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (lock1, QUANTITY=2, FREE=A) and (pool1, QUANTITY=1)\n"
    )
    (entry,) = [e for e in compile_twin(catalog).excluded if "resx" in e]
    assert entry.startswith("M34 resx:")
    assert "lock1 x2 FREE=A" in entry and "pool1 x1" in entry


def test_report_inventories_calendars_and_surfaces_u6b() -> None:
    """DL-25: the report inventories referenced calendars per job and
    surfaces U6b via the M24 row (DL-53: per-trigger timezone U6a resolved;
    calendar-algebra parity with AutoSys extended calendars stays open as
    U6b). DL-36 refinement: definitions can now travel as autocal_asc
    exports, so each row states whether the set carries one."""
    catalog = lower_source(
        "insert_job: calj\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: month_end\nexclude_calendar: holidays\n"
        'start_times: "22:00"\n\n'
        "extended_calendar: month_end\nadjust: 0\n"
    )
    report = render_migration_report(catalog)
    assert "## Calendars (M24" in report
    assert "`month_end` (extended, defined in set) — used by `calj`" in report
    assert "`holidays` (NO DEFINITION in set) — used by `calj`" in report
    assert "**U6b**" in report  # calendar parity open question now listed
    # dead-config calendars (no date_conditions) are L005's business, not the report's
    dead = lower_source(
        "insert_job: deadj\njob_type: c\ncommand: x\nmachine: m1\nrun_calendar: month_end\n"
    )
    assert "## Calendars" not in render_migration_report(dead)
    # U6a resolved (DL-53): timezone alone (M26) no longer surfaces a question
    tz_only = lower_source(
        "insert_job: tzj\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "22:00"\ntimezone: US/Eastern\n'
    )
    assert "## Open questions" not in render_migration_report(tz_only)


def test_classify_edges_partitions_by_cls_and_loses_nothing() -> None:
    graph = derive_graph(_corpus_catalog())
    plan = classify_edges(graph)
    assert len(plan.exact) + len(plan.assumed) + len(plan.refused) == len(graph.edges)
    assert all(edge.cls == "exact" for edge in plan.exact)
    assert all(edge.cls == "assumed" for edge in plan.assumed)
    assert all(edge.cls == "redesign" for edge in plan.refused)
    counts = plan.counts()
    assert counts == {
        "exact": len(plan.exact),
        "assumed": len(plan.assumed),
        "refused": len(plan.refused),
    }


def test_every_assumed_edge_arrives_with_its_assumption() -> None:
    """Part II requirement: A rows compile + emit assumption records; the
    DerivedEdge validator guarantees the record exists (defense in depth)."""
    plan = classify_edges(derive_graph(_corpus_catalog()))
    assert plan.assumed  # corpus has A rows
    assert all(edge.assumption for edge in plan.assumed)


# ------------------------------------------------------------------- the U3 block


def test_compile_to_uc_raises_blocked_on_u3_naming_the_unblock_path() -> None:
    with pytest.raises(BlockedOnU3) as exc_info:
        compile_to_uc(_corpus_catalog())
    message = str(exc_info.value)
    assert "U3" in message
    assert "openapi.json" in message
    assert "uc-edge-schema" in message


# --------------------------------------------------------------- migration report


def test_report_pins_hash_version_and_totals() -> None:
    catalog = _corpus_catalog()
    report = render_migration_report(catalog)
    assert f"`{catalog_hash(catalog)}`" in report
    graph = derive_graph(catalog)
    plan = classify_edges(graph)
    counts = plan.counts()
    assert (
        f"jobs: {len(catalog.jobs)}, derived edges: {len(graph.edges)}"
        f" (exact {counts['exact']}, assumed {counts['assumed']},"
        f" refused {counts['refused']})" in report
    )


def test_report_is_deterministic() -> None:
    catalog = _corpus_catalog()
    assert render_migration_report(catalog) == render_migration_report(catalog)


def test_report_lists_every_refused_edge_with_source_location() -> None:
    catalog = _corpus_catalog()
    graph = derive_graph(catalog)
    report = render_migration_report(catalog, graph)
    refused_section = report.split("## Refused constructs")[1].split("## ")[0]
    for edge in classify_edges(graph).refused:
        assert f"`{edge.src}`" in refused_section
        assert edge.source_atom is not None
        assert f"{edge.source_atom.file}:{edge.source_atom.line_start}" in refused_section


def test_report_lists_every_assumption() -> None:
    catalog = _corpus_catalog()
    graph = derive_graph(catalog)
    report = render_migration_report(catalog, graph)
    assumed_section = report.split("## Assumptions")[1].split("## ")[0]
    for edge in classify_edges(graph).assumed:
        assert edge.assumption is not None
        assert edge.assumption.split("\n")[0] in assumed_section


def test_report_carries_m27_flags_mutex_or_shapes_and_boundary() -> None:
    report = render_migration_report(_corpus_catalog())
    assert "**M27** `quarter_past`" in report  # run_window flag (pass 6)
    assert "`mutex_a`, `mutex_b` → Mutually Exclusive Tasks" in report
    assert "`mutex_serial` self-exclusion → UC Instance Wait" in report
    assert "`DB_BACKUP^PRD`" in report  # external boundary
    assert "blocked on **U3**" in report


def test_report_open_question_ledger_tracks_used_rows_only() -> None:
    """DL-53 closed U2/U4/U5/U7/U8 from public vendor docs: their content now
    lives in per-edge assumption strings (derive.py), not the open-question
    ledger. The corpus uses M02/M03 (U5's old rows), M09 (U8's), and M15
    (U2's), so those three would have listed pre-DL-53 and must not now.
    U4/U7's rows (M08/M31, M29/M30) are corpus-unused either way -- their
    removal gate is test_report_no_questions_for_exitcode_only_catalog.
    What remains: U1 (corpus M12 OR shape, DL-38's fold_t003_or_join.jil)
    and U6b (corpus M24 calendars)."""
    report = render_migration_report(_corpus_catalog())
    assert "**U1**" in report
    assert "**U6b**" in report
    assert "**U2**" not in report
    assert "**U4**" not in report
    assert "**U5**" not in report
    assert "**U7**" not in report
    assert "**U8**" not in report


def test_report_no_questions_for_exitcode_only_catalog() -> None:
    """DL-53: U4 (exit-code mechanism) is resolved and M08 no longer keys the
    ledger -- the pre-DL-53 table would have listed U4 for this catalog."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: b\nmachine: m1\ncondition: e(p) = 0\n"
    )
    assert "## Open questions" not in render_migration_report(catalog)


def test_report_resolved_questions_not_listed_for_lookback_only_catalog() -> None:
    """DL-53: U5 (Time Scope bounds) is resolved and no longer keys the
    open-question ledger, so a catalog whose only A-row is an M03 lookback
    edge surfaces NO open-questions section at all -- not even with U1
    absent-but-listed-as-empty; the section itself must not render."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(p, 12.00)\n"
    )
    report = render_migration_report(catalog)
    assert "## Open questions" not in report
    assert "**U1**" not in report  # no M12 OR shape anywhere in the catalog


def test_report_u1_appears_when_an_or_shape_exists() -> None:
    catalog = lower_source(
        "insert_job: p1\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: p2\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: c\nmachine: m1\ncondition: s(p1) | s(p2)\n"
    )
    report = render_migration_report(catalog)
    assert "## OR shapes (M12" in report
    assert "**U1**" in report


def test_report_sections_absent_when_empty() -> None:
    catalog = lower_source(
        "insert_job: only\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
    )
    report = render_migration_report(catalog)
    assert "## Refused constructs" not in report
    assert "## Assumptions" not in report
    assert "## Mutual exclusion" not in report
    assert "## OR shapes" not in report
    assert "## External boundary" not in report
    assert "## Open questions" not in report
    assert "derived edges: 0 (exact 0, assumed 0, refused 0)" in report


def test_report_exact_edges_stay_out_of_the_findings_sections() -> None:
    """E rows compile silently (Part II requirement 1): an exact edge shows
    up in the totals but produces no report item."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: b\nmachine: m1\ncondition: f(p)\n"
    )
    report = render_migration_report(catalog)
    assert "derived edges: 1 (exact 1, assumed 0, refused 0)" in report
    assert "## Refused constructs" not in report
    assert "## Assumptions" not in report
    assert "**M04**" not in report


# --------------------------------------------------------------------------- CLI


def test_cli_report_renders_to_stdout_and_exits_0_despite_refused_rows() -> None:
    result = runner.invoke(app, ["report", *[str(p) for p in LOWERABLE_CORPUS]])
    assert result.exit_code == 0  # the report IS the channel; lint is the gate
    assert result.stdout.startswith("# Migration report")
    assert "## Refused constructs" in result.stdout


def test_cli_report_writes_out_file(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    result = runner.invoke(
        app, ["report", "--out", str(target), str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").startswith("# Migration report")
    assert "wrote" in result.stdout


def test_cli_report_lowering_refusal_exits_2() -> None:
    result = runner.invoke(app, ["report", str(CORPUS_DIR / "sem31_xor.jil")])
    assert result.exit_code == 2
    assert "SEM-31" in result.stderr


def test_compile_twin_exports_explicit_code_sets_for_m31() -> None:
    """M31/DL-33: success_codes/fail_codes reach the twin model so the UC
    interpreter judges with the same boundary as the AutoSys oracle."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: x\nmachine: m1\n"
        "max_exit_success: 2\nsuccess_codes: 20-30\nfail_codes: 5\n"
    )
    model = compile_twin(catalog)
    assert model.max_exit_success == {"p": 2}
    assert model.success_codes == {"p": [(20, 30)]}
    assert model.fail_codes == {"p": [(5, 5)]}
