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
import shutil
import tempfile

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.ir import lower_source
from dsl41.oracle_state import Event, JobRuntime
from dsl41.runner import Engine, start_run
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
from dsl41.runner_journal import read_journal, read_outbox

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


def _effect(kind: str = "SPAWN", *, job: str = "j", run_number: int = 1, index: int = 1) -> Effect:
    return Effect(
        effect_id=effect_id_for(index, kind, job, run_number),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        job=job,
        run_number=run_number,
        executor_id="local",
        index=index,
        at=T0,
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


# ------------------------------------------------------- 2. planning (ss4 step 7)


def test_planning_reads_the_ghost_run_gate() -> None:
    """A CHANGE_STATUS-parity STARTING overwrite re-emits the status without
    advancing the run, and vendor parity launches nothing. The gate has
    always meant that; what moved is WHERE it is asked -- it now decides
    whether an effect EXISTS, which is the honest place, because the shell
    never intended to act."""
    emitted = [_ev("STATUS", 0, job="j", status="STARTING")]
    common = dict(index=1, executor_id="local", live={}, dispatchable=frozenset({"j"}))

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
    )
    assert plan_effects(emitted, live={}, **common) == []  # type: ignore[arg-type]
    [kill] = plan_effects(emitted, live={"j": 1}, **common)  # type: ignore[arg-type]
    assert (kill.kind, kill.run_number) == ("KILL", 1)


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
        )
        == []
    )


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


# ---------------------------------------------- 4. the engine drives the outbox


def test_a_start_writes_its_intent_before_it_launches_anything(tmp_path: Path) -> None:
    """ss4 step 7 and ss1: the decision and the outbox entry it implies are
    ONE batch. The order in the log is what makes an interrupted engine
    readable -- the effect record sits after the result that decided it and
    before any evidence the act happened."""
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
    kinds = [r["rec"] for r in records]
    assert kinds.index("effect") > kinds.index("result")  # step 7's batch, in order
    [intent] = [r for r in records if r["rec"] == "effect"]
    assert (intent["kind"], intent["job"], intent["run_number"]) == ("SPAWN", "j", 1)
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
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=0),
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


@pytest.fixture
def short_root():
    """AF_UNIX paths are length-limited, so supervisor tests use a short base
    directory rather than pytest's deep tmp_path."""
    directory = tempfile.mkdtemp(prefix="dsl41e-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


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
    from dsl41.runner import _kill_outcome_from_spool

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
    assert "ended on its own" in (finished.detail or "")
