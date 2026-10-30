"""An engine-made input whose apply raises (concurrency-model ss4).

The engine dry-applies every engine-made input on a fork before it logs
it. If the apply raises and the batch's time half applies alone on a
second fork, the input is quarantined: logged with a rejected decision
under `engine_apply_faulted`, its time half applied, its attempt not. If
the time half raises too, the engine stops before the append with
`TimeHalfStop`, and `dsl41 run` exits 6. Replay classifies an undecided
attempt that raises the same way, so a log an older build left poisoned
resumes through step 6a.

Faults are injected the way test_dry_apply.py injects them: a patched
`RuntimeState.transition` raises after a job moves into one status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fork_harness import state_bytes

from dsl41.ir import lower_source
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event, JobStatus, RuntimeState
from dsl41.runner import Engine, _Pending
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_admission import (
    QUARANTINE_LOG_PREFIX,
    AdmissionRefused,
    ApplyResult,
    Envelope,
    TimeHalfStop,
    quarantine_verdict,
)
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_hosts import HostCommand
from dsl41.runner_journal import read_journal, replay_inputs
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)
LATER = T0 + timedelta(minutes=1)
_JIL = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n"
#: `t` is killed by its term_run_time timer one minute after it starts:
#: the kill is the time half of the first input stamped at or after LATER
_TERM_JIL = "insert_job: t\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n\n" + _JIL
CODE = "engine_apply_faulted"


def _on_status(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    """Raise after every move of any job into `status`."""
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
            raise RuntimeError(f"an injected fault on {job}")

    monkeypatch.setattr(RuntimeState, "transition", transition)


def _commit_fault(monkeypatch: pytest.MonkeyPatch, job: str, statuses: set[str]) -> None:
    """Raise at commit time, in the batch's `__exit__`, when `job` stands
    in one of `statuses`: the check cannot tell which half moved it."""
    real = RuntimeState.commit_input

    def commit_input(self: RuntimeState) -> Any:
        if self.job[job].status in statuses:
            raise RuntimeError(f"an injected commit fault on {job}")
        return real(self)

    monkeypatch.setattr(RuntimeState, "commit_input", commit_input)


def _start(at: datetime = T0, job: str = "j") -> Event:
    return Event(at=at, kind="STARTJOB", payload={"job": job})


def _journaled(run_root: Path, text: str = _JIL, *, adapter: FakeAdapter | None = None) -> Engine:
    return start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter or FakeAdapter()},
    )


async def _resume(run_root: Path, text: str = _JIL) -> Engine:
    return await resume_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
    )


async def _close(engine: Engine) -> None:
    await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()


def _records(run_root: Path) -> list[dict[str, Any]]:
    return read_journal(run_root / "journal.jsonl")


def _decision(records: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    found = [r for r in records if r.get("rec") == "decision" and r["index"] == index]
    assert len(found) <= 1
    return found[0] if found else None


def _dump(engine: Engine) -> list[dict[str, Any]]:
    return [entry.model_dump(mode="json") for entry in engine.oracle.trace()]


# ------------------------------------------------------- one engine-made kind each


def _fault_on_host_quarantine(monkeypatch: pytest.MonkeyPatch) -> None:
    import dsl41.runner_admission as admission

    real = admission.apply_host_command

    def apply_host_command(store: RuntimeState, cmd: HostCommand, **kwargs: Any) -> None:
        real(store, cmd, **kwargs)
        if cmd.verb == "quarantine":
            raise RuntimeError("an injected fault on host local")

    monkeypatch.setattr(admission, "apply_host_command", apply_host_command)


#: kind -> (how to arm its fault, how the reason names the input)
_KINDS: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], str]] = {
    "tick": (lambda mp: _on_status(mp, "STARTING"), "STARTJOB for job j from scheduler"),
    "completion": (
        lambda mp: _on_status(mp, "SUCCESS"),
        "STATUS for job j run 1 from adapter",
    ),
    "reconcile": (
        lambda mp: _on_status(mp, "SUCCESS"),
        "STATUS for job j run 1 from reconcile",
    ),
    "routing": (_fault_on_host_quarantine, "host quarantine of local from reconcile"),
}


async def _play_kind(kind: str, engine: Engine) -> None:
    """Raise one engine-made input of `kind`, after any control input the
    scenario needs, and run to quiescence."""
    if kind == "tick":
        engine.inject(_start(), source="scheduler")
    elif kind == "completion":
        engine.inject(_start(), source="control")  # FakeAdapter completes it
    elif kind == "reconcile":
        engine.inject(_start(), source="control")
        await engine.run_until_quiescent(T0)
        engine.inject(
            Event(
                at=LATER,
                kind="STATUS",
                payload={"job": "j", "status": "SUCCESS", "run_number": 1, "exit_code": 0},
            ),
            source="reconcile",
        )
    else:
        engine.note_executor_unreachable()
    await engine.run_until_quiescent(LATER)


#: kind -> where its quarantined input leaves the entity it would have moved
_HELD = {"tick": "INACTIVE", "completion": "RUNNING", "reconcile": "RUNNING", "routing": "active"}


def _held(kind: str, engine: Engine) -> str:
    store = engine.oracle.store
    return store.hosts["local"].state if kind == "routing" else store.job["j"].status


@contextmanager
def _faulted(kind: str, armed: bool) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        if armed:
            _KINDS[kind][0](mp)
        yield


def _the_input(kind: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    rec = {"routing": "host"}.get(kind, "input")
    source = {"tick": "scheduler", "completion": "adapter"}.get(kind, "reconcile")
    found = [r for r in records if r.get("rec") == rec and r.get("source") == source]
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.parametrize("armed", [True, False])
def test_an_engine_made_input_whose_apply_raises_is_quarantined_and_the_engine_runs_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, armed: bool
) -> None:
    """Live: the engine continues, and the WAL holds the input and its
    rejected decision with the code. The reason names the input and the
    fault, and one stderr line says so. Unarmed, the input applies."""
    run_root = tmp_path / "run"
    adapter = FakeAdapter(default=None) if kind == "reconcile" else FakeAdapter()

    async def scenario() -> Engine:
        engine = _journaled(run_root, adapter=adapter)
        with _faulted(kind, armed):
            await _play_kind(kind, engine)
        await _close(engine)
        return engine

    engine = asyncio.run(scenario())
    records = _records(run_root)
    attempt = _the_input(kind, records)
    decided = _decision(records, attempt["seq"])
    assert decided is not None
    err = capsys.readouterr().err
    if not armed:
        assert (decided["decision"], decided["code"]) == ("applied", None)
        assert engine.quarantined == [] and QUARANTINE_LOG_PREFIX not in err
        return
    assert (decided["decision"], decided["code"]) == ("rejected", CODE)
    what = _KINDS[kind][1]
    assert decided["reason"].startswith(f"quarantined: applying {what} at ")
    assert "raised RuntimeError: an injected fault on" in decided["reason"]
    assert [result.index for result, _ in engine.quarantined] == [attempt["seq"]]
    assert engine.drops == []  # a quarantined input is on one list: `quarantined`
    assert isinstance(engine.quarantined[0][1], RuntimeError)
    assert engine.quarantined[0][1].__traceback__ is None  # it pins no fork's frames
    assert err.count(f"{QUARANTINE_LOG_PREFIX}: input {attempt['seq']}: quarantined:") == 1
    # the input's own effect is what the fault cost, and nothing else
    assert _held(kind, engine) == _HELD[kind]


@pytest.mark.parametrize("kind", list(_KINDS))
def test_a_quarantine_replays_on_the_same_build_and_on_a_fixed_one(
    tmp_path: Path, kind: str
) -> None:
    """The live trace equals the replayed one. Resume on the faulting build
    passes, and resume on a build without the fault keeps the rejection:
    the durable decision wins over a gate that would now apply it."""
    run_root = tmp_path / "run"
    adapter = FakeAdapter(default=None) if kind == "reconcile" else FakeAdapter()

    async def scenario() -> tuple[list[dict[str, Any]], Engine, Engine]:
        engine = _journaled(run_root, adapter=adapter)
        with _faulted(kind, True):
            await _play_kind(kind, engine)
            live = _dump(engine)
            await _close(engine)
            same = await _resume(run_root)
            await _close(same)
        fixed = await _resume(run_root)
        await _close(fixed)
        return live, same, fixed

    live, same, fixed = asyncio.run(scenario())
    index = _the_input(kind, _records(run_root))["seq"]
    assert _dump(same) == live and _dump(fixed) == live
    for resumed in (same, fixed):
        result = resumed.decisions.for_index(index)
        assert result is not None and (result.decision, result.code) == ("rejected", CODE)
        assert _held(kind, resumed) == _HELD[kind]


def test_the_dry_apply_is_what_quarantines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard, removed: an engine that does not dry-apply an engine-made
    input logs it, raises on the real apply and leaves it undecided -- the
    crash loop this change ends."""
    run_root = tmp_path / "run"
    monkeypatch.setattr(Engine, "_dry_apply_engine_made", lambda _self, _attempt: None)
    _on_status(monkeypatch, "SUCCESS")

    async def scenario() -> None:
        engine = _journaled(run_root)
        engine.inject(_start(), source="control")
        try:
            with pytest.raises(RuntimeError, match="an injected fault on j"):
                await engine.run_until_quiescent(LATER)
        finally:
            await _close(engine)

    asyncio.run(scenario())
    completion = _the_input("completion", _records(run_root))
    assert _decision(_records(run_root), completion["seq"]) is None


