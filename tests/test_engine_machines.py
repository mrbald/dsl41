"""The engine tier's machines (DL-289): host routing and admission; and the
effect outbox and the subscription feed through their tiers. The seal
boundary, the effect, host routing and admission are statecharts now
(DL-327, DL-331, DL-333, DL-334); their chart tests are
test_seal_boundary_chart.py, test_effect_chart.py, test_host_chart.py and
test_admission_chart.py.

Each machine is registered and well formed, and each new rule has a test
that triggers it and one that does not: the routing chart's rows and the
gate's rejections, a decided host command the chart refuses, and a second
outcome refused by the outbox. The anchor's machines are statecharts too
(DL-336): test_anchor_machines.py. A test that builds a chart's cells by hand
records into its own directory, so its hits stay out of the session's gate."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import HOST, Event
from dsl41.period import estate_wal
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ADMISSION,
    ApplyResult,
    Attempt,
    DecisionIndex,
    apply_attempt,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_effects import Effect, EffectOutcome, Outbox, effect_id_for
from dsl41.runner_hosts import (
    LOCAL_EXECUTOR_ID,
    HostCommand,
    apply_host_command,
    host_move,
    kill_allowance,
    seed_local_executor,
)
from dsl41.runner_journal import (
    Journal,
    ReplayFault,
    read_journal,
    read_outbox,
    replay_period,
)
from dsl41.runner_startup import resume_run, start_run
from dsl41.state_machine import (
    HITS_ENV,
    Executor,
    Outcome,
    Row,
    StatechartError,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
ENGINE_MACHINES = (HOST, ADMISSION)


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, not strict, so a deliberate violation
    stays out of the session and production's handling runs."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


def test_the_engine_machines_are_registered_and_well_formed() -> None:
    for machine in ENGINE_MACHINES:
        assert machine in MACHINES
        assert well_formed(machine) == []
    marked = [(row.id, row.mark) for row in ADMISSION.rows if row.mark]
    marked += [(row.id, row.mark) for row in HOST.rows if row.mark]
    # the relay's re-registration is a contract rule with no code yet
    assert marked == [("host.12", "spec-only")]


# ------------------------------------------------------------ host routing


def _oracle(deadman_s: float | None = 60.0) -> Oracle:
    oracle = Oracle(lower_source(_JIL))
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=T0, deadman_s=deadman_s)
    return oracle


def _host(oracle: Oracle, index: int, verb: str, *, at: datetime = T0, **fields: Any) -> Any:
    """Apply one host command through steps 5-7, as the engine and replay do."""
    attempt = Attempt(
        index=index,
        at=at,
        request_id=f"r{index}",
        fingerprint="fp",
        host=HostCommand(
            verb=verb,  # type: ignore[arg-type]
            host_id=LOCAL_EXECUTOR_ID,
            force=fields.pop("force", False),
        ),
        **fields,
    )
    return apply_attempt(oracle, attempt, grace_s=10.0)


#: (verbs that put the row in its state, the verb, force, transition, state after)
_MOVES = [
    ((), "activate", False, "host.01", "active"),
    ((), "drain", False, "host.02", "passive"),
    (("drain",), "activate", False, "host.03", "active"),
    (("drain",), "drain", False, "host.04", "passive"),
    ((), "quarantine", False, "host.05", "quarantined"),
    (("drain",), "quarantine", False, "host.05", "quarantined"),
    (("quarantine",), "quarantine", False, "host.06", "quarantined"),
    (("drain", "quarantine"), "reinstate", False, "host.07", "passive"),
    (("quarantine",), "reinstate", False, "host.07", "active"),
    ((), "reinstate", False, "host.08", "active"),
    (("drain",), "reinstate", False, "host.09", "passive"),
    ((), "evict", True, "host.11", "evicted"),
    (("drain",), "evict", True, "host.11", "evicted"),
    (("quarantine",), "evict", True, "host.11", "evicted"),
]


@pytest.mark.parametrize(("setup", "verb", "force", "taken", "after"), _MOVES)
def test_each_routing_verb_takes_its_declared_transition(
    setup: tuple[str, ...], verb: str, force: bool, taken: str, after: str
) -> None:
    oracle = _oracle()
    for index, step in enumerate(setup, start=1):
        assert _host(oracle, index, step).result.decision == "applied"
    row = oracle.store.host(LOCAL_EXECUTOR_ID)
    assert row is not None
    cmd = HostCommand(verb=verb, host_id=LOCAL_EXECUTOR_ID, force=force)  # type: ignore[arg-type]
    assert host_move(oracle.store, cmd).rows == (taken,)
    applied = _host(oracle, len(setup) + 1, verb, force=force, claimed_actor="alice@ops")
    assert applied.result.decision == "applied"
    assert oracle.store.hosts[LOCAL_EXECUTOR_ID].state == after
    # an internal transition moves no revision; every other one moves it once
    moved = "host:local" in applied.result.revisions
    assert moved is (after != row.state)


def test_a_gated_eviction_takes_host_10_once_the_bound_has_passed() -> None:
    oracle = _oracle(deadman_s=60.0)
    _host(oracle, 1, "quarantine")
    bound = 60.0 + kill_allowance(10.0) + 1.0  # the skew floor wins at this interval
    early = _host(oracle, 2, "evict", at=T0 + timedelta(seconds=bound))
    assert early.result.code == "eviction_bound_pending"
    assert oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "quarantined"
    row = oracle.store.host(LOCAL_EXECUTOR_ID)
    assert row is not None
    late = T0 + timedelta(seconds=bound + 1)
    move = host_move(oracle.store, HostCommand(verb="evict", host_id=LOCAL_EXECUTOR_ID))
    assert move.rows == ("host.10",)  # the chart's row; the bound is the gate's
    assert _host(oracle, 3, "evict", at=late).result.decision == "applied"
    evicted = oracle.store.hosts[LOCAL_EXECUTOR_ID]
    assert (evicted.state, evicted.generation, evicted.forced_by) == ("evicted", 1, None)


@pytest.mark.parametrize(
    ("setup", "verb", "force", "code", "read"),
    [
        (("quarantine",), "activate", False, "host_quarantined", "host.13"),
        (("quarantine",), "drain", False, "host_quarantined", "host.14"),
        ((), "evict", False, "host_not_quarantined", "host.15"),
        ((), "evict", True, "force_needs_actor", "host.11"),
        (("evict",), "evict", False, "host_already_evicted", "host.20"),
        (("evict",), "activate", False, "host_evicted", "host.16"),
        (("evict",), "reinstate", False, "host_evicted", "host.19"),
        (("evict",), "quarantine", False, "host_evicted", "host.18"),
        (("evict",), "evict", True, "host_already_evicted", "host.21"),
    ],
)
def test_a_rejected_verb_takes_no_transition_and_moves_nothing(
    setup: tuple[str, ...],
    verb: str,
    force: bool,
    code: str,
    read: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate reads the chart on a scratch executor: a refusal row, or the
    row whose facts the gate then rejects. The store's instance does not
    move, and the row does not either."""
    oracle = _oracle()
    for index, step in enumerate(setup, start=1):
        forced = step == "evict"
        _host(oracle, index, step, force=forced, claimed_actor="alice@ops" if forced else None)
    before = oracle.store.hosts[LOCAL_EXECUTOR_ID]
    configuration = oracle.store.machines.active("host:local")
    # only the rejected command records here: the one row the gate read
    directory = tmp_path / "rejected"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    rejected = _host(oracle, len(setup) + 1, verb, force=force)
    assert rejected.result.decision == "rejected" and rejected.result.code == code
    assert oracle.store.hosts[LOCAL_EXECUTOR_ID] == before
    assert oracle.store.machines.active("host:local") == configuration
    recorded = [
        json.loads(line)
        for path in directory.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [r["id"] for r in recorded if r["machine"] == "host" and r["kind"] == "row"] == [read]


@pytest.mark.parametrize(
    ("setup", "cmd", "why", "facts"),
    [
        (
            (),
            HostCommand(verb="drain", host_id="nowhere"),
            r"drain on 'nowhere' is decided applied, but no host instance holds it",
            [("machine", "host"), ("instance", "'host:nowhere'"), ("active", "no instance")],
        ),
        (
            ("quarantine",),
            HostCommand(verb="activate", host_id="local"),
            r"activate on 'local' is decided applied, but the chart refuses it \(host_quarantined\)",
            [
                ("machine", "host"),
                ("instance", "'host:local'"),
                ("active", "host, quarantined"),
                ("item", "the event activate"),
                ("payload", "None"),
                ("ancestry", "none: a root item"),
                ("deferred", "empty"),
                ("candidates", "host.13"),
            ],
        ),
    ],
)
def test_a_decided_host_command_with_no_transition_stops_the_apply(
    setup: tuple[str, ...], cmd: HostCommand, why: str, facts: list[tuple[str, str]]
) -> None:
    """A durable `applied` the chart refuses here, or one for a host with no
    instance, was not decided by this chart. It raises a `StatechartError`
    with the executor's structured report, and neither the row nor the
    instance moves; on replay the report also names the input
    (statechart-engine ss5)."""
    oracle = _oracle()
    for index, step in enumerate(setup, start=1):
        _host(oracle, index, step)
    before = dict(oracle.store.hosts)
    configuration = oracle.store.machines.active("host:local")
    oracle.store.begin_input()
    try:
        with pytest.raises(StatechartError, match=why) as raised:
            apply_host_command(oracle.store, cmd, actor=None)
    finally:
        oracle.store.commit_input()
    assert dict(oracle.store.hosts) == before
    assert oracle.store.machines.active("host:local") == configuration
    assert raised.value.facts[: len(facts)] == facts
    if setup:  # the executor's recent microsteps end with the refusal
        lines = str(raised.value).splitlines()
        assert lines[-1] == "microstep: 'host:local' activate fired rows host.13 depth 0"


# ------------------------------------------------------------- the outbox


def _effect(kind: str = "SPAWN") -> Effect:
    return Effect(
        effect_id=effect_id_for(1, kind, "j", 1),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        job="j",
        run_number=1,
        executor_id=LOCAL_EXECUTOR_ID,
        index=1,
        at=T0,
        run_id="00000000-0000-4000-8000-000000000001",
        generation=0,
    )


@pytest.mark.parametrize("state", ["applied", "retired", "indeterminate"])
def test_a_pending_effect_takes_its_one_outcome(state: str) -> None:
    outbox = Outbox()
    effect = _effect()
    outbox.record(effect)
    assert outbox.state_of(effect.effect_id) == "pending"
    outbox.record(effect)  # an exact replay of the record moves nothing
    assert outbox.state_of(effect.effect_id) == "pending"
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state=state))  # type: ignore[arg-type]
    assert outbox.state_of(effect.effect_id) == state
    assert outbox.pending() == []


