"""The dry apply, the named replay fault and the inert violation option
(concurrency-model ss4, period-model ss11).

A control input is applied first on `Oracle.fork()`, before the frontier
moves. An apply that raises refuses it with `apply_faulted`, and a refusal
leaves nothing in the WAL. An engine-made input whose apply raises is
quarantined or stops the engine (test_engine_quarantine.py). A logged input
with a durable decision that raises on replay stops replay with an error
that names it. `run --on-transition-violation` is inert since DL-353:
given, it prints one deprecation line.

Every fault here is an injected one, raised after a patched `transition`,
so a test chooses which move faults. The fork leak test over the SEM corpus
is the `fork` param of test_oracle.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fork_harness import (
    ORACLE_COPIED,
    ORACLE_FRESH,
    ORACLE_IDLE,
    ORACLE_SHARED,
    STORE_COPIED,
    STORE_DORMANT,
    STORE_REBUILT,
    STORE_SHARED,
    state_bytes,
)

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobStatus, RuntimeState
from dsl41.runner import Engine, _Pending
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    AdmissionRefused,
    ApplyResult,
    Attempt,
    Envelope,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_journal import ReplayFault, read_journal, replay_inputs
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _on_status(
    monkeypatch: pytest.MonkeyPatch, status: str, act: Callable[[RuntimeState, str], None]
) -> None:
    """Run `act` after every move of any job into `status`."""
    real = RuntimeState.transition

    def transition(
        self: RuntimeState,
        job: str,
        new: JobStatus,
        at: datetime | None,
        exit_code: int | None = None,
        *,
        clear_exit_code: bool = False,
    ) -> None:
        real(self, job, new, at, exit_code, clear_exit_code=clear_exit_code)
        if new == status:
            act(self, job)

    monkeypatch.setattr(RuntimeState, "transition", transition)


def _fault(store: RuntimeState, job: str) -> None:
    raise RuntimeError(f"an injected fault on {job}")


def _start(at: datetime = T0) -> Event:
    return Event(at=at, kind="STARTJOB", payload={"job": "j"})


def _engine(**kwargs: Any) -> Engine:
    return Engine(
        lower_source(_JIL), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}, **kwargs
    )


def _journaled(run_root: Path) -> Engine:
    return start_run(
        lower_source(_JIL), run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()


def _attempt(index: int, *, kind: str | None, **fields: Any) -> Attempt:
    return Attempt(
        index=index,
        at=fields.pop("at", T0),
        request_id=fields.pop("request_id", f"r{index}"),
        fingerprint="fp",
        kind=kind,
        **fields,
    )


# ------------------------------------------------------------- the fork


def test_the_fork_copies_every_mutable_attribute_and_shares_only_fixed_ones() -> None:
    """The attribute sets are pinned: a new field on either class fails here
    until `fork` and `fork_harness.state_bytes` both name it. The history
    starts empty on the fork, so a fork costs nothing in the period's
    length; the cascade's bookkeeping is false or empty between inputs, on
    the oracle and its fork (DL-350); and the fork's `job_instance` is
    rebound to the fork."""
    oracle = Oracle(lower_source(_JIL))
    assert set(vars(oracle)) == ORACLE_COPIED | ORACLE_FRESH | ORACLE_IDLE | ORACLE_SHARED
    assert set(vars(oracle.store)) == STORE_COPIED | STORE_DORMANT | STORE_REBUILT | STORE_SHARED
    oracle.feed(_start())
    assert oracle._trace and oracle._emitted  # history to leave behind
    oracle._window_starts = [("b", 1, "cause")]  # set only inside a box start
    twin = oracle.fork()
    for name in ORACLE_SHARED:
        assert getattr(twin, name) is getattr(oracle, name), name
    for name in ORACLE_FRESH:
        assert getattr(twin, name) == [], name
        assert getattr(twin, name) is not getattr(oracle, name), name
    for name in ORACLE_IDLE:
        assert not getattr(oracle, name) and not getattr(twin, name), name
        if isinstance(getattr(oracle, name), (set, dict, list)):
            assert getattr(twin, name) is not getattr(oracle, name), name
    for name in ORACLE_COPIED:
        value = getattr(oracle, name)
        assert getattr(twin, name) == value or name == "store", name
        if not isinstance(value, (bool, type(None), datetime)):
            assert getattr(twin, name) is not value, name
    for name in STORE_COPIED:
        value = getattr(oracle.store, name)
        assert getattr(twin.store, name) == value, name
        if not isinstance(value, (bool, int, str)):
            assert getattr(twin.store, name) is not value, name
    for name in STORE_DORMANT:
        assert getattr(twin.store, name) == {} == getattr(oracle.store, name), name
    for name in STORE_REBUILT:  # the twin's own executor
        assert getattr(twin.store, name) is not getattr(oracle.store, name), name
    for name in STORE_SHARED:
        rebound = getattr(twin.store, name)
        assert rebound is not getattr(oracle.store, name), name
        assert getattr(rebound, "__self__", None) is twin, name
    oracle._window_starts = None
    assert oracle.fork()._window_starts is None


def test_a_row_the_oracle_s_fork_creates_never_reaches_the_parent() -> None:
    """DL-350: the twin's macrostep reaches an instance only the twin
    has (a STATUS for a job the catalog lacks creates its row); the parent's
    next macrostep checks only the instances it reached itself."""
    oracle = Oracle(lower_source(_JIL))
    oracle.feed(_start())
    twin = oracle.fork()
    later = T0 + timedelta(minutes=1)
    twin.feed(Event(at=later, kind="STATUS", payload={"job": "ghost", "status": "SUCCESS"}))
    assert "ghost" in twin.store.job and "ghost" not in oracle.store.job
    oracle.feed(Event(at=later, kind="STATUS", payload={"job": "j", "exit_code": 0}))
    assert oracle.store.job["j"].status == "SUCCESS"


def test_a_dry_apply_that_raises_leaves_the_original_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault fires after the fork's row moved: the original still has
    no STARTING row, no trace line and no committed input."""
    oracle = Oracle(lower_source(_JIL))
    before = state_bytes(oracle)
    _on_status(monkeypatch, "STARTING", _fault)
    twin = oracle.fork()
    with pytest.raises(RuntimeError, match="injected fault"):
        twin.feed(_start())
    assert twin.store.job["j"].status == "STARTING"
    assert state_bytes(oracle) == before


