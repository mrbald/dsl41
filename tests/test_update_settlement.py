"""What an update waits for and what it keeps (spec-reload, DL-427, DL-431): a stop is finished
only when its cleanup is (the ladder done, nothing of it left), not at its record; an
interrupted attempt of a job whose mark the plan changes takes its plan under the applied
marked set; an attempt's first stop cause survives the intent; a changed job's `term_run_time`
keeps counting; a retry is answered from its stored admission while the engine drains; and
`dsl41 runs` reads each run under the configuration it ran under.

The process stand-ins are `test_engine_stops_cookie`'s: no real process is signalled.
"""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from store_surgery import rows
from test_engine_stops_cookie import (
    OTHER_ID,
    PGID,
    RUN_ID,
    SPAWN,
    T0,
    _Procs,
)

from dsl41 import runner_procid
from dsl41.oracle_state import Event
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_journal import decode_snapshot, dispatch_record
from dsl41.runner_startup import start_run
from dsl41.runner_stops import AttemptFacts
from dsl41.runner_update import UpdateRequest

ANCHOR = "insert_job: anchor\njob_type: c\ncommand: true\n\n"
SIGNALED = {"version": 1, "outcome": "signaled", "signal": 9}


def _jil(jobs: dict[str, str], extra: dict[str, str] | None = None) -> str:
    extra = extra or {}
    return ANCHOR + "".join(
        f"insert_job: {job}\njob_type: c\ncommand: {cmd}\ncondition: s(anchor)\n"
        f"{extra.get(job, '')}\n"
        for job, cmd in jobs.items()
    )


def _estate(tmp_path: Path, text: str, marked: dict[str, str] | None = None) -> Path:
    directory = tmp_path / "estate"
    directory.mkdir(exist_ok=True)
    (directory / "estate.toml").write_text('name = "t"\nfiles = ["jobs.jil"]\n')
    (directory / "jobs.jil").write_text(text)
    lines = "".join(f'"{job}" = "{why}"\n' for job, why in (marked or {}).items())
    (directory / "non-idempotent.toml").write_text(f"[jobs]\n{lines}" if lines else "")
    return directory


def _engine(tmp_path: Path, text: str, **adapter: Any) -> Engine:
    from dsl41.reload_plan import load_estate

    return start_run(
        load_estate(_estate(tmp_path, text)).catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(**({"default": None} | adapter))},
        settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
    )


def _adopt(engine: Engine, job: str, attempt: str, pgid: int) -> None:
    """An observed attempt of `job`'s run 1, as the cookie tests adopt one, with its dispatch
    record, so the open table holds it."""
    env = {"DSL41_COOKIE": runner_procid.cookie("inst", attempt)}
    assert engine.journal is not None
    engine.journal.append(
        dispatch_record(
            job, 1, attempt=attempt, spool="/nonexistent", started_at=T0, rev=1, ordinal=1, env=env
        )
    )
    engine.adopt_attempt(AttemptFacts(attempt, job, 1, 1, Path("/nonexistent"), env))
    engine.stops.scan_available = True
    spawn = {**SPAWN, "run_id": attempt, "job": job, "command_pgid": pgid, "command_pid": pgid}
    engine.stops.note_boot(spawn)
    engine.attempts.spawn_recorded(attempt, spawn)


async def _request(engine: Engine, token: str) -> Any:
    answer = engine.request_update(UpdateRequest(token, True, 1, engine.baseline_id, "u1"))
    await engine.run_until_quiescent(engine.clock.now())
    return answer.result()


def _plan(engine: Engine, tmp_path: Path) -> str:
    assert engine.run_root is not None
    return reread(tmp_path / "estate", engine.update.live(), engine.run_root / "plans").token


def _close(engine: Engine) -> None:
    assert engine.journal is not None
    engine.journal.close()


def _kinds(tmp_path: Path, kind: str) -> list[dict[str, Any]]:
    return [body for _, k, body in rows(tmp_path / "run") if k == kind]