@pytest.mark.parametrize(("first", "second"), [("applied", "applied"), ("retired", "applied")])
def test_a_second_outcome_is_refused_and_the_first_stands(
    recording: Path, first: str, second: str
) -> None:
    """ss5: an outcome is final. The outbox refuses a second one before
    anything is written or posted, the same outcome again included (DL-295)."""
    outbox = Outbox()
    effect = _effect()
    outbox.record(effect)
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state=first))  # type: ignore[arg-type]
    with pytest.raises(
        EngineError, match=rf"the effect is already {first}, and an outcome is final"
    ):
        outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state=second, detail="late"))  # type: ignore[arg-type]
    assert outbox.state_of(effect.effect_id) == first
    assert outbox.result_for(effect.effect_id) == EffectOutcome(
        effect_id=effect.effect_id, state=first
    )  # type: ignore[arg-type]


def test_a_second_outcome_s_refusal_is_the_outbox_s_own_error(recording: Path) -> None:
    """The refusal is the outbox's own EngineError, which names the row an
    outcome would need: the effect's instance is gone once it is final, so no
    chart is asked (DL-295)."""
    outbox = Outbox()
    effect = _effect()
    outbox.record(effect)
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied"))
    with pytest.raises(
        EngineError,
        match=r"the effect is already applied, and an outcome is final \(effect\.02: applied is"
        r" not a source of effect\.02\) \(concurrency-model ss5\)$",
    ):
        outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied"))


