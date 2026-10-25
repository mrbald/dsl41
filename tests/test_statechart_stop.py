"""A `StatechartError` is an assertion failure, never a fault to contain
(docs/statechart-engine.md §5): DL-320's classification lets it through, the
engine stops before any durable write and exits 7 with its report under one
stderr prefix, and the supervisor lets it through its belts to the same stop."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dsl41 import runner_admission, runner_journal
from dsl41.ir import lower_source
from dsl41.oracle import InputBatch, Oracle
from dsl41.oracle_state import Event, JobStatus, OracleError, RuntimeState
from dsl41.period import estate_wal
from dsl41.runner import Engine, _Pending
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import Attempt, classify_fault, trial_apply
from dsl41.runner_clock import VirtualClock
from dsl41.runner_journal import ReplayFault, read_journal, replay_period
from dsl41.runner_startup import start_run
from dsl41.state_machine import (
    EXIT_STATECHART_ERROR,
    STATECHART_LOG_PREFIX,
    StatechartError,
    UnhandledEvent,
)

T0 = datetime(2026, 7, 1, 8, 0)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"


def _error() -> UnhandledEvent:
    error = UnhandledEvent("job j: no row and no deferral for STARTJOB")
    error.facts = [("machine", "job"), ("instance", "'j'")]
    return error


# ---- (1) DL-320's classification never catches it


class _Forkable:
    def fork(self) -> _Forkable:
        return self


def test_trial_apply_and_classify_fault_let_a_statechart_error_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def apply(*args: Any, **kwargs: Any) -> Any:
        raise _error()

    monkeypatch.setattr(runner_admission, "apply_attempt", apply)
    attempt = Attempt(index=1, at=T0, request_id="engine:1", fingerprint="fp")
    with pytest.raises(UnhandledEvent):
        trial_apply(_Forkable(), attempt)  # type: ignore[arg-type]
    with pytest.raises(UnhandledEvent):
        classify_fault(_Forkable(), attempt)  # type: ignore[arg-type]


def test_any_other_fault_is_still_returned_for_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def apply(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("an ordinary fault")

    monkeypatch.setattr(runner_admission, "apply_attempt", apply)
    attempt = Attempt(index=1, at=T0, request_id="engine:1", fingerprint="fp")
    assert isinstance(trial_apply(_Forkable(), attempt), RuntimeError)  # type: ignore[arg-type]


_HOLDER = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    "insert_job: h\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=A)\n"
)


def _holder_oracle() -> Oracle:
    """A job running on a unit of R: its success releases the unit."""
    oracle = Oracle(lower_source(_HOLDER))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "h"}))
    return oracle


def _fail_to_release(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """The release raises `error` after the row has landed its terminal status:
    the batch's cleanup then finds a terminal row that still holds capacity."""

    def release(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(RuntimeState, "release_reservations", release)


def _success() -> Attempt:
    return Attempt(
        index=2,
        at=T0,
        request_id="engine:2",
        fingerprint="fp",
        kind="STATUS",
        payload={"job": "h", "status": "SUCCESS"},
    )


def test_a_statechart_error_in_a_release_is_not_replaced_by_the_batch_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch's cleanup refuses a terminal row that still holds capacity,
    which would replace the statechart error in flight with an `OracleError`
    and let the dry apply journal an `engine_apply_faulted` quarantine. The
    error leaves unchanged, with the cleanup's finding as a note."""
    oracle = _holder_oracle()
    error = _error()
    _fail_to_release(monkeypatch, error)
    with pytest.raises(UnhandledEvent) as raised:
        trial_apply(oracle, _success())
    assert raised.value is error
    assert any("the batch's cleanup then raised OracleError" in n for n in error.__notes__)
    with pytest.raises(UnhandledEvent) as classified:
        classify_fault(oracle, _success())
    assert classified.value is error  # nothing is returned as a rejection to journal


def test_any_other_fault_in_a_release_is_still_replaced_by_the_cleanup_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same route with an ordinary fault: today's behaviour, unchanged."""
    oracle = _holder_oracle()
    _fail_to_release(monkeypatch, RuntimeError("an ordinary fault"))
    fault = trial_apply(oracle, _success())
    assert isinstance(fault, OracleError) and "holds" in str(fault)
    assert isinstance(fault.__context__, RuntimeError)


def _fail_to_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    def commit(self: Any, *, time_half: bool = False) -> None:
        raise OracleError("the cleanup's own finding")

    monkeypatch.setattr(InputBatch, "_commit", commit)


@pytest.mark.parametrize("fault", [_error(), RuntimeError("an ordinary fault")])
def test_the_batch_cleanup_on_entry_keeps_only_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch, fault: Exception
) -> None:
    """The time half's cleanup on the way out of `__enter__`: a statechart error
    in flight survives it, with a note, and any other exception is replaced."""
    oracle = Oracle(lower_source(_HOLDER))

    def break_clock(*args: Any, **kwargs: Any) -> None:
        raise fault

    monkeypatch.setattr(Oracle, "_lazy_clock_checks", break_clock)
    _fail_to_commit(monkeypatch)
    with pytest.raises(Exception) as raised:
        oracle.batch(T0).__enter__()
    if isinstance(fault, StatechartError):
        assert raised.value is fault
        assert any("the cleanup's own finding" in n for n in fault.__notes__)
    else:
        assert isinstance(raised.value, OracleError) and raised.value is not fault


