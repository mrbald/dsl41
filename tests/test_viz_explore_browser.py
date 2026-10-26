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

import json
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
from test_viz_explore import S_EDGE_TEXT, _COND_TEXT, _NESTED_BOX_TEXT

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
    d.page.set_viewport_size({"width": 1600, "height": 1000})
    d.page.reload()
    _ready(d)
    return d


def _open_control(d: Driven, selector: str) -> str | None:
    """Open the HTML panel that owns a control, through its real disclosure."""
    panel: str | None = d.page.locator(selector).evaluate(
        "el => el.closest('.chrome-panel')?.id || null"
    )
    if panel and not d.page.locator(f"#{panel}").is_visible():
        d.page.locator(f'[aria-controls="{panel}"]').click(timeout=_CLICK_TIMEOUT_MS)
    return panel


def _click(d: Driven, selector: str) -> None:
    """Legacy graph tests use a control and put its panel away before the
    next canvas gesture. New disclosure tests leave panels open explicitly."""
    panel = _open_control(d, selector)
    d.page.locator(selector).click(timeout=_CLICK_TIMEOUT_MS)
    if panel and d.page.locator(f"#{panel}").is_visible():
        d.page.locator(f"#{panel} [data-close-panel]").click(timeout=_CLICK_TIMEOUT_MS)


def _clear_selection(d: Driven) -> None:
    """An empty precondition needs no click on its disabled clear control."""
    if _selected_ids(d):
        _click(d, "#clear-selection")


def _clear_highlights(d: Driven) -> None:
    if d.page.evaluate("() => highlighted().nonempty()"):
        _click(d, "#clear-highlights")


def _set_checked(d: Driven, selector: str, checked: bool) -> None:
    panel = _open_control(d, selector)
    d.page.locator(selector).set_checked(checked, timeout=_CLICK_TIMEOUT_MS)
    if panel:
        d.page.locator(f"#{panel} [data-close-panel]").click(timeout=_CLICK_TIMEOUT_MS)


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
    """Click the page's own "show all" control and wait for the settle. Not a
    reset between tests -- that is `_reset`'s reload, which every test gets
    for free -- but the "show all" button itself, for the three tests that
    exercise what it actually does: restore visibility while keeping the
    selection and the highlights (DL-196), which a mid-test hide or focus
    needs undone before the rest of that same test can proceed."""
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
    _clear_selection(driven)
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
    _clear_selection(driven)


def test_search_unhides_a_node_hidden_by_a_focus(driven: Driven) -> None:
    """The template's stated contract: a search that cannot find a hidden node
    is a lying search."""
    _ready(driven)
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
    _clear_selection(driven)


def test_show_all_restores_every_element_and_keeps_selection_and_highlights(
    driven: Driven,
) -> None:
    """DL-196 slice 3: "show all" is visibility only. The old name here
    ("...and_clears_highlights") described a fact that is now false: the
    selection and the highlights both survive it."""
    _ready(driven)
    _clear_selection(driven)
    _clear_highlights(driven)
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
    _clear_selection(driven)
    _clear_highlights(driven)


def test_relayout_toggle_off_leaves_leaf_positions_alone(driven: Driven) -> None:
    """ "Arrange after hiding" is OFF (DL-196: the preservation principle -- a
    node moves only when a layout runs, and the button is one click away), so
    a focus hides, places the hubs and fits, and moves nothing. The toggle is
    unchecked unless a test checked it, and every test that checks it
    unchecks it again in a `finally`, so the assertion below reads the page's
    own default."""
    _ready(driven)
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


def test_relayout_toggle_on_re_lays_out_after_a_focus(driven: Driven) -> None:
    """The mirror of the test above, and the toggle's whole job: switched on,
    a focus runs ELK over what is left. Restores the default -- off -- and
    the whole graph."""
    _ready(driven)
    sample = _sample_node(driven)
    before = driven.page.evaluate(_LEAF_POSITIONS)
    _set_checked(driven, "#relayout", True)
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
        _set_checked(driven, "#relayout", False)  # the page's default, for every test after this


def test_details_panel_opens_for_a_node_and_for_an_edge(driven: Driven) -> None:
    _ready(driven)
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
    _clear_selection(driven)
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
    _clear_selection(driven)


def test_dismissing_the_menu_with_a_background_click_leaves_the_selection_intact(
    driven: Driven,
) -> None:
    """DL-196's own verification ask: the plugin closes on the tapstart of an
    outside click, and cytoscape then completes that click as a background
    tap that would otherwise clear the selection; dismissing on a NODE is a
    click and replaces it -- this pins only the background case, real mouse,
    three engines."""
    _ready(driven)
    _clear_selection(driven)
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
    _clear_selection(driven)


def test_ctrl_click_on_a_node_leaves_the_selection_unchanged_either_way(driven: Driven) -> None:
    """DL-196's webkit verification ask: does ctrl+click on a node open the
    context menu (the Mac convention) or select it? Either is acceptable --
    what must hold is that it does not leave the selection in a surprising
    state. The actual outcome per engine is reported alongside this slice's
    test report, not asserted here as one fixed behaviour."""
    _ready(driven)
    _clear_selection(driven)
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
    _clear_selection(driven)


def test_no_uncaught_page_errors(driven: Driven) -> None:
    """Last: every error the whole session threw. A control that "works" while
    throwing is not working -- and DL-77's throw was silent."""
    _ready(driven)
    assert driven.errors == [], f"{driven.engine}: {driven.errors}"


