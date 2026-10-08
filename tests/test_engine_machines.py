"""The engine tier's state machines (DL-289): host routing, admission, the
effect outbox, the subscription feed and the seal boundary.

Each machine is registered and well formed, and each new rule has a test
that triggers it and one that does not: the routing table's transitions and
rejections, a host's violation as a `host:` trace line, a second outcome
refused by the outbox, and a violation outside an oracle batch reported on
stderr. A test that breaks a transition on purpose records into its own
directory without the strict variable, so the violation stays out of the
session's gate."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import VIOLATION_MARKER, Event
from dsl41.period import estate_wal
from dsl41.runner import SEAL_BOUNDARY, Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ADMISSION,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    apply_attempt,
    report_violation,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_effects import EFFECT, Effect, EffectOutcome, Outbox, effect_id_for
from dsl41.runner_hosts import (
    HOST,
    LOCAL_EXECUTOR_ID,
    HostCommand,
    HostGate,
    apply_host_command,
    host_move,
    kill_allowance,
    seed_local_executor,
)
from dsl41.runner_codes import Rejection
from dsl41.runner_journal import (
    SUBSCRIPTION,
    Journal,
    ReplayFault,
    read_journal,
    read_outbox,
    replay_period,
)
from dsl41.runner_startup import resume_run, start_run
from dsl41.state_machine import (
    HITS_ENV,
    STRICT_ENV,
    VIOLATION_LOG_PREFIX,
    StateMachine,
    Transition,
    TransitionError,
    Violation,
    well_formed,
)

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
ENGINE_MACHINES = (HOST, ADMISSION, EFFECT, SUBSCRIPTION, SEAL_BOUNDARY)


@pytest.fixture
def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, not strict, so a deliberate violation
    stays out of the session and production's handling runs."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.delenv(STRICT_ENV, raising=False)
    return directory


def test_the_engine_machines_are_registered_and_well_formed() -> None:
    for machine in ENGINE_MACHINES:
        assert machine in MACHINES
        assert well_formed(machine) == []
    marked = [(t.id, t.mark) for m in ENGINE_MACHINES for t in m.transitions if t.mark]
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
    move = host_move(row, cmd, HostGate(at=T0, grace_s=10.0, actor="alice@ops"))
    assert isinstance(move, Transition) and move.id == taken
    applied = _host(oracle, len(setup) + 1, verb, force=force, claimed_actor="alice@ops")
    assert applied.result.decision == "applied" and applied.violations == []
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
    move = host_move(
        row, HostCommand(verb="evict", host_id=LOCAL_EXECUTOR_ID), HostGate(late, 10.0, None)
    )
    assert isinstance(move, Transition) and move.id == "host.10"
    assert _host(oracle, 3, "evict", at=late).result.decision == "applied"
    evicted = oracle.store.hosts[LOCAL_EXECUTOR_ID]
    assert (evicted.state, evicted.generation, evicted.forced_by) == ("evicted", 1, None)


@pytest.mark.parametrize(
    ("setup", "verb", "force", "code"),
    [
        (("quarantine",), "activate", False, "host_quarantined"),
        (("quarantine",), "drain", False, "host_quarantined"),
        ((), "evict", False, "host_not_quarantined"),
        ((), "evict", True, "force_needs_actor"),
        (("evict",), "evict", False, "host_already_evicted"),
        (("evict",), "activate", False, "host_evicted"),
        (("evict",), "reinstate", False, "host_evicted"),
        (("evict",), "quarantine", False, "host_evicted"),
        (("evict",), "evict", True, "host_already_evicted"),
    ],
)
def test_a_rejected_verb_takes_no_transition_and_moves_nothing(
    setup: tuple[str, ...],
    verb: str,
    force: bool,
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle()
    for index, step in enumerate(setup, start=1):
        forced = step == "evict"
        _host(oracle, index, step, force=forced, claimed_actor="alice@ops" if forced else None)
    before = oracle.store.hosts[LOCAL_EXECUTOR_ID]
    # only the rejected command records here: any hit or violation is a take
    directory = tmp_path / "rejected"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    rejected = _host(oracle, len(setup) + 1, verb, force=force)
    assert rejected.result.decision == "rejected" and rejected.result.code == code
    assert oracle.store.hosts[LOCAL_EXECUTOR_ID] == before
    assert rejected.violations == []
    recorded = [
        json.loads(line)
        for path in directory.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [r for r in recorded if r["machine"] == "host"] == []


@pytest.mark.parametrize(
    ("setup", "cmd", "why"),
    [
        ((), HostCommand(verb="drain", host_id="nowhere"), "unknown_host"),
        (("quarantine",), HostCommand(verb="activate", host_id="local"), "host_quarantined"),
    ],
)
def test_a_decided_host_command_with_no_transition_stops_the_apply(
    setup: tuple[str, ...], cmd: HostCommand, why: str
) -> None:
    """A durable `applied` the table cannot take here was not decided by
    this state machine. It raises before anything moves; on replay that is
    a fault naming the input (period-model ss11)."""
    oracle = _oracle()
    for index, step in enumerate(setup, start=1):
        _host(oracle, index, step)
    before = dict(oracle.store.hosts)
    oracle.store.begin_input()
    try:
        with pytest.raises(EngineError, match=rf"no transition for it \({why}\)"):
            apply_host_command(oracle.store, cmd, actor=None)
    finally:
        oracle.store.commit_input()
    assert dict(oracle.store.hosts) == before


def _without(machine: StateMachine[Any], dropped: str) -> StateMachine[Any]:
    """The same machine with one transition undeclared, so taking it is a
    violation."""
    return StateMachine(
        name=machine.name,
        states=machine.states,
        initial=machine.initial,
        finals=machine.finals,
        transitions=tuple(t for t in machine.transitions if t.id != dropped),
    )


def test_a_host_violation_is_a_trace_line_under_the_host_key(
    recording: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The apply notes the check's result on the batch's channel (DL-292),
    so a broken routing transition is a `TRANSITION_VIOLATION` trace line
    whose subject is `host:local`, and the `trace` verb serves it as it is."""
    monkeypatch.setattr("dsl41.runner_hosts.HOST", _without(HOST, "host.02"))
    oracle = _oracle()
    clean = _host(oracle, 1, "quarantine")
    assert clean.violations == []
    _host(oracle, 2, "reinstate")
    drained = _host(oracle, 3, "drain")
    assert drained.result.decision == "applied"  # an engine never refuses a made decision
    assert [(subject, v.transition) for subject, v in drained.violations] == [
        ("host:local", "host.02")
    ]
    markers = [t for t in oracle.trace() if t.transition == VIOLATION_MARKER]
    assert [(t.job, t.cause.split(":")[0]) for t in markers] == [
        ("host:local", "host.02 active->passive")
    ]

    engine = Engine(
        lower_source(_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )
    engine.oracle = oracle
    served = ControlServer(engine, tmp_path / "control.sock")._trace({"since": 0})
    entries = [e for e in served["entries"] if e["transition"] == VIOLATION_MARKER]
    assert [(e["job"], e["cause"]) for e in entries] == [(markers[0].job, markers[0].cause)]


def test_an_operator_host_verb_that_breaks_the_table_is_refused_and_a_leader_one_is_traced(
    recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DL-292's policy on the routing table: the operator's `drain` is a
    control input, so its dry apply refuses it before the WAL; the leader's
    `quarantine` is the engine's own observation, so it is taken and traced."""
    monkeypatch.setattr("dsl41.runner_hosts.HOST", _without(_without(HOST, "host.02"), "host.05"))

    async def scenario() -> Engine:
        engine = Engine(
            lower_source(_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        revision = engine.oracle.store.revision("host:local")
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id=LOCAL_EXECUTOR_ID),
            Envelope(request_id="r-drain", expect={"host:local": revision}, epoch=engine.epoch),
        )
        engine.inject_host(HostCommand(verb="quarantine", host_id=LOCAL_EXECUTOR_ID))
        await engine.run_until_quiescent(T0)
        with pytest.raises(EngineError) as refused:
            await drained
        assert getattr(refused.value, "code", None) == "transition_violation"
        assert "host.02 on host:local" in str(refused.value)
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "quarantined"
    markers = [t.job for t in engine.oracle.trace() if t.transition == VIOLATION_MARKER]
    assert markers == ["host:local"]


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
    """ss5: an outcome is final. The effect machine's check refuses a second
    one before anything is written, the same outcome again included."""
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


def test_a_strict_session_raises_on_a_second_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    monkeypatch.setenv(STRICT_ENV, "1")
    outbox = Outbox()
    effect = _effect()
    outbox.record(effect)
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied"))
    with pytest.raises(TransitionError, match=r"effect\.02"):
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


def test_a_replayed_host_decision_with_no_transition_stops_resume_naming_the_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable `applied` host decision this build's table cannot take
    raises in the apply; replay wraps it as a ReplayFault naming the input,
    and resume stops with it."""
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
    # a later build whose table answers a quarantine on an active row with a rejection
    monkeypatch.setattr(
        "dsl41.runner_hosts._routed",
        lambda row, cmd, gate: Rejection("host_quarantined", "a changed table"),
    )
    oracle = Oracle(lower_source(_JIL))
    with pytest.raises(ReplayFault, match=r"replay stopped at input 1 \(host quarantine") as fault:
        replay_period(oracle, read_journal(estate_wal(run_root)))
    assert fault.value.index == 1
    assert "no transition for it (host_quarantined)" in str(fault.value)

    async def resume() -> None:
        await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )

    with pytest.raises(EngineError, match=r"resume stopped: replay stopped at input 1"):
        asyncio.run(resume())


def _start() -> Event:
    return Event(at=T0, kind="STARTJOB", payload={"job": "j"})


# ---------------------------------------------------------------- admission


def _attempt(request_id: str, fingerprint: str = "fp", index: int = 1) -> Attempt:
    return Attempt(index=index, at=T0, request_id=request_id, fingerprint=fingerprint)


def test_a_request_id_moves_unseen_admitted_decided() -> None:
    index = DecisionIndex()
    assert index.state("r1") == "unseen" and index.state(None) == "unseen"
    index.refuse("r1")  # refused after dedup: it stays unseen
    assert index.state("r1") == "unseen"
    index.note(_attempt("r1"))
    assert index.state("r1") == "admitted"
    index.record(
        ApplyResult(
            index=1, request_id="r1", decision="rejected", reason="no", code="precondition_failed"
        )
    )
    assert index.state("r1") == "rejected"
    assert index.lookup("r1", "fp") is not None  # the stored decision answers


def test_an_admission_violation_is_one_stderr_line_and_the_move_proceeds(
    recording: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Admission's moves are not oracle inputs, so no trace line can carry a
    violation: it is reported on stderr, and the index still records."""
    index = DecisionIndex()
    index.record(ApplyResult(index=1, request_id="r1", decision="applied"))  # never admitted
    assert index.state("r1") == "applied"
    err = capsys.readouterr().err
    assert err == (
        "dsl41: transition violation: admission admission.02 unseen->applied:"
        " unseen is not a source of admission.02\n"
    )
    index.note(_attempt("r2"))  # a declared move reports nothing
    report_violation(None)
    assert capsys.readouterr().err == ""
    violations = (recording / f"violations-{os.getpid()}.jsonl").read_text()
    assert "admission.02" in violations


class _BrokenStderr:
    def write(self, _text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_violation_report_with_a_broken_stderr_is_dropped_and_the_move_proceeds(
    recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`report_violation` never raises: `DecisionIndex.record` runs between an
    apply and its decision line, where a raise would stop the engine."""
    import sys

    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    index = DecisionIndex()
    index.record(ApplyResult(index=1, request_id="r1", decision="applied"))  # never admitted
    assert index.state("r1") == "applied"
    assert "admission.02" in (recording / f"violations-{os.getpid()}.jsonl").read_text()


def test_an_engine_violation_line_carries_the_journal_alert_prefix(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runbook alerts on one journal prefix, `VIOLATION_LOG_PREFIX`."""
    report_violation(Violation("admission", "admission.99", "a", "b", "fake"))
    (line,) = capsys.readouterr().err.splitlines()
    assert VIOLATION_LOG_PREFIX == "dsl41: transition violation"
    assert line == f"{VIOLATION_LOG_PREFIX}: admission admission.99 a->b: fake"


# ------------------------------------------------------- the subscription


def test_a_feed_moves_backfill_live_closed(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "j.jsonl", fsync_each=False)
    try:
        feed = journal.subscribe()
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
        feed = journal.subscribe(reasons.append, budget=1)
        record = {"rec": "preflight", "items": [{"message": "x" * 100}]}
        journal._write(record)  # an empty backlog takes one record
        assert feed.state == "backfill"
        journal._write(record)  # the second does not fit: the journal drops the feed
        assert feed.state == "removed" and feed.overflow is not None
        journal.unsubscribe(feed)
        assert feed.state == "removed"
    finally:
        journal.close()
