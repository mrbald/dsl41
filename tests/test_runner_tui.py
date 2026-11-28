"""RunnerApp Textual TUI tests (phase 11d).

Normative spec: docs/runner-design.md ss11 (UI: one Textual app, terminal
and web) and ss13 item 6 ("TUI: textual pilot snapshot smoke only");
runner_tui.py's own module docstring is the normative detail for
ControlClient (the ss10 socket client), parse_console_command (the ss11
event-console grammar) and RunnerApp (jobs table / explain pane / log tail /
event console). House style follows test_runner_control.py: the
`_serve`/`_teardown` harness, asyncio.run per scenario, the POSIX skip
guard -- duplicated here rather than imported, matching how test_runner.py
duplicates test_oracle.py's own small helpers. `short_root` (AF_UNIX
sun_path length) is conftest.py's shared fixture, not duplicated here.

Every expected outcome here was verified empirically against the real app
before the assertion was written (CLAUDE.md: fidelity is tested, not
asserted) -- see the final report for anything that surprised us or
contradicted the design doc.

Section 3 (RunnerApp pilot smoke) is deliberately a HANDFUL of tests, not an
exhaustive UI matrix (ss13 item 6): one per major view (table, explain,
console, timers, log tail), each driving the real control socket end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import shlex
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.console import Console
from textual import events
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.pilot import _get_mouse_message_arguments
from textual.widgets import DataTable, Footer, HelpPanel, Input, RichLog, Static

from dsl41 import runner_tui
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.pane_layout import Split
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_control import (
    JOB_EVENT_VERBS,
    ControlClient,
    ControlClientError,
    ControlServer,
)
from dsl41.console_words import STATUS_ROWS, VERB_ROWS
from dsl41.runner_startup import start_run
from dsl41.runner_transitions import agrees_through
from dsl41.runner_tui import (
    _COLUMNS,
    _CONSOLE_VERBS,
    ConfirmScreen,
    ConsoleWordsScreen,
    RunnerApp,
    SpecScreen,
    TriggersScreen,
    _ConsoleInput,
    _FilterInput,
    _JobsTable,
    _LogPane,
    _LogTail,
    _outcome_line,
    _transport_line,
    assemble_detail_trigger_lines,
    assemble_trigger_rows,
    compile_search,
    confirm_body,
    echo_label,
    format_countdown,
    parse_console_command,
)

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)


async def _serve(
    run_root: Path,
    text: str,
    *,
    adapter: FakeAdapter | None = None,
    spec_texts: dict[str, str] | None = None,
) -> tuple[Engine, ControlServer, asyncio.Task]:
    """Shared harness (test_runner_control.py): a real-domain, hold_open
    engine serving a control socket, run_until_quiescent(datetime.max) as a
    background task -- the exact shape `dsl41 run --ui` drives."""
    catalog = lower_source(text)
    clock = RealClock()
    adapter = adapter if adapter is not None else FakeAdapter()
    engine = start_run(
        catalog, run_root, clock=clock, adapters={"CMD": adapter, "FW": adapter}, hold_open=True
    )
    server = ControlServer(engine, run_root / "control.sock", spec_texts=spec_texts)
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task


async def _teardown(engine: Engine, server: ControlServer, loop_task: asyncio.Task) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await server.close()
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


async def _wait_for_async(predicate, timeout_s: float = 3.0, interval_s: float = 0.02) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


async def _wait_for_ui(pilot, predicate, timeout_s: float = 5.0, interval_s: float = 0.05) -> None:
    """Poll a synchronous predicate against live app state, pumping the
    pilot's message queue and yielding real time between checks -- RunnerApp
    drives its refresh via a worker task + a real control-socket round trip,
    so state changes asynchronously across event-loop iterations, not
    synchronously with a single pilot.pause()."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


# ---------------------------------------------------- 1. parse_console_command


def test_parse_job_verb_with_explicit_job() -> None:
    assert parse_console_command("STARTJOB myjob", None) == {
        "cmd": "sendevent",
        "verb": "STARTJOB",
        "payload": {"job": "myjob"},
    }


def test_parse_job_verb_is_case_insensitive() -> None:
    assert parse_console_command("startjob myjob", None) == {
        "cmd": "sendevent",
        "verb": "STARTJOB",
        "payload": {"job": "myjob"},
    }


def test_parse_job_verb_without_explicit_job_defaults_to_the_selected_row() -> None:
    assert parse_console_command("KILLJOB", "selected_job") == {
        "cmd": "sendevent",
        "verb": "KILLJOB",
        "payload": {"job": "selected_job"},
    }


def test_parse_job_verb_without_explicit_job_and_no_selection_errors() -> None:
    assert parse_console_command("STARTJOB", None) == "STARTJOB: no job selected"


@pytest.mark.parametrize("verb", sorted(JOB_EVENT_VERBS))
def test_parse_every_job_verb_takes_at_most_one_job(verb: str) -> None:
    assert parse_console_command(f"{verb} a b", None) == f"{verb} takes at most one job"


def test_parse_set_global_name_equals_value() -> None:
    assert parse_console_command("SET_GLOBAL FLAG=go", None) == {
        "cmd": "sendevent",
        "verb": "SET_GLOBAL",
        "payload": {"name": "FLAG", "value": "go"},
    }


def test_parse_set_global_value_may_itself_contain_an_equals_sign() -> None:
    """`partition` splits on the FIRST '=' only, so the value may contain
    more of them untouched."""
    assert parse_console_command("SET_GLOBAL FLAG=a=b", None) == {
        "cmd": "sendevent",
        "verb": "SET_GLOBAL",
        "payload": {"name": "FLAG", "value": "a=b"},
    }


@pytest.mark.parametrize(
    "text",
    [
        "SET_GLOBAL FLAG",  # no '='
        "SET_GLOBAL =go",  # empty name
        "SET_GLOBAL FLAG=go extra",  # wrong arity
    ],
)
def test_parse_set_global_malformed_variants(text: str) -> None:
    assert parse_console_command(text, None) == 'SET_GLOBAL expects "NAME=value"'


def test_parse_change_status_status_first_with_selected_job() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS", "jobx") == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "jobx", "status": "SUCCESS"},
    }


def test_parse_change_status_status_first_without_selection_errors() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS", None) == "CHANGE_STATUS: no job selected"


def test_parse_change_status_job_first() -> None:
    assert parse_console_command("CHANGE_STATUS jobx SUCCESS", None) == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "jobx", "status": "SUCCESS"},
    }


def test_parse_change_status_job_first_with_exit_code() -> None:
    assert parse_console_command("CHANGE_STATUS jobx FAILURE 1", None) == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "jobx", "status": "FAILURE", "exit_code": 1},
    }


def test_parse_change_status_status_first_with_exit_code_and_selected_job() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS 0", "jobx") == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "jobx", "status": "SUCCESS", "exit_code": 0},
    }


def test_parse_change_status_non_integer_exit_code_errors() -> None:
    assert parse_console_command("CHANGE_STATUS jobx FAILURE notanumber", None) == (
        "exit_code must be an integer, got 'notanumber'"
    )


def test_parse_change_status_too_many_exit_code_args_errors() -> None:
    assert (
        parse_console_command("CHANGE_STATUS jobx FAILURE 1 2", None)
        == "CHANGE_STATUS expects at most one exit_code"
    )


@pytest.mark.parametrize("text", ["CHANGE_STATUS jobx", "CHANGE_STATUS"])
def test_parse_change_status_missing_args_errors(text: str) -> None:
    assert parse_console_command(text, None) == "CHANGE_STATUS expects [job] STATUS [exit_code]"


def test_parse_change_status_status_first_but_second_token_is_also_a_status() -> None:
    """F8 (DL-187 item 8): the shorthand applies only when the SECOND token
    is not itself a status. Here it is, so this is the explicit `<job>
    STATUS` form -- a job that happens to be named SUCCESS."""
    assert parse_console_command("CHANGE_STATUS SUCCESS FAILURE", None) == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "SUCCESS", "status": "FAILURE"},
    }


def test_parse_change_status_shorthand_survives_a_non_status_second_token() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS 3", "jobx") == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "jobx", "status": "SUCCESS", "exit_code": 3},
    }


def test_parse_change_status_explicit_form_with_a_status_named_job_and_exit_code() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS FAILURE 2", None) == {
        "cmd": "sendevent",
        "verb": "CHANGE_STATUS",
        "payload": {"job": "SUCCESS", "status": "FAILURE", "exit_code": 2},
    }


def test_parse_unknown_verb_errors() -> None:
    """F6 (DL-187 item 7): the error enumerates the verbs sorted, so an
    operator who mistypes one sees the whole grammar, not just a refusal."""
    assert parse_console_command("FROBNICATE job", None) == (
        f"unknown verb 'FROBNICATE' -- expected one of: {', '.join(_CONSOLE_VERBS)}"
    )
    assert _CONSOLE_VERBS == sorted(_CONSOLE_VERBS)
    assert "DISARM" in _CONSOLE_VERBS
    assert {"SET_GLOBAL", "CHANGE_STATUS"} <= set(_CONSOLE_VERBS)


@pytest.mark.parametrize("text", ["", "   "])
def test_parse_empty_input_errors(text: str) -> None:
    assert parse_console_command(text, None) == "empty command"


# --------------------------------------------- 1b. the four outcomes (S4, DL-92)


def test_the_console_says_where_an_applied_command_landed() -> None:
    line = _outcome_line("> ON_HOLD j", {"ok": True, "decision": "applied", "index": 7})
    assert line.plain == "> ON_HOLD j: applied @ #7"
    assert line.style == "green"


def test_the_console_separates_a_rejection_from_a_refusal() -> None:
    """A rejection IS in the log, at an index, and re-sending it unchanged
    loses the same race -- so it must not read like the refusal above."""
    line = _outcome_line(
        "> OFF_HOLD j",
        {"ok": False, "decision": "rejected", "index": 9, "error": "precondition failed: ..."},
    )
    assert line.plain == "> OFF_HOLD j: rejected @ #9: precondition failed: ..."
    assert line.style == "red"


def test_the_console_does_not_paint_an_undecided_command_as_a_failure() -> None:
    """The one outcome that has not failed. Red would teach the operator to
    press the key again, which is exactly what must not happen while the
    engine may be applying the first press."""
    line = _outcome_line("> KILLJOB j", {"ok": False, "error": "no decision within 5.0s"})
    assert line.style == "yellow"
    assert "NO DECISION" in line.plain and "do not resend" in line.plain
    assert "no decision within 5.0s" in line.plain


def test_the_console_reads_a_lost_round_trip_by_whether_it_was_sent() -> None:
    """A command with no ANSWER has the same two readings as one with an
    answer, and the console used to give it only one: "not sent". That is the
    refusal reading, and it is right for a socket that was never reached and
    wrong for one that hung up after the write -- the engine fsyncs an
    attempt before it feeds it, so the command may already be admitted."""
    never = _transport_line("> ON_HOLD j", ControlClientError("no such file"))
    assert never.plain == "> ON_HOLD j: not sent (no such file)"
    assert never.style == "red"
    lost = _transport_line("> ON_HOLD j", ControlClientError("engine hung up", delivered=True))
    assert "NO DECISION" in lost.plain and "do not resend" in lost.plain
    assert "engine hung up" in lost.plain
    assert lost.style == "yellow"  # matches the undecided ANSWER above, on purpose


# --------------------------------------------------- 1c. echo_label (F9, DL-187 item 9)


def test_echo_label_names_a_job_verb() -> None:
    assert (
        echo_label({"cmd": "sendevent", "verb": "KILLJOB", "payload": {"job": "j1"}})
        == "> KILLJOB j1"
    )


def test_echo_label_names_a_globals_value() -> None:
    assert (
        echo_label(
            {"cmd": "sendevent", "verb": "SET_GLOBAL", "payload": {"name": "FLAG", "value": "go"}}
        )
        == "> SET_GLOBAL FLAG=go"
    )


def test_echo_label_names_change_status_with_an_exit_code() -> None:
    assert (
        echo_label(
            {
                "cmd": "sendevent",
                "verb": "CHANGE_STATUS",
                "payload": {"job": "j1", "status": "FAILURE", "exit_code": 2},
            }
        )
        == "> CHANGE_STATUS j1 FAILURE 2"
    )


def test_echo_label_names_change_status_without_an_exit_code() -> None:
    assert (
        echo_label(
            {
                "cmd": "sendevent",
                "verb": "CHANGE_STATUS",
                "payload": {"job": "j1", "status": "SUCCESS"},
            }
        )
        == "> CHANGE_STATUS j1 SUCCESS"
    )


# ------------------------------------------------------------- 2. ControlClient


def test_control_client_request_round_trip(short_root: Path) -> None:
    text = "insert_job: cc_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            resp = await client.request({"cmd": "status"})
            assert resp["ok"] is True
            assert set(resp["jobs"]) == {"cc_job"}
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_control_client_status_response_over_the_64k_default_readline_limit(
    short_root: Path,
) -> None:
    """LimitOverrunError regression: one `status` response is one JSON line
    covering EVERY job (~220 bytes each), so a ~300-job estate overruns
    asyncio's 64 KiB default readline() buffer -- a default-limit connection
    raises ValueError('Separator is not found, and chunk exceed the limit')
    on the very first TUI refresh. The client must open every connection
    with an explicit limit (LINE_LIMIT)."""
    text = "".join(
        f"insert_job: bulk_{i:04d}\njob_type: c\ncommand: x\nmachine: m1\n\n" for i in range(400)
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            resp = await client.request({"cmd": "status"})
            assert resp["ok"] is True
            assert len(resp["jobs"]) == 400
            # self-check: the fixture really is past the 64 KiB default, so
            # this test regresses if the per-job payload ever shrinks below it
            assert len(json.dumps(resp, sort_keys=True)) > 64 * 1024
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_control_client_reconnects_after_the_server_closes_and_reopens(short_root: Path) -> None:
    """The drop-and-retry contract (module docstring): a transport error
    drops the stashed connection so the NEXT request reconnects. Reproduced
    here by closing the server out from under a live client, then rebinding
    a fresh ControlServer at the same path against the same engine."""
    text = "insert_job: rc_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        client = ControlClient(server.path)
        try:
            first = await client.request({"cmd": "status"})
            assert first["ok"] is True

            await server.close()
            with pytest.raises(ControlClientError):
                await client.request({"cmd": "status"})

            server2 = ControlServer(engine, run_root / "control.sock")
            await server2.start()
            second = await client.request({"cmd": "status"})
            assert second["ok"] is True
            await server2.close()
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_control_client_says_whether_a_failed_request_left_the_process(
    short_root: Path,
) -> None:
    """The async client's half of the S7c split. `_transport_line` above
    renders the distinction; this is where it has to be produced, and it is
    produced at the drain -- everything before it never reached a socket,
    everything after it did.

    The delivered arm is the interesting one and it is not an OSError: the
    write succeeds into a socket whose peer has hung up, and the failure
    arrives as an empty `readline`."""
    never = ControlClient(short_root / "nobody.sock")
    with pytest.raises(ControlClientError) as undelivered:
        asyncio.run(never.request({"cmd": "status"}))
    assert undelivered.value.delivered is False

    async def hang_up_on_one_request() -> ControlClientError:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()  # take the request, answer nothing
            writer.close()

        path = short_root / "hangup.sock"
        server = await asyncio.start_unix_server(handle, str(path))
        client = ControlClient(path)
        try:
            with pytest.raises(ControlClientError) as delivered:
                await client.request({"cmd": "status"})
            return delivered.value
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    exc = asyncio.run(hang_up_on_one_request())
    assert exc.delivered is True
    assert "hung up" in str(exc)


def test_control_client_subscribe_yields_a_record_for_an_injected_event(short_root: Path) -> None:
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            read = await client.request({"cmd": "status", "job": "sub_job"})
            resp = await client.request(
                {
                    "cmd": "sendevent",
                    "baseline_id": read["baseline_id"],
                    "request_id": "tui-sub-1",
                    "verb": "ON_HOLD",
                    "payload": {"job": "sub_job"},
                    "expect": {"job:sub_job": read["jobs"]["sub_job"]["state_rev"]},
                }
            )
            assert resp["ok"] is True

            async def held() -> bool:
                r = await client.request({"cmd": "status", "job": "sub_job"})
                return bool(r["jobs"]["sub_job"]["on_hold"])

            await _wait_for_async(held)

            records = []
            async for record in client.subscribe(since=0):
                records.append(record)
                if record.get("kind") == "ON_HOLD":
                    break
            assert any(
                r.get("kind") == "ON_HOLD" and r.get("payload", {}).get("job") == "sub_job"
                for r in records
            )
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_control_client_subscribe_raises_on_a_refused_subscribe(short_root: Path) -> None:
    """A journal-less Engine (constructed directly, no `journal=`) refuses
    subscribe with "this run has no journal" (ControlServer._subscribe);
    ControlClient.subscribe must surface that refusal as a
    ControlClientError, not hang or swallow it."""
    text = "insert_job: nj_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        run_root.mkdir()
        engine = Engine(
            lower_source(text),
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
        )
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        client = ControlClient(server.path)
        try:
            with pytest.raises(ControlClientError):
                async for _record in client.subscribe():
                    pass
            # the same refusal on the raw socket, with its stable code
            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(b'{"cmd": "subscribe", "v": 5}\n')
                await writer.drain()
                line = json.loads(await asyncio.wait_for(reader.readline(), timeout=5.0))
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
            assert line == {"ok": False, "code": "no_journal", "error": "this run has no journal"}
        finally:
            await client.close()
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            await server.close()
            await engine.shutdown()

    asyncio.run(scenario())


# --------------------------------------------------------- 3. RunnerApp pilot smoke


def test_pilot_jobs_table_shows_every_catalog_job(short_root: Path) -> None:
    text = (
        "insert_job: tp_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: tp_b\njob_type: c\ncommand: y\nmachine: m1\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 2)
                table = app.query_one("#jobs", DataTable)
                assert table.row_count == 2
                assert app._rows == {"tp_a", "tp_b"}
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_s_key_startjob_lands_success_in_the_table(short_root: Path) -> None:
    """Focus the table, move the cursor onto the job's row (setting
    RunnerApp._selected via the RowHighlighted handler), then press "s"
    (action_send('STARTJOB')) -- the DL-46 headless-CLI-equivalent path."""
    text = "insert_job: tp_start\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("tp_start", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tp_start" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "tp_start"

                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("tp_start", "status")) == "SUCCESS"
                )
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_explain_pane_atoms_and_console_change_status_echo(short_root: Path) -> None:
    """One test covering two adjacent bullets: the explain pane's per-atom
    checkmarks (unsatisfied then satisfied), driven by the SAME console
    CHANGE_STATUS submission whose "ok" echo into #console proves the
    console round trip."""
    text = (
        "insert_job: tp_ea\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: tp_eb\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(tp_ea)\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 2)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=1)  # alpha-sorted: tp_ea, tp_eb
                await pilot.pause()
                assert app._selected == "tp_eb"

                pane = app.query_one("#explain", Static)
                await _wait_for_ui(pilot, lambda: str(pane.content))
                assert "✘ s(tp_ea)" in str(pane.content)

                console = app.query_one("#console", RichLog)
                cmdline = app.query_one("#cmdline", Input)
                cmdline.focus()
                cmdline.value = "CHANGE_STATUS tp_ea SUCCESS 0"
                await pilot.press("enter")

                # "applied @ #N": a v2 answer is a DECISION with an index, not
                # a receipt with a timestamp (DL-90)
                await _wait_for_ui(
                    pilot, lambda: any("applied @ #" in ln.text for ln in console.lines)
                )
                await _wait_for_ui(pilot, lambda: "✔ s(tp_ea)" in str(pane.content))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pending_term_run_time_timer_appears_while_running_and_clears_after_terminal(
    short_root: Path,
) -> None:
    """The `timers` column mirrors Oracle.pending_timers()'s own
    stale-by-status rule: non-empty while RUNNING, empty once the job
    leaves RUNNING -- no need to wait out the real deadline itself."""
    text = "insert_job: tp_trt\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # inert: nothing completes on its own
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tp_trt" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()

                await pilot.press("s")
                await _wait_for_ui(
                    pilot,
                    lambda: (
                        str(table.get_cell("tp_trt", "timers")) != ""
                        and str(table.get_cell("tp_trt", "status")) == "RUNNING"
                    ),
                )
                assert "term_run_time" in str(table.get_cell("tp_trt", "timers"))

                cmdline = app.query_one("#cmdline", Input)
                cmdline.focus()
                cmdline.value = "CHANGE_STATUS tp_trt SUCCESS 0"
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("tp_trt", "status")) == "SUCCESS"
                )
                assert str(table.get_cell("tp_trt", "timers")) == ""
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_log_tail_shows_appended_bytes_for_the_selected_job(short_root: Path) -> None:
    """No real process needed (module note): status resolves a CMD job's
    log path once run_number >= 1 regardless of adapter, so a FakeAdapter
    run is enough -- write straight to the resolved path and let the
    0.5s _tail_step poll pick it up."""
    text = "insert_job: tp_log\njob_type: c\ncommand: z\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("tp_log", 1): (3.0, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tp_log" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()

                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("tp_log", "status")) == "RUNNING"
                )
                await _wait_for_ui(pilot, lambda: app._log_paths.get("tp_log") is not None)
                out_path, _err_path = app._log_paths["tp_log"]
                assert out_path is not None
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "ab") as f:
                    f.write(b"hello from the job\n")

                logtail = app.query_one("#logtail", RichLog)
                await _wait_for_ui(pilot, lambda: len(logtail.lines) > 0)
                assert any("hello from the job" in ln.text for ln in logtail.lines)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_d_key_opens_the_spec_popup_and_escape_closes_it(short_root: Path) -> None:
    """DL-64 job-details popup: `d` on the selected row pushes a SpecScreen
    whose body carries the `spec` verb's rendered JIL block plus the status
    facts; escape pops it."""
    text = "insert_job: tp_spec\njob_type: c\ncommand: echo spec\nmachine: m1\n"
    block = "insert_job: tp_spec\njob_type: c\ncommand: echo spec\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", text, spec_texts={"tp_spec": block}
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tp_spec" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()

                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                body = str(app.screen.query_one("#specbox Static", Static).content)
                assert "command: echo spec" in body
                assert "INACTIVE" in body  # the status fact line

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pane_geometry_maximize_toggle_and_share_nudges(short_root: Path) -> None:
    """DL-64 keyboard geometry, DL-67 target: `m` maximizes the log PANE
    (tail + search prompt, so the prompt stays visible while zoomed) and
    hands focus to the pager; `m` again restores. `]` and `}` move their
    split by a fifth, within the limits of DL-520: the log pane, and the
    table column, the split's participants."""
    text = "insert_job: tp_geo\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tp_geo" in app._rows)
                logbox = app.query_one("#logbox", _LogPane)
                logtail = app.query_one("#logtail", _LogTail)

                await pilot.press("m")
                await pilot.pause()
                assert app.screen.maximized is logbox
                assert app.focused is logtail  # focus IS the pager mode
                await pilot.press("m")  # pass-through key: the app toggles
                await pilot.pause()
                assert app.screen.maximized is None

                # 120 columns; 27 rows above the 11 of the console
                await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
                await pilot.press("]")
                assert app._sizes.log == pytest.approx(0.8)
                await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (5, 22))
                await pilot.press("]")  # clamped: the explain pane keeps 3 rows
                await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (3, 24))
                await pilot.press("{")
                await pilot.press("{")
                await pilot.press("{")  # clamped: the table column keeps 20 columns
                # tablecol (the filter+table column) is the horizontal-split
                # participant, not #jobs inside it (DL-65 review)
                await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (20, 100))
                assert app.query_one("#tablecol").styles.width.value == 20
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_f1_and_question_mark_toggle_the_help_panel(short_root: Path) -> None:
    """F6 (DL-187 item 7): F1 mounts Textual's own context help panel and
    F1 again removes it; `?` does the same while the jobs table has focus
    (the pager's own `?` is its reverse search, covered elsewhere)."""
    text = "insert_job: hp_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "hp_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                await pilot.pause()

                def panel_present() -> bool:
                    return bool(app.screen.query(HelpPanel))

                await pilot.press("f1")
                await _wait_for_ui(pilot, panel_present)
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: not panel_present())

                await pilot.press("question_mark")
                await _wait_for_ui(pilot, panel_present)
                await pilot.press("question_mark")
                await _wait_for_ui(pilot, lambda: not panel_present())
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_f1_reaches_the_maximized_pager_and_the_triggers_screen(short_root: Path) -> None:
    """P1-6: help must be reachable from inside a maximized pane and from
    inside a modal, not just the default screen with the table focused --
    a modal chain blocks app bindings (screen.py:449), which is why every
    modal binds F1 to app.toggle_help_panel itself (DL-187 item 7), and
    HelpPanel's `-textual-system` class survives Screen.arrange's maximize
    filter (MINOR 5, checked)."""
    text = "insert_job: hm_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "hm_job" in app._rows)

                def panel_present() -> bool:
                    return bool(app.screen.query(HelpPanel))

                table = app.query_one("#jobs", DataTable)
                table.focus()
                await pilot.pause()
                await pilot.press("m")
                await pilot.pause()
                assert app.screen.maximized is not None  # the log pane, zoomed

                await pilot.press("f1")
                await _wait_for_ui(pilot, panel_present)
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: not panel_present())
                await pilot.press("m")  # restore before opening the modal
                await pilot.pause()

                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                await pilot.press("f1")
                await _wait_for_ui(pilot, panel_present)
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: not panel_present())
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


