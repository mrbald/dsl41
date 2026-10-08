"""The dry apply, the violation channel and the named replay fault
(concurrency-model ss4, period-model ss11).

A control input is applied first on `Oracle.fork()`, before the frontier
moves. An apply that raises refuses it with `apply_faulted`; an apply
that records a violation refuses it with `transition_violation`, unless
the engine runs `--on-transition-violation continue`. A refusal leaves
nothing in the WAL. An engine-made input is never refused: its move is
taken and the violation becomes a trace line, and under `stop` the
engine halts once the decision is durable. A logged input that raises on
replay stops replay with an error that names it.

No machine calls `StateMachine.take` yet, so every violation here is a
fake one, noted on the store's channel by a patched `transition`. The
fork leak test over the SEM corpus is the `fork` param of test_oracle.py.
"""

from __future__ import annotations

import asyncio

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fork_harness import ORACLE_COPIED, ORACLE_SHARED, STORE_COPIED, state_bytes
from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import VIOLATION_MARKER, Event, JobStatus, RuntimeState
from dsl41.runner import Engine, _Pending
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    VIOLATION_POLICIES,
    AdmissionRefused,
    ApplyResult,
    Attempt,
    Envelope,
    TransitionStop,
    apply_attempt,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_hosts import HostCommand
from dsl41.runner_journal import ReplayFault, read_journal, replay_inputs
from dsl41.runner_startup import resume_run, start_run
from dsl41.state_machine import TransitionError, Violation

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"

#: a violation no machine declares; it reaches the channel without `take`,
#: so the strict test plugin does not see it
FAKE = Violation("job_status", "job_status.99", "STARTING", "RUNNING", "a fake violation")


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


def _violate(store: RuntimeState, job: str) -> None:
    store.note_violation(job, FAKE)


def _fault(store: RuntimeState, job: str) -> None:
    raise RuntimeError(f"an injected fault on {job}")


def _strict(store: RuntimeState, job: str) -> None:
    raise TransitionError("a strict check failed")


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