@pytest.mark.parametrize("case", ["record-mid-ladder", "record-after-the-kill-with-a-survivor"])
def test_the_update_applies_only_once_each_stop_is_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """DL-431: a stop is finished when its ladder is done and a reading finds nothing of it
    left. A record that lands while the ladder runs, or after the KILL with a survivor the
    held poll then KILLs, leaves the update STOPPING until that is so; only then does it
    apply."""
    box = _Procs(monkeypatch, p4194311=(PGID, True, True), p51=(PGID, False, True))
    box.tokens[PGID] = "t"
    if case == "record-after-the-kill-with-a-survivor":

        def fork_after_the_kill(pid: int, sig: int) -> None:
            if (pid, sig) == (PGID, signal.SIGKILL):
                box.procs[77] = (PGID, True, True)

        box.on_signal = fork_after_the_kill
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, "j", RUN_ID, PGID)
        _estate(tmp_path, _jil({"j": "sleep 31"}))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted)
        if case == "record-mid-ladder":
            await asyncio.sleep(0)  # the ladder has TERMed and waits out its grace
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            seen.append(engine.update.stopping)
            await engine.attempts.join()
        else:
            await engine.attempts.join()
            engine.attempts.status_recorded(RUN_ID, SIGNALED)
            await engine.attempts.join()
            seen.append(engine.update.stopping)
            engine.stops._look()  # KILLs the survivor
            seen.append(engine.update.stopping)
            engine.stops._look()  # nothing left: the stop is finished
        seen.append(engine.update.stopping)  # the end is a queued step of the single writer
        await engine.run_until_quiescent(engine.clock.now())
        seen.append(engine.update.stopping)
        seen.append(engine.config_rev)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    stopping = [True] if case == "record-mid-ladder" else [True, True]
    assert seen == [*stopping, True, False, 2]
    assert box.procs == {}
    order = [(k, b.get("event"), b.get("job")) for _, k, b in rows(tmp_path / "run")]
    assert order.index(("outcome", None, "j")) > order.index(("config", "applied", None), 1)


def test_an_interruption_of_a_newly_marked_job_waits_for_the_applied_marked_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-wiring 2b step 1: while the update stops `j`, `b`, which the plan marks
    non-idempotent and leaves running, loses its wrapper. Its plan is chosen after the
    application, under the new marked set: one interrupted hold, no second dispatch. The update
    neither stops `b` nor waits for it."""
    other = PGID + 1
    _Procs(monkeypatch, p4194311=(PGID, True, False), p4194312=(other, True, False))
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "b": "sleep 30"}))
        for job in ("j", "b"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, "j", RUN_ID, PGID)
        _adopt(engine, "b", OTHER_ID, other)
        _estate(tmp_path, _jil({"j": "sleep 31", "b": "sleep 30"}), {"b": "posts twice"})
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted) and answer.decision.stop_set == ("j",)
        engine.attempts.stop(OTHER_ID, "wrapper_lost")
        engine.attempts.status_recorded(OTHER_ID, SIGNALED)
        await engine.attempts.join()
        seen.append([p["job"] for p in _kinds(tmp_path, "follow_up") if p["event"] == "plan"])
        seen.append(engine.update.stopping)
        engine.attempts.status_recorded(RUN_ID, SIGNALED)
        await engine.attempts.join()
        await engine.run_until_quiescent(engine.clock.now())
        seen.append(engine.oracle.store.job["b"].on_hold)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    assert seen == [[], True, True]
    plans: dict[str, str] = {}
    for body in _kinds(tmp_path, "follow_up"):
        if body["event"] == "plan":  # the first plan of each: the stand-in tasks close later
            plans.setdefault(body["job"], body["plan"])
    assert plans == {"j": "rerun", "b": "hold"}
    assert [d["ordinal"] for d in _kinds(tmp_path, "dispatch") if d["job"] == "b"] == [1, 1]
    [applied] = [c for c in _kinds(tmp_path, "config") if c["event"] == "applied" and "token" in c]
    assert applied["stopped"] == [["j", "rerun"]]  # `b` is not the update's stop


def test_an_attempt_already_stopping_keeps_its_first_cause_through_intent_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intent records the cause of the stop an attempt already takes; `update_stop` only
    for one the update stops first. The open table and the snapshot a restart reads keep it."""
    _Procs(monkeypatch, p4194311=(PGID, True, True))
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, "j", RUN_ID, PGID)
        engine.attempts.stop(RUN_ID, "wrapper_lost")
        _estate(tmp_path, _jil({"j": "sleep 31"}))
        await _request(engine, _plan(engine, tmp_path))
        assert engine.journal is not None
        seen.append(engine.journal.open.attempts[RUN_ID].stop)
        seq = engine.take_snapshot()
        stored = engine.journal.store.latest_snapshot()
        assert stored is not None and stored.seq == seq
        seen.append(decode_snapshot(seq, stored.data, where="t").open.attempts[RUN_ID].stop)
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    [intent] = [c for c in _kinds(tmp_path, "config") if c["event"] == "intent"]
    assert [entry["cause"] for entry in intent["stop"]] == ["wrapper_lost"]
    assert seen == ["wrapper_lost", "wrapper_lost"]


