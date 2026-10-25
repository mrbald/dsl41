"""admission as a statechart (docs/statechart-engine.md §16, DL-334).

The chart is well formed and draws no warning. The generated completeness test
runs the admitted witness and the two finals by every decision and every
fingerprint partition, against a table written from concurrency-model §4, not
from the rows; a decided id is never posted to, because the index answers it
from the stored decision and has already forgotten its instance (DL-217,
CM-05). Every event a poster sends is in the chart's alphabet: the live loop,
replay and the seal's readiness lookup. The index runs on the engine's
executor, and replay keeps one instance open at a time where the attempt and
decision lines interleave. A statechart error met at an attempt or a decision
in the loop, in replay, or at the seal's readiness lookup stops `dsl41 run`
with exit 7 and the report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc
from test_boundary import C1_JIL, C2_JIL, _catalog, _close, _refusal, _request, _stage

from dsl41 import runner_admission
from dsl41.boundary import BoundaryRefusal
from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle_state import Event
from dsl41.period import RuntimeProfile, estate_wal, stage_manifest, wal_path, write_bundle
from dsl41.runner import Engine, _PendingSeal
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    ADMISSION,
    DECIDE,
    INPUT,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Envelope,
    RequestCollision,
    describe_input,
    fingerprint,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import read_attempts, read_decisions, read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import resume_run, start_run
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    Context,
    Executor,
    Outcome,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = ADMISSION
T0 = datetime(2026, 7, 1, 8, 0)
R = "r-1"
FP = "fp-a"
DECISIONS = ("applied", "rejected")


def _attempt(request_id: str = R, fingerprint: str = FP, index: int = 1) -> Attempt:
    return Attempt(index=index, at=T0, request_id=request_id, fingerprint=fingerprint)


def _result(decision: str, request_id: str = R, index: int = 1) -> ApplyResult:
    if decision == "applied":
        return ApplyResult(index=index, request_id=request_id, decision="applied")
    return ApplyResult(
        index=index,
        request_id=request_id,
        decision="rejected",
        reason="the entity moved",
        code="precondition_failed",
    )


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert alphabet(CHART) == [DECIDE, INPUT]
    assert not CHART.actions  # the index writes its maps after the microstep


def test_each_decision_row_ends_in_its_own_decision() -> None:
    """The partition is the decision, and the row it picks ends there: the
    stored decision and the chart's leaf name the same state."""
    rows = [row for row in CHART.rows if row.source == "admitted" and row.event == DECIDE]
    assert sorted((row.guard, row.target) for row in rows) == [(d, d) for d in DECISIONS]


# ---- the abstraction (§13)


def _admitted_witness() -> tuple[Any, Any]:
    """An id whose attempt line the index noted and whose decision it has not."""
    machines = Executor()
    index = DecisionIndex(machines)
    index.note(_attempt())
    return machines, index.key(R)


def _final_witness(state: str) -> sc.Builder:
    """An id resting in a final state. The index forgets the instance in the
    call that made it final and posts nothing to it, so the witness restores
    one under an index's key."""

    def build() -> tuple[Any, Any]:
        machines = Executor()
        key = DecisionIndex(machines).key(R)
        machines.restore(key, CHART, {"admission", state}, data=_attempt())
        return machines, key

    return build


ABSTRACTION = sc.Abstraction(
    witnesses={"admitted": _admitted_witness, **{d: _final_witness(d) for d in DECISIONS}},
    partitions={
        DECIDE: {d: _result(d) for d in DECISIONS},
        INPUT: {"same fingerprint": FP, "other fingerprint": "fp-b"},
    },
)


def _section_4(witness: str, event: str, label: str) -> str:
    """The cell as concurrency-model §4 states it, written without the rows:
    an id in flight takes its decision, applied or rejected (step 7); an
    input naming it with another fingerprint is refused `request_id_reused`
    with no decision to carry, and the same fingerprint again means a second
    writer, which the index raises on (step 2). A decision is final: the
    index answers a decided id from the stored decision before any post
    (DL-217, CM-05), so a final's declaration only has to be complete, and it
    ignores the event."""
    if witness in DECISIONS:
        return f"ignored in {witness}"
    if event == DECIDE:
        return {"applied": "admission.02", "rejected": "admission.03"}[label]
    return {"other fingerprint": "refused request_id_reused", "same fingerprint": "admission.10"}[
        label
    ]