def _markers(oracle: Oracle) -> list[tuple[str, str]]:
    return [(t.job, t.cause) for t in oracle.trace() if t.transition == VIOLATION_MARKER]


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
    until `fork` and `fork_harness.state_bytes` both name it."""
    oracle = Oracle(lower_source(_JIL))
    assert set(vars(oracle)) == ORACLE_COPIED | ORACLE_SHARED
    assert set(vars(oracle.store)) == STORE_COPIED
    oracle._window_starts = [("b", 1, "cause")]  # set only inside a box start
    twin = oracle.fork()
    for name in ORACLE_SHARED:
        assert getattr(twin, name) is getattr(oracle, name), name
    for name in ORACLE_COPIED:
        value = getattr(oracle, name)
        assert getattr(twin, name) == value or name == "store", name
        if not isinstance(value, (bool, type(None), datetime)):
            assert getattr(twin, name) is not value, name
    for name in STORE_COPIED:
        value = getattr(oracle.store, name)
        assert getattr(twin.store, name) == value, name
        if not isinstance(value, (bool, int)):
            assert getattr(twin.store, name) is not value, name
    oracle._window_starts = None
    assert oracle.fork()._window_starts is None


def test_a_dry_host_command_on_a_fork_leaves_the_routing_table_alone() -> None:
    """The SEM corpus never touches a host row, so the routing table's half
    of the leak test is here: a drain applied on a fork moves the fork's row
    and not the original's, and the real apply reaches the fork's result."""
    oracle = _engine().oracle
    attempt = _attempt(
        1,
        kind=None,
        host=HostCommand(verb="drain", host_id="local"),
        source="control",
        expect={"host:local": oracle.store.revision("host:local")},
    )
    before = state_bytes(oracle)
    twin = oracle.fork()
    dry = apply_attempt(twin, attempt)
    assert dry.result.decision == "applied"
    assert twin.store.hosts["local"].state == "passive"
    assert state_bytes(oracle) == before
    real = apply_attempt(oracle, attempt)
    assert real.result == dry.result
    assert state_bytes(oracle) == state_bytes(twin)


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


# ------------------------------------------------------ the violation channel


def test_the_channel_collects_violations_and_ignores_a_matched_move() -> None:
    store = RuntimeState()
    store.note_violation("j", None)
    assert store.drain_violations() == []
    store.note_violation("j", FAKE)
    store.note_violation("host:local", FAKE)
    assert store.drain_violations() == [("j", FAKE), ("host:local", FAKE)]
    assert store.drain_violations() == []


def test_a_violation_is_a_trace_line_and_rides_on_the_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_status(monkeypatch, "STARTING", _violate)
    oracle = Oracle(lower_source(_JIL))
    applied = apply_attempt(
        oracle, _attempt(1, kind="STARTJOB", payload={"job": "j"}, source="control")
    )
    assert applied.violations == [("j", FAKE)]
    assert _markers(oracle) == [
        ("j", "job_status.99 STARTING->RUNNING: a fake violation"),
    ]
    assert oracle.store.job["j"].status == "RUNNING"  # the move is taken
    assert oracle.store.drain_violations() == []  # the commit drained it


def test_a_clean_apply_records_no_violation() -> None:
    oracle = Oracle(lower_source(_JIL))
    applied = apply_attempt(
        oracle, _attempt(1, kind="STARTJOB", payload={"job": "j"}, source="control")
    )
    assert applied.violations == []
    assert _markers(oracle) == []


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
        (
            _Pending(
                at=T0,
                request_id="r",
                host=HostCommand(verb="drain", host_id="local"),
                envelope=_ENVELOPE,
            ),
            True,
        ),
        (
            _Pending(at=T0, request_id=None, host=HostCommand(verb="quarantine", host_id="local")),
            False,
        ),
        (_Pending(at=T0, request_id=None), False),  # a time observation
    ],
)
def test_only_a_socket_request_or_a_control_event_is_a_control_input(
    pending: _Pending, control: bool
) -> None:
    assert pending.control is control


# ------------------------------------------------------------- refusals


def _submit_start(engine: Engine, request_id: str = "r1") -> asyncio.Future[ApplyResult]:
    expect = {"job:j": engine.oracle.store.revision("job:j")}
    future = engine.submit(
        _start(), Envelope(request_id=request_id, expect=expect, epoch=engine.epoch)
    )
    assert isinstance(future, asyncio.Future)
    return future


@pytest.mark.parametrize(
    ("act", "code", "policy"),
    [
        (_fault, "apply_faulted", "refuse"),
        (_fault, "apply_faulted", "continue"),
        (_fault, "apply_faulted", "stop"),
        (_violate, "transition_violation", "refuse"),
        (_violate, "transition_violation", "stop"),
    ],
)
def test_a_control_input_whose_apply_fails_is_refused_before_the_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    act: Callable[[RuntimeState, str], None],
    code: str,
    policy: Any,
) -> None:
    """A refusal leaves nothing: no input record, no index, no moved state."""
    _on_status(monkeypatch, "STARTING", act)

    async def scenario() -> AdmissionRefused:
        engine = _journaled(tmp_path / "run")
        engine.on_violation = policy
        before = (engine.frontiers, state_bytes(engine.oracle))
        future = _submit_start(engine)
        await engine.run_until_quiescent(T0)
        with pytest.raises(AdmissionRefused) as refused:
            await future
        assert (engine.frontiers, state_bytes(engine.oracle)) == before
        assert [rid for rid, _ in engine.refusals] == ["r1"]
        await _close(engine)
        return refused.value

    refusal = asyncio.run(scenario())
    assert refusal.code == code
    assert str(refusal).startswith("refused before admission")
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    assert not [r for r in records if r.get("rec") in ("input", "decision")]


def test_a_clean_control_input_is_admitted_after_its_dry_apply(tmp_path: Path) -> None:
    async def scenario() -> ApplyResult:
        engine = _journaled(tmp_path / "run")
        future = _submit_start(engine)
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        result = await future
        assert engine.refusals == []
        assert engine.oracle.store.job["j"].status == "SUCCESS"
        await _close(engine)
        return result

    assert asyncio.run(scenario()).decision == "applied"
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    assert [r["request_id"] for r in records if r.get("rec") == "input"][0] == "r1"


def test_a_gate_rejection_is_still_a_logged_decision(tmp_path: Path) -> None:
    """The dry apply refuses on a fault or a violation only. A stale
    `expect` passes it and is rejected at step 6, at an index."""

    async def scenario() -> ApplyResult:
        engine = _journaled(tmp_path / "run")
        future = engine.submit(
            _start(), Envelope(request_id="stale", expect={"job:j": 7}, epoch=engine.epoch)
        )
        await engine.run_until_quiescent(T0)
        result = await future
        await _close(engine)
        return result

    result = asyncio.run(scenario())
    assert (result.decision, result.code) == ("rejected", "precondition_failed")
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    assert [r["request_id"] for r in records if r.get("rec") == "input"] == ["stale"]
    assert [r["code"] for r in records if r.get("rec") == "decision"] == ["precondition_failed"]


def test_continue_admits_a_violating_control_input_with_its_trace_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_status(monkeypatch, "STARTING", _violate)

    async def scenario() -> Engine:
        engine = _engine()
        engine.on_violation = "continue"
        engine.inject(_start())  # an in-process script's command
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.refusals == []
    assert engine.oracle.store.job["j"].run_number == 1  # the command applied
    assert _markers(engine.oracle) == [("j", "job_status.99 STARTING->RUNNING: a fake violation")]


def test_a_strict_check_raises_through_the_dry_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test plugin's `TransitionError` is not a fault to refuse: the
    test fails where the move was made."""
    _on_status(monkeypatch, "STARTING", _strict)

    async def scenario() -> None:
        engine = _engine()
        engine.inject(_start())
        try:
            with pytest.raises(TransitionError):
                await engine.run_until_quiescent(T0)
            assert engine.refusals == []
        finally:
            await engine.shutdown()

    asyncio.run(scenario())


