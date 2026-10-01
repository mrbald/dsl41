"""The effect outbox: intent before the attempt, and what came of it (S5c).

Normative spec: docs/concurrency-model.md ss5 (effects), ss4 step 7 (the
outbox commits with the decision), ss1 (it lives IN the ledger). `ssN` here
always names concurrency-model.

ss5 exists for one sentence in ss0 -- no `(job, run_number)` ever executes
twice -- and the obligations that land here are CM-09's application half and
CM-06's `outcome_unavailable`. Four properties carry them, and each is
tested with the contrast that makes it non-vacuous:

  * **Intent is recorded before the attempt.** The point is not the record,
    it is what the record makes possible: a kill the engine decided and did
    not deliver used to be a `task.cancel()` with no id, so an engine that
    died in between left a DETACHED run orphaned -- reconciliation skips it,
    because its job is already TERMINAL. The test that matters is the one
    where that run is stopped after a restart.
  * **Supersession is by exact desired state**, not by generation. ss5 is
    explicit that the obvious guard never fires for the case that motivates
    it, because KILLJOB does not advance `run_number`: a delayed SPAWN for
    run N is still "current" after run N has been TERMINATED.
  * **Three states, not two.** An effect that was attempted and cannot be
    reported on is a fact. Collapsing it into a failure would report a
    signal that DID land as one that did not.
  * **At-most-once application.** An applied effect is never applied again,
    and a pending one is never applied twice by two drains.

The outbox is built ON the spool (DL-93), so the tests read the spool where
the spool is the record -- a run directory means the spawn reached the host,
and `status.json` says how the run ended. Nothing here re-invents either.
"""

from __future__ import annotations

import asyncio
import json

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pydantic import ValidationError

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobRuntime
from dsl41.period import wal_path
from dsl41.runner import Engine
from dsl41.runner_clock import EngineError
from dsl41.runner_startup import start_run
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_effects import (
    OUTCOME_UNAVAILABLE,
    Effect,
    EffectOutcome,
    Outbox,
    effect_id_for,
    plan_effects,
    superseded_reason,
)
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import read_journal, read_outbox, replay_inputs
from model_harness import _lose_the_decision_for

T0 = datetime(2026, 7, 1, 8, 0)

_SOLO_JIL = "insert_job: j\njob_type: c\ncommand: x\n"


def _ev(kind: str, minutes: float, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)  # type: ignore[arg-type]


def _engine(jil: str = _SOLO_JIL, adapter: FakeAdapter | None = None) -> Engine:
    return Engine(
        lower_source(jil),
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter(default=None)},
    )


def _effect(
    kind: str = "SPAWN",
    *,
    job: str = "j",
    run_number: int = 1,
    index: int = 1,
    run_id: str | None = None,
    generation: int | None = None,
) -> Effect:
    return Effect(
        effect_id=effect_id_for(index, kind, job, run_number),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        job=job,
        run_number=run_number,
        executor_id="local",
        index=index,
        at=T0,
        run_id=run_id,
        generation=generation,
    )


# ------------------------------------------------------------- 1. the outbox


def test_an_effect_is_pending_until_something_says_otherwise() -> None:
    """The three states, and the fourth thing that is not a state: an effect
    this outbox never saw reads None, not `pending`. Confusing "nobody
    intended this" with "intended and not yet done" is how a dropped record
    becomes an invisible no-op."""
    outbox = Outbox()
    assert outbox.state_of("nothing") is None

    effect = _effect()
    outbox.record(effect)
    assert outbox.state_of(effect.effect_id) == "pending"
    assert outbox.pending() == [effect]
    assert outbox.result_for(effect.effect_id) is None  # not attempted

    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied", run_id="r1"))
    assert outbox.state_of(effect.effect_id) == "applied"
    assert outbox.pending() == []


def test_cm06_an_effect_that_cannot_be_reported_on_answers_outcome_unavailable() -> None:
    """ss5's third state, and the reason it exists: persist the intent, act,
    crash before persisting the result, and nothing can know whether the
    signal landed. Reporting that as a failure would say a kill did not land
    when it may have -- which is exactly the assumption that lets a second
    process start."""
    outbox = Outbox()
    effect = _effect("KILL")
    outbox.record(effect)
    outbox.resolve(
        EffectOutcome(effect_id=effect.effect_id, state="indeterminate", detail="nobody saw it")
    )
    assert outbox.state_of(effect.effect_id) == "indeterminate"
    assert outbox.result_for(effect.effect_id) == OUTCOME_UNAVAILABLE
    assert outbox.pending() == []  # attempted: never re-driven blindly


def test_the_outbox_holds_one_run_one_identity_both_directions() -> None:
    """period-model ss11a: one `(job, run_number)` maps to one `run_id` and
    one `run_id` to one run. The planner keeps this by construction, so a
    record that breaks it is corruption or a foreign writer -- and acting on
    either half would act on the wrong process."""
    rid_a = "00000000-0000-4000-8000-00000000000a"
    rid_b = "00000000-0000-4000-8000-00000000000b"
    outbox = Outbox()
    outbox.record(_effect("SPAWN", index=1, run_id=rid_a, generation=0))
    # the same run under a second id
    with pytest.raises(EngineError, match="one run, one identity"):
        outbox.record(_effect("KILL", index=2, run_id=rid_b, generation=0))
    # the same id claimed by a second run
    with pytest.raises(EngineError, match="one identity, one run"):
        outbox.record(_effect("SPAWN", index=3, run_number=2, run_id=rid_a, generation=0))
    # the KILL that carries ITS run's id is the agreeing case
    outbox.record(_effect("KILL", index=2, run_id=rid_a, generation=0))
    # and identity-less records (pre-DL-118) claim nothing
    outbox.record(_effect("KILL", index=4, run_number=3))
    # but a NEW identity-less intent for a run ALREADY identified is not
    # planned by this code: the planner looks a KILL's id up from the SPAWN
    # that bound it, so this can only be corruption or a foreign writer
    with pytest.raises(EngineError, match="identity-less intent for an identified run"):
        outbox.record(_effect("KILL", index=5, run_id=None))