# ------------------------------------------------------- replay and rollback


def _poisoned_root(run_root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """A root an older build left: the completion is in the WAL with no
    decision, because that build applied it after the append and died.
    Returns its index."""

    async def scenario() -> None:
        engine = _journaled(run_root)
        engine.inject(_start(), source="control")
        try:
            with pytest.raises(RuntimeError):
                await engine.run_until_quiescent(LATER)
        finally:
            await _close(engine)

    with monkeypatch.context() as older:
        older.setattr(Engine, "_dry_apply_engine_made", lambda _self, _attempt: None)
        _on_status(older, "SUCCESS")
        asyncio.run(scenario())
    index = int(_the_input("completion", _records(run_root))["seq"])
    assert _decision(_records(run_root), index) is None
    return index


@pytest.mark.parametrize("fixed", [False, True])
def test_a_root_poisoned_by_an_undecided_faulting_attempt_resumes_through_6a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixed: bool,
) -> None:
    """Replay applies the undecided attempt on a fork first. On the build
    that still faults, its time half applies alone, so the verdict is the
    quarantine, which resume writes (step 6a) with the stderr line. On a
    fixed build the attempt applies, as before."""
    run_root = tmp_path / "run"
    index = _poisoned_root(run_root, monkeypatch)
    capsys.readouterr()
    if not fixed:
        _on_status(monkeypatch, "SUCCESS")

    async def scenario() -> Engine:
        resumed = await _resume(run_root)
        await _close(resumed)
        return resumed

    resumed = asyncio.run(scenario())
    written = _decision(_records(run_root), index)
    assert written is not None
    err = capsys.readouterr().err
    if fixed:
        assert (written["decision"], written["code"]) == ("applied", None)
        assert resumed.oracle.store.job["j"].status == "SUCCESS"
        assert QUARANTINE_LOG_PREFIX not in err
        return
    assert (written["decision"], written["code"]) == ("rejected", CODE)
    assert "STATUS for job j run 1 from adapter" in written["reason"]
    assert resumed.oracle.store.job["j"].status == "RUNNING"
    assert err.count(f"{QUARANTINE_LOG_PREFIX}: input {index}:") == 1


