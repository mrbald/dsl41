"""seal_boundary as a statechart (docs/statechart-engine.md §16, DL-327).

The chart is well formed and draws no warning. The generated completeness test
runs every witness configuration by every event and payload partition, and a
table written from period-model §6-7, not from the rows, pins each cell. Every
event name the engine posts is in the chart's alphabet, and the chart's own
actions raise nothing. A statechart error inside a seal, whether the drain's,
the commit's or the chart's own, stops the engine with exit 7: the boundary
neither aborts nor moves, and the request is not answered.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc
from test_boundary import C1_JIL, C2_JIL, T0, _close, _genesis, _refusal, _request, _seal, _stage

from dsl41 import runner
from dsl41.boundary import BoundaryFailStop, PeriodSealed
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event, RuntimeState
from dsl41.period import wal_path
from dsl41.runner import Engine, _PendingSeal
from dsl41.runner_admission import AdmissionRefused
from dsl41.runner_clock import EngineError
from dsl41.runner_journal import read_journal
from dsl41.runner_seal import (
    COMMITTED,
    EXCEPTION,
    PARKED,
    QUIESCED,
    SEAL_BOUNDARY,
    SealExit,
    SealFault,
    seal_phase,
)
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    Context,
    Executor,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = SEAL_BOUNDARY
IN_FLIGHT = ("requested", "frozen", "committing")
FINALS = ("sealed", "aborted", "stopped")


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES


# ---- the abstraction (§13)


def _witness(phase: str) -> sc.Builder:
    """A boundary resting in `phase`. The guards and actions read only the
    event's payload and the active states, so the instance has no data."""
    parents = {"boundary", "in_flight"} if phase in IN_FLIGHT else {"boundary"}

    def build() -> tuple[Any, str]:
        executor = Executor()
        executor.restore("seal", CHART, parents | {phase})
        return executor, "seal"

    return build


#: the exception's type: the stop, a refusal, and an OSError (ss7: not just
#: EngineError is reversible)
_ERRORS: dict[str, Callable[[], Exception]] = {
    "past_ponr": lambda: BoundaryFailStop("the seal append failed"),
    "refused": lambda: AdmissionRefused("stale epoch", code="stale_epoch"),
    "oserror": lambda: OSError(28, "No space left on device"),
}
_FLAGS = ("applying", "unadmitted", "unfinished")


def _fault(error: str, fence: str, flags: tuple[str, ...]) -> SealFault:
    return SealFault(
        error=_ERRORS[error](),
        fence_lost=fence == "lost",
        applying=7 if "applying" in flags else None,
        unadmitted="STATUS" if "unadmitted" in flags else None,
        append_unfinished="unfinished" in flags,
    )


def _partitions() -> dict[str, dict[str, Any]]:
    """Every exception type, by fence, by every set of DL-274's flags: so a
    lost fence with a flag set, a flag with the stop type, and two or three
    flags at once are cells of their own."""
    exception: dict[str, Any] = {}
    for error, fence in itertools.product(_ERRORS, ("intact", "lost")):
        for size in range(len(_FLAGS) + 1):
            for flags in itertools.combinations(_FLAGS, size):
                label = f"{error}/{fence}/{'+'.join(flags) or 'none'}"
                exception[label] = _fault(error, fence, flags)
    return {EXCEPTION: exception}


ABSTRACTION = sc.Abstraction(
    witnesses={phase: _witness(phase) for phase in (*IN_FLIGHT, *FINALS)},
    partitions=_partitions(),
)


def _row(number: str) -> str:
    return f"seal_boundary.{number}"


def _section_7(phase: str, event: str, label: str) -> str:
    """The cell as period-model ss6-7 states it, written without the rows.

    The boundary freezes when the barrier parks, commits once quiesced, and is
    sealed when the commit returns. An exception is classified in one order:
    a failure past the point of no return stops; a lost fence stops; an attempt not fully applied, an input the drain
    could not admit or an unfinished append stops; anything else aborts and
    refuses. A finished boundary takes nothing."""
    if phase in FINALS:
        return f"ignored in {phase}"
    if event != EXCEPTION:
        return _row({PARKED: "01", QUIESCED: "02", COMMITTED: "03"}[event])
    error, fence, flags = label.split("/")
    if error == "past_ponr":
        return _row("13")
    if fence == "lost":
        return _row("13")
    if flags != "none":
        return _row("06")
    return _row("04")