def test_a_reused_effect_id_with_different_content_refuses() -> None:
    """`effect_id` is derived, so two different effects under one id mean the
    log disagrees with itself. Overwriting would let the later record
    silently replace the intent the earlier one recorded -- a pending SPAWN
    flipped into a KILL loses the start. Exact replay stays a no-op."""
    outbox = Outbox()
    spawn = _effect("SPAWN", generation=0)
    outbox.record(spawn)
    outbox.record(spawn)  # replay meets the live record: no-op
    assert [e.effect_id for e in outbox.pending()] == [spawn.effect_id]
    forged = spawn.model_copy(update={"kind": "KILL"})
    with pytest.raises(EngineError, match="disagrees with itself"):
        outbox.record(forged)
    assert [e.kind for e in outbox.pending()] == ["SPAWN"]  # the intent survived


def test_an_outcome_must_belong_to_its_effect() -> None:
    """DL-118's readback half at the outcome layer: an outcome for an effect
    this outbox never saw means the log lost the record that said what was
    meant, and an outcome naming a different run_id than its effect bound is
    a stranger's fate filed under this run's intent. Both refuse."""
    rid = "00000000-0000-4000-8000-00000000000c"
    outbox = Outbox()
    with pytest.raises(EngineError, match="unknown effect"):
        outbox.resolve(EffectOutcome(effect_id="e9:SPAWN:ghost.1", state="applied"))
    effect = _effect("SPAWN", run_id=rid, generation=0)
    outbox.record(effect)
    with pytest.raises(EngineError, match="stranger's fate"):
        outbox.resolve(
            EffectOutcome(effect_id=effect.effect_id, state="applied", run_id="not-this-run")
        )
    outbox.resolve(EffectOutcome(effect_id=effect.effect_id, state="applied", run_id=rid))
    assert outbox.state_of(effect.effect_id) == "applied"


def test_generation_is_strict_and_never_coerced() -> None:
    """The fold gate compares generation to exactly 0; lax pydantic would
    coerce `false`, `"0"` and `0.0` into integers that pass it. Strict mode
    refuses them at the model."""
    for bogus in (False, "0", 0.0):
        with pytest.raises(ValidationError):
            Effect.model_validate(
                {
                    "effect_id": "e1:SPAWN:j.1",
                    "kind": "SPAWN",
                    "job": "j",
                    "run_number": 1,
                    "executor_id": "local",
                    "index": 1,
                    "at": T0.isoformat(),
                    "generation": bogus,
                }
            )


def test_the_outbox_keeps_admission_order() -> None:
    """ss5 makes per-run effect ordering mandatory. A KILL decided after a
    SPAWN that overtook it would stop a run that had not started."""
    outbox = Outbox()
    spawn, kill = _effect("SPAWN", index=1), _effect("KILL", index=2)
    outbox.record(spawn)
    outbox.record(kill)
    assert [e.kind for e in outbox.pending()] == ["SPAWN", "KILL"]
    # and recording the same effect twice -- replay meeting the live engine's
    # own record -- neither duplicates it nor reorders it
    outbox.record(spawn)
    assert [e.effect_id for e in outbox.pending()] == [spawn.effect_id, kill.effect_id]


def test_an_effect_id_is_derived_not_minted() -> None:
    """Replay reconstructs the same outbox without trusting a uuid the log
    happens to carry. One admitted input decides at most one effect of each
    kind per job, so the index plus that triple is already unique."""
    assert effect_id_for(7, "SPAWN", "nightly", 3) == "e7:SPAWN:nightly.3"
    assert effect_id_for(7, "KILL", "nightly", 3) != effect_id_for(7, "SPAWN", "nightly", 3)


#: the identity binds every planning call needs (DL-118): a deterministic
#: mint so assertions can name the ids, generation 0, no prior bindings.
_IDENTITY: dict = dict(generation=0, run_ids={}, mint_run_id=lambda: "rid-minted")


# ------------------------------------------------------- 2. planning (ss4 step 7)


def test_planning_reads_the_ghost_run_gate() -> None:
    """A CHANGE_STATUS-parity STARTING overwrite re-emits the status without
    advancing the run, and vendor parity launches nothing. The gate has
    always meant that; what moved is WHERE it is asked -- it now decides
    whether an effect EXISTS, which is the honest place, because the shell
    never intended to act."""
    emitted = [_ev("STATUS", 0, job="j", status="STARTING")]
    common = dict(index=1, executor_id="local", live={}, dispatchable=frozenset({"j"}), **_IDENTITY)

    real = plan_effects(emitted, runs={"j": 1}, dispatched={}, **common)  # type: ignore[arg-type]
    assert [e.kind for e in real] == ["SPAWN"] and real[0].run_number == 1

    ghost = plan_effects(emitted, runs={"j": 1}, dispatched={"j": 1}, **common)  # type: ignore[arg-type]
    assert ghost == []


