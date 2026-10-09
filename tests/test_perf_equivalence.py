"""Two per-input costs that grew with the length of a period, and the proof
that removing them changed no answer.

The planner used to rebuild its `(job, run_number) -> run_id` map from the
whole outbox on every input, and the control `trace` verb used to copy the
whole oracle trace on every call. The outbox now keeps the SPAWN bindings as
it records, and the oracle slices before it copies. Each test builds the
state, computes the answer the old way and the new way, and requires the
same bytes.
"""

from __future__ import annotations

import json

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dsl41 import runner as runner_mod
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_admission import Applied, ApplyResult
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer
from dsl41.runner_effects import Effect, Outbox, effect_id_for

T0 = datetime(2026, 7, 1, 8, 0)

_JIL = (
    "insert_job: pa\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: pb\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(pa)\n"
)


def _effect(kind: str, job: str, run_number: int, index: int, run_id: str | None = None) -> Effect:
    return Effect(
        effect_id=effect_id_for(index, kind, job, run_number),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        job=job,
        run_number=run_number,
        executor_id="local",
        index=index,
        at=T0,
        run_id=run_id,
        generation=None,
    )


def _old_run_ids(outbox: Outbox) -> dict[tuple[str, int], str]:
    """The computation the engine used to run on every input."""
    return {
        (e.job, e.run_number): e.run_id
        for e in outbox.effects()
        if e.kind == "SPAWN" and e.run_id is not None
    }


def _built_outbox() -> Outbox:
    outbox = Outbox()
    index = 0
    for run_number in range(1, 40):
        for job in ("a", "b", "c"):
            index += 1
            rid = f"{job}-{run_number}"
            outbox.record(_effect("SPAWN", job, run_number, index, rid))
            if run_number % 2:
                index += 1
                outbox.record(_effect("KILL", job, run_number, index, rid))
    # a SPAWN that carries no identity, and a KILL that names an id no SPAWN
    # bound: neither is in the planner's map
    index += 1
    outbox.record(_effect("SPAWN", "d", 1, index, None))
    index += 1
    outbox.record(_effect("KILL", "e", 1, index, "stranger"))
    # an exact replay of a record is a no-op
    outbox.record(_effect("SPAWN", "a", 1, 1, "a-1"))
    return outbox


def test_the_outbox_spawn_bindings_equal_a_walk_of_the_outbox() -> None:
    outbox = Outbox()
    assert dict(outbox.spawn_run_ids()) == _old_run_ids(outbox) == {}
    outbox = _built_outbox()
    assert dict(outbox.spawn_run_ids()) == _old_run_ids(outbox)
    assert ("e", 1) not in outbox.spawn_run_ids()  # the KILL's id is not a SPAWN binding
    assert ("d", 1) not in outbox.spawn_run_ids()  # no identity, no binding
    assert outbox.spawn_run_ids()[("a", 1)] == "a-1"


def test_the_spawn_bindings_view_is_read_only_and_live() -> None:
    outbox = Outbox()
    view = outbox.spawn_run_ids()
    with pytest.raises(TypeError):
        view[("a", 1)] = "x"  # type: ignore[index]
    outbox.record(_effect("SPAWN", "a", 1, 1, "r1"))
    assert view[("a", 1)] == "r1"


def test_a_refused_record_leaves_the_spawn_bindings_alone() -> None:
    from dsl41.runner_clock import EngineError

    outbox = Outbox()
    outbox.record(_effect("SPAWN", "a", 1, 1, "r1"))
    with pytest.raises(EngineError):
        outbox.record(_effect("SPAWN", "b", 1, 2, "r1"))  # one id, two runs
    with pytest.raises(EngineError):
        outbox.record(_effect("KILL", "a", 1, 3, "r2"))  # one run, two ids
    assert dict(outbox.spawn_run_ids()) == _old_run_ids(outbox) == {("a", 1): "r1"}


def test_the_engine_plans_from_the_same_bindings_the_old_walk_built() -> None:
    engine = Engine(
        lower_source(_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    for effect in _built_outbox().effects():
        engine.outbox.record(effect)
    seen: list[dict[tuple[str, int], str]] = []
    real = runner_mod.plan_effects

    def spy(*args: Any, **kwargs: Any) -> list[Effect]:
        seen.append(dict(kwargs["run_ids"]))
        return real(*args, **kwargs)

    applied = Applied(result=ApplyResult(index=1, request_id="r", decision="applied"))
    runner_mod.plan_effects = spy  # type: ignore[assignment]
    try:
        engine._plan_effects(applied, 999)
    finally:
        runner_mod.plan_effects = real  # type: ignore[assignment]
    assert seen == [_old_run_ids(engine.outbox)]


def _traced_oracle() -> Oracle:
    oracle = Oracle(lower_source(_JIL))
    for n in range(4):
        oracle.feed(Event(at=T0 + timedelta(minutes=n), kind="STARTJOB", payload={"job": "pa"}))
        oracle.feed(
            Event(
                at=T0 + timedelta(minutes=n, seconds=30),
                kind="STATUS",
                payload={"job": "pa", "status": "SUCCESS"},
            )
        )
    return oracle


def test_trace_since_equals_filtering_a_full_copy_at_every_edge() -> None:
    oracle = _traced_oracle()
    full = oracle.trace()
    n = len(full)
    assert n > 6
    for since in (-5, -1, 0, 1, 3, n - 1, n, n + 1, n + 100):
        old_entries = [e for seq, e in enumerate(full, start=1) if seq > since]
        last_seq, entries = oracle.trace_since(since)
        assert last_seq == n
        assert entries == old_entries, since
    # the copies do not alias the oracle's own entries
    _, entries = oracle.trace_since(0)
    entries[0].job = "mutated"
    assert oracle.trace()[0].job != "mutated"


def _old_trace_response(oracle: Oracle, since: int) -> dict[str, Any]:
    entries = oracle.trace()
    return {
        "ok": True,
        "last_seq": len(entries),
        "entries": [
            {
                "seq": seq,
                "at": entry.at.isoformat(),
                "job": entry.job,
                "transition": entry.transition,
                "cause": entry.cause,
            }
            for seq, entry in enumerate(entries, start=1)
            if seq > since
        ],
    }


def test_the_trace_verb_answers_the_same_bytes_as_the_full_copy(tmp_path: Path) -> None:
    engine = Engine(
        lower_source(_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    engine.oracle = _traced_oracle()
    server = ControlServer(engine, tmp_path / "control.sock")
    n = len(engine.oracle.trace())
    for since in (-3, 0, 1, 4, n - 1, n, n + 7):
        new = server._trace({"cmd": "trace", "since": since})
        old = _old_trace_response(engine.oracle, since)
        assert json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True), since
    # no `since` at all is the whole trace
    assert server._trace({"cmd": "trace"}) == _old_trace_response(engine.oracle, 0)
