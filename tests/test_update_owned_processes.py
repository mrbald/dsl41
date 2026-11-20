"""An update stops every process the engine owns for the jobs it names, whatever the job's row
says and whichever run the process belongs to, and applies only once each is gone (DL-525,
DL-522): a changed job an operator set INACTIVE while its command runs (DL-235), a changed job
whose earlier run is still being stopped while its row has opened a newer run, a changed job
whose row a KILLJOB has made TERMINATED while its stop sequence runs, and one whose group is
held after it outlived its stop. A restart while the update waits keeps waiting for the same
attempt.

The scenarios run on the virtual clock with `test_engine_stops_cookie`'s process stand-ins. Each
stop sequence is parked until the test ends it, and the job's group reads alive until then
(`test_update_atomic._Ladders`). The harness's adapter task for the job is the observed
attempt's wrapper: the engine's own kills reach the attempt through it, and it exits when the
test says so (`_observed`).
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from store_surgery import rows
from test_engine_stops_cookie import PGID, SPAWN, T0, _Procs
from test_update_atomic import _applications, _downs, _Ladders, _resume
from test_update_settlement import SIGNALED, _close, _engine, _estate, _jil, _plan, _request

from dsl41 import runner_procid, runner_stops
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_attempts import ORACLE_KILL
from dsl41.runner_stops import AttemptFacts

#: a job's status set by an operator while its command runs (DL-235)
INACTIVE: dict[str, object] = {"job": "j", "status": "INACTIVE"}
#: the inputs that open `j`'s run 2 while run 1 runs on behind an INACTIVE row (DL-235)
NEWER_RUN = (
    Event(at=T0, kind="STATUS", payload=INACTIVE),
    Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}),
)
KILLJOB = (Event(at=T0, kind="KILLJOB", payload={"job": "j"}),)


class _Box(_Procs):
    """`_Procs` whose cookie scan names the attempt `run_id`."""

    run_id = ""

    def _ours(self, _prefix: str) -> runner_procid.Ours:
        cookied = {pid for pid, (_g, cookie, _i) in self.procs.items() if cookie}
        return runner_procid.Ours({pid: (self.run_id, f"ticks:{pid}") for pid in cookied})


def _observed(
    engine: Engine, job: str, pgid: int, *, wrapper: bool = True
) -> tuple[str, asyncio.Task[None] | None]:
    """Take over the engine's own dispatch of `job`'s run 1 as the live engine observes it: its
    spool holds `spawn.json` naming group `pgid`, and the attempt tier holds it RUNNING. With
    `wrapper`, the harness's adapter task, which the engine holds under the attempt's run id,
    is the attempt's wrapper: an oracle kill stops the attempt and moves the task to reaping,
    and the task exits when the test cancels it. Without, the task is collected now and the
    engine holds no task of `job`. Answers the run id and the wrapper task."""
    assert engine.journal is not None
    [(attempt, open_)] = [(a, o) for a, o in engine.journal.open.attempts.items() if o.job == job]
    assert open_.spool is not None
    spool = Path(open_.spool)
    spool.mkdir(parents=True, exist_ok=True)
    spawn = {**SPAWN, "run_id": attempt, "job": job, "command_pgid": pgid, "command_pid": pgid}
    spawn["boot_id"] = runner_procid.current_boot_id()
    (spool / "spawn.json").write_text(json.dumps(spawn))
    live = engine._live[job]
    assert live.attempt == attempt
    task: asyncio.Task[None] | None = live.task
    if wrapper:
        engine._observed_tasks[id(live.task)] = attempt
    else:
        del engine._live[job]
        live.task.cancel()
        engine._reaping.append(live.task)
        task = None
    env = {**open_.env, "DSL41_COOKIE": runner_procid.cookie("inst", attempt)}
    engine.adopt_attempt(AttemptFacts(attempt, job, 1, 1, spool, env))
    engine._restored.discard(attempt)  # dispatched by this engine, not found by a restart
    engine.stops.scan_available = True
    engine.stops.note_boot(spawn)
    engine.attempts.spawn_recorded(attempt, spawn)
    return attempt, task


async def _killed_by_the_engine(
    engine: Engine,
    ladders: _Ladders,
    attempt: str,
    wrapper: asyncio.Task[None] | None,
    events: tuple[Event, ...],
) -> None:
    """Feed `events`, whose input makes the engine stop run 1 as an oracle kill through its
    wrapper. The loop runs on while the stop sequence is parked: the exit record lands, the
    wrapper exits, and the loop settles with the stop sequence still running."""
    assert wrapper is not None
    for event in events:
        engine.inject(event)
    loop = asyncio.ensure_future(engine.run_until_quiescent(T0))
    async with asyncio.timeout(30):
        await ladders.running(PGID)
        assert engine.stop_cause(attempt) == ORACLE_KILL
        engine.attempts.status_recorded(attempt, SIGNALED)
        wrapper.cancel()
        await loop


def _journal(tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    return [(kind, body) for _, kind, body in rows(tmp_path / "run")]


def _intent_and_applied(journal: list[tuple[str, dict[str, Any]]]) -> tuple[int, int]:
    """The index of the update's `config intent` and of its `config applied`."""
    [intent] = [
        i for i, (k, b) in enumerate(journal) if (k, b.get("event")) == ("config", "intent")
    ]
    [applied] = [
        i
        for i, (k, b) in enumerate(journal)
        if (k, b.get("event")) == ("config", "applied") and "token" in b
    ]
    return intent, applied


