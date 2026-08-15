"""RunnerApp Textual TUI tests (phase 11d).

Normative spec: docs/runner-design.md ss11 (UI: one Textual app, terminal
and web) and ss13 item 6 ("TUI: textual pilot snapshot smoke only");
runner_tui.py's own module docstring is the normative detail for
ControlClient (the ss10 socket client), parse_console_command (the ss11
event-console grammar) and RunnerApp (jobs table / explain pane / log tail /
event console). House style follows test_runner_control.py: `short_root`
(AF_UNIX sun_path length), the `_serve`/`_teardown` harness, asyncio.run per
scenario, the POSIX skip guard -- duplicated here rather than imported,
matching how test_runner.py duplicates test_oracle.py's own small helpers.

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
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("textual")

from dsl41.ir import lower_source
from dsl41.runner import Engine
from dsl41.runner_startup import start_run
from dsl41.runner_control import (
    JOB_EVENT_VERBS,
    ControlClient,
    ControlClientError,
    ControlServer,
)
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_tui import (
    RunnerApp,
    SpecScreen,
    TriggersScreen,
    _LogPane,
    _LogTail,
    _outcome_line,
    assemble_detail_trigger_lines,
    assemble_trigger_rows,
    compile_search,
    format_countdown,
    parse_console_command,
)
from textual.binding import Binding
from textual.widgets import DataTable, Input, RichLog, Static

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)


@pytest.fixture
def short_root():
    """A short-path base directory for AF_UNIX control sockets (see
    test_runner_control.py's fixture of the same name/docstring)."""
    d = tempfile.mkdtemp(prefix="dsl41t-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
    assert parse_console_command("STARTJOB", None) == "STARTJOB needs a job (none selected)"


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
    assert (
        parse_console_command("CHANGE_STATUS SUCCESS", None)
        == "CHANGE_STATUS needs a job (none selected)"
    )


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


def test_parse_unknown_verb_errors() -> None:
    assert (
        parse_console_command("FROBNICATE job", None)
        == "unknown verb 'FROBNICATE' (sendevent verbs only)"
    )


@pytest.mark.parametrize("text", ["", "   "])
def test_parse_empty_input_errors(text: str) -> None:
    assert parse_console_command(text, None) == "empty command"


# --------------------------------------------- 1b. the four outcomes (S4, DL-92)


def test_the_console_says_where_an_applied_command_landed() -> None:
    line = _outcome_line("> ON_HOLD j", {"ok": True, "decision": "applied", "index": 7})
    assert line.plain == "> ON_HOLD j: applied @ #7"
    assert line.style == "green"


def test_the_console_says_a_refusal_left_nothing_to_look_up() -> None:
    """The operator's next move after a refusal is to send it again, and the
    line has to support that: nothing was written down, so there is no index
    and no journal entry to go looking for."""
    line = _outcome_line("> ON_HOLD j", {"ok": False, "refused": True, "error": "no expect"})
    assert line.plain == "> ON_HOLD j: not sent, nothing logged: no expect"
    assert line.style == "red"


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
                    "epoch": read["epoch"],
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
    hands focus to the pager; `m` again restores. `]` and `}` move the fr
    shares within their 1..4 clamp -- on the pane, the split participant."""
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

                assert (app._log_share, app._table_share) == (3, 3)
                await pilot.press("]")
                assert app._log_share == 4
                assert str(logbox.styles.height) == "4fr"
                await pilot.press("]")  # clamped: neither pane may collapse
                assert app._log_share == 4
                await pilot.press("{")
                await pilot.press("{")
                await pilot.press("{")  # clamped at 1
                assert app._table_share == 1
                # tablecol (the filter+table column) is the horizontal-split
                # participant, not #jobs inside it (DL-65 review)
                assert str(app.query_one("#tablecol").styles.width) == "1fr"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------- 4. DL-65 estate navigation (box tree / filter / views / details)


def test_pilot_box_tree_order_indent_fold_and_fold_all(short_root: Path) -> None:
    """DL-65 box tree: rows follow the box hierarchy (alpha within each
    level), members indent under their box, space folds the SELECTED box
    (members leave the table, the label gains the hidden-descendant count),
    space again unfolds, and `z` folds/unfolds every box at once."""
    text = (
        "insert_job: bt_box\njob_type: b\n\n"
        "insert_job: bt_m1\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_box\n\n"
        "insert_job: bt_m2\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_box\n\n"
        "insert_job: bt_inner\njob_type: b\nbox_name: bt_box\n\n"
        "insert_job: bt_deep\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bt_inner\n\n"
        "insert_job: bt_solo\njob_type: c\ncommand: x\nmachine: m1\n"
    )
    tree_order = ["bt_box", "bt_inner", "bt_deep", "bt_m1", "bt_m2", "bt_solo"]

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._row_order == tree_order)
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
                await _wait_for_ui(pilot, lambda: app._row_order == ["bt_box", "bt_solo"])
                assert str(table.get_cell("bt_box", "job")) == "▸ bt_box (4)"

                await pilot.press("space")  # unfold restores the same order
                await _wait_for_ui(pilot, lambda: app._row_order == tree_order)
                assert str(table.get_cell("bt_box", "job")) == "▾ bt_box"

                await pilot.press("z")  # fold ALL boxes
                await _wait_for_ui(pilot, lambda: app._row_order == ["bt_box", "bt_solo"])
                await pilot.press("z")  # unfold all
                await _wait_for_ui(pilot, lambda: app._row_order == tree_order)
        finally:
            await _teardown(engine, server, loop_task)

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
                await _wait_for_ui(pilot, lambda: app._row_order == ["pr_box"])
                label = table.get_cell("pr_box", "job")
                assert str(label) == "▸ pr_box (2, 1!)"
                assert label.style == "bold red"
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_pilot_slash_filter_narrows_flat_enter_keeps_escape_clears(short_root: Path) -> None:
    """DL-65 incremental name filter: `/` focuses the filter line, each
    keystroke re-narrows the table LIVE and FLAT (a match inside a box must
    never be invisible), space-separated substrings AND together, the border
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
                await _wait_for_ui(pilot, lambda: app._row_order == tree_order)
                table = app.query_one("#jobs", DataTable)

                await pilot.press("slash")
                await pilot.pause()
                assert app.focused is not None and app.focused.id == "filterline"

                await pilot.press("a", "p")
                await _wait_for_ui(pilot, lambda: app._row_order == ["fx_apple", "fx_apricot"])
                # the filtered view is FLAT: the box member renders unindented
                assert str(table.get_cell("fx_apple", "job")) == "fx_apple"
                assert "jobs 2/4" in str(table.border_title)
                assert "/ap/" in str(table.border_title)

                await pilot.press("space", "p", "l", "e")  # "ap ple": terms AND
                await _wait_for_ui(pilot, lambda: app._row_order == ["fx_apple"])

                await pilot.press("enter")  # keep the filter, back to the table
                await pilot.pause()
                assert app.focused is table
                assert app._filter == "ap ple"
                assert app._row_order == ["fx_apple"]

                await pilot.press("slash")
                await pilot.pause()
                await pilot.press("escape")  # clear and refocus the table
                await _wait_for_ui(pilot, lambda: app._row_order == tree_order)
                assert app._filter == ""
                assert app.focused is table
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
                await _wait_for_ui(pilot, lambda: app._row_order == ["vc_bad"])
                assert "problems" in str(table.border_title)

                await pilot.press("v")  # active: nothing STARTING/RUNNING/QUE_WAIT
                await _wait_for_ui(pilot, lambda: app._row_order == [])
                await pilot.press("v")  # back to all
                await _wait_for_ui(pilot, lambda: app._row_order == ["vc_bad", "vc_ok"])
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


def test_pilot_details_popup_shows_needs_blocks_and_a_log_tail(short_root: Path) -> None:
    """DL-65 popup additions on top of DL-64's spec block: the `deps` verb's
    needs:/blocks: lines (upstream condition entities / referencing jobs)
    and a short local log tail of the job's resolved log_out -- an explicit
    std_out_file resolves before any run, so the tail needs no adapter."""
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
            app = RunnerApp(server.path)
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: len(app._rows) == 3)
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
        "]",
        "[",
        "}",
        "{",  # pane shares
    }

    def keys(bindings) -> set[str]:
        # tuple-form bindings are legal textual; a guard that skipped them
        # would have a hole exactly where a careless future binding lands
        return {b.key if isinstance(b, Binding) else b[0] for b in bindings}

    leaks = keys(RunnerApp.BINDINGS) - keys(_LogTail.BINDINGS) - pass_through
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