@pytest.mark.parametrize("fault", [_error(), RuntimeError("an ordinary fault")])
def test_the_batch_cleanup_on_exit_keeps_only_a_statechart_error(
    monkeypatch: pytest.MonkeyPatch, fault: Exception
) -> None:
    """The same for `__exit__`, where the attempt half raised."""
    oracle = Oracle(lower_source(_HOLDER))
    _fail_to_commit(monkeypatch)
    with pytest.raises(Exception) as raised, oracle.batch(T0):
        raise fault
    if isinstance(fault, StatechartError):
        assert raised.value is fault
        assert any("the cleanup's own finding" in n for n in fault.__notes__)
    else:
        assert isinstance(raised.value, OracleError) and raised.value is not fault


def _raise_on_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared machine that finds no row when a job starts."""
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
        if new in ("STARTING", "RUNNING"):
            raise _error()
        real(self, job, new, at, exit_code, clear_exit_code=clear_exit_code)

    monkeypatch.setattr(RuntimeState, "transition", transition)


@pytest.mark.parametrize("source", ["scheduler", "control"])
def test_the_engine_stops_before_any_durable_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """An engine-made input is not quarantined and a control input is not
    refused: the error leaves the engine, and nothing of the input is logged."""
    run_root = tmp_path / "run"

    async def scenario() -> tuple[int, int]:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        await engine.run_until_quiescent(T0)
        before = len(read_journal(run_root / "journal.jsonl"))
        start = Event(at=T0, kind="STARTJOB", payload={"job": "j"})
        start.source = source
        with pytest.MonkeyPatch.context() as mp:
            _raise_on_running(mp)
            with pytest.raises(UnhandledEvent):
                await engine._admit_and_apply(_Pending(at=T0, request_id=None, ev=start))
        assert engine.quarantined == []
        assert engine.oracle.store.job["j"].status == "INACTIVE"
        after = len(read_journal(run_root / "journal.jsonl"))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        return before, after

    before, after = asyncio.run(scenario())
    assert after == before


def _journaled_start(run_root: Path) -> None:
    """A run root whose log holds one applied STARTJOB, input 1."""

    async def scenario() -> None:
        engine = start_run(
            lower_source(_JIL),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
        engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "j"}), source="control")
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())


def _replay_meets_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def apply(*args: Any, **kwargs: Any) -> Any:
        raise _error()

    monkeypatch.setattr(runner_journal, "apply_attempt", apply)


WAL_FACT = "WAL input: 1: STARTJOB for job j from control"


def test_replay_stops_on_a_statechart_error_as_itself_naming_the_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not wrapped as a `ReplayFault`: the error reaches the stop, and its report names
    the WAL input as a `ReplayFault` would."""
    run_root = tmp_path / "run"
    _journaled_start(run_root)
    _replay_meets_one(monkeypatch)
    with pytest.raises(UnhandledEvent) as raised:
        replay_period(Oracle(lower_source(_JIL)), read_journal(estate_wal(run_root)))
    assert not isinstance(raised.value, ReplayFault)
    assert WAL_FACT in str(raised.value).splitlines()


