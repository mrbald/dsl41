"""The terminal UI reads its history from the stored entries of `transitions` (DL-541).

The scripted engine of `test_runner_tui._HistoryReplies` pages its rows as control-protocol
ss4 says; each test drives the poll with the `r` key and asserts on the requests the client
sent, the console and the jobs table.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("textual")

from test_runner_tui import (  # noqa: E402
    _KEY_MAX,
    _HistoryReplies,
    _opening,
    _start_refusal,
    _wait_for_ui,
)
from rich.text import Text  # noqa: E402
from textual.widgets import DataTable, RichLog, Static  # noqa: E402

from dsl41 import runner_tui  # noqa: E402
from dsl41.runner_control import ControlClientError  # noqa: E402
from dsl41.runner_tui import RunnerApp, SpecScreen, _LogTail  # noqa: E402

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)

NOW = datetime(2026, 7, 1, 8)


def _at(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


def _alarm(seq: int, at: str, job: str = "trace_job") -> dict:
    return {
        "seq": seq,
        "n": 0,
        "at": at,
        "job": job,
        "transition": "MUST_START_ALARM",
        "cause": f"alarm-{seq}",
    }


def _steps(count: int, first: int = 1) -> list[dict]:
    """Stored entries that never fold on the console and are no alarm."""
    return _HistoryReplies.entries("step", count, first=first, transition="INACTIVE->STARTING")


def _lines(app: RunnerApp) -> list[str]:
    return [line.text for line in app.query_one("#console", RichLog).lines]


def _flat(app: RunnerApp) -> str:
    return re.sub(r"\s+", "", "".join(_lines(app)))


@contextlib.asynccontextmanager
async def _viewer(short_root: Path, replies: _HistoryReplies, *, size=(160, 40)):
    """A real app over `replies`, the poll and the journal wake-ups stopped; it yields once
    the first status answer is in the table."""
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
        await _wait_for_ui(pilot, lambda: bool(app._rows) and not app._refreshing)
        yield app, pilot


async def _poll(app: RunnerApp, pilot) -> None:
    await app._refresh()
    await pilot.pause()


def _cell(app: RunnerApp, job: str) -> str:
    return str(app.query_one("#jobs", DataTable).get_cell(job, "alarms"))


def _title(app: RunnerApp) -> str:
    return str(app.query_one("#jobs", DataTable).border_title)


def _problems(app: RunnerApp) -> set[str]:
    return {name for name, row in app._jobs_snapshot.items() if app._is_problem(name, row)}


def _backward(replies: _HistoryReplies) -> list[dict]:
    return [
        r
        for r in replies.requests
        if r["cmd"] == "transitions" and r.get("direction") == "backward"
    ]


def test_dl541_a_join_over_200000_rows_is_one_page_and_no_trace_is_sent(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(200_000)

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            joins = [r for r in _backward(replies) if "transition" not in r]
            assert joins == [{"cmd": "transitions", "direction": "backward", "limit": 500}]
            assert app._join == (200_000, _KEY_MAX)
            shown = [line for line in _lines(app) if "[step-" in line]
            assert len(shown) == 500
            assert "[step-199501]" in shown[0] and "[step-200000]" in shown[-1]
            assert app.query_one("#console", runner_tui._Console).oldest_key() == (199_501, 0)
            assert not [r for r in replies.requests if r["cmd"] == "trace"]
            # one scan per alarm transition, each one page here, then a follow from the join
            scans = [r for r in _backward(replies) if "transition" in r]
            assert sorted(r["transition"] for r in scans) == [
                "MUST_COMPLETE_ALARM",
                "MUST_START_ALARM",
            ]
            assert replies.follows() == [{"cursor": [200_000, _KEY_MAX], "direction": "forward"}]

    asyncio.run(scenario())


def test_dl541_alarms_count_inside_the_window_and_two_viewers_agree(short_root) -> None:
    """One alarm before twice the window, one between the window and twice it, one inside it:
    the cell and Problems show the last only, for a viewer that joined before it and one that
    joined after."""
    replies = _HistoryReplies()
    replies.status["engine_now"] = NOW.isoformat()
    replies.status["jobs"] = {
        name: {"status": "INACTIVE", "job_type": "CMD"} for name in ("trace_job", "old_job")
    }
    replies.rows = [
        _alarm(1, _at(-timedelta(hours=49))),
        _alarm(2, _at(-timedelta(hours=30))),
        _alarm(3, _at(-timedelta(hours=30)), job="old_job"),
    ]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (early, pilot):
            await _poll(early, pilot)
            assert set(early._alarm_times["trace_job"]) == {(2, 0)}  # the scan stopped at 1
            assert _cell(early, "trace_job") == "" and _problems(early) == set()
            replies.rows.append(_alarm(4, _at(-timedelta(hours=1))))
            await _poll(early, pilot)
            assert _cell(early, "trace_job") == "1" and _cell(early, "old_job") == ""
            assert _problems(early) == {"trace_job"}
            await pilot.press("v")  # the problems view
            await pilot.pause()
            assert early._rows == {"trace_job"}
            assert "alarms" not in _title(early)
        async with _viewer(short_root, replies) as (late, pilot):
            await _poll(late, pilot)
            assert _cell(late, "trace_job") == "1" and _cell(late, "old_job") == ""
            assert _problems(late) == {"trace_job"}

    asyncio.run(scenario())


def test_dl541_an_alarm_leaves_problems_with_the_present_time_and_comes_back_on_a_step_back(
    short_root,
) -> None:
    replies = _HistoryReplies()
    replies.rows = [_alarm(1, NOW.isoformat())]
    replies.status["engine_now"] = NOW.isoformat()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert _problems(app) == {"trace_job"}
            asked = len(_backward(replies))
            for step, problem in (
                (timedelta(hours=24), True),
                (timedelta(hours=24, seconds=1), False),
                (timedelta(hours=47), False),
                (timedelta(hours=23), True),  # the clock steps back
            ):
                replies.status["engine_now"] = _at(step)
                await _poll(app, pilot)
                assert (_problems(app) == {"trace_job"}) is problem, step
                assert _cell(app, "trace_job") == ("1" if problem else "")
            assert len(_backward(replies)) == asked  # no entry was read again

    asyncio.run(scenario())


def test_dl541_a_followed_alarm_is_added_once_and_never_at_or_below_the_join(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(3)
    replies.status["engine_now"] = NOW.isoformat()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert app._join == (3, _KEY_MAX) and app._alarm_count("trace_job") == 0
            # a row at or below the join point is the scans', never the follow's
            app._consume_entries(
                [_alarm(3, NOW.isoformat()), _alarm(2, NOW.isoformat())], follow=True
            )
            assert app._alarm_times == {}
            replies.rows.append(_alarm(4, NOW.isoformat()))
            await _poll(app, pilot)
            await _poll(app, pilot)
            # the same row handed over again keeps one time
            app._consume_entries([_alarm(4, NOW.isoformat())], follow=True)
            assert app._alarm_times == {"trace_job": {(4, 0): NOW}}
            assert _cell(app, "trace_job") == "1"

    asyncio.run(scenario())


@pytest.mark.parametrize("cut", ["rows", "requests", "gap"])
def test_dl541_a_scan_cut_short_marks_the_alarms_incomplete(short_root, cut: str) -> None:
    replies = _HistoryReplies()
    replies.status["engine_now"] = NOW.isoformat()
    if cut == "rows":
        replies.rows = [_alarm(seq, NOW.isoformat()) for seq in range(1, 2102)]
    elif cut == "requests":
        replies.page_cap = 100
        replies.rows = [_alarm(seq, NOW.isoformat()) for seq in range(1, 1002)]
    else:
        replies.floor = 1
        replies.gaps = [[1, 4]]
        replies.rows = [_alarm(seq, NOW.isoformat()) for seq in (5, 6)]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            scans = [r for r in _backward(replies) if r.get("transition") == "MUST_START_ALARM"]
            assert len(scans) <= 8
            assert "alarms incomplete" in _title(app)
            assert _problems(app) == {"trace_job"}
            if cut == "rows":
                assert sum(r["limit"] for r in scans) == 2000
                assert len(app._alarm_times["trace_job"]) == 2000
            elif cut == "requests":
                assert len(scans) == 8 and len(app._alarm_times["trace_job"]) == 800
            else:
                assert "no entries stored for seqs 1-4" in _lines(app)

    asyncio.run(scenario())


def test_dl541_a_status_without_engine_now_makes_the_alarms_unavailable(short_root) -> None:
    replies = _HistoryReplies()
    del replies.status["engine_now"]
    replies.status["jobs"]["failed_job"] = {"status": "FAILURE", "job_type": "CMD"}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "alarms unavailable" in _title(app)
            assert _cell(app, "trace_job") == ""
            assert _problems(app) == {"failed_job"}  # the status part works as before
            assert not [r for r in _backward(replies) if "transition" in r]  # no scan
            # the engine sends its present time from now on: the join is read again with it
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            assert "alarms" not in _title(app)
            assert _problems(app) == {"failed_job", "trace_job"}

    asyncio.run(scenario())


def test_dl541_a_virtual_clock_at_its_minimum_does_not_raise(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["engine_now"] = datetime.min.isoformat()
    replies.rows = [_alarm(1, datetime.max.replace(microsecond=0).isoformat())]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            await _poll(app, pilot)
            assert app._engine_now == datetime.min
            # an alarm dated after the present time is inside the window
            assert _cell(app, "trace_job") == "1"

    asyncio.run(scenario())


def _filled(app: RunnerApp) -> bool:
    """The details popup's refusal total is read (DL-541)."""
    return not [w for w in app.workers if w.group in ("details", "refusals") and not w.is_finished]


