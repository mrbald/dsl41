"""ss11 Textual TUI (phase 11d): one thin app, terminal today, web via
textual-serve in 11e.

The app is a client of the control socket ONLY -- never in-process with the
engine (DL-41 item 8: textual-serve spawns one app instance per browser
session, so an in-process engine would hand every viewer a private
universe). Terminal attachment is `dsl41 ui --socket <run_root>/control.sock`
against any running engine, or `dsl41 run --ui`, which starts the engine and
runs this app in the same terminal (quitting the app stops that run -- the
engine is tethered to the process; a detached engine takes viewers via
`dsl41 ui` instead).

Normative detail for DL-46, within DL-41's frame:

- CHANGE FEED, CONSUMED IDEMPOTENTLY (DL-45 item 5): a `subscribe`
  connection is used purely as a wake-up signal -- every record, seq'd or
  not, schedules a state refresh and is never rendered directly, so the
  at-least-once dispatch/drop race window costs a redundant refresh, not a
  duplicated row. What the user SEES always comes from the idempotent
  queries: the jobs table from `status`, the running commentary from
  `trace --since` (trace seqs are stable positions), the explain pane from
  `explain`. A 2s polling interval backstops a lost subscription; the
  subscription itself reconnects with backoff while the socket is down.
- JOBS TABLE = the ss10 status response verbatim: status, status_at,
  run_number, exit_code, ice/hold/noexec flags, plus the DL-46 additions --
  `pending_timers` (the oracle's own liveness filter; display truth is the
  dispatch truth) and per-run `log_out`/`log_err`. Alarm counts are counted
  off MUST_START_ALARM / MUST_COMPLETE_ALARM trace transitions -- the
  oracle's trace is the only alarm authority, the TUI just tallies it.
- LOG TAIL is a byte tail of the CURRENT run's resolved ss6 append target
  (`log_out`/`log_err` from status; `o` toggles the stream), read from the
  local filesystem: the TUI runs on the engine host in both postures
  (terminal; textual-serve serves FROM the host, E3), so file reads need no
  protocol verb. It starts near the tail (last 8 KiB), follows appends, and
  resets on truncation -- smoke-grade, not line-perfect. std_* paths carry
  verbatim (DL-39): a RELATIVE std file resolves against the viewer's cwd,
  guaranteed to match the wrapper's only under `run --ui` (shared cwd).
- LOG PAGER (DL-67): whenever the log tail has FOCUS -- which `m` grants --
  it is a less-style pager. Focus is the mode switch: textual consults the
  focused widget's bindings before the app's, so one mechanism provides the
  pager verbs AND shadows the operator verbs (`k` scrolls up, never
  KILLJOB; `f`/space page forward, never FORCE; `q`/escape leave the pager,
  never the app; verbs with no pager meaning ring the bell, as less does).
  Paging never mutates the estate. `/` and `?` search (regex, smartcase,
  all matches reverse-video, `n`/`N` wrap in the less directions), `&`
  shows only matching lines -- a view of the buffer, never a loss; an empty
  submit clears, escape cancels the prompt only (ESCAPE_TO_MINIMIZE is off
  so textual cannot swallow it first). Follow is pinned-at-bottom:
  scrolling up pauses ([paused] in the title), `F`/`G`/End resume. Buffer,
  filter view, and widget cap at _PAGER_BUFFER_LINES in lockstep; `m`
  maximizes the log PANE (tail + prompt line) so the prompt stays visible
  while zoomed. `m`/`o`/`r` and the resize keys pass through by design.
- EVENT CONSOLE accepts exactly the ss10 sendevent verbs (job verbs;
  SET_GLOBAL NAME=value; CHANGE_STATUS [job] STATUS [exit_code]); an
  omitted job means the selected row. Key bindings fire the common verbs on
  the selected job. Every request and its response is echoed to the
  console; refusals render red and change nothing -- the server already
  validates against the catalog (vendor parity), the TUI never pre-judges.
- TRIGGERS VIEW (`t`, DL-68): "what fires next" -- the ss10 `timers` verb
  (pending oracle timers merged with each scheduled job's next calendar
  tick, due-ordered by the server) re-queried on a 2s interval while open,
  each row with an absolute UTC due and a countdown against now. Armed jobs
  (the SEM-32 latch from `status`) append below every dated row: no due --
  the next condition edge starts them. Due-less rows (a filewatch, once a
  later unit serves one) render generically. READ-ONLY: every operator verb
  is shadowed to the bell (DL-67 discipline); `t`/`q`/escape close. The
  jobs table's flags column carries `A` for the same latch.
- JOB DETAILS POPUP (`d` / Enter on a row): the ss10 `spec` verb's
  preserve-rendered JIL block -- the post-placeholder source THIS engine
  loaded -- topped with the status facts, the `deps` verb's needs/blocks
  lines (the blast radius, DL-65), and a short local log tail. HEADER
  CLOCK is UTC, matching the engine's naive-UTC time basis (ss9): every
  timestamp on screen is one time base, deliberately not the viewer's
  local wall (DL-64).
- NAVIGATION AT ESTATE SCALE (DL-65): the jobs table renders the BOX TREE
  (indent from the status response's box_name; space folds the selected
  box, `z` folds/unfolds all; a folded box shows its hidden descendant
  count and a red problem tally -- a fold must never swallow a FAILURE
  silently). `/` opens an incremental name filter (space-separated
  substrings AND'd; Enter keeps it, Esc clears; with the log pager focused
  `/` searches the LOG instead -- find-in-what-fills-the-screen), `v`
  cycles the view
  all -> problems -> active. Filtered and non-'all' views are FLAT:
  a match inside a folded box must never be invisible. The console
  moved to `:` (vim command-line muscle memory). The status response's
  spec_drift flag renders in the subtitle: files changed on disk, the
  running catalog is the truth.
- PANE GEOMETRY, keyboard only: `m` toggles maximizing the log pane and
  hands focus to the pager (`q`/escape also restore); `]`/`[` grow/shrink
  the log against explain, `}`/`{` the jobs table against the side column.
  No mouse splitters. While anything is maximized the tree filter and the
  console never take focus -- an input the operator cannot see must never
  swallow keystrokes (DL-67).
- ONE REQUEST CONNECTION, lock-serialized (the server answers one line per
  line); `subscribe` owns its own connection until hangup (ss10). A dead
  socket flips the header subtitle to "disconnected", is reported once to
  the console, and every path retries quietly -- the TUI outlives engine
  restarts. A cancelled exchange drops the connection (a superseded worker
  must not leave its unread response to desync the next request). NO
  client-side timeouts, deliberately: a live-but-wedged engine parks the
  data plane while quit stays responsive; liveness recovery of a stuck
  engine is the operator's call, not a viewer heuristic.

The textual import is guarded (runner-design ss14): the core package keeps
its three runtime deps; this module needs `pip install 'dsl41[ui]'`.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from rich.cells import cell_len
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult, RenderResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.coordinate import Coordinate
    from textual.css.query import NoMatches
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

    # private textual module, deliberately: the Header composes its clock
    # internally with no timezone hook, and these names are stable across
    # the pinned textual>=8 family (DL-46). Revisit at any floor bump.
    from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
except ModuleNotFoundError as exc:  # pragma: no cover -- exercised via CLI guard
    raise ModuleNotFoundError(
        "the dsl41 TUI needs the optional [ui] extra: pip install 'dsl41[ui]'"
    ) from exc

from dsl41.runner_control import (
    JOB_EVENT_VERBS,
    STATUSES,
    ControlClient,
    ControlClientError,
)

_ALARM_TRANSITIONS = frozenset({"MUST_START_ALARM", "MUST_COMPLETE_ALARM"})
_STATUS_STYLE = {
    "INACTIVE": "dim",
    "STARTING": "cyan",
    "RUNNING": "bold yellow",
    "SUCCESS": "green",
    "FAILURE": "bold red",
    "TERMINATED": "magenta",
}
_TAIL_SEED_BYTES = 8192  # start a fresh tail this close to EOF
#: pager memory bound: buffer, widget, and display list all cap here in
#: lockstep. Paging serves triage; the file on disk is the forensic truth.
_PAGER_BUFFER_LINES = 10_000
_COLUMNS = ("job", "status", "at", "run", "exit", "flags", "timers", "alarms")
#: fixed render widths for the bounded columns (max of content and header
#: label: TERMINATED, HH:MM:SS, IHN, ...). Auto-width stays only where content
#: genuinely varies -- job (indent, fold markers, rollup tallies) and timers.
#: The point is to keep update_cell's update_width off these columns: textual's
#: width recompute re-measures a WHOLE column per updated-and-shrunk cell, and
#: with every cell of every row queued each refresh that is O(cells x rows) --
#: ~2M rich measures, ~25s of blocked loop, at a 519-job estate.
_COLUMN_WIDTHS = {"status": 10, "at": 8, "run": 4, "exit": 4, "flags": 5, "alarms": 6}


def _cell_sig(value: Any) -> tuple[str, str]:
    """Comparable identity of a rendered cell: (plain text, style). Text.__eq__
    ignores the style attribute, so diffing the cell objects directly would
    call a restyled-but-same-text cell unchanged."""
    if isinstance(value, Text):
        return value.plain, str(value.style)
    return str(value), ""


class _UTCHeaderClock(HeaderClock):
    """The engine's time basis is naive UTC (runner ss9); every timestamp
    this app renders -- status_at, trace, timers -- is UTC. The stock header
    clock renders LOCAL wall time, which reads as a constant skew against
    every other number on screen; this one matches the data (DL-64)."""

    def render(self) -> RenderResult:
        return Text(datetime.now(UTC).strftime("%H:%M:%S") + " UTC")


class _UTCHeader(Header):
    """Stock Header with the clock swapped for the UTC one (always shown)."""

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _UTCHeaderClock()


class SpecScreen(ModalScreen[None]):
    """Job-details popup: runtime facts + the `spec` verb's JIL block --
    the post-placeholder source the RUNNING engine loaded, not whatever
    the file on disk says now. Escape/enter/d closes."""

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("enter", "dismiss", "close", show=False),
        Binding("d", "dismiss", "close", show=False),
    ]
    CSS = """
    SpecScreen { align: center middle; }
    #specbox {
        width: 90%; max-width: 110; height: 80%;
        border: round $primary; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, title: str, body: Text) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="specbox"):
            yield Static(self._body)

    def on_mount(self) -> None:
        box = self.query_one("#specbox")
        box.border_title = self._title
        box.focus()