def test_a_changed_jobs_term_run_time_keeps_counting_across_the_update(tmp_path: Path) -> None:
    """Spec-reload: the oracle's timers (`term_run_time`, run windows) are the oracle's and an
    update leaves them; the scheduler's plans are what it rebuilds. A running job whose
    `term_run_time` the update raises is still ended by the deadline its run armed."""
    seen: list[str] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "x"}, {"j": "term_run_time: 1\n"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _estate(tmp_path, _jil({"j": "y"}, {"j": "term_run_time: 10\n"}))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted) and engine.config_rev == 2
        seen.append(engine.oracle.store.job["j"].status)
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        seen.append(engine.oracle.store.job["j"].status)
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    assert seen == ["RUNNING", "TERMINATED"]


@pytest.mark.parametrize("when", ["applied", "stopping"])
def test_a_retry_is_answered_from_its_admission_while_the_engine_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """The request id comes before the drain: an exact retry of an update admitted before
    `stop_requested` is answered with its admission (and its application, once done); a new
    update is refused `engine_draining`."""
    _Procs(monkeypatch, p4194311=(PGID, True, True))
    seen: list[Any] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "k": "x"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, "j", RUN_ID, PGID)
        jobs = {"j": "sleep 31"} if when == "stopping" else {"j": "sleep 30", "k": "y"}
        _estate(tmp_path, _jil(jobs))
        token = _plan(engine, tmp_path)
        request = UpdateRequest(token, True, 1, engine.baseline_id, "u1")
        first = engine.request_update(request)
        await engine.run_until_quiescent(engine.clock.now())
        stop = engine.request_stop()
        retry = engine.request_update(request)
        fresh = engine.request_update(UpdateRequest(token, True, 1, engine.baseline_id, "u2"))
        await engine.run_until_quiescent(engine.clock.now())
        seen.extend([first.result(), retry.result(), fresh.result(), stop.result()])
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    first, retry, fresh, _state = seen
    assert retry.decision == first.decision and isinstance(retry.decision, Admitted)
    assert retry.code is None
    if when == "applied":
        assert retry.application is not None and retry.application["rev"] == 2
    assert fresh.code == "engine_draining"