# ------------------------------------------------------- which inputs are control

_ENVELOPE = Envelope(request_id="r", expect={"job:j": 0})


@pytest.mark.parametrize(
    ("pending", "control"),
    [
        (_Pending(at=T0, request_id="r", ev=_start(), envelope=_ENVELOPE), True),
        (
            _Pending(at=T0, request_id=None, ev=_start().model_copy(update={"source": "control"})),
            True,
        ),
        (
            _Pending(at=T0, request_id=None, ev=_start().model_copy(update={"source": "adapter"})),
            False,
        ),
        (
            _Pending(
                at=T0, request_id=None, ev=_start().model_copy(update={"source": "scheduler"})
            ),
            False,
        ),
        (
            _Pending(
                at=T0, request_id=None, ev=_start().model_copy(update={"source": "reconcile"})
            ),
            False,
        ),
        (_Pending(at=T0, request_id=None, ev=_start()), False),  # unattributed: the bisim harness
        (_Pending(at=T0, request_id=None), False),  # a time observation
    ],
)
def test_only_a_socket_request_or_a_control_event_is_a_control_input(
    pending: _Pending, control: bool
) -> None:
    assert _engine()._attempt(pending, 1, "fp").engine_made is not control


# ------------------------------------------------------------- refusals


def _submit_start(engine: Engine, request_id: str = "r1") -> asyncio.Future[ApplyResult]:
    expect = {"job:j": engine.oracle.store.revision("job:j")}
    future = engine.submit(_start(), Envelope(request_id=request_id, expect=expect))
    assert isinstance(future, asyncio.Future)
    return future


