"""`update` wired into the engine (spec-reload "`dsl41 update`", DL-427): the admission as a step
of the single writer, the stops through the attempt tier, the dispatch filter, the application,
the store's `config intent` and `config applied`, the snapshot of what an update leaves behind,
replay across an update, a restart's step 4, and the two verbs over the control socket.

The engine scenarios run real wrappers on the real clock; assertions read the recorded order,
never elapsed time. A crash is the loop cancelled with no shutdown record, then a restart.
"""

from __future__ import annotations

from dsl41 import main_config as main

from reload_fixtures import prepare_estate, write_config

import asyncio
import contextlib
import json
import os
import re
import time
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from types import SimpleNamespace

import pytest
from store_surgery import rows
from update_wiring_barrier import (
    FAULT_DEADLINES_S,
    FAULT_WAIT_S,
    SCENARIO_S,
    READINESS_FAULTS,
    WATCHDOG_GRACE_S,
    RerunBarrier,
    Supervision,
    descendants,
    process_table,
)

from dsl41.canon import canonical_bytes, decode
from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event
from dsl41.reload_plan import reread
from dsl41.reload_update import Admitted, Refused
from dsl41.runner import Engine
from dsl41.runner_adapters import JobAdapter, LocalCommandAdapter
from dsl41.runner_admission import AdmissionRefused, Envelope, Frontiers
from dsl41.runner_clock import EngineError, RealClock
from dsl41.runner_config import EngineSettings, RuntimeProfile
from dsl41.reload_diff import catalog_hash_v2
from dsl41.runner_journal import (
    OpenTable,
    Position,
    decode_snapshot,
    encode_snapshot,
    oracle_rows,
    replay_store,
    restore_oracle,
)
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_store import MemoryStore, SqliteStore
from dsl41.runner_update import UpdateAnswer, UpdateRequest

GRACE = EngineSettings(grace_term_s=0.5, grace_kill_s=1.0)
TIMEOUT_S = 30.0
RECORD_WAITS = EngineSettings(grace_term_s=GRACE.grace_term_s, grace_kill_s=TIMEOUT_S)
T0 = datetime(2026, 7, 1, 1)


# ---------------------------------------------------------------- the snapshot (Astra M1, M2)


def _round_trip(oracle: Oracle, catalog: CatalogIR) -> Oracle:
    """`oracle` through the store's snapshot API: written, read back, restored over `catalog`."""
    store = MemoryStore()
    data = encode_snapshot(
        oracle,
        frontiers=Frontiers(),
        position=Position(),
        config_rev=1,
        open_table=OpenTable(),
    )
    from dsl41.runner_store import Record

    store.append(Record(T0, "engine", None, None, {"event": "started", "at": T0.isoformat()}))
    store.put_snapshot(1, 25, data)
    stored = store.latest_snapshot()
    assert stored is not None
    snapshot = decode_snapshot(stored.seq, stored.data, where="test")
    return restore_oracle(catalog, snapshot.rows, semantics=oracle.semantics)


def _swap(oracle: Oracle, catalog: CatalogIR) -> None:
    oracle.replace_catalog(catalog, semantics=oracle.semantics, tz_aliases=None, default_tz=None)


def _feed(oracle: Oracle, minutes: int, kind: str, **payload: object) -> None:
    oracle.feed(Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload))  # type: ignore[arg-type]


_C = "insert_job: c\njob_type: c\ncommand: x\nmachine: m1\n"
_BASE = "insert_job: k\njob_type: c\ncommand: x\nmachine: m1\n\n"


def test_a_held_job_removed_before_a_snapshot_and_re_added_after_keeps_its_hold_and_run() -> None:
    with_c, without_c = lower_source(_BASE + _C), lower_source(_BASE)
    oracle = Oracle(with_c)
    _feed(oracle, 0, "FORCE_STARTJOB", job="c")
    _feed(oracle, 1, "STATUS", job="c", run_number=1, exit_code=0)
    _feed(oracle, 2, "ON_HOLD", job="c")
    _swap(oracle, without_c)
    restored = _round_trip(oracle, without_c)
    assert restored.store.tombstones == oracle.store.tombstones
    _swap(restored, with_c)
    row = restored.store.job["c"]
    assert (row.on_hold, row.run_number, row.status) == (True, 1, "INACTIVE")
    _feed(restored, 3, "FORCE_STARTJOB", job="c")
    assert restored.store.job["c"].run_number == 2


_HOLDER = (
    "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=A)\n\n"
)
_QUEUE = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    + _HOLDER
    + "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=A)\n"
)


def test_a_waiter_whose_holder_was_removed_before_a_snapshot_wakes_as_it_does_uninterrupted() -> (
    None
):
    without_a = lower_source(_QUEUE.replace(_HOLDER, ""))
    oracles = []
    for _ in range(2):
        oracle = Oracle(lower_source(_QUEUE))
        _feed(oracle, 0, "FORCE_STARTJOB", job="a")
        _feed(oracle, 0, "FORCE_STARTJOB", job="b")
        _swap(oracle, without_a)
        oracles.append(oracle)
    uninterrupted, restored = oracles[0], _round_trip(oracles[1], without_a)
    assert restored.store.job["b"].status == "QUE_WAIT"
    for oracle in (uninterrupted, restored):
        oracle.advance(T0 + timedelta(seconds=1))
    assert restored.store.job["b"] == uninterrupted.store.job["b"]
    assert restored.store.job["b"].status == "RUNNING"


_TOKENS = "insert_resource: TOKENS\nres_type: D\namount: 5\n\n"
_SPENDER = "insert_job: s\njob_type: c\ncommand: x\nmachine: m1\n"


def test_two_spent_depletable_units_survive_a_drop_a_snapshot_and_a_re_add() -> None:
    with_tokens = lower_source(_TOKENS + _SPENDER + "resources: (TOKENS, QUANTITY=2)\n")
    without = lower_source(_SPENDER)
    oracle = Oracle(with_tokens)
    _feed(oracle, 0, "FORCE_STARTJOB", job="s")
    assert oracle.store.job["s"].reservations
    _swap(oracle, without)
    assert set(oracle.ghost_resources()) == {"TOKENS"}
    restored = _round_trip(oracle, without)
    assert set(restored.ghost_resources()) == {"TOKENS"}
    _feed(restored, 1, "STATUS", job="s", run_number=1, exit_code=0)
    assert dict(restored.store.consumed) == {"r:TOKENS": 2}
    _feed(restored, 2, "RELEASE_RESOURCE", job="s")
    assert dict(restored.store.consumed) == {"r:TOKENS": 2}
    _swap(restored, lower_source(_TOKENS + _SPENDER))
    assert dict(restored.store.consumed) == {"r:TOKENS": 2}


def test_the_carried_rows_keep_the_ghosts_through_canonical_bytes() -> None:
    oracle = Oracle(lower_source(_TOKENS + _SPENDER + "resources: (TOKENS, QUANTITY=2)\n"))
    _feed(oracle, 0, "FORCE_STARTJOB", job="s")
    _swap(oracle, lower_source(_SPENDER))
    rows_ = CarriedRows.model_validate(decode(canonical_bytes(oracle_rows(oracle))))
    assert set(rows_.ghosts) == {"TOKENS"} and rows_.wake_owed


# ---------------------------------------------------------------- the engine


#: The job every other one depends on, which no scenario starts: strict lint takes no job of a
#: scenario for a dangling one (L011).
ANCHOR = "insert_job: anchor\njob_type: c\ncommand: true\n\n"


def _jil(jobs: dict[str, str], extra: str = "") -> str:
    return ANCHOR + "".join(
        f"insert_job: {job}\njob_type: c\ncommand: {cmd}\ncondition: s(anchor)\n{extra}\n"
        for job, cmd in jobs.items()
    )