def test_the_completeness_table_is_section_4() -> None:
    """Every cell: three configurations by four partitions, none excluded."""
    assert CHART.exclusions == ()
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert table == {
        (witness, event, label): _section_4(witness, event, label)
        for witness in ABSTRACTION.witnesses
        for event, labels in ABSTRACTION.partitions.items()
        for label in labels
    }
    assert len(table) == 12


#: the ids out of use: .11 and .12 are the final states' implicit ignore (DL-380)
RETIRED = {"admission.11", "admission.12"}


def test_no_row_is_sourced_at_the_root_and_the_retired_ids_stay_out() -> None:
    """A decided instance's cells take no row: the engine ignores the event in
    a final state (statechart-engine §10)."""
    ids = ("02", "03", "09", "10")
    assert [row.id for row in CHART.rows] == [f"admission.{n}" for n in ids]
    assert [row.id for row in CHART.rows if row.source == CHART.root.name] == []
    assert {row.id for row in CHART.rows} & RETIRED == set()  # a retired id stays out (DL-364)
    assert set(CHART.guards) == {"applied", "rejected", "same", "other"}


def _spied_dispatch(posts: list[tuple[str, tuple[str, ...]]]) -> Executor:
    """An executor that records each post: the event and the rows it fired."""
    machines = Executor()
    dispatch = machines.dispatch

    def spy(key: Any, event: ChartEvent) -> list[Outcome]:
        outcomes = dispatch(key, event)
        posts.append((event.name, tuple(row for o in outcomes for row in o.rows)))
        return outcomes

    machines.dispatch = spy  # type: ignore[method-assign]
    return machines


@pytest.mark.parametrize("decision", DECISIONS)
def test_a_decided_id_never_reaches_the_chart(decision: str) -> None:
    """The final cells, as the tier meets them: the index answers an exact
    retry from the stored decision and refuses a reused id carrying it,
    before any post, and the instance is gone."""
    posts: list[tuple[str, tuple[str, ...]]] = []
    machines = _spied_dispatch(posts)
    index = DecisionIndex(machines)
    index.note(_attempt())
    index.record(_result(decision))
    assert posts == [(DECIDE, (f"admission.0{2 if decision == 'applied' else 3}",))]
    assert index.lookup(R, FP) == _result(decision)
    with pytest.raises(RequestCollision) as reused:
        index.lookup(R, "fp-b")
    assert reused.value.original == _result(decision)
    assert len(posts) == 1  # nothing more posted
    assert not machines._pool  # and nothing left unrun
    assert index.state(R) == decision
    with pytest.raises(StatechartError, match="no instance"):
        machines.active(index.key(R))  # forgotten once final


def test_an_id_in_flight_is_asked_of_its_instance() -> None:
    """Another fingerprint takes the refusal row and is refused with no
    decision to carry; the same one takes the internal row and raises
    today's EngineError. Neither moves the instance."""
    posts: list[tuple[str, tuple[str, ...]]] = []
    index = DecisionIndex(_spied_dispatch(posts))
    index.note(_attempt())
    with pytest.raises(RequestCollision) as reused:
        index.lookup(R, "fp-b")
    assert reused.value.original is None
    with pytest.raises(EngineError, match="admitted but undecided"):
        index.lookup(R, FP)
    assert posts == [(INPUT, ("admission.09",)), (INPUT, ("admission.10",))]
    assert index.state(R) == "admitted"


@pytest.fixture
def private_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record into a private directory, so a guard outcome the gate forbids
    stays out of the session."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    return directory


def test_a_decision_no_row_takes_raises_and_writes_nothing(private_hits: Path) -> None:
    """A partition the guards do not cover is a gap: the post raises
    `UnhandledEvent`, the root's row does not swallow it, and the index
    stores no decision."""
    index = DecisionIndex()
    index.note(_attempt())
    stranger = ApplyResult.model_construct(index=1, request_id=R, decision="unknown")
    with pytest.raises(UnhandledEvent, match="no row and no deferral for decide"):
        index.record(stranger)
    assert index.state(R) == "admitted"
    assert index.for_index(1) is None


