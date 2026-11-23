"""Only the engine's own start plans a launch (DL-550, BUG-107).

An emitted STARTING plans a start only when the batch's trace holds the job's numbered start
entry for its current run (DL-537): the `->STARTING` that `_w_starting` writes. An operator's
`CHANGE_STATUS ... STARTING` writes no number, so it launches nothing, also where the run number
moved without a dispatch: an ON_NOEXEC bypass. Every real start still launches once, live and
in a replay from genesis. Each scenario runs an engine on a virtual clock over a run root; no
sleep, no random data.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from store_surgery import rows
from transitions_check import (  # noqa: F401 -- autouse
    rows_equal_their_replay,
    rows_equal_their_replay_once,
)

from dsl41 import runner_admission
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import STORE_NAME, read_journal, replay_store
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import SqliteStore

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO = "insert_job: j\njob_type: c\ncommand: x\n"

#: `x` ends by its runtime limit at T0 + 1 minute, and `j` starts on `t(x)` (DL-232's batch: a
#: timer due at the input's instant fires inside that input)
_DEADLINE = (
    "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: y\ncondition: t(x)\n"
)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ev(minutes: float, kind: str, **payload: object) -> Event:
    return Event(at=_at(minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


class _Counting(FakeAdapter):
    """A FakeAdapter that records every run it is asked to carry out."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, int]] = []

    async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
        self.calls.append((job_ir.name, run_number))
        return await super().run(job_ir, run_number, ctx)


def _dispatched(run: Path) -> list[tuple[str, int]]:
    return [(r["job"], r["run_number"]) for r in read_journal(run) if r["rec"] == "dispatch"]


def _kinds(run: Path, kind: str) -> list[dict[str, Any]]:
    """The stored journal records of `kind`, as the store holds them."""
    return [body for _, found, body in rows(run) if found == kind]


def _genesis_spawns(run: Path) -> set[tuple[str, int]]:
    """The starts a replay of the root from genesis derives (`Replay.spawns`)."""
    with closing(SqliteStore(run / STORE_NAME, read_only=True)) as store:
        return replay_store(store, where="t").replay.spawns


def _start(run: Path, text: str, adapter: FakeAdapter, job_type: str = "CMD") -> Engine:
    return start_run(
        lower_source(text), run, clock=VirtualClock(start=T0), adapters={job_type: adapter}
    )


async def _resume(run: Path, text: str, adapter: FakeAdapter, minutes: float) -> Engine:
    return await resume_run(
        lower_source(text), run, clock=VirtualClock(start=_at(minutes)), adapters={"CMD": adapter}
    )


async def _feed(engine: Engine, *events: Event) -> None:
    for event in events:
        engine.inject(event)
        await engine.run_until_quiescent(event.at)


async def _close(engine: Engine) -> None:
    journal = engine.journal
    assert journal is not None
    try:
        await engine.shutdown()
    finally:
        journal.close()


def _run(run: Path, text: str, adapter: FakeAdapter, body: Any, job_type: str = "CMD") -> Any:
    """Run `body(engine)` on a fresh engine over `run`; the engine and its store are closed on
    every path."""

    async def scenario() -> Any:
        engine = _start(run, text, adapter, job_type)
        try:
            return await body(engine)
        finally:
            await _close(engine)

    return asyncio.run(scenario())


class _Every(Collection[tuple[str, int]]):
    """Every `(job, run)`: the build before DL-550, which planned a start for any emitted
    STARTING past the ghost-run gate."""

    def __contains__(self, item: object) -> bool:
        return True

    def __iter__(self) -> Any:
        return iter(())

    def __len__(self) -> int:
        return 0


# ------------------------------------------------------- an injected STARTING


