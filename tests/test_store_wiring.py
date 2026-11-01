"""The store wired into the engine (spec-wiring 1b): the wire projection of every
control-protocol ss5 record, the outcome and its follow-up plan, the interruption rule at a
restart, a crash between an outcome and its follow-up, pending dispatches, snapshots and
pruning.

Every crash here is a hook on the one writer (`Journal.append`) or a record removed or added
between two incarnations, never a sleep.
"""

from __future__ import annotations

from contextlib import closing

import asyncio
import json
import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from store_surgery import append, rows

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_journal import (
    Journal,
    decode_snapshot,
    oracle_rows,
    read_backfill,
    read_journal,
    read_records,
    replay_inputs,
    restore_oracle,
    wire_record,
)
from dsl41.runner_preflight import PreflightItem
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_stops import AttemptFacts
from dsl41.runner_store import MemoryStore

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\n"
_PAIR_JIL = _JIL + "\ninsert_job: k\njob_type: c\ncommand: y\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


def _kinds(run_root: Path, kind: str) -> list[dict[str, Any]]:
    return [body for _, k, body in rows(run_root) if k == kind]


def _plans(run_root: Path) -> list[dict[str, Any]]:
    return [body for body in _kinds(run_root, "follow_up") if body["event"] == "plan"]


async def _recovered(engine: Engine) -> Engine:
    """Run the loop over what is due now: a restart's steps 6 and 7 take effect there, and the
    loop records step 8 after them."""
    await engine.run_until_quiescent(engine.clock.now())
    assert engine.started.is_set()
    return engine


async def _resume(
    run_root: Path, text: str = _JIL, *, minutes: float = 2, adapter: Any = None, **kwargs: Any
) -> Engine:
    return await resume_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0 + timedelta(minutes=minutes)),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
        **kwargs,
    )


# ------------------------------------------------------------- 1. the wire (ss5)

#: every record name control-protocol ss5 streams, with the keys its shape fixes
_SECTION_5: dict[str, set[str]] = {
    "segment": {
        "rec",
        "rev",
        "at",
        "catalog_hash",
        "runtime_hash",
        "state_machine_version",
        "baseline_id",
        "clock_domain",
    },
    "input": {"rec", "seq", "at", "request_id", "fingerprint", "kind", "payload", "source"},
    "advance": {"rec", "seq", "at", "request_id", "fingerprint"},
    "decision": {
        "rec",
        "index",
        "request_id",
        "decision",
        "reason",
        "code",
        "revisions",
        "legacy_batch",
    },
    "dispatch": {"rec", "job", "run_number", "run_id", "run_dir", "started_at", "rev"},
    "drop": {"rec", "at", "kind", "payload", "reason"},
    "preflight": {"rec", "items"},
}


def test_the_backfill_projects_every_section_5_record_name(tmp_path: Path) -> None:
    """spec-wiring 1b: the subscribe backfill projects every record the frozen wire sends from
    the store kind that carries it, in the shape ss5 fixes: `segment` from the configuration,
    `input` and `advance` from `input`, `decision`, `dispatch`, `drop` and `preflight` from
    their own kinds. The kinds the wire has no name for (`outcome`, `follow_up`,
    `dispatch_pending`, `engine`) are not on it."""
    text = (
        "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 1\n\n"
        "insert_job: t\njob_type: c\ncommand: y\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:30"\n'
    )
    catalog = lower_source(text)
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (5.0, 0)}, default=None)},
            scheduler=Scheduler(catalog, start=T0),
        )
        assert engine.journal is not None
        engine.journal.preflight(
            [PreflightItem(severity="WARN", code="w", job="j", message="m")], T0
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())

    async def missed() -> None:  # resumed past 08:30: the tick is dropped
        engine = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(hours=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=T0),
        )
        await _close(await _recovered(engine))

    asyncio.run(missed())
    store = MemoryStore()
    for _, record in read_records(run_root):
        store.append(record)
    wires = read_backfill(store, since=0, upto=store.last_seq()).records
    named = {wire["rec"] for wire in wires}
    assert named == set(_SECTION_5)
    for wire in wires:
        assert set(wire) == _SECTION_5[wire["rec"]], wire
    stored = {record.kind for _, record in read_records(run_root)}
    assert {"outcome", "follow_up", "engine", "config"} <= stored
    for kind in ("outcome", "follow_up", "engine"):
        assert all(
            wire_record(record) is None
            for _, record in read_records(run_root)
            if record.kind == kind
        )


def test_the_backfill_cut_is_positional_and_the_seam_bounds_it() -> None:
    """The cut keeps everything after the last input at or below the cursor, and a record
    above the seam is the live feed's, not the backfill's."""
    from dsl41.runner_admission import ApplyResult, Attempt

    journal = Journal(MemoryStore())
    for index in (1, 2, 3):
        attempt = Attempt(index=index, at=T0, request_id=f"r{index}", fingerprint="f")
        journal.admission(
            attempt, ApplyResult(index=index, request_id=f"r{index}", decision="applied")
        )
    seam = journal.last_seq - 2  # the last admission is the feed's
    backfill = read_backfill(journal.store, since=1, upto=seam)
    assert backfill.gap_from is None
    assert [(w["rec"], w.get("seq", w.get("index"))) for w in backfill.records] == [
        ("decision", 1),
        ("advance", 2),
        ("decision", 2),
    ]


# ------------------------------------------------------------- 2. outcomes and plans


def test_a_live_end_records_its_outcome_and_a_complete_plan_finished_by_its_feed(
    tmp_path: Path,
) -> None:
    """A natural end is appended with plan `complete` in one transaction; the completion
    admission carries the step's done mark, naming the input that discharged it, so the open
    table holds nothing once the feed lands."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (5.0, 3)})},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.journal is not None
        assert engine.journal.open.attempts == {} and engine.journal.open.plans == {}
        await _close(engine)

    asyncio.run(scenario())
    [outcome] = _kinds(run_root, "outcome")
    assert (outcome["outcome"], outcome["status"]) == ("natural", {"exit_code": 3})
    [plan] = _plans(run_root)
    assert plan["plan"] == "complete" and plan["steps"][0]["step"] == "feed"
    [done] = [b for b in _kinds(run_root, "follow_up") if b["event"] == "done"]
    fed = [
        seq
        for seq, kind, body in rows(run_root)
        if kind == "input" and body.get("kind") == "STATUS"
    ]
    assert done["seq"] == fed[0]


def test_a_killed_attempt_is_closed_with_no_plan_once_reaped(tmp_path: Path) -> None:
    """The oracle decided the run's end: the cancelled attempt is closed with a `stopped`
    outcome and plan `none`, so a restart has nothing of it to settle."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        engine.inject(_ev("KILLJOB", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.journal is not None and engine.journal.open.attempts == {}
        await _close(engine)

    asyncio.run(scenario())
    [outcome] = _kinds(run_root, "outcome")
    assert (outcome["outcome"], outcome["cause"]) == ("stopped", "killed")
    assert [p["plan"] for p in _plans(run_root)] == ["none"]


