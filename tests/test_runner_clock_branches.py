"""Branch tests for src/dsl41/runner_clock.py: the real clock's bookkeeping
stubs and its plain (no interrupt) wait, which the engine tests never take."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from dsl41.runner_clock import RealClock


def test_real_clock_has_no_virtual_sleepers() -> None:
    clock = RealClock()
    assert clock.next_sleeper_due() is None
    assert clock.pending_sleepers() == 0


def test_real_clock_wait_without_interrupt_sleeps_until_the_deadline() -> None:
    clock = RealClock()
    deadline = clock.now() + timedelta(milliseconds=30)
    asyncio.run(clock.wait_until(deadline))
    assert clock.now() >= deadline


def test_real_clock_wait_without_interrupt_re_slices_a_long_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait longer than one slice sleeps in slices, none longer than the cap, and ends at
    the deadline."""
    slices: list[float] = []
    real_sleep = asyncio.sleep

    async def counting_sleep(seconds: float) -> None:
        slices.append(seconds)
        await real_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    monkeypatch.setattr(RealClock, "_MAX_SLICE_S", 0.01)
    clock = RealClock()
    deadline = clock.now() + timedelta(milliseconds=60)
    asyncio.run(clock.wait_until(deadline))
    assert clock.now() >= deadline
    assert len(slices) > 1
    assert max(slices) <= 0.01
