"""The branches of the stop wiring that the scenarios of `test_engine_stops.py` do not take: the
adapter's observed launch, the stop ladder, the engine's side of the tiers, the restart's step 5
and the reads that show an attempt (spec-wiring 2b, DL-426). Real processes where a branch
needs one; a stand-in where the branch is about a race or a failure the host cannot produce on
demand."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from store_surgery import rows

from dsl41 import runner_adapters, runner_procid
from dsl41.engine_charts import AttemptOutcome, ExitRecord
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import (
    AdapterContext,
    FakeAdapter,
    FileWatcherAdapter,
    Launch,
    LocalCommandAdapter,
    spool_record,
    stop_ladder,
)
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import attempt_note, status_label
from dsl41.runner_journal import decode_snapshot, dispatch_record, open_reader
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_stops import COMMAND_SURVIVED, AttemptFacts

T0 = datetime(2026, 7, 1, 8, 0)
RUN_ID = "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e4a10"
OTHER_ID = "0f6a8f4c-1c2d-4b5e-8f90-a1b2c3d4e5f6"
_JIL = "insert_job: j\njob_type: c\ncommand: sleep 30\n"


class _Seen:
    """An `AttemptObserver` that keeps what it was told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def spawn_recorded(self, spawn: Mapping[str, Any]) -> None:
        self.calls.append(("spawn", dict(spawn)))

    def status_recorded(self, status: Mapping[str, Any]) -> None:
        self.calls.append(("status", dict(status)))

    def spawn_failed(self, error: str) -> None:
        self.calls.append(("spawn_failed", error))

    def wrapper_exited(self, spawn: Mapping[str, Any] | None) -> None:
        self.calls.append(("exited", spawn))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _ctx(tmp_path: Path) -> AdapterContext:
    run_root = tmp_path / "root"
    return AdapterContext(
        clock=RealClock(),
        run_root=run_root,
        run_id=RUN_ID,
        spool=run_root / "runs" / "j.1" / "1",
    )


def _job(command: str = "sleep 30") -> Any:
    return lower_source(f"insert_job: j\njob_type: c\ncommand: {command}\n").jobs["j"]


# ------------------------------------------------------------------- the observed launch


def test_an_observed_launch_reports_spawn_then_record(tmp_path: Path) -> None:
    seen = _Seen()
    ctx = _ctx(tmp_path)
    launch = Launch(seen, ctx.spool)
    asyncio.run(LocalCommandAdapter(grace_seconds=1.0).observe(_job("true"), 1, ctx, launch))
    assert seen.names() == ["spawn", "status"]
    assert seen.calls[1][1]["outcome"] == "exited" and launch.lifeline_w is None


def test_a_wrapper_that_cannot_start_is_a_failed_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_spawn(*_: Any, **__: Any) -> Any:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    seen = _Seen()
    ctx = _ctx(tmp_path)
    asyncio.run(LocalCommandAdapter().observe(_job(), 1, ctx, Launch(seen, ctx.spool)))
    assert seen.calls == [("spawn_failed", "wrapper spawn failed: [Errno 24] Too many open files")]


class _Proc:
    """A wrapper stand-in: `stdin` fails or cancels as told; `kill` and `wait` are recorded."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.killed = False
        self.returncode = 0
        fail_with = fail

        class _Stdin:
            def write(self, *_: bytes) -> None:
                if fail_with is not None:
                    raise fail_with

            async def drain(self) -> None: ...

            def close(self) -> None: ...

        self.stdin = _Stdin()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


@pytest.mark.parametrize(
    ("fail", "expected"),
    [
        (
            BrokenPipeError(32, "Broken pipe"),
            [("spawn_failed", "wrapper spawn failed: [Errno 32] Broken pipe")],
        ),
        (asyncio.CancelledError(), "cancelled"),
    ],
    ids=["spec_write_failed", "cancelled_during_the_spec"],
)
def test_a_wrapper_lost_while_its_spec_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: BaseException, expected: Any
) -> None:
    proc = _Proc(fail)

    async def spawn(*_: Any, **__: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    seen = _Seen()
    ctx = _ctx(tmp_path)
    run = LocalCommandAdapter().observe(_job(), 1, ctx, Launch(seen, ctx.spool))
    if expected == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run)
        assert proc.killed and seen.calls == []  # DL-359: nothing launched, nothing to report
    else:
        asyncio.run(run)
        assert seen.calls == expected


def test_a_stop_before_the_wrapper_exists_kills_it_unlaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = _Proc()
    seen = _Seen()
    ctx = _ctx(tmp_path)
    launch = Launch(seen, ctx.spool)

    async def spawn(*_: Any, **__: Any) -> Any:
        launch.abort()  # the stop lands while the wrapper is being created
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    asyncio.run(LocalCommandAdapter().observe(_job(), 1, ctx, launch))
    assert proc.killed and seen.calls == [("exited", None)]


class _StoppedAsTheSpecGoes(Launch):
    """A launch whose stop lands while the spec is written: `aborted` turns true as the
    adapter marks the spec sent, before `spawn.json` exists."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "spec_sent" and value:
            object.__setattr__(self, "aborted", True)
        object.__setattr__(self, name, value)


def test_a_stop_while_the_spec_is_on_its_way_closes_the_lifeline(tmp_path: Path) -> None:
    """A stop that lands during the spec write, before `spawn.json`: the lifeline is closed as
    soon as the spec is sent, so the wrapper ends what it launches and records it."""
    seen = _Seen()
    ctx = _ctx(tmp_path)
    launch = _StoppedAsTheSpecGoes(seen, ctx.spool)
    asyncio.run(
        asyncio.wait_for(LocalCommandAdapter(grace_seconds=1.0).observe(_job(), 1, ctx, launch), 30)
    )
    status = [call for name, call in seen.calls if name == "status"]
    assert [s["cause"] for s in status] == ["parent lost"]


