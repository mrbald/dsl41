"""Markdown/Mermaid rendering tests (phase 6, DL-35): dsl41.viz per its
module-docstring decisions -- generated ids, thinned edge labels, box
subgraphs + collapse threshold, pseudo-node shapes/classes, mutex links
(pairwise / self-badge / clique hub), FW triggers, schedule digests, the
component split, and the full `to_markdown` report (summary, legend, per-
workflow charts, Locks, Appendices A/B/C) -- plus the `dsl41 viz`
CLI command.

There is no Mermaid parser in the toolchain; validity is pinned
structurally: balanced subgraph/end blocks, id-safe node identifiers (per
chart, including every fence inside a markdown report), and a hand-checked
golden render for one small catalog.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from dsl41.ast_jil import parse_file
from dsl41.cli import app
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.viz import (
    _auto_direction,
    _common_prefix,
    _component_title,
    _incident_nodes,
    _is_standalone,
    split_components,
    to_markdown,
    to_mermaid,
)

CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in sorted(CORPUS_DIR.glob("*.jil")) if p.name not in EXPECT_LOWER_ERROR]

runner = CliRunner()


def corpus_catalog() -> CatalogIR:
    return lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])


def graph_of(text: str) -> DerivedGraph:
    return derive_graph(lower_source(text))


_MERMAID_FENCE = re.compile(r"```mermaid\n(.*?)\n```", re.S)


def _mermaid_fences(markdown: str) -> list[str]:
    """Every fenced Mermaid body in a markdown report, in document order
    (index 0 is always the legend)."""
    return _MERMAID_FENCE.findall(markdown)


def _assert_ids_are_generated(mermaid: str) -> None:
    """Every node/edge line starts with a generated `n<digit>` id; style
    lines (classDef/class/subgraph/end/linkStyle) are exempt."""
    for line in mermaid.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith(("classDef", "class ", "subgraph", "end", "linkStyle")):
            continue
        assert re.match(r"n\d+\b", stripped), line


def _assert_subgraphs_balance(mermaid: str) -> None:
    opens = [ln for ln in mermaid.splitlines() if ln.strip().startswith("subgraph")]
    ends = [ln for ln in mermaid.splitlines() if ln.strip() == "end"]
    assert len(opens) == len(ends)


# ------------------------------------------------------------------ golden render


def test_golden_small_catalog() -> None:
    """Hand-checked render pinning the full output shape for one catalog:
    a box subgraph with an assumed M15 override edge (label THINNED away --
    via==success, no lookback, not redesign), an exact M04 edge, and a
    redesign M33 edge with its external pseudo-node (now a hexagon) plus
    the redesign linkStyle line."""
    text = (
        "insert_job: bx\njob_type: b\nbox_success: s(m1)\n\n"
        "insert_job: m1\njob_type: c\ncommand: a\nmachine: h\nbox_name: bx\n\n"
        "insert_job: m2\njob_type: c\ncommand: b\nmachine: h\nbox_name: bx\n"
        "condition: f(m1)\n\n"
        "insert_job: tail\njob_type: c\ncommand: c\nmachine: h\n"
        "condition: s(remote^PRD)\n"
    )
    expected = (
        "flowchart LR\n"
        '    subgraph n0["bx"]\n'
        '        n1["m1"]\n'
        '        n2["m2"]\n'
        "    end\n"
        '    n3["tail"]\n'
        '    n4{{"remote^PRD"}}\n'
        "    n1 -.-> n0\n"  # M15 assumed, success, no lookback -> label thinned to nothing
        '    n1 -->|"f"| n2\n'  # M04 exact, via=failure -> letter shown, no mapping row (not redesign)
        '    n4 ==>|"s M33"| n3\n'  # redesign: via letter + mapping row always shown
        "    linkStyle 2 stroke:#b91c1c,stroke-width:2px\n"
        "    classDef external fill:#e0ecff,stroke:#1d4ed8,color:#111\n"
        "    class n4 external\n"
    )
    assert to_mermaid(graph_of(text)) == expected


# ----------------------------------------------------------------- structure rules


def test_ids_are_generated_and_names_live_in_labels() -> None:
    text = (
        "insert_job: WEIRD.NAME#1\njob_type: c\ncommand: x\nmachine: h\n"
        r"condition: s(JOB\:WITH\:COLONS)" + "\n"
    )
    mermaid = to_mermaid(graph_of(text))
    _assert_ids_are_generated(mermaid)
    assert '"WEIRD.NAME#1"' in mermaid
    assert '"\N{WARNING SIGN} JOB:WITH:COLONS"' in mermaid  # undefined producer, warning prefix


def test_subgraph_end_blocks_balance_and_nest() -> None:
    text = (
        "insert_job: outer\njob_type: b\n\n"
        "insert_job: inner\njob_type: b\nbox_name: outer\n\n"
        "insert_job: leaf\njob_type: c\ncommand: x\nmachine: h\nbox_name: inner\n"
    )
    mermaid = to_mermaid(graph_of(text))
    opens = [ln for ln in mermaid.splitlines() if ln.strip().startswith("subgraph")]
    _assert_subgraphs_balance(mermaid)
    assert len(opens) == 2
    outer_line = next(ln for ln in opens if '"outer"' in ln)
    inner_line = next(ln for ln in opens if '"inner"' in ln)
    assert len(inner_line) - len(inner_line.lstrip()) > len(outer_line) - len(outer_line.lstrip())


# ------------------------------------------------------------- edge label thinning


def test_arrow_styles_encode_edge_class() -> None:
    catalog = corpus_catalog()
    mermaid = to_mermaid(derive_graph(catalog))
    assert '-.->|"v"|' in mermaid  # assumed, via=global (letter shown: via != success)
    assert '==>|"s M33"|' in mermaid  # redesign, cross-instance
    assert '==>|"v M16"|' in mermaid  # redesign, box global gate
    # exact arrows: none in the corpus graph today; golden test covers M04


def test_success_edge_with_no_lookback_has_no_label() -> None:
    """DL-35 thinning: via letter only when via != success (or redesign),
    mapping row only on redesign edges -- a plain assumed success latch with
    no lookback renders as a bare arrow, not `-.->|"..."|`."""
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n"
    )
    mermaid = to_mermaid(graph_of(text))
    edge_line = next(ln for ln in mermaid.splitlines() if "-.->" in ln)
    assert edge_line.strip() == "n0 -.-> n1"
    assert "|" not in edge_line


def test_labels_carry_lookback_raw_tokens() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    assert '"s, 00.30 M02"' in mermaid
    assert r'"s, 01\:00 M02"' in mermaid  # escaped-colon raw survives verbatim


def test_redesign_linkstyle_indices_when_not_the_first_link() -> None:
    """linkStyle counts ALL emitted link statements in emission order; craft
    a catalog where an exact edge is emitted before a redesign edge so the
    index is provably not just "the first line"."""
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons1\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(prod)\n\n"
        "insert_job: cons2\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(ghost_producer)\n"
    )
    mermaid = to_mermaid(graph_of(text))
    lines = mermaid.splitlines()
    assert lines[lines.index('    n0 -->|"f"| n1')] is not None
    assert (
        "    linkStyle 1 stroke:#b91c1c,stroke-width:2px" in mermaid
    )  # redesign is link #1, not #0


# ------------------------------------------------------------- pseudo-node shapes


def test_pseudo_node_shapes_and_classes() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    assert re.search(r'n\d+\[/"REGION"/\]', mermaid)  # global -> parallelogram
    assert re.search(r'n\d+\{\{"DB_BACKUP\^PRD"\}\}', mermaid)  # external -> hexagon (was [[..]])
    assert re.search(
        r'n\d+\["\N{WARNING SIGN} JOB:WITH:COLONS"\]', mermaid
    )  # undefined -> warning prefix
    assert re.search(r"class n[\d,n]+ globalvar", mermaid)
    assert re.search(r"class n[\d,n]+ external", mermaid)
    assert re.search(r"class n[\d,n]+ undefined", mermaid)
    for cls in ("globalvar", "external", "undefined"):
        class_def_line = next(ln for ln in mermaid.splitlines() if f"classDef {cls}" in ln)
        assert "color:#111" in class_def_line


def test_fw_job_renders_as_stadium_with_trigger_class() -> None:
    text = "insert_job: fwj\njob_type: f\nwatch_file: /tmp/f\nmachine: m1\n"
    mermaid = to_mermaid(graph_of(text))
    assert 'n0(["\N{PAGE FACING UP} fwj"])' in mermaid
    assert "classDef trigger fill:#def7ec,stroke:#046c4e,color:#111" in mermaid
    assert "class n0 trigger" in mermaid


def test_scheduled_job_gets_a_second_label_line() -> None:
    text = (
        "insert_job: sched\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
    )
    mermaid = to_mermaid(graph_of(text))
    assert 'n0["sched<br/>\N{ALARM CLOCK} 10:00 all"]' in mermaid  # plain rect, not a stadium


def test_fw_job_with_a_schedule_gets_both_stadium_and_second_line() -> None:
    text = (
        "insert_job: fwj\njob_type: f\nwatch_file: /tmp/f\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: mo,tu\nstart_times: "06:00"\n'
    )
    mermaid = to_mermaid(graph_of(text))
    assert 'n0(["\N{PAGE FACING UP} fwj<br/>\N{ALARM CLOCK} 06:00 mo,tu"])' in mermaid


def test_scheduled_box_subgraph_title_stays_single_line() -> None:
    """DL-35a (3): hosts render <br/> in subgraph TITLES inconsistently, so
    box titles use middle-dot separators; member labels keep <br/>."""
    text = (
        "insert_job: bx\njob_type: b\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "06:00"\n\n'
        "insert_job: m1\njob_type: c\ncommand: x\nmachine: h\nbox_name: bx\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "07:00"\n'
    )
    mermaid = to_mermaid(graph_of(text))
    title = next(ln for ln in mermaid.splitlines() if ln.strip().startswith("subgraph"))
    assert "<br/>" not in title
    assert 'subgraph n0["bx \N{MIDDLE DOT} \N{ALARM CLOCK} 06:00 all"]' in title
    assert 'n1["m1<br/>\N{ALARM CLOCK} 07:00 all"]' in mermaid


# --------------------------------------------------------------------------- mutex


def test_mutex_pair_renders_dotted_lock_link() -> None:
    text = (
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx2)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx1)\n"
    )
    mermaid = to_mermaid(graph_of(text))
    assert re.search(r"n0 x-\. lock \.-x n1|n1 x-\. lock \.-x n0", mermaid)
    assert "mutex" not in mermaid  # the word changed to "lock" (DL-35)


def test_self_mutex_renders_as_a_badge_not_a_self_link() -> None:
    text = "insert_job: s1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(s1)\n"
    mermaid = to_mermaid(graph_of(text))
    assert 'n0["s1<br/>\N{LOCK} single-instance"]' in mermaid
    assert "x-. lock .-x" not in mermaid
    assert "-.->" not in mermaid  # n(self) never becomes an edge either


def test_complete_clique_of_three_renders_a_shared_hub_not_pairwise_links() -> None:
    text = (
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(b) & n(c)\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(a) & n(c)\n\n"
        "insert_job: c\njob_type: c\ncommand: z\nmachine: m1\ncondition: n(a) & n(b)\n"
    )
    mermaid = to_mermaid(graph_of(text))
    assert 'n3(("\N{LOCK}"))' in mermaid
    assert mermaid.count(" -.- ") == 3  # hub -> each of a, b, c
    assert "x-. lock .-x" not in mermaid  # no pairwise links once it's a complete clique
    assert (
        "classDef lockNode fill:#f3f4f6,stroke:#6b7280,color:#111,stroke-dasharray: 2 2" in mermaid
    )
    assert "class n3 lockNode" in mermaid


def test_incomplete_triangle_stays_pairwise() -> None:
    """a-b and b-c are stated but a-c is not: the JIL never claimed a is
    excluded from c, so no hub -- pairwise links only."""
    text = (
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(b)\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(a) & n(c)\n\n"
        "insert_job: c\njob_type: c\ncommand: z\nmachine: m1\ncondition: n(b)\n"
    )
    mermaid = to_mermaid(graph_of(text))
    assert mermaid.count("x-. lock .-x") == 2
    assert "((" not in mermaid  # no hub node


# ------------------------------------------------------------------------ escaping


def test_quotes_in_names_escape_to_mermaid_entity() -> None:
    text = 'insert_job: j\njob_type: c\ncommand: x\nmachine: h\ncondition: s(odd"name)\n'
    mermaid = to_mermaid(graph_of(text))
    assert (
        '"\N{WARNING SIGN} odd#quot;name"' in mermaid
    )  # undefined producer: warning prefix + escape
    assert 'odd"name' not in mermaid


def test_deterministic_output() -> None:
    first = to_mermaid(derive_graph(corpus_catalog()))
    second = to_mermaid(derive_graph(corpus_catalog()))
    assert first == second


# ------------------------------------------------------------------- collapse rule

_BIG_BOX = "insert_job: big\njob_type: b\n\n" + "\n".join(
    f"insert_job: member_{i:02d}\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
    for i in range(15)
)
_BIG_BOX += "\ninsert_job: after\njob_type: c\ncommand: y\nmachine: h\ncondition: s(member_00)\n"


def test_collapse_threshold_folds_big_boxes() -> None:
    mermaid = to_mermaid(graph_of(_BIG_BOX), collapse_threshold=10)
    assert "subgraph" not in mermaid
    assert '[["big (15 members)"]]' in mermaid
    assert "member_00" not in mermaid  # hidden member
    assert "collapsedBox" in mermaid


def test_collapsed_box_label_counts_hidden_triggers() -> None:
    """DL-35a (4): folding must not silently hide watcher/schedule facts."""
    text = _BIG_BOX.replace(
        "insert_job: member_03\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n",
        "insert_job: member_03\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "05:00"\n',
    ).replace(
        "insert_job: member_04\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n",
        "insert_job: member_04\njob_type: f\nwatch_file: /in/f\nmachine: h\nbox_name: big\n",
    )
    mermaid = to_mermaid(graph_of(text), collapse_threshold=10)
    assert '[["big (15 members, 1 \N{ALARM CLOCK}, 1 \N{PAGE FACING UP})"]]' in mermaid


def test_collapse_reanchors_edges_to_the_box_node() -> None:
    # after's dependency on member_00 re-anchors to the collapsed box node;
    # the edge itself is M02 assumed (cross-stream: box vs. unboxed), so the
    # thinned label is empty -- no |"..."| on the arrow.
    mermaid = to_mermaid(graph_of(_BIG_BOX), collapse_threshold=10)
    box_id_match = re.search(r'(n\d+)\[\["big \(15 members\)"\]\]', mermaid)
    after_id_match = re.search(r'(n\d+)\["after"\]', mermaid)
    assert box_id_match and after_id_match
    assert f"{box_id_match.group(1)} -.-> {after_id_match.group(1)}" in mermaid


def test_collapse_redesign_edge_reanchors_with_label_and_linkstyle() -> None:
    """A redesign edge into a collapsed box still re-anchors to the box node
    and keeps its mapping-row label + linkStyle index."""
    text = "insert_job: big\njob_type: b\nbox_success: s(outsider)\n\n" + "\n".join(
        f"insert_job: member_{i:02d}\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
        for i in range(15)
    )
    text += "\ninsert_job: outsider\njob_type: c\ncommand: y\nmachine: h\n"
    mermaid = to_mermaid(graph_of(text), collapse_threshold=10)
    box_id_match = re.search(r'(n\d+)\[\["big \(15 members\)"\]\]', mermaid)
    outsider_id_match = re.search(r'(n\d+)\["outsider"\]', mermaid)
    assert box_id_match and outsider_id_match
    assert f'{outsider_id_match.group(1)} ==>|"s M16"| {box_id_match.group(1)}' in mermaid
    assert "linkStyle 0 stroke:#b91c1c,stroke-width:2px" in mermaid


def test_intra_box_edges_vanish_when_collapsed() -> None:
    text = _BIG_BOX.replace(
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n",
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
        "condition: s(member_02)\n",
    )
    mermaid = to_mermaid(graph_of(text), collapse_threshold=10)
    box_id = re.search(r'(n\d+)\[\["big \(15 members\)"\]\]', mermaid)
    assert box_id is not None
    assert f"{box_id.group(1)} -.-> {box_id.group(1)}" not in mermaid


def test_threshold_boundary_is_strictly_greater() -> None:
    mermaid = to_mermaid(graph_of(_BIG_BOX), collapse_threshold=15)
    assert "subgraph" in mermaid  # 15 members == threshold -> NOT collapsed


def test_default_threshold_keeps_corpus_boxes_expanded() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    # box_a + gate_box + sem24's NIGHT_SB/NIGHT_B nested pair (DL-18)
    assert mermaid.count("subgraph") == 4


def test_direction_td() -> None:
    assert to_mermaid(derive_graph(corpus_catalog()), direction="TD").startswith("flowchart TD\n")


# ---------------------------------------------------------------- split_components


def test_dependency_edges_and_box_comembership_connect_components() -> None:
    text = (
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n\n"
        "insert_job: m2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bx\n"
    )
    graph = graph_of(text)
    assert split_components(graph) == [["bx", "m1", "m2"]]  # box membership alone connects


def test_mutex_pairs_do_not_connect_components() -> None:
    """DL-35: a shared lock would glue unrelated streams -- mutex members
    stay in separate components (they show up in Shared locks instead)."""
    text = (
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx2)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx1)\n"
    )
    graph = graph_of(text)
    assert split_components(graph) == [["mx1"], ["mx2"]]


def test_component_ordering_is_descending_size_then_catalog_position() -> None:
    text = (
        "insert_job: solo_first\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(solo_second)\n\n"
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n\n"
        "insert_job: solo_second\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(solo_first)\n"
    )
    graph = graph_of(text)
    components = split_components(graph)
    assert components == [["prod", "cons"], ["solo_first"], ["solo_second"]]


def test_standalone_job_consuming_only_a_global_is_not_standalone() -> None:
    text = "insert_job: uses_global\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = 1\n"
    graph = graph_of(text)
    (comp,) = split_components(graph)
    assert not _is_standalone(comp, _incident_nodes(graph))


def test_standalone_job_that_is_a_mutex_member_is_not_standalone() -> None:
    text = (
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx2)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx1)\n"
    )
    graph = graph_of(text)
    touched = _incident_nodes(graph)
    for comp in split_components(graph):
        assert not _is_standalone(comp, touched)


def test_truly_isolated_job_is_standalone() -> None:
    text = "insert_job: lonely\njob_type: c\ncommand: x\nmachine: m1\n"
    graph = graph_of(text)
    (comp,) = split_components(graph)
    assert _is_standalone(comp, _incident_nodes(graph))


def test_component_title_prefers_top_level_box_and_counts_extra_members() -> None:
    text = (
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n\n"
        "insert_job: m2\njob_type: c\ncommand: y\nmachine: m1\nbox_name: bx\n\n"
        "insert_job: outsider\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(m1)\n"
    )
    graph = graph_of(text)
    (comp,) = split_components(graph)
    assert _component_title(comp, graph) == "bx (+1 more)"


def test_component_title_has_no_suffix_when_the_box_covers_the_whole_component() -> None:
    text = (
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
    )
    graph = graph_of(text)
    (comp,) = split_components(graph)
    assert _component_title(comp, graph) == "bx"


def test_common_prefix_requires_four_chars_and_a_separator_and_strict_length() -> None:
    assert _common_prefix(["etl_load_a", "etl_load_b", "etl_load_c"]) == "etl_load_"
    assert _common_prefix(["ab_c", "ab_d"]) == ""  # prefix candidate "ab_" is only 3 chars
    assert _common_prefix(["etl_", "etl_load"]) == ""  # a name EQUALS the candidate prefix
    assert _common_prefix(["solo"]) == ""


def test_auto_direction_chain_is_lr_fanout_is_td() -> None:
    chain_text = (
        "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(a)\n\n"
        "insert_job: c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(b)\n"
    )
    chain_graph = graph_of(chain_text)
    assert _auto_direction(["a", "b", "c"], chain_graph) == "LR"

    fan_text = "insert_job: root\njob_type: c\ncommand: r\nmachine: m1\n\n" + "".join(
        f"insert_job: k{i}\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(root)\n\n"
        for i in range(4)
    )
    fan_graph = graph_of(fan_text)
    comp = ["root"] + [f"k{i}" for i in range(4)]
    assert _auto_direction(comp, fan_graph) == "TD"


# -------------------------------------------------------------------- to_markdown


def test_to_markdown_title_and_summary_line() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    md = to_markdown(derive_graph(catalog), title="sem10_box_basic.jil")
    assert md.startswith("# Workflow graph: sem10_box_basic.jil\n")
    assert "3 jobs \N{MIDDLE DOT} 1 edges" in md
    assert "(Appendix A, not charted)" in md


def test_to_markdown_legend_is_always_present() -> None:
    md = to_markdown(graph_of("insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"), title="t")
    assert "<summary>Legend</summary>" in md
    assert _mermaid_fences(md)[0].startswith("flowchart LR\n")


def test_to_markdown_component_gets_a_wid_heading_with_a_mermaid_fence() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n"
    )
    md = to_markdown(graph_of(text), title="t")
    assert "## W1 \N{EM DASH} prod (+1 more) (2 jobs)" in md
    fences = _mermaid_fences(md)
    assert len(fences) == 2  # legend + W1
    assert 'n0["prod"]' in fences[1]


def test_to_markdown_prefix_stripping_is_announced_in_the_chart_only() -> None:
    text = (
        "insert_job: etl_load_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: etl_load_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(etl_load_a)\n"
    )
    md = to_markdown(graph_of(text), title="t")
    assert "All names share the prefix `etl_load_` (stripped in the chart)." in md
    assert "## W1 \N{EM DASH} etl_load_a (+1 more) (2 jobs)" in md  # title itself is NOT stripped
    fences = _mermaid_fences(md)
    assert 'n0["a"]' in fences[1] and 'n1["b"]' in fences[1]


def test_to_markdown_no_prefix_note_when_no_common_prefix() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n"
    )
    md = to_markdown(graph_of(text), title="t")
    assert "share the prefix" not in md


def test_to_markdown_standalone_jobs_are_not_charted_by_default() -> None:
    text = "insert_job: lonely\njob_type: c\ncommand: x\nmachine: m1\n"
    md = to_markdown(graph_of(text), title="t")
    assert "## W1" not in md
    assert "None." not in md.split("## Appendix A")[1].split("## Appendix B")[0]
    assert "| lonely | CMD" in md


def test_to_markdown_include_singletons_adds_a_standalone_chart() -> None:
    text = "insert_job: lonely\njob_type: c\ncommand: x\nmachine: m1\n"
    md = to_markdown(graph_of(text), title="t", include_singletons=True)
    assert "## Standalone jobs (1)" in md
    fences = _mermaid_fences(md)
    assert any('n0["lonely"]' in f for f in fences)
    assert "(Appendix A, not charted)" not in md  # summary line drops the caveat too


def test_to_markdown_locks_section_enumerates_every_group() -> None:
    """DL-35a (2): the Locks section lists ALL mutex groups with kind and
    chart ids -- cross-component pairs included, nothing only-counted."""
    text = (
        "insert_job: mx1\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(mx2)\n\n"
        "insert_job: mx2\njob_type: c\ncommand: y\nmachine: m1\ncondition: n(mx1)\n\n"
        "insert_job: solo\njob_type: c\ncommand: z\nmachine: m1\ncondition: n(solo)\n"
    )
    md = to_markdown(graph_of(text), title="t")
    assert "## Locks" in md
    assert "| mx1 \N{MULTIPLICATION SIGN} mx2 | pair | W1, W2 |" in md
    assert "| solo | self | W3 |" in md


def test_dangling_mutex_member_renders_undefined_not_keyerror() -> None:
    """DL-35a (1): unqualified n(<undefined>) used to crash to_markdown; the
    ghost now renders as an undefined pseudo-node in its partner's chart."""
    text = "insert_job: real_job\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(ghost)\n"
    md = to_markdown(graph_of(text), title="t")
    chart = next(f for f in _mermaid_fences(md)[1:] if "real_job" in f)
    assert '"\N{WARNING SIGN} ghost"' in chart
    assert "x-. lock .-x" in chart
    assert "undefined" in chart  # classDef applied
    assert "| ghost \N{MULTIPLICATION SIGN} real_job | pair | not in catalog, W1 |" in md