def test_a_decision_with_no_open_instance_raises_and_writes_nothing() -> None:
    """A decision for an id never noted, or a second one for a decided id,
    has no attempt in flight: the index refuses it in its own words, with the
    machine, the key and the item, before any post or write. The table
    reported it on stderr and stored the decision."""
    posts: list[tuple[str, tuple[str, ...]]] = []
    index = DecisionIndex(_spied_dispatch(posts))
    with pytest.raises(StatechartError) as raised:
        index.record(_result("applied"))
    lines = str(raised.value).splitlines()
    assert lines[0] == (
        f"admission {index.key(R)!r}: a decision at index 1 for a request id with no attempt"
        " in flight"
    )
    assert {"machine: admission", "active: no instance", "item: the event decide"} <= set(lines)
    assert index.state(R) is None and index.for_index(1) is None
    index.note(_attempt())
    index.record(_result("rejected"))
    with pytest.raises(StatechartError) as again:
        index.record(_result("applied", index=2))
    lines = str(again.value).splitlines()
    assert lines[0].startswith(f"admission {index.key(R)!r}: a decision at index 2 ")
    assert "stored decision: rejected at index 1" in lines
    assert posts == [(DECIDE, ("admission.03",))]  # neither refusal posted
    assert index.state(R) == "rejected" and index.for_index(2) is None


def test_a_second_attempt_for_an_id_in_flight_carries_the_instance_s_facts() -> None:
    """The live engine raises on an id in flight at dedup, so only a log can
    hold two attempts for one: the index refuses the second before any start,
    with the executor's facts about the open instance, which keeps the
    first attempt."""
    machines = Executor()
    index = DecisionIndex(machines)
    index.note(_attempt())
    with pytest.raises(StatechartError) as raised:
        index.note(_attempt(fingerprint="fp-b", index=2))
    lines = str(raised.value).splitlines()
    assert lines[0] == (
        f"admission {index.key(R)!r}: an attempt at index 2 for a request id in flight since"
        " index 1: the log disagrees with itself"
    )
    assert {"machine: admission", "active: admission, admitted", "item: the event input"} <= set(
        lines
    )
    assert index.state(R) == "admitted"
    assert machines.data(index.key(R)) == _attempt()


@pytest.mark.parametrize("decision", DECISIONS)
def test_a_second_attempt_for_a_decided_id_is_refused_before_any_start(decision: str) -> None:
    """Only a corrupt log holds it: the live engine answers a decided id at
    dedup. The index raises before any start, and the decision stands."""
    starts: list[Any] = []
    machines = Executor()
    start = machines.start

    def spy(key: Any, chart: Any, data: Any = None) -> None:
        starts.append(key)
        start(key, chart, data)

    machines.start = spy  # type: ignore[method-assign]
    index = DecisionIndex(machines)
    index.note(_attempt())
    index.record(_result(decision))
    with pytest.raises(StatechartError) as raised:
        index.note(_attempt(fingerprint="fp-b", index=2))
    lines = str(raised.value).splitlines()
    assert lines[0] == (
        f"admission {index.key(R)!r}: an attempt at index 2 for a request id decided at"
        f" index 1 ({decision}): the log disagrees with itself"
    )
    assert f"stored decision: {decision} at index 1" in lines
    assert len(starts) == 1
    assert index.state(R) == decision
    assert index.lookup(R, FP) == _result(decision)


def test_a_failed_start_leaves_the_index_and_the_executor_agreeing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry of a broken chart raises after the instance exists: the
    index already holds the fingerprint, so both say the id is in flight,
    and a second attempt meets the index's refusal, not the executor's."""
    _broken(monkeypatch, tmp_path)
    machines = Executor()
    index = DecisionIndex(machines)
    with pytest.raises(StatechartError, match="action 'enter' is not in the registry"):
        index.note(_attempt())
    assert index.state(R) == "admitted"
    assert machines.active(index.key(R)) == {"admission", "admitted"}
    with pytest.raises(StatechartError, match="in flight since index 1"):
        index.note(_attempt(index=2))


def test_two_indexes_on_one_executor_name_themselves_apart() -> None:
    machines = Executor()
    first, second = DecisionIndex(machines), DecisionIndex(machines)
    assert repr(first) != repr(second)
    assert re.fullmatch(r"decisions@[0-9a-f]+", repr(first))


def test_the_chart_s_guards_raise_nothing() -> None:
    """Each row's guard, on every payload of its event, in every leaf."""
    payloads: dict[str, list[Any]] = {
        DECIDE: [_result(d) for d in DECISIONS],
        INPUT: [FP, "fp-b"],
    }
    for row in CHART.rows:
        guard = CHART.guards[row.guard]
        for leaf in ("admitted", *DECISIONS):
            for payload in payloads[row.event]:
                event = ChartEvent(row.event, payload)
                ctx = Context("k", event, frozenset({"admission", leaf}), _attempt())
                assert isinstance(guard(ctx), bool)