class Estate:
    """An estate directory the engine runs and `reread` reads: a main config, one JIL file and
    `non-idempotent.toml`."""

    def __init__(self, root: Path, text: str, marked: dict[str, str] | None = None) -> None:
        self.dir = root / "estate"
        self.dir.mkdir(parents=True, exist_ok=True)
        write_config(self.dir)
        self.write(text, marked)

    def write(self, text: str, marked: dict[str, str] | None = None) -> None:
        (self.dir / "jobs.jil").write_text(text)
        lines = "".join(f'"{job}" = "{why}"\n' for job, why in (marked or {}).items())
        (self.dir / "non-idempotent.toml").write_text(f"[jobs]\n{lines}" if lines else "")

    @property
    def catalog(self) -> CatalogIR:
        return prepare_estate(self.dir).catalog

    @property
    def marked(self) -> dict[str, str]:
        from dsl41.idempotence import load_exceptions

        return dict(load_exceptions(self.dir).reasons)


def _adapters(settings: EngineSettings) -> dict[str, JobAdapter]:
    return {"CMD": cast(JobAdapter, LocalCommandAdapter(grace_seconds=settings.grace_term_s))}


def _start(
    run_root: Path,
    estate: Estate,
    *,
    scheduler: Scheduler | None = None,
    settings: EngineSettings = GRACE,
) -> Engine:
    return start_run(
        estate.catalog,
        run_root,
        clock=RealClock(),
        adapters=_adapters(settings),
        scheduler=scheduler,
        hold_open=True,
        settings=settings,
        non_idempotent=estate.marked,
        source_capture=main.encoded_parts(prepare_estate(estate.dir)),
        launched=main.profile_of(prepare_estate(estate.dir)),
    )


async def _resume(run_root: Path, estate: Estate) -> Engine:
    return await resume_run(
        estate.catalog,
        run_root,
        clock=RealClock(),
        adapters=_adapters(GRACE),
        hold_open=True,
        settings=GRACE,
    )


def _start_jobs(engine: Engine, *jobs: str) -> None:
    for job in jobs:
        engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))


async def _until(predicate: Callable[[], object]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


async def _running(engine: Engine, *jobs: str, ordinal: int = 1) -> None:
    await _until(lambda: all(engine.attempt_view(job) is not None for job in jobs))
    run_root = engine.run_root
    assert run_root is not None
    for job in jobs:
        await _until((run_root / "runs" / f"{job}.1" / str(ordinal) / "spawn.json").exists)


async def _finish(engine: Engine, loop: asyncio.Future[Any]) -> None:
    original: BaseException | None = None
    try:
        async with asyncio.timeout(TIMEOUT_S):
            if loop.done():
                await loop
            if not engine.draining:
                stop = engine.request_stop()
                done, _ = await asyncio.wait({stop, loop}, return_when=asyncio.FIRST_COMPLETED)
                if loop in done:
                    await loop
                if not stop.done():
                    stop.cancel()
                else:
                    stop.result()
            await loop
    except BaseException as exc:
        original = exc
        raise
    finally:
        try:
            async with asyncio.timeout(TIMEOUT_S):
                await engine.shutdown()
        except BaseException as exc:
            if original is None:
                raise
            original.add_note("cleanup: " + repr(exc))
        finally:
            assert engine.journal is not None
            engine.journal.close()


async def _crash(engine: Engine, loop: asyncio.Future[Any]) -> None:
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _plan(engine: Engine, estate: Estate) -> str:
    assert engine.run_root is not None
    return reread(
        engine.update.live(), engine.run_root / "plans", candidate=prepare_estate(estate.dir)
    ).token


def _request(engine: Engine, token: str, *, rid: str = "r1") -> UpdateRequest:
    return UpdateRequest(token, engine.config_rev, engine.baseline_id, rid)


async def _update(
    engine: Engine, token: str, *, rid: str = "r1", expect: int | None = None
) -> UpdateAnswer:
    request = _request(engine, token, rid=rid)
    if expect is not None:
        request = UpdateRequest(token, expect, engine.baseline_id, rid)
    return await engine.request_update(request)


async def _applied(answer: UpdateAnswer) -> dict[str, Any]:
    assert answer.applied is not None
    async with asyncio.timeout(TIMEOUT_S):
        return await answer.applied


def _kinds(run_root: Path, kind: str) -> list[dict[str, Any]]:
    return [body for _, k, body in rows(run_root) if k == kind]


def _configs(run_root: Path) -> list[dict[str, Any]]:
    return _kinds(run_root, "config")


def test_two_running_jobs_are_stopped_and_eighteen_untouched_with_the_engine_running(
    tmp_path: Path,
) -> None:
    """spec-reload: a plan that changes two running jobs stops them;
    the two are stopped with cause `update_stop`, the eighteen others run on, the engine stays
    RUNNING throughout, and each stopped idempotent job reruns the same run under its new
    definition once the update applied."""
    jobs = {f"j{i:02}": "sleep 30" for i in range(20)}
    estate = Estate(tmp_path, _jil(jobs))
    run_root = tmp_path / "run"
    marker = tmp_path / "new"
    marker.mkdir()
    states: list[str] = []

    async def scenario() -> tuple[UpdateAnswer, UpdateAnswer, dict[str, Any], Engine]:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, *jobs)
        await _running(engine, *jobs)
        changed = dict(jobs, **{j: f"touch {marker}/{j}; exec sleep 30" for j in ("j03", "j07")})
        estate.write(_jil(changed))
        token = _plan(engine, estate)
        admitted = await _update(engine, token)
        states.append(engine.lifecycle.state)
        body = await _applied(admitted)
        states.append(engine.lifecycle.state)
        for job in ("j03", "j07"):
            await _until((marker / job).exists)
        await _running(engine, "j03", "j07", ordinal=2)
        await _finish(engine, loop)
        return admitted, body, engine

    admitted, body, engine = asyncio.run(scenario())
    assert isinstance(admitted.decision, Admitted)
    assert admitted.decision.stop_set == ("j03", "j07")
    assert states == ["RUNNING", "RUNNING"]
    assert (body["rev"], body["changed"], body["stopped"]) == (
        2,
        ["j03", "j07"],
        [["j03", "rerun"], ["j07", "rerun"]],
    )
    [recorded] = [c for c in _configs(run_root) if c["event"] == "applied" and "token" in c]
    assert recorded == body
    # the stops' outcomes are committed after the application, each with its rerun plan
    order = [(k, b.get("event"), b.get("job")) for _, k, b in rows(run_root)]
    applied_at = order.index(("config", "applied", None))
    stopped = [i for i, (k, _, job) in enumerate(order) if k == "outcome" and job in ("j03", "j07")]
    assert stopped and min(stopped) > applied_at
    outcomes: dict[str, list[tuple[str, str]]] = {}
    for o in _kinds(run_root, "outcome"):
        outcomes.setdefault(o["job"], []).append((o["outcome"], o["cause"]))
    for job in ("j03", "j07"):
        # the update's stop, then the drain's stop of the rerun
        assert outcomes[job] == [("stopped", "update_stop"), ("stopped", "engine_stop")]
    plans = [(p["job"], p["plan"]) for p in _kinds(run_root, "follow_up") if p["event"] == "plan"]
    assert plans[:2] == [("j03", "rerun"), ("j07", "rerun")]
    # the eighteen ran on until the drain stopped them with its own cause
    untouched = [o for j, o in outcomes.items() if j not in ("j03", "j07")]
    assert untouched == [[("stopped", "engine_stop")]] * 18
    second = [d for d in _kinds(run_root, "dispatch") if d.get("ordinal") == 2]
    assert sorted(d["job"] for d in second) == ["j03", "j07"]
    assert all(d["after"] == "update_stop" for d in second)
    assert engine.config_rev == 2


