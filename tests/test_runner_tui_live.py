"""The terminal UI reads live engine replies, clock deadlines and job log files."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from reload_fixtures import write_config
from test_access import _map_granting
from test_runner_tui import _serve, _teardown, _wait_for_async, _wait_for_ui
from textual.widgets import DataTable, RichLog, Static

from dsl41.ir import lower_source
from dsl41.runner_access import AccessControl
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import RealClock
from dsl41.runner_control import ControlClient, ControlServer
from dsl41.runner_config import EngineSettings
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_tui import RunnerApp, SpecScreen, TriggersScreen

TEXT = "insert_job: live_job\njob_type: c\ncommand: true\n"
T0 = datetime(2026, 7, 1, 8)
SCHEDULED = TEXT + 'date_conditions: 1\ndays_of_week: all\nstart_mins: "0,1,2,3"\n'


class _Clock(RealClock):
    """The real-domain loop waits for an explicit wall-clock step or an input."""

    def __init__(self) -> None:
        self.at = T0 - timedelta(seconds=1)
        self.changed = asyncio.Event()

    def now(self) -> datetime:
        return self.at

    def advance(self, at: datetime) -> None:
        assert at >= self.at
        self.at = at
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        while self.at < t:
            waits = [asyncio.create_task(self.changed.wait())]
            if interrupt is not None:
                waits.append(asyncio.create_task(interrupt.wait()))
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    task.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
            if interrupt is not None and interrupt.is_set():
                return

    async def sleep_until(self, t: datetime) -> None:
        await self.wait_until(t)


@contextlib.asynccontextmanager
async def _engine_run(
    root: Path,
    text: str,
    clock: RealClock | None = None,
    *,
    resume: bool = False,
    access_map: Path | None = None,
):
    clock = clock if clock is not None else RealClock()
    catalog = lower_source(text)
    options = dict(
        clock=clock,
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(catalog, start=clock.now()),
        settings=EngineSettings(snapshot_interval_s=1),
        hold_open=True,
    )
    engine = (
        await resume_run(catalog, root, **options)
        if resume
        else start_run(catalog, root, **options)
    )
    access = AccessControl.arm(access_map, root) if access_map is not None else None
    server = ControlServer(engine, root / "control.sock", access=access)
    await server.start()
    task = asyncio.create_task(engine.run_until_quiescent(datetime.max))
    try:
        yield engine, server
    finally:
        await _teardown(engine, server, task)


def _console(app: RunnerApp) -> str:
    return re.sub(
        r"\s+", "", "".join(line.text for line in app.query_one("#console", RichLog).lines)
    )


async def _type(pilot, text: str) -> None:
    await pilot.press("escape", "colon")
    await pilot.press(*text, "enter")


@contextlib.asynccontextmanager
async def _source_run(root: Path, text: str = TEXT):
    directory = root / "estate"
    config = write_config(directory)
    jil = directory / "jobs.jil"
    jil.write_text(text)
    run_root = directory / "run"
    run_root.mkdir()
    (run_root / "engine.toml").write_text("grace_term_s = 2\ngrace_kill_s = 0.2\n")
    output = root / "engine.txt"
    with output.open("wb") as stream:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "dsl41",
            "run",
            "--config",
            str(config),
            stdout=stream,
            stderr=stream,
        )
    client = ControlClient(directory / "run" / "control.sock")

    async def ready() -> bool:
        assert process.returncode is None, output.read_text()
        if not client.path.exists():
            return False
        return bool((await client.request({"cmd": "timers"}))["ok"])

    try:
        await _wait_for_async(ready, timeout_s=10)
        yield client, jil
    finally:
        try:
            if process.returncode is None:
                try:
                    await client.request({"cmd": "engine_stop"})
                    assert await asyncio.wait_for(process.wait(), 8) == 0, output.read_text()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        finally:
            await client.close()


def test_pilot_reread_widens_the_live_status_column(short_root: Path, cli_env) -> None:
    async def scenario() -> None:
        async with _source_run(short_root) as (client, jil):
            app = RunnerApp(client.path)
            async with app.run_test(size=(160, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                table = app.query_one("#jobs", DataTable)
                assert table.columns["status"].width == 10
                original_width = table.virtual_size.width
                jil.write_text(TEXT.replace("true", "false"))
                answer = await client.request({"cmd": "reread"})
                assert answer["ok"], answer
                await pilot.press("r")
                await _wait_for_ui(
                    pilot, lambda: "reread" in str(table.get_cell("live_job", "status"))
                )
                assert table.columns["status"].width == 44
                assert table.virtual_size.width == original_width + 34
                answer = await client.request(
                    {
                        "cmd": "update",
                        "baseline_id": app._baseline,
                        "request_id": "reread",
                        "wait": True,
                    }
                )
                assert answer["ok"], answer
                await pilot.press("r")
                await _wait_for_ui(
                    pilot,
                    lambda: (
                        table.columns["status"].width == 10
                        and table.virtual_size.width == original_width
                    ),
                )
                assert "reread" not in str(table.get_cell("live_job", "status"))

    asyncio.run(scenario())


def test_pilot_subtitle_names_a_stopping_update_then_the_applied_revision(
    short_root: Path, cli_env
) -> None:
    """The configuration line names a live update until its TERM-ignoring job stops."""
    program = short_root / "stubborn.py"
    program.write_text(
        "import signal\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "while True: signal.pause()\n"
    )
    command = "exec " + shlex.join([sys.executable, str(program)])

    async def scenario() -> None:
        async with _source_run(short_root, TEXT.replace("true", command)) as (client, jil):
            app = RunnerApp(client.path)
            async with app.run_test(size=(200, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                assert "rev 1" in app.sub_title
                await pilot.press("s")

                def ready() -> bool:
                    row = app._jobs_snapshot["live_job"]
                    log = row.get("log_out")
                    return (
                        row["status"] == "RUNNING"
                        and log is not None
                        and Path(log).exists()
                        and "ready\n" in Path(log).read_text()
                    )

                await _wait_for_ui(pilot, ready)
                assert Path(app._jobs_snapshot["live_job"]["log_err"]).read_text() == ""
                jil.write_text(TEXT)
                forecast = await client.request({"cmd": "reread"})
                assert forecast["ok"], forecast
                assert forecast["preview"]["stop"] == ["live_job"], forecast
                before = await client.request({"cmd": "status"})
                assert before["jobs"]["live_job"]["status"] == "RUNNING", str(before)
                answer = await client.request(
                    {
                        "cmd": "update",
                        "baseline_id": app._baseline,
                        "request_id": "subtitle-update",
                    }
                )
                stopping = await client.request({"cmd": "status"})
                assert answer["ok"] and answer["stop"] == ["live_job"], (
                    forecast,
                    before,
                    answer,
                    stopping,
                    Path(before["jobs"]["live_job"]["log_err"]).read_text(),
                )
                assert stopping["config_rev"] == 1, stopping
                assert stopping["update"]["stopping"] == ["live_job"], stopping
                line = f"rev 1  [update {stopping['update']['token'][:12]} stopping: live_job]"
                await pilot.press("r")
                await _wait_for_ui(pilot, lambda: line in app.sub_title)
                console = app.query_one("#console", RichLog)
                assert any(entry.text == line for entry in console.lines)

                await _wait_for_ui(pilot, lambda: "rev 2" in app.sub_title, timeout_s=10)
                assert "[update " not in app.sub_title
                assert any(entry.text == "rev 2" for entry in console.lines)
                applied = await client.request({"cmd": "status"})
                assert applied["config_rev"] == 2 and "update" not in applied, applied
                await _wait_for_ui(
                    pilot,
                    lambda: any(
                        " CONFIG [configuration rev 2]" in entry.text for entry in console.lines
                    ),
                )
                config = next(
                    entry
                    for entry in console.lines
                    if " CONFIG [configuration rev 2]" in entry.text
                )
                assert any(segment.style and segment.style.dim for segment in config)
                assert app._rows == {"live_job"}
                table = app.query_one("#jobs", DataTable)
                assert [str(key.value) for key in table.rows] == ["live_job"]
                assert app._alarm_times == {}
                assert str(table.get_cell("live_job", "alarms")) == ""

    asyncio.run(scenario())


def test_pilot_access_reload_refuses_queries_and_details_then_recovers(short_root: Path) -> None:
    async def scenario() -> None:
        path = _map_granting(short_root / "access.toml", "adm")
        async with _engine_run(
            short_root / "run", TEXT + "term_run_time: 5\n", access_map=path
        ) as (_, server):
            app = RunnerApp(server.path, owns_run=True)
            async with app.run_test(size=(160, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._selected == "live_job")
                console = app.query_one("#console", RichLog)
                table = app.query_one("#jobs", DataTable)
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: app._jobs_snapshot["live_job"]["status"] == "RUNNING"
                )
                await pilot.press("t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                triggers = app.screen.query_one("#trigbox", DataTable)
                await _wait_for_ui(pilot, lambda: triggers.row_count == 1)
                previous = [triggers.get_row(key) for key in triggers.rows]
                _map_granting(path, None)
                assert server.access is not None
                server.access.reload()
                await pilot.press("r")
                await _wait_for_ui(pilot, lambda: "refused" in str(triggers.border_title))
                assert "timers needs read tier" in str(triggers.border_title)
                assert [triggers.get_row(key) for key in triggers.rows] == previous
                await pilot.press("escape", "r")
                await _wait_for_ui(
                    pilot, lambda: set(app._query_faults) == {"status", "transitions", "timers"}
                )
                assert "(refused)" in app.sub_title
                assert table.row_count == 1
                await pilot.press("r", "d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                body = str(app.screen.query_one("#specbox Static", Static).content)
                assert "spec needs read tier" in body
                assert "run 0" not in body
                assert "no source text" not in body
                assert "needs:" not in body and "blocks:" not in body
                await pilot.press("escape", "t")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, TriggersScreen))
                triggers = app.screen.query_one("#trigbox", DataTable)
                _map_granting(path, "adm")
                server.access.reload()
                await pilot.press("r")
                await _wait_for_ui(pilot, lambda: "refused" not in str(triggers.border_title))
                await pilot.press("escape", "r")
                await _wait_for_ui(pilot, lambda: not app._query_faults)
                assert "(refused)" not in app.sub_title
                for verb in ("status", "transitions", "timers"):
                    assert sum(f"{verb} query refused:" in line.text for line in console.lines) == 1
                    assert any(line.text == f"{verb} query ok again" for line in console.lines)

    asyncio.run(scenario())


def test_pilot_console_refuses_unknown_job_and_verb_and_creates_a_global(short_root: Path) -> None:
    async def scenario() -> None:
        engine, server, task = await _serve(short_root / "run", TEXT)
        client = ControlClient(server.path)
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(200, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                await _type(pilot, "ON_HOLD nosuch")
                await _wait_for_ui(
                    pilot, lambda: ">ON_HOLDnosuch:notsent,nothinglogged:" in _console(app)
                )
                assert "unknownjob" in _console(app).lower()
                console = app.query_one("#console", RichLog)
                refusal = next(
                    line for line in console.lines if "not sent, nothing logged" in line.text
                )
                assert any(
                    segment.style and segment.style.color and segment.style.color.name == "red"
                    for segment in refusal
                )
                await _type(pilot, "FOO")
                await _wait_for_ui(
                    pilot, lambda: "unknownverb'FOO'--expectedoneof:" in _console(app)
                )
                await _type(pilot, "SET_GLOBAL FLAG=go")
                await _wait_for_ui(pilot, lambda: ">SET_GLOBALFLAG=go:applied@#" in _console(app))
                assert "sendeventSET_GLOBAL--socket" in _console(app)
                assert "--globalFLAG=go" in _console(app)
                assert "--expect0" in _console(app)
                answer = await client.request({"cmd": "global", "name": "FLAG"})
                assert answer["globals"]["FLAG"] == {"present": True, "value": "go", "state_rev": 1}
        finally:
            await client.close()
            await _teardown(engine, server, task)

    asyncio.run(scenario())


def test_pilot_disk_drift_advice_clears_after_a_real_update(short_root: Path, cli_env) -> None:
    async def scenario() -> None:
        async with _source_run(short_root) as (client, jil):
            jil.write_text(TEXT + "description: synthetic edit\n")
            app = RunnerApp(client.path)
            async with app.run_test(size=(200, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "SPEC DRIFT" in app.sub_title)
                assert "rundsl41reread" in _console(app)
                assert "thenthedsl41updatecommanditprints" in _console(app)
                await pilot.press("r")
                assert _console(app).count("specdrift:") == 1
                answer = await client.request(
                    {
                        "cmd": "update",
                        "baseline_id": app._baseline,
                        "request_id": "drift",
                        "wait": True,
                    }
                )
                assert answer["ok"], answer
                await pilot.press("r")
                await _wait_for_ui(pilot, lambda: "SPEC DRIFT" not in app.sub_title)
                assert _console(app).count("specdrift:") == 1
                assert "rev 2" in app.sub_title
                console = app.query_one("#console", RichLog)
                assert any(line.text == "rev 2" for line in console.lines)

    asyncio.run(scenario())


def test_pilot_box_details_and_an_empty_job_log(short_root: Path) -> None:
    text = (
        "insert_job: outer\njob_type: b\n\n"
        "insert_job: member\njob_type: c\ncommand: true\nbox_name: outer\n"
    )

    async def scenario() -> None:
        engine, server, task = await _serve(
            short_root / "run", text, adapter=FakeAdapter(default=None)
        )
        try:
            app = RunnerApp(server.path)
            async with app.run_test(size=(160, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: app._selected == "outer")
                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                assert "members: member" in str(
                    app.screen.query_one("#specbox Static", Static).content
                )
                await pilot.press("escape")
                await pilot.press("s")
                await _wait_for_ui(
                    pilot, lambda: app._jobs_snapshot["member"]["status"] == "RUNNING"
                )
                await pilot.press("down")
                await _wait_for_ui(pilot, lambda: app._selected == "member")
                out = Path(app._log_paths["member"][0])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"")
                await pilot.press("d")
                await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                body = str(app.screen.query_one("#specbox Static", Static).content)
                assert "box:     outer" in body and f"log out: {out}" in body
                assert "RUNNING" in body and "log tail:" not in body
        finally:
            await _teardown(engine, server, task)

    asyncio.run(scenario())


def test_pilot_scheduler_ticks_flush_a_repeat_summary_on_the_next_job_entry(
    short_root: Path,
    local_timezone,
) -> None:
    async def scenario() -> None:
        clock = _Clock()
        async with _engine_run(short_root / "run", SCHEDULED, clock) as (_, server):
            app = RunnerApp(server.path)
            async with app.run_test(size=(180, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                clock.advance(T0)
                await _wait_for_ui(
                    pilot, lambda: app._jobs_snapshot["live_job"]["status"] == "RUNNING"
                )
                for minute in (1, 2, 3):
                    clock.advance(T0 + timedelta(minutes=minute))
                    await _wait_for_ui(pilot, lambda: _refusals_folded(app, "live_job") == minute)
                assert _console(app).count("START_REFUSED") == 1
                console = app.query_one("#console", RichLog)
                first = next(line for line in console.lines if "START_REFUSED" in line.text)
                assert any(
                    segment.style and segment.style.color and segment.style.color.name == "yellow"
                    for segment in first
                )
                await _type(pilot, "CHANGE_STATUS live_job SUCCESS 0")
                await _wait_for_ui(pilot, lambda: "repeated2moretimes" in _console(app))
                expected = (
                    "2026-07-01T10:03:00 live_job START_REFUSED repeated 2 more times,"
                    " last 2026-07-01T10:03:00"
                )
                assert re.sub(r"\s+", "", expected) in _console(app)
                summary = next(line for line in console.lines if "more times" in line.text)
                assert any(segment.style and segment.style.dim for segment in summary)
                assert _console(app).index("repeated2moretimes") < _console(app).index(
                    "RUNNING->SUCCESS"
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("deadline", ["must_start", "must_complete"])
def test_pilot_deadline_alarms_increment_the_count_and_render_red(
    short_root: Path, deadline: str
) -> None:
    text = (
        "insert_job: gate\njob_type: c\ncommand: true\n\n"
        + SCHEDULED
        + f"condition: s(gate)\n{deadline}_times: +1\n"
    )

    async def scenario() -> None:
        clock = _Clock()
        async with _engine_run(short_root / "run", text, clock) as (_, server):
            app = RunnerApp(server.path)
            async with app.run_test(size=(180, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                clock.advance(T0)
                await _wait_for_ui(pilot, lambda: app._jobs_snapshot["live_job"]["armed"])
                clock.advance(T0 + timedelta(minutes=1))
                await _wait_for_ui(pilot, lambda: app._alarm_count("live_job") == 1)
                alarm = deadline.upper() + "_ALARM"
                console = app.query_one("#console", RichLog)
                lines = [line for line in console.lines if alarm in line.text]
                assert len(lines) == 1
                assert any(
                    segment.style
                    and segment.style.color
                    and segment.style.color.name == "red"
                    and segment.style.bold
                    for segment in lines[0]
                )
                table = app.query_one("#jobs", DataTable)
                assert str(table.get_cell("live_job", "alarms")) == "1"

    asyncio.run(scenario())


@pytest.mark.parametrize("restart", ["resume", "new-baseline"])
def test_pilot_restart_keeps_the_history_or_joins_again_and_flushes_pending_refusals(
    short_root: Path, restart: str
) -> None:
    """A resumed engine continues the stored history: one line, and the console and the alarm
    times stay. A fresh root under a new baseline joins again. Either way the folded refusals
    are flushed first (DL-480, DL-541)."""

    async def scenario() -> None:
        root = short_root / "run"
        clock = _Clock()
        text = (
            SCHEDULED + "\ninsert_job: gate\njob_type: c\ncommand: true\n\n"
            "insert_job: alarm_job\njob_type: c\ncommand: true\ncondition: s(gate)\n"
            'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\nmust_start_times: +1\n'
        )
        async with contextlib.AsyncExitStack() as engines:
            _, server = await engines.enter_async_context(_engine_run(root, text, clock))
            app = RunnerApp(server.path)
            async with app.run_test(size=(180, 40)) as pilot:
                await _wait_for_ui(pilot, lambda: "live_job" in app._rows)
                clock.advance(T0)
                await _wait_for_ui(
                    pilot, lambda: app._jobs_snapshot["live_job"]["status"] == "RUNNING"
                )
                for minute in (1, 2, 3):
                    clock.advance(T0 + timedelta(minutes=minute))
                    await _wait_for_ui(pilot, lambda: _refusals_folded(app, "live_job") == minute)
                await _wait_for_ui(pilot, lambda: app._alarm_count("alarm_job") == 1)
                baseline, history = app._baseline, app._history
                assert "repeated2moretimes" not in _console(app)
                await engines.aclose()
                if restart == "new-baseline":
                    shutil.rmtree(root)
                await engines.enter_async_context(
                    _engine_run(root, text, clock, resume=restart == "resume")
                )
                await pilot.press("r")
                await _wait_for_ui(pilot, lambda: "repeated2moretimes" in _console(app))
                if restart == "resume":
                    restarted = "enginerestarted;thestoredhistorycontinues"
                    await _wait_for_ui(pilot, lambda: restarted in _console(app))
                    assert app._history != history and app._baseline == baseline
                    assert _console(app).index("repeated2moretimes") < _console(app).index(
                        restarted
                    )
                    assert _console(app).count(restarted) == 1
                    assert app._alarm_count("alarm_job") == 1
                    assert "RESTART" not in _console(app)
                    # the popup reads the run's total from the engine's store, across the restart
                    app._choose("live_job")
                    app.query_one("#jobs", DataTable).focus()
                    await pilot.press("d")
                    await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
                    await _wait_for_ui(
                        pilot,
                        lambda: (
                            "scheduler refusals: 3 (run 1)"
                            in str(app.screen.query_one("#specbox Static", Static).content)
                        ),
                    )
                    await pilot.press("escape")
                else:
                    await _wait_for_ui(pilot, lambda: app._baseline != baseline)
                    assert "baselinechanged;joiningthehistoryagain" in _console(app)
                    assert app._alarm_count("alarm_job") == 0
                assert _console(app).count("repeated2moretimes") == 1

    asyncio.run(scenario())


def _refusals_folded(app: RunnerApp, job: str) -> int:
    """The scheduler refusals of `job` the console's open fold holds: its first line and its
    repeats."""
    run = app._refusal_runs.get(job)
    return 0 if run is None else run.repeats + 1