def test_a_terminal_with_no_live_run_plans_no_kill() -> None:
    """Planning an effect that could only ever be superseded would put noise
    in the log and give a reader a kill that never happened to explain."""
    emitted = [_ev("STATUS", 0, job="j", status="SUCCESS")]
    common = dict(
        index=1,
        executor_id="local",
        runs={"j": 1},
        dispatched={"j": 1},
        dispatchable=frozenset({"j"}),
        **_IDENTITY,
    )
    assert plan_effects(emitted, live={}, **common) == []  # type: ignore[arg-type]
    [kill] = plan_effects(emitted, live={"j": 1}, **common)  # type: ignore[arg-type]
    assert (kill.kind, kill.run_number) == ("KILL", 1)


def test_two_terminal_transitions_of_one_live_run_plan_one_kill() -> None:
    """A deadline firing inside a `CHANGE_STATUS SUCCESS` batch ends one live
    run twice, at two instants. One derived id cannot carry two contents, so
    the planner keeps the first: the run's actual kill (DL-232)."""
    emitted = [
        _ev("STATUS", 2, job="j", status="TERMINATED"),
        _ev("STATUS", 2.5, job="j", status="SUCCESS"),
    ]
    [kill] = plan_effects(
        emitted,
        index=3,
        executor_id="local",
        runs={"j": 1},
        dispatched={"j": 1},
        live={"j": 1},
        dispatchable=frozenset({"j"}),
        **_IDENTITY,
    )
    assert (kill.kind, kill.run_number, kill.at) == ("KILL", 1, T0 + timedelta(minutes=2))


def test_boxes_and_ghosts_get_no_effects() -> None:
    """`dispatchable` is the catalog jobs with a registered adapter. A box
    folds from its members and a CHANGE_STATUS-invented entity has no
    definition, so neither is ever something to act on."""
    emitted = [_ev("STATUS", 0, job="thebox", status="STARTING")]
    assert (
        plan_effects(
            emitted,
            index=1,
            executor_id="local",
            runs={"thebox": 1},
            dispatched={},
            live={},
            dispatchable=frozenset(),
            **_IDENTITY,
        )
        == []
    )


def test_pr36a_a_spawn_mints_its_run_id_in_the_planning_transaction() -> None:
    """period-model ss2.3: every SPAWN effect carries `run_id`, minted with
    the effect -- identity is created in the decision transaction and
    nowhere later, so the WAL and the spool can only ever name one key."""
    emitted = [_ev("STATUS", 0, job="j", status="STARTING")]
    [spawn] = plan_effects(
        emitted,
        index=1,
        executor_id="local",
        runs={"j": 1},
        dispatched={},
        live={},
        dispatchable=frozenset({"j"}),
        **_IDENTITY,
    )
    assert (spawn.kind, spawn.run_id) == ("SPAWN", "rid-minted")


def test_pr36a_a_kill_carries_the_run_id_its_spawn_bound_or_none() -> None:
    """A KILL never mints: the run it stops already has an identity, bound by
    the SPAWN this run root recorded -- or by no one, for a run a pre-DL-118
    journal spawned, which is the one honest None."""
    emitted = [_ev("STATUS", 0, job="j", status="SUCCESS")]
    common = dict(
        index=2,
        executor_id="local",
        runs={"j": 1},
        dispatched={"j": 1},
        live={"j": 1},
        dispatchable=frozenset({"j"}),
        generation=0,
        mint_run_id=_refuse_mint,
    )
    [kill] = plan_effects(emitted, run_ids={("j", 1): "rid-from-spawn"}, **common)  # type: ignore[arg-type]
    assert (kill.kind, kill.run_id) == ("KILL", "rid-from-spawn")
    [legacy] = plan_effects(emitted, run_ids={}, **common)  # type: ignore[arg-type]
    assert (legacy.kind, legacy.run_id) == ("KILL", None)


def _refuse_mint() -> str:
    raise AssertionError("a KILL must not mint an identity")


def test_pr16_every_effect_records_the_generation_it_was_born_under() -> None:
    """The executor host row's generation, read at birth: an effect born
    before an eviction cannot pass for one born after it, because the value
    it read is in the durable record."""
    emitted = [_ev("STATUS", 0, job="j", status="STARTING")]
    [spawn] = plan_effects(
        emitted,
        index=1,
        executor_id="local",
        runs={"j": 1},
        dispatched={},
        live={},
        dispatchable=frozenset({"j"}),
        generation=3,
        run_ids={},
        mint_run_id=lambda: "rid",
    )
    assert spawn.generation == 3


# --------------------------------------------------------- 3. supersession (ss5)


def test_a_spawn_for_a_run_that_has_ended_is_superseded_not_applied() -> None:
    """ss5's motivating case, and the reason the guard is not a generation
    comparison: KILLJOB does not advance `run_number`, so a delayed SPAWN for
    run N is still "current" after run N has been TERMINATED. A version check
    would wave it through and start a process for a job the operator just
    killed."""
    effect = _effect("SPAWN", run_number=1)
    running = JobRuntime(status="RUNNING", run_number=1)
    assert superseded_reason(effect, running, None) is None

    killed = JobRuntime(status="TERMINATED", run_number=1)  # SAME run_number
    reason = superseded_reason(effect, killed, None)
    assert reason is not None and "already TERMINATED" in reason


def test_a_spawn_for_a_superseded_run_number_is_superseded() -> None:
    effect = _effect("SPAWN", run_number=1)
    reason = superseded_reason(effect, JobRuntime(status="RUNNING", run_number=2), None)
    assert reason is not None and "at run 2, not the 1" in reason


