"""Runner engine trace tests (phase 11a).

Normative spec: docs/runner-design.md ss3 (the two oracle additions), ss4
(engine loop, dispatch table, stale-completion gate), ss6 (adapters), ss9
(clock domains), ss13 (bisimulation is the acceptance gate). runner.py's own
module docstring pins the determinism rules these tests exercise: queued-
event-vs-timer ordering, the natural-exit-vs-kill race resolving
deterministically to the kill under VirtualClock, and the real-vs-virtual
scope split of the stale-completion gate.

Every expected outcome here was verified empirically against the real
oracle/runner before the assertion was written (CLAUDE.md: fidelity is
tested, not asserted) -- see the final report for anything that surprised
us or contradicted the design doc.

House style follows test_oracle.py: T0 = datetime(2026, 7, 1, 8, 0), naive
datetimes, an `ev()` helper, JIL text fixtures inline. Engine scenarios are
driven from one `async def` per test via a single `asyncio.run(...)` call --
adapter tasks must stay on one event loop across multiple
`run_until_quiescent` calls within a scenario. EngineHarness
(tests/bisim_harness.py) gives a synchronous Oracle-compatible facade for the
two tests that compare directly against Oracle-direct traces/emitted events.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from bisim_harness import EngineHarness
from hypothesis import given, settings
from hypothesis import strategies as st

from dsl41.ir import JobIR, lower_source
from dsl41.oracle import Event, EventKind, Oracle, OracleError
from dsl41.runner import AdapterContext, Engine, EngineError, FakeAdapter, VirtualClock

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


def transitions(o: Oracle, job: str) -> list[str]:
    return [t.transition for t in o.trace() if t.job == job]


class _RecordingAdapter:
    """Wraps a real adapter and records every (job, run_number) dispatch, so
    tests can prove an adapter task was -- or was NOT -- spawned (ss4
    dispatch table: BOX rows and ON_NOEXEC-bypassed starts get no dispatch)."""

    def __init__(self, inner: FakeAdapter) -> None:
        self.inner = inner
        self.calls: list[tuple[str, int]] = []

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int:
        self.calls.append((job_ir.name, run_number))
        return await self.inner.run(job_ir, run_number, ctx)


class _RaisingAdapter:
    """ss6 contract violation: an adapter whose run() raises. 11a defines no
    real-failure semantics (those arrive with real adapters in 11b) -- the
    exception must propagate loudly out of run_until_quiescent rather than
    being swallowed (Engine._settle: "adapter bug: fail loudly, never
    guess")."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int:
        raise RuntimeError("adapter exploded")


# --------------------------------------------------------- 1. next_timer_due()


def test_next_timer_due_none_then_start_deadline_then_earliest_of_two() -> None:
    """(runner-design ss3): next_timer_due() is a read-only peek at the timer
    heap. A fresh oracle has nothing armed -> None. Starting a term_run_time
    job arms its deadline (T0+5min), the sole timer. Starting a second job
    whose must_complete_times deadline lands nearer (T0+3min) must flip
    next_timer_due() to it -- proving a genuine min over the heap, not
    "whichever timer happens to exist"."""
    text = (
        "insert_job: ntd_a\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        "insert_job: ntd_b\njob_type: c\ncommand: y\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\nmust_complete_times: +2\n'
    )
    o = Oracle(lower_source(text))
    assert o.next_timer_due() is None
    o.feed(ev("STARTJOB", 0, job="ntd_a"))
    assert o.next_timer_due() == T0 + timedelta(minutes=5)
    o.feed(ev("STARTJOB", 1, job="ntd_b"))  # must_complete deadline: T0+1+2 = T0+3, nearer
    assert o.next_timer_due() == T0 + timedelta(minutes=3)


# ---------------------------------------------------------------- 2. advance()


def test_advance_fires_due_timer_identically_to_feed_drain() -> None:
    """(runner-design ss3/ss13): advance(now) fires a due term_run_time timer
    exactly as feed()'s lazy drain would. Two identical oracles from one
    catalog: one driven feed-only (a later dummy STATUS whose `at` passes the
    5-minute deadline), the other driven with an explicit advance() at the
    deadline before that same dummy feed -- traces must be identical."""
    text = (
        "insert_job: trt_job\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        "insert_job: dummy_trt\njob_type: c\ncommand: z\nmachine: m1\n"
    )
    o_feed = Oracle(lower_source(text))
    o_feed.feed(ev("STARTJOB", 0, job="trt_job"))
    o_feed.feed(ev("STATUS", 10, job="dummy_trt", status="SUCCESS"))

    o_advance = Oracle(lower_source(text))
    o_advance.feed(ev("STARTJOB", 0, job="trt_job"))
    o_advance.advance(T0 + timedelta(minutes=5))
    o_advance.feed(ev("STATUS", 10, job="dummy_trt", status="SUCCESS"))

    assert [t.model_dump() for t in o_feed.trace()] == [t.model_dump() for t in o_advance.trace()]


def test_advance_with_nothing_due_is_a_noop() -> None:
    """(runner-design ss3): advance() with no timer due returns [] and
    leaves the trace untouched."""
    o = Oracle(lower_source("insert_job: solo\njob_type: c\ncommand: x\nmachine: m1\n"))
    o.feed(ev("STARTJOB", 0, job="solo"))
    trace_before = [t.model_dump() for t in o.trace()]
    assert o.advance(T0 + timedelta(minutes=1)) == []
    assert [t.model_dump() for t in o.trace()] == trace_before


def test_advance_backwards_raises_oracle_error() -> None:
    """(runner-design ss3): advance()'s non-decreasing-time discipline mirrors
    feed()'s -- going backwards is refused loudly, never silently ignored."""
    o = Oracle(lower_source("insert_job: solo2\njob_type: c\ncommand: x\nmachine: m1\n"))
    o.feed(ev("STARTJOB", 0, job="solo2"))
    o.advance(T0 + timedelta(minutes=10))
    with pytest.raises(OracleError, match="backwards"):
        o.advance(T0 + timedelta(minutes=5))


def test_feed_before_a_later_advance_raises_oracle_error() -> None:
    """(runner-design ss3): advance(now) moves the oracle's clock to `now`
    just as feed(ev) moves it to ev.at -- a subsequent feed() at an earlier
    time is refused too, same discipline, same error family."""
    o = Oracle(lower_source("insert_job: solo3\njob_type: c\ncommand: x\nmachine: m1\n"))
    o.feed(ev("STARTJOB", 0, job="solo3"))
    o.advance(T0 + timedelta(minutes=10))
    with pytest.raises(OracleError, match="backwards"):
        o.feed(ev("STATUS", 5, job="solo3", status="SUCCESS"))


_TERM_ADVANCE_JIL = (
    "insert_job: haj\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
    "insert_job: hbj\njob_type: c\ncommand: y\nmachine: m1\ncondition: t(haj) | s(haj)\n"
)


@st.composite
def _random_status_script(draw: st.DrawFn) -> tuple[list[Event], float]:
    """Random monotone STATUS SUCCESS/FAILURE injections over haj (the
    term_run_time job) and hbj (a consumer whose condition references it),
    in the style of test_oracle.py's `_random_diamond_script`."""
    n = draw(st.integers(min_value=0, max_value=8))
    events: list[Event] = []
    minute = 0.0
    for _ in range(n):
        minute += draw(st.integers(min_value=0, max_value=5))
        job = draw(st.sampled_from(["haj", "hbj"]))
        status = draw(st.sampled_from(["SUCCESS", "FAILURE"]))
        events.append(ev("STATUS", minute, job=job, status=status))
    return events, minute


@given(_random_status_script())
@settings(max_examples=50, deadline=None)
def test_hypothesis_feed_only_vs_advance_interleaved_traces_identical(
    args: tuple[list[Event], float],
) -> None:
    """(runner-design ss13 point 2): random monotone scripts over a catalog
    with one term_run_time job and one consumer produce identical traces
    whether fed straight through, or with advance() calls to each event's
    `at` interleaved before it plus a final advance past the whole script.
    The feed-only arm flushes tail timers with a FEED of a late dummy event
    -- never advance() -- so the pinned feed-drain-vs-advance parity holds
    for tail timers too (a regression in advance()'s own drain cannot hit
    both arms identically and cancel out)."""
    script, last_minute = args
    flush_at = last_minute + 100

    o_feed = Oracle(lower_source(_TERM_ADVANCE_JIL))
    o_feed.feed(ev("STARTJOB", 0, job="haj"))
    for e in script:
        o_feed.feed(e)
    # feed-only tail flush: an undefined-job STATUS is a pure clock carrier
    # (SEM-06: undefined jobs never satisfy conditions; no trace entry for
    # consumers) -- empirically verified to add only its own trace entry
    o_feed.feed(ev("STATUS", flush_at, job="flush_tick", status="SUCCESS"))

    o_advance = Oracle(lower_source(_TERM_ADVANCE_JIL))
    o_advance.feed(ev("STARTJOB", 0, job="haj"))
    for e in script:
        assert o_advance._now is not None
        if e.at > o_advance._now:
            o_advance.advance(e.at)
        o_advance.feed(e)
    o_advance.advance(T0 + timedelta(minutes=flush_at))
    o_advance.feed(ev("STATUS", flush_at, job="flush_tick", status="SUCCESS"))

    assert [t.model_dump() for t in o_feed.trace()] == [t.model_dump() for t in o_advance.trace()]


# -------------------------------------------------------------- 3. VirtualClock


def test_virtual_clock_forward_only_time_and_sleeper_bookkeeping() -> None:
    """(runner-design ss9): wait_until moves `now` forward and a smaller `t`
    is a no-op; sleep_until in the past returns immediately without
    registering a sleeper; next_sleeper_due/pending_sleepers reflect
    registered sleeps and prune cancelled ones (VirtualClock._prune -- an
    engine-cancelled adapter leaves a dead future behind)."""

    async def scenario() -> None:
        clock = VirtualClock(start=T0)
        assert clock.now() == T0

        await clock.wait_until(T0 + timedelta(minutes=5))
        assert clock.now() == T0 + timedelta(minutes=5)
        await clock.wait_until(T0 + timedelta(minutes=1))  # smaller t: no-op
        assert clock.now() == T0 + timedelta(minutes=5)

        assert clock.pending_sleepers() == 0
        await clock.sleep_until(T0)  # already in the past relative to now
        assert clock.pending_sleepers() == 0  # never registered

        task = asyncio.create_task(clock.sleep_until(T0 + timedelta(minutes=100)))
        await asyncio.sleep(0)  # let the task register its sleeper
        assert clock.pending_sleepers() == 1
        assert clock.next_sleeper_due() == T0 + timedelta(minutes=100)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert clock.pending_sleepers() == 0  # pruned

    asyncio.run(scenario())


# --------------------------------------------------------- 4. Engine + FakeAdapter


def test_scripted_completion_cascades_to_consumer_end_to_end() -> None:
    """(runner-design ss4): job A scripted (duration 300s, exit 0), STARTJOB
    at T0 -> INACTIVE->STARTING->RUNNING at T0, RUNNING->SUCCESS at T0+5min;
    a consumer B (condition s(A)) auto-starts at T0+5min -- a cascade driven
    entirely by an adapter completion, not a script-injected STATUS."""

    async def scenario() -> None:
        text = (
            "insert_job: job_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
            "insert_job: job_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(job_a)\n"
        )
        adapter = FakeAdapter({("job_a", 1): (300.0, 0)})
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="job_a"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=10))

        assert transitions(engine.oracle, "job_a") == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
            "RUNNING->SUCCESS",
        ]
        assert engine.oracle.trace()[-2].at == T0 + timedelta(minutes=5)  # job_a SUCCESS
        assert transitions(engine.oracle, "job_b")[:2] == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
        ]
        job_b_start = next(t for t in engine.oracle.trace() if t.job == "job_b")
        assert job_b_start.at == T0 + timedelta(minutes=5)
        await engine.shutdown()

    asyncio.run(scenario())