_KEY_MAX = 2**63 - 1


class _HistoryReplies:
    """Controlled `status` and `transitions` answers (DL-541). `rows` are the stored entries,
    each with its key; a page follows control-protocol ss4: entries in key order, `next` the
    key of its last entry in its direction or the request's cursor, never above
    `[watermark, 2^63 - 1]`, `gaps` the seqs of `gaps` the page spans, `continuous_through`
    by `histories`. `refusal` answers every `transitions` request in place of a page."""

    def __init__(self) -> None:
        self.status: dict[str, Any] = {
            "ok": True,
            "baseline_id": "period-a",
            "engine_now": "2026-07-01T08:00:00",
            "jobs": {"trace_job": {"status": "INACTIVE", "job_type": "CMD"}},
        }
        self.baseline = "period-a"
        self.rows: list[dict] = self.entries("old", 2)
        self.histories: list[tuple[str, int]] = [("history-a", 1)]
        self.floor = 1
        self.gaps: list[list[int]] = []
        #: the key of the newest pruned row a forward cursor below it misses
        self.pruned_through: tuple[int, int] | None = None
        self.watermark: int | None = None
        self.refusal: dict | None = None
        #: the most entries one page holds, as a byte cap would cut it
        self.page_cap: int | None = None
        #: (predicate, event): a request the predicate takes waits for the event
        self.gates: list[tuple[Any, asyncio.Event]] = []
        #: a request this predicate takes raises ControlClientError
        self.fail: Any = None
        #: called with each `transitions` request after its answer is shaped
        self.after: Any = None
        #: whether `status` names the store's history (DL-541), as an engine of this build does
        self.names_history = True
        #: a `continuous_through` every page reports in place of the computed one
        self.through: int | None = None
        #: mangle(request, answer) -> the answer sent, to send one out of the contract's form
        self.mangle: Any = None
        self.requests: list[dict] = []
        self.disconnected = False

    @staticmethod
    def entries(
        cause: str,
        count: int,
        *,
        first: int = 1,
        transition: str = "MUST_START_ALARM",
        at: str = "2026-07-01T08:00:00",
        job: str = "trace_job",
    ) -> list[dict]:
        return [
            {
                "seq": seq,
                "n": 0,
                "at": at,
                "job": job,
                "transition": transition,
                "cause": f"{cause}-{seq}",
            }
            for seq in range(first, first + count)
        ]

    def upto(self) -> int:
        if self.watermark is not None:
            return self.watermark
        return max((row["seq"] for row in self.rows), default=self.floor - 1)

    def transitions(self, request: dict) -> dict:
        upto = self.upto()
        rows = sorted(
            (
                row
                for row in self.rows
                if row["seq"] <= upto
                and request.get("job") in (None, row["job"])
                and request.get("transition") in (None, row["transition"])
            ),
            key=lambda row: (row["seq"], row["n"]),
        )
        cursor = tuple(request["cursor"]) if request.get("cursor") is not None else None
        backward = request.get("direction") == "backward"
        limit = request.get("limit", 500)
        if self.page_cap is not None:
            limit = min(limit, self.page_cap)
        if backward:
            after = [r for r in rows if cursor is None or (r["seq"], r["n"]) < cursor]
            page, more = after[max(0, len(after) - limit) :], len(after) > limit
            last = (page[0]["seq"], page[0]["n"]) if page else None
            high = upto if cursor is None else min(cursor[0], upto)
            low = (high if last is None else last[0]) if more else self.floor
        else:
            after = [r for r in rows if cursor is None or (r["seq"], r["n"]) > cursor]
            page, more = after[:limit], len(after) > limit
            last = (page[-1]["seq"], page[-1]["n"]) if page else None
            low = self.floor if cursor is None else cursor[0]
            high = min((low if last is None else last[0]) if more else upto, upto)
        following = cursor if last is None else last
        if following is not None and following > (upto, _KEY_MAX):
            following = (upto, _KEY_MAX)
        pruned = []
        through = self.pruned_through
        if cursor is not None and not backward and through is not None and cursor < through:
            first, newest = max(cursor[0], 1), min(through[0], upto)
            if first <= newest:
                pruned.append([first, newest])
        elif cursor is not None and backward and cursor[0] < self.floor:
            if 1 <= min(cursor[0], upto):
                pruned.append([1, min(cursor[0], upto)])
        # covered and gaps account for the span at or above the floor, as the engine's do
        start = max(low, self.floor)
        gaps = sorted(
            [max(a, start), min(b, high)] for a, b in self.gaps if max(a, start) <= min(b, high)
        )
        covered, at = [], start
        for a, b in gaps:
            if a > at:
                covered.append([at, a - 1])
            at = max(at, b + 1)
        if at <= high:
            covered.append([at, high])
        answer = {
            "ok": True,
            "baseline_id": self.baseline,
            "applied_index": 0,
            "entries": [dict(row) for row in page],
            "next": None if following is None else list(following),
            "more": more,
            "floor": self.floor,
            "expired": bool(pruned),
            "pruned": pruned,
            "covered": covered,
            "gaps": gaps,
            "watermark": upto,
            "history": self.histories[-1][0],
        }
        if request.get("history") is not None:
            answer["continuous_through"] = agrees_through(self.histories, request["history"], upto)
            if self.through is not None:
                answer["continuous_through"] = self.through
        if self.mangle is not None:
            answer = self.mangle(request, answer)
        return answer

    async def request(self, request):
        self.requests.append(dict(request))
        for taken, gate in list(self.gates):
            if taken(request):
                await gate.wait()
        if self.disconnected or (self.fail is not None and self.fail(request)):
            raise ControlClientError("test disconnect")
        if request["cmd"] == "status":
            if self.names_history and "history" not in self.status:
                return {**self.status, "history": self.histories[-1][0]}
            return self.status
        if request["cmd"] == "transitions":
            if self.refusal is not None:
                return self.refusal
            answer = self.transitions(request)
            if self.after is not None:
                self.after(request)
            return answer
        if request["cmd"] in ("spec", "deps", "timers"):
            return {"ok": True}
        assert request["cmd"] == "explain", request
        return {"ok": True, "condition": None}

    def follows(self) -> list[dict]:
        """The forward `transitions` requests sent, without their history token."""
        return [
            {k: v for k, v in r.items() if k not in ("cmd", "history")}
            for r in self.requests
            if r["cmd"] == "transitions" and r.get("direction") == "forward"
        ]


@contextlib.asynccontextmanager
async def _history_poll_app(short_root: Path, *, size: tuple[int, int] = (160, 40)):
    """Mount the real widgets over `_HistoryReplies`, with polling driven only by each test's r
    key; it yields once the join and its alarm scans are read."""
    replies = _HistoryReplies()
    app = RunnerApp(short_root / "control.sock")
    app._client.request = replies.request
    app._reads.request = replies.request

    async def no_journal_wakeups():
        return

    app._follow_journal = no_journal_wakeups
    set_interval = app.set_interval

    def controlled_interval(interval, callback, **kwargs):
        timer = set_interval(interval, callback, **kwargs)
        if callback == app._poll:
            timer.pause()
        return timer

    app.set_interval = controlled_interval
    async with app.run_test(size=size) as pilot:
        await _wait_for_ui(pilot, lambda: app._join is not None and "trace_job" in app._rows)
        yield app, pilot, replies


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative-shell-characters"])
def test_pilot_spec_drift_advice_clears_and_does_not_repeat_on_reconnect(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    """DL-474: status alone supplies drift; the advice quotes both socket arguments."""
    socket_root = Path("run root's $;") if relative else short_root

    async def scenario() -> None:
        async with _history_poll_app(socket_root, size=(80, 40)) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            notices: list[str] = []
            write = app._console_write

            def record(text):
                notices.append(text.plain)
                write(text)

            monkeypatch.setattr(app, "_console_write", record)
            socket = shlex.quote(str((socket_root / "control.sock").absolute()))
            expected = (
                "spec drift: estate files changed on disk; run dsl41 reread"
                f" -S {socket} to read them, then the dsl41 update command it prints to adopt"
            )
            badge = "[SPEC DRIFT: estate files changed on disk]"
            assert badge not in app.sub_title

            replies.status["spec_drift"] = True
            await pilot.press("r")
            await pilot.pause()
            assert notices.count(expected) == 1
            rendered = re.sub(r"\s+", "", "".join(line.text for line in console.lines))
            assert re.sub(r"\s+", "", expected) in rendered
            assert badge in app.sub_title

            await pilot.press("r")
            await pilot.pause()
            assert notices.count(expected) == 1

            replies.disconnected = True
            await pilot.press("r")
            await pilot.pause()
            assert app._connected is False
            replies.disconnected = False
            await pilot.press("r")
            await pilot.pause()
            assert app._connected is True
            assert notices.count(expected) == 1

            for cleared in (False, None):
                replies.status["spec_drift"] = cleared
                await pilot.press("r")
                await pilot.pause()
                assert badge not in app.sub_title
                replies.status["spec_drift"] = True
                await pilot.press("r")
                await pilot.pause()
                assert badge in app.sub_title
            assert notices.count(expected) == 3
            assert {request["cmd"] for request in replies.requests} <= {
                "status",
                "transitions",
                "explain",
            }

    asyncio.run(scenario())


def test_dl210_u1_u4_trace_baseline_change_restarts_a_longer_trace_next_poll(
    short_root: Path,
) -> None:
    """First sight seeds identity. A status answer under a new baseline forgets the held
    history before the table is drawn, and the same pass joins the new history (DL-541)."""

    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            # u4: mounting joined once, without a reset pass or notice.
            joins = [r for r in replies.requests if r["cmd"] == "transitions" and "cursor" not in r]
            assert joins == [{"cmd": "transitions", "direction": "backward", "limit": 500}]
            assert app._alarm_count("trace_job") == 2
            assert not any("baseline changed" in line.text for line in console.lines)
            before = len(console.lines)
            replies.requests.clear()
            replies.status.update(baseline_id="period-b", jobs={"new_job": {"status": "SUCCESS"}})
            replies.baseline = "period-b"
            replies.histories = [("history-b", 1)]
            replies.rows = replies.entries("new", 4)

            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._join == (4, _KEY_MAX))
            scan = {"direction": "backward", "cursor": [4, _KEY_MAX], "limit": 500}
            assert replies.requests == [
                {"cmd": "status"},
                {"cmd": "transitions", "direction": "backward", "limit": 500},
                {
                    "cmd": "transitions",
                    "transition": "MUST_COMPLETE_ALARM",
                    **scan,
                    "history": "history-b",
                },
                {
                    "cmd": "transitions",
                    "transition": "MUST_START_ALARM",
                    **scan,
                    "history": "history-b",
                },
            ]
            assert app._rows == {"new_job"} and app._baseline == "period-b"
            assert app._alarm_count("trace_job") == 4
            lines = [line.text for line in console.lines]
            assert lines[before] == "baseline changed; joining the history again"
            assert sum("[new-" in text for text in lines) == 4
            assert sum("baseline changed" in text for text in lines) == 1

    asyncio.run(scenario())


_SCHEDULER_REFUSAL = "already RUNNING -- concurrent or repeated start request, no effect (STARTJOB event (scheduler))"


def _start_refusal(
    seq: int,
    job: str = "trace_job",
    cause: str = _SCHEDULER_REFUSAL,
    source: str | None = "scheduler",
) -> dict:
    return {
        "seq": seq,
        "n": 0,
        "at": (datetime(2026, 7, 1, 23, 30) + timedelta(seconds=seq)).isoformat(),
        "job": job,
        "transition": "START_REFUSED",
        "cause": cause,
        **(
            {"start_source": source, "run_number": 1, "refused_status": "RUNNING"}
            if source is not None
            else {}
        ),
    }


def _opening(seq: int, run: int = 1, job: str = "trace_job", n: int = 0) -> dict:
    """The stored status entry that opens run `run` of `job` (DL-537)."""
    return {
        "seq": seq,
        "n": n,
        "at": f"2026-07-01T23:29:{seq:02}",
        "job": job,
        "transition": "INACTIVE->STARTING",
        "cause": "STARTJOB event (scheduler)",
        "run_number": run,
    }


def _console_text(console: RichLog) -> str:
    return re.sub(r"\s+", "", "".join(line.text for line in console.lines))


def test_pilot_identical_refusal_after_a_different_entry_prints_a_new_first_line(
    short_root: Path, local_timezone
) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, _):
            console = app.query_one("#console", RichLog)
            app._consume_entries(
                [
                    _start_refusal(3),
                    _start_refusal(4),
                    {
                        "seq": 5,
                        "at": "2026-07-01T23:30:05",
                        "job": "trace_job",
                        "transition": "MUST_COMPLETE_ALARM",
                        "cause": "deadline",
                    },
                    _start_refusal(6),
                ],
                follow=True,
            )
            await pilot.pause()
            summary = (
                "2026-07-02T01:30:05 trace_job START_REFUSED repeated 1 more time,"
                " last 2026-07-02T01:30:04"
            )
            alarm = "2026-07-02T01:30:05 trace_job MUST_COMPLETE_ALARM [deadline]"
            new_first = f"2026-07-02T01:30:06 trace_job START_REFUSED [{_SCHEDULER_REFUSAL}]"
            rendered = _console_text(console)
            assert re.sub(r"\s+", "", summary + alarm + new_first) in rendered
            assert rendered.count(re.sub(r"\s+", "", f"[{_SCHEDULER_REFUSAL}]")) == 2
            assert app._refusal_runs["trace_job"].repeats == 0

    asyncio.run(scenario())


def test_pilot_interleaved_jobs_keep_separate_refusal_runs(
    short_root: Path, local_timezone
) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            changed_cause = _SCHEDULER_REFUSAL.replace("RUNNING", "QUE_WAIT")
            replies.rows.extend(
                [
                    _start_refusal(3, "job_a"),
                    _start_refusal(4, "job_b"),
                    _start_refusal(5, "job_a"),
                    _start_refusal(6, "job_b"),
                    _start_refusal(7, "job_b"),
                    _start_refusal(8, "job_a", changed_cause),
                    _start_refusal(9, "job_b", changed_cause),
                    _start_refusal(10, "job_a", changed_cause),
                ],
            )
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (10, _KEY_MAX))
            await pilot.pause()
            rendered = _console_text(console)
            for job, count_text, at, last in (
                ("job_a", "1 more time", "08", "05"),
                ("job_b", "2 more times", "09", "07"),
            ):
                summary = (
                    f"2026-07-02T01:30:{at} {job} START_REFUSED repeated {count_text},"
                    f" last 2026-07-02T01:30:{last}"
                )
                following = f"2026-07-02T01:30:{at} {job} START_REFUSED [{changed_cause}]"
                assert re.sub(r"\s+", "", summary + following) in rendered
            assert rendered.count("START_REFUSED") == 6

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cause, source",
    [
        (_SCHEDULER_REFUSAL.replace("scheduler", "control"), "control"),
        ("unknown refusal origin", None),
        (
            "re-trigger loop inside one instant: started twice in one nested cascade; refused (L010)",
            None,
        ),
        (_SCHEDULER_REFUSAL, None),
        (_SCHEDULER_REFUSAL, "control"),
    ],
    ids=[
        "operator",
        "unknown",
        "bounded-loop",
        "legacy-scheduler-text",
        "misleading-scheduler-text",
    ],
)
def test_pilot_operator_and_unknown_refusals_print_every_time(
    short_root: Path, local_timezone, cause: str, source: str | None
) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            replies.rows.extend(
                [
                    _opening(2, n=1),
                    _start_refusal(3),
                    _start_refusal(4),
                    _start_refusal(5, cause=cause, source=source),
                    _start_refusal(6, cause=cause, source=source),
                ],
            )
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (6, _KEY_MAX))
            await pilot.pause()
            rendered = _console_text(console)
            summary = (
                "2026-07-02T01:30:05 trace_job START_REFUSED repeated 1 more time,"
                " last 2026-07-02T01:30:04"
            )
            first_operator = f"2026-07-02T01:30:05 trace_job START_REFUSED [{cause}]"
            assert re.sub(r"\s+", "", summary + first_operator) in rendered
            assert rendered.count(re.sub(r"\s+", "", f"[{cause}]")) == (
                3 if cause == _SCHEDULER_REFUSAL else 2
            )
            assert rendered.count("repeated1moretime,") == 1
            assert app._refusal_runs == {}
            # only an explicit scheduler origin counts towards the run's stored total (DL-541)
            row = {"status": "RUNNING", "run_number": 1}
            assert await app._refusal_total("trace_job", row, "period-a", app._history) == (2, True)

    asyncio.run(scenario())


def test_pilot_reconnect_preserves_refusal_summary_without_recounting(
    short_root: Path, local_timezone
) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            replies.rows.append(_opening(2, n=1))
            replies.rows.extend(_start_refusal(seq) for seq in (3, 4, 5))
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (5, _KEY_MAX))
            replies.disconnected = True
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._connected is False)
            replies.disconnected = False
            # A new cause flushes the run; the rows already read are not read again.
            replies.rows.append(_start_refusal(6, cause="operator", source="control"))
            replies.requests.clear()
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (6, _KEY_MAX))
            await pilot.press("r")
            await pilot.pause()
            assert [r["cursor"] for r in replies.follows()] == [[5, _KEY_MAX], [6, _KEY_MAX]]
            rendered = _console_text(console)
            assert rendered.count("repeated2moretimes") == 1
            assert rendered.count("START_REFUSED") == 3
            row = {"status": "RUNNING", "run_number": 1}
            assert await app._refusal_total("trace_job", row, "period-a", app._history) == (3, True)

    asyncio.run(scenario())


@pytest.mark.parametrize("job_type", ["CMD", "BOX"])
def test_pilot_details_count_current_run_and_keep_queued_refusals_apart(
    short_root: Path, job_type: str
) -> None:
    """The popup reads the run's stored refusals when it opens (DL-541): the current run's,
    the queued start's apart, and the same total after an engine restart."""

    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            row = replies.status["jobs"]["trace_job"]
            row.update(status="RUNNING", run_number=1, job_type=job_type, started_by="schedule")

            async def details(expected: str | None) -> None:
                app.query_one("#jobs", DataTable).focus()
                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                await _wait_for_ui(
                    pilot,
                    lambda: (
                        not [
                            w
                            for w in app.workers
                            if w.group in ("details", "refusals") and not w.is_finished
                        ]
                    ),
                )
                await pilot.pause()
                body = str(app.screen.query_one("#specbox Static", Static).content)
                if expected is None:
                    assert "scheduler refusals:" not in body
                else:
                    assert expected in body
                    assert body.index("started by:") < body.index("scheduler refusals:")
                await pilot.press("escape")

            replies.rows.append(_opening(2, n=1))
            await pilot.press("r")
            await details("scheduler refusals: 0 (run 1)\n")  # exact, never a removed line
            replies.rows.extend(
                [
                    _start_refusal(3, cause="opaque cause"),
                    {**_start_refusal(4), "refused_status": "STARTING"},
                    {**_start_refusal(5), "refused_status": "QUE_WAIT"},
                    _start_refusal(6, job="another_job"),
                    _start_refusal(7, source="control"),
                ]
            )
            await details("scheduler refusals: 2 (run 1)")
            row["status"] = "QUE_WAIT"
            await details("scheduler refusals: 1 (queued)")
            row["status"] = "SUCCESS"
            await details("scheduler refusals: 2 (run 1)")
            row.update(status="RUNNING", run_number=2)
            replies.rows.append(_opening(7, run=2, n=1))
            await details("scheduler refusals: 0 (run 2)\n")
            replies.rows.append({**_start_refusal(8), "run_number": 2})
            await details("scheduler refusals: 1 (run 2)")
            # an engine restart: the popup shows the run's stored total, with no qualifier
            replies.histories.append(("history-b", 9))
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._history == "history-b")
            await details("scheduler refusals: 1 (run 2)\n")
            reads = [r for r in replies.requests if r["cmd"] == "transitions" and r.get("job")]
            # the walk's first page carries the history the viewer holds (DL-541)
            assert reads[-1] == {
                "cmd": "transitions",
                "job": "trace_job",
                "direction": "backward",
                "limit": 500,
                "history": "history-b",
            }

    asyncio.run(scenario())


def test_dl210_u3_mixed_read_baselines_forget_the_history_and_draw_the_table(
    short_root: Path,
) -> None:
    """A status answer under another read header than the held history forgets it, alarm
    times included; the table is drawn from status; a join whose page comes under the old
    header is not taken (DL-541)."""

    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            before = len(console.lines)
            replies.requests.clear()
            replies.status.update(baseline_id="period-b", jobs={"mixed_job": {"status": "SUCCESS"}})
            replies.rows.extend(replies.entries("mixed", 1, first=3))

            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: "mixed_job" in app._rows)
            await pilot.pause()
            assert [r for r in replies.requests if r["cmd"] == "transitions"] == [
                {"cmd": "transitions", "direction": "backward", "limit": 500}
            ]
            assert app._join is None and app._alarm_times == {}
            assert app._baseline == "period-b" and app._rows == {"mixed_job"}
            assert len(console.lines) == before + 1
            assert not any("[mixed-" in line.text for line in console.lines)

    asyncio.run(scenario())


def test_dl210_u5_reconnect_same_baseline_keeps_cursor_across_a_resume(short_root: Path) -> None:
    """Disconnect preserves alarms and cursor; the next pass follows on from the cursor."""

    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            console = app.query_one("#console", RichLog)
            replies.disconnected = True
            await pilot.press("r")
            await pilot.pause()
            assert app._connected is False
            assert app._cursor == (2, _KEY_MAX) and app._alarm_count("trace_job") == 2

            replies.disconnected = False
            replies.requests.clear()
            replies.rows.extend(replies.entries("old", 1, first=3))
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (3, _KEY_MAX))
            assert [r["cursor"] for r in replies.follows()] == [[2, _KEY_MAX]]
            assert app._alarm_count("trace_job") == 3
            assert sum("[old-" in line.text for line in console.lines) == 3
            assert not any("baseline changed" in line.text for line in console.lines)

    asyncio.run(scenario())


def test_dl210_headerless_transitions_refusal_preserves_cursor(short_root: Path) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root) as (app, pilot, replies):
            replies.refusal = {"ok": False, "error": "lineage proof lost"}
            await pilot.press("r")
            await pilot.pause()
            assert app._cursor == (2, _KEY_MAX) and app._alarm_count("trace_job") == 2
            console = app.query_one("#console", RichLog)
            assert any("transitions query refused" in line.text for line in console.lines)
            assert not any("baseline changed" in line.text for line in console.lines)

    asyncio.run(scenario())


def test_pilot_a_timers_refusal_does_not_erase_a_live_status_refusal(short_root: Path) -> None:
    """Regression (review MAJOR): a single shared fault slot let one verb's
    recovery erase another's still-live refusal. The keyed `_query_faults`
    memo (P1-1) tracks status/trace/timers independently -- a good
    `timers` answer while `status` is still refusing must not clear the
    app's "(refused)" subtitle or write a false "status query ok again"."""
    text = "insert_job: xq_run\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING: a real dated row
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "xq_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("xq_run", "status")) == "RUNNING"
                )

                inner = app._client.request

                async def refuse_status(request):
                    if request.get("cmd") == "status":
                        return {"ok": False, "error": "status down"}
                    return await inner(request)

                app._client.request = refuse_status
                await app._refresh()
                await _wait_for_ui(pilot, lambda: app._query_faults.get("status") == "status down")
                assert "(refused)" in app.sub_title

                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                screen = app.screen
                assert isinstance(screen, TriggersScreen)
                await screen._refresh()  # timers is NOT refused -- unlike status
                await pilot.pause()

                # the still-live status refusal must survive an unrelated
                # verb's successful, independent query
                assert app._query_faults.get("status") == "status down"
                assert "timers" not in app._query_faults
                assert "(refused)" in app.sub_title
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------- 4. DL-65 estate navigation (box tree / filter / views / details)


#: a nested box estate: bt_box holds bt_inner (which holds bt_deep), bt_m1 and bt_m2
_BOX_TREE_JIL = (
    "insert_job: bt_box\njob_type: b\n\n"
    "insert_job: bt_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_box\n\n"
    "insert_job: bt_m2\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_box\n\n"
    "insert_job: bt_inner\njob_type: b\nbox_name: bt_box\n\n"
    "insert_job: bt_deep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_inner\n\n"
    "insert_job: bt_solo\njob_type: c\ncommand: x\nmachine: m1\n"
)
_BOX_TREE_ORDER = ["bt_box", "bt_inner", "bt_deep", "bt_m1", "bt_m2", "bt_solo"]


