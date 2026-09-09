"""Cross-engine smoke test: the viz --format explore page RUNNING in a browser.

Every other viz test asserts on emitted bytes. None of them ever ran the page,
which is exactly how DL-77 shipped: cytoscape-context-menus registers CUSTOMIZED
BUILT-IN elements, WebKit does not implement them, cy.contextMenus threw, and
everything below the throw -- the initial ELK layout, the toolbar, the search --
never ran. A byte assertion cannot see that; only a real engine can. So this file
drives one emitted page in chromium, webkit and firefox and exercises precisely
the controls that defect killed, with the engine in every test id.

It is a smoke test, not a rendering test: it asks whether each control is wired
and does its job, never how the picture looks.

Opt-in, and skipped -- never failed -- when it is not: driving three engines
costs well under two minutes, and the browsers are a separate ~200MB install,
so a plain `pytest -q` must neither slow down nor start needing them. DSL41_BROWSER_TESTS=1 turns it on; .github/workflows/ci.yml's explore-page
job sets it, which is where these run on every push (playwright itself is a
dev-only dependency -- the package keeps its three runtime deps).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

if os.environ.get("DSL41_BROWSER_TESTS") != "1":  # pragma: no cover
    pytest.skip(
        "browser smoke tests are opt-in: set DSL41_BROWSER_TESTS=1 (CI's explore-page job does)",
        allow_module_level=True,
    )

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright is dev-only (pyproject [dev]); `uv sync --extra dev` installs it",
)

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright
from test_viz import corpus_catalog
from test_viz import CORPUS_DIR
from test_viz_explore import _COND_TEXT, _NESTED_BOX_TEXT

from dsl41.ir import lower_source
from dsl41.viz_explore import to_explore_html

ENGINES = ("chromium", "webkit", "firefox")

#: ELK on the corpus graph (81 nodes) is sub-second everywhere; the ceiling is
#: for a loaded CI runner, and its expiry is the DL-77 signature (layout never
#: runs at all), so it must not be mistaken for slowness.
_LAYOUT_TIMEOUT_MS = 60_000
#: one focus re-layout + its fit animation
_SETTLE_MS = 900
_CLICK_TIMEOUT_MS = 15_000

#: the page's own readiness signal: #stats starts at "laying out&hellip;" and is
#: rewritten by the layoutstop handler.
_LAID_OUT = (
    "() => { const s = document.getElementById('stats');"
    " return !!s && !s.textContent.includes('laying out'); }"
)


@dataclass
class Driven:
    """One engine's live page, plus every uncaught error it has thrown."""

    engine: str
    page: Any
    errors: list[str] = field(default_factory=list)
    #: set once the layout is known never to arrive, so the remaining tests fail
    #: with the same diagnosis instead of each waiting out the timeout again
    dead: str = ""