@pytest.mark.parametrize(
    ("status", "outcome", "plan"),
    [
        ({"outcome": "exited", "exit_code": 0}, "natural", "complete"),
        ({"outcome": "signaled", "signal": 9}, "natural", "complete"),
        ({"outcome": "terminated", "cause": "parent lost"}, "stopped", "rerun"),
        ({"outcome": "spawn_failed", "error": "no such file"}, "spawn_failed", "complete"),
        (None, "unobservable", "rerun"),
    ],
    ids=["exited", "signaled", "lifeline", "spawn_failed", "no_record"],
)
def test_the_spool_record_names_how_the_attempt_ended(
    tmp_path: Path, status: dict[str, Any] | None, outcome: str, plan: str
) -> None:
    """spec-wiring 2b's table at the restart's observation (spec-restart §4): the open attempt
    goes through the attempt chart, fed its `status.json` in the loop. Only the lifeline's kill
    is a stop; a missing record is unobservable, ended by the engine's death. Both
    interruptions of an idempotent job rerun."""
    run_root = tmp_path / "run"
    _open_attempt(run_root)
    [dispatch] = _kinds(run_root, "dispatch")
    if status is not None:
        spool = Path(dispatch["spool"])
        spool.mkdir(parents=True)
        identity = {"version": 1, "run_id": dispatch["attempt"], "job": "j", "run_number": 1}
        (spool / "status.json").write_text(json.dumps({**identity, **status}))

    async def scenario() -> None:
        engine = await _resume(run_root)
        await engine.run_until_quiescent(engine.clock.now())
        await _close(engine)

    asyncio.run(scenario())
    [ended] = _kinds(run_root, "outcome")
    assert (ended["attempt"], ended["outcome"]) == (dispatch["attempt"], outcome)
    if status is None:
        assert ended["cause"] == "engine_death"
    assert [p["plan"] for p in _plans(run_root)] == [plan]


def _open_attempt(run_root: Path, text: str = _JIL) -> None:
    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"k": "posts twice"} if "insert_job: k" in text else None,
        )
        for job in ("j", "k") if "insert_job: k" in text else ("j",):
            engine.inject(_ev("STARTJOB", 0, job=job))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(scenario())


def test_the_interruption_rule_reruns_an_idempotent_job_and_holds_a_marked_one(
    tmp_path: Path,
) -> None:
    """spec-wiring 2b's rule at the restart: both attempts died with the engine and left no
    record. `j` is idempotent, so its run starts again as attempt 2 with the same invocation
    and the oracle is told nothing; `k` is named in `non-idempotent.toml`, so it is FAILURE
    with cause `interrupted`, then ON_HOLD, with the evidence on its plan."""
    run_root = tmp_path / "run"
    _open_attempt(run_root, _PAIR_JIL)

    async def scenario() -> Engine:
        engine = await _resume(run_root, _PAIR_JIL)
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _close(engine)
        return engine

    engine = asyncio.run(scenario())
    plans = {p["job"]: p for p in _plans(run_root)}
    assert plans["j"]["plan"] == "rerun" and plans["k"]["plan"] == "hold"
    assert plans["k"]["evidence"]["attempt"]
    assert engine.oracle.store.job["j"].status == "RUNNING"
    k = engine.oracle.store.job["k"]
    assert (k.status, k.on_hold) == ("FAILURE", True)
    fed = [b for b in _kinds(run_root, "input") if b.get("source") == "reconcile"]
    # one input: the FAILURE that holds the job in the same transition (Astra B1)
    assert [(b["kind"], b["payload"]["job"]) for b in fed] == [("STATUS", "k")]
    assert (fed[0]["payload"]["cause"], fed[0]["payload"]["hold"]) == ("interrupted", True)
    dispatched = [(b["job"], b["ordinal"]) for b in _kinds(run_root, "dispatch")]
    assert dispatched == [("j", 1), ("k", 1), ("j", 2)]
    first, second = [b for b in _kinds(run_root, "dispatch") if b["job"] == "j"]
    assert second["attempt"] == plans["j"]["steps"][0]["attempt"]
    assert second["env"]["DSL41_RUN_NUMBER"] == first["env"]["DSL41_RUN_NUMBER"] == "1"
    assert second["env"]["DSL41_SCHEDULED_AT"] == first["env"]["DSL41_SCHEDULED_AT"]
    # the identity is the uuid the plan named; the ordinal is DSL41_ATTEMPT (DL-118, DL-426)
    assert second["attempt"] != first["attempt"]
    assert (first["env"]["DSL41_ATTEMPT"], second["env"]["DSL41_ATTEMPT"]) == ("1", "2")
    # the cause the replacement follows: the engine's death (spec-restart §4)
    assert plans["j"]["steps"][0]["after"] == "engine_death"


def test_the_interruption_rule_drops_a_removed_job_and_a_run_the_oracle_ended(
    tmp_path: Path,
) -> None:
    """Steps 2 and 3: an attempt whose job left the catalog, or whose run is no longer the
    oracle's current one in STARTING or RUNNING, gets plan `none`."""
    run_root = tmp_path / "run"
    _open_attempt(run_root)

    gone, ended = "0f6a8f4c-1c2d-4b5e-8f90-a1b2c3d4e5f6", "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e4a10"

    async def scenario() -> None:
        engine = await _resume(run_root)
        for attempt, job, run in ((gone, "gone", 1), (ended, "j", 7)):
            engine.adopt_attempt(AttemptFacts(attempt, job, run, 1, tmp_path / "none"))
            engine.attempts.wrapper_exited(attempt, None)  # no record, no command: unobservable
        await _close(engine)

    asyncio.run(scenario())
    plans = {p["attempt"]: p for p in _plans(run_root)}
    assert (plans[gone]["plan"], plans[ended]["plan"]) == ("none", "none")


