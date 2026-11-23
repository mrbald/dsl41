"""The engine's door for an event from outside: one payload schema per kind (DL-554).

An event that comes from outside conforms to its kind's schema, or it is refused at the door
with `invalid_argument`, naming the field, what was expected and what came. Nothing is queued,
journalled or indexed for it. The door is every path a fresh event takes: `Engine.inject`,
`Engine.submit`, the model harness's `deliver`, a rehearsal's scenario file and an adapter's
result. A replay is not one: a stored record keeps what an earlier build admitted.
"""

from __future__ import annotations

import asyncio
import enum
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import JobIR, lower_source
from dsl41.oracle_state import Event, EventKind
from dsl41.runner import Engine
from dsl41.runner_adapters import AdapterContext, FakeAdapter, Failed, outcome_from_status
from dsl41.runner_admission import AdmissionRefused, Envelope, fresh_event
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_history import read_run_root
from dsl41.runner_journal import read_records
from dsl41.runner_startup import resume_run, start_run

from model_harness import ModelRun

T0 = datetime(2026, 7, 1, 8, 0)
_LEAF = "insert_job: j\njob_type: c\ncommand: x\n"


class _Run(enum.IntEnum):
    ONE = 1


class _Signal(enum.IntEnum):
    KILL = 9


def _ev(kind: EventKind, **payload: object) -> Event:
    return Event(at=T0, kind=kind, payload=payload)


def _engine(script: dict[Any, Any] | None = None) -> Engine:
    """A rehearsal-shaped engine: its records go to a memory store."""
    return start_run(
        lower_source(_LEAF + "\ninsert_global: g\nvalue: a\n"),
        None,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(script or {}, default=None)},
    )


def _inputs(engine: Engine) -> list[dict[str, Any]]:
    assert engine.journal is not None
    return [dict(r.payload) for _, r in engine.journal.store.read_from(0) if r.kind == "input"]


# One case per schema line: the event, and the field its refusal names.
_REFUSED: list[tuple[str, Event, str]] = [
    ("a job event with no job", _ev("STARTJOB"), "job"),
    ("a job that is not a string", _ev("ON_ICE", job=1), "job"),
    ("a timer with no job", _ev("TIMER", check="must_start", run=0), "job"),
    ("a STATUS with neither status nor exit code", _ev("STATUS", job="j"), "status"),
    ("a status nobody may inject", _ev("STATUS", job="j", status="QUE_WAIT"), "status"),
    ("an unknown status", _ev("STATUS", job="j", status="DONE"), "status"),
    ("a status that is no string", _ev("STATUS", job="j", status=None, exit_code=0), "status"),
    ("an exit code as a string", _ev("STATUS", job="j", exit_code="0"), "exit_code"),
    ("an exit code true", _ev("STATUS", job="j", exit_code=True), "exit_code"),
    (
        "an exit code beside a status",
        _ev("STATUS", job="j", status="FAILURE", exit_code=1.0),
        "exit_code",
    ),
    ("run_number true", _ev("STATUS", job="j", status="SUCCESS", run_number=True), "run_number"),
    ("run_number as text", _ev("STATUS", job="j", status="SUCCESS", run_number="1"), "run_number"),
    (
        "run_number past the store's integers",
        _ev("STATUS", job="j", status="SUCCESS", run_number=2**63),
        "run_number",
    ),
    (
        "run_number below the store's integers",
        _ev("STATUS", job="j", status="SUCCESS", run_number=-(2**63) - 1),
        "run_number",
    ),
    ("run_number null", _ev("STATUS", job="j", status="SUCCESS", run_number=None), "run_number"),
    ("hold that is no boolean", _ev("STATUS", job="j", status="FAILURE", hold=1), "hold"),
    (
        "ended_at that is no timestamp",
        _ev("STATUS", job="j", exit_code=0, ended_at="yesterday"),
        "ended_at",
    ),
    ("ended_at that is no string", _ev("STATUS", job="j", exit_code=0, ended_at=5), "ended_at"),
    ("cause that is no string", _ev("STATUS", job="j", status="FAILURE", cause=3), "cause"),
    ("SET_GLOBAL with an empty name", _ev("SET_GLOBAL", name="", value="x"), "name"),
    ("SET_GLOBAL with no name", _ev("SET_GLOBAL", value="x"), "name"),
    ("SET_GLOBAL with no value", _ev("SET_GLOBAL", name="g"), "value"),
    ("SET_GLOBAL with a number", _ev("SET_GLOBAL", name="g", value=3), "value"),
    (
        "a start's deferred cause as a number",
        _ev("STARTJOB", job="j", deferred_cause=7),
        "deferred_cause",
    ),
    ("a timer's deferred cause null", _ev("TIMER", job="j", deferred_cause=None), "deferred_cause"),
    ("a value with a lone surrogate", _ev("SET_GLOBAL", name="g", value="\ud800"), "value"),
    (
        "a lone surrogate in a field the schema does not name",
        _ev("TIMER", job="j", box=["\udc00"]),
        "box",
    ),
    ("a float in a field the schema does not name", _ev("STARTJOB", job="j", x=1.5), "x"),
    ("a nested float", _ev("TIMER", job="j", box={"run": [0.5]}), "box"),
    ("kill signals as text", _ev("KILLJOB", job="j", kill_signals="2,9"), "kill_signals"),
    ("kill signals not ending in KILL", _ev("KILLJOB", job="j", kill_signals=[2]), "kill_signals"),
    ("a kill signal true", _ev("KILLJOB", job="j", kill_signals=[True, 9]), "kill_signals"),
]