async def _details(app: RunnerApp, pilot) -> str:
    app.query_one("#jobs", DataTable).focus()
    await pilot.press("d")
    await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
    await _wait_for_ui(pilot, lambda: _filled(app))
    await pilot.pause()
    body = str(app.screen.query_one("#specbox Static", Static).content)
    await pilot.press("escape")
    await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
    return body


def _other(seq: int) -> dict:
    """A stored entry of the job that is neither a refusal nor a run's opening."""
    return {**_steps(1, first=seq)[0], "transition": "STARTING->RUNNING"}


@pytest.mark.parametrize(
    ("case", "status", "expected"),
    [
        ("no-refusal-old-gap-below", "RUNNING", "scheduler refusals: 0 (run 1)\n"),
        ("three-refusals-between-others", "RUNNING", "scheduler refusals: 3 (run 1)\n"),
        ("opening-without-number", "RUNNING", "scheduler refusals: at least 2 (run 1)"),
        ("gap-above-opening", "RUNNING", "scheduler refusals: at least 2 (run 1)"),
        ("bound", "RUNNING", "scheduler refusals: at least 2000 (run 1)"),
        ("queued", "QUE_WAIT", "scheduler refusals: 1 (queued)\n"),
        ("zero-without-opening", "RUNNING", "scheduler refusals: unknown (run 1)"),
    ],
)
def test_dl541_a_refusal_total_is_exact_when_the_run_s_opening_is_reached(
    short_root, case: str, status: str, expected: str | None
) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status=status, run_number=1)
    opening = _opening(2)
    replies.rows = {
        "no-refusal-old-gap-below": [opening, _other(3)],
        "three-refusals-between-others": [
            opening,
            _start_refusal(3),
            _other(4),
            _start_refusal(5),
            _other(6),
            _start_refusal(7),
        ],
        "opening-without-number": [
            {k: v for k, v in opening.items() if k != "run_number"},
            _start_refusal(3),
            _start_refusal(4),
        ],
        "gap-above-opening": [opening, _start_refusal(4), _start_refusal(5)],
        "bound": [opening, *(_start_refusal(seq) for seq in range(3, 2104))],
        "queued": [opening, _start_refusal(3), {**_start_refusal(4), "refused_status": "QUE_WAIT"}],
        "zero-without-opening": _steps(3, first=2),
    }[case]
    replies.gaps = {"no-refusal-old-gap-below": [[1, 1]], "gap-above-opening": [[3, 3]]}.get(
        case, []
    )

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            body = await _details(app, pilot)
            if case == "no-refusal-old-gap-below":
                assert expected in body
                row = replies.status["jobs"]["trace_job"]
                assert await app._refusal_total("trace_job", row, "period-a", app._history) == (
                    0,
                    True,
                )
            else:
                assert expected in body
            reads = [r for r in _backward(replies) if r.get("job")]
            assert reads and all(r["job"] == "trace_job" and "transition" not in r for r in reads)
            assert len(reads) <= 8 and sum(r["limit"] for r in reads) <= 2000

    asyncio.run(scenario())


def test_dl541_a_refusal_total_stops_at_the_run_s_opening(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=2)
    replies.page_cap = 2
    replies.rows = [
        _opening(1),
        *(_start_refusal(seq) for seq in range(2, 9)),
        _opening(9, run=2),
        *({**_start_refusal(seq), "run_number": 2} for seq in (10, 11, 12)),
    ]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "scheduler refusals: 3 (run 2)\n" in await _details(app, pilot)
            reads = [r for r in _backward(replies) if r.get("job")]
            assert [r.get("cursor") for r in reads] == [None, [11, 0]]

    asyncio.run(scenario())


def test_dl541_a_restart_keeps_the_console_and_the_alarms_and_writes_one_line(short_root) -> None:
    replies = _HistoryReplies()  # two alarms at NOW, the present time

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            before = len(_lines(app))
            replies.histories.append(("history-b", 3))
            replies.rows.extend(_steps(2, first=3))
            await _poll(app, pilot)
            await _poll(app, pilot)
            lines = _lines(app)
            assert lines.count("engine restarted; the stored history continues") == 1
            assert len(lines) == before + 3
            assert app._history == "history-b" and app._cursor == (4, _KEY_MAX)
            assert app._alarm_count("trace_job") == 2

    asyncio.run(scenario())