def test_launch_abort_branches(tmp_path: Path) -> None:
    """`abort` before a wrapper exists only marks it; with the spec sent and `spawn.json` on
    disk it leaves the ladder to the tier; with neither it closes the lifeline once."""
    launch = Launch(_Seen(), tmp_path)
    launch.abort()
    assert launch.aborted and launch.lifeline_w is None
    read, write = os.pipe()
    launch = Launch(_Seen(), tmp_path, proc=_Proc(), spec_sent=True, lifeline_w=write)  # type: ignore[arg-type]
    (tmp_path / "spawn.json").write_text("{}")
    launch.abort()
    assert launch.lifeline_w == write
    (tmp_path / "spawn.json").unlink()
    launch.abort()
    launch.close_lifeline()
    assert launch.lifeline_w is None and os.read(read, 1) == b""
    os.close(read)
    gone = Launch(_Seen(), None, proc=_Proc(), spec_sent=False)  # type: ignore[arg-type]

    class _Gone(_Proc):
        def kill(self) -> None:
            raise ProcessLookupError

    gone.proc = _Gone()  # type: ignore[assignment]
    gone.abort()  # a wrapper already gone is not an error
    unspooled = Launch(_Seen(), None, proc=_Proc(), spec_sent=True)  # type: ignore[arg-type]
    unspooled.abort()


def test_a_spool_record_naming_another_run_refuses(tmp_path: Path) -> None:
    (tmp_path / "spawn.json").write_text(json.dumps({"version": 1, "run_id": OTHER_ID}))
    with pytest.raises(EngineError, match="stranger's record .DL-118."):
        spool_record(tmp_path / "spawn.json", RUN_ID)
    assert spool_record(tmp_path / "status.json", RUN_ID) is None