# ---------------------------------------------------- DL-190: collapse and trace
#
# `driven_trace` shares one `trace_page_url` load per engine across every test
# below that takes it (`_driven_trace` is the module-scoped page; `driven_trace`
# reloads it before each test, DL-201). A mutating test restores what it
# changed before it returns, or -- like `_collapse_b`'s three callers -- builds
# its own precondition rather than relying on what an earlier test left.


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
    assert driven_trace.page.evaluate("() => foldedMembers(cy.$id('B'))") == 4, driven_trace.engine
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
    path rather than a vacuous one. The search's own expand leaves the page
    fully expanded already, so nothing further needs restoring."""
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
    _clear_selection(driven_trace)


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
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('IM').select(); }")
    d.page.evaluate("() => { ec.collapse(cy.$id('B'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["B"], d.engine
    d.page.evaluate("() => { ec.expand(cy.$id('B'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["B"], d.engine
    assert d.page.evaluate("() => cy.$id('IM').selected()") is False, d.engine
    _clear_selection(d)


def test_sel_fan_in_tree_from_two_seeds_matches_the_union_of_each_seeds_tree(
    driven_trace: Driven,
) -> None:
    """DL-196's own verification ask: a two-seed transitive walk under
    trace-through-boxes reaches exactly the union of what each seed reaches
    alone, the gate/producer promotion (fanInTree's `requeue`) included."""
    d = driven_trace
    _ready(d)
    _clear_selection(d)
    expected = sorted(set(_tree_ids(d, "C", "fanInTree")) | set(_tree_ids(d, "D", "fanInTree")))
    d.page.evaluate("() => { cy.$id('C').select(); cy.$id('D').select(); }")
    _control_ready(d, "#sel-fan-in-tree")
    _click(d, "#sel-fan-in-tree")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == expected, d.engine
    _clear_selection(d)


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
    d.page.evaluate("() => { cy.$id('B').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _visible_ids(d) == ["B", "C", "D", "P", "Q"], d.engine
    _clear_selection(d)
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

    _clear_selection(d)
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
# like the DL-190 block above (`_driven_cond` is module-scoped; `driven_cond`
# reloads it before each test, DL-201). Only the collapse test mutates the
# graph, and it expands again before it returns.

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
    label: str = d.page.evaluate(
        "(id) => { const svg = decodeURIComponent(nodeMarks(cy.$id(id)).split(',')[1]);"
        " return new DOMParser().parseFromString(svg, 'image/svg+xml').documentElement.textContent; }",
        node_id,
    )
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
        "AOA": "\u2228",
        "ANY": "\u2228",
        "CPX": "\u2228*",
        "PLAIN": "",
        "BOX": "\u2228",
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
    of offering a link to an element that is not drawn, and stops saying so
    once the arrow is back."""
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
    # Multiple hits expose exact full-name choices in details (DL-384).
    driven_cond.page.evaluate("() => { cy.emit('tap'); }")
    driven_cond.page.fill("#search", "ME")
    driven_cond.page.press("#search", "Enter")
    driven_cond.page.get_by_role("heading", name="Find matches (2)", exact=True).wait_for(
        state="visible"
    )
    for name in ("MEM", "MEM2"):
        assert driven_cond.page.get_by_role(
            "button", name="Select and inspect " + name, exact=True
        ).is_visible(), driven_cond.engine
    assert _selected_ids(driven_cond) == ["MEM", "MEM2"]
    driven_cond.page.fill("#search", "")
    driven_cond.page.press("#search", "Enter")
    _clear_selection(driven_cond)


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

    assert _label(driven_cond, "BOX") == "\u22282", driven_cond.engine
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
    assert _label(driven_cond, "BOX") == "\u2228", driven_cond.engine


def test_no_uncaught_page_errors_on_the_condition_page(driven_cond: Driven) -> None:
    _ready(driven_cond)
    assert driven_cond.errors == [], f"{driven_cond.engine}: {driven_cond.errors}"


# ------------------------------------------------------------- DL-192: locks
#
# `driven_locks` shares one page per engine across this block, like the two
# above (`_driven_locks` is module-scoped; `driven_locks` reloads it before
# each test, DL-201, so a test that mutates the graph no longer needs to
# restore it for the tests after it -- only the ones still below actually
# need the restored state for their own remaining assertions).


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
    `finally`. Leaves the focus in place; the next test's own reload
    restores full visibility."""
    _ready(driven_locks)
    _set_checked(driven_locks, "#relayout", True)
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
        _set_checked(driven_locks, "#relayout", False)  # the page's default


def test_hubs_stay_clear_of_the_jobs_after_a_collapse(driven_locks: Driven) -> None:
    """A collapse takes members out and leaves the box: the hubs are placed
    again against the new picture. DL-192's property, kept under DL-196 --
    the fold itself runs no layout now, and placeLocks runs from the fold's
    own handler instead of from a layout's. Expands before returning."""
    _ready(driven_locks)
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
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_d').select(); }")
    d.page.evaluate("() => { ec.collapse(cy.$id('lk_box'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_box"], d.engine
    d.page.evaluate("() => { ec.expand(cy.$id('lk_box'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_box"], d.engine
    assert d.page.evaluate("() => cy.$id('lk_d').selected()") is False, d.engine
    _clear_selection(d)


def test_hide_others_collapses_a_selected_box_with_no_selected_member(
    driven_locks: Driven,
) -> None:
    """DL-196: a selected box with no selected descendant is folded, not
    emptied -- it stands for its members and no sibling is drawn. The hub
    context is read off the box after the fold: R_ONE reaches through the
    now-folded lk_x2, R_BIG does not (lk_d's pair link is not a hub)."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
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
    _clear_selection(d)


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
    assert driven_locks.page.evaluate(
        "() => decodeURIComponent(nodeMarks(cy.$id('lk_h'))).includes('<rect')"
    )


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
        "lk_x1 · 1 unit, released on success",  # no FREE: the vendor default (DL-256)
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
    _set_checked(driven_locks, "#locks", False)
    driven_locks.page.wait_for_timeout(300)
    assert driven_locks.page.evaluate("() => cy.elements('.lock:visible').length") == 0
    # ...and the jobs are all still there: the toggle hides locks alone
    assert driven_locks.page.evaluate("() => cy.nodes(':visible').length") == 18
    _set_checked(driven_locks, "#locks", True)
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


def test_lock_members_then_hide_others_shows_the_hub_and_every_member(driven_locks: Driven) -> None:
    """DL-196 declines a dedicated "focus lock" item ("select members, then
    hide others" loses nothing): a real right-click on the hub for the first
    step, the toolbar for the second."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
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
    _clear_selection(d)


def test_node_menu_offers_walks_and_lock_items_by_node_kind(driven_locks: Driven) -> None:
    """DL-196 slice 3: the six flow items plus lock-peers sit only on
    `node[!lock]`, and lock-peers only where a lock link exists; a hub
    (`node.lock`) offers `lock-members` and `hide-lock` instead, never a
    flow item. Restores the page by dismissing each menu with a background
    click."""
    d = driven_locks
    _ready(d)
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
    _clear_selection(d)
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
    _clear_selection(d)


def _walk_fan_in_from_lk_x2(d: Driven) -> None:
    """Clear the selection, select lk_x1 and lk_x5, then right-click lk_x2 and
    walk its fan-in tree from the node menu -- the five-node selection
    (lk_box, lk_seed, lk_x1, lk_x2, lk_x5) that
    `test_node_menu_walk_adds_to_an_existing_selection` and
    `test_hide_others_keeps_the_selection_then_show_all_restores_visibility`
    each need as their own precondition, factored out so neither inherits it
    from the other."""
    _clear_selection(d)
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
    _clear_selection(d)


def test_edge_tap_inspects_without_touching_node_selection(driven_locks: Driven) -> None:
    """DL-196 slice 3: edges are unselectable (`cy.edges().unselectify()`),
    so a tap on one only marks the arrow under inspection; the node
    selection is untouched, and the next tap anywhere clears the mark."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    d.page.evaluate("() => { cy.edges().not('.lock')[0].emit('tap'); }")
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_x1"], d.engine
    assert d.page.evaluate("() => cy.edges('.inspected').length") == 1, d.engine
    assert d.page.evaluate("() => cy.edges(':selected').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_x1').emit('tap'); }")
    d.page.wait_for_timeout(200)
    assert d.page.evaluate("() => cy.edges('.inspected').length") == 0, d.engine
    _clear_selection(d)
    _click(d, "#d-close")  # both taps above opened the details panel


def test_a_highlighted_folded_member_shows_a_proxy_and_select_highlighted_expands_it(
    driven_locks: Driven,
) -> None:
    """DL-196: a highlighted folded member keeps `.hl`; its box gets
    `.hl-proxy` and the count reads "m highlighted (k not drawn)". Select
    highlighted expands the box to reach its target and adds it."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    _clear_highlights(d)
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
    _clear_highlights(d)
    d.page.evaluate("() => { ec.expandAll({ layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.nodes('.hl').length") == 0, d.engine
    assert d.page.evaluate("() => cy.nodes('.hl-proxy').length") == 0, d.engine
    _clear_selection(d)


def test_find_select_and_highlight_modes_cover_the_full_contract(driven_locks: Driven) -> None:
    """Enter selects, replacing; shift+Enter adds; `#find-highlight` adds to
    the highlight and leaves the selection alone; a hub match is skipped
    while the locks toggle is off, and `#stats` says so."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    _clear_highlights(d)
    if d.page.get_attribute("#details", "hidden") is None:
        _click(d, "#d-close")  # this test's own assertions depend on the panel starting shut

    d.page.fill("#search", "zzz")
    d.page.press("#search", "Enter")
    d.page.wait_for_timeout(200)
    assert d.page.inner_text("#find-note") == "no match for “zzz”", d.engine

    _set_checked(d, "#locks", False)
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
    _clear_highlights(d)
    d.page.fill("#search", "R_ONE")
    _click(d, "#find-highlight")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert "lock:r:R_ONE" not in d.page.evaluate("() => cy.nodes('.hl').map(n => n.id())"), d.engine
    assert d.page.inner_text("#stats").endswith(
        "find “R_ONE”: 1 match highlighted (1 lock skipped: locks off)"
    ), (d.engine, d.page.inner_text("#stats"))
    _clear_highlights(d)

    _set_checked(d, "#locks", True)
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
    _clear_selection(d)
    _clear_highlights(d)


def test_lock_members_and_lock_peers_expand_a_folded_box_to_reach_their_target(
    driven_locks: Driven,
) -> None:
    """DL-196: the explicit membership ops -- lock members, lock peers --
    expand a folded box to reach their target, unlike a walk. Each adds to
    whatever was already selected."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)

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

    _clear_selection(d)
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

    _clear_selection(d)
    d.page.evaluate("() => { selectLockPeers(cy.$id('lk_x1')); }")
    assert _selected_ids(d) == ["lk_x1", "lk_x2"], d.engine
    _clear_selection(d)
    d.page.evaluate("() => { selectLockPeers(cy.$id('lk_e')); }")
    assert _selected_ids(d) == ["lk_e", "lk_f", "lk_g"], d.engine
    _clear_selection(d)


def test_hide_selected_unselects_and_stats_selected_fits_to_the_selection(
    driven_locks: Driven,
) -> None:
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    _control_ready(d, "#hide-selected")
    _click(d, "#hide-selected")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == [], d.engine
    assert d.page.evaluate("() => cy.$id('lk_a').visible()") is False, d.engine
    assert d.page.inner_text("#stats").endswith("hid 1"), d.engine

    d.page.evaluate("() => { cy.$id('lk_b').select(); cy.zoom(4); }")
    _control_ready(d, "#stats-selected")
    _click(d, "#stats-selected")
    d.page.wait_for_timeout(300)
    zoom = d.page.evaluate("() => cy.zoom()")
    assert abs(zoom - 4) > 1e-6, f"{d.engine}: #stats-selected left the zoom at {zoom}"
    _clear_selection(d)


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
    _clear_selection(d)
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
    _clear_selection(d)


def test_the_locks_toggle_survives_a_fold(driven_locks: Driven) -> None:
    """A rework fix: a collapse gives the crossing lock links new meta-edges,
    which must carry the toggle's `lockoff` class too, through both a
    collapse and an expand."""
    d = driven_locks
    _ready(d)
    _set_checked(d, "#locks", False)
    d.page.wait_for_timeout(300)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.elements('.lock:visible').length") == 0, d.engine
    _set_checked(d, "#locks", True)
    d.page.wait_for_timeout(300)
    assert d.page.evaluate("() => cy.elements('.lock').not(':visible').length") == 0, d.engine


def test_expand_removes_a_stale_highlight_proxy(driven_locks: Driven) -> None:
    """A rework fix: `.hl-proxy` must not survive the expand that makes it
    meaningless -- the box no longer stands for the highlighted member."""
    d = driven_locks
    _ready(d)
    _clear_highlights(d)
    d.page.evaluate("() => { highlightNodes(cy.$id('lk_d')); }")
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert d.page.evaluate("() => cy.$id('lk_box').hasClass('hl-proxy')"), d.engine
    d.page.evaluate("() => { cy.$id('lk_box').emit('dbltap'); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert not d.page.evaluate("() => cy.$id('lk_box').hasClass('hl-proxy')"), d.engine
    _clear_highlights(d)


def test_a_collapsed_box_borrows_its_members_lock_links_for_the_menu(driven_locks: Driven) -> None:
    """A rework fix: `lk_box` folds a member's lock link into a meta-edge of
    its own, so while collapsed it carries `peered` (lock-peers reads it) --
    and loses the class again on expand."""
    d = driven_locks
    _ready(d)
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
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    d.page.wait_for_timeout(200)
    d.page.locator("#stats-selected").focus()
    assert d.page.evaluate("() => document.activeElement.id") == "stats-selected", d.engine
    d.page.evaluate("() => { updateStats('probe'); }")
    d.page.wait_for_timeout(200)
    assert d.page.evaluate("() => document.activeElement.id") == "stats-selected", d.engine
    _clear_selection(d)


def test_find_note_clears_when_the_query_changes(driven_locks: Driven) -> None:
    """A rework fix: `#find-note` follows the query field, so a no-match note
    does not linger once the operator starts typing a different one."""
    d = driven_locks
    _ready(d)
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
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lock:r:R_ONE').select(); }")
    _control_ready(d, "#sel-lock-peers")
    _click(d, "#sel-lock-peers")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_x1", "lk_x2", "lock:r:R_ONE"], d.engine
    assert d.page.inner_text("#stats").endswith("lock peers of selection (1): 2 added"), d.engine

    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_c').select(); }")
    _control_ready(d, "#sel-lock-peers")
    _click(d, "#sel-lock-peers")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _selected_ids(d) == ["lk_c", "lk_d"], d.engine
    assert d.page.inner_text("#stats").endswith("lock peers of selection (1): 1 added"), d.engine
    _clear_selection(d)


def test_hide_selected_keeps_the_viewport_and_hide_others_fits(driven_locks: Driven) -> None:
    """DL-196's geometry rule, after the second review's correction: a hide
    op's own removal never moves the viewport by itself -- hide-selected
    leaves pan and zoom exactly where they were; hide-others fits to what it
    kept."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_a').select(); }")
    _control_ready(d, "#hide-selected")
    view = _viewport(d)
    _click(d, "#hide-selected")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _viewport(d) == view, d.engine

    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    view = _viewport(d)
    _click(d, "#hide-others")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _viewport(d) != view, d.engine
    _clear_selection(d)


# --------------------------------------- DL-196 slice 4: marquee, Escape, disabled
#
# All of this block uses `driven_locks`: 18 jobs + 4 hubs = 22 nodes once
# everything is drawn. Every test restores the selection, the highlights and
# any class it added.


def test_a_plain_drag_on_the_background_pans_and_selects_nothing(driven_locks: Driven) -> None:
    """DL-196 THE MOUSE: plain drag pans, as on every map."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
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
    _clear_selection(d)
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
    _clear_selection(d)


def test_shift_drag_box_around_one_node_adds_to_the_selection(driven_locks: Driven) -> None:
    """DL-196: the box ADDS -- with `lk_b` already selected, a box around
    `lk_h` alone leaves both."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
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
    _clear_selection(d)


def test_a_hidden_node_is_immune_to_the_marquee(driven_locks: Driven) -> None:
    """DL-196: the marquee reaches drawn nodes only. `lk_h` is boxed exactly
    where it sits, but hidden first, so nothing is selected."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    box = _rendered_bbox(d, "cy.$id('lk_h')")
    # The marquee reads the drawn-elements cache that cytoscape's next frame
    # refreshes after a class change; drag only once that frame is drawn.
    d.page.evaluate(
        "() => new Promise(done => { cy.one('render', () => done());"
        " cy.$id('lk_h').addClass('hidden'); })"
    )
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
    _clear_selection(d)


def test_marquee_modifier_accepts_control_and_meta_too(driven_locks: Driven) -> None:
    """DL-196's review of the bundle: the modifier set is shift, ctrl and cmd,
    and it is not configurable -- not shift alone."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    box = _rendered_bbox(d, "cy.$id('lk_r')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Control",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_r"], (d.engine, "Control")
    _clear_selection(d)

    box = _rendered_bbox(d, "cy.$id('lk_q')")
    _drag(
        d,
        {"x": box["x1"] - 6, "y": box["y1"] - 6},
        {"x": box["x2"] + 6, "y": box["y2"] + 6},
        modifier="Meta",
    )
    d.page.wait_for_timeout(200)
    assert _selected_ids(d) == ["lk_q"], (d.engine, "Meta")
    _clear_selection(d)


def test_shift_drag_starting_on_a_node_does_nothing(driven_locks: Driven) -> None:
    """DL-196: the marquee starts on the background only -- a shift+drag that
    starts ON a node does nothing at all, which is why the rule needed no
    code of its own (the G5 verification ask). The node does not move and
    nothing is selected."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
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
    _clear_selection(d)
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
    _clear_selection(d)


def test_dragging_a_selected_node_moves_the_whole_selection(driven_locks: Driven) -> None:
    """DL-196: nodes stay grabbable, and a drag on a selected node moves the
    whole selection -- manual placement, undone only by a layout run. A real
    mouse drag of `lk_x3`; `lk_x4`'s model position moves by the same delta.
    The exact numbers depend on the fit zoom, so only equality and
    non-zero-ness are asserted, as the brief asks."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    d.page.evaluate("() => { cy.$id('lk_x3').select(); cy.$id('lk_x4').select(); }")
    before = _positions(d, ["lk_x3", "lk_x4"])
    start = _client_point(d, "lk_x3")
    _drag(d, start, {"x": start["x"] + 120, "y": start["y"] + 60})
    d.page.wait_for_timeout(200)
    after = _positions(d, ["lk_x3", "lk_x4"])
    delta = {k: [after[k][0] - before[k][0], after[k][1] - before[k][1]] for k in before}
    assert delta["lk_x3"] == delta["lk_x4"], (d.engine, delta)
    assert delta["lk_x3"] != [0, 0], f"{d.engine}: the drag moved nothing"
    _clear_selection(d)


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
    _clear_selection(d)
    _clear_highlights(d)

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

    _clear_highlights(d)
    d.page.wait_for_timeout(200)
    assert_state(True, True, "after clear-highlights")


def test_canvas_menu_items_are_selection_dependent_too(driven_locks: Driven) -> None:
    """Codex review of slice 4: the four canvas-menu walks are the same ops as
    their toolbar twins, through the plugin's own enableMenuItem /
    disableMenuItem -- they start disabled at load and flip with the
    selection, same as every other selection-dependent control."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)
    assert _disabled_map(d, _CANVAS_MENU_WALKS) == dict.fromkeys(_CANVAS_MENU_WALKS, True), d.engine
    d.page.evaluate("() => { cy.$id('lk_x1').select(); }")
    d.page.wait_for_timeout(200)
    assert _disabled_map(d, _CANVAS_MENU_WALKS) == dict.fromkeys(_CANVAS_MENU_WALKS, False), (
        d.engine
    )
    _clear_selection(d)


def test_escape_closes_a_menu_then_clears_a_focused_find_field_then_the_selection(
    driven_locks: Driven,
) -> None:
    """DL-196: Escape's three jobs, most local first. A menu open takes
    precedence over the find field, which takes precedence over the
    selection; dismissing a menu never touches the selection, and a second
    right-click reopens it."""
    d = driven_locks
    _ready(d)
    _clear_selection(d)

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
    _clear_selection(d)

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
    _clear_selection(d)

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
    _clear_selection(d)
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
    _clear_selection(d)
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
    _clear_selection(d)

    d.page.evaluate("() => { ec.collapse(cy.$id('lb_boxm'), { layoutBy: null }); }")
    d.page.wait_for_timeout(_SETTLE_MS)
    assert _folded(d, "lb_boxm"), f"{d.engine}: lb_boxm never folded"
    assert d.page.evaluate("() => cy.$id('lb_kid').length") == 0, d.engine

    _lock_members_from_the_menu(d, "lock:r:R_BOX")
    assert not _folded(d, "lb_boxm"), f"{d.engine}: the member box stayed folded"
    assert d.page.evaluate("() => cy.$id('lb_kid').length") == 1, d.engine
    assert _selected_ids(d) == ["lb_boxm", "lb_deep", "lb_flat"], d.engine
    assert d.page.inner_text("#stats").endswith("members of R_BOX (3)"), d.engine
    _clear_selection(d)


def test_lock_members_reaches_a_member_two_boxes_deep(driven_lock_box: Driven) -> None:
    """`lb_deep` sits in `lb_inner` sits in `lb_outer`. Collapsing the outer
    box takes both out of the graph, so the box that has to be expanded is
    two levels above the member the hub names."""
    d = driven_lock_box
    _ready(d)
    _clear_selection(d)

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
    _clear_selection(d)


def test_no_uncaught_page_errors_on_the_lock_box_page(driven_lock_box: Driven) -> None:
    _ready(driven_lock_box)
    assert driven_lock_box.errors == [], f"{driven_lock_box.engine}: {driven_lock_box.errors}"


# ------------------------------------------------------ DL-204 compact controls


def test_edge_style_switch_preserves_geometry_and_graph_state(driven_locks: Driven) -> None:
    """DL-381: routing changes through the labelled control keep graph state and lock grammar."""
    d = driven_locks
    _tap(d, "lk_a")
    d.page.evaluate(
        "() => { highlightNodes(cy.$id('lk_b')); cy.$id('lk_q').addClass('hidden');"
        " cy.edges().not('.lock')[0].addClass('inspected');"
        " cy.zoom(1.2); cy.pan({x: 37, y: 59});"
        " window.routingLayouts = 0; cy.on('layoutstart', () => { routingLayouts++; }); }"
    )
    _open_control(d, "#edge-style")
    control = d.page.get_by_label("Edge style", exact=True)
    assert control.input_value() == "taxi", d.engine
    snapshot = (
        "() => ({nodes: cy.nodes().map(n => [n.id(), n.position(), n.selected()]),"
        " classes: cy.elements().map(e => [e.id(), e.classes()]),"
        " pan: cy.pan(), zoom: cy.zoom(), rules: cy.style().length,"
        " details: document.getElementById('details').innerHTML})"
    )
    before = d.page.evaluate(snapshot)
    for style in ("bezier", "taxi", "bezier", "taxi"):
        control.select_option(style)
        curves = d.page.evaluate(
            "() => ({flow: [...new Set(cy.edges().not('.lock, .nesting')"
            ".map(e => e.style('curve-style')))],"
            " locks: [...new Set(cy.edges('.lock').map(e => e.style('curve-style')))]})"
        )
        assert curves == {"flow": [style], "locks": ["straight"]}, (d.engine, curves)
        assert d.page.evaluate(snapshot) == before, (d.engine, style)
        assert d.page.evaluate("() => routingLayouts") == 0, d.engine
    control.focus()
    d.page.keyboard.press("Shift+Tab")
    d.page.keyboard.press("Tab")
    assert d.page.evaluate("() => document.activeElement.id") == "edge-style", d.engine
    assert d.errors == [], (d.engine, d.errors)


def test_edge_style_follows_fold_restore_and_arrange(driven_cond: Driven) -> None:
    """DL-381: new and restored edges follow the choice without losing branch annotations."""
    d = driven_cond
    _tap(d, "MEM")
    painted = _painted(d)
    assert painted, d.engine
    grammar = (
        "() => cy.edges().map(e => [e.id(), e.style('line-style'),"
        " e.style('line-color'), e.style('target-arrow-fill'), e.style('label')])"
    )
    before = d.page.evaluate(grammar)
    _open_control(d, "#edge-style")
    control = d.page.get_by_label("Edge style", exact=True)
    control.select_option("bezier")
    assert _painted(d) == painted, d.engine
    assert d.page.evaluate(grammar) == before, d.engine
    _click(d, "#collapse-all")
    d.page.wait_for_function("() => cy.edges('.cy-expand-collapse-meta-edge').length > 0")
    assert d.page.evaluate(
        "() => cy.edges('.cy-expand-collapse-meta-edge')"
        ".every(e => e.style('curve-style') === 'bezier')"
    ), d.engine
    _open_control(d, "#edge-style")
    control.select_option("taxi")
    _click(d, "#expand-all")
    d.page.wait_for_function("() => cy.$id('MEM').length === 1")
    assert d.page.evaluate(
        "() => cy.edges().not('.lock, .nesting')"
        ".every(e => e.style('curve-style') === 'taxi')"
        " && cy.edges('.nesting').every(e => e.style('curve-style') === 'bezier')"
    ), d.engine
    _open_control(d, "#edge-style")
    control.select_option("bezier")
    _arm_layout_wait(d)
    _click(d, "#arrange")
    _wait_layout_done(d)
    assert control.input_value() == "bezier", d.engine
    assert d.page.evaluate(
        "() => cy.edges().not('.lock').every(e => e.style('curve-style') === 'bezier')"
    ), d.engine
    assert d.errors == [], (d.engine, d.errors)


def test_edge_style_keeps_ancestor_member_edges_drawable(driven: Driven) -> None:
    """DL-381: ancestor/member edges retain their Bezier exception in either mode."""
    d = driven
    assert d.page.evaluate("() => cy.edges('.nesting').length") > 0, d.engine
    _open_control(d, "#edge-style")
    for style in ("bezier", "taxi"):
        d.page.get_by_label("Edge style", exact=True).select_option(style)
        assert d.page.evaluate(
            "() => cy.edges('.nesting').every(e => e.style('curve-style') === 'bezier')"
        ), d.engine
    assert d.errors == [], (d.engine, d.errors)


def _edge_mode(d: Driven, mode: str) -> None:
    _open_control(d, "#edge-style")
    d.page.get_by_label("Edge style", exact=True).select_option(mode)
    _click(d, "#view-panel [data-close-panel]")


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("direction", ("RIGHT", "DOWN"))
def test_elk_routes_render_nested_bends_ports_and_shapes(
    _playwright: Any, tmp_path: Any, engine: str, direction: str
) -> None:
    """DL-382: actual rendered points retain ELK's bends in both layout directions."""
    text = (
        _NESTED_BOX_TEXT
        + "\ninsert_job: WATCH\njob_type: f\nbox_name: IB\nwatch_file: /synthetic/in\n"
        "machine: m1\n\n"
        "insert_job: INNER\njob_type: c\nbox_name: IB\ncommand: x\nmachine: m1\n"
        "condition: s(IM) & s(WATCH)\n\n"
        "insert_job: GLOBAL_GATE\njob_type: c\nbox_name: B\ncommand: x\nmachine: m1\n"
        "condition: s(INNER) & v(READY) = 1\n\n"
        "insert_job: LOOP\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(LOOP)\n"
    )
    path = tmp_path / "routes.html"
    path.write_text(to_explore_html(lower_source(text), direction=direction), encoding="utf-8")
    browser = getattr(_playwright, engine).launch()
    try:
        d = Driven(engine=engine, page=browser.new_page())
        warnings: list[str] = []
        d.page.on("pageerror", lambda err: d.errors.append(str(err)))
        d.page.on(
            "console", lambda msg: warnings.append(msg.text) if msg.type == "warning" else None
        )
        d.page.goto(path.as_uri())
        _ready(d)
        before = d.page.evaluate(_LEAF_POSITIONS)
        _edge_mode(d, "elk")
        result = d.page.evaluate(
            """() => {
              const errors = [], shapes = new Set();
              let routed = 0, inner = 0, bends = 0;
              cy.edges().not('.lock, .nesting').forEach(edge => {
                if (edge.source().same(edge.target())) {
                  if (elkRoute(edge)) errors.push('loop was routed');
                  return;
                }
                const route = elkRoute(edge), raw = edge.scratch('elk');
                if (!route) { errors.push('missing route: ' + edge.id()); return; }
                routed++;
                const source = edge.source(), target = edge.target();
                shapes.add(source.style('shape')); shapes.add(target.style('shape'));
                const actual = [edge.sourceEndpoint(), ...(edge.segmentPoints() || []),
                  edge.targetEndpoint()];
                let x = 0, y = 0;
                if (raw.container !== 'root') {
                  inner++;
                  let container = cy.$id(raw.container);
                  while (container.nonempty()) {
                    x += container.scratch('elk').x; y += container.scratch('elk').y;
                    container = container.parent();
                  }
                }
                const expectedBends = raw.sections[0].bendPoints || [];
                bends += expectedBends.length;
                if (actual.length !== expectedBends.length + 2) errors.push(edge.id() + ': length');
                expectedBends.forEach((point, i) => {
                  if (Math.hypot(actual[i+1].x - point.x - x,
                      actual[i+1].y - point.y - y) > 0.01) errors.push(edge.id() + ': bend');
                });
                actual.forEach((point, i) => {
                  if (!Number.isFinite(point.x) || !Number.isFinite(point.y))
                    errors.push(edge.id() + ': nonfinite');
                  if (i && Math.abs(point.x - actual[i-1].x) > 0.01 &&
                      Math.abs(point.y - actual[i-1].y) > 0.01) errors.push(edge.id() + ': diagonal');
                });
                [source, target].forEach((node, i) => {
                  const point = actual[i ? actual.length-1 : 0], center = node.position();
                  const nx = (point.x - center.x) / (node.outerWidth()/2);
                  const ny = (point.y - center.y) / (node.outerHeight()/2);
                  if (Math.abs(nx) > 1.001 || Math.abs(ny) > 1.001)
                    errors.push(edge.id() + ': outside node');
                  if (node.style('shape') === 'diamond' &&
                      Math.abs(Math.abs(nx) + Math.abs(ny) - 1) > 0.001)
                    errors.push(edge.id() + ': diamond outline');
                  if (node.style('shape') === 'rhomboid' &&
                      Math.min(Math.abs(ny+1), Math.abs(ny-1),
                        Math.abs(nx-(-0.6665+0.3335*ny)),
                        Math.abs(nx-(0.6665+0.3335*ny))) > 0.001)
                    errors.push(edge.id() + ': rhomboid outline');
                });
              });
              return {errors, routed, inner, bends, shapes: [...shapes].sort()};
            }"""
        )
        assert result["errors"] == [], (engine, direction, result)
        assert result["routed"] >= 8 and result["inner"] >= 2 and result["bends"] >= 2
        assert result["shapes"] == ["diamond", "rhomboid", "round-rectangle"]
        assert d.page.evaluate(_LEAF_POSITIONS) == before
        assert warnings == [] and d.errors == [], (engine, direction, warnings, d.errors)
    finally:
        browser.close()


def test_elk_routes_expire_on_real_drag_and_return_on_arrange(driven_trace: Driven) -> None:
    """DL-382: moving an unconnected member expires unrelated routes without a layout."""
    d = driven_trace
    _edge_mode(d, "elk")
    assert "routed" in d.page.inner_text("#routing-status"), d.engine
    assert d.page.locator("#refresh-routes").is_hidden(), d.engine
    assert d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    d.page.evaluate(
        "() => { window.routeLayouts = 0; cy.on('layoutstart', () => routeLayouts++); }"
    )
    point = _client_point(d, "N")
    _drag(d, point, {"x": point["x"] + 55, "y": point["y"] + 35})
    d.page.wait_for_function("() => !cy.edges().some(e => !!elkRoute(e))")
    assert d.page.evaluate("() => routeLayouts") == 0, d.engine
    assert d.page.evaluate(
        "() => cy.edges().not('.nesting, .lock').every(e =>"
        " e.style('curve-style') === 'taxi' && e.style('source-endpoint') === 'outside-to-node')"
    ), d.engine
    assert "expired" in d.page.inner_text("#routing-status"), d.engine
    assert d.page.locator("#refresh-routes").is_visible(), d.engine
    _arm_layout_wait(d)
    d.page.locator("#status-scroll").focus()
    for _ in range(3):
        d.page.keyboard.press("Tab")
        if d.page.evaluate("() => document.activeElement.id === 'refresh-routes'"):
            break
    assert d.page.evaluate("() => document.activeElement.id") == "refresh-routes", d.engine
    d.page.keyboard.press("Enter")
    _wait_layout_done(d)
    assert d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e)) && !elkRunning"), d.engine
    assert "routed" in d.page.inner_text("#routing-status"), d.engine
    assert d.page.locator("#refresh-routes").is_hidden(), d.engine


def test_elk_routes_expire_across_folds_and_hidden_nodes(driven_trace: Driven) -> None:
    """DL-382: removed edges cannot restore old paths, including after a collapsed layout."""
    d = driven_trace
    _edge_mode(d, "elk")
    d.page.evaluate(
        "() => { window.routeLayouts = 0; cy.on('layoutstart', () => routeLayouts++); }"
    )
    outside = _positions(d, ["P", "Q", "C", "D"])
    _click(d, "#collapse-all")
    d.page.wait_for_function("() => cy.$id('M').empty()")
    assert not d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    assert "expired" in d.page.inner_text("#routing-status"), d.engine
    assert _positions(d, list(outside)) == outside
    assert d.page.evaluate("() => routeLayouts") == 0
    _arm_layout_wait(d)
    _click(d, "#arrange")
    _wait_layout_done(d)
    assert d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    _click(d, "#expand-all")
    d.page.wait_for_function("() => cy.$id('IM').nonempty()")
    assert not d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    _arm_layout_wait(d)
    _click(d, "#arrange")
    _wait_layout_done(d)
    point = _client_point(d, "N")
    d.page.mouse.click(point["x"], point["y"])
    d.page.wait_for_function("() => cy.$id('N').selected()")
    _control_ready(d, "#hide-selected")
    _click(d, "#hide-selected")
    assert not d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    _arm_layout_wait(d)
    _click(d, "#arrange")
    _wait_layout_done(d)
    assert d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    _click(d, "#show-all")
    assert not d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine


def test_elk_route_cache_survives_view_and_annotation_changes(driven_trace: Driven) -> None:
    """DL-382: view-only operations preserve paths and switching restores automatic endpoints."""
    d = driven_trace
    _edge_mode(d, "elk")
    epoch = d.page.evaluate("() => routeEpoch")
    geometry = d.page.evaluate(_LEAF_POSITIONS)
    d.page.evaluate("() => { cy.$id('N').select(); highlightNodes(cy.$id('M')); }")
    d.page.evaluate("() => { cy.zoom(1.25); cy.pan({x: 31, y: 47}); }")
    for mode in ("bezier", "taxi", "elk", "bezier", "elk"):
        _edge_mode(d, mode)
        assert d.page.evaluate("() => routeEpoch") == epoch
        assert d.page.evaluate(_LEAF_POSITIONS) == geometry
        assert d.page.evaluate("() => cy.$id('N').selected() && cy.$id('M').hasClass('hl')")
        if mode != "elk":
            assert d.page.evaluate(
                "() => cy.edges().every(e => e.style('edge-distances') === 'intersection'"
                " && e.style('source-endpoint') === 'outside-to-node'"
                " && e.style('target-endpoint') === 'outside-to-node')"
            ), d.engine
    assert d.page.evaluate("() => cy.edges().every(e => !!elkRoute(e))"), d.engine


def test_elk_route_rejects_unsupported_sections_without_losing_the_edge(
    driven_trace: Driven,
) -> None:
    """DL-382: a bad section does not become a partial or non-finite rendered path."""
    d = driven_trace
    _edge_mode(d, "elk")
    result = d.page.evaluate(
        """() => {
          const edge = cy.edges()[1], original = edge.scratch('elk');
          const variants = [
            {...original, sections: []},
            {...original, sections: [original.sections[0], original.sections[0]]},
            {...original, sections: [{startPoint: {x: NaN, y: 0}, endPoint: {x: 5, y: 0}}]},
            {...original, container: 'missing-container'},
            {...original, sections: [{startPoint: {x: 0, y: 0}, endPoint: {x: 3, y: 4}}]}
          ];
          const targetPosition = {...edge.target().position()};
          try {
            const rejected = variants.map(raw => {
              edge.scratch('elk', raw);
              const route = captureElkRoute(edge);
              edge.scratch('_elkRoute', route); edge.updateStyle();
              return route === null && edge.style('curve-style') === 'taxi' && !edge.removed();
            });
            const source = edge.source().position();
            edge.target().position({x: source.x + 10, y: source.y});
            edge.scratch('elk', {...original, container: 'root', sections: [{
              startPoint: {x: source.x-15, y: source.y},
              endPoint: {x: source.x+25, y: source.y}
            }]});
            rejected.push(captureElkRoute(edge) === null);
            return rejected;
          } finally {
            edge.scratch('elk', original); edge.target().position(targetPosition);
          }
        }"""
    )
    assert result == [True] * 6, (d.engine, result)
    assert d.errors == [], (d.engine, d.errors)


def test_elk_arrangements_serialize_and_reject_changed_topology(driven_trace: Driven) -> None:
    """DL-382: a queued layout cannot publish paths for an earlier visible graph."""
    d = driven_trace
    _edge_mode(d, "elk")
    d.page.evaluate(
        """() => {
          window.routeEvents = [];
          cy.on('layoutstart layoutstop', event => routeEvents.push(event.type));
          arrangeVisible('first'); arrangeVisible('second'); arrangeVisible('last');
        }"""
    )
    d.page.wait_for_function(
        "() => routeEvents.filter(e => e === 'layoutstop').length === 2 && !elkRunning"
    )
    assert d.page.evaluate("() => routeEvents") == [
        "layoutstart",
        "layoutstop",
        "layoutstart",
        "layoutstop",
    ], d.engine
    assert d.page.evaluate(
        "() => !cy.autoungrabify() && !document.getElementById('arrange').disabled"
        " && cy.edges().some(e => !!elkRoute(e))"
    ), d.engine
    d.page.evaluate("() => { routeEvents = []; arrangeVisible(); cy.$id('N').addClass('hidden'); }")
    d.page.wait_for_function("() => routeEvents.includes('layoutstop') && !elkRunning")
    assert not d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))"), d.engine
    assert d.errors == [], (d.engine, d.errors)


