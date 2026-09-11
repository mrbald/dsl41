"""Interactive exploration page (dsl41 viz --format explore) and its vendored asset.

Validity strategy mirrors test_viz_html.py: no browser in the toolchain, so
the page is pinned structurally -- the elements JSON (parent mapping, EXT
synthesis, edge classes, DL-35 label grammar, full assumptions), the JSON
embedding's escaping invariant, the vendor payload present and inline-safe,
and the CLI flag absorptions. What only a browser can verify (ELK render,
context menu, focus feel) is recorded in DL-71, not tested here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from test_viz import CORPUS_DIR, LOWERABLE_CORPUS, catalog_of, corpus_catalog, runner
from test_viz_html import _vendor_bytes

from dsl41.cli import app
from dsl41.derive import derive_graph
from dsl41.viz_explore import _elements, _flatten, _shape, to_explore_html

# ------------------------------------------------------------ vendor integrity


#: @ungap/custom-elements 1.3.0 min.js, copied byte-exact from npm like
#: mermaid's payload -- so it gets a full hash pin, not a size floor.
_CUSTOM_ELEMENTS_SHA256 = "cc14433db77c53e92706d93a0c8e3df870d9826c6c334044c9fe976c2726cb22"


def test_vendored_cytoscape_bundle_is_inline_safe_and_attributed() -> None:
    payload = _vendor_bytes("cytoscape-explore.iife.min.js")
    # esbuild output is not byte-reproducible, so a floor -- set above the
    # pre-DL-190 bundle (1,943,787 bytes), so a rebuild that lost the
    # expand-collapse extension fails here and not in a browser
    assert len(payload) > 1_960_000
    assert b"</script" not in payload  # inline-safety: embedded without escaping
    assert payload.startswith(b"/*!")  # attribution banner from vendor_mermaid.sh
    assert b"EPL-2.0" in payload[:400]
    assert b"cytoscape-expand-collapse 4.1.1 (MIT)" in payload[:400]  # DL-190
    assert b"var cyBundle" in payload[:600]  # the IIFE global the page JS expects
    assert b"expandCollapse" in payload  # the core extension name the page calls


def test_vendored_custom_elements_polyfill_is_the_pinned_npm_payload() -> None:
    # cytoscape-context-menus builds its menu from CUSTOMIZED BUILT-IN elements
    # (customElements.define(..., {extends: "div"})), which WebKit does not
    # implement -- this payload supplies them (DL-77).
    payload = _vendor_bytes("custom-elements.min.js")
    assert hashlib.sha256(payload).hexdigest() == _CUSTOM_ELEMENTS_SHA256
    assert b"</script" not in payload  # inline-safety: embedded without escaping
    assert payload.startswith(b"/*!")  # upstream's own banner is the attribution
    assert b"ISC" in payload[:80]
    assert b"customElements" in payload


# ------------------------------------------------------------------- elements


def _corpus_elements(name: str) -> dict[str, list[dict[str, object]]]:
    catalog = catalog_of((CORPUS_DIR / name).read_text(encoding="utf-8"))
    return _elements(catalog, derive_graph(catalog))


def _flow_edges(els: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    """The derived edges alone. Lock links share the `edges` list (cytoscape
    has one), carry no derivation, and are never one of `graph.edges` (DL-192)."""
    return [e for e in els["edges"] if "lock" not in str(e["classes"]).split()]


def _flow_nodes(els: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    """The catalog and EXT nodes alone: a lock hub is neither (DL-192)."""
    return [n for n in els["nodes"] if "lock" not in str(n["classes"]).split()]


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


#: A box nested inside a box (B holds M, N, IB; IB holds IM) -- the shape
#: DL-190's `collapse_threshold` marking rule needs to prove itself against:
#: a nested box can be over the threshold too, and must never be marked
#: itself. test_viz_explore_browser.py imports this one (unlike S_EDGE_TEXT
#: above, which predates this module and stayed duplicated per file) --
#: one module owns the text, the other builds pages from it.
_NESTED_BOX_TEXT = (
    "insert_job: P\njob_type: c\ncommand: p\nmachine: m1\n\n"
    "insert_job: Q\njob_type: c\ncommand: q\nmachine: m1\n\n"
    "insert_job: B\njob_type: b\ncondition: s(P)\n\n"
    "insert_job: M\njob_type: c\nbox_name: B\ncondition: s(Q)\ncommand: m\nmachine: m1\n\n"
    "insert_job: N\njob_type: c\nbox_name: B\ncommand: n\nmachine: m1\n\n"
    "insert_job: IB\njob_type: b\nbox_name: B\n\n"
    "insert_job: IM\njob_type: c\nbox_name: IB\ncommand: im\nmachine: m1\n\n"
    "insert_job: C\njob_type: c\ncondition: s(B)\ncommand: c\nmachine: m1\n\n"
    "insert_job: D\njob_type: c\ncondition: s(M)\ncommand: d\nmachine: m1\n"
)


def test_elements_collapse_threshold_never_marks_a_nested_box() -> None:
    # DL-190: the rule is TOP-LEVEL boxes only. B has 3 direct members (M, N,
    # IB) -- over a threshold of 0, and gets marked. IB has 1 direct member
    # (IM) -- also over 0, but IB's own parent is B, not None, so the
    # emitter must never mark it: a nested box goes with its parent.
    catalog = catalog_of(_NESTED_BOX_TEXT)
    els = _elements(catalog, derive_graph(catalog), collapse_threshold=0)
    by_id = {n["data"]["id"]: n["data"] for n in els["nodes"]}  # type: ignore[index]
    assert by_id["B"].get("collapsed") is True
    assert "collapsed" not in by_id["IB"]
    assert "collapsed" not in by_id["IM"]  # not a box at all


#: Every condition shape the page has a rule for, in one catalog: an
#: all-of-any with one OR (AOA) and with two (TWO), a flat any (ANY), an
#: any-of-all (AOFA), a nesting too deep to draw (CPX), an OR with a lock in
#: it (LOCK), a plain AND (PLAIN), a nested AND that flattens to one (FLAT),
#: a BOX whose own condition is an OR, and a member whose branched edge folds
#: into a meta-edge when the box collapses. test_viz_explore_browser.py
#: imports this one and drives the page it emits (DL-191).
_COND_TEXT = (
    "".join(
        f"insert_job: {name}\njob_type: c\ncommand: {name.lower()}\nmachine: m1\n\n"
        for name in ("A", "B", "C", "D", "E")
    )
    + "insert_job: AOA\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: s(A) & (s(B) | f(C))\n\n"
    "insert_job: TWO\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: (s(A) | s(B)) & (s(C) | s(D)) & s(E)\n\n"
    "insert_job: ANY\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: s(A) | s(B) | s(C)\n\n"
    "insert_job: AOFA\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: (s(A) & s(B)) | s(C)\n\n"
    "insert_job: CPX\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: s(A) & (s(B) | (s(C) & s(D)))\n\n"
    "insert_job: LOCK\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: n(A) | s(B)\n\n"
    "insert_job: PLAIN\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(A) & s(B)\n\n"
    "insert_job: FLAT\njob_type: c\ncommand: x\nmachine: m1\n"
    "condition: s(A) & (s(B) & s(C))\n\n"
    "insert_job: BOX\njob_type: b\ncondition: s(A) | s(B)\n\n"
    "insert_job: MEM\njob_type: c\nbox_name: BOX\ncommand: m\nmachine: m1\n"
    "condition: s(C) | s(D)\n\n"
    "insert_job: MEM2\njob_type: c\nbox_name: BOX\ncommand: m2\nmachine: m1\n\n"
    "insert_job: OUT\njob_type: c\ncommand: o\nmachine: m1\ncondition: s(MEM) | s(A)\n"
)


def _cond_elements() -> dict[str, list[dict[str, object]]]:
    catalog = catalog_of(_COND_TEXT)
    return _elements(catalog, derive_graph(catalog))


def _badges(els: dict[str, list[dict[str, object]]]) -> dict[str, str]:
    """node id -> the badge its label wears, catalog nodes only. The six-value
    shape stays in the emitter (DL-193); what crosses is this."""
    return {
        n["data"]["id"]: n["data"]["cond_badge"]  # type: ignore[index,misc]
        for n in els["nodes"]
        if "cond_badge" in n["data"]  # type: ignore[operator]
    }


def _branches(els: dict[str, list[dict[str, object]]], target: str) -> dict[str, str | None]:
    """producer id -> the branch label of its edge into `target`."""
    return {
        e["data"]["source"]: e["data"]["branch"]  # type: ignore[index,misc]
        for e in _flow_edges(els)
        if e["data"]["target"] == target  # type: ignore[index]
    }


def _leaves(tree: object) -> list[dict[str, object]]:
    """Every leaf of one cond_tree, in order."""
    node = cast("dict[str, object]", tree)
    if "op" not in node:
        return [node]
    items = cast("list[object]", node["items"])
    return [leaf for item in items for leaf in _leaves(item)]


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
    # cls stays the FIRST class, the style channel it always was; `any` is the
    # second and orthogonal one -- this arrow is one alternative of an OR
    # (DL-191), which the arrowhead draws and the line style does not touch
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    els = _elements(catalog, graph)
    flow = _flow_edges(els)
    classes = [str(e["classes"]).split() for e in flow]
    assert [c[0] for c in classes] == [e.cls for e in graph.edges]
    assert [c[1:] == ["any"] for c in classes] == [
        e["data"]["branch"] is not None
        for e in flow  # type: ignore[index]
    ]


def test_elements_cover_every_node_and_edge() -> None:
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    els = _elements(catalog, graph)
    nodes = _flow_nodes(els)  # lock hubs come after these and are neither (DL-192)
    catalog_ids = [n["data"]["id"] for n in nodes[: len(graph.nodes)]]  # type: ignore[index]
    assert catalog_ids == graph.nodes  # catalog nodes first, source order
    assert len(_flow_edges(els)) == len(graph.edges)
    ext = nodes[len(graph.nodes) :]
    assert all("ext" in str(n["classes"]) for n in ext)
    endpoints = {e.src for e in graph.edges} | {e.dst for e in graph.edges}
    # a mutex member no job defines is EXT too, and belongs to no edge
    danglers = {m for group in graph.mutex_groups for m in group} - set(graph.nodes)
    assert {n["data"]["id"] for n in ext} == (endpoints | danglers) - set(graph.nodes)  # type: ignore[index]


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


#: DL-175's own S-EDGE fixture, closing its named residue in this module too
#: (DL-176): a local `insert_job: foo^PRD` whose display form collides with
#: a genuinely foreign M33 producer referenced by `bar`'s condition. Kept
#: local, not imported (this suite has no shared-fixture module) -- also
#: appears in test_derive.py, test_viz.py and test_backend_uc.py.
S_EDGE_TEXT = (
    "insert_xinst: PRD\nxtype: a\nxmachine: h.example.com\nxport: 9000\n\n"
    "insert_job: foo^PRD\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: bar\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(foo^PRD)\n"
)


def test_dl176_local_dangler_and_foreign_producer_are_distinct_cytoscape_elements() -> None:
    """DL-175's review named this module's `endpoint in charted` test as its
    own deferred seventh S-EDGE site: a local job `foo^PRD` and the display
    form of a genuinely foreign M33 producer `foo^PRD` used to fold onto ONE
    cytoscape node, no `ext` element synthesized. Now two elements: the
    local job keeps its own raw id, the foreign producer gets a namespaced
    id (it collides with the local job's id), and the M33 edge's `source`
    is the foreign element, never the local one (DL-176)."""
    catalog = catalog_of(S_EDGE_TEXT)
    graph = derive_graph(catalog)
    (edge,) = graph.edges
    assert edge.mapping_row == "M33" and edge.src == "foo^PRD"
    els = _elements(catalog, graph)
    ids = [n["data"]["id"] for n in els["nodes"]]  # type: ignore[index]
    assert ids == ["foo^PRD", "bar", "_foo^PRD"]  # two distinct elements, not one
    ext = _node(els, "_foo^PRD")
    assert ext["classes"] == "ext"
    assert ext["data"]["label"] == "foo^PRD"  # type: ignore[index]  # same display form
    (ext_edge,) = els["edges"]
    assert ext_edge["data"]["source"] == "_foo^PRD"  # type: ignore[index]  # never "foo^PRD"
    assert ext_edge["data"]["target"] == "bar"  # type: ignore[index]


# ------------------------------------------------- condition structure (DL-191)


def test_elements_carry_the_condition_text_of_every_bearing_attribute() -> None:
    # slice 1: the text the panel shows, rendered from the Cond tree, one row
    # per condition-bearing attribute -- a box override is as visible as a
    # condition, and a job with neither carries explicit nulls
    els = _corpus_elements("sem12_external_gate.jil")
    gate = _node(els, "gate_box")["data"]
    assert gate["condition"] is None  # type: ignore[index]
    assert gate["box_success"] == "s(gate_outside_job)"  # type: ignore[index]
    assert gate["box_failure"] == "v(ABORT_FLAG) = 1"  # type: ignore[index]
    # both of those edges are M16 -- the attribute they came from is read off
    # the walk, never guessed from the mapping row. Two single readings badge
    # nothing: every incoming arrow must hold, which is the default.
    assert gate["cond_badge"] == ""  # type: ignore[index]
    member = _node(els, "gate_member_a")["data"]
    assert member["condition"] is None and member["cond_badge"] == ""  # type: ignore[index]


def test_elements_or_join_marks_each_alternative_with_its_branch() -> None:
    # the corpus OR join (DL-38's T-003): a flat any, one branch per operand,
    # every arrow labelled with it and classed `any`
    els = _corpus_elements("fold_t003_or_join.jil")
    assert _badges(els)["fold_or_join"] == " \N{LOGICAL OR}"
    assert _branches(els, "fold_or_join") == {"fold_or_m1": "|1", "fold_or_m2": "|2"}
    labels = {
        e["data"]["source"]: (e["data"]["label"], e["classes"])  # type: ignore[index]
        for e in _flow_edges(els)
        if e["data"]["target"] == "fold_or_join"  # type: ignore[index]
    }
    # a flat OR carries NO label suffix: the hollow head already says "one
    # alternative", and one label per arrow groups nothing (DL-191 R2)
    assert labels["fold_or_m1"] == ("", "assumed any")
    tree = _node(els, "fold_or_join")["data"]["cond_tree"]["condition"]  # type: ignore[index,call-overload]
    assert tree["op"] == "or"
    assert [leaf["atom"] for leaf in _leaves(tree)] == ["s(fold_or_m1)", "s(fold_or_m2)"]
    assert all(leaf["edge"] for leaf in _leaves(tree))  # every alternative is an arrow


def test_elements_bare_notrunning_atoms_are_lock_leaves_with_no_edge() -> None:
    # M07: a bare local n() is a mutex record, not an edge -- it was invisible
    # on the page entirely. Now it is a leaf that says why it has no arrow.
    els = _corpus_elements("m07_mutex.jil")
    assert _badges(els)["mutex_b"] == ""  # an AND of a lock and an arrow
    leaves = _leaves(_node(els, "mutex_b")["data"]["cond_tree"]["condition"])  # type: ignore[index,call-overload]
    # no edge id IS the lock: the leaf states the fact once (DL-193)
    assert [(leaf["atom"], leaf["edge"]) for leaf in leaves] == [
        ("n(mutex_a)", None),
        ("s(mutex_feeder)", "e0"),
    ]
    serial = _leaves(_node(els, "mutex_serial")["data"]["cond_tree"]["condition"])  # type: ignore[index,call-overload]
    assert serial == [{"atom": "n(mutex_serial)", "edge": None, "branch": None}]


def test_shape_classifies_every_drawable_reading() -> None:
    """The six-value vocabulary stays on the emitter's side of the boundary
    (DL-193): it picks the branch labels and the canvas suffix here. What the
    page is handed is the badge below."""
    catalog = catalog_of(_COND_TEXT)
    assert {
        name: _shape(_flatten(cond))
        for name, job in catalog.jobs.items()
        for origin, cond, _span in job.iter_conditions()
        if origin == "condition"
    } == {
        "AOA": "all-of-any",
        "TWO": "all-of-any",
        "ANY": "any",
        "AOFA": "any-of-all",
        "CPX": "complex",
        "LOCK": "any",
        "PLAIN": "all",
        "FLAT": "all",  # `s(A) & (s(B) & s(C))`: one AND, not a nesting
        "BOX": "any",
        "MEM": "any",
        "OUT": "any",
    }


def test_elements_badge_the_or_readings_and_only_those() -> None:
    # the three-value badge the page prints: an OR the canvas draws as
    # branches, the starred form for one too deep to draw, and nothing at all
    # for the default reading (every incoming arrow must hold)
    OR, DEEP = " \N{LOGICAL OR}", " \N{LOGICAL OR}*"
    assert _badges(_cond_elements()) == {
        "A": "",
        "B": "",
        "C": "",
        "D": "",
        "E": "",
        "AOA": OR,
        "TWO": OR,
        "ANY": OR,
        "AOFA": OR,
        "CPX": DEEP,
        "LOCK": OR,
        "PLAIN": "",
        "FLAT": "",
        "BOX": OR,
        "MEM": OR,
        "MEM2": "",
        "OUT": OR,
    }


def test_elements_all_of_any_names_each_or_when_there_is_more_than_one() -> None:
    els = _cond_elements()
    # one OR: the operand index carries it alone, and the AND operand outside
    # the OR stays unlabelled -- an unlabelled arrow is an AND arrow
    assert _branches(els, "AOA") == {"A": None, "B": "|1", "C": "|2"}
    # two ORs: a letter per OR in operand order, so `a|1` and `b|1` are
    # different alternations rather than the same one twice
    assert _branches(els, "TWO") == {"A": "a|1", "B": "a|2", "C": "b|1", "D": "b|2", "E": None}


def test_elements_any_of_all_shares_one_branch_across_a_whole_alternative() -> None:
    # `(s(A) & s(B)) | s(C)`: A and B are one alternative and carry one label
    els = _cond_elements()
    assert _branches(els, "AOFA") == {"A": "|1", "B": "|1", "C": "|2"}
    tree = _node(els, "AOFA")["data"]["cond_tree"]["condition"]  # type: ignore[index,call-overload]
    assert tree["op"] == "or"
    assert [item.get("op") for item in tree["items"]] == ["and", None]


def test_elements_complex_shape_labels_no_branch_at_all() -> None:
    # deeper than one alternation: the page cannot draw it honestly, so it
    # labels nothing and the badge sends the reader to the tree and the text
    els = _cond_elements()
    assert _branches(els, "CPX") == {"A": None, "B": None, "C": None, "D": None}
    assert all(
        "any" not in str(e["classes"])
        for e in _flow_edges(els)
        if e["data"]["target"] == "CPX"  # type: ignore[index]
    )


def test_elements_lock_inside_an_or_keeps_its_branch_without_an_edge() -> None:
    # `n(A) | s(B)`: the lock is the first alternative -- it has a branch and
    # no arrow, which is exactly why the tree has to show it
    els = _cond_elements()
    assert _branches(els, "LOCK") == {"B": "|2"}
    leaves = _leaves(_node(els, "LOCK")["data"]["cond_tree"]["condition"])  # type: ignore[index,call-overload]
    assert leaves == [
        {"atom": "n(A)", "edge": None, "branch": "|1"},
        {"atom": "s(B)", "edge": leaves[1]["edge"], "branch": "|2"},
    ]


def test_elements_two_or_attributes_never_share_a_branch_identity() -> None:
    """The review's MAJOR: a box whose condition and box_success are both ORs
    states two different alternations, and both number themselves from one.
    The label stays human, the identity the page groups and paints by carries
    the attribute."""
    text = (
        "".join(
            f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n"
            for name in ("A", "B", "C", "D")
        )
        + "insert_job: BX\njob_type: b\ncondition: s(A) | s(B)\nbox_success: s(C) | s(D)\n"
    )
    catalog = catalog_of(text)
    els = _elements(catalog, derive_graph(catalog))
    keyed = {
        str(e["data"]["source"]): (e["data"]["attr"], e["data"]["branch"])  # type: ignore[index]
        for e in _flow_edges(els)
    }
    assert keyed == {
        "A": ("condition", "|1"),
        "B": ("condition", "|2"),
        "C": ("box_success", "|1"),
        "D": ("box_success", "|2"),
    }
    # ...and the tree states each attribute's branches under that attribute,
    # so the page composes the same identity for the swatch and the paint
    trees = _node(els, "BX")["data"]["cond_tree"]  # type: ignore[index]
    branches = {
        attr: [leaf["branch"] for leaf in _leaves(tree)]
        for attr, tree in trees.items()  # type: ignore[union-attr]
    }
    assert branches == {"condition": ["|1", "|2"], "box_success": ["|1", "|2"]}
    assert "function branchKey(attr, branch)" in to_explore_html(catalog)


def test_elements_canvas_suffix_only_where_it_groups_arrows() -> None:
    """DL-191 R2 (visual check): the hollow head already says "one
    alternative", so the label suffix is spent only where it GROUPS -- an
    any-of-all's shared `|k`, and which OR of several under one AND. The
    branch data is complete either way."""
    els = _cond_elements()

    def labels(target: str) -> dict[str, object]:
        return {
            str(e["data"]["source"]): e["data"]["label"]  # type: ignore[index]
            for e in _flow_edges(els)
            if e["data"]["target"] == target  # type: ignore[index]
        }

    assert labels("ANY") == {"A": "", "B": "", "C": ""}  # flat any: nothing to group
    assert labels("AOA") == {"A": "", "B": "", "C": "f"}  # one OR under an AND
    assert labels("AOFA") == {"A": "|1", "B": "|1", "C": "|2"}  # the alternative
    assert labels("TWO") == {"A": "|a", "B": "|a", "C": "|b", "D": "|b", "E": ""}
    # the branches themselves are untouched, and still tell the two ORs apart
    assert _branches(els, "TWO") == {"A": "a|1", "B": "a|2", "C": "b|1", "D": "b|2", "E": None}


def test_elements_every_corpus_edge_matches_the_atom_it_derives_from() -> None:
    """No silent loss, over the whole corpus: _elements raises when a derived
    edge matches no condition atom, or a non-mutex atom no edge. Each file on
    its own, then all of them as one catalog -- and the tree leaves must name
    every emitted edge exactly once."""
    for path in LOWERABLE_CORPUS:
        catalog = catalog_of(path.read_text(encoding="utf-8"))
        graph = derive_graph(catalog)
        assert len(_flow_edges(_elements(catalog, graph))) == len(graph.edges), path.name
    catalog = corpus_catalog()
    els = _elements(catalog, derive_graph(catalog))
    named: list[str] = []
    for node in els["nodes"]:
        for tree in node["data"].get("cond_tree", {}).values():  # type: ignore[union-attr]
            named.extend(str(leaf["edge"]) for leaf in _leaves(tree) if leaf["edge"])
    assert sorted(named) == sorted(str(e["data"]["id"]) for e in _flow_edges(els))  # type: ignore[index]
    assert len(named) == len(set(named))  # each edge named by exactly one leaf


# --------------------------------------------------------------- locks (DL-192)


def _locks(els: dict[str, list[dict[str, object]]]) -> dict[str, dict[str, object]]:
    """The lock hubs by id."""
    return {
        str(n["data"]["id"]): n["data"]  # type: ignore[index,misc]
        for n in els["nodes"]
        if "lock" in str(n["classes"]).split()
    }


def _lock_links(els: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    return [e for e in els["edges"] if "lock" in str(e["classes"]).split()]


def test_elements_draw_one_hub_per_consumed_resource() -> None:
    # DL-192: resources are not in IR-G, so the emitter reads them off IR-F.
    # R_UNUSED is declared and consumed by nobody: no hub, and the summary
    # counts it instead (below).
    els = _corpus_elements("viz_locks.jil")
    hubs = _locks(els)
    assert [h for h in hubs if h.startswith("lock:r:")] == [
        "lock:r:R_ONE",
        "lock:r:R_BIG",
        "lock:r:R_GATE",
    ]
    one = hubs["lock:r:R_ONE"]
    assert one["label"] == "\N{LOCK} R_ONE (1)"
    assert one["capacity"] == 1
    # one sentence per member, worded here and printed by the page (DL-193)
    assert one["members"] == [
        {"id": "lk_x1", "job": "lk_x1", "boxes": [], "how": "1 unit, released on completion"},
        {
            "id": "lk_x2",
            "job": "lk_x2",
            "boxes": ["lk_box"],
            # FREE=N: the units are never given back (DL-50)
            "how": "1 unit, never released",
        },
    ]
    assert hubs["lock:r:R_BIG"]["label"] == "\N{LOCK} R_BIG (3)"


def test_elements_resource_link_label_thins_like_an_edge_label() -> None:
    # quantity only when it is more than one unit, FREE only when stated
    els = _corpus_elements("viz_locks.jil")
    labels = {
        str(e["data"]["target"]): e["data"]["label"]  # type: ignore[index,misc]
        for e in _lock_links(els)
        if e["data"]["lock"] == "resource"  # type: ignore[index]
    }
    # lk_x5 lists R_BIG twice and the two groups coalesce into one link of
    # three units; a summed quantity states no single FREE letter
    assert labels == {"lk_x1": "", "lk_x2": "N", "lk_x3": "2 A", "lk_x4": "2", "lk_x5": "3"}


def test_elements_unsized_resource_hub_says_so() -> None:
    # two ways to have no capacity, one reading: an undeclared resource
    # (L016's finding) and a malformed `amount` (preflight's, DL-50)
    text = (
        "insert_resource: BAD\nres_type: R\namount: many\n\n"
        "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (BAD, QUANTITY=1) and (NOWHERE, QUANTITY=1)\n"
    )
    catalog = catalog_of(text)
    hubs = _locks(_elements(catalog, derive_graph(catalog)))
    assert hubs["lock:r:BAD"]["label"] == "\N{LOCK} BAD (?)"
    assert hubs["lock:r:BAD"]["capacity"] is None
    assert hubs["lock:r:NOWHERE"]["label"] == "\N{LOCK} NOWHERE (?)"


def test_elements_mutex_pair_tees_the_end_that_waits() -> None:
    # a pair forms from ONE reference, so one-way is the common case:
    # lk_a names n(lk_b) and waits; lk_c and lk_d name each other
    els = _corpus_elements("viz_locks.jil")
    pairs = {
        (str(e["data"]["source"]), str(e["data"]["target"])): e["data"]  # type: ignore[index]
        for e in _lock_links(els)
        if "pair" in str(e["classes"]).split()
    }
    one_way = pairs[("lk_a", "lk_b")]
    assert (one_way["source_tee"], one_way["target_tee"]) == (True, False)
    assert one_way["directions"] == ["lk_a waits while lk_b runs"]
    mutual = pairs[("lk_c", "lk_d")]
    assert (mutual["source_tee"], mutual["target_tee"]) == (True, True)
    assert mutual["directions"] == [
        "lk_c waits while lk_d runs",
        "lk_d waits while lk_c runs",
    ]


def test_elements_complete_clique_is_one_hub_with_a_row_per_member() -> None:
    # DL-35 item 6, kept: only a COMPLETE clique collapses to a hub, and the
    # tee marks the members that wait (lk_g names nobody, so its end is bare)
    els = _corpus_elements("viz_locks.jil")
    hub = _locks(els)["lock:m:lk_e+lk_f+lk_g"]
    assert hub["label"] == "\N{LOCK} mutex"
    assert hub["capacity"] is None
    assert [m["how"] for m in hub["members"]] == [  # type: ignore[index,union-attr]
        "waits while lk_f, lk_g run",
        "waits while lk_g runs; lk_e waits while it runs",
        "lk_e, lk_f wait while it runs",
    ]
    tees = {
        str(e["data"]["target"]): e["data"]["target_tee"]  # type: ignore[index,misc]
        for e in _lock_links(els)
        if e["data"]["source"] == "lock:m:lk_e+lk_f+lk_g"  # type: ignore[index]
    }
    assert tees == {"lk_e": True, "lk_f": True, "lk_g": False}


def test_elements_threshold_resource_holds_nothing() -> None:
    """MAJOR from the review: `res_type: T` is a level check. The pool
    classifies it 'gate' with no release policy BEFORE consulting the FREE
    table (capacity.requirement_demand), and the panel used to report it as
    held units released on completion."""
    els = _corpus_elements("viz_locks.jil")
    (member,) = _locks(els)["lock:r:R_GATE"]["members"]  # type: ignore[misc]
    assert member["how"] == "threshold gate: needs 2 free, holds nothing"
    (link,) = [e for e in _lock_links(els) if e["data"]["target"] == "lk_x4"]  # type: ignore[index]
    assert link["data"]["demand"] == member["how"]  # type: ignore[index]


def test_elements_coalesce_one_job_two_groups_on_one_resource() -> None:
    """MINOR from the review: two groups on one bucket drew two links on top
    of each other and two rows of one unit, while the pool reserves their
    SUM. One link, one row, the summed demand -- `capacity.merge_requirements`
    is the one owner of that arithmetic."""
    els = _corpus_elements("viz_locks.jil")
    big = [m for m in _locks(els)["lock:r:R_BIG"]["members"] if m["job"] == "lk_x5"]  # type: ignore[union-attr,index]
    # (R_BIG, QUANTITY=1) and (R_BIG, QUANTITY=2) are three units, once
    assert big == [
        {
            "id": "lk_x5",
            "job": "lk_x5",
            "boxes": [],
            "how": "3 units, released on completion",
        }
    ]
    links = [e for e in _lock_links(els) if e["data"]["target"] == "lk_x5"]  # type: ignore[index]
    assert len(links) == 1 and links[0]["data"]["label"] == "3"  # type: ignore[index]


def test_elements_merge_the_most_restrictive_release_of_two_groups() -> None:
    # asymmetric FREE never frees early: the pool's own merge rule (DL-50)
    text = (
        "insert_resource: R\nres_type: R\namount: 4\n\n"
        "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
        "resources: (R, QUANTITY=1, FREE=A) and (R, QUANTITY=1, FREE=N)\n"
    )
    catalog = catalog_of(text)
    (member,) = _locks(_elements(catalog, derive_graph(catalog)))["lock:r:R"]["members"]  # type: ignore[misc]
    assert member["how"] == "2 units, never released"


def test_elements_incomplete_mutex_component_stays_pairwise() -> None:
    """DL-35 item 6: a hub claims a COMPLETE clique. lk_p names n(lk_q) and
    lk_q names n(lk_r), so two of the three pairs are stated and no hub may
    speak for them."""
    els = _corpus_elements("viz_locks.jil")
    assert [h for h in _locks(els) if h.startswith("lock:m:")] == ["lock:m:lk_e+lk_f+lk_g"]
    pairs = [
        (str(e["data"]["source"]), str(e["data"]["target"]))  # type: ignore[index]
        for e in _lock_links(els)
        if "pair" in str(e["classes"]).split()
    ]
    assert ("lk_p", "lk_q") in pairs and ("lk_q", "lk_r") in pairs
    assert ("lk_p", "lk_r") not in pairs  # never stated, never drawn


def test_elements_self_mutex_is_a_badge_and_never_a_node() -> None:
    els = _corpus_elements("viz_locks.jil")
    assert _node(els, "lk_h")["data"]["self_lock"] is True  # type: ignore[index]
    assert "self_lock" not in _node(els, "lk_a")["data"]  # type: ignore[operator]
    assert not any("lk_h" in hub for hub in _locks(els))
    ends = {str(e["data"]["source"]) for e in _lock_links(els)}  # type: ignore[index]
    ends |= {str(e["data"]["target"]) for e in _lock_links(els)}  # type: ignore[index]
    assert "lk_h" not in ends


def test_elements_m07_corpus_file_keeps_the_report_shapes() -> None:
    # the M07 fixture: one mutual pair, one self-exclusion, and the s() edge
    # beside them untouched by any of it
    els = _corpus_elements("m07_mutex.jil")
    links = _lock_links(els)
    assert [
        (str(e["data"]["source"]), str(e["data"]["target"]))  # type: ignore[index]
        for e in links
    ] == [("mutex_a", "mutex_b")]
    assert links[0]["data"]["directions"] == [  # type: ignore[index]
        "mutex_a waits while mutex_b runs",
        "mutex_b waits while mutex_a runs",
    ]
    assert _node(els, "mutex_serial")["data"]["self_lock"] is True  # type: ignore[index]
    assert len(_flow_edges(els)) == 1  # s(mutex_feeder) is still an ordinary edge


def test_elements_dangling_mutex_target_becomes_an_ext_node() -> None:
    # a bare n() naming a job nothing defines: L001 owns the finding, and the
    # page draws the reference rather than dropping it (DL-35a's rule)
    catalog = catalog_of(
        "insert_job: j2\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(ghost)\n"
    )
    els = _elements(catalog, derive_graph(catalog))
    assert _node(els, "ghost")["classes"] == "ext"
    (link,) = _lock_links(els)
    assert (link["data"]["source"], link["data"]["target"]) == ("ghost", "j2")  # type: ignore[index]
    assert link["data"]["target_tee"] is True  # type: ignore[index]  # j2 is the one that waits


def test_elements_lock_ids_never_collide_with_a_job_named_like_one() -> None:
    # cytoscape ids are one namespace over every element, and `lock:r:R` is a
    # legal job name -- the hub widens with the same underscore idiom an EXT
    # node and an edge id use
    text = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: lock:r:R\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1)\n"
    )
    catalog = catalog_of(text)
    els = _elements(catalog, derive_graph(catalog))
    ids = [n["data"]["id"] for n in els["nodes"]]  # type: ignore[index]
    ids += [e["data"]["id"] for e in els["edges"]]  # type: ignore[index]
    assert ids == ["lock:r:R", "_lock:r:R", "lock:e:0"]
    assert len(ids) == len(set(ids))


def test_elements_whole_corpus_draws_both_lock_kinds() -> None:
    catalog = corpus_catalog()
    els = _elements(catalog, derive_graph(catalog))
    assert {str(data["lock"]) for data in _locks(els).values()} == {"resource", "mutex"}
    # every lock link joins elements the page actually has
    ids = {str(n["data"]["id"]) for n in els["nodes"]}  # type: ignore[index]
    for link in _lock_links(els):
        assert str(link["data"]["source"]) in ids  # type: ignore[index]
        assert str(link["data"]["target"]) in ids  # type: ignore[index]


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


def _page_payload(page: str) -> dict[str, Any]:
    raw = _GRAPH_DATA.search(page)
    assert raw is not None
    assert "<" not in raw.group(1)  # the escaping invariant the embedding rests on
    result: dict[str, Any] = json.loads(raw.group(1))
    return result


def _page_elements(page: str) -> dict[str, list[dict[str, object]]]:
    """The elements half of the payload: the header totals ride beside them
    (DL-193)."""
    payload = _page_payload(page)
    return {"nodes": payload["nodes"], "edges": payload["edges"]}


def test_to_explore_html_escapes_the_json_but_round_trips_elements() -> None:
    catalog = corpus_catalog()
    graph = derive_graph(catalog)
    page = to_explore_html(catalog, graph)
    els = _elements(catalog, graph)
    assert _page_elements(page) == els
    # and the totals travel with them: the page recomputed this arithmetic in
    # JavaScript, over the same arrays, and the two were kept equal by hand
    totals = _page_payload(page)["totals"]
    assert totals["nodes"] == sum(
        1 for n in els["nodes"] if "lock" not in str(n["classes"]).split()
    )
    assert totals["edges"] == len(_flow_edges(els))


def test_to_explore_html_embeds_each_vendor_payload_exactly_once() -> None:
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    for name in ("cytoscape-explore.iife.min.js", "custom-elements.min.js"):
        probe = _vendor_bytes(name).decode("utf-8")[:200]
        assert page.count(probe) == 1


def test_to_explore_html_loads_the_polyfill_before_the_cytoscape_bundle() -> None:
    # order is the whole point: the plugin's customized built-in elements are
    # registered when the bundle loads, so customElements must already be
    # patched by then, or Safari throws "Illegal constructor" (DL-77)
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    polyfill = _vendor_bytes("custom-elements.min.js").decode("utf-8")[:200]
    bundle = _vendor_bytes("cytoscape-explore.iife.min.js").decode("utf-8")[:200]
    assert page.index(polyfill) < page.index(bundle)


def test_to_explore_html_wires_everything_essential_above_the_optional_plugin() -> None:
    # DL-77: the context menu is the one optional part of the page. It is
    # registered LAST and guarded, so a plugin that throws (an unpolyfilled
    # browser, a future bump) costs itself and nothing else -- the bug this
    # rule comes from left the ELK layout, the toolbar and the search dead
    # below the throw, with the page looking merely slow.
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    registration = page.index("cy.contextMenus({ menuItems: menuItems })")
    for essential in (
        'wire("show-all", showAll);',
        'wire("fit", function () {',
        'wire("hide-others", hideOthers);',
        'document.getElementById("search").addEventListener',
        'wire("find-select", function (evt) {',
        'initial.on("layoutstop"',
    ):
        assert page.index(essential) < registration, essential
    # ...and the loss is named, not swallowed. Two optional extensions since
    # DL-190, so each APPENDS its notice: one failing must not erase the other
    assert page.count("} catch (err) {") == 2
    assert 'lostFeature += " \N{MIDDLE DOT} context menu unavailable in this browser";' in page
    assert 'lostFeature += " \N{MIDDLE DOT} box collapse unavailable in this browser";' in page
    assert page.count("+ lostFeature;") == 1  # every updateStats keeps it
    # the expand-collapse extension (DL-190) is guarded the same way. The
    # layout is CONSTRUCTED and its stop handler attached above both optional
    # plugins -- that is the wiring DL-77 orders -- and only the RUN sits
    # below the expand-collapse guard, so the handler fires knowing whether
    # there is an extension to fold with (DL-193)
    ec_registration = page.index("cy.expandCollapse({")
    assert "try {" in page[ec_registration - 60 : ec_registration]
    assert page.index('initial.on("layoutstop"') < ec_registration < registration
    assert ec_registration < page.index("initial.run();") < registration


def test_to_explore_html_routes_edges_along_the_layout_axis() -> None:
    # ELK lays the graph out in layers; a bezier between node centres throws
    # that away and reads as a spline tangle, so edges route orthogonally
    # along the same axis the layout used (DL-77).
    catalog = catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n")
    page = to_explore_html(catalog)
    assert '"curve-style": "taxi"' in page
    assert '"taxi-direction": TAXI_DIRECTION' in page
    assert 'TAXI_DIRECTION = DIRECTION === "DOWN" ? "vertical" : "horizontal"' in page
    # the one exception: taxi cannot draw an edge whose endpoints overlap, and
    # a member pointing at its own box is exactly that -- those keep the bezier
    # rather than silently vanishing from the picture
    assert 'selector: "edge.nesting"' in page
    assert page.count('"curve-style": "bezier"') == 1
    # re-classified after every collapse/expand (DL-190): a collapse re-points
    # a member's edges at its box, so a meta-edge can land on a box's ancestor
    assert 'edge.toggleClass("nesting", nested)' in page
    assert 'cy.on("expandcollapse.aftercollapse", function (evt) {' in page
    assert 'cy.on("expandcollapse.afterexpand", function (evt) {' in page


def test_to_explore_html_survives_marker_shaped_job_and_title() -> None:
    # a legal job name containing a substitution marker must not splice the
    # vendor bundle into the elements JSON, and a marker-shaped title must
    # not duplicate the JSON into <title> (review finding: single-pass
    # substitution, replaced content never re-scanned)
    text = "insert_job: EVIL__DSL41_CUSTOM_ELEMENTS_JS__X\njob_type: c\ncommand: x\nmachine: m1\n"
    page = to_explore_html(catalog_of(text), title="x__DSL41_ELEMENTS_JSON__.jil")
    for name in ("cytoscape-explore.iife.min.js", "custom-elements.min.js"):
        probe = _vendor_bytes(name).decode("utf-8")[:200]
        assert page.count(probe) == 1
    names = [n["data"]["id"] for n in _page_elements(page)["nodes"]]  # type: ignore[index]
    assert names == ["EVIL__DSL41_CUSTOM_ELEMENTS_JS__X"]  # JSON intact, name verbatim
    assert "Explore: x__DSL41_ELEMENTS_JSON__.jil</title>" in page


def test_to_explore_html_wires_the_trace_toggle_and_step_functions() -> None:
    # DL-190: the trace-through-boxes control, the two collapse toolbar
    # buttons, and the two step functions the focus items read closure() over
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    assert 'id="trace-boxes"' in page
    assert 'id="collapse-all"' in page
    assert 'id="expand-all"' in page
    assert "function fanInStep(nodes)" in page
    assert "function fanOutStep(nodes)" in page
    # ...and the two tree closures the tree items call, which keep how a box
    # was reached (a box override is a completion predicate, not a start gate)
    assert "function fanInTree(start)" in page
    assert "function fanOutTree(start)" in page
    assert '"fan-in-tree": { word: "fan-in", transitive: true, set: fanInTree },' in page
    assert '"fan-out-tree": { word: "fan-out", transitive: true, set: fanOutTree },' in page
    # ...and the two seats that call the table: the node item and the
    # selection-seeded toolbar button. A table nothing dispatches through
    # would satisfy the two lines above (review finding C11)
    assert "runWalk(kind, n, shortId(n.id()));" in page
    assert "runWalk(kind, selectedNodes(), selectionLabel());" in page
    assert "found = flowSeed.empty() ? cy.collection() : WALKS[kind].set(flowSeed);" in page
    assert "function isOverride(edge)" in page


def test_to_explore_html_folds_without_a_layout_and_carries_the_arrange_button() -> None:
    # DL-196 slice (2): a collapse or an expand runs no layout, no fit and no
    # pan -- `layoutBy: null` is what says so to the extension, and the
    # handlers place the hubs and rewrite the count themselves. The toolbar
    # carries an explicit `arrange` beside `fit`, and the toggle the fold used
    # to share is renamed and DEFAULTS OFF: a node moves only when a layout
    # runs, and a layout runs at load, on the button, or after a hide op with
    # the toggle on.
    page = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    assert "    layoutBy: null,\n" in page
    assert "layoutBy: function" not in page  # the fold called relayoutOrFit through this
    assert 'id="arrange"' in page
    assert page.index('id="fit"') < page.index('id="arrange"') < page.index('id="collapse-all"')
    assert '<input type="checkbox" id="relayout"> arrange after hiding' in page
    assert 'id="relayout" checked' not in page
    assert "re-layout on focus" not in page
    # the button and the toggle's ON branch are one layout path, not two
    assert "function arrangeVisible(suffix, after)" in page
    assert "arrangeVisible(suffix, after);" in page  # relayoutOrFit delegates to it...
    assert 'arrangeVisible("arranged");' in page  # ...and the button runs the same path
    # ...and the find path runs none of it: it expands in place and fits the
    # matches. Read out of runFind's own body: an exclusion pinned to one
    # spelling passes the moment the call is spelled differently (C11)
    find_body = page[
        page.index("function runFind(mode, add)") : page.index("findField.addEventListener")
    ]
    assert "relayoutOrFit" not in find_body and "arrangeVisible" not in find_body
    assert "ec.expandRecursively(box, { layoutBy: null });" in page
    assert "cy.fit(live, 60);" in page  # the matches still drawn when the report runs
    # what still follows a fold: the hubs (DL-192) and the collapsed count.
    # Read out of the two handler bodies rather than counted, so a reindent
    # or a third legitimate caller does not fail this with a bare number.
    assert "function afterFold(" in page
    collapse_at = page.index('cy.on("expandcollapse.aftercollapse"')
    expand_at = page.index('cy.on("expandcollapse.afterexpand"')
    assert "afterFold();" in page[collapse_at:expand_at]
    assert "afterFold();" in page[expand_at : page.index("} catch (err) {", expand_at)]


def test_to_explore_html_carries_the_condition_grammar() -> None:
    # DL-191, the page half: the badge rule, the hollow arrowhead, the branch
    # ramp, the tree the panel renders, and the legend that states the default
    # reading. The browser module drives all of it; this pins the wiring.
    page = to_explore_html(catalog_of(_COND_TEXT))
    # the page source carries the JS escapes, not the characters
    # the badge is emitted, and the page source carries the JSON escape
    assert r'"cond_badge": " \u2228*"' in page  # complex: read the tree and the text
    assert r'"cond_badge": " \u2228"' in page  # an OR the canvas draws as branches
    assert 'function condBadge(ele) { return ele.data("cond_badge") || ""; }' in page
    assert "nodeLabel(ele).length" in page
    assert "all incoming arrows must hold (AND) unless the job" in page
    assert "a bare n() is a lock and draws no arrow" in page
    assert '{ selector: "edge.any", style: {' in page
    assert '"target-arrow-fill": "hollow"' in page
    # the ramp is six colours, none of them the amber of the `assumed` class
    # (visual check), and colour is the redundant channel
    assert page.count('var BRANCH_COLORS = ["#0072b2", "#009e73", "#7c3aed",') == 1
    assert "#d55e00" not in page[: page.index("var STYLE_BASE")]
    assert 'e.addClass("br-" + (order.indexOf(key) % BRANCH_COLORS.length))' in page
    # ...and a meta-edge takes none of it: the ramp is concatenated BETWEEN the
    # base styles and the tail, and the meta-edge rule is in the tail
    assert "style: STYLE_BASE.concat(branchStyles(), STYLE_TAIL)" in page
    assert page.index('selector: "edge.any"') < page.index("var STYLE_TAIL")
    assert page.index("var STYLE_TAIL") < page.index('"target-arrow-fill": "filled"')
    # the panel: three text rows and the tree
    for row in ('["condition", d.condition, "code"]', '["box_success", d.box_success, "code"]'):
        assert row in page
    assert "function renderCondTrees(n, order)" in page
    assert '"(lock, no arrow)"' in page and '"(not on canvas)"' in page
    # Enter with exactly one hit selects the node and opens its details
    assert "if (hits.length === 1 && shown.nonempty()) {" in page
    # the find trio is disabled until the load path's last layout stops
    # (DL-196's second review, G6/G7), and finishInitial enables exactly it
    assert '<input id="search" type="search" disabled' in page
    assert 'id="find-select" disabled' in page and 'id="find-highlight" disabled' in page
    assert '["search", "find-select", "find-highlight"].forEach(function (id) {' in page
    assert page.index('["search", "find-select", "find-highlight"]') > page.index(
        "function finishInitial() {"
    )
    # ...kind-aware (DL-196): a hub match opens the lock panel
    assert (
        'if (hits.hasClass("lock")) showLockDetails(hits[0]); else showNodeDetails(hits[0]);'
        in page
    )
    # the leaf highlight answers the keyboard as well as the pointer
    assert 'button.addEventListener("focus", mark);' in page
    assert 'button.addEventListener("blur", unmark);' in page
    # the edge panel names the attribute the edge came from and its branch
    assert '["attribute", d.attr],' in page and '["branch", d.branch],' in page


def test_to_explore_html_summary_counts_the_locks_it_draws() -> None:
    # DL-192: hubs + stated pairs + self badges, and the resources nothing
    # consumes counted as a stated omission. `edges` stays the DEPENDENCY
    # count -- a lock link is not a dependency.
    catalog = catalog_of((CORPUS_DIR / "viz_locks.jil").read_text(encoding="utf-8"))
    page = to_explore_html(catalog, title="locks")
    # 3 resource hubs + 1 clique hub + 4 pairs + 1 self badge, beside the five
    # dependency edges lk_seed fans out
    assert "18 jobs \N{MIDDLE DOT} 5 edges \N{MIDDLE DOT} 1 boxes" in page
    assert "\N{MIDDLE DOT} 9 locks \N{MIDDLE DOT} 1 unused" in page
    # #stats reads the same arithmetic out of the payload; only "nodes" differs
    # from the header's "jobs", and on purpose -- it counts the EXT nodes too
    assert _page_payload(page)["totals"] == {
        "nodes": 18,  # this fixture has no EXT endpoint, so the two words agree
        "edges": 5,
        "boxes": 1,
        "locks": 9,
        "unused": 1,
    }
    # nothing unused -> nothing said (read the summary span: the vendored
    # bundle has the word "unused" in it somewhere, as it has most words)
    plain = to_explore_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    summary = re.search(r'<span class="summary">(.*?)</span>', plain)
    assert summary is not None
    assert (
        summary.group(1)
        == "1 jobs \N{MIDDLE DOT} 0 edges \N{MIDDLE DOT} 0 boxes \N{MIDDLE DOT} 0 locks"
    )


def test_to_explore_html_carries_the_lock_grammar() -> None:
    """DL-192, the page half. The browser block drives all of this for real;
    what is pinned here is the wiring a skipped browser run would leave
    unchecked -- the control, the style channels, the menu item -- and not
    the bodies of the functions behind them."""
    page = to_explore_html(catalog_of((CORPUS_DIR / "viz_locks.jil").read_text(encoding="utf-8")))
    # locks are outside every layout and placed on their members afterwards
    assert 'function flow(elements) { return elements.not(".lock"); }' in page
    assert "function placeLocks()" in page
    assert "function freeSpot(hub, start, strict, relaxed)" in page
    # at load, on arrange, on a fit-only hide op, and after a fold -- a fold
    # runs no layout now (DL-196), so its own handler places them
    assert "placeLocks();" in page
    assert "function afterFold(" in page
    assert "layoutBy: null," in page
    # a focused job keeps its own hubs as context, never a hub's other members
    assert 'keep.connectedEdges(".lock").connectedNodes().filter(".lock")' in page
    # the toggle, the tee, the octagon and the menu item
    assert 'id="locks"' in page and '".lockoff"' in page
    assert '{ selector: "edge.lock[?source_tee]", style: {' in page
    assert '"target-arrow-shape": "tee"' in page
    assert 'shape: "octagon"' in page
    # DL-196 slice 3: a hub's menu offers its members and its own hiding
    assert '{ id: "lock-members", content: "select lock members", selector: "node.lock",' in page
    assert '{ id: "hide-lock", content: "hide this lock", selector: "node.lock",' in page
    assert "focus-lock" not in page
    assert "dotted gray = lock" in page
    # a threshold gate holds nothing, and both halves of the header count the
    # dependency graph by "edges"
    # the demand is worded in the emitter and printed by the page (DL-193)
    assert '["demand", d.demand]' in page
    assert '"demand": "threshold gate: needs 2 free, holds nothing"' in page
    assert "var TOTAL_EDGES = payload.totals.edges;" in page


def test_to_explore_html_collapse_threshold_none_marks_nothing() -> None:
    # None is the CLI's own default for --format explore (cli_compile.py):
    # the page must open on the whole graph, not the report's 12
    catalog = catalog_of(_NESTED_BOX_TEXT)
    page = to_explore_html(catalog, collapse_threshold=None)
    assert '"collapsed"' not in page


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
        [
            "viz",
            "--format",
            "explore",
            "--out",
            str(target),
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
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


def test_cli_viz_explore_refuses_only_the_undeliverable_flags() -> None:
    # DL-75's rule applied to what the page actually does: elkLayout runs
    # with fit:true -- it scales its layout to the viewport, which is the very
    # thing --fixed-scale asks an emitter to stop doing. That one is refused,
    # with the reason. (--collapse-threshold left this list at DL-190: the
    # page folds the over-threshold boxes before its first layout.)
    result = runner.invoke(
        app,
        ["viz", "--format", "explore", "--fixed-scale", str(CORPUS_DIR / "sem10_box_basic.jil")],
    )
    assert result.exit_code == 2
    assert "--fixed-scale cannot shape --format explore" in result.stderr
    assert "shape Mermaid charts" not in result.stderr  # say what, and why
    assert "--format html" in result.stderr


def test_cli_viz_explore_collapse_threshold_marks_the_boxes_that_start_collapsed(
    tmp_path: Path,
) -> None:
    # DL-190: the report's rule -- a top-level box with MORE direct members
    # than the threshold folds -- marks the node the page collapses before
    # its first layout. Without the flag nothing is marked: the page opens
    # on the whole graph, as DL-71 built it (the report's default 12 is not
    # borrowed).
    jil = _solo_jil(tmp_path)  # box_a holds one member
    marked = runner.invoke(
        app, ["viz", "--format", "explore", "--collapse-threshold", "0", str(jil)]
    )
    assert marked.exit_code == 0, marked.stderr
    assert marked.stderr == ""
    by_id = {n["data"]["id"]: n["data"] for n in _page_elements(marked.stdout)["nodes"]}  # type: ignore[index]
    assert by_id["box_a"].get("collapsed") is True
    assert "collapsed" not in by_id["job_a"] and "collapsed" not in by_id["solo"]

    at_threshold = runner.invoke(
        app, ["viz", "--format", "explore", "--collapse-threshold", "1", str(jil)]
    )
    assert at_threshold.exit_code == 0
    assert not any(
        "collapsed" in n["data"]
        for n in _page_elements(at_threshold.stdout)["nodes"]  # type: ignore[index]
    )  # one member is not MORE than one

    plain = runner.invoke(app, ["viz", "--format", "explore", str(jil)])
    assert plain.exit_code == 0
    assert not any("collapsed" in n["data"] for n in _page_elements(plain.stdout)["nodes"])  # type: ignore[index]


def _solo_jil(tmp_path: Path) -> Path:
    """A box with one member plus a standalone job -- the singleton the
    Mermaid report would drop without --include-singletons."""
    path = tmp_path / "solo.jil"
    path.write_text(
        "insert_job: box_a\njob_type: b\n\n"
        "insert_job: job_a\nbox_name: box_a\njob_type: c\n"
        "command: sleep 1\nmachine: machine1\n\n"
        "insert_job: solo\njob_type: c\ncommand: sleep 2\nmachine: machine1\n",
        encoding="utf-8",
    )
    return path


def test_cli_viz_explore_accepts_the_flags_the_canvas_already_delivers(tmp_path: Path) -> None:
    # DL-75: refuse only what the format cannot deliver. The page always lays
    # out with ELK and always carries every standalone job, so --elk and
    # --include-singletons name effects the operator is getting anyway --
    # each is accepted silently and the page still renders.
    jil = _solo_jil(tmp_path)
    for argv in (["--elk"], ["--include-singletons"], ["--elk", "--include-singletons"]):
        result = runner.invoke(app, ["viz", "--format", "explore", *argv, str(jil)])
        assert result.exit_code == 0, (argv, result.stderr)
        assert result.stderr == ""
        assert result.stdout.startswith("<!doctype html>")
        assert 'name: "elk"' in result.stdout  # --elk: the layout it asked for, always on
        nodes = _page_elements(result.stdout)["nodes"]
        names = sorted(n["data"]["id"] for n in nodes)  # type: ignore[index]
        assert names == ["box_a", "job_a", "solo"]  # --include-singletons: present anyway


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
    assert len(page_nodes) == 3  # every node is emitted; the page folds, not the emitter
