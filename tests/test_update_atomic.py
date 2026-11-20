"""An update is atomic across the jobs it names (DL-522), and its notices take effect in the
writer's turn order (DL-523, DL-432): the order of notices against a competing request, the
order of an exit record and the end of its stop sequence, a restart while the update waits,
and the processes of the named jobs against `config applied`.

The virtual-clock scenarios use `test_engine_stops_cookie`'s process stand-ins and park each
stop ladder on an event of its own, so the test decides when each stop sequence ends. The
process scenarios run real wrappers on the real clock and read the recorded order.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate
from store_surgery import append, rows
from test_engine_stops_cookie import OTHER_ID, PGID, RUN_ID, SPAWN, T0, _Procs
from test_update_settlement import (
    SIGNALED,
    _adopt,
    _close,
    _crashed_update,
    _engine,
    _estate,
    _jil,
    _kinds,
    _plan,
    _request,
)
from test_update_wiring import (
    TIMEOUT_S,
    Estate,
    _applied,
    _finish,
    _running,
    _start,
    _until,
    _update,
)
from test_update_wiring import _jil as wiring_jil
from test_update_wiring import _plan as wiring_plan

from dsl41 import runner_procid, runner_stops
from dsl41.oracle_state import Event
from dsl41.reload_update import EXIT_RECORDED, Admitted, UpdateTier
from dsl41.runner import Engine, outcome_record, plan_record
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_startup import resume_run
from dsl41.runner_store import Record
from dsl41.runner_update import EngineUpdate, UpdateRequest


class _Ladders:
    """Every stop ladder waits on its group's own event; nothing outlives a ladder. A test
    releases one group's ladder and awaits it, so it decides the order the stop sequences
    end in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: list[int] = []
        self.opened = False
        #: the groups a reading finds alive until their ladder is released (a restart's
        #: cleanup reads them before it runs the ladder)
        self.alive: set[int] = set()
        self.started: dict[int, asyncio.Event] = {}

        async def ladder(_self: Any, pgid: int, _token: str, _grace: float) -> None:
            self.entered.append(pgid)
            self.started.setdefault(pgid, asyncio.Event()).set()
            await self.gate(pgid).wait()

        async def outlives(_self: Any, _pgid: int, _token: str, _grace: float) -> bool:
            return False

        def alive(_self: Any, pgid: int, _token: str) -> bool:
            return pgid in self.alive and not self.gate(pgid).is_set()

        monkeypatch.setattr(runner_stops.EngineStops, "ladder", ladder)
        monkeypatch.setattr(runner_stops.EngineStops, "outlives", outlives)
        monkeypatch.setattr(runner_stops.EngineStops, "command_alive", alive)

    def open_all(self) -> None:
        """Let every ladder end, a later one too, so a failed scenario still shuts down."""
        self.opened = True
        for gate in self.gates.values():
            gate.set()

    async def running(self, pgid: int) -> None:
        """Wait until the group's ladder has started."""
        await self.started.setdefault(pgid, asyncio.Event()).wait()

    def gate(self, pgid: int) -> asyncio.Event:
        gate = self.gates.setdefault(pgid, asyncio.Event())
        if self.opened:
            gate.set()
        return gate

    async def end(self, engine: Engine, attempt: str, pgid: int) -> None:
        """End one stop sequence: its done callback runs before this returns."""
        task = engine.attempts.ladder_task(attempt)
        assert task is not None and not task.done()
        self.gate(pgid).set()
        await task


def _dispatched(engine: Engine, job: str, attempt: str, pgid: int) -> None:
    """An observed attempt of `job`'s run 1 that this engine dispatched (`_adopt`), not one a
    restart found: the loop does not wait for its ladder at quiescence."""
    _adopt(engine, job, attempt, pgid)
    engine._restored.discard(attempt)