def test_sem09_max_exit_success_boundary_stays_oracle_side_via_engine() -> None:
    """(runner-design ss4 DL-33 boundary): the engine feeds only a raw
    exit_code; SEM-09 classification stays the oracle's job. max_exit_success:
    2 -> exit 2 records SUCCESS, exit 3 (a second run) records FAILURE."""

    async def scenario() -> None:
        text = "insert_job: p9\njob_type: c\ncommand: x\nmachine: m1\nmax_exit_success: 2\n"
        adapter = FakeAdapter({("p9", 1): (60.0, 2), ("p9", 2): (60.0, 3)})
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="p9"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["p9"].status == "SUCCESS"
        assert engine.oracle.store.job["p9"].exit_code == 2

        engine.inject(ev("STARTJOB", 3, job="p9"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        assert engine.oracle.store.job["p9"].status == "FAILURE"
        assert engine.oracle.store.job["p9"].exit_code == 3
        await engine.shutdown()

    asyncio.run(scenario())


def test_default_instant_success_all_transitions_at_same_instant() -> None:
    """(runner-design ss6): FakeAdapter() with no script defaults to instant
    success -- STARTING/RUNNING/SUCCESS all trace at the same T0."""

    async def scenario() -> None:
        text = "insert_job: da\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter()
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="da"))
        await engine.run_until_quiescent(T0)
        trace = engine.oracle.trace()
        assert [t.transition for t in trace] == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
            "RUNNING->SUCCESS",
        ]
        assert all(t.at == T0 for t in trace)
        await engine.shutdown()

    asyncio.run(scenario())