@pytest.fixture(scope="module")
def page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One page, emitted from the synthetic corpus, shared by all engines."""
    path = tmp_path_factory.mktemp("explore") / "explore.html"
    path.write_text(to_explore_html(corpus_catalog(), title="corpus"), encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def _playwright() -> Any:
    """One Playwright driver for the whole module. `driven`, `driven_trace`
    and `driven_folded` are all module-scoped and independently parametrized,
    so pytest keeps more than one alive at once (a later test in the file
    can still need an earlier fixture's engine) -- a second, nested
    `sync_playwright()` while an outer one is still open raises "Sync API
    inside the asyncio loop", not a graph assertion failure, so the fixtures
    below share this one instance and each launches (and closes) its own
    browser."""
    with sync_playwright() as pw:
        yield pw


def _open_driven(pw: Any, engine: str, url: str) -> Any:
    """`driven`, `driven_trace` and `driven_folded`'s shared body: launch one
    engine from the module's Playwright instance and load one page into it."""
    try:
        browser = getattr(pw, engine).launch()
    except PlaywrightError as exc:  # pragma: no cover -- environment, not logic
        if "Executable doesn't exist" not in str(exc):
            raise
        pytest.skip(f"the {engine} binary is absent; `playwright install {engine}`")
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    d = Driven(engine=engine, page=page)
    page.on("pageerror", lambda err: d.errors.append(str(err)))
    page.goto(url)
    try:
        yield d
    finally:
        browser.close()


@pytest.fixture(scope="module", params=ENGINES)
def driven(request: pytest.FixtureRequest, page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, page_url)


@pytest.fixture(scope="module")
def trace_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """DL-190: boxes nested two deep (B holds M, N, IB; IB holds IM), a
    box-gated consumer (C, on s(B)) and a member-gated one (D, on s(M)) --
    the shape the fan-in/fan-out-through-boxes rules and box collapse need,
    which the corpus page (no boxes nested past one level) cannot exercise.
    `_NESTED_BOX_TEXT` is test_viz_explore.py's own fixture, unmodified."""
    catalog = lower_source(_NESTED_BOX_TEXT)
    path = tmp_path_factory.mktemp("explore-trace") / "trace.html"
    path.write_text(to_explore_html(catalog, title="trace"), encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def folded_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Same catalog, `--collapse-threshold 1`: B has 3 direct members and
    folds before the first layout; IB has 1 (not MORE than 1) and is nested
    besides, so it never folds on its own."""
    catalog = lower_source(_NESTED_BOX_TEXT)
    path = tmp_path_factory.mktemp("explore-folded") / "folded.html"
    path.write_text(
        to_explore_html(catalog, title="folded", collapse_threshold=1), encoding="utf-8"
    )
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def driven_trace(request: pytest.FixtureRequest, trace_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, trace_page_url)


@pytest.fixture(scope="module", params=ENGINES)
def driven_folded(request: pytest.FixtureRequest, folded_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, folded_page_url)


# --------------------------------------------------------------------- helpers

# Every page.evaluate here returns plain data or nothing. Returning a cytoscape
# object (`cy.zoom(5)` and `.emit()` both hand back one for chaining) makes
# playwright serialize the whole graph -- chromium is merely slow at it, webkit
# never finishes, and it reads as a hung test rather than an error.


def _ready(d: Driven) -> None:
    """Wait for the initial ELK layout. Called by every test: after the first it
    returns at once, and when it does not, the failure names the DL-77 shape."""
    if d.dead:
        pytest.fail(d.dead)
    try:
        d.page.wait_for_function(_LAID_OUT, timeout=_LAYOUT_TIMEOUT_MS)
    except PlaywrightTimeout:
        d.dead = (
            f"{d.engine}: the initial ELK layout never completed -- #stats is still "
            f"{d.page.inner_text('#stats')!r}. Uncaught page errors: {d.errors or 'none'}"
        )
        pytest.fail(d.dead)


def _click(d: Driven, selector: str) -> None:
    """force=True: the actionability wait (headless firefox times out on the
    toolbar buttons) proves nothing here -- the buttons are static, and what is
    under test is whether the click reaches a listener at all."""
    d.page.locator(selector).click(timeout=_CLICK_TIMEOUT_MS, force=True)


def _show_all(d: Driven) -> None:
    """Reset the shared page between tests through its own control."""
    _click(d, "#show-all")
    d.page.wait_for_timeout(_SETTLE_MS)


def _counts(d: Driven) -> dict[str, int]:
    return dict(
        d.page.evaluate(
            "() => ({nodes: cy.nodes().length, edges: cy.edges().length,"
            " visible_nodes: cy.nodes(':visible').length,"
            " visible_edges: cy.edges(':visible').length,"
            " hits: cy.nodes('.hit').length})"
        )
    )


def _sample_node(d: Driven) -> str:
    """A leaf job with both fan-in and fan-out, so focusing on part of its
    neighbourhood is always a proper narrowing."""
    node: str = d.page.evaluate(
        "() => { const n = cy.nodes().filter(n => n.isChildless()"
        " && n.incomers('node').length && n.outgoers('node').length)[0]"
        " || cy.nodes().filter(n => n.isChildless())[0]; return n.id(); }"
    )
    return node


def _client_point(d: Driven, node_id: str) -> dict[str, float]:
    """Where node_id sits on screen, for a real mouse event."""
    point: dict[str, float] = d.page.evaluate(
        "(id) => { const p = cy.$id(id).renderedPosition();"
        " const r = document.getElementById('cy').getBoundingClientRect();"
        " return {x: r.left + p.x, y: r.top + p.y}; }",
        node_id,
    )
    return point


def _visible_ids(d: Driven) -> list[str]:
    ids: list[str] = d.page.evaluate("() => cy.nodes(':visible').map(n => n.id())")
    return sorted(ids)


def _tree_ids(d: Driven, node_id: str, tree: str) -> list[str]:
    """The DL-190 focus-item tree: `tree(cy.$id(node_id)).map(id).sort()`, ids
    only -- `tree` is a page-global tree function's own name (`fanInTree` or
    `fanOutTree`, the ones the menu's tree items call), a fixed set of
    literals this module controls, never test input, so splicing it into the
    expression is safe."""
    ids: list[str] = d.page.evaluate(f"() => {tree}(cy.$id('{node_id}')).map(n => n.id()).sort()")
    return ids


def _detail_rows(d: Driven) -> dict[str, str]:
    """#d-rows as a dict, keyed by the th label -- the panel omits empty
    rows (showDetails), so a key's absence is itself informative."""
    rows: list[list[str]] = d.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr')).map("
        "tr => [tr.querySelector('th').textContent, tr.querySelector('td').textContent])"
    )
    return dict(rows)


# ----------------------------------------------------------------------- tests


def test_initial_layout_completes_and_shows_the_whole_graph(driven: Driven) -> None:
    _ready(driven)
    counts = _counts(driven)
    assert counts["nodes"] > 1 and counts["edges"] > 1
    assert counts["visible_nodes"] == counts["nodes"]
    assert counts["visible_edges"] == counts["edges"]
    stats = driven.page.inner_text("#stats")
    assert "visible" in stats, f"{driven.engine}: {stats!r}"


def test_fit_button_rescales_the_view(driven: Driven) -> None:
    _ready(driven)
    driven.page.evaluate("() => { cy.zoom(5); }")
    _click(driven, "#fit")
    driven.page.wait_for_timeout(_SETTLE_MS)
    zoom = driven.page.evaluate("() => cy.zoom()")
    assert abs(zoom - 5) > 1e-6, f"{driven.engine}: #fit left the zoom at {zoom}"


def test_search_marks_hits_and_reports_no_match(driven: Driven) -> None:
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    driven.page.fill("#search", sample[: max(6, len(sample) // 2)])
    driven.page.press("#search", "Enter")
    driven.page.wait_for_timeout(_SETTLE_MS)
    stats = driven.page.inner_text("#stats")
    assert _counts(driven)["hits"] > 0, f"{driven.engine}: nothing marked, stats={stats!r}"
    assert "hit" in stats, f"{driven.engine}: {stats!r}"

    driven.page.fill("#search", "zzz-no-such-job-zzz")
    driven.page.press("#search", "Enter")
    driven.page.wait_for_timeout(_SETTLE_MS)
    assert "no match" in driven.page.inner_text("#stats")

    driven.page.fill("#search", "")
    driven.page.press("#search", "Enter")


def test_search_unhides_a_node_hidden_by_a_focus(driven: Driven) -> None:
    """The template's stated contract: a search that cannot find a hidden node
    is a lying search."""
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    driven.page.evaluate(
        "(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample
    )
    driven.page.wait_for_timeout(_SETTLE_MS)
    hidden = driven.page.evaluate(
        "() => { const h = cy.nodes().filter(n => n.isChildless() && !n.visible())[0];"
        " return h ? h.id() : null; }"
    )
    assert hidden, f"{driven.engine}: the focus hid nothing"
    driven.page.fill("#search", hidden)
    driven.page.press("#search", "Enter")
    driven.page.wait_for_timeout(_SETTLE_MS)
    assert driven.page.evaluate("(id) => cy.$id(id).visible()", hidden), (
        f"{driven.engine}: {hidden} stayed hidden after searching for it"
    )
    driven.page.fill("#search", "")
    driven.page.press("#search", "Enter")


def test_show_all_restores_every_element_and_clears_highlights(driven: Driven) -> None:
    _ready(driven)
    sample = _sample_node(driven)
    driven.page.evaluate(
        "(id) => { const n = cy.$id(id); n.union(n.descendants()).addClass('hidden'); updateStats(); }",
        sample,
    )
    driven.page.wait_for_timeout(200)
    assert not driven.page.evaluate("(id) => cy.$id(id).visible()", sample)
    _show_all(driven)
    counts = _counts(driven)
    assert counts["visible_nodes"] == counts["nodes"], f"{driven.engine}: {counts}"
    assert counts["visible_edges"] == counts["edges"], f"{driven.engine}: {counts}"
    assert counts["hits"] == 0


def test_relayout_toggle_off_leaves_leaf_positions_alone(driven: Driven) -> None:
    """With the toggle off a focus only fits. Compound boxes are excluded on
    purpose: a box's position is its children's bounding box, so hiding members
    legitimately moves it -- only leaves can be pinned."""
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    driven.page.uncheck("#relayout")
    leaves = (
        "() => Object.fromEntries(cy.nodes().filter(n => n.isChildless())"
        ".map(n => [n.id(), [n.position('x'), n.position('y')]]))"
    )
    before = driven.page.evaluate(leaves)
    driven.page.evaluate(
        "(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample
    )
    driven.page.wait_for_timeout(_SETTLE_MS)
    after = driven.page.evaluate(leaves)
    moved = [
        k
        for k, v in before.items()
        if abs(v[0] - after[k][0]) > 0.5 or abs(v[1] - after[k][1]) > 0.5
    ]
    assert not moved, (
        f"{driven.engine}: {len(moved)} leaf node(s) moved with re-layout off: {moved[:5]}"
    )
    driven.page.check("#relayout")
    _show_all(driven)


def test_details_panel_opens_for_a_node_and_for_an_edge(driven: Driven) -> None:
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    driven.page.evaluate("(id) => { cy.$id(id).emit('tap'); }", sample)
    driven.page.wait_for_timeout(200)
    assert driven.page.get_attribute("#details", "hidden") is None, (
        f"{driven.engine}: panel stayed shut"
    )
    assert driven.page.inner_text("#d-title") == sample
    assert driven.page.evaluate("() => document.querySelectorAll('#d-rows tr').length") > 0

    _click(driven, "#d-close")
    assert driven.page.get_attribute("#details", "hidden") is not None

    edge = driven.page.evaluate(
        "() => { const e = cy.edges()[0]; e.emit('tap');"
        " return {source: e.data('source'), target: e.data('target')}; }"
    )
    driven.page.wait_for_timeout(200)
    title = driven.page.inner_text("#d-title")
    assert edge["source"] in title and edge["target"] in title, f"{driven.engine}: {title!r}"
    assert driven.page.evaluate("() => document.querySelectorAll('#d-rows tr').length") > 0
    _click(driven, "#d-close")


def test_context_menu_opens_on_right_click_and_an_item_narrows_the_graph(driven: Driven) -> None:
    """The DL-77 defect itself: the menu is built from customized built-in
    elements, so this is the one control that needs a real mouse."""
    _ready(driven)
    _show_all(driven)
    assert "context menu unavailable" not in driven.page.inner_text("#stats"), (
        f"{driven.engine}: the page reports its own context menu as lost"
    )
    sample = _sample_node(driven)
    before = _counts(driven)["visible_nodes"]
    point = _client_point(driven, sample)
    driven.page.mouse.click(point["x"], point["y"], button="right")
    driven.page.wait_for_timeout(500)
    items = driven.page.evaluate(
        "() => Array.from(document.querySelectorAll('#fan-in,#fan-out,#fan-in-tree,#fan-out-tree,"
        "#both-trees,#neighbours,#hide,#menu-show-all,#menu-fit')).map(e => e.id)"
    )
    assert len(items) == 9, f"{driven.engine}: menu items present = {items}"
    assert driven.page.evaluate(
        "() => { const e = document.querySelector('.cy-context-menus-cxt-menu');"
        " return !!e && getComputedStyle(e).display !== 'none'; }"
    ), f"{driven.engine}: right-click did not open the menu"

    _click(driven, "#fan-in-tree")
    driven.page.wait_for_timeout(_SETTLE_MS)
    after = _counts(driven)["visible_nodes"]
    assert 0 < after < before, (
        f"{driven.engine}: fan-in tree left {after} of {before} nodes visible"
    )
    assert driven.page.evaluate("(id) => cy.$id(id).visible()", sample)
    _show_all(driven)


def test_no_uncaught_page_errors(driven: Driven) -> None:
    """Last: every error the whole session threw. A control that "works" while
    throwing is not working -- and DL-77's throw was silent."""
    _ready(driven)
    assert driven.errors == [], f"{driven.engine}: {driven.errors}"


# ---------------------------------------------------- DL-190: collapse and trace
#
# `driven_trace` shares one `trace_page_url` load per engine across every test
# below that takes it, module-scoped and run in file order (pyproject.toml
# carries no pytest-randomly or similar, and there is no conftest.py to add
# one). Several of these tests build on the graph state the previous one left, the
# same way the corpus tests above lean on `_show_all` -- documented at each
# such test, and each mutating test either restores the base state itself or
# hands off to a test that expects exactly what it left behind.


def test_canvas_layers_include_the_expand_collapse_cue(driven_trace: Driven) -> None:
    _ready(driven_trace)
    # cytoscape's own three canvas layers, plus expand-collapse's corner-cue
    # overlay -- its absence is this extension failing to attach at all
    assert driven_trace.page.evaluate("() => document.querySelectorAll('#cy canvas').length") == 4


def test_fan_in_tree_of_m_traces_through_its_box(driven_trace: Driven) -> None:
    _ready(driven_trace)
    assert _tree_ids(driven_trace, "M", "fanInTree") == ["B", "M", "P", "Q"]


def test_fan_out_tree_of_m_traces_through_its_box(driven_trace: Driven) -> None:
    _ready(driven_trace)
    assert _tree_ids(driven_trace, "M", "fanOutTree") == ["C", "D", "M"]


def test_fan_out_tree_of_b_reaches_every_member_and_release(driven_trace: Driven) -> None:
    _ready(driven_trace)
    assert _tree_ids(driven_trace, "B", "fanOutTree") == ["B", "C", "D", "IB", "IM", "M", "N"]


def test_fan_in_tree_of_im_reaches_both_enclosing_boxes(driven_trace: Driven) -> None:
    _ready(driven_trace)
    assert _tree_ids(driven_trace, "IM", "fanInTree") == ["B", "IB", "IM", "P"]


def test_trace_boxes_off_drops_box_gating_from_fan_in_and_fan_out(driven_trace: Driven) -> None:
    """The toggle read directly (`viaBoxes()` reads `.checked` live, no change
    event needed) -- restored to checked after, in a `finally`, so a failed
    assertion here cannot leave every later test in this block silently
    computing edges-only trees instead of the through-boxes ones they claim."""
    _ready(driven_trace)
    driven_trace.page.evaluate("() => { document.getElementById('trace-boxes').checked = false; }")
    try:
        assert _tree_ids(driven_trace, "M", "fanInTree") == ["M", "Q"]
        assert _tree_ids(driven_trace, "B", "fanOutTree") == ["B", "C"]
    finally:
        driven_trace.page.evaluate(
            "() => { document.getElementById('trace-boxes').checked = true; }"
        )


def test_context_menu_lists_the_box_items_in_declared_order(driven_trace: Driven) -> None:
    """`collapse`/`expand` sit only where their selector matches the clicked
    node (a plain box shows `collapse`, not `expand`), but the DOM holds
    every configured item regardless -- non-matching ones are `display:
    none`, not absent -- so querying all thirteen ids finds them all, in the
    order menuItems.splice/.push builds them."""
    _ready(driven_trace)
    point = _client_point(driven_trace, "B")
    driven_trace.page.mouse.click(point["x"], point["y"], button="right")
    driven_trace.page.wait_for_timeout(500)
    try:
        items = driven_trace.page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'#fan-in,#fan-out,#fan-in-tree,#fan-out-tree,#both-trees,#neighbours,#hide,"
            "#collapse,#expand,#menu-show-all,#menu-fit,#menu-collapse-all,#menu-expand-all'"
            ")).map(e => e.id)"
        )
        assert items == [
            "fan-in",
            "fan-out",
            "fan-in-tree",
            "fan-out-tree",
            "both-trees",
            "neighbours",
            "hide",
            "collapse",
            "expand",
            "menu-show-all",
            "menu-fit",
            "menu-collapse-all",
            "menu-expand-all",
        ], driven_trace.engine
    finally:
        driven_trace.page.mouse.click(10, 10)  # dismiss: an outside click, nothing under test
        driven_trace.page.wait_for_timeout(200)


def test_dbltap_collapses_box_b_and_folds_its_border_edges(driven_trace: Driven) -> None:
    """Leaves B collapsed: the next two tests continue from this state."""
    _ready(driven_trace)
    driven_trace.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)

    assert _visible_ids(driven_trace) == ["B", "C", "D", "P", "Q"], driven_trace.engine
    meta_ends = sorted(
        driven_trace.page.evaluate(
            "() => cy.edges('.cy-expand-collapse-meta-edge')"
            ".map(e => e.source().id() + '>' + e.target().id())"
        )
    )
    assert meta_ends == ["B>D", "Q>B"], driven_trace.engine
    assert driven_trace.page.evaluate("() => nodeLabel(cy.$id('B'))") == "B (4)", (
        driven_trace.engine
    )
    stats = driven_trace.page.inner_text("#stats")
    assert "visible 5 / 9 nodes · 4 / 4 edges · 1 box collapsed" in stats, (
        driven_trace.engine,
        stats,
    )
    assert _tree_ids(driven_trace, "C", "fanInTree") == ["B", "C", "P", "Q"]


def test_meta_edge_and_collapsed_box_show_their_own_details_rows(driven_trace: Driven) -> None:
    """Continues from the dbltap test above -- B is still collapsed. Tapping
    only opens the panel, so this leaves the collapse untouched for the
    search test after it."""
    _ready(driven_trace)
    stats = driven_trace.page.inner_text("#stats")
    assert "1 box collapsed" in stats, (
        f"{driven_trace.engine}: expected B still collapsed from the prior test, got {stats!r}"
    )

    driven_trace.page.evaluate(
        "() => { cy.edges('.cy-expand-collapse-meta-edge')"
        ".filter(e => e.source().id() === 'Q')[0].emit('tap'); }"
    )
    driven_trace.page.wait_for_timeout(200)
    rows = _detail_rows(driven_trace)
    assert rows["stands for"] == "Q → M (inside a collapsed box)", (driven_trace.engine, rows)
    assert rows["via"] == "success", (driven_trace.engine, rows)
    _click(driven_trace, "#d-close")

    driven_trace.page.evaluate("() => { cy.$id('B').emit('tap'); }")
    driven_trace.page.wait_for_timeout(200)
    rows = _detail_rows(driven_trace)
    assert rows["members"] == "4 (collapsed)", (driven_trace.engine, rows)
    _click(driven_trace, "#d-close")


def test_search_expands_a_collapsed_box_to_find_the_hit(driven_trace: Driven) -> None:
    """Continues from the collapsed B left above -- asserted, not just
    assumed, so a reordering fails here with a clear diagnosis instead of
    passing vacuously (a search on an already-expanded page would also find
    IM). Restores the page to fully expanded and the search box empty -- the
    base state the remaining tests in this block assume. Also re-fits the
    view: the search's own `cy.fit(hits, 60)` zooms tight around the one
    hit, which can leave other nodes' rendered position outside the fixed
    1600x1000 viewport -- fine for the id-only assertions elsewhere, but the
    next tests click real screen points and need everything back on screen."""
    _ready(driven_trace)
    stats = driven_trace.page.inner_text("#stats")
    assert "1 box collapsed" in stats, (
        f"{driven_trace.engine}: expected B still collapsed from the dbltap test, got {stats!r}"
    )
    driven_trace.page.fill("#search", "IM")
    driven_trace.page.press("#search", "Enter")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == [
        "B",
        "C",
        "D",
        "IB",
        "IM",
        "M",
        "N",
        "P",
        "Q",
    ], driven_trace.engine
    assert driven_trace.page.inner_text("#stats").endswith("1 hit"), (
        driven_trace.engine,
        driven_trace.page.inner_text("#stats"),
    )
    driven_trace.page.fill("#search", "")
    driven_trace.page.press("#search", "Enter")
    _show_all(driven_trace)


def test_context_menu_collapse_then_a_mouse_dblclick_expand(driven_trace: Driven) -> None:
    """Real controls, not page globals: a genuine right-click plus the
    `#collapse` menu item (the technique
    test_context_menu_opens_on_right_click_and_an_item_narrows_the_graph
    above uses for the DL-77 defect), then a genuine mouse double-click to
    expand -- `cy.$id('B').emit('dbltap')` above proves the toggle wires up,
    this proves a real double-click reaches it too. Starts and ends fully
    expanded."""
    _ready(driven_trace)
    point = _client_point(driven_trace, "B")
    driven_trace.page.mouse.click(point["x"], point["y"], button="right")
    driven_trace.page.wait_for_timeout(500)
    _click(driven_trace, "#collapse")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == ["B", "C", "D", "P", "Q"], driven_trace.engine

    point = _client_point(driven_trace, "B")  # collapse moved it; re-read
    driven_trace.page.mouse.dblclick(point["x"], point["y"])
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == [
        "B",
        "C",
        "D",
        "IB",
        "IM",
        "M",
        "N",
        "P",
        "Q",
    ], driven_trace.engine


def test_collapse_all_and_expand_all_toolbar_buttons(driven_trace: Driven) -> None:
    _ready(driven_trace)
    _click(driven_trace, "#collapse-all")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == ["B", "C", "D", "P", "Q"], driven_trace.engine

    _click(driven_trace, "#expand-all")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == [
        "B",
        "C",
        "D",
        "IB",
        "IM",
        "M",
        "N",
        "P",
        "Q",
    ], driven_trace.engine


def test_no_uncaught_page_errors_on_the_trace_page(driven_trace: Driven) -> None:
    _ready(driven_trace)
    assert driven_trace.errors == [], f"{driven_trace.engine}: {driven_trace.errors}"


def test_folded_page_starts_with_box_b_already_collapsed(driven_folded: Driven) -> None:
    """--collapse-threshold 1 (folded_page_url): B (3 direct members) folds;
    IB (1 member, and nested under B besides) does not fold on its own --
    the emitter-level rule test_elements_collapse_threshold_never_marks_a_
    nested_box in test_viz_explore.py pins. This pins the RESULT once the
    page is ready: that B really is collapsed, not merely marked, once the
    page's own initial-fold step has had a chance to run (the production
    code's docstring, not this test, is what claims the fold happens before
    the first layout)."""
    _ready(driven_folded)
    assert _visible_ids(driven_folded) == ["B", "C", "D", "P", "Q"], driven_folded.engine
    stats = driven_folded.page.inner_text("#stats")
    assert "visible 5 / 9 nodes · 4 / 4 edges · 1 box collapsed" in stats, (
        driven_folded.engine,
        stats,
    )


def test_no_uncaught_page_errors_on_the_folded_page(driven_folded: Driven) -> None:
    _ready(driven_folded)
    assert driven_folded.errors == [], f"{driven_folded.engine}: {driven_folded.errors}"


# ------------------------------------------- the box gate, overrides and the guard

_OVERRIDE_JIL = """insert_job: box_a
job_type: b
box_success: s(job_a)

insert_job: job_a
job_type: c
box_name: box_a
command: a
machine: m1

insert_job: job_b
job_type: c
box_name: box_a
command: b
machine: m1

insert_job: X
job_type: c
condition: s(box_a)
command: x
machine: m1
"""


@pytest.fixture(scope="module")
def override_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A box whose box_success names a member (an M15 edge job_a -> box_a),
    and an outside consumer of the box."""
    path = tmp_path_factory.mktemp("explore-override") / "override.html"
    path.write_text(to_explore_html(lower_source(_OVERRIDE_JIL), title="override"))
    return path.as_uri()


@pytest.fixture(scope="module")
def broken_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The trace page with the expand-collapse registration forced to throw --
    DL-77's own proof technique for a guard, applied to the DL-190 guard."""
    page = to_explore_html(lower_source(_NESTED_BOX_TEXT), title="broken")
    assert page.count("cy.expandCollapse({") == 1
    path = tmp_path_factory.mktemp("explore-broken") / "broken.html"
    path.write_text(page.replace("cy.expandCollapse({", "cy.expandCollapseMissing({"))
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def driven_override(
    request: pytest.FixtureRequest, override_page_url: str, _playwright: Any
) -> Any:
    yield from _open_driven(_playwright, request.param, override_page_url)


@pytest.fixture(scope="module", params=ENGINES)
def driven_broken(request: pytest.FixtureRequest, broken_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, broken_page_url)


def test_the_box_gate_leaves_a_box_override_out_of_a_members_fan_in(
    driven_override: Driven,
) -> None:
    """SEM-12: box_success names job_a, so job_a -> box_a is a completion
    predicate (M15), not a start gate. Through boxes, job_b's fan-in is its
    box and what gates the box -- nothing gates it here -- and never its
    sibling. A consumer of the box (X, on s(box_a)) waits for the box's
    COMPLETION, so for X the override producer is upstream."""
    _ready(driven_override)
    assert _tree_ids(driven_override, "job_b", "fanInTree") == ["box_a", "job_b"]
    assert _tree_ids(driven_override, "X", "fanInTree") == ["X", "box_a", "job_a"]


def test_fan_out_through_an_override_does_not_release_the_siblings(driven_override: Driven) -> None:
    """job_a's completion folds box_a (SEM-12) and so reaches X; it does not
    START box_a, so job_b is not downstream of job_a. Picking the box itself
    is its start, and every member is downstream of that."""
    _ready(driven_override)
    assert _tree_ids(driven_override, "job_a", "fanOutTree") == ["X", "box_a", "job_a"]
    assert _tree_ids(driven_override, "box_a", "fanOutTree") == ["X", "box_a", "job_a", "job_b"]


def test_search_into_a_folded_box_restores_members_a_layout_placed(driven_folded: Driven) -> None:
    """The review's blocker: a box folded before its members ever had a layout
    restores them on cytoscape's default grid. The first layout now runs over
    the whole graph before the emitter's folds, and a search that expands a
    box re-lays out, so the nine nodes come back at distinct positions (a box
    holding one member shares that member's centre, hence eight)."""
    d = driven_folded
    _ready(d)
    if not d.page.evaluate("() => ec !== null && ec.isExpandable(cy.$id('B'))"):
        d.page.evaluate("() => { ec.collapse(cy.$id('B'), { layoutBy: null }); }")
    assert d.page.evaluate("() => cy.$id('IM').length") == 0  # IM is inside the folded box
    d.page.fill("#search", "IM")
    d.page.press("#search", "Enter")
    d.page.wait_for_function(
        "() => cy.$id('IM').length === 1"
        " && document.getElementById('stats').textContent.includes('1 hit')",
        timeout=_LAYOUT_TIMEOUT_MS,
    )
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.nodes().length") == 9
    distinct = d.page.evaluate(
        "() => new Set(cy.nodes().map(n => Math.round(n.position('x')) + ','"
        " + Math.round(n.position('y')))).size"
    )
    assert distinct >= 8, distinct


def test_a_throwing_collapse_extension_costs_only_itself(driven_broken: Driven) -> None:
    """DL-77's rule, proven the way DL-77 proved it: with the registration
    forced to throw, the layout still completes, #stats names the loss, the
    two buttons that need the extension go inert, the menu carries no box
    items, and search still works. No uncaught error escapes the guard."""
    d = driven_broken
    _ready(d)
    assert "box collapse unavailable in this browser" in d.page.inner_text("#stats")
    assert d.page.evaluate("() => ec === null")
    assert d.page.evaluate(
        "() => document.getElementById('collapse-all').disabled"
        " && document.getElementById('expand-all').disabled"
    )
    menu_ids = d.page.evaluate(
        "() => Array.from(document.querySelectorAll('.cy-context-menus-cxt-menuitem'))"
        ".map(e => e.id)"
    )
    assert "fan-in-tree" in menu_ids
    assert not {"collapse", "expand", "menu-collapse-all", "menu-expand-all"} & set(menu_ids)
    d.page.fill("#search", "IM")
    d.page.press("#search", "Enter")
    d.page.wait_for_function(
        "() => document.getElementById('stats').textContent.includes('1 hit')",
        timeout=_CLICK_TIMEOUT_MS,
    )
    assert d.errors == [], d.errors


# ------------------------------------------- DL-191: condition visibility
#
# `driven_cond` shares one `cond_page_url` load per engine across the block,
# module-scoped and run in file order, like the DL-190 block above. Only the
# collapse test mutates the graph, and it expands again before it returns.

_TREE = "#d-tree"


@pytest.fixture(scope="module")
def cond_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Every condition shape the page has a rule for, plus a box whose own
    condition is an OR and a member whose branched arrow folds into a
    meta-edge. `_COND_TEXT` is test_viz_explore.py's own fixture, unmodified
    -- the emitter tests pin the JSON it produces, these drive the page."""
    path = tmp_path_factory.mktemp("explore-cond") / "cond.html"
    path.write_text(to_explore_html(lower_source(_COND_TEXT), title="conditions"))
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def driven_cond(request: pytest.FixtureRequest, cond_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, cond_page_url)


def _label(d: Driven, node_id: str) -> str:
    label: str = d.page.evaluate("(id) => nodeLabel(cy.$id(id))", node_id)
    return label


def _tap(d: Driven, node_id: str) -> None:
    d.page.evaluate("(id) => { cy.$id(id).emit('tap'); }", node_id)
    d.page.wait_for_timeout(200)


def _painted(d: Driven) -> list[str]:
    """Every arrow carrying a branch colour, as `source>target:class`."""
    painted: list[str] = d.page.evaluate(
        "() => cy.edges().filter(e => e.classes().some(c => c.indexOf('br-') === 0))"
        ".map(e => e.data('source') + '>' + e.data('target') + ':'"
        " + e.classes().filter(c => c.indexOf('br-') === 0).join(','))"
    )
    return sorted(painted)


def test_the_badge_marks_or_shapes_and_only_those(driven_cond: Driven) -> None:
    """The page's default reading is that every incoming arrow must hold, so
    only a departure from it is badged: an OR the canvas draws as branches
    gets the sign, a nesting too deep gets the starred form, a plain AND gets
    nothing."""
    _ready(driven_cond)
    labels = {name: _label(driven_cond, name) for name in ("AOA", "ANY", "CPX", "PLAIN", "BOX")}
    assert labels == {
        "AOA": "AOA \u2228",
        "ANY": "ANY \u2228",
        "CPX": "CPX \u2228*",
        "PLAIN": "PLAIN",
        "BOX": "BOX \u2228",
    }, driven_cond.engine


def test_branch_arrows_are_hollow_and_carry_their_branch_in_the_label(
    driven_cond: Driven,
) -> None:
    """The line-style channel stays the edge class's (exact/assumed/redesign);
    the arrowhead is the branch channel, and the label names the branch."""
    _ready(driven_cond)
    fills = driven_cond.page.evaluate(
        "() => ({any: Array.from(new Set(cy.edges('.any').map(e => e.style('target-arrow-fill')))),"
        " rest: Array.from(new Set(cy.edges().filter(e => !e.hasClass('any'))"
        ".map(e => e.style('target-arrow-fill'))))})"
    )
    assert fills == {"any": ["hollow"], "rest": ["filled"]}, driven_cond.engine
    labels = driven_cond.page.evaluate(
        "() => Object.fromEntries(cy.edges().filter(e => e.data('target') === 'TWO')"
        ".map(e => [e.data('source'), e.data('label')]))"
    )
    assert labels == {"A": "a|1", "B": "a|2", "C": "b|1", "D": "b|2", "E": ""}, driven_cond.engine


def test_details_panel_shows_the_condition_rows_and_the_tree(driven_cond: Driven) -> None:
    _ready(driven_cond)
    _tap(driven_cond, "AOA")
    rows = _detail_rows(driven_cond)
    assert rows["condition"] == "s(A) & (s(B) | f(C))", (driven_cond.engine, rows)
    tree = driven_cond.page.inner_text(_TREE)
    assert tree.split("\n") == ["condition", "all of:", "s(A)", "any of:", "s(B)", "f(C)"], (
        driven_cond.engine,
        tree,
    )
    # the two branches of the OR carry the two swatches the arrows carry
    swatches = driven_cond.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-tree .swatch')).map(s => s.title)"
    )
    assert swatches == ["branch |1", "branch |2"], driven_cond.engine


def test_a_lock_leaf_says_why_it_has_no_arrow(driven_cond: Driven) -> None:
    """M07: a bare local n() is a mutex record and never an edge, so it used
    to be invisible on this page entirely."""
    _ready(driven_cond)
    _tap(driven_cond, "LOCK")
    tree = driven_cond.page.inner_text(_TREE)
    assert tree.split("\n") == [
        "condition",
        "any of:",
        "n(A) (lock, no arrow)",
        "s(B)",
    ], (driven_cond.engine, tree)
    # it is still an alternative: it holds a branch, and only the arrow is missing
    assert (
        driven_cond.page.evaluate("() => document.querySelectorAll('#d-tree .swatch').length") == 2
    ), driven_cond.engine


def test_tapping_a_node_paints_its_branches_and_a_blank_tap_clears_them(
    driven_cond: Driven,
) -> None:
    _ready(driven_cond)
    _tap(driven_cond, "TWO")
    assert _painted(driven_cond) == [
        "A>TWO:br-0",
        "B>TWO:br-1",
        "C>TWO:br-2",
        "D>TWO:br-3",
    ], driven_cond.engine
    # the next node tap repaints for that node alone...
    _tap(driven_cond, "AOA")
    assert _painted(driven_cond) == ["B>AOA:br-0", "C>AOA:br-1"], driven_cond.engine
    # ...and a tap on the canvas clears the paint and shuts the panel
    driven_cond.page.evaluate("() => { cy.emit('tap'); }")
    driven_cond.page.wait_for_timeout(200)
    assert _painted(driven_cond) == [], driven_cond.engine
    assert driven_cond.page.get_attribute("#details", "hidden") is not None


def test_a_tree_leaf_highlights_its_arrow_on_hover_and_selects_it_on_click(
    driven_cond: Driven,
) -> None:
    """A real pointer, not an emitted event: the leaf is a DOM button and the
    arrow it names is a canvas element."""
    _ready(driven_cond)
    _tap(driven_cond, "AOA")
    leaves = driven_cond.page.locator("#d-tree button.leaf")
    assert leaves.count() == 3, driven_cond.engine
    leaves.nth(1).hover()
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate(
        "() => cy.edges('.leaf-hover').map(e => e.data('source') + '>' + e.data('target'))"
    ) == ["B>AOA"], driven_cond.engine
    driven_cond.page.mouse.move(5, 5)
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate("() => cy.edges('.leaf-hover').length") == 0
    leaves.nth(1).click()
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate(
        "() => cy.edges(':selected').map(e => e.data('source') + '>' + e.data('target'))"
    ) == ["B>AOA"], driven_cond.engine
    driven_cond.page.evaluate("() => { cy.elements().unselect(); cy.emit('tap'); }")


def test_a_leaf_whose_arrow_is_off_the_canvas_says_so(driven_cond: Driven) -> None:
    """A focus can hide the arrow a leaf names; the leaf then says so instead
    of offering a link to an element that is not drawn. Restores the page."""
    _ready(driven_cond)
    driven_cond.page.evaluate("() => { cy.$id('B').addClass('hidden'); }")
    driven_cond.page.wait_for_timeout(_SETTLE_MS)
    _tap(driven_cond, "AOA")
    tree = driven_cond.page.inner_text(_TREE)
    assert "s(B) (not on canvas)" in tree, (driven_cond.engine, tree)
    _show_all(driven_cond)
    _tap(driven_cond, "AOA")
    assert "(not on canvas)" not in driven_cond.page.inner_text(_TREE), driven_cond.engine


def test_search_enter_with_one_hit_selects_the_node_and_opens_its_details(
    driven_cond: Driven,
) -> None:
    """Keyboard alone answers "what gates this job?": one hit is unambiguous,
    so Enter selects it and opens the panel. Leaves the search box empty."""
    _ready(driven_cond)
    driven_cond.page.evaluate("() => { cy.emit('tap'); }")
    driven_cond.page.fill("#search", "AOFA")
    driven_cond.page.press("#search", "Enter")
    driven_cond.page.wait_for_timeout(_SETTLE_MS)
    assert driven_cond.page.get_attribute("#details", "hidden") is None, driven_cond.engine
    assert driven_cond.page.inner_text("#d-title") == "AOFA"
    assert driven_cond.page.evaluate("() => cy.nodes(':selected').map(n => n.id())") == ["AOFA"]
    assert _detail_rows(driven_cond)["condition"] == "(s(A) & s(B)) | s(C)"
    # ...and a search that hits several leaves the panel alone
    driven_cond.page.evaluate("() => { cy.emit('tap'); }")
    driven_cond.page.fill("#search", "ME")
    driven_cond.page.press("#search", "Enter")
    driven_cond.page.wait_for_timeout(_SETTLE_MS)
    assert driven_cond.page.get_attribute("#details", "hidden") is not None, driven_cond.engine
    driven_cond.page.fill("#search", "")
    driven_cond.page.press("#search", "Enter")
    _show_all(driven_cond)


def test_a_collapsed_box_keeps_its_own_badge_and_details_and_its_meta_edges_stay_neutral(
    driven_cond: Driven,
) -> None:
    """A collapsed box stands for its members, so it shows its OWN condition's
    badge and never merges theirs, and the meta-edges that stand for several
    arrows carry no branch label, no hollow head and no branch colour.
    Expands again before returning."""
    _ready(driven_cond)
    _tap(driven_cond, "MEM")  # paint MEM's branches first: the fold must clear them
    assert _painted(driven_cond) == ["C>MEM:br-0", "D>MEM:br-1"], driven_cond.engine
    driven_cond.page.evaluate("() => { cy.$id('BOX').emit('dbltap'); }")
    driven_cond.page.wait_for_timeout(_SETTLE_MS)

    assert _label(driven_cond, "BOX") == "BOX (2) \u2228", driven_cond.engine
    assert _painted(driven_cond) == [], driven_cond.engine
    meta = driven_cond.page.evaluate(
        "() => cy.edges('.cy-expand-collapse-meta-edge').map(e => [e.data('source')"
        " + '>' + e.data('target'), e.style('target-arrow-fill'), e.style('label')])"
    )
    assert meta, driven_cond.engine
    assert all(row[1] == "filled" and row[2] == "" for row in meta), (driven_cond.engine, meta)

    _tap(driven_cond, "BOX")
    rows = _detail_rows(driven_cond)
    assert rows["members"] == "2 (collapsed)" and rows["condition"] == "s(A) | s(B)", rows
    assert driven_cond.page.inner_text(_TREE).split("\n") == [
        "condition",
        "any of:",
        "s(A)",
        "s(B)",
    ], driven_cond.engine

    # a leaf whose arrow the fold re-pointed says where it went: the arrow IS
    # on the canvas, standing for several edges rather than for this one
    _tap(driven_cond, "OUT")
    assert "s(MEM) (folded: BOX \u2192 OUT)" in driven_cond.page.inner_text(_TREE), (
        driven_cond.engine,
        driven_cond.page.inner_text(_TREE),
    )

    driven_cond.page.evaluate("() => { cy.$id('BOX').emit('dbltap'); }")
    driven_cond.page.wait_for_timeout(_SETTLE_MS)
    assert _label(driven_cond, "BOX") == "BOX \u2228", driven_cond.engine
    _show_all(driven_cond)


def test_no_uncaught_page_errors_on_the_condition_page(driven_cond: Driven) -> None:
    _ready(driven_cond)
    assert driven_cond.errors == [], f"{driven_cond.engine}: {driven_cond.errors}"


# ------------------------------------------------------------- DL-192: locks
#
# `driven_locks` shares one page per engine across this block, like the two
# above. Only the last two tests mutate the graph, and each restores it.


@pytest.fixture(scope="module")
def locks_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """tests/corpus/viz_locks.jil: both lock kinds and every shape -- two
    resource semaphores (one shared, one a pool), a declared resource nobody
    consumes, a one-way pair, a mutual pair, a complete clique, a
    self-exclusion, and a box holding one member of each kind."""
    path = tmp_path_factory.mktemp("explore-locks") / "locks.html"
    catalog = lower_source((CORPUS_DIR / "viz_locks.jil").read_text(encoding="utf-8"))
    path.write_text(to_explore_html(catalog, title="locks"))
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def driven_locks(request: pytest.FixtureRequest, locks_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, locks_page_url)


def _hub_inside_members(d: Driven, hub_id: str) -> dict[str, Any]:
    """Where a hub sits against the bounding box of the members that are
    actually drawn."""
    result: dict[str, Any] = d.page.evaluate(
        """(id) => {
          const hub = cy.$id(id), p = hub.position();
          const ms = (hub.data('members') || []).map(m => cy.$id(m.id))
            .filter(n => n.nonempty() && n.visible());
          const xs = ms.map(n => n.position('x')), ys = ms.map(n => n.position('y'));
          return {
            members: ms.map(n => n.id()),
            inside: p.x >= Math.min(...xs) - 1 && p.x <= Math.max(...xs) + 1
                 && p.y >= Math.min(...ys) - 1 && p.y <= Math.max(...ys) + 1
          };
        }""",
        hub_id,
    )
    return result


def test_lock_hubs_sit_on_their_members(driven_locks: Driven) -> None:
    """A lock is a fact ABOUT jobs, so it is left out of the layout and put
    at the centroid of the members that are drawn -- inside their bounding
    box, by construction."""
    _ready(driven_locks)
    for hub in ("lock:r:R_ONE", "lock:r:R_BIG", "lock:m:lk_e+lk_f+lk_g"):
        placed = _hub_inside_members(driven_locks, hub)
        assert placed["members"], (driven_locks.engine, hub)
        assert placed["inside"], (driven_locks.engine, hub, placed)


def test_lock_links_are_dotted_and_the_tee_marks_the_waiter(driven_locks: Driven) -> None:
    _ready(driven_locks)
    styles = driven_locks.page.evaluate(
        "() => Object.fromEntries(cy.edges('.lock').map(e => ["
        "e.data('source') + '>' + e.data('target'),"
        " [e.style('line-style'), e.style('source-arrow-shape'), e.style('target-arrow-shape')]]))"
    )
    assert styles["lk_a>lk_b"] == ["dotted", "tee", "none"], driven_locks.engine
    assert styles["lk_c>lk_d"] == ["dotted", "tee", "tee"], driven_locks.engine
    assert styles["lock:r:R_ONE>lk_x1"] == ["dotted", "none", "none"], driven_locks.engine
    # the self-exclusion is a badge on the job, not a node and not a link
    assert driven_locks.page.evaluate("() => nodeLabel(cy.$id('lk_h'))") == "lk_h \U0001f512"


def test_lock_hub_details_name_every_member(driven_locks: Driven) -> None:
    _ready(driven_locks)
    driven_locks.page.evaluate("() => { cy.$id('lock:r:R_ONE').emit('tap'); }")
    driven_locks.page.wait_for_timeout(200)
    rows = driven_locks.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr')).map("
        "tr => [tr.querySelector('th').textContent, tr.querySelector('td').textContent])"
    )
    assert rows[0] == ["kind", "resource semaphore"], (driven_locks.engine, rows)
    assert rows[1] == ["capacity", "1 unit"], (driven_locks.engine, rows)
    members = [value for label, value in rows if label == "member"]
    assert members == [
        "lk_x1 · 1 unit, released on completion",
        "lk_x2 · in box lk_box · 1 unit, never released",
    ], (driven_locks.engine, members)
    assert driven_locks.page.inner_text("#d-title") == "R_ONE"

    driven_locks.page.evaluate("() => { cy.$id('lock:m:lk_e+lk_f+lk_g').emit('tap'); }")
    driven_locks.page.wait_for_timeout(200)
    rows = driven_locks.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr')).map("
        "tr => [tr.querySelector('th').textContent, tr.querySelector('td').textContent])"
    )
    assert rows[0] == ["kind", "mutual exclusion"], (driven_locks.engine, rows)
    assert [value for label, value in rows if label == "member"] == [
        "lk_e · waits while lk_f, lk_g run",
        "lk_f · waits while lk_g runs; lk_e waits while it runs",
        "lk_g · lk_e, lk_f wait while it runs",
    ], driven_locks.engine
    _click(driven_locks, "#d-close")


def test_pair_link_details_give_both_directions(driven_locks: Driven) -> None:
    _ready(driven_locks)
    driven_locks.page.evaluate(
        "() => { cy.edges('.lock').filter(e => e.data('source') === 'lk_c')[0].emit('tap'); }"
    )
    driven_locks.page.wait_for_timeout(200)
    rows = _detail_rows(driven_locks)
    assert rows["kind"] == "mutual exclusion"
    waits = driven_locks.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr'))"
        ".filter(tr => tr.querySelector('th').textContent === 'waits')"
        ".map(tr => tr.querySelector('td').textContent)"
    )
    assert waits == [
        "lk_c waits while lk_d runs",
        "lk_d waits while lk_c runs",
    ], (driven_locks.engine, waits)
    _click(driven_locks, "#d-close")


def test_the_locks_toggle_hides_and_restores_every_lock_element(driven_locks: Driven) -> None:
    _ready(driven_locks)
    before = driven_locks.page.evaluate("() => cy.elements('.lock').length")
    assert before > 0
    driven_locks.page.uncheck("#locks")
    driven_locks.page.wait_for_timeout(300)
    assert driven_locks.page.evaluate("() => cy.elements('.lock:visible').length") == 0
    # ...and the jobs are all still there: the toggle hides locks alone
    assert driven_locks.page.evaluate("() => cy.nodes(':visible').length") == 12
    driven_locks.page.check("#locks")
    driven_locks.page.wait_for_timeout(300)
    assert driven_locks.page.evaluate("() => cy.elements('.lock:visible').length") == before


def test_a_lock_is_never_a_step_in_a_fan_in_or_a_fan_out(driven_locks: Driven) -> None:
    """Two jobs sharing a semaphore are not upstream of each other: a lock
    orders nothing (M07/DL-21), so the trace walks the flow edges alone."""
    _ready(driven_locks)
    assert _tree_ids(driven_locks, "lk_x2", "fanInTree") == ["lk_box", "lk_x2"]
    assert _tree_ids(driven_locks, "lk_x1", "fanInTree") == ["lk_x1"]
    assert _tree_ids(driven_locks, "lk_x1", "fanOutTree") == ["lk_x1"]
    assert _tree_ids(driven_locks, "lk_a", "fanOutTree") == ["lk_a"]


def test_a_focused_job_keeps_its_own_hub_and_not_the_other_members(
    driven_locks: Driven,
) -> None:
    """The hub is context for the job in focus; the jobs on its other side
    are not what was asked for."""
    _ready(driven_locks)
    driven_locks.page.evaluate("() => { focusOn(cy.$id('lk_x1')); }")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_locks) == ["lk_x1", "lock:r:R_ONE"], driven_locks.engine
    _show_all(driven_locks)


def test_focus_lock_menu_item_shows_the_hub_and_every_member(driven_locks: Driven) -> None:
    """A real right-click on a hub, then the item -- the DL-77 technique."""
    _ready(driven_locks)
    _show_all(driven_locks)
    point = _client_point(driven_locks, "lock:r:R_ONE")
    driven_locks.page.mouse.click(point["x"], point["y"], button="right")
    driven_locks.page.wait_for_timeout(500)
    _click(driven_locks, "#focus-lock")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_locks) == [
        "lk_box",  # lk_x2's own box comes with it: a member without it cannot render
        "lk_x1",
        "lk_x2",
        "lock:r:R_ONE",
    ], driven_locks.engine
    _show_all(driven_locks)


def test_a_collapse_folds_a_lock_link_into_a_meta_edge_that_names_its_member(
    driven_locks: Driven,
) -> None:
    """lk_x2 and lk_d sit in lk_box. Folding it re-points their lock links at
    the box, and each one still says which member it really joins -- and the
    hub follows its members, sitting beside the box when they are all in it.
    Expands again before returning."""
    _ready(driven_locks)
    driven_locks.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)
    folded = sorted(
        driven_locks.page.evaluate(
            "() => cy.edges('.lock.cy-expand-collapse-meta-edge')"
            ".map(e => e.data('source') + '>' + e.data('target'))"
        )
    )
    assert folded == ["lk_c>lk_box", "lock:r:R_ONE>lk_box"], driven_locks.engine
    driven_locks.page.evaluate(
        "() => { cy.edges('.lock.cy-expand-collapse-meta-edge')"
        ".filter(e => e.data('source') === 'lock:r:R_ONE')[0].emit('tap'); }"
    )
    driven_locks.page.wait_for_timeout(200)
    rows = _detail_rows(driven_locks)
    assert rows["stands for"] == "lock:r:R_ONE — lk_x2 (inside a collapsed box)", (
        driven_locks.engine,
        rows,
    )
    assert rows["release"] == "never released", rows
    _click(driven_locks, "#d-close")

    # the panel says where a folded member went
    driven_locks.page.evaluate("() => { cy.$id('lock:r:R_ONE').emit('tap'); }")
    driven_locks.page.wait_for_timeout(200)
    members = driven_locks.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr'))"
        ".filter(tr => tr.querySelector('th').textContent === 'member')"
        ".map(tr => tr.querySelector('td').textContent)"
    )
    assert members[1].endswith("folded into lk_box"), (driven_locks.engine, members)
    _click(driven_locks, "#d-close")

    driven_locks.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)
    assert driven_locks.page.evaluate("() => cy.$id('lk_x2').length") == 1


def test_no_uncaught_page_errors_on_the_locks_page(driven_locks: Driven) -> None:
    _ready(driven_locks)
    assert driven_locks.errors == [], f"{driven_locks.engine}: {driven_locks.errors}"