def test_replay_names_an_undecided_input_whose_dry_apply_meets_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash window: input 1 has no decision, so replay classifies it on a fork
    first. The error is not classified; it stops replay naming the input."""
    run_root = tmp_path / "run"
    _journaled_start(run_root)
    records = read_journal(estate_wal(run_root))
    decided = next(
        i for i, r in enumerate(records) if r.get("rec") == "decision" and r.get("index") == 1
    )
    records = records[:decided]  # the crash came after the attempt and before its decision

    def apply(*args: Any, **kwargs: Any) -> Any:
        raise _error()

    monkeypatch.setattr(runner_admission, "apply_attempt", apply)
    with pytest.raises(UnhandledEvent) as raised:
        replay_period(Oracle(lower_source(_JIL)), records)
    assert WAL_FACT in str(raised.value).splitlines()


def test_an_offline_reader_exits_7_with_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dsl41 journal` replays the log; every reader verb is wrapped the same way."""
    from typer.testing import CliRunner

    from dsl41.cli import app

    root = tmp_path / "run"
    assert _rehearse(tmp_path, "--run-root", str(root)).exit_code == 0
    _replay_meets_one(monkeypatch)
    result = CliRunner().invoke(app, ["journal", str(root)])
    assert result.exit_code == 7, result.output
    assert f"{STATECHART_LOG_PREFIX}: job j: no row and no deferral for STARTJOB" in result.output
    assert f"{STATECHART_LOG_PREFIX}: WAL input: 1: " in result.output


def test_a_resume_that_meets_one_exits_7_and_releases_the_lock(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41 import cli_run, runner_startup
    from dsl41.period import runtime_profile_from_cli
    from dsl41.runner_startup import acquire_run_root

    run_root = short_root / "root"
    profile = runtime_profile_from_cli()

    async def crash(self: Engine, horizon: datetime) -> list[Event]:
        raise RuntimeError("test: stop the genesis run at once")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Engine, "run_until_quiescent", crash)
        assert (
            asyncio.run(
                cli_run._serve_run(
                    lower_source(_JIL), run_root, False, [], profile=profile, supervisor="spawn"
                )
            )
            == 1
        )

    def replay(*args: Any, **kwargs: Any) -> Any:
        error = _error()
        error.facts.append(("WAL input", "1: STARTJOB for job j from control"))
        raise error

    monkeypatch.setattr(runner_startup, "replay_inputs", replay)
    capsys.readouterr()
    code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL), run_root, True, [], profile=profile, supervisor="spawn"
        )
    )
    assert code == 7
    err = capsys.readouterr().err
    assert f"{STATECHART_LOG_PREFIX}: {WAL_FACT}" in err.splitlines()
    acquire_run_root(run_root).release()  # the lock went with the stop


# ---- (3) the engine prints the report and exits 7


