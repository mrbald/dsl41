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
from test_viz import CORPUS_DIR, corpus_catalog
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

#: every leaf node's position, keyed by id. Compound boxes are left out on
#: purpose: a box's position is its children's bounding box, so hiding members
#: legitimately moves it -- only leaves can be pinned.
_LEAF_POSITIONS = (
    "() => Object.fromEntries(cy.nodes().filter(n => n.isChildless())"
    ".map(n => [n.id(), [n.position('x'), n.position('y')]]))"
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
    """One Playwright driver for the whole module. `_driven`, `_driven_trace`
    and `_driven_folded` are all module-scoped and independently parametrized,
    so pytest keeps more than one alive at once (a later test in the file
    can still need an earlier fixture's engine) -- a second, nested
    `sync_playwright()` while an outer one is still open raises "Sync API
    inside the asyncio loop", not a graph assertion failure, so the fixtures
    below share this one instance and each launches (and closes) its own
    browser."""
    with sync_playwright() as pw:
        yield pw


def _open_driven(pw: Any, engine: str, url: str) -> Any:
    """Every module-scoped `_driven*` fixture's shared body: launch one engine
    from the module's Playwright instance and load one page into it."""
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
def _driven(request: pytest.FixtureRequest, page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, page_url)


@pytest.fixture
def driven(_driven: Driven) -> Driven:
    """Function-scoped: reloads `_driven`'s shared page and waits for the
    layout the reload retriggers, so every test starts from the page's true
    initial state -- folds, toggles, selection, highlights and node
    positions all restored -- rather than inheriting the previous test's."""
    return _reset(_driven)


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
def _driven_trace(request: pytest.FixtureRequest, trace_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, trace_page_url)


@pytest.fixture
def driven_trace(_driven_trace: Driven) -> Driven:
    """See `driven`: reloads `_driven_trace`'s shared page before every test."""
    return _reset(_driven_trace)


@pytest.fixture(scope="module", params=ENGINES)
def _driven_folded(request: pytest.FixtureRequest, folded_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, folded_page_url)


@pytest.fixture
def driven_folded(_driven_folded: Driven) -> Driven:
    """See `driven`: reloads `_driven_folded`'s shared page before every test."""
    return _reset(_driven_folded)


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


def _reset(d: Driven) -> Driven:
    """Every function-scoped `driven*` fixture's shared body: reload the page
    and wait for the layout the reload retriggers. A reload restores the
    page's true initial state -- folds, toggles, selection, highlights and
    node positions, all of it -- which the old per-test `_show_all` click
    never did, and on the corpus page it is three to six times cheaper
    (measured: 0.16-0.30s here across the three engines, against 0.94-0.96s
    for `_show_all`). Checks `d.dead` itself, the same short-circuit `_ready`
    uses, so a page whose layout never arrives fails once, with one
    diagnosis, instead of a fresh timeout per test."""
    if d.dead:
        pytest.fail(d.dead)
    d.page.reload()
    _ready(d)
    return d


def _click(d: Driven, selector: str) -> None:
    """force=True: the actionability wait (headless firefox times out on the
    toolbar buttons) proves nothing here -- the buttons are static, and what is
    under test is whether the click reaches a listener at all."""
    d.page.locator(selector).click(timeout=_CLICK_TIMEOUT_MS, force=True)


def _control_ready(d: Driven, selector: str) -> None:
    """Wait for a control the selection or the highlight gates to catch up.

    `refreshControls` runs inside the debounced `#stats` rewrite, one task
    after the selection event that triggered it (DL-199's coalescer), so a
    click issued in the same breath as a raw `.select()` would land on a
    control still carrying `disabled` from DL-200. An operator cannot
    outrun a task boundary; a test driving the page globals can. Waiting on
    the control itself, rather than on a duration, is the same discipline
    the layout waits use."""
    d.page.wait_for_function(
        "(sel) => { const el = document.querySelector(sel); return !!el && !el.disabled; }",
        arg=selector,
        timeout=_CLICK_TIMEOUT_MS,
    )


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
            " selected: cy.nodes(':selected').length,"
            " highlighted: highlighted().length})"
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


def _canvas_point(d: Driven, dx: float, dy: float) -> dict[str, float]:
    """A point measured from the `#cy` element's OWN box, not the page's --
    clear of the toolbar above it (which wraps to a different number of rows
    per fixture) and, for a small offset from a corner, clear of the fitted
    graph too: `cy.fit(..., 40)` reserves that much padding around it."""
    point: dict[str, float] = d.page.evaluate(
        "(off) => { const r = document.getElementById('cy').getBoundingClientRect();"
        " return {x: r.left + off[0], y: r.top + off[1]}; }",
        [dx, dy],
    )
    return point


def _positions(d: Driven, ids: list[str]) -> dict[str, list[float]]:
    """Where each of `ids` sits in model coordinates. DL-196's rule is about
    exactly this: a node moves only when a layout runs, so a fold must leave
    every one of these untouched. `ids` is a literal list this module writes,
    never test input."""
    at: dict[str, list[float]] = d.page.evaluate(
        "(ids) => Object.fromEntries(ids.map("
        "id => [id, [cy.$id(id).position('x'), cy.$id(id).position('y')]]))",
        ids,
    )
    return at


def _viewport(d: Driven) -> list[float]:
    """Pan and zoom: the other half of the same rule -- the viewport moves
    only after a layout run, after a find, or on a fit."""
    view: list[float] = d.page.evaluate("() => [cy.pan().x, cy.pan().y, cy.zoom()]")
    return view


def _all_on_screen(d: Driven) -> bool:
    """Everything drawn is inside the canvas: what a fit leaves behind."""
    fitted: bool = d.page.evaluate(
        "() => { const b = cy.nodes(':visible').renderedBoundingBox();"
        " const r = document.getElementById('cy').getBoundingClientRect();"
        " return b.x1 >= -1 && b.y1 >= -1 && b.x2 <= r.width + 1 && b.y2 <= r.height + 1; }"
    )
    return fitted


def _visible_ids(d: Driven) -> list[str]:
    ids: list[str] = d.page.evaluate("() => cy.nodes(':visible').map(n => n.id())")
    return sorted(ids)


def _selected_ids(d: Driven) -> list[str]:
    """The selection layer (DL-196): the set the next bulk op acts on."""
    ids: list[str] = d.page.evaluate("() => cy.nodes(':selected').map(n => n.id())")
    return sorted(ids)


def _arm_layout_wait(d: Driven) -> None:
    """G9: a completion signal for the one layout an action under test is
    about to run, armed before the action so the stop cannot race the arm."""
    d.page.evaluate(
        "() => { window.__layoutDone = false;"
        " cy.one('layoutstop', () => { window.__layoutDone = true; }); }"
    )


def _wait_layout_done(d: Driven) -> None:
    d.page.wait_for_function("() => window.__layoutDone === true", timeout=_LAYOUT_TIMEOUT_MS)


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


# ------------------------------------------------------- DL-196 slice 4 helpers

#: the eleven selection-dependent controls plus the stats button, and the two
#: highlight-dependent ones -- the page's own `SELECTION_CONTROLS` and
#: `HIGHLIGHT_CONTROLS`, kept here as literals rather than read off the page so
#: a test that pins the wrong id fails loudly instead of agreeing with itself.
_SELECTION_CONTROLS = [
    "sel-fan-in",
    "sel-fan-in-tree",
    "sel-fan-out",
    "sel-fan-out-tree",
    "sel-lock-peers",
    "clear-selection",
    "highlight-selected",
    "unhighlight-selected",
    "hide-selected",
    "hide-others",
    "fit-selection",
]
_HIGHLIGHT_CONTROLS = ["select-highlighted", "clear-highlights"]
_CANVAS_MENU_WALKS = ["menu-fan-in", "menu-fan-out", "menu-fan-in-tree", "menu-fan-out-tree"]


def _disabled_map(d: Driven, ids: list[str]) -> dict[str, bool]:
    result: dict[str, bool] = d.page.evaluate(
        "(ids) => Object.fromEntries(ids.map(id => [id, document.getElementById(id).disabled]))",
        ids,
    )
    return result


def _menu_open(d: Driven) -> bool:
    open_: bool = d.page.evaluate(
        "() => { const m = document.querySelector('.cy-context-menus-cxt-menu');"
        " return !!m && getComputedStyle(m).display !== 'none'; }"
    )
    return open_


def _rendered_bbox(d: Driven, selector: str) -> dict[str, float]:
    """The on-screen box of `selector` -- a page-global expression this module
    controls (e.g. "cy.$id('lk_h')" or "cy.nodes(':visible')"), never test
    input -- for a real mouse marquee: `renderedBoundingBox()` plus the `#cy`
    client rect, the same construction `_client_point` uses for one point."""
    box: dict[str, float] = d.page.evaluate(
        f"() => {{ const b = {selector}.renderedBoundingBox();"
        " const r = document.getElementById('cy').getBoundingClientRect();"
        " return {x1: r.left + b.x1, y1: r.top + b.y1, x2: r.left + b.x2, y2: r.top + b.y2}; }"
    )
    return box


def _drag(
    d: Driven,
    start: dict[str, float],
    end: dict[str, float],
    *,
    modifier: str | None = None,
    steps: int = 8,
) -> None:
    """A real mouse drag from `start` to `end`, moved in steps so the renderer
    sees motion rather than a jump. `modifier` (Shift, Control or Meta -- the
    marquee's own set, DL-196) is held down before the mousedown and released
    only after the mouseup, so a marquee driven through this helper always
    ADDS: cytoscape's own rule is that the box adds while the modifier is
    still held at mouseup (verified in DL-196's review of the bundle)."""
    if modifier:
        d.page.keyboard.down(modifier)
    d.page.mouse.move(start["x"], start["y"])
    d.page.mouse.down()
    d.page.mouse.move(end["x"], end["y"], steps=steps)  # interpolated: the renderer sees motion
    d.page.mouse.up()
    if modifier:
        d.page.keyboard.up(modifier)


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


def test_search_select_mode_selects_matches_and_reports_no_match(driven: Driven) -> None:
    """DL-196 slice 3: the private `.hit` class is gone. Enter runs "select
    matches", replacing the selection; a query with no match says so beside
    the field (`#find-note`), never in `#stats`, and changes nothing."""
    _ready(driven)
    _show_all(driven)
    _click(driven, "#clear-selection")
    sample = _sample_node(driven)
    driven.page.fill("#search", sample[: max(6, len(sample) // 2)])
    driven.page.press("#search", "Enter")
    driven.page.wait_for_timeout(_SETTLE_MS)
    stats = driven.page.inner_text("#stats")
    assert _counts(driven)["selected"] > 0, f"{driven.engine}: nothing selected, stats={stats!r}"
    assert "selected" in stats, f"{driven.engine}: {stats!r}"

    driven.page.fill("#search", "zzz-no-such-job-zzz")
    driven.page.press("#search", "Enter")
    driven.page.wait_for_timeout(_SETTLE_MS)
    assert driven.page.inner_text("#find-note") == "no match for “zzz-no-such-job-zzz”", (
        driven.engine
    )

    driven.page.fill("#search", "")
    driven.page.press("#search", "Enter")
    _click(driven, "#clear-selection")


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
    _click(driven, "#clear-selection")


def test_show_all_restores_every_element_and_keeps_selection_and_highlights(
    driven: Driven,
) -> None:
    """DL-196 slice 3: "show all" is visibility only. The old name here
    ("...and_clears_highlights") described a fact that is now false: the
    selection and the highlights both survive it."""
    _ready(driven)
    _click(driven, "#clear-selection")
    _click(driven, "#clear-highlights")
    sample = _sample_node(driven)
    driven.page.evaluate(
        "(id) => { const n = cy.$id(id); n.select(); highlightNodes(n);"
        " n.union(n.descendants()).addClass('hidden'); updateStats(); }",
        sample,
    )
    driven.page.wait_for_timeout(200)
    assert not driven.page.evaluate("(id) => cy.$id(id).visible()", sample)
    _show_all(driven)
    counts = _counts(driven)
    assert counts["visible_nodes"] == counts["nodes"], f"{driven.engine}: {counts}"
    assert counts["visible_edges"] == counts["edges"], f"{driven.engine}: {counts}"
    assert driven.page.evaluate("(id) => cy.$id(id).selected()", sample), (
        f"{driven.engine}: show all cleared the selection"
    )
    assert driven.page.evaluate("(id) => cy.$id(id).hasClass('hl')", sample), (
        f"{driven.engine}: show all cleared the highlight"
    )
    _click(driven, "#clear-selection")
    _click(driven, "#clear-highlights")


def test_relayout_toggle_off_leaves_leaf_positions_alone(driven: Driven) -> None:
    """ "Arrange after hiding" is OFF (DL-196: the preservation principle -- a
    node moves only when a layout runs, and the button is one click away), so
    a focus hides, places the hubs and fits, and moves nothing. The toggle is
    unchecked unless a test checked it, and every test that checks it
    unchecks it again in a `finally`, so the assertion below reads the page's
    own default."""
    _ready(driven)
    _show_all(driven)
    assert not driven.page.evaluate("() => document.getElementById('relayout').checked"), (
        f"{driven.engine}: the arrange-after-hiding toggle is checked"
    )
    sample = _sample_node(driven)
    before = driven.page.evaluate(_LEAF_POSITIONS)
    driven.page.evaluate(
        "(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample
    )
    driven.page.wait_for_timeout(_SETTLE_MS)
    after = driven.page.evaluate(_LEAF_POSITIONS)
    moved = [
        k
        for k, v in before.items()
        if abs(v[0] - after[k][0]) > 0.5 or abs(v[1] - after[k][1]) > 0.5
    ]
    assert not moved, (
        f"{driven.engine}: {len(moved)} leaf node(s) moved with"
        f" arrange after hiding off: {moved[:5]}"
    )
    _show_all(driven)


def test_relayout_toggle_on_re_lays_out_after_a_focus(driven: Driven) -> None:
    """The mirror of the test above, and the toggle's whole job: switched on,
    a focus runs ELK over what is left. Restores the default -- off -- and
    the whole graph."""
    _ready(driven)
    _show_all(driven)
    sample = _sample_node(driven)
    before = driven.page.evaluate(_LEAF_POSITIONS)
    driven.page.check("#relayout")
    try:
        _arm_layout_wait(driven)
        driven.page.evaluate(
            "(id) => { const n = cy.$id(id); focusOn(n.union(n.incomers('node'))); }", sample
        )
        _wait_layout_done(driven)
        driven.page.wait_for_timeout(_SETTLE_MS)
        after = driven.page.evaluate(_LEAF_POSITIONS)
        moved = [
            k
            for k, v in before.items()
            if abs(v[0] - after[k][0]) > 0.5 or abs(v[1] - after[k][1]) > 0.5
        ]
        assert moved, f"{driven.engine}: nothing moved with arrange after hiding on"
    finally:
        driven.page.uncheck("#relayout")  # the page's default, for every test after this
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


def test_context_menu_opens_on_right_click_and_an_item_selects_the_walk(driven: Driven) -> None:
    """The DL-77 defect itself: the menu is built from customized built-in
    elements, so this is the one control that needs a real mouse. Under
    DL-196 slice 3 a walk item ADDS to the selection -- it no longer narrows
    the graph by itself, that is hide-others' separate job."""
    _ready(driven)
    _show_all(driven)
    _click(driven, "#clear-selection")
    assert "context menu unavailable" not in driven.page.inner_text("#stats"), (
        f"{driven.engine}: the page reports its own context menu as lost"
    )
    sample = _sample_node(driven)
    point = _client_point(driven, sample)
    driven.page.mouse.click(point["x"], point["y"], button="right")
    driven.page.wait_for_timeout(500)
    assert driven.page.evaluate(
        "() => { const e = document.querySelector('.cy-context-menus-cxt-menu');"
        " return !!e && getComputedStyle(e).display !== 'none'; }"
    ), f"{driven.engine}: right-click did not open the menu"
    assert driven.page.evaluate(
        "() => getComputedStyle(document.getElementById('fan-in-tree')).display !== 'none'"
    ), f"{driven.engine}: fan-in-tree not offered on a plain node"

    _click(driven, "#fan-in-tree")
    driven.page.wait_for_timeout(_SETTLE_MS)
    selected = _selected_ids(driven)
    assert sample in selected, f"{driven.engine}: {sample} not selected after its own fan-in-tree"
    assert selected == _tree_ids(driven, sample, "fanInTree"), driven.engine
    _click(driven, "#clear-selection")


def test_dismissing_the_menu_with_a_background_click_leaves_the_selection_intact(
    driven: Driven,
) -> None:
    """DL-196's own verification ask: the plugin closes on the tapstart of an
    outside click, and cytoscape then completes that click as a background
    tap that would otherwise clear the selection; dismissing on a NODE is a
    click and replaces it -- this pins only the background case, real mouse,
    three engines."""
    _ready(driven)
    _show_all(driven)
    _click(driven, "#clear-selection")
    sample = _sample_node(driven)
    driven.page.evaluate("(id) => { cy.$id(id).select(); }", sample)
    before = _selected_ids(driven)
    point = _client_point(driven, sample)
    driven.page.mouse.click(point["x"], point["y"], button="right")
    driven.page.wait_for_timeout(500)
    bg = _canvas_point(driven, 10, 10)  # the canvas's own corner: a real background tap
    driven.page.mouse.click(bg["x"], bg["y"])
    driven.page.wait_for_timeout(250)
    assert _selected_ids(driven) == before, (
        f"{driven.engine}: dismissing the menu with a background click changed the selection"
    )
    _click(driven, "#clear-selection")


def test_ctrl_click_on_a_node_leaves_the_selection_unchanged_either_way(driven: Driven) -> None:
    """DL-196's webkit verification ask: does ctrl+click on a node open the
    context menu (the Mac convention) or select it? Either is acceptable --
    what must hold is that it does not leave the selection in a surprising
    state. The actual outcome per engine is reported alongside this slice's
    test report, not asserted here as one fixed behaviour."""
    _ready(driven)
    _show_all(driven)
    _click(driven, "#clear-selection")
    sample = _sample_node(driven)
    point = _client_point(driven, sample)
    driven.page.keyboard.down("Control")
    driven.page.mouse.click(point["x"], point["y"])
    driven.page.keyboard.up("Control")
    driven.page.wait_for_timeout(500)
    opened = driven.page.evaluate(
        "() => { const e = document.querySelector('.cy-context-menus-cxt-menu');"
        " return !!e && getComputedStyle(e).display !== 'none'; }"
    )
    selected = _selected_ids(driven)
    assert selected in ([], [sample]), (
        f"{driven.engine}: ctrl+click left an unexpected selection {selected}"
        f" (menu opened: {opened})"
    )
    if opened:
        driven.page.mouse.click(10, 10)
        driven.page.wait_for_timeout(200)
    _click(driven, "#clear-selection")


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


def test_context_menu_lists_every_item_in_declared_order(driven_trace: Driven) -> None:
    """Every configured item sits in the DOM regardless of the clicked node --
    non-matching ones are `display: none`, not absent -- so querying all
    sixteen finds them all, in the order the template's `menuItems` array
    builds them (DL-196 slice 3): the six flow items, lock peers, hide, the
    box pair, the hub pair, then the four canvas-only walks."""
    _ready(driven_trace)
    point = _client_point(driven_trace, "B")
    driven_trace.page.mouse.click(point["x"], point["y"], button="right")
    driven_trace.page.wait_for_timeout(500)
    try:
        items = driven_trace.page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'#fan-in,#fan-out,#fan-in-tree,#fan-out-tree,#both-trees,#neighbours,"
            "#lock-peers,#hide,#collapse,#expand,#lock-members,#hide-lock,"
            "#menu-fan-in,#menu-fan-out,#menu-fan-in-tree,#menu-fan-out-tree'"
            ")).map(e => e.id)"
        )
        assert items == [
            "fan-in",
            "fan-out",
            "fan-in-tree",
            "fan-out-tree",
            "both-trees",
            "neighbours",
            "lock-peers",
            "hide",
            "collapse",
            "expand",
            "lock-members",
            "hide-lock",
            "menu-fan-in",
            "menu-fan-out",
            "menu-fan-in-tree",
            "menu-fan-out-tree",
        ], driven_trace.engine
    finally:
        driven_trace.page.mouse.click(10, 10)  # dismiss: an outside click, nothing under test
        driven_trace.page.wait_for_timeout(200)


def _collapse_b(d: Driven) -> None:
    """Collapse box B with a real dbltap and wait for the settle. The
    precondition `test_meta_edge_and_collapsed_box_show_their_own_details_rows`
    and `test_search_expands_a_collapsed_box_to_find_the_match` need, factored
    out so each builds it itself rather than inheriting it from
    `test_dbltap_collapses_box_b_and_folds_its_border_edges`, which uses it too."""
    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)


def test_dbltap_collapses_box_b_and_folds_its_border_edges(driven_trace: Driven) -> None:
    """A real dbltap on B collapses it and folds its border edges into meta
    edges."""
    _ready(driven_trace)
    _collapse_b(driven_trace)

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
    """Collapses B itself (via `_collapse_b`) as its own precondition, then
    checks that a meta edge and the collapsed box each show their own details
    rows. Tapping only opens the panel, so it leaves the collapse untouched."""
    _ready(driven_trace)
    _collapse_b(driven_trace)
    stats = driven_trace.page.inner_text("#stats")
    assert "1 box collapsed" in stats, (
        f"{driven_trace.engine}: expected the collapse helper to leave B collapsed, got {stats!r}"
    )

    driven_trace.page.evaluate(
        "() => { cy.edges('.cy-expand-collapse-meta-edge')"
        ".filter(e => e.source().id() === 'Q')[0].emit('tap'); }"
    )
    driven_trace.page.wait_for_timeout(200)
    rows = _detail_rows(driven_trace)
    assert rows["stands for"] == "Q → M (inside a collapsed box)", (driven_trace.engine, rows)
    assert rows["via"] == "success", (driven_trace.engine, rows)
    # the fold re-points the edge and keeps its data, `attr` included -- which
    # is what lets isOverride read the partition off it (DL-193)
    assert rows["attribute"] == "condition", (driven_trace.engine, rows)
    _click(driven_trace, "#d-close")

    driven_trace.page.evaluate("() => { cy.$id('B').emit('tap'); }")
    driven_trace.page.wait_for_timeout(200)
    rows = _detail_rows(driven_trace)
    assert rows["members"] == "4 (collapsed)", (driven_trace.engine, rows)
    _click(driven_trace, "#d-close")


def test_search_expands_a_collapsed_box_to_find_the_match(driven_trace: Driven) -> None:
    """Collapses B itself (via `_collapse_b`) as its own precondition: a
    search on an already-expanded page would also find IM, so asserting the
    collapse before searching is what makes this a real test of the expand
    path rather than a vacuous one. Restores the page to fully expanded and
    the search box empty afterward, and re-fits the view: the search's own
    `cy.fit(hits, 60)` zooms tight around the one match, which can leave
    other nodes' rendered position outside the fixed 1600x1000 viewport."""
    _ready(driven_trace)
    _collapse_b(driven_trace)
    stats = driven_trace.page.inner_text("#stats")
    assert "1 box collapsed" in stats, (
        f"{driven_trace.engine}: expected the collapse helper to leave B collapsed, got {stats!r}"
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
    assert driven_trace.page.inner_text("#stats").endswith("1 match selected"), (
        driven_trace.engine,
        driven_trace.page.inner_text("#stats"),
    )
    driven_trace.page.fill("#search", "")
    driven_trace.page.press("#search", "Enter")
    _click(driven_trace, "#clear-selection")
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

    # the fold leaves B's centre where it was (DL-196), but it is now one
    # small node rather than a container: re-read the rendered point
    point = _client_point(driven_trace, "B")
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
    """The two buttons, and DL-196 through them: a fold runs no layout, no
    fit and no pan, so the viewport is exactly where it was on both sides."""
    _ready(driven_trace)
    view = _viewport(driven_trace)
    _click(driven_trace, "#collapse-all")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(driven_trace) == ["B", "C", "D", "P", "Q"], driven_trace.engine
    assert _viewport(driven_trace) == view, driven_trace.engine

    _click(driven_trace, "#expand-all")
    driven_trace.page.wait_for_timeout(_SETTLE_MS)
    assert _viewport(driven_trace) == view, driven_trace.engine
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


def test_a_collapse_and_an_expand_move_nothing_outside_the_box(driven_trace: Driven) -> None:
    """DL-196's own rule, both ways round: a fold runs neither layout nor fit
    nor pan, so every node outside the box keeps its exact position and the
    viewport does not move -- through the collapse, and through the expand
    after it. Starts and ends fully expanded."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    outside = ["C", "D", "P", "Q"]
    before, view = _positions(d, outside), _viewport(d)

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "P", "Q"], d.engine
    assert _positions(d, outside) == before, d.engine
    assert _viewport(d) == view, d.engine

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('IM').length") == 1, d.engine
    assert _positions(d, outside) == before, d.engine
    assert _viewport(d) == view, d.engine


def test_the_members_follow_a_collapsed_box_that_was_dragged(driven_trace: Driven) -> None:
    """DL-196's own verification ask for this slice: a fold that runs no
    layout is worth having only if manual placement survives it. Collapse B,
    drag the collapsed node by a known delta -- `node.position` is where a
    drag ends -- expand, and every member is exactly that delta from where it
    was, nested ones included. Starts and ends fully expanded."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    members, outside = ["M", "N", "IM"], ["C", "D", "P", "Q"]
    before, outside_before = _positions(d, members), _positions(d, outside)

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "P", "Q"], d.engine
    d.page.evaluate(
        "(at) => { cy.$id('B').position({x: at[0] + 500, y: at[1] + 300}); }",
        _positions(d, ["B"])["B"],
    )
    d.page.wait_for_timeout(200)

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    after = _positions(d, members)
    delta = {
        k: [round(after[k][0] - before[k][0]), round(after[k][1] - before[k][1])] for k in members
    }
    assert delta == {k: [500, 300] for k in members}, (d.engine, delta)
    assert _positions(d, outside) == outside_before, d.engine


def test_the_members_follow_a_collapsed_box_dragged_with_a_real_pointer(
    driven_trace: Driven,
) -> None:
    """G8, carried from DL-198's review into this slice: the same property as
    the test above, this time through a REAL mouse drag of the collapsed box
    rather than `node.position()`. The box's own model-space delta is read
    off the page rather than assumed from the screen-pixel one, since a drag
    moves a node by the screen delta divided by the current zoom. Starts and
    ends fully expanded."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    members, outside = ["M", "N", "IM"], ["C", "D", "P", "Q"]
    before, outside_before = _positions(d, members), _positions(d, outside)

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "P", "Q"], d.engine
    box_before = _positions(d, ["B"])["B"]

    start = _client_point(d, "B")
    _drag(d, start, {"x": start["x"] + 150, "y": start["y"] + 100})
    d.page.wait_for_timeout(200)
    box_after = _positions(d, ["B"])["B"]
    box_delta = [round(box_after[0] - box_before[0]), round(box_after[1] - box_before[1])]
    assert box_delta != [0, 0], f"{d.engine}: the pointer drag did not move the box"

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    after = _positions(d, members)
    delta = {
        k: [round(after[k][0] - before[k][0]), round(after[k][1] - before[k][1])] for k in members
    }
    assert delta == {k: box_delta for k in members}, (d.engine, delta, box_delta)
    assert _positions(d, outside) == outside_before, d.engine


def test_the_arrange_button_lays_the_drawn_graph_out_again(driven_trace: Driven) -> None:
    """Since a fold moves nothing, the operator needs one control that does.
    Drag a node far out of place -- `node.position` is where a drag ends --
    press arrange, and ELK puts it back among the others; the view fits what
    it drew. Leaves the page laid out and fitted."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    d.page.evaluate("() => { cy.$id('P').position({x: 9000, y: 9000}); }")
    assert _positions(d, ["P"])["P"] == [9000, 9000], d.engine
    # derange the VIEWPORT too, or the fit assertion cannot fail: the page was
    # fitted a moment ago, and ELK's fresh coordinates land inside that view
    # whether a fit ran after them or not
    d.page.evaluate("() => { cy.zoom(6); cy.pan({x: -3000, y: -2000}); }")
    assert not _all_on_screen(d), f"{d.engine}: the viewport was not deranged"
    _click(d, "#arrange")
    d.page.wait_for_function(
        "() => cy.$id('P').position('x') !== 9000 || cy.$id('P').position('y') !== 9000",
        timeout=_LAYOUT_TIMEOUT_MS,
    )
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _positions(d, ["P"])["P"] != [9000, 9000], d.engine
    assert _all_on_screen(d), f"{d.engine}: arrange did not end with a fit"


def test_collapse_moves_a_nested_members_selection_to_the_outer_box(driven_trace: Driven) -> None:
    """DL-196: a collapse moves a folded member's selection to its box, and
    an expand leaves the box selected with the member unselected -- nested
    too. IM sits two levels down (inside IB, inside B); collapsing B still
    moves its selection all the way up, not to the intermediate IB."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('IM').select(); }")
    d.page.evaluate("() => { ec.collapse(cy.$id('B'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["B"], d.engine
    d.page.evaluate("() => { ec.expand(cy.$id('B'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["B"], d.engine
    assert d.page.evaluate("() => cy.$id('IM').selected()") is False, d.engine
    _click(d, "#clear-selection")


def test_sel_fan_in_tree_from_two_seeds_matches_the_union_of_each_seeds_tree(
    driven_trace: Driven,
) -> None:
    """DL-196's own verification ask: a two-seed transitive walk under
    trace-through-boxes reaches exactly the union of what each seed reaches
    alone, the gate/producer promotion (fanInTree's `requeue`) included."""
    d = driven_trace
    _ready(d)
    _click(d, "#clear-selection")
    expected = sorted(set(_tree_ids(d, "C", "fanInTree")) | set(_tree_ids(d, "D", "fanInTree")))
    d.page.evaluate("() => { cy.$id('C').select(); cy.$id('D').select(); }")
    _control_ready(d, "#sel-fan-in-tree")
    _click(d, "#sel-fan-in-tree")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == expected, d.engine
    _click(d, "#clear-selection")


def test_sel_fan_in_tree_walks_through_a_collapsed_box_then_hide_others_keeps_it_folded(
    driven_trace: Driven,
) -> None:
    """B stays collapsed throughout: a walk never descends into a folded
    box's members, it continues through the proxy's own border meta-edges
    (G1, correcting DL-196's "stops at" wording); hide-others leaves the
    fold untouched too, since none of B's own members are in the selection
    (the fold-on-hide rule is about a selected box with no selected member,
    which does not apply here -- B itself was never selected)."""
    d = driven_trace
    _ready(d)
    _show_all(d)
    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "P", "Q"], d.engine
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('D').select(); }")
    _control_ready(d, "#sel-fan-in-tree")
    _click(d, "#sel-fan-in-tree")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["B", "D", "P", "Q"], d.engine
    assert d.page.evaluate("() => cy.$id('B').hasClass('cy-expand-collapse-collapsed-node')"), (
        d.engine
    )
    stats = d.page.inner_text("#stats")
    assert stats.endswith("fan-in of selection (1), transitive, via boxes"), (d.engine, stats)

    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "D", "P", "Q"], d.engine
    assert d.page.evaluate("() => cy.$id('B').hasClass('cy-expand-collapse-collapsed-node')"), (
        d.engine
    )

    _click(d, "#clear-selection")
    _show_all(d)
    d.page.evaluate("() => { if (ec.isExpandable(cy.$id('B'))) ec.expand(cy.$id('B')); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "IB", "IM", "M", "N", "P", "Q"], d.engine


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


def test_expanding_the_pre_folded_box_moves_nothing_but_its_members(
    driven_folded: Driven,
) -> None:
    """The same DL-196 rule where the fold was the emitter's, not the
    operator's: expanding B restores its members around B's own position and
    touches nothing else -- no layout, no fit, no pan. (The hubs are placed
    again after every fold; this fixture declares no locks, so there are none
    to move -- test_hubs_stay_clear_of_the_jobs_after_a_collapse carries that
    half.) Leaves B expanded; the search test further down folds it again
    itself."""
    d = driven_folded
    _ready(d)
    assert d.page.evaluate("() => ec !== null && ec.isExpandable(cy.$id('B'))"), d.engine
    outside = ["C", "D", "P", "Q"]
    before, view = _positions(d, outside), _viewport(d)

    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('IM').length") == 1, d.engine
    assert _positions(d, outside) == before, d.engine
    assert _viewport(d) == view, d.engine


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
def _driven_override(
    request: pytest.FixtureRequest, override_page_url: str, _playwright: Any
) -> Any:
    yield from _open_driven(_playwright, request.param, override_page_url)


@pytest.fixture
def driven_override(_driven_override: Driven) -> Driven:
    """See `driven`: reloads `_driven_override`'s shared page before every test."""
    return _reset(_driven_override)


@pytest.fixture(scope="module", params=ENGINES)
def _driven_broken(request: pytest.FixtureRequest, broken_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, broken_page_url)


@pytest.fixture
def driven_broken(_driven_broken: Driven) -> Driven:
    """See `driven`: reloads `_driven_broken`'s shared page before every test."""
    return _reset(_driven_broken)


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
    restores them on cytoscape's default grid, every one of them on the same
    point. The first layout runs over the whole graph before the emitter's
    folds, so the members carry real positions into the fold and come back
    around the box's own position, on distinct points. The expand runs no
    layout of its own (DL-196), so the nodes outside the box do not move."""
    d = driven_folded
    _ready(d)
    if not d.page.evaluate("() => ec !== null && ec.isExpandable(cy.$id('B'))"):
        d.page.evaluate("() => { ec.collapse(cy.$id('B'), { layoutBy: null }); }")
        d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('IM').length") == 0  # IM is inside the folded box
    outside = ["C", "D", "P", "Q"]
    before = _positions(d, outside)
    d.page.fill("#search", "IM")
    d.page.press("#search", "Enter")
    d.page.wait_for_function(
        "() => cy.$id('IM').length === 1"
        " && document.getElementById('stats').textContent.includes('1 match selected')",
        timeout=_LAYOUT_TIMEOUT_MS,
    )
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.nodes().length") == 9
    assert _positions(d, outside) == before, d.engine
    distinct = d.page.evaluate(
        "() => new Set(['M', 'N', 'IM'].map(id => Math.round(cy.$id(id).position('x')) + ','"
        " + Math.round(cy.$id(id).position('y')))).size"
    )
    assert distinct == 3, (d.engine, distinct)


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
    assert not {"collapse", "expand"} & set(menu_ids)
    d.page.fill("#search", "IM")
    d.page.press("#search", "Enter")
    d.page.wait_for_function(
        "() => document.getElementById('stats').textContent.includes('1 match selected')",
        timeout=_CLICK_TIMEOUT_MS,
    )
    # arrange is wired above the guard as well (DL-196 under DL-77's rule):
    # the layout it runs is essential, and the loss notice survives it
    d.page.evaluate("() => { cy.$id('P').position({x: 9000, y: 9000}); }")
    _click(d, "#arrange")
    d.page.wait_for_function("() => cy.$id('P').position('x') !== 9000", timeout=_LAYOUT_TIMEOUT_MS)
    stats = d.page.inner_text("#stats")
    assert "visible" in stats and "box collapse unavailable in this browser" in stats, stats
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
def _driven_cond(request: pytest.FixtureRequest, cond_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, cond_page_url)


@pytest.fixture
def driven_cond(_driven_cond: Driven) -> Driven:
    """See `driven`: reloads `_driven_cond`'s shared page before every test."""
    return _reset(_driven_cond)


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
    # the suffix names which OR, not which operand: it is there to GROUP the
    # arrows of one alternation, and the hollow head already says "alternative"
    labels = driven_cond.page.evaluate(
        "() => Object.fromEntries(cy.edges().filter(e => e.data('target') === 'TWO')"
        ".map(e => [e.data('source'), e.data('label')]))"
    )
    assert labels == {"A": "|a", "B": "|a", "C": "|b", "D": "|b", "E": ""}, driven_cond.engine
    # a flat OR spends no label at all
    flat = driven_cond.page.evaluate(
        "() => Array.from(new Set(cy.edges().filter(e => e.data('target') === 'ANY')"
        ".map(e => e.data('label'))))"
    )
    assert flat == [""], driven_cond.engine


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


def test_a_tree_leaf_highlights_its_arrow_on_hover_and_inspects_it_on_click(
    driven_cond: Driven,
) -> None:
    """A real pointer, not an emitted event: the leaf is a DOM button and the
    arrow it names is a canvas element. DL-196 slice 3: a leaf click marks
    the arrow `.inspected` (edges are unselectable), it does not select it."""
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
    # the keyboard gets the same answer: the leaf is a real button, Tab
    # reaches it, and focus must highlight what hover highlights
    leaves.nth(1).focus()
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate(
        "() => cy.edges('.leaf-hover').map(e => e.data('source') + '>' + e.data('target'))"
    ) == ["B>AOA"], driven_cond.engine
    driven_cond.page.evaluate("() => document.activeElement.blur()")
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate("() => cy.edges('.leaf-hover').length") == 0
    leaves.nth(1).click()
    driven_cond.page.wait_for_timeout(200)
    assert driven_cond.page.evaluate(
        "() => cy.edges('.inspected').map(e => e.data('source') + '>' + e.data('target'))"
    ) == ["B>AOA"], driven_cond.engine
    driven_cond.page.evaluate("() => { cy.emit('tap'); }")


def test_edge_details_name_the_attribute_and_the_branch(driven_cond: Driven) -> None:
    """The review's MINOR: an operator who taps a hollow arrow could not learn
    which alternation it belonged to. The panel says both now."""
    _ready(driven_cond)
    driven_cond.page.evaluate(
        "() => { cy.edges().filter(e => e.data('source') === 'B'"
        " && e.data('target') === 'AOA')[0].emit('tap'); }"
    )
    driven_cond.page.wait_for_timeout(200)
    rows = _detail_rows(driven_cond)
    assert rows["attribute"] == "condition", (driven_cond.engine, rows)
    assert rows["branch"] == "|1", (driven_cond.engine, rows)
    _click(driven_cond, "#d-close")


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
    _click(driven_cond, "#clear-selection")
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
# above. Four of these tests mutate the graph -- the toggle, the two focus
# tests and the collapse -- and each restores what it changed; the focus
# tests also call `_show_all` on entry, so a failure between two halves of
# one of them cannot leave the rest of that engine's module hidden.


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
def _driven_locks(request: pytest.FixtureRequest, locks_page_url: str, _playwright: Any) -> Any:
    yield from _open_driven(_playwright, request.param, locks_page_url)


@pytest.fixture
def driven_locks(_driven_locks: Driven) -> Driven:
    """See `driven`: reloads `_driven_locks`'s shared page before every test."""
    return _reset(_driven_locks)


def _hub_overlaps(d: Driven) -> list[str]:
    """Every drawn hub whose box touches a drawn job or box. A lock is a fact
    ABOUT jobs and is placed on them after the layout, so the one thing it
    must not do is cover one."""
    hits: list[str] = d.page.evaluate(
        """() => {
          const hits = [];
          cy.nodes('.lock:visible').forEach(hub => {
            const h = hub.boundingBox();
            cy.nodes(':visible').not('.lock').forEach(other => {
              const o = other.boundingBox();
              if (h.x1 < o.x2 && o.x1 < h.x2 && h.y1 < o.y2 && o.y1 < h.y2) {
                hits.push(hub.id() + ' over ' + other.id());
              }
            });
          });
          return hits;
        }"""
    )
    return hits


def _drawn_hubs(d: Driven) -> list[str]:
    ids: list[str] = d.page.evaluate("() => cy.nodes('.lock:visible').map(n => n.id())")
    return sorted(ids)


def test_initial_stats_and_find_controls_are_ready(driven_locks: Driven) -> None:
    """DL-196 slice 3's `#stats` format and G6/G7's fix (the find field and
    its two buttons stay disabled until `finishInitial`, like `#arrange`).
    Runs first against this fixture, before any other test mutates it."""
    d = driven_locks
    _ready(d)
    stats = d.page.inner_text("#stats")
    assert stats == (
        "visible 18 / 18 nodes · 5 / 5 edges · 9 locks · 0 selected · 0 highlighted"
    ), (d.engine, stats)
    for el_id in ("search", "find-select", "find-highlight"):
        assert d.page.evaluate(f"() => document.getElementById('{el_id}').disabled") is False, (
            f"{d.engine}: #{el_id} still disabled after _ready"
        )


def test_lock_hubs_are_placed_clear_of_every_job(driven_locks: Driven) -> None:
    """The centroid of a group's members is usually a point one of them
    occupies -- and with one drawn member it IS that member. The hub walks a
    spiral from there until nothing is under it."""
    _ready(driven_locks)
    assert len(_drawn_hubs(driven_locks)) == 4, driven_locks.engine
    assert _hub_overlaps(driven_locks) == [], driven_locks.engine


def test_a_hub_with_one_drawn_member_is_beside_it_and_the_view_still_fits(
    driven_locks: Driven,
) -> None:
    """Focusing lk_x1 hides its only R_ONE partner. The hub must not land on
    the one member left, and the fit must see both -- a fit over one point
    zooms until the octagon fills the viewport and the job vanishes under
    it. Runs with "arrange after hiding" ON, which is the branch that fits
    AFTER ELK and so the one this test was written for; the toggle defaults
    off since DL-196, so it is checked here and unchecked again in a
    `finally`. Restores the page."""
    _ready(driven_locks)
    _show_all(driven_locks)
    driven_locks.page.check("#relayout")
    try:
        _arm_layout_wait(driven_locks)
        driven_locks.page.evaluate("() => { focusOn(cy.$id('lk_x1')); }")
        _wait_layout_done(driven_locks)
        driven_locks.page.wait_for_timeout(_SETTLE_MS)
        assert _visible_ids(driven_locks) == ["lk_x1", "lock:r:R_ONE"], driven_locks.engine
        assert _hub_overlaps(driven_locks) == [], driven_locks.engine
        zoom = driven_locks.page.evaluate("() => cy.zoom()")
        assert zoom < 12, f"{driven_locks.engine}: fit degenerated to {zoom}x"
    finally:
        driven_locks.page.uncheck("#relayout")  # the page's default
    _show_all(driven_locks)


def test_hubs_stay_clear_of_the_jobs_after_a_collapse(driven_locks: Driven) -> None:
    """A collapse takes members out and leaves the box: the hubs are placed
    again against the new picture. DL-192's property, kept under DL-196 --
    the fold itself runs no layout now, and placeLocks runs from the fold's
    own handler instead of from a layout's. Expands before returning."""
    _ready(driven_locks)
    _show_all(driven_locks)
    driven_locks.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)
    assert _hub_overlaps(driven_locks) == [], driven_locks.engine
    driven_locks.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    driven_locks.page.wait_for_timeout(_SETTLE_MS)