def test_a_replacement_superseded_before_it_launches_is_closed_with_no_launch(
    tmp_path: Path,
) -> None:
    """A rerun waits for the due work: an overdue `term_run_time` ends the run first, so the
    replacement is not launched and its step is marked done with no effect."""
    text = "insert_job: j\njob_type: c\ncommand: x\nterm_run_time: 2\n"
    run_root = tmp_path / "run"
    _open_attempt(run_root, text)

    async def scenario() -> Engine:
        engine = await _resume(run_root, text, minutes=5)
        await engine.run_until_quiescent(T0 + timedelta(minutes=6))
        await _close(engine)
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].status == "TERMINATED"
    assert [b["ordinal"] for b in _kinds(run_root, "dispatch")] == [1]
    [done] = [b for b in _kinds(run_root, "follow_up") if b["event"] == "done"]
    assert done["seq"] is None


# ------------------------------------------------------------- 3. a crash in a plan


@pytest.mark.parametrize("where", ["feed", "rerun"])
def test_a_crash_between_an_outcome_and_its_follow_up_is_repaired_once(
    tmp_path: Path, where: str
) -> None:
    """Acceptance 5. The engine dies after an outcome and its plan are durable and before the
    plan's step lands: the completion's admission (a natural end), or the replacement's
    dispatch (an interruption). The restart finishes the plan from the open table, without
    reclassifying it, and the effect happens once: one STATUS input, or one replacement."""
    run_root = tmp_path / "run"
    if where == "feed":
        adapter = FakeAdapter({("j", 1): (5.0, 0)})

        async def first() -> None:
            engine = start_run(
                lower_source(_JIL),
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": adapter},
            )
            assert engine.journal is not None
            real = engine.journal.append

            def die_at_the_step(*records: Any) -> int:
                if any(r.kind == "follow_up" and r.payload["event"] == "done" for r in records):
                    raise RuntimeError("the engine died before the plan's step")
                return real(*records)

            engine.journal.append = die_at_the_step  # type: ignore[method-assign]
            engine.inject(_ev("STARTJOB", 0, job="j"))
            with pytest.raises(RuntimeError, match="before the plan's step"):
                await engine.run_until_quiescent(T0 + timedelta(minutes=1))
            engine.journal.append = real  # type: ignore[method-assign]
            await _close(engine)

        asyncio.run(first())
    else:
        _open_attempt(run_root)

        async def interrupted() -> None:
            engine = await _resume(run_root)
            assert engine.journal is not None
            real = engine.journal.append

            def die_at_the_dispatch(*records: Any) -> int:
                if any(r.kind == "dispatch" for r in records):
                    raise RuntimeError("the engine died before the plan's step")
                return real(*records)

            engine.journal.append = die_at_the_dispatch  # type: ignore[method-assign]
            with contextlib.suppress(RuntimeError):
                await engine.run_until_quiescent(T0 + timedelta(minutes=3))
            engine.journal.append = real  # type: ignore[method-assign]
            await _close(engine)

        asyncio.run(interrupted())
    [plan] = [p for p in _plans(run_root) if p["plan"] != "complete" or where == "feed"]
    assert not [b for b in _kinds(run_root, "follow_up") if b["event"] == "done"]

    async def repaired() -> Engine:
        engine = await _resume(run_root, minutes=4)
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await _close(engine)
        return engine

    engine = asyncio.run(repaired())
    assert [p["plan"] for p in _plans(run_root)] == [plan["plan"]]  # never reclassified
    assert len([b for b in _kinds(run_root, "follow_up") if b["event"] == "done"]) == 1
    if where == "feed":
        statuses = [b for b in _kinds(run_root, "input") if b.get("kind") == "STATUS"]
        assert len(statuses) == 1
        assert engine.oracle.store.job["j"].status == "SUCCESS"
    else:
        assert [b["ordinal"] for b in _kinds(run_root, "dispatch")] == [1, 2]
        assert engine.oracle.store.job["j"].status == "RUNNING"

    async def again() -> None:  # a third start adds nothing to a finished plan
        await _close(await _resume(run_root, minutes=6))

    asyncio.run(again())
    assert len([b for b in _kinds(run_root, "follow_up") if b["event"] == "done"]) == 1


# ------------------------------------------------------------- 4. pending dispatches