@pytest.mark.parametrize(
    ("ev", "field"), [(ev, field) for _, ev, field in _REFUSED], ids=[c for c, _, _ in _REFUSED]
)
def test_inject_refuses_a_payload_off_its_schema_naming_the_field(ev: Event, field: str) -> None:
    """`inject` raises at once: nothing is queued, no index is taken, nothing is journalled."""
    engine = _engine()
    before = engine.frontiers.committed_index
    with pytest.raises(AdmissionRefused) as refused:
        engine.inject(ev)
    assert refused.value.code == "invalid_argument"
    assert f"{ev.kind} payload.{field}" in str(refused.value)
    assert engine._queue == []
    asyncio.run(engine.run_until_quiescent(T0 + timedelta(minutes=1)))
    assert engine.frontiers.committed_index == before
    assert _inputs(engine) == []


@pytest.mark.parametrize(
    ("ev", "field"), [(ev, field) for _, ev, field in _REFUSED], ids=[c for c, _, _ in _REFUSED]
)
def test_submit_fails_the_same_way(ev: Event, field: str) -> None:
    """`submit` returns an awaitable that fails with the refusal: an `AdmissionRefused`, the
    exception the control socket answers with `refused: true`."""
    engine = _engine()
    before = engine.frontiers.committed_index

    async def scenario() -> None:
        submitted = engine.submit(ev, Envelope(request_id="r1", expect={"job:j": 0}))
        assert engine._queue == []
        with pytest.raises(AdmissionRefused) as refused:
            await submitted
        assert refused.value.code == "invalid_argument"
        assert f"{ev.kind} payload.{field}" in str(refused.value)
        await engine.run_until_quiescent(T0 + timedelta(minutes=1))

    asyncio.run(scenario())
    assert engine.frontiers.committed_index == before
    assert _inputs(engine) == []