def test_lock_hidden_by_collapse_still_enumerated_in_locks_section() -> None:
    """DL-35a (2): a pair wholly inside a collapsed box is not drawable, but
    it must not vanish from the report."""
    text = _BIG_BOX.replace(
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n",
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
        "condition: n(member_02)\n",
    )
    md = to_markdown(graph_of(text), title="t", collapse_threshold=10)
    charts = _mermaid_fences(md)[1:]
    assert not any("x-. lock .-x" in c for c in charts)  # suppressed in-chart
    assert "| member_01 \N{MULTIPLICATION SIGN} member_02 | pair | W1 |" in md


def test_to_markdown_appendix_a_lists_kind_schedule_and_truncated_command() -> None:
    text = (
        "insert_job: lonely\njob_type: c\n"
        f"command: echo {'x' * 80}\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n'
    )
    md = to_markdown(graph_of(text), title="t")
    row = next(ln for ln in md.splitlines() if ln.startswith("| lonely |"))
    assert "| CMD |" in row
    assert "09:00 all" in row
    assert "\N{HORIZONTAL ELLIPSIS}" in row  # truncated at 60 chars


def test_to_markdown_appendix_b_lists_non_exact_edges_with_untruncated_assumption() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem10_box_basic.jil")])
    md = to_markdown(derive_graph(catalog), title="t")
    section = md.split("## Appendix B")[1].split("## Appendix C")[0]
    assert "job_a" in section and "box_a" in section and "M15" in section
    assert "early-exit completion override needs explicit Skip-path restructuring" in section