def test_pilot_box_tree_order_indent_fold_and_fold_all(short_root: Path) -> None:
    """DL-65 box tree: rows follow the box hierarchy (alpha within each
    level), members indent under their box, space folds the SELECTED box
    (members leave the table, the label gains the hidden-descendant count),
    space again unfolds, and `z` folds/unfolds every box at once."""
    text = _BOX_TREE_JIL
    tree_order = _BOX_TREE_ORDER

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                table = app.query_one("#jobs", DataTable)
                assert str(table.get_cell("bt_box", "job")) == "▾ bt_box"
                assert str(table.get_cell("bt_inner", "job")) == "  ▾ bt_inner"
                assert str(table.get_cell("bt_deep", "job")) == "    bt_deep"
                assert str(table.get_cell("bt_m1", "job")) == "  bt_m1"
                assert str(table.get_cell("bt_solo", "job")) == "bt_solo"

                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "bt_box"

                await pilot.press("space")  # fold: the whole subtree buries
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["bt_box", "bt_solo"])
                assert str(table.get_cell("bt_box", "job")) == "▸ bt_box (4)"

                await pilot.press("space")  # unfold restores the same order
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                assert str(table.get_cell("bt_box", "job")) == "▾ bt_box"

                await pilot.press("z")  # fold ALL boxes
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["bt_box", "bt_solo"])
                await pilot.press("z")  # unfold all
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


async def _click_label(pilot, row: int, char: int, scroll_x: int = 0, button: int = 1) -> None:
    """Click character `char` of the job label on table row `row`. The table's border and the
    cell padding put character 0 two columns in; the border and the header put row 0 two lines
    down."""
    await pilot.click("#jobs", offset=(2 + char - scroll_x, 2 + row), button=button)
    await pilot.pause()


@contextlib.asynccontextmanager
async def _box_tree_app(short_root: Path):
    """The nested box estate on the real app, its tree shown, the table focused on row 0, and
    `action_details` counted instead of run, so a click that would open details is seen at
    once."""
    engine, server, loop_task = await _serve(
        short_root / "run", _BOX_TREE_JIL, spec_texts={"bt_box": "insert_job: bt_box\n"}
    )
    try:
        app = RunnerApp(server.path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _BOX_TREE_ORDER)
            table = app.query_one("#jobs", _JobsTable)
            table.focus()
            table.move_cursor(row=0)
            await _wait_for_ui(pilot, lambda: app._selected == "bt_box")
            details: list[str | None] = []
            app.action_details = lambda: details.append(app._selected)
            yield app, pilot, table, details
    finally:
        await _teardown(engine, server, loop_task)


def test_pilot_dl512_a_click_on_a_box_marker_folds_and_unfolds_it(short_root: Path) -> None:
    """DL-512: a click on a top-level box's marker folds it as space does, a second click
    unfolds it. The clicked row is the selected one, and no details open, though the row was
    already selected."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            stack = list(app.screen_stack)
            await _click_label(pilot, 0, 0)  # "▾ bt_box": the marker
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["bt_box", "bt_solo"])
            assert str(table.get_cell("bt_box", "job")) == "▸ bt_box (4)"
            assert app._selected == "bt_box" and table.cursor_row == 0

            await _click_label(pilot, 0, 1)  # the space after the marker belongs to it
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _BOX_TREE_ORDER)
            assert str(table.get_cell("bt_box", "job")) == "▾ bt_box"
            assert app._selected == "bt_box" and table.cursor_row == 0
            await pilot.pause()
            assert details == [] and list(app.screen_stack) == stack

    asyncio.run(scenario())


def test_pilot_dl512_a_click_left_of_a_nested_box_name_folds_that_box(short_root: Path) -> None:
    """DL-512: the indent and the marker of a nested box fold that box, not its parent, and
    select its row."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            folded = ["bt_box", "bt_inner", "bt_m1", "bt_m2", "bt_solo"]
            await _click_label(pilot, 1, 0)  # "  ▾ bt_inner": the indent
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == folded)
            assert app._collapsed == {"bt_inner"}
            assert app._selected == "bt_inner" and table.cursor_row == 1
            assert str(table.get_cell("bt_inner", "job")) == "  ▸ bt_inner (1)"

            await _click_label(pilot, 1, 2)  # the marker
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _BOX_TREE_ORDER)
            assert app._collapsed == set() and app._selected == "bt_inner"
            await pilot.pause()
            assert details == []

    asyncio.run(scenario())


def test_pilot_dl512_a_click_on_a_box_name_selects_and_does_not_fold(short_root: Path) -> None:
    """DL-512: a click on a box's name selects it as before; a click on the selected box's name
    opens details as before. Neither folds."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _click_label(pilot, 1, 4)  # "  ▾ bt_inner": the name's first letter
            await _wait_for_ui(pilot, lambda: app._selected == "bt_inner")
            assert details == [] and app._collapsed == set()

            await _click_label(pilot, 1, 4)
            await _wait_for_ui(pilot, lambda: details == ["bt_inner"])
            assert app._collapsed == set()
            assert app._table_sync.identity == _BOX_TREE_ORDER

    asyncio.run(scenario())


def test_pilot_dl512_a_click_left_of_a_job_name_folds_nothing(short_root: Path) -> None:
    """DL-512: the indent of a job that is no box is no fold target. The click selects the row
    as before, and a second click there opens details as before, so no fold tag leaked onto
    the row."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _click_label(pilot, 3, 0)  # "  bt_m1": the indent
            await _wait_for_ui(pilot, lambda: app._selected == "bt_m1")
            await pilot.pause()
            assert app._collapsed == set() and details == []
            await _click_label(pilot, 3, 0)
            await _wait_for_ui(pilot, lambda: details == ["bt_m1"])
            assert app._collapsed == set()
            assert app._table_sync.identity == _BOX_TREE_ORDER

    asyncio.run(scenario())


def test_pilot_dl512_under_a_filter_a_first_column_click_folds_nothing(short_root: Path) -> None:
    """DL-512: a flat view draws no marker, so a click at a box's first characters selects the
    row and folds nothing, and a second click there opens details as before."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await pilot.press("slash", "b", "t", "enter")
            await _wait_for_ui(pilot, lambda: app._filter == "bt" and table.has_focus)
            flat = sorted(_BOX_TREE_ORDER)
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == flat)
            row = flat.index("bt_inner")
            await _click_label(pilot, row, 0)
            await _wait_for_ui(pilot, lambda: app._selected == "bt_inner")
            await pilot.press("z")  # a flat view folds nothing from the keys either
            await pilot.pause()
            assert app._collapsed == set() and details == []
            await _click_label(pilot, row, 0)
            await _wait_for_ui(pilot, lambda: details == ["bt_inner"])
            assert app._collapsed == set()
            assert app._table_sync.identity == flat

    asyncio.run(scenario())


def test_pilot_dl512_the_marker_answers_with_the_table_scrolled(short_root: Path) -> None:
    """DL-512: the hit area moves with the table's horizontal scroll, since it is the drawn
    span itself."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            assert table.max_scroll_x >= 3, "the table must scroll at this size"
            table.scroll_to(x=3, animate=False)
            await _wait_for_ui(pilot, lambda: table.scroll_x == 3)
            await _click_label(pilot, 1, 2, scroll_x=3)  # "  ▾ bt_inner": its marker
            await _wait_for_ui(pilot, lambda: app._collapsed == {"bt_inner"})
            assert app._selected == "bt_inner"
            await pilot.pause()
            assert details == []

    asyncio.run(scenario())


def test_pilot_dl512_a_right_click_on_a_marker_folds_nothing(short_root: Path) -> None:
    """DL-512: only the first button folds. A right click on a marker goes to the table as
    before: it selects the row, and on the selected row it opens details."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _click_label(pilot, 1, 2, button=3)  # "  ▾ bt_inner": the marker
            await _wait_for_ui(pilot, lambda: app._selected == "bt_inner")
            await pilot.pause()
            assert app._collapsed == set() and details == []
            await _click_label(pilot, 1, 2, button=3)
            await _wait_for_ui(pilot, lambda: details == ["bt_inner"])
            assert app._collapsed == set()
            assert app._table_sync.identity == _BOX_TREE_ORDER

    asyncio.run(scenario())


def test_pilot_dl512_the_lower_border_names_the_fold_keys(short_root: Path) -> None:
    """DL-512: the jobs table's lower border reads `space fold · z fold all` on a box with
    members, `z fold all` on another row of a tree with such a box, and nothing in a flat view
    or in a tree without one. It follows the cursor, a filter set and cleared, and fold all."""
    both, z_only = "space fold · z fold all", "z fold all"

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, _details):

            def hint() -> str:
                return table.border_subtitle or ""

            await _wait_for_ui(pilot, lambda: hint() == both)
            table.move_cursor(row=_BOX_TREE_ORDER.index("bt_m1"))  # a job
            await _wait_for_ui(pilot, lambda: hint() == z_only)
            table.move_cursor(row=_BOX_TREE_ORDER.index("bt_inner"))  # a nested box
            await _wait_for_ui(pilot, lambda: hint() == both)

            await pilot.press("slash", "b", "t", "enter")
            await _wait_for_ui(pilot, lambda: app._filter == "bt" and table.has_focus)
            await _wait_for_ui(pilot, lambda: hint() == "")
            await pilot.press("slash", "escape")
            await _wait_for_ui(pilot, lambda: app._filter == "" and table.has_focus)
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _BOX_TREE_ORDER)
            await _wait_for_ui(pilot, lambda: hint() == both)

            await pilot.press("v")  # the problems view is flat
            await _wait_for_ui(pilot, lambda: hint() == "")
            await pilot.press("v", "v")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _BOX_TREE_ORDER)

            table.move_cursor(row=_BOX_TREE_ORDER.index("bt_solo"))
            await _wait_for_ui(pilot, lambda: hint() == z_only)
            await pilot.press("z")  # fold all from a job's row, as before
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["bt_box", "bt_solo"])
            assert app._selected == "bt_solo" and hint() == z_only
            await pilot.press("space")  # on a job, space folds nothing, as before
            await pilot.pause()
            assert app._collapsed == {"bt_box", "bt_inner"}
            table.move_cursor(row=0)  # the folded box still has members
            await _wait_for_ui(pilot, lambda: hint() == both)

    asyncio.run(scenario())

    async def no_box() -> None:
        text = (
            "insert_job: nb_box\njob_type: b\n\n"
            "insert_job: nb_job\njob_type: c\ncommand: x\nmachine: m1\n"
        )
        engine, server, loop_task = await _serve(short_root / "run-nb", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 2 and app._selected)
                table = app.query_one("#jobs", DataTable)
                for row in (0, 1):  # an empty box and a job: nothing to fold
                    table.move_cursor(row=row)
                    await pilot.pause()
                    assert (table.border_subtitle or "") == ""
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(no_box())


# ------------------------------------------------- DL-532: the selection is a job

_SEL_JIL = (
    "insert_job: sel_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: sel_box\njob_type: b\n\n"
    "insert_job: sel_inner\njob_type: b\nbox_name: sel_box\n\n"
    "insert_job: sel_deep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: sel_inner\n\n"
    "insert_job: sel_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: sel_box\n\n"
    "insert_job: sel_z\njob_type: c\ncommand: x\nmachine: m1\n"
)
_SEL_ORDER = ["sel_a", "sel_box", "sel_inner", "sel_deep", "sel_m1", "sel_z"]
#: `=sel_[az]` shows these two, flat, and hides every other job
_SEL_FILTER = "=sel_[az]"
_SEL_FILTERED = ["sel_a", "sel_z"]
#: the table's verb keys and their verbs, read from its bindings
_SEND_KEYS = [
    (b.key, b.action.removeprefix("app.send('").removesuffix("')"))
    for b in _JobsTable.BINDINGS
    if isinstance(b, Binding) and b.action.startswith("app.send(")
]


@contextlib.asynccontextmanager
async def _selection_app(short_root: Path, text: str = _SEL_JIL, order: list[str] = _SEL_ORDER):
    """The DL-532 estate on the real app with the table focused. Every request goes through
    the real socket; `seen` lists each one, and the last of `edits`, when set, rewrites the
    jobs of each status answer, the shape an update that adds or removes a job leaves."""
    engine, server, loop_task = await _serve(short_root / "run", text)
    try:
        app = RunnerApp(server.path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == order)
            table = app.query_one("#jobs", _JobsTable)
            table.focus()
            seen: list[dict] = []
            edits: list = []
            inner = app._client.request

            async def capture(request):
                seen.append(request)
                response = await inner(request)
                if request.get("cmd") == "status" and edits and response.get("ok"):
                    response = {**response, "jobs": edits[-1](dict(response["jobs"]))}
                return response

            app._client.request = capture
            yield app, pilot, table, server, seen, edits
    finally:
        await _teardown(engine, server, loop_task)


def _sent(seen: list[dict]) -> list[dict]:
    return [r for r in seen if r.get("cmd") == "sendevent"]


async def _pick(pilot, app: RunnerApp, table: DataTable, job: str) -> None:
    """The operator moves the cursor onto `job`'s row."""
    table.move_cursor(row=app._table_sync.identity.index(job))
    await _wait_for_ui(pilot, lambda: app._selected == job)


async def _held_jobs(server: ControlServer) -> set[str]:
    """The jobs on hold, read through the control socket."""
    client = ControlClient(server.path)
    try:
        jobs = (await client.request({"cmd": "status"}))["jobs"]
    finally:
        await client.close()
    return {name for name, row in jobs.items() if row.get("on_hold")}


def _lines(app: RunnerApp) -> list[str]:
    return [line.text for line in app.query_one("#console", RichLog).lines]


def _row_background(table: DataTable, row: int) -> Any:
    """The background the table paints on its row `row` (the header is line 0)."""
    return next(iter(table.render_line(1 + row))).style.bgcolor


async def _filter(pilot, app: RunnerApp, text: str, rows: list[str]) -> None:
    await pilot.press("slash", *text, "enter")
    await _wait_for_ui(pilot, lambda: app._table_sync.identity == rows)


async def _clear_filter(pilot, app: RunnerApp) -> None:
    await pilot.press("slash")
    await pilot.pause()
    await pilot.press("escape")
    await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SEL_ORDER)


def test_pilot_dl532_the_first_answer_selects_the_first_row(short_root: Path) -> None:
    """DL-532 keeps the first load as it was: with nothing ever selected, the first row of the
    first answer is selected, and its explain pane and log tail are shown."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            assert app._selected == "sel_a" and table.cursor_row == 0
            assert not table.has_class("-resting")
            await _wait_for_ui(
                pilot, lambda: app.query_one("#explain-box").border_title == "explain: sel_a"
            )

    asyncio.run(scenario())


def test_pilot_dl532_fold_all_selects_the_enclosing_box_and_h_holds_it(short_root: Path) -> None:
    """DL-532: `z` hides the selected member, so its box, the row that now stands for it, is
    selected and the console says so in one line. `h` holds that box, not row 0's job. `z`
    again shows the member, and it is selected again."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, server, seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await pilot.press("z")
            await _wait_for_ui(
                pilot, lambda: app._table_sync.identity == ["sel_a", "sel_box", "sel_z"]
            )
            assert app._selected == "sel_box" and table.cursor_row == 1
            assert _lines(app).count("sel_box is selected; the fold hides sel_m1") == 1
            await app._refresh()  # a refresh that leaves the same box in its place keeps it
            assert app._selected == "sel_box"

            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["sel_box"]["on_hold"])
            assert [r["payload"] for r in _sent(seen)] == [{"job": "sel_box"}]
            assert await _held_jobs(server) == {"sel_box"}

            await pilot.press("z")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SEL_ORDER)
            assert app._selected == "sel_m1" and table.cursor_row == 4

    asyncio.run(scenario())


def test_pilot_dl532_a_nested_fold_selects_the_outermost_folded_box_shown(
    short_root: Path,
) -> None:
    """DL-532: a member of a box inside a box. `z` folds both, so the outer box stands for it.
    Space unfolds the outer box and the inner box, still folded, stands for it; space again
    shows the member, and it is selected again. Each move is said in one console line."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, server, seen, _edits):
            await _pick(pilot, app, table, "sel_deep")
            await pilot.press("z")
            await _wait_for_ui(
                pilot, lambda: app._table_sync.identity == ["sel_a", "sel_box", "sel_z"]
            )
            assert app._selected == "sel_box" and table.cursor_row == 1
            assert _lines(app).count("sel_box is selected; the fold hides sel_deep") == 1

            await pilot.press("space")
            await _wait_for_ui(
                pilot,
                lambda: (
                    app._table_sync.identity == ["sel_a", "sel_box", "sel_inner", "sel_m1", "sel_z"]
                ),
            )
            assert app._selected == "sel_inner" and table.cursor_row == 2
            assert _lines(app).count("sel_inner is selected; the fold hides sel_deep") == 1

            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["sel_inner"]["on_hold"])
            assert await _held_jobs(server) == {"sel_inner"}

            await pilot.press("space")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SEL_ORDER)
            assert app._selected == "sel_deep" and table.cursor_row == 3
            assert [r["payload"] for r in _sent(seen)] == [{"job": "sel_inner"}]

    asyncio.run(scenario())


def test_pilot_dl532_a_filter_that_hides_the_selection_leaves_nothing_selected(
    short_root: Path,
) -> None:
    """DL-532: a filter hides the selected job, so nothing is selected. The cursor rests on a
    row painted as any other row. Every verb key on the table, `d`, and a console verb without
    a job say "no job selected" and send nothing; a console verb that names a job is sent."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, server, seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            assert app.focused is table
            assert app._selected is None and table.has_class("-resting")
            assert _row_background(table, table.cursor_row) == _row_background(table, 1)
            await _wait_for_ui(
                pilot, lambda: app.query_one("#explain-box").border_title == "explain"
            )

            assert len(_SEND_KEYS) == 9
            for key, verb in _SEND_KEYS:
                await pilot.press(key)
                await pilot.pause()
                assert _lines(app)[-1] == f"{verb}: no job selected"
                assert not isinstance(app.screen, ConfirmScreen)
            await pilot.press("d")
            await pilot.pause()
            assert _lines(app)[-1] == "details: no job selected"
            assert not isinstance(app.screen, SpecScreen)

            await pilot.press("colon", *"ON_HOLD", "enter")
            await pilot.pause()
            assert _lines(app)[-1] == "ON_HOLD: no job selected"
            await pilot.press(*"CHANGE_STATUS SUCCESS", "enter")
            await pilot.pause()
            assert _lines(app)[-1] == "CHANGE_STATUS: no job selected"
            assert _sent(seen) == [] and not [w for w in app.workers if w.group == "send"]

            await pilot.press(*"ON_HOLD sel_z", "enter")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["sel_z"]["on_hold"])
            assert [r["payload"] for r in _sent(seen)] == [{"job": "sel_z"}]
            assert await _held_jobs(server) == {"sel_z"}
            assert app._selected is None

    asyncio.run(scenario())


def test_pilot_dl532_a_cleared_filter_selects_the_hidden_job_again(short_root: Path) -> None:
    """DL-532: clearing the filter selects the hidden job again. Under a filter, Enter on the
    resting row picks it without opening details and down moves on; once the operator picked
    a job there, clearing the filter keeps that newer selection."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            assert app._selected is None
            await _clear_filter(pilot, app)
            assert app._selected == "sel_m1" and table.cursor_row == 4
            assert not table.has_class("-resting")

            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected == "sel_a" and not isinstance(app.screen, SpecScreen)
            assert not table.has_class("-resting")
            await pilot.press("down")
            await _wait_for_ui(pilot, lambda: app._selected == "sel_z")
            await _clear_filter(pilot, app)
            assert app._selected == "sel_z" and table.cursor_row == 5

    asyncio.run(scenario())


def test_pilot_dl532_a_refresh_while_the_selection_is_hidden_changes_nothing(
    short_root: Path,
) -> None:
    """DL-532: status answers while a filter hides the selected job select no row and ask no
    explain; the job is still selected again when the filter clears."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            await pilot.pause()
            mark = len(seen)
            for _ in range(3):
                await app._refresh()
                await pilot.pause()
            assert app._selected is None and table.has_class("-resting")
            assert table.cursor_row == 0
            assert [r for r in seen[mark:] if r.get("cmd") in ("explain", "sendevent")] == []
            await _clear_filter(pilot, app)
            assert app._selected == "sel_m1"

    asyncio.run(scenario())


def test_pilot_dl532_an_update_moves_no_selection(short_root: Path) -> None:
    """DL-532: an answer that adds a job sorting first keeps the selection on its job. One
    that removes the selected job leaves nothing selected, and `h` says so. Up or down on the
    resting cursor then selects the row it rests on."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, seen, edits):
            await _pick(pilot, app, table, "sel_z")
            edits.append(lambda jobs: {**jobs, "sel_0": dict(jobs["sel_a"])})
            await app._refresh()
            await pilot.pause()
            assert app._table_sync.identity == ["sel_0", *_SEL_ORDER]
            assert app._selected == "sel_z" and table.cursor_row == 6

            edits.append(
                lambda jobs: {
                    name: row
                    for name, row in {**jobs, "sel_0": jobs["sel_a"]}.items()
                    if name != "sel_z"
                }
            )
            await app._refresh()
            await pilot.pause()
            assert app._table_sync.identity == ["sel_0", *_SEL_ORDER[:-1]]
            assert app._selected is None and table.has_class("-resting")
            await pilot.press("h")
            await pilot.pause()
            assert _lines(app)[-1] == "ON_HOLD: no job selected" and _sent(seen) == []

            await pilot.press("down")
            await pilot.pause()
            assert app._selected == "sel_0" and table.cursor_row == 0

    asyncio.run(scenario())


def test_pilot_dl532_a_rebuild_never_passes_the_selection_through_another_row(
    short_root: Path,
) -> None:
    """The bounce guard under DL-532: rebuilds queued back to back, each firing the table's
    row-0 highlight before the cursor lands, never select another job, so no other job's
    explain is asked and the log tail keeps its source."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, seen, _edits):
            await _pick(pilot, app, table, "sel_z")
            await pilot.pause()
            sources: list[Any] = []
            pager = app.query_one("#logtail", _LogTail)
            inner = pager.set_source

            def set_source(*args: Any) -> None:
                sources.append(args)
                inner(*args)

            pager.set_source = set_source  # type: ignore[method-assign]
            mark = len(seen)
            jobs = dict(app._jobs_snapshot)
            more = {**jobs, "sel_0": dict(jobs["sel_a"])}
            for snapshot in (more, jobs, more, jobs):
                app._update_table(snapshot)
            await pilot.pause()
            app._tail_step()
            await app._refresh()
            await pilot.pause()
            assert app._selected == "sel_z" and table.cursor_row == 5
            explained = {r["job"] for r in seen[mark:] if r.get("cmd") == "explain"}
            assert explained <= {"sel_z"} and sources == []

    asyncio.run(scenario())


def test_pilot_dl532_a_filter_typed_down_to_one_job_selects_it_for_h(short_root: Path) -> None:
    """DL-532 rule 6: the operator's filter leaves one job, so it is selected as if picked,
    though the job selected before was another; `h` holds it."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, server, seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, "sel_z", ["sel_z"])
            assert app._selected == "sel_z" and not table.has_class("-resting")
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["sel_z"]["on_hold"])
            assert [r["payload"] for r in _sent(seen)] == [{"job": "sel_z"}]
            assert await _held_jobs(server) == {"sel_z"}

    asyncio.run(scenario())


def test_pilot_dl532_a_refresh_that_leaves_one_other_job_selects_nothing(
    short_root: Path,
) -> None:
    """DL-532: rule 6 is the operator's filter change only. A status filter shows one job,
    selected by rule 6; a status answer then moves it out and another single job in. Nothing
    is selected, `h` says so, and the new row rests unpainted."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, seen, _edits):
            await _set_status(app, pilot, "sel_a", "FAILURE")
            await _filter(pilot, app, "status=fa", ["sel_a"])
            assert app._selected == "sel_a"
            await _set_status(app, pilot, "sel_z", "FAILURE")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_a", "sel_z"])
            await _set_status(app, pilot, "sel_a", "SUCCESS")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_z"])
            assert app._selected is None and app._rest == "sel_z"
            assert table.has_class("-resting")
            assert _row_background(table, 0) == _row_background(table, 1)
            mark = len(_sent(seen))
            await pilot.press("h")
            await pilot.pause()
            assert _lines(app)[-1] == "ON_HOLD: no job selected"
            assert len(_sent(seen)) == mark

    asyncio.run(scenario())


def test_pilot_dl532_the_sole_job_a_filter_shows_stays_selected_when_cleared(
    short_root: Path,
) -> None:
    """DL-532 rule 6: X was chosen, the filter shows only Y, so Y is chosen; clearing the
    filter keeps Y selected."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, "sel_z", ["sel_z"])
            assert app._selected == "sel_z"
            await _clear_filter(pilot, app)
            assert app._selected == "sel_z" and table.cursor_row == 5

    asyncio.run(scenario())


def test_pilot_dl532_typing_to_one_job_without_enter_selects_nothing(short_root: Path) -> None:
    """DL-532 rule 6 is Enter's: a keystroke that leaves one job shown selects nothing, and the
    row rests unpainted."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await pilot.press("slash", *"sel_z")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_z"])
            assert app._selected is None and table.has_class("-resting")

    asyncio.run(scenario())


