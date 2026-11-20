"""A file watch of a job an update names launches and polls nothing while the update waits, also
after a restart, and is taken up after the application only while its run is still live there
(DL-526, DL-432). A restart classifies a watch's durable evidence first: a complete log is fed,
a run that ended is closed, a log that does not read as the attempt's is quarantined, and only an
unfinished watch of a live run is held for the update.

The scenarios run on the virtual clock with `test_engine_stops_cookie`'s process stand-ins. The
update waits for `j`, a changed command whose stop sequence the test parks and ends
(`test_update_atomic._Ladders`); `w` is a file watch on a file that never appears. Each
`Engine._launch` is counted, and each line of `watch.jsonl` is one poll.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from reload_fixtures import prepare_estate, write_config
from store_surgery import rows
from store_surgery import update as rewrite
from test_engine_stops_cookie import PGID, T0, _Procs
from test_update_atomic import _Ladders, _update_steps
from test_update_owned_processes import _crash, _observed, _started, _teardown
from test_update_settlement import SIGNALED, _close, _jil, _plan, _request

from dsl41 import main_config as main
from dsl41.oracle_state import Event
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    ARTIFACT_FORMAT_VERSION,
    WATCH_LOG,
    FakeAdapter,
    FileWatcherAdapter,
    append_watch_line,
)
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_update import EngineUpdate, UpdateRequest

#: the watch interval before and after the update
OLD_S, NEW_S = 60, 90


def _watch(tmp_path: Path, name: str = "w", interval: int = OLD_S, extra: str = "") -> str:
    return (
        f"insert_job: {name}\njob_type: f\nwatch_file: {tmp_path / 'never'}\n"
        f"watch_interval: {interval}\n{extra}\n"
    )


def _estate(tmp_path: Path, text: str, existence: str) -> Path:
    directory = tmp_path / "estate"
    semantics = f'semantics = {{fw-existence = "{existence}"}}\n'
    write_config(directory, estate=semantics if existence != "stable" else "")
    (directory / "jobs.jil").write_text(text)
    (directory / "non-idempotent.toml").write_text("")
    return directory


def _adapters(existence: str) -> dict[str, Any]:
    return {"CMD": FakeAdapter(default=None), "FW": FileWatcherAdapter(existence=existence)}  # type: ignore[arg-type]


def _start(tmp_path: Path, text: str, existence: str) -> Engine:
    estate = prepare_estate(_estate(tmp_path, text, existence))
    return start_run(
        estate.catalog,
        tmp_path / "run",
        source_capture=main.encoded_parts(estate),
        launched=main.profile_of(estate),
        clock=VirtualClock(start=T0),
        adapters=_adapters(existence),
        settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
    )


async def _resume(tmp_path: Path, at: datetime, existence: str) -> Engine:
    return await resume_run(
        prepare_estate(tmp_path / "estate").catalog,
        tmp_path / "run",
        clock=VirtualClock(start=at),
        adapters=_adapters(existence),
        settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
    )


def _launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, str | None, Any]]:
    """Every `Engine._launch` from now on: job, run, run id and the watch interval it runs."""
    launches: list[tuple[str, int, str | None, Any]] = []
    real = Engine._launch

    def counted(self: Engine, job_ir: Any, run_number: int, adapter: Any, **kwargs: Any) -> None:
        interval = getattr(job_ir.exec_, "watch_interval", None)
        launches.append((job_ir.name, run_number, kwargs.get("run_id"), interval))
        real(self, job_ir, run_number, adapter, **kwargs)

    monkeypatch.setattr(Engine, "_launch", counted)
    return launches


def _polls(tmp_path: Path, run: int = 1) -> int:
    """The lines of `w`'s watch log: the start line and one per poll."""
    log = tmp_path / "run" / "runs" / f"w.{run}" / WATCH_LOG
    return len(log.read_bytes().splitlines()) if log.exists() else 0