def test_to_markdown_appendix_b_says_none_when_every_edge_is_exact() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(prod)\n"
    )
    md = to_markdown(graph_of(text), title="t")
    section = md.split("## Appendix B")[1].split("## Appendix C")[0]
    assert "None" in section


def test_to_markdown_appendix_c_renders_redesign_flags_table() -> None:
    catalog = lower_catalog([parse_file(CORPUS_DIR / "sem30_schedule.jil")])
    md = to_markdown(derive_graph(catalog), title="t", include_singletons=True)
    section = md.split("## Appendix C")[1]
    assert "### Redesign flags" in section
    assert "quarter_past" in section and "M27" in section


def test_to_markdown_appendix_c_says_none_when_all_three_are_empty() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    md = to_markdown(graph_of(text), title="t")
    section = md.split("## Appendix C")[1]
    assert "None." in section
    assert "### Redesign flags" not in section
    assert "### OR shapes" not in section
    assert "### Cycles" not in section


def test_to_markdown_direction_auto_picks_per_component() -> None:
    fan_text = "insert_job: root\njob_type: c\ncommand: r\nmachine: m1\n\n" + "".join(
        f"insert_job: k{i}\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(root)\n\n"
        for i in range(4)
    )
    md = to_markdown(graph_of(fan_text), title="t")
    fences = _mermaid_fences(md)
    assert fences[1].startswith("flowchart TD\n")