def test_dsl41_run_exits_7_with_the_report_under_one_prefix(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41 import cli_run
    from dsl41.period import runtime_profile_from_cli

    assert cli_run.EXIT_STATECHART_ERROR == EXIT_STATECHART_ERROR == 7

    async def halt(self: Engine, horizon: datetime) -> list[Event]:
        raise _error()

    monkeypatch.setattr(Engine, "run_until_quiescent", halt)
    code = asyncio.run(
        cli_run._serve_run(
            lower_source(_JIL),
            short_root / "root",
            False,
            [],
            profile=runtime_profile_from_cli(),
            supervisor="spawn",
        )
    )
    assert code == 7
    err = [line for line in capsys.readouterr().err.splitlines() if STATECHART_LOG_PREFIX in line]
    assert err == [
        f"{STATECHART_LOG_PREFIX}: job j: no row and no deferral for STARTJOB",
        f"{STATECHART_LOG_PREFIX}: machine: job",
        f"{STATECHART_LOG_PREFIX}: instance: 'j'",
    ]
    note = cli_run._reattach_note(None, short_root, detached=True, code=code)
    assert note is not None and note.startswith("detached: reattach")


def test_the_engine_units_do_not_restart_exit_7() -> None:
    deploy = Path(__file__).resolve().parent.parent / "examples" / "nightbank" / "deploy"
    for unit in ("dsl41-engine.service", "dsl41-supervisor.service"):
        (line,) = [
            line
            for line in (deploy / unit).read_text(encoding="utf-8").splitlines()
            if line.startswith("RestartPreventExitStatus=")
        ]
        assert "7" in line.split("=", 1)[1].split(), unit


# ---- (4) the supervisor lets it through its belts to a stop


def test_the_supervisor_dispatch_belt_lets_a_statechart_error_through(
    queued_supervisor: Any,
) -> None:
    from dsl41 import runner_supervisor

    sup, connect = queued_supervisor
    conn, peer = connect()

    def ping(_conn: Any, _req: Any) -> Any:
        raise runner_supervisor.StatechartError("supervisor_lease: no row for RENEW")

    sup._h_ping = ping
    peer.sendall(b'{"v":1,"cmd":"PING"}\n')
    with pytest.raises(runner_supervisor.StatechartError):
        sup._readable(conn)


def test_a_statechart_error_in_a_shutdown_wait_is_not_logged_as_a_retry(
    queued_supervisor: Any, capsys: pytest.CaptureFixture[str], stand: Any
) -> None:
    """The wait reads the lease (`_reap`, `_push_exit`), so the chart can stop the
    supervisor inside a SHUTDOWN. That is a stop, not a failed wait to run again,
    so the "running it once more" line is not written. An OSError there is the
    non-triggering case: it is logged as a retry. The SHUTDOWN's row moved the
    process to shutting_down, so it serves again before the second request."""
    from dsl41 import runner_supervisor

    sup, connect = queued_supervisor
    conn, _peer = connect()
    granted = sup._h_acquire(conn, {"controller_id": "c"})
    creds = {"incarnation": sup.incarnation, "token": granted["token"]}
    errors = iter([runner_supervisor.StatechartError("stop"), OSError(5, "synthetic")])

    def wait() -> None:
        raise next(errors)

    sup._shutdown_wait = wait
    with pytest.raises(runner_supervisor.StatechartError):
        sup._h_shutdown(conn, creds)
    assert "running it once more" not in capsys.readouterr().err
    stand(sup, "serving")
    with pytest.raises(OSError):
        sup._h_shutdown(conn, creds)
    assert "running it once more" in capsys.readouterr().err


def test_the_supervisor_still_answers_any_other_handler_error(queued_supervisor: Any) -> None:
    sup, connect = queued_supervisor
    conn, peer = connect()

    def ping(_conn: Any, _req: Any) -> Any:
        raise RuntimeError("one request's problem")

    sup._h_ping = ping
    peer.sendall(b'{"v":1,"cmd":"PING"}\n')
    sup._readable(conn)
    assert sup._conns


def test_the_supervisor_read_belt_lets_a_statechart_error_through(
    queued_supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41 import runner_supervisor

    sup, connect = queued_supervisor
    conn, _peer = connect()

    def read(_conn: Any) -> None:
        raise runner_supervisor.StatechartError("a machine broke while reading")

    monkeypatch.setattr(sup, "_read_conn", read)
    with pytest.raises(runner_supervisor.StatechartError):
        sup._readable(conn)


def test_the_supervisor_exits_7_when_its_construction_meets_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41 import runner_supervisor

    class Failing:
        def __init__(self, run_root: str, deadman_s: float | None = None) -> None:
            raise runner_supervisor.StatechartError("supervisor_lease: restore refused")

    monkeypatch.setattr(runner_supervisor, "Supervisor", Failing)
    assert runner_supervisor.main(["--run-root", str(tmp_path)]) == 7
    assert capsys.readouterr().err.splitlines() == [
        f"{STATECHART_LOG_PREFIX}: supervisor_lease: restore refused"
    ]


def test_the_supervisor_exits_7_with_the_report_under_one_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dsl41 import runner_supervisor

    class Stopping:
        def __init__(self, run_root: str, deadman_s: float | None = None) -> None:
            pass

        def run(self) -> int:
            error = runner_supervisor.StatechartError("supervisor_lease: two rows enabled")
            error.facts = [("machine", "supervisor_lease")]
            raise error

    monkeypatch.setattr(runner_supervisor, "Supervisor", Stopping)
    assert runner_supervisor.main(["--run-root", str(tmp_path)]) == 7
    assert capsys.readouterr().err.splitlines() == [
        f"{STATECHART_LOG_PREFIX}: supervisor_lease: two rows enabled",
        f"{STATECHART_LOG_PREFIX}: machine: supervisor_lease",
    ]


def test_every_executor_error_is_one_base() -> None:
    from dsl41.state_machine import AmbiguousTransition, ReentrantDispatch

    for error in (AmbiguousTransition, ReentrantDispatch, UnhandledEvent):
        assert issubclass(error, StatechartError)


# ---- (3) rehearse stops the same way


_TICKED_JIL = 'insert_job: d\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\nstart_times: "09:00"\n'


def _rehearse(tmp_path: Path, *extra: str) -> Any:
    from typer.testing import CliRunner

    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_TICKED_JIL)
    args = ["rehearse", str(jil), "--start", "2026-07-01T08:00:00", "--hours", "2", *extra]
    return CliRunner().invoke(app, args)


def test_rehearse_exits_7_with_the_report_under_one_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Oracle._fire_timers_due

    def fire(self: Oracle, at: datetime) -> None:
        if at >= datetime(2026, 7, 1, 8, 30):
            raise _error()
        real(self, at)

    monkeypatch.setattr(Oracle, "_fire_timers_due", fire)
    result = _rehearse(tmp_path)
    assert result.exit_code == 7, result.output
    assert f"{STATECHART_LOG_PREFIX}: job j: no row and no deferral for STARTJOB" in result.output
    assert f"{STATECHART_LOG_PREFIX}: machine: job" in result.output


def test_rehearse_exits_7_when_a_cadence_sweep_meets_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dsl41 import cli_run

    def check(*args: Any, **kwargs: Any) -> None:
        raise _error()

    monkeypatch.setattr(cli_run, "_emit_cadence_check", check)
    result = _rehearse(tmp_path, "--check-cadence")
    assert result.exit_code == 7, result.output
    assert f"{STATECHART_LOG_PREFIX}: machine: job" in result.output


# ---- every verb that runs, replays or reads back an estate is wrapped

WRAPPED_VERBS = [
    ("run", "cli_run", "run"),
    ("rehearse", "cli_run", "rehearse"),
    ("journal", "cli_run", "journal"),
    ("runs", "cli_run", "runs"),
    ("seal", "cli_estate", "seal"),
    ("audit", "cli_estate", "audit"),
    ("verify", "cli_estate", "verify"),
    ("estate reclaim", "cli_estate", "estate_reclaim"),
    ("estate prune", "cli_estate", "estate_prune"),
]


def _registered(verb: str) -> Any:
    """The callback cli.py registered for `verb`, a top-level verb or `estate <sub>`."""
    from dsl41 import cli

    app, name = (cli.estate_app, verb.split()[1]) if verb.startswith("estate ") else (cli.app, verb)
    for info in app.registered_commands:
        assert info.callback is not None
        if (info.name or info.callback.__name__.replace("_", "-")) == name:
            return info.callback
    raise AssertionError(f"{verb} is not registered")


@pytest.mark.parametrize(("verb", "module", "function"), WRAPPED_VERBS)
def test_each_estate_verb_is_registered_inside_the_statechart_stop(
    verb: str, module: str, function: str
) -> None:
    import importlib

    from dsl41.cli_common import stops_on_statechart_error

    callback = _registered(verb)
    guarded = stops_on_statechart_error(lambda: None)
    assert callback.__code__ is guarded.__code__, f"{verb} is registered unwrapped"
    assert callback.__wrapped__ is getattr(importlib.import_module(f"dsl41.{module}"), function)