@pytest.mark.parametrize("noexec", [True, False], ids=["bypass", "start"])
def test_a_bypass_and_an_injected_starting_in_one_batch(tmp_path: Path, noexec: bool) -> None:
    """`j` waits on `t(x)`, `x` ends by its runtime limit, and a `STATUS STARTING` on `j` is
    injected at that deadline: the timer and the injection are one input. Under ON_NOEXEC `j.1`
    bypasses and the injection re-emits STARTING at run 1: nothing of `j` is dispatched. Without
    it `j.1` is a real start and launches once."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)

    async def body(engine: Engine) -> list[tuple[str, str, int | None]]:
        if noexec:
            await _feed(engine, _ev(0, "ON_NOEXEC", job="j"))
        await _feed(
            engine, _ev(0, "STARTJOB", job="x"), _ev(1, "STATUS", job="j", status="STARTING")
        )
        return [
            (e.job, e.transition, e.run_number) for e in engine.oracle.trace() if e.at == _at(1)
        ]

    batch = _run(run, _DEADLINE, adapter, body)
    ended = ("x", "RUNNING->TERMINATED", None)
    if noexec:
        assert batch == [ended, ("j", "INACTIVE->SUCCESS", 1), ("j", "SUCCESS->STARTING", None)]
    else:
        assert batch == [
            ended,
            ("j", "INACTIVE->STARTING", 1),
            ("j", "STARTING->RUNNING", None),
            ("j", "RUNNING->STARTING", None),
        ]
    inputs = [r for r in read_journal(run) if r["rec"] == "input"]
    assert len(inputs) == 2 + noexec  # the deadline fired inside the injection's input
    want = [("x", 1)] if noexec else [("x", 1), ("j", 1)]
    assert _dispatched(run) == want and adapter.calls == want
    assert _genesis_spawns(run) == set(want)


def test_a_bypass_an_injection_and_two_restarts_launch_nothing(tmp_path: Path) -> None:
    """A bypass, a restart, an injected `STATUS STARTING`, another restart. The second
    restart replays from genesis, where the injection's run is past the gate: it derives no
    start and owes none, so nothing launches at either restart."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)

    async def scenario() -> None:
        engine = _start(run, _SOLO, adapter)
        try:
            await _feed(engine, _ev(0, "ON_NOEXEC", job="j"), _ev(1, "STARTJOB", job="j"))
        finally:
            await _close(engine)
        second = await _resume(run, _SOLO, adapter, 4)
        try:
            await _feed(second, _ev(5, "STATUS", job="j", status="STARTING"))
            assert second.live_jobs() == frozenset()
        finally:
            await _close(second)
        third = await _resume(run, _SOLO, adapter, 8)
        try:
            await third.run_until_quiescent(_at(9))
            row = third.oracle.store.job["j"]
            assert (row.status, row.run_number) == ("STARTING", 1)
            assert third.live_jobs() == frozenset()
        finally:
            await _close(third)

    asyncio.run(scenario())
    assert _dispatched(run) == [] and adapter.calls == []
    assert _kinds(run, "dispatch_pending") == []
    assert _genesis_spawns(run) == set()


def test_an_earlier_build_s_dispatch_of_an_injected_starting_is_settled_as_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root written by the build before DL-550: after a bypass, the injected `STATUS STARTING`
    got a `dispatch` record for run 1. This build resumes it: its replay derives no start and
    owes none. The recorded attempt is reconciled as any open attempt: with no spool it closes
    `unobservable`, and its plan tries run 1 again as attempt 2 (spec-wiring 2b)."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)

    async def body(engine: Engine) -> None:
        await _feed(engine, _ev(0, "ON_NOEXEC", job="j"), _ev(1, "STARTJOB", job="j"))
        await _feed(engine, _ev(2, "STATUS", job="j", status="STARTING"))

    with monkeypatch.context() as patch:
        patch.setattr(runner_admission, "run_starts", lambda _entries: _Every())
        _run(run, _SOLO, adapter, body)
    [record] = [r for r in read_journal(run) if r["rec"] == "dispatch"]
    assert (record["job"], record["run_number"], adapter.calls) == ("j", 1, [("j", 1)])
    assert _genesis_spawns(run) == set()

    async def restart() -> tuple[str, int]:
        engine = await _resume(run, _SOLO, adapter, 4)
        try:
            await engine.run_until_quiescent(_at(5))
            assert engine.started.is_set()
            row = engine.oracle.store.job["j"]
            return row.status, row.run_number
        finally:
            await _close(engine)

    assert asyncio.run(restart()) == ("STARTING", 1)
    # the recorded attempt has no spool to settle from, so it ends unobservable and its plan
    # runs run 1 again as its second attempt (spec-wiring 2b); nothing is owed as a new start
    [outcome] = _kinds(run, "outcome")
    assert (outcome["attempt"], outcome["outcome"]) == (record["run_id"], "unobservable")
    first, rerun = _kinds(run, "dispatch")
    assert (first["attempt"], first["ordinal"]) == (record["run_id"], 1)
    assert (rerun["run_number"], rerun["ordinal"], rerun["after"]) == (1, 2, outcome["cause"])
    assert _kinds(run, "dispatch_pending") == []
    assert adapter.calls == [("j", 1), ("j", 1)]


