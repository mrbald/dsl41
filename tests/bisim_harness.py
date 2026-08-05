"""Bisimulation harness (runner-design ss13, DL-41 decision 9).

Drives Engine(VirtualClock, inert FakeAdapter) through the Oracle's
feed()/run_script()/trace()/store surface so every SEM trace test in
test_oracle.py parametrizes over the Oracle-direct and Engine paths
unchanged (the autouse fixture there flips which one the oracle() helper
builds).

The FakeAdapter runs default=None (inert): SEM scripts inject STATUS
completions themselves -- the script IS the adapter, exactly as in
oracle-direct runs -- so identical traces are a pinned property, not a
tautology: the engine's advance()-driven timer path, dispatch table, and
event queueing all sit between the script and the oracle in this mode.

All harnesses share one never-closed module-level event loop: adapter tasks
must span feed() calls, per-harness loops would leak fds under
hypothesis-driven tests (hundreds of live harnesses inside one example run),
and Engine.shutdown() at fixture teardown cancels each harness's parked
tasks so nothing is garbage-collected while pending.
"""

from __future__ import annotations

import asyncio

from dsl41.ir import CatalogIR
from dsl41.oracle import Event, StatusStore, TraceEntry
from dsl41.runner import Engine, FakeAdapter, VirtualClock

_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
    return _LOOP


class EngineHarness:
    """Oracle-compatible facade over an Engine driven step-by-step:
    feed(ev) = inject + run_until_quiescent(horizon=ev.at), so the virtual
    clock never runs ahead of the script -- matching the oracle's own lazy
    timer discipline (timers fire when the script's clock reaches them)."""

    def __init__(self, catalog: CatalogIR) -> None:
        adapter = FakeAdapter(default=None)
        self.engine = Engine(
            catalog, clock=VirtualClock(), adapters={"CMD": adapter, "FW": adapter}
        )

    @property
    def store(self) -> StatusStore:
        return self.engine.oracle.store

    def feed(self, ev: Event) -> list[Event]:
        self.engine.inject(ev)
        return _loop().run_until_complete(self.engine.run_until_quiescent(ev.at))

    def run_script(self, events: list[Event]) -> list[TraceEntry]:
        for ev in events:
            self.feed(ev)
        return self.trace()

    def trace(self) -> list[TraceEntry]:
        return self.engine.oracle.trace()

    def close(self) -> None:
        _loop().run_until_complete(self.engine.shutdown())