@pytest.mark.parametrize(
    ("payload", "stored"),
    [
        ({"job": "j", "status": "SUCCESS"}, {"job": "j", "status": "SUCCESS"}),
        (
            {"job": "j", "status": "SUCCESS", "run_number": 2**63 - 1},
            {"job": "j", "status": "SUCCESS", "run_number": 2**63 - 1},
        ),
        (
            {"job": "j", "status": "SUCCESS", "run_number": -(2**63)},
            {"job": "j", "status": "SUCCESS", "run_number": -(2**63)},
        ),
        (
            {"job": "j", "status": "FAILURE", "run_number": _Run.ONE, "exit_code": _Run.ONE},
            {"job": "j", "status": "FAILURE", "run_number": 1, "exit_code": 1},
        ),
        (
            {"job": "j", "exit_code": 0, "ended_at": "2026-07-01T07:59:00+00:00", "cause": "x"},
            {"job": "j", "exit_code": 0, "ended_at": "2026-07-01T07:59:00+00:00", "cause": "x"},
        ),
        (
            {"job": "j", "status": "FAILURE", "hold": False},
            {"job": "j", "status": "FAILURE", "hold": False},
        ),
    ],
)
def test_a_payload_on_its_schema_is_queued_with_plain_integers(
    payload: dict[str, object], stored: dict[str, object]
) -> None:
    """Absent and both ends of the signed 64-bit range are run numbers; an `IntEnum` is taken
    as its plain integer."""
    engine = _engine()
    engine.inject(Event(at=T0, kind="STATUS", payload=payload))
    [(_, _, pending)] = engine._queue
    assert pending.ev is not None
    assert pending.ev.payload == stored
    numbers = [v for k, v in pending.ev.payload.items() if k in ("run_number", "exit_code")]
    assert all(type(v) is int for v in numbers)
    asyncio.run(engine.run_until_quiescent(T0 + timedelta(minutes=1)))
    [record] = _inputs(engine)
    assert record["payload"] == stored


def test_kill_signals_are_taken_as_a_plain_list() -> None:
    checked = fresh_event(_ev("KILLJOB", job="j", kill_signals=(2, _Signal.KILL)))
    assert checked.payload == {"job": "j", "kill_signals": [2, 9]}
    assert all(type(n) is int for n in checked.payload["kill_signals"])  # type: ignore[attr-defined]
    assert fresh_event(_ev("KILLJOB", job="j")).payload == {"job": "j"}


def test_an_empty_global_value_is_a_value() -> None:
    """`v(G) = ""` is a legal condition, so an empty value must be settable."""
    ev = _ev("SET_GLOBAL", name="g", value="")
    assert fresh_event(ev).payload == {"name": "g", "value": ""}


def test_a_field_name_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(AdmissionRefused, match="not a Unicode scalar string") as refused:
        fresh_event(_ev("STARTJOB", job="j", **{"\ud800": 1}))
    assert refused.value.code == "invalid_argument"


def test_fields_the_schema_does_not_name_are_left_as_they_are() -> None:
    """A scripted TIMER keeps its own fields; a string `deferred_cause` is kept as it is."""
    timer = _ev("TIMER", job="j", check="bogus", run=True, box=["x"])
    assert fresh_event(timer).payload == timer.payload
    start = _ev("FORCE_STARTJOB", job="j", deferred_cause="tick")
    assert fresh_event(start).payload == start.payload
    alarm = _ev("MUST_START_ALARM")
    assert fresh_event(alarm).payload == {}


def test_the_engine_queues_its_own_copy() -> None:
    """The caller's event changed after `inject` does not change what is journalled."""
    engine = _engine()
    ev = _ev("STATUS", job="j", status="SUCCESS", kill=[1])
    engine.inject(ev)
    ev.payload["status"] = "FAILURE"
    ev.payload["kill"].append(2)  # type: ignore[attr-defined]
    ev.at = T0 + timedelta(minutes=5)
    asyncio.run(engine.run_until_quiescent(T0 + timedelta(minutes=10)))
    [record] = _inputs(engine)
    assert record["payload"] == {"job": "j", "status": "SUCCESS", "kill": [1]}
    assert record["at"] == T0.isoformat()
    assert ev.source is None