def test_an_undecided_attempt_whose_time_half_raises_still_stops_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay quarantines only when the time half applies alone. Here
    the time half of every batch after `j` started raises, so the
    undecided completion's does too, and replay stops naming it."""
    run_root = tmp_path / "run"
    index = _poisoned_root(run_root, monkeypatch)
    real = Oracle._fire_timers_due

    def fire(self: Oracle, at: datetime) -> None:
        if self.store.job["j"].status == "RUNNING":
            raise RuntimeError("an injected timer fault")
        real(self, at)

    records = _records(run_root)
    replay_inputs(Oracle(lower_source(_JIL)), records)  # unarmed, it replays
    monkeypatch.setattr(Oracle, "_fire_timers_due", fire)
    with pytest.raises(EngineError, match=f"replay stopped at input {index} .*timer fault"):
        replay_inputs(Oracle(lower_source(_JIL)), records)


def _quarantined_completion(run_root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    async def scenario() -> None:
        engine = _journaled(run_root)
        engine.inject(_start(), source="control")
        await engine.run_until_quiescent(LATER)
        await _close(engine)

    with monkeypatch.context() as faulting:
        _on_status(faulting, "SUCCESS")
        asyncio.run(scenario())
    return int(_the_input("completion", _records(run_root))["seq"])


@pytest.mark.parametrize(
    ("change", "diverges"),
    [("code", False), ("revisions", True)],
)
def test_an_unregistered_stored_code_replays_past_the_revisions_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, diverges: bool
) -> None:
    """The rollback claim. No reader checks a stored code against the
    registry, so a build that never heard of a code replays a rejection
    carrying it through the time half alone, and the revisions the live
    engine recorded match. The contrast case changes the revisions, and
    the same replay stops as diverged: the check does run."""
    run_root = tmp_path / "run"
    index = _quarantined_completion(run_root, monkeypatch)
    records = []
    for record in _records(run_root):
        if record.get("rec") == "decision" and record["index"] == index:
            assert record["code"] == CODE
            if change == "code":
                record = {**record, "code": "a_code_no_build_has_heard_of"}
            else:
                record = {**record, "revisions": {"job:j": 99}}
        records.append(record)
    oracle = Oracle(lower_source(_JIL))
    if diverges:
        with pytest.raises(EngineError, match=f"replay diverged at index {index}"):
            replay_inputs(oracle, records)
        return
    replay = replay_inputs(oracle, records)
    result = replay.decisions.for_index(index)
    assert result is not None and result.code == "a_code_no_build_has_heard_of"
    assert replay.recovered == []
    assert oracle.store.job["j"].status == "RUNNING"