def test_a_pending_dispatch_is_discharged_while_its_run_is_starting(tmp_path: Path) -> None:
    """Startup step 7: a `dispatch_pending` whose run is still the oracle's current run in
    STARTING is dispatched, its `dispatch` naming the pending record; one whose run moved on
    is recorded discharged with no launch."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(_PAIR_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 0, job="j"))
        engine.inject(_ev("STARTJOB", 0, job="k"))
        engine.inject(_ev("KILLJOB", 0.5, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())
    for job in ("j", "k"):
        pending = {"event": "pending", "job": job, "run_number": 1, "scheduled_at": T0.isoformat()}
        append(run_root, "dispatch_pending", pending, at=T0)

    async def second() -> Engine:
        engine = await _resume(run_root, _PAIR_JIL)
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert engine.journal is not None and engine.journal.open.pending == {}
        await _close(engine)
        return engine

    asyncio.run(second())
    seqs = {
        body["job"]: seq
        for seq, kind, body in rows(run_root)
        if kind == "dispatch_pending" and body["event"] == "pending"
    }
    [dispatch] = _kinds(run_root, "dispatch")
    assert (dispatch["job"], dispatch["discharges"]) == ("j", seqs["j"])
    [discharged] = [b for b in _kinds(run_root, "dispatch_pending") if b["event"] == "discharged"]
    assert (discharged["pending"], discharged["dispatch"]) == (seqs["k"], None)


def test_a_restart_after_a_drain_with_no_shutdown_records_drain_interrupted(
    tmp_path: Path,
) -> None:
    """Startup step 8: the last incarnation recorded `draining` and no shutdown record."""
    run_root = tmp_path / "run"
    _open_attempt(run_root)
    append(run_root, "engine", {"event": "draining", "at": T0.isoformat()}, at=T0)

    async def restart(minutes: float) -> None:
        await _close(await _recovered(await _resume(run_root, minutes=minutes)))

    asyncio.run(restart(2))
    events = [b["event"] for b in _kinds(run_root, "engine")]
    assert events[-2:] == ["drain_interrupted", "started"]
    asyncio.run(restart(3))
    assert [b["event"] for b in _kinds(run_root, "engine")].count("drain_interrupted") == 1


# ------------------------------------------------------------- 5. snapshots and pruning


def _night(run_root: Path, settings: EngineSettings | None = None) -> Engine:
    """Nightbank-shaped load on one engine: a box chain whose members end on their own, with a
    snapshot taken partway."""
    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: m1\njob_type: c\ncommand: x\nbox_name: b\n\n"
        "insert_job: m2\njob_type: c\ncommand: x\nbox_name: b\ncondition: s(m1)\n\n"
        "insert_job: after\njob_type: c\ncommand: x\ncondition: s(b)\n"
    )
    adapter = FakeAdapter(default=(30.0, 0))
    return start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter},
        settings=settings,
    )


def test_snapshot_plus_tail_equals_full_replay_on_a_real_engine_run(tmp_path: Path) -> None:
    """Acceptance 6. A snapshot the engine takes mid-run, restored and fed the tail, reaches
    the rows and the trace a full replay of the same store reaches; and a restart from it runs
    on as the full replay would."""
    run_root = tmp_path / "run"

    async def scenario() -> int:
        engine = _night(run_root)
        engine.inject(_ev("STARTJOB", 0, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=45))
        seq = engine.take_snapshot()
        engine.inject(_ev("SET_GLOBAL", 1, name="G", value="v"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await _close(engine)
        return seq

    seq = asyncio.run(scenario())
    engine = _night(tmp_path / "shape")  # the catalog, read the same way
    catalog = engine.oracle.catalog
    assert engine.journal is not None
    engine.journal.close()
    store = MemoryStore()
    for _, record in read_records(run_root):
        store.append(record)
    full = Oracle(catalog.model_copy(deep=True))
    replay_inputs(full, read_journal(store))
    from dsl41.runner_journal import open_reader

    reader = open_reader(run_root)
    snapshot = reader.latest_snapshot()
    reader.close()
    assert snapshot is not None and snapshot.seq == seq
    restored = decode_snapshot(seq, snapshot.data, where="t")
    resumed = restore_oracle(catalog.model_copy(deep=True), restored.rows)
    tail = [w for s, r in store.read_from(seq) if (w := wire_record(r)) is not None]
    replay_inputs(resumed, tail, frontiers=restored.frontiers)
    assert oracle_rows(resumed) == oracle_rows(full)
    assert resumed.trace() == full.trace()[len(full.trace()) - len(resumed.trace()) :]
    assert full.store.job["after"].status == "SUCCESS"

    async def restart() -> Engine:
        again = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=6)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await _close(again)
        return again

    # the records the snapshot covers are gone, so only a restart that restores the snapshot
    # reaches the full replay's rows: one that ignored it would replay the tail over nothing
    _prune_head(run_root, seq)
    assert read_records(run_root)[0][0] == seq
    again = asyncio.run(restart())
    assert oracle_rows(again.oracle) == oracle_rows(full) | {"now": oracle_rows(full)["now"]}


def test_a_snapshot_with_an_unsettled_attempt_keeps_its_dispatch_across_a_prune(
    tmp_path: Path,
) -> None:
    """Acceptance 6's second half. The pruning floor is the lower of the snapshot's seq and the
    oldest seq the open table refers to, so the dispatch of an attempt still running survives
    a prune that removes everything else older than the retention window, and a restart from
    the snapshot settles it."""
    run_root = tmp_path / "run"

    async def scenario() -> int:
        engine = start_run(
            lower_source(_PAIR_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("k", 1): (5.0, 0)}, default=None)},
            settings=EngineSettings(retention_days=1),
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))  # parked: open
        engine.inject(_ev("STARTJOB", 1, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        for minute in range(3, 8):
            engine.inject(_ev("SET_GLOBAL", minute, name="G", value=str(minute)))
        engine.inject(_ev("SET_GLOBAL", 3 * 24 * 60, name="G", value="late"))
        await engine.run_until_quiescent(T0 + timedelta(days=3))
        seq = engine.take_snapshot()
        await _close(engine)
        return seq

    seq = asyncio.run(scenario())
    kept = rows(run_root)
    [dispatch] = [(s, b) for s, kind, b in kept if kind == "dispatch" and b["job"] == "j"]
    assert dispatch[0] < seq
    assert kept[0][0] == dispatch[0]  # the floor: nothing older survived, the dispatch did

    async def restart() -> None:
        engine = await _resume(run_root, _PAIR_JIL, minutes=3 * 24 * 60)
        await engine.run_until_quiescent(engine.clock.now())  # the catch-up observes it
        await _close(engine)

    asyncio.run(restart())
    [outcome] = [b for b in _kinds(run_root, "outcome") if b["job"] == "j"]
    assert outcome["outcome"] == "unobservable"


def test_the_engine_snapshots_on_its_cadence_once_the_journal_moved(tmp_path: Path) -> None:
    """`snapshot_interval_s` after the first applied boundary, when the journal moved."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _night(run_root, EngineSettings(snapshot_interval_s=60))
        engine.inject(_ev("STARTJOB", 0, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        assert engine.journal is not None
        assert engine.journal.store.latest_snapshot() is not None
        await _close(engine)

    asyncio.run(scenario())
    assert [b["event"] for b in _kinds(run_root, "engine")].count("snapshot") >= 1


def test_a_payload_the_store_cannot_hold_is_refused_before_admission(tmp_path: Path) -> None:
    """ss3.2 has no float: a control input that carries one is refused, nothing is recorded,
    and the engine serves on."""
    from dsl41.runner_admission import AdmissionRefused

    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        answer = engine.submit(
            _ev("SET_GLOBAL", 0, name="G", value=1.5),
            Envelope(request_id="f", expect={"global:G": 0}),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        with pytest.raises(AdmissionRefused) as refused:
            await answer
        assert refused.value.code == "invalid_argument"
        await _close(engine)

    asyncio.run(scenario())
    assert _kinds(run_root, "input") == []


# ------------------------------------------------------------- 6. the edges


def test_an_exact_retry_after_a_snapshot_restart_is_answered_from_the_store(
    tmp_path: Path,
) -> None:
    """The decision index replays only the tail after a snapshot, so a request decided before
    it is answered from the store (`Store.find_request`): an exact retry gets the first
    decision and applies nothing, the same id for another command is refused, and an id the
    store never saw is admitted."""
    from dsl41.runner_admission import RequestCollision

    run_root = tmp_path / "run"
    hold = _ev("ON_HOLD", 0, job="j")

    async def first() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        answer = engine.submit(hold, Envelope(request_id="r1", expect={"job:j": 0}))
        await engine.run_until_quiescent(T0)
        assert (await answer).index == 1
        engine.take_snapshot()
        await _close(engine)

    asyncio.run(first())

    async def second() -> Engine:
        engine = await _resume(run_root)
        assert engine.decisions.state("r1") is None  # not in the replayed tail
        again = engine.submit(hold, Envelope(request_id="r1", expect={"job:j": 0}))
        other = engine.submit(
            _ev("OFF_HOLD", 2, job="j"), Envelope(request_id="r1", expect={"job:j": 1})
        )
        fresh = engine.submit(
            _ev("OFF_HOLD", 2, job="j"), Envelope(request_id="r2", expect={"job:j": 1})
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert (await again).index == 1
        with pytest.raises(RequestCollision):
            await other
        assert (await fresh).decision == "applied"
        await _close(engine)
        return engine

    engine = asyncio.run(second())
    assert [request for request, _ in engine.deduped] == ["r1"]


def test_a_cancel_that_meets_an_attempt_already_closed_records_nothing_more() -> None:
    """A task reaped after its own outcome landed (the adapter returned before the cancel)
    gets no second outcome."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (1.0, 0)})},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.journal is not None
        before = engine.journal.last_seq
        task = asyncio.ensure_future(asyncio.sleep(0))
        await task
        from dsl41.runner import _LiveRun

        engine._killed[id(task)] = _LiveRun(1, task, attempt="gone")
        engine._close_killed(task)
        assert engine.journal.last_seq == before
        await engine.shutdown()

    asyncio.run(scenario())


def test_a_watch_whose_run_ended_is_closed_with_no_plan_at_a_restart(tmp_path: Path) -> None:
    """DL-242: a watch the operator set INACTIVE is left running by the engine (DL-235); at a
    restart only a live row is watched again, so the attempt is closed with no plan."""
    text = "insert_job: w\njob_type: f\nwatch_file: /tmp/dsl41-never-appears\n"
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="w"))
        engine.inject(_ev("STATUS", 1, job="w", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await _close(engine)

    asyncio.run(first())

    async def second() -> Engine:
        engine = await resume_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=3)),
            adapters={"FW": FakeAdapter(default=None)},
        )
        await engine.run_until_quiescent(engine.clock.now())  # the watch's catch-up step
        await _close(engine)
        return engine

    engine = asyncio.run(second())
    assert engine.live_jobs() == frozenset()
    [outcome] = _kinds(run_root, "outcome")
    assert (outcome["outcome"], outcome["cause"]) == ("stopped", "run ended")


def test_a_hold_plan_cut_before_its_step_finishes_it_once(tmp_path: Path) -> None:
    """A plan's step is one transaction with its effect: a crash at the hold's FAILURE leaves
    the step open, and the restart feeds it once; the job ends FAILURE and held (the hold is
    one oracle transition with the FAILURE, Astra B1)."""
    run_root = tmp_path / "run"
    _open_attempt(run_root, _PAIR_JIL)

    async def interrupted() -> None:
        engine = await _resume(run_root, _PAIR_JIL)
        assert engine.journal is not None
        real = engine.journal.append

        def die_at_the_hold(*records: Any) -> int:
            holds = (r.payload.get("payload", {}).get("hold") for r in records if r.kind == "input")
            if any(holds):
                raise RuntimeError("the engine died before the hold")
            return real(*records)

        engine.journal.append = die_at_the_hold  # type: ignore[method-assign]
        with contextlib.suppress(RuntimeError):
            await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        engine.journal.append = real  # type: ignore[method-assign]
        await _close(engine)

    asyncio.run(interrupted())

    async def repaired() -> Engine:
        engine = await _resume(run_root, _PAIR_JIL, minutes=4)
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await _close(engine)
        return engine

    engine = asyncio.run(repaired())
    fed = [b["kind"] for b in _kinds(run_root, "input") if b.get("source") == "reconcile"]
    assert fed == ["STATUS"]
    k = engine.oracle.store.job["k"]
    assert (k.status, k.on_hold) == ("FAILURE", True)


@pytest.mark.parametrize("damage", ["foreign", "directory"])
def test_a_store_that_does_not_open_is_refused_by_name(tmp_path: Path, damage: str) -> None:
    """A reader and a restart refuse an `engine.db` that is not a dsl41 store, or not a file,
    naming it."""
    import sqlite3

    from dsl41.runner_clock import EngineError
    from dsl41.runner_journal import open_reader

    run_root = tmp_path / "run"
    run_root.mkdir()
    if damage == "foreign":
        with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
            db.execute("CREATE TABLE other (x)")
    else:
        (run_root / "engine.db").mkdir()
    with pytest.raises(EngineError, match="engine.db"):
        open_reader(run_root)
    if damage == "foreign":
        with pytest.raises(EngineError, match="not a dsl41 store"):
            asyncio.run(_resume(run_root))


# ------------------------------------------------------------- 7. round-2 pins


def _prune_head(run_root: Path, below: int) -> None:
    """Delete every journal row below seq `below`, as the retention would once they are old."""
    from dsl41.runner_store import SqliteStore

    with closing(SqliteStore(run_root / "engine.db")) as store:
        store.prune_before(below, datetime(2999, 1, 1))


def test_the_journal_verb_replays_a_pruned_store_from_its_latest_snapshot(tmp_path: Path) -> None:
    """`dsl41 journal` on a store whose head the retention pruned restores the latest snapshot
    and replays what follows, as a restart does, and says so in its first line; with no
    snapshot to replay from it refuses by name."""
    from store_surgery import drop_snapshots
    from typer.testing import CliRunner

    from dsl41.cli import app

    run_root = tmp_path / "run"

    async def scenario() -> int:
        engine = _night(run_root)
        engine.inject(_ev("STARTJOB", 0, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=45))
        seq = engine.take_snapshot()
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        await _close(engine)
        return seq

    seq = asyncio.run(scenario())
    whole = CliRunner().invoke(app, ["journal", str(run_root)])
    assert whole.exit_code == 0, whole.output
    _prune_head(run_root, seq)  # the input before the snapshot's decision goes with the head
    pruned = CliRunner().invoke(app, ["journal", str(run_root)])
    assert pruned.exit_code == 0, pruned.output
    first, *trace = pruned.output.splitlines()
    assert first.startswith("trace since the snapshot at ")
    assert first.endswith("`dsl41 runs` has the history")
    assert trace and whole.output.splitlines()[-len(trace) :] == trace
    drop_snapshots(run_root)
    refused = CliRunner().invoke(app, ["journal", str(run_root)])
    assert refused.exit_code == 2
    assert "holds no snapshot" in refused.output


def test_a_restart_from_a_snapshot_opens_its_trace_with_a_restart_line(tmp_path: Path) -> None:
    """A restart serves the trace from the snapshot it resumed from, and the first line says
    so (`query trace` and the TUI read this trace)."""
    run_root = tmp_path / "run"

    async def scenario() -> Engine:
        engine = _night(run_root)
        engine.inject(_ev("STARTJOB", 0, job="b"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        engine.take_snapshot()
        catalog = engine.oracle.catalog
        await _close(engine)
        again = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=6)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await _close(await _recovered(again))
        return again

    again = asyncio.run(scenario())
    first, *rest = again.oracle.trace()
    assert (first.job, first.transition) == ("", "RESTART")
    assert first.cause.startswith("trace since the snapshot at ")
    assert first.cause.endswith("`dsl41 runs` has the history")
    assert rest == []  # nothing happened after the snapshot


def test_a_pruned_store_that_retains_no_input_still_sends_the_gap_marker() -> None:
    """control-protocol ss5: right after a prune the store may retain no input, only the last
    decision and lifecycle records. A cursor below the pruned inputs still gets the marker,
    naming the index after the last one the snapshot covers; a cursor at that last index is
    contiguous and gets none."""

    async def scenario() -> Engine:
        engine = start_run(
            lower_source(_JIL),
            None,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        for minute in range(3):
            engine.inject(_ev("SET_GLOBAL", minute, name="G", value=str(minute)))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        return engine

    engine = asyncio.run(scenario())
    assert engine.journal is not None
    store = engine.journal.store
    seq = engine.take_snapshot()
    store.prune_before(seq, datetime(2999, 1, 1))
    assert all(record.kind != "input" for _, record in store.read_from(0))
    last = engine.frontiers.committed_index
    assert read_backfill(store, since=0, upto=store.last_seq()).gap_from == last + 1
    assert read_backfill(store, since=last - 1, upto=store.last_seq()).gap_from == last + 1
    assert read_backfill(store, since=last, upto=store.last_seq()).gap_from is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_a_prune_reads_seqs_only_and_leaves_no_hole(tmp_path: Path, kind: str) -> None:
    """A record's instant is not monotonic in seq (the `engine started` of a restart precedes
    the feeds stamped at the journal's last instant). The prune bound is the first seq inside
    the retention window, so a record older than the window that follows a newer one is kept,
    and the kept rows are one unbroken range."""
    from dsl41.runner_store import Record, SqliteStore

    store = MemoryStore() if kind == "memory" else SqliteStore(tmp_path / "engine.db")
    old, new = T0, T0 + timedelta(days=100)
    for at in (old, old, new, old, new, old):
        store.append(Record(at, "engine", None, None, {"event": "started", "at": at.isoformat()}))
    store.put_snapshot(6, 1, b"{}")
    assert store.prune_before(6, T0 + timedelta(days=50)) == 2
    assert [seq for seq, _ in store.read_from(0)] == [3, 4, 5, 6]
    store.close()


def test_owed_starts_stay_pending_until_each_dispatch_commits(tmp_path: Path) -> None:
    """Starts a restart owes are `dispatch_pending` records until each `dispatch` commits, and
    each leaves the engine's owed list only then. A dispatch that raises after the first owed
    start leaves the second pending in the open table, which a snapshot carries, and the next
    restart dispatches it once, naming its pending record."""
    run_root = tmp_path / "run"

    async def first() -> None:  # both starts decided, neither dispatched: the DL-102 window
        engine = start_run(
            lower_source(_PAIR_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine._dispatch = lambda emitted: None  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 0, job="j"))
        engine.inject(_ev("STARTJOB", 0, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())

    async def crashed() -> None:
        engine = await _resume(run_root, _PAIR_JIL)
        assert engine.journal is not None
        # the catch-up's owed step makes them pending, in the loop (spec-restart §6)
        assert engine.journal.open.pending == {}
        real = engine.journal.append
        dispatches: list[int] = []

        def die_at_the_second(*records: Any) -> int:
            if any(r.kind == "dispatch" for r in records):
                dispatches.append(1)
                if len(dispatches) == 2:
                    raise RuntimeError("the engine died at the second owed start")
            return real(*records)

        engine.journal.append = die_at_the_second  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="second owed start"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        engine.journal.append = real  # type: ignore[method-assign]
        assert len(engine._deferred) == 1  # the one that raised, still owed
        [left] = engine.journal.open.pending.values()
        engine.take_snapshot()
        await _close(engine)
        snapshot = decode_snapshot(0, _latest_snapshot_data(run_root), where="t")
        assert [p.job for p in snapshot.open.pending.values()] == [left.job]

    asyncio.run(crashed())
    pendings = {
        body["job"]: seq
        for seq, kind, body in rows(run_root)
        if kind == "dispatch_pending" and body["event"] == "pending"
    }
    [(dispatched, _)] = [(b["job"], b) for b in _kinds(run_root, "dispatch")]
    left_job = ({"j", "k"} - {dispatched}).pop()

    async def repaired() -> None:
        await _close(await _recovered(await _resume(run_root, _PAIR_JIL, minutes=4)))

    asyncio.run(repaired())
    by_job = [(b["job"], b["ordinal"], b.get("discharges")) for b in _kinds(run_root, "dispatch")]
    assert (left_job, 1, pendings[left_job]) in by_job
    assert [job for job, ordinal, _ in by_job if ordinal == 1].count(left_job) == 1
    assert [job for job, ordinal, _ in by_job if ordinal == 1].count(dispatched) == 1


def _latest_snapshot_data(run_root: Path) -> bytes:
    from dsl41.runner_journal import open_reader

    with closing(open_reader(run_root)) as reader:
        stored = reader.latest_snapshot()
    assert stored is not None
    return stored.data


def test_startup_records_engine_started_after_steps_6_and_7_and_then_the_drops(
    tmp_path: Path,
) -> None:
    """spec-restart §6's order: the observation's outcome and plan, its replacement dispatch
    behind the barrier, then `engine started` and the missed-tick drops. The fold writes
    nothing: every record of the catch-up is the loop's."""
    text = _JIL + "\ninsert_job: t\njob_type: c\ncommand: y\ndate_conditions: 1\n"
    text += 'days_of_week: all\nstart_times: "08:30"\n'
    run_root = tmp_path / "run"

    async def first() -> None:
        catalog = lower_source(text)
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=T0),
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await _close(engine)

    asyncio.run(first())
    before = len(rows(run_root))

    async def restart() -> None:
        catalog = lower_source(text)
        engine = await _resume(run_root, text, minutes=60, scheduler=Scheduler(catalog, start=T0))
        assert not engine.started.is_set()
        assert [kind for _, kind, _ in rows(run_root)[before:]] == []
        await _close(await _recovered(engine))

    asyncio.run(restart())
    after = [(kind, body.get("event")) for _, kind, body in rows(run_root)[before:]]
    assert after == [
        ("outcome", None),
        ("follow_up", "plan"),
        ("dispatch", None),
        ("follow_up", "done"),
        ("engine", "started"),
        ("drop", None),
    ]


def test_a_starting_overwrite_after_a_snapshot_invents_no_second_start(tmp_path: Path) -> None:
    """The snapshot seeds replay's ghost-run gate: a CHANGE_STATUS STARTING for a run the
    snapshot holds plans no start live (DL-234), and none on a restart either. Two restarts
    each run the interrupted attempt again as the next ordinal; no ordinal is reused, and each
    attempt has its own spool."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        engine.take_snapshot()
        engine.inject(_ev("STATUS", 2, job="j", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _close(engine)

    asyncio.run(first())
    assert len(_kinds(run_root, "dispatch")) == 1  # live: the overwrite launched nothing

    async def restart(minutes: float) -> None:
        await _close(await _recovered(await _resume(run_root, minutes=minutes)))

    asyncio.run(restart(4))
    asyncio.run(restart(5))
    dispatches = _kinds(run_root, "dispatch")
    assert [(b["run_number"], b["ordinal"]) for b in dispatches] == [(1, 1), (1, 2), (1, 3)]
    assert len({b["spool"] for b in dispatches}) == 3
    assert len({b["attempt"] for b in dispatches}) == 3
    assert not [b for b in _kinds(run_root, "dispatch_pending") if b["event"] == "pending"]


class _CountingAdapter(FakeAdapter):
    """A FakeAdapter that records every job it was asked to run."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def run(self, job_ir: Any, run_number: int, ctx: Any) -> int:
        self.calls.append(job_ir.name)
        return await super().run(job_ir, run_number, ctx)


def test_a_dependent_of_a_recovered_completion_waits_behind_the_recovery_barrier(
    tmp_path: Path,
) -> None:
    """DL-425: no launch past its deadline at a restart, for the dependents of a recovered
    completion too. The completion of `a` is fed from its plan at the restart; it starts `b`,
    whose term_run_time is already past. The start is owed, not launched, so the overdue
    deadline ends the run first: `b` is never dispatched and its adapter is never called."""
    text = (
        "insert_job: a\njob_type: c\ncommand: x\n\n"
        "insert_job: b\njob_type: c\ncommand: y\ncondition: s(a)\nterm_run_time: 1\n"
    )
    run_root = tmp_path / "run"

    async def first() -> None:  # `a` ends, and the engine dies before its completion lands
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("a", 1): (5.0, 0)}, default=None)},
        )
        assert engine.journal is not None
        real = engine.journal.append

        def die_at_the_feed(*records: Any) -> int:
            if any(r.kind == "follow_up" and r.payload["event"] == "done" for r in records):
                raise RuntimeError("the engine died before the completion landed")
            return real(*records)

        engine.journal.append = die_at_the_feed  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 0, job="a"))
        with pytest.raises(RuntimeError, match="before the completion landed"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        engine.journal.append = real  # type: ignore[method-assign]
        await _close(engine)

    asyncio.run(first())
    adapter = _CountingAdapter(default=None)

    async def restart() -> Engine:
        engine = await _resume(run_root, text, minutes=10, adapter=adapter)
        # past the restart instant, so the virtual domain fires what is overdue (the frontier
        # rule)
        await engine.run_until_quiescent(T0 + timedelta(minutes=11))
        await _close(engine)
        return engine

    engine = asyncio.run(restart())
    assert engine.oracle.store.job["a"].status == "SUCCESS"
    assert engine.oracle.store.job["b"].status == "TERMINATED"
    assert [b["job"] for b in _kinds(run_root, "dispatch")] == ["a"]
    assert adapter.calls == []
    [discharged] = [b for b in _kinds(run_root, "dispatch_pending") if b["event"] == "discharged"]
    assert discharged["dispatch"] is None


@pytest.mark.parametrize("fault", ["first", "between"])
def test_a_failed_dispatch_is_not_an_applied_boundary_and_a_restart_recovers_it(
    tmp_path: Path, fault: str
) -> None:
    """An admitted start with neither its `dispatch` nor its `dispatch_pending` keeps the
    engine off the applied boundary: the failure cleanup takes no snapshot that would hide it,
    and the restart, from the snapshot before it, derives the start and dispatches it. The
    fault lands before the first sibling's dispatch, or between two siblings'."""
    from dsl41.cli_run import _record_shutdown

    text = (
        "insert_job: b\njob_type: b\n\n"
        "insert_job: m1\njob_type: c\ncommand: x\nbox_name: b\n\n"
        "insert_job: m2\njob_type: c\ncommand: x\nbox_name: b\n"
    )
    run_root = tmp_path / "run"

    async def first() -> int:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(),
        )
        engine.inject(_ev("SET_GLOBAL", 0, name="G", value="v"))
        await engine.run_until_quiescent(T0)
        seq = engine.take_snapshot()
        assert engine.journal is not None
        real = engine.journal.append
        seen: list[int] = []

        def die_at_a_dispatch(*records: Any) -> int:
            if any(r.kind == "dispatch" for r in records):
                seen.append(1)
                if len(seen) == (1 if fault == "first" else 2):
                    raise RuntimeError("the dispatch failed")
            return real(*records)

        engine.journal.append = die_at_a_dispatch  # type: ignore[method-assign]
        engine.inject(_ev("STARTJOB", 1, job="b"))
        with pytest.raises(RuntimeError, match="the dispatch failed"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        engine.journal.append = real  # type: ignore[method-assign]
        assert not engine.at_applied_boundary
        await engine.shutdown()
        _record_shutdown(engine, running=True)  # the CLI's failure cleanup: no record
        engine.journal.close()
        return seq

    seq = asyncio.run(first())
    from dsl41.runner_journal import open_reader

    with closing(open_reader(run_root)) as reader:
        stored = reader.latest_snapshot()
    assert stored is not None and stored.seq == seq  # no snapshot hid the start
    before = {b["job"] for b in _kinds(run_root, "dispatch")}
    assert before == (set() if fault == "first" else {"m1"})

    async def restart() -> None:
        await _close(await _recovered(await _resume(run_root, text, minutes=3)))

    asyncio.run(restart())
    started = [(b["job"], b["ordinal"]) for b in _kinds(run_root, "dispatch")]
    assert ("m2", 1) in started and started.count(("m2", 1)) == 1
    assert started.count(("m1", 1)) == 1


def test_a_pruned_request_id_is_free_before_and_after_a_restart(tmp_path: Path) -> None:
    """spec-journal: retention is the one authority for retry. Once a request's decision is
    pruned its id is free: the live engine forgets it at the prune, so the same command under
    the same id is admitted again, as it is after a restart."""
    run_root = tmp_path / "run"

    def setter(name: str, minute: float) -> tuple[Event, Envelope]:
        envelope = Envelope(request_id=f"r-{name}", expect={f"global:{name}": 0})
        return _ev("SET_GLOBAL", minute, name=name, value="v"), envelope

    async def first() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            settings=EngineSettings(retention_days=1),
        )
        a, b = engine.submit(*setter("G", 0)), engine.submit(*setter("H", 0))
        await engine.run_until_quiescent(T0)
        old = {(await a).index, (await b).index}
        engine.inject(_ev("SET_GLOBAL", 3 * 24 * 60, name="X", value="late"))
        await engine.run_until_quiescent(T0 + timedelta(days=3))
        engine.take_snapshot()
        assert engine.journal is not None
        assert engine.journal.store.find_request("r-G") is None  # pruned
        assert engine.decisions.state("r-G") is None  # and forgotten
        again = engine.submit(*setter("G", 3 * 24 * 60 + 1))
        await engine.run_until_quiescent(T0 + timedelta(days=3, minutes=2))
        assert (await again).index not in old
        assert engine.deduped == []
        await _close(engine)

    asyncio.run(first())

    async def second() -> None:
        engine = await _resume(run_root, minutes=3 * 24 * 60 + 5)
        await _recovered(engine)
        again = engine.submit(*setter("H", 3 * 24 * 60 + 6))
        await engine.run_until_quiescent(T0 + timedelta(days=3, minutes=10))
        assert (await again).index > 2
        assert engine.deduped == []
        await _close(engine)

    asyncio.run(second())


