"""host as a statechart (docs/statechart-engine.md §16, DL-333).

The chart is well formed and draws no warning. The generated completeness
test runs a witness for each routing state, quarantine from each routed
state, against a table written from concurrency-model §8, not from the rows;
the relay's re-registration from `evicted` (host.12, spec-only) is the one
excluded cell. Every event a poster sends is in the chart's alphabet, and its
guards raise nothing.

It is the first oracle-tier machine: the store owns its executor. A new row
starts its instance, a re-registration starts nothing, a fork copies the
executor with each host's data bound to the fork's rows (DL-339), and a
carried drain is the configuration the next period opens in. The per-input
revisions over the exhaustive host walk equal the ones the table
implementation recorded. A gap in the chart is a
`StatechartError`, met live, on the dry apply of an operator's command, and
in replay, where it names the WAL input.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc
from test_boundary import C2_JIL, _close, _genesis, _request, _resume, _seal, _stage
from test_effect_chart import _serve
from test_exhaustive_engine import _ACTOR, _HOST_BUILDS, _HOST_JIL, _host_attempt, _host_events

from dsl41.ir import lower_source
from dsl41.machines import MACHINES
from dsl41.oracle import Oracle
from dsl41.oracle_state import FORCE_EVICT, HOST, REGISTER, HostData, OracleError, RuntimeState
from dsl41.period import estate_wal, wal_path
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Envelope, apply_attempt
from dsl41.runner_clock import VirtualClock
from dsl41.runner_hosts import (
    LOCAL_EXECUTOR_ID,
    HostCommand,
    apply_host_command,
    host_event,
    host_move,
    host_rejection_reason,
    seed_local_executor,
)
from dsl41.runner_journal import ReplayFault, read_journal, replay_period
from dsl41.runner_startup import resume_run, start_run
from dsl41.state_machine import (
    HITS_ENV,
    STATECHART_LOG_PREFIX,
    Context,
    Executor,
    Row,
    StatechartError,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

CHART = HOST
T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
KEY = RuntimeState.host_key("h")


def test_the_chart_is_well_formed_and_draws_no_warning() -> None:
    assert well_formed(CHART) == []
    assert chart_warnings(CHART) == []
    assert CHART.clock == "logical"
    assert CHART in MACHINES
    assert alphabet(CHART) == sorted(
        ["activate", "drain", "evict", FORCE_EVICT, "quarantine", "reinstate", REGISTER]
    )
    assert not CHART.actions  # the tier writes the row from the leaf after the microstep
    assert {row.code for row in CHART.rows if row.kind == "refusal"} == {
        "host_quarantined",
        "host_not_quarantined",
        "host_evicted",
        "host_already_evicted",
    }
    assert [(row.id, row.mark) for row in CHART.rows if row.mark] == [("host.12", "spec-only")]


# ---- the abstraction (§13)


def _store(*verbs: str) -> RuntimeState:
    """A store holding host `h`, moved by `verbs` through the tier's apply."""
    store = RuntimeState()
    store.begin_input()
    store.register_host("h", deadman_s=60.0, at=T0)
    store.commit_input()
    for verb in verbs:
        store.begin_input()
        cmd = HostCommand(verb=verb, host_id="h", force=verb == "evict")  # type: ignore[arg-type]
        apply_host_command(store, cmd, actor=_ACTOR)
        store.commit_input()
    return store


def _witness(*verbs: str) -> sc.Builder:
    def build() -> tuple[Any, Any]:
        return _store(*verbs).machines, KEY

    return build


WITNESSES: dict[str, tuple[str, ...]] = {
    "active": (),
    "passive": ("drain",),
    "quarantined from active": ("quarantine",),
    "quarantined from passive": ("drain", "quarantine"),
    "evicted": ("evict",),
}
ABSTRACTION = sc.Abstraction(witnesses={name: _witness(*v) for name, v in WITNESSES.items()})


