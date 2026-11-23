"""The terminal UI's explain pane says why a job has not started and goes to each cause
(DL-535; DL-534 for the `explain` fields, DL-532 for the selection).

The lines are a pure function of one answer (`explain_lines`, `downstream_lines`,
`dependency_names`), checked from fixture replies. The hop is checked on the real app over a
real control socket, with the two-second poll stopped and no journal wake-ups, so every
refresh is the test's or the hop's own. The test's client holds back an answer where a test
needs a wait: a hop lands only on a status answer asked after it began, so a withheld status
answer keeps the hop waiting, deterministically, while keys are pressed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

pytest.importorskip("textual")

from textual import events
from textual.widgets import Input, Static

from dsl41.display_time import format_local_at
from dsl41.runner_control import ControlClientError
from dsl41.runner_tui import (
    ConfirmScreen,
    PaneLine,
    RunnerApp,
    SpecScreen,
    _ExplainPane,
    _JobsTable,
    dependency_names,
    downstream_lines,
    explain_lines,
)
from test_runner_tui import (
    _SEND_KEYS,
    _held_jobs,
    _lines,
    _post_raw,
    _serve,
    _teardown,
    _history_poll_app,
    _wait_for_ui,
)

AS_OF = "2026-07-01T08:00:00"


def _reply(**fields: Any) -> dict[str, Any]:
    """A DL-534 `explain` answer for a plain job, with `fields` over it."""
    base: dict[str, Any] = {
        "ok": True,
        "job": "j",
        "condition": None,
        "satisfied": True,
        "atoms": [],
        "as_of": AS_OF,
        "attempt": "start",
        "disposition": "admit",
        "gates": [],
        "wake": [],
    }
    return base | fields


def _gate(kind: str, state: str, text: str, job: str | None = None, at: str | None = None):
    return {"kind": kind, "state": state, "text": text, "job": job, "at": at}


def _shown(lines: list[PaneLine]) -> list[tuple[str, str | None]]:
    return [(line.text.plain, line.target) for line in lines]


def _texts(lines: list[PaneLine]) -> list[str]:
    return [line.text.plain for line in lines]


# ---------------------------------------------------------------- lines from fixture replies


def test_dl535_the_instant_comes_first_and_is_always_shown() -> None:
    lines = explain_lines(_reply(), {})
    assert lines[0].text.plain == f"  as of {format_local_at(AS_OF)}"
    assert lines[0].target is None


@pytest.mark.parametrize(
    ("attempt", "disposition", "words"),
    [
        ("start", "admit", "a start now: would start"),
        ("start", "queue", "a start now: would queue for capacity"),
        ("start", "bypass", "a start now: would complete without running"),
        ("start", "defer", "a start now: would wait for its run_window to open"),
        ("start", "skip", "a start now: would be skipped"),
        ("start", "ignored", "a start now: would not start"),
        ("readmit", "fits", "a queue scan now: fits alone; the waiters ahead go first"),
        ("readmit", "short", "a queue scan now: does not fit"),
        ("readmit", "held", "a queue scan now: stays queued on hold"),
        ("readmit", "cancel", "a queue scan now: cancels it, its box is not running"),
        ("readmit", "leave", "a queue scan now: takes it out of the queue, its recheck fails"),
        (None, "running", "running"),
    ],
)
def test_dl535_each_disposition_is_said_in_words(
    attempt: str | None, disposition: str, words: str
) -> None:
    lines = explain_lines(_reply(attempt=attempt, disposition=disposition), {})
    assert lines[1].text.plain == f"  {words}"


def test_dl535_the_deciding_gate_comes_first_then_the_open_gates_then_the_context() -> None:
    """The chart's order is ice, hold, date, box: hold decides, ice is open before it, and the
    date and the box would also stop the attempt. The box gate names the box: a link."""
    gates = [
        _gate("ice", "open", "not on ice"),
        _gate("hold", "deciding", "on hold"),
        _gate("date", "context", "dated and not armed"),
        _gate("box", "context", "box 'B' is not running", job="B"),
    ]
    lines = explain_lines(_reply(disposition="ignored", gates=gates), {})
    assert _shown(lines[1:6]) == [
        ("  a start now: would not start", None),
        ("    deciding: on hold", None),
        ("    open: not on ice", None),
        ("    context: dated and not armed", None),
        ("›   context: box 'B' is not running", "B"),
    ]


def test_dl535_a_deferring_window_names_its_opening() -> None:
    at = "2026-07-01T10:00:00"
    gate = _gate("window", "deciding", "outside run_window: deferred to its next opening", at=at)
    lines = explain_lines(_reply(disposition="defer", gates=[gate]), {})
    assert lines[2].text.plain == (
        f"    deciding: outside run_window: deferred to its next opening at {format_local_at(at)}"
    )


def test_dl535_a_running_file_watch_says_what_it_watches() -> None:
    watching = {"file": "/in/f.dat", "interval": 30, "min_size": 5}
    lines = explain_lines(_reply(attempt=None, disposition="running", watching=watching), {})
    assert "    watching /in/f.dat every 30s, min_size 5" in _texts(lines)


def test_dl535_a_shortage_names_each_holder_and_a_spent_bucket_says_nobody_holds_it() -> None:
    capacity = {
        "admissible": False,
        "blockers": [],
        "short": [
            {
                "bucket": "r:R",
                "capacity": 2,
                "demand": 1,
                "used": 2,
                "short": 1,
                "spent": 0,
                "holders": [{"job": "h1", "units": 1}, {"job": "h2", "units": 1}],
            },
            {
                "bucket": "d:D",
                "capacity": 1,
                "demand": 1,
                "used": 1,
                "short": 1,
                "spent": 1,
                "holders": [],
            },
        ],
    }
    lines = explain_lines(_reply(disposition="queue", capacity=capacity), {})
    assert _shown(lines[3:]) == [
        ("  capacity: r:R short by 1: needs 1, 2 of 2 in use", None),
        ("›   held by h1: 1 unit", "h1"),
        ("›   held by h2: 1 unit", "h2"),
        ("  capacity: d:D short by 1: needs 1, 1 of 1 in use, 1 spent", None),
        ("    nobody holds it", None),
    ]


def test_dl535_blocking_waiters_and_waiters_ahead_are_links() -> None:
    capacity = {
        "admissible": False,
        "blockers": [{"job": "w1", "kind": "resource"}],
        "short": [],
        "ahead": [{"job": "w2", "buckets": ["r:R", "r:S"]}],
        "text": "a queue scan decides",
    }
    lines = explain_lines(_reply(attempt="readmit", disposition="short", capacity=capacity), {})
    assert _shown(lines[3:]) == [
        ("› capacity: blocked by waiter w1, higher priority (resource)", "w1"),
        ("› capacity: ahead in the queue: w2 (r:R, r:S)", "w2"),
        ("  capacity: a queue scan decides", None),
    ]


def test_dl535_a_demand_with_room_says_so() -> None:
    capacity = {"admissible": True, "blockers": [], "short": []}
    assert "  capacity: room for it now" in _texts(explain_lines(_reply(capacity=capacity), {}))


@pytest.mark.parametrize(
    ("dispatch", "text"),
    [
        (
            {"run_number": 3, "kind": "start", "wait": "drain", "attempt": None},
            "  dispatch: the start of run 3 waits for the engine's drain",
        ),
        (
            {"run_number": 3, "kind": "replacement", "wait": "cleanup", "attempt": "a-7"},
            "  dispatch: the replacement of run 3 waits for attempt a-7 to settle",
        ),
        (
            {
                "run_number": 3,
                "kind": "start",
                "wait": "update",
                "token": "update-1-of-two",
                "removes": True,
            },
            "  dispatch: the start of run 3 waits for update update-1-of-, which removes the job",
        ),
        (
            {
                "run_number": 3,
                "kind": "start",
                "wait": "update",
                "token": "update-2",
                "removes": False,
            },
            "  dispatch: the start of run 3 waits for update update-2",
        ),
    ],
)
def test_dl535_each_dispatch_wait_is_one_line(dispatch: dict[str, Any], text: str) -> None:
    lines = explain_lines(_reply(dispatch=dispatch), {})
    assert lines[-1].text.plain == text and lines[-1].target is None


def test_dl535_recovering_follows_the_instant() -> None:
    lines = explain_lines(_reply(recovering="the engine is recovering"), {})
    assert lines[1].text.plain == "  recovering: the engine is recovering"


def test_dl535_each_wake_kind_is_a_line_and_a_box_wake_links_the_box() -> None:
    tick = "2026-07-01T09:30:00"
    due = "2026-07-01T10:00:00"
    wakes = [
        {"kind": "tick", "text": "its next scheduled tick", "job": None, "at": tick},
        {"kind": "tick", "text": "none scheduled", "job": None, "at": None},
        {"kind": "deferred", "text": "its run_window-deferred start", "job": None, "at": due},
        {"kind": "condition", "text": "a job or global is updated", "job": None, "at": None},
        {"kind": "box", "text": "box 'B' starts", "job": "B", "at": None},
        {"kind": "scan", "text": "a queue scan", "job": None, "at": None},
        {"kind": "operator", "text": "only an operator's start", "job": None, "at": None},
    ]
    lines = explain_lines(_reply(wake=wakes), {})
    assert _shown(lines[-7:]) == [
        (f"  wake: its next scheduled tick at {format_local_at(tick)}", None),
        ("  wake: no scheduled tick", None),
        (f"  wake: its run_window-deferred start at {format_local_at(due)}", None),
        ("  wake: a job or global is updated", None),
        ("› wake: box 'B' starts", "B"),
        ("  wake: a queue scan", None),
        ("  wake: only an operator's start", None),
    ]


def _atom(atom: str, job: str | None, true: bool = False, **flags: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "atom": atom,
        "kind": "global" if job is None else "status",
        "job": job,
        "lookback": None,
        "guard": False,
        "self": False,
        "undefined": False,
        "external": False,
        "true": true,
    }
    return entry | flags


_ATOMS = [
    _atom("s(a, 01.00)", "a", lookback="01.00"),
    _atom("n(j)", "j", True, guard=True, self=True),
    _atom("n(a)", "a", True, guard=True),
    _atom("s(j)", "j", self=True),
    _atom("s(ghost)", "ghost", undefined=True),
    _atom("s(x^OTH)", "x^OTH", external=True),
    _atom("v(G) = go", None, actual=None),
]


def test_dl535_a_guard_a_self_an_undefined_and_an_external_atom_are_no_links() -> None:
    lines = explain_lines(_reply(condition="...", satisfied=False, atoms=_ATOMS), {})
    assert _shown(lines[2:]) == [
        ("  condition: ... (false)", None),
        ("›   ✘ s(a, 01.00)", "a"),
        ("    ✔ n(j)  [guard, self]", None),
        ("    ✔ n(a)  [guard]", None),
        ("    ✘ s(j)  [self]", None),
        ("    ✘ s(ghost)  [undefined]", None),
        ("    ✘ s(x^OTH)  [external]", None),
        ("    ✘ v(G) = go   (unset)", None),
    ]


def test_dl535_no_condition_is_said_and_a_held_job_s_hold_decides() -> None:
    gates = [_gate("ice", "open", "not on ice"), _gate("hold", "deciding", "on hold")]
    lines = explain_lines(_reply(disposition="ignored", gates=gates), {"on_hold": True})
    assert _texts(lines)[1:5] == [
        "  a start now: would not start",
        "    deciding: on hold",
        "    open: not on ice",
        "  no condition",
    ]


def test_dl535_an_answer_without_gates_is_today_s_text_with_no_links() -> None:
    legacy = {
        "ok": True,
        "job": "j",
        "condition": "s(a) & v(G) = go",
        "satisfied": False,
        "atoms": [
            {"atom": "s(a)", "true": True},
            {"atom": "v(G) = go", "true": False, "actual": "no"},
        ],
    }
    lines = explain_lines(legacy, {})
    assert _shown(lines) == [
        ("waiting on:", None),
        ("  s(a) & v(G) = go", None),
        ("", None),
        ("  ✔ s(a)", None),
        ("  ✘ v(G) = go   = 'no'", None),
    ]


@pytest.mark.parametrize(
    ("row", "text"),
    [
        ({}, "no condition -- starts on demand/schedule"),
        ({"on_hold": True}, "no condition -- on hold"),
        ({"on_ice": True}, "no condition -- on ice"),
    ],
)
def test_dl535_an_old_engine_s_job_with_no_condition_says_its_hold(
    row: dict[str, Any], text: str
) -> None:
    lines = explain_lines({"ok": True, "condition": None}, row)
    assert _shown(lines) == [(text, None)]


def test_dl535_the_downstream_lines_name_the_atom_and_link_all_but_the_job_itself() -> None:
    deps = {"downstream": ["k", "m", "src"], "upstream_atoms": []}
    atoms_of = {
        "k": [_atom("s(src)", "src"), _atom("v(G) = go", None)],
        "m": [_atom("n(src)", "src", guard=True)],
    }
    assert _shown(downstream_lines("src", deps, atoms_of)) == [
        ("  jobs whose condition names src:", None),
        ("›   k: s(src)", "k"),
        ("›   m: n(src) [guard]", "m"),
        ("    src: its own condition names it [self]", None),
    ]
    assert _shown(downstream_lines("src", {"downstream": []}, {})) == [
        ("  no job's condition names src", None)
    ]


def test_dl535_an_old_engine_s_downstream_lines_are_no_links() -> None:
    assert _shown(downstream_lines("src", {"downstream": ["k"]}, {})) == [
        ("  jobs whose condition names src:", None),
        ("    k", None),
    ]


def test_dl535_the_details_dependency_words_label_guards_and_self_references() -> None:
    assert dependency_names(_ATOMS) == {
        "needs": ["a", "ghost (undefined)", "x^OTH (external)", "v(G)"],
        "guard": ["n(j) (self)", "n(a)"],
        "self": ["s(j)"],
    }


def test_dl535_an_atom_line_and_a_wake_line_are_keyed_by_what_they_name() -> None:
    """A reordered condition gives each atom's line the key it had: the key names the atom,
    never its place."""
    first = [_atom("s(a)", "a"), _atom("s(b)", "b")]
    wakes = [
        {"kind": "condition", "text": "c", "job": None, "at": None},
        {"kind": "box", "text": "box 'B' starts", "job": "B", "at": None},
    ]
    one = explain_lines(_reply(condition="x", atoms=first, wake=wakes), {})
    two = explain_lines(_reply(condition="x", atoms=first[::-1], wake=wakes[::-1]), {})
    keys = {line.text.plain: line.key for line in one}
    assert {line.text.plain: line.key for line in two} == keys
    assert len(set(keys.values())) == len(keys)


def test_dl535_more_downstream_jobs_than_were_detailed_are_counted_in_one_line() -> None:
    deps = {"downstream": ["k", "m", "n"], "upstream_atoms": []}
    assert _shown(downstream_lines("src", deps, {"k": [_atom("s(src)", "src")]})) == [
        ("  jobs whose condition names src:", None),
        ("›   k: s(src)", "k"),
        ("›   m", "m"),
        ("›   n", "n"),
        ("    2 not detailed: their atoms were not read", None),
    ]


# ---------------------------------------------------------------- the hop, on the real app

#: a job at the top whose condition names a job two boxes deep, below thirty other jobs
_HOP_JIL = (
    "insert_job: a_src\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(zz_deep)\n\n"
    + "".join(f"insert_job: m_{i:02d}\njob_type: c\ncommand: x\nmachine: m1\n\n" for i in range(30))
    + "insert_job: zz_box\njob_type: b\n\n"
    + "insert_job: zz_inner\njob_type: b\nbox_name: zz_box\n\n"
    + "insert_job: zz_deep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: zz_inner\n"
)
_HOP_ORDER = ["a_src", *(f"m_{i:02d}" for i in range(30)), "zz_box", "zz_inner", "zz_deep"]

#: a chain a <- b <- c <- d, and g, whose guard names b
_CHAIN_JIL = (
    "insert_job: ch_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ch_b)\n\n"
    "insert_job: ch_b\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ch_c)\n\n"
    "insert_job: ch_c\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(ch_d)\n\n"
    "insert_job: ch_d\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: ch_g\njob_type: c\ncommand: x\nmachine: m1\ncondition: n(ch_b)\n"
)
_CHAIN_ORDER = ["ch_a", "ch_b", "ch_c", "ch_d", "ch_g"]


class _Client:
    """The test's side of the app's control client. `seen` lists every request. The last of
    `edits` rewrites the jobs of each status answer, and `rev`, when set, its configuration
    rev; `baseline`, when set, is the read header of each status and transitions answer. A verb in
    `refused` is answered ok:false, and one in `failing` gets no answer. `crafted` answers a
    job's `explain` with the answer its function gives. `hold(key)` withholds the answers to
    the requests `key` names, a verb or (verb, job), until `release(key)`; `release_one(key)`
    lets only those held now go on; `waiting(key)` counts those held now."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.seen: list[dict] = []
        self.edits: list = []
        self.rev: int | None = None
        self.baseline: str | None = None
        self.refused: set[str] = set()
        self.failing: set[str] = set()
        self.crafted: dict[str, Any] = {}
        self._gates: dict[Any, asyncio.Event] = {}
        self._waiting: dict[Any, int] = {}

    def hold(self, key: Any) -> None:
        self._gates[key] = asyncio.Event()

    def release(self, key: Any) -> None:
        self._gates.pop(key).set()

    def release_one(self, key: Any) -> None:
        gate, self._gates[key] = self._gates[key], asyncio.Event()
        gate.set()

    def waiting(self, key: Any) -> int:
        return self._waiting.get(key, 0)

    def asked(self, cmd: str, since: int = 0) -> list[str | None]:
        return [r.get("job") for r in self.seen[since:] if r.get("cmd") == cmd]

    async def request(self, request):
        self.seen.append(request)
        for key in (request.get("cmd"), (request.get("cmd"), request.get("job"))):
            gate = self._gates.get(key)
            if gate is not None:
                self._waiting[key] = self._waiting.get(key, 0) + 1
                try:
                    await gate.wait()
                finally:
                    self._waiting[key] -= 1
        cmd = request.get("cmd")
        if cmd in self.failing:
            raise ControlClientError("the test's engine did not answer", delivered=True)
        if cmd in self.refused:
            return {"ok": False, "error": "refused by the test"}
        if cmd == "explain" and request.get("job") in self.crafted:
            return self.crafted[request["job"]]()
        response = await self.inner(request)
        if cmd == "status" and response.get("ok"):
            if self.edits:
                response = {**response, "jobs": self.edits[-1](dict(response["jobs"]))}
            if self.rev is not None:
                response = {**response, "config_rev": self.rev}
        if cmd in ("status", "transitions") and self.baseline is not None and response.get("ok"):
            response = {**response, "baseline_id": self.baseline}
        return response


