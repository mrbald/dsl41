"""Store-backed journal tests: record shapes, the wire projection, the open table, replay,
and the `dsl41 journal` CLI.

Normative spec: spec-wiring 1b (the store replaces the journal), docs/runner-design.md ss7
(record kinds and recovery) and ss4 (the stale-completion gate whose rejections the store also
records). runner_journal.py's Journal/read_journal/replay_inputs docstrings are the API under
test; cli_run.py's `journal` command is the CLI surface. Restart and the reconciliation ladder
are tests/test_runner_lifecycle.py's territory -- this file stays on the record itself: what
gets written, how the wire sees it, and that replaying the inputs alone reproduces the exact
trace a live engine produced.

House style follows test_runner.py: T0 = datetime(2026, 7, 1, 8, 0), an ev() helper, one
asyncio.run per async scenario.
"""

from __future__ import annotations

from contextlib import closing

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, EventKind
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import ApplyResult, Attempt, Frontiers, fingerprint
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.reload_diff import catalog_hash_v2 as catalog_hash
from dsl41.reload_plan import load_estate, reread
from dsl41.runner_config import Configuration
from dsl41.runner_journal import (
    BackfillRefused,
    Journal,
    OpenTable,
    Position,
    decode_snapshot,
    dispatch_record,
    encode_snapshot,
    follow_up_done,
    read_backfill,
    read_journal,
    replay_inputs,
    replay_store,
    wire_record,
)
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import active_configuration, resume_run, start_run
from dsl41.runner_store import MemoryStore, Record, SqliteStore
from dsl41.runner_update import UpdateRequest
from dsl41.state_machine import Executor

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
_IDENTITY = {"baseline_id": "b", "clock_domain": "virtual", "state_version": "1"}


def _journal(tmp_path: Path) -> Journal:
    """A journal over a fresh store file under `tmp_path`."""
    path = tmp_path / "engine.db"
    return Journal(SqliteStore(path, identity=_IDENTITY), path=path)


# --------------------------------------------------------------- 1. record shapes


def test_genesis_opens_with_the_configuration_the_wire_names_segment(tmp_path: Path) -> None:
    """(spec-wiring 1b): genesis records configuration rev 1 and `engine started`. The wire
    projects the `config` record as the `segment` a subscriber met first in 1.x: the catalog
    hash, the runtime hash, the state-machine version, the baseline and the clock domain. The
    build's version is deliberately absent: a patch release must not move these bytes
    (PR-07)."""
    catalog = lower_source(_SOLO_JIL)
    engine = start_run(
        catalog, tmp_path / "run", clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )
    assert engine.journal is not None
    engine.journal.close()
    segment = read_journal(tmp_path / "run")[0]
    assert segment == {
        "rec": "segment",
        "rev": 1,
        "at": T0.isoformat(),
        "catalog_hash": catalog_hash(catalog),
        "runtime_hash": segment["runtime_hash"],
        "state_machine_version": STATE_MACHINE_VERSION,
        "baseline_id": engine.baseline_id,
        "clock_domain": "virtual",
    }
    assert segment["runtime_hash"].startswith("sha256:")
    assert "dsl41_version" not in segment
    meta = SqliteStore(tmp_path / "run" / "engine.db", read_only=True)
    try:
        assert meta.meta()["state_version"] == str(STATE_MACHINE_VERSION)
        assert meta.current_config_rev() == 1
        assert [r.kind for _, r in meta.read_from(0)] == ["config", "engine"]
    finally:
        meta.close()


def test_the_writer_refuses_a_record_this_binary_does_not_write(tmp_path: Path) -> None:
    """DL-145's write-side registry: a record's kind is one of `KINDS`, an `engine` record names
    a lifecycle event, an `outcome` one of `OUTCOMES`, a plan one of `PLANS`, or nothing is
    appended, not even the records beside it in the transaction."""
    journal = _journal(tmp_path)
    plan = {"event": "plan", "attempt": "a", "job": "j", "run_number": 1, "steps": []}
    refused = [
        (Record(T0, "tally", None, None, {"n": 1}), "record kind 'tally'"),
        (Record(T0, "segment", None, None, {"n": 1}), "record kind 'segment'"),
        (Record(T0, "engine", None, None, {"event": "paused"}), "engine record 'paused'"),
        (Record(T0, "outcome", None, None, {"outcome": "vanished"}), "outcome 'vanished'"),
        (Record(T0, "follow_up", None, None, {**plan, "plan": "retry"}), "plan 'retry'"),
    ]
    try:
        for record, message in refused:
            good = Record(T0, "engine", None, None, {"event": "started"})
            with pytest.raises(EngineError, match=message):
                journal.append(good, record)
        with pytest.raises(EngineError, match="engine record 'paused'"):
            journal.engine("paused", T0)
    finally:
        journal.close()
    assert read_journal(tmp_path) == []