# ---- the executor (statechart-engine §11)


def test_an_engine_holds_only_an_index_on_its_own_executor() -> None:
    engine = Engine(
        lower_source("insert_job: j\njob_type: c\ncommand: x\n"),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.decisions.machines is engine.machines
    with pytest.raises(StatechartError, match="the decision index runs on another executor"):
        engine.decisions = DecisionIndex()
    mine = DecisionIndex(engine.machines)
    engine.decisions = mine
    assert engine.decisions is mine


def test_two_indexes_on_one_executor_keep_their_own_instances() -> None:
    """The key holds the index: a refused resume leaves its instances on the
    wiring's executor, and a retry builds its own index beside them."""
    machines = Executor()
    first, second = DecisionIndex(machines), DecisionIndex(machines)
    first.note(_attempt())
    second.note(_attempt())
    first.record(_result("applied"))
    assert first.state(R) == "applied"
    assert second.state(R) == "admitted"
    second.record(_result("rejected"))
    assert second.state(R) == "rejected"


def _wal(lines: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """A log: an opening, then `input` or `decision` lines by index, applied."""
    records: list[dict[str, Any]] = [{"rec": "opening", "baseline_id": "b", "first_index": 1}]
    for rec, index in lines:
        if rec == "input":
            records.append(
                {
                    "rec": "input",
                    "seq": index,
                    "at": T0.isoformat(),
                    "request_id": f"r{index}",
                    "fingerprint": "fp",
                    "kind": "STARTJOB",
                    "payload": {"job": "j"},
                    "source": "control",
                }
            )
        else:
            records.append(
                {
                    "rec": "decision",
                    "index": index,
                    "request_id": f"r{index}",
                    "decision": "applied",
                }
            )
    return records


def test_replay_keeps_one_instance_open_where_the_lines_interleave() -> None:
    """`read_decisions` reads in WAL order: each attempt opens its instance and
    its decision closes it, so the peak is one, and the crash window's
    attempt is the one left open for replay's second pass."""
    machines = Executor()
    open_now: list[int] = []
    start, forget = machines.start, machines.forget

    def counted_start(key: Any, chart: Any, data: Any = None) -> None:
        start(key, chart, data)
        open_now.append(open_now[-1] + 1 if open_now else 1)

    def counted_forget(key: Any) -> None:
        forget(key)
        open_now.append(open_now[-1] - 1)

    machines.start = counted_start  # type: ignore[method-assign]
    machines.forget = counted_forget  # type: ignore[method-assign]
    lines = [("input", 1), ("decision", 1), ("input", 2), ("decision", 2), ("input", 3)]
    index = read_decisions(_wal(lines), machines)
    assert max(open_now) == 1
    assert open_now[-1] == 1
    assert [index.state(f"r{i}") for i in (1, 2, 3)] == ["applied", "applied", "admitted"]


# ---- the alphabet (DL-325)

_JIL = "insert_job: j\njob_type: c\ncommand: x\n"


def _posting(posted: list[tuple[str, tuple[str, ...]]]) -> Executor:
    """A process executor that records every start of and post to a request
    id's instance: `<start>` or the event, and the rows it fired."""
    machines = Executor()
    start, dispatch = machines.start, machines.dispatch

    def is_admission(key: Any) -> bool:
        return isinstance(key, tuple) and isinstance(key[0], DecisionIndex)

    def start_spy(key: Any, chart: Any, data: Any = None) -> None:
        if is_admission(key):
            posted.append(("<start>", ()))
        start(key, chart, data)

    def dispatch_spy(key: Any, event: ChartEvent) -> list[Outcome]:
        outcomes = dispatch(key, event)
        if is_admission(key):
            posted.append((event.name, tuple(row for o in outcomes for row in o.rows)))
        return outcomes

    machines.start = start_spy  # type: ignore[method-assign]
    machines.dispatch = dispatch_spy  # type: ignore[method-assign]
    return machines


def _genesis_on(run_root: Path, machines: Executor) -> Engine:
    """`test_boundary._genesis`, on a given process executor."""
    catalog, sources = _catalog(C1_JIL)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=write_bundle(run_root, sources),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    return start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
        machines=machines,
    )


def _hold(engine: Engine, request_id: str, kind: str, revision: int) -> Any:
    event = Event(at=T0, kind=kind, payload={"job": "j"})  # type: ignore[arg-type]
    envelope = Envelope(request_id=request_id, expect={"job:j": revision}, epoch=engine.epoch)
    return engine.submit(event, envelope)


def _fingerprint_of(engine: Engine, kind: str, revision: int) -> str:
    """The fingerprint the engine gives `_hold`'s command."""
    return fingerprint(
        baseline_id=engine.baseline_id,
        kind=kind,
        payload={"job": "j"},
        source="control",
        epoch=engine.epoch,
        expect={"job:j": revision},
    )


def test_every_event_a_poster_sends_is_in_the_alphabet(tmp_path: Path) -> None:
    """The live loop notes and decides an applied and a rejected command,
    refuses a command whose id is in flight, and stops on the same command
    in flight; resume replays the decided attempts and decides the one a
    crash left open; the seal's readiness lookup refuses an id in flight.
    Every post is `decide` or `input`, and every row fires. An id is in
    flight here as a second writer would leave it."""
    run_root = tmp_path / "run"
    live: list[tuple[str, tuple[str, ...]]] = []
    engine = start_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        machines=_posting(live),
    )

    async def scenario() -> None:
        applied = _hold(engine, "a", "ON_HOLD", 0)
        rejected = _hold(engine, "b", "OFF_HOLD", 0)  # the hold moved the revision
        await engine.run_until_quiescent(T0)
        assert (await applied).decision == "applied"
        assert (await rejected).decision == "rejected"
        engine.decisions.note(_attempt("c", "fp-c", index=99))
        reused = _hold(engine, "c", "ON_HOLD", 1)
        await engine.run_until_quiescent(T0)
        with pytest.raises(RequestCollision):
            await reused
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    assert {(DECIDE, ("admission.02",)), (DECIDE, ("admission.03",))} <= set(live)
    assert ("input", ("admission.09",)) in live
    # the crash landed between the rejected attempt's line and its decision
    wal = estate_wal(run_root)
    lines = wal.read_text().splitlines(keepends=True)
    records = read_journal(wal)
    (at,) = [i for i, r in enumerate(records) if r["rec"] == "decision" and r["request_id"] == "b"]
    assert not any(r["rec"] in ("input", "decision") for r in records[at + 1 :])
    wal.write_text("".join(lines[:at] + lines[at + 1 :]))
    resumed: list[tuple[str, tuple[str, ...]]] = []

    async def resume() -> Engine:
        return await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=1)),
            adapters={"CMD": FakeAdapter(default=None)},
            machines=_posting(resumed),
        )

    again = asyncio.run(resume())
    assert again.decisions.state("a") == "applied"
    assert again.decisions.state("b") == "rejected"  # decided again, the same way
    assert ("<start>", ()) in resumed
    assert {(DECIDE, ("admission.02",)), (DECIDE, ("admission.03",))} <= set(resumed)
    assert again.journal is not None
    again.journal.close()

    sealing: list[tuple[str, tuple[str, ...]]] = []
    root = tmp_path / "sealed"
    genesis = _genesis_on(root, _posting(sealing))
    request = _request(genesis, _stage(root, C2_JIL))
    genesis.decisions.note(_attempt(request.request_id, "fp-open", index=99))
    refused = asyncio.run(_refusal(genesis, request))
    assert isinstance(refused, BoundaryRefusal)
    assert "already names another command" in str(refused)
    assert sealing[-1] == ("input", ("admission.09",))
    _close(genesis)

    stopped: list[tuple[str, tuple[str, ...]]] = []
    lone = Engine(
        lower_source(_JIL),
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        machines=_posting(stopped),
    )
    lone.decisions.note(_attempt("d", _fingerprint_of(lone, "ON_HOLD", 0)))

    async def second_writer() -> None:
        try:
            _hold(lone, "d", "ON_HOLD", 0)
            with pytest.raises(EngineError, match="admitted but undecided"):
                await lone.run_until_quiescent(T0)
        finally:
            await lone.shutdown()

    asyncio.run(second_writer())
    assert stopped[-1] == ("input", ("admission.10",))
    posted = live + resumed + sealing + stopped
    assert {name for name, _ in posted} - {"<start>"} <= set(alphabet(CHART))
    rows = {row for _, fired in posted for row in fired}
    assert rows == {row.id for row in CHART.rows}