def test_runs_reads_each_run_under_the_configuration_it_ran_under(tmp_path: Path) -> None:
    """`dsl41 runs` after an update that changes a job's exit-code semantics, removes a job,
    adds one and retypes one: each run replays and is reported under its own rev, with that
    rev's catalog hash, job fingerprint and verdict; after a prune, from the snapshot's."""
    from dsl41.runner_history import read_run_root

    script = {("a", 1): (0.0, 1), ("a", 2): (0.0, 1), ("a", 3): (0.0, 1)}
    first = _jil({"a": "x", "x": "x", "r": "x"})
    box = "insert_job: r\njob_type: b\ncondition: s(anchor)\n\n"
    second = _jil({"a": "x", "y": "x"}, {"a": "max_exit_success: 1\n"}) + box
    run_root = tmp_path / "run"
    hashes: list[str] = []
    found: list[dict[tuple[str, int], Any]] = []

    def start(engine: Engine, *jobs: str) -> None:
        for job in jobs:
            engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))

    async def scenario() -> None:
        engine = _engine(tmp_path, first, script=script, default=(0.0, 0))
        start(engine, "a", "x", "r")
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        _estate(tmp_path, second)
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted)
        start(engine, "a", "y", "r")
        await engine.run_until_quiescent(engine.clock.now() + timedelta(seconds=1))
        hashes.extend(
            c["catalog_hash"] for c in _kinds(tmp_path, "config") if c["event"] == "applied"
        )
        found.append({(row.job, row.run_number): row for row in read_run_root(run_root)})
        engine.take_snapshot()
        assert engine.journal is not None
        engine.journal.store.prune_before(10**6, T0 + timedelta(days=9999))
        start(engine, "a")
        await engine.run_until_quiescent(engine.clock.now() + timedelta(seconds=1))
        found.append({(row.job, row.run_number): row for row in read_run_root(run_root)})
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    one, two = hashes
    before, after = found
    label = {key: (row.status, row.catalog_hash) for key, row in before.items()}
    assert label[("a", 1)] == ("FAILURE", one)  # exit 1 under rev 1's boundary
    assert label[("a", 2)] == ("SUCCESS", two)  # exit 1 under rev 2's
    assert label[("x", 1)] == ("SUCCESS", one)  # a removed job's run
    assert label[("y", 1)] == ("SUCCESS", two)  # an added job's
    assert label[("r", 1)] == ("SUCCESS", one)  # the retyped job's command run
    assert before[("r", 2)].catalog_hash == two  # and its box run
    assert before[("a", 1)].job_hash != before[("a", 2)].job_hash
    # after the prune the replay starts from the snapshot's rev 2
    assert (after[("a", 3)].status, after[("a", 3)].catalog_hash) == ("SUCCESS", two)


# ---------------------------------------------------------------- round 2: one settlement rule


def _park_ladders(
    monkeypatch: pytest.MonkeyPatch, survivor: bool = False
) -> tuple[asyncio.Event, asyncio.Event]:
    """Every stop ladder (and a restart's finish of a cut-short one) waits for `release`;
    `entered` is set once one waits. The test releases it after it has looked. With
    `survivor`, the finish then finds a process left, which the held poll later finds gone."""
    from dsl41 import runner_stops
    from dsl41.runner_stops import NOTHING_LEFT, Left

    entered, release = asyncio.Event(), asyncio.Event()

    async def ladder(_self: Any, _pgid: int, _token: str, _grace: float) -> None:
        entered.set()
        await release.wait()

    async def finish(_self: Any, _attempt: str, _pgid: int, _token: str) -> Any:
        entered.set()
        await release.wait()
        return Left("ours", {42: "ticks:42"}) if survivor else NOTHING_LEFT

    monkeypatch.setattr(runner_stops.EngineStops, "ladder", ladder)
    monkeypatch.setattr(runner_stops.EngineStops, "finish", finish)
    return entered, release