def test_elk_rejection_releases_controls_and_preserves_the_view(driven_trace: Driven) -> None:
    """DL-383: an actual engine rejection leaves the graph usable and retryable."""
    d = driven_trace
    _edge_mode(d, "elk")
    d.page.evaluate("() => { cy.$id('M').select(); highlightNodes(cy.$id('N')); }")
    before = (d.page.evaluate(_LEAF_POSITIONS), _viewport(d), _selected_ids(d))
    d.page.evaluate(
        """() => {
          window.goodElkLayout = elkLayout;
          elkLayout = () => {
            const config = goodElkLayout();
            config.elk.algorithm = 'dsl41-test-missing-algorithm';
            return config;
          };
        }"""
    )
    _click(d, "#arrange")
    d.page.wait_for_function("() => !!layoutError && !elkRunning")
    assert (d.page.evaluate(_LEAF_POSITIONS), _viewport(d), _selected_ids(d)) == before
    assert d.page.evaluate(
        "() => !cy.autoungrabify() && !document.getElementById('arrange').disabled"
        " && cy.$id('N').hasClass('hl') && !cy.edges().some(e => !!elkRoute(e))"
    ), d.engine
    assert "Layout failed" in d.page.inner_text("#routing-status"), d.engine
    assert "dsl41-test-missing-algorithm" in d.page.get_attribute("#routing-status", "title")
    assert d.page.locator("#refresh-routes").is_visible(), d.engine
    d.page.evaluate("() => { elkLayout = goodElkLayout; }")
    _arm_layout_wait(d)
    d.page.locator("#refresh-routes").click()
    _wait_layout_done(d)
    assert "routed" in d.page.inner_text("#routing-status"), d.engine
    assert d.page.evaluate("() => !layoutError && !elkRunning && !cy.autoungrabify()")
    assert d.errors == [], (d.engine, d.errors)