def test_a_control_input_whose_apply_fails_is_refused_before_the_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal leaves nothing: no input record, no index, no moved state."""
    _on_status(monkeypatch, "STARTING", _fault)

    async def scenario() -> AdmissionRefused:
        engine = _journaled(tmp_path / "run")
        before = (engine.frontiers, state_bytes(engine.oracle))
        future = _submit_start(engine)
        await engine.run_until_quiescent(T0)
        with pytest.raises(AdmissionRefused) as refused:
            await future
        assert (engine.frontiers, state_bytes(engine.oracle)) == before
        assert [kept for _, kept in engine.refused_inputs] == [refused.value]  # r1's
        await _close(engine)
        return refused.value

    refusal = asyncio.run(scenario())
    assert refusal.code == "apply_faulted"
    assert str(refusal).startswith("refused before admission")
    records = read_journal(tmp_path / "run")
    assert not [r for r in records if r.get("rec") in ("input", "decision")]


def test_a_clean_control_input_is_admitted_after_its_dry_apply(tmp_path: Path) -> None:
    async def scenario() -> ApplyResult:
        engine = _journaled(tmp_path / "run")
        future = _submit_start(engine)
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        result = await future
        assert engine.refused_inputs == []
        assert engine.oracle.store.job["j"].status == "SUCCESS"
        await _close(engine)
        return result

    assert asyncio.run(scenario()).decision == "applied"
    records = read_journal(tmp_path / "run")
    assert [r["request_id"] for r in records if r.get("rec") == "input"][0] == "r1"


def test_a_gate_rejection_is_still_a_logged_decision(tmp_path: Path) -> None:
    """The dry apply refuses on a fault only. A stale
    `expect` passes it and is rejected at step 6, at an index."""

    async def scenario() -> ApplyResult:
        engine = _journaled(tmp_path / "run")
        future = engine.submit(_start(), Envelope(request_id="stale", expect={"job:j": 7}))
        await engine.run_until_quiescent(T0)
        result = await future
        await _close(engine)
        return result

    result = asyncio.run(scenario())
    assert (result.decision, result.code) == ("rejected", "precondition_failed")
    records = read_journal(tmp_path / "run")
    assert [r["request_id"] for r in records if r.get("rec") == "input"] == ["stale"]
    assert [r["code"] for r in records if r.get("rec") == "decision"] == ["precondition_failed"]


# ------------------------------------------------------------- replay


def _completed_run(run_root: Path) -> list[dict[str, Any]]:
    async def scenario() -> None:
        engine = _journaled(run_root)
        engine.inject(_start())
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        await _close(engine)

    asyncio.run(scenario())
    return read_journal(run_root)


def test_a_replayed_input_that_raises_stops_resume_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine-made half (period-model ss11): the log holds a completion
    this build cannot apply. Resume refuses, and the error names the input."""
    run_root = tmp_path / "run"
    records = _completed_run(run_root)
    completion = next(
        r for r in records if r.get("source") == "adapter" and r["payload"].get("exit_code") == 0
    )
    _on_status(monkeypatch, "SUCCESS", _fault)

    async def scenario() -> EngineError:
        with pytest.raises(EngineError) as stopped:
            await resume_run(
                lower_source(_JIL),
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter()},
            )
        return stopped.value

    error = asyncio.run(scenario())
    assert str(error).startswith(f"{run_root}: resume stopped: replay stopped at input")
    assert (
        f"input {completion['seq']} (STATUS from adapter at {completion['at']}, request_id"
        f" engine:{completion['seq']}): RuntimeError: an injected fault on j"
    ) in str(error)
    assert "roll back to the release that wrote this store" in str(error)
    assert isinstance(error.__cause__, ReplayFault)
    assert error.__cause__.index == completion["seq"]
    assert error.code is None


def test_a_clean_log_replays_without_a_fault(tmp_path: Path) -> None:
    records = _completed_run(tmp_path / "run")
    oracle = Oracle(lower_source(_JIL))
    replay_inputs(oracle, records)
    assert oracle.store.job["j"].status == "SUCCESS"


@pytest.mark.parametrize(
    ("attempt", "what"),
    [
        (
            _attempt(3, kind="STARTJOB", payload={"job": "j"}, source="control"),
            "STARTJOB from control",
        ),
        (_attempt(3, kind=None), "time observation from no source"),
    ],
)
def test_the_replay_fault_names_the_input(attempt: Attempt, what: str) -> None:
    fault = ReplayFault(attempt, EngineError("diverged", code="clock_regressed"))
    assert str(fault) == (
        f"replay stopped at input 3 ({what} at {T0.isoformat()}, request_id r3):"
        " EngineError: diverged"
    )
    assert fault.code == "clock_regressed" and fault.index == 3
    assert ReplayFault(attempt, KeyError("k")).code is None