def test_the_command_is_given_the_invocation_its_dispatch_record_holds(tmp_path: Path) -> None:
    """The crash spec's invocation reaches the process: the wrapper spec carries the
    `dispatch` record's `env`, so the command sees DSL41_JOB, DSL41_RUN_NUMBER,
    DSL41_SCHEDULED_AT and DSL41_ATTEMPT as the record holds them."""
    from dsl41.runner_adapters import LocalCommandAdapter
    from dsl41.runner_clock import RealClock

    seen = tmp_path / "seen.txt"
    text = (
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: printf '%s|%s|%s|%s'"
        ' "$DSL41_JOB" "$DSL41_RUN_NUMBER" "$DSL41_SCHEDULED_AT" "$DSL41_ATTEMPT"'
        f" > {seen}\n"
    )
    run_root = tmp_path / "run"

    async def scenario() -> None:
        clock = RealClock()
        engine = start_run(
            lower_source(text),
            run_root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(grace_seconds=1.0)},
        )
        engine.inject(Event(at=clock.now(), kind="STARTJOB", payload={"job": "j"}))
        await engine.run_until_quiescent(datetime.max)
        await _close(engine)

    asyncio.run(scenario())
    [dispatch] = _kinds(run_root, "dispatch")
    env = dispatch["env"]
    job, run_number, scheduled, attempt = seen.read_text().split("|")
    assert (job, run_number) == ("j", "1") == (env["DSL41_JOB"], env["DSL41_RUN_NUMBER"])
    # DSL41_ATTEMPT is the ordinal; the record's `attempt` is the run id (DL-118, DL-426)
    assert (
        (scheduled, attempt)
        == (env["DSL41_SCHEDULED_AT"], str(dispatch["ordinal"]))
        == (
            env["DSL41_SCHEDULED_AT"],
            "1",
        )
    )