def test_dl541_a_restored_root_and_a_changed_baseline_each_join_again(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            # a restored copy never saw this viewer's history: it continues through seq 0
            replies.histories = [("history-restored", 1)]
            replies.rows = replies.rows[:1]
            await _poll(app, pilot)
            line = "the stored history does not continue past seq 0; joining again"
            assert _lines(app).count(line) == 1
            assert app._history == "history-restored" and app._join == (1, _KEY_MAX)
            assert app._alarm_count("trace_job") == 1
            replies.baseline = "period-b"
            replies.status["baseline_id"] = "period-b"
            replies.histories = [("history-b", 1)]
            replies.rows = []
            await _poll(app, pilot)
            assert _lines(app).count("baseline changed; joining the history again") == 1
            assert app._history == "history-b" and app._join == (0, _KEY_MAX)
            assert app._alarm_times == {}

    asyncio.run(scenario())


def test_dl541_an_expired_cursor_writes_its_line_and_goes_on(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.rows = _steps(2, first=6)
            replies.floor, replies.pruned_through = 6, (5, 0)
            await _poll(app, pilot)
            await _poll(app, pilot)
            line = "pruned before this view read them: seqs 2-5"
            assert _lines(app).count(line) == 1
            assert app._cursor == (7, _KEY_MAX)
            assert sum("[step-" in text for text in _lines(app)) == 2

    asyncio.run(scenario())


def test_dl541_an_expired_empty_page_does_not_repeat_its_request(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.requests.clear()
            replies.rows, replies.watermark = [], 5
            replies.floor, replies.pruned_through = 6, (5, 0)
            await _poll(app, pilot)
            await _poll(app, pilot)
            assert [r["cursor"] for r in replies.follows()] == [[2, _KEY_MAX], [5, _KEY_MAX]]
            assert sum(text.startswith("pruned before") for text in _lines(app)) == 1

    asyncio.run(scenario())


def test_dl541_the_console_is_bounded_and_the_pager_pages_back_to_the_floor(
    short_root, monkeypatch
) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            replies.rows = _steps(10_100)
            await _poll(app, pilot)
            console = app.query_one("#console", runner_tui._Console)
            assert len(console.lines) == 10_000
            oldest = console.oldest_key()
            assert oldest is not None and oldest[0] > 100
            shown = {text for text in _lines(app) if "[step-" in text}
            assert not any("[step-3]" in text for text in shown)
            monkeypatch.setattr(runner_tui, "_JOIN_LIMIT", 50)
            pager = app.query_one("#logtail", _LogTail)
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: len(pager._buffer) == 50)
            assert app.screen.maximized is not None and app.focused is pager
            assert f"stored events before seq {oldest[0]}" in str(pager.border_title)
            assert f"[step-{oldest[0] - 1}]" in pager._buffer[-1]
            pages = 1
            while app._pager_pin is not None and app._pager_pin.before is not None:
                await pilot.press("E")
                pages += 1
                await _wait_for_ui(pilot, lambda: len(pager._buffer) >= min(50 * pages, oldest[0]))
            assert pager._buffer[0] == "-- the stored history starts here, at seq 1 --"
            assert "[step-1]" in pager._buffer[1] and "[step-3]" in pager._buffer[3]
            assert len(pager._buffer) == oldest[0]  # every earlier entry, and the floor's line
            await pilot.press("q")
            await _wait_for_ui(pilot, lambda: app._pager_pin is None)
            assert "log (out)" in str(pager.border_title)

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["no_journal", "transitions_unavailable", "unknown_cmd"])
def test_dl541_no_stored_history_says_why_once_and_the_table_stays_live(
    short_root, code: str
) -> None:
    replies = _HistoryReplies()
    replies.refusal = {"ok": False, "code": code, "error": f"synthetic {code}"}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.status["jobs"]["trace_job"]["status"] = "FAILURE"
            await _poll(app, pilot)
            line = f"no stored history: synthetic {code} ({code})"
            assert _lines(app).count(line) == 1
            assert str(app.query_one("#jobs", DataTable).get_cell("trace_job", "status")) == (
                "FAILURE"
            )
            assert "(refused)" not in app.sub_title
            assert "alarms unavailable" in _title(app)
            assert {r["cmd"] for r in replies.requests} <= {"status", "transitions", "explain"}

    asyncio.run(scenario())


def test_dl541_richlog_keeps_the_private_fields_the_console_reads() -> None:
    """`_Console` reads RichLog's `_size_known` and `_start_line` (DL-541). Before the first
    size `_size_known` is false, a write renders nothing, and the replay goes through `write` in
    order; `_start_line` counts the rendered lines `max_lines` evicted. A textual that changed
    either would fail here, and the console's oldest key with it."""
    from textual.app import App

    replayed: list[str] = []

    class _Probe(RichLog):
        def write(self, content, *args, **kwargs):
            if self._size_known:
                replayed.append(str(content))
            return super().write(content, *args, **kwargs)

    class _ProbeApp(App[None]):
        def compose(self):
            yield _Probe(id="probe", max_lines=3, markup=False, wrap=False)
            yield runner_tui._Console(id="console")

    async def scenario() -> None:
        app = _ProbeApp()
        probe = _Probe(max_lines=3, markup=False, wrap=False)
        assert probe._size_known is False and probe._start_line == 0
        async with app.run_test(size=(80, 24)) as pilot:
            probe = app.query_one("#probe", _Probe)
            console = app.query_one("#console", runner_tui._Console)
            await pilot.pause()
            assert probe._size_known is True
            for index in range(5):
                probe.write(f"line {index}")
            assert [strip.text for strip in probe.lines] == ["line 2", "line 3", "line 4"]
            assert probe._start_line == 2
            assert replayed[-5:] == [f"line {index}" for index in range(5)]
            for seq in range(1, 4):
                console.write_entry(Text(f"entry {seq}"), (seq, 0))
            assert console.oldest_key() == (1, 0)

        # writes made before the size is known keep their keys through RichLog's replay
        early = runner_tui._Console(id="early")
        early.write_entry(Text("early 7"), (7, 0))
        early.write(Text("no entry"))
        early.write_entry(Text("early 8"), (8, 0))
        assert early._size_known is False and early.lines == []
        assert early.oldest_key() == (7, 0)
        async with _ProbeApp().run_test(size=(80, 24)) as pilot:
            await pilot.app.mount(early)
            await _wait_for_ui(pilot, lambda: len(early.lines) == 3)
            assert [strip.text.rstrip() for strip in early.lines] == [
                "early 7",
                "no entry",
                "early 8",
            ]
            assert list(early._marks) == [(0, (7, 0)), (2, (8, 0))] and not early._early

    asyncio.run(scenario())


def test_dl541_a_gap_on_a_followed_page_marks_the_alarms_incomplete_until_the_next_join(
    short_root,
) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "alarms" not in _title(app)
            replies.rows.extend(_steps(1, first=6))
            replies.gaps = [[3, 5]]
            await _poll(app, pilot)
            assert "no entries stored for seqs 3-5" in _lines(app)
            assert "alarms incomplete" in _title(app)
            replies.gaps = []
            await _poll(app, pilot)
            assert "alarms incomplete" in _title(app)  # until the next join
            replies.baseline = "period-b"
            replies.status["baseline_id"] = "period-b"
            await _poll(app, pilot)
            await _poll(app, pilot)
            assert app._join == (6, _KEY_MAX) and "alarms" not in _title(app)

    asyncio.run(scenario())


async def _fresh(short_root: Path, replies: _HistoryReplies) -> tuple[list[str], dict[str, str]]:
    """What a viewer that joins now shows: its entry lines and its alarm cells."""
    async with _viewer(short_root, replies) as (app, pilot):
        await _poll(app, pilot)
        cells = {name: _cell(app, name) for name in app._jobs_snapshot}
        return [t for t in _lines(app) if "[old-" in t or "[step-" in t], cells


def test_dl541_a_disconnect_in_a_join_s_alarm_scan_leaves_the_viewer_not_joined(
    short_root,
) -> None:
    replies = _HistoryReplies()  # two alarms at the present time
    replies.fail = lambda r: r.get("transition") == "MUST_START_ALARM"

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            assert app._join is None and app._alarm_times == {}
            assert not [t for t in _lines(app) if "[old-" in t]  # nothing shown before it is read
            replies.fail = None
            await _poll(app, pilot)
            shown = [t for t in _lines(app) if "[old-" in t]
            cells = {name: _cell(app, name) for name in app._jobs_snapshot}
        assert (shown, cells) == await _fresh(short_root, replies)
        assert cells == {"trace_job": "2"} and len(shown) == 2

    asyncio.run(scenario())


def test_dl541_a_root_that_loses_its_history_forgets_the_alarms(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert _cell(app, "trace_job") == "2"
            # the root is replaced by one whose rows cannot be read; its job has the same name
            replies.refusal = {"ok": False, "code": "transitions_unavailable", "error": "unset"}
            await _poll(app, pilot)
            assert app._join is None and app._alarm_times == {}
            assert _cell(app, "trace_job") == "" and "alarms unavailable" in _title(app)
            cells = {name: _cell(app, name) for name in app._jobs_snapshot}
        assert cells == (await _fresh(short_root, replies))[1]

    asyncio.run(scenario())


def test_dl541_engine_now_appearing_after_a_join_scans_from_the_cursor_and_writes_no_line_twice(
    short_root,
) -> None:
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "alarms unavailable" in _title(app)
            replies.rows.extend(_steps(1, first=3))
            replies.rows.append(_alarm(4, NOW.isoformat()))
            await _poll(app, pilot)
            assert app._alarm_times == {}  # none is kept while the clock is unknown
            replies.requests.clear()
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            scans = [r for r in _backward(replies) if "transition" in r]
            assert {tuple(r["cursor"]) for r in scans} == {(4, _KEY_MAX)}  # the follow cursor
            assert not [r for r in _backward(replies) if "transition" not in r]  # no rejoin
            assert sum("[old-" in t for t in _lines(app)) == 2
            assert sum("[step-3]" in t for t in _lines(app)) == 1
            assert sum("[alarm-4]" in t for t in _lines(app)) == 1
            assert _cell(app, "trace_job") == "3" and "alarms" not in _title(app)

    asyncio.run(scenario())


def test_dl541_a_step_back_past_the_window_scans_again_and_matches_a_fresh_viewer(
    short_root,
) -> None:
    replies = _HistoryReplies()
    replies.rows = [_alarm(1, NOW.isoformat())]
    replies.status["engine_now"] = _at(timedelta(hours=49))

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert _cell(app, "trace_job") == "" and app._alarm_times == {}
            replies.status["engine_now"] = _at(timedelta(hours=23))
            await _poll(app, pilot)
            attached = _cell(app, "trace_job")
        assert attached == "1"
        assert (await _fresh(short_root, replies))[1] == {"trace_job": attached}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("case", "cut"),
    [("below-both-stops", False), ("read-old", True), ("read-recent", True), ("above-stop", True)],
)
def test_dl541_any_gap_a_scan_reads_marks_the_alarms_incomplete(
    short_root, case: str, cut: bool
) -> None:
    """No gap is dated by entry times (DL-541): a gap a scan reads marks the alarms whatever
    the entries around it are dated; one below where every scan stopped is never reached."""
    replies = _HistoryReplies()
    old, recent = _at(-timedelta(days=3)), _at(-timedelta(hours=1))
    complete = {"transition": "MUST_COMPLETE_ALARM"}
    if case == "below-both-stops":
        replies.gaps = [[1, 1]]
        replies.rows = [_alarm(2, old), {**_alarm(3, old), **complete}, _alarm(4, NOW.isoformat())]
    elif case in ("read-old", "read-recent"):
        replies.gaps = [[1, 4]]
        around = old if case == "read-old" else recent
        replies.rows = [*({**step, "at": around} for step in _steps(20, first=5))]
        replies.rows.append(_alarm(25, NOW.isoformat()))
    else:
        replies.gaps = [[2, 3]]
        replies.rows = [_alarm(1, old), {**_alarm(4, old), **complete}, _alarm(5, NOW.isoformat())]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert ("alarms incomplete" in _title(app)) is cut
            assert _problems(app) == {"trace_job"}
            # no read dates a gap: every forward read is the follow's
            assert not [
                r
                for r in replies.requests
                if r.get("direction") == "forward" and r.get("limit") is not None
            ]

    asyncio.run(scenario())


def test_dl541_a_popup_read_across_two_histories_is_unknown(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.page_cap = 2
    replies.rows = [_start_refusal(seq) for seq in range(1, 7)]

    def replace_root(request: dict) -> None:
        if request.get("job") == "trace_job":
            replies.histories = [("history-other", 1)]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.after = replace_root
            body = await _details(app, pilot)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


def test_dl541_the_restart_line_stands_at_the_boundary(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.histories.append(("history-b", 4))
            replies.rows.extend(_steps(3, first=3))
            await _poll(app, pilot)
            tail = _lines(app)[-4:]
            assert "[step-3]" in tail[0] and "[step-4]" in tail[2] and "[step-5]" in tail[3]
            assert tail[1] == "engine restarted; the stored history continues"

    asyncio.run(scenario())


def test_dl541_a_gap_that_holds_the_join_watermark_is_told_once(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)
    replies.watermark = 3
    replies.gaps = [[3, 3]]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            for _ in range(3):
                await _poll(app, pilot)
            assert _lines(app).count("no entries stored for seq 3") == 1

    asyncio.run(scenario())


def test_dl541_the_table_is_drawn_before_the_history_and_a_pass_follows_four_pages(
    short_root,
) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            gate = asyncio.Event()
            replies.gates.append((lambda r: r.get("direction") == "forward", gate))
            replies.status["jobs"]["trace_job"]["status"] = "FAILURE"
            task = asyncio.create_task(app._refresh())
            await _wait_for_ui(
                pilot,
                lambda: (
                    str(app.query_one("#jobs", DataTable).get_cell("trace_job", "status"))
                    == "FAILURE"
                ),
            )
            assert not task.done()  # the history read is still held
            replies.gates.clear()
            gate.set()
            await task
            replies.rows.extend(_steps(2100, first=3))
            replies.requests.clear()
            await _poll(app, pilot)
            cmds = [
                "F" if r.get("direction") == "forward" else r["cmd"][0] for r in replies.requests
            ]
            assert "".join(c for c in cmds if c in "sF") == "sFFFFsF"
            assert app._cursor == (2102, _KEY_MAX)

    asyncio.run(scenario())


def test_dl541_the_popup_opens_before_its_history_read_and_holds_nothing(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            gate = asyncio.Event()
            refusals = asyncio.Event()

            async def held_reads(request):
                if request.get("job") == "trace_job":
                    refusals.set()
                    await gate.wait()
                return await replies.request(request)

            app._reads.request = held_reads
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("d")
            await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
            await _wait_for_ui(pilot, refusals.is_set)
            body = str(app.screen.query_one("#specbox Static", Static).content)
            assert "scheduler refusals: loading (run 1)" in body
            asked = len(replies.requests)
            await _poll(app, pilot)  # the refresh does not wait for the popup's read
            assert len(replies.requests) > asked
            gate.set()
            await _wait_for_ui(
                pilot,
                lambda: (
                    "scheduler refusals: 1 (run 1)"
                    in str(app.screen.query_one("#specbox Static", Static).content)
                ),
            )

    asyncio.run(scenario())


def test_dl541_the_pager_reaches_the_floor_beyond_ten_thousand_earlier_rows(
    short_root, monkeypatch
) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(12_000)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            monkeypatch.setattr(runner_tui, "_JOIN_LIMIT", 5000)
            pager = app.query_one("#logtail", _LogTail)
            presses = 0
            while app._pager_pin is None or app._pager_pin.before is not None:
                await pilot.press("E")
                presses += 1
                await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
                assert presses <= 4
            assert presses == 3
            assert len(pager._buffer) == 10_000
            assert pager._buffer[0] == "-- the stored history starts here, at seq 1 --"
            assert "[step-1]" in pager._buffer[1]
            assert "newest lines dropped" in str(pager.border_title)

    asyncio.run(scenario())


def test_dl541_notices_that_evict_every_keyed_line_start_the_pager_above_the_last_evicted(
    short_root,
) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            replies.rows.extend(_steps(3, first=3))
            await _poll(app, pilot)
            for index in range(10_001):
                app._console_write(Text(f"notice {index}"))
            console = app.query_one("#console", runner_tui._Console)
            assert console.oldest_key() is None
            replies.requests.clear()
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            assert _backward(replies)[0]["cursor"] == [5, 1]
            pager = app.query_one("#logtail", _LogTail)
            assert any("[step-5]" in line for line in pager._buffer)

    asyncio.run(scenario())


def test_dl541_a_press_while_a_page_is_read_rings_and_sends_nothing(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            replies.rows = _steps(12_000)
            await _poll(app, pilot)
            await _poll(app, pilot)
            await _poll(app, pilot)
            bells: list[int] = []
            app.bell = lambda: bells.append(1)  # type: ignore[method-assign]
            gate = asyncio.Event()
            replies.gates.append(
                (lambda r: r.get("cursor") is not None and r.get("direction") == "backward", gate)
            )
            replies.requests.clear()
            await pilot.press("E")
            await pilot.press("E")
            await pilot.pause()
            assert len(_backward(replies)) == 1 and bells == [1]
            gate.set()
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            replies.gates.clear()
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            cursors = [r["cursor"] for r in _backward(replies)]
            assert len(cursors) == 2 and cursors[1] < cursors[0]

    asyncio.run(scenario())


def test_dl541_a_pager_read_across_two_histories_releases_to_the_log(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            replies.rows = _steps(12_000)
            for _ in range(6):
                await _poll(app, pilot)
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            replies.histories = [("history-restored", 1)]
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: app._pager_pin is None)
            line = "earlier events: the stored history changed; the pager shows the log again"
            assert _lines(app).count(line) == 1
            assert "log (out)" in str(app.query_one("#logtail", _LogTail).border_title)

    asyncio.run(scenario())


def test_dl541_a_rescan_anchored_at_an_alarm_row_keeps_it(short_root) -> None:
    """The follow's fourth page ends on an alarm; the clock becomes known in that pass, and
    the rescan anchored at the cursor includes the cursor's own row."""
    replies = _HistoryReplies()  # two alarms at the present time
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.rows.extend(_steps(1999, first=3))
            replies.rows.append(_alarm(2002, NOW.isoformat()))
            replies.rows.extend(_steps(10, first=2003))
            replies.status["engine_now"] = NOW.isoformat()
            replies.requests.clear()
            await _poll(app, pilot)
            scans = [r for r in _backward(replies) if "transition" in r]
            assert {tuple(r["cursor"]) for r in scans if r.get("limit") == 500} >= {(2002, 1)}
            attached = _cell(app, "trace_job")
        assert attached == "3"
        assert (await _fresh(short_root, replies))[1] == {"trace_job": attached}

    asyncio.run(scenario())


def test_dl541_a_clock_lost_and_found_rebuilds_more_than_a_hundred_alarms(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            del replies.status["engine_now"]
            replies.rows.extend(_alarm(seq, NOW.isoformat()) for seq in range(3, 153))
            await _poll(app, pilot)
            assert app._alarm_times == {} and "alarms unavailable" in _title(app)
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            attached = _cell(app, "trace_job")
        assert attached == "152"
        assert (await _fresh(short_root, replies))[1] == {"trace_job": attached}

    asyncio.run(scenario())


def test_dl541_a_run_opened_after_the_status_answer_is_passed_over(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="SUCCESS", run_number=1)
    replies.rows.extend(
        [
            _opening(2, n=1),
            _start_refusal(3),
            _start_refusal(4),
            _opening(5, run=2),
            {**_start_refusal(6), "run_number": 2},
        ]
    )

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "scheduler refusals: 2 (run 1)\n" in await _details(app, pilot)

    asyncio.run(scenario())


def test_dl541_the_restart_line_waits_for_the_first_row_of_the_new_history(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(2)

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.histories.append(("history-b", 6))  # continuous through seq 5
            replies.rows.extend(_steps(5, first=3))
            replies.page_cap = 1  # a page boundary after seq 3
            await _poll(app, pilot)
            lines = [t for t in _lines(app) if "[step-" in t or "restarted" in t]
            order = [t.split("[")[-1].rstrip("]") if "[" in t else "restart" for t in lines]
            assert order[-6:] == ["step-3", "step-4", "step-5", "restart", "step-6", "step-7"]

    asyncio.run(scenario())


def test_dl541_a_closed_popup_frees_the_reads_connection(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows = _steps(3000)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            lock, gate, walking = asyncio.Lock(), asyncio.Event(), asyncio.Event()

            async def serialized(request):  # one connection, as ControlClient serializes it
                async with lock:
                    if request.get("job"):
                        walking.set()
                        await gate.wait()  # a read the engine never answers
                    return await replies.request(request)

            app._reads.request = serialized
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("d")
            await _wait_for_ui(pilot, walking.is_set)
            await pilot.press("escape")
            await _wait_for_ui(pilot, lambda: not isinstance(app.screen, SpecScreen))
            await _wait_for_ui(pilot, lambda: not lock.locked())
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            assert len(app.query_one("#logtail", _LogTail)._buffer) == 500

    asyncio.run(scenario())


def test_dl541_a_root_joined_while_the_popup_waits_reads_unknown(short_root) -> None:
    """The popup captured root A's read header and token when it opened; a refresh joins root
    B before the walk's first page, and B's total is not shown for A's row."""
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3), _start_refusal(4)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            gate, walking = asyncio.Event(), asyncio.Event()

            async def held(request):
                if request.get("job"):
                    walking.set()
                    await gate.wait()
                return await replies.request(request)

            app._reads.request = held
            app.query_one("#jobs", DataTable).focus()
            await pilot.press("d")
            await _wait_for_ui(pilot, walking.is_set)
            replies.baseline = replies.status["baseline_id"] = "period-b"
            replies.histories = [("history-b", 1)]
            replies.rows = [_opening(1), *(_start_refusal(seq) for seq in range(2, 7))]
            await _poll(app, pilot)
            assert app._history == "history-b"  # the viewer joined root B
            gate.set()
            await _wait_for_ui(pilot, lambda: _filled(app))
            await pilot.pause()
            body = str(app.screen.query_one("#specbox Static", Static).content)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


async def _details_held(app: RunnerApp, pilot, replies: _HistoryReplies, meanwhile) -> str:
    """Open the popup and run `meanwhile` while it waits for `deps`, after its status answer."""
    gate, waiting = asyncio.Event(), asyncio.Event()

    def at_deps(request: dict) -> bool:
        if request["cmd"] == "deps":
            waiting.set()
            return True
        return False

    replies.gates.append((at_deps, gate))
    app.query_one("#jobs", DataTable).focus()
    await pilot.press("d")
    await _wait_for_ui(pilot, waiting.is_set)
    await meanwhile()
    replies.gates.clear()
    gate.set()
    await _wait_for_ui(pilot, lambda: isinstance(app.screen, SpecScreen))
    await _wait_for_ui(pilot, lambda: _filled(app))
    await pilot.pause()
    body = str(app.screen.query_one("#specbox Static", Static).content)
    await pilot.press("escape")
    return body


def test_dl541_a_fork_followed_while_the_popup_opens_never_gives_its_total(short_root) -> None:
    """The popup reads fork A's row; while it waits for `deps` and `timers`, a refresh follows
    same-baseline fork B. The total is A's or unknown, never B's."""
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3), _start_refusal(4)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)

            async def fork() -> None:
                replies.histories = [("history-a", 1), ("history-b", 3)]
                replies.rows = [*replies.rows[:3], *(_start_refusal(s) for s in range(3, 8))]
                await _poll(app, pilot)

            body = await _details_held(app, pilot, replies, fork)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


def test_dl541_a_restored_copy_before_its_first_record_reads_unknown(short_root) -> None:
    """A restored copy R of H starts at seq 3 and has appended nothing: a page reports
    `history = R`, continuous through its own watermark, for a popup that holds H (b)."""
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows = [_opening(1), _start_refusal(2)]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)

            async def restore() -> None:
                replies.histories = [("history-a", 1), ("history-r", 3)]

            body = await _details_held(app, pilot, replies, restore)
            assert "scheduler refusals: unknown (run 1)" in body
            page = [r for r in _backward(replies) if r.get("job")][-1]
            assert page["history"] == "history-a"

    asyncio.run(scenario())


@pytest.mark.parametrize("check", ["a-header", "b-history", "c-continuity", "none"])
def test_dl541_each_page_check_alone_makes_the_total_unknown(short_root, check: str) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)

            async def change() -> None:
                if check == "a-header":  # the pages only: same token, same continuity
                    replies.baseline = "period-x"
                elif check == "b-history":  # continuity holds through the watermark
                    replies.histories = [("history-a", 1), ("history-x", 4)]
                elif check == "c-continuity":  # same header, same token
                    replies.through = 2

            body = await _details_held(app, pilot, replies, change)
            expected = "1 (run 1)\n" if check == "none" else "unknown (run 1)"
            assert f"scheduler refusals: {expected}" in body

    asyncio.run(scenario())


def test_dl541_an_engine_that_does_not_name_its_history_reads_unknown_at_once(short_root) -> None:
    replies = _HistoryReplies()
    replies.names_history = False
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.requests.clear()
            body = await _details(app, pilot)
            assert "scheduler refusals: unknown (run 1)" in body
            assert not [r for r in replies.requests if r.get("job") and r["cmd"] == "transitions"]

    asyncio.run(scenario())


def test_dl541_a_popup_opened_right_after_a_restart_is_exact(short_root) -> None:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3), _start_refusal(4)])

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.histories.append(("history-b", 5))  # restarted; the viewer has not followed
            assert app._history == "history-a"
            assert "scheduler refusals: 2 (run 1)\n" in await _details(app, pilot)

    asyncio.run(scenario())


