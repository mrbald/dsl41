"""Interactive exploration page (dsl41 viz --format explore) and its vendored asset.

Validity strategy mirrors test_viz_html.py: no browser in the toolchain, so
the page is pinned structurally -- the elements JSON (parent mapping, EXT
synthesis, edge classes, DL-35 label grammar, full assumptions), the JSON
embedding's escaping invariant, the vendor payload present and inline-safe,
and the CLI flag absorptions. What only a browser can verify (ELK render,
context menu, focus feel) is recorded in DL-71, not tested here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from test_viz import CORPUS_DIR, catalog_of, corpus_catalog, runner
from test_viz_html import _vendor_bytes

from dsl41.cli import app
from dsl41.derive import derive_graph
from dsl41.viz_explore import _elements, to_explore_html

# ------------------------------------------------------------ vendor integrity


def test_vendored_cytoscape_bundle_is_inline_safe_and_attributed() -> None:
    payload = _vendor_bytes("cytoscape-explore.iife.min.js")
    assert len(payload) > 1_500_000  # esbuild output is not byte-reproducible
    assert b"</script" not in payload  # inline-safety: embedded without escaping
    assert payload.startswith(b"/*!")  # attribution banner from vendor_mermaid.sh
    assert b"EPL-2.0" in payload[:400]
    assert b"var cyBundle" in payload[:600]  # the IIFE global the page JS expects


# ------------------------------------------------------------------- elements


def _corpus_elements(name: str) -> dict[str, list[dict[str, object]]]:
    catalog = catalog_of((CORPUS_DIR / name).read_text(encoding="utf-8"))
    return _elements(catalog, derive_graph(catalog))


def _node(els: dict[str, list[dict[str, object]]], node_id: str) -> dict[str, object]:
    matches = [n for n in els["nodes"] if n["data"]["id"] == node_id]  # type: ignore[index]
    assert len(matches) == 1
    return matches[0]


def test_elements_nodes_carry_meta_and_box_parent() -> None:
    els = _corpus_elements("sem10_box_basic.jil")
    box = _node(els, "box_a")
    assert box["classes"] == "box"
    assert "parent" not in box["data"]  # type: ignore[operator]
    member = _node(els, "job_a")
    assert member["classes"] == "cmd"
    assert member["data"]["kind"] == "CMD"  # type: ignore[index]
    assert member["data"]["parent"] == "box_a"  # type: ignore[index]
    assert member["data"]["detail"] == "sleep 15"  # type: ignore[index]


def test_elements_synthesize_ext_nodes_for_undefined_and_external() -> None:
    els = _corpus_elements("sem06_dangling.jil")
    assert _node(els, "THIS_JOB_DOES_NOT_EXIST")["classes"] == "ext"
    external = _node(els, "also_missing^PRD")
    assert external["classes"] == "ext"
    assert external["data"]["kind"] == "EXT"  # type: ignore[index]


def test_elements_ext_global_class_iff_via_global() -> None:
    els = _corpus_elements("sem08_globals.jil")
    assert _node(els, "BillID")["classes"] == "ext global"
    els = _corpus_elements("sem06_dangling.jil")
    assert "global" not in _node(els, "also_missing^PRD")["classes"]  # type: ignore[operator]


def test_elements_edges_carry_annotations_untruncated() -> None:
    catalog = catalog_of((CORPUS_DIR / "sem12_external_gate.jil").read_text(encoding="utf-8"))
    graph = derive_graph(catalog)
    els = _elements(catalog, graph)
    by_source = {e["data"]["source"]: e["data"] for e in els["edges"]}  # type: ignore[index]
    gate = by_source["gate_outside_job"]
    assert gate["cls"] == "redesign"
    assert gate["mapping_row"] == "M16"
    # full assumption text, byte-equal to the derived edge (never truncated)
    derived = next(e for e in graph.edges if e.src == "gate_outside_job")
    assert gate["assumption"] == derived.assumption
    assert by_source["ABORT_FLAG"]["via"] == "global"


def test_elements_edge_labels_reuse_dl35_thinning_grammar() -> None:
    # success + no lookback -> empty label; redesign -> mapping row appended;
    # lookback raw token always present
    els = _corpus_elements("sem10_box_basic.jil")
    assert els["edges"][0]["data"]["label"] == ""  # type: ignore[index]
    els = _corpus_elements("sem04_lookback.jil")
    labels = {e["data"]["source"]: e["data"]["label"] for e in els["edges"]}  # type: ignore[index]
    assert labels["Joba"] == "s, 01\\:00 M02"
    lookbacks = {e["data"]["source"]: e["data"]["lookback"] for e in els["edges"]}  # type: ignore[index]
    assert lookbacks["Joba"] == "01\\:00"


def test_elements_edge_class_is_the_style_class() -> None:
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    els = _elements(catalog, graph)
    assert [e["classes"] for e in els["edges"]] == [e.cls for e in graph.edges]


def test_elements_cover_every_node_and_edge() -> None:
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    els = _elements(catalog, graph)
    catalog_ids = [n["data"]["id"] for n in els["nodes"][: len(graph.nodes)]]  # type: ignore[index]
    assert catalog_ids == graph.nodes  # catalog nodes first, source order
    assert len(els["edges"]) == len(graph.edges)
    ext = els["nodes"][len(graph.nodes) :]
    assert all("ext" in n["classes"] for n in ext)  # type: ignore[operator]
    endpoints = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    assert {n["data"]["id"] for n in ext} == endpoints - set(graph.nodes)  # type: ignore[index]


def test_elements_edge_ids_never_collide_with_job_names() -> None:
    # cytoscape ids share one namespace across nodes and edges; a job
    # literally named "e0" would otherwise swallow the first edge at
    # cytoscape init with no page error (review finding)
    text = (
        "insert_job: e0\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(feeder)\n\n"
        "insert_job: feeder\njob_type: c\ncommand: x\nmachine: m1\n"
    )
    catalog = catalog_of(text)
    els = _elements(catalog, derive_graph(catalog))
    ids = [n["data"]["id"] for n in els["nodes"]] + [e["data"]["id"] for e in els["edges"]]  # type: ignore[index]
    assert len(ids) == len(set(ids))
    assert els["edges"][0]["data"]["id"] == "_e0"  # type: ignore[index]


def test_elements_are_deterministic_across_hash_seeds() -> None:
    # in-process double emission is a tautology (same PYTHONHASHSEED, same
    # insertion history); a set-iteration regression only shows across
    # interpreters with different seeds (review finding)
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from dsl41.derive import derive_graph\n"
        "from dsl41.ir import lower_source\n"
        "from dsl41.viz_explore import _elements\n"
        "catalog = lower_source(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "sys.stdout.write(json.dumps(_elements(catalog, derive_graph(catalog))))\n"
    )
    pages = {
        subprocess.run(
            [sys.executable, "-c", script, str(CORPUS_DIR / "sem12_external_gate.jil")],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1")
    }
    assert len(pages) == 1


# ------------------------------------------------------------------- the page

_GRAPH_DATA = re.compile(r'<script id="graph-data" type="application/json">(.*?)</script>', re.S)


def _page_elements(page: str) -> dict[str, list[dict[str, object]]]:
    raw = _GRAPH_DATA.search(page)
    assert raw is not None
    assert "<" not in raw.group(1)  # the escaping invariant the embedding rests on
    result: dict[str, list[dict[str, object]]] = json.loads(raw.group(1))
    return result


def test_to_explore_html_escapes_the_json_but_round_trips_elements() -> None:
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    assert _page_elements(to_explore_html(catalog, graph)) == _elements(catalog, graph)


def test_to_explore_html_embeds_the_vendor_payload_exactly_once() -> None:
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    probe = _vendor_bytes("cytoscape-explore.iife.min.js").decode("utf-8")[:200]
    assert page.count(probe) == 1


def test_to_explore_html_survives_marker_shaped_job_and_title() -> None:
    # a legal job name containing a substitution marker must not splice the
    # vendor bundle into the elements JSON, and a marker-shaped title must
    # not duplicate the JSON into <title> (review finding: single-pass
    # substitution, replaced content never re-scanned)
    text = "insert_job: EVIL__DSL41_CYTOSCAPE_JS__X\njob_type: c\ncommand: x\nmachine: m1\n"
    page = to_explore_html(catalog_of(text), title="x__DSL41_ELEMENTS_JSON__.jil")
    probe = _vendor_bytes("cytoscape-explore.iife.min.js").decode("utf-8")[:200]
    assert page.count(probe) == 1
    names = [n["data"]["id"] for n in _page_elements(page)["nodes"]]  # type: ignore[index]
    assert names == ["EVIL__DSL41_CYTOSCAPE_JS__X"]  # JSON intact, name verbatim
    assert "Explore: x__DSL41_ELEMENTS_JSON__.jil</title>" in page


def test_to_explore_html_maps_direction_to_elk() -> None:
    catalog = catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n")
    assert 'DIRECTION = "RIGHT"' in to_explore_html(catalog)
    assert 'DIRECTION = "RIGHT"' in to_explore_html(catalog, direction="LR")
    assert 'DIRECTION = "DOWN"' in to_explore_html(catalog, direction="TD")


def test_to_explore_html_escapes_title_and_counts_summary() -> None:
    catalog = catalog_of((CORPUS_DIR / "sem10_box_basic.jil").read_text(encoding="utf-8"))
    page = to_explore_html(catalog, title="a<b&c")
    assert "Explore: a&lt;b&amp;c</title>" in page
    assert "3 jobs \N{MIDDLE DOT} 1 edges \N{MIDDLE DOT} 1 boxes" in page


def test_to_explore_html_singletons_always_present() -> None:
    # navigation replaces collapsing: search must find standalone jobs
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    assert [n["data"]["id"] for n in _page_elements(page)["nodes"]] == ["solo"]  # type: ignore[index]


# --------------------------------------------------------------------------- CLI


def test_cli_viz_explore_writes_out_file(tmp_path: Path) -> None:
    target = tmp_path / "explore.html"
    result = runner.invoke(
        app,
        ["viz", "--format", "explore", "--out", str(target), str(CORPUS_DIR / "sem10_box_basic.jil")],
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert target.stat().st_size > 1_500_000  # the vendor payload really embedded
    assert "wrote" in result.stdout


def test_cli_viz_explore_stdout_is_the_navigation_page() -> None:
    result = runner.invoke(
        app, ["viz", "--format", "explore", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 0
    assert 'id="graph-data"' in result.stdout  # the explore page...
    assert 'id="chart-data"' not in result.stdout  # ...not the --format html report


def test_cli_viz_explore_refuses_chart_shaping_flags() -> None:
    # DL-75: the page is one canvas -- no Mermaid frontmatter, boxes never
    # collapse, singletons always present. None of these can be delivered,
    # so each is refused instead of silently ignored.
    for flag in ("--elk", "--fixed-scale", "--include-singletons"):
        result = runner.invoke(
            app, ["viz", "--format", "explore", flag, str(CORPUS_DIR / "sem10_box_basic.jil")]
        )
        assert result.exit_code == 2, flag
        assert f"{flag} cannot shape --format explore" in result.stderr
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "explore",
            "--collapse-threshold",
            "1",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 2
    assert "--collapse-threshold" in result.stderr


def test_cli_viz_explore_honors_direction() -> None:
    # --direction is the one shaping option the canvas can deliver (DL-75)
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "explore",
            "--direction",
            "TD",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    assert result.stdout.startswith("<!doctype html>")
    assert 'DIRECTION = "DOWN"' in result.stdout
    page_nodes = _page_elements(result.stdout)["nodes"]
    assert len(page_nodes) == 3  # boxes are never collapsed here
