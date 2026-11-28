"""ss11 Textual TUI: one thin app, terminal today and web via textual-serve (11e).

The app is a client of the control socket only, never in-process with the engine (DL-41 item 8:
textual-serve spawns one app per browser session, so an in-process engine would give every viewer
a private universe). `dsl41 ui --socket <run_root>/control.sock` attaches to a running engine.
`dsl41 run --ui` starts the engine and this app in one terminal; quitting stops that run, since
the engine is tethered to the process. A detached engine takes viewers via `dsl41 ui`.

Contract detail for DL-46, within DL-41's frame:

- Change feed, consumed idempotently (DL-45 item 5): a `subscribe` connection is only a wake-up
  signal. Every record schedules a state refresh and is never rendered, so the at-least-once
  dispatch/drop race costs a redundant refresh, not a duplicated row. The screen comes from
  idempotent queries: the jobs table from `status`, the commentary from the stored entries of
  `transitions`, the explain pane from `explain`. A 2s poll backstops a lost subscription, which
  reconnects with backoff. The viewer never sends `trace`.
- Stored history (DL-541): each pass draws the jobs table from `status` first, then reads the
  history. The viewer checks a `transitions` answer against the schema of the fields it reads
  and trusts the engine for the rest: one decoder takes a page whole or refuses it with a
  diagnostic naming the field, and every reader uses the typed page. The join is `transitions`
  backward, 500 entries, shown oldest first; its `[watermark, 2^63 - 1]` is the join point. A
  join is whole or not at all: a disconnect or a refusal in its page or its alarm scans leaves
  the viewer not joined. Each wake and poll then
  reads forward pages from the cursor with the history token, at most 4 per pass, the rest in a
  trailing pass, continuity and read header checked before a page's rows are used. A new token
  that continues the history is an engine restart: one console line at the boundary, and the
  console and the alarm times stay. A history that does not continue what is held, a page or a
  status answer under another read header, or no stored history any more forgets everything
  held and joins again. An expired cursor and a gap each write one line. `no_journal`,
  `transitions_unavailable` and `unknown_cmd` leave the table live and the console says once
  why there is no history. The console holds at most 10 000 lines; `E` opens the stored entries
  before the oldest it holds in the pager, a page per press, back to the retained floor; past
  the pager's 10 000 lines a page drops the newest. The pager's and the details popup's reads
  carry the history token on a connection of their own.
- The jobs table is the ss10 status response verbatim: status, status_at, run_number, exit_code,
  ice/hold/noexec flags, plus `pending_timers` (the oracle's own liveness filter, so display truth
  is dispatch truth) and per-run `log_out`/`log_err`. The alarms column counts a job's
  MUST_START_ALARM and MUST_COMPLETE_ALARM entries dated within 24 hours of `status.engine_now`,
  and such a job is a problem (DL-541). Alarm times exist only while `engine_now` is known.
  A scan has an anchor: everything at or below it is the scan's, everything above it the
  follow's. It reads backward per transition from the anchor, the anchor's own row included,
  until an entry is older than twice the window, at most 2000 rows and 8 requests each; a
  followed alarm above the anchor adds its time. The join's scan is anchored
  at the join point. When `engine_now` becomes known, the first time or after a status without
  it, or steps back by more than the window, one scan anchored at the follow cursor rebuilds the
  times. An alarm is dated by its deadline: after a stall or an outage longer than the window,
  an alarm raised late for an old deadline is not in Problems, and it can keep a viewer that
  joins then from seeing a different, recent alarm. The table's title marks the alarms
  incomplete after a scan cut by its bound or one that read a gap, or a followed page with a
  gap, until the next scan; no gap is dated by entry times. On a root upgraded in place the
  mark stays until an alarm of each kind older than twice the window is stored above the
  earlier build's gap, or retention prunes it; after a prune an attached viewer keeps the mark
  until its next scan, and a new viewer scans at its join. The title marks the alarms
  unavailable from an engine whose `status` has no `engine_now`.
- Explain pane (DL-535): for the selected job, one line per item, the instant the answer is for,
  `recovering`, the disposition with its deciding gate first and then the open gates and the
  context facts, the condition with its atoms, capacity (short buckets, holders or "nobody holds
  it", blocking waiters, waiters ahead), the dispatch wait and what would wake the job. A line
  naming a catalog job is a link, marked `›`; a guard, a self reference, an undefined or an
  external name is labelled and is no link. Each line is keyed by what it names, so the cursor
  stays on its line across answers. `e` focuses the pane; up and down move between links, the
  paging keys move the cursor with the view, Enter goes to the linked job (on a line scrolled
  out of view it only brings the line back), Backspace goes back, `b` switches to the jobs one
  hop downstream, Escape returns to the table. A hop is the operator's pick (`_choose`) of a link
  in the answer the pane shows for the selected job: the target's boxes are unfolded, a filter or
  a view that hides it gives way with a console line, and the hop lands on a status answer asked
  after it began, with the target selected and its row on screen. Until then the verb keys, `d`,
  space and a console verb without a job name are held, each with a console line; a change of
  the selection the hop did not make ends it with a line, and so does a status pass that
  fails, is refused or sees a new baseline. An answer is shown only for the latest
  request and while its job is selected. The downstream mode asks each downstream job's atoms
  once per selected job, configuration rev and baseline, for at most `_DETAIL_CAP` jobs. An
  engine whose answer has no `gates` gets the condition text and no links. The pane asks on a
  change of the selected job, a refresh and a change of mode only.
- The log tail is a byte tail of the current run's resolved runner-design ss6 append target
  (`log_out`/`log_err` from status; `o` toggles the stream), read from the local filesystem. The
  TUI runs on the engine host in both postures (textual-serve serves from the host, E3), so file
  reads need no protocol verb. It starts near the end (last 8 KiB), follows appends and resets on
  truncation. std_* paths carry verbatim (DL-39): a relative path resolves against the viewer's
  cwd, which matches the wrapper's only under `run --ui`. A missing file is a silent wait; any
  other read fault writes one red console line per distinct (path, error) and keeps polling
  (DL-187 item 10).
- Log pager (DL-67): whenever the log tail has focus, which `m` grants, it is a less-style pager.
  Focus is the mode switch: textual consults the focused widget's bindings before the app's, so
  one mechanism provides the pager verbs and shadows the operator verbs (`k` scrolls up, never
  KILLJOB; `f` and space page forward, never FORCE; `q` and escape leave the pager, never the
  app; verbs with no pager meaning ring the bell). Paging never mutates the estate. `/` and `?`
  search (regex, smartcase, all matches reverse-video, `n`/`N` wrap in the less directions); `&`
  shows only matching lines, a view of the buffer; an empty submit clears and escape cancels the
  prompt only (ESCAPE_TO_MINIMIZE is off so textual cannot swallow it). Follow is
  pinned-at-bottom: scrolling up pauses ([paused] in the title) and `F`/`G`/End resume. Buffer,
  filter view and widget cap at _PAGER_BUFFER_LINES in lockstep. A search or filter is matched
  under the pager's processor-time limit of DL-514, and a new search matches only the lines the
  filter shows: one refused at the prompt leaves the view as it was, and one in force that new
  lines stop is cleared, with a note in the title. `m` maximizes the
  log pane (tail plus prompt line) so the prompt stays visible. `m`/`o`/`r`, `F1` and the resize
  keys pass through to the app.
- The event console (`:`) accepts exactly the ss10 sendevent verbs (job verbs; SET_GLOBAL
  NAME=value; CHANGE_STATUS [job] STATUS [exit_code]); an omitted job means the selected job.
  Escape clears the typed line and returns focus to the table. Every request and response is
  echoed; refusals render red and change nothing. The server validates against the catalog and
  the TUI never pre-judges. The echo names the whole request, including a global's value, a
  status and an exit code (DL-187 item 9). CHANGE_STATUS's status-first shorthand applies only
  when the next token is not itself a status; otherwise the line is the explicit
  `<job> STATUS [exit_code]` form (item 8).
- Safety (DL-187): the verb keys live on the jobs table, not the app, so they fire and the Footer
  offers them only while the table has focus; `k` in the console log or explain pane is dead.
  KILLJOB and FORCE_STARTJOB from a key open a confirm first (a typed KILLJOB is already intent),
  naming the target's status, the revision and, for a box, the members it reaches. The revision
  is frozen before the modal opens, so a job that moved meanwhile is refused by DL-90. Quit
  follows posture: a detached viewer exits at once ("detach"); `run --ui` owns the engine, so
  quitting stops the run and is confirmed first ("stop run").
- Help (`F1`, and `?` outside the pager) toggles Textual's context help panel. Every pane and
  popup with keys of its own carries a short `HELP` string, read off the focused widget's
  ancestors. An unknown verb typed at `:` is answered with the sorted list of verbs (DL-187
  item 7). F1 on the console line opens a full-width screen of every verb with its key and
  meaning, every status CHANGE_STATUS takes and three examples, from the tables in
  `console_words`; it scrolls from the keyboard, and closing it returns to the line as it was
  (DL-516). The line suggests the rest of the word being typed in grey, never a verb whose key
  asks first; Right at the end of the line or Tab takes it. Tab never leaves the line: with
  nothing to take it rings the bell (DL-516).
- Triggers view (`t`, DL-68): "what fires next". It re-queries the ss10 `timers` verb (pending
  oracle timers merged with each scheduled job's next calendar tick, due-ordered by the server)
  every 2s while open, each row with a full host-local due (DL-473) and a countdown. Armed jobs
  (the SEM-32 latch from `status`) append below every dated row with no due: the next condition
  edge starts them. Due-less rows render generically. It is read-only: every operator verb is
  shadowed to the bell (DL-67) and `t`/`q`/escape close. The jobs table's flags column carries
  `A` for the same latch. An ok:false `timers` answer leaves the rows as they were, names the
  refusal on the border title and marks the app subtitle as a status or transitions refusal does
  (DL-187 item 6).
- Job details popup (`d` or Enter on a row): the ss10 `spec` verb's preserve-rendered JIL block
  (the post-placeholder source this engine loaded), topped with the status facts, the `deps`
  verb's needs/blocks lines (the blast radius, DL-65; from `upstream_atoms` when served, with a
  guard and a self reference labelled and never a dependency, DL-535), the run's stored
  scheduler refusal total (DL-541: "loading" when the popup opens, then read once, back to the
  entry that opens the run, from pages of the history its own `status` answer names. It is
  exact, "at least N" or unknown: "at least" past a bound, over a gap above that entry or when it
  is not reached; "unknown" when that is zero, when a page is not of that history or holds an
  entry the server cut, on any refusal, transport failure or malformed answer, or when the
  engine names no history. "unknown" stays until the popup is opened again) and a short local
  log tail. The header
  clock and owned timestamps use the host's local time as YYYY-MM-DDTHH:MM:SS (DL-473).
- Navigation at estate scale (DL-65): the jobs table renders the box tree (indent from the status
  response's box_name; space folds the selected box, `z` folds or unfolds all; a folded box shows
  its hidden descendant count and a red problem tally, so a fold never hides a FAILURE). In the
  tree, a first-button click on a box's indent or marker selects and folds it. The table's lower
  border names the fold keys: `space fold · z fold all` on a box with members, `z fold all` on
  another row of a tree that has one, nothing in a flat view (DL-512). `/` opens
  the filter line (DL-511): words that all match, each a name pattern, a status or box test, or
  negated, read at each keystroke. A refused line shows its message under the line and leaves the
  filter in force; Enter keeps a good line, Esc clears. The filter is matched under the jobs
  filter's processor-time limit of DL-514: a draft it stops is refused, and a filter in force it
  stops later, by a status answer or a change of view, is cleared, with a console line. With the
  log pager focused `/` searches
  the log instead. `v` cycles the view all, problems, active.
  Filtered and non-'all' views are flat, so a match inside a folded box is never invisible. The
  console is on `:`. The status response's spec_drift flag renders in the subtitle: files changed
  on disk, the running catalog is the truth.
- Pane geometry (DL-520): three splits, the table column against the side column and the explain
  pane against the log pane as ratios, the console as rows, kept for the session and across a
  change of the terminal's size. A first-button drag on a split's border, the two lines that
  meet there, moves it with the pointer; the press, the drag, the release and its click reach no
  widget, and the lines
  take the accent colour under the pointer and during the drag. `+`/`=` and `-` resize the
  focused pane by 5 points or 2 rows; `]`/`[` move the log against explain and `}`/`{` the jobs
  table against the side column by a fifth. No pane goes under 20 columns, the explain pane
  under 3 rows, the log under 5, the console box under 6, the area above it under 8. `m` toggles
  maximizing the log pane, which then fills the screen, and hands focus to the pager (`q`/escape also restore); nothing
  resizes meanwhile. While anything is maximized the tree filter and the console never take
  focus, so an input the operator cannot see never swallows keystrokes (DL-67).
- One request connection, lock-serialized (the server answers one line per line); `subscribe`
  owns its connection until hangup (ss10). A dead socket flips the header subtitle to
  "disconnected", is reported once to the console, and every path retries quietly, so the TUI
  outlives engine restarts. A cancelled exchange drops the connection so a replaced worker
  cannot leave an unread response to desync the next request. There are no client-side timeouts:
  a live-but-wedged engine parks the data plane while quit stays responsive, and recovering a
  stuck engine is the operator's call.

The textual import is guarded (runner-design ss14): the core package keeps its three runtime
deps; this module needs `pip install 'dsl41[ui]'`.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import reprlib
import shlex
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from rich import box
    from rich.cells import cell_len
    from rich.console import Group
    from rich.style import Style
    from rich.table import Table
    from rich.text import Text
    from textual import events, on
    from textual.app import App, ComposeResult, RenderResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.css.query import NoMatches
    from textual.events import Click
    from textual.message import Message
    from textual.screen import ModalScreen, Screen
    from textual.suggester import Suggester
    from textual.geometry import Region
    from textual.widget import Widget
    from textual.widgets import DataTable, Footer, Header, HelpPanel, Input, RichLog, Static

    # private textual module, deliberately: the Header composes its clock
    # internally with no timezone hook, and these names are stable across
    # the pinned textual>=8 family (DL-46). Revisit at any floor bump.
    from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
except ModuleNotFoundError as exc:  # pragma: no cover -- exercised via CLI guard
    raise ModuleNotFoundError(
        "the dsl41 TUI needs the optional [ui] extra: pip install 'dsl41[ui]'"
    ) from exc

from dsl41.console_words import STATUS_ROWS, VERB_ROWS, complete_line
from dsl41.display_time import format_local_at
from dsl41.job_filter import JobFilter, read_filter
from dsl41.match_bound import Stopped, run_bounded
from dsl41.pane_layout import Sizes, Split, place, set_cells, step
from dsl41.runner_access import Principal
from dsl41.start_refusals import scheduler_refusal_counts
from dsl41.runner_admission import addressed_key
from dsl41.runner_clock import EngineError
from dsl41.runner_control import (
    APPLIED,
    JOB_EVENT_VERBS,
    REFUSED,
    REJECTED,
    UNKNOWN,
    status_flags,
    STATUSES,
    ControlClient,
    ATTEMPT_NOTE_WIDTH,
    ControlClientError,
    attempt_note,
    reread_note,
    claimed_actor,
    command,
    original_decision_text,
    outcome_of,
    read_for,
    revision_in,
)


_ALARM_TRANSITIONS = frozenset({"MUST_START_ALARM", "MUST_COMPLETE_ALARM"})
#: DL-541: the console's bound in rendered lines, and the stored entries a join reads
_CONSOLE_LINES = 10_000
_JOIN_LIMIT = 500
#: DL-541: a job is a problem by alarms while the engine's present time is at most this far
#: past the time an alarm is dated by; alarm times are kept for twice as long
_ALARM_WINDOW = timedelta(hours=24)
#: DL-541: the rows and the requests one backward read may spend: an alarm scan per name, or
#: a refusal total
_SCAN_ROWS = 2000
_SCAN_REQUESTS = 8
#: DL-541: the forward pages one pass follows before it leaves the rest to a trailing pass
_FOLLOW_PAGES = 4
#: the largest part of a `transitions` key (control-protocol ss4)
_KEY_MAX = 2**63 - 1
#: DL-541: the refusals that mean this engine keeps no history a viewer can read
_NO_HISTORY_CODES = frozenset({"no_journal", "transitions_unavailable", "unknown_cmd"})
#: a stored entry's key, `(seq, n)` (control-protocol ss4 `transitions`)
type _Key = tuple[int, int]
_STATUS_STYLE = {
    "INACTIVE": "dim",
    "STARTING": "cyan",
    "RUNNING": "bold yellow",
    "SUCCESS": "green",
    "FAILURE": "bold red",
    "TERMINATED": "magenta",
}
_TAIL_SEED_BYTES = 8192  # start a fresh tail this close to EOF
#: the details popup's one-shot log preview: read this many trailing bytes, keep the last
#: `_DETAILS_TAIL_LINES` of them (DL-178h). It is smaller than `_TAIL_SEED_BYTES`, which seeds a
#: live polled tail, because a ten-line snapshot needs less.
_DETAILS_TAIL_BYTES = 4096
_DETAILS_TAIL_LINES = 10
#: pager memory bound: buffer, widget and display list cap here in lockstep.
_PAGER_BUFFER_LINES = 10_000
#: seconds of processor time one pass of the jobs filter may use (DL-514)
_FILTER_LIMIT_S = 0.25
#: seconds of processor time one pass of the log pager may use; an honest pattern costs more
#: over 10 000 log lines than over the job names (DL-514)
_PAGER_LIMIT_S = 5.0
_COLUMNS = ("job", "status", "attempt", "at", "run", "exit", "flags", "timers", "alarms")
#: fixed render widths for the bounded columns (the wider of content and header label).
#: Auto-width stays on job and timers, whose content varies. Fixed widths keep update_cell's
#: update_width off these columns: textual's width recompute re-measures a whole column per
#: updated cell, which is O(cells x rows) at estate scale (~25s of blocked loop at 519 jobs).
_COLUMN_WIDTHS = {
    "status": 10,
    "attempt": ATTEMPT_NOTE_WIDTH,
    "at": 19,
    "run": 4,
    "exit": 4,
    "flags": 5,
    "alarms": 6,
}
#: every first token `:` accepts, sorted: what an unknown verb's error lists (DL-187 item 7).
#: The console's HELP lists `VERB_ROWS`, which a test holds to the same verbs (DL-516).
_CONSOLE_VERBS = sorted(JOB_EVENT_VERBS | {"SET_GLOBAL", "CHANGE_STATUS"})


#: the console's opening sentence, in its HELP and on the verbs screen (DL-516)
_CONSOLE_OPENING = (
    "Type a verb and Enter; a verb without a job acts on the selected job, and grey text is a"
    " suggestion that Right or Tab takes."
)


def console_words_view() -> Group:
    """The verbs screen's body, built from the word tables of `console_words` (DL-516): the
    opening sentence, the verbs, the statuses and three examples. Cells are plain Text, so a
    `[job]` is not read as markup. A cell wraps at spaces; each first column is as wide as its
    longest word, so no word is cut at 80 columns."""
    verbs = Table(box=box.SIMPLE_HEAD, show_edge=False, title="Verbs", title_justify="left")
    verbs.add_column("Verb", min_width=max(len(row.verb) for row in VERB_ROWS))
    verbs.add_column("Key", min_width=len("f, asks"))
    verbs.add_column("Meaning")
    for row in VERB_ROWS:
        key = f"{row.key}, asks" if row.asks else (row.key or "")
        verbs.add_row(Text(f"{row.verb} {row.args}".rstrip()), Text(key), Text(row.meaning))
    statuses = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        title="Statuses of CHANGE_STATUS",
        title_justify="left",
    )
    statuses.add_column("Status", min_width=max(len(row.status) for row in STATUS_ROWS))
    statuses.add_column("Meaning")
    for status_row in STATUS_ROWS:
        statuses.add_row(Text(status_row.status), Text(status_row.meaning))
    examples = Table(box=box.SIMPLE_HEAD, show_edge=False, title="Examples", title_justify="left")
    examples.add_column("Line", min_width=len("CHANGE_STATUS"))
    examples.add_column("Meaning")
    examples.add_row("CHANGE_STATUS SUCCESS", "the selected job counts as succeeded")
    examples.add_row("CHANGE_STATUS ex_load FAILURE 3", "ex_load counts as failed, exit code 3")
    examples.add_row("SET_GLOBAL NAME=value", "the global NAME takes the value")
    notes = Text(
        "A key marked asks opens a confirm first. KILLJOB and FORCE_STARTJOB are typed in"
        " full; the line does not complete them.\n"
        "ON_HOLD, ON_ICE and ON_NOEXEC do nothing to a job that is STARTING or RUNNING. On a"
        " box, ON_NOEXEC sets the box and every job in it INACTIVE at once, SUCCESS included;"
        " it does nothing if the box or a job in it is live, queued or on ice.",
        style="dim",
    )
    return Group(Text(_CONSOLE_OPENING), Text(), verbs, notes, Text(), statuses, Text(), examples)


def _is_scheduler_refusal(entry: Mapping[str, Any]) -> bool:
    """Only an explicit scheduler origin permits collapsing (DL-480)."""
    return entry.get("transition") == "START_REFUSED" and entry.get("start_source") == "scheduler"


@dataclass
class _RefusalRun:
    cause: str
    last_clock: str
    repeats: int = 0


class _HistoryEnds(Exception):
    """A `transitions` page does not continue the history it was read for (DL-541). `kind` is
    `refused`, `gone` (no stored history), `moved` (another read header) or `broken` (the
    stored history does not continue past `through`)."""

    def __init__(self, kind: str, through: int | None = None) -> None:
        super().__init__(kind)
        self.kind, self.through = kind, through


@dataclass
class _Scan:
    """What an alarm scan read (DL-541): the times by job and key, and whether it was cut."""

    times: dict[str, dict[_Key, datetime]]
    cut: bool


# ----------------------------------------------- the `transitions` answer (DL-541)
#
# The viewer checks the schema of what it reads and trusts the engine for the rest: each field
# it reads is present where the contract says it always is and has its type. An answer that
# is not is refused whole, with a diagnostic naming the verb, the field's path, what was
# expected and what came. Relations between fields are the engine's to keep; the tests hold
# the engine to them (`tests/transitions_conformance.py`).


class MalformedPage(ValueError):
    """A `transitions` answer that does not fit the schema of what the viewer reads."""


_ABSENT = object()


#: the longest diagnostic a malformed answer gives, ellipsis included
_DIAGNOSTIC_MOST = 200


def _schema_error(path: str, expected: str, value: Any) -> MalformedPage:
    found = "nothing" if value is _ABSENT else f"{type(value).__name__} {reprlib.repr(value)}"
    text = f"transitions: {path}: expected {expected}, got {found}"
    if len(text) > _DIAGNOSTIC_MOST:
        text = text[: _DIAGNOSTIC_MOST - 1] + "…"
    return MalformedPage(text)


def _typed(container: Mapping[str, Any], name: str, path: str, kind: str) -> Any:
    """The field `name`, present and of `kind`: string, integer (never a boolean), boolean,
    list, or a key (a pair of integers)."""
    value = container.get(name, _ABSENT)
    ok = {
        "a string": isinstance(value, str),
        "an integer": type(value) is int,
        "a boolean": type(value) is bool,
        "a list": isinstance(value, list),
        "a key [seq, n]": isinstance(value, list)
        and len(value) == 2
        and all(type(part) is int for part in value),
    }[kind]
    if not ok:
        raise _schema_error(f"{path}{name}", kind, value)
    return value


@dataclass(frozen=True)
class PageEntry:
    """One entry of a decoded page: the `trace` entry's fields, typed, and its key."""

    seq: int
    n: int
    at: datetime
    at_text: str
    job: str
    transition: str
    cause: str
    start_source: str | None = None
    run_number: int | None = None
    refused_status: str | None = None
    cut: bool = False

    @property
    def key(self) -> _Key:
        return self.seq, self.n

    def fields(self) -> dict[str, Any]:
        """The entry as the console and the refusal count read it: its decoded fields only."""
        out: dict[str, Any] = {
            "seq": self.seq,
            "n": self.n,
            "at": self.at_text,
            "job": self.job,
            "transition": self.transition,
            "cause": self.cause,
        }
        for name in ("start_source", "run_number", "refused_status"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        return out


type _Interval = tuple[int, int]


@dataclass(frozen=True)
class TransitionPage:
    """The fields of a `transitions` answer the viewer reads, typed (control-protocol ss4)."""

    baseline_id: str
    entries: tuple[PageEntry, ...]
    next: _Key | None
    more: bool
    floor: int
    expired: bool
    pruned: tuple[_Interval, ...]
    gaps: tuple[_Interval, ...]
    watermark: int
    history: str | None
    continuous_through: int | None = None


def _schema_entry(value: Any, path: str) -> PageEntry:
    if not isinstance(value, Mapping):
        raise _schema_error(path, "an object", value)
    at_text = _typed(value, "at", f"{path}.", "a string")
    at = _naive_utc(at_text)
    if at is None or at.isoformat() != at_text:  # a timestamp as `trace` writes it
        raise _schema_error(f"{path}.at", "a naive UTC timestamp as `trace` writes it", at_text)
    optional: dict[str, Any] = {}
    for name in ("start_source", "refused_status"):
        if name in value:
            optional[name] = _typed(value, name, f"{path}.", "a string")
    if "run_number" in value:
        optional["run_number"] = _typed(value, "run_number", f"{path}.", "an integer")
    if "cut" in value:
        optional["cut"] = _typed(value, "cut", f"{path}.", "a boolean")
    return PageEntry(
        seq=_typed(value, "seq", f"{path}.", "an integer"),
        n=_typed(value, "n", f"{path}.", "an integer"),
        at=at,
        at_text=at_text,
        job=_typed(value, "job", f"{path}.", "a string"),
        transition=_typed(value, "transition", f"{path}.", "a string"),
        cause=_typed(value, "cause", f"{path}.", "a string"),
        **optional,
    )


def _schema_intervals(answer: Mapping[str, Any], name: str) -> tuple[_Interval, ...]:
    out: list[_Interval] = []
    for index, value in enumerate(_typed(answer, name, "", "a list")):
        if not (isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value)):
            raise _schema_error(f"{name}[{index}]", "a seq interval [low, high]", value)
        out.append((value[0], value[1]))
    return tuple(out)