@contextlib.asynccontextmanager
async def _pane_app(short_root, text: str, order: list[str]):
    """The real app over `text` on a real engine and socket, the table focused, the poll
    stopped and no journal wake-ups."""
    engine, server, loop_task = await _serve(short_root / "run", text)
    try:
        app = RunnerApp(server.path)

        async def no_journal_wakeups() -> None:
            return

        app._follow_journal = no_journal_wakeups  # type: ignore[method-assign]
        set_interval = app.set_interval

        def controlled_interval(interval, callback, **kwargs):
            # the two-second poll is not started: every refresh is the test's or the hop's
            return None if callback == app._poll else set_interval(interval, callback, **kwargs)

        app.set_interval = controlled_interval  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == order)
            table = app.query_one("#jobs", _JobsTable)
            table.focus()
            client = _Client(app._client.request)
            app._client.request = client.request  # type: ignore[method-assign]
            yield app, pilot, table, server, client
    finally:
        await _teardown(engine, server, loop_task)


def _sent(client: _Client) -> list[dict]:
    return [r["payload"] for r in client.seen if r.get("cmd") == "sendevent"]


def _pane(app: RunnerApp) -> _ExplainPane:
    return app.query_one("#explain-box", _ExplainPane)


def _pane_text(app: RunnerApp) -> str:
    return str(app.query_one("#explain", Static).content)