async def _popup_with_reads(app: RunnerApp, pilot, replies: _HistoryReplies, answer) -> str:
    """The popup's walk gets `answer(request)` for each of its pages."""

    async def reads(request):
        if request.get("job"):
            return await answer(request)
        return await replies.request(request)

    app._reads.request = reads
    return await _details(app, pilot)


def _popup_replies() -> _HistoryReplies:
    replies = _HistoryReplies()
    replies.status["jobs"]["trace_job"].update(status="RUNNING", run_number=1)
    replies.rows.extend([_opening(2, n=1), _start_refusal(3), _start_refusal(4)])
    return replies


@pytest.mark.parametrize(
    "code", ["no_journal", "unknown_cmd", "transitions_unavailable", "access_denied"]
)
def test_dl541_any_refusal_of_the_popup_s_read_shows_unknown(short_root, code: str) -> None:
    """The loading line is never removed: a refusal of any code, named or not, reads
    unknown (DL-541)."""
    replies = _popup_replies()

    async def refused(request):
        return {"ok": False, "code": code, "error": f"synthetic {code}"}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            body = await _popup_with_reads(app, pilot, replies, refused)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["transport", "malformed-entries", "not-an-object", "no-next"])
def test_dl541_a_failed_or_malformed_popup_read_shows_unknown(short_root, fault: str) -> None:
    replies = _popup_replies()

    async def faulty(request):
        if fault == "transport":
            raise ControlClientError("synthetic transport failure")
        page = replies.transitions({**request, "history": request.get("history")})
        if fault == "malformed-entries":
            return {**page, "entries": "not a list"}
        if fault == "not-an-object":
            return {**page, "entries": [*page["entries"], 7]}
        return {**page, "more": True, "next": None, "entries": []}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            body = await _popup_with_reads(app, pilot, replies, faulty)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