async def _barrier_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: str, barrier: RerunBarrier
) -> Engine:
    from dsl41 import runner_adapters

    monkeypatch.setattr(
        runner_adapters, "_WRAPPER_PATH", Path(__file__).with_name("update_wiring_wrapper.py")
    )
    jobs = {f"j{i:02}": "exec sleep 300" for i in range(20)}
    updated = ("j03", "j07")
    estate = Estate(tmp_path, _jil(jobs))
    engine = _start(tmp_path / "run", estate, settings=RECORD_WAITS)
    engine.adapters["CMD"] = cast(JobAdapter, barrier)
    supervision = Supervision(engine, barrier)
    barrier.supervision = supervision
    original: BaseException | None = None
    try:
        async with asyncio.timeout(SCENARIO_S):
            _start_jobs(engine, *jobs)
            await supervision.wait(barrier.initial_ready.wait(), "initial-spawn-records")
            estate.write(_jil(dict(jobs, **{j: "exec sleep 301" for j in updated})))
            answer = await supervision.wait(
                _update(engine, _plan(engine, estate)), "update-admission"
            )
            assert isinstance(answer.decision, Admitted)
            assert answer.decision.stop_set == updated
            applied = await supervision.wait(_applied(answer), "update-applied")
            assert (applied["rev"], applied["changed"], applied["stopped"]) == (
                2,
                list(updated),
                [[j, "rerun"] for j in updated],
            )
            if barrier.fault == "late-registration":
                await supervision.wait(barrier.arrived.wait(), "late-registration-arrival")
                assert not barrier.launches
                barrier.resume()
                barrier.registration.set()
            if barrier.fault in READINESS_FAULTS:
                await supervision.wait(barrier.arrived.wait(), "readiness-withheld-entered")
                assert len(barrier.launches) == 2 and not barrier.ready.is_set()
            await supervision.wait(
                barrier.ready.wait(),
                "rerun-readiness",
                timeout=(FAULT_WAIT_S if barrier.fault in READINESS_FAULTS else SCENARIO_S),
            )
            assert set(barrier.launches) == {"j03.1", "j07.1"}
            for job in updated:
                spawn = tmp_path / "run/runs" / f"{job}.1" / "2/spawn.json"
                assert spawn.exists() is (pause == "post_record")
            assert engine.lifecycle.state == "RUNNING"
            assert await supervision.wait(engine.request_stop(), "drain-admission") == "DRAINING"
            for launch in barrier.launches.values():
                assert launch.spec_sent
                assert (launch.lifeline_w is None) is barrier.before_record
            barrier.resume()
            if barrier.fault == "withheld-exit":
                await supervision.wait(barrier.exit_withheld.wait(), "exit-withheld-entered")
                assert len(barrier.exited) == 2 and not barrier.exit_ack.is_set()
            await supervision.wait(
                supervision.task,
                "wrapper-exit-acknowledgement",
                timeout=(FAULT_WAIT_S if barrier.fault == "withheld-exit" else SCENARIO_S),
            )
    except BaseException as exc:
        original = exc
        exc.add_note(supervision.diagnostic())
        raise
    finally:
        try:
            await supervision.cleanup()
        except BaseException as exc:
            if original is None:
                exc.add_note(supervision.diagnostic())
                raise
            original.add_note("cleanup: " + repr(exc) + "; " + supervision.diagnostic())
    return engine


@pytest.mark.parametrize(
    ("pause", "cause"),
    [("post_spawn_pre_record", "lifeline"), ("post_record", "engine_stop")],
    ids=["pre-record", "post-record"],
)
def test_draining_updated_reruns_on_each_side_of_the_spawn_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pause: str, cause: str
) -> None:
    """Pause after command creation on each side of spawn-record publication (DL-359).

    Both orderings keep the update's two replacements and the other eighteen attempts
    distinct, with exact stop causes and no surviving commands or wrappers.
    """
    jobs = {f"j{i:02}": "exec sleep 300" for i in range(20)}
    updated = ("j03", "j07")
    run_root = tmp_path / "run"
    barrier = RerunBarrier(tmp_path / "barrier", before_record=pause == "post_spawn_pre_record")
    engine = asyncio.run(_barrier_scenario(tmp_path, monkeypatch, pause, barrier))
    outcomes: dict[str, list[tuple[str, str]]] = {}
    for outcome in _kinds(run_root, "outcome"):
        outcomes.setdefault(outcome["job"], []).append((outcome["outcome"], outcome["cause"]))
    assert set(outcomes) == set(jobs)
    for job in updated:
        assert outcomes[job] == [("stopped", "update_stop"), ("stopped", cause)]
    for job in set(jobs) - set(updated):
        assert outcomes[job] == [("stopped", "engine_stop")]
    dispatches = _kinds(run_root, "dispatch")
    assert sorted((d["job"], d["ordinal"]) for d in dispatches) == sorted(
        [(j, 1) for j in jobs] + [(j, 2) for j in updated]
    )
    assert all(d["after"] == "update_stop" for d in dispatches if d["ordinal"] == 2)
    assert (engine.config_rev, engine.lifecycle.state, engine.exit_code, engine.leaving_record) == (
        2,
        "STOPPED",
        0,
        "clean_shutdown",
    )
    assert engine.held_groups == {}
    assert all(engine.attempt_view(j) is None for j in jobs)
    spawns = list((run_root / "runs").glob("*/*/spawn.json"))
    assert len(spawns) == 22
    for path in spawns:
        spawn = json.loads(path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(spawn["wrapper_pid"], 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(spawn["command_pgid"], 0)
        status = json.loads(path.with_name("status.json").read_text())
        if path.parent.name == "2" and barrier.before_record:
            assert (status["outcome"], status["cause"]) == ("terminated", "parent lost")
        else:
            assert (status["outcome"], status["signal"]) == ("signaled", signal.SIGTERM)


@pytest.mark.parametrize(
    "fault",
    [
        "no-waitid",
        "observer-failure",
        "late-registration",
        "withheld-readiness",
        "withheld-exit",
        "inventory-failure",
    ],
)
def test_barrier_faults_finish_under_an_external_watchdog(tmp_path: Path, fault: str) -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("update_wiring_faults.py")),
        fault,
        str(tmp_path),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        output, _ = process.communicate(timeout=FAULT_DEADLINES_S[fault] + WATCHDOG_GRACE_S)
    except subprocess.TimeoutExpired:
        table = process_table()
        owned = descendants(table, {process.pid})
        wrappers = set()
        inventory = tmp_path / "barrier/owned.json"
        if inventory.exists():
            wrappers = {
                pid
                for pid in json.loads(inventory.read_text())
                if pid in table and table[pid][0] == process.pid
            }
        for pid in owned - {process.pid}:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
                if pid not in wrappers:
                    os.kill(pid, signal.SIGKILL)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for pid in wrappers:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            process.kill()
            output, _ = process.communicate(timeout=5)
        pytest.fail(f"external watchdog: {fault}; ps={table}; owned={owned}; {output}")
    assert process.returncode == 0, output
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["survivors"] == []
    assert not (set(report["owned"]) & process_table().keys())
    assert report["released"] and report["engine_task_done"] and report["shutdown_done"]
    assert report["wrappers_reaped"] and report["open_fifos"] == []
    diagnostic = report["diagnostic"]
    assert all(
        key in diagnostic
        for key in ("phase", "attempts", "pid", "engine_task_exception", "outstanding_waits")
    )
    if fault == "observer-failure":
        assert report["exception"] == "RuntimeError('injected observer failure')"
        assert "injected observer failure" in diagnostic
        assert report["engine_exception_preserved"]
    elif fault in READINESS_FAULTS | {"withheld-exit"}:
        assert report["exception_type"] == "TimeoutError"
        assert report["failed_phase"] == (
            "rerun-readiness" if fault in READINESS_FAULTS else "wrapper-exit-acknowledgement"
        )
        assert report["failed_phase"] in diagnostic
        blocked = "release" if fault in READINESS_FAULTS else "exit-acknowledgement"
        assert f'"wait": "{blocked}"' in diagnostic
    else:
        assert report["exception"] is None
    if fault == "inventory-failure":
        assert "TimeoutExpired" in diagnostic and "CalledProcessError" in diagnostic
        assert len(report["inventory_errors"]) >= 2
        assert "cleanup:" in diagnostic
    else:
        assert report["inventory_errors"] == []


def test_closed_fifo_objects_remain_closed_when_their_fd_numbers_are_reused(tmp_path: Path) -> None:
    barrier = RerunBarrier(tmp_path / "barrier", before_record=True)
    read_fd, write_fd = os.pipe()
    barrier.pipes[tmp_path / "attempt"] = (
        os.fdopen(read_fd, "rb", buffering=0),
        os.fdopen(write_fd, "wb", buffering=0),
    )
    try:
        assert len(barrier.open_fifos) == 2
        with open(os.devnull, "rb") as unrelated:
            barrier.close()
            os.dup2(unrelated.fileno(), read_fd)
            try:
                barrier.close()
                assert barrier.open_fifos == []
                assert os.read(read_fd, 1) == b""
            finally:
                os.close(read_fd)
    finally:
        barrier.close()


def test_cleanup_signals_live_wrapper_handles_and_records_unproven_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[int] = []
    live = SimpleNamespace(pid=101, returncode=None, kill=lambda: killed.append(101))
    exited = SimpleNamespace(pid=102, returncode=0, kill=lambda: killed.append(102))

    def reject_raw_pid_signal(*args: Any) -> None:
        pytest.fail(f"signal by remembered pid: {args}")

    monkeypatch.setattr(os, "kill", reject_raw_pid_signal)
    supervision = Supervision.__new__(Supervision)
    supervision.barrier = cast(RerunBarrier, SimpleNamespace(owned={101, 102, 103}))
    supervision.skipped_signals = []
    supervision.kill_wrappers(
        [cast(asyncio.subprocess.Process, live), cast(asyncio.subprocess.Process, exited)]
    )
    assert killed == [101]
    assert supervision.skipped_signals == [
        {"pid": 102, "reason": "no live process handle"},
        {"pid": 103, "reason": "no live process handle"},
    ]


def test_a_plan_job_the_oracle_starts_while_the_update_stops_waits_for_the_application(
    tmp_path: Path,
) -> None:
    """The dispatch filter: while the update stops a job that ignores TERM, the oracle starts a
    job the plan changes; the start is journaled `dispatch_pending` after the intent and is
    dispatched only after `config applied`, under the new definition."""
    slow = "trap '' TERM; sleep 30 & wait"
    estate = Estate(tmp_path, _jil({"slow": slow, "dep": "sleep 30"}))
    run_root = tmp_path / "run"
    marker = tmp_path / "dep.new"
    seen: list[object] = []

    async def scenario() -> Engine:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "slow")
        await _running(engine, "slow")
        estate.write(_jil({"slow": "sleep 31", "dep": f"touch {marker}; exec sleep 30"}))
        token = _plan(engine, estate)
        answer = await _update(engine, token)
        seen.append(engine.update.view())
        # the decision is answered once the start it decided is dispatched or held
        decided = await engine.submit(
            Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": "dep"}),
            Envelope(request_id="dep", expect={"job:dep": engine.oracle.store.revision("job:dep")}),
        )
        seen.append(decided.decision)
        seen.append(engine.attempt_view("dep"))
        await _applied(answer)
        await _until(marker.exists)
        await _finish(engine, loop)
        return engine

    asyncio.run(scenario())
    view, decision, attempt = seen
    assert isinstance(view, dict) and view["stopping"] == ["slow"] and decision == "applied"
    assert attempt is None  # started in the oracle, launched nowhere
    order = [(k, b.get("event"), b.get("job"), b.get("ordinal")) for _, k, b in rows(run_root)]
    intent = order.index(("config", "intent", None, None))
    applied = order.index(("config", "applied", None, None), intent)
    updates = [c for c in _configs(run_root) if c["event"] == "applied" and "token" in c]
    assert [c["rev"] for c in updates] == [2]  # this token's one application
    held = order.index(("dispatch_pending", "pending", "dep", None))
    dispatched = order.index(("dispatch", None, "dep", 1))
    assert intent < held < applied < dispatched


