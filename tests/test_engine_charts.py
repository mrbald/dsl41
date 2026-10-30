"""The engine lifecycle and the attempt as statecharts (src/dsl41/engine_charts.py).

Both charts are well formed and draw no warning. The generated completeness test runs every
witness configuration by every event and payload partition, and a table written from the drain
design, not from the rows, pins each cell. The scenarios drive the charts by events only, with a
bench that plays the tier: it posts events, carries no clock, and records the rows and the intents
in order. Assertions are on that order and never on elapsed time.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41.engine_charts import (
    ABORT_DEADLINE,
    ALL_ATTEMPTS_SETTLED,
    ARM_ABORT_DEADLINE,
    ARM_RECORD_TIMEOUT,
    ATTEMPT,
    CANCEL_RECORD_TIMEOUT,
    CANCEL_WATCHERS,
    ENGINE_KEY,
    ENGINE_LIFECYCLE,
    ENGINE_STOP,
    EXIT_PROCESS,
    EXIT_RECORDED,
    JOURNAL_DRAINING,
    JOURNAL_SHUTDOWN_DURING_STARTUP,
    RECORD_CLEAN_SHUTDOWN,
    RECORD_OUTCOME,
    RECORD_TIMEOUT,
    RECORD_UNCLEAN_SHUTDOWN,
    REPLAY_DONE,
    SPAWN_FAILED,
    SPAWN_RECORDED,
    STOP,
    STOP_ATTEMPT,
    STOP_ATTEMPTS,
    STOP_REQUESTED,
    UPDATE_STOP,
    WRAPPER_GONE,
    AttemptOutcome,
    AttemptRecord,
    DrainRequest,
    ExitRecord,
    Lifecycle,
    outcome_of,
)
from dsl41.state_machine import (
    Executor,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

ENGINE_STATES = ("STARTING", "RUNNING", "DRAINING", "STOPPED")
ATTEMPT_STATES = ("SPAWNING", "RUNNING", "STOPPING", "ENDED")
#: the graces and the time of a stop request in the scenarios
REQUEST = DrainRequest(now=100.0, grace_term_s=10.0, grace_kill_s=5.0)
# the spec's deadline: now + grace_term_s + grace_kill_s + 5 s = 100 + 10 + 5 + 5
DEADLINE = 120.0
#: the wrapper's records: a command that ended by itself, one the adapter's ladder killed, one the
#: wrapper killed on lifeline EOF, and a launch the wrapper aborted
NATURAL = ExitRecord(
    exit_code=0, started_at="2026-10-30T01:00:00Z", ended_at="2026-10-30T01:00:05Z"
)
FAILED = ExitRecord(exit_code=3)
BY_STOP = ExitRecord(signal=15, terminated_by="stop")
BY_LIFELINE = ExitRecord(signal=15, terminated_by="lifeline")
BY_WRAPPER = ExitRecord(terminated_by="wrapper")


@pytest.mark.parametrize("chart", [ENGINE_LIFECYCLE, ATTEMPT], ids=lambda c: c.name)
def test_the_charts_are_well_formed_and_draw_no_warning(chart: Any) -> None:
    assert well_formed(chart) == []
    assert chart_warnings(chart) == []
    assert chart.clock == "logical"


def test_the_states_and_events_are_the_designs_and_an_update_has_no_engine_event() -> None:
    assert [r.id for r in ENGINE_LIFECYCLE.rows] == [f"engine_lifecycle.0{n}" for n in range(1, 7)]
    assert alphabet(ENGINE_LIFECYCLE) == sorted(
        [REPLAY_DONE, STOP_REQUESTED, ALL_ATTEMPTS_SETTLED, ABORT_DEADLINE]
    )
    assert alphabet(ATTEMPT) == sorted(
        [SPAWN_RECORDED, SPAWN_FAILED, STOP, EXIT_RECORDED, WRAPPER_GONE, RECORD_TIMEOUT]
    )
    assert not any("update" in event for event in alphabet(ENGINE_LIFECYCLE))
    assert ENGINE_LIFECYCLE.guards == {} and ATTEMPT.guards == {}


# ---- the bench: the tier's part, with no clock and no process


@dataclass
class Bench:
    """One executor, the lifecycle instance and the attempts, and the ordered record."""

    executor: Executor = field(default_factory=Executor)
    log: list[tuple[Any, ...]] = field(default_factory=list)

    def send(
        self, key: Hashable, name: str, payload: Any = None, generation: int | None = None
    ) -> None:
        for outcome in self.executor.dispatch(key, ChartEvent(name, payload, generation)):
            if outcome.kind == "discarded":
                self.log.append(("discarded", key, name))
            for row in outcome.rows:
                self.log.append(("row", key, row))
            if outcome.kind == "fired" and not outcome.rows:
                self.log.append(("implicit ignore", key, outcome.event.name))
            for intent in outcome.intents:
                self.log.append(("intent", key, intent.name, intent.detail))

    def state(self, key: Hashable, states: tuple[str, ...]) -> str:
        (leaf,) = self.executor.active(key) & set(states)
        return leaf

    def engine(self, to: str = "RUNNING") -> Lifecycle:
        life = Lifecycle()
        self.executor.start(ENGINE_KEY, ENGINE_LIFECYCLE, life)
        self.executor.run()
        if to != "STARTING":
            self.send(ENGINE_KEY, REPLAY_DONE)
        return life

    def attempt(self, key: str, to: str = "RUNNING") -> AttemptRecord:
        record = AttemptRecord()
        self.executor.start(key, ATTEMPT, record)
        self.executor.run()
        if to == "RUNNING":
            self.send(key, SPAWN_RECORDED)
        return record

    def generation(self, key: str) -> int:
        """The generation of `record_timeout` the chart holds: what the tier read when it armed."""
        return self.executor.generation(key, RECORD_TIMEOUT)

    def timeout(self, key: str, generation: int) -> None:
        self.send(key, RECORD_TIMEOUT, generation=generation)

    def engine_state(self) -> str:
        return self.state(ENGINE_KEY, ENGINE_STATES)

    def attempt_state(self, key: str) -> str:
        return self.state(key, ATTEMPT_STATES)

    def mark(self) -> int:
        return len(self.log)

    def since(self, mark: int) -> list[tuple[Any, ...]]:
        return self.log[mark:]


def _drain(bench: Bench, keys: list[str]) -> list[str]:
    """Post `stop_requested`, then play the wiring: send `stop` to every unsettled attempt.
    Answers the attempts that were told."""
    bench.send(ENGINE_KEY, STOP_REQUESTED, REQUEST)
    told = [k for k in keys if bench.attempt_state(k) != "ENDED"]
    for key in told:
        bench.send(key, STOP, ENGINE_STOP)
    return told


# ---- the abstraction (statechart-engine §13)


def _engine_witness(state: str) -> sc.Builder:
    def build() -> tuple[Any, Any]:
        bench = Bench()
        bench.engine("STARTING")
        if state != "STARTING":
            bench.send(ENGINE_KEY, REPLAY_DONE)
        if state in ("DRAINING", "STOPPED"):
            bench.send(ENGINE_KEY, STOP_REQUESTED, REQUEST)
        if state == "STOPPED":
            bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
        return bench.executor, ENGINE_KEY

    return build


def _attempt_witness(state: str) -> sc.Builder:
    def build() -> tuple[Any, Any]:
        bench = Bench()
        bench.attempt("run", "SPAWNING")
        if state == "ENDED":
            bench.send("run", SPAWN_FAILED)
        if state in ("RUNNING", "STOPPING"):
            bench.send("run", SPAWN_RECORDED)
        if state == "STOPPING":
            bench.send("run", STOP, ENGINE_STOP)
        return bench.executor, "run"

    return build


ENGINE_ABSTRACTION = sc.Abstraction(
    witnesses={state: _engine_witness(state) for state in ENGINE_STATES},
    partitions={
        STOP_REQUESTED: {"request": REQUEST},
        ABORT_DEADLINE: {"two_unsettled": ("a", "b"), "none_unsettled": ()},
    },
)
ATTEMPT_ABSTRACTION = sc.Abstraction(
    witnesses={state: _attempt_witness(state) for state in ATTEMPT_STATES},
    partitions={
        STOP: {"engine_stop": ENGINE_STOP, "update_stop": UPDATE_STOP},
        EXIT_RECORDED: {
            "natural": NATURAL,
            "by_stop": BY_STOP,
            "by_lifeline": BY_LIFELINE,
            "by_wrapper": BY_WRAPPER,
        },
    },
)

#: Each cell the drain design gives: the cell value, the row's target and its action. The row's
#: source is the witness and its event is the cell's event.
type Design = dict[str, dict[str, tuple[str, str, str]]]
ENGINE_DESIGN: Design = {
    "STARTING": {
        REPLAY_DONE: ("engine_lifecycle.01", "RUNNING", ""),
        STOP_REQUESTED: ("engine_lifecycle.06", "STOPPED", "shutdown_during_startup"),
    },
    "RUNNING": {STOP_REQUESTED: ("engine_lifecycle.02", "DRAINING", "begin_drain")},
    "DRAINING": {
        STOP_REQUESTED: ("engine_lifecycle.03", "", "deadline_now"),
        ALL_ATTEMPTS_SETTLED: ("engine_lifecycle.04", "STOPPED", "clean_shutdown"),
        ABORT_DEADLINE: ("engine_lifecycle.05", "STOPPED", "unclean_shutdown"),
    },
    "STOPPED": {event: ("ignored in STOPPED", "", "") for event in alphabet(ENGINE_LIFECYCLE)},
}
ATTEMPT_DESIGN: Design = {
    "SPAWNING": {
        SPAWN_RECORDED: ("attempt.01", "RUNNING", ""),
        SPAWN_FAILED: ("attempt.02", "ENDED", "end_spawn_failed"),
        STOP: ("attempt.03", "STOPPING", "stop"),
        EXIT_RECORDED: ("attempt.11", "ENDED", "end_from_record"),
        WRAPPER_GONE: ("attempt.12", "ENDED", "end_unobservable"),
    },
    "RUNNING": {
        STOP: ("attempt.04", "STOPPING", "stop"),
        EXIT_RECORDED: ("attempt.05", "ENDED", "end_from_record"),
        WRAPPER_GONE: ("attempt.13", "ENDED", "end_unobservable"),
    },
    "STOPPING": {
        STOP: ("ignored attempt.08", "", ""),
        EXIT_RECORDED: ("attempt.06", "ENDED", "end_from_record"),
        RECORD_TIMEOUT: ("attempt.07", "ENDED", "end_unobservable"),
        SPAWN_RECORDED: ("attempt.09", "", "stop_attempt"),
        SPAWN_FAILED: ("attempt.10", "ENDED", "end_spawn_failed"),
        WRAPPER_GONE: ("attempt.14", "ENDED", "end_unobservable"),
    },
    "ENDED": {event: ("ignored in ENDED", "", "") for event in alphabet(ATTEMPT)},
}


def _pin(
    chart: Any,
    table: dict[tuple[str, str, str], str],
    design: Design,
    partitions: dict[str, int],
) -> None:
    """Every cell the table holds is the design's: its value, and the row it names has the
    design's source, event, target and action. A stale generation is discarded. The design has
    no cell the table lacks."""
    rows = {row.id: row for row in chart.rows}
    for (witness, event, label), value in table.items():
        cell, target, action = design[witness][event]
        if label.endswith(sc.STALE):
            assert value == "discarded", (witness, event, label)
            continue
        assert value == cell, (witness, event, label)
        if cell.startswith("ignored in"):
            continue
        row = rows[cell.removeprefix("ignored ")]
        assert (row.source, row.event, row.target, row.action) == (witness, event, target, action)
    assert {(w, e) for w, e, _ in table} == {(w, e) for w, events in design.items() for e in events}
    assert len(table) == sum(partitions.get(e, 1) for events in design.values() for e in events)


def test_the_engine_completeness_table_is_the_drain_design() -> None:
    table = sc.completeness_table(ENGINE_LIFECYCLE, ENGINE_ABSTRACTION)
    _pin(ENGINE_LIFECYCLE, table, ENGINE_DESIGN, {ABORT_DEADLINE: 2})


def test_the_attempt_completeness_table_is_the_drain_design() -> None:
    table = sc.completeness_table(ATTEMPT, ATTEMPT_ABSTRACTION)
    # a record has four partitions; the re-armable timer has the current generation and a stale one
    _pin(ATTEMPT, table, ATTEMPT_DESIGN, {EXIT_RECORDED: 4, STOP: 2, RECORD_TIMEOUT: 2})
    assert table[("STOPPING", RECORD_TIMEOUT, "any/stale")] == "discarded"
    assert ("RUNNING", RECORD_TIMEOUT, "any") not in table


@pytest.mark.parametrize(
    ("chart", "witness", "abstraction"),
    [
        (ENGINE_LIFECYCLE, _engine_witness, ENGINE_ABSTRACTION),
        (ATTEMPT, _attempt_witness, ATTEMPT_ABSTRACTION),
    ],
    ids=["engine_lifecycle", "attempt"],
)
def test_an_excluded_cell_raises_rather_than_being_ignored(
    chart: Any, witness: Any, abstraction: sc.Abstraction
) -> None:
    assert chart.exclusions
    for exclusion in chart.exclusions:
        executor, key = witness(exclusion.state)()
        generation = executor.generation(key, exclusion.event)
        with pytest.raises(UnhandledEvent):
            executor.dispatch(key, ChartEvent(exclusion.event, generation=generation))


# ---- the engine: drain


def test_a_stop_request_during_startup_exits_0_with_nothing_to_drain() -> None:
    bench = Bench()
    bench.engine("STARTING")
    mark = bench.mark()
    bench.send(ENGINE_KEY, STOP_REQUESTED, REQUEST)
    assert bench.engine_state() == "STOPPED"
    assert bench.since(mark) == [
        ("row", ENGINE_KEY, "engine_lifecycle.06"),
        ("intent", ENGINE_KEY, JOURNAL_SHUTDOWN_DURING_STARTUP, None),
        ("intent", ENGINE_KEY, EXIT_PROCESS, 0),
    ]


def test_a_drain_with_no_attempts_stops_clean_and_exits_0() -> None:
    bench = Bench()
    bench.engine()
    mark = bench.mark()
    assert _drain(bench, []) == []
    assert bench.engine_state() == "DRAINING"
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    assert bench.engine_state() == "STOPPED"
    assert bench.since(mark) == [
        ("row", ENGINE_KEY, "engine_lifecycle.02"),
        ("intent", ENGINE_KEY, JOURNAL_DRAINING, None),
        ("intent", ENGINE_KEY, STOP_ATTEMPTS, ENGINE_STOP),
        ("intent", ENGINE_KEY, CANCEL_WATCHERS, None),
        ("intent", ENGINE_KEY, ARM_ABORT_DEADLINE, DEADLINE),
        ("row", ENGINE_KEY, "engine_lifecycle.04"),
        ("intent", ENGINE_KEY, RECORD_CLEAN_SHUTDOWN, None),
        ("intent", ENGINE_KEY, EXIT_PROCESS, 0),
    ]


def test_a_drain_with_one_running_attempt_waits_for_its_record() -> None:
    bench = Bench()
    life = bench.engine()
    record = bench.attempt("a")
    mark = bench.mark()
    assert _drain(bench, ["a"]) == ["a"]
    assert life.deadline == DEADLINE
    assert (bench.engine_state(), bench.attempt_state("a")) == ("DRAINING", "STOPPING")
    bench.send("a", EXIT_RECORDED, BY_STOP)
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    stopped = AttemptOutcome("stopped", cause=ENGINE_STOP, record=BY_STOP)
    assert record.outcome == stopped
    assert bench.since(mark) == [
        ("row", ENGINE_KEY, "engine_lifecycle.02"),
        ("intent", ENGINE_KEY, JOURNAL_DRAINING, None),
        ("intent", ENGINE_KEY, STOP_ATTEMPTS, ENGINE_STOP),
        ("intent", ENGINE_KEY, CANCEL_WATCHERS, None),
        ("intent", ENGINE_KEY, ARM_ABORT_DEADLINE, DEADLINE),
        ("row", "a", "attempt.04"),
        ("intent", "a", STOP_ATTEMPT, ENGINE_STOP),
        ("intent", "a", ARM_RECORD_TIMEOUT, None),
        ("row", "a", "attempt.06"),
        ("intent", "a", RECORD_OUTCOME, stopped),
        ("intent", "a", CANCEL_RECORD_TIMEOUT, None),
        ("row", ENGINE_KEY, "engine_lifecycle.04"),
        ("intent", ENGINE_KEY, RECORD_CLEAN_SHUTDOWN, None),
        ("intent", ENGINE_KEY, EXIT_PROCESS, 0),
    ]


def test_a_drain_with_twenty_attempts_in_three_states_settles_every_one() -> None:
    bench = Bench()
    bench.engine()
    keys = [f"job{n:02d}" for n in range(20)]
    records = {}
    for n, key in enumerate(keys):
        records[key] = bench.attempt(key, "SPAWNING" if n % 3 == 0 else "RUNNING")
        if n % 3 == 2:
            bench.send(key, STOP, UPDATE_STOP)
    before = {key: bench.attempt_state(key) for key in keys}
    assert sorted(set(before.values())) == ["RUNNING", "SPAWNING", "STOPPING"]
    mark = bench.mark()
    assert _drain(bench, keys) == keys
    after_drain = bench.since(mark)
    assert after_drain[0] == ("row", ENGINE_KEY, "engine_lifecycle.02")
    # a stopping attempt takes the second stop as an ignored row, and keeps the first cause
    ignored = [e for e in after_drain if e[0] == "row" and e[2] == "attempt.08"]
    stopping = {k for k, v in before.items() if v == "STOPPING"}
    assert len(ignored) == len(stopping)
    assert {e[1] for e in ignored} == stopping
    for key in keys:
        bench.send(key, EXIT_RECORDED, BY_STOP)
    assert all(bench.attempt_state(key) == "ENDED" for key in keys)
    assert bench.engine_state() == "DRAINING"
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    assert bench.engine_state() == "STOPPED"
    for key, state in before.items():
        cause = UPDATE_STOP if state == "STOPPING" else ENGINE_STOP
        assert records[key].outcome == AttemptOutcome("stopped", cause=cause, record=BY_STOP)
    assert bench.log[-3:] == [
        ("row", ENGINE_KEY, "engine_lifecycle.04"),
        ("intent", ENGINE_KEY, RECORD_CLEAN_SHUTDOWN, None),
        ("intent", ENGINE_KEY, EXIT_PROCESS, 0),
    ]


def test_a_second_stop_request_moves_the_deadline_to_now_and_stays_in_draining() -> None:
    bench = Bench()
    life = bench.engine()
    bench.attempt("a")
    _drain(bench, ["a"])
    assert life.deadline == DEADLINE
    mark = bench.mark()
    bench.send(ENGINE_KEY, STOP_REQUESTED, DrainRequest(now=103.0, grace_term_s=10, grace_kill_s=5))
    assert bench.engine_state() == "DRAINING"
    assert life.deadline == 103.0
    assert bench.since(mark) == [
        ("row", ENGINE_KEY, "engine_lifecycle.03"),
        ("intent", ENGINE_KEY, ARM_ABORT_DEADLINE, 103.0),
    ]
    assert bench.attempt_state("a") == "STOPPING"


def test_the_deadline_stops_the_engine_unclean_and_exits_0() -> None:
    bench = Bench()
    bench.engine()
    bench.attempt("a")
    bench.attempt("b")
    _drain(bench, ["a", "b"])
    bench.send("b", EXIT_RECORDED, BY_STOP)
    mark = bench.mark()
    bench.send(ENGINE_KEY, ABORT_DEADLINE, ("a",))
    assert bench.engine_state() == "STOPPED"
    assert bench.since(mark) == [
        ("row", ENGINE_KEY, "engine_lifecycle.05"),
        ("intent", ENGINE_KEY, RECORD_UNCLEAN_SHUTDOWN, ("a",)),
        ("intent", ENGINE_KEY, EXIT_PROCESS, 0),
    ]


def test_a_settled_drain_ignores_a_late_deadline_and_a_late_settle() -> None:
    bench = Bench()
    bench.engine()
    _drain(bench, [])
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    mark = bench.mark()
    bench.send(ENGINE_KEY, ABORT_DEADLINE, ())
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    bench.send(ENGINE_KEY, STOP_REQUESTED, REQUEST)
    assert bench.since(mark) == [
        ("implicit ignore", ENGINE_KEY, ABORT_DEADLINE),
        ("implicit ignore", ENGINE_KEY, ALL_ATTEMPTS_SETTLED),
        ("implicit ignore", ENGINE_KEY, STOP_REQUESTED),
    ]


def test_the_deadline_wins_when_it_comes_first_and_the_settle_after_it_is_ignored() -> None:
    bench = Bench()
    bench.engine()
    bench.attempt("a")
    _drain(bench, ["a"])
    bench.send(ENGINE_KEY, ABORT_DEADLINE, ("a",))
    bench.send("a", EXIT_RECORDED, BY_STOP)
    mark = bench.mark()
    bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
    assert bench.since(mark) == [("implicit ignore", ENGINE_KEY, ALL_ATTEMPTS_SETTLED)]


# ---- the attempt


def _ending(outcome: AttemptOutcome) -> list[tuple[Any, ...]]:
    """The two intents every ENDED row emits, in order."""
    return [
        ("intent", "a", RECORD_OUTCOME, outcome),
        ("intent", "a", CANCEL_RECORD_TIMEOUT, None),
    ]


def test_a_stop_during_spawning_kills_the_wrapper_and_ends_stopped() -> None:
    bench = Bench()
    record = bench.attempt("a", "SPAWNING")
    mark = bench.mark()
    bench.send("a", STOP, ENGINE_STOP)
    assert bench.attempt_state("a") == "STOPPING"
    bench.send("a", EXIT_RECORDED, BY_STOP)
    stopped = AttemptOutcome("stopped", cause=ENGINE_STOP, record=BY_STOP)
    assert record.outcome == stopped
    assert bench.since(mark) == [
        ("row", "a", "attempt.03"),
        ("intent", "a", STOP_ATTEMPT, ENGINE_STOP),
        ("intent", "a", ARM_RECORD_TIMEOUT, None),
        ("row", "a", "attempt.06"),
        *_ending(stopped),
    ]


def test_a_failed_spawn_ends_with_the_outcome_spawn_failed_and_its_error() -> None:
    bench = Bench()
    record = bench.attempt("a", "SPAWNING")
    mark = bench.mark()
    bench.send("a", SPAWN_FAILED, "cannot open stdout")
    failed = AttemptOutcome("spawn_failed", error="cannot open stdout")
    assert bench.attempt_state("a") == "ENDED"
    assert record.outcome == failed
    assert bench.since(mark) == [("row", "a", "attempt.02"), *_ending(failed)]


def test_a_record_in_spawning_ends_the_attempt_from_the_record() -> None:
    # the wrapper launched, could not write spawn.json, killed the command and recorded
    bench = Bench()
    record = bench.attempt("a", "SPAWNING")
    mark = bench.mark()
    bench.send("a", EXIT_RECORDED, BY_WRAPPER)
    failed = AttemptOutcome("spawn_failed", record=BY_WRAPPER)
    assert bench.attempt_state("a") == "ENDED"
    assert record.outcome == failed
    assert bench.since(mark) == [("row", "a", "attempt.11"), *_ending(failed)]


def test_a_natural_exit_of_a_running_attempt_carries_the_record() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", EXIT_RECORDED, FAILED)
    assert record.outcome == AttemptOutcome("natural", record=FAILED)
    assert bench.attempt_state("a") == "ENDED"


def test_a_lifeline_record_in_a_running_attempt_is_stopped_not_natural() -> None:
    # a recovered attempt rests in RUNNING; the wrapper killed its command when the engine died
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", EXIT_RECORDED, BY_LIFELINE)
    assert record.outcome == AttemptOutcome("stopped", cause="lifeline", record=BY_LIFELINE)


@pytest.mark.parametrize(
    ("exit_record", "cause", "expected"),
    [
        (NATURAL, None, AttemptOutcome("natural", record=NATURAL)),
        (NATURAL, ENGINE_STOP, AttemptOutcome("natural", record=NATURAL)),
        (BY_LIFELINE, None, AttemptOutcome("stopped", cause="lifeline", record=BY_LIFELINE)),
        (BY_STOP, UPDATE_STOP, AttemptOutcome("stopped", cause=UPDATE_STOP, record=BY_STOP)),
        (BY_STOP, None, AttemptOutcome("stopped", cause="stop", record=BY_STOP)),
        (BY_WRAPPER, None, AttemptOutcome("spawn_failed", record=BY_WRAPPER)),
    ],
)
def test_the_outcome_is_derived_from_the_record_in_one_place(
    exit_record: ExitRecord, cause: str | None, expected: AttemptOutcome
) -> None:
    assert outcome_of(exit_record, cause) == expected


def test_a_command_that_exited_before_the_stop_ends_natural() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", STOP, UPDATE_STOP)
    mark = bench.mark()
    bench.send("a", EXIT_RECORDED, NATURAL)
    done = AttemptOutcome("natural", record=NATURAL)
    assert record.outcome == done
    assert bench.since(mark) == [("row", "a", "attempt.06"), *_ending(done)]


@pytest.mark.parametrize("state", ["SPAWNING", "RUNNING", "STOPPING"])
def test_a_wrapper_that_exits_without_a_record_ends_unobservable(state: str) -> None:
    bench = Bench()
    record = bench.attempt("a", "SPAWNING" if state == "SPAWNING" else "RUNNING")
    if state == "STOPPING":
        bench.send("a", STOP, ENGINE_STOP)
    assert bench.attempt_state("a") == state
    mark = bench.mark()
    bench.send("a", WRAPPER_GONE)
    # the outcome keeps the cause of the stop the attempt took
    unobservable = AttemptOutcome("unobservable", cause=record.cause)
    assert record.outcome == unobservable
    rows = {"SPAWNING": "attempt.12", "RUNNING": "attempt.13", "STOPPING": "attempt.14"}
    assert bench.since(mark) == [("row", "a", rows[state]), *_ending(unobservable)]


def test_a_second_stop_keeps_the_first_cause_and_runs_no_second_ladder() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", STOP, UPDATE_STOP)
    mark = bench.mark()
    bench.send("a", STOP, ENGINE_STOP)
    assert bench.since(mark) == [("row", "a", "attempt.08")]
    assert record.cause == UPDATE_STOP
    bench.send("a", EXIT_RECORDED, BY_STOP)
    assert record.outcome == AttemptOutcome("stopped", cause=UPDATE_STOP, record=BY_STOP)


def test_a_spawn_recorded_during_stopping_runs_the_ladder_again_and_re_arms_the_timer() -> None:
    bench = Bench()
    record = bench.attempt("a", "SPAWNING")
    bench.send("a", STOP, ENGINE_STOP)
    first = bench.generation("a")
    mark = bench.mark()
    bench.send("a", SPAWN_RECORDED)
    second = bench.generation("a")
    assert second == first + 1
    assert bench.attempt_state("a") == "STOPPING"
    assert bench.since(mark) == [
        ("row", "a", "attempt.09"),
        ("intent", "a", STOP_ATTEMPT, ENGINE_STOP),
        ("intent", "a", ARM_RECORD_TIMEOUT, None),
    ]
    # the first ladder's timer fires mid-ladder: it is stale and does nothing
    mark = bench.mark()
    bench.timeout("a", first)
    assert bench.since(mark) == [("discarded", "a", RECORD_TIMEOUT)]
    assert bench.attempt_state("a") == "STOPPING"
    assert record.outcome is None
    bench.timeout("a", second)
    assert record.outcome == AttemptOutcome("unobservable", cause=ENGINE_STOP)


def test_a_failed_spawn_during_stopping_ends_spawn_failed() -> None:
    bench = Bench()
    record = bench.attempt("a", "SPAWNING")
    bench.send("a", STOP, UPDATE_STOP)
    mark = bench.mark()
    bench.send("a", SPAWN_FAILED, "cannot open stdin")
    failed = AttemptOutcome("spawn_failed", error="cannot open stdin")
    assert bench.attempt_state("a") == "ENDED"
    assert record.outcome == failed
    assert bench.since(mark) == [("row", "a", "attempt.10"), *_ending(failed)]


def test_a_record_after_the_record_timeout_is_ignored_and_the_outcome_stands() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", STOP, ENGINE_STOP)
    generation = bench.generation("a")
    mark = bench.mark()
    bench.timeout("a", generation)
    unobservable = AttemptOutcome("unobservable", cause=ENGINE_STOP)
    assert record.outcome == unobservable
    bench.send("a", EXIT_RECORDED, BY_STOP)
    bench.timeout("a", generation)
    assert bench.attempt_state("a") == "ENDED"
    assert record.outcome == unobservable
    assert bench.since(mark) == [
        ("row", "a", "attempt.07"),
        *_ending(unobservable),
        ("implicit ignore", "a", EXIT_RECORDED),
        ("implicit ignore", "a", RECORD_TIMEOUT),
    ]


def test_a_stop_then_a_kill_record_ends_stopped_with_the_cause() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", STOP, ENGINE_STOP)
    assert bench.attempt_state("a") == "STOPPING"
    bench.send("a", EXIT_RECORDED, ExitRecord(signal=9, terminated_by="stop"))
    assert record.outcome == AttemptOutcome(
        "stopped", cause=ENGINE_STOP, record=ExitRecord(signal=9, terminated_by="stop")
    )


def test_a_stop_then_a_record_timeout_ends_unobservable() -> None:
    bench = Bench()
    record = bench.attempt("a")
    bench.send("a", STOP, ENGINE_STOP)
    bench.timeout("a", bench.generation("a"))
    assert record.outcome == AttemptOutcome("unobservable", cause=ENGINE_STOP)


# ---- an update stop: the engine chart is not involved


def test_update_stop_running_stops_two_of_twenty_and_leaves_the_engine_running() -> None:
    bench = Bench()
    bench.engine()
    keys = [f"job{n:02d}" for n in range(20)]
    records = {key: bench.attempt(key) for key in keys}
    mark = bench.mark()
    touched = keys[3:5]
    for key in touched:
        bench.send(key, STOP, UPDATE_STOP)
        assert bench.engine_state() == "RUNNING"
    for key in touched:
        bench.send(key, EXIT_RECORDED, BY_STOP)
        assert bench.engine_state() == "RUNNING"
    assert {e[1] for e in bench.since(mark)} == set(touched)
    assert not any(e[1] == ENGINE_KEY for e in bench.since(mark))
    for key in touched:
        assert bench.attempt_state(key) == "ENDED"
        assert records[key].outcome == AttemptOutcome("stopped", cause=UPDATE_STOP, record=BY_STOP)
    running = [k for k in keys if k not in touched]
    assert len(running) == 18
    assert all(bench.attempt_state(k) == "RUNNING" for k in running)
    assert all(records[k].outcome is None for k in running)


def test_a_statechart_error_is_not_swallowed_by_the_bench() -> None:
    bench = Bench()
    bench.engine("STARTING")
    with pytest.raises(StatechartError):
        bench.send(ENGINE_KEY, ALL_ATTEMPTS_SETTLED)