async def _pick(pilot, app: RunnerApp, job: str) -> None:
    """The operator moves the table's cursor onto `job`, and its pane is answered."""
    table = app.query_one("#jobs", _JobsTable)
    table.move_cursor(row=app._table_sync.identity.index(job))
    await _wait_for_ui(pilot, lambda: app._selected == job and _pane(app).source == job)
    await _wait_for_ui(pilot, lambda: bool(_pane(app).lines))


async def _focus_pane(pilot, app: RunnerApp) -> None:
    await pilot.press("e")
    await _wait_for_ui(pilot, lambda: app.focused is _pane(app))


async def _cursor_to(pilot, app: RunnerApp, target: str) -> None:
    """Move the pane's cursor down onto the first link to `target`."""
    pane = _pane(app)
    for _ in range(len(pane.lines) + 1):
        current = pane.current()
        if current is not None and current.target == target:
            return
        await pilot.press("down")
        await pilot.pause()
    raise AssertionError(f"no link to {target}: {_shown(pane.lines)}")


async def _landed(pilot, app: RunnerApp, job: str) -> None:
    """A hop to `job` landed: it is selected, its row is the cursor's and painted on screen,
    and its own pane is answered."""
    table = app.query_one("#jobs", _JobsTable)
    await _wait_for_ui(
        pilot,
        lambda: app._hop is None and app._selected == job and _pane(app).source == job,
    )
    await _wait_for_ui(pilot, lambda: bool(_pane(app).lines))
    row = app._table_sync.identity.index(job)
    assert table.cursor_row == row and app._row_on_screen(table, row)
    painted = "".join(segment.text for segment in table.render_line(1 + row - int(table.scroll_y)))
    assert job in painted, painted