# ------------------------------------------------------- the time half


def _armed_time_half(mp: pytest.MonkeyPatch, how: str) -> None:
    if how == "commit":
        _commit_fault(mp, "t", {"TERMINATED"})
    elif how == "commit-attempt":
        _commit_fault(mp, "j", {"STARTING", "RUNNING"})
    else:
        _on_status(mp, "TERMINATED")


@pytest.mark.parametrize(
    ("how", "stops"),
    [
        ("timer", True),  # the verbless time observation: all time half
        ("tick", True),  # a tick whose batch fires the kill first
        ("commit", True),  # the kill trips a commit-time check in `__exit__`
        ("commit-attempt", False),  # the attempt trips it: a quarantine
    ],
)
def test_a_time_half_fault_stops_before_the_append_and_leaves_a_clean_log(
    tmp_path: Path, how: str, stops: bool
) -> None:
    """`t` is killed by its timer at LATER. A fault in that kill stops the
    engine before the input that carries it is logged: no index, no
    record, no moved state. The second fork is what tells a commit-time
    fault of the time half from one of the attempt."""
    run_root = tmp_path / "run"

    async def scenario() -> tuple[BaseException | None, Engine]:
        engine = _journaled(run_root, _TERM_JIL, adapter=FakeAdapter(default=None))
        engine.inject(_start(job="t"), source="control")
        await engine.run_until_quiescent(T0)
        before = (engine.frontiers, state_bytes(engine.oracle), len(_records(run_root)))
        raised: BaseException | None = None
        with pytest.MonkeyPatch.context() as mp:
            _armed_time_half(mp, how)
            try:
                if how == "timer":
                    await engine.run_until_quiescent(LATER)
                else:
                    tick = _start(LATER)
                    tick.source = "scheduler"
                    await engine._admit_and_apply(_Pending(at=LATER, request_id=None, ev=tick))
            except TimeHalfStop as exc:
                raised = exc
                after = (engine.frontiers, state_bytes(engine.oracle), len(_records(run_root)))
                assert after == before
        await _close(engine)
        return raised, engine

    raised, engine = asyncio.run(scenario())
    assert (raised is not None) is stops
    if not stops:
        assert [result.code for result, _ in engine.quarantined] == [CODE]
        assert engine.oracle.store.job["t"].status == "TERMINATED"
        assert engine.oracle.store.job["j"].status == "INACTIVE"
        return
    assert str(raised).startswith("engine stopped before admitting input")
    assert "its time half raised RuntimeError: an injected" in str(raised)
    assert isinstance(raised.__cause__, RuntimeError)
    assert engine.oracle.store.job["t"].status == "RUNNING"
    assert engine.quarantined == []