def test_a_spawn_for_a_job_set_inactive_is_superseded() -> None:
    """ss5 asks whether the run is still desired running, not only whether it
    has ended. An injected INACTIVE sets the run aside without ending it or
    moving its `run_number`, so only the status can tell (DL-232)."""
    effect = _effect("SPAWN", run_number=1)
    assert superseded_reason(effect, JobRuntime(status="STARTING", run_number=1), None) is None
    reason = superseded_reason(effect, JobRuntime(status="INACTIVE", run_number=1), None)
    assert reason == "j is INACTIVE: the run this spawn was for is no longer desired running"


def test_a_kill_names_the_run_it_was_decided_for() -> None:
    """The other direction of the same rule. A kill decided for run 1 must
    not stop run 2 -- which is a live run nobody asked to stop, started after
    the kill was decided."""
    effect = _effect("KILL", run_number=1)
    assert superseded_reason(effect, JobRuntime(status="RUNNING", run_number=1), 1) is None
    for live, marker in ((2, "live run is 2"), (None, "no live run to kill")):
        reason = superseded_reason(effect, JobRuntime(status="RUNNING", run_number=1), live)
        assert reason is not None and marker in reason


def test_an_effect_for_a_job_with_no_row_is_superseded() -> None:
    reason = superseded_reason(_effect(), None, None)
    assert reason is not None and "no runtime row" in reason


def test_an_injected_inactive_on_a_running_job_plans_no_kill() -> None:
    """DL-235: CHANGE_STATUS INACTIVE on a launched run plans no KILL -- the
    process runs on and only its reservations release (DL-120), as the
    vendor's database write would leave them. The contrasting case, a
    terminal CHANGE_STATUS on the same live run, does plan one
    (`test_a_terminal_with_no_live_run_plans_no_kill` above)."""
    emitted = [_ev("STATUS", 0, job="j", status="INACTIVE")]
    common = dict(
        index=1,
        executor_id="local",
        runs={"j": 1},
        dispatched={"j": 1},
        dispatchable=frozenset({"j"}),
        **_IDENTITY,
    )
    assert plan_effects(emitted, live={"j": 1}, **common) == []  # type: ignore[arg-type]


# ---------------------------------------------- 4. the engine drives the outbox


def test_a_start_records_its_intent_in_the_decision_that_planned_it(tmp_path: Path) -> None:
    """ss4 step 7 and ss1: the decision and the outbox entries it implies are
    ONE batch -- and since DL-118 one RECORD, so there is no order between
    them left to get wrong.

    This is what remains of `test_a_start_writes_its_intent_before_it_
    launches_anything`, which asserted that the `effect` record followed the
    `result` record. Ordering was the most the two-record shape could prove,
    and it is not the property ss4 step 7 asks for: an engine could write
    them in that order and still die between the fsyncs. The atomicity claim
    is now `tests/test_decision_record.py`'s
    `test_pr35_decision_and_effects_commit_together`. What stays here is the
    record's contents and the outbox it rebuilds."""
    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    [decision] = [r for r in records if r["rec"] == "decision"]
    assert decision["decision"] == "applied" and decision["legacy_batch"] is False
    [intent] = decision["effects"]
    assert (intent["kind"], intent["job"], intent["run_number"]) == ("SPAWN", "j", 1)
    assert intent["index"] == decision["index"]  # the effect names its own decision
    assert intent["executor_id"] == "local"  # ss5: at-most-once is bound to a host
    [outcome] = [r for r in records if r["rec"] == "effect_result"]
    assert outcome["state"] == "applied"
    assert engine.outbox.state_of(intent["effect_id"]) == "applied"

    # and the log rebuilds the same outbox: a resuming engine knows what the
    # last one meant to do without re-deriving it from a planner that may
    # have changed
    rebuilt = read_outbox(records)
    assert rebuilt.state_of(intent["effect_id"]) == "applied"
    assert rebuilt.pending() == []


def test_cm09_an_applied_effect_is_never_applied_twice() -> None:
    """At-most-once application. Two drains of the outbox must not launch two
    processes -- which is the whole of ss0's safety property at this tier."""
    engine = _engine()

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        launched = engine.oracle.store.job["j"].run_number
        assert engine.live_jobs() == frozenset({"j"})

        engine._dispatch()  # a second drain, deliberately
        engine._dispatch()
        assert engine.live_jobs() == frozenset({"j"})
        assert engine.oracle.store.job["j"].run_number == launched
        assert engine.outbox.pending() == []

    asyncio.run(scenario())


def test_a_kill_is_an_effect_with_an_id() -> None:
    """DL-93's phrasing, made literal: the shell's stop is a recorded act,
    not a `task.cancel()` nothing wrote down. Both effects for the run appear
    in admission order, and both resolve."""
    engine = _engine()

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        engine.inject(_ev("KILLJOB", 1, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

        kinds = [(e.kind, engine.outbox.state_of(e.effect_id)) for e in engine.outbox.effects()]
        assert kinds == [("SPAWN", "applied"), ("KILL", "applied")]
        assert engine.live_jobs() == frozenset()

    asyncio.run(scenario())


def test_a_drain_holds_spawns_and_lets_kills_through() -> None:
    """ss8's routing column is about NEW work: `passive` says running work
    continues to completion, and a kill is how running work ends. Holding
    kills during a drain would make KILLJOB stop working exactly while an
    operator is most likely to reach for it."""
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    engine = _engine(
        "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="a"))
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id="local"),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=0),
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=20))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STARTJOB", 0.5, job="b"))  # held: no new work routed
        engine.inject(_ev("KILLJOB", 0.6, job="a"))  # delivered: it stops running work
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

        assert engine.held_jobs() == frozenset({"b"})
        assert engine.live_jobs() == frozenset()
        assert engine.oracle.store.job["a"].status == "TERMINATED"

    asyncio.run(scenario())