def test_the_completeness_table_is_section_7() -> None:
    table = sc.completeness_table(CHART, ABSTRACTION)
    expected = {
        (phase, event, label): _section_7(phase, event, label) for phase, event, label in table
    }
    assert table == expected
    assert {event for _, event, _ in table} == set(alphabet(CHART))
    # each phase takes its own lifecycle event, a final one all three; the
    # other lifecycle cells are the declaration's exclusions
    exceptions = len(_ERRORS) * 2 * 2 ** len(_FLAGS)
    assert len(table) == len(IN_FLIGHT) * (exceptions + 1) + len(FINALS) * (exceptions + 3)


def test_an_event_out_of_order_raises_rather_than_being_ignored() -> None:
    """The excluded cells: in flight, a lifecycle event out of its order finds
    no row, and only a finished boundary's final state ignores the event."""
    out_of_order = {exclusion.state: set() for exclusion in CHART.exclusions}
    for exclusion in CHART.exclusions:
        out_of_order[exclusion.state].add(exclusion.event)
    assert out_of_order == {
        "requested": {QUIESCED, COMMITTED},
        "frozen": {PARKED, COMMITTED},
        "committing": {PARKED, QUIESCED},
    }
    for phase, events in out_of_order.items():
        for event in events:
            executor, key = _witness(phase)()
            with pytest.raises(UnhandledEvent, match=f"no row and no deferral for {event}"):
                executor.dispatch(key, ChartEvent(event))


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (("applying", "unadmitted", "unfinished"), "attempt 7 admitted"),
        (("unadmitted", "unfinished"), "unadmitted STATUS input"),
        (("unfinished",), "unfinished WAL append"),
    ],
)
def test_the_in_doubt_note_names_the_first_flag(flags: tuple[str, ...], named: str) -> None:
    executor, key = _witness("frozen")()
    (outcome,) = executor.dispatch(key, ChartEvent(EXCEPTION, _fault("oserror", "intact", flags)))
    assert outcome.rows == (_row("06"),)
    (exit_,) = outcome.intents
    assert exit_.verdict == "stop" and exit_.note.startswith("DL-274: ") and named in exit_.note


def _labelled(label: str) -> SealFault:
    error, fence, flags = label.split("/")
    return _fault(error, fence, () if flags == "none" else tuple(flags.split("+")))


@pytest.mark.parametrize(
    "label",
    [
        "past_ponr/intact/applying+unadmitted+unfinished",
        "past_ponr/lost/applying",
        "oserror/lost/applying+unadmitted+unfinished",
        "refused/lost/unfinished",
        "past_ponr/intact/none",
        "oserror/lost/none",
    ],
)
def test_past_the_point_of_no_return_or_with_the_fence_lost_the_stop_has_no_note(
    label: str,
) -> None:
    """`stopped`'s entry notes DL-274's flags only in doubt: past the point of
    no return, and with the fence lost, the flags may be set and the note is
    empty."""
    executor, key = _witness("frozen")()
    (outcome,) = executor.dispatch(key, ChartEvent(EXCEPTION, _labelled(label)))
    assert outcome.rows == (_row("13"),)
    assert outcome.intents == (SealExit("stop"),)


# ---- the exit is the final state's entry


#: the ids out of use: .05 and .07 are row .13 (DL-377), .08 went with DL-353, and .09 to .12
#: are the final states' implicit ignore (DL-380)
RETIRED = {_row("05"), _row("07"), _row("08"), _row("09"), _row("10"), _row("11"), _row("12")}


def test_three_rows_take_the_exception_and_the_retired_ids_stay_out() -> None:
    """No row carries an action: each final state's entry reports the exit."""
    exceptions = [
        (row.id, row.guard, row.target)
        for row in CHART.rows
        if row.event == EXCEPTION and row.kind == "external"
    ]
    assert exceptions == [
        (_row("04"), "reversible", "aborted"),
        (_row("13"), "stops", "stopped"),
        (_row("06"), "in_doubt", "stopped"),
    ]
    assert len(CHART.rows) == 6
    assert {row.id for row in CHART.rows} & RETIRED == set()  # a retired id stays out (DL-364)
    assert [row.id for row in CHART.rows if row.action] == []
    (phase,) = CHART.root.regions
    entries = {state.name: state.entry for state in phase.states if state.final}
    assert entries == {"sealed": "answer", "aborted": "abort", "stopped": "stop"}


