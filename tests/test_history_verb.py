"""The `history` read (DL-540): a job's past runs in bounded pages, folded by the run-history
fold from the history index's openings and records. For every named scenario and job, the
pages at limit 1, 2 and 20, joined, equal the whole fold over the whole journal with the
replayed trace and `spool={}` (`read_run_root`), filtered to the job.

The scenarios run on a virtual clock, except a retry and an update's replacement, which need
the wrapper and wait on a condition, never on a fixed delay. `transitions_check` compares each
root's stored rows and index with its replay.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_history_index import (
    _as_transitions_build,
    _bypassed,
    _killed,
    _late_exit,
    _readded,
    _reconciled,
    _replaced,
    _retried,
    _rev_between_runs,
)
from test_run_openings import _LEAF, T0, _at, _ev, _Root
from test_run_openings import _scenario as _run
from test_transitions import _JIL, _as_previous_build, _close, _incarnation, _query, _server
from test_transitions import _start as _start_engine
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import runner_history, runner_runs
from dsl41.ir import lower_source
from dsl41.oracle_state import Event, TraceEntry
from dsl41.runner_access import REQUIRED_TIER, Tier
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_history import RunRow, read_run_root
from dsl41.runner_journal import STORE_NAME
from dsl41.runner_runs import BUDGET, Declined, Request, Restored, answer, parse
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import (
    HistoryQuery,
    MemoryStore,
    Record,
    RecordOwner,
    RunOpening,
    SqliteStore,
    Store,
)
from dsl41.runner_transitions import Stopped

KINDS = ["sqlite", "memory"]

#: An attempt id in the ss11a grammar, numbered by its last four digits.
_ATTEMPT = "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e{:04d}"


def _read(
    store: Store,
    job: str,
    *,
    limit: int = 20,
    cursor: int | None = None,
    budget: int = BUDGET,
    byte_cap: int = runner_runs.REPLY_BYTES,
    restored: Restored | None = None,
    history: str | None = None,
) -> dict[str, Any]:
    return answer(
        store,
        Request(job, cursor, limit, history),
        upto=store.last_seq(),
        header={},
        restored=restored or Restored(None, []),
        budget=budget,
        byte_cap=byte_cap,
    )


@contextmanager
def _reader(run_root: Path) -> Iterator[SqliteStore]:
    with closing(SqliteStore(run_root / STORE_NAME, read_only=True)) as store:
        yield store


def _pages(store: Store, job: str, limit: int, **kwargs: Any) -> list[dict[str, Any]]:
    """Every page of `job`, newest first, each cursor the last page's `next`."""
    pages: list[dict[str, Any]] = []
    cursor: int | None = None
    while True:
        page = _read(store, job, limit=limit, cursor=cursor, **kwargs)
        pages.append(page)
        if not page["more"]:
            return pages
        assert cursor is None or page["next"] < cursor
        cursor = page["next"]


def _paged(run_root: Path, job: str, limit: int) -> list[RunRow]:
    with _reader(run_root) as store:
        pages = _pages(store, job, limit)
    assert all(page["incomplete"] == [] for page in pages)
    rows = [RunRow.model_validate(run["row"]) for page in pages for run in page["runs"]]
    numbers = [row.run_number for row in rows]
    assert numbers == sorted(numbers, reverse=True)
    return rows[::-1]


def _whole(run_root: Path, job: str) -> list[RunRow]:
    """The whole fold of the root with no spool, filtered to `job`, by run number."""
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(runner_history, "_read_all_spool", lambda records: {})
        rows = [row for row in read_run_root(run_root) if row.job == job]
    return sorted(rows, key=lambda row: row.run_number)


def _jobs(run_root: Path) -> list[str]:
    with _reader(run_root) as store:
        return sorted({str(job) for (job,) in store._db.execute("SELECT job FROM job_runs")})


def _openings(run_root: Path) -> list[tuple[int, int, str, int, int]]:
    with _reader(run_root) as store:
        return [
            tuple(row)
            for row in store._db.execute("SELECT * FROM job_runs ORDER BY seq, n").fetchall()
        ]


# ------------------------------------------------------------------ the named scenarios


def _restarted(tmp_path: Path) -> None:
    """Runs of `j` before and after a restart from a snapshot."""
    root = tmp_path / "run"
    asyncio.run(_incarnation(root, genesis=True, snapshots=1))
    asyncio.run(_incarnation(root, snapshots=1))


_BOXES = (
    "insert_job: b1\njob_type: b\n\ninsert_job: b2\njob_type: b\n\n"
    "insert_job: m\njob_type: c\ncommand: x\nbox_name: {box}\n"
)


def _moved(tmp_path: Path) -> None:
    """An update moves `m` from box `b1` to `b2`; its CONFIG mark names no job."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="b1"))
        await root.engine.run_until_quiescent(_at(2))
        await root.update(3, _BOXES.format(box="b2"))
        await root.feed(_ev(4, "STARTJOB", job="b2"))
        await root.engine.run_until_quiescent(_at(6))

    script = {("m", 1): (60.0, 0), ("m", 2): (60.0, 0)}
    _run(tmp_path, _BOXES.format(box="b1"), body, script)
    marks = [o for o in _openings(tmp_path / "run") if o[2] == "m"]
    assert [rev for *_, rev in marks] == [1, 2]


def _changed_twice(tmp_path: Path) -> None:
    """An operator CHANGE_STATUS after a finished run, then after a bypassed run that
    dispatched nothing: the first rewrites run 1, the second rewrites no run."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(2))
        await root.feed(_ev(3, "STATUS", job="j", status="FAILURE"))
        await root.feed(_ev(4, "ON_NOEXEC", job="j"), _ev(5, "STARTJOB", job="j"))
        await root.feed(_ev(6, "STATUS", job="j", status="TERMINATED"))

    _run(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 0)})
    [row] = _whole(tmp_path / "run", "j")
    assert (row.run_number, row.status) == (1, "FAILURE")


_TIMED = "insert_job: J\njob_type: c\ncommand: x\ncondition: t(J)\nterm_run_time: 1\n"