def test_dl541_a_page_with_a_cut_entry_makes_the_total_unknown(short_root) -> None:
    """A long name takes a page over the byte cap: one of two refusals comes back shortened
    with `cut: true`, and the walk must not count the other alone as exact."""
    replies = _popup_replies()

    async def cut(request):
        page = replies.transitions(request)
        entries = [dict(entry) for entry in page["entries"]]
        for entry in entries:
            if entry["seq"] == 3:
                entry.update(job=entry["job"][:3], cut=True)
        return {**page, "entries": entries}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            body = await _popup_with_reads(app, pilot, replies, cut)
            assert "scheduler refusals: unknown (run 1)" in body
            whole = await _popup_with_reads(app, pilot, replies, replies.request)
            assert "scheduler refusals: 2 (run 1)\n" in whole

    asyncio.run(scenario())


def _without(answer: dict, field: str) -> dict:
    return {k: v for k, v in answer.items() if k != field}


def _faults(app: RunnerApp) -> list[str]:
    return [
        t
        for t in _lines(app)
        if t.startswith("transitions query refused: a malformed answer: transitions: ")
    ]


@pytest.mark.parametrize("where", ["join-page", "join-alarm-scan"])
def test_dl541_a_join_that_meets_an_answer_out_of_form_holds_nothing(short_root, where) -> None:
    """The join with its alarm scan is whole or not at all: one fault line, nothing held,
    and the next pass joins again (DL-541)."""
    replies = _HistoryReplies()

    def bad(request: dict, answer: dict) -> dict:
        scan = "transition" in request
        hit = scan if where == "join-alarm-scan" else (not scan and "cursor" not in request)
        return _without(answer, "gaps") if hit else answer

    replies.mangle = bad

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert app._join is None and app._alarm_times == {}
            assert not [t for t in _lines(app) if "[old-" in t]
            assert len(_faults(app)) == 1 and "(refused)" in app.sub_title
            # the fault line carries the decoder's diagnostic
            assert "malformedanswer:transitions:gaps:expectedalist,gotnothing" in _flat(app)
            replies.mangle = None
            await _poll(app, pilot)
            assert app._join == (2, _KEY_MAX) and _cell(app, "trace_job") == "2"
            assert "transitions query ok again" in _lines(app)

    asyncio.run(scenario())