@pytest.mark.parametrize(
    ("phase", "event", "label", "row", "exit_"),
    [
        ("committing", COMMITTED, "", "03", SealExit("sealed")),
        ("requested", EXCEPTION, "refused/intact/none", "04", SealExit("abort")),
        ("committing", EXCEPTION, "past_ponr/intact/none", "13", SealExit("stop")),
        ("frozen", EXCEPTION, "oserror/lost/none", "13", SealExit("stop")),
        ("frozen", EXCEPTION, "oserror/intact/unfinished", "06", None),
    ],
)
def test_the_entry_s_exit_joins_the_entering_row_s_one_outcome(
    phase: str, event: str, label: str, row: str, exit_: SealExit | None
) -> None:
    """On a bare executor, the post that enters a final state answers one
    outcome: its row, with the entry's one `SealExit` as its only intent. The
    engine's `_seal_exit` unpacks exactly that."""
    executor, key = _witness(phase)()
    payload = _labelled(label) if label else None
    outcomes = executor.dispatch(key, ChartEvent(event, payload))
    (outcome,) = outcomes
    assert outcome.rows == (_row(row),) and outcome.depth == 0
    (reported,) = outcomes[0].intents
    assert isinstance(reported, SealExit)
    if exit_ is None:
        assert reported.verdict == "stop" and "unfinished WAL append" in reported.note
    else:
        assert reported == exit_


#: Every cell of the chart at 395baba, before the final states' entries reported the exits,
#: recorded once by "phase|event|partition": the outcome count, kind, rows, intents
#: (verdict, note) and leaf, or the error class it raised.
_AT_BASE: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parent / "seal_boundary_cells.json").read_text()
)
#: the ids DL-377 merged, as the chart names their cells now
_MERGED = {_row("05"): _row("13"), _row("07"): _row("13")}


def _cell(phase: str, event: str, payload: SealFault | None) -> dict[str, Any]:
    executor, key = _witness(phase)()
    try:
        outcomes = executor.dispatch(key, ChartEvent(event, payload))
    except StatechartError as error:
        return {"raises": type(error).__name__}
    return {
        "outcomes": len(outcomes),
        "kind": outcomes[0].kind,
        "rows": list(outcomes[0].rows),
        "intents": [[i.verdict, i.note] for outcome in outcomes for i in outcome.intents],
        "leaf": seal_phase(executor.active(key)),
    }


def test_every_cell_answers_as_it_did_before_the_exits_moved_to_the_entries() -> None:
    """All 306 cells, 153 of them in flight: the same outcome count, kind,
    intents and leaf, the same error where one was raised, and the same row
    under the merged ids. Each stop cause took 48 cells, so the table holds
    both causes of `stops`. The 153 cells of the finals, each recorded with a
    retired root row, keep everything but the row: the final state ignores
    the event with no row (DL-380)."""
    faults = _partitions()[EXCEPTION]
    assert len(_AT_BASE) == 306
    assert sum(1 for cell in _AT_BASE if cell.split("|")[0] in IN_FLIGHT) == 153
    for cell, recorded in _AT_BASE.items():
        phase, event, label = cell.split("|")
        expected = dict(recorded)
        if phase in FINALS:
            assert len(recorded["rows"]) == 1 and set(recorded["rows"]) <= RETIRED
            expected["rows"] = []
        elif "rows" in expected:
            expected["rows"] = [_MERGED.get(row, row) for row in expected["rows"]]
        assert _cell(phase, event, faults[label] if label else None) == expected, cell
    for retired in _MERGED:
        assert sum(1 for cell in _AT_BASE.values() if cell.get("rows") == [retired]) == 48


# ---- the alphabet (DL-325)


def _spy(engine: Engine, posted: list[str]) -> None:
    """Record every event the engine posts to a seal boundary, and check that
    each post is one microstep at depth 0 whose outcome carries at most one
    `SealExit`. The outbox's effects run on the same executor; their posts
    are not the seal's."""
    dispatch = engine._machines.dispatch

    def spy(key: Any, event: ChartEvent) -> Any:
        outcomes = dispatch(key, event)
        if not isinstance(key, _PendingSeal):
            return outcomes
        assert [outcome.depth for outcome in outcomes] == [0], outcomes
        assert len(outcomes[0].intents) <= 1, outcomes
        assert all(isinstance(intent, SealExit) for intent in outcomes[0].intents)
        posted.append(event.name)
        return outcomes

    engine._machines.dispatch = spy  # type: ignore[method-assign]