def test_collapse_moves_a_selected_members_selection_to_its_box(driven_locks: Driven) -> None:
    """DL-196: a collapse moves a folded member's selection to its box; an
    expand leaves the box selected and the member unselected."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_d').select(); }")
    d.page.evaluate("() => { ec.collapse(cy.$id('lk_box'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_box"], d.engine
    d.page.evaluate("() => { ec.expand(cy.$id('lk_box'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_box"], d.engine
    assert d.page.evaluate("() => cy.$id('lk_d').selected()") is False, d.engine
    _click(d, "#clear-selection")


def test_hide_others_collapses_a_selected_box_with_no_selected_member(
    driven_locks: Driven,
) -> None:
    """DL-196: a selected box with no selected descendant is folded, not
    emptied -- it stands for its members and no sibling is drawn. The hub
    context is read off the box after the fold: R_ONE reaches through the
    now-folded lk_x2, R_BIG does not (lk_d's pair link is not a hub)."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_box').select(); }")
    _control_ready(d, "#hide-others")
    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_x2').length") == 0, (
        f"{d.engine}: lk_box was not collapsed"
    )
    assert _visible_ids(d) == ["lk_box", "lock:r:R_ONE"], d.engine
    assert _selected_ids(d) == ["lk_box"], d.engine

    _click(d, "#show-all")
    d.page.wait_for_timeout(_SETTLE_MS)
    d.page.evaluate("() => { ec.expandAll({ layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.elements('.hidden').length") == 0, d.engine
    _click(d, "#clear-selection")


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

    # a threshold (res_type T) is a level check: it holds nothing, so there
    # is no release to state
    driven_locks.page.evaluate("() => { cy.$id('lock:r:R_GATE').emit('tap'); }")
    driven_locks.page.wait_for_timeout(200)
    rows = driven_locks.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-rows tr')).map("
        "tr => [tr.querySelector('th').textContent, tr.querySelector('td').textContent])"
    )
    assert [value for label, value in rows if label == "member"] == [
        "lk_x4 · threshold gate: needs 2 free, holds nothing"
    ], (driven_locks.engine, rows)

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
    assert driven_locks.page.evaluate("() => cy.nodes(':visible').length") == 18
    driven_locks.page.check("#locks")
    driven_locks.page.wait_for_timeout(300)
    assert driven_locks.page.evaluate("() => cy.elements('.lock:visible').length") == before