def test_the_held_set_is_the_outbox(tmp_path: Path) -> None:
    """DL-94 derived held-ness from the oracle's status because intent had
    nowhere durable to live. It does now, and the difference is visible where
    it matters: a held start survives a restart as a pending effect, with no
    special case in reconciliation to stop it being failed as never-spawned."""
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    engine = start_run(
        lower_source(_SOLO_JIL),
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id="local"),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert (await drained).decision == "applied"
        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    assert engine.held_jobs() == frozenset({"j"})
    [held] = engine.outbox.pending()
    assert (held.kind, held.job) == ("SPAWN", "j")
    assert engine.journal is not None
    engine.journal.close()

    # and the log says so on its own, with no engine to ask
    rebuilt = read_outbox(read_journal(tmp_path / "run" / "journal.jsonl"))
    assert [(e.kind, e.job) for e in rebuilt.pending()] == [("SPAWN", "j")]


# ------------------------------------------- 5. the leak a recorded kill closes


def test_a_recorded_kill_is_resolved_from_the_spool_three_ways(short_root: Path) -> None:
    """ss5's three states over the case that produces all of them: a kill
    the engine decided and did not deliver, met at resume with no live
    wrapper.

    `status.json` saying signalled means the kill landed. Saying exited means
    the run finished first, so the kill is retired -- superseded by the
    truth. NO status record and no live wrapper means nobody can say, and
    `indeterminate` is the only honest answer (E7). The three-way split is
    the point: two of these would report a signal that did land as one that
    did not."""
    from dsl41.runner_startup import _kill_outcome_from_spool

    run_dir = short_root / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    effect = _effect("KILL")

    unobserved = _kill_outcome_from_spool(short_root, effect)
    assert unobserved.state == "indeterminate"
    assert "nothing can say whether it landed" in (unobserved.detail or "")

    # the record's own verdict is the TOP-LEVEL `outcome`; `observed` is the
    # wrapper's forensics about how the group died (supervisor-protocol ss3),
    # and this fixture named it in its first draft -- which is how DL-98
    # found the engine reading it as the verdict too
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "run_id": "r9",
                "outcome": "terminated",
                "cause": "parent lost",
                "observed": {"outcome": "signaled", "signal": 15},
            }
        )
    )
    landed = _kill_outcome_from_spool(short_root, effect)
    assert (landed.state, landed.run_id) == ("applied", "r9")

    (run_dir / "status.json").write_text(
        json.dumps({"run_id": "r9", "outcome": "signaled", "signal": 9})
    )
    assert _kill_outcome_from_spool(short_root, effect).state == "applied"

    (run_dir / "status.json").write_text(
        json.dumps({"run_id": "r9", "outcome": "exited", "exit_code": 0})
    )
    finished = _kill_outcome_from_spool(short_root, effect)
    assert finished.state == "retired"
    assert "the spool records no kill" in (finished.detail or "")

    # ...and the detail does not claim a run that never started "ended on
    # its own": `spawn_failed` and an unrecognised record are retired too,
    # and the sentence covers all three (DL-151)
    for record in ({"outcome": "spawn_failed", "error": "no such file"}, {"outcome": "moon"}):
        (run_dir / "status.json").write_text(json.dumps({"run_id": "r9", **record}))
        never = _kill_outcome_from_spool(short_root, effect)
        assert never.state == "retired"
        assert never.detail == finished.detail


# ------------------------------- the three arms of _apply_effect (DL-105)