def _section_8(witness: str, event: str) -> str:
    """The cell as concurrency-model §8 states it, written without the rows.

    `active` routes and `passive` drains; an operator's verb that asks for
    the state the host is in moves nothing; the leader quarantines a routed
    host and reinstates what it interrupted; an operator may not change a
    quarantined host's state; eviction needs `quarantined` (precondition 1)
    unless forced; an evicted host refuses every verb. Re-registration keeps
    the state."""
    state = witness.split()[0]
    if event == REGISTER:
        return "ignored host.22"
    if state == "evicted":
        refused = "host_already_evicted" if event in ("evict", FORCE_EVICT) else "host_evicted"
        return f"refused {refused}"
    if event == FORCE_EVICT:
        return "host.11"
    if state == "quarantined":
        return {
            "activate": "refused host_quarantined",
            "drain": "refused host_quarantined",
            "quarantine": "host.06",
            "reinstate": "host.07",
            "evict": "host.10",
        }[event]
    if event == "evict":
        return "refused host_not_quarantined"
    if event == "quarantine":
        return "host.05"
    return {
        ("active", "activate"): "host.01",
        ("active", "drain"): "host.02",
        ("passive", "activate"): "host.03",
        ("passive", "drain"): "host.04",
        ("active", "reinstate"): "host.08",
        ("passive", "reinstate"): "host.09",
    }[(state, event)]


def test_the_completeness_table_is_section_8() -> None:
    """Every cell: five configurations by seven events, but the relay's
    re-registration from `evicted`, which is spec-only and the one exclusion."""
    assert [(x.state, x.event) for x in CHART.exclusions] == [("evicted", REGISTER)]
    table = sc.completeness_table(CHART, ABSTRACTION)
    assert table == {
        (witness, event, sc.ANY): _section_8(witness, event)
        for witness in WITNESSES
        for event in alphabet(CHART)
        if (witness, event) != ("evicted", REGISTER)
    }
    assert len(table) == 34


@pytest.mark.parametrize(
    ("witness", "branch", "leaf"),
    [
        ("quarantined from active", "host.24", "active"),
        ("quarantined from passive", "host.23", "passive"),
    ],
)
def test_reinstating_puts_back_the_remembered_state(witness: str, branch: str, leaf: str) -> None:
    """host.07's choice reads what quarantine interrupted; the apply writes
    that leaf, the row forgets the remembered state, and the revision moves."""
    store = _store(*WITNESSES[witness])
    cmd = HostCommand(verb="reinstate", host_id="h")
    read = host_move(store, cmd)
    assert (read.rows, read.branches) == (("host.07",), (branch,))
    before = store.revision(KEY)
    store.begin_input()
    apply_host_command(store, cmd, actor=None)
    assert store.commit_input() == [KEY]
    row = store.hosts["h"]
    assert (row.state, row.state_before_quarantine) == (leaf, None)
    assert store.revision(KEY) == before + 1
    assert store.machines.active(KEY) - {"host", "routed"} == {leaf}


def test_a_second_quarantine_keeps_what_the_first_interrupted() -> None:
    """The verb is idempotent on its own: a second call on a quarantined row
    keeps the remembered state and moves no revision, so a reinstatement
    still lands where the first quarantine interrupted."""
    store = _store("drain", "quarantine")
    before = store.revision(KEY)
    store.begin_input()
    store.quarantine_host("h")
    assert store.commit_input() == []
    assert store.revision(KEY) == before
    assert store.hosts["h"].state_before_quarantine == "passive"
    store.begin_input()
    apply_host_command(store, HostCommand(verb="reinstate", host_id="h"), actor=None)
    store.commit_input()
    assert store.hosts["h"].state == "passive"


