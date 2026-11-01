"""A restart's decision for a live start the crash took (DL-102, DL-315, DL-424).

A start's admission is one transaction, its `dispatch` record, naming the attempt's `run_id`,
is durable before its adapter task exists, and its spool is made after (runner-design ss7). So
a restart reads three cases off the store:

  * a `dispatch` record and no outcome: an open attempt. A command is settled from its spool
    (with no spool, unobservable, and the interruption rule runs it again); a watch is
    relaunched under the recorded `run_id`;
  * no record, and replay derives the start: it was decided and never launched, and the restart
    dispatches it once the due work is processed;
  * neither: the live engine launched nothing for the run, and the restart launches nothing.

These tests moved here from the routing-table suite, which held the same restarts behind a
drained host.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_WATCH_JIL = "insert_job: w\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


class _CountingWatch(FakeAdapter):
    """An inert adapter that records each run, with the identity it was given."""

    def __init__(self) -> None:
        super().__init__(default=None)
        self.calls: list[tuple[str, int, str | None]] = []

    async def run(self, job_ir, run_number, ctx):  # type: ignore[no-untyped-def]
        self.calls.append((job_ir.name, run_number, ctx.run_id))
        return await super().run(job_ir, run_number, ctx)


def _count_launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every `Engine._launch` from now on, whether or not its task ever runs."""
    launches: list[tuple[str, int]] = []
    real = Engine._launch

    def counted(self, job_ir, run_number, adapter, **kwargs):  # type: ignore[no-untyped-def]
        launches.append((job_ir.name, run_number))
        real(self, job_ir, run_number, adapter, **kwargs)

    monkeypatch.setattr(Engine, "_launch", counted)
    return launches


def _genesis_engine(run_root: Path, jil: str, job_type: str) -> Engine:
    return start_run(
        lower_source(jil),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={job_type: FakeAdapter(default=None)},
    )


async def _resume(run_root: Path, jil: str, adapter: FakeAdapter, minutes: float = 4) -> Engine:
    job_type = "FW" if "job_type: f" in jil else "CMD"
    return await resume_run(
        lower_source(jil),
        run_root,
        clock=VirtualClock(start=T0 + timedelta(minutes=minutes)),
        adapters={job_type: adapter},
    )


