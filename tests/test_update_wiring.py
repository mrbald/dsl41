"""`update` wired into the engine (spec-reload "`dsl41 update`", DL-427): the admission as a step
of the single writer, the stops through the attempt tier, the dispatch filter, the application,
the store's `config intent` and `config applied`, the snapshot of what an update leaves behind,
replay across an update, a restart's step 4, and the two verbs over the control socket.

The engine scenarios run real wrappers on the real clock; assertions read the recorded order,
never elapsed time. A crash is the loop cancelled with no shutdown record, then a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from store_surgery import rows

from dsl41.canon import canonical_bytes, decode
from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event
from dsl41.reload_plan import load_estate, reread, spec_texts
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
    """An estate directory the engine runs and `reread` reads: `estate.toml`, one JIL file and
    `non-idempotent.toml`."""

    def __init__(self, root: Path, text: str, marked: dict[str, str] | None = None) -> None:
        self.dir = root / "estate"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "estate.toml").write_text('name = "t"\nfiles = ["jobs.jil"]\n')
        self.write(text, marked)

    def write(self, text: str, marked: dict[str, str] | None = None) -> None:
        (self.dir / "jobs.jil").write_text(text)
        lines = "".join(f'"{job}" = "{why}"\n' for job, why in (marked or {}).items())
        (self.dir / "non-idempotent.toml").write_text(f"[jobs]\n{lines}" if lines else "")

    @property
    def catalog(self) -> CatalogIR:
        return load_estate(self.dir).catalog

    @property
    def marked(self) -> dict[str, str]:
        from dsl41.idempotence import load_exceptions

        return dict(load_exceptions(self.dir).reasons)


def _adapters(settings: EngineSettings) -> dict[str, JobAdapter]:
    return {"CMD": cast(JobAdapter, LocalCommandAdapter(grace_seconds=settings.grace_term_s))}


def _start(run_root: Path, estate: Estate, *, scheduler: Scheduler | None = None) -> Engine:
    return start_run(
        estate.catalog,
        run_root,
        clock=RealClock(),
        adapters=_adapters(GRACE),
        scheduler=scheduler,
        hold_open=True,
        settings=GRACE,
        non_idempotent=estate.marked,
    )


async def _resume(run_root: Path, estate: Estate) -> Engine:
    return await resume_run(
        estate.catalog,
        run_root,
        clock=RealClock(),
        adapters=_adapters(GRACE),
        hold_open=True,
        settle_seconds=0.2,
        settings=GRACE,
    )


def _start_jobs(engine: Engine, *jobs: str) -> None:
    for job in jobs:
        engine.inject(Event(at=engine.clock.now(), kind="FORCE_STARTJOB", payload={"job": job}))


async def _until(predicate: Callable[[], object]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


async def _running(engine: Engine, *jobs: str) -> None:
    await _until(lambda: all(engine.attempt_view(job) is not None for job in jobs))
    run_root = engine.run_root
    assert run_root is not None
    for job in jobs:
        await _until((run_root / "runs" / f"{job}.1" / "1" / "spawn.json").exists)


async def _finish(engine: Engine, loop: asyncio.Future[Any]) -> None:
    if not engine.draining:
        await engine.request_stop()
    async with asyncio.timeout(TIMEOUT_S):
        await loop
    await engine.shutdown()
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
    return reread(estate.dir, engine.update.live(), engine.run_root / "plans").token


def _request(engine: Engine, token: str, *, stop: bool = False, rid: str = "r1") -> UpdateRequest:
    return UpdateRequest(token, stop, engine.config_rev, engine.baseline_id, rid)


async def _update(
    engine: Engine, token: str, *, stop: bool = False, rid: str = "r1", expect: int | None = None
) -> UpdateAnswer:
    request = _request(engine, token, stop=stop, rid=rid)
    if expect is not None:
        request = UpdateRequest(token, stop, expect, engine.baseline_id, rid)
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
    """spec-reload: a plan that changes two running jobs is refused without consent; with it,
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
        refused = await _update(engine, token, rid="no-consent")
        admitted = await _update(engine, token, stop=True)
        states.append(engine.lifecycle.state)
        body = await _applied(admitted)
        states.append(engine.lifecycle.state)
        for job in ("j03", "j07"):
            await _until((marker / job).exists)
        await _finish(engine, loop)
        return refused, admitted, body, engine

    refused, admitted, body, engine = asyncio.run(scenario())
    assert isinstance(refused.decision, Refused)
    assert (refused.decision.code, refused.decision.names) == ("would_stop_running", ("j03", "j07"))
    assert isinstance(admitted.decision, Admitted)
    assert admitted.decision.stop_set == ("j03", "j07")
    assert states == ["RUNNING", "RUNNING"]
    assert (body["rev"], body["changed"], body["stopped"]) == (
        2,
        ["j03", "j07"],
        [["j03", "rerun"], ["j07", "rerun"]],
    )
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
        answer = await _update(engine, token, stop=True)
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
        # an exact retry repeats the request: the token, the consent and the rev it read
        answers["retry"] = await _update(engine, first, rid="one", expect=1)
        answers["stale"] = await _update(engine, second, rid="two")
        answers["expect"] = await _update(engine, second, rid="three", expect=1)
        answers["reused"] = await _update(engine, second, rid="one", stop=True)
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
    assert answers["reused"].code == "request_id_reused"
    assert answers["no_plan"].code == "invalid_argument"
    assert [c["event"] for c in _configs(run_root)] == ["applied", "intent", "applied"]


