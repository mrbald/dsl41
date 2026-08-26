"""Self-contained HTML report (dsl41 viz --format html) and its vendored assets.

Validity strategy mirrors test_viz.py: no browser in the toolchain, so the
page is pinned structurally -- chart parity with the Markdown report, the
JSON embedding's escaping invariant, vendor payloads present and inline-safe,
page defaults that only `mermaid.initialize` can set (maxEdges/maxTextSize
are secure-listed). What only a browser can verify (the ELK render itself,
pan/zoom, per-chart error isolation) is recorded in DL-70, not tested here.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path

from test_viz import (
    CORPUS_DIR,
    _assert_ids_are_generated,
    _assert_subgraphs_balance,
    _mermaid_fences,
    catalog_of,
    corpus_catalog,
    runner,
)

from dsl41.cli import app
from dsl41.viz import LEGEND_PROSE, to_markdown, to_mermaid
from dsl41.viz_html import to_html, to_html_chart

# mermaid.min.js is copied byte-exact from the npm tarball, so a full pin is
# stable; the esbuild-built elk bundle is not byte-reproducible across esbuild
# versions, so it gets a size floor + invariants instead (scripts/
# vendor_mermaid.sh regenerates both).
_MERMAID_SHA256 = "18327bef70d96fb505fe7287d9f6a7362ebf07ff6576ddfaffb1a06f3e1a2954"


def _vendor_bytes(name: str) -> bytes:
    return (files("dsl41") / "_vendor" / name).read_bytes()


# ------------------------------------------------------------ vendor integrity


def test_vendored_mermaid_is_the_pinned_npm_payload() -> None:
    payload = _vendor_bytes("mermaid.min.js")
    assert hashlib.sha256(payload).hexdigest() == _MERMAID_SHA256
    assert len(payload) > 3_000_000
    assert b"</script" not in payload  # inline-safety: embedded without escaping
    assert b"/*!" in payload  # bundled third-party license banners survived


def test_vendored_elk_bundle_is_inline_safe_and_attributed() -> None:
    payload = _vendor_bytes("mermaid-layout-elk.iife.min.js")
    assert len(payload) > 1_000_000
    assert b"</script" not in payload  # inline-safety: embedded without escaping
    assert payload.startswith(b"/*!")  # attribution banner from vendor_mermaid.sh
    assert b"EPL-2.0" in payload[:400]
    assert b"var elkLayouts" in payload[:600]  # the IIFE global the page JS expects


# ------------------------------------------------------------------- the page

_CHART_DATA = re.compile(r'<script id="chart-data" type="application/json">(.*?)</script>', re.S)


def _charts(page: str) -> list[dict[str, str]]:
    raw = _CHART_DATA.search(page)
    assert raw is not None
    assert "<" not in raw.group(1)  # the escaping invariant the embedding rests on
    result: list[dict[str, str]] = json.loads(raw.group(1))["charts"]
    return result


def test_to_html_chart_count_matches_markdown_fences() -> None:
    catalog = corpus_catalog()
    page_charts = _charts(to_html(catalog))
    fences = _mermaid_fences(to_markdown(catalog))
    assert len(page_charts) == len(fences)  # both count legend + one per workflow


def test_to_html_escapes_the_json_but_round_trips_the_chart() -> None:
    # a scheduled job's label line-breaks with <br/>: the raw script block
    # must not contain "<", yet the decoded chart must get the tag back
    text = 'insert_job: nightly\njob_type: c\ncommand: x\nmachine: m1\nstart_times: "03:00"\n'
    page = to_html(catalog_of(text), include_singletons=True)
    decoded = "\n".join(c["src"] for c in _charts(page))
    assert "<br/>" in decoded


def test_to_html_chart_bodies_are_structurally_valid() -> None:
    for chart in _charts(to_html(corpus_catalog())):
        assert not chart["src"].startswith("---")  # page config, not frontmatter
        _assert_subgraphs_balance(chart["src"])
        if chart["el"] != "c0":  # the hand-written legend has its own ids
            _assert_ids_are_generated(chart["src"])


def test_to_html_embeds_each_vendor_payload_exactly_once() -> None:
    page = to_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    for name in ("mermaid.min.js", "mermaid-layout-elk.iife.min.js"):
        probe = _vendor_bytes(name).decode("utf-8")[:200]
        assert page.count(probe) == 1


def test_to_html_survives_marker_shaped_job_and_title() -> None:
    # a legal job name (or title) containing a substitution marker must not
    # splice the mermaid payload into the chart JSON (review finding:
    # single-pass substitution, replaced content never re-scanned)
    text = "insert_job: EVIL__DSL41_MERMAID_JS__X\njob_type: c\ncommand: x\nmachine: m1\n"
    page = to_html(catalog_of(text), title="x__DSL41_CHART_JSON__.jil", include_singletons=True)
    for name in ("mermaid.min.js", "mermaid-layout-elk.iife.min.js"):
        probe = _vendor_bytes(name).decode("utf-8")[:200]
        assert page.count(probe) == 1
    assert "EVIL__DSL41_MERMAID_JS__X" in "\n".join(c["src"] for c in _charts(page))


def test_to_html_pins_page_defaults() -> None:
    # maxEdges/maxTextSize are secure-listed: only initialize can raise them,
    # and the bank estate exceeds the 500-edge/50k-char defaults
    page = to_html(catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    assert 'layout: "elk"' in page
    assert "useMaxWidth: false" in page
    assert "maxEdges: 10000" in page
    assert "startOnLoad: false" in page
    assert 'id="progress"' in page
    # one zoom toolbar per viewport, hidden until its chart renders
    assert page.count('class="zoombar" hidden') == page.count('class="viewport"')


def test_to_html_appendix_parity_with_markdown() -> None:
    catalog = corpus_catalog()
    page = to_html(catalog)
    md = to_markdown(catalog)
    # Appendix B rows: one <tr> per markdown table row (same annotated edges)
    md_b_rows = re.search(r"## Appendix B.*?(?=\n## )", md, re.S).group(0).count("\n|") - 2
    html_b = re.search(r'<section id="appendix-b">.*?</section>', page, re.S).group(0)
    assert html_b.count("<tr>") - 1 == md_b_rows  # -1: header row
    # Locks section appears in both or neither
    assert ("## Locks" in md) == ('<section id="locks">' in page)
    # standalone jobs of Appendix A all appear escaped in the page
    assert ("| job |" in md) == ("<th>job</th>" in page)


def test_to_html_appendix_cells_are_escaped_and_ellipsised() -> None:
    long_cmd = "/bin/echo " + "&x" * 40  # standalone, >60 chars, needs escaping
    text = f"insert_job: solo\njob_type: c\ncommand: {long_cmd}\nmachine: m1\n"
    page = to_html(catalog_of(text))
    cell = re.search(r"<code>(.*?)</code>", page.split('id="appendix-a"')[1]).group(1)
    assert cell.endswith("\N{HORIZONTAL ELLIPSIS}")
    assert "&amp;x" in cell
    assert len(cell.replace("&amp;", "&")) == 60


def test_to_html_include_singletons_adds_a_chart() -> None:
    text = "insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"
    without = _charts(to_html(catalog_of(text)))
    with_ = _charts(to_html(catalog_of(text), include_singletons=True))
    assert len(with_) == len(without) + 1
    assert '<section id="standalone">' in to_html(catalog_of(text), include_singletons=True)


def test_to_html_is_deterministic() -> None:
    catalog = corpus_catalog()
    assert to_html(catalog) == to_html(catalog)


# ------------------------------------------------------------ the single-chart page


def test_to_html_chart_is_one_whole_graph_chart_beside_the_legend() -> None:
    # DL-70(4), restored by DL-76: the page holds the legend chart and the
    # whole graph, and nothing the report's structure needs (toc, sections
    # per workflow, appendices) -- one chart has nothing to index.
    catalog = corpus_catalog()
    page = to_html_chart(catalog)
    assert [c["el"] for c in _charts(page)] == ["c0", "c1"]
    assert '<section id="whole">' in page
    assert '<nav class="toc">' not in page
    assert '<section id="appendix-a">' not in page
    # the legend survives: an HTML page is a terminal artifact (DL-70(4))
    assert '<details class="legend">' in page
    assert LEGEND_PROSE.split("\n")[0] in page


def test_to_html_chart_embeds_the_bare_chart_verbatim() -> None:
    # same chart --format chart pipes out, minus the frontmatter the page
    # config replaces (layout/useMaxWidth come from mermaid.initialize)
    catalog = corpus_catalog()
    body = _charts(to_html_chart(catalog))[1]["src"]
    assert body == to_mermaid(catalog, direction="LR").rstrip("\n")
    assert not body.startswith("---")


def test_to_html_chart_summary_counts_the_whole_graph() -> None:
    catalog = catalog_of("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n")
    assert "1 jobs \N{MIDDLE DOT} 0 edges" in to_html_chart(catalog)


def test_to_html_chart_collapse_threshold_reaches_the_chart() -> None:
    catalog = catalog_of((CORPUS_DIR / "sem10_box_basic.jil").read_text(encoding="utf-8"))
    page = to_html_chart(catalog, collapse_threshold=1)
    assert "box_a (2 members)" in _charts(page)[1]["src"]


def test_to_html_chart_is_deterministic() -> None:
    catalog = corpus_catalog()
    assert to_html_chart(catalog) == to_html_chart(catalog)


# --------------------------------------------------------------------------- CLI


def test_cli_viz_names_the_format_that_replaced_the_flag_pair() -> None:
    # DL-75 deleted DL-70(4)'s single-chart page on a miscount of the old
    # surface; DL-76 restored it as --format html-chart. The combination
    # still gets its own refusal line, now naming that one format instead
    # of two that emit something else.
    result = runner.invoke(
        app, ["viz", "--html", "--whole-graph", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 2
    assert (
        "--html --whole-graph (the single-chart offline page, DL-70) was replaced"
        " by --format html-chart" in result.stderr
    )
    assert "--format explore" not in result.stderr


def test_cli_viz_html_writes_out_file(tmp_path: Path) -> None:
    target = tmp_path / "report.html"
    result = runner.invoke(
        app,
        ["viz", "--format", "html", "--out", str(target), str(CORPUS_DIR / "sem10_box_basic.jil")],
    )
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert target.stat().st_size > 4_000_000  # the vendor payloads really embedded
    assert "wrote" in result.stdout


def test_cli_viz_html_stdout() -> None:
    result = runner.invoke(
        app, ["viz", "--format", "html", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 0
    assert result.stdout.startswith("<!doctype html>")


def test_cli_viz_html_stays_silent_on_elk_and_fixed_scale() -> None:
    # DL-75: the page already lays out with ELK at natural scale, so both
    # flags' asked-for effect happens -- silence, not a refusal
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "html",
            "--elk",
            "--fixed-scale",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    assert result.stderr == ""
    assert (
        result.stdout
        == runner.invoke(
            app, ["viz", "--format", "html", str(CORPUS_DIR / "sem10_box_basic.jil")]
        ).stdout
    )


def test_cli_viz_html_shaping_flags_reach_the_charts() -> None:
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "html",
            "--collapse-threshold",
            "1",
            "--direction",
            "TD",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    assert "box_a (2 members)" in result.stdout  # threshold 1 collapsed the box
    assert "flowchart TD" in result.stdout


def test_cli_viz_html_chart_writes_out_file(tmp_path: Path) -> None:
    target = tmp_path / "chart.html"
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "html-chart",
            "--out",
            str(target),
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    page = target.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert '<section id="whole">' in page  # the single chart...
    assert '<details class="legend">' in page  # ...and the legend beside it (DL-70(4))
    assert target.stat().st_size > 4_000_000  # the vendor payloads really embedded
    assert "wrote" in result.stdout


def test_cli_viz_html_chart_stdout_is_the_single_chart_page() -> None:
    result = runner.invoke(
        app, ["viz", "--format", "html-chart", str(CORPUS_DIR / "sem10_box_basic.jil")]
    )
    assert result.exit_code == 0
    assert result.stdout.startswith("<!doctype html>")
    assert 'id="chart-data"' in result.stdout  # the DL-70 page...
    assert '<section id="whole">' in result.stdout  # ...with one chart, no appendices
    assert '<section id="appendix-a">' not in result.stdout


def test_cli_viz_html_chart_accepts_every_shaping_flag(tmp_path: Path) -> None:
    # DL-76: refuse only what the format cannot deliver, and this one
    # delivers all five. --elk/--fixed-scale are page defaults (as under
    # --format html), --collapse-threshold and --direction reach to_mermaid,
    # and its whole-graph chart carries every standalone job already -- so
    # --include-singletons asks for what is there. No refusal, no exit 2.
    jil = tmp_path / "solo.jil"
    jil.write_text(
        "insert_job: box_a\njob_type: b\n\n"
        "insert_job: job_a\nbox_name: box_a\njob_type: c\n"
        "command: sleep 1\nmachine: machine1\n\n"
        "insert_job: solo\njob_type: c\ncommand: sleep 2\nmachine: machine1\n",
        encoding="utf-8",
    )
    plain = runner.invoke(app, ["viz", "--format", "html-chart", str(jil)])
    assert plain.exit_code == 0
    assert '"solo"' in _charts(plain.stdout)[1]["src"]  # --include-singletons: there anyway
    for argv in (["--elk"], ["--fixed-scale"], ["--include-singletons"]):
        result = runner.invoke(app, ["viz", "--format", "html-chart", *argv, str(jil)])
        assert result.exit_code == 0, (argv, result.stderr)
        assert result.stderr == ""
        assert result.stdout == plain.stdout  # accepted, and the page is unchanged


def test_cli_viz_html_chart_shaping_flags_reach_the_chart() -> None:
    result = runner.invoke(
        app,
        [
            "viz",
            "--format",
            "html-chart",
            "--collapse-threshold",
            "1",
            "--direction",
            "TD",
            str(CORPUS_DIR / "sem10_box_basic.jil"),
        ],
    )
    assert result.exit_code == 0
    assert "box_a (2 members)" in result.stdout  # threshold 1 collapsed the box
    assert "flowchart TD" in result.stdout