@dataclass(frozen=True)
class Refusal:
    """An `ok: false` answer's envelope (control-protocol ss2): `code` names the reason and may
    be missing on a replayed decision; `error` is prose."""

    code: str | None
    error: str


def decode_refusal(answer: Mapping[str, Any]) -> Refusal:
    """The envelope of an `ok: false` answer (control-protocol ss2): `error` is a string, and
    so is `code` when present; a replayed decision may omit `code`."""
    code = answer.get("code")
    if "code" in answer:
        code = _typed(answer, "code", "", "a string")
    error = _typed(answer, "error", "", "a string")
    return Refusal(code, error.strip())


def decode_transitions(answer: Any, request: Mapping[str, Any]) -> TransitionPage:
    """The fields of `answer` the viewer reads, typed, or `MalformedPage` with a diagnostic
    (DL-541). A refusal is classified before this. A field the viewer does not read, and any
    relation between fields, is not checked: the engine is trusted for them."""
    if not isinstance(answer, Mapping):
        raise _schema_error("(answer)", "an object", answer)
    if answer.get("ok") is not True:
        raise _schema_error("ok", "true", answer.get("ok", _ABSENT))
    entries = tuple(
        _schema_entry(value, f"entries[{index}]")
        for index, value in enumerate(_typed(answer, "entries", "", "a list"))
    )
    following = answer.get("next", _ABSENT)
    if following is not None:
        following = tuple(_typed(answer, "next", "", "a key [seq, n]"))
    history = answer.get("history", _ABSENT)
    if history is not None:
        history = _typed(answer, "history", "", "a string")
    through = None
    if request.get("history") is not None:
        through = _typed(answer, "continuous_through", "", "an integer")
    return TransitionPage(
        baseline_id=_typed(answer, "baseline_id", "", "a string"),
        entries=entries,
        next=following,
        more=_typed(answer, "more", "", "a boolean"),
        floor=_typed(answer, "floor", "", "an integer"),
        expired=_typed(answer, "expired", "", "a boolean"),
        pruned=_schema_intervals(answer, "pruned"),
        gaps=_schema_intervals(answer, "gaps"),
        watermark=_typed(answer, "watermark", "", "an integer"),
        history=history,
        continuous_through=through,
    )


def _just_above(key: _Key) -> _Key:
    """The least key above `key`: a backward page from it holds `key` itself."""
    seq, n = key
    return (seq, n + 1) if n < _KEY_MAX else (seq + 1, 0)


@dataclass
class _PagerPin:
    """The pager shows stored entries (DL-541). `before` is the key the next page reads
    before, None once a page reached the retained floor; `start` is where the first page read
    from, under `history` and `baseline`; `reading` while a page is asked."""

    before: _Key | None
    start: _Key
    history: str | None
    baseline: str | None
    reading: bool = False


def _wire_key(value: Any) -> _Key | None:
    """A `[seq, n]`, or None unless it is a pair of integers."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    if not all(type(part) is int for part in value):
        return None
    return int(value[0]), int(value[1])


def _entry_key(entry: Mapping[str, Any]) -> _Key | None:
    """A stored entry's `(seq, n)`, or None for an entry without one."""
    return _wire_key([entry.get("seq"), entry.get("n")])


def _naive_utc(value: Any) -> datetime | None:
    """An `at` or `engine_now` as the engine writes it, naive UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is None else None


def _refusal_line(row: Mapping[str, Any], total: str) -> Text:
    """The details popup's line for a run's scheduler refusals (DL-541)."""
    run = "queued" if row.get("status") == "QUE_WAIT" else f"run {row.get('run_number')}"
    return Text(f"scheduler refusals: {total} ({run})\n")


def seq_words(intervals: Any) -> str:
    """Seq intervals `[[low, high], ...]` as words: `seq 7`, `seqs 31-35, 40`; empty for
    none."""
    parts: list[str] = []
    for interval in intervals if isinstance(intervals, list) else []:
        bounds = _wire_key(interval)
        if bounds is not None:
            low, high = bounds
            parts.append(str(low) if low == high else f"{low}-{high}")
    if not parts:
        return ""
    return (
        f"seq {parts[0]}" if len(parts) == 1 and "-" not in parts[0] else f"seqs {', '.join(parts)}"
    )


def entry_line(entry: Mapping[str, Any]) -> Text:
    """The line the console and the pager show for one entry."""
    transition = str(entry.get("transition", ""))
    if transition in _ALARM_TRANSITIONS:
        style = "bold red"
    elif transition == "START_REFUSED":
        # DL-64: a refused operator start answers "why did nothing happen"
        style = "yellow"
    else:
        style = "dim"
    clock = format_local_at(entry.get("at"))
    return Text(
        f"{clock} {entry.get('job', '')} {transition} [{entry.get('cause', '')}]", style=style
    )


def _cell_sig(value: Any) -> tuple[str, str]:
    """Comparable identity of a rendered cell: (plain text, style); Text.__eq__ ignores style."""
    if isinstance(value, Text):
        return value.plain, str(value.style)
    return str(value), ""


class _TableSync:
    """One DataTable refresh engine (DL-178v), shared by TriggersScreen and RunnerApp.

    Unchanged identity (row order and membership) is a steady state: it diffs cell signatures
    and pushes only changed cells, so a ticking countdown does not queue every cell for textual's
    width recompute. Changed identity clears and rebuilds every row, restoring the cursor to
    whichever identity survived. `_COLUMN_WIDTHS` is applied here for every caller.

    `identity` decides rebuild versus steady state and indexes `sigs`; `keys` is the DataTable
    row key per row in the same order. They differ for TriggersScreen, whose rows are keyed by
    position but identified by (job, kind, detail)."""

    def __init__(self) -> None:
        self.identity: list[Any] = []
        self.sigs: list[list[tuple[str, str]]] = []

    def refresh(
        self,
        table: DataTable[Any],
        *,
        identity: Sequence[Any],
        keys: Sequence[str],
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        on_rebuild: Callable[[Sequence[Any], Any], Any | None] | None = None,
        column_widths: Mapping[str, int] | None = None,
    ) -> None:
        """On a membership or order change, `on_rebuild` runs once, after clear() and before any
        add_row. It gets `(new_identity, old_selected)`: the identity about to be shown and the
        identity the cursor sat on, read before clear() resets `table.cursor_row` (DL-178v). It
        returns the identity to restore the cursor to, or None; with None the cursor stays on
        row 0, where clear() and add_row leave it."""
        resized = False
        for column_info in table.ordered_columns:
            width = (column_widths or {}).get(column_info.key.value or "")
            if width is not None and column_info.width != width:
                column_info.width = width
                resized = True
        if list(identity) != self.identity or resized:
            cursor = table.cursor_row
            old_selected = self.identity[cursor] if 0 <= cursor < len(self.identity) else None
            table.clear()
            target = on_rebuild(identity, old_selected) if on_rebuild is not None else None
            self.identity = list(identity)
            self.sigs = []
            for key, cells in zip(keys, rows):
                self.sigs.append([_cell_sig(c) for c in cells])
                table.add_row(*cells, key=key)
            if target is not None and target in self.identity:
                table.move_cursor(row=self.identity.index(target))
            return
        for i, (key, cells) in enumerate(zip(keys, rows)):
            sigs = [_cell_sig(c) for c in cells]
            old = self.sigs[i]
            if sigs == old:
                continue
            for col_i, (column, cell, sig) in enumerate(zip(columns, cells, sigs)):
                if old[col_i] == sig:
                    continue
                # update_width only for an auto-width column whose width changed
                widen = column not in _COLUMN_WIDTHS and cell_len(old[col_i][0]) != cell_len(sig[0])
                table.update_cell(key, column, cell, update_width=widen)
            self.sigs[i] = sigs


class _LocalHeaderClock(HeaderClock):
    """Full host-local time, with 19 content columns even at 80 columns (DL-473)."""

    DEFAULT_CSS = """
    _LocalHeaderClock { width: 21; }
    """

    def render(self) -> RenderResult:
        return Text(format_local_at(datetime.now(UTC)))


class _LocalHeader(Header):
    """Stock Header with the full host-local clock (DL-473)."""

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _LocalHeaderClock()


#: spliced into every modal's BINDINGS (DL-187 item 7): a modal chain blocks app bindings
#: (screen.py:449), so help must be reachable from each modal.
_HELP_BINDINGS = (
    Binding("f1", "app.toggle_help_panel", "help", show=False),
    Binding("question_mark", "app.toggle_help_panel", "help", show=False),
)
#: spliced into a read-only modal's BINDINGS: operator verbs and pane keys ring the bell as the
#: log pager does; check_action=False would let the key fall through to the app binding it
#: shadows (DL-67)
_OPERATOR_BELLS = tuple(
    Binding(key, "app.bell", "", show=False)
    for key in (
        *("s", "f", "k", "i", "I", "h", "H", "n", "N"),
        *("d", "m", "o", "slash", "v", "space", "z", "colon", "e", "E", "]", "[", "}", "{"),
        *("plus", "equals_sign", "minus"),
    )
)


class SpecScreen(ModalScreen[None]):
    """Job-details popup: runtime facts and the `spec` verb's JIL block, the source the running
    engine loaded, not the file on disk. Escape, enter, d or q closes; `q` is the pager's leave
    key and must not fall through to the app's quit (DL-187)."""

    HELP = """
    The running engine's own facts for one job: status, dependencies,
    the loaded JIL source, and a short log tail.

    The run's scheduler refusals are read once, when the popup opens
    (DL-541): exact, "at least N", or "unknown" when the stored history
    cannot prove the count. "unknown" stays until the popup is opened
    again.

    q, escape, enter, or d closes.
    """
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("enter", "dismiss", "close", show=False),
        Binding("d", "dismiss", "close", show=False),
        Binding("q", "dismiss", "close", show=False),
        *_HELP_BINDINGS,
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
        self._static = Static(body)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="specbox"):
            yield self._static

    def show(self, body: Text) -> None:
        """A body read after the popup opened, such as the refusal total (DL-541)."""
        self._static.update(body)

    def on_unmount(self) -> None:
        # DL-541: a closed popup's history read in flight is cancelled; the reads connection
        # drops an exchange that is cancelled, so the next reader starts on a fresh one
        self.app.workers.cancel_group(self.app, "refusals")

    def on_mount(self) -> None:
        box = self.query_one("#specbox")
        box.border_title = self._title
        box.border_subtitle = "q/esc close"
        box.focus()


#: names listed in a box confirm before the rest is summed as a count
_CONFIRM_MEMBER_CAP = 8


def confirm_body(
    verb: str, target: str, status: str, revision: int | None, members: Sequence[str]
) -> str:
    """The ConfirmScreen text for one job verb: what is sent, the state it is sent against and,
    for a box, who else it reaches. Pure, so the blast radius is testable."""
    lines = [
        f"{verb} {target}",
        f"now {status or '?'} at revision {revision if revision is not None else '?'}",
    ]
    if members:
        shown = ", ".join(members[:_CONFIRM_MEMBER_CAP])
        extra = len(members) - _CONFIRM_MEMBER_CAP
        lines.append(
            f"box: reaches {len(members)} member(s) -- {shown}"
            + (f" and {extra} more" if extra > 0 else "")
        )
    return "\n".join(lines)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no gate in front of a destructive key (DL-187).

    Typed console commands are not gated; `k` sits one keystroke from every navigation key.
    Enter or `y` confirms; escape, `n` and `q` cancel, since `q` leaves every other view."""

    HELP = """
    Enter or y confirms; escape, n, or q cancels. Nothing is sent until
    confirmed, and then only against the state and revision named above.
    """
    BINDINGS = [
        Binding("enter", "confirm", "confirm"),
        Binding("y", "confirm", "yes", show=False),
        Binding("escape", "cancel", "cancel"),
        Binding("n", "cancel", "no", show=False),
        Binding("q", "cancel", "no", show=False),
        *_HELP_BINDINGS,
    ]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirmbox {
        width: 70%; max-width: 90; height: auto; max-height: 60%;
        border: round $warning; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirmbox"):
            yield Static(self._body)

    def on_mount(self) -> None:
        box = self.query_one("#confirmbox")
        box.border_title = self._title
        box.border_subtitle = "enter/y confirm -- esc/n/q cancel"
        box.focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


def format_countdown(due: datetime, now: datetime) -> str:
    """Humanized time-to-due ("42s", "3m12s", "2h05m", "1d03h"); "-" once due or overdue."""
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
    """(due, in, job, kind, detail) rows for the triggers view: the server's due-ordered `timers`
    entries, due-less rows (filewatch shows "watching") below them, then one name-sorted row per
    armed job (the SEM-32 latch: no due, the next condition edge starts it)."""
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
            dated.append((format_local_at(due), format_countdown(due, now), job, kind, detail))
        else:
            undated.append(
                (
                    format_local_at(entry.get("due")),
                    "watching" if kind == "filewatch" else "-",
                    job,
                    kind,
                    detail,
                )
            )
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
    """The details popup's trigger lines (DL-68): started by, the armed latch, a live filewatch,
    the earliest dated timer for this job, then every pending timer."""
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
        lines.append(f"next: {first.get('kind', '?')} @ {format_local_at(first['due'])}")
    pending = [e for e in row.get("pending_timers") or [] if isinstance(e.get("due"), str)]
    if pending:
        lines.append("pending timers:")
        for entry in pending:
            lines.append(f"  {entry.get('kind', '?')} @ {format_local_at(entry['due'])}")
    return lines


class ConsoleWordsScreen(ModalScreen[None]):
    """The event console's verbs and statuses on a screen of its own, at full width (DL-516):
    the library's help panel is too narrow for them. F1 on the console line opens it; Escape,
    `q` and F1 close it, and focus goes back to the line as it was. It scrolls from the
    keyboard and the wheel, and it is read-only: the operator verbs ring the bell (DL-67)."""

    HELP = """
    The console's verbs, the statuses of CHANGE_STATUS and examples.
    Up, Down, PageUp, PageDown, Home and End scroll; q, escape or F1
    closes.
    """
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close", show=False),
        Binding("f1", "dismiss", "close", show=False),
        *_OPERATOR_BELLS,
    ]
    CSS = """
    ConsoleWordsScreen { align: center middle; }
    #wordsbox {
        width: 100%; height: 100%;
        border: round $primary; background: $surface; padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="wordsbox"):
            yield Static(console_words_view(), id="words")

    def on_mount(self) -> None:
        box = self.query_one("#wordsbox")
        box.border_title = "console verbs and statuses"
        box.border_subtitle = "q/esc/F1 close"
        box.focus()


class TriggersScreen(ModalScreen[None]):
    """DL-68 triggers view: the `timers` verb live (2s re-query while open) plus the armed
    latches from the last status snapshot. Read-only: every operator verb is shadowed to the
    bell (DL-67). t, q and escape close; r re-queries."""

    HELP = """
    Read-only (DL-68): every operator verb rings the bell here and
    touches nothing in the estate.

    Rows are due-ordered timers plus armed jobs waiting on a condition
    edge. r re-queries now; q, escape, or t closes.
    """
    _COLUMNS = ("due", "in", "job", "kind", "detail")
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close", show=False),
        Binding("t", "dismiss", "close", show=False),
        Binding("r", "refresh_now", "refresh", show=False),
        *_HELP_BINDINGS,
        *_OPERATOR_BELLS,
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
        table.border_subtitle = "q/esc/t close"
        table.focus()
        self._table_sync = _TableSync()
        self.action_refresh_now()
        self.set_interval(2.0, self.action_refresh_now)

    def action_refresh_now(self) -> None:
        # the bound method, not a coroutine: a replaced exclusive worker must not leave a
        # never-awaited coroutine (the RunnerApp explain-worker pattern)
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
        try:
            table = self.query_one("#trigbox", DataTable)
        except NoMatches:
            return  # a worker resuming after the await can outlive the screen
        if not response.get("ok"):
            # DL-187 item 6 (P1-1): the last good rows stay; the refusal is named on the border
            # and, through the app's keyed memo, the subtitle, as for status and transitions
            error = str(response.get("error", "")).strip()
            # P1-3: a Text title, because border_title strings parse as rich markup and this
            # error is the server's
            table.border_title = Text(f"triggers (refused: {error})")
            app._set_query_fault("timers", error)
            return
        app._set_query_fault("timers", None)
        timers = response.get("timers", [])
        rows = assemble_trigger_rows(
            timers, app._jobs_snapshot, datetime.now(UTC).replace(tzinfo=None)
        )
        # a membership or order change rebuilds and restores the cursor to the same trigger if it
        # survived; a clear() every 2s would bounce the cursor and scroll (DL-67)
        ids = [(job, kind, detail) for _, _, job, kind, detail in rows]
        self._table_sync.refresh(
            table,
            identity=ids,
            keys=[str(i) for i in range(len(rows))],
            columns=self._COLUMNS,
            rows=rows,
            on_rebuild=lambda _new_ids, old_selected: old_selected,
        )
        table.border_title = f"triggers ({len(rows)})"


#: the style meta key on a box row's indent and marker, naming the box: a click there folds it
#: (DL-512)
_FOLD_META = "fold"


class _JobsTable(DataTable[Any]):
    """The jobs table, the only place the operator verbs are bound (DL-187).

    A widget binding fires only while this table has focus, and the Footer (which renders
    screen.active_bindings along the focus chain) offers the verbs only there. As in the pager,
    focus is the mode."""

    HELP = """
    Verb keys fire only while this table has focus (DL-187): s start,
    f force-start, k kill, i/I ice on/off, h/H hold on/off, n/N noexec
    on/off. k and f confirm first.

    d or Enter opens details, t opens triggers, / filters (F1 in the
    filter line shows the syntax), v cycles all/problems/active, space
    folds a box, z folds every box. A click on a box's marker folds it.

    The selection is a job, not a row. A fold that hides it selects the
    box shown in its place. A filter or view that hides it leaves nothing
    selected: up, down, Enter or a click on any cell selects a row. Enter
    on the filter line, or `v`, that leaves one job shown selects that
    job; typing in the filter line selects nothing.

    e moves focus to the explain pane, where up, down and Enter go to
    the job a line names, and Backspace goes back (DL-535).

    E opens the stored events before the oldest the console holds in
    the pager; E there reads the page before (DL-541). The alarms
    column, and the problems view, count a job's alarms dated within 24
    hours of the engine's present time. An alarm is dated by its
    deadline: after a stall or outage longer than that, an alarm
    raised late for an old deadline is only in the console, and it
    can keep a viewer that joins then from seeing a different, recent
    alarm. The title marks the alarms unavailable without the engine's
    present time, and incomplete when a read met seqs with no stored
    entries (an alarm may be there) or a scan reached its bound of
    2000 rows or 8 requests. On a root upgraded in place that mark can
    stay until an alarm of each kind older than two days is stored
    above the earlier build's seqs, or retention drops them; after
    that, this view keeps the mark until its next scan, and a view
    that joins anew does not show it.

    Drag a border to resize; + and - resize the focused pane.

    F1 in the console line (`:`) lists every verb and every status
    CHANGE_STATUS takes.
    """
    BINDINGS = [
        Binding("s", "app.send('STARTJOB')", "start"),
        # visible: FORCE is the rerun verb; plain STARTJOB is SEM-10-gated on box members
        # (DL-64)
        Binding("f", "app.send('FORCE_STARTJOB')", "force"),
        Binding("k", "app.send('KILLJOB')", "kill"),
        Binding("i", "app.send('ON_ICE')", "ice", show=False),
        Binding("I", "app.send('OFF_ICE')", "off-ice", show=False),
        Binding("h", "app.send('ON_HOLD')", "hold", show=False),
        Binding("H", "app.send('OFF_HOLD')", "off-hold", show=False),
        Binding("n", "app.send('ON_NOEXEC')", "noexec", show=False),
        Binding("N", "app.send('OFF_NOEXEC')", "off-noexec", show=False),
    ]
    #: with nothing selected the cursor rests on a row painted as any other row, so the
    #: highlight is never on a job the keys would not act on (DL-532)
    DEFAULT_CSS = """
    _JobsTable.-resting > .datatable--cursor, _JobsTable.-resting:focus > .datatable--cursor {
        background: transparent;
        color: $foreground;
        text-style: none;
    }
    """

    def action_cursor_up(self) -> None:
        if not self._pick_rest():
            super().action_cursor_up()

    def action_cursor_down(self) -> None:
        if not self._pick_rest():
            super().action_cursor_down()

    def _pick_rest(self) -> bool:
        """With nothing selected, up or down selects the row the cursor rests on (DL-532)."""
        app = self.app
        return isinstance(app, RunnerApp) and app.pick_rest()

    class FoldClicked(Message):
        """A click on a box's indent or marker (DL-512). It is posted after the cursor move's
        RowHighlighted, so the app selects the row before it folds the box."""

        def __init__(self, job: str) -> None:
            super().__init__()
            self.job = job

    def on_click(self, event: Click) -> None:
        """A first-button click on a box's indent or marker selects the row and folds the box;
        it opens no details. The hit area is the span `_decorated_cells` marks, so the drawing
        and the hit test cannot disagree (DL-512). Every other click goes on to
        `DataTable._on_click`, except a first-button click anywhere on the row the cursor rests
        on while nothing is selected: that click selects the row and does nothing else
        (DL-532)."""
        meta = event.style.meta
        job = meta.get(_FOLD_META)
        if job is None and event.button == 1 and meta.get("row") == self.cursor_row:
            if self._pick_rest():
                event.prevent_default()
                event.stop()
            return
        if job is None or event.button != 1:
            return
        event.prevent_default()
        event.stop()
        self.move_cursor(row=meta["row"])
        self.post_message(self.FoldClicked(job))