def test_dl541_a_followed_answer_out_of_form_keeps_the_cursor(short_root) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.rows.extend(_steps(2, first=3))
            replies.mangle = lambda request, answer: (
                {**answer, "entries": [{**answer["entries"][0], "seq": "3"}]}
                if request.get("direction") == "forward" and answer["entries"]
                else answer
            )
            await _poll(app, pilot)
            await _poll(app, pilot)
            assert app._cursor == (2, _KEY_MAX) and len(_faults(app)) == 1
            assert not [t for t in _lines(app) if "[step-" in t]
            replies.mangle = None
            await _poll(app, pilot)
            assert app._cursor == (4, _KEY_MAX)
            assert sum("[step-" in t for t in _lines(app)) == 2

    asyncio.run(scenario())


def test_dl541_a_later_rescan_that_meets_an_answer_out_of_form_ends_incomplete(short_root) -> None:
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                {**answer, "gaps": "none"} if "transition" in request else answer
            )
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            assert "alarms incomplete" in _title(app) and app._alarms_scanned
            assert app._join == (2, _KEY_MAX)  # the history is still followed

    asyncio.run(scenario())


def test_dl541_a_pager_page_out_of_form_keeps_the_pager_s_place(short_root) -> None:
    replies = _HistoryReplies()
    replies.rows = _steps(12_000)

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                {**answer, "next": [1]}
                if request.get("direction") == "backward" and request.get("cursor")
                else answer
            )
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            pin = app._pager_pin
            assert pin is not None and pin.before == pin.start
            assert any(
                t.startswith("earlier events: a malformed answer: transitions: ")
                for t in _lines(app)
            )
            replies.mangle = None
            await pilot.press("E")
            await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
            assert len(app.query_one("#logtail", _LogTail)._buffer) == 500

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["no-gaps", "malformed-gaps", "entry-without-key"])
def test_dl541_astra_x6_answers_out_of_form_read_unknown_in_the_popup(short_root, fault) -> None:
    replies = _popup_replies()

    async def mangled(request):
        answer = replies.transitions(request)
        entries = [dict(entry) for entry in answer["entries"]]
        if fault == "no-gaps":
            return _without(answer, "gaps")
        if fault == "malformed-gaps":
            return {**answer, "gaps": [["x", 1]]}
        return {**answer, "entries": [_without(entries[0], "seq"), *entries[1:]]}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            body = await _popup_with_reads(app, pilot, replies, mangled)
            assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