def test_a_stale_token_and_an_open_update_are_refused_and_a_retry_is_answered_once(
    tmp_path: Path,
) -> None:
    estate = Estate(tmp_path, _jil({"a": "sleep 30", "b": "sleep 30"}))
    run_root = tmp_path / "run"
    answers: dict[str, UpdateAnswer] = {}

    async def scenario() -> None:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        estate.write(_jil({"a": "sleep 31", "b": "sleep 30"}))
        first = _plan(engine, estate)
        estate.write(_jil({"a": "sleep 31", "b": "sleep 31"}))
        second = _plan(engine, estate)
        answers["first"] = await _update(engine, first, rid="one")
        await _applied(answers["first"])
        # An exact retry uses identity, independent of a later prepared candidate (DL-463).
        answers["retry"] = await _update(engine, first, rid="one", expect=1)
        answers["stale"] = await _update(engine, second, rid="two")
        answers["expect"] = await _update(engine, second, rid="three", expect=1)
        answers["reused"] = await _update(engine, second, rid="one")
        answers["no_plan"] = await _update(engine, "0" * 64, rid="four")
        await _finish(engine, loop)

    asyncio.run(scenario())
    assert isinstance(answers["first"].decision, Admitted)
    retry = answers["retry"]
    assert retry.decision == answers["first"].decision
    assert retry.application is not None and retry.application["rev"] == 2
    for name in ("stale", "expect"):
        decision = answers[name].decision
        assert isinstance(decision, Refused) and decision.code == "stale_plan"
    assert answers["reused"].application == retry.application
    assert answers["no_plan"].code == "invalid_argument"
    assert [c["event"] for c in _configs(run_root)] == ["applied", "intent", "applied"]


def test_a_box_that_started_since_preparation_is_restarted(tmp_path: Path) -> None:
    box = ANCHOR + "insert_job: bx\njob_type: b\ncondition: s(anchor)\n\n"
    member = "insert_job: m\njob_type: c\ncommand: sleep 30\nbox_name: bx\n\n"
    estate = Estate(tmp_path, box + member)
    run_root = tmp_path / "run"
    answers: list[UpdateAnswer] = []

    async def scenario() -> None:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        estate.write(box + member + "insert_job: n\njob_type: c\ncommand: sleep 1\nbox_name: bx\n")
        token = _plan(engine, estate)
        _start_jobs(engine, "bx")
        await _until(lambda: engine.oracle.store.job["bx"].status == "RUNNING")
        answers.append(await _update(engine, token))
        await _finish(engine, loop)

    asyncio.run(scenario())
    [answer] = answers
    assert isinstance(answer.decision, Admitted)


def test_a_changed_job_gets_a_rebuilt_tick_and_an_unchanged_one_keeps_its_own(
    tmp_path: Path,
) -> None:
    def scheduled(times: dict[str, str]) -> str:
        return "".join(
            f"insert_job: {job}\njob_type: c\ncommand: sleep 1\ndate_conditions: 1\n"
            f'days_of_week: all\nstart_times: "{at}"\n\n'
            for job, at in times.items()
        )

    estate = Estate(tmp_path, scheduled({"kept": "03:00", "moved": "04:00"}))
    run_root = tmp_path / "run"
    ticks: list[dict[str, datetime]] = []

    async def scenario() -> None:
        now = RealClock().now()
        scheduler = Scheduler(estate.catalog, start=now, default_tz="UTC")
        engine = _start(run_root, estate, scheduler=scheduler)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        assert engine.scheduler is not None
        ticks.append({job: at for at, job in engine.scheduler.upcoming()})
        estate.write(scheduled({"kept": "03:00", "moved": "05:00"}))
        await _applied(await _update(engine, _plan(engine, estate)))
        ticks.append({job: at for at, job in engine.scheduler.upcoming()})
        await _finish(engine, loop)

    asyncio.run(scenario())
    before, after = ticks
    assert after["kept"] == before["kept"]
    assert (after["moved"] - before["moved"]) % timedelta(days=1) == timedelta(hours=1)