def _downs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each member the update takes out of its open set (its `exit_recorded` event)."""
    downs: list[str] = []
    send = UpdateTier._send

    def counted(self: UpdateTier, name: str, payload: object = None) -> None:
        if name == EXIT_RECORDED:
            assert isinstance(payload, str)
            downs.append(payload)
        send(self, name, payload)

    monkeypatch.setattr(UpdateTier, "_send", counted)
    return downs


def _applications(tmp_path: Path) -> list[dict[str, Any]]:
    """The `config applied` records of an update (the start's has no token)."""
    return [c for c in _kinds(tmp_path, "config") if c["event"] == "applied" and "token" in c]


def _queued(engine: Engine) -> list[tuple[str, object]]:
    """The writer's queue in the order it takes it: each step's kind and its job."""
    return [
        ("update_exit", p.update)
        if p.control == "update_exit"
        else (str(p.control or (p.ev and p.ev.kind)), p.ev and p.ev.payload.get("job"))
        for _, _, p in sorted(engine._queue, key=lambda item: item[:2])
    ]


def _order(tmp_path: Path) -> list[tuple[str, Any, Any, Any, Any]]:
    """The journal as (kind, event, job, ordinal, rev)."""
    return [
        (k, b.get("event"), b.get("job"), b.get("ordinal"), b.get("rev"))
        for _, k, b in rows(tmp_path / "run")
    ]


def _applied_index(order: list[tuple[str, Any, Any, Any, Any]]) -> int:
    """The index of the update's `config applied`, the second one (the first is the start's)."""
    found = [i for i, o in enumerate(order) if o[:2] == ("config", "applied")]
    assert len(found) == 2, found
    return found[1]


# ---------------------------------------------------------------- the order of notices


@pytest.mark.parametrize("last", ["k", "x"], ids=["start-between-notices", "start-after-notices"])
def test_a_start_queued_before_the_last_notice_runs_under_the_old_revision_and_after_it_the_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, last: str
) -> None:
    """DL-523, DL-432: each notice consumes only its member, and the update's check after it
    decides whether publication follows. With `j`'s notice, a start of `x` (a job the update does
    not name) and `k`'s notice queued in that order, `x` is dispatched once, under revision 1,
    before `config applied`. With both notices ahead of the start, `config applied` comes first
    and `x` is dispatched once under revision 2."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    other = PGID + 1

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "k": "sleep 30", "x": "true"}))
        try:
            for job in ("j", "k"):
                engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "j", RUN_ID, PGID)
            _dispatched(engine, "k", OTHER_ID, other)
            _estate(tmp_path, _jil({"j": "sleep 31", "k": "sleep 31", "x": "true"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            assert answer.decision.stop_set == ("j", "k")
            for attempt in (RUN_ID, OTHER_ID):
                engine.attempts.status_recorded(attempt, SIGNALED)
            assert engine.update.stopping and engine.update.view() == {
                "token": answer.decision.token,
                "stopping": ["j", "k"],
            }
            start = Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "x"})
            await ladders.end(engine, RUN_ID, PGID)  # j's notice
            if last == "k":
                engine.inject(start)
                await ladders.end(engine, OTHER_ID, other)  # k's notice
            else:
                await ladders.end(engine, OTHER_ID, other)
                engine.inject(start)
            assert _queued(engine) == (
                [("update_exit", "j"), ("FORCE_STARTJOB", "x"), ("update_exit", "k")]
                if last == "k"
                else [("update_exit", "j"), ("update_exit", "k"), ("FORCE_STARTJOB", "x")]
            )
            await engine.run_until_quiescent(T0)
            assert not engine.update.stopping and engine.config_rev == 2
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    order = _order(tmp_path)
    applied = _applied_index(order)
    dispatches = [(i, o[4]) for i, o in enumerate(order) if o[0] == "dispatch" and o[2] == "x"]
    assert len(dispatches) == 1
    [(at, rev)] = dispatches
    if last == "k":
        assert at < applied and rev == 1
    else:
        assert at > applied and rev == 2


# ---------------------------------------------------------------- one member's exit and cleanup


async def _stopping(engine: Engine, tmp_path: Path, jobs: tuple[str, ...] = ("j",)) -> Admitted:
    """Start `jobs` (each an observed attempt, `j` as RUN_ID on PGID, `k` as OTHER_ID on
    PGID + 1), admit an update that changes them, and answer the admission."""
    names = {"j": (RUN_ID, PGID), "k": (OTHER_ID, PGID + 1)}
    for job in jobs:
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
    await engine.run_until_quiescent(T0)
    for job in jobs:
        _dispatched(engine, job, *names[job])
    _estate(tmp_path, _jil({job: "sleep 31" for job in jobs}))
    answer = await _request(engine, _plan(engine, tmp_path))
    assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == jobs
    return answer.decision


def test_a_member_whose_record_comes_first_stays_open_until_its_stop_sequence_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-431, DL-522: a stop is finished when its stop sequence has ended, not at its record.
    The exit record lands while the ladder runs: the member stays in the open set and nothing is
    queued for it; the ladder's end queues one notice, and the member is taken down once."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            await _stopping(engine, tmp_path)
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            await engine.run_until_quiescent(T0)
            assert engine.update.tier is not None
            seen.append((set(engine.update.tier.open), _queued(engine), list(downs)))
            await ladders.end(engine, RUN_ID, PGID)
            seen.append((set(engine.update.tier.open), _queued(engine), list(downs)))
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [
        ({"j"}, [], []),
        ({"j"}, [("update_exit", "j")], []),
        (False, 2),
    ]
    assert downs == ["j"]
    assert len(_applications(tmp_path)) == 1


def test_a_member_whose_group_is_clear_waits_for_its_record_and_a_later_timeout_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-431, DL-523: the stop sequence ends with nothing left before the outcome is known.
    The member stays open until the record arrives, which is before the record timeout; then it
    is taken down once. The timeout that fires afterwards meets an ended attempt and changes
    nothing."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    downs = _downs(monkeypatch)
    timers: list[tuple[str, int]] = []
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        monkeypatch.setattr(
            engine.stops,
            "arm_timer",
            lambda key, generation, _delay: timers.append((key, generation)),
        )
        try:
            await _stopping(engine, tmp_path)
            await ladders.end(engine, RUN_ID, PGID)
            assert engine.update.tier is not None
            seen.append(
                (engine.attempts.state(RUN_ID), set(engine.update.tier.open), _queued(engine))
            )
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            seen.append((engine.attempts.state(RUN_ID), _queued(engine)))
            [(key, generation)] = timers
            engine.attempts.record_timeout(key, generation)
            seen.append(_queued(engine))
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [
        ("STOPPING", {"j"}, []),
        ("ENDED", [("update_exit", "j")]),
        [("update_exit", "j")],
        (False, 2),
    ]
    assert downs == ["j"]
    [outcome] = [o for o in _kinds(tmp_path, "outcome") if o["attempt"] == RUN_ID]
    assert (outcome["outcome"], outcome["cause"]) == ("stopped", "update_stop")


def test_a_second_notice_for_a_member_already_down_changes_neither_the_open_set_nor_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-432, DL-523: a notice consumes only its member. A second notice for `j`, already out of
    the open set, leaves `k` open, the update stopping and the journal as it was. The second
    notice is posted directly; no validated input is known to reach it (DL-491)."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "k": "sleep 30"}))
        try:
            await _stopping(engine, tmp_path, ("j", "k"))
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            await ladders.end(engine, RUN_ID, PGID)
            await engine.run_until_quiescent(T0)
            assert engine.update.tier is not None
            before = rows(tmp_path / "run")
            seen.append(set(engine.update.tier.open))
            engine.post_update_exit("j")
            await engine.run_until_quiescent(T0)
            seen.append((set(engine.update.tier.open), engine.update.stopping))
            seen.append(rows(tmp_path / "run") == before)
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [{"k"}, ({"k"}, True), True]
    assert downs == ["j"]


def test_clearance_of_another_attempt_of_the_same_job_does_not_bring_the_member_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-432: a member is down when the attempt the update selected is finished. `j`'s first
    dispatch (the stand-in task's attempt id) is another attempt of the same job; clearance
    reported for it leaves `j` open and queues nothing. The selected attempt's own end takes
    `j` down once. The clearance is reported directly; no validated input is known to reach it
    (DL-491)."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            await _stopping(engine, tmp_path)
            [first] = [d["attempt"] for d in _kinds(tmp_path, "dispatch") if d["attempt"] != RUN_ID]
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            engine.update.cleaned(first)
            assert engine.update.tier is not None
            seen.append((set(engine.update.tier.open), _queued(engine)))
            await ladders.end(engine, RUN_ID, PGID)
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [({"j"}, []), (False, 2)]
    assert downs == ["j"]


def test_a_member_whose_attempt_the_engine_forgot_before_its_notice_still_comes_down_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-431, DL-491: `j` exits 0 and leaves a child, so its leak ladder runs after the record;
    its plan completes, and an operator's STATUS shows it RUNNING again. An update selects that
    attempt, which ended before the admission and still cleans. The ladder's end forgets the
    attempt and then queues the notice: the member comes down once and the update applies
    once."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)  # the child the command left
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            _dispatched(engine, "j", RUN_ID, PGID)
            engine.attempts.status_recorded(
                RUN_ID, {"version": 1, "outcome": "exited", "exit_code": 0}
            )
            engine._facts[RUN_ID].exited = True  # its wrapper has exited
            await engine.run_until_quiescent(T0)  # the plan's feed: the plan is done
            engine.inject(Event(at=T0, kind="STATUS", payload={"job": "j", "status": "RUNNING"}))
            await engine.run_until_quiescent(T0)
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("j",)
            seen.append((engine.update.stopping, engine.attempts.knows(RUN_ID)))
            await ladders.end(engine, RUN_ID, PGID)
            seen.append((engine.attempts.knows(RUN_ID), _queued(engine)))
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            engine.stops.cancel_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    assert seen == [(True, True), (False, [("update_exit", "j")]), (False, 2)]
    assert downs == ["j"]
    assert len(_applications(tmp_path)) == 1


# ---------------------------------------------------------------- a restart while the update waits


def _crashed_pair(tmp_path: Path) -> dict[str, str]:
    """A run root whose engine died while an update stopped `j` and `k`. `j`'s spool holds its
    signalled record; `k`'s holds only `spawn.json`, so the restart finds no record for it.
    Answers each job's attempt id."""
    attempts: dict[str, str] = {}

    async def first() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "k": "sleep 30"}))
        for job in ("j", "k"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        assert engine.journal is not None
        table = engine.journal.open.attempts
        for job, pgid in (("j", PGID), ("k", PGID + 1)):
            [run_id] = [a for a, open_ in table.items() if open_.job == job]
            attempts[job] = run_id
            spool = Path(str(table[run_id].spool))
            spool.mkdir(parents=True, exist_ok=True)
            spawn = {**SPAWN, "run_id": run_id, "job": job, "command_pgid": pgid}
            spawn |= {"command_pid": pgid, "boot_id": runner_procid.current_boot_id()}
            (spool / "spawn.json").write_text(json.dumps(spawn))
            if job == "j":
                status = {"version": 1, "job": job, "run_number": 1, "run_id": run_id}
                (spool / "status.json").write_text(json.dumps({**status, **SIGNALED}))
        _estate(tmp_path, _jil({"j": "sleep 31", "k": "sleep 31"}))
        token = _plan(engine, tmp_path)
        intent = {
            "event": "intent",
            "at": T0.isoformat(),
            "token": token,
            "live_rev": 1,
            "stop": [
                {"job": job, "run_number": 1, "attempt": attempts[job], "cause": "update_stop"}
                for job in ("j", "k")
            ],
            "filter": ["j", "k"],
            "stop_running": True,
            "request_id": "u1",
            "fingerprint": UpdateRequest(token, 1, engine.baseline_id, "u1").fingerprint,
        }
        engine.journal.append(Record(T0, "config", None, "u1", intent))
        for live in list(engine._live.values()):
            live.task.cancel()
        await engine.shutdown()
        _close(engine)  # the engine dies: no shutdown record

    asyncio.run(first())
    return attempts


async def _resume(tmp_path: Path, start: datetime) -> Engine:
    return await resume_run(
        prepare_estate(tmp_path / "estate").catalog,
        tmp_path / "run",
        clock=VirtualClock(start=start),
        adapters={"CMD": FakeAdapter(default=None)},
        settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
    )


def _update_steps(monkeypatch: pytest.MonkeyPatch) -> tuple[list[tuple[Any, ...]], asyncio.Event]:
    """Each update step the loop takes: its stamp, the member it names (None for a look), the
    open set after it and whether the update still stops. The event is set after the first."""
    steps: list[tuple[Any, ...]] = []
    first = asyncio.Event()
    step = Engine._update_step

    async def recorded(self: Engine, pending: Any) -> None:
        await step(self, pending)
        tier = self.update.tier
        assert tier is not None
        steps.append((pending.at, pending.update, set(tier.open), self.update.stopping))
        first.set()

    monkeypatch.setattr(Engine, "_update_step", recorded)
    return steps, first


def test_a_restored_closing_member_stays_open_until_its_cleanup_and_comes_down_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-433, DL-432: `j`'s outcome was committed while its cleanup ran when the engine died.
    The restart restores it closing: before its cleanup reads anything the update is stopping
    with `j` open. The cleanup reads the command alive and runs the ladder; the update waits for
    it. Once the ladder ends, `j` comes down once and `config applied` is written once, after
    `j`'s `ladder_done`."""
    run_root, attempt = _crashed_update(tmp_path, closing=True)
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def restart() -> None:
        engine = await _resume(tmp_path, T0 + timedelta(minutes=1))
        try:
            tier = engine.update.tier
            assert tier is not None
            seen.append((engine.update.stopping, set(tier.open), engine.attempts.state(attempt)))
            loop = asyncio.ensure_future(engine.run_until_quiescent(engine.clock.now()))
            async with asyncio.timeout(30):
                await ladders.running(PGID)  # the loop took the catch-up's cleanup step
                seen.append((engine.update.stopping, set(tier.open), list(downs)))
                await ladders.end(engine, attempt, PGID)
                await loop
            await engine.run_until_quiescent(engine.clock.now())
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    assert seen == [(True, {"j"}, "ENDED"), (True, {"j"}, []), (False, 2)]
    assert downs == ["j"]
    assert len(_applications(tmp_path)) == 1
    order = [(k, b.get("event"), b.get("attempt")) for _, k, b in rows(run_root)]
    applied = [i for i, o in enumerate(order) if o[:2] == ("config", "applied")][-1]
    assert order.index(("follow_up", "ladder_done", attempt)) < applied


@pytest.mark.parametrize("all_down_before", [True, False], ids=["first-look", "last-stop"])
def test_after_a_restart_the_first_check_follows_the_recovered_feed_and_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_down_before: bool
) -> None:
    """DL-432, DL-433: the update applies at its first check, the loop's only update step, which
    carries the same stamp as the recovered feed of `b` (outside the stop set) and is taken after
    it; whether `j` was down before the crash or its observation in the catch-up is the last
    stop."""

    run_root, j = _crashed_update(tmp_path, closing=False)
    if all_down_before:
        ended = outcome_record(j, "j", 1, "stopped", {}, T0, cause="update_stop")
        append(run_root, "outcome", ended.payload, at=T0)
        append(run_root, "follow_up", plan_record(j, "j", 1, "none", [], T0).payload, at=T0)
    _Procs(monkeypatch)
    steps, _first = _update_steps(monkeypatch)

    async def restart() -> None:
        engine = await _resume(tmp_path, T0)
        try:
            await engine.run_until_quiescent(engine.clock.now())
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    assert [step[1:] for step in steps] == [(None, set(), False)]
    order = rows(run_root)
    [applied] = [s for s, k, b in order if k == "config" and b.get("event") == "applied"][1:]
    [feed] = [
        (s, b["at"])
        for s, k, b in order
        if k == "input" and b.get("source") == "reconcile" and b["payload"]["job"] == "b"
    ]
    assert feed[0] < applied
    assert datetime.fromisoformat(feed[1]) == steps[0][0]  # the same stamp