def test_a_wrapper_that_leaves_no_record_is_reported_with_its_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wrapper's spawn.json is read once more after it exits, before the missing record is
    reported; a cancel while it runs closes the lifeline and waits for it."""
    seen = _Seen()
    ctx = _ctx(tmp_path)
    launch = Launch(seen, ctx.spool)
    real = runner_adapters.spool_record

    def late(path: Path, run_id: str) -> Any:
        if path.name == "status.json":
            return None  # as if the wrapper never wrote it
        # spawn.json is first seen only after the wrapper exited
        exited = launch.proc is not None and launch.proc.returncode is not None
        return real(path, run_id) if exited else None

    monkeypatch.setattr(runner_adapters, "spool_record", late)
    asyncio.run(
        asyncio.wait_for(
            LocalCommandAdapter(grace_seconds=1.0).observe(_job("true"), 1, ctx, launch), 30
        )
    )
    assert seen.names() == ["spawn", "exited"]
    assert seen.calls[1][1] is not None


def test_a_cancelled_watch_closes_the_lifeline_and_lets_the_wrapper_record(
    tmp_path: Path,
) -> None:
    seen = _Seen()
    ctx = _ctx(tmp_path)
    launch = Launch(seen, ctx.spool)

    async def scenario() -> None:
        task = asyncio.ensure_future(
            LocalCommandAdapter(grace_seconds=0.5).observe(_job(), 1, ctx, launch)
        )
        while "spawn" not in seen.names():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ctx.spool is not None
    status = spool_record(ctx.spool / "status.json", RUN_ID)
    assert status is not None and status["cause"] == "parent lost"


# ------------------------------------------------------------------- the stop ladder


def _group(command: list[str]) -> tuple[subprocess.Popen[bytes], str]:
    child = subprocess.Popen(command, process_group=0)
    token = None
    while token is None:
        token = runner_procid.proc_start_token(child.pid)
    return child, token


def test_the_ladder_terms_and_returns_when_the_group_goes() -> None:
    child, token = _group(["sleep", "30"])
    asyncio.run(stop_ladder(child.pid, token, 5.0))
    assert child.wait(5) == -signal.SIGTERM


def test_the_ladder_kills_a_group_that_ignores_term() -> None:
    child, token = _group(
        [
            sys.executable,
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);print(1,flush=True);time.sleep(30)",
        ]
    )
    asyncio.run(asyncio.sleep(0.3))
    asyncio.run(stop_ladder(child.pid, token, 0.2))
    assert child.wait(5) == -signal.SIGKILL


def test_the_ladder_sends_nothing_to_a_group_whose_leader_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []
    monkeypatch.setattr(runner_procid, "killpg_quiet", lambda *args: sent.append(args[1]))
    monkeypatch.setattr(runner_procid, "verify_alive", lambda *_: False)
    asyncio.run(stop_ladder(1, "t", 0.1))
    assert sent == []
    answers = iter([True, False])  # alive at TERM, gone by the KILL after a zero grace
    monkeypatch.setattr(runner_procid, "verify_alive", lambda *_: next(answers))
    monkeypatch.setattr(runner_adapters, "group_exists", lambda _pgid: False)  # and so is the group
    asyncio.run(stop_ladder(1, "t", 0.0))
    assert sent == [signal.SIGTERM]
    # a leader gone while its group lives: the closing SIGKILL (Opus 3, DL-226)
    answers = iter([True, False, False])
    monkeypatch.setattr(runner_adapters, "group_exists", lambda _pgid: True)
    asyncio.run(stop_ladder(1, "t", 0.0))
    assert sent == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]


# ------------------------------------------------------------------- the engine's side


def _engine(tmp_path: Path, text: str = _JIL, **kwargs: Any) -> Engine:
    return start_run(
        lower_source(text),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        **kwargs,
    )


def _adopt(engine: Engine, job: str = "j", run: int = 1, attempt: str = RUN_ID) -> AttemptFacts:
    facts = AttemptFacts(attempt, job, run, 1, Path("/nonexistent"), exited=True)
    engine.adopt_attempt(facts)
    return facts


def test_a_group_recorded_under_another_boot_is_dead(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path)
        stops = engine.stops
        child, token = _group(["sleep", "30"])
        try:
            assert stops.command_alive(child.pid, token)
            stops.note_boot(
                {"command_pgid": child.pid, "command_start_time": token, "boot_id": "x"}
            )
            assert not stops.command_alive(child.pid, token)
            stops.note_boot({"command_pgid": "not an int"})
        finally:
            child.kill()
            child.wait()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_a_stop_that_cannot_be_carried_out_is_noted_and_shown(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path)
        facts = _adopt(engine)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}
        engine.attempts.spawn_recorded(RUN_ID, spawn)
        engine.stops.ladder_failed(RUN_ID, OSError("EPERM"))
        assert engine.held_groups == {RUN_ID: 2**22 + 7} and engine.blocked("j") == RUN_ID
        assert facts.note == "ladder failed: EPERM" and facts.settled.is_set()
        engine.stops.command_survived(RUN_ID)
        assert facts.note == COMMAND_SURVIVED
        engine.stops.kill_wrapper(RUN_ID)  # a restart's attempt has no wrapper of ours
        engine.attempts.stop(RUN_ID, "engine_stop")
        view = engine.attempt_view("j")
        assert view is not None and (view["state"], view["note"]) == ("STOPPING", COMMAND_SURVIVED)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_a_timer_fault_stops_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path)

        def boom(_key: str, _generation: int) -> None:
            raise EngineError("a stranger's record")

        monkeypatch.setattr(engine.attempts, "record_timeout", boom)
        engine.stops.arm_timer("k", 1, 0.0)
        engine.stops.arm_timer("k", 2, 0.0)  # replaces the first
        engine.stops.cancel_timer("nothing armed")
        await asyncio.sleep(0.05)
        with pytest.raises(EngineError, match="a stranger's record"):
            await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_attempt_view_and_plan_bookkeeping_branches(tmp_path: Path) -> None:
    """The view is shown only for the job's current run while its attempt is unsettled; a plan
    that is gone or done changes nothing; an attempt is forgotten once its plan is done and
    its wrapper has exited."""

    async def scenario() -> None:
        engine = _engine(tmp_path)
        assert engine.attempt_view("j") is None  # no attempt
        _adopt(engine)
        assert engine.attempt_view("j") is None  # no row yet
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        assert engine.attempt_view("j") is not None
        engine._step_done("no such plan", 0)
        engine.perform_plan("no such plan")
        engine._plan_finished("unknown attempt")
        engine._forget_if_over("unknown attempt")
        engine.attempts.wrapper_exited(RUN_ID, None)  # unobservable: plan rerun, recorded
        # the rerun is dispatched at once (no other attempt of the job is unsettled), so the
        # plan is done and, its wrapper gone, the attempt is forgotten
        assert not engine.attempts.knows(RUN_ID)
        _adopt(engine, run=2, attempt=OTHER_ID)
        assert engine.attempt_view("j") is None  # another run than the row's
        assert engine._job_attempt["j"] == OTHER_ID
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_an_engine_with_no_store_runs_a_plan_from_memory() -> None:
    async def scenario() -> None:
        engine = Engine(
            lower_source(_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.lifecycle.replay_done()
        facts = AttemptFacts(RUN_ID, "j", 1, 1, Path("/nonexistent"), {"DSL41_JOB": "j"})
        engine.adopt_attempt(facts)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        engine.record_outcome(
            RUN_ID,
            AttemptOutcome("natural", record=ExitRecord(exit_code=0, wrapper_outcome="exited")),
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        assert engine.oracle.store.job["j"].status == "SUCCESS"
        engine.journal_lifecycle("draining")  # no store: nothing to write
        assert engine._pending("j", 2) is None
        engine.leave("clean_shutdown", ())
        engine.exit_code = 0
        await engine.run_until_quiescent(T0 + timedelta(seconds=2))
        assert engine.stopped.is_set() and not engine.shutdown_recorded
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_watcher_is_cancelled_at_the_drain_and_keeps_its_open_attempt(tmp_path: Path) -> None:
    """spec-wiring 2b: a file watcher has no attempt; the drain cancels its polling, writes no
    outcome, and the restart resumes it from `watch.jsonl`."""
    run_root = tmp_path / "run"
    text = f"insert_job: w\njob_type: f\nwatch_file: {tmp_path / 'never'}\nwatch_interval: 60\n"

    async def scenario() -> Engine:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=RealClock(),
            adapters={"FW": FileWatcherAdapter()},
            hold_open=True,
            settings=EngineSettings(grace_term_s=0.5, grace_kill_s=0.5),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        engine.inject(Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "w"}))
        while not (run_root / "runs" / "w.1" / "watch.jsonl").exists():
            await asyncio.sleep(0.01)
        assert await engine.request_stop() == "STOPPED"
        await asyncio.wait_for(loop, 30)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(scenario())
    assert engine.leaving_record == "clean_shutdown"
    assert [b for _, k, b in rows(run_root) if k == "outcome"] == []
    store = open_reader(run_root)
    try:
        stored = store.latest_snapshot()
        assert stored is not None
        assert list(decode_snapshot(stored.seq, stored.data, where="t").open.attempts)
    finally:
        store.close()


def test_a_start_decided_while_draining_is_owed_to_the_restart(tmp_path: Path) -> None:
    """spec-drain: the oracle keeps deciding while the engine drains, and a STARTING decision
    is journaled as `dispatch_pending`, never launched. An attempt still stopping keeps the
    engine in DRAINING."""
    text = (
        "insert_job: j\njob_type: c\ncommand: x\n\n"
        "insert_job: k\njob_type: c\ncommand: y\ncondition: s(j)\n"
    )

    async def scenario() -> None:
        engine = _engine(tmp_path, text)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine, job="x")  # an attempt with no wrapper of ours: it stops until its timer
        engine.request_stop()
        await engine.run_until_quiescent(T0)
        assert engine.lifecycle.state == "DRAINING"
        completion = {"job": "j", "run_number": 1, "exit_code": 0}
        engine.inject(Event(at=T0, kind="STATUS", payload=completion), source="adapter")
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        k = engine.oracle.store.job["k"]
        assert (k.run_number, k.status in ("STARTING", "RUNNING")) == (1, True)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    pending = [b for _, k, b in rows(tmp_path / "run") if k == "dispatch_pending"]
    assert [(b["job"], b["run_number"]) for b in pending] == [("k", 1)]
    assert [b["job"] for _, k, b in rows(tmp_path / "run") if k == "dispatch"] == ["j"]


# ------------------------------------------------------------------- the restart's step 5


def _open(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    [dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch"]
    Path(dispatch["spool"]).mkdir(parents=True)
    return run_root, dispatch


def _resume(run_root: Path) -> Engine:
    async def scenario() -> Engine:
        engine = await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.3,
            grace_seconds=0.0,
            settings=EngineSettings(grace_term_s=0.2, grace_kill_s=0.3),
        )
        await engine.run_until_quiescent(engine.clock.now())
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    return asyncio.run(scenario())


def _write(spool: Path, name: str, dispatch: dict[str, Any], **fields: Any) -> None:
    identity = {"version": 1, "run_id": dispatch["attempt"], "job": "j", "run_number": 1}
    (spool / name).write_text(json.dumps({**identity, **fields}))


def test_a_live_wrapper_gets_its_window_to_record(tmp_path: Path) -> None:
    """Step 5: a wrapper still alive in this boot is given the settle window; the record it
    writes in that window is the attempt's."""
    run_root, dispatch = _open(tmp_path)
    spool = Path(dispatch["spool"])
    me = os.getpid()
    token = runner_procid.proc_start_token(me)
    child, child_token = _group(["sleep", "30"])
    try:
        _write(
            spool,
            "spawn.json",
            dispatch,
            wrapper_pid=me,
            wrapper_start_time=token,
            command_pid=child.pid,
            command_pgid=child.pid,
            command_start_time=child_token,
            boot_id=runner_procid.current_boot_id(),
        )
        timer = __import__("threading").Timer(
            0.1, _write, (spool, "status.json", dispatch), {"outcome": "exited", "exit_code": 0}
        )
        timer.start()
        _resume(run_root)
        timer.join()
    finally:
        child.kill()
        child.wait()
    [ended] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert ended["outcome"] == "natural"