def _dispatches(journal: list[tuple[str, dict[str, Any]]], since: int) -> list[tuple[int, int]]:
    """Each dispatch of `j` from index `since` on: (run number, rev)."""
    return [
        (b["run_number"], b["rev"])
        for k, b in journal[since:]
        if k == "dispatch" and b["job"] == "j"
    ]


def _event_index(journal: list[tuple[str, dict[str, Any]]], kind: str, attempt: str) -> int:
    """The index of the attempt's one `outcome` or `ladder_done`."""
    [found] = [
        i
        for i, (k, b) in enumerate(journal)
        if b.get("attempt") == attempt
        and (k == kind if kind == "outcome" else b.get("event") == kind)
    ]
    return found


def _teardown(engine: Engine, ladders: _Ladders) -> None:
    ladders.open_all()
    engine.stops.cancel_all()


async def _crash(engine: Engine) -> None:
    """The engine dies: its stop sequences and tasks end with nothing recorded, and no
    shutdown record is written."""
    for task in engine.attempts.running_ladders():
        task.cancel()
    for live in list(engine._live.values()):
        live.task.cancel()
    engine.stops.cancel_all()
    await engine.shutdown()
    _close(engine)


async def _started(ladders: _Ladders, pgid: int) -> None:
    """Wait until the group's stop sequence has started; a stop that never starts fails."""
    async with asyncio.timeout(30):
        await ladders.running(pgid)


# ---------------------------------------------------------------- live