def test_every_reader_refuses_a_record_kind_this_binary_does_not_know(tmp_path: Path) -> None:
    """The read side of the registry (protocol-evolution: an unknown kind refuses by name): the
    wire projection, the open table and the backfill each name the kind, and none skips it."""
    store = MemoryStore()
    store.append(Record(T0, "tally", None, None, {"n": 1}))
    ((seq, record),) = list(store.read_from(0))
    with pytest.raises(EngineError, match="record kind 'tally'"):
        wire_record(record)
    with pytest.raises(EngineError, match="record kind 'tally'"):
        OpenTable().note(seq, record)
    with pytest.raises(EngineError, match="record kind 'tally'"):
        read_journal(store)
    with pytest.raises(BackfillRefused, match="record kind 'tally'"):
        read_backfill(store, since=0, upto=seq)


def _attempt(index: int, event: Event | None, *, source: str | None = "control") -> Attempt:
    at = event.at if event is not None else T0 + timedelta(minutes=index)
    return Attempt(
        index=index,
        at=at,
        request_id=f"r{index}",
        fingerprint=fingerprint(
            baseline_id="b",
            kind=event.kind if event else None,
            payload=dict(event.payload) if event else {},
            source=source if event else None,
        ),
        kind=event.kind if event else None,
        payload=dict(event.payload) if event else {},
        source=source if event else None,
    )


def _applied(attempt: Attempt) -> ApplyResult:
    return ApplyResult(index=attempt.index, request_id=attempt.request_id, decision="applied")


def test_input_record_carries_seq_at_kind_payload_source_and_the_admission_fields(
    tmp_path: Path,
) -> None:
    """(runner-design ss7 + concurrency-model ss4): on the wire an input is {seq, at, kind,
    payload, source} plus the `request_id` a retry is answered by and the `fingerprint` that
    catches an id reused for a different command. `seq` is the admission index. An attempt
    and its decision are ONE store transaction, so no crash can leave one without the other."""
    journal = _journal(tmp_path)
    first = _attempt(1, ev("STARTJOB", 0, job="j1"))
    second = _attempt(2, ev("STATUS", 1, job="j1", run_number=1, exit_code=0), source="adapter")
    assert journal.admission(first, _applied(first)) == 2
    assert journal.admission(second, _applied(second)) == 4
    journal.close()
    records = read_journal(tmp_path)
    inputs = [r for r in records if r["rec"] == "input"]
    assert inputs[0] == {
        "rec": "input",
        "seq": 1,
        "at": T0.isoformat(),
        "kind": "STARTJOB",
        "payload": {"job": "j1"},
        "source": "control",
        "request_id": "r1",
        "fingerprint": first.fingerprint,
    }
    assert inputs[1]["seq"] == 2
    assert inputs[1]["source"] == "adapter"
    assert inputs[1]["payload"] == {"job": "j1", "run_number": 1, "exit_code": 0}
    assert [r["rec"] for r in records] == ["input", "decision", "input", "decision"]


def test_a_time_observation_is_admitted_as_an_attempt_with_no_verb(tmp_path: Path) -> None:
    """(concurrency-model ss4): the standalone time observation takes the same path as an
    operator command and leaves the same kind of record -- an `input` with no verb, which the
    wire calls `advance`. Its decision rides under `index`, not `seq`: a decision shares its
    attempt's number, and the subscribe cursor is keyed on seq."""
    journal = _journal(tmp_path)
    attempt = _attempt(1, None)
    journal.admission(attempt, _applied(attempt))
    journal.close()
    records = read_journal(tmp_path)
    advance = next(r for r in records if r["rec"] == "advance")
    assert advance == {
        "rec": "advance",
        "seq": 1,
        "at": (T0 + timedelta(minutes=1)).isoformat(),
        "request_id": "r1",
        "fingerprint": advance["fingerprint"],
    }
    # no `expect`: ss0's mandate is on EXTERNALLY REQUESTED mutations
    assert "expect" not in advance and "claimed_actor" not in advance
    decision = next(r for r in records if r["rec"] == "decision")
    assert decision == {
        "rec": "decision",
        "index": 1,
        "request_id": "r1",
        "decision": "applied",
        "reason": None,
        "code": None,
        "revisions": {},
        "legacy_batch": False,
    }