# ---- a statechart error stops the tier (statechart-engine §5)


def _gap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: str) -> None:
    """Replace the chart with a copy that lacks one row, recording its hits
    into a private directory."""
    directory = tmp_path / "hits"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(HITS_ENV, str(directory))
    rows = tuple(row for row in CHART.rows if row.id != f"admission.{number}")
    monkeypatch.setattr(runner_admission, "ADMISSION", replace(CHART, rows=rows))


def _broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replace the chart with a copy whose `admitted` enters through an action
    it does not register, so every attempt's start raises."""
    directory = tmp_path / "hits"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(HITS_ENV, str(directory))
    region = CHART.root.regions[0]
    states = tuple(replace(s, entry="enter") if s.name == "admitted" else s for s in region.states)
    root = replace(CHART.root, regions=(replace(region, states=states),))
    monkeypatch.setattr(runner_admission, "ADMISSION", replace(CHART, root=root))


def test_replay_names_the_attempt_whose_instance_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _broken(monkeypatch, tmp_path)
    records = _wal([("input", 1)])
    with pytest.raises(StatechartError, match="action 'enter' is not in the registry") as raised:
        read_decisions(records)
    (attempt,) = read_attempts(records)
    assert f"WAL input: 1: {describe_input(attempt)}" in str(raised.value).splitlines()


