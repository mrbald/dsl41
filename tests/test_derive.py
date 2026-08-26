"""IR-F -> IR-G derivation tests (phase 5): derive.py's seven passes + the
graph-rule additions to lint.py (L008-L014).

Normative spec: dsl41.derive's own module docstring pins the phase-5 decisions
-- global-atom pseudo-node edges, cross-instance boundary+redesign duality,
dangling-producer edges kept (never dropped; redesign on row M02 per DL-12),
unqualified-n() mutex-pair extraction (never merged into connected
components; lookback-qualified n() stays an edge), the same-cycle detector
(trigger-signature cadence + box-cadence inheritance fixpoint; boxes are
streams), box-override edge classification (M15 transitive member / M16
non-member), the assumption-mandatory-iff-assumed rule, and the
condition-only scope of the structural passes -- every bullet gets a test
here (section 12 pins the post-review corrections). Also dsl41.lint's
"Phase-5 graph-rule readings" docstring block (L008-L014); docs/ir-design.md
ss5 (passes) + ss9 (rule severities); docs/stonebranch-semantics.md Part II
(the M01-M36 mapping rows the classifier assigns); docs/autosys-semantics.md
SEM-01/04/06/07/10/12/17/30-35.

Corpus/lowering conventions mirror test_ir.py/test_lint.py: LOWERABLE_CORPUS
excludes sem31_xor.jil (a deliberate SEM-31 lowering failure). Two fixtures
are new in this phase and join the pool used by every whole-corpus test in
this file *and* in test_ir.py/test_lint.py: m07_mutex.jil (M07 mutex-pair +
self-exclusion detector, L012) and sem12_external_gate.jil (SEM-12
box-override external gating, M16/L008).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from dsl41.ast_jil import parse_file
from dsl41.conditions import GlobalAtom, JobRef, StatusAtom
from dsl41.derive import (
    DerivedEdge,
    DerivedGraph,
    _ancestor_sets,
    derive_graph,
    local_producer,
)
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.lint import (
    GRAPH_RULES,
    lint_catalog,
    rule_l008,
    rule_l009,
    rule_l010,
    rule_l011,
    rule_l012,
    rule_l013,
    rule_l014,
    rule_l020,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS = sorted(CORPUS_DIR.glob("*.jil"))

#: sem31_xor.jil is a deliberate SEM-31 mutual-exclusivity violation; excluded
#: from every whole-corpus pass here, mirroring test_ir.py/test_lint.py
#: exactly. m07_mutex.jil and sem12_external_gate.jil are this phase's new
#: fixtures (M07/L012 and SEM-12/M16/L008 triggers respectively) and join
#: the pool picked up by LOWERABLE_CORPUS below.
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in CORPUS if p.name not in EXPECT_LOWER_ERROR]

SEM10_BOX_BASIC = CORPUS_DIR / "sem10_box_basic.jil"
SEM04_LOOKBACK = CORPUS_DIR / "sem04_lookback.jil"
SEM30_SCHEDULE = CORPUS_DIR / "sem30_schedule.jil"
M07_MUTEX = CORPUS_DIR / "m07_mutex.jil"
SEM12_EXTERNAL_GATE = CORPUS_DIR / "sem12_external_gate.jil"


def _graph(text: str) -> DerivedGraph:
    return derive_graph(lower_source(text))


def _only_edge(text: str) -> DerivedEdge:
    (edge,) = _graph(text).edges
    return edge


# ----------------------------------------------------------- 1. model validators


def _atom(name: str = "a") -> StatusAtom:
    return StatusAtom(job=JobRef(name=name), status="SUCCESS")


def _global_atom(name: str = "FLAG") -> GlobalAtom:
    return GlobalAtom(name=name, op="=", value="1")


def test_derived_edge_assumed_without_assumption_is_rejected() -> None:
    with pytest.raises(ValidationError, match="assumed edge requires a recorded assumption"):
        DerivedEdge(src="a", dst="b", via="success", atom=_atom(), cls="assumed", mapping_row="M01")


def test_derived_edge_exact_with_assumption_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exact edge must not carry an assumption"):
        DerivedEdge(
            src="a",
            dst="b",
            via="failure",
            atom=_atom(),
            cls="exact",
            mapping_row="M04",
            assumption="why",
        )


def test_derived_edge_redesign_allows_assumption_optional() -> None:
    bare = DerivedEdge(
        src="FLAG", dst="b", via="global", atom=_global_atom(), cls="redesign", mapping_row="M16"
    )
    assert bare.assumption is None
    noted = DerivedEdge(
        src="FLAG",
        dst="b",
        via="global",
        atom=_global_atom(),
        cls="redesign",
        mapping_row="M16",
        assumption="note",
    )
    assert noted.assumption == "note"


def test_derived_edge_via_must_match_the_atom_kind() -> None:
    """DL-73: global iff GlobalAtom, exitcode iff ExitCodeAtom -- the pair the
    UC backend narrows on to read op/value off the edge."""
    with pytest.raises(ValidationError, match="via 'global' does not match atom kind 'status'"):
        DerivedEdge(src="a", dst="b", via="global", atom=_atom(), cls="redesign", mapping_row="M16")
    with pytest.raises(ValidationError, match="via 'success' does not match atom kind 'global'"):
        DerivedEdge(
            src="FLAG",
            dst="b",
            via="success",
            atom=_global_atom(),
            cls="redesign",
            mapping_row="M16",
        )
    with pytest.raises(ValidationError, match="via 'exitcode' does not match atom kind 'status'"):
        DerivedEdge(
            src="a", dst="b", via="exitcode", atom=_atom(), cls="redesign", mapping_row="M16"
        )


# --------------------------------------------------------------------- 2. BoxTree


def test_box_tree_roots_children_parent_from_sem10_box_basic() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    tree = derive_graph(catalog).box_tree
    assert tree.roots == ["box_a"]
    assert tree.children == {"box_a": ["job_a", "job_b"]}
    assert tree.parent == {"job_a": "box_a", "job_b": "box_a"}


def test_box_tree_top_for_a_member_and_for_the_top_level_box_itself() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    tree = derive_graph(catalog).box_tree
    assert tree.top("job_a") == "box_a"  # member -> its box
    assert tree.top("job_b") == "box_a"
    assert tree.top("box_a") == "box_a"  # top-level box -> itself


_NESTED_BOX_JIL = (
    "insert_job: outer_box\njob_type: b\n\n"
    "insert_job: inner_box\njob_type: b\nbox_name: outer_box\n\n"
    "insert_job: leaf_job\njob_type: c\ncommand: x\nmachine: m1\nbox_name: inner_box\n\n"
    "insert_job: lonely_job\njob_type: c\ncommand: y\nmachine: m1\n"
)


def test_box_tree_top_walks_a_nested_box_chain_to_the_outermost_root() -> None:
    tree = derive_graph(lower_source(_NESTED_BOX_JIL)).box_tree
    assert tree.top("leaf_job") == "outer_box"
    # inner_box is itself a box (has members) but is ALSO a member of outer_box;
    # its own top is the outer root, not itself.
    assert tree.top("inner_box") == "outer_box"
    assert tree.top("outer_box") == "outer_box"


def test_box_tree_top_is_none_for_a_boxless_job() -> None:
    tree = derive_graph(lower_source(_NESTED_BOX_JIL)).box_tree
    assert tree.top("lonely_job") is None


# ------------------------------------------------- 3. pass 1+3 classification matrix


def test_m01_producer_and_consumer_share_one_trigger_cadence() -> None:
    text = (
        "insert_job: prod_m01\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m01\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(prod_m01)\n"
    )
    edge = _only_edge(text)
    assert (edge.src, edge.dst, edge.via) == ("prod_m01", "cons_m01", "success")
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M01"
    assert edge.lookback is None
    assert edge.assumption is not None and "cross-run staleness" in edge.assumption


def test_m02_cross_stream_different_trigger_cadence() -> None:
    text = (
        "insert_job: prod_m02\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m02\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "11:00"\n'
        "condition: s(prod_m02)\n"
    )
    edge = _only_edge(text)
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M02"
    assert edge.assumption is not None and "Task Monitor" in edge.assumption


def test_m02_undefined_producer() -> None:
    text = (
        "insert_job: cons_m02_undef\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(does_not_exist)\n"
    )
    edge = _only_edge(text)
    assert edge.src == "does_not_exist"
    # redesign, not assumed (DL-12): compiling an A-row edge to a nonexistent
    # vertex would be silent loss; L001 carries the error finding.
    assert edge.cls == "redesign"
    assert edge.mapping_row == "M02"
    assert edge.assumption is not None and "not defined in the catalog" in edge.assumption


def test_m03_lookback_window_and_zero_forms() -> None:
    text = (
        "insert_job: prod_m03\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_m03_window\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(prod_m03, 2)\n\n"
        "insert_job: cons_m03_zero\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(prod_m03, 0)\n"
    )
    by_dst = {e.dst: e for e in _graph(text).edges}

    window = by_dst["cons_m03_window"]
    assert window.mapping_row == "M03"
    assert window.lookback is not None and window.lookback.kind == "window"
    assert window.assumption is not None and "Q2" not in window.assumption

    zero = by_dst["cons_m03_zero"]
    assert zero.mapping_row == "M03"
    assert zero.lookback is not None and zero.lookback.kind == "zero"
    assert zero.assumption is not None and "Q2" in zero.assumption  # zero-lookback anchoring (Q2)


def test_m04_same_cadence_failure_atom_carries_the_staleness_assumption() -> None:
    """DL-153: the class follows M01, so a same-stream f() is A with M01's
    staleness assumption (a stale FAILURE from a previous cycle satisfies
    f(), P-M04). The producer is scheduled and the consumer inherits its
    cadence as its sole condition predecessor, so the cadence flavor
    applies."""
    text = (
        "insert_job: prod_m04\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m04\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(prod_m04)\n"
    )
    edge = _only_edge(text)
    assert edge.via == "failure"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M04"
    assert edge.assumption is not None
    assert "share one trigger cadence" in edge.assumption
    assert "cross-run staleness" in edge.assumption


def test_m04_same_box_failure_atom_carries_the_box_staleness_assumption() -> None:
    """DL-153: the box half of the same-cycle detector (DL-12) picks the
    box flavor of the staleness assumption -- one box run is one cycle, no
    cadence needed, and the latch still outlives the run (P-M04)."""
    text = (
        "insert_job: bx_m04\njob_type: b\n\n"
        "insert_job: bm_prod\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx_m04\n\n"
        "insert_job: bm_cons\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bx_m04\n"
        "condition: f(bm_prod)\n"
    )
    edge = _only_edge(text)
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M04"
    assert edge.assumption is not None
    assert "one top-level box" in edge.assumption
    assert "cross-run staleness" in edge.assumption


def test_m04_cross_stream_failure_atom_is_assumed() -> None:
    """DL-153: a cross-cadence f() is a latched-FAILURE watcher (SEM-01/02).
    UC evaluates edges within the workflow instance only (UCS-13), so the
    edge arrives A with the Task-Monitor assumption recorded -- never a
    silent plain Failure edge."""
    text = (
        "insert_job: prod_m04x\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m04x\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "11:00"\n'
        "condition: f(prod_m04x)\n"
    )
    edge = _only_edge(text)
    assert edge.via == "failure"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M04"
    assert edge.assumption is not None
    assert "Task Monitor" in edge.assumption and "Failed" in edge.assumption
    assert "UCS-06" in edge.assumption


def test_m04_undefined_producer_stays_m02_redesign() -> None:
    """DL-153 leaves the M02-R early exit alone: f() on an undefined
    producer refuses before the discriminator runs (DL-12, SEM-06)."""
    text = (
        "insert_job: cons_m04_undef\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: f(ghost_m04)\n"
    )
    edge = _only_edge(text)
    assert edge.cls == "redesign"
    assert edge.mapping_row == "M02"


def test_m05_same_cadence_done_atom_carries_the_staleness_assumption() -> None:
    """DL-153: same split as M04 -- a same-stream d() is A with M01's
    staleness assumption (cadence inherited, as in the M04 test above)."""
    text = (
        "insert_job: prod_m05\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m05\njob_type: c\ncommand: y\nmachine: m1\ncondition: d(prod_m05)\n"
    )
    edge = _only_edge(text)
    assert edge.via == "done"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M05"
    assert edge.assumption is not None
    assert "share one trigger cadence" in edge.assumption
    assert "cross-run staleness" in edge.assumption


def test_m05_cross_stream_done_atom_is_assumed() -> None:
    """DL-153: cross-cadence d() watches any completion, latched (SEM-01/02);
    same A split as M04."""
    text = (
        "insert_job: prod_m05x\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
        "insert_job: cons_m05x\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "11:00"\n'
        "condition: d(prod_m05x)\n"
    )
    edge = _only_edge(text)
    assert edge.via == "done"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M05"
    assert edge.assumption is not None
    assert "Task Monitor" in edge.assumption and "any completion" in edge.assumption


def test_m06_terminated_atom_is_assumed() -> None:
    text = (
        "insert_job: prod_m06\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_m06\njob_type: c\ncommand: y\nmachine: m1\ncondition: t(prod_m06)\n"
    )
    edge = _only_edge(text)
    assert edge.via == "terminated"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M06"
    assert edge.assumption is not None and "Cancelled" in edge.assumption


def test_m08_exitcode_comparison_is_assumed() -> None:
    text = (
        "insert_job: prod_m08\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_m08\njob_type: c\ncommand: y\nmachine: m1\ncondition: e(prod_m08) > 5\n"
    )
    edge = _only_edge(text)
    assert edge.via == "exitcode"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M08"


def test_m09_global_atom_in_condition_is_assumed_via_global() -> None:
    text = (
        "insert_job: cons_m09\njob_type: c\ncommand: y\nmachine: m1\ncondition: v(SOME_FLAG) = 1\n"
    )
    edge = _only_edge(text)
    assert edge.src == "SOME_FLAG"  # the global variable's name, not a job
    assert edge.dst == "cons_m09"
    assert edge.via == "global"
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M09"


def test_m15_box_success_member_ref_is_assumed() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    graph = derive_graph(catalog)
    (edge,) = [e for e in graph.edges if e.mapping_row == "M15"]
    assert (edge.src, edge.dst, edge.via) == ("job_a", "box_a", "success")
    assert edge.cls == "assumed"


def test_m16_box_success_non_member_ref_is_redesign() -> None:
    text = (
        "insert_job: gate\njob_type: b\nbox_success: s(outsider)\n\n"
        "insert_job: member_x\njob_type: c\ncommand: x\nmachine: m1\nbox_name: gate\n\n"
        "insert_job: outsider\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    edge = _only_edge(text)
    assert (edge.src, edge.dst, edge.via) == ("outsider", "gate", "success")
    assert edge.cls == "redesign"
    assert edge.mapping_row == "M16"


def test_m33_cross_instance_condition_ref_is_redesign_and_external_boundary() -> None:
    text = (
        "insert_job: cons_m33\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_ext^PRD)\n"
    )
    graph = _graph(text)
    (edge,) = graph.edges
    assert edge.src == "prod_ext^PRD"
    assert edge.dst == "cons_m33"
    assert edge.cls == "redesign"
    assert edge.mapping_row == "M33"
    assert graph.external_boundary == [JobRef(name="prod_ext", instance="PRD")]


def test_undefined_producer_with_lookback_prefers_m02_over_m03() -> None:
    """Documented precedence (derive.py's _classify_condition_edge): the
    undefined-producer check runs before the lookback check, so a lookback
    qualifier on a dangling reference is still M02, never M03."""
    text = (
        "insert_job: cons_undef_lookback\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(ghost_producer, 2)\n"
    )
    edge = _only_edge(text)
    assert edge.mapping_row == "M02"
    assert edge.lookback is not None  # the token still parses/is recorded; just not decisive


# ------------------------------------------------------------- 4. pass 2 mutex (m07_mutex.jil)


def test_m07_mutex_groups_from_the_fixture() -> None:
    catalog = lower_catalog([parse_file(M07_MUTEX)])
    graph = derive_graph(catalog)
    assert graph.mutex_groups == [["mutex_a", "mutex_b"], ["mutex_serial"]]


def test_m07_mutex_refs_produce_no_edges_only_the_feeder_edge_remains() -> None:
    catalog = lower_catalog([parse_file(M07_MUTEX)])
    graph = derive_graph(catalog)
    assert all(e.via != "notrunning" for e in graph.edges)
    (edge,) = graph.edges  # mutex_a's and mutex_serial's n() refs never become edges
    assert (edge.src, edge.dst, edge.via) == ("mutex_feeder", "mutex_b", "success")
    assert edge.mapping_row == "M01"  # still classified despite the sibling n() atom


def test_n_cross_instance_ref_stays_an_edge_not_a_mutex_pair() -> None:
    text = "insert_job: cons_xinst\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(other^PRD)\n"
    graph = _graph(text)
    assert graph.mutex_groups == []
    (edge,) = graph.edges
    assert edge.via == "notrunning"
    assert edge.cls == "redesign"
    assert edge.mapping_row == "M33"
    assert edge.src == "other^PRD"


def test_n_under_box_success_stays_an_edge_not_a_mutex_pair() -> None:
    text = (
        "insert_job: gate2\njob_type: b\nbox_success: n(member_y)\n\n"
        "insert_job: member_y\njob_type: c\ncommand: x\nmachine: m1\nbox_name: gate2\n"
    )
    graph = _graph(text)
    assert graph.mutex_groups == []
    (edge,) = graph.edges
    assert edge.via == "notrunning"
    assert edge.mapping_row == "M15"  # member ref -> completion predicate, not a start gate
    assert edge.cls == "assumed"


# --------------------------------------------------------- 5. same-cycle detector details


def test_box_siblings_are_same_cycle_regardless_of_schedule() -> None:
    text = (
        "insert_job: box_x\njob_type: b\n\n"
        "insert_job: sib_a\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_x\n\n"
        "insert_job: sib_b\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_x\n"
        "condition: s(sib_a)\n"
    )
    edge = _only_edge(text)
    assert edge.mapping_row == "M01"  # one box run == one cycle (ir-design ss5)


def test_m01_assumption_names_the_half_of_the_detector_that_fired() -> None:
    """DL-151: the M01 assumption used to claim a shared trigger cadence for
    every same-cycle pair. Two unscheduled box siblings have NO cadence, so
    the record asserted a fact the catalog does not carry. The box half and
    the cadence half now state their own reason."""
    boxed = _only_edge(
        "insert_job: box_z\njob_type: b\n\n"
        "insert_job: sib_p\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_z\n\n"
        "insert_job: sib_q\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_z\n"
        "condition: s(sib_p)\n"
    )
    assert boxed.mapping_row == "M01"
    assert boxed.assumption is not None
    assert "members of one top-level box" in boxed.assumption
    assert "trigger cadence" not in boxed.assumption
    cadenced = _only_edge(
        "insert_job: tick_p\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n\n'
        "insert_job: tick_q\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
        "condition: s(tick_p)\n"
    )
    assert cadenced.mapping_row == "M01"
    assert cadenced.assumption is not None
    assert "share one trigger cadence" in cadenced.assumption


def test_member_inherits_box_cadence_and_it_propagates_to_a_condition_consumer() -> None:
    text = (
        "insert_job: box_y\njob_type: b\ndate_conditions: 1\ndays_of_week: all\n"
        'start_times: "09:00"\n\n'
        "insert_job: mem_no_sched\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_y\n\n"
        "insert_job: cons_inherit\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(mem_no_sched)\n"
    )
    edge = _only_edge(text)
    assert (edge.src, edge.dst) == ("mem_no_sched", "cons_inherit")
    # Both sides resolve to box_y's cadence signature, but the producer lives
    # inside a box and the consumer outside it: boxes are streams (DL-12,
    # M14 note), so a signature collision is NOT same-cycle -> M02.
    assert edge.mapping_row == "M02"
    assert edge.assumption is not None and "cross-stream" in edge.assumption


def test_consumer_chain_cadence_inherits_through_the_fixpoint() -> None:
    text = (
        "insert_job: chain_a\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n\n'
        "insert_job: chain_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(chain_a)\n\n"
        "insert_job: chain_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(chain_b)\n"
    )
    by_dst = {e.dst: e for e in _graph(text).edges}
    assert by_dst["chain_b"].mapping_row == "M01"
    assert by_dst["chain_c"].mapping_row == "M01"  # inherited two hops through the fixpoint


def test_unknown_cadence_pair_classifies_as_m02() -> None:
    text = (
        "insert_job: unk_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: unk_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(unk_prod)\n"
    )
    edge = _only_edge(text)
    assert edge.mapping_row == "M02"  # unknown-vs-anything is conservative, never M01


def test_fw_producer_source_cadence_propagates_to_an_unscheduled_consumer() -> None:
    text = (
        "insert_job: fw_prod\njob_type: f\nwatch_file: /tmp/f\nmachine: m1\n\n"
        "insert_job: fw_cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(fw_prod)\n"
    )
    edge = _only_edge(text)
    assert edge.mapping_row == "M01"  # a file watcher is its own source cadence


# ---------------------------------------------------------------- 6. pass 4 OR shapes


def test_or_shape_common_ancestor_diamond() -> None:
    text = (
        "insert_job: root\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b1\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(root)\n\n"
        "insert_job: b2\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(root)\n\n"
        "insert_job: consumer_diamond\njob_type: c\ncommand: w\nmachine: m1\n"
        "condition: s(b1) | s(b2)\n"
    )
    (shape,) = _graph(text).or_shapes
    assert shape.job == "consumer_diamond"
    assert shape.kind == "common_ancestor"
    assert shape.branches == [["b1"], ["b2"]]
    assert "root" in shape.lowering


def test_or_shape_independent_producers() -> None:
    text = (
        "insert_job: indep_p1\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: indep_p2\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: indep_consumer\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: s(indep_p1) | s(indep_p2)\n"
    )
    (shape,) = _graph(text).or_shapes
    assert shape.kind == "independent"


def test_or_shape_mixed_when_one_branch_has_only_a_global_atom() -> None:
    text = (
        "insert_job: mixed_p1\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: mixed_consumer\njob_type: c\ncommand: b\nmachine: m1\n"
        "condition: s(mixed_p1) | v(FLAG) = 1\n"
    )
    (shape,) = _graph(text).or_shapes
    assert shape.kind == "mixed"
    assert shape.branches == [["mixed_p1"], []]


def test_or_shape_nested_under_and_is_found() -> None:
    text = (
        "insert_job: nested_p_x\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: nested_a\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: nested_b\njob_type: c\ncommand: cc\nmachine: m1\n\n"
        "insert_job: nested_consumer\njob_type: c\ncommand: d\nmachine: m1\n"
        "condition: s(nested_p_x) & (s(nested_a) | s(nested_b))\n"
    )
    shapes = _graph(text).or_shapes
    assert len(shapes) == 1
    assert shapes[0].job == "nested_consumer"
    assert shapes[0].branches == [["nested_a"], ["nested_b"]]
    assert shapes[0].kind == "independent"  # nested_p_x is a sibling And operand, not an ancestor


def test_and_only_condition_has_no_or_shapes() -> None:
    text = (
        "insert_job: and_only_p1\njob_type: c\ncommand: a\nmachine: m1\n\n"
        "insert_job: and_only_p2\njob_type: c\ncommand: b\nmachine: m1\n\n"
        "insert_job: and_only_consumer\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: s(and_only_p1) & s(and_only_p2)\n"
    )
    assert _graph(text).or_shapes == []


# ---------------------------------------------------------------------- 7. pass 6


def test_run_window_job_gets_an_m27_redesign_flag() -> None:
    catalog = lower_catalog([parse_file(SEM30_SCHEDULE)])
    graph = derive_graph(catalog)
    assert len(graph.redesign_flags) == 1
    (flag,) = graph.redesign_flags
    assert flag.job == "quarter_past"  # the run_window job; test_must_start_complete has none
    assert flag.mapping_row == "M27"


def test_no_run_window_means_no_redesign_flags() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    graph = derive_graph(catalog)
    assert graph.redesign_flags == []


# ------------------------------------------------------------ 8. pass 7 structural


def test_chain_of_three_local_condition_edges() -> None:
    text = (
        "insert_job: lin_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: lin_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(lin_a)\n\n"
        "insert_job: lin_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(lin_b)\n"
    )
    assert _graph(text).chains == [["lin_a", "lin_b", "lin_c"]]


def test_diamond_has_no_chain_but_parallel_groups_catches_the_siblings() -> None:
    text = (
        "insert_job: dia_root\njob_type: c\ncommand: r\nmachine: m1\n\n"
        "insert_job: dia_b1\njob_type: c\ncommand: a\nmachine: m1\ncondition: s(dia_root)\n\n"
        "insert_job: dia_b2\njob_type: c\ncommand: b\nmachine: m1\ncondition: s(dia_root)\n\n"
        "insert_job: dia_join\njob_type: c\ncommand: c\nmachine: m1\n"
        "condition: s(dia_b1) & s(dia_b2)\n"
    )
    graph = _graph(text)
    assert graph.chains == []  # the root's fan-out and the join's fan-in both break linearity
    assert graph.parallel_groups == [["dia_b1", "dia_b2"]]


def test_two_cycle_detected_as_one_sorted_scc() -> None:
    text = (
        "insert_job: cyc_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cyc_b)\n\n"
        "insert_job: cyc_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cyc_a)\n"
    )
    assert _graph(text).cycles == [["cyc_a", "cyc_b"]]


def test_self_referencing_condition_is_a_self_loop_cycle() -> None:
    """n(self) is mutex, never an edge (module docstring); s(self) IS a
    genuine self-loop edge, and self-loops count as cycles (_cycles)."""
    text = (
        "insert_job: self_loop_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(self_loop_job)\n"
    )
    assert _graph(text).cycles == [["self_loop_job"]]


def test_no_cycles_over_the_whole_corpus() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert derive_graph(catalog).cycles == []


# --------------------------------------------------------------- 9. whole corpus


def test_whole_corpus_exact_edge_count_and_mapping_row_counter() -> None:
    """Pins the exact numbers so a regression in any classifier surfaces
    immediately. Recomputed empirically after adding m07_mutex.jil (1 new
    edge: mutex_feeder->mutex_b, M01) and sem12_external_gate.jil (2 new
    edges: the box_success/box_failure M16 pair) -- see the fixture-specific
    tests above for the per-edge reasoning. sem24_status_resource.jil
    (DL-18) adds 1 M02 edge: the plain s(SEED_C) gate on the top box --
    producer and consumer are both unscheduled, so the same-cycle detector
    conservatively classifies the latch as cross-stream (M02), not M01.
    DL-38's fold_t003_or_join.jil and fold_t004_typed_links.jil add 18 more
    edges (fold_t006_resources.jil/fold_t007_schedules.jil carry no
    `condition:` at all, so they add none): the two fan-out+OR-join groups
    in fold_t003_or_join.jil are 10 same-cadence success edges (+10 M01);
    fold_t004_typed_links.jil's uniform f-chain, uniform d-fan-out, and
    mixed s/f/f/d chain add 4 M04 (failure) edges and 3 M05 (done) edges
    (A-class with the cadence staleness flavor since DL-153 -- each chain
    shares one inherited cadence), and 1 more same-cadence M01 success edge; names_colon_join.jil
    (DL-39) adds 1 M01 success edge between colon-named jobs.
    l020_iced_consumer.jil (DL-151) adds 3 M02 edges: its four jobs are all
    unscheduled and unboxed, so every s() latch there is cross-stream."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    graph = derive_graph(catalog)
    assert len(graph.edges) == 40
    assert Counter(e.mapping_row for e in graph.edges) == Counter(
        {"M01": 13, "M02": 11, "M04": 4, "M05": 3, "M09": 2, "M03": 2, "M33": 2, "M16": 2, "M15": 1}
    )


def test_whole_corpus_mutex_groups_boundary_and_redesign_flags() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    graph = derive_graph(catalog)
    assert graph.mutex_groups == [
        ["etl:load", "etl:probe"],  # names_colon_join.jil (DL-39)
        ["mutex_a", "mutex_b"],
        ["mutex_serial"],
    ]
    assert graph.external_boundary == [
        JobRef(name="also_missing", instance="PRD"),  # sem06_dangling.jil
        JobRef(name="DB_BACKUP", instance="PRD"),  # torture_colon.jil
    ]
    # sink_cmd joined with kitchen_sink.jil (DL-37): its run_window is part
    # of the every-typed-lane coverage and correctly draws the M27 flag.
    assert [(f.job, f.mapping_row) for f in graph.redesign_flags] == [
        ("sink_cmd", "M27"),
        ("quarter_past", "M27"),
    ]
    assert graph.cycles == []


def _chain_catalog(n: int, *, reverse: bool = False) -> CatalogIR:
    blocks = ["insert_job: c0\njob_type: c\ncommand: x\nmachine: m\n"]
    blocks += [
        f"insert_job: c{i}\njob_type: c\ncommand: x\nmachine: m\ncondition: s(c{i - 1})\n"
        for i in range(1, n)
    ]
    if reverse:
        blocks.reverse()
    return lower_source("\n".join(blocks))


def test_dl20_reverse_declared_chain_derives_without_recursion() -> None:
    """DL-20: ancestor walks are iterative -- declaration order must never
    decide between success and RecursionError (autorep exports order freely).
    3000 exceeds the default Python recursion limit with margin."""
    graph = derive_graph(_chain_catalog(3000, reverse=True))
    assert len(graph.edges) == 2999


def test_dl20_chain_catalog_memory_stays_linear() -> None:
    """DL-20: the old whole-catalog ancestor sets were Theta(n^2) memory on
    chains (the `dsl41 lint` OOM kill: 741MB peak at n=5000). With ancestors
    computed only for Or-branch producers, a 2000-chain with no `|` must
    derive well under the quadratic footprint; the bound is generous so the
    test only fails if the quadratic pass comes back."""
    import tracemalloc

    catalog = _chain_catalog(2000)
    tracemalloc.start()
    derive_graph(catalog)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 60_000_000  # quadratic ancestors alone were ~200MB at n=2000


def test_dl20_ancestor_sets_complete_on_cycles_and_only_for_roots() -> None:
    """The iterative closure is complete on cyclic graphs regardless of visit
    order (the old memoized recursion returned incomplete mid-cycle sets)
    and computes nothing for nodes that were not requested."""
    preds = {"a": {"c"}, "b": {"a"}, "c": {"b"}, "solo": set[str]()}
    out = _ancestor_sets(["b"], preds)
    assert out == {"b": {"a", "b", "c"}}


def test_whole_corpus_derivation_is_deterministic() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    first = derive_graph(catalog).model_dump_json()
    second = derive_graph(catalog).model_dump_json()
    assert first == second


# ------------------------------------------------------- 10. graph lint rules L008-L014


def test_l008_fires_twice_on_sem12_external_gate() -> None:
    catalog = lower_catalog([parse_file(SEM12_EXTERNAL_GATE)])
    graph = derive_graph(catalog)
    violations = rule_l008(catalog, graph)
    assert len(violations) == 2
    by_detail = {v.detail: v for v in violations}
    assert set(by_detail) == {"gate_outside_job", "ABORT_FLAG"}  # outside-job ref + global ref
    assert all(v.jobs == ["gate_box"] and v.severity == "warn" for v in violations)


def test_l008_quiet_on_sem10_box_basic_member_ref() -> None:
    catalog = lower_catalog([parse_file(SEM10_BOX_BASIC)])
    graph = derive_graph(catalog)
    assert rule_l008(catalog, graph) == []  # M15 (member ref) is the legitimate early-exit shape


def test_l008_names_a_cross_instance_gate_by_the_atom_fact_not_the_display_shape() -> None:
    """DL-175 (found by its own adversarial review): the external-instance
    wording used to read `"^" in edge.src` -- accidentally sound today
    (box_success/box_failure share the condition grammar's JOB_NAME, which
    bans `^`), but a string-shape decider is the same defect class as
    everywhere else in this codebase, and no test exercised this wording at
    all before this one. Now reads the atom's `instance` fact."""
    text = (
        "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
        "insert_job: l8bx\njob_type: b\nbox_success: s(l8ext^PRD)\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l008(catalog, derive_graph(catalog))
    assert "'l8ext^PRD' on an external instance" in violation.message


def test_l009_fires_on_consumer_stale_in_sem04_lookback() -> None:
    catalog = lower_catalog([parse_file(SEM04_LOOKBACK)])
    graph = derive_graph(catalog)
    (violation,) = rule_l009(catalog, graph)
    assert violation.jobs == ["consumer_stale"]  # the fixture's documented purpose
    assert violation.detail == "upstream_daily"


def test_l009_quiet_when_the_consumer_is_unscheduled() -> None:
    text = (
        "insert_job: prod_l009\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_l009\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod_l009)\n"
    )
    catalog = lower_source(text)
    assert rule_l009(catalog, derive_graph(catalog)) == []


def test_l009_quiet_when_the_atom_carries_a_lookback() -> None:
    text = (
        "insert_job: prod_l009b\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons_l009_lookback\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(prod_l009b, 2)\n"
    )
    catalog = lower_source(text)
    assert rule_l009(catalog, derive_graph(catalog)) == []


def test_l010_fires_on_an_inline_cycle_message_names_the_arrow_path() -> None:
    text = (
        "insert_job: cyc_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cyc_b)\n\n"
        "insert_job: cyc_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cyc_a)\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l010(catalog, derive_graph(catalog))
    assert "->" in violation.message


_L011_QUIET_CASES: list[tuple[str, str]] = [
    (
        "scheduled-job",
        "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n',
    ),
    (
        "box-and-its-member",
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n",
    ),
    (
        "fw-job",
        "insert_job: fwj\njob_type: f\nwatch_file: /tmp/f\nmachine: m1\n",
    ),
    (
        "mutex-participant",
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx2)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx1)\n",
    ),
    (
        "edge-src-and-dst",
        "insert_job: es1\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: es2\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(es1)\n",
    ),
]


@pytest.mark.parametrize(
    "text", [c[1] for c in _L011_QUIET_CASES], ids=[c[0] for c in _L011_QUIET_CASES]
)
def test_l011_quiet_for_wired_or_otherwise_accounted_jobs(text: str) -> None:
    catalog = lower_source(text)
    assert rule_l011(catalog, derive_graph(catalog)) == []


def test_l011_fires_for_a_bare_unwired_job() -> None:
    text = "insert_job: bare_j\njob_type: c\ncommand: x\nmachine: m1\n"
    catalog = lower_source(text)
    (violation,) = rule_l011(catalog, derive_graph(catalog))
    assert violation.jobs == ["bare_j"]
    assert violation.severity == "warn"


#: DL-175 (the S-EDGE class): a local dangling job spelled exactly like a
#: cross-instance display form, and a second local job whose producer is
#: genuinely on that instance. The same text (kept local, not imported) also
#: appears in test_viz.py and test_backend_uc.py.
S_EDGE_TEXT = (
    "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
    "insert_job: foo^PRD\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: bar\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(foo^PRD)\n"
)


def test_l011_dl175_a_local_dangler_is_not_shielded_by_a_cross_instance_display_form() -> None:
    """DL-162a named this a live false negative: `wired.add(edge.src)`
    unconditionally added `bar`'s M33 edge's display form "foo^PRD" -- the
    SAME string as the local, genuinely dangling job's own name -- so L011
    never fired on it. `local_producer` resolves None for the M33 edge (its
    atom's instance is PRD, not this catalog), so the local job is not
    wired and L011 catches it (DL-175)."""
    catalog = lower_source(S_EDGE_TEXT)
    graph = derive_graph(catalog)
    (edge,) = graph.edges
    assert edge.mapping_row == "M33" and edge.src == "foo^PRD"
    (violation,) = rule_l011(catalog, graph)
    assert violation.jobs == ["foo^PRD"]
    # the collision itself, still present -- what the fix stops believing:
    assert edge.src in catalog.jobs


def test_l012_fires_per_group_from_m07_mutex_self_exclusion_mentions_instance_wait() -> None:
    catalog = lower_catalog([parse_file(M07_MUTEX)])
    graph = derive_graph(catalog)
    violations = rule_l012(catalog, graph)
    assert len(violations) == 2  # one per group: the pair, and the self-exclusion
    assert all(v.severity == "info" for v in violations)
    by_jobs = {tuple(v.jobs): v for v in violations}
    assert set(by_jobs) == {("mutex_a", "mutex_b"), ("mutex_serial",)}
    assert "Instance Wait" in by_jobs[("mutex_serial",)].message


def test_l013_fires_on_a_box_member_with_its_own_schedule() -> None:
    text = (
        "insert_job: boxc\njob_type: b\n\n"
        "insert_job: sched_member\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxc\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    )
    catalog = lower_source(text)
    (violation,) = rule_l013(catalog, derive_graph(catalog))
    assert violation.jobs == ["sched_member"]
    assert violation.detail == "boxc"


def test_l013_quiet_on_a_plain_member_with_no_schedule() -> None:
    text = (
        "insert_job: boxd\njob_type: b\n\n"
        "insert_job: plain_member\njob_type: c\ncommand: x\nmachine: m1\nbox_name: boxd\n"
    )
    catalog = lower_source(text)
    assert rule_l013(catalog, derive_graph(catalog)) == []


def test_l014_fires_on_case_colliding_names() -> None:
    text = (
        "insert_job: JobA\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: joba\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l014(catalog, derive_graph(catalog))
    assert violation.severity == "error"
    assert set(violation.jobs) == {"JobA", "joba"}


def test_l014_quiet_on_the_whole_corpus() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    assert rule_l014(catalog, derive_graph(catalog)) == []


def test_l020_fires_when_every_immediate_predecessor_translates_to_skip() -> None:
    """M19 (DL-151, Part II requirement 3): an iced producer satisfies its
    atoms in AutoSys, so the consumer runs; UC cascades the skip onto it."""
    text = (
        "insert_job: icy\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: skipped_path\njob_type: c\ncommand: y\nmachine: m1\n"
        "status: ON_NOEXEC\n\n"
        "insert_job: downstream\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(icy) & s(skipped_path)\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l020(catalog, derive_graph(catalog))
    assert violation.code == "L020"
    assert violation.severity == "warn"
    assert violation.jobs == ["downstream"]
    assert violation.detail == "icy,skipped_path"
    assert "UCS-02" in violation.message


def test_l020_quiet_when_one_predecessor_still_runs() -> None:
    """One live predecessor converges: UC skips only the iced branch and the
    consumer still runs, so the rule needs ALL of them (M19 note)."""
    text = (
        "insert_job: icy2\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: livewire\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: mixed_cons\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(icy2) & s(livewire)\n"
    )
    catalog = lower_source(text)
    assert rule_l020(catalog, derive_graph(catalog)) == []


def test_l020_quiet_on_a_held_predecessor_and_on_a_box_override_ref() -> None:
    """ON_HOLD is M20: it blocks downstream on BOTH sides, so it is not this
    divergence. A box_success reference is a completion predicate on the
    box, never a start gate, so it does not make the box an iced consumer."""
    held = lower_source(
        "insert_job: onhold\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_HOLD\n\n"
        "insert_job: after_hold\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(onhold)\n"
    )
    assert rule_l020(held, derive_graph(held)) == []
    override = lower_source(
        "insert_job: icebox\njob_type: b\nbox_success: s(frozen)\n\n"
        "insert_job: frozen\njob_type: c\ncommand: x\nmachine: m1\n"
        "box_name: icebox\nstatus: ON_ICE\n"
    )
    assert rule_l020(override, derive_graph(override)) == []


def test_l020_quiet_when_the_consumer_itself_translates_to_skip() -> None:
    """A consumer inserted ON_ICE is skipped on BOTH sides, so there is no
    cascade divergence to flag -- the rule exists for a consumer AutoSys
    would run (review follow-up)."""
    text = (
        "insert_job: icy3\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: icy_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "status: ON_ICE\ncondition: s(icy3)\n"
    )
    catalog = lower_source(text)
    assert rule_l020(catalog, derive_graph(catalog)) == []


def test_l020_fires_once_on_the_corpus_fixture() -> None:
    """The house rule: one corpus fixture that trips the rule and one that
    does not -- l020_iced_consumer.jil carries both (l20_consumer trips,
    l20_mixed does not)."""
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    (violation,) = rule_l020(catalog, derive_graph(catalog))
    assert violation.jobs == ["l20_consumer"]


# --------------------------------------------------------- 11. lint_catalog integration


def test_lint_catalog_precomputed_graph_matches_the_recomputed_path() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    graph = derive_graph(catalog)
    assert lint_catalog(catalog, graph) == lint_catalog(catalog)


def test_lint_catalog_report_order_is_irf_rules_block_then_graph_rules_block() -> None:
    catalog = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    codes = [v.code for v in lint_catalog(catalog).violations]
    graph_codes = {code for code, _ in GRAPH_RULES}
    first_graph_index = next(i for i, c in enumerate(codes) if c in graph_codes)
    assert all(c not in graph_codes for c in codes[:first_graph_index])
    assert all(c in graph_codes for c in codes[first_graph_index:])


# ------------------------------------------------ 12. review-driven regressions

# Behaviors fixed below; each test pins the corrected behavior so it cannot
# regress silently.


def test_transitive_box_member_override_is_m15_not_m16() -> None:
    """SEM-12 'inside the box' is transitive: a grandchild referenced by the
    outermost box's box_success is an internal early-exit (M15 assumed), not
    an external hung-RUNNING gate (M16 redesign) -- and no L008."""
    text = (
        "insert_job: outer\njob_type: b\nbox_success: s(grandchild)\n\n"
        "insert_job: middle\njob_type: b\nbox_name: outer\n\n"
        "insert_job: grandchild\njob_type: c\ncommand: x\nmachine: m1\nbox_name: middle\n"
    )
    catalog = lower_source(text)
    (edge,) = derive_graph(catalog).edges
    assert (edge.src, edge.dst) == ("grandchild", "outer")
    assert edge.cls == "assumed"
    assert edge.mapping_row == "M15"
    assert rule_l008(catalog, derive_graph(catalog)) == []


def test_cross_box_identical_cadence_is_m02_not_m01() -> None:
    """Two identically scheduled boxes are two UC workflows: a trigger-
    signature collision across box boundaries is not a shared stream."""
    schedule = 'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    text = (
        f"insert_job: box_p\njob_type: b\n{schedule}\n"
        "insert_job: p1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_p\n\n"
        f"insert_job: box_q\njob_type: b\n{schedule}\n"
        "insert_job: q1\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_q\n"
        "condition: s(p1)\n"
    )
    (edge,) = derive_graph(lower_source(text)).edges
    assert (edge.src, edge.dst) == ("p1", "q1")
    assert edge.mapping_row == "M02"
    assert edge.assumption is not None and "cross-stream" in edge.assumption


def test_same_top_level_box_is_still_same_cycle() -> None:
    text = (
        "insert_job: box_r\njob_type: b\n\n"
        "insert_job: r1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: box_r\n\n"
        "insert_job: r2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: box_r\n"
        "condition: s(r1)\n"
    )
    (edge,) = derive_graph(lower_source(text)).edges
    assert edge.mapping_row == "M01"


def test_lookback_qualified_notrunning_stays_an_edge_with_its_lookback() -> None:
    """n(job, window) is NOT mutex-classified: mutex groups cannot carry the
    qualifier, and dropping it would be unmaterialized loss (ir-design ss1).
    It classifies like any lookback atom (M03) with via=notrunning."""
    text = (
        "insert_job: nq_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: nq_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: n(nq_prod, 00.30)\n"
    )
    graph = derive_graph(lower_source(text))
    assert graph.mutex_groups == []
    (edge,) = graph.edges
    assert edge.via == "notrunning"
    assert edge.mapping_row == "M03"
    assert edge.lookback is not None and edge.lookback.minutes == 30


def test_l009_skips_undefined_producers() -> None:
    """An undefined producer is L001's error; a staleness warning about a
    job that never ran would contradict it."""
    text = (
        "insert_job: sched_cons\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(ghost_producer)\n"
    )
    catalog = lower_source(text)
    assert rule_l009(catalog, derive_graph(catalog)) == []


def test_l008_message_names_the_global_variable_correctly() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem12_external_gate.jil")])
    report = lint_catalog(catalog)
    by_detail = {v.detail: v for v in report.by_code("L008")}
    assert "global variable 'ABORT_FLAG'" in by_detail["ABORT_FLAG"].message
    assert "not one of its members" in by_detail["gate_outside_job"].message
    assert "not one of its members" not in by_detail["ABORT_FLAG"].message


#: Every row a catalog can put on an edge, in one lowerable source: M01 (same
#: cadence s), M02 (cross-stream s), M02-R (undefined producer), M03
#: (lookback), M04 (f), M05 (d), M06 (t), M08 (exit code), M09 (global in a
#: condition), M15 (box_success member ref), M16 (box_success non-member),
#: M16 (box_success global) and M33 (cross-instance ref). `is_start_gate` is
#: asserted over all eleven rows -- the first cut claimed "all ten" and had
#: no f/d/t atom in it at all (DL-162a).
_EVERY_ROW = (
    "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
    "insert_job: sg_prod\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
    "insert_job: sg_same\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    "condition: s(sg_prod)\n\n"
    "insert_job: sg_cross\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\n'
    "condition: s(sg_prod) & s(sg_ghost) & s(sg_prod, 30) & e(sg_prod) = 0"
    " & v(SG_FLAG) = 1 & s(sg_remote^PRD) & f(sg_prod) & d(sg_prod) & t(sg_prod)\n\n"
    "insert_job: sg_box\njob_type: b\nbox_success: s(sg_member)\n"
    "box_failure: s(sg_prod) | v(SG_ABORT) = 1\n\n"
    "insert_job: sg_member\njob_type: c\ncommand: x\nmachine: m1\nbox_name: sg_box\n"
)


def test_is_start_gate_splits_the_derived_row_vocabulary() -> None:
    """The one start-gate predicate (DL-162), over every row a catalog reaches.

    A box override (M15/M16) is a completion predicate on a box, not an
    ordering, and a global (via="global") names no job -- neither gates a
    start. Everything else does, INCLUDING an undefined producer (M02-R) and
    a cross-instance one (M33): those name a job this catalog cannot look up,
    which is a separate question every reader asks separately."""
    graph = _graph(_EVERY_ROW)
    gates = {(e.src, e.dst, e.mapping_row): e.is_start_gate for e in graph.edges}
    assert len(gates) == len(graph.edges)  # no two edges collapse onto one key
    assert gates == {
        ("sg_prod", "sg_same", "M01"): True,
        ("sg_prod", "sg_cross", "M02"): True,
        ("sg_ghost", "sg_cross", "M02"): True,  # undefined producer, redesign
        ("sg_prod", "sg_cross", "M03"): True,  # lookback-qualified
        ("sg_prod", "sg_cross", "M04"): True,  # f(), cross-stream
        ("sg_prod", "sg_cross", "M05"): True,  # d(), cross-stream
        ("sg_prod", "sg_cross", "M06"): True,  # t()
        ("sg_prod", "sg_cross", "M08"): True,  # exit code
        ("SG_FLAG", "sg_cross", "M09"): False,  # global: names no job
        ("sg_remote^PRD", "sg_cross", "M33"): True,  # cross-instance
        ("sg_member", "sg_box", "M15"): False,  # box override, member ref
        ("sg_prod", "sg_box", "M16"): False,  # box override, non-member
        ("SG_ABORT", "sg_box", "M16"): False,  # box override on a global
    }


def test_l009_quiet_on_every_success_edge_that_is_not_a_start_gate() -> None:
    """The DL-162 swap is behavior-neutral, and this states the property
    correctly: within {via==success, no lookback, LOCAL producer this catalog
    defines}, `is_start_gate` and the old `mapping_row in (M01, M02)` test
    agree edge for edge. The rows the predicate has to keep out are box
    overrides on a SCHEDULED box (M15/M16) -- a completion predicate, never a
    stale start gate -- and they are kept out here.

    The first cut asserted something weaker and circular -- that only M01/M02
    are REACHABLE in that slice -- which is false twice over: M15/M16 reach it
    (a `box_success: s(member)` on a scheduled box), and an M33 edge reaches
    it whenever a catalog job's own name collides with the `name^INST`
    spelling. The second one was a live false positive; `local_producer` is
    the fix and this is the assertion that would have caught it (DL-162a)."""
    text = (
        "insert_job: sg2_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: sg2_box\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "box_success: s(sg2_member)\nbox_failure: s(sg2_prod)\n\n"
        "insert_job: sg2_member\njob_type: c\ncommand: x\nmachine: m1\nbox_name: sg2_box\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    assert {e.mapping_row for e in graph.edges} == {"M15", "M16"}
    assert rule_l009(catalog, graph) == []

    corpus = lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])
    checked = 0
    for edge in derive_graph(corpus).edges:
        if edge.via != "success" or edge.lookback is not None:
            continue
        if local_producer(edge, corpus) is None:
            continue
        checked += 1
        assert edge.is_start_gate == (edge.mapping_row in ("M01", "M02")), edge
    assert checked  # an empty slice would make the loop above vacuous


def test_a_job_named_like_a_cross_instance_ref_is_not_its_producer() -> None:
    """DL-162a, found by adversarial review of DL-162. A cross-instance edge's
    `src` is the composite display form `name^INST`, and lowering constrains
    job-name characters nowhere -- so a catalog holding its own job called
    `foo^PRD` made `src in catalog.jobs` answer yes about a producer derive
    had just placed on ANOTHER instance.

    L009 fired a stale-latch warning about a job it also reports as external,
    and L020 would have counted it as an ICEable predecessor. `local_producer`
    reads `instance` off the atom, which is the fact, so both stay quiet."""
    text = (
        "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
        "insert_job: foo^PRD\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: caret_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        "condition: s(foo^PRD)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    (edge,) = graph.edges
    assert edge.mapping_row == "M33" and edge.src == "foo^PRD"
    assert edge.is_start_gate  # it DOES gate the start; it just names no local job
    assert edge.src in catalog.jobs  # the collision itself, still present
    assert local_producer(edge, catalog) is None  # and no longer believed
    assert rule_l009(catalog, graph) == []
    assert rule_l020(catalog, graph) == []


def test_l020_reads_a_global_gate_as_no_predecessor_at_all() -> None:
    """The global half of `is_start_gate`, which no fixture held before
    (DL-162). A consumer gated on one iced job AND a global still has exactly
    one JOB predecessor, and it is iced -- so the M19 divergence is real and
    the rule fires. A global gate cannot rescue it: UC cascades the skip from
    the task edge whatever the variable says."""
    text = (
        "insert_job: sg3_icy\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
        "insert_job: sg3_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(sg3_icy) & v(SG3_FLAG) = 1\n"
    )
    catalog = lower_source(text)
    (violation,) = rule_l020(catalog, derive_graph(catalog))
    assert violation.jobs == ["sg3_cons"]
    assert violation.detail == "sg3_icy"  # the global is not listed as a predecessor


def test_or_shapes_cover_box_overrides() -> None:
    text = (
        "insert_job: or_box\njob_type: b\nbox_success: s(om1) | s(om2)\n\n"
        "insert_job: om1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: or_box\n\n"
        "insert_job: om2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: or_box\n"
    )
    (shape,) = derive_graph(lower_source(text)).or_shapes
    assert shape.job == "or_box"
    assert shape.attr == "box_success"


def test_common_ancestor_excludes_undefined_names() -> None:
    """A diamond whose only common ancestor is undefined cannot anchor a
    restructure (L001 owns the finding) -> independent, not common_ancestor."""
    text = (
        "insert_job: da_b1\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ghost_root)\n\n"
        "insert_job: da_b2\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(ghost_root)\n\n"
        "insert_job: da_join\njob_type: c\ncommand: z\nmachine: m1\n"
        "condition: s(da_b1) | s(da_b2)\n"
    )
    graph = derive_graph(lower_source(text))
    (shape,) = [s for s in graph.or_shapes if s.job == "da_join"]
    assert shape.kind == "independent"


def test_chain_members_inside_a_cycle_are_not_double_reported() -> None:
    text = (
        "insert_job: cyc_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cyc_c)\n\n"
        "insert_job: cyc_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cyc_a)\n\n"
        "insert_job: cyc_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(cyc_b)\n"
    )
    graph = derive_graph(lower_source(text))
    assert graph.cycles == [["cyc_a", "cyc_b", "cyc_c"]]
    assert graph.chains == []


def test_external_boundary_does_not_alias_the_condition_ast() -> None:
    text = (
        "insert_job: xb_cons\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(remote_job^PRD)\n"
    )
    catalog = lower_source(text)
    graph = derive_graph(catalog)
    (boundary_ref,) = graph.external_boundary
    attr = catalog.jobs["xb_cons"].sem.condition
    assert attr is not None
    condition = attr.cond
    assert isinstance(condition, StatusAtom)
    assert boundary_ref == condition.job
    assert boundary_ref is not condition.job  # copied, not aliased


def test_edge_atom_does_not_alias_the_condition_ast() -> None:
    """DL-73: the same pin for DerivedEdge.atom/lookback -- equal to the IR-F
    node it was derived from, never the same object (nor a shared subtree)."""
    text = (
        "insert_job: al_prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: al_cons\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(al_prod, 01\\:00)\n"
    )
    catalog = lower_source(text)
    (edge,) = derive_graph(catalog).edges
    attr = catalog.jobs["al_cons"].sem.condition
    assert attr is not None
    atom = attr.cond
    assert isinstance(atom, StatusAtom)
    assert edge.atom == atom
    assert edge.atom is not atom
    assert edge.atom.job is not atom.job  # deep: the nested JobRef is copied too
    assert edge.lookback == atom.lookback
    assert edge.lookback is not atom.lookback


def test_l011_empty_box_message_names_the_container() -> None:
    text = "insert_job: hollow\njob_type: b\n"
    catalog = lower_source(text)
    (violation,) = rule_l011(catalog, derive_graph(catalog))
    assert "empty box never completes" in violation.message