def test_a_member_down_at_the_first_checks_stamp_stays_open_until_its_own_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-432, DL-523: after a restart the catch-up observes `j` (down, taken by the first
    check) and `k` (no record: its cleanup's ladder runs). The first check takes `j` and leaves
    `k` open. `k`'s stop ends after that check; its notice carries the first check's stamp and
    is queued behind it, and `k` stays open until that notice is consumed, which applies the
    update."""
    attempts = _crashed_pair(tmp_path)
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID + 1)
    downs = _downs(monkeypatch)
    steps, first = _update_steps(monkeypatch)
    seen: list[object] = []

    async def restart() -> None:
        engine = await _resume(tmp_path, T0 + timedelta(minutes=1))
        try:
            loop = asyncio.ensure_future(engine.run_until_quiescent(engine.clock.now()))
            async with asyncio.timeout(30):
                await first.wait()
                tier = engine.update.tier
                assert tier is not None
                seen.append((set(tier.open), list(downs), engine.update.stopping))
                await ladders.end(engine, attempts["k"], PGID + 1)
                seen.append((set(tier.open), _queued(engine)))
                await loop
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            ladders.open_all()
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    assert seen == [
        ({"k"}, ["j"], True),
        ({"k"}, [("update_exit", "k")]),
        (False, 2),
    ]
    assert downs == ["j", "k"]
    assert [step[1:] for step in steps] == [(None, {"k"}, True), ("k", set(), False)]
    assert steps[0][0] == steps[1][0]  # equal stamps: the arrival order decides


def test_a_first_check_stamped_ahead_of_present_time_waits_for_the_horizon_to_reach_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-432: the catch-up's steps and the first check are stamped at the journal's last
    instant. A restart on a virtual clock behind that instant takes none of them while the
    horizon is present time; once the horizon reaches the stamp, the first check takes `j` and
    applies the update."""
    _crashed_update(tmp_path, closing=False)
    _Procs(monkeypatch)
    steps, _first = _update_steps(monkeypatch)
    seen: list[object] = []

    async def restart() -> None:
        engine = await _resume(tmp_path, T0 - timedelta(minutes=1))
        try:
            assert engine.resume_at is not None and engine.resume_at > engine.clock.now()
            await engine.run_until_quiescent(engine.clock.now())
            seen.append((engine.update.stopping, list(steps)))
            await engine.run_until_quiescent(engine.resume_at)
            seen.append((engine.update.stopping, engine.config_rev))
            seen.append(engine.resume_at)
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    resume_at = seen.pop()
    assert seen == [(True, []), (False, 2)]
    assert [(step[0], step[1:]) for step in steps] == [(resume_at, (None, set(), False))]


# ---------------------------------------------------------------- the processes of the named jobs


#: a TERM grace of one day, beyond any scenario: no KILL ends a parked stop
NO_KILL_S = 86400.0

_PARKED = """import signal, sys, time
from pathlib import Path

ready, termed, release = (Path(arg) for arg in sys.argv[1:])


def stop(*_):
    termed.touch()
    while not release.exists():
        time.sleep(0.01)
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
ready.touch()
while True:
    signal.pause()
"""


class _Parked:
    """A command that, once TERMed, waits for its release file and exits 0, so the update's wait
    lasts until the test releases it. `ready` is touched once its TERM handler is in place."""

    def __init__(self, tmp_path: Path, job: str) -> None:

        program = tmp_path / "parked.py"
        program.write_text(_PARKED)
        self.ready, self.termed, self.release = (
            tmp_path / f"{job}.{name}" for name in ("ready", "termed", "release")
        )
        files = " ".join(shlex.quote(str(path)) for path in (self.ready, self.termed, self.release))
        self.command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(program))} {files}"


