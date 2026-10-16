"""Self-contained HTML pages of the derived graph (DL-70).

`to_html` is the whole report and `to_html_chart` the whole graph as one chart (DL-76). Both
format viz's ReportContent, so nothing the Markdown report says goes missing here. The page
renders its Mermaid charts in the browser with vendored mermaid and ELK (src/dsl41/_vendor/),
offline. Uniform scale and ELK layout are set in mermaid.initialize, the only place the
secure-listed maxEdges and maxTextSize limits can be raised; per-chart frontmatter drops
secure keys.

- Chart sources embed as one JSON <script> with every "<" escaped to \\u003c; the page
  drives mermaid.render() itself for progress and per-chart error isolation.
- Vendor payloads are inlined verbatim; tests pin their integrity (no </script substring,
  attribution banner).
- Template substitution is single-pass with unique markers (`substitute`), not str.format
  or string.Template, because the template is {}-heavy and the vendor JS is full of "$".
  Replaced content is never re-scanned, so a job or file named like a marker cannot trigger
  a second substitution.
- Table cells follow viz's content policy: full text for assumptions, a 60-char ellipsis
  for command/path cells (viz.truncate_cell); html.escape here, pipe-escaping there.
- Table cells mirror viz's content policy: full text for assumptions,
  60-char ellipsis for command/path cells (viz.truncate_cell, shared with
  _code_cell); escaping is html.escape here, pipe-escaping there.
"""

from __future__ import annotations

import html
import json
import re
from importlib.resources import files
from typing import Literal

from dsl41.derive import DerivedGraph, derive_graph
from dsl41.ir import CatalogIR
from dsl41.viz import (
    APPENDIX_A_HEADERS,
    APPENDIX_B_HEADERS,
    DEFAULT_COLLAPSE_THRESHOLD,
    LEGEND_CHART,
    LEGEND_PROSE,
    LOCKS_PROSE,
    OR_SHAPE_HEADERS,
    REDESIGN_FLAG_HEADERS,
    Direction,
    report_content,
    to_mermaid,
    truncate_cell,
)


def substitute(template: str, mapping: dict[str, str]) -> str:
    """Replace every __DSL41_*__ marker in one pass over the template.

    Replacement text is never re-scanned, so data containing a marker cannot splice a later
    payload into itself (DL-70, DL-71)."""
    pattern = re.compile("|".join(re.escape(marker) for marker in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], template)


def _text(raw: str | None) -> str:
    """Table/heading text: newlines flatten (as in viz._cell), HTML-escaped."""
    if raw is None:
        return ""
    return html.escape(raw.replace("\n", " "))