@pytest.mark.parametrize("value", ["refuse", "continue", "stop"])
def test_the_violation_option_is_inert_and_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """`--on-transition-violation` is accepted and inert since DL-353:
    given any of its values, `run` prints one deprecation line to stderr and
    serves exactly as without it. Without it, nothing is printed. A value it
    never accepted is still a usage error."""
    from typer.testing import CliRunner

    from dsl41 import cli_run
    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_JIL)
    seen: list[dict[str, Any]] = []

    async def serve(*args: Any, **kwargs: Any) -> int:
        seen.append({k: v for k, v in kwargs.items() if k != "reattach"})
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    base = ["run", str(jil), "--run-root", str(tmp_path / "root"), "--as-machine", "m1"]
    plain = CliRunner().invoke(app, base)
    given = CliRunner().invoke(app, [*base, "--on-transition-violation", value])
    assert plain.exit_code == given.exit_code == 0, given.output
    assert cli_run.INERT_VIOLATION_OPTION not in plain.stderr
    assert given.stderr.count(cli_run.INERT_VIOLATION_OPTION) == 1
    assert seen[0] == seen[1] and "on_violation" not in seen[0]
    bad = CliRunner().invoke(app, [*base, "--on-transition-violation", "ignore"])
    assert bad.exit_code == 2


def test_a_crash_exits_1_and_5_is_retired(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine that fails while running exits 1. Exit 5 was
    `--on-transition-violation stop` until DL-353; deployed units still
    list it in `RestartPreventExitStatus`, so no exit code of `run` takes it."""
    from dsl41 import cli_run
    from dsl41.runner_config import runtime_profile_from_cli

    assert 5 not in {value for name, value in vars(cli_run).items() if name.startswith("EXIT_")}

    async def halt(self: Engine, horizon: datetime) -> list[Event]:
        raise EngineError("test: a crash")

    monkeypatch.setattr(Engine, "run_until_quiescent", halt)
    exit_code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL),
            short_root / "root",
            False,
            [],
            profile=runtime_profile_from_cli(),
        )
    )
    assert exit_code == 1


# ------------------------------------------------------- review round 1


def _rehearse_bogus(tmp_path: Path, *, scenario: bool = True) -> Any:
    from typer.testing import CliRunner

    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_JIL)
    path = tmp_path / "scenario.json"
    path.write_text(
        '{"events":[{"at":"2026-07-01T08:00:00","kind":"STATUS",'
        '"payload":{"job":"j","status":"BOGUS"}}]}'
    )
    args = ["rehearse", str(jil), "--start", "2026-07-01T08:00:00", "--hours", "1"]
    return CliRunner().invoke(app, [*args, *(["--scenario", str(path)] if scenario else [])])


def test_rehearse_fails_on_a_scenario_event_that_faults(tmp_path: Path) -> None:
    """No silent loss. Before the dry apply the event raised and rehearse
    printed `rehearse failed: unknown status 'BOGUS'` with exit 1; it still
    does, though the event is now refused before the log."""
    result = _rehearse_bogus(tmp_path)
    assert result.exit_code == 1, result.output
    assert "rehearse failed:" in result.stderr
    assert "unknown status 'BOGUS'" in result.stderr
    assert _rehearse_bogus(tmp_path, scenario=False).exit_code == 0


def test_rehearse_names_the_refused_event_and_its_code(tmp_path: Path) -> None:
    err = _rehearse_bogus(tmp_path).stderr
    assert 'refused STATUS {"job": "j", "status": "BOGUS"} @ 2026-07-01T08:00:00:' in err
    assert ": apply_faulted: refused before admission" in err


def test_runs_refuses_a_root_whose_replay_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dsl41 runs` exits 2 naming the input, not with a traceback."""
    from genesis_harness import _genesis
    from typer.testing import CliRunner

    from dsl41.cli import app
    from dsl41.oracle_state import OracleError

    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _genesis(run_root)
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "a"}))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(scenario())
    assert CliRunner().invoke(app, ["runs", str(run_root)]).exit_code == 0

    def oracle_fault(store: RuntimeState, job: str) -> None:
        raise OracleError("an injected oracle fault")

    _on_status(monkeypatch, "STARTING", oracle_fault)
    result = CliRunner().invoke(app, ["runs", str(run_root)])
    assert result.exit_code == 2, result.output
    assert "replay failed (replay stopped at input" in result.stderr
    assert "an injected oracle fault" in result.stderr