def test_a_native_rejection_names_its_stored_code(tmp_path: Path) -> None:
    """A rejection names one of the append-only stored codes at birth (period-model ss2.3);
    one that does not is refused and nothing is appended."""
    journal = _journal(tmp_path)
    attempt = _attempt(1, ev("STARTJOB", 0, job="j1"))
    with pytest.raises(EngineError, match="names its stored code"):
        journal.admission(
            attempt,
            ApplyResult(index=1, request_id="r1", decision="rejected", reason="no", code="made_up"),
        )
    journal.close()
    assert read_journal(tmp_path) == []


def test_dispatch_record_carries_job_run_number_run_id_run_dir_started_at(
    tmp_path: Path,
) -> None:
    """(runner-design ss7): a dispatch record precedes its attempt's adapter task and names
    the attempt id the wrapper spec carries as `run_id`. The store keeps the ordinal and the
    invocation; the wire keeps the 1.x shape, with the attempt's spool as `run_dir`, and adds
    the configuration rev the run was dispatched under (DL-427)."""
    journal = _journal(tmp_path)
    run_id = "00000000-0000-4000-8000-000000000001"
    journal.append(
        dispatch_record(
            "j1", 1, attempt=run_id, spool="/abs/runs/j1.1/1", started_at=T0, rev=1, env={"A": "1"}
        )
    )
    with pytest.raises(EngineError, match="outside the ss11a uuid4 grammar"):
        dispatch_record("j1", 1, attempt="freehand", spool=None, started_at=T0, rev=1)
    journal.close()
    dispatch = next(r for r in read_journal(tmp_path) if r["rec"] == "dispatch")
    assert dispatch == {
        "rec": "dispatch",
        "job": "j1",
        "run_number": 1,
        "run_id": run_id,
        "run_dir": "/abs/runs/j1.1/1",
        "started_at": T0.isoformat(),
        "rev": 1,
    }


def test_drop_record_carries_at_kind_payload_reason_and_no_seq(tmp_path: Path) -> None:
    """(runner-design ss7): drop records an input refused before admission, a scheduler tick
    missed across downtime (E9); it carries no seq -- it never fed the oracle."""
    journal = _journal(tmp_path)
    journal.drop(ev("STATUS", 3, job="j1", run_number=0, exit_code=0), "run_number mismatch")
    journal.close()
    drop = next(r for r in read_journal(tmp_path) if r["rec"] == "drop")
    assert drop == {
        "rec": "drop",
        "at": (T0 + timedelta(minutes=3)).isoformat(),
        "kind": "STATUS",
        "payload": {"job": "j1", "run_number": 0, "exit_code": 0},
        "reason": "run_number mismatch",
    }


def test_the_kinds_the_wire_does_not_carry_project_to_nothing() -> None:
    """The outcome, its plan, a pending dispatch, an engine record and a configuration intent
    are the store's own: the frozen wire has no name for them (control-protocol ss5)."""
    for kind, payload in (
        ("outcome", {"attempt": "a", "outcome": "natural"}),
        ("follow_up", {"event": "plan", "plan": "complete"}),
        ("dispatch_pending", {"event": "pending"}),
        ("engine", {"event": "started"}),
        ("config", {"event": "intent"}),
    ):
        assert wire_record(Record(T0, kind, None, None, payload)) is None


# ------------------------------------------------------------- 2. the open table