async def _hop(pilot, app: RunnerApp, target: str) -> None:
    await _cursor_to(pilot, app, target)
    await pilot.press("enter")
    await _landed(pilot, app, target)


async def _enter_held(pilot, app: RunnerApp, client: _Client, target: str) -> None:
    """Enter on the link to `target` with the status answer withheld: the hop is under way,
    its target picked, and it cannot land."""
    await _cursor_to(pilot, app, target)
    client.hold("status")
    await pilot.press("enter")
    await _wait_for_ui(pilot, lambda: client.waiting("status") == 1)
    assert app._hop is not None and app._hop.target == target and app._selected == target


def test_pilot_dl535_a_hop_unfolds_two_boxes_selects_the_job_on_screen_and_h_holds_it(
    short_root,
) -> None:
    """Both boxes above the target are folded and the source is at the top of a long table.
    The hop unfolds them, scrolls the target's row on screen and selects it, and `h` from the
    table holds the target and nothing else."""

    async def scenario() -> None:
        async with _pane_app(short_root, _HOP_JIL, _HOP_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await pilot.press("z")
            await _wait_for_ui(pilot, lambda: app._collapsed == {"zz_box", "zz_inner"})
            await _pick(pilot, app, "a_src")
            await _focus_pane(pilot, app)
            await _hop(pilot, app, "zz_deep")
            assert not app._collapsed & {"zz_box", "zz_inner"}
            assert table.scroll_y > 0  # the target's row was scrolled into view
            assert app._back == ["a_src"] and app.focused is _pane(app)
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: app.focused is table)
            assert app._selected == "zz_deep"
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["zz_deep"].get("on_hold"))
            assert _sent(client) == [{"job": "zz_deep"}]
            assert await _held_jobs(server) == {"zz_deep"}

    asyncio.run(scenario())


def test_pilot_dl535_a_hop_clears_a_filter_that_hides_the_target(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _HOP_JIL, _HOP_ORDER) as (app, pilot, _t, _s, _c):
            await pilot.press("slash", *"=a_*", "enter")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["a_src"])
            await _wait_for_ui(pilot, lambda: _pane(app).source == "a_src")
            await _wait_for_ui(pilot, lambda: bool(_pane(app).lines))
            await _focus_pane(pilot, app)
            await _hop(pilot, app, "zz_deep")
            assert app._filter == "" and app.query_one("#filterline", Input).value == ""
            assert _lines(app).count("filter /=a_*/ hides zz_deep; cleared") == 1

    asyncio.run(scenario())


def test_pilot_dl535_a_hop_changes_a_view_that_hides_the_target(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _HOP_JIL, _HOP_ORDER) as (app, pilot, _t, _s, client):
            # the source failed, so the problems view shows it alone
            client.edits.append(
                lambda jobs: {**jobs, "a_src": {**jobs["a_src"], "status": "FAILURE"}}
            )
            await app._refresh()
            await pilot.press("v")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["a_src"])
            await _wait_for_ui(pilot, lambda: _pane(app).source == "a_src")
            await _wait_for_ui(pilot, lambda: bool(_pane(app).lines))
            await _focus_pane(pilot, app)
            await _hop(pilot, app, "zz_deep")
            assert app._view_mode == 0
            assert _lines(app).count("view problems hides zz_deep; showing all jobs") == 1

    asyncio.run(scenario())


def test_pilot_dl535_backspace_returns_through_three_hops(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, _c):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            for target in ("ch_b", "ch_c", "ch_d"):
                await _hop(pilot, app, target)
            assert app._back == ["ch_a", "ch_b", "ch_c"]
            for back in ("ch_c", "ch_b", "ch_a"):
                await pilot.press("backspace")
                await _landed(pilot, app, back)
            assert app._back == []
            await pilot.press("backspace")
            await pilot.pause()
            assert _lines(app)[-1] == "back: no earlier job" and app._selected == "ch_a"

    asyncio.run(scenario())