async def _close_engine(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _records(run_root: Path) -> list[dict[str, Any]]:
    return read_journal(run_root)


def _dispatches(run_root: Path) -> list[tuple[str, int, str]]:
    return [
        (str(r["job"]), int(r["run_number"]), str(r["run_id"]))
        for r in _records(run_root)
        if r["rec"] == "dispatch"
    ]


def _reconciled(run_root: Path) -> list[dict[str, object]]:
    """The completions resume's ladder injected, by payload."""
    return [
        r["payload"]
        for r in _records(run_root)
        if r.get("rec") == "input" and r.get("source") == "reconcile"
    ]


def _decisions_at(run_root: Path, index: int) -> list[dict[str, object]]:
    return [r for r in _records(run_root) if r["rec"] == "decision" and r["index"] == index]


async def _die_before_the_dispatch(engine: Engine, ev: Event) -> int:
    """Admit `ev` and kill the engine after its admission and before any `dispatch` record it
    implies (DL-102's window). Returns the attempt's index."""
    assert engine.journal is not None
    journal = engine.journal
    real = journal.append

    def append(*records: Any) -> int:
        if any(record.kind == "dispatch" for record in records):
            raise RuntimeError("the engine died before the dispatch record")
        return real(*records)

    journal.append = append  # type: ignore[method-assign]
    engine.inject(ev)
    with contextlib.suppress(RuntimeError):
        await engine.run_until_quiescent(ev.at + timedelta(minutes=1))
    await _close_engine(engine)
    records = _records(engine.journal.path)  # type: ignore[arg-type]
    index = max(int(r["seq"]) for r in records if r["rec"] == "input")
    assert [r for r in records if r["rec"] == "decision" and r["index"] == index]
    return index


async def _start_a_watch(run_root: Path, *, takeover: bool) -> str | None:
    """Start `w` once and stop the engine. `takeover` models an engine that dies after the
    decision record and before the dispatch record (DL-102's window). Returns the `run_id` the
    dispatch record bound, None when there is none."""
    engine = _genesis_engine(run_root, _WATCH_JIL, "FW")
    if takeover:
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
    engine.inject(_ev("STARTJOB", 0, job="w"))
    await engine.run_until_quiescent(T0 + timedelta(minutes=1))
    assert engine.oracle.store.job["w"].status == "RUNNING"
    await _close_engine(engine)
    bound = _dispatches(run_root)
    if takeover:
        assert bound == []
        return None
    [(_job, _run, run_id)] = bound
    return run_id


@pytest.mark.parametrize("takeover", [False, True], ids=["resume", "takeover"])
def test_a_watch_is_launched_once_at_resume(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, takeover: bool
) -> None:
    """The two untraced cases of a decided watch start. With a `dispatch` record and nothing on
    disk, the watch is re-dispatched under the recorded id, because polling is an idempotent
    re-read (DL-44 item 7). With no record (DL-102's window), resume dispatches the start, which
    writes the record first. Either way the restart launches the watch once, under the identity
    its record names (DL-118)."""

    async def scenario() -> None:
        run_root = short_root / "run"
        bound = await _start_a_watch(run_root, takeover=takeover)
        launches = _count_launches(monkeypatch)
        watch = _CountingWatch()
        resumed = await _resume(run_root, _WATCH_JIL, watch)
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
            assert resumed.oracle.store.job["w"].status == "RUNNING"
            assert resumed.live_jobs() == frozenset({"w"})
            assert launches == [("w", 1)]
            [(job, run_number, run_id)] = _dispatches(run_root)
            assert (job, run_number) == ("w", 1)
            assert bound is None or run_id == bound
            assert watch.calls == [("w", 1, run_id)]
        finally:
            await _close_engine(resumed)

    asyncio.run(scenario())


@pytest.mark.parametrize("job_type", ["FW", "CMD"])
def test_a_start_decided_and_never_dispatched_is_dispatched_at_resume(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, job_type: str
) -> None:
    """A start whose decision landed and whose `dispatch` record did not. The restart
    dispatches the start replay derives from the decision (DL-102): one `dispatch` record with
    a fresh uuid4 run_id, then one launch under it. Nothing was launched before the crash, so
    nothing is settled."""

    async def scenario() -> None:
        run_root = short_root / "run"
        jil, job = (_WATCH_JIL, "w") if job_type == "FW" else (_SOLO_JIL, "j")
        engine = _genesis_engine(run_root, jil, job_type)
        index = await _die_before_the_dispatch(engine, _ev("FORCE_STARTJOB", 1, job=job))
        launches = _count_launches(monkeypatch)
        adapter = _CountingWatch()
        resumed = await _resume(run_root, jil, adapter)
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
            row = resumed.oracle.store.job[job]
            assert (row.status, row.run_number) == ("RUNNING", 1)
            assert resumed.live_jobs() == frozenset({job})
            assert launches == [(job, 1)]
            [decision] = _decisions_at(run_root, index)
            assert decision["decision"] == "applied"
            [(_job, _run, run_id)] = _dispatches(run_root)
            assert adapter.calls == [(job, 1, run_id)]
        finally:
            await _close_engine(resumed)
        assert _reconciled(run_root) == []

    asyncio.run(scenario())


def test_a_start_dispatched_at_a_restart_is_dispatched_once(short_root: Path) -> None:
    """The dispatch is durable: a second restart finds the attempt open, writes no second
    decision and no second `dispatch` record, and relaunches the watch under the id the first
    restart bound (DL-118)."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _WATCH_JIL, "FW")
        index = await _die_before_the_dispatch(engine, _ev("FORCE_STARTJOB", 1, job="w"))
        first = await _resume(run_root, _WATCH_JIL, _CountingWatch())
        await first.run_until_quiescent(T0 + timedelta(minutes=4))  # the loop dispatches
        await _close_engine(first)
        [(_job, _run, run_id)] = _dispatches(run_root)

        watch = _CountingWatch()
        second = await _resume(run_root, _WATCH_JIL, watch, minutes=5)
        try:
            await second.run_until_quiescent(T0 + timedelta(minutes=6))
            assert watch.calls == [("w", 1, run_id)]
        finally:
            await _close_engine(second)
        assert len(_decisions_at(run_root, index)) == 1
        assert _dispatches(run_root) == [("w", 1, run_id)]

    asyncio.run(scenario())


def test_a_watch_whose_log_names_another_run_is_quarantined(short_root: Path) -> None:
    """A dispatched watch whose `watch.jsonl` names another run_id than the attempt the store
    bound: a store/spool identity split (DL-118). The restart never takes that watch up: it
    quarantines the attempt (spec-restart §7, DL-433), one `unobservable` outcome with cause
    `quarantined` naming the split, the job held, and the engine starts."""
    from store_surgery import rows

    from dsl41.runner_adapters import ARTIFACT_FORMAT_VERSION, append_watch_line

    async def scenario() -> None:
        run_root = short_root / "run"
        await _start_a_watch(run_root, takeover=False)
        log = run_root / "runs" / "w.1" / "watch.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.unlink(missing_ok=True)
        append_watch_line(
            log,
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "at": T0 + timedelta(minutes=2),
                "kind": "start",
                "run_id": None,
            },
        )
        watch = _CountingWatch()
        engine = await _resume(run_root, _WATCH_JIL, watch)
        await engine.run_until_quiescent(engine.clock.now())
        assert engine.started.is_set() and engine.oracle.store.job["w"].on_hold
        await _close_engine(engine)
        [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
        assert (outcome["outcome"], outcome["cause"]) == ("unobservable", "quarantined")
        assert "watch.jsonl reports run_id None" in outcome["status"]["note"]

    asyncio.run(scenario())


#: a box whose failure condition already holds: its start bypasses the noexec member `k`, the
#: fold then fails the box, and the job_terminator member `j` is TERMINATED in the batch that
#: started it, so its start is planned and superseded with no `dispatch` record (DL-242, DL-348)
_TERMINATOR_JIL = (
    "insert_job: x\njob_type: c\ncommand: x\n\n"
    "insert_job: b\njob_type: b\nbox_failure: s(x)\n\n"
    "insert_job: j\njob_type: c\ncommand: y\nbox_name: b\njob_terminator: y\n\n"
    "insert_job: k\njob_type: c\ncommand: y\nbox_name: b\n"
)


def test_a_starting_overwrite_of_a_superseded_start_is_left_live(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-315's case. Run 1 of `j` starts and is TERMINATED inside the input that decided it,
    so the live engine launches nothing and writes no `dispatch` record. An operator's
    `CHANGE_STATUS STARTING` at the same run number is decided and the engine dies. The restart
    launches nothing: the run number did not move (DL-232), the start replay derives was
    superseded, and nothing was dispatched. The row stays live with no FAILURE, for the
    operator to end or restart."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _TERMINATOR_JIL, "CMD")
        engine.inject(_ev("STARTJOB", 0, job="x"))
        engine.inject(_ev("STATUS", 1, job="x", status="SUCCESS"))
        engine.inject(_ev("ON_NOEXEC", 1.5, job="k"))
        engine.inject(_ev("STARTJOB", 2, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        row = engine.oracle.store.job["j"]
        assert (row.status, row.run_number) == ("TERMINATED", 1)
        assert [job for job, _run, _id in _dispatches(run_root)] == ["x"]
        index = await _die_before_the_dispatch(engine, _ev("STATUS", 4, job="j", status="STARTING"))
        launches = _count_launches(monkeypatch)
        resumed = await _resume(run_root, _TERMINATOR_JIL, _CountingWatch(), minutes=5)
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=6))
            row = resumed.oracle.store.job["j"]
            assert (row.status, row.run_number) == ("STARTING", 1)
            assert resumed.live_jobs() == frozenset()
            assert launches == []
        finally:
            await _close_engine(resumed)
        [decision] = _decisions_at(run_root, index)
        assert decision["decision"] == "applied"
        assert [job for job, _run, _id in _dispatches(run_root)] == ["x"]
        assert _reconciled(run_root) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("job_type", ["FW", "CMD"])
def test_a_start_no_decision_planned_is_left_as_it_is_at_a_restart(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, job_type: str
) -> None:
    """An operator set a job that never ran RUNNING. No decision planned a start for it, so the
    live engine launches nothing, and neither does a restart (DL-315): a watch is not launched
    with no identity, and a command is not failed as a lost dispatch."""

    async def scenario() -> None:
        run_root = short_root / "run"
        jil, job = (_WATCH_JIL, "w") if job_type == "FW" else (_SOLO_JIL, "j")
        engine = _genesis_engine(run_root, jil, job_type)
        engine.inject(_ev("STATUS", 0, job=job, status="RUNNING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close_engine(engine)
        assert _dispatches(run_root) == []

        launches = _count_launches(monkeypatch)
        resumed = await _resume(run_root, jil, _CountingWatch())
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
            row = resumed.oracle.store.job[job]
            assert (row.status, row.run_number) == ("RUNNING", 0)
            assert resumed.live_jobs() == frozenset()
            assert launches == []
        finally:
            await _close_engine(resumed)
        assert _reconciled(run_root) == []
        assert _dispatches(run_root) == []

    asyncio.run(scenario())


def test_a_watch_no_decision_planned_is_not_relaunched_from_its_directory(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator set a watch that never ran RUNNING, so no decision planned a start for its
    run, and a run directory holds an incomplete watch log with no identity. A restart reads the
    log but launches nothing: a watch is relaunched only under a recorded dispatch, never with
    no identity (DL-315)."""
    from dsl41.runner_adapters import ARTIFACT_FORMAT_VERSION, append_watch_line

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _WATCH_JIL, "FW")
        engine.inject(_ev("STATUS", 0, job="w", status="RUNNING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close_engine(engine)
        run_dir = run_root / "runs" / "w.0"
        run_dir.mkdir(parents=True)
        append_watch_line(
            run_dir / "watch.jsonl",
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "at": T0 + timedelta(minutes=1),
                "kind": "start",
                "run_id": None,
            },
        )

        launches = _count_launches(monkeypatch)
        watch = _CountingWatch()
        resumed = await _resume(run_root, _WATCH_JIL, watch)
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=5))
            row = resumed.oracle.store.job["w"]
            assert (row.status, row.run_number) == ("RUNNING", 0)
            assert resumed.live_jobs() == frozenset()
            assert launches == []
            assert watch.calls == []
        finally:
            await _close_engine(resumed)
        assert _reconciled(run_root) == []

    asyncio.run(scenario())


