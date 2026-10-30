"""A per-call cost that grew with the length of a period, and the proof that
removing it changed no answer.

The control `trace` verb used to copy the whole oracle trace on every call;
the oracle now slices before it copies. Each test builds the state, computes
the answer the old way and the new way, and requires the same bytes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import VirtualClock
from dsl41.runner_control import ControlServer

T0 = datetime(2026, 7, 1, 8, 0)

_JIL = (
    "insert_job: pa\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: pb\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(pa)\n"
)


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