def test_dl541_a_well_formed_entry_of_another_job_is_not_counted(short_root) -> None:
    """Content is trusted, the popup's own check of job and run stays: an entry of another
    job in the walk's answer is neither a refusal of this run nor its opening."""
    replies = _popup_replies()

    async def with_another_job(request):
        answer = replies.transitions(request)
        other = [
            {**_start_refusal(5, job="other_job"), "n": 1},
            {**_opening(5, job="other_job"), "n": 2},
        ]
        entries = [*answer["entries"], *other]
        return {**answer, "entries": entries, "next": [entries[0]["seq"], entries[0]["n"]]}

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.rows.append(_other(5))  # the watermark covers seq 5
            body = await _popup_with_reads(app, pilot, replies, with_another_job)
            assert "scheduler refusals: 2 (run 1)\n" in body

    asyncio.run(scenario())


_BAD_REFUSAL = {"ok": False, "code": [], "error": "bad"}


@pytest.mark.parametrize("reader", ["join", "follow", "rescan", "pager", "popup"])
def test_dl541_a_refusal_out_of_form_is_a_malformed_page_for_each_reader(
    short_root, reader: str
) -> None:
    """A refusal whose envelope is out of form (a code that is no string) is read as an answer
    out of form, never as an exception escaping a reader (DL-541)."""
    replies = _popup_replies() if reader == "popup" else _HistoryReplies()
    if reader == "pager":
        replies.rows = _steps(12_000)
    if reader == "rescan":
        del replies.status["engine_now"]

    def bad(request: dict, answer: dict) -> dict:
        if reader == "join" and "cursor" not in request:
            return dict(_BAD_REFUSAL)
        if reader == "follow" and request.get("direction") == "forward":
            return dict(_BAD_REFUSAL)
        if reader == "rescan" and "transition" in request:
            return dict(_BAD_REFUSAL)
        return answer

    if reader in ("join", "follow", "rescan"):
        replies.mangle = bad

    async def scenario() -> None:
        async with _viewer(short_root, replies, size=(200, 40)) as (app, pilot):
            await _poll(app, pilot)
            if reader == "join":
                assert app._join is None and len(_faults(app)) == 1
            elif reader == "follow":
                replies.rows.extend(_steps(1, first=3))
                await _poll(app, pilot)
                assert app._cursor == (2, _KEY_MAX) and len(_faults(app)) == 1
            elif reader == "rescan":
                replies.status["engine_now"] = NOW.isoformat()
                await _poll(app, pilot)
                assert "alarms incomplete" in _title(app)
            elif reader == "pager":

                async def refused(request):
                    if request.get("direction") == "backward" and request.get("cursor"):
                        return dict(_BAD_REFUSAL)
                    return await replies.request(request)

                app._reads.request = refused
                await pilot.press("E")
                await _wait_for_ui(pilot, lambda: not app._pager_pin.reading)
                assert app._pager_pin.before == app._pager_pin.start
                assert any(
                    t.startswith("earlier events: a malformed answer: transitions: ")
                    for t in _lines(app)
                )
            else:

                async def refused_walk(request):
                    return dict(_BAD_REFUSAL)

                body = await _popup_with_reads(app, pilot, replies, refused_walk)
                assert "scheduler refusals: unknown (run 1)" in body

    asyncio.run(scenario())