def _timed_out(tmp_path: Path) -> None:
    """Astra's probe: J restarts on its own termination. The timer terminates J.1 and opens
    J.2 in one admission, and J.1's stopped outcome comes after J.2's dispatch."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "FORCE_STARTJOB", job="J"))
        await root.engine.run_until_quiescent(_at(2.5))

    _run(tmp_path, _TIMED, body)
    root = tmp_path / "run"
    first = _whole(root, "J")[0]
    assert (first.run_number, first.status) == (1, "TERMINATED")
    with _reader(root) as store:
        kinds = [
            (seq, record.kind, record.payload.get("run_number"))
            for seq, record in store.read_from(0)
        ]
    [(second, *_)] = [o for o in _openings(root) if o[3] == 2]
    [ended] = [seq for seq, kind, run in kinds if kind == "outcome" and run == 1]
    assert ended > second


def _retyped(tmp_path: Path) -> None:
    """An empty box J runs, is set STARTING and TERMINATED by injected status, an update
    retypes it to a command, and it runs as one."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="J"))
        await root.feed(_ev(1, "STATUS", job="J", status="STARTING"))
        await root.feed(_ev(2, "STATUS", job="J", status="TERMINATED"))
        await root.update(3, "insert_job: J\njob_type: c\ncommand: x\n")
        await root.feed(_ev(4, "STARTJOB", job="J"), _ev(5, "STATUS", job="J", status="SUCCESS"))

    _run(tmp_path, "insert_job: J\njob_type: b\n", body)


def _empty_box(tmp_path: Path) -> None:
    """An empty box runs twice."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="B"), _ev(1, "STARTJOB", job="B"))

    _run(tmp_path, "insert_job: B\njob_type: b\n", body)


def _true_run(tmp_path: Path) -> None:
    """Runs 1 and 2 complete, then STATUS FAILURE with `run_number=true`: run 1's."""

    async def body(root: _Root) -> None:
        for minutes in (0, 2):
            await root.feed(_ev(minutes, "STARTJOB", job="j"))
            await root.engine.run_until_quiescent(_at(minutes + 1.5))
        await root.feed(_ev(5, "STATUS", job="j", status="FAILURE", run_number=True))

    _run(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 0), ("j", 2): (60.0, 0)})


def _past_integers(tmp_path: Path) -> None:
    """A STATUS naming run 2**63, which the index holds as no run."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(2))
        await root.feed(_ev(3, "STATUS", job="j", status="FAILURE", run_number=2**63))
        await root.feed(_ev(4, "STARTJOB", job="j"))

    _run(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 0)})


_AGAIN = "insert_job: j\njob_type: c\ncommand: x\ncondition: s(j)\n"


def _opened_by_its_own_status(tmp_path: Path) -> None:
    """A STATUS with no run number ends j's run 1 and, by j's own condition, opens run 2 in
    the same admission: the input stands at the opening's own seq, admitted before it."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "FORCE_STARTJOB", job="j"))
        await root.feed(_ev(1, "STATUS", job="j", status="SUCCESS"))
        await root.feed(_ev(2, "STATUS", job="j", status="SUCCESS"))

    _run(tmp_path, _AGAIN, body)
    root = tmp_path / "run"
    with _reader(root) as store:
        statuses = [
            seq
            for seq, record in store.read_from(0)
            if record.kind == "input" and record.payload.get("kind") == "STATUS"
        ]
    opened = [seq for seq, _, job, run, _ in _openings(root) if job == "j" and run > 1]
    assert opened and set(opened) <= set(statuses)
    rows = _whole(root, "j")
    assert [(row.run_number, row.status) for row in rows][:2] == [(1, "SUCCESS"), (2, "SUCCESS")]