# -------------------- 6. DL-68 triggers view (t: due-ordered timers + armed latch)


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


def test_assemble_trigger_rows_dated_stable_then_filewatch_then_armed_sorted() -> None:
    """The server's due order carries through untouched; a due-less filewatch
    row renders generically below the dated rows; armed jobs (from the status
    snapshot, not the timers verb) append last, name-sorted; today's dues are
    time-only, tomorrow's carry the date."""
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
        ("12:30:00", "30m00s", "tv_sched", "schedule", ""),
        ("2026-08-10 01:00:00", "13h00m", "tv_late", "must_start", ""),
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


def test_assemble_trigger_rows_malformed_due_string_falls_back_to_undated() -> None:
    """A `due` that fails datetime.fromisoformat (server/client version
    skew, a hand-typed test fixture) must not crash the TUI renderer -- it
    degrades to a due-less row exactly like a missing key."""
    timers = [{"due": "not-a-timestamp", "job": "tv_bad", "kind": "schedule"}]
    now = datetime(2026, 8, 9, 12, 0, 0)
    assert assemble_trigger_rows(timers, {}, now) == [("-", "-", "tv_bad", "schedule", "")]


def test_assemble_detail_trigger_lines_full_story_in_spec_order() -> None:
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
        "next: term_run_time @ 2026-08-09 12:45:00Z",
        "pending timers:",
        "  term_run_time @ 12:45:00",
        "  must_complete @ 12:50:00",
    ]