class _FilterInput(Input):
    """The `/` filter line (DL-511). Escape clears and hides it; Enter (Input.Submitted, handled
    by the app) keeps a good line and returns to the table, and stays on a refused one."""

    HELP = """
    Words separated by spaces. A job shows when every word matches it.
    Letter case is ignored.

    - `core`: the name contains core; `*`, `?` and `[...]` are wildcards
    - `=core_*`: the whole name matches the pattern
    - `@a*,b*`: the whole name matches one pattern of the list
    - `~regex`: the regular expression is found in the name; write a space as `\\s`
    - `status=su`, `status@su,fa`: the status, by its name or a start that fits exactly one
    - `box=b*`, `box@a*,b*`: the name of the job's own box matches
    - `!word`, `status!=su`, `box!@a*,b*`: the word does not match
    - `\\text`: the name contains the text as written

    Example: `!status=su =core_*`. Enter keeps a good line, Esc clears.
    """
    BINDINGS = [
        Binding("escape", "clear_filter", "clear"),
        Binding("tab", "stay", "", show=False),
    ]

    def action_clear_filter(self) -> None:
        app = self.app
        assert isinstance(app, RunnerApp)
        app.clear_filter()

    def action_stay(self) -> None:
        """Tab has no completion here and does not leave the line (DL-518, as DL-516 for the
        console line)."""
        self.app.bell()


class _ConsoleInput(Input):
    """The `:` console line. Escape clears the half-typed command and returns focus to the jobs
    table, as _FilterInput does, so an abandoned line cannot wait for a stray Enter (DL-187)."""

    HELP = f"""
    {_CONSOLE_OPENING}

    F1 on this line shows every verb and status.
    """
    BINDINGS = [
        Binding("escape", "cancel_command", "cancel"),
        Binding("tab", "take_suggestion", "take suggestion", show=False),
        Binding("f1", "app.console_words", "verbs"),
    ]

    def action_cancel_command(self) -> None:
        self.value = ""
        self.app.query_one("#jobs", DataTable).focus()

    def action_take_suggestion(self) -> None:
        """Tab, with the cursor at the end, takes the grey suggestion when one shows, so the line
        becomes what the person sees. The library clears the suggestion on every change of the
        line, so a showing one belongs to the present text. When none shows (it is empty after
        a focus event and until a worker answers a keystroke), Tab sets the line to the
        completion rule's answer for its text and the present job names, computed now
        (DL-519). With no completion, or the cursor inside the line, Tab rings the bell and
        changes nothing. It never leaves the line; Escape is the way out (DL-516)."""
        app = self.app
        assert isinstance(app, RunnerApp)
        completed = (
            (self._suggestion or complete_line(self.value, app._jobs_snapshot))
            if self.cursor_at_end
            else None
        )
        if completed is None:
            app.bell()
            return
        self.value = completed
        self.cursor_position = len(completed)


class _ConsoleSuggester(Suggester):
    """The console line's completion (DL-516) over the job names of the present status answer,
    read at each keystroke. The cache is off, since the job names change, and matching keeps
    case, since job names match exactly."""

    def __init__(self, jobs: Callable[[], Iterable[str]]) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._jobs = jobs

    async def get_suggestion(self, value: str) -> str | None:
        return complete_line(value, self._jobs())


def compile_search(pattern: str) -> re.Pattern[str] | str:
    """Compile a pager search or filter pattern: regex, smartcase (insensitive unless the pattern
    has an uppercase letter). Returns an error string instead of raising, for the prompt."""
    flags = 0 if any(ch.isupper() for ch in pattern) else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        return f"bad pattern: {exc}"


#: a pager row to write: the line and the spans its search highlights
type _PagerRow = tuple[str, list[tuple[int, int]]]
#: the prompt's refusal of a pattern that a bounded pass stopped (DL-514)
_PATTERN_TOO_SLOW = "the pattern took too long to match"


def _pager_rows(
    lines: Iterable[str], line_filter: re.Pattern[str] | None, needle: re.Pattern[str] | None
) -> list[_PagerRow]:
    """The lines a pager view shows, each with its search spans. It only matches, so it runs
    in a bounded pass before the view changes (DL-514). A line with any match, even an empty
    one, is a search match."""
    rows: list[_PagerRow] = []
    for line in lines:
        if line_filter is None or line_filter.search(line):
            spans = [] if needle is None else [m.span() for m in needle.finditer(line)]
            rows.append((line, spans))
    return rows


class _LogSearchInput(Input):
    """The pager prompt line (`/`, `?`, `&`). Escape cancels the prompt and returns focus to the
    log without leaving the pager. RunnerApp disables textual's escape-to-minimize, which
    swallows escape before any binding runs while a widget is maximized, and binds escape
    itself."""

    BINDINGS = [Binding("escape", "cancel_prompt", "cancel")]

    def action_cancel_prompt(self) -> None:
        pane = self.parent
        assert isinstance(pane, _LogPane)
        pane.close_prompt()