def _probe_application(
    monkeypatch: pytest.MonkeyPatch, run_root: Path, jobs: tuple[str, ...], old: tuple[str, ...]
) -> dict[str, Any]:
    """What is true of the named jobs' processes when `config applied` is journaled: for each
    job of `old`, whether its first attempt's recorded command group still exists, and the
    spool directories of `jobs` that exist."""

    seen: dict[str, Any] = {}
    journal_applied = EngineUpdate.journal_applied

    def probed(self: EngineUpdate, application: Any) -> Any:
        groups: dict[str, bool] = {}
        for job in old:
            spawn = json.loads((run_root / "runs" / f"{job}.1" / "1" / "spawn.json").read_text())
            try:
                os.killpg(int(spawn["command_pgid"]), 0)
                groups[job] = True
            except ProcessLookupError:
                groups[job] = False
        seen["groups"] = groups
        seen["spools"] = sorted(
            str(path.relative_to(run_root / "runs"))
            for job in jobs
            for path in (run_root / "runs").glob(f"{job}.*/*")
            if path.is_dir()
        )
        return journal_applied(self, application)

    monkeypatch.setattr(EngineUpdate, "journal_applied", probed)
    return seen


@pytest.mark.parametrize(
    "case", ["both-running", "one-running-one-started", "both-started-a-third-running"]
)
def test_no_process_of_a_named_job_runs_between_admission_and_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-522: the update is atomic across the jobs it names. `p` and `c` are changed together.
    Every named job's running process is stopped, `config applied` is journaled only once each
    stopped process group is gone, no process of `p` or `c` is launched or dispatched between
    the intent and `config applied`, and afterwards each is dispatched once, under revision 2.
    A start of a named idle job that arrives during the wait is held `dispatch_pending`; its row
    already shows the run the start opened."""

    running = {
        "both-running": ("c", "p"),
        "one-running-one-started": ("p",),
        "both-started-a-third-running": ("b",),
    }[case]
    started = tuple(job for job in ("c", "p") if job not in running)
    named = ("b", "c", "p") if "b" in running else ("c", "p")
    parked = {job: _Parked(tmp_path, job) for job in running}
    old = {job: parked[job].command if job in parked else "exec sleep 30" for job in named}
    new = {job: "exec sleep 31" for job in named}
    estate = Estate(tmp_path, wiring_jil(old))
    run_root = tmp_path / "run"
    probe = _probe_application(monkeypatch, run_root, ("c", "p"), running)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        # a TERM grace no run reaches: a parked command ends only on its release, so the
        # wait lasts until the test releases it, however slow the machine
        settings = EngineSettings(grace_term_s=NO_KILL_S, grace_kill_s=TIMEOUT_S)
        engine = _start(run_root, estate, settings=settings)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            for job in running:
                engine.inject(
                    Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job})
                )
            await _running(engine, *running)
            await _until(lambda: all(parked[job].ready.exists() for job in running))
            estate.write(wiring_jil(new))
            answer = await _update(engine, wiring_plan(engine, estate))
            assert isinstance(answer.decision, Admitted)
            seen["stop_set"] = answer.decision.stop_set
            await _until(lambda: all(parked[job].termed.exists() for job in running))
            for job in started:
                decided = await engine.submit(
                    Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}),
                    Envelope(request_id=f"start-{job}", expect={}),
                )
                seen.setdefault("decisions", []).append(decided.decision)
            store = engine.oracle.store.job
            seen["rows"] = {job: (store[job].status, store[job].run_number) for job in named}
            seen["stopping"] = engine.update.view()
            for job in running:
                parked[job].release.touch()
            await _applied(answer)
            await _running(engine, *named, ordinal=1)
            for job in running:
                await _until((run_root / "runs" / f"{job}.1" / "2" / "spawn.json").exists)
        finally:
            for one in parked.values():
                one.release.touch()
            await _finish(engine, loop)

    asyncio.run(scenario())
    assert seen["stop_set"] == running
    assert seen["stopping"]["stopping"] == list(running)
    assert seen.get("decisions", []) == ["applied"] * len(started)
    assert probe["groups"] == {job: False for job in running}
    # no process of `p` or `c` launched but the stopped ones' first attempts
    assert probe["spools"] == [f"{job}.1/1" for job in ("c", "p") if job in running]
    order = _order(tmp_path)
    intent = order.index(("config", "intent", None, None, None))
    applied = [i for i, o in enumerate(order) if o[:2] == ("config", "applied")][-1]
    assert not [o for o in order[intent:applied] if o[0] == "dispatch" and o[2] in named]
    pending = [o[2] for o in order[intent:applied] if o[0] == "dispatch_pending"]
    assert pending == list(started)
    after = [(o[2], o[3], o[4]) for o in order[applied:] if o[0] == "dispatch"]
    assert sorted(after) == sorted((job, 2 if job in running else 1, 2) for job in named)
    # the row of a held start opened its run and shows RUNNING while its launch waits
    assert seen["rows"] == {job: ("RUNNING", 1) for job in named}