def test_a_refusal_code_the_gate_has_no_sentence_for_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate names every code; a chart refusal with another code is a
    disagreement between the two, never a rejection under a borrowed code."""
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    refusal = Row("host.15", "routed", "evict", kind="refusal", code="host_draining")
    rows = tuple(refusal if row.id == "host.15" else row for row in CHART.rows)
    monkeypatch.setattr("dsl41.oracle_state.HOST", replace(CHART, rows=rows))
    store = _store()
    with pytest.raises(StatechartError, match="the refusal code 'host_draining' has no sentence"):
        host_rejection_reason(store, HostCommand(verb="evict", host_id="h"), T0, grace_s=10.0)


def test_the_chart_s_guards_raise_nothing() -> None:
    for name, verbs in WITNESSES.items():
        store = _store(*verbs)
        active = store.machines.active(KEY)
        for event in alphabet(CHART):
            ctx = Context(KEY, ChartEvent(event), active, HostData(store, "h"))
            for guard in CHART.guards.values():
                assert isinstance(guard(ctx), bool), name


# ---- the store's executor (statechart-engine §11)


def test_a_new_row_starts_its_instance_and_a_re_registration_starts_nothing() -> None:
    store = _store("drain")
    assert store.machines.active(KEY) == {"host", "routed", "passive"}
    machines = store.machines
    store.begin_input()
    store.register_host("h", deadman_s=45.0, at=T0 + timedelta(minutes=5))
    store.commit_input()
    assert store.machines is machines
    assert store.machines.active(KEY) == {"host", "routed", "passive"}  # the drain stands
    assert store.machines.data(KEY) == HostData(store, "h")


def test_a_fork_rebuilds_its_own_executor_from_its_rows() -> None:
    """A fork's executor is its own: a copy of the source's (DL-339), in the
    same configuration, with the host's data bound to the fork's store, and
    its moves never reach the source."""
    store = _store("quarantine")
    twin = store.fork()
    assert twin.machines is not store.machines
    assert twin.machines.active(KEY) == store.machines.active(KEY)
    assert twin.machines.data(KEY).store is twin
    twin.begin_input()
    apply_host_command(twin, HostCommand(verb="reinstate", host_id="h"), actor=None)
    twin.commit_input()
    assert twin.machines.active(KEY) == {"host", "routed", "active"}
    assert store.machines.active(KEY) == {"host", "quarantined"}
    assert store.hosts["h"].state == "quarantined"


@pytest.mark.parametrize(
    ("write", "state"),
    [
        (lambda store: store.set_host_state("h", "passive"), "passive"),
        (lambda store: store.quarantine_host("h"), "quarantined"),
        (lambda store: store.evict_host("h", forced_by=None), "evicted"),
    ],
)
def test_a_state_write_no_microstep_made_raises_and_writes_nothing(write: Any, state: str) -> None:
    """Only the tier writes a state, from the leaf a microstep reached. A
    write the instance is not in is a bug: it raises before anything is
    written, and the executor does not follow it."""
    store = _store()
    machines, before = store.machines, store.hosts["h"]
    store.begin_input()
    with pytest.raises(StatechartError) as raised:
        write(store)
    assert store.commit_input() == []
    assert str(raised.value) == (
        f"host 'h': a write of state {state} while its instance is in active, host, routed;"
        " only the tier writes a state, from the leaf a microstep reached"
    )
    assert store.machines is machines
    assert store.machines.active(KEY) == {"host", "routed", "active"}
    assert store.hosts["h"] == before


def test_a_witness_writes_the_row_and_rebuilds_the_executor_from_the_rows() -> None:
    """A test's witness (DL-293) writes a row directly, with no microstep,
    through the one explicit store method, which rebuilds the executor from
    the rows as an install does. The write takes the input's revision."""
    store = _store()
    machines = store.machines
    store.begin_input()
    store.witness_host("h", state="quarantined", state_before_quarantine="passive")
    assert store.commit_input() == [KEY]
    assert store.machines is not machines
    assert store.machines.active(KEY) == {"host", "quarantined"}
    assert store.machines.data(KEY) == HostData(store, "h")
    with pytest.raises(OracleError, match="no host 'nope'"):
        store.witness_host("nope", state="passive")