def test_every_event_the_engine_posts_is_in_the_alphabet(tmp_path: Path) -> None:
    """The poster is the engine: a committed seal posts the three lifecycle
    events, and a refused one posts the exception."""
    posted: list[str] = []
    committed = _genesis(tmp_path / "one")
    _spy(committed, posted)
    asyncio.run(_seal(committed, _request(committed, _stage(tmp_path / "one", C2_JIL))))
    _close(committed)
    assert posted == [PARKED, QUIESCED, COMMITTED]
    refused = _genesis(tmp_path / "two")
    _spy(refused, posted)
    stale = _request(refused, _stage(tmp_path / "two", C2_JIL), epoch=refused.epoch + 1)
    assert asyncio.run(_refusal(refused, stale)).code == "stale_epoch"
    _close(refused)
    assert set(posted) == set(alphabet(CHART))


def test_a_seal_fault_s_repr_leads_with_the_error() -> None:
    fault = SealFault(AdmissionRefused("stale", code="stale_epoch"), applying=3)
    assert repr(fault) == (
        "SealFault(AdmissionRefused('stale'), fence_lost=False, applying=3,"
        " unadmitted=None, append_unfinished=False)"
    )


def test_the_chart_s_actions_raise_nothing() -> None:
    """No action, each a final state's entry, raises an event or requests a
    continuation: each reports one exit, for the engine to carry out."""
    fault = _fault("oserror", "intact", ("applying",))
    for name, action in CHART.actions.items():
        effects = action(Context("seal", ChartEvent(EXCEPTION, fault), frozenset(), None))
        assert effects is not None and effects.send == () and effects.then == "", name
        assert [type(intent) for intent in effects.intents] == [SealExit], name


# ---- a statechart error in a seal stops the engine (DL-327)


def _no_abort(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    aborts: list[bool] = []
    monkeypatch.setattr(engine, "abort_boundary", lambda: aborts.append(True))
    return aborts


def _stopped_in(run_root: Path, engine: Engine, error: Exception) -> tuple[Any, Any]:
    """Run a seal that meets `error`, and answer its request's future and the
    boundary. The loop task ends with that error and nothing else."""

    async def scenario() -> tuple[Any, Any]:
        future = engine.submit_seal(_request(engine, _stage(run_root, C2_JIL)))
        pending = engine._seal
        with pytest.raises(UnhandledEvent) as raised:
            await engine.run_until_quiescent(T0)
        assert raised.value is error
        return future, pending

    return asyncio.run(scenario())


def _journal(run_root: Path) -> list[dict[str, Any]]:
    return list(read_journal(wal_path(run_root, 1)))


def _left_alone(run_root: Path, engine: Engine, future: Any, pending: Any, phase: str) -> None:
    """Nothing answered, nothing aborted, nothing sealed, nothing moved."""
    assert not future.done()
    assert engine.sealing  # the barrier is still parked
    assert engine._seal_phase(pending) == phase
    assert all(record["rec"] != "seal" for record in read_journal(wal_path(run_root, 1)))


def test_a_statechart_error_in_the_drain_stops_the_engine_with_nothing_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry apply in the drain meets one, with no DL-274 flag set and the
    fence intact: the cell that the abort row would otherwise take. The seal
    does not post it."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}), source="scheduler")
    error = UnhandledEvent("job a: no row and no deferral for STARTJOB")
    posted: list[str] = []
    _spy(engine, posted)

    def classify(*_args: Any, **_kwargs: Any) -> Any:
        assert engine._applying is None and engine._unadmitted is None
        assert engine.journal is not None and not engine.journal.append_unfinished
        assert engine.fence is not None and engine.fence.intact()
        raise error

    monkeypatch.setattr(runner, "classify_fault", classify)
    aborts = _no_abort(engine, monkeypatch)
    future, pending = _stopped_in(run_root, engine, error)
    assert aborts == [] and posted == [PARKED]
    _left_alone(run_root, engine, future, pending, "frozen")
    _close(engine)


_HOLDER = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: h\njob_type: c\ncommand: x\nresources: (R, QUANTITY=1, FREE=A)\n\n"
)