# --------------------------------------------------------- engine-made facts


def test_an_engine_made_violation_is_taken_and_traced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter's SUCCESS breaks a (fake) transition. It cannot be
    refused, so it applies, leaves its trace line, and the engine goes on."""
    _on_status(monkeypatch, "SUCCESS", _violate)

    async def scenario() -> Engine:
        engine = _engine()
        engine.inject(_start())
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        await engine.shutdown()
        return engine

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["j"].status == "SUCCESS"
    assert engine.refusals == []
    assert [job for job, _ in _markers(engine.oracle)] == ["j"]


def test_stop_halts_after_the_decision_is_durable_and_resume_replays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "run"
    _on_status(monkeypatch, "SUCCESS", _violate)

    async def scenario() -> tuple[list[tuple[str, str]], Engine]:
        engine = _journaled(run_root)
        engine.on_violation = "stop"
        engine.inject(_start())
        try:
            with pytest.raises(TransitionStop, match="decision is") as stopped:
                await engine.run_until_quiescent(T0 + timedelta(seconds=1))
            assert stopped.value.code == "transition_violation"
            live = _markers(engine.oracle)
        finally:
            await _close(engine)
        resumed = await resume_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter()},
        )
        await _close(resumed)
        return live, resumed

    live, resumed = asyncio.run(scenario())
    records = read_journal(run_root / "journal.jsonl")
    completions = [r for r in records if r.get("rec") == "input" and r.get("source") == "adapter"]
    assert completions and completions[-1]["payload"].get("exit_code") == 0
    decided = {r["index"] for r in records if r.get("rec") == "decision"}
    assert completions[-1]["seq"] in decided  # the stop came after the decision
    # replay met the same violation and wrote the same line, without a raise
    assert resumed.oracle.store.job["j"].status == "SUCCESS"
    assert live and _markers(resumed.oracle) == live


def test_refuse_does_not_stop_on_an_engine_made_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contrast to `stop`: the same violation under the default leaves
    the engine running to quiescence."""
    _on_status(monkeypatch, "SUCCESS", _violate)

    async def scenario() -> Engine:
        engine = _journaled(tmp_path / "run")
        engine.inject(_start())
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        await _close(engine)
        return engine

    assert asyncio.run(scenario()).oracle.store.job["j"].status == "SUCCESS"