_SCENARIOS: dict[str, Callable[[Path], None]] = {
    "a restart": _restarted,
    "an update that moves a job to another box": _moved,
    "a job removed and added again": _readded,
    "a CHANGE_STATUS after a finished run and after an undispatched one": _changed_twice,
    "KILLJOB": _killed,
    "term_run_time, its close after the next opening": _timed_out,
    "a reconcile completion with no end": _reconciled,
    "a rejected late exit": _late_exit,
    "a retry": _retried,
    "an update that replaces an attempt": _replaced,
    "an update between two runs": _rev_between_runs,
    "a box retyped to a command": _retyped,
    "an empty box": _empty_box,
    "an ON_NOEXEC bypass": _bypassed,
    "a STATUS with run_number true": _true_run,
    "a STATUS with run number 2**63": _past_integers,
    "a STATUS that opens its job's next run": _opened_by_its_own_status,
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_the_pages_of_each_job_joined_equal_the_whole_fold(tmp_path: Path, name: str) -> None:
    _SCENARIOS[name](tmp_path)
    root = tmp_path / "run"
    jobs = _jobs(root)
    assert jobs
    compared = 0
    for job in jobs:
        whole = _whole(root, job)
        compared += len(whole)
        for limit in (1, 2, 20):
            assert _paged(root, job, limit) == whole, (job, limit)
    assert compared


def test_a_status_whose_admission_fires_an_overdue_timer_after_a_restart(tmp_path: Path) -> None:
    """Astra's sequence: J restarts on its own termination with a one-minute term_run_time; the
    engine goes down while J.1 runs and restarts after the deadline. An unnumbered SUCCESS at
    08:01:30 fires the overdue timer: J.1 is TERMINATED at 08:01 and J.2 opens at 08:01 in that
    admission. Every page width reads J.1 as the whole fold does."""
    root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(_TIMED),
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(0, "FORCE_STARTJOB", job="J"))
        await engine.run_until_quiescent(_at(0.5))
        await _close(engine)

    async def second() -> None:
        engine = await resume_run(
            lower_source(_TIMED),
            root,
            clock=VirtualClock(start=_at(1.5)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(1.5, "STATUS", job="J", status="SUCCESS"))
        await engine.run_until_quiescent(_at(1.5))
        await _close(engine)

    asyncio.run(first())
    asyncio.run(second())
    with _reader(root) as store:
        [status] = [
            seq
            for seq, record in store.read_from(0)
            if record.kind == "input" and record.payload.get("kind") == "STATUS"
        ]
    assert [o[0] for o in _openings(root) if o[3] == 2] == [status]
    whole = _whole(root, "J")
    assert (whole[0].run_number, whole[0].status, whole[0].ended_at) == (1, "TERMINATED", _at(1))
    for limit in (1, 2, 20):
        assert _paged(root, "J", limit) == whole, limit


def _after_an_overdue_restart(
    tmp_path: Path, admit: Callable[[Any], Awaitable[None]], minutes: float
) -> Path:
    """J restarts on its own termination with a one-minute term_run_time; the engine goes down
    while J.1 runs and restarts at `minutes`, where `admit` gives it one input first."""
    root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(_TIMED),
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(0, "FORCE_STARTJOB", job="J"))
        await engine.run_until_quiescent(_at(0.5))
        await _close(engine)

    async def second() -> None:
        engine = await resume_run(
            lower_source(_TIMED),
            root,
            clock=VirtualClock(start=_at(minutes)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await admit(engine)
        await _close(engine)

    asyncio.run(first())
    asyncio.run(second())
    return root


def _status_inputs(root: Path) -> list[tuple[int, Record]]:
    with _reader(root) as store:
        return [
            (seq, record)
            for seq, record in store.read_from(0)
            if record.kind in ("input", "decision")
            and (record.kind == "decision" or record.payload.get("kind") == "STATUS")
        ][-2:]


def test_one_admission_that_opens_two_runs_is_read_whole(tmp_path: Path) -> None:
    """Astra's round-3 sequence: after the restart an unnumbered TERMINATED fires the overdue
    timer; J.1 ends, J.2 opens, the input ends J.2 and J.3 opens, all at one seq. The page of
    runs 2 and 1 holds the input and its decision, and every width reads as the whole fold."""

    async def admit(engine: Any) -> None:
        engine.inject(_ev(1.5, "STATUS", job="J", status="TERMINATED"))
        await engine.run_until_quiescent(_at(1.5))

    root = _after_an_overdue_restart(tmp_path, admit, 1.5)
    [(status, _), (decided, _)] = _status_inputs(root)
    assert [o[0] for o in _openings(root) if o[3] in (2, 3)] == [status, status]
    with _reader(root) as store:
        page = store.read_history(HistoryQuery("J", 3, 2, store.last_seq(), BUDGET))
    assert [run.run_number for run in page.runs] == [2, 1]
    assert [seq for seq, _ in page.loose] == [status, decided]
    whole = _whole(root, "J")
    for limit in (1, 2, 20):
        assert _paged(root, "J", limit) == whole, limit


def test_an_input_at_the_opening_above_the_page_is_the_page_s(tmp_path: Path) -> None:
    """j restarts on its own failure; twice an operator FAILURE with exit 7 and no run number
    ends a run and opens the next in its own admission. Each input stands at the seq of the
    opening above its run's page and belongs to that page: every width reads run 1 FAILURE
    with exit 7, as the whole fold does."""

    async def body(root: _Root) -> None:
        await root.feed(_ev(0, "FORCE_STARTJOB", job="j"))
        for minutes in (1, 2):
            await root.feed(_ev(minutes, "STATUS", job="j", status="FAILURE", exit_code=7))

    _run(tmp_path, "insert_job: j\njob_type: c\ncommand: x\ncondition: f(j)\n", body)
    root = tmp_path / "run"
    whole = _whole(root, "j")
    assert (whole[0].run_number, whole[0].status, whole[0].exit_code) == (1, "FAILURE", 7)
    for limit in (1, 2, 20):
        assert _paged(root, "j", limit) == whole, limit


def test_a_rejected_status_whose_time_half_opens_the_next_run_is_left_out(
    tmp_path: Path,
) -> None:
    """After the restart a SUCCESS naming no run arrives exactly at J.1's deadline and is
    rejected; its time half fires the timer, ends J.1 and opens J.2 at that instant. The fold
    gives J.1 the rejected input unless it reads the decision one seq past J.2's opening, so
    every width reads J.1 TERMINATED, as the whole fold does."""

    async def admit(engine: Any) -> None:
        stale = Envelope(request_id="r1", expect={"job:J": 999})
        decided = engine.submit(_ev(1, "STATUS", job="J", status="SUCCESS"), stale)
        await engine.run_until_quiescent(_at(1))
        assert (await decided).decision == "rejected"

    root = _after_an_overdue_restart(tmp_path, admit, 1)
    [(status, _), (decided, decision)] = _status_inputs(root)
    assert decision.payload["decision"] == "rejected" and decided == status + 1
    assert [o[0] for o in _openings(root) if o[3] == 2] == [status]
    whole = _whole(root, "J")
    assert (whole[0].run_number, whole[0].status) == (1, "TERMINATED")
    for limit in (1, 2, 20):
        assert _paged(root, "J", limit) == whole, limit


def test_a_pruned_store_reads_a_run_under_the_rev_its_opening_stored(tmp_path: Path) -> None:
    """Astra's sequence: rev 2 has `max_exit_success: 0`, rev 3 has 5, a snapshot follows rev 3,
    and a run opened after it exits 3. A prune leaves rev 2's record as the first retained one.
    The page reads the run under rev 3, as the whole fold did before the prune (BUG-110 is the
    offline fold after it)."""

    def text(command: str, most: int) -> str:
        return f"insert_job: j\njob_type: c\ncommand: {command}\nmax_exit_success: {most}\n"

    async def body(root: _Root) -> None:
        await root.update(1, text("y", 0))
        await root.update(2, text("z", 5))
        root.engine.take_snapshot()
        await root.feed(_ev(3, "STARTJOB", job="j"))
        await root.engine.run_until_quiescent(_at(5))

    _run(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 3)})
    root = tmp_path / "run"
    before = _whole(root, "j")
    assert [(row.run_number, row.status, row.exit_code) for row in before] == [(1, "SUCCESS", 3)]
    with closing(SqliteStore(root / STORE_NAME)) as store:
        [applied] = [
            seq
            for seq, record in store.read_from(0)
            if record.kind == "config" and record.payload.get("rev") == 2
        ]
        assert store.prune_before(applied, T0 + timedelta(days=365)) > 0
        assert store.first_seq() == applied
    assert _paged(root, "j", 1) == before


def test_a_page_holds_whole_admissions_and_counts_every_row_it_reads(tmp_path: Path) -> None:
    """Run 1 of a job whose own STATUS opened run 2: its page holds run 2's opening entry, the
    input at that opening's seq and its decision one seq past it, and the smallest budget that
    holds the page is the count of the rows it read."""
    _opened_by_its_own_status(tmp_path)
    root = tmp_path / "run"
    [(upper, *_)] = [o for o in _openings(root) if o[3] == 2]
    with _reader(root) as store:
        read = store.read_history(HistoryQuery("j", 2, 1, store.last_seq(), BUDGET))
        used = len(read.entries) + len(read.owned) + len(read.loose)
        assert [(seq, record.kind) for seq, record in read.loose][-2:] == [
            (upper, "input"),
            (upper + 1, "decision"),
        ]
        assert any(entry.get("run_number") == 2 for _, _, entry in read.entries)
        assert _evidence(store, "j", 1) == used


