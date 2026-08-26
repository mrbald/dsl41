"""Self-contained HTML pages of the derived graph (DL-70): to_html is the
whole report, to_html_chart the whole graph as one chart (DL-76).

Same content as viz.to_markdown -- both emitters format one ReportContent,
so nothing the Markdown report says can go missing here (no-silent-loss).
The page renders its Mermaid charts in the browser with vendored mermaid +
ELK (src/dsl41/_vendor/), fully offline: file:// works, no network is
touched. Uniform scale (flowchart.useMaxWidth=false) and ELK layout are page
defaults set via mermaid.initialize, which is also the only place the
secure-listed maxEdges/maxTextSize limits can be raised past bank-estate
size (per-chart frontmatter cannot -- mermaid drops secure keys there).

Rendering decisions (each with a test):
- Chart sources are embedded as one JSON <script> with every "<" escaped to
  \\u003c (valid JSON, neutralizes </script and <!-- in one rule); the page
  drives mermaid.render() itself for progress + per-chart error isolation.
- Vendor payloads are inlined verbatim; their integrity invariants (no
  </script substring, attribution banner) are pinned in tests.
- Template substitution is unique-marker single-pass (substitute), not
  str.format or string.Template: the template is {}-heavy JS/CSS and the
  vendor JS is full of "$". Single-pass, not chained .replace(): replaced
  content is never re-scanned, so marker-shaped user input (a job or file
  named __DSL41_..._JS__) cannot trigger a second substitution.
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
    DEFAULT_COLLAPSE_THRESHOLD,
    LEGEND_CHART,
    LEGEND_PROSE,
    LOCKS_PROSE,
    Direction,
    job_detail,
    job_kind,
    job_schedule,
    report_content,
    to_mermaid,
    truncate_cell,
)


def substitute(template: str, mapping: dict[str, str]) -> str:
    """Replace every __DSL41_*__ marker in one pass over the template.
    Replacement text is never re-scanned, so data that happens to contain a
    marker (job names and file names allow the marker charset) cannot splice
    a later payload into itself (review finding on DL-70/71)."""
    pattern = re.compile("|".join(re.escape(marker) for marker in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], template)


def _text(raw: str | None) -> str:
    """Table/heading text: newlines flatten (as in viz._cell), HTML-escaped."""
    if raw is None:
        return ""
    return html.escape(raw.replace("\n", " "))


def _code(raw: str | None) -> str:
    """Command/path cell: <code> span, 60-char ellipsis (viz.truncate_cell:
    full text is IR-F's responsibility, not the report's)."""
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
    # chartbox anchors the zoom toolbar: the viewport scrolls, so anything
    # positioned inside it would pan away with the chart
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
    """The shell both pages share (DL-76): header, legend chart, chart JSON,
    vendor payloads. Splitting it out is what lets the two emitters below be
    two functions rather than one with a mode flag -- the shape DL-75 is
    about."""
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
    """The whole graph as ONE chart in the same offline page (DL-70(4),
    restored by DL-76 as --format html-chart). No toc and no appendices --
    a single chart has nothing to index -- but the legend stays: an HTML
    page is a terminal artifact, unlike the pipeable bare chart."""
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
    """One self-contained HTML page with report parity: summary, legend,
    charts, locks, appendices."""
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
            f'<summary><span class="h2">Standalone jobs ({len(content.standalone)})'
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
    if content.standalone:
        meta_rows = []
        for name in content.standalone:
            job = catalog.jobs.get(name)
            meta_rows.append(
                [
                    _text(name),
                    _text(job_kind(job)),
                    _text(job_schedule(job)),
                    _code(job_detail(job)),
                ]
            )
        body_a = _table(["job", "kind", "schedule", "command / watched file"], meta_rows)
    else:
        body_a = "<p>None.</p>"
    tables.append(f'<section id="appendix-a">\n<h2>{_text(heading_a)}</h2>\n{body_a}\n</section>')

    toc.append('<li><a href="#appendix-b">Appendix B</a></li>')
    heading_b = "Appendix B \N{EM DASH} edge annotations"
    if content.annotated:
        edge_rows = [
            [
                _text(e.src),
                _text(e.dst),
                e.via,
                _text(e.lookback.raw if e.lookback is not None else ""),
                e.cls,
                _text(e.mapping_row),
                _text(e.assumption),
            ]
            for e in content.annotated
        ]
        body_b = _table(
            ["producer", "consumer", "via", "lookback", "class", "row", "assumption"],
            edge_rows,
        )
    else:
        body_b = "<p>None \N{EM DASH} every edge maps exactly.</p>"
    tables.append(f'<section id="appendix-b">\n<h2>{_text(heading_b)}</h2>\n{body_b}\n</section>')

    toc.append('<li><a href="#appendix-c">Appendix C</a></li>')
    heading_c = "Appendix C \N{EM DASH} redesign flags, OR shapes, cycles"
    parts_c: list[str] = []
    if graph.redesign_flags:
        flag_rows = [
            [_text(f.job), _text(f.mapping_row), _text(f.reason)] for f in graph.redesign_flags
        ]
        parts_c.append("<h3>Redesign flags</h3>\n" + _table(["job", "row", "reason"], flag_rows))
    if graph.or_shapes:
        shape_rows = [
            [_text(s.job), _text(s.attr), _text(s.kind), _text(s.lowering)] for s in graph.or_shapes
        ]
        parts_c.append(
            "<h3>OR shapes (M12)</h3>\n"
            + _table(["job", "attr", "kind", "suggested lowering"], shape_rows)
        )
    if graph.cycles:
        items = "\n".join(
            f"<li>{_text(' \N{RIGHTWARDS ARROW} '.join(cycle))}</li>" for cycle in graph.cycles
        )
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
