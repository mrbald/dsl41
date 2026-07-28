"""UC-backend tests (phase 9): edge classification + migration report +
`dsl41 report` / `dsl41 uc` CLIs + the U3a base record bundle.

Normative spec: docs/stonebranch-semantics.md Part II "Mapping-driven
compiler requirements" 1-3; docs/uc-edge-schema.md (the U3a frozen record
shape); backend_uc.py's module docstring (DL-15 + DL-55).
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from dsl41.ast_jil import parse_file
from dsl41.backend_uc import (
    classify_edges,
    compile_to_uc,
    compile_twin,
    render_migration_report,
)
from dsl41.cli import app
from dsl41.derive import derive_graph
from dsl41.equiv import catalog_hash
from dsl41.ir import CatalogIR, lower_catalog, lower_source, tool_version

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


# ----------------------------------------------- U3a base record bundle (DL-55)


def test_compile_to_uc_record_matches_frozen_schema_shape() -> None:
    """Golden shape pin against docs/uc-edge-schema.md: value wrappers,
    string vertexIds, retainSysIds false, straight edges, base tokens."""
    catalog = lower_source(
        "insert_job: extract\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: load\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(extract)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.quarantined == []
    (record,) = bundle.records
    assert record == {
        "type": "taskWorkflow",
        "name": "wf_extract",
        "retainSysIds": False,
        "workflowVertices": [
            {
                "task": {"value": "extract"},
                "vertexId": "1",
                "vertexX": "90",
                "vertexY": "90",
            },
            {
                "task": {"value": "load"},
                "vertexId": "2",
                "vertexX": "90",
                "vertexY": "270",
            },
        ],
        "workflowEdges": [
            {
                "condition": {"value": "Success"},
                "sourceId": {"value": "1"},
                "targetId": {"value": "2"},
                "straightEdge": True,
            }
        ],
    }


def test_compile_to_uc_never_emits_forbidden_create_attributes() -> None:
    """CREATE-ONLY hygiene (uc-edge-schema.md): no sysId/version/export
    attrs anywhere in any record; retainSysIds pinned false on each."""
    bundle = compile_to_uc(_corpus_catalog())
    assert bundle.records
    forbidden = {"sysId", "version", "exportTable", "exportReleaseLevel"}
    for record in bundle.records:
        assert record["retainSysIds"] is False
        assert not forbidden.intersection(record)
        payload = json.dumps(record)
        assert "sysId" not in payload and "exportTable" not in payload


def test_compile_to_uc_quarantines_whole_workflow_on_cancelled_edge() -> None:
    """M06: t() lowers to the twin's `cancelled`, which has no base wire
    token -- the WHOLE workflow is withheld, sibling base edges included
    (no partial workflow, DL-55)."""
    catalog = lower_source(
        "insert_job: seed\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: mid\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(seed)\n\n"
        "insert_job: cleanup\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(mid)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.records == []
    (workflow,) = bundle.quarantined
    (reason,) = workflow.reasons
    assert "M06" in reason and "cancelled" in reason and "mid -> cleanup" in reason


def test_compile_to_uc_quarantines_var_condition_edges() -> None:
    """M08 exitcode / M09 global var-condition edges are U3b rich forms."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: j\njob_type: c\ncommand: b\nmachine: m1\ncondition: e(p) = 0\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.records == []
    (workflow,) = bundle.quarantined
    (reason,) = workflow.reasons
    assert "variable condition" in reason and "U3b" in reason


def test_compile_to_uc_is_deterministic_and_pins_provenance() -> None:
    catalog = _corpus_catalog()
    first, second = compile_to_uc(catalog), compile_to_uc(catalog)
    assert first == second
    assert first.catalog_hash == catalog_hash(catalog)
    assert not any("blocked" in note.lower() for note in first.notes)


def test_compile_to_uc_notes_carry_apply_worklist_and_excluded_ledger() -> None:
    """The bundle is self-describing: referenced-task worklist, verbatim-name
    assumption, and the twin lowering's exclusion ledger VERBATIM (a count
    was not enough -- review MINOR 3) all travel IN the artifact (DL-55)."""
    catalog = _corpus_catalog()
    bundle = compile_to_uc(catalog)
    assert any(note.startswith("referenced tasks must exist") for note in bundle.notes)
    assert any("names pass through verbatim" in note for note in bundle.notes)
    assert bundle.excluded == compile_twin(catalog).excluded
    assert bundle.excluded  # the corpus has R rows/resources: ledger non-empty