def test_no_tier_calls_the_witness_write() -> None:
    """`witness_host` is a test's builder: under src/ nothing calls it, and
    the only `witness_host(` is its own definition."""
    src = Path(__file__).resolve().parents[1] / "src"
    found = [
        line.strip()
        for path in sorted(src.rglob("*.py"))
        for line in path.read_text().splitlines()
        if "witness_host(" in line
    ]
    assert found == ["def witness_host(self, host_id: str, **fields: object) -> None:"]


def test_a_carried_drain_is_the_configuration_the_next_period_opens_in(tmp_path: Path) -> None:
    """Seal and reopen: the drain crosses the boundary in the carried row, the
    next period's store restores its instance from it, and an activate then
    takes host.03 from there."""
    run_root = tmp_path / "run"
    engine = _genesis(run_root)

    async def under_c1() -> None:
        engine.inject_host(HostCommand(verb="drain", host_id=engine.executor_id))
        await engine.run_until_quiescent(T0)

    asyncio.run(under_c1())
    asyncio.run(_seal(engine, _request(engine, _stage(run_root, C2_JIL))))
    _close(engine)
    opened = _resume(run_root, C2_JIL)
    key = RuntimeState.host_key(opened.executor_id)
    store = opened.oracle.store
    assert store.hosts[opened.executor_id].state == "passive"
    assert store.machines.active(key) == {"host", "routed", "passive"}
    store.begin_input()
    cmd = HostCommand(verb="activate", host_id=opened.executor_id)
    (outcome,) = store.machines.dispatch(key, host_event(cmd))
    store.commit_input()
    assert outcome.rows == ("host.03",)
    _close(opened)


# ---- the alphabet (DL-325)