def _at_application(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """What holds when `config applied` is journaled: the polls of `w` so far."""
    seen: dict[str, Any] = {}
    journal_applied = EngineUpdate.journal_applied

    def probed(self: EngineUpdate, application: Any) -> Any:
        seen["polls"] = _polls(tmp_path)
        return journal_applied(self, application)

    monkeypatch.setattr(EngineUpdate, "journal_applied", probed)
    return seen


def _journal(tmp_path: Path) -> list[tuple[str, dict[str, Any]]]:
    return [(kind, body) for _, kind, body in rows(tmp_path / "run")]


def _applied(journal: list[tuple[str, dict[str, Any]]]) -> tuple[int, dict[str, Any]]:
    """The index and body of the update's `config applied` (the start's has no token)."""
    [found] = [
        (i, b)
        for i, (k, b) in enumerate(journal)
        if (k, b.get("event")) == ("config", "applied") and "token" in b
    ]
    return found


def _of(journal: list[tuple[str, dict[str, Any]]], attempt: str) -> list[tuple[int, str, str]]:
    """The outcome and plan records of `attempt`: index, kind and outcome or plan."""
    return [
        (i, k, str(b.get("outcome") if k == "outcome" else b.get("plan")))
        for i, (k, b) in enumerate(journal)
        if b.get("attempt") == attempt
        and (k == "outcome" or (k == "follow_up" and b.get("event") == "plan"))
    ]


def _dispatches(journal: list[tuple[str, dict[str, Any]]], job: str) -> list[tuple[int, int, int]]:
    """Each `dispatch` of `job`: index, run number and rev."""
    return [
        (i, b["run_number"], b["rev"])
        for i, (k, b) in enumerate(journal)
        if k == "dispatch" and b["job"] == job
    ]


def _line(at: datetime, run_id: str, *, size: int | None, stable: int) -> dict[str, Any]:
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": at,
        "exists": size is not None,
        "kind": "poll",
        "qualifying": size is not None,
        "run_id": run_id,
        "size": size,
        "stable_polls": stable,
    }


def _rewrite(tmp_path: Path, run_id: str | None, polls: list[dict[str, Any]]) -> None:
    """`w`'s watch log replaced by a start line naming `run_id` and `polls`."""
    log = tmp_path / "run" / "runs" / "w.1" / WATCH_LOG
    log.unlink()
    start = {"artifact_format_version": ARTIFACT_FORMAT_VERSION, "at": T0, "kind": "start"}
    append_watch_line(log, {**start, "run_id": run_id})
    for line in polls:
        append_watch_line(log, line)


async def _admitted_then_killed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existence: str = "stable",
    unnamed: bool = False,
    during: tuple[Event, ...] = (),
) -> dict[str, str]:
    """`j` and `w` run (and `v`, a watch the update does not name, with `unnamed`); an update
    that changes `j` and `w` is admitted, `during` is fed while it waits for `j`'s stop, and
    the engine dies. Answers each job's attempt id."""
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    extra = _watch(tmp_path, "v") if unnamed else ""
    engine = _start(tmp_path, _jil({"j": "sleep 30"}) + _watch(tmp_path) + extra, existence)
    jobs = ("j", "w", "v") if unnamed else ("j", "w")
    for job in jobs:
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
    await engine.run_until_quiescent(T0)
    attempt, _wrapper = _observed(engine, "j", PGID)
    assert engine.journal is not None
    attempts = {o.job: a for a, o in engine.journal.open.attempts.items()}
    assert attempts["j"] == attempt
    changed = _jil({"j": "sleep 31"}) + _watch(tmp_path, interval=NEW_S) + extra
    _estate(tmp_path, changed, existence)
    answer = await _request(engine, _plan(engine, tmp_path))
    assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("j", "w")
    for event in during:
        engine.inject(event)
    await engine.run_until_quiescent(T0)
    await _started(ladders, PGID)
    assert engine.update.stopping and engine.config_rev == 1
    await _crash(engine)
    return attempts


async def _restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: dict[str, str],
    at: datetime,
    *,
    apply: bool,
    existence: str = "stable",
) -> list[object]:
    """Restart at `at`; once the loop has taken its first look at the update, either end `j`'s
    stop sequence and let the update apply, or kill the engine again. Answers what held after
    the first look and, with `apply`, after the application."""
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    _steps, first = _update_steps(monkeypatch)
    seen: list[object] = []
    engine = await _resume(tmp_path, at, existence)
    loop = asyncio.ensure_future(engine.run_until_quiescent(at))
    try:
        async with asyncio.timeout(30):  # a guard against a hang only
            await first.wait()
            seen.append((engine.update.stopping, engine.config_rev, sorted(engine.live_jobs())))
            if not apply:
                loop.cancel()
                await _crash(engine)
                return seen
            await ladders.running(PGID)
            await ladders.end(engine, attempts["j"], PGID)
            await loop
        await engine.run_until_quiescent(at)
        seen.append((engine.update.stopping, engine.config_rev, sorted(engine.live_jobs())))
    finally:
        if apply:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)
    return seen