def test_the_open_table_follows_an_attempt_a_pending_start_and_a_plan() -> None:
    """(spec-wiring 1b): a dispatch opens an attempt and an outcome closes it; a pending start
    closes when a dispatch discharges it; a plan stays until every step has its done mark. The
    floor is the oldest seq any of them refers to, and the snapshot carries the table whole."""
    table = OpenTable()
    attempt = "00000000-0000-4000-8000-000000000001"
    table.note(
        3,
        Record(
            T0,
            "dispatch_pending",
            None,
            None,
            {
                "event": "pending",
                "job": "p",
                "run_number": 1,
                "scheduled_at": T0.isoformat(),
            },
        ),
    )
    table.note(5, dispatch_record("j1", 1, attempt=attempt, spool=None, started_at=T0, rev=1))
    plan = {"event": "plan", "attempt": attempt, "job": "j1", "run_number": 1, "plan": "hold"}
    steps = [{"step": "feed", "payload": {}}, {"step": "hold", "payload": {}}]
    table.note(6, Record(T0, "follow_up", None, None, {**plan, "steps": steps}))
    table.note(7, Record(T0, "outcome", None, None, {"attempt": attempt, "outcome": "natural"}))
    assert table.floor() == 3 and list(table.attempts) == []
    assert OpenTable.from_payload(table.to_payload()).to_payload() == table.to_payload()
    table.note(8, follow_up_done(attempt, 0, 7, T0))
    assert table.plans[attempt].next_step() == 1
    table.note(9, follow_up_done(attempt, 1, None, T0))
    assert table.plans == {}
    other = "00000000-0000-4000-8000-000000000002"
    table.note(
        10, dispatch_record("p", 1, attempt=other, spool=None, started_at=T0, rev=1, discharges=3)
    )
    assert table.pending == {} and table.floor() == 10
    # a plan with no steps is never open; a done mark for a closed plan changes nothing
    table.note(11, Record(T0, "follow_up", None, None, {**plan, "plan": "none", "steps": []}))
    table.note(12, follow_up_done("gone", 0, 1, T0))
    assert table.plans == {}
    table.note(
        13,
        Record(
            T0,
            "dispatch_pending",
            None,
            None,
            {
                "event": "discharged",
                "pending": 99,
                "dispatch": None,
            },
        ),
    )


def test_a_snapshot_round_trips_and_a_foreign_one_is_refused_by_name() -> None:
    """The snapshot holds the oracle's rows, the log's position, the scheduler's position, the
    configuration rev and the open table; a snapshot that does not read is refused naming
    where it was read."""
    oracle = Oracle(lower_source(_SOLO_JIL))
    oracle.feed(ev("STARTJOB", 0, job="j1"))
    position = Position()
    position.note(Record(T0, "config", None, None, {"event": "applied", "at": T0.isoformat()}))
    data = encode_snapshot(
        oracle,
        frontiers=Frontiers(committed_index=1, applied_index=1, at=T0),
        position=position,
        config_rev=1,
        open_table=OpenTable(),
    )
    snapshot = decode_snapshot(7, data, where="here")
    assert snapshot.seq == 7 and snapshot.config_rev == 1
    assert snapshot.frontiers.applied_index == 1 and snapshot.position.frontier == T0
    assert snapshot.rows.jobs["j1"].status == oracle.store.job["j1"].status
    with pytest.raises(EngineError, match="here: unreadable snapshot at seq 7"):
        decode_snapshot(7, b"[]", where="here")
    with pytest.raises(EngineError, match="here: unreadable snapshot at seq 7"):
        decode_snapshot(7, b"{}", where="here")


# ------------------------------------------------------------- 3. catalog_hash


def test_catalog_hash_stable_across_identical_loads() -> None:
    """(catalog_hash docstring): a sha256 of the canonical form -- the same estate text loaded
    twice hashes identically."""
    assert catalog_hash(lower_source(_SOLO_JIL)) == catalog_hash(lower_source(_SOLO_JIL))


def test_catalog_hash_differs_on_any_change() -> None:
    """(catalog_hash docstring): an estate that changed in ANY way is another catalog -- even
    a one-token command change flips it."""
    changed = _SOLO_JIL.replace("command: x", "command: y")
    assert catalog_hash(lower_source(_SOLO_JIL)) != catalog_hash(lower_source(changed))


# ------------------------------------------------------------------ 4. replay


async def _run_virtual(
    text: str,
    run_root: Path,
    script: list[Event],
    *,
    adapter: FakeAdapter,
    horizon: datetime,
    scheduler: Scheduler | None = None,
) -> Engine:
    engine = start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter, "FW": adapter},
        scheduler=scheduler,
    )
    for e in script:
        engine.inject(e)
    await engine.run_until_quiescent(horizon)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()
    return engine


def test_replay_reproduces_a_startjob_cascade_and_killjob_trace(tmp_path: Path) -> None:
    """(runner-design ss7 inputs-only principle): a virtual-domain engine
    run's WAL, replayed through a fresh Oracle, reproduces the SAME trace the
    live engine produced -- byte-identical, proving emitted events really are
    a pure function of the input sequence and nothing else is ever needed to
    reconstruct them."""
    text = (
        "insert_job: job_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: job_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(job_a)\n"
    )
    adapter = FakeAdapter({("job_a", 1): (300.0, 0)})
    script = [ev("STARTJOB", 0, job="job_a"), ev("KILLJOB", 2, job="job_a")]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=30)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["job_a"].status == "TERMINATED"  # killed before its 5min exit
    assert engine.oracle.store.job["job_b"].status == "INACTIVE"  # s(job_a) never satisfied

    records = read_journal(tmp_path / "run")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]