def test_a_lock_is_never_a_step_in_a_fan_in_or_a_fan_out(driven_locks: Driven) -> None:
    """Two jobs sharing a semaphore are not upstream of each other: a lock
    orders nothing (M07/DL-21), so the trace walks the flow edges alone."""
    _ready(driven_locks)
    # lk_x1 and lk_x2 share R_ONE and neither is upstream of the other: what
    # a fan-in reaches is the seed and the box, never the other member
    assert _tree_ids(driven_locks, "lk_x2", "fanInTree") == ["lk_box", "lk_seed", "lk_x2"]
    assert _tree_ids(driven_locks, "lk_x1", "fanInTree") == ["lk_seed", "lk_x1"]
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


def test_lock_members_then_hide_others_shows_the_hub_and_every_member(driven_locks: Driven) -> None:
    """DL-196 declines a dedicated "focus lock" item ("select members, then
    hide others" loses nothing): a real right-click on the hub for the first
    step, the toolbar for the second."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    point = _client_point(d, "lock:r:R_ONE")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    _click(d, "#lock-members")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_x1", "lk_x2"], d.engine
    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == [
        "lk_box",  # lk_x2's own box comes with it: a member without it cannot render
        "lk_x1",
        "lk_x2",
        "lock:r:R_ONE",
    ], d.engine
    assert _hub_overlaps(d) == [], d.engine
    _click(d, "#clear-selection")
    _show_all(d)


def test_node_menu_offers_walks_and_lock_items_by_node_kind(driven_locks: Driven) -> None:
    """DL-196 slice 3: the six flow items plus lock-peers sit only on
    `node[!lock]`, and lock-peers only where a lock link exists; a hub
    (`node.lock`) offers `lock-members` and `hide-lock` instead, never a
    flow item. Restores the page by dismissing each menu with a background
    click."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    flow_ids = ["fan-in", "fan-out", "fan-in-tree", "fan-out-tree", "both-trees", "neighbours"]

    def _shown(ids: list[str]) -> list[str]:
        selector = ",".join("#" + i for i in ids)
        result: list[str] = d.page.evaluate(
            f"() => Array.from(document.querySelectorAll('{selector}'))"
            ".filter(e => getComputedStyle(e).display !== 'none').map(e => e.id)"
        )
        return result

    def _dismiss() -> None:
        d.page.mouse.click(10, 10)  # background: nothing under test
        d.page.wait_for_timeout(200)

    # lk_x1 has an R_ONE link: all six walks, lock-peers and hide
    point = _client_point(d, "lk_x1")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert sorted(_shown([*flow_ids, "lock-peers", "hide"])) == sorted(
        [*flow_ids, "lock-peers", "hide"]
    ), d.engine
    _dismiss()

    # lk_seed carries no lock link at all: the six walks and hide, never lock-peers
    point = _client_point(d, "lk_seed")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert sorted(_shown(flow_ids + ["hide"])) == sorted(flow_ids + ["hide"]), d.engine
    assert _shown(["lock-peers"]) == [], d.engine
    _dismiss()

    # the hub offers exactly lock-members and hide-lock, no flow item
    point = _client_point(d, "lock:r:R_ONE")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert _shown(flow_ids) == [], d.engine
    assert sorted(_shown(["lock-members", "hide-lock"])) == ["hide-lock", "lock-members"], d.engine
    assert d.page.inner_text("#lock-members") == "select members of R_ONE", d.engine
    _dismiss()