def test_an_earlier_build_s_pending_dispatch_of_an_injected_starting_launches_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the build before DL-550: a bypass, a restart, an injected `STATUS STARTING`, and a
    restart whose genesis replay derived run 1 as owed; that restart died after its
    `dispatch_pending` committed and before the `dispatch`. This build's replay derives no
    start, and its recovery still launches the recorded pending run once: a pending dispatch an
    earlier build recorded is carried out as recorded."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)

    async def earlier_build() -> None:
        engine = _start(run, _SOLO, adapter)
        try:
            await _feed(engine, _ev(0, "ON_NOEXEC", job="j"), _ev(1, "STARTJOB", job="j"))
        finally:
            await _close(engine)
        second = await _resume(run, _SOLO, adapter, 4)
        try:
            await _feed(second, _ev(5, "STATUS", job="j", status="STARTING"))
        finally:
            await _close(second)
        third = await _resume(run, _SOLO, adapter, 8)
        assert third.journal is not None
        real = third.journal.append

        def die_before_the_dispatch(*records: Any, **rows: Any) -> int:
            if any(record.kind == "dispatch" for record in records):
                raise RuntimeError("the engine died before the dispatch record")
            return real(*records, **rows)

        third.journal.append = die_before_the_dispatch  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="before the dispatch record"):
                await third.run_until_quiescent(_at(9))
        finally:
            third.journal.append = real  # type: ignore[method-assign]
            await _close(third)

    with monkeypatch.context() as patch:
        patch.setattr(runner_admission, "run_starts", lambda _entries: _Every())
        asyncio.run(earlier_build())
    [pending] = _kinds(run, "dispatch_pending")
    assert (pending["job"], pending["run_number"], pending["event"]) == ("j", 1, "pending")
    assert _dispatched(run) == [] and adapter.calls == []
    assert _genesis_spawns(run) == set()

    async def restart() -> None:
        engine = await _resume(run, _SOLO, adapter, 12)
        try:
            await engine.run_until_quiescent(_at(13))
            assert engine.live_jobs() == frozenset({"j"})
        finally:
            await _close(engine)

    asyncio.run(restart())
    [dispatch] = _kinds(run, "dispatch")
    assert (dispatch["job"], dispatch["run_number"]) == ("j", 1)
    assert dispatch["discharges"] is not None
    assert adapter.calls == [("j", 1)]


# ------------------------------------------------------------- real starts


_LOAD = (
    "insert_machine: lm\ntype: a\nnode_name: lm\nmax_load: 1\n\n"
    "insert_job: a\njob_type: c\ncommand: x\nmachine: lm\njob_load: 1\npriority: 1\n\n"
    "insert_job: b\njob_type: c\ncommand: y\nmachine: lm\njob_load: 1\npriority: 1\n"
)
_BOX = "insert_job: bx\njob_type: b\n\ninsert_job: m\njob_type: c\ncommand: x\nbox_name: bx\n"
_WATCH = "insert_job: w\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n"

#: case: the estate, its adapter's job type, the script and the starts it launches
_REAL: dict[str, tuple[str, str, list[str], list[tuple[str, int]]]] = {
    "admission": (_SOLO, "CMD", ["j"], [("j", 1)]),
    "readmitted": (_LOAD, "CMD", ["a", "b"], [("a", 1), ("b", 1)]),
    "member": (_BOX, "CMD", ["bx"], [("m", 1)]),
    "watch": (_WATCH, "FW", ["w"], [("w", 1)]),
}


@pytest.mark.parametrize("case", list(_REAL))
def test_a_real_start_launches_once(tmp_path: Path, case: str) -> None:
    """An ordinary admission, a waiter readmitted from QUE_WAIT when `a`'s exit frees the load,
    a box member and a file watch each launch once, live and in a replay from genesis. An
    injected `STATUS STARTING` on the started run afterwards launches nothing more."""
    text, job_type, jobs, want = _REAL[case]
    run = tmp_path / "run"
    adapter = _Counting({("a", 1): (60.0, 0)}, default=None)

    async def body(engine: Engine) -> None:
        await _feed(engine, *(_ev(0, "STARTJOB", job=job) for job in jobs))
        if case == "readmitted":
            assert engine.oracle.store.job["b"].status == "QUE_WAIT"
        await engine.run_until_quiescent(_at(2))
        job, run_number = want[-1]
        row = engine.oracle.store.job[job]
        assert (row.status, row.run_number) == ("RUNNING", run_number)
        await _feed(engine, _ev(3, "STATUS", job=job, status="STARTING"))

    _run(run, text, adapter, body, job_type)
    assert _dispatched(run) == want and adapter.calls == want
    assert _genesis_spawns(run) == set(want)