def test_pilot_dl535_backspace_skips_a_job_an_update_removed(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            for target in ("ch_b", "ch_c", "ch_d"):
                await _hop(pilot, app, target)
            client.edits.append(
                lambda jobs: {name: row for name, row in jobs.items() if name != "ch_c"}
            )
            await app._refresh()
            await pilot.pause()
            await pilot.press("backspace")
            await _landed(pilot, app, "ch_b")
            assert _lines(app).count("back: ch_c is no longer a job; skipped") == 1
            assert app._back == ["ch_a"]

    asyncio.run(scenario())


# ---------------------------------------------------------------- the hop's wait


def test_pilot_dl535_keys_queued_behind_enter_act_on_nothing_until_the_hop_lands(
    short_root,
) -> None:
    """Behind Enter, while the hop waits for its status answer: Escape, then `h`, `d` and
    space from the table, and a console verb without a job name. Each says it is held and
    nothing is sent. Once the hop lands the same key and the same verb act on the target."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, "ch_b")
            client.hold("status")
            await pilot.press("enter", "escape", "h", "d", "space")
            await _wait_for_ui(pilot, lambda: client.waiting("status") == 1)
            await pilot.press("colon", *"ON_ICE", "enter")
            await pilot.pause()
            held = "held until ch_b is selected and shown"
            assert _lines(app)[-4:] == [
                f"ON_HOLD: {held}",
                f"details: {held}",
                f"fold: {held}",
                f"ON_ICE: {held}; nothing sent",
            ]
            assert _sent(client) == [] and not isinstance(app.screen, SpecScreen)
            assert app._hop is not None

            client.release("status")
            await _landed(pilot, app, "ch_b")
            table.focus()
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["ch_b"].get("on_hold"))
            await pilot.press("colon", *"ON_ICE", "enter")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["ch_b"].get("on_ice"))
            assert _sent(client) == [{"job": "ch_b"}, {"job": "ch_b"}]
            assert await _held_jobs(server) == {"ch_b"}

    asyncio.run(scenario())


def test_pilot_dl535_a_status_answer_asked_before_enter_does_not_land_the_hop(
    short_root,
) -> None:
    """A poll's status request is in flight when Enter starts the hop. Its answer is not one
    asked after the hop began: the hop stays unconfirmed and the keys stay held. The answer to
    the next request lands it."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, "ch_b")
            client.hold("status")
            app._poll()  # the two-second poll: its status is in flight before Enter
            await _wait_for_ui(pilot, lambda: client.waiting("status") == 1)
            await pilot.press("enter")
            await _wait_for_ui(pilot, lambda: app._hop is not None)
            asked = len(client.asked("status"))
            client.release_one("status")  # the answer to the request sent before Enter
            await _wait_for_ui(pilot, lambda: len(client.asked("status")) == asked + 1)
            await _wait_for_ui(pilot, lambda: client.waiting("status") == 1)
            await pilot.pause()
            assert app._hop is not None and not app._hop.confirmed
            await pilot.press("escape", "h")
            await pilot.pause()
            assert _lines(app)[-1] == "ON_HOLD: held until ch_b is selected and shown"
            assert _sent(client) == []
            client.release("status")
            await _landed(pilot, app, "ch_b")
            table.focus()
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["ch_b"].get("on_hold"))
            assert _sent(client) == [{"job": "ch_b"}]
            assert await _held_jobs(server) == {"ch_b"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("fault", "why"),
    [
        ("failing", "the engine did not answer"),
        ("baseline", "the baseline changed"),
        ("refused", "the status query was refused"),
    ],
)
def test_pilot_dl535_a_status_pass_that_ends_the_hop_leaves_the_keys_on_the_selection(
    short_root, fault: str, why: str
) -> None:
    """The status pass the hop waits for gets no answer, sees a new baseline, or is
    refused. The hop ends with its line, the selection stays as it is, and `h` then holds the
    selected job and nothing else."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _enter_held(pilot, app, client, "ch_b")
            if fault == "failing":
                client.failing.add("status")
            elif fault == "refused":
                client.refused.add("status")
            else:
                client.baseline = "another-period"
            client.release("status")
            line = f"go to ch_b stopped: {why}"
            await _wait_for_ui(pilot, lambda: line in _lines(app))
            assert app._hop is None and app._selected == "ch_b"
            assert _lines(app).count(line) == 1
            client.failing.clear()
            client.refused.clear()
            if client.baseline is not None:
                # the table was drawn from the faked header; the engine's own comes back first
                client.baseline = None
                await app._refresh()
            table.focus()
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: bool(_sent(client)))
            await _wait_for_ui(pilot, lambda: not [w for w in app.workers if w.group == "send"])
            assert _sent(client) == [{"job": "ch_b"}] and app._selected == "ch_b"
            assert await _held_jobs(server) == {"ch_b"}

    asyncio.run(scenario())


def test_pilot_dl535_a_hop_whose_row_never_comes_on_screen_gives_up(short_root) -> None:
    """The log is maximized while the hop waits, so the table is not on screen when the status
    answer comes: the hop looks for its row through ten refreshes, gives up with a line, and
    the keys act again."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _enter_held(pilot, app, client, "ch_b")
            await pilot.press("m")
            await _wait_for_ui(pilot, lambda: app.screen.maximized is not None)
            client.release("status")
            line = "go to ch_b stopped: its row did not come on screen after 10 refreshes"
            await _wait_for_ui(pilot, lambda: line in _lines(app))
            assert app._hop is None and _lines(app).count(line) == 1
            assert app._selected == "ch_b"

    asyncio.run(scenario())