# ---------------------------------------------------------------- a restart while the update waits


def test_a_held_watch_is_not_launched_across_restarts_and_is_taken_up_once_after_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-526, DL-432: the engine dies while an update that changes `j` and `w` waits for `j`'s
    stop, and restarts twice while it still waits. No restart launches `w` or lets it poll
    before the application. After it, `w` is taken up once, under the applied definition, with
    its original run and attempt id and no second `dispatch` record; `config applied` names it
    `rerun`."""
    _Procs(monkeypatch)
    attempts = asyncio.run(_admitted_then_killed(tmp_path, monkeypatch))
    polls = _polls(tmp_path)
    launches = _launches(monkeypatch)
    probe = _at_application(monkeypatch, tmp_path)
    first = T0 + timedelta(minutes=2)
    second = T0 + timedelta(minutes=4)
    seen = asyncio.run(_restart(tmp_path, monkeypatch, attempts, first, apply=False))
    seen.append(list(launches))
    seen += asyncio.run(_restart(tmp_path, monkeypatch, attempts, second, apply=True))
    assert seen == [(True, 1, []), [], (True, 1, []), (False, 2, ["j", "w"])]
    assert probe["polls"] == polls
    assert [launch for launch in launches if launch[0] == "w"] == [("w", 1, attempts["w"], NEW_S)]
    journal = _journal(tmp_path)
    _at, applied = _applied(journal)
    assert ["w", "rerun"] in applied["stopped"]
    assert [d[1:] for d in _dispatches(journal, "w")] == [(1, 1)]
    assert _of(journal, attempts["w"]) == []


def _box(tmp_path: Path, *, added: bool) -> str:
    """Box `b` with the command `c` and the watch `w`; with `added`, also the command `n`."""
    members = {
        "c": "job_type: c\ncommand: sleep 30\n",
        "w": f"job_type: f\nwatch_file: {tmp_path / 'never'}\nwatch_interval: {OLD_S}\n",
    }
    if added:
        members["n"] = "job_type: c\ncommand: true\n"
    jobs = "".join(f"insert_job: {job}\n{body}box_name: b\n\n" for job, body in members.items())
    return "insert_job: b\njob_type: b\n\n" + jobs


@pytest.mark.parametrize("path", ["live", "restart"])
def test_an_unchanged_watch_of_a_restarted_box_is_not_launched_while_the_update_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """DL-526, DL-463, DL-525: an update that adds a member to the running box `b` restarts it.
    It stops `c` and `w`, the members with a process, though the plan leaves both unchanged. It
    waits for `c`'s stop. On the live path, and after the engine dies and restarts during that
    wait, `w` neither launches nor polls before the application. After it, the box's restart
    ends run 1 and opens run 2: run 1 of `w` is closed once with no plan, `config applied` names
    it `none`, and run 2 is dispatched and launched once under the applied revision.

    On the restart path the test narrows the recorded intent's `filter` by hand to the plan's
    added, changed and removed jobs, the filter an earlier build wrote (DL-491, DL-527). The
    stop list still names `w`'s attempt, so the restart holds the watch through it alone."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    probe = _at_application(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    async def first() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        engine = _start(tmp_path, _box(tmp_path, added=False), "stable")
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "b"}))
        await engine.run_until_quiescent(T0)
        attempt, wrapper = _observed(engine, "c", PGID)
        assert engine.journal is not None and wrapper is not None
        [seen["w"]] = [a for a, o in engine.journal.open.attempts.items() if o.job == "w"]
        seen["c"] = attempt
        _estate(tmp_path, _box(tmp_path, added=True), "stable")
        answer = await _request(engine, _plan(engine, tmp_path))
        decision = answer.decision
        assert isinstance(decision, Admitted)
        assert "w" in decision.stop_set
        await _started(ladders, PGID)
        seen["polls"] = _polls(tmp_path)
        del launches[:]
        if path == "restart":
            await _crash(engine)
            return
        try:
            engine.attempts.status_recorded(attempt, SIGNALED)
            wrapper.cancel()
            await engine.run_until_quiescent(T0)
            assert engine.update.stopping
            async with asyncio.timeout(30):  # a guard against a hang only
                await ladders.end(engine, attempt, PGID)
            await engine.run_until_quiescent(T0)
            assert not engine.update.stopping and engine.config_rev == 2
        finally:
            _teardown(engine, ladders)
            await engine.shutdown()
            _close(engine)

    asyncio.run(first())
    if path == "restart":
        [(seq, intent)] = [
            (seq, body)
            for seq, kind, body in rows(tmp_path / "run")
            if (kind, body.get("event")) == ("config", "intent")
        ]
        assert "w" in intent["filter"]
        rewrite(tmp_path / "run", seq, {**intent, "filter": ["n"]})
        at = T0 + timedelta(minutes=2)
        restarted = asyncio.run(_restart(tmp_path, monkeypatch, {"j": seen["c"]}, at, apply=True))
        assert restarted[0] == (True, 1, [])
        assert restarted[1][:2] == (False, 2)
    assert probe["polls"] == seen["polls"]
    watch = seen["w"]
    [(job, run, run_id, interval)] = [launch for launch in launches if launch[0] == "w"]
    assert (job, run, interval) == ("w", 2, OLD_S) and run_id != watch
    journal = _journal(tmp_path)
    applied, body = _applied(journal)
    assert ["w", "none"] in body["stopped"]
    ended = _of(journal, watch)
    assert [(kind, what) for _i, kind, what in ended] == [
        ("outcome", "stopped"),
        ("follow_up", "none"),
    ]
    assert all(i > applied for i, _kind, _what in ended)
    dispatched = _dispatches(journal, "w")
    assert [d[1:] for d in dispatched] == [(1, 1), (2, 2)] and dispatched[1][0] > applied


