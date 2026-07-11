"""ControlServer + CLI tests (phase 11c).

Normative spec: docs/runner-design.md ss10 (control plane), ss9 (time
domains), ss13 (testing), and runner.py's own 11c docstring block (DL-45
pins the decisions). House style follows test_runner_adapters.py: real
domain (RealClock + FakeAdapter, durations as short real sleeps),
asyncio.run per scenario, tmp_path run roots.

Every expected outcome here was verified empirically against the real
ControlServer/CLI before the assertion was written (CLAUDE.md: fidelity is
tested, not asserted) -- see the final report for anything that surprised us
or contradicted the design doc.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import signal
import socket as socket_mod
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle import Event
from dsl41.runner import (
    ControlServer,
    Engine,
    EngineError,
    FakeAdapter,
    RealClock,
    read_journal,
    start_run,
)

if not sys.platform.startswith(("linux", "darwin")):  # pragma: no cover
    pytest.skip("unix-domain control sockets are POSIX-only", allow_module_level=True)

cli_runner = CliRunner()


@pytest.fixture
def short_root():
    """A short-path base directory for AF_UNIX control sockets. pytest's
    default tmp_path lives deep under the platform temp dir and can exceed
    sun_path's length limit (104 bytes on macOS) once run_root/control.sock
    is appended -- unlike ordinary files, unix-socket paths have no
    workaround for that, so tests that bind a control socket use this
    instead of tmp_path."""
    d = tempfile.mkdtemp(prefix="dsl41c-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def wait_for(predicate, timeout_s: float = 5.0, interval_s: float = 0.02):
    """Poll (blocking) until predicate() is truthy; return its value. Loud on
    timeout. For the SEPARATE-PROCESS CLI integration test only -- in-process
    ControlServer tests must use the async helpers below (a blocking socket
    call on the same thread/loop as the server would deadlock it)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


def _sync_control_call(sock_path: Path, request: dict, timeout: float = 5.0) -> dict:
    """Blocking control-socket round trip -- ONLY for the subprocess test,
    where the engine lives in a different process/loop."""
    conn = socket_mod.socket(socket_mod.AF_UNIX)
    conn.settimeout(timeout)
    conn.connect(str(sock_path))
    conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    conn.close()
    return json.loads(buf)