def test_dl541_a_rescan_whose_answer_line_is_no_object_ends_incomplete(short_root) -> None:
    """A line that is no JSON object takes the transport's path, as `ControlClient` states:
    the viewer reads it as a lost connection, and the rescan it broke ends incomplete."""
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.fail = lambda request: "transition" in request
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            assert app._connected is False
            assert "alarms incomplete" in _title(app)

    asyncio.run(scenario())


def test_dl541_the_recovery_line_comes_before_the_lines_of_the_page_that_recovered(
    short_root,
) -> None:
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.refusal = {"ok": False, "code": "access_denied", "error": "no tier"}
            await _poll(app, pilot)
            replies.refusal = None
            replies.rows.extend(_steps(1, first=3))
            await _poll(app, pilot)
            lines = _lines(app)
            assert lines.index("transitions query ok again") < next(
                i for i, t in enumerate(lines) if "[step-3]" in t
            )

    asyncio.run(scenario())


def test_dl541_a_failed_rescan_after_a_clock_step_back_repaints_the_title(short_root) -> None:
    """The clock steps back past the window; the rescan it calls for fails on the transport.
    The alarms are marked incomplete, and the title shows it on that pass."""
    replies = _HistoryReplies()  # two alarms at the present time

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            assert "alarms" not in _title(app)
            replies.status["engine_now"] = _at(-timedelta(hours=25))
            replies.fail = lambda request: "transition" in request
            await _poll(app, pilot)
            assert app._connected is False and app._alarms_cut
            assert "alarms incomplete" in _title(app)

    asyncio.run(scenario())


@pytest.mark.parametrize("trigger", ["clock-appears", "clock-steps-back"])
def test_dl541_a_rescan_the_transport_broke_is_asked_again(short_root, trigger: str) -> None:
    """A transport failure changes no scan state: the next pass asks the scan again, and once
    the transport recovers the alarms are held and the title clears (DL-541)."""
    replies = _HistoryReplies()  # two alarms at the present time
    if trigger == "clock-appears":
        del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            if trigger == "clock-appears":
                replies.status["engine_now"] = NOW.isoformat()
            else:
                replies.status["engine_now"] = _at(-timedelta(hours=25))
            replies.fail = lambda request: "transition" in request
            await _poll(app, pilot)
            assert app._connected is False and "alarms incomplete" in _title(app)
            replies.fail = None
            replies.requests.clear()
            await _poll(app, pilot)
            assert [r for r in replies.requests if "transition" in r]  # asked again
            assert "alarms" not in _title(app)
            # a time after the present one is inside the window, as before the step
            assert _cell(app, "trace_job") == "2" and app._alarms_scanned

    asyncio.run(scenario())


def test_dl541_a_malformed_rescan_and_popup_read_each_write_their_diagnostic(short_root) -> None:
    replies = _popup_replies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                _without(answer, "gaps") if "transition" in request else answer
            )
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            assert "alarms incomplete" in _title(app)
            assert (
                "alarmscanqueryrefused:amalformedanswer:transitions:gaps:expectedalist,gotnothing"
                in (_flat(app))
            )
            replies.mangle = None

            async def bad(request):
                return _without(replies.transitions(request), "watermark")

            body = await _popup_with_reads(app, pilot, replies, bad)
            assert "scheduler refusals: unknown (run 1)" in body
            assert "details:amalformedanswer:transitions:watermark:expectedaninteger" in _flat(app)

    asyncio.run(scenario())


def test_dl541_a_rescan_that_became_due_stays_due_until_a_scan_completes(short_root) -> None:
    """The clock steps back 24 hours and one second; the rescan fails on the transport; two
    seconds later the transport is back, and the step back, measured again, is under the
    window. The rescan is still owed: the next pass scans and the title clears (DL-541)."""
    replies = _HistoryReplies()

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.status["engine_now"] = _at(-timedelta(hours=24, seconds=1))
            replies.fail = lambda request: "transition" in request
            await _poll(app, pilot)
            assert app._connected is False and "alarms incomplete" in _title(app)
            replies.fail = None
            replies.status["engine_now"] = _at(-timedelta(hours=24) + timedelta(seconds=1))
            replies.requests.clear()
            await _poll(app, pilot)
            assert [r for r in replies.requests if "transition" in r]
            assert "alarms" not in _title(app)

    asyncio.run(scenario())


def test_dl541_a_malformed_rescan_says_its_fault_once_and_its_recovery_once(short_root) -> None:
    """The rescan is retried every pass; its malformed-answer line is said when the fault
    appears, not again while it stays, and the recovery once when a scan works (DL-541)."""
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                _without(answer, "gaps") if "transition" in request else answer
            )
            replies.status["engine_now"] = NOW.isoformat()
            for _ in range(3):
                replies.requests.clear()
                await _poll(app, pilot)
                assert [r for r in replies.requests if "transition" in r]  # retried each pass
            refused = [t for t in _lines(app) if t.startswith("alarm scan query refused:")]
            assert len(refused) == 1 and "alarms incomplete" in _title(app)
            replies.mangle = None
            await _poll(app, pilot)
            await _poll(app, pilot)
            assert _lines(app).count("alarm scan query ok again") == 1
            assert "alarms" not in _title(app)

    asyncio.run(scenario())


def test_dl541_a_rejoin_after_malformed_scans_ends_the_alarm_scan_fault_once(short_root) -> None:
    """Three malformed scan replies, then a baseline change: the history is forgotten and
    joined again, the rejoin's scans complete, the subtitle is clean, and the recovery line is
    written once (DL-541)."""
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                _without(answer, "gaps") if "transition" in request else answer
            )
            replies.status["engine_now"] = NOW.isoformat()
            for _ in range(3):
                await _poll(app, pilot)
            assert "(refused)" in app.sub_title
            replies.mangle = None
            replies.baseline = replies.status["baseline_id"] = "period-b"
            replies.histories = [("history-b", 1)]
            await _poll(app, pilot)
            assert app._join == (2, _KEY_MAX) and app._alarms_scanned and not app._rescan_owed
            assert "(refused)" not in app.sub_title and "alarm scan" not in app._query_faults
            assert _lines(app).count("alarm scan query ok again") == 1
            assert "alarms" not in _title(app)

    asyncio.run(scenario())


def test_dl541_a_history_reset_while_the_scan_fault_is_said_leaves_no_refused(short_root) -> None:
    replies = _HistoryReplies()
    del replies.status["engine_now"]

    async def scenario() -> None:
        async with _viewer(short_root, replies) as (app, pilot):
            await _poll(app, pilot)
            replies.mangle = lambda request, answer: (
                _without(answer, "gaps") if "transition" in request else answer
            )
            replies.status["engine_now"] = NOW.isoformat()
            await _poll(app, pilot)
            assert "(refused)" in app.sub_title
            app._reset_history()
            assert "(refused)" not in app.sub_title and not app._rescan_owed

    asyncio.run(scenario())
