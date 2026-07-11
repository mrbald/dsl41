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
- EVENT CONSOLE accepts exactly the ss10 sendevent verbs (job verbs;
  SET_GLOBAL NAME=value; CHANGE_STATUS [job] STATUS [exit_code]); an
  omitted job means the selected row. Key bindings fire the common verbs on
  the selected job. Every request and its response is echoed to the
  console; refusals render red and change nothing -- the server already
  validates against the catalog (vendor parity), the TUI never pre-judges.
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
import contextlib
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

try:
    from rich.text import Text
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.coordinate import Coordinate
    from textual.css.query import NoMatches
    from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static
except ModuleNotFoundError as exc:  # pragma: no cover -- exercised via CLI guard
    raise ModuleNotFoundError(
        "the dsl41 TUI needs the optional [ui] extra: pip install 'dsl41[ui]'"
    ) from exc

from dsl41.runner import _JOB_EVENT_VERBS, _STATUSES

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
_COLUMNS = ("job", "status", "at", "run", "exit", "flags", "timers", "alarms")


class ControlClientError(RuntimeError):
    """The control socket is unreachable or hung up mid-exchange."""


class ControlClient:
    """JSON-lines client of the ss10 control socket. One persistent
    request/response connection, serialized by a lock; subscribe() opens its
    OWN connection because a subscription owns its connection until hangup
    (ss10). Any transport error drops the connection so the next request
    reconnects -- the client outlives engine restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            try:
                if self._writer is None:
                    self._reader, self._writer = await asyncio.open_unix_connection(str(self.path))
                assert self._reader is not None
                self._writer.write(json.dumps(payload).encode("utf-8") + b"\n")
                await self._writer.drain()
                line = await self._reader.readline()
            except OSError as exc:
                await self._drop()
                raise ControlClientError(str(exc)) from exc
            except BaseException:
                # a CANCELLED exchange (an exclusive worker superseded
                # mid-request) leaves the response unread on the stream;
                # reusing the connection would hand that stale line to the
                # NEXT request and offset every reply after it (DL-46
                # review B1) -- drop the connection, reconnect lazily
                await self._drop()
                raise
            if not line:
                await self._drop()
                raise ControlClientError("engine hung up")
            try:
                response = json.loads(line)
            except ValueError as exc:
                await self._drop()
                raise ControlClientError(f"bad response line: {exc}") from exc
            if not isinstance(response, dict):
                await self._drop()
                raise ControlClientError("response is not a JSON object")
            return response

    async def subscribe(self, since: int | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yield journal records until the engine hangs up. Raises
        ControlClientError if the connection fails or the engine refuses
        (e.g. a journal-less run)."""
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path))
        except OSError as exc:
            raise ControlClientError(str(exc)) from exc
        try:
            request: dict[str, Any] = {"cmd": "subscribe"}
            if since is not None:
                request["since"] = since
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            ack_line = await reader.readline()
            if not ack_line:
                raise ControlClientError("engine hung up before the subscribe ack")
            ack = json.loads(ack_line)
            if not ack.get("ok"):
                raise ControlClientError(str(ack.get("error", "subscribe refused")))
            while True:
                line = await reader.readline()
                if not line:
                    return  # engine gone; the caller decides whether to retry
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # torn record: it is only a wake-up signal anyway
                yield record
        except OSError as exc:
            raise ControlClientError(str(exc)) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _drop(self) -> None:
        # detach BEFORE the awaits: _drop runs on cancellation paths, and a
        # re-delivered CancelledError mid-close must not leave a half-dead
        # connection looking attached
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


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

    if verb in _JOB_EVENT_VERBS:
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
        if args and args[0].upper() in _STATUSES:
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
    CSS = """
    #main { height: 1fr; }
    #jobs { width: 3fr; border: round $primary; }
    #side { width: 2fr; }
    #explain-box { height: 2fr; border: round $primary; }
    #logtail { height: 3fr; border: round $primary; }
    #consolebox { height: 11; }
    #console { height: 1fr; border: round $secondary; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("s", "send('STARTJOB')", "start"),
        Binding("f", "send('FORCE_STARTJOB')", "force", show=False),
        Binding("k", "send('KILLJOB')", "kill"),
        Binding("i", "send('ON_ICE')", "ice", show=False),
        Binding("I", "send('OFF_ICE')", "off-ice", show=False),
        Binding("h", "send('ON_HOLD')", "hold", show=False),
        Binding("H", "send('OFF_HOLD')", "off-hold", show=False),
        Binding("n", "send('ON_NOEXEC')", "noexec", show=False),
        Binding("N", "send('OFF_NOEXEC')", "off-noexec", show=False),
        Binding("o", "toggle_stream", "out/err"),
        Binding("r", "refresh", "refresh"),
        Binding("slash", "focus_console", "console"),
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
        self._tail_path: str | None = None
        self._tail_pos: int | None = None
        self._connected: bool | None = None  # None = never yet reported
        self._refreshing = False
        self._dirty = False

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield DataTable(id="jobs")
            with Vertical(id="side"):
                with VerticalScroll(id="explain-box"):
                    yield Static(id="explain")
                yield RichLog(id="logtail", highlight=False, markup=False, wrap=False)
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
            table.add_column(label, key=label)
        self.query_one("#explain-box").border_title = "explain"
        self.query_one("#logtail", RichLog).border_title = "log"
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
                    self._update_table(status.get("jobs", {}))
                await self._update_explain()
                if not self._dirty:
                    return
        except NoMatches:
            return  # app teardown unmounted the widgets mid-refresh
        finally:
            self._refreshing = False

    def _set_connected(self, up: bool, detail: str = "") -> None:
        if up == self._connected:
            return
        self._connected = up
        console = self.query_one("#console", RichLog)
        if up:
            self.sub_title = str(self.socket_path)
            console.write(Text("connected", style="green"))
        else:
            self.sub_title = f"{self.socket_path} (disconnected)"
            console.write(Text(f"control socket unreachable: {detail}", style="red"))

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
            style = "bold red" if transition in _ALARM_TRANSITIONS else "dim"
            console.write(
                Text(f"{clock} {job} {transition} [{entry.get('cause', '')}]", style=style)
            )

    def _update_table(self, jobs: dict[str, dict[str, Any]]) -> None:
        table = self.query_one("#jobs", DataTable)
        for name in sorted(jobs):
            row = jobs[name]
            self._log_paths[name] = (row.get("log_out"), row.get("log_err"))
            cells = self._row_cells(name, row)
            if name in self._rows:
                for column, value in zip(_COLUMNS, cells):
                    table.update_cell(name, column, value, update_width=True)
            else:
                table.add_row(*cells, key=name)
                self._rows.add(name)
        if self._selected is None and table.row_count:
            self._selected = str(table.coordinate_to_cell_key(Coordinate(0, 0)).row_key.value)

    def _row_cells(self, name: str, row: dict[str, Any]) -> list[Any]:
        status = str(row.get("status", ""))
        at = str(row.get("status_at") or "")
        flags = "".join(
            mark
            for mark, flag in (("I", "on_ice"), ("H", "on_hold"), ("N", "on_noexec"))
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
            text.append(f"{atom.get('atom', '')}\n", style="" if true else "bold")
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
            widget = self.query_one("#logtail", RichLog)
        except NoMatches:
            return  # a set_interval tick can outlive the unmounting screen
        paths = self._log_paths.get(self._selected or "", (None, None))
        path = paths[self._tail_stream]
        stream = ("out", "err")[self._tail_stream]
        if path != self._tail_path:
            self._tail_path = path
            self._tail_pos = None
            widget.clear()
            widget.border_title = f"log ({stream}): {path or 'none yet'}"
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
            widget.clear()
        if size == self._tail_pos:
            return
        try:
            with open(path, "rb") as handle:
                handle.seek(self._tail_pos)
                data = handle.read(size - self._tail_pos)
        except OSError:
            return
        self._tail_pos = size
        for line in data.decode("utf-8", errors="replace").splitlines():
            widget.write(line)

    def action_toggle_stream(self) -> None:
        self._tail_stream = 1 - self._tail_stream
        self._tail_step()

    # ------------------------------------------------------ event console

    @on(DataTable.RowHighlighted, "#jobs")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        selected = str(event.row_key.value)
        if selected == self._selected:
            return
        self._selected = selected
        self.run_worker(self._update_explain(), group="explain", exclusive=True)
        self._tail_step()

    def action_send(self, verb: str) -> None:
        request = parse_console_command(verb, self._selected)
        console = self.query_one("#console", RichLog)
        if isinstance(request, str):
            console.write(Text(request, style="red"))
            return
        self.run_worker(self._do_sendevent(request), group="send", exclusive=False)

    def action_focus_console(self) -> None:
        self.query_one("#cmdline", Input).focus()

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