class _LogTail(RichLog):
    """The log tail, and a less-style pager whenever it has focus.

    Focus is the mode: textual consults the focused widget's bindings before the app's, so the
    keymap provides the pager verbs and shadows the operator verbs (`k` scrolls up, never
    KILLJOB; `f` pages down, never FORCE_STARTJOB; `q` leaves the pager, not the app).
    Estate-mutating keys with no pager meaning ring the bell. `m`/`o`/`r` and the pane-resize
    keys fall through to the app (allowlist in tests). Paging never mutates the estate.

    Search is regex with smartcase, all matches reverse-video, `n`/`N` wrap. `&` shows only
    matching lines, a view of the buffer. Follow is pinned-at-bottom: scrolling up pauses it
    ([paused] in the title) and `F`/`G`/End resume. Buffer, display list and widget cap at
    _PAGER_BUFFER_LINES in lockstep.
    """

    HELP = """
    Focus makes this a pager (DL-67): the operator verbs are shadowed to
    the bell here. j/k or the arrows scroll, f/space page down, b page
    up, g/G jump to the top/bottom.

    / searches forward, ? backward, n/N repeat the last search, & shows
    only matching lines. q or escape leaves the pager.

    E shows the stored events before the console's oldest instead of
    the log, a page per press, until the retained floor (DL-541). Past
    the pager's 10 000 lines a page drops the newest lines.
    """
    BINDINGS = [
        # search (the pager's reason to exist)
        Binding("slash", "prompt('search')", "search"),
        Binding("question_mark", "prompt('rsearch')", "search back", show=False),
        Binding("ampersand", "prompt('filter')", "filter lines"),
        Binding("n", "match_step(1)", "next"),
        Binding("N", "match_step(-1)", "prev"),
        # motion (less/vim); shadows k=KILLJOB, f=FORCE, d=details, space=fold, z=fold-all
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
        # estate-mutating verbs with no pager meaning ring the bell; check_action=False would
        # let the key fall through to the app binding it shadows
        Binding("s", "app.bell", "", show=False),
        Binding("i", "app.bell", "", show=False),
        Binding("I", "app.bell", "", show=False),
        Binding("h", "app.bell", "", show=False),
        Binding("H", "app.bell", "", show=False),
        Binding("v", "app.bell", "", show=False),
        Binding("colon", "app.bell", "", show=False),
        Binding("e", "app.bell", "", show=False),
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
        #: what a stop on new lines cleared, shown in the title until the next pattern (DL-514)
        self._notice = ""
        #: the title while the pager shows stored entries instead of a log, and whether a page
        #: dropped its newest lines at the bound (DL-541)
        self._label: str | None = None
        self._dropped = False

    # ------------------------------------------------------------- data plane

    def show_entries(self, label: str) -> None:
        """Show stored entries, read backward, instead of the log, under `label` (DL-541)."""
        self.set_source("events", None)
        self._label = label
        self._refresh_title()

    def drop_entries(self) -> None:
        """The log takes the pager back; its next `set_source` shows it."""
        self._label, self._dropped = None, False

    def prepend(self, lines: list[str]) -> None:
        """Earlier lines go above the buffer; past its bound the newest lines go, since the
        pager reads backward, and the title says so (DL-541). The view stays on the line it
        showed. The filter and the search in force run over the new lines and the lines kept
        in one bounded pass (DL-514)."""
        lines = lines[-_PAGER_BUFFER_LINES:]
        keep = max(0, min(len(self._buffer), _PAGER_BUFFER_LINES - len(lines)))
        if keep < len(self._buffer):
            self._dropped = True
        kept = list(self._buffer)[:keep]
        top = int(self.scroll_y)
        line_filter, needle = self._line_filter, self._needle
        rows = run_bounded(
            lambda: (
                _pager_rows(lines, line_filter, needle),
                _pager_rows(kept, line_filter, needle),
            ),
            _PAGER_LIMIT_S,
        )
        self._buffer.clear()
        self._buffer.extend([*lines, *kept])
        if isinstance(rows, Stopped):
            self._drop_patterns(False)
            return
        added, rest = rows
        self._redraw([*added, *rest])
        self.scroll_to(y=top + len(added), animate=False)
        self._refresh_title()

    def set_source(self, stream: str, path: str | None) -> None:
        """New tail target (job switch, o toggle, truncation): drop the buffer, keep the
        search and filter patterns, as less does across files. A note of patterns cleared on
        the old log goes with it (DL-514)."""
        self._stream, self._path = stream, path
        self._buffer.clear()
        self._display = []
        self._matches = []
        self._match_pos = None
        self._notice = ""
        self.clear()
        self._refresh_title()

    def feed(self, lines: list[str]) -> None:
        """New lines join the buffer first, so a stop loses none of them. Which of them show, and
        their search spans, come from one bounded pass before anything is written (DL-514)."""
        was_at_end = self.is_vertical_scroll_end
        self._buffer.extend(lines)
        rows = run_bounded(
            lambda: _pager_rows(lines, self._line_filter, self._needle), _PAGER_LIMIT_S
        )
        if isinstance(rows, Stopped):
            self._drop_patterns(was_at_end)
            return
        self._write_rows(rows)
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

    def _drop_patterns(self, was_at_end: bool) -> None:
        """New lines stopped the pass of the filter or search in force: both are cleared once,
        since a pass cannot tell which one ran long, and the view is rebuilt from the buffer
        without them. A view at its end follows the tail and a paused one keeps its place. The
        title says so until the next pattern is applied or the log changes (DL-514)."""
        top = int(self.scroll_y)  # clear() inside _redraw resets the scroll
        self._line_filter, self._line_filter_text = None, ""
        self._needle, self._needle_text = None, ""
        self._redraw(_pager_rows(self._buffer, None, None))
        self._notice = "a pattern took too long to match; search and filter cleared"
        if was_at_end:
            self.scroll_end(animate=False)
        else:
            self.scroll_to(y=min(top, self.max_scroll_y), animate=False)
        self._refresh_title()

    def _write_rows(self, rows: list[_PagerRow]) -> None:
        for line, spans in rows:
            self._display.append(line)
            text = Text(line)
            for start, end in spans:
                text.stylize("reverse", start, end)
            if spans:
                self._matches.append(len(self._display) - 1)
            self.write(text, scroll_end=False)

    def _redraw(self, rows: list[_PagerRow]) -> None:
        """Re-render the view from rows computed beforehand."""
        self.clear()
        self._display = []
        self._matches = []
        self._match_pos = None
        self._write_rows(rows)

    # ------------------------------------------------------------ search model

    def apply_search(self, pattern: str, backward: bool) -> str | None:
        """Set the needle ('' clears it); returns an error string for the prompt, None on
        success. It jumps to the first match after (for `?`, before) the top row, wrapping."""
        needle: re.Pattern[str] | None = None
        if pattern:
            compiled = compile_search(pattern)
            if isinstance(compiled, str):
                return compiled
            needle = compiled
        # the search matches only the lines the filter in force shows, so it never runs that
        # filter again, and clearing it runs no pattern (DL-514)
        shown = self._display
        rows = (
            _pager_rows(shown, None, None)
            if needle is None
            else run_bounded(lambda: _pager_rows(shown, None, needle), _PAGER_LIMIT_S)
        )
        if isinstance(rows, Stopped):
            return _PATTERN_TOO_SLOW
        top = int(self.scroll_y)  # clear() inside _redraw resets the scroll
        self._redraw(rows)
        self._notice = ""
        self._needle, self._needle_text, self._search_backward = needle, pattern, backward
        if self._matches:
            if backward:
                below = [i for i, row in enumerate(self._matches) if row < top]
                self._match_pos = below[-1] if below else len(self._matches) - 1
            else:
                above = [i for i, row in enumerate(self._matches) if row > top]
                self._match_pos = above[0] if above else 0
            self._jump()
        else:
            # a cleared search, or one that matches nothing, keeps the view in place
            self.scroll_to(y=min(top, self.max_scroll_y), animate=False)
        self._refresh_title()
        return None

    def apply_line_filter(self, pattern: str) -> str | None:
        line_filter: re.Pattern[str] | None = None
        if pattern:
            compiled = compile_search(pattern)
            if isinstance(compiled, str):
                return compiled
            line_filter = compiled
        # the filter runs over the buffer, and the search over the lines that pass it (DL-514)
        needle = self._needle
        rows = run_bounded(lambda: _pager_rows(self._buffer, line_filter, needle), _PAGER_LIMIT_S)
        if isinstance(rows, Stopped):
            return _PATTERN_TOO_SLOW
        self._redraw(rows)
        self._notice = ""
        self._line_filter, self._line_filter_text = line_filter, pattern
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

    # the inherited motion actions animate and move relative to scroll_target_y, which goes
    # stale when the viewport resizes; a pager snaps from the real offset instead

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
        # arrows and wheel bypass our actions, so a tick keeps the [paused] title current
        self.set_interval(0.5, self._refresh_title)

    def _refresh_title(self) -> None:
        title = Text(self._label or f"log ({self._stream}): {self._path or 'none yet'}")
        if self._label and self._dropped:
            title.append(
                f"  newest lines dropped: the pager holds {_PAGER_BUFFER_LINES}",
                style="bold yellow",
            )
        if self._line_filter_text:
            title.append(f"  &{self._line_filter_text}", style="italic")
        if self._needle_text:
            at = "-" if self._match_pos is None else str(self._match_pos + 1)
            title.append(f"  /{self._needle_text}/ {at}/{len(self._matches)}", style="italic")
        if self._notice:
            title.append(f"  {self._notice}", style="bold yellow")
        if not self.is_vertical_scroll_end:
            title.append("  [paused]", style="bold yellow")
        if title.plain != self._title_cache:
            self._title_cache = title.plain
            self.border_title = title


class _Console(RichLog):
    """The event console, bounded at `_CONSOLE_LINES` rendered lines (DL-541). A line written
    for a stored entry keeps the entry's key, so the oldest key still on screen is known as
    lines are evicted, and the pager reads the entries before it.

    DL-541: it reads two private fields of the pinned textual RichLog. `_start_line` counts the
    rendered lines the bound has evicted since the log began, and `_size_known` is false while
    writes wait, deferred, for the first size; RichLog replays those writes in order through
    `write` once it is known. `test_dl541_richlog_keeps_the_private_fields_the_console_reads`
    fails if the installed textual stops providing either with that meaning."""

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- textual's own name
        super().__init__(id=id, markup=False, wrap=True, max_lines=_CONSOLE_LINES)
        #: (index of the line's first rendered line since the console began, entry key)
        self._marks: deque[tuple[int, _Key]] = deque()
        #: the keys of writes that wait for the first size, in the order RichLog replays them
        self._early: deque[_Key | None] = deque()
        self._key: _Key | None = None
        #: the highest key whose line the bound evicted
        self._evicted: _Key | None = None

    def write_entry(self, text: Text, key: _Key | None) -> None:
        self._key = key
        try:
            self.write(text)
        finally:
            self._key = None

    def write(self, content: Any, *args: Any, **kwargs: Any) -> Any:
        key, self._key = self._key, None
        if not self._size_known:
            self._early.append(key)
            return super().write(content, *args, **kwargs)
        if self._early:  # RichLog replays the waiting writes before any other write
            key = self._early.popleft()
        first = self._start_line + len(self.lines)
        result = super().write(content, *args, **kwargs)
        if key is not None:
            self._marks.append((first, key))
        while self._marks and self._marks[0][0] < self._start_line:
            evicted = self._marks.popleft()[1]
            self._evicted = evicted if self._evicted is None else max(self._evicted, evicted)
        return result

    def clear(self) -> Any:
        self._marks.clear()
        self._early.clear()
        self._evicted = None
        return super().clear()

    def oldest_key(self) -> _Key | None:
        """The key of the oldest entry whose line the console still holds whole."""
        if self._marks:
            return self._marks[0][1]
        keys = [key for key in self._early if key is not None]
        return keys[0] if keys else None

    def earlier_from(self) -> _Key | None:
        """Where reading the entries before the console starts (DL-541): its oldest keyed
        line, or, when the bound evicted every keyed line, just above the highest key it
        evicted."""
        oldest = self.oldest_key()
        if oldest is not None:
            return oldest
        return None if self._evicted is None else _just_above(self._evicted)

    def forget_keys(self) -> None:
        """The lines stay and stand for no key: the next history read keys its own."""
        self._marks.clear()
        self._early = deque(None for _ in self._early)
        self._evicted = None


def _focus_now(widget: Widget) -> None:
    """Move focus to the widget a key just opened, before the action returns. A key queued
    behind the opening key must not reach the widget that had focus: `Widget.focus` defers the
    move with `call_later`, and the table's verb keys fire in that gap."""
    widget.refresh()
    widget.screen.set_focus(widget)


class _LogPane(Vertical):
    """The log column cell: the pager plus its prompt line. Maximizing this, not the bare
    RichLog, keeps the prompt visible, because maximize hides every widget outside the
    maximized subtree."""

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
        _focus_now(prompt)

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


#: what the console says about each ss3 outcome:
#: applied  -- the index, so the operator can find it in the journal.
#: refused  -- "not sent": no index, no log entry; sending again is safe. The console mints a
#:             fresh id per command, so a refusal is never a retry's; a collision, which only a
#:             reused id meets, says what the id decided earlier (DL-217).
#: rejected -- the table they typed against had moved. It is in the log, and the refresh below
#:             the line is the re-read.
#: unknown  -- yellow, not red: the engine may be applying it now, so look, never resend.
_OUTCOME_STYLE = {APPLIED: "green", REFUSED: "red", REJECTED: "red"}

# DL-393: pin the runtime before activation moves its parent link. Resolving
# the Python symlink itself would discard the virtual environment.
_RECOVERY_PYTHON = Path(sys.executable).parent.resolve() / Path(sys.executable).name


def _outcome_line(label: str, response: Mapping[str, Any]) -> Text:
    """One console line for one command answer, by ss3 outcome (DL-92): red means "send
    again" only for a refusal, so refusal, rejection and unknown render differently."""
    outcome = outcome_of(response)
    error = str(response.get("error", "")).strip()
    if outcome == APPLIED:
        body = f"applied @ #{response.get('index', '?')}"
    elif outcome == REFUSED and (original := original_decision_text(response)) is not None:
        # a collision: this request was refused but its id is logged under an earlier decision
        # (DL-217)
        body = f"refused: {error or 'request_id collision'}; {original}"
    elif outcome == REFUSED:
        body = f"not sent, nothing logged: {error or 'refused'}"
    elif outcome == REJECTED:
        body = f"rejected @ #{response.get('index', '?')}: {error or 'the target moved'}"
    else:
        body = f"NO DECISION -- may still apply, do not resend: {error or 'no answer'}"
    return Text(f"{label}: {body}", style=_OUTCOME_STYLE.get(outcome, "yellow"))


def _transport_line(label: str, exc: ControlClientError) -> Text:
    """One console line for a command that got no answer. A request that never left the app is
    the refusal case: nothing logged, send again. One that left and got nothing back is the
    unknown case: "not sent" would make the operator resend an admitted command."""
    if not exc.delivered:
        return Text(f"{label}: not sent ({exc})", style="red")
    return Text(
        f"{label}: NO DECISION -- may still apply, do not resend: {exc}",
        style="yellow",
    )


def recovery_command(socket_path: Path, request: Mapping[str, Any]) -> str:
    """The complete, shell-quoted recovery command for a TUI mutation (DL-391)."""
    payload = request["payload"]
    args = [
        str(_RECOVERY_PYTHON),
        "-P",
        "-m",
        "dsl41",
        "sendevent",
        str(request["verb"]),
        "--socket",
        str(socket_path.absolute()),
    ]
    if "job" in payload:
        args.extend(("--job", str(payload["job"])))
    if "name" in payload:
        args.extend(("--global", f"{payload['name']}={payload['value']}"))
    if "status" in payload:
        args.extend(("--status", str(payload["status"])))
    if "exit_code" in payload:
        args.extend(("--exit-code", str(payload["exit_code"])))
    (revision,) = request["expect"].values()
    args.extend(
        (
            "--request-id",
            str(request["request_id"]),
            "--expect",
            str(revision),
            "--baseline",
            str(request["baseline_id"]),
            "--claimed-actor",
            str(request["claimed_actor"]),
        )
    )
    return shlex.join(args)


def parse_console_command(text: str, selected: str | None) -> dict[str, Any] | str:
    """Parse an event-console line into the verb half of a sendevent request, or return an
    error string. Grammar (ss11): `<JOB_VERB> [job]`, `SET_GLOBAL NAME=value`,
    `CHANGE_STATUS [job] STATUS [exit_code]`; an omitted job targets the selected job.

    The concurrency-model ss6 envelope is not built here (DL-90): `expect` is the revision of the
    app's last refresh, which a pure parser cannot know."""
    tokens = text.split()
    if not tokens:
        return "empty command"
    verb = tokens[0].upper()
    args = tokens[1:]

    def _job(explicit: str | None) -> dict[str, Any] | str:
        job = explicit if explicit is not None else selected
        if job is None:
            return f"{verb}: no job selected"
        return {"cmd": "sendevent", "verb": verb, "payload": {"job": job}}

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
        return {"cmd": "sendevent", "verb": verb, "payload": {"name": name, "value": value}}
    if verb == "CHANGE_STATUS":
        # the status-first shorthand applies only when the second token is not a status:
        # "CHANGE_STATUS SUCCESS FAILURE" names a job SUCCESS (DL-187 item 8)
        shorthand = (
            bool(args)
            and args[0].upper() in STATUSES
            and (len(args) == 1 or args[1].upper() not in STATUSES)
        )
        if shorthand:
            job, status, rest = selected, args[0].upper(), args[1:]
            if job is None:
                return "CHANGE_STATUS: no job selected"
        elif len(args) >= 2:
            job, status, rest = args[0], args[1].upper(), args[2:]
        else:
            return "CHANGE_STATUS expects [job] STATUS [exit_code]"
        payload: dict[str, Any] = {"job": job, "status": status}
        if rest:
            if len(rest) > 1:
                return "CHANGE_STATUS expects at most one exit_code"
            try:
                payload["exit_code"] = int(rest[0])
            except ValueError:
                return f"exit_code must be an integer, got {rest[0]!r}"
        return {"cmd": "sendevent", "verb": verb, "payload": payload}
    return f"unknown verb {verb!r} -- expected one of: {', '.join(_CONSOLE_VERBS)}"


def echo_label(request: dict[str, Any]) -> str:
    """The console's echo of a sendevent request before it is sent (DL-187 item 9): the whole
    request, including a global's value, a status and an exit code."""
    verb = str(request.get("verb"))
    payload = request.get("payload") or {}
    if verb == "SET_GLOBAL":
        return f"> SET_GLOBAL {payload.get('name')}={payload.get('value')}"
    if verb == "CHANGE_STATUS":
        tail = payload.get("exit_code")
        rest = f" {tail}" if tail is not None else ""
        return f"> CHANGE_STATUS {payload.get('job')} {payload.get('status')}{rest}"
    target = payload.get("job") or payload.get("name") or ""
    return f"> {verb} {target}".rstrip()


@dataclass(frozen=True)
class PaneLine:
    """One line of the explain pane (DL-535). `target` is the catalog job the line names, the
    job a hop goes to; None for a line that is no link. `key` names the line by what it names,
    never by its position, so the cursor stays on the same line when an answer reorders the
    lines. A link is bound to the job and the answer the pane shows it for by the pane's own
    `source` and `gen`: the pane shows one answer's lines whole."""

    text: Text
    target: str | None = None
    key: str = ""


def _unique_keys(lines: list[PaneLine]) -> list[PaneLine]:
    """Each line's key, with a count for its second and later occurrence (DL-535)."""
    seen: dict[str, int] = {}
    out = []
    for line in lines:
        count = seen.get(line.key, 0)
        seen[line.key] = count + 1
        out.append(replace(line, key=f"{line.key}#{count}") if count else line)
    return out


def _pane_line(
    text: str | Text, *, target: str | None = None, key: str = "", style: str = ""
) -> PaneLine:
    """A line with its gutter: `›` for a link, blank otherwise (DL-535)."""
    body = text if isinstance(text, Text) else Text(text, style=style)
    gutter = Text("› ", style="bold") if target is not None else Text("  ")
    return PaneLine(gutter + body, target, key or body.plain)


#: the disposition in words, by attempt and disposition (DL-534, DL-535)
_DISPOSITION_WORDS: dict[tuple[str | None, str], str] = {
    ("start", "admit"): "a start now: would start",
    ("start", "queue"): "a start now: would queue for capacity",
    ("start", "bypass"): "a start now: would complete without running",
    ("start", "defer"): "a start now: would wait for its run_window to open",
    ("start", "skip"): "a start now: would be skipped",
    ("start", "ignored"): "a start now: would not start",
    ("readmit", "fits"): "a queue scan now: fits alone; the waiters ahead go first",
    ("readmit", "short"): "a queue scan now: does not fit",
    ("readmit", "held"): "a queue scan now: stays queued on hold",
    ("readmit", "cancel"): "a queue scan now: cancels it, its box is not running",
    ("readmit", "leave"): "a queue scan now: takes it out of the queue, its recheck fails",
    (None, "running"): "running",
}


def _at_suffix(at: Any) -> str:
    return f" at {format_local_at(at)}" if isinstance(at, str) else ""


def _disposition_lines(reply: Mapping[str, Any]) -> list[PaneLine]:
    """The disposition in words, then the deciding gate, the open gates and the context
    facts, each marked as such (DL-535). A running file watch says what it watches."""
    attempt, disposition = reply.get("attempt"), str(reply.get("disposition"))
    words = _DISPOSITION_WORDS.get((attempt, disposition), f"{attempt}: {disposition}")
    lines = [_pane_line(words, style="bold")]
    gates = [gate for gate in reply.get("gates") or [] if isinstance(gate, Mapping)]
    for state in ("deciding", "open", "context"):
        for gate in gates:
            if gate.get("state") != state:
                continue
            text = f"  {state}: {gate.get('text', '')}{_at_suffix(gate.get('at'))}"
            job = gate.get("job")
            lines.append(
                _pane_line(
                    text,
                    target=job if isinstance(job, str) else None,
                    key=f"gate:{gate.get('kind')}",
                    style="bold" if state == "deciding" else ("dim" if state == "open" else ""),
                )
            )
    watching = reply.get("watching")
    if isinstance(watching, Mapping):
        text = f"  watching {watching.get('file')} every {watching.get('interval')}s"
        if watching.get("min_size") is not None:
            text += f", min_size {watching['min_size']}"
        lines.append(_pane_line(text))
    return lines


def atom_link(atom: Mapping[str, Any]) -> str | None:
    """The catalog job an atom's line links to (DL-535): the job it names, unless it is a
    guard, a self reference, an undefined or an external name, or a global."""
    job = atom.get("job")
    if not isinstance(job, str) or any(
        atom.get(flag) for flag in ("guard", "self", "undefined", "external")
    ):
        return None
    return job


def _atom_labels(atom: Mapping[str, Any]) -> str:
    """What an atom that is no dependency link is, in words."""
    return ", ".join(
        word
        for flag, word in (
            ("guard", "guard"),
            ("self", "self"),
            ("undefined", "undefined"),
            ("external", "external"),
        )
        if atom.get(flag)
    )


def _condition_lines(reply: Mapping[str, Any]) -> list[PaneLine]:
    condition = reply.get("condition")
    if condition is None:
        return [_pane_line("no condition", style="dim")]
    truth = "true" if reply.get("satisfied") else "false"
    lines = [_pane_line(f"condition: {condition} ({truth})", style="bold")]
    for atom in reply.get("atoms") or []:
        if not isinstance(atom, Mapping):
            continue
        true = bool(atom.get("true"))
        text = Text("  ")
        text.append("✔ " if true else "✘ ", style="green" if true else "red")
        text.append(str(atom.get("atom", "")), style="" if true else "bold")
        if "actual" in atom:  # a global atom carries the effective value (DL-66)
            actual = atom["actual"]
            text.append(f"   = {actual!r}" if actual is not None else "   (unset)", style="dim")
        if labels := _atom_labels(atom):
            text.append(f"  [{labels}]", style="dim")
        key = f"atom:{atom.get('kind')}:{atom.get('job')}:{atom.get('atom')}"
        lines.append(_pane_line(text, target=atom_link(atom), key=key))
    return lines


def _capacity_lines(capacity: Any) -> list[PaneLine]:
    """The short buckets, each with its holders or "nobody holds it", the waiters that block,
    the waiters ahead and the scan's sentence, one line each (DL-535)."""
    if not isinstance(capacity, Mapping):
        return []
    short = [entry for entry in capacity.get("short") or [] if isinstance(entry, Mapping)]
    blockers = [entry for entry in capacity.get("blockers") or [] if isinstance(entry, Mapping)]
    ahead = [entry for entry in capacity.get("ahead") or [] if isinstance(entry, Mapping)]
    lines: list[PaneLine] = []
    if capacity.get("admissible") and not short and not blockers:
        lines.append(_pane_line("capacity: room for it now"))
    for entry in short:
        bucket = entry.get("bucket")
        text = (
            f"capacity: {bucket} short by {entry.get('short')}: needs {entry.get('demand')},"
            f" {entry.get('used')} of {entry.get('capacity')} in use"
        )
        if entry.get("spent"):
            text += f", {entry['spent']} spent"
        lines.append(_pane_line(text, key=f"short:{bucket}"))
        holders = [h for h in entry.get("holders") or [] if isinstance(h, Mapping)]
        for holder in holders:
            units = holder.get("units")
            job = holder.get("job")
            lines.append(
                _pane_line(
                    f"  held by {job}: {units} unit{'' if units == 1 else 's'}",
                    target=job if isinstance(job, str) else None,
                    key=f"holder:{bucket}:{job}",
                )
            )
        if not holders:
            lines.append(_pane_line("  nobody holds it", key=f"nobody:{bucket}"))
    for entry in blockers:
        job = entry.get("job")
        lines.append(
            _pane_line(
                f"capacity: blocked by waiter {job}, higher priority ({entry.get('kind')})",
                target=job if isinstance(job, str) else None,
                key=f"blocker:{job}",
            )
        )
    for entry in ahead:
        job = entry.get("job")
        buckets = ", ".join(str(b) for b in entry.get("buckets") or [])
        lines.append(
            _pane_line(
                f"capacity: ahead in the queue: {job} ({buckets})",
                target=job if isinstance(job, str) else None,
                key=f"ahead:{job}",
            )
        )
    if capacity.get("text"):
        lines.append(_pane_line(f"capacity: {capacity['text']}", style="dim"))
    return lines


def _dispatch_lines(dispatch: Any) -> list[PaneLine]:
    if not isinstance(dispatch, Mapping):
        return []
    wait = dispatch.get("wait")
    if wait == "drain":
        what = "the engine's drain"
    elif wait == "cleanup":
        what = f"attempt {dispatch.get('attempt')} to settle"
    elif wait == "update":
        what = f"update {str(dispatch.get('token') or '')[:12]}"
        if dispatch.get("removes"):
            what += ", which removes the job"
    else:
        what = str(wait)
    kind, run = dispatch.get("kind"), dispatch.get("run_number")
    text = f"dispatch: the {kind} of run {run} waits for {what}"
    return [_pane_line(text, style="yellow")]


def _wake_lines(wakes: Any) -> list[PaneLine]:
    lines: list[PaneLine] = []
    for entry in wakes if isinstance(wakes, list) else []:
        if not isinstance(entry, Mapping):
            continue
        kind, at, job = entry.get("kind"), entry.get("at"), entry.get("job")
        text = "no scheduled tick" if kind == "tick" and at is None else str(entry.get("text"))
        lines.append(
            _pane_line(
                f"wake: {text}{_at_suffix(at)}",
                target=job if isinstance(job, str) else None,
                key=f"wake:{kind}:{job}",
            )
        )
    return lines


def _legacy_lines(reply: Mapping[str, Any], row: Mapping[str, Any]) -> list[PaneLine]:
    """The pane for an engine whose `explain` has no `gates` (DL-535): the condition and its
    atoms as before, no links. With no condition, the status row's hold or ice says why it
    does not start."""
    condition = reply.get("condition")
    if condition is None:
        if row.get("on_hold"):
            why = "on hold"
        elif row.get("on_ice"):
            why = "on ice"
        else:
            why = "starts on demand/schedule"
        return [PaneLine(Text(f"no condition -- {why}", style="dim"))]
    satisfied = bool(reply.get("satisfied"))
    lines = [
        PaneLine(Text("waiting on:" if not satisfied else "satisfied:", style="bold")),
        PaneLine(Text(f"  {condition}")),
        PaneLine(Text("")),
    ]
    for atom in reply.get("atoms") or []:
        true = bool(atom.get("true"))
        text = Text()
        text.append("  ✔ " if true else "  ✘ ", style="green" if true else "red")
        text.append(f"{atom.get('atom', '')}", style="" if true else "bold")
        if "actual" in atom:  # global atoms carry the effective value (DL-66)
            actual = atom["actual"]
            text.append(f"   = {actual!r}" if actual is not None else "   (unset)", style="dim")
        lines.append(PaneLine(text))
    return lines


def explain_lines(reply: Mapping[str, Any], row: Mapping[str, Any]) -> list[PaneLine]:
    """The explain pane's lines for one `explain` answer (DL-535; DL-534 for the fields), one
    line per item, in this order: the instant the answer is for; the recovery sentence; the
    disposition with the deciding gate first, then the open gates and the context facts; the
    condition and its atoms; capacity; the dispatch wait; what would wake the job. A line that
    names a catalog job links to it; a guard, a self reference, an undefined or an external
    name does not. An answer without `gates` is an older engine's: `_legacy_lines`. `row` is
    the job's status row."""
    if "gates" not in reply:
        return _legacy_lines(reply, row)
    lines = [_pane_line(f"as of {format_local_at(reply.get('as_of'))}", style="dim")]
    if reply.get("recovering"):
        lines.append(_pane_line(f"recovering: {reply['recovering']}", style="yellow"))
    lines += _disposition_lines(reply)
    lines += _condition_lines(reply)
    lines += _capacity_lines(reply.get("capacity"))
    lines += _dispatch_lines(reply.get("dispatch"))
    lines += _wake_lines(reply.get("wake"))
    return _unique_keys(lines)


def downstream_lines(
    source: str,
    deps: Mapping[str, Any],
    atoms_of: Mapping[str, Any],
) -> list[PaneLine]:
    """The pane's second mode (DL-535): the jobs one hop downstream of `source`, from `deps`,
    each with the atom of its condition that names `source`, read off its own `deps` answer
    in `atoms_of`. Each is a link but `source` itself, a self reference. A job missing from
    `atoms_of` is not detailed: its name only, and one line says how many. An engine whose
    `deps` has no `upstream_atoms` gets the names and no links."""
    downstream = [job for job in deps.get("downstream") or [] if isinstance(job, str)]
    if not downstream:
        return [_pane_line(f"no job's condition names {source}", style="dim")]
    linkable = isinstance(deps.get("upstream_atoms"), list)
    lines = [_pane_line(f"jobs whose condition names {source}:", style="bold")]
    undetailed = 0
    for job in downstream:
        if job == source:
            lines.append(_pane_line(f"  {job}: its own condition names it [self]"))
            continue
        if linkable and job not in atoms_of:
            undetailed += 1
            lines.append(_pane_line(f"  {job}", target=job, key=f"down:{job}"))
            continue
        atoms = atoms_of.get(job)
        named = [
            atom
            for atom in (atoms if isinstance(atoms, list) else [])
            if isinstance(atom, Mapping) and atom.get("job") == source and not atom.get("external")
        ]
        words = ", ".join(
            str(atom.get("atom")) + (" [guard]" if atom.get("guard") else "") for atom in named
        )
        if not linkable:
            words = ""
        elif not words:
            words = "no atom of its condition names it"
        lines.append(
            _pane_line(
                f"  {job}: {words}" if words else f"  {job}",
                target=job if linkable else None,
                key=f"down:{job}",
            )
        )
    if undetailed:
        lines.append(
            _pane_line(f"  {undetailed} not detailed: their atoms were not read", style="dim")
        )
    return _unique_keys(lines)


def dependency_names(atoms: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The details popup's dependency words from `deps`' `upstream_atoms` (DL-535): `needs`,
    the jobs and globals the condition depends on, an undefined or external name labelled;
    `guard`, each guard, a self one labelled; `self`, each other self reference. Neither a
    guard nor a self reference is a dependency."""
    out: dict[str, list[str]] = {"needs": [], "guard": [], "self": []}
    for atom in atoms:
        if atom.get("guard"):
            word = str(atom.get("atom")) + (" (self)" if atom.get("self") else "")
            section = "guard"
        elif atom.get("self"):
            word, section = str(atom.get("atom")), "self"
        elif atom.get("kind") == "global":
            name = str(atom.get("atom", "")).partition("(")[2].partition(")")[0]
            word, section = f"v({name})", "needs"
        else:
            labels = _atom_labels(atom)
            word = str(atom.get("job")) + (f" ({labels})" if labels else "")
            section = "needs"
        if word not in out[section]:
            out[section].append(word)
    return out


class _ExplainPane(VerticalScroll):
    """Why the selected job has not started, as lines, some of them links to another job
    (DL-535). It shows what the app gives it (`show`): the lines of one answer for one source
    job, whole, so every link it shows is bound to that job (`source`) and that answer's
    generation (`gen`). The cursor moves between the links and stays on the line with the same
    key across answers; it is drawn only while the pane has focus. An action never applies to
    a line the operator cannot see: the paging keys move the cursor with the view, and Enter on
    a cursor line scrolled off screen brings it back and does nothing else. Enter, Backspace
    and Escape are the app's: a hop is the operator's pick."""

    HELP = """
    Why the selected job has not started (DL-535): the instant the
    answer is for, what a start would do now and the gate that decides,
    the condition, capacity, the dispatch wait, and what would wake it.
    A line marked › names another job.

    e from the jobs table moves here. Up and down move between the
    marked lines, and PageUp, PageDown, Home and End move the view and
    the cursor with it. Enter selects the job the line names,
    unfolding, unfiltering or changing the view to show it; on a line
    scrolled out of view it brings the line back first. Backspace goes
    back; b switches to the jobs whose condition names this job and
    back; Escape returns to the jobs table. The verb keys act only from
    the jobs table.
    """
    BINDINGS = [
        Binding("up", "link(-1)", "previous link", show=False),
        Binding("down", "link(1)", "next link", show=False),
        Binding("pageup", "page(-1)", "page up", show=False),
        Binding("pagedown", "page(1)", "page down", show=False),
        Binding("home", "edge(-1)", "top", show=False),
        Binding("end", "edge(1)", "end", show=False),
        Binding("enter", "app.pane_hop", "go to job"),
        Binding("backspace", "app.pane_back", "back"),
        Binding("b", "toggle_mode", "causes/downstream"),
        Binding("escape", "app.pane_leave", "jobs table"),
    ]

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002 -- textual's own name
        super().__init__(id=id)
        #: the job the lines were computed for, and the generation of the request whose answer
        #: they are
        self.source: str | None = None
        self.gen = 0
        self.lines: list[PaneLine] = []
        #: index into `lines` of the line the cursor is on, a link, or None
        self.cursor: int | None = None
        #: "causes" or "downstream"
        self.mode = "causes"

    def compose(self) -> ComposeResult:
        yield Static(id="explain")

    def show(self, source: str | None, gen: int, lines: list[PaneLine]) -> None:
        """Show `lines`, the answer of generation `gen` for `source`. The cursor stays on the
        line with the same key, else it goes to the first link while the pane has focus, else
        nowhere."""
        kept = self.current() if source == self.source else None
        self.source, self.gen, self.lines = source, gen, list(lines)
        self.cursor = None
        if kept is not None:
            self.cursor = next(
                (i for i, line in enumerate(self.lines) if line.target and line.key == kept.key),
                None,
            )
        moved = self.cursor is None
        if moved and self.has_focus:
            self.cursor = self._next_link(-1, 1)
        self._draw(scroll=moved)

    def current(self) -> PaneLine | None:
        """The link the cursor is on."""
        if self.cursor is None or not 0 <= self.cursor < len(self.lines):
            return None
        return self.lines[self.cursor]

    def _next_link(self, start: int, step: int) -> int | None:
        index = start + step
        while 0 <= index < len(self.lines):
            if self.lines[index].target is not None:
                return index
            index += step
        return None

    def _spans(self) -> list[tuple[int, int]]:
        """Each line's first row and its rows, wrapped at the pane's width."""
        width = max(1, self.scrollable_content_region.width)
        spans, top = [], 0
        for line in self.lines:
            rows = max(1, len(line.text.wrap(self.app.console, width)))
            spans.append((top, rows))
            top += rows
        return spans

    def _on_screen(self, index: int, spans: list[tuple[int, int]]) -> bool:
        top = int(self.scroll_y)
        return top <= spans[index][0] < top + self.scrollable_content_region.height

    def cursor_on_screen(self) -> bool:
        """Whether the cursor's line is in view (DL-535)."""
        return self.cursor is not None and self._on_screen(self.cursor, self._spans())

    def reveal_cursor(self) -> None:
        """Scroll the cursor's line into view."""
        self._draw(scroll=True)

    def action_link(self, step: int) -> None:
        """Up and down move between the links; with none they scroll."""
        if not any(line.target is not None for line in self.lines):
            self.scroll_relative(y=step, animate=False, immediate=True)
            return
        start = self.cursor if self.cursor is not None else (-1 if step > 0 else len(self.lines))
        found = self._next_link(start, step)
        if found is not None:
            self.cursor = found
            self._draw(scroll=True)

    def action_page(self, sign: int) -> None:
        """PageUp and PageDown: a view up or down, the cursor with it (DL-535)."""
        y = self.scroll_y + sign * self.scrollable_content_region.height
        self.scroll_to(y=y, animate=False, immediate=True)
        self._cursor_with_view(sign)

    def action_edge(self, sign: int) -> None:
        """Home and End: the first or the last view, the cursor with it (DL-535)."""
        self.scroll_to(y=0 if sign < 0 else self.max_scroll_y, animate=False, immediate=True)
        self._cursor_with_view(sign)

    def _cursor_with_view(self, sign: int) -> None:
        """Keep the cursor on its line if that line is in view, else put it on the first link
        in view after a move down, the last after a move up, or nowhere."""
        spans = self._spans()
        if self.cursor is None or not self._on_screen(self.cursor, spans):
            shown = [
                i
                for i, line in enumerate(self.lines)
                if line.target is not None and self._on_screen(i, spans)
            ]
            self.cursor = (shown[0] if sign > 0 else shown[-1]) if shown else None
        self._draw()

    def action_toggle_mode(self) -> None:
        self.mode = "downstream" if self.mode == "causes" else "causes"
        app = self.app
        assert isinstance(app, RunnerApp)
        app.pane_mode_changed()

    def on_focus(self) -> None:
        if self.cursor is None:
            self.cursor = self._next_link(-1, 1)
        self.border_subtitle = Text("↑↓ link · enter go · backspace back · b mode · esc table")
        self._draw(scroll=True)

    def on_blur(self) -> None:
        self.border_subtitle = ""
        self._draw()

    def _draw(self, *, scroll: bool = False) -> None:
        """Render the lines, the cursor's line in reverse while the pane has focus; with
        `scroll`, scroll that line into view. A redraw for a new answer with the cursor on the
        same line leaves the view where the operator put it."""
        texts = []
        for index, line in enumerate(self.lines):
            text = line.text.copy()
            if index == self.cursor and self.has_focus:
                text.stylize("reverse")
            texts.append(text)
        try:
            static = self.query_one("#explain", Static)
        except NoMatches:
            return  # before compose
        static.update(Text("\n").join(texts))
        if scroll and self.cursor is not None and self.has_focus:
            top, rows = self._spans()[self.cursor]
            width = max(1, self.scrollable_content_region.width)
            self.scroll_to_region(Region(0, top, width, rows), animate=False, immediate=True)


def config_line(status: Mapping[str, Any]) -> str:
    """What the subtitle says of the configuration (spec-reload): `rev N`, and while an update
    stops its jobs, the internal plan token's first twelve hex digits and the jobs still stopping.
    Empty for an engine that names no rev."""
    rev = status.get("config_rev")
    if not isinstance(rev, int):
        return ""
    line = f"rev {rev}"
    update = status.get("update")
    if isinstance(update, Mapping):
        jobs = ", ".join(str(job) for job in update.get("stopping") or ()) or "none"
        line += f"  [update {str(update.get('token', ''))[:12]} stopping: {jobs}]"
    return line


@dataclass
class _Hop:
    """A hop to `target` until a status answer asked after it began still lists the target and
    the target's row is selected and on screen (DL-535). `after` is the number of status
    requests sent before it began; `confirmed` is set by the first answer to a later one;
    `frames` counts the refreshes it has then waited for the row."""

    target: str
    after: int
    confirmed: bool = False
    frames: int = 0


#: refreshes a hop waits for its row to come on screen before it gives up (DL-535)
_HOP_FRAMES = 10
#: downstream jobs whose atoms the downstream mode asks for, per selected job and rev (DL-535)
_DETAIL_CAP = 50


@dataclass(frozen=True)
class _Drag:
    """A drag of a split's border: the split, the pointer's column (table) or row (log,
    console) at the press, and the split's cells then (DL-520)."""

    split: Split
    start: int
    cells: int


#: the split `+` and `-` move for each focusable pane, and by how much for `+`: points of the
#: ratio, or rows for the console; the explain pane grows against the log (DL-520)
_PANE_STEPS: dict[str, tuple[Split, float]] = {
    "jobs": (Split.TABLE, 0.05),
    "explain-box": (Split.LOG, -0.05),
    "logtail": (Split.LOG, 0.05),
    "console": (Split.CONSOLE, 2),
}

#: the border lines that meet at each split, as a widget and the side of its border. The handle
#: is these lines and nothing else, and they are what takes the accent colour, so the cell a press
#: lands on and the line that lights up cannot disagree. The pager's prompt, when it shows, is
#: the log pane's lowest widget: its bottom line stands for the log's (DL-520).
_HANDLE_LINES: dict[Split, tuple[tuple[str, str], ...]] = {
    Split.TABLE: (
        ("jobs", "right"),
        ("explain-box", "left"),
        ("logtail", "left"),
        ("logsearch", "left"),
    ),
    Split.LOG: (("explain-box", "bottom"), ("logtail", "top")),
    Split.CONSOLE: (
        ("jobs", "bottom"),
        ("logtail", "bottom"),
        ("logsearch", "bottom"),
        ("console", "top"),
    ),
}


def _on_border(region: Region, side: str, x: int, y: int) -> bool:
    """Whether a screen cell lies on one side of a widget's border; a hidden widget's region is
    empty and has none."""
    across = region.x <= x < region.right
    down = region.y <= y < region.bottom
    if side == "left":
        return x == region.x and down
    if side == "right":
        return x == region.right - 1 and down
    return across and y == (region.y if side == "top" else region.bottom - 1)


class _PaneRow(Horizontal):
    """The row above the console. When its size changes, the splits are placed again, so a
    change of the terminal's size keeps the ratios and the console's rows (DL-520)."""

    def on_resize(self) -> None:
        app = self.app
        assert isinstance(app, RunnerApp)
        app._place_panes()


class _MainScreen(Screen[None]):
    """The app's own screen. It sees each mouse event before the library sends it to the widget
    under the pointer, so a press on a split's border starts a drag that selects no row, folds
    nothing, moves no focus and starts no text selection (DL-520). The library has no public
    hook ahead of that dispatch; `_forward_event` is where both its drivers and its pilot hand
    a mouse event to the screen."""

    def _forward_event(self, event: events.Event) -> None:
        app = self.app
        if (
            isinstance(event, events.MouseEvent)
            and not event.is_forwarded
            and isinstance(app, RunnerApp)
            and app._pane_mouse(event)
        ):
            return
        super()._forward_event(event)

    def watch_maximized(self) -> None:
        # nothing resizes while the log is maximized; on the way back the splits are placed on
        # the screen there is then
        app = self.app
        assert isinstance(app, RunnerApp)
        app._release_handle()
        app.call_after_refresh(app._place_panes)
        if self.maximized is None:
            app._release_pager()

    def on_screen_suspend(self) -> None:
        app = self.app
        assert isinstance(app, RunnerApp)
        app._release_handle()


class RunnerApp(App[None]):
    """The ss11 app: jobs table, explain pane, log tail, event console."""

    TITLE = "dsl41 runner"
    #: textual's default swallows escape before any binding runs while a widget is maximized, so
    #: escape would exit the pager instead of cancelling its prompt. Explicit bindings own it
    #: (_LogTail leave, _LogSearchInput cancel).
    ESCAPE_TO_MINIMIZE = False
    CSS = """
    #main { height: 1fr; }
    #tablecol { width: 3fr; }
    #filterline { display: none; }
    #filtermsg { display: none; height: auto; padding: 0 1; }
    #jobs { height: 1fr; border: round $primary; }
    #side { width: 2fr; }
    #explain-box { height: 2fr; border: round $primary; }
    #logbox { height: 3fr; }
    #logtail { height: 1fr; border: round $primary; }
    #logsearch { display: none; }
    #consolebox { height: 11; }
    #console { height: 1fr; border: round $secondary; }
    /* a split's handle in the accent colour: one side of each border, its label as it was */
    #jobs.-grab-right { border-right: round $accent; }
    #explain-box.-grab-left, #logtail.-grab-left { border-left: round $accent; }
    #jobs.-grab-bottom, #explain-box.-grab-bottom, #logtail.-grab-bottom {
        border-bottom: round $accent;
        border-subtitle-color: $primary;
    }
    #logtail.-grab-top { border-top: round $accent; border-title-color: $primary; }
    #logsearch.-grab-left { border-left: tall $accent; }
    #logsearch.-grab-bottom { border-bottom: tall $accent; }
    #console.-grab-top { border-top: round $accent; border-title-color: $secondary; }
    """

    #: No `send(...)` action belongs here: the operator verbs live on _JobsTable, where focus
    #: gates them (DL-187).
    BINDINGS = [
        # the label is posture-dependent and set per instance in __init__
        Binding("q", "quit", "quit"),
        Binding("d", "details", "details"),
        Binding("t", "triggers", "triggers"),
        Binding("m", "maximize_log", "zoom log"),
        Binding("o", "toggle_stream", "out/err"),
        Binding("r", "refresh", "refresh"),
        Binding("f1", "toggle_help_panel", "help"),
        # the pager's `?` binding shadows this one while the log has focus (DL-187 item 7)
        Binding("question_mark", "toggle_help_panel", "help", show=False),
        # navigation at estate scale (DL-65)
        Binding("slash", "focus_filter", "filter"),
        Binding("v", "cycle_view", "view"),
        Binding("space", "toggle_box", "fold", show=False),
        Binding("z", "toggle_all", "fold-all", show=False),
        Binding("colon", "focus_console", "console"),
        # the explain pane takes focus, where up, down and Enter go to a cause (DL-535)
        Binding("e", "focus_explain", "explain", show=False),
        # the stored events before the console's oldest, in the pager (DL-541)
        Binding("E", "earlier", "earlier events", show=False),
        # pane sizing (DL-520): ]/[ move the log against explain and }/{ the jobs table against
        # the side column by a fifth; +/= and - resize the focused pane by a small step
        Binding("]", "resize('log', 1)", "log taller", show=False),
        Binding("[", "resize('log', -1)", "log shorter", show=False),
        Binding("}", "resize('table', 1)", "table wider", show=False),
        Binding("{", "resize('table', -1)", "table narrower", show=False),
        Binding("plus", "grow_pane(1)", "grow pane", show=False),
        Binding("equals_sign", "grow_pane(1)", "grow pane", show=False),
        Binding("minus", "grow_pane(-1)", "shrink pane", show=False),
    ]

    def __init__(
        self, socket_path: Path, owns_run: bool = False, person: Principal | None = None
    ) -> None:
        super().__init__()
        self.socket_path = Path(socket_path)
        #: True under `dsl41 run --ui`, where quitting stops the run; False for `dsl41 ui`, where
        #: quitting detaches a viewer (DL-187)
        self.owns_run = bool(owns_run)
        # assign the per-key list: BindingsMap.bind appends, active_bindings keeps the first
        # binding for a key, and appending would leak into every other instance
        self._bindings.key_to_bindings["q"] = [
            Binding("q", "quit", "stop run" if self.owns_run else "detach")
        ]
        # a web person's name and roles stand where the socket path stands otherwise; they are the
        # lookup's answer, not the grant (DL-510)
        self._subtitle_base = (
            f"{person.spelling} ({', '.join(sorted(person.groups)) or 'no roles'})"
            if person is not None
            else str(self.socket_path)
        )
        self.sub_title = self._subtitle_base
        self._client = ControlClient(self.socket_path, person)
        #: DL-541: the pager's and the details popup's history reads have a connection of their
        #: own, so a stalled read never holds the table's refresh or the operator's keys
        self._reads = ControlClient(self.socket_path, person)
        #: the selection is a job, not a row (DL-532). `_chosen` is the job the operator picked
        #: last, kept while a fold or a filter hides it. `_selected`, what the keys act on, is
        #: derived from it on each render: the job when shown, else the folded box that stands
        #: for it, else nothing. `_picked` is False until the first answer selects its first row.
        self._chosen: str | None = None
        self._picked = False
        self._selected: str | None = None
        #: the row the cursor rests on while nothing is selected
        self._rest: str | None = None
        #: (chosen job, box) when the operator's own fold made that box stand for the job; a box
        #: that stands for it by any other change of the rows is not selected (DL-532)
        self._stand: tuple[str, str] | None = None
        #: when the table last rebuilt or moved its cursor itself: a highlight or a row-selected
        #: event stamped no later is the table's own, not a pick (DL-532). Textual stamps each
        #: message with time.monotonic off Windows, and the control socket is POSIX-only.
        self._cursor_set_at = float("-inf")
        #: held from the moment a key whose dialog or popup opens from a worker is handled until
        #: that dialog or popup is open or refused (DL-518); the table's verb keys ring the bell
        #: meanwhile. Quit is never held back. The token tells a hold from a later one.
        self._opening: object | None = None
        #: the generation of the explain pane's last request: an answer is shown only if it
        #: answers this one and its job is still the selected job (DL-535)
        self._explain_gen = 0
        #: the hop under way: until its job is selected and its row is on screen no key acts
        #: on a job (DL-535)
        self._hop: _Hop | None = None
        #: the jobs each hop left, for Backspace (DL-535)
        self._back: list[str] = []
        #: status requests sent; a hop is confirmed only by the answer to one sent after it
        #: began (DL-535)
        self._status_asked = 0
        #: the configuration rev of the last status answer, and the downstream mode's detail:
        #: ((selected job, rev, baseline), each downstream job's atoms), asked once per job,
        #: rev and baseline, since a restart on a fresh root can repeat a rev number (DL-535)
        self._config_rev: Any = None
        self._details: tuple[tuple[str, Any, str], dict[str, Any]] | None = None
        self._rows: set[str] = set()  # DataTable row keys we created
        #: DL-541: the stored history this viewer follows. `_join` is `[watermark, 2^63 - 1]` of
        #: the join's answer, None until a join; `_cursor` is the key the next forward page
        #: reads after; `_history` the answer's history token; `_history_baseline` the read
        #: header the rows came under.
        self._join: _Key | None = None
        self._cursor: _Key | None = None
        self._history: str | None = None
        self._history_baseline: str | None = None
        #: DL-541: the reason the engine keeps no history this viewer can read, once said
        self._no_history: str | None = None
        #: DL-541: the highest seq a console line has named as not stored
        self._gaps_told = 0
        #: DL-541: the engine's present time from the last status answer; None from an engine
        #: that does not send it, and then the alarm part of Problems is unavailable
        self._engine_now: datetime | None = None
        #: DL-541: job -> key -> the time each alarm entry is dated by, up to twice
        #: `_ALARM_WINDOW` old: a scan's, at or below its anchor, and each followed one above.
        #: `_alarms_cut` is true from a scan cut by its bound or one that read a gap, or a
        #: followed page with a gap, until the next scan; `_alarms_scanned` once a scan
        #: was read whole.
        self._alarm_times: dict[str, dict[_Key, datetime]] = {}
        self._alarms_cut = False
        self._alarms_scanned = False
        #: DL-541: the latest present time seen since the alarm times were last scanned
        self._now_max: datetime | None = None
        #: DL-541: a rescan became due and no scan has completed since
        self._rescan_owed = False
        #: DL-541: everything at or below this key is the alarm scan's, everything above it
        #: the follow's
        self._alarm_anchor: _Key | None = None
        #: DL-541: an engine restart whose line waits for the first row above this seq
        self._restart_at: int | None = None
        #: DL-541: set while the pager shows stored entries
        self._pager_pin: _PagerPin | None = None
        self._refusal_runs: dict[str, _RefusalRun] = {}
        self._log_paths: dict[str, tuple[str | None, str | None]] = {}
        self._tail_stream: int = 0  # 0 = out, 1 = err
        # (stream, path), since out and err may resolve to the same file and `o` must retitle
        self._tail_key: tuple[str, str | None] | None = None
        self._tail_pos: int | None = None
        #: the three splits' sizes for this session, never saved (DL-520)
        self._sizes = Sizes()
        #: the drag of a split's border under way, if any
        self._drag: _Drag | None = None
        #: the split whose handle is in the accent colour: under the pointer or dragged
        self._lit: Split | None = None
        #: the buttons whose press started or interrupted a drag: each owns its release
        self._owned: set[int] = set()
        #: a release that was swallowed is followed by the library's click; it is eaten
        self._eat_click = False
        # DL-65 navigation state
        self._jobs_snapshot: dict[str, dict[str, Any]] = {}  # last status response
        #: the concurrency-model ss6 read header from the last answer: the log the revisions in
        #: `_jobs_snapshot` were read from, so an `expect` cannot name a re-baselined run root's
        #: number
        self._baseline = ""
        self._claimed_actor = person.spelling if person is not None else claimed_actor()
        #: the filter in force (DL-511): its text, for the title, and its reading. A refused draft
        #: on the line leaves both as they were.
        self._filter = ""
        self._job_filter = JobFilter()
        self._view_mode = 0  # index into _VIEW_MODES
        self._collapsed: set[str] = set()  # box rows folded shut
        self._table_sync = _TableSync()  # DL-178v: shared with TriggersScreen
        self._spec_drift = False
        #: spec-reload: the configuration rev the engine runs and, while an update stops its
        #: jobs, its token and those jobs, as the subtitle shows them
        self._config = ""
        self._connected: bool | None = None  # None = never yet reported
        self._refreshing = False
        self._dirty = False
        #: DL-187 item 6 (P1-1): verb -> error for each of status, transitions and timers answering
        #: ok:false; empty while queries are clean. Keyed so one verb's recovery cannot erase
        #: another's live refusal.
        self._query_faults: dict[str, str] = {}
        #: DL-187 item 10: (path, error) of the last log-tail read fault reported, so a repeat
        #: stays quiet. A local read is not a protocol answer, so it is not in `_query_faults`.
        self._tail_fault: tuple[str, str] | None = None

    # ------------------------------------------------------------- layout

    def get_default_screen(self) -> Screen[Any]:
        return _MainScreen(id="_default")

    def compose(self) -> ComposeResult:
        yield _LocalHeader()
        with _PaneRow(id="main"):
            with Vertical(id="tablecol"):
                yield _FilterInput(
                    placeholder="filter: !status=su =core_* -- Enter keeps, Esc clears, F1 syntax",
                    id="filterline",
                )
                yield Static(id="filtermsg")
                yield _JobsTable(id="jobs")
            with Vertical(id="side"):
                yield _ExplainPane(id="explain-box")
                with _LogPane(id="logbox"):
                    yield _LogTail(id="logtail")
                    yield _LogSearchInput(id="logsearch")
        with Vertical(id="consolebox"):
            yield _Console(id="console")
            yield _ConsoleInput(
                placeholder="<VERB> [job] | SET_GLOBAL N=v | CHANGE_STATUS [job] STATUS"
                " [exit] -- empty job = selected job; F1 lists verbs",
                id="cmdline",
                suggester=_ConsoleSuggester(lambda: self._jobs_snapshot),
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
        if self.owns_run:
            self.run_worker(self._refresh(), group="refresh", exclusive=False)
            self.run_worker(self._follow_journal(), group="journal", exclusive=True)
        else:
            self.run_worker(self._start_detached(), group="refresh", exclusive=False)
        self.set_interval(2.0, self._poll)
        self.set_interval(0.5, self._tail_step)

    # ------------------------------------------------------- state refresh

    async def _start_detached(self) -> None:
        # DL-499: the first denied status ends a detached tab before it subscribes.
        await self._refresh()
        if self.return_code != 2:
            self.run_worker(self._follow_journal(), group="journal", exclusive=True)

    def _poll(self) -> None:
        self.run_worker(self._refresh(), group="refresh", exclusive=False)

    def action_refresh(self) -> None:
        self._poll()

    def _reset_history(self) -> None:
        """Forget everything held of the stored history, alarm times included, so a later pass
        joins again (DL-541). The console keeps its lines, which then stand for no key. A pager
        that shows stored entries gives way to the log."""
        # DL-480: report pending repeats before forgetting the old history.
        self._flush_refusal_runs()
        self._join = self._cursor = None
        self._history = self._history_baseline = None
        self._gaps_told = 0
        self._alarm_times.clear()
        self._alarms_cut = self._alarms_scanned = False
        self._now_max = self._alarm_anchor = self._restart_at = None
        # what is held is dropped, an owed rescan and an alarm-scan fault with it
        self._rescan_owed = False
        self._set_query_fault("alarm scan", None)
        try:
            self.query_one("#console", _Console).forget_keys()
        except NoMatches:
            return  # teardown
        if self._pager_pin is not None:
            self._console_write(
                Text("earlier events: the stored history changed; the pager shows the log again")
            )
            self._release_pager()

    def _flush_refusal_runs(self) -> None:
        for job, run in list(self._refusal_runs.items()):
            self._flush_refusal_run(job, run.last_clock)

    async def _refresh(self) -> None:
        """Re-query status, then the stored history, then explain. The table is drawn from the
        status answer before the history is read (DL-541). Refreshes triggered while one is in
        flight fold into a single trailing pass."""
        if self._refreshing:
            self._dirty = True
            return
        self._refreshing = True
        try:
            while True:
                self._dirty = False
                self._status_asked += 1
                asked = self._status_asked
                try:
                    status = await self._read_status({"cmd": "status"})
                except ControlClientError as exc:
                    self._set_connected(False, str(exc))
                    self._stop_hop("the engine did not answer")
                    return
                self._set_connected(True)
                # DL-187 item 6 (P1-1): an ok:false status answer is a query refusal, not a
                # dead socket: the last good table stays and the operator is told
                self._set_query_fault(
                    "status", None if status.get("ok") else str(status.get("error", "")).strip()
                )
                if status.get("ok"):
                    self._see_engine_now(_naive_utc(status.get("engine_now")))
                    self._check_status_baseline(status)
                    # concurrency-model ss6 read header: the revisions below belong to THIS log
                    self._baseline = str(status.get("baseline_id") or "")
                    self._set_drift(bool(status.get("spec_drift")))
                    self._set_config(config_line(status))
                    self._config_rev = status.get("config_rev")
                    self._update_table(status.get("jobs", {}))
                    self._confirm_hop(asked)
                else:
                    self._stop_hop("the status query was refused")
                try:
                    await self._read_history(status)
                except ControlClientError as exc:
                    self._set_connected(False, str(exc))
                    # a rescan that failed marked the alarms incomplete: the title shows it
                    self._render_table()
                    return
                self._render_table()  # the alarms and their mark as the history left them
                await self._update_explain()
                if not self._dirty:
                    return
        except NoMatches:
            return  # app teardown unmounted the widgets mid-refresh
        finally:
            self._refreshing = False

    async def _read_status(self, request: dict[str, Any]) -> dict[str, Any]:
        """A denied status ends a detached viewer and its current operation (DL-499)."""
        response = await self._client.request(request)
        if (
            not self.owns_run
            and response.get("ok") is False
            and response.get("code") == "access_denied"
        ):
            self.exit(message=Text(str(response.get("error", ""))), return_code=2)
            raise asyncio.CancelledError
        return response

    def _refresh_subtitle(self) -> None:
        base = self._subtitle_base
        if self._connected is False:
            base += " (disconnected)"
        if self._query_faults:
            base += " (refused)"
        if self._config:
            base += f"  {self._config}"
        if self._spec_drift:
            base += "  [SPEC DRIFT: estate files changed on disk]"
        self.sub_title = base

    def _set_config(self, line: str) -> None:
        """spec-reload: the subtitle names the configuration rev and an update while it stops;
        the console says when one starts stopping and when the rev moves."""
        if line == self._config:
            return
        if self._config:
            self._console_write(Text(line, style="yellow"))
        self._config = line
        self._refresh_subtitle()

    def _set_connected(self, up: bool, detail: str = "") -> None:
        if up == self._connected:
            return
        self._connected = up
        self._refresh_subtitle()
        if up:
            self._console_write(Text("connected", style="green"))
        else:
            self._console_write(Text(f"control socket unreachable: {detail}", style="red"))

    def _set_query_fault(self, verb: str, error: str | None) -> None:
        """DL-187 item 6 (P1-1): an ok:false answer from `status`, `transitions` or `timers`, distinct
        from a dead socket and from "nothing fires next". It is keyed by verb, so one verb's
        recovery cannot erase another's live refusal; the subtitle reads whether the map holds
        anything.

        On change it writes the red refusal for `verb`, or a green recovery line when `verb`
        clears."""
        if error == self._query_faults.get(verb):
            return
        if error is None:
            del self._query_faults[verb]
            self._console_write(Text(f"{verb} query ok again", style="green"))
        else:
            self._query_faults[verb] = error
            self._console_write(Text(f"{verb} query refused: {error}", style="red"))
        self._refresh_subtitle()

    def _set_drift(self, drift: bool) -> None:
        """Report source drift (DL-65); reread reads and stages the disk inputs, and the update
        command it prints applies that read (DL-569)."""
        if drift == self._spec_drift:
            return
        self._spec_drift = drift
        self._refresh_subtitle()
        if drift:
            socket = shlex.quote(str(self.socket_path.absolute()))
            self._console_write(
                Text(
                    "spec drift: estate files changed on disk; run dsl41 reread"
                    f" -S {socket} to read them, then the dsl41 update command it prints"
                    " to adopt",
                    style="yellow",
                )
            )

    def _flush_refusal_run(self, job: str, clock: str) -> None:
        run = self._refusal_runs.pop(job, None)
        if run is not None and run.repeats:
            unit = "time" if run.repeats == 1 else "times"
            self._console_write(
                Text(
                    f"{clock} {job} START_REFUSED repeated {run.repeats} more {unit},"
                    f" last {run.last_clock}",
                    style="dim",
                )
            )

    # ------------------------------------------------ stored history (DL-541)

    def _see_engine_now(self, now: datetime | None) -> None:
        """Alarm times exist only while the engine's present time is known (DL-541): a status
        without it drops them, and the next with it rebuilds them by one scan."""
        self._engine_now = now
        if now is None:
            self._alarm_times, self._alarms_scanned, self._now_max = {}, False, None
            return
        if not self._alarms_scanned:
            self._rescan_owed = True  # the clock is known, the first time or again
        elif self._now_max is not None and self._now_max - now > _ALARM_WINDOW:
            self._rescan_owed = True  # it stepped back by more than the window
        if self._alarms_scanned:
            self._now_max = now if self._now_max is None else max(self._now_max, now)

    def _check_status_baseline(self, status: Mapping[str, Any]) -> None:
        """A status answer under another read header than the held history forgets all of it,
        before the table is drawn (DL-541)."""
        baseline = status.get("baseline_id")
        held = self._history_baseline
        if self._join is None or not isinstance(baseline, str) or not baseline or held is None:
            return
        if baseline != held:
            self._stop_hop("the baseline changed")
            self._reset_history()
            self._console_write(Text("baseline changed; joining the history again", "yellow"))

    async def _page(
        self,
        request: dict[str, Any],
        *,
        baseline: str | None,
        history: str | None,
        held: int,
        client: ControlClient | None = None,
    ) -> TransitionPage:
        """One `transitions` page of the history read under `baseline` and `history`, decoded
        whole and checked before its rows are used (DL-541): it comes under that read header,
        and the stored history continues `history` through seq `held`. A refusal raises
        `_HistoryEnds`: one that means no stored history is said once on the console, any
        other is a query refusal (DL-187 item 6). A malformed answer raises
        `MalformedPage`; each reader says what that means for it."""
        wire = {"cmd": "transitions", **request}
        if history is not None:
            wire["history"] = history
        answer = await (client or self._client).request(wire)
        if isinstance(answer, Mapping) and answer.get("ok") is False:  # a refusal comes first
            refusal = decode_refusal(answer)  # a malformed envelope is MalformedPage
            raise _HistoryEnds(self._history_refused(refusal, quiet=client is not None))
        page = decode_transitions(answer, wire)
        if baseline is not None and page.baseline_id != baseline:
            raise _HistoryEnds("moved")
        through = page.continuous_through
        if history is not None and through is not None and through < held:
            raise _HistoryEnds("broken", through)
        return page

    def _history_refused(self, refusal: Refusal, *, quiet: bool) -> str:
        """The kind of a refused page: `gone` for no stored history, else `refused`."""
        error, code = refusal.error, refusal.code
        if code not in _NO_HISTORY_CODES:
            if not quiet:
                self._set_query_fault("transitions", error)
            return "refused"
        if not quiet:
            self._set_query_fault("transitions", None)
            reason = f"{error} ({code})"
            if reason != self._no_history:
                self._no_history = reason
                self._console_write(Text(f"no stored history: {reason}", style="yellow"))
        return "gone"

    def _history_ended(self, end: _HistoryEnds) -> None:
        """What a page that does not fit leaves (DL-541). No stored history, another read
        header or a history that does not continue forgets everything held; a plain refusal
        keeps it for the next pass."""
        if end.kind == "refused" or self._join is None:
            return  # nothing is held: the next pass joins again
        if end.kind == "moved":
            self._stop_hop("the baseline changed")
            self._reset_history()
            self._console_write(Text("baseline changed; joining the history again", "yellow"))
        elif end.kind == "broken":
            self._reset_history()
            self._console_write(
                Text(
                    f"the stored history does not continue past seq {end.through}; joining again",
                    style="yellow",
                )
            )
            self._dirty = True
        else:
            self._reset_history()

    async def _read_history(self, status: Mapping[str, Any]) -> None:
        """Join the stored history, or follow it, then scan the alarms again when the engine's
        present time first appears or steps back by more than the window (DL-541)."""
        baseline = status.get("baseline_id") if status.get("ok") else None
        try:
            if self._join is None:
                await self._join_history(baseline if isinstance(baseline, str) else None)
            else:
                await self._follow_history()
            if self._rescan_due():
                await self._rescan()
        except _HistoryEnds as end:
            self._history_ended(end)
        except MalformedPage as exc:
            # DL-541: a join fails whole and joins again next pass; a follow keeps its cursor
            self._set_query_fault("transitions", f"a malformed answer: {exc}")
        self._drop_old_alarms()

    def _history_read(self) -> None:
        """A join or a followed page was read whole: a refusal or a fault of earlier is over,
        and its recovery line goes before the lines the page writes (DL-541)."""
        self._set_query_fault("transitions", None)
        self._no_history = None

    def _tell_gaps(self, page: TransitionPage) -> tuple[_Interval, ...]:
        """One line for the seqs a page reports with no stored entries, each seq once; the
        page's gaps."""
        told = self._gaps_told
        fresh: list[list[int]] = []
        for low, high in page.gaps:
            if high > told:
                fresh.append([max(low, told + 1), high])
                self._gaps_told = max(self._gaps_told, high)
        if fresh:
            self._console_write(Text(f"no entries stored for {seq_words(fresh)}", style="yellow"))
        return page.gaps

    async def _join_history(self, baseline: str | None) -> None:
        """The newest stored entries and, with the engine's present time, one alarm scan
        anchored at the join point `[watermark, 2^63 - 1]` (DL-541). A join is whole or not at
        all: nothing is shown or held until every page of it is read."""
        page = await self._page(
            {"direction": "backward", "limit": _JOIN_LIMIT}, baseline=baseline, history=None, held=0
        )
        join: _Key = (page.watermark, _KEY_MAX)
        history = page.history
        if page.baseline_id:
            baseline = page.baseline_id
        now = self._engine_now
        scan = None if now is None else await self._scan(join, now, baseline, history)
        self._history_read()  # the join was read whole
        self._join = self._cursor = join
        self._history, self._history_baseline = history, baseline
        self._tell_gaps(page)
        self._consume_entries([entry.fields() for entry in page.entries])
        if scan is None:
            self._take_scan(None, join, now)
        else:
            self._scan_completed(scan, join, now)

    def _scan_completed(self, scan: _Scan, anchor: _Key, now: datetime | None) -> None:
        """The one completion of an alarm scan, the join's or a later rescan's, whole or
        bounded (DL-541): its times are held, no rescan is owed, and an alarm-scan fault is
        over, with its recovery line if one was said."""
        self._take_scan(scan, anchor, now)
        self._rescan_owed = False
        self._set_query_fault("alarm scan", None)

    def _take_scan(self, scan: _Scan | None, anchor: _Key, now: datetime | None) -> None:
        """The alarm times are a scan's: everything at or below `anchor` is the scan's,
        everything above it the follow's (DL-541)."""
        self._alarm_anchor = anchor
        if scan is None:
            self._alarm_times, self._alarms_cut, self._alarms_scanned = {}, False, False
            return
        self._alarm_times, self._alarms_cut, self._alarms_scanned = scan.times, scan.cut, True
        self._now_max = now

    def _rescan_due(self) -> bool:
        """The alarm times are rebuilt by one scan when the engine's present time becomes known,
        the first time or again after a status without it, or steps back by more than the window
        against the latest one seen since the last scan: times dropped past twice the window
        could then be inside it (DL-541). A rescan that became due stays due until a scan
        completes; a transport failure or a malformed page leaves it due."""
        return self._join is not None and self._engine_now is not None and self._rescan_owed

    async def _rescan(self) -> None:
        """Scan anchored at the follow cursor, so every row the follow read is the scan's and no
        console line is written twice or skipped; the times read replace the times held only
        once every page is read (DL-541)."""
        now, anchor = self._engine_now, self._cursor
        assert now is not None and anchor is not None
        try:
            scan = await self._scan(anchor, now, self._history_baseline, self._history)
        except ControlClientError:
            # DL-541: a transport failure changes no scan state, so the next pass asks again;
            # a scan held meanwhile is marked incomplete. The failure goes on to the
            # transport's path.
            if self._alarms_scanned:
                self._alarms_cut = True
            raise
        except MalformedPage as exc:
            # DL-541: a later rescan that meets a malformed answer ends incomplete; the times
            # it would replace stay, and the operator gets the diagnostic, once while it stays
            self._set_query_fault("alarm scan", f"a malformed answer: {exc}")
            held = self._alarm_times if self._alarms_scanned else {}
            kept = self._alarm_anchor if self._alarms_scanned else None
            self._take_scan(_Scan(dict(held), True), kept or anchor, now)
            return
        self._scan_completed(scan, anchor, now)

    async def _scan(
        self, anchor: _Key, now: datetime, baseline: str | None, history: str | None
    ) -> _Scan:
        """The times of the alarm entries at or below `anchor`, the anchor's own row included,
        each transition read backward, each page newest first, until one is dated more than
        twice the window before `now`, within `_SCAN_ROWS` rows and `_SCAN_REQUESTS` requests
        per transition (DL-541). A bound hit, or any gap in what the scan read, cuts it: a gap
        holds seqs whose entries are unknown, and nothing dates them."""
        times: dict[str, dict[_Key, datetime]] = {}
        cut = False
        # a backward page holds the keys below its cursor: from just above a row key, the
        # anchor's own row is the scan's; the join point is above every row of its seq
        start = anchor if anchor[1] == _KEY_MAX else _just_above(anchor)
        for name in sorted(_ALARM_TRANSITIONS):
            cursor, rows, requests = start, 0, 0
            while True:
                if rows >= _SCAN_ROWS or requests >= _SCAN_REQUESTS:
                    cut = True
                    break
                request = {"transition": name, "direction": "backward", "cursor": list(cursor)}
                page = await self._page(
                    {**request, "limit": min(_JOIN_LIMIT, _SCAN_ROWS - rows)},
                    baseline=baseline,
                    history=history,
                    held=anchor[0],
                )
                requests += 1
                rows += len(page.entries)
                stop: int | None = None
                for entry in reversed(page.entries):  # a page comes in ascending key order
                    if entry.key > anchor:
                        continue
                    if now - entry.at > 2 * _ALARM_WINDOW:
                        stop = entry.seq
                        break
                    times.setdefault(entry.job, {})[entry.key] = entry.at
                # a gap below the stop is never reached
                if any(stop is None or high >= stop for _, high in page.gaps):
                    cut = True
                if stop is not None or not page.more:
                    break
                following = page.next
                if following is None or following >= cursor:
                    cut = True
                    break
                cursor = following
        return _Scan(times, cut)

    async def _follow_history(self) -> None:
        """Forward pages from the cursor until `more` is false, at most `_FOLLOW_PAGES` in one
        pass; the rest is left to a trailing pass (DL-541). Past the last page the cursor is
        `[watermark, 2^63 - 1]`, so an expired, empty page is not asked again."""
        for _ in range(_FOLLOW_PAGES):
            cursor = self._cursor
            assert cursor is not None
            page = await self._page(
                {"cursor": list(cursor), "direction": "forward"},
                baseline=self._history_baseline,
                history=self._history,
                held=cursor[0],
            )
            self._history_read()  # this page was read whole, before its lines
            if page.expired:
                pruned = seq_words([list(interval) for interval in page.pruned]) or "some seqs"
                self._console_write(
                    Text(f"pruned before this view read them: {pruned}", style="yellow")
                )
            if self._tell_gaps(page):
                # DL-541: an alarm in seqs with no stored entries is unknown until the next scan
                self._alarms_cut = True
            self._follow_rows(page)
            following = page.next
            if not page.more:
                self._say_restart()  # the follow reached the watermark (DL-541)
                self._cursor = max(cursor, following or cursor, (page.watermark, _KEY_MAX))
                return
            if following is None or following <= cursor:
                return  # a page that does not move on ends this pass
            self._cursor = following
        self._dirty = True

    def _follow_rows(self, page: TransitionPage) -> None:
        """A followed page's rows. A new history token that continues the held one is an
        engine restart: its line waits until the first row above the old token's
        `continuous_through` is written, across pages and passes, or until the follow reaches
        the watermark (DL-541)."""
        entries = [entry.fields() for entry in page.entries]
        history = page.history
        if history is not None and history != self._history:
            through = page.continuous_through
            if self._history is not None and through is not None and self._restart_at is None:
                self._restart_at = through
            self._history = history
        self._consume_entries(entries, follow=True)

    def _say_restart(self) -> None:
        if self._restart_at is not None:
            self._restart_at = None
            self._flush_refusal_runs()
            self._console_write(
                Text("engine restarted; the stored history continues", style="yellow")
            )

    def _consume_entries(self, entries: Any, *, follow: bool = False) -> None:
        """One console line per entry, folding repeated scheduler refusals (DL-480), each line
        keyed by its entry; a followed alarm entry above the alarm anchor adds its time, and a
        held restart line goes before the first row above the old history (DL-541)."""
        console = self.query_one("#console", _Console)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            key = _entry_key(entry)
            if follow and self._restart_at is not None and key is not None:
                if key[0] > self._restart_at:
                    self._say_restart()
            job = str(entry.get("job", ""))
            if follow and str(entry.get("transition", "")) in _ALARM_TRANSITIONS:
                self._keep_alarm(job, key, entry.get("at"))
            clock = format_local_at(entry.get("at"))
            cause = str(entry.get("cause", ""))
            scheduler_refusal = _is_scheduler_refusal(entry)
            run = self._refusal_runs.get(job)
            if scheduler_refusal and run is not None and run.cause == cause:
                run.repeats += 1
                run.last_clock = clock
                continue
            self._flush_refusal_run(job, clock)
            if scheduler_refusal:
                self._refusal_runs[job] = _RefusalRun(cause, clock)
            console.write_entry(entry_line(entry), key)

    def _keep_alarm(self, job: str, key: _Key | None, at: Any) -> None:
        """A followed alarm's time, keyed so a row read twice is kept once (DL-541). Alarm times
        exist only while the engine's present time is known and a scan holds; a row at or below
        the scan's anchor is the scan's."""
        when = _naive_utc(at)
        anchor = self._alarm_anchor
        if key is None or when is None or anchor is None or key <= anchor:
            return
        if self._engine_now is not None and self._alarms_scanned:
            self._alarm_times.setdefault(job, {})[key] = when

    def _drop_old_alarms(self) -> None:
        """Alarm times more than twice the window old go; none inside it does (DL-541)."""
        now = self._engine_now
        if now is None:
            return
        for job in list(self._alarm_times):
            kept = {
                key: at
                for key, at in self._alarm_times[job].items()
                if now - at <= 2 * _ALARM_WINDOW
            }
            if kept:
                self._alarm_times[job] = kept
            else:
                del self._alarm_times[job]

    def _alarm_count(self, job: str) -> int:
        """The alarms of `job` in the window: dated at most `_ALARM_WINDOW` before the engine's
        present time, evaluated at each refresh (DL-541). Only times are subtracted, never the
        window from the present, so a virtual clock at its minimum cannot overflow."""
        now = self._engine_now
        if now is None:
            return 0
        return sum(1 for at in self._alarm_times.get(job, {}).values() if now - at <= _ALARM_WINDOW)

    def _alarm_note(self) -> str:
        """How far the alarm part of Problems can be trusted (DL-541): unavailable without the
        engine's present time or a stored history, incomplete after a scan cut by its bound or
        one that read a gap, or a followed page with a gap, until the next scan.
        The status part does not depend on it."""
        if self._engine_now is None or self._join is None:
            return "alarms unavailable"
        if not self._alarms_scanned or self._alarms_cut:
            return "alarms incomplete"
        return ""

    _VIEW_MODES = ("all", "problems", "active")

    def _update_table(self, jobs: dict[str, dict[str, Any]]) -> None:
        self._jobs_snapshot = jobs
        for name, row in jobs.items():
            self._log_paths[name] = (row.get("log_out"), row.get("log_err"))
        if self._chosen not in jobs:
            self._chosen = None  # a removed job leaves nothing selected (DL-532)
        self._render_table()

    def _is_problem(self, name: str, row: dict[str, Any]) -> bool:
        """The one definition of 'problem', shared by the `v` problems view and the folded-box
        rollup. QUE_WAIT counts: on a contended estate it can park a region indefinitely."""
        status = str(row.get("status", ""))
        return status in ("FAILURE", "TERMINATED", "QUE_WAIT") or self._alarm_count(name) > 0

    def _match(self, name: str, row: dict[str, Any], job_filter: JobFilter) -> bool:
        mode = self._VIEW_MODES[self._view_mode]
        if mode == "problems" and not self._is_problem(name, row):
            return False
        if mode == "active" and str(row.get("status", "")) not in (
            "STARTING",
            "RUNNING",
            "QUE_WAIT",
        ):
            return False
        box = row.get("box_name")
        return job_filter.matches(
            name, str(row.get("status", "")), box if isinstance(box, str) else None
        )

    def _children(self) -> dict[str | None, list[str]]:
        """box name -> member names (None for top level), from the status snapshot's box_name
        (DL-65), alphabetical within each level."""
        kids: dict[str | None, list[str]] = {}
        for name in sorted(self._jobs_snapshot):
            box = self._jobs_snapshot[name].get("box_name")
            kids.setdefault(box if isinstance(box, str) else None, []).append(name)
        return kids

    def _visible(self, job_filter: JobFilter) -> list[tuple[str, int]]:
        """(job, depth) rows in display order: the box tree with collapsed subtrees omitted, or a
        flat list under `job_filter` while it has words or a non-'all' view is active, so a match
        inside a collapsed box is never invisible. Only the flat list runs a person's pattern."""
        jobs = self._jobs_snapshot
        if job_filter.tests or self._view_mode:
            return [(n, 0) for n in sorted(jobs) if self._match(n, jobs[n], job_filter)]
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
        """(descendants, problems) hidden beneath a collapsed box; 'problem' is `_is_problem`,
        the set the `v` view shows."""
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
            # filtered and non-'all' views have no fold markers or rollup: the members are listed
            return cells
        marker = ""
        if kids.get(name):
            marker = "▸ " if name in self._collapsed else "▾ "
        lead = "  " * depth + marker
        label = lead + name
        style = ""
        if name in self._collapsed and kids.get(name):
            total, problems = self._rollup(name, kids)
            label += f" ({total}{f', {problems}!' if problems else ''})"
            if problems:
                style = "bold red"
        text = Text(label, style=style)
        if marker:
            # a box's indent and marker are its click target (DL-512)
            text.stylize(Style.from_meta({_FOLD_META: name}), 0, len(lead))
        cells[0] = text
        return cells

    def _stand_in(self, order: Sequence[str], flat: bool) -> str | None:
        """The job the keys act on among the rows `order` (DL-532): the chosen job when it is a
        row; in the tree, its nearest enclosing box that is a row, the folded box that stands
        for it; otherwise nothing, for a flat view hides it outright."""
        job = self._chosen
        if job is None or job in order:
            return job
        if flat:
            return None
        rows, seen = set(order), {job}
        box = self._jobs_snapshot.get(job, {}).get("box_name")
        while isinstance(box, str) and box not in seen:
            if box in rows:
                return box
            seen.add(box)
            box = self._jobs_snapshot.get(box, {}).get("box_name")
        return None

    def _set_selected(self, job: str | None) -> None:
        """Make `job` what the keys act on: the explain pane, the log tail and the fold hint
        follow it, and with nothing selected the cursor's row is painted as any other
        (DL-532)."""
        try:
            table = self.query_one("#jobs", _JobsTable)
        except NoMatches:
            return  # teardown
        table.set_class(job is None, "-resting")
        if job == self._selected:
            return
        self._selected = job
        if self._hop is not None and job != self._hop.target:
            # DL-535: a change of the selection the hop did not make ends it
            self._stop_hop(self._hop_lost(self._hop.target))
        if job is not None and job != self._chosen:
            self._console_write(
                Text(f"{job} is selected; the fold hides {self._chosen}", style="yellow")
            )
        self._name_fold_keys(table)
        # the bound method, not a coroutine: a replaced exclusive worker would leave a
        # never-awaited coroutine (arg-type: run_worker's ResultType inference rejects the bound
        # async method that textual accepts)
        self.run_worker(self._update_explain, group="explain", exclusive=True)  # type: ignore[arg-type]
        self._tail_step()

    def _choose(self, job: str) -> None:
        """The operator picked `job` (DL-532)."""
        self._chosen, self._picked, self._rest, self._stand = job, True, None, None
        self._set_selected(job)

    def pick_rest(self) -> bool:
        """Select the row the cursor rests on, if nothing is selected, and scroll it into view
        first, so the job the keys act on is on screen (DL-532)."""
        if self._selected is not None or self._rest is None:
            return False
        table = self.query_one("#jobs", _JobsTable)
        table.move_cursor(row=table.cursor_row, scroll=True)
        self._choose(self._rest)
        return True

    def _visible_in_force(self) -> list[tuple[str, int]]:
        """The rows under the filter in force, matched in one bounded pass (DL-514). A stopped
        pass clears that filter once, with a console line, and the rows are the view's."""
        visible = run_bounded(lambda: self._visible(self._job_filter), _FILTER_LIMIT_S)
        if not isinstance(visible, Stopped):
            return visible
        self._console_write(
            Text(f"filter /{self._filter}/ took too long to match; cleared", style="yellow")
        )
        self._drop_filter()
        return self._visible(self._job_filter)

    def _render_table(
        self, visible: list[tuple[str, int]] | None = None, *, fold: bool = False
    ) -> None:
        """Render `visible`, which a draft's bounded pass computed, or the rows under the
        filter in force. `fold` is True for the operator's own fold or unfold."""
        try:
            table = self.query_one("#jobs", DataTable)
        except NoMatches:
            return  # teardown
        if visible is None:
            visible = self._visible_in_force()
        kids = self._children()
        flat = bool(self._filter or self._view_mode)
        order = [name for name, _ in visible]
        rows = [self._decorated_cells(name, depth, kids, flat=flat) for name, depth in visible]
        self._rows = set(order)
        if not self._picked and order:
            # the first answer with rows selects its first row, as nothing was picked yet
            self._chosen, self._picked = order[0], True
        # no other change of the rows selects a row the operator did not choose (DL-532)
        target = self._stand_in(order, flat)
        if target is None or target == self._chosen:
            self._stand = None
        elif fold:
            self._stand = (str(self._chosen), target)
        elif self._stand != (self._chosen, target):
            target = None  # a box the operator's fold did not put in the job's place
        rebuilt: list[bool] = []

        def on_rebuild(_order: Sequence[Any], _old_selected: Any) -> str | None:
            rebuilt.append(True)
            return target

        self._table_sync.refresh(
            table,
            identity=order,
            keys=order,
            columns=_COLUMNS,
            rows=rows,
            on_rebuild=on_rebuild,
            column_widths={
                "status": 44
                if any(self._jobs_snapshot[name].get("reread") for name in order)
                else 10
            },
        )
        # a Text title, since border_title strings parse as rich markup
        title = Text(f"jobs {len(order)}/{len(self._jobs_snapshot)}")
        if self._filter:
            title.append(f" /{self._filter}/", style="italic")
        mode = self._VIEW_MODES[self._view_mode]
        if mode != "all":
            title.append(f" · {mode}", style="bold")
        note = self._alarm_note()
        if note:
            # DL-541: the alarm part of Problems, and the alarms column, is marked
            title.append(f" · {note}", style="yellow")
        table.border_title = title
        if target is not None and table.cursor_row != order.index(target):
            table.move_cursor(row=order.index(target))
            rebuilt.append(True)
        if rebuilt:
            self._cursor_set_at = time.monotonic()
        self._rest = order[table.cursor_row] if target is None and order else None
        self._set_selected(target)
        self._name_fold_keys(table)

    def _row_cells(self, name: str, row: dict[str, Any]) -> list[Any]:
        status = str(row.get("status", ""))
        # IHNA order: operator flags are set states and the armed latch comes last. The payload's
        # owner holds the alphabet (DL-145), so `--brief` and this cell agree
        flags = status_flags(row)
        timers = row.get("pending_timers") or []
        timer_text = ""
        if timers:
            first = timers[0]
            clock = format_local_at(first.get("due"))
            timer_text = f"{first.get('kind', '?')}@{clock}"
            if len(timers) > 1:
                timer_text += f" +{len(timers) - 1}"
        alarms = self._alarm_count(name)
        exit_code = row.get("exit_code")
        return [
            name,
            Text(status + reread_note(row), style=_STATUS_STYLE.get(status, "")),
            attempt_note(row),  # DL-426: beside the status, empty for a first attempt running
            format_local_at(row.get("status_at")),
            str(row.get("run_number", "")),
            "" if exit_code is None else str(exit_code),
            flags,
            timer_text,
            Text(str(alarms), style="bold red") if alarms else "",
        ]

    async def _update_explain(self) -> None:
        """Ask for the selected job's pane and show the answer (DL-535). Each request has a
        generation; an answer is shown only if it answers the latest request and its job is
        still the selected job, so a late answer for another job never fills the pane. Lines
        for another job are cleared when the request is sent."""
        pane = self.query_one("#explain-box", _ExplainPane)
        job = self._selected
        self._explain_gen += 1
        gen = self._explain_gen
        if job is None:
            pane.border_title = "explain"
            pane.show(None, gen, [])
            return
        pane.border_title = f"explain: {job}" if pane.mode == "causes" else f"downstream: {job}"
        if job != pane.source:
            pane.show(job, gen, [])
        try:
            lines = await self._pane_lines(job, pane.mode)
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            return
        if gen != self._explain_gen or self._selected != job:
            return
        pane.show(job, gen, lines)

    async def _pane_lines(self, job: str, mode: str) -> list[PaneLine]:
        """The pane's lines for `job`: its `explain` answer, or in the downstream mode its
        `deps` answer with each downstream job's atoms. Those are asked once per selected job,
        configuration rev and baseline, for at most `_DETAIL_CAP` jobs, and kept until one of
        them changes, so a refresh asks one `deps` (DL-535)."""
        if mode == "causes":
            response = await self._client.request({"cmd": "explain", "job": job})
            if not response.get("ok"):
                error = str(response.get("error", "explain failed"))
                return [PaneLine(Text(error, style="red"))]
            return explain_lines(response, self._jobs_snapshot.get(job, {}))
        deps = await self._client.request({"cmd": "deps", "job": job})
        if not deps.get("ok"):
            return [PaneLine(Text(str(deps.get("error", "deps failed")), style="red"))]
        if not isinstance(deps.get("upstream_atoms"), list):
            return downstream_lines(job, deps, {})
        key = (job, self._config_rev, self._baseline)
        if self._details is None or self._details[0] != key:
            others = [name for name in deps.get("downstream") or [] if name != job]
            atoms_of: dict[str, Any] = {}
            for other in others[:_DETAIL_CAP]:
                answer = await self._client.request({"cmd": "deps", "job": other})
                if answer.get("ok") and isinstance(answer.get("upstream_atoms"), list):
                    atoms_of[other] = answer["upstream_atoms"]
            self._details = (key, atoms_of)
        return downstream_lines(job, deps, self._details[1])

    def pane_mode_changed(self) -> None:
        """`b` in the pane: ask again in the other mode (DL-535)."""
        self.run_worker(self._update_explain, group="explain", exclusive=True)  # type: ignore[arg-type]

    # ------------------------------------------------- hop to a cause (DL-535)

    def _held_back(self, what: str) -> bool:
        """Whether a key that acts on a job is held: a dialog or popup is opening (DL-518), or
        a hop has not landed (DL-535). A held key rings the bell; one a hop holds also says so
        in a console line, naming `what` it would have done."""
        if self._hop is not None:
            self.bell()
            self._console_write(
                Text(f"{what}: held until {self._hop.target} is selected and shown", "yellow")
            )
            return True
        if self._opening:
            self.bell()
            return True
        return False

    def action_focus_explain(self) -> None:
        """`e`: the explain pane takes focus (DL-535)."""
        if self.screen.maximized is not None:
            return  # never focus a pane the maximized view is hiding
        _focus_now(self.query_one("#explain-box", _ExplainPane))

    def action_pane_leave(self) -> None:
        """Escape in the pane: focus returns to the jobs table, the selection unchanged."""
        self.query_one("#jobs", _JobsTable).focus()

    def action_pane_hop(self) -> None:
        """Enter in the pane: the operator picks the job the cursor's link names (DL-535). The
        pane must show the selected job's answer, the link's line must be in view, and its job
        must still be a job. A line scrolled out of view is scrolled back and nothing else
        happens; otherwise nothing changes and the console says why."""
        if self._held_back("go to"):
            return
        pane = self.query_one("#explain-box", _ExplainPane)
        line = pane.current()
        if line is None or line.target is None:
            self.bell()
            return
        if not pane.cursor_on_screen():
            pane.reveal_cursor()  # an action never applies to a line the operator cannot see
            return
        target, source = line.target, pane.source
        if source is None or source != self._selected:
            why = "the pane no longer shows the selected job's answer"
        elif target not in self._jobs_snapshot:
            why = f"{target} is no longer a job"
        else:
            self._back.append(source)
            self._go(target)
            return
        self._console_write(Text(f"go to {target}: {why}; nothing changed", style="yellow"))

    def action_pane_back(self) -> None:
        """Backspace in the pane: back to the job the last hop left (DL-535). A job that is no
        longer a job is dropped with a console line and the one before it is tried."""
        if self._held_back("back"):
            return
        while self._back:
            job = self._back.pop()
            if job in self._jobs_snapshot:
                self._go(job)
                return
            self._console_write(Text(f"back: {job} is no longer a job; skipped", style="yellow"))
        self._console_write(Text("back: no earlier job", style="dim"))

    def _go(self, target: str) -> None:
        """Reveal `target`, pick it by DL-532's `_choose`, and hold every key that acts on a
        job until the hop lands (DL-535): a status answer asked from now on still lists the
        target, and its row is selected and on screen."""
        self._reveal(target)
        self._choose(target)
        self._render_table()
        self._hop = _Hop(target, after=self._status_asked)
        self._poll()

    def _confirm_hop(self, asked: int) -> None:
        """A status answer was applied: one to a request sent after the hop began confirms it,
        and the row's place on screen is checked after the screen refreshes."""
        hop = self._hop
        if hop is not None and not hop.confirmed and asked > hop.after:
            hop.confirmed = True
            self.call_after_refresh(self._finish_hop, hop)

    def _hop_lost(self, target: str) -> str:
        """Why a hop to `target` can no longer land, as the selection now stands."""
        if target not in self._jobs_snapshot:
            return "it is no longer a job"
        if target not in self._table_sync.identity:
            return "it is hidden"
        return "another job was selected"

    def _stop_hop(self, why: str) -> None:
        """End the hop under way, if any, with a console line; the keys act again."""
        hop, self._hop = self._hop, None
        if hop is not None:
            self._console_write(Text(f"go to {hop.target} stopped: {why}", style="yellow"))

    def _reveal(self, job: str) -> None:
        """Make `job` a row (DL-535): its enclosing boxes are unfolded; a filter that hides it
        is cleared and a view that hides it gives way to the view of every job, each of those
        two with a console line. The table is rendered by the caller; focus stays where it
        is."""
        jobs = self._jobs_snapshot
        row = jobs.get(job, {})
        box, seen = row.get("box_name"), {job}
        while isinstance(box, str) and box not in seen:
            self._collapsed.discard(box)
            seen.add(box)
            box = jobs.get(box, {}).get("box_name")
        own_box = row.get("box_name")
        status = str(row.get("status", ""))
        if self._job_filter.tests and not self._job_filter.matches(
            job, status, own_box if isinstance(own_box, str) else None
        ):
            self._console_write(
                Text(f"filter /{self._filter}/ hides {job}; cleared", style="yellow")
            )
            self._drop_filter()
            self.query_one("#filterline", Input).styles.display = "none"
        mode = self._VIEW_MODES[self._view_mode]
        if mode != "all" and not self._match(job, row, JobFilter()):
            self._console_write(Text(f"view {mode} hides {job}; showing all jobs", style="yellow"))
            self._view_mode = 0

    def _finish_hop(self, hop: _Hop) -> None:
        """Land a confirmed `hop` once its job's row is the cursor's and on screen; else scroll
        it there and look again after the next refresh, `_HOP_FRAMES` times at most, then give
        up with a console line. A change of the selection ends the hop in `_set_selected`."""
        if self._hop is not hop:
            return
        target = hop.target
        try:
            table = self.query_one("#jobs", _JobsTable)
        except NoMatches:
            return  # teardown
        identity = self._table_sync.identity
        if self._selected != target or target not in identity:
            self._stop_hop(self._hop_lost(target))
            return
        row = identity.index(target)
        if table.cursor_row == row and self._row_on_screen(table, row):
            self._hop = None
            return
        hop.frames += 1
        if hop.frames > _HOP_FRAMES:
            self._stop_hop(f"its row did not come on screen after {_HOP_FRAMES} refreshes")
            return
        table.move_cursor(row=row, scroll=True)
        self._cursor_set_at = time.monotonic()
        self.call_after_refresh(self._finish_hop, hop)

    @staticmethod
    def _row_on_screen(table: DataTable[Any], row: int) -> bool:
        """Whether table row `row` lies inside the table's scrolled window; every row is one
        line high (`add_row`'s default), under a one-line header."""
        height = table.scrollable_content_region.height - (1 if table.show_header else 0)
        return height > 0 and table.scroll_y <= row < table.scroll_y + height

    # -------------------------------------------------------- change feed

    async def _follow_journal(self) -> None:
        """Wake-up signal only: any record means the estate may have moved; rendering comes from
        the idempotent queries, so at-least-once delivery is harmless."""
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
        if self._pager_pin is not None:
            return  # the pager shows stored entries until the operator leaves it (DL-541)
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
        except FileNotFoundError:
            return  # not created yet; the ordinary silent wait (DL-187 item 10)
        except OSError as exc:
            self._report_tail_fault(path, exc)
            return
        # the fault memo is not cleared here: a fault in the open or read below would be wiped
        # and re-set every tick, breaking once-per-distinct-error. The clear waits for a pass
        # with nothing left to fail.
        if self._tail_pos is None:
            self._tail_pos = max(0, size - _TAIL_SEED_BYTES)
        elif size < self._tail_pos:  # truncated underneath us: start over
            self._tail_pos = 0
            pager.set_source(stream, path)
        if size == self._tail_pos:
            self._clear_tail_fault()  # stat succeeded; nothing new to read
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(self._tail_pos)
                data = handle.read(size - self._tail_pos)
        except FileNotFoundError:
            return  # raced away between the stat above and this open
        except OSError as exc:
            self._report_tail_fault(path, exc)
            return
        self._clear_tail_fault()
        self._tail_pos = size
        pager.feed(data.decode("utf-8", errors="replace").splitlines())

    def _report_tail_fault(self, path: str, exc: OSError) -> None:
        """DL-187 item 10: a read fault that is not the ordinary missing-file wait, such as a
        permission error or a directory. One red line per distinct (path, error)."""
        memo = (path, str(exc))
        if memo == self._tail_fault:
            return
        self._tail_fault = memo
        self._console_write(Text(f"log tail {path}: {exc}", style="red"))

    def _clear_tail_fault(self) -> None:
        self._tail_fault = None

    def action_toggle_stream(self) -> None:
        self._tail_stream = 1 - self._tail_stream
        self._tail_step()

    # ------------------------------------------------- earlier entries (DL-541)

    def action_earlier(self) -> None:
        """`E`: the stored entries before the oldest the console holds, in the pager, one page
        per press, back to the retained floor (DL-541). The first press pins the pager to them
        and zooms it at once, so a key typed next is the pager's (DL-518); leaving the pager
        gives it back to the log. A press while a page is read, or at the floor, rings the
        bell and sends nothing."""
        if self._join is None:
            self._console_write(Text("earlier events: no stored history to read", style="yellow"))
            return
        pager = self.query_one("#logtail", _LogTail)
        pin = self._pager_pin
        if pin is None:
            start = self.query_one("#console", _Console).earlier_from() or self._join
            pin = self._pager_pin = _PagerPin(start, start, self._history, self._history_baseline)
            pager.show_entries(f"stored events before seq {start[0]}")
            pane = self.query_one("#logbox", _LogPane)
            if self.screen.maximized is not pane:
                self.screen.maximize(pane)
            _focus_now(pager)
        if pin.reading or pin.before is None:
            self.bell()
            return
        pin.reading = True
        work = functools.partial(self._page_earlier, pin)
        self.run_worker(work, group="earlier", exclusive=False)  # type: ignore[arg-type]

    async def _page_earlier(self, pin: _PagerPin) -> None:
        """One page before `pin.before`, read under the history the pin was taken from and
        checked as the follow checks its pages (DL-541). Past the pager's bound the page drops
        the pager's newest lines, since it reads backward."""
        before = pin.before
        assert before is not None
        try:
            page = await self._page(
                {"direction": "backward", "cursor": list(before), "limit": _JOIN_LIMIT},
                baseline=pin.baseline,
                history=pin.history,
                held=pin.start[0],
                client=self._reads,
            )
        except ControlClientError as exc:
            self._console_write(Text(f"earlier events: {exc}", style="red"))
            return
        except MalformedPage as exc:
            # DL-541: the page could not be read; the pager keeps its place, and `E` asks again
            self._console_write(Text(f"earlier events: a malformed answer: {exc}", "red"))
            return
        except _HistoryEnds as end:
            if self._pager_pin is not pin:
                return
            if end.kind == "refused" or end.kind == "gone":
                self._console_write(Text("earlier events: the engine refused the read", "red"))
                return
            self._console_write(
                Text("earlier events: the stored history changed; the pager shows the log again")
            )
            self._release_pager()
            return
        finally:
            pin.reading = False
        if self._pager_pin is not pin:
            return  # the operator left the pager meanwhile
        pager = self.query_one("#logtail", _LogTail)
        lines = [entry_line(entry.fields()).plain for entry in page.entries]
        gaps = seq_words([list(gap) for gap in page.gaps])
        if gaps:
            lines.insert(0, f"-- no entries stored for {gaps} --")
        following = page.next
        if page.more and following is not None and following < before:
            pin.before = following
        else:
            pin.before = None
            lines.insert(0, f"-- the stored history starts here, at seq {page.floor} --")
        pager.prepend(lines)

    def _release_pager(self) -> None:
        """The log pane left the screen's zoom: the pager follows the selected job's log again
        (DL-541)."""
        if self._pager_pin is None:
            return
        self._pager_pin = None
        # a page read in flight is cancelled, and with it its exchange on the reads connection
        self.workers.cancel_group(self, "earlier")
        try:
            self.query_one("#logtail", _LogTail).drop_entries()
        except NoMatches:
            return
        self._tail_key = None
        self._tail_step()

    # ------------------------------------------------------------------ help

    def action_toggle_help_panel(self) -> None:
        """F1, and `?` outside the pager (DL-187 item 7): toggle Textual's context-help panel
        (App.action_show_help_panel and action_hide_help_panel, textual/app.py:4562)."""
        try:
            self.screen.query_one(HelpPanel)
        except NoMatches:
            self.screen.mount(HelpPanel())
        else:
            self.screen.query("HelpPanel").remove()

    # ------------------------------------------------------ event console

    @on(DataTable.RowHighlighted, "#jobs")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """A cursor move the operator made picks its row (DL-532)."""
        job = self._current_row(event)
        if job is not None and job != self._selected and job != self._rest:
            self._choose(job)

    def _current_row(self, event: DataTable.RowHighlighted | DataTable.RowSelected) -> str | None:
        """The job of the row `event` names, if the operator's act made it: the event was
        posted after the table last set its own cursor, and its row is still the cursor's row.
        A rebuild's own events, such as the first add_row's row-0 highlight, and an event a
        rebuild overtook are not picks (DL-532)."""
        if event.row_key is None or event.row_key.value is None:
            return None
        if event.time <= self._cursor_set_at:
            return None
        job = str(event.row_key.value)
        identity = self._table_sync.identity
        row = event.data_table.cursor_row
        if row != event.cursor_row or row >= len(identity) or identity[row] != job:
            return None
        return job

    @on(DataTable.RowSelected, "#jobs")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter or a click on the cursor's row opens the selected job's details; on the row
        the cursor rests on it picks that row, and opens nothing (DL-532). An event a rebuild
        overtook does neither."""
        job = self._current_row(event)
        if job is None:
            return
        if self._selected is None:
            if job == self._rest:
                self.pick_rest()
            return
        self.action_details()

    def _hold(self) -> object:
        """Hold the verb keys until what a key is opening is open or refused (DL-518)."""
        self._opening = hold = object()
        return hold

    def _release(self, hold: object | None) -> None:
        """End `hold`; a later hold stays."""
        if hold is not None and self._opening is hold:
            self._opening = None

    def action_details(self) -> None:
        if self._selected is None:
            self._console_write(Text("details: no job selected", style="red"))
            return
        if self._held_back("details"):
            return
        # partial, not a bare coroutine: a replaced worker must not leave it unawaited
        work = functools.partial(self._open_details, self._selected, self._hold())
        self.run_worker(work, group="details", exclusive=True)  # type: ignore[arg-type]

    async def _open_details(self, job: str, hold: object) -> None:
        try:
            await self._show_details(job, hold)
        finally:
            self._release(hold)

    def action_triggers(self) -> None:
        self.push_screen(TriggersScreen())

    def action_console_words(self) -> None:
        """F1 on the console line: the verbs screen in place of the help panel (DL-516)."""
        self.push_screen(ConsoleWordsScreen())

    async def _refusal_total(
        self, job: str, row: Mapping[str, Any], baseline: str | None, history: str | None
    ) -> tuple[int, bool]:
        """The stored scheduler refusals of `row`'s run, or of its queued start, as
        `(count, exact)` (DL-541). It is unknown, `(0, False)`, unless proved exact: any end of
        the read other than the one exact path in `_walk_refusals`, a refusal of any code, a
        transport failure, a malformed answer or an exception included, is unknown or, for a
        verified identity with incomplete coverage, a lower bound. A cancel, when the popup
        closes, goes through."""
        try:
            return await self._walk_refusals(job, row, baseline, history)
        except MalformedPage as exc:
            self._console_write(Text(f"details: a malformed answer: {exc}", style="red"))
            return 0, False
        except Exception:  # noqa: BLE001 -- every end but the proved-exact one is not exact
            return 0, False

    async def _walk_refusals(
        self, job: str, row: Mapping[str, Any], baseline: str | None, history: str | None
    ) -> tuple[int, bool]:
        """`row`, `baseline` and `history` come from one `status` answer, the popup's own: its
        row, its read header and the history it names; the walk reads and writes no field of
        the viewer. It reads the job's stored entries backward from the present, each page
        newest first, on the reads connection, until the entry that opens the run: the status
        entry carrying its run number, which only a run's opening and START_REFUSED carry
        (DL-537); an entry of a later run, one that opened after the status answer, is passed
        over. Every page carries `history` and must (a) come under `baseline`, (b) report
        `history` as its own and (c) report it continuous through that page's own watermark,
        and hold whole entries: a page that fails any of these, holds an entry the server cut,
        or is malformed, or no `history` to carry (an engine that does not name it), makes the
        total unknown. Past `_SCAN_ROWS` rows or `_SCAN_REQUESTS` requests, over a gap above the
        opening, at the floor, or at an earlier run's entry first (an opening stored with no
        number), it is a lower bound. It is exact only on reaching the opening with none of
        these."""
        unknown = (0, False)
        run_number = row.get("run_number")
        if type(run_number) is not int or baseline is None or history is None:
            return unknown
        queued = row.get("status") == "QUE_WAIT"
        cursor: _Key | None = None
        total = rows = requests = 0
        whole = True  # no gap above the opening in what was read
        while True:
            if rows >= _SCAN_ROWS or requests >= _SCAN_REQUESTS:
                return total, False
            request: dict[str, Any] = {
                "job": job,
                "direction": "backward",
                "limit": min(_JOIN_LIMIT, _SCAN_ROWS - rows),
            }
            if cursor is not None:
                request["cursor"] = list(cursor)
            # any refusal raises, a malformed answer raises MalformedPage; the checks below
            # are the walk's own: (a) (b) (c)
            page = await self._page(
                request, baseline=None, history=history, held=0, client=self._reads
            )
            if (
                page.baseline_id != baseline  # (a) the row's read header
                or page.history != history  # (b) the history the row's answer names
                or page.continuous_through != page.watermark  # (c) it holds through this page
            ):
                return unknown
            if any(entry.cut for entry in page.entries):
                return unknown  # an entry the server shortened to fit the page
            requests += 1
            rows += len(page.entries)
            stop: int | None = None
            opened = False
            for entry in reversed(page.entries):  # a page comes in ascending key order
                if entry.job != job:
                    continue  # not an entry of what was asked: neither counted nor an opening
                run = entry.run_number
                if run is not None and run > run_number:
                    continue  # a run that opened after the status answer
                if run is not None and (run < run_number or "->" in entry.transition):
                    # the run's opening, or an entry of an earlier run: no entry of this run
                    # lies below it
                    stop = entry.seq
                    opened = run == run_number
                    break
                # QUE_WAIT is a class of its own: queue admission has not yet moved the run
                counts = scheduler_refusal_counts([entry.fields()])
                for (name, counted, kind), count in counts.items():
                    if name == job and counted == run_number and (kind == "QUE_WAIT") == queued:
                        total += count
            if any(stop is None or high >= stop for _, high in page.gaps):
                whole = False
            if stop is not None:
                if opened and whole:
                    return total, True  # the one exact path
                return total, False
            if not page.more:
                return total, False  # the floor first: the opening is not stored
            cursor = page.next
            if cursor is None:
                return unknown

    async def _show_details(self, job: str, hold: object | None = None) -> None:
        """The spec popup: runtime facts from `status`, dependency lines from `deps` (the blast
        radius, DL-65), source from `spec`, and a short local log tail."""
        try:
            spec = await self._client.request({"cmd": "spec", "job": job})
            status = await self._read_status({"cmd": "status", "job": job})
            deps = await self._client.request({"cmd": "deps", "job": job})
            timers = await self._client.request({"cmd": "timers"})
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            return
        self._set_connected(True)
        row = status.get("jobs", {}).get(job, {}) if status.get("ok") else {}
        body = Text()
        if row:
            state = str(row.get("status", ""))
            body.append(state, style=_STATUS_STYLE.get(state, "bold"))
            exit_code = row.get("exit_code")
            body.append(
                f"  run {row.get('run_number', 0)}"
                f"  exit {'-' if exit_code is None else exit_code}"
                f"  at {format_local_at(row.get('status_at'))}\n"
            )
            # DL-68 trigger lines
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
        head, body = body, Text()
        if row:
            for label, key in (("log out", "log_out"), ("log err", "log_err")):
                if row.get(key):
                    body.append(f"{label}: {row[key]}\n", style="dim")
        if deps.get("ok"):
            atoms = deps.get("upstream_atoms")
            if isinstance(atoms, list):
                # DL-535: a guard and a self reference are labelled, never a dependency
                words = dependency_names([a for a in atoms if isinstance(a, Mapping)])
            else:
                globals_ = (f"v({g})" for g in deps.get("globals", []))
                words = {"needs": [*deps.get("upstream", []), *globals_], "guard": [], "self": []}
            for label, section in (("needs:   ", "needs"), ("guard:   ", "guard")):
                if words[section]:
                    body.append(label, style="bold")
                    body.append(", ".join(words[section]) + "\n")
            if words["self"]:
                body.append("self:    ", style="bold")
                body.append(", ".join(words["self"]) + "\n")
            if deps.get("box_name"):
                body.append("box:     ", style="bold")
                body.append(f"{deps['box_name']}\n")
            downstream = deps.get("downstream", [])
            if isinstance(atoms, list) and any(
                isinstance(a, Mapping) and a.get("self") for a in atoms
            ):
                # the job's own self reference puts it in its own downstream (DL-535)
                downstream = [name for name in downstream if name != job]
            if downstream:
                body.append("blocks:  ", style="bold")
                body.append(", ".join(downstream) + "\n")
            members = deps.get("members", [])
            if members:
                # containment is blast radius: a KILLJOB here reaches these
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
                    handle.seek(max(0, os.stat(out_path).st_size - _DETAILS_TAIL_BYTES))
                    lines = (
                        handle.read()
                        .decode("utf-8", errors="replace")
                        .splitlines()[-_DETAILS_TAIL_LINES:]
                    )
            except OSError:
                lines = []
            if lines:
                body.append("\nlog tail:\n", style="bold")
                for line in lines:
                    body.append(f"  {line}\n", style="dim")
        screen = SpecScreen(job, head + body)
        self.push_screen(screen)
        self._release(hold)
        if row and type(row.get("run_number")) is int:
            # DL-541: the popup is open before its history is read, and says the total is
            # loading. Its row, read header and history all come from its own status answer.
            header, named = status.get("baseline_id"), status.get("history")
            baseline = header if isinstance(header, str) and header else None
            token = named if isinstance(named, str) and named else None
            screen.show(head + _refusal_line(row, "loading") + body)
            work = functools.partial(
                self._fill_refusals, screen, job, row, head, body, baseline, token
            )
            self.run_worker(work, group="refusals", exclusive=True)  # type: ignore[arg-type]

    async def _fill_refusals(
        self,
        screen: SpecScreen,
        job: str,
        row: Mapping[str, Any],
        head: Text,
        tail: Text,
        baseline: str | None,
        history: str | None,
    ) -> None:
        """The run's stored refusal total, read once and placed in the open popup when it
        arrives (DL-541): exact N, `at least N` for a lower bound, or `unknown`, which stays
        until the popup is opened again. The loading line is always replaced."""
        count, exact = await self._refusal_total(job, row, baseline, history)
        if screen not in self.screen_stack:
            return
        total = str(count) if exact else (f"at least {count}" if count else "unknown")
        screen.show(head + _refusal_line(row, total) + tail)

    # ------------------------------------------------------- pane geometry

    def action_maximize_log(self) -> None:
        pane = self.query_one("#logbox", _LogPane)
        if self.screen.maximized is pane:
            self.screen.minimize()
            self.query_one("#jobs", DataTable).focus()
        else:
            # the pane, not the bare log: the search prompt must stay visible
            self.screen.maximize(pane)
            # focus the log explicitly: focus is the pager mode switch, and textual would
            # focus the console Input, which consumes every letter key
            _focus_now(self.query_one("#logtail", _LogTail))

    def action_resize(self, pane: str, delta: int) -> None:
        """`]` `[` `}` `{`: a fifth of the split, from where it is (DL-520)."""
        self._step(Split(pane), delta / 5)

    def action_grow_pane(self, sign: int) -> None:
        """`+` (or `=`) and `-`: grow or shrink the focused pane by 5 points of the ratio, or
        the console by 2 rows. A text line takes the keys as text. The triggers view and the
        verbs screen ring the bell for them, and no other dialog sees them (DL-520)."""
        focused = self.focused
        steps = _PANE_STEPS.get(focused.id or "") if focused is not None else None
        if steps is not None:
            self._step(steps[0], steps[1] * sign)

    def _pane_area(self) -> tuple[int, int] | None:
        """The columns of the row above the console, and the rows of that row and the console
        together; None while the log is maximized, when nothing resizes (DL-520)."""
        main = self.query_one("#main")
        if main.screen.maximized is not None or not main.region.width:
            return None
        return main.region.width, self.query_one(Footer).region.y - main.region.y

    def _place_panes(self) -> None:
        """Place the splits on the screen there is now. The table column, the explain pane and
        the console get cells; the side column and the log pane keep their fr and take what is
        left, so the maximized log still fills the screen."""
        area = self._pane_area()
        if area is None:
            return
        placed = place(self._sizes, *area)
        self.query_one("#tablecol").styles.width = placed.table
        explain = area[1] - placed.console - placed.log
        self.query_one("#explain-box").styles.height = explain
        self.query_one("#consolebox").styles.height = placed.console

    def _step(self, split: Split, delta: float) -> None:
        area = self._pane_area()
        if area is not None:
            self._set_sizes(step(self._sizes, split, delta, *area))

    def _set_sizes(self, sizes: Sizes) -> None:
        if sizes != self._sizes:
            self._sizes = sizes
            self._place_panes()

    def _handle_lines(self, split: Split) -> list[tuple[str, str]]:
        """The border lines of a split as they are on screen now: with the pager's prompt
        showing, its bottom line is the log pane's, and the log's own is between the two."""
        prompt = bool(self.query_one("#logsearch").display)
        skip = ("logtail", "bottom") if prompt else None
        return [
            line
            for line in _HANDLE_LINES[split]
            if line != skip and (prompt or line[0] != "logsearch")
        ]

    def _handle_at(self, x: int, y: int) -> Split | None:
        """The split whose handle is at a screen cell: a cell of one of the border lines that
        meet there. A corner cell belongs to the horizontal line it ends."""
        if self._pane_area() is None:
            return None
        for split in (Split.CONSOLE, Split.LOG, Split.TABLE):
            for name, side in self._handle_lines(split):
                if _on_border(self.query_one(f"#{name}").region, side, x, y):
                    return split
        return None

    def _pane_mouse(self, event: events.MouseEvent) -> bool:
        """See a mouse event before any widget; True when it belongs to a drag of a split's
        border and goes no further (DL-520). A first-button press on a handle starts the drag;
        each move with that button held sets the split so the border follows the pointer; its
        release ends the drag. A press swallowed this way owns its release, and the release
        owns the click the library makes of it: both are swallowed also when the drag ended
        early (a resize, a lost focus, a dialog), so nothing under the pointer is selected,
        folded or opened. Another button pressed during a drag is swallowed the same way and
        leaves the drag going. A move with no button held means every button was released
        outside the app: the drag ends and nothing is owned any more. A move with no drag
        lights the handle under the pointer."""
        if isinstance(event, events.Click):
            eaten, self._eat_click = self._eat_click, False
            return eaten
        drag = self._drag
        if isinstance(event, events.MouseMove):
            if drag is not None and event.button == 1:
                self._drag_to(drag, event)
                return True
            if event.button == 0:
                self._owned.clear()
            self._drag = None
            self._light(self._handle_at(event.x, event.y) if event.button == 0 else None)
            return False
        if isinstance(event, events.MouseDown):
            self._eat_click = False
            self._owned.discard(event.button)  # its release was lost outside the app
            if drag is not None:
                self._owned.add(event.button)
                return True
            split = self._handle_at(event.x, event.y) if event.button == 1 else None
            area = self._pane_area()
            if split is None or area is None:
                return False
            pointer = event.x if split is Split.TABLE else event.y
            self._drag = _Drag(split, pointer, place(self._sizes, *area).of(split))
            self._owned.add(1)
            self._light(split)
            return True
        if isinstance(event, events.MouseUp) and event.button in self._owned:
            self._owned.discard(event.button)
            self._eat_click = True
            if drag is not None and event.button == 1:
                self._drag_to(drag, event)
                self._drag = None
                self._light(self._handle_at(event.x, event.y))
            return True
        return False

    def _drag_to(self, drag: _Drag, event: events.MouseEvent) -> None:
        area = self._pane_area()
        assert area is not None  # maximizing the log ends a drag (watch_maximized)
        if drag.split is Split.TABLE:
            cells = drag.cells + event.x - drag.start
        else:  # the measured pane is below its border: moving the border down shrinks it
            cells = drag.cells - (event.y - drag.start)
        self._set_sizes(set_cells(self._sizes, drag.split, cells, *area))

    def _light(self, split: Split | None) -> None:
        """Give one split's handle the accent colour, and the others their own."""
        if split is self._lit:
            return
        if self._lit is not None:
            for name, side in _HANDLE_LINES[self._lit]:
                self.query_one(f"#{name}").remove_class(f"-grab-{side}")
        if split is not None:
            for name, side in self._handle_lines(split):
                self.query_one(f"#{name}").add_class(f"-grab-{side}")
        self._lit = split

    def _release_handle(self) -> None:
        """End a drag and give the handle its colour back: the screen changed, the terminal's
        size changed or the app lost focus, so the release may never come."""
        self._drag = None
        self._light(None)

    def on_resize(self) -> None:
        # the row above the console places the splits again when its own size changes
        self._release_handle()

    def on_app_blur(self) -> None:
        self._release_handle()

    # ------------------------------------------------- quit / send (DL-187)

    _QUIT_BODY = "stop the run?\n\nlive jobs are cancelled; wrappers record the kills"

    async def _confirm(self, title: str, body: str, hold: object | None = None) -> bool:
        """The one confirm idiom for every destructive action. A confirm already open wins: a
        ctrl+q or key that reaches through it is ignored, not stacked, read from screen state.
        `hold` ends when the dialog is pushed, or at once when one is already open (DL-518);
        from then the dialog's own bindings rule."""
        if isinstance(self.screen, ConfirmScreen):
            self._release(hold)
            return False
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.push_screen(
            ConfirmScreen(title, body), callback=lambda result: answer.set_result(bool(result))
        )
        self._release(hold)
        return await answer

    async def action_quit(self) -> None:
        """Quit by posture. A detached viewer (`dsl41 ui`) exits at once, since confirming a
        detach would train operators to press through the dialog that matters. `run --ui` owns
        the engine, so quit stops the run and is confirmed. Ctrl+Q and the command palette's
        Quit route here. The confirm runs in a worker that awaits the answer. Quit is never held
        back by a key that is opening something (DL-518)."""
        if not self.owns_run:
            self.exit()
            return
        work = functools.partial(self._confirm_quit, self._hold())
        self.run_worker(work, group="quit", exclusive=False)  # type: ignore[arg-type]

    async def _confirm_quit(self, hold: object) -> None:
        try:
            if await self._confirm("stop run", self._QUIT_BODY, hold):
                self.exit()
        finally:
            self._release(hold)

    #: verbs a key press confirms first (DL-187): the two that destroy work in flight
    _CONFIRM_VERBS = frozenset({"KILLJOB", "FORCE_STARTJOB"})

    def action_send(self, verb: str) -> None:
        if self._held_back(verb):
            return
        request = parse_console_command(verb, self._selected)
        if isinstance(request, str):
            self._console_write(Text(request, style="red"))
            return
        if request.get("verb") in self._CONFIRM_VERBS:
            # partial, not a bare coroutine: a worker cancelled by a quit must not leave it
            # unawaited
            work = functools.partial(self._confirm_send, request, self._hold())
            self.run_worker(work, group="send", exclusive=False)  # type: ignore[arg-type]
            return
        self.run_worker(self._do_sendevent(request), group="send", exclusive=False)

    async def _confirm_send(self, request: dict[str, Any], hold: object) -> None:
        try:
            await self._confirmed_send(request, hold)
        finally:
            self._release(hold)

    async def _confirmed_send(self, request: dict[str, Any], hold: object) -> None:
        """Gate a destructive key press behind ConfirmScreen (DL-187). The precondition is read
        before the modal opens and carried through, so the operator agrees to the revision shown
        and a job that moved is refused by DL-90."""
        payload = dict(request.get("payload") or {})
        verb = str(request.get("verb"))
        target = str(payload.get("job") or payload.get("name") or "")
        label = echo_label(request)  # the one place a request is spelled (DL-187 item 9)
        # snapshot before any await: status, blast radius and revision describe one instant
        row = self._jobs_snapshot.get(target) or {}
        members = self._descendants(target)
        try:
            frozen = await self._precondition(addressed_key(verb, payload))
        except (EngineError, ControlClientError) as exc:
            self._console_write(
                Text(f"{label}: cannot confirm without current pins: {exc}", style="red")
            )
            return
        body = confirm_body(
            verb,
            target,
            str(row.get("status") or "?"),
            frozen[1],
            members,
        )
        if not await self._confirm("confirm", body, hold):
            self._console_write(Text(f"{label}: cancelled", style="dim"))
            return
        await self._do_sendevent(request, frozen=frozen)

    def _descendants(self, name: str) -> list[str]:
        """Every job beneath a box, depth-first: the blast radius a box verb reaches (DL-65)."""
        kids = self._children()
        out: list[str] = []

        def walk(box: str) -> None:
            for child in kids.get(box, []):
                out.append(child)
                walk(child)

        walk(name)
        return out

    def action_focus_console(self) -> None:
        if self.screen.maximized is not None:
            return  # never focus an input the maximized view is hiding
        _focus_now(self.query_one("#cmdline", Input))

    # -------------------------------------------- filter / view / fold (DL-65)

    def action_focus_filter(self) -> None:
        # defense in depth: the pager shadows `/`, but a focus hole must not reopen the
        # invisible-filter bug (DL-67)
        if self.screen.maximized is not None:
            return
        line = self.query_one("#filterline", Input)
        line.styles.display = "block"
        _focus_now(line)

    def clear_filter(self) -> None:
        self.query_one("#jobs", DataTable).focus()
        self._drop_filter()
        self.query_one("#filterline", Input).styles.display = "none"
        self._render_table()

    def _drop_filter(self) -> None:
        """Empty the line and clear the filter's text, predicate and message. A line without
        focus closes; one with focus stays open, so the next keystroke is not a verb key."""
        line = self.query_one("#filterline", Input)
        line.value = ""
        self._filter, self._job_filter = "", JobFilter()
        self._show_filter_refusal(None)
        if not line.has_focus:
            line.styles.display = "none"

    def _show_filter_refusal(self, message: str | None) -> None:
        """The refused line's message, under the filter line; a Text, so it is never markup."""
        box = self.query_one("#filtermsg", Static)
        box.update(Text(message or "", style="bold red"))
        box.styles.display = "block" if message else "none"

    @on(Input.Changed, "#filterline")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        text = event.value.strip()
        reading = read_filter(text)
        if isinstance(reading, str):
            # the filter in force stays and goes on applying to each status answer (DL-511)
            self._show_filter_refusal(reading)
            return
        # the draft is tried over the present rows before it takes force (DL-514)
        visible = run_bounded(lambda: self._visible(reading), _FILTER_LIMIT_S)
        if isinstance(visible, Stopped):
            self._show_filter_refusal("the filter took too long to match and is not applied")
            return
        self._show_filter_refusal(None)
        self._filter, self._job_filter = text, reading
        self._render_table(visible)

    @on(Input.Submitted, "#filterline")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            self.clear_filter()
            return
        if text != self._filter:
            return  # a refused or stopped draft keeps the input and its message (DL-511, DL-514)
        self.query_one("#filterline", Input).styles.display = "none"
        self.query_one("#jobs", DataTable).focus()
        self._pick_sole_row()

    def action_cycle_view(self) -> None:
        self._view_mode = (self._view_mode + 1) % len(self._VIEW_MODES)
        self._render_table()
        self._pick_sole_row()

    def _pick_sole_row(self) -> None:
        """Enter on the filter line, or the view key, that leaves one row shown in a filtered
        view picks that row, as a pick by hand would: it is the one row shown and highlighted
        (DL-532 rule 6). Only those two call this. A keystroke in the filter line, a status
        answer or any other change of the rows never does, so it selects nothing."""
        rows = self._table_sync.identity
        if (self._filter or self._view_mode) and len(rows) == 1 and rows[0] != self._selected:
            self._choose(rows[0])

    def _foldable(self, name: str | None) -> bool:
        """A box with members in the shown tree: what space and a marker click fold, and the
        row the lower border names both fold keys on (DL-512). The tree exists only in the
        unfiltered 'all' view."""
        return (
            name is not None
            and not self._filter
            and not self._view_mode
            and bool(self._children().get(name))
        )

    def _name_fold_keys(self, table: DataTable[Any]) -> None:
        """The jobs table's lower border names the fold keys that act on the selected job
        (DL-512): both on a box with members, `z` on another row of a tree with such a box,
        none in a flat view. A Text, since a border string parses as markup."""
        hint = ""
        if self._foldable(self._selected):
            hint = "space fold · z fold all"
        elif not (self._filter or self._view_mode):
            kids = self._children()
            if any(name in kids for name in self._jobs_snapshot):
                hint = "z fold all"
        # the getter returns markup, which equals these hints: they hold no markup characters
        if (table.border_subtitle or "") != hint:
            table.border_subtitle = Text(hint)

    def action_toggle_box(self) -> None:
        if self._hop is not None:
            self._held_back("fold")  # no key acts on a job until a hop lands (DL-535)
            return
        self._toggle_box(self._selected)

    @on(_JobsTable.FoldClicked)
    def _on_fold_clicked(self, event: _JobsTable.FoldClicked) -> None:
        # the click picks its row even where the cursor already rested (DL-512, DL-532)
        if event.job != self._selected:
            self._choose(event.job)
        self._toggle_box(event.job)

    def _toggle_box(self, name: str | None) -> None:
        if name is None or not self._foldable(name):
            return
        self._collapsed.symmetric_difference_update({name})
        self._render_table(fold=True)

    def action_toggle_all(self) -> None:
        if self._filter or self._view_mode:
            return
        if self._collapsed:
            self._collapsed.clear()
        else:
            kids = self._children()
            self._collapsed = {name for name in self._jobs_snapshot if kids.get(name)}
        self._render_table(fold=True)

    @on(Input.Submitted, "#cmdline")
    def _on_command(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        request = parse_console_command(text, self._selected)
        jobless = isinstance(request, dict) and isinstance(parse_console_command(text, None), str)
        if self._hop is not None and jobless:
            # DL-535: a verb without a job names no job until a hop lands
            self._console_write(
                Text(
                    f"{text.split()[0].upper()}: held until {self._hop.target} is selected"
                    " and shown; nothing sent",
                    style="yellow",
                )
            )
            return
        if isinstance(request, str):
            self._console_write(Text(request, style="red"))
            return
        self.run_worker(self._do_sendevent(request), group="send", exclusive=False)

    async def _do_sendevent(
        self, request: dict[str, Any], frozen: tuple[str, int] | None = None
    ) -> None:
        """Complete the concurrency-model ss6 envelope and send it (DL-90).

        `expect` is the revision `_jobs_snapshot` showed when the operator acted (ss0): a job
        that moved since is refused and the operator is told. Anything the table does not hold
        (a global, a `JOB^INST` ghost) is read first.

        `frozen` is that tuple read before a confirm modal opened (DL-187): the revision the
        operator agreed to."""
        payload = dict(request.get("payload") or {})
        label = echo_label(request)  # DL-187 item 9: the whole request
        try:
            key = addressed_key(str(request.get("verb")), payload)
        except EngineError as exc:
            self._console_write(Text(f"{label}: {exc}", style="red"))
            return
        recovery: str | None = None
        try:
            baseline, revision = frozen if frozen is not None else await self._precondition(key)
            envelope = command(
                str(request.get("verb")),
                payload,
                key=key,
                revision=revision,
                baseline_id=baseline,
                claimed_actor=self._claimed_actor,
            )
            recovery = recovery_command(self.socket_path, envelope)
            # DL-391: render the complete pinned command before the write can start.
            self._console_write(Text(f"sending: {recovery}", style="dim"))
            await self.query_one("#console", RichLog).wait_for_refresh()
            response = await self._client.request(envelope)
        except ControlClientError as exc:
            self._set_connected(False, str(exc))
            fault = exc if recovery is not None else ControlClientError(str(exc))
            self._console_write(_transport_line(label, fault))
            if exc.delivered and recovery is not None:
                self._console_write(Text(f"re-read; recover ONLY as: {recovery}", style="yellow"))
            return
        self._set_connected(True)
        self._console_write(_outcome_line(label, response))
        if outcome_of(response) == UNKNOWN:
            self._console_write(Text(f"re-read; recover ONLY as: {recovery}", style="yellow"))
        await self._refresh()

    async def _precondition(self, key: str) -> tuple[str, int]:
        """The concurrency-model ss6 read header and the revision to name for `key`: for a job
        the table shows, the revision the operator saw (ss0); otherwise a fresh read."""
        namespace, _, name = key.partition(":")
        row = self._jobs_snapshot.get(name) if namespace == "job" else None
        if row is not None and isinstance(row.get("state_rev"), int):
            return self._baseline, row["state_rev"]
        request = read_for(key)
        read = (
            await self._read_status(request)
            if request["cmd"] == "status"
            else await self._client.request(request)
        )
        return str(read.get("baseline_id") or ""), revision_in(read, key)

    def _console_write(self, text: Text) -> None:
        try:
            self.query_one("#console", RichLog).write(text)
        except NoMatches:
            pass  # a worker resuming after an await can outlive the screen

    # ------------------------------------------------------------ teardown

    async def on_unmount(self) -> None:
        await self._client.close()
        await self._reads.close()