def test_pilot_dl532_typing_from_one_match_to_none_and_back_then_enter_selects_it(
    short_root: Path,
) -> None:
    """DL-532 rule 6: typing to one match, to none and back to one selects nothing; Enter on
    the one row shown selects it."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await pilot.press("slash", *"sel_d")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_deep"])
            await pilot.press("x")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == [])
            await pilot.press("backspace")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_deep"])
            assert app._selected is None
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected == "sel_deep" and not table.has_class("-resting")

    asyncio.run(scenario())


_LOAD_JIL = "".join(
    f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n"
    for name in ("load", "load_eod", "load_eom", "other")
)


@pytest.mark.parametrize("ending", ["=load_*", "=load_"], ids=["several", "none"])
def test_pilot_dl532_a_prefix_with_one_match_then_escape_keeps_the_earlier_pick(
    short_root: Path, ending: str
) -> None:
    """DL-532: `=load` on the way to `=load_*` shows only `load`. Typing picks nothing, so Esc
    returns to the job picked before the filter (rule 4)."""
    order = ["load", "load_eod", "load_eom", "other"]

    async def scenario() -> None:
        async with _selection_app(short_root, _LOAD_JIL, order) as (
            app,
            pilot,
            table,
            _server,
            _seen,
            _edits,
        ):
            await _pick(pilot, app, table, "other")
            await pilot.press("slash", *"=load")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["load"])
            assert app._selected is None
            await pilot.press(*ending.removeprefix("=load"))
            await _wait_for_ui(pilot, lambda: app._filter == ending)
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == order)
            assert app._selected == "other" and table.cursor_row == 3

    asyncio.run(scenario())


def test_pilot_dl532_enter_selects_the_one_row_a_refresh_left_under_the_filter(
    short_root: Path,
) -> None:
    """DL-532 rule 6 at Enter: while the operator types a status filter, status answers swap
    the one row shown. Enter selects the row then shown and highlighted, and `h` holds it."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, server, _seen, _edits):
            await _set_status(app, pilot, "sel_a", "FAILURE")
            await _pick(pilot, app, table, "sel_m1")
            await pilot.press("slash", *"status=fa")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_a"])
            assert app._selected is None
            await _set_status(app, pilot, "sel_z", "FAILURE")
            await _set_status(app, pilot, "sel_a", "SUCCESS")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_z"])
            assert app._selected is None
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected == "sel_z" and table.cursor_row == 0
            assert app._table_sync.identity == ["sel_z"] and not table.has_class("-resting")
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["sel_z"]["on_hold"])
            assert await _held_jobs(server) == {"sel_z"}

    asyncio.run(scenario())


def _with_status(jobs: dict, **statuses: str) -> dict:
    return {**jobs, **{name: {**jobs[name], "status": s} for name, s in statuses.items()}}


def test_pilot_dl532_one_answer_that_swaps_the_single_row_selects_nothing(
    short_root: Path,
) -> None:
    """DL-532: one status answer takes the selected single row out of the filter and puts
    another single row in. Nothing is selected and the new row rests unpainted."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, edits):
            await _set_status(app, pilot, "sel_a", "FAILURE")
            await _filter(pilot, app, "status=fa", ["sel_a"])
            assert app._selected == "sel_a"
            edits.append(lambda jobs: _with_status(jobs, sel_a="SUCCESS", sel_z="FAILURE"))
            await app._refresh()
            await pilot.pause()
            assert app._table_sync.identity == ["sel_z"]
            assert app._selected is None and table.has_class("-resting")

    asyncio.run(scenario())


def test_pilot_dl532_a_row_selected_event_a_rebuild_overtook_selects_nothing(
    short_root: Path,
) -> None:
    """DL-532: Enter on Z as a status answer takes Z out of the filter. The table's
    RowSelected for Z arrives after the rebuild, and so does the rebuild's own row-0
    highlight. Neither selects: nothing is selected afterwards."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, edits):
            await _set_status(app, pilot, "sel_a", "FAILURE")
            await _set_status(app, pilot, "sel_z", "FAILURE")
            await _filter(pilot, app, "status=fa", ["sel_a", "sel_z"])
            await _pick(pilot, app, table, "sel_z")
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            table.post_message(DataTable.RowSelected(table, 1, key))
            edits.append(lambda jobs: _with_status(jobs, sel_z="SUCCESS"))
            app._update_table(_with_status(app._jobs_snapshot, sel_z="SUCCESS"))
            await pilot.pause()
            assert app._table_sync.identity == ["sel_a"]
            assert app._selected is None and app._rest == "sel_a"
            assert table.has_class("-resting") and not isinstance(app.screen, SpecScreen)

    asyncio.run(scenario())


def test_pilot_dl532_a_pick_between_a_rebuild_and_its_events_stands(short_root: Path) -> None:
    """DL-532: with the cursor resting on row 0, a rebuild is followed by a pick before the
    rebuild's own row-0 highlight is handled. That highlight does not replace the pick."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            assert app._selected is None and table.cursor_row == 0
            jobs = dict(app._jobs_snapshot)
            app._update_table({name: row for name, row in jobs.items() if name != "sel_z"})
            app._update_table(jobs)
            assert app._table_sync.identity == _SEL_FILTERED
            assert table.cursor_row == 0 and app._rest == "sel_a"
            app._choose("sel_z")  # a pick that lands before the rebuild's events
            await pilot.pause()
            assert app._selected == "sel_z"

    asyncio.run(scenario())


@pytest.mark.parametrize("x", [3, 30], ids=["job-cell", "other-cell"])
def test_pilot_dl532_a_click_anywhere_on_the_resting_row_selects_it(
    short_root: Path, x: int
) -> None:
    """DL-532: with nothing selected, a click on the resting row, on its job cell or another
    cell, selects that row and opens no details."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _pick(pilot, app, table, "sel_m1")
            await _filter(pilot, app, _SEL_FILTER, _SEL_FILTERED)
            assert app._selected is None
            await pilot.click("#jobs", offset=(x, 2))
            await pilot.pause()
            assert app._selected == "sel_a" and not table.has_class("-resting")
            assert not isinstance(app.screen, SpecScreen)

    asyncio.run(scenario())


_MANY = [f"many_{i:02d}" for i in range(60)]
_MANY_JIL = "".join(
    f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n\n" for name in [*_MANY, "x_pick"]
)


def test_pilot_dl532_a_pick_after_a_scroll_shows_the_row_it_selects(short_root: Path) -> None:
    """DL-532: pick X; a submitted filter hides it and shows more rows than fit; the table is
    scrolled as the mouse wheel scrolls it; down selects the resting row and scrolls it into
    view, and `h` holds that visible, highlighted job."""

    async def scenario() -> None:
        async with _selection_app(short_root, _MANY_JIL, [*_MANY, "x_pick"]) as (
            app,
            pilot,
            table,
            server,
            _seen,
            _edits,
        ):
            await _pick(pilot, app, table, "x_pick")
            await _filter(pilot, app, "many_", _MANY)
            assert app._selected is None and table.cursor_row == 0
            table.scroll_to(y=30, animate=False)
            await _wait_for_ui(pilot, lambda: table.scroll_y == 30)
            await pilot.press("down")
            await _wait_for_ui(pilot, lambda: table.scroll_y == 0)
            assert app._selected == "many_00" and table.cursor_row == 0
            assert not table.has_class("-resting")
            await pilot.press("h")
            await _wait_for_ui(pilot, lambda: app._jobs_snapshot["many_00"]["on_hold"])
            assert await _held_jobs(server) == {"many_00"}

    asyncio.run(scenario())


def test_pilot_dl532_an_update_that_puts_the_job_under_a_folded_box_selects_nothing(
    short_root: Path,
) -> None:
    """DL-532: a box stands for the chosen job only through the operator's own fold. The job
    is shown and selected, the operator folds every box, then an update moves the job into a
    folded box: nothing is selected, and `h` says so."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, seen, edits):
            await _pick(pilot, app, table, "sel_z")
            await pilot.press("z")
            await _wait_for_ui(
                pilot, lambda: app._table_sync.identity == ["sel_a", "sel_box", "sel_z"]
            )
            assert app._selected == "sel_z"
            edits.append(lambda jobs: {**jobs, "sel_z": {**jobs["sel_z"], "box_name": "sel_box"}})
            await app._refresh()
            await pilot.pause()
            assert app._table_sync.identity == ["sel_a", "sel_box"]
            assert app._selected is None and table.has_class("-resting")
            await pilot.press("h")
            await pilot.pause()
            assert _lines(app)[-1] == "ON_HOLD: no job selected" and _sent(seen) == []

    asyncio.run(scenario())


def test_pilot_dl532_a_view_change_to_one_job_selects_it(short_root: Path) -> None:
    """DL-532 rule 6 at a view change: the problems view shows the one failed job, so it is
    selected; the view cycled on to one that shows none selects nothing."""

    async def scenario() -> None:
        async with _selection_app(short_root) as (app, pilot, table, _server, _seen, _edits):
            await _set_status(app, pilot, "sel_z", "FAILURE")
            await _pick(pilot, app, table, "sel_m1")
            await pilot.press("v")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["sel_z"])
            assert app._selected == "sel_z"
            await pilot.press("v")
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == [])
            assert app._selected is None

    asyncio.run(scenario())


# ------------------------------------------------- DL-520: pane borders and +/-


def _panes(app: RunnerApp) -> tuple[int, int, int, int, int, int]:
    """The table column's and the side column's widths, the explain pane's, the log pane's and
    the console's heights, and the height of the row above the console."""
    regions = {
        name: app.query_one(f"#{name}").region
        for name in ("tablecol", "side", "explain-box", "logbox", "consolebox", "main")
    }
    return (
        regions["tablecol"].width,
        regions["side"].width,
        regions["explain-box"].height,
        regions["logbox"].height,
        regions["consolebox"].height,
        regions["main"].height,
    )


def _handles(app: RunnerApp) -> dict[str, tuple[int, int]]:
    """A cell on each split's handle, read off the widgets' regions: the table's right border,
    the log pane's top border and the console's top border."""
    tablecol, logbox, console = (
        app.query_one(f"#{name}").region for name in ("tablecol", "logbox", "consolebox")
    )
    return {
        "table": (tablecol.right - 1, tablecol.y + 6),
        "log": (logbox.x + 10, logbox.y),
        "console": (console.x + 30, console.y),
    }


async def _drag(pilot, start: tuple[int, int], *path: tuple[int, int]) -> None:
    """Press the first button at `start`, move with it held through `path`, and release at the
    last point, as a terminal reports a drag."""
    await pilot.mouse_down(offset=start)
    for point in path:
        await pilot._post_mouse_events([events.MouseMove], offset=point, button=1)
    await pilot.mouse_up(offset=path[-1] if path else start)
    await pilot.pause()


def _shift(point: tuple[int, int], dx: int = 0, dy: int = 0) -> tuple[int, int]:
    return point[0] + dx, point[1] + dy


def test_pilot_dl520_dragging_each_border_moves_it_with_the_pointer_and_back(
    short_root: Path,
) -> None:
    """DL-520: a drag on the table's right border by N columns widens the table column by N and
    narrows the side by N; back left restores it. The explain/log border and the console's
    border follow the pointer by rows in the same way."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            start = (72, 48, 11, 16, 11, 27)
            await _wait_for_ui(pilot, lambda: _panes(app) == start)
            handle = _handles(app)

            await _drag(
                pilot, handle["table"], _shift(handle["table"], 3), _shift(handle["table"], 7)
            )
            await _wait_for_ui(pilot, lambda: _panes(app) == (79, 41, 11, 16, 11, 27))
            moved = _shift(handle["table"], 7)
            await _drag(pilot, moved, handle["table"])
            await _wait_for_ui(pilot, lambda: _panes(app) == start)

            await _drag(pilot, handle["log"], _shift(handle["log"], dy=4))  # down: log shorter
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 15, 12, 11, 27))
            await _drag(pilot, _shift(handle["log"], dy=4), handle["log"])
            await _wait_for_ui(pilot, lambda: _panes(app) == start)

            await _drag(pilot, handle["console"], _shift(handle["console"], dy=-5))  # up
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (16, 22))
            await _drag(pilot, _shift(handle["console"], dy=-5), handle["console"])
            await _wait_for_ui(pilot, lambda: _panes(app) == start)

            # the other line of each handle drags the same split
            side_left = (app.query_one("#side").region.x, handle["table"][1])
            await _drag(pilot, side_left, _shift(side_left, -2))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (70, 50))
            explain_bottom = _shift(handle["log"], dy=-1)
            await _drag(pilot, explain_bottom, _shift(explain_bottom, dy=-2))
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (9, 18))
            above_bottom = _shift(handle["console"], dy=-1)
            await _drag(pilot, above_bottom, _shift(above_bottom, dy=3))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (8, 30))

    asyncio.run(scenario())


def test_pilot_dl520_the_limits_hold_at_both_ends_of_each_split(short_root: Path) -> None:
    """DL-520: no pane narrower than 20 columns; the explain pane at least 3 rows and the log
    pane at least 5; the console box at least 6 rows and the area above it at least 8. The border
    stops at the limit while the pointer goes on."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            handle = _handles(app)
            table = handle["table"]
            await _drag(pilot, table, (110, table[1]), (119, table[1]))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (100, 20))
            await _drag(pilot, (99, table[1]), (0, table[1]))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (20, 100))

            log = handle["log"]
            await _drag(pilot, log, (log[0], 1))  # the top of the side column
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (3, 24))
            log = (log[0], app.query_one("#logbox").region.y)
            await _drag(pilot, log, (log[0], 39))
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (22, 5))

            console = handle["console"]
            await _drag(pilot, console, (console[0], 0))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (30, 8))
            console = (console[0], app.query_one("#consolebox").region.y)
            await _drag(pilot, console, (console[0], 39))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (6, 32))

    asyncio.run(scenario())


def test_pilot_dl520_a_press_and_release_on_a_handle_changes_no_size(short_root: Path) -> None:
    """DL-520: a press and release without a move, the click a person makes on a border,
    changes no size, selects no row and opens nothing."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            start = (72, 48, 11, 16, 11, 27)
            await _wait_for_ui(pilot, lambda: _panes(app) == start)
            sizes = app._sizes
            for point in _handles(app).values():
                await pilot.click(offset=point)
                await pilot.pause()
                assert _panes(app) == start and app._sizes == sizes
            await _drag(pilot, _handles(app)["table"])
            assert _panes(app) == start and app._sizes == sizes
            assert app._selected == "bt_box" and table.cursor_row == 0
            assert details == [] and app._collapsed == set()

    asyncio.run(scenario())


def test_pilot_dl520_a_drag_from_a_handle_selects_folds_and_opens_nothing(
    short_root: Path,
) -> None:
    """DL-520: a drag that starts on a handle and ends over a row's marker selects no row, folds
    nothing, opens nothing, moves no focus and starts no text selection. A click on a row and on
    a box's marker still do what they do (DL-512), and a press in the log still starts a text
    selection."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            console = app.query_one("#console", RichLog)
            console.focus()
            await pilot.pause()
            row1 = table.region.y + 3  # bt_inner's row: border, header, row 0
            # the pointer ends on bt_inner's indent, past the table's 20-column limit
            await _drag(pilot, (71, row1), (40, row1), (table.region.x + 2, row1))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (20, 100))
            # the bottom border of the table, over the first column, is the console's handle
            await _drag(pilot, (2, 27), (2, 26), (2, 27))
            await pilot.click(offset=(71 - 52, row1))  # the table's right border now
            await pilot.pause()
            assert app.focused is console
            assert app._selected == "bt_box" and table.cursor_row == 0
            assert details == [] and app._collapsed == set()
            assert app._table_sync.identity == _BOX_TREE_ORDER

            logbox = app.query_one("#logbox").region
            await pilot.mouse_down(offset=(logbox.x + 5, logbox.y))
            assert app.screen._select_state is None
            await pilot.mouse_up(offset=(logbox.x + 5, logbox.y))
            await pilot.mouse_down(offset=(logbox.x + 5, logbox.y + 2))
            assert app.screen._select_state is not None
            await pilot.mouse_up(offset=(logbox.x + 5, logbox.y + 2))

            await _click_label(pilot, 1, 4)  # bt_inner's name
            await _wait_for_ui(pilot, lambda: app._selected == "bt_inner")
            await _click_label(pilot, 0, 0)  # bt_box's marker
            await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["bt_box", "bt_solo"])
            assert details == []

    asyncio.run(scenario())


def test_pilot_dl520_a_release_off_the_handle_or_outside_the_app_ends_the_drag(
    short_root: Path,
) -> None:
    """DL-520: the drag goes on while the pointer leaves the handle with the button held, and a
    release anywhere ends it. A move with no button held, as after a release outside the app,
    ends it too. A later move without a button changes nothing."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            table = _handles(app)["table"]
            await _drag(pilot, table, (80, 20), (60, 3))  # off the handle, then released
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (61, 59))  # border on 60
            await pilot.hover(offset=(90, 20))
            await pilot.hover(offset=(100, 30))
            await pilot.pause()
            assert _panes(app)[:2] == (61, 59) and app._drag is None

            await pilot.mouse_down(offset=(60, 10))
            await pilot._post_mouse_events([events.MouseMove], offset=(50, 10), button=1)
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (51, 69))
            # a press of another button changes nothing: the drag goes on
            await pilot._post_mouse_events([events.MouseDown], offset=(45, 10), button=3)
            assert app._drag is not None and app._lit is Split.TABLE
            await pilot.hover(offset=(40, 10))  # the button was let go outside the app
            await pilot.hover(offset=(30, 10))
            await pilot.pause()
            assert _panes(app)[:2] == (51, 69) and app._drag is None and app._lit is None

            await pilot.mouse_down(offset=(50, 10))
            await pilot._post_mouse_events([events.MouseMove], offset=(55, 10), button=1)
            app.post_message(events.AppBlur())  # the window lost focus: no release will come
            await _wait_for_ui(pilot, lambda: app._drag is None and app._lit is None)
            await pilot._post_mouse_events([events.MouseMove], offset=(70, 10), button=1)
            await pilot.pause()
            assert _panes(app)[:2] == (56, 64)

    asyncio.run(scenario())


def test_pilot_dl520_nothing_resizes_while_the_log_is_maximized(short_root: Path) -> None:
    """DL-520: while the log pane is maximized, a drag where a border was and every resize key
    change nothing; back from the maximized view the layout is as it was."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            start = (72, 48, 11, 16, 11, 27)
            await _wait_for_ui(pilot, lambda: _panes(app) == start)
            handles = _handles(app)
            sizes = app._sizes
            await pilot.press("m")
            await _wait_for_ui(pilot, lambda: app.screen.maximized is not None)
            for point in handles.values():
                await _drag(pilot, point, _shift(point, 5, 5))
            await pilot.press("plus", "equals_sign", "minus", "minus", "]", "}", "[", "{")
            await pilot.pause()
            assert app._sizes == sizes and app._drag is None
            await pilot.press("m")
            await _wait_for_ui(pilot, lambda: app.screen.maximized is None)
            await _wait_for_ui(pilot, lambda: _panes(app) == start)

    asyncio.run(scenario())


def test_pilot_dl520_plus_and_minus_resize_the_focused_pane(short_root: Path) -> None:
    """DL-520: `+` and `=` grow and `-` shrinks the focused pane, by 5 points of the ratio or 2
    rows: the table column's width, the explain pane's or the log pane's height against the
    other, the console's height. In the console line they are typed as text, and in a dialog
    they do nothing."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            await pilot.press("plus")  # the table is focused: 0.65 of 120
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (78, 42))
            await pilot.press("equals_sign")
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (84, 36))
            await pilot.press("minus")
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (78, 42))

            app.query_one("#explain-box").focus()
            await pilot.pause()
            await pilot.press("plus")  # the log's share 0.55 of 27 rows
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (12, 15))
            app.query_one("#logtail").focus()
            await pilot.pause()
            await pilot.press("plus", "plus")  # 0.65
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (9, 18))
            await pilot.press("minus")
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (11, 16))
            app.query_one("#console").focus()
            await pilot.pause()
            await pilot.press("plus")
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (13, 25))
            await pilot.press("minus", "minus")
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (9, 29))

            sizes, panes = app._sizes, _panes(app)
            app.screen.set_focus(None)  # no pane focused: nothing to resize
            await pilot.press("plus", "minus")
            await pilot.pause()
            assert app._sizes == sizes
            await pilot.press("colon")
            cmdline = app.query_one("#cmdline", Input)
            await pilot.press("plus", "equals_sign", "minus")
            await pilot.pause()
            assert cmdline.value == "+=-"
            assert app._sizes == sizes and _panes(app) == panes
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("t")  # the triggers view, a dialog
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
            await pilot.press("plus", "equals_sign", "minus")
            await pilot.pause()
            assert app._sizes == sizes
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: not isinstance(app.screen, TriggersScreen))
            assert _panes(app) == panes

    asyncio.run(scenario())


def test_pilot_dl520_the_bracket_keys_step_a_fifth_from_where_a_drag_left_the_split(
    short_root: Path,
) -> None:
    """DL-520: after a drag, `}` and `{` move the table's border by a fifth from where the drag
    left it, and `]` and `[` the log's, within the limits."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            handles = _handles(app)
            await _drag(pilot, handles["table"], (49, handles["table"][1]))  # 50 of 120
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (50, 70))
            await pilot.press("right_curly_bracket")  # 50/120 + 0.2 = 74/120
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (74, 46))
            await pilot.press("right_curly_bracket")
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (98, 22))
            await pilot.press("right_curly_bracket")  # the side keeps 20 columns
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (100, 20))
            await pilot.press("left_curly_bracket")
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (76, 44))

            log = handles["log"]
            await _drag(pilot, log, _shift(log, dy=-6))  # 22 of 27 rows
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (5, 22))
            await pilot.press("left_square_bracket")  # 22/27 - 0.2 = 16.6 rows
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (10, 17))
            await pilot.press("right_square_bracket", "right_square_bracket")
            await _wait_for_ui(pilot, lambda: _panes(app)[2:4] == (3, 24))

    asyncio.run(scenario())


def test_pilot_dl520_a_change_of_the_terminal_size_keeps_the_ratios_and_the_console_rows(
    short_root: Path,
) -> None:
    """DL-520: the two ratios and the console's rows outlast a change of the terminal's size,
    within the limits there; a screen too small for the limits keeps the present layout, and
    nothing resizes it."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            handles = _handles(app)
            await _drag(pilot, handles["table"], (47, handles["table"][1]))  # 48 of 120: 0.4
            await _drag(pilot, handles["console"], _shift(handles["console"], dy=-4))  # 15 rows
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (48, 72))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (15, 23))
            log = round(app._sizes.log * 23)
            assert _panes(app)[3] == log

            await pilot.resize_terminal(180, 50)  # 48 rows under the header and the footer
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 108, 13, 20, 15, 33))
            await pilot.resize_terminal(50, 16)  # 14 rows: the console gives way to 6
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (20, 30))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (6, 8))
            await pilot.resize_terminal(120, 40)
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (48, 72))
            await _wait_for_ui(pilot, lambda: _panes(app)[3:] == (log, 15, 23))

            sizes = app._sizes
            await pilot.resize_terminal(36, 30)  # too narrow for two 20-column panes
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (14, 22))
            table.focus()
            await pilot.pause()
            await pilot.press("plus", "right_curly_bracket")
            await _drag(pilot, (13, 10), (20, 10))
            assert app._sizes.table == sizes.table
            await pilot.resize_terminal(120, 40)
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (48, 72))

    asyncio.run(scenario())


def _edge_colours(widget, y: int) -> list[tuple[str, str | None]]:
    """The text and the colour of each segment of one rendered line of a widget, its borders
    included."""
    line = widget.render_lines(widget.region.reset_offset)[y]
    return [
        (segment.text, segment.style.color.name if segment.style and segment.style.color else None)
        for segment in line
    ]


def test_pilot_dl520_the_handle_takes_the_accent_colour_under_the_pointer_and_in_a_drag(
    short_root: Path,
) -> None:
    """DL-520: while the pointer is over a handle, and during its drag, the two border lines of
    that split take the accent colour; they return to their colour when the pointer leaves or
    the drag ends. The titles and subtitles on those lines keep their colour."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            jobs, explain, logtail, console = (
                app.query_one(f"#{name}") for name in ("jobs", "explain-box", "logtail", "console")
            )
            plain = _edge_colours(jobs, 5)[-1][1]
            title = _edge_colours(logtail, 0)[2]
            subtitle = _edge_colours(jobs, -1)[2]
            events_title = _edge_colours(console, 0)[2]
            assert (
                title[0] == " log (out): none yet " and subtitle[0] == " space fold · z fold all "
            )
            handles = _handles(app)

            await pilot.hover(offset=handles["table"])
            await pilot.pause()
            accent = _edge_colours(jobs, 5)[-1][1]
            assert accent != plain, _edge_colours(explain, 5)
            assert _edge_colours(explain, 5)[0][1] == accent, _edge_colours(explain, 5)
            assert _edge_colours(logtail, 5)[0][1] == accent
            assert _edge_colours(logtail, 0)[0][1] == plain  # not this split's line

            await pilot.hover(offset=handles["log"])
            await pilot.pause()
            assert _edge_colours(jobs, 5)[-1][1] == plain
            assert _edge_colours(explain, -1)[0][1] == accent
            assert _edge_colours(logtail, 0)[0][1] == accent
            assert _edge_colours(logtail, 0)[2] == title

            await pilot.hover(offset=handles["console"])
            await pilot.pause()
            assert _edge_colours(jobs, -1)[0][1] == accent
            assert _edge_colours(jobs, -1)[2] == subtitle
            assert _edge_colours(logtail, -1)[0][1] == accent
            assert _edge_colours(console, 0)[0][1] != _edge_colours(console, 2)[0][1]
            assert _edge_colours(console, 0)[2] == events_title

            await pilot.hover(offset=(30, 10))  # inside the table
            await pilot.pause()
            assert app._lit is None
            assert _edge_colours(jobs, -1)[0][1] == plain and _edge_colours(jobs, -1)[2] == subtitle

            # during a drag the handle stays lit away from it; the release away returns it
            await pilot.mouse_down(offset=handles["table"])
            await pilot._post_mouse_events([events.MouseMove], offset=(90, 30), button=1)
            assert _edge_colours(jobs, 5)[-1][1] == accent
            await pilot.mouse_up(offset=(100, 33))
            await pilot.pause()
            assert app._lit is None and _edge_colours(jobs, 5)[-1][1] == plain

    asyncio.run(scenario())


