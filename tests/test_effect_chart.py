"""effect as a statechart (docs/statechart-engine.md §16, DL-331).

The chart is well formed and draws no warning. The generated completeness test
runs the pending witnesses by every outcome, and a table written from
concurrency-model §5, not from the rows, pins each cell; a resolved effect is
never posted to, because the outbox refuses a second outcome itself and has
already forgotten the instance (DL-295). Every event a poster sends is in the
chart's alphabet, and its guards raise nothing. The outbox runs on the
engine's executor. A statechart error out of `record` or `resolve`, met in the
engine loop, in replay or in a seal's drain, stops `dsl41 run` with exit 7 and
the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
import statechart_completeness as sc

from dsl41.runner_admission import Envelope
from dsl41.runner_hosts import HostCommand
from dsl41 import runner_effects
from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event
from dsl41.run_root import estate_wal, wal_path
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_effects import (
    EFFECT,
    RESOLVE,
    Effect,
    EffectOutcome,
    Outbox,
    Outcome,
    effect_id_for,
)
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID
from dsl41.runner_journal import ReplayFault, read_journal, read_outbox
from dsl41.runner_startup import resume_run, start_run
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
from dsl41.state_machine import (
    Outcome as StepOutcome,
)

CHART = EFFECT
T0 = datetime(2026, 7, 1, 8, 0)
OUTCOMES: tuple[Outcome, ...] = get_args(Outcome)
RUN_ID = "00000000-0000-4000-8000-000000000001"


def _effect(kind: str = "SPAWN") -> Effect:
    return Effect(
        effect_id=effect_id_for(1, kind, "j", 1),  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        job="j",
        run_number=1,
        executor_id=LOCAL_EXECUTOR_ID,
        index=1,
        at=T0,
        run_id=RUN_ID,
        generation=0,
    )


def _outcome(state: str, effect_id: str | None = None) -> EffectOutcome:
    return EffectOutcome(effect_id=effect_id or _effect().effect_id, state=state)  # type: ignore[arg-type]


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert alphabet(CHART) == [RESOLVE]
    assert not CHART.actions  # the outbox writes the outcome after the microstep


def test_each_outcome_row_ends_in_its_own_outcome() -> None:
    """The partition is the outcome's state, and the row it picks ends there:
    the outbox's record and the chart's leaf name the same state."""
    rows = [row for row in CHART.rows if row.source == "pending"]
    assert sorted((row.guard, row.target) for row in rows) == sorted((o, o) for o in OUTCOMES)


# ---- the abstraction (§13)


def _pending_witness(kind: str) -> sc.Builder:
    """An effect the outbox recorded and has not resolved: a SPAWN, or a KILL."""

    def build() -> tuple[Any, Any]:
        machines = Executor()
        effect = _effect(kind)
        outbox = Outbox(machines)
        outbox.record(effect)
        return machines, outbox.key(effect.effect_id)

    return build


def _final_witness(state: str) -> sc.Builder:
    """An effect resting in a final state. The outbox forgets the instance in
    the call that made it final and posts nothing to it, so the witness
    restores one under an outbox's key."""

    def build() -> tuple[Any, Any]:
        machines = Executor()
        key = Outbox(machines).key(_effect().effect_id)
        machines.restore(key, CHART, {"effect", state})
        return machines, key

    return build


ABSTRACTION = sc.Abstraction(
    witnesses={
        "pending SPAWN": _pending_witness("SPAWN"),
        "pending KILL": _pending_witness("KILL"),
        **{state: _final_witness(state) for state in OUTCOMES},
    },
    partitions={RESOLVE: {state: _outcome(state) for state in OUTCOMES}},
)


def _section_5(witness: str, event: str, label: str) -> str:
    """The cell as concurrency-model §5 states it, written without the rows: a
    pending effect takes the outcome it is given, applied, retired or
    indeterminate. An outcome is final: the outbox refuses a second one
    before any post (DL-295), so a resolved effect's declaration only has to
    be complete, and it ignores the event."""
    assert event == RESOLVE
    if witness in OUTCOMES:
        return f"ignored in {witness}"
    return {"applied": "effect.02", "retired": "effect.03", "indeterminate": "effect.04"}[label]