def test_compile_to_uc_notes_name_mutex_groups_and_exit_code_tasks() -> None:
    """Review MAJOR 2: a mutex-only catalog must not produce a clean-looking
    bundle -- M07 groups and M31 exit-code boundaries the records cannot
    carry are named in the notes (no silent loss, DL-04)."""
    catalog = lower_source(
        "insert_job: mxa\njob_type: c\ncommand: a\nmachine: m1\ncondition: n(mxb)\n\n"
        "insert_job: mxb\njob_type: c\ncommand: b\nmachine: m1\ncondition: n(mxa)\n\n"
        "insert_job: coded\njob_type: c\ncommand: c\nmachine: m1\nmax_exit_success: 2\n"
    )
    bundle = compile_to_uc(catalog)
    (mutex_note,) = [n for n in bundle.notes if n.startswith("M07")]
    assert "mxa+mxb" in mutex_note and "Mutually Exclusive" in mutex_note
    (exit_note,) = [n for n in bundle.notes if n.startswith("M31")]
    assert "coded" in exit_note and "Exit Code" in exit_note


def test_compile_to_uc_notes_list_synthesized_workflow_names() -> None:
    """Review MINOR 5: component workflows get invented wf_* names; the
    bundle says which record names are NOT estate names, and the verbatim
    claim is scoped to task names."""
    catalog = lower_source(
        "insert_job: extract\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: load\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(extract)\n"
    )
    bundle = compile_to_uc(catalog)
    (synth_note,) = [n for n in bundle.notes if "synthesized" in n]
    assert "wf_extract" in synth_note
    (name_note,) = [n for n in bundle.notes if "verbatim" in n]
    assert name_note.startswith("task names pass through verbatim")
    # a box-derived workflow name IS an estate name: no synthesized note
    boxed = lower_source(
        "insert_job: nightly\njob_type: b\n\n"
        "insert_job: step1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: nightly\n"
    )
    assert not any("synthesized" in n for n in compile_to_uc(boxed).notes)