def test_pilot_dl520_the_key_list_names_the_resize_keys(short_root: Path) -> None:
    """DL-520: the F1 key list names each resize key by what it does."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await pilot.press("f1")
            await _wait_for_ui(pilot, lambda: bool(app.screen.query(HelpPanel)))
            listing = Console(width=200, file=io.StringIO(), record=True)
            listing.print(app.screen.query_one("BindingsTable").render_bindings_table())
            lines = listing.export_text().splitlines()
            for keys, name in (
                ("]", "log taller"),
                ("[", "log shorter"),
                ("}", "table wider"),
                ("{", "table narrower"),
                ("+ =", "grow pane"),
                ("-", "shrink pane"),
            ):
                assert any(line.split() == [*keys.split(), *name.split()] for line in lines), name

    asyncio.run(scenario())


@pytest.mark.parametrize(("size", "rows"), [((120, 40), 39), ((80, 24), 23)])
def test_pilot_dl520_the_zoomed_log_fills_the_screen_and_leaving_restores_the_split(
    short_root: Path, size: tuple[int, int], rows: int
) -> None:
    """DL-520: `m` zooms the log pane to the whole screen above the footer, as before any
    resize (39 rows at 120x40, 23 at 80x24), also after the splits were moved; leaving the zoom
    gives the split back as it was."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await pilot.resize_terminal(*size)
            await pilot.press("plus", "right_square_bracket")  # table wider, log taller
            await pilot.pause()
            panes = _panes(app)
            await _wait_for_ui(pilot, lambda: _panes(app) == panes)
            logbox = app.query_one("#logbox")
            await pilot.press("m")
            await _wait_for_ui(pilot, lambda: logbox.region == Region(0, 0, size[0], rows))
            await pilot.press("m")
            await _wait_for_ui(pilot, lambda: app.screen.maximized is None)
            await _wait_for_ui(pilot, lambda: _panes(app) == panes)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120, 40), (80, 16)])
def test_pilot_dl520_the_console_box_at_its_least_shows_an_event_line_above_the_command_line(
    short_root: Path, size: tuple[int, int]
) -> None:
    """DL-520: at its least, 6 rows, the console box shows one line of events above the
    command line, and the command line stays above the footer."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await pilot.resize_terminal(*size)
            app.query_one("#console").focus()
            await pilot.pause()
            await pilot.press(*["minus"] * 4)
            await _wait_for_ui(pilot, lambda: _panes(app)[4] == 6)
            console, cmdline, footer = (
                app.query_one(selector).region for selector in ("#console", "#cmdline", Footer)
            )
            assert console.height - 2 == 1  # one line between its two border lines
            assert console.bottom == cmdline.y and cmdline.bottom <= footer.y

    asyncio.run(scenario())


async def _post_raw(pilot, event_class: type, point: tuple[int, int], button: int = 1) -> None:
    """Post a mouse event to the app as the drivers do: through `App.on_event`, which hands it
    to the screen and makes a click of a release on the pressed widget. The pilot's own mouse
    calls skip that path."""
    app = pilot.app
    app.post_message(event_class(**_get_mouse_message_arguments(app.screen, point, button=button)))
    await pilot.pause()


def test_pilot_dl520_a_drag_on_the_drivers_path_resizes_and_its_click_reaches_nothing(
    short_root: Path,
) -> None:
    """DL-520: raw press, moves and release posted to the app move the table's border. The
    release lands over bt_inner's fold marker, past the 20-column limit, and the click the
    app makes of it selects, folds and opens nothing. A plain click on that path still
    selects. The drag goes through `_MainScreen._forward_event`: a library that stopped calling
    it would fail here."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            jobs = table.region
            row1 = jobs.y + 3
            await _post_raw(pilot, events.MouseDown, (jobs.right - 1, row1))
            await _post_raw(pilot, events.MouseMove, (60, row1))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (61, 59))
            await _post_raw(pilot, events.MouseMove, (jobs.x + 5, row1))
            await _post_raw(pilot, events.MouseUp, (jobs.x + 5, row1))
            await _wait_for_ui(pilot, lambda: _panes(app)[:2] == (20, 100))
            assert app._selected == "bt_box" and app._collapsed == set() and details == []

            await _post_raw(pilot, events.MouseDown, (jobs.x + 8, row1))
            await _post_raw(pilot, events.MouseUp, (jobs.x + 8, row1))
            await _wait_for_ui(pilot, lambda: app._selected == "bt_inner")
            assert app._collapsed == set() and details == []

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["resize", "blur", "second button"])
@pytest.mark.parametrize("target", ["selected row", "marker"])
def test_pilot_dl520_a_release_after_the_drag_ended_early_reaches_nothing(
    short_root: Path, ending: str, target: str
) -> None:
    """DL-520: the press that started a drag owns its release and the click made of it. When
    the drag ends early, by a resize or a lost focus, or is interrupted by another button,
    the release over the selected row opens no details and over a box's marker folds
    nothing. The events go the drivers' way."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, table, details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            jobs = table.region
            border = (jobs.right - 1, jobs.y + 6)
            point = (jobs.x + (6 if target == "selected row" else 2), jobs.y + 2)  # bt_box
            await _post_raw(pilot, events.MouseDown, border)
            await _post_raw(pilot, events.MouseMove, (40, border[1]))
            if ending == "resize":
                await pilot.resize_terminal(120, 41)
            elif ending == "blur":
                app.post_message(events.AppBlur())
            else:
                await _post_raw(pilot, events.MouseDown, point, button=3)
                await _post_raw(pilot, events.MouseUp, point, button=3)
            await pilot.pause()
            await _post_raw(pilot, events.MouseMove, point)
            await _post_raw(pilot, events.MouseUp, point)
            await pilot.pause()
            assert details == [] and app._collapsed == set()
            assert app._selected == "bt_box" and app._drag is None and not app._owned

            await _post_raw(pilot, events.MouseDown, point)  # the next click is a click
            await _post_raw(pilot, events.MouseUp, point)
            if target == "selected row":
                await _wait_for_ui(pilot, lambda: details == ["bt_box"])
            else:
                await _wait_for_ui(pilot, lambda: app._collapsed == {"bt_box"})

    asyncio.run(scenario())


def test_pilot_dl520_the_handle_is_only_the_lines_that_meet(short_root: Path) -> None:
    """DL-520: with the pager's prompt open, its bottom line is the log pane's: a press there
    drags the console split and that line lights up, while the log's own bottom line above
    the prompt is no handle. The prompt's left line belongs to the table split. The filter
    line's right border is no handle: a press there focuses the line as before."""

    async def scenario() -> None:
        async with _box_tree_app(short_root) as (app, pilot, _table, _details):
            await _wait_for_ui(pilot, lambda: _panes(app) == (72, 48, 11, 16, 11, 27))
            logtail, prompt = app.query_one("#logtail"), app.query_one("#logsearch")
            logtail.focus()
            await pilot.pause()
            await pilot.press("slash")
            await _wait_for_ui(pilot, lambda: prompt.region.height == 3)
            bottom = (prompt.region.x + 5, prompt.region.bottom - 1)
            await pilot.hover(offset=bottom)
            await pilot.pause()
            assert app._lit is Split.CONSOLE
            assert prompt.has_class("-grab-bottom") and not logtail.has_class("-grab-bottom")
            await pilot.hover(offset=(logtail.region.x + 5, logtail.region.bottom - 1))
            await pilot.pause()
            assert app._lit is None and not prompt.has_class("-grab-bottom")
            await pilot.hover(offset=(prompt.region.x, prompt.region.y + 1))
            await pilot.pause()
            assert app._lit is Split.TABLE and prompt.has_class("-grab-left")
            await _drag(pilot, bottom, _shift(bottom, dy=-3))
            await _wait_for_ui(pilot, lambda: _panes(app)[4:] == (14, 24))
            assert app.focused is prompt

            await pilot.press("escape")
            table = app.query_one("#jobs")
            table.focus()
            await pilot.pause()
            await pilot.press("slash")
            line = app.query_one("#filterline", Input)
            await _wait_for_ui(pilot, lambda: line.region.height == 3)
            table.focus()
            await pilot.pause()
            edge = (line.region.right - 1, line.region.y + 1)
            await pilot.hover(offset=edge)
            await pilot.pause()
            assert app._lit is None
            await pilot.click(offset=edge)
            await pilot.pause()
            assert app.focused is line and app._drag is None and _panes(app)[:2] == (72, 48)

    asyncio.run(scenario())


def test_pilot_folded_box_rollup_carries_a_red_problem_tally(short_root: Path) -> None:
    """A fold must never swallow a FAILURE silently (DL-65): with one member
    driven to FAILURE, the folded box label reads "(2, 1!)" in bold red."""
    text = (
        "insert_job: pr_box\njob_type: b\n\n"
        "insert_job: pr_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: pr_box\n\n"
        "insert_job: pr_m2\njob_type: c\ncommand: x\nmachine: m1\nbox_name: pr_box\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 3)
                table = app.query_one("#jobs", DataTable)

                cmdline = app.query_one("#cmdline", Input)
                cmdline.focus()
                cmdline.value = "CHANGE_STATUS pr_m1 FAILURE 1"
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("pr_m1", "status")) == "FAILURE"
                )

                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "pr_box"
                await pilot.press("space")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["pr_box"])
                label = table.get_cell("pr_box", "job")
                assert str(label) == "▸ pr_box (2, 1!)"
                assert label.style == "bold red"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_slash_filter_narrows_flat_enter_keeps_escape_clears(short_root: Path) -> None:
    """DL-65 incremental filter: `/` focuses the filter line, each
    keystroke re-narrows the table LIVE and FLAT (a match inside a box must
    never be invisible), space-separated words AND together, the border
    title carries the visible/total counts plus the filter, Enter keeps the
    filter and returns to the table, Escape clears and refocuses it."""
    text = (
        "insert_job: fx_box\njob_type: b\n\n"
        "insert_job: fx_apple\njob_type: c\ncommand: x\nmachine: m1\nbox_name: fx_box\n\n"
        "insert_job: fx_apricot\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: fx_banana\njob_type: c\ncommand: x\nmachine: m1\n"
    )
    tree_order = ["fx_apricot", "fx_banana", "fx_box", "fx_apple"]

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                table = app.query_one("#jobs", DataTable)

                await pilot.press("slash")
                await pilot.pause()
                assert app.focused is not None and app.focused.id == "filterline"

                await pilot.press("a", "p")
                await _wait_for_ui(
                    pilot, lambda: app._table_sync.identity == ["fx_apple", "fx_apricot"]
                )
                # the filtered view is FLAT: the box member renders unindented
                assert str(table.get_cell("fx_apple", "job")) == "fx_apple"
                assert "jobs 2/4" in str(table.border_title)
                assert "/ap/" in str(table.border_title)

                await pilot.press("space", "p", "l", "e")  # "ap ple": terms AND
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fx_apple"])

                await pilot.press("enter")  # keep the filter, back to the table
                await pilot.pause()
                assert app.focused is table
                assert app._filter == "ap ple"
                assert app._table_sync.identity == ["fx_apple"]

                await pilot.press("slash")
                await pilot.pause()
                await pilot.press("escape")  # clear and refocus the table
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                assert app._filter == ""
                assert app.focused is table
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


_FILTER_ESTATE = (
    "insert_job: fl_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: fl_b\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: fl_c\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: other_d\njob_type: c\ncommand: x\nmachine: m1\n"
)


async def _set_status(app: RunnerApp, pilot, job: str, status: str) -> None:
    request = parse_console_command(f"CHANGE_STATUS {job} {status}", None)
    assert not isinstance(request, str)
    await app._do_sendevent(request)
    await _wait_for_ui(pilot, lambda: app._jobs_snapshot[job]["status"] == status)


def _filter_message(app: RunnerApp) -> str | None:
    """The refusal under the filter line, or None while it is hidden."""
    box = app.query_one("#filtermsg", Static)
    return str(box.content) if str(box.styles.display) == "block" else None


def test_pilot_filter_not_success_under_a_name_pattern(short_root: Path) -> None:
    """DL-511's need: `!status=su =fl_*` shows exactly the jobs under the
    pattern that are not in SUCCESS, and the title shows that filter."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _FILTER_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 4)
                await _set_status(app, pilot, "fl_a", "SUCCESS")
                await _set_status(app, pilot, "fl_b", "FAILURE")
                await _set_status(app, pilot, "other_d", "FAILURE")

                await pilot.press("slash", *"!status=su =fl_*")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fl_b", "fl_c"])
                table = app.query_one("#jobs", DataTable)
                assert "/!status=su =fl_*/" in str(table.border_title)
                assert _filter_message(app) is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_a_refused_draft_keeps_the_filter_in_force(short_root: Path) -> None:
    """DL-511: a refused line leaves the filter in force, and the title with
    it, and shows its message under the line; a status answer still moves
    jobs through that filter; a good line again removes the message."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _FILTER_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 4)
                table = app.query_one("#jobs", DataTable)
                await _set_status(app, pilot, "fl_a", "SUCCESS")

                await pilot.press("slash", *"!status=su =fl_*")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fl_b", "fl_c"])

                await pilot.press("space", "~", "(")  # an unclosed group: refused
                await _wait_for_ui(pilot, lambda: _filter_message(app) is not None)
                assert (_filter_message(app) or "").startswith("~(: bad regular expression: ")
                assert app._filter == "!status=su =fl_*"
                assert app._table_sync.identity == ["fl_b", "fl_c"]
                assert "/!status=su =fl_*/" in str(table.border_title)
                assert "~(" not in str(table.border_title)

                # later status answers go through the filter in force
                await _set_status(app, pilot, "fl_c", "SUCCESS")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fl_b"])
                await _set_status(app, pilot, "fl_a", "FAILURE")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fl_a", "fl_b"])
                assert _filter_message(app) is not None

                await pilot.press("backspace")  # " ~": still refused, a new message
                await _wait_for_ui(pilot, lambda: _filter_message(app) == "~: empty pattern")
                await pilot.press("backspace")  # the good line again
                await _wait_for_ui(pilot, lambda: _filter_message(app) is None)
                assert app._filter == "!status=su =fl_*"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_enter_stays_on_a_refused_line_and_escape_clears_all(short_root: Path) -> None:
    """DL-511: Enter on a refused line stays in the input with its message;
    Escape clears the line, the filter in force and the message; Enter on
    an empty line clears, as Escape does."""
    tree_order = ["fl_a", "fl_b", "fl_c", "other_d"]

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _FILTER_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                table = app.query_one("#jobs", DataTable)
                line = app.query_one("#filterline", Input)

                await pilot.press("slash", *"fl status=s")
                await _wait_for_ui(pilot, lambda: _filter_message(app) is not None)
                assert _filter_message(app) == "status=s: s could be STARTING or SUCCESS"
                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is line
                assert str(line.styles.display) == "block"
                assert _filter_message(app) == "status=s: s could be STARTING or SUCCESS"

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                assert app.focused is table
                assert app._filter == "" and line.value == ""
                assert str(line.styles.display) == "none"
                assert _filter_message(app) is None

                # Enter on an emptied line clears and returns to the table
                await pilot.press("slash", *"=fl_a")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["fl_a"])
                await pilot.press(*["backspace"] * 5, "enter")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                assert app.focused is table and app._filter == ""
                assert str(line.styles.display) == "none"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_a_refused_draft_survives_a_focus_move_and_f1_shows_the_syntax(
    short_root: Path,
) -> None:
    """DL-511: a refused draft and its message stay on the open line when focus moves to the
    table without Enter or Escape, and the title keeps the filter in force; `/` returns to the
    draft, F1 there shows the filter line's syntax help, and Escape clears all three."""
    tree_order = ["fl_a", "fl_b", "fl_c", "other_d"]

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _FILTER_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                table = app.query_one("#jobs", DataTable)
                line = app.query_one("#filterline", Input)

                await pilot.press("slash", *"=fl_*")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order[:3])
                await pilot.press("space", "=")  # an empty pattern: refused
                await _wait_for_ui(pilot, lambda: _filter_message(app) == "=: empty pattern")

                table.focus()  # neither Enter nor Escape
                await pilot.pause()
                assert app.focused is table
                assert str(line.styles.display) == "block" and line.value == "=fl_* ="
                assert _filter_message(app) == "=: empty pattern"
                assert "/=fl_*/" in str(table.border_title)
                assert app._table_sync.identity == tree_order[:3]

                await pilot.press("slash")
                await pilot.pause()
                assert app.focused is line and line.value == "=fl_* ="
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: bool(app.screen.query(HelpPanel)))
                shown = app.screen.query_one(HelpPanel).query_one("#widget-help").source
                assert shown == textwrap.dedent(_FilterInput.HELP.rstrip())
                assert "`!status=su =core_*`" in shown
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: not app.screen.query(HelpPanel))

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == tree_order)
                assert app.focused is table and app._filter == "" and line.value == ""
                assert str(line.styles.display) == "none"
                assert _filter_message(app) is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


@pytest.mark.parametrize("words", ["status=te", "~^zz"], ids=["status", "regex"])
def test_pilot_a_filter_with_no_match_leaves_the_verb_keys_nothing_to_send(
    short_root: Path, words: str
) -> None:
    """DL-65's guard under DL-511: with zero matches no job is selected, so
    a verb key on the table sends nothing. A regular expression's zero
    comes from the render's bounded pass, as any other word's does (DL-514)."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _FILTER_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 4)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "fl_a"

                sent: list[dict] = []
                inner = app._client.request

                async def capture(request):
                    if request.get("cmd") == "sendevent":
                        sent.append(request)
                    return await inner(request)

                app._client.request = capture

                await pilot.press("slash", *words, "enter")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == [])
                assert app.focused is table
                assert app._selected is None
                await pilot.press("s", "h", "k")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert sent == []
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


#: a long, ordinary job name on which `([a-z_]+)+\d$` backtracks for minutes (DL-514)
_SLOW_NAME = "core_daily_load_customer_accounts_x"
_SLOW_WORD = r"~([a-z_]+)+\d$"
_SLOW_ESTATE = (
    f"insert_job: {_SLOW_NAME}\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: core_b\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: other_c\njob_type: c\ncommand: x\nmachine: m1\n"
)
_SLOW_ORDER = sorted([_SLOW_NAME, "core_b", "other_c"])


def test_pilot_dl514_a_draft_that_takes_too_long_is_refused(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514: a draft whose bounded pass is stopped is refused like a word
    that does not read. Its message shows, the filter in force keeps the
    table and the title, Enter stays in the line, and a draft that matches
    in time clears the message and takes force. The app answers keys after."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _SLOW_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SLOW_ORDER)
                table = app.query_one("#jobs", DataTable)
                line = app.query_one("#filterline", Input)
                core = sorted([_SLOW_NAME, "core_b"])

                await pilot.press("slash", *"=core_*")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == core)

                # the whole draft at once, as a paste: typed, its prefix `~([a-z_]+)` reads
                # well and would take force first
                monkeypatch.setattr(runner_tui, "_FILTER_LIMIT_S", 0.02)
                line.value = "=core_* " + _SLOW_WORD
                line.cursor_position = len(line.value)
                await _wait_for_ui(pilot, lambda: _filter_message(app) is not None)
                assert _filter_message(app) == (
                    "the filter took too long to match and is not applied"
                )
                assert app._filter == "=core_*"
                assert app._table_sync.identity == core
                assert "/=core_*/" in str(table.border_title)

                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is line
                assert str(line.styles.display) == "block"

                # the real limit again. The backspaces remove `$`; then `d`, leaving
                # `~([a-z_]+)+\d`, which still backtracks and meets a real stop; then `\`,
                # leaving a refused draft. `~([a-z_]+)+` then matches in time.
                monkeypatch.undo()
                await pilot.press("backspace", "backspace", "backspace")
                await _wait_for_ui(pilot, lambda: _filter_message(app) is None)
                assert app._filter == r"=core_* ~([a-z_]+)+"
                assert app._table_sync.identity == core

                await pilot.press("enter")  # the app answers: Enter now leaves the line
                await pilot.pause()
                assert app.focused is table
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl514_a_filter_stopped_on_a_status_answer_is_cleared_once(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514: a filter in force matches in time while no job is in SUCCESS,
    since its status word answers first. A status answer that puts the long
    name in SUCCESS stops its pass: the line, the text and the predicate are
    cleared once, the console says so, and the table shows every job."""

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", _SLOW_ESTATE)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SLOW_ORDER)
                table = app.query_one("#jobs", DataTable)
                line = app.query_one("#filterline", Input)
                console = app.query_one("#console", RichLog)
                words = "status=su " + _SLOW_WORD

                await pilot.press("slash", *words, "enter")
                await _wait_for_ui(pilot, lambda: app._filter == words)
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == [])
                assert app.focused is table

                monkeypatch.setattr(runner_tui, "_FILTER_LIMIT_S", 0.02)
                await _set_status(app, pilot, _SLOW_NAME, "SUCCESS")
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == _SLOW_ORDER)
                assert app._filter == "" and app._job_filter.tests == ()
                assert line.value == "" and str(line.styles.display) == "none"
                assert _filter_message(app) is None
                assert str(table.border_title) == "jobs 3/3"
                note = re.sub(r"\s+", "", f"filter /{words}/ took too long to match; cleared")
                assert _console_text(console).count(note) == 1

                await _set_status(app, pilot, "core_b", "SUCCESS")  # a later answer
                assert _console_text(console).count(note) == 1
                assert app._table_sync.identity == _SLOW_ORDER
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_v_cycles_all_problems_active_and_back_to_all(short_root: Path) -> None:
    """DL-65 view cycle: `v` -> problems (FAILURE/TERMINATED/QUE_WAIT or
    alarmed only), `v` -> active (STARTING/RUNNING/QUE_WAIT: none here),
    `v` -> back to all."""
    text = (
        "insert_job: vc_bad\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: vc_ok\njob_type: c\ncommand: x\nmachine: m1\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 2)
                table = app.query_one("#jobs", DataTable)

                cmdline = app.query_one("#cmdline", Input)
                cmdline.focus()
                cmdline.value = "CHANGE_STATUS vc_bad FAILURE 1"
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("vc_bad", "status")) == "FAILURE"
                )

                table.focus()
                await pilot.press("v")  # problems: only the FAILURE row stays
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["vc_bad"])
                assert "problems" in str(table.border_title)

                await pilot.press("v")  # active: nothing STARTING/RUNNING/QUE_WAIT
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == [])
                await pilot.press("v")  # back to all
                await _wait_for_ui(pilot, lambda: app._table_sync.identity == ["vc_bad", "vc_ok"])
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_colon_focuses_the_event_console(short_root: Path) -> None:
    """DL-65 rebind: the console moved from `/` (now the filter) to `:`."""
    text = "insert_job: cr_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "cr_job" in app._rows)
                await pilot.press("colon")
                await pilot.pause()
                assert app.focused is not None and app.focused.id == "cmdline"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def _queue_keys(app: RunnerApp, *keys: tuple[str, str | None]) -> None:
    """Post `(key, character)` events to the app with no await between them, so none is
    handled before the next is queued: the shape of a paste or of a browser's key burst."""
    for key, character in keys:
        app.post_message(events.Key(key, character))


def _letters(word: str) -> list[tuple[str, str | None]]:
    return [(c, c) for c in word]


async def _settle(pilot) -> None:
    """Let a queued burst be handled and any verb it started reach the engine."""
    await pilot.pause()
    await asyncio.sleep(0.3)
    await pilot.pause()


async def _trace_length(server: ControlServer) -> int:
    client = ControlClient(server.path)
    try:
        return len((await client.request({"cmd": "trace", "since": 0}))["entries"])
    finally:
        await client.close()