def test_the_model_harness_delivers_through_the_door(tmp_path: Path) -> None:
    run = ModelRun(_LEAF, tmp_path / "run")
    engine = run.start()

    async def scenario() -> None:
        try:
            with pytest.raises(AdmissionRefused, match="payload.run_number"):
                run.deliver(_ev("STATUS", job="j", exit_code=0, run_number=True))
            assert engine._queue == []
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ a rehearsal's scenario file


def test_a_scenario_file_is_refused_before_its_adapter_entries_and_the_run_root(
    tmp_path: Path, cli_env: None
) -> None:
    """The second event names run `true`: the file and the entry are named, the adapter entry
    that would not coerce is never read, and the run root is not created."""
    jil = tmp_path / "jobs.jil"
    jil.write_text(_LEAF)
    scenario = tmp_path / "scenario.json"
    at = "2026-07-01T08:00:00"
    scenario.write_text(
        json.dumps(
            {
                "adapter": {"runs": [{"job": "j", "run_number": "not a number"}]},
                "events": [
                    {"at": at, "kind": "STARTJOB", "payload": {"job": "j"}},
                    {
                        "at": at,
                        "kind": "STATUS",
                        "payload": {"job": "j", "status": "SUCCESS", "run_number": True},
                    },
                ],
            }
        )
    )
    run_root = tmp_path / "run"
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--start",
            at,
            "--scenario",
            str(scenario),
            "--run-root",
            str(run_root),
        ],
    )
    assert result.exit_code == 2, result.output
    assert f"scenario {scenario}: events[1]: STATUS payload.run_number" in result.output
    assert not run_root.exists()


def test_a_scenario_event_names_its_wrong_field(tmp_path: Path, cli_env: None) -> None:
    jil = tmp_path / "jobs.jil"
    jil.write_text(_LEAF)
    scenario = tmp_path / "scenario.json"
    at = "2026-07-01T08:00:00"
    event = {"at": at, "kind": "STATUS", "payload": {"job": "j", "status": "SUCCESS"}}
    event["payload"]["run_number"] = "1"  # type: ignore[index]
    scenario.write_text(json.dumps({"events": [event]}))
    result = CliRunner().invoke(
        app, ["rehearse", str(jil), "--start", at, "--scenario", str(scenario)]
    )
    assert result.exit_code == 2, result.output
    assert f"scenario {scenario}: events[0]: STATUS payload.run_number" in result.output


_AT = "2026-07-01T08:00:00"
_START = {"at": _AT, "kind": "STARTJOB", "payload": {"job": "j"}}


@pytest.mark.parametrize(
    ("scenario", "named"),
    [
        (
            {
                "events": [
                    {"at": _AT, "kind": "SET_GLOBAL", "payload": {"name": "g", "value": "\ud800"}}
                ]
            },
            "events[0]: SET_GLOBAL payload.value",
        ),
        ({"events": {}}, '"events": expected a list'),
        ({"events": [_START, {"at": _AT, "kind": "STARTJOB", "payload": []}]}, "events[1]: "),
        (
            {"adapter": {"runs": [{"job": "j", "run_number": True, "exit_code": 0}]}},
            "adapter.runs[0].run_number: expected an integer",
        ),
        (
            {"adapter": {"runs": [{"job": 1, "run_number": 1, "exit_code": 0}]}},
            "adapter.runs[0].job: expected a string",
        ),
        (
            {
                "adapter": {
                    "runs": [{"job": "j", "run_number": 1, "duration_s": 0, "exit_code": False}]
                }
            },
            "adapter.runs j#1: exit_code: expected an integer, got False",
        ),
        (
            {"adapter": {"default": [0, False]}},
            "adapter.default: exit_code: expected an integer, got False",
        ),
        (
            {"adapter": {"default": {"duration_s": 0, "exit_code": False}}},
            "adapter.default: exit_code: expected an integer, got False",
        ),
        (
            {"adapter": {"default": [True, 0]}},
            "adapter.default: duration_s: expected a number, got True",
        ),
    ],
)
def test_a_scenario_file_off_its_shape_is_refused_naming_the_entry(
    tmp_path: Path, cli_env: None, scenario: dict[str, Any], named: str
) -> None:
    """Each is refused with exit 2 before the run root exists: nothing is coerced into run 1
    or exit 0, and the entry is named."""
    jil = tmp_path / "jobs.jil"
    jil.write_text(_LEAF + "\ninsert_global: g\nvalue: a\n")
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(scenario))
    run_root = tmp_path / "run"
    args = ["rehearse", str(jil), "--start", _AT, "--scenario", str(path), "--run-root"]
    result = CliRunner().invoke(app, [*args, str(run_root)])
    assert result.exit_code == 2, result.output
    assert f"scenario {path}: {named}" in result.output
    assert not run_root.exists()