async def _control_call(sock_path: Path, request: dict) -> dict:
    """Async one-shot control-socket round trip for in-process tests (task
    spec: 'Connect with asyncio.open_unix_connection')."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(json.dumps(request).encode("utf-8") + b"\n")
        await writer.drain()
        line = await reader.readline()
        return json.loads(line)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _wait_for_async(predicate, timeout_s: float = 3.0, interval_s: float = 0.02):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {predicate}")


async def _serve(
    run_root: Path, text: str, *, adapter: FakeAdapter | None = None, scheduler=None
) -> tuple[Engine, ControlServer, asyncio.Task]:
    """Shared harness: a real-domain, hold_open engine serving a control
    socket, with run_until_quiescent(datetime.max) as a background task (the
    exact shape `dsl41 run` drives, ss10)."""
    catalog = lower_source(text)
    clock = RealClock()
    adapter = adapter if adapter is not None else FakeAdapter()
    engine = start_run(
        catalog,
        run_root,
        clock=clock,
        adapters={"CMD": adapter, "FW": adapter},
        scheduler=scheduler,
        hold_open=True,
    )
    server = ControlServer(engine, run_root / "control.sock")
    await server.start()
    loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
    return engine, server, loop_task


async def _teardown(engine: Engine, server: ControlServer, loop_task: asyncio.Task) -> None:
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task
    await server.close()
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()


# ------------------------------------------------------------------ 1. sendevent


def test_sendevent_startjob_drives_a_job_to_success(short_root: Path) -> None:
    text = "insert_job: sv_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("sv_job", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "sv_job"}
            )
            assert resp["ok"] is True

            async def succeeded() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "sv_job"})
                return r["ok"] and r["jobs"]["sv_job"]["status"] == "SUCCESS"

            await _wait_for_async(succeeded)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_sendevent_unknown_job_is_rejected(short_root: Path) -> None:
    text = "insert_job: uk_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "does-not-exist"}
            )
            assert resp["ok"] is False
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_set_global_then_a_value_conditioned_job_fires(short_root: Path) -> None:
    text = "insert_job: gflag_job\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"

    async def scenario() -> None:
        adapter = FakeAdapter(default=(0.05, 0))
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _control_call(
                server.path,
                {"cmd": "sendevent", "event": "SET_GLOBAL", "name": "FLAG", "value": "go"},
            )
            assert resp["ok"] is True

            async def started() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "gflag_job"})
                return r["jobs"]["gflag_job"]["status"] != "INACTIVE"

            await _wait_for_async(started)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_change_status_bad_status_rejected_valid_status_updates_the_store(short_root: Path) -> None:
    text = "insert_job: cs_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            bad = await _control_call(
                server.path,
                {"cmd": "sendevent", "event": "CHANGE_STATUS", "job": "cs_job", "status": "BOGUS"},
            )
            assert bad["ok"] is False

            good = await _control_call(
                server.path,
                {
                    "cmd": "sendevent",
                    "event": "CHANGE_STATUS",
                    "job": "cs_job",
                    "status": "SUCCESS",
                    "exit_code": 0,
                },
            )
            assert good["ok"] is True

            async def updated() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "cs_job"})
                return r["jobs"]["cs_job"]["status"] == "SUCCESS"

            await _wait_for_async(updated)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_every_accepted_control_event_is_journaled_with_source_control(short_root: Path) -> None:
    text = "insert_job: jr_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            await _control_call(server.path, {"cmd": "sendevent", "event": "ON_HOLD", "job": "jr_job"})
            await _control_call(
                server.path, {"cmd": "sendevent", "event": "OFF_HOLD", "job": "jr_job"}
            )
            await _control_call(
                server.path,
                {"cmd": "sendevent", "event": "SET_GLOBAL", "name": "G1", "value": "v1"},
            )

            async def all_seen() -> bool:
                records = read_journal(run_root / "journal.jsonl")
                kinds = [r["kind"] for r in records if r.get("rec") == "input"]
                return "ON_HOLD" in kinds and "OFF_HOLD" in kinds and "SET_GLOBAL" in kinds

            await _wait_for_async(all_seen)
            records = read_journal(run_root / "journal.jsonl")
            control_inputs = [
                r
                for r in records
                if r.get("rec") == "input" and r["kind"] in ("ON_HOLD", "OFF_HOLD", "SET_GLOBAL")
            ]
            assert len(control_inputs) == 3
            assert all(r["source"] == "control" for r in control_inputs)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_on_hold_off_hold_roundtrip_visible_in_status_flags(short_root: Path) -> None:
    text = "insert_job: hold_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            await _control_call(
                server.path, {"cmd": "sendevent", "event": "ON_HOLD", "job": "hold_job"}
            )

            async def held() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "hold_job"})
                return r["jobs"]["hold_job"]["on_hold"] is True

            await _wait_for_async(held)

            await _control_call(
                server.path, {"cmd": "sendevent", "event": "OFF_HOLD", "job": "hold_job"}
            )

            async def released() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "hold_job"})
                return r["jobs"]["hold_job"]["on_hold"] is False

            await _wait_for_async(released)
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------------------------- 2. queries


def test_status_query_all_single_and_unknown(short_root: Path) -> None:
    text = (
        "insert_job: st_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: st_b\njob_type: c\ncommand: y\nmachine: m1\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            all_resp = await _control_call(server.path, {"cmd": "status"})
            assert all_resp["ok"] is True
            assert set(all_resp["jobs"]) == {"st_a", "st_b"}
            assert all_resp["jobs"]["st_a"]["status"] == "INACTIVE"

            single = await _control_call(server.path, {"cmd": "status", "job": "st_b"})
            assert single["ok"] is True
            assert set(single["jobs"]) == {"st_b"}

            unknown = await _control_call(server.path, {"cmd": "status", "job": "nope"})
            assert unknown["ok"] is False
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_trace_query_since_filtering(short_root: Path) -> None:
    text = "insert_job: tr_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("tr_job", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "tr_job"}
            )
            assert resp["ok"] is True

            async def has_three() -> bool:
                r = await _control_call(server.path, {"cmd": "trace"})
                return r["ok"] and len(r["entries"]) >= 3

            await _wait_for_async(has_three)

            full = await _control_call(server.path, {"cmd": "trace"})
            assert full["ok"] is True
            assert len(full["entries"]) == full["last_seq"]
            assert [e["seq"] for e in full["entries"]] == list(range(1, full["last_seq"] + 1))

            partial = await _control_call(server.path, {"cmd": "trace", "since": 1})
            assert [e["seq"] for e in partial["entries"]] == list(range(2, full["last_seq"] + 1))
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_explain_null_condition_and_status_atom_truth_before_and_after(short_root: Path) -> None:
    text = (
        "insert_job: ex_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: ex_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(ex_a)\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            none_resp = await _control_call(server.path, {"cmd": "explain", "job": "ex_a"})
            assert none_resp == {
                "ok": True,
                "job": "ex_a",
                "condition": None,
                "satisfied": True,
                "atoms": [],
            }

            before = await _control_call(server.path, {"cmd": "explain", "job": "ex_b"})
            assert before["ok"] is True
            assert before["condition"] == "s(ex_a)"
            assert before["satisfied"] is False
            assert before["atoms"] == [{"atom": "s(ex_a)", "true": False}]

            resp = await _control_call(
                server.path,
                {"cmd": "sendevent", "event": "CHANGE_STATUS", "job": "ex_a", "status": "SUCCESS"},
            )
            assert resp["ok"] is True

            async def satisfied() -> bool:
                r = await _control_call(server.path, {"cmd": "explain", "job": "ex_b"})
                return bool(r["satisfied"])

            await _wait_for_async(satisfied)
            after = await _control_call(server.path, {"cmd": "explain", "job": "ex_b"})
            assert after["satisfied"] is True
            assert after["atoms"] == [{"atom": "s(ex_a)", "true": True}]
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_plan_waves_for_a_chain_and_a_cycle_refuses(short_root: Path) -> None:
    chain_text = (
        "insert_job: pl_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: pl_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(pl_a)\n\n"
        "insert_job: pl_c\njob_type: c\ncommand: z\nmachine: m1\ncondition: s(pl_b)\n"
    )
    cycle_text = (
        "insert_job: cy_a\njob_type: c\ncommand: x\nmachine: m1\ncondition: s(cy_b)\n\n"
        "insert_job: cy_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(cy_a)\n"
    )

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run_chain", chain_text)
        try:
            resp = await _control_call(server.path, {"cmd": "plan"})
            assert resp == {"ok": True, "waves": [["pl_a"], ["pl_b"], ["pl_c"]]}
        finally:
            await _teardown(engine, server, loop_task)

        engine2, server2, loop_task2 = await _serve(short_root / "run_cycle", cycle_text)
        try:
            resp2 = await _control_call(server2.path, {"cmd": "plan"})
            assert resp2["ok"] is False
        finally:
            await _teardown(engine2, server2, loop_task2)

    asyncio.run(scenario())


# ------------------------------------------------------------------ 3. subscribe


def test_subscribe_backfills_since_zero_then_streams_a_live_record_once(short_root: Path) -> None:
    text = "insert_job: sub_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(run_root, text)
        try:
            r1 = await _control_call(
                server.path, {"cmd": "sendevent", "event": "ON_HOLD", "job": "sub_job"}
            )
            assert r1["ok"] is True

            async def journaled() -> bool:
                records = read_journal(run_root / "journal.jsonl")
                return any(r.get("kind") == "ON_HOLD" for r in records)

            await _wait_for_async(journaled)

            reader, writer = await asyncio.open_unix_connection(str(server.path))
            try:
                writer.write(json.dumps({"cmd": "subscribe", "since": 0}).encode() + b"\n")
                await writer.drain()
                ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert ack == {"ok": True, "subscribed": True}

                backfilled = []
                seen_header = False
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    record = json.loads(line)
                    backfilled.append(record)
                    if record.get("rec") == "header":
                        seen_header = True
                    if record.get("kind") == "ON_HOLD":
                        break
                assert seen_header
                assert sum(1 for r in backfilled if r.get("kind") == "ON_HOLD") == 1

                # a NEW live event, sent only after the backfill is fully drained
                r2 = await _control_call(
                    server.path, {"cmd": "sendevent", "event": "OFF_HOLD", "job": "sub_job"}
                )
                assert r2["ok"] is True
                live = json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))
                assert live.get("kind") == "OFF_HOLD"
                # the seq'd input record must not have been duplicated across
                # the backfill/live seam
                assert sum(1 for r in backfilled if r.get("kind") == "OFF_HOLD") == 0
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------------------- 4. socket hygiene


def test_control_socket_file_mode_is_0600(short_root: Path) -> None:
    text = "insert_job: perm_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            mode = server.path.stat().st_mode & 0o777
            assert mode == 0o600
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_stale_socket_file_is_detected_and_claimed(short_root: Path) -> None:
    text = "insert_job: stale_job\njob_type: c\ncommand: x\nmachine: m1\n"
    run_root = short_root / "run"

    async def scenario() -> None:
        run_root.mkdir()
        sock_path = run_root / "control.sock"
        dead = socket_mod.socket(socket_mod.AF_UNIX)
        dead.bind(str(sock_path))
        dead.close()  # nothing listening: a crashed run's leftover
        assert sock_path.exists()

        catalog = lower_source(text)
        engine = start_run(catalog, run_root, clock=RealClock(), adapters={"CMD": FakeAdapter()})
        server = ControlServer(engine, sock_path)
        await server.start()  # must claim it silently, never raise
        try:
            assert sock_path.exists()
            assert (sock_path.stat().st_mode & 0o777) == 0o600
        finally:
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())


def test_live_socket_refuses_a_second_engine(short_root: Path) -> None:
    text = "insert_job: live_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            second = ControlServer(engine, server.path)
            with pytest.raises(EngineError, match="live"):
                await second.start()
            # the refused probe left a half-accepted connection on the live
            # server; a real round-trip both proves the server survived the
            # probe AND drains that connection through its handler before
            # teardown (unix accepts process in order) -- without it the
            # probe's transport can still be mid-accept when the server
            # closes, and its GC trips after the loop is gone
            response = await _control_call(server.path, {"cmd": "status"})
            assert response["ok"] is True
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


# ------------------------------------------------- 5. commit-discipline regression


def test_fast_real_completion_processed_before_a_far_later_term_run_time_timer() -> None:
    """DL-45 decision 1 + review T2 regression, two engine bugs in one net.
    A job held RUNNING by an inert adapter arms a term_run_time timer 60s in
    the future; a second, scripted job completes after ~0.1s of real time.
    The pre-11c engine journaled the advance and slept UNINTERRUPTIBLY until
    the timer's instant, so the fast completion (stamped mid-sleep) fed
    BEHIND the already-advanced oracle clock and crashed with OracleError
    "feed time went backwards". The first 11c cut then had the T2 shortcut:
    with the only KNOWN due instant beyond the horizon it returned in
    microseconds, abandoning the live adapter whose completion had no due
    timestamp. The fixed engine processes the completion promptly, waits
    out the horizon for the still-live job, and returns at the horizon --
    fast is SUCCESS, no crash, ~2s wall time."""
    text = (
        "insert_job: parked\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 1\n\n"
        "insert_job: fast\njob_type: c\ncommand: y\nmachine: m1\n"
    )

    async def scenario() -> float:
        clock = RealClock()
        adapter = FakeAdapter({("fast", 1): (0.1, 0)}, default=None)
        engine = Engine(lower_source(text), clock=clock, adapters={"CMD": adapter, "FW": adapter})
        now = clock.now()
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "parked"}))
        engine.inject(Event(at=now, kind="STARTJOB", payload={"job": "fast"}))
        t0 = time.monotonic()
        await engine.run_until_quiescent(now + timedelta(seconds=2))
        elapsed = time.monotonic() - t0
        assert engine.oracle.store.job["fast"].status == "SUCCESS"
        await engine.shutdown()
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 5.0


# ------------------------------------------------------------------------ 6. CLI


def test_cli_rehearse_scheduled_estate_deterministic_start_hours(tmp_path: Path) -> None:
    jil = tmp_path / "estate.jil"
    jil.write_text(
        "insert_job: reh_job\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n',
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app, ["rehearse", str(jil), "--start", "2026-07-06T08:00:00", "--hours", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "reh_job" in result.output
    assert "SUCCESS" in result.output


def test_cli_rehearse_with_a_scenario_file(tmp_path: Path) -> None:
    jil = tmp_path / "estate.jil"
    jil.write_text("insert_job: sc_job\njob_type: c\ncommand: x\nmachine: m1\n", encoding="utf-8")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "adapter": {
                    "default": None,
                    "runs": [{"job": "sc_job", "run_number": 1, "duration_s": 0.0, "exit_code": 0}],
                },
                "events": [
                    {"at": "2026-07-06T08:00:00", "kind": "STARTJOB", "payload": {"job": "sc_job"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--scenario",
            str(scenario),
            "--start",
            "2026-07-06T08:00:00",
            "--hours",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sc_job" in result.output
    assert "SUCCESS" in result.output


def test_cli_rehearse_preflight_error_estate_exits_2(tmp_path: Path) -> None:
    jil = tmp_path / "bad.jil"
    jil.write_text(
        "insert_job: cal_job\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\nrun_calendar: some_cal\n",
        encoding="utf-8",
    )
    result = cli_runner.invoke(app, ["rehearse", str(jil)])
    assert result.exit_code == 2
    assert "calendar" in result.output


def test_cli_run_subprocess_sendevent_and_query_end_to_end(short_root: Path) -> None:
    """Integration test: spawn `dsl41 run` as a real subprocess (the pattern
    tests/test_runner_lifecycle.py uses), wait for its control socket, drive
    it with sendevent/query over the wire, then stop it with SIGTERM."""
    jil = short_root / "estate.jil"
    jil.write_text("insert_job: proc_job\njob_type: c\ncommand: exit 0\n", encoding="utf-8")
    run_root = short_root / "run"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from dsl41.cli import app; app()",
            "run",
            str(jil),
            "--run-root",
            str(run_root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        sock_path = run_root / "control.sock"
        wait_for(lambda: sock_path.exists(), timeout_s=5.0)

        resp = wait_for(
            lambda: _sync_control_call(
                sock_path, {"cmd": "sendevent", "event": "STARTJOB", "job": "proc_job"}
            ),
            timeout_s=5.0,
        )
        assert resp["ok"] is True

        def succeeded() -> bool:
            r = _sync_control_call(sock_path, {"cmd": "status", "job": "proc_job"})
            return bool(r["ok"]) and r["jobs"]["proc_job"]["status"] == "SUCCESS"

        wait_for(succeeded, timeout_s=5.0)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5.0)
        assert proc.returncode == 0
        assert (run_root / "journal.jsonl").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --------------------------------------------------- 7. status query extensions (11d, DL-46)


def test_status_pending_timers_visible_while_running_then_gone_after_completion(
    short_root: Path,
) -> None:
    """(runner-design ss11, DL-46): the status response's `pending_timers`
    field mirrors Oracle.pending_timers()'s own liveness rule -- a
    term_run_time deadline shows up while the run is RUNNING (a slow
    FakeAdapter script holds it there) and is gone once the run completes
    naturally, well before the deadline itself would ever fire."""
    text = "insert_job: pte_job\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 10\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("pte_job", 1): (0.3, 0)}, default=None)
        engine, server, loop_task = await _serve(short_root / "run", text, adapter=adapter)
        try:
            resp = await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "pte_job"}
            )
            assert resp["ok"] is True

            async def running() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "pte_job"})
                return r["jobs"]["pte_job"]["status"] == "RUNNING"

            await _wait_for_async(running)
            while_running = await _control_call(server.path, {"cmd": "status", "job": "pte_job"})
            timers = while_running["jobs"]["pte_job"]["pending_timers"]
            assert len(timers) == 1
            assert timers[0]["kind"] == "term_run_time"

            async def done() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "pte_job"})
                return r["jobs"]["pte_job"]["status"] == "SUCCESS"

            await _wait_for_async(done)
            after = await _control_call(server.path, {"cmd": "status", "job": "pte_job"})
            assert after["jobs"]["pte_job"]["pending_timers"] == []
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_log_paths_default_shape_for_a_ran_cmd_job(short_root: Path) -> None:
    """(runner-design ss6/ss11, DL-46): with no std_out_file/std_err_file,
    a ran CMD job's log_out/log_err resolve to
    <run_root>/logs/<job>.<run_number>.{out,err} -- job_log_paths()'s
    default shape, the same resolver the LocalCommandAdapter uses."""
    run_root = short_root / "run"
    text = "insert_job: ple_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("ple_job", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(run_root, text, adapter=adapter)
        try:
            await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "ple_job"}
            )

            async def dispatched() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "ple_job"})
                return r["jobs"]["ple_job"]["status"] in ("RUNNING", "SUCCESS")

            await _wait_for_async(dispatched)
            resp = await _control_call(server.path, {"cmd": "status", "job": "ple_job"})
            jobs = resp["jobs"]["ple_job"]
            assert jobs["log_out"] == str(run_root / "logs" / "ple_job.1.out")
            assert jobs["log_err"] == str(run_root / "logs" / "ple_job.1.err")
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_log_paths_explicit_std_out_file_honored_per_stream(
    short_root: Path, tmp_path: Path
) -> None:
    """(runner-design ss6/ss11, DL-46): an explicit std_out_file is honored
    verbatim; job_log_paths() resolves EACH stream independently, so an
    unset std_err_file still falls back to its own default path rather
    than going along with the explicit std_out_file."""
    run_root = short_root / "run"
    out_file = tmp_path / "custom.out"
    text = f"insert_job: pls_job\njob_type: c\ncommand: x\nmachine: m1\nstd_out_file: {out_file}\n"

    async def scenario() -> None:
        adapter = FakeAdapter({("pls_job", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(run_root, text, adapter=adapter)
        try:
            await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "pls_job"}
            )

            async def dispatched() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "pls_job"})
                return r["jobs"]["pls_job"]["status"] in ("RUNNING", "SUCCESS")

            await _wait_for_async(dispatched)
            resp = await _control_call(server.path, {"cmd": "status", "job": "pls_job"})
            jobs = resp["jobs"]["pls_job"]
            assert jobs["log_out"] == str(out_file)
            assert jobs["log_err"] == str(run_root / "logs" / "pls_job.1.err")
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_log_paths_never_ran_cmd_without_std_files_is_null(short_root: Path) -> None:
    """(runner-design ss11, DL-46): a never-started CMD job with no explicit
    std_out_file/std_err_file has nothing to tail -- both fields are null,
    never a guessed run_number-1 path."""
    text = "insert_job: plv_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine, server, loop_task = await _serve(short_root / "run", text)
        try:
            resp = await _control_call(server.path, {"cmd": "status", "job": "plv_job"})
            jobs = resp["jobs"]["plv_job"]
            assert jobs["log_out"] is None
            assert jobs["log_err"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(scenario())


def test_status_log_paths_non_cmd_jobs_are_null(short_root: Path, tmp_path: Path) -> None:
    """(runner-design ss11, DL-46): log_out/log_err are CMD-only -- a BOX
    (no exec spec at all) and an FW job (its own watch_file, not a ss6
    append target) both report null, running or not."""
    box_root = short_root / "run_box"
    box_text = (
        "insert_job: plb_box\njob_type: b\n\n"
        "insert_job: plb_mem\njob_type: c\ncommand: x\nmachine: m1\nbox_name: plb_box\n"
    )

    async def box_scenario() -> None:
        adapter = FakeAdapter({("plb_mem", 1): (0.05, 0)}, default=None)
        engine, server, loop_task = await _serve(box_root, box_text, adapter=adapter)
        try:
            await _control_call(
                server.path, {"cmd": "sendevent", "event": "STARTJOB", "job": "plb_box"}
            )

            async def box_running() -> bool:
                r = await _control_call(server.path, {"cmd": "status", "job": "plb_box"})
                return r["jobs"]["plb_box"]["status"] != "INACTIVE"

            await _wait_for_async(box_running)
            resp = await _control_call(server.path, {"cmd": "status", "job": "plb_box"})
            jobs = resp["jobs"]["plb_box"]
            assert jobs["log_out"] is None
            assert jobs["log_err"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(box_scenario())

    fw_root = short_root / "run_fw"
    watch_file = tmp_path / "watched.txt"
    fw_text = f"insert_job: plf_job\njob_type: f\nwatch_file: {watch_file}\nwatch_interval: 60\n"

    async def fw_scenario() -> None:
        engine, server, loop_task = await _serve(fw_root, fw_text)
        try:
            resp = await _control_call(server.path, {"cmd": "status", "job": "plf_job"})
            jobs = resp["jobs"]["plf_job"]
            assert jobs["log_out"] is None
            assert jobs["log_err"] is None
        finally:
            await _teardown(engine, server, loop_task)

    asyncio.run(fw_scenario())
