"""Runner time domains (ss9): the engine's clocks, and `EngineError`.

The engine time basis is NAIVE UTC in the real domain (RealClock.now()), so DST
never runs the oracle's non-decreasing feed discipline backwards (ss6-ss7;
DL-41a and DL-42 pin the lifecycle semantics). The scheduler converts per-job
zoneinfo ticks to UTC instants.

`EngineError` is the shell's one refusal type. This module is the bottom of
the runner import DAG (DL-74); only `runner_codes` sits below it, as the
registry an EngineError takes its optional code from.
"""

from __future__ import annotations

import asyncio
import heapq

from datetime import UTC, datetime
from typing import Protocol

from dsl41.runner_codes import Code


class EngineError(RuntimeError):
    """A shell-level refusal, never a semantics verdict: the engine cannot make progress.

    `code` is the stable control code of the reason, when the raise site names
    one (control-protocol §2). A control catch site answers a code-less one as
    `engine_error` and never reads the prose."""

    def __init__(self, *args: object, code: Code | None = None) -> None:
        super().__init__(*args)
        self.code: Code | None = code


class ZeroDelayCycleError(EngineError):
    """A zero-delay condition cycle generated unbounded work at one virtual instant (DL-184).

    It carries the instant and the jobs that started there; `rehearse --check-cadence`
    converts exactly this refusal into an unbounded-multi-fire finding."""

    def __init__(self, message: str, *, instant: datetime, jobs: tuple[str, ...]) -> None:
        super().__init__(message)
        self.instant = instant
        self.jobs = jobs


class Clock(Protocol):
    """ss9 time domain: the engine's only source of "now" and waiting."""

    #: True for the virtual domain (engine drives time and settles adapters);
    #: False for the real domain (the loop blocks on real IO instead)
    virtual: bool

    def now(self) -> datetime: ...

    def next_sleeper_due(self) -> datetime | None:
        """Earliest pending adapter sleep, or None. RealClock returns None: real sleeps wake
        themselves."""
        ...

    def pending_sleepers(self) -> int:
        """Count of pending adapter sleeps, for Engine._settle. RealClock returns 0."""
        ...

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        """Engine-side wait (ss9). Real: sleep until `t`, waking early when `interrupt` fires.
        Virtual: jump instantly."""
        ...

    async def sleep_until(self, t: datetime) -> None:
        """Adapter-side blocking wait: returns once now >= t."""
        ...


class VirtualClock:
    """ss9: jumps to the next wake instantly, because the oracle takes explicit timestamps.

    wait_until() moves `now` forward and resolves due sleeps; sleep_until() parks the
    calling adapter task until the clock reaches its deadline. `interrupt` is ignored."""

    virtual = True

    def __init__(self, start: datetime = datetime.min) -> None:
        self._now = start
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = 0  # heap tie-break: registration order

    def now(self) -> datetime:
        return self._now

    def next_sleeper_due(self) -> datetime | None:
        self._prune()
        return self._sleepers[0][0] if self._sleepers else None

    def pending_sleepers(self) -> int:
        self._prune()
        return len(self._sleepers)

    def _prune(self) -> None:
        # a cancelled adapter task leaves a dead future; due/pending reads see live work only
        if any(fut.done() for _, _, fut in self._sleepers):
            self._sleepers = [entry for entry in self._sleepers if not entry[2].done()]
            heapq.heapify(self._sleepers)

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        if t > self._now:
            self._now = t
        self._prune()
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():  # pragma: no branch -- _prune left only live futures
                fut.set_result(None)

    async def sleep_until(self, t: datetime) -> None:
        if t <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (t, self._seq, fut))
        await fut


class RealClock:
    """ss9 wall-clock domain. now() is NAIVE UTC (see the module docstring).

    wait_until() sleeps in bounded slices, waking early when `interrupt` fires.
    sleep_until() is a plain sleep; the engine never drives it, so
    next_sleeper_due() and pending_sleepers() stay empty (DL-43 item 5)."""

    virtual = False

    #: one wait slice is capped, which bounds drift
    _MAX_SLICE_S = 3600.0

    def now(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def next_sleeper_due(self) -> datetime | None:
        return None

    def pending_sleepers(self) -> int:
        return 0

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        while True:
            remaining = (t - self.now()).total_seconds()
            if remaining <= 0:
                return
            slice_s = min(remaining, self._MAX_SLICE_S)
            if interrupt is None:
                await asyncio.sleep(slice_s)
                continue
            try:
                await asyncio.wait_for(interrupt.wait(), timeout=slice_s)
                return  # queue activity: the engine loop re-plans its wait
            except TimeoutError:
                continue

    async def sleep_until(self, t: datetime) -> None:
        remaining = (t - self.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)