def test_a_crash_between_intent_and_application_resumes_to_applied_before_any_dispatch(
    tmp_path: Path,
) -> None:
    """spec-crash step 4: the engine dies while the update stops a job that ignores TERM. The
    restart resumes the update STOPPING, observes its attempts in the catch-up, and applies it as a
    loop step before any dispatch of a plan job: the removed job is not rerun, the changed
    idempotent one is, a marked one is held as interrupted, and a running job whose mark the
    plan adds is held under the applied marked set."""
    slow = "trap '' TERM; sleep 30 & wait"
    text = _jil({"gone": slow, "idem": slow, "held": slow, "other": slow, "newmark": slow})
    estate = Estate(tmp_path, text)
    run_root = tmp_path / "run"
    slow_grace = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)

    async def crash() -> None:
        engine = start_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(slow_grace),
            hold_open=True,
            settings=slow_grace,
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "gone", "idem", "held", "other", "newmark")
        await _running(engine, "gone", "idem", "held", "other", "newmark")
        estate.write(
            # `newmark` is left running and marked: the crash interrupts it, and the restart
            # chooses its plan under the applied marked set
            _jil({"idem": "sleep 31", "held": "sleep 31", "other": slow, "newmark": slow}),
            {"held": "posts twice", "newmark": "posts twice"},
        )
        answer = await _update(engine, _plan(engine, estate))
        assert isinstance(answer.decision, Admitted)
        await _crash(engine, loop)

    asyncio.run(crash())
    assert [c["event"] for c in _configs(run_root)] == ["applied", "intent"]

    async def restart() -> Engine:
        engine = await _resume(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await _until(lambda: engine.attempt_view("idem") is not None)
        await _until(lambda: engine.oracle.store.job["held"].on_hold)
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    order = [(k, b.get("event"), b.get("job")) for _, k, b in rows(run_root)]
    # genesis's rev, then the update's, recorded once, after the intent and before any dispatch
    # of a plan job
    applied = [i for i, o in enumerate(order) if o[:2] == ("config", "applied")]
    intent = order.index(("config", "intent", None))
    assert len(applied) == 2 and intent < applied[1]
    plan_jobs = ("gone", "idem", "held", "newmark")
    after_crash = order[intent:]
    first_dispatch = next(
        i for i, o in enumerate(after_crash) if o[0] == "dispatch" and o[2] in plan_jobs
    )
    assert after_crash.index(("config", "applied", None)) < first_dispatch
    dispatched = [job for k, _, job in after_crash if k == "dispatch"]
    assert "gone" not in dispatched and "idem" in dispatched and "held" not in dispatched
    held = engine.oracle.store.job["held"]
    assert (held.status, held.on_hold) == ("FAILURE", True)
    # the stop's exit was read as the update's stop, never as a natural FAILURE: the hold
    # carries the interruption's evidence, and the job is not run again
    assert engine.evidence["held"]["cause"] in ("update_stop", "lifeline")
    assert held.run_number == 1
    assert "gone" in engine.oracle.store.tombstones
    assert engine.config_rev == 2
    plans = {p["job"]: p["plan"] for p in _kinds(run_root, "follow_up") if p["event"] == "plan"}
    assert (plans["gone"], plans["idem"], plans["held"]) == ("none", "rerun", "hold")
    # `newmark`'s interruption was kept until the application, then held under its new mark
    held_at = next(i for i, o in enumerate(order) if o[0] == "outcome" and o[2] == "newmark")
    assert held_at > applied[1]
    assert plans["newmark"] == "hold" and "newmark" not in dispatched


def test_a_restart_after_an_update_runs_the_applied_configuration_and_replays_across_it(
    tmp_path: Path,
) -> None:
    """The store's configuration rows are the record: a restart after an update runs rev 2,
    replays the inputs before the update under rev 1 and those after under rev 2, and refuses
    an estate on disk that differs from rev 2. A reader's replay ends where the engine did."""
    estate = Estate(tmp_path, _jil({"a": "sleep 1", "b": "sleep 1"}))
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "a")
        await _until(lambda: engine.oracle.store.job["a"].status == "SUCCESS")
        estate.write(_jil({"b": "sleep 1", "c": "sleep 1"}))
        await _applied(await _update(engine, _plan(engine, estate)))
        _start_jobs(engine, "c")
        await _until(lambda: engine.oracle.store.job["c"].status == "SUCCESS")
        await _crash(engine, loop)

    asyncio.run(first())

    async def second() -> Engine:
        engine = await _resume(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(second())
    assert engine.config_rev == 2 and set(engine.oracle.catalog.jobs) == {"anchor", "b", "c"}
    assert engine.oracle.store.job["c"].status == "SUCCESS"
    assert engine.oracle.store.tombstones["a"].status == "SUCCESS"
    store = SqliteStore(run_root / "engine.db")
    try:
        replayed = replay_store(store, where="test")
    finally:
        store.close()
    assert replayed.replay.rev == 2
    assert replayed.oracle.store.job["c"] == engine.oracle.store.job["c"]
    assert replayed.oracle.store.tombstones == engine.oracle.store.tombstones

    async def refused() -> None:
        estate.write(_jil({"b": "sleep 2", "c": "sleep 1"}))
        await _resume(run_root, estate)

    with pytest.raises(EngineError, match="catalog hash mismatch"):
        asyncio.run(refused())


def test_a_snapshot_taken_while_the_update_stops_carries_it(tmp_path: Path) -> None:
    slow = "trap '' TERM; sleep 30 & wait"
    estate = Estate(tmp_path, _jil({"slow": slow}))
    run_root = tmp_path / "run"
    slow_grace = EngineSettings(grace_term_s=5.0, grace_kill_s=1.0)

    async def crash() -> None:
        engine = start_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(slow_grace),
            hold_open=True,
            settings=slow_grace,
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "slow")
        await _running(engine, "slow")
        estate.write(_jil({"slow": "sleep 31"}))
        await _update(engine, _plan(engine, estate))
        engine.take_snapshot()
        await _crash(engine, loop)

    asyncio.run(crash())
    store = SqliteStore(run_root / "engine.db")
    try:
        stored = store.latest_snapshot()
        assert stored is not None
        snapshot = decode_snapshot(stored.seq, stored.data, where="test")
    finally:
        store.close()
    assert snapshot.update is not None and snapshot.open.update is not None
    [held] = snapshot.open.attempts.values()
    assert held.stop == "update_stop"
    assert snapshot.open.floor() is not None
    assert snapshot.open.floor() <= snapshot.update["seq"]

    plans = run_root / "plans"
    plans.rename(run_root / "aside")
    with pytest.raises(EngineError, match="its plan cannot be read"):
        asyncio.run(_resume(run_root, estate))
    (run_root / "aside").rename(plans)

    async def other_machine() -> None:
        await resume_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(GRACE),
            settings=GRACE,
            declared=main.profile_of(prepare_estate(estate.dir)).model_copy(
                update={"as_machine": ("elsewhere",)}
            ),
        )

    with pytest.raises(EngineError, match="runtime-profile mismatch on as_machine"):
        asyncio.run(other_machine())

    async def other_window() -> None:
        from dsl41.runner_adapters import FileWatcherAdapter

        adapters = {**_adapters(GRACE), "FW": FileWatcherAdapter(default_interval_s=99)}
        await resume_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=cast("dict[str, JobAdapter]", adapters),
            settings=GRACE,
        )

    with pytest.raises(EngineError, match="runtime-profile mismatch on fw_default_interval_us"):
        asyncio.run(other_window())
    refusals: list[str] = []

    async def restart() -> Engine:
        engine = await resume_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(GRACE),
            scheduler=Scheduler(estate.catalog, start=RealClock().now(), default_tz="UTC"),
            hold_open=True,
            settings=GRACE,
            declared=main.profile_of(prepare_estate(estate.dir)),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await _until(engine.started.is_set)
        # a fresh index learns the update's request id from the store: a sendevent under it
        # reuses the id
        try:
            await engine.submit(
                Event(at=engine.clock.now(), kind="ON_HOLD", payload={"job": "slow"}),
                Envelope(request_id="r1", expect={"job:slow": 99}),
            )
        except AdmissionRefused as exc:
            refusals.append(exc.code)
        await _finish(engine, loop)
        return engine

    engine = asyncio.run(restart())
    assert engine.config_rev == 2
    assert refusals == ["request_id_reused"]
    assert engine.scheduler is not None
    assert engine.scheduler.catalog_hash == catalog_hash_v2(engine.oracle.catalog)