def test_pilot_dl535_a_target_removed_during_the_hop_stops_it(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _enter_held(pilot, app, client, "ch_b")
            client.edits.append(
                lambda jobs: {name: row for name, row in jobs.items() if name != "ch_b"}
            )
            client.release("status")
            line = "go to ch_b stopped: it is no longer a job"
            await _wait_for_ui(pilot, lambda: line in _lines(app))
            assert app._hop is None and app._selected is None

    asyncio.run(scenario())


def test_pilot_dl535_a_target_hidden_during_the_hop_stops_it(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _enter_held(pilot, app, client, "ch_b")
            await pilot.press("slash", *"=ch_a")
            line = "go to ch_b stopped: it is hidden"
            await _wait_for_ui(pilot, lambda: line in _lines(app))
            assert app._hop is None and app._selected is None
            client.release("status")
            await _wait_for_ui(pilot, lambda: client.waiting("status") == 0)
            await pilot.pause()
            assert _lines(app).count(line) == 1

    asyncio.run(scenario())


def test_pilot_dl535_a_pick_in_the_table_during_the_hop_stops_it_and_stands(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _enter_held(pilot, app, client, "ch_b")
            await pilot.press("escape", "down")
            line = "go to ch_b stopped: another job was selected"
            await _wait_for_ui(pilot, lambda: line in _lines(app))
            assert app._hop is None and app._selected == "ch_c"
            client.release("status")
            await _wait_for_ui(pilot, lambda: client.waiting("status") == 0)
            await pilot.pause()
            assert app._selected == "ch_c" and app.focused is table
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["ch_c"].get("on_hold"))
            assert await _held_jobs(server) == {"ch_c"}

    asyncio.run(scenario())


# ---------------------------------------------------------------- which answer the pane shows


def test_pilot_dl535_a_late_answer_for_another_job_never_fills_the_pane(short_root) -> None:
    """A's answer is held back; B is selected and its answer is held back too. A's answer then
    arrives: the pane does not show it under B, and Enter there goes nowhere. B's answer then
    fills the pane."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            _server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            client.hold(("explain", "ch_a"))
            client.hold(("explain", "ch_b"))
            late = asyncio.ensure_future(app._update_explain())  # a refresh's ask for A
            await _wait_for_ui(pilot, lambda: client.waiting(("explain", "ch_a")) == 1)
            table.move_cursor(row=_CHAIN_ORDER.index("ch_b"))
            await _wait_for_ui(pilot, lambda: _pane(app).source == "ch_b")
            client.release(("explain", "ch_a"))
            await late
            await pilot.pause()
            pane = _pane(app)
            assert pane.source == "ch_b" and pane.lines == []
            assert pane.border_title == "explain: ch_b"
            assert "s(ch_b)" not in _pane_text(app)
            await _focus_pane(pilot, app)
            mark = len(_lines(app))
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected == "ch_b" and app._back == [] and app._hop is None
            assert _lines(app)[mark:] == [] and _sent(client) == []
            client.release(("explain", "ch_b"))
            await _wait_for_ui(pilot, lambda: any(line.target for line in _pane(app).lines))
            assert pane.current() is not None and pane.current().target == "ch_c"

    asyncio.run(scenario())


def _crafted(holders: list[str]) -> dict[str, Any]:
    """An `explain` answer for `src` that names `holders` as holders of R, in that order."""
    capacity = {
        "admissible": False,
        "blockers": [],
        "short": [
            {
                "bucket": "r:R",
                "capacity": len(holders),
                "demand": 1,
                "used": len(holders),
                "short": 1,
                "spent": 0,
                "holders": [{"job": job, "units": 1} for job in holders],
            }
        ],
    }
    gates = [_gate("capacity", "deciding", "short of capacity")]
    return _reply(job="src", disposition="queue", gates=gates, capacity=capacity)


def _crafted_atoms(names: list[str]) -> dict[str, Any]:
    """An `explain` answer for `src` whose condition's atoms name `names`, in that order."""
    atoms = [_atom(f"s({name})", name) for name in names]
    return _reply(job="src", condition=" & ".join(a["atom"] for a in atoms), atoms=atoms)


_PAIR_JIL = "".join(
    f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n"
    for name in ("h1", "h2", "h3", "src")
)
_PAIR_ORDER = ["h1", "h2", "h3", "src"]


def test_pilot_dl535_a_reply_for_another_job_at_the_current_generation_is_not_shown(
    short_root,
) -> None:
    """A refresh's ask for src is in flight. The operator picks h2, and h2's own ask has not
    started, so src's answer still answers the latest request. It is not shown: its job is no
    longer the selected job."""

    async def scenario() -> None:
        async with _pane_app(short_root, _PAIR_JIL, _PAIR_ORDER) as (app, pilot, table, _s, client):
            holders = [["h1"]]
            client.crafted["src"] = lambda: _crafted(holders[-1])
            await _pick(pilot, app, "src")
            client.hold(("explain", "src"))
            late = asyncio.ensure_future(app._update_explain())
            await _wait_for_ui(pilot, lambda: client.waiting(("explain", "src")) == 1)
            deferred: list = []
            run_worker = app.run_worker

            def defer_explain(work, *args, **kwargs):
                if kwargs.get("group") == "explain":
                    deferred.append(work)
                    return None
                return run_worker(work, *args, **kwargs)

            app.run_worker = defer_explain  # type: ignore[method-assign]
            table.move_cursor(row=_PAIR_ORDER.index("h2"))
            await _wait_for_ui(pilot, lambda: app._selected == "h2" and bool(deferred))
            gen = app._explain_gen
            holders.append(["h3"])
            client.release(("explain", "src"))
            await late
            await pilot.pause()
            assert app._explain_gen == gen  # no newer request: only the job check stops it
            assert "held by h3" not in _pane_text(app)
            app.run_worker = run_worker  # type: ignore[method-assign]
            run_worker(deferred[-1], group="explain", exclusive=True)
            await _wait_for_ui(pilot, lambda: _pane(app).source == "h2" and bool(_pane(app).lines))

    asyncio.run(scenario())


def test_pilot_dl535_an_older_answer_for_the_same_job_is_not_shown(short_root) -> None:
    """A refresh's causes ask for ch_b is in flight when `b` switches the pane to the
    downstream mode, whose answer comes first. The causes answer then arrives for the selected
    job, but it answers an older request, so the pane keeps the downstream lines."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "ch_b")
            await _focus_pane(pilot, app)
            client.hold(("explain", "ch_b"))
            late = asyncio.ensure_future(app._update_explain())
            await _wait_for_ui(pilot, lambda: client.waiting(("explain", "ch_b")) == 1)
            await pilot.press("b")
            pane = _pane(app)
            downstream = [
                ("  jobs whose condition names ch_b:", None),
                ("›   ch_a: s(ch_b)", "ch_a"),
                ("›   ch_g: n(ch_b) [guard]", "ch_g"),
            ]
            await _wait_for_ui(pilot, lambda: _shown(pane.lines) == downstream)
            client.release(("explain", "ch_b"))
            await late
            await pilot.pause()
            assert app._selected == "ch_b" and _shown(pane.lines) == downstream
            assert pane.border_title == "downstream: ch_b"

    asyncio.run(scenario())


def test_pilot_dl535_enter_on_lines_of_a_job_no_longer_selected_changes_nothing(
    short_root,
) -> None:
    """The operator picks ch_b in the table and goes back to the pane before ch_b's ask has
    started, so the pane still shows ch_a's lines. Enter on ch_a's link changes nothing and
    says why."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, table, _s, _c):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, "ch_b")
            deferred: list = []
            run_worker = app.run_worker

            def defer_explain(work, *args, **kwargs):
                if kwargs.get("group") == "explain":
                    deferred.append(work)
                    return None
                return run_worker(work, *args, **kwargs)

            app.run_worker = defer_explain  # type: ignore[method-assign]
            await pilot.press("escape", "down")
            await _wait_for_ui(pilot, lambda: app._selected == "ch_b" and bool(deferred))
            await _focus_pane(pilot, app)
            assert _pane(app).source == "ch_a" and _pane(app).current().target == "ch_b"
            await pilot.press("enter")
            await pilot.pause()
            assert _lines(app)[-1] == (
                "go to ch_b: the pane no longer shows the selected job's answer; nothing changed"
            )
            assert app._hop is None and app._back == [] and app._selected == "ch_b"
            app.run_worker = run_worker  # type: ignore[method-assign]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("answers", "on", "then"),
    [
        ([["h1", "h2", "h3"], ["h3", "h1", "h2"]], "h2", "h2"),
        ([["h1", "h2", "h3"], ["h3", "h1"]], "h2", "h3"),
    ],
    ids=["reordered", "gone"],
)
def test_pilot_dl535_a_refresh_that_reorders_the_holders_keeps_the_cursor_on_its_line(
    short_root, answers: list[list[str]], on: str, then: str
) -> None:
    """The cursor is on h2's line, the second link. A refresh reorders the holders, so the
    first link is h3's and the second h1's: the cursor stays on h2's line. If h2 is gone it
    moves to the first link. Enter goes to the job the highlighted line names."""

    async def scenario() -> None:
        async with _pane_app(short_root, _PAIR_JIL, _PAIR_ORDER) as (app, pilot, _t, _s, client):
            shown = answers[:1]
            client.crafted["src"] = lambda: _crafted(shown[-1])
            await _pick(pilot, app, "src")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, on)
            shown.append(answers[1])
            await app._refresh()
            await pilot.pause()
            assert _pane(app).current() is not None and _pane(app).current().target == then
            await pilot.press("enter")
            await _landed(pilot, app, then)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("answers", "then"),
    [([["h1", "h2"], ["h2", "h1"]], "h1"), ([["h1", "h2"], ["h2"]], "h2")],
    ids=["reordered", "gone"],
)
def test_pilot_dl535_a_reordered_condition_keeps_the_cursor_on_its_atom(
    short_root, answers: list[list[str]], then: str
) -> None:
    """The cursor is on s(h1), the first atom. An update reorders the condition to
    `s(h2) & s(h1)`: the cursor stays on s(h1), now the second line. With s(h1) gone it moves
    to the first link."""

    async def scenario() -> None:
        async with _pane_app(short_root, _PAIR_JIL, _PAIR_ORDER) as (app, pilot, _t, _s, client):
            shown = answers[:1]
            client.crafted["src"] = lambda: _crafted_atoms(shown[-1])
            await _pick(pilot, app, "src")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, "h1")
            shown.append(answers[1])
            await app._refresh()
            await pilot.pause()
            current = _pane(app).current()
            assert current is not None and current.target == then
            assert f"s({then})" in current.text.plain
            await pilot.press("enter")
            await _landed(pilot, app, then)

    asyncio.run(scenario())