def test_every_event_a_poster_sends_is_in_the_alphabet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's `submit_host`, the leader's `inject_host` from the
    renewal callbacks, and replay at resume all post through the real paths;
    the gate's scratch reading posts the same names. A carried install posts
    nothing: it restores."""
    posted: list[str] = []
    real = Executor.dispatch

    def spy(self: Executor, key: Any, event: ChartEvent) -> Any:
        if isinstance(key, str) and key.startswith("host:"):
            posted.append(event.name)
        return real(self, key, event)

    monkeypatch.setattr(Executor, "dispatch", spy)
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_JIL),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter()},
        deadman_s=60.0,
    )

    async def operator(
        verb: str, request_id: str, force: bool = False, decision: str = "applied"
    ) -> None:
        cmd = HostCommand(verb=verb, host_id=LOCAL_EXECUTOR_ID, force=force)  # type: ignore[arg-type]
        revision = engine.oracle.store.revision("host:local")
        envelope = Envelope(
            request_id=request_id,
            expect={"host:local": revision},
            epoch=engine.epoch,
            claimed_actor=_ACTOR,
        )
        decided = engine.submit_host(cmd, envelope)
        await engine.run_until_quiescent(engine.clock.now())
        result = await decided
        assert result.decision == decision, (verb, result.code, result.reason)

    async def scenario() -> None:
        await operator("drain", "r1")
        engine.note_executor_unreachable()
        await engine.run_until_quiescent(T0)
        engine.note_executor_reachable()
        await engine.run_until_quiescent(T0)
        await operator("activate", "r2")
        engine.note_executor_unreachable()
        await engine.run_until_quiescent(T0)
        await engine.clock.wait_until(T0 + timedelta(minutes=5))  # past the bound
        await operator("evict", "r3")
        # the gate reads force_evict and refuses it: the host is already evicted
        await operator("evict", "r4", force=True, decision="rejected")
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    live = set(posted)
    assert live == {"drain", "quarantine", "reinstate", "activate", "evict", FORCE_EVICT}
    posted.clear()

    async def resume() -> Engine:
        return await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
            deadman_s=60.0,
        )

    again = asyncio.run(resume())
    assert again.oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "evicted"
    # replay re-applies each applied command, without the gate; the rejected
    # one replays as its stored decision and posts nothing
    assert set(posted) == live - {FORCE_EVICT}
    assert again.journal is not None
    again.journal.close()
    assert live <= set(alphabet(CHART))


# ---- per-input revision equality (statechart-engine §16, oracle tier)

#: Recorded from the table implementation (the base of DL-333) by the same walk.
_RECORDED = Path(__file__).with_name("host_revisions.json")


def _row(oracle: Oracle) -> list[Any]:
    row = oracle.store.hosts[LOCAL_EXECUTOR_ID]
    return [row.state, row.generation, row.forced_by, row.state_before_quarantine, row.state_rev]


def _revision_table() -> dict[str, list[Any]]:
    """For every build of the exhaustive walk and every host command in it:
    the decision, its code, the revisions it moved and the row after. A
    command the apply cannot take is `raised`, with whether the revision
    check or the chart stopped it."""
    catalog = lower_source(_HOST_JIL)
    table: dict[str, list[Any]] = {}
    for deadman in (None, 60.0):
        for number, build in enumerate(_HOST_BUILDS):
            base = Oracle(catalog)
            seed_local_executor(base.store, LOCAL_EXECUTOR_ID, at=T0, deadman_s=deadman)
            built: list[Any] = []
            for index, (verb, force, offset) in enumerate(build, start=1):
                step = _host_attempt(
                    index,
                    verb,
                    force=force,
                    at=T0 + timedelta(seconds=offset),
                    actor=_ACTOR,
                    host=LOCAL_EXECUTOR_ID,
                )
                result = apply_attempt(base, step, grace_s=10.0).result
                built.append([result.decision, result.code, result.revisions, _row(base)])
            table[f"deadman={deadman} build={number}"] = built
            for name, attempt, decided in _host_events(base._now or T0):
                twin = base.fork()
                try:
                    result = apply_attempt(twin, attempt, decided=decided, grace_s=10.0).result
                    cell: list[Any] = [result.decision, result.code, result.revisions]
                except Exception as exc:  # noqa: BLE001 -- the cell records that it raised
                    cell = ["raised", "diverged" if "replay diverged" in str(exc) else "no move"]
                table[f"deadman={deadman} build={number} {name}"] = [*cell, _row(twin)]
    return table


def test_every_host_input_moves_the_revisions_the_table_implementation_moved() -> None:
    """Per input: the same decision, code, revisions and row as the table
    implementation recorded, over 1120 inputs. Only the class of a durable
    decision the chart refuses changed (a `StatechartError`, was an
    EngineError), and the cell records only that it raised."""
    recorded = json.loads(_RECORDED.read_text())
    assert len(recorded) == 1120
    table = _revision_table()
    assert [key for key in recorded if table.get(key) != recorded[key]] == []
    assert table.keys() == recorded.keys()


# ---- a statechart error stops the tier (statechart-engine §5)


def _gap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, number: str) -> None:
    """Replace the chart with a copy that lacks one row, recording its hits
    into a private directory. Every instance built after this is of the copy."""
    directory = tmp_path / "hits"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv(HITS_ENV, str(directory))
    rows = tuple(row for row in CHART.rows if row.id != f"host.{number}")
    monkeypatch.setattr("dsl41.oracle_state.HOST", replace(CHART, rows=rows))


def _inputs(run_root: Path) -> list[dict[str, Any]]:
    return [r for r in read_journal(estate_wal(run_root)) if r["rec"] in ("input", "host")]


def test_a_leader_s_quarantine_that_meets_a_gap_stops_the_loop_and_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live: the engine-made input's dry apply meets the gap in the gate's
    reading of the chart. DL-320's classification re-raises it, the loop
    stops, and nothing is logged."""
    _gap(monkeypatch, tmp_path, "05")
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_JIL), run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )
    engine.note_executor_unreachable()
    with pytest.raises(UnhandledEvent, match="no row and no deferral for quarantine") as raised:
        asyncio.run(engine.run_until_quiescent(T0))
    assert "machine: host" in str(raised.value).splitlines()
    assert engine.oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "active"
    assert engine.journal is not None
    engine.journal.close()
    assert _inputs(run_root) == []