def test_canvas_menu_offers_the_four_walks_of_the_selection(driven_locks: Driven) -> None:
    """DL-196: the canvas menu carries no per-node items, only the four
    walks, seeded from the whole selection; the label names the count."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_x1').select(); cy.$id('lk_x5').select(); }")
    bg = _canvas_point(d, 10, 10)  # near the canvas's own corner, inside the fit padding
    d.page.mouse.click(bg["x"], bg["y"], button="right")
    d.page.wait_for_timeout(500)
    canvas_ids = ["menu-fan-in", "menu-fan-out", "menu-fan-in-tree", "menu-fan-out-tree"]
    selector = ",".join("#" + i for i in canvas_ids)
    shown = d.page.evaluate(
        f"() => Array.from(document.querySelectorAll('{selector}'))"
        ".filter(e => getComputedStyle(e).display !== 'none').map(e => e.id)"
    )
    assert sorted(shown) == sorted(canvas_ids), d.engine
    assert d.page.inner_text("#menu-fan-in") == "select fan-in of selection (2)", d.engine
    assert d.page.inner_text("#menu-fan-in-tree") == "select fan-in of selection (2), transitive", (
        d.engine
    )
    d.page.mouse.click(bg["x"], bg["y"])  # dismiss: background, nothing under test
    d.page.wait_for_timeout(200)
    _click(d, "#clear-selection")


def _walk_fan_in_from_lk_x2(d: Driven) -> None:
    """Clear the selection, select lk_x1 and lk_x5, then right-click lk_x2 and
    walk its fan-in tree from the node menu -- the five-node selection
    (lk_box, lk_seed, lk_x1, lk_x2, lk_x5) that
    `test_node_menu_walk_adds_to_an_existing_selection` and
    `test_hide_others_keeps_the_selection_then_show_all_restores_visibility`
    each need as their own precondition, factored out so neither inherits it
    from the other."""
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_x1').select(); cy.$id('lk_x5').select(); }")
    point = _client_point(d, "lk_x2")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    _click(d, "#fan-in-tree")
    d.page.wait_for_timeout(_SETTLE_MS)


def test_node_menu_walk_adds_to_an_existing_selection(driven_locks: Driven) -> None:
    """DL-196: a walk from the node menu ADDS the seed's closure to whatever
    was already selected; the label names the actual seed (the clicked node,
    not the selection) and the via-boxes qualifier follows the toggle."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _walk_fan_in_from_lk_x2(d)
    assert _selected_ids(d) == ["lk_box", "lk_seed", "lk_x1", "lk_x2", "lk_x5"], d.engine
    stats = d.page.inner_text("#stats")
    assert stats.endswith("5 selected · 0 highlighted · fan-in of lk_x2, transitive, via boxes"), (
        d.engine,
        stats,
    )