def test_a_pruned_store_with_no_usable_snapshot_is_refused_by_its_readers() -> None:
    """A pruned head with no snapshot breaks the pruner's rule, and a snapshot another state
    version wrote cannot anchor a replay: the backfill and a reader's replay refuse both by
    name. With no decision retained, the pruner's eviction bound is the one past the last
    admitted index."""
    from dsl41.runner import _first_decision_index
    from dsl41.runner_clock import EngineError
    from dsl41.runner_journal import BackfillRefused, replay_store
    from dsl41.runner_store import Record

    store = MemoryStore()
    for _ in range(3):
        store.append(Record(T0, "engine", None, None, {"event": "started", "at": T0.isoformat()}))
    store.put_snapshot(2, 999, b"{}")
    store.prune_before(2, datetime(2999, 1, 1))
    assert _first_decision_index(store, 7) == 7
    with pytest.raises(EngineError, match="state version 999"):
        replay_store(store, where="here")
    bare = MemoryStore()
    bare.append(Record(T0, "engine", None, None, {"event": "started", "at": T0.isoformat()}))
    bare.append(Record(T0, "engine", None, None, {"event": "started", "at": T0.isoformat()}))
    bare.put_snapshot(2, 1, b"{}")
    bare.prune_before(2, datetime(2999, 1, 1))
    bare._snapshots.clear()  # type: ignore[attr-defined]
    with pytest.raises(BackfillRefused, match="holds no snapshot"):
        read_backfill(bare, since=0, upto=bare.last_seq())


def test_evicting_below_every_decision_empties_the_index() -> None:
    """A prune that removed every decision frees every decided id; an id still in flight has no
    decision and stays."""
    from dsl41.runner_journal import read_decisions

    def attempt(index: int, request_id: str) -> dict[str, Any]:
        return {
            "rec": "advance",
            "seq": index,
            "at": T0.isoformat(),
            "request_id": request_id,
            "fingerprint": "f",
        }

    decided = {"rec": "decision", "index": 1, "request_id": "r1", "decision": "applied"}
    index = read_decisions([attempt(1, "r1"), decided, attempt(2, "r2")])
    index.evict_below(10)
    assert index.state("r1") is None and index.for_index(1) is None
    assert index.state("r2") == "admitted"