def _fire_fault_from(mp: pytest.MonkeyPatch, when: datetime) -> None:
    """Every batch stamped at or after `when` raises in its time half."""
    real = Oracle._fire_timers_due

    def fire(self: Oracle, at: datetime) -> None:
        if at >= when:
            raise RuntimeError("an injected timer fault")
        real(self, at)

    mp.setattr(Oracle, "_fire_timers_due", fire)


@pytest.mark.parametrize("fixed", [False, True])
def test_after_a_time_half_stop_resume_replays_the_clean_log(tmp_path: Path, fixed: bool) -> None:
    """The log holds nothing of the stopped tick, so resume replays it on
    any build. The faulting build stops again on its next engine-made
    input; a fixed build applies it."""
    run_root = tmp_path / "run"

    def tick() -> Event:
        ev = _start(LATER)
        ev.source = "scheduler"
        return ev

    async def scenario() -> tuple[bool, Engine, int]:
        engine = _journaled(run_root, adapter=FakeAdapter(default=None))
        logged = len(_records(run_root))
        with pytest.MonkeyPatch.context() as mp:
            _fire_fault_from(mp, LATER)
            engine.inject(tick(), source="scheduler")
            with pytest.raises(TimeHalfStop):
                await engine.run_until_quiescent(LATER)
            await _close(engine)
            assert len(_records(run_root)) == logged
            if fixed:
                mp.undo()
            resumed = await _resume(run_root)
            resumed.inject(tick(), source="scheduler")
            stopped = False
            try:
                await resumed.run_until_quiescent(LATER)
            except TimeHalfStop:
                stopped = True
            await _close(resumed)
        return stopped, resumed, logged

    stopped, resumed, logged = asyncio.run(scenario())
    assert stopped is not fixed
    assert resumed.oracle.store.job["j"].status == ("RUNNING" if fixed else "INACTIVE")
    inputs = [r for r in _records(run_root) if r.get("rec") == "input"]
    assert len(inputs) == (1 if fixed else 0)


@pytest.mark.parametrize("armed", [True, False])
def test_a_control_input_under_a_time_half_fault_stays_refused_apply_faulted(
    tmp_path: Path, armed: bool
) -> None:
    """DL-292's refusal is unchanged: an operator's command whose batch
    fires the faulting kill is refused before the log, in either half."""
    run_root = tmp_path / "run"

    async def scenario() -> ApplyResult | AdmissionRefused:
        engine = _journaled(run_root, _TERM_JIL, adapter=FakeAdapter(default=None))
        engine.inject(_start(job="t"), source="control")
        await engine.run_until_quiescent(T0)
        expect = {"job:j": engine.oracle.store.revision("job:j")}
        with pytest.MonkeyPatch.context() as mp:
            if armed:
                _on_status(mp, "TERMINATED")
            future = engine.submit(
                _start(LATER), Envelope(request_id="op-1", expect=expect, epoch=engine.epoch)
            )
            stopped = False
            try:
                await engine.run_until_quiescent(LATER)
            except TimeHalfStop:
                stopped = True  # the engine's own time observation, after the refusal
            assert stopped is armed
            try:
                answer: ApplyResult | AdmissionRefused = await future
            except AdmissionRefused as exc:
                answer = exc
        await _close(engine)
        return answer

    answer = asyncio.run(scenario())
    ids = [r.get("request_id") for r in _records(run_root) if r.get("rec") == "input"]
    if armed:
        assert isinstance(answer, AdmissionRefused) and answer.code == "apply_faulted"
        assert "op-1" not in ids
    else:
        assert isinstance(answer, ApplyResult) and answer.decision == "applied"
        assert "op-1" in ids