def test_hide_others_keeps_the_selection_then_show_all_restores_visibility(
    driven_locks: Driven,
) -> None:
    """Builds its own five-node selection (via `_walk_fan_in_from_lk_x2`):
    hide-others keeps the 5 selected plus the hubs their lock links reach;
    show-all restores visibility only -- the selection stays exactly what it
    was (DL-196: show all is visibility alone)."""
    d = driven_locks
    _ready(d)
    _walk_fan_in_from_lk_x2(d)
    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == sorted(
        ["lk_box", "lk_seed", "lk_x1", "lk_x2", "lk_x5", "lock:r:R_BIG", "lock:r:R_ONE"]
    ), d.engine
    stats = d.page.inner_text("#stats")
    assert (
        "visible 5 / 18 nodes · 3 / 5 edges · 9 locks · 5 selected · 0 highlighted"
        " · hide others (5 kept)" in stats
    ), (d.engine, stats)
    selection_before = _selected_ids(d)

    _click(d, "#show-all")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == selection_before, d.engine
    counts = _counts(d)
    assert counts["visible_nodes"] == 22, (d.engine, counts)
    assert d.page.inner_text("#stats").endswith("show all"), d.engine
    _click(d, "#clear-selection")


def test_edge_tap_inspects_without_touching_node_selection(driven_locks: Driven) -> None:
    """DL-196 slice 3: edges are unselectable (`cy.edges().unselectify()`),
    so a tap on one only marks the arrow under inspection; the node
    selection is untouched, and the next tap anywhere clears the mark."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    d.page.evaluate("() => { cy.edges().not('.lock')[0].emit('tap'); }")
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_x1"], d.engine
    assert d.page.evaluate("() => cy.edges('.inspected').length") == 1, d.engine
    assert d.page.evaluate("() => cy.edges(':selected').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_x1').emit('tap'); }")
    d.page.wait_for_timeout(200)
    assert d.page.evaluate("() => cy.edges('.inspected').length") == 0, d.engine
    _click(d, "#clear-selection")
    _click(d, "#d-close")  # both taps above opened the details panel


def test_a_highlighted_folded_member_shows_a_proxy_and_select_highlighted_expands_it(
    driven_locks: Driven,
) -> None:
    """DL-196: a highlighted folded member keeps `.hl`; its box gets
    `.hl-proxy` and the count reads "m highlighted (k not drawn)". Select
    highlighted expands the box to reach its target and adds it."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    _click(d, "#clear-highlights")
    d.page.evaluate("() => { cy.$id('lk_d').select(); }")
    _control_ready(d, "#highlight-selected")
    _click(d, "#highlight-selected")
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_box').hasClass('hl-proxy')"), d.engine
    stats = d.page.inner_text("#stats")
    assert "1 highlighted (1 not drawn) · highlighted 1" in stats, (d.engine, stats)

    _click(d, "#select-highlighted")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_d').length") == 1, f"{d.engine}: lk_box not expanded"
    assert _selected_ids(d) == ["lk_box", "lk_d"], d.engine
    assert d.page.inner_text("#stats").endswith("selected highlighted (1)"), d.engine

    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    _click(d, "#clear-highlights")
    d.page.evaluate("() => { ec.expandAll({ layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.nodes('.hl').length") == 0, d.engine
    assert d.page.evaluate("() => cy.nodes('.hl-proxy').length") == 0, d.engine
    _click(d, "#clear-selection")