def test_a_changed_job_set_inactive_while_its_command_runs_is_stopped_before_the_update_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-525, DL-235: an operator's INACTIVE leaves `j`'s command running while its row is not
    live. An update that changes `j` stops that process and keeps waiting while its group is
    alive, its exit record and its wrapper's exit included; it applies once, after the stop
    sequence ends. The run is not rerun: its row is not live, so the interruption rule gives
    `none`, which `config applied` names."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            attempt, wrapper = _observed(engine, "j", PGID)
            assert wrapper is not None
            engine.inject(Event(at=T0, kind="STATUS", payload=INACTIVE))
            await engine.run_until_quiescent(T0)
            seen.append((engine.oracle.store.job["j"].status, engine.attempts.state(attempt)))
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            seen.append((answer.decision.stop_set, engine.update.stopping, engine.config_rev))
            await _started(ladders, PGID)
            seen.append(engine.stop_cause(attempt))
            engine.attempts.status_recorded(attempt, SIGNALED)
            wrapper.cancel()
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev, list(downs)))
            await ladders.end(engine, attempt, PGID)
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
            seen.append(attempt)
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    attempt = str(seen.pop())
    assert seen == [
        ("INACTIVE", "RUNNING"),
        (("j",), True, 1),
        "update_stop",
        (True, 1, []),
        (False, 2),
    ]
    assert downs == ["j"]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [["j", "none"]]
    journal = _journal(tmp_path)
    intent, at = _intent_and_applied(journal)
    assert journal[intent][1]["stop"] == [
        {"job": "j", "run_number": 1, "attempt": attempt, "cause": "update_stop"}
    ]
    ended = journal[_event_index(journal, "outcome", attempt)][1]
    assert (ended["outcome"], ended["cause"]) == ("stopped", "update_stop")
    assert _event_index(journal, "outcome", attempt) > at
    [plan] = [
        b
        for k, b in journal
        if k == "follow_up" and b["event"] == "plan" and b["attempt"] == attempt
    ]
    assert plan["plan"] == "none"
    assert _dispatches(journal, intent) == []  # no rerun of that run, no other launch of `j`


@pytest.mark.parametrize("stop", ["engine-kill", "posted-kill"])
def test_an_earlier_run_still_being_stopped_holds_the_update_and_the_new_run_starts_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """DL-525, DL-426: `j`'s run 1 is being stopped while its row has opened run 2, whose start
    waits for run 1 to settle. An update that changes `j` selects run 1's attempt, keeps its
    first stop cause and waits while its group is alive, until its stop sequence ends. Run 2's
    start is launched once, after `config applied`, under revision 2; run 1 gets `none`.

    With `engine-kill`, the start's own stop of run 1 goes through the wrapper, and run 1's
    record has landed when the update is admitted. With `posted-kill`, run 1 is still STOPPING
    at the admission; the test posts the oracle kill to the attempt itself, so this case does
    not cover the engine's hand-off of that kill. In the virtual domain the loop settles before
    each step and waits for the wrapper a kill moves to reaping, and the harness's wrapper
    exits only with its record, so no admission can come between the kill and the record."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            attempt, wrapper = _observed(engine, "j", PGID, wrapper=stop == "engine-kill")
            if stop == "engine-kill":
                await _killed_by_the_engine(engine, ladders, attempt, wrapper, NEWER_RUN)
            else:
                for event in NEWER_RUN:
                    engine.inject(event)
                await engine.run_until_quiescent(T0)
                engine.attempts.stop(attempt, ORACLE_KILL)
                await _started(ladders, PGID)
            row = engine.oracle.store.job["j"]
            seen.append((row.run_number, engine.attempts.state(attempt)))
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            seen.append((answer.decision.stop_set, engine.update.stopping, engine.config_rev))
            if stop == "posted-kill":
                engine.attempts.status_recorded(attempt, SIGNALED)
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev, list(downs)))
            await ladders.end(engine, attempt, PGID)
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
            seen.append(attempt)
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    attempt = str(seen.pop())
    state = "ENDED" if stop == "engine-kill" else "STOPPING"
    assert seen == [(2, state), (("j",), True, 1), (True, 1, []), (False, 2)]
    assert downs == ["j"]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [["j", "none"]]
    journal = _journal(tmp_path)
    intent, at = _intent_and_applied(journal)
    assert journal[intent][1]["stop"] == [
        {"job": "j", "run_number": 1, "attempt": attempt, "cause": ORACLE_KILL}
    ]
    if stop == "engine-kill":
        assert _event_index(journal, "ladder_done", attempt) < at
    pending = [b["run_number"] for k, b in journal[:intent] if k == "dispatch_pending"]
    assert pending == [2]
    assert _dispatches(journal, intent) == [(2, 2)]
    assert _dispatches(journal, at) == [(2, 2)]  # after the application only


def test_a_killed_job_whose_row_is_terminated_while_its_stop_sequence_runs_holds_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-525, DL-426: a KILLJOB makes `j`'s row TERMINATED at once, while the engine's stop
    of its command still runs; the exit record has landed and the wrapper has exited. An
    update that changes `j` waits while that stop sequence runs and its group is alive, and
    applies once, after it. `config applied` says `none` for `j`, and `j` is not rerun."""
    _Procs(monkeypatch)
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            attempt, wrapper = _observed(engine, "j", PGID)
            await _killed_by_the_engine(engine, ladders, attempt, wrapper, KILLJOB)
            row = engine.oracle.store.job["j"]
            ladder = engine.attempts.ladder_running(attempt)
            seen.append((row.status, engine.attempts.state(attempt), ladder))
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            seen.append((answer.decision.stop_set, engine.update.stopping, engine.config_rev))
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, list(downs)))
            await ladders.end(engine, attempt, PGID)
            await engine.run_until_quiescent(T0)
            seen.append((engine.update.stopping, engine.config_rev))
            seen.append(attempt)
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    attempt = str(seen.pop())
    assert seen == [
        ("TERMINATED", "ENDED", True),
        (("j",), True, 1),
        (True, []),
        (False, 2),
    ]
    assert downs == ["j"]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [["j", "none"]]
    journal = _journal(tmp_path)
    intent, at = _intent_and_applied(journal)
    assert journal[intent][1]["stop"] == [
        {"job": "j", "run_number": 1, "attempt": attempt, "cause": ORACLE_KILL}
    ]
    assert _event_index(journal, "ladder_done", attempt) < at
    assert _dispatches(journal, intent) == []