@pytest.mark.parametrize(("number", "decision"), [("02", "applied"), ("03", "rejected")])
def test_replay_names_the_decision_a_gap_meets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: str, decision: str
) -> None:
    """Not wrapped as a `ReplayFault`: the error leaves as itself, and its
    report names the WAL record."""
    _gap(monkeypatch, tmp_path, number)
    records = _wal([("input", 1)])
    decided = _result(decision, request_id="r1")
    records.append({**decided.model_dump(mode="json"), "rec": "decision"})
    with pytest.raises(UnhandledEvent) as raised:
        read_decisions(records)
    lines = str(raised.value).splitlines()
    assert re.match(
        r"admission \(decisions@[0-9a-f]+, 'r1'\): no row and no deferral for decide", lines[0]
    )
    assert f"WAL input: 1: the {decision} decision" in lines


def _report(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(STATECHART_LOG_PREFIX)]


_RUN_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _serve(run_root: Path, resume: bool) -> int:
    from dsl41 import cli_run
    from dsl41.period import runtime_profile_from_cli

    serve = cli_run._serve_run(
        lower_source(_RUN_JIL),
        run_root,
        resume,
        [],
        profile=runtime_profile_from_cli(),
        supervisor="spawn",
    )
    # bounded: an engine that swallowed the error would serve on forever
    return asyncio.run(asyncio.wait_for(serve, timeout=60))


