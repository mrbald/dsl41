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
costs ~50s, roughly the whole rest of the suite, and the browsers are a separate
~200MB install, so a plain `pytest -q` must neither slow down nor start needing
them. DSL41_BROWSER_TESTS=1 turns it on; .github/workflows/ci.yml's explore-page
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


@pytest.fixture(scope="module", params=ENGINES)
def driven(request: pytest.FixtureRequest, page_url: str) -> Any:
    with sync_playwright() as pw:
        try:
            browser = getattr(pw, request.param).launch()
        except PlaywrightError as exc:  # pragma: no cover -- environment, not logic
            if "Executable doesn't exist" not in str(exc):
                raise
            pytest.skip(f"the {request.param} binary is absent; `playwright install {request.param}`")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        d = Driven(engine=request.param, page=page)
        page.on("pageerror", lambda err: d.errors.append(str(err)))
        page.goto(page_url)
        try:
            yield d
        finally:
            browser.close()


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
    driven.page.evaluate("(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample)
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
    driven.page.evaluate("(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample)
    driven.page.wait_for_timeout(_SETTLE_MS)
    after = driven.page.evaluate(leaves)
    moved = [k for k, v in before.items() if abs(v[0] - after[k][0]) > 0.5 or abs(v[1] - after[k][1]) > 0.5]
    assert not moved, f"{driven.engine}: {len(moved)} leaf node(s) moved with re-layout off: {moved[:5]}"
    driven.page.check("#relayout")
    _show_all(driven)


def test_details_panel_opens_for_a_node_and_for_an_edge(driven: Driven) -> None:
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    driven.page.evaluate("(id) => { cy.$id(id).emit('tap'); }", sample)
    driven.page.wait_for_timeout(200)
    assert driven.page.get_attribute("#details", "hidden") is None, f"{driven.engine}: panel stayed shut"
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
    assert 0 < after < before, f"{driven.engine}: fan-in tree left {after} of {before} nodes visible"
    assert driven.page.evaluate("(id) => cy.$id(id).visible()", sample)
    _show_all(driven)


def test_no_uncaught_page_errors(driven: Driven) -> None:
    """Last: every error the whole session threw. A control that "works" while
    throwing is not working -- and DL-77's throw was silent."""
    _ready(driven)
    assert driven.errors == [], f"{driven.engine}: {driven.errors}"