def _code(raw: str | None) -> str:
    """Command/path cell: a <code> span cut by viz.truncate_cell."""
    if not raw:
        return ""
    flat = truncate_cell(raw.replace("\n", " "))
    return f"<code>{html.escape(flat)}</code>"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Rows carry pre-formatted cell HTML (callers pick _text/_code per column)."""
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "\n".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody>\n</table>"


def _viewport(chart_id: str) -> str:
    # chartbox anchors the zoom toolbar: the viewport scrolls, so a child would pan away
    return (
        '<div class="chartbox">'
        '<div class="zoombar" hidden>'
        '<button type="button" data-zoom="in" title="zoom in">+</button>'
        '<button type="button" data-zoom="out" title="zoom out">\N{MINUS SIGN}</button>'
        '<button type="button" data-zoom="reset" title="reset zoom">1:1</button>'
        "</div>"
        f'<div class="viewport"><div id="{chart_id}" class="chart">'
        f'<p class="pending">chart pending&hellip;</p></div></div>'
        "</div>"
    )


def _page(
    *,
    title: str,
    summary: str,
    charts: list[dict[str, str]],
    toc: list[str],
    sections: list[str],
    tables: list[str],
) -> str:
    """The shell both pages share (DL-76): header, legend chart, chart JSON, vendor payloads.

    It keeps the two emitters two functions, not one with a mode flag (DL-75)."""
    toc_html = '<nav class="toc"><ol>\n' + "\n".join(toc) + "\n</ol></nav>" if toc else ""
    payload = json.dumps({"charts": charts}).replace("<", "\\u003c")

    package = files("dsl41")
    template = (package / "templates" / "viz_report.html").read_text(encoding="utf-8")
    return substitute(
        template,
        {
            "__DSL41_TITLE__": _text(title),
            "__DSL41_SUMMARY__": _text(summary),
            "__DSL41_LEGEND_PROSE__": _text(LEGEND_PROSE.rstrip("\n")),
            "__DSL41_TOC__": toc_html,
            "__DSL41_SECTIONS__": "\n".join(sections),
            "__DSL41_TABLES__": "\n".join(tables),
            "__DSL41_CHART_JSON__": payload,
            "__DSL41_MERMAID_JS__": (package / "_vendor" / "mermaid.min.js").read_text(
                encoding="utf-8"
            ),
            "__DSL41_ELK_JS__": (package / "_vendor" / "mermaid-layout-elk.iife.min.js").read_text(
                encoding="utf-8"
            ),
        },
    )


def to_html_chart(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    title: str = "catalog",
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction | Literal["auto"] = "auto",
) -> str:
    """The whole graph as one chart in the offline page (DL-70, DL-76).

    No toc and no appendices, but the legend stays: an HTML page is a terminal artifact."""
    if graph is None:
        graph = derive_graph(catalog)
    body = to_mermaid(
        catalog,
        graph,
        collapse_threshold=collapse_threshold,
        direction="LR" if direction == "auto" else direction,
    )
    return _page(
        title=title,
        summary=f"{len(graph.nodes)} jobs \N{MIDDLE DOT} {len(graph.edges)} edges",
        charts=[
            {"el": "c0", "src": LEGEND_CHART.rstrip("\n")},
            {"el": "c1", "src": body.rstrip("\n")},
        ],
        toc=[],
        sections=[f'<section id="whole">\n{_viewport("c1")}\n</section>'],
        tables=[],
    )


def to_html(
    catalog: CatalogIR,
    graph: DerivedGraph | None = None,
    *,
    title: str = "catalog",
    collapse_threshold: int = DEFAULT_COLLAPSE_THRESHOLD,
    direction: Direction | Literal["auto"] = "auto",
    include_singletons: bool = False,
) -> str:
    """One self-contained HTML page with report parity (summary, legend, charts, appendices)."""
    if graph is None:
        graph = derive_graph(catalog)
    charts: list[dict[str, str]] = [{"el": "c0", "src": LEGEND_CHART.rstrip("\n")}]
    toc: list[str] = []
    sections: list[str] = []
    tables: list[str] = []

    content = report_content(
        catalog,
        graph,
        collapse_threshold=collapse_threshold,
        direction=direction,
        include_singletons=include_singletons,
    )
    summary = content.summary

    for i, sec in enumerate(content.sections, start=1):
        chart_id = f"c{i}"
        charts.append({"el": chart_id, "src": sec.chart.rstrip("\n")})
        heading = f"{sec.wid} \N{EM DASH} {sec.comp_title} ({sec.count_label})"
        toc.append(f'<li><a href="#w{i}">{_text(heading)}</a></li>')
        prefix_note = (
            f"<p>All names share the prefix <code>{html.escape(sec.prefix)}</code>"
            " (stripped in the chart).</p>"
            if sec.prefix
            else ""
        )
        sections.append(
            f'<section id="w{i}">\n<details open>\n'
            f'<summary><span class="h2">{_text(heading)}</span></summary>\n'
            f"{prefix_note}{_viewport(chart_id)}\n</details>\n</section>"
        )

    if content.standalone_chart is not None:
        chart_id = f"c{len(content.sections) + 1}"
        charts.append({"el": chart_id, "src": content.standalone_chart.rstrip("\n")})
        toc.append('<li><a href="#standalone">Standalone jobs</a></li>')
        sections.append(
            '<section id="standalone">\n<details open>\n'
            f'<summary><span class="h2">Standalone jobs ({len(content.standalone_rows)})'
            f"</span></summary>\n{_viewport(chart_id)}\n</details>\n</section>"
        )

    if content.lock_rows:
        toc.append('<li><a href="#locks">Locks</a></li>')
        rows = [
            [_text(joined), kind, _text(charts_col)]
            for joined, kind, charts_col in content.lock_rows
        ]
        tables.append(
            f'<section id="locks">\n<h2>Locks</h2>\n<p>{_text(LOCKS_PROSE)}</p>\n'
            f"{_table(['lock', 'kind', 'charts'], rows)}\n</section>"
        )

    toc.append('<li><a href="#appendix-a">Appendix A</a></li>')
    heading_a = "Appendix A \N{EM DASH} standalone jobs (not part of any workflow)"
    if content.standalone_rows:
        meta_rows = [
            [_text(name), _text(kind), _text(schedule), _code(detail)]
            for name, kind, schedule, detail in content.standalone_rows
        ]
        body_a = _table(list(APPENDIX_A_HEADERS), meta_rows)
    else:
        body_a = "<p>None.</p>"
    tables.append(f'<section id="appendix-a">\n<h2>{_text(heading_a)}</h2>\n{body_a}\n</section>')

    toc.append('<li><a href="#appendix-b">Appendix B</a></li>')
    heading_b = "Appendix B \N{EM DASH} edge annotations"
    if content.annotated_rows:
        edge_rows = [
            [
                _text(src),
                _text(dst),
                via,
                _text(lookback),
                cls,
                _text(mapping_row),
                _text(assumption),
            ]
            for src, dst, via, lookback, cls, mapping_row, assumption in content.annotated_rows
        ]
        body_b = _table(list(APPENDIX_B_HEADERS), edge_rows)
    else:
        body_b = "<p>None \N{EM DASH} every edge maps exactly.</p>"
    tables.append(f'<section id="appendix-b">\n<h2>{_text(heading_b)}</h2>\n{body_b}\n</section>')

    toc.append('<li><a href="#appendix-c">Appendix C</a></li>')
    heading_c = "Appendix C \N{EM DASH} redesign flags, OR shapes, cycles"
    parts_c: list[str] = []
    if content.redesign_flag_rows:
        flag_rows = [
            [_text(job), _text(mapping_row), _text(reason)]
            for job, mapping_row, reason in content.redesign_flag_rows
        ]
        parts_c.append("<h3>Redesign flags</h3>\n" + _table(list(REDESIGN_FLAG_HEADERS), flag_rows))
    if content.or_shape_rows:
        shape_rows = [
            [_text(job), _text(attr), _text(kind), _text(lowering)]
            for job, attr, kind, lowering in content.or_shape_rows
        ]
        parts_c.append("<h3>OR shapes (M12)</h3>\n" + _table(list(OR_SHAPE_HEADERS), shape_rows))
    if content.cycle_rows:
        items = "\n".join(f"<li>{_text(cycle)}</li>" for cycle in content.cycle_rows)
        parts_c.append(f"<h3>Cycles (L010)</h3>\n<ul>\n{items}\n</ul>")
    body_c = "\n".join(parts_c) if parts_c else "<p>None.</p>"
    tables.append(f'<section id="appendix-c">\n<h2>{_text(heading_c)}</h2>\n{body_c}\n</section>')

    return _page(
        title=title,
        summary=summary,
        charts=charts,
        toc=toc,
        sections=sections,
        tables=tables,
    )