def test_a_live_wrapper_that_never_records_gets_the_ladder(tmp_path: Path) -> None:
    """Step 5: a wrapper alive past its window with no record is lost; its command gets the
    ladder (`stop` with cause `wrapper_lost`), and with no record after the KILL the attempt
    is unobservable."""
    run_root, dispatch = _open(tmp_path)
    spool = Path(dispatch["spool"])
    me = os.getpid()
    child, child_token = _group(["sleep", "30"])
    try:
        _write(
            spool,
            "spawn.json",
            dispatch,
            wrapper_pid=me,
            wrapper_start_time=runner_procid.proc_start_token(me),
            command_pid=child.pid,
            command_pgid=child.pid,
            command_start_time=child_token,
            boot_id=runner_procid.current_boot_id(),
        )
        _resume(run_root)
        assert child.wait(5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    [ended] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert (ended["outcome"], ended["cause"]) == ("unobservable", "wrapper_lost")


def test_a_record_that_names_another_run_tuple_is_read_as_absent(tmp_path: Path) -> None:
    run_root, dispatch = _open(tmp_path)
    spool = Path(dispatch["spool"])
    _write(spool, "status.json", dispatch, job="k", outcome="exited", exit_code=0)
    _write(spool, "spawn.json", dispatch, wrapper_pid="x", boot_id="another boot")
    _resume(run_root)
    [ended] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert ended["outcome"] == "unobservable"


def test_the_evidence_folds_from_the_snapshot_and_the_tail(tmp_path: Path) -> None:
    """spec-crash: a held job's evidence is shown until its next start, across a restart: from
    the snapshot, then the hold plans and the starts after it."""
    from dsl41.runner_startup import _fold_evidence
    from dsl41.runner_store import Record

    engine = Engine(lower_source(_JIL), clock=VirtualClock(start=T0), adapters={})
    snapshot = type("S", (), {"evidence": {"j": {"run_number": 1, "attempt": "j.1.1"}}})()

    def record(kind: str, **body: Any) -> tuple[int, Record]:
        return 0, Record(T0, kind, None, None, body)

    tail = [
        record("dispatch", job="j", run_number=1),
        record("follow_up", event="plan", job="k", run_number=3, evidence={"attempt": "k.3.1"}),
        record("dispatch", job="j", run_number=2),
        record("dispatch", job="x", run_number=1),
    ]
    _fold_evidence(engine, snapshot, tail)
    assert engine.evidence == {"k": {"attempt": "k.3.1", "run_number": 3}}
    _fold_evidence(engine, None, [])
    assert "k" in engine.evidence


def test_a_dispatch_record_carries_the_cause_it_follows() -> None:
    record = dispatch_record("j", 1, attempt=RUN_ID, spool=None, started_at=T0, after="engine_stop")
    assert record.payload["after"] == "engine_stop"


# ------------------------------------------------------------------- the reads


@pytest.mark.parametrize(
    ("attempt", "note", "label"),
    [
        (None, "", "RUNNING"),
        ({"n": 1, "state": "RUNNING", "after": None}, "", "RUNNING"),
        (
            {"n": 2, "state": "RUNNING", "after": "update_stop"},
            "attempt 2, after update_stop",
            "RUNNING (attempt 2, after update_stop)",
        ),
        (
            {"n": 1, "state": "SPAWNING", "after": "lifeline"},
            "after lifeline",
            "RUNNING (after lifeline)",
        ),
        (
            {"n": 1, "state": "STOPPING", "cause": "engine_stop", "note": "ladder failed: EPERM"},
            "STOPPING (ladder failed: EPERM)",
            "STOPPING (ladder failed: EPERM)",
        ),
        (
            {"n": 1, "state": "STOPPING", "cause": "engine_stop", "note": None},
            "STOPPING (engine_stop)",
            "STOPPING (engine_stop)",
        ),
        ({"n": 1, "state": "STOPPING"}, "STOPPING", "STOPPING"),
        (
            {"n": 1, "state": "ENDED", "note": COMMAND_SURVIVED, "held": True},
            f"held: {COMMAND_SURVIVED}",
            f"RUNNING (held: {COMMAND_SURVIVED})",
        ),
    ],
)
def test_the_attempt_is_shown_beside_the_status(attempt: Any, note: str, label: str) -> None:
    row: dict[str, Any] = {"status": "RUNNING"}
    if attempt is not None:
        row["attempt"] = attempt
    assert (attempt_note(row), status_label(row)) == (note, label)


# ------------------------------------------------------------------- the remaining arcs


def test_a_wrapper_that_exits_with_neither_record_is_reported_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrapper that exits before `spawn.json` and writes no record: `wrapper_exited` with no
    spawn, whose verdict is `wrapper_gone`."""
    stand_in = tmp_path / "wrapper.py"
    stand_in.write_text("import sys\nsys.stdin.read()\nsys.exit(3)\n")
    monkeypatch.setattr(runner_adapters, "_WRAPPER_PATH", stand_in)
    seen = _Seen()
    ctx = _ctx(tmp_path)
    asyncio.run(LocalCommandAdapter().observe(_job(), 1, ctx, Launch(seen, ctx.spool)))
    assert seen.calls == [("exited", None)]


def test_engine_side_arcs(tmp_path: Path) -> None:
    """An observed attempt the oracle stops after it ended gets no second stop; a held job's
    evidence leaves at its next start; a spawn read with no `spawn.json`; a wrapper reported
    gone with its spawn; a failed spawn named by its cause; `status` shows the evidence."""
    from dsl41.runner import _LiveRun, outcome_kind
    from dsl41.runner_control import ControlServer
    from dsl41.runner_stops import Observer

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.evidence["j"] = {"run_number": 0, "attempt": "j.0.1"}
        server = ControlServer(engine, tmp_path / "unused.sock")
        assert server._status({"job": "j"})["jobs"]["j"]["interrupted"]["attempt"] == "j.0.1"
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        assert "j" not in engine.evidence  # run 1 started
        facts = _adopt(engine, attempt=OTHER_ID)
        facts.spool = tmp_path / "empty"
        facts.spool.mkdir()
        assert engine.stops.read_spool(OTHER_ID) == (None, None)
        spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}
        Observer(engine.stops, OTHER_ID).wrapper_exited(spawn)
        third = "9d1f2e3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
        engine.adopt_attempt(AttemptFacts(third, "j", 1, 1, facts.spool))
        Observer(engine.stops, third).wrapper_exited(None)  # no spawn.json: nothing to note
        assert engine.attempts.state(third) == "ENDED"
        assert (
            engine.attempts.knows(OTHER_ID) is False or engine.attempts.state(OTHER_ID) == "ENDED"
        )
        # its wrapper still runs (`exited` false), so the ended attempt is not yet forgotten
        engine.adopt_attempt(AttemptFacts(RUN_ID, "j", 1, 1, facts.spool))
        engine.attempts.wrapper_exited(RUN_ID, None)
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        ended = _LiveRun(1, asyncio.ensure_future(done), attempt=RUN_ID)  # type: ignore[arg-type]
        engine._kill(ended)  # ended already: reaped, never stopped again
        assert ended.task in engine._reaping
        assert (
            outcome_kind(runner_adapters.Failed("wrapper spawn failed: EMFILE")) == "spawn_failed"
        )
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_a_superseded_replacement_on_an_engine_with_no_store_is_closed(tmp_path: Path) -> None:
    from dsl41.runner import Deferred

    async def scenario() -> None:
        engine = Engine(
            lower_source(_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine._discharge(Deferred("j", 5, plan=RUN_ID, step=0, attempt=OTHER_ID))
        assert engine._live == {}  # the run is not the oracle's: nothing launched
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_command_attempt_needs_a_run_root() -> None:
    """The attempt tier runs a command attempt from its spool, so an engine with no run root
    refuses the dispatch before anything is recorded or launched."""

    async def scenario() -> None:
        engine = Engine(
            lower_source(_JIL),
            clock=VirtualClock(start=T0),
            adapters={"CMD": LocalCommandAdapter()},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        with pytest.raises(EngineError, match="needs a run_root"):
            await engine.run_until_quiescent(T0)
        assert engine._live == {} and engine._facts == {}
        await engine.shutdown()

    asyncio.run(scenario())


# ------------------------------------------------------------------- rework round 2


def test_the_ladder_kills_members_that_outlive_their_leader() -> None:
    """Opus 3 (DL-226): under dash `sh` leads the group, and a child that ignores TERM outlives
    it. The ladder sends the closing SIGKILL once the leader is gone while the group still
    exists, and the engine counts a group that still exists after its ladder as alive."""
    script = "(trap '' TERM; exec sleep 30) & wait"
    child, token = _group(["/bin/sh", "-c", script])
    other, other_token = _group(["/bin/sh", "-c", script])

    async def scenario() -> bool:
        engine = Engine(lower_source(_JIL), clock=VirtualClock(start=T0), adapters={})
        stops = engine.stops
        await asyncio.sleep(0.3)  # each member has trapped TERM
        await stops.ladder(child.pid, token, 2.0)
        child.wait(5)
        for _ in range(100):
            if not runner_adapters.group_exists(child.pid):
                break
            await asyncio.sleep(0.05)
        # a laddered group whose leader is gone and whose member lives has survived its stop
        os.kill(other.pid, signal.SIGTERM)
        other.wait(5)
        stops._laddered.add(other.pid)
        return stops.command_alive(other.pid, other_token)

    try:
        survived = asyncio.run(scenario())
    finally:
        for group in (child.pid, other.pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
    assert child.returncode == -signal.SIGTERM
    assert not runner_adapters.group_exists(child.pid)  # the member died of the closing SIGKILL
    assert survived


def test_a_dead_unreaped_leader_is_no_live_member() -> None:
    """A group whose only member is a zombie its parent has not reaped holds no live member,
    so a stop it took is not reported as survived."""
    child, _token = _group(["sleep", "30"])
    try:
        assert runner_adapters.group_has_live_member(child.pid)
        os.kill(child.pid, signal.SIGKILL)
        for _ in range(100):
            if not runner_adapters.group_has_live_member(child.pid):
                break
            time.sleep(0.05)
        assert not runner_adapters.group_has_live_member(child.pid)
    finally:
        child.kill()
        child.wait()


def test_the_oracle_takes_hold_only_on_a_failure() -> None:
    """The interruption hold rides only on a FAILURE (DL-424): any other use is refused."""
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError

    oracle = Oracle(lower_source(_JIL))
    oracle.feed(Event(at=T0, kind="FORCE_STARTJOB", payload={"job": "j"}))
    bad = Event(at=T0, kind="STATUS", payload={"job": "j", "status": "RUNNING", "hold": True})
    with pytest.raises(OracleError, match="only on a FAILURE"):
        oracle.feed(bad)


def test_a_stop_still_queued_at_teardown_is_answered() -> None:
    async def scenario() -> str:
        engine = Engine(lower_source(_JIL), clock=VirtualClock(start=T0), adapters={})
        reply = engine.request_stop()
        await engine.shutdown()  # the loop never ran
        return await reply

    assert asyncio.run(scenario()) == "STARTING"


def test_a_timer_fault_during_startup_fails_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra M3: a fault a tier's timer raises while step 5 waits ends the wait; the restart
    fails, shuts down and releases the lock."""
    from dsl41.runner_attempts import AttemptTier
    from dsl41.runner_ledger import acquire_run_root

    run_root, dispatch = _open(tmp_path)
    spool = Path(dispatch["spool"])
    me = os.getpid()
    child, child_token = _group(["sleep", "30"])

    def boom(self: AttemptTier, attempt: str, generation: int) -> None:
        raise EngineError("a stranger's record at the timeout")

    monkeypatch.setattr(AttemptTier, "record_timeout", boom)
    try:
        _write(
            spool,
            "spawn.json",
            dispatch,
            wrapper_pid=me,
            wrapper_start_time=runner_procid.proc_start_token(me),
            command_pid=child.pid,
            command_pgid=child.pid,
            command_start_time=child_token,
            boot_id=runner_procid.current_boot_id(),
        )
        with pytest.raises(EngineError, match="a stranger's record at the timeout"):
            _resume(run_root)
    finally:
        child.kill()
        child.wait()
    acquire_run_root(run_root).release()  # the refused restart let the lock go


def test_a_starting_engine_waits_out_a_probe_and_refuses_a_holder(tmp_path: Path) -> None:
    """Astra M5: `engine stop --wait` probes `leader.lock` with a shared flock for microseconds;
    an engine starting meanwhile retries for one second before it refuses. A real holder is
    still refused, named."""
    import fcntl
    import threading

    from dsl41.runner_ledger import ACQUIRE_PATIENCE_S, acquire_run_root

    run_root = tmp_path / "root"
    run_root.mkdir()
    probe = os.open(run_root / "leader.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(probe, fcntl.LOCK_SH)
    threading.Timer(0.3, os.close, (probe,)).start()  # held across the acquisition's first try
    acquire_run_root(run_root).release()
    holder = acquire_run_root(run_root)
    try:
        started = time.monotonic()
        with pytest.raises(EngineError, match="held by another engine"):
            acquire_run_root(run_root)
        assert time.monotonic() - started >= ACQUIRE_PATIENCE_S
    finally:
        holder.release()


def test_a_restart_keeps_each_plan_step_s_time_monotone(tmp_path: Path) -> None:
    """Astra M1, as Opus R2-1 corrected it: a recovered step is stamped with the attempt's
    recorded end, never earlier than the admission frontier; an attempt with no recorded end
    (here the marked job's hold) takes the frontier. A wrapper that ended later than the
    journal's last instant cannot make the hold go back in time."""
    text = _JIL + "\ninsert_job: k\njob_type: c\ncommand: sleep 30\n"
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"k": "posts twice"},
        )
        for job in ("j", "k"):
            engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": job}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())
    [j_dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch" and b["job"] == "j"]
    spool = Path(j_dispatch["spool"])
    spool.mkdir(parents=True)
    late = (T0 + timedelta(seconds=30)).isoformat() + "+00:00"
    _write(spool, "status.json", j_dispatch, outcome="exited", exit_code=0, ended_at=late)

    async def restart() -> Engine:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await engine.run_until_quiescent(engine.clock.now())
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(restart())
    assert engine.oracle.store.job["j"].status == "SUCCESS"
    k = engine.oracle.store.job["k"]
    assert (k.status, k.on_hold) == ("FAILURE", True)
    fed = [b for _, kind, b in rows(run_root) if kind == "input" and b.get("source") == "reconcile"]
    stamps = {b["payload"]["job"]: b["at"] for b in fed}
    assert stamps == {"k": T0.isoformat(), "j": (T0 + timedelta(seconds=30)).isoformat()}


def test_a_replacement_dispatch_carries_its_cause_on_the_wire() -> None:
    """Astra N3: the wire's `dispatch` projects `after`, the cause of the stop the replacement
    follows; a first attempt's has none."""
    from dsl41.runner_journal import wire_record

    first = dispatch_record("j", 1, attempt=RUN_ID, spool=None, started_at=T0)
    second = dispatch_record(
        "j", 1, attempt=OTHER_ID, spool=None, started_at=T0, ordinal=2, after="engine_stop"
    )
    assert "after" not in (wire_record(first) or {})
    assert (wire_record(second) or {})["after"] == "engine_stop"


def test_a_long_note_is_cut_to_the_attempt_column() -> None:
    """Opus 6: a ladder's exception text has no bound of its own; the note is cut to the
    column the TUI keeps for it, and `query --brief` widens its status field to fit."""
    from dsl41.runner_control import ATTEMPT_NOTE_WIDTH

    row = {"status": "RUNNING", "attempt": {"n": 1, "state": "STOPPING", "note": "x" * 200}}
    note = attempt_note(row)
    assert len(note) == ATTEMPT_NOTE_WIDTH and note.endswith("…")
    assert status_label(row) == note


def test_a_success_recorded_before_an_outage_deadline_stands(tmp_path: Path) -> None:
    """Opus R2-1: `x` exits 0 at T0+2m, its `term_run_time` (10 minutes) falls inside the
    outage, and the engine restarts at T0+30m. The completion is fed at its recorded end, so
    it beats the deadline: `x` ends SUCCESS and `y`, on `s(x)`, runs."""
    text = (
        "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 10\n\n"
        "insert_job: y\njob_type: c\ncommand: y\ncondition: s(x)\n"
    )
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())
    [dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch"]
    spool = Path(dispatch["spool"])
    spool.mkdir(parents=True)
    ended = (T0 + timedelta(minutes=2)).isoformat() + "+00:00"
    identity = {"version": 1, "run_id": dispatch["attempt"], "job": "x", "run_number": 1}
    (spool / "status.json").write_text(
        json.dumps({**identity, "outcome": "exited", "exit_code": 0, "ended_at": ended})
    )

    async def restart() -> Engine:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=30)),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=31))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(restart())
    assert engine.oracle.store.job["x"].status == "SUCCESS"
    assert engine.oracle.store.job["y"].run_number == 1
    assert [b["job"] for _, k, b in rows(run_root) if k == "dispatch"] == ["x", "y"]