def test_a_scenario_with_numbers_as_written_still_plays(tmp_path: Path, cli_env: None) -> None:
    """The contrast: integer and float durations and integer exit codes are taken."""
    jil = tmp_path / "jobs.jil"
    jil.write_text(_LEAF)
    path = tmp_path / "scenario.json"
    runs = [{"job": "j", "run_number": 1, "duration_s": 1.5, "exit_code": 3}]
    path.write_text(json.dumps({"adapter": {"default": [0, 0], "runs": runs}, "events": [_START]}))
    result = CliRunner().invoke(
        app, ["rehearse", str(jil), "--start", _AT, "--hours", "1", "--scenario", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert "RUNNING->FAILURE" in result.output


# ------------------------------------------------------------------ an adapter's result


class _TrueAdapter:
    """A custom adapter that returns `True` for an exit code."""

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> bool:
        return True


def test_an_adapter_returning_true_is_refused_before_its_result_is_recorded(
    tmp_path: Path,
) -> None:
    """The engine stops with the refusal, as for any result it cannot read. The attempt stays
    open: its dispatch is stored and no outcome is, so the next start settles it."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            lower_source(_LEAF),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": _TrueAdapter()},  # type: ignore[dict-item]
        )
        try:
            engine.inject(_ev("STARTJOB", job="j"))
            with pytest.raises(EngineError, match=r"adapter for 'j' returned True: .*exit_code"):
                await engine.run_until_quiescent(T0 + timedelta(minutes=1))
        finally:
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
    kinds = [record.kind for _, record in read_records(run_root)]
    assert "dispatch" in kinds
    assert "outcome" not in kinds


def test_a_wrapper_record_whose_exit_code_is_true_is_malformed() -> None:
    """The wrapper's record maps to a FAILURE with its cause, live and at resume alike."""
    result = outcome_from_status({"outcome": "exited", "exit_code": True})
    assert result == Failed("malformed status record: outcome 'exited' with exit_code=True")


# ------------------------------------------------------------------ a replay is not the door


def test_a_stored_run_number_true_still_replays_and_folds(tmp_path: Path) -> None:
    """An earlier build admitted `run_number: true` unchecked. The restart replays the stored
    record as it is, and the fold reads it as run 1, as before."""
    run_root = tmp_path / "run"
    catalog = lower_source(_LEAF)

    async def scenario() -> tuple[str, str]:
        engine = start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j", 1): (60.0, 0)})},
        )
        engine.inject(_ev("STARTJOB", job="j"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        late = Event(
            at=T0 + timedelta(minutes=3),
            kind="STATUS",
            payload={"job": "j", "status": "FAILURE", "run_number": True},
        )
        engine._enqueue(late, source="control")  # below the door, as the earlier build took it
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        before = engine.oracle.store.job["j"].status
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()
        resumed = await resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=4)),
            adapters={"CMD": FakeAdapter()},
        )
        after = resumed.oracle.store.job["j"].status
        await resumed.shutdown()
        assert resumed.journal is not None
        resumed.journal.close()
        return before, after

    assert asyncio.run(scenario()) == ("FAILURE", "FAILURE")
    assert [(row.run_number, row.status) for row in read_run_root(run_root)] == [(1, "FAILURE")]