def test_replay_reproduces_a_set_global_gated_trace(tmp_path: Path) -> None:
    """(runner-design ss7): a second, structurally different script --
    SET_GLOBAL rather than STARTJOB/KILLJOB -- reproduces just as exactly,
    proving replay_inputs is not special-cased to any one event kind."""
    text = "insert_job: gate\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"
    adapter = FakeAdapter({("gate", 1): (5.0, 0)})
    script = [
        ev("SET_GLOBAL", 0, name="FLAG", value="stop"),
        ev("SET_GLOBAL", 1, name="FLAG", value="go"),
    ]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=10)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["gate"].status == "SUCCESS"

    records = read_journal(tmp_path / "run")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]


def test_replay_reproduces_a_disarm_trace_to_the_same_state(tmp_path: Path) -> None:
    """DL-158: a journal carrying a DISARM replays to the same state. The
    live run arms on a tick, the operator disarms, the later condition edge
    starts nothing; a fresh Oracle fed the same WAL reaches the identical
    trace and the identical unarmed, INACTIVE row."""
    text = (
        "insert_job: dis_r\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\n'
        "condition: s(gate_r)\n\n"
        "insert_job: gate_r\njob_type: c\ncommand: y\nmachine: m1\n"
    )
    adapter = FakeAdapter(default=None)
    script = [
        ev("STARTJOB", 0, job="dis_r"),  # condition false -> arms
        ev("DISARM", 1, job="dis_r"),
        ev("STATUS", 2, job="gate_r", status="SUCCESS"),  # edge meets no latch
    ]

    async def scenario() -> Engine:
        # the wired scheduler's own 23:00 tick is far past the horizon: the
        # script injects the tick itself, so the run is deterministic
        return await _run_virtual(
            text,
            tmp_path / "run",
            script,
            adapter=adapter,
            horizon=T0 + timedelta(minutes=10),
            scheduler=Scheduler(lower_source(text), start=T0),
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["dis_r"].status == "INACTIVE"  # no start followed the edge
    assert engine.oracle.store.job["dis_r"].armed is False

    records = read_journal(tmp_path / "run")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]
    assert fresh.store.job["dis_r"].armed is False
    assert fresh.store.job["dis_r"].status == "INACTIVE"


def test_replay_reproduces_held_units_and_a_release_resource(tmp_path: Path) -> None:
    """DL-256: a journal carrying a held unit and a RELEASE_RESOURCE replays
    to the same state. The holder fails and keeps its unit (an omitted FREE,
    the vendor default), the waiter queues, the operator releases, and the
    waiter runs; a fresh Oracle fed the same WAL reaches the identical trace
    and rows."""
    text = (
        "insert_resource: JLOCK\nres_type: R\namount: 1\n\n"
        "insert_job: jh\njob_type: c\ncommand: x\nmachine: m1\nresources: (JLOCK, QUANTITY=1)\n\n"
        "insert_job: jw\njob_type: c\ncommand: y\nmachine: m1\nresources: (JLOCK, QUANTITY=1)\n"
    )
    adapter = FakeAdapter({("jh", 1): (30.0, 1)}, default=None)
    script = [
        ev("STARTJOB", 0, job="jh"),
        ev("STARTJOB", 0, job="jw"),
        ev("RELEASE_RESOURCE", 2, job="jh"),
    ]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=5)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["jh"].status == "FAILURE"
    assert engine.oracle.store.job["jh"].reservations == ()
    assert engine.oracle.store.job["jw"].status == "RUNNING"

    records = read_journal(tmp_path / "run")
    assert [r["kind"] for r in records if r.get("rec") == "input"].count("RELEASE_RESOURCE") == 1
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]
    assert dict(fresh.store.job) == dict(engine.oracle.store.job)