def test_inert_adapter_parks_forever_job_stays_running() -> None:
    """(runner-design ss6): FakeAdapter(default=None) makes unscripted runs
    inert -- the task parks on sleep_until(datetime.max). Running to a far
    horizon (T0+1 day) leaves the job RUNNING with no completion and no
    drops."""

    async def scenario() -> None:
        text = "insert_job: ia\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter(default=None)
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="ia"))
        await engine.run_until_quiescent(T0 + timedelta(days=1))
        assert engine.oracle.store.job["ia"].status == "RUNNING"
        assert engine.drops == []
        await engine.shutdown()

    asyncio.run(scenario())


def test_killjob_mid_run_terminates_and_the_scripted_completion_never_lands() -> None:
    """(runner-design ss4; runner.py module docstring "virtual races resolve
    to the kill"): a completion scripted for T0+10min, KILLJOB injected at
    T0+5min -> TERMINATED at T0+5min. Running past T0+10min proves the
    cancelled adapter task's completion never enqueues: no further
    transitions, drops stays empty."""

    async def scenario() -> None:
        text = "insert_job: ka\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter({("ka", 1): (600.0, 0)})  # would complete at +10min
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="ka"))
        engine.inject(ev("KILLJOB", 5, job="ka"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=20))
        assert transitions(engine.oracle, "ka") == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
            "RUNNING->TERMINATED",
        ]
        assert engine.oracle.trace()[-1].at == T0 + timedelta(minutes=5)
        assert engine.drops == []
        await engine.shutdown()

    asyncio.run(scenario())