def test_a_store_of_another_state_version_refuses_by_name(tmp_path: Path) -> None:
    """Opus R2-2: the plan-step grammar changed with 25, so a store written by a 24 binary,
    whose open plan may hold the older two-step hold, refuses to start naming both versions."""
    import sqlite3
    from contextlib import closing

    from dsl41.runner_ledger import STATE_MACHINE_VERSION

    run_root, _dispatch = _open(tmp_path)
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute("UPDATE meta SET value = '24' WHERE key = 'state_version'")
    assert STATE_MACHINE_VERSION == 25
    with pytest.raises(EngineError, match="state version 24, this binary runs 25"):
        _resume(run_root)


def test_a_live_plan_cut_before_its_feed_is_stamped_at_the_recorded_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra M1 reopened: a plan committed live carries no end; the restart reads it from the
    attempt's `outcome` record. `x` started at T0 with a one-minute deadline, its wrapper
    recorded exit 0 at T0+2m, and the engine died after the outcome and plan commit, before
    the feed. The restart feeds it at T0+2m, so the deadline fires first: `x` is TERMINATED,
    the completion is rejected stale, and `y` (`s(x)`) never starts."""
    text = (
        "insert_job: x\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
        "insert_job: y\njob_type: c\ncommand: y\ncondition: s(x)\n"
    )
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "x"}))
        await engine.run_until_quiescent(T0)
        [dispatch] = [b for _, k, b in rows(run_root) if k == "dispatch"]
        run_id = dispatch["attempt"]
        engine.adopt_attempt(AttemptFacts(run_id, "x", 1, 1, Path(dispatch["spool"])))
        monkeypatch.setattr(engine, "perform_plan", lambda _attempt: None)  # dies before it
        ended = (T0 + timedelta(minutes=2)).isoformat() + "+00:00"
        record = {"version": 1, "run_id": run_id, "job": "x", "run_number": 1}
        engine.attempts.spawn_recorded(run_id, {**record, "started_at": T0.isoformat()})
        engine.attempts.status_recorded(
            run_id, {**record, "outcome": "exited", "exit_code": 0, "ended_at": ended}
        )
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(first())
    [plan] = [b for _, k, b in rows(run_root) if k == "follow_up" and b["event"] == "plan"]
    assert "ended_at" not in plan["steps"][0]["payload"]  # the outcome says it, not the plan

    async def restart() -> Engine:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=5)),
            adapters={"CMD": FakeAdapter(default=None)},
            settle_seconds=0.0,
            grace_seconds=0.0,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=6))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return engine

    engine = asyncio.run(restart())
    assert engine.oracle.store.job["x"].status == "TERMINATED"
    assert engine.oracle.store.job.get("y") is None or engine.oracle.store.job["y"].run_number == 0
    decisions = [b for _, k, b in rows(run_root) if k == "decision"]
    assert [b["code"] for b in decisions if b["decision"] == "rejected"] == ["stale_completion"]
    [feed] = [b for _, k, b in rows(run_root) if k == "input" and b.get("source") == "reconcile"]
    assert feed["at"] == (T0 + timedelta(minutes=2)).isoformat()


def test_a_record_written_as_the_wrapper_died_is_read_before_the_dead_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R2-M1, the successor of the resume ladder's record-before-the-first-check case:
    step 5 reads no status, then the wrapper writes exit 0 and dies before the liveness check.
    The dead verdict re-reads the spool first, so the attempt ends `natural` and completes;
    nothing is rerun."""
    from dsl41 import runner_startup

    run_root, dispatch = _open(tmp_path)
    spool = Path(dispatch["spool"])
    _write(spool, "spawn.json", dispatch, wrapper_pid=1, wrapper_start_time="t", boot_id="b")

    def dies_after_recording(_engine: Any, _spawn: Any) -> bool:
        _write(spool, "status.json", dispatch, outcome="exited", exit_code=0)
        return False

    monkeypatch.setattr(runner_startup, "_wrapper_alive", dies_after_recording)
    _resume(run_root)
    [ended] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert ended["outcome"] == "natural"
    assert [
        b["plan"] for _, k, b in rows(run_root) if k == "follow_up" and b["event"] == "plan"
    ] == ["complete"]