def test_the_completeness_table_is_section_5() -> None:
    """Every cell: five configurations by three partitions, none excluded."""
    assert CHART.exclusions == ()
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert table == {
        (witness, RESOLVE, state): _section_5(witness, RESOLVE, state)
        for witness in ABSTRACTION.witnesses
        for state in OUTCOMES
    }
    assert len(table) == 15


#: the id out of use: .05 is the final states' implicit ignore (DL-380)
RETIRED = {"effect.05"}


def test_no_row_is_sourced_at_the_root_and_the_retired_id_stays_out() -> None:
    """A resolved effect's cells take no row: the engine ignores the event in
    a final state (statechart-engine §10)."""
    assert [row.id for row in CHART.rows] == [f"effect.0{n}" for n in (2, 3, 4)]
    assert [row.id for row in CHART.rows if row.source == CHART.root.name] == []
    assert {row.id for row in CHART.rows} & RETIRED == set()  # a retired id stays out (DL-364)
    assert set(CHART.guards) == set(OUTCOMES)


@pytest.mark.parametrize("first", OUTCOMES)
def test_a_second_outcome_never_reaches_the_chart(first: str) -> None:
    """The final cells, as the tier meets them: the outbox refuses a second
    outcome with today's EngineError, before any post, and the first stands."""
    posts: list[str] = []
    machines = Executor()
    dispatch = machines.dispatch

    def spy(key: Any, event: ChartEvent) -> list[StepOutcome]:
        posts.append(event.payload.state)
        return dispatch(key, event)

    machines.dispatch = spy  # type: ignore[method-assign]
    outbox = Outbox(machines)
    effect = _effect()
    outbox.record(effect)
    outbox.resolve(_outcome(first))
    for second in OUTCOMES:
        row = {"applied": "effect.02", "retired": "effect.03", "indeterminate": "effect.04"}[second]
        with pytest.raises(EngineError) as refused:
            outbox.resolve(_outcome(second))
        assert str(refused.value) == (
            f"outcome {second} for {effect.effect_id}: the effect is already {first}, and an"
            f" outcome is final ({row}: {first} is not a source of {row})"
            " (concurrency-model ss5)"
        )
    assert posts == [first]
    assert not machines._pool  # nothing posted and left unrun
    assert outbox.state_of(effect.effect_id) == first
    assert outbox.result_for(effect.effect_id) == (
        "outcome_unavailable" if first == "indeterminate" else _outcome(first)
    )
    with pytest.raises(StatechartError, match="no instance"):
        machines.active(outbox.key(effect.effect_id))  # forgotten once final


@pytest.fixture
def private_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, so a guard outcome the gate forbids
    stays out of the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


def test_an_outcome_no_row_takes_raises_and_writes_nothing(private_hits: Path) -> None:
    """A partition the guards do not cover is a gap: the post raises
    `UnhandledEvent`, the root's row does not swallow it, and the outbox
    writes no outcome."""
    outbox = Outbox()
    effect = _effect()
    outbox.record(effect)
    stranger = EffectOutcome.model_construct(effect_id=effect.effect_id, state="unknown")
    with pytest.raises(UnhandledEvent, match="no row and no deferral for resolve"):
        outbox.resolve(stranger)
    assert outbox.state_of(effect.effect_id) == "pending"
    assert outbox.pending() == [effect]
    assert outbox.result_for(effect.effect_id) is None


def test_the_chart_s_guards_raise_nothing() -> None:
    for guard in CHART.guards.values():
        for leaf in ("pending", *OUTCOMES):
            for state in OUTCOMES:
                ctx = Context(
                    "k", ChartEvent(RESOLVE, _outcome(state)), frozenset({"effect", leaf}), None
                )
                assert isinstance(guard(ctx), bool)


# ---- the executor (statechart-engine §11)


