"""Resume's decision for a live start that left no run directory (DL-102, DL-315).

A start's `dispatch` record, naming its `run_id`, is durable before its adapter task exists, and
its run directory is made after (runner-design ss7). So resume reads three cases off the log:

  * a `dispatch` record and no directory: the start was launched and the crash took it. A
    tethered command is FAILURE, a watch is relaunched under the recorded `run_id`;
  * no record, and replay derives the start: it was decided and never launched, and resume
    dispatches it now;
  * neither: the live engine launched nothing for the run, and resume launches nothing.

These tests moved here from the routing-table suite, which held the same restarts behind a
drained host.
"""

from __future__ import annotations

import asyncio
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
from dsl41.runner_journal import Replay, read_journal
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

    def counted(self, job_ir, run_number, adapter, *, run_id=None):  # type: ignore[no-untyped-def]
        launches.append((job_ir.name, run_number))
        real(self, job_ir, run_number, adapter, run_id=run_id)

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
        settle_seconds=0.0,
        grace_seconds=0.0,
    )


async def _close_engine(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _records(run_root: Path) -> list[dict[str, Any]]:
    return read_journal(run_root / "journal.jsonl")


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


async def _die_before_the_decision(engine: Engine, ev: Event) -> int:
    """Admit `ev` and kill the engine after its attempt record and before its decision record
    (DL-156's window). Returns the attempt's index."""

    def die(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the engine died before the decision record")

    assert engine.journal is not None
    path = engine.journal.path
    engine.journal.decision = die  # type: ignore[method-assign]
    engine.inject(ev)
    with pytest.raises(RuntimeError, match="before the decision record"):
        await engine.run_until_quiescent(ev.at + timedelta(minutes=1))
    await _close_engine(engine)
    records = read_journal(path)
    index = int(records[-1]["seq"])
    assert records[-1]["rec"] == "input"
    assert not [r for r in records if r["rec"] == "decision" and r["index"] == index]
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
def test_an_undecided_start_is_committed_and_dispatched_at_resume(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, job_type: str
) -> None:
    """A start whose decision never landed. Resume re-decides it through the gate (DL-156),
    writes the decision, and dispatches the start replay derives from it (DL-315): one
    `dispatch` record with a fresh uuid4 run_id, then one launch under it. A command is not
    failed as a lost dispatch, because nothing was launched before the crash."""

    async def scenario() -> None:
        run_root = short_root / "run"
        jil, job = (_WATCH_JIL, "w") if job_type == "FW" else (_SOLO_JIL, "j")
        engine = _genesis_engine(run_root, jil, job_type)
        index = await _die_before_the_decision(engine, _ev("FORCE_STARTJOB", 1, job=job))
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


def test_a_committed_start_is_committed_once(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit is durable: a second restart finds the attempt decided, recovers nothing,
    writes no second decision and no second `dispatch` record, and relaunches the watch under
    the id the first restart bound (DL-315)."""
    import dsl41.runner_startup as startup

    replays: list[Replay] = []
    real = startup.replay_inputs

    def recording(*args: Any, **kwargs: Any) -> Replay:
        replay = real(*args, **kwargs)
        replays.append(replay)
        return replay

    monkeypatch.setattr(startup, "replay_inputs", recording)

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _WATCH_JIL, "FW")
        index = await _die_before_the_decision(engine, _ev("FORCE_STARTJOB", 1, job="w"))
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
        assert [len(r.recovered) for r in replays] == [1, 0]

    asyncio.run(scenario())


def test_a_refused_resume_writes_nothing(short_root: Path) -> None:
    """A run root an earlier build resumed: a watch start lost its decision, and that build
    launched the watch with no identity, so `watch.jsonl` starts with `run_id: null`. This
    build's resume would dispatch the start under a fresh id, so it refuses the root by name as a
    WAL/spool identity split (DL-118). It checks before it writes the recovered decision, so the
    refused resume appends nothing and the build that wrote the log can still open it (DL-315)."""
    from dsl41.run_root import active_wal
    from dsl41.runner_adapters import ARTIFACT_FORMAT_VERSION, append_watch_line
    from dsl41.runner_clock import EngineError

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _WATCH_JIL, "FW")
        await _die_before_the_decision(engine, _ev("FORCE_STARTJOB", 1, job="w"))
        run_dir = run_root / "runs" / "w.1"
        run_dir.mkdir(parents=True)
        append_watch_line(
            run_dir / "watch.jsonl",
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "at": T0 + timedelta(minutes=2),
                "kind": "start",
                "run_id": None,
            },
        )
        wal = active_wal(run_root)
        before = wal.read_bytes()
        with pytest.raises(EngineError, match="watch.jsonl reports run_id None"):
            await _resume(run_root, _WATCH_JIL, _CountingWatch())
        assert wal.read_bytes() == before

    asyncio.run(scenario())


def test_a_recovered_start_followed_by_decided_ones_dispatches_nothing_stale(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log an earlier build resumed without writing the lost decision: the undecided start of
    run 1 is followed by a decided start of run 2, dispatched under its own id. This build writes
    the lost decision once and dispatches nothing for run 1, whose row has moved on; run 2 keeps
    its one record and id (DL-315)."""

    async def scenario() -> None:
        run_root = short_root / "run"
        engine = _genesis_engine(run_root, _SOLO_JIL, "CMD")
        index = await _die_before_the_decision(engine, _ev("FORCE_STARTJOB", 1, job="j"))
        with monkeypatch.context() as earlier_build:
            earlier_build.setattr(Engine, "commit_recovered", lambda *_args: None)
            old = await _resume(run_root, _SOLO_JIL, FakeAdapter(default=None))
            try:
                await old.run_until_quiescent(T0 + timedelta(minutes=4))
                old.inject(_ev("KILLJOB", 5, job="j"))
                old.inject(_ev("FORCE_STARTJOB", 5.5, job="j"))
                await old.run_until_quiescent(T0 + timedelta(minutes=6))
                assert old.oracle.store.job["j"].run_number == 2
            finally:
                await _close_engine(old)
        assert _decisions_at(run_root, index) == []
        before = _dispatches(run_root)
        assert [(job, run) for job, run, _ in before] == [("j", 1), ("j", 2)]

        resumed = await _resume(run_root, _SOLO_JIL, FakeAdapter(default=None), minutes=7)
        try:
            assert resumed.oracle.store.job["j"].run_number == 2
        finally:
            await _close_engine(resumed)
        assert len(_decisions_at(run_root, index)) == 1
        assert _dispatches(run_root) == before

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


def test_a_recovered_starting_overwrite_of_a_superseded_start_is_left_live(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-315's case. Run 1 of `j` starts and is TERMINATED inside the input that decided it,
    so the live engine launches nothing and writes no `dispatch` record. An operator's
    `CHANGE_STATUS STARTING` at the same run number then loses its decision. Resume writes that
    decision and launches nothing: the run number did not move (DL-232), the start replay
    derives was superseded, and nothing was dispatched. The row stays live with no FAILURE, for
    the operator to end or restart."""

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
        index = await _die_before_the_decision(engine, _ev("STATUS", 4, job="j", status="STARTING"))
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
        real_write = engine.journal._write

        def write(record: dict[str, Any]) -> None:
            order.append(str(record["rec"]))
            real_write(record)

        engine.journal._write = write  # type: ignore[method-assign]
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
    loop dispatches recovered starts only once nothing is due (`Engine.recover_start`)."""
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
    """A resume that fails after it relaunched a watch cancels and collects that task before
    the journal detaches and the lock is released, so no adapter outlives the refusal."""
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

    def launch(self, job_ir, run_number, adapter, *, run_id=None):  # type: ignore[no-untyped-def]
        real_launch(self, job_ir, run_number, adapter, run_id=run_id)
        tasks.append(self._live[job_ir.name].task)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("an injected fault after the watch relaunched")

    order: list[str] = []
    real_release = LeaderLock.release

    def release(self: LeaderLock) -> None:
        order.append("released with " + ",".join(str(t.done()) for t in tasks))
        real_release(self)

    monkeypatch.setattr(Engine, "_launch", launch)
    monkeypatch.setattr(startup, "_inject_completion", fail)
    monkeypatch.setattr(LeaderLock, "release", release)

    async def second() -> None:
        with pytest.raises(RuntimeError, match="after the watch relaunched"):
            await resume_run(
                lower_source(jil),
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=2)),
                adapters={"FW": _CountingWatch(), "CMD": FakeAdapter(default=None)},
                settle_seconds=0.0,
                grace_seconds=0.0,
            )

    asyncio.run(second())
    assert len(tasks) == 1 and tasks[0].cancelled()
    assert order[0] == "released with True"
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