@pytest.mark.parametrize("failure", ("synchronous", "capture"))
def test_elk_pipeline_failure_restores_the_previous_grab_setting(
    driven_trace: Driven, failure: str
) -> None:
    """DL-383: run throws and rejected position/capture work share the failure boundary."""
    d = driven_trace
    d.page.evaluate(
        """failure => {
          cy.autoungrabify(failure === 'synchronous');
          if (failure === 'synchronous') {
            const makeLayout = routedLayout;
            routedLayout = elements => {
              const layout = makeLayout(elements);
              layout.run = () => { throw new Error('synchronous test failure'); };
              return layout;
            };
          } else {
            captureElkRoutes = () => { throw new Error('capture test failure'); };
          }
        }""",
        failure,
    )
    _click(d, "#arrange")
    d.page.wait_for_function("() => !!layoutError && !elkRunning")
    assert d.page.evaluate("() => cy.autoungrabify()") == (failure == "synchronous")
    assert not d.page.locator("#arrange").is_disabled(), d.engine
    # A layout failure is visible even with the default Orthogonal edge style.
    assert d.page.locator("#routing-status").is_visible(), d.engine
    assert failure in d.page.get_attribute("#routing-status", "title")
    assert d.page.locator("#refresh-routes").is_visible(), d.engine
    assert d.errors == [], (d.engine, d.errors)


def test_elk_old_failure_cannot_release_a_new_arrangement(driven_trace: Driven) -> None:
    """DL-383: a stop listener can begin the next run before the old promise settles."""
    d = driven_trace
    _edge_mode(d, "elk")
    d.page.evaluate(
        """() => {
          window.oldLayoutRejected = false;
          const old = routedLayout(cy.elements(':visible'));
          old.on('layoutstop', () => {
            arrangeVisible('new arrangement');
            throw new Error('late old stop listener');
          });
          runElkLayout(old);
          old.promise.catch(() => { oldLayoutRejected = true; });
        }"""
    )
    d.page.wait_for_function(
        "() => oldLayoutRejected && !elkRunning && lastOp === 'new arrangement'"
    )
    assert d.page.evaluate(
        "() => !layoutError && !cy.autoungrabify() &&"
        " !document.getElementById('arrange').disabled && cy.edges().some(e => !!elkRoute(e))"
    ), d.engine
    assert "routed" in d.page.inner_text("#routing-status"), d.engine
    assert d.errors == [], (d.engine, d.errors)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("stage", ("initial", "folded"))
def test_elk_startup_failure_finishes_loading_and_allows_retry(
    _playwright: Any, tmp_path: Any, engine: str, stage: str
) -> None:
    """DL-383: either startup layout may fail without leaving the page loading forever."""
    page = to_explore_html(lower_source(_NESTED_BOX_TEXT), collapse_threshold=1)
    injection = (
        "initial.options.elk.algorithm = 'dsl41-test-missing-algorithm';\n"
        if stage == "initial"
        else "const goodElkLayout = elkLayout;\n"
        "elkLayout = () => { const config = goodElkLayout();\n"
        "config.elk.algorithm = 'dsl41-test-missing-algorithm'; return config; };\n"
    )
    page = page.replace("runElkLayout(initial);", injection + "runElkLayout(initial);")
    path = tmp_path / "startup-failure.html"
    path.write_text(page, encoding="utf-8")
    browser = getattr(_playwright, engine).launch()
    try:
        d = Driven(engine=engine, page=browser.new_page())
        d.page.on("pageerror", lambda error: d.errors.append(str(error)))
        d.page.goto(path.as_uri())
        _ready(d)
        assert d.page.evaluate("() => !loading && !elkRunning && !cy.autoungrabify()")
        assert not d.page.locator("#search").is_disabled(), (engine, stage)
        assert not d.page.locator("#arrange").is_disabled(), (engine, stage)
        assert "Layout failed" in d.page.inner_text("#routing-status"), (engine, stage)
        if stage == "folded":
            d.page.evaluate("() => { elkLayout = goodElkLayout; }")
        _arm_layout_wait(d)
        d.page.locator("#refresh-routes").click()
        _wait_layout_done(d)
        assert d.page.evaluate("() => !layoutError && !elkRunning")
        assert d.page.locator("#routing-status").is_hidden()  # default Orthogonal style
        assert d.errors == [], (engine, stage, d.errors)
    finally:
        browser.close()


def _chrome_size(d: Driven) -> dict[str, float]:
    size: dict[str, float] = d.page.evaluate(
        "() => ({header: document.querySelector('header').getBoundingClientRect().height,"
        " status: document.getElementById('status-bar').getBoundingClientRect().height,"
        " toolbar: document.getElementById('toolbar').getBoundingClientRect().height,"
        " page: document.documentElement.scrollWidth, width: innerWidth})"
    )
    return size


def test_compact_rows_stay_bounded_on_resize_and_narrow_controls_are_reachable(
    driven: Driven,
) -> None:
    d = driven
    for width, height in ((1280, 800), (1024, 768), (800, 600), (360, 300)):
        d.page.set_viewport_size({"width": width, "height": height})
        size = _chrome_size(d)
        assert size["header"] + size["status"] <= 110, (d.engine, width, size)
        assert size["toolbar"] <= 36 and size["header"] <= 80, (d.engine, width, size)
        assert size["page"] == width, (d.engine, width, size)
        if width >= 1024:
            for selector in ("#search", "#find-select", "#find-highlight", "#view-toggle"):
                box = d.page.locator(selector).bounding_box()
                assert box and 0 <= box["x"] < box["x"] + box["width"] <= width, box
        # A real click scrolls a narrow command row to its offscreen disclosure.
        d.page.locator("#view-toggle").click()
        assert d.page.locator("#view-panel").is_visible(), d.engine
        d.page.locator("#view-panel [data-close-panel]").click()
        d.page.locator("#search").fill("no-such-synthetic-job")
        d.page.locator("#find-select").click()
        assert "no match" in d.page.inner_text("#find-note"), d.engine
        assert _chrome_size(d) == size, d.engine
        d.page.locator("#help-toggle").click()
        summary = d.page.locator("#catalog-summary")
        assert summary.is_visible(), d.engine
        assert summary.inner_text() == d.page.locator("header .summary").text_content(), d.engine
        assert "1 unused" in summary.inner_text(), d.engine  # synthetic unused resource
        d.page.keyboard.press("Escape")


def test_panels_and_help_leave_graph_geometry_and_layers_unchanged(driven_trace: Driven) -> None:
    d = driven_trace
    point = _client_point(d, "M")
    d.page.mouse.click(point["x"], point["y"])
    assert d.page.locator("#details").is_visible(), d.engine
    d.page.evaluate("() => { cy.$id('M').addClass('hl'); cy.pan({x: 37, y: 59}); cy.zoom(1.2); }")
    before = (d.page.evaluate(_LEAF_POSITIONS), _viewport(d), _selected_ids(d), _counts(d))
    for name in ("selection", "highlight", "visibility", "view", "help"):
        d.page.locator(f"#{name}-toggle").click()
        assert d.page.locator(".chrome-panel:visible").count() == 1, d.engine
        assert d.page.locator("#details").is_visible(), d.engine
        assert before == (
            d.page.evaluate(_LEAF_POSITIONS),
            _viewport(d),
            _selected_ids(d),
            _counts(d),
        ), (d.engine, name)
    d.page.keyboard.press("Escape")
    assert d.page.locator(".chrome-panel:visible").count() == 0, d.engine
    assert d.page.evaluate("() => document.activeElement.id") == "help-toggle", d.engine
    assert d.page.locator("#details").is_visible(), d.engine
    assert before == (d.page.evaluate(_LEAF_POSITIONS), _viewport(d), _selected_ids(d), _counts(d))


def test_node_details_stay_operable_under_an_open_controls_panel(
    driven_trace: Driven,
) -> None:
    """DL-204 keeps the node-details panel separate, and #chrome-backdrop --
    the layer that turns an outside gesture into a dismissal -- covers all of
    #wrap. So details has to sit ABOVE it, or a controls panel makes details
    inert and swallows the click meant for its own buttons, DL-191's
    condition tree included.

    `is_visible` cannot see this: an occluded panel is still visible, and
    that is exactly how the defect passed a green suite. The assertion is
    therefore hit-testing plus a real click that has to ACT."""
    d = driven_trace
    point = _client_point(d, "M")
    d.page.mouse.click(point["x"], point["y"])
    assert d.page.locator("#details").is_visible(), d.engine
    d.page.locator("#selection-toggle").click()
    assert d.page.locator("#selection-panel").is_visible(), d.engine
    hit = d.page.evaluate(
        "() => { const r = document.getElementById('d-close').getBoundingClientRect();"
        " const t = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);"
        " return t ? t.id : null; }"
    )
    assert hit == "d-close", (d.engine, hit)
    d.page.locator("#d-close").click()
    # its own button acted, and the same click is an outside gesture for the
    # panel, so that dismisses too -- one click, both surfaces, no graph change
    assert not d.page.locator("#details").is_visible(), d.engine
    assert d.page.locator(".chrome-panel:visible").count() == 0, d.engine
    assert d.page.get_attribute("#selection-toggle", "aria-expanded") == "false", d.engine


def test_panel_keyboard_repeat_actions_and_escape_preserve_find_and_selection(
    driven_trace: Driven,
) -> None:
    d = driven_trace
    d.page.locator("#search").fill("still typing")
    d.page.evaluate("() => { cy.$id('M').select(); }")
    d.page.locator("#selection-toggle").focus()
    d.page.keyboard.press("Enter")
    assert d.page.locator("#selection-panel").is_visible(), d.engine
    # Opening focuses Close. Tab reaches the first enabled action.
    d.page.keyboard.press("Tab")
    assert d.page.evaluate("() => document.activeElement.id") == "sel-fan-in", d.engine
    d.page.keyboard.press("Space")
    first = _selected_ids(d)
    assert "M" in first and len(first) > 1, (d.engine, first)
    assert d.page.locator("#selection-panel").is_visible(), d.engine
    d.page.keyboard.press("Enter")
    assert set(first) <= set(_selected_ids(d)), d.engine
    assert d.page.locator("#selection-panel").is_visible(), d.engine
    selected = _selected_ids(d)
    d.page.keyboard.press("Escape")
    assert d.page.evaluate("() => document.activeElement.id") == "selection-toggle", d.engine
    assert d.page.input_value("#search") == "still typing", d.engine
    assert _selected_ids(d) == selected, d.engine
    d.page.locator("#search").focus()
    d.page.keyboard.press("Escape")
    assert d.page.input_value("#search") == "", d.engine
    assert _selected_ids(d) == selected, d.engine
    d.page.keyboard.press("Escape")
    assert _selected_ids(d) == [], d.engine
    # Space also opens a disclosure; Shift+Tab leaves it without a focus trap.
    d.page.locator("#visibility-toggle").focus()
    d.page.keyboard.press("Space")
    assert d.page.locator("#visibility-panel").is_visible(), d.engine
    d.page.keyboard.press("Shift+Tab")
    assert not d.page.locator("#visibility-panel").is_visible(), d.engine
    assert d.page.evaluate("() => document.activeElement.id") == "snapshot-toggle", d.engine
    d.page.keyboard.press("Shift+Tab")
    assert d.page.evaluate("() => document.activeElement.id") == "view-toggle", d.engine


def test_outside_panel_dismissal_consumes_canvas_gesture_then_canvas_works(
    driven_trace: Driven,
) -> None:
    d = driven_trace
    d.page.evaluate("() => { cy.$id('M').select(); }")
    before = (_selected_ids(d), _viewport(d), d.page.evaluate(_LEAF_POSITIONS))
    for name in ("visibility", "help"):
        d.page.locator(f"#{name}-toggle").click()
        box = d.page.locator("#cy").bounding_box()
        assert box
        d.page.mouse.click(box["x"] + box["width"] - 5, box["y"] + box["height"] - 5)
        assert not d.page.locator(f"#{name}-panel").is_visible(), d.engine
        assert before == (_selected_ids(d), _viewport(d), d.page.evaluate(_LEAF_POSITIONS))
    d.page.mouse.click(box["x"] + box["width"] - 5, box["y"] + box["height"] - 5)
    assert _selected_ids(d) == [], d.engine


def test_help_is_closed_scrollable_and_has_all_existing_gestures_and_legend(driven: Driven) -> None:
    d = driven
    assert not d.page.locator("#help-panel").is_visible(), d.engine
    d.page.set_viewport_size({"width": 360, "height": 240})
    d.page.locator("#help-toggle").click()
    help_text = d.page.inner_text("#help-panel")
    for phrase in (
        "shift+click",
        "shift+drag",
        "right-click",
        "double-click",
        "Escape",
        "drag pans, wheel zooms",
        "moves the whole selection",
        "there is no undo",
        "all incoming arrows must hold (AND)",
        "bare n() is a lock and draws no arrow",
        "dotted gray = lock",
        "tee marks the job that waits",
    ):
        assert phrase in help_text, (d.engine, phrase)
    panel = d.page.locator("#help-panel")
    assert panel.evaluate("el => el.scrollHeight > el.clientHeight"), d.engine
    panel.hover()
    d.page.mouse.wheel(0, 600)
    d.page.wait_for_function("() => document.getElementById('help-panel').scrollTop > 0")
    assert _chrome_size(d)["page"] == 360, d.engine
    d.page.keyboard.press("Escape")
    assert not panel.is_visible(), d.engine
    assert d.page.evaluate("() => document.activeElement.id") == "help-toggle", d.engine