def test_pilot_bug95_a_burst_after_colon_sends_no_verb(short_root: Path) -> None:
    """BUG-95: the `s` of a `start` queued behind `:` does not reach the table's STARTJOB for
    the selected job; focus is on the console line and nothing was sent."""
    text = "insert_job: bu_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "bu_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "bu_job"
                before = await _trace_length(server)

                _queue_keys(app, ("colon", ":"), *_letters("start"))
                await _settle(pilot)

                assert app.focused is app.query_one("#cmdline", Input)
                assert await _trace_length(server) == before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_bug95_a_burst_after_slash_sends_no_verb(short_root: Path) -> None:
    """BUG-95: the `s` of a `su` queued behind `/` does not reach STARTJOB; focus is on the
    filter line and nothing was sent."""
    text = "insert_job: bf_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "bf_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "bf_job"
                before = await _trace_length(server)

                _queue_keys(app, ("slash", "/"), *_letters("su"))
                await _settle(pilot)

                assert app.focused is app.query_one("#filterline", Input)
                assert await _trace_length(server) == before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_bug95_a_burst_after_the_pager_slash_keeps_focus_on_the_prompt(
    short_root: Path,
) -> None:
    """BUG-95: with the log focused, the `q` queued behind `/` does not leave the pager; focus
    is on the prompt."""
    text = "insert_job: bp_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING; the test owns the log
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                pager = await _seed_pager(pilot, app, "bp_job", ["one", "two"])
                pager.focus()
                await pilot.pause()
                assert app.focused is pager

                _queue_keys(app, ("slash", "/"), ("q", "q"))
                await _settle(pilot)

                assert app.focused is app.query_one("#logsearch", Input)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_bug95_a_burst_after_m_sends_no_verb(short_root: Path) -> None:
    """BUG-95: the `s` queued behind `m` does not reach the table's STARTJOB for the selected
    job; focus is on the log and nothing was sent."""
    text = "insert_job: bm_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "bm_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "bm_job"
                before = await _trace_length(server)

                _queue_keys(app, ("m", "m"), ("s", "s"))
                await _settle(pilot)

                assert app.focused is app.query_one("#logtail", _LogTail)
                assert await _trace_length(server) == before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


async def _select_job(pilot, app: RunnerApp, job: str) -> None:
    await _wait_for_ui(pilot, lambda: job in app._rows)
    table = app.query_one("#jobs", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.pause()
    assert app._selected == job


def _run_selected(short_root: Path, job: str, body, *, owns_run: bool = False) -> None:
    """Serve one job, select it on the real app, and run `body(app, pilot, server)`."""
    text = f"insert_job: {job}\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path, owns_run=owns_run)
            async with app.run_test(size=(120, 40)) as pilot:
                await _select_job(pilot, app, job)
                await body(app, pilot, server)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("opening", "queued"), [("k", "n"), ("f", "i"), ("k", "s")], ids=["k-n", "f-i", "k-s"]
)
def test_pilot_dl518_a_key_queued_behind_k_or_f_sends_nothing_and_the_confirm_opens(
    short_root: Path, opening: str, queued: str
) -> None:
    """DL-518 rule 2: the confirm opens from a worker, so the `n` meant as its "no" would
    reach the table's ON_NOEXEC first. Nothing is sent and the confirm opens."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        _queue_keys(app, (opening, opening), (queued, queued))
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert await _trace_length(server) == before

    _run_selected(short_root, "dl518_c", body)


def test_pilot_dl518_a_detached_viewer_quits_on_a_key_queued_behind_k(short_root: Path) -> None:
    """DL-518: quit is never held back. `q` queued behind `k` leaves the viewer, and nothing
    was sent."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        _queue_keys(app, ("k", "k"), ("q", "q"))
        await asyncio.sleep(0.5)
        assert app._exit is True
        assert await _trace_length(server) == before

    _run_selected(short_root, "dl518_q", body)


@pytest.mark.parametrize(
    ("opening", "quitting"),
    [("d", "q"), ("k", "q"), ("d", "ctrl+q"), ("k", "ctrl+q")],
    ids=["d-q", "k-q", "d-ctrlq", "k-ctrlq"],
)
def test_pilot_dl518_a_detached_viewer_quits_while_the_engine_does_not_answer(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, opening: str, quitting: str
) -> None:
    """DL-518: a request that never answers parks the popup or the confirm that was opening;
    the viewer can still be left, by `q` and by Ctrl+Q."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        async def never(request: dict[str, Any]) -> dict[str, Any]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def never_pinned(key: str) -> tuple[str, int]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(app._client, "request", never)
        monkeypatch.setattr(app, "_precondition", never_pinned)
        _queue_keys(app, (opening, opening))
        await asyncio.sleep(0.2)
        assert app._opening is not None  # the request is parked, the verb keys are held
        _queue_keys(app, (quitting, None))
        await asyncio.sleep(0.5)
        assert app._exit is True

    _run_selected(short_root, "dl518_w", body)


def test_pilot_dl518_a_key_queued_behind_d_sends_nothing_and_the_details_open(
    short_root: Path,
) -> None:
    """DL-518 rule 2: the details popup opens after four round trips; the `s` queued behind
    `d` is not STARTJOB."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        _queue_keys(app, ("d", "d"), ("s", "s"))
        await _settle(pilot)
        assert isinstance(app.screen, SpecScreen)
        assert await _trace_length(server) == before

    _run_selected(short_root, "dl518_d", body)


def test_pilot_dl518_a_key_queued_behind_the_quit_that_asks_sends_nothing(
    short_root: Path,
) -> None:
    """DL-518 rule 2, `run --ui`: the quit confirm opens from a worker; the `h` queued behind
    `q` is not ON_HOLD, and the confirm opens."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        _queue_keys(app, ("q", "q"), ("h", "h"))
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert await _trace_length(server) == before

    _run_selected(short_root, "dl518_r", body, owns_run=True)


@pytest.mark.parametrize("closing", ["n", "escape"])
def test_pilot_dl518_the_verb_keys_work_again_once_the_confirm_is_closed(
    short_root: Path, closing: str
) -> None:
    """DL-518 rule 2: the hold ends with the dialog. After it closes, `h` sends ON_HOLD."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        await pilot.press("k")
        await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press(closing)
        await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        await _settle(pilot)
        assert app._opening is None
        assert await _trace_length(server) == before
        await pilot.press("h")

        async def sent() -> bool:
            return await _trace_length(server) > before

        await _wait_for_async(sent)

    _run_selected(short_root, "dl518_x", body)


def test_pilot_dl518_the_verb_keys_and_d_work_again_after_the_details_popup_closes(
    short_root: Path,
) -> None:
    """DL-518: the hold ends when the popup opens. After it closes, `h` sends and `d` opens
    the popup again."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        await pilot.press("d")
        await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
        await pilot.press("escape")
        await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
        assert app._opening is None
        await pilot.press("h")

        async def sent() -> bool:
            return await _trace_length(server) > before

        await _wait_for_async(sent)
        await pilot.press("d")
        await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))

    _run_selected(short_root, "dl518_p", body)


def test_pilot_dl518_the_verb_keys_work_again_after_a_confirmed_send(short_root: Path) -> None:
    """DL-518: the hold ends when the confirm opens, not when the send is answered. After
    `k`, `y` a verb key sends."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        await pilot.press("k")
        await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert app._opening is None
        await pilot.press("y")
        await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        await _settle(pilot)
        before = await _trace_length(server)
        await pilot.press("h")

        async def sent() -> bool:
            return await _trace_length(server) > before

        await _wait_for_async(sent)

    _run_selected(short_root, "dl518_y", body)