def test_find_select_and_highlight_modes_cover_the_full_contract(driven_locks: Driven) -> None:
    """Enter selects, replacing; shift+Enter adds; `#find-highlight` adds to
    the highlight and leaves the selection alone; a hub match is skipped
    while the locks toggle is off, and `#stats` says so."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    _click(d, "#clear-highlights")
    if d.page.get_attribute("#details", "hidden") is None:
        _click(d, "#d-close")  # this test's own assertions depend on the panel starting shut

    d.page.fill("#search", "zzz")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(200)
    assert d.page.inner_text("#find-note") == "no match for “zzz”", d.engine

    d.page.uncheck("#locks")
    d.page.wait_for_timeout(300)
    d.page.fill("#search", "R_ONE")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == [], d.engine
    stats = d.page.inner_text("#stats")
    assert stats.endswith("find “R_ONE”: 1 match selected (1 lock skipped: locks off)"), (
        d.engine,
        stats,
    )
    assert d.page.get_attribute("#details", "hidden") is not None, d.engine

    # "highlight matches" honours the toggle too: the hub is skipped, never marked
    _click(d, "#clear-highlights")
    d.page.fill("#search", "R_ONE")
    _click(d, "#find-highlight")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert "lock:r:R_ONE" not in d.page.evaluate("() => cy.nodes('.hl').map(n => n.id())"), d.engine
    assert d.page.inner_text("#stats").endswith(
        "find “R_ONE”: 1 match highlighted (1 lock skipped: locks off)"
    ), (d.engine, d.page.inner_text("#stats"))
    _click(d, "#clear-highlights")

    d.page.check("#locks")
    d.page.wait_for_timeout(300)
    d.page.fill("#search", "R_ONE")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lock:r:R_ONE"], d.engine
    assert d.page.inner_text("#d-title") == "R_ONE", d.engine

    d.page.fill("#search", "lk_a")
    d.page.press("#search", "Shift+Enter")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_a", "lock:r:R_ONE"], d.engine

    d.page.fill("#search", "lk_x")
    selection_before = _selected_ids(d)
    _click(d, "#find-highlight")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == selection_before, d.engine
    assert sorted(d.page.evaluate("() => cy.nodes('.hl').map(n => n.id())")) == [
        "lk_x1",
        "lk_x2",
        "lk_x3",
        "lk_x4",
        "lk_x5",
    ], d.engine
    assert d.page.inner_text("#stats").endswith("find “lk_x”: 5 matches highlighted"), d.engine

    d.page.fill("#search", "")
    d.page.press("#search", "Enter")
    if d.page.get_attribute("#details", "hidden") is None:
        _click(d, "#d-close")
    _click(d, "#clear-selection")
    _click(d, "#clear-highlights")


def test_lock_members_and_lock_peers_expand_a_folded_box_to_reach_their_target(
    driven_locks: Driven,
) -> None:
    """DL-196: the explicit membership ops -- lock members, lock peers --
    expand a folded box to reach their target, unlike a walk. Each adds to
    whatever was already selected."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")

    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    point = _client_point(d, "lock:r:R_ONE")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    _click(d, "#lock-members")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_x1", "lk_x2"], d.engine
    assert d.page.evaluate("() => cy.$id('lk_x2').length") == 1, f"{d.engine}: lk_box not expanded"
    assert d.page.inner_text("#stats").endswith("members of R_ONE (2)"), d.engine

    _click(d, "#clear-selection")
    # "forgotten on the next tick" (the fold's own beforecollapse handler): give
    # the click's own unselect a tick to clear `justUnselected` before folding,
    # or the just-cleared lk_x2 reads as "just unselected by this collapse"
    d.page.wait_for_timeout(200)
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    point = _client_point(d, "lk_c")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    _click(d, "#lock-peers")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_c", "lk_d"], d.engine
    assert d.page.inner_text("#stats").endswith("lock peers of lk_c (1)"), d.engine

    _click(d, "#clear-selection")
    d.page.evaluate("() => { selectLockPeers(cy.$id('lk_x1')); }")
    assert _selected_ids(d) == ["lk_x1", "lk_x2"], d.engine
    _click(d, "#clear-selection")
    d.page.evaluate("() => { selectLockPeers(cy.$id('lk_e')); }")
    assert _selected_ids(d) == ["lk_e", "lk_f", "lk_g"], d.engine
    _click(d, "#clear-selection")


def test_hide_selected_unselects_and_stats_selected_fits_to_the_selection(
    driven_locks: Driven,
) -> None:
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    _control_ready(d, "#hide-selected")
    _click(d, "#hide-selected")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == [], d.engine
    assert d.page.evaluate("() => cy.$id('lk_a').visible()") is False, d.engine
    assert d.page.inner_text("#stats").endswith("hid 1"), d.engine
    _show_all(d)

    d.page.evaluate("() => { cy.$id('lk_b').select(); cy.zoom(4); }")
    _control_ready(d, "#stats-selected")
    _click(d, "#stats-selected")
    d.page.wait_for_timeout(300)
    zoom = d.page.evaluate("() => cy.zoom()")
    assert abs(zoom - 4) > 1e-6, f"{d.engine}: #stats-selected left the zoom at {zoom}"
    _click(d, "#clear-selection")


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
    assert rows["demand"] == "1 unit, never released", rows
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


def test_hide_selected_on_a_collapsed_box_hides_what_it_stands_for(driven_locks: Driven) -> None:
    """A rework fix: hiding a collapsed box must hide what it stands for, not
    just the box node itself -- a later find that expands the box and
    reveals one member must not also reveal its untouched sibling."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    d.page.evaluate("() => { cy.$id('lk_box').select(); }")
    _control_ready(d, "#hide-selected")
    _click(d, "#hide-selected")
    d.page.wait_for_timeout(_SETTLE_MS)
    d.page.fill("#search", "lk_x2")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_x2').visible()") is True, d.engine
    assert d.page.evaluate("() => cy.$id('lk_d').visible()") is False, d.engine
    assert d.page.evaluate("() => cy.$id('lk_d').hasClass('hidden')") is True, d.engine
    d.page.fill("#search", "")
    d.page.press("#search", "Enter")
    _click(d, "#show-all")
    d.page.wait_for_timeout(_SETTLE_MS)
    _click(d, "#clear-selection")


def test_the_locks_toggle_survives_a_fold(driven_locks: Driven) -> None:
    """A rework fix: a collapse gives the crossing lock links new meta-edges,
    which must carry the toggle's `lockoff` class too, through both a
    collapse and an expand."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    d.page.uncheck("#locks")
    d.page.wait_for_timeout(300)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    d.page.check("#locks")
    d.page.wait_for_timeout(300)
    assert d.page.evaluate("() => cy.elements('.lock').not(':visible').length") == 0, d.engine


def test_expand_removes_a_stale_highlight_proxy(driven_locks: Driven) -> None:
    """A rework fix: `.hl-proxy` must not survive the expand that makes it
    meaningless -- the box no longer stands for the highlighted member."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-highlights")
    d.page.evaluate("() => { highlightNodes(cy.$id('lk_d')); }")
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_box').hasClass('hl-proxy')"), d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert not d.page.evaluate("() => cy.$id('lk_box').hasClass('hl-proxy')"), d.engine
    _click(d, "#clear-highlights")


def test_a_collapsed_box_borrows_its_members_lock_links_for_the_menu(driven_locks: Driven) -> None:
    """A rework fix: `lk_box` folds a member's lock link into a meta-edge of
    its own, so while collapsed it carries `peered` (lock-peers reads it) --
    and loses the class again on expand."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_box').hasClass('peered')"), d.engine
    point = _client_point(d, "lk_box")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert d.page.evaluate(
        "() => getComputedStyle(document.getElementById('lock-peers')).display !== 'none'"
    ), d.engine
    d.page.mouse.click(10, 10)  # dismiss: background, nothing under test
    d.page.wait_for_timeout(200)
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert not d.page.evaluate("() => cy.$id('lk_box').hasClass('peered')"), d.engine


def test_stats_selected_button_keeps_keyboard_focus_across_a_rewrite(driven_locks: Driven) -> None:
    """A rework fix: the button is now created once and rewritten in place,
    so a keyboard user does not lose focus every time `#stats` updates."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    d.page.wait_for_timeout(200)
    d.page.locator("#stats-selected").focus()
    assert d.page.evaluate("() => document.activeElement.id") == "stats-selected", d.engine
    d.page.evaluate("() => { updateStats('probe'); }")
    d.page.wait_for_timeout(200)
    assert d.page.evaluate("() => document.activeElement.id") == "stats-selected", d.engine
    _click(d, "#clear-selection")


def test_find_note_clears_when_the_query_changes(driven_locks: Driven) -> None:
    """A rework fix: `#find-note` follows the query field, so a no-match note
    does not linger once the operator starts typing a different one."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    d.page.fill("#search", "zzz")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(200)
    assert d.page.inner_text("#find-note") != "", d.engine
    d.page.fill("#search", "lk_a")
    d.page.wait_for_timeout(200)
    assert d.page.inner_text("#find-note") == "", d.engine
    d.page.fill("#search", "")


def test_sel_lock_peers_button_adds_hub_members_and_job_peers(driven_locks: Driven) -> None:
    """DL-196: the toolbar's selection-seeded form of lock members / lock
    peers, one op over the whole selection. The seed label carries its own
    count, as the walks' does, so what the op found is said in words rather
    than a second parenthesis."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lock:r:R_ONE').select(); }")
    _control_ready(d, "#sel-lock-peers")
    _click(d, "#sel-lock-peers")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_x1", "lk_x2", "lock:r:R_ONE"], d.engine
    assert d.page.inner_text("#stats").endswith("lock peers of selection (1): 2 added"), d.engine

    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_c').select(); }")
    _control_ready(d, "#sel-lock-peers")
    _click(d, "#sel-lock-peers")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_c", "lk_d"], d.engine
    assert d.page.inner_text("#stats").endswith("lock peers of selection (1): 1 added"), d.engine
    _click(d, "#clear-selection")


def test_hide_selected_keeps_the_viewport_and_hide_others_fits(driven_locks: Driven) -> None:
    """DL-196's geometry rule, after the second review's correction: a hide
    op's own removal never moves the viewport by itself -- hide-selected
    leaves pan and zoom exactly where they were; hide-others fits to what it
    kept."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    _control_ready(d, "#hide-selected")
    view = _viewport(d)
    _click(d, "#hide-selected")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _viewport(d) == view, d.engine
    _show_all(d)

    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    view = _viewport(d)
    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _viewport(d) != view, d.engine
    _show_all(d)
    _click(d, "#clear-selection")