def test_a_statechart_error_in_the_oracle_s_cleanup_route_stops_the_engine_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain's dry apply meets one inside the oracle's batch, while a
    release is moving a terminal row: the batch's cleanup trips on the half-moved
    row. The error leaves as itself, so the drain does not quarantine the input,
    no `engine_apply_faulted` decision is journaled, and the seal stops as in the
    drain test above."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root, text=_HOLDER + C1_JIL)
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "h"}), source="scheduler")
    engine.inject(
        Event(at=T0, kind="STATUS", payload={"job": "h", "status": "SUCCESS"}), source="scheduler"
    )
    error = UnhandledEvent("job h: no row and no deferral for STATUS")

    def release(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(RuntimeState, "release_reservations", release)
    aborts = _no_abort(engine, monkeypatch)
    posted: list[str] = []
    _spy(engine, posted)

    async def scenario() -> tuple[Any, Any]:
        future = engine.submit_seal(_request(engine, _stage(run_root, _HOLDER + C2_JIL)))
        pending = engine._seal
        with pytest.raises(UnhandledEvent) as raised:
            await engine.run_until_quiescent(T0)
        assert raised.value is error
        return future, pending

    future, pending = asyncio.run(scenario())
    assert aborts == [] and posted == [PARKED]
    _left_alone(run_root, engine, future, pending, "frozen")
    assert all(record.get("code") != "engine_apply_faulted" for record in _journal(run_root))
    assert any("the batch's cleanup then raised OracleError" in n for n in error.__notes__)
    _close(engine)


def test_a_statechart_error_while_committing_stops_the_engine_before_the_seal_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same outcome inside `commit_boundary`, after the sidecar and before
    the `seal` append, where any other exception aborts."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    error = UnhandledEvent("a machine broke while committing")

    def hook(stage: str) -> None:
        if stage == "after_sidecar":
            raise error

    monkeypatch.setattr(engine, "crash_point", hook)
    aborts = _no_abort(engine, monkeypatch)
    future, pending = _stopped_in(run_root, engine, error)
    assert aborts == []
    _left_alone(run_root, engine, future, pending, "committing")
    _close(engine)


def test_a_second_seal_during_the_answer_turns_is_refused_naming_sealed(tmp_path: Path) -> None:
    """The boundary has committed and answered its request, and the loop yields
    a few turns before it raises `PeriodSealed`. A request that lands in those
    turns is refused `seal_in_flight` with the phase `sealed`: the instance
    outlives them, so the refusal never becomes an internal error."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    first = _request(engine, staged)
    second = _request(engine, staged, request_id="r-seal-2")
    during: list[Any] = []

    async def scenario() -> Any:
        running = engine.submit_seal(first)
        pending = engine._seal

        async def inject() -> None:
            while not running.done():
                await asyncio.sleep(0)
            during.append(engine.submit_seal(second))

        injector = asyncio.create_task(inject())
        with pytest.raises(PeriodSealed):
            await engine.run_until_quiescent(T0)
        await injector
        return pending

    pending = asyncio.run(scenario())
    (late,) = during
    refusal = late.exception()
    assert isinstance(refusal, EngineError) and refusal.code == "seal_in_flight"
    assert "request_id r-seal-1, sealed" in str(refusal)
    assert "internal_error" not in str(refusal)
    assert getattr(refusal, "in_flight_request_id", None) == "r-seal-1"
    with pytest.raises(StatechartError):  # the instance is forgotten once the answer is out
        engine._machines.active(pending)
    _close(engine)


def _gap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: str = "04") -> None:
    """Replace the chart with a copy that lacks one row, by default the row for
    a reversible exception, recording its hits into a private directory."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    rows = tuple(row for row in CHART.rows if row.id != _row(number))
    monkeypatch.setattr(runner, "SEAL_BOUNDARY", replace(CHART, rows=rows))


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


@pytest.mark.parametrize("source", ["drain", "chart"])
def test_dsl41_run_exits_7_when_a_seal_meets_a_statechart_error(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    """`dsl41 run` with a seal in flight: a dry apply in the drain meets one,
    or the seal chart has a guard gap that a refused seal falls into. Either
    way the engine prints the report under one prefix and exits 7."""
    from dsl41 import cli_run
    from dsl41.ir import lower_source
    from dsl41.period import runtime_profile_from_cli

    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    posted: list[str] = []

    async def with_a_seal(self: Engine, horizon: datetime) -> list[Event]:
        _spy(self, posted)
        if source == "drain":
            self.inject(Event(at=self.clock.now(), kind="STARTJOB", payload={"job": "j"}))
            request = _request(self, _stage(run_root, C2_JIL))
        else:
            request = _request(self, _stage(run_root, C2_JIL), epoch=self.epoch + 1)
        self.submit_seal(request)
        return await real(self, horizon)

    def classify(*_args: Any, **_kwargs: Any) -> Any:
        raise UnhandledEvent("job j: no row and no deferral for STARTJOB")

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_seal)
    if source == "drain":
        monkeypatch.setattr(runner, "trial_apply", classify)
        monkeypatch.setattr(runner, "classify_fault", classify)
        first = "job j: no row and no deferral for STARTJOB"
    else:
        _gap(monkeypatch, tmp_path)
        first = "seal_boundary _PendingSeal('r-seal-1'): no row and no deferral for exception"
    jil = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    serve = cli_run._serve_run(
        lower_source(jil), run_root, False, [], profile=runtime_profile_from_cli()
    )
    # bounded: an engine that swallowed the error would serve on forever
    code = asyncio.run(asyncio.wait_for(serve, timeout=60))
    assert code == 7
    assert posted == ([PARKED] if source == "drain" else [])  # the seal met it
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: {first}")
    if source == "chart":
        assert f"{STATECHART_LOG_PREFIX}: machine: seal_boundary" in report
        # the report names what the seal met, not a truncated payload
        (met,) = (line for line in report if "seal exception: " in line)
        assert met.startswith(
            f"{STATECHART_LOG_PREFIX}: seal exception: AdmissionRefused: epoch 2 is not this"
        )


def test_a_statechart_error_at_the_committed_post_leaves_the_seal_durable(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `committed` post follows `commit_boundary`, so a chart without that
    row stops the engine with exit 7 after the `seal` record is durable and the
    lineage head is closed. The request is not answered."""
    from test_estate import _head

    from dsl41 import cli_run
    from dsl41.boundary import ClosedHead
    from dsl41.ir import lower_source
    from dsl41.period import runtime_profile_from_cli

    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    futures: list[Any] = []

    async def with_a_seal(self: Engine, horizon: datetime) -> list[Event]:
        futures.append(self.submit_seal(_request(self, _stage(run_root, C2_JIL))))
        return await real(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_seal)
    _gap(monkeypatch, tmp_path, "03")
    jil = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
    serve = cli_run._serve_run(
        lower_source(jil), run_root, False, [], profile=runtime_profile_from_cli()
    )
    code = asyncio.run(asyncio.wait_for(serve, timeout=60))
    assert code == 7
    report = _report(capsys.readouterr().err)
    assert report[0].startswith(
        f"{STATECHART_LOG_PREFIX}: seal_boundary _PendingSeal('r-seal-1'):"
        " no row and no deferral for committed"
    )
    (future,) = futures
    assert not future.done()
    assert [record["rec"] for record in _journal(run_root)].count("seal") == 1
    assert isinstance(_head(run_root), ClosedHead)


def test_the_offline_sealer_exits_7_when_the_seal_chart_meets_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The offline `dsl41 seal` runs the same boundary in its own engine: a
    statechart error there is reported and exits 7, not 2, and the period is
    still open."""
    from test_estate import _estate, _head, _native_root, _seal_offline

    from dsl41.boundary import OpenHead
    from dsl41.period import seal_path

    c1, _, _ = _estate(tmp_path / "estate")
    broken = tmp_path / "estate" / "broken.jil"
    broken.write_text("insert_job: a\njob_type: c\ncommand: x\nmachine: nowhere\n")
    run_root = tmp_path / "run"
    _native_root(run_root, c1)
    _gap(monkeypatch, tmp_path)
    result = _seal_offline(run_root, broken)
    assert result.exit_code == 7, result.output
    assert f"{STATECHART_LOG_PREFIX}: machine: seal_boundary" in result.output.splitlines()
    assert not seal_path(run_root, 1).exists()
    assert isinstance(_head(run_root), OpenHead)