def test_pilot_dl518_the_verb_keys_work_again_after_a_cancelled_quit_that_asks(
    short_root: Path,
) -> None:
    """DL-518: after the quit confirm is cancelled, `q` asks again and a verb key sends."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        for closing in ("n", "escape"):
            await pilot.press("q")
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press(closing)
            await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            await _settle(pilot)
            assert app._opening is None and app._exit is False
        await pilot.press("h")

        async def sent() -> bool:
            return await _trace_length(server) > before

        await _wait_for_async(sent)

    _run_selected(short_root, "dl518_n", body, owns_run=True)


def test_pilot_dl518_the_verb_keys_work_again_after_a_refused_precondition(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-518 rule 2: a confirm that is refused before it opens ends the hold too."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        async def unavailable(key: str) -> tuple[str, int]:
            raise ControlClientError("engine unavailable")

        before = await _trace_length(server)
        with monkeypatch.context() as refused:
            refused.setattr(app, "_precondition", unavailable)
            await pilot.press("k")
            await _settle(pilot)
        assert app._opening is None
        assert not isinstance(app.screen, ConfirmScreen)
        await pilot.press("h")

        async def sent() -> bool:
            return await _trace_length(server) > before

        await _wait_for_async(sent)

    _run_selected(short_root, "dl518_u", body)


def test_pilot_dl518_tab_in_the_filter_line_stays_in_the_line(short_root: Path) -> None:
    """DL-518 rule 3: Tab in the filter line rings the bell and does not leave; the `s` typed
    after it is in the line, not STARTJOB."""

    async def body(app: RunnerApp, pilot, server: ControlServer) -> None:
        before = await _trace_length(server)
        await pilot.press("slash")
        await pilot.press("r", "t", "tab", "s")
        await pilot.pause()
        line = app.query_one("#filterline", Input)
        assert app.focused is line and line.value == "rts"
        assert await _trace_length(server) == before

    _run_selected(short_root, "dl518_f", body)


def test_pilot_details_popup_shows_needs_blocks_and_a_log_tail(short_root: Path) -> None:
    """DL-65 popup additions on top of DL-64's spec block: the `deps` verb's
    needs:/blocks: lines (upstream condition entities / referencing jobs)
    and a short local log tail of the job's resolved log_out -- an explicit
    std_out_file is read from the latest dispatch (DL-447)."""
    out_path = short_root / "dp_b.out"
    text = (
        "insert_job: dp_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: dp_b\njob_type: c\ncommand: y\nmachine: m1\n"
        f"condition: s(dp_a)\nstd_out_file: {out_path}\n\n"
        "insert_job: dp_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(dp_b)\n"
    )
    block = "insert_job: dp_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(dp_a)\n"

    async def scenario() -> None:
        out_path.write_text("first line\nlast fake log line\n")
        engine, server, loop_task = await _serve(
            short_root / "run", text, spec_texts={"dp_b": block}
        )
        try:
            engine.inject(
                Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": "dp_b"})
            )
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(
                    pilot,
                    lambda: (
                        len(app._rows) == 3
                        and app._log_paths.get("dp_b", (None, None))[0] == str(out_path)
                    ),
                )
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=1)  # alpha-sorted: dp_a, dp_b, dp_c
                await pilot.pause()
                assert app._selected == "dp_b"

                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                body = str(app.screen.query_one("#specbox Static", Static).content)
                assert "needs:   dp_a" in body
                assert "blocks:  dp_c" in body
                assert "command: y" in body  # the spec block still renders
                assert "log tail:" in body
                assert "last fake log line" in body
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ----------------------- 5. DL-67 log pager (the zoomed/focused log tail = less)


def test_compile_search_smartcase_and_bad_pattern() -> None:
    """Pager patterns are regex with smartcase (rg/vim): case-insensitive
    unless the pattern itself carries an uppercase letter; a broken regex
    comes back as an error STRING for the prompt to render, never a raise."""
    lower = compile_search("error")
    assert not isinstance(lower, str)
    assert lower.search("an ERROR line")
    upper = compile_search("Error")
    assert not isinstance(upper, str)
    assert upper.search("Error!") and not upper.search("error!")
    bad = compile_search("(")
    assert isinstance(bad, str) and bad.startswith("bad pattern:")


def test_pager_bindings_shadow_or_allowlist_every_app_key() -> None:
    """Drift guard: while the pager has focus its bindings shadow the app's,
    so every key the app binds must be either remapped/neutralized by the
    pager or on the DELIBERATE pass-through allowlist. A new app binding
    that lands in neither set would silently fire operator actions from
    inside the pager -- the exact bug class DL-67 removed."""
    pass_through = {
        "m",  # zoom toggle
        "o",  # out/err stream toggle -- log-scoped
        "r",  # refresh: harmless, keeps the tail's source fresh
        "t",  # triggers view: a read-only modal (DL-68), estate untouched
        "f1",  # help panel (F6): `?` is already shadowed for reverse search
        "]",
        "[",
        "}",
        "{",  # pane shares
        "plus",
        "equals_sign",
        "minus",  # the focused pane's size, here the log's (DL-520)
        "E",  # the page of stored events before the pager's first (DL-541)
    }

    def keys(bindings) -> set[str]:
        # tuple-form bindings are legal textual; a guard that skipped them
        # would have a hole exactly where a careless future binding lands
        return {b.key if isinstance(b, Binding) else b[0] for b in bindings}

    # the operator verbs moved to _JobsTable (DL-187), where focus already
    # gates them -- but the union keeps this guard at the coverage it had:
    # the pager shadows them too, defense in depth
    bound = keys(RunnerApp.BINDINGS) | keys(_JobsTable.BINDINGS)
    leaks = bound - keys(_LogTail.BINDINGS) - pass_through
    assert not leaks, f"app keys reachable from the focused pager: {sorted(leaks)}"


async def _seed_pager(pilot, app: RunnerApp, job: str, lines: list[str]) -> _LogTail:
    """Select the job, start it (the one legitimate '> STARTJOB' echo), then
    write `lines` to its resolved log_out and wait for the tail to show them
    -- the test_pilot_log_tail technique, shared by every pager test."""
    await _wait_for_ui(pilot, lambda: job in app._rows)
    table = app.query_one("#jobs", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.pause()
    assert app._selected == job
    await pilot.press("s")
    await _wait_for_ui(pilot, lambda: (app._log_paths.get(job) or (None, None))[0] is not None)
    _append_log(app, job, lines)
    pager = app.query_one("#logtail", _LogTail)
    await _wait_for_ui(pilot, lambda: len(pager._buffer) >= len(lines))
    return pager


def _append_log(app: RunnerApp, job: str, lines: list[str]) -> None:
    path = app._log_paths[job][0]
    assert path is not None
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "ab") as f:
        f.write(("\n".join(lines) + "\n").encode())


def test_pilot_zoomed_slash_searches_the_log_and_escape_cancels_only_the_prompt(
    short_root: Path,
) -> None:
    """The DL-67 headline: with the log maximized, `/` opens the PAGER's
    search prompt (visible inside the maximized pane), never the hidden
    tree filter; Enter finds; Escape in the prompt cancels the prompt and
    stays zoomed -- textual's escape-to-minimize special case would swallow
    it first, which is why the app turns that off."""
    text = "insert_job: pg_s\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING; the test owns the log
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                lines = [f"line {i:04d}" for i in range(50)]
                lines[10] = "needle here 10"
                lines[30] = "needle here 30"
                pager = await _seed_pager(pilot, app, "pg_s", lines)

                await pilot.press("m")
                await pilot.pause()
                assert app.focused is pager

                await pilot.press("slash")
                await pilot.pause()
                assert app.focused is not None and app.focused.id == "logsearch"
                filterline = app.query_one("#filterline", Input)
                assert str(filterline.styles.display) == "none"  # the old bug

                await pilot.press(*"needle")
                await pilot.press("enter")
                await pilot.pause()
                assert app.focused is pager  # prompt closed, back to the log
                assert pager._matches == [10, 30]
                assert "/needle/" in pager._title_cache
                assert app._filter == ""  # the jobs tree never felt a thing

                await pilot.press("slash")
                await pilot.pause()
                await pilot.press("escape")  # cancel the PROMPT...
                await pilot.pause()
                assert app.screen.maximized is not None  # ...not the zoom
                assert app.focused is pager

                await pilot.press("escape")  # escape on the log DOES leave
                await pilot.pause()
                assert app.screen.maximized is None
                assert app.focused is app.query_one("#jobs", DataTable)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pager_motion_keys_scroll_and_never_reach_the_operator_verbs(
    short_root: Path,
) -> None:
    """The hazard DL-67 exists to remove: less/vim muscle memory inside the
    pager must move the VIEW, never the estate. `k` scrolls up (never
    KILLJOB), `f`/`b` page (never FORCE_STARTJOB), the no-pager-meaning
    verbs ring the bell and do nothing, `:` cannot focus the hidden console,
    and `q` leaves the pager, not the app. Every sendevent echoes to the
    console, so 'no new echo' is proof of 'no verb fired'."""
    text = "insert_job: pg_k\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                pager = await _seed_pager(pilot, app, "pg_k", [f"line {i:04d}" for i in range(200)])
                console = app.query_one("#console", RichLog)

                await pilot.press("m")
                await pilot.pause()
                assert pager.is_vertical_scroll_end  # fresh tail follows
                bottom = pager.scroll_y
                assert bottom > 0

                await pilot.press("k")  # less: up one line. NEVER KILLJOB.
                await pilot.pause()
                assert pager.scroll_y == bottom - 1
                await pilot.press("b")  # page up
                await pilot.pause()
                assert pager.scroll_y < bottom - 1
                await pilot.press("f")  # less: page forward. NEVER FORCE.
                await pilot.pause()
                assert pager.scroll_y > bottom - 1 - pager.scrollable_content_region.height

                echoes_before = sum(1 for ln in console.lines if ln.text.startswith("> "))
                await pilot.press("s", "i", "h", "v", "colon")
                await pilot.pause()
                assert app.focused is pager  # `:` did not focus the hidden console
                echoes_after = sum(1 for ln in console.lines if ln.text.startswith("> "))
                assert echoes_after == echoes_before  # paging mutated nothing
                assert not any("KILLJOB" in ln.text or "FORCE" in ln.text for ln in console.lines)

                await pilot.press("q")  # less muscle memory: leave the PAGER
                await pilot.pause()
                assert app.screen.maximized is None
                assert app.focused is app.query_one("#jobs", DataTable)
                assert not app._exit  # the app itself is alive
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pager_search_jump_n_wraps_and_question_mark_reverses(short_root: Path) -> None:
    """less search semantics: `/` jumps to the first match after the current
    top row (wrapping when none), `n` repeats in the search direction with
    wraparound, `N` opposes, `?` searches backward and flips what `n`
    means, and an empty submit clears the needle without moving the view."""
    text = "insert_job: pg_n\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                lines = [f"line {i:04d}" for i in range(200)]
                for row in (5, 50, 120):
                    lines[row] = f"mark {row}"
                pager = await _seed_pager(pilot, app, "pg_n", lines)

                await pilot.press("m")
                await pilot.pause()
                assert pager.scroll_y > 120  # following: every mark is above

                await pilot.press("slash", *"mark", "enter")
                await pilot.pause()
                assert pager._matches == [5, 50, 120]
                assert pager._match_pos == 0  # nothing below: wrapped to first
                assert pager.scroll_y == 5  # match at top, like less
                assert "/mark/ 1/3" in pager._title_cache

                await pilot.press("n")
                await pilot.pause()
                assert (pager._match_pos, pager.scroll_y) == (1, 50)
                await pilot.press("n", "n")
                await pilot.pause()
                assert pager._match_pos == 0  # 120, then wrap to 5
                await pilot.press("N")
                await pilot.pause()
                assert pager._match_pos == 2  # N opposes: wrap straight back

                await pilot.press("slash", "enter")  # empty submit clears
                await pilot.pause()
                assert pager._needle is None
                assert "/mark/" not in pager._title_cache
                anchored = pager.scroll_y  # de-highlight must not move the view
                assert anchored == 120

                await pilot.press("question_mark", *"mark", "enter")
                await pilot.pause()
                assert (pager._match_pos, pager.scroll_y) == (1, 50)  # last BELOW top
                await pilot.press("n")  # n follows the ? direction: upward
                await pilot.pause()
                assert (pager._match_pos, pager.scroll_y) == (0, 5)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pager_ampersand_line_filter_is_a_view_and_bad_patterns_hold_the_prompt(
    short_root: Path,
) -> None:
    """less's `&`: show only matching lines. It is a VIEW of the buffer --
    lines appended while filtered join the view when they match, an empty
    submit restores everything -- and a broken regex keeps the prompt open
    with the error on its border instead of half-applying."""
    text = "insert_job: pg_f\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                lines = [f"line {i:04d}" for i in range(60)]
                for row in (7, 21, 40):
                    lines[row] = f"ERROR at {row}"
                pager = await _seed_pager(pilot, app, "pg_f", lines)

                await pilot.press("m")
                await pilot.pause()
                await pilot.press("ampersand", *"ERROR", "enter")
                await pilot.pause()
                assert len(pager._display) == 3
                assert len(pager.lines) == 3  # the widget shows exactly the view
                assert "&ERROR" in pager._title_cache

                _append_log(app, "pg_f", ["plain tail line", "ERROR at tail"])
                await _wait_for_ui(pilot, lambda: len(pager._display) == 4)
                assert len(pager._buffer) == 62  # the buffer lost nothing

                await pilot.press("ampersand", "enter")  # empty clears the view
                await _wait_for_ui(pilot, lambda: len(pager._display) == 62)

                await pilot.press("ampersand", "(", "enter")  # broken regex
                await pilot.pause()
                prompt = app.query_one("#logsearch", Input)
                assert app.focused is prompt  # still open, value intact
                assert prompt.value == "("
                assert "bad pattern" in str(prompt.border_title)
                await pilot.press("escape")
                await pilot.pause()
                assert app.focused is pager
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl514_a_pager_pattern_that_takes_too_long_is_refused_at_the_prompt(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514: a filter or a search whose bounded pass is stopped is refused
    through the prompt's error path. The view, and the filter in force, stay."""
    text = "insert_job: pg_x\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                lines = [f"line {i:04d}" for i in range(40)]
                lines[12] = f"loaded {_SLOW_NAME}"
                lines[30] = "loaded core_b"
                pager = await _seed_pager(pilot, app, "pg_x", lines)
                prompt = app.query_one("#logsearch", Input)

                await pilot.press("m")
                await pilot.pause()
                await pilot.press("ampersand", *"loaded", "enter")
                await pilot.pause()
                assert pager._display == [lines[12], lines[30]]

                monkeypatch.setattr(runner_tui, "_PAGER_LIMIT_S", 0.02)
                slow = _SLOW_WORD[1:]
                for verb in ("ampersand", "slash"):
                    await pilot.press(verb, *slow, "enter")
                    await pilot.pause()
                    assert app.focused is prompt and prompt.value == slow
                    assert "the pattern took too long to match" in str(prompt.border_title)
                    assert pager._display == [lines[12], lines[30]]
                    assert len(pager.lines) == 2
                    assert pager._line_filter_text == "loaded"
                    assert pager._needle is None and pager._matches == []
                    await pilot.press("escape")
                    await pilot.pause()
                    assert app.focused is pager

                await pilot.press("slash", "(", "enter")  # a search that does not compile
                await pilot.pause()
                assert "bad pattern" in str(prompt.border_title)
                assert pager._needle is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_a_batch_past_the_cap_trims_the_view_and_shifts_the_matches(
    short_root: Path,
) -> None:
    """A batch that carries the view past `_PAGER_BUFFER_LINES` trims its
    oldest rows as the widget does, shifts the search matches, and keeps a
    paused view where it was. The batch's rows come from one bounded pass."""
    text = "insert_job: pg_z\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "pg_z" in app._rows)
                # the tail has taken its source, so no later source change empties the buffer
                await _wait_for_ui(pilot, lambda: app._tail_key is not None)
                pager = app.query_one("#logtail", _LogTail)
                pager.feed(["hit 0"] + [f"line {i}" for i in range(1, 100)])
                assert pager.apply_search("hit", backward=False) is None
                assert (pager._matches, pager._match_pos) == ([0], 0)
                await pilot.pause()
                pager.scroll_home(animate=False)
                await _wait_for_ui(pilot, lambda: not pager.is_vertical_scroll_end)

                pager.feed(
                    [f"hit {i}" if i % 1000 == 0 else f"line {i}" for i in range(100, 10_100)]
                )
                assert len(pager._display) == 10_000
                assert pager._display[0] == "line 100"
                assert pager._matches == [i - 100 for i in range(1000, 10_100, 1000)]
                assert pager._match_pos == 0
                assert len(pager._buffer) == 10_000

                assert pager.apply_search("", backward=False) is None  # no search: no position
                pager.feed([f"tail {i}" for i in range(10)])
                assert pager._display[0] == "line 110" and pager._display[-1] == "tail 9"
                assert (pager._matches, pager._match_pos) == ([], None)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl514_new_lines_that_stop_the_filter_in_force_clear_it(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514: a filter in force matches the seeded lines in time; a batch
    with the long name stops its pass. The filter is cleared once, the title
    says so, and every line of the batch is in the view."""
    text = "insert_job: pg_y\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                seeded = ["start", "job_7", "end ok"]
                pager = await _seed_pager(pilot, app, "pg_y", seeded)

                await pilot.press("m")
                await pilot.pause()
                await pilot.press("ampersand", *_SLOW_WORD[1:], "enter")
                await pilot.pause()
                assert pager._display == ["job_7"]

                monkeypatch.setattr(runner_tui, "_PAGER_LIMIT_S", 0.02)
                batch = ["plain", _SLOW_NAME, "job_8"]
                _append_log(app, "pg_y", batch)
                await _wait_for_ui(pilot, lambda: len(pager._buffer) == 6)
                await _wait_for_ui(pilot, lambda: pager._line_filter is None)
                assert pager._line_filter_text == "" and pager._needle is None
                assert pager._display == seeded + batch
                assert len(pager.lines) == 6
                await _wait_for_ui(pilot, lambda: "search and filter cleared" in pager._title_cache)
                assert "&" not in pager._title_cache

                # the next pattern applied in time removes the note
                monkeypatch.undo()
                await pilot.press("slash", *"job", "enter")
                await pilot.pause()
                assert pager._matches == [1, 5]
                assert "cleared" not in pager._title_cache
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl514_a_stop_on_new_lines_keeps_a_paused_view_and_its_note_leaves_with_the_log(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514: when new lines stop the filter in force under a paused view,
    the rebuilt view keeps its place instead of jumping to the tail. The
    title's note belongs to that log: a change of stream drops it."""
    text = "insert_job: pg_p\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                seeded = [f"step_{chr(97 + i % 26)}{i % 10}" for i in range(100)]
                pager = await _seed_pager(pilot, app, "pg_p", seeded)

                await pilot.press("m")
                await pilot.pause()
                await pilot.press("ampersand", *_SLOW_WORD[1:], "enter")
                await pilot.pause()
                assert pager._display == seeded
                await pilot.press("g")  # top: the view pauses
                await _wait_for_ui(pilot, lambda: "[paused]" in pager._title_cache)

                monkeypatch.setattr(runner_tui, "_PAGER_LIMIT_S", 0.02)
                _append_log(app, "pg_p", ["plain", _SLOW_NAME])
                await _wait_for_ui(pilot, lambda: pager._line_filter is None)
                assert pager._display == [*seeded, "plain", _SLOW_NAME]
                await _wait_for_ui(pilot, lambda: "search and filter cleared" in pager._title_cache)
                assert pager.scroll_y == 0 and not pager.is_vertical_scroll_end

                await pilot.press("o")  # the other stream: a different log
                await _wait_for_ui(pilot, lambda: "log (err)" in pager._title_cache)
                assert "cleared" not in pager._title_cache
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl514_a_search_does_not_run_the_filter_in_force_again(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-514 at the pager's real limit: `&.*error` over 10 000 buffered lines
    of 200 characters is accepted at the prompt. Under a limit too small for
    that filter's pass over the buffer, a new search and the clearing of a
    search are still accepted, since they match only the lines the filter
    shows; the same filter given again is refused."""
    text = "insert_job: pg_e\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "pg_e" in app._rows)
                # the tail has taken its source, so no later source change empties the buffer
                await _wait_for_ui(pilot, lambda: app._tail_key is not None)
                pager = app.query_one("#logtail", _LogTail)
                prompt = app.query_one("#logsearch", Input)
                lines = [
                    (f"2026-11-17T10:00:00 job_{i:05d} step {i % 7} " + "x" * 200)[:200]
                    for i in range(10_000)
                ]
                for i in range(0, 10_000, 100):
                    lines[i] = (f"2026-11-17T10:00:00 job_{i:05d} error code {i} " + "y" * 200)[
                        :200
                    ]
                pager.feed(lines)
                assert len(pager._buffer) == 10_000

                await pilot.press("m")
                await pilot.pause()
                await pilot.press("ampersand", *".*error", "enter")
                await pilot.pause()
                assert app.focused is pager
                assert pager._line_filter_text == ".*error"
                assert len(pager._display) == 100

                monkeypatch.setattr(runner_tui, "_PAGER_LIMIT_S", 0.02)
                await pilot.press("slash", *"code", "enter")
                await pilot.pause()
                assert app.focused is pager
                assert len(pager._matches) == 100 and pager._needle_text == "code"
                await pilot.press("slash", "enter")  # clearing the search
                await pilot.pause()
                assert app.focused is pager and pager._needle is None

                await pilot.press("ampersand", *".*error", "enter")  # the full pass again
                await pilot.pause()
                assert app.focused is prompt
                assert "the pattern took too long to match" in str(prompt.border_title)
                assert len(pager._display) == 100
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_pager_follow_is_pinned_at_bottom_and_capital_f_resumes(short_root: Path) -> None:
    """Follow semantics: pinned at the bottom the tail sticks through new
    appends; any scroll up pauses it ([paused] in the title) and appends no
    longer move the view; `F` jumps back to the end and follow resumes."""
    text = "insert_job: pg_w\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                pager = await _seed_pager(pilot, app, "pg_w", [f"line {i:04d}" for i in range(100)])

                await pilot.press("m")
                await pilot.pause()
                assert pager.is_vertical_scroll_end

                _append_log(app, "pg_w", [f"tail {i}" for i in range(20)])
                await _wait_for_ui(pilot, lambda: len(pager._buffer) == 120)
                # the buffer arriving and the tail scrolling settle in
                # different frames, so the scroll is waited for, not asserted
                # on the frame the buffer landed in
                await _wait_for_ui(pilot, lambda: pager.is_vertical_scroll_end)

                await pilot.press("g")  # top -> paused
                await pilot.pause()
                assert pager.scroll_y == 0
                _append_log(app, "pg_w", [f"more {i}" for i in range(20)])
                await _wait_for_ui(pilot, lambda: len(pager._buffer) == 140)
                assert pager.scroll_y == 0  # the operator's place held still
                await _wait_for_ui(pilot, lambda: "[paused]" in pager._title_cache)

                await pilot.press("F")
                await pilot.pause()
                assert pager.is_vertical_scroll_end
                _append_log(app, "pg_w", ["one more"])
                await _wait_for_ui(pilot, lambda: len(pager._buffer) == 141)
                await _wait_for_ui(pilot, lambda: pager.is_vertical_scroll_end)  # following
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_log_tail_permission_fault_is_reported_once_and_recovers(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10 (DL-187 item 10): a read fault that is NOT the ordinary missing-
    file wait (a permission error, deterministic across macOS and Linux CI
    via a monkeypatched os.stat) writes one red console line per distinct
    (path, error) -- never once per poll tick -- and a later successful
    read clears the memo silently (no recovery line was asked for)."""
    import os

    text = "insert_job: lt_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING; the test owns the log
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _seed_pager(pilot, app, "lt_run", ["line 0"])
                path = app._log_paths["lt_run"][0]
                assert path is not None

                real_stat = os.stat
                state = {"fail": True}

                def flaky_stat(target, *args, **kwargs):
                    if state["fail"] and str(target) == path:
                        raise PermissionError(13, "Permission denied", path)
                    return real_stat(target, *args, **kwargs)

                monkeypatch.setattr(os, "stat", flaky_stat)
                console = app.query_one("#console", RichLog)

                def faults() -> int:
                    return sum(1 for ln in console.lines if f"log tail {path}:" in ln.text)

                await _wait_for_ui(pilot, lambda: faults() == 1)
                await pilot.pause()
                assert faults() == 1  # a repeat tick does not repeat the line
                assert app._tail_fault == (path, "[Errno 13] Permission denied: " + repr(path))

                state["fail"] = False
                await _wait_for_ui(pilot, lambda: app._tail_fault is None)
                assert faults() == 1  # the recovery clears the memo without a new line
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_log_tail_open_fault_dedupes_even_while_stat_keeps_succeeding(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (review MAJOR): the memo used to be cleared right after a
    successful `os.stat`, before `open`/`read` ran. A fault that lives in
    open() (stat keeps succeeding, open keeps denying -- the realistic
    Unix shape when directory search is allowed but file read is not)
    would then be wiped and immediately re-set every 0.5s tick, spamming
    the console instead of reporting the one line the contract promises."""
    from dsl41 import runner_tui

    text = "insert_job: lo_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING; the test owns the log
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _seed_pager(pilot, app, "lo_run", ["line 0"])
                path = app._log_paths["lo_run"][0]
                assert path is not None

                real_open = open

                def flaky_open(target, *args, **kwargs):
                    if str(target) == path:
                        raise PermissionError(13, "Permission denied", path)
                    return real_open(target, *args, **kwargs)

                # os.stat is left alone -- only open() denies, every call
                monkeypatch.setattr(runner_tui, "open", flaky_open, raising=False)
                console = app.query_one("#console", RichLog)

                def faults() -> int:
                    return sum(1 for ln in console.lines if f"log tail {path}:" in ln.text)

                # a pending, never-consumed delta: with open() denied, the
                # tail position never advances, so every tick keeps retrying
                # open() against the SAME unread bytes (a real spam vector)
                _append_log(app, "lo_run", ["line 1"])
                await _wait_for_ui(pilot, lambda: faults() == 1)
                for _ in range(10):  # pump several more 0.5s ticks, not a fixed wall-clock wait
                    await pilot.pause()
                assert faults() == 1  # still one line, not a flood
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# -------------------- 6. DL-68 triggers view (t: due-ordered timers + armed latch)


def test_pilot_header_reads_all_19_characters_at_80_columns(
    short_root: Path, monkeypatch, local_timezone
) -> None:
    import dsl41.runner_tui as tui

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 1, 23, 30, 45, tzinfo=tz)

    monkeypatch.setattr(tui, "datetime", FrozenDateTime)

    async def scenario() -> None:
        async with _history_poll_app(short_root, size=(80, 24)) as (app, pilot, _):
            clock = app.query_one(tui._LocalHeaderClock)
            for width in (80, 120, 80):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                assert clock.content_size.width == 19
                assert clock.region.right <= width
                assert "2026-07-02T01:30:45" in clock.render_line(0).text

    asyncio.run(scenario())


def test_pilot_local_times_in_table_trace_details_and_triggers(
    short_root: Path, local_timezone
) -> None:
    async def scenario() -> None:
        async with _history_poll_app(short_root, size=(240, 50)) as (app, pilot, replies):
            row = {
                "status": "SUCCESS",
                "status_at": "2026-07-01T23:30:00",
                "pending_timers": [{"due": "2026-01-01T23:45:00", "kind": "schedule"}],
            }
            replies.status["jobs"] = {"trace_job": row}
            # a time inside the cause is the engine's text and is shown as it came
            cause = "configuration rev 2 applied at 2026-07-01T23:30:00"
            replies.rows.append(
                {
                    "seq": 3,
                    "n": 0,
                    "at": "2026-07-01T23:30:00",
                    "job": "",
                    "transition": "CONFIG",
                    "cause": cause,
                }
            )
            request = replies.request

            async def answer(query):
                if query["cmd"] == "spec":
                    return {"ok": True, "text": "insert_job: trace_job\n"}
                if query["cmd"] == "deps":
                    return {"ok": True}
                if query["cmd"] == "timers":
                    return {
                        "ok": True,
                        "timers": [{"job": "trace_job", **row["pending_timers"][0]}],
                    }
                return await request(query)

            app._client.request = answer
            await pilot.press("r")
            await _wait_for_ui(pilot, lambda: app._cursor == (3, _KEY_MAX))
            table = app.query_one("#jobs", DataTable)
            assert table.get_cell("trace_job", "at") == "2026-07-02T01:30:00"
            assert table.columns[next(k for k in table.columns if k.value == "at")].width == 19
            assert table.get_cell("trace_job", "timers") == "schedule@2026-01-02T00:45:00"
            await pilot.pause()
            console = app.query_one("#console", RichLog)
            rendered = re.sub(r"\s+", "", "".join(line.text for line in console.lines))
            assert "2026-07-02T01:30:00" in rendered
            assert re.sub(r"\s+", "", cause) in rendered
            table.focus()
            await pilot.press("d")
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
            body = str(app.screen.query_one("#specbox Static", Static).content)
            assert "at 2026-07-02T01:30:00" in body
            assert "next: schedule @ 2026-01-02T00:45:00" in body
            assert "  schedule @ 2026-01-02T00:45:00" in body
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
            await pilot.press("t")
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
            triggers = app.screen.query_one("#trigbox", DataTable)
            await _wait_for_ui(pilot, lambda: triggers.row_count == 1)
            assert triggers.get_row_at(0)[0] == "2026-01-02T00:45:00"
            await pilot.press("escape")

    asyncio.run(scenario())


def test_format_countdown_humanizes_each_magnitude_and_dashes_overdue() -> None:
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert format_countdown(now + timedelta(seconds=42), now) == "42s"
    assert format_countdown(now + timedelta(minutes=3, seconds=12), now) == "3m12s"
    assert format_countdown(now + timedelta(hours=2, minutes=5), now) == "2h05m"
    assert format_countdown(now + timedelta(days=1, hours=3), now) == "1d03h"
    assert format_countdown(now, now) == "-"  # due now = nothing left to count
    assert format_countdown(now - timedelta(seconds=5), now) == "-"


def test_format_countdown_pins_exact_unit_boundaries() -> None:
    """The magnitude cascade (seconds -> minutes -> hours -> days) trips on
    divmod, not on a threshold compare -- pin the exact crossings so a
    future refactor of the cascade can't silently shift them by one unit."""
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert format_countdown(now + timedelta(seconds=59), now) == "59s"
    assert format_countdown(now + timedelta(seconds=60), now) == "1m00s"
    assert format_countdown(now + timedelta(minutes=59), now) == "59m00s"
    assert format_countdown(now + timedelta(minutes=60), now) == "1h00m"
    assert format_countdown(now + timedelta(hours=23), now) == "23h00m"
    assert format_countdown(now + timedelta(hours=24), now) == "1d00h"


def test_assemble_trigger_rows_dated_stable_then_filewatch_then_armed_sorted(
    local_timezone,
) -> None:
    """The server's due order carries through untouched; a due-less filewatch
    row renders generically below the dated rows; armed jobs (from the status
    snapshot, not the timers verb) append last, name-sorted. Dues use full
    host-local timestamps (DL-473)."""
    timers = [
        {"due": "2026-08-09T12:30:00", "job": "tv_sched", "kind": "schedule"},
        {"due": "2026-08-10T01:00:00", "job": "tv_late", "kind": "must_start"},
        {"job": "tv_watch", "kind": "filewatch", "detail": "/data/in.csv"},
    ]
    jobs = {
        "tv_b_armed": {"status": "INACTIVE", "armed": True},
        "tv_a_armed": {"status": "INACTIVE", "armed": True},
        "tv_plain": {"status": "INACTIVE", "armed": False},
    }
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert assemble_trigger_rows(timers, jobs, now) == [
        ("2026-08-09T14:30:00", "30m00s", "tv_sched", "schedule", ""),
        ("2026-08-10T03:00:00", "13h00m", "tv_late", "must_start", ""),
        ("-", "watching", "tv_watch", "filewatch", "/data/in.csv"),
        ("-", "-", "tv_a_armed", "armed", "waiting on next condition edge"),
        ("-", "-", "tv_b_armed", "armed", "waiting on next condition edge"),
    ]


def test_assemble_trigger_rows_empty_timers_and_no_armed_jobs_yields_no_rows() -> None:
    assert assemble_trigger_rows([], {}, datetime(2026, 8, 9, 12, 0, 0)) == []
    jobs = {"tv_plain": {"status": "INACTIVE", "armed": False}}
    assert assemble_trigger_rows([], jobs, datetime(2026, 8, 9, 12, 0, 0)) == []


def test_assemble_trigger_rows_preserves_input_order_for_equal_due_times() -> None:
    """The server, not this function, is the sort authority (DL-65 due
    ordering); two entries sharing a due time must come through in the
    order the server gave them, not get reshuffled by a stable-sort
    assumption the assembly function doesn't actually need to hold."""
    timers = [
        {"due": "2026-08-09T12:30:00", "job": "tv_z", "kind": "schedule"},
        {"due": "2026-08-09T12:30:00", "job": "tv_a", "kind": "must_start"},
    ]
    now = datetime(2026, 8, 9, 12, 0, 0)
    rows = assemble_trigger_rows(timers, {}, now)
    assert [r[2] for r in rows] == ["tv_z", "tv_a"]


def test_assemble_trigger_rows_malformed_due_string_is_explicit(local_timezone) -> None:
    """A `due` that fails datetime.fromisoformat (server/client version
    skew, a hand-typed test fixture) must not crash the TUI renderer -- it
    names the invalid timestamp rather than hiding it (DL-473)."""
    timers = [{"due": "not-a-timestamp", "job": "tv_bad", "kind": "schedule"}]
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert assemble_trigger_rows(timers, {}, now) == [
        ("invalid timestamp: 'not-a-timestamp'", "-", "tv_bad", "schedule", "")
    ]


def test_assemble_detail_trigger_lines_full_story_in_spec_order(local_timezone) -> None:
    """DL-68 popup trigger story, pure assembly: started by, armed, a live
    watch (min_size only when set), the earliest dated timer for THIS job
    (other jobs' entries and due-less rows ignored), then one indented line
    per pending timer under a header."""
    row = {
        "started_by": "sendevent STARTJOB (operator)",
        "armed": True,
        "watching": {"file": "/data/in.csv", "interval": 30, "min_size": 1024},
        "pending_timers": [
            {"due": "2026-08-09T12:45:00", "kind": "term_run_time"},
            {"due": "2026-08-09T12:50:00", "kind": "must_complete"},
        ],
    }
    timers = [
        {"due": "2026-08-09T12:05:00", "job": "other", "kind": "schedule"},
        {"due": None, "job": "dt_job", "kind": "filewatch", "detail": "x"},
        {"due": "2026-08-10T04:00:00", "job": "dt_job", "kind": "schedule"},
        {"due": "2026-08-09T12:45:00", "job": "dt_job", "kind": "term_run_time"},
    ]
    assert assemble_detail_trigger_lines("dt_job", row, timers) == [
        "started by: sendevent STARTJOB (operator)",
        "armed: waiting on next condition edge",
        "watching /data/in.csv every 30s, min_size 1024",
        "next: term_run_time @ 2026-08-09T14:45:00",
        "pending timers:",
        "  term_run_time @ 2026-08-09T14:45:00",
        "  must_complete @ 2026-08-09T14:50:00",
    ]


def test_assemble_detail_trigger_lines_empty_row_yields_nothing() -> None:
    assert assemble_detail_trigger_lines("dt_job", {}, []) == []
    assert assemble_detail_trigger_lines(
        "dt_job", {"watching": {"file": "/f", "interval": 60, "min_size": None}}, []
    ) == ["watching /f every 60s"]


#: the flags cell's position in a jobs-table row (`runner_tui._COLUMNS`)
_FLAGS = _COLUMNS.index("flags")


def test_row_cells_flags_carry_the_armed_latch_in_ihna_order() -> None:
    """The status payload's `armed` boolean (SEM-32; served since DL-54 but
    never rendered) surfaces as flag A, ordered after I/H/N."""
    app = RunnerApp(Path("/tmp/unused.sock"))
    all_on = {
        "status": "INACTIVE",
        "on_ice": True,
        "on_hold": True,
        "on_noexec": True,
        "armed": True,
    }
    assert app._row_cells("j", all_on)[_FLAGS] == "IHNA"
    assert app._row_cells("j", {"status": "INACTIVE", "armed": True})[_FLAGS] == "A"
    assert app._row_cells("j", {"status": "INACTIVE", "armed": False})[_FLAGS] == ""


def test_row_cells_flags_compose_partial_combinations_in_deterministic_order() -> None:
    """Partial flag sets (not just the all-on/A-alone extremes) still land
    letters in the fixed I-H-N-A order, never in insertion/dict order."""
    app = RunnerApp(Path("/tmp/unused.sock"))
    i_and_a = {"status": "INACTIVE", "on_ice": True, "armed": True}
    assert app._row_cells("j", i_and_a)[_FLAGS] == "IA"
    h_n_and_a = {"status": "INACTIVE", "on_hold": True, "on_noexec": True, "armed": True}
    assert app._row_cells("j", h_n_and_a)[_FLAGS] == "HNA"


def test_both_surfaces_render_the_flags_column_from_one_alphabet(monkeypatch) -> None:
    """DL-145. `dsl41 query --brief` and this table each carried their own
    (mark, key) table for the same status payload, in the same order, kept
    equal by hand. Two equal copies test nothing, so this drives the
    SOURCE.

    The alphabet belongs to the module that owns the payload
    (`runner_control.STATUS_FLAG_MARKS`). Move it there and BOTH surfaces
    move: a hand-kept copy would go on spelling the old one, which is
    exactly the drift the copies were one edit away from."""
    import itertools

    from dsl41 import runner_control
    from dsl41.cli_output import prepare_status
    from dsl41.cli_control import _brief_flags
    from dsl41.runner_control import STATUS_FLAG_MARKS

    app = RunnerApp(Path("/tmp/unused.sock"))
    keys = [key for _, key in STATUS_FLAG_MARKS]
    assert keys == ["on_ice", "on_hold", "on_noexec", "armed"]
    for combination in itertools.product((False, True), repeat=len(keys)):
        row = {"status": "INACTIVE", **dict(zip(keys, combination, strict=True))}
        assert app._row_cells("j", row)[_FLAGS] == _brief_flags(row)
        assert prepare_status({"jobs": {"j": row}}).rows[0][6] == (_brief_flags(row) or "-")

    # a FIFTH flag, added where the payload is built: both surfaces publish
    # it, and neither has a table of its own that could miss it
    moved = (*STATUS_FLAG_MARKS, ("Z", "quarantined"))
    monkeypatch.setattr(runner_control, "STATUS_FLAG_MARKS", moved)
    fifth = {"status": "INACTIVE", "on_ice": True, "quarantined": True}
    assert app._row_cells("j", fifth)[_FLAGS] == "IZ"
    assert _brief_flags(fifth) == "IZ"
    assert prepare_status({"jobs": {"j": fifth}}).rows[0][6] == "IZ"


def test_t_binding_is_registered_with_a_footer_label() -> None:
    bindings = {b.key: b for b in RunnerApp.BINDINGS if isinstance(b, Binding)}
    assert bindings["t"].action == "triggers"
    assert bindings["t"].description == "triggers"
    assert bindings["t"].show is True


def test_triggers_screen_shadows_every_app_key() -> None:
    """The pager drift guard's sibling (DL-67 discipline): while the
    read-only triggers screen is open, every key the app binds must be
    remapped (close/refresh) or neutralized to the bell by the screen --
    a new app binding reachable from inside it would mutate the estate
    from a view that exists to look."""

    def keys(bindings) -> set[str]:
        return {b.key if isinstance(b, Binding) else b[0] for b in bindings}

    bound = keys(RunnerApp.BINDINGS) | keys(_JobsTable.BINDINGS)
    leaks = bound - keys(TriggersScreen.BINDINGS)
    assert not leaks, f"app keys reachable from the triggers screen: {sorted(leaks)}"


def test_pilot_t_opens_the_live_triggers_view_and_operator_verbs_do_not_fire(
    short_root: Path,
) -> None:
    """`t` pushes the TriggersScreen; while the started job RUNs its
    term_run_time timer shows as a dated row with a live countdown; the
    operator verbs pressed inside the screen reach nothing (no new console
    echo, no spec popup); escape closes."""
    text = "insert_job: tv_run\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING: the timer stays pending
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tv_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("tv_run", "status")) == "RUNNING"
                )

                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                trig = app.screen.query_one("#trigbox", DataTable)
                await _wait_for_ui(pilot, lambda: trig.row_count >= 1)
                due, countdown, job, kind, _detail = trig.get_row_at(0)
                assert (job, kind) == ("tv_run", "term_run_time")
                assert due != "-"
                assert countdown != "-"  # counting down, not overdue

                console = app.query_one("#console", RichLog)
                echoes_before = sum(1 for ln in console.lines if ln.text.startswith("> "))
                await pilot.press("s", "f", "k", "d", "colon")
                await pilot.pause()
                assert isinstance(app.screen, TriggersScreen)  # d opened no popup
                assert app.focused is trig  # `:` did not focus the hidden console
                echoes_after = sum(1 for ln in console.lines if ln.text.startswith("> "))
                assert echoes_after == echoes_before  # looking mutated nothing

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, TriggersScreen))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_triggers_refresh_preserves_the_cursor_across_ticks(short_root: Path) -> None:
    """The 2s re-query must not clear() the table -- a clear
    resets the row cursor and scroll to 0, so the operator could never read
    past the first screenful of an estate-scale trigger list. Steady-state
    refreshes update cells in place; a membership change rebuilds but puts
    the cursor back on the same trigger."""
    text = (
        "insert_job: tc_a\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        "insert_job: tc_b\njob_type: c\ncommand: y\nmachine: m1\nterm_run_time: 5\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # both stay RUNNING: two dated rows
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 2)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                for row in (0, 1):
                    table.move_cursor(row=row)
                    await pilot.pause()
                    await pilot.press("s")
                await _wait_for_ui(
                    pilot,
                    lambda: all(
                        str(table.get_cell(j, "status")) == "RUNNING" for j in ("tc_a", "tc_b")
                    ),
                )

                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                screen = app.screen
                assert isinstance(screen, TriggersScreen)
                trig = screen.query_one("#trigbox", DataTable)
                await _wait_for_ui(pilot, lambda: trig.row_count == 2)
                trig.move_cursor(row=1)
                await pilot.pause()

                await screen._refresh()  # the interval tick, without the 2s wait
                await pilot.pause()
                assert trig.row_count == 2
                assert trig.cursor_row == 1  # clear() would have bounced it to 0
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# --------------------------------- 7. safety: focus scope, confirm, posture


_VERB_KEYS = {"s", "f", "k", "i", "I", "h", "H", "n", "N"}