def test_an_operator_s_drain_whose_dry_apply_meets_a_gap_stops_the_loop_unrefused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control input's dry apply meets the gap: `trial_apply` re-raises
    it (DL-320), the command is neither refused nor answered, and nothing is
    logged."""
    _gap(monkeypatch, tmp_path, "02")
    run_root = tmp_path / "run"
    engine = start_run(
        lower_source(_JIL), run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )

    async def scenario() -> Any:
        decided = engine.submit_host(
            HostCommand(verb="drain", host_id=LOCAL_EXECUTOR_ID),
            Envelope(request_id="r1", expect={"host:local": 1}, epoch=engine.epoch),
        )
        with pytest.raises(UnhandledEvent, match="no row and no deferral for drain"):
            await engine.run_until_quiescent(T0)
        return decided

    decided = asyncio.run(scenario())
    assert not decided.done()
    assert engine.oracle.store.hosts[LOCAL_EXECUTOR_ID].state == "active"
    assert engine.journal is not None
    engine.journal.close()
    assert _inputs(run_root) == []


def _quarantined_root(run_root: Path) -> None:
    """A run root whose log holds one applied quarantine, the leader's."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )
        engine.note_executor_unreachable()
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def test_replay_names_the_host_input_a_gap_meets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not wrapped as a `ReplayFault`: the error leaves as itself, and its
    report names the WAL input."""
    run_root = tmp_path / "run"
    _quarantined_root(run_root)
    _gap(monkeypatch, tmp_path, "05")
    with pytest.raises(UnhandledEvent) as raised:
        replay_period(Oracle(lower_source(_JIL)), read_journal(estate_wal(run_root)))
    assert not isinstance(raised.value, ReplayFault)
    lines = str(raised.value).splitlines()
    assert lines[0].startswith("host 'host:local': no row and no deferral for quarantine")
    assert "WAL input: 1: host quarantine of local from reconcile" in lines


def test_dsl41_run_exits_7_when_resume_replays_a_host_decision_the_chart_refuses(
    short_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The DL-295 case: a durable `applied` the chart now refuses. `dsl41 run
    --resume` prints the report and exits 7 (it was a ReplayFault, exit 2)."""
    run_root = short_root / "root"
    real = Engine.run_until_quiescent

    async def quarantine_then_stop(self: Engine, horizon: datetime) -> list[Any]:
        self.note_executor_unreachable()
        loop = asyncio.create_task(real(self, horizon))
        for _ in range(3000):  # until the quarantine is applied
            row = self.oracle.store.host(self.executor_id)
            if row is not None and row.state == "quarantined":
                break
            await asyncio.sleep(0.01)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        raise RuntimeError("test: stop once the quarantine is applied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Engine, "run_until_quiescent", quarantine_then_stop)
        assert _serve(run_root, False) == 1
    capsys.readouterr()
    directory = tmp_path / "hits"
    directory.mkdir()
    monkeypatch.setenv(HITS_ENV, str(directory))
    refusal = Row("host.05", "routed", "quarantine", kind="refusal", code="host_quarantined")
    rows = tuple(refusal if row.id == "host.05" else row for row in CHART.rows)
    monkeypatch.setattr("dsl41.oracle_state.HOST", replace(CHART, rows=rows))
    assert _serve(run_root, True) == 7
    report = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(STATECHART_LOG_PREFIX)
    ]
    assert report[0].startswith(
        f"{STATECHART_LOG_PREFIX}: host quarantine on 'local' is decided applied, but the chart"
        " refuses it (host_quarantined)"
    )
    (wal_input,) = [line for line in report if "WAL input:" in line]
    assert wal_input.endswith("host quarantine of local from reconcile")
    assert any(r["rec"] == "host" for r in read_journal(wal_path(run_root, 1)))