def test_the_dispatch_record_precedes_the_launch(short_root: Path) -> None:
    """Journal before effect (runner-design ss7): the `dispatch` record of a start, naming a
    uuid4 run_id, is written after the decision that planned it and before the adapter runs, and
    the adapter is handed that id (DL-118)."""

    async def scenario() -> None:
        run_root = short_root / "run"
        adapter = _CountingWatch()
        engine = _genesis_engine(run_root, _SOLO_JIL, "CMD")
        engine.adapters["CMD"] = adapter
        order: list[str] = []
        assert engine.journal is not None
        real_append = engine.journal.append

        def append(*records: Any) -> int:
            order.extend(record.kind for record in records)
            return real_append(*records)

        engine.journal.append = append  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close_engine(engine)
        assert order[:3] == ["input", "decision", "dispatch"]
        [(_job, _run, run_id)] = _dispatches(run_root)
        assert adapter.calls == [("j", 1, run_id)]
        assert json.loads(json.dumps(run_id)) == run_id

    asyncio.run(scenario())


def test_an_overdue_deadline_applies_before_a_recovered_start_dispatches(
    short_root: Path,
) -> None:
    """A start with a one-minute `term_run_time` is decided and the engine dies before its
    `dispatch` record. Resumed at two minutes, the overdue deadline fires first and ends the
    run, so the recovered start is superseded: no adapter call and no `dispatch` record. The
    loop dispatches owed starts only once nothing is due (`Engine.defer`)."""
    jil = "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 1\n"

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, jil, "CMD")
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))
        await _close_engine(engine)
        assert _dispatches(run_root) == []

        adapter = _CountingWatch()
        resumed = await _resume(run_root, jil, adapter, minutes=2)
        try:
            await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
            assert resumed.oracle.store.job["j"].status == "TERMINATED"
            assert adapter.calls == []
        finally:
            await _close_engine(resumed)
        assert _dispatches(run_root) == []

    asyncio.run(scenario())