def test_a_record_the_index_holds_below_full_from_is_read_where_it_sits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's sequence: this build indexes an injected FAILURE naming run 2, which has not
    opened; the build before it runs a stretch with no index; this build returns and run 2
    opens above `full_from`. The page reads the indexed record below `full_from`, as the whole
    fold does."""
    from test_transitions import _latest

    root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(root)
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(10))
        status = {"job": "j", "status": "FAILURE", "run_number": 2, "exit_code": 7}
        engine.inject(_ev(11, "STATUS", **status))
        await engine.run_until_quiescent(_at(12))
        await _close(engine)

    async def gap() -> None:
        engine = await resume_run(
            lower_source(_JIL),
            root,
            clock=VirtualClock(start=_latest(root) + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)

    asyncio.run(first())
    with _reader(root) as store:
        indexed = [
            seq for seq, owner, run in store._db.execute("SELECT * FROM job_records") if run == 2
        ]
    assert indexed
    with _as_transitions_build(monkeypatch):
        asyncio.run(gap())
    found, _ = asyncio.run(_history_after_restart(root))
    assert max(indexed) < found["full_from"]["seq"]
    [row] = [run["row"] for run in found["runs"] if run["row"]["run_number"] == 2]
    assert (row["status"], row["exit_code"]) == ("FAILURE", 7)
    whole = _whole(root, "j")
    for limit in (1, 2, 20):
        assert _paged(root, "j", limit) == [r for r in whole if r.run_number == 2], limit


def _two_runs(tmp_path: Path, *, later: bool) -> Path:
    """Two runs of `j`; with `later`, an operator STATUS after run 2 ended."""

    async def body(root: _Root) -> None:
        for minutes in (0, 2):
            await root.feed(_ev(minutes, "STARTJOB", job="j"))
            await root.engine.run_until_quiescent(_at(minutes + 1.5))
        if later:
            await root.feed(_ev(4, "STATUS", job="j", status="FAILURE"))

    _run(tmp_path, _LEAF.format(cmd="x"), body, {("j", 1): (60.0, 0), ("j", 2): (60.0, 0)})
    return tmp_path / "run"


def test_a_page_reads_no_record_more_than_one_seq_past_the_opening_above_it(
    tmp_path: Path,
) -> None:
    """Run 1 needs the same budget with and without an unnumbered STATUS after run 2 ended:
    the page reads up to one seq past run 2's opening and no further."""
    plain = _two_runs(tmp_path / "plain", later=False)
    later = _two_runs(tmp_path / "later", later=True)
    [(upper, *_)] = [o for o in _openings(later) if o[3] == 2]
    with _reader(plain) as one, _reader(later) as other:
        assert _evidence(one, "j", 1) == _evidence(other, "j", 1)
        read = other.read_history(HistoryQuery("j", 2, 1, other.last_seq(), BUDGET))
    assert all(seq <= upper + 1 for seq, _ in read.loose)


def test_a_stop_while_the_items_are_sized_ends_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _three_runs(tmp_path)
    sized: list[object] = []
    real = runner_runs._size

    def sizing(value: object) -> int:
        sized.append(value)
        return real(value)

    monkeypatch.setattr(runner_runs, "_size", sizing)
    with _reader(root) as store, pytest.raises(Stopped):
        _read_stopped(store, runner_runs.REPLY_BYTES, lambda: bool(sized))
    assert len(sized) == 1


def _memory_root(text: str) -> Store:
    async def scenario() -> Store:
        engine = start_run(
            lower_source(text),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (30.0, 0)}, default=None)},
        )
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(2))
        await engine.shutdown()
        assert engine.journal is not None
        return engine.journal.store

    return asyncio.run(scenario())


def test_configurations_are_decoded_once_per_stored_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two stores hold different bytes under rev 1: each is decoded once and kept by the digest
    of its bytes, never by its rev; past the bound the oldest goes."""
    from dsl41.runner_config import Configuration

    real = Configuration.from_bytes
    decoded: list[str] = []

    def counted(data: bytes, *, where: str) -> Configuration:
        decoded.append(where)
        return real(data, where=where)

    monkeypatch.setattr(runner_runs, "_kept", type(runner_runs._kept)())
    monkeypatch.setattr(Configuration, "from_bytes", staticmethod(counted))
    first, second = _memory_root(_LEAF.format(cmd="x")), _memory_root(_LEAF.format(cmd="y"))
    one, other = _read(first, "j"), _read(second, "j")
    assert _read(first, "j") == one and len(decoded) == 2
    assert one["runs"][0]["row"]["catalog_hash"] != other["runs"][0]["row"]["catalog_hash"]
    monkeypatch.setattr(runner_runs, "CONFIGURATIONS_KEPT", 1)
    _read(_memory_root(_LEAF.format(cmd="z")), "j")
    _read(first, "j")
    assert len(decoded) == 4


# ------------------------------------------------------------------ attempts


def _attempts(run_root: Path, job: str) -> list[list[dict[str, Any]]]:
    with _reader(run_root) as store:
        page = _read(store, job)
    return [run["attempts"] for run in page["runs"]]


def test_a_retry_shows_each_dispatch_with_its_outcome(tmp_path: Path) -> None:
    _retried(tmp_path)
    [attempts] = _attempts(tmp_path / "run", "j")
    assert [a["ordinal"] for a in attempts] == [1, 2]
    assert [a["outcome"]["exit_code"] for a in attempts] == [3, 3]
    assert attempts[0]["after"] is None and attempts[1]["after"] == "exit 3"
    assert all(a["outcome"]["ended_at"] and a["outcome"]["kind"] == "natural" for a in attempts)
    assert all("env" not in a and a["log_out"] is not None for a in attempts)


def test_a_replacement_shows_the_stopped_attempt_and_the_one_after_it(tmp_path: Path) -> None:
    _replaced(tmp_path)
    [attempts] = _attempts(tmp_path / "run", "j")
    first, second = attempts[:2]
    assert first["outcome"] is not None and second["after"] == "update_stop"
    assert second["rev"] > first["rev"]


def test_a_dispatch_with_no_outcome_after_a_crash_reads_null(tmp_path: Path) -> None:
    """The engine went down with `k`'s attempt open; before the restart ends it, the store holds
    its dispatch and no outcome."""
    root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(_JIL + "\ninsert_job: k\njob_type: c\ncommand: y\n"),
            root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"k": "posts twice"},
        )
        engine.inject(_ev(0, "STARTJOB", job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())
    [[attempt]] = _attempts(root, "k")
    assert attempt["outcome"] is None and attempt["ordinal"] == 1


def _another_attempt(first: Record, ordinal: int) -> Record:
    """A copy of dispatch `first` as attempt `ordinal` of the same run, with its own id."""
    body = dict(first.payload)
    attempt = _ATTEMPT.format(ordinal)
    tag = json.loads(base64.urlsafe_b64decode(body["env"]["DSL41_RUN"]))
    run_tag = base64.urlsafe_b64encode(json.dumps({**tag, "run_id": attempt}).encode()).decode()
    env = {
        **body["env"],
        "DSL41_ATTEMPT": str(ordinal),
        "AUTORUN": f"{body['first_dispatch_seq']}-{ordinal}",
        "DSL41_RUN": run_tag,
    }
    return Record(
        first.at_utc,
        "dispatch",
        None,
        None,
        body | {"attempt": attempt, "ordinal": ordinal, "env": env},
    )


def test_more_than_fifty_attempts_keep_the_newest_with_a_mark() -> None:
    """Fifty-two dispatches of one run, the later ones appended to the store as the writer
    would: the newest fifty are shown, with the mark."""

    async def scenario() -> Store:
        engine = start_run(
            lower_source(_LEAF.format(cmd="x")),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(1))
        await engine.shutdown()
        assert engine.journal is not None
        return engine.journal.store

    store = asyncio.run(scenario())
    [first] = [record for _, record in store.read_from(0) if record.kind == "dispatch"]
    for ordinal in range(2, 53):
        store.append(_another_attempt(first, ordinal), owners=[RecordOwner(0, "j", 1)])
    [run] = _read(store, "j")["runs"]
    assert run["attempts_cut"] is True and len(run["attempts"]) == runner_runs.ATTEMPTS_MAX
    assert run["attempts"][-1]["ordinal"] == 52 and run["attempts"][0]["ordinal"] == 3
    assert all(attempt["outcome"] is None for attempt in run["attempts"])


# ------------------------------------------------------------------ the boundary and earlier


def _snapshot_seq(run_root: Path) -> int:
    with _reader(run_root) as store:
        snapshot = store.latest_snapshot()
    assert snapshot is not None
    return snapshot.seq


async def _history_after_restart(run_root: Path, job: str = "j") -> tuple[Any, Any]:
    """Restart the root, run one start and kill of `job`, and ask `history`."""
    from test_transitions import _latest

    engine = await resume_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=_latest(run_root) + timedelta(minutes=1)),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    await engine.run_until_quiescent(engine.clock.now())
    now = engine.clock.now()
    engine.inject(Event(at=now, kind="FORCE_STARTJOB", payload={"job": job}))
    engine.inject(Event(at=now + timedelta(seconds=6), kind="KILLJOB", payload={"job": job}))
    await engine.run_until_quiescent(now + timedelta(minutes=1))
    server = _server(engine, run_root)
    found = await server._respond({"cmd": "history", "job": job})
    await _close(engine)
    return found, engine


def test_a_root_with_earlier_records_starts_its_full_rows_at_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A previous build ran `j` twice around a snapshot; this build resumes from it. Full rows
    start at this build's first record, `earlier` holds the snapshot's windows below the first
    full opening, and the run the previous build opened after the snapshot is in neither
    list: `missing` names its seqs."""
    root = tmp_path / "run"
    with _as_previous_build(monkeypatch):
        asyncio.run(_incarnation(root, genesis=True, snapshots=1))
    last, snapshot = _last_seq(root), _snapshot_seq(root)
    found, engine = asyncio.run(_history_after_restart(root))
    assert engine.restored_seq == snapshot
    assert found["ok"] is True and found["full_from"]["seq"] == last + 1
    assert found["full_from"]["at"] is not None and found["watermark"] >= last + 1
    assert [run["row"]["run_number"] for run in found["runs"]] == [3]
    earlier = found["earlier"]
    assert earlier["snapshot_seq"] == snapshot and earlier["missing"] == [snapshot + 1, last]
    assert [window["run"] for window in earlier["windows"]] == [1]
    assert found["next"] == 1 and found["more"] is False
    below = _read_root(root, "j", cursor=3, restored=Restored(snapshot, earlier["windows"]))
    assert below["runs"] == [] and [w["run"] for w in below["earlier"]["windows"]] == [1]