def test_a_start_held_behind_its_job_s_group_launches_once_when_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start decided while its job's group is held (`blocked`, DL-426) is durable as
    `dispatch_pending` and launches once, under that record, when the group is gone."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)
    held = {"j"}
    real = Engine.blocked

    def blocked(engine: Engine, job: str, replacing: str | None = None) -> str | None:
        return "held" if job in held else real(engine, job, replacing)

    monkeypatch.setattr(Engine, "blocked", blocked)

    async def body(engine: Engine) -> None:
        await _feed(engine, _ev(0, "STARTJOB", job="j"))
        assert _dispatched(run) == [] and [item.job for item in engine._held_starts] == ["j"]
        held.clear()
        engine._release("j")
        await _feed(engine, _ev(1, "STATUS", job="j", status="STARTING"))

    _run(run, _SOLO, adapter, body)
    assert _dispatched(run) == [("j", 1)] and adapter.calls == [("j", 1)]
    [pending] = _kinds(run, "dispatch_pending")
    assert (pending["job"], pending["run_number"]) == ("j", 1)
    assert _genesis_spawns(run) == {("j", 1)}


def test_a_start_planned_while_draining_launches_once_at_the_restart(tmp_path: Path) -> None:
    """An engine-made start decided while the engine drains waits as `dispatch_pending`, and
    the restart launches it once (spec-drain)."""
    run = tmp_path / "run"
    adapter = _Counting(default=None)

    async def scenario() -> None:
        engine = _start(run, _SOLO, adapter)
        try:
            engine.request_stop()
            engine.inject(_ev(0, "STARTJOB", job="j"), source=None)  # the engine's own
            await engine.run_until_quiescent(_at(1))
            assert engine.draining and _dispatched(run) == []
        finally:
            await _close(engine)
        restarted = await _resume(run, _SOLO, adapter, 2)
        try:
            await restarted.run_until_quiescent(_at(3))
        finally:
            await _close(restarted)

    asyncio.run(scenario())
    assert _dispatched(run) == [("j", 1)] and adapter.calls == [("j", 1)]
    assert _genesis_spawns(run) == {("j", 1)}


def test_a_recovered_owed_start_launches_once(tmp_path: Path) -> None:
    """`a` exits and the engine dies before its completion lands. The restart feeds it, which
    starts `b`: an owed start, durable as `dispatch_pending` and launched once the recovery's
    due work has run (DL-425)."""
    text = (
        "insert_job: a\njob_type: c\ncommand: x\n\n"
        "insert_job: b\njob_type: c\ncommand: y\ncondition: s(a)\n"
    )
    run = tmp_path / "run"

    async def first() -> None:
        engine = _start(run, text, FakeAdapter({("a", 1): (5.0, 0)}, default=None))
        assert engine.journal is not None
        real = engine.journal.append

        def die_at_the_feed(*records: Any, **rows: Any) -> int:
            if any(r.kind == "follow_up" and r.payload["event"] == "done" for r in records):
                raise RuntimeError("the engine died before the completion landed")
            return real(*records, **rows)

        engine.journal.append = die_at_the_feed  # type: ignore[method-assign]
        try:
            engine.inject(_ev(0, "STARTJOB", job="a"))
            with pytest.raises(RuntimeError, match="before the completion landed"):
                await engine.run_until_quiescent(_at(1))
        finally:
            engine.journal.append = real  # type: ignore[method-assign]
            await _close(engine)

    asyncio.run(first())
    adapter = _Counting(default=None)

    async def restart() -> None:
        engine = await _resume(run, text, adapter, 10)
        try:
            await engine.run_until_quiescent(_at(11))
            assert engine.oracle.store.job["a"].status == "SUCCESS"
            assert engine.oracle.store.job["b"].status == "RUNNING"
        finally:
            await _close(engine)

    asyncio.run(restart())
    assert _dispatched(run) == [("a", 1), ("b", 1)] and adapter.calls == [("b", 1)]
    pending = _kinds(run, "dispatch_pending")
    assert [(r["job"], r["event"]) for r in pending] == [("b", "pending")]
    assert _genesis_spawns(run) == {("a", 1), ("b", 1)}