def test_an_attempt_ended_before_the_admission_still_cleaning_holds_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt of a job the plan changes ended before the admission while its cleanup
    ladder still runs: the update counts it stopped only once the ladder is done."""
    _Procs(monkeypatch)
    entered, release = _park_ladders(monkeypatch)
    seen: list[object] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, "j", RUN_ID, PGID)
        engine.attempts.stop(RUN_ID, "wrapper_lost")
        await entered.wait()
        engine.attempts.status_recorded(RUN_ID, SIGNALED)  # ended; its ladder runs on
        _estate(tmp_path, _jil({"j": "sleep 31"}))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted)
        seen.append((engine.update.stopping, engine.config_rev))
        release.set()
        await engine.attempts.join()
        await engine.run_until_quiescent(engine.clock.now())  # the end is a loop step
        seen.append((engine.update.stopping, engine.config_rev))
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    assert seen == [(True, 1), (False, 2)]


def _crashed_update(tmp_path: Path, *, closing: bool, idle: bool = False) -> tuple[Path, str]:
    """A run root whose engine died while an update stopped `j`, with `b` running beside it;
    both attempts' spools hold their records (`j` signalled, `b` exited 0). With `closing`,
    `j`'s outcome was committed while its cleanup ladder ran. With `idle`, the estate also has
    `p`, which the plan changes, and `u`, which it leaves, neither running. Answers the run
    root and `j`'s attempt id."""
    from dsl41.runner import outcome_record, plan_record
    from dsl41.runner_store import Record

    run_root = tmp_path / "run"
    attempts: dict[str, str] = {}

    async def first() -> None:
        quiet = {"p": "x", "u": "x"} if idle else {}
        engine = _engine(tmp_path, _jil({"j": "sleep 30", "b": "sleep 30", **quiet}))
        for job in ("j", "b"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        assert engine.journal is not None
        for job in ("j", "b"):
            attempt = engine.journal.open.attempts
            [run_id] = [a for a, open_ in attempt.items() if open_.job == job]
            attempts[job] = run_id
            spool = Path(str(attempt[run_id].spool))
            spool.mkdir(parents=True, exist_ok=True)
            pgid = PGID if job == "j" else PGID + 1
            spawn = {**SPAWN, "run_id": run_id, "job": job, "command_pgid": pgid}
            spawn |= {"command_pid": pgid, "boot_id": runner_procid.current_boot_id()}
            (spool / "spawn.json").write_text(json.dumps(spawn))
            end = {"outcome": "signaled", "signal": 9} if job == "j" else {"outcome": "exited"}
            status = {"version": 1, "job": job, "run_number": 1, "run_id": run_id, **end}
            (spool / "status.json").write_text(json.dumps({**status, "exit_code": 0}))
        if closing:
            at = engine.clock.now()
            ended = outcome_record(attempts["j"], "j", 1, "stopped", {}, at, cause="wrapper_lost")
            ended = ended.__class__(
                at, "outcome", None, None, {**ended.payload, "ladder": "running"}
            )
            # the crash cut the cleanup short after the outcome and its rerun plan were in
            step = {"step": "dispatch", "attempt": OTHER_ID, "ordinal": 2, "env": {}}
            step |= {"after": "wrapper_lost"}
            engine.journal.append(ended, plan_record(attempts["j"], "j", 1, "rerun", [step], at))
        changed = {"p": "y", "u": "x"} if idle else {}
        _estate(tmp_path, _jil({"j": "sleep 31", "b": "sleep 30", **changed}))
        token = _plan(engine, tmp_path)
        fingerprint = UpdateRequest(token, True, 1, engine.baseline_id, "u1").fingerprint
        intent = {
            "event": "intent",
            "at": engine.clock.now().isoformat(),
            "token": token,
            "live_rev": 1,
            "stop": [
                {"job": "j", "run_number": 1, "attempt": attempts["j"], "cause": "update_stop"}
            ],
            "filter": ["j", "p"] if idle else ["j"],
            "stop_running": True,
            "request_id": "u1",
            "fingerprint": fingerprint,
        }
        engine.journal.append(Record(engine.clock.now(), "config", None, "u1", intent))
        for task in list(engine._live.values()):
            task.task.cancel()
        await engine.shutdown()
        _close(engine)  # the engine dies: no shutdown record

    asyncio.run(first())
    return run_root, attempts["j"]


def test_a_box_run_keeps_the_rev_it_ran_under_at_an_equal_instant_and_after_a_prune(
    tmp_path: Path,
) -> None:
    """A box run is labelled with the rev in force where it opened, in record order: a box
    that ran at the very instant of the update that changed it keeps the old rev, a box the
    update removed keeps its run, a retyped job keeps its box run, and a snapshot's retained
    runs keep their rev through a prune."""
    from dsl41.runner_history import read_run_root

    def boxes(**jobs: str) -> str:
        return ANCHOR + "".join(
            f"insert_job: {job}\njob_type: b\ncondition: s(anchor)\n{extra}\n"
            for job, extra in jobs.items()
        )

    first = boxes(r="", q="", t="")
    # q removed, t retyped to a command
    second = _jil({"t": "x"}) + boxes(r='description: "edited"\n').removeprefix(ANCHOR)
    run_root = tmp_path / "run"
    hashes: list[str] = []
    found: list[dict[tuple[str, int], Any]] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, first, default=(0.0, 0))
        for job in ("r", "q", "t"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        _estate(tmp_path, second)
        answer = await _request(engine, _plan(engine, tmp_path))  # at the same instant
        assert isinstance(answer.decision, Admitted) and engine.clock.now() == T0
        for job in ("r", "t"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        hashes.extend(
            c["catalog_hash"] for c in _kinds(tmp_path, "config") if c["event"] == "applied"
        )
        found.append({(row.job, row.run_number): row for row in read_run_root(run_root)})
        engine.take_snapshot()
        assert engine.journal is not None
        engine.journal.store.prune_before(10**6, T0 + timedelta(days=9999))
        found.append({(row.job, row.run_number): row for row in read_run_root(run_root)})
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    one, two = hashes
    for rows_ in found:
        label = {key: row.catalog_hash for key, row in rows_.items()}
        assert label[("r", 1)] == one and label[("r", 2)] == two
        assert label[("q", 1)] == one  # removed by the update
        assert label[("t", 1)] == one  # the box run of the retyped job
        assert rows_[("r", 1)].job_hash != rows_[("r", 2)].job_hash


def test_the_spec_drift_baseline_names_the_original_paths_of_every_part(tmp_path: Path) -> None:
    """The applied plan's bytes are bound to the paths `reread` read them from: each estate
    file (one the update adds included) in the estate directory, each site layer at its own
    path, never the plan directory's copies, wherever the run root is."""
    from dsl41.reload_plan import Live, load_estate, reread
    from dsl41.runner_update import open_plan

    directory = _estate(tmp_path, _jil({"a": "x"}))
    (directory / "estate.toml").write_text('name = "t"\nfiles = ["jobs.jil", "more.jil"]\n')
    (directory / "more.jil").write_text("insert_job: m\njob_type: c\ncommand: x\ncondition: s(a)\n")
    external = tmp_path / "elsewhere" / "site.properties"
    external.parent.mkdir()
    external.write_text("SITE=one\n")
    (tmp_path / "old").mkdir()
    live = Live(load_estate(_estate(tmp_path / "old", _jil({"a": "x"}))).catalog, {}, 1, {})
    run_root = tmp_path / "run"
    token = reread(directory, live, run_root / "plans", [external]).token
    expected = {
        str(directory.absolute() / name) for name in ("estate.toml", "jobs.jil", "more.jil")
    } | {str(external.absolute())}
    assert set(open_plan(run_root, token).fingerprint()) == expected
    moved = tmp_path / "moved"
    run_root.rename(moved)
    assert set(open_plan(moved, token).fingerprint()) == expected


def test_a_plan_written_before_the_estate_directory_was_recorded_is_stale(
    tmp_path: Path,
) -> None:
    """A plan an older `reread` wrote names no estate directory: refused `stale_plan` before
    anything is written, saying it predates the engine."""
    from dsl41.canon import canonical_bytes, decode

    seen: list[Any] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"a": "x"}))
        _estate(tmp_path, _jil({"a": "y"}))
        token = _plan(engine, tmp_path)
        plan_json = tmp_path / "run" / "plans" / token / "plan.json"
        document = decode(plan_json.read_bytes())
        del document["estate_dir"]
        plan_json.write_bytes(canonical_bytes(document) + b"\n")
        seen.append(await _request(engine, token))
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    [answer] = seen
    assert answer.code == "stale_plan" and "predates this engine" in str(answer.error)
    assert [c["event"] for c in _kinds(tmp_path, "config")] == ["applied"]