def test_a_held_group_of_a_terminal_row_holds_the_update_until_the_group_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-525, DL-426: a KILLJOB makes `j`'s row TERMINATED, and a member of `j`'s group that
    ignores TERM outlives the stop sequence, so the group is held. An update that changes `j`
    waits while the group is held: the held poll's first look KILLs the member and the update
    still waits; the next look finds nothing left (`group_gone`) and the update applies once.
    `config applied` says `none` for `j`. The test takes the poll's looks itself."""
    box = _Box(monkeypatch, p4194311=(PGID, True, True))
    box.tokens[PGID] = "t"
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)

    async def survives(_self: Any, _pgid: int, _token: str, _grace: float, *_: Any) -> bool:
        return True

    monkeypatch.setattr(runner_stops.EngineStops, "outlives", survives)
    downs = _downs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        monkeypatch.setattr(engine.stops, "watch_groups", lambda: None)
        try:
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
            await engine.run_until_quiescent(T0)
            attempt, wrapper = _observed(engine, "j", PGID)
            box.run_id = attempt
            await _killed_by_the_engine(engine, ladders, attempt, wrapper, KILLJOB)
            await ladders.end(engine, attempt, PGID)
            await engine.attempts.join()  # the survivor check holds the group
            await engine.run_until_quiescent(T0)
            row = engine.oracle.store.job["j"]
            seen.append((row.status, attempt in engine.held_groups))
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            seen.append((answer.decision.stop_set, engine.update.stopping, engine.config_rev))
            engine.stops._look()  # KILLs the member
            await engine.run_until_quiescent(T0)
            seen.append((box.procs, attempt in engine.held_groups, engine.update.stopping))
            engine.stops._look()  # nothing left
            await engine.run_until_quiescent(T0)
            seen.append((attempt in engine.held_groups, engine.update.stopping, engine.config_rev))
            seen.append(attempt)
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    attempt = str(seen.pop())
    assert seen == [
        ("TERMINATED", True),
        (("j",), True, 1),
        ({}, True, True),
        (False, False, 2),
    ]
    assert downs == ["j"]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [["j", "none"]]
    journal = _journal(tmp_path)
    intent, at = _intent_and_applied(journal)
    assert [entry["attempt"] for entry in journal[intent][1]["stop"]] == [attempt]
    assert _event_index(journal, "ladder_done", attempt) < at
    assert _dispatches(journal, intent) == []