def format_due(due: datetime, now: datetime) -> str:
    """Absolute UTC due: HH:MM:SS today, date + time otherwise. Pure
    function (naive-UTC datetimes, the engine's time basis)."""
    if due.date() == now.date():
        return due.strftime("%H:%M:%S")
    return due.strftime("%Y-%m-%d %H:%M:%S")


def format_countdown(due: datetime, now: datetime) -> str:
    """Humanized time-to-due ("42s", "3m12s", "2h05m", "1d03h"); "-" once
    due/overdue -- a negative countdown reads as a bug, not a fact. Pure
    function so the rendering rule is testable without a terminal."""
    total = int((due - now).total_seconds())
    if total <= 0:
        return "-"
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def assemble_trigger_rows(
    timers: list[dict[str, Any]],
    jobs: dict[str, dict[str, Any]],
    now: datetime,
) -> list[tuple[str, str, str, str, str]]:
    """(due, in, job, kind, detail) rows for the triggers view: the server's
    due-ordered `timers` entries kept stable, due-less rows (filewatch shows
    "watching") below them, then one row per armed job (the SEM-32 latch --
    no due, the next condition edge starts it), name-sorted. Pure function
    so the assembly needs no live socket."""
    dated: list[tuple[str, str, str, str, str]] = []
    undated: list[tuple[str, str, str, str, str]] = []
    for entry in timers:
        job = str(entry.get("job", ""))
        kind = str(entry.get("kind", "?"))
        detail = str(entry.get("detail") or "")
        due: datetime | None = None
        if isinstance(entry.get("due"), str):
            try:
                due = datetime.fromisoformat(entry["due"])
            except ValueError:
                due = None
        if due is not None:
            dated.append((format_due(due, now), format_countdown(due, now), job, kind, detail))
        else:
            undated.append(("-", "watching" if kind == "filewatch" else "-", job, kind, detail))
    armed = [
        ("-", "-", name, "armed", "waiting on next condition edge")
        for name in sorted(jobs)
        if jobs[name].get("armed")
    ]
    return dated + undated + armed


def assemble_detail_trigger_lines(
    job: str,
    row: dict[str, Any],
    timers: list[dict[str, Any]],
) -> list[str]:
    """The details popup's trigger story (DL-68): started by, the armed
    latch, a live filewatch, the earliest dated timer for this job, then
    every pending timer. Pure so the assembly needs no live socket."""
    lines: list[str] = []
    if row.get("started_by"):
        lines.append(f"started by: {row['started_by']}")
    if row.get("armed"):
        lines.append("armed: waiting on next condition edge")
    watching = row.get("watching")
    if isinstance(watching, dict):
        line = f"watching {watching.get('file')} every {watching.get('interval')}s"
        if watching.get("min_size") is not None:
            line += f", min_size {watching['min_size']}"
        lines.append(line)
    dated = [e for e in timers if e.get("job") == job and isinstance(e.get("due"), str)]
    if dated:
        first = min(dated, key=lambda e: str(e["due"]))
        try:
            due = datetime.fromisoformat(str(first["due"]))
        except ValueError:
            due = None
        if due is not None:
            lines.append(f"next: {first.get('kind', '?')} @ {due:%Y-%m-%d %H:%M:%S}Z")
    pending = [e for e in row.get("pending_timers") or [] if isinstance(e.get("due"), str)]
    if pending:
        lines.append("pending timers:")
        for entry in pending:
            try:
                at = datetime.fromisoformat(str(entry["due"]))
            except ValueError:
                continue
            lines.append(f"  {entry.get('kind', '?')} @ {at:%H:%M:%S}")
    return lines