def test_an_exact_retry_of_a_refused_request_is_refused_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is never noted in the decision index, so the retry is
    dry-applied again: the same code, and still nothing in the log."""
    _on_status(monkeypatch, "STARTING", _fault)

    async def scenario() -> list[str | None]:
        engine = _journaled(tmp_path / "run")
        codes: list[str | None] = []
        for _ in range(2):
            future = _submit_start(engine, "same-id")
            await engine.run_until_quiescent(T0)
            with pytest.raises(AdmissionRefused) as refused:
                await future
            codes.append(refused.value.code)
        assert engine.frontiers.committed_index == 0 and engine.deduped == []
        await _close(engine)
        return codes

    assert asyncio.run(scenario()) == ["apply_faulted", "apply_faulted"]
    records = read_journal(tmp_path / "run")
    assert not [r for r in records if r.get("rec") in ("input", "decision")]


# ------------------------------------------------------- review round 3

#: a condition-only consumer of FLAG: the flag sweep's case sets it
_GATED_JIL = "insert_job: gate\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"


def _fault_on_go(monkeypatch: pytest.MonkeyPatch) -> None:
    """SET_GLOBAL FLAG=go faults. Only the flag sweep sends one."""
    from dsl41.oracle_state import OracleError

    real = RuntimeState.set_global

    def set_global(self: RuntimeState, name: str, value: str) -> None:
        if value == "go":
            raise OracleError("an injected fault on FLAG=go")
        real(self, name, value)

    monkeypatch.setattr(RuntimeState, "set_global", set_global)


def test_a_refusal_met_only_in_a_sweep_fails_rehearse_naming_its_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main play is clean; the flag sweep's own SET_GLOBAL is refused.
    Its case would otherwise count the gated job as suppressed."""
    from typer.testing import CliRunner

    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_GATED_JIL)
    args = ["rehearse", str(jil), "--start", "2026-07-01T08:00:00", "--hours", "2"]
    clean = CliRunner().invoke(app, [*args, "--check-cadence", "--sweep", "flags"])
    assert clean.exit_code in (0, 3), clean.output
    _fault_on_go(monkeypatch)
    assert CliRunner().invoke(app, [*args, "--check-cadence"]).exit_code in (0, 3)
    swept = CliRunner().invoke(app, [*args, "--check-cadence", "--sweep", "flags"])
    assert swept.exit_code == 1, swept.output
    assert 'rehearse failed: refused SET_GLOBAL {"name": "FLAG", "value": "go"}' in swept.stderr
    assert "in sweep flags:FLAG='go'" in swept.stderr
    assert ": apply_faulted: " in swept.stderr


def test_a_sweep_raises_every_refusal_after_playing_every_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dsl41.derive import derive_graph
    from dsl41.rehearse_check import (
        CadencePolicy,
        Interpretation,
        SweepRefused,
        run_flag_sweep,
    )

    catalog = lower_source(_GATED_JIL)
    _fault_on_go(monkeypatch)
    with pytest.raises(SweepRefused) as refused:
        run_flag_sweep(
            catalog,
            derive_graph(catalog),
            {},
            FakeAdapter(),
            [],
            start=T0,
            horizon=T0 + timedelta(hours=2),
            reading=Interpretation(),
            injected_start=frozenset(),
            injected_force=frozenset(),
            policy=CadencePolicy(schema_version=1),
            parked=frozenset(),
            no_success_exit=frozenset(),
        )
    lost = refused.value.lost
    assert lost and all(item.case.startswith("flags:FLAG='go'") for item in lost)
    assert {item.kind for item in lost} == {"refused"}
    assert all(item.why.startswith("apply_faulted: ") for item in lost)