@pytest.mark.parametrize("closing", [False, True], ids=["running-ladder", "closing-attempt"])
def test_a_restart_starts_with_the_update_stopping_and_applies_it_as_a_loop_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closing: bool
) -> None:
    """Startup step 4 restores the update and nothing waits for it: the engine starts with the
    update STOPPING and its filter holding while `j`'s stop is not finished (a ladder the
    restart runs, parked; or, for an attempt whose cleanup the crash cut short, a survivor the
    restart's finish leaves held). A job of the plan the oracle starts waits
    `dispatch_pending`; a job outside it runs. Once the test releases the stop, the update
    applies as a step of the loop, before any dispatch of a plan job; a closing attempt's rerun
    plan dispatches exactly one replacement, after it."""
    from dsl41.reload_plan import load_estate
    from dsl41.runner_startup import resume_run

    run_root, _ = _crashed_update(tmp_path, closing=closing, idle=True)
    _Procs(monkeypatch)
    entered, release = _park_ladders(monkeypatch, survivor=closing)
    seen: dict[str, Any] = {}

    def order() -> list[tuple[str, Any, Any, Any]]:
        return [(k, b.get("event"), b.get("job"), b.get("ordinal")) for _, k, b in rows(run_root)]

    async def restart() -> None:
        resumed = asyncio.ensure_future(
            resume_run(
                load_estate(tmp_path / "estate").catalog,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                settle_seconds=0.0,
                grace_seconds=0.0,
                settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
            )
        )
        async with asyncio.timeout(30):
            await entered.wait()
            if closing:
                release.set()  # the finish returns: something is left, and held
            engine = await resumed
        await engine.run_until_quiescent(engine.clock.now())
        seen["view"] = engine.update.view()
        for job in ("p", "u"):
            engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(engine.clock.now())
        seen["before"] = order()
        if closing:
            engine.stops._look()  # the held poll finds nothing left: the stop is finished
        else:
            release.set()
            await engine.attempts.join()
        seen["queued"] = engine.update.stopping  # the end waits for its loop step
        await engine.run_until_quiescent(engine.clock.now())
        seen["after"] = order()
        seen["rev"] = engine.config_rev
        await engine.shutdown()
        _close(engine)

    asyncio.run(restart())
    assert seen["view"] is not None and seen["view"]["stopping"] == ["j"]
    before = seen["before"]
    intent = before.index(("config", "intent", None, None))
    assert ("config", "applied", None, None) not in before[intent:]
    assert ("dispatch_pending", "pending", "p", None) in before[intent:]
    assert not [o for o in before[intent:] if o[0] == "dispatch" and o[2] in ("j", "p")]
    assert [o for o in before[intent:] if o[0] == "dispatch" and o[2] == "u"]
    assert seen["queued"] is True and seen["rev"] == 2
    after = seen["after"]
    applied = after.index(("config", "applied", None, None), intent)
    plan_jobs = [
        i for i, o in enumerate(after) if i > intent and o[0] == "dispatch" and o[2] in ("j", "p")
    ]
    assert plan_jobs and min(plan_jobs) > applied  # no plan job before the application
    replacements = [o for o in after[intent:] if o[0] == "dispatch" and o[2] == "j"]
    assert replacements == [("dispatch", None, "j", 2)]  # exactly one, the rerun's