def _last_seq(run_root: Path) -> int:
    with _reader(run_root) as store:
        return store.last_seq()


def _read_root(run_root: Path, job: str, **kwargs: Any) -> dict[str, Any]:
    with _reader(run_root) as store:
        return _read(store, job, **kwargs)


def test_a_fresh_root_has_no_restored_snapshot_and_no_earlier_windows(tmp_path: Path) -> None:
    root = tmp_path / "run"

    async def scenario() -> tuple[Any, Any]:
        engine = _start_engine(root)
        engine.inject(_ev(0, "STARTJOB", job="j"))
        await engine.run_until_quiescent(_at(1))
        found = await _server(engine, root)._respond({"cmd": "history", "job": "j"})
        await _close(engine)
        return found, engine

    found, engine = asyncio.run(scenario())
    assert engine.restored_seq is None
    assert found["full_from"]["seq"] == 1 and found["more"] is False
    assert found["earlier"] == {"snapshot_seq": None, "missing": None, "windows": []}
    assert [run["row"]["run_number"] for run in found["runs"]] == [1]


def test_a_restart_with_no_snapshot_restores_no_windows(tmp_path: Path) -> None:
    root = tmp_path / "run"
    asyncio.run(_incarnation(root, genesis=True))
    found, engine = asyncio.run(_history_after_restart(root))
    assert engine.restored_seq is None and found["earlier"]["windows"] == []
    assert [run["row"]["run_number"] for run in found["runs"]] == [2, 1]