def test_a_log_with_two_outcomes_for_one_effect_does_not_replay(
    recording: Path, tmp_path: Path
) -> None:
    """The refusal runs on replay too: a WAL that resolves one effect twice
    is refused rather than read with the later outcome winning."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )
        engine.inject(_start())
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    wal = estate_wal(run_root)
    records = read_journal(wal)
    results = [r for r in records if r.get("rec") == "effect_result"]
    assert results, "the run resolved its spawn"
    read_outbox(records)  # the log as written replays
    with wal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(results[0], sort_keys=True) + "\n")
    effect_id = results[0]["effect_id"]
    with pytest.raises(ReplayFault, match=rf"outcome of effect {effect_id}.*an outcome is final"):
        read_outbox(read_journal(wal))

    async def resume() -> None:
        await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )

    # resume stops through the replay-fault wrapper, naming the effect
    with pytest.raises(
        EngineError, match=rf"resume stopped: replay stopped at the .*{effect_id}"
    ) as stopped:
        asyncio.run(resume())
    assert "Deploy a build that fixes the fault, or roll back" in str(stopped.value)


def _refusing_quarantine() -> object:
    """A later build's chart that answers a quarantine on a routed host with
    a refusal row instead of host.05."""
    refusal = Row("host.05", "routed", "quarantine", kind="refusal", code="host_quarantined")
    rows = tuple(refusal if row.id == "host.05" else row for row in HOST.rows)
    return replace(HOST, rows=rows)


def test_a_replayed_host_decision_the_chart_refuses_stops_resume_naming_the_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable `applied` host decision this build's chart refuses raises a
    `StatechartError` in the apply; replay adds the WAL input to its report,
    and resume stops with it (DL-333; it was a ReplayFault before)."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )
        engine.inject_host(HostCommand(verb="quarantine", host_id=LOCAL_EXECUTOR_ID))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "quarantined"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setattr("dsl41.oracle_state.HOST", _refusing_quarantine())
    oracle = Oracle(lower_source(_JIL))
    with pytest.raises(
        StatechartError, match=r"the chart refuses it \(host_quarantined\)"
    ) as raised:
        replay_period(oracle, read_journal(estate_wal(run_root)))
    assert not isinstance(raised.value, ReplayFault)
    lines = str(raised.value).splitlines()
    assert "WAL input: 1: host quarantine of local from reconcile" in lines
    assert oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "active"

    async def resume() -> None:
        await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )

    with pytest.raises(StatechartError, match="the chart refuses it"):
        asyncio.run(resume())


def _start() -> Event:
    return Event(at=T0, kind="STARTJOB", payload={"job": "j"})


# ---------------------------------------------------------------- admission


def _attempt(request_id: str, fingerprint: str = "fp", index: int = 1) -> Attempt:
    return Attempt(index=index, at=T0, request_id=request_id, fingerprint=fingerprint)


def test_a_request_id_moves_unseen_admitted_decided() -> None:
    index = DecisionIndex()
    assert index.state("r1") is None  # unseen: no instance, no decision
    index.note(_attempt("r1"))
    assert index.state("r1") == "admitted"
    index.record(
        ApplyResult(
            index=1, request_id="r1", decision="rejected", reason="no", code="precondition_failed"
        )
    )
    assert index.state("r1") == "rejected"
    assert index.lookup("r1", "fp") is not None  # the stored decision answers


# ------------------------------------------------------- the subscription


def test_a_feed_moves_backfill_live_closed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    try:
        feed = journal.subscribe(Executor())
        assert feed.state == "backfill"
        feed.go_live()
        assert feed.state == "live"
        journal.unsubscribe(feed)
        assert feed.state == "closed"
        journal.unsubscribe(feed)  # a feed already gone leaves nothing to close
        assert feed.state == "closed"
    finally:
        journal.close()


def test_an_overflowed_feed_is_removed_and_its_unsubscribe_takes_nothing(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    try:
        reasons: list[str] = []
        machines = Executor()
        feed = journal.subscribe(machines, reasons.append, budget=1)
        record = {"rec": "preflight", "items": [{"message": "x" * 100}]}
        journal._write(record)  # an empty backlog takes one record
        assert feed.state == "backfill"
        journal._write(record)  # the second does not fit: the journal drops the feed
        assert feed.state == "removed" and feed.overflow is not None
        with pytest.raises(StatechartError, match="no instance"):
            machines.active(feed)  # forgotten once it rests in `removed`
        posts: list[str] = []
        dispatch = machines.dispatch

        def spy(key: Any, event: ChartEvent) -> list[Outcome]:
            posts.append(event.name)
            return dispatch(key, event)

        machines.dispatch = spy  # type: ignore[method-assign]
        journal.unsubscribe(feed)
        assert feed.state == "removed" and posts == []  # nothing posted
    finally:
        journal.close()