@pytest.mark.parametrize("group", ["none", "dead", "leader", "member", "stranger", "survivor"])
def test_a_restart_finishes_the_ladder_of_a_closing_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    """Astra R3-M1, R4-M2: an attempt whose outcome was committed while its ladder ran stays
    open; the restart finishes that ladder before step 6 performs the plan. No group, or a
    dead one, closes it at once; a live leader with its recorded start token gets the whole
    ladder. Members whose leader is gone, and a process in that group with another start
    token, may be a stranger's: they get no signal, and the attempt stays open and held, its
    rerun not launched. So does a group that outlives the whole ladder."""
    from store_surgery import append

    from dsl41 import runner_stops

    run_root, dispatch = _open(tmp_path)
    run_id, spool = dispatch["attempt"], Path(dispatch["spool"])
    sent: list[tuple[int, int]] = []
    real_killpg = runner_procid.killpg_quiet

    def recorded(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(runner_procid, "killpg_quiet", recorded)
    child: subprocess.Popen[bytes] | None = None
    if group != "none":
        if group == "member":  # a shell leads, and its child ignores TERM
            child, token = _group(["/bin/sh", "-c", "(trap '' TERM; exec sleep 30) & wait"])
        else:  # the command alone in its group
            child, token = _group(["sleep", "30"])
        time.sleep(0.2)
        if group in ("dead", "member"):
            child.send_signal(signal.SIGKILL)
            child.wait()
        _write(
            spool,
            "spawn.json",
            dispatch,
            command_pid=child.pid,
            command_pgid=child.pid,
            command_start_time="another start" if group == "stranger" else token,
            boot_id=runner_procid.current_boot_id(),
        )
    if group == "survivor":
        monkeypatch.setattr(runner_stops, "group_has_live_member", lambda _pgid: True)
    body = {"attempt": run_id, "job": "j", "run_number": 1, "status": {}, "at": T0.isoformat()}
    append(
        run_root,
        "outcome",
        {**body, "outcome": "stopped", "cause": "engine_stop", "ladder": "running"},
        at=T0,
    )
    replacement = {"step": "dispatch", "attempt": OTHER_ID, "ordinal": 2, "env": {}, "after": "x"}
    plan = {"event": "plan", "attempt": run_id, "job": "j", "run_number": 1, "plan": "rerun"}
    append(run_root, "follow_up", {**plan, "steps": [replacement]}, at=T0)
    try:
        engine = _resume(run_root)
        alive = child is not None and runner_adapters.group_has_live_member(child.pid)
    finally:
        if child is not None:
            with contextlib.suppress(OSError):  # a group already gone
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    kinds = [(k, b.get("event")) for _, k, b in rows(run_root)]
    closed = ("follow_up", "ladder_done") in kinds
    dispatches = [b["ordinal"] for _, k, b in rows(run_root) if k == "dispatch"]
    held = group in ("member", "stranger", "survivor")
    assert (closed, dispatches) == ((False, [1]) if held else (True, [1, 2]))
    assert (engine.blocked("j") == run_id) is held
    assert (run_id in engine.attempts.unsettled()) is held
    if group in ("member", "stranger"):  # not provably ours: nothing sent, still alive
        assert sent == [] and alive
    if group == "leader":
        assert child is not None and sent[0] == (child.pid, signal.SIGTERM)


def test_a_held_group_keeps_its_job_s_starts_back_until_it_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra R4-M3's guard on every path: a start owed by a restart waits while its job's
    group is held; a second held attempt of the job keeps it waiting; a stopping attempt whose
    group is gone has its record timeout read again; a recovering engine defers what it
    releases, a draining one leaves the starts to the restart's pending records, and a plan
    of another attempt of the job goes once the job is free. A start the oracle moved past
    is dropped, with or without a pending record; a look that raises stops the loop; a
    process listing that fails proves nothing gone."""
    from dsl41.engine_charts import DrainRequest
    from dsl41.runner import Deferred, _Plan
    from dsl41.runner_journal import OpenAttempt
    from dsl41.runner_stops import GROUP_POLL_S

    spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}
    third = "9d1f2e3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f"

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        engine.attempts.spawn_recorded(RUN_ID, spawn)
        engine.attempts.stop(RUN_ID, "engine_stop")  # STOPPING; its ladder sends nothing
        await asyncio.sleep(0.05)
        engine.stops.command_survived(RUN_ID)
        # a second held attempt of the job, one a restart found (any unsettled one would do)
        other = OpenAttempt(OTHER_ID, "j", 1, 1, str(tmp_path / "other"), 0, T0.isoformat())
        engine.held_at_restart(other, 2**22 + 7)
        assert engine.blocked("j") in (RUN_ID, OTHER_ID)
        owed = Deferred("j", 1)
        engine._discharge(owed)
        assert engine._held_starts == [owed]
        engine._discharge(Deferred("j", 9))  # moved past: dropped, no pending record to close
        engine._plans[third] = _Plan(third, "j", 1, [], 0)
        engine._waiting_plans.add(third)
        engine.group_gone(RUN_ID)  # read again; OTHER_ID still holds the job
        assert engine.attempts.state(RUN_ID) == "ENDED"
        assert engine._held_starts == [owed] and third in engine._waiting_plans
        engine.recovery = lambda: None
        engine.group_gone(OTHER_ID)
        # RUN_ID's rerun waited for OTHER_ID's group; the owed start waits for RUN_ID to settle
        assert [d.plan for d in engine._deferred] == [RUN_ID] and engine._held_starts == [owed]
        engine._discharge(engine._deferred.pop(0))  # the rerun goes, so RUN_ID settles
        assert engine._deferred == [owed] and engine._held_starts == []
        assert third not in engine._plans
        engine.recovery = None
        engine._held_starts.append(owed)
        engine.hold_group(third, 2**22 + 7)
        engine._facts[third] = AttemptFacts(third, "j", 1, 1, Path("/nonexistent"))
        engine.lifecycle.stop_requested(DrainRequest(time.monotonic(), 0.1, 0.1))
        engine.held_groups.clear()
        engine._release("j")
        assert engine._held_starts == [owed]  # the restart launches it
        engine.hold_group(third, 2**22 + 7)
        fourth = "1e2d3c4b-5a69-4788-9a0b-c1d2e3f4a5b6"
        open_attempt = OpenAttempt(fourth, "k", 1, 1, str(tmp_path / "spool"), 0, T0.isoformat())
        engine.held_at_restart(open_attempt, 2**22 + 7)
        engine._facts[fourth].finished = True  # a hold plan done while the group lives
        engine._forget_if_over(fourth)
        assert engine.blocked("k") == fourth  # kept: its group is still held

        def broken(_pgid: int) -> bool:
            raise OSError("ps")

        monkeypatch.setattr("dsl41.runner_stops.group_has_live_member", broken)
        await asyncio.sleep(GROUP_POLL_S + 0.1)
        assert isinstance(engine.fault, OSError)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    failing = subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="")
    monkeypatch.setattr(runner_adapters.subprocess, "run", lambda *_a, **_k: failing)
    monkeypatch.setattr(runner_adapters, "sys", SimpleNamespace(platform="darwin"))  # `ps`
    assert runner_adapters.group_has_live_member(2**22 + 7)


def test_a_replacement_waits_for_the_ladder_of_the_attempt_it_replaces(tmp_path: Path) -> None:
    """Astra R3-M1 live: the attempt ends while its ladder still runs, so its outcome is
    committed with `ladder: running` and its rerun waits. The ladder's end closes the attempt
    (`ladder_done`), and only then is attempt 2 dispatched."""
    run_root = tmp_path / "run"
    spawn = {"command_pgid": 2**22 + 7, "command_pid": 2**22 + 7, "command_start_time": "t"}

    async def scenario() -> None:
        engine = _engine(tmp_path)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(T0)
        _adopt(engine)
        engine.attempts.spawn_recorded(RUN_ID, spawn)
        engine.attempts.stop(RUN_ID, "update_stop")  # its ladder is a task not yet run
        engine.attempts.wrapper_exited(RUN_ID, spawn)  # the end lands while the ladder runs
        assert RUN_ID in engine._waiting_plans
        await asyncio.sleep(0.05)  # the ladder ends
        assert RUN_ID not in engine._waiting_plans
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    order = [(k, b.get("event") or b.get("ordinal")) for _, k, b in rows(run_root) if k != "input"]
    assert order.index(("follow_up", "ladder_done")) < order.index(("dispatch", 2))
    [outcome] = [b for _, k, b in rows(run_root) if k == "outcome"]
    assert outcome["ladder"] == "running"