def test_compile_to_uc_quarantines_duplicate_record_names() -> None:
    """Review MINOR 6: a box literally named wf_x plus a standalone job x
    both serialize to record name 'wf_x' -- an upsert wrapper would silently
    clobber, so every collision party quarantines."""
    catalog = lower_source(
        "insert_job: wf_x\njob_type: b\n\n"
        "insert_job: member\njob_type: c\ncommand: a\nmachine: m1\nbox_name: wf_x\n\n"
        "insert_job: x\njob_type: c\ncommand: b\nmachine: m1\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.records == []
    assert sorted(q.name for q in bundle.quarantined) == ["wf_x", "wf_x"]
    for workflow in bundle.quarantined:
        (reason,) = workflow.reasons
        assert "record name collision" in reason and "2 workflows" in reason


def test_compile_to_uc_empty_box_emits_record_without_dangling_worklist() -> None:
    """NIT 9 polish: an empty top-level box still gets its (vertex-less)
    record, but the apply-worklist note must not render a dangling
    '(0): ' -- it is gated on referenced tasks existing."""
    catalog = lower_source("insert_job: emptybox\njob_type: b\n")
    bundle = compile_to_uc(catalog)
    (record,) = bundle.records
    assert record["workflowVertices"] == [] and record["workflowEdges"] == []
    assert not any(note.startswith("referenced tasks") for note in bundle.notes)


def test_compile_to_uc_no_notes_at_all_when_every_workflow_quarantines() -> None:
    """The apply-worklist and verbatim-name notes are gated on `records`
    being non-empty; a catalog whose only workflow quarantines must not
    leak an apply note for tasks that will never be created."""
    catalog = lower_source(
        "insert_job: seed\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: mid\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(seed)\n\n"
        "insert_job: cleanup\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(mid)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.records == []
    assert bundle.notes == []


def test_compile_to_uc_excluded_ledger_empty_when_twin_excludes_nothing() -> None:
    """`bundle.excluded` mirrors the twin's ledger verbatim; a catalog with
    no R-class edges, resources, definition-time status, or global gates
    carries an empty ledger, while the apply-worklist notes DO fire."""
    catalog = lower_source(
        "insert_job: extract\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: load\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(extract)\n"
    )
    assert compile_twin(catalog).excluded == []
    bundle = compile_to_uc(catalog)
    assert any(note.startswith("referenced tasks must exist") for note in bundle.notes)
    assert any("names pass through verbatim" in note for note in bundle.notes)
    assert bundle.excluded == []


def test_compile_to_uc_edge_condition_tokens_map_f_and_d_exactly() -> None:
    """uc-edge-schema.md base condition tokens, verbatim: f() -> 'Failure',
    d() -> 'Success/Failure' (forward slash) on the wire."""
    catalog = lower_source(
        "insert_job: p\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: onfail\njob_type: c\ncommand: b\nmachine: m1\ncondition: f(p)\n\n"
        "insert_job: ondone\njob_type: c\ncommand: c\nmachine: m1\ncondition: d(p)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.quarantined == []
    (record,) = bundle.records
    tokens = {edge["condition"]["value"] for edge in record["workflowEdges"]}
    assert tokens == {"Failure", "Success/Failure"}


def test_vertex_layout_diamond_shape_gets_distinct_layers_and_x_within_layer() -> None:
    """_vertex_layout: y grows with longest-path depth (root=0, join=2);
    the two same-depth siblings share y but get distinct x by arrival
    order -- pins the layered-layout contract exactly, coordinate for
    coordinate, against the shape from test_derive.py's diamond fixture."""
    catalog = lower_source(
        "insert_job: dia_root\njob_type: c\ncommand: r\nmachine: m1\n\n"
        "insert_job: dia_b1\njob_type: c\ncommand: a\nmachine: m1\ncondition: s(dia_root)\n\n"
        "insert_job: dia_b2\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(dia_root)\n\n"
        "insert_job: dia_join\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: s(dia_b1) & s(dia_b2)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.quarantined == []
    (record,) = bundle.records
    assert record["name"] == "wf_dia_root"
    coords = {v["task"]["value"]: (v["vertexX"], v["vertexY"]) for v in record["workflowVertices"]}
    assert coords["dia_root"] == ("90", "90")
    assert coords["dia_b1"][1] == coords["dia_b2"][1] == "270"  # same layer
    assert coords["dia_b1"][0] != coords["dia_b2"][0]  # distinct x within the layer
    assert coords["dia_join"] == ("90", "450")  # one layer past both siblings
    ids = {v["task"]["value"]: v["vertexId"] for v in record["workflowVertices"]}
    pairs = {(e["sourceId"]["value"], e["targetId"]["value"]) for e in record["workflowEdges"]}
    assert pairs == {
        (ids["dia_root"], ids["dia_b1"]),
        (ids["dia_root"], ids["dia_b2"]),
        (ids["dia_b1"], ids["dia_join"]),
        (ids["dia_b2"], ids["dia_join"]),
    }


def test_vertex_layout_handles_a_condition_cycle_without_hanging() -> None:
    """L010 tolerates dependency cycles as legal AutoSys; _vertex_layout's
    back-edge guard (on_stack) must terminate on one too, and the two
    members must still land at different depths (no infinite mutual
    recursion, deterministic coordinates on repeat calls)."""
    catalog = lower_source(
        "insert_job: cyc_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cyc_b)\n\n"
        "insert_job: cyc_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cyc_a)\n"
    )
    first = compile_to_uc(catalog)
    second = compile_to_uc(catalog)
    assert first == second  # deterministic despite the cycle
    assert first.quarantined == []
    (record,) = first.records
    coords = {v["task"]["value"]: v["vertexY"] for v in record["workflowVertices"]}
    assert coords["cyc_a"] != coords["cyc_b"]  # distinct layers, not collapsed
    pairs = {(e["sourceId"]["value"], e["targetId"]["value"]) for e in record["workflowEdges"]}
    ids = {v["task"]["value"]: v["vertexId"] for v in record["workflowVertices"]}
    assert pairs == {(ids["cyc_a"], ids["cyc_b"]), (ids["cyc_b"], ids["cyc_a"])}


def test_vertex_layout_handles_a_self_loop_without_hanging() -> None:
    """s(self) is a genuine self-loop edge (test_derive.py); the layout's
    on_stack guard must treat it as a back edge too, and the emitted
    self-edge's sourceId/targetId both resolve to the single vertexId."""
    catalog = lower_source(
        "insert_job: self_loop_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(self_loop_job)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.quarantined == []
    (record,) = bundle.records
    assert record["workflowVertices"] == [
        {
            "task": {"value": "self_loop_job"},
            "vertexId": "1",
            "vertexX": "90",
            "vertexY": "90",
        }
    ]
    (edge,) = record["workflowEdges"]
    assert edge["sourceId"] == edge["targetId"] == {"value": "1"}
    assert edge["condition"] == {"value": "Success"}


def test_compile_to_uc_box_workflow_named_after_box_with_nested_alias_note() -> None:
    """M18 v1 (DL-16): nested boxes flatten into the top-level box's record;
    the record is named after the ROOT box, the box jobs themselves never
    appear as a workflowVertex, and the nested box's name surfaces only as
    an alias note (no UC record of its own)."""
    catalog = lower_source(
        "insert_job: outer_box\njob_type: b\n\n"
        "insert_job: inner_box\njob_type: b\nbox_name: outer_box\n\n"
        "insert_job: leaf_a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner_box\n\n"
        "insert_job: leaf_b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: inner_box\n"
        "condition: s(leaf_a)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.quarantined == []
    (record,) = bundle.records
    assert record["name"] == "outer_box"  # named after the root box
    task_names = {v["task"]["value"] for v in record["workflowVertices"]}
    assert task_names == {"leaf_a", "leaf_b"}  # box jobs are never vertices
    assert "outer_box" not in task_names and "inner_box" not in task_names
    assert any(
        note.startswith("workflow outer_box: nested boxes flattened")
        and "alias names (inner_box)" in note
        for note in bundle.notes
    )


def test_compile_to_uc_quarantine_lists_both_reasons_on_one_offending_workflow() -> None:
    """A workflow whose edges carry BOTH the missing-token defect (M06
    `t()` -> cancelled) and a var-condition defect (M08 `e() = ` on an
    already-base-token edge) must list one reason per offending edge, not
    just the first found (no silent partial reporting, DL-04/DL-55)."""
    catalog = lower_source(
        "insert_job: seed\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: gate\njob_type: c\ncommand: b\nmachine: m1\ncondition: e(seed) = 0\n\n"
        "insert_job: cleanup\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(gate)\n"
    )
    bundle = compile_to_uc(catalog)
    assert bundle.records == []
    (workflow,) = bundle.quarantined
    assert len(workflow.reasons) == 2
    cancelled = [r for r in workflow.reasons if "cancelled" in r]
    var_cond = [r for r in workflow.reasons if "variable condition" in r]
    assert len(cancelled) == 1 and "M06" in cancelled[0] and "gate -> cleanup" in cancelled[0]
    assert len(var_cond) == 1 and "exit:seed" in var_cond[0] and "U3b" in var_cond[0]


def test_compile_to_uc_quarantine_is_per_workflow_and_covers_every_twin_workflow() -> None:
    """Quarantine is per-workflow, not catalog-global (DL-55): one
    quarantined component must not suppress its clean siblings, and
    records+quarantined together must account for EVERY workflow the twin
    produced -- nothing vanishes between compile_twin and compile_to_uc."""
    catalog = lower_source(
        "insert_job: seedA\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: midA\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(seedA)\n\n"
        "insert_job: cleanupA\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(midA)\n\n"
        "insert_job: seedB\njob_type: c\ncommand: d\nmachine: m1\n\n"
        "insert_job: jobB\njob_type: c\ncommand: e\nmachine: m1\ncondition: s(seedB)\n\n"
        "insert_job: loneC\njob_type: c\ncommand: f\nmachine: m1\n"
    )
    graph = derive_graph(catalog)
    twin = compile_twin(catalog, graph)
    first = compile_to_uc(catalog, graph)
    second = compile_to_uc(catalog, graph)
    assert [r["name"] for r in first.records] == [r["name"] for r in second.records]
    assert [q.name for q in first.quarantined] == [q.name for q in second.quarantined]
    record_names = [r["name"] for r in first.records]
    quarantined_names = [q.name for q in first.quarantined]
    assert quarantined_names == ["wf_seedA"]
    assert record_names == ["wf_seedB", "wf_loneC"]  # siblings still emit
    assert set(record_names) | set(quarantined_names) == {wf.name for wf in twin.workflows}
    assert len(first.records) + len(first.quarantined) == len(twin.workflows)
    # referential integrity within each surviving record
    for record in first.records:
        vertex_ids = {v["vertexId"] for v in record["workflowVertices"]}
        for edge in record["workflowEdges"]:
            assert edge["sourceId"]["value"] in vertex_ids
            assert edge["targetId"]["value"] in vertex_ids


def test_compile_to_uc_edge_endpoints_resolve_to_known_vertex_ids_across_corpus() -> None:
    """vertexId/sourceId/targetId consistency, whole-corpus regression: no
    edge in any emitted record may reference a vertexId absent from that
    SAME record's workflowVertices."""
    bundle = compile_to_uc(_corpus_catalog())
    assert bundle.records
    for record in bundle.records:
        vertex_ids = {v["vertexId"] for v in record["workflowVertices"]}
        assert len(vertex_ids) == len(record["workflowVertices"])  # no id collisions
        for edge in record["workflowEdges"]:
            assert edge["sourceId"]["value"] in vertex_ids
            assert edge["targetId"]["value"] in vertex_ids


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
    assert "blocked on **U3b**" in report  # footer: U3a emits, U3b residue named
    assert "`dsl41 uc`" in report


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


def test_report_u3a_summary_bullet_and_quarantine_section_match_bundle_counts() -> None:
    """The `- UC base serialization (U3a): N of M workflows emit; K
    quarantined` bullet and the `## Quarantined workflows` section must
    agree with compile_to_uc's own counts and per-edge reasons -- the
    report is a rendering of the bundle, not an independent recompute."""
    catalog = lower_source(
        "insert_job: seedA\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: midA\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(seedA)\n\n"
        "insert_job: cleanupA\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(midA)\n\n"
        "insert_job: seedB\njob_type: c\ncommand: d\nmachine: m1\n"
    )
    graph = derive_graph(catalog)
    bundle = compile_to_uc(catalog, graph)
    report = render_migration_report(catalog, graph)
    assert (
        f"- UC base serialization (U3a): {len(bundle.records)} of"
        f" {len(bundle.records) + len(bundle.quarantined)} workflows emit;"
        f" {len(bundle.quarantined)} quarantined" in report
    )
    assert "## Quarantined workflows (U3a base schema cannot express these)" in report
    (workflow,) = bundle.quarantined
    assert f"- `{workflow.name}`" in report
    for reason in workflow.reasons:
        assert f"  - {reason}" in report


def test_report_quarantine_section_absent_when_nothing_quarantines() -> None:
    catalog = lower_source(
        "insert_job: extract\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: load\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(extract)\n"
    )
    report = render_migration_report(catalog)
    assert "## Quarantined workflows" not in report
    assert "- UC base serialization (U3a): 1 of 1 workflows emit; 0 quarantined" in report


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


def test_cli_uc_emits_bundle_and_summarizes_quarantine_on_stderr(tmp_path: Path) -> None:
    source = tmp_path / "t.jil"
    source.write_text(
        "insert_job: seed\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: mid\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(seed)\n\n"
        "insert_job: cleanup\njob_type: c\ncommand: c\nmachine: m1\ncondition: t(mid)\n\n"
        "insert_job: solo\njob_type: c\ncommand: d\nmachine: m1\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["uc", str(source)])
    assert result.exit_code == 0  # bundle generated; quarantine is not failure
    bundle = json.loads(result.stdout)
    assert [r["name"] for r in bundle["records"]] == ["wf_solo"]
    assert [q["name"] for q in bundle["quarantined"]] == ["wf_seed"]
    assert "1 record(s); 1 quarantined" in result.stderr
    assert "quarantined wf_seed:" in result.stderr
    # --strict flips quarantine into exit 1
    assert runner.invoke(app, ["uc", "--strict", str(source)]).exit_code == 1


def test_cli_uc_writes_out_file_and_strict_passes_when_clean(tmp_path: Path) -> None:
    source = tmp_path / "clean.jil"
    source.write_text(
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(a)\n",
        encoding="utf-8",
    )
    target = tmp_path / "bundle.json"
    result = runner.invoke(app, ["uc", "--strict", "--out", str(target), str(source)])
    assert result.exit_code == 0
    bundle = json.loads(target.read_text(encoding="utf-8"))
    assert bundle["records"][0]["type"] == "taskWorkflow"
    assert "wrote" in result.stdout


def test_cli_uc_lowering_refusal_exits_2() -> None:
    """Same exit-2 contract as `dsl41 report` (CLAUDE.md's shared
    exit-code table): the input never reaching the backend is a hard
    refusal, not a quarantine."""
    result = runner.invoke(app, ["uc", str(CORPUS_DIR / "sem31_xor.jil")])
    assert result.exit_code == 2
    assert "SEM-31" in result.stderr


def test_cli_uc_bundle_json_round_trips_with_expected_top_level_keys(tmp_path: Path) -> None:
    source = tmp_path / "clean.jil"
    source.write_text(
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(a)\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["uc", str(source)])
    assert result.exit_code == 0
    bundle = json.loads(result.stdout)
    assert set(bundle.keys()) == {
        "catalog_hash",
        "tool_version",
        "records",
        "quarantined",
        "excluded",
        "notes",
    }
    assert bundle["tool_version"] == tool_version()
    assert isinstance(bundle["catalog_hash"], str) and bundle["catalog_hash"]


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


# ------------------------------------------------------- hypothesis (breadth fuzz)

_HYP_NAMES = ["ja", "jb", "jc", "jd", "je"]


@st.composite
def _small_dag_source(draw: st.DrawFn) -> str:
    """Random small catalog: each job may condition on any STRICTLY
    earlier job via s()/f()/d()/t() -- forward-only references keep the
    source a DAG, so this explores workflow/layer/quarantine shape without
    also re-testing cycle safety (that has its own dedicated tests
    above)."""
    n = draw(st.integers(min_value=1, max_value=len(_HYP_NAMES)))
    names = _HYP_NAMES[:n]
    blocks: list[str] = []
    for i, name in enumerate(names):
        block = f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n"
        if i > 0:
            preds = draw(st.lists(st.sampled_from(names[:i]), min_size=0, max_size=i, unique=True))
            if preds:
                atoms = " & ".join(
                    f"{draw(st.sampled_from(['s', 'f', 'd', 't']))}({pred})" for pred in preds
                )
                block += f"condition: {atoms}\n"
        blocks.append(block)
    return "\n".join(blocks)


@given(source=_small_dag_source())
@settings(max_examples=60, deadline=None)
def test_hypothesis_bundle_determinism_and_referential_integrity(source: str) -> None:
    """Property pin over random small DAG catalogs: compile_to_uc is
    deterministic; records+quarantined together always account for every
    twin workflow (nothing vanishes, DL-55); every emitted edge endpoint
    resolves to a vertexId declared in that SAME record; vertexIds within
    one record are never reused."""
    catalog = lower_source(source)
    graph = derive_graph(catalog)
    twin = compile_twin(catalog, graph)
    first = compile_to_uc(catalog, graph)
    second = compile_to_uc(catalog, graph)
    assert first == second
    record_names = {r["name"] for r in first.records}
    quarantined_names = {q.name for q in first.quarantined}
    assert not record_names & quarantined_names  # no workflow in both buckets
    assert record_names | quarantined_names == {wf.name for wf in twin.workflows}
    assert len(first.records) + len(first.quarantined) == len(twin.workflows)
    for record in first.records:
        vertex_ids = [v["vertexId"] for v in record["workflowVertices"]]
        assert len(vertex_ids) == len(set(vertex_ids))  # no id collisions
        vertex_id_set = set(vertex_ids)
        for edge in record["workflowEdges"]:
            assert edge["sourceId"]["value"] in vertex_id_set
            assert edge["targetId"]["value"] in vertex_id_set