# --------------------------------------- DL-196 slice 4: marquee, Escape, disabled
#
# All of this block uses `driven_locks`: 18 jobs + 4 hubs = 22 nodes once
# everything is drawn. Every test restores the selection, the highlights and
# any class it added.


def test_a_plain_drag_on_the_background_pans_and_selects_nothing(driven_locks: Driven) -> None:
    """DL-196 THE MOUSE: plain drag pans, as on every map."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    view_before = _viewport(d)
    start = _canvas_point(d, 10, 10)  # inside the fit padding: guaranteed background
    end = _canvas_point(d, 32, 28)
    _drag(d, start, end)
    d.page.wait_for_timeout(200)
    assert _viewport(d)[:2] != view_before[:2], f"{d.engine}: the drag did not pan"
    assert _selected_ids(d) == [], d.engine
    d.page.evaluate(
        "(p) => { cy.pan({x: p[0], y: p[1]}); }", [view_before[0], view_before[1]]
    )  # restore: an exact pan set, no animation involved
    d.page.wait_for_timeout(200)


def test_shift_drag_box_around_the_whole_graph_selects_every_drawn_node(
    driven_locks: Driven,
) -> None:
    """DL-196: shift+drag adds a region; the marquee reaches every drawn node,
    hub or job, and takes no edge (edges are unselectify'd, DL-196), and pans
    nothing -- only a plain drag does that."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    view_before = _viewport(d)
    box = _rendered_bbox(d, "cy.nodes(':visible')")
    _drag(
        d,
        {"x": box["x1"] - 20, "y": box["y1"] - 20},
        {"x": box["x2"] + 20, "y": box["y2"] + 20},
        modifier="Shift",
    )
    d.page.wait_for_timeout(200)
    assert _counts(d)["selected"] == 22, (d.engine, _counts(d))
    assert d.page.evaluate("() => cy.edges(':selected').length") == 0, d.engine
    assert _viewport(d) == view_before, f"{d.engine}: the marquee panned the view"
    _click(d, "#clear-selection")


def test_shift_drag_box_around_one_node_adds_to_the_selection(driven_locks: Driven) -> None:
    """DL-196: the box ADDS -- with `lk_b` already selected, a box around
    `lk_h` alone leaves both."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_b').select(); }")
    box = _rendered_bbox(d, "cy.$id('lk_h')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Shift",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_b", "lk_h"], d.engine
    _click(d, "#clear-selection")


def test_a_hidden_node_is_immune_to_the_marquee(driven_locks: Driven) -> None:
    """DL-196: the marquee reaches drawn nodes only. `lk_h` is boxed exactly
    where it sits, but hidden first, so nothing is selected."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    box = _rendered_bbox(d, "cy.$id('lk_h')")
    d.page.evaluate("() => { cy.$id('lk_h').addClass('hidden'); }")
    d.page.wait_for_timeout(200)
    try:
        _drag(
            d,
            {"x": box["x1"] - 6, "y": box["y1"] - 6},
            {"x": box["x2"] + 6, "y": box["y2"] + 6},
            modifier="Shift",
        )
        d.page.wait_for_timeout(200)
        assert _selected_ids(d) == [], d.engine
    finally:
        d.page.evaluate("() => { cy.$id('lk_h').removeClass('hidden'); }")
    _click(d, "#clear-selection")


def test_marquee_modifier_accepts_control_and_meta_too(driven_locks: Driven) -> None:
    """DL-196's review of the bundle: the modifier set is shift, ctrl and cmd,
    and it is not configurable -- not shift alone."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    box = _rendered_bbox(d, "cy.$id('lk_r')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Control",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_r"], (d.engine, "Control")
    _click(d, "#clear-selection")

    box = _rendered_bbox(d, "cy.$id('lk_q')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Meta",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_q"], (d.engine, "Meta")
    _click(d, "#clear-selection")


def test_shift_drag_starting_on_a_node_does_nothing(driven_locks: Driven) -> None:
    """DL-196: the marquee starts on the background only -- a shift+drag that
    starts ON a node does nothing at all, which is why the rule needed no
    code of its own (the G5 verification ask). The node does not move and
    nothing is selected."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    before = _positions(d, ["lk_g"])["lk_g"]
    start = _client_point(d, "lk_g")
    _drag(d, start, {"x": start["x"] + 80, "y": start["y"] + 60}, modifier="Shift")
    d.page.wait_for_timeout(200)
    after = _positions(d, ["lk_g"])["lk_g"]
    assert after == before, (d.engine, before, after)
    assert _selected_ids(d) == [], d.engine


def test_marquee_replaces_the_selection_if_the_modifier_is_released_before_mouseup(
    driven_locks: Driven,
) -> None:
    """cytoscape's own rule (DL-196's review of the bundle): the box ADDS to
    the selection only while the modifier is still held at mouseup; released
    early, the same drag ends as a plain box that REPLACES it instead --
    `lk_b`, already selected, drops out."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_b').select(); }")
    box = _rendered_bbox(d, "cy.$id('lk_h')")
    d.page.keyboard.down("Shift")
    d.page.mouse.move(box["x1"] - 6, box["y1"] - 6)
    d.page.mouse.down()
    d.page.mouse.move(box["x2"] + 6, box["y2"] + 6, steps=8)
    d.page.keyboard.up("Shift")  # released BEFORE the button: this is the replace case
    d.page.mouse.up()
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_h"], d.engine
    _click(d, "#clear-selection")


def test_dragging_a_selected_node_moves_the_whole_selection(driven_locks: Driven) -> None:
    """DL-196: nodes stay grabbable, and a drag on a selected node moves the
    whole selection -- manual placement, undone only by a layout run. A real
    mouse drag of `lk_x3`; `lk_x4`'s model position moves by the same delta.
    The exact numbers depend on the fit zoom, so only equality and
    non-zero-ness are asserted, as the brief asks."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_x3').select(); cy.$id('lk_x4').select(); }")
    before = _positions(d, ["lk_x3", "lk_x4"])
    start = _client_point(d, "lk_x3")
    _drag(d, start, {"x": start["x"] + 120, "y": start["y"] + 60})
    d.page.wait_for_timeout(200)
    after = _positions(d, ["lk_x3", "lk_x4"])
    delta = {k: [after[k][0] - before[k][0], after[k][1] - before[k][1]] for k in before}
    assert delta["lk_x3"] == delta["lk_x4"], (d.engine, delta)
    assert delta["lk_x3"] != [0, 0], f"{d.engine}: the drag moved nothing"
    _click(d, "#clear-selection")


def test_selection_and_highlight_dependent_controls_track_the_layers(
    driven_locks: Driven,
) -> None:
    """DL-196: a control that reads the selection is disabled while nothing
    is selected, one that reads the highlight while nothing is marked, and
    `refreshControls` runs on every #stats rewrite. In order: at load (here,
    nothing selected or highlighted), after the marquee selects one node,
    after "highlight selected", after Escape clears the selection (the marks
    survive), after "clear highlights" with a selection still present."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    _click(d, "#clear-highlights")

    def assert_state(sel_disabled: bool, hl_disabled: bool, stage: str) -> None:
        sel = _disabled_map(d, _SELECTION_CONTROLS)
        hl = _disabled_map(d, _HIGHLIGHT_CONTROLS)
        stats_disabled = _disabled_map(d, ["stats-selected"])["stats-selected"]
        assert all(v == sel_disabled for v in sel.values()), (d.engine, stage, sel)
        assert all(v == hl_disabled for v in hl.values()), (d.engine, stage, hl)
        assert stats_disabled == sel_disabled, (d.engine, stage, "stats-selected")

    assert_state(True, True, "at load")

    box = _rendered_bbox(d, "cy.$id('lk_r')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Shift",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_r"], d.engine
    assert d.page.inner_text("#stats-selected") == "1 selected", d.engine
    assert_state(False, True, "after the marquee selects one node")

    _click(d, "#highlight-selected")
    d.page.wait_for_timeout(200)
    assert_state(False, False, "after highlight-selected")

    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == [], d.engine
    assert_state(True, False, "after Escape clears the selection")

    _click(d, "#clear-highlights")
    d.page.wait_for_timeout(200)
    assert_state(True, True, "after clear-highlights")


def test_canvas_menu_items_are_selection_dependent_too(driven_locks: Driven) -> None:
    """Codex review of slice 4: the four canvas-menu walks are the same ops as
    their toolbar twins, through the plugin's own enableMenuItem /
    disableMenuItem -- they start disabled at load and flip with the
    selection, same as every other selection-dependent control."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    assert _disabled_map(d, _CANVAS_MENU_WALKS) == dict.fromkeys(_CANVAS_MENU_WALKS, True), d.engine
    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    d.page.wait_for_timeout(200)
    assert _disabled_map(d, _CANVAS_MENU_WALKS) == dict.fromkeys(_CANVAS_MENU_WALKS, False), (
        d.engine
    )
    _click(d, "#clear-selection")