def test_a_verbless_input_is_never_quarantined() -> None:
    """A time observation has no attempt half to give up."""
    from dsl41.runner_admission import Attempt

    oracle = Oracle(lower_source(_JIL))
    advance = Attempt(index=1, at=T0, request_id="engine:1", fingerprint="fp")
    verbed = advance.model_copy(update={"kind": "STARTJOB", "payload": {"job": "j"}})
    fault = RuntimeError("x")
    assert quarantine_verdict(oracle, advance, fault) is fault
    assert not isinstance(quarantine_verdict(oracle, verbed, fault), Exception)


# ------------------------------------------------------- the CLI


_TICKED_JIL = 'insert_job: d\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\nstart_times: "09:00"\n'


def _rehearse(tmp_path: Path, *extra: str) -> Any:
    from typer.testing import CliRunner

    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_TICKED_JIL)
    args = ["rehearse", str(jil), "--start", "2026-07-01T08:00:00", "--hours", "2", *extra]
    return CliRunner().invoke(app, args)


@pytest.mark.parametrize("armed", [True, False])
def test_rehearse_exits_non_zero_on_a_time_half_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, armed: bool
) -> None:
    real = Oracle._fire_timers_due

    def fire(self: Oracle, at: datetime) -> None:
        if at >= datetime(2026, 7, 1, 8, 30):
            raise RuntimeError("an injected timer fault")
        real(self, at)

    if armed:
        monkeypatch.setattr(Oracle, "_fire_timers_due", fire)
    result = _rehearse(tmp_path)
    if not armed:
        assert result.exit_code == 0, result.output
        return
    assert result.exit_code == 1, result.output
    assert "rehearse failed: engine stopped before admitting input" in result.output
    assert "STARTJOB for job d from scheduler" in result.output
    assert "its time half raised RuntimeError: an injected timer fault" in result.output


@pytest.mark.parametrize("armed", [True, False])
def test_rehearse_names_a_quarantined_input_and_the_journal_narrates_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, armed: bool
) -> None:
    """Rehearse fails on a quarantine, as on a refusal. `dsl41 journal`
    prints the rejected engine-made decision beside the trace, from the
    decision records: nothing replayed reads the line."""
    from typer.testing import CliRunner

    from dsl41.cli import app

    root = tmp_path / "run"
    with monkeypatch.context() as mp:
        if armed:
            _on_status(mp, "SUCCESS")
        played = _rehearse(tmp_path, "--run-root", str(root))
    journal = CliRunner().invoke(app, ["journal", str(root)])
    assert journal.exit_code == 0, journal.output
    if not armed:
        assert played.exit_code == 0, played.output
        assert "rejected input" not in journal.output
        assert "d RUNNING->SUCCESS" in journal.output
        return
    assert played.exit_code == 1, played.output
    assert "quarantined input 2: quarantined: applying STATUS for job d run 1 from adapter" in (
        played.output
    )
    assert f"{QUARANTINE_LOG_PREFIX}: input 2: " in played.output
    assert (
        "rejected input 2 (STATUS for job d run 1 from adapter): engine_apply_faulted:"
        " quarantined: applying STATUS for job d run 1 from adapter at"
    ) in journal.output
    assert "d RUNNING->SUCCESS" not in journal.output


@pytest.mark.parametrize("broken", [True, False])
def test_the_quarantine_line_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], broken: bool
) -> None:
    """A stderr that cannot be written drops the line and never raises into
    the engine between a decision and its answer."""
    import sys

    from dsl41.runner_admission import report_quarantine

    class Broken:
        def write(self, text: str) -> int:
            raise OSError("stderr is gone")

    if broken:
        monkeypatch.setattr(sys, "stderr", Broken())
    result = ApplyResult(
        index=4, request_id="engine:4", decision="rejected", reason="why", code=CODE
    )
    report_quarantine(result)
    if not broken:
        assert capsys.readouterr().err == f"{QUARANTINE_LOG_PREFIX}: input 4: why\n"


def _fault_on_the_engines_oracle(mp: pytest.MonkeyPatch) -> None:
    """Every batch of the engine's oracle and its forks raises in its time
    half. The boundary's classification evaluates seeded oracles of its
    own, which hold no host row; a fault there would refuse the seal
    before anything is drained."""
    real = Oracle._fire_timers_due

    def fire(self: Oracle, at: datetime) -> None:
        if "local" in self.store.hosts:
            raise RuntimeError("an injected timer fault")
        real(self, at)

    mp.setattr(Oracle, "_fire_timers_due", fire)