def test_a_box_run_keeps_its_rev_through_snapshot_prune_resume_update_snapshot_prune(
    tmp_path: Path,
) -> None:
    """Astra's five steps: boxes run under rev 1; a snapshot and a prune; a restart; an
    update that edits one box and removes the other; a snapshot and a prune again. Each box run
    keeps rev 1, recorded when it opened, and the removed box's run stays in `runs`."""
    from dsl41.reload_plan import load_estate
    from dsl41.runner_history import read_run_root
    from dsl41.runner_startup import resume_run

    def boxes(**jobs: str) -> str:
        return ANCHOR + "".join(
            f"insert_job: {job}\njob_type: b\ncondition: s(anchor)\n{extra}\n"
            for job, extra in jobs.items()
        )

    run_root = tmp_path / "run"
    hashes: list[str] = []

    def prune(engine: Engine) -> None:
        engine.take_snapshot()
        assert engine.journal is not None
        engine.journal.store.prune_before(10**6, T0 + timedelta(days=9999))

    async def first() -> None:
        engine = _engine(tmp_path, boxes(r="", q=""), default=(0.0, 0))
        for job in ("r", "q"):
            engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        prune(engine)
        await engine.shutdown()
        _close(engine)

    async def second() -> None:
        engine = await resume_run(
            load_estate(tmp_path / "estate").catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter()},
            settle_seconds=0.0,
            grace_seconds=0.0,
            settings=EngineSettings(grace_term_s=0.05, grace_kill_s=0.1),
        )
        await engine.run_until_quiescent(engine.clock.now())
        _estate(tmp_path, boxes(r='description: "edited"\n'))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted)
        engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": "r"}))
        await engine.run_until_quiescent(engine.clock.now())
        assert engine.journal is not None
        hashes.extend(
            [
                load_configuration_hash(engine, 1),
                load_configuration_hash(engine, 2),
            ]
        )
        prune(engine)
        await engine.shutdown()
        _close(engine)

    asyncio.run(first())
    asyncio.run(second())
    one, two = hashes
    label = {(row.job, row.run_number): row.catalog_hash for row in read_run_root(run_root)}
    assert label[("r", 1)] == one and label[("q", 1)] == one and label[("r", 2)] == two