def test_escape_closes_a_menu_then_clears_a_focused_find_field_then_the_selection(
    driven_locks: Driven,
) -> None:
    """DL-196: Escape's three jobs, most local first. A menu open takes
    precedence over the find field, which takes precedence over the
    selection; dismissing a menu never touches the selection, and a second
    right-click reopens it."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")

    # 1. a menu open: Escape closes it and leaves the selection unchanged
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    point = _client_point(d, "lk_g")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert _menu_open(d), f"{d.engine}: the menu never opened"
    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert not _menu_open(d), f"{d.engine}: Escape did not close the menu"
    assert _selected_ids(d) == ["lk_a"], d.engine
    # ...and a second right-click opens it again
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert _menu_open(d), f"{d.engine}: the second right-click did not reopen the menu"
    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    _click(d, "#clear-selection")

    # 2. the find field focused and holding text: Escape empties it and
    #    leaves the selection unchanged
    d.page.evaluate("() => { cy.$id('lk_b').select(); }")
    d.page.fill("#search", "lk_x")
    d.page.evaluate("() => findField.focus()")
    assert d.page.evaluate("() => document.activeElement.id") == "search", d.engine
    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert d.page.input_value("#search") == "", d.engine
    assert _selected_ids(d) == ["lk_b"], d.engine
    _click(d, "#clear-selection")

    # 3. neither a menu nor a find field holding text: Escape clears the
    #    selection
    d.page.evaluate("() => { cy.$id('lk_c').select(); document.activeElement.blur(); }")
    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == [], d.engine


def test_escape_closes_a_menu_without_touching_a_focused_find_field(driven_locks: Driven) -> None:
    """Codex review of slice 4: Escape consumes exactly one layer per press.
    `preventDefault` stops webkit's own default action on Escape -- a search
    input clears itself natively -- which used to empty the find field in the
    same keystroke that closed the menu. Fill the field, focus it, open a
    menu, refocus the field, and one Escape must close the menu alone; a
    second Escape then empties the field."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.fill("#search", "lk_x")
    d.page.evaluate("() => findField.focus()")
    point = _client_point(d, "lk_g")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert _menu_open(d), f"{d.engine}: the menu never opened"
    d.page.evaluate("() => findField.focus()")  # the right-click may have moved focus
    assert d.page.evaluate("() => document.activeElement.id") == "search", d.engine

    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert not _menu_open(d), f"{d.engine}: Escape did not close the menu"
    assert d.page.input_value("#search") == "lk_x", (
        d.engine,
        "the field was emptied on the same Escape that closed the menu",
    )

    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(200)
    assert d.page.input_value("#search") == "", d.engine


def test_escape_during_a_held_background_click_clears_the_pending_menu_stash(
    driven_locks: Driven,
) -> None:
    """Codex review of slice 4, the obscure race: a background mousedown
    while a menu is open stashes the current selection (it is restored one
    tick after the tap completes, DL-196's menu-dismissal rule) -- and the
    menu itself is gone by the time Escape is checked, closed already as
    part of that same tapstart. Escape still finds a selection to act on and
    clears it, and now clears the pending stash too, so releasing the mouse
    does not put the selection back. Before the fix, the release did."""
    d = driven_locks
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    point = _client_point(d, "lk_g")
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    assert _menu_open(d), f"{d.engine}: the menu never opened"

    bg = _canvas_point(d, 10, 10)
    d.page.mouse.move(bg["x"], bg["y"])
    d.page.mouse.down()
    d.page.wait_for_timeout(100)
    d.page.keyboard.press("Escape")
    d.page.wait_for_timeout(100)
    d.page.mouse.up()
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == [], f"{d.engine}: the release put the cleared selection back"


def test_no_uncaught_page_errors_on_the_locks_page(driven_locks: Driven) -> None:
    _ready(driven_locks)
    assert driven_locks.errors == [], f"{driven_locks.engine}: {driven_locks.errors}"


# ------------------------- DL-191 rework: two OR attributes on one box (MAJOR)

_TWO_ATTR_TEXT = (
    "".join(
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n"
        for name in ("A", "B", "C", "D")
    )
    + "insert_job: BX\njob_type: b\ncondition: s(A) | s(B)\nbox_success: s(C) | s(D)\n"
)


@pytest.fixture(scope="module")
def two_attr_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A box whose START gate and whose COMPLETION override are both ORs.
    Both alternations number themselves from one, and they are not
    alternatives of one another."""
    path = tmp_path_factory.mktemp("explore-two-attr") / "two.html"
    path.write_text(to_explore_html(lower_source(_TWO_ATTR_TEXT), title="two attributes"))
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def _driven_two_attr(
    request: pytest.FixtureRequest, two_attr_page_url: str, _playwright: Any
) -> Any:
    yield from _open_driven(_playwright, request.param, two_attr_page_url)


@pytest.fixture
def driven_two_attr(_driven_two_attr: Driven) -> Driven:
    """See `driven`: reloads `_driven_two_attr`'s shared page before every test."""
    return _reset(_driven_two_attr)


def test_two_or_attributes_get_four_distinct_branch_colours(driven_two_attr: Driven) -> None:
    """Before the rework A and C shared `|1` and were painted one colour,
    which said they were alternatives of one another. They are not: A
    satisfies the box's start gate, C its completion override."""
    _ready(driven_two_attr)
    driven_two_attr.page.evaluate("() => { cy.$id('BX').emit('tap'); }")
    driven_two_attr.page.wait_for_timeout(300)
    painted = driven_two_attr.page.evaluate(
        "() => Object.fromEntries(cy.edges().filter(e => e.classes()"
        ".some(c => c.indexOf('br-') === 0)).map(e => [e.data('source'),"
        " e.classes().filter(c => c.indexOf('br-') === 0).join(',')]))"
    )
    assert painted == {"A": "br-0", "B": "br-1", "C": "br-2", "D": "br-3"}, driven_two_attr.engine
    # the panel shows two trees, four branch groups, four distinct swatches
    swatches = driven_two_attr.page.evaluate(
        "() => Array.from(document.querySelectorAll('#d-tree .swatch'))"
        ".map(s => [s.title, s.style.background])"
    )
    assert [t for t, _ in swatches] == ["branch |1", "branch |2", "branch |1", "branch |2"]
    assert len({colour for _, colour in swatches}) == 4, (driven_two_attr.engine, swatches)


def test_no_uncaught_page_errors_on_the_two_attribute_page(driven_two_attr: Driven) -> None:
    _ready(driven_two_attr)
    assert driven_two_attr.errors == [], f"{driven_two_attr.engine}: {driven_two_attr.errors}"


# ----------------- DL-75 rework slice 2: a lock member behind a box (MAJOR)

#: A resource hub whose three members sit in the three states a membership op
#: has to reach: `lb_flat` is drawn, `lb_deep` is two boxes deep, and
#: `lb_boxm` IS a box -- a box job can declare a resource, so a hub can name
#: one, and collapsed it stands on the canvas holding a non-member. The locks
#: corpus has neither shape: its one box is flat and no box is a member.
_LOCK_BOX_TEXT = """insert_resource: R_BOX
res_type: R
amount: 3

insert_job: lb_seed
job_type: c
command: /opt/lb_seed.sh
machine: m1
date_conditions: 1
days_of_week: all
start_times: "05:00"

insert_job: lb_boxm
job_type: b
condition: s(lb_seed)
resources: (R_BOX, QUANTITY=1)

insert_job: lb_kid
job_type: c
box_name: lb_boxm
command: /opt/lb_kid.sh
machine: m1

insert_job: lb_outer
job_type: b
condition: s(lb_seed)

insert_job: lb_inner
job_type: b
box_name: lb_outer

insert_job: lb_deep
job_type: c
box_name: lb_inner
command: /opt/lb_deep.sh
machine: m1
resources: (R_BOX, QUANTITY=1)

insert_job: lb_flat
job_type: c
command: /opt/lb_flat.sh
machine: m1
condition: s(lb_seed)
resources: (R_BOX, QUANTITY=1)
"""


@pytest.fixture(scope="module")
def lock_box_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("explore-lock-box") / "lockbox.html"
    path.write_text(to_explore_html(lower_source(_LOCK_BOX_TEXT), title="lock boxes"))
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def _driven_lock_box(
    request: pytest.FixtureRequest, lock_box_page_url: str, _playwright: Any
) -> Any:
    yield from _open_driven(_playwright, request.param, lock_box_page_url)


@pytest.fixture
def driven_lock_box(_driven_lock_box: Driven) -> Driven:
    """See `driven`: reloads `_driven_lock_box`'s shared page before every test.

    Not one of the brief's eight named fixtures (0f4470b added it the same
    day, after the brief's list was drafted); it has the identical
    module-scoped-fixture flaw, so it gets the identical fix rather than
    being left out."""
    return _reset(_driven_lock_box)


def _folded(d: Driven, node_id: str) -> bool:
    collapsed: bool = d.page.evaluate(
        "(id) => cy.$id(id).hasClass('cy-expand-collapse-collapsed-node')", node_id
    )
    return collapsed


def _lock_members_from_the_menu(d: Driven, hub: str) -> None:
    """The operator's own route to the op: right-click the hub, click the
    item. `placeLocks` has already run by here -- the caller waits out the
    fold -- so the hub is where the page last drew it."""
    point = _client_point(d, hub)
    d.page.mouse.click(point["x"], point["y"], button="right")
    d.page.wait_for_timeout(500)
    _click(d, "#lock-members")
    d.page.wait_for_timeout(_SETTLE_MS)


def test_lock_members_expands_a_member_that_is_itself_a_collapsed_box(
    driven_lock_box: Driven,
) -> None:
    """The member IS the box. Nothing of the hub's is folded inside it -- it
    holds `lb_kid`, which the hub does not name -- so a box is reached here
    only by matching its own id. Select lock members expands it, as the
    chain walk this slice deleted did."""
    d = driven_lock_box
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")

    d.page.evaluate("() => { ec.collapse(cy.$id('lb_boxm'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _folded(d, "lb_boxm"), f"{d.engine}: lb_boxm never folded"
    assert d.page.evaluate("() => cy.$id('lb_kid').length") == 0, d.engine

    _lock_members_from_the_menu(d, "lock:r:R_BOX")
    assert not _folded(d, "lb_boxm"), f"{d.engine}: the member box stayed folded"
    assert d.page.evaluate("() => cy.$id('lb_kid').length") == 1, d.engine
    assert _selected_ids(d) == ["lb_boxm", "lb_deep", "lb_flat"], d.engine
    assert d.page.inner_text("#stats").endswith("members of R_BOX (3)"), d.engine
    _click(d, "#clear-selection")


def test_lock_members_reaches_a_member_two_boxes_deep(driven_lock_box: Driven) -> None:
    """`lb_deep` sits in `lb_inner` sits in `lb_outer`. Collapsing the outer
    box takes both out of the graph, so the box that has to be expanded is
    two levels above the member the hub names."""
    d = driven_lock_box
    _ready(d)
    _show_all(d)
    _click(d, "#clear-selection")

    d.page.evaluate("() => { ec.collapse(cy.$id('lb_outer'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _folded(d, "lb_outer"), f"{d.engine}: lb_outer never folded"
    assert d.page.evaluate("() => cy.$id('lb_deep').length") == 0, d.engine
    assert d.page.evaluate("() => cy.$id('lb_inner').length") == 0, d.engine

    _lock_members_from_the_menu(d, "lock:r:R_BOX")
    assert not _folded(d, "lb_outer"), f"{d.engine}: the outer box stayed folded"
    assert d.page.evaluate("() => cy.$id('lb_deep').length") == 1, d.engine
    assert _selected_ids(d) == ["lb_boxm", "lb_deep", "lb_flat"], d.engine
    assert d.page.inner_text("#stats").endswith("members of R_BOX (3)"), d.engine
    _click(d, "#clear-selection")


def test_no_uncaught_page_errors_on_the_lock_box_page(driven_lock_box: Driven) -> None:
    _ready(driven_lock_box)
    assert driven_lock_box.errors == [], f"{driven_lock_box.engine}: {driven_lock_box.errors}"