def test_term_run_time_auto_terminates_through_the_engine_advance_path() -> None:
    """(runner-design ss3/ss4): term_run_time: 5 with a completion scripted
    for +10min -> the oracle's own timer (fired via the engine's
    Oracle.advance() path, ss4 step 3) auto-TERMINATEs at T0+5min; the
    scripted completion never feeds; drops stays empty."""

    async def scenario() -> None:
        text = "insert_job: tb\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n"
        adapter = FakeAdapter({("tb", 1): (600.0, 0)})
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="tb"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=20))
        assert transitions(engine.oracle, "tb") == [
            "INACTIVE->STARTING",
            "STARTING->RUNNING",
            "RUNNING->TERMINATED",
        ]
        assert "term_run_time" in engine.oracle.trace()[-1].cause
        assert engine.oracle.trace()[-1].at == T0 + timedelta(minutes=5)
        assert engine.drops == []
        await engine.shutdown()

    asyncio.run(scenario())


def test_stale_completion_gate_run_number_mismatch_and_already_terminal() -> None:
    """(runner-design ss4 DL-41 decision 4): white-box test. Under
    VirtualClock the kill-vs-natural-exit race always resolves to the kill
    (runner.py module docstring), so the gate has no black-box trigger in
    11a -- it guards the REAL time domain (11b). We reach it here only by
    forging completions through the private `Engine._enqueue(..., is_completion=True)`
    path: a stale run_number is dropped with "run_number mismatch"; a
    current-run_number completion on an already-terminal job is dropped with
    "job already terminal". Both leave the trace untouched."""

    async def scenario() -> None:
        text = "insert_job: sa\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter({("sa", 1): (60.0, 0)})
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="sa"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["sa"].status == "SUCCESS"
        trace_before = [t.model_dump() for t in engine.oracle.trace()]

        engine._enqueue(
            Event(
                at=T0 + timedelta(minutes=3),
                kind="STATUS",
                payload={"job": "sa", "run_number": 0, "exit_code": 0},
            ),
            is_completion=True,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert len(engine.drops) == 1
        assert "run_number mismatch" in engine.drops[0][1]
        assert [t.model_dump() for t in engine.oracle.trace()] == trace_before

        engine._enqueue(
            Event(
                at=T0 + timedelta(minutes=4),
                kind="STATUS",
                payload={"job": "sa", "run_number": 1, "exit_code": 0},
            ),
            is_completion=True,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=4))
        assert len(engine.drops) == 2
        assert "already terminal" in engine.drops[1][1]
        assert [t.model_dump() for t in engine.oracle.trace()] == trace_before
        await engine.shutdown()

    asyncio.run(scenario())


