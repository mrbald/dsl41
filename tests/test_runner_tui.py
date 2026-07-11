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
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("textual")

from dsl41.ir import lower_source
from dsl41.runner import (
    ControlServer,
    Engine,
    FakeAdapter,
    RealClock,
    _JOB_EVENT_VERBS,
    start_run,
)
from dsl41.runner_tui import ControlClient, ControlClientError, RunnerApp, parse_console_command
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
    run_root: Path, text: str, *, adapter: FakeAdapter | None = None
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
    server = ControlServer(engine, run_root / "control.sock")
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
        "event": "STARTJOB",
        "job": "myjob",
    }


def test_parse_job_verb_is_case_insensitive() -> None:
    assert parse_console_command("startjob myjob", None) == {
        "cmd": "sendevent",
        "event": "STARTJOB",
        "job": "myjob",
    }


def test_parse_job_verb_without_explicit_job_defaults_to_the_selected_row() -> None:
    assert parse_console_command("KILLJOB", "selected_job") == {
        "cmd": "sendevent",
        "event": "KILLJOB",
        "job": "selected_job",
    }


def test_parse_job_verb_without_explicit_job_and_no_selection_errors() -> None:
    assert parse_console_command("STARTJOB", None) == "STARTJOB needs a job (none selected)"


@pytest.mark.parametrize("verb", sorted(_JOB_EVENT_VERBS))
def test_parse_every_job_verb_takes_at_most_one_job(verb: str) -> None:
    assert parse_console_command(f"{verb} a b", None) == f"{verb} takes at most one job"


def test_parse_set_global_name_equals_value() -> None:
    assert parse_console_command("SET_GLOBAL FLAG=go", None) == {
        "cmd": "sendevent",
        "event": "SET_GLOBAL",
        "name": "FLAG",
        "value": "go",
    }


def test_parse_set_global_value_may_itself_contain_an_equals_sign() -> None:
    """`partition` splits on the FIRST '=' only, so the value may contain
    more of them untouched."""
    assert parse_console_command("SET_GLOBAL FLAG=a=b", None) == {
        "cmd": "sendevent",
        "event": "SET_GLOBAL",
        "name": "FLAG",
        "value": "a=b",
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
        "event": "CHANGE_STATUS",
        "job": "jobx",
        "status": "SUCCESS",
    }


def test_parse_change_status_status_first_without_selection_errors() -> None:
    assert (
        parse_console_command("CHANGE_STATUS SUCCESS", None)
        == "CHANGE_STATUS needs a job (none selected)"
    )


def test_parse_change_status_job_first() -> None:
    assert parse_console_command("CHANGE_STATUS jobx SUCCESS", None) == {
        "cmd": "sendevent",
        "event": "CHANGE_STATUS",
        "job": "jobx",
        "status": "SUCCESS",
    }


def test_parse_change_status_job_first_with_exit_code() -> None:
    assert parse_console_command("CHANGE_STATUS jobx FAILURE 1", None) == {
        "cmd": "sendevent",
        "event": "CHANGE_STATUS",
        "job": "jobx",
        "status": "FAILURE",
        "exit_code": 1,
    }


def test_parse_change_status_status_first_with_exit_code_and_selected_job() -> None:
    assert parse_console_command("CHANGE_STATUS SUCCESS 0", "jobx") == {
        "cmd": "sendevent",
        "event": "CHANGE_STATUS",
        "job": "jobx",
        "status": "SUCCESS",
        "exit_code": 0,
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
            resp = await client.request({"cmd": "sendevent", "event": "ON_HOLD", "job": "sub_job"})
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

                await _wait_for_ui(pilot, lambda: any("ok @" in ln.text for ln in console.lines))
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
                    lambda: str(table.get_cell("tp_trt", "timers")) != ""
                    and str(table.get_cell("tp_trt", "status")) == "RUNNING",
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