def test_the_operator_verbs_live_on_the_jobs_table_not_on_the_app() -> None:
    """Drift guard for DL-187: an app-level `send(...)` binding fires from
    every focusable widget in the app -- which is how `k` in the console log
    used to kill the selected job. The verbs belong to the focused table."""

    def keys(bindings) -> set[str]:
        return {b.key if isinstance(b, Binding) else b[0] for b in bindings}

    def actions(bindings) -> set[str]:
        return {b.action if isinstance(b, Binding) else b[1] for b in bindings}

    leaked = {a for a in actions(RunnerApp.BINDINGS) if a.startswith("send(")}
    assert not leaked, f"operator verbs still bound at app level: {sorted(leaked)}"
    assert keys(_JobsTable.BINDINGS) == _VERB_KEYS
    assert all(a.startswith("app.send(") for a in actions(_JobsTable.BINDINGS))
    # widening this set widens what a single keystroke can do unconfirmed
    assert RunnerApp._CONFIRM_VERBS == frozenset({"KILLJOB", "FORCE_STARTJOB"})


def test_confirm_body_names_the_frozen_revision_and_caps_the_member_list() -> None:
    """The confirm text is a pure function of what the operator was shown:
    verb, target, the status and the FROZEN revision, and -- for a box --
    the blast radius, capped so a wide box stays readable."""
    plain = confirm_body("KILLJOB", "cb_job", "RUNNING", 7, [])
    assert plain.splitlines() == ["KILLJOB cb_job", "now RUNNING at revision 7"]

    wide = confirm_body("KILLJOB", "cb_box", "RUNNING", 3, [f"m{i}" for i in range(11)])
    assert "reaches 11 member(s)" in wide
    assert "m0, m1, m2, m3, m4, m5, m6, m7 and 3 more" in wide
    assert "m8" not in wide

    unknown = confirm_body("KILLJOB", "cb_ghost", "", None, [])
    assert unknown.splitlines()[1] == "now ? at revision ?"


def test_pilot_k_in_the_console_log_reaches_nothing(short_root: Path) -> None:
    """The bug DL-187 closes: the event console's RichLog is focusable and
    shadows nothing, so an app-level `k` killed the selected job from a
    widget that exists to be read. With the verbs on the table it is dead --
    no confirm, no echo, no status change."""
    text = "insert_job: fk_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING: a kill would show
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "fk_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("fk_run", "status")) == "RUNNING"
                )

                console = app.query_one("#console", RichLog)
                console.focus()
                await pilot.pause()
                assert app.focused is console
                echoes_before = sum(1 for ln in console.lines if ln.text.startswith("> "))

                await pilot.press("k")
                await pilot.pause()
                assert not isinstance(app.screen, ConfirmScreen)
                assert sum(1 for ln in console.lines if ln.text.startswith("> ")) == echoes_before
                assert str(table.get_cell("fk_run", "status")) == "RUNNING"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_k_confirms_first_escape_cancels_and_enter_sends(short_root: Path) -> None:
    """`k` on the focused table opens the confirm, not a kill. Escape sends
    nothing and says so; a second `k` plus Enter is what reaches the
    engine."""
    text = "insert_job: ck_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "ck_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("ck_run", "status")) == "RUNNING"
                )
                console = app.query_one("#console", RichLog)

                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                body = str(app.screen.query_one(Static).content)
                assert body.startswith("KILLJOB ck_run")
                assert "now RUNNING at revision" in body

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
                await _wait_for_ui(
                    pilot,
                    lambda: any(ln.text == "> KILLJOB ck_run: cancelled" for ln in console.lines),
                )
                assert not any("> KILLJOB ck_run: applied" in ln.text for ln in console.lines)
                assert str(table.get_cell("ck_run", "status")) == "RUNNING"

                # the modal handed focus back, so the verb keys live again
                assert app.focused is table
                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                await pilot.press("enter")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
                await _wait_for_ui(
                    pilot,
                    lambda: any("> KILLJOB ck_run: applied @ #" in ln.text for ln in console.lines),
                )
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_the_confirmed_kill_names_the_revision_the_dialog_showed(short_root: Path) -> None:
    """The freeze is the whole point of the confirm (DL-90, DL-187): the
    send names the revision the operator AGREED to, so a job that moved
    while they were reading is rejected instead of killed at whatever state
    it had reached. A precondition re-read after the confirm can only ever
    agree with itself, and the kill would land silently."""
    text = "insert_job: fz_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # stays RUNNING for the whole test
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "fz_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("fz_run", "status")) == "RUNNING"
                )

                sent: list[dict] = []
                inner = app._client.request

                async def capture(request):
                    if request.get("cmd") == "sendevent":
                        sent.append(request)
                    return await inner(request)

                app._client.request = capture

                await pilot.press("k")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                shown = app._jobs_snapshot["fz_run"]["state_rev"]
                assert f"at revision {shown}" in str(app.screen.query_one(Static).content)

                # the estate moves under the operator while the dialog is up;
                # ON_ICE would not move a RUNNING job (DL-254), a status write does
                nudge = parse_console_command("CHANGE_STATUS fz_run RUNNING", None)
                assert not isinstance(nudge, str)
                await app._do_sendevent(nudge)
                await _wait_for_ui(pilot, lambda: app._jobs_snapshot["fz_run"]["state_rev"] > shown)

                console = app.query_one("#console", RichLog)
                await pilot.press("y")  # enter is not the only way in
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
                await _wait_for_ui(
                    pilot,
                    lambda: any(
                        "> KILLJOB fz_run: rejected @ #" in ln.text for ln in console.lines
                    ),
                )

                kills = [r for r in sent if r.get("verb") == "KILLJOB"]
                assert len(kills) == 1
                assert kills[0]["expect"] == {"job:fz_run": shown}
                assert not any("> KILLJOB fz_run: applied" in ln.text for ln in console.lines)
                assert str(table.get_cell("fz_run", "status")) == "RUNNING"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_force_on_a_box_names_the_members_it_reaches(short_root: Path) -> None:
    """A box verb's blast radius is the confirm's whole point (DL-65): the
    members are named, not counted."""
    text = (
        "insert_job: fb_box\njob_type: b\n\n"
        "insert_job: fb_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: fb_box\n\n"
        "insert_job: fb_inner\njob_type: b\nbox_name: fb_box\n\n"
        "insert_job: fb_deep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: fb_inner\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 4)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                assert app._selected == "fb_box"

                await pilot.press("f")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                body = str(app.screen.query_one(Static).content)
                assert body.startswith("FORCE_STARTJOB fb_box")
                # descendants, depth-first: an inner box's own member counts
                assert "reaches 3 member(s) -- fb_inner, fb_deep, fb_m1" in body

                await pilot.press("n")  # escape is not the only way out
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_a_typed_killjob_sends_without_a_confirm(short_root: Path) -> None:
    """Writing out KILLJOB is already an act of intent; only the KEY is
    confirmed (DL-187). A modal in front of the typed form would train the
    operator to press through it."""
    text = "insert_job: tk_run\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "tk_run" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: str(table.get_cell("tk_run", "status")) == "RUNNING"
                )

                console = app.query_one("#console", RichLog)
                cmdline = app.query_one("#cmdline", Input)
                cmdline.focus()
                cmdline.value = "KILLJOB tk_run"
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot,
                    lambda: any("> KILLJOB tk_run: applied @ #" in ln.text for ln in console.lines),
                )
                assert not isinstance(app.screen, ConfirmScreen)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_escape_clears_the_console_line_and_returns_to_the_table(short_root: Path) -> None:
    """F2: an abandoned command line must not sit there waiting for a stray
    Enter. Escape clears it and hands focus back to the table."""
    text = "insert_job: ec_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "ec_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()
                console = app.query_one("#console", RichLog)
                cmdline = app.query_one("#cmdline", _ConsoleInput)
                echoes_before = sum(1 for ln in console.lines if ln.text.startswith("> "))

                await pilot.press("colon")
                await pilot.pause()
                assert app.focused is cmdline
                await pilot.press("K", "I", "L", "L")
                await pilot.pause()
                assert cmdline.value == "KILL"

                await pilot.press("escape")
                await pilot.pause()
                assert cmdline.value == ""
                assert app.focused is table

                await pilot.press("enter")
                await pilot.pause()
                assert sum(1 for ln in console.lines if ln.text.startswith("> ")) == echoes_before
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------- the console's words, help and completion (DL-516)


def test_dl516_the_verb_table_names_every_console_verb() -> None:
    verbs = [row.verb for row in VERB_ROWS]
    assert sorted(verbs) == _CONSOLE_VERBS
    assert len(set(verbs)) == len(verbs)


def test_dl516_the_verb_table_keys_and_asks_agree_with_the_jobs_table() -> None:
    sent = {}
    for binding in _JobsTable.BINDINGS:
        assert isinstance(binding, Binding)
        verb = re.fullmatch(r"app\.send\('(\w+)'\)", binding.action)
        if verb is not None:
            sent[verb.group(1)] = binding.key
    assert {row.verb: row.key for row in VERB_ROWS if row.key is not None} == sent
    assert {row.verb for row in VERB_ROWS if row.asks} == RunnerApp._CONFIRM_VERBS
    assert all(row.key is not None for row in VERB_ROWS if row.asks)


async def _console_app(pilot, app: RunnerApp, job: str) -> _ConsoleInput:
    """The table's first row selected and the console focused by `:`."""
    await _wait_for_ui(pilot, lambda: job in app._rows)
    table = app.query_one("#jobs", DataTable)
    table.focus()
    table.move_cursor(row=0)
    await pilot.pause()
    await pilot.press("colon")
    await pilot.pause()
    cmdline = app.query_one("#cmdline", _ConsoleInput)
    assert app.focused is cmdline
    return cmdline


def _line_text(cmdline: _ConsoleInput) -> str:
    return "".join(segment.text for segment in cmdline.render_line(0)).strip()


def test_pilot_dl516_ch_shows_the_suggestion_and_tab_takes_it(short_root: Path) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                await pilot.press("c", "h")
                await _wait_for_ui(pilot, lambda: "chANGE_STATUS" in _line_text(cmdline))
                assert cmdline.value == "ch"
                await pilot.press("tab")
                await pilot.pause()
                assert cmdline.value == "CHANGE_STATUS"
                assert cmdline.cursor_position == len("CHANGE_STATUS")
                assert app.focused is cmdline
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl519_tab_completes_after_the_app_loses_and_regains_focus(
    short_root: Path,
) -> None:
    """DL-519: a focus event empties the library's grey suggestion, as a Tab press does in a
    browser. Tab completes by the rule all the same."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                await pilot.press("c", "h")
                await _wait_for_ui(pilot, lambda: "chANGE_STATUS" in _line_text(cmdline))
                app.post_message(events.AppBlur())
                app.post_message(events.AppFocus())
                await pilot.pause()
                assert cmdline._suggestion == "" and cmdline.value == "ch"
                await pilot.press("tab")
                await pilot.pause()
                assert cmdline.value == "CHANGE_STATUS"
                assert cmdline.cursor_position == len("CHANGE_STATUS")
                assert app.focused is cmdline
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl519_tab_completes_before_the_suggestion_is_answered(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-519: `c`, `h` and Tab queued together, while the suggestion worker has not answered
    and does not. The grey suggestion stays empty; Tab completes by the rule all the same."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def unanswered(self: object, value: str) -> str | None:
        await asyncio.Event().wait()
        return None

    monkeypatch.setattr(runner_tui._ConsoleSuggester, "get_suggestion", unanswered)

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                _queue_keys(app, ("c", "c"), ("h", "h"), ("tab", "\t"))
                await pilot.pause()
                assert cmdline._suggestion == ""
                assert cmdline.value == "CHANGE_STATUS"
                assert app.focused is cmdline
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl519_tab_takes_the_grey_job_after_the_job_names_change(short_root: Path) -> None:
    """DL-519: `KILLJOB jo` shows the grey completion to the one job that starts with `jo`. A
    status answer then adds a job that sorts before it and starts the same. Tab gives the job
    that was shown, not the new one."""
    text = "insert_job: job_b\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "job_b")
                await pilot.press(*"KILLJOB", "space", "j", "o")
                await _wait_for_ui(pilot, lambda: "KILLJOB job_b" in _line_text(cmdline))
                assert cmdline._suggestion == "KILLJOB job_b"
                app._jobs_snapshot = {**app._jobs_snapshot, "job_a": {"status": "INACTIVE"}}
                _queue_keys(app, ("tab", "\t"))
                await pilot.pause()
                assert cmdline.value == "KILLJOB job_b"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_right_takes_a_verb_and_a_job_from_the_status_answer(
    short_root: Path,
) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                await pilot.press("s", "t")
                await _wait_for_ui(pilot, lambda: "stARTJOB" in _line_text(cmdline))
                await pilot.press("right")
                await pilot.pause()
                assert cmdline.value == "STARTJOB"
                await pilot.press("space", "c")
                await _wait_for_ui(pilot, lambda: "STARTJOB cw_job" in _line_text(cmdline))
                await pilot.press("right")
                await pilot.pause()
                assert cmdline.value == "STARTJOB cw_job"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_change_status_su_tab_enter_sends_success_for_the_selected_row(
    short_root: Path,
) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                console = app.query_one("#console", RichLog)
                await pilot.press(*"CHANGE_STATUS", "space", "s", "u")
                await _wait_for_ui(pilot, lambda: "CHANGE_STATUS suCCESS" in _line_text(cmdline))
                await pilot.press("tab")
                await pilot.pause()
                assert cmdline.value == "CHANGE_STATUS SUCCESS"
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot,
                    lambda: any(
                        "> CHANGE_STATUS cw_job SUCCESS: applied @ #" in ln.text
                        for ln in console.lines
                    ),
                )
            trace = await client.request({"cmd": "trace"})
            # the engine took one injected status for the selected row, and it is SUCCESS
            injected = [
                entry["transition"]
                for entry in trace["entries"]
                if entry["job"] == "cw_job" and entry["cause"] == "injected STATUS"
            ]
            assert injected == ["INACTIVE->SUCCESS"]
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def _count_bells(app: RunnerApp) -> list[None]:
    """Each ring of the app's bell, from now on."""
    rings: list[None] = []
    app.bell = lambda: rings.append(None)  # type: ignore[method-assign]
    return rings


def test_pilot_dl516_tab_never_leaves_the_console_line(short_root: Path) -> None:
    """With nothing to take, or with the cursor inside the line, Tab rings the bell and changes
    nothing: no focus move, no text, no cursor move."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                rings = _count_bells(app)
                await pilot.press("tab")  # an empty line
                await pilot.pause()
                assert app.focused is cmdline and cmdline.value == "" and len(rings) == 1

                await pilot.press("x", "tab")  # a word that starts nothing
                await pilot.pause()
                assert app.focused is cmdline and cmdline.value == "x" and len(rings) == 2

                cmdline.value = ""
                await pilot.press("c", "h")
                await _wait_for_ui(pilot, lambda: "chANGE_STATUS" in _line_text(cmdline))
                await pilot.press("left", "tab")  # a suggestion shows, the cursor is inside
                await pilot.pause()
                assert app.focused is cmdline and cmdline.value == "ch"
                assert cmdline.cursor_position == 1 and len(rings) == 3
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_a_job_name_typed_after_tab_stays_in_the_console_line(
    short_root: Path,
) -> None:
    """Shell habit: a verb, a space, Tab, then the job's name. The name's letters include verb
    keys of the jobs table (n, i, h, f); they all land in the line and send nothing."""
    text = "insert_job: nightly_feed\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "nightly_feed")
                console = app.query_one("#console", RichLog)
                echoes = sum(1 for ln in console.lines if ln.text.startswith("> "))
                await pilot.press(*"STARTJOB", "space", "tab", *"nightly_feed")
                await pilot.pause()
                assert app.focused is cmdline
                assert cmdline.value == "STARTJOB nightly_feed"
                assert not isinstance(app.screen, ConfirmScreen)
                assert sum(1 for ln in console.lines if ln.text.startswith("> ")) == echoes
            trace = await client.request({"cmd": "trace"})
            assert [e for e in trace["entries"] if e.get("job") == "nightly_feed"] == []
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_k_tab_enter_sends_nothing(short_root: Path) -> None:
    """KILLJOB is never suggested, so `k`, Tab, Enter is the line `k`: Tab rings, and Enter
    is refused as an unknown verb. Nothing is sent and no confirm opens."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        client = ControlClient(server.path)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                console = app.query_one("#console", RichLog)
                rings = _count_bells(app)
                await pilot.press("k")
                await pilot.pause()
                await pilot.press("tab")
                await pilot.pause()
                assert cmdline.value == "k" and len(rings) == 1
                await pilot.press("enter")
                await _wait_for_ui(
                    pilot, lambda: any("unknown verb 'K'" in ln.text for ln in console.lines)
                )
                assert not isinstance(app.screen, ConfirmScreen)
                assert not any(ln.text.startswith("> KILLJOB") for ln in console.lines)
            trace = await client.request({"cmd": "trace"})
            assert [e for e in trace["entries"] if e.get("job") == "cw_job"] == []
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def _screen_text(app: RunnerApp, size: tuple[int, int]) -> str:
    """The text the screen shows now, as a terminal would draw it."""
    console = Console(
        width=size[0], height=size[1], file=io.StringIO(), record=True, legacy_windows=False
    )
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_pilot_dl516_f1_on_the_console_opens_the_verbs_screen(
    short_root: Path, size: tuple[int, int]
) -> None:
    """F1 on the console line opens the verbs screen in place of the help panel. A verb row
    shows; a status row and an example show or are reached by PageDown, which scrolls. No word
    of a meaning is cut at either width."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=size) as pilot:
                await _console_app(pilot, app, "cw_job")
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConsoleWordsScreen))
                assert not app.screen.query(HelpPanel)
                box = app.screen.query_one("#wordsbox", VerticalScroll)
                assert app.focused is box
                seen = _screen_text(app, size)
                assert re.search(r"FORCE_STARTJOB +f, asks +start the job despite", seen)
                while box.scroll_y < box.max_scroll_y:
                    before = box.scroll_y
                    await pilot.press("pagedown")
                    await _wait_for_ui(pilot, lambda: box.scroll_y > before)
                    seen += _screen_text(app, size)
                assert re.search(r"SUCCESS +counts as succeeded", seen)
                assert re.search(r"CHANGE_STATUS ex_load FAILURE 3 +ex_load counts as failed", seen)
                words = set(seen.split())
                flat = " ".join(seen.replace("│", " ").split())
                assert "KILLJOB and FORCE_STARTJOB are typed in full" in flat
                assert "On a box, ON_NOEXEC sets the box and every job in it INACTIVE" in flat
                for row in [*VERB_ROWS, *STATUS_ROWS]:
                    assert set(row.meaning.split()) <= words, row
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_pagedown_scrolls_the_verbs_screen_and_home_returns(
    short_root: Path,
) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(80, 24)) as pilot:
                await _console_app(pilot, app, "cw_job")
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConsoleWordsScreen))
                box = app.screen.query_one("#wordsbox", VerticalScroll)
                assert box.max_scroll_y > 0  # longer than an 80x24 screen
                await pilot.press("pagedown")
                await _wait_for_ui(pilot, lambda: box.scroll_y > 0)
                await pilot.press("end")
                await _wait_for_ui(pilot, lambda: box.scroll_y == box.max_scroll_y)
                await pilot.press("up")
                await _wait_for_ui(pilot, lambda: box.scroll_y == box.max_scroll_y - 1)
                await pilot.press("home")
                await _wait_for_ui(pilot, lambda: box.scroll_y == 0)
                await pilot.press("down")
                await _wait_for_ui(pilot, lambda: box.scroll_y == 1)
                await pilot.press("pageup")
                await _wait_for_ui(pilot, lambda: box.scroll_y == 0)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


@pytest.mark.parametrize("close", ["escape", "q", "f1"])
def test_pilot_dl516_closing_the_verbs_screen_keeps_the_console_line(
    short_root: Path, close: str
) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                cmdline = await _console_app(pilot, app, "cw_job")
                await pilot.press(*"STARTJOB cw", "left", "left")
                await pilot.pause()
                assert cmdline.cursor_position == len("STARTJOB ")
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConsoleWordsScreen))
                await pilot.press(close)
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConsoleWordsScreen))
                assert app.focused is cmdline
                assert cmdline.value == "STARTJOB cw"
                assert cmdline.cursor_position == len("STARTJOB ")
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_f1_off_the_console_line_toggles_the_help_panel(short_root: Path) -> None:
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "cw_job" in app._rows)
                app.query_one("#jobs", DataTable).focus()
                await pilot.pause()
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: bool(app.screen.query(HelpPanel)))
                assert not isinstance(app.screen, ConsoleWordsScreen)
                help_view = app.screen.query_one(HelpPanel).query_one("#widget-help")
                assert help_view.source == textwrap.dedent(_JobsTable.HELP.rstrip())
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: not app.screen.query(HelpPanel))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_dl516_operator_keys_on_the_verbs_screen_send_nothing(short_root: Path) -> None:
    """The verbs screen is read-only. The table's verb keys cannot reach the table under a
    modal; `:`, `/` and `d` are app bindings that would, and the screen's bells shadow them
    (DL-67): nothing opens, no line takes focus, nothing is sent."""
    text = "insert_job: cw_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=None)  # inert: a started job stays RUNNING
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        client = ControlClient(server.path)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _console_app(pilot, app, "cw_job")
                console = app.query_one("#console", RichLog)
                echoes = sum(1 for ln in console.lines if ln.text.startswith("> "))
                await pilot.press("f1")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConsoleWordsScreen))
                words_screen = app.screen
                box = words_screen.query_one("#wordsbox", VerticalScroll)
                rings = _count_bells(app)
                await pilot.press("k", "s", "f", "h", "i", "n", "colon", "slash", "d")
                await pilot.pause()
                assert app.screen is words_screen
                assert app.focused is box
                assert len(rings) == 9
                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConsoleWordsScreen))
                assert not app.query_one("#filterline", Input).display
                assert sum(1 for ln in console.lines if ln.text.startswith("> ")) == echoes
            trace = await client.request({"cmd": "trace"})
            assert [e for e in trace["entries"] if e.get("job") == "cw_job"] == []
        finally:
            await client.close()
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_quit_confirms_when_the_app_owns_the_run(short_root: Path) -> None:
    """`run --ui` owns the engine, so `q` stops the run -- confirmed first.
    Escape leaves the app running; Enter exits."""
    text = "insert_job: qo_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path, owns_run=True)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "qo_job" in app._rows)
                app.query_one("#jobs", DataTable).focus()

                await pilot.press("q")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                assert "stop the run?" in str(app.screen.query_one(Static).content)

                await pilot.press("escape")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
                assert app._exit is False

                await pilot.press("q")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, ConfirmScreen))
                await pilot.press("enter")
                await _wait_for_ui(pilot, lambda: app._exit is True)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_quit_detaches_at_once_when_it_does_not_own_the_run(short_root: Path) -> None:
    """`dsl41 ui` leaves the engine running, so `q` is a detach: confirming
    it would train the operator to press through the dialog that matters."""
    text = "insert_job: qd_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "qd_job" in app._rows)
                app.query_one("#jobs", DataTable).focus()
                await pilot.press("q")
                await _wait_for_ui(pilot, lambda: app._exit is True)
                assert not isinstance(app.screen, ConfirmScreen)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_the_footer_names_the_quit_posture(short_root: Path) -> None:
    """The label the Footer renders comes from screen.active_bindings, and
    it must say which of the two quits this app is offering (DL-187)."""
    text = "insert_job: qf_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            for owns_run, label in ((False, "detach"), (True, "stop run")):
                app = RunnerApp(server.path, owns_run=owns_run)
                async with app.run_test(size=(120, 40)) as pilot:
                    await _wait_for_ui(pilot, lambda: "qf_job" in app._rows)
                    assert app.screen.active_bindings["q"].binding.description == label
                # the per-instance assignment must not have leaked into the
                # class-level merge, which every future app instance copies
                merged = RunnerApp._merged_bindings
                assert merged is not None
                assert [b.description for b in merged.key_to_bindings["q"]] == ["quit"]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_q_closes_the_spec_popup(short_root: Path) -> None:
    """F7: a reader who came to look reaches for the pager's leave key, and
    `q` must close the popup rather than fall through to the app's quit."""
    text = "insert_job: qs_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(
            short_root / "run", text, spec_texts={"qs_job": text}
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "qs_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                table.focus()
                table.move_cursor(row=0)
                await pilot.pause()

                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                assert app.screen.query_one("#specbox").border_subtitle == "q/esc close"

                await pilot.press("q")
                await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
                assert app._exit is False
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_the_configuration_line_names_the_revision_or_is_empty() -> None:
    """The configuration line names the revision when status supplies one."""
    from dsl41.runner_tui import config_line

    stopping = {"config_rev": 1, "update": {"token": "ab" * 32, "stopping": ["a", "b"]}}
    assert config_line(stopping) == "rev 1  [update abababababab stopping: a, b]"
    assert config_line({"config_rev": 1}) == "rev 1"
    assert config_line({"config_rev": 2}) == "rev 2"
    assert config_line({}) == ""