def test_on_noexec_bypass_spawns_nothing() -> None:
    """(runner-design ss4): ON_NOEXEC's SEM-22 bypass emits SUCCESS without
    ever emitting STARTING, so nothing spawns "by construction" (module
    docstring). Proven with a recording adapter: it never sees a call."""

    async def scenario() -> None:
        text = "insert_job: ne\njob_type: c\ncommand: x\nmachine: m1\n"
        recorder = _RecordingAdapter(FakeAdapter())
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": recorder, "FW": recorder},
        )
        engine.inject(ev("ON_NOEXEC", 0, job="ne"))
        engine.inject(ev("STARTJOB", 1, job="ne"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["ne"].status == "SUCCESS"
        assert recorder.calls == []
        await engine.shutdown()

    asyncio.run(scenario())


def test_box_has_no_dispatch_row_only_the_member_spawns() -> None:
    """(runner-design ss4 dispatch table: "anything on a BOX -> none"): a box
    with one member -- starting the box spawns only the member's adapter
    task, never one for the box itself."""

    async def scenario() -> None:
        text = (
            "insert_job: bx\njob_type: b\n\n"
            "insert_job: mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: bx\n"
        )
        recorder = _RecordingAdapter(FakeAdapter())
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": recorder, "FW": recorder},
        )
        engine.inject(ev("STARTJOB", 0, job="bx"))
        await engine.run_until_quiescent(T0)
        assert recorder.calls == [("mem", 1)]
        await engine.shutdown()

    asyncio.run(scenario())


def test_horizon_discipline_time_only_moves_forward_across_calls() -> None:
    """(runner-design ss9): a completion scripted for T0+10min. Running to
    T0+5min leaves the job RUNNING (work due after the horizon waits); a
    SECOND run_until_quiescent to T0+15min on the same engine/loop then
    completes it at T0+10min -- time only moves forward across calls."""

    async def scenario() -> None:
        text = "insert_job: ha\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter({("ha", 1): (600.0, 0)})
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="ha"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))
        assert engine.oracle.store.job["ha"].status == "RUNNING"

        await engine.run_until_quiescent(T0 + timedelta(minutes=15))
        assert engine.oracle.store.job["ha"].status == "SUCCESS"
        assert engine.oracle.store.job["ha"].status_at == T0 + timedelta(minutes=10)
        await engine.shutdown()

    asyncio.run(scenario())