def test_an_engine_holds_only_an_outbox_on_its_own_executor() -> None:
    engine = Engine(
        lower_source("insert_job: j\njob_type: c\ncommand: x\n"),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.outbox.machines is engine.machines
    with pytest.raises(StatechartError, match="the outbox runs on another executor"):
        engine.outbox = Outbox()
    mine = Outbox(engine.machines)
    engine.outbox = mine
    assert engine.outbox is mine


def test_a_retry_after_a_refused_resume_runs_on_the_same_executor(tmp_path: Path) -> None:
    """A resume refused after replay leaves its outbox's instances on the
    executor it was given until the process exits: here the open SPAWN,
    whose spool names another process. A retry with the same executor
    builds its own outbox, whose keys are its own, and opens."""
    run_root = tmp_path / "run"
    solo = "insert_job: j\njob_type: c\ncommand: x\n"
    engine = start_run(
        lower_source(solo),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, "j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        await engine.shutdown()

    asyncio.run(scenario())
    assert engine.journal is not None
    engine.journal.close()
    (spawn,) = engine.outbox.effects()
    # the crash landed between the launch and its record: the SPAWN is open
    wal = estate_wal(run_root)
    lines = wal.read_text().splitlines(keepends=True)
    wal.write_text("".join(line for line in lines if '"effect_result"' not in line))
    spool = run_root / "runs" / "j.1"
    spool.mkdir(parents=True)
    stranger = "00000000-0000-4000-8000-000000000002"
    (spool / "spawn.json").write_text(json.dumps({"run_id": stranger}))
    machines = Executor()

    async def resume() -> Engine:
        return await resume_run(
            lower_source(solo),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=2)),
            adapters={"CMD": FakeAdapter(default=None)},
            machines=machines,
        )

    with pytest.raises(EngineError, match=stranger):
        asyncio.run(resume())
    (spool / "spawn.json").unlink()
    again = asyncio.run(resume())
    assert again.outbox.machines is machines
    assert again.outbox.state_of(spawn.effect_id) == "applied"  # re-driven (DL-102)
    assert again.journal is not None
    again.journal.close()


# ---- the alphabet (DL-325)

_JIL = "insert_job: a\njob_type: c\ncommand: x\n\ninsert_job: b\njob_type: c\ncommand: y\n"


def _ev(kind: str, minutes: float, job: str) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload={"job": job})


def _posting(posted: list[tuple[str, tuple[str, ...]]]) -> Executor:
    """A process executor that records every post to an effect: the event and
    the rows it fired. Each post is one microstep at depth 0."""
    machines = Executor()
    dispatch = machines.dispatch

    def spy(key: Any, event: ChartEvent) -> list[StepOutcome]:
        outcomes = dispatch(key, event)
        if isinstance(key, tuple) and isinstance(key[0], Outbox):  # (outbox, effect_id)
            assert [outcome.depth for outcome in outcomes] == [0], outcomes
            posted.append((event.name, tuple(row for o in outcomes for row in o.rows)))
        return outcomes

    machines.dispatch = spy  # type: ignore[method-assign]
    return machines


# ---- a statechart error stops the tier (statechart-engine §5)


def _gap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: str = "02") -> None:
    """Replace the chart with a copy that lacks one row, by default the row
    for `applied`, recording its hits into a private directory."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    rows = tuple(row for row in CHART.rows if row.id != f"effect.{number}")
    monkeypatch.setattr(runner_effects, "EFFECT", replace(CHART, rows=rows))


def _decision(effect: Effect) -> dict[str, Any]:
    return {"rec": "decision", "index": effect.index, "effects": [effect.model_dump(mode="json")]}


def _result(outcome: EffectOutcome) -> dict[str, Any]:
    return {"rec": "effect_result", **outcome.model_dump(mode="json")}


def test_replay_names_the_outcome_record_a_gap_meets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not wrapped as an `OutcomeReplayFault`: the error leaves as itself, and
    its report names the WAL record."""
    _gap(monkeypatch, tmp_path)
    effect = _effect()
    with pytest.raises(UnhandledEvent) as raised:
        read_outbox([_decision(effect), _result(_outcome("applied"))])
    assert not isinstance(raised.value, ReplayFault)
    lines = str(raised.value).splitlines()
    assert lines[0].startswith(
        f"effect (outbox, '{effect.effect_id}'): no row and no deferral for resolve"
    )
    assert f"WAL input: the applied outcome of effect {effect.effect_id}" in lines


def _broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replace the chart with a copy whose `pending` enters through an action
    it does not register, so every record's start raises."""
    directory = tmp_path / "hits"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(HITS_ENV, str(directory))
    region = CHART.root.regions[0]
    states = tuple(replace(s, entry="enter") if s.name == "pending" else s for s in region.states)
    root = replace(CHART.root, regions=(replace(region, states=states),))
    monkeypatch.setattr(runner_effects, "EFFECT", replace(CHART, root=root))