class _Died(Exception):
    """The engine dies inside the loop."""


def test_a_watch_only_the_filter_holds_is_held_after_a_restart_and_taken_up_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-526, DL-235: on the real clock, a KILLJOB of `w`, an update that changes `j` and `w`,
    and an operator's RUNNING for `w`'s run are taken in one turn of the loop. The KILLJOB
    takes `w`'s task from the live set and cancels it, and nothing is reaped before the next
    input: the admission finds `w`'s row TERMINATED and no process of `w`, so its stop list
    names `j` alone while its dispatch filter holds `w`. The RUNNING makes the same run live
    again, and the engine dies before the cancelled task is reaped, so `w`'s attempt is still
    open. The restart holds `w` while the update waits; after the application `w` is taken up
    once, under its run id and the applied definition, and `config applied` names no entry for
    it."""
    from test_update_wiring import TIMEOUT_S

    from dsl41.runner_clock import RealClock

    _Procs(monkeypatch)
    seen: dict[str, Any] = {}
    real = Engine._admit_and_apply

    async def dying(self: Engine, pending: Any) -> Any:
        out = await real(self, pending)
        ev = pending.ev
        if ev is not None and ev.kind == "STATUS" and ev.payload.get("job") == "w":
            seen["unreaped"] = ("w" not in self._live, len(self._killed), len(self._reaping))
            raise _Died
        return out

    def engine_of(clock: Any) -> dict[str, Any]:
        return {
            "clock": clock,
            "adapters": _adapters("stable"),
            "settings": EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
            "hold_open": True,
        }

    async def first() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        text = _jil({"j": "sleep 30"}) + _watch(tmp_path)
        estate = prepare_estate(_estate(tmp_path, text, "stable"))
        engine = start_run(
            estate.catalog,
            tmp_path / "run",
            source_capture=main.encoded_parts(estate),
            launched=main.profile_of(estate),
            **engine_of(RealClock()),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            async with asyncio.timeout(TIMEOUT_S):  # a guard against a hang only
                for job in ("j", "w"):
                    engine.inject(
                        Event(at=engine.now_present(), kind="FORCE_STARTJOB", payload={"job": job})
                    )
                while not ({"j", "w"} <= engine.live_jobs() and _polls(tmp_path) >= 2):
                    await asyncio.sleep(0.01)
            attempt, _wrapper = _observed(engine, "j", PGID)
            assert engine.journal is not None
            [seen["w"]] = [a for a, o in engine.journal.open.attempts.items() if o.job == "w"]
            seen["j"] = attempt
            changed = _jil({"j": "sleep 31"}) + _watch(tmp_path, interval=NEW_S)
            _estate(tmp_path, changed, "stable")
            token = _plan(engine, tmp_path)
            seen["polls"] = _polls(tmp_path)
            monkeypatch.setattr(Engine, "_admit_and_apply", dying)
            # one turn of the loop takes the three, in this order
            engine.inject(Event(at=engine.now_present(), kind="KILLJOB", payload={"job": "w"}))
            engine.request_update(UpdateRequest(token, 1, engine.baseline_id, "u1"))
            running = {"job": "w", "status": "RUNNING"}
            engine.inject(Event(at=engine.now_present(), kind="STATUS", payload=running))
            with pytest.raises(_Died):
                async with asyncio.timeout(TIMEOUT_S):  # a guard against a hang only
                    await loop
        finally:
            monkeypatch.setattr(Engine, "_admit_and_apply", real)
            for task in engine.attempts.running_ladders():
                task.cancel()
            await engine.shutdown()
            _close(engine)

    asyncio.run(first())
    assert seen["unreaped"] == (True, 1, 1)
    journal = _journal(tmp_path)
    [intent] = [b for k, b in journal if (k, b.get("event")) == ("config", "intent")]
    assert [entry["job"] for entry in intent["stop"]] == ["j"] and "w" in intent["filter"]
    assert _of(journal, seen["w"]) == []
    launches = _launches(monkeypatch)
    probe = _at_application(monkeypatch, tmp_path)

    async def restart() -> None:
        ladders = _Ladders(monkeypatch)
        ladders.alive.add(PGID)
        _steps, looked = _update_steps(monkeypatch)
        engine = await resume_run(
            prepare_estate(tmp_path / "estate").catalog,
            tmp_path / "run",
            **engine_of(RealClock()),
        )
        assert engine.update._answer is not None
        applied = engine.update._answer.applied
        assert applied is not None
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            async with asyncio.timeout(TIMEOUT_S):  # a guard against a hang only
                await looked.wait()
                seen["looked"] = (engine.update.stopping, sorted(engine.live_jobs()))
                seen["launches"] = list(launches)
                await ladders.running(PGID)
                await ladders.end(engine, seen["j"], PGID)
                await applied
            live = engine._live.get("w")
            seen["live"] = None if live is None else (live.run_number, live.attempt)
        finally:
            _teardown(engine, ladders)
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
            await engine.shutdown()
            _close(engine)

    asyncio.run(restart())
    assert seen["looked"] == (True, []) and seen["launches"] == []
    assert probe["polls"] == seen["polls"]
    assert [launch for launch in launches if launch[0] == "w"] == [("w", 1, seen["w"], NEW_S)]
    assert seen["live"] == (1, seen["w"])
    journal = _journal(tmp_path)
    _at, body = _applied(journal)
    assert [entry for entry in body["stopped"] if entry[0] == "w"] == []
    assert [d[1:] for d in _dispatches(journal, "w")] == [(1, 1)]
    assert _of(journal, seen["w"]) == []


@pytest.mark.parametrize(
    "case", ["complete", "immediate-first-poll", "run-ended", "foreign-log", "unreadable-log"]
)
def test_a_restart_classifies_a_named_watchs_evidence_before_it_holds_the_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-526, PR-34a, DL-258, DL-242, DL-118: a restart reads a named watch's evidence before
    the update can hold it. A complete log, or a qualifying first poll under `fw-existence=
    immediate`, is the watch's outcome: its outcome, its plan and its feed precede `config
    applied`. A run that ended is closed with no plan, and a log that does not read as the
    attempt's is quarantined; both before the application. None of them launches `w`.

    Every log here but `run-ended`'s is written by hand (DL-491). The admission cancels the
    watch in the step that journals the intent, so no poll follows it: the complete and the
    immediate logs arise only from a root an earlier build wrote. The foreign and the unreadable
    logs stand for disk corruption."""
    existence = "immediate" if case == "immediate-first-poll" else "stable"
    killjob = (Event(at=T0, kind="KILLJOB", payload={"job": "w"}),)
    during = killjob if case == "run-ended" else ()
    _Procs(monkeypatch)
    attempts = asyncio.run(
        _admitted_then_killed(tmp_path, monkeypatch, existence=existence, during=during)
    )
    watch = attempts["w"]
    later = T0 + timedelta(seconds=30)
    if case == "complete":
        _rewrite(
            tmp_path,
            watch,
            [_line(T0, watch, size=None, stable=0), _line(later, watch, size=6, stable=1)]
            + [_line(later + timedelta(seconds=1), watch, size=6, stable=2)],
        )
    elif case == "immediate-first-poll":
        _rewrite(tmp_path, watch, [_line(T0, watch, size=6, stable=1)])
    elif case == "foreign-log":
        _rewrite(tmp_path, "someone-else", [])
    elif case == "unreadable-log":
        log = tmp_path / "run" / "runs" / "w.1" / WATCH_LOG
        log.write_bytes(b'{"kind": "poll"}\n')
    launches = _launches(monkeypatch)
    at = T0 + timedelta(minutes=2)
    seen = asyncio.run(
        _restart(tmp_path, monkeypatch, attempts, at, apply=True, existence=existence)
    )
    assert seen == [(True, 1, []), (False, 2, ["j"])]
    assert [launch for launch in launches if launch[0] == "w"] == []
    journal = _journal(tmp_path)
    applied, body = _applied(journal)
    assert ["w", "none"] in body["stopped"]
    ended = _of(journal, watch)
    expected = {
        "complete": ("natural", "complete"),
        "immediate-first-poll": ("natural", "complete"),
        "run-ended": ("stopped", "none"),
        "foreign-log": ("unobservable", "hold"),
        "unreadable-log": ("unobservable", "hold"),
    }[case]
    assert [(kind, what) for _i, kind, what in ended] == [
        ("outcome", expected[0]),
        ("follow_up", expected[1]),
    ]
    assert all(i < applied for i, _kind, _what in ended)
    if expected[1] == "complete":
        [feed] = [
            i
            for i, (k, b) in enumerate(journal)
            if k == "input" and b.get("kind") == "STATUS" and b["payload"]["job"] == "w"
        ]
        assert ended[-1][0] < feed < applied


