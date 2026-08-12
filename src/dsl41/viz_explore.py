"""Interactive exploration page of the derived graph (DL-71).

A navigation LENS over the whole graph, not the artifact of record: the
Markdown/HTML report keeps the appendices and stays the no-silent-loss
carrier. What the page must still honor is the report's content policy for
what it does show -- every edge annotation is reachable via the details
panel, and assumptions are never truncated.

The emitter goes straight from (catalog, graph) to cytoscape.js elements
JSON -- no Mermaid text anywhere. The page lays out with ELK (cytoscape-elk
driving elk.bundled.js on the main thread: no Worker, no fetch, offline like
DL-70's report page) and re-layouts the visible subset on focus.

Emission decisions (each with a test):
- Nodes carry id/label plus kind/schedule/detail read off IR-F through
  viz's display-facts helpers, and `parent` from box_tree.parent -- boxes
  become cytoscape compound nodes.
- Edge endpoints outside the catalog (undefined producers, externals
  "name^INST", global variable names) synthesize EXT nodes, class `ext`
  plus `global` when some referencing edge has via=="global".
- Edges carry via/lookback/cls/mapping_row/assumption; cls doubles as the
  style class (exact/assumed/redesign); the canvas label reuses
  viz.edge_label, so the DL-35 thinning grammar is re-expressed, not forked.
- The elements JSON embeds with DL-70's rule: every "<" becomes \\u003c
  (valid JSON, neutralizes </script and <!-- in one rule).
- Template substitution is single-pass unique-marker (viz_html.substitute):
  replaced content is never re-scanned, so marker-shaped job/file names
  cannot splice a later payload into the page.
"""

from __future__ import annotations

import html
import json
from importlib.resources import files
from typing import Literal

from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR
from dsl41.viz import Direction, edge_label, job_detail, job_kind, job_schedule
from dsl41.viz_html import substitute

_KIND_CLASS = {"BOX": "box", "FW": "fw"}  # anything else renders as a command


def _elements(catalog: CatalogIR, graph: DerivedGraph) -> dict[str, list[dict[str, object]]]:
    """Cytoscape elements for the whole graph. Pure function; deterministic
    for identical input (catalog nodes in source order, EXT nodes in
    first-reference order, edges in derivation order)."""
    nodes: list[dict[str, object]] = []
    for name in graph.nodes:
        job = catalog.jobs.get(name)
        kind = job_kind(job) or "CMD"
        data: dict[str, object] = {
            "id": name,
            "label": name,
            "kind": kind,
            "schedule": job_schedule(job),
            "detail": job_detail(job),
        }
        parent = graph.box_tree.parent.get(name)
        if parent is not None:
            data["parent"] = parent
        nodes.append({"data": data, "classes": _KIND_CLASS.get(kind, "cmd")})

    charted = set(graph.nodes)
    ext_order: list[str] = []
    ext_global: dict[str, bool] = {}
    for edge in graph.edges:
        for endpoint in (edge.src, edge.dst):
            if endpoint in charted:
                continue
            if endpoint not in ext_global:
                ext_order.append(endpoint)
                ext_global[endpoint] = False
            if endpoint == edge.src and edge.via == "global":
                ext_global[endpoint] = True
    for name in ext_order:
        nodes.append(
            {
                "data": {
                    "id": name,
                    "label": name,
                    "kind": "EXT",
                    "schedule": None,
                    "detail": None,
                },
                "classes": "ext global" if ext_global[name] else "ext",
            }
        )

    # cytoscape ids are unique across ALL elements and node ids are raw job
    # names, so a job literally named "e0" would silently swallow an edge at
    # cytoscape init (review finding) -- prefix until the id is free; distinct
    # tails keep prefixed ids distinct from each other.
    node_ids = {node["data"]["id"] for node in nodes}  # type: ignore[index]

    def edge_id(i: int) -> str:
        candidate = f"e{i}"
        while candidate in node_ids:
            candidate = "_" + candidate
        return candidate

    edges: list[dict[str, object]] = []
    for i, edge in enumerate(graph.edges):
        edges.append(
            {
                "data": {
                    "id": edge_id(i),
                    "source": edge.src,
                    "target": edge.dst,
                    "via": edge.via,
                    "lookback": edge.lookback.raw if edge.lookback is not None else None,
                    "cls": edge.cls,
                    "mapping_row": edge.mapping_row,
                    "assumption": edge.assumption,
                    "label": edge_label(edge),
                },
                "classes": edge.cls,
            }
        )
    return {"nodes": nodes, "edges": edges}


def to_explore_html(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    title: str = "catalog",
    direction: Direction | Literal["auto"] = "auto",
) -> str:
    """One self-contained offline HTML page: the whole graph, always ELK,
    always natural scale, boxes never collapse, singletons always present
    (search must find them). --direction maps auto/LR -> RIGHT, TD -> DOWN."""
    if graph is None:
        graph = derive_graph(catalog)
    elements = _elements(catalog, graph)
    boxes = sum(1 for n in elements["nodes"] if n["classes"] == "box")
    summary = (
        f"{len(graph.nodes)} jobs \N{MIDDLE DOT} {len(elements['edges'])} edges"
        f" \N{MIDDLE DOT} {boxes} boxes"
    )
    payload = json.dumps(elements).replace("<", "\\u003c")

    package = files("dsl41")
    template = (package / "templates" / "viz_explore.html").read_text(encoding="utf-8")
    return substitute(
        template,
        {
            "__DSL41_TITLE__": html.escape(title),
            "__DSL41_SUMMARY__": html.escape(summary),
            "__DSL41_ELK_DIRECTION__": "DOWN" if direction == "TD" else "RIGHT",
            "__DSL41_ELEMENTS_JSON__": payload,
            "__DSL41_CYTOSCAPE_JS__": (
                package / "_vendor" / "cytoscape-explore.iife.min.js"
            ).read_text(encoding="utf-8"),
        },
    )