def test_an_input_naming_a_removed_job_is_refused_before_the_oracle(tmp_path: Path) -> None:
    estate = Estate(tmp_path, _jil({"a": "sleep 1", "b": "sleep 1"}))
    run_root = tmp_path / "run"
    errors: list[BaseException] = []

    async def scenario() -> None:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        estate.write(_jil({"b": "sleep 1"}))
        await _applied(await _update(engine, _plan(engine, estate)))
        expect = {"job:a": 0}
        try:
            await engine.submit(
                Event(at=engine.clock.now(), kind="STARTJOB", payload={"job": "a"}),
                Envelope(request_id="x", expect=expect),
            )
        except AdmissionRefused as exc:
            errors.append(exc)
        engine.inject(
            Event(at=engine.clock.now(), kind="STATUS", payload={"job": "a", "run_number": 0}),
            source="adapter",
        )
        await _until(lambda: engine.drops)  # the engine-made input is rejected, not applied
        await _finish(engine, loop)

    asyncio.run(scenario())
    [error] = errors
    assert isinstance(error, AdmissionRefused) and error.code == "unknown_job"
    decisions = [d for d in _kinds(run_root, "decision") if d.get("code") == "stale_completion"]
    assert decisions and "removed by an update" in decisions[-1]["reason"]


# ---------------------------------------------------------------- the verbs over the socket