def test_down_a_build_and_back_moves_the_boundary_to_the_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This build runs `j`, the build before it runs `j` with no index, and this build returns:
    full rows start at the return, and no run from before the gap is a full row."""
    root = tmp_path / "run"
    asyncio.run(_incarnation(root, genesis=True))
    with _as_transitions_build(monkeypatch):
        asyncio.run(_incarnation(root, snapshots=1))
    back = _last_seq(root) + 1
    found, _ = asyncio.run(_history_after_restart(root))
    assert found["full_from"]["seq"] == back
    full = [run["row"]["run_number"] for run in found["runs"]]
    assert full == [o[3] for o in _openings(root) if o[0] >= back]
    assert all(o[3] not in full for o in _openings(root) if o[0] < back)
    with _reader(root) as store:
        assert all(
            page["runs"] == [] or page["full_from"]["seq"] == back for page in _pages(store, "j", 1)
        )


def test_pruning_keeps_the_rows_of_the_retained_runs(tmp_path: Path) -> None:
    root = tmp_path / "run"
    asyncio.run(_incarnation(root, genesis=True, snapshots=1))
    copy = tmp_path / "copy" / "run"
    shutil.copytree(root, copy)
    before = _paged(copy, "j", 1)
    with closing(SqliteStore(copy / STORE_NAME)) as store:
        snapshot = store.latest_snapshot()
        assert snapshot is not None
        assert store.prune_before(snapshot.seq, T0 + timedelta(days=365)) > 0
        first = store.first_seq()
    after = _paged(copy, "j", 1)
    kept = {o[3] for o in _openings(copy) if o[0] >= first}
    assert after and len(after) < len(before)
    assert after == [row for row in before if row.run_number in kept]
    assert _read_root(copy, "j")["full_from"]["seq"] == first


# ------------------------------------------------------------------ the budget and the decline


def _three_runs(tmp_path: Path) -> Path:
    """Three runs of `j`; an operator STATUS after run 2 makes its evidence the largest."""

    async def body(root: _Root) -> None:
        for minutes in (0, 2, 5):
            await root.feed(_ev(minutes, "STARTJOB", job="j"))
            await root.engine.run_until_quiescent(_at(minutes + 1.5))
            if minutes == 2:
                await root.feed(_ev(4, "STATUS", job="j", status="FAILURE"))

    script = {("j", run): (60.0, 0) for run in (1, 2, 3)}
    _run(tmp_path, _LEAF.format(cmd="x"), body, script)
    return tmp_path / "run"


def _evidence(store: Store, job: str, run: int) -> int:
    """The smallest budget that holds run `run` alone."""
    budget = 0
    while _read(store, job, limit=1, cursor=run + 1, budget=budget)["incomplete"]:
        budget += 1
    return budget


def _numbers(page: dict[str, Any]) -> list[int]:
    return [run["row"]["run_number"] for run in page["runs"]]


def test_a_short_page_comes_with_more_and_its_cursor_resumes(tmp_path: Path) -> None:
    root = _three_runs(tmp_path)
    with _reader(root) as store:
        whole = _read(store, "j")
        budget = _evidence(store, "j", 3)  # holds the newest run and not the one below it
        short = _read(store, "j", budget=budget)
        rest = _read(store, "j", cursor=short["next"])
    assert _numbers(whole) == [3, 2, 1] and whole["more"] is False
    assert _numbers(short) == [3] and short["more"] is True and short["next"] == 3
    assert short["runs"] + rest["runs"] == whole["runs"]


def test_a_run_larger_than_the_budget_is_incomplete_and_the_next_page_starts_below_it(
    tmp_path: Path,
) -> None:
    root = _three_runs(tmp_path)
    with _reader(root) as store:
        budget = max(_evidence(store, "j", 3), _evidence(store, "j", 1))
        assert _evidence(store, "j", 2) > budget
        page = _read(store, "j", limit=1, budget=budget, cursor=3)
        rest = _read(store, "j", budget=budget, cursor=page["next"])
        last = _read(store, "j", budget=_evidence(store, "j", 1) - 1, cursor=2)
    assert page["incomplete"] == [2] and page["runs"] == [] and page["next"] == 2
    assert page["more"] is True
    assert _numbers(rest) == [1] and rest["more"] is False
    assert last["incomplete"] == [1] and last["more"] is False and last["next"] == 1


def test_an_undispatched_opening_moves_the_cursor_past_it(tmp_path: Path) -> None:
    _bypassed(tmp_path)
    with _reader(tmp_path / "run") as store:
        page = _read(store, "j", limit=1)
        rest = _read(store, "j", limit=1, cursor=page["next"])
    assert page["runs"] == [] and page["next"] == 2 and page["more"] is True
    assert [r["row"]["run_number"] for r in rest["runs"]] == [1] and rest["more"] is False


def _opened(store: Store, *openings: tuple[int, int]) -> None:
    """One record per opening `(run_number, rev)` of `j`, with its index row."""
    for n, (run, rev) in enumerate(openings):
        store.append(
            Record(T0 + timedelta(seconds=n), "engine", None, None, {"event": "x"}),
            transitions=[
                TraceEntry(
                    at=T0, job="j", transition="INACTIVE->STARTING", cause="c", run_number=run
                ).model_dump()
            ],
            openings=[RunOpening(0, "j", run, rev)],
        )


@contextmanager
def _kind_store(kind: str, directory: Path) -> Iterator[Store]:
    store: Store = SqliteStore(directory / "s.db") if kind == "sqlite" else MemoryStore()
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("kind", KINDS)
def test_a_page_that_shows_a_run_number_twice_declines_naming_it(kind: str, tmp_path: Path) -> None:
    """Two openings of `j.2`, written to the index directly: the page declines, and so does a
    page whose edge cuts between them."""
    with _kind_store(kind, tmp_path) as store:
        _opened(store, (1, 1), (2, 1), (2, 1))
        with pytest.raises(Declined, match="shows run 2 of 'j' twice"):
            _read(store, "j")
        with pytest.raises(Declined, match="shows run 2 of 'j' twice"):
            _read(store, "j", limit=1)


@pytest.mark.parametrize("kind", KINDS)
def test_a_missing_configuration_row_is_an_error_that_names_the_rev(
    kind: str, tmp_path: Path
) -> None:
    with _kind_store(kind, tmp_path) as store:
        _opened(store, (1, 7))
        with pytest.raises(Declined, match="no configuration rev 7"):
            _read(store, "j")


def test_a_store_with_no_covered_interval_at_the_watermark_serves_no_full_rows() -> None:
    store = MemoryStore()
    _opened(store, (1, 1))
    store._history_coverage = []
    page = _read(store, "j")
    assert page["full_from"] is None and page["runs"] == []
    assert page["earlier"]["missing"] == [1, 1]


# ------------------------------------------------------------------ the verb


def test_the_verb_is_a_read_tier_query() -> None:
    assert REQUIRED_TIER["history"] is Tier.READ is REQUIRED_TIER["transitions"]


@pytest.mark.parametrize(
    ("request_fields", "names"),
    [
        ({}, "job"),
        ({"job": 3}, "job"),
        ({"job": "j", "cursor": -1}, "cursor"),
        ({"job": "j", "cursor": 2**63}, "cursor"),
        ({"job": "j", "cursor": True}, "cursor"),
        ({"job": "j", "limit": -1}, "limit"),
        ({"job": "j", "limit": 0}, "limit"),
        ({"job": "j", "limit": "all"}, "limit"),
        ({"job": "j", "history": 1}, "history"),
    ],
)
def test_a_malformed_request_is_refused_naming_its_field(
    request_fields: dict[str, Any], names: str
) -> None:
    refused = parse(request_fields)
    assert isinstance(refused, str) and refused.startswith(names)


def test_the_request_defaults_and_its_limit_cap() -> None:
    assert parse({"job": "j"}) == Request("j", None, 20, None)
    assert parse({"job": "j", "limit": 10**6, "cursor": 2**63 - 1, "history": "h"}) == Request(
        "j", 2**63 - 1, 200, "h"
    )


def _engine_with_runs(root: Path | None) -> Any:
    engine = start_run(
        lower_source(_JIL),
        root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter({("j", 1): (30.0, 0)}, default=None)},
    )
    engine.inject(_ev(0, "STARTJOB", job="j"))
    engine.inject(_ev(2, "STARTJOB", job="j"))
    return engine


@pytest.mark.parametrize("rehearsal", [False, True])
def test_the_verb_answers_a_page_with_its_header_and_watermark(
    tmp_path: Path, rehearsal: bool
) -> None:
    """A run root's store is read off the loop, a rehearsal's in process; both answer the same
    page, with the header and watermark taken when the read began."""
    root = None if rehearsal else tmp_path / "run"

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, Any]:
        engine = _engine_with_runs(root)
        await engine.run_until_quiescent(_at(5))
        server = _server(engine, tmp_path)
        found = await server._respond({"v": 5, "cmd": "history", "job": "j", "history": "x"})
        refused = await server._respond({"v": 5, "cmd": "history", "job": "j", "limit": "all"})
        assert engine.journal is not None
        watermark, last_at = engine.journal.last_seq, engine.journal.position.last_at
        await _close(engine)
        return found, refused, watermark, last_at

    found, refused, watermark, last_at = asyncio.run(scenario())
    assert refused["ok"] is False and refused["code"] == "invalid_argument"
    assert found["ok"] is True and found["watermark"] == watermark
    assert found["as_of"] == last_at.isoformat() and found["continuous_through"] == 0
    assert [run["row"]["run_number"] for run in found["runs"]] == [2, 1]
    assert found["runs"][1]["row"]["status"] == "SUCCESS"
    assert set(found) == {
        "ok",
        "job",
        "runs",
        "incomplete",
        "next",
        "more",
        "full_from",
        "history",
        "continuous_through",
        "earlier",
        "watermark",
        "as_of",
        "baseline_id",
        "applied_index",
    }


def _filled(store: Store) -> None:
    """Three openings of `j` and one of `k`, each with its dispatch; an operator STATUS and its
    decision with no run at run 3's opening seq and after it; a CONFIG mark; run 1's outcome
    after run 2 opened. Appended as the writer would, with the index rows."""

    def record(kind: str, n: int, /, **body: Any) -> Record:
        return Record(T0 + timedelta(seconds=n), kind, None, None, body)

    def entry(job: str, transition: str, run: int | None = None) -> dict[str, Any]:
        return TraceEntry(
            at=T0, job=job, transition=transition, cause="c", run_number=run
        ).model_dump()

    store.put_config(b"one", "d1")
    store.put_config(b"two", "d2")
    for n, (job, run, rev) in enumerate([("j", 1, 1), ("k", 1, 1), ("j", 2, 1), ("j", 3, 2)]):
        if run == 3:
            store.append(
                record("config", 10 * n, event="applied", rev=2),
                transitions=[entry("", "CONFIG")],
            )
        store.append(
            record("input", 10 * n, index=n, kind="STATUS", payload={"job": job}),
            record("decision", 10 * n, index=n),
            transitions=[entry(job, "INACTIVE->STARTING", run), entry(job, "STARTING->RUNNING")],
            openings=[RunOpening(0, job, run, rev)],
            owners=[RecordOwner(0, job, None), RecordOwner(1, job, None)],
        )
        store.append(
            record("dispatch", 10 * n + 1, job=job, run_number=run),
            owners=[RecordOwner(0, job, run)],
        )
        if run == 2:
            store.append(
                record("outcome", 10 * n + 2, job="j", run_number=1),
                owners=[RecordOwner(0, "j", 1)],
            )


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"limit": 1},
        {"limit": 2, "cursor": 3},
        {"cursor": 2},
        {"budget": 0},
        {"budget": 5},
        {"budget": 9},
        {"upto": 9},
        {"job": "k"},
        {"job": "x"},
    ],
)
def test_the_memory_store_and_the_file_read_the_same_page(
    tmp_path: Path, query: dict[str, Any]
) -> None:
    reads = []
    for kind in KINDS:
        with _kind_store(kind, tmp_path) as store:
            _filled(store)
            fields = {
                "job": "j",
                "cursor": None,
                "limit": 20,
                "upto": store.last_seq(),
                "budget": BUDGET,
            }
            reads.append(store.read_history(HistoryQuery(**(fields | query))))
    file, memory = reads
    assert file == memory
    if not query:
        assert [run.run_number for run in file.runs] == [3, 2, 1]
        assert [e["transition"] for _, _, e in file.entries].count("CONFIG") == 1
        assert file.configs == {1: b"one", 2: b"two"}


def test_the_verb_without_a_store_is_refused() -> None:
    from dsl41.runner import Engine

    engine = Engine(lower_source(_JIL), clock=VirtualClock(start=T0), adapters={})
    found = asyncio.run(_server(engine, Path("/nonexistent"))._respond({"cmd": "history"}))
    assert found["ok"] is False and found["code"] == "no_journal"


@pytest.mark.parametrize(
    ("patched", "code"),
    [("trim_transitions", "transitions_unavailable"), ("cover_history", "history_unavailable")],
)
def test_the_verb_declines_with_its_reason_while_a_start_reason_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched: str, code: str
) -> None:
    root = tmp_path / "run"
    with _as_previous_build(monkeypatch):  # so the start has a gap to record
        asyncio.run(_incarnation(root, genesis=True))

    def broken(*args: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as context:
        context.setattr(SqliteStore, patched, broken)
        found, _ = asyncio.run(_history_after_restart(root))
    assert found["ok"] is False and found["code"] == code and "disk full" in found["error"]
    served, _ = asyncio.run(_history_after_restart(root))
    assert served["ok"] is True


def test_a_decline_of_the_page_answers_with_its_reason(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        engine = _engine_with_runs(None)
        await engine.run_until_quiescent(_at(5))
        assert engine.journal is not None
        store = engine.journal.store
        assert isinstance(store, MemoryStore)
        store._job_runs.append(store._job_runs[-1])
        found = await _server(engine, tmp_path)._respond({"cmd": "history", "job": "j"})
        await _close(engine)
        return found

    found = asyncio.run(scenario())
    assert found["ok"] is False and found["code"] == "history_unavailable"
    assert "shows run 2 of 'j' twice" in found["error"]


def test_the_answer_cap_drops_the_oldest_rows_first(tmp_path: Path) -> None:
    _true_run(tmp_path)
    windows = [{"run": 0, "started_at": "x" * 4000}]
    with _reader(tmp_path / "run") as store:
        full = _read(store, "j", restored=Restored(None, windows))
        size = _size(full)
        windowless = _read(store, "j", restored=Restored(None, windows), byte_cap=size - 1)
        one = _read(store, "j", byte_cap=_size(_read(store, "j")) - 1)
        bare = _size(_read(store, "j", cursor=1))
        alone = _read(store, "j", limit=1, byte_cap=bare + 20)
    assert [w["run"] for w in full["earlier"]["windows"]] == [0]
    assert windowless["earlier"]["windows"] == [] and windowless["more"] is True
    assert len(windowless["runs"]) == 2 and windowless["next"] == 1
    assert _size(windowless) <= size - 1
    assert [r["row"]["run_number"] for r in one["runs"]] == [2]
    assert one["more"] is True and one["next"] == 2 and one["earlier"] is None
    assert alone["runs"] == [] and alone["incomplete"] == [2] and alone["next"] == 2


def test_every_cap_gives_an_answer_within_it_whose_cursor_moves(tmp_path: Path) -> None:
    """For caps from an answer of one number up to the whole one, the answer fits, and one
    that says `more` holds something below its cursor; paging at that cap reads every run and
    window once, oldest last."""
    root = _three_runs(tmp_path)
    windows = [{"run": 0, "started_at": "x" * 300}]
    restored = Restored(None, windows)
    with _reader(root) as store:
        whole = _read(store, "j", restored=restored)
        expected = [*_numbers(whole), 0]
        smallest = _size(_read(store, "j", cursor=0)) + 40  # an empty page and one number
        for cap in range(smallest, _size(whole) + 1, 97):
            seen: list[int] = []
            for page in _pages(store, "j", 20, restored=restored, byte_cap=cap):
                assert _size(page) <= cap
                seen += _numbers(page) + page["incomplete"]
                seen += [w["run"] for w in (page["earlier"] or {}).get("windows", [])]
            assert seen == expected, cap


def test_a_window_that_passes_the_cap_alone_is_passed_as_incomplete(tmp_path: Path) -> None:
    _true_run(tmp_path)
    restored = Restored(None, [{"run": 0, "started_at": "x" * 4000}])
    with _reader(tmp_path / "run") as store:
        cap = _size(_read(store, "j", cursor=1, restored=Restored(None, []))) + 20
        page = _read(store, "j", cursor=1, restored=restored, byte_cap=cap)
    assert page["incomplete"] == [0] and page["next"] == 0 and page["more"] is False
    assert page["earlier"]["windows"] == []


def test_an_answer_that_cannot_hold_one_item_is_refused(tmp_path: Path) -> None:
    """An unknown job whose name alone passes the cap: no page, so no cursor that stands
    still; the server answers `invalid_argument`."""
    from dsl41.runner_transitions import AnswerTooLarge

    store = MemoryStore()
    _opened(store, (1, 1))
    with pytest.raises(AnswerTooLarge, match="no run or window in it"):
        _read(store, "x" * 2000, byte_cap=1000)

    async def scenario() -> dict[str, Any]:
        engine = _engine_with_runs(None)
        await engine.run_until_quiescent(_at(5))
        server = _server(engine, tmp_path)
        server.history_bytes = 1000
        found = await server._respond({"cmd": "history", "job": "x" * 2000})
        await _close(engine)
        return found

    found = asyncio.run(scenario())
    assert found["ok"] is False and found["code"] == "invalid_argument"


def _size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True).encode("utf-8"))


def test_a_read_stopped_before_it_was_folded_raises() -> None:
    store = MemoryStore()
    _opened(store, (1, 1))
    with pytest.raises(Stopped, match="before its answer was shaped"):
        answer(
            store,
            Request("j", None, 20, None),
            upto=store.last_seq(),
            header={},
            restored=Restored(None, []),
            stopped=lambda: True,
        )


def test_a_stop_after_the_fold_or_while_the_answer_is_cut_ends_the_read(tmp_path: Path) -> None:
    """The read checks its stop between configuration decodes, after the fold and in each turn
    that cuts the answer to the cap."""
    root = _three_runs(tmp_path)
    with _reader(root) as store:
        whole = _read(store, "j")
        raised = 0
        for after in range(1, 12):
            calls = iter(range(after))

            def stopped() -> bool:
                return next(calls, None) is None

            for cap in (_size(whole), _size(whole) // 2):
                try:
                    _read_stopped(store, cap, stopped)
                except Stopped:
                    raised += 1
                calls = iter(range(after))
        assert raised > 2


def _read_stopped(store: Store, cap: int, stopped: Callable[[], bool]) -> dict[str, Any]:
    return answer(
        store,
        Request("j", None, 20, None),
        upto=store.last_seq(),
        header={},
        restored=Restored(None, []),
        byte_cap=cap,
        stopped=stopped,
    )


def test_a_read_in_flight_does_not_hold_up_an_admission(tmp_path: Path) -> None:
    """The read runs off the engine's loop: while it holds a read transaction open on its own
    connection, the loop admits a control input and commits its decision; the read then
    answers as of the watermark it was given."""
    from dsl41 import runner_control

    root = tmp_path / "run"
    entered, release = threading.Event(), threading.Event()
    real = runner_control.history_answer

    def held(store: SqliteStore, request: Request, **kwargs: Any) -> Any:
        store._db.execute("BEGIN")
        store._db.execute("SELECT COUNT(*) FROM job_runs").fetchone()
        entered.set()
        assert release.wait(30)
        store._db.execute("COMMIT")
        return real(store, request, **kwargs)

    async def scenario() -> tuple[dict[str, Any], int]:
        engine = _start_engine(root)
        await engine.run_until_quiescent(T0)
        server = _server(engine, tmp_path)
        assert engine.journal is not None
        before = engine.journal.last_seq
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(runner_control, "history_answer", held)
            reading = asyncio.ensure_future(server._respond({"cmd": "history", "job": "j"}))
            await asyncio.to_thread(entered.wait, 30)
            decided = engine.submit(
                _ev(0, "STARTJOB", job="j"),
                Envelope(request_id="r1", expect={"job:j": engine.oracle.store.revision("job:j")}),
            )
            await engine.run_until_quiescent(T0 + timedelta(seconds=1))
            result = await decided
            assert result.decision == "applied" and not reading.done()
            release.set()
            found = await reading
        await _close(engine)
        return found, before

    found, before = asyncio.run(scenario())
    assert found["ok"] is True and found["watermark"] == before and found["runs"] == []


# ------------------------------------------------------------------ the command line


def test_query_history_sends_the_job_and_its_paging(
    monkeypatch: pytest.MonkeyPatch, cli_env: None
) -> None:
    result, sent = _query(monkeypatch, "history", "--job", "j")
    assert result.exit_code == 0, result.output
    assert sent == [{"cmd": "history", "job": "j"}]
    result, sent = _query(monkeypatch, "history", "--job", "j", "--limit", "5", "--cursor", "41")
    assert result.exit_code == 0, result.output
    assert sent == [{"cmd": "history", "job": "j", "limit": 5, "cursor": 41}]


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("history",), "history requires --job"),
        (("history", "--job", "j", "--cursor", "4:1"), "--cursor '4:1' is not a run number"),
        (("history", "--job", "j", "--forward"), "--forward applies to transitions only"),
        (
            ("history", "--job", "j", "--limit", "1", "--cursor", "3", "--forward"),
            "--forward applies to transitions only",
        ),
        (("history", "--job", "j", "--cursor", "9" * 4301), "is not a run number"),
        (("history", "--job", "j", "--since", "3"), "history takes --cursor"),
        (("history", "--job", "j", "--name", "G"), "--name applies to global and globals"),
    ],
)
def test_query_history_refuses_by_name_what_it_does_not_take(
    monkeypatch: pytest.MonkeyPatch, cli_env: None, arguments: tuple[str, ...], error: str
) -> None:
    result, sent = _query(monkeypatch, *arguments)
    assert result.exit_code == 2 and error in result.output
    assert sent == []
