"""CMD retries stay inside one live run (DL-485)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dispatch_fixtures import dispatch_record
from store_surgery import rows
from test_engine_stops import _finish, _jil, _resume, _spawned, _start, _start_jobs, _until
from test_restart import T0, _close, _open, _resume as resume_virtual, _write
from test_run_history import _applied, _dispatch, _header, _status_input
from test_status_hook import Capture

from dsl41 import runner_history
from dsl41.engine_charts import AttemptOutcome
from dsl41.ir import Semantics, lower_source
from dsl41.lint import rule_known_unsupported
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter, LocalCommandAdapter
from dsl41.runner_attempts import (
    DISPATCH,
    ORACLE_KILL,
    exit_record_from_status,
    follow_up_plan,
    oracle_input_for,
)
from dsl41.runner_clock import RealClock, VirtualClock
from dsl41.runner_config import EngineSettings
from dsl41.runner_control import ControlClient, ControlServer, attempt_note, status_label
from dsl41.runner_history import fold_run_rows, read_run_root
from dsl41.runner_journal import OpenTable, check_dispatch
from dsl41.runner_stops import AttemptFacts

RID = "5b0c3f0e-6a52-4d3e-9a0b-2f7d6c1e4a10"


def natural(code: Any = 1, **fields: Any) -> AttemptOutcome:
    return AttemptOutcome(
        "natural",
        record=exit_record_from_status(
            {"outcome": "exited", "exit_code": code, **fields}, stop_in_progress=False
        ),
    )


def plan(outcome: AttemptOutcome | None = None, **changes: Any):
    return follow_up_plan(
        outcome or natural(),
        **{
            "job": "j",
            "run_number": 1,
            "attempt": 1,
            "in_catalog": True,
            "live_run": (1, "RUNNING"),
            "marked": False,
            "cmd_semantics": Semantics(n_retrys=2),
            **changes,
        },
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "signaled", "signal": 9, "exit_code": 1},
        {"exit_code": "1"},
        {"exit_code": True},
        {"outcome": "terminated", "cause": "other"},
    ],
)
def test_retry_discriminator_uses_only_oracle_input(fields):
    outcome = natural(**fields)
    chosen = plan(outcome)
    assert chosen.kind == "complete"
    assert chosen.steps[0].detail == oracle_input_for(outcome)


@pytest.mark.parametrize(
    "changes",
    [
        {"in_catalog": False},
        {"live_run": None},
        {"live_run": (2, "RUNNING")},
        {"live_run": (1, "INACTIVE")},
        {"live_run": (1, "SUCCESS")},
        {"cmd_semantics": None},
        {"cmd_semantics": Semantics(n_retrys=0)},
        {"attempt": 3},
    ],
)
def test_retry_eligibility(changes):
    assert plan(**changes).kind == "complete"


@pytest.mark.parametrize(
    "sem,code,retries",
    [
        (Semantics(n_retrys=1), 0, False),
        (Semantics(n_retrys=1), 1, True),
        (Semantics(n_retrys=1, max_exit_success=4), 3, False),
        (Semantics(n_retrys=1, max_exit_success=4), 5, True),
        (Semantics(n_retrys=1, success_codes=[(3, 5)]), 3, False),
        (Semantics(n_retrys=1, success_codes=[(3, 5)]), 0, True),
        (Semantics(n_retrys=1, fail_codes=[(0, 0)]), 0, True),
        (Semantics(n_retrys=1, fail_codes=[(0, 0)]), 7, False),
    ],
)
def test_retry_verdict(sem, code, retries):
    assert (plan(natural(code), cmd_semantics=sem).kind == "rerun") == retries


def test_marked_natural_failure_retries_and_starting_is_live():
    chosen = plan(marked=True, live_run=(1, "STARTING"), mint=lambda: RID)
    assert chosen.kind == "rerun" and chosen.steps[0].effect == DISPATCH
    assert (chosen.steps[0].detail.run_id, chosen.steps[0].detail.ordinal) == (RID, 2)
    assert plan(AttemptOutcome("spawn_failed")).kind == "complete"
    assert plan(AttemptOutcome("stopped", cause=ORACLE_KILL)).kind == "none"


@pytest.mark.parametrize("count", range(21))
def test_retry_interruption_budget_and_limit_evidence(count):
    sem = Semantics(n_retrys=count)
    limit = 5 + count
    stopped = AttemptOutcome("unobservable")
    assert plan(stopped, attempt=limit - 1, cmd_semantics=sem).kind == "rerun"
    held = plan(stopped, attempt=limit, cmd_semantics=sem)
    assert held.kind == "hold" and held.steps[0].detail.limit == limit
    assert plan(stopped, attempt=limit + 1, cmd_semantics=sem).kind == "hold"
    assert plan(stopped, cmd_semantics=sem, marked=True).kind == "hold"
    assert plan(stopped, cmd_semantics=sem, quarantined="bad record").kind == "hold"
    assert plan(attempt=count + 1, cmd_semantics=sem).kind == "complete"
    if count:
        assert plan(attempt=count, cmd_semantics=sem).steps[0].detail.ordinal == count + 1


@pytest.mark.parametrize("kind", ["c", "b", "f", "removed"])
def test_plan_for_catalog_semantics_branch(kind, tmp_path):
    execution = {"c": "command: true", "b": "", "f": "watch_file: /tmp/synthetic"}
    text = (
        ""
        if kind == "removed"
        else (f"insert_job: j\njob_type: {kind}\n{execution[kind]}\nn_retrys: 2\n")
    )
    engine = Engine(lower_source(text), clock=VirtualClock(start=T0), adapters={})
    if kind != "removed":
        engine.oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    engine._facts[RID] = AttemptFacts(RID, "j", 1, 1, tmp_path)
    assert engine.plan_for(RID, natural()).kind == ("rerun" if kind == "c" else "complete")


@pytest.mark.parametrize("kind", ["c", "b", "f"])
@pytest.mark.parametrize("count", [0, 1, 20])
def test_retry_warning_scope(kind, count):
    execution = {"c": "command: true", "b": "", "f": "watch_file: /tmp/synthetic"}
    catalog = lower_source(
        f"insert_job: j\njob_type: {kind}\n{execution[kind]}\nn_retrys: {count}\n"
    )
    assert bool(rule_known_unsupported(catalog)) == (kind != "c" and count > 0)


@asynccontextmanager
async def serving(engine):
    loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
    try:
        yield loop
    finally:
        await _finish(engine, loop)


def records(root, kind):
    return [body for _, actual, body in rows(root) if actual == kind]


def terminal(engine, job="j"):
    return engine.oracle.store.job[job].status in {"SUCCESS", "FAILURE", "TERMINATED"}


@pytest.mark.parametrize(
    "count,success,marked",
    [(None, False, False), (0, False, False), (1, True, True), (20, False, False)],
)
def test_cmd_retry_success_and_exhaustion(tmp_path, count, success, marked):
    root = tmp_path / "run"
    extra = "" if count is None else f"n_retrys: {count}\n"
    count = count or 0
    command = f'test "$DSL41_ATTEMPT" -ge {count + 1}' if success else "exit 3"
    text = _jil({"j": command}, extra)

    async def scenario():
        engine = _start(root, text, marked={"j": "explicit retries"} if marked else None)
        capture = Capture()
        engine.status_hooks = capture
        async with serving(engine):
            _start_jobs(engine, "j")
            await _until(lambda: terminal(engine))
            row = engine.oracle.store.job["j"]
            assert (row.status, row.run_number) == ("SUCCESS" if success else "FAILURE", 1)
            failures = [item for item in capture.rows if item.event == "job_failure"]
            assert len(failures) == (0 if success else 1)

    asyncio.run(scenario())
    dispatches = records(root, "dispatch")
    assert [d["ordinal"] for d in dispatches] == list(range(1, count + 2))
    assert len({d["attempt"] for d in dispatches}) == count + 1
    assert len({d["env"]["DSL41_COOKIE"] for d in dispatches}) == count + 1
    for key in ("DSL41_RUN_NUMBER", "DSL41_SCHEDULED_AT"):
        assert len({d["env"][key] for d in dispatches}) == 1
    if not success:
        assert all(d["after"] == "exit 3" for d in dispatches[1:])
    completions = [b for b in records(root, "input") if b.get("source") == "adapter"]
    assert len(completions) == 1
    [history] = read_run_root(root)
    first = json.loads((Path(dispatches[0]["spool"]) / "spawn.json").read_text())
    assert history.started_at == datetime.fromisoformat(first["started_at"]).replace(tzinfo=None)


@pytest.mark.parametrize("mode", [">", ">>", ""])
def test_cmd_retry_output_streams(tmp_path, mode):
    root = tmp_path / "run"
    output, error = tmp_path / "out", tmp_path / "err"
    output.write_text("old\n")
    error.write_text("old\n")
    command = 'echo "$DSL41_ATTEMPT"; echo "$DSL41_ATTEMPT" >&2; test "$DSL41_ATTEMPT" -eq 3'
    text = _jil(
        {"j": command}, f"n_retrys: 2\nstd_out_file: {mode}{output}\nstd_err_file: {mode}{error}\n"
    )

    async def scenario():
        engine = _start(root, text)
        async with serving(engine):
            _start_jobs(engine, "j")
            await _until(lambda: terminal(engine))

    asyncio.run(scenario())
    expected = "3\n" if mode == ">" else "old\n1\n2\n3\n"
    assert output.read_text() == error.read_text() == expected


@pytest.mark.parametrize("flag", ["ON_HOLD", "ON_ICE"])
def test_cmd_retry_box_flags_and_resources(tmp_path, flag):
    root = tmp_path / "run"
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    text = (
        "insert_resource: R\nres_type: R\namount: 1\n\n"
        "insert_job: b\njob_type: b\nbox_failure: f(j)\n\n"
        "insert_job: j\njob_type: c\nbox_name: b\nn_retrys: 1\n"
        "box_terminator: y\njob_terminator: y\n"
        "resources: (R, QUANTITY=1, FREE=A)\n"
        f'command: if [ "$DSL41_ATTEMPT" = 1 ]; then exit 1; fi; read x < {gate}\n\n'
        "insert_job: failure\njob_type: c\ncommand: true\ncondition: f(j)\n\n"
        "insert_job: waiter\njob_type: c\ncommand: true\nresources: (R, QUANTITY=1, FREE=A)\n"
    )

    async def scenario():
        engine = _start(root, text)
        capture = Capture()
        engine.status_hooks = capture
        async with serving(engine):
            _start_jobs(engine, "b")
            await _spawned(root, "j", n=2)
            before = engine.oracle.store.job["j"]
            assert before.reservations and engine.oracle.store.job["b"].status == "RUNNING"
            engine.inject(Event(at=engine.clock.now(), kind=flag, payload={"job": "j"}))
            _start_jobs(engine, "waiter")
            await _until(lambda: engine.oracle.store.job["waiter"].status == "QUE_WAIT")
            row = engine.oracle.store.job["j"]
            assert not row.on_hold and not row.on_ice
            assert row.run_number == 1 and row.status_at == before.status_at
            assert row.reservations == before.reservations
            assert engine.oracle.store.job["failure"].run_number == 0
            assert not [r for r in capture.rows if r.event == "job_failure"]
            await asyncio.to_thread(gate.write_text, "go\n")
            await _until(lambda: terminal(engine, "waiter"))
            assert engine.oracle.store.job["b"].status == "SUCCESS"
            assert engine.oracle.store.job["failure"].run_number == 0

    asyncio.run(scenario())


def test_cmd_retry_original_deadline_and_independent_alarm(tmp_path, monkeypatch):
    root = tmp_path / "run"
    text = _jil(
        {"j": 'if [ "$DSL41_ATTEMPT" = 1 ]; then exit 1; fi; exec sleep 30'},
        "n_retrys: 2\nterm_run_time: 2\ndate_conditions: 1\ndays_of_week: all\n"
        'start_times: "00:00"\nmust_complete_times: +1\n',
    )

    async def scenario():
        from dsl41.runner_scheduler import Scheduler
        from dsl41.runner_startup import start_run

        catalog = lower_source(text)
        clock = RealClock()
        engine = start_run(
            catalog,
            root,
            clock=clock,
            adapters={"CMD": LocalCommandAdapter(grace_seconds=0.2)},
            scheduler=Scheduler(catalog, start=clock.now()),
            hold_open=True,
            settings=EngineSettings(grace_term_s=0.2, grace_kill_s=0.3),
        )
        capture = Capture()
        engine.status_hooks = capture
        async with serving(engine):
            _start_jobs(engine, "j")
            await _spawned(root, "j", n=2)
            # A wall step supplies due time, as in test_runner_clock_steps; the wrapper is real.
            first_status = engine.oracle.store.job["j"].status_at
            due = first_status + timedelta(minutes=2)
            assert (due, "j", "term_run_time") in engine.oracle.pending_timers()
            monkeypatch.setattr(engine.clock, "now", lambda: due + timedelta(seconds=1))
            engine.wake()
            await _until(lambda: terminal(engine))
            assert engine.oracle.store.job["j"].status == "TERMINATED"
            assert engine.oracle.store.job["j"].run_number == 1
            assert not [r for r in capture.rows if r.event == "job_failure"]
            assert [r for r in capture.rows if r.event == "alarm"]

    asyncio.run(scenario())
    assert len(records(root, "dispatch")) == 2


@pytest.mark.parametrize("remove", [False, True])
def test_update_preserves_or_discharges_committed_retry(tmp_path, monkeypatch, remove):
    from test_update_wiring import (
        Estate,
        _applied as applied,
        _jil as update_jil,
        _plan,
        _start as start_update,
        _start_jobs as force_jobs,
        _update,
    )

    root = tmp_path / "run"
    estate = Estate(tmp_path, update_jil({"j": "exit 1"}, "n_retrys: 2\n"))

    async def scenario():
        engine = start_update(root, estate)
        perform = engine.perform_plan
        held = []

        def at_plan(attempt):
            held.append(attempt)

        monkeypatch.setattr(engine, "perform_plan", at_plan)
        async with serving(engine):
            try:
                force_jobs(engine, "j")
                await _until(lambda: bool(held))
                chosen = engine._plans[held[0]].steps[0]["attempt"]
                estate.write("" if remove else update_jil({"j": "exec sleep 30"}))
                answer = await _update(engine, _plan(engine, estate))
                await applied(answer)
                monkeypatch.setattr(engine, "perform_plan", perform)
                perform(held[0])
                if remove:
                    assert held[0] not in engine._plans
                else:
                    await _spawned(root, "j", n=2)
                    assert engine.attempt_view("j")["run_id"] == chosen
            finally:
                monkeypatch.setattr(engine, "perform_plan", perform)
                for attempt in held:
                    perform(attempt)

    asyncio.run(scenario())
    assert len(records(root, "dispatch")) == (1 if remove else 2)


def test_rehearse_and_play_do_not_retry():
    from dsl41.oracle import Oracle
    from dsl41.rehearse_check import expected_bounds, run_fail_sweep
    from dsl41.derive import derive_graph

    catalog = lower_source(_jil({"j": "false"}, "n_retrys: 2\n"))

    async def scenario():
        engine = Engine(
            catalog, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter(default=(0, 1))}
        )
        try:
            _start_jobs(engine, "j")
            await engine.run_until_quiescent(T0)
            assert engine.oracle.store.job["j"].status == "FAILURE"
            assert engine.oracle.store.job["j"].run_number == 1
        finally:
            await engine.shutdown()

    asyncio.run(scenario())
    oracle = Oracle(catalog)
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "j"}))
    oracle.feed(Event(at=T0, kind="STATUS", payload={"job": "j", "exit_code": 1}))
    assert oracle.store.job["j"].status == "FAILURE" and oracle.store.job["j"].run_number == 1
    bounds = expected_bounds(catalog, derive_graph(catalog), {}, parked=set())
    cases, _ = run_fail_sweep(
        catalog,
        {"j": 1},
        bounds,
        FakeAdapter(default=(0, 0)),
        [],
        start=T0,
        horizon=T0 + timedelta(hours=1),
        producers=["j"],
    )
    assert cases[0].outcome == "inconclusive_retries"


@pytest.mark.parametrize("ending", ["signal", "spawn"])
def test_cmd_retry_final_non_exit_outcomes(tmp_path, monkeypatch, ending):
    root = tmp_path / "run"
    command = (
        'if [ "$DSL41_ATTEMPT" = 1 ]; then exit 3; fi; '
        f"exec {sys.executable} -c 'import os,signal; os.kill(os.getpid(),signal.SIGKILL)'"
    )
    text = _jil({"j": command}, "n_retrys: 20\n")

    async def scenario():
        engine = _start(root, text)
        if ending == "spawn":
            from dsl41 import runner_adapters

            prepare = runner_adapters._prepare_output_files

            def fail_second(job, spec):
                if spec["env"]["DSL41_ATTEMPT"] == "2":
                    raise OSError("synthetic second launch failure")
                prepare(job, spec)

            monkeypatch.setattr(runner_adapters, "_prepare_output_files", fail_second)
        async with serving(engine):
            _start_jobs(engine, "j")
            await _until(lambda: terminal(engine))
            assert engine.oracle.store.job["j"].status == (
                "TERMINATED" if ending == "signal" else "FAILURE"
            )

    asyncio.run(scenario())
    assert [d["ordinal"] for d in records(root, "dispatch")] == [1, 2]


def test_cmd_retry_query_and_tui_note(short_root, capsys, cli_env):
    root = short_root / "run"
    text = _jil(
        {"j": 'if [ "$DSL41_ATTEMPT" = 1 ]; then exit 3; fi; exec sleep 30'}, "n_retrys: 1\n"
    )

    async def scenario():
        engine = _start(root, text, signals=(9,))
        async with serving(engine):
            server = ControlServer(engine, root / "control.sock")
            await server.start()
            client = ControlClient(root / "control.sock")
            try:
                _start_jobs(engine, "j")
                await _spawned(root, "j", n=2)
                answer = await client.request({"cmd": "status", "job": "j", "v": 3})
                row = answer["jobs"]["j"]
                assert attempt_note(row) == "attempt 2, after exit 3"
                assert status_label(row) == "RUNNING (attempt 2, after exit 3)"
                from dsl41.runner_tui import RunnerApp

                app = RunnerApp(root / "control.sock")
                assert app._row_cells("j", row)[2] == "attempt 2, after exit 3"
                query = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "dsl41",
                    "query",
                    "status",
                    "j",
                    "--brief",
                    "-S",
                    str(root / "control.sock"),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await query.communicate()
                assert query.returncode == 0, stderr.decode()
                printed = re.sub(r"\x1b\[[0-9;]*m", "", stdout.decode())
                assert "attempt 2, after exit 3" in printed
                print(printed)
                engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
                await _until(lambda: terminal(engine))
                assert engine.oracle.store.job["j"].status == "TERMINATED"
            finally:
                await client.close()
                await server.close()

    asyncio.run(scenario())
    assert "RUNNING (attempt 2, after exit 3)" in capsys.readouterr().out
    assert len(records(root, "dispatch")) == 2


@pytest.mark.parametrize("rerun", ["retry", "interruption"])
@pytest.mark.parametrize(
    "missing",
    [None, "first", "middle", "last", "unreadable-spawn", "missing-status", "unreadable-status"],
)
def test_retry_history_spans_three_minute_attempts(tmp_path, monkeypatch, missing, rerun):
    journal = [_header()]
    for index in range(3):
        directory = tmp_path / str(index + 1)
        directory.mkdir()
        identity = {"job": "j1", "run_number": 1, "run_id": str(uuid4())}
        (directory / "spawn.json").write_text(
            json.dumps(
                {**identity, "started_at": (T0 + timedelta(minutes=index, seconds=1)).isoformat()}
            )
        )
        (directory / "status.json").write_text(
            json.dumps(
                {**identity, "ended_at": (T0 + timedelta(minutes=index + 1, seconds=1)).isoformat()}
            )
        )
        if missing == ("first", "middle", "last")[index]:
            (directory / "spawn.json").unlink()
        journal.append(
            _dispatch(
                "j1",
                1,
                run_dir=str(directory),
                started_at=T0 + timedelta(minutes=index),
                run_id=identity["run_id"],
            )
        )
        if index:
            journal[-1]["after"] = "exit 3" if rerun == "retry" else "engine_stop"
    if missing == "missing-status":
        (tmp_path / "3/status.json").unlink()
    if missing in {"unreadable-spawn", "unreadable-status"}:
        unreadable = (
            tmp_path / "3" / ("spawn.json" if missing == "unreadable-spawn" else "status.json")
        )
        open_record = Path.open

        def refuse_latest(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError("synthetic unreadable latest record")
            return open_record(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_latest)
    journal.extend(
        [
            _status_input(1, T0 + timedelta(minutes=3), job="j1", run_number=1, exit_code=0),
            _applied(1),
            _dispatch("other", 1, run_dir=None, started_at=T0),
        ]
    )
    spool = runner_history._read_all_spool(journal)
    catalog = lower_source(_jil({"j1": "true"}, f"n_retrys: {2 if rerun == 'retry' else 0}\n"))
    result = fold_run_rows(journal, spool=spool, catalog=catalog)
    row = next(row for row in result if row.job == "j1")
    journal_clock = missing in {"last", "unreadable-spawn", "missing-status", "unreadable-status"}
    assert row.duration_s == (60 if missing == "first" else 180)
    assert row.clock_source == ("journal" if journal_clock else "spool")
    if journal_clock:
        assert row.started_at == T0 and row.ended_at == T0 + timedelta(minutes=3)
    else:
        assert row.started_at == T0 + timedelta(seconds=121 if missing == "first" else 1)
        assert row.ended_at == T0 + timedelta(minutes=3, seconds=1)


@pytest.mark.parametrize("rerun", ["retry", "interruption"])
def test_later_spawn_failure_history_uses_first_dispatch(tmp_path, rerun):
    first, later = tmp_path / "1", tmp_path / "2"
    first.mkdir()
    later.mkdir()
    first_id, later_id = str(uuid4()), str(uuid4())
    identity = {"job": "j1", "run_number": 1, "run_id": first_id}
    (first / "spawn.json").write_text(
        json.dumps({**identity, "started_at": (T0 + timedelta(seconds=1)).isoformat()})
    )
    (first / "status.json").write_text(
        json.dumps(
            {
                **identity,
                "outcome": "exited",
                "exit_code": 3,
                "ended_at": (T0 + timedelta(seconds=3)).isoformat(),
            }
        )
    )
    (later / "status.json").write_text(
        json.dumps(
            {
                **identity,
                "run_id": later_id,
                "outcome": "spawn_failed",
                "error": "synthetic failure",
                "ended_at": (T0 + timedelta(seconds=5)).isoformat(),
            }
        )
    )
    replacement = _dispatch(
        "j1", 1, run_dir=str(later), started_at=T0 + timedelta(seconds=4), run_id=later_id
    )
    replacement["after"] = "exit 3" if rerun == "retry" else "engine_stop"
    journal = [
        _header(),
        _dispatch("j1", 1, run_dir=str(first), started_at=T0, run_id=first_id),
        replacement,
        _status_input(
            1,
            T0 + timedelta(seconds=5),
            job="j1",
            run_number=1,
            status="FAILURE",
            cause="wrapper_spawn_failed",
        ),
        _applied(1),
    ]
    catalog = lower_source(_jil({"j1": "true"}, f"n_retrys: {2 if rerun == 'retry' else 0}\n"))
    [row] = fold_run_rows(journal, spool=runner_history._read_all_spool(journal), catalog=catalog)
    assert row.started_at == T0 and row.ended_at == T0 + timedelta(seconds=5)
    assert row.duration_s == 5 and row.clock_source == "journal"
    assert row.status == "FAILURE" and row.run_dir == str(later)


def test_retry_ordinal_25_round_trips_and_formats(tmp_path):
    record = dispatch_record(
        "j",
        1,
        attempt=RID,
        ordinal=25,
        spool=str(tmp_path / "25"),
        started_at=T0,
        rev=1,
        after="exit 3",
    )
    check_dispatch(record.payload)
    table = OpenTable()
    table.note(1, record)
    restored = OpenTable.from_payload(table.to_payload())
    assert restored.attempts[RID].ordinal == 25 and restored.floor() == 1
    assert attempt_note({"attempt": {"n": 25, "after": "exit 3"}}) == "attempt 25, after exit 3"


@pytest.mark.parametrize("expired", [False, True])
def test_restart_natural_failure_defers_until_due_work(tmp_path, expired):
    root = tmp_path / "run"
    text = _jil({"j": "true"}, "n_retrys: 2\nterm_run_time: 10\n")
    [dispatch] = _open(root, text, ["j"])
    _write(dispatch, "spawn.json", started_at=T0.isoformat())
    _write(
        dispatch,
        "status.json",
        outcome="exited",
        exit_code=1,
        ended_at=(T0 + timedelta(minutes=1)).isoformat(),
    )

    async def scenario():
        engine = await resume_virtual(root, text, minutes=20 if expired else 2)
        try:
            await engine.run_until_quiescent(engine.clock.now() + timedelta(minutes=1))
            assert engine.oracle.store.job["j"].status == ("TERMINATED" if expired else "RUNNING")
        finally:
            await _close(engine)

    asyncio.run(scenario())
    assert [d["ordinal"] for d in records(root, "dispatch")] == ([1] if expired else [1, 2])
    [chosen] = [p for p in records(root, "follow_up") if p.get("event") == "plan"]
    assert chosen["plan"] == "rerun" and chosen["steps"][0]["after"] == "exit 1"
    [done] = [p for p in records(root, "follow_up") if p.get("event") == "done"]
    assert (done["seq"] is None) == expired


def test_cmd_retry_natural_failure_during_drain_is_owed_once(tmp_path, monkeypatch):
    from dsl41.engine_charts import DrainRequest

    root = tmp_path / "run"
    text = _jil(
        {"j": 'if [ "$DSL41_ATTEMPT" = 1 ]; then exit 1; fi; exec sleep 30'}, "n_retrys: 1\n"
    )

    async def first():
        engine = _start(root, text)
        record = engine.record_outcome

        def drain_at_record(attempt, outcome):
            if outcome.kind == "natural":
                engine.lifecycle.stop_requested(DrainRequest(time.monotonic(), 0.5, 1.0))
            record(attempt, outcome)

        monkeypatch.setattr(engine, "record_outcome", drain_at_record)
        async with serving(engine) as loop:
            _start_jobs(engine, "j")
            await asyncio.wait_for(asyncio.shield(loop), 30)

    asyncio.run(first())
    assert len(records(root, "dispatch")) == 1

    async def second():
        engine = await _resume(root, text)
        async with serving(engine):
            await _spawned(root, "j", n=2)
            assert engine.oracle.store.job["j"].run_number == 1

    asyncio.run(second())
    assert [d["ordinal"] for d in records(root, "dispatch")] == [1, 2]


@pytest.mark.parametrize("boundary", ["try", "outcome", "dispatch"])
def test_cmd_retry_real_engine_death_boundaries(tmp_path, boundary):
    root = tmp_path / "run"
    text = _jil(
        {"j": 'if [ "$DSL41_ATTEMPT" = 1 ]; then exit 3; fi; exec sleep 30'}, "n_retrys: 3\n"
    )
    source = tmp_path / "jobs.jil"
    source.write_text(text)
    ready = tmp_path / "ready"
    program = (
        "import asyncio,sys; sys.path.insert(0,sys.argv[1]); "
        "from test_cmd_retries import crash_driver; "
        "asyncio.run(crash_driver(*sys.argv[2:]))"
    )

    async def scenario():
        with (tmp_path / "driver.log").open("w") as log:
            driver = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    program,
                    str(Path(__file__).parent),
                    str(root),
                    str(source),
                    str(ready),
                    boundary,
                ],
                stdout=log,
                stderr=log,
            )
            try:
                await _until(lambda: ready.exists() or driver.poll() is not None)
                assert ready.exists(), (tmp_path / "driver.log").read_text()
                driver.kill()
                driver.wait(timeout=10)
            finally:
                if driver.poll() is None:
                    driver.kill()
                    driver.wait(timeout=10)
        if boundary == "try":
            await _until((root / "runs/j.1/2/status.json").exists)
        target = 2 if boundary == "outcome" else 3
        engine = await _resume(root, text)
        async with serving(engine):
            await _spawned(root, "j", n=target)
            assert engine.oracle.store.job["j"].run_number == 1

    asyncio.run(scenario())
    dispatches = records(root, "dispatch")
    assert [d["ordinal"] for d in dispatches] == list(
        range(1, (2 if boundary == "outcome" else 3) + 1)
    )
    assert len({d["attempt"] for d in dispatches}) == len(dispatches)
    if boundary == "outcome":
        chosen = next(p for p in records(root, "follow_up") if p.get("plan") == "rerun")
        assert dispatches[1]["attempt"] == chosen["steps"][0]["attempt"]


async def crash_driver(root, source, ready, boundary):
    root, ready = Path(root), Path(ready)
    engine = _start(root, Path(source).read_text())
    perform = engine.perform_plan
    launch = engine._launch

    def pause():
        ready.touch()
        os.kill(os.getpid(), signal.SIGSTOP)

    def at_plan(attempt):
        if boundary == "outcome":
            pause()
        perform(attempt)

    def at_launch(*args, **kwargs):
        if boundary == "dispatch" and kwargs.get("env", {}).get("DSL41_ATTEMPT") == "2":
            pause()
        launch(*args, **kwargs)

    engine.perform_plan = at_plan
    engine._launch = at_launch
    _start_jobs(engine, "j")
    loop = asyncio.create_task(engine.run_until_quiescent(datetime.max))
    if boundary == "try":
        await _spawned(root, "j", n=2)
        ready.touch()
    await loop


@pytest.mark.parametrize("change", ["lower", "raise", "unset", "remove"])
def test_cmd_retry_updates(tmp_path, change):
    from test_update_wiring import (
        Estate,
        _applied as applied,
        _jil as update_jil,
        _plan,
        _start as start_update,
        _start_jobs as force_jobs,
        _update,
    )

    root = tmp_path / "run"
    initial = 0 if change == "raise" else 20
    threshold = 0 if change == "raise" else 7
    command = f'if [ "$DSL41_ATTEMPT" -le {threshold} ]; then exit 1; fi; exec sleep 30'
    original = update_jil({"j": command}, f"n_retrys: {initial}\n")
    estate = Estate(tmp_path, original)

    async def scenario():
        engine = start_update(root, estate)
        async with serving(engine):
            force_jobs(engine, "j")
            await _spawned(root, "j", n=1 if change == "raise" else 8)
            count = {"lower": 2, "raise": 2, "unset": 0, "remove": 0}[change]
            extra = "" if change == "unset" else f"n_retrys: {count}\n"
            changed = update_jil({"j": "exec sleep 30"}, extra) if change != "remove" else ""
            estate.write(changed)
            answer = await _update(engine, _plan(engine, estate))
            body = await applied(answer)
            expected = "rerun" if change == "raise" else "none" if change == "remove" else "hold"
            assert body["stopped"] == [["j", expected]]
            if expected == "hold":
                assert engine.oracle.store.job["j"].on_hold
                assert engine.evidence["j"]["limit"] == 5 + count
                assert engine.oracle.store.job["j"].status == "FAILURE"
            elif expected == "rerun":
                await _spawned(root, "j", n=2)
                assert engine.attempt_view("j")["after"] == "update_stop"
            else:
                assert "j" not in engine.oracle.catalog.jobs

    asyncio.run(scenario())
    if change != "raise":
        assert len(records(root, "dispatch")) == 8


def test_update_keeps_natural_failure_and_uses_applied_count(tmp_path, monkeypatch):
    from test_update_wiring import (
        Estate,
        _applied as applied,
        _jil as update_jil,
        _plan,
        _start as start_update,
        _start_jobs as force_jobs,
        _update,
    )

    root = tmp_path / "run"
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    estate = Estate(tmp_path, update_jil({"j": f"read x < {gate}; exit 1"}))

    async def scenario():
        engine = start_update(root, estate)
        async with serving(engine):
            force_jobs(engine, "j")
            await _spawned(root, "j")
            stop = engine.update.stop_attempt

            def delay_term(job, cause):
                if job != "j":
                    stop(job, cause)

            monkeypatch.setattr(engine.update, "stop_attempt", delay_term)
            estate.write(update_jil({"j": "exec sleep 30"}, "n_retrys: 2\n"))
            answer = await _update(engine, _plan(engine, estate))
            assert engine.update.stopping
            await asyncio.to_thread(gate.write_text, "go\n")
            body = await applied(answer)
            assert body["stopped"] == [["j", "rerun"]]
            await _spawned(root, "j", n=2)
            assert engine.attempt_view("j")["after"] == "exit 1"

    asyncio.run(scenario())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="leaderless leak needs the cookie scan"
)
def test_cmd_retry_killjob_during_leftover_ladder(tmp_path, monkeypatch):
    root = tmp_path / "run"
    text = _jil({"j": "sleep 30 & exit 1"}, "n_retrys: 2\n")

    async def scenario():
        engine = _start(root, text, signals=(9,))
        release = asyncio.Event()
        ladder = engine.stops.ladder

        async def paused(*args):
            await release.wait()
            await ladder(*args)

        monkeypatch.setattr(engine.stops, "ladder", paused)
        async with serving(engine):
            try:
                _start_jobs(engine, "j")
                await _until(lambda: bool(engine._waiting_plans))
                assert engine.oracle.store.job["j"].status == "RUNNING"
                engine.inject(Event(at=engine.clock.now(), kind="KILLJOB", payload={"job": "j"}))
                await _until(lambda: terminal(engine))
                release.set()
                await _until(lambda: not engine._plans)
                assert engine.oracle.store.job["j"].status == "TERMINATED"
            finally:
                release.set()

    asyncio.run(scenario())
    assert len(records(root, "dispatch")) == 1
    [done] = [p for p in records(root, "follow_up") if p.get("event") == "done"]
    assert done["seq"] is None


def test_closing_natural_retry_under_leak_launches_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from store_surgery import append
    from test_restart import _set_cookie
    from dsl41 import runner_procid, runner_stops
    from dsl41.runner import outcome_record, plan_record

    root = tmp_path / "run"
    text = _jil({"j": "true"}, "n_retrys: 1\n")
    [dispatch] = _open(root, text, ["j"])
    _set_cookie(root)
    attempt = dispatch["attempt"]
    _write(
        dispatch,
        "spawn.json",
        command_pid=11,
        command_pgid=11,
        command_start_time="ticks:11",
        started_at=T0.isoformat(),
    )
    ended = outcome_record(attempt, "j", 1, "natural", {"exit_code": 1}, T0)
    append(root, ended.kind, {**ended.payload, "ladder": "running"}, at=T0)
    replacement = str(uuid4())
    step = {
        "step": "dispatch",
        "attempt": replacement,
        "ordinal": 2,
        "env": {**dispatch["env"], "DSL41_ATTEMPT": "2"},
        "after": "exit 1",
    }
    chosen = plan_record(attempt, "j", 1, "rerun", [step], T0)
    append(root, chosen.kind, chosen.payload, at=T0)
    monkeypatch.setattr(runner_stops, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(runner_procid, "pidfd_supported", lambda: False)
    monkeypatch.setattr(runner_procid, "verify_alive", lambda *_: False)
    monkeypatch.setattr(runner_procid, "linux_group_pids", lambda *_: {})
    monkeypatch.setattr(
        runner_procid, "ours", lambda *_: runner_procid.Ours({13: (attempt, "ticks:13")})
    )
    monkeypatch.setattr(
        runner_procid, "signal_ours", lambda *_a, **_k: pytest.fail("leak signalled")
    )
    monkeypatch.setattr(runner_stops.EngineStops, "leak_policy", property(lambda _: "leak"))

    async def scenario():
        engine = await resume_virtual(root, text)
        try:
            await engine.run_until_quiescent(engine.clock.now() + timedelta(minutes=1))
            assert engine.started.is_set() and not engine._waiting_plans
        finally:
            await _close(engine)

    asyncio.run(scenario())
    assert [d["attempt"] for d in records(root, "dispatch")] == [attempt, replacement]
    assert any(p.get("event") == "ladder_done" for p in records(root, "follow_up"))


def test_cmd_natural_failure_after_injected_inactive_does_not_retry(tmp_path):
    root = tmp_path / "run"
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    text = _jil({"j": f"read x < {gate}; exit 1"}, "n_retrys: 2\n")

    async def scenario():
        engine = _start(root, text)
        async with serving(engine):
            _start_jobs(engine, "j")
            await _spawned(root, "j")
            engine.inject(
                Event(
                    at=engine.clock.now(), kind="STATUS", payload={"job": "j", "status": "INACTIVE"}
                )
            )
            await _until(lambda: engine.oracle.store.job["j"].status == "INACTIVE")
            await asyncio.to_thread(gate.write_text, "go\n")
            await _until(lambda: bool(records(root, "outcome")))
            chosen = next(p for p in records(root, "follow_up") if p.get("event") == "plan")
            assert chosen["plan"] == "complete"
            assert len(records(root, "dispatch")) == 1

    asyncio.run(scenario())