@pytest.fixture(scope="module")
def degraded_page_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Both optional plugins absent, with a real emitted long title."""
    page = to_explore_html(corpus_catalog(), title="Synthetic long title " * 100)
    assert page.count("cy.contextMenus({") == page.count("cy.expandCollapse({") == 1
    page = page.replace("cy.contextMenus({", "cy.contextMenusMissing({")
    page = page.replace("cy.expandCollapse({", "cy.expandCollapseMissing({")
    path = tmp_path_factory.mktemp("explore-degraded") / "degraded.html"
    path.write_text(page, encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module", params=ENGINES)
def _driven_degraded(
    request: pytest.FixtureRequest, degraded_page_url: str, _playwright: Any
) -> Any:
    yield from _open_driven(_playwright, request.param, degraded_page_url)


@pytest.fixture
def driven_degraded(_driven_degraded: Driven) -> Driven:
    return _reset(_driven_degraded)


def test_long_title_status_and_missing_plugins_keep_html_actions_usable(
    driven_degraded: Driven,
) -> None:
    d = driven_degraded
    for width, height in ((1280, 800), (1024, 768), (360, 300)):
        d.page.set_viewport_size({"width": width, "height": height})
        size = _chrome_size(d)
        assert size["header"] + size["status"] <= 110 and size["page"] == width, size
        warning = d.page.locator("#feature-warning")
        assert warning.is_visible(), d.engine
        for phrase in ("context menu unavailable", "box collapse unavailable"):
            assert phrase in (warning.get_attribute("aria-label") or ""), d.engine
            assert phrase in d.page.inner_text("#stats"), d.engine
        long_query = "no-such-synthetic-job-" * 150
        d.page.locator("#search").fill(long_query)
        d.page.locator("#find-select").click()
        assert long_query in d.page.inner_text("#find-note"), d.engine
        assert _chrome_size(d) == size, d.engine
        assert d.page.locator("#status-scroll").evaluate("el => el.scrollWidth > el.clientWidth")
        warning.click()
        assert d.page.locator("#status-scroll").evaluate("el => el.scrollLeft > 0"), d.engine
        assert d.page.evaluate("() => document.activeElement.id") == "status-scroll", d.engine
    d.page.set_viewport_size({"width": 1280, "height": 800})
    d.page.locator("#search").fill(_sample_node(d))
    d.page.locator("#find-select").click()
    assert _selected_ids(d), d.engine
    # The persistent count button keeps keyboard focus when status is rewritten.
    d.page.locator("#stats-selected").focus()
    d.page.evaluate("() => { updateStats('synthetic operation '.repeat(100)); }")
    assert d.page.evaluate("() => document.activeElement.id") == "stats-selected", d.engine
    d.page.keyboard.press("Enter")
    d.page.locator("#highlight-toggle").click()
    d.page.locator("#highlight-selected").click()
    assert _counts(d)["highlighted"] > 0, d.engine
    assert d.page.locator("#highlight-panel").is_visible(), d.engine
    d.page.locator("#visibility-toggle").click()
    assert d.page.locator("#relayout").is_checked() is False, d.engine
    assert d.page.locator("#locks").is_checked(), d.engine
    d.page.locator("#hide-selected").click()
    assert _selected_ids(d) == [], d.engine
    d.page.locator("#show-all").click()
    d.page.locator("#selection-toggle").click()
    assert d.page.locator("#trace-boxes").is_checked(), d.engine
    d.page.locator("#select-highlighted").click()
    assert _selected_ids(d), d.engine
    d.page.locator("#view-toggle").click()
    assert d.page.locator("#collapse-all").is_disabled(), d.engine
    assert d.page.locator("#expand-all").is_disabled(), d.engine
    d.page.locator("#fit-selection").click()
    d.page.locator("#help-toggle").click()
    assert d.page.locator("#help-panel").is_visible(), d.engine
    assert d.errors == [], (d.engine, d.errors)


def _scroll_graph_menu(d: Driven, *, bottom: bool) -> None:
    """Use real wheel gestures until the native scroll boundary. Firefox
    caps one wheel to 60px here even when the requested delta is 600px."""
    menu = d.page.locator(".cy-context-menus-cxt-menu")
    menu.hover()
    for _ in range(10):
        top, end = menu.evaluate("el => [el.scrollTop, el.scrollHeight - el.clientHeight]")
        if abs(top - (end if bottom else 0)) <= 1:
            return
        d.page.mouse.wheel(0, 600 if bottom else -600)
        d.page.wait_for_function(
            "previous => { const m = openMenu(); return m && m.scrollTop !== previous; }",
            arg=top,
        )
    pytest.fail(f"{d.engine}: wheel gestures never reached the menu's scroll boundary")


def test_short_narrow_graph_menu_scrolls_to_first_and_last_actions_without_zoom(
    driven_trace: Driven,
) -> None:
    d = driven_trace
    d.page.set_viewport_size({"width": 360, "height": 240})
    # Place one unboxed node at controlled edge/center points, away from all
    # other nodes. The graph menu positions itself relative to each real click.
    d.page.evaluate(
        "() => { cy.nodes().positions(() => ({x:-1000,y:-1000}));"
        " cy.resize(); cy.zoom(1); cy.pan({x:0,y:0}); }"
    )
    # The boundary is menu-specific. A wheel over the canvas still zooms.
    d.page.mouse.move(20, 190)
    d.page.mouse.wheel(0, 60)
    d.page.wait_for_function("() => cy.zoom() !== 1")
    d.page.evaluate("() => { cy.zoom(1); cy.pan({x:0,y:0}); }")
    for x, y in ((20, 20), (180, 68), (310, 97), (310, 120)):
        # Cytoscape applies a class change lazily, at its next frame, and its
        # hit test reads the drawn-elements cache that frame refreshes. A
        # right-click before that frame misses the just-unhidden C and opens
        # the canvas menu. The move always draws, so the frame arrives.
        d.page.evaluate(
            "p => new Promise(done => { cy.one('render', () => done());"
            " cy.nodes().unselect(); cy.$id('C').removeClass('hidden').position(p); })",
            {"x": x, "y": y},
        )
        point = _client_point(d, "C")
        d.page.mouse.click(point["x"], point["y"], button="right")
        menu = d.page.locator(".cy-context-menus-cxt-menu")
        bounds = menu.bounding_box()
        canvas = d.page.locator("#cy").bounding_box()
        assert bounds and canvas
        assert (
            canvas["x"] <= bounds["x"]
            and bounds["x"] + bounds["width"] <= canvas["x"] + canvas["width"] + 1
            and canvas["y"] <= bounds["y"]
            and bounds["y"] + bounds["height"] <= canvas["y"] + canvas["height"] + 1
        ), (d.engine, x, y, bounds, canvas)
        # Last time left the menu scrolled down. Return to its first action
        # by a wheel gesture, not Playwright's automatic scroll-before-click.
        view = _viewport(d)
        positions = d.page.evaluate(_LEAF_POSITIONS)
        _scroll_graph_menu(d, bottom=False)
        d.page.locator("#fan-in").click()
        assert "C" in _selected_ids(d) and len(_selected_ids(d)) > 1, d.engine
        assert _viewport(d) == view and d.page.evaluate(_LEAF_POSITIONS) == positions, d.engine
        d.page.mouse.click(point["x"], point["y"], button="right")
        _scroll_graph_menu(d, bottom=True)
        assert _viewport(d) == view, d.engine
        d.page.locator("#hide").click()
        assert not d.page.evaluate("() => cy.$id('C').visible()"), d.engine
        assert _viewport(d) == view, d.engine
    assert d.errors == [], (d.engine, d.errors)


# ------------------------------------------------------- DL-384 compact captions

_LONG_NAME_A = "REGION_ASSET_NORTH_LEDGER_PRIMARY_01_C"
_LONG_NAME_B = "REGION_ASSET_SOUTH_LEDGER_PRIMARY_01_C"
_EMPTY_BOX = "EMPTY_BOX_WITH_A_LONG_TITLE"
_CJK_NAME = "東京監査_識別名を保持する_C"
_WIDE_NAME = "WWWWWWWWWWWWWWWWWWWWWWWWW"
_LABEL_TEXT = (
    _NESTED_BOX_TEXT + f"\ninsert_job: {_LONG_NAME_A} job_type: c\nbox_name: IB\n"
    "command: true\ncondition: s(P) | s(Q)\n"
    + f"\ninsert_job: {_LONG_NAME_B} job_type: c\nbox_name: IB\n"
    + f"command: true\ncondition: n({_LONG_NAME_B})\n"
    + "\ninsert_job: FW_OR job_type: f\nwatch_file: /synthetic/watch\n"
    "condition: (s(P) | s(Q)) & n(FW_OR)\n"
    + f"\ninsert_job: {_EMPTY_BOX} job_type: b\n"
    + f"\ninsert_job: {_CJK_NAME} job_type: c\nbox_name: IB\ncommand: true\ncondition: s(M)\n"
    + f"\ninsert_job: {_WIDE_NAME} job_type: c\ncommand: true\ncondition: s(P)\n"
)


@pytest.fixture(scope="module", params=[(e, d) for e in ENGINES for d in ("LR", "TD")])
def _driven_labels(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, _playwright: Any
) -> Any:
    engine, direction = request.param
    path = tmp_path_factory.mktemp("explore-labels") / "labels.html"
    path.write_text(
        to_explore_html(lower_source(_LABEL_TEXT), title="labels", direction=direction),
        encoding="utf-8",
    )
    yield from _open_driven(_playwright, engine, path.as_uri())


@pytest.fixture
def driven_labels(_driven_labels: Driven) -> Driven:
    return _reset(_driven_labels)


def _label_geometry(d: Driven) -> Any:
    return d.page.evaluate(
        "() => ({nodes: cy.nodes().map(n => [n.id(), n.position(), n.outerWidth(),"
        " n.outerHeight(), stableBounds(n), n.layoutDimensions({nodeDimensionsIncludeLabels:false})]),"
        " fit: cy.getFitViewport(stableBounds(cy.nodes(':visible')),40),"
        " epoch: [routeEpoch,routeLayoutEpoch],"
        " routes: cy.edges().not('.lock, .nesting').map(e => [e.id(),!!elkRoute(e)])})"
    )


def test_compact_caption_attention_preserves_geometry_routes_and_fit(driven_labels: Driven) -> None:
    """DL-384: paint-only attention preserves compound, layout, Fit and route bounds."""
    d = driven_labels
    d.page.evaluate(
        "() => {cy.zoom(.85); cy.center(cy.$id('IB')); placeLocks();"
        " window.labelLayouts=0;cy.on('layoutstart',()=>labelLayouts++);}"
    )
    before = _label_geometry(d)
    point = _client_point(d, _LONG_NAME_A)
    d.page.mouse.move(**point)
    d.page.wait_for_function("() => !document.getElementById('name-callout').hidden")
    assert d.page.inner_text("#name-callout") == _LONG_NAME_A
    assert d.page.locator("#details").is_hidden()
    assert _selected_ids(d) == []
    assert _painted(d) == []
    assert _label_geometry(d) == before
    d.page.mouse.click(**point)
    d.page.mouse.move(**_canvas_point(d, 8, 8))
    assert d.page.inner_text("#d-title") == _LONG_NAME_A
    assert _label_geometry(d) == before
    d.page.evaluate("() => {cy.nodes().select();highlightNodes(cy.$id('P'));}")
    assert _label_geometry(d) == before
    _open_control(d, "#node-labels")
    control = d.page.get_by_label("Labels", exact=True)
    for mode in ("always", "adaptive", "always", "adaptive"):
        control.select_option(mode)
        assert _label_geometry(d) == before
    d.page.evaluate("() => {cy.zoom(.3); cy.nodes().unselect();}")
    assert d.page.evaluate("() => cy.$id('P').style('text-opacity')") == "0"
    assert _label_geometry(d)["nodes"] == before["nodes"]
    assert d.page.evaluate("() => cy.$id('B').style('text-opacity')") == "1"
    d.page.evaluate("() => {cy.$id('P').select();}")
    assert d.page.evaluate("() => cy.$id('P').style('text-opacity')") == "1"
    assert _label_geometry(d)["nodes"] == before["nodes"]
    assert d.page.evaluate("() => labelLayouts") == 0
    assert d.errors == []


def test_full_name_callout_hover_priority_and_viewport_bounds(driven_labels: Driven) -> None:
    """DL-384: a pointer-transparent preview never replaces details or branch ownership."""
    d = driven_labels
    d.page.evaluate("() => {cy.zoom(.9); cy.center(cy.$id('IB'));}")
    point = _client_point(d, _LONG_NAME_A)
    d.page.mouse.click(**point)
    d.page.mouse.move(**_canvas_point(d, 8, 8))
    d.page.wait_for_function(
        "() => document.getElementById('name-callout').textContent.includes('NORTH')"
    )
    details = d.page.locator("#details").inner_html()
    painted = _painted(d)
    d.page.mouse.move(**_client_point(d, _LONG_NAME_B))
    d.page.wait_for_function(
        "() => document.getElementById('name-callout').textContent.includes('SOUTH')"
    )
    assert d.page.locator("#details").inner_html() == details
    assert _painted(d) == painted
    assert _selected_ids(d) == [_LONG_NAME_A]
    d.page.mouse.move(**_canvas_point(d, 8, 8))
    d.page.wait_for_function(
        "() => document.getElementById('name-callout').textContent.includes('NORTH')"
    )
    d.page.evaluate(
        "(id) => {const n=cy.$id(id);cy.pan({x:12-n.position('x')*cy.zoom(),"
        " y:12-n.position('y')*cy.zoom()});}",
        _LONG_NAME_A,
    )
    d.page.wait_for_function(
        "(id) => Math.abs(Number(document.querySelector('#name-link line').getAttribute('x2'))"
        " - cy.$id(id).renderedPosition().x) < .01",
        arg=_LONG_NAME_A,
    )
    bounds = d.page.evaluate(
        "() => {const c=document.getElementById('name-callout'),b=c.getBoundingClientRect(),"
        " w=document.getElementById('wrap').getBoundingClientRect();return {visible:!c.hidden,"
        " inside:b.left>=w.left&&b.top>=w.top&&b.right<=w.right&&b.bottom<=w.bottom,"
        " pointer:getComputedStyle(c).pointerEvents,link:getComputedStyle(document.getElementById('name-link')).pointerEvents};}"
    )
    assert bounds == {"visible": True, "inside": True, "pointer": "none", "link": "none"}
    _click(d, "#d-close")
    d.page.wait_for_function("() => document.getElementById('name-callout').hidden")
    assert _selected_ids(d) == [_LONG_NAME_A]
    for node_id, compound in ((_LONG_NAME_A, False), ("IB", True)):
        d.page.evaluate(
            "({id,compound}) => {const n=cy.$id(id),z=cy.zoom();"
            " const x=compound?40-n.outerWidth()*z/2:-10;"
            " cy.pan({x:x-n.position('x')*z,y:120-n.position('y')*z});}",
            {"id": node_id, "compound": compound},
        )
        d.page.mouse.move(**_canvas_point(d, 35 if compound else 5, 120))
        d.page.wait_for_function(
            "(name) => {const c=document.getElementById('name-callout');"
            " return !c.hidden && c.textContent===name;}",
            arg=node_id,
        )
        anchor_x = d.page.evaluate(
            "Number(document.querySelector('#name-link line').getAttribute('x2'))"
        )
        assert anchor_x == 0
    d.page.evaluate("() => {cy.pan({x:-100000,y:-100000});}")
    d.page.wait_for_function("() => document.getElementById('name-callout').hidden")
    assert d.errors == []


def test_ambiguous_captions_have_full_name_keyboard_choices(driven_labels: Driven) -> None:
    """DL-384: colliding captions are disambiguated through native Find result buttons."""
    d = driven_labels
    captions = d.page.evaluate(
        "(ids) => ids.map(id=>nodeCaption(cy.$id(id)))", [_LONG_NAME_A, _LONG_NAME_B]
    )
    assert captions[0] == captions[1]
    assert all("\u2026" in c and c.startswith("REGION") and c.endswith("C") for c in captions)
    search = d.page.locator("#search")
    search.fill("REGION_ASSET")
    search.press("Enter")
    assert d.page.inner_text("#d-title") == "Find matches (2)"
    matches = d.page.locator("#d-tree button")
    assert matches.all_text_contents() == [
        "Select and inspect " + _LONG_NAME_A,
        "Select and inspect " + _LONG_NAME_B,
    ]
    assert _painted(d) == []
    matches.nth(1).focus()
    d.page.keyboard.press("Enter")
    assert d.page.inner_text("#d-title") == _LONG_NAME_B
    assert _selected_ids(d) == [_LONG_NAME_B]
    d.page.wait_for_function(
        "() => document.getElementById('name-callout').textContent.includes('SOUTH')"
    )
    assert d.errors == []


def test_compact_marks_survive_suppressed_names_and_nested_fold(driven_labels: Driven) -> None:
    """DL-384: OR/self-lock/member marks remain distinct from captions and fold in place."""
    d = driven_labels
    d.page.evaluate("() => {cy.zoom(.3);}")
    assert _label(d, _LONG_NAME_A) == "\u2228"
    assert _label(d, "FW_OR") == "\u2228"
    assert d.page.evaluate("() => decodeURIComponent(nodeMarks(cy.$id('FW_OR'))).includes('<rect')")
    assert d.page.evaluate(
        "(id) => decodeURIComponent(nodeMarks(cy.$id(id))).includes('<rect')", _LONG_NAME_B
    )
    assert d.page.evaluate("(id) => cy.$id(id).style('text-opacity')", _LONG_NAME_A) == "0"
    outside = _positions(d, ["P", "Q", "C", "D"])
    d.page.evaluate("() => {highlightNodes(cy.$id('P')); ec.collapse(cy.$id('IB'));}")
    d.page.wait_for_function(
        "() => foldPending === null && cy.$id('IB').hasClass('cy-expand-collapse-collapsed-node')"
    )
    assert _label(d, "IB") == "4"
    assert d.page.evaluate("() => nodeCaption(cy.$id('IB'))") == "IB"
    assert _positions(d, ["P", "Q", "C", "D"]) == outside
    _open_control(d, "#node-labels")
    d.page.get_by_label("Labels", exact=True).select_option("always")
    search = d.page.locator("#search")
    search.fill(_LONG_NAME_B)
    search.press("Enter")
    assert not _folded(d, "IB")
    assert d.page.inner_text("#d-title") == _LONG_NAME_B
    assert d.page.evaluate("() => cy.$id('P').hasClass('hl')")
    assert d.page.evaluate("(id) => cy.$id(id).style('text-opacity')", _LONG_NAME_A) == "1"
    assert _positions(d, ["P", "Q", "C", "D"]) == outside
    d.page.evaluate("() => {ec.collapse(cy.$id('IB')); cy.zoom(.3);}")
    _open_control(d, "#node-labels")
    d.page.get_by_label("Labels", exact=True).select_option("adaptive")
    d.page.evaluate("() => {ec.expand(cy.$id('IB'));}")
    assert d.page.evaluate("(id) => cy.$id(id).style('text-opacity')", _LONG_NAME_A) == "0"
    assert d.errors == []


def test_caption_area_does_not_steal_pan_or_selected_node_drag(driven_labels: Driven) -> None:
    """DL-384: captions are not gesture targets; the body retains selected group drag."""
    d = driven_labels
    d.page.evaluate(
        "(id) => {cy.zoom(1);const n=cy.$id(id);cy.pan({x:400-n.position('x'),"
        " y:420-n.position('y')});}",
        "P",
    )
    point = _client_point(d, "P")
    offset = [52, 0] if d.page.evaluate("DIRECTION") == "DOWN" else [0, 23]
    d.page.mouse.move(point["x"] + offset[0], point["y"] + offset[1])
    pan = d.page.evaluate("() => cy.pan()")
    d.page.mouse.down()
    d.page.mouse.move(point["x"] + offset[0] + 35, point["y"] + offset[1] + 32, steps=8)
    d.page.mouse.up()
    assert d.page.evaluate("() => cy.pan()") != pan
    assert _selected_ids(d) == []
    d.page.evaluate(
        "(ids) => {cy.nodes().filter(n=>ids.includes(n.id())).select();}",
        [_LONG_NAME_A, _LONG_NAME_B],
    )
    before = _positions(d, [_LONG_NAME_A, _LONG_NAME_B])
    point = _client_point(d, _LONG_NAME_A)
    d.page.mouse.move(**point)
    d.page.mouse.down()
    d.page.mouse.move(point["x"] + 31, point["y"] + 29, steps=8)
    d.page.mouse.up()
    after = _positions(d, [_LONG_NAME_A, _LONG_NAME_B])
    deltas = [[after[n][i] - before[n][i] for i in (0, 1)] for n in before]
    assert deltas[0] == pytest.approx(deltas[1])
    assert deltas[0] == pytest.approx([31, 29], abs=2)
    assert d.errors == []


def test_compact_caption_lock_obstacles_and_fit_ignore_attention(driven_locks: Driven) -> None:
    """DL-384: re-placing hubs reads the same caption bounds under zoom/selection/marks."""
    d = driven_locks
    d.page.evaluate("() => placeLocks()")
    before = _label_geometry(d)
    d.page.evaluate("() => {cy.nodes().select(); highlightNodes(cy.nodes());}")
    _open_control(d, "#node-labels")
    control = d.page.get_by_label("Labels", exact=True)
    for mode in ("always", "adaptive"):
        control.select_option(mode)
        d.page.evaluate("() => {cy.zoom(.2); placeLocks();}")
        assert _label_geometry(d) == before
    # Halo paint is excluded from the obstacle contract (DL-384).
    d.page.evaluate("() => {cy.nodes().unselect(); clearHighlights();}")
    assert _hub_overlaps(d) == []
    assert d.errors == []


def test_compact_caption_empty_box_keeps_top_landmark_and_native_rectangle(
    driven_labels: Driven,
) -> None:
    """DL-384: an empty expanded box owns a title even though it is childless."""
    d = driven_labels
    facts = d.page.evaluate(
        "(id) => {const n=cy.$id(id),k=n.scratch('elk'),l=k.labels[0],"
        " b=n.boundingBox({includeNodes:false,includeEdges:false,includeLabels:true,"
        " includeOverlays:false,includeUnderlays:false});return {expanded:expandedBox(n),"
        " parent:n.isParent(),placement:k.layoutOptions['elk.nodeLabels.placement'],"
        " delta:[b.x1-k.x-l.x,b.y1-k.y-l.y,b.w-l.width,b.h-l.height]};}",
        _EMPTY_BOX,
    )
    assert facts["expanded"] and not facts["parent"]
    assert facts["placement"] == "[OUTSIDE,H_CENTER,V_TOP]"
    assert facts["delta"] == pytest.approx([0, 0, 0, 0], abs=0.01)
    before = _label_geometry(d)
    d.page.evaluate("() => {cy.zoom(.2);}")
    assert d.page.evaluate("(id) => cy.$id(id).style('text-opacity')", _EMPTY_BOX) == "1"
    assert _label_geometry(d)["nodes"] == before["nodes"]
    assert d.errors == []


def test_compact_caption_pixel_budget_and_native_leaf_rectangles(driven_labels: Driven) -> None:
    """DL-384: wide glyphs keep both ends without a second ellipsis or port enlargement."""
    d = driven_labels
    facts = d.page.evaluate(
        "(ids) => ids.map(id=>{const n=cy.$id(id),k=n.scratch('elk'),l=k.labels[0],"
        " c=nodeCaption(n),b=n.boundingBox({includeNodes:false,includeEdges:false,"
        " includeLabels:true,includeOverlays:false,includeUnderlays:false});"
        " let x=k.x,y=k.y;n.ancestors().forEach(a=>{x+=a.scratch('elk').x;y+=a.scratch('elk').y;});"
        "return {caption:c,first:c[0]===n.data('label')[0],last:c.at(-1)===n.data('label').at(-1),"
        "width:b.w,budget:DIRECTION==='DOWN'?112:132,wrap:n.style('text-wrap'),"
        "body:[k.width,k.height],actualBody:[n.outerWidth(),n.outerHeight()],"
        "delta:[b.x1-x-l.x,b.y1-y-l.y,b.w-l.width,b.h-l.height]};})",
        [_CJK_NAME, _WIDE_NAME],
    )
    for fact in facts:
        assert fact["first"] and fact["last"] and "\u2026" in fact["caption"]
        assert fact["width"] <= fact["budget"] + 0.01
        assert fact["wrap"] == "none"
        assert fact["body"] == fact["actualBody"]
        assert fact["delta"] == pytest.approx([0, 0, 0, 0], abs=0.01)
    assert d.errors == []


# ------------------------------------------------------- DL-385 offline snapshots

_SNAPSHOT_TEXT = (
    _LABEL_TEXT
    + "\n"
    + S_EDGE_TEXT
    + "\ninsert_job: __proto__\njob_type: c\ncommand: true\n"
    + "\ninsert_job: observes_flag\njob_type: c\ncommand: true\ncondition: value(SNAPSHOT_FLAG) = 1\n"
)


@pytest.fixture(scope="module", params=[(e, d) for e in ENGINES for d in ("LR", "TD")])
def _driven_snapshot(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, _playwright: Any
) -> Any:
    engine, direction = request.param
    path = tmp_path_factory.mktemp("explore-snapshot") / "snapshot.html"
    path.write_text(
        to_explore_html(lower_source(_SNAPSHOT_TEXT), title="snapshot", direction=direction),
        encoding="utf-8",
    )
    yield from _open_driven(_playwright, engine, path.as_uri())


@pytest.fixture
def driven_snapshot(_driven_snapshot: Driven) -> Driven:
    return _reset(_driven_snapshot)


def _choose_snapshot(d: Driven, data: Any, name: str = "snapshot.json") -> None:
    _open_control(d, "#snapshot-import")
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    d.page.locator("#snapshot-file").set_input_files(
        {"name": name, "mimeType": "application/json", "buffer": text.encode()}
    )


def _import_snapshot(d: Driven, data: Any, name: str = "snapshot.json") -> None:
    generation = d.page.evaluate("snapshotGeneration")
    _choose_snapshot(d, data, name)
    d.page.wait_for_function(
        "generation => snapshotGeneration > generation && !snapshotPending", arg=generation
    )


def _snapshot_invariants(d: Driven) -> Any:
    return d.page.evaluate(
        "() => ({nodes:cy.nodes().union(foldedNodes()).map(n=>"
        " [n.id(),n.data('label'),n.data('kind'),n.data('parent'),n.position(),"
        " n.outerWidth(),n.outerHeight(),n.classes(),n.selected()]),"
        " edges:cy.edges().map(e=>[e.id(),e.data('source'),e.data('target'),e.classes(),"
        " e.data('originalEnds')?[endId(e.data('originalEnds').source),endId(e.data('originalEnds').target)]:null,"
        " e.scratch('_elkRoute')?.points]),"
        " epoch:[routeEpoch,routeLayoutEpoch,routesPresent],geometry:[...routeGeometry],"
        " fit:cy.getFitViewport(stableBounds(cy.nodes(':visible')),40),"
        " view:[cy.pan(),cy.zoom()],detailsClosed:document.getElementById('details').hidden,"
        " inspected:inspectedName?.id(),hovered:hoveredName?.id()})"
    )


def _snapshot_marker(d: Driven, node_id: str) -> str:
    return d.page.evaluate(
        'id=>decodeURIComponent(nodeMarks(cy.$id(id))).match(/data-observation="([^"]+)"/)?.[1] || \'\'',
        node_id,
    )


def _snapshot_filter_state(d: Driven, state: str) -> None:
    _open_control(d, "#snapshot-filter")
    d.page.locator("#snapshot-filter").select_option(
        label="State " + json.dumps(state, ensure_ascii=False) + " (1)"
    )


def test_snapshot_minimal_file_input_raw_values_and_metadata(driven_snapshot: Driven) -> None:
    """DL-385: native file input, arbitrary strings and metadata are text, never markup."""
    d = driven_snapshot
    _open_control(d, "#snapshot-import")
    with d.page.expect_file_chooser() as chooser:
        d.page.locator("#snapshot-import").click()
    chooser.value.set_files(
        {
            "name": "minimal.json",
            "mimeType": "application/json",
            "buffer": b'{"nodes":{"P":{"state":"queued"}}}',
        }
    )
    d.page.wait_for_function("() => snapshotLoaded && !snapshotPending")
    assert _snapshot_marker(d, "P") == "neutral"
    raw = {
        "source": ["example", {"__proto__": {"retained": True}}],
        "capture": False,
        "__proto__": {"retained": "top-level"},
        "nodes": {
            "P": {"state": "", "__proto__": {"retained": "node"}},
            "Q": {"state": " \t\n"},
            "M": {"state": "not supplied"},
            "N": {"state": '""'},
            "__proto__": {"state": "<svg onload=window.snapshotXss=1>"},
            "not-a-node": {
                "state": "unknown",
                "note": [None, "<img src=x onerror=window.snapshotXss=1>"],
            },
        },
    }
    _import_snapshot(d, raw)
    assert d.page.locator("#snapshot-error").is_hidden()
    assert (
        json.loads(d.page.evaluate("() => JSON.stringify(Object.fromEntries(snapshotNodes))"))
        == raw["nodes"]
    )
    assert json.loads(d.page.evaluate("JSON.stringify(snapshotMetadata)")) == {
        k: v for k, v in raw.items() if k != "nodes"
    }
    assert "Unknown IDs: 1" in d.page.inner_text("#snapshot-buckets")
    assert d.page.locator("#snapshot-panel img, #snapshot-panel svg").count() == 0
    assert d.page.evaluate("() => window.snapshotXss") is None
    for state in ("", " \t\n", "not supplied", '""'):
        _snapshot_filter_state(d, state)
        assert d.page.inner_text("#snapshot-find-count") == "1 eligible matches"
        assert json.dumps(state, ensure_ascii=False) in d.page.inner_text("#snapshot-results")
    d.page.locator("#snapshot-filter").select_option("missing")
    assert "not supplied" in d.page.inner_text("#snapshot-results")
    assert not any(
        text.startswith("M ·")
        for text in d.page.locator("#snapshot-results > details > summary").all_text_contents()
    )
    d.page.locator("#snapshot-metadata summary").click()
    assert json.loads(d.page.inner_text("#snapshot-metadata pre"))["__proto__"] == {
        "retained": "top-level"
    }
    assert d.errors == []


@pytest.mark.parametrize(
    "invalid",
    [
        "{",
        "[]",
        '{"nodes":[]}',
        '{"nodes":{"P":{"state":7}}}',
        '{"nodes":{"unknown":{"state":false}}}',
        '{"nodes":{"P":{}}}',
    ],
)
def test_snapshot_invalid_candidate_is_atomic(driven_snapshot: Driven, invalid: str) -> None:
    """DL-385: malformed entries cannot partially replace a prior valid observation set."""
    d = driven_snapshot
    _import_snapshot(d, {"nodes": {"P": {"state": "SU"}, "Q": {"state": "RU"}}})
    before = d.page.evaluate("() => [...snapshotNodes]")
    _import_snapshot(d, invalid)
    assert d.page.locator("#snapshot-error").is_visible()
    assert d.page.evaluate("() => [...snapshotNodes]") == before
    assert _snapshot_marker(d, "P") == "neutral"
    assert d.page.inner_text("#snapshot-notice") == "Previous snapshot kept."
    _import_snapshot(d, {"nodes": {"Q": {"state": ""}}})
    assert _snapshot_marker(d, "P") == ""
    assert _snapshot_marker(d, "Q") == "neutral"
    _import_snapshot(d, {"nodes": {}})
    assert d.page.inner_text("#snapshot-notice").startswith("Loaded 0 observations")
    assert _snapshot_marker(d, "Q") == ""
    assert d.page.locator("#snapshot-data").is_visible()
    d.page.locator("#snapshot-filter").select_option("missing")
    assert d.page.locator("#snapshot-select").is_enabled()
    assert d.errors == []


def test_snapshot_duplicate_keys_exact_external_join_and_ineligible_ids(
    driven_snapshot: Driven,
) -> None:
    """DL-385: native last-key-wins and emitted identity remain explicit and independent."""
    d = driven_snapshot
    d.page.evaluate("() => {cy.$id('_foo^PRD').emit('tap');}")
    assert d.page.locator("#d-rows").inner_text().count("_foo^PRD") == 1
    assert "emitted ID" in d.page.inner_text("#d-rows")
    edge_id = d.page.evaluate("() => cy.edges().not('.lock')[0].id()")
    _import_snapshot(d, '{"nodes":{"P":{"state":"first"},"P":{"state":"x","state":"last"}}}')
    assert d.page.evaluate("() => snapshotNodes.get('P').state") == "last"
    _import_snapshot(
        d,
        {
            "nodes": {
                "foo^PRD": {"state": "SU"},
                "_foo^PRD": {"state": "RU"},
                "FOO^PRD": {"state": "case differs"},
                edge_id: {"state": "edge"},
            }
        },
    )
    d.page.locator("#snapshot-preset").select_option("autosys")
    assert _snapshot_marker(d, "foo^PRD") == "success"
    assert _snapshot_marker(d, "_foo^PRD") == "running"
    assert "External matches: 1 · Unknown IDs: 1 · Ineligible IDs: 1" == d.page.inner_text(
        "#snapshot-buckets"
    )
    groups = d.page.locator("#snapshot-unmatched > details")
    groups.nth(0).locator(":scope > summary").click()
    groups.nth(0).locator("details > summary").click()
    assert json.loads(groups.nth(0).locator("pre").inner_text()) == {"state": "case differs"}
    groups.nth(1).locator(":scope > summary").click()
    assert groups.locator("button").count() == 0
    _import_snapshot(d, {"nodes": {edge_id: {"state": None}}})
    assert d.page.locator("#snapshot-error").is_visible()
    assert _snapshot_marker(d, "_foo^PRD") == "running"
    assert d.errors == []


def test_snapshot_read_generations_clear_errors_and_hidden_preference(
    driven_snapshot: Driven,
) -> None:
    """DL-385: controlled File.text promises expose ordering, without timing assumptions."""
    d = driven_snapshot
    _import_snapshot(d, {"nodes": {"P": {"state": "prior"}}})
    d.page.evaluate(
        "() => {window.snapshotReads=new Map();File.prototype.text=function(){"
        " return new Promise((resolve,reject)=>{snapshotReads.set(this.name,{resolve,reject});});};}"
    )
    _choose_snapshot(d, "", "old.json")
    _choose_snapshot(d, "", "new.json")
    d.page.evaluate(
        '() => {snapshotReads.get(\'new.json\').resolve(\'{"nodes":{"Q":{"state":"new"}}}\');}'
    )
    d.page.wait_for_function("() => !snapshotPending")
    d.page.evaluate("() => {snapshotReads.get('old.json').reject(new Error('stale error'));}")
    assert d.page.locator("#snapshot-error").is_hidden()
    assert d.page.evaluate("() => [...snapshotNodes.keys()]") == ["Q"]
    _choose_snapshot(d, "", "old-success.json")
    _choose_snapshot(d, "", "latest.json")
    d.page.evaluate(
        '() => {snapshotReads.get(\'latest.json\').resolve(\'{"nodes":{"Q":{"state":"latest"}}}\');}'
    )
    d.page.wait_for_function("() => !snapshotPending")
    d.page.evaluate(
        '() => {snapshotReads.get(\'old-success.json\').resolve(\'{"nodes":{"P":{"state":"stale"}}}\');}'
    )
    assert d.page.evaluate("() => snapshotNodes.get('Q').state") == "latest"
    assert not d.page.evaluate("() => snapshotNodes.has('P')")
    _choose_snapshot(d, "", "old2.json")
    _choose_snapshot(d, "", "bad.json")
    d.page.evaluate("() => {snapshotReads.get('bad.json').resolve('{');}")
    d.page.wait_for_function("() => !snapshotPending")
    error = d.page.inner_text("#snapshot-error")
    d.page.evaluate("() => {snapshotReads.get('old2.json').resolve('{\"nodes\":{}}');}")
    assert d.page.inner_text("#snapshot-error") == error
    assert d.page.evaluate("() => [...snapshotNodes.keys()]") == ["Q"]
    _choose_snapshot(d, "", "hidden.json")
    assert d.page.locator("#snapshot-error").is_hidden()
    d.page.locator("#snapshot-shown").uncheck()
    d.page.locator("#snapshot-preset").select_option("autosys")
    d.page.evaluate(
        '() => {snapshotReads.get(\'hidden.json\').resolve(\'{"nodes":{"P":{"state":"SU"}}}\');}'
    )
    d.page.wait_for_function("() => !snapshotPending")
    assert not d.page.locator("#snapshot-shown").is_checked()
    assert _snapshot_marker(d, "P") == ""
    for name, reject in (("cleared-success.json", False), ("cleared-error.json", True)):
        _choose_snapshot(d, "", name)
        d.page.locator("#snapshot-clear").click()
        d.page.evaluate(
            "({name,reject})=>{const r=snapshotReads.get(name);if(reject)r.reject(new Error('late'));"
            ' else r.resolve(\'{"nodes":{"P":{"state":"late"}}}\');}',
            {"name": name, "reject": reject},
        )
        assert d.page.evaluate("() => snapshotNodes.size") == 0
        assert d.page.locator("#snapshot-error").is_hidden()
        assert not d.page.locator("#snapshot-shown").is_checked()
        assert d.page.locator("#snapshot-preset").input_value() == "autosys"
    assert d.errors == []


def test_snapshot_geometry_routes_layers_and_observation_only_details(
    driven_snapshot: Driven,
) -> None:
    """DL-385: import and display changes touch neither graph state nor condition leaf hover."""
    d = driven_snapshot
    _edge_mode(d, "elk")
    d.page.evaluate(
        "(id)=>{cy.$id(id).emit('tap');highlightNodes(cy.$id('P'));cy.$id('N').addClass('hidden');"
        " cy.$id('Q').select();cy.zoom(.85);cy.center(cy.$id('IB'));"
        " window.snapshotLayouts=0;cy.on('layoutstart',()=>snapshotLayouts++);}",
        _LONG_NAME_A,
    )
    # Restore a valid route generation after the deliberately hidden node.
    _click(d, "#arrange")
    d.page.wait_for_function("() => !elkRunning")
    d.page.evaluate("() => {snapshotLayouts=0;}")
    _open_control(d, "#snapshot-import")
    leaf = d.page.locator("#d-tree button.leaf").first
    leaf.evaluate(
        "leaf=>{window.snapshotPointerLeaves=0;window.snapshotPaintCalls={};"
        " leaf.addEventListener('mouseleave',()=>snapshotPointerLeaves++);"
        " for(const name of ['showDetails','showNodeDetails','paintBranches','clearBranchPaint',"
        " 'renderCondTrees']){const fn=window[name];snapshotPaintCalls[name]=0;"
        " window[name]=function(...args){snapshotPaintCalls[name]++;return fn(...args);};}}"
    )
    leaf.hover()
    leaf_rect = leaf.bounding_box()
    condition_dom = d.page.inner_html("#d-tree")
    before = _snapshot_invariants(d)

    def assert_observation_only() -> None:
        d.page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>resolve()))")
        assert _snapshot_invariants(d) == before
        assert leaf.bounding_box() == leaf_rect
        assert d.page.inner_html("#d-tree") == condition_dom
        assert d.page.evaluate("snapshotPointerLeaves") == 0
        assert d.page.evaluate("() => Object.values(snapshotPaintCalls).every(n=>n===0)")

    _import_snapshot(d, {"nodes": {_LONG_NAME_A: {"state": "SU"}, "P": {"state": "arbitrary"}}})
    assert_observation_only()
    assert 'State: "SU"' in d.page.inner_text("#d-observation")
    _import_snapshot(d, {"nodes": {_LONG_NAME_A: {"state": "FA"}}})
    assert_observation_only()
    for _ in range(2):
        d.page.locator("#snapshot-shown").evaluate("el=>el.click()")
        assert_observation_only()
    for preset in ("autosys", "generic"):
        d.page.locator("#snapshot-preset").select_option(preset)
        assert_observation_only()
    d.page.locator("#snapshot-clear").evaluate("el=>el.click()")
    assert_observation_only()
    _import_snapshot(d, {"nodes": {_LONG_NAME_A: {"state": "SU"}, "P": {"state": "arbitrary"}}})
    assert_observation_only()
    d.page.locator("#snapshot-preset").hover()
    before = _snapshot_invariants(d)
    for preset in ("autosys", "generic"):
        d.page.locator("#snapshot-preset").select_option(preset)
        assert _snapshot_invariants(d) == before
    d.page.locator("#snapshot-shown").uncheck()
    assert d.page.locator("#d-observation").is_hidden()
    assert _snapshot_invariants(d) == before
    d.page.locator("#snapshot-shown").check()
    d.page.locator("#snapshot-filter").select_option("missing")
    assert _snapshot_invariants(d) == before
    d.page.locator("#snapshot-clear").click()
    assert _snapshot_invariants(d) == before
    assert d.page.inner_html("#d-tree") == condition_dom
    assert d.page.evaluate("snapshotLayouts") == 0
    assert d.errors == []


def test_snapshot_folds_leaf_scopes_and_explicit_keyboard_find(driven_snapshot: Driven) -> None:
    """DL-385: box own state is separate, nested leaf coverage is fold/hide invariant."""
    d = driven_snapshot
    _import_snapshot(
        d, {"nodes": {"B": {"state": "own box"}, "M": {"state": "done"}, "IM": {"state": ""}}}
    )
    _click(d, "#snapshot-panel [data-close-panel]")
    d.page.evaluate("() => {cy.$id('B').emit('tap');}")
    assert 'State: "own box"' in d.page.inner_text("#d-observation")
    assert "Leaf jobs: 2 of 6 supplied; 4 missing" in d.page.inner_text("#d-observation")
    d.page.evaluate("() => {ec.collapse(cy.$id('IB'));ec.collapse(cy.$id('B'));}")
    d.page.wait_for_function("() => foldPending === null")
    before = _snapshot_invariants(d)
    _import_snapshot(d, {"nodes": {"M": {"state": "done"}, "IM": {"state": ""}}})
    assert _snapshot_invariants(d) == before
    assert _snapshot_marker(d, "B") == ""
    assert "State: not supplied" in d.page.inner_text("#d-observation")
    assert "Leaf jobs: 2 of 6 supplied; 4 missing" in d.page.inner_text("#d-observation")
    _snapshot_filter_state(d, "")
    d.page.locator("#snapshot-select").focus()
    d.page.keyboard.press("Enter")
    d.page.wait_for_function("() => foldPending === null && cy.$id('IM').visible()")
    d.page.wait_for_function(
        "() => document.getElementById('stats').textContent.endsWith('snapshot: 1 selected')"
    )
    assert _selected_ids(d) == ["IM"]
    assert not _folded(d, "B") and not _folded(d, "IB")
    assert _snapshot_marker(d, "IM") == "neutral"
    assert 'State: ""' in d.page.inner_text("#d-observation")
    d.page.evaluate("() => {cy.$id('IB').emit('tap');}")
    assert "Leaf jobs: 1 of 4 supplied; 3 missing" in d.page.inner_text("#d-observation")
    _snapshot_filter_state(d, "done")
    d.page.evaluate(
        "() => {window.snapshotNavigationCount=0;window.snapshotReportCount=0;"
        " const navigate=navigateSnapshot,report=updateStats;"
        " navigateSnapshot=function(...args){snapshotNavigationCount++;return navigate(...args);};"
        " updateStats=function(op){report(op);"
        " if(typeof op==='string'&&op.startsWith('snapshot: '))snapshotReportCount++;};}"
    )
    for selector in ("#snapshot-select", '[data-snapshot-id="M"][data-snapshot-action="select"]'):
        if selector != "#snapshot-select":
            d.page.locator("#snapshot-results summary").click()
        button = d.page.locator(selector)
        for chord in ("Shift+Enter", "Shift+Space", "Shift+click"):
            d.page.evaluate("() => {cy.nodes().unselect();cy.$id('IM').select();}")
            navigation_count = d.page.evaluate("snapshotNavigationCount")
            if chord == "Shift+click":
                button.click(modifiers=["Shift"])
            else:
                button.focus()
                d.page.keyboard.press(chord)
            assert _selected_ids(d) == ["IM", "M"]
            d.page.wait_for_function("() => snapshotReportCount === snapshotNavigationCount")
            assert d.page.evaluate("snapshotNavigationCount") == navigation_count + 1
            button.focus()
            d.page.keyboard.press("Enter")
            assert _selected_ids(d) == ["M"]
            d.page.wait_for_function("() => snapshotReportCount === snapshotNavigationCount")
            assert d.page.evaluate("snapshotNavigationCount") == navigation_count + 2
    _snapshot_filter_state(d, "")
    d.page.locator("#snapshot-select").focus()
    d.page.keyboard.press("Enter")
    d.page.wait_for_function("() => snapshotReportCount === snapshotNavigationCount")
    assert _selected_ids(d) == ["IM"]
    d.page.evaluate(
        "() => {window.snapshotFitCount=0;const fit=cy.fit;"
        " cy.fit=function(...args){snapshotFitCount++;return fit.apply(this,args);};}"
    )
    for edge_mode in ("taxi", "elk"):
        _edge_mode(d, edge_mode)
        d.page.evaluate("() => {cy.$id('M').position({x:5000,y:5000}).addClass('hidden');}")
        d.page.wait_for_function("() => !cy.$id('M').visible()")
        if edge_mode == "elk":
            _arm_layout_wait(d)
            _click(d, "#arrange")
            _wait_layout_done(d)
            assert d.page.evaluate("() => routeLayoutEpoch === routeEpoch && routesPresent")
            assert d.page.evaluate("() => cy.edges().some(e => !!elkRoute(e))")
        d.page.evaluate("() => {cy.zoom(1);cy.pan({x:0,y:0});}")
        _snapshot_filter_state(d, "done")
        epoch = d.page.evaluate("routeEpoch")
        navigation_count = d.page.evaluate("snapshotNavigationCount")
        d.page.evaluate("snapshotFitCount=0")
        assert d.page.evaluate("() => !cy.$id('M').visible()")
        assert d.page.evaluate("() => cy.$id('M').renderedPosition().x > cy.width()")
        d.page.locator("#snapshot-highlight").click()
        d.page.wait_for_function("() => snapshotReportCount === snapshotNavigationCount")
        assert d.page.evaluate("snapshotNavigationCount") == navigation_count + 1
        assert d.page.evaluate("snapshotFitCount") == 1
        assert d.page.evaluate(
            "() => {const n=cy.$id('M'),p=n.renderedPosition();"
            " return n.visible()&&n.hasClass('hl')&&p.x>=0&&p.x<=cy.width()"
            " &&p.y>=0&&p.y<=cy.height();}"
        )
        assert "not drawn" not in d.page.inner_text("#stats")
        assert d.page.inner_text("#stats").endswith("snapshot: 1 highlighted")
        assert _selected_ids(d) == ["IM"]
        if edge_mode == "elk":
            assert d.page.evaluate("routeEpoch") > epoch
            assert d.page.evaluate("() => !cy.edges().some(e => !!elkRoute(e))")
    assert d.errors == []


def test_snapshot_callout_closed_edge_details_and_small_panel(driven_snapshot: Driven) -> None:
    """DL-385: overlay text follows existing attention, chrome remains bounded and escapable."""
    d = driven_snapshot
    _import_snapshot(d, {"nodes": {"P": {"state": "x" * 1000}, "B": {"state": "RU"}}})
    d.page.locator("#snapshot-panel [data-close-panel]").click()
    assert d.page.locator("#details").is_hidden()
    d.page.evaluate("() => {cy.zoom(1);cy.center(cy.$id('P'));}")
    d.page.mouse.move(**_client_point(d, "P"))
    d.page.wait_for_function(
        "() => document.getElementById('name-callout').textContent.includes('State:')"
    )
    assert d.page.inner_text("#name-callout").startswith("P\nState: ")
    assert d.page.locator("#details").is_hidden()
    bounds = d.page.locator("#name-callout").bounding_box()
    canvas = d.page.locator("#cy").bounding_box()
    assert bounds and canvas and bounds["width"] <= 360
    assert (
        d.page.locator("#name-callout").evaluate("el=>getComputedStyle(el).pointerEvents") == "none"
    )
    d.page.evaluate("() => {cy.edges().not('.lock')[0].emit('tap');}")
    edge_details = d.page.inner_html("#details")
    _import_snapshot(d, {"nodes": {"P": {"state": "replacement"}}})
    assert d.page.inner_html("#details") == edge_details
    d.page.set_viewport_size({"width": 360, "height": 300})
    bounds = d.page.locator("#snapshot-panel").bounding_box()
    canvas = d.page.locator("#cy").bounding_box()
    assert bounds and canvas
    assert bounds["width"] <= canvas["width"] and bounds["height"] <= canvas["height"]
    assert d.page.locator(".chrome-panel:visible").count() == 1
    d.page.keyboard.press("Escape")
    assert d.page.locator("#snapshot-panel").is_hidden()
    assert d.page.evaluate("document.activeElement.id") == "snapshot-toggle"
    assert d.errors == []


def test_snapshot_callout_avoids_details_without_graph_changes(driven_snapshot: Driven) -> None:
    """DL-386: feasible avoidance follows inspection, hover and native metadata toggles."""
    d = driven_snapshot
    observation = {"state": "review needed", "note": ["synthetic metadata"] * 30}
    _import_snapshot(d, {"nodes": {_LONG_NAME_A: observation, _LONG_NAME_B: observation}})
    d.page.locator("#snapshot-panel [data-close-panel]").click()
    _edge_mode(d, "elk")
    d.page.evaluate("(id) => {cy.zoom(.95);cy.center(cy.$id(id));}", _LONG_NAME_A)
    d.page.mouse.click(**_client_point(d, _LONG_NAME_A))
    d.page.mouse.move(**_canvas_point(d, 8, 8))
    d.page.wait_for_function(
        "() => !document.getElementById('name-callout').hidden && nameFrame === null"
    )

    def place_near_right(node_id: str, y: float) -> None:
        d.page.evaluate(
            "({id,y}) => {const n=cy.$id(id),z=cy.zoom();"
            " cy.pan({x:cy.width()-75-n.position('x')*z,y:y-n.position('y')*z});}",
            {"id": node_id, "y": y},
        )

    avoided = (
        "() => {const c=document.getElementById('name-callout').getBoundingClientRect(),"
        " d=document.getElementById('details').getBoundingClientRect();"
        " return nameFrame===null && !nameCallout.hidden && c.width>0 && c.height>0"
        " && c.right <= d.left-7.5;}"
    )
    place_near_right(_LONG_NAME_A, 150)
    d.page.wait_for_function(avoided)
    before = _snapshot_invariants(d)
    geometry = _label_geometry(d)
    d.page.evaluate("() => {scheduleNameCallout();}")
    d.page.wait_for_function(avoided)
    assert _snapshot_invariants(d) == before

    # A different real hovered body lies just below details; its preview reaches into it.
    hover_y = d.page.evaluate(
        "() => details.getBoundingClientRect().bottom-cy.container().getBoundingClientRect().top+35"
    )
    place_near_right(_LONG_NAME_B, hover_y)
    details_html = d.page.inner_html("#details")
    attention = (_selected_ids(d), _painted(d))
    d.page.mouse.move(**_client_point(d, _LONG_NAME_B))
    d.page.wait_for_function(
        "(id) => hoveredName?.id()===id && nameCallout.textContent.startsWith(id)",
        arg=_LONG_NAME_B,
    )
    d.page.wait_for_function(avoided)
    assert d.page.inner_html("#details") == details_html
    assert (_selected_ids(d), _painted(d)) == attention
    assert _label_geometry(d) == geometry
    d.page.mouse.move(**_canvas_point(d, 8, 8))
    d.page.wait_for_function("() => hoveredName===null && nameFrame===null")

    # Expanding metadata alone changes the panel rectangle, with no canvas event.
    metadata_y = d.page.evaluate(
        "() => details.getBoundingClientRect().bottom-cy.container().getBoundingClientRect().top"
        " + nameCallout.offsetHeight + 60"
    )
    place_near_right(_LONG_NAME_A, metadata_y)
    d.page.wait_for_function(
        "() => nameFrame===null && nameCallout.getBoundingClientRect().left"
        " > details.getBoundingClientRect().left"
    )
    before = _snapshot_invariants(d)
    d.page.locator("#d-observation summary").click()
    d.page.wait_for_function(avoided)
    assert d.page.locator("#d-observation details").evaluate("el=>el.open")
    assert _snapshot_invariants(d) == before
    d.page.locator("#d-observation summary").click()
    d.page.wait_for_function(
        "() => nameFrame===null && nameCallout.getBoundingClientRect().left"
        " > details.getBoundingClientRect().left"
    )

    _import_snapshot(d, {"nodes": {_LONG_NAME_A: {"state": "replacement"}}})
    d.page.locator("#snapshot-panel [data-close-panel]").click()
    d.page.wait_for_function("() => nameCallout.textContent.includes('replacement')")
    assert _snapshot_invariants(d) == before
    _click(d, "#snapshot-clear")
    d.page.wait_for_function("() => !nameCallout.textContent.includes('State:')")
    assert _snapshot_invariants(d) == before

    d.page.set_viewport_size({"width": 360, "height": 300})
    d.page.wait_for_function("() => cy.width()===360")
    place_near_right(_LONG_NAME_A, 50)
    d.page.wait_for_function(
        "() => nameFrame===null && !nameCallout.hidden && cy.width()===360"
        " && nameCallout.getBoundingClientRect().right"
        " <= cy.container().getBoundingClientRect().right-7.5"
    )
    bounds = d.page.locator("#name-callout").bounding_box()
    canvas = d.page.locator("#cy").bounding_box()
    panel = d.page.locator("#details").bounding_box()
    assert bounds and canvas and panel
    assert bounds["x"] + bounds["width"] > panel["x"]
    assert panel["x"] - canvas["x"] - 8 - bounds["width"] < 8
    assert bounds["x"] >= canvas["x"] + 8
    assert bounds["width"] <= canvas["width"] - 16
    assert bounds["y"] >= canvas["y"] + 8
    assert bounds["y"] + bounds["height"] <= canvas["y"] + canvas["height"] - 8
    assert (
        d.page.locator("#name-callout").evaluate("el=>getComputedStyle(el).pointerEvents") == "none"
    )
    assert d.page.locator("#name-link").evaluate("el=>getComputedStyle(el).pointerEvents") == "none"
    zoom = d.page.evaluate("() => cy.zoom()")
    d.page.mouse.move(bounds["x"] + 6, bounds["y"] + 6)
    d.page.mouse.wheel(0, -100)
    d.page.wait_for_function("z=>cy.zoom()!==z", arg=zoom)
    d.page.locator("#d-close").click()
    d.page.wait_for_function("() => nameCallout.hidden")
    d.page.set_viewport_size({"width": 1600, "height": 1000})
    d.page.wait_for_function("() => cy.width()===1600")
    assert _label_geometry(d) == geometry
    assert d.errors == []


@pytest.mark.parametrize("location", ("entry", "top"))
def test_snapshot_native_display_failure_retains_prior_model_and_ui(
    driven_snapshot: Driven,
    location: str,
    record_property: Any,
) -> None:
    """DL-385: native display capacity is checked before replacing a valid snapshot."""
    d = driven_snapshot
    _import_snapshot(d, {"source": "prior", "nodes": {"P": {"state": "prior"}}})
    before = _snapshot_invariants(d)
    coverage = d.page.inner_text("#snapshot-coverage")
    results = d.page.inner_html("#snapshot-results")
    value = "[" * 12000 + "0" + "]" * 12000
    deep = (
        '{"nodes":{"P":{"state":"new","metadata":' + value + "}}}"
        if location == "entry"
        else '{"new":true,"metadata":' + value + ',"nodes":{"P":{"state":"new"}}}'
    )
    # Separate native capacity evidence from the deterministic injected preparation failure.
    supported = d.page.evaluate(
        "({text,location})=>{const value=JSON.parse(text);try {"
        "JSON.stringify(location==='entry'?value.nodes.P:{new:true,metadata:value.metadata});return true;}"
        "catch(error){return false;}}",
        {"text": deep, "location": location},
    )
    record_property("native_parse_succeeded", True)
    record_property("native_stringify_succeeded", supported)
    if supported:
        # Native limits differ. A deterministic serializer failure exercises display preparation.
        d.page.evaluate(
            "() => {const stringify=JSON.stringify;JSON.stringify=function(value,...args){"
            " if(value?.state==='new'||value?.new===true)throw new RangeError('injected preparation failure');"
            " return stringify(value,...args);};}"
        )
    _import_snapshot(d, deep)
    assert "text-display capacity" in d.page.inner_text("#snapshot-error")
    assert d.page.evaluate("() => snapshotNodes.get('P').state") == "prior"
    assert d.page.inner_text("#snapshot-coverage") == coverage
    assert d.page.inner_html("#snapshot-results") == results
    assert _snapshot_invariants(d) == before
    assert d.page.inner_text("#snapshot-notice") == "Previous snapshot kept."
    assert d.errors == []


def test_snapshot_real_tab_order_and_long_text_stay_inside_panel(driven_snapshot: Driven) -> None:
    """DL-385: WebKit reaches every control and unbounded input cannot widen chrome."""
    d = driven_snapshot
    _open_control(d, "#snapshot-import")
    # The chooser wait is armed before the round trips that reach the control, because a busy
    # Chromium renderer can run a key event ahead of a just-sent file-chooser interception.
    with d.page.expect_file_chooser() as chooser:
        d.page.locator("#snapshot-panel [data-close-panel]").focus()
        d.page.keyboard.press("Tab")
        assert d.page.evaluate("document.activeElement.id") == "snapshot-import"
        d.page.keyboard.press("Enter")
    chooser.value.set_files(
        {
            "name": "keyboard.json",
            "mimeType": "application/json",
            "buffer": b'{"nodes":{"P":{"state":"known"}}}',
        }
    )
    d.page.wait_for_function("() => snapshotLoaded && !snapshotPending")
    d.page.locator("#snapshot-import").focus()
    for expected in (
        "snapshot-clear",
        "snapshot-shown",
        "snapshot-preset",
        "snapshot-filter",
        "snapshot-select",
        "snapshot-highlight",
    ):
        d.page.keyboard.press("Tab")
        assert d.page.evaluate("document.activeElement.id") == expected
    d.page.keyboard.press("Tab")
    assert d.page.evaluate("document.activeElement.tagName") == "SUMMARY"
    d.page.keyboard.press("Enter")
    d.page.keyboard.press("Tab")
    assert d.page.evaluate("document.activeElement.dataset.snapshotAction") == "select"
    d.page.keyboard.press("Tab")
    assert d.page.evaluate("document.activeElement.dataset.snapshotAction") == "highlight"
    d.page.set_viewport_size({"width": 360, "height": 300})
    _import_snapshot(d, {"nodes": {"P": {"state": "W" * 1000}}}, "F" * 1000 + ".json")
    dimensions = d.page.locator("#snapshot-panel").evaluate("el=>[el.clientWidth,el.scrollWidth]")
    assert dimensions[1] <= dimensions[0] + 1
    _import_snapshot(d, {"nodes": {"I" * 1000: {"state": None}}})
    assert d.page.locator("#snapshot-error").is_visible()
    dimensions = d.page.locator("#snapshot-panel").evaluate("el=>[el.clientWidth,el.scrollWidth]")
    assert dimensions[1] <= dimensions[0] + 1
    assert d.errors == []


def test_snapshot_lock_inspection_isolation_and_ineligible_raw_entries(
    driven_locks: Driven,
) -> None:
    """DL-385: neither lock hub nor lock-edge inspection is refreshed by observations."""
    d = driven_locks
    hub = "lock:r:R_ONE"
    edge = d.page.evaluate("() => cy.edges('.lock')[0].id()")
    for target in (hub, edge):
        d.page.evaluate("id=>{cy.$id(id).emit('tap');}", target)
        before = d.page.inner_html("#details")
        inspected = d.page.evaluate("() => cy.edges('.inspected').map(e=>e.id())")
        _import_snapshot(
            d,
            {
                "nodes": {
                    hub: {"state": "not a job"},
                    edge: {"state": "not a node"},
                    "lk_a": {"state": "SU"},
                }
            },
        )
        assert d.page.inner_html("#details") == before
        assert d.page.evaluate("() => cy.edges('.inspected').map(e=>e.id())") == inspected
        assert "Ineligible IDs: 2" in d.page.inner_text("#snapshot-buckets")
        assert _snapshot_marker(d, hub) == ""
        d.page.locator("#snapshot-shown").uncheck()
        assert d.page.inner_html("#details") == before
        d.page.locator("#snapshot-shown").check()
        d.page.locator("#snapshot-clear").click()
        assert d.page.inner_html("#details") == before
    assert d.errors == []


def test_snapshot_degraded_plugins_keep_native_import_and_find(driven_degraded: Driven) -> None:
    """DL-385: optional graph plugins do not own offline observations or native actions."""
    d = driven_degraded
    node_id = _sample_node(d)
    _import_snapshot(
        d, {"nodes": {node_id: {"state": "manual vocabulary"}}, "capture": {"note": "offline"}}
    )
    assert _snapshot_marker(d, node_id) == "neutral"
    assert "1 eligible matches" == d.page.inner_text("#snapshot-find-count")
    d.page.locator("#snapshot-select").focus()
    d.page.keyboard.press("Enter")
    assert _selected_ids(d) == [node_id]
    assert 'State: "manual vocabulary"' in d.page.inner_text("#d-observation")
    assert d.errors == []


def test_snapshot_reload_has_explicit_page_defaults(driven_snapshot: Driven) -> None:
    """DL-385: a new page has no snapshot and does not restore hidden/preset form history."""
    d = driven_snapshot
    _import_snapshot(d, {"nodes": {"P": {"state": "SU"}}})
    d.page.locator("#snapshot-shown").uncheck()
    d.page.locator("#snapshot-preset").select_option("autosys")
    d.page.reload()
    _ready(d)
    _open_control(d, "#snapshot-import")
    assert d.page.locator("#snapshot-shown").is_checked()
    assert d.page.locator("#snapshot-preset").input_value() == "generic"
    assert d.page.locator("#snapshot-data").is_hidden()
    assert _snapshot_marker(d, "P") == ""
    assert d.errors == []