def test_to_markdown_explicit_direction_overrides_auto_for_every_chart() -> None:
    fan_text = "insert_job: root\njob_type: c\ncommand: r\nmachine: m1\n\n" + "".join(
        f"insert_job: k{i}\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(root)\n\n"
        for i in range(4)
    )
    md = to_markdown(graph_of(fan_text), title="t", direction="LR")
    fences = _mermaid_fences(md)
    assert fences[1].startswith("flowchart LR\n")


def test_to_markdown_elk_prepends_frontmatter_inside_the_chart_fence_only() -> None:
    text = (
        "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(prod)\n"
    )
    md = to_markdown(graph_of(text), title="t", elk=True)
    fences = _mermaid_fences(md)
    assert not fences[0].startswith("---")  # legend is a fixed template, untouched
    assert fences[1].startswith("---\nconfig:\n  layout: elk\n---\nflowchart")


def test_to_markdown_is_deterministic() -> None:
    graph = derive_graph(corpus_catalog())
    assert to_markdown(graph, title="t") == to_markdown(graph, title="t")


def test_to_markdown_whole_corpus_charts_are_balanced_and_id_safe() -> None:
    md = to_markdown(derive_graph(corpus_catalog()), title="corpus")
    fences = _mermaid_fences(md)
    assert len(fences) > 1
    for fence in fences[1:]:  # skip the hand-written legend
        _assert_subgraphs_balance(fence)
        _assert_ids_are_generated(fence)