def _many_atoms() -> dict[str, Any]:
    """An answer for `src` with forty atoms, each a link, more than the pane shows."""
    atoms = [_atom(f"s(h{i % 3 + 1}, {i:02d}.00)", f"h{i % 3 + 1}") for i in range(40)]
    return _reply(job="src", condition="a long condition", atoms=atoms)


def test_pilot_dl535_the_paging_keys_move_the_cursor_with_the_view(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _PAIR_JIL, _PAIR_ORDER) as (app, pilot, _t, _s, client):
            client.crafted["src"] = _many_atoms
            await _pick(pilot, app, "src")
            await _focus_pane(pilot, app)
            pane = _pane(app)
            for key in ("pagedown", "pagedown", "end", "pageup", "home"):
                before = pane.cursor
                await pilot.press(key)
                await pilot.pause()
                # each key moved the view past the cursor's line, so the cursor moved with it
                assert pane.cursor_on_screen() and pane.cursor != before, key
            assert pane.scroll_y == 0
            await pilot.press("end")
            await pilot.pause()
            target = pane.current().target
            await pilot.press("enter")
            await _landed(pilot, app, target)

    asyncio.run(scenario())


def test_pilot_dl535_enter_on_a_line_the_wheel_scrolled_away_brings_it_back_first(
    short_root,
) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _PAIR_JIL, _PAIR_ORDER) as (app, pilot, _t, _s, client):
            client.crafted["src"] = _many_atoms
            await _pick(pilot, app, "src")
            await _focus_pane(pilot, app)
            pane = _pane(app)
            cursor = pane.cursor
            region = pane.region
            point = (region.x + 2, region.y + 2)
            for _ in range(30):
                if not pane.cursor_on_screen():
                    break
                await _post_raw(pilot, events.MouseScrollDown, point, button=0)
            assert not pane.cursor_on_screen() and pane.cursor == cursor
            mark = len(_lines(app))
            await pilot.press("enter")
            await pilot.pause()
            assert pane.cursor_on_screen() and app._selected == "src" and app._hop is None
            assert _lines(app)[mark:] == []
            target = pane.current().target
            await pilot.press("enter")
            await _landed(pilot, app, target)

    asyncio.run(scenario())


def test_pilot_dl535_a_target_removed_before_enter_changes_nothing(short_root) -> None:
    """The link to ch_b is on screen; a status answer then drops ch_b. Enter writes one line
    and changes nothing, and `h` from the table holds ch_a, the selection, only."""

    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            await _cursor_to(pilot, app, "ch_b")
            client.edits.append(
                lambda jobs: {name: row for name, row in jobs.items() if name != "ch_b"}
            )
            await app._refresh()
            await pilot.pause()
            assert _pane(app).current() is not None and _pane(app).current().target == "ch_b"
            mark = len(_lines(app))
            await pilot.press("enter")
            await pilot.pause()
            assert _lines(app)[mark:] == ["go to ch_b: ch_b is no longer a job; nothing changed"]
            assert app._selected == "ch_a" and app._hop is None and app._back == []
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: app.focused is table)
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["ch_a"].get("on_hold"))
            assert _sent(client) == [{"job": "ch_a"}]
            assert await _held_jobs(server) == {"ch_a"}

    asyncio.run(scenario())