def test_replay_names_the_decision_whose_effect_meets_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record whose instance cannot start: the report names the decision."""
    _broken(monkeypatch, tmp_path)
    with pytest.raises(StatechartError, match="action 'enter' is not in the registry") as raised:
        read_outbox([_decision(_effect())])
    assert "WAL input: 1: the decision's effects" in str(raised.value).splitlines()


def test_two_outboxes_on_one_executor_keep_their_own_instances() -> None:
    """The key holds the outbox: a second outbox on the same executor records
    the same effect, and each resolves its own."""
    machines = Executor()
    first, second = Outbox(machines), Outbox(machines)
    first.record(_effect())
    second.record(_effect())
    first.resolve(_outcome("applied"))
    assert first.state_of(_effect().effect_id) == "applied"
    assert second.state_of(_effect().effect_id) == "pending"
    second.resolve(_outcome("retired"))
    assert second.state_of(_effect().effect_id) == "retired"


def test_a_chart_that_lost_an_outcome_row_raises_a_statechart_error_on_a_second_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second outcome's refusal reads its row from the chart. A chart
    without that row is a chart fault: a `StatechartError` naming the
    outcome, with the WAL record in its report, never a `StopIteration` or
    a replay fault."""
    _gap(monkeypatch, tmp_path)
    effect = _effect()
    records = [_decision(effect), _result(_outcome("retired")), _result(_outcome("applied"))]
    with pytest.raises(StatechartError) as raised:
        read_outbox(records)
    assert type(raised.value) is StatechartError
    lines = str(raised.value).splitlines()
    assert lines[0] == "effect: no row of pending takes the applied outcome"
    assert f"WAL input: the applied outcome of effect {effect.effect_id}" in lines


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