def test_reread_and_update_over_the_control_socket(short_root: Path) -> None:
    from typer.testing import CliRunner
    from dsl41.cli import app
    from dsl41.runner_control import roundtrip
    from test_main_config import JOB, estate_file
    from test_main_config_restart import cli_engine

    path = estate_file(short_root, estate='non_idempotent = "non-idempotent.toml"\n')
    (short_root / "non-idempotent.toml").write_text('[jobs]\nC = "synthetic finished effect"\n')
    box = (
        "insert_job: BX\njob_type: b\n"
        + JOB.replace("A", "B").replace("true", "sleep 30")
        + "box_name: BX\n"
        + JOB.replace("A", "C")
        + "box_name: BX\n"
    )
    source = short_root / "a.jil"
    source.write_text(JOB.replace("true", "sleep 30") + box)
    root = short_root / "run"
    root.mkdir()
    (root / "engine.toml").write_text("grace_term_s = 0.01\ngrace_kill_s = 0.01\n")
    sock = root / "control.sock"
    runner = CliRunner()
    with cli_engine(path) as ask:
        status = ask("status")
        start = roundtrip(
            sock,
            {
                "cmd": "sendevent",
                "verb": "FORCE_STARTJOB",
                "payload": {"job": "A"},
                "baseline_id": status["baseline_id"],
                "request_id": "start",
                "expect": {"job:A": status["jobs"]["A"]["state_rev"]},
            },
        )
        assert start["ok"], start
        deadline = time.monotonic() + 10
        while ask("status")["jobs"]["A"]["status"] != "RUNNING":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        source.write_text(JOB.replace("true", "sleep 31") + box)
        preview = runner.invoke(app, ["reread", "-S", str(sock)])
        assert preview.exit_code == 1, preview.output
        assert "changed 1 job" in preview.output and "1 job: A" in preview.output
        assert "stopping 1 running job: A" in preview.output
        assert "token:" not in preview.output
        source.write_text(JOB.replace("true", "sleep 32") + box)
        result = runner.invoke(app, ["update", "-S", str(sock), "--wait"])
        assert result.exit_code == 0, result.output
        deadline = time.monotonic() + 10
        while ask("status")["jobs"]["A"].get("attempt", {}).get("n") != 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        row = ask("status")["jobs"]["A"]
        assert row["run_number"] == 1 and row["attempt"]["after"] == "update_stop"
        assert "reread" not in row
        assert ask()["catalog"]["jobs"]["A"]["exec_"]["command"] == "sleep 32"
        rid = re.search(r"request_id: (\S+)", result.output).group(1)
        status = ask("status")
        assert roundtrip(
            sock,
            {
                "cmd": "sendevent",
                "verb": "FORCE_STARTJOB",
                "payload": {"job": "BX"},
                "baseline_id": status["baseline_id"],
                "request_id": "start-box",
                "expect": {"job:BX": status["jobs"]["BX"]["state_rev"]},
            },
        )["ok"]
        deadline = time.monotonic() + 10
        while not (
            ask("status")["jobs"]["B"]["status"] == "RUNNING"
            and ask("status")["jobs"]["C"]["status"] == "SUCCESS"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        source.write_text(
            JOB.replace("true", "sleep 32") + box + JOB.replace("A", "NEW") + "box_name: BX\n"
        )
        preview = runner.invoke(app, ["reread", "-S", str(sock)])
        assert preview.exit_code == 1 and "C   would be ON_HOLD" in preview.output
        assert ask("status")["jobs"]["BX"]["reread"]["action"] == "restart_box"
        box_result = runner.invoke(app, ["update", "-S", str(sock), "--wait"])
        assert box_result.exit_code == 0, box_result.output
        deadline = time.monotonic() + 10
        while (
            ask("status")["jobs"]["B"].get("attempt", {}).get("n") != 1
            or ask("status")["jobs"]["B"]["run_number"] != 2
        ):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        jobs = ask("status")["jobs"]
        assert jobs["BX"]["run_number"] == 2
        assert jobs["C"]["on_hold"] and jobs["C"]["run_number"] == 1
        assert jobs["B"]["attempt"]["after"] is None
        source.unlink()
        retry = runner.invoke(app, ["update", "-S", str(sock), "--request-id", rid, "--wait"])
        assert retry.exit_code == 0, retry.output
        assert "applied: catalog revision 2" in retry.output
        assert ask()["revision"] == 3


def _spec(run_root: Path, job: str) -> dict[str, Any]:
    from dsl41.runner_control import roundtrip

    return roundtrip(run_root / "control.sock", {"cmd": "spec", "job": job, "v": 3})


def _status(run_root: Path) -> dict[str, Any]:
    from dsl41.runner_control import roundtrip

    return roundtrip(run_root / "control.sock", {"cmd": "status", "v": 3})


def test_while_an_update_stops_a_retry_waits_for_it_and_another_update_is_refused(
    tmp_path: Path,
) -> None:
    """One update is in flight at a time. While it stops, an exact retry is answered with the
    same admission and, once applied, the same application; another plan is refused with
    `update_in_progress`; a sendevent's request id is a reuse. An update sent after the engine
    began to drain is refused with `engine_draining`."""
    slow = "trap '' TERM; sleep 30 & wait"
    estate = Estate(tmp_path, _jil({"slow": slow, "b": "sleep 1"}))
    run_root = tmp_path / "run"
    slow_grace = EngineSettings(grace_term_s=2.0, grace_kill_s=1.0)
    answers: dict[str, UpdateAnswer] = {}
    bodies: list[dict[str, Any]] = []

    async def scenario() -> None:
        engine = start_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(slow_grace),
            hold_open=True,
            settings=slow_grace,
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        await engine.submit(
            Event(at=engine.clock.now(), kind="ON_HOLD", payload={"job": "b"}),
            Envelope(request_id="sent", expect={"job:b": 1}),
        )
        _start_jobs(engine, "slow")
        await _running(engine, "slow")
        estate.write(_jil({"slow": "sleep 31", "b": "sleep 1"}))
        first = _plan(engine, estate)
        estate.write(_jil({"slow": "sleep 32", "b": "sleep 1"}))
        second = _plan(engine, estate)
        answers["first"] = await _update(engine, first)
        answers["retry"] = await _update(engine, first)
        answers["other"] = await _update(engine, second, rid="r2")
        answers["sent"] = await _update(engine, second, rid="sent")
        bodies.append(await _applied(answers["retry"]))
        bodies.append(await _applied(answers["first"]))
        stop = engine.request_stop()
        answers["draining"] = await _update(engine, second, rid="r3")
        await stop
        await _finish(engine, loop)

    asyncio.run(scenario())
    assert answers["retry"].decision == answers["first"].decision
    other = answers["other"].decision
    assert isinstance(other, Refused) and other.code == "update_in_progress"
    assert answers["sent"].code == "request_id_reused"
    assert answers["draining"].code == "engine_draining"
    assert bodies[0] == bodies[1] and bodies[0]["rev"] == 2


def test_a_task_the_attempt_tier_does_not_run_is_cancelled_then_taken_up_or_closed(
    tmp_path: Path,
) -> None:
    """A stop-set task with no attempt chart (the test adapter, a file watch) is cancelled at
    the admission: once the update applied, a changed job's task is launched again under its
    run id, and a removed job's attempt is closed with no plan. A settings edit that flips
    `fw-existence` rebuilds the FW adapter under the new value."""
    from dsl41.runner_adapters import FakeAdapter, FileWatcherAdapter
    from dsl41.runner_clock import VirtualClock

    estate = Estate(tmp_path, _jil({"a": "x", "b": "x", "c": "x"}))
    run_root = tmp_path / "run"
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        fake = FakeAdapter(default=None)
        engine = start_run(
            estate.catalog,
            run_root,
            clock=clock,
            adapters={"CMD": fake, "FW": FileWatcherAdapter(existence="stable")},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        _start_jobs(engine, "a", "b")
        # an injected STARTING launches nothing: the update has no attempt of `c` to stop
        engine.inject(
            Event(at=clock.now(), kind="STATUS", payload={"job": "c", "status": "STARTING"})
        )
        await engine.run_until_quiescent(clock.now())
        first = engine._live["a"].attempt
        write_config(estate.dir, estate='[estate.semantics]\nfw-existence = "immediate"\n')
        estate.write(_jil({"a": "y", "c": "y"}))
        answer = engine.request_update(_request(engine, _plan(engine, estate)))
        await engine.run_until_quiescent(clock.now())
        seen["answer"] = answer.result()
        seen["live"] = {job: run.attempt for job, run in engine._live.items()}
        seen["first"] = first
        fw = engine.adapters["FW"]
        assert isinstance(fw, FileWatcherAdapter)
        seen["existence"] = fw.existence
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert isinstance(seen["answer"].decision, Admitted)
    assert seen["live"] == {"a": seen["first"]}
    assert seen["existence"] == "immediate"
    [closed] = [o for o in _kinds(run_root, "outcome") if o["job"] == "b"]
    assert (closed["outcome"], closed["cause"]) == ("stopped", "update_stop")
    plans = [p for p in _kinds(run_root, "follow_up") if p["event"] == "plan"]
    assert [(p["job"], p["plan"]) for p in plans] == [("b", "none")]
    # the record names what followed each stop: `a` taken up, `b` closed, and `c`, with no
    # attempt to stop, none (its start goes under the new definition)
    [applied] = [c for c in _configs(run_root) if c["event"] == "applied" and "token" in c]
    assert applied["stopped"] == [["a", "rerun"], ["b", "none"], ["c", "none"]]


def test_a_stored_configuration_row_is_kept_on_a_retry_and_another_at_its_rev_refused() -> None:
    from dsl41.runner_config import Configuration
    from dsl41.runner_update import store_config

    store = MemoryStore()
    first = Configuration(catalog=lower_source(_jil({"a": "x"})), profile=RuntimeProfile())
    second = Configuration(catalog=lower_source(_jil({"b": "x"})), profile=RuntimeProfile())
    assert store.put_config(first.to_bytes(), first.digest()) == 1
    store_config(store, second, 2)
    store_config(store, second, 2)  # the row an engine wrote before it died: kept
    assert store.current_config_rev() == 2
    with pytest.raises(EngineError, match="configuration rev 2 does not match"):
        store_config(store, first, 2)
    assert store.current_config_rev() == 2


def test_the_record_and_replay_seams_refuse_what_they_cannot_read() -> None:
    from dsl41.runner_journal import check_record, load_configuration, replay_inputs
    from dsl41.runner_store import Record

    with pytest.raises(EngineError, match="config record 'later'"):
        check_record(Record(T0, "config", None, None, {"event": "later"}))
    with pytest.raises(EngineError, match="no configuration rev 7"):
        load_configuration(MemoryStore(), 7, where="here")
    segments = [{"rec": "segment", "rev": 1}, {"rec": "segment", "rev": 2}]
    with pytest.raises(EngineError, match="applied configuration rev 2 and this replay"):
        replay_inputs(Oracle(lower_source(_C)), segments)
    table = OpenTable()
    intent = {"event": "intent", "token": "t", "stop": [{"job": "j", "attempt": "gone"}]}
    table.note(1, Record(T0, "config", None, None, intent))
    assert table.update is not None and table.attempts == {}
    table.note(2, Record(T0, "config", None, None, {"event": "applied", "token": "other"}))
    assert table.update is not None and table.floor() == 1
    table.note(3, Record(T0, "config", None, None, {"event": "applied", "token": "t"}))
    assert table.update is None


def test_refit_rebuilds_only_an_adapter_built_under_another_switch() -> None:
    from dsl41.runner import refit_adapters
    from dsl41.runner_adapters import FileWatcherAdapter
    from dsl41.semantics import resolve

    fw = FileWatcherAdapter(default_interval_s=7, existence="stable")
    same = refit_adapters({"FW": fw}, resolve({}))
    assert same["FW"] is fw
    moved = refit_adapters({"FW": fw}, resolve({"fw-existence": "immediate"}))["FW"]
    assert isinstance(moved, FileWatcherAdapter)
    assert (moved.existence, moved.default_interval_s) == ("immediate", 7)


def test_the_update_verb_refuses_a_malformed_request_and_answers_a_slow_application_pending(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retired fields refuse; tokenless exact retries share the pending application."""
    from dsl41.cli_reload import socket_send, update_command
    from dsl41.runner_control import ControlServer, claimed_actor, roundtrip
    from test_main_config import estate_file
    from dsl41 import main_config as main

    slow = "trap '' TERM; sleep 30 & wait"
    path = estate_file(short_root)
    (short_root / "a.jil").write_text(_jil({"slow": slow}))
    candidate = main.prepare(path)
    run_root = short_root / "run"
    slow_grace = EngineSettings(grace_term_s=3.0, grace_kill_s=1.0)
    answers: dict[str, dict[str, Any]] = {}

    def ask(**fields: Any) -> dict[str, Any]:
        return roundtrip(run_root / "control.sock", {"cmd": "update", "v": 4, **fields})

    async def scenario() -> None:
        engine = start_run(
            candidate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(slow_grace),
            hold_open=True,
            settings=slow_grace,
            launched=main.profile_of(candidate),
            source_capture=main.encoded_parts(candidate),
        )
        engine.reread_source = str(path)
        engine.launch_settings = dict(candidate.metadata["launch"])
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        envelope = {
            "request_id": "u1",
            "baseline_id": engine.baseline_id,
            "claimed_actor": claimed_actor(),
        }
        try:
            for field, value in (
                ("token", "../x"),
                ("stop_running", True),
                ("expect", {"config": 1}),
                ("wait", "yes"),
            ):
                answers[field] = await asyncio.to_thread(ask, **{field: value}, **envelope)
            answers["unknown"] = await asyncio.to_thread(ask, retry_only=True, **envelope)
            _start_jobs(engine, "slow")
            await _running(engine, "slow")
            (short_root / "a.jil").write_text(_jil({"slow": "sleep 31"}))
            server._application_window = lambda: 0.05
            answers["pending"] = await asyncio.to_thread(ask, wait=True, **envelope)
            answers["status"] = await asyncio.to_thread(
                roundtrip, run_root / "control.sock", {"cmd": "status"}
            )
            answers["other"] = await asyncio.to_thread(ask, **{**envelope, "request_id": "u2"})
            answers["retry"] = await asyncio.to_thread(ask, retry_only=True, **envelope)
            capsys.readouterr()

            def cli_retry() -> int:
                return update_command(
                    send=socket_send(run_root / "control.sock", request_id="u1"), wait=True
                )

            answers["cli"] = {"code": await asyncio.to_thread(cli_retry)}
            captured = capsys.readouterr()
            answers["cli"].update(out=captured.out, err=captured.err)
        finally:
            await _finish(engine, loop)
            await server.close()

    asyncio.run(scenario())
    for field in ("token", "stop_running", "expect", "wait", "unknown"):
        assert answers[field]["code"] == "invalid_argument", answers[field]
        assert answers[field]["refused"]
        assert "token" not in answers[field]
    assert answers["status"]["update_window_s"] == 0.05
    assert answers["cli"]["code"] == 1 and "update pending;" in answers["cli"]["out"]
    assert "--request-id u1 --wait" in answers["cli"]["out"]
    assert "request_id: u1" in answers["cli"]["err"]
    assert (
        answers["pending"]["ok"],
        answers["pending"]["stop"],
        answers["pending"]["pending"],
    ) == (True, ["slow"], True)
    assert answers["status"]["update"]["stopping"] == ["slow"]
    assert (answers["retry"]["ok"], answers["retry"]["application"]) == (True, None)


def test_an_engine_with_no_run_root_or_an_unreadable_plan_refuses_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41 import main_config
    from dsl41.reload_plan import RereadRefused
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock

    estate = Estate(tmp_path, _jil({"a": "x"}))
    codes: list[str | None] = []

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        bare = start_run(
            estate.catalog,
            None,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        answer = bare.request_update(_request(bare, "0" * 64))
        await bare.run_until_quiescent(clock.now())
        codes.append(answer.result().code)
        engine = start_run(
            estate.catalog,
            tmp_path / "run",
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        estate.write(_jil({"a": "y"}))
        token = _plan(engine, estate)

        def unreadable(*args: object) -> object:
            raise RereadRefused(["the timezone database moved"])

        monkeypatch.setattr(main_config, "verified_from_parts", unreadable)
        answer = engine.request_update(_request(engine, token))
        await engine.run_until_quiescent(clock.now())
        codes.append(answer.result().code)
        assert "does not load: the timezone database moved" in str(answer.result().error)
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert codes == ["invalid_argument", "invalid_argument"]


def test_a_client_that_gave_up_on_the_decision_leaves_the_loop_running(tmp_path: Path) -> None:
    """A `decision_timeout` cancels the request's future. The loop takes the step all the same:
    the update is journaled and applied, the time advance it owes is fed and the application
    finished, and the loop lives on."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock

    estate = Estate(tmp_path, _jil({"a": "x"}))
    run_root = tmp_path / "run"
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            estate.catalog,
            run_root,
            clock=clock,
            adapters={"CMD": FakeAdapter()},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        estate.write(_jil({"a": "y"}))
        request = _request(engine, _plan(engine, estate))

        def finished(_opened: object) -> None:
            seen["finished"] = True

        engine.update.listeners.append(finished)
        future = engine.request_update(request)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(future, 0)
        seen["cancelled"] = future.cancelled()
        await engine.run_until_quiescent(clock.now())
        seen["rev"] = engine.config_rev
        retry = engine.request_update(request)  # the client's retry under the same id
        await engine.run_until_quiescent(clock.now())
        assert retry.result().application is not None
        seen["retried"] = retry.result().application["rev"]
        _start_jobs(engine, "a")
        await engine.run_until_quiescent(clock.now())  # the loop still takes inputs
        seen["status"] = engine.oracle.store.job["a"].status
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert seen == {
        "cancelled": True,
        "rev": 2,
        "finished": True,
        "retried": 2,
        "status": "SUCCESS",
    }
    order = [(k, b.get("event"), b.get("kind")) for _, k, b in rows(run_root)]
    applied = order.index(("config", "applied", None), order.index(("config", "intent", None)))
    assert order[applied + 1] == ("input", None, None)  # the time advance it owes


def test_a_retry_while_a_resumed_update_still_stops_waits_for_its_application(
    tmp_path: Path,
) -> None:
    """The update a restart resumed has no request in flight, and an exact retry of the
    original is answered from its intent with the admission, and with the application once
    the last stop ends; a retry of an intent the store holds with no application is answered
    with the admission alone. A plan whose catalog hash is not the engine's is `stale_plan`
    before anything is written."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_store import Record
    from dsl41.runner_update import admitted_of

    estate = Estate(tmp_path, _jil({"a": "x", "b": "x"}))
    run_root = tmp_path / "run"
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        engine = start_run(
            estate.catalog,
            run_root,
            clock=clock,
            adapters={"CMD": FakeAdapter(default=None)},
            source_capture=main.encoded_parts(prepare_estate(estate.dir)),
            launched=main.profile_of(prepare_estate(estate.dir)),
        )
        _start_jobs(engine, "a")
        await engine.run_until_quiescent(clock.now())
        assert engine.journal is not None
        estate.write(_jil({"a": "y", "b": "x"}))
        token = _plan(engine, estate)
        request = _request(engine, token)
        attempt = engine._live["a"].attempt
        intent = {
            "event": "intent",
            "token": token,
            "live_rev": 1,
            "stop": [{"job": "a", "run_number": 1, "attempt": attempt}],
            "filter": ["a"],
            "stop_running": True,
            "request_id": "r1",
            "fingerprint": request.fingerprint,
        }
        engine.journal.append(Record(clock.now(), "config", None, "r1", intent))
        engine.update.resume(intent)
        assert engine.update.stopping and engine.update.view() == {
            "token": token,
            "stopping": ["a"],
        }
        retry = engine.request_update(request)
        await engine.run_until_quiescent(clock.now())
        seen["retry"] = retry.result()
        assert seen["retry"].applied is not None
        seen["early"] = seen["retry"].applied.done()
        engine.update.ended("a")  # a step of the single writer
        await engine.run_until_quiescent(clock.now())
        seen["body"] = await seen["retry"].applied
        ghost = {**intent, "token": "e" * 64, "request_id": "r2"}
        ghost["fingerprint"] = UpdateRequest("e" * 64, 1, engine.baseline_id, "r2").fingerprint
        engine.journal.append(Record(clock.now(), "config", None, "r2", ghost))
        lone = engine.request_update(UpdateRequest("e" * 64, 1, engine.baseline_id, "r2"))
        await engine.run_until_quiescent(clock.now())
        seen["lone"] = lone.result()
        estate.write(_jil({"a": "z", "b": "x"}))
        stale = _plan(engine, estate)
        plan_json = run_root / "plans" / stale / "plan.json"
        document = decode(plan_json.read_bytes())
        plan_json.write_bytes(canonical_bytes({**document, "live_digest": "sha256:0"}) + b"\n")
        digest = engine.request_update(_request(engine, stale, rid="r3"))
        await engine.run_until_quiescent(clock.now())
        seen["digest"] = digest.result()
        engine.journal.close()

    asyncio.run(scenario())
    assert seen["retry"].decision == admitted_of(
        {**_intent_shape(), "token": seen["body"]["token"]}
    )
    assert seen["early"] is False and seen["body"]["rev"] == 2
    assert seen["lone"].decision is not None and seen["lone"].application is None
    decision = seen["digest"].decision
    assert isinstance(decision, Refused) and decision.code == "stale_plan"
    assert [c["event"] for c in _configs(run_root)].count("intent") == 2  # nothing more


def _intent_shape() -> dict[str, Any]:
    return {"stop": [{"job": "a", "run_number": 1}], "filter": ["a"]}