def test_pilot_dl535_the_table_s_action_keys_send_nothing_from_the_pane(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (
            app,
            pilot,
            _table,
            server,
            client,
        ):
            await _pick(pilot, app, "ch_a")
            await _focus_pane(pilot, app)
            assert len(_SEND_KEYS) == 9
            for key, _verb in _SEND_KEYS:
                await pilot.press(key)
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert app.focused is _pane(app)
            assert _sent(client) == [] and not [w for w in app.workers if w.group == "send"]
            assert await _held_jobs(server) == set()

    asyncio.run(scenario())


def test_pilot_dl535_the_downstream_mode_links_hops_and_goes_back(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _CHAIN_JIL, _CHAIN_ORDER) as (app, pilot, _t, _s, _c):
            await _pick(pilot, app, "ch_b")
            await _focus_pane(pilot, app)
            await pilot.press("b")
            pane = _pane(app)
            await _wait_for_ui(
                pilot, lambda: pane.mode == "downstream" and any(line.target for line in pane.lines)
            )
            assert pane.border_title == "downstream: ch_b"
            assert _shown(pane.lines) == [
                ("  jobs whose condition names ch_b:", None),
                ("›   ch_a: s(ch_b)", "ch_a"),
                ("›   ch_g: n(ch_b) [guard]", "ch_g"),
            ]
            await _hop(pilot, app, "ch_a")
            assert _shown(pane.lines) == [("  no job's condition names ch_a", None)]
            await pilot.press("backspace")
            await _landed(pilot, app, "ch_b")
            await pilot.press("b")
            await _wait_for_ui(
                pilot, lambda: pane.mode == "causes" and pane.border_title == "explain: ch_b"
            )

    asyncio.run(scenario())


_FAN_JIL = "insert_job: rc_src\njob_type: c\ncommand: x\nmachine: m1\n\n" + "".join(
    f"insert_job: rc_d{i}\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(rc_src)\n\n"
    for i in range(1, 4)
)
_FAN_ORDER = ["rc_d1", "rc_d2", "rc_d3", "rc_src"]


def test_pilot_dl535_the_downstream_detail_is_asked_once_per_job_and_revision(
    short_root,
) -> None:
    """Entering the mode asks `deps` for the job and once for each of its three downstream
    jobs. Three refreshes then ask one `deps` each, for the job. A new configuration rev asks
    for the detail again, once."""

    async def scenario() -> None:
        async with _pane_app(short_root, _FAN_JIL, _FAN_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "rc_src")
            await _focus_pane(pilot, app)
            mark = len(client.seen)
            await pilot.press("b")
            pane = _pane(app)
            await _wait_for_ui(pilot, lambda: len([ln for ln in pane.lines if ln.target]) == 3)
            assert client.asked("deps", mark) == ["rc_src", "rc_d1", "rc_d2", "rc_d3"]
            mark = len(client.seen)
            for _ in range(3):
                await app._refresh()
            assert client.asked("deps", mark) == ["rc_src"] * 3
            assert client.asked("explain", mark) == []
            mark = len(client.seen)
            client.rev = 99
            await app._refresh()
            await app._refresh()
            assert client.asked("deps", mark) == ["rc_src", "rc_d1", "rc_d2", "rc_d3", "rc_src"]
            assert "  rc_d1: s(rc_src)" in _texts(pane.lines)[1]

    asyncio.run(scenario())


def test_pilot_dl535_a_new_baseline_asks_the_downstream_detail_again(short_root) -> None:
    """A viewer stays attached across an engine restart on a fresh root that reports the same
    configuration rev: the new baseline asks for the detail again, once."""

    async def scenario() -> None:
        async with _pane_app(short_root, _FAN_JIL, _FAN_ORDER) as (app, pilot, _t, _s, client):
            client.rev = 7
            await app._refresh()
            await _pick(pilot, app, "rc_src")
            await _focus_pane(pilot, app)
            await pilot.press("b")
            pane = _pane(app)
            await _wait_for_ui(pilot, lambda: len([ln for ln in pane.lines if ln.target]) == 3)
            await app._refresh()
            mark = len(client.seen)
            client.baseline = "fresh-root"
            await app._refresh()  # the table is drawn from the new header in this pass (DL-541)
            assert app._baseline == "fresh-root" and app._config_rev == 7
            assert client.asked("deps", mark) == ["rc_src", "rc_d1", "rc_d2", "rc_d3"]
            await app._refresh()
            assert client.asked("deps", mark) == ["rc_src", "rc_d1", "rc_d2", "rc_d3", "rc_src"]

    asyncio.run(scenario())


_HUB_JIL = "insert_job: hub\njob_type: c\ncommand: x\nmachine: m1\n\n" + "".join(
    f"insert_job: sp_{i:02d}\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(hub)\n\n"
    for i in range(52)
)
_HUB_ORDER = ["hub", *(f"sp_{i:02d}" for i in range(52))]


def test_pilot_dl535_the_downstream_detail_stops_at_fifty_jobs(short_root) -> None:
    async def scenario() -> None:
        async with _pane_app(short_root, _HUB_JIL, _HUB_ORDER) as (app, pilot, _t, _s, client):
            await _pick(pilot, app, "hub")
            await _focus_pane(pilot, app)
            mark = len(client.seen)
            await pilot.press("b")
            pane = _pane(app)
            await _wait_for_ui(pilot, lambda: len([ln for ln in pane.lines if ln.target]) == 52)
            assert client.asked("deps", mark) == ["hub", *(f"sp_{i:02d}" for i in range(50))]
            texts = _texts(pane.lines)
            assert texts[-1] == "    2 not detailed: their atoms were not read"
            assert texts[-3:-1] == ["›   sp_50", "›   sp_51"]
            mark = len(client.seen)
            await app._refresh()
            assert client.asked("deps", mark) == ["hub"]

    asyncio.run(scenario())


def test_pilot_dl535_an_old_engine_s_pane_is_today_s_text_with_no_links(short_root) -> None:
    """The scripted engine answers `explain` without `gates` and with `condition: null`, for
    a job that is then put on hold."""

    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            pane = _pane(app)
            await _wait_for_ui(pilot, lambda: bool(pane.lines))
            assert _shown(pane.lines) == [("no condition -- starts on demand/schedule", None)]
            replies.status["jobs"]["trace_job"]["on_hold"] = True
            await pilot.press("r")
            await _wait_for_ui(
                pilot, lambda: _shown(pane.lines) == [("no condition -- on hold", None)]
            )
            await _focus_pane(pilot, app)
            assert pane.current() is None
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected == "trace_job" and app._back == []

    asyncio.run(scenario())


def test_pilot_dl535_the_details_label_a_guard_and_a_self_reference(short_root) -> None:
    text = (
        "insert_job: dp_up\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: dp_m\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(dp_up) | n(dp_m) | s(dp_m) | s(dp_ghost)\n\n"
        "insert_job: dp_k\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(dp_m)\n"
    )

    async def scenario() -> None:
        async with _pane_app(short_root, text, ["dp_k", "dp_m", "dp_up"]) as (
            app,
            pilot,
            _t,
            _s,
            _c,
        ):
            await _pick(pilot, app, "dp_m")
            await pilot.press("d")
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
            body = str(app.screen.query_one("#specbox Static", Static).content)
            assert "needs:   dp_up, dp_ghost (undefined)\n" in body
            assert "guard:   n(dp_m) (self)\n" in body
            assert "self:    s(dp_m)\n" in body
            assert "blocks:  dp_k\n" in body

    asyncio.run(scenario())