def test_a_refused_resume_stops_the_tasks_it_started_before_it_releases_the_lock(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resume that fails after its fold has started no adapter task: the fold takes the watch
    up only as a catch-up step of the loop (spec-restart §2, Opus round 1, R5), so a refusal
    leaves no adapter behind when the journal detaches and the lock is released."""
    import dsl41.runner_startup as startup
    from dsl41.runner_ledger import LeaderLock
    from dsl41.runner_startup import acquire_run_root

    jil = _WATCH_JIL + "\n" + _SOLO_JIL
    run_root = short_root / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(jil),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FakeAdapter(default=None), "CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="w"))
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close_engine(engine)

    asyncio.run(first())
    assert sorted(job for job, _run, _id in _dispatches(run_root)) == ["j", "w"]

    tasks: list[asyncio.Task[None]] = []
    real_launch = Engine._launch

    def launch(self, job_ir, run_number, adapter, **kwargs):  # type: ignore[no-untyped-def]
        real_launch(self, job_ir, run_number, adapter, **kwargs)
        tasks.append(self._live[job_ir.name].task)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("an injected fault after the fold")

    order: list[str] = []
    real_release = LeaderLock.release

    def release(self: LeaderLock) -> None:
        order.append("released with " + ",".join(str(t.done()) for t in tasks))
        real_release(self)

    monkeypatch.setattr(Engine, "_launch", launch)
    monkeypatch.setattr(startup, "_first_reading", fail)  # after the fold
    monkeypatch.setattr(LeaderLock, "release", release)

    async def second() -> None:
        with pytest.raises(RuntimeError, match="after the fold"):
            await resume_run(
                lower_source(jil),
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=2)),
                adapters={"FW": _CountingWatch(), "CMD": FakeAdapter(default=None)},
            )

    asyncio.run(second())
    assert tasks == []  # the watch's step never ran
    assert order[0] == "released with "
    acquire_run_root(run_root).release()


def test_a_recovered_start_dispatches_in_the_call_that_holds_a_deadline_at_now(
    short_root: Path,
) -> None:
    """Virtual clock: the run's own `term_run_time` deadline falls exactly at the resume
    instant, and in a call whose horizon is that instant it stays lazy (the frontier rule). The
    recovered start must not wait behind it: the loop dispatches queued starts once nothing it
    can process now is left, in the same call. The deadline fires in a later call."""
    jil = "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 1\n"

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, jil, "CMD")
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close_engine(engine)
        assert _dispatches(run_root) == []

        adapter = _CountingWatch()
        resumed = await _resume(run_root, jil, adapter, minutes=2)
        try:
            assert resumed.oracle.next_timer_due() == T0 + timedelta(minutes=2)
            await resumed.run_until_quiescent(T0 + timedelta(minutes=2))
            assert resumed.oracle.store.job["j"].status == "RUNNING"  # the deadline is lazy
            assert [call[:2] for call in adapter.calls] == [("j", 1)]
            await resumed.run_until_quiescent(T0 + timedelta(minutes=3))
            assert resumed.oracle.store.job["j"].status == "TERMINATED"
        finally:
            await _close_engine(resumed)
        assert [job for job, _run, _id in _dispatches(run_root)] == ["j"]

    asyncio.run(scenario())