def test_a_watch_of_a_job_the_update_does_not_name_is_resumed_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-526, DL-522: only named jobs wait. `v`, a running watch the update does not name, is taken up
    by the restart's catch-up under its own attempt id, before the application."""
    _Procs(monkeypatch)
    attempts = asyncio.run(_admitted_then_killed(tmp_path, monkeypatch, unnamed=True))
    launches = _launches(monkeypatch)
    at = T0 + timedelta(minutes=2)
    seen = asyncio.run(_restart(tmp_path, monkeypatch, attempts, at, apply=False))
    [(stopping, rev, live)] = seen
    assert stopping and rev == 1 and "v" in live
    assert [launch[:3] for launch in launches if launch[0] == "v"] == [("v", 1, attempts["v"])]


# ---------------------------------------------------------------- the application takes a watch up


async def _live_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    launches: list[tuple[str, int, str | None, Any]],
) -> dict[str, Any]:
    """`j` and `w` run; an update that changes `j` and changes, removes or retypes `w` waits
    for `j`'s stop. With `deadline`, `w`'s `term_run_time` falls at the instant `j`'s stop ends;
    with `newer-run`, `w` is killed and started again while the update waits. Answers `w`'s
    attempt id, its row and the live jobs after the application, and the launches of `w` after
    the admission."""
    ladders = _Ladders(monkeypatch)
    ladders.alive.add(PGID)
    limit = "term_run_time: 1\n" if case == "deadline" else ""
    engine = _start(tmp_path, _jil({"j": "sleep 30"}) + _watch(tmp_path, extra=limit), "stable")
    seen: dict[str, Any] = {}
    try:
        for job in ("j", "w"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        attempt, wrapper = _observed(engine, "j", PGID)
        assert engine.journal is not None and wrapper is not None
        [seen["w"]] = [a for a, o in engine.journal.open.attempts.items() if o.job == "w"]
        new = {
            "deadline": _watch(tmp_path, interval=NEW_S, extra=limit),
            "current": _watch(tmp_path, interval=NEW_S),
            "newer-run": _watch(tmp_path, interval=NEW_S),
            "removed": "",
            "retyped": "insert_job: w\njob_type: c\ncommand: true\n\n",
        }[case]
        _estate(tmp_path, _jil({"j": "sleep 31"}) + new, "stable")
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("j", "w")
        await _started(ladders, PGID)
        del launches[:]
        if case == "newer-run":
            for kind in ("KILLJOB", "FORCE_STARTJOB"):
                engine.inject(Event(at=T0, kind=kind, payload={"job": "w"}))
        engine.attempts.status_recorded(attempt, SIGNALED)
        wrapper.cancel()
        await engine.run_until_quiescent(T0)
        assert engine.update.stopping
        if case == "deadline":
            due = engine.oracle.next_timer_due()
            assert due is not None and due > T0
            await engine.clock.wait_until(due)  # the stop ends at the deadline's instant
            seen["due"] = engine.oracle.store.job["w"].status
        async with asyncio.timeout(30):  # a guard against a hang only
            await ladders.end(engine, attempt, PGID)
        await engine.run_until_quiescent(engine.clock.now())
        assert not engine.update.stopping and engine.config_rev == 2
        row = engine.oracle.store.job.get("w")
        seen["row"] = None if row is None else (row.status, row.run_number)
        live = engine._live.get("w")
        seen["live"] = None if live is None else (live.run_number, live.attempt)
        seen["launches"] = [launch for launch in launches if launch[0] == "w"]
    finally:
        _teardown(engine, ladders)
        await engine.shutdown()
        _close(engine)
    return seen


def test_a_watch_whose_run_the_applications_time_advance_ends_is_closed_once_and_not_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-526, DL-432: `w`'s `term_run_time` deadline falls at the instant the update applies.
    The record chooses `rerun` while `w`'s run is live; the time advance after it ends the run.
    The watch is then closed once with no plan and not launched."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    seen = asyncio.run(_live_update(tmp_path, monkeypatch, "deadline", launches))
    assert seen["due"] == "RUNNING"
    assert seen["row"] == ("TERMINATED", 1) and seen["live"] is None
    assert seen["launches"] == []
    journal = _journal(tmp_path)
    applied, body = _applied(journal)
    assert ["w", "rerun"] in body["stopped"]
    ended = _of(journal, seen["w"])
    assert [(kind, what) for _i, kind, what in ended] == [
        ("outcome", "stopped"),
        ("follow_up", "none"),
    ]
    assert all(i > applied for i, _kind, _what in ended)


@pytest.mark.parametrize("case", ["current", "removed", "retyped", "newer-run"])
def test_after_the_application_a_cancelled_watch_is_taken_up_only_while_its_run_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-526, DL-432: a changed watch whose run is still current is taken up once, under its
    run id and the applied definition, with no outcome of its own. A removed or retyped watch is
    closed once with no plan and not launched. A watch killed and started again while the
    update waits is closed, and its newer run is dispatched once, after the application, under
    the applied revision, and keeps its own task."""
    _Procs(monkeypatch)
    launches = _launches(monkeypatch)
    seen = asyncio.run(_live_update(tmp_path, monkeypatch, case, launches))
    journal = _journal(tmp_path)
    applied, body = _applied(journal)
    watch = seen["w"]
    ended = [(kind, what) for _i, kind, what in _of(journal, watch)]
    if case == "current":
        assert ["w", "rerun"] in body["stopped"]
        assert seen["launches"] == [("w", 1, watch, NEW_S)]
        assert seen["live"] == (1, watch) and ended == []
        return
    assert ["w", "none"] in body["stopped"]
    assert ended == [("outcome", "stopped"), ("follow_up", "none")]
    assert all(i > applied for i, _kind, _what in _of(journal, watch))
    if case == "newer-run":
        [(at, run, rev)] = [d for d in _dispatches(journal, "w") if d[1] == 2]
        assert at > applied and (run, rev) == (2, 2)
        assert [launch[:2] for launch in seen["launches"]] == [("w", 2)]
        assert seen["live"] is not None and seen["live"][0] == 2
        assert seen["row"] == ("RUNNING", 2)
    else:
        assert seen["launches"] == [] and seen["live"] is None