def _fork_only_commit_fault(mp: pytest.MonkeyPatch) -> None:
    """The completion's attempt raises KeyError after `j` moves to
    SUCCESS. A batch that moved nothing -- the time half alone, on a fork,
    whose trace starts empty -- trips a commit-time RuntimeError. So the
    whole apply and the time half alone raise different faults."""
    from dsl41.oracle import InputBatch

    real_transition = RuntimeState.transition

    def transition(self: RuntimeState, job: str, new: Any, *args: Any, **kw: Any) -> None:
        real_transition(self, job, new, *args, **kw)
        if new == "SUCCESS":
            raise KeyError("an attempt-half fault")

    real_commit = InputBatch._commit

    def commit(self: InputBatch) -> None:
        if not self._oracle._trace and self._oracle.store.job["j"].status == "RUNNING":
            raise RuntimeError("a time-half commit fault")
        real_commit(self)

    mp.setattr(RuntimeState, "transition", transition)
    mp.setattr(InputBatch, "_commit", commit)


def test_the_stop_names_what_the_time_half_raised_alone(tmp_path: Path) -> None:
    """Live: the stop names the second fork's fault, with the whole apply's
    as context. The contrast is the timer stop above, whose two faults read
    the same and are named once."""
    run_root = tmp_path / "run"

    async def scenario() -> TimeHalfStop:
        engine = _journaled(run_root)
        with pytest.MonkeyPatch.context() as mp:
            _fork_only_commit_fault(mp)
            engine.inject(_start(), source="control")
            with pytest.raises(TimeHalfStop) as stopped:
                await engine.run_until_quiescent(LATER)
        await _close(engine)
        return stopped.value

    stop = asyncio.run(scenario())
    assert "its time half raised RuntimeError: a time-half commit fault" in str(stop)
    assert "(applying the whole input raised KeyError: 'an attempt-half fault')" in str(stop)


@pytest.mark.parametrize("differ", [True, False])
def test_the_replay_stop_names_what_the_time_half_raised_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, differ: bool
) -> None:
    """Replay names the time half's fault too, and adds the whole apply's
    as a note only when the two differ."""
    run_root = tmp_path / "run"
    index = _poisoned_root(run_root, monkeypatch)
    records = _records(run_root)
    with monkeypatch.context() as mp:
        if differ:
            _fork_only_commit_fault(mp)
        else:
            real = Oracle._fire_timers_due
            mp.setattr(
                Oracle,
                "_fire_timers_due",
                lambda self, at: (
                    real(self, at)
                    if self.store.job["j"].status != "RUNNING"
                    else (_ for _ in ()).throw(RuntimeError("an injected timer fault"))
                ),
            )
        with pytest.raises(EngineError, match=f"replay stopped at input {index} ") as stopped:
            replay_inputs(Oracle(lower_source(_JIL)), records)
    notes = getattr(stopped.value, "__notes__", [])
    if differ:
        assert "RuntimeError: a time-half commit fault" in str(stopped.value)
        assert notes == ["applying the whole input raised KeyError: 'an attempt-half fault'"]
    else:
        assert "RuntimeError: an injected timer fault" in str(stopped.value)
        assert notes == []


_GATED_JIL = "insert_job: gate\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"


@pytest.mark.parametrize("armed", [True, False])
def test_a_quarantine_met_only_in_a_sweep_fails_rehearse_naming_its_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, armed: bool
) -> None:
    """The main play never runs `gate`; the flag sweep's case sets FLAG and
    runs it, and its completion is quarantined. The case's counts would
    report a run that only lost its completion, so rehearse fails naming
    the case, as it does for a refusal."""
    from typer.testing import CliRunner

    from dsl41.cli import app

    jil = tmp_path / "estate.jil"
    jil.write_text(_GATED_JIL)
    real = RuntimeState.transition

    def transition(self: RuntimeState, job: str, new: Any, *args: Any, **kw: Any) -> None:
        real(self, job, new, *args, **kw)
        if job == "gate" and new == "SUCCESS":
            raise RuntimeError("an injected fault on gate")

    if armed:
        monkeypatch.setattr(RuntimeState, "transition", transition)
    args = ["rehearse", str(jil), "--start", "2026-07-01T08:00:00", "--hours", "2"]
    result = CliRunner().invoke(app, [*args, "--check-cadence", "--sweep", "flags"])
    if not armed:
        assert result.exit_code in (0, 3), result.output
        assert "quarantined" not in result.output
        return
    assert result.exit_code == 1, result.output
    assert "rehearse failed: quarantined input" in result.stderr
    assert "in sweep flags:FLAG='go'" in result.stderr
    assert "applying STATUS for job gate run 1 from adapter" in result.stderr