_RUN_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _no_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply a SPAWN without starting a process: the gap is met at its outcome."""

    def launch(self: Engine, *_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(Engine, "_launch", launch)


def _serve(run_root: Path, resume: bool) -> int:
    from dsl41 import cli_run
    from dsl41.run_root import runtime_profile_from_cli

    serve = cli_run._serve_run(
        lower_source(_RUN_JIL),
        run_root,
        resume,
        [],
        profile=runtime_profile_from_cli(),
    )
    # bounded: an engine that swallowed the error would serve on forever
    return asyncio.run(asyncio.wait_for(serve, timeout=60))


def _gap_line(report: list[str]) -> str:
    (line,) = [line for line in report if "no row and no deferral for resolve" in line]
    return line


def test_dsl41_run_exits_7_when_a_resolve_in_the_loop_meets_a_gap(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dispatch applies a SPAWN and its outcome meets the gap: the engine
    prints the report under one prefix and exits 7, before the outcome's
    record is written."""
    run_root = short_root / "root"
    real = Engine.run_until_quiescent

    async def with_a_start(self: Engine, horizon: datetime) -> list[Event]:
        self.inject(Event(at=self.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        return await real(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_start)
    _no_launch(monkeypatch)
    _gap(monkeypatch, tmp_path)
    assert _serve(run_root, False) == 7
    report = _report(capsys.readouterr().err)
    assert report[0] == _gap_line(report)
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: effect (outbox, 'e")
    assert f"{STATECHART_LOG_PREFIX}: machine: effect" in report
    records = read_journal(wal_path(run_root, 1))
    assert any(r["rec"] == "decision" and r["effects"] for r in records)
    assert not any(r["rec"] == "effect_result" for r in records)


def test_dsl41_run_exits_7_when_replay_meets_a_gap_naming_the_wal_input(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run applies a SPAWN and stops. Its resume replays the `applied`
    outcome into a chart without that row: `dsl41 run` exits 7, the report
    names the WAL record, and the lock goes with the stop."""
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    result = _applied_spawn_root(run_root, monkeypatch, capsys)
    _gap(monkeypatch, tmp_path)
    assert _serve(run_root, True) == 7
    report = _report(capsys.readouterr().err)
    assert report[0] == _gap_line(report)
    wal_input = f"WAL input: the applied outcome of effect {result['effect_id']}"
    assert f"{STATECHART_LOG_PREFIX}: {wal_input}" in report
    acquire_run_root(run_root).release()  # the lock went with the stop


def test_dsl41_run_exits_7_when_a_second_outcome_meets_a_chart_without_its_row(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The log resolves the spawn `retired` and then `applied`, and the chart
    lost the `applied` row: the refusal cannot name its row, so resume stops
    with the statechart error, exit 7, naming the WAL record."""
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    result = _applied_spawn_root(run_root, monkeypatch, capsys)
    wal = wal_path(run_root, 1)
    retired = json.dumps({**result, "state": "retired"}, sort_keys=True)
    lines = [
        retired if json.loads(line) == result else line for line in wal.read_text().splitlines()
    ]
    wal.write_text("\n".join([*lines, json.dumps(result, sort_keys=True)]) + "\n")
    _gap(monkeypatch, tmp_path)
    assert _serve(run_root, True) == 7
    report = _report(capsys.readouterr().err)
    assert report[0] == (
        f"{STATECHART_LOG_PREFIX}: effect: no row of pending takes the applied outcome"
    )
    wal_input = f"WAL input: the applied outcome of effect {result['effect_id']}"
    assert f"{STATECHART_LOG_PREFIX}: {wal_input}" in report
    acquire_run_root(run_root).release()


def _applied_spawn_root(
    run_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """A run root whose run applied one SPAWN and then stopped with exit 1:
    answers the `effect_result` record."""
    _no_launch(monkeypatch)
    real = Engine.run_until_quiescent

    async def start_then_stop(self: Engine, horizon: datetime) -> list[Event]:
        self.inject(Event(at=self.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        loop = asyncio.create_task(real(self, horizon))
        for _ in range(3000):  # until the spawn's outcome is journaled
            if next(self.outbox.effects(), None) is not None and not self.outbox.pending():
                break
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        raise RuntimeError("test: stop once the spawn is applied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Engine, "run_until_quiescent", start_then_stop)
        assert _serve(run_root, False) == 1
    (result,) = [r for r in read_journal(wal_path(run_root, 1)) if r["rec"] == "effect_result"]
    assert result["state"] == "applied"
    capsys.readouterr()
    return result


def test_every_event_a_poster_sends_is_in_the_alphabet(tmp_path: Path) -> None:
    """An engine run applies a SPAWN, retires a held one whose run ended, and
    applies a KILL whose `effect_result` the crash then loses. The resume
    replays the first two and resolves the KILL `indeterminate` (no wrapper,
    no status). Every post is `resolve`."""
    run_root = tmp_path / "run"
    live: list[tuple[str, tuple[str, ...]]] = []
    engine = start_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter({("a", 1): (3600.0, 0)}, default=None)},
        machines=_posting(live),
    )

    async def scenario() -> None:
        engine.inject(_ev("STARTJOB", 0, "a"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        drained = engine.submit_host(
            HostCommand(verb="drain", host_id="local"),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=engine.epoch),
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert (await drained).decision == "applied"
        engine.inject(_ev("STARTJOB", 3, "b"))  # held: the host routes no new work
        await engine.run_until_quiescent(T0 + timedelta(minutes=4))
        engine.inject(_ev("KILLJOB", 5, "b"))  # the held spawn's run ended
        engine.inject(_ev("KILLJOB", 6, "a"))  # a kill is never held
        await engine.run_until_quiescent(T0 + timedelta(minutes=7))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert sorted(live) == sorted(
        [(RESOLVE, ("effect.02",)), (RESOLVE, ("effect.03",)), (RESOLVE, ("effect.02",))]
    )
    # the crash landed between the kill and its record
    wal = estate_wal(run_root)
    records = read_journal(wal)
    kept = [
        line
        for line, record in zip(wal.read_text().splitlines(keepends=True), records, strict=True)
        if not (record.get("rec") == "effect_result" and ":KILL:" in record["effect_id"])
    ]
    assert len(kept) == len(records) - 1
    wal.write_text("".join(kept))

    resumed: list[tuple[str, tuple[str, ...]]] = []
    machines = _posting(resumed)

    async def resume() -> Engine:
        return await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=8)),
            adapters={"CMD": FakeAdapter(default=None)},
            machines=machines,
        )

    again = asyncio.run(resume())
    assert again.outbox.machines is machines
    assert sorted(resumed) == sorted(
        [(RESOLVE, ("effect.02",)), (RESOLVE, ("effect.03",)), (RESOLVE, ("effect.04",))]
    )
    (kill,) = [e for e in again.outbox.effects() if e.kind == "KILL"]
    assert again.outbox.state_of(kill.effect_id) == "indeterminate"
    assert again.journal is not None
    again.journal.close()

    assert {name for name, _ in live + resumed} <= set(alphabet(CHART))