class TriggersScreen(ModalScreen[None]):
    """DL-68 triggers view: the `timers` verb live (2s re-query while open)
    plus the armed latches from the last status snapshot. Read-only -- every
    operator verb is shadowed to the bell (DL-67: keys must never mutate the
    estate from a view that exists to look); t/q/escape close, r re-queries."""

    _COLUMNS = ("due", "in", "job", "kind", "detail")
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close", show=False),
        Binding("t", "dismiss", "close", show=False),
        Binding("r", "refresh_now", "refresh", show=False),
        # operator verbs and pane/navigation keys: bell, exactly like the
        # log pager -- NOT check_action=False, which would let the key fall
        # through to the app binding it exists to shadow (DL-67)
        Binding("s", "app.bell", "", show=False),
        Binding("f", "app.bell", "", show=False),
        Binding("k", "app.bell", "", show=False),
        Binding("i", "app.bell", "", show=False),
        Binding("I", "app.bell", "", show=False),
        Binding("h", "app.bell", "", show=False),
        Binding("H", "app.bell", "", show=False),
        Binding("n", "app.bell", "", show=False),
        Binding("N", "app.bell", "", show=False),
        Binding("d", "app.bell", "", show=False),
        Binding("m", "app.bell", "", show=False),
        Binding("o", "app.bell", "", show=False),
        Binding("slash", "app.bell", "", show=False),
        Binding("v", "app.bell", "", show=False),
        Binding("space", "app.bell", "", show=False),
        Binding("z", "app.bell", "", show=False),
        Binding("colon", "app.bell", "", show=False),
        Binding("]", "app.bell", "", show=False),
        Binding("[", "app.bell", "", show=False),
        Binding("}", "app.bell", "", show=False),
        Binding("{", "app.bell", "", show=False),
    ]
    CSS = """
    TriggersScreen { align: center middle; }
    #trigbox {
        width: 90%; max-width: 110; height: 80%;
        border: round $primary; background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield DataTable(id="trigbox")

    def on_mount(self) -> None:
        table = self.query_one("#trigbox", DataTable)
        table.cursor_type = "row"
        for label in self._COLUMNS:
            table.add_column(label, key=label)
        table.border_title = "triggers"
        table.focus()
        self._row_ids: list[tuple[str, str, str]] = []
        self._last_rows: list[tuple[str, str, str, str, str]] = []
        self.action_refresh_now()
        self.set_interval(2.0, self.action_refresh_now)

    def action_refresh_now(self) -> None:
        # the bound METHOD, not a coroutine (the RunnerApp explain-worker
        # pattern: a superseded exclusive worker must not leave a
        # created-never-awaited coroutine)
        self.run_worker(self._refresh, group="triggers", exclusive=True)  # type: ignore[arg-type]

    async def _refresh(self) -> None:
        app = self.app
        assert isinstance(app, RunnerApp)
        try:
            response = await app._client.request({"cmd": "timers"})
        except ControlClientError as exc:
            app._set_connected(False, str(exc))
            return
        app._set_connected(True)
        timers = response.get("timers", []) if response.get("ok") else []
        rows = assemble_trigger_rows(
            timers, app._jobs_snapshot, datetime.now(UTC).replace(tzinfo=None)
        )
        try:
            table = self.query_one("#trigbox", DataTable)
        except NoMatches:
            return  # a worker resuming after the await can outlive the screen
        ids = [(job, kind, detail) for _, _, job, kind, detail in rows]
        if ids == self._row_ids:
            # steady state: countdowns tick but membership is unchanged --
            # update cells in place; clear() every 2s would reset the row
            # cursor and scroll (the jobs-table pathology, DL-67; review
            # MAJOR)
            for i, (row, old) in enumerate(zip(rows, self._last_rows)):
                if row == old:
                    continue
                for column, new, prev in zip(self._COLUMNS, row, old):
                    if new != prev:
                        table.update_cell(str(i), column, new, update_width=True)
        else:
            # membership/order changed: rebuild, then put the cursor back on
            # the same trigger if it survived
            cursor = table.cursor_row
            selected = self._row_ids[cursor] if 0 <= cursor < len(self._row_ids) else None
            table.clear()
            for i, row in enumerate(rows):
                table.add_row(*row, key=str(i))
            if selected in ids:
                table.move_cursor(row=ids.index(selected))
            self._row_ids = ids
        self._last_rows = rows
        table.border_title = f"triggers ({len(rows)})"


class _FilterInput(Input):
    """The `/` filter line. Escape clears and hides it; Enter (Input.Submitted,
    handled by the app) keeps the filter applied and returns to the table."""

    BINDINGS = [Binding("escape", "clear_filter", "clear")]

    def action_clear_filter(self) -> None:
        app = self.app
        assert isinstance(app, RunnerApp)
        app.clear_filter()


def compile_search(pattern: str) -> re.Pattern[str] | str:
    """Compile a pager search/filter pattern: regex, smartcase (insensitive
    unless the pattern itself carries an uppercase letter -- the rg/vim
    default). Returns an error string instead of raising so the prompt can
    render it; pure function so the rule is testable without a terminal."""
    flags = 0 if any(ch.isupper() for ch in pattern) else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        return f"bad pattern: {exc}"


class _LogSearchInput(Input):
    """The pager prompt line (`/`, `?`, `&`). Escape cancels the PROMPT and
    hands focus back to the log -- it must not leave the pager, which is why
    RunnerApp disables textual's escape-to-minimize special case (it swallows
    escape BEFORE any binding runs while a widget is maximized) and owns
    escape through explicit bindings instead."""

    BINDINGS = [Binding("escape", "cancel_prompt", "cancel")]

    def action_cancel_prompt(self) -> None:
        pane = self.parent
        assert isinstance(pane, _LogPane)
        pane.close_prompt()


class _LogTail(RichLog):
    """The log tail, and -- whenever it has focus -- a less-style pager.

    Focus IS the mode: textual consults the focused widget's bindings before
    the app's, so the keymap below both provides the pager verbs and shadows
    the operator verbs (`k` aimed at scroll-up must never KILLJOB, `f` aimed
    at page-forward must never FORCE_STARTJOB, `q` leaves the pager, not the
    app). Estate-mutating keys with no pager meaning ring the bell exactly
    like less does on an unknown key; `m`/`o`/`r` and the pane-resize keys
    deliberately fall through to the app (allowlist in tests). Paging must
    never mutate the estate.

    Search is regex with smartcase, all matches reverse-video, `n`/`N`
    wrap; `&` shows only matching lines (less's own filter key) as a VIEW of
    the buffer, never a loss. Follow is pinned-at-bottom: scrolling up
    pauses it ([paused] in the title), `F`/`G`/End resume. The buffer, the
    display list, and the widget all cap at _PAGER_BUFFER_LINES in lockstep.
    """

    BINDINGS = [
        # search (the pager's reason to exist)
        Binding("slash", "prompt('search')", "search"),
        Binding("question_mark", "prompt('rsearch')", "search back", show=False),
        Binding("ampersand", "prompt('filter')", "filter lines"),
        Binding("n", "match_step(1)", "next"),
        Binding("N", "match_step(-1)", "prev"),
        # motion (less/vim), shadowing k=KILLJOB / f=FORCE / d=details /
        # space=fold / z=fold-all with their pager meanings
        Binding("j", "scroll_down", "down", show=False),
        Binding("k", "scroll_up", "up", show=False),
        Binding("d", "half_page(1)", "half down", show=False),
        Binding("u", "half_page(-1)", "half up", show=False),
        Binding("f", "page_down", "page down", show=False),
        Binding("space", "page_down", "page down", show=False),
        Binding("z", "page_down", "page down", show=False),
        Binding("b", "page_up", "page up", show=False),
        Binding("g", "scroll_home", "top", show=False),
        Binding("G", "follow", "end", show=False),
        Binding("F", "follow", "follow"),
        # leaving the pager (q must not quit the app from muscle memory)
        Binding("q", "leave", "close"),
        Binding("escape", "leave", "close", show=False),
        # estate-mutating operator verbs with no pager meaning: bell, like
        # less on an unknown key -- NOT check_action=False, which would let
        # the key fall through to the app binding it exists to shadow
        Binding("s", "app.bell", "", show=False),
        Binding("i", "app.bell", "", show=False),
        Binding("I", "app.bell", "", show=False),
        Binding("h", "app.bell", "", show=False),
        Binding("H", "app.bell", "", show=False),
        Binding("v", "app.bell", "", show=False),
        Binding("colon", "app.bell", "", show=False),
    ]

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- textual's own name
        super().__init__(
            id=id,
            highlight=False,
            markup=False,
            wrap=False,
            auto_scroll=False,
            max_lines=_PAGER_BUFFER_LINES,
        )
        self._stream = "out"
        self._path: str | None = None
        self._buffer: deque[str] = deque(maxlen=_PAGER_BUFFER_LINES)
        self._display: list[str] = []  # lines currently in the widget (filter view)
        self._needle: re.Pattern[str] | None = None
        self._needle_text = ""
        self._search_backward = False
        self._line_filter: re.Pattern[str] | None = None
        self._line_filter_text = ""
        self._matches: list[int] = []  # widget rows matching the needle
        self._match_pos: int | None = None  # index into _matches
        self._title_cache = ""

    # ------------------------------------------------------------- data plane

    def set_source(self, stream: str, path: str | None) -> None:
        """New tail target (job switch, o toggle, truncation): drop the
        buffer, keep the search/filter patterns -- less keeps the pattern
        across files, and `n` after a job switch is a feature."""
        self._stream, self._path = stream, path
        self._buffer.clear()
        self._display = []
        self._matches = []
        self._match_pos = None
        self.clear()
        self._refresh_title()

    def feed(self, lines: list[str]) -> None:
        was_at_end = self.is_vertical_scroll_end
        for line in lines:
            self._buffer.append(line)
            if self._line_filter is None or self._line_filter.search(line):
                self._display.append(line)
                self._write_row(line, len(self._display) - 1)
        overflow = len(self._display) - _PAGER_BUFFER_LINES
        if overflow > 0:
            # the widget already trimmed itself via max_lines; mirror it
            del self._display[:overflow]
            dropped = 0
            shifted = []
            for row in self._matches:
                if row < overflow:
                    dropped += 1
                else:
                    shifted.append(row - overflow)
            self._matches = shifted
            if self._match_pos is not None:
                self._match_pos = max(0, self._match_pos - dropped) if self._matches else None
        if was_at_end:
            self.scroll_end(animate=False)
        elif overflow > 0:
            # keep the operator's place: content slid up under a paused view
            self.scroll_to(y=max(0, self.scroll_y - overflow), animate=False)
        self._refresh_title()

    def _write_row(self, line: str, row: int) -> None:
        text = Text(line)
        if self._needle is not None:
            spans = list(self._needle.finditer(line))
            if spans:
                for span in spans:
                    text.stylize("reverse", span.start(), span.end())
                self._matches.append(row)
        self.write(text, scroll_end=False)

    def _rebuild(self) -> None:
        """Re-render the whole view from the buffer (search or filter
        changed): restyles every line and recomputes the match rows."""
        self.clear()
        self._display = []
        self._matches = []
        self._match_pos = None
        for line in self._buffer:
            if self._line_filter is None or self._line_filter.search(line):
                self._display.append(line)
                self._write_row(line, len(self._display) - 1)

    # ------------------------------------------------------------ search model

    def apply_search(self, pattern: str, backward: bool) -> str | None:
        """Set (or with '' clear) the needle; returns an error string for the
        prompt to render, None on success. On success jumps less-style: the
        first match after (before, for `?`) the current top row, wrapping."""
        top = int(self.scroll_y)  # clear() inside _rebuild resets the scroll
        if not pattern:
            self._needle = None
            self._needle_text = ""
            self._rebuild()
            # de-highlight in place: clearing a search must not move the view
            self.scroll_to(y=min(top, self.max_scroll_y), animate=False)
            self._refresh_title()
            return None
        compiled = compile_search(pattern)
        if isinstance(compiled, str):
            return compiled
        self._needle, self._needle_text, self._search_backward = compiled, pattern, backward
        self._rebuild()
        if self._matches:
            if backward:
                below = [i for i, row in enumerate(self._matches) if row < top]
                self._match_pos = below[-1] if below else len(self._matches) - 1
            else:
                above = [i for i, row in enumerate(self._matches) if row > top]
                self._match_pos = above[0] if above else 0
            self._jump()
        else:
            self.scroll_to(y=min(top, self.max_scroll_y), animate=False)
        self._refresh_title()
        return None

    def apply_line_filter(self, pattern: str) -> str | None:
        if pattern:
            compiled = compile_search(pattern)
            if isinstance(compiled, str):
                return compiled
            self._line_filter, self._line_filter_text = compiled, pattern
        else:
            self._line_filter, self._line_filter_text = None, ""
        self._rebuild()
        self.scroll_end(animate=False)  # a new view of a log starts at its tail
        self._refresh_title()
        return None

    def action_match_step(self, step: int) -> None:
        """`n` repeats in the search direction, `N` opposes it (less)."""
        if not self._matches:
            self.app.bell()  # no search, or the pattern matches nothing
            return
        direction = -step if self._search_backward else step
        if self._match_pos is None:
            self._match_pos = 0 if direction > 0 else len(self._matches) - 1
        else:
            self._match_pos = (self._match_pos + direction) % len(self._matches)
        self._jump()
        self._refresh_title()

    def _jump(self) -> None:
        assert self._match_pos is not None
        self.scroll_to(y=self._matches[self._match_pos], animate=False)  # match at top (less)

    # ------------------------------------------------------------ pager verbs

    def action_prompt(self, kind: str) -> None:
        pane = self.parent
        assert isinstance(pane, _LogPane)
        pane.open_prompt(kind)

    # the widget-inherited motion actions animate by default and move
    # RELATIVE TO scroll_target_y, which goes stale when the viewport
    # changes size (the small pane's scroll_end leaves a target beyond the
    # maximized pane's max, and every scroll_up after that only shaves the
    # phantom overshoot). A pager SNAPS (less has no smooth scrolling), and
    # snapping from the real offset sidesteps the stale target entirely.

    def _snap(self, y: float) -> None:
        self.scroll_to(y=y, animate=False)
        self.call_after_refresh(self._refresh_title)

    def action_half_page(self, sign: int) -> None:
        half = max(1, self.scrollable_content_region.height // 2)
        self._snap(self.scroll_y + sign * half)

    def action_follow(self) -> None:
        self._snap(self.max_scroll_y)

    def action_scroll_up(self) -> None:
        self._snap(self.scroll_y - 1)

    def action_scroll_down(self) -> None:
        self._snap(self.scroll_y + 1)

    def action_page_up(self) -> None:
        self._snap(self.scroll_y - self.scrollable_content_region.height)

    def action_page_down(self) -> None:
        self._snap(self.scroll_y + self.scrollable_content_region.height)

    def action_scroll_home(self) -> None:
        self._snap(0)

    def action_scroll_end(self) -> None:
        self._snap(self.max_scroll_y)

    def action_leave(self) -> None:
        self.screen.minimize()
        self.app.query_one("#jobs", DataTable).focus()

    # ------------------------------------------------------------------ title

    def on_mount(self) -> None:
        self._refresh_title()
        # scroll state feeds the title ([paused]); arrows/wheel bypass our
        # actions, so a light tick keeps it honest between feeds
        self.set_interval(0.5, self._refresh_title)

    def _refresh_title(self) -> None:
        title = Text(f"log ({self._stream}): {self._path or 'none yet'}")
        if self._line_filter_text:
            title.append(f"  &{self._line_filter_text}", style="italic")
        if self._needle_text:
            at = "-" if self._match_pos is None else str(self._match_pos + 1)
            title.append(f"  /{self._needle_text}/ {at}/{len(self._matches)}", style="italic")
        if not self.is_vertical_scroll_end:
            title.append("  [paused]", style="bold yellow")
        if title.plain != self._title_cache:
            self._title_cache = title.plain
            self.border_title = title


class _LogPane(Vertical):
    """The log column cell: the pager plus its prompt line. Maximizing THIS
    (not the bare RichLog) is what keeps the prompt visible while zoomed --
    maximize hides every widget outside the maximized subtree, which is
    exactly how the old tree-filter-under-zoom bug happened."""

    ALLOW_MAXIMIZE = True  # containers default to False

    _prompt_kind = "search"  # which verb the open prompt serves
    _PROMPTS = {
        "search": ("/", "search (regex, smartcase) -- Enter finds, empty clears, Esc cancels"),
        "rsearch": ("?", "search backward (regex, smartcase) -- empty clears, Esc cancels"),
        "filter": ("&", "show only matching lines (regex, smartcase) -- empty clears"),
    }

    def open_prompt(self, kind: str) -> None:
        marker, placeholder = self._PROMPTS[kind]
        prompt = self.query_one("#logsearch", _LogSearchInput)
        self._prompt_kind = kind
        prompt.placeholder = placeholder
        prompt.border_title = marker
        prompt.styles.display = "block"
        prompt.focus()

    def close_prompt(self) -> None:
        prompt = self.query_one("#logsearch", _LogSearchInput)
        prompt.value = ""
        prompt.border_title = ""
        prompt.styles.display = "none"
        self.query_one("#logtail", _LogTail).focus()

    @on(Input.Submitted, "#logsearch")
    def _on_prompt_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # the app handles #filterline submits; this one is ours
        pager = self.query_one("#logtail", _LogTail)
        kind = self._prompt_kind
        if kind == "filter":
            error = pager.apply_line_filter(event.value.strip())
        else:
            error = pager.apply_search(event.value.strip(), backward=(kind == "rsearch"))
        if error is not None:
            # stay open with the text intact; the border carries the refusal
            event.input.border_title = Text(error, style="bold red")
            return
        self.close_prompt()


def parse_console_command(text: str, selected: str | None) -> dict[str, Any] | str:
    """Parse an event-console line into a sendevent request, or return an
    error string. Grammar (ss11): `<JOB_VERB> [job]`, `SET_GLOBAL NAME=value`,
    `CHANGE_STATUS [job] STATUS [exit_code]`; an omitted job targets the
    selected row. Pure function so the parser is testable without a
    terminal."""
    tokens = text.split()
    if not tokens:
        return "empty command"
    verb = tokens[0].upper()
    args = tokens[1:]

    def _job(explicit: str | None) -> dict[str, Any] | str:
        job = explicit if explicit is not None else selected
        if job is None:
            return f"{verb} needs a job (none selected)"
        return {"cmd": "sendevent", "event": verb, "job": job}

    if verb in JOB_EVENT_VERBS:
        if len(args) > 1:
            return f"{verb} takes at most one job"
        return _job(args[0] if args else None)
    if verb == "SET_GLOBAL":
        if len(args) != 1 or "=" not in args[0]:
            return 'SET_GLOBAL expects "NAME=value"'
        name, _, value = args[0].partition("=")
        if not name:
            return 'SET_GLOBAL expects "NAME=value"'
        return {"cmd": "sendevent", "event": verb, "name": name, "value": value}
    if verb == "CHANGE_STATUS":
        if args and args[0].upper() in STATUSES:
            job, status, rest = selected, args[0].upper(), args[1:]
            if job is None:
                return "CHANGE_STATUS needs a job (none selected)"
        elif len(args) >= 2:
            job, status, rest = args[0], args[1].upper(), args[2:]
        else:
            return "CHANGE_STATUS expects [job] STATUS [exit_code]"
        request: dict[str, Any] = {
            "cmd": "sendevent",
            "event": verb,
            "job": job,
            "status": status,
        }
        if rest:
            if len(rest) > 1:
                return "CHANGE_STATUS expects at most one exit_code"
            try:
                request["exit_code"] = int(rest[0])
            except ValueError:
                return f"exit_code must be an integer, got {rest[0]!r}"
        return request
    return f"unknown verb {verb!r} (sendevent verbs only)"


class RunnerApp(App[None]):
    """The ss11 app: jobs table, explain pane, log tail, event console."""

    TITLE = "dsl41 runner"
    #: textual's default swallows escape BEFORE any binding runs while a
    #: widget is maximized -- escape in the pager's search prompt would exit
    #: the pager instead of cancelling the prompt. Escape is owned by
    #: explicit bindings instead (_LogTail leave, _LogSearchInput cancel).
    ESCAPE_TO_MINIMIZE = False
    CSS = """
    #main { height: 1fr; }
    #tablecol { width: 3fr; }
    #filterline { display: none; }
    #jobs { height: 1fr; border: round $primary; }
    #side { width: 2fr; }
    #explain-box { height: 2fr; border: round $primary; }
    #logbox { height: 3fr; }
    #logtail { height: 1fr; border: round $primary; }
    #logsearch { display: none; }
    #consolebox { height: 11; }
    #console { height: 1fr; border: round $secondary; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("s", "send('STARTJOB')", "start"),
        # visible: FORCE is the rerun verb -- plain STARTJOB is SEM-10-gated
        # on box members and its refusal used to be silent (DL-64)
        Binding("f", "send('FORCE_STARTJOB')", "force"),
        Binding("k", "send('KILLJOB')", "kill"),
        Binding("i", "send('ON_ICE')", "ice", show=False),
        Binding("I", "send('OFF_ICE')", "off-ice", show=False),
        Binding("h", "send('ON_HOLD')", "hold", show=False),
        Binding("H", "send('OFF_HOLD')", "off-hold", show=False),
        Binding("n", "send('ON_NOEXEC')", "noexec", show=False),
        Binding("N", "send('OFF_NOEXEC')", "off-noexec", show=False),
        Binding("d", "details", "details"),
        Binding("t", "triggers", "triggers"),
        Binding("m", "maximize_log", "zoom log"),
        Binding("o", "toggle_stream", "out/err"),
        Binding("r", "refresh", "refresh"),
        # navigation at estate scale (DL-65): / filters by name, v cycles
        # all -> problems -> active, space folds the selected box, z all
        Binding("slash", "focus_filter", "filter"),
        Binding("v", "cycle_view", "view"),
        Binding("space", "toggle_box", "fold", show=False),
        Binding("z", "toggle_all", "fold-all", show=False),
        Binding("colon", "focus_console", "console"),
        # pane sizing (no mouse splitters in a keyboard-first TUI):
        # ]/[ grow/shrink the log tail against explain; }/{ the jobs table
        # against the whole side column
        Binding("]", "resize('log', 1)", "log+", show=False),
        Binding("[", "resize('log', -1)", "log-", show=False),
        Binding("}", "resize('table', 1)", "table+", show=False),
        Binding("{", "resize('table', -1)", "table-", show=False),
    ]

    def __init__(self, socket_path: Path) -> None:
        super().__init__()
        self.socket_path = Path(socket_path)
        self.sub_title = str(self.socket_path)
        self._client = ControlClient(self.socket_path)
        self._selected: str | None = None
        self._rows: set[str] = set()  # DataTable row keys we created
        self._trace_seq = 0
        self._alarms: dict[str, int] = {}
        self._log_paths: dict[str, tuple[str | None, str | None]] = {}
        self._tail_stream: int = 0  # 0 = out, 1 = err
        # (stream, path) rather than the bare path: out and err may legally
        # resolve to the SAME file, and an `o` toggle must still retitle
        self._tail_key: tuple[str, str | None] | None = None
        self._tail_pos: int | None = None
        # pane shares, resized by ]/[ and }/{ -- fr weights out of _SHARE_TOTAL
        self._log_share = 3  # log vs explain (CSS default 3fr : 2fr)
        self._table_share = 3  # jobs table vs side column (CSS default 3fr : 2fr)
        # DL-65 navigation state
        self._jobs_snapshot: dict[str, dict[str, Any]] = {}  # last status response
        self._filter = ""  # space-separated substrings, AND'd, case-insensitive
        self._view_mode = 0  # index into _VIEW_MODES
        self._collapsed: set[str] = set()  # box rows folded shut
        self._row_order: list[str] = []  # current table order; differs -> rebuild
        #: row key -> cell signatures as last rendered; the steady-state diff
        #: (unchanged rows cost nothing, changed rows update only their
        #: changed cells)
        self._cell_sigs: dict[str, list[tuple[str, str]]] = {}
        #: set across a table rebuild that restores the selection: the first
        #: add_row fires a spurious row-0 highlight BEFORE move_cursor lands,
        #: and each such bounce would wipe the log tail (review MAJOR). The
        #: highlight handler ignores events until the restore target arrives.
        self._restore_selected: str | None = None
        self._spec_drift = False
        self._connected: bool | None = None  # None = never yet reported
        self._refreshing = False
        self._dirty = False

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield _UTCHeader()
        with Horizontal(id="main"):
            with Vertical(id="tablecol"):
                yield _FilterInput(
                    placeholder="filter: substring(s) -- Enter keeps, Esc clears", id="filterline"
                )
                yield DataTable(id="jobs")
            with Vertical(id="side"):
                with VerticalScroll(id="explain-box"):
                    yield Static(id="explain")
                with _LogPane(id="logbox"):
                    yield _LogTail(id="logtail")
                    yield _LogSearchInput(id="logsearch")
        with Vertical(id="consolebox"):
            yield RichLog(id="console", markup=False, wrap=True)
            yield Input(
                placeholder="STARTJOB [job] | KILLJOB [job] | SET_GLOBAL N=v"
                " | CHANGE_STATUS [job] STATUS [exit] -- empty job = selected row",
                id="cmdline",
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs", DataTable)
        table.cursor_type = "row"
        for label in _COLUMNS:  # singular add_column: key= predates the 6.2 tuple form
            table.add_column(label, key=label, width=_COLUMN_WIDTHS.get(label))
        self.query_one("#explain-box").border_title = "explain"
        self.query_one("#console", RichLog).border_title = "events"
        table.focus()
        self.run_worker(self._refresh(), group="refresh", exclusive=False)
        self.run_worker(self._follow_journal(), group="journal", exclusive=True)
        self.set_interval(2.0, self._poll)
        self.set_interval(0.5, self._tail_step)

    # ------------------------------------------------------- state refresh

    def _poll(self) -> None:
        self.run_worker(self._refresh(), group="refresh", exclusive=False)

    def action_refresh(self) -> None:
        self._poll()

    async def _refresh(self) -> None:
        """Re-query status/trace/explain. Coalescing guard: refreshes
        triggered while one is in flight fold into a single trailing pass."""
        if self._refreshing:
            self._dirty = True
            return
        self._refreshing = True
        try:
            while True:
                self._dirty = False
                try:
                    status = await self._client.request({"cmd": "status"})
                    trace = await self._client.request({"cmd": "trace", "since": self._trace_seq})
                except ControlClientError as exc:
                    self._set_connected(False, str(exc))
                    return
                self._set_connected(True)
                if trace.get("ok"):
                    last_seq = trace.get("last_seq")
                    if isinstance(last_seq, int) and last_seq < self._trace_seq:
                        # the engine serving this socket has a SHORTER trace
                        # than our cut: the run root was re-baselined under a
                        # reattaching viewer -- restart the commentary and the
                        # alarm tally from the fresh oracle's top
                        self._trace_seq = 0
                        self._alarms.clear()
                        self._dirty = True
                    self._consume_trace(trace.get("entries", []))
                if status.get("ok"):
                    self._set_drift(bool(status.get("spec_drift")))
                    self._update_table(status.get("jobs", {}))
                await self._update_explain()
                if not self._dirty:
                    return
        except NoMatches:
            return  # app teardown unmounted the widgets mid-refresh
        finally:
            self._refreshing = False

    def _refresh_subtitle(self) -> None:
        base = str(self.socket_path)
        if self._connected is False:
            base += " (disconnected)"
        if self._spec_drift:
            base += "  [SPEC DRIFT: estate files changed on disk]"
        self.sub_title = base

    def _set_connected(self, up: bool, detail: str = "") -> None:
        if up == self._connected:
            return
        self._connected = up
        self._refresh_subtitle()
        console = self.query_one("#console", RichLog)
        if up:
            console.write(Text("connected", style="green"))
        else:
            console.write(Text(f"control socket unreachable: {detail}", style="red"))

    def _set_drift(self, drift: bool) -> None:
        """DL-65 daemon-reload-hint analog, inverted: there is no reload --
        the subtitle says the running catalog no longer matches the files."""
        if drift == self._spec_drift:
            return
        self._spec_drift = drift
        self._refresh_subtitle()
        if drift:
            self._console_write(
                Text(
                    "spec drift: estate files changed on disk; the running catalog is"
                    " the truth (cold restart into a fresh run root to adopt)",
                    style="yellow",
                )
            )

    def _consume_trace(self, entries: list[dict[str, Any]]) -> None:
        console = self.query_one("#console", RichLog)
        for entry in entries:
            seq = entry.get("seq")
            if isinstance(seq, int):
                if seq <= self._trace_seq:
                    continue  # idempotent consumption: never render twice
                self._trace_seq = seq
            transition = str(entry.get("transition", ""))
            job = str(entry.get("job", ""))
            if transition in _ALARM_TRANSITIONS:
                self._alarms[job] = self._alarms.get(job, 0) + 1
            at = str(entry.get("at", ""))
            clock = at[11:19] if len(at) >= 19 else at
            if transition in _ALARM_TRANSITIONS:
                style = "bold red"
            elif transition == "START_REFUSED":
                # DL-64: a refused operator start must not blend into the
                # dim commentary -- it IS the answer to "why did nothing happen"
                style = "yellow"
            else:
                style = "dim"
            console.write(
                Text(f"{clock} {job} {transition} [{entry.get('cause', '')}]", style=style)
            )

    _VIEW_MODES = ("all", "problems", "active")

    def _update_table(self, jobs: dict[str, dict[str, Any]]) -> None:
        self._jobs_snapshot = jobs
        for name, row in jobs.items():
            self._log_paths[name] = (row.get("log_out"), row.get("log_err"))
        self._render_table()

    def _is_problem(self, name: str, row: dict[str, Any]) -> bool:
        """ONE definition of 'problem', shared by the `v` problems view and
        the folded-box rollup (review MAJOR: two definitions one keystroke
        apart hid a QUE_WAIT under a quiet fold). QUE_WAIT counts: on a
        contended estate it can park a region indefinitely."""
        status = str(row.get("status", ""))
        return status in ("FAILURE", "TERMINATED", "QUE_WAIT") or bool(self._alarms.get(name))

    def _match(self, name: str, row: dict[str, Any]) -> bool:
        mode = self._VIEW_MODES[self._view_mode]
        if mode == "problems" and not self._is_problem(name, row):
            return False
        if mode == "active" and str(row.get("status", "")) not in (
            "STARTING",
            "RUNNING",
            "QUE_WAIT",
        ):
            return False
        hay = name.lower()
        return all(term in hay for term in self._filter.lower().split())

    def _children(self) -> dict[str | None, list[str]]:
        """box name -> member names (None -> top-level), from the status
        snapshot's box_name field (DL-65) -- alpha within each level."""
        kids: dict[str | None, list[str]] = {}
        for name in sorted(self._jobs_snapshot):
            box = self._jobs_snapshot[name].get("box_name")
            kids.setdefault(box if isinstance(box, str) else None, []).append(name)
        return kids

    def _visible(self) -> list[tuple[str, int]]:
        """(job, depth) rows in display order: the box tree with collapsed
        subtrees omitted -- or a FLAT filtered list while a name filter or a
        non-'all' view mode is active (predictability over cleverness: a
        filter match inside a collapsed box must never be invisible)."""
        jobs = self._jobs_snapshot
        if self._filter or self._view_mode:
            return [(n, 0) for n in sorted(jobs) if self._match(n, jobs[n])]
        kids = self._children()
        out: list[tuple[str, int]] = []
        buried: set[str] = set()

        def bury(name: str) -> None:
            buried.add(name)
            for child in kids.get(name, []):
                bury(child)

        def walk(name: str, depth: int) -> None:
            out.append((name, depth))
            for child in kids.get(name, []):
                bury(child) if name in self._collapsed else walk(child, depth + 1)

        for top in kids.get(None, []):
            walk(top, 0)
        placed = {n for n, _ in out} | buried
        # ghosts whose box_name names a job the store never saw: flat, never hidden
        out.extend((n, 0) for n in sorted(jobs) if n not in placed)
        return out

    def _rollup(self, name: str, kids: dict[str | None, list[str]]) -> tuple[int, int]:
        """(descendants, problems) hidden beneath a collapsed box: a folded
        subtree must never hide a problem silently -- and 'problem' is
        _is_problem, the same set the `v` problems view shows."""
        total = problems = 0
        stack = list(kids.get(name, []))
        while stack:
            n = stack.pop()
            total += 1
            if self._is_problem(n, self._jobs_snapshot.get(n, {})):
                problems += 1
            stack.extend(kids.get(n, []))
        return total, problems

    def _decorated_cells(
        self, name: str, depth: int, kids: dict[str | None, list[str]], *, flat: bool = False
    ) -> list[Any]:
        cells = self._row_cells(name, self._jobs_snapshot.get(name, {}))
        if flat:
            # filtered / non-'all' views: no fold markers, no rollup -- the
            # "hidden beneath" claim would be false with the members listed
            # right below (review MINOR)
            return cells
        marker = ""
        if kids.get(name):
            marker = "▸ " if name in self._collapsed else "▾ "
        label = "  " * depth + marker + name
        style = ""
        if name in self._collapsed and kids.get(name):
            total, problems = self._rollup(name, kids)
            label += f" ({total}{f', {problems}!' if problems else ''})"
            if problems:
                style = "bold red"
        cells[0] = Text(label, style=style)
        return cells

    def _render_table(self) -> None:
        try:
            table = self.query_one("#jobs", DataTable)
        except NoMatches:
            return  # teardown
        visible = self._visible()
        kids = self._children()
        flat = bool(self._filter or self._view_mode)
        order = [name for name, _ in visible]
        if order != self._row_order:
            # membership/order changed (fold, filter, mode, new jobs):
            # rebuild wholesale and put the cursor back on the selected key
            table.clear()
            self._row_order = order
            self._rows = set(order)
            # arm the bounce guard BEFORE add_row: the first row fires a
            # spurious row-0 highlight ahead of the cursor restore below
            self._restore_selected = self._selected if self._selected in self._rows else None
            if not order:
                # nothing visible: keyed verbs must not fire at a job the
                # operator cannot see (review MAJOR)
                self._selected = None
            self._cell_sigs = {}
            for name, depth in visible:
                cells = self._decorated_cells(name, depth, kids, flat=flat)
                self._cell_sigs[name] = [_cell_sig(cell) for cell in cells]
                table.add_row(*cells, key=name)
            if self._restore_selected is not None:
                table.move_cursor(row=order.index(self._restore_selected))
            elif order:
                table.move_cursor(row=0)  # highlight event resyncs _selected
        else:
            # steady state: update only cells whose (text, style) actually
            # changed -- pushing every cell each refresh queues them all for
            # textual's width recompute, which re-measures a whole column per
            # shrunk cell (the O(cells x rows) freeze at estate scale)
            for name, depth in visible:
                cells = self._decorated_cells(name, depth, kids, flat=flat)
                sigs = [_cell_sig(cell) for cell in cells]
                old = self._cell_sigs.get(name)
                if sigs == old:
                    continue
                for i, (column, cell, sig) in enumerate(zip(_COLUMNS, cells, sigs)):
                    if old is not None and old[i] == sig:
                        continue
                    # update_width only where it can matter: an auto-width
                    # column whose rendered width changed with the text
                    widen = column not in _COLUMN_WIDTHS and (
                        old is None or cell_len(old[i][0]) != cell_len(sig[0])
                    )
                    table.update_cell(name, column, cell, update_width=widen)
                self._cell_sigs[name] = sigs
        # a Text title: border_title strings parse as rich markup, so a
        # user-typed filter (or a bracketed mode tag) would vanish into it
        title = Text(f"jobs {len(order)}/{len(self._jobs_snapshot)}")
        if self._filter:
            title.append(f" /{self._filter}/", style="italic")
        mode = self._VIEW_MODES[self._view_mode]
        if mode != "all":
            title.append(f" · {mode}", style="bold")
        table.border_title = title
        if self._selected is None and table.row_count:
            self._selected = str(table.coordinate_to_cell_key(Coordinate(0, 0)).row_key.value)

    def _row_cells(self, name: str, row: dict[str, Any]) -> list[Any]:
        status = str(row.get("status", ""))
        at = str(row.get("status_at") or "")
        # A last: the operator flags are set states, the armed latch is a
        # promise -- IHNA order keeps the cell diffable by eye
        flags = "".join(
            mark
            for mark, flag in (
                ("I", "on_ice"),
                ("H", "on_hold"),
                ("N", "on_noexec"),
                ("A", "armed"),
            )
            if row.get(flag)
        )
        timers = row.get("pending_timers") or []
        timer_text = ""
        if timers:
            first = timers[0]
            due = str(first.get("due", ""))
            clock = due[11:19] if len(due) >= 19 else due
            timer_text = f"{first.get('kind', '?')}@{clock}"
            if len(timers) > 1:
                timer_text += f" +{len(timers) - 1}"
        alarms = self._alarms.get(name, 0)
        exit_code = row.get("exit_code")
        return [
            name,
            Text(status, style=_STATUS_STYLE.get(status, "")),
            at[11:19] if len(at) >= 19 else at,
            str(row.get("run_number", "")),
            "" if exit_code is None else str(exit_code),
            flags,
            timer_text,
            Text(str(alarms), style="bold red") if alarms else "",
        ]

    async def _update_explain(self) -> None:
        pane = self.query_one("#explain", Static)
        box = self.query_one("#explain-box")
        job = self._selected
        if job is None:
            box.border_title = "explain"
            pane.update("")
            return
        box.border_title = f"explain: {job}"
        try:
            response = await self._client.request({"cmd": "explain", "job": job})
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            return
        if not response.get("ok"):
            pane.update(Text(str(response.get("error", "explain failed")), style="red"))
            return
        text = Text()
        condition = response.get("condition")
        if condition is None:
            text.append("no condition -- starts on demand/schedule", style="dim")
            pane.update(text)
            return
        satisfied = bool(response.get("satisfied"))
        text.append("waiting on:\n" if not satisfied else "satisfied:\n", style="bold")
        text.append(f"  {condition}\n\n")
        for atom in response.get("atoms", []):
            true = bool(atom.get("true"))
            text.append("  ✔ " if true else "  ✘ ", style="green" if true else "red")
            text.append(f"{atom.get('atom', '')}", style="" if true else "bold")
            if "actual" in atom:  # global atoms carry the effective value (DL-66)
                actual = atom["actual"]
                text.append(f"   = {actual!r}" if actual is not None else "   (unset)", style="dim")
            text.append("\n")
        pane.update(text)

    # -------------------------------------------------------- change feed

    async def _follow_journal(self) -> None:
        """Wake-up signal only: any record means the estate may have moved.
        Rendering always comes from the idempotent queries (module
        docstring), so at-least-once delivery is harmless here."""
        while True:
            try:
                async for _record in self._client.subscribe():
                    self._poll()
            except ControlClientError as exc:
                if "no journal" in str(exc):
                    return  # journal-less run: polling alone carries the UI
            await asyncio.sleep(1.0)

    # ----------------------------------------------------------- log tail

    def _tail_step(self) -> None:
        try:
            pager = self.query_one("#logtail", _LogTail)
        except NoMatches:
            return  # a set_interval tick can outlive the unmounting screen
        paths = self._log_paths.get(self._selected or "", (None, None))
        path = paths[self._tail_stream]
        stream = ("out", "err")[self._tail_stream]
        if (stream, path) != self._tail_key:
            self._tail_key = (stream, path)
            self._tail_pos = None
            pager.set_source(stream, path)
        if path is None:
            return
        try:
            size = os.stat(path).st_size
        except OSError:
            return  # not created yet; keep watching
        if self._tail_pos is None:
            self._tail_pos = max(0, size - _TAIL_SEED_BYTES)
        elif size < self._tail_pos:  # truncated underneath us: start over
            self._tail_pos = 0
            pager.set_source(stream, path)
        if size == self._tail_pos:
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(self._tail_pos)
                data = handle.read(size - self._tail_pos)
        except OSError:
            return
        self._tail_pos = size
        pager.feed(data.decode("utf-8", errors="replace").splitlines())

    def action_toggle_stream(self) -> None:
        self._tail_stream = 1 - self._tail_stream
        self._tail_step()

    # ------------------------------------------------------ event console

    @on(DataTable.RowHighlighted, "#jobs")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        selected = str(event.row_key.value)
        if self._restore_selected is not None:
            if selected != self._restore_selected:
                return  # the rebuild's spurious row-0 bounce, not the operator
            # the restore landed; it equals _selected, so the guard below
            # keeps the log tail and explain pane untouched
            self._restore_selected = None
        if selected == self._selected:
            return
        self._selected = selected
        # the bound METHOD, not a coroutine: an exclusive worker superseded
        # before it starts would leave a created-never-awaited coroutine
        # (rebuild-driven highlight bursts made that warning real)
        # (arg-type: run_worker's ResultType inference chokes on the bound
        # async method; textual accepts it -- iscoroutinefunction dispatch)
        self.run_worker(self._update_explain, group="explain", exclusive=True)  # type: ignore[arg-type]
        self._tail_step()

    @on(DataTable.RowSelected, "#jobs")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected = str(event.row_key.value)
        self.action_details()

    def action_details(self) -> None:
        if self._selected is not None:
            # partial, not a bare coroutine: an exclusive worker superseded
            # before starting must not leave a created-never-awaited coroutine
            work = functools.partial(self._show_details, self._selected)
            self.run_worker(work, group="details", exclusive=True)  # type: ignore[arg-type]

    def action_triggers(self) -> None:
        self.push_screen(TriggersScreen())

    async def _show_details(self, job: str) -> None:
        """The spec popup: runtime facts from `status`, dependency lines from
        `deps` (needs/blocks -- the blast radius, DL-65), source from `spec`,
        and a short local log tail (the `systemctl status` composite)."""
        try:
            spec = await self._client.request({"cmd": "spec", "job": job})
            status = await self._client.request({"cmd": "status", "job": job})
            deps = await self._client.request({"cmd": "deps", "job": job})
            timers = await self._client.request({"cmd": "timers"})
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            return
        self._set_connected(True)
        body = Text()
        row = status.get("jobs", {}).get(job, {}) if status.get("ok") else {}
        if row:
            state = str(row.get("status", ""))
            body.append(state, style=_STATUS_STYLE.get(state, "bold"))
            exit_code = row.get("exit_code")
            body.append(
                f"  run {row.get('run_number', 0)}"
                f"  exit {'-' if exit_code is None else exit_code}"
                f"  at {row.get('status_at') or '-'} UTC\n"
            )
            # DL-68 trigger story: started by, armed latch, live filewatch,
            # next tick from the timers verb, pending timers
            timer_entries = timers.get("timers", []) if timers.get("ok") else []
            for line in assemble_detail_trigger_lines(job, row, timer_entries):
                label, sep, rest = line.partition(": ")
                if sep and not line.startswith(" "):
                    body.append(label + sep, style="bold")
                    body.append(rest + "\n")
                elif line.endswith(":"):
                    body.append(line + "\n", style="bold")
                else:
                    body.append(line + "\n")
            for label, key in (("log out", "log_out"), ("log err", "log_err")):
                if row.get(key):
                    body.append(f"{label}: {row[key]}\n", style="dim")
        if deps.get("ok"):
            needs = [*deps.get("upstream", []), *(f"v({g})" for g in deps.get("globals", []))]
            if needs:
                body.append("needs:   ", style="bold")
                body.append(", ".join(needs) + "\n")
            if deps.get("box_name"):
                body.append("box:     ", style="bold")
                body.append(f"{deps['box_name']}\n")
            downstream = deps.get("downstream", [])
            if downstream:
                body.append("blocks:  ", style="bold")
                body.append(", ".join(downstream) + "\n")
            members = deps.get("members", [])
            if members:
                # containment IS blast radius: a KILLJOB here reaches these
                body.append("members: ", style="bold")
                body.append(", ".join(members) + "\n")
        body.append("\n")
        if not spec.get("ok"):
            body.append(str(spec.get("error", "spec failed")), style="red")
        elif spec.get("jil"):
            body.append(str(spec["jil"]))
        else:
            body.append("no source text served by this engine", style="dim")
        out_path = row.get("log_out") if row else None
        if out_path:
            try:
                with open(out_path, "rb") as handle:
                    handle.seek(max(0, os.stat(out_path).st_size - 4096))
                    lines = handle.read().decode("utf-8", errors="replace").splitlines()[-10:]
            except OSError:
                lines = []
            if lines:
                body.append("\nlog tail:\n", style="bold")
                for line in lines:
                    body.append(f"  {line}\n", style="dim")
        self.push_screen(SpecScreen(job, body))

    # ------------------------------------------------------- pane geometry

    _SHARE_TOTAL = 5  # both splits are X fr : (5 - X) fr

    def action_maximize_log(self) -> None:
        pane = self.query_one("#logbox", _LogPane)
        if self.screen.maximized is pane:
            self.screen.minimize()
            self.query_one("#jobs", DataTable).focus()
        else:
            # the PANE, not the bare log: maximize hides everything outside
            # the maximized subtree, and the search prompt must stay visible
            self.screen.maximize(pane)
            # focus the log EXPLICITLY: focus is the pager mode switch, and
            # textual otherwise lands focus on the console Input, which
            # consumes every letter key as text -- the restore keystroke
            # included
            self.query_one("#logtail", _LogTail).focus()

    def action_resize(self, pane: str, delta: int) -> None:
        span = range(1, self._SHARE_TOTAL)  # 1..4: neither side collapses
        if pane == "log":
            self._log_share = min(max(self._log_share + delta, span.start), span[-1])
            # logbox, not #logtail: the pane (log + prompt) is what
            # participates in the vertical split against explain
            self.query_one("#logbox", _LogPane).styles.height = f"{self._log_share}fr"
            self.query_one(
                "#explain-box"
            ).styles.height = f"{self._SHARE_TOTAL - self._log_share}fr"
        else:
            self._table_share = min(max(self._table_share + delta, span.start), span[-1])
            # tablecol, not #jobs: the table sits inside the filter column,
            # which is what participates in the horizontal split (review)
            self.query_one("#tablecol").styles.width = f"{self._table_share}fr"
            self.query_one("#side").styles.width = f"{self._SHARE_TOTAL - self._table_share}fr"

    def action_send(self, verb: str) -> None:
        request = parse_console_command(verb, self._selected)
        console = self.query_one("#console", RichLog)
        if isinstance(request, str):
            console.write(Text(request, style="red"))
            return
        self.run_worker(self._do_sendevent(request), group="send", exclusive=False)

    def action_focus_console(self) -> None:
        if self.screen.maximized is not None:
            return  # never focus an input the maximized view is hiding
        self.query_one("#cmdline", Input).focus()

    # -------------------------------------------- filter / view / fold (DL-65)

    def action_focus_filter(self) -> None:
        # defense in depth: the pager's bindings shadow `/` while the log is
        # focused, but a focus hole must never reopen the type-into-an-
        # invisible-filter bug that motivated the pager (DL-67)
        if self.screen.maximized is not None:
            return
        line = self.query_one("#filterline", Input)
        line.styles.display = "block"
        line.focus()

    def clear_filter(self) -> None:
        line = self.query_one("#filterline", Input)
        line.value = ""
        self._filter = ""
        line.styles.display = "none"
        self.query_one("#jobs", DataTable).focus()
        self._render_table()

    @on(Input.Changed, "#filterline")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._render_table()

    @on(Input.Submitted, "#filterline")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            self.clear_filter()
            return
        self.query_one("#filterline", Input).styles.display = "none"
        self.query_one("#jobs", DataTable).focus()

    def action_cycle_view(self) -> None:
        self._view_mode = (self._view_mode + 1) % len(self._VIEW_MODES)
        self._render_table()

    def action_toggle_box(self) -> None:
        name = self._selected
        if name is None or self._filter or self._view_mode:
            return  # the tree only exists in the unfiltered 'all' view
        if not self._children().get(name):
            return  # not a box with members: nothing to fold
        self._collapsed.symmetric_difference_update({name})
        self._render_table()

    def action_toggle_all(self) -> None:
        if self._filter or self._view_mode:
            return
        if self._collapsed:
            self._collapsed.clear()
        else:
            kids = self._children()
            self._collapsed = {name for name in self._jobs_snapshot if kids.get(name)}
        self._render_table()

    @on(Input.Submitted, "#cmdline")
    def _on_command(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        request = parse_console_command(text, self._selected)
        console = self.query_one("#console", RichLog)
        if isinstance(request, str):
            console.write(Text(request, style="red"))
            return
        self.run_worker(self._do_sendevent(request), group="send", exclusive=False)

    async def _do_sendevent(self, request: dict[str, Any]) -> None:
        target = request.get("job") or request.get("name") or ""
        label = f"> {request.get('event')} {target}".rstrip()
        try:
            response = await self._client.request(request)
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            self._console_write(Text(f"{label}: not sent ({exc})", style="red"))
            return
        self._set_connected(True)
        if response.get("ok"):
            self._console_write(Text(f"{label}: ok @ {response.get('at', '')}", style="green"))
        else:
            self._console_write(Text(f"{label}: {response.get('error', 'refused')}", style="red"))
        await self._refresh()

    def _console_write(self, text: Text) -> None:
        try:
            self.query_one("#console", RichLog).write(text)
        except NoMatches:
            pass  # a worker resuming after an await can outlive the screen

    # ------------------------------------------------------------ teardown

    async def on_unmount(self) -> None:
        await self._client.close()