# --------------------------------------------------------------------------- CLI


def test_cli_viz_renders_a_markdown_report() -> None:
    result = runner.invoke(app, ["viz", str(CORPUS_DIR / "sem10_box_basic.jil")])
    assert result.exit_code == 0
    assert result.stdout.startswith("# Workflow graph:")
    assert "```mermaid" in result.stdout
    assert 'subgraph n0["box_a"]' in result.stdout


def test_cli_viz_options() -> None:
    result = runner.invoke(
        app,
        [
            "viz",
            "--direction",
            "TD",
            "--collapse-threshold",
            "1",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    assert "flowchart TD" in result.stdout
    assert '[["box_a (2 members)"]]' in result.stdout


def test_cli_viz_include_singletons_adds_a_section() -> None:
    result = runner.invoke(
        app, ["viz", "--include-singletons", str(CORPUS_DIR / "sem30_schedule.jil")]
    )
    assert result.exit_code == 0
    assert "## Standalone jobs" in result.stdout


def test_cli_viz_elk_prepends_frontmatter() -> None:
    result = runner.invoke(app, ["viz", "--elk", str(CORPUS_DIR / "sem10_box_basic.jil")])
    assert result.exit_code == 0
    assert "config:\n  layout: elk" in result.stdout


def test_cli_viz_writes_out_file(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    result = runner.invoke(
        app, ["viz", "--out", str(target), str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").startswith("# Workflow graph:")
    assert "wrote" in result.stdout


def test_cli_viz_bad_direction_exits_2() -> None:
    result = runner.invoke(
        app, ["viz", "--direction", "diagonal", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 2
    assert "--direction" in result.stderr


def test_cli_viz_lowering_refusal_exits_2() -> None:
    result = runner.invoke(app, ["viz", str(CORPUS_DIR / "sem31_xor.jil")])
    assert result.exit_code == 2
    assert "SEM-31" in result.stderr