@pytest.mark.parametrize("row", ["RUNNING", "INACTIVE"])
def test_a_named_job_with_no_process_the_engine_owns_is_down_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str
) -> None:
    """DL-525, DL-432: an operator's status puts changed `j` in a row with no process behind
    it. Live, it is in the stop set and down at once; not live, it is not stopped. Either way
    the update applies in its admission's step, and `j` gets no launch from it."""
    _Procs(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        try:
            engine.inject(Event(at=T0, kind="STATUS", payload={"job": "j", "status": row}))
            await engine.run_until_quiescent(T0)
            _estate(tmp_path, _jil({"j": "sleep 31"}))
            answer = await _request(engine, _plan(engine, tmp_path))
            assert isinstance(answer.decision, Admitted)
            seen.append((answer.decision.stop_set, engine.update.stopping, engine.config_rev))
        finally:
            await engine.shutdown()
            _close(engine)

    asyncio.run(scenario())
    stops = ("j",) if row == "RUNNING" else ()
    assert seen == [(stops, False, 2)]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [[job, "none"] for job in stops]
    assert _dispatches(_journal(tmp_path), 0) == []


# ---------------------------------------------------------------- a restart while the update waits


@pytest.mark.parametrize("case", ["inactive", "earlier-run"])
def test_a_restart_while_the_update_waits_for_an_owned_process_waits_for_the_same_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-525, DL-433: the engine dies while an update waits for the attempt it selected for
    `j`: run 1 of an INACTIVE row, still stopping with no record; or run 1, which the start of
    run 2 stopped, whose outcome is in while its stop sequence runs. The restart restores the
    update stopping with `j` open and the same attempt, finishes that attempt's stop, and
    applies once. Run 2's start, in the second case, is launched once, after `config
    applied`, under revision 2."""
    _Procs(monkeypatch)
    selected: list[str] = []

    async def first() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        attempt, wrapper = _observed(engine, "j", PGID)
        selected.append(attempt)
        if case == "inactive":
            engine.inject(Event(at=T0, kind="STATUS", payload=INACTIVE))
            await engine.run_until_quiescent(T0)
        else:
            await _killed_by_the_engine(engine, ladders, attempt, wrapper, NEWER_RUN)
        _estate(tmp_path, _jil({"j": "sleep 31"}))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("j",)
        assert engine.update.stopping and engine.config_rev == 1
        await _started(ladders, PGID)
        await _crash(engine)

    asyncio.run(first())
    [attempt] = selected
    seen: list[object] = []

    async def restart() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)  # the restart's reading finds run 1's command alive
        engine = await _resume(tmp_path, T0 + timedelta(minutes=1))
        try:
            tier = engine.update.tier
            assert tier is not None
            seen.append((engine.update.stopping, set(tier.open), dict(engine.update._attempts)))
            loop = asyncio.ensure_future(engine.run_until_quiescent(engine.clock.now()))
            async with asyncio.timeout(30):
                await ladders.running(PGID)
                seen.append((engine.update.stopping, set(tier.open)))
                await ladders.end(engine, attempt, PGID)
                await loop
            await engine.run_until_quiescent(engine.clock.now())
            seen.append((engine.update.stopping, engine.config_rev))
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    assert seen == [(True, {"j"}, {"j": attempt}), (True, {"j"}), (False, 2)]
    [applied] = _applications(tmp_path)
    assert applied["stopped"] == [["j", "none"]]
    journal = _journal(tmp_path)
    intent, at = _intent_and_applied(journal)
    assert [entry["attempt"] for entry in journal[intent][1]["stop"]] == [attempt]
    if case == "inactive":
        assert _event_index(journal, "outcome", attempt) > at  # kept for the application
    else:
        assert _event_index(journal, "outcome", attempt) < intent
        assert intent < _event_index(journal, "ladder_done", attempt) < at
    expected = [] if case == "inactive" else [(2, 2)]
    assert _dispatches(journal, intent) == expected
    assert _dispatches(journal, at) == expected