def test_replay_refuses_an_unknown_event_kind_by_name(tmp_path: Path) -> None:
    """protocol-evolution ss1 (the WAL row) at the event alphabet, the wall
    DL-158's compatibility statement names: an `input` record whose kind is
    outside this build's alphabet refuses BY NAME at `Attempt.event()`,
    never as a raw pydantic traceback -- this is what an old binary meets
    when a newer writer's kind (DISARM then, anything later) sits in the
    journal it replays."""
    text = "insert_job: solo_uk\njob_type: c\ncommand: x\nmachine: m1\n"
    adapter = FakeAdapter({("solo_uk", 1): (1.0, 0)})
    script = [ev("STARTJOB", 0, job="solo_uk")]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=5)
        )

    asyncio.run(scenario())
    records = read_journal(tmp_path / "run")
    doctored = [
        {**r, "kind": "FUTURE_KIND"} if r.get("rec") == "input" and r.get("kind") else r
        for r in records
    ]
    fresh = Oracle(lower_source(text))
    with pytest.raises(EngineError, match="'FUTURE_KIND'.*alphabet does not include"):
        replay_inputs(fresh, doctored)


def test_replay_reproduces_source_tagged_causes_and_started_by(tmp_path: Path) -> None:
    """(DL-68): input records persist source, so a fresh Oracle replay
    re-derives the SAME "(control)"-tagged cause and started_by the live
    engine recorded -- provenance survives a resume."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    adapter = FakeAdapter({("j1", 1): (5.0, 0)})
    script = [ev("STARTJOB", 0, job="j1")]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=10)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j1"].started_by == "STARTJOB event (control)"

    records = read_journal(tmp_path / "run")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert fresh.store.job["j1"].started_by == "STARTJOB event (control)"
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]


# ------------------------------------------------ 5. engine journal-first behavior


def test_input_records_tag_injected_events_control_and_completions_adapter(
    tmp_path: Path,
) -> None:
    """(runner-design ss7 input record: source in {scheduler, adapter,
    control, reconcile}): Engine.inject() defaults to source="control";
    an adapter completion enqueues with source="adapter" (Engine._run_adapter
    -> _enqueue's default) -- both land in the WAL untouched."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j1", 1): (60.0, 0)})},
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    inputs = [r for r in read_journal(tmp_path / "run") if r["rec"] == "input"]
    assert inputs[0]["kind"] == "STARTJOB"
    assert inputs[0]["source"] == "control"
    completion = next(r for r in inputs if r["kind"] == "STATUS")
    assert completion["source"] == "adapter"
    assert completion["payload"] == {"job": "j1", "run_number": 1, "exit_code": 0}


def test_gate_dropped_completion_is_journaled_as_an_admitted_rejection(tmp_path: Path) -> None:
    """(runner-design ss4 stale-completion gate + ss7, concurrency-model
    ss4): a completion the engine drops (run_number mismatch) never feeds
    the oracle but IS journaled -- "never a silent overwrite" extends to
    never silently vanishing either.

    S2 changed WHERE it lands. The gate now runs after admission, so the
    completion is in the log as an attempt and its rejection is in the log
    as that attempt's result. That is not bookkeeping: a `drop` record was
    absence, and absence cannot be told apart from a crash, so replay had
    to guess. A recorded rejection is a decision replay can honour.

    White-box via the same Engine._enqueue(..., source="adapter") trick
    test_runner.py's gate test uses -- 11a has no black-box trigger for this
    gate under VirtualClock (runner.py module docstring: the
    natural-exit-vs-kill race always resolves to the kill there)."""
    text = "insert_job: sa\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("sa", 1): (60.0, 0)})},
        )
        engine.inject(ev("STARTJOB", 0, job="sa"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["sa"].status == "SUCCESS"
        engine._enqueue(
            Event(
                at=T0 + timedelta(minutes=3),
                kind="STATUS",
                payload={"job": "sa", "run_number": 0, "exit_code": 0},
            ),
            source="adapter",  # what makes it a COMPLETION, and so gated
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert len(engine.drops) == 1
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    records = read_journal(tmp_path / "run")
    rejected = [r for r in records if r["rec"] == "decision" and r["decision"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "run_number mismatch"
    assert rejected[0]["revisions"] == {}  # a rejection moved nothing
    attempt = next(r for r in records if r.get("seq") == rejected[0]["index"])
    assert attempt["kind"] == "STATUS"
    assert attempt["payload"] == {"job": "sa", "run_number": 0, "exit_code": 0}
    assert attempt["source"] == "adapter"  # and the log says why it was gated


# ------------------------------------------------------------- 6. start_run gate


def test_start_run_refuses_an_existing_journal(tmp_path: Path) -> None:
    """(start_run docstring): a run_root that already holds an engine.db must be resumed
    (resume_run), never silently re-baselined."""
    catalog = lower_source(_SOLO_JIL)
    run_root = tmp_path / "run"
    engine = start_run(
        catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )
    assert engine.journal is not None
    engine.journal.close()
    with pytest.raises(EngineError, match="already exists"):
        start_run(catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()})


# --------------------------------------------------------- 7. `dsl41 journal` CLI


def test_cli_journal_renders_the_trace_when_the_catalog_matches(tmp_path: Path) -> None:
    """(cli.py journal command docstring): replays the WAL through a fresh
    Oracle and prints "{at} {job} {transition} [{cause}]" per trace entry --
    the same information `dsl41.oracle.Oracle.trace()` carries, rendered."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(text)
    # loaded with file=str(jil_path) so its catalog_hash matches what the CLI
    # computes from the SAME path via parse_file -- SourceSpan.file is part
    # of the hashed model, so a "<memory>" load would mismatch a real one
    catalog = lower_source(text, file=str(jil_path))
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    result = CliRunner().invoke(app, ["journal", str(run_root), str(jil_path)])
    assert result.exit_code == 0
    assert result.output == (
        "2026-07-01T08:00:00 j1 INACTIVE->STARTING [STARTJOB event (control)]\n"
        "2026-07-01T08:00:00 j1 STARTING->RUNNING"
        " [QUE_WAIT collapses to immediate (ss7 non-goal)]\n"
        "2026-07-01T08:00:00 j1 RUNNING->SUCCESS [injected STATUS]\n"
    )


def test_cli_journal_exits_2_on_catalog_hash_mismatch(tmp_path: Path) -> None:
    """(cli.py journal command docstring / runner-design ss7): the estate
    file differs from the one this journal ran -- refuse rather than render
    a trace that silently mixes an old run with new semantics."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(text)
    catalog = lower_source(text, file=str(jil_path))
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    jil_path.write_text(text.replace("command: x", "command: y"))  # the estate drifted
    result = CliRunner().invoke(app, ["journal", str(run_root), str(jil_path)])
    assert result.exit_code == 2
    assert "catalog hash mismatch" in result.output


def test_cli_journal_refuses_another_state_version(tmp_path: Path) -> None:
    """DL-424: a store another build wrote names that build's state version in its meta. Read
    against the SAME estate file, it is refused for its version, before any hash is compared:
    never "catalog hash mismatch", never a trace."""
    import sqlite3

    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(text)
    catalog = lower_source(text, file=str(jil_path))
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    with closing(sqlite3.connect(run_root / "engine.db", isolation_level=None)) as db:
        db.execute(
            "UPDATE meta SET value = ? WHERE key = 'state_version'",
            (str(STATE_MACHINE_VERSION - 1),),
        )
    result = CliRunner().invoke(app, ["journal", str(run_root), str(jil_path)])
    assert result.exit_code == 2
    assert f"state version {STATE_MACHINE_VERSION - 1}" in result.output
    assert "catalog hash mismatch" not in result.output
    assert "STARTING" not in result.output


def test_cli_journal_refuses_a_directory_that_is_not_a_run_root(tmp_path: Path) -> None:
    """A directory with no `engine.db` is not a run root, and a 1.x root is refused by name:
    2.0.0 reads the store only."""
    result = CliRunner().invoke(app, ["journal", str(tmp_path)])
    assert result.exit_code == 2 and "not a run root" in result.output
    (tmp_path / "journal.jsonl").write_text("{}\n")
    result = CliRunner().invoke(app, ["journal", str(tmp_path)])
    assert result.exit_code == 2 and "a 1.x run root" in result.output


# ------------------------------------ the record's own edges (DL-105)


def test_the_preflight_caveats_a_run_started_under_are_in_the_log(tmp_path: Path) -> None:
    """ss8's "prints, RECORDS, and runs" made literal (DL-45). A WARN that only reached the
    terminal would be gone by the time anyone asks why a run behaved the way it did; replay
    ignores the record, because it is not an input -- it is the caveat list the inputs were
    admitted under."""
    from dsl41.runner_preflight import PreflightItem

    journal = _journal(tmp_path)
    items = [
        PreflightItem(severity="WARN", code="machine", job="j", message="pool split; local"),
        PreflightItem(severity="WARN", code="timezone", job="k", message="no base tz"),
    ]
    journal.preflight(items, T0)
    journal.close()

    [record] = read_journal(tmp_path)
    assert record["rec"] == "preflight"
    assert [i["code"] for i in record["items"]] == ["machine", "timezone"]
    assert [i["severity"] for i in record["items"]] == ["WARN", "WARN"]


def test_unsubscribing_something_that_never_subscribed_is_a_no_op() -> None:
    """ss10 viewers come and go, and a double `unsubscribe` -- a viewer that drops while its
    handler is being cancelled -- must not take the journal down with it. The fan-out never
    blocks on a subscriber and never dies of one."""
    journal = Journal(MemoryStore(identity=_IDENTITY))
    queue = journal.subscribe(Executor())
    journal.unsubscribe(queue)
    journal.unsubscribe(queue)  # already gone
    journal.unsubscribe(asyncio.Queue())  # type: ignore[arg-type]  # never here
    journal.drop(Event(at=T0, kind="STARTJOB", payload={"job": "j"}), "for the fan-out")
    assert queue.empty()  # unsubscribed means unsubscribed
    journal.close()


_ORDER_GATE = "insert_job: GATE\njob_type: c\ncommand: x\nmachine: m1\n\n"
_ORDER_JIL = (
    _ORDER_GATE
    + "insert_resource: SHARED\nres_type: R\namount: 1\n\n"
    + "".join(
        f"insert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n"
        "condition: s(GATE)\nresources: (SHARED, QUANTITY=1, FREE=A)\n\n"
        for name in ("LOW", "HIGH")
    )
)


@pytest.mark.parametrize(
    "route", ["live", "restart", "restart_tail", "reload", "reload_restart", "reload_restart_tail"]
)
def test_catalog_order_survives_store_replay_restart_and_reload(tmp_path: Path, route: str) -> None:
    async def scenario() -> None:
        root = tmp_path / "run"
        clock = VirtualClock(start=T0)
        adapter = FakeAdapter(default=None)
        engine = start_run(
            lower_source(_ORDER_GATE if "reload" in route else _ORDER_JIL),
            root,
            clock=clock,
            adapters={"CMD": adapter},
        )
        expected_rev = 1
        try:
            if "reload" in route:
                estate = tmp_path / "estate"
                estate.mkdir()
                (estate / "estate.toml").write_text('name = "order"\nfiles = ["jobs.jil"]\n')
                (estate / "jobs.jil").write_text(_ORDER_JIL)
                candidate = load_estate(estate)
                plan = reread(
                    estate, engine.update.live(), root / "plans", candidate=candidate, start=T0
                )
                pending = engine.request_update(
                    UpdateRequest(plan.token, False, 1, engine.baseline_id, "order-update")
                )
                await engine.run_until_quiescent(T0)
                answer = pending.result()
                assert answer.applied is not None
                assert answer.applied.result()["rev"] == 2
                expected_rev = 2
            if "restart" in route:
                if not route.endswith("_tail"):
                    await engine.shutdown()
                assert engine.journal is not None
                engine.journal.close()
                stored = active_configuration(root)
                engine = await resume_run(
                    stored.catalog,
                    root,
                    clock=clock,
                    adapters={"CMD": adapter},
                    stored_sources=True,
                )
                await engine.run_until_quiescent(T0)
            assert engine.config_rev == expected_rev
            engine.inject(ev("STARTJOB", job="GATE"))
            engine.inject(ev("STATUS", 1, job="GATE", status="SUCCESS"))
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
            assert {name: row.status for name, row in engine.oracle.store.job.items()} == {
                "GATE": "SUCCESS",
                "LOW": "RUNNING",
                "HIGH": "QUE_WAIT",
            }
            engine.inject(ev("STATUS", 2, job="LOW", status="SUCCESS"))
            engine.inject(ev("STATUS", 3, job="HIGH", status="SUCCESS"))
            await engine.run_until_quiescent(T0 + timedelta(minutes=3))
            assert all(row.status == "SUCCESS" for row in engine.oracle.store.job.values())
            assert [t.job for t in engine.oracle.trace() if t.transition.endswith("STARTING")] == [
                "GATE",
                "LOW",
                "HIGH",
            ]
            assert engine.journal is not None
            stored = Configuration.from_bytes(
                engine.journal.store.get_config(expected_rev), where="order regression"
            )
            replayed = replay_store(engine.journal.store, where="order regression")
            assert list(stored.catalog.jobs) == ["GATE", "LOW", "HIGH"]
            assert replayed.replay.rev == expected_rev
            assert replayed.oracle.store.job == engine.oracle.store.job
            assert [t.model_dump() for t in replayed.oracle.trace() if t.job] == [
                t.model_dump() for t in engine.oracle.trace() if t.job
            ]
            assert {name: row.state_rev for name, row in replayed.oracle.store.job.items()} == {
                "GATE": 2,
                "LOW": 2,
                "HIGH": 3,
            }
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