def test_stop_in_a_seal_drain_is_not_turned_into_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cutoff's own time observation is engine-made. A violation on it
    under `stop` halts the engine: no abort reopens C1, the seal is not
    answered and no `seal` record exists."""
    import dsl41.runner as runner_mod
    from test_boundary import C2_JIL, _genesis, _request, _stage

    run_root = tmp_path / "run"
    engine = _genesis(run_root)
    staged = _stage(run_root, C2_JIL)
    engine.on_violation = "stop"
    real_apply = runner_mod.apply_attempt

    def violating(oracle: Oracle, attempt: Attempt, **kwargs: Any) -> Any:
        applied = real_apply(oracle, attempt, **kwargs)
        if attempt.kind is None and attempt.host is None:
            applied.violations.append(("cutoff", FAKE))
        return applied

    monkeypatch.setattr(runner_mod, "apply_attempt", violating)

    async def scenario() -> None:
        sealed = engine.submit_seal(_request(engine, staged))
        try:
            with pytest.raises(TransitionStop):
                await engine.run_until_quiescent(T0)
            assert not sealed.done()
            assert engine.sealing is True  # no abort ran
        finally:
            await _close(engine)

    asyncio.run(scenario())
    assert engine.journal is not None
    assert not [r for r in read_journal(engine.journal.path) if r["rec"] == "seal"]


# ------------------------------------------------------------- replay


def _completed_run(run_root: Path) -> list[dict[str, Any]]:
    async def scenario() -> None:
        engine = _journaled(run_root)
        engine.inject(_start())
        await engine.run_until_quiescent(T0 + timedelta(seconds=1))
        await _close(engine)

    asyncio.run(scenario())
    return read_journal(run_root / "journal.jsonl")


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
    assert "roll back to the release that wrote this log" in str(error)
    assert isinstance(error.__cause__, ReplayFault)
    assert error.__cause__.index == completion["seq"]
    assert error.code is None


def test_a_clean_log_replays_without_a_fault(tmp_path: Path) -> None:
    records = _completed_run(tmp_path / "run")
    oracle = Oracle(lower_source(_JIL))
    replay_inputs(oracle, records)
    assert oracle.store.job["j"].status == "SUCCESS"


def test_a_strict_check_raises_through_replay_unwrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = _completed_run(tmp_path / "run")
    _on_status(monkeypatch, "SUCCESS", _strict)
    with pytest.raises(TransitionError):
        replay_inputs(Oracle(lower_source(_JIL)), records)


@pytest.mark.parametrize(
    ("attempt", "what"),
    [
        (
            _attempt(3, kind="STARTJOB", payload={"job": "j"}, source="control"),
            "STARTJOB from control",
        ),
        (
            _attempt(3, kind=None, host=HostCommand(verb="drain", host_id="h"), source="control"),
            "host drain from control",
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


def test_the_policies_are_the_cli_choices() -> None:
    from dsl41.cli_run import OnViolation

    assert tuple(choice.value for choice in OnViolation) == VIOLATION_POLICIES


@pytest.mark.parametrize("policy", VIOLATION_POLICIES)
def test_serve_sets_the_policy_before_the_loop_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: Any
) -> None:
    """`_serve_run` hands the option to the engine before the control
    socket binds, so no input is admitted under another policy."""
    from dsl41 import cli_run, runner_control
    from dsl41.period import runtime_profile_from_cli

    seen: list[str] = []

    async def bind(self: Any) -> None:
        seen.append(self.engine.on_violation)
        raise EngineError("test: stop at the bind")

    monkeypatch.setattr(runner_control.ControlServer, "start", bind)
    code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL),
            tmp_path / "root",
            False,
            [],
            profile=runtime_profile_from_cli(),
            on_violation=policy,
        )
    )
    assert code == 2 and seen == [policy]


def test_run_passes_the_option_and_defaults_to_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from dsl41 import cli_run
    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_JIL)
    seen: list[str] = []

    async def serve(*args: Any, on_violation: str, **kwargs: Any) -> int:
        seen.append(on_violation)
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    base = ["run", str(jil), "--run-root", str(tmp_path / "root"), "--as-machine", "m1"]
    for flags in ([], ["--on-transition-violation", "continue"]):
        result = CliRunner().invoke(app, [*base, *flags])
        assert result.exit_code == 0, result.output
    bad = CliRunner().invoke(app, [*base, "--on-transition-violation", "ignore"])
    assert bad.exit_code == 2
    assert seen == ["refuse", "continue"]


@pytest.mark.parametrize(
    ("raised", "code"),
    [(TransitionStop("test: a stop"), 5), (EngineError("test: a crash"), 1)],
)
def test_a_transition_stop_exits_5_and_a_crash_exits_1(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, raised: EngineError, code: int
) -> None:
    """`--on-transition-violation stop` has its own exit code, so the engine
    units' `RestartPreventExitStatus=2 3 5` keeps a stopped engine down. 4 is
    the control verbs' unknown outcome; 5 collides with no verb's code."""
    from dsl41 import cli_run
    from dsl41.period import runtime_profile_from_cli

    assert cli_run.EXIT_TRANSITION_STOP == 5

    async def halt(self: Engine, horizon: datetime) -> list[Event]:
        raise raised

    monkeypatch.setattr(Engine, "run_until_quiescent", halt)
    exit_code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL),
            short_root / "root",
            False,
            [],
            profile=runtime_profile_from_cli(),
            on_violation="stop",
        )
    )
    assert exit_code == code


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


_TERM_JIL = "insert_job: t\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n"


def _set_global(at: datetime) -> Event:
    return Event(at=at, kind="SET_GLOBAL", payload={"name": "G", "value": "1"})


@pytest.mark.parametrize("policy", ["refuse", "stop"])
def test_a_violation_in_the_time_half_does_not_refuse_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: Any
) -> None:
    """`t`'s term_run_time kill is due at the SET_GLOBAL's stamp and fires
    in the command's batch. The kill's (fake) violation is the engine's
    own: the command applies, the line is traced, and under `stop` the
    engine stops once THIS batch commits."""
    _on_status(monkeypatch, "TERMINATED", _violate)
    later = T0 + timedelta(minutes=1)

    async def scenario() -> tuple[Engine, BaseException | None]:
        engine = start_run(
            lower_source(_TERM_JIL),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.on_violation = policy
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "t"}), source=None)
        await engine.run_until_quiescent(T0)
        engine.inject(_set_global(later))
        stopped: BaseException | None = None
        try:
            await engine.run_until_quiescent(later)
        except TransitionStop as exc:
            stopped = exc
        await _close(engine)
        return engine, stopped

    engine, stopped = asyncio.run(scenario())
    assert engine.refusals == []
    assert engine.oracle.store.global_value("G") == "1"
    assert engine.oracle.store.job["t"].status == "TERMINATED"
    assert [job for job, _ in _markers(engine.oracle)] == ["t"]
    assert (stopped is not None) is (policy == "stop")
    records = read_journal(tmp_path / "run" / "journal.jsonl")
    command = next(r for r in records if r.get("kind") == "SET_GLOBAL")
    assert command["seq"] in {r["index"] for r in records if r.get("rec") == "decision"}


def test_the_batch_tells_the_time_half_from_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_status(monkeypatch, "TERMINATED", _violate)
    oracle = Oracle(lower_source(_TERM_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "t"}))
    applied = apply_attempt(
        oracle,
        _attempt(
            2,
            kind="SET_GLOBAL",
            at=T0 + timedelta(minutes=1),
            payload={"name": "G", "value": "1"},
            source="control",
        ),
    )
    assert applied.violations == [("t", FAKE)]
    assert applied.command_violations == []


def test_a_time_half_that_raises_reports_its_violations_as_the_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def violate_then_fail(store: RuntimeState, job: str) -> None:
        store.note_violation(job, FAKE)
        raise RuntimeError("the time half failed")

    _on_status(monkeypatch, "TERMINATED", violate_then_fail)
    oracle = Oracle(lower_source(_TERM_JIL))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "t"}))
    batch = oracle.batch(T0 + timedelta(minutes=1))
    with pytest.raises(RuntimeError, match="time half failed"):
        batch.__enter__()
    assert batch.violations == [("t", FAKE)]
    assert batch.command_violations == []
    assert [job for job, _ in _markers(oracle)] == ["t"]


def test_a_fork_does_not_carry_a_pending_violation() -> None:
    oracle = Oracle(lower_source(_JIL))
    oracle.store.note_violation("j", FAKE)
    assert oracle.fork().store.drain_violations() == []
    assert oracle.store.drain_violations() == [("j", FAKE)]


def test_strict_mode_refuses_a_violation_noted_outside_an_input() -> None:
    """The test plugin sets the strict variable for the whole suite, so any
    `take` site outside an input fails the suite at the next input."""
    store = RuntimeState()
    store.note_violation("host:local", FAKE)
    with pytest.raises(TransitionError, match="noted outside an InputBatch"):
        store.begin_input()
    store.begin_input()  # the orphan was dropped, not kept
    store.commit_input()


def test_production_drops_an_orphan_violation_to_stderr_with_no_trace_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41.state_machine import STRICT_ENV

    monkeypatch.delenv(STRICT_ENV, raising=False)
    oracle = Oracle(lower_source(_JIL))
    oracle.store.note_violation("host:local", FAKE)
    oracle.feed(_start())
    assert _markers(oracle) == []
    err = capsys.readouterr().err
    assert err.count("dropped a transition violation noted outside an InputBatch") == 1
    assert "host:local job_status.99 STARTING->RUNNING: a fake violation" in err
    oracle.feed(_start(T0 + timedelta(minutes=1)))
    assert capsys.readouterr().err == ""  # written once


def test_runs_refuses_a_root_whose_replay_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dsl41 runs` exits 2 naming the input, not with a traceback."""
    from typer.testing import CliRunner

    from dsl41.cli import app
    from dsl41.oracle_state import OracleError
    from test_boundary import _genesis

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
    _on_status(monkeypatch, "STARTING", _violate)

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

    assert asyncio.run(scenario()) == ["transition_violation", "transition_violation"]
    records = read_journal(tmp_path / "run" / "journal.jsonl")
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
    cases = [case for case, _, _ in refused.value.refusals]
    assert cases and all(case.startswith("flags:FLAG='go'") for case in cases)
    assert {r.code for _, _, r in refused.value.refusals} == {"apply_faulted"}