def test_a_box_that_started_since_reread_refuses_the_plan(tmp_path: Path) -> None:
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
        answers.append(await _update(engine, token, stop=True))
        await _finish(engine, loop)

    asyncio.run(scenario())
    [answer] = answers
    assert isinstance(answer.decision, Refused)
    assert (answer.decision.code, answer.decision.names) == ("box_running", ("bx",))
    assert [c["event"] for c in _configs(run_root)] == ["applied"]


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
    restart resumes the update STOPPING, settles its attempts in step 5, and applies it as a
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
        answer = await _update(engine, _plan(engine, estate), stop=True)
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
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        _start_jobs(engine, "slow")
        await _running(engine, "slow")
        estate.write(_jil({"slow": "sleep 31"}))
        await _update(engine, _plan(engine, estate), stop=True)
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
            settle_seconds=0.2,
            settings=GRACE,
            declared=RuntimeProfile(as_machine=("elsewhere",)),
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
            settle_seconds=0.2,
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
            settle_seconds=0.2,
            settings=GRACE,
            declared=RuntimeProfile(),
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


def test_reread_and_update_over_the_control_socket(
    short_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two verbs as an operator runs them: `reread` reads the engine's catalog through the
    `catalog` verb and prints the plan; `update --wait` sends the mutation and prints the
    application. A token with no plan, and a plan that would stop a running job without
    consent, exit 2."""
    from dsl41.cli_reload import reread_command, socket_send, update_command
    from dsl41.runner_control import ControlServer

    estate = Estate(short_root, _jil({"a": "sleep 30", "b": "sleep 1"}))
    run_root = short_root / "run"
    external = short_root / "external.properties"  # a site layer outside the estate (`-p`)
    external.write_text("SITE=one\n")
    codes: list[int] = []
    outputs: list[str] = []

    jil = estate.dir / "jobs.jil"
    specs: list[Any] = []

    async def scenario() -> None:
        engine = _start(run_root, estate)
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        # the baseline `run` gives its server: the files it loaded, the JIL it serves
        loaded = {str(jil): hashlib.sha256(jil.read_bytes()).hexdigest()}
        texts = spec_texts(load_estate(estate.dir).parsed, engine.oracle.catalog)
        server = ControlServer(
            engine, run_root / "control.sock", spec_texts=texts, estate_fingerprint=loaded
        )
        await server.start()
        try:
            _start_jobs(engine, "a")
            await _running(engine, "a")
            estate.write(_jil({"a": "sleep 31", "b": "sleep 2"}))
            specs.append((await asyncio.to_thread(_status, run_root))["spec_drift"])

            def reread_now() -> int:
                send = socket_send(run_root)
                return reread_command(
                    estate.dir, run_root=run_root, send=send, properties=[external]
                )

            codes.append(await asyncio.to_thread(reread_now))
            out = capsys.readouterr().out
            outputs.append(out)
            token = out.strip().splitlines()[-1].removeprefix("token: ")

            def update_now(stop: bool) -> int:
                return update_command(
                    token,
                    run_root=run_root,
                    send=socket_send(run_root),
                    stop_running=stop,
                    wait=True,
                )

            codes.append(await asyncio.to_thread(update_now, False))
            codes.append(await asyncio.to_thread(update_now, True))
            outputs.append(capsys.readouterr().out)
            status = await asyncio.to_thread(_status, run_root)
            outputs.append(json.dumps(status["config_rev"]))
            specs.append(status["spec_drift"])
            specs.append(await asyncio.to_thread(_spec, run_root, "a"))
            # an edit of the original site layer, not of the plan's copy, is drift
            server.DRIFT_CHECK_INTERVAL_S = 0  # type: ignore[misc]
            external.write_text("SITE=two\n")
            specs.append((await asyncio.to_thread(_status, run_root))["spec_drift"])
        finally:
            await server.close()
            await _finish(engine, loop)

    asyncio.run(scenario())
    assert codes == [1, 2, 0]
    plan, applied, rev = outputs
    assert "changed: a, b" in plan and "stops running: a" in plan
    assert "applied: catalog revision 2" in applied and "stopped a: rerun" in applied
    assert rev == "2"
    # the files on disk differ from the running catalog until the update applies them
    before, after, spec, edited = specs
    assert (before, after, edited) == (True, False, True)
    assert "command: sleep 31" in spec["jil"]


def _spec(run_root: Path, job: str) -> dict[str, Any]:
    from dsl41.runner_control import roundtrip

    return roundtrip(run_root / "control.sock", {"cmd": "spec", "job": job, "v": 3})


def _status(run_root: Path) -> dict[str, Any]:
    from dsl41.runner_control import roundtrip

    return roundtrip(run_root / "control.sock", {"cmd": "status", "v": 3})


def test_the_catalog_answer_maps_back_to_the_live_record() -> None:
    from dsl41.cli_reload import live_of_answer

    catalog = lower_source(_jil({"a": "x"}))
    answer = {
        "ok": True,
        "catalog": json.loads(json.dumps(catalog.model_dump(mode="json"))),
        "settings": {"timezone": "UTC"},
        "revision": 3,
        "status": {"a": "RUNNING"},
        "runs": {"a": 2},
        "tombstones": {"z": ["ON_HOLD"]},
    }
    live = live_of_answer(answer)
    assert (live.catalog, live.revision, live.runs, live.tombstones) == (
        catalog,
        3,
        {"a": 2},
        {"z": ("ON_HOLD",)},
    )


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
        answers["first"] = await _update(engine, first, stop=True)
        answers["retry"] = await _update(engine, first, stop=True)
        answers["other"] = await _update(engine, second, stop=True, rid="r2")
        answers["sent"] = await _update(engine, second, stop=True, rid="sent")
        bodies.append(await _applied(answers["retry"]))
        bodies.append(await _applied(answers["first"]))
        stop = engine.request_stop()
        answers["draining"] = await _update(engine, second, stop=True, rid="r3")
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
        )
        _start_jobs(engine, "a", "b")
        # an injected STARTING launches nothing: the update has no attempt of `c` to stop
        engine.inject(
            Event(at=clock.now(), kind="STATUS", payload={"job": "c", "status": "STARTING"})
        )
        await engine.run_until_quiescent(clock.now())
        first = engine._live["a"].attempt
        (estate.dir / "estate.toml").write_text(
            'name = "t"\nfiles = ["jobs.jil"]\n[semantics]\nfw-existence = "immediate"\n'
        )
        estate.write(_jil({"a": "y", "c": "y"}))
        answer = engine.request_update(_request(engine, _plan(engine, estate), stop=True))
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
    with pytest.raises(EngineError, match="took configuration rev 3, the update applies rev 2"):
        store_config(store, first, 2)


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
    """The socket's `update`: a token that names no plan directory, a consent that is not a
    boolean and a missing `expect` are refused before the engine sees them; a plan with no
    directory is `invalid_argument`; `status` names the update while it stops; `--wait` past
    the application window answers `pending`; and a loop that takes no step answers
    `decision_timeout`."""
    from dsl41.cli_reload import socket_send, update_command
    from dsl41.runner_control import ControlServer, claimed_actor, roundtrip

    slow = "trap '' TERM; sleep 30 & wait"
    estate = Estate(short_root, _jil({"slow": slow}))
    run_root = short_root / "run"
    slow_grace = EngineSettings(grace_term_s=3.0, grace_kill_s=1.0)
    answers: dict[str, dict[str, Any]] = {}

    def ask(**fields: Any) -> dict[str, Any]:
        return roundtrip(run_root / "control.sock", {"cmd": "update", "v": 3, **fields})

    async def scenario() -> None:
        engine = start_run(
            estate.catalog,
            run_root,
            clock=RealClock(),
            adapters=_adapters(slow_grace),
            hold_open=True,
            settings=slow_grace,
        )
        loop = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        envelope = {
            "request_id": "u1",
            "baseline_id": engine.baseline_id,
            "expect": {"config": 1},
            "claimed_actor": claimed_actor(),
        }
        try:
            answers["token"] = await asyncio.to_thread(ask, token="../x", **envelope)
            answers["consent"] = await asyncio.to_thread(
                ask, token="0" * 64, stop_running="yes", **envelope
            )
            answers["expect"] = await asyncio.to_thread(
                ask, token="0" * 64, request_id="u0", baseline_id=engine.baseline_id
            )
            answers["no_plan"] = await asyncio.to_thread(ask, token="0" * 64, **envelope)
            _start_jobs(engine, "slow")
            await _running(engine, "slow")
            estate.write(_jil({"slow": "sleep 31"}))
            token = _plan(engine, estate)
            server._application_window = lambda: 0.2  # type: ignore[method-assign]
            answers["pending"] = await asyncio.to_thread(
                ask, token=token, stop_running=True, wait=True, **envelope
            )
            answers["status"] = await asyncio.to_thread(
                roundtrip, run_root / "control.sock", {"cmd": "status", "v": 3}
            )
            # an exact retry without --wait is answered from the admission at once
            answers["retry"] = await asyncio.to_thread(
                ask, token=token, stop_running=True, **envelope
            )
            capsys.readouterr()

            def cli_retry() -> int:
                send = socket_send(run_root, request_id="u1")
                return update_command(
                    token, run_root=run_root, send=send, stop_running=True, wait=True
                )

            answers["cli"] = {"code": await asyncio.to_thread(cli_retry)}
            answers["cli"]["out"] = capsys.readouterr().out
            loop.cancel()  # the loop takes no further step
            await asyncio.gather(loop, return_exceptions=True)
            server.DECISION_TIMEOUT_S = 0.1  # type: ignore[misc]
            answers["timeout"] = await asyncio.to_thread(
                ask, token=token, **{**envelope, "request_id": "u2"}
            )
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
    assert (answers["token"]["code"], answers["consent"]["code"]) == (
        "invalid_argument",
        "invalid_argument",
    )
    assert answers["expect"]["code"] == "expect_required"
    assert (answers["no_plan"]["code"], answers["no_plan"]["refused"]) == ("invalid_argument", True)
    # every refusal names the token as sent and the names it concerns, none here
    for name in ("token", "consent", "expect", "no_plan"):
        assert answers[name]["names"] == [], name
    assert (answers["token"]["token"], answers["no_plan"]["token"]) == ("../x", "0" * 64)
    assert answers["status"]["update_window_s"] == 0.2
    # `--wait` that ends with the update admitted and not applied exits 1, naming the token
    assert answers["cli"]["code"] == 1 and "pending: " in answers["cli"]["out"]
    pending = answers["pending"]
    assert (pending["ok"], pending["stop"], pending["pending"]) == (True, ["slow"], True)
    assert answers["status"]["update"]["stopping"] == ["slow"]
    assert (answers["retry"]["ok"], answers["retry"]["application"]) == (True, None)
    assert answers["timeout"]["code"] == "decision_timeout"
    assert "refused" not in answers["timeout"]


def test_an_engine_with_no_run_root_or_an_unreadable_plan_refuses_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41 import runner_update
    from dsl41.reload_plan import RereadRefused
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock

    estate = Estate(tmp_path, _jil({"a": "x"}))
    codes: list[str | None] = []

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        bare = start_run(estate.catalog, None, clock=clock, adapters={"CMD": FakeAdapter()})
        answer = bare.request_update(_request(bare, "0" * 64))
        await bare.run_until_quiescent(clock.now())
        codes.append(answer.result().code)
        engine = start_run(
            estate.catalog, tmp_path / "run", clock=clock, adapters={"CMD": FakeAdapter()}
        )
        estate.write(_jil({"a": "y"}))
        token = _plan(engine, estate)

        def unreadable(*args: object) -> object:
            raise RereadRefused(["the timezone database moved"])

        monkeypatch.setattr(runner_update, "load_estate", unreadable)
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
        engine = start_run(estate.catalog, run_root, clock=clock, adapters={"CMD": FakeAdapter()})
        estate.write(_jil({"a": "y"}))
        request = _request(engine, _plan(engine, estate))
        future = engine.request_update(request)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(future, 0)
        seen["cancelled"] = future.cancelled()
        await engine.run_until_quiescent(clock.now())
        seen["rev"], seen["applied"] = engine.config_rev, engine.update.applied
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
        "applied": False,
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
            estate.catalog, run_root, clock=clock, adapters={"CMD": FakeAdapter(default=None)}
        )
        _start_jobs(engine, "a")
        await engine.run_until_quiescent(clock.now())
        assert engine.journal is not None
        estate.write(_jil({"a": "y", "b": "x"}))
        token = _plan(engine, estate)
        request = _request(engine, token, stop=True)
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
        ghost["fingerprint"] = UpdateRequest(
            "e" * 64, True, 1, engine.baseline_id, "r2"
        ).fingerprint
        engine.journal.append(Record(clock.now(), "config", None, "r2", ghost))
        lone = engine.request_update(UpdateRequest("e" * 64, True, 1, engine.baseline_id, "r2"))
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
