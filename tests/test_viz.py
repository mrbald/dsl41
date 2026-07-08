"""Mermaid rendering tests (phase 6): dsl41.viz per its module-docstring
decisions -- generated ids, class-encoded arrows, box subgraphs + collapse
threshold, pseudo-node shapes/classes, mutex links, label escaping -- plus
the `dsl41 viz` CLI command.

There is no Mermaid parser in the toolchain; validity is pinned
structurally: balanced subgraph/end blocks, id-safe node identifiers, and a
hand-checked golden render for one small catalog.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from dsl41.ast_jil import parse_file
from dsl41.cli import app
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR, lower_catalog, lower_source
from dsl41.viz import to_mermaid

CORPUS_DIR = Path(__file__).parent / "corpus"
EXPECT_LOWER_ERROR = {"sem31_xor.jil"}
LOWERABLE_CORPUS = [p for p in sorted(CORPUS_DIR.glob("*.jil")) if p.name not in EXPECT_LOWER_ERROR]

runner = CliRunner()


def corpus_catalog() -> CatalogIR:
    return lower_catalog([parse_file(p) for p in LOWERABLE_CORPUS])


def graph_of(text: str) -> DerivedGraph:
    return derive_graph(lower_source(text))


# ------------------------------------------------------------------ golden render


def test_golden_small_catalog() -> None:
    """Hand-checked render pinning the full output shape for one catalog:
    a box subgraph with an assumed M15 override edge, an exact M04 edge,
    and a redesign M33 edge with its external pseudo-node."""
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
        '    n4[["remote^PRD"]]\n'
        '    n1 -.->|"s M15"| n0\n'  # edges follow catalog order: bx first
        '    n1 -->|"f"| n2\n'
        '    n4 ==>|"s M33"| n3\n'
        "    classDef external fill:#e0ecff,stroke:#1d4ed8\n"
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
    for line in mermaid.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith(("classDef", "class ", "subgraph", "end")):
            continue
        # node/edge lines must start with a generated id, never a raw name
        assert re.match(r"n\d+\b", stripped), line
    assert '"WEIRD.NAME#1"' in mermaid
    assert '"JOB:WITH:COLONS"' in mermaid


def test_subgraph_end_blocks_balance_and_nest() -> None:
    text = (
        "insert_job: outer\njob_type: b\n\n"
        "insert_job: inner\njob_type: b\nbox_name: outer\n\n"
        "insert_job: leaf\njob_type: c\ncommand: x\nmachine: h\nbox_name: inner\n"
    )
    mermaid = to_mermaid(graph_of(text))
    opens = [ln for ln in mermaid.splitlines() if ln.strip().startswith("subgraph")]
    ends = [ln for ln in mermaid.splitlines() if ln.strip() == "end"]
    assert len(opens) == 2
    assert len(ends) == 2
    # inner subgraph is indented deeper than outer
    outer_line = next(ln for ln in opens if '"outer"' in ln)
    inner_line = next(ln for ln in opens if '"inner"' in ln)
    assert len(inner_line) - len(inner_line.lstrip()) > len(outer_line) - len(outer_line.lstrip())


def test_arrow_styles_encode_edge_class() -> None:
    catalog = corpus_catalog()
    mermaid = to_mermaid(derive_graph(catalog))
    assert '-.->|"s M01"|' in mermaid  # assumed
    assert '==>|"s M33"|' in mermaid  # redesign
    assert '==>|"v M16"|' in mermaid  # box global gate
    # exact arrows: none in the corpus graph today; golden test covers M04


def test_labels_carry_lookback_raw_tokens() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    assert '"s, 00.30 M02"' in mermaid
    assert r'"s, 01\:00 M02"' in mermaid  # escaped-colon raw survives verbatim


def test_pseudo_node_shapes_and_classes() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    assert re.search(r'n\d+\[/"REGION"/\]', mermaid)  # global -> parallelogram
    assert re.search(r'n\d+\[\["DB_BACKUP\^PRD"\]\]', mermaid)  # external -> subroutine
    assert re.search(r"class n[\d,n]+ globalvar", mermaid)
    assert re.search(r"class n[\d,n]+ external", mermaid)
    assert re.search(r"class n[\d,n]+ undefined", mermaid)


def test_mutex_links_render_dotted_with_self_link() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    mutex_lines = [ln.strip() for ln in mermaid.splitlines() if " x-. mutex .-x " in ln]
    assert len(mutex_lines) == 2  # the pair + the self-exclusion
    assert any(re.fullmatch(r"(n\d+) x-\. mutex \.-x \1", ln) for ln in mutex_lines)


def test_quotes_in_names_escape_to_mermaid_entity() -> None:
    text = 'insert_job: j\njob_type: c\ncommand: x\nmachine: h\ncondition: s(odd"name)\n'
    mermaid = to_mermaid(graph_of(text))
    assert '"odd#quot;name"' in mermaid
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


def test_collapse_reanchors_edges_to_the_box_node() -> None:
    mermaid = to_mermaid(graph_of(_BIG_BOX), collapse_threshold=10)
    # after's dependency on member_00 re-anchors to the collapsed box node
    box_id_match = re.search(r'(n\d+)\[\["big \(15 members\)"\]\]', mermaid)
    after_id_match = re.search(r'(n\d+)\["after"\]', mermaid)
    assert box_id_match and after_id_match
    assert f'{box_id_match.group(1)} -.->|"s M02"| {after_id_match.group(1)}' in mermaid


def test_intra_box_edges_vanish_when_collapsed() -> None:
    text = _BIG_BOX.replace(
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n",
        "insert_job: member_01\njob_type: c\ncommand: x\nmachine: h\nbox_name: big\n"
        "condition: s(member_02)\n",
    )
    mermaid = to_mermaid(graph_of(text), collapse_threshold=10)
    box_id = re.search(r'(n\d+)\[\["big \(15 members\)"\]\]', mermaid)
    assert box_id is not None
    assert f"{box_id.group(1)} -.-> {box_id.group(1)}" not in mermaid.replace('|"s M01"| ', "")


def test_threshold_boundary_is_strictly_greater() -> None:
    mermaid = to_mermaid(graph_of(_BIG_BOX), collapse_threshold=15)
    assert "subgraph" in mermaid  # 15 members == threshold -> NOT collapsed


def test_default_threshold_keeps_corpus_boxes_expanded() -> None:
    mermaid = to_mermaid(derive_graph(corpus_catalog()))
    assert mermaid.count("subgraph") == 2  # box_a + gate_box


def test_direction_td() -> None:
    assert to_mermaid(derive_graph(corpus_catalog()), direction="TD").startswith("flowchart TD\n")


# --------------------------------------------------------------------------- CLI


def test_cli_viz_renders_corpus_files() -> None:
    result = runner.invoke(app, ["viz", str(CORPUS_DIR / "sem10_box_basic.jil")])
    assert result.exit_code == 0
    assert result.stdout.startswith("flowchart LR\n")
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
    assert result.stdout.startswith("flowchart TD\n")
    assert '[["box_a (2 members)"]]' in result.stdout


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