def test_adapter_exception_propagates_loudly() -> None:
    """(runner-design ss6/ss13 note 3): an adapter whose run() raises makes
    run_until_quiescent raise that same exception -- 11a defines no real
    failure semantics (those arrive with real adapters in 11b), so the
    engine must fail loudly rather than guess. The job is left RUNNING."""

    async def scenario() -> None:
        text = "insert_job: xa\njob_type: c\ncommand: x\nmachine: m1\n"
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _RaisingAdapter(), "FW": _RaisingAdapter()},
        )
        engine.inject(ev("STARTJOB", 0, job="xa"))
        with pytest.raises(RuntimeError, match="adapter exploded"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        assert engine.oracle.store.job["xa"].status == "RUNNING"
        await engine.shutdown()

    asyncio.run(scenario())


def test_engine_harness_feed_returns_same_emitted_events_as_oracle_feed() -> None:
    """(runner-design ss13): EngineHarness.feed and Oracle.feed must return
    the same emitted Event list for the same script. Exercised on a
    must_start_times scenario so a MUST_START_ALARM is actually armed and
    fired (the start is abandoned: SEM-32/Q3 default, condition false)."""
    text = (
        "insert_job: ms_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n'
        "must_start_times: +5\ncondition: s(ms_gate)\n\n"
        "insert_job: ms_gate\njob_type: c\ncommand: y\nmachine: m1\n\n"
        "insert_job: ms_dummy\njob_type: c\ncommand: z\nmachine: m1\n"
    )

    o = Oracle(lower_source(text))
    o.feed(ev("STARTJOB", 0, job="ms_job"))
    emitted_o = o.feed(ev("STATUS", 10, job="ms_dummy", status="SUCCESS"))

    harness = EngineHarness(lower_source(text))
    try:
        harness.feed(ev("STARTJOB", 0, job="ms_job"))
        emitted_h = harness.feed(ev("STATUS", 10, job="ms_dummy", status="SUCCESS"))
    finally:
        harness.close()

    # full model_dump: kind, payload AND at -- the facade must forward the
    # oracle's timestamps untouched (an 11b WAL path re-stamping events at
    # the wall-clock frontier would be invisible to a kind/payload compare)
    assert [e.model_dump() for e in emitted_o] == [e.model_dump() for e in emitted_h]
    assert any(e.kind == "MUST_START_ALARM" and e.job() == "ms_job" for e in emitted_o)


_ENGINE_BISIM_JIL = (
    "insert_job: ej_a\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
    "insert_job: ej_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(ej_a)\n\n"
    "insert_job: ej_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(ej_b) | v(FLAG) = go\n"
)
_ENGINE_BISIM_JOBS = ["ej_a", "ej_b", "ej_c"]


@st.composite
def _random_engine_script(draw: st.DrawFn) -> list[Event]:
    """Random monotone scripts over 3 jobs: injected STATUS across the FULL
    status vocabulary -- terminal SUCCESS/FAILURE/TERMINATED plus the
    non-terminal STARTING/RUNNING/INACTIVE overwrites (CHANGE_STATUS parity;
    the ghost-run gate's blind spot without them) -- plus SET_GLOBAL and
    STARTJOB on the term_run_time job, so engine timers actually fire."""
    n = draw(st.integers(min_value=0, max_value=8))
    events: list[Event] = []
    minute = 0.0
    for _ in range(n):
        minute += draw(st.integers(min_value=0, max_value=5))
        kind = draw(st.sampled_from(["STATUS", "SET_GLOBAL", "STARTJOB"]))
        if kind == "STATUS":
            job = draw(st.sampled_from(_ENGINE_BISIM_JOBS))
            status = draw(
                st.sampled_from(
                    ["SUCCESS", "FAILURE", "TERMINATED", "STARTING", "RUNNING", "INACTIVE"]
                )
            )
            events.append(ev("STATUS", minute, job=job, status=status))
        elif kind == "SET_GLOBAL":
            value = draw(st.sampled_from(["go", "stop"]))
            events.append(ev("SET_GLOBAL", minute, name="FLAG", value=value))
        else:
            events.append(ev("STARTJOB", minute, job="ej_a"))
    return events


@given(_random_engine_script())
@settings(max_examples=50, deadline=None)
def test_hypothesis_engine_bisimulation_startjob_and_term_run_time(script: list[Event]) -> None:
    """(runner-design ss13 points 1-2 combined): random monotone scripts
    (injected STATUS across the full vocabulary + SET_GLOBAL + STARTJOB,
    over a catalog including a term_run_time job so engine timers fire)
    through Oracle-direct vs EngineHarness -> identical traces."""
    o = Oracle(lower_source(_ENGINE_BISIM_JIL))
    for e in script:
        o.feed(e)

    harness = EngineHarness(lower_source(_ENGINE_BISIM_JIL))
    try:
        for e in script:
            harness.feed(e)
        assert [t.model_dump() for t in o.trace()] == [t.model_dump() for t in harness.trace()]
    finally:
        harness.close()


# ------------------------------------- 5. review-driven regressions (xhigh, DL-43)

# Engine defects confirmed by the phase-11a adversarial review; each test
# pins the corrected behavior so it cannot regress silently.


def test_zero_delay_cycle_raises_engine_error_instead_of_livelocking() -> None:
    """Review CONFIRMED: a condition cycle over instant completions (the
    AutoSys tight-loop pattern, L010, compressed to zero duration) generated
    unbounded work at one frozen virtual instant -- run_until_quiescent never
    returned. The engine now refuses with EngineError after a catalog-scaled
    same-instant event budget (runner.py frontier/guard docstring)."""
    text = (
        "insert_job: cyc_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cyc_b)\n\n"
        "insert_job: cyc_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cyc_a)\n"
    )

    async def scenario() -> None:
        adapter = FakeAdapter()  # default instant success: the rehearse default
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STATUS", 0, job="cyc_b", status="SUCCESS"))
        with pytest.raises(EngineError, match="zero-delay"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()

    asyncio.run(scenario())


def test_injected_status_starting_launches_no_ghost_run() -> None:
    """Review CONFIRMED: an injected CHANGE_STATUS-parity STATUS STARTING
    overwrite re-emits STARTING without a run_number bump and used to spawn
    a real adapter task whose completion rewrote the job to SUCCESS -- a run
    the semantics core never decided to start. Vendor parity: sendevent
    CHANGE_STATUS rewrites the DB status and launches nothing. Pinned:
    engine trace == oracle-direct trace (job stays STARTING forever), no
    live task, no completion, drops empty."""
    text = "insert_job: gj\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("gj", 0): (60.0, 0)}, default=None)
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STATUS", 0, job="gj", status="STARTING"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=5))

        o = Oracle(lower_source(text))
        o.feed(ev("STATUS", 0, job="gj", status="STARTING"))

        assert [t.model_dump() for t in engine.oracle.trace()] == [
            t.model_dump() for t in o.trace()
        ]
        assert engine.oracle.store.job["gj"].status == "STARTING"
        assert engine.drops == []
        await engine.shutdown()

    asyncio.run(scenario())


def test_negative_term_run_time_matches_oracle_direct_instead_of_crashing() -> None:
    """Review CONFIRMED: lowering accepts a negative term_run_time, arming a
    timer already in the past; oracle-direct tolerates it (feed back-dates
    the firing) but the engine crashed calling advance() backwards. The
    frontier rule now clamps: the past-due timer fires once time moves past
    the frontier, back-dated to its due time -- byte-identical to the
    oracle-direct trace."""
    text = "insert_job: neg\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: -5\n"

    harness = EngineHarness(lower_source(text))
    try:
        harness.feed(ev("STARTJOB", 0, job="neg"))
        assert harness.store.job["neg"].status == "RUNNING"  # lazy, like the oracle
        harness.feed(ev("STATUS", 5, job="tick_undef", status="SUCCESS"))
        engine_trace = [t.model_dump() for t in harness.trace()]
    finally:
        harness.close()

    o = Oracle(lower_source(text))
    o.feed(ev("STARTJOB", 0, job="neg"))
    assert o.store.job["neg"].status == "RUNNING"
    o.feed(ev("STATUS", 5, job="tick_undef", status="SUCCESS"))

    assert engine_trace == [t.model_dump() for t in o.trace()]


def test_zero_delta_deadline_stays_lazy_like_the_oracle() -> None:
    """Review CONFIRMED: a term_run_time of 0 arms a timer due exactly at
    the arming instant; the oracle's lazy discipline keeps it armed until
    the next feed, but the engine fired it eagerly at the same horizon --
    observably divergent store state at the same script point. The frontier
    rule pins laziness: RUNNING after the arming feed on BOTH paths,
    TERMINATED (back-dated to the due instant) after the next feed."""
    text = "insert_job: zd\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 0\n"

    harness = EngineHarness(lower_source(text))
    try:
        harness.feed(ev("STARTJOB", 0, job="zd"))
        mid_engine = harness.store.job["zd"].status
        harness.feed(ev("STATUS", 3, job="tick_undef", status="SUCCESS"))
        engine_trace = [t.model_dump() for t in harness.trace()]
    finally:
        harness.close()

    o = Oracle(lower_source(text))
    o.feed(ev("STARTJOB", 0, job="zd"))
    assert mid_engine == o.store.job["zd"].status == "RUNNING"
    o.feed(ev("STATUS", 3, job="tick_undef", status="SUCCESS"))

    assert engine_trace == [t.model_dump() for t in o.trace()]
    assert o.store.job["zd"].status == "TERMINATED"


class _TeardownBugAdapter:
    """An adapter whose cancellation path itself raises -- the 11b analog is
    a process-group kill or status-record failure during teardown, exactly
    the class of loss that must never vanish silently."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> int:
        try:
            await ctx.clock.sleep_until(datetime.max)
        finally:
            raise RuntimeError("teardown bug")


def test_cancellation_teardown_exceptions_are_not_swallowed() -> None:
    """Review CONFIRMED: shutdown() gathered cancelled tasks with
    return_exceptions=True and dropped anything they died with; the
    dispatch-cancel path popped the task from tracking entirely. Both paths
    now re-raise a non-CancelledError loudly (shutdown inspects the gather
    results; dispatch-cancelled tasks move to a reaping list that _settle
    collects)."""
    text = "insert_job: tb\njob_type: c\ncommand: x\nmachine: m1\n"

    async def shutdown_path() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _TeardownBugAdapter()},
        )
        engine.inject(ev("STARTJOB", 0, job="tb"))
        await engine.run_until_quiescent(T0)
        with pytest.raises(RuntimeError, match="teardown bug"):
            await engine.shutdown()

    async def cancel_path() -> None:
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": _TeardownBugAdapter()},
        )
        engine.inject(ev("STARTJOB", 0, job="tb"))
        engine.inject(ev("KILLJOB", 1, job="tb"))
        with pytest.raises(RuntimeError, match="teardown bug"):
            await engine.run_until_quiescent(T0 + timedelta(minutes=2))

    asyncio.run(shutdown_path())
    asyncio.run(cancel_path())


def test_shutdown_cancels_the_live_task_before_it_can_complete() -> None:
    """(runner-design ss4): shutdown() cancels every live adapter task.
    Proven black-box with a scripted (not inert) completion far in the
    future: start the job, shutdown() before the scripted completion time,
    then run past it -- if cancellation worked, the completion never
    enqueues (status stays RUNNING, drops stays empty, nothing emitted); a
    subsequent run_until_quiescent is a no-op returning []."""

    async def scenario() -> None:
        text = "insert_job: sd\njob_type: c\ncommand: x\nmachine: m1\n"
        adapter = FakeAdapter({("sd", 1): (86400.0, 0)})  # completes in 1 day
        engine = Engine(
            lower_source(text),
            clock=VirtualClock(start=T0),
            adapters={"CMD": adapter, "FW": adapter},
        )
        engine.inject(ev("STARTJOB", 0, job="sd"))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["sd"].status == "RUNNING"

        await engine.shutdown()

        out = await engine.run_until_quiescent(T0 + timedelta(days=2))
        assert out == []
        assert engine.oracle.store.job["sd"].status == "RUNNING"
        assert engine.drops == []

    asyncio.run(scenario())