@pytest.mark.parametrize("armed", [True, False])
def test_the_journal_narrates_a_control_input_that_replay_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, armed: bool
) -> None:
    """A command whose real apply raised after a passing dry apply is in
    the WAL with no decision. Replay quarantines it on a build that still
    faults, and the narration names it though its source is `control`.
    On a fixed build it applies, and nothing is narrated."""
    import dsl41.runner as runner_mod
    from dsl41.cli_run import _rejection_lines

    run_root = tmp_path / "run"
    real_apply = runner_mod.apply_attempt

    def apply_on_the_engine(oracle: Oracle, attempt: Any, **kwargs: Any) -> Any:
        if attempt.request_id == "op-1":
            raise RuntimeError("only the real apply raised")
        return real_apply(oracle, attempt, **kwargs)

    async def scenario() -> None:
        engine = _journaled(run_root)
        expect = {"job:j": engine.oracle.store.revision("job:j")}
        engine.submit(_start(), Envelope(request_id="op-1", expect=expect, epoch=engine.epoch))
        try:
            with pytest.raises(RuntimeError, match="only the real apply raised"):
                await engine.run_until_quiescent(T0)
        finally:
            await _close(engine)

    with monkeypatch.context() as mp:
        mp.setattr(runner_mod, "apply_attempt", apply_on_the_engine)
        asyncio.run(scenario())
    records = _records(run_root)
    if armed:
        _on_status(monkeypatch, "STARTING")
    replay = replay_inputs(Oracle(lower_source(_JIL)), records)
    lines = _rejection_lines(records, replay.decisions)
    if not armed:
        assert lines == []
        return
    assert len(lines) == 1
    assert lines[0].startswith(
        f"{T0.isoformat()} rejected input 1 (STARTJOB for job j from control):"
        " engine_apply_faulted: quarantined: applying STARTJOB"
    )


def _raised(exc: Exception) -> Exception:
    try:
        raise exc
    except Exception as caught:  # noqa: BLE001 -- the test keeps what was raised
        return caught


@pytest.mark.parametrize("armed", [True, False])
def test_a_kept_fault_drops_the_tracebacks_of_its_cause_and_its_context(armed: bool) -> None:
    from dsl41.runner import _without_frames

    cause, context = _raised(ValueError("cause")), _raised(KeyError("context"))
    fault = _raised(RuntimeError("fault"))
    fault.__cause__, fault.__context__ = cause, context
    kept = _without_frames(fault) if armed else fault
    chain = (kept, cause, context)
    if armed:
        assert all(link.__traceback__ is None for link in chain)
    else:
        assert all(link.__traceback__ is not None for link in chain)


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (TimeHalfStop, 6),
        (EngineError("test: a crash"), 1),
    ],
)
def test_a_time_half_stop_exits_6_and_a_crash_exits_1(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, raised: Any, code: int
) -> None:
    """Exit 6 is the time-half stop's own, so the engine units'
    `RestartPreventExitStatus=2 3 6 7` keeps it down: every restart would
    meet the same timers."""
    from dsl41 import cli_run
    from dsl41.run_root import runtime_profile_from_cli
    from dsl41.runner_admission import Attempt

    assert cli_run.EXIT_TIME_HALF_STOP == 6
    if raised is TimeHalfStop:
        attempt = Attempt(index=3, at=T0, request_id="engine:3", fingerprint="fp")
        raised = TimeHalfStop(attempt, RuntimeError("test: a timer fault"))

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
        )
    )
    assert exit_code == code