def test_a_held_spawn_whose_run_has_since_ended_is_retired_not_applied() -> None:
    """ss5's supersession, delivered rather than merely decided. Held work is
    the case that makes it reachable on one host: the drain parks a SPAWN in
    the outbox and the operator kills the job while it waits. Supersession
    is decided before the routing hold (DL-234), so the dispatch pass after
    the kill retires the effect while the host is still drained. Applying it
    when the host comes back would start a run the estate has already ended.

    `superseded_reason` is proven as a function elsewhere; this is the wiring
    that acts on it, which is a different claim (DL-105)."""
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    engine = _engine()

    async def scenario() -> None:
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id="local"),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        [held] = engine.outbox.pending()
        assert (held.kind, held.job, held.run_number) == ("SPAWN", "j", 1)
        assert engine.live_jobs() == frozenset()  # held: no process behind it

        engine.inject(_ev("KILLJOB", 2, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert engine.oracle.store.job["j"].status == "TERMINATED"
        assert engine.outbox.state_of(held.effect_id) == "retired"  # before activate

        back = engine.submit_host(
            HostCommand(verb="activate", host_id="local"),
            Envelope(request_id="r2", expect={"host:local": 2}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=4))
        assert (await back).decision == "applied"

    asyncio.run(scenario())
    assert engine.live_jobs() == frozenset()  # the routing came back, the run did not
    assert engine.oracle.store.job["j"].run_number == 1  # and nothing re-ran it
    result = engine.outbox.result_for("e2:SPAWN:j.1")
    assert result is not None and result.state == "retired"
    assert result.detail == "j is already TERMINATED: the run this spawn was for has ended"
    assert engine.outbox.pending() == []


#: a renewable slot, so a run that holds capacity shows it on its row
_SLOT_JIL = (
    "insert_resource: SLOT\nres_type: R\namount: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nresources: (SLOT, QUANTITY=1)\n"
)


def test_a_held_spawn_whose_job_was_set_inactive_is_retired_not_applied() -> None:
    """The TERMINATED case above, with `CHANGE_STATUS INACTIVE` instead of a
    kill. INACTIVE is not terminal and leaves `run_number` alone, so before
    DL-232 the spawn passed both checks and launched a run that held no
    capacity: the INACTIVE edge had already released the slot."""
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    engine = _engine(_SLOT_JIL)

    async def scenario() -> None:
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id="local"),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=10))
        assert (await drained).decision == "applied"

        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        [held] = engine.outbox.pending()
        assert (held.kind, held.job, held.run_number) == ("SPAWN", "j", 1)
        assert engine.oracle.store.job["j"].reservations != ()  # the slot is held

        engine.inject(_ev("STATUS", 2, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        row = engine.oracle.store.job["j"]
        assert (row.status, row.run_number, row.reservations) == ("INACTIVE", 1, ())

        back = engine.submit_host(
            HostCommand(verb="activate", host_id="local"),
            Envelope(request_id="r2", expect={"host:local": 2}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=4))
        assert (await back).decision == "applied"

    asyncio.run(scenario())
    assert engine.live_jobs() == frozenset()  # no process behind the INACTIVE row
    row = engine.oracle.store.job["j"]
    assert (row.status, row.run_number, row.reservations) == ("INACTIVE", 1, ())
    result = engine.outbox.result_for("e2:SPAWN:j.1")
    assert result is not None and result.state == "retired"
    assert result.detail == "j is INACTIVE: the run this spawn was for is no longer desired running"
    assert engine.outbox.pending() == []


#: j's dependent, so a completion wrongly applied to an INACTIVE row would
#: be visible one hop downstream too
_INACTIVE_GATE_JIL = (
    "insert_job: j\njob_type: c\ncommand: x\n\n"
    "insert_job: dep\njob_type: c\ncommand: y\ncondition: s(j)\n"
)


def test_a_completion_for_a_job_an_operator_set_inactive_is_rejected_not_applied(
    tmp_path: Path,
) -> None:
    """DL-235's other gate. A `CHANGE_STATUS INACTIVE` on a launched run
    plans no KILL, so the process runs on and its natural exit still
    arrives -- the test above holds a SPAWN; this one holds nothing, and the
    completion comes in through the stale-completion gate (ss4) instead of
    ss5's supersession. Applying it would turn the operator's INACTIVE into
    SUCCESS or FAILURE and start `dep`, undoing the reset. Mirrors
    `test_a_held_spawn_whose_job_was_set_inactive_is_retired_not_applied`
    above and `test_cm06_a_result_from_a_superseded_run_is_not_applied`
    (test_model_harness.py), whose live-vs-replayed check this restates
    with an injected INACTIVE in place of a fresh overwriting run."""
    catalog = lower_source(_INACTIVE_GATE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},  # inert: no natural exit of its own
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"

        engine._enqueue(  # forge the late natural exit as a COMPLETION
            _ev("STATUS", 2, job="j", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    row = engine.oracle.store.job["j"]
    assert (row.status, row.run_number) == ("INACTIVE", 1)
    assert engine.oracle.store.job["dep"].status == "INACTIVE"  # never started
    assert engine.live_jobs() == {"j"}  # the process is still live in the shell
    assert all(e.kind != "KILL" for e in engine.outbox.effects())  # no KILL planned
    assert engine.drops and engine.drops[-1][1] == "job not live: INACTIVE"
    rejected = engine.decisions.for_index(engine.frontiers.applied_index)
    assert rejected is not None and rejected.decision == "rejected"
    assert rejected.reason == "job not live: INACTIVE"
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    # a decision record with its reason, not the resume sweep's "drop" rec
    assert [r for r in records if r.get("rec") == "drop"] == []
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    assert (fresh.store.job["j"].status, fresh.store.job["j"].run_number) == ("INACTIVE", 1)
    assert fresh.store.job["dep"].status == engine.oracle.store.job["dep"].status
    assert [e.effect_id for e in replay.outbox.effects()] == [
        e.effect_id for e in engine.outbox.effects()
    ]


def test_a_restart_on_an_inactive_row_cancels_the_orphan_and_spawns_again() -> None:
    """DL-235's M1. An injected INACTIVE leaves the earlier run live in the
    shell (no KILL), and the oracle's DL-81 refusal only blocks STARTJOB on
    a STARTING/RUNNING/QUE_WAIT row, so a restart on the INACTIVE row is
    admitted. `_apply_spawn`'s "one live attempt per job" branch -- marked
    unreachable before DL-235 -- is reached this way: the shell holds one
    live run per job, so the earlier adapter task is cancelled, and the
    second SPAWN is the record of the supersession. A real CMD adapter
    runs its kill ladder on that cancel (runner_adapters.py); this
    FakeAdapter test pins the cancel, not the ladder. The vendor would run
    both."""
    engine = _engine(_SOLO_JIL)
    orphan: asyncio.Task[None] | None = None

    async def scenario() -> None:
        nonlocal orphan
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"
        orphan = engine._live["j"].task

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"
        assert not orphan.done()  # still live in the shell, no KILL

        engine.inject(_ev("STARTJOB", 2, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    assert orphan is not None and orphan.cancelled()  # the earlier task is gone
    row = engine.oracle.store.job["j"]
    assert (row.status, row.run_number) == ("RUNNING", 2)
    assert engine.live_jobs() == {"j"}
    assert engine._live["j"].run_number == 2  # run 2 only, nothing left of run 1
    kinds = [(e.kind, e.job, e.run_number) for e in engine.outbox.effects()]
    assert kinds == [("SPAWN", "j", 1), ("SPAWN", "j", 2)]  # two SPAWNs, no KILL


def test_a_crash_window_completion_on_an_inactive_row_recovers_as_rejected(
    tmp_path: Path,
) -> None:
    """DL-235's compatibility statement, pinned. The admitted run-1
    completion for a row an operator set INACTIVE crashes before its
    rejection is durable (the ss4 gate's crash window, DL-156); replay
    re-decides it through the same gate and must reach the SAME verdict
    the live engine did -- `STATE_MACHINE_VERSION` moved to 2 exactly
    because v1 re-derives a different verdict here. `dep` pins that
    the recovered rejection, not an accepted completion, is what reaches
    the dependent."""
    catalog = lower_source(_INACTIVE_GATE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="j"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["j"].status == "RUNNING"

        engine.inject(_ev("STATUS", 1, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["j"].status == "INACTIVE"

        engine._enqueue(  # the admitted run-1 completion, about to lose its decision
            _ev("STATUS", 2, job="j", run_number=1, exit_code=0), source="adapter"
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    _lose_the_decision_for(tmp_path / "run", "adapter")

    path = wal_path(tmp_path / "run", 1)
    records = read_journal(path)
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    [seq] = [
        r["seq"]
        for r in records
        if r.get("rec") == "input" and r.get("kind") == "STATUS" and r.get("source") == "adapter"
    ]
    recovered = replay.decisions.for_index(seq)
    assert recovered is not None
    assert recovered.decision == "rejected"
    assert recovered.reason == "job not live: INACTIVE"
    assert replay.recovered == [
        recovered
    ]  # reached through the recovery path, not a durable record
    assert (fresh.store.job["j"].status, fresh.store.job["j"].run_number) == ("INACTIVE", 1)
    assert fresh.store.job["dep"].status == "INACTIVE"  # never started


#: a negative term_run_time arms a deadline already past due, which the
#: frontier rule fires inside the next input's batch, back-dated to its due
#: instant: two terminal transitions of one live run, at two instants, in one
#: batch
_PAST_DUE_JIL = "insert_job: x\njob_type: c\ncommand: sleep 300\nterm_run_time: -1\n"


def test_a_deadline_and_a_status_ending_one_run_in_one_batch_record_one_kill(
    tmp_path: Path,
) -> None:
    """Before DL-232 the batch planned two KILLs under one id with different
    instants, and `Outbox.record` raised after the decision was journaled. The
    replay of that log re-records effects through the same path, so resume
    met the same error. Now the loop records one KILL, and the log replays."""
    catalog = lower_source(_PAST_DUE_JIL)
    engine = start_run(
        catalog,
        tmp_path / "run",
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, job="x"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["x"].status == "RUNNING"  # the deadline waits
        assert engine.live_jobs() == frozenset({"x"})
        engine.inject(_ev("STATUS", 0, job="x", status="SUCCESS"))
        await engine.run_until_quiescent(T0)

    asyncio.run(scenario())
    assert [(t.at, t.transition) for t in engine.oracle.trace()][-2:] == [
        (T0 - timedelta(minutes=1), "RUNNING->TERMINATED"),
        (T0, "TERMINATED->SUCCESS"),
    ]
    [kill] = [e for e in engine.outbox.effects() if e.kind == "KILL"]
    assert (kill.job, kill.run_number, kill.at) == ("x", 1, T0 - timedelta(minutes=1))
    assert engine.journal is not None
    engine.journal.close()

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    fresh = Oracle(catalog)
    replay = replay_inputs(fresh, records)
    assert [e.effect_id for e in replay.outbox.effects() if e.kind == "KILL"] == [kill.effect_id]
    assert fresh.store.job["x"].status == "SUCCESS"


async def _drained(engine: Engine) -> None:
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    drained = engine.submit_host(
        HostCommand(verb="drain", host_id="local"),
        Envelope(
            request_id="drain",
            expect={"host:local": engine.oracle.store.revision("host:local")},
            epoch=engine.epoch,
        ),
    )
    await engine.run_until_quiescent(engine.clock.now() + timedelta(seconds=10))
    assert (await drained).decision == "applied"


async def _activated(engine: Engine, minutes: float) -> None:
    from dsl41.runner_admission import Envelope
    from dsl41.runner_hosts import HostCommand

    back = engine.submit_host(
        HostCommand(verb="activate", host_id="local"),
        Envelope(
            request_id="activate",
            expect={"host:local": engine.oracle.store.revision("host:local")},
            epoch=engine.epoch,
        ),
    )
    await engine.run_until_quiescent(T0 + timedelta(minutes=minutes))
    assert (await back).decision == "applied"


@pytest.mark.parametrize("ending", ["INACTIVE", "TERMINATED"])
def test_a_starting_overwrite_after_a_retired_spawn_launches_nothing(
    tmp_path: Path, ending: str
) -> None:
    """DL-232: a retired effect stays retired. Retirement counts the run as
    dispatched, so a `CHANGE_STATUS STARTING` at the same run number meets
    the ghost-run gate and plans nothing. Before, the gate planned a second
    SPAWN with a second identity and the engine died on DL-118's
    one-run-one-identity check. The log replays, and a resume agrees."""
    from dsl41.runner_startup import resume_run

    root = tmp_path / "run"
    catalog = lower_source(_SOLO_JIL)

    async def scenario() -> None:
        engine = start_run(
            catalog, root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter(default=None)}
        )
        await _drained(engine)
        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        if ending == "TERMINATED":
            engine.inject(_ev("KILLJOB", 2, job="j"))
        else:
            engine.inject(_ev("STATUS", 2, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _activated(engine, 4)
        engine.inject(_ev("STATUS", 5, job="j", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=6))

        assert engine.live_jobs() == frozenset()
        row = engine.oracle.store.job["j"]
        assert (row.status, row.run_number) == ("STARTING", 1)
        [spawn] = [e for e in engine.outbox.effects() if e.kind == "SPAWN"]
        assert engine.outbox.state_of(spawn.effect_id) == "retired"
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

        records = read_journal(root / "journal.jsonl")
        planned = [e for r in records if r["rec"] == "decision" for e in r["effects"]]
        assert [(e["kind"], e["job"], e["run_number"]) for e in planned] == [("SPAWN", "j", 1)]

        # resume replays the same log and rebuilds `_dispatched` from the rows
        resumed = await resume_run(
            catalog,
            root,
            clock=VirtualClock(start=T0 + timedelta(minutes=6)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        assert resumed.oracle.store.job["j"].model_dump() == row.model_dump()
        assert resumed.outbox.state_of(spawn.effect_id) == "retired"
        assert resumed._dispatched == engine._dispatched == {"j": 1}
        await resumed.run_until_quiescent(T0 + timedelta(minutes=7))
        assert resumed.live_jobs() == frozenset()
        assert [e.effect_id for e in resumed.outbox.effects() if e.kind == "SPAWN"] == [
            spawn.effect_id
        ]
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()

    asyncio.run(scenario())


#: two jobs that share one renewable slot
_ONE_SLOT_JIL = (
    "insert_resource: SLOT\nres_type: R\namount: 1\n\n"
    "insert_job: j\njob_type: c\ncommand: x\nresources: (SLOT, QUANTITY=1)\n\n"
    "insert_job: k\njob_type: c\ncommand: x\nresources: (SLOT, QUANTITY=1)\n"
)


def test_a_held_spawn_is_retired_at_the_edge_that_ends_its_run_not_at_activation() -> None:
    """DL-232: supersession is decided before the routing hold. The INACTIVE
    edge releases j's slot and retires j's held SPAWN in the dispatch pass
    that follows it. A later `CHANGE_STATUS RUNNING` cannot re-arm it, so
    at activation only k launches, and k holds the slot. Before, the spawn
    waited behind the hold, read RUNNING at activation, and launched j with
    no reservation beside k."""
    engine = _engine(_ONE_SLOT_JIL)

    async def scenario() -> None:
        await _drained(engine)
        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        [held] = engine.outbox.pending()
        assert (held.kind, held.job) == ("SPAWN", "j")

        engine.inject(_ev("STATUS", 2, job="j", status="INACTIVE"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.outbox.state_of(held.effect_id) == "retired"  # still drained

        engine.inject(_ev("STATUS", 2.5, job="j", status="RUNNING"))
        engine.inject(_ev("STARTJOB", 3, job="k"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        await _activated(engine, 4)

    asyncio.run(scenario())
    assert engine.live_jobs() == frozenset({"k"})
    j, k = engine.oracle.store.job["j"], engine.oracle.store.job["k"]
    assert (j.status, j.run_number, j.reservations) == ("RUNNING", 1, ())
    assert k.status == "RUNNING" and k.reservations != ()


def test_a_starting_overwrite_while_a_spawn_is_held_plans_no_second_spawn(
    tmp_path: Path,
) -> None:
    """DL-234: a run has its one identity from its plan. A held SPAWN already
    counts as dispatched, so a `CHANGE_STATUS STARTING` at the same run
    number meets the ghost-run gate and plans nothing. Before, it planned a
    second SPAWN for run 1 with a second identity, and the outbox refused it
    ("one run, one identity", DL-118) after the decision was journaled. The
    log stays consistent, and a resume agrees."""
    from dsl41.runner_startup import resume_run

    root = tmp_path / "run"
    catalog = lower_source(_SOLO_JIL)

    async def scenario() -> None:
        engine = start_run(
            catalog, root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter(default=None)}
        )
        await _drained(engine)
        engine.inject(_ev("STARTJOB", 0.5, job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        [held] = engine.outbox.pending()
        assert (held.kind, held.job, held.run_number) == ("SPAWN", "j", 1)

        engine.inject(_ev("STATUS", 2, job="j", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        row = engine.oracle.store.job["j"]
        assert (row.status, row.run_number) == ("STARTING", 1)
        assert engine.outbox.pending() == [held]  # still current, still held
        assert engine.live_jobs() == frozenset()
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

        records = read_journal(root / "journal.jsonl")
        planned = [e for r in records if r["rec"] == "decision" for e in r["effects"]]
        assert [e["effect_id"] for e in planned] == [held.effect_id]

        resumed = await resume_run(
            catalog,
            root,
            clock=VirtualClock(start=T0 + timedelta(minutes=3)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        assert resumed.oracle.store.job["j"].model_dump() == row.model_dump()
        assert [e.effect_id for e in resumed.outbox.pending()] == [held.effect_id]
        assert resumed._dispatched == engine._dispatched == {"j": 1}
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()

    asyncio.run(scenario())