def load_configuration_hash(engine: Engine, rev: int) -> str:
    from dsl41.runner_journal import load_configuration

    assert engine.journal is not None
    return load_configuration(engine.journal.store, rev, where="t").catalog_hash


def test_a_snapshot_or_a_history_with_a_run_and_no_rev_is_refused(tmp_path: Path) -> None:
    """A run's rev is recorded, never derived: a snapshot whose retained run lacks `rev` does
    not read, and `runs` refuses a dispatch that names no rev the store holds."""
    from dsl41.canon import canonical_bytes, decode
    from dsl41.runner_clock import EngineError
    from dsl41.runner_history import RunHistoryError, read_run_root

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"a": "x"}), default=(0.0, 0))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)
        seq = engine.take_snapshot()
        assert engine.journal is not None
        stored = engine.journal.store.latest_snapshot()
        assert stored is not None
        raw = decode(stored.data)
        raw["runs"] = {"a": [{**run, "rev": None} for run in raw["runs"]["a"]]}
        with pytest.raises(EngineError, match="retained run a.1 records no rev"):
            decode_snapshot(seq, canonical_bytes(raw), where="t")
        engine.journal.append(
            dispatch_record("a", 2, attempt=OTHER_ID, spool=None, started_at=T0, rev=9)
        )
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    with pytest.raises(RunHistoryError, match="run a.2 records no configuration rev"):
        read_run_root(tmp_path / "run")


def test_a_run_keeps_the_rev_it_opened_under_when_its_replacement_runs_under_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run is not an attempt. `j.1` opens under rev 1; an update changes `j` and stops it;
    the replacement attempt of the same run dispatches under rev 2. `runs` reports the run
    under rev 1 before and after a prune that drops the first attempt's dispatch, and the
    second attempt's `dispatch.rev` is 2: the attempt's own fact, never read as the run's."""
    from dsl41.runner import outcome_record, plan_record
    from dsl41.runner_history import read_run_root
    from dsl41.runner_journal import load_configuration

    _Procs(monkeypatch)
    run_root = tmp_path / "run"
    hashes: list[str] = []
    labels: list[str] = []

    async def scenario() -> None:
        engine = _engine(tmp_path, _jil({"j": "sleep 30"}))
        engine.inject(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        # the stand-in task's attempt is closed, so the observed one below is the run's only
        # open attempt and the prune can pass the first dispatch
        live = engine._live.pop("j")
        live.task.cancel()
        await asyncio.gather(live.task, return_exceptions=True)
        assert engine.journal is not None and live.attempt is not None
        engine.journal.append(
            outcome_record(live.attempt, "j", 1, "stopped", {}, T0, cause="test"),
            plan_record(live.attempt, "j", 1, "none", [], T0),
        )
        _adopt(engine, "j", RUN_ID, PGID)
        _estate(tmp_path, _jil({"j": "sleep 31"}))
        answer = await _request(engine, _plan(engine, tmp_path))
        assert isinstance(answer.decision, Admitted)
        engine.attempts.status_recorded(RUN_ID, SIGNALED)
        await engine.attempts.join()
        await engine.run_until_quiescent(engine.clock.now())  # applies; the rerun dispatches
        assert engine.journal is not None
        store = engine.journal.store
        hashes.extend(load_configuration(store, rev, where="t").catalog_hash for rev in (1, 2))
        labels.extend(r.catalog_hash for r in read_run_root(run_root) if r.job == "j")
        engine.take_snapshot()
        floor = engine.journal.open.floor()
        assert floor is not None
        store.prune_before(floor, T0 + timedelta(days=9999))  # past the first dispatch
        labels.extend(r.catalog_hash for r in read_run_root(run_root) if r.job == "j")
        kept = [b for _, k, b in rows(run_root) if k == "dispatch" and b["job"] == "j"]
        assert [d["ordinal"] for d in kept] == [2]  # the first attempt's dispatches are gone
        engine.stops.cancel_all()
        await engine.shutdown()
        _close(engine)

    asyncio.run(scenario())
    one, _two = hashes
    assert labels == [one, one]
    second = [d for d in _kinds(tmp_path, "dispatch") if d["job"] == "j" and d["ordinal"] == 2]
    assert [d["rev"] for d in second] == [2]