def test_assemble_detail_trigger_lines_empty_row_yields_nothing() -> None:
    assert assemble_detail_trigger_lines("dt_job", {}, []) == []
    assert assemble_detail_trigger_lines(
        "dt_job", {"watching": {"file": "/f", "interval": 60, "min_size": None}}, []
    ) == ["watching /f every 60s"]


def test_row_cells_flags_carry_the_armed_latch_in_ihna_order() -> None:
    """The status payload's `armed` boolean (SEM-32; served since DL-54 but
    never rendered) surfaces as flag A, ordered after I/H/N."""
    app = RunnerApp(Path("/tmp/unused.sock"))
    all_on = {"status": "INACTIVE", "on_ice": True, "on_hold": True, "on_noexec": True,
              "armed": True}
    assert app._row_cells("j", all_on)[5] == "IHNA"
    assert app._row_cells("j", {"status": "INACTIVE", "armed": True})[5] == "A"
    assert app._row_cells("j", {"status": "INACTIVE", "armed": False})[5] == ""


def test_row_cells_flags_compose_partial_combinations_in_deterministic_order() -> None:
    """Partial flag sets (not just the all-on/A-alone extremes) still land
    letters in the fixed I-H-N-A order, never in insertion/dict order."""
    app = RunnerApp(Path("/tmp/unused.sock"))
    i_and_a = {"status": "INACTIVE", "on_ice": True, "armed": True}
    assert app._row_cells("j", i_and_a)[5] == "IA"
    h_n_and_a = {"status": "INACTIVE", "on_hold": True, "on_noexec": True, "armed": True}
    assert app._row_cells("j", h_n_and_a)[5] == "HNA"


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

    leaks = keys(RunnerApp.BINDINGS) - keys(TriggersScreen.BINDINGS)
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
    """Review MAJOR: the 2s re-query must not clear() the table -- a clear
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