def test_dsl41_run_exits_7_when_a_decision_meets_a_gap_live_and_at_replay(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loop's first applied decision meets the gap: the engine prints the
    report under one prefix and exits 7 before the decision line is written,
    so the attempt is left in the crash window. Resume replays it through
    the gate, meets the same gap at its recovered decision, names the WAL
    input and exits 7."""
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    real = Engine.run_until_quiescent

    async def with_a_start(self: Engine, horizon: datetime) -> list[Event]:
        self.inject(Event(at=self.clock.now(), kind="STARTJOB", payload={"job": "j"}))
        return await real(self, horizon)

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_start)
    _gap(monkeypatch, tmp_path, "02")
    assert _serve(run_root, False) == 7
    report = _report(capsys.readouterr().err)
    assert "no row and no deferral for decide" in report[0]
    assert report[0].startswith(f"{STATECHART_LOG_PREFIX}: admission (decisions@")
    assert f"{STATECHART_LOG_PREFIX}: machine: admission" in report
    records = read_journal(wal_path(run_root, 1))
    assert not any(r["rec"] == "decision" for r in records)
    (last,) = read_attempts(records)  # the stop left it undecided
    monkeypatch.setattr(Engine, "run_until_quiescent", real)
    assert _serve(run_root, True) == 7
    report = _report(capsys.readouterr().err)
    assert "no row and no deferral for decide" in report[0]
    wal_input = f"{STATECHART_LOG_PREFIX}: WAL input: {last.index}: {describe_input(last)}"
    assert wal_input in report
    acquire_run_root(run_root).release()  # the lock went with the stop


def test_dsl41_run_exits_7_when_the_seal_s_readiness_lookup_meets_a_gap(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The seal's request id is in flight, as a second writer would leave it,
    and the chart lost the row that refuses another fingerprint. Readiness
    asks the index, which asks the instance: the boundary parks nothing,
    aborts nothing and refuses nothing, the request is unanswered and the
    engine exits 7 (DL-327)."""
    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    futures: list[Any] = []
    aborts: list[bool] = []
    seal_posts: list[str] = []
    keys: list[str] = []

    async def with_a_seal(self: Engine, horizon: datetime) -> list[Event]:
        dispatch = self.machines.dispatch

        def spy(key: Any, event: ChartEvent) -> list[Outcome]:
            if isinstance(key, _PendingSeal):
                seal_posts.append(event.name)
            return dispatch(key, event)

        self.machines.dispatch = spy  # type: ignore[method-assign]
        request = _request(self, _stage(run_root, C2_JIL))
        self.decisions.note(_attempt(request.request_id, "fp-open", index=99))
        keys.append(repr(self.decisions.key(request.request_id)))
        futures.append(self.submit_seal(request))
        return await real(self, horizon)

    def abort(self: Engine) -> None:
        aborts.append(True)

    monkeypatch.setattr(Engine, "run_until_quiescent", with_a_seal)
    monkeypatch.setattr(Engine, "abort_boundary", abort)
    _gap(monkeypatch, tmp_path, "09")
    assert _serve(run_root, False) == 7
    report = _report(capsys.readouterr().err)
    assert report[0] == (
        f"{STATECHART_LOG_PREFIX}: admission {keys[0]}: no row and no deferral"
        " for input in ['admission', 'admitted']"
    )
    assert seal_posts == []  # readiness runs before the barrier parks
    assert aborts == []
    (future,) = futures
    assert not future.done()  # not refused, not answered
    records = read_journal(wal_path(run_root, 1))
    assert all(record["rec"] != "seal" for record in records)


def test_dsl41_run_exits_7_when_resume_meets_a_second_attempt_for_a_decided_id(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run decides a held command under `a` and stops. A corrupt log then
    holds a second attempt line for `a`: resume's index pass refuses it, the
    report names the WAL input, and `dsl41 run` exits 7."""
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    real = Engine.run_until_quiescent
    decided: list[bool] = []

    async def hold_then_stop(self: Engine, horizon: datetime) -> list[Event]:
        hold = Event(at=self.clock.now(), kind="ON_HOLD", payload={"job": "j"})
        self.inject(hold, request_id="a")
        loop = asyncio.create_task(real(self, horizon))
        for _ in range(3000):  # until the hold's decision is journaled
            if self.decisions.state("a") == "applied":
                decided.append(True)
                break
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        raise RuntimeError("test: stop once the hold is decided")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Engine, "run_until_quiescent", hold_then_stop)
        assert _serve(run_root, False) == 1
    assert decided, "the hold was not decided in 30 s"  # exit 1 either way
    capsys.readouterr()
    wal = wal_path(run_root, 1)
    records = read_journal(wal)
    (first,) = [r for r in records if r["rec"] == "input" and r.get("request_id") == "a"]
    seq = max(int(r["seq"]) for r in records if "seq" in r) + 1
    second = {**first, "seq": seq}
    with wal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second, sort_keys=True) + "\n")
    (attempt,) = [a for a in read_attempts(read_journal(wal)) if a.index == seq]
    assert _serve(run_root, True) == 7
    report = _report(capsys.readouterr().err)
    assert re.match(
        rf"{STATECHART_LOG_PREFIX}: admission \(decisions@[0-9a-f]+, 'a'\): an attempt at"
        rf" index {seq} for a request id decided at index",
        report[0],
    )
    assert f"{STATECHART_LOG_PREFIX}: stored decision: applied at index {first['seq']}" in report
    assert f"{STATECHART_LOG_PREFIX}: WAL input: {seq}: {describe_input(attempt)}" in report
    acquire_run_root(run_root).release()  # the lock went with the stop
