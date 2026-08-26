"""Runner time domains (ss9): the engine's clocks.

Split out of runner.py by DL-74, with the paragraph it owns, verbatim.

Phase 11b (ss6-ss7; DL-41a/DL-42 pin the lifecycle semantics):

- Engine time basis is NAIVE UTC in the real domain (RealClock.now()):
  DST never runs the oracle's non-decreasing feed discipline backwards.
  The 11c scheduler converts per-job zoneinfo ticks to UTC instants.

EngineError lives here too: it is the shell's one refusal type, every module
above raises it, and this module is the bottom of the runner import DAG
(DL-74).
"""

from __future__ import annotations

import asyncio
import heapq

from datetime import UTC, datetime
from typing import Any, Protocol

from dsl41.canon import ARTIFACT_FORMAT_VERSION, CanonError, decode


class EngineError(RuntimeError):
    """A shell-level refusal (never a semantics verdict): the engine detected
    it cannot make progress -- e.g. the zero-delay-cycle guard in
    run_until_quiescent. Loud by design (CLAUDE.md: no silent loss)."""


def parse_sealed_preamble(data: bytes | str, *, where: str) -> tuple[dict[str, Any], object]:
    """The preamble every sealed-artifact reader shares (PR-08d): decode
    ss3.2-canonical JSON, refuse a payload that is not a JSON object, pop
    the stamped `digest` key, and check `artifact_format_version`.

    Returns `(payload, stamped)`. Everything after this is each artifact's
    own: `model_validate`, its own error prose, the stamped-vs-computed
    digest check, and any class-specific coercion (`Attestation.from_bytes`,
    `ArchiveReceipt.from_bytes`)."""
    try:
        payload = decode(data)
    except CanonError as exc:
        raise EngineError(f"{where}: not ss3.2-canonical JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise EngineError(f"{where}: not a JSON object")
    stamped = payload.pop("digest", None)
    version = payload.get("artifact_format_version")
    if version != ARTIFACT_FORMAT_VERSION:
        raise EngineError(
            f"{where}: artifact_format_version {version!r}: this binary implements"
            f" {ARTIFACT_FORMAT_VERSION} (PR-08d)"
        )
    return payload, stamped


class Clock(Protocol):
    """ss9 time domain: the engine's only source of "now" and waiting."""

    #: True for the virtual domain (engine drives time and settles adapters);
    #: False for the real domain (the loop blocks on real IO instead)
    virtual: bool

    def now(self) -> datetime: ...

    def next_sleeper_due(self) -> datetime | None:
        """Earliest pending adapter sleep, or None. RealClock (11b) returns
        None -- real sleeps wake themselves; only the virtual domain needs
        the engine to drive them forward."""
        ...

    def pending_sleepers(self) -> int:
        """Count of pending adapter sleeps. Virtual-domain bookkeeping for
        Engine._settle; RealClock (11b) returns 0 and its loop blocks on
        real IO instead of settling."""
        ...

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        """Engine-side wait (ss9): real -- sleep until `t`, waking early when
        `interrupt` fires (queue activity); virtual -- jump instantly."""
        ...

    async def sleep_until(self, t: datetime) -> None:
        """Adapter-side blocking wait: returns once now >= t."""
        ...


class VirtualClock:
    """ss9: jumps to the next wake instantly -- enabled by the oracle taking
    explicit timestamps everywhere. The engine owns time: wait_until() moves
    `now` forward and resolves due sleeps; sleep_until() parks the calling
    adapter task until the engine's clock reaches its deadline. `interrupt`
    is ignored: jumps are instantaneous, there is nothing to interrupt."""

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
        # a cancelled adapter task (engine cancel on terminal status) leaves
        # a dead future behind; drop them so due/pending reads see live work
        if any(fut.done() for _, _, fut in self._sleepers):
            self._sleepers = [entry for entry in self._sleepers if not entry[2].done()]
            heapq.heapify(self._sleepers)

    async def wait_until(self, t: datetime, interrupt: asyncio.Event | None = None) -> None:
        if t > self._now:
            self._now = t
        self._prune()
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.set_result(None)

    async def sleep_until(self, t: datetime) -> None:
        if t <= self._now:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (t, self._seq, fut))
        await fut


class RealClock:
    """ss9 wall-clock domain. now() is NAIVE UTC (module docstring: DST must
    never run the oracle's non-decreasing feed discipline backwards; the 11c
    scheduler converts zoneinfo ticks to UTC instants). wait_until() sleeps
    in bounded chunks, waking early when `interrupt` fires (queue activity);
    sleep_until() is a plain sleep -- real sleeps wake themselves, so the
    engine never drives them (next_sleeper_due()/pending_sleepers() are the
    virtual domain's bookkeeping and stay empty here, DL-43 item 5)."""

    virtual = False

    #: cap one wait slice; re-checking hourly costs nothing and bounds drift
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
